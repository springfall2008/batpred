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

TWO SOC LIMITS ARE NARROWER THAN PREDBAT'S. The charge target goes in the grid-charge limit,
which accepts 20-100, and the discharge floor key accepts 5-45. A plan asking to charge to 10% or
to export down to 60% cannot be expressed on this hardware at all. Those values are CLAMPED and
the clamp is logged, because sending an out-of-range value would either be rejected or silently
ignored, and both look identical to a window that never ran. The overall charge ceiling,
CHG_BAT_SOC_LMT, also caps solar charging, so it is never written.
"""

import asyncio
import json
import os
import statistics
import time
from datetime import datetime
import aiohttp
from component_base import ComponentBase
from coordinator import inverter_record
from hanchu_const import (
    HANCHU_ACK_MAP,
    HANCHU_ACK_OK,
    HANCHU_AC_COUPLED_FIELD,
    HANCHU_BASE_URL,
    HANCHU_CACHE_CONFIG,
    HANCHU_CACHE_CONTROL,
    HANCHU_CACHE_STATIC,
    HANCHU_CAPACITY_FIELD,
    HANCHU_CODE_FAILED,
    HANCHU_CODE_OK,
    HANCHU_CODE_REFRESH_REJECTED,
    HANCHU_CODE_UNAUTHORISED,
    HANCHU_CONTROL_KEYS,
    HANCHU_DEBUG_REDACT_KEYS,
    HANCHU_DEV_TYPE_DTU,
    HANCHU_DEVICE_STATUS_FIELD,
    HANCHU_DEVICE_STATUS_OFFLINE,
    HANCHU_DEVICE_STATUSES,
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
    HANCHU_KEY_GRID_CHARGE_SOC,
    HANCHU_KEY_WORK_MODE,
    HANCHU_LOOP_HANDOVER_POLL,
    HANCHU_LOOP_HANDOVER_TIMEOUT,
    HANCHU_MENU_ARRAY_SLOT_TYPES,
    HANCHU_MENU_CODE,
    HANCHU_MENU_DATA,
    HANCHU_MENU_ENABLE_CYCLE,
    HANCHU_MENU_ITEMS,
    HANCHU_MENU_LISTENER,
    HANCHU_MENU_LISTENER_SHOW,
    HANCHU_MENU_MAX,
    HANCHU_MENU_MIN,
    HANCHU_MENU_SECTION,
    HANCHU_MENU_SIGNAL,
    HANCHU_MENU_TYPE,
    HANCHU_MODEL_FIELD,
    HANCHU_MODELS,
    HANCHU_POWER_STEP,
    HANCHU_RANGES,
    HANCHU_READBACK_SETTLE,
    HANCHU_REFRESH_MIN_INTERVAL,
    HANCHU_RETRIES,
    HANCHU_SECONDS_PER_DAY,
    HANCHU_SLOT_COUNT,
    HANCHU_SLOT_DISABLED,
    HANCHU_SOC_SCALE,
    HANCHU_STATION_FIELD,
    HANCHU_STORAGE_MODULE,
    HANCHU_SUCCESS_BY_CODE_ENDPOINTS,
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
    HANCHU_FAILED_WRITE_INTERVAL,
    HANCHU_GENERATOR_FIELD,
    HANCHU_WRITE_FAILURE_NOTIFY,
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
# exposes. support_discharge_freeze is False because CHG_PWR_LMT only limits charging from the grid,
# so a zero charge rate cannot stop solar filling the battery (batpred#5665). charge_control_immediate is False because everything goes through the timed slot table -
# the one immediate primitive this API has (fastChargeDischarge) is duration-based and would lapse
# behind Predbat's back, so it is deliberately unused.
HANCHU_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": False,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}


class HanchuAPI(ComponentBase):
    """Hanchu ESS cloud component."""

    # Trace every request and response. On by default, because this API is undocumented and the trace is
    # what a customer's log needs to show when something goes wrong. It writes up to 2000 characters per
    # device on every poll (batpred#5672), so hanchu_api_debug: False quietens the log once things work.
    # The command-line harness traces too, unless --no-trace.
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
        inverter_limit=None,
        api_delay=1,
        min_write_interval=60,
        api_debug=True,
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
        self.inverter_limit_override = float(inverter_limit) if inverter_limit else 0.0
        self.api_debug = bool(api_debug)
        # Devices already warned that the menu gave no power bounds, so the warning is said once.
        self.warned_default_power = set()
        # Devices whose default-power warning waits on the first status poll - see _warn_if_default_power().
        self.default_power_deferred = set()
        # Consecutive rejected settings writes per serial, and whether the user has been told.
        self.write_failures = {}
        self.write_failure_notified = set()
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
        # The highest charge and discharge power limit each device has been seen holding, per serial
        # and key. See ranges_for() for why this, and not the value held now, raises the menu's ceiling.
        self.power_ceiling = {}
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
        # Per serial: whether the datalogger is online (devStatus), None while unknown or pending.
        self.device_online = {}
        # Per serial: which time-slot format the menu describes ("slots", "array" or "unknown"), and the
        # work modes its TCT_/TDT_ slots are shown in. Absent until a menu has been read.
        self.slot_model = {}
        self.slot_work_modes = {}
        self._slot_notes_logged = set()
        # One HTTP session for every call, opened on first use and closed by final(). Hanchu's cloud
        # takes 7-45s to answer; a session per request made timeouts and retries far more common.
        self._session = None
        # The event loop run() ticks on, which the session and write_lock belong to. Control events
        # can arrive on another loop entirely - see _on_component_loop().
        self._loop = None
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
                session = self._http_session(timeout)
                async with session.post(url, headers=self._headers(), json=body if body is not None else {}) as response:
                    status = response.status
                    payload = None
                    if status == 200:
                        payload = await response.json(content_type=None)
                    elif status == HANCHU_CODE_UNAUTHORISED:
                        # The status alone says the session has expired. The body may be empty or an
                        # HTML error page, and failing to parse it must not stop the re-auth below.
                        try:
                            payload = await response.json(content_type=None)
                        except ValueError:
                            payload = None
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
            code = payload.get("code") if isinstance(payload, dict) else None
            # An expired token shows up either as the HTTP status or as code 401 inside a 200. Checked
            # before the body's shape, because a 401 status needs no body to be acted on.
            if (status == HANCHU_CODE_UNAUTHORISED or code == HANCHU_CODE_UNAUTHORISED) and not anonymous and not auth_retried:
                auth_retried = True
                self.log("Info: Hanchu {} reported an expired session, re-authenticating".format(path))
                if await self.reauthenticate():
                    continue
                return False, None

            if not isinstance(payload, dict):
                self.log("Warn: Hanchu {} returned a non-object body".format(path))
                return False, None

            if payload.get("success") or (code == HANCHU_CODE_OK and endpoint_key in HANCHU_SUCCESS_BY_CODE_ENDPOINTS):
                return True, payload.get("data")
            message = payload.get("msg")
            self.last_api_error = str(message) if message is not None else ""
            if code == HANCHU_CODE_FAILED:
                self.log("Warn: Hanchu {} was rejected by the device: {}".format(path, self.last_api_error))
            else:
                self.log("Warn: Hanchu {} was unsuccessful (code {}): {}".format(path, code, self.last_api_error))
            return False, payload.get("data")

        return False, None

    def _http_session(self, timeout):
        """Return the open HTTP session, opening one if there is none.

        Reused only when it positively reports itself open (closed is False): a session that has been
        closed, or anything that is not a real session, is replaced rather than trusted.
        """
        if self._session is None or getattr(self._session, "closed", True) is not False:
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close_session(self):
        """Close the HTTP session if one is open. Safe to call more than once."""
        session, self._session = self._session, None
        if session is not None and getattr(session, "closed", True) is False:
            try:
                await session.close()
            except Exception as error:
                self.log("Warn: Hanchu could not close its HTTP session: {}".format(error))

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
            # The below-10-means-kW heuristic is only for a field with no unit sibling at all (loadPwr).
            # One that has a sibling but arrived without it is read as watts: small grid and battery
            # readings are routine, and an idle 3 W battery must not become 3000 W (batpred#5672).
            unit_field = HANCHU_TELEMETRY_UNIT_FIELD.get(leaf)
            if unit_field is None:
                watts = scale_to_watts(raw)
            else:
                watts = scale_to_watts(raw, payload.get(unit_field), threshold=0)
            if watts is None:
                continue
            values[leaf] = -watts if leaf in HANCHU_TELEMETRY_NEGATE else watts
        # AC-coupled solar is only in the bypass meter (pvTtPwr reads 0), so it is added in where the
        # unit says it is AC-coupled. The switch decides, not the reading: a bypass figure on a unit
        # that reports no bypass meter is not trusted. See HANCHU_AC_COUPLED_FIELD.
        if self._as_float(payload.get(HANCHU_AC_COUPLED_FIELD), 0) == 1 and "ac_pv_power" in values:
            values["pv_power"] = round(values.get("pv_power", 0.0) + values["ac_pv_power"], 1)
        self.device_values[sn] = values

        detail = self.device_detail.setdefault(sn, {})
        capacity = self._as_float(payload.get(HANCHU_CAPACITY_FIELD), 0.0)
        if capacity > 0:
            detail["capacity_kwh"] = capacity
        station = payload.get(HANCHU_STATION_FIELD)
        if station:
            detail["station_id"] = str(station)
        generator = payload.get(HANCHU_GENERATOR_FIELD)
        if generator is not None:
            detail["has_generator"] = str(generator).strip().lower() in ("true", "1")
        self._apply_model(sn, payload.get(HANCHU_MODEL_FIELD))
        self._apply_device_status(sn, payload.get(HANCHU_DEVICE_STATUS_FIELD))
        return True

    def _apply_model(self, sn, code):
        """Record a device's devModel and its name, saying once what the ratings table makes of it.

        Kept in device_detail, which reaches the static cache at the next static refresh (up to
        HANCHU_TTL_STATIC later), so a restart before then has no model until its first status poll -
        which runs in the same cycle, before anything is published. A code the table does not know is
        kept too: it says why the menu and held values are still in use, and it is what a user would
        report to have the model added.
        """
        if code is None or str(code).strip() == "":
            return
        number = int(self._as_float(code, -1))
        detail = self.device_detail.setdefault(sn, {})
        detail["model_code"] = number
        model = HANCHU_MODELS.get(number)
        if model:
            detail["model"] = model[0]
        else:
            detail.pop("model", None)
        if (sn, "model", number) in self._slot_notes_logged:
            return
        self._slot_notes_logged.add((sn, "model", number))
        if not model:
            self.log("Info: Hanchu {} reports devModel {}, which Predbat has no ratings for, so its power limits are taken from the settings menu and the values it holds - please report the model".format(sn, code))
            return
        name, battery, ac = model
        battery_text = "{} W".format(battery) if battery else "not known (taken from the settings menu)"
        self.log("Info: Hanchu {} is a {} (devModel {}): battery limit {}, AC rating {} W".format(sn, name, number, battery_text, ac))

    def model_rating(self, sn):
        """Return (battery limit W, AC rating W) for a device's model, either one None when not known. See HANCHU_MODELS."""
        model = HANCHU_MODELS.get(self.device_detail.get(sn, {}).get("model_code"))
        if not model:
            return None, None
        return model[1], model[2]

    def _apply_device_status(self, sn, status):
        """Record the datalogger's devStatus, saying so once when it goes offline and once when it returns.

        Offline means the cloud is serving the last readings it had and a write cannot reach the
        inverter. Pending (99) and a missing field are unknown, not offline.
        """
        if status is None:
            return
        code = int(self._as_float(status, -1))
        self.device_detail.setdefault(sn, {})["status"] = HANCHU_DEVICE_STATUSES.get(code, str(status))
        online = False if code == HANCHU_DEVICE_STATUS_OFFLINE else (True if code == 1 else None)
        previous = self.device_online.get(sn)
        if online is False and previous is not False:
            self.log("Warn: Hanchu {} is offline (devStatus {}) - its readings are stale and Predbat's settings cannot reach it until it reconnects".format(sn, code))
        elif online is not False and previous is False:
            self.log("Info: Hanchu {} is back online".format(sn))
        self.device_online[sn] = online

    async def refresh_power(self):
        """Poll live telemetry for every device. True when at least one reported a SoC."""
        got_any = False
        for sn in list(self.device_list):
            ok, data = await self._post("device_status", body={"sn": sn})
            if ok and self._apply_status_payload(sn, data):
                got_any = True
                if sn in self.default_power_deferred:
                    self._warn_if_default_power(sn, self.device_ranges.get(sn) or {})
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
        for item in self._menu_items(payload):
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

    @staticmethod
    def _menu_items(payload):
        """Yield every item dict in a menu response's energy section, flattening its groups.

        _post hands over the envelope's inner data, which holds the section directly. A whole
        envelope, still wrapped in its own data, is accepted too.
        """
        section = payload if isinstance(payload, dict) else {}
        if HANCHU_MENU_SECTION not in section and isinstance(section.get(HANCHU_MENU_DATA), dict):
            section = section[HANCHU_MENU_DATA]
        energy = section.get(HANCHU_MENU_SECTION)
        if not isinstance(energy, dict):
            return
        for group in energy.get(HANCHU_MENU_ITEMS) or []:
            for item in group if isinstance(group, list) else []:
                if isinstance(item, dict):
                    yield item

    def _parse_menu_slot_model(self, payload):
        """Work out which time-slot format a device's menu describes, returning (model, work_modes).

        "slots" when an item edits TCT_START_1 - the format this component writes - with the work modes
        its listener shows it in (empty when it names none). "array" when the menu has the other
        format's period items or FLAG_ENABLE_CYCLE and no TCT_ item. "unknown" otherwise: a menu that
        shows neither is not proof the device cannot run the slots, so it does not block control.
        """
        has_array = False
        for item in self._menu_items(payload):
            signal = str(item.get(HANCHU_MENU_SIGNAL) or item.get(HANCHU_MENU_CODE) or "")
            signals = [part.strip() for part in signal.split(",")]
            if HANCHU_KEY_CHARGE_START.format(1) in signals:
                listener = item.get(HANCHU_MENU_LISTENER) or {}
                show = str(listener.get(HANCHU_MENU_LISTENER_SHOW) or "") if isinstance(listener, dict) else ""
                modes = [int(part) for part in (piece.strip() for piece in show.split(",")) if part.isdigit()]
                return "slots", modes
            if str(item.get(HANCHU_MENU_TYPE)) in HANCHU_MENU_ARRAY_SLOT_TYPES or HANCHU_MENU_ENABLE_CYCLE in signals:
                has_array = True
        return ("array" if has_array else "unknown"), []

    def _apply_slot_model(self, sn, model, modes):
        """Record a device's slot format and slot work modes, saying once what was found when it matters."""
        self.slot_model[sn] = model
        self.slot_work_modes[sn] = list(modes)
        if model == "array" and (sn, "array") not in self._slot_notes_logged:
            self._slot_notes_logged.add((sn, "array"))
            self.log("Warn: Hanchu {} uses a different time-slot format (array-valued periods with FLAG_ENABLE_CYCLE), which Predbat cannot drive yet - it will be monitored but not controlled".format(sn))
        elif model == "unknown" and (sn, "unknown") not in self._slot_notes_logged:
            self._slot_notes_logged.add((sn, "unknown"))
            self.log("Info: Hanchu {}'s settings menu shows no time slots at all; Predbat will still write the TCT_/TDT_ slots - check them in the Hanchu app".format(sn))

    def slot_work_mode(self, sn):
        """Return the work mode to write with a window: User-defined, unless the menu runs the slots only in other modes.

        The menu's listener on the slot items names the work modes they are shown in. Where it names
        modes and User-defined is not among them, the first one it names is used instead, and that is
        logged once - it would be the first device seen to do so.
        """
        modes = self.slot_work_modes.get(sn) or []
        if not modes or HANCHU_WORK_MODE_FOR_SCHEDULE in modes:
            return HANCHU_WORK_MODE_FOR_SCHEDULE
        if (sn, "mode") not in self._slot_notes_logged:
            self._slot_notes_logged.add((sn, "mode"))
            self.log("Warn: Hanchu {}'s menu runs its time slots in work mode {} rather than User-defined ({}), so work mode {} is written with a window".format(sn, modes[0], HANCHU_WORK_MODE_FOR_SCHEDULE, modes[0]))
        return modes[0]

    def ranges_for(self, sn):
        """Return the parameter ranges to clamp against for one device.

        The menu's own bounds where it reported them, and the built-in defaults for everything
        else. Built per call rather than cached as a merged dict so a later menu poll cannot leave
        a stale entry behind.

        A power limit's ceiling is raised to the highest value the device has been seen holding, because
        a value it holds is one it accepts. The highest seen rather than the value held now: Predbat
        overwrites these keys itself, with 0 for a hold or a rate inside the menu's range, so the next
        read would otherwise drop the ceiling back to the menu's and the raise would undo itself
        (batpred#5672).

        Where the model is known (HANCHU_MODELS), the ceiling is then capped at its battery limit: a held
        setting above what the battery can do moves no more power. HARDWARE (batpred#5684): a
        HESS-HY-S-6.0K holds DSCHG_PWR_LMT 6000 against the menu's 0-5000 W, and still exports at its
        5000 W battery limit. So the held-value raise only matters for a unit rated above the menu's
        maximum, or one whose model is not known.

        The model's rating never raises a ceiling. It sets battery_rate_max and inverter_limit, but
        nothing yet shows the cloud accepts a power limit above the menu's maximum that the device has
        not itself been seen holding, and a refused key would turn every write into a partial accept
        that is resent each cycle. So a 10 kW unit whose menu says 0-5000 is written at most 5000 W
        until it is seen holding more (a limit set by an installer, say), and that gap is said once per
        key - only once a settings read has shown what the device holds.
        """
        merged = dict(HANCHU_RANGES)
        merged.update(self.device_ranges.get(sn) or {})
        settings = self.device_settings.get(sn) or {}
        ceiling = self.power_ceiling.get(sn) or {}
        battery_limit = self.model_rating(sn)[0] or 0
        for key in (HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER):
            held = int(max(self._as_float(settings.get(key), 0.0), self._as_float(ceiling.get(key), 0.0)))
            low, menu_high = merged[key]
            high = max(menu_high, held)
            if battery_limit:
                high = min(high, battery_limit)
            merged[key] = (low, high)
            if high > menu_high and (sn, "held", key) not in self._clamp_logged:
                self._clamp_logged.add((sn, "held", key))
                self.log("Info: Hanchu {} holds {} {} W, above the menu's {} W maximum (a range the menu shares across models), so {} W is used as its limit".format(sn, key, held, menu_high, high))
            if battery_limit > high and sn in self.device_settings and (sn, "rating", key) not in self._clamp_logged:
                self._clamp_logged.add((sn, "rating", key))
                self.log("Info: Hanchu {} is rated {} W but {} is only known to accept up to {} W, so Predbat writes no more than that. If the inverter is ever seen holding a higher limit, Predbat will use it".format(sn, battery_limit, key, high))
        return merged

    def _note_power_ceiling(self, sn, settings):
        """Remember the highest charge and discharge power limit a device has been seen holding. See ranges_for()."""
        ceiling = self.power_ceiling.setdefault(sn, {})
        for key in (HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER):
            held = int(self._as_float((settings or {}).get(key), 0.0))
            if held > int(self._as_float(ceiling.get(key), 0.0)):
                ceiling[key] = held

    def _note_external_change(self, sn, observed, read_started):
        """Forget the applied payload when the device no longer holds it, so the next tick sends it again.

        Change detection compares a new payload only with the last one Predbat sent, so without this a
        slot or work mode changed in the Hanchu app, a device reset, or a write the cloud acknowledged
        but never delivered would leave the inverter on the wrong settings until the plan itself
        changed - the silent drift of batpred#5157 (batpred#5672).

        Only keys the read-back carries are compared, as numbers, since iotGet returns "3" or 70.0
        where the write sent 3 or 70. A read that a write may have overtaken is not judged: one that
        began less than HANCHU_READBACK_SETTLE seconds after the last write, or before it.
        HARDWARE (batpred#5305): an accepted write reads back within 1-2 s.
        """
        applied = self.applied_payload.get(sn)
        if not applied or not isinstance(observed, dict):
            return
        last = self.last_write_time.get(sn)
        if last is not None and last > read_started - HANCHU_READBACK_SETTLE:
            return
        differing = settings_mismatches({key: value for key, value in applied.items() if observed.get(key) is not None}, observed)
        if not differing:
            return
        detail = ", ".join("{}={} (Predbat wrote {})".format(key, got, value) for key, value, got in differing)
        self.log("Warn: Hanchu {} settings no longer match what Predbat wrote: {}. The Hanchu app or a device reset may have changed them, so they will be sent again".format(sn, detail))
        self.applied_payload.pop(sn, None)

    def _warn_if_default_power(self, sn, ranges):
        """Warn once when no power bounds are held for a device, so the 5 kW default applies.

        Hanchu units run from 3 kW to 50 kW AC: the 5000 W default matches the battery limit of the
        single-phase 3.0-6.0K, but not their AC ratings or any larger unit. Called after every menu
        attempt, failed ones included - a failed menu leaves the default in force just as surely as
        one that answered without bounds. Said once per device per session.

        Not said for a model whose battery limit and AC rating are both in HANCHU_MODELS, since then
        neither comes from the menu. The config tier runs before the first status poll, which is what
        reports the model, so a device whose model has not been heard yet is not warned about on a
        guess: refresh_power() asks again straight after that poll.
        """
        missing = [key for key in (HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER) if key not in ranges]
        if not missing or sn in self.warned_default_power:
            return
        if "model_code" not in self.device_detail.get(sn, {}) and sn not in self.device_values:
            self.default_power_deferred.add(sn)
            return
        self.default_power_deferred.discard(sn)
        if None not in self.model_rating(sn):
            return
        self.warned_default_power.add(sn)
        self.log(
            "Warn: Hanchu {} did not report its power limits ({}), so the {} W default is in use. Set hanchu_battery_rate_max and hanchu_inverter_limit if that is not your unit's rating".format(
                sn, ", ".join(missing), HANCHU_RANGES[HANCHU_KEY_CHARGE_POWER][1]
            )
        )

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
                self._apply_slot_model(sn, *self._parse_menu_slot_model(menu))
            # Warned on a failed menu too: with no ranges held, the 5 kW default is just as much
            # in force. A device whose ranges were read on an earlier cycle is not warned about.
            self._warn_if_default_power(sn, self.device_ranges.get(sn) or {})
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
            read_started = time.time()
            ok, data = await self._post("iot_get", body={"sn": sn, "devType": HANCHU_DEV_TYPE_DTU, "keys": list(HANCHU_CONTROL_KEYS)})
            if ok and isinstance(data, dict):
                self.device_settings[sn] = dict(data)
                self._note_power_ceiling(sn, data)
                self._note_external_change(sn, data, read_started)
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

        The model's battery limit from HANCHU_MODELS where it is known (batpred#5664). Otherwise the
        CHARGE POWER key's upper bound - the menu's, or a higher value the device holds (see
        ranges_for) - which depends on what the limits were last set to rather than on the unit.
        Leaving battery_rate_max unmapped is NOT neutral: inverter.py then uses a hard-coded 2600 W
        silently on every plan, and makes it the governing term in the charge rate calculation, so an
        absent value is worse than this one. The user's override wins where they know the pack's real limit.
        """
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        battery_limit = self.model_rating(sn)[0]
        if battery_limit:
            return float(battery_limit)
        return float(self.ranges_for(sn).get(HANCHU_KEY_CHARGE_POWER, (0, 0))[1])

    def inverter_limit(self, sn):
        """Return the inverter's nominal AC power in watts.

        The model's AC rating from HANCHU_MODELS where it is known (batpred#5664): on a
        HESS-HY-S-3.0K that is 3000 W, below the 5000 W the menu and the battery accept. Otherwise
        the discharge power ceiling stands in for it: the largest figure the device will accept, raised
        to a higher value the device holds (see ranges_for). That is a setting, not a rating, so it can
        be wrong either way - which is why the table exists, and why the warning in
        _warn_if_default_power() points at hanchu_inverter_limit. Deliberately the DISCHARGE bound
        rather than the charge one, because that is the direction the inverter's AC rating actually
        limits. The user's override wins.
        """
        if self.inverter_limit_override > 0:
            return self.inverter_limit_override
        ac_rating = self.model_rating(sn)[1]
        if ac_rating:
            return float(ac_rating)
        return float(self.ranges_for(sn).get(HANCHU_KEY_DISCHARGE_POWER, (0, 0))[1])

    def reserve_max(self, devices=None):
        """Return the highest reserve every device can hold, in percent - the discharge floor key's ceiling."""
        return min(int(self.ranges_for(sn).get(HANCHU_KEY_DISCHARGE_SOC, HANCHU_RANGES[HANCHU_KEY_DISCHARGE_SOC])[1]) for sn in (devices or self.device_list))

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
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "ac_pv_power": "W", "generator_power": "W"}
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
            status = self.device_detail.get(sn, {}).get("status")
            if status:
                self.dashboard_item(self._sensor_name(sn, "status"), state=status, attributes={"friendly_name": "Hanchu {} Status".format(sn), "icon": "mdi:check-network-outline"}, app="hanchu")

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

        # The reserve lands on the discharge floor key, which only goes up to 45% on this hardware.
        # Predbat decides whether it can hold the battery on the reserve against inverter_reserve_max
        # (execute.py, Hold Charging), so left at its default of 100 it would hold at 80% by writing
        # 81 - clamped to 45 - and drain the battery to 45% before re-charging from the grid. One
        # value, because inverter.py reads it without an index; the lowest device ceiling governs.
        self.set_arg_auto("inverter_reserve_max", self.reserve_max(devices))

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
        charge_low, charge_high = ranges.get(HANCHU_KEY_GRID_CHARGE_SOC, (0, 100))
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
        - a firmware version: the plain-JSON surface carries none. The model name is reported, as
          info.model, only where getDeviceStatus's devModel is one HANCHU_MODELS names.
        - the work mode: it changes, and refresh_discovery() compares whole reports, so a changing
          value would re-file the report every time it moved.
        - an AC-coupled verdict. bypMeterSwitch does distinguish the two (HANCHU_AC_COUPLED_FIELD),
          but it is only used to fold AC-coupled solar into pv_power and is not kept, and the
          record's composition vocabulary has been confirmed against no Hanchu system.

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
            # Site-wide in apps.yaml, so every record carries the value automatic_config() binds.
            ratings["inverter_reserve_max"] = self.reserve_max()

            account_ids = {"station": detail["station_id"]} if detail.get("station_id") else {}
            info = {"model": detail["model"]} if detail.get("model") else {}

            inverters.append(
                inverter_record(
                    "hanchu:{}".format(sn),
                    inverter_type="HanchuCloud",
                    composition="direct",
                    functions=functions,
                    capabilities=dict(HANCHU_CAPABILITIES),
                    hardware_ids={"serial": sn},
                    account_ids=account_ids,
                    info=info,
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
        charge_low, charge_high = ranges.get(HANCHU_KEY_GRID_CHARGE_SOC, (0, 100))
        floor_low, floor_high = ranges.get(HANCHU_KEY_DISCHARGE_SOC, (0, 100))
        # Each direction advertises its own ceiling: a 6 kW unit can discharge more than it charges.
        power_high = {direction: int(ranges.get(key, (0, 20000))[1]) or 20000 for direction, key in (("charge", HANCHU_KEY_CHARGE_POWER), ("export", HANCHU_KEY_DISCHARGE_POWER))}

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
                attributes={"min": 0, "max": power_high[direction], "step": HANCHU_POWER_STEP, "unit_of_measurement": "W", "friendly_name": "Hanchu {} {} Power".format(sn, direction.title()), "icon": "mdi:flash"},
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
                await self._on_component_loop(self.apply_schedule(sn))
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

    async def _on_component_loop(self, coro):
        """Await coro on the component's own event loop, handing it over when called from another one.

        Predbat presses the write button from its plan thread through ha.py's run_async(), which runs
        the event on a throwaway loop of its own. The HTTP session and write_lock belong to the loop
        run() ticks on, so using them from the throwaway loop failed every time with "Timeout context
        manager should be used inside a task", and each change reached the device only on the next
        tick, about a minute later. The same hand-over gateway.py's _publish_raw() makes.

        Runs coro in place when already on the component's loop, or when that loop is not running
        (not started yet, or stopped), as before. A handed-over write is waited for for at most
        HANCHU_LOOP_HANDOVER_TIMEOUT seconds; it is shielded, so giving up the wait does not cancel it.

        Stopping or restarting the component ends that loop under a waiting caller, two ways. Its
        asyncio.run() cancels the write in flight, which is logged and returns False here rather than
        escaping as a CancelledError - a cancellation of the caller itself is still re-raised. Or the
        loop stops before the handed-over write starts, which would then never run, so the wait is
        taken in short steps and abandoned as soon as the loop is no longer running.
        """
        loop = self._loop
        if loop is None or loop is asyncio.get_running_loop() or loop.is_closed() or not loop.is_running():
            return await coro
        future = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))
        deadline = time.monotonic() + HANCHU_LOOP_HANDOVER_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.log("Warn: Hanchu write is still running on the component's loop after {}s; Predbat will stop waiting for it".format(HANCHU_LOOP_HANDOVER_TIMEOUT))
                return False
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=min(HANCHU_LOOP_HANDOVER_POLL, remaining))
            except asyncio.TimeoutError:
                if loop.is_closed() or not loop.is_running():
                    self.log("Info: Hanchu component's loop stopped before the write ran; it will be sent when the component next runs")
                    return False
            except asyncio.CancelledError:
                if not future.cancelled():
                    raise
                self.log("Info: Hanchu write was cancelled on the component's loop as the component stopped; it will be sent when the component next runs")
                return False

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
        charge target (the grid-charge limit), the device-wide discharge floor, the work mode
        (unless the user has turned that off) and both slot tables. Every value is clamped to the
        range this device reports. The overall charge ceiling, CHG_BAT_SOC_LMT, is never written:
        it caps solar charging too, so it stays as the user set it.

        A charge window and an export window that overlap are never both written - the vendor's own
        settings card refuses to submit that. See _resolve_overlap().

        A hold is written as a charge slot at zero power, because a zero discharge rate on its own
        does nothing outside a slot on this hardware - see _rate_signal(). The reserve is never
        written above the current SoC: the slot does the holding, and a reserve above the SoC could
        make the inverter charge from the grid to reach it.

        Every one of the three charge and three discharge slots is written, and any the plan does
        not use is set to 00:00-00:00 - see HANCHU_SLOT_DISABLED. User-defined mode runs all six,
        so a slot left over from the Hanchu app would otherwise fight the plan.
        """
        ranges = self.ranges_for(sn)
        charge, export = self._resolve_overlap(sn, schedule.get("charge", {}), schedule.get("export", {}))
        payload = {}
        signal, extra_direction = self._rate_signal(sn, charge, export)
        soc = self.device_values.get(sn, {}).get("soc")

        pairs = {"charge": self._slot_pairs(sn, "charge", charge), "export": self._slot_pairs(sn, "export", export)}
        if extra_direction:
            # The hold slot goes in slot 3, which Predbat's own window never uses.
            other = "export" if extra_direction == "charge" else "charge"
            extra = self._extra_slot(sn, pairs[extra_direction][:2] + pairs[other])
            if extra:
                pairs[extra_direction][2] = extra
            else:
                signal = None

        # Power. Rate 0 is how Predbat expresses freeze, and 0 is inside the accepted range, so it
        # survives the clamp and is passed straight through. A hold zeroes the charge rate as well,
        # because it is carried by a charge slot at zero power - decided after the slot, so a hold
        # with no room for one leaves the plan's charge rate alone (batpred#5672).
        charge_power = 0 if signal == "hold" else charge.get("power", 0)
        payload[HANCHU_KEY_CHARGE_POWER] = self._clamp(sn, HANCHU_KEY_CHARGE_POWER, charge_power, ranges)
        payload[HANCHU_KEY_DISCHARGE_POWER] = self._clamp(sn, HANCHU_KEY_DISCHARGE_POWER, export.get("power", 0), ranges)

        # The charge target goes in the grid-charge limit, clamped to 20-100 on this hardware, so a
        # plan asking for less cannot be expressed - _clamp says so once per device. During a hold it
        # is capped at the current SoC as a backstop - see _hold_grid_target().
        grid_target = charge.get("soc", 0)
        if signal == "hold" and soc is not None:
            grid_target = self._hold_grid_target(sn, grid_target, soc, ranges)
        payload[HANCHU_KEY_GRID_CHARGE_SOC] = self._clamp(sn, HANCHU_KEY_GRID_CHARGE_SOC, grid_target, ranges)

        # The discharge floor is device-wide, so it carries the export target while an export
        # window is actually running and the reserve at every other moment - the same shape every
        # other cloud adapter with a single floor register uses.
        floor = export.get("soc", 0) if self._window_active_now(export) else schedule.get("reserve", 0)
        if soc is not None and not self._window_active_now(export):
            floor = min(self._as_float(floor, 0), int(self._as_float(soc, 0)))
        payload[HANCHU_KEY_DISCHARGE_SOC] = self._clamp(sn, HANCHU_KEY_DISCHARGE_SOC, floor, ranges)

        for direction, start_key, end_key in (("charge", HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END), ("export", HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END)):
            for index, (start, end) in enumerate(pairs[direction], start=1):
                payload[start_key.format(index)] = start
                payload[end_key.format(index)] = end

        # The work mode is only asserted when there is actually a window to run, and only if the
        # user has not opted out. It is the least well evidenced thing in this component (see
        # HANCHU_WORK_MODE_FOR_SCHEDULE), so it is never written speculatively - with no window
        # enabled the mode is the user's business.
        if self.work_mode_control and (charge.get("enable") or export.get("enable") or signal):
            payload[HANCHU_KEY_WORK_MODE] = self.slot_work_mode(sn)
        return payload

    def _hold_grid_target(self, sn, target, soc, ranges):
        """Return the grid-charge limit to write during a hold: the charge target, but never above the current SoC.

        The hold slot is a charge slot at zero power, so if that zero did not take - a refused key,
        or the interval before a failed write is resent - the slot would charge from the grid into
        the plan's target, which for a hold added outside a charge window is a FUTURE window's and can
        be 100. With the limit at the SoC there is nothing for it to charge to. It adds no API call,
        being part of the same iotSet, and the normal branch writes the real target back once the
        hold ends (batpred#5667). It only limits charging from the grid, so solar still charges.

        The limit already written is kept while it is still at or below the SoC, so solar raising
        the SoC during a hold does not send a new write at every percent - each one is a slow cloud
        call that can collide with the next (error 300004).
        """
        low = ranges.get(HANCHU_KEY_GRID_CHARGE_SOC, HANCHU_RANGES[HANCHU_KEY_GRID_CHARGE_SOC])[0]
        cap = min(self._as_float(target, 0), max(int(self._as_float(soc, 0)), low))
        written = (self.applied_payload.get(sn) or {}).get(HANCHU_KEY_GRID_CHARGE_SOC)
        if written is not None and low <= self._as_float(written, -1) <= cap:
            return int(self._as_float(written))
        return int(cap)

    def _rate_signal(self, sn, charge, export):
        """Read a hold out of Predbat's rates, returning (signal, direction of the slot to add).

        On an inverter with no pause mode, execute.py asks for every hold - freeze charge, hold
        charging, holding for a car or for iBoost - with a discharge rate of 0, and for freeze-export
        with a charge rate of 0. HARDWARE (batpred#5305): on a HESS-HY-S-6.0K neither rate does
        anything outside a slot - the battery kept feeding the house at a zero discharge rate - but a
        charge slot at zero power held it. So a hold becomes a charge slot at zero power.

        Freeze-export (a zero charge rate) adds no slot, and Predbat no longer asks for one: INVERTER_DEF
        sets support_discharge_freeze False. Predbat's freeze-export exports surplus solar while the
        battery still covers the house; HARDWARE (batpred#5305) showed a discharge slot at zero power
        stopping the battery covering the house, so no slot fits, and HARDWARE (batpred#5665, an
        AC-coupled HESS-HY-S-5.0K) showed CHG_PWR_LMT only limiting charging from the grid, so a zero
        charge rate leaves solar filling the battery. "Maximum Charge SOC" held at the current SoC would
        stop it, but only from 50% up and only with a guaranteed restore, which this component lacks.
        A zero charge rate that still arrives (set by hand) is passed through as it is.

        Returns (None, None) for an export that is running (it owns the battery), for both rates
        zero or both set (no signal - an unset schedule reads as zeros), and for a charge window that
        is running with the battery still below its target (that is charging). A running charge
        window whose target is met is a hold carried by that window, so no slot is added.
        """
        if self._window_active_now(export):
            return None, None
        charge_zero = self._as_float(charge.get("power"), 0) == 0
        export_zero = self._as_float(export.get("power"), 0) == 0
        if charge_zero == export_zero:
            return None, None
        if export_zero:
            if self._window_active_now(charge):
                soc = self.device_values.get(sn, {}).get("soc")
                if soc is not None and self._as_float(soc, 0) >= self._as_float(charge.get("soc"), 0):
                    return "hold", None
                return None, None
            return "hold", "charge"
        return None, None

    def _extra_slot(self, sn, taken):
        """Return the (start, end) seconds for a hold slot, or None if there is no room.

        It runs from the start of the current half hour to an hour later, so it changes - and is
        written - only every 30 minutes, and if Predbat stops renewing it the inverter is back in
        self-use within the hour. It stops at 23:59 rather than wrapping, and is cut short of any
        enabled slot it would overlap (the vendor card refuses overlapping slots).
        """
        start = (self.minutes_now - self.minutes_now % 30) * 60
        end = min(start + 3600, HANCHU_SECONDS_PER_DAY - 60)
        for other_start, other_end in taken:
            if (other_start, other_end) == (HANCHU_SLOT_DISABLED, HANCHU_SLOT_DISABLED):
                continue
            if other_start < end and start < other_end:
                if other_start > start:
                    end = other_start
                else:
                    self.log("Info: Hanchu {} has no free time for a hold slot now - an enabled slot already covers it".format(sn))
                    return None
        if end <= start:
            return None
        return (start, end)

    def _resolve_overlap(self, sn, charge, export):
        """Return (charge, export) with one of them disabled if their slots would overlap.

        The window running now is kept. If neither is running (or, which Predbat never asks for,
        both are), the one starting sooner is kept. Windows that only touch at an edge do not overlap.
        The drop is logged once per pair of windows, because a dropped window never runs.
        """
        if not (charge.get("enable") and export.get("enable")):
            return charge, export
        disabled = (HANCHU_SLOT_DISABLED, HANCHU_SLOT_DISABLED)
        charge_pairs = [pair for pair in self._slot_pairs(sn, "charge", charge) if pair != disabled]
        export_pairs = [pair for pair in self._slot_pairs(sn, "export", export) if pair != disabled]
        if not any(c_start < e_end and e_start < c_end for c_start, c_end in charge_pairs for e_start, e_end in export_pairs):
            return charge, export
        charge_active, export_active = self._window_active_now(charge), self._window_active_now(export)
        if charge_active != export_active:
            keep_charge = charge_active
        else:

            def minutes_until(window):
                """Return minutes from now until a window starts, wrapping past midnight."""
                return (hhmmss_to_seconds(window.get("start", "00:00:00")) // 60 - self.minutes_now) % (24 * 60)

            keep_charge = minutes_until(charge) <= minutes_until(export)
        dropped, kept = ("export", "charge") if keep_charge else ("charge", "export")
        note = (sn, "overlap", charge.get("start"), charge.get("end"), export.get("start"), export.get("end"))
        if note not in self._clamp_logged:
            self._clamp_logged.add(note)
            self.log(
                "Warn: Hanchu {} charge window {}-{} and export window {}-{} overlap, which the device does not accept, so only the {} window is written and the {} window is dropped".format(
                    sn, charge.get("start"), charge.get("end"), export.get("start"), export.get("end"), kept, dropped
                )
            )
        if keep_charge:
            return charge, dict(export, enable=False)
        return dict(charge, enable=False), export

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
        interval = self.min_write_interval
        if self.write_failures.get(sn):
            interval = min(interval, HANCHU_FAILED_WRITE_INTERVAL)
        return (time.time() - last) >= interval

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
            # Every tick lands here, so it is only said while the API trace is on.
            if self.api_debug:
                self.log("Debug: Hanchu {} settings unchanged, nothing sent".format(sn))
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
            self._record_write_failure(sn)
            # Saved inline so a container kill right after a write does not lose the pacing
            # timestamp just stamped above and reopen the retry storm this guards against.
            await self.save_control()
            return False

        rejected = self.rejected_keys(data)
        if rejected:
            # A partial accept is NOT cached as applied: the next cycle must rebuild and resend,
            # or the rejected keys would be assumed to be in force forever.
            self.log("Warn: Hanchu {} accepted the write but refused {}; it will be rebuilt and resent on the next eligible cycle".format(sn, ", ".join(rejected)))
            self._record_write_failure(sn)
            await self.save_control()
            return False

        self.applied_payload[sn] = dict(payload)
        self.log("Info: Hanchu wrote {} setting(s) for {} in one call".format(len(payload), sn))
        self._record_write_success(sn)
        await self.save_control()
        return True

    def _record_write_failure(self, sn):
        """Count a rejected write; after several in a row, tell the user once.

        A log line alone is not enough here: a rejected shorten leaves the inverter running past
        the plan, and a rejected lengthen ends a window early. Both need a person to notice.
        """
        count = self.write_failures.get(sn, 0) + 1
        self.write_failures[sn] = count
        if count >= HANCHU_WRITE_FAILURE_NOTIFY and sn not in self.write_failure_notified:
            self.write_failure_notified.add(sn)
            self.non_fatal_error_occurred()
            self.call_notify("Predbat: Hanchu {} has rejected {} settings writes in a row ({}). The inverter may not be following the plan - check it in the Hanchu app".format(sn, count, self.last_api_error or "no reason given"))

    def _record_write_success(self, sn):
        """Clear the failure streak, and say so if the user was told about it."""
        if sn in self.write_failure_notified:
            self.log("Info: Hanchu {} settings writes are being accepted again after {} failures".format(sn, self.write_failures.get(sn, 0)))
            self.write_failure_notified.discard(sn)
        self.write_failures.pop(sn, None)

    async def apply_schedule(self, sn, force=False):
        """Build and send the locally held schedule for one device.

        Refused for a device on the array time-slot format: it would accept the TCT_/TDT_ write and
        ignore it, which looks exactly like a window that ran.
        """
        if not self.control_enable:
            return False
        if self.slot_model.get(sn) == "array":
            if (sn, "array") not in self._slot_notes_logged:
                self._slot_notes_logged.add((sn, "array"))
                self.log("Warn: Hanchu {} uses a different time-slot format (array-valued periods with FLAG_ENABLE_CYCLE), which Predbat cannot drive yet - nothing is written to it".format(sn))
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
        await self.save_cache(
            HANCHU_CACHE_CONFIG,
            {
                "device_ranges": {sn: list(ranges.items()) for sn, ranges in self.device_ranges.items()},
                "device_settings": self.device_settings,
                "power_ceiling": self.power_ceiling,
                "slot_model": self.slot_model,
                "slot_work_modes": self.slot_work_modes,
            },
        )

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
        self.power_ceiling = {sn: dict(keys or {}) for sn, keys in (config.get("power_ceiling") or {}).items()}
        # A cache saved before power_ceiling existed still holds the last read, which seeds it.
        for sn, settings in self.device_settings.items():
            self._note_power_ceiling(sn, settings)
        self.slot_model = dict(config.get("slot_model") or {})
        self.slot_work_modes = {sn: list(modes or []) for sn, modes in (config.get("slot_work_modes") or {}).items()}
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
        offline = [sn for sn in self.device_list if self.device_online.get(sn) is False]
        if offline:
            return "Hanchu {} offline - readings are stale".format(", ".join(offline))
        unsupported = [sn for sn in self.device_list if self.slot_model.get(sn) == "array"]
        if unsupported:
            return "Hanchu {} uses a time-slot format Predbat cannot drive".format(", ".join(unsupported))
        return None

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any schedule change.

        Returns True on a completed cycle, False on a failure that should hold the component in
        ComponentBase's startup backoff and be retried. Deliberately explicit rather than falling
        through to Python's implicit None: ComponentBase only clears its `first` flag when this
        returns something truthy, so an accidental None would strand the component in the
        ever-growing startup backoff forever even though every later cycle was working.
        """
        self._loop = asyncio.get_running_loop()
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
        # An offline datalogger means stale readings and writes that cannot land, so the component is
        # not reported healthy while any device is offline; health_message() then says why.
        if not any(self.device_online.get(sn) is False for sn in self.device_list):
            self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state on shutdown so a restart resumes without re-polling, then close the HTTP session."""
        await self.save_static()
        await self.save_config()
        await self.save_control()
        await self.close_session()


# ----------------------------------------------------------------------
# Command-line harness for live hardware
#
#   python3 hanchu.py --account you@example.com            read-only report (the default)
#   python3 hanchu.py ... --write-test                     write a future window, read it back, restore
#   python3 hanchu.py ... --live-window charge             run a real window now, watch it, restore
#   python3 hanchu.py ... --restore hanchu_snapshot_*.json put back a snapshot a test saved
#
# Nobody on the project has Hanchu hardware, so this is how a tester proves or disproves what
# hanchu_const.py marks INFERRED. The pure helpers below decide what gets written, what gets
# restored and how a run is judged, and are unit-tested (tests/test_hanchu_cli.py); the async
# drivers that talk to the real cloud are not.
# ----------------------------------------------------------------------

# The live tests, each named after the Predbat state it reproduces.
HANCHU_CLI_SCENARIOS = ("charge", "export", "freeze-charge", "freeze-export", "hold")

# What each scenario should make the battery do, printed before anything is written so the tester
# knows what they are watching for.
HANCHU_CLI_EXPECTATION = {
    "charge": "the battery CHARGES (battery power negative) at about the requested power",
    "export": "the battery DISCHARGES (battery power positive) and the surplus is exported",
    "freeze-charge": "the battery neither charges nor discharges while the house draws from the grid",
    "freeze-export": "the battery does not charge, so surplus solar is exported instead",
    "hold": "the battery neither charges nor discharges, because the open charge slot's target is already met",
}

# Every key a restore may write back: everything build_write_payload() can touch. The overall charge
# ceiling is read but never written by Predbat, so a restore leaves it alone too.
HANCHU_CLI_RESTORE_KEYS = [key for key in HANCHU_CONTROL_KEYS if key != HANCHU_KEY_CHARGE_SOC]

# apps.yaml keys --config reads, mapped to the command-line fields they stand in for.
HANCHU_CLI_CREDENTIAL_KEYS = {"account": "hanchu_account", "password": "hanchu_password", "serial": "hanchu_inverter_sn"}

# Readings inside this many seconds of a write are ignored when judging: the cloud relays the write
# to the inverter and the inverter ramps, so the first minute shows the old behaviour.
HANCHU_CLI_SETTLE_SECONDS = 60

# Battery or surplus power below this many watts is treated as noise when judging a run.
HANCHU_CLI_THRESHOLD_W = 100


def merge_hanchu_credentials(cli, config):
    """Fill any credential not given on the command line from an apps.yaml pred_bat section.

    An explicit command-line value always wins. hanchu_inverter_sn may be a list in apps.yaml, but
    the harness drives one device, so a list gives its first entry.

    Returns (merged, used): used names the apps.yaml keys actually taken, so the caller can say so.
    """
    merged = dict(cli)
    used = []
    for cli_name, config_key in HANCHU_CLI_CREDENTIAL_KEYS.items():
        if merged.get(cli_name):
            continue
        value = config.get(config_key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            merged[cli_name] = str(value)
            used.append(config_key)
    return merged, used


def _cli_number(value):
    """Return a settings value as an int where it reads as a number, else unchanged.

    iotGet returns values as strings or floats ("3", 70.0), while build_write_payload() produces
    ints, so both sides are normalised before they are compared or written back.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return value


def _cli_hhmm(minutes):
    """Format minutes since midnight as Predbat's HH:MM:SS control-entity form."""
    return "{:02d}:{:02d}:00".format(minutes // 60, minutes % 60)


def build_cli_schedule(scenario, start_minutes, soc, settings, ranges, window_minutes, power, require_effect=True, target=None):
    """Build the schedule a live test writes, in the shape get_schedule_settings_ha() produces.

    It goes through the component's own build_write_payload() afterwards, so the test exercises the
    shipped payload path rather than a hand-built one. Each scenario mirrors what execute.py leaves
    on the control entities for that state:

    - charge: a charge window from start_minutes, target 10 points above the current SoC.
    - export: an export window from start_minutes, floor 10 points below the current SoC.
    - freeze-charge: no window, discharge rate 0 and the reserve raised to SoC + 1 (how Predbat
      holds the battery on an inverter with no pause mode).
    - freeze-export: no window, charge rate 0 (how Predbat stops the battery soaking up solar).
    - hold: a charge window from start_minutes whose grid-charge target is the current SoC, with no
      charge or discharge power - a candidate single hold for every SoC from 20% up, where the
      reserve stops at 45%. It holds only if the inverter idles in a slot whose target is met.

    The rate Predbat is not using is left at the device maximum, as Predbat does outside a window,
    and a disabled charge window carries a 100% target - Predbat's demand-mode value, and the one
    that cannot cap solar charging if the target turns out to be device-wide.

    target, when given, replaces the charge target or export floor - set it just past the current SoC
    to watch the inverter stop a window at the target by itself.

    Raises ValueError when the scenario is unknown, the window would have under two minutes before
    midnight, or (with require_effect) the battery is already where the test would move it, so the
    run could only ever come back inconclusive.

    Returns (schedule, notes), notes explaining anything adjusted to fit the device.
    """
    if scenario not in HANCHU_CLI_SCENARIOS:
        raise ValueError("unknown scenario {!r}; choose one of {}".format(scenario, ", ".join(HANCHU_CLI_SCENARIOS)))
    notes = []
    charge_low, charge_high = ranges.get(HANCHU_KEY_GRID_CHARGE_SOC, HANCHU_RANGES[HANCHU_KEY_GRID_CHARGE_SOC])
    floor_low, floor_high = ranges.get(HANCHU_KEY_DISCHARGE_SOC, HANCHU_RANGES[HANCHU_KEY_DISCHARGE_SOC])
    charge_max = int(ranges.get(HANCHU_KEY_CHARGE_POWER, HANCHU_RANGES[HANCHU_KEY_CHARGE_POWER])[1])
    discharge_max = int(ranges.get(HANCHU_KEY_DISCHARGE_POWER, HANCHU_RANGES[HANCHU_KEY_DISCHARGE_POWER])[1])
    soc = float(soc)

    reserve = _cli_number(settings.get(HANCHU_KEY_DISCHARGE_SOC))
    if not isinstance(reserve, int):
        reserve = int(floor_low)
    reserve = max(int(floor_low), min(int(floor_high), reserve))

    def capped(requested, high):
        """Cap a requested power at the device's range, noting it when the cap bites."""
        if requested is None:
            return high
        requested = max(0, int(requested))
        if requested > high:
            notes.append("power capped at {} W, the most this device accepts".format(high))
            return high
        return requested

    schedule = {
        "reserve": reserve,
        "charge": {"enable": False, "soc": int(charge_high), "power": charge_max, "start": "00:00:00", "end": "00:00:00"},
        "export": {"enable": False, "soc": reserve, "power": discharge_max, "start": "00:00:00", "end": "00:00:00"},
    }

    if scenario in ("charge", "export", "hold"):
        end_minutes = start_minutes + int(window_minutes)
        if end_minutes > 23 * 60 + 59:
            end_minutes = 23 * 60 + 59
            notes.append("window cut at 23:59 so it does not cross midnight")
        if end_minutes - start_minutes < 2:
            raise ValueError("under two minutes left before midnight - run the test after 00:00")
        window = {"enable": True, "start": _cli_hhmm(start_minutes), "end": _cli_hhmm(end_minutes)}

    if scenario == "charge":
        if require_effect and soc >= charge_high - 1:
            raise ValueError("the battery is at {:.0f}%, so a charge would show nothing - try export or freeze-charge".format(soc))
        if require_effect and target is not None and soc >= int(target):
            raise ValueError("the battery is already at {:.0f}%, at or above the {}% target - set --target above it".format(soc, target))
        target = min(int(charge_high), int(soc) + 10) if target is None else int(target)
        if target < charge_low:
            notes.append("charge target raised to {}%, the lowest this device accepts".format(charge_low))
            target = int(charge_low)
        schedule["charge"] = dict(window, soc=target, power=capped(power, charge_max))
    elif scenario == "export":
        wanted = int(soc) - 10 if target is None else int(target)
        floor = max(int(floor_low), min(int(floor_high), wanted))
        if floor == int(floor_high) and wanted > floor_high:
            notes.append("export floor set to {}%, the highest this device accepts, so the battery may export further than asked".format(floor_high))
        # An explicit target is the tester's choice, so only a battery already at or below it is
        # refused; the default floor also needs a little room to show anything.
        if require_effect and soc <= floor + (0 if target is not None else 2):
            raise ValueError("the battery is at {:.0f}%, too close to the {}% floor for an export to show anything - try charge or freeze-export".format(soc, floor))
        schedule["export"] = dict(window, soc=floor, power=capped(power, discharge_max))
    elif scenario == "hold":
        if int(soc) < charge_low:
            notes.append("target set to {}%, the lowest the grid-charge limit accepts; with no charge power the slot still cannot charge".format(charge_low))
        schedule["charge"] = dict(window, soc=max(int(charge_low), min(int(charge_high), int(soc))), power=0)
        schedule["export"]["power"] = 0
    elif scenario == "freeze-charge":
        # execute.py, on an inverter with no pause mode: discharge rate 0 AND the reserve raised to
        # SoC + 1. HARDWARE (batpred#5305): the rate alone does not hold the battery outside a slot.
        # Above the floor's ceiling Predbat will not freeze at all (inverter_reserve_max).
        if require_effect and int(soc) + 1 > floor_high:
            raise ValueError("the battery is at {:.0f}%, above the {}% the reserve can be set to - above that Predbat holds with its charge window instead, which --live-window hold reproduces".format(soc, floor_high))
        schedule["reserve"] = max(int(floor_low), min(int(floor_high), int(soc) + 1))
        schedule["export"]["soc"] = schedule["reserve"]
        schedule["export"]["power"] = 0
    else:
        schedule["charge"]["power"] = 0
    return schedule, notes


def cli_future_start(minutes_now, window_minutes):
    """Pick a start time for the write test's window: three hours from now, never crossing midnight.

    The write test proves a write lands and reads back without moving any energy, so its window must
    not cover the present, and must fit inside one day since the harness never writes a wrap-around.
    """
    start = (minutes_now + 180) % (24 * 60)
    if start + window_minutes > 23 * 60 + 59:
        start = 60
    return start


def settings_mismatches(expected, actual):
    """Return (key, expected, actual) for every expected setting the read-back does not match.

    Compared as numbers, since iotGet returns "3" or 70.0 where the write sent 3 or 70. A key the
    read-back does not carry at all is a mismatch with actual None.
    """
    mismatches = []
    for key, value in expected.items():
        got = actual.get(key)
        if got is None or _cli_number(got) != _cli_number(value):
            mismatches.append((key, value, got))
    return mismatches


def restore_payload(snapshot, keys):
    """Build the write that puts keys back to their snapshot values.

    Returns (payload, missing). missing names keys the snapshot never held, which cannot be restored
    and must be reported rather than silently left at the test's value.
    """
    payload = {}
    missing = []
    for key in keys:
        if snapshot.get(key) is None:
            missing.append(key)
        else:
            payload[key] = _cli_number(snapshot[key])
    return payload, missing


def save_cli_snapshot(folder, sn, settings):
    """Save the settings read before a test write, so the test can be undone even if the run dies.

    Returns the file's path, which the harness prints before it writes anything.
    """
    path = os.path.join(folder or ".", "hanchu_snapshot_{}_{}.json".format(sn, datetime.now().strftime("%Y%m%d_%H%M%S")))
    with open(path, "w") as handle:
        json.dump({"serial": sn, "taken": datetime.now().replace(microsecond=0).isoformat(), "settings": settings}, handle, indent=2, default=str)
    return path


def load_cli_snapshot(path):
    """Load a snapshot saved by save_cli_snapshot(), returning (serial, settings).

    Raises ValueError when the file is unreadable or does not look like a snapshot.
    """
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise ValueError("could not read snapshot {}: {}".format(path, error))
    if not isinstance(data, dict) or not data.get("serial") or not isinstance(data.get("settings"), dict):
        raise ValueError("{} is not a Hanchu settings snapshot".format(path))
    return str(data["serial"]), data["settings"]


def judge_live_window(scenario, samples, threshold=HANCHU_CLI_THRESHOLD_W, settle_seconds=HANCHU_CLI_SETTLE_SECONDS, target=None):
    """Decide whether a live test did what the scenario expects, from its telemetry samples.

    samples are dicts of Predbat-convention readings (battery negative on charge) plus "elapsed",
    seconds since the write was accepted. Readings inside settle_seconds are ignored.

    A freeze is only judged on readings where it could show something: freeze-charge needs the house
    to be drawing more than solar supplies, freeze-export needs a solar surplus. Without that, a held
    battery and a free one look identical, and the answer is INCONCLUSIVE rather than a false PASS.

    With a target (--target) on a charge or export, the question is instead whether the inverter
    stops at the target by itself: moving before the SoC reaches it, idle once it has. A run that
    never reaches the target says nothing about stopping, so it is INCONCLUSIVE.

    Returns (verdict, reason), verdict one of PASS, FAIL or INCONCLUSIVE.
    """

    def reading(item, key):
        """Return one reading as a float, treating a missing or junk value as zero."""
        try:
            return float(item.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    settled = [item for item in samples if item.get("elapsed", 0) >= settle_seconds and item.get("battery_power") is not None]
    if not settled:
        return "INCONCLUSIVE", "no readings after the first {}s, while the inverter is still settling - run for longer".format(settle_seconds)

    def solar(item):
        """Return the solar reading - pv_power, which already carries AC-coupled solar where there is any."""
        return reading(item, "pv_power")

    battery = [reading(item, "battery_power") for item in settled]
    median = statistics.median(battery)
    if target is not None and scenario in ("charge", "export"):
        charging = scenario == "charge"
        reached = [index for index, item in enumerate(settled) if (reading(item, "soc") >= target if charging else reading(item, "soc") <= target)]
        if not reached:
            return "INCONCLUSIVE", "the SoC never reached the {}% target in the window, so stopping at it was not tested - set --target closer or run longer".format(target)
        after = [reading(item, "battery_power") for item in settled[reached[0] :]]
        moving = sum(1 for value in after if (value <= -threshold if charging else value >= threshold))
        if moving * 2 > len(after):
            return "FAIL", "the battery kept {} past the {}% target (median {:.0f} W after reaching it)".format("charging" if charging else "discharging", target, statistics.median(after))
        return "PASS", "the battery stopped at the {}% target by itself (median {:.0f} W after reaching it)".format(target, statistics.median(after))
    if scenario == "charge":
        if sum(1 for value in battery if value <= -threshold) * 2 > len(battery):
            return "PASS", "the battery charged at a median {:.0f} W".format(-median)
        return "FAIL", "the battery did not charge - median battery power {:.0f} W (negative is charging)".format(median)
    if scenario == "export":
        if sum(1 for value in battery if value >= threshold) * 2 > len(battery):
            return "PASS", "the battery discharged at a median {:.0f} W".format(median)
        return "FAIL", "the battery did not discharge - median battery power {:.0f} W (positive is discharging)".format(median)
    if scenario in ("freeze-charge", "hold"):
        demand = [item for item in settled if reading(item, "load_power") - solar(item) > threshold]
        if not demand:
            return "INCONCLUSIVE", "solar covered the house load throughout, so a held battery and a free one look the same - retry when the house is drawing power"
        draining = [reading(item, "battery_power") for item in demand]
        if sum(1 for value in draining if value >= threshold) * 2 > len(draining):
            return "FAIL", "the battery fed the house at a median {:.0f} W when it should have been held".format(statistics.median(draining))
        # With the house drawing more than solar supplies, any charging is coming from the grid - a
        # reserve above the SoC can make an inverter do that, and it is no more a hold than draining.
        if sum(1 for value in draining if value <= -threshold) * 2 > len(draining):
            return "FAIL", "the battery charged from the grid at a median {:.0f} W when it should have been held".format(-statistics.median(draining))
        return "PASS", "the battery held (median {:.0f} W) while the house drew more than solar supplied".format(statistics.median(draining))
    surplus = [item for item in settled if solar(item) - reading(item, "load_power") > threshold]
    if not surplus:
        return "INCONCLUSIVE", "there was no solar surplus, so there was nothing for the battery to refuse - retry in sunshine"
    charging = [reading(item, "battery_power") for item in surplus]
    if sum(1 for value in charging if value <= -threshold) * 2 > len(charging):
        return "FAIL", "the battery charged from the surplus at a median {:.0f} W when charging should have been stopped".format(-statistics.median(charging))
    return "PASS", "the battery stayed out of the surplus (median {:.0f} W)".format(statistics.median(charging))


def _cli_confirm(prompt, assume_yes=False):  # pragma: no cover
    """Ask before writing to a real inverter, treating a closed stdin or Ctrl-C as no."""
    if assume_yes:
        return True
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nNo input available, nothing sent.")
        return False
    return answer.strip().lower() == "y"


def _cli_refresh_clock(client):  # pragma: no cover
    """Move the mock base's clock to now, so build_write_payload() judges active windows correctly.

    MockBase fixes its clock when it is built, and the discharge floor in the payload depends on
    whether an export window is running at this minute.
    """
    base = client.base
    base.now_utc = datetime.now(base.local_tz)
    base.now_utc_exact = base.now_utc
    base.midnight_utc = base.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    base.minutes_now = base.now_utc.hour * 60 + base.now_utc.minute


def _build_hanchu_cli(args, automatic=False):  # pragma: no cover
    """Construct a HanchuAPI around a MockBase for a standalone run.

    control_enable is always off: the harness writes through _write_payload() directly, after asking,
    and must never have run() or the reconcile loop write on its behalf. Pacing is off because each
    harness write is deliberate and the restore must not be held back.
    """
    from mock_base import MockBase

    client = HanchuAPI(
        MockBase(),
        account=args.account,
        password=args.password,
        inverter_sn=args.serial,
        automatic=automatic,
        control_enable=False,
        api_delay=args.api_delay,
        min_write_interval=0,
    )
    client.api_debug = not args.no_trace
    # Recorded on args so _cli_main() can close its HTTP session however the driver returns.
    args.client = client
    return client


async def _cli_main(driver, args):  # pragma: no cover
    """Run one harness driver, then close the HTTP session it opened, whichever way it returned."""
    try:
        await driver(args)
    finally:
        client = getattr(args, "client", None)
        if client is not None:
            await client.close_session()


def _cli_slot_table(settings):  # pragma: no cover
    """Format the device's charge and discharge slots as one readable line each."""
    lines = []
    for label, start_key, end_key in (("charge", HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END), ("discharge", HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END)):
        slots = []
        for index in range(1, HANCHU_SLOT_COUNT + 1):
            start, end = settings.get(start_key.format(index)), settings.get(end_key.format(index))
            if start is None and end is None:
                slots.append("{}: not read".format(index))
            elif _cli_number(start) == HANCHU_SLOT_DISABLED and _cli_number(end) == HANCHU_SLOT_DISABLED:
                slots.append("{}: off".format(index))
            else:
                slots.append("{}: {}-{}".format(index, seconds_to_hhmmss(start)[:5], seconds_to_hhmmss(end)[:5]))
        lines.append("  {:9} slots  {}".format(label, "   ".join(slots)))
    return lines


def _cli_print_device(client, sn):  # pragma: no cover
    """Print everything a tester needs to check one device against the Hanchu app."""
    settings = client.device_settings.get(sn, {})
    print("\n=== {} ===".format(sn))
    mode = settings.get(HANCHU_KEY_WORK_MODE)
    print("  work mode       {} (raw {!r}){}".format(HANCHU_WORK_MODES.get(_cli_number(mode), "unknown"), mode, "" if _cli_number(mode) == HANCHU_WORK_MODE_FOR_SCHEDULE else " - Predbat switches this to User-defined when it writes a window"))
    for line in _cli_slot_table(settings):
        print(line)
    for key in (HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER, HANCHU_KEY_CHARGE_SOC, HANCHU_KEY_DISCHARGE_SOC, HANCHU_KEY_GRID_CHARGE_SOC):
        print("  {:20} {!r}".format(key, settings.get(key)))

    menu = client.device_ranges.get(sn) or {}
    print("  ranges (compare with the Hanchu app):")
    for key in HANCHU_RANGES:
        low, high = client.ranges_for(sn)[key]
        print("    {:20} {}-{}  from {}".format(key, low, high, "the menu" if key in menu else "the BUILT-IN DEFAULT"))
    if not menu:
        print("  WARNING: the menu gave no ranges, so every limit above is a default. On a 3.68, 6 or 10 kW unit the power limits will be wrong.")

    print("  telemetry       {}".format(json.dumps(client.device_values.get(sn, {}))))
    print("  energy today    {}".format(json.dumps(client.device_energy.get(sn, {}))))
    print("  derived         soc_max {:.2f} kWh, battery_rate_max {:.0f} W, inverter_limit {:.0f} W".format(client.battery_capacity(sn), client.battery_rate_max(sn), client.inverter_limit(sn)))
    model = client.slot_model.get(sn)
    print(
        "  time slots      {}{}".format(
            {"slots": "TCT_/TDT_ slots - the format Predbat writes", "array": "ARRAY periods with FLAG_ENABLE_CYCLE - Predbat will NOT control this device", "unknown": "the menu shows no slot items"}.get(model, "menu not read"),
            ", shown in work mode(s) {}".format(client.slot_work_modes.get(sn)) if client.slot_work_modes.get(sn) else "",
        )
    )
    detail = client.device_detail.get(sn, {})
    print("  device status   {}".format(detail.get("status", "not reported")))


async def _cli_print_coupling(client, sn):  # pragma: no cover
    """Print the raw fields that tell an AC-coupled system from a DC-coupled one, and the generator's.

    On an AC-coupled system the solar is only in bypMeterTotalPower and pvTtPwr reads zero, and
    bypMeterSwitch is 1; the component adds the bypass meter into pv_power on those systems. The raw
    fields are printed so a tester can check that, and report a system with both kinds of PV. The
    generator fields are printed for the same reason: nobody yet knows their sign, or whether the
    generator's output is already counted in the load or grid readings.
    """
    ok, data = await client._post("device_status", body={"sn": sn})
    if not ok or not isinstance(data, dict):
        print("  coupling        could not read getDeviceStatus")
        return
    switch = data.get("bypMeterSwitch")
    print("  coupling        bypMeterSwitch={!r} bypMeterTotalPower={!r} pvTtPwr={!r} -> {}".format(switch, data.get("bypMeterTotalPower"), data.get("pvTtPwr"), {1: "AC-coupled", 0: "DC-coupled"}.get(_cli_number(switch), "unknown")))
    print("  generator       hasDg={!r} dgPAcTotal={!r} {} (load {!r}, grid {!r}, battery {!r})".format(data.get("hasDg"), data.get("dgPAcTotal"), data.get("dgPAcTotalUnit") or "", data.get("loadPwr"), data.get("meterPPwr"), data.get("batP")))
    # Raw fields whose meaning is not yet known, printed so a run can be compared against the app: the
    # several mode fields, the vendor's own schedulers (which could fight Predbat if switched on), and
    # the two different "PV today" figures the status carries.
    for label, keys in (
        ("modes", ("devModel", "workModeCmb", "workMode", "workModeFlag", "pcsStatusDesc", "batSts")),
        ("vendor schedulers", ("strategyOpen", "schedulePending", "scheduleExecuting", "smartflexAuthSts", "smartflexSchedule", "smartflexExecuting")),
        ("today (status)", ("pvTdGe", "pvDge", "loadTdEe", "gridTdEe", "gridTdFe", "batTdChg", "batTdDschg")),
    ):
        print("  {:15} {}".format(label, "  ".join("{}={!r}".format(key, data.get(key)) for key in keys)))


async def _cli_print_menu(client, sn, dump_path=None):  # pragma: no cover
    """List the menu's energy items, and optionally save the whole raw menu to a file.

    The menu can list one setting several times, each for a different set of product models, and a
    trace line is cut at 2000 characters, so the full menu is the only way to see which entry the
    range parser should have used.
    """
    ok, data = await client._post("menu", body={"sn": sn})
    if not ok or not isinstance(data, dict):
        print("  menu            could not be read")
        return
    if dump_path:
        path = dump_path if len(client.device_list) == 1 else "{}_{}".format(sn, dump_path)
        with open(path, "w") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        print("  menu            saved in full to {}".format(path))
    print("  menu energy items (signal, type, min-max, models, shown when):")
    for item in client._menu_items(data):
        signal = item.get("itemCodeSignal") or item.get("itemCode") or "?"
        bounds = "{}-{}".format(item.get("minVal"), item.get("maxVal")) if item.get("minVal") not in (None, "") or item.get("maxVal") not in (None, "") else ""
        listener = item.get("listener") if isinstance(item.get("listener"), dict) else {}
        print(
            "    {:32} type {:3} {:12} models {}{}{}".format(
                signal,
                str(item.get("itemType")),
                bounds,
                item.get("productModel") or "all",
                "  when {}={}".format(listener.get("code"), listener.get("show")) if listener else "",
                "  hidden" if item.get("hidden") else "",
            )
        )


async def _cli_prepare(client):  # pragma: no cover
    """Log in, pick the device and read its settings, ranges and SoC before a test write.

    Returns (sn, settings, soc), or (None, None, None) when anything needed for a safe write is
    missing. Without the current settings there is nothing to restore, so the harness refuses.
    """
    if not await client.ensure_token():
        print("LOGIN FAILED{}".format(": {}".format(client.last_api_error) if client.last_api_error else ""))
        return None, None, None
    await client.refresh_static()
    if not client.device_list:
        print("NO DEVICES - discovery {}".format("failed" if client.discovery_ok is False else "found no DTU on this account"))
        return None, None, None
    sn = client.device_list[0]
    if len(client.device_list) > 1:
        print("Several devices found ({}); using {} - pass --serial to choose".format(", ".join(client.device_list), sn))
    await client.refresh_config()
    await client.refresh_power()
    settings = client.device_settings.get(sn)
    if not settings:
        print("Could not read {}'s current settings (iotGet) - refusing to write without a snapshot to restore".format(sn))
        return None, None, None
    soc = client.device_values.get(sn, {}).get("soc")
    if soc is None:
        print("Could not read {}'s SoC (getDeviceStatus) - refusing to write".format(sn))
        return None, None, None
    _cli_print_device(client, sn)
    if client.slot_model.get(sn) == "array":
        print("{} uses the array time-slot format, which Predbat does not write - refusing. Please report this menu on batpred#5305".format(sn))
        return None, None, None
    if client.device_online.get(sn) is False:
        print("{} is offline, so a write cannot reach it - refusing".format(sn))
        return None, None, None
    return sn, settings, soc


async def _cli_await_settings(client, sn, expected, timeout=120, interval=10):  # pragma: no cover
    """Read the settings back until they match expected, or the timeout runs out.

    Cloud round trips take 7-45s (batpred#5305), so one immediate read could report a write that
    has in fact landed as missing. Returns (actual, mismatches, seconds_waited).
    """
    started = time.time()
    while True:
        ok, data = await client._post("iot_get", body={"sn": sn, "devType": HANCHU_DEV_TYPE_DTU, "keys": list(expected)})
        actual = dict(data) if ok and isinstance(data, dict) else {}
        mismatches = settings_mismatches(expected, actual)
        waited = int(time.time() - started)
        if not mismatches or waited >= timeout:
            return actual, mismatches, waited
        print("  ...read-back does not match yet after {}s ({} key(s) differ), retrying in {}s".format(waited, len(mismatches), interval))
        await asyncio.sleep(interval)


def _cli_print_mismatches(mismatches):  # pragma: no cover
    """Print each setting that did not read back as written."""
    for key, wanted, got in mismatches:
        print("    {:20} wrote {!r}, reads {!r}".format(key, wanted, got))


async def _cli_write(client, sn, payload, label):  # pragma: no cover
    """Send one payload, then read it back. Returns True only when the device holds every value."""
    print("\nWriting {} ({} setting(s))...".format(label, len(payload)))
    if not await client._write_payload(sn, payload, force=True):
        print("  write NOT accepted: {}".format(client.last_api_error or "see the Warn: lines above"))
        return False
    _actual, mismatches, waited = await _cli_await_settings(client, sn, payload)
    if mismatches:
        print("  accepted, but {} setting(s) still differ after {}s:".format(len(mismatches), waited))
        _cli_print_mismatches(mismatches)
        return False
    print("  accepted and read back intact after {}s".format(waited))
    return True


async def _cli_restore(client, sn, snapshot, keys, snapshot_path, args):  # pragma: no cover
    """Put keys back to their snapshot values, and say exactly what to run if that fails."""
    payload, missing = restore_payload(snapshot, keys)
    if missing:
        print("WARNING: the snapshot holds no value for {}, so they stay as the test left them".format(", ".join(missing)))
    if not payload:
        print("Nothing to restore")
        return
    if not await _cli_write(client, sn, payload, "the original settings back"):
        print("WARNING: {} was NOT fully restored. Check it in the Hanchu app, or retry with:".format(sn))
        print("  python3 hanchu.py --account <your account> --restore {}".format(snapshot_path))


def _cli_snapshot(client, sn, settings, args):  # pragma: no cover
    """Save the pre-write settings to disk and tell the tester how to restore them by hand."""
    path = save_cli_snapshot(args.snapshot_dir, sn, settings)
    print("\nSaved the current settings to {}".format(path))
    print("If this run is interrupted, put them back with:  python3 hanchu.py --account <your account> --restore {}".format(path))
    return path


async def test_hanchu_api(args):  # pragma: no cover
    """Read-only report: log in, discover, run one full cycle with control off, and print the results."""
    client = _build_hanchu_cli(args, automatic=True)
    print("Calling run() once (read-only: control is off, nothing is written to the inverter)...")
    ok = await client.run(seconds=0, first=True)
    if not ok:
        if not client._token:
            print("\nLOGIN FAILED - check the e-mail and password you use in the Hanchu app{}".format(": {}".format(client.last_api_error) if client.last_api_error else ""))
        elif client.discovery_ok is False:
            print("\nLOGGED IN, but the device list call failed{}".format(": {}".format(client.last_api_error) if client.last_api_error else ""))
        elif not client.device_list:
            print("\nLOGGED IN, but no DTU was found on this account{}".format(" matching --serial {}".format(args.serial) if args.serial else ""))
        else:
            print("\nDISCOVERY OK ({}), but the first telemetry poll came back empty - see the Warn: lines above".format(", ".join(client.device_list)))
        await client.final()
        return
    for sn in client.device_list:
        _cli_print_device(client, sn)
        await _cli_print_coupling(client, sn)
        await _cli_print_menu(client, sn, args.dump_menu)
    print("\nThe 'Set arg' lines above are what hanchu_automatic: true would put in apps.yaml.")
    await client.final()
    print("Done")


async def test_hanchu_write_readback(args):  # pragma: no cover
    """Write a charge window three hours away, read it back, then restore the original settings.

    Proves iotSet lands and the slot encoding reads back as written, without moving any energy.
    """
    client = _build_hanchu_cli(args)
    sn, settings, soc = await _cli_prepare(client)
    if not sn:
        return
    _cli_refresh_clock(client)
    start = cli_future_start(client.minutes_now, args.minutes)
    schedule, notes = build_cli_schedule("charge", start, soc, settings, client.ranges_for(sn), args.minutes, args.power, require_effect=False)
    payload = client.build_write_payload(sn, schedule)
    print("\nWrite test: a charge window {}-{}, away from now so the battery does not move".format(schedule["charge"]["start"][:5], schedule["charge"]["end"][:5]))
    for note in notes:
        print("  note: {}".format(note))
    print("Payload:\n{}".format(json.dumps(payload, indent=2)))
    if not _cli_confirm("Write this to {} and then restore it? [y/N] ".format(sn), args.yes):
        return
    path = _cli_snapshot(client, sn, settings, args)
    try:
        if await _cli_write(client, sn, payload, "the test window"):
            print("PASS: the write landed and read back intact")
        else:
            print("FAIL: the write did not land as sent")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nInterrupted - restoring")
    finally:
        await _cli_restore(client, sn, settings, list(payload), path, args)
    print("Done")


async def test_hanchu_live_window(args):  # pragma: no cover
    """Run one Predbat state on the real inverter for a few minutes, watch the battery, then restore.

    The schedule goes through the component's own build_write_payload(), so this tests the payload
    Predbat would actually send. A charge or export window ends on the inverter by itself at the
    window end even if this process dies; a freeze has no end time, so the snapshot is the way back.
    """
    client = _build_hanchu_cli(args)
    sn, settings, soc = await _cli_prepare(client)
    if not sn:
        return
    scenario = args.live_window
    _cli_refresh_clock(client)
    # Predbat's clock, and so every window it writes, sits on the 5-minute planning grid: at 19:29 a
    # window starts at 19:25. The test starts on the same grid, so build_write_payload() sees the
    # window as running exactly as it would in Predbat, and is lengthened by the minutes already
    # past the grid line so it still runs for --minutes from now.
    now = datetime.now()
    real_minutes = now.hour * 60 + now.minute
    try:
        schedule, notes = build_cli_schedule(scenario, client.minutes_now, soc, settings, client.ranges_for(sn), args.minutes + real_minutes - client.minutes_now, args.power, target=args.target)
    except ValueError as error:
        print("\nNot running {}: {}".format(scenario, error))
        return
    payload = client.build_write_payload(sn, schedule)
    if scenario.startswith("freeze"):
        if args.user_defined:
            payload[HANCHU_KEY_WORK_MODE] = HANCHU_WORK_MODE_FOR_SCHEDULE
        elif HANCHU_KEY_WORK_MODE not in payload and _cli_number(settings.get(HANCHU_KEY_WORK_MODE)) != HANCHU_WORK_MODE_FOR_SCHEDULE:
            print("\nNOTE: the work mode is not User-defined, and a freeze writes no window, so Predbat would not change it.")
            print("  In practice an earlier Predbat window has usually set User-defined already; add --user-defined to test that case.")

    window = schedule["charge"] if schedule["charge"]["enable"] else schedule["export"]
    if window["enable"]:
        print("\nLive test: {} window {}-{}. Expect {}.".format(scenario, window["start"][:5], window["end"][:5], HANCHU_CLI_EXPECTATION[scenario]))
        print("The window uses this computer's clock - check it matches the inverter's.")
        # Watch until the window ends on the inverter, not for a fixed time from the write.
        hold_seconds = hhmmss_to_seconds(window["end"]) - (real_minutes * 60 + now.second)
    else:
        print("\nLive test: {} for {} minute(s), with no window - it lasts until the restore. Expect {}.".format(scenario, args.minutes, HANCHU_CLI_EXPECTATION[scenario]))
        hold_seconds = args.minutes * 60
    for note in notes:
        print("  note: {}".format(note))
    print("Payload:\n{}".format(json.dumps(payload, indent=2)))
    if not _cli_confirm("Write this to {} now, run it, and then restore? [y/N] ".format(sn), args.yes):
        return
    if window["enable"]:
        # The tester may have taken a while to answer the prompt; the window's end has not moved.
        now = datetime.now()
        hold_seconds = hhmmss_to_seconds(window["end"]) - (now.hour * 3600 + now.minute * 60 + now.second)
        if hold_seconds <= 0:
            print("The window has already ended while waiting for an answer - nothing written")
            return
    path = _cli_snapshot(client, sn, settings, args)
    written_from = time.time()

    samples = []
    try:
        if await _cli_write(client, sn, payload, "the {} test".format(scenario)):
            # The write and read-back took real time, which the window has already used up.
            hold_seconds = max(0, hold_seconds - int(time.time() - written_from))
            started = time.time()
            print("\nWatching for {}s (Ctrl-C restores early). Battery: negative = charging. Grid: positive = exporting.".format(hold_seconds))
            while time.time() - started < hold_seconds:
                await client.refresh_power()
                values = client.device_values.get(sn, {})
                # Measured from the write, not from the read-back: the settle time is the inverter's.
                elapsed = int(time.time() - written_from)
                samples.append(dict(values, elapsed=elapsed))
                print(
                    "  {}  +{:4}s  SoC {}%  battery {} W  grid {} W  pv {} W  ac-pv {} W  load {} W{}".format(
                        datetime.now().strftime("%H:%M:%S"),
                        elapsed,
                        values.get("soc"),
                        values.get("battery_power"),
                        values.get("grid_power"),
                        values.get("pv_power"),
                        values.get("ac_pv_power"),
                        values.get("load_power"),
                        "  generator {} W".format(values.get("generator_power")) if "generator_power" in values else "",
                    )
                )
                await asyncio.sleep(max(1, min(args.interval, hold_seconds - (time.time() - started))))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nInterrupted - restoring")
    finally:
        verdict, reason = judge_live_window(scenario, samples, target=window.get("soc") if args.target is not None and window["enable"] else None)
        print("\n{}: {} - {}".format(verdict, scenario, reason))
        await _cli_restore(client, sn, settings, list(payload), path, args)
    print("Done")


async def restore_hanchu_snapshot(args):  # pragma: no cover
    """Write a saved snapshot back, to undo a test that did not get to restore itself."""
    try:
        serial, snapshot = load_cli_snapshot(args.restore)
    except ValueError as error:
        print(error)
        return
    args.serial = args.serial or serial
    if args.serial.lower() != serial.lower():
        print("The snapshot is for {}, not {} - refusing".format(serial, args.serial))
        return
    client = _build_hanchu_cli(args)
    if not await client.ensure_token():
        print("LOGIN FAILED{}".format(": {}".format(client.last_api_error) if client.last_api_error else ""))
        return
    await client.refresh_static()
    if serial not in client.device_list:
        print("{} is not on this account (found: {})".format(serial, ", ".join(client.device_list) or "nothing"))
        return
    payload, missing = restore_payload(snapshot, HANCHU_CLI_RESTORE_KEYS)
    print("Restoring {} from {}:\n{}".format(serial, args.restore, json.dumps(payload, indent=2)))
    if not _cli_confirm("Write these settings back to {}? [y/N] ".format(serial), args.yes):
        return
    await _cli_restore(client, serial, snapshot, HANCHU_CLI_RESTORE_KEYS, args.restore, args)
    print("Done")


def main():  # pragma: no cover
    """Command-line entry point: Hanchu diagnostics and live-hardware tests."""
    import argparse
    import getpass

    parser = argparse.ArgumentParser(
        description="Hanchu ESS cloud diagnostics and live-hardware tests. With no mode option it is read-only.",
        epilog="Run from apps/predbat. The password can also come from HANCHU_PASSWORD or --config; otherwise you are prompted for it.",
    )
    parser.add_argument("--account", help="The e-mail address you sign into the Hanchu app with")
    parser.add_argument("--password", help="The Hanchu app password (prefer HANCHU_PASSWORD or the prompt, which keep it out of shell history)")
    parser.add_argument("--serial", default=None, help="Drive this DTU serial when the account has several")
    parser.add_argument("--config", help="Read hanchu_account, hanchu_password and hanchu_inverter_sn from an apps.yaml, resolving !secret as Predbat does")
    parser.add_argument("--api-delay", type=float, default=1, help="Seconds to wait between API calls (default 1)")
    parser.add_argument("--no-trace", action="store_true", help="Stop printing every API request and response")
    parser.add_argument("--dump-menu", metavar="FILE", help="With the read-only report, save the device's full settings menu as JSON to FILE")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write-test", action="store_true", help="Write a charge window three hours from now, read it back, then restore - moves no energy")
    mode.add_argument("--live-window", choices=HANCHU_CLI_SCENARIOS, help="Run this Predbat state on the inverter now, watch the battery, then restore")
    mode.add_argument("--restore", metavar="SNAPSHOT", help="Write back a snapshot file a test saved")
    parser.add_argument("--minutes", type=int, default=10, help="Length of the test window in minutes (default 10)")
    parser.add_argument("--power", type=int, default=1000, help="Charge or export power for the test in watts (default 1000)")
    parser.add_argument("--target", type=int, default=None, help="Charge target or export floor in percent for --live-window charge/export (default: 10 points from the current SoC). Set it just past the current SoC to watch the inverter stop at it")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between readings during --live-window (default 30)")
    parser.add_argument("--user-defined", action="store_true", help="With a freeze test, also set the User-defined work mode, as an earlier Predbat window would have")
    parser.add_argument("--snapshot-dir", default=".", help="Where to save the pre-write snapshot (default: the current directory)")
    parser.add_argument("--yes", action="store_true", help="Do not ask before writing")
    args = parser.parse_args()

    if args.config:
        import yaml
        from utils import load_apps_yaml

        try:
            config, _ = load_apps_yaml(args.config)
        except (yaml.YAMLError, KeyError, OSError, ValueError) as error:
            parser.error("could not read Hanchu credentials from {}: {}".format(args.config, error))
        merged, used = merge_hanchu_credentials({"account": args.account, "password": args.password, "serial": args.serial}, config or {})
        args.account, args.password, args.serial = merged["account"], merged["password"], merged["serial"]
        print("Read from {}: {}".format(args.config, ", ".join(used) if used else "nothing - no hanchu_ keys found"))
    if not args.account:
        parser.error("no account: pass --account, or --config pointing at an apps.yaml holding hanchu_account")
    if not args.password:
        args.password = os.environ.get("HANCHU_PASSWORD")
    if not args.password:
        try:
            args.password = getpass.getpass("Hanchu password for {}: ".format(args.account))
        except (EOFError, KeyboardInterrupt):
            parser.error("no password: pass --password, set HANCHU_PASSWORD, or use --config")
    if args.minutes < 2:
        parser.error("--minutes must be at least 2")

    if args.write_test:
        asyncio.run(_cli_main(test_hanchu_write_readback, args))
    elif args.live_window:
        asyncio.run(_cli_main(test_hanchu_live_window, args))
    elif args.restore:
        asyncio.run(_cli_main(restore_hanchu_snapshot, args))
    else:
        asyncio.run(_cli_main(test_hanchu_api, args))


if __name__ == "__main__":  # pragma: no cover
    main()
