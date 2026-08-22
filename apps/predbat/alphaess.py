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
    ALPHAESS_CODE_TIMESTAMP,
    ALPHAESS_DEBUG_REDACT_KEYS,
    ALPHAESS_DEBUG_REDACT_KEYS_RESPONSE,
    ALPHAESS_RETRIES,
    ALPHAESS_TIMEOUT,
    ALPHAESS_TELEMETRY,
    ALPHAESS_TELEMETRY_NEGATE,
    ALPHAESS_HISTORY,
    ALPHAESS_HISTORY_FEED_IN,
    ALPHAESS_HISTORY_GRID_CHARGE,
    ALPHAESS_LIVE_FAIL_LIMIT,
    ALPHAESS_TTL_POWER,
    ALPHAESS_TTL_POWER_DEMOTED,
    ALPHAESS_ENERGY,
    ALPHAESS_ENERGY_LOAD_FIELD,
    ALPHAESS_AC_COUPLED_MODELS,
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
            self.log("Warn: AlphaESS rejected the request timestamp ({}) - this host's clock is more than 300 seconds from AlphaESS server time, it is a clock problem and not a credentials problem".format(self.describe_code(code)))
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
        self.device_values[sn] = values
        return True

    def _apply_history_payload(self, sn, samples):
        """Map the most recent getOneDayPowerBySn sample into device_values.

        Returns False when the history carries no SOC, which is the only thing that makes
        a serial undriveable - everything else can be defaulted.
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
        self.device_values[sn] = {
            "soc": soc,
            "pv_power": self._as_float(sample.get("ppv"), 0.0),
            "load_power": self._as_float(sample.get("load"), 0.0),
            # The history has no signed grid field, so it is reconstructed from the two
            # positive-only fields and then negated for Predbat's convention.
            "grid_power": -(grid_charge - feed_in),
        }
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
        """
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_device_data(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS telemetry poll failed for {}: {}".format(sn, error))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("power")
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

    def _sn_from_entity(self, entity_id):
        """Extract the serial from an AlphaESS entity id, or None if unresolvable.

        Entity ids are always {domain}.{prefix}_alphaess_{sn}_{leaf}, so the serial is
        always followed by "_". Matching sn + "_" rather than a bare prefix keeps
        prefix-colliding serials apart - an entity for AL701 must never route to AL70,
        which would send a control write to the wrong inverter.
        """
        text = str(entity_id).lower()
        for sn in self.device_list:
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

    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right inverter and apply it."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log("Warn: AlphaESS could not resolve an inverter for {}".format(entity_id))
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)
