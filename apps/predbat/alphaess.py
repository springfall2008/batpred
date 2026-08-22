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
