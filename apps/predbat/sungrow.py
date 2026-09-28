# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sungrow iSolarCloud OpenAPI Library
# -----------------------------------------------------------------------------

"""Sungrow iSolarCloud OpenAPI integration for Predbat.

Registers each discovered energy-storage (hybrid) inverter as a ``SungrowCloud`` Predbat
inverter, publishing monitoring sensors and the usual schedule control entities, and
translating Predbat's plan into Sungrow's INSTANTANEOUS external-dispatch commands.

THE HEARTBEAT IS THE CENTRAL FACT OF THIS COMPONENT.

Sungrow offer TWO control paths, and this component drives the instantaneous one.

The path taken here is external energy dispatch: 10003 mode, 10004 charge/discharge/stop,
10005 power. It carries no time at all - there is no "charge between 02:00 and 05:00" in it,
only "charge at 3000 W, now" - and control is held open by parameter 10017, the External EMS
Heartbeat. Predbat declares an interval of 1-1000 seconds and must keep writing that
parameter inside it. If the beats stop, the inverter drops external dispatch and returns to
self-consumption by itself.

The path NOT taken is the forced-charging schedule, 10065-10076, which does carry times
(start and end hour/minute for two windows, each with a target SoC). It was rejected as the
primary path for four reasons, in order of weight:

  1. It is CHARGE ONLY. Nothing in the published control table is a forced-DISCHARGE window,
     so Predbat's export windows could not be expressed through it at all. External dispatch
     would still be needed for export, and running two control mechanisms against one battery
     is worse than running one.
  2. It carries no power setpoint - only a target SoC - so Predbat's charge rate, and the
     rate-zero it uses to signal freeze, have nowhere to go.
  3. It is two windows, recurring daily (10066 is weekdays/everyday). Predbat routinely plans
     more than two charge slots across 48 hours, and plans them for a specific date; a window
     written today fires again tomorrow.
  4. It persists in the inverter. That is a real advantage when Predbat is unreachable, and a
     real hazard when Predbat is unreachable AND its last plan has gone stale.

Reason 4 cuts both ways, which is why build_forced_charge_params() exists and is offered
behind sungrow_forced_charge_schedule (default off) - see that method. Reasons 1 to 3 are
what make the schedule insufficient as the primary path regardless of the safety argument.

The cost of the choice made here is stated plainly: with instantaneous control, Predbat must
stay alive and reachable every few minutes for the battery to follow the plan. A network
partition loses the rest of a charge window. The schedule path would not have, and that is
exactly the trade the opt-in flag offers.

That revert is a SAFETY PROPERTY, not a limitation, and nothing here should try to defeat
it. If Predbat is killed mid-charge, crashes, loses its network or is simply stopped, the
battery goes back to ordinary self-consumption within one heartbeat interval. It does not
sit stopped with the house on the grid, and it does not hold a stale forced setpoint. The
heartbeat therefore runs as its own asyncio task with its own cadence (see
``_heartbeat_loop``), not as something run() does when it happens to tick.

So Predbat's window entities are read every cycle and evaluated AGAINST THE CLOCK here:
"is a charge window active this minute" becomes "command charge at this power now", and
leaving the window becomes "release control". The windows are never written to the
inverter.

WRITES ARE ASYNCHRONOUS. There is no synchronous PUT. A write is three calls:
paramSettingCheck (does this device accept parameter configuration at all), paramSetting
(queue a task, receive a task_id) and getParamSettingTask (poll until the task leaves the
running state). ``_write_params`` owns that flow; every caller sees one awaitable that
returns True only when the inverter is known to have taken the values.

SCALING IS UNVERIFIED. Appendix 10 does not publish the coefficients - its own words are
"refer to the external communication protocols for inverters to determine the
coefficients", and those protocols are per-model. Everything assumed lives in one table in
sungrow_const.py under a banner. A wrong coefficient is silently 10x or 100x out.

Auth is OAuth 2.0 and follows fox.py/teslemetry.py exactly: this component never runs the
OAuth dance. It holds an access token plus its expiry and hash, refreshes through
predbat.com's oauth-refresh edge function before a call, and retries once on a 401.
Requests additionally carry the application's appkey in the body and its x-access-key in
the headers, neither of which is an OAuth credential.
"""

import asyncio
import json
import time
import aiohttp
from component_base import ComponentBase
from coordinator import inverter_record
from oauth_mixin import OAuthMixin
from sungrow_const import (
    SUNGROW_ENDPOINTS,
    SUNGROW_LANG,
    SUNGROW_TIMEOUT,
    SUNGROW_RETRIES,
    SUNGROW_RESULT_OK,
    SUNGROW_CHECK_OK,
    SUNGROW_CHECK_SUPPORTED,
    SUNGROW_CHECK_UNSUPPORTED,
    SUNGROW_DEV_RESULT_OK,
    SUNGROW_TASK_RUNNING,
    SUNGROW_TASK_DONE,
    SUNGROW_TASK_FIRST_POLL_SECONDS,
    SUNGROW_TASK_POLL_SECONDS,
    SUNGROW_TASK_TIMEOUT_SECONDS,
    SUNGROW_TASK_EXPIRE_SECONDS,
    SUNGROW_SET_TYPE_WRITE,
    SUNGROW_SET_TYPE_READ,
    SUNGROW_ESS_DEVICE_TYPES,
    SUNGROW_PARAM_SOC_UPPER,
    SUNGROW_PARAM_SOC_LOWER,
    SUNGROW_PARAM_EMS_MODE,
    SUNGROW_PARAM_COMMAND,
    SUNGROW_PARAM_POWER,
    SUNGROW_PARAM_HEARTBEAT,
    SUNGROW_PARAM_FORCED_CHARGING,
    SUNGROW_PARAM_FORCED_VALID_TIME,
    SUNGROW_PARAM_FORCED_START_HOUR_1,
    SUNGROW_PARAM_FORCED_START_MIN_1,
    SUNGROW_PARAM_FORCED_END_HOUR_1,
    SUNGROW_PARAM_FORCED_END_MIN_1,
    SUNGROW_PARAM_FORCED_TARGET_SOC_1,
    SUNGROW_PARAM_FORCED_START_HOUR_2,
    SUNGROW_PARAM_FORCED_START_MIN_2,
    SUNGROW_PARAM_FORCED_END_HOUR_2,
    SUNGROW_PARAM_FORCED_END_MIN_2,
    SUNGROW_PARAM_FORCED_TARGET_SOC_2,
    SUNGROW_EMS_SELF_CONSUMPTION,
    SUNGROW_EMS_EXTERNAL_DISPATCH,
    SUNGROW_CMD_CHARGE,
    SUNGROW_CMD_DISCHARGE,
    SUNGROW_CMD_STOP,
    SUNGROW_ENABLE,
    SUNGROW_DISABLE,
    SUNGROW_FORCED_EVERYDAY,
    SUNGROW_HEARTBEAT_DEFAULT_SECONDS,
    SUNGROW_ESS_POINTS,
    SUNGROW_ESS_ENERGY,
    SUNGROW_ERROR_CODES,
    SUNGROW_CLOCK_ERROR_CODE,
    SUNGROW_CHECK_RESULTS,
    SUNGROW_PARAM_STATUS,
    SUNGROW_PARAM_STATUS_SUCCESS,
    SUNGROW_DEVICE_ESS,
    SUNGROW_MAX_POINTS_PER_REQUEST,
    SUNGROW_AUTH_ERROR_CODES,
    SUNGROW_RATE_LIMIT_CODES,
    SUNGROW_PLANT_POINTS,
    SUNGROW_PLANT_TELEMETRY,
    SUNGROW_PLANT_ENERGY,
    SUNGROW_WH_TO_KWH,
    SUNGROW_STORAGE_MODULE,
    SUNGROW_CACHE_STATIC,
    SUNGROW_CACHE_CONTROL,
    SUNGROW_TTL_STATIC,
    SUNGROW_TTL_POWER,
    SUNGROW_DEBUG_REDACT_KEYS,
    gateway_url,
    strip_point_prefix,
    as_float,
    scale_for_write,
    heartbeat_send_interval,
    heartbeat_declared_interval,
    signed_battery_power,
    signed_grid_power,
    hhmmss_to_hour_minute,
    hm_to_minutes,
    window_is_empty,
    param_entry,
    describe_param,
)

# The behaviour a SungrowCloud inverter has, stated for the discovery record's capabilities.
# A literal, never read back from INVERTER_DEF: the record has to rebuild the row on its own,
# or the completeness test proves nothing.
#
# charge_control_immediate is False even though the underlying commands are instantaneous.
# The flag describes the CONTROL SURFACE Predbat is given, not the wire protocol: this
# component takes ordinary charge/export windows and does the "is it now" arithmetic itself
# (see decide_command), because the external-dispatch parameters it drives carry no time for
# Predbat to write a window into. Sungrow's forced-charging parameters DO carry times, and
# the module docstring says why they are not the path taken. support_feedin_first is False -
# there is no feed-in-first mode in the control parameter table.
SUNGROW_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}


class SungrowAPI(ComponentBase, OAuthMixin):
    """Sungrow iSolarCloud OpenAPI cloud component."""

    # Trace every API request/response while the Sungrow integration beds in. Nobody on the
    # project has Sungrow hardware, so a tester's log is the only evidence available for the
    # inferred behaviour - and for the unverified scaling coefficients in particular. Flip to
    # False once those are confirmed against a real inverter.
    api_debug = True

    def initialize(
        self,
        appkey="",
        access_key="",
        key="",
        gateway="",
        auth_method=None,
        token_expires_at=None,
        token_hash=None,
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=True,
        battery_rate_max=None,
        heartbeat_interval=SUNGROW_HEARTBEAT_DEFAULT_SECONDS,
        forced_charge_schedule=False,
        api_delay=2,
        min_write_interval=300,
        **kwargs,
    ):
        """Initialise the Sungrow component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has
        already resolved each arg from its sungrow_* config key and passes it BY ARG NAME
        (e.g. appkey <- sungrow_appkey), exactly like fox/deye/alphaess. Consume the kwargs
        directly - do NOT re-derive with get_arg("appkey"): that bare name is not in
        apps.yaml, so it would always return the default.
        """
        self.log("Info: SungrowAPI initialising")
        self.appkey = appkey or ""
        self.access_key = access_key or ""
        self.base_url = gateway_url(gateway)
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_rate_max_override = float(battery_rate_max) if battery_rate_max else 0.0
        self.heartbeat_interval = int(as_float(heartbeat_interval, SUNGROW_HEARTBEAT_DEFAULT_SECONDS))
        self.forced_charge_schedule = bool(forced_charge_schedule)
        self.api_delay = max(0, float(api_delay or 0))
        self.min_write_interval = max(0, int(min_write_interval or 0))

        # OAuth is wired exactly as fox.py does it - the mixin assigns 'key' to access_token
        # when auth_method is oauth, and the oauth-refresh edge function owns the refresh chain.
        self._init_oauth(auth_method, key, token_expires_at, "sungrow")
        self.token_hash = token_hash or ""

        self.plant_list = []
        self.plant_detail = {}
        self.plant_values = {}
        self.plant_energy = {}
        # Every per-device map is keyed by the device UUID, because that is what the Grid
        # Control endpoints address. The serial is display only and is what entity ids are
        # built from, so the two must never be confused - a write addressed by serial is
        # simply rejected.
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_energy = {}
        self.local_schedule = {}
        self.applied_command = {}
        self.last_write_time = {}
        # Devices Predbat has actually been asked to drive, i.e. whose write button has been
        # pressed at least once. The reconcile loop only re-applies for these, so a startup
        # cycle can never take control of an inverter before there is a plan to apply.
        self.control_active = set()
        # Devices currently held in external dispatch. This is what the heartbeat loop beats
        # for, and it is deliberately NOT persisted: after a restart the inverter has already
        # reverted to self-consumption (or is about to), so restoring "we hold control" would
        # be a claim about hardware state that is no longer true.
        self.control_held = set()
        self._heartbeat_task = None
        self._heartbeat_stop = False
        # Per-device "does this device accept parameter configuration" verdict from
        # paramSettingCheck. Cached because it is a property of the hardware and the account
        # tier, not a transient error, and re-probing it every cycle spends a call to be told
        # the same thing.
        self._write_supported = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        # The most recent body-level API failure message, and whether the last discovery
        # attempt actually reached the API. Both exist so the standalone CLI can name
        # precisely which stage failed.
        self.last_api_error = ""
        self.discovery_ok = None
        if not self.control_enable:
            self.log("Info: Sungrow control is disabled (sungrow_control_enable is false); monitoring only")
        if self.forced_charge_schedule:
            self.log(
                "Warn: Sungrow sungrow_forced_charge_schedule is on. The forced-charging window (10065-10076) is stored IN THE INVERTER and survives Predbat stopping, "
                "so it defeats the external EMS heartbeat's safe revert to self-consumption. Only leave this on if you want the inverter to keep running the last window Predbat wrote."
            )

    # -------------------------------------------------------------------------
    # Request layer
    # -------------------------------------------------------------------------

    def _headers(self):
        """Return the headers every OpenAPI request carries.

        x-access-key identifies the APPLICATION and is issued with the appkey; the bearer
        token identifies the PLANT OWNER who authorised it. Both are required - a request
        with only one of them is rejected, and the two failure messages look alike, which is
        why _note_failure spells out which credential a rejection points at.
        """
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "x-access-key": self.access_key,
        }
        if self.access_token:
            headers["Authorization"] = "Bearer {}".format(self.access_token)
        return headers

    @staticmethod
    def redact(payload):
        """Return a log-safe copy of a payload with credentials masked.

        Only credentials are masked. result_msg, result_code and req_serial_num are never
        masked: nobody on the project has Sungrow hardware, so a tester's log is the only
        diagnostic available and hiding the error text would defeat the point of api_debug.
        """
        if not isinstance(payload, dict):
            return payload
        return {key: ("***" if key in SUNGROW_DEBUG_REDACT_KEYS else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response while api_debug is on."""
        if not self.api_debug:
            return
        if payload is None:
            self.log("Info: Sungrow API {} {}".format(direction, what))
            return
        try:
            rendered = json.dumps(self.redact(payload), default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(payload)[:2000]
        self.log("Info: Sungrow API {} {} {}".format(direction, what, rendered))

    def _note_failure(self, path, body):
        """Record and log one API-level failure, without ever logging a credential.

        The full error-code appendix could not be sourced, so this does not pretend to map
        every code to a meaning. It reports the code and the SERVER'S OWN message, which is
        strictly more useful than a half-invented table, and separates out the two small
        groups of codes where the right reaction genuinely differs.
        """
        code = str(body.get("result_code", ""))
        message = body.get("result_msg") or ""
        serial = body.get("req_serial_num") or ""
        # Sungrow's own result_msg is often terser than Appendix 2's description of the same
        # code, so both are reported: the description says what the code MEANS and the message
        # says what this particular call hit.
        described = SUNGROW_ERROR_CODES.get(code)
        detail = "{} - {}".format(message, described) if described and described != message else (message or described or "")
        self.last_api_error = "{} {}".format(code, detail).strip()
        suffix = " [req {}]".format(serial) if serial else ""
        if code == SUNGROW_CLOCK_ERROR_CODE:
            # Called out on its own because it otherwise looks exactly like a bad credential,
            # which is the same trap AlphaESS's 6006 sets. It is a clock problem on this host.
            self.log("Warn: Sungrow rejected the request timestamp on {} ({}) - this host's clock is too far from Sungrow server time. It is a clock problem, not a credentials problem{}".format(path, code, suffix))
            return
        if code in SUNGROW_AUTH_ERROR_CODES:
            # Called out because "appkey invalid" and "token expired" otherwise read as an
            # ordinary API failure and send people looking at the inverter instead of the
            # credentials.
            self.log("Warn: Sungrow rejected the credentials on {} ({} {}) - check sungrow_appkey and sungrow_access_key, or reconnect the Sungrow account{}".format(path, code, detail, suffix))
            return
        if code in SUNGROW_RATE_LIMIT_CODES:
            # A pacing signal, not a component fault. Logging it at Warn would read as a
            # genuine malfunction on an account that is simply being polled hard.
            self.log("Info: Sungrow rate-limited {} ({} {}); this is a pacing signal, not a fault, and will be retried on the next tier refresh{}".format(path, code, detail, suffix))
            return
        self.log("Warn: Sungrow {} returned result_code {} {}{}".format(path, code or "(none)", detail, suffix))

    async def _request(self, endpoint_key, body=None, _retry_after_refresh=False):
        """Perform one authorised API call, returning (ok, result_data).

        ok is True only when the envelope itself reported success. A transport failure and
        an API-level rejection are deliberately collapsed into the same False here - unlike
        AlphaESS, Sungrow's envelope carries no code the caller branches on - but they are
        logged differently so the distinction survives where it matters, in the log.
        """
        path = SUNGROW_ENDPOINTS[endpoint_key]
        url = "{}{}".format(self.base_url, path)
        payload = dict(body or {})
        payload["appkey"] = self.appkey
        payload.setdefault("lang", SUNGROW_LANG)

        # Refresh BEFORE the call, not after a failure: a token that expires mid-plan would
        # otherwise cost a whole cycle. _retry_after_refresh guards the reactive path below
        # from refreshing twice for one request.
        if self.auth_method == "oauth" and not _retry_after_refresh:
            if not await self.check_and_refresh_oauth_token():
                self.log("Warn: Sungrow OAuth token refresh failed, skipping API call to {}".format(path))
                return False, None

        self.debug_api("request", path, payload)
        for attempt in range(SUNGROW_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=SUNGROW_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._headers(), json=payload) as response:
                        status = response.status
                        if status == 401 and self.auth_method == "oauth" and not _retry_after_refresh:
                            if await self.handle_oauth_401():
                                return await self._request(endpoint_key, body=body, _retry_after_refresh=True)
                            self.log("Warn: Sungrow {} returned HTTP 401 and the token could not be refreshed".format(path))
                            return False, None
                        if status != 200:
                            self.log("Warn: Sungrow {} returned HTTP {}".format(path, status))
                            return False, None
                        data = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt + 1 >= SUNGROW_RETRIES:
                    self.log("Warn: Sungrow {} transport failure: {}".format(path, error))
                    return False, None
                await asyncio.sleep(1 + attempt)
                continue

            if not isinstance(data, dict):
                self.log("Warn: Sungrow {} returned a non-object body".format(path))
                return False, None
            self.debug_api("response", path, data)
            if str(data.get("result_code")) == SUNGROW_RESULT_OK:
                return True, data.get("result_data")
            self._note_failure(path, data)
            return False, None
        return False, None

    # -------------------------------------------------------------------------
    # Discovery
    # -------------------------------------------------------------------------

    async def get_plants(self):
        """Discover every plant the authorised account can see, returning its ps_ids.

        Sets discovery_ok so an empty account can be told apart from a failed call - the two
        are indistinguishable from the returned list alone, and the CLI has to name which one
        happened.
        """
        # valid_flag "1,3" is deliberate. The parameter defaults to 1 (Normal) ALONE, so a plant
        # in the Connected state (3) is silently missing from the default listing - which looks
        # exactly like an account with no plants and is not something the user could diagnose.
        ok, data = await self._request("plant_list", {"page": 1, "size": 100, "valid_flag": "1,3"})
        if not ok:
            self.discovery_ok = False
            return list(self.plant_list)
        self.discovery_ok = True
        plants = []
        detail = {}
        for entry in (data or {}).get("pageList") or []:
            ps_id = entry.get("ps_id")
            if ps_id is None:
                continue
            plants.append(str(ps_id))
            detail[str(ps_id)] = entry
        self.plant_list = plants
        self.plant_detail = detail
        return plants

    async def get_devices(self, ps_id):
        """Discover the energy-storage inverters in one plant, returning their UUIDs.

        Filtered to SUNGROW_ESS_DEVICE_TYPES at the request rather than after it: a plant can
        contain meters, loggers, string inverters and battery packs, none of which Predbat
        drives, and asking for everything and discarding most of it just makes the response
        bigger.
        """
        ok, data = await self._request("device_list", {"ps_id": ps_id, "page": 1, "size": 100, "device_type_list": [str(value) for value in SUNGROW_ESS_DEVICE_TYPES]})
        if not ok:
            return []
        wanted = [str(sn).lower() for sn in self.inverter_sn_filter]
        found = []
        for entry in (data or {}).get("pageList") or []:
            uuid = entry.get("uuid")
            if uuid is None:
                continue
            serial = str(entry.get("device_sn") or entry.get("sn") or uuid)
            if wanted and serial.lower() not in wanted:
                continue
            entry = dict(entry)
            entry["ps_id"] = str(ps_id)
            entry["serial"] = serial
            self.device_detail[str(uuid)] = entry
            found.append(str(uuid))
        return found

    async def refresh_static(self):
        """Re-discover plants and their inverters. True when discovery worked.

        Branches on discovery_ok rather than on list emptiness: a failed call and an empty
        account both produce an empty list but need different handling. Deliberately does NOT
        assign an empty discovery result over a working device_list - this tier re-runs every
        eight hours, so one transient failure must not take a working component down until
        the next success. Absence of a result is not a result.
        """
        previous = list(self.device_list)
        await self.get_plants()
        if self.discovery_ok is False:
            if previous:
                self.log("Warn: Sungrow plant discovery failed; keeping the {} previously known inverter(s)".format(len(previous)))
            else:
                self.log("Warn: Sungrow plant discovery failed; the account may still have plants")
            return False
        devices = []
        for ps_id in self.plant_list:
            devices.extend(await self.get_devices(ps_id))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if not devices:
            if previous:
                self.log("Warn: Sungrow discovery returned no energy-storage inverters; keeping the {} previously known one(s)".format(len(previous)))
                return True
            self.log("Warn: Sungrow found no energy-storage inverters on this account")
            self.device_list = []
            self.mark_refreshed("static")
            return True
        self.device_list = devices
        self.mark_refreshed("static")
        return True

    # -------------------------------------------------------------------------
    # Tier clock
    # -------------------------------------------------------------------------

    def tier_expired(self, tier, ttl_minutes):
        """Return whether a refresh tier is due."""
        last = self._tier_refreshed.get(tier)
        if last is None:
            return True
        return (time.time() - last) >= (ttl_minutes * 60)

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Stamp a refresh tier as just completed, optionally aged by age_minutes."""
        self._tier_refreshed[tier] = time.time() - (age_minutes * 60)

    # -------------------------------------------------------------------------
    # Telemetry
    # -------------------------------------------------------------------------

    @staticmethod
    def _points_from_row(row, point_map):
        """Turn one realtime data row into {leaf: float}, keeping only points that were reported.

        A point the inverter does not have is absent, or spelled "--". Both become an omitted
        leaf rather than 0.0, because a fabricated zero is indistinguishable from a real one -
        publishing 0 W of PV on an AC-coupled battery would look exactly like night time.
        """
        values = {}
        for key, raw in (row or {}).items():
            point_id = strip_point_prefix(key)
            if point_id is None:
                continue
            leaf = point_map.get(point_id)
            if leaf is None:
                continue
            numeric = as_float(raw, None)
            if numeric is not None:
                values[leaf] = numeric
        return values

    async def fetch_plant_realtime(self, ps_id):
        """Read one plant's realtime measuring points into plant_values/plant_energy.

        This is the FALLBACK source, not the primary one - telemetry() and energy() both prefer
        the device read, which describes one inverter where this aggregates the whole site. It
        is polled anyway, every cycle, because getDeviceRealTimeData is not served by every
        account and model and a site that loses it would otherwise lose SoC, PV, load, grid and
        every daily counter in one go. This read is what keeps such a site planning.
        """
        ok, data = await self._request("plant_realtime", {"ps_id_list": [ps_id], "point_id_list": list(SUNGROW_PLANT_POINTS.keys()), "is_get_point_dict": "1"})
        if not ok:
            return False
        rows = (data or {}).get("device_point_list") or []
        for row in rows:
            if str(row.get("ps_id", ps_id)) != str(ps_id):
                continue
            values = self._points_from_row(row, SUNGROW_PLANT_POINTS)
            self.plant_values[ps_id] = values
            # Wh on the wire throughout; Predbat works in kWh.
            energy = {}
            for leaf, point_leaf in SUNGROW_PLANT_ENERGY.items():
                if point_leaf in values:
                    energy[leaf] = round(values[point_leaf] * SUNGROW_WH_TO_KWH, 3)
            self.plant_energy[ps_id] = energy
            return True
        return False

    async def fetch_device_realtime(self, uuid):
        """Read one inverter's own measuring points into device_values.

        The request carries device_type as well as ps_key_list: this endpoint answers for ONE
        device type at a time, which is why only the energy-storage inverter's own points are
        asked for here and not the battery pack's - those belong to a separate battery device.

        A failure is treated as ordinary and non-fatal. Not every account and model serves this
        endpoint, and publish_data falls back to the plant-level points for everything it would
        have supplied, so losing it costs battery SoH and the split charge/discharge powers, not
        the plan.
        """
        detail = self.device_detail.get(uuid, {})
        ps_key = detail.get("ps_key")
        if not ps_key:
            return False
        point_map = dict(SUNGROW_ESS_POINTS)
        # point_id_list is capped at 100 per request. This map is far smaller, so there is
        # nothing to chunk today - but adding points without noticing the cap would produce a
        # rejection that looks like an unsupported endpoint rather than an oversized request.
        if len(point_map) > SUNGROW_MAX_POINTS_PER_REQUEST:
            self.log("Warn: Sungrow is asking for {} measuring points, above the documented limit of {}".format(len(point_map), SUNGROW_MAX_POINTS_PER_REQUEST))
        device_type = detail.get("device_type", SUNGROW_DEVICE_ESS)
        ok, data = await self._request("device_realtime", {"ps_key_list": [ps_key], "device_type": str(device_type), "point_id_list": list(point_map.keys()), "is_get_point_dict": "1"})
        if not ok:
            return False
        for row in (data or {}).get("device_point_list") or []:
            values = self._points_from_row(row, point_map)
            if not values:
                continue
            # The two unsigned halves are combined once here so every consumer - the published
            # sensor, the control decision and the discovery record - sees the same signed
            # value with the same convention.
            battery_power = signed_battery_power(values.get("battery_charge_power"), values.get("battery_discharge_power"))
            if battery_power is not None:
                values["battery_power"] = battery_power
            grid_power = signed_grid_power(values.get("purchased_power"), values.get("feed_in_power"))
            if grid_power is not None:
                values["grid_power"] = grid_power
            if "battery_soc" in values:
                values["soc"] = values["battery_soc"]
            elif "battery_level" in values:
                values["soc"] = values["battery_level"]
            self.device_values[uuid] = values
            # The daily counters are Wh on the wire like everything else, and are split out into
            # their own map so energy() can prefer them over the plant's aggregate without
            # re-converting on every read.
            self.device_energy[uuid] = {leaf: round(values[leaf] * SUNGROW_WH_TO_KWH, 3) for leaf in SUNGROW_ESS_ENERGY if leaf in values}
            return True
        return False

    async def refresh_power(self):
        """Refresh the telemetry tier. True when at least one plant answered."""
        any_ok = False
        for ps_id in list(self.plant_list):
            if await self.fetch_plant_realtime(ps_id):
                any_ok = True
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        for uuid in list(self.device_list):
            await self.fetch_device_realtime(uuid)
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if any_ok:
            self.mark_refreshed("power")
        return any_ok

    def telemetry(self, uuid, leaf):
        """Return one telemetry value for an inverter, device read first then plant read.

        The device read is preferred because it describes THIS inverter, where the plant read
        aggregates everything in the plant - identical on a residential single-inverter site
        and not identical on a site with two. Returns None when neither source reported it.
        """
        value = self.device_values.get(uuid, {}).get(leaf)
        if value is not None:
            return value
        ps_id = self.device_detail.get(uuid, {}).get("ps_id")
        plant_leaf = SUNGROW_PLANT_TELEMETRY.get(leaf)
        if ps_id is None or plant_leaf is None:
            return None
        return self.plant_values.get(ps_id, {}).get(plant_leaf)

    def energy(self, uuid, leaf):
        """Return one daily energy counter in kWh for an inverter, device read first then plant.

        Same preference as telemetry(), and for the same reason: the energy-storage inverter
        carries its own daily counters (13199 load, 13147 import, 13122 export, 13112 PV), which
        describe THIS inverter, where the plant's counters aggregate everything on the site.
        Identical on a residential single-inverter plant and not identical on a larger one.
        """
        value = self.device_energy.get(uuid, {}).get(leaf)
        if value is not None:
            return value
        ps_id = self.device_detail.get(uuid, {}).get("ps_id")
        if ps_id is None:
            return None
        return self.plant_energy.get(ps_id, {}).get(leaf)

    def battery_capacity(self, uuid):
        """Return the battery capacity in kWh, from the plant's chargeable + dischargeable energy.

        Sungrow report no nameplate capacity anywhere this component can reach. What the plant
        does report is how much can still go in (83235) and how much can still come out
        (83236) right now, and those two sum to the usable pack - a derivation, not a reading,
        so it moves a little with temperature and with the SoC limits in force. Returns 0.0
        when either half is missing, which leaves soc_max unbound rather than half-sized.
        """
        ps_id = self.device_detail.get(uuid, {}).get("ps_id")
        values = self.plant_values.get(ps_id, {}) if ps_id else {}
        chargeable = values.get("chargeable_energy")
        dischargeable = values.get("dischargeable_energy")
        if chargeable is None or dischargeable is None:
            return 0.0
        return round((chargeable + dischargeable) * SUNGROW_WH_TO_KWH, 3)

    def battery_rate_max(self, uuid):
        """Return the battery charge/discharge power limit in watts.

        The plant's maximum rechargeable/dischargeable power points are the inverter's own
        statement of what the pack will take right now, which is exactly what Predbat wants.
        Where they are missing the user's override is the only source - leaving this unmapped
        is NOT neutral, because inverter.py then silently uses a hard-coded 2600 W on every
        plan, which is roughly half of a 5 kW SH hybrid's real rate.
        """
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        ps_id = self.device_detail.get(uuid, {}).get("ps_id")
        values = self.plant_values.get(ps_id, {}) if ps_id else {}
        rates = [values.get("max_rechargeable_power"), values.get("max_dischargeable_power")]
        rates = [rate for rate in rates if rate]
        return max(rates) if rates else 0.0

    # -------------------------------------------------------------------------
    # Publishing
    # -------------------------------------------------------------------------

    def _serial(self, uuid):
        """Return the display serial for a device UUID, falling back to the UUID itself."""
        return str(self.device_detail.get(uuid, {}).get("serial") or uuid)

    def _sensor_name(self, uuid, leaf):
        """Return a namespaced Sungrow sensor entity id."""
        return "sensor.{}_sungrow_{}_{}".format(self.prefix, self._serial(uuid).lower(), leaf)

    def _control_name(self, domain, uuid, leaf):
        """Return a namespaced Sungrow control entity id."""
        return "{}.{}_sungrow_{}_{}".format(domain, self.prefix, self._serial(uuid).lower(), leaf)

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "battery_soh": "%", "battery_temperature": "°C", "battery_voltage": "V", "battery_current": "A"}
        for uuid in self.device_list:
            serial = self._serial(uuid)
            for leaf, unit in units.items():
                value = self.telemetry(uuid, leaf)
                if value is None:
                    continue
                self.dashboard_item(
                    self._sensor_name(uuid, leaf),
                    state=round(value, 3),
                    attributes={"unit_of_measurement": unit, "friendly_name": "Sungrow {} {}".format(serial, leaf.replace("_", " ").title())},
                    app="sungrow",
                )

            # Ratings are published only when actually derivable - an arg pointing at a sensor
            # that never appears is worse than an absent arg the user can fill in.
            # Published as a DIAGNOSTIC and never used for soc_max: 13140's name cell says
            # "Battery Capacity (kWh)" and its unit cell says "Wh" in Sungrow's own table, so one
            # of the two is wrong by a factor of a thousand and the docs do not say which. It is
            # surfaced because a tester comparing it against the derived capacity below is
            # exactly how that gets resolved.
            reported = self.device_values.get(uuid, {}).get("battery_capacity_reported")
            if reported is not None:
                self.dashboard_item(
                    self._sensor_name(uuid, "battery_capacity_reported"),
                    state=round(reported, 3),
                    attributes={"friendly_name": "Sungrow {} Battery Capacity (reported, UNIT UNVERIFIED - Sungrow document this point as both kWh and Wh)".format(serial), "icon": "mdi:help-circle-outline"},
                    app="sungrow",
                )

            capacity = self.battery_capacity(uuid)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(uuid, "battery_capacity"), state=capacity, attributes={"unit_of_measurement": "kWh", "friendly_name": "Sungrow {} Battery Capacity".format(serial)}, app="sungrow")
            rate_max = self.battery_rate_max(uuid)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(uuid, "battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": "Sungrow {} Battery Rate Max".format(serial)}, app="sungrow")

            # Daily energy counters feed Predbat's load/import/export learning. They reset at
            # midnight; minute_data/clean_incrementing_reverse absorbs that.
            for leaf in SUNGROW_PLANT_ENERGY:
                value = self.energy(uuid, leaf)
                if value is None:
                    continue
                self.dashboard_item(
                    self._sensor_name(uuid, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "Sungrow {} {}".format(serial, leaf.replace("_", " ").title())},
                    app="sungrow",
                )

            self.dashboard_item(
                self._sensor_name(uuid, "control_state"),
                state="external_dispatch" if uuid in self.control_held else "self_consumption",
                attributes={"friendly_name": "Sungrow {} Control State".format(serial), "icon": "mdi:transmission-tower"},
                app="sungrow",
            )

    # -------------------------------------------------------------------------
    # Control entities
    # -------------------------------------------------------------------------

    @staticmethod
    def _empty_schedule():
        """Return a fresh, disabled schedule shape - the single source of truth for its defaults."""
        return {
            "reserve": 0,
            "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        }

    async def publish_schedule_settings_ha(self, uuid):
        """Publish the charge/export schedule control entities for one inverter."""
        serial = self._serial(uuid)
        local = self.local_schedule.get(uuid, {})
        # Deliberately NOT clamped. These entities are Predbat's control surface: it writes a
        # value then reads it back to confirm, so publishing anything other than what was
        # written guarantees a mismatch and a retry storm. Clamping happens at the API
        # boundary, in scale_for_write.
        self.dashboard_item(
            self._control_name("number", uuid, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Sungrow {} Battery Schedule Reserve".format(serial), "icon": "mdi:gauge"},
            app="sungrow",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes Predbat
            # replace these entities with its own dummies and the window never arrives.
            self.dashboard_item(
                self._control_name("select", uuid, "battery_schedule_{}_start_time".format(direction)),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": "Sungrow {} {} Start".format(serial, direction.title()), "icon": "mdi:clock-outline"},
                app="sungrow",
            )
            self.dashboard_item(
                self._control_name("select", uuid, "battery_schedule_{}_end_time".format(direction)),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": "Sungrow {} {} End".format(serial, direction.title()), "icon": "mdi:clock-outline"},
                app="sungrow",
            )
            self.dashboard_item(
                self._control_name("number", uuid, "battery_schedule_{}_soc".format(direction)),
                state=int(window.get("soc", 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Sungrow {} {} SoC".format(serial, direction.title()), "icon": "mdi:gauge"},
                app="sungrow",
            )
            self.dashboard_item(
                self._control_name("number", uuid, "battery_schedule_{}_power".format(direction)),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": "Sungrow {} {} Power".format(serial, direction.title()), "icon": "mdi:flash"},
                app="sungrow",
            )
            self.dashboard_item(
                self._control_name("switch", uuid, "battery_schedule_{}_enable".format(direction)),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": "Sungrow {} {} Enable".format(serial, direction.title()), "icon": "mdi:check-circle-outline"},
                app="sungrow",
            )
        self.dashboard_item(
            self._control_name("switch", uuid, "battery_schedule_charge_write"),
            state="off",
            attributes={"friendly_name": "Sungrow {} Schedule Write".format(serial), "icon": "mdi:content-save"},
            app="sungrow",
        )

    async def get_schedule_settings_ha(self, uuid):
        """Read the control entities into the schedule shape decide_command consumes.

        Numeric casts route through as_float so an entity legitimately reporting
        "unknown"/"unavailable" - right after a Home Assistant restart, before Predbat
        republishes - falls back to 0 rather than raising and killing the control loop.
        """
        schedule = {"reserve": int(as_float(self.get_state_wrapper(self._control_name("number", uuid, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", uuid, "battery_schedule_{}_enable".format(direction)), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", uuid, "battery_schedule_{}_start_time".format(direction)), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", uuid, "battery_schedule_{}_end_time".format(direction)), default="00:00:00"),
                "soc": int(as_float(self.get_state_wrapper(self._control_name("number", uuid, "battery_schedule_{}_soc".format(direction)), default=0), 0)),
                "power": int(as_float(self.get_state_wrapper(self._control_name("number", uuid, "battery_schedule_{}_power".format(direction)), default=0), 0)),
            }
        self.local_schedule[uuid] = schedule
        return schedule

    def _uuid_from_entity(self, entity_id):
        """Resolve a control entity id back to its device UUID, or None.

        Entity ids are always {domain}.{prefix}_sungrow_{serial}_{leaf}, so the serial is
        always followed by "_". Matching serial + "_" rather than a bare prefix keeps
        prefix-colliding serials apart - an entity for A2211 must never route to A221, which
        would send a control write to the wrong inverter.
        """
        text = str(entity_id).lower()
        for uuid in self.device_list:
            if "_sungrow_{}_".format(self._serial(uuid).lower()) in text:
                return uuid
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

    def update_local_schedule(self, uuid, entity_id, value):
        """Apply one control-entity change to the locally held schedule."""
        schedule = self.local_schedule.setdefault(uuid, self._empty_schedule())
        leaf = str(entity_id).split("_sungrow_{}_".format(self._serial(uuid).lower()), 1)[-1]
        if leaf == "battery_schedule_reserve":
            schedule["reserve"] = int(as_float(value, 0))
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
                window[field] = int(as_float(value, 0))
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
        uuid = self._uuid_from_entity(entity_id)
        if not uuid:
            self.log("Warn: Sungrow could not resolve an inverter for {}".format(entity_id))
            return
        # The write button is NOT forced. Predbat presses it on every cycle as its normal
        # "apply the schedule" action (INVERTER_DEF time_button_press), not only when the plan
        # actually changed, so force=True here would bypass the change-detection gate on every
        # single cycle. DEYE hit this exact bug first: PR #4371 measured 40 button presses
        # producing 36 byte-identical control orders over two hours on a live site once the
        # button forced the write. Do not reintroduce force=True here.
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                # Predbat is now actively driving this inverter, so the reconcile loop may
                # re-apply from here on. Marked on the press itself rather than on a successful
                # write: a write that failed still means Predbat owns this inverter and the
                # next tick should retry.
                self.control_active.add(uuid)
                await self.apply_schedule(uuid)
            return
        self.update_local_schedule(uuid, entity_id, value)
        await self.publish_schedule_settings_ha(uuid)

    # -------------------------------------------------------------------------
    # The control decision
    # -------------------------------------------------------------------------

    def _window_active_now(self, window):
        """Return whether a stored window covers Predbat's current minute.

        Hours above 24 are accepted because Predbat can represent a window crossing midnight
        as (for example) 23:00-25:00. This component never writes a window to the inverter, so
        unlike a schedule-based one it does not have to split it - it only has to answer
        whether now is inside it.
        """
        start = hm_to_minutes(window.get("start"))
        end = hm_to_minutes(window.get("end"))
        if start == end:
            return False
        now = int(self.minutes_now) % (24 * 60)
        if end > 24 * 60 and now < start:
            now += 24 * 60
        if start < end:
            return start <= now < end
        return now >= start or now < end

    def decide_command(self, uuid, schedule):
        """Translate Predbat's plan into the instantaneous command to hold right now.

        Returns a dict of the external-dispatch state to put the inverter in, or None meaning
        "release control and let the inverter run self-consumption". This is where Predbat's
        time windows stop being times: Sungrow's parameters carry no schedule, so the window
        arithmetic happens here on every cycle and what goes out is a setpoint for this minute.

        The order of the branches is the order of Predbat's own precedence:

        1. An active export window with real power is a forced discharge. Its target SoC
           becomes the LOWER limit (10002), which is what stops the discharge at the target
           rather than at the pack floor.
        2. An active charge window whose target is above the current SoC is a forced charge,
           with the target as the UPPER limit (10001).
        3. Anything else that must not move the battery is a STOP (10004 = 204). That covers
           both of Predbat's freezes: an export window at zero power (freeze export / any
           generic no-discharge hold), and a charge window whose target has already been
           reached, where execute_plan has cleared the enable switch but left the window
           intact. Sungrow has a real stop command, so unlike AlphaESS this needs no synthetic
           low-target charge profile to express a hold.
        4. Otherwise there is nothing to hold and control is released, which also stops the
           heartbeat and lets the inverter fall back on its own.
        """
        charge = schedule.get("charge", {}) or {}
        export = schedule.get("export", {}) or {}
        soc_now = as_float(self.telemetry(uuid, "soc"), 0.0)
        reserve = int(as_float(schedule.get("reserve"), 0))

        charge_active = self._window_active_now(charge)
        export_active = self._window_active_now(export)
        charge_target = as_float(charge.get("soc"), 0.0)
        charge_power = as_float(charge.get("power"), 0.0)
        export_target = as_float(export.get("soc"), 0.0)
        export_power = as_float(export.get("power"), 0.0)

        if export.get("enable") and export_active and export_power > 0:
            return {"command": SUNGROW_CMD_DISCHARGE, "power": export_power, "soc_lower": max(reserve, int(export_target)), "reason": "export window active"}

        if charge.get("enable") and charge_active and charge_power > 0 and charge_target > soc_now:
            return {"command": SUNGROW_CMD_CHARGE, "power": charge_power, "soc_upper": int(charge_target), "reason": "charge window active"}

        # Freeze export: Predbat expresses every no-discharge hold as an export window with
        # zero power, and leaves the window times in place so the intent survives.
        if export_active and export_power <= 0 and (export.get("enable") or charge_active):
            return {"command": SUNGROW_CMD_STOP, "power": 0, "soc_lower": reserve, "reason": "discharge hold"}

        # Charge target already reached inside its own window - hold, do not discharge back
        # down to it. Gated on the window still being active so stale target/time entities
        # cannot extend the hold past the period it was planned for.
        if charge_active and charge_target > 0 and charge_target <= soc_now:
            return {"command": SUNGROW_CMD_STOP, "power": 0, "soc_upper": int(charge_target), "reason": "charge target reached"}

        return None

    def build_param_list(self, decision):
        """Turn a decision into the param_list paramSetting expects.

        10003 is written FIRST and in the same task as the command. The portal is explicit
        that the EMS mode and the heartbeat have to travel together when control is taken, and
        writing 10004 while the inverter is still in self-consumption is simply ignored - the
        command is accepted and does nothing, which is the worst of both worlds because
        nothing reports an error.

        The heartbeat is included in the taking-control task for the same reason. The
        heartbeat LOOP then keeps it alive; this is only the first beat.
        """
        params = [
            param_entry(SUNGROW_PARAM_EMS_MODE, scale_for_write(SUNGROW_PARAM_EMS_MODE, SUNGROW_EMS_EXTERNAL_DISPATCH)),
            param_entry(SUNGROW_PARAM_HEARTBEAT, scale_for_write(SUNGROW_PARAM_HEARTBEAT, heartbeat_declared_interval(self.heartbeat_interval))),
            param_entry(SUNGROW_PARAM_COMMAND, scale_for_write(SUNGROW_PARAM_COMMAND, decision["command"])),
            param_entry(SUNGROW_PARAM_POWER, scale_for_write(SUNGROW_PARAM_POWER, decision.get("power", 0))),
        ]
        if "soc_upper" in decision:
            params.append(param_entry(SUNGROW_PARAM_SOC_UPPER, scale_for_write(SUNGROW_PARAM_SOC_UPPER, decision["soc_upper"])))
        if "soc_lower" in decision:
            params.append(param_entry(SUNGROW_PARAM_SOC_LOWER, scale_for_write(SUNGROW_PARAM_SOC_LOWER, decision["soc_lower"])))
        return params

    def build_forced_charge_params(self, schedule):
        """Build the forced-charging window parameters (10065-10076) from Predbat's charge window.

        This is the SECOND of Sungrow's two control paths, and the one this component does not
        drive. It is offered here as an opt-in supplement, never as a replacement - the module
        docstring gives the full reasoning, but in short it is charge-only, carries no power
        setpoint, and holds two daily-recurring windows, so it cannot express export, freeze,
        a charge rate, or more than two slots.

        OFF BY DEFAULT because of the one thing it does that external dispatch does not: this
        window is stored IN THE INVERTER and survives Predbat stopping, the network dropping
        and the heartbeat lapsing. That is a feature to a user who wants the battery to keep
        following the last plan through an outage, and a hazard to everyone else, because the
        inverter will go on force-charging from the grid to a target nobody is supervising,
        every day, on whatever rates apply then. Enabling it is a deliberate trade of the safe
        revert for continuity, so it is the user's call and not the default.

        Window 2 is left disabled. Predbat only ever has one charge window to express at a time
        here, so writing anything into it would be inventing a second charge the plan never
        asked for.
        """
        charge = schedule.get("charge", {}) or {}
        enabled = bool(charge.get("enable")) and not window_is_empty(charge.get("start"), charge.get("end"))
        if not enabled:
            return [param_entry(SUNGROW_PARAM_FORCED_CHARGING, scale_for_write(SUNGROW_PARAM_FORCED_CHARGING, SUNGROW_DISABLE))]
        start_hour, start_minute = hhmmss_to_hour_minute(charge.get("start"))
        end_hour, end_minute = hhmmss_to_hour_minute(charge.get("end"))
        return [
            param_entry(SUNGROW_PARAM_FORCED_CHARGING, scale_for_write(SUNGROW_PARAM_FORCED_CHARGING, SUNGROW_ENABLE)),
            param_entry(SUNGROW_PARAM_FORCED_VALID_TIME, scale_for_write(SUNGROW_PARAM_FORCED_VALID_TIME, SUNGROW_FORCED_EVERYDAY)),
            param_entry(SUNGROW_PARAM_FORCED_START_HOUR_1, scale_for_write(SUNGROW_PARAM_FORCED_START_HOUR_1, start_hour)),
            param_entry(SUNGROW_PARAM_FORCED_START_MIN_1, scale_for_write(SUNGROW_PARAM_FORCED_START_MIN_1, start_minute)),
            param_entry(SUNGROW_PARAM_FORCED_END_HOUR_1, scale_for_write(SUNGROW_PARAM_FORCED_END_HOUR_1, end_hour)),
            param_entry(SUNGROW_PARAM_FORCED_END_MIN_1, scale_for_write(SUNGROW_PARAM_FORCED_END_MIN_1, end_minute)),
            param_entry(SUNGROW_PARAM_FORCED_TARGET_SOC_1, scale_for_write(SUNGROW_PARAM_FORCED_TARGET_SOC_1, int(as_float(charge.get("soc"), 0)))),
            param_entry(SUNGROW_PARAM_FORCED_START_HOUR_2, scale_for_write(SUNGROW_PARAM_FORCED_START_HOUR_2, 0)),
            param_entry(SUNGROW_PARAM_FORCED_START_MIN_2, scale_for_write(SUNGROW_PARAM_FORCED_START_MIN_2, 0)),
            param_entry(SUNGROW_PARAM_FORCED_END_HOUR_2, scale_for_write(SUNGROW_PARAM_FORCED_END_HOUR_2, 0)),
            param_entry(SUNGROW_PARAM_FORCED_END_MIN_2, scale_for_write(SUNGROW_PARAM_FORCED_END_MIN_2, 0)),
            param_entry(SUNGROW_PARAM_FORCED_TARGET_SOC_2, scale_for_write(SUNGROW_PARAM_FORCED_TARGET_SOC_2, 0)),
        ]

    # -------------------------------------------------------------------------
    # The write path: check -> dispatch -> poll
    # -------------------------------------------------------------------------

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the inverter."""
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    async def check_write_support(self, uuid):
        """Return True/False/None for whether this device accepts parameter configuration.

        Cached: it is a property of the hardware and the account tier, not a transient error,
        so re-probing every cycle spends a call to be told the same thing. A failure leaves
        the verdict unknown so the next cycle re-probes rather than permanently writing the
        device off.
        """
        if uuid in self._write_supported:
            return self._write_supported[uuid]
        ok, data = await self._request("param_check", {"uuid": str(uuid), "set_type": SUNGROW_SET_TYPE_WRITE})
        if not ok:
            return None
        data = data or {}
        if str(data.get("check_result")) != SUNGROW_CHECK_OK:
            return None
        dev_list = data.get("dev_result_list") or []
        if not dev_list:
            return None
        verdict = str(dev_list[0].get("check_result"))
        if verdict == SUNGROW_CHECK_SUPPORTED:
            self._write_supported[uuid] = True
            return True
        if verdict == SUNGROW_CHECK_UNSUPPORTED:
            self._write_supported[uuid] = False
            self.log("Warn: Sungrow {} does not accept parameter configuration; Predbat can monitor it but cannot control it. This is usually an account-tier or model limitation.".format(self._serial(uuid)))
            return False
        return None

    async def _await_task(self, uuid, task_id):
        """Poll getParamSettingTask until the dispatched task finishes, returning its param_list.

        Returns None on failure or timeout. The first poll is delayed deliberately: a task
        queued microseconds ago is always still running, so polling immediately just spends a
        request to be told so. The whole wait is bounded so a wedged task cannot stall the
        component's tick past ComponentBase's run_timeout.
        """
        deadline = time.time() + SUNGROW_TASK_TIMEOUT_SECONDS
        await asyncio.sleep(SUNGROW_TASK_FIRST_POLL_SECONDS)
        while time.time() < deadline:
            ok, data = await self._request("param_task", {"uuid": str(uuid), "task_id": str(task_id)})
            if not ok:
                return None
            status = (data or {}).get("command_status")
            if status == SUNGROW_TASK_DONE:
                param_list = (data or {}).get("param_list") or []
                # A COMPLETED TASK IS NOT A SUCCESSFUL WRITE. The status is two-level: the task
                # reports 8 once it has finished running, while each parameter inside it carries
                # its own status, and one of them can have failed or timed out while the task as
                # a whole "completed". Reading only the task level reports a setpoint as landed
                # when the inverter never took it, which is the worst possible failure here -
                # Predbat would cache the decision as applied and never retry it.
                bad = [entry for entry in param_list if entry.get("command_status") not in (None, SUNGROW_PARAM_STATUS_SUCCESS)]
                if bad:
                    detail = ", ".join("{} {}".format(describe_param(entry.get("param_code")), SUNGROW_PARAM_STATUS.get(entry.get("command_status"), entry.get("command_status"))) for entry in bad)
                    self.log("Warn: Sungrow task {} for {} completed but {} of {} parameter(s) did not take: {}".format(task_id, self._serial(uuid), len(bad), len(param_list), detail))
                    return None
                return param_list
            if status != SUNGROW_TASK_RUNNING:
                self.log("Warn: Sungrow task {} for {} ended in command_status {}".format(task_id, self._serial(uuid), status))
                return None
            await asyncio.sleep(SUNGROW_TASK_POLL_SECONDS)
        self.log("Warn: Sungrow task {} for {} did not finish within {}s".format(task_id, self._serial(uuid), SUNGROW_TASK_TIMEOUT_SECONDS))
        return None

    async def dispatch_params(self, uuid, param_list, set_type=SUNGROW_SET_TYPE_WRITE, task_name="Predbat"):
        """Dispatch one parameter task and wait for its result. Returns param_list or None.

        This is the whole asynchronous write in one place. paramSetting does NOT apply
        anything by itself - it queues a task and answers with a task_id, and the inverter may
        take seconds to pick it up. Treating its success envelope as "the inverter has the
        value" is the single easiest mistake to make against this API, so no caller sees the
        task_id: they get either the completed read-back or None.
        """
        body = {
            "set_type": set_type,
            "uuid": str(uuid),
            "task_name": "{} {}".format(task_name, time.strftime("%Y-%m-%d %H:%M:%S")),
            "expire_second": SUNGROW_TASK_EXPIRE_SECONDS,
            "param_list": param_list,
        }
        ok, data = await self._request("param_setting", body)
        if not ok:
            return None
        data = data or {}
        if str(data.get("check_result")) != SUNGROW_CHECK_OK:
            check = str(data.get("check_result"))
            self.log("Warn: Sungrow rejected the parameter task for {} with check_result {} ({})".format(self._serial(uuid), check, SUNGROW_CHECK_RESULTS.get(check, "undocumented")))
            return None
        dev_list = data.get("dev_result_list") or []
        if not dev_list or str(dev_list[0].get("code")) != SUNGROW_DEV_RESULT_OK:
            code = str(dev_list[0].get("code")) if dev_list else ""
            self.log("Warn: Sungrow rejected the parameter task for {}: {} ({})".format(self._serial(uuid), code or "no device result", SUNGROW_CHECK_RESULTS.get(code, "undocumented")))
            return None
        task_id = dev_list[0].get("task_id")
        if not task_id:
            self.log("Warn: Sungrow accepted the parameter task for {} but returned no task_id".format(self._serial(uuid)))
            return None
        return await self._await_task(uuid, task_id)

    async def read_params(self, uuid, param_codes):
        """Read parameters back from the inverter, returning {param_code: raw set value}.

        A read is the same dispatch-then-poll flow as a write with set_type 2 and an empty
        set_value, which is why it shares dispatch_params rather than having its own client.
        """
        results = await self.dispatch_params(uuid, [param_entry(code) for code in param_codes], set_type=SUNGROW_SET_TYPE_READ, task_name="Predbat readback")
        if results is None:
            return None
        return {str(entry.get("param_code")): entry.get("return_value") for entry in results}

    def _write_allowed(self, uuid, force=False):
        """Return True when a control write for one inverter may go out now.

        A change arriving inside the pacing window is HELD, not dropped: apply_settings leaves
        the applied-command cache untouched, so the next eligible tick rebuilds and sends it.
        The heartbeat deliberately does NOT go through this gate - it is not a change of plan
        and holding it back would drop control of the battery.
        """
        if force or not self.min_write_interval:
            return True
        last = self.last_write_time.get(uuid)
        if last is None:
            return True
        return (time.time() - last) >= self.min_write_interval

    async def _write_params(self, uuid, decision, force=False):
        """Send one control decision if it differs from the last applied one and pacing allows.

        Returns whether the inverter is now KNOWN TO MATCH this decision, not merely whether
        an exception was avoided. True covers "sent and confirmed" and "already matched, so
        nothing needed sending" alike. False covers both a HELD change and an outright
        rejection - a caller must not read either of those as the inverter matching the plan.
        """
        if self._write_supported.get(uuid) is False:
            return False
        if not force and self.applied_command.get(uuid) == decision:
            return True
        if not self._write_allowed(uuid, force=force):
            self.log("Info: Sungrow {} change is held by sungrow_min_write_interval ({}s) and will be applied on the next eligible cycle".format(self._serial(uuid), self.min_write_interval))
            return False

        supported = await self.check_write_support(uuid)
        if supported is False:
            return False

        params = self.build_param_list(decision)
        if self.forced_charge_schedule:
            params.extend(self.build_forced_charge_params(self.local_schedule.get(uuid, {})))

        # Stamp every attempt, success or not. A persistently rejected write must still be
        # paced, or the reconcile loop re-dispatches it on every single tick forever. The
        # decision is deliberately NOT cached as applied on any failure path, so it keeps
        # retrying - just paced, not hammering.
        self.last_write_time[uuid] = time.time()
        results = await self.dispatch_params(uuid, params)
        if results is None:
            self.log("Warn: Sungrow control write for {} was not confirmed ({})".format(self._serial(uuid), decision.get("reason", "")))
            await self.save_control()
            return False

        self.applied_command[uuid] = dict(decision)
        # Control is only marked as HELD once the task has actually completed. Starting the
        # heartbeat for a task that never landed would beat for a mode the inverter is not in.
        self.control_held.add(uuid)
        self._ensure_heartbeat()
        self.log("Info: Sungrow wrote {} for {} ({})".format(describe_param(SUNGROW_PARAM_COMMAND), self._serial(uuid), decision.get("reason", "")))
        await self.save_control()
        return True

    async def release_control(self, uuid):
        """Hand the inverter back to self-consumption and stop beating for it.

        Called when the plan has nothing to hold. Writing mode 0 explicitly is faster and more
        legible than letting the heartbeat lapse - the inverter would get there on its own
        within one interval, which is the safety net, not the normal path. The device leaves
        control_held even if the write fails, because in that case the lapsing heartbeat will
        do it anyway and continuing to beat would hold a mode Predbat no longer wants.
        """
        if uuid not in self.control_held:
            return True
        self.control_held.discard(uuid)
        self.applied_command.pop(uuid, None)
        if not self.control_enable or self._is_read_only():
            return True
        params = [param_entry(SUNGROW_PARAM_EMS_MODE, scale_for_write(SUNGROW_PARAM_EMS_MODE, SUNGROW_EMS_SELF_CONSUMPTION))]
        results = await self.dispatch_params(uuid, params, task_name="Predbat release")
        if results is None:
            self.log("Info: Sungrow could not confirm the release of {} to self-consumption; the external EMS heartbeat will lapse and the inverter will revert on its own".format(self._serial(uuid)))
            return False
        self.log("Info: Sungrow released {} back to self-consumption".format(self._serial(uuid)))
        await self.save_control()
        return True

    async def apply_settings(self, uuid, schedule, force=False):
        """Decide and apply the command for one inverter."""
        if not self.control_enable:
            return False
        decision = self.decide_command(uuid, schedule)
        if decision is None:
            return await self.release_control(uuid)
        return await self._write_params(uuid, decision, force=force)

    async def apply_schedule(self, uuid, force=False):
        """Apply the locally held schedule for one inverter."""
        schedule = self.local_schedule.get(uuid)
        if not schedule:
            return False
        return await self.apply_settings(uuid, schedule, force=force)

    async def _reconcile_control(self, uuid):
        """Re-apply this inverter's decision if Predbat already controls it, unforced.

        Gated on read-only for a specific reason. Predbat's own read-only handling covers every
        write that originates from a plan, but NOT one this component initiates itself - and
        this is exactly that. The decision is time-aware, so simply crossing a window boundary
        changes it with no plan change at all, and without this gate that transition would
        write to the inverter while Predbat was in read-only mode (GH#4436).
        """
        if uuid not in self.control_active or self._is_read_only() or not self.control_enable:
            return
        try:
            await self.apply_schedule(uuid)
        except Exception as error:
            self.log("Warn: Sungrow control apply failed for {}: {}".format(self._serial(uuid), error))

    # -------------------------------------------------------------------------
    # The heartbeat
    # -------------------------------------------------------------------------

    def _ensure_heartbeat(self):
        """Start the heartbeat loop if control is held and it is not already running.

        Idempotent, and called from every path that takes control, so there is never more than
        one loop and never a held device without one.
        """
        if not self.control_held:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_stop = False
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        """Keep writing parameter 10017 for every device Predbat currently holds.

        This runs on its OWN cadence, not run()'s. ComponentBase ticks run() once a minute,
        which is already too slow for a user who has set sungrow_heartbeat_interval near the
        documented one-second minimum, and it would couple losing control of the battery to
        any slow API call inside a normal cycle.

        The loop exits when nothing is held any more, which is what makes it safe to restart
        from _ensure_heartbeat() without tracking its lifetime anywhere else.

        Beats go out at a fraction of the promised interval (heartbeat_send_interval) so a
        single lost request does not immediately cost control. A failed beat is logged and
        retried on the next pass rather than escalated: the consequence of beats stopping
        altogether is a revert to self-consumption, which is the safe state, so there is
        nothing to escalate to.
        """
        interval = heartbeat_send_interval(self.heartbeat_interval)
        self.log("Info: Sungrow heartbeat loop started, beating every {:.0f}s for a declared interval of {}s".format(interval, self.heartbeat_interval))
        while not self._heartbeat_stop and not self.api_stop and self.control_held:
            await asyncio.sleep(interval)
            if self._heartbeat_stop or self.api_stop:
                break
            # Read-only mode must stop the beats, not just the plan writes. Continuing to beat
            # would hold the inverter in external dispatch with whatever setpoint it last had,
            # which is precisely the "Predbat is not driving but the battery is still forced"
            # state read-only exists to avoid. Letting the beats lapse returns it to
            # self-consumption within one interval.
            if self._is_read_only() or not self.control_enable:
                self.log("Info: Sungrow heartbeat paused (read-only or control disabled); the inverter will revert to self-consumption")
                break
            for uuid in list(self.control_held):
                await self.send_heartbeat(uuid)
        self.log("Info: Sungrow heartbeat loop stopped")

    async def send_heartbeat(self, uuid):
        """Write one heartbeat to parameter 10017 for a single inverter.

        Deliberately not routed through _write_params: a beat is not a change of plan, so it
        must bypass both the change-detection cache (every beat is byte-identical to the last
        one by design) and the write pacing gate (holding a beat back drops control).
        """
        results = await self.dispatch_params(uuid, [param_entry(SUNGROW_PARAM_HEARTBEAT, scale_for_write(SUNGROW_PARAM_HEARTBEAT, heartbeat_declared_interval(self.heartbeat_interval)))], task_name="Predbat heartbeat")
        if results is None:
            self.log("Warn: Sungrow heartbeat for {} was not confirmed; if the beats keep failing the inverter will revert to self-consumption".format(self._serial(uuid)))
            return False
        return True

    async def _stop_heartbeat(self):
        """Stop the heartbeat loop and wait for it to unwind."""
        self._heartbeat_stop = True
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # -------------------------------------------------------------------------
    # Automatic configuration and discovery
    # -------------------------------------------------------------------------

    async def automatic_config(self):
        """Register every discovered inverter as a SungrowCloud Predbat inverter."""
        devices = list(self.device_list)
        if not devices:
            self.log("Warn: Sungrow automatic_config found no inverters")
            return
        self.set_arg_auto("inverter_type", ["SungrowCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))
        self.set_arg_auto("soc_percent", [self._sensor_name(uuid, "soc") for uuid in devices])
        self.set_arg_auto("battery_power", [self._sensor_name(uuid, "battery_power") for uuid in devices])
        self.set_arg_auto("grid_power", [self._sensor_name(uuid, "grid_power") for uuid in devices])
        self.set_arg_auto("load_power", [self._sensor_name(uuid, "load_power") for uuid in devices])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name(uuid, "pv_power") for uuid in devices])
        # Own the sign flags rather than leaving them to whatever else configured this install.
        # base.args is shared and NOT namespaced per inverter type, so a component that
        # legitimately inverts its own grid sensor - teslemetry and fox both do - leaves that
        # key set for every inverter index, and a Sungrow inverter that never claims it
        # inherits the flip. All three are False because the publish path already emits
        # Predbat's conventions (see signed_grid_power and signed_battery_power).
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])

        # Only map an arg when EVERY inverter reports the underlying value.
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(self.energy(uuid, leaf) is not None for uuid in devices):
                self.set_arg_auto(leaf, [self._sensor_name(uuid, leaf) for uuid in devices])
            else:
                self.log("Warn: Sungrow not every inverter reports {}, it must be set manually in apps.yaml".format(leaf))

        if all(self.battery_capacity(uuid) > 0 for uuid in devices):
            self.set_arg_auto("soc_max", [self._sensor_name(uuid, "battery_capacity") for uuid in devices])
        else:
            self.log("Warn: Sungrow could not derive a battery capacity for every inverter (it needs both the chargeable and dischargeable energy points), soc_max must be set manually in apps.yaml")
        if all(self.battery_rate_max(uuid) > 0 for uuid in devices):
            self.set_arg_auto("battery_rate_max", [self._sensor_name(uuid, "battery_rate_max") for uuid in devices])
        else:
            self.log("Warn: Sungrow no battery power limit available, battery_rate_max must be set manually in apps.yaml or with sungrow_battery_rate_max")
        # Deliberately NOT auto-mapped. Nothing in the measuring points is the SITE's grid
        # connection limit, and a G98/G99-capped site can sit far below the inverter rating.
        # Unlike battery_rate_max, nothing measures and reports this error back, so a guess
        # would over-export silently. Predbat falls back to 99999W until the user sets it.
        self.log("Warn: Sungrow does not report an export power limit; set export_limit in apps.yaml if your grid connection is capped below the inverter rating, otherwise Predbat will plan exports it cannot deliver")

        self.set_arg_auto("reserve", [self._control_name("number", uuid, "battery_schedule_reserve") for uuid in devices])
        self.set_arg_auto("charge_start_time", [self._control_name("select", uuid, "battery_schedule_charge_start_time") for uuid in devices])
        self.set_arg_auto("charge_end_time", [self._control_name("select", uuid, "battery_schedule_charge_end_time") for uuid in devices])
        self.set_arg_auto("charge_limit", [self._control_name("number", uuid, "battery_schedule_charge_soc") for uuid in devices])
        self.set_arg_auto("charge_rate", [self._control_name("number", uuid, "battery_schedule_charge_power") for uuid in devices])
        self.set_arg_auto("scheduled_charge_enable", [self._control_name("switch", uuid, "battery_schedule_charge_enable") for uuid in devices])
        self.set_arg_auto("discharge_start_time", [self._control_name("select", uuid, "battery_schedule_export_start_time") for uuid in devices])
        self.set_arg_auto("discharge_end_time", [self._control_name("select", uuid, "battery_schedule_export_end_time") for uuid in devices])
        self.set_arg_auto("discharge_target_soc", [self._control_name("number", uuid, "battery_schedule_export_soc") for uuid in devices])
        self.set_arg_auto("discharge_rate", [self._control_name("number", uuid, "battery_schedule_export_power") for uuid in devices])
        self.set_arg_auto("scheduled_discharge_enable", [self._control_name("switch", uuid, "battery_schedule_export_enable") for uuid in devices])
        self.set_arg_auto("schedule_write_button", [self._control_name("switch", uuid, "battery_schedule_charge_write") for uuid in devices])

    def _discovery_entities(self, uuid):
        """The settings automatic_config() binds for one inverter, as discovery entity descriptors.

        Each entity id is formed with the same _sensor_name()/_control_name() call
        automatic_config() makes, so the two cannot drift apart. The record describes this
        inverter alone: a setting automatic_config() binds only when a figure is known - an
        energy counter, the capacity, the rate - is described whenever this inverter reports
        it, although automatic_config() binds it only once every inverter does. pv_power and
        pv_today are described even under automatic_ignore_pv, which is the user's opt-out
        rather than a fact about the device. The three *_power_invert settings are always
        False, which is the descriptor's default, so no descriptor carries invert.
        export_limit is never bound (see automatic_config()).
        """
        entities = {
            "soc_percent": {"entity_id": self._sensor_name(uuid, "soc"), "access": "r", "unit": "%"},
            "battery_power": {"entity_id": self._sensor_name(uuid, "battery_power"), "access": "r", "unit": "W"},
            "grid_power": {"entity_id": self._sensor_name(uuid, "grid_power"), "access": "r", "unit": "W"},
            "load_power": {"entity_id": self._sensor_name(uuid, "load_power"), "access": "r", "unit": "W"},
            "pv_power": {"entity_id": self._sensor_name(uuid, "pv_power"), "access": "r", "unit": "W"},
        }
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if self.energy(uuid, leaf) is not None:
                entities[leaf] = {"entity_id": self._sensor_name(uuid, leaf), "access": "r", "unit": "kWh"}
        if self.battery_capacity(uuid) > 0:
            entities["soc_max"] = {"entity_id": self._sensor_name(uuid, "battery_capacity"), "access": "r", "unit": "kWh"}
        if self.battery_rate_max(uuid) > 0:
            entities["battery_rate_max"] = {"entity_id": self._sensor_name(uuid, "battery_rate_max"), "access": "r", "unit": "W"}

        entities["reserve"] = {"entity_id": self._control_name("number", uuid, "battery_schedule_reserve"), "access": "rw", "unit": "%"}
        for direction, prefix in (("charge", "charge"), ("export", "discharge")):
            entities["{}_start_time".format(prefix)] = {"entity_id": self._control_name("select", uuid, "battery_schedule_{}_start_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["{}_end_time".format(prefix)] = {"entity_id": self._control_name("select", uuid, "battery_schedule_{}_end_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["scheduled_{}_enable".format(prefix)] = {"entity_id": self._control_name("switch", uuid, "battery_schedule_{}_enable".format(direction)), "access": "rw", "domain": "switch"}
        entities["charge_limit"] = {"entity_id": self._control_name("number", uuid, "battery_schedule_charge_soc"), "access": "rw", "unit": "%"}
        entities["charge_rate"] = {"entity_id": self._control_name("number", uuid, "battery_schedule_charge_power"), "access": "rw", "unit": "W", "step": 100}
        entities["discharge_target_soc"] = {"entity_id": self._control_name("number", uuid, "battery_schedule_export_soc"), "access": "rw", "unit": "%"}
        entities["discharge_rate"] = {"entity_id": self._control_name("number", uuid, "battery_schedule_export_power"), "access": "rw", "unit": "W", "step": 100}
        entities["schedule_write_button"] = {"entity_id": self._control_name("switch", uuid, "battery_schedule_charge_write"), "access": "rw", "domain": "switch"}
        return entities

    def build_discovery(self):
        """Describe the discovered Sungrow inverters for the discovery catalogue.

        Reads only what discovery and the telemetry tier already hold, so this adds no API
        calls and cannot change what the inverter does. Reporting is independent of
        self.automatic: the catalogue records the hardware, and the report's own "automatic"
        flag says whether Predbat wired apps.yaml to it.

        capabilities is the SUNGROW_CAPABILITIES constant and entities is
        _discovery_entities(uuid): together they rebuild the SungrowCloud INVERTER_DEF row,
        and entities holds every setting automatic_config() binds.

        Deliberately not reported:
        - export_limit: Sungrow report no grid export limit anywhere this component can reach.
        - firmware: the OpenAPI device list carries no firmware version.
        - a battery SoH rating: SoH is a value that CHANGES, and refresh_discovery() compares
          whole reports, so reporting it would re-file the report every time it moved.
        - an AC-coupled verdict: nothing in the device list distinguishes hybrid from
          AC-coupled, and inventing one from "PV power reads zero" would call every AC-coupled
          site hybrid on a cloudy night.

        Returns None when nothing has been discovered yet, which refresh_discovery() treats as
        "nothing to report, ask again next cycle".
        """
        if not self.device_list:
            return None

        inverters = []
        for uuid in self.device_list:
            detail = self.device_detail.get(uuid, {}) or {}
            serial = self._serial(uuid)

            # PV is reported at the plant, not the device, so "does this site have solar" is a
            # plant question. A site whose plant reports PV power at all has solar; one that has
            # never reported it is recorded as battery-only rather than guessed at.
            ps_id = detail.get("ps_id")
            plant = self.plant_values.get(ps_id, {}) if ps_id else {}
            functions = ["solar", "battery"] if plant.get("total_active_power_of_pv") is not None else ["battery"]

            info = {}
            if detail.get("device_model"):
                info["model"] = str(detail["device_model"])
            elif detail.get("device_name"):
                info["model"] = str(detail["device_name"])

            ratings = {}
            battery_kwh = self.battery_capacity(uuid)
            if battery_kwh > 0:
                ratings["soc_max"] = battery_kwh
            rate_max = self.battery_rate_max(uuid)
            if rate_max > 0:
                ratings["battery_rate_max"] = rate_max

            hardware_ids = {"serial": serial, "uuid": str(uuid)}

            inverters.append(
                inverter_record(
                    "sungrow:{}".format(serial),
                    inverter_type="SungrowCloud",
                    control=bool(self.control_enable),
                    composition="direct",
                    functions=functions,
                    capabilities=dict(SUNGROW_CAPABILITIES),
                    hardware_ids=hardware_ids,
                    account_ids={"ps_id": str(ps_id)} if ps_id else None,
                    info=info,
                    ratings=ratings,
                    entities=self._discovery_entities(uuid),
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    async def load_cache(self, name):
        """Load one cache blob from the Storage component, or None when unavailable."""
        storage = self.storage
        if storage is None:
            return None
        try:
            return await storage.load(SUNGROW_STORAGE_MODULE, name)
        except Exception as error:
            self.log("Warn: Sungrow could not load the {} cache: {}".format(name, error))
            return None

    async def save_cache(self, name, data):
        """Save one cache blob to the Storage component, ignoring an absent Storage."""
        storage = self.storage
        if storage is None:
            return
        try:
            await storage.save(SUNGROW_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log("Warn: Sungrow could not save the {} cache: {}".format(name, error))

    async def save_static(self):
        """Persist discovery so a restart resumes without re-listing plants and devices."""
        await self.save_cache(SUNGROW_CACHE_STATIC, {"plant_list": self.plant_list, "plant_detail": self.plant_detail, "device_list": self.device_list, "device_detail": self.device_detail, "write_supported": self._write_supported})

    async def save_control(self):
        """Persist the write pacing clock and applied decisions.

        control_held is deliberately NOT persisted. After a restart the inverter has already
        reverted to self-consumption, or is within one heartbeat interval of doing so, and
        restoring "we hold control" would be a claim about hardware state that is no longer
        true - the component would start beating for a mode the inverter is not in and would
        never re-send the command that puts it there.
        """
        await self.save_cache(SUNGROW_CACHE_CONTROL, {"last_write_time": {str(key): value for key, value in self.last_write_time.items()}, "applied_command": {str(key): value for key, value in self.applied_command.items()}})

    async def restore_state(self):
        """Restore the caches on the first cycle, marking the guard only on success."""
        static = await self.load_cache(SUNGROW_CACHE_STATIC)
        if static:
            self.plant_list = static.get("plant_list") or []
            self.plant_detail = static.get("plant_detail") or {}
            self.device_list = static.get("device_list") or []
            self.device_detail = static.get("device_detail") or {}
            self._write_supported = static.get("write_supported") or {}
            if self.device_list:
                # Aged so the static tier still re-discovers soon after a restart rather than
                # trusting an eight-hour-old device list for a further eight hours.
                self.mark_refreshed("static", age_minutes=SUNGROW_TTL_STATIC / 2)
        control = await self.load_cache(SUNGROW_CACHE_CONTROL)
        if control:
            self.last_write_time = {key: value for key, value in (control.get("last_write_time") or {}).items()}
            self.applied_command = dict(control.get("applied_command") or {})
        self._cache_restored = True

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any control change.

        Returns True on a completed cycle, False on a failure that should hold the component
        in ComponentBase's startup backoff and be retried. Deliberately explicit rather than
        falling through to Python's implicit None: ComponentBase only clears its `first` flag
        when this returns something truthy, so an accidental None would strand the component
        in the ever-growing startup backoff forever.
        """
        if first and not self._cache_restored:
            await self.restore_state()
        if not self.appkey or not self.access_key:
            self.log("Warn: Sungrow needs both sungrow_appkey and sungrow_access_key; register an application at https://developer-api.isolarcloud.com/")
            return False
        if self.auth_method == "oauth" and not self.access_token:
            self.log("Warn: Sungrow has no OAuth access token; reconnect the Sungrow account")
            return False

        if self.tier_expired("static", SUNGROW_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: Sungrow found no energy-storage inverters on this account")
            return False

        live_ok = True
        if self.tier_expired("power", SUNGROW_TTL_POWER):
            live_ok = await self.refresh_power()

        for uuid in list(self.device_list):
            # Read the control entities EVERY tick, the first one included. Home Assistant
            # retains them across a Predbat restart, so on a restart they already hold the live
            # plan; seeding local_schedule from _empty_schedule() and publishing that back would
            # overwrite it and cancel an in-flight charge until Predbat next replanned.
            try:
                await self.get_schedule_settings_ha(uuid)
            except Exception as error:
                self.log("Warn: Sungrow schedule read failed for {}: {}".format(self._serial(uuid), error))
            await self._reconcile_control(uuid)
            await self.publish_schedule_settings_ha(uuid)

        await self.publish_data()

        # Filed right after this cycle's publish and BEFORE the deferred-startup branch below:
        # that branch returns False when the first telemetry poll fails, so a report filed any
        # later would never be filed on exactly the installs whose dump most needs to say what
        # hardware was found.
        self.refresh_discovery()

        if first and not live_ok:
            # Startup has not really succeeded without telemetry: automatic_config() runs on the
            # first cycle ALONE, so it would map only the args backed by cached ratings and
            # permanently skip soc_max and the energy args for the whole session.
            self.log("Warn: Sungrow first telemetry poll returned nothing, deferring startup; it will be retried after a backoff")
            return False

        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state and hand every held inverter back to self-consumption on shutdown.

        The heartbeat is stopped FIRST, so a beat cannot race the release and put the inverter
        straight back into external dispatch. Releasing explicitly is a courtesy rather than a
        requirement: if this never ran - a container kill, a power cut - the beats simply stop
        and the inverter reverts by itself within one interval. That is the whole point of the
        dead-man's switch, and it is why a Predbat that dies leaves a customer's battery in
        self-consumption rather than stopped or stuck on a stale setpoint.
        """
        await self._stop_heartbeat()
        for uuid in list(self.control_held):
            try:
                await self.release_control(uuid)
            except Exception as error:
                self.log("Warn: Sungrow could not release {} on shutdown: {}; the heartbeat has stopped, so it will revert to self-consumption on its own".format(self._serial(uuid), error))
        await self.save_static()
        await self.save_control()
