# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sunsynk Connect API Library
# -----------------------------------------------------------------------------

"""Sunsynk Connect Cloud API integration.

Injected-token Bearer client for Sunsynk Connect inverters. Predbat.com's SaaS edge
functions own the Sunsynk RSA publicKey -> login -> access-token exchange and refresh
chain server-side; the ``sunsynk_key`` config value handed to this component IS the
resulting Bearer access token, so ``SunsynkAPI`` never performs RSA signing or login
itself here - it only issues plain Bearer-authenticated requests and flags
``needs_reauth`` when the injected token is rejected, for the SaaS side to notice and
refresh. Device discovery, publishing and control are added in later tasks.
"""

import asyncio
import json
import random
from datetime import datetime

import aiohttp

from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from predbat_metrics import record_api_call

SUNSYNK_DOMAIN = "https://api.sunsynk.net"
TIMEOUT = 60
SUNSYNK_RETRIES = 10

# Maximum age (minutes) of cached data before an API refresh is triggered. Not yet consumed
# by this skeleton - device discovery/settings/real-time polling land in later tasks - but
# defined here now so the refresh cadence is fixed once, matching fox.py's FOX_REFRESH_*
# constants.
SUNSYNK_REFRESH_STATIC = 24 * 60  # Device/plant list rarely changes
SUNSYNK_REFRESH_SETTINGS = 60  # Device settings
SUNSYNK_REFRESH_REALTIME = 5  # Real time monitoring data

# Storage cache keys for device data persisted between reboots. Populated by later tasks
# (device discovery, settings, real-time values); listed now so the cache key namespace is
# fixed from the start, matching fox.py's FOX_CACHE_KEYS.
SUNSYNK_CACHE_KEYS = ["device_list", "device_settings", "device_values"]


class SunsynkAPI(ComponentBase, OAuthMixin):
    """Sunsynk Connect API client."""

    def initialize(self, key, automatic, automatic_ignore_pv=False, inverter_sn=None, auth_method=None, token_expires_at=None, token_hash=None, base_url=SUNSYNK_DOMAIN):
        """Initialise the Sunsynk API component."""
        self.key = key
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.base_url = base_url
        self.failures_total = 0
        # Set on a 401/403 so callers (and the SaaS oauth-refresh edge function, which owns
        # the RSA/login chain) can notice the injected token was rejected; cleared again on
        # the next successful request. This component never attempts a re-login itself.
        self.needs_reauth = False

        # Cache state for later tasks (device discovery/settings/real-time values) - empty
        # here since this skeleton does not poll anything yet.
        self.device_list = []
        self.device_values = {}
        self.device_settings = {}
        self.data_age = {}

        # Initialise OAuth bookkeeping (expiry tracking, provider_name etc.) for consistency
        # with fox.py/deye.py, even though Sunsynk's injected-token model means refreshing is
        # done entirely server-side rather than via check_and_refresh_oauth_token().
        self._init_oauth(auth_method=auth_method, key=key, token_expires_at=token_expires_at, provider_name="sunsynk")
        # _init_oauth() only sets self.access_token when auth_method == "oauth" (None
        # otherwise) - but 'key' IS the Bearer access token here regardless of auth_method,
        # so it must be (re)applied after _init_oauth or get_headers() would send "Bearer
        # None" whenever auth_method isn't "oauth".
        self.access_token = key
        # _init_oauth() also unconditionally resets token_hash to "" (see oauth_mixin.py) so
        # the configured value must be applied after it too, exactly as fox.py/deye.py do -
        # otherwise a configured hash is silently discarded and the Predbat.com SaaS dedup
        # keyed on it breaks.
        self.token_hash = token_hash or ""

        # Convert inverter_sn to list, same normalisation as fox.py/deye.py
        if inverter_sn is None:
            self.inverter_sn_filter = []
        elif isinstance(inverter_sn, str):
            self.inverter_sn_filter = [inverter_sn]
        else:
            self.inverter_sn_filter = inverter_sn

    def get_headers(self):
        """Return the plain Bearer authorisation headers used for every Sunsynk request.

        No request signing (unlike fox.py's MD5 signature) - Sunsynk reads use a plain
        Bearer token, and that token is injected/refreshed server-side rather than derived
        here.
        """
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def is_alive(self):
        """Check if the API is alive (component started and holding a usable token)."""
        return self.api_started and bool(self.access_token)

    async def request_get(self, path, post=False, datain=None):
        """
        Retry wrapper around request_get_func.
        """
        retries = 0
        result = None
        self.log("Sunsynk: API Requesting {} {} - data {}".format("POST" if post else "GET", path, datain))

        while retries < SUNSYNK_RETRIES:
            result, allow_retry = await self.request_get_func(path, post=post, datain=datain)
            if result is not None:
                return result
            if not allow_retry:
                break
            retries += 1
            await asyncio.sleep(retries * random.random())
        self.log("Sunsynk: API Response failed after {} retries for {}".format(SUNSYNK_RETRIES, path))
        return result

    async def request_get_func(self, path, post=False, datain=None):
        """
        Perform a single Sunsynk API request.

        Plain Bearer auth, no request signing. On 401/403 there is no re-login attempt -
        the access token is injected and refreshed server-side by the SaaS edge function, so
        a stale token here just sets self.needs_reauth for the caller to notice, rather than
        triggering a login flow this component doesn't implement.
        """
        headers = self.get_headers()
        url = self.base_url + path
        self.log("Sunsynk: API Request: path {} post {} datain {}".format(path, post, datain))

        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if post:
                    async with session.post(url, headers=headers, json=datain) as response:
                        status_code = response.status
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            self.log("Warn: Sunsynk: Failed to decode response from {} code {}".format(url, status_code))
                            data = None
                else:
                    async with session.get(url, headers=headers, params=datain) as response:
                        status_code = response.status
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            self.log("Warn: Sunsynk: Failed to decode response from {} code {}".format(url, status_code))
                            data = None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Sunsynk: Exception during request to {url}: {e}")
            self.failures_total += 1
            record_api_call("sunsynk", False, "connection_error")
            return None, False

        if status_code in (401, 403):
            # Injected-token model: no re-login here, just flag it for the caller/SaaS side.
            self.needs_reauth = True
            self.log("Warn: Sunsynk: Authentication error with status code {} from {}".format(status_code, url))
            self.failures_total += 1
            record_api_call("sunsynk", False, "auth_error")
            return None, False

        if status_code in (200, 201):
            self.needs_reauth = False
            if data is None:
                data = {}
            self.update_success_timestamp()
            record_api_call("sunsynk")
            return data, False

        self.failures_total += 1
        if status_code == 429:
            # Rate limiting so wait up to 30 seconds and allow a retry
            self.log("Info: Sunsynk: Rate limiting detected, waiting...")
            record_api_call("sunsynk", False, "rate_limit")
            await asyncio.sleep(random.random() * 30 + 1)
            return None, True
        record_api_call("sunsynk", False, "server_error")
        return None, False


class MockBase:  # pragma: no cover
    """Mock base class for standalone testing"""

    def __init__(self):
        """Initialise minimal base state (timezone, prefix, args, entities)."""
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return a stored entity's state (or the full record when raw=True)."""
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Store an entity's state and attributes."""
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        """Print a timestamped log message."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print and store a dashboard entity update."""
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if "options" in attributes:
                attributes["options"] = "..."
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None):
        """Return a config argument (always the default - no real config backing)."""
        return default

    def set_arg(self, key, value):
        """Print a config argument write for debugging."""
        state = None
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and "." in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")
