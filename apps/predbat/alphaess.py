# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# AlphaESS Open API Library
# -----------------------------------------------------------------------------

"""AlphaESS Open API integration for Predbat.

Registers each discovered AlphaESS system as an ``AlphaESSCloud`` Predbat inverter,
publishing monitoring sensors and schedule control entities. Predbat's controls map
straight onto the AlphaESS schedule fields - gridCharge/timeChaf/batHighCap for charging
and ctrDis/timeDisf/batUseCap for export - and the inverter does the timing, so there is
no per-instant work mode to derive.

Auth is stateless: every request carries appId, timeStamp and
sign = sha512(appId + appSecret + timeStamp). There is no token and no refresh, so the
self-hosted add-on and the Predbat.com SaaS path share one code path.
"""

import asyncio
import hashlib
import json
import time
import aiohttp
from datetime import datetime
from component_base import ComponentBase
from alphaess_const import (
    ALPHAESS_BASE_URL,
    ALPHAESS_ENDPOINTS,
    ALPHAESS_RETURN_CODES,
    ALPHAESS_CODE_OK,
    ALPHAESS_CODE_ALREADY_BOUND,
    ALPHAESS_CODE_NOT_BOUND,
    ALPHAESS_CODE_TIMESTAMP,
    ALPHAESS_CODE_TOO_FAST,
    ALPHAESS_CODE_NO_PERMISSION,
    ALPHAESS_CODE_OFFLINE,
    ALPHAESS_CODE_SIGN,
    ALPHAESS_SIGN_WINDOW_SECONDS,
    ALPHAESS_DEBUG_REDACT_KEYS,
    ALPHAESS_DEBUG_REDACT_KEYS_RESPONSE,
    ALPHAESS_RETRIES,
    ALPHAESS_TIMEOUT,
    ALPHAESS_TELEMETRY,
    ALPHAESS_TELEMETRY_NEGATE,
    ALPHAESS_HISTORY,
    ALPHAESS_EV_DETAIL,
    ALPHAESS_HISTORY_FEED_IN,
    ALPHAESS_HISTORY_GRID_CHARGE,
    ALPHAESS_LIVE_FAIL_LIMIT,
    ALPHAESS_TTL_POWER,
    ALPHAESS_TTL_POWER_DEMOTED,
    ALPHAESS_ENERGY,
    ALPHAESS_ENERGY_LOAD_FIELD,
    ALPHAESS_AC_COUPLED_MODELS,
    snap_time_grid,
    hhmmss_to_hhmm,
    hm_to_minutes,
    window_is_empty,
    ALPHAESS_TIME_DISABLED,
    ALPHAESS_TIME_MAX,
    ALPHAESS_SETTLE_POLLS,
    ALPHAESS_STORAGE_MODULE,
    ALPHAESS_CACHE_STATIC,
    ALPHAESS_CACHE_CONFIG,
    ALPHAESS_CACHE_RATINGS,
    ALPHAESS_CACHE_CONTROL,
    ALPHAESS_TTL_STATIC,
    ALPHAESS_TTL_CONFIG,
    ALPHAESS_TTL_ENERGY,
)


class AlphaESSAPI(ComponentBase):
    """AlphaESS Open API cloud component."""

    # Trace every API request/response while the AlphaESS integration beds in. Nobody on
    # the project has an AlphaESS account, so a tester's log is the only evidence available
    # for the inferred behaviour; flip to False once it is confirmed.
    api_debug = True

    def initialize(
        self,
        app_id="",
        app_secret="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=True,
        battery_rate_max=None,
        api_delay=2,
        min_write_interval=300,
        **kwargs,
    ):
        """Initialise the AlphaESS component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has
        already resolved each arg from its alphaess_* config key and passes it BY ARG NAME
        (e.g. app_id <- alphaess_app_id), exactly like fox/deye/sunsynk. Consume the kwargs
        directly - do NOT re-derive with get_arg("app_id"): that bare name is not in
        apps.yaml (the key is alphaess_app_id), so it would always return the default.
        """
        self.log("Info: AlphaESSAPI initialising")
        self.app_id = app_id or ""
        self.app_secret = app_secret or ""
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_rate_max_override = float(battery_rate_max) if battery_rate_max else 0.0
        self.api_delay = max(0, float(api_delay or 0))
        self.min_write_interval = max(0, int(min_write_interval or 0))
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_energy = {}
        self.device_config = {}
        self.local_schedule = {}
        self.applied_payload = {}
        self.last_write_time = {}
        self.settle_count = {}
        # Serials Predbat has actually been asked to drive, i.e. ones whose write button has
        # been pressed at least once. The reconcile loop only re-applies for these, so a
        # startup cycle can never clobber an inverter before there is a plan to apply.
        self.control_active = set()
        # Per-serial verdicts, all cached to disk so a restart does not re-learn them.
        self._periodic_ok = {}
        self._live_ok = {}
        self._live_fail_count = {}
        # Per-serial "is an EV charger physically fitted" verdict: True/False, or absent when
        # never seen. Cached like _live_ok because it is a hardware fact the history path
        # cannot observe - getOneDayPowerBySn carries a charger POWER but no fitted/absent
        # signal, so a serial that starts life demoted would otherwise never learn it.
        self._ev_present = {}
        self._unbind_done = set()
        self._tier_refreshed = {}
        self._cache_restored = False
        self._restore_had_error = False
        # The most recent body-level API failure message (msg/expMsg only - never a
        # credential), and whether the last discovery attempt actually reached the API. Both
        # exist so the standalone CLI can name precisely which stage failed.
        self.last_api_error = ""
        self.discovery_ok = None
        if not self.control_enable:
            self.log("Info: AlphaESS control is disabled (alphaess_control_enable is false); monitoring only")

    def _headers(self):
        """Return the signed headers every AlphaESS request carries.

        sign is sha512(appId + appSecret + timeStamp) as lower-case hex. Both `timeStamp`
        (documented) and `timestamp` (what the reference client sends) are included, since
        the API accepts either and it costs nothing to send both.
        """
        timestamp = str(int(time.time()))
        raw = "{}{}{}".format(self.app_id, self.app_secret, timestamp)
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "appId": self.app_id,
            "timeStamp": timestamp,
            "timestamp": timestamp,
            "sign": hashlib.sha512(raw.encode("ascii")).hexdigest(),
        }

    @staticmethod
    def redact(payload, direction="request"):
        """Return a log-safe copy of a payload with secrets redacted.

        Requests mask the full secret set (appSecret, sign, app_secret, code, checkCode).
        Responses mask only genuine secrets and never mask code, msg, info or expMsg,
        which are essential for diagnostics when nobody on the project has AlphaESS hardware.
        """
        if not isinstance(payload, dict):
            return payload
        redact_keys = ALPHAESS_DEBUG_REDACT_KEYS if direction == "request" else ALPHAESS_DEBUG_REDACT_KEYS_RESPONSE
        return {key: ("***" if key in redact_keys else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response while api_debug is on."""
        if not self.api_debug:
            return
        if payload is None:
            self.log("Info: AlphaESS API {} {}".format(direction, what))
            return
        try:
            rendered = json.dumps(self.redact(payload, direction), default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(payload)[:2000]
        self.log("Info: AlphaESS API {} {} {}".format(direction, what, rendered))

    def describe_code(self, code):
        """Return 'code (description)' for a return code, or just the code when unknown."""
        description = ALPHAESS_RETURN_CODES.get(code)
        return "{} ({})".format(code, description) if description else str(code)

    def _note_failure(self, code, body, path):
        """Record and log one API-level failure, without ever logging a credential.

        expMsg is the only field that names a bad parameter - msg just says
        "Parameter error" - so it is always included when present. 6006 is called out as a
        host clock problem because it otherwise looks exactly like a bad AppSecret.
        """
        msg = body.get("msg") or body.get("info") or ""
        exp_msg = body.get("expMsg") or ""
        detail = "{}{}".format(msg, " - {}".format(exp_msg) if exp_msg else "")
        self.last_api_error = detail or self.describe_code(code)
        if code == ALPHAESS_CODE_TIMESTAMP:
            self.log(
                "Warn: AlphaESS rejected the request timestamp ({}) - this host's clock is more than {} seconds from AlphaESS server time, it is a clock problem and not a credentials problem".format(self.describe_code(code), ALPHAESS_SIGN_WINDOW_SECONDS)
            )
            return
        if code in ALPHAESS_CODE_SIGN:
            self.log("Warn: AlphaESS rejected the request signature ({}) - check alphaess_app_id and alphaess_app_secret".format(self.describe_code(code)))
            return
        if code == ALPHAESS_CODE_TOO_FAST:
            # A pacing signal, not a component fault: both write endpoints are documented as
            # writable once per 24 hours, so a busy response here is expected under load.
            # Logging it at Warn would contradict that and read as a genuine malfunction.
            self.log("Info: AlphaESS {} was rate-limited ({}); this is a pacing signal, not a fault, and will be retried after the write pacing interval".format(path, self.describe_code(code)))
            return
        if code == ALPHAESS_CODE_OFFLINE:
            # Routine and transient - a system that has simply dropped off the cloud, not a
            # component fault. fetch_device_data already treats a getLastPowerData failure
            # (6042 is common there) as the normal trigger to fall back to the history path.
            self.log("Info: AlphaESS {} reported the system offline ({}); this is routine and not treated as a failure".format(path, self.describe_code(code)))
            return
        self.log("Warn: AlphaESS {} returned {} {}".format(path, self.describe_code(code), detail))

    async def _request(self, method, endpoint_key, params=None, body=None):
        """Perform one signed API call, returning (code, data).

        Two failure modes are kept apart deliberately. An API-level failure returns the
        envelope's own code, because the write endpoints answer data:null whether they
        succeeded or not and the code is the only way to tell - that is the whole reason
        this component has its own client rather than using the PyPI package. A transport
        failure returns -1, since it affects every endpoint and says nothing about the
        request itself.
        """
        path = ALPHAESS_ENDPOINTS[endpoint_key]
        url = "{}{}".format(ALPHAESS_BASE_URL, path)
        self.debug_api("request", "{} {}".format(method, path), body if body is not None else params)
        for attempt in range(ALPHAESS_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=ALPHAESS_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    call = session.post(url, headers=self._headers(), json=body or {}) if method == "POST" else session.get(url, headers=self._headers(), params=params or {})
                    async with call as response:
                        if response.status != 200:
                            self.log("Warn: AlphaESS {} returned HTTP {}".format(path, response.status))
                            return -1, None
                        data = await response.json()
            except Exception as error:
                if attempt + 1 >= ALPHAESS_RETRIES:
                    self.log("Warn: AlphaESS {} transport failure: {}".format(path, error))
                    return -1, None
                await asyncio.sleep(1 + attempt)
                continue

            if not isinstance(data, dict):
                self.log("Warn: AlphaESS {} returned a non-object body".format(path))
                return -1, None
            self.debug_api("response", path, data)
            code = data.get("code")
            # The periodic endpoints report status in `info` where the rest use `msg`.
            if code == ALPHAESS_CODE_OK or data.get("msg") == "Success" or data.get("info") == "Success":
                return ALPHAESS_CODE_OK, data.get("data")
            self._note_failure(code, data, path)
            return code, None
        return -1, None

    async def _get(self, endpoint_key, params=None):
        """Perform a signed GET, returning (code, data)."""
        return await self._request("GET", endpoint_key, params=params)

    async def _post(self, endpoint_key, body=None):
        """Perform a signed POST, returning (code, data)."""
        return await self._request("POST", endpoint_key, body=body)

    @staticmethod
    def _as_float(value, default=0.0):
        """Coerce an API value to float, returning default for None/'unknown'/junk."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def has_battery(detail):
        """Return True when a discovered system actually has a battery.

        AlphaESS also sells plug-in solar (the VT1000 family), which has nothing for
        Predbat to drive. This is a capability check rather than a model blacklist so it
        catches every non-battery product AlphaESS ships now or later without anyone
        maintaining a table.
        """
        try:
            return float(detail.get("cobat") or 0) > 0
        except (TypeError, ValueError):
            return False

    async def get_device_list(self):
        """Discover every battery system bound to the AppID, returning its serials.

        Sets discovery_ok so an empty account can be told apart from a failed call - the
        two are indistinguishable from the returned list alone, and the CLI has to name
        which one happened.
        """
        code, data = await self._get("ess_list")
        if code != ALPHAESS_CODE_OK:
            self.discovery_ok = False
            return list(self.device_list)
        self.discovery_ok = True
        wanted = [str(sn).lower() for sn in self.inverter_sn_filter]
        serials = []
        detail = {}
        for entry in data or []:
            sn = entry.get("sysSn")
            if not sn:
                continue
            if wanted and str(sn).lower() not in wanted:
                continue
            if not self.has_battery(entry):
                # Logged by serial and model so it reads as recognised-and-passed-over
                # rather than silently missing. Said even for an explicitly requested
                # serial: there is no plan to apply to a system with no battery, and a
                # filter matching nothing otherwise looks identical to an empty account.
                self.log("Info: AlphaESS skipping {} (model {}) - it reports no battery capacity, so there is nothing for Predbat to control".format(sn, entry.get("minv", "unknown")))
                continue
            serials.append(sn)
            detail[sn] = entry
        self.device_list = serials
        self.device_detail = detail
        return serials

    def battery_capacity(self, sn):
        """Return the battery capacity in kWh from getEssList's cobat."""
        return self._as_float(self.device_detail.get(sn, {}).get("cobat"), 0.0)

    def inverter_limit(self, sn):
        """Return the inverter's nominal AC power in watts, from getEssList's poinv (kW)."""
        return self._as_float(self.device_detail.get(sn, {}).get("poinv"), 0.0) * 1000.0

    def battery_rate_max(self, sn):
        """Return the battery charge/discharge power limit in watts.

        The API reports no battery power limit and no pack current or voltage to derive one
        from, so this falls back to the inverter's nominal AC power. That is deliberate
        rather than lazy: leaving battery_rate_max unmapped is NOT neutral, because
        inverter.py:410 then uses a hard-coded 2600W - roughly half a SMILE5's real rate -
        silently, on every plan. inverter.py:423 makes battery_rate_max the governing term
        in min(inverter_limit_charge, battery_rate_max_raw), so mapping inverter_limit
        alone does not rescue it.

        On a matched AlphaESS package poinv is close to the battery rate. Where it is not,
        the estimate is high, and that is the safer error: inverter.py measures the achieved
        rate and logs a battery_rate_max_scaling suggestion, so an over-estimate reports
        itself while the 2600W default never does.
        """
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        return self.inverter_limit(sn)

    async def refresh_static(self):
        """Re-discover systems and refresh their static detail. True when discovery worked.

        Branches on discovery_ok rather than on list emptiness: both a failed call and an
        empty account produce an empty list but require different handling. Deliberately
        does NOT assign an empty discovery result over a working device_list.
        This tier re-runs every 8 hours in a long-lived process, so one transient failure
        must not take a working component down until the next success - and assigning the
        empty result would additionally write {'device_list': []} to the cache and stamp it
        fresh, so a restart would restore nothing and skip re-discovery for a full TTL.
        Absence of a result is not a result.
        """
        previous = list(self.device_list)
        serials = await self.get_device_list()
        if self.discovery_ok is False:
            # The call itself failed. Preserve the working list and do not advance the tier clock.
            self.device_list = previous
            if previous:
                self.log("Warn: AlphaESS discovery failed; keeping the {} previously known inverter(s)".format(len(previous)))
            else:
                self.log("Warn: AlphaESS inverter discovery failed; the account may still have systems")
            return False
        if not serials:
            # Discovery succeeded but returned nothing (empty account or filter matched nothing).
            if self.inverter_sn_filter:
                self.log("Warn: AlphaESS discovery succeeded but the configured serial filter {} matched nothing in this account".format(self.inverter_sn_filter))
            else:
                self.log("Warn: AlphaESS this account has no battery systems bound to it")
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

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Start a tier's clock. Called ONLY on a successful refresh.

        Marking unconditionally would defeat the first-cycle checks in run(): a retry after
        a deferred startup would find the tier "fresh", skip the poll entirely and then run
        automatic_config() with no data after all - the very thing those checks exist to
        prevent.
        """
        self._tier_refreshed[tier] = time.time() - (age_minutes * 60)

    def _history_query_date(self):
        """Return today's date as yyyy-MM-dd in the user's timezone, for the day endpoints."""
        return datetime.now(self.local_tz).strftime("%Y-%m-%d")

    def power_tier_ttl(self):
        """Return the power tier interval, longer once any serial is on the history path.

        getOneDayPowerBySn returns ~288 records for a full day, so it must not sit on a
        60-second loop. Five minutes is the resolution the history actually has anyway.
        """
        if any(ok is False for ok in self._live_ok.values()):
            return ALPHAESS_TTL_POWER_DEMOTED
        return ALPHAESS_TTL_POWER

    def _apply_live_payload(self, sn, payload):
        """Map a getLastPowerData object into device_values, or return False without a SOC."""
        if not isinstance(payload, dict) or payload.get("soc") is None:
            return False
        values = {}
        for leaf, field in ALPHAESS_TELEMETRY.items():
            value = self._as_float(payload.get(field), 0.0)
            if leaf in ALPHAESS_TELEMETRY_NEGATE:
                # pgrid is positive on IMPORT; Predbat's convention is negative on import.
                # pbat needs no negation - the API's own live sample balances as
                # pgrid + pbat = pload, so a positive pbat is already discharge.
                value = -value
            values[leaf] = value
        # AlphaESS uses null-for-absent in these detail objects: the API docs state
        # pevDetail values are "null when no charger is fitted". A unit with no DC strings
        # should therefore report nulls, while a hybrid at NIGHT reports zeros. Null versus
        # zero is the discriminator, and unlike a PV-power threshold it works at any hour.
        # Applying the pevDetail convention to ppvDetail is inference - VERIFY@FIELD.
        pv_detail = payload.get("ppvDetail") or {}
        strings = [pv_detail.get("ppv{}".format(index)) for index in range(1, 5)]
        values["ppv_detail_all_null"] = bool(pv_detail) and all(value is None for value in strings)
        # Same null-for-absent convention, but here it is DOCUMENTED rather than inferred:
        # a non-null ev*Power means a charger is fitted. Only ever set to True, never back to
        # False from a later poll, because pevDetail can legitimately be absent from a
        # response and losing the verdict would unmap car_charging_energy mid-session.
        ev_detail = payload.get("pevDetail") or {}
        if ev_detail and any(ev_detail.get(key) is not None for key in ALPHAESS_EV_DETAIL):
            if not self._ev_present.get(sn):
                self.log("Info: AlphaESS {} reports an EV charger fitted; its ev_energy_today sensor is eligible for car_charging_energy".format(sn))
            self._ev_present[sn] = True
        elif ev_detail and sn not in self._ev_present:
            self._ev_present[sn] = False
        self.device_values[sn] = values
        return True

    def _apply_history_payload(self, sn, samples):
        """Map the most recent getOneDayPowerBySn sample into device_values.

        Returns False when the history carries no SOC, which is the only thing that makes
        a serial undriveable - everything else can be defaulted or derived.
        """
        if not isinstance(samples, list) or not samples:
            return False
        sample = samples[-1]
        soc = None
        for field in ALPHAESS_HISTORY["soc"]:
            # cbat is what the live API returns; the portal documents cobat and reading
            # that name silently yields None. Try both, cbat first.
            if sample.get(field) is not None:
                soc = self._as_float(sample.get(field), None)
                break
        if soc is None:
            return False
        feed_in = self._as_float(sample.get(ALPHAESS_HISTORY_FEED_IN), 0.0)
        grid_charge = self._as_float(sample.get(ALPHAESS_HISTORY_GRID_CHARGE), 0.0)
        pv_power = self._as_float(sample.get(ALPHAESS_HISTORY["pv_power"][0]), 0.0)
        load_power = self._as_float(sample.get(ALPHAESS_HISTORY["load_power"][0]), 0.0)
        ev_field = ALPHAESS_HISTORY["ev_power"][0]
        ev_power = self._as_float(sample.get(ev_field), 0.0) if sample.get(ev_field) is not None else None
        self.device_values[sn] = {
            "soc": soc,
            "pv_power": pv_power,
            "load_power": load_power,
            # The history has no signed grid field, so it is reconstructed from the two
            # positive-only fields and then negated for Predbat's convention.
            "grid_power": -(grid_charge - feed_in),
            # This endpoint has no pbat field either, so battery_power is derived from the
            # power balance ppv + pgrid + pbat = pload, using AlphaESS's own positive-on-
            # import pgrid convention (pgrid = gridCharge - feedIn, i.e. the negation of the
            # grid_power leaf above): pbat = pload - ppv - (gridCharge - feedIn). The design
            # spec's live-sample arithmetic (pgrid 11 + pbat 1264 = pload 1275 with ppv 0)
            # establishes that this is positive-on-discharge, matching ALPHAESS_TELEMETRY's
            # pbat pass-through, so the two paths agree on sign. Deriving it rather than
            # leaving it unmapped matters because automatic_config() maps battery_power
            # unconditionally for every inverter, including one that starts life on this
            # path (a restart landing straight on a demoted serial); an arg pointed at a
            # sensor that never appears is worse than an absent one, and would silently
            # disable find_battery_size and battery-curve learning for the whole session.
            "battery_power": load_power - pv_power - (grid_charge - feed_in),
        }
        # Only published when the sample actually carried it. A system with no charger has
        # no pchargingPile, and publishing a fabricated 0 W would look identical to a
        # charger that is simply idle.
        if ev_power is not None:
            self.device_values[sn]["ev_power"] = ev_power
        return True

    async def fetch_device_history(self, sn):
        """Populate device_values for one serial from today's power history."""
        code, data = await self._get("one_day_power", params={"sysSn": sn, "queryDate": self._history_query_date()})
        if code != ALPHAESS_CODE_OK:
            return False
        return self._apply_history_payload(sn, data)

    async def reprobe_live(self, sn):
        """Re-test getLastPowerData for a demoted serial, restoring it on success.

        Runs on the config tier, so a system that was merely offline or briefly failing
        climbs back to 60-second live data by itself, and a genuinely incapable one costs
        two extra calls an hour rather than one per minute.
        """
        code, data = await self._get("last_power", params={"sysSn": sn})
        if code == ALPHAESS_CODE_OK and self._apply_live_payload(sn, data):
            if self._live_ok.get(sn) is False:
                self.log("Info: AlphaESS {} is serving live power data again, returning it to the live telemetry path".format(sn))
            self._live_ok[sn] = True
            self._live_fail_count[sn] = 0
            return True
        return False

    async def fetch_device_data(self, sn):
        """Populate device_values for one serial, preferring live data over history.

        The rule is behavioural, not model-based: if live data is not present, use the
        history. That covers the models known not to serve getLastPowerData, any unlisted
        model with the same gap, and a system that has simply stopped answering - none of
        which a model list would catch.
        """
        if self._live_ok.get(sn) is not False:
            code, data = await self._get("last_power", params={"sysSn": sn})
            if code == ALPHAESS_CODE_OK and self._apply_live_payload(sn, data):
                self._live_ok[sn] = True
                self._live_fail_count[sn] = 0
                return True
            self._live_fail_count[sn] = self._live_fail_count.get(sn, 0) + 1
            if self._live_fail_count[sn] >= ALPHAESS_LIVE_FAIL_LIMIT:
                self._live_ok[sn] = False
                self.log(
                    "Info: AlphaESS {} has not served live power data {} times running; using the 5-minute history instead. It is re-probed every config refresh, so this reverses by itself if the system recovers.".format(sn, self._live_fail_count[sn])
                )

        if await self.fetch_device_history(sn):
            return True

        # No SOC on either path means Predbat cannot plan for this serial. Say which call
        # failed rather than registering it with a fabricated SOC.
        self.log("Warn: AlphaESS {} returned no usable soc from getLastPowerData and no cbat in its power history, so Predbat cannot drive it this cycle".format(sn))
        return False

    async def refresh_power(self):
        """Poll telemetry for every inverter, reporting whether anything came back.

        The tier clock is started only when a poll actually succeeded - see mark_refreshed.
        Also persists the live-vs-history verdict (the "ratings" cache) whenever a serial's
        _live_ok flips this cycle - a demotion or a self-heal - so a restart does not lose
        it and re-learn it from scratch. Not saved unconditionally every tick: this tier
        runs every 60 seconds, and most ticks change nothing.
        """
        got_any = False
        ratings_changed = False
        for sn in self.device_list:
            before = self._live_ok.get(sn)
            try:
                if await self.fetch_device_data(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS telemetry poll failed for {}: {}".format(sn, error))
            if self._live_ok.get(sn) != before:
                ratings_changed = True
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("power")
        if ratings_changed:
            await self.save_ratings()
        return got_any

    async def fetch_device_energy(self, sn):
        """Populate device_energy for one serial from today's energy totals.

        Two calls, because getOneDateEnergyBySn has no load field at all - load energy
        only exists on getSumDataForCustomer as eload. These counters reset at midnight;
        minute_data/clean_incrementing_reverse absorbs that.
        """
        code, data = await self._get("one_date_energy", params={"sysSn": sn, "queryDate": self._history_query_date()})
        if code != ALPHAESS_CODE_OK or not isinstance(data, dict):
            return False
        energy = {}
        for leaf, field in ALPHAESS_ENERGY.items():
            value = data.get(field)
            if value is not None:
                energy[leaf] = self._as_float(value, 0.0)

        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        sum_code, sum_data = await self._get("sum_data", params={"sysSn": sn})
        load_today = None
        if sum_code == ALPHAESS_CODE_OK and isinstance(sum_data, dict):
            raw_load = sum_data.get(ALPHAESS_ENERGY_LOAD_FIELD)
            if raw_load is not None:
                load_today = self._as_float(raw_load, None)
        if load_today is None:
            # The API docs warn that most SumData fields are null without a configured
            # tariff. Derive it rather than leaving load_today unmapped, which would cost
            # Predbat its load learning entirely.
            derived = self._as_float(data.get("epv"), 0.0) + self._as_float(data.get("eInput"), 0.0) - self._as_float(data.get("eOutput"), 0.0) - self._as_float(data.get("eCharge"), 0.0) + self._as_float(data.get("eDischarge"), 0.0)
            load_today = max(0.0, derived)
        energy["load_today"] = load_today
        self.device_energy[sn] = energy
        return True

    async def refresh_energy(self):
        """Poll the daily energy counters for every inverter."""
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_device_energy(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS energy poll failed for {}: {}".format(sn, error))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("energy")
        return got_any

    def _sensor_name(self, sn, leaf):
        """Return a namespaced AlphaESS sensor entity id."""
        return "sensor.{}_alphaess_{}_{}".format(self.prefix, sn.lower(), leaf)

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced AlphaESS control entity id."""
        return "{}.{}_alphaess_{}_{}".format(domain, self.prefix, sn.lower(), leaf)

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "ev_power": "W"}
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(
                        self._sensor_name(sn, leaf),
                        state=values[leaf],
                        attributes={"unit_of_measurement": unit, "friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())},
                        app="alphaess",
                    )

            # Ratings are published only when actually derivable - an arg pointing at a
            # sensor that never appears is worse than an absent arg the user can fill in.
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=round(capacity, 3), attributes={"unit_of_measurement": "kWh", "friendly_name": "AlphaESS {} Battery Capacity".format(sn)}, app="alphaess")
            limit = self.inverter_limit(sn)
            if limit > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=round(limit), attributes={"unit_of_measurement": "W", "friendly_name": "AlphaESS {} Inverter Limit".format(sn)}, app="alphaess")
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": "AlphaESS {} Battery Rate Max".format(sn)}, app="alphaess")

            detail = self.device_detail.get(sn, {})
            for leaf, field in (("inverter_model", "minv"), ("battery_model", "mbat"), ("ems_status", "emsStatus")):
                if detail.get(field) is not None:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=detail[field], attributes={"friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())}, app="alphaess")
            # Published but deliberately NOT mapped to soc_percent/soc_kw: the arithmetic in
            # the API docs' live samples fits both "current SOC" and "configured usable
            # depth" equally well, and this is an 8-hour tier anyway. Live SOC comes from
            # LastPower.soc where there is no ambiguity.
            for leaf, field, unit in (("pv_nominal", "popv", "kW"), ("usable_capacity", "usCapacity", "%"), ("surplus_capacity", "surplusCobat", "kWh")):
                if detail.get(field) is not None:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=detail[field], attributes={"unit_of_measurement": unit, "friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())}, app="alphaess")

            # Daily energy counters feed Predbat's load/import/export learning. They reset
            # at midnight; minute_data/clean_incrementing_reverse absorbs that.
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())},
                    app="alphaess",
                )

    def detect_ac_coupled(self, sn):
        """Return True (AC-coupled), False (hybrid) or None (undecided) for one serial.

        Two signals must AGREE before a verdict is returned, because the errors are not
        symmetric - see apply_hybrid_verdict.
        """
        detail = self.device_detail.get(sn, {})
        if str(detail.get("minv", "")) in ALPHAESS_AC_COUPLED_MODELS:
            return True
        values = self.device_values.get(sn, {})
        strings_absent = values.get("ppv_detail_all_null")
        if strings_absent is None:
            return None
        pv_nameplate = self._as_float(detail.get("popv"), 0.0)
        pv_energy = self._as_float(self.device_energy.get(sn, {}).get("pv_today"), 0.0)
        # Signal 2 needs daylight to mean anything: with no PV energy yet today, "popv is
        # zero" says nothing about where the PV is.
        nameplate_says_ac = pv_nameplate <= 0 and pv_energy > 0
        if strings_absent and nameplate_says_ac:
            return True
        if not strings_absent:
            return False
        return None

    async def apply_hybrid_verdict(self):
        """Move switch.predbat_inverter_hybrid only on positive evidence of AC coupling.

        inverter_hybrid is one of Predbat's OWN CONFIG_ITEMS switches rather than an
        apps.yaml arg, so it is written with set_state_external - writing the entity state
        alone would move the displayed switch without changing the value the planner reads.

        The two errors are NOT symmetric. inverter_hybrid False on an actually-hybrid
        system stops PV counting against inverter_limit, so Predbat plans charge-plus-PV
        beyond what the inverter can pass and the surplus is clipped with targets silently
        missed. True on an actually-AC-coupled system merely under-uses the battery.
        Predbat defaults to True and every mainstream AlphaESS unit is a hybrid, so the
        switch is only ever moved towards AC-coupled, and only on agreeing evidence.
        """
        if not self.device_list:
            return
        verdicts = [self.detect_ac_coupled(sn) for sn in self.device_list]
        entity = "switch.{}_inverter_hybrid".format(self.prefix)
        if verdicts and all(verdict is True for verdict in verdicts):
            models = ", ".join(str(self.device_detail.get(sn, {}).get("minv", "unknown")) for sn in self.device_list)
            self.log("Info: AlphaESS detected AC coupling (no DC strings reported, and PV energy with no PV nameplate) for model(s) {}; setting {} off".format(models, entity))
            await self.set_state_external(entity, False)
            return
        if any(verdict is None for verdict in verdicts):
            models = ", ".join(str(self.device_detail.get(sn, {}).get("minv", "unknown")) for sn in self.device_list)
            self.log("Info: AlphaESS could not determine hybrid versus AC coupling for model(s) {}; leaving {} at its current value. If this is an AC-coupled retrofit, turn that switch off by hand.".format(models, entity))

    async def automatic_config(self):
        """Register every discovered inverter as an AlphaESSCloud Predbat inverter."""
        devices = list(self.device_list)
        if not devices:
            self.log("Warn: AlphaESS automatic_config found no inverters")
            return
        self.set_arg_auto("inverter_type", ["AlphaESSCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))
        self.set_arg_auto("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg_auto("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg_auto("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        self.set_arg_auto("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])
        # Own the sign flags rather than leaving them to whatever else configured this
        # install. base.args is shared and NOT namespaced per inverter type, so a component
        # that legitimately inverts its own grid sensor - teslemetry sets grid_power_invert
        # True, fox does the same - leaves that key set for every inverter index, and an
        # AlphaESS inverter that never claims it inherits the flip. The published sensor is
        # then correct and inverter.py negates it again, so an export reads as an import.
        # All three are False because publish_data already emits Predbat's conventions.
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])

        # Only map an arg when EVERY inverter reports the underlying value.
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in devices):
                self.set_arg_auto(leaf, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log("Warn: AlphaESS not every inverter reports {}, it must be set manually in apps.yaml".format(leaf))

        # car_charging_energy is mapped only for serials whose pevDetail actually reported a
        # charger, not for every inverter: on a system with none, eChargingPile reads a
        # permanent 0.0 and pointing Predbat at it would look identical to a charger that is
        # never used. A list is correct here even though APPS_SCHEMA types this as a single
        # "sensor" - the validator accepts a list for that type (predbat.py) and
        # minute_data_import_export sums however many entities it is given, which is what a
        # household with two charger-equipped inverters needs. Mapping it is inert on its own:
        # Predbat only subtracts car energy from house load when car_charging_hold is on.
        ev_devices = [sn for sn in devices if self._ev_present.get(sn) and "ev_energy_today" in self.device_energy.get(sn, {})]
        if ev_devices:
            self.set_arg_auto("car_charging_energy", [self._sensor_name(sn, "ev_energy_today") for sn in ev_devices])
            self.log("Info: AlphaESS mapped car_charging_energy to the EV charger energy of {} inverter(s): {}. It only affects the plan once car_charging_hold is enabled.".format(len(ev_devices), ev_devices))
        else:
            self.log("Info: AlphaESS found no EV charger on any inverter, so car_charging_energy is left unset")

        if all(self.battery_capacity(sn) > 0 for sn in devices):
            self.set_arg_auto("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: AlphaESS no battery capacity available for every inverter, soc_max must be set manually in apps.yaml")
        if all(self.inverter_limit(sn) > 0 for sn in devices):
            self.set_arg_auto("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: AlphaESS no poinv reported, inverter_limit must be set manually in apps.yaml")
        if all(self.battery_rate_max(sn) > 0 for sn in devices):
            self.set_arg_auto("battery_rate_max", [self._sensor_name(sn, "battery_rate_max") for sn in devices])
        else:
            self.log("Warn: AlphaESS no battery rate available, battery_rate_max must be set manually in apps.yaml")
        # Deliberately NOT auto-mapped. poinv is the inverter rating, not the site's
        # grid-connection limit, and a G98/G99-capped site can sit far below it. Unlike
        # battery_rate_max, nothing measures and reports this error back, so a guess would
        # over-export silently. Predbat falls back to 99999W until the user sets it.
        self.log("Warn: AlphaESS does not report an export power limit; set export_limit in apps.yaml if your grid connection is capped below the inverter rating, otherwise Predbat will plan exports it cannot deliver")
        # battery_min_soc is deliberately NOT mapped: batUseCap is a field Predbat writes,
        # so reading it back as the floor would be circular.

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

        await self.apply_hybrid_verdict()

    @staticmethod
    def _empty_schedule():
        """Return a fresh, disabled schedule shape - the single source of truth for its defaults.

        Used where a schedule has to be seeded from nothing: a control event arriving for a
        serial local_schedule has not seen yet. Kept as one helper rather than a literal
        repeated at each call site, so adding or renaming a field cannot silently diverge
        between copies. run() deliberately does NOT seed from here - it reads the control
        entities instead, whose per-field defaults produce exactly this shape when nothing
        has been published yet, but which hold Predbat's live plan after a restart.
        """
        return {
            "reserve": 0,
            "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        }

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        # Deliberately NOT clamped to any floor. This entity is Predbat's control surface:
        # it writes a value then reads it back to confirm (write_and_poll_value), so
        # publishing anything other than what was written guarantees a mismatch and a retry
        # storm. Clamping happens at the API boundary in the payload builder.
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "AlphaESS {} Battery Schedule Reserve".format(sn), "icon": "mdi:gauge"},
            app="alphaess",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes
            # Predbat replace these entities with its own dummies (inverter.py, the
            # inv_charge_time_format != "HH:MM:SS" branch) and the window never arrives.
            self.dashboard_item(
                self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": "AlphaESS {} {} Start".format(sn, direction.title()), "icon": "mdi:clock-outline"},
                app="alphaess",
            )
            self.dashboard_item(
                self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": "AlphaESS {} {} End".format(sn, direction.title()), "icon": "mdi:clock-outline"},
                app="alphaess",
            )
            self.dashboard_item(
                self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)),
                state=int(window.get("soc", 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "AlphaESS {} {} SoC".format(sn, direction.title()), "icon": "mdi:gauge"},
                app="alphaess",
            )
            self.dashboard_item(
                self._control_name("number", sn, "battery_schedule_{}_power".format(direction)),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": "AlphaESS {} {} Power".format(sn, direction.title()), "icon": "mdi:flash"},
                app="alphaess",
            )
            self.dashboard_item(
                self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": "AlphaESS {} {} Enable".format(sn, direction.title()), "icon": "mdi:check-circle-outline"},
                app="alphaess",
            )
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": "AlphaESS {} Schedule Write".format(sn), "icon": "mdi:content-save"}, app="alphaess")
        self.dashboard_item(
            self._control_name("switch", sn, "unbind"),
            state="on" if sn in self._unbind_done else "off",
            attributes={"friendly_name": "AlphaESS {} Unbind (one-way - re-binding needs a code emailed to the system owner)".format(sn), "icon": "mdi:link-off"},
            app="alphaess",
        )

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape the payload builder consumes.

        Numeric casts route through _as_float so an entity legitimately reporting
        "unknown"/"unavailable" - for instance right after a HA restart, before Predbat
        republishes - falls back to 0 rather than raising and killing the control loop.
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

    def _sn_from_entity(self, entity_id, include_unbound=False):
        """Extract the serial from an AlphaESS entity id, or None if unresolvable.

        Entity ids are always {domain}.{prefix}_alphaess_{sn}_{leaf}, so the serial is
        always followed by "_". Matching sn + "_" rather than a bare prefix keeps
        prefix-colliding serials apart - an entity for AL701 must never route to AL70,
        which would send a control write to the wrong inverter.

        include_unbound also checks self._unbind_done as a fallback, reusing the same
        anchored match, so an already-unbound serial's OWN unbind switch keeps resolving
        after the serial leaves device_list - otherwise a genuine Home Assistant turn_off
        on that switch could never reach _handle_unbind_event, and the documented
        "turning it back off clears the latch" behaviour would be unreachable. Every
        other caller MUST leave this False: an unbound serial's API calls are refused, so
        resolving it for any entity other than its own unbind switch would let Predbat
        send a live control write (and _reconcile_control would then keep repeating it
        every cycle) to a system it has explicitly been told it no longer controls.
        """
        text = str(entity_id).lower()
        for sn in self.device_list:
            if "_alphaess_{}_".format(sn.lower()) in text:
                return sn
        if include_unbound:
            for sn in self._unbind_done:
                if "_alphaess_{}_".format(sn.lower()) in text:
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
        leaf = str(entity_id).split("_alphaess_{}_".format(sn.lower()), 1)[-1]
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
        """Handle a number entity change."""
        await self._handle_control_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle a switch entity service call."""
        await self._handle_control_event(entity_id, service)

    @staticmethod
    def _clamp_percent(value, low=0, high=100):
        """Clamp a SOC percentage to the API's accepted range."""
        try:
            return int(max(low, min(high, int(float(value)))))
        except (TypeError, ValueError):
            return low

    def split_window(self, start, end):
        """Split a window at midnight, returning ((start1, end1), (start2, end2)) in HH:mm.

        INVERTER_DEF sets can_span_midnight False because wrap-around behaviour is
        undocumented for timeChaf1/timeChae1, so Predbat splits rather than writing a
        window whose meaning is unknown. Period 2 exists for the remainder and for nothing
        else - it is the midnight split, not a state.
        """
        start_min = hm_to_minutes(hhmmss_to_hhmm(start))
        end_text = str(end or "")
        end_hours = end_text.split(":")[0] if ":" in end_text else "0"
        try:
            end_min = int(end_hours) * 60 + int(end_text.split(":")[1])
        except (TypeError, ValueError, IndexError):
            end_min = start_min
        if end_min <= 24 * 60:
            return (hhmmss_to_hhmm(start), hhmmss_to_hhmm(end)), (ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED)
        wrapped = end_min - 24 * 60
        second_end = "{:02d}:{:02d}".format(wrapped // 60, wrapped % 60)
        return (hhmmss_to_hhmm(start), ALPHAESS_TIME_MAX), (ALPHAESS_TIME_DISABLED, second_end)

    def _snapped_periods(self, sn, direction, start, end, enabled):
        """Return two snapped HH:mm period pairs, disabling anything with no usable time.

        Times snap INWARD (start up, end down) so a snapped window is never wider than the
        one Predbat asked for. If snapping collapses or inverts a window it is written as
        disabled rather than as a wrap-around, and the decision is logged - a silently
        ignored off-grid window would look written and never run.
        """
        if not enabled:
            return (ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED), (ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED)
        (raw_start1, raw_end1), (raw_start2, raw_end2) = self.split_window(start, end)
        periods = []
        for index, (raw_start, raw_end) in enumerate(((raw_start1, raw_end1), (raw_start2, raw_end2)), start=1):
            if raw_start == ALPHAESS_TIME_DISABLED and raw_end == ALPHAESS_TIME_DISABLED:
                periods.append((ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED))
                continue
            snapped_start = snap_time_grid(raw_start, "start")
            snapped_end = snap_time_grid(raw_end, "end")
            if window_is_empty(snapped_start, snapped_end):
                if index == 1:
                    self.log("Info: AlphaESS {} {} window {}-{} collapsed to nothing on the API's 15-minute grid, so it is written as disabled rather than as an undocumented wrap-around".format(sn, direction, raw_start, raw_end))
                periods.append((ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED))
                continue
            periods.append((snapped_start, snapped_end))
        return periods[0], periods[1]

    def build_charge_payload(self, sn, schedule):
        """Build the full updateChargeConfigInfo body for one inverter.

        A FULL REPLACEMENT, not a patch: all seven fields are always present, because the
        endpoint silently resets anything omitted.
        """
        window = schedule.get("charge", {}) or {}
        enabled = bool(window.get("enable"))
        # charge_rate zero is Predbat signalling freeze charge / no cross-charging. There
        # is no pause endpoint, so a zeroed rate is the only signal available and it
        # overrides the window outright.
        rate = self._as_float(window.get("power"), 0.0)
        if enabled and rate <= 0:
            enabled = False
        (start1, end1), (start2, end2) = self._snapped_periods(sn, "charge", window.get("start"), window.get("end"), enabled)
        # Period 1 collapsing under snapping does not disable the payload on its own - period
        # 2 carries the midnight-split remainder and can be a valid window in its own right.
        # Only disable the payload when BOTH periods have nothing left to write, otherwise a
        # midnight-crossing charge window whose period 1 collapses would silently stop
        # charging even though its period 2 is still a real window.
        period1_empty = start1 == ALPHAESS_TIME_DISABLED and end1 == ALPHAESS_TIME_DISABLED
        period2_empty = start2 == ALPHAESS_TIME_DISABLED and end2 == ALPHAESS_TIME_DISABLED
        if period1_empty and period2_empty:
            enabled = False
        return {
            "sysSn": sn,
            "gridCharge": 1 if enabled else 0,
            "timeChaf1": start1,
            "timeChae1": end1,
            "timeChaf2": start2,
            "timeChae2": end2,
            "batHighCap": self._clamp_percent(window.get("soc", 100) if enabled else 100),
        }

    def build_discharge_payload(self, sn, schedule):
        """Build the full updateDisChargeConfigInfo body for one inverter.

        batUseCap serves two Predbat concepts because the API has only one field for the
        discharge floor: it is the export target while an export window is programmed and
        the reserve otherwise.
        """
        window = schedule.get("export", {}) or {}
        enabled = bool(window.get("enable"))
        reserve = self._clamp_percent(schedule.get("reserve", 0))
        export_rate = self._as_float(window.get("power"), 0.0)

        # discharge_rate zero means hold SOC (freeze export): discharge time control ON
        # with no permitted period, so the battery cannot discharge at all. This does NOT
        # depend on the charge rate - Predbat reaches discharge_rate == 0 together with
        # charge_rate == 0 through ordinary, reachable combinations (set_freeze_export_during_demand
        # zeroing the charge rate while a car-charging-from-battery hold or
        # iboost_prevent_discharge zeroes the discharge rate in the same pass), and that
        # combination is an explicit hold, not an absence of a plan. Treating it as "no plan"
        # would let the battery discharge into the EV or iBoost load against the hold Predbat
        # asked for.
        if export_rate <= 0:
            return {
                "sysSn": sn,
                "ctrDis": 1,
                "timeDisf1": ALPHAESS_TIME_DISABLED,
                "timeDise1": ALPHAESS_TIME_DISABLED,
                "timeDisf2": ALPHAESS_TIME_DISABLED,
                "timeDise2": ALPHAESS_TIME_DISABLED,
                "batUseCap": reserve,
            }

        (start1, end1), (start2, end2) = self._snapped_periods(sn, "export", window.get("start"), window.get("end"), enabled)
        # Same rule as build_charge_payload: only disable the payload when BOTH periods have
        # collapsed, so a period-1 collapse never throws away a genuine period-2 remainder and
        # costs a missed peak-rate export.
        period1_empty = start1 == ALPHAESS_TIME_DISABLED and end1 == ALPHAESS_TIME_DISABLED
        period2_empty = start2 == ALPHAESS_TIME_DISABLED and end2 == ALPHAESS_TIME_DISABLED
        if period1_empty and period2_empty:
            enabled = False
        return {
            "sysSn": sn,
            # With no export window, discharge time control is OFF: that is demand mode,
            # where the battery covers the house normally down to batUseCap.
            "ctrDis": 1 if enabled else 0,
            "timeDisf1": start1,
            "timeDise1": end1,
            "timeDisf2": start2,
            "timeDise2": end2,
            "batUseCap": self._clamp_percent(window.get("soc", reserve)) if enabled else reserve,
        }

    @staticmethod
    def payloads_equal(first, second):
        """Return True when two payloads are byte-identical, ignoring key order."""
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        return json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)

    @staticmethod
    def _owned_fields(direction):
        """Return the payload fields Predbat writes for one direction.

        Only these count as interference: a field Predbat never writes changing is the
        user configuring their own inverter, not something overwriting Predbat's plan.
        """
        if direction == "charge":
            return ("gridCharge", "timeChaf1", "timeChae1", "timeChaf2", "timeChae2", "batHighCap")
        if direction == "periodic":
            return ("gridChargeCycle", "ctrDisCycle", "chargeTimeList", "dischargeTimeList")
        return ("ctrDis", "timeDisf1", "timeDise1", "timeDisf2", "timeDise2", "batUseCap")

    # chargeTimeList/dischargeTimeList entries carry fields Predbat never writes (the API
    # may echo more than it was sent), so only these are compared - see
    # _normalize_periodic_entry.
    _PERIODIC_LIST_FIELDS = ("chargeTimeList", "dischargeTimeList")

    @staticmethod
    def _normalize_periodic_entry(entry):
        """Reduce one chargeTimeList/dischargeTimeList element to Predbat's own sub-fields,
        normalised so a semantically-identical server echo cannot read as interference.

        chargeLimit is compared as a float, because a server may legitimately answer 100.0
        where Predbat wrote the int 100 - the value is unchanged. chargePower is treated as
        ABSENT whenever it is zero, missing or unparseable, because _periodic_entry only
        ever adds the key when Predbat's own rate is > 0; a server that echoes back
        chargePower: 0 on the idle (disabled) entry - near-certain for any implementation
        that stores a power per period - must not be mistaken for someone setting a real
        power Predbat never asked for. Any other field the server happens to add (or key
        order, which a plain dict already ignores) is not looked at, because Predbat did
        not write it and cannot know what it should be.
        """
        if not isinstance(entry, dict):
            return None
        try:
            limit = float(entry.get("chargeLimit"))
        except (TypeError, ValueError):
            limit = None
        try:
            power = float(entry.get("chargePower"))
        except (TypeError, ValueError):
            power = 0.0
        return {
            "beginTime": entry.get("beginTime"),
            "endTime": entry.get("endTime"),
            "chargeLimit": limit,
            "chargePower": power if power > 0 else None,
        }

    def _periodic_list_differs(self, observed_value, applied_value):
        """Return True when a chargeTimeList/dischargeTimeList genuinely changed.

        Structural, not byte-for-byte: compares the normalised entries (see
        _normalize_periodic_entry) rather than str()-ing the raw lists, which would flag a
        semantically-identical server echo as interference over nothing but key order, an
        extra chargePower: 0, or an int-vs-float chargeLimit.
        """
        observed_list = observed_value if isinstance(observed_value, list) else []
        applied_list = applied_value if isinstance(applied_value, list) else []
        return [self._normalize_periodic_entry(entry) for entry in observed_list] != [self._normalize_periodic_entry(entry) for entry in applied_list]

    def _field_differs(self, direction, field, observed_value, applied_value):
        """Return True when one owned field's observed value differs from what Predbat wrote.

        Every field except the periodic time lists is a scalar (an HH:mm string, a 0/1
        flag, or a plain integer) and is compared as a string - that already catches every
        real change without needing type-aware handling. The two periodic list fields carry
        nested dicts the server is free to reformat or extend, so they get the structural
        comparison instead - see _periodic_list_differs.
        """
        if direction == "periodic" and field in self._PERIODIC_LIST_FIELDS:
            return self._periodic_list_differs(observed_value, applied_value)
        return str(observed_value) != str(applied_value)

    def note_external_change(self, sn, direction, observed):
        """Report when a Predbat-owned field no longer matches what Predbat last wrote.

        Suppressed for the first ALPHAESS_SETTLE_POLLS reads after a write, because
        settings only reach the inverter on its next cloud poll - typically one to five
        minutes later - so an immediate read-back legitimately shows the old values.
        Without that window every single write would raise a false alarm.

        Does nothing before Predbat has written anything for this serial and direction:
        with no recorded intent there is nothing to have been overwritten, and what the
        inverter reports is simply the user's own configuration.
        """
        applied = self.applied_payload.get(sn, {}).get(direction)
        if not applied or not isinstance(observed, dict):
            return
        key = (sn, direction)
        polls = self.settle_count.get(key, 0) + 1
        self.settle_count[key] = polls
        if polls <= ALPHAESS_SETTLE_POLLS:
            return
        differing = [field for field in self._owned_fields(direction) if field in observed and self._field_differs(direction, field, observed.get(field), applied.get(field))]
        if not differing:
            return
        detail = ", ".join("{}={} (Predbat wrote {})".format(field, observed.get(field), applied.get(field)) for field in differing)
        self.log(
            "Warn: AlphaESS {} {} settings no longer match what Predbat wrote: {}. The AlphaESS app, another Predbat instance, or the installer portal may have changed them - these endpoints are whole-object replacements, so the last writer wins.".format(
                sn, direction, detail
            )
        )
        # Clear the recorded intent so the next cycle re-applies rather than deciding the
        # payload is unchanged and leaving the inverter on someone else's settings.
        self.applied_payload.get(sn, {}).pop(direction, None)

    async def fetch_config(self, sn):
        """Read the current charge and discharge config for one inverter.

        NOT a read-modify-write baseline: both legacy payload builders replace the whole
        object from the locally held schedule rather than patching device_config, so
        nothing reads this back into a write today. It exists to surface the inverter's
        current settings for external-change detection and, once a serial is entitled to
        the periodic API, for reconciling that schedule too.

        Returns True when both legacy reads succeed. The periodic schedule read (entitled
        serials only) is SUPPLEMENTARY and deliberately does NOT gate this return: it is
        one extra call layered on top of the two that already govern every serial, so a
        transient failure on it alone must not make refresh_config decide the whole tier
        failed - that would stall the 30-minute config tier clock and repeat all three GETs
        on every 60-second poll until it recovers, for a read that is not on the write path
        at all. A half-successful LEGACY read still fails the overall result - starting the
        tier clock on that would leave the failed half stale for a full TTL, the same class
        of bug a premature refresh_static success would have (marking a tier fresh on an
        unsuccessful poll). Whatever data DID come back is kept regardless of the overall
        result, so a transient failure on any one endpoint does not blank out the others'
        last-known values - it is logged instead.
        """
        entry = self.device_config.setdefault(sn, {})
        code, charge = await self._get("charge_config", params={"sysSn": sn})
        charge_ok = code == ALPHAESS_CODE_OK and isinstance(charge, dict)
        if charge_ok:
            self.note_external_change(sn, "charge", charge)
            entry["charge"] = charge
        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        code, discharge = await self._get("discharge_config", params={"sysSn": sn})
        discharge_ok = code == ALPHAESS_CODE_OK and isinstance(discharge, dict)
        if discharge_ok:
            self.note_external_change(sn, "discharge", discharge)
            entry["discharge"] = discharge
        if not charge_ok:
            self.log("Warn: AlphaESS {} charge config read failed; keeping the previous cycle's value".format(sn))
        if not discharge_ok:
            self.log("Warn: AlphaESS {} discharge config read failed; keeping the previous cycle's value".format(sn))

        if self._periodic_ok.get(sn) is True:
            # An entitled system writes via setTimeChargeBySn, so applied_payload[sn] only
            # ever carries a "periodic" key - note_external_change("charge"/"discharge", ...)
            # above can never find a match for these serials, and interference on the
            # legacy config objects (which this serial is not written through) would go
            # undetected forever. Read the periodic schedule back and compare like-for-like
            # against what Predbat actually wrote, rather than trying to translate the
            # legacy fields onto the periodic intent - the two endpoints are not guaranteed
            # to describe the same state the same way. Its own success/failure is NOT
            # folded into the return value below - see the docstring.
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
            code, periodic = await self._get("time_charge", params={"sysSn": sn})
            periodic_ok = code == ALPHAESS_CODE_OK and isinstance(periodic, dict)
            if periodic_ok:
                self.note_external_change(sn, "periodic", periodic)
                entry["periodic"] = periodic
            else:
                self.log("Warn: AlphaESS {} periodic schedule read failed; keeping the previous cycle's value".format(sn))
        return charge_ok and discharge_ok

    async def refresh_config(self):
        """Refresh the config baseline for every inverter, and re-probe demoted telemetry.

        The live re-probe lives here rather than on the power tier so a demoted serial
        costs two extra calls an hour instead of one a minute, while still self-healing.

        Saves inline rather than only at shutdown, so a container kill or crash - the
        ordinary Home Assistant add-on restart - does not lose what was just learned: the
        config baseline whenever at least one serial's read fully succeeded, and the
        live-vs-history ("ratings") verdict whenever a re-probe actually restores one.
        Neither is saved unconditionally every tick - this tier runs every 30 minutes and
        most ticks change nothing worth persisting again.
        """
        got_any = False
        live_restored = False
        for sn in self.device_list:
            try:
                if await self.fetch_config(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS config read failed for {}: {}".format(sn, error))
            if sn not in self._periodic_ok:
                try:
                    await self.probe_periodic(sn)
                except Exception as error:
                    self.log("Warn: AlphaESS periodic probe failed for {}: {}".format(sn, error))
            if self._live_ok.get(sn) is False:
                try:
                    if await self.reprobe_live(sn):
                        live_restored = True
                except Exception as error:
                    self.log("Warn: AlphaESS live re-probe failed for {}: {}".format(sn, error))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("config")
            await self.save_config()
        if live_restored:
            await self.save_ratings()
        return got_any

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the inverter."""
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    def _write_allowed(self, sn, direction, force=False):
        """Return True when a write for one serial and direction may go out now.

        The minimum interval treats the documented 24-hour write limit as a real budget.
        A change arriving inside the window is HELD, not dropped: apply_settings leaves the
        applied-payload cache untouched, so the next eligible tick rebuilds and sends it.
        """
        if force or not self.min_write_interval:
            return True
        last = self.last_write_time.get((sn, direction))
        if last is None:
            return True
        return (time.time() - last) >= self.min_write_interval

    async def _write_payload(self, sn, direction, endpoint_key, payload, force=False):
        """Send one payload if it differs from the last applied one and pacing allows.

        Returns whether the inverter is now KNOWN TO MATCH this payload, not merely whether
        an exception was avoided. True covers "sent and confirmed" and "already matched, so
        nothing needed sending" alike. False covers both a HELD change (a real difference
        exists, but the pacing gate is deliberately not sending it yet) and an outright
        rejection - a caller must not read either of those as the inverter matching the plan.
        """
        cache = self.applied_payload.setdefault(sn, {})
        if not force and self.payloads_equal(cache.get(direction), payload):
            self.log("Info: AlphaESS {} {} settings unchanged, nothing sent".format(sn, direction))
            return True
        if not self._write_allowed(sn, direction, force=force):
            self.log("Info: AlphaESS {} {} change is held by alphaess_min_write_interval ({}s) and will be applied on the next eligible cycle".format(sn, direction, self.min_write_interval))
            return False
        code, _ = await self._post(endpoint_key, body=payload)
        # Stamp every attempt, success or not - not just success and 6053. A persistently
        # rejected write (6008 "Set failed", 6042 "system offline", 6001 "Parameter error",
        # ...) must still be paced by alphaess_min_write_interval, or _reconcile_control
        # re-POSTs it every single tick forever, burning the documented 24-hour write budget
        # while doing it. The write is deliberately NOT cached as applied below on any
        # failure path, so it keeps retrying - just paced, not hammering.
        self.last_write_time[(sn, direction)] = time.time()
        if code != ALPHAESS_CODE_OK:
            if code == ALPHAESS_CODE_TOO_FAST:
                # A pacing signal, not a broken component.
                self.log("Info: AlphaESS rate-limited the {} write for {}; it will be retried after the write pacing interval".format(direction, sn))
            else:
                self.log("Warn: AlphaESS {} write for {} was rejected with {}".format(direction, sn, self.describe_code(code)))
            # Saved inline so a container kill or crash right after a write does not lose
            # the pacing timestamp just stamped above and reopen the retry storm this
            # guards against.
            await self.save_control()
            return False
        cache[direction] = payload
        self.settle_count[(sn, direction)] = 0
        self.log("Info: AlphaESS wrote {} settings for {}".format(direction, sn))
        # Saved inline (not only at shutdown) so a container kill or crash right after a
        # successful write does not lose the applied-payload cache or the write timestamp -
        # a restart would otherwise re-learn both from scratch, or worse, forget the write
        # pacing budget was just spent.
        await self.save_control()
        return True

    async def probe_periodic(self, sn):
        """Return True/False/None for whether this system may use the periodic API.

        6017 is an ENTITLEMENT verdict, not a transient error - the API docs are explicit
        that the endpoint is live and the SN binding check passes, and what fails is a
        check on the account tier or hardware. So it is cached and never retried. Any other
        failure leaves the verdict unknown so the next config tier re-probes.
        """
        if sn in self._periodic_ok:
            return self._periodic_ok[sn]
        code, _ = await self._get("time_charge", params={"sysSn": sn})
        if code == ALPHAESS_CODE_OK:
            self._periodic_ok[sn] = True
            self.log("Info: AlphaESS {} is entitled to the periodic schedule API; up to six windows and a power setpoint are available".format(sn))
            return True
        if code == ALPHAESS_CODE_NO_PERMISSION:
            self._periodic_ok[sn] = False
            self.log("Info: AlphaESS {} is not entitled to the periodic schedule API ({}), using the two-window legacy endpoints. This is a permanent verdict and is not retried.".format(sn, self.describe_code(code)))
            return False
        return None

    def _periodic_entry(self, start, end, soc, power):
        """Build one chargeTimeList/dischargeTimeList element.

        chargeLimit is documented as [10,100] and anything outside gets 6001, so it is
        clamped to 10 at the bottom rather than passed through as Predbat's 0.
        """
        entry = {"beginTime": start, "endTime": end, "chargeLimit": self._clamp_percent(soc, low=10, high=100)}
        if power > 0:
            entry["chargePower"] = int(power)
        return entry

    def build_periodic_payload(self, sn, schedule):
        """Build the setTimeChargeBySn body for one inverter.

        executeCycleType 0 (daily) only: Predbat replans continuously, so a weekday-aware
        schedule has nothing to express.

        Both lists must carry at least one element - an empty list is rejected with 6001
        "time list is null", and omitting the key gets 10001 - so a direction with no plan
        gets a filler period and is disabled via its cycle flag instead. The discharge
        filler's chargeLimit is the ONLY carrier of the reserve floor on this path (there is
        no separate standing-floor field, unlike batUseCap on the legacy pair), so it always
        holds schedule["reserve"] rather than an arbitrary constant - see
        build_discharge_payload for the same one-field-two-purposes rule on the legacy pair.
        """
        charge = schedule.get("charge", {}) or {}
        export = schedule.get("export", {}) or {}
        reserve = schedule.get("reserve", 10)
        charge_rate = self._as_float(charge.get("power"), 0.0)
        export_rate = self._as_float(export.get("power"), 0.0)
        charge_on = bool(charge.get("enable")) and charge_rate > 0

        (charge_start, charge_end), _ = self._snapped_periods(sn, "charge", charge.get("start"), charge.get("end"), charge_on)
        if window_is_empty(charge_start, charge_end):
            charge_on = False
        charge_list = [self._periodic_entry(charge_start, charge_end, charge.get("soc", 100), charge_rate)] if charge_on else [self._periodic_entry(ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED, 10, 0)]

        # export_rate zero means hold SOC (freeze export), exactly as build_discharge_payload:
        # discharge time control ON with no permitted period, so the battery cannot discharge
        # at all. Decided before, and independently of, the window/enable check - Predbat
        # reaches export_rate == 0 through ordinary, reachable combinations (see
        # build_discharge_payload's comment), and that is an explicit hold, not the absence of
        # a plan. Getting this wrong lets the battery discharge into the EV or iBoost load
        # against the hold Predbat asked for; ctrDisCycle 0 here is DEMAND MODE, not a hold.
        export_hold = export_rate <= 0
        if export_hold:
            export_on = False
        else:
            export_on = bool(export.get("enable"))
            (export_start, export_end), _ = self._snapped_periods(sn, "export", export.get("start"), export.get("end"), export_on)
            if window_is_empty(export_start, export_end):
                export_on = False

            # Charge and discharge periods must not overlap or the write is rejected with
            # 6008. Standard interval overlap: the two windows overlap when each one starts
            # before the other ends. Trim the edge NEAREST the overlap rather than always the
            # export start - a charge-after-export window (export 12:00-13:30, charge
            # 13:00-14:00) needs its END trimmed back to the charge start, not its start
            # pushed past the charge end, which would invert the window and destroy it.
            if charge_on and export_on and export_start < charge_end and export_end > charge_start:
                if export_start < charge_start:
                    self.log(
                        "Info: AlphaESS {} export window {}-{} overlaps the charge window {}-{}, which the periodic API rejects as overlapping; trimming the export end to {}".format(sn, export_start, export_end, charge_start, charge_end, charge_start)
                    )
                    export_end = charge_start
                else:
                    self.log("Info: AlphaESS {} charge window ends {} and export starts {}, which the periodic API rejects as overlapping; trimming the export start to {}".format(sn, charge_end, export_start, charge_end))
                    export_start = charge_end
                if window_is_empty(export_start, export_end):
                    export_on = False

        discharge_list = [self._periodic_entry(export_start, export_end, export.get("soc", reserve), export_rate)] if export_on else [self._periodic_entry(ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED, reserve, 0)]
        return {
            "sysSn": sn,
            "executeCycleType": 0,
            "chargeTimeList": charge_list,
            "dischargeTimeList": discharge_list,
            "gridChargeCycle": 1 if charge_on else 0,
            # export_hold sets this ON (1) with no permitted period - the hard hold.
            # export_on sets it ON for a real scheduled window. Otherwise it is OFF (0),
            # demand mode, where the battery covers the house down to the reserve floor
            # carried by the idle discharge entry above.
            "ctrDisCycle": 1 if (export_on or export_hold) else 0,
        }

    async def apply_settings(self, sn, schedule, force=False):
        """Build and send the settings for one inverter, gated independently per direction.

        Entitled systems use the periodic API, which carries up to six windows and a real
        power setpoint in one call. Everyone else uses the legacy pair, where charge and
        discharge are gated separately so a charge-only change does not consume a discharge
        write - both endpoints are documented as writable once per 24 hours.
        """
        if not self.control_enable:
            return False
        if self._periodic_ok.get(sn) is True:
            payload = self.build_periodic_payload(sn, schedule)
            return await self._write_payload(sn, "periodic", "set_time_charge", payload, force=force)
        charge_payload = self.build_charge_payload(sn, schedule)
        discharge_payload = self.build_discharge_payload(sn, schedule)
        charge_ok = await self._write_payload(sn, "charge", "update_charge_config", charge_payload, force=force)
        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        discharge_ok = await self._write_payload(sn, "discharge", "update_discharge_config", discharge_payload, force=force)
        return charge_ok and discharge_ok

    async def apply_schedule(self, sn, force=False):
        """Apply the locally held schedule for one inverter."""
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        return await self.apply_settings(sn, schedule, force=force)

    async def _reconcile_control(self, sn):
        """Re-apply sn's schedule if Predbat already controls it, unforced.

        Gated on read-only for a specific reason. Predbat's own read-only handling
        (execute.py:145) covers every write that originates from a plan, but NOT one this
        component initiates itself - and this is exactly that. The payload is time-aware
        because batUseCap switches between the export target and the reserve, so a window
        transition changes it with no plan change at all, and without this gate that
        transition would write to the inverter while Predbat was in read-only mode. This is
        GH#4436 (deye.py:1661, sunsynk.py:1346).
        """
        if sn not in self.control_active or self._is_read_only() or not self.control_enable:
            return
        try:
            await self.apply_schedule(sn)
        except Exception as error:
            self.log("Warn: AlphaESS schedule apply failed for {}: {}".format(sn, error))

    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right inverter and apply it.

        The _unbind suffix is checked BEFORE resolving the serial, and only that branch
        passes include_unbound=True to _sn_from_entity: an unbound serial must resolve
        for its own unbind switch (so turning it back off can clear the latch) and for
        nothing else - every other entity type for an unbound serial must stay
        unresolvable, the same as before it had ever been discovered.
        """
        is_unbind = str(entity_id).endswith("_unbind")
        sn = self._sn_from_entity(entity_id, include_unbound=is_unbind)
        if not sn:
            self.log("Warn: AlphaESS could not resolve an inverter for {}".format(entity_id))
            return
        if is_unbind:
            await self._handle_unbind_event(sn, value)
            return
        # The write button is NOT forced. Predbat presses this on every cycle as its normal
        # "apply the schedule" action (INVERTER_DEF time_button_press), not only when the
        # plan actually changed, so force=True here would bypass the applied-payload
        # change-detection gate on every single cycle. DEYE hit this exact bug first:
        # PR #4371 (commit 3e1de759) measured 40 button presses producing 36 byte-identical
        # control orders over two hours on a live site once the button forced the write.
        # Do not reintroduce force=True here.
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                # Predbat is now actively driving this inverter, so the reconcile loop may
                # re-apply from here on. Marked on the press itself rather than on a
                # successful write: a write that failed still means Predbat owns this
                # inverter and the next tick should retry.
                self.control_active.add(sn)
                await self.apply_schedule(sn)
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

    async def request_verification_code(self, sn, check_code):
        """Ask AlphaESS to email a verification code to the system owner.

        GET, despite the portal describing the payload as "request parameter (Json)" - a
        POST returns HTTP 405. The code is emailed to the END USER's registered address and
        is never returned here, which is why binding cannot be driven from a toggle.
        """
        code, _ = await self._get("verification_code", params={"sysSn": sn, "checkCode": check_code})
        if code == ALPHAESS_CODE_OK:
            return True, "AlphaESS has emailed a verification code to the registered owner of {}".format(sn)
        return False, "Could not request a verification code for {}: {}".format(sn, self.describe_code(code))

    async def bind_system(self, sn, code):
        """Bind a system to this AppID using the emailed verification code.

        Judged on the response code alone: the endpoint answers data:null whether it
        succeeded or not. 6003 "You have bound this SN" is an idempotent success.
        The one-time code is never logged - _post redacts it.
        """
        result, _ = await self._post("bind", body={"sysSn": sn, "code": code})
        if result == ALPHAESS_CODE_OK:
            return True, "Bound {} to this AppID".format(sn)
        if result == ALPHAESS_CODE_ALREADY_BOUND:
            return True, "{} was already bound to this AppID".format(sn)
        return False, "Could not bind {}: {}".format(sn, self.describe_code(result))

    async def unbind_system(self, sn):
        """Unbind a system from this AppID.

        6005 "This appId is not bound to the SN" means it was already gone, which is the
        outcome the caller wanted.
        """
        result, _ = await self._post("unbind", body={"sysSn": sn})
        if result == ALPHAESS_CODE_OK:
            return True, "Unbound {} from this AppID".format(sn)
        if result == ALPHAESS_CODE_NOT_BOUND:
            return True, "{} was not bound to this AppID".format(sn)
        return False, "Could not unbind {}: {}".format(sn, self.describe_code(result))

    async def _handle_unbind_event(self, sn, value):
        """Drive the per-serial unbind toggle, following Sigenergy's offboard pattern.

        Deliberately NOT gated on switch.predbat_set_read_only or alphaess_control_enable:
        both guard writes to the INVERTER, and unbinding is account management rather than
        an inverter write.

        Turning the switch back off clears the latch so discovery picks the system up again
        if it was re-bound via the CLI or the AlphaESS portal. It does NOT re-bind - that
        is impossible without the code emailed to the system owner, so the switch is
        one-way from Home Assistant.
        """
        if not self._to_bool(value):
            if sn in self._unbind_done:
                self._unbind_done.discard(sn)
                await self.save_control()
            return
        if sn in self._unbind_done:
            return
        ok, message = await self.unbind_system(sn)
        if not ok:
            # Leave the latch clear so the next tick retries, matching
            # _offboard_system_if_needed.
            self.log("Warn: AlphaESS {}".format(message))
            return
        self._unbind_done.add(sn)
        if sn in self.device_list:
            self.device_list = [serial for serial in self.device_list if serial != sn]
        self.log("Info: AlphaESS {}. Predbat can no longer read or control it, so num_inverters and the auto-configured args in apps.yaml now reference a system it cannot reach - re-bind it from the portal or the CLI, or update apps.yaml.".format(message))
        await self.save_control()

    async def load_cache(self, name):
        """Load one cache file, returning {} when absent or unreadable.

        self.storage being None is checked FIRST and returns silently, with no warning and
        no _restore_had_error: it means there is simply no Storage component configured
        (the normal state for a standalone CLI run), which is a permanent, by-design
        condition rather than a transient fault worth retrying or warning about. Only a
        REAL failure below flags the restore as incomplete.
        """
        if self.storage is None:
            return {}
        try:
            data = await self.storage.load(ALPHAESS_STORAGE_MODULE, name)
        except Exception as error:
            self.log("Warn: AlphaESS could not load cache {}: {}".format(name, error))
            self._restore_had_error = True
            return {}
        return data if isinstance(data, dict) else {}

    async def save_cache(self, name, data):
        """Save one cache file, tolerating a storage failure.

        Silently does nothing when self.storage is None - there is nothing to warn about.
        """
        if self.storage is None:
            return
        try:
            await self.storage.save(ALPHAESS_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log("Warn: AlphaESS could not save cache {}: {}".format(name, error))

    async def save_static(self):
        """Persist discovery. Refuses to overwrite a good cache with an empty result.

        Writing {'device_list': []} and stamping the tier fresh would make a restart
        restore nothing and skip re-discovery for a full TTL. Absence of a result is not
        a result.
        """
        if not self.device_list:
            return
        await self.save_cache(ALPHAESS_CACHE_STATIC, {"device_list": self.device_list, "device_detail": self.device_detail})

    async def save_config(self):
        """Persist the config baseline and the periodic entitlement verdicts."""
        await self.save_cache(ALPHAESS_CACHE_CONFIG, {"device_config": self.device_config, "periodic_ok": self._periodic_ok})

    async def save_ratings(self):
        """Persist the live-telemetry capability verdicts."""
        await self.save_cache(ALPHAESS_CACHE_RATINGS, {"live_ok": self._live_ok, "ev_present": self._ev_present})

    async def save_control(self):
        """Persist the control state that must survive a restart.

        last_write_time is keyed (sn, direction), which JSON cannot carry as a dict key, so
        it is flattened to "sn|direction" strings. Without persisting it, the documented
        24-hour write budget resets on every restart, and a restart loop bypasses
        alphaess_min_write_interval entirely against endpoints writable once per 24 hours.
        """
        await self.save_cache(
            ALPHAESS_CACHE_CONTROL,
            {
                "local_schedule": self.local_schedule,
                "applied_payload": self.applied_payload,
                "control_active": sorted(self.control_active),
                "unbind_done": sorted(self._unbind_done),
                "last_write_time": {"{}|{}".format(sn, direction): timestamp for (sn, direction), timestamp in self.last_write_time.items()},
            },
        )

    async def restore_state(self):
        """Restore every cached verdict so a restart does not re-learn them from scratch.

        _cache_restored is set only when nothing failed, so a transient storage outage is
        retried on a later cycle rather than silently marked done with nothing restored.
        """
        self._restore_had_error = False
        static = await self.load_cache(ALPHAESS_CACHE_STATIC)
        if static.get("device_list"):
            self.device_list = list(static["device_list"])
            self.device_detail = dict(static.get("device_detail") or {})
        config = await self.load_cache(ALPHAESS_CACHE_CONFIG)
        self.device_config = dict(config.get("device_config") or {})
        self._periodic_ok = dict(config.get("periodic_ok") or {})
        ratings = await self.load_cache(ALPHAESS_CACHE_RATINGS)
        self._live_ok = dict(ratings.get("live_ok") or {})
        self._ev_present = dict(ratings.get("ev_present") or {})
        control = await self.load_cache(ALPHAESS_CACHE_CONTROL)
        self.local_schedule = dict(control.get("local_schedule") or {})
        self.applied_payload = dict(control.get("applied_payload") or {})
        self.control_active = set(control.get("control_active") or [])
        self._unbind_done = set(control.get("unbind_done") or [])
        self.last_write_time = {}
        for key, timestamp in (control.get("last_write_time") or {}).items():
            sn, separator, direction = str(key).partition("|")
            if separator:
                self.last_write_time[(sn, direction)] = timestamp
        self._cache_restored = not self._restore_had_error

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any schedule change.

        Returns True on a completed cycle, False on a failure that should hold the
        component in ComponentBase's startup backoff and be retried. Deliberately explicit
        rather than falling through to Python's implicit `None`: ComponentBase only clears
        its `first` flag and moves to the normal cadence when this returns something
        truthy, so an accidental `None` here would strand the component in the
        ever-growing startup backoff (60s doubling to 128 minutes) forever, even though
        every cycle after the first was actually working.
        """
        if first and not self._cache_restored:
            # Guarded on _cache_restored, not just `first`: a deferred startup (no
            # telemetry, no systems found yet) returns False here and leaves
            # ComponentBase's `first` flag set, so run() is retried with first still True.
            # Without this guard a SUCCESSFUL restore_state() would re-run on every one of
            # those retries, redoing storage reads for nothing and re-overwriting anything
            # already progressed in memory since - restore_state() itself already refuses
            # to mark the guard done on a genuine failure, so a real storage outage still
            # retries here as before.
            await self.restore_state()
        if not self.app_id or not self.app_secret:
            self.log("Warn: AlphaESS needs both alphaess_app_id and alphaess_app_secret; get them from https://open.alphaess.com/")
            return False

        if self.tier_expired("static", ALPHAESS_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: AlphaESS found no battery systems on this account")
            return False
        if self.tier_expired("config", ALPHAESS_TTL_CONFIG):
            await self.refresh_config()
        live_ok = True
        if self.tier_expired("power", self.power_tier_ttl()):
            live_ok = await self.refresh_power()
        if self.tier_expired("energy", ALPHAESS_TTL_ENERGY):
            await self.refresh_energy()

        for sn in list(self.device_list):
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
                self.log("Warn: AlphaESS schedule read failed for {}: {}".format(sn, error))
            await self._reconcile_control(sn)
            # Published every tick, not just first: this is Predbat's control surface and
            # must keep reflecting local_schedule as it changes.
            await self.publish_schedule_settings_ha(sn)

        await self.publish_data()

        if first and not live_ok:
            # Startup has not really succeeded without telemetry: automatic_config() runs
            # on the first cycle ALONE, so it would map only the args backed by cached
            # ratings and permanently skip soc_max and the energy args for the whole
            # session. Returning False leaves ComponentBase's `first` flag set, so the
            # entire startup path is retried on its backoff until a poll comes back.
            self.log("Warn: AlphaESS first telemetry poll returned nothing, deferring startup; it will be retried after a backoff")
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


def _build_alphaess(mock_base, args):  # pragma: no cover
    """Construct an AlphaESSAPI around a MockBase for standalone command-line use.

    Passed into the constructor in a single call: ComponentBase.__init__ already calls
    initialize(**kwargs), so a separate follow-up call to initialize() would re-run it a
    second time and print a duplicate banner before the CLI has reported anything.
    """
    return AlphaESSAPI(
        mock_base,
        app_id=args.app_id,
        app_secret=args.app_secret,
        inverter_sn=args.serial,
        automatic=False,
        control_enable=False,
        api_delay=args.api_delay,
    )


def _confirm(prompt):  # pragma: no cover
    """Ask for confirmation, treating a closed stdin and Ctrl-C as 'no'.

    This is the only verification tool a remote tester has, so a closed or redirected
    stdin (SSH, CI, a container with no TTY) must not crash it with a raw traceback -
    EOFError and Ctrl-C both mean "no", cleanly.
    """
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nNo input available, nothing sent.")
        return False
    return answer.strip().lower() == "y"


async def test_alphaess_api(args):  # pragma: no cover
    """Run one AlphaESS diagnostic pass, or a single bind/unbind action."""
    from mock_base import MockBase

    client = _build_alphaess(MockBase(), args)

    if args.verify:
        print(f"This asks AlphaESS to EMAIL a verification code to the registered owner of {args.serial}.")
        if not _confirm("Send that request? [y/N] "):
            return
        ok, message = await client.request_verification_code(args.serial, args.check_code)
        print(("OK: " if ok else "FAILED: ") + message)
        return

    if args.bind:
        print(f"This binds {args.serial} to AppID {args.app_id}.")
        if not _confirm("Send the bind request? [y/N] "):
            return
        ok, message = await client.bind_system(args.serial, args.code)
        print(("OK: " if ok else "FAILED: ") + message)
        return

    if args.unbind:
        print(f"This UNBINDS {args.serial} from AppID {args.app_id}.")
        print("Predbat will no longer be able to read or control it, and re-binding needs a code emailed to the system owner.")
        if not _confirm("Send the unbind request? [y/N] "):
            return
        ok, message = await client.unbind_system(args.serial)
        print(("OK: " if ok else "FAILED: ") + message)
        return

    print("Calling run() once (read-only: discover, poll config/telemetry, publish)...")
    ok = await client.run(seconds=0, first=True)
    if not ok:
        if not client.app_id or not client.app_secret:
            print("\nMISSING CREDENTIALS - --app-id and --app-secret must both be non-empty.")
            print("  Get them from https://open.alphaess.com/")
        elif client.discovery_ok is False:
            print("\nDISCOVERY FAILED - the getEssList call itself was rejected.")
            if client.last_api_error:
                print(f"  AlphaESS said: {client.last_api_error}")
            print("  Check --app-id and --app-secret, and check this host's clock: the signature is")
            print("  only valid within 300 seconds of AlphaESS server time.")
        elif not client.device_list:
            print("\nCREDENTIALS OK, but no battery systems were found on this account.")
            if args.serial:
                print(f"  --serial {args.serial!r} was set - a filter matching nothing looks identical to an empty")
                print("  account; retry without --serial to check.")
            print("  Systems reporting no battery capacity (plug-in solar) are skipped by design.")
        else:
            print(f"\nDISCOVERY OK ({len(client.device_list)} system(s): {client.device_list}), but the first telemetry poll came back empty.")
            print("  Check the Warn: lines above for which endpoint failed.")
        await client.final()
        return

    print(f"Systems: {client.device_list}")
    for sn in client.device_list:
        print(f"\n--- {sn} detail ---")
        print(json.dumps(client.device_detail.get(sn, {}), indent=2, default=str))
        print(f"\n--- {sn} telemetry ---")
        print(json.dumps(client.device_values.get(sn, {}), indent=2, default=str))
        print(f"\n--- {sn} energy ---")
        print(json.dumps(client.device_energy.get(sn, {}), indent=2, default=str))
        if args.dump_settings:
            print(f"\n--- {sn} charge/discharge config ---")
            print(json.dumps(client.device_config.get(sn, {}), indent=2, default=str))
        live = "live (getLastPowerData)" if client._live_ok.get(sn) is not False else "history (getOneDayPowerBySn, 5 minute)"
        periodic = {True: "yes", False: "no (6017)", None: "unknown"}[client._periodic_ok.get(sn)]
        print(f"\nDerived: capacity={client.battery_capacity(sn):.2f} kWh, inverter_limit={client.inverter_limit(sn):.0f} W, battery_rate_max={client.battery_rate_max(sn):.0f} W")
        print(f"Telemetry source: {live}; periodic schedule API entitled: {periodic}")

    await client.final()
    print("Done")


def main():  # pragma: no cover
    """Command-line entry point for AlphaESS diagnostics and system binding."""
    import argparse

    parser = argparse.ArgumentParser(description="AlphaESS Open API diagnostics")
    parser.add_argument("--app-id", required=True, help="AlphaESS developer AppID from https://open.alphaess.com/")
    parser.add_argument("--app-secret", required=True, help="AlphaESS developer AppSecret")
    parser.add_argument("--serial", default=None, help="Restrict to one system serial (sysSn)")
    parser.add_argument("--api-delay", type=float, default=2, help="Seconds to wait between API calls")
    parser.add_argument("--dump-settings", action="store_true", help="Print the full charge/discharge config objects")
    parser.add_argument("--verify", action="store_true", help="Ask AlphaESS to email a verification code to the system owner (needs --serial and --check-code)")
    parser.add_argument("--check-code", default=None, help="The system's CheckCode, from the device label or the installer")
    parser.add_argument("--bind", action="store_true", help="Bind --serial to this AppID (needs --code from the verification email)")
    parser.add_argument("--code", default=None, help="Verification code from the email triggered by --verify")
    parser.add_argument("--unbind", action="store_true", help="Unbind --serial from this AppID")
    args = parser.parse_args()

    if (args.verify or args.bind or args.unbind) and not args.serial:
        parser.error("--serial is required with --verify, --bind or --unbind")
    if args.verify and not args.check_code:
        parser.error("--check-code is required with --verify")
    if args.bind and not args.code:
        parser.error("--code is required with --bind (run --verify first to have one emailed)")

    asyncio.run(test_alphaess_api(args))


if __name__ == "__main__":  # pragma: no cover
    main()
