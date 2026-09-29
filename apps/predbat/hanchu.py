# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Hanchu ESS cloud API library
# -----------------------------------------------------------------------------

"""Hanchu ESS cloud integration for Predbat.

Registers each Hanchu DTU discovered on the user's Hanchu app account as a ``HanchuCloud``
Predbat inverter, publishing monitoring sensors and schedule control entities. The inverter
owns the timing: Predbat writes a timed charge window and a timed discharge window into the
device's own slot table and the device acts on them, so there is no per-minute work mode to
derive.

NO VENDOR DOCUMENTATION EXISTS. Every endpoint, field and parameter range used here was
learned by reading three MIT-licensed Home Assistant integrations, and the provenance of each
one - and whether it was read once, corroborated across repos, or merely inferred - is recorded
in hanchu_const.py. Read that file's header before trusting anything here. No code, structure
or comment from those integrations is reproduced; only the wire facts.

WRITES ARE BATCHED, DELIBERATELY. The reference integration stages every control change in
memory and flushes the whole set in ONE iotSet call behind an explicit button, and says in its
own docstring that this exists to avoid rate limiting. Predbat re-plans every few minutes and
touches up to nineteen settings, so a per-setting write would be nineteen calls a cycle against
an API with no documented budget. build_write_payload() therefore assembles the complete
control state as a single dict, _write_payload() sends it in one call, and it is sent only when
it differs from the last payload the device accepted.

AUTH IS A PASSWORD LOGIN, not OAuth. The user's own Hanchu app e-mail and password are
exchanged for a session token, which is refreshed proactively at ~25 days and re-obtained by
logging in again if the API ever answers 401. Predbat holds the credentials, so an expired or
rejected token is always recoverable without user action - unlike the reference integration,
which has to ask Home Assistant to re-authenticate.

TWO SOC LIMITS ARE NARROWER THAN PREDBAT'S. The charge target key accepts 50-100 and the
discharge floor key accepts 5-45. A plan asking to charge to 40% or to export down to 60%
cannot be expressed on this hardware at all. Those values are CLAMPED and the clamp is logged,
because sending an out-of-range value would either be rejected or silently ignored, and both
look identical to a window that never ran.
"""

import asyncio
import json
import time
import aiohttp
from component_base import ComponentBase
from coordinator import inverter_record
from hanchu_const import (
    HANCHU_ACK_MAP,
    HANCHU_ACK_OK,
    HANCHU_BASE_URL,
    HANCHU_CACHE_CONFIG,
    HANCHU_CACHE_CONTROL,
    HANCHU_CACHE_STATIC,
    HANCHU_CAPACITY_FIELD,
    HANCHU_CODE_FAILED,
    HANCHU_CODE_REFRESH_REJECTED,
    HANCHU_CODE_UNAUTHORISED,
    HANCHU_CONTROL_KEYS,
    HANCHU_DEBUG_REDACT_KEYS,
    HANCHU_DEV_TYPE_DTU,
    HANCHU_DEVICE_SN_FIELD,
    HANCHU_DEVICE_TYPE_FIELD,
    HANCHU_ENDPOINTS,
    HANCHU_ENERGY,
    HANCHU_HEADER_LOCALE,
    HANCHU_HEADER_PLATFORM,
    HANCHU_HEADER_TOKEN,
    HANCHU_KEY_CHARGE_END,
    HANCHU_KEY_CHARGE_POWER,
    HANCHU_KEY_CHARGE_SOC,
    HANCHU_KEY_CHARGE_START,
    HANCHU_KEY_DISCHARGE_END,
    HANCHU_KEY_DISCHARGE_POWER,
    HANCHU_KEY_DISCHARGE_SOC,
    HANCHU_KEY_DISCHARGE_START,
    HANCHU_KEY_WORK_MODE,
    HANCHU_MENU_DATA,
    HANCHU_MENU_ITEMS,
    HANCHU_MENU_MAX,
    HANCHU_MENU_MIN,
    HANCHU_MENU_SECTION,
    HANCHU_MENU_SIGNAL,
    HANCHU_POWER_STEP,
    HANCHU_RANGES,
    HANCHU_REFRESH_MIN_INTERVAL,
    HANCHU_RETRIES,
    HANCHU_SLOT_COUNT,
    HANCHU_SLOT_DISABLED,
    HANCHU_SOC_SCALE,
    HANCHU_STATION_FIELD,
    HANCHU_STORAGE_MODULE,
    HANCHU_TELEMETRY,
    HANCHU_TELEMETRY_NEGATE,
    HANCHU_TELEMETRY_UNIT_FIELD,
    HANCHU_TIMEOUT,
    HANCHU_TOKEN_REFRESH_SECONDS,
    HANCHU_TTL_CONFIG,
    HANCHU_TTL_ENERGY,
    HANCHU_TTL_POWER,
    HANCHU_TTL_STATIC,
    HANCHU_WORK_MODE_FOR_SCHEDULE,
    HANCHU_WORK_MODES,
    HANCHU_WRITE_RETRY_DELAY,
    clamp_range,
    hhmmss_to_seconds,
    scale_to_watts,
    seconds_to_hhmmss,
    split_window_seconds,
    window_is_empty,
)

# The behaviour a HanchuCloud inverter has, stated for the discovery record's capabilities. A
# literal, never read back from INVERTER_DEF: the record has to rebuild the row on its own, or the
# completeness test proves nothing.
#
# support_feedin_first is False because there is no feed-in-first work mode among the four this API
# exposes. charge_control_immediate is False because everything goes through the timed slot table -
# the one immediate primitive this API has (fastChargeDischarge) is duration-based and would lapse
# behind Predbat's back, so it is deliberately unused.
HANCHU_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}


class HanchuAPI(ComponentBase):
    """Hanchu ESS cloud component."""

    # Trace every request and response while this integration beds in. There is no vendor
    # specification and nobody on the project has Hanchu hardware, so a tester's log is the only
    # evidence available for everything hanchu_const.py marks INFERRED. Flip to False once the
    # inferred items have been confirmed.
    api_debug = True

    def initialize(
        self,
        account="",
        password="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=True,
        work_mode_control=True,
        battery_rate_max=None,
        api_delay=1,
        min_write_interval=60,
        **kwargs,
    ):
        """Initialise the Hanchu component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has already
        resolved each arg from its hanchu_* config key and passes it BY ARG NAME (e.g. account <-
        hanchu_account), exactly like alphaess/sunsynk/fox. Consume the kwargs directly - do NOT
        re-derive with get_arg("account"): that bare name is not in apps.yaml (the key is
        hanchu_account), so it would always return the default.
        """
        self.log("Info: HanchuAPI initialising")
        self.account = account or ""
        self.password = password or ""
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.work_mode_control = work_mode_control
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_rate_max_override = float(battery_rate_max) if battery_rate_max else 0.0
        self.api_delay = max(0, float(api_delay or 0))
        self.min_write_interval = max(0, int(min_write_interval or 0))
        # An instance attribute rather than the constant used inline, so the unit tests can zero it:
        # it is a real wall-clock pause between a failed write and its one retry, and nothing in a
        # test talks to the real cloud. Not a config key - there is no reason a user would tune it.
        self.write_retry_delay = HANCHU_WRITE_RETRY_DELAY

        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_energy = {}
        # Per-serial control values last READ back from the device, and the per-serial parameter
        # ranges the menu endpoint reports. Ranges are per device because the menu overrides the
        # built-in defaults - a 3.6 kW unit and a 5 kW unit do not share a charge power ceiling.
        self.device_settings = {}
        self.device_ranges = {}
        self.local_schedule = {}
        # The last payload the device ACCEPTED, per serial. Serves three purposes: change
        # detection (so an unchanged plan sends nothing), the pacing baseline, and the state a
        # restart resumes from.
        self.applied_payload = {}
        self.last_write_time = {}
        # One settings write in flight at a time. The schedule button and the reconcile loop can
        # both reach _write_payload, and a single call can take 15-45s; two overlapping control
        # writes are refused by the cloud with error 300004 (HARDWARE, batpred#5305).
        self.write_lock = asyncio.Lock()
        # Serials Predbat has actually been asked to drive, i.e. whose write button has been
        # pressed at least once. The reconcile loop only re-applies for these, so a startup cycle
        # can never clobber an inverter's existing slots before there is a plan to apply.
        self.control_active = set()

        self._token = ""
        self._token_time = 0.0
        self._last_refresh_attempt = 0.0
        self._tier_refreshed = {}
        self._cache_restored = False
        self._restore_had_error = False
        # Clamps already reported, keyed (sn, key), so a limitation that persists for hours is
        # explained once rather than every cycle.
        self._clamp_logged = set()
        # The most recent body-level API failure message (msg only - never a credential), and
        # whether the last discovery attempt actually reached the API, so an empty account can be
        # told apart from a failed call.
        self.last_api_error = ""
        self.discovery_ok = None
        if not self.control_enable:
            self.log("Info: Hanchu control is disabled (hanchu_control_enable is false); monitoring only")
        if not self.work_mode_control:
            self.log("Info: Hanchu work mode control is disabled (hanchu_work_mode_control is false); the timed windows are written but the work mode is left exactly as the user set it")

    # ------------------------------------------------------------------
    # Request layer
    # ------------------------------------------------------------------

    def _headers(self):
        """Return the headers every Hanchu request carries.

        appPlat identifies the client to the server and locale selects the language of any message
        it sends back. The session token goes in its own header, not in Authorization.
        """
        headers = {"Content-Type": "application/json", "Accept": "*/*", "appPlat": HANCHU_HEADER_PLATFORM, "locale": HANCHU_HEADER_LOCALE}
        if self._token:
            headers[HANCHU_HEADER_TOKEN] = self._token
        return headers

    @staticmethod
    def redact(payload, path=""):
        """Return a log-safe copy of a payload with credentials masked.

        Two things are masked. The credential KEYS, which covers the login body's password. And,
        for the login and refresh endpoints ONLY, the envelope's `data` field: on those two paths
        `data` IS the session token - a bare string, not an object - so masking by key name alone
        would put full control of the user's inverter into the log. One reference integration masks
        exactly these two paths' `data` for exactly this reason.

        Everything else is left intact deliberately. code, msg and every data field of every other
        endpoint are always logged: there is no vendor specification for this API, so a tester's
        trace is the only evidence available for what it actually returns.
        """
        if not isinstance(payload, dict):
            return payload
        redact_keys = set(HANCHU_DEBUG_REDACT_KEYS)
        if path in (HANCHU_ENDPOINTS["login"], HANCHU_ENDPOINTS["refresh"]):
            redact_keys.add("data")
        return {key: ("***" if key in redact_keys else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response while api_debug is on.

        `what` is the endpoint path, and is passed to redact() as well as logged: the login and
        refresh responses carry the token in `data`, so redaction depends on which path this is.
        """
        if not self.api_debug:
            return
        if payload is None:
            self.log("Info: Hanchu API {} {}".format(direction, what))
            return
        try:
            rendered = json.dumps(self.redact(payload, path=what), default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(payload)[:2000]
        self.log("Info: Hanchu API {} {} {}".format(direction, what, rendered))

    async def _post(self, endpoint_key, body=None, anonymous=False):
        """Perform one API call, returning (ok, data).

        `ok` is the envelope's own success flag, not merely "no exception was raised". A
        body-level failure and a transport failure are both False, but only the body-level one
        stamps last_api_error, because only it carries the API's own reason.

        A 401 - which this API answers as HTTP 200 carrying code 401, as well as by status - is
        handled here exactly once per call: the token is refreshed (or a fresh login performed)
        and the request is retried a single time. That retry is earned by the re-auth and is
        independent of the transport attempt budget, so an expiry landing on the last transport
        attempt is still recovered rather than discarded.

        Never raises, so every caller fails closed.
        """
        path = HANCHU_ENDPOINTS[endpoint_key]
        url = "{}{}".format(HANCHU_BASE_URL, path)
        timeout = aiohttp.ClientTimeout(total=HANCHU_TIMEOUT)
        auth_retried = False
        attempt = 0

        # A while loop rather than for-in-range: `attempt` counts transport failures only. The
        # re-auth branch deliberately does not touch it, and `auth_retried` caps that to one extra
        # pass, so total work stays bounded while an expiry never costs the call.
        while attempt < HANCHU_RETRIES:
            self.debug_api("request", path, body if body is not None else {})
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._headers(), json=body if body is not None else {}) as response:
                        status = response.status
                        payload = await response.json(content_type=None) if status in (200, HANCHU_CODE_UNAUTHORISED) else None
            # Deliberately broad: the docstring promises this never raises, so every caller can fail
            # closed. A narrower tuple let a bare OSError from the socket layer escape run().
            except Exception as error:
                attempt += 1
                if attempt >= HANCHU_RETRIES:
                    self.log("Warn: Hanchu {} transport failure: {}".format(path, error))
                    return False, None
                await asyncio.sleep(2 * attempt)
                continue

            if status != 200 and status != HANCHU_CODE_UNAUTHORISED:
                self.log("Warn: Hanchu {} returned HTTP {}".format(path, status))
                attempt += 1
                if attempt >= HANCHU_RETRIES:
                    return False, None
                await asyncio.sleep(2 * attempt)
                continue

            self.debug_api("response", path, payload if isinstance(payload, dict) else {"data": payload})
            if not isinstance(payload, dict):
                self.log("Warn: Hanchu {} returned a non-object body".format(path))
                return False, None

            code = payload.get("code")
            # An expired token shows up either as the HTTP status or as code 401 inside a 200.
            if (status == HANCHU_CODE_UNAUTHORISED or code == HANCHU_CODE_UNAUTHORISED) and not anonymous and not auth_retried:
                auth_retried = True
                self.log("Info: Hanchu {} reported an expired session, re-authenticating".format(path))
                if await self.reauthenticate():
                    continue
                return False, None

            if payload.get("success"):
                return True, payload.get("data")
            message = payload.get("msg")
            self.last_api_error = str(message) if message is not None else ""
            if code == HANCHU_CODE_FAILED:
                self.log("Warn: Hanchu {} was rejected by the device: {}".format(path, self.last_api_error))
            else:
                self.log("Warn: Hanchu {} was unsuccessful (code {}): {}".format(path, code, self.last_api_error))
            return False, payload.get("data")

        return False, None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def token_expired(self):
        """Return True when the held token is old enough to be refreshed proactively.

        The real server-side lifetime is unknown; the interval is the reference integration's own
        safety margin rather than a documented figure, which is why a reactive 401 path exists too.
        """
        if not self._token:
            return True
        return (time.time() - self._token_time) >= HANCHU_TOKEN_REFRESH_SECONDS

    async def fetch_token(self):
        """Log in with the account credentials and store the session token. True on success.

        The login body carries the password in PLAINTEXT over TLS, which is what the maintained
        reference integration does. A third integration RSA-encrypts it against a different
        endpoint; that path is not used, and the disagreement is recorded in hanchu_const.py.
        """
        if not self.account or not self.password:
            return False
        ok, data = await self._post("login", body={"account": self.account, "pwd": self.password}, anonymous=True)
        # The token arrives as a bare string in `data`, not as an object with a token field.
        token = data if isinstance(data, str) and data else ""
        if not ok or not token:
            self.log("Warn: Hanchu login failed; check hanchu_account and hanchu_password - they are the e-mail and password you use in the Hanchu app{}".format(" ({})".format(self.last_api_error) if self.last_api_error else ""))
            return False
        self._token = token
        self._token_time = time.time()
        self.log("Info: Hanchu login succeeded")
        return True

    async def refresh_token(self):
        """Exchange the held token for a fresh one, falling back to a full login. True on success.

        A rejected refresh is NOT fatal here, unlike in the reference integration: Predbat holds
        the account credentials, so it can simply log in again instead of asking the user to
        re-authenticate. The suppression window stops a burst of expiries becoming a refresh storm.
        """
        if not self._token:
            return await self.fetch_token()
        if (time.time() - self._last_refresh_attempt) < HANCHU_REFRESH_MIN_INTERVAL:
            # Another path refreshed moments ago. Report whether a token is still held rather
            # than spending a second call on the same expiry.
            return bool(self._token)
        self._last_refresh_attempt = time.time()
        ok, data = await self._post("refresh", body={"token": self._token}, anonymous=True)
        token = data if isinstance(data, str) and data else ""
        if ok and token:
            self._token = token
            self._token_time = time.time()
            self.log("Info: Hanchu session token refreshed")
            return True
        self.log("Info: Hanchu token refresh was rejected{}, logging in again".format(" (code {})".format(HANCHU_CODE_REFRESH_REJECTED) if not ok else ""))
        return await self.fetch_token()

    async def reauthenticate(self):
        """Recover a usable session after an auth failure. True if a retry is worth making."""
        return await self.refresh_token()

    async def ensure_token(self):
        """Make sure a usable token is held, logging in or refreshing as needed. True on success."""
        if not self._token:
            return await self.fetch_token()
        if self.token_expired():
            return await self.refresh_token()
        return True

    # ------------------------------------------------------------------
    # Discovery and telemetry
    # ------------------------------------------------------------------

    @staticmethod
    def _as_float(value, default=0.0):
        """Coerce an API value to float, returning default for None/'unknown'/junk."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def get_device_list(self):
        """Discover every DTU on the account, returning its serials.

        Only devType "2" - the datalogger the control endpoints are addressed to - is kept. Sets
        discovery_ok so an empty account can be told apart from a failed call: the two are
        indistinguishable from the returned list alone.
        """
        ok, data = await self._post("device_list")
        if not ok:
            self.discovery_ok = False
            return list(self.device_list)
        self.discovery_ok = True
        wanted = [str(sn).lower() for sn in self.inverter_sn_filter]
        serials = []
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            sn = entry.get(HANCHU_DEVICE_SN_FIELD)
            if not sn:
                continue
            dev_type = str(entry.get(HANCHU_DEVICE_TYPE_FIELD, HANCHU_DEV_TYPE_DTU))
            if dev_type != HANCHU_DEV_TYPE_DTU:
                # Named rather than skipped silently, so a device Predbat recognised and passed
                # over does not look identical to one it never saw.
                self.log("Info: Hanchu skipping {} - it reports device type {}, and only the DTU (type {}) accepts the control endpoints".format(sn, dev_type, HANCHU_DEV_TYPE_DTU))
                continue
            if wanted and str(sn).lower() not in wanted:
                continue
            serials.append(sn)
        self.device_list = serials
        return serials

    async def refresh_static(self):
        """Re-discover devices. True when discovery worked.

        Branches on discovery_ok rather than on list emptiness: a failed call and an empty account
        both produce an empty list but need different handling. Deliberately does NOT assign an
        empty discovery result over a working device_list - this tier re-runs every eight hours, so
        one transient failure must not take a working component down, and assigning the empty
        result would additionally cache it and stamp the tier fresh, so a restart would restore
        nothing and skip re-discovery for a full TTL. Absence of a result is not a result.
        """
        previous = list(self.device_list)
        serials = await self.get_device_list()
        if self.discovery_ok is False:
            self.device_list = previous
            if previous:
                self.log("Warn: Hanchu discovery failed; keeping the {} previously known device(s)".format(len(previous)))
            else:
                self.log("Warn: Hanchu device discovery failed; the account may still have devices")
            return False
        if not serials:
            if self.inverter_sn_filter:
                self.log("Warn: Hanchu discovery succeeded but the configured serial filter {} matched nothing on this account".format(self.inverter_sn_filter))
            else:
                self.log("Warn: Hanchu this account has no DTU devices on it")
            return False
        self.mark_refreshed("static")
        await self.save_static()
        return True

    def tier_expired(self, tier, ttl_minutes):
        """Return True when a refresh tier is due, or has never run."""
        age = self._tier_refreshed.get(tier)
        if age is None:
            return True
        return (time.time() - age) >= (ttl_minutes * 60)

    def mark_refreshed(self, tier):
        """Start a tier's clock. Called ONLY on a successful refresh.

        Marking unconditionally would defeat the first-cycle checks in run(): a retry after a
        deferred startup would find the tier "fresh", skip the poll entirely and then run
        automatic_config() with no data after all - the very thing those checks exist to prevent.
        """
        self._tier_refreshed[tier] = time.time()

    def _apply_status_payload(self, sn, payload):
        """Map one getDeviceStatus object into device_values and device_detail.

        Returns False without a battery SoC, which is the one field Predbat cannot plan without.
        Power fields are normalised to watts through their own sibling unit string where the API
        provides one, and the two whose sign is opposite to Predbat's convention are negated here
        rather than at publish time, so everything downstream reads one convention.
        """
        if not isinstance(payload, dict) or payload.get(HANCHU_TELEMETRY["soc"]) is None:
            return False
        values = {}
        for leaf, field in HANCHU_TELEMETRY.items():
            raw = payload.get(field)
            if raw is None:
                continue
            if leaf == "soc":
                values[leaf] = round(self._as_float(raw) * HANCHU_SOC_SCALE, 1)
                continue
            watts = scale_to_watts(raw, payload.get(HANCHU_TELEMETRY_UNIT_FIELD.get(leaf)))
            if watts is None:
                continue
            values[leaf] = -watts if leaf in HANCHU_TELEMETRY_NEGATE else watts
        self.device_values[sn] = values

        detail = self.device_detail.setdefault(sn, {})
        capacity = self._as_float(payload.get(HANCHU_CAPACITY_FIELD), 0.0)
        if capacity > 0:
            detail["capacity_kwh"] = capacity
        station = payload.get(HANCHU_STATION_FIELD)
        if station:
            detail["station_id"] = str(station)
        return True

    async def refresh_power(self):
        """Poll live telemetry for every device. True when at least one reported a SoC."""
        got_any = False
        for sn in list(self.device_list):
            ok, data = await self._post("device_status", body={"sn": sn})
            if ok and self._apply_status_payload(sn, data):
                got_any = True
            else:
                self.log("Warn: Hanchu {} returned no usable live telemetry".format(sn))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("power")
        return got_any

    async def refresh_energy(self):
        """Poll the daily energy counters for every device. True when at least one reported."""
        got_any = False
        for sn in list(self.device_list):
            ok, data = await self._post("device_statistics", body={"sn": sn})
            if not ok or not isinstance(data, dict):
                continue
            energy = {}
            for leaf, field in HANCHU_ENERGY.items():
                if data.get(field) is not None:
                    energy[leaf] = round(self._as_float(data.get(field)), 3)
            if energy:
                self.device_energy[sn] = energy
                got_any = True
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("energy")
        return got_any

    def _parse_menu_ranges(self, payload):
        """Extract per-device parameter ranges from a menu response, as {key: (min, max)}.

        The menu is the phone app's own settings screen: a section of GROUPS, each a list of items,
        each item naming the control key it edits and the bounds the app enforces for it. Only keys
        this component has a built-in default for are taken, so an unexpected item cannot introduce
        a range for something Predbat never writes. An item whose bounds do not parse is skipped
        rather than defaulting, because a wrong range silently clamps a good plan.
        """
        ranges = {}
        section = (payload or {}).get(HANCHU_MENU_DATA, {}) if isinstance(payload, dict) else {}
        energy = section.get(HANCHU_MENU_SECTION) if isinstance(section, dict) else None
        if not isinstance(energy, dict):
            return ranges
        for group in energy.get(HANCHU_MENU_ITEMS) or []:
            for item in group if isinstance(group, list) else []:
                if not isinstance(item, dict):
                    continue
                key = item.get(HANCHU_MENU_SIGNAL)
                if key not in HANCHU_RANGES:
                    continue
                try:
                    low = int(float(item[HANCHU_MENU_MIN]))
                    high = int(float(item[HANCHU_MENU_MAX]))
                except (KeyError, TypeError, ValueError):
                    continue
                if high > low:
                    ranges[key] = (low, high)
        return ranges

    def ranges_for(self, sn):
        """Return the parameter ranges to clamp against for one device.

        The menu's own bounds where it reported them, and the built-in defaults for everything
        else. Built per call rather than cached as a merged dict so a later menu poll cannot leave
        a stale entry behind.
        """
        merged = dict(HANCHU_RANGES)
        merged.update(self.device_ranges.get(sn) or {})
        return merged

    async def refresh_config(self):
        """Read each device's parameter ranges and current control values. True when any answered.

        Both come from one call each. The control read is a SINGLE iotGet carrying every key, for
        the same reason the write is a single iotSet: nineteen separate reads a cycle against an
        API with no documented budget is exactly what the reference integration's batching exists
        to avoid.
        """
        got_any = False
        for sn in list(self.device_list):
            ok, menu = await self._post("menu", body={"sn": sn})
            if ok:
                ranges = self._parse_menu_ranges(menu)
                if ranges:
                    self.device_ranges[sn] = ranges
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
            ok, data = await self._post("iot_get", body={"sn": sn, "devType": HANCHU_DEV_TYPE_DTU, "keys": list(HANCHU_CONTROL_KEYS)})
            if ok and isinstance(data, dict):
                self.device_settings[sn] = dict(data)
                got_any = True
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("config")
            await self.save_config()
        return got_any

    def battery_capacity(self, sn):
        """Return the usable battery capacity in kWh, or 0.0 when the device has not reported it."""
        return self._as_float(self.device_detail.get(sn, {}).get("capacity_kwh"), 0.0)

    def battery_rate_max(self, sn):
        """Return the battery charge/discharge power limit in watts.

        Taken from the CHARGE POWER key's own upper bound, which the menu reports per device - the
        only power ceiling this API exposes. Leaving battery_rate_max unmapped is NOT neutral:
        inverter.py then uses a hard-coded 2600 W silently on every plan, and makes it the
        governing term in the charge rate calculation, so an absent value is worse than this one.
        The user's override wins where they know the pack's real limit.
        """
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        return float(self.ranges_for(sn).get(HANCHU_KEY_CHARGE_POWER, (0, 0))[1])

    def inverter_limit(self, sn):
        """Return the inverter's nominal AC power in watts.

        The API reports no inverter rating, so the discharge power ceiling stands in for it: it is
        the largest AC figure the device will accept and on a matched package the two are close.
        Deliberately the DISCHARGE bound rather than the charge one, because that is the direction
        the inverter's AC rating actually limits.
        """
        return float(self.ranges_for(sn).get(HANCHU_KEY_DISCHARGE_POWER, (0, 0))[1])

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _sensor_name(self, sn, leaf):
        """Return a namespaced Hanchu sensor entity id."""
        return "sensor.{}_hanchu_{}_{}".format(self.prefix, str(sn).lower(), leaf)

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced Hanchu control entity id."""
        return "{}.{}_hanchu_{}_{}".format(domain, self.prefix, str(sn).lower(), leaf)

    def publish_device_slots(self, sn):
        """Publish the charge/discharge slot table exactly as the device reports it.

        Diagnostic only - never mapped to a Predbat arg, and never fed back into local_schedule.
        The slots carry seconds since midnight on the wire, which is unreadable in a dashboard, so
        each is published as HH:MM:SS with the raw value alongside. A slot the device has not
        reported is skipped rather than published as zero, which would claim it is disabled when
        the truth is that it was not read.
        """
        settings = self.device_settings.get(sn) or {}
        for direction, start_key, end_key in (("charge", HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END), ("export", HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END)):
            for index in range(1, HANCHU_SLOT_COUNT + 1):
                for edge, key in (("start", start_key.format(index)), ("end", end_key.format(index))):
                    raw = settings.get(key)
                    if raw is None:
                        continue
                    self.dashboard_item(
                        self._sensor_name(sn, "device_{}_slot_{}_{}".format(direction, index, edge)),
                        state=seconds_to_hhmmss(raw),
                        attributes={"friendly_name": "Hanchu {} Device {} Slot {} {}".format(sn, direction.title(), index, edge.title()), "raw": raw, "control_key": key, "icon": "mdi:clock-check-outline"},
                        app="hanchu",
                    )

    async def publish_data(self):
        """Publish monitoring sensors for each device."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "ac_pv_power": "W"}
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(
                        self._sensor_name(sn, leaf),
                        state=values[leaf],
                        attributes={"unit_of_measurement": unit, "friendly_name": "Hanchu {} {}".format(sn, leaf.replace("_", " ").title())},
                        app="hanchu",
                    )

            # Ratings are published only when actually derivable - an arg pointing at a sensor that
            # never appears is worse than an absent arg the user can fill in.
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=round(capacity, 3), attributes={"unit_of_measurement": "kWh", "friendly_name": "Hanchu {} Battery Capacity".format(sn)}, app="hanchu")
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": "Hanchu {} Battery Rate Max".format(sn)}, app="hanchu")
            limit = self.inverter_limit(sn)
            if limit > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=round(limit), attributes={"unit_of_measurement": "W", "friendly_name": "Hanchu {} Inverter Limit".format(sn)}, app="hanchu")

            # Diagnostic only. The work mode is what the schedule's correctness depends on most -
            # see HANCHU_WORK_MODE_FOR_SCHEDULE - so a tester needs to be able to see it.
            mode = self.device_settings.get(sn, {}).get(HANCHU_KEY_WORK_MODE)
            if mode is not None:
                self.dashboard_item(
                    self._sensor_name(sn, "work_mode"),
                    state=HANCHU_WORK_MODES.get(int(self._as_float(mode, 0)), str(mode)),
                    attributes={"friendly_name": "Hanchu {} Work Mode".format(sn), "raw": mode},
                    app="hanchu",
                )
            station = self.device_detail.get(sn, {}).get("station_id")
            if station:
                self.dashboard_item(self._sensor_name(sn, "station_id"), state=station, attributes={"friendly_name": "Hanchu {} Station".format(sn)}, app="hanchu")

            # The slot table as the DEVICE reports it, alongside what Predbat asked for. This is
            # the single most useful diagnostic on an undocumented API: it is what distinguishes
            # "the window was never written" from "the window was written and the inverter ignored
            # it", which is exactly the failure mode the inferred work mode would produce. Read
            # from the config tier's own iotGet, so it costs no extra call.
            self.publish_device_slots(sn)

            # Daily energy counters feed Predbat's load/import/export learning. They reset at
            # midnight; minute_data/clean_incrementing_reverse absorbs that.
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "Hanchu {} {}".format(sn, leaf.replace("_", " ").title())},
                    app="hanchu",
                )

    async def automatic_config(self):
        """Register every discovered device as a HanchuCloud Predbat inverter."""
        devices = list(self.device_list)
        if not devices:
            self.log("Warn: Hanchu automatic_config found no devices")
            return
        self.set_arg_auto("inverter_type", ["HanchuCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))
        self.set_arg_auto("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg_auto("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg_auto("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        self.set_arg_auto("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])

        # Own the sign flags rather than leaving them to whatever else configured this install.
        # base.args is shared and NOT namespaced per inverter type, so a component that
        # legitimately inverts its own grid sensor leaves that key set for every inverter index,
        # and a Hanchu inverter that never claims it inherits the flip - the published sensor would
        # then be correct and inverter.py would negate it again, so an export would read as an
        # import. All three are False because _apply_status_payload already emits Predbat's
        # conventions.
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])

        # Only map an arg when EVERY device reports the underlying value.
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in devices):
                self.set_arg_auto(leaf, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log("Warn: Hanchu not every device reports {}, it must be set manually in apps.yaml".format(leaf))

        if all(self.battery_capacity(sn) > 0 for sn in devices):
            self.set_arg_auto("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: Hanchu no battery capacity reported for every device, soc_max must be set manually in apps.yaml")
        if all(self.battery_rate_max(sn) > 0 for sn in devices):
            self.set_arg_auto("battery_rate_max", [self._sensor_name(sn, "battery_rate_max") for sn in devices])
        else:
            self.log("Warn: Hanchu no battery power limit available, battery_rate_max must be set manually in apps.yaml")
        if all(self.inverter_limit(sn) > 0 for sn in devices):
            self.set_arg_auto("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: Hanchu no inverter power limit available, inverter_limit must be set manually in apps.yaml")

        # Deliberately NOT auto-mapped. The discharge power bound is the inverter's own limit, not
        # the site's grid connection cap, and a G98/G99-capped site can sit well below it. Nothing
        # measures and reports this error back, so a guess would over-export silently.
        self.log("Warn: Hanchu does not report an export power limit; set export_limit in apps.yaml if your grid connection is capped below the inverter rating, otherwise Predbat will plan exports it cannot deliver")
        # battery_min_soc is deliberately NOT mapped: the discharge floor key is a field Predbat
        # writes, so reading it back as the floor would be circular.

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

    # ------------------------------------------------------------------
    # Discovery catalogue
    # ------------------------------------------------------------------

    def _discovery_entities(self, sn):
        """The settings automatic_config() binds for one device, as discovery entity descriptors.

        Each entity id is formed with the same _sensor_name()/_control_name() call
        automatic_config() makes, so the two cannot drift apart. The record describes THIS device
        alone: a setting automatic_config() binds only when every device reports a figure is still
        described whenever this one does. pv_power and pv_today are described even under
        automatic_ignore_pv, which is the user's opt-out rather than a fact about the hardware. The
        three *_power_invert settings are always False, which is the descriptor's default, so none
        carries invert. export_limit and battery_min_soc are never bound.
        """
        entities = {
            "soc_percent": {"entity_id": self._sensor_name(sn, "soc"), "access": "r", "unit": "%"},
            "battery_power": {"entity_id": self._sensor_name(sn, "battery_power"), "access": "r", "unit": "W"},
            "grid_power": {"entity_id": self._sensor_name(sn, "grid_power"), "access": "r", "unit": "W"},
            "load_power": {"entity_id": self._sensor_name(sn, "load_power"), "access": "r", "unit": "W"},
            "pv_power": {"entity_id": self._sensor_name(sn, "pv_power"), "access": "r", "unit": "W"},
        }
        energy = self.device_energy.get(sn, {})
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf in energy:
                entities[leaf] = {"entity_id": self._sensor_name(sn, leaf), "access": "r", "unit": "kWh"}
        if self.battery_capacity(sn) > 0:
            entities["soc_max"] = {"entity_id": self._sensor_name(sn, "battery_capacity"), "access": "r", "unit": "kWh"}
        if self.battery_rate_max(sn) > 0:
            entities["battery_rate_max"] = {"entity_id": self._sensor_name(sn, "battery_rate_max"), "access": "r", "unit": "W"}
        if self.inverter_limit(sn) > 0:
            entities["inverter_limit"] = {"entity_id": self._sensor_name(sn, "inverter_limit"), "access": "r", "unit": "W"}

        ranges = self.ranges_for(sn)
        charge_low, charge_high = ranges.get(HANCHU_KEY_CHARGE_SOC, (0, 100))
        floor_low, floor_high = ranges.get(HANCHU_KEY_DISCHARGE_SOC, (0, 100))
        entities["reserve"] = {"entity_id": self._control_name("number", sn, "battery_schedule_reserve"), "access": "rw", "unit": "%", "min": floor_low, "max": floor_high}
        for prefix, direction in (("charge", "charge"), ("discharge", "export")):
            entities["{}_start_time".format(prefix)] = {"entity_id": self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["{}_end_time".format(prefix)] = {"entity_id": self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["scheduled_{}_enable".format(prefix)] = {"entity_id": self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), "access": "rw", "domain": "switch"}
        entities["charge_limit"] = {"entity_id": self._control_name("number", sn, "battery_schedule_charge_soc"), "access": "rw", "unit": "%", "min": charge_low, "max": charge_high}
        entities["charge_rate"] = {"entity_id": self._control_name("number", sn, "battery_schedule_charge_power"), "access": "rw", "unit": "W", "step": HANCHU_POWER_STEP}
        entities["discharge_target_soc"] = {"entity_id": self._control_name("number", sn, "battery_schedule_export_soc"), "access": "rw", "unit": "%", "min": floor_low, "max": floor_high}
        entities["discharge_rate"] = {"entity_id": self._control_name("number", sn, "battery_schedule_export_power"), "access": "rw", "unit": "W", "step": HANCHU_POWER_STEP}
        entities["schedule_write_button"] = {"entity_id": self._control_name("switch", sn, "battery_schedule_charge_write"), "access": "rw", "domain": "switch"}
        return entities

    def build_discovery(self):
        """Describe the discovered Hanchu devices for the discovery catalogue.

        Reads only what the poll tiers already hold, so this adds no API calls and cannot change
        what the device does. Reporting is independent of self.automatic: the catalogue records the
        hardware, and the report's own "automatic" flag says whether Predbat wired apps.yaml to it.

        Deliberately NOT reported:
        - export_limit: the API reports no export power limit, and the discharge power bound is the
          inverter rating rather than the grid connection's cap (automatic_config() warns).
        - a model name or firmware version: the plain-JSON surface carries neither. The device list
          entry may well have more fields, but no reference integration reads any of them, so
          inventing names for them would be a guess.
        - the work mode: it changes, and refresh_discovery() compares whole reports, so a changing
          value would re-file the report every time it moved.
        - an AC-coupled verdict: there is no signal for it. bypMeterTotalPower is AC-coupled PV,
          but its presence says a bypass meter exists, not that the inverter is AC-coupled.

        "solar" is claimed only once PV has actually been observed, which is why it keys off a
        reported pv_power rather than off a nameplate the API never gives.

        Returns None when nothing has been discovered yet, which refresh_discovery() treats as
        "nothing to report, ask again next cycle".
        """
        if not self.device_list:
            return None
        inverters = []
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            detail = self.device_detail.get(sn, {})
            functions = ["solar", "battery"] if "pv_power" in values else ["battery"]

            ratings = {}
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                ratings["battery_rate_max"] = rate_max
            limit = self.inverter_limit(sn)
            if limit > 0:
                ratings["inverter_limit"] = limit
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                ratings["soc_max"] = capacity

            account_ids = {"station": detail["station_id"]} if detail.get("station_id") else {}

            inverters.append(
                inverter_record(
                    "hanchu:{}".format(sn),
                    inverter_type="HanchuCloud",
                    composition="direct",
                    functions=functions,
                    capabilities=dict(HANCHU_CAPABILITIES),
                    hardware_ids={"serial": sn},
                    account_ids=account_ids,
                    ratings=ratings,
                    entities=self._discovery_entities(sn),
                )
            )
        return {"automatic": self.automatic, "inverters": inverters}

    # ------------------------------------------------------------------
    # Control entities
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_schedule():
        """Return a fresh, disabled schedule shape - the single source of truth for its defaults.

        Used where a schedule has to be seeded from nothing: a control event arriving for a device
        local_schedule has not seen yet. Kept as one helper rather than a literal repeated at each
        call site, so adding or renaming a field cannot silently diverge between copies.
        """
        return {
            "reserve": 0,
            "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        }

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one device."""
        local = self.local_schedule.get(sn, {})
        ranges = self.ranges_for(sn)
        charge_low, charge_high = ranges.get(HANCHU_KEY_CHARGE_SOC, (0, 100))
        floor_low, floor_high = ranges.get(HANCHU_KEY_DISCHARGE_SOC, (0, 100))
        power_high = int(ranges.get(HANCHU_KEY_CHARGE_POWER, (0, 20000))[1]) or 20000

        # Deliberately NOT clamped to the device's range on publish. This entity is Predbat's
        # control surface: it writes a value then reads it back to confirm (write_and_poll_value),
        # so publishing anything other than what was written guarantees a mismatch and a retry
        # storm. The min/max attributes advertise the range; the clamp itself happens at the API
        # boundary in build_write_payload().
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": floor_low, "max": floor_high, "step": 1, "unit_of_measurement": "%", "friendly_name": "Hanchu {} Battery Schedule Reserve".format(sn), "icon": "mdi:gauge"},
            app="hanchu",
        )
        for direction, soc_bounds in (("charge", (charge_low, charge_high)), ("export", (floor_low, floor_high))):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes inverter.py
            # replace these entities with its own dummies and the window never reaches here.
            self.dashboard_item(
                self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": "Hanchu {} {} Start".format(sn, direction.title()), "icon": "mdi:clock-outline"},
                app="hanchu",
            )
            self.dashboard_item(
                self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": "Hanchu {} {} End".format(sn, direction.title()), "icon": "mdi:clock-outline"},
                app="hanchu",
            )
            self.dashboard_item(
                self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)),
                state=int(window.get("soc", 0)),
                attributes={"min": soc_bounds[0], "max": soc_bounds[1], "step": 1, "unit_of_measurement": "%", "friendly_name": "Hanchu {} {} SoC".format(sn, direction.title()), "icon": "mdi:gauge"},
                app="hanchu",
            )
            self.dashboard_item(
                self._control_name("number", sn, "battery_schedule_{}_power".format(direction)),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": power_high, "step": HANCHU_POWER_STEP, "unit_of_measurement": "W", "friendly_name": "Hanchu {} {} Power".format(sn, direction.title()), "icon": "mdi:flash"},
                app="hanchu",
            )
            self.dashboard_item(
                self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": "Hanchu {} {} Enable".format(sn, direction.title()), "icon": "mdi:check-circle-outline"},
                app="hanchu",
            )
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": "Hanchu {} Schedule Write".format(sn), "icon": "mdi:content-save"}, app="hanchu")

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape the payload builder consumes.

        Numeric casts route through _as_float so an entity legitimately reporting
        "unknown"/"unavailable" - right after a Home Assistant restart, before Predbat republishes -
        falls back to 0 rather than raising and killing the control loop.
        """
        schedule = {"reserve": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), default="00:00:00"),
                "soc": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)), default=0), 0)),
                "power": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_{}_power".format(direction)), default=0), 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule

    def _sn_from_entity(self, entity_id):
        """Extract the serial from a Hanchu entity id, or None if unresolvable.

        Entity ids are always {domain}.{prefix}_hanchu_{sn}_{leaf}, so the serial is always
        followed by "_". Matching sn + "_" rather than a bare prefix keeps prefix-colliding serials
        apart - an entity for HC701 must never route to HC70, which would send a control write to
        the wrong inverter.
        """
        text = str(entity_id).lower()
        for sn in self.device_list:
            if "_hanchu_{}_".format(str(sn).lower()) in text:
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

    def update_local_schedule(self, sn, entity_id, value):
        """Apply one control-entity change to the locally held schedule."""
        schedule = self.local_schedule.setdefault(sn, self._empty_schedule())
        leaf = str(entity_id).split("_hanchu_{}_".format(str(sn).lower()), 1)[-1]
        if leaf == "battery_schedule_reserve":
            schedule["reserve"] = int(self._as_float(value, 0))
            return
        for direction in ("charge", "export"):
            prefix = "battery_schedule_{}_".format(direction)
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

    async def select_event(self, entity_id, value):
        """Handle a select entity change."""
        await self._handle_control_event(entity_id, value)

    async def number_event(self, entity_id, value):
        """Handle a number entity value change."""
        await self._handle_control_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle a switch entity service call."""
        await self._handle_control_event(entity_id, service)

    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right device and apply it."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log("Warn: Hanchu could not resolve a device for {}".format(entity_id))
            return
        # The write button is NOT forced. Predbat presses this on every cycle as its normal "apply
        # the schedule" action (INVERTER_DEF time_button_press), not only when the plan actually
        # changed, so force=True here would bypass the change-detection gate on every single cycle
        # and turn one press into one write. DEYE hit exactly this: 40 presses produced 36
        # byte-identical control orders over two hours on a live site. Do not reintroduce it.
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                # Predbat is now actively driving this device, so the reconcile loop may re-apply
                # from here on. Marked on the press itself rather than on a successful write: a
                # failed write still means Predbat owns this inverter and the next tick should retry.
                self.control_active.add(sn)
                await self.apply_schedule(sn)
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

    # ------------------------------------------------------------------
    # Payload building
    # ------------------------------------------------------------------

    def _clamp(self, sn, key, value, ranges=None):
        """Clamp one control value into its key's range, logging the first time it has to move.

        Logged once per (device, key) rather than every cycle: the SoC limits are narrower than
        Predbat's, so a perfectly ordinary plan can sit outside them for hours, and repeating the
        message every tick would bury everything else. Silently sending an out-of-range value is
        not an option - it would either be rejected or ignored, and both look exactly like a window
        that was written and never ran.
        """
        bounded, clamped = clamp_range(key, value, ranges or self.ranges_for(sn))
        if clamped and (sn, key) not in self._clamp_logged:
            self._clamp_logged.add((sn, key))
            low, high = (ranges or self.ranges_for(sn)).get(key, (bounded, bounded))
            self.log("Warn: Hanchu {} cannot be set to {} on {} - this hardware only accepts {}-{}, so {} was sent instead. Predbat's plan will be limited accordingly.".format(key, value, sn, low, high, bounded))
        return bounded

    def _window_active_now(self, window):
        """Return whether a stored window covers Predbat's current minute.

        The discharge floor key is a single device-wide value, not a per-window one, so it has to
        carry the export target while an export window is running and the reserve at every other
        moment. That makes the payload time-aware, which is why the reconcile loop exists and why
        it has to respect read-only mode.
        """
        if not window.get("enable"):
            return False
        start = hhmmss_to_seconds(window.get("start", "00:00:00")) // 60
        end_text = str(window.get("end", "00:00:00"))
        parts = end_text.split(":")
        try:
            end = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
        except (TypeError, ValueError):
            return False
        if end <= start:
            return False
        return start <= self.minutes_now < end

    def _slot_pairs(self, sn, direction, window):
        """Return the up-to-three (start, end) second pairs for one direction's slot table.

        Predbat drives one window per direction; this device has three slots per direction. Slot 1
        carries the window, slot 2 carries whatever runs past midnight (INVERTER_DEF sets
        can_span_midnight False so Predbat does not ask for a wrap, but it still spells "until
        midnight" as an end hour of 24), and slot 3 is always spare - written as disabled, so a leftover app slot cannot run. A window the split has
        collapsed to nothing is written as disabled rather than as an undocumented wrap-around,
        and that decision is logged - a silently ignored window looks written and never runs.
        """
        disabled = (HANCHU_SLOT_DISABLED, HANCHU_SLOT_DISABLED)
        if not window.get("enable"):
            return [disabled] * HANCHU_SLOT_COUNT
        first, second = split_window_seconds(window.get("start", "00:00:00"), window.get("end", "00:00:00"))
        pairs = []
        for index, pair in enumerate((first, second), start=1):
            if pair == disabled:
                pairs.append(disabled)
                continue
            if window_is_empty(pair[0], pair[1]):
                if index == 1:
                    self.log("Info: Hanchu {} {} window {}-{} carries no usable time, so it is written as disabled rather than as an undocumented wrap-around".format(sn, direction, window.get("start"), window.get("end")))
                pairs.append(disabled)
                continue
            pairs.append(pair)
        while len(pairs) < HANCHU_SLOT_COUNT:
            pairs.append(disabled)
        return pairs

    def build_write_payload(self, sn, schedule):
        """Build the single batched iotSet value dict for one device.

        ONE dict, sent in ONE call. The reference integration stages changes and flushes them in a
        single call specifically to avoid rate limiting, and Predbat touches up to nineteen
        settings per cycle, so anything per-setting would be nineteen calls every few minutes
        against an API with no documented budget.

        A COMPLETE control state, not a patch, for the keys Predbat owns: the two power limits, the
        charge target, the device-wide discharge floor, the work mode (unless the user has turned
        that off) and both slot tables. Every value is clamped to the range this device reports.

        Every one of the three charge and three discharge slots is written, and any the plan does
        not use is set to 00:00-00:00 - see HANCHU_SLOT_DISABLED. User-defined mode runs all six,
        so a slot left over from the Hanchu app would otherwise fight the plan.
        """
        ranges = self.ranges_for(sn)
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})
        payload = {}

        # Power. Rate 0 is how Predbat expresses freeze, and 0 is inside the accepted range, so it
        # survives the clamp and is passed straight through.
        payload[HANCHU_KEY_CHARGE_POWER] = self._clamp(sn, HANCHU_KEY_CHARGE_POWER, charge.get("power", 0), ranges)
        payload[HANCHU_KEY_DISCHARGE_POWER] = self._clamp(sn, HANCHU_KEY_DISCHARGE_POWER, export.get("power", 0), ranges)

        # The charge target. Clamped to 50-100 on this hardware, so a plan asking for less cannot
        # be expressed - _clamp says so once per device.
        payload[HANCHU_KEY_CHARGE_SOC] = self._clamp(sn, HANCHU_KEY_CHARGE_SOC, charge.get("soc", 0), ranges)

        # The discharge floor is device-wide, so it carries the export target while an export
        # window is actually running and the reserve at every other moment - the same shape every
        # other cloud adapter with a single floor register uses.
        floor = export.get("soc", 0) if self._window_active_now(export) else schedule.get("reserve", 0)
        payload[HANCHU_KEY_DISCHARGE_SOC] = self._clamp(sn, HANCHU_KEY_DISCHARGE_SOC, floor, ranges)

        for direction, window, start_key, end_key in (
            ("charge", charge, HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END),
            ("export", export, HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END),
        ):
            for index, (start, end) in enumerate(self._slot_pairs(sn, direction, window), start=1):
                payload[start_key.format(index)] = start
                payload[end_key.format(index)] = end

        # The work mode is only asserted when there is actually a window to run, and only if the
        # user has not opted out. It is the least well evidenced thing in this component (see
        # HANCHU_WORK_MODE_FOR_SCHEDULE), so it is never written speculatively - with no window
        # enabled the mode is the user's business.
        if self.work_mode_control and (charge.get("enable") or export.get("enable")):
            payload[HANCHU_KEY_WORK_MODE] = HANCHU_WORK_MODE_FOR_SCHEDULE
        return payload

    @staticmethod
    def payloads_equal(first, second):
        """Return True when two payloads are identical, ignoring key order."""
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        return json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)

    @staticmethod
    def rejected_keys(data):
        """Return the keys one iotSet response says the device refused.

        One reference integration reads a per-key acknowledgement map in which "1" means accepted;
        the other two ignore it entirely. Single-source, so its ABSENCE is never treated as a
        failure - only a present map naming a key with anything other than "1" is.
        """
        if not isinstance(data, dict):
            return []
        ack = data.get(HANCHU_ACK_MAP)
        if not isinstance(ack, dict):
            return []
        return sorted(key for key, value in ack.items() if str(value) != HANCHU_ACK_OK)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the inverter."""
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    def _write_allowed(self, sn, force=False):
        """Return True when a write for one device may go out now.

        A change arriving inside the pacing window is HELD, not dropped: _write_payload leaves the
        applied-payload cache untouched, so the next eligible tick rebuilds and sends it.
        """
        if force or not self.min_write_interval:
            return True
        last = self.last_write_time.get(sn)
        if last is None:
            return True
        return (time.time() - last) >= self.min_write_interval

    async def _write_payload(self, sn, payload, force=False):
        """Serialise every settings write through one lock, then send it.

        The equality and pacing checks run INSIDE the lock, so a caller that waited sees the write
        the one before it just made and does not resend the same payload.
        """
        async with self.write_lock:
            return await self._write_payload_locked(sn, payload, force=force)

    async def _write_payload_locked(self, sn, payload, force=False):
        """Send one batched payload if it differs from the last accepted one and pacing allows.

        Returns whether the device is now KNOWN TO MATCH this payload, not merely whether an
        exception was avoided. True covers "sent and accepted" and "already matched, so nothing
        needed sending" alike. False covers both a HELD change - a real difference exists, but the
        pacing gate is deliberately not sending it yet - and an outright rejection; a caller must
        not read either as the device matching the plan.
        """
        if not payload:
            return True
        if not force and self.payloads_equal(self.applied_payload.get(sn), payload):
            self.log("Info: Hanchu {} settings unchanged, nothing sent".format(sn))
            return True
        if not self._write_allowed(sn, force=force):
            self.log("Info: Hanchu {} change is held by hanchu_min_write_interval ({}s) and will be applied on the next eligible cycle".format(sn, self.min_write_interval))
            return False

        body = {"sn": sn, "devType": HANCHU_DEV_TYPE_DTU, "value": payload}
        ok, data = await self._post("iot_set", body=body)
        if not ok:
            # One retry after a short pause, matching the reference integration's own flush
            # behaviour, because a single rejected batch strands every setting in it.
            self.log("Warn: Hanchu {} settings write failed ({}), retrying once in {}s".format(sn, self.last_api_error or "no reason given", self.write_retry_delay))
            if self.write_retry_delay:
                await asyncio.sleep(self.write_retry_delay)
            ok, data = await self._post("iot_set", body=body)

        # Stamp every attempt, success or not. A persistently rejected write must still be paced,
        # or the reconcile loop re-posts it on every tick forever. The payload is deliberately NOT
        # cached as applied on any failure path, so it keeps retrying - just paced, not hammering.
        now = time.time()
        self.last_write_time[sn] = now
        if not ok:
            self.log("Warn: Hanchu {} settings write was rejected after a retry: {}".format(sn, self.last_api_error or "no reason given"))
            # Saved inline so a container kill right after a write does not lose the pacing
            # timestamp just stamped above and reopen the retry storm this guards against.
            await self.save_control()
            return False

        rejected = self.rejected_keys(data)
        if rejected:
            # A partial accept is NOT cached as applied: the next cycle must rebuild and resend,
            # or the rejected keys would be assumed to be in force forever.
            self.log("Warn: Hanchu {} accepted the write but refused {}; it will be rebuilt and resent on the next eligible cycle".format(sn, ", ".join(rejected)))
            await self.save_control()
            return False

        self.applied_payload[sn] = dict(payload)
        self.log("Info: Hanchu wrote {} setting(s) for {} in one call".format(len(payload), sn))
        await self.save_control()
        return True

    async def apply_schedule(self, sn, force=False):
        """Build and send the locally held schedule for one device."""
        if not self.control_enable:
            return False
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        return await self._write_payload(sn, self.build_write_payload(sn, schedule), force=force)

    async def _reconcile_control(self, sn):
        """Re-apply sn's schedule if Predbat already controls it, unforced.

        Gated on read-only for a specific reason. Predbat's own read-only handling covers every
        write that originates from a plan, but NOT one this component initiates itself - and this
        is exactly that. The payload is time-aware because the device-wide discharge floor switches
        between the export target and the reserve, so a window transition changes it with no plan
        change at all, and without this gate that transition would write to the inverter while
        Predbat was in read-only mode.
        """
        if sn not in self.control_active or self._is_read_only() or not self.control_enable:
            return
        try:
            await self.apply_schedule(sn)
        except Exception as error:
            self.log("Warn: Hanchu schedule apply failed for {}: {}".format(sn, error))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def load_cache(self, name):
        """Load one cache file, returning {} when absent or unreadable.

        self.storage being None is checked FIRST and returns silently: it means no Storage
        component is configured, which is a permanent, by-design condition rather than a transient
        fault worth warning about. Only a real failure below flags the restore as incomplete.
        """
        if self.storage is None:
            return {}
        try:
            data = await self.storage.load(HANCHU_STORAGE_MODULE, name)
        except Exception as error:
            self.log("Warn: Hanchu could not load cache {}: {}".format(name, error))
            self._restore_had_error = True
            return {}
        return data if isinstance(data, dict) else {}

    async def save_cache(self, name, data):
        """Save one cache file, tolerating a storage failure."""
        if self.storage is None:
            return
        try:
            await self.storage.save(HANCHU_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log("Warn: Hanchu could not save cache {}: {}".format(name, error))

    async def save_static(self):
        """Persist discovery. Refuses to overwrite a good cache with an empty result.

        Writing an empty device list and stamping the tier fresh would make a restart restore
        nothing and skip re-discovery for a full TTL. Absence of a result is not a result.
        """
        if not self.device_list:
            return
        await self.save_cache(HANCHU_CACHE_STATIC, {"device_list": self.device_list, "device_detail": self.device_detail})

    async def save_config(self):
        """Persist the per-device parameter ranges and the control values last read back."""
        await self.save_cache(HANCHU_CACHE_CONFIG, {"device_ranges": {sn: list(ranges.items()) for sn, ranges in self.device_ranges.items()}, "device_settings": self.device_settings})

    async def save_control(self):
        """Persist the control state that must survive a restart.

        The applied payload matters most: without it a restart re-sends a payload the device
        already holds, and - worse - forgets which slots this component has written, so the rule
        that only our own slots may be zeroed would be lost. The write timestamp is persisted for
        the same reason, so a restart loop cannot bypass the pacing interval entirely.
        """
        await self.save_cache(
            HANCHU_CACHE_CONTROL,
            {
                "local_schedule": self.local_schedule,
                "applied_payload": self.applied_payload,
                "control_active": sorted(self.control_active),
                "last_write_time": dict(self.last_write_time),
            },
        )

    async def restore_state(self):
        """Restore every cached value so a restart does not re-learn them from scratch.

        _cache_restored is set only when nothing failed, so a transient storage outage is retried
        on a later cycle rather than silently marked done with nothing restored.
        """
        self._restore_had_error = False
        static = await self.load_cache(HANCHU_CACHE_STATIC)
        if static.get("device_list"):
            self.device_list = list(static["device_list"])
            self.device_detail = dict(static.get("device_detail") or {})
        config = await self.load_cache(HANCHU_CACHE_CONFIG)
        # Ranges round-trip as lists of pairs because JSON cannot carry a tuple; rebuilt as tuples
        # so clamp_range sees exactly what the live path gives it.
        self.device_ranges = {sn: {key: tuple(bounds) for key, bounds in (pairs or [])} for sn, pairs in (config.get("device_ranges") or {}).items()}
        self.device_settings = dict(config.get("device_settings") or {})
        control = await self.load_cache(HANCHU_CACHE_CONTROL)
        self.local_schedule = dict(control.get("local_schedule") or {})
        self.applied_payload = dict(control.get("applied_payload") or {})
        self.control_active = set(control.get("control_active") or [])
        self.last_write_time = dict(control.get("last_write_time") or {})
        self._cache_restored = not self._restore_had_error

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def health_message(self):
        """Return a short reason this component is unhealthy, or None when it has nothing to add."""
        if not self.account or not self.password:
            return "Hanchu needs hanchu_account and hanchu_password"
        if not self._token:
            return "Hanchu is not logged in{}".format(": {}".format(self.last_api_error) if self.last_api_error else "")
        if not self.device_list:
            return "Hanchu found no devices on this account"
        return None

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any schedule change.

        Returns True on a completed cycle, False on a failure that should hold the component in
        ComponentBase's startup backoff and be retried. Deliberately explicit rather than falling
        through to Python's implicit None: ComponentBase only clears its `first` flag when this
        returns something truthy, so an accidental None would strand the component in the
        ever-growing startup backoff forever even though every later cycle was working.
        """
        if first and not self._cache_restored:
            # Guarded on _cache_restored, not just `first`: a deferred startup returns False below
            # and leaves ComponentBase's `first` flag set, so run() is retried with first still
            # True. Without this guard a SUCCESSFUL restore would re-run on every retry, redoing
            # storage reads for nothing and re-overwriting anything progressed in memory since.
            await self.restore_state()

        if not self.account or not self.password:
            self.log("Warn: Hanchu needs both hanchu_account and hanchu_password - the e-mail address and password you sign into the Hanchu app with")
            return False
        if not await self.ensure_token():
            return False

        if self.tier_expired("static", HANCHU_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: Hanchu found no devices on this account")
            return False
        if self.tier_expired("config", HANCHU_TTL_CONFIG):
            await self.refresh_config()
        live_ok = True
        if self.tier_expired("power", HANCHU_TTL_POWER):
            live_ok = await self.refresh_power()
        if self.tier_expired("energy", HANCHU_TTL_ENERGY):
            await self.refresh_energy()

        for sn in list(self.device_list):
            # Read the control entities EVERY tick, the first one included. Home Assistant retains
            # them across a Predbat restart, so on a restart they already hold the live plan;
            # seeding local_schedule from _empty_schedule() and publishing that back would
            # overwrite it and cancel an in-flight charge until Predbat next replanned.
            # get_schedule_settings_ha falls back to the disabled default per field, so a genuinely
            # cold start still lands on exactly _empty_schedule()'s shape.
            try:
                await self.get_schedule_settings_ha(sn)
            except Exception as error:
                self.log("Warn: Hanchu schedule read failed for {}: {}".format(sn, error))
            await self._reconcile_control(sn)
            # Published every tick, not just the first: this is Predbat's control surface and must
            # keep reflecting local_schedule as it changes.
            await self.publish_schedule_settings_ha(sn)

        await self.publish_data()

        # Filed right after this cycle's publish and BEFORE the deferred-startup return below,
        # which is the branch that fires on exactly the installs whose dump most needs to say what
        # hardware was found. refresh_discovery() owns the compare/retry/guard loop and never raises.
        self.refresh_discovery()

        if first and not live_ok:
            # Startup has not really succeeded without telemetry: automatic_config() runs on the
            # first cycle ALONE, so it would map only the args backed by cached values and
            # permanently skip soc_max and the energy args for the whole session. Returning False
            # leaves ComponentBase's `first` flag set, so the startup path is retried on backoff.
            self.log("Warn: Hanchu first telemetry poll returned nothing, deferring startup; it will be retried after a backoff")
            return False

        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state on shutdown so a restart resumes without re-polling."""
        await self.save_static()
        await self.save_config()
        await self.save_control()
