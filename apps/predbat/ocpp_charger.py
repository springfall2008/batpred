# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""OCPP 1.6J virtual charge point.

Presents Predbat to an OCPP central system - for example the one Intelligent Octopus Go uses,
wss://ocpp.octopus.energy - as if it were the EV charger. The central system's commands
(RemoteStartTransaction, RemoteStopTransaction, SetChargingProfile) are turned into Home
Assistant service calls that start and stop the real charger, and the real charger's power and
energy sensors are reported back to the central system as MeterValues.

The meter readings must always be the real charger's. Octopus only records a planned slot as a
completed dispatch - the slots it bills at the off-peak rate - when metered energy backs it, so a
synthesised reading would misreport what was charged. Nothing here ever invents a reading: with no
energy sensor configured, energy is integrated from the real power sensor instead.

The protocol is small enough to speak directly over aiohttp's websocket client (OCPP-J is a JSON
array per message), so this needs no OCPP library.
"""

import asyncio
import functools
import json
import uuid
from datetime import datetime, timedelta, timezone

import argparse
import base64
import os

import aiohttp

from component_base import ComponentBase

OCPP_DEFAULT_URL = "wss://ocpp.octopus.energy"
OCPP_SUBPROTOCOL = "ocpp1.6"

# OCPP-J message type ids
MSG_CALL = 2
MSG_CALLRESULT = 3
MSG_CALLERROR = 4

# BootNotification identity; OCPP limits chargePointModel to 20 characters
CHARGE_POINT_VENDOR = "Predbat"
CHARGE_POINT_MODEL = "Predbat Virtual"

# A car drawing less than this is connected but not taking energy (SuspendedEV)
CHARGING_POWER_THRESHOLD_W = 100
# A rise larger than this between two energy samples is a sensor rebase, not energy delivered
MAX_ENERGY_STEP_KWH = 25.0
# Integrating power across a gap longer than this would guess at energy nobody measured
MAX_POWER_INTEGRATION_SECONDS = 300

TICK_SECONDS = 5
CALL_TIMEOUT_SECONDS = 30
RECONNECT_MIN_SECONDS = 10
RECONNECT_MAX_SECONDS = 300
CONTROL_RETRY_SECONDS = 60
SAVE_INTERVAL_SECONDS = 300

STORAGE_MODULE = "ocpp_charger"
STORAGE_STATE = "state"

DEFAULT_PLUGGED_RESPONSE = ["yes", "on", "true", "connected", "charging", "plugged in"]

# Configuration keys the central system may read; those in READ_ONLY_KEYS reject ChangeConfiguration
DEFAULT_CONFIGURATION = {
    "HeartbeatInterval": "60",
    "MeterValueSampleInterval": "60",
    "MeterValuesSampledData": "Energy.Active.Import.Register,Power.Active.Import",
    "ClockAlignedDataInterval": "0",
    "MeterValuesAlignedData": "Energy.Active.Import.Register",
    "NumberOfConnectors": "1",
    "ChargeProfileMaxStackLevel": "10",
    "ChargingScheduleAllowedChargingRateUnit": "Current,Power",
    "ChargingScheduleMaxPeriods": "48",
    "MaxChargingProfilesInstalled": "10",
    "SupportedFeatureProfiles": "Core,SmartCharging,RemoteTrigger,LocalAuthListManagement",
}
READ_ONLY_KEYS = {"NumberOfConnectors", "ChargeProfileMaxStackLevel", "ChargingScheduleAllowedChargingRateUnit", "ChargingScheduleMaxPeriods", "MaxChargingProfilesInstalled", "SupportedFeatureProfiles"}

# OCPP 1.6 ChargePointStatus values used here
STATUS_AVAILABLE = "Available"
STATUS_PREPARING = "Preparing"
STATUS_CHARGING = "Charging"
STATUS_SUSPENDED_EV = "SuspendedEV"
STATUS_SUSPENDED_EVSE = "SuspendedEVSE"
STATUS_FINISHING = "Finishing"


class OCPPCallError(Exception):
    """The central system answered a CALL with a CALLERROR."""


def ocpp_timestamp(when):
    """Format a datetime as the UTC ISO-8601 string OCPP messages carry."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ocpp_time(value):
    """Parse an OCPP ISO-8601 timestamp into an aware UTC datetime, or None if it cannot be read."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def schedule_limit_amps(profile, now, tx_started, voltage):
    """Return the current limit in amps a charging profile sets at `now`, or None when it sets none.

    Handles Absolute, Relative (from the transaction start) and daily/weekly Recurring profiles and
    their validFrom/validTo window and schedule duration. A limit given in watts is converted with
    the period's numberPhases, taken as 1 when absent - a UK domestic supply is single phase.
    """
    valid_from = parse_ocpp_time(profile.get("validFrom"))
    valid_to = parse_ocpp_time(profile.get("validTo"))
    if (valid_from and now < valid_from) or (valid_to and now >= valid_to):
        return None

    schedule = profile.get("chargingSchedule") or {}
    kind = profile.get("chargingProfileKind", "Absolute")
    start = parse_ocpp_time(schedule.get("startSchedule"))
    if kind == "Relative":
        start = tx_started or now
    elif kind == "Recurring":
        if start is None:
            return None
        period_seconds = 7 * 24 * 3600 if profile.get("recurrencyKind") == "Weekly" else 24 * 3600
        cycles = int((now - start).total_seconds() // period_seconds)
        start = start + timedelta(seconds=cycles * period_seconds)
    elif start is None:
        start = tx_started or now

    elapsed = (now - start).total_seconds()
    duration = schedule.get("duration")
    if elapsed < 0 or (duration and elapsed >= float(duration)):
        return None

    limit = None
    phases = 1
    for period in sorted(schedule.get("chargingSchedulePeriod") or [], key=lambda item: float(item.get("startPeriod", 0))):
        if float(period.get("startPeriod", 0)) <= elapsed:
            limit = float(period.get("limit", 0))
            phases = int(period.get("numberPhases") or 1)
    if limit is None:
        return None
    if schedule.get("chargingRateUnit") == "W":
        return limit / (voltage * max(phases, 1))
    return limit


def basic_auth_header(user, password):
    """HTTP Basic auth header value, built by hand as aiohttp's auth= and BasicAuth are deprecated."""
    token = base64.b64encode("{}:{}".format(user, password or "").encode("utf-8")).decode("ascii")
    return "Basic " + token


def format_service_data(template, data):
    """Fill a service template's {placeholders} from `data`, leaving values without one untouched."""
    result = {}
    for key, value in template.items():
        if key == "service":
            continue
        if isinstance(value, str) and "{" in value:
            try:
                value = value.format(**data)
            except (KeyError, IndexError, ValueError):
                pass
        result[key] = value
    return result


class OCPPCharger(ComponentBase):
    """OCPP 1.6J virtual charge point that relays a central system's control to a real charger."""

    def initialize(self, charge_point_id=None, password=None, url=OCPP_DEFAULT_URL, power_sensor=None, energy_sensor=None, plugged_sensor=None, soc_sensor=None, voltage=230):
        """Store the configuration and reset the connection, transaction and meter state."""
        self.charge_point_id = str(charge_point_id).strip() if charge_point_id else None
        self.password = password
        self.url = (url or OCPP_DEFAULT_URL).rstrip("/")
        self.power_sensor = power_sensor
        self.energy_sensor = energy_sensor
        self.plugged_sensor = plugged_sensor
        self.soc_sensor = soc_sensor
        try:
            self.voltage = float(voltage) if voltage else 230.0
        except (TypeError, ValueError):
            self.voltage = 230.0

        self.configuration = dict(DEFAULT_CONFIGURATION)
        self.local_list_version = 0
        self.connected = False
        self.registered = False
        self.status_sent = None
        self.finishing = False
        self.transaction_id = None
        self.tx_started = None
        self.id_tag = None
        self.profiles = []
        self.charge_enabled = None
        self.charge_current = None
        self.last_control_attempt = None
        self.read_only_logged = False

        self.register_wh = 0.0
        self.power_w = 0.0
        self.soc = None
        self._last_energy_kwh = None
        self._last_power_sample = None
        self._last_meter_sent = None
        self._last_clock_slot = None
        self._last_sent = None
        self._last_saved = None
        self._last_published = None
        self._state_loaded = False

        self._ws = None
        self._task = None
        # Log every OCPP frame in both directions - the command line test turns this on
        self.trace_frames = False
        self._pending = {}
        self._call_lock = None

    # ------------------------------------------------------------------
    # Time, state and entity helpers
    # ------------------------------------------------------------------

    def clock(self):
        """Current time as an aware UTC datetime (a method so tests can pin it)."""
        return datetime.now(timezone.utc)

    def control_read_only_now(self):
        """True while Predbat is in read-only mode, when the real charger must not be commanded."""
        read_only = getattr(self.base, "set_read_only", None)
        if read_only is None:
            read_only = self.get_arg("set_read_only", False)
        return bool(read_only)

    def read_float(self, entity_id, required_unit=None):
        """Read a numeric sensor, returning None when it is missing or not a number."""
        if not entity_id:
            return None
        value = self.get_state_wrapper(entity_id, default=None, required_unit=required_unit)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def car_plugged(self):
        """Is a car plugged into the real charger?

        Reads ocpp_charger_plugged when set, else Predbat's own car_charging_planned sensor for the
        first car, matched against the same response list either way.
        """
        responses = self.get_arg("ocpp_charger_plugged_response", None) or self.get_arg("car_charging_planned_response", None) or DEFAULT_PLUGGED_RESPONSE
        responses = [str(item).lower() for item in responses]
        if self.plugged_sensor:
            state = self.get_state_wrapper(self.plugged_sensor, default=None)
        else:
            state = self.get_arg("car_charging_planned", None, index=0)
        if isinstance(state, bool):
            return state
        if state is None:
            return False
        return str(state).lower() in responses

    def maybe_publish(self, now):
        """Publish when the reported state has changed, and at least once a minute otherwise."""
        key = (self.connected, self.status_sent, self.transaction_id, self.charge_enabled, self.active_limit_amps(now))
        if self._last_published and self._last_published[0] == key and (now - self._last_published[1]).total_seconds() < 60:
            return
        self._last_published = (key, now)
        self.publish()

    def publish(self):
        """Publish the charge point's state as a Predbat sensor."""
        limit = self.active_limit_amps()
        self.dashboard_item(
            "sensor.{}_ocpp_charger_status".format(self.prefix),
            state=self.status_sent if self.connected and self.status_sent else "Disconnected",
            attributes={
                "friendly_name": "OCPP Virtual Charger",
                "icon": "mdi:ev-station",
                "connected": self.connected,
                "central_system": self.url,
                "transaction_id": self.transaction_id,
                "transaction_started": ocpp_timestamp(self.tx_started) if self.tx_started else None,
                "charging_enabled": self.charge_enabled,
                "limit_amps": round(limit, 1) if limit is not None else None,
                "power_w": round(self.power_w, 0),
                "energy_register_kwh": round(self.register_wh / 1000.0, 3),
                "profiles": len(self.profiles),
            },
            app="ocpp",
        )

    async def load_state(self):
        """Restore the meter register and any open transaction saved before a restart."""
        self._state_loaded = True
        if self.storage is None:
            return
        try:
            saved = await self.storage.load(STORAGE_MODULE, STORAGE_STATE)
        except Exception as e:
            self.log("Warn: OCPP: Could not load saved state: {}".format(e))
            return
        if not isinstance(saved, dict):
            return
        try:
            self.register_wh = max(float(saved.get("register_wh", 0.0)), 0.0)
        except (TypeError, ValueError):
            self.register_wh = 0.0
        if saved.get("transaction_id") is not None:
            self.transaction_id = saved.get("transaction_id")
            self.tx_started = parse_ocpp_time(saved.get("tx_started"))
            self.id_tag = saved.get("id_tag")
            self.profiles = [profile for profile in saved.get("profiles", []) if isinstance(profile, dict)]
            self.log("Info: OCPP: Resuming transaction {} after restart".format(self.transaction_id))

    async def save_state(self):
        """Save the meter register and open transaction so a restart neither rewinds nor loses them."""
        self._last_saved = self.clock()
        if self.storage is None:
            return
        state = {
            "register_wh": round(self.register_wh, 1),
            "transaction_id": self.transaction_id,
            "tx_started": ocpp_timestamp(self.tx_started) if self.tx_started else None,
            "id_tag": self.id_tag,
            "profiles": self.profiles if self.transaction_id is not None else [],
        }
        try:
            await self.storage.save(STORAGE_MODULE, STORAGE_STATE, state, format="json")
        except Exception as e:
            self.log("Warn: OCPP: Could not save state: {}".format(e))

    # ------------------------------------------------------------------
    # Meter
    # ------------------------------------------------------------------

    def sample_meter(self, now):
        """Read the real charger's power and advance the energy register.

        With an energy sensor the register follows its increases; a fall is a new session on a
        per-session counter, so the new reading is the energy since the reset. Without one, power
        is integrated between samples, but never across a gap too long to have been measured.
        """
        power = self.read_float(self.power_sensor, required_unit="W")
        power = max(power, 0.0) if power is not None else 0.0
        if self.energy_sensor:
            energy = self.read_float(self.energy_sensor, required_unit="kWh")
            if energy is not None:
                if self._last_energy_kwh is not None:
                    step = energy - self._last_energy_kwh if energy >= self._last_energy_kwh else energy
                    if step > MAX_ENERGY_STEP_KWH:
                        self.log("Warn: OCPP: Ignoring an energy jump of {:.1f} kWh on {} - treating it as a sensor rebase".format(step, self.energy_sensor))
                    elif step > 0:
                        self.register_wh += step * 1000.0
                self._last_energy_kwh = energy
        elif self._last_power_sample is not None:
            last_time, last_power = self._last_power_sample
            seconds = (now - last_time).total_seconds()
            if 0 < seconds <= MAX_POWER_INTEGRATION_SECONDS:
                self.register_wh += (last_power + power) / 2.0 * seconds / 3600.0
        self._last_power_sample = (now, power)
        self.power_w = power
        soc = self.read_float(self.soc_sensor)
        self.soc = soc if soc is not None and 0 <= soc <= 100 else None

    def meter_value(self, now, context, measurands):
        """Build one MeterValues entry for the requested measurands that Predbat can actually read."""
        sampled = []
        for measurand in [item.strip() for item in measurands.split(",") if item.strip()]:
            if measurand == "Energy.Active.Import.Register":
                sampled.append({"value": str(int(round(self.register_wh))), "context": context, "measurand": measurand, "unit": "Wh"})
            elif measurand == "Power.Active.Import":
                sampled.append({"value": str(int(round(self.power_w))), "context": context, "measurand": measurand, "unit": "W"})
            elif measurand == "SoC" and self.soc is not None:
                sampled.append({"value": str(int(round(self.soc))), "context": context, "measurand": measurand, "unit": "Percent"})
        if not sampled:
            sampled.append({"value": str(int(round(self.register_wh))), "context": context, "measurand": "Energy.Active.Import.Register", "unit": "Wh"})
        return {"timestamp": ocpp_timestamp(now), "sampledValue": sampled}

    async def send_meter_values(self, now, context="Sample.Periodic", measurands=None):
        """Send MeterValues for connector 1, tied to the open transaction if there is one."""
        if measurands is None:
            measurands = self.configuration.get("MeterValuesSampledData", DEFAULT_CONFIGURATION["MeterValuesSampledData"])
        payload = {"connectorId": 1, "meterValue": [self.meter_value(now, context, measurands)]}
        if self.transaction_id is not None:
            payload["transactionId"] = self.transaction_id
        await self.call("MeterValues", payload)

    # ------------------------------------------------------------------
    # Charging profiles and control
    # ------------------------------------------------------------------

    def active_limit_amps(self, now=None):
        """The current limit the installed profiles allow now, or None when none limits it.

        A TxProfile (this transaction's own) overrides a TxDefaultProfile; within a purpose the
        highest stackLevel that sets a limit wins; a ChargePointMaxProfile caps the result.
        """
        now = now or self.clock()

        def best(purpose):
            """Limit from the highest-stack profile of `purpose` that sets one now."""
            candidates = [profile for profile in self.profiles if profile.get("chargingProfilePurpose") == purpose]
            for profile in sorted(candidates, key=lambda item: int(item.get("stackLevel", 0)), reverse=True):
                limit = schedule_limit_amps(profile, now, self.tx_started, self.voltage)
                if limit is not None:
                    return limit
            return None

        limit = best("TxProfile") if self.transaction_id is not None else None
        if limit is None:
            limit = best("TxDefaultProfile")
        cap = best("ChargePointMaxProfile")
        if cap is not None:
            limit = cap if limit is None else min(limit, cap)
        return limit

    def install_profile(self, profile):
        """Install a charging profile, replacing any with the same id or the same purpose and stack level."""
        profile_id = profile.get("chargingProfileId")
        purpose = profile.get("chargingProfilePurpose")
        stack = profile.get("stackLevel")
        self.profiles = [item for item in self.profiles if item.get("chargingProfileId") != profile_id and not (item.get("chargingProfilePurpose") == purpose and item.get("stackLevel") == stack)]
        self.profiles.append(profile)

    async def call_service_template(self, config_key, data):
        """Call the Home Assistant service(s) configured under `config_key`.

        A template is a service name, a dict with a "service" key plus its data, or a list of
        either. String values may use {current} (amps) and {power} (watts). Returns True when every
        call was accepted, False when one failed, and None when nothing is configured.
        """
        templates = self.args.get(config_key, None)
        if not templates:
            return None
        if not isinstance(templates, list):
            templates = [templates]
        loop = asyncio.get_running_loop()
        all_ok = True
        for template in templates:
            if isinstance(template, str):
                service, service_data = template, {}
            elif isinstance(template, dict) and template.get("service"):
                service, service_data = str(template["service"]), format_service_data(template, data)
            else:
                self.log("Warn: OCPP: {} entry {} has no service".format(config_key, template))
                all_ok = False
                continue
            self.log("Info: OCPP: Calling {} service {} with {}".format(config_key, service, service_data))
            result = await loop.run_in_executor(None, functools.partial(self.base.call_service_wrapper, service.replace(".", "/"), **service_data))
            if not result:
                self.log("Warn: OCPP: Service {} for {} was not accepted".format(service, config_key))
                all_ok = False
        return all_ok

    async def apply_control(self, now, plugged):
        """Start or stop the real charger to match what the central system currently allows.

        The charger may only charge inside a transaction the central system started, and only while
        its charging profiles allow a non-zero current. Nothing is commanded while no car is
        plugged in or in read-only mode, and a failed command is retried at most once a minute.
        """
        limit = self.active_limit_amps(now) if self.transaction_id is not None else None
        want_on = self.transaction_id is not None and (limit is None or limit > 0)
        current = int(limit) if want_on and limit is not None else None
        if not plugged and not want_on:
            return
        if want_on == self.charge_enabled and (not want_on or current == self.charge_current):
            return
        if self.last_control_attempt and (now - self.last_control_attempt).total_seconds() < CONTROL_RETRY_SECONDS:
            return
        if self.control_read_only_now():
            if not self.read_only_logged:
                self.log("Info: OCPP: Predbat is in read-only mode, not {} the charger".format("starting" if want_on else "stopping"))
                self.read_only_logged = True
            return
        self.read_only_logged = False
        self.last_control_attempt = now
        amps = current if current is not None else 32
        data = {"current": amps, "power": int(amps * self.voltage)}
        result = await self.call_service_template("ocpp_charger_start_service" if want_on else "ocpp_charger_stop_service", data)
        if result is False:
            return
        self.charge_enabled = want_on
        self.charge_current = current
        self.last_control_attempt = None

    # ------------------------------------------------------------------
    # Transactions and status
    # ------------------------------------------------------------------

    def desired_status(self, plugged):
        """The ChargePointStatus that describes the real charger right now."""
        if not plugged:
            return STATUS_AVAILABLE
        if self.transaction_id is None:
            return STATUS_FINISHING if self.finishing else STATUS_PREPARING
        limit = self.active_limit_amps()
        if limit is not None and limit <= 0:
            return STATUS_SUSPENDED_EVSE
        return STATUS_CHARGING if self.power_w >= CHARGING_POWER_THRESHOLD_W else STATUS_SUSPENDED_EV

    async def send_status(self, status):
        """Send a StatusNotification for connector 1."""
        await self.call("StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": status, "timestamp": ocpp_timestamp(self.clock())})
        self.status_sent = status

    async def start_transaction(self, id_tag):
        """Open a transaction for `id_tag` and record the id the central system assigns."""
        now = self.clock()
        self.sample_meter(now)
        result = await self.call("StartTransaction", {"connectorId": 1, "idTag": id_tag, "meterStart": int(round(self.register_wh)), "timestamp": ocpp_timestamp(now)})
        self.transaction_id = result.get("transactionId")
        self.tx_started = now
        self.id_tag = id_tag
        self.finishing = False
        status = (result.get("idTagInfo") or {}).get("status")
        self.log("Info: OCPP: Transaction {} started for {} ({})".format(self.transaction_id, id_tag, status))
        await self.save_state()
        if status != "Accepted":
            await self.stop_transaction("DeAuthorized")

    async def stop_transaction(self, reason):
        """Close the open transaction, dropping its TxProfiles."""
        if self.transaction_id is None:
            return
        now = self.clock()
        self.sample_meter(now)
        transaction_id = self.transaction_id
        self.transaction_id = None
        self.tx_started = None
        self.profiles = [profile for profile in self.profiles if profile.get("chargingProfilePurpose") != "TxProfile"]
        self.finishing = reason != "EVDisconnected"
        await self.call("StopTransaction", {"transactionId": transaction_id, "meterStop": int(round(self.register_wh)), "timestamp": ocpp_timestamp(now), "reason": reason})
        self.log("Info: OCPP: Transaction {} stopped ({})".format(transaction_id, reason))
        await self.save_state()

    async def tick(self):
        """One pass of the connected loop: sample, follow the plug, command the charger, report."""
        now = self.clock()
        self.sample_meter(now)
        plugged = self.car_plugged()
        if not plugged:
            self.finishing = False
            if self.transaction_id is not None:
                await self.stop_transaction("EVDisconnected")
        await self.apply_control(now, plugged)

        status = self.desired_status(plugged)
        if status != self.status_sent:
            await self.send_status(status)

        interval = self.config_int("MeterValueSampleInterval")
        if interval > 0 and (self._last_meter_sent is None or (now - self._last_meter_sent).total_seconds() >= interval):
            self._last_meter_sent = now
            await self.send_meter_values(now)

        clock_interval = self.config_int("ClockAlignedDataInterval")
        if clock_interval > 0:
            slot = int(now.timestamp() // clock_interval)
            if self._last_clock_slot is not None and slot != self._last_clock_slot:
                await self.send_meter_values(now, context="Sample.Clock", measurands=self.configuration.get("MeterValuesAlignedData", "Energy.Active.Import.Register"))
            self._last_clock_slot = slot

        heartbeat = self.config_int("HeartbeatInterval")
        if heartbeat > 0 and (self._last_sent is None or (now - self._last_sent).total_seconds() >= heartbeat):
            await self.call("Heartbeat", {})

        if self._last_saved is None or (now - self._last_saved).total_seconds() >= SAVE_INTERVAL_SECONDS:
            await self.save_state()

    def config_int(self, key):
        """A configuration value as an int, 0 when it is not a number."""
        try:
            return int(float(self.configuration.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # OCPP-J transport
    # ------------------------------------------------------------------

    async def send_frame(self, frame):
        """Send one OCPP-J frame on the open websocket."""
        if self._ws is None or self._ws.closed:
            raise ConnectionError("OCPP websocket is not open")
        text = json.dumps(frame)
        if self.trace_frames:
            self.log("OCPP >>> {}".format(text))
        await self._ws.send_str(text)
        self._last_sent = self.clock()

    async def call(self, action, payload):
        """Send a CALL and wait for its CALLRESULT payload; raises on CALLERROR, timeout or disconnect.

        OCPP allows a charge point one outstanding CALL at a time, so calls are serialised.
        """
        if self._call_lock is None:
            self._call_lock = asyncio.Lock()
        async with self._call_lock:
            unique_id = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            self._pending[unique_id] = future
            try:
                await self.send_frame([MSG_CALL, unique_id, action, payload])
                return await asyncio.wait_for(future, timeout=CALL_TIMEOUT_SECONDS)
            finally:
                self._pending.pop(unique_id, None)

    async def handle_frame(self, text):
        """Dispatch one received frame: answer a CALL, or resolve the pending CALL it replies to."""
        if self.trace_frames:
            self.log("OCPP <<< {}".format(text))
        try:
            frame = json.loads(text)
        except ValueError:
            self.log("Warn: OCPP: Ignoring a frame that is not JSON: {}".format(text[:200]))
            return
        if not isinstance(frame, list) or len(frame) < 3:
            self.log("Warn: OCPP: Ignoring a malformed frame: {}".format(text[:200]))
            return
        message_type, unique_id = frame[0], frame[1]
        if message_type == MSG_CALLRESULT:
            future = self._pending.get(unique_id)
            if future and not future.done():
                future.set_result(frame[2] if isinstance(frame[2], dict) else {})
        elif message_type == MSG_CALLERROR:
            future = self._pending.get(unique_id)
            if future and not future.done():
                future.set_exception(OCPPCallError("{}: {}".format(frame[2], frame[3] if len(frame) > 3 else "")))
        elif message_type == MSG_CALL and len(frame) >= 4:
            await self.handle_call(unique_id, frame[2], frame[3] if isinstance(frame[3], dict) else {})

    async def handle_call(self, unique_id, action, payload):
        """Answer a CALL from the central system, then run whatever the answer promised."""
        self.log("Info: OCPP: Received {} {}".format(action, json.dumps(payload)))
        handler = getattr(self, "on_" + "".join("_" + char.lower() if char.isupper() else char for char in action).lstrip("_"), None)
        follow_up = None
        if handler is None:
            await self.send_frame([MSG_CALLERROR, unique_id, "NotImplemented", "{} is not supported".format(action), {}])
            return
        try:
            response, follow_up = handler(payload)
        except Exception as e:
            self.log("Warn: OCPP: {} handler failed: {}".format(action, e))
            await self.send_frame([MSG_CALLERROR, unique_id, "InternalError", str(e), {}])
            return
        await self.send_frame([MSG_CALLRESULT, unique_id, response])
        if follow_up is not None:
            # Run after the reply is sent, and as its own task: a follow-up is itself a CALL, which
            # must not be awaited from inside the reader loop that would receive its answer.
            asyncio.ensure_future(self.run_follow_up(follow_up))

    async def run_follow_up(self, coroutine):
        """Run a CALL handler's follow-up, logging rather than raising any failure."""
        try:
            await coroutine
        except Exception as e:
            self.log("Warn: OCPP: Follow-up action failed: {}".format(e))

    # ------------------------------------------------------------------
    # CALL handlers - each returns (response payload, follow-up coroutine or None)
    # ------------------------------------------------------------------

    def on_change_configuration(self, payload):
        """Store a configuration value the central system sets, refusing the read-only keys."""
        key = payload.get("key")
        if key in READ_ONLY_KEYS:
            return {"status": "Rejected"}, None
        self.configuration[key] = str(payload.get("value", ""))
        return {"status": "Accepted"}, None

    def on_get_configuration(self, payload):
        """Report the requested configuration keys, or all of them when none are named."""
        keys = payload.get("key") or list(self.configuration.keys())
        known = [{"key": key, "readonly": key in READ_ONLY_KEYS, "value": self.configuration[key]} for key in keys if key in self.configuration]
        unknown = [key for key in keys if key not in self.configuration]
        return {"configurationKey": known, "unknownKey": unknown}, None

    def on_send_local_list(self, payload):
        """Accept the local authorisation list; only its version is kept."""
        self.local_list_version = payload.get("listVersion", self.local_list_version)
        return {"status": "Accepted"}, None

    def on_get_local_list_version(self, payload):
        """Report the local authorisation list version last received."""
        return {"listVersion": self.local_list_version}, None

    def on_clear_cache(self, payload):
        """Accept a cache clear; there is no authorisation cache to clear."""
        return {"status": "Accepted"}, None

    def on_data_transfer(self, payload):
        """Refuse vendor-specific data transfers."""
        return {"status": "UnknownVendorId"}, None

    def on_reset(self, payload):
        """Refuse a reset: the charger being modelled is not Predbat's to reboot."""
        return {"status": "Rejected"}, None

    def on_change_availability(self, payload):
        """Refuse availability changes; the connector is always operative."""
        return {"status": "Rejected"}, None

    def on_unlock_connector(self, payload):
        """Report that the connector cannot be unlocked remotely."""
        return {"status": "NotSupported"}, None

    def on_get_composite_schedule(self, payload):
        """Refuse composite schedule requests."""
        return {"status": "Rejected"}, None

    def on_remote_start_transaction(self, payload):
        """Start a transaction on the central system's request, installing any profile it carries.

        Refused while a transaction is already open or no car is plugged in - the central system
        only asks once the charger reports a car, so a start without one is stale.
        """
        if self.transaction_id is not None or not self.car_plugged():
            return {"status": "Rejected"}, None
        profile = payload.get("chargingProfile")
        if isinstance(profile, dict):
            self.install_profile(profile)
        return {"status": "Accepted"}, self.start_transaction(payload.get("idTag", ""))

    def on_remote_stop_transaction(self, payload):
        """Stop the open transaction on the central system's request."""
        if self.transaction_id is None or payload.get("transactionId") != self.transaction_id:
            return {"status": "Rejected"}, None
        return {"status": "Accepted"}, self.stop_transaction("Remote")

    def on_set_charging_profile(self, payload):
        """Install a charging profile; a TxProfile needs an open transaction to attach to."""
        profile = payload.get("csChargingProfiles")
        if not isinstance(profile, dict):
            return {"status": "Rejected"}, None
        purpose = profile.get("chargingProfilePurpose")
        if purpose == "TxProfile" and self.transaction_id is None:
            return {"status": "Rejected"}, None
        if purpose not in ("TxProfile", "TxDefaultProfile", "ChargePointMaxProfile"):
            return {"status": "Rejected"}, None
        self.install_profile(profile)
        return {"status": "Accepted"}, None

    def on_clear_charging_profile(self, payload):
        """Remove the charging profiles matching every criterion the request gives."""
        profile_id = payload.get("id")
        purpose = payload.get("chargingProfilePurpose")
        stack = payload.get("stackLevel")

        def matches(profile):
            """Does `profile` match all of the request's criteria?"""
            if profile_id is not None and profile.get("chargingProfileId") != profile_id:
                return False
            if purpose is not None and profile.get("chargingProfilePurpose") != purpose:
                return False
            return stack is None or profile.get("stackLevel") == stack

        remaining = [profile for profile in self.profiles if not matches(profile)]
        cleared = len(remaining) != len(self.profiles)
        self.profiles = remaining
        return {"status": "Accepted" if cleared else "Unknown"}, None

    def on_trigger_message(self, payload):
        """Send a requested message straight after accepting the trigger."""
        requested = payload.get("requestedMessage")
        follow_ups = {
            "BootNotification": self.boot,
            "Heartbeat": lambda: self.call("Heartbeat", {}),
            "StatusNotification": lambda: self.send_status(self.desired_status(self.car_plugged())),
            "MeterValues": lambda: self.send_meter_values(self.clock()),
        }
        if requested not in follow_ups:
            return {"status": "NotImplemented"}, None
        return {"status": "Accepted"}, follow_ups[requested]()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def boot(self):
        """Register with the central system; returns True once it accepts the BootNotification."""
        result = await self.call("BootNotification", {"chargePointVendor": CHARGE_POINT_VENDOR, "chargePointModel": CHARGE_POINT_MODEL})
        interval = result.get("interval")
        if interval:
            self.configuration["HeartbeatInterval"] = str(int(interval))
        self.registered = result.get("status") == "Accepted"
        self.log("Info: OCPP: BootNotification {} (heartbeat {}s)".format(result.get("status"), interval))
        return self.registered

    async def reader(self, websocket):
        """Receive frames until the websocket closes, then fail any CALL still awaiting its answer."""
        try:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self.handle_frame(message.data)
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            # Otherwise a CALL sent just before the close would sit out its full timeout
            self.fail_pending()

    def fail_pending(self):
        """Fail every CALL still awaiting an answer, as the connection it was sent on has gone."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("OCPP connection closed"))

    async def session(self, websocket):
        """Run one connection: boot, report the connector, then tick until it closes."""
        self._ws = websocket
        self.connected = True
        self.status_sent = None
        reader = asyncio.ensure_future(self.reader(websocket))
        try:
            while not await self.boot():
                await asyncio.sleep(max(self.config_int("HeartbeatInterval"), RECONNECT_MIN_SECONDS))
            self.update_success_timestamp()
            while not self.api_stop and not reader.done() and not websocket.closed:
                try:
                    await self.tick()
                except OCPPCallError as e:
                    # The central system refused one message; the connection itself is fine
                    self.log("Warn: OCPP: Central system rejected a message: {}".format(e))
                self.maybe_publish(self.clock())
                await asyncio.sleep(TICK_SECONDS)
        finally:
            reader.cancel()
            self.connected = False
            self._ws = None
            self.fail_pending()

    async def connection_loop(self):
        """Keep a connection to the central system open, reconnecting with backoff."""
        backoff = RECONNECT_MIN_SECONDS
        url = "{}/{}".format(self.url, self.charge_point_id)
        while not self.api_stop:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, protocols=(OCPP_SUBPROTOCOL,), headers={"Authorization": basic_auth_header(self.charge_point_id, self.password)}) as websocket:
                        self.log("Info: OCPP: Connected to {} as {}".format(self.url, self.charge_point_id))
                        if websocket.protocol != OCPP_SUBPROTOCOL:
                            self.log("Warn: OCPP: {} did not agree the {} subprotocol (got {})".format(self.url, OCPP_SUBPROTOCOL, websocket.protocol))
                        backoff = RECONNECT_MIN_SECONDS
                        await self.session(websocket)
            except asyncio.CancelledError:
                break
            except aiohttp.WSServerHandshakeError as e:
                self.log("Warn: OCPP: {} refused the connection (HTTP {}) - check ocpp_charger_id and ocpp_charger_password".format(self.url, e.status))
            except Exception as e:
                self.log("Warn: OCPP: Connection error: {}".format(e))
            self.publish()
            if self.api_stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def run(self, seconds, first):
        """Start the connection task on the first call; afterwards keep it alive and publish state."""
        if first:
            if not self.charge_point_id or not self.password or not self.power_sensor:
                self.log("Error: OCPP: ocpp_charger_id, ocpp_charger_password and ocpp_charger_power must all be set")
                return False
            if not self._state_loaded:
                await self.load_state()
            self.publish()
        if self._task is None or self._task.done():
            if self._task is not None and not self._task.cancelled() and self._task.exception():
                self.log("Warn: OCPP: Connection task stopped: {}".format(self._task.exception()))
            self._task = asyncio.ensure_future(self.connection_loop())
        if self.connected:
            self.update_success_timestamp()
        return True

    async def final(self):
        """Stop the connection task and save state; an open transaction resumes after a restart."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.save_state()


# Stand-in entities for the command line test: no real charger is attached, so power is 0 W
CLI_POWER_SENSOR = "sensor.ocpp_test_power"
CLI_PLUGGED_SENSOR = "sensor.ocpp_test_plugged"


async def cli_test_central_system(charge_point_id, password, url, duration, plugged):  # pragma: no cover
    """Connect to a central system as the charger and log every OCPP message both ways.

    Nothing real is attached, so the charger reports 0 W and no energy - which is the truth - and
    its status follows from that (SuspendedEV once a session starts, never Charging). With
    `plugged` it reports a car plugged in, which makes a supplier such as Octopus plan and start
    a real smart-charging session; at the end (or on Ctrl-C) the session is closed and the car
    reported unplugged. Service calls that would control a real charger are only printed.
    """
    from mock_base import MockBase

    class QuietMockBase(MockBase):
        """MockBase that stores the status sensor without printing it every minute."""

        def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
            """Store the entity silently."""
            self.set_state_wrapper(entity_id, state, attributes)

    # Stand-in services, so the log shows when a real charger would be started and stopped
    base = QuietMockBase(ocpp_charger_plugged_response=["on"], ocpp_charger_start_service={"service": "script.ocpp_test_start", "current": "{current}"}, ocpp_charger_stop_service="script.ocpp_test_stop")
    base.entities[CLI_POWER_SENSOR] = {"state": 0}
    base.entities[CLI_PLUGGED_SENSOR] = {"state": "on" if plugged else "off"}

    def call_service_wrapper(service, **kwargs):
        """Print the service call a real setup would make, and report it accepted."""
        print("SERVICE (not called): {} {}".format(service, kwargs))
        return True

    base.call_service_wrapper = call_service_wrapper
    charger = OCPPCharger(base, charge_point_id=charge_point_id, password=password, url=url, power_sensor=CLI_POWER_SENSOR, plugged_sensor=CLI_PLUGGED_SENSOR)
    charger.trace_frames = True
    print("Connecting to {}/{} as a charger with {} for {}s - Ctrl-C to end early".format(charger.url, charge_point_id, "a car plugged in" if plugged else "no car", duration))
    task = asyncio.ensure_future(charger.connection_loop())
    try:
        await asyncio.sleep(duration)
    except asyncio.CancelledError:
        print("Interrupted - closing down")
    finally:
        if charger.connected and plugged:
            print("Reporting the car unplugged")
            base.entities[CLI_PLUGGED_SENSOR]["state"] = "off"
            for _ in range(30):
                if charger.transaction_id is None and charger.status_sent == STATUS_AVAILABLE:
                    break
                await asyncio.sleep(1)
        charger.api_stop = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    print("Done: last status {}, transaction {}".format(charger.status_sent, charger.transaction_id))


def main():  # pragma: no cover
    """Command line test: connect to an OCPP central system as a charger and log the exchange."""
    parser = argparse.ArgumentParser(description="Test an OCPP central system by connecting to it as a charger (reports 0 W - nothing real is attached)")
    parser.add_argument("--id", required=True, help="Charge point id, e.g. the Id shown in your supplier's charger settings")
    parser.add_argument("--password", default=os.environ.get("OCPP_CHARGER_PASSWORD"), help="OCPP password (or set OCPP_CHARGER_PASSWORD to keep it out of your shell history)")
    parser.add_argument("--url", default=OCPP_DEFAULT_URL, help="Central system URL, without the charge point id (default {})".format(OCPP_DEFAULT_URL))
    parser.add_argument("--plugged", action="store_true", help="Report a car plugged in. With Octopus this plans and starts a real smart-charging session, which Predbat may then plan battery charging around")
    parser.add_argument("--duration", type=int, default=240, help="Seconds to stay connected (default 240)")
    args = parser.parse_args()
    if not args.password:
        parser.error("--password or OCPP_CHARGER_PASSWORD is required")
    try:
        asyncio.run(cli_test_central_system(args.id, args.password, args.url, args.duration, args.plugged))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
