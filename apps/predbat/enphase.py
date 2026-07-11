# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Enphase Enlighten cloud component
#
# Talks to the unofficial Enphase Enlighten web-app API (the same endpoints the
# Enlighten web/mobile apps use). There is no official API with battery control.
# Reference behaviour derived from https://github.com/barneyonline/ha-enphase-energy
# -----------------------------------------------------------------------------

"""Enphase Enlighten cloud API client component.

Talks to the unofficial Enphase Enlighten web-app API used by the Enlighten
web/mobile apps, since there is no official API offering battery control.
This module currently provides the component skeleton: initialisation,
cache load/save helpers and staleness checks. Login, data reads, sensor
publishing and battery control are added by later tasks.
"""

from datetime import datetime, timedelta, timezone
import asyncio
import base64
import json
import random
import uuid

import aiohttp

from component_base import ComponentBase
from predbat_metrics import record_api_call

# Defined locally (not imported from utils) - every cloud component defines its own
# copy of this table rather than sharing one, matching the pattern used by fox.py.
BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME_FULL = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M") + ":00") for minute in range(0, 24 * 60, 1)]

BASE_URL = "https://enlighten.enphaseenergy.com"
LOGIN_PATH = "/login/login.json"
SELF_TOKEN_PATH = "/users/self/token"
SITE_SEARCH_PATH = "/app-api/search_sites.json"
BATTERY_CONFIG_BASE = "/service/batteryConfig/api/v1"

# Refresh ages in minutes for each data category
ENPHASE_REFRESH_STATIC = 24 * 60
ENPHASE_REFRESH_SETTINGS = 5
ENPHASE_REFRESH_ENERGY = 15
ENPHASE_REFRESH_POWER = 1

ENPHASE_CACHE_KEYS = ["sites", "battery_status", "battery_settings", "profile", "schedules", "site_settings", "lifetime_energy", "latest_power"]
ENPHASE_CACHE_VERSION = 1

# Battery profiles accepted by the profile endpoint
PROFILE_SELF_CONSUMPTION = "self-consumption"
PROFILE_COST_SAVINGS = "cost_savings"
PROFILE_BACKUP_ONLY = "backup_only"

# Schedule families
SCHEDULE_CHARGE = "CFG"  # charge from grid
SCHEDULE_EXPORT = "DTG"  # discharge to grid
SCHEDULE_FREEZE = "RBD"  # restrict battery discharge

ENPHASE_RETRIES = 5

# Browser mimicry - Enlighten rejects non-browser requests with 406/login walls
ENPHASE_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
BATTERY_UI_ORIGIN = "https://battery-profile-ui.enphaseenergy.com"


def decode_jwt_claims(token):
    """Decode the payload segment of a JWT without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError):
        return {}


class EnphaseAPI(ComponentBase):
    """Enphase Enlighten cloud API client component."""

    # Login guard-rail tunables - protect the Enphase account from lockout
    LOGIN_REUSE_SECONDS = 30
    LOGIN_COOLDOWN_SECONDS = 300
    LOGIN_SUSPEND_SECONDS = 24 * 3600
    LOGIN_MAX_REJECTS = 3

    def initialize(self, username, password, site_id=None, automatic=False, automatic_ignore_pv=False):
        """Initialise the Enphase API component state."""
        self.username = username
        self.password = password
        self.site_id = str(site_id) if site_id else None
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv

        # Auth state
        self.cookie_header = ""  # serialised cookie header for Enlighten
        self.eauth_token = None  # JWT from /users/self/token
        self.manager_token = None  # enlighten_manager_token_production cookie JWT
        self.xsrf_token = None
        self.user_id = None  # decoded from JWT, needed by BatteryConfig
        self.token_expires_at = None

        # Login guard rails (avoid Enphase account lockout)
        self.login_last_success = None  # datetime of last successful login
        self.login_cooldown_until = None  # datetime before which logins are banned
        self.login_reject_count = 0  # consecutive rejected logins

        # Cloud data
        self.sites = []
        self.battery_status = {}
        self.battery_settings = {}
        self.profile = {}
        self.schedules = {}
        self.site_settings = {}
        self.lifetime_energy = {}
        self.latest_power = {}

        # Local (HA-side) schedule model, written by events, applied on write switch
        self.local_schedule = {}

        # Derived power state: previous cumulative kWh samples per channel
        self.prev_energy_sample = {}

        # Pending writes awaiting cloud settle confirmation
        self.pending_writes = {}

        # BatteryConfig header variant: "primary" (e-auth-token + requestid) or
        # "cookie_eauth" fallback (cookie + XHR header) needed on some regions/firmware
        self.battery_config_variant = "primary"

        # Age (datetime of last update) per cached data category
        self.data_age = {}
        self.failures_total = 0
        self.requests_today = 0
        self.last_midnight_utc = None
        self.last_error_status = None  # HTTP status (or None) of the most recent request_json() failure

    def is_alive(self):
        """Return True when the component has started and discovered a site."""
        return self.api_started and bool(self.sites)

    def _data_age_minutes(self, key):
        """Return the age in minutes of the in-memory data for a cache key, or None if unknown."""
        timestamp = self.data_age.get(key, None)
        if timestamp is None:
            return None
        return (datetime.now(timezone.utc) - timestamp).total_seconds() / 60.0

    def _needs_refresh(self, key, max_age_minutes):
        """Return True if the data for a cache key is missing or older than max_age_minutes."""
        age = self._data_age_minutes(key)
        return age is None or age >= max_age_minutes

    async def _save_cache(self, key, data):
        """Save data to storage under the enphase module and record its update time."""
        now = datetime.now(timezone.utc)
        self.data_age[key] = now
        if self.storage:
            await self.storage.save("enphase", key, data, format="json", expiry=now + timedelta(days=1))

    async def _load_cache(self, key):
        """Load cached data for a key from storage, recording its age. Returns None if absent."""
        if not self.storage:
            return None
        data = await self.storage.load("enphase", key)
        if data is None:
            return None
        age = await self.storage.age("enphase", key)
        if age is None:
            return None
        self.data_age[key] = datetime.now(timezone.utc) - timedelta(minutes=age)
        return data

    async def load_cached_data(self):
        """Restore cached cloud data from storage on startup to avoid re-polling after a reboot."""
        if not self.storage:
            return
        version = await self.storage.load("enphase", "cache_version")
        if version != ENPHASE_CACHE_VERSION:
            self.log("Enphase: Cache version changed, forcing full refresh")
            await self.storage.save("enphase", "cache_version", ENPHASE_CACHE_VERSION, format="json")
            return
        for key in ENPHASE_CACHE_KEYS:
            data = await self._load_cache(key)
            if data is not None:
                setattr(self, key, data)
        if self.sites:
            self.update_success_timestamp()

    async def run(self, seconds, first):
        """Main polling body, invoked every 60 seconds by ComponentBase."""
        if first:
            await self.load_cached_data()
        # Later tasks fill in: login, per-tier refresh, publishing
        return True

    def login_allowed(self):
        """Return True when a password login attempt is currently permitted by the guard rails."""
        if self.login_cooldown_until and datetime.now(timezone.utc) < self.login_cooldown_until:
            return False
        return True

    def _login_rejected(self, reason, unrecoverable=False):
        """Record a rejected login and set the appropriate cooldown.

        Fatal (app-wide) error signalling is reserved for genuinely unrecoverable
        states (MFA required, account blocked) or once the suspend tier is reached
        after repeated transient rejections - a single 401/403/no-token/session
        rejection must not mark the whole app as not-running.
        """
        self.login_reject_count += 1
        if self.login_reject_count >= self.LOGIN_MAX_REJECTS:
            delay = self.LOGIN_SUSPEND_SECONDS
        else:
            delay = self.LOGIN_COOLDOWN_SECONDS
        self.login_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.log(f"Warn: Enphase: Login rejected ({reason}), cooling down for {delay} seconds (rejection {self.login_reject_count})")
        if unrecoverable or self.login_reject_count >= self.LOGIN_MAX_REJECTS:
            self.fatal_error_occurred()

    async def login(self):
        """Authenticate with Enlighten: password login, token mint, site discovery."""
        # Reuse a very recent successful login (coalesces concurrent 401 refreshes)
        if self.login_last_success and (datetime.now(timezone.utc) - self.login_last_success).total_seconds() < self.LOGIN_REUSE_SECONDS and self.eauth_token:
            return True
        if not self.login_allowed():
            self.log("Warn: Enphase: Login suppressed by cooldown after previous rejections")
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": ENPHASE_USER_AGENT,
            "Referer": BASE_URL + "/",
        }
        status, data, text, cookies = await self.request_raw("POST", BASE_URL + LOGIN_PATH, headers=headers, data={"user[email]": self.username, "user[password]": self.password})

        if status in (401, 403):
            self._login_rejected("invalid credentials")
            return False
        if isinstance(data, dict) and data.get("requires_mfa"):
            self._login_rejected("account requires MFA - disable MFA on the Enphase account to use this component", unrecoverable=True)
            return False
        if isinstance(data, dict) and data.get("isBlocked"):
            self._login_rejected("account is blocked", unrecoverable=True)
            return False
        if "session" in str(text).lower():
            # "Too many active sessions" - detect regardless of HTTP status, Enlighten sometimes returns 200
            self._login_rejected("too many active sessions")
            return False
        if status != 200:
            self._login_rejected(f"http status {status}")
            return False

        # Persist cookies from the login (session cookie + manager token JWT)
        self._absorb_cookies(cookies)

        # Mint the e-auth/bearer token; Enlighten may rotate the session cookie here
        status, token_data, text, cookies = await self.request_raw("GET", BASE_URL + SELF_TOKEN_PATH, headers=self.get_headers("site"))
        self._absorb_cookies(cookies)
        if status == 200 and isinstance(token_data, dict):
            token = token_data.get("token") or token_data.get("auth_token") or token_data.get("access_token")
            if token:
                self.eauth_token = token
                claims = decode_jwt_claims(token)
                self.user_id = str(claims.get("user_id") or claims.get("userId") or claims.get("sub") or "") or None
                self.token_expires_at = token_data.get("expires_at") or token_data.get("expiresAt") or claims.get("exp")
        if not self.eauth_token:
            self._login_rejected("no auth token returned")
            return False

        # Discover sites
        status, sites_data, text, cookies = await self.request_raw("GET", BASE_URL + SITE_SEARCH_PATH, headers=self.get_headers("site"), params={"searchText": "", "favourite": "false"})
        sites = []
        if status == 200:
            entries = sites_data if isinstance(sites_data, list) else (sites_data or {}).get("sites", [])
            for entry in entries:
                sid = str(entry.get("site_id") or entry.get("id") or "")
                if sid and (not self.site_id or sid == self.site_id):
                    sites.append({"site_id": sid, "name": entry.get("name", sid)})
        if sites:
            self.sites = sites
            await self._save_cache("sites", sites)

        self.login_last_success = datetime.now(timezone.utc)
        self.login_reject_count = 0
        self.login_cooldown_until = None
        self.log(f"Enphase: Login successful, {len(self.sites)} site(s)")
        return True

    def _absorb_cookies(self, cookies):
        """Merge response cookies into the serialised cookie header and pick out special tokens."""
        if not cookies:
            return
        current = {}
        for part in self.cookie_header.split("; "):
            if "=" in part:
                name, value = part.split("=", 1)
                current[name] = value
        current.update(cookies)
        self.cookie_header = "; ".join(f"{k}={v}" for k, v in current.items() if v)
        self.manager_token = current.get("enlighten_manager_token_production", self.manager_token)
        self.xsrf_token = current.get("XSRF-TOKEN", current.get("BP-XSRF-Token", self.xsrf_token))

    def get_headers(self, family, write=False):
        """Build request headers for an endpoint family ('site' or 'battery_config')."""
        if family == "battery_config":
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": BATTERY_UI_ORIGIN,
                "Referer": BATTERY_UI_ORIGIN + "/",
                "User-Agent": ENPHASE_USER_AGENT,
                "e-auth-token": self.eauth_token or "",
            }
            if self.battery_config_variant == "cookie_eauth":
                # Fallback variant needed on some regions/firmware: cookie-backed with XHR marker
                headers["X-Requested-With"] = "XMLHttpRequest"
                if self.cookie_header:
                    headers["Cookie"] = self.cookie_header
            else:
                headers["requestid"] = str(uuid.uuid4())
            bearer = self.manager_token or self.eauth_token
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            if self.user_id:
                headers["Username"] = self.user_id
            if write:
                headers["Content-Type"] = "application/json"
                if self.xsrf_token:
                    headers["X-XSRF-Token"] = self.xsrf_token
            return headers

        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": ENPHASE_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL + "/",
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if self.eauth_token:
            headers["Authorization"] = f"Bearer {self.eauth_token}"
            headers["e-auth-token"] = self.eauth_token
        if self.xsrf_token:
            headers["X-CSRF-Token"] = self.xsrf_token
        return headers

    async def request_raw(self, method, url, headers=None, data=None, json_body=None, params=None):
        """Perform one HTTP request, returning (status, json_or_none, text, cookie_dict). Overridden in tests."""
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, data=data, json=json_body, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                text = await response.text()
                cookies = {key: morsel.value for key, morsel in response.cookies.items()}
                json_data = None
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    try:
                        json_data = await response.json(content_type=None)
                    except ValueError:
                        json_data = None
                return response.status, json_data, text, cookies

    def _is_login_wall(self, json_data, text):
        """Return True when a JSON endpoint answered with an HTML login page instead of JSON.

        Enlighten sometimes responds to an expired/invalid session with a 200 status and an
        HTML login page body rather than a 401, so this must be checked independently of status.
        """
        if json_data is not None:
            return False
        stripped = (text or "").lstrip().lower()
        return stripped.startswith("<!doctype") or stripped.startswith("<html")

    async def request_json(self, method, path, family="site", json_body=None, data=None, params=None):
        """Perform an authenticated JSON request with retries and a single 401 re-login.

        Builds the request URL from BASE_URL + path and attaches family-appropriate headers
        via get_headers(). Handles failure modes as follows:
        - A 401/403 status, or an HTML login-wall body on what should be a JSON endpoint, is
          treated as an auth failure: for the "battery_config" family this first tries the
          "cookie_eauth" header variant fallback (without consuming the single re-login
          attempt); otherwise it performs one login() and retries once.
        - HTTP 429 and 5xx responses, plus timeouts/connection errors, are retried with
          jittered backoff up to ENPHASE_RETRIES times.
        - Any other non-200 status is treated as a terminal failure.
        Every outcome is recorded via record_api_call("enphase", ...) for metrics/health.
        Returns the parsed JSON body on success, or None on failure (self.last_error_status is
        set to the last HTTP status seen, where available).
        """
        url = BASE_URL + path
        relogin_done = False
        self.last_error_status = None
        for retry in range(ENPHASE_RETRIES):
            headers = self.get_headers(family, write=(method != "GET"))
            try:
                status, json_data, text, cookies = await self.request_raw(method, url, headers=headers, data=data, json_body=json_body, params=params)
            except (asyncio.TimeoutError, aiohttp.ClientError) as error:
                self.log(f"Warn: Enphase: Request error on {path}: {error}")
                record_api_call("enphase", False, "connection_error")
                await asyncio.sleep(1 + retry * random.random() * 5)
                continue

            self.requests_today += 1
            auth_failed = status in (401, 403) or self._is_login_wall(json_data, text)
            if auth_failed:
                record_api_call("enphase", False, "auth_error")
                if family == "battery_config" and self.battery_config_variant == "primary":
                    # Some regions/firmware reject the primary BatteryConfig header shape;
                    # switch to the cookie-backed fallback variant before burning a re-login.
                    self.log("Enphase: BatteryConfig auth failed, switching to cookie header variant")
                    self.battery_config_variant = "cookie_eauth"
                    continue
                if relogin_done or not await self.login():
                    self.last_error_status = status
                    self.failures_total += 1
                    return None
                relogin_done = True
                continue

            if status == 429 or status >= 500:
                record_api_call("enphase", False, "rate_limit" if status == 429 else "server_error")
                await asyncio.sleep(min(30, (retry + 1) * (2 + random.random() * 3)))
                continue

            if status != 200:
                self.log(f"Warn: Enphase: HTTP {status} on {path}")
                record_api_call("enphase", False, "client_error")
                self.last_error_status = status
                self.failures_total += 1
                return None

            record_api_call("enphase", True)
            self.update_success_timestamp()
            return json_data

        self.failures_total += 1
        return None
