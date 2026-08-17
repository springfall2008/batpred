# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sunsynk Cloud API Library
# -----------------------------------------------------------------------------

"""Sunsynk Connect cloud API integration for Predbat.

Registers each discovered Sunsynk inverter as a ``SunsynkCloud`` Predbat inverter,
publishing monitoring sensors and DEYE-style schedule control entities. Predbat drives
those entities through the generic Inverter class; this module derives the Sunsynk work
mode internally and applies it by read-modify-write of the whole settings object.

Three auth methods: ``password`` (RSA-encrypted login, the default), ``password_legacy``
(the pre-2025 plaintext login, opt-in) and ``oauth`` (token injected by Predbat.com).
The RSA path never downgrades to the plaintext one — see ``fetch_token``.
"""

import asyncio
import hashlib
import json
import time
import aiohttp
from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from sunsynk_const import (
    SUNSYNK_REGIONS,
    SUNSYNK_ENDPOINTS,
    SUNSYNK_TIMEOUT,
    SUNSYNK_RETRIES,
    SUNSYNK_CLIENT_ID,
    SUNSYNK_AUTH_ERROR_MARKERS,
    SUNSYNK_DEBUG_MAX_CHARS,
    SUNSYNK_DEBUG_REDACT_KEYS,
    rsa_encrypt_pkcs1v15,
)


class SunsynkAPI(ComponentBase, OAuthMixin):
    """Sunsynk Connect cloud API component."""

    # Trace every API request/response while the Sunsynk integration beds in. Nobody on
    # the project has a Sunsynk account, so a tester's log is the only evidence available
    # for the inferred wire format; flip to False once the format is confirmed.
    api_debug = True

    def initialize(
        self,
        username="",
        password="",
        key="",
        region="sunsynk",
        auth_method="password",
        token_expires_at=None,
        token_hash="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=False,
        battery_nominal_voltage=None,
        **kwargs,
    ):
        """Initialise the Sunsynk component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has
        already resolved each arg from its sunsynk_* config key and passes it BY ARG NAME
        (e.g. region <- sunsynk_region), exactly like fox/deye/enphase. Consume the kwargs
        directly — do NOT re-derive with get_arg("region"): that bare name is not in
        apps.yaml (the key is sunsynk_region), so it would always return the default.
        """
        self.log("Info: SunsynkAPI initialising")
        self.username = username
        self.password = password
        self.region = region or "sunsynk"
        self.auth_method = auth_method or "password"
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_nominal_voltage = float(battery_nominal_voltage) if battery_nominal_voltage else 0.0
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_capacity = {}
        self.device_pack_voltage = {}
        self.device_energy = {}
        self.device_rated_power = {}
        self.local_schedule = {}
        self.applied_payload = {}
        self.settle_count = {}
        self.control_active = set()
        self.cached_values = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        self._soc_floor_warned = set()
        if self.region not in SUNSYNK_REGIONS:
            self.log(f"Warn: Sunsynk unknown region '{self.region}', falling back to sunsynk")
            self.region = "sunsynk"
        if self.auth_method == "password_legacy":
            self.log("Warn: Sunsynk auth_method 'password_legacy' sends your password in plaintext over TLS. It exists for regions still on the pre-2025 login; prefer 'password' where it works.")
        if not self.control_enable:
            self.log("Info: Sunsynk control is disabled (sunsynk_control_enable is false); monitoring only. The write format has not been confirmed against live hardware.")
        # Pass auth_method STRAIGHT THROUGH, exactly as deye.py does. _init_oauth owns
        # self.auth_method (`self.auth_method = auth_method or "api_key"`, oauth_mixin.py),
        # so collapsing the three modes to two here would rewrite "password_legacy" to
        # "password" and make the plaintext login silently unreachable — fetch_token would
        # take the RSA branch for a user who deliberately asked for the legacy one.
        # _init_oauth leaves access_token None for every non-oauth value, which is right
        # for both self-hosted modes: they obtain their token in fetch_token.
        self._init_oauth(auth_method=self.auth_method, key=key, token_expires_at=token_expires_at, provider_name="sunsynk")
        # _init_oauth resets token_hash to "" (see oauth_mixin.py), so a configured value
        # must be applied AFTER it, exactly as fox.py and deye.py do — otherwise the
        # Predbat.com SaaS refresh dedup keyed on it breaks.
        self.token_hash = token_hash

    @property
    def base_url(self):
        """Return the API host for the configured region."""
        return SUNSYNK_REGIONS.get(self.region, SUNSYNK_REGIONS["sunsynk"])["host"]

    @property
    def source(self):
        """Return the 'source' token this region's login signs with."""
        return SUNSYNK_REGIONS.get(self.region, SUNSYNK_REGIONS["sunsynk"])["source"]

    def _auth_headers(self):
        """Return the bearer headers used for every authenticated call."""
        return {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.access_token}"}

    @staticmethod
    def redact(payload):
        """Return payload with credential-bearing keys replaced, for safe logging."""
        if not isinstance(payload, dict):
            return payload
        return {key: ("<redacted>" if key in SUNSYNK_DEBUG_REDACT_KEYS else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response when api_debug is on, with secrets redacted."""
        if not self.api_debug:
            return
        if payload is None:
            self.log(f"Debug: Sunsynk {direction} {what}")
            return
        text = json.dumps(self.redact(payload), default=str)
        if len(text) > SUNSYNK_DEBUG_MAX_CHARS:
            text = text[:SUNSYNK_DEBUG_MAX_CHARS] + f"... (truncated, {len(text)} chars)"
        self.log(f"Debug: Sunsynk {direction} {what} {text}")

    @staticmethod
    def is_auth_error_body(data):
        """Return True if a 200-with-failure body means the token needs refreshing.

        Sunsynk does not answer an expired token with HTTP 401 — it returns HTTP 200
        carrying {"success": false, "msg": "..."}. Status-code-only handling therefore
        never triggers a refresh and the component stays broken until a restart.
        """
        if not isinstance(data, dict) or data.get("success"):
            return False
        message = str(data.get("msg", "")).lower()
        return any(marker in message for marker in SUNSYNK_AUTH_ERROR_MARKERS)

    @staticmethod
    def _nonce():
        """Return the millisecond nonce Sunsynk's login signs over."""
        return int(time.time() * 1000)

    def _sign(self, nonce, suffix):
        """Return the md5 signature Sunsynk's login expects for a nonce and suffix."""
        return hashlib.md5(f"nonce={nonce}&source={self.source}{suffix}".encode("utf-8")).hexdigest()

    async def fetch_public_key(self):
        """Fetch the RSA public key the password is encrypted with. Returns "" on failure."""
        nonce = self._nonce()
        params = {"source": self.source, "nonce": str(nonce), "sign": self._sign(nonce, "POWER_VIEW")}
        data = await self._request("GET", "public_key", params=params)
        # This endpoint returns the key as a bare string in `data`, not a dict.
        return data if isinstance(data, str) else ""

    async def fetch_token(self):
        """Log in by the configured method and store the access token. Returns True on success.

        The RSA path NEVER falls back to the plaintext one. Auto-downgrade would convert
        any externally-triggerable failure of the public-key step into a plaintext
        credential transmission, and against a TLS-intercepting middlebox the RSA layer is
        the only thing protecting the password. A failure logs a diagnostic pointing at
        sunsynk_auth_method: password_legacy so the user can choose it deliberately.
        """
        if self.auth_method == "oauth":
            # OAuthMixin already holds the injected token; nothing to log in with.
            return bool(self.access_token)

        if self.auth_method == "password_legacy":
            body = {"areaCode": "sunsynk", "client_id": SUNSYNK_CLIENT_ID, "grant_type": "password", "password": self.password, "source": self.source, "username": self.username}
            data = await self._request("POST", "token_legacy", body=body)
        else:
            public_key = await self.fetch_public_key()
            if not public_key:
                self.log("Warn: Sunsynk could not fetch the login public key, so the RSA login cannot proceed. It is NOT retried in plaintext. If your region still serves the older login, set sunsynk_auth_method: password_legacy in apps.yaml.")
                return False
            try:
                encrypted = rsa_encrypt_pkcs1v15(public_key, self.password)
            except ValueError as error:
                self.log(f"Warn: Sunsynk could not encrypt the password for login: {error}")
                return False
            nonce = self._nonce()
            body = {
                "client_id": SUNSYNK_CLIENT_ID,
                "grant_type": "password",
                "password": encrypted,
                "source": self.source,
                "username": self.username,
                "nonce": nonce,
                "sign": self._sign(nonce, public_key[:10]),
            }
            data = await self._request("POST", "token", body=body)

        token = (data or {}).get("access_token")
        if not token:
            self.log("Warn: Sunsynk login did not return an access token; check the username and password")
            return False
        self.access_token = token
        self.refresh_token = data.get("refresh_token", "")
        expires_in = data.get("expires_in")
        if expires_in:
            self.token_expires_at = time.time() + float(expires_in)
        self.log("Info: Sunsynk login succeeded")
        return True

    async def _request(self, method, endpoint_key, sn=None, params=None, body=None):
        """Perform one API call, returning the response's `data` payload, or None on failure.

        Retries transport errors and non-200 responses with backoff, and treats a
        body-level auth failure as a token refresh followed by exactly one retry.

        Failure is None, NOT {} — a successful call can legitimately carry no data (the
        settings write is one), so {} would be ambiguous between "worked, nothing to
        return" and "failed", and a write failure would be silently read as success.
        Read callers go through _get, which coerces None to {} because they all want a
        dict; the write path uses the None directly to detect failure. Never raises, so
        every caller fails closed.
        """
        path = SUNSYNK_ENDPOINTS[endpoint_key]
        if sn:
            path = path.format(sn=sn)
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=SUNSYNK_TIMEOUT)
        # Login endpoints are unauthenticated; everything else carries the bearer token.
        anonymous = endpoint_key in ("public_key", "token", "token_legacy")
        headers = {"Accept": "application/json", "Content-Type": "application/json"} if anonymous else self._auth_headers()
        refreshed = False

        for attempt in range(SUNSYNK_RETRIES):
            self.debug_api(method, url, body if body is not None else params)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers, params=params, json=body) as response:
                        if response.status != 200:
                            self.log(f"Warn: Sunsynk {method} {path} returned HTTP {response.status}")
                            await asyncio.sleep(2**attempt)
                            continue
                        payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                self.log(f"Warn: Sunsynk {method} {path} failed: {error}")
                await asyncio.sleep(2**attempt)
                continue

            self.debug_api("<-", url, payload if isinstance(payload, dict) else {"data": payload})
            if self.is_auth_error_body(payload) and not anonymous and not refreshed:
                refreshed = True
                self.log(f"Info: Sunsynk {path} reported an auth failure, refreshing the token")
                if await self.fetch_token():
                    headers = self._auth_headers()
                    continue
                return None
            if not isinstance(payload, dict) or not payload.get("success"):
                message = payload.get("msg") if isinstance(payload, dict) else payload
                self.log(f"Warn: Sunsynk {method} {path} was unsuccessful: {message}")
                return None
            data = payload.get("data")
            return data if data is not None else {}

        return None

    async def _get(self, endpoint_key, sn=None, params=None):
        """GET one endpoint, returning its `data` payload, or {} on failure.

        Read callers all want a dict and treat an empty one as "nothing usable", so the
        None that _request reports on failure is coerced here.
        """
        result = await self._request("GET", endpoint_key, sn=sn, params=params)
        return result if result is not None else {}

    async def _post(self, endpoint_key, sn=None, body=None):
        """POST one endpoint, returning its `data` payload, or None on failure.

        None is passed straight through, unlike _get: the settings write succeeds with no
        data payload, so only None can distinguish a failed write from a successful one.
        """
        return await self._request("POST", endpoint_key, sn=sn, body=body)
