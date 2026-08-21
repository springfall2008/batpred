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
mode internally and applies it by read-modify-write of the System Mode settings group
(the write endpoint silently discards anything larger - see SUNSYNK_SYSTEM_MODE_FIELDS).

Three auth methods: ``password`` (RSA-encrypted login, the default), ``password_legacy``
(the pre-2025 plaintext login, opt-in) and ``oauth`` (token injected by Predbat.com).
The RSA path never downgrades to the plaintext one — see ``fetch_token``.
"""

import argparse
import asyncio
import hashlib
import json
import time
import aiohttp
from component_base import ComponentBase
from mock_base import MockBase
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
    SUNSYNK_PAGE_SIZE,
    SUNSYNK_MAX_DISCOVERY_PAGES,
    SUNSYNK_TELEMETRY,
    SUNSYNK_ENERGY,
    SUNSYNK_TELEMETRY_NEGATE,
    SUNSYNK_CAPACITY_AH_FIELD,
    SUNSYNK_CHARGE_VOLT_FIELD,
    SUNSYNK_CHARGE_CURRENT_FIELDS,
    SUNSYNK_EXPORT_LIMIT_FIELD,
    SUNSYNK_RATED_POWER_FIELD,
    SUNSYNK_BATTERY_LOW_CAP_FIELD,
    LIFEPO4_CELL_COUNTS,
    LIFEPO4_CHARGE_VOLTS_MIN,
    LIFEPO4_CHARGE_VOLTS_MAX,
    LIFEPO4_CHARGE_VOLTS_TYPICAL,
    LIFEPO4_NOMINAL_VOLTS_PER_CELL,
    SUNSYNK_WORKMODE,
    SUNSYNK_WORKMODE_FIELD,
    SUNSYNK_SOLAR_SELL_FIELD,
    SUNSYNK_TOU_ENABLE_FIELD,
    SUNSYNK_SERIAL_FIELD,
    SUNSYNK_SYSTEM_MODE_FIELDS,
    SUNSYNK_DERIVED_SLOT_FIELDS,
    SUNSYNK_DAY_FIELDS,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    TOU_FILLER_TIMES,
    FREEZE_EXPORT_SOC,
    SUNSYNK_SETTLE_POLLS,
    SUNSYNK_TTL_STATIC,
    SUNSYNK_TTL_CONFIG,
    SUNSYNK_TTL_LIVE,
    SUNSYNK_STORAGE_MODULE,
    SUNSYNK_CACHE_STATIC,
    SUNSYNK_CACHE_CONFIG,
    SUNSYNK_CACHE_RATINGS,
    SUNSYNK_CACHE_CONTROL,
    SUNSYNK_RESTORE_MAX_CONTROL,
    encode_setting,
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
        control_enable=True,
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
        self.device_energy = {}
        self.device_rated_power = {}
        self.local_schedule = {}
        self.applied_payload = {}
        self.settle_count = {}
        # Serials Predbat has actually been asked to drive, i.e. ones whose write button
        # has been pressed at least once. run() only re-applies a schedule for these, so a
        # startup cycle can never clobber an inverter before there is a plan to apply.
        self.control_active = set()
        self._tier_refreshed = {}
        self._cache_restored = False
        self._soc_floor_warned = set()
        # The most recent body-level API failure message (the `msg` field only - see
        # _request - never a credential), and whether the last discovery attempt actually
        # reached the API. Both exist so the standalone CLI (test_sunsynk_api) can name
        # precisely which stage failed instead of dumping all possibilities and pointing at
        # the Warn: trace.
        self.last_api_error = ""
        self.discovery_ok = None
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
        """Return payload with credential-bearing keys replaced, for safe logging.

        Recursive over dicts AND lists, exactly as deye.py's redact is. A top-level-only
        rewrite is not enough: _request traces the whole {code, msg, success, data}
        envelope, and the login response nests access_token/refresh_token one level down
        inside `data`, so a Sunsynk bearer token — full control of the user's inverter —
        was written to the log verbatim. api_debug defaults to True precisely so testers
        paste raw traffic into GitHub issues, and docs/components.md promises that traffic
        comes "with credentials redacted".
        """
        if isinstance(payload, dict):
            return {key: ("<redacted>" if key in SUNSYNK_DEBUG_REDACT_KEYS else SunsynkAPI.redact(value)) for key, value in payload.items()}
        if isinstance(payload, list):
            return [SunsynkAPI.redact(value) for value in payload]
        return payload

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

    async def reauthenticate(self, context):
        """Obtain a fresh access token after an auth failure. Returns True if a retry is worth it.

        The oauth path MUST go through handle_oauth_401(): in that mode the token is
        injected by Predbat.com and fetch_token() has nothing to log in with, so it merely
        reports that a token is still held — the retry would then go out carrying exactly
        the same dead bearer and the component would stay broken until the process
        restarted. Same shape as deye.py's reauthenticate(), and the same reactive refresh
        every other OAuthMixin component performs (fox, solis, teslemetry, kraken).
        """
        if await self.handle_oauth_401():
            return True
        if self.auth_method == "oauth":
            self.log(f"Warn: Sunsynk OAuth re-authentication failed on {context}")
            return False
        return await self.fetch_token()

    async def _request(self, method, endpoint_key, sn=None, params=None, body=None):
        """Perform one API call, returning the response's `data` payload, or None on failure.

        Retries transport errors and non-200 responses with backoff, and treats a
        body-level auth failure as a token refresh followed by exactly one retry. That
        retry is earned by the refresh and is independent of the transport-attempt
        budget: even if the auth failure arrives on the LAST transport attempt, the
        retry after a successful refresh still happens rather than being discarded.

        Failure is None, NOT {} — a successful call can legitimately carry no data (the
        settings write is one), so {} would be ambiguous between "worked, nothing to
        return" and "failed", and a write failure would be silently read as success.
        Read callers go through _get, which coerces None to {} because they all want a
        dict; the write path uses the None directly to detect failure. Never raises, so
        every caller fails closed.

        A body-level failure (HTTP 200 with `success: false`) also stamps self.last_api_error
        with the response's own `msg`, so a caller such as the standalone CLI can report the
        API's own reason rather than pointing the user at the Warn: trace.
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
        attempt = 0

        # A `while attempt < SUNSYNK_RETRIES` loop, not `for attempt in range(...)`: `attempt`
        # counts only transport-level failures (non-200 / exception) below. The auth-refresh
        # branch further down deliberately does NOT touch it, so a refresh earns its retry
        # even when it lands on what would have been the final transport attempt. `refreshed`
        # still caps this to at most one extra pass, so total work stays bounded.
        while attempt < SUNSYNK_RETRIES:
            self.debug_api(method, url, body if body is not None else params)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers, params=params, json=body) as response:
                        if response.status != 200:
                            self.log(f"Warn: Sunsynk {method} {path} returned HTTP {response.status}")
                            attempt += 1
                            await asyncio.sleep(2 ** (attempt - 1))
                            continue
                        payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                self.log(f"Warn: Sunsynk {method} {path} failed: {error}")
                attempt += 1
                await asyncio.sleep(2 ** (attempt - 1))
                continue

            self.debug_api("<-", url, payload if isinstance(payload, dict) else {"data": payload})
            if self.is_auth_error_body(payload) and not anonymous and not refreshed:
                refreshed = True
                self.log(f"Info: Sunsynk {path} reported an auth failure, refreshing the token")
                if await self.reauthenticate(path):
                    headers = self._auth_headers()
                    continue
                return None
            if not isinstance(payload, dict) or not payload.get("success"):
                message = payload.get("msg") if isinstance(payload, dict) else payload
                # Captured here, not just logged: this is the ONLY place a body-level
                # failure is known, so it is the only place that can hand the standalone
                # CLI something better than "check the Warn: lines above". Always the
                # response's own `msg`, never anything from the request we just sent, so a
                # credential can never end up in it.
                self.last_api_error = str(message) if message is not None else ""
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

    @staticmethod
    def _as_float(value, default=0.0):
        """Coerce an API value to float, tolerating strings and nulls."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def get_device_list(self):
        """Discover every inverter on the account, honouring the serial filter.

        Stops on the first of three signals: an empty page, a page that contributes no
        serial not already seen, or the running unique-serial count reaching the
        server-reported `total`. The middle signal is the important one - it is what
        actually bounds the loop, because neither of the other two can be trusted alone:
        the endpoint's real pagination behaviour is unverified (no test account exists
        yet - see the VERIFY@SPIKE notes in sunsynk_const.py), a page can be non-empty
        yet carry no `sn` on any entry, and `total` is entirely server-controlled with
        no sanity check possible on this side. Without the no-new-serials check, either
        of those alone can spin the loop forever against a malformed or hostile response
        (confirmed empirically: an all-missing-`sn` page and a `total` in the millions
        both hung or made millions of calls under the previous, `total`-only version of
        this method).

        Serials are deduplicated - overlapping pages must not inflate device_list, which
        would otherwise fan out to duplicated per-cycle polling - but the returned order
        is first-seen order, not sorted, because a later task's automatic_config builds
        per-inverter arg lists positionally from this list.

        Bounded by SUNSYNK_MAX_DISCOVERY_PAGES regardless of any of the above, purely as
        defence in depth against a `total` that is corrupt or never satisfied even though
        every page keeps contributing genuinely new serials. Hitting that cap is logged
        so a real account with more inverters than the cap allows is diagnosable rather
        than silently truncated.

        Also sets self.discovery_ok, so refresh_static() and the standalone CLI can tell
        "the account genuinely has no inverters" apart from "the discovery call itself
        failed". _get() coerces both a transport/API failure and a body with no `infos` at
        all down to a dict, but only a real failure ever coerces all the way to the empty
        dict {} (_get's own failure sentinel) - a successful call against an empty account
        still returns a non-empty dict carrying `total`/`infos`, just with `infos` empty.
        That distinction has to be made at THIS call site, before the two are folded
        together by the `infos` check below - once infos is [] either way, nothing
        downstream can tell them apart any more.
        """
        serials = []
        seen = set()
        self.discovery_ok = False
        for page in range(1, SUNSYNK_MAX_DISCOVERY_PAGES + 1):
            params = {"page": str(page), "limit": str(SUNSYNK_PAGE_SIZE), "type": "-2", "status": "-1"}
            data = await self._get("inverter_list", params=params)
            if not data:
                # {} only ever means the call itself failed (see above) - discovery_ok
                # stays False so this is never mistaken for "the account has none".
                break
            self.discovery_ok = True
            infos = data.get("infos") or []
            if not infos:
                break
            new_serials = 0
            for info in infos:
                serial = info.get("sn")
                if not serial:
                    continue
                serial = str(serial)
                if serial not in seen:
                    seen.add(serial)
                    serials.append(serial)
                    new_serials += 1
            if new_serials == 0:
                # A page that adds nothing new can never close the gap to `total`, however
                # large that gap is reported to be - continuing would spin indefinitely.
                break
            total = int(self._as_float(data.get("total"), len(serials)))
            if len(serials) >= total:
                break
        else:
            self.log(f"Warn: Sunsynk device discovery stopped after the {SUNSYNK_MAX_DISCOVERY_PAGES}-page safety cap with {len(serials)} serial(s) found; the account may hold more inverters than were discovered")
        if self.inverter_sn_filter:
            wanted = {str(sn).lower() for sn in self.inverter_sn_filter}
            serials = [sn for sn in serials if sn.lower() in wanted]
        self.device_list = serials
        return serials

    async def fetch_device_detail(self, sn):
        """Fetch static inverter detail, capturing the rated power for inverter_limit."""
        data = await self._get("inverter_detail", sn=sn)
        if not data:
            return {}
        self.device_detail[sn] = data
        rated = self._as_float(data.get(SUNSYNK_RATED_POWER_FIELD))
        # Only overwrite a known rating — a payload that omits it must not clear it.
        if rated > 0:
            self.device_rated_power[sn] = rated
        return data

    async def fetch_device_data(self, sn):
        """Poll the four realtime endpoints and flatten them onto Predbat's sensor leaves."""
        responses = {}
        for endpoint in ("battery", "grid", "load", "input"):
            params = {"sn": sn, "lan": "en"} if endpoint == "battery" else {"sn": sn}
            responses[endpoint] = await self._get(endpoint, sn=sn, params=params)

        values = {}
        for leaf, (endpoint, field) in SUNSYNK_TELEMETRY.items():
            payload = responses.get(endpoint) or {}
            if field not in payload or payload[field] is None:
                continue
            value = self._as_float(payload[field])
            if leaf in SUNSYNK_TELEMETRY_NEGATE:
                value = -value
            # Preserve integers as integers so SOC publishes as 62 rather than 62.0.
            values[leaf] = int(value) if float(value).is_integer() and leaf in ("soc",) else value
        # Ratings inputs are kept raw on device_values so the derivations below can read them.
        battery = responses.get("battery") or {}
        for field in (SUNSYNK_CAPACITY_AH_FIELD, SUNSYNK_CHARGE_VOLT_FIELD) + SUNSYNK_CHARGE_CURRENT_FIELDS:
            if field in battery and battery[field] is not None:
                values[field] = self._as_float(battery[field])
        self.device_values[sn] = values

        energy = {}
        for leaf, (endpoint, field) in SUNSYNK_ENERGY.items():
            payload = responses.get(endpoint) or {}
            # Absent counters stay absent: an arg pointing at a sensor that is never
            # published is worse than an absent arg, which the user can fill in themselves.
            if field in payload and payload[field] is not None:
                energy[leaf] = self._as_float(payload[field])
        self.device_energy[sn] = energy
        return values

    async def fetch_settings(self, sn):
        """Read the whole settings object: config, plus the baseline the write group is built from.

        The read returns everything (350 keys on a real inverter); only the System Mode subset
        of it is ever posted back. See SUNSYNK_SYSTEM_MODE_FIELDS.
        """
        data = await self._get("settings_read", sn=sn)
        if data:
            self.device_settings[sn] = data
        return data

    def nominal_pack_voltage(self, charge_volts):
        """Infer the pack's nominal voltage from its BMS charge target.

        Sunsynk reports battery capacity in amp-hours, so a voltage is needed to reach kWh.
        A LiFePO4 stack charges to about 3.55V per cell and sits at about 3.2V nominal, so
        the cell count follows from the charge target and the nominal voltage from that.
        Returns 0 when neither an override nor a charge target is available — a wrong
        soc_max is worse than none, because Predbat would plan against a battery that
        does not exist.
        """
        if self.battery_nominal_voltage > 0:
            return self.battery_nominal_voltage
        charge_volts = self._as_float(charge_volts)
        if charge_volts <= 0:
            return 0.0
        # Pick the standard stack size whose implied volts-per-cell falls inside the LiFePO4
        # charge window, breaking ties toward the typical value. Dividing by one assumed
        # figure and rounding is wrong at both ends of that window - 3.55 turns a 24-cell
        # pack charged at 3.65V/cell into 25 cells, and 3.65 turns a 16-cell pack charged at
        # 3.45V/cell into 15 - and either way the error is silent, ~4%, and lands in soc_max.
        candidates = [(abs(charge_volts / cells - LIFEPO4_CHARGE_VOLTS_TYPICAL), cells) for cells in LIFEPO4_CELL_COUNTS if LIFEPO4_CHARGE_VOLTS_MIN <= charge_volts / cells <= LIFEPO4_CHARGE_VOLTS_MAX]
        if not candidates:
            # A charge target that fits no standard stack is not something to guess at: a
            # wrong soc_max makes Predbat plan against a battery that does not exist.
            self.log(f"Warn: Sunsynk cannot infer a LiFePO4 stack size from chargeVolt {charge_volts}; set sunsynk_battery_nominal_voltage in apps.yaml to derive capacity")
            return 0.0
        return min(candidates)[1] * LIFEPO4_NOMINAL_VOLTS_PER_CELL

    def battery_capacity(self, sn):
        """Return the usable battery capacity in kWh, or 0 when it cannot be derived."""
        values = self.device_values.get(sn, {})
        amp_hours = self._as_float(values.get(SUNSYNK_CAPACITY_AH_FIELD))
        volts = self.nominal_pack_voltage(values.get(SUNSYNK_CHARGE_VOLT_FIELD))
        if amp_hours <= 0 or volts <= 0:
            return 0.0
        return amp_hours * volts / 1000.0

    def battery_rate_max(self, sn):
        """Return the maximum charge rate in watts, or 0 when it cannot be derived."""
        values = self.device_values.get(sn, {})
        # First candidate with a positive value wins. A real system was seen reporting
        # maxChargeCurrentLimit 0.0 alongside chargeCurrentLimit 216.0, which derived a rate
        # of 0 and made automatic_config skip battery_rate_max entirely.
        amps = next((amp for amp in (self._as_float(values.get(field)) for field in SUNSYNK_CHARGE_CURRENT_FIELDS) if amp > 0), 0.0)
        volts = self.nominal_pack_voltage(values.get(SUNSYNK_CHARGE_VOLT_FIELD))
        if amps <= 0 or volts <= 0:
            return 0.0
        return amps * volts

    def inverter_limit(self, sn):
        """Return the inverter's AC power rating in watts, or 0 when unknown.

        This is the hardware rating (ratePower) only. It is deliberately NOT reduced by
        pvMaxLimit: despite that setting's "Inverter Power Limiter" label in the app, Sunsynk
        documents it as an EXPORT cap, so the inverter can still deliver its full rating to
        the house. See export_limit.
        """
        return self.device_rated_power.get(sn, 0.0)

    def export_limit(self, sn):
        """Return the maximum export power in watts, or 0 when unknown.

        Predbat's inverter.py defaults export_limit to 99999W - effectively unlimited - so
        leaving this unmapped lets it plan an export the inverter will simply clip. A real
        system had a 7000W export cap behind an 8000W inverter.

        Bounded by the inverter rating too: whatever the setting nominally allows, the
        inverter cannot export more AC than it can produce.
        """
        limits = [value for value in (self.inverter_limit(sn), self._as_float(self.device_settings.get(sn, {}).get(SUNSYNK_EXPORT_LIMIT_FIELD))) if value > 0]
        return min(limits) if limits else 0.0

    def battery_reserve_min(self, sn):
        """Return the inverter's own SOC floor as a percent, or 0 when unknown."""
        settings = self.device_settings.get(sn, {})
        return int(self._as_float(settings.get(SUNSYNK_BATTERY_LOW_CAP_FIELD)))

    def derive_control_state(self, schedule, current_soc):
        """Map Predbat's schedule intent to a Sunsynk control state (see the spec's table).

        The work mode governs whether the BATTERY may export, and it is orthogonal to
        solarSell, which governs whether surplus PV may. Confirmed on a live system that
        exported 11.1 kWh in a day while sitting in "Limited to Home" with solarSell on, so
        the mode does not gate solar.

        Non-export states therefore use zero_export_ct ("Limited to Home"): the battery
        serves the whole house, measured at the grid CT, without exporting. The stricter
        zero_export_load ("Zero-Export + Limit To Load Only") measures at the inverter's own
        output instead, so on a CT-clamp install it would stop the battery serving anything
        not wired to the inverter, and the shortfall would come from the grid.

        Semantics are inherited from DEYE — the same registers sit behind both clouds —
        but every wire value comes from SUNSYNK_WORKMODE, never from DEYE's enum.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})

        if export.get("enable"):
            export_soc = int(export.get("soc", FREEZE_EXPORT_SOC))
            if export_soc >= FREEZE_EXPORT_SOC:
                return self._freeze_export_state(reserve)
            return {"behaviour": "export", "work_mode": SUNSYNK_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": export_soc, "power": int(export.get("power", 0))}

        if charge.get("enable"):
            charge_soc = int(charge.get("soc", 0))
            if charge_soc > current_soc and charge_soc > reserve:
                return {"behaviour": "charge", "work_mode": SUNSYNK_WORKMODE["zero_export_ct"], "grid_charge": True, "solar_sell": False, "slot_soc": charge_soc, "power": int(charge.get("power", 0))}
            if charge_soc == reserve:
                # A freeze charge holds via the RESERVE, not this rate: Predbat sets the
                # reserve to soc_percent + 1 for the duration (execute.py), and cap{n}
                # follows it, which is what stops the battery discharging below where it
                # started. Solar charging above that is allowed and expected - a freeze
                # charge only bars discharge.
                #
                # The zero rate is therefore not what makes this a hold, and must not be
                # read as one: CONFIRMED live 2026-08-20 that a zero slot rate does NOT
                # stop the battery charging (rate 0 written and read back, battery carried
                # on at 554 W until it simply reached 100%). What it does stop is the
                # battery being SOLD to the grid - see _freeze_export_state.
                return {"behaviour": "freeze_charge", "work_mode": SUNSYNK_WORKMODE["zero_export_ct"], "grid_charge": True, "solar_sell": False, "slot_soc": reserve, "power": 0}
            # The battery is already at or above the requested target, so grid charge stays
            # OFF - the charge is simply not triggered. The slot still carries Predbat's
            # charge rate, because that is the rate it has chosen for this window; only the
            # behaviour differs, not the power. Zero here would mean freeze, which is a
            # different state, and the inverter's full rating would discard Predbat's rate.
            return {"behaviour": "hold_charge", "work_mode": SUNSYNK_WORKMODE["zero_export_ct"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}

        # No window is active, so the only thing left that can distinguish demand from a
        # freeze export is the charge rate. Predbat expresses Freeze Export - "demand mode,
        # but with charging disabled" - by turning the forced-export window OFF and calling
        # adjust_charge_rate(0), because SunsynkCloud declares has_timed_pause False and
        # that is the only lever execute.py has left. A zero charge rate always means "do
        # not charge the battery" (the same call backs set_freeze_export_during_demand and
        # the cross-charging guards), so it is honoured here rather than dropped.
        #
        # Without this the freeze never reached the inverter at all: charge_rate maps to the
        # per-window battery_schedule_charge_power, which derive_control_state only reads
        # inside an ENABLED charge window - and a freeze export has none. Predbat said
        # Freeze Export, Sunsynk wrote a byte-identical self-use programme to plain Demand,
        # and surplus PV charged the battery instead of being exported.
        #
        # CONFIRMED live end to end (inverter 2405116013, 2026-08-20 11:30). Before the
        # write the battery was taking 250 W of PV at 99% SoC; after it, battery power sat
        # at 6, 5, 5, -6, -10 W across five polls while the grid export tracked PV minus
        # load almost exactly (3620 W PV, 149 W load, 3347 W exported). The battery stopped
        # charging, was not discharged, and every surplus watt reached the grid. Restoring
        # the demand payload put it straight back to charging at 535 W.
        #
        # A zero charge rate is only read as a freeze while the EXPORT rate is non-zero,
        # which is what makes the signal "charging disabled, discharging still allowed" -
        # Freeze Export exactly - rather than just "zero". It also keeps a system whose
        # battery rates were never derived (both entities still at their published 0, or
        # battery_rate_max unmappable) out of a permanent freeze: all-zero is not a plan,
        # it is an absence of one, and demand is the right thing to fall back to.
        if int(charge.get("power", 0)) == 0 and int(export.get("power", 0)) > 0:
            return self._freeze_export_state(reserve)

        return {"behaviour": "idle", "work_mode": SUNSYNK_WORKMODE["zero_export_ct"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": 0}

    @staticmethod
    def _freeze_export_state(reserve):
        """Return the control state for a freeze export: sell the surplus, keep serving the house.

        Selling First, the per-slot Sell flag ON, the slot rate at ZERO, and the cap at the
        reserve. Each field earns its place, and all four were settled on live hardware
        (inverter 2405116013, 2026-08-20) rather than inferred:

          * Selling First is what stops the surplus solar charging the battery. Limited to
            Home runs PV -> load -> battery -> grid, so the battery fills first: a 99%
            battery was still taking 540 W of PV in that mode while the rest exported.
          * The RATE is the sell-rate cap, and zero is what stops the battery being pushed
            out to the grid. Two runs differing only in this field settle it - at 8000 W
            with the cap 3% under the SoC the battery drained to the grid at up to 4715 W,
            and at 0 W with the cap 5% under the SoC it held at 1-28 W across five polls
            while the export still tracked PV minus load exactly. This is the field the
            original code had right.
          * The CAP is the reserve, so the battery keeps the room it needs to cover the
            house when the load exceeds the solar. The rate is what makes that safe: a cap
            below the SoC is only dangerous while the sell rate is non-zero.

        The cap must NEVER be FREEZE_EXPORT_SOC. That constant is Predbat's SENTINEL for
        "this export window is a freeze" (export_limits_best of 99), not a battery level,
        and writing it through to cap{n} told a Selling First inverter to drive the battery
        to 99% - charging it from the very solar the freeze exists to export. Conflating the
        marker with the target is the whole of what made a freeze look like an export, and
        it is the only field of the four that was ever wrong.

        NOT confirmed live: that the battery still covers the house under this mode. It
        needs the load to exceed the solar, and every run so far was midday sun against a
        250 W house. The cap is what should allow it and nothing observed contradicts that,
        but it is the one leg of this resting on reasoning rather than a measurement.
        """
        return {"behaviour": "freeze_export", "work_mode": SUNSYNK_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": int(reserve), "power": 0}

    @staticmethod
    def _to_slot_time(value):
        """Normalise a schedule time to the HH:MM Sunsynk's slots require.

        The control entities carry HH:MM:SS because that is what Predbat writes (see
        INVERTER_DEF charge_time_format), so the seconds are dropped here — at the one
        point a schedule time becomes a slot time.
        """
        parts = str(value or "00:00").split(":")
        if len(parts) < 2:
            return "00:00"
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except ValueError:
            return "00:00"

    @staticmethod
    def _hm_to_minutes(hm):
        """Convert an HH:MM string to minutes since midnight (0 on bad input)."""
        try:
            parts = str(hm).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    def _window_active(self, window, now_minutes):
        """Return True if an enabled window covers now_minutes, handling a midnight wrap."""
        if not window.get("enable") or not window.get("start") or not window.get("end"):
            return False
        start = self._hm_to_minutes(self._to_slot_time(window["start"]))
        end = self._hm_to_minutes(self._to_slot_time(window["end"]))
        if start == end:
            return False
        if start < end:
            return start <= now_minutes < end
        return now_minutes >= start or now_minutes < end

    def _self_use_slot(self, start_time, reserve, self_use_power):
        """Build a self-use slot holding at the reserve SOC.

        self_use_power must NOT be zero. The rate is the battery's power limit for the
        slot, so a zero-rate self-use slot risks stopping the battery serving the house for
        the whole interval and pushing the load onto the grid. Self-use slots cover most of
        the day, so this is the default state and not somewhere to take the risk.

        Precisely what a zero rate does is only half known. CONFIRMED live 2026-08-20 that
        it does NOT stop CHARGING (rate 0 written and read back, the battery carried on at
        554 W until it reached 100%), and that it DOES stop the battery being sold to the
        grid under Selling First - which is what _freeze_export_state relies on. Its effect
        on discharge to the HOUSE was never measured, because every run was midday sun
        against a 250 W load. This guard stays because that is the untested direction.
        """
        return {"time": start_time, "power": int(self_use_power), "soc": int(reserve), "grid_charge": False, "sell": False}

    def _action_slot(self, start_time, state):
        """Build a slot realising a derived control state."""
        return {"time": start_time, "power": int(state["power"]), "soc": int(state["slot_soc"]), "grid_charge": bool(state["grid_charge"]), "sell": bool(state.get("solar_sell"))}

    def _slot_for(self, start_time, state, reserve, self_use_power):
        """Build the slot for one derived state: an action slot, or self-use when idle.

        A state that asks for nothing - no grid charge, no sell, no power - is demand, and
        demand needs the inverter's full rating so the battery can serve the house (see
        _self_use_slot). Anything else is written verbatim, which is what lets a freeze,
        whose only non-default field is the sell flag, survive as a zero-power slot.
        """
        if state.get("grid_charge") or state.get("solar_sell") or state.get("power"):
            return self._action_slot(start_time, state)
        return self._self_use_slot(start_time, reserve, self_use_power)

    def build_tou_slots(self, schedule, current_soc, self_use_power):
        """Build exactly TOU_SLOT_COUNT ordered slots covering 24h from the schedule windows.

        Slots are sequential intervals ("from this start until the next slot's start") and
        Sunsynk documents that they MUST be set chronologically, so every start is written
        distinct and ascending. A slot is an interval whatever its grid-charge flag says,
        which is what lets a filler slot terminate the charge window before it.

        Segment boundaries are collected from a 00:00 baseline plus each enabled window's
        start (its action) and end (back to the baseline), then padded with fillers and
        trimmed to the earliest, most imminent TOU_SLOT_COUNT.

        The baseline is DERIVED rather than assumed to be self-use, because "no window is
        active" is not always demand: a freeze export is exactly that state plus a zero
        charge rate (see derive_control_state). Every slot the schedule does not otherwise
        claim - the 00:00 start, each window's end, and the fillers - therefore carries the
        baseline, so a freeze covers the whole programme instead of being defeated by the
        first filler that happens to cover the current time.

        That coarseness is deliberate. Predbat never tells this component when a freeze
        ends - it disables the export window rather than describing it - so there is no
        boundary to write. Pinning the freeze to "now" instead would move every slot time
        on every tick and turn the applied-payload change detection into a write per cycle.
        The programme is rebuilt whenever Predbat's plan changes (run() applies each tick
        for control_active inverters), so it reverts to self-use as soon as the charge rate
        comes back.
        """
        reserve = int(schedule.get("reserve", 0))
        baseline = self.derive_control_state({"reserve": reserve, "charge": {"enable": False, "power": int(schedule.get("charge", {}).get("power", 0))}, "export": {"enable": False, "power": int(schedule.get("export", {}).get("power", 0))}}, current_soc)
        segments = {"00:00": dict(baseline)}
        for direction in ("charge", "export"):
            window = schedule.get(direction, {})
            if not (window.get("enable") and window.get("start") and window.get("end")):
                continue
            start_time = self._to_slot_time(window["start"])
            end_time = self._to_slot_time(window["end"])
            if start_time == end_time:
                # Mirrors the guard in _window_active: a zero-length window has no interval
                # to act over. Compared on the NORMALISED times so "02:00:00" vs "02:00"
                # is caught too. Without this, an enable event that arrives before the time
                # fields (both still the "00:00:00" default) would add an action segment at
                # 00:00 with no matching return-to-self-use segment - an unterminated,
                # multi-hour full-power grid-charge/export slot even though _active_state
                # correctly reports the window inactive.
                continue
            intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
            intent[direction] = {"enable": True, "soc": window.get("soc", 0), "power": window.get("power", 0)}
            segments[start_time] = self.derive_control_state(intent, current_soc)
            segments.setdefault(end_time, dict(baseline))

        slots = []
        for start_time, state in sorted(segments.items(), key=lambda item: item[0]):
            slots.append(self._slot_for(start_time, state, reserve, self_use_power))

        used = {slot["time"] for slot in slots}
        for filler in TOU_FILLER_TIMES:
            if len(slots) >= TOU_SLOT_COUNT:
                break
            if filler not in used:
                slots.append(self._slot_for(filler, baseline, reserve, self_use_power))
                used.add(filler)
        return sorted(slots, key=lambda slot: slot["time"])[:TOU_SLOT_COUNT]

    def _now_minutes(self):
        """Return minutes since local midnight, for time-aware window selection."""
        try:
            return int(self.minutes_now)
        except (TypeError, ValueError):
            return 0

    def _active_state(self, schedule, current_soc, now_minutes):
        """Derive the control state for the window active at now_minutes, else idle.

        Sunsynk has a single global work mode, so the top-level mode must follow the
        window active RIGHT NOW rather than a static export-first precedence: otherwise
        an export window enabled elsewhere in the day would pin the mode to selling-first
        and block the charge window's grid charging.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})
        # The charge rate is carried even when no window is active: with both windows shut
        # a zero rate is Predbat's Freeze Export, and the mode has to follow it rather than
        # sit in demand (see derive_control_state).
        intent = {"reserve": reserve, "charge": {"enable": False, "power": int(charge.get("power", 0))}, "export": {"enable": False, "power": int(export.get("power", 0))}}
        if self._window_active(export, now_minutes):
            intent["export"] = {"enable": True, "soc": export.get("soc", 0), "power": export.get("power", 0)}
        elif self._window_active(charge, now_minutes):
            intent["charge"] = {"enable": True, "soc": charge.get("soc", 0), "power": charge.get("power", 0)}
        return self.derive_control_state(intent, current_soc)

    def _owned_payload(self, sn, schedule, current_soc, now_minutes):
        """Build only the fields Predbat owns, with ZERO network I/O.

        Pure in schedule, current_soc and now_minutes, plus the CACHED
        battery_reserve_min(sn) - i.e. whatever settings were last read into
        device_settings, not a fresh read - so apply_settings can call this on every tick
        to check whether anything would actually change before paying for a real read. A
        consequence: a floor change alone will not be noticed until the config tier next
        refreshes device_settings for this serial (acceptable for an installer setting,
        since build_settings_payload rebuilds and re-clamps against the fresh floor as
        soon as a real read does happen - see apply_settings).
        """
        # Self-use slots get the inverter's full rating so the battery is free to serve
        # whatever the house draws. Zero would freeze it (see _self_use_slot); if the rating
        # is unknown, fall back to the largest slot power already on the inverter rather
        # than write a freeze by accident.
        self_use_power = int(self.inverter_limit(sn)) or self._existing_slot_power(sn)
        if not self_use_power:
            self.log(f"Warn: Sunsynk {sn} has no inverter rating and no existing slot power, so self-use slots would be written with zero power (a freeze); skipping the write")
            return {}
        slots = self.build_tou_slots(schedule, current_soc, self_use_power)
        active = self._active_state(schedule, current_soc, now_minutes)

        # Never ask the battery to go below the floor its own installer settings declare.
        # Predbat's control entities start at 0 and only reach their real values once it
        # has written them, so without this the first write of a cycle sends slot SOC 0 to
        # a pack whose floor is 14%. Applied last so no caller can bypass it.
        floor = self.battery_reserve_min(sn)
        if floor > 0:
            lifted = False
            for slot in slots:
                if slot["soc"] < floor:
                    slot["soc"] = floor
                    lifted = True
            if lifted and sn not in self._soc_floor_warned:
                self._soc_floor_warned.add(sn)
                self.log(f"Info: Sunsynk {sn} raising requested slot SOC to the inverter's {floor}% floor (batteryLowCap)")

        # Every owned field goes through encode_setting, including the work mode: it is the
        # single place that knows which fields Sunsynk wants bare and which quoted, so a
        # field that bypasses it is a field whose encoding cannot be corrected in one place.
        payload = {SUNSYNK_SERIAL_FIELD: sn, SUNSYNK_WORKMODE_FIELD: encode_setting(SUNSYNK_WORKMODE_FIELD, active["work_mode"])}
        # Solar Export is always left ON, regardless of what the active state derives.
        #
        # solarSell does not govern the BATTERY - it governs whether surplus PV reaches the
        # grid at all. Predbat plans when the battery charges and exports; it assumes, like
        # every other inverter it drives, that spare solar exports passively in the
        # background. Deriving this from the active window (on only inside an export window,
        # off otherwise) would curtail surplus PV for most daylight hours, silently costing
        # the user export revenue on a sunny day once the battery is full - and Predbat has
        # no notion it is doing so, because nothing in its model represents PV curtailment.
        #
        # An export window still exports: that is driven by the selling-first work mode and
        # the slot SoC targets, not by this flag. Turning it off is never useful here, only
        # harmful, so it is not derived at all.
        payload[SUNSYNK_SOLAR_SELL_FIELD] = encode_setting(SUNSYNK_SOLAR_SELL_FIELD, "1")
        payload[SUNSYNK_TOU_ENABLE_FIELD] = encode_setting(SUNSYNK_TOU_ENABLE_FIELD, "1")
        for day in SUNSYNK_DAY_FIELDS:
            payload[day] = encode_setting(day, True)
        for index, slot in enumerate(slots, start=1):
            payload[TOU_FIELD["time"].format(n=index)] = encode_setting(TOU_FIELD["time"].format(n=index), slot["time"])
            payload[TOU_FIELD["power"].format(n=index)] = encode_setting(TOU_FIELD["power"].format(n=index), slot["power"])
            payload[TOU_FIELD["soc"].format(n=index)] = encode_setting(TOU_FIELD["soc"].format(n=index), slot["soc"])
            payload[TOU_FIELD["grid_charge"].format(n=index)] = encode_setting(TOU_FIELD["grid_charge"].format(n=index), slot["grid_charge"])
            # The per-slot Sell flag ("Sell" in the app). It MUST be 1 for a forced export
            # slot, and every per-slot flag must be present in the payload or the API
            # silently discards them all - see TOU_FIELD.
            # Through encode_setting like every other owned field, so wire encoding stays in
            # one place. Passed as 1/0 rather than a bool: sellTime{n}En is deliberately NOT
            # in SUNSYNK_BOOL_FIELDS (the API returns it as "1"/"0", unlike time{n}on's
            # "true"/"false"), so it falls through to str() - and str(True) would be "True".
            payload[TOU_FIELD["sell"].format(n=index)] = encode_setting(TOU_FIELD["sell"].format(n=index), 1 if slot["sell"] else 0)
        return payload

    def build_settings_payload(self, sn, schedule, current_soc, now_minutes=None):
        """Build the full settings object to POST for one inverter.

        Read-modify-write over the System Mode group only: start from the last-read values
        for those fields so the ones Predbat does not own survive verbatim, then overwrite the
        slots, mode and flags it does. Fields outside the group are never sent - the endpoint
        discards an oversized object entirely.
        Returns {} when self.device_settings holds no baseline for sn - a payload built
        from an empty baseline would contain only the owned keys, and posting it would
        drop every installer setting Predbat does not own. This is a public producer, not
        just an apply_settings implementation detail, so the guard belongs here rather
        than only in a caller - the same reasoning as the SOC clamp living at the API
        boundary.
        """
        baseline = self.device_settings.get(sn)
        if not baseline:
            return {}
        if now_minutes is None:
            now_minutes = self._now_minutes()
        # Only the System Mode group is sent. The endpoint accepts a larger object and then
        # silently discards the whole write - see SUNSYNK_SYSTEM_MODE_FIELDS - so restricting
        # this is what makes the write land at all. It also means a schedule write can never
        # disturb the battery, grid or generator settings: they are simply never transmitted.
        payload = {key: value for key, value in baseline.items() if key in SUNSYNK_SYSTEM_MODE_FIELDS and key not in SUNSYNK_DERIVED_SLOT_FIELDS}
        payload.update(self._owned_payload(sn, schedule, current_soc, now_minutes))
        return payload

    def _existing_slot_power(self, sn):
        """Return the largest per-slot power already set on the inverter, or 0 if none.

        Used only as a fallback for self-use slots when the inverter rating is unknown -
        writing zero there would freeze the battery, so reusing whatever the installer
        already had is strictly better than that.
        """
        settings = self.device_settings.get(sn, {})
        powers = [self._as_float(settings.get(TOU_FIELD["power"].format(n=n))) for n in range(1, TOU_SLOT_COUNT + 1)]
        return int(max(powers)) if powers and max(powers) > 0 else 0

    def payloads_equal(self, a, b):
        """Compare two settings payloads for change detection."""
        return dict(a or {}) == dict(b or {})

    async def apply_settings(self, sn, schedule, current_soc, force=False):
        """Read, modify and write the settings object for one inverter.

        Returns True if a write was performed. The owned-field diff is checked FIRST,
        against _owned_payload (zero network I/O) and the owned subset of the last-applied
        payload: a no-op tick therefore never touches the network at all, not even a
        settings read - closing the race with the Sunsynk phone app tighter than reading
        on every tick would, since now no read happens except immediately before an actual
        write. Only when that diff (or force=True) says something would actually change
        does this read the live settings, and it fails closed if that read comes back
        empty: without a fresh baseline there is no way to build a payload that preserves
        every field Predbat does not own.

        Deliberately always reads immediately before a write rather than reusing a cached
        baseline, even one read moments earlier in the same tick by refresh_config: that
        cache can be stale in a way nothing here can detect - fetch_settings only updates
        device_settings on a SUCCESSFUL read, so a failed per-serial config-tier poll (one
        inverter offline, one timeout) leaves the old baseline in place with no signal
        that it did not just refresh, and restore_state seeds device_settings from the
        config cache with no age bound at all (unlike the control cache's
        SUNSYNK_RESTORE_MAX_CONTROL). Reusing it would risk writing a stale
        batteryLowCap/installer setting straight back to the battery - on the one call in
        this component that changes what the battery actually does, not just what Predbat
        thinks it asked for. The extra reads this costs are bounded by the change-detection
        gate above (never more than once per genuine plan change) and are worth it for
        that guarantee.
        """
        if not self.control_enable:
            return False

        now_minutes = self._now_minutes()
        owned = self._owned_payload(sn, schedule, current_soc, now_minutes)
        applied = self.applied_payload.get(sn)
        if applied and not force:
            applied_owned = {key: applied.get(key) for key in owned}
            if self.payloads_equal(owned, applied_owned):
                # Nothing Predbat owns has changed, so skip the read entirely - there is
                # nothing to write and therefore nothing to re-read a fresh baseline for.
                return False

        # Only reached on a genuine change (or force=True). Re-read immediately before
        # writing so the race with the Sunsynk phone app is as small as possible, and so
        # unowned fields - and the SOC floor the clamp above used a possibly-stale value
        # for - carry the newest values.
        previous_settings = dict(self.device_settings.get(sn, {}))
        settings = await self.fetch_settings(sn)
        if not settings:
            self.log(f"Warn: Sunsynk {sn} settings read failed, skipping the write (no baseline to modify)")
            return False
        self.note_external_change(sn, previous_settings, settings)

        payload = self.build_settings_payload(sn, schedule, current_soc, now_minutes)
        if not payload:
            self.log(f"Warn: Sunsynk {sn} has no settings baseline to modify, skipping the write")
            return False

        # _post reports failure as None. A successful settings write carries no data
        # payload, so {} means "written, nothing returned" and only None means failure.
        response = await self._post("settings_set", sn=sn, body=payload)
        if response is None:
            self.log(f"Warn: Sunsynk {sn} settings write failed, the plan has not reached the inverter")
            return False
        self.applied_payload[sn] = payload
        self.settle_count[sn] = 0
        self.log(f"Info: Sunsynk {sn} settings written ({self._active_state(schedule, current_soc, now_minutes)['behaviour']})")
        return True

    def note_settle(self, sn, settings):
        """Track how many config-tier polls the inverter has disagreed with the last write for.

        Counted in CONFIG-TIER POLLS, not in run() cycles: this is only called from
        refresh_config, which runs on SUNSYNK_TTL_CONFIG (15 minutes), so
        SUNSYNK_SETTLE_POLLS = 3 is about 45 minutes of divergence before the warning.
        A write is acknowledged by the cloud long before the dongle collects it, so
        divergence within that bound is normal latency, not a failure.
        Both sides are decoded through encode_setting before comparing: Sunsynk is known to
        hand the boolean fields (time{n}on) back as strings ("true"/"false", "1"/"0") as
        often as bare booleans (see SUNSYNK_FALSE_STRINGS), and a raw str() comparison would
        read that rendering difference as a permanent mismatch against a perfectly healthy
        inverter, so settle_count would never reset and the warning would fire forever.

        Watches every field _owned_fields() reports except SUNSYNK_SERIAL_FIELD - sn is an
        echo of the request, not a setting the inverter applies, so comparing it adds
        nothing. This must stay the full owned set, not a hand-picked subset: a partial
        apply of a field left out here (e.g. a per-slot power limit or a day flag) would
        compare equal on everything checked and wrongly settle.

        Only compared over keys actually present in settings, and settle_count is left
        untouched (neither reset nor bumped) if none of them are. Nobody on this project has
        a Sunsynk account to confirm every owned field is echoed back by a real /read - a key
        missing from the read-back means "the API told us nothing about this field", not "the
        inverter diverged", and treating absence as divergence would reintroduce exactly the
        cry-wolf failure this docstring already warns about above: a perfectly healthy
        inverter warned about forever because encode_setting(key, None) can never match what
        was actually applied.
        """
        applied = self.applied_payload.get(sn)
        if not applied or not settings:
            return
        owned = self._owned_fields() - {SUNSYNK_SERIAL_FIELD}
        present = [key for key in owned if key in settings]
        if not present:
            return
        if all(encode_setting(key, settings.get(key)) == encode_setting(key, applied.get(key)) for key in present):
            self.settle_count[sn] = 0
            return
        self.settle_count[sn] = self.settle_count.get(sn, 0) + 1
        if self.settle_count[sn] > SUNSYNK_SETTLE_POLLS:
            self.log(f"Warn: Sunsynk {sn} has not applied Predbat's settings after {self.settle_count[sn]} settings polls; check the inverter is online in the Sunsynk app")

    def _owned_fields(self):
        """Return every settings key this component writes, so the rest can be watched."""
        owned = {SUNSYNK_SERIAL_FIELD, SUNSYNK_WORKMODE_FIELD, SUNSYNK_SOLAR_SELL_FIELD, SUNSYNK_TOU_ENABLE_FIELD}
        owned.update(SUNSYNK_DAY_FIELDS)
        for n in range(1, TOU_SLOT_COUNT + 1):
            owned.update(TOU_FIELD[concept].format(n=n) for concept in ("time", "power", "soc", "grid_charge"))
        return owned

    def note_external_change(self, sn, before, after):
        """Log when someone else changed a setting Predbat does not own.

        There is one whole-object write endpoint, so a race with the Sunsynk phone app is
        unavoidable and last writer wins. Predbat cannot prevent it, but it can say so —
        otherwise a user's app change silently disappearing into a read-modify-write looks
        like the inverter losing settings by itself.
        """
        if not before or not after:
            return
        owned = self._owned_fields()
        changed = [key for key, value in after.items() if key not in owned and key in before and str(before[key]) != str(value)]
        if changed:
            self.log(f"Info: Sunsynk {sn} settings changed outside Predbat since the last read: {', '.join(sorted(changed))}")

    def _sensor_name(self, sn, leaf):
        """Return a namespaced Sunsynk sensor entity id."""
        return f"sensor.{self.prefix}_sunsynk_{sn.lower()}_{leaf}"

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced Sunsynk control entity id."""
        return f"{domain}.{self.prefix}_sunsynk_{sn.lower()}_{leaf}"

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "temperature": "°C", "battery_voltage": "V"}
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=values[leaf], attributes={"unit_of_measurement": unit, "friendly_name": f"Sunsynk {sn} {leaf.replace('_', ' ').title()}"}, app="sunsynk")

            # Ratings are published only when actually derivable. An arg pointing at a
            # sensor that never appears is worse than an absent arg, which the user can
            # fill in via apps.yaml.
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=round(capacity, 3), attributes={"unit_of_measurement": "kWh", "friendly_name": f"Sunsynk {sn} Battery Capacity"}, app="sunsynk")
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} Battery Rate Max"}, app="sunsynk")
            rated_power = self.inverter_limit(sn)
            if rated_power > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=rated_power, attributes={"unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} Inverter Limit"}, app="sunsynk")
            export_cap = self.export_limit(sn)
            if export_cap > 0:
                self.dashboard_item(self._sensor_name(sn, "export_limit"), state=export_cap, attributes={"unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} Export Limit"}, app="sunsynk")
            floor = self.battery_reserve_min(sn)
            if floor > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_reserve_min"), state=floor, attributes={"unit_of_measurement": "%", "friendly_name": f"Sunsynk {sn} Battery Reserve Min"}, app="sunsynk")

            # Daily energy counters feed Predbat's load/import/export history learning.
            # They reset at midnight; minute_data/clean_incrementing_reverse absorb that.
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": f"Sunsynk {sn} {leaf.replace('_', ' ').title()}"},
                    app="sunsynk",
                )

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        # Deliberately NOT clamped to the inverter floor. This entity is Predbat's control
        # surface: it writes a value then reads it back to confirm (write_and_poll_value),
        # so publishing anything other than what was written guarantees a mismatch and a
        # retry storm. The floor is enforced at the API boundary in build_settings_payload.
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Sunsynk {sn} Battery Schedule Reserve", "icon": "mdi:gauge"},
            app="sunsynk",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes
            # Predbat replace these entities with its own dummies (inverter.py, the
            # inv_charge_time_format != "HH:MM:SS" branch) and the window never arrives.
            self.dashboard_item(
                self._control_name("select", sn, f"battery_schedule_{direction}_start_time"),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": f"Sunsynk {sn} {direction.title()} Start", "icon": "mdi:clock-outline"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("select", sn, f"battery_schedule_{direction}_end_time"),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": f"Sunsynk {sn} {direction.title()} End", "icon": "mdi:clock-outline"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("number", sn, f"battery_schedule_{direction}_soc"),
                state=int(window.get("soc", 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Sunsynk {sn} {direction.title()} SoC", "icon": "mdi:gauge"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("number", sn, f"battery_schedule_{direction}_power"),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} {direction.title()} Power", "icon": "mdi:flash"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("switch", sn, f"battery_schedule_{direction}_enable"),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": f"Sunsynk {sn} {direction.title()} Enable", "icon": "mdi:check-circle-outline"},
                app="sunsynk",
            )
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": f"Sunsynk {sn} Schedule Write", "icon": "mdi:content-save"}, app="sunsynk")

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape control derivation consumes.

        Numeric casts route through _as_float so an entity legitimately reporting
        "unknown"/"unavailable" — for instance right after a HA restart, before Predbat
        republishes — falls back to 0 rather than raising and killing the control loop.
        """
        schedule = {"reserve": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, f"battery_schedule_{direction}_enable"), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), default="00:00:00"),
                "soc": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_soc"), default=0), 0)),
                "power": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_power"), default=0), 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule

    def _sn_from_entity(self, entity_id):
        """Extract the inverter serial from a Sunsynk entity id, or None if unresolvable.

        Entity ids are always {domain}.{prefix}_sunsynk_{sn}_{leaf}, so the serial is
        always followed by "_". Matching sn + "_" rather than a bare prefix keeps
        prefix-colliding serials apart — an entity for INV11 must never route to INV1,
        which would send a control write to the wrong inverter.
        """
        text = str(entity_id).lower()
        for sn in self.device_list:
            if f"_sunsynk_{sn.lower()}_" in text:
                return sn
        return None

    @staticmethod
    def _to_bool(value, current=False):
        """Coerce a switch service or state to a boolean, keeping current when unknown."""
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("turn_on", "on", "true", "1"):
            return True
        if text in ("turn_off", "off", "false", "0"):
            return False
        if text == "toggle":
            return not current
        return current

    @staticmethod
    def _empty_schedule():
        """Return a fresh, disabled schedule shape - the single source of truth for its defaults.

        Used where a schedule has to be seeded from nothing: a control event arriving for an
        inverter local_schedule has not seen yet. Kept as one helper rather than a literal
        repeated at each call site, so adding or renaming a field cannot silently diverge
        between copies. run() deliberately does NOT seed from here - it reads the control
        entities instead, whose per-field defaults produce exactly this shape when nothing
        has been published yet, but which hold Predbat's live plan after a restart.
        """
        return {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}

    def update_local_schedule(self, sn, entity_id, value):
        """Apply one control-entity change to the locally held schedule."""
        schedule = self.local_schedule.setdefault(sn, self._empty_schedule())
        leaf = str(entity_id).split(f"_sunsynk_{sn.lower()}_", 1)[-1]
        if leaf == "battery_schedule_reserve":
            schedule["reserve"] = int(self._as_float(value, 0))
            return
        for direction in ("charge", "export"):
            prefix = f"battery_schedule_{direction}_"
            if not leaf.startswith(prefix):
                continue
            field = leaf[len(prefix) :]
            window = schedule.setdefault(direction, {})
            if field in ("start_time", "end_time"):
                window[field.replace("_time", "")] = str(value)
            elif field in ("soc", "power"):
                window[field] = int(self._as_float(value, 0))
            elif field == "enable":
                window["enable"] = self._to_bool(value, window.get("enable", False))
            return

    async def apply_schedule(self, sn, force=False):
        """Apply the locally held schedule for one inverter."""
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        current_soc = int(self._as_float(self.device_values.get(sn, {}).get("soc"), 0))
        return await self.apply_settings(sn, schedule, current_soc, force=force)

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the inverter."""
        return self.get_state_wrapper(f"switch.{self.prefix}_set_read_only", default="off") == "on"

    async def _reconcile_control(self, sn):
        """Re-apply sn's schedule if Predbat already controls it, unforced (matches deye.py's _reconcile_control).

        A no-op for an inverter Predbat has not yet driven via the write button (not in
        ``control_active``), and a no-op while ``switch.predbat_set_read_only`` is on: the
        top-level work mode is time-aware, so a window transition changes the payload even
        with no plan change, and without this guard that transition would write to the
        inverter regardless of read-only.
        """
        if sn not in self.control_active or self._is_read_only():
            return
        try:
            if await self.apply_schedule(sn):
                await self.save_control()
        except Exception as error:
            self.log(f"Warn: Sunsynk schedule apply failed for {sn}: {error}")

    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right inverter and apply it."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log(f"Warn: Sunsynk could not resolve an inverter for {entity_id}")
            return
        # The write button is NOT forced. Predbat presses this on every cycle as its
        # normal "apply the schedule" action (INVERTER_DEF time_button_press), not only
        # when the plan actually changed, so force=True here would bypass the
        # applied-payload change-detection gate on every single cycle. deye.py hit this
        # exact bug first: PR #4371 (commit 3e1de759) measured 40 button presses
        # producing 36 byte-identical control orders over two hours on a live site once
        # the button forced the write. Unforced, the applied-payload cache in
        # apply_settings is the single source of truth for whether a write is needed;
        # do not reintroduce force=True here.
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                # Predbat is now actively driving this inverter, so run() may re-apply its
                # schedule from here on (see the control_active gate there). Marked on the
                # press itself rather than on a successful write: a write that failed still
                # means Predbat owns this inverter and the next tick should retry.
                self.control_active.add(sn)
                await self.apply_schedule(sn)
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

    async def select_event(self, entity_id, value):
        """Handle a select entity change."""
        await self._handle_control_event(entity_id, value)

    async def number_event(self, entity_id, value):
        """Handle a number entity change."""
        await self._handle_control_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle a switch entity service call."""
        await self._handle_control_event(entity_id, service)

    async def load_cache(self, name):
        """Load one cache file, returning {} when absent or unreadable.

        Also flags an in-progress restore_state() attempt as incomplete via
        _restore_had_error when a REAL failure is caught here — see restore_state for why.
        self.storage being None is checked first and returns silently, with no warning and
        no _restore_had_error: it means there is simply no Storage component configured (the
        normal state for a standalone CLI run, see mock_base.py), which is a permanent,
        by-design condition rather than a transient fault worth retrying or warning about.
        """
        if self.storage is None:
            return {}
        try:
            data = await self.storage.load(SUNSYNK_STORAGE_MODULE, name)
        except Exception as error:
            self.log(f"Warn: Sunsynk could not load cache {name}: {error}")
            self._restore_had_error = True
            return {}
        return data if isinstance(data, dict) else {}

    async def save_cache(self, name, data):
        """Save one cache file, tolerating a storage failure.

        Silently does nothing when self.storage is None (no Storage component configured,
        the normal state for a standalone CLI run) - there is nothing to warn about, only
        a REAL save failure below is worth logging.
        """
        if self.storage is None:
            return
        try:
            await self.storage.save(SUNSYNK_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log(f"Warn: Sunsynk could not save cache {name}: {error}")

    async def age_cache(self, name):
        """Return the age in minutes of one cache file, or None when unavailable.

        Fails soft exactly like load_cache/save_cache: storage being absent (self.storage
        is None, the normal state for a standalone CLI run), raising, or the entry never
        having been written are all reported as None rather than propagating. Storage being
        absent is a permanent, by-design condition, not a fault, so it returns silently and
        leaves _restore_had_error untouched - unlike a REAL failure below, which still warns
        and still flags _restore_had_error so a transient storage outage is retried on a
        later call rather than being silently marked done with nothing restored.
        """
        if self.storage is None:
            return None
        try:
            return await self.storage.age(SUNSYNK_STORAGE_MODULE, name)
        except Exception as error:
            self.log(f"Warn: Sunsynk could not read cache age for {name}: {error}")
            self._restore_had_error = True
            return None

    async def save_static(self):
        """Persist discovery results, which change only when the hardware does."""
        await self.save_cache(SUNSYNK_CACHE_STATIC, {"device_list": self.device_list, "device_detail": self.device_detail})

    async def save_config(self):
        """Persist the last-read settings object, the baseline for read-modify-write."""
        await self.save_cache(SUNSYNK_CACHE_CONFIG, {"device_settings": self.device_settings})

    async def save_ratings(self):
        """Persist derived ratings so automatic_config can map args at startup."""
        await self.save_cache(SUNSYNK_CACHE_RATINGS, {"device_rated_power": self.device_rated_power})

    async def save_control(self):
        """Persist the applied-payload cache used for write change detection."""
        await self.save_cache(SUNSYNK_CACHE_CONTROL, {"applied_payload": self.applied_payload})

    async def restore_state(self):
        """Restore cached state at startup and seed each tier's clock from its file age.

        Telemetry is deliberately not cached: the live tier polls every few minutes, Home
        Assistant already retains the last published value of every entity, and
        publish_data only writes a sensor when it has a value — so a failed poll leaves
        the previous reading in place rather than overwriting it.

        _cache_restored is set only once this attempt has completed with no storage error,
        not on entry. Every storage access below already fails soft via load_cache/
        age_cache (they return {}/None rather than raising), so self.storage being None —
        the normal state for a standalone CLI run — or a backend that raises on every call
        cannot crash this method. But marking the guard unconditionally on entry would
        still lock cache restoration out for the life of the process after a single bad
        tick, since the guard is checked before any work happens and nothing would ever
        clear it again. _restore_had_error tracks whether load_cache/age_cache actually hit
        a storage failure during THIS attempt (as opposed to a cache simply not existing
        yet, which is not an error), and the guard is only set when they did not — so a
        later call, once storage recovers, tries again rather than silently doing nothing
        forever. A successful attempt still sets the guard unconditionally, so restore can
        never run twice and clobber live state with stale cached state.
        """
        if self._cache_restored:
            return
        self._restore_had_error = False

        static = await self.load_cache(SUNSYNK_CACHE_STATIC)
        if static:
            self.device_list = static.get("device_list", []) or []
            self.device_detail = static.get("device_detail", {}) or {}
            age = await self.age_cache(SUNSYNK_CACHE_STATIC)
            if age is not None:
                self.mark_refreshed("static", age)

        config = await self.load_cache(SUNSYNK_CACHE_CONFIG)
        if config:
            self.device_settings = config.get("device_settings", {}) or {}
            age = await self.age_cache(SUNSYNK_CACHE_CONFIG)
            if age is not None:
                self.mark_refreshed("config", age)

        ratings = await self.load_cache(SUNSYNK_CACHE_RATINGS)
        if ratings:
            self.device_rated_power = ratings.get("device_rated_power", {}) or {}

        # Bounded: restoring this asserts the inverter still holds what Predbat last wrote.
        # A redundant write is cheap; a skipped one lets the battery diverge from the plan.
        control_age = await self.age_cache(SUNSYNK_CACHE_CONTROL)
        if control_age is not None and control_age <= SUNSYNK_RESTORE_MAX_CONTROL:
            control = await self.load_cache(SUNSYNK_CACHE_CONTROL)
            self.applied_payload = control.get("applied_payload", {}) or {}
        elif control_age is not None:
            self.log(f"Info: Sunsynk control cache is {control_age:.1f} minutes old (limit {SUNSYNK_RESTORE_MAX_CONTROL}), forcing a rewrite")

        if not self._restore_had_error:
            self._cache_restored = True

    def tier_expired(self, tier, ttl_minutes):
        """Return True if a tier has never run or is older than its TTL."""
        last = self._tier_refreshed.get(tier)
        if last is None:
            return True
        return (time.time() - last) / 60.0 >= ttl_minutes

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Record that a tier just refreshed, or seed its clock from a cache age."""
        self._tier_refreshed[tier] = time.time() - (age_minutes * 60.0)

    async def automatic_config(self):
        """Register every discovered inverter as a SunsynkCloud Predbat inverter."""
        devices = [sn.lower() for sn in self.device_list]
        if not devices:
            self.log("Warn: Sunsynk automatic_config found no inverters")
            return
        self.set_arg_auto("inverter_type", ["SunsynkCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))
        self.set_arg_auto("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg_auto("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg_auto("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        # Own the sign flags rather than leaving them to whatever else configured this
        # install. base.args is shared and NOT namespaced per inverter type, so a component
        # that legitimately inverts its own grid sensor - teslemetry sets grid_power_invert
        # True, fox does the same - leaves that key set for every inverter index, and a
        # Sunsynk inverter that never claims it inherits the flip. The published sensor is
        # then correct and inverter.py negates it again, so an export reads as an import and
        # the power-flow arrow points the wrong way.
        #
        # All three are False because publish_data already emits Predbat's conventions:
        # grid negative on import (SUNSYNK_TELEMETRY_NEGATE), battery positive on discharge
        # and load positive, each confirmed live. Set explicitly, not left to the default,
        # so the value cannot depend on which components happen to share the install.
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])
        self.set_arg_auto("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        self.set_arg_auto("battery_temperature", [self._sensor_name(sn, "temperature") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])

        # Only map an arg when EVERY inverter reports the underlying value. An arg aimed at
        # a sensor that is never published is worse than an absent arg, which the user can
        # fill in via apps.yaml.
        for leaf in SUNSYNK_ENERGY:
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in self.device_list):
                self.set_arg_auto(leaf, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log(f"Warn: Sunsynk not every inverter reports {leaf}, it must be set manually in apps.yaml")

        if all(self.battery_capacity(sn) > 0 for sn in self.device_list):
            self.set_arg_auto("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: Sunsynk no battery capacity available for every inverter, soc_max must be set manually in apps.yaml")
        if all(self.battery_rate_max(sn) > 0 for sn in self.device_list):
            self.set_arg_auto("battery_rate_max", [self._sensor_name(sn, "battery_rate_max") for sn in devices])
        else:
            self.log("Warn: Sunsynk no battery charge-current limit available, battery_rate_max must be set manually in apps.yaml")
        if all(self.inverter_limit(sn) > 0 for sn in self.device_list):
            self.set_arg_auto("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: Sunsynk no ratePower reported, inverter_limit must be set manually in apps.yaml")
        # Without this Predbat falls back to inverter.py's effectively unlimited 99999W
        # default and plans exports the inverter simply clips. pvMaxLimit is the export cap
        # despite its "Inverter Power Limiter" label in the app - a G98/G99 site capped at
        # 3.68kW behind a much larger inverter is exactly the case this protects.
        # See SUNSYNK_EXPORT_LIMIT_FIELD.
        if all(self.export_limit(sn) > 0 for sn in self.device_list):
            self.set_arg_auto("export_limit", [self._sensor_name(sn, "export_limit") for sn in devices])
        else:
            self.log("Warn: Sunsynk no export power limit available, export_limit must be set manually in apps.yaml")
        if all(self.battery_reserve_min(sn) > 0 for sn in self.device_list):
            self.set_arg_auto("battery_min_soc", [self._sensor_name(sn, "battery_reserve_min") for sn in devices])

        self.set_arg_auto("reserve", [self._control_name("number", sn, "battery_schedule_reserve") for sn in devices])
        self.set_arg_auto("charge_start_time", [self._control_name("select", sn, "battery_schedule_charge_start_time") for sn in devices])
        self.set_arg_auto("charge_end_time", [self._control_name("select", sn, "battery_schedule_charge_end_time") for sn in devices])
        self.set_arg_auto("charge_limit", [self._control_name("number", sn, "battery_schedule_charge_soc") for sn in devices])
        self.set_arg_auto("charge_rate", [self._control_name("number", sn, "battery_schedule_charge_power") for sn in devices])
        self.set_arg_auto("scheduled_charge_enable", [self._control_name("switch", sn, "battery_schedule_charge_enable") for sn in devices])
        self.set_arg_auto("discharge_start_time", [self._control_name("select", sn, "battery_schedule_export_start_time") for sn in devices])
        self.set_arg_auto("discharge_end_time", [self._control_name("select", sn, "battery_schedule_export_end_time") for sn in devices])
        self.set_arg_auto("discharge_target_soc", [self._control_name("number", sn, "battery_schedule_export_soc") for sn in devices])
        self.set_arg_auto("discharge_rate", [self._control_name("number", sn, "battery_schedule_export_power") for sn in devices])
        self.set_arg_auto("scheduled_discharge_enable", [self._control_name("switch", sn, "battery_schedule_export_enable") for sn in devices])
        self.set_arg_auto("schedule_write_button", [self._control_name("switch", sn, "battery_schedule_charge_write") for sn in devices])

    async def refresh_static(self):
        """Re-discover inverters and refresh their static detail. Returns True if discovery worked.

        get_device_list() overwrites device_list with whatever discovery returned, which was
        harmless while discovery only ran at startup — but this tier re-runs every 8 hours in
        a long-lived process, and one transient failure must not take a working component
        down until the next success. Assigning the empty result and then marking the tier
        fresh and saving would additionally write {'device_list': []} to disk and stamp it
        fresh, so a restart would restore nothing and skip re-discovery for a full TTL.
        Absence of a result is not a result (deye.py refuses exactly this).

        The warning distinguishes "the account really has none" from "the discovery call
        itself failed" using get_device_list()'s discovery_ok flag (Fix 3) - both used to
        read identically ("discovery returned no inverters"), which is misleading during a
        genuine outage: the account has not changed, the API just could not be reached.
        """
        previous_devices = self.device_list
        await self.get_device_list()
        if not self.device_list:
            if previous_devices:
                self.device_list = previous_devices
                reason = "returned no inverters" if self.discovery_ok else "could not be reached"
                self.log(f"Warn: Sunsynk discovery {reason}, keeping the {len(previous_devices)} already known")
            # Neither marked nor saved either way: an empty discovery must be retried on the
            # next tick, not cached and then left alone for the next eight hours.
            return False
        for sn in self.device_list:
            try:
                await self.fetch_device_detail(sn)
            except Exception as error:
                self.log(f"Warn: Sunsynk inverter detail failed for {sn}: {error}")
        self.mark_refreshed("static")
        await self.save_static()
        await self.save_ratings()
        return True

    async def refresh_config(self):
        """Re-read the settings object for every inverter, logging any change made outside Predbat.

        Captures each inverter's previous baseline BEFORE the read: apply_settings only
        re-reads on a genuine plan change, so this poll is where nearly every externally
        made change (the phone app, the installer) is first observed, and fetch_settings
        overwrites device_settings[sn] in place - after the call there would be nothing
        left to compare against. The design spec names this logging as the mitigation for
        the read-modify-write race with other clients that a single whole-object write
        endpoint cannot otherwise avoid.

        Returns True when at least one inverter's read succeeded. The cache is only re-saved
        in that case (mirrors deye.py's refresh_config), so a tick where every read fails does not
        re-stamp a days-stale on-disk cache as fresh - which would otherwise make
        restore_state skip the config tier for a full TTL after a restart while running
        on stale settings.
        """
        got_any = False
        for sn in self.device_list:
            previous = dict(self.device_settings.get(sn, {}))
            try:
                settings = await self.fetch_settings(sn)
            except Exception as error:
                # One inverter's read raising must not cost the others theirs, nor abort
                # the rest of the tick (publishing, the success timestamp).
                self.log(f"Warn: Sunsynk settings read failed for {sn}: {error}")
                continue
            if settings:
                got_any = True
                self.note_external_change(sn, previous, settings)
            # Track whether the inverter has caught up with the last write. Latency of a
            # few minutes is normal; persistent divergence is worth surfacing.
            self.note_settle(sn, settings)
        self.mark_refreshed("config")
        if got_any:
            await self.save_config()
        return got_any

    async def refresh_live(self):
        """Poll telemetry for every inverter, reporting whether anything came back.

        The tier clock is only started when a poll actually succeeded. Marking it
        unconditionally would defeat run()'s first-cycle telemetry check: the retry after a
        deferred startup would find the tier "fresh", skip the poll entirely and then run
        automatic_config() with no telemetry after all — the very thing that check exists
        to prevent. Mirrors deye.py's refresh_live.
        """
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_device_data(sn):
                    got_any = True
            except Exception as error:
                self.log(f"Warn: Sunsynk telemetry poll failed for {sn}: {error}")
        if got_any:
            self.mark_refreshed("live")
        return got_any

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any schedule change.

        Returns True on a completed cycle, False on a failure that should hold the
        component in ComponentBase's startup backoff and be retried. Deliberately
        explicit rather than falling through to Python's implicit `None`: ComponentBase
        only clears its `first` flag and moves to the normal 60-second cadence when this
        returns something truthy, so an accidental `None` here would strand the component
        in the ever-growing startup backoff (60s doubling to 128 minutes) forever, even
        though every cycle after the first was actually working.
        """
        # The Predbat.com path holds an injected token with a real expiry, so it has to be
        # refreshed before anything is polled with it - nothing else in this component ever
        # would, and an expired token then breaks every call until the process restarts.
        # Returns True immediately for the two self-hosted auth methods.
        if not await self.check_and_refresh_oauth_token():
            self.log("Warn: Sunsynk OAuth token is invalid and could not be refreshed, skipping this cycle")
            return False

        if first:
            await self.restore_state()
            if not await self.fetch_token():
                self.log("Warn: Sunsynk login failed, the component will retry on the next cycle")
                return False

        if self.tier_expired("static", SUNSYNK_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: Sunsynk found no inverters on this account")
            return False
        if self.tier_expired("config", SUNSYNK_TTL_CONFIG):
            await self.refresh_config()
        live_ok = True
        if self.tier_expired("live", SUNSYNK_TTL_LIVE):
            live_ok = await self.refresh_live()

        for sn in self.device_list:
            # Read the control entities EVERY tick, the first one included. Home Assistant
            # retains them across a Predbat restart, so on a restart they already hold the
            # live plan; seeding local_schedule from _empty_schedule() and publishing that
            # back would overwrite it (dashboard_item reaches set_state_wrapper) and cancel
            # an in-flight charge until Predbat next replanned. get_schedule_settings_ha
            # falls back to the disabled default per field, so a genuinely cold start still
            # lands on exactly _empty_schedule()'s shape.
            try:
                await self.get_schedule_settings_ha(sn)
            except Exception as error:
                self.log(f"Warn: Sunsynk schedule read failed for {sn}: {error}")
            await self._reconcile_control(sn)
            # Published every tick, not just first: this is Predbat's control surface and
            # must keep reflecting local_schedule as it changes, matching deye.py's run().
            await self.publish_schedule_settings_ha(sn)

        await self.publish_data()

        if first and not live_ok:
            # Startup has not really succeeded without telemetry: automatic_config() runs on
            # the first cycle ALONE, so it would map only the args backed by cached ratings
            # and permanently skip soc_max, battery_rate_max and the energy args for the
            # whole session. Returning False leaves ComponentBase's `first` flag set, so the
            # entire startup path is retried on its backoff until a poll comes back.
            self.log("Warn: Sunsynk first telemetry poll returned nothing, deferring startup; it will be retried after a backoff")
            return False

        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state on shutdown so a restart resumes without re-polling."""
        await self.save_static()
        await self.save_config()
        await self.save_ratings()
        await self.save_control()


def _build_sunsynk(mock_base, args):  # pragma: no cover
    """Construct a SunsynkAPI around a MockBase for standalone command-line use.

    Passed into the constructor in a single call, matching deye.py's _build_deye:
    ComponentBase.__init__ already calls initialize(**kwargs), so a separate follow-up
    call to initialize() would re-run it a second time with the real args, printing a
    duplicate "SunsynkAPI initialising" / "control is disabled" pair before the CLI has
    even reported which region it is using.

    inverter_sn is passed straight through from --serial, exactly as deye.py's
    _build_deye does, so a serial restricts discovery itself (run() -> refresh_static() ->
    get_device_list()) rather than being trusted unverified after the fact.
    """
    return SunsynkAPI(
        mock_base,
        username=args.username,
        password=args.password,
        region=args.region,
        auth_method=args.auth_method,
        inverter_sn=args.serial,
        # The CLI is the verification tool, so control is on - but test_sunsynk_api still
        # asks before it sends anything to a real inverter, and run() only ever calls
        # apply_settings/apply_schedule for a serial already in control_active, which
        # starts empty on every fresh CLI process - so the default path stays read-only.
        control_enable=True,
        automatic=False,
    )


async def test_sunsynk_api(args):  # pragma: no cover
    """Run one read-only Sunsynk cycle via run(), then dump what it discovered.

    Drives the exact orchestration a live install uses - login, the tiered static/
    config/live refresh, publish_data, the control_active gate - rather than calling
    fetch_token/get_device_list/fetch_device_detail/fetch_device_data/fetch_settings
    individually, matching test_deye_api. Nobody on the project has a Sunsynk account, so
    this CLI is the only tool a remote tester has, and run() is where the real behaviour
    lives; a CLI that bypasses it cannot smoke-test what actually runs in production.

    Read-only by default: control_enable only gates apply_settings/apply_schedule, and
    run() only calls those for a serial already in control_active - empty here, since
    nothing has pressed the write button in this process. --write-test opts into a single
    harmless settings round-trip afterwards, unchanged from before.

    run() returns False on a failed login, an empty device list, or (on first) a failed
    telemetry poll. The old version of this CLI printed all three possibilities and told
    the user to go read the Warn: lines for which one actually happened - a real tester
    hit exactly that and could not tell a bad password from an empty account. Diagnosing
    precisely from the component's post-call state removes the guesswork:
      - no access token -> login itself failed (see fetch_token/self.last_api_error).
      - a token but no device_list -> login worked, discovery found nothing (or failed -
        see self.discovery_ok, Fix 3).
      - a device_list but run() still failed -> the first telemetry poll came back empty.
    These three are exhaustive for the auth methods this CLI exposes (password,
    password_legacy): check_and_refresh_oauth_token() only ever returns False for the
    oauth flow, which --auth-method cannot select.
    """
    mock_base = MockBase()
    client = _build_sunsynk(mock_base, args)
    print(f"Region {args.region} -> {client.base_url} (source={client.source}), auth={args.auth_method}")

    print("Calling run() once (read-only: login, discover, poll config/telemetry, publish)...")
    ok = await client.run(seconds=0, first=True)
    if not ok:
        if not client.access_token:
            print("\nLOGIN FAILED: no access token was returned.")
            if client.last_api_error:
                print(f"  Sunsynk said: {client.last_api_error}")
            print("  Check --username/--password, check --region, and if this region may still serve")
            print("  the older login, retry with --auth-method password_legacy.")
        elif not client.device_list:
            if client.discovery_ok:
                print("\nLOGIN SUCCEEDED, but the account returned no inverters.")
                if args.serial:
                    print(f"  --serial {args.serial!r} was set - a filter matching nothing looks identical to an empty account; retry without --serial to check.")
            else:
                print("\nLOGIN SUCCEEDED, but the inverter-discovery call itself failed (the account may still have inverters).")
                print("  Check the Warn: lines above for the transport/API error, and retry.")
        else:
            print(f"\nLOGIN and DISCOVERY both succeeded ({len(client.device_list)} inverter(s): {client.device_list}), but the first telemetry poll came back empty.")
            print("  Check the Warn: lines above for which realtime endpoint failed.")
        await client.final()
        return

    serials = client.device_list
    print(f"Inverters: {serials}")
    for sn in serials:
        print(f"\n--- {sn} detail ---")
        print(json.dumps(client.device_detail.get(sn, {}), indent=2, default=str))
        print(f"\n--- {sn} telemetry ---")
        print(json.dumps(client.device_values.get(sn, {}), indent=2, default=str))
        if args.dump_settings:
            print(f"\n--- {sn} settings ---")
            print(json.dumps(client.device_settings.get(sn, {}), indent=2, default=str))
        print(f"\nDerived: capacity={client.battery_capacity(sn):.2f} kWh, rate_max={client.battery_rate_max(sn):.0f} W, floor={client.battery_reserve_min(sn)}%")
        if args.write_test:
            # A deliberately harmless schedule: a self-use day at the inverter's own floor.
            schedule = {
                "reserve": max(client.battery_reserve_min(sn), 10),
                "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
                "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            }
            # Built from whatever run() already read into device_settings; empty when the
            # config-tier read failed for this serial, which build_settings_payload
            # reports by returning {} rather than a partial, unowned-field-dropping payload.
            payload = client.build_settings_payload(sn, schedule, current_soc=50)
            if not payload:
                print(f"\nNo settings baseline for {sn} (run() did not read its settings), skipping --write-test.")
                continue
            print(f"\n--- {sn} would write ---")
            print(json.dumps(payload, indent=2, default=str))
            # This is the only verification tool a remote tester has, so a closed/redirected
            # stdin (SSH, CI, a container with no TTY) must not crash it with a raw
            # traceback - EOFError and Ctrl-C both mean "no", cleanly.
            try:
                confirm = input("Send this to the inverter? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                print("\nNo input available, nothing sent.")
                continue
            if confirm.strip().lower() == "y":
                await client.apply_settings(sn, schedule, current_soc=50, force=True)
                print("Written. Re-reading in 60 seconds is the only way to confirm the dongle collected it.")

    await client.final()
    print("Done")


def main():  # pragma: no cover
    """Command-line entry point for Sunsynk diagnostics."""
    parser = argparse.ArgumentParser(description="Sunsynk Cloud API diagnostics")
    parser.add_argument("--username", required=True, help="Sunsynk Connect account e-mail")
    parser.add_argument("--password", required=True, help="Sunsynk Connect account password")
    parser.add_argument("--region", default="sunsynk", choices=sorted(SUNSYNK_REGIONS), help="API region")
    parser.add_argument("--auth-method", default="password", choices=["password", "password_legacy"], help="Login flow: RSA-encrypted (default) or the pre-2025 plaintext one")
    parser.add_argument("--serial", default=None, help="Restrict to one inverter serial")
    parser.add_argument("--dump-settings", action="store_true", help="Print the full settings object")
    parser.add_argument("--write-test", action="store_true", help="Build a harmless self-use payload and offer to send it")
    asyncio.run(test_sunsynk_api(parser.parse_args()))


if __name__ == "__main__":
    main()
