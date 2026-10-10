# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Fronius Solar.web (Query + Flexibility API) Library
# -----------------------------------------------------------------------------

"""Fronius Solar.web integration for Predbat.

Registers one Fronius PV system (a GEN24-class hybrid with a battery and a Fronius Smart Meter) as
a ``FroniusCloud`` Predbat inverter. Telemetry comes from the Solar.web Query API; control goes
through the Solar.web Flexibility API, which needs a contract with Fronius and the system owner's
consent. Both use the same AccessKeyId / AccessKeyValue header pair on every call.

THE SCHEDULE IS THE CENTRAL FACT OF THIS COMPONENT.

Fronius control is not a setting and not an instantaneous setpoint. It is a SCHEDULE: an ordered
list of commands, each with a UTC start time and a duration in seconds, and every schedule sent
for a PV system REPLACES the previous one in full. That maps naturally onto Predbat's plan, so this
component does not hold the inverter in a mode - it rebuilds a short, dated schedule from Predbat's
control entities on every tick and sends it only when what it would make the inverter do has
actually changed (see segments_equivalent in fronius_const.py).

The mapping, per period:

- charge window active  -> ChargeBattery MinW = MaxW = charge rate (an exact rate; grid fills any
                           shortfall in PV, and discharging is blocked);
- charge target reached -> hold (below). ChargeBattery has no target-SoC parameter, so reaching the
                           target - rounded the way Predbat writes it - is detected here from the
                           telemetry SoC and turned into a schedule change;
- freeze charge / hold  -> hold: ChargeBattery MinW = 0, MaxW = battery rate, meaning no discharge,
                           no grid import, solar may still charge. Predbat signals it by setting the
                           discharge rate to zero, and that signal wins inside a charge window too;
                           if the charge rate is also zero, MaxW = 0 (Fronius's "keep the level");
- export window active  -> DischargeBattery MinW = MaxW = export rate (surplus goes to the grid,
                           and charging is blocked), until the export target Predbat writes to the
                           charge SoC entity (target_soc_used_for_discharge) is reached;
- freeze export         -> DischargeBattery MinW = 0, MaxW = discharge rate (the battery may cover
                           house load but cannot charge, and nothing is forced out to the grid);
- otherwise             -> no command at all, i.e. the inverter's own self-consumption.

FAILURE BEHAVIOUR. Every schedule ends at most FRONIUS_HORIZON_MINUTES + FRONIUS_RENEW_MINUTES
(90 minutes) after it was sent, and the component renews it - every 30 to 60 minutes when nothing
else changes - while Predbat is running. If Predbat
stops, crashes, loses its network or is put into read-only mode, the last schedule simply runs out
and the inverter returns to its own normal operation. Nothing is stored in the inverter beyond that
horizon, so a stale plan cannot keep forcing the battery for hours. The trade is that a forced
charge already in flight continues until the schedule runs out (up to 90 minutes, with no target
SoC to stop it - see the charge-target bullet above).

LATENCY. Fronius accept a schedule within seconds but may take up to two minutes to deliver it to
the inverter. Predbat publishes the next window well ahead of its start (set_window_minutes), and
a change is sent as soon as it is within FRONIUS_LOOKAHEAD_MINUTES (30) of now, so upcoming
windows reach the inverter long before they begin. A change to the CURRENT period (a target
reached, a freeze starting) necessarily lands up to two minutes late.

COST. Query reads are billed per data point. One power-flow read is one data point and carries
every channel Predbat uses, so it is read once per Predbat cycle. Daily energy is one data point
per channel, so it is read less often (fronius_energy_interval), and device metadata once a day.
A running count is published on the status sensor.

UNVERIFIED AGAINST HARDWARE. Nobody on the project has a Fronius system with a Flexibility API
contract. Every request and response is traced to the log while api_debug is on. The documented
readings this component relies on, each a judgement where the documentation is ambiguous, are:

- power-flow signs follow "positive towards the inverter" (fronius_const.py, FRONIUS_FLOW_*);
- dispatchParameters is sent in the object-keyed form every JSON example uses, with a single
  fallback to the name/value array the object tables describe, and the first accepted schedule is
  read back to confirm its parameters stuck (FRONIUS_PARAMS_*);
- the single-system schedule body is a bare JSON array of commands, as in its example;
- ChargeBattery's grid import serves MinW only, so MinW = 0 never imports and PV fills up to MaxW
  (see _hold_params - the notes do not say this outright); DischargeBattery with MinW = 0 covers
  load without forcing export;
- daily energy for "today" is requested by the Predbat timezone's local date.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
from component_base import ComponentBase
from coordinator import inverter_record
from fronius_const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_EXPORT_LIMIT,
    ACTION_HOLD,
    ACTION_IDLE,
    ACTION_NO_CHARGE,
    FRONIUS_AUTH_BACKOFF_SECONDS,
    FRONIUS_AUTH_ERROR_CODES,
    FRONIUS_CACHE_CONTROL,
    FRONIUS_CACHE_STATIC,
    FRONIUS_CANCEL_DONE_CODES,
    FRONIUS_CONTROL_URL,
    FRONIUS_DEVICE_TYPES,
    FRONIUS_ENDPOINTS,
    FRONIUS_ENERGY_CHANNELS,
    FRONIUS_FLOW_BATTERY,
    FRONIUS_FLOW_GRID,
    FRONIUS_FLOW_LOAD,
    FRONIUS_FLOW_PV,
    FRONIUS_FLOW_SOC,
    FRONIUS_HEADER_KEY_ID,
    FRONIUS_HEADER_KEY_VALUE,
    FRONIUS_HEADER_RATE_REMAINING,
    FRONIUS_HEADER_RATE_RESET,
    FRONIUS_LOOKAHEAD_MINUTES,
    FRONIUS_MAINTENANCE_BACKOFF_SECONDS,
    FRONIUS_MAINTENANCE_CODES,
    FRONIUS_MIN_SEGMENT_SECONDS,
    FRONIUS_MIN_WRITE_SECONDS,
    FRONIUS_PARAM_EXPORT_LIMIT_W,
    FRONIUS_PARAM_MAX_W,
    FRONIUS_PARAM_MIN_W,
    FRONIUS_PARAMS_DEFAULT,
    FRONIUS_PARAMS_LIST,
    FRONIUS_PARAMS_OBJECT,
    FRONIUS_QUERY_URL,
    FRONIUS_RATE_LIMIT_BACKOFF_MAX_SECONDS,
    FRONIUS_RATE_LIMIT_BACKOFF_SECONDS,
    FRONIUS_RATE_LIMIT_CODES,
    FRONIUS_SCOPE_CONTROL,
    FRONIUS_SCOPE_QUERY,
    FRONIUS_SHAPE_ERROR_CODES,
    FRONIUS_STORAGE_MODULE,
    FRONIUS_TIMEOUT,
    FRONIUS_TTL_ENERGY_DEFAULT,
    FRONIUS_TTL_POWER,
    FRONIUS_TTL_RETRY,
    FRONIUS_TTL_STATIC,
    FRONIUS_SYSTEM_NOT_FOUND_CODES,
    FRONIUS_UNSUPPORTED_CONTROL_CODES,
    as_float,
    ceil_minute,
    channel_values,
    daily_energy_kwh,
    describe_error,
    horizon_end,
    make_segment,
    normalise_params,
    null_channels,
    merge_segments,
    parse_error_body,
    parse_zulu,
    predbat_battery_power,
    predbat_grid_power,
    predbat_load_power,
    schedule_hash,
    segments_equivalent,
    segments_from_json,
    segments_to_commands,
    segments_to_json,
    segment_dispatch_type,
    short_system_id,
    window_to_utc,
    zulu,
)

# The behaviour a FroniusCloud inverter has, stated for the discovery record's capabilities. A
# literal, never read back from INVERTER_DEF: the record has to rebuild the row on its own, or the
# completeness test proves nothing.
#
# charge_control_immediate is False: Predbat hands this component ordinary windows and the
# component turns them into a dated schedule itself. support_feedin_first is False - the API has
# no export-first work mode; freeze export is expressed as a no-charge discharge command instead.
FRONIUS_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}

# Published as the initial value of the two power entities when the battery's own limit is not yet
# known. Deliberately not zero: a zero discharge rate is how Predbat signals a hold, so a zero
# default would read as "hold the battery" until Predbat first wrote a real rate.
FRONIUS_DEFAULT_POWER_W = 2600


class FroniusCloud(ComponentBase):
    """Fronius Solar.web Query + Flexibility API component for one PV system."""

    # Trace every API request and response while the integration beds in. Nobody on the project
    # has a Fronius system with a Flexibility API contract, so a tester's log is the only evidence
    # for the documented-but-unverified readings listed in the module docstring.
    api_debug = True

    def initialize(
        self,
        access_key_id="",
        access_key_value="",
        pv_system_id="",
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=True,
        battery_rate_max=None,
        grid_export_limit=None,
        energy_interval=FRONIUS_TTL_ENERGY_DEFAULT,
        query_url=None,
        control_url=None,
        **kwargs,
    ):
        """Initialise the Fronius component from its resolved config args.

        The Components registry resolves each arg from its fronius_* config key and passes it by
        arg name (access_key_id <- fronius_access_key_id). Consume the kwargs directly rather than
        re-reading them with get_arg, which would look up the bare name and miss.
        """
        self.log("Info: Fronius component initialising")
        self.access_key_id = str(access_key_id or "").strip()
        self.access_key_value = str(access_key_value or "").strip()
        self.pv_system_id = str(pv_system_id or "").strip()
        self.system_short_id = short_system_id(self.pv_system_id)
        self.automatic = bool(automatic)
        self.automatic_ignore_pv = bool(automatic_ignore_pv)
        self.control_enable = bool(control_enable)
        self.battery_rate_max_override = max(0.0, as_float(battery_rate_max, 0.0))
        limit = as_float(grid_export_limit)
        self.grid_export_limit = int(limit) if limit is not None and limit >= 0 else None
        self.energy_interval = max(FRONIUS_TTL_POWER, int(as_float(energy_interval, FRONIUS_TTL_ENERGY_DEFAULT)))
        self.query_url = str(query_url or FRONIUS_QUERY_URL).rstrip("/")
        self.control_url = str(control_url or FRONIUS_CONTROL_URL).rstrip("/")

        # Telemetry
        self.flow = {}
        # When each power-flow channel was last reported with a real value, so a value Solar.web has
        # since returned as NULL - or simply not refreshed - is not mistaken for a current one.
        self.flow_seen = {}
        self.flow_time = None
        self.is_online = None
        self.last_flow_ok = None
        self.energy = {}
        self.battery_info = {}
        self.inverter_info = {}
        self.pv_seen = False

        # Control
        self.local_schedule = self._empty_schedule()
        self.planned_segments = []
        self.sent_segments = []
        self.dispatch_id = None
        self.last_write_time = None
        self.params_shape = FRONIUS_PARAMS_DEFAULT
        self.params_shape_confirmed = False
        # Whether Predbat has written any control entity (this session, or before a restart - it is
        # persisted). The reconcile loop only applies once it has, so a first-ever startup tick can
        # never take control of the battery from entities Predbat has not yet set.
        self.control_active = False
        # Set when the API says this PV system cannot be battery-controlled at all (no battery, a
        # pre-GEN24 inverter, a multi-inverter site). Monitoring continues; writes stop.
        self.control_unsupported = None
        # When a forced schedule cannot be cancelled (no dispatch id, empty replacement refused),
        # nothing more can be done until it runs out at this epoch; until then the idle plan is not
        # retried every tick.
        self._cancel_blocked_until = None
        # The control scope's backoff deadline already reported, so a parked scope logs once.
        self._parked_reported = None
        # Read-back verifications of the parameter shape that found the parameters missing.
        self._shape_verify_failures = 0

        # API health
        self._backoff_until = {}
        self._backoff_reason = {}
        self._rate_backoff_seconds = {}
        self.auth_failed = {}
        self.rate_limit_remaining = {}
        self.last_api_error = ""
        self.data_points_today = 0
        self._data_points_date = None
        self._tier_refreshed = {}
        self._cache_restored = False

        if not self.control_enable:
            self.log("Info: Fronius control is disabled (fronius_control_enable is false); monitoring only")

    # -------------------------------------------------------------------------
    # Clock
    # -------------------------------------------------------------------------

    def utc_now(self):
        """Return the real current time in UTC. A method so tests can move the clock."""
        return datetime.now(timezone.utc)

    def _epoch(self):
        """Return utc_now() as a Unix timestamp, so every timer in the component shares one clock."""
        return self.utc_now().timestamp()

    def tier_expired(self, tier, ttl_minutes):
        """Return whether a refresh tier is due."""
        last = self._tier_refreshed.get(tier)
        if last is None:
            return True
        return (self._epoch() - last) >= ttl_minutes * 60

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Stamp a refresh tier as just completed, optionally aged by age_minutes."""
        self._tier_refreshed[tier] = self._epoch() - age_minutes * 60

    def mark_retry(self, tier, ttl_minutes):
        """Schedule a failed tier to be retried after FRONIUS_TTL_RETRY minutes rather than next tick."""
        self.mark_refreshed(tier, age_minutes=max(0, ttl_minutes - FRONIUS_TTL_RETRY))

    # -------------------------------------------------------------------------
    # Request layer
    # -------------------------------------------------------------------------

    def _path(self, endpoint, **values):
        """Format one endpoint path for this PV system."""
        return FRONIUS_ENDPOINTS[endpoint].format(pv_system_id=self.pv_system_id, **values)

    def _headers(self, has_body=False):
        """Return the headers every call carries. The two key headers authenticate every request."""
        headers = {FRONIUS_HEADER_KEY_ID: self.access_key_id, FRONIUS_HEADER_KEY_VALUE: self.access_key_value, "Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response while api_debug is on. Headers are never logged."""
        if not self.api_debug:
            return
        if payload is None:
            self.log("Info: Fronius API {} {}".format(direction, what))
            return
        try:
            rendered = json.dumps(payload, default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(payload)[:2000]
        for secret in (self.access_key_id, self.access_key_value):
            if secret:
                rendered = rendered.replace(secret, "***")
        self.log("Info: Fronius API {} {} {}".format(direction, what, rendered))

    def _in_backoff(self, scope):
        """Return whether calls to a scope are currently parked."""
        return self._epoch() < self._backoff_until.get(scope, 0)

    def _park(self, scope, seconds, reason):
        """Park a scope for a number of seconds, recording why for the status sensor."""
        self._backoff_until[scope] = self._epoch() + seconds
        self._backoff_reason[scope] = reason

    def _note_rate_limit(self, scope, headers, path):
        """Back off after a 429, honouring x-rate-limit-reset when Fronius send it."""
        reset = parse_zulu(headers.get(FRONIUS_HEADER_RATE_RESET))
        wait = None
        if reset is not None:
            wait = (reset - self.utc_now()).total_seconds()
        if wait is None or wait <= 0:
            previous = self._rate_backoff_seconds.get(scope, 0)
            wait = FRONIUS_RATE_LIMIT_BACKOFF_SECONDS if not previous else previous * 2
        wait = int(min(max(wait, FRONIUS_RATE_LIMIT_BACKOFF_SECONDS), FRONIUS_RATE_LIMIT_BACKOFF_MAX_SECONDS))
        self._rate_backoff_seconds[scope] = wait
        self._park(scope, wait, "rate_limited")
        # A pacing signal, not a fault - Info rather than Warn.
        self.log("Info: Fronius rate-limited {} on {}; pausing {} calls for {}s".format(path, scope, scope, wait))

    def _note_auth_failure(self, scope, status, code, message, path):
        """Report a credential or permission rejection once and park the scope.

        The message names what to check. A 403 from the control host is the expected answer when the
        key has no Flexibility API contract or the system owner has not consented, so it says so
        and makes clear that monitoring carries on.
        """
        self.auth_failed[scope] = True
        self._park(scope, FRONIUS_AUTH_BACKOFF_SECONDS, "auth_failed")
        detail = describe_error(code, message) if code is not None or message else "no detail"
        if code in FRONIUS_SYSTEM_NOT_FOUND_CODES:
            advice = "check fronius_pv_system_id is the PV system's Solar.web id"
        elif scope == FRONIUS_SCOPE_CONTROL:
            advice = "control needs a Flexibility API contract with Fronius and the PV system owner's consent; monitoring continues"
        else:
            advice = "check fronius_access_key_id and fronius_access_key_value, and that the key has access to this PV system"
        self.log("Warn: Fronius rejected {} {} (HTTP {}, {}) - {}. Pausing {} calls for {} minutes".format(scope, path, status, detail, advice, scope, FRONIUS_AUTH_BACKOFF_SECONDS // 60))

    async def _request(self, scope, method, path, params=None, body=None):
        """Perform one authenticated call and return a result dict. Never raises.

        The dict carries ok (2xx), status, data (the parsed JSON body or None), code and message
        (the detailed Fronius error, when there was one) and skipped (True when the scope was
        parked and no call was made). Every failure is logged here, once, with a meaning attached,
        so callers only branch on the outcome.
        """
        result = {"ok": False, "status": None, "data": None, "code": None, "message": "", "skipped": False}
        if self._in_backoff(scope):
            result["skipped"] = True
            result["message"] = self._backoff_reason.get(scope, "backing off")
            return result

        base_url = self.control_url if scope == FRONIUS_SCOPE_CONTROL else self.query_url
        url = "{}{}".format(base_url, path)
        self.debug_api("request", "{} {}{}".format(method, path, "?" + "&".join("{}={}".format(k, v) for k, v in params.items()) if params else ""), body)
        try:
            timeout = aiohttp.ClientTimeout(total=FRONIUS_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=self._headers(body is not None), params=params, json=body) as response:
                    status = response.status
                    headers = {str(key).lower(): value for key, value in (response.headers or {}).items()}
                    text = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            self.last_api_error = "{} {}: {}".format(method, path, error or type(error).__name__)
            self.log("Warn: Fronius {} {} transport failure: {}".format(method, path, error or type(error).__name__))
            return result

        result["status"] = status
        data = None
        if text and str(text).strip():
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        result["data"] = data
        if FRONIUS_HEADER_RATE_REMAINING in headers:
            self.rate_limit_remaining[scope] = headers[FRONIUS_HEADER_RATE_REMAINING]
        self.debug_api("response", "{} {} HTTP {}".format(method, path, status), data)

        if 200 <= status < 300:
            result["ok"] = True
            self.auth_failed[scope] = False
            self._rate_backoff_seconds[scope] = 0
            return result

        code, message = parse_error_body(data)
        result["code"] = code
        result["message"] = message
        self.last_api_error = "HTTP {} {}".format(status, describe_error(code, message))
        if status == 429 or code in FRONIUS_RATE_LIMIT_CODES:
            self._note_rate_limit(scope, headers, path)
        elif status in (401, 403) or code in FRONIUS_AUTH_ERROR_CODES or code in FRONIUS_SYSTEM_NOT_FOUND_CODES:
            self._note_auth_failure(scope, status, code, message, path)
        elif code in FRONIUS_MAINTENANCE_CODES or status == 503:
            self._park(scope, FRONIUS_MAINTENANCE_BACKOFF_SECONDS, "maintenance")
            self.log("Info: Fronius {} is unavailable ({}); pausing {} calls for {} minutes".format(scope, describe_error(code, message), scope, FRONIUS_MAINTENANCE_BACKOFF_SECONDS // 60))
        else:
            self.log("Warn: Fronius {} {} returned HTTP {} {}".format(method, path, status, describe_error(code, message)))
        return result

    def _count_data_points(self, count):
        """Add to today's estimate of billed query data points, resetting at local midnight."""
        today = self.utc_now().astimezone(self.local_tz).date().isoformat()
        if self._data_points_date != today:
            self._data_points_date = today
            self.data_points_today = 0
        self.data_points_today += count

    # -------------------------------------------------------------------------
    # Telemetry
    # -------------------------------------------------------------------------

    async def refresh_static(self):
        """Read battery and inverter metadata (capacity, power limits, model). True on success.

        One data point per call, refreshed daily. A failure keeps whatever was known before -
        absence of a result is not a result.
        """
        res = await self._request(FRONIUS_SCOPE_QUERY, "GET", self._path("devices"), params={"type": FRONIUS_DEVICE_TYPES, "isActive": "true"})
        if not res["ok"]:
            if not res["skipped"]:
                self.mark_retry("static", FRONIUS_TTL_STATIC)
            return False
        self._count_data_points(1)
        data = res["data"]
        devices = data.get("devices") if isinstance(data, dict) else data
        battery = {"capacity_wh": 0.0, "max_charge_w": 0.0, "max_discharge_w": 0.0}
        inverter = {"nominal_ac_w": 0.0}
        batteries = 0
        for device in devices or []:
            if not isinstance(device, dict) or device.get("isActive") is False:
                continue
            kind = str(device.get("deviceType") or "").lower()
            if kind == "battery":
                batteries += 1
                battery["capacity_wh"] += as_float(device.get("capacity"), 0.0)
                battery["max_charge_w"] += as_float(device.get("maxChargePower"), 0.0)
                battery["max_discharge_w"] += as_float(device.get("maxDischargePower"), 0.0)
                for field in ("minSOC", "maxSOC"):
                    if as_float(device.get(field)) is not None and field not in battery:
                        battery[field] = as_float(device.get(field))
                if device.get("deviceName") and "model" not in battery:
                    battery["model"] = str(device["deviceName"]).strip()
            elif kind == "inverter":
                inverter["nominal_ac_w"] += as_float(device.get("nominalAcPower"), 0.0)
                if "model" not in inverter and (device.get("deviceTypeDetails") or device.get("deviceName")):
                    inverter["model"] = str(device.get("deviceTypeDetails") or device.get("deviceName")).strip()
                serial = device.get("serialNumber") or device.get("serialnumber")
                if serial and "serial" not in inverter:
                    inverter["serial"] = str(serial).strip()
        if not batteries:
            self.log("Warn: Fronius found no active battery on PV system {}; Predbat can monitor it but there is nothing to control".format(self.pv_system_id))
        self.battery_info = battery
        self.inverter_info = inverter
        self.mark_refreshed("static")
        return True

    async def refresh_power(self):
        """Read the real-time power flow - one call, one data point, every channel Predbat uses.

        Solar.web answers NULL for every channel when it timed out reading the inverter itself; that
        is treated as a failed read and retried later rather than published as zeros. A NULL for one
        channel drops that channel's previous value, so a stale SoC never drives a target check.
        """
        res = await self._request(FRONIUS_SCOPE_QUERY, "GET", self._path("flowdata"))
        if not res["ok"]:
            if not res["skipped"]:
                self.mark_retry("power", FRONIUS_TTL_POWER)
            return False
        self._count_data_points(1)
        data = res["data"] if isinstance(res["data"], dict) else {}
        status = data.get("status") or {}
        body = data.get("data") or {}
        values = channel_values(body.get("channels"))
        nulls = null_channels(body.get("channels"))
        if isinstance(status, dict) and "isOnline" in status:
            self.is_online = bool(status.get("isOnline"))
        if not values:
            self.log("Info: Fronius power flow returned no values (Solar.web could not reach the inverter{}); will retry".format("" if self.is_online is not False else ", which is reported offline"))
            self.mark_retry("power", FRONIUS_TTL_POWER)
            return False
        now = self._epoch()
        for name in nulls:
            self.flow.pop(name, None)
            self.flow_seen.pop(name, None)
        self.flow.update(values)
        for name in values:
            self.flow_seen[name] = now
        if FRONIUS_FLOW_PV in values:
            self.pv_seen = True
        self.flow_time = body.get("logDateTime")
        self.last_flow_ok = self._epoch()
        self.mark_refreshed("power")
        return True

    async def refresh_energy(self):
        """Read today's aggregated energy counters. One data point per channel requested."""
        today = self.utc_now().astimezone(self.local_tz).date().isoformat()
        params = {"from": today, "duration": 1, "channel": ",".join(FRONIUS_ENERGY_CHANNELS)}
        res = await self._request(FRONIUS_SCOPE_QUERY, "GET", self._path("aggrdata"), params=params)
        if not res["ok"]:
            if not res["skipped"]:
                self.mark_retry("energy", self.energy_interval)
            return False
        self._count_data_points(len(FRONIUS_ENERGY_CHANNELS))
        data = res["data"] if isinstance(res["data"], dict) else {}
        entries = data.get("data") or []
        if isinstance(entries, dict):
            entries = [entries]
        chosen = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            stamp = str(entry.get("logDate") or entry.get("logDateTime") or "")
            if stamp.startswith(today) or chosen is None:
                chosen = entry
        energy = daily_energy_kwh(channel_values((chosen or {}).get("channels")))
        if not energy:
            self.log("Info: Fronius returned no daily energy for {} (a Smart Meter is needed for these channels); will retry".format(today))
            self.mark_retry("energy", self.energy_interval)
            return False
        self.energy = energy
        self.mark_refreshed("energy")
        return True

    def telemetry(self, leaf):
        """Return one telemetry value in Predbat's units and sign convention, or None."""
        if leaf == "soc":
            return self.flow.get(FRONIUS_FLOW_SOC)
        if leaf == "battery_power":
            return predbat_battery_power(self.flow.get(FRONIUS_FLOW_BATTERY))
        if leaf == "grid_power":
            return predbat_grid_power(self.flow.get(FRONIUS_FLOW_GRID))
        if leaf == "load_power":
            return predbat_load_power(self.flow.get(FRONIUS_FLOW_LOAD))
        if leaf == "pv_power":
            value = self.flow.get(FRONIUS_FLOW_PV)
            return None if value is None else abs(value)
        return None

    def battery_capacity(self):
        """Return the battery capacity in kWh from device metadata, or 0.0 when unknown."""
        return round(as_float(self.battery_info.get("capacity_wh"), 0.0) / 1000.0, 3)

    def battery_rate_max(self):
        """Return the battery power limit in watts: the override, else the metadata's nominal maximum.

        Fronius describe maxChargePower / maxDischargePower as nominal upper bounds the battery often
        does not quite reach, which is acceptable for planning and for clamping a command.
        """
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        rates = [as_float(self.battery_info.get("max_charge_w"), 0.0), as_float(self.battery_info.get("max_discharge_w"), 0.0)]
        rates = [rate for rate in rates if rate > 0]
        return max(rates) if rates else 0.0

    def inverter_limit(self):
        """Return the inverter's nominal AC power in watts, or 0.0 when unknown."""
        return as_float(self.inverter_info.get("nominal_ac_w"), 0.0)

    # -------------------------------------------------------------------------
    # Publishing
    # -------------------------------------------------------------------------

    def _sensor_name(self, leaf):
        """Return a prefixed Fronius sensor entity id."""
        return "sensor.{}_fronius_{}_{}".format(self.prefix, self.system_short_id, leaf)

    def _control_name(self, domain, leaf):
        """Return a prefixed Fronius control entity id."""
        return "{}.{}_fronius_{}_{}".format(domain, self.prefix, self.system_short_id, leaf)

    def api_status(self):
        """Summarise the component's API health as one word for the status sensor."""
        if self.auth_failed.get(FRONIUS_SCOPE_QUERY):
            return "auth_failed"
        if self._in_backoff(FRONIUS_SCOPE_QUERY):
            return self._backoff_reason.get(FRONIUS_SCOPE_QUERY, "backing_off")
        if self.control_enable and self.auth_failed.get(FRONIUS_SCOPE_CONTROL):
            return "control_not_permitted"
        if self.control_enable and self.control_unsupported:
            return "control_unsupported"
        if self.is_online is False:
            return "offline"
        if self.last_flow_ok is None:
            return "unknown"
        return "online"

    async def publish_data(self):
        """Publish the monitoring sensors and the component status."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "load_power": "W", "pv_power": "W"}
        for leaf, unit in units.items():
            value = self.telemetry(leaf)
            if value is None:
                continue
            self.dashboard_item(
                self._sensor_name(leaf),
                state=round(value, 3),
                attributes={"unit_of_measurement": unit, "device_class": "battery" if leaf == "soc" else "power", "state_class": "measurement", "friendly_name": "Fronius {}".format(leaf.replace("_", " ").title())},
                app="fronius",
            )

        capacity = self.battery_capacity()
        if capacity > 0:
            self.dashboard_item(self._sensor_name("battery_capacity"), state=capacity, attributes={"unit_of_measurement": "kWh", "friendly_name": "Fronius Battery Capacity"}, app="fronius")
        rate_max = self.battery_rate_max()
        if rate_max > 0:
            self.dashboard_item(self._sensor_name("battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": "Fronius Battery Rate Max"}, app="fronius")
        inverter_limit = self.inverter_limit()
        if inverter_limit > 0:
            self.dashboard_item(self._sensor_name("inverter_limit"), state=round(inverter_limit), attributes={"unit_of_measurement": "W", "friendly_name": "Fronius Inverter Limit"}, app="fronius")

        # Daily counters reset at local midnight; Predbat's incrementing-sensor handling absorbs that.
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            value = self.energy.get(leaf)
            if value is None:
                continue
            self.dashboard_item(
                self._sensor_name(leaf),
                state=value,
                attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing", "friendly_name": "Fronius {}".format(leaf.replace("_", " ").title())},
                app="fronius",
            )

        current = self.planned_segments[0]["action"] if self.planned_segments and self.planned_segments[0]["start"] <= ceil_minute(self.utc_now()) else ACTION_IDLE
        self.dashboard_item(
            self._sensor_name("status"),
            state=self.api_status(),
            attributes={
                "friendly_name": "Fronius Status",
                "icon": "mdi:solar-power",
                "pv_system_id": self.pv_system_id,
                "is_online": self.is_online,
                "data_time": self.flow_time,
                "last_error": self.last_api_error,
                "data_points_today": self.data_points_today,
                "rate_limit_remaining": dict(self.rate_limit_remaining),
                "control_enable": self.control_enable,
                "control_active": self.control_active,
                "control_unsupported": self.control_unsupported,
                "current_action": current,
                "dispatch_id": self.dispatch_id,
                "schedule_hash": schedule_hash(self.sent_segments) if self.sent_segments else None,
                "schedule": segments_to_json(self.sent_segments),
                "last_write": datetime.fromtimestamp(self.last_write_time, tz=timezone.utc).isoformat() if self.last_write_time else None,
            },
            app="fronius",
        )

    # -------------------------------------------------------------------------
    # Control entities
    # -------------------------------------------------------------------------

    @staticmethod
    def _empty_schedule():
        """Return a fresh, disabled schedule shape - the single source of truth for its defaults."""
        return {
            "charge": {"enable": False, "soc": 0, "power": None, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": None, "start": "00:00:00", "end": "00:00:00"},
        }

    def _default_power(self):
        """Return the power published before Predbat has written one (see FRONIUS_DEFAULT_POWER_W)."""
        rate_max = self.battery_rate_max()
        return int(rate_max) if rate_max > 0 else FRONIUS_DEFAULT_POWER_W

    async def publish_schedule_settings_ha(self):
        """Publish the charge/export window control entities.

        Deliberately not clamped. These entities are Predbat's control surface: it writes a value and
        reads it back to confirm, so publishing anything other than what was written guarantees a
        mismatch and a retry storm. Clamping happens when the schedule is built.
        """
        rate_cap = max(int(self.battery_rate_max()), 20000)
        for direction in ("charge", "export"):
            window = self.local_schedule.get(direction, {})
            power = window.get("power")
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes Predbat
            # replace these entities with its own dummies and the window never arrives.
            self.dashboard_item(
                self._control_name("select", "battery_schedule_{}_start_time".format(direction)),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": "Fronius {} Start".format(direction.title()), "icon": "mdi:clock-outline"},
                app="fronius",
            )
            self.dashboard_item(
                self._control_name("select", "battery_schedule_{}_end_time".format(direction)),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": "Fronius {} End".format(direction.title()), "icon": "mdi:clock-outline"},
                app="fronius",
            )
            self.dashboard_item(
                self._control_name("number", "battery_schedule_{}_soc".format(direction)),
                state=int(as_float(window.get("soc"), 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Fronius {} SoC".format(direction.title()), "icon": "mdi:gauge"},
                app="fronius",
            )
            self.dashboard_item(
                self._control_name("number", "battery_schedule_{}_power".format(direction)),
                state=int(as_float(power, self._default_power())),
                attributes={"min": 0, "max": rate_cap, "step": 100, "unit_of_measurement": "W", "friendly_name": "Fronius {} Power".format(direction.title()), "icon": "mdi:flash"},
                app="fronius",
            )
            self.dashboard_item(
                self._control_name("switch", "battery_schedule_{}_enable".format(direction)),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": "Fronius {} Enable".format(direction.title()), "icon": "mdi:check-circle-outline"},
                app="fronius",
            )
        self.dashboard_item(
            self._control_name("switch", "battery_schedule_charge_write"),
            state="off",
            attributes={"friendly_name": "Fronius Schedule Write", "icon": "mdi:content-save"},
            app="fronius",
        )

    def get_schedule_settings_ha(self):
        """Read the control entities into the schedule shape build_segments consumes.

        Read every tick, the first included: Home Assistant retains these entities across a Predbat
        restart, so they already hold the live plan, and seeding from defaults would publish over it.
        A power entity that does not exist yet reads as None, which build_segments treats as "no rate
        signal" rather than as a zero (a hold).
        """
        schedule = {}
        for direction in ("charge", "export"):
            power_state = self.get_state_wrapper(self._control_name("number", "battery_schedule_{}_power".format(direction)), default=None)
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", "battery_schedule_{}_enable".format(direction)), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", "battery_schedule_{}_start_time".format(direction)), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", "battery_schedule_{}_end_time".format(direction)), default="00:00:00"),
                "soc": int(as_float(self.get_state_wrapper(self._control_name("number", "battery_schedule_{}_soc".format(direction)), default=0), 0)),
                "power": as_float(power_state),
            }
        self.local_schedule = schedule
        return schedule

    def _owns_entity(self, entity_id):
        """Return whether a control entity id belongs to this component's PV system."""
        return "_fronius_{}_".format(self.system_short_id) in str(entity_id).lower()

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

    def update_local_schedule(self, entity_id, value):
        """Apply one control-entity change to the locally held schedule."""
        leaf = str(entity_id).split("_fronius_{}_".format(self.system_short_id), 1)[-1]
        for direction in ("charge", "export"):
            prefix = "battery_schedule_{}_".format(direction)
            if not leaf.startswith(prefix):
                continue
            field = leaf[len(prefix) :]
            window = self.local_schedule.setdefault(direction, {})
            if field in ("start_time", "end_time"):
                window[field.replace("_time", "")] = str(value)
            elif field == "soc":
                window["soc"] = int(as_float(value, 0))
            elif field == "power":
                window["power"] = as_float(value)
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
        """Apply one control-entity event, or the write button.

        Any write to these entities means Predbat is driving the battery, so it arms the per-tick
        reconcile. That matters for the rate entities: a freeze or hold is signalled by a rate write
        alone, with no button press, and would otherwise never be applied on an install whose plan
        has not yet needed a window.

        The write button is NOT a forced write: the change detection still decides whether anything
        is sent. Other writes are applied by the next tick's reconcile rather than one by one, because
        Predbat writes a window as several entities in a row and each intermediate state would
        otherwise become a schedule of its own.
        """
        if not self._owns_entity(entity_id):
            return
        if not self.control_active:
            self.control_active = True
            # Persisted so a restart keeps renewing the schedule even if Predbat's plan is steady
            # and it writes nothing new for a while.
            await self.save_control()
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                await self.apply_schedule()
            return
        self.update_local_schedule(entity_id, value)
        await self.publish_schedule_settings_ha()

    # -------------------------------------------------------------------------
    # Building the schedule
    # -------------------------------------------------------------------------

    def _clamp_power(self, watts):
        """Return a command power as a non-negative int, capped at the battery's limit when known."""
        value = max(0, int(round(as_float(watts, 0.0))))
        rate_max = self.battery_rate_max()
        if rate_max > 0:
            value = min(value, int(rate_max))
        return value

    def control_soc(self):
        """Return the SoC to make control decisions on, or None when it is not fresh.

        Fresh means reported within three power-flow periods. An older figure would let a target
        check act on a battery level that has since moved by an unknown amount.
        """
        seen = self.flow_seen.get(FRONIUS_FLOW_SOC)
        if seen is None or (self._epoch() - seen) > 3 * FRONIUS_TTL_POWER * 60:
            return None
        return self.flow.get(FRONIUS_FLOW_SOC)

    @staticmethod
    def target_reached(soc, target):
        """Return whether SoC has reached a target the way Predbat counts it.

        Predbat writes targets as whole percentages rounded from kWh (calc_percent_limit), so a
        freeze at 55.6% is written as a target of 56. Comparing the raw float would read that as
        "0.4% short" and charge from the grid; rounding first matches what Predbat meant.
        """
        return soc is not None and target > 0 and int(soc + 0.5) >= target

    def _hold_params(self, charge_power):
        """Return ChargeBattery parameters for "no discharge, no grid import, solar may charge".

        MinW = 0 with MaxW at the battery's rate. The Flexibility API notes for ChargeBattery say any
        parameter combination prevents discharging, that grid energy is imported when PV produces
        less than is "necessary to charge the batteries", and that "if the minimum charge rate can be
        exceeded by the PV energy, the charge rate will increase up to the maximum requested level".
        The import is read as covering the MINIMUM only - with MinW = 0 nothing is necessary - and
        PV fills up to MaxW. The notes never say outright which bound the import serves, so this is
        the one reading here that has to be confirmed on hardware.

        When Predbat has also set the charge rate to zero there is nothing to allow, so MaxW is 0:
        Fronius's documented "keep the current level".
        """
        if charge_power is not None and charge_power <= 0:
            return {FRONIUS_PARAM_MIN_W: 0, FRONIUS_PARAM_MAX_W: 0}
        rate = self.battery_rate_max() or as_float(charge_power, 0.0) or FRONIUS_DEFAULT_POWER_W
        return {FRONIUS_PARAM_MIN_W: 0, FRONIUS_PARAM_MAX_W: self._clamp_power(rate)}

    def decide_action(self, moment, current, charge_window, export_window):
        """Return (action, params) for the period starting at moment.

        current is True only for the period that starts now. The SoC checks and the rate-derived
        states (hold, no-charge) are facts about now, so they apply to that period alone; a future
        period is decided from its windows only and re-decided once it becomes current.

        Precedence follows Predbat's own: an export window, then a charge window, then the freeze
        and hold states it signals through the rate entities.
        """
        charge = self.local_schedule.get("charge", {})
        export = self.local_schedule.get("export", {})
        soc = self.control_soc()
        charge_power = charge.get("power")
        export_power = export.get("power")
        # A discharge rate of zero is how Predbat signals freeze charge, hold charging and every other
        # "do not discharge" hold on an inverter without a pause control.
        discharge_held = export_power is not None and export_power <= 0

        if export_window and export_window[0] <= moment < export_window[1] and as_float(export_power, 0.0) > 0:
            # With target_soc_used_for_discharge, Predbat writes the export target to charge_limit
            # (this component's charge SoC entity); discharge_target_soc only ever carries the reserve
            # (inverter.adjust_force_export). The higher of the two is the floor to stop at.
            target = max(as_float(charge.get("soc"), 0.0), as_float(export.get("soc"), 0.0))
            # Stop forcing export once the target is reached. Predbat itself then drops to Demand
            # for the rest of the window; until its next cycle this period falls through below.
            if not (current and soc is not None and soc <= target):
                power = self._clamp_power(export_power)
                return ACTION_DISCHARGE, {FRONIUS_PARAM_MIN_W: power, FRONIUS_PARAM_MAX_W: power}

        if charge_window and charge_window[0] <= moment < charge_window[1]:
            target = as_float(charge.get("soc"), 0.0)
            # Freeze charge and hold charging both keep the window enabled, set the target to the
            # current level and zero the discharge rate. That hold signal wins inside the window, so
            # a freeze can never become a grid charge on a fractional SoC.
            if current and discharge_held:
                return ACTION_HOLD, self._hold_params(charge_power)
            # ChargeBattery has no target SoC, so reaching it is detected here and turned into a
            # hold for the rest of the window rather than charging on towards 100%.
            if current and self.target_reached(soc, target):
                return ACTION_HOLD, self._hold_params(charge_power)
            if as_float(charge_power, 0.0) > 0:
                power = self._clamp_power(charge_power)
                return ACTION_CHARGE, {FRONIUS_PARAM_MIN_W: power, FRONIUS_PARAM_MAX_W: power}
            return ACTION_HOLD, self._hold_params(charge_power)

        if current:
            if discharge_held:
                return ACTION_HOLD, self._hold_params(charge_power)
            # A charge rate of zero is how it signals freeze export: cover the load from the
            # battery if needed, but never charge it, so surplus solar goes to the grid.
            if charge_power is not None and charge_power <= 0 and as_float(export_power, 0.0) > 0:
                return ACTION_NO_CHARGE, {FRONIUS_PARAM_MIN_W: 0, FRONIUS_PARAM_MAX_W: self._clamp_power(export_power)}

        if self.grid_export_limit is not None:
            return ACTION_EXPORT_LIMIT, {FRONIUS_PARAM_EXPORT_LIMIT_W: self.grid_export_limit}
        return ACTION_IDLE, {}

    def build_segments(self, now):
        """Build the schedule Predbat wants from now to the horizon, as merged segments.

        Returns (segments, start, end). The period boundaries are the start, the horizon and every
        window edge in between, so each period carries exactly one decision. Windows are mapped to
        real UTC instants from the Predbat timezone's wall clock (see window_to_utc), which is what
        keeps a window right across a DST change.
        """
        start = ceil_minute(now)
        end = horizon_end(start)
        charge = self.local_schedule.get("charge", {})
        export = self.local_schedule.get("export", {})
        charge_window = window_to_utc(charge.get("start"), charge.get("end"), now, self.local_tz) if charge.get("enable") else None
        export_window = window_to_utc(export.get("start"), export.get("end"), now, self.local_tz) if export.get("enable") else None

        points = {start, end}
        for window in (charge_window, export_window):
            if not window:
                continue
            for edge in window:
                if start < edge < end:
                    points.add(edge)
        ordered = sorted(points)
        segments = []
        for index in range(len(ordered) - 1):
            lo, hi = ordered[index], ordered[index + 1]
            action, params = self.decide_action(lo, lo == start, charge_window, export_window)
            segments.append(make_segment(lo, hi, action, params))
        return merge_segments(segments), start, end

    @staticmethod
    def sendable(segments):
        """Keep only the segments that become commands: non-idle and at least a minute long."""
        return [segment for segment in segments if segment["action"] != ACTION_IDLE and (segment["end"] - segment["start"]).total_seconds() >= FRONIUS_MIN_SEGMENT_SECONDS]

    # -------------------------------------------------------------------------
    # The write path
    # -------------------------------------------------------------------------

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the inverter."""
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    def _other_shape(self, shape):
        """Return the other documented dispatchParameters shape."""
        return FRONIUS_PARAMS_LIST if shape == FRONIUS_PARAMS_OBJECT else FRONIUS_PARAMS_OBJECT

    async def put_schedule(self, segments):
        """Send a schedule, replacing every earlier one for this PV system. True when accepted.

        Tries the current dispatchParameters shape first and, until one has been confirmed, retries
        once with the other shape when the API rejects the request as malformed. The first accepted
        schedule is read back (verify_schedule) because an API that silently ignored a shape would
        otherwise look exactly like success. See FRONIUS_PARAMS_* in fronius_const.py.
        """
        prefix = "predbat-{}".format(schedule_hash(segments))
        shapes = [self.params_shape]
        if not self.params_shape_confirmed:
            shapes.append(self._other_shape(self.params_shape))
        for attempt, shape in enumerate(shapes):
            commands = segments_to_commands(segments, prefix, shape)
            res = await self._request(FRONIUS_SCOPE_CONTROL, "PUT", self._path("schedules"), body=commands)
            if res["ok"]:
                data = res["data"] if isinstance(res["data"], dict) else {}
                # Every PUT supersedes the previous schedule, so the old id no longer names anything
                # live. Keeping it would make a later cancel address the wrong schedule.
                self.dispatch_id = data.get("dispatchId")
                if not self.dispatch_id:
                    self.log("Warn: Fronius accepted the schedule but returned no dispatchId; it cannot be cancelled early and will run out at its own end")
                if shape != self.params_shape:
                    self.log("Info: Fronius accepted dispatchParameters in the {} form".format(shape))
                self.params_shape = shape
                if not self.params_shape_confirmed:
                    return await self.verify_schedule(segments, shape)
                return True
            if res["skipped"]:
                return False
            if res["code"] in FRONIUS_UNSUPPORTED_CONTROL_CODES:
                self.control_unsupported = describe_error(res["code"], res["message"])
                self.log("Warn: Fronius cannot battery-control PV system {}: {}. Predbat will keep monitoring it".format(self.pv_system_id, self.control_unsupported))
                return False
            if attempt + 1 < len(shapes) and (res["status"] in (400, 422) or res["code"] in FRONIUS_SHAPE_ERROR_CODES):
                self.log("Info: Fronius rejected the schedule as malformed; retrying once with dispatchParameters in the {} form".format(shapes[attempt + 1]))
                continue
            return False
        return False

    async def verify_schedule(self, segments, shape):
        """Read the first accepted schedule back once to confirm its parameters were taken.

        Returns True when the schedule may be treated as sent. If the read-back shows commands with
        no parameters, the shape was silently ignored: the other shape is adopted and False returned,
        so the schedule is re-sent on the next eligible tick. An unreadable answer proves nothing and
        leaves the shape unconfirmed. After two empty read-backs (one per shape) the read-back itself
        is presumed not to echo parameters, and the current shape is kept.
        """
        if not self.dispatch_id:
            return True
        res = await self._request(FRONIUS_SCOPE_CONTROL, "GET", self._path("schedule", dispatch_id=self.dispatch_id))
        data = res["data"]
        commands = data.get("commands") if isinstance(data, dict) else data
        if not res["ok"] or not isinstance(commands, list) or not commands:
            return True
        wanted = [segment["params"] for segment in segments if segment_dispatch_type(segment["action"])]
        got = [normalise_params(command.get("dispatchParameters")) for command in commands if isinstance(command, dict)]
        if any(got) and (not wanted or wanted[0] in got):
            self.params_shape_confirmed = True
            self.log("Info: Fronius confirmed dispatchParameters in the {} form".format(shape))
            return True
        self._shape_verify_failures += 1
        if self._shape_verify_failures >= 2:
            self.params_shape_confirmed = True
            self.log("Warn: Fronius schedule read-back never shows parameters in either form; keeping the {} form".format(shape))
            return True
        self.params_shape = self._other_shape(shape)
        self.log("Warn: Fronius accepted the schedule but its read-back has no parameters in the {} form; re-sending in the {} form".format(shape, self.params_shape))
        return False

    async def cancel_schedule(self):
        """Cancel the last schedule so the inverter returns to normal operation. True when done.

        Addressed by dispatch id. Without one, an empty replacement schedule is tried (a new schedule
        supersedes the old). If the API refuses that too, nothing can stop the forced schedule early:
        that is logged once and the state kept, and it runs out at its own end (at most the horizon).
        """
        if self.dispatch_id:
            res = await self._request(FRONIUS_SCOPE_CONTROL, "DELETE", self._path("schedule", dispatch_id=self.dispatch_id))
            if res["ok"] or res["code"] in FRONIUS_CANCEL_DONE_CODES:
                self.dispatch_id = None
                return True
            return False
        res = await self._request(FRONIUS_SCOPE_CONTROL, "PUT", self._path("schedules"), body=[])
        if res["ok"]:
            data = res["data"] if isinstance(res["data"], dict) else {}
            self.dispatch_id = data.get("dispatchId")
            return True
        if not res["skipped"]:
            ends = [segment["end"] for segment in self.sent_segments]
            self._cancel_blocked_until = max(ends).timestamp() if ends else None
            self.log("Warn: Fronius cannot cancel the running schedule (no dispatch id, and an empty replacement was refused); it will run out by itself at {}".format(zulu(max(ends)) if ends else "its end"))
        return False

    async def apply_schedule(self, force=False):
        """Build the schedule and send it if it would change what the inverter does. True when in step.

        "In step" covers both "sent and accepted" and "nothing needed sending". False covers a held,
        skipped or rejected write - a caller must not read either as the inverter matching the plan.
        """
        if not self.control_enable or self.control_unsupported or self._is_read_only():
            return False
        now = self.utc_now()
        segments, start, _ = self.build_segments(now)
        self.planned_segments = segments
        wanted = self.sendable(segments)
        # A schedule that has fully run out is no longer outstanding, whatever happened to it.
        if self.sent_segments and not any(segment["end"] > start for segment in self.sent_segments):
            self.sent_segments = []
            self._cancel_blocked_until = None
        compare_end = start + timedelta(minutes=FRONIUS_LOOKAHEAD_MINUTES)
        if not force and segments_equivalent(self.sent_segments, wanted, start, compare_end):
            return True
        epoch = now.timestamp()
        if self._in_backoff(FRONIUS_SCOPE_CONTROL):
            # Parked after a 403/429/maintenance: _request already said why. Report the held change
            # once per backoff period rather than warning and re-saving state every tick.
            until = self._backoff_until.get(FRONIUS_SCOPE_CONTROL)
            if self._parked_reported != until:
                self._parked_reported = until
                self.log("Info: Fronius schedule change held while control calls are paused ({})".format(self._backoff_reason.get(FRONIUS_SCOPE_CONTROL, "backing off")))
            return False
        if not wanted and self._cancel_blocked_until is not None:
            if epoch < self._cancel_blocked_until:
                return False
            self._cancel_blocked_until = None
            self.sent_segments = []
            return True
        if not force and self.last_write_time is not None and (epoch - self.last_write_time) < FRONIUS_MIN_WRITE_SECONDS:
            self.log("Info: Fronius schedule change held for {}s to pace writes; it will be sent on a later tick".format(FRONIUS_MIN_WRITE_SECONDS))
            return False
        # Stamped on every attempt, success or not, so a persistently rejected write is paced rather
        # than retried every tick.
        self.last_write_time = epoch
        if not wanted:
            if not any(segment["end"] > start for segment in self.sent_segments):
                self.sent_segments = []
                return True
            ok = await self.cancel_schedule()
            if ok:
                self.sent_segments = []
                self.log("Info: Fronius schedule cancelled; the inverter returns to its own self-consumption")
            await self.save_control()
            return ok
        ok = await self.put_schedule(wanted)
        if ok:
            self.sent_segments = wanted
            self._cancel_blocked_until = None
            summary = ", ".join("{} {}-{}".format(seg["action"], seg["start"].strftime("%H:%MZ"), seg["end"].strftime("%H:%MZ")) for seg in wanted)
            self.log("Info: Fronius schedule {} sent (dispatch {}): {}".format(schedule_hash(wanted), self.dispatch_id, summary))
        else:
            self.log("Warn: Fronius schedule was not accepted; it will be retried")
        await self.save_control()
        return ok

    async def _reconcile_control(self):
        """Re-apply the schedule on every tick once Predbat is driving the battery.

        The schedule is time-aware - crossing a window edge, reaching a target or moving the horizon
        changes it with no plan change at all - so it is re-evaluated every tick, not only on a
        button press. Gated on read-only because that write would otherwise originate here, outside
        Predbat's own read-only handling.
        """
        if not self.control_active or not self.control_enable or self._is_read_only():
            return
        try:
            await self.apply_schedule()
        except Exception as error:
            self.log("Warn: Fronius control apply failed: {}".format(error))

    # -------------------------------------------------------------------------
    # Automatic configuration and discovery
    # -------------------------------------------------------------------------

    async def automatic_config(self):
        """Register the PV system as a FroniusCloud Predbat inverter."""
        self.set_arg_auto("inverter_type", ["FroniusCloud"])
        self.set_arg_auto("num_inverters", 1)
        self.set_arg_auto("soc_percent", [self._sensor_name("soc")])
        self.set_arg_auto("battery_power", [self._sensor_name("battery_power")])
        self.set_arg_auto("grid_power", [self._sensor_name("grid_power")])
        self.set_arg_auto("load_power", [self._sensor_name("load_power")])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name("pv_power")])
        # The published sensors already follow Predbat's sign conventions, so the invert flags are
        # owned here rather than inherited from whatever else configured this install.
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False])

        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if self.energy.get(leaf) is not None:
                self.set_arg_auto(leaf, [self._sensor_name(leaf)])
            else:
                self.log("Warn: Fronius does not report {} yet (it needs a Fronius Smart Meter), it must be set manually in apps.yaml".format(leaf))

        if self.battery_capacity() > 0:
            self.set_arg_auto("soc_max", [self._sensor_name("battery_capacity")])
        else:
            self.log("Warn: Fronius reports no battery capacity, soc_max must be set manually in apps.yaml")
        if self.battery_rate_max() > 0:
            self.set_arg_auto("battery_rate_max", [self._sensor_name("battery_rate_max")])
        else:
            self.log("Warn: Fronius reports no battery power limit, battery_rate_max must be set manually in apps.yaml or with fronius_battery_rate_max")
        if self.inverter_limit() > 0:
            self.set_arg_auto("inverter_limit", [self._sensor_name("inverter_limit")])
        # Not auto-mapped: nothing here is the SITE's grid export cap, which can sit well below the
        # inverter rating, and a guess would over-export silently.
        self.log("Warn: Fronius does not report an export power limit; set export_limit in apps.yaml if your grid connection is capped below the inverter rating")

        self.set_arg_auto("charge_start_time", [self._control_name("select", "battery_schedule_charge_start_time")])
        self.set_arg_auto("charge_end_time", [self._control_name("select", "battery_schedule_charge_end_time")])
        self.set_arg_auto("charge_limit", [self._control_name("number", "battery_schedule_charge_soc")])
        self.set_arg_auto("charge_rate", [self._control_name("number", "battery_schedule_charge_power")])
        self.set_arg_auto("scheduled_charge_enable", [self._control_name("switch", "battery_schedule_charge_enable")])
        self.set_arg_auto("discharge_start_time", [self._control_name("select", "battery_schedule_export_start_time")])
        self.set_arg_auto("discharge_end_time", [self._control_name("select", "battery_schedule_export_end_time")])
        self.set_arg_auto("discharge_target_soc", [self._control_name("number", "battery_schedule_export_soc")])
        self.set_arg_auto("discharge_rate", [self._control_name("number", "battery_schedule_export_power")])
        self.set_arg_auto("scheduled_discharge_enable", [self._control_name("switch", "battery_schedule_export_enable")])
        self.set_arg_auto("schedule_write_button", [self._control_name("switch", "battery_schedule_charge_write")])

    def _discovery_entities(self):
        """The settings automatic_config() binds, as discovery entity descriptors.

        Each entity id comes from the same _sensor_name()/_control_name() call automatic_config()
        makes, so the two cannot drift apart. There is no reserve: the API cannot set one, so the
        row says has_reserve_soc False and Predbat supplies its own dummy.
        """
        entities = {
            "soc_percent": {"entity_id": self._sensor_name("soc"), "access": "r", "unit": "%"},
            "battery_power": {"entity_id": self._sensor_name("battery_power"), "access": "r", "unit": "W"},
            "grid_power": {"entity_id": self._sensor_name("grid_power"), "access": "r", "unit": "W"},
            "load_power": {"entity_id": self._sensor_name("load_power"), "access": "r", "unit": "W"},
            "pv_power": {"entity_id": self._sensor_name("pv_power"), "access": "r", "unit": "W"},
        }
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if self.energy.get(leaf) is not None:
                entities[leaf] = {"entity_id": self._sensor_name(leaf), "access": "r", "unit": "kWh"}
        if self.battery_capacity() > 0:
            entities["soc_max"] = {"entity_id": self._sensor_name("battery_capacity"), "access": "r", "unit": "kWh"}
        if self.battery_rate_max() > 0:
            entities["battery_rate_max"] = {"entity_id": self._sensor_name("battery_rate_max"), "access": "r", "unit": "W"}
        if self.inverter_limit() > 0:
            entities["inverter_limit"] = {"entity_id": self._sensor_name("inverter_limit"), "access": "r", "unit": "W"}
        for direction, prefix in (("charge", "charge"), ("export", "discharge")):
            entities["{}_start_time".format(prefix)] = {"entity_id": self._control_name("select", "battery_schedule_{}_start_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["{}_end_time".format(prefix)] = {"entity_id": self._control_name("select", "battery_schedule_{}_end_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["scheduled_{}_enable".format(prefix)] = {"entity_id": self._control_name("switch", "battery_schedule_{}_enable".format(direction)), "access": "rw", "domain": "switch"}
        entities["charge_limit"] = {"entity_id": self._control_name("number", "battery_schedule_charge_soc"), "access": "rw", "unit": "%"}
        entities["charge_rate"] = {"entity_id": self._control_name("number", "battery_schedule_charge_power"), "access": "rw", "unit": "W", "step": 100}
        entities["discharge_target_soc"] = {"entity_id": self._control_name("number", "battery_schedule_export_soc"), "access": "rw", "unit": "%"}
        entities["discharge_rate"] = {"entity_id": self._control_name("number", "battery_schedule_export_power"), "access": "rw", "unit": "W", "step": 100}
        entities["schedule_write_button"] = {"entity_id": self._control_name("switch", "battery_schedule_charge_write"), "access": "rw", "domain": "switch"}
        return entities

    def build_discovery(self):
        """Describe the PV system for the discovery catalogue, or None before the first telemetry read.

        Reads only data already held, so it adds no API calls. Battery SoH and similar moving values
        are deliberately left out: refresh_discovery() compares whole reports, so a moving value
        would re-file the report every cycle.
        """
        if not self.pv_system_id or (not self.flow and not self.battery_info):
            return None
        info = {}
        model = self.inverter_info.get("model")
        if model:
            info["model"] = model
        ratings = {}
        if self.battery_capacity() > 0:
            ratings["soc_max"] = self.battery_capacity()
        if self.battery_rate_max() > 0:
            ratings["battery_rate_max"] = self.battery_rate_max()
        if self.inverter_limit() > 0:
            ratings["inverter_limit"] = self.inverter_limit()
        hardware_ids = {"pv_system_id": self.pv_system_id}
        if self.inverter_info.get("serial"):
            hardware_ids["serial"] = self.inverter_info["serial"]
        record = inverter_record(
            "fronius:{}".format(self.pv_system_id),
            inverter_type="FroniusCloud",
            control=bool(self.control_enable),
            composition="direct",
            functions=["solar", "battery"] if self.pv_seen else ["battery"],
            capabilities=dict(FRONIUS_CAPABILITIES),
            hardware_ids=hardware_ids,
            info=info,
            ratings=ratings,
            entities=self._discovery_entities(),
        )
        return {"automatic": self.automatic, "inverters": [record]}

    def health_message(self):
        """Return a short reason the component is unhealthy, for the run status, or None."""
        if self.auth_failed.get(FRONIUS_SCOPE_QUERY):
            return "Fronius access key rejected ({})".format(self.last_api_error or "check the keys")
        if self.control_enable and self.auth_failed.get(FRONIUS_SCOPE_CONTROL):
            return "Fronius control not permitted - Flexibility API contract or owner consent missing"
        if self.control_enable and self.control_unsupported:
            return "Fronius cannot control this system: {}".format(self.control_unsupported)
        return None

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    async def load_cache(self, name):
        """Load one cache blob from the Storage component, or None when unavailable."""
        storage = self.storage
        if storage is None:
            return None
        try:
            return await storage.load(FRONIUS_STORAGE_MODULE, name)
        except Exception as error:
            self.log("Warn: Fronius could not load the {} cache: {}".format(name, error))
            return None

    async def save_cache(self, name, data):
        """Save one cache blob to the Storage component, ignoring an absent Storage."""
        storage = self.storage
        if storage is None:
            return
        try:
            await storage.save(FRONIUS_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log("Warn: Fronius could not save the {} cache: {}".format(name, error))

    async def save_static(self):
        """Persist device metadata so a restart does not spend a data point re-reading it."""
        await self.save_cache(FRONIUS_CACHE_STATIC, {"pv_system_id": self.pv_system_id, "battery_info": self.battery_info, "inverter_info": self.inverter_info})

    async def save_control(self):
        """Persist the last schedule sent, so a restart does not re-send an identical one."""
        await self.save_cache(
            FRONIUS_CACHE_CONTROL,
            {
                "pv_system_id": self.pv_system_id,
                "sent_segments": segments_to_json(self.sent_segments),
                "dispatch_id": self.dispatch_id,
                "params_shape": self.params_shape,
                "params_shape_confirmed": self.params_shape_confirmed,
                "last_write_time": self.last_write_time,
                "control_active": self.control_active,
            },
        )

    async def restore_state(self):
        """Restore the caches on the first cycle. A cache for a different PV system is ignored."""
        static = await self.load_cache(FRONIUS_CACHE_STATIC)
        if isinstance(static, dict) and static.get("pv_system_id") == self.pv_system_id:
            self.battery_info = dict(static.get("battery_info") or {})
            self.inverter_info = dict(static.get("inverter_info") or {})
            if self.battery_info:
                # Aged so the static tier still re-reads within a few hours of a restart.
                self.mark_refreshed("static", age_minutes=FRONIUS_TTL_STATIC * 3 / 4)
        control = await self.load_cache(FRONIUS_CACHE_CONTROL)
        if isinstance(control, dict) and control.get("pv_system_id") == self.pv_system_id:
            self.sent_segments = segments_from_json(control.get("sent_segments"))
            self.dispatch_id = control.get("dispatch_id")
            if control.get("params_shape") in (FRONIUS_PARAMS_LIST, FRONIUS_PARAMS_OBJECT):
                self.params_shape = control["params_shape"]
                self.params_shape_confirmed = bool(control.get("params_shape_confirmed"))
            self.last_write_time = as_float(control.get("last_write_time"))
            # Predbat was driving this system before the restart. Without this, a steady plan writes
            # no entity, nothing re-arms the reconcile, and the restored schedule silently lapses.
            self.control_active = bool(control.get("control_active")) or self.control_active
        self._cache_restored = True

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def missing_config(self):
        """Return the config keys that must be set before the component can run."""
        missing = []
        if not self.access_key_id:
            missing.append("fronius_access_key_id")
        if not self.access_key_value:
            missing.append("fronius_access_key_value")
        if not self.pv_system_id:
            missing.append("fronius_pv_system_id")
        return missing

    async def run(self, seconds, first):
        """Main component tick: refresh each tier when due, publish, and keep the schedule in step.

        Returns True on a completed cycle and False to stay in ComponentBase's startup backoff, which
        is also what keeps bad keys from being retried every minute at startup.
        """
        if first and not self._cache_restored:
            await self.restore_state()
        missing = self.missing_config()
        if missing:
            self.log("Warn: Fronius needs {} in apps.yaml".format(", ".join(missing)))
            return False

        if self.tier_expired("static", FRONIUS_TTL_STATIC):
            if await self.refresh_static():
                await self.save_static()
        if self.tier_expired("power", FRONIUS_TTL_POWER):
            await self.refresh_power()
        if self.tier_expired("energy", self.energy_interval):
            await self.refresh_energy()

        try:
            self.get_schedule_settings_ha()
        except Exception as error:
            self.log("Warn: Fronius schedule read failed: {}".format(error))
        await self._reconcile_control()
        await self.publish_schedule_settings_ha()
        await self.publish_data()
        self.refresh_discovery()

        if first and not self.flow:
            # automatic_config() runs on the first cycle alone, so starting without telemetry would
            # skip soc_max and the energy args for the whole session.
            self.log("Warn: Fronius first power-flow read returned nothing, deferring startup; it will be retried after a backoff")
            return False
        if first and self.automatic:
            await self.automatic_config()
        # Healthy only while telemetry is fresh, so a component that has lost its data shows up as
        # unhealthy rather than quietly serving stale values.
        if self.last_flow_ok is not None and (self._epoch() - self.last_flow_ok) < 3 * FRONIUS_TTL_POWER * 60:
            self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state on shutdown.

        The schedule is deliberately NOT cancelled. On a restart that would interrupt a planned
        charge for no reason, and on a real shutdown the schedule runs out by itself within the
        horizon - the dead-man's switch described in the module docstring.
        """
        await self.save_static()
        await self.save_control()
