# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Ohme API library.
# Credits to: https://github.com/dan-r/ohmepy for the original code.
# -----------------------------------------------------------------------------


"""Ohme EV charger integration.

Provides monitoring and control of Ohme EV chargers including charge
session tracking, mode control (smart/max/paused), and integration with
Octopus Intelligent dispatch for coordinated EV charging.
"""

import asyncio
import json
from enum import Enum
from typing import Any, Optional, Self, Mapping
from dataclasses import dataclass
import datetime
import aiohttp
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Union
from datetime import timedelta, timezone
from const import TIME_FORMAT_HA
from component_base import ComponentBase
from predbat_metrics import record_api_call

GOOGLE_API_KEY = "AIzaSyC8ZeZngm33tpOXLpbXeKfwtyZ1WrkbdBY"  # cspell:disable-line
VERSION = "1.5.1"


JsonValueType = Union[Dict[str, "JsonValueType"], List["JsonValueType"], str, int, float, bool, None]

# Ohme attribute table for Home Assistant entities
ohme_attribute_table = {
    "mode": {"friendly_name": "Ohme Charge Mode", "icon": "mdi:ev-station"},
    "status": {"friendly_name": "Ohme Charger Status", "icon": "mdi:ev-station"},
    "power_watts": {"friendly_name": "Ohme Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power"},
    "power_amps": {"friendly_name": "Ohme Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "device_class": "current"},
    "power_volts": {"friendly_name": "Ohme Voltage", "icon": "mdi:sine-wave", "unit_of_measurement": "V", "device_class": "voltage"},
    # "ct_amps": {"friendly_name": "Ohme CT Clamp Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "device_class": "current"},
    "max_charge": {"friendly_name": "Ohme Max Charge Enabled", "icon": "mdi:battery-charging-100"},
    # "available": {"friendly_name": "Ohme Available", "icon": "mdi:connection"},
    "target_soc": {"friendly_name": "Ohme Target SOC", "icon": "mdi:battery-charging", "unit_of_measurement": "%", "device_class": "battery", "min": 0, "max": 100, "step": 1},
    "target_time": {"friendly_name": "Ohme Target Time", "icon": "mdi:clock-outline"},
    "preconditioning": {"friendly_name": "Ohme Preconditioning", "icon": "mdi:air-conditioner", "unit_of_measurement": "mins", "min": 0, "max": 60, "step": 5},
    "slots": {"friendly_name": "Ohme Charge Slots", "icon": "mdi:calendar-clock"},
    "energy": {"friendly_name": "Ohme Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "Wh", "device_class": "energy"},
    "battery_percent": {"friendly_name": "Ohme Battery Percent", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery"},
    "current_vehicle": {"friendly_name": "Ohme Current Vehicle", "icon": "mdi:car"},
    "connected": {"friendly_name": "Ohme Car Connected", "icon": "mdi:ev-plug-type2"},
    "energy_today": {"friendly_name": "Ohme Charge Energy Today", "icon": "mdi:ev-station", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    "approve_charge": {"friendly_name": "Ohme Approve Charge", "icon": "mdi:check-circle-outline"},
}

# Delivered-energy sensor built by OhmeAPI.update_energy_today() - see that method for why
# Ohme's own energy figure cannot be used for this
ENERGY_TODAY_ENTITY = "sensor.predbat_ohme_energy_today"

# How often the Predbat-led control loop re-evaluates the plan. The plan is minute-granular so a
# finer cadence buys nothing, and this matches the gateway EV charger control loop
CONTROL_INTERVAL_SECONDS = 60

# Format Predbat writes its planned car charging windows in - see PredBat.time_abs_str()
PLAN_TIME_FORMAT = "%m-%d %H:%M:%S"

# Longest gap between power readings we will still integrate over. The charge session is polled
# every 120 seconds, so a longer gap means Predbat stalled or was restarted and we have no evidence
# of what the charger did meanwhile - under-counting is safe, inventing energy is not
MAX_ENERGY_GAP_SECONDS = 600

BASE_TIME = datetime.datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(0, 24 * 60, 1)]


def time_next_occurs(hour: int, minute: int) -> datetime.datetime:
    """Find when this time next occurs."""
    current = datetime.datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= datetime.datetime.now():
        target = target + datetime.timedelta(days=1)

    return target


@dataclass
class ChargeSlot:
    """Dataclass for reporting an individual charge slot."""

    start: datetime.datetime
    end: datetime.datetime
    energy: float

    def __str__(self):
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"

    def to_dict(self) -> dict[str, JsonValueType]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "start": str(self.start.isoformat()),
            "end": str(self.end.isoformat()),
            "energy": float(self.energy),
        }


def slot_list(data: Dict[str, Any]) -> List[ChargeSlot]:
    """Get list of charge slots with energy delta summed for merged slots."""
    session_slots = data.get("allSessionSlots", [])
    if not session_slots:
        return []

    slots: List[ChargeSlot] = []

    for slot in session_slots:
        start_time = datetime.datetime.fromtimestamp(slot["startTimeMs"] / 1000).replace(microsecond=0).astimezone()
        end_time = datetime.datetime.fromtimestamp(slot["endTimeMs"] / 1000).replace(microsecond=0).astimezone()

        hours = (end_time - start_time).total_seconds() / 3600
        energy = round((slot["watts"] * hours) / 1000, 2)

        slots.append(ChargeSlot(start_time, end_time, energy))

    # Merge adjacent slots
    merged_slots: List[ChargeSlot] = []
    for slot in slots:
        if merged_slots and merged_slots[-1].end == slot.start:
            # Merge slot by extending the end time and summing energy
            merged_slots[-1] = ChargeSlot(
                merged_slots[-1].start,
                slot.end,
                merged_slots[-1].energy + slot.energy,
            )
        else:
            merged_slots.append(slot)

    return merged_slots


def vehicle_to_name(vehicle: Dict[str, Any]) -> str:
    """Translate vehicle object to human readable name."""
    if vehicle.get("name") is not None:
        return vehicle["name"]

    model: Dict[str, Any] = vehicle.get("model") or {}
    brand: Dict[str, Any] = model.get("brand") or {}

    brand_name = brand.get("name") or model.get("make") or "Unknown"
    model_name = model.get("modelName") or "Unknown"
    year_from = model.get("availableFromYear")
    year_to = model.get("availableToYear") or ""

    if year_from is None:
        return f"{brand_name} {model_name}"

    return f"{brand_name} {model_name} ({year_from}-{year_to})"


class ChargerStatus(Enum):
    """Charger state enum."""

    UNPLUGGED = "unplugged"
    PENDING_APPROVAL = "pending_approval"
    CHARGING = "charging"
    PLUGGED_IN = "plugged_in"
    PAUSED = "paused"
    FINISHED = "finished"


# Charger states that mean a car is plugged in and still wants charge, used for car_charging_planned.
# FINISHED is excluded deliberately - the car is still plugged in but has nothing left to take
CONNECTED_STATUSES = (ChargerStatus.PENDING_APPROVAL, ChargerStatus.CHARGING, ChargerStatus.PLUGGED_IN, ChargerStatus.PAUSED)


class ChargerMode(Enum):
    """Charger mode enum."""

    SMART_CHARGE = "smart_charge"
    MAX_CHARGE = "max_charge"
    PAUSED = "paused"


@dataclass
class ChargerPower:
    """Dataclass for reporting power status of charger."""

    watts: float
    amps: float
    volts: int | None
    ct_amps: float


class OhmeAPI(ComponentBase):
    """Ohme API component for EV charger integration."""

    def initialize(self, email, password, ohme_automatic=False, ohme_automatic_octopus_intelligent=None, ohme_control=False):
        """Initialise the Ohme API component"""
        self.email = email
        self.password = password
        self.client = OhmeApiClient(email, password, self.log)
        self.queued_events = []
        self.ohme_automatic = ohme_automatic
        # Tri-state: True/False force the Intelligent wiring on or off, None auto-detects it
        self.ohme_automatic_octopus_intelligent = ohme_automatic_octopus_intelligent
        self.ohme_control = ohme_control
        self.control_active = False
        self.control_windows = []
        # The state we last pushed to the charger, None until we have acted or after releasing it
        self.control_charging = None
        # Last read-only state acted on, None until the control loop has run once
        self.control_read_only = None
        # The charger's own target percent as it was before Predbat took control, restored on release
        self.control_saved_target = None
        self.energy_today = 0.0
        self.energy_today_date = None
        self.energy_last_time = None
        self.energy_last_watts = 0.0
        self.energy_restored = False

    def last_updated_time(self):
        """
        Get the last successful update time from the client
        """
        if self.client:
            return self.client.last_success_timestamp
        return None

    async def run(self, seconds, first):
        """
        Main run loop
        """
        if first:
            self.log("Ohme API: Started")

        # Process queued events
        refresh = False
        if self.queued_events:
            while self.queued_events:
                event = self.queued_events.pop(0)
                handler, *args = event
                try:
                    await handler(*args)
                except ApiException as e:
                    self.log("Warn: Ohme API: Event handler error: {}".format(e))
            refresh = True

        if first or refresh or (seconds % (30 * 60)) == 0:
            await self.client.async_update_device_info()
            # Advanced settings are broken in latest API
            # await self.client.async_get_advanced_settings()

        if first or refresh or (seconds % 120) == 0:
            await self.client.async_get_charge_session()
            await self.publish_data()

        if first and self.client.serial:
            if self.ohme_automatic:
                await self.automatic_config()
            octopus_intelligent = self.octopus_intelligent_wanted()
            if octopus_intelligent:
                await self.automatic_config_octopus_intelligent()
            self.enable_control(octopus_intelligent)

        if self.control_active and (seconds % CONTROL_INTERVAL_SECONDS) == 0:
            await self.control_charge()

        self.update_success_timestamp()
        return True

    def enable_control(self, octopus_intelligent):
        """
        Decide whether Predbat-led charge control should run, and say why when it will not.
        """
        if not self.ohme_control:
            return
        if not self.ohme_automatic:
            self.log("Warn: Ohme API: ohme_control needs ohme_automatic set to register the car, charge control is disabled")
            return
        if octopus_intelligent:
            self.log("Warn: Ohme API: ohme_control is ignored while the Intelligent slots come from Ohme - Octopus already schedules the charge")
            return
        self.control_active = True
        self.log("Info: Ohme API: Predbat-led charge control enabled")

    def control_read_only_now(self):
        """
        Is Predbat in read only mode - the effective state rather than just the switch.

        axle_control forces read only by setting the attribute without touching the config arg, so
        read the attribute first and fall back to the arg for the window before it is first set.
        """
        read_only = getattr(self.base, "set_read_only", None)
        if read_only is None:
            read_only = self.get_arg("set_read_only", False)
        return bool(read_only)

    def charger_mode(self):
        """
        The charger's current mode, or None when there is no session to read it from.
        """
        try:
            return self.client.mode
        except (KeyError, TypeError):
            return None

    def charger_target(self):
        """
        The charger's own target percent, or None when there is no rule to read it from.
        """
        try:
            return self.client.target_soc
        except (KeyError, TypeError):
            return None

    def refresh_car_windows(self):
        """
        Read Predbat's planned car charging windows into control_windows.

        The binary sensor's own on/off state only refreshes on Predbat's 5 minute cycle, so the
        planned attribute is parsed and evaluated against the clock here instead - otherwise every
        window boundary would be acted on up to 5 minutes late.

        Returns True once a plan has been read, False while the sensor has never been published -
        which is what stops the loop pausing a car on startup before it knows anything.
        """
        planned = self.get_state_wrapper("binary_sensor." + self.prefix + "_car_charging_slot", attribute="planned")
        if planned is None:
            return False

        now = self.now_utc_exact
        windows = []
        for window in planned:
            try:
                start = self.local_tz.localize(datetime.datetime.strptime(window["start"], PLAN_TIME_FORMAT).replace(year=now.year))
                end = self.local_tz.localize(datetime.datetime.strptime(window["end"], PLAN_TIME_FORMAT).replace(year=now.year))
            except (KeyError, TypeError, ValueError):
                continue
            # The plan carries no year, so rebuild it around now for windows that cross New Year
            if start < now - timedelta(hours=23):
                start = start.replace(year=start.year + 1)
                end = end.replace(year=end.year + 1)
            elif end < start:
                end = end.replace(year=end.year + 1)
            windows.append((start, end))
        self.control_windows = windows
        return True

    def should_charge_now(self):
        """
        Is now inside one of Predbat's planned charging windows.
        """
        now = self.now_utc_exact
        return any(start <= now < end for start, end in self.control_windows)

    def control_drifted(self, should_charge):
        """
        Has the charger moved away from the state we last set, e.g. changed in the Ohme app.

        Purely edge-triggered control diverges silently once anything else touches the charger, so
        the mode we already poll is compared against what we asked for and re-applied if it moved.
        """
        if self.control_charging is None:
            return False
        mode = self.charger_mode()
        if mode is None:
            # Nothing plugged in to correct
            return False
        if should_charge:
            return mode is not ChargerMode.MAX_CHARGE
        return mode is not ChargerMode.PAUSED

    async def release_charger(self):
        """
        Hand the charger back to Ohme's own smart schedule.
        """
        if self.control_charging is None:
            return
        self.log("Info: Ohme API: Read only mode, releasing the charger back to Ohme")
        if not self.control_charging:
            await self.client.async_resume_charge()
        await self.client.async_max_charge(False)
        # Max charge overrides the charger's own target percent, so put back what the user had
        # before Predbat took over - otherwise Ohme's smart schedule is left charging to the wrong
        # level once we hand it back
        if self.control_saved_target is not None:
            await self.client.async_set_target(target_percent=self.control_saved_target)
            self.log("Info: Ohme API: Restored the charger target to {}%".format(self.control_saved_target))
            self.control_saved_target = None
        self.control_charging = None

    async def control_charge(self):
        """
        Drive the charger from Predbat's car charging plan.

        Predbat holds the charger for as long as it is in control: max charge inside a planned
        window, paused outside one. Read only mode is the release - it hands the charger back to
        Ohme rather than leaving a pause in place. A component stop deliberately does not release,
        as that is nearly always a restart and releasing would glitch an in-progress charge.
        """
        if self.control_read_only_now():
            if self.control_read_only is not True:
                self.control_read_only = True
                await self.release_charger()
            return
        if self.control_read_only:
            self.log("Info: Ohme API: Read only mode cleared, resuming charge control")
        self.control_read_only = False

        if not self.refresh_car_windows():
            return

        should_charge = self.should_charge_now()
        drifted = self.control_drifted(should_charge)
        if should_charge == self.control_charging and not drifted:
            return

        if drifted:
            self.log("Info: Ohme API: Charger was changed away from what Predbat set, re-applying")
        if should_charge:
            # Snapshot the user's target before max charge overrides it, so release can put it back
            if self.control_saved_target is None:
                self.control_saved_target = self.charger_target()
            self.log("Info: Ohme API: Charge window active, setting max charge")
            await self.client.async_max_charge(True)
        else:
            self.log("Info: Ohme API: Outside the charge plan, pausing the charger")
            await self.client.async_pause_charge()
        self.control_charging = should_charge

    def octopus_intelligent_wanted(self):
        """
        Decide whether to take the Octopus Intelligent car slots from Ohme.

        The apps.yaml flag is a tri-state. Set explicitly it always wins, so a user who wants the
        slots read straight from Octopus (or who has no Octopus component for us to ask) keeps
        control. Left unset it is auto-detected, but only when ohme_automatic is on - otherwise
        enabling this would start rewiring the config of every existing Ohme user who has asked
        Predbat for nothing.
        """
        if self.ohme_automatic_octopus_intelligent is not None:
            return bool(self.ohme_automatic_octopus_intelligent)
        if not self.ohme_automatic:
            return False

        # OctopusAPI has already detected this by the time we run - it sits earlier in
        # COMPONENT_LIST and Components.start() waits for each component's first run in turn
        octopus = self.base.components.get_component("octopus") if self.base.components else None
        if not octopus:
            return False
        tariff_code = (getattr(octopus, "tariffs", {}) or {}).get("import", {}).get("tariffCode")
        if not octopus.is_intelligent_go_tariff(tariff_code):
            return False
        self.log("Info: Ohme API: Detected Intelligent Octopus tariff {}, taking the car slots from Ohme".format(tariff_code))
        return True

    async def automatic_config(self):
        """
        Register the Ohme charger with Predbat as a car.

        Covers everything Ohme can tell us about the car itself. The Octopus Intelligent slot
        wiring is deliberately separate - see automatic_config_octopus_intelligent().
        """
        self.log("Info: Ohme API: Registering the Ohme charger as a car")
        if self.get_arg("num_cars", 0) < 1:
            self.set_arg("num_cars", 1)
        self.set_arg("car_charging_planned", ["binary_sensor.predbat_ohme_connected"])
        self.set_arg("car_charging_soc", ["sensor.predbat_ohme_battery_percent"])

        # Wire up the delivered-energy sensor so car_charging_hold can subtract car charging
        # precisely instead of falling back to the car_charging_threshold heuristic. This runs
        # before auto_config(final=True), so an unmatched regex from the apps.yaml default is
        # still present as its literal "re:" string rather than having been removed yet - treat
        # that as unconfigured, but leave a real charger (Zappi, Wallbox, hand-set sensor) alone.
        existing = self.get_arg("car_charging_energy", default=None, indirect=False)
        if (not existing) or (isinstance(existing, str) and existing.startswith("re:")):
            self.set_arg_auto("car_charging_energy", ENERGY_TODAY_ENTITY)
        else:
            self.log("Info: Ohme API: Leaving car_charging_energy set to {} rather than using {}".format(existing, ENERGY_TODAY_ENTITY))

    async def automatic_config_octopus_intelligent(self):
        """
        Automatically set the predbat entities to take the Intelligent car slots from Ohme.

        Claims the car slot args so OctopusAPI.automatic_config() stops re-wiring them to its own
        dispatch entities - it re-runs whenever the tariff or intelligent device set moves, which
        would otherwise silently undo this part way through a run.
        """
        self.log("Info: Ohme API: Setting Predbat to use Ohme")
        self.base.car_slot_owner = "ohme"
        self.set_arg("octopus_intelligent_slot", "binary_sensor.predbat_ohme_slot_active")
        self.set_arg("octopus_ready_time", "select.predbat_ohme_target_time")
        self.set_arg("octopus_charge_limit", "number.predbat_ohme_target_percent")

    def restore_energy_today(self, now):
        """
        Seed today's charge energy from the sensor published before the last restart.

        Without this a Predbat restart drops the running total back to zero mid-day, and any
        charging already delivered today stops being subtracted from the load history.
        """
        self.energy_today_date = now.date()
        previous_date = self.base.load_previous_value_from_ha(ENERGY_TODAY_ENTITY, attribute="energy_date")
        if previous_date != now.date().isoformat():
            # Nothing published yet, or a total left over from an earlier day
            return
        try:
            self.energy_today = max(0.0, float(self.base.load_previous_value_from_ha(ENERGY_TODAY_ENTITY)))
        except (TypeError, ValueError):
            return
        self.log("Info: Ohme API: Restored {} kWh of charge energy already delivered today".format(self.energy_today))

    def update_energy_today(self, watts, now):
        """
        Integrate the charger's power reading into a daily incrementing energy total (kWh).

        Ohme reports the car's absolute battery content rather than the energy the charger has
        delivered, so its own energy figure cannot drive car_charging_energy - it reads zero for
        cars that do not report SoC, and jumps by the whole battery content for those that do.
        Summing power over time gives a real delivered-energy figure instead, which is what the
        Home Assistant Ohme integration now recommends since its energy sensor was removed.

        A left Riemann sum is used - each interval is charged at the power seen at its start - so
        the ramp missed at the beginning of a charge is traded against the tail counted at the end
        rather than systematically over-counting.
        """
        if not self.energy_restored:
            self.energy_restored = True
            self.restore_energy_today(now)

        last_time = self.energy_last_time
        last_watts = self.energy_last_watts
        self.energy_last_time = now
        self.energy_last_watts = watts if watts and watts > 0 else 0.0

        if last_time is None or now <= last_time:
            return self.energy_today

        gap_seconds = (now - last_time).total_seconds()
        interval_seconds = gap_seconds

        # Roll the day over before any early return below, so a stall spanning midnight cannot
        # leave yesterday's total being published against yesterday's date into the new day
        if self.energy_today_date != now.date():
            # Past midnight - the finished day's total stays in Home Assistant's history, so start
            # again and count only the part of this interval that falls on the new day. The time of
            # day is by definition the time elapsed since local midnight, so take it from the wall
            # clock rather than building a midnight datetime - there is then no timezone offset to
            # attach, and none to get wrong
            self.energy_today = 0.0
            self.energy_today_date = now.date()
            interval_seconds = min(interval_seconds, now.hour * 3600 + now.minute * 60 + now.second)

        # Measured against the original gap, not the part of it that fell after midnight - a stall
        # is no more evidence of what the charger did either side of midnight
        if gap_seconds > MAX_ENERGY_GAP_SECONDS:
            self.log("Warn: Ohme API: {}s since the last power reading, not counting that gap rather than assuming the charger ran throughout".format(int(gap_seconds)))
            return self.energy_today

        self.energy_today += last_watts * interval_seconds / 3600.0 / 1000.0
        return self.energy_today

    async def publish_data(self):
        """
        Publish data to HA using dashboard_item
        """
        mode = self.client.mode
        status = self.client.status
        power = self.client.power
        max_charge = self.client.max_charge
        available = self.client.available
        target_soc = self.client.target_soc
        target_time = self.client.target_time
        preconditioning = self.client.preconditioning
        slots = self.client.slots
        energy = self.client.energy
        battery = self.client.battery
        vehicle = self.client.current_vehicle

        # self.log("Info: Ohme API: Mode: %s, Status: %s, Power: %sW, %sA, %sV, CT: %sA, Max Charge: %s, Available: %s, Target SoC: %s%%, Target Time: %s, Preconditioning: %s mins, Vehicle: %s, Slots: %s" % (
        #         mode, status, power.watts, power.amps, power.volts, power.ct_amps, max_charge, available, target_soc,
        #         target_time, preconditioning, vehicle, slots)
        #        )

        # Create entity name prefix
        entity_name_sensor = "sensor.predbat_ohme"
        entity_name_number = "number.predbat_ohme"
        entity_name_select = "select.predbat_ohme"
        entity_name_switch = "switch.predbat_ohme"
        entity_name_binary_sensor = "binary_sensor.predbat_ohme"

        # Publish mode and status
        if mode is None:
            mode = "disconnected"
        else:
            mode = str(mode.value)
        self.dashboard_item(entity_name_sensor + "_mode", state=mode, attributes=ohme_attribute_table.get("mode", {}), app="ohme")

        if status is None:
            status = "unknown"
        else:
            status = str(status.value)
        self.dashboard_item(entity_name_sensor + "_status", state=status, attributes=ohme_attribute_table.get("status", {}), app="ohme")

        # Publish power data
        if power:
            self.dashboard_item(entity_name_sensor + "_power_watts", state=power.watts, attributes=ohme_attribute_table.get("power_watts", {}), app="ohme")
            self.dashboard_item(entity_name_sensor + "_power_amps", state=power.amps, attributes=ohme_attribute_table.get("power_amps", {}), app="ohme")
            self.dashboard_item(entity_name_sensor + "_power_volts", state=power.volts, attributes=ohme_attribute_table.get("power_volts", {}), app="ohme")
            # self.dashboard_item(entity_name_sensor + "_ct_amps", state=power.ct_amps, attributes=ohme_attribute_table.get("ct_amps", {}), app="ohme")

        # A car is plugged in and still wants charge - drives car_charging_planned under ohme_automatic
        self.dashboard_item(entity_name_binary_sensor + "_connected", state="on" if self.client.status in CONNECTED_STATUSES else "off", attributes=ohme_attribute_table.get("connected", {}), app="ohme")

        # Delivered-energy total, suitable for car_charging_energy unlike Ohme's own energy figure
        # Predbat's configured timezone, not the host's - the daily reset and the date this total is
        # filed under have to follow the user's local midnight, which a UTC container would not
        energy_today = self.update_energy_today(power.watts if power else 0, self.now_utc_exact)
        energy_today_attributes = ohme_attribute_table.get("energy_today", {}).copy()
        energy_today_attributes["energy_date"] = self.energy_today_date.isoformat() if self.energy_today_date else None
        self.dashboard_item(ENERGY_TODAY_ENTITY, state=round(energy_today, 3), attributes=energy_today_attributes, app="ohme")

        # Publish boolean states
        self.dashboard_item(entity_name_switch + "_max_charge", state=max_charge, attributes=ohme_attribute_table.get("max_charge", {}), app="ohme")
        self.dashboard_item(entity_name_binary_sensor + "_available", state="on" if available else "off", attributes=ohme_attribute_table.get("available", {}), app="ohme")

        # Publish target data
        self.dashboard_item(entity_name_number + "_target_percent", state=target_soc, attributes=ohme_attribute_table.get("target_soc", {}), app="ohme")

        # Target time
        target_time_str = "00:00"
        if target_time and len(target_time) == 2:
            target_time_str = f"{target_time[0]:02d}:{target_time[1]:02d}"
        target_attributes = ohme_attribute_table.get("target_time", {})
        target_attributes["options"] = OPTIONS_TIME
        self.dashboard_item(entity_name_select + "_target_time", state=target_time_str, attributes=target_attributes, app="ohme")

        # Publish preconditioning
        self.dashboard_item(entity_name_number + "_preconditioning", state=preconditioning, attributes=ohme_attribute_table.get("preconditioning", {}), app="ohme")

        # Publish slot information
        num_slots = len(slots) if slots else 0
        slot_attributes = ohme_attribute_table.get("slots", {}).copy()

        planned_dispatches = []
        completed_dispatches = []
        slot_active = False
        for slot in slots:
            start = slot.start
            end = slot.end
            slot_energy = slot.energy  # Renamed to avoid collision with session energy variable
            is_completed = False
            if end < datetime.datetime.now().astimezone():
                is_completed = True
            if start <= datetime.datetime.now().astimezone() <= end:
                slot_active = True
            dispatch = {"start": start.strftime(TIME_FORMAT_HA), "end": end.strftime(TIME_FORMAT_HA), "energy": -slot_energy, "location": "AT_HOME"}
            if is_completed:
                completed_dispatches.append(dispatch)
            else:
                planned_dispatches.append(dispatch)

        if slots:
            slot_attributes["planned_dispatches"] = planned_dispatches
            slot_attributes["completed_dispatches"] = completed_dispatches
        self.dashboard_item(entity_name_binary_sensor + "_slot_active", state=slot_active, attributes=slot_attributes, app="ohme")

        # Publish energy and battery data
        self.dashboard_item(entity_name_sensor + "_energy", state=energy, attributes=ohme_attribute_table.get("energy", {}), app="ohme")
        self.dashboard_item(entity_name_sensor + "_battery_percent", state=battery, attributes=ohme_attribute_table.get("battery_percent", {}), app="ohme")
        self.dashboard_item(entity_name_sensor + "_current_vehicle", state=vehicle, attributes=ohme_attribute_table.get("current_vehicle", {}), app="ohme")

        # Approve charge switch
        self.dashboard_item(entity_name_switch + "_approve_charge", state="off", attributes=ohme_attribute_table.get("approve_charge", {}), app="ohme")

    # Event stubs to queue for main thread
    async def select_event(self, entity_id, value):
        self.queued_events.append((self.select_event_handler, entity_id, value))

    async def number_event(self, entity_id, value):
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def switch_event(self, entity_id, service):
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    # event handlers
    async def select_event_handler(self, entity_id, value):
        """
        Select event
        """
        if entity_id.endswith("_target_time"):
            if value in OPTIONS_TIME:
                hour, minute = map(int, value.split(":"))
                await self.client.async_apply_session_rule(target_time=(hour, minute))
                self.log(f"Info: Ohme API: Set target time to {hour:02d}:{minute:02d}")
            else:
                self.log(f"Warn: Ohme API: Invalid target time value: {value}")

    async def number_event_handler(self, entity_id, value):
        """
        Number event
        """
        # Must match the entity published by publish_data() (number.predbat_ohme_target_percent),
        # which is also what ohme_automatic_octopus_intelligent binds octopus_charge_limit to
        if entity_id.endswith("_target_percent"):
            if (isinstance(value, float) or isinstance(value, int)) and 0 <= value <= 100:
                await self.client.async_apply_session_rule(target_percent=int(value))
            else:
                self.log(f"Warn: Ohme API: Invalid target SoC value: {value}")
        elif entity_id.endswith("_preconditioning"):
            try:
                value = int(value)
            except (ValueError, TypeError):
                self.log(f"Warn: Ohme API: Invalid preconditioning value: {value}")
                return
            if value == 0:
                self.log(f"Info: Ohme API: Set preconditioning to off")
                await self.client.async_apply_session_rule(pre_condition=True)
            else:
                self.log(f"Info: Ohme API: Set preconditioning length to {int(value)} mins")
                await self.client.async_apply_session_rule(pre_condition=True, pre_condition_length=int(value))

    async def switch_event_handler(self, entity_id, service):
        """
        Switch event
        """
        if entity_id.endswith("_max_charge"):
            if service == "turn_on":
                await self.client.async_max_charge(True)
            elif service == "turn_off":
                await self.client.async_max_charge(False)
        elif entity_id.endswith("_approve_charge"):
            if service == "turn_on":
                if self.client.status != ChargerStatus.PENDING_APPROVAL:
                    self.log("Warn: Ohme API: Charger not pending approval")
                    return
                await self.client.async_approve_charge()
                self.log("Info: Ohme API: Approved charge")


class OhmeApiClient:
    """API client for Ohme EV chargers."""

    def __init__(self, email: str, password: str, log):
        if email is None or password is None:
            raise AuthException("Credentials not provided")

        # Credentials from configuration
        self.email = email
        self._password = password
        self.log = log

        # Charger and its capabilities
        self.device_info: dict[str, Any] = {}
        self._charge_session: dict[str, Any] = {}
        self._advanced_settings: dict[str, Any] = {}
        self._next_session: dict[str, Any] = {}
        self._cars: list[Any] = []

        self.energy: float = 0.0
        self.battery: int = 0

        self._capabilities: dict[str, bool | str | list[str]] = {}
        self._configuration: dict[str, bool | str] = {}
        self.ct_connected: bool = False
        self.cap_available: bool = True
        self.cap_enabled: bool = False
        self.solar_capable: bool = False

        # Authentication
        self._token_birth: float = 0.0
        self._token: str | None = None
        self._refresh_token: str | None = None

        # User info
        self.serial = ""

        # Sessions
        self._session = None
        self._close_session = False
        self._timeout = 10
        self._last_rule: dict[str, Any] = {}
        self.last_success_timestamp = None

    # Auth methods

    async def async_login(self) -> bool:
        """Refresh the user auth token from the stored credentials."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True

        async with asyncio.timeout(self._timeout):
            async with self._session.post(
                f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={GOOGLE_API_KEY}",
                data={
                    "email": self.email,
                    "password": self._password,
                    "returnSecureToken": True,
                },
            ) as resp:
                if resp.status != 200:
                    raise AuthException("Incorrect credentials")

                resp_json = await resp.json()
                self._token_birth = time.time()
                self._token = resp_json["idToken"]
                self._refresh_token = resp_json["refreshToken"]
                return True
        raise AuthException("Incorrect credentials")

    async def _async_refresh_session(self) -> bool:
        """Refresh auth token if needed."""
        if self._token is None:
            return await self.async_login()

        # Don't refresh token unless its over 45 mins old
        if time.time() - self._token_birth < 2700:
            return True

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True

        async with asyncio.timeout(self._timeout):
            async with self._session.post(
                f"https://securetoken.googleapis.com/v1/token?key={GOOGLE_API_KEY}",
                data={
                    "grantType": "refresh_token",
                    "refreshToken": self._refresh_token,
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    msg = f"Warn: Ohme auth refresh: {text}"
                    self.log(msg)
                    raise AuthException(msg)

                resp_json = await resp.json()
                self._token_birth = time.time()
                self._token = resp_json["id_token"]
                self._refresh_token = resp_json["refresh_token"]
                return True

    # Internal methods

    async def _handle_api_error(self, url: str, resp: aiohttp.ClientResponse):
        """Raise an exception if API response failed."""
        if resp.status != 200:
            text = await resp.text()
            msg = f"Warn:Ohme API response error: {url}, {resp.status}; {text}"
            self.log(msg)
            if resp.status in (401, 403):
                record_api_call("ohme", False, "auth_error")
            else:
                record_api_call("ohme", False, "server_error")
            raise ApiException(msg)

    async def _make_request(
        self,
        method: str,
        url: str,
        data: Optional[Mapping[str, str | bool]] = None,
        skip_json: bool = False,
    ):
        """Make an HTTP request."""
        await self._async_refresh_session()

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True

        async with asyncio.timeout(self._timeout):
            try:
                async with self._session.request(
                    method=method,
                    url=f"https://api.ohme.io{url}",
                    data=json.dumps(data) if data and method in {"PUT", "POST"} else data,
                    headers={
                        "Authorization": f"Firebase {self._token}",
                        "Content-Type": "application/json",
                        "User-Agent": f"ohmepy/{VERSION}",
                    },
                ) as resp:
                    # self.log("Info: %s request to %s, status code %s" % (method, url, resp.status))
                    await self._handle_api_error(url, resp)

                    if skip_json and method == "POST":
                        result = await resp.text()
                    else:
                        result = await resp.json() if method != "PUT" else True
                    record_api_call("ohme")
                    return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                record_api_call("ohme", False, "connection_error")
                raise ApiException(f"Ohme connection error: {e}") from e

    def _charge_in_progress(self) -> bool:
        """Is a charge in progress? Used to determine if schedule or session should be adjusted."""
        return self.status is not ChargerStatus.UNPLUGGED and self.status is not ChargerStatus.PENDING_APPROVAL

    # Simple getters

    def is_capable(self, capability: str) -> bool:
        """Return whether or not this model has a given capability."""
        return bool(self._capabilities[capability])

    def configuration_value(self, value: str) -> bool:
        """Return a boolean configuration value."""
        return bool(self._configuration.get(value))

    @property
    def status(self) -> ChargerStatus:
        """Return status from enum."""
        if self._charge_session["mode"] == "PENDING_APPROVAL":
            return ChargerStatus.PENDING_APPROVAL
        elif self._charge_session["mode"] == "DISCONNECTED":
            return ChargerStatus.UNPLUGGED
        elif self._charge_session["mode"] == "STOPPED":
            return ChargerStatus.PAUSED
        elif self._charge_session["mode"] == "FINISHED_CHARGE":
            return ChargerStatus.FINISHED
        elif self._charge_session.get("power") and self._charge_session["power"].get("watt", 0) > 0:
            return ChargerStatus.CHARGING
        else:
            return ChargerStatus.PLUGGED_IN

    @property
    def mode(self) -> Optional[ChargerMode]:
        """Return status from enum."""
        if self._charge_session["mode"] == "SMART_CHARGE":
            return ChargerMode.SMART_CHARGE
        elif self._charge_session["mode"] == "MAX_CHARGE":
            return ChargerMode.MAX_CHARGE
        elif self._charge_session["mode"] == "STOPPED":
            return ChargerMode.PAUSED

        return None

    @property
    def max_charge(self) -> bool:
        """Get if max charge is enabled."""
        return self._charge_session.get("mode") == "MAX_CHARGE"

    @property
    def available(self) -> bool:
        """CT reading."""
        return self._advanced_settings.get("online", False)

    @property
    def power(self) -> ChargerPower:
        """Return all power readings."""

        charge_power = self._charge_session.get("power") or {}
        return ChargerPower(
            watts=charge_power.get("watt", 0),
            amps=charge_power.get("amp", 0),
            volts=charge_power.get("volt", None),
            ct_amps=self._advanced_settings.get("clampAmps", 0),
        )

    @property
    def target_soc(self) -> int:
        """Target state of charge."""
        if self.status is ChargerStatus.PAUSED and self._charge_session.get("suspendedRule") is not None:
            return self._charge_session.get("suspendedRule", {}).get("targetPercent", 0)
        elif self._charge_in_progress():
            return int(self._charge_session["appliedRule"]["targetPercent"])

        return int(self._next_session.get("targetPercent", 0))

    @property
    def target_time(self) -> tuple[int, int]:
        """Target state of charge."""
        if self._charge_in_progress():
            target = int(self._charge_session["appliedRule"]["targetTime"])
        else:
            target = int(self._next_session.get("targetTime", 0))

        return (target // 3600, (target % 3600) // 60)

    @property
    def preconditioning(self) -> int:
        """Preconditioning time."""
        if self._charge_in_progress():
            if self._last_rule.get("preconditioningEnabled"):
                return int(self._last_rule.get("preconditionLengthMins", 0))
        else:
            if self._next_session.get("preconditioningEnabled"):
                return int(self._next_session.get("preconditionLengthMins", 0))

        return 0

    @property
    def slots(self) -> list[ChargeSlot]:
        """Slot list."""
        return slot_list(self._charge_session)

    @property
    def next_slot_start(self) -> datetime.datetime | None:
        """Next slot start."""
        return min(
            (slot.start for slot in self.slots if slot.start > datetime.datetime.now().astimezone()),
            default=None,
        )

    @property
    def next_slot_end(self) -> datetime.datetime | None:
        """Next slot start."""
        return min(
            (slot.end for slot in self.slots if slot.end > datetime.datetime.now().astimezone()),
            default=None,
        )

    @property
    def vehicles(self) -> list[str]:
        """Return a list of vehicle names."""
        output = []
        for vehicle in self._cars:
            output.append(vehicle_to_name(vehicle))
        return output

    @property
    def current_vehicle(self) -> Optional[str]:
        """Returns the name of the currently selected vehicle."""
        # The selected vehicle is the first one in this list
        if len(self._cars) > 0:
            return vehicle_to_name(self._cars[0])
        return None

    # Push methods

    async def async_pause_charge(self) -> bool:
        """Pause an ongoing charge"""
        result = await self._make_request("POST", f"/v1/chargeSessions/{self.serial}/stop", skip_json=True)
        return bool(result)

    async def async_resume_charge(self) -> bool:
        """Resume a paused charge"""
        result = await self._make_request("POST", f"/v1/chargeSessions/{self.serial}/resume", skip_json=True)
        return bool(result)

    async def async_approve_charge(self) -> bool:
        """Approve a charge"""
        result = await self._make_request("PUT", f"/v1/chargeSessions/{self.serial}/approve?approve=true")
        return bool(result)

    async def async_max_charge(self, state: bool = True) -> bool:
        """Enable max charge"""
        result = await self._make_request(
            "PUT",
            f"/v1/chargeSessions/{self.serial}/rule?maxCharge=" + str(state).lower(),
        )
        return bool(result)

    async def async_set_mode(self, mode: ChargerMode | str) -> None:
        """Set charger mode."""
        if isinstance(mode, str):
            mode = ChargerMode(mode)

        if mode is ChargerMode.MAX_CHARGE:
            await self.async_max_charge(True)
        elif mode is ChargerMode.SMART_CHARGE:
            await self.async_max_charge(False)
        elif mode is ChargerMode.PAUSED:
            await self.async_pause_charge()

    async def async_apply_session_rule(
        self,
        max_price: Optional[float] = None,
        target_time: Optional[tuple[int, int]] = None,
        target_percent: Optional[int] = None,
        pre_condition: Optional[bool] = None,
        pre_condition_length: Optional[int] = None,
    ) -> bool:
        """Apply rule to ongoing charge/stop max charge."""
        # Check every property. If we've provided it, use that. If not, use the existing.
        if max_price is None:
            if "settings" in self._last_rule and self._last_rule["settings"] is not None and len(self._last_rule["settings"]) > 1:
                max_price = self._last_rule["settings"][0]["enabled"]
            else:
                max_price = False

        if target_percent is None:
            target_percent = self._last_rule["targetPercent"] if "targetPercent" in self._last_rule else 80

        if pre_condition is None:
            pre_condition = self._last_rule["preconditioningEnabled"] if "preconditioningEnabled" in self._last_rule else False

        if not pre_condition_length:
            pre_condition_length = self._last_rule["preconditionLengthMins"] if ("preconditionLengthMins" in self._last_rule and self._last_rule["preconditionLengthMins"] is not None) else 30

        if target_time is None:
            # Default to 9am
            target_time_cache = self._last_rule["targetTime"] if "targetTime" in self._last_rule else 32400
            target_time = (target_time_cache // 3600, (target_time_cache % 3600) // 60)

        target_ts = int(time_next_occurs(target_time[0], target_time[1]).timestamp() * 1000)

        # Convert these to string form
        max_price_str = "true" if max_price else "false"
        pre_condition_str = "true" if pre_condition else "false"

        result = await self._make_request(
            "PUT",
            f"/v1/chargeSessions/{self.serial}/rule?enableMaxPrice={max_price_str}&targetTs={target_ts}&enablePreconditioning={pre_condition_str}&toPercent={target_percent}&preconditionLengthMins={pre_condition_length}",
        )
        return bool(result)

    async def async_change_price_cap(self, enabled: Optional[bool] = None, cap: Optional[float] = None) -> bool:
        """Change price cap settings."""
        settings = await self._make_request("GET", "/v1/users/me/settings")
        if enabled is not None:
            settings["chargeSettings"][0]["enabled"] = enabled

        if cap is not None:
            settings["chargeSettings"][0]["value"] = cap

        result = await self._make_request("PUT", "/v1/users/me/settings", data=settings)
        return bool(result)

    async def async_update_schedule(
        self,
        target_percent: Optional[int] = None,
        target_time: Optional[tuple[int, int]] = None,
        pre_condition: Optional[bool] = None,
        pre_condition_length: Optional[int] = None,
    ) -> bool:
        """Update the schedule for the next charge."""
        rule = self._next_session

        # Account for user having no rules
        if not rule:
            return False

        # Update percent and time if provided
        if target_percent is not None:
            rule["targetPercent"] = target_percent
        if target_time is not None:
            rule["targetTime"] = (target_time[0] * 3600) + (target_time[1] * 60)

        # Update pre-conditioning if provided
        if pre_condition is not None:
            rule["preconditioningEnabled"] = pre_condition
        if pre_condition_length:
            rule["preconditionLengthMins"] = pre_condition_length

        await self._make_request("PUT", f"/v1/chargeRules/{rule['id']}", data=rule)
        return True

    async def async_set_target(
        self,
        target_percent: Optional[int] = None,
        target_time: Optional[tuple[int, int]] = None,
        pre_condition_length: Optional[int] = None,
    ) -> bool:
        """Set a target time/percentage."""
        pre_condition: Optional[bool] = None
        if pre_condition_length is not None:
            pre_condition = bool(pre_condition_length)

        if self._charge_in_progress():
            await self.async_apply_session_rule(
                target_time=target_time,
                target_percent=target_percent,
                pre_condition=pre_condition,
                pre_condition_length=pre_condition_length,
            )
        else:
            await self.async_update_schedule(
                target_time=target_time,
                target_percent=target_percent,
                pre_condition=pre_condition,
                pre_condition_length=pre_condition_length,
            )
        return True

    async def async_set_configuration_value(self, values: Mapping[str, bool]) -> bool:
        """Set a configuration value or values."""
        result = await self._make_request("PUT", f"/v1/chargeDevices/{self.serial}/appSettings", data=values)
        await asyncio.sleep(1)  # The API is slow to update after this request

        return bool(result)

    async def async_set_vehicle(self, selected_name: str) -> bool:
        """Set the vehicle to be charged."""
        for vehicle in self._cars:
            if vehicle_to_name(vehicle) == selected_name:
                result = await self._make_request("PUT", f"/v1/car/{vehicle['id']}/select")

                return True
        return False

    # Pull methods

    async def async_get_charge_session(self) -> None:
        """Fetch charge sessions endpoint."""
        # Retry if state is CALCULATING or DELIVERING
        for attempt in range(3):
            resp = await self._make_request("GET", "/v1/chargeSessions")
            resp = resp[0]

            if resp.get("mode") != "CALCULATING" and resp.get("mode") != "DELIVERING":
                self.last_success_timestamp = datetime.datetime.now(timezone.utc)
                break

            if attempt < 2:  # Only sleep if there are more retries left
                await asyncio.sleep(1)

        self._charge_session = resp

        # Store last rule
        if resp["mode"] == "SMART_CHARGE" and "appliedRule" in resp:
            self._last_rule = resp["appliedRule"]

        # Get energy reading
        if self._charge_in_progress() and resp.get("batterySoc") is not None:
            self.energy = max(0, self.energy, resp["batterySoc"].get("wh") or 0)
        else:
            self.energy = 0

        self.battery = ((resp.get("car") or {}).get("batterySoc") or {}).get("percent") or (resp.get("batterySoc") or {}).get("percent") or 0

        resp = await self._make_request("GET", "/v1/chargeSessions/nextSessionInfo")
        self._next_session = resp.get("rule", {})

    async def async_get_advanced_settings(self) -> None:
        """Get advanced settings (mainly for CT clamp reading)"""
        resp = await self._make_request("GET", f"/v1/chargeDevices/{self.serial}/advancedSettings")

        self._advanced_settings = resp

        # clampConnected is not reliable, so check clampAmps being > 0 as an alternative
        if resp["clampConnected"] or (isinstance(resp.get("clampAmps"), float) and resp.get("clampAmps") > 0):
            self.ct_connected = True

    async def async_update_device_info(self) -> bool:
        """Update _device_info with our charger model."""
        resp = await self._make_request("GET", "/v1/users/me/account")
        self._cars = resp.get("cars") or []

        try:
            self.cap_enabled = resp["userSettings"]["chargeSettings"][0]["enabled"]
        except:
            pass

        device = resp["chargeDevices"][0]

        self._capabilities = device["modelCapabilities"]
        self._configuration = device["optionalSettings"]
        self.serial = device["id"]

        self.device_info = {
            "name": device["modelTypeDisplayName"],
            "model": device["modelTypeDisplayName"].replace("Ohme ", ""),
            "sw_version": device["firmwareVersionLabel"],
        }

        if resp["tariff"] is not None and resp["tariff"]["dsrTariff"]:
            self.cap_available = False

        solar_modes = device["modelCapabilities"]["solarModes"]
        if isinstance(solar_modes, list) and len(solar_modes) == 1:
            self.solar_capable = True

        return True

    async def close(self) -> None:
        """Close open client session."""
        if self._session and self._close_session:
            await self._session.close()

    async def __aenter__(self) -> Self:
        """Async enter."""
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Async exit."""
        await self.close()


# Exceptions
class ApiException(Exception):
    ...


class AuthException(ApiException):
    ...
