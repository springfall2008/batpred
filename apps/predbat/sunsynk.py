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
    SUNSYNK_PAGE_SIZE,
    SUNSYNK_MAX_DISCOVERY_PAGES,
    SUNSYNK_TELEMETRY,
    SUNSYNK_ENERGY,
    SUNSYNK_TELEMETRY_NEGATE,
    SUNSYNK_CAPACITY_AH_FIELD,
    SUNSYNK_CHARGE_VOLT_FIELD,
    SUNSYNK_MAX_CHARGE_CURRENT_FIELD,
    SUNSYNK_RATED_POWER_FIELD,
    SUNSYNK_BATTERY_LOW_CAP_FIELD,
    LIFEPO4_CHARGE_VOLTS_PER_CELL,
    LIFEPO4_NOMINAL_VOLTS_PER_CELL,
    SUNSYNK_WORKMODE,
    SUNSYNK_WORKMODE_FIELD,
    SUNSYNK_SOLAR_SELL_FIELD,
    SUNSYNK_TOU_ENABLE_FIELD,
    SUNSYNK_SERIAL_FIELD,
    SUNSYNK_DAY_FIELDS,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    TOU_FILLER_TIMES,
    FREEZE_EXPORT_SOC,
    SUNSYNK_SETTLE_POLLS,
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
        """
        serials = []
        seen = set()
        for page in range(1, SUNSYNK_MAX_DISCOVERY_PAGES + 1):
            params = {"page": str(page), "limit": str(SUNSYNK_PAGE_SIZE), "type": "-2", "status": "-1"}
            data = await self._get("inverter_list", params=params)
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
        for field in (SUNSYNK_CAPACITY_AH_FIELD, SUNSYNK_CHARGE_VOLT_FIELD, SUNSYNK_MAX_CHARGE_CURRENT_FIELD):
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
        """Read the whole settings object, which is both config and the write baseline."""
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
        # VERIFY@SPIKE — this rounds to the nearest whole cell assuming every pack charges
        # to (close enough to) LIFEPO4_CHARGE_VOLTS_PER_CELL. It is exact for a target that
        # is itself an exact multiple of 3.55V/cell, but a legitimate pack charged to a
        # different per-cell target - e.g. 24 cells at 3.45V/cell = 82.8V - rounds to 23
        # cells instead of 24, silently giving a nominal voltage (and therefore capacity and
        # battery_rate_max) about 4% wrong. See sunsynk_const.py for the full note; no live
        # hardware has confirmed the real spread of charge targets in use.
        cells = round(charge_volts / LIFEPO4_CHARGE_VOLTS_PER_CELL)
        if cells <= 0:
            return 0.0
        return cells * LIFEPO4_NOMINAL_VOLTS_PER_CELL

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
        amps = self._as_float(values.get(SUNSYNK_MAX_CHARGE_CURRENT_FIELD))
        volts = self.nominal_pack_voltage(values.get(SUNSYNK_CHARGE_VOLT_FIELD))
        if amps <= 0 or volts <= 0:
            return 0.0
        return amps * volts

    def battery_reserve_min(self, sn):
        """Return the inverter's own SOC floor as a percent, or 0 when unknown."""
        settings = self.device_settings.get(sn, {})
        return int(self._as_float(settings.get(SUNSYNK_BATTERY_LOW_CAP_FIELD)))

    def derive_control_state(self, schedule, current_soc):
        """Map Predbat's schedule intent to a Sunsynk control state (see the spec's table).

        Semantics are inherited from DEYE — the same registers sit behind both clouds —
        but every wire value comes from SUNSYNK_WORKMODE, never from DEYE's enum.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})

        if export.get("enable"):
            export_soc = int(export.get("soc", FREEZE_EXPORT_SOC))
            behaviour = "freeze_export" if export_soc >= FREEZE_EXPORT_SOC else "export"
            slot_soc = FREEZE_EXPORT_SOC if export_soc >= FREEZE_EXPORT_SOC else export_soc
            return {"behaviour": behaviour, "work_mode": SUNSYNK_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": slot_soc, "power": int(export.get("power", 0))}

        if charge.get("enable"):
            charge_soc = int(charge.get("soc", 0))
            if charge_soc > current_soc and charge_soc > reserve:
                return {"behaviour": "charge", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": charge_soc, "power": int(charge.get("power", 0))}
            if charge_soc == reserve:
                return {"behaviour": "freeze_charge", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}
            return {"behaviour": "hold_charge", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}

        return {"behaviour": "idle", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": 0}

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

    def _self_use_slot(self, start_time, reserve):
        """Build a self-use slot holding at the reserve SOC."""
        return {"time": start_time, "power": 0, "soc": int(reserve), "grid_charge": False}

    def _action_slot(self, start_time, state):
        """Build a slot realising a derived control state."""
        return {"time": start_time, "power": int(state["power"]), "soc": int(state["slot_soc"]), "grid_charge": bool(state["grid_charge"])}

    def build_tou_slots(self, schedule, current_soc):
        """Build exactly TOU_SLOT_COUNT ordered slots covering 24h from the schedule windows.

        Slots are sequential intervals, so every start time must be distinct and ascending.
        Segment boundaries are collected from a 00:00 self-use baseline plus each enabled
        window's start (its action) and end (back to self-use), then padded with fillers
        and trimmed to the earliest, most imminent TOU_SLOT_COUNT.
        """
        reserve = int(schedule.get("reserve", 0))
        idle = {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None}
        segments = {"00:00": dict(idle)}
        for direction in ("charge", "export"):
            window = schedule.get(direction, {})
            if not (window.get("enable") and window.get("start") and window.get("end")):
                continue
            intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
            intent[direction] = {"enable": True, "soc": window.get("soc", 0), "power": window.get("power", 0)}
            segments[self._to_slot_time(window["start"])] = self.derive_control_state(intent, current_soc)
            segments.setdefault(self._to_slot_time(window["end"]), dict(idle))

        slots = []
        for start_time, state in sorted(segments.items(), key=lambda item: item[0]):
            if state.get("grid_charge") or state.get("solar_sell") or state.get("power"):
                slots.append(self._action_slot(start_time, state))
            else:
                slots.append(self._self_use_slot(start_time, reserve))

        used = {slot["time"] for slot in slots}
        for filler in TOU_FILLER_TIMES:
            if len(slots) >= TOU_SLOT_COUNT:
                break
            if filler not in used:
                slots.append(self._self_use_slot(filler, reserve))
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
        intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
        if self._window_active(export, now_minutes):
            intent["export"] = {"enable": True, "soc": export.get("soc", 0), "power": export.get("power", 0)}
        elif self._window_active(charge, now_minutes):
            intent["charge"] = {"enable": True, "soc": charge.get("soc", 0), "power": charge.get("power", 0)}
        return self.derive_control_state(intent, current_soc)

    def build_settings_payload(self, sn, schedule, current_soc, now_minutes=None):
        """Build the full settings object to POST for one inverter.

        Read-modify-write: start from the last-read settings so every field Predbat does
        not own survives verbatim, then overwrite only the slots, mode and flags it does.
        """
        if now_minutes is None:
            now_minutes = self._now_minutes()
        slots = self.build_tou_slots(schedule, current_soc)
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

        payload = dict(self.device_settings.get(sn, {}))
        payload[SUNSYNK_SERIAL_FIELD] = sn
        payload[SUNSYNK_WORKMODE_FIELD] = active["work_mode"]
        payload[SUNSYNK_SOLAR_SELL_FIELD] = encode_setting(SUNSYNK_SOLAR_SELL_FIELD, "1" if active["solar_sell"] else "0")
        payload[SUNSYNK_TOU_ENABLE_FIELD] = encode_setting(SUNSYNK_TOU_ENABLE_FIELD, "1")
        for day in SUNSYNK_DAY_FIELDS:
            payload[day] = encode_setting(day, True)
        for index, slot in enumerate(slots, start=1):
            payload[TOU_FIELD["time"].format(n=index)] = encode_setting(TOU_FIELD["time"].format(n=index), slot["time"])
            payload[TOU_FIELD["power"].format(n=index)] = encode_setting(TOU_FIELD["power"].format(n=index), slot["power"])
            payload[TOU_FIELD["soc"].format(n=index)] = encode_setting(TOU_FIELD["soc"].format(n=index), slot["soc"])
            payload[TOU_FIELD["grid_charge"].format(n=index)] = encode_setting(TOU_FIELD["grid_charge"].format(n=index), slot["grid_charge"])
        return payload

    def payloads_equal(self, a, b):
        """Compare two settings payloads for change detection."""
        return dict(a or {}) == dict(b or {})

    async def apply_settings(self, sn, schedule, current_soc, force=False):
        """Read, modify and write the settings object for one inverter.

        Returns True if a write was performed. Fails closed: without a fresh read there is
        no baseline to modify, so nothing is written rather than posting a payload that
        would drop every field Predbat does not own.
        """
        if not self.control_enable:
            return False

        # Re-read immediately before writing so the race with the Sunsynk phone app is as
        # small as possible, and so unowned fields carry the newest values.
        previous_settings = dict(self.device_settings.get(sn, {}))
        settings = await self.fetch_settings(sn)
        if not settings:
            self.log(f"Warn: Sunsynk {sn} settings read failed, skipping the write (no baseline to modify)")
            return False
        self.note_external_change(sn, previous_settings, settings)

        payload = self.build_settings_payload(sn, schedule, current_soc)
        previous = self.applied_payload.get(sn)
        if previous and self.payloads_equal(previous, payload) and not force:
            # Nothing changed, so do not churn the dongle. Track how long the read-back has
            # disagreed; normal cloud-to-dongle latency is one to five minutes.
            return False

        # _post reports failure as None. A successful settings write carries no data
        # payload, so {} means "written, nothing returned" and only None means failure.
        response = await self._post("settings_set", sn=sn, body=payload)
        if response is None:
            self.log(f"Warn: Sunsynk {sn} settings write failed, the plan has not reached the inverter")
            return False
        self.applied_payload[sn] = payload
        self.settle_count[sn] = 0
        self.log(f"Info: Sunsynk {sn} settings written ({self._active_state(schedule, current_soc, self._now_minutes())['behaviour']})")
        return True

    def note_settle(self, sn, settings):
        """Track how many cycles the inverter has disagreed with the last write.

        A write is acknowledged by the cloud long before the dongle collects it, so
        divergence within SUNSYNK_SETTLE_POLLS cycles is normal latency, not a failure.
        """
        applied = self.applied_payload.get(sn)
        if not applied or not settings:
            return
        owned = [SUNSYNK_WORKMODE_FIELD] + [TOU_FIELD[c].format(n=n) for n in range(1, TOU_SLOT_COUNT + 1) for c in ("time", "soc", "grid_charge")]
        if all(str(settings.get(key)) == str(applied.get(key)) for key in owned):
            self.settle_count[sn] = 0
            return
        self.settle_count[sn] = self.settle_count.get(sn, 0) + 1
        if self.settle_count[sn] > SUNSYNK_SETTLE_POLLS:
            self.log(f"Warn: Sunsynk {sn} has not applied Predbat's settings after {self.settle_count[sn]} cycles; check the inverter is online in the Sunsynk app")

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
