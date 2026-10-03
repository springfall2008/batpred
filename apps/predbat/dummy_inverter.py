# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""A simulated inverter and battery, for demonstrating Predbat and for replaying logs.

The component publishes the same kind of sensors and controls a cloud inverter component does, and wires
Predbat to them with automatic_config, so Predbat plans and controls it exactly as it would a real one. Behind
those entities sits a minute-by-minute model of a hybrid inverter with a battery: the battery charges and
discharges within its rate limits and losses, PV and battery share the inverter's AC limit, export is capped
at the export limit, and PV that cannot go anywhere is clipped. Charge and export windows written by Predbat
are obeyed; outside them the inverter runs self-consumption (Eco).

PV comes from Predbat's own forecast when it has one, otherwise from a simple clear-sky curve; load is a
constant or an hourly profile. Every setting lives in one apps.yaml block:

    dummy_inverter:
      battery_size: 10          # kWh usable
      battery_rate_max: 3600    # W, charge and discharge
      inverter_limit: 5000      # W, AC output shared by PV and battery
      export_limit: 5000        # W
      battery_loss: 0.96        # charge efficiency
      battery_loss_discharge: 0.96
      inverter_loss: 0.96       # AC <-> DC conversion for the battery
      reserve: 4                # %
      soc_initial: 50           # %
      pv_peak: 4.0              # kW, clear-sky curve peak when there is no forecast
      pv_scaling: 1.0           # applied to the forecast or curve
      load: 0.4                 # kW constant, or a list of 24 hourly kW values

The model is deliberately independent of Predbat's prediction engine, so it can catch a prediction that
disagrees with what an inverter would actually do.
"""
import math
from datetime import datetime, timedelta

from component_base import ComponentBase

DUMMY_DEFAULTS = {
    "battery_size": 10.0,
    "battery_rate_max": 3600.0,
    "inverter_limit": 5000.0,
    "export_limit": 5000.0,
    "battery_loss": 0.96,
    "battery_loss_discharge": 0.96,
    "inverter_loss": 0.96,
    "reserve": 4.0,
    "soc_initial": 50.0,
    "pv_peak": 4.0,
    "pv_scaling": 1.0,
    "sunrise": 6.5,
    "sunset": 18.5,
    "load": 0.4,
}

DUMMY_OPTIONS_TIME = [(datetime(2000, 1, 1) + timedelta(minutes=minute)).strftime("%H:%M:%S") for minute in range(0, 24 * 60)]

# Never simulate more than this many minutes in one go, so a long stall (a suspended laptop) does not
# spend minutes replaying a day the user did not see
MAX_CATCH_UP_MINUTES = 60

# The capabilities the simulated inverter has, mirroring INVERTER_DEF["DUMMY"] in config.py
DUMMY_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": False,
    "can_span_midnight": True,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": False,
}


def time_to_minute(text):
    """Convert HH:MM or HH:MM:SS to minutes past midnight, or None if it cannot be read."""
    try:
        parts = str(text).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def in_window(minute_of_day, start_text, end_text):
    """True when minute_of_day falls in the window [start, end), which may run past midnight."""
    start = time_to_minute(start_text)
    end = time_to_minute(end_text)
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def simulate_minute(params, controls, soc_kwh, pv_kw, load_kw, minute_of_day):
    """Advance the battery by one minute and return the new SoC and the minute's power flows.

    params are the configured limits and losses (DUMMY_DEFAULTS keys); controls are the charge/export windows
    and reserve as Predbat writes them. Returns (soc_kwh, flows) where flows holds kW for the minute:
    battery (+ discharging, - charging), grid (+ importing, - exporting), pv (as produced, after clipping),
    load, and clipped (PV that was available but could not be used).

    The battery sits on the DC side with the PV, so PV charging the battery does not pass through the inverter;
    battery discharge and grid charging do, and pay inverter_loss. The inverter's AC output - PV not going to the
    battery, plus battery discharge - never exceeds inverter_limit, with PV taking priority, as observed on real
    hybrids. Grid export never exceeds export_limit; anything PV still cannot place is clipped.
    """
    hours = 1.0 / 60.0
    soc_max = params["battery_size"]
    rate_max = params["battery_rate_max"] / 1000.0
    inverter_limit = params["inverter_limit"] / 1000.0
    export_limit = params["export_limit"] / 1000.0
    reserve_kwh = soc_max * float(controls.get("reserve", params["reserve"])) / 100.0
    charge = controls.get("charge", {})
    export = controls.get("export", {})

    charging = charge.get("enable") and in_window(minute_of_day, charge.get("start_time"), charge.get("end_time"))
    exporting = export.get("enable") and in_window(minute_of_day, export.get("start_time"), export.get("end_time"))

    # Room in the battery and energy above the floor, as kW that can flow for the whole minute
    room_kw = max(soc_max - soc_kwh, 0.0) / hours / params["battery_loss"]
    if charging:
        target_kwh = soc_max * float(charge.get("target_soc", 100)) / 100.0
        room_kw = min(room_kw, max(target_kwh - soc_kwh, 0.0) / hours / params["battery_loss"])
    floor_kwh = reserve_kwh
    if exporting:
        floor_kwh = max(floor_kwh, soc_max * float(export.get("target_soc", 0)) / 100.0)
    available_kw = max(soc_kwh - floor_kwh, 0.0) / hours * params["battery_loss_discharge"]

    battery_charge = 0.0  # kW into the battery, DC
    battery_discharge = 0.0  # kW out of the battery, DC
    pv_to_battery = 0.0
    grid_to_battery = 0.0

    if charging:
        # Charge at the window's rate, PV first and the grid for the rest
        rate = min(float(charge.get("rate", rate_max * 1000.0)) / 1000.0, rate_max)
        battery_charge = min(rate, room_kw)
        pv_to_battery = min(pv_kw, battery_charge)
        grid_to_battery = battery_charge - pv_to_battery
    elif exporting:
        # Discharge at the window's rate, within what the inverter has left after PV
        rate = min(float(export.get("rate", rate_max * 1000.0)) / 1000.0, rate_max)
        ac_headroom = max(inverter_limit - pv_kw, 0.0)
        battery_discharge = min(rate, available_kw, ac_headroom / params["inverter_loss"])
    else:
        # Eco: cover the load from PV, then the battery; surplus PV charges the battery
        surplus = pv_kw - load_kw
        if surplus >= 0:
            battery_charge = min(surplus, rate_max, room_kw)
            pv_to_battery = battery_charge
        else:
            ac_headroom = max(inverter_limit - pv_kw, 0.0)
            battery_discharge = min(-surplus / params["inverter_loss"], rate_max, available_kw, ac_headroom / params["inverter_loss"])

    # PV that is not charging the battery goes through the inverter, capped at the AC limit with PV first
    pv_ac = pv_kw - pv_to_battery
    clipped = max(pv_ac - inverter_limit, 0.0)
    pv_ac -= clipped
    battery_ac = battery_discharge * params["inverter_loss"]
    grid_charge_ac = grid_to_battery / params["inverter_loss"]

    # Grid balances the rest: + import, - export. Export is capped; PV takes the cut, then the battery
    grid = load_kw + grid_charge_ac - pv_ac - battery_ac
    if grid < -export_limit:
        excess = -export_limit - grid
        cut_battery = min(excess, battery_ac)
        battery_ac -= cut_battery
        battery_discharge = battery_ac / params["inverter_loss"]
        excess -= cut_battery
        pv_ac -= excess
        clipped += excess
        grid = -export_limit

    soc_kwh = soc_kwh + battery_charge * hours * params["battery_loss"] - battery_discharge * hours / params["battery_loss_discharge"]
    soc_kwh = min(max(soc_kwh, 0.0), soc_max)
    flows = {
        "battery": battery_discharge - battery_charge,
        "grid": grid,
        "pv": pv_kw - clipped,
        "load": load_kw,
        "clipped": clipped,
    }
    return soc_kwh, flows


def clear_sky_pv(params, minute_of_day):
    """A clear-sky PV curve: a half sine between sunrise and sunset peaking at pv_peak kW."""
    hour = minute_of_day / 60.0
    sunrise, sunset = params["sunrise"], params["sunset"]
    if hour <= sunrise or hour >= sunset:
        return 0.0
    return params["pv_peak"] * math.sin(math.pi * (hour - sunrise) / (sunset - sunrise))


def load_at(params, minute_of_day):
    """The configured load at minute_of_day: a constant kW, or one of 24 hourly kW values."""
    load = params["load"]
    if isinstance(load, (list, tuple)) and load:
        return float(load[(minute_of_day // 60) % len(load)])
    return float(load)


class DummyInverter(ComponentBase):
    """A simulated hybrid inverter and battery that Predbat plans and controls like a real one."""

    def initialize(self, config=None, automatic=True, **kwargs):
        """Read the dummy_inverter apps.yaml block over the defaults and set up the simulation state."""
        self.params = dict(DUMMY_DEFAULTS)
        if isinstance(config, dict):
            for key in DUMMY_DEFAULTS:
                if key in config and config[key] is not None:
                    self.params[key] = config[key] if key == "load" else float(config[key])
        self.automatic = automatic
        self.soc_kwh = self.params["battery_size"] * self.params["soc_initial"] / 100.0
        self.totals = {"pv": 0.0, "load": 0.0, "import": 0.0, "export": 0.0, "clipped": 0.0}
        self.flows = {"battery": 0.0, "grid": 0.0, "pv": 0.0, "load": 0.0, "clipped": 0.0}
        self.last_minute = None
        rate_w = self.params["battery_rate_max"]
        self.controls = {
            "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "enable": False, "target_soc": 100, "rate": rate_w},
            "export": {"start_time": "00:00:00", "end_time": "00:00:00", "enable": False, "target_soc": 0, "rate": rate_w},
            "reserve": self.params["reserve"],
        }

    def entity(self, domain, name):
        """Return the entity ID for one of the dummy inverter's entities."""
        return "{}.{}_dummy_{}".format(domain, self.prefix, name)

    def pv_now(self, minute_of_day):
        """PV in kW: Predbat's forecast for this minute when it has one, otherwise the clear-sky curve."""
        forecast = getattr(self.base, "pv_forecast_minute", None)
        if forecast and minute_of_day in forecast:
            # The forecast holds kWh per minute
            return max(float(forecast[minute_of_day]) * 60.0, 0.0) * self.params["pv_scaling"]
        return clear_sky_pv(self.params, minute_of_day) * self.params["pv_scaling"]

    def step(self, minutes_absolute):
        """Simulate from the last simulated minute up to minutes_absolute (minutes since an arbitrary epoch)."""
        if self.last_minute is None:
            self.last_minute = minutes_absolute
            return
        minutes = min(max(minutes_absolute - self.last_minute, 0), MAX_CATCH_UP_MINUTES)
        for offset in range(minutes):
            minute_of_day = (minutes_absolute - minutes + offset) % (24 * 60)
            pv_kw = self.pv_now(minute_of_day)
            load_kw = load_at(self.params, minute_of_day)
            self.soc_kwh, self.flows = simulate_minute(self.params, self.controls, self.soc_kwh, pv_kw, load_kw, minute_of_day)
            self.totals["pv"] += self.flows["pv"] / 60.0
            self.totals["load"] += self.flows["load"] / 60.0
            self.totals["clipped"] += self.flows["clipped"] / 60.0
            if self.flows["grid"] > 0:
                self.totals["import"] += self.flows["grid"] / 60.0
            else:
                self.totals["export"] += -self.flows["grid"] / 60.0
        self.last_minute = minutes_absolute

    def publish(self):
        """Publish the simulated sensors."""
        soc_max = self.params["battery_size"]
        sensors = [
            ("battery_soc", round(self.soc_kwh, 3), "kWh", "Battery SoC"),
            ("battery_capacity", soc_max, "kWh", "Battery capacity"),
            ("battery_soc_percent", round(self.soc_kwh / soc_max * 100.0, 1) if soc_max else 0, "%", "Battery SoC %"),
            ("battery_power", round(self.flows["battery"] * 1000.0), "W", "Battery power"),
            ("pv_power", round(self.flows["pv"] * 1000.0), "W", "PV power"),
            ("load_power", round(self.flows["load"] * 1000.0), "W", "Load power"),
            ("grid_power", round(self.flows["grid"] * 1000.0), "W", "Grid power"),
            ("clipped_power", round(self.flows["clipped"] * 1000.0), "W", "Clipped PV power"),
            ("battery_rate_max", self.params["battery_rate_max"], "W", "Battery rate max"),
            ("inverter_limit", self.params["inverter_limit"], "W", "Inverter limit"),
            ("export_limit", self.params["export_limit"], "W", "Export limit"),
            ("pv_lifetime", round(self.totals["pv"], 3), "kWh", "PV total"),
            ("load_lifetime", round(self.totals["load"], 3), "kWh", "Load total"),
            ("grid_import_lifetime", round(self.totals["import"], 3), "kWh", "Grid import total"),
            ("grid_export_lifetime", round(self.totals["export"], 3), "kWh", "Grid export total"),
            ("clipped_lifetime", round(self.totals["clipped"], 3), "kWh", "Clipped PV total"),
        ]
        for name, state, unit, friendly in sensors:
            attributes = {"friendly_name": "Dummy inverter {}".format(friendly), "unit_of_measurement": unit}
            if unit == "kWh" and name.endswith("_lifetime"):
                attributes["state_class"] = "total_increasing"
            self.dashboard_item(self.entity("sensor", name), state=state, attributes=attributes, app="dummy_inverter")
        self.dashboard_item(self.entity("sensor", "time"), state=self.now_utc.strftime("%Y-%m-%d %H:%M:%S"), attributes={"friendly_name": "Dummy inverter time"}, app="dummy_inverter")

    def control_info(self, direction, field):
        """Return (entity_id, attributes, state) for one control, as Predbat reads and writes it."""
        values = self.controls[direction] if direction else self.controls
        value = values[field]
        name = "{}_{}".format(direction, field) if direction else field
        friendly = "Dummy inverter {}".format(name.replace("_", " "))
        if field.endswith("_time"):
            return self.entity("select", name), {"friendly_name": friendly, "options": DUMMY_OPTIONS_TIME}, value
        if field == "enable":
            return self.entity("switch", name), {"friendly_name": friendly}, "on" if value else "off"
        if field == "rate":
            return self.entity("number", name), {"friendly_name": friendly, "unit_of_measurement": "W", "min": 0, "max": self.params["battery_rate_max"], "step": 1}, value
        return self.entity("number", name), {"friendly_name": friendly, "unit_of_measurement": "%", "min": 0, "max": 100, "step": 1}, value

    def control_fields(self):
        """Every (direction, field) pair the dummy inverter exposes as a control."""
        fields = [(direction, field) for direction in ("charge", "export") for field in ("start_time", "end_time", "enable", "target_soc", "rate")]
        return fields + [(None, "reserve")]

    def publish_controls(self):
        """Publish the control entities with their current values."""
        for direction, field in self.control_fields():
            entity_id, attributes, state = self.control_info(direction, field)
            self.dashboard_item(entity_id, state=state, attributes=attributes, app="dummy_inverter")

    def find_control(self, entity_id):
        """Return the (direction, field) a control entity ID belongs to, or (None, None) when it is not one."""
        for direction, field in self.control_fields():
            if self.control_info(direction, field)[0] == entity_id:
                return direction, field
        return None, None

    def update_control(self, entity_id, value):
        """Apply a write from Predbat to one control and re-publish it. Returns True when it was a dummy control."""
        direction, field = self.find_control(entity_id)
        if field is None:
            return False
        target = self.controls[direction] if direction else self.controls
        if field == "enable":
            current = target[field]
            target[field] = {"turn_on": True, "turn_off": False, "toggle": not current}.get(value, value in (True, "on"))
        elif field.endswith("_time"):
            if time_to_minute(value) is None:
                self.log("Warn: DummyInverter: ignoring unreadable time {} for {}".format(value, entity_id))
                return True
            target[field] = str(value)
        else:
            try:
                target[field] = float(value)
            except (TypeError, ValueError):
                self.log("Warn: DummyInverter: ignoring unreadable value {} for {}".format(value, entity_id))
                return True
        entity, attributes, state = self.control_info(direction, field)
        self.dashboard_item(entity, state=state, attributes=attributes, app="dummy_inverter")
        return True

    async def select_event(self, entity_id, value):
        """Handle a select write from Predbat."""
        self.update_control(entity_id, value)

    async def number_event(self, entity_id, value):
        """Handle a number write from Predbat."""
        self.update_control(entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle a switch service call from Predbat."""
        self.update_control(entity_id, service)

    def automatic_config(self):
        """Point Predbat's inverter settings at the dummy inverter's entities."""
        self.set_arg("num_inverters", 1)
        self.set_arg("inverter_type", ["DUMMY"])
        for arg, name in (
            ("soc_kw", "battery_soc"),
            ("soc_max", "battery_capacity"),
            ("battery_power", "battery_power"),
            ("battery_rate_max", "battery_rate_max"),
            ("inverter_limit", "inverter_limit"),
            ("export_limit", "export_limit"),
            ("pv_power", "pv_power"),
            ("grid_power", "grid_power"),
            ("load_power", "load_power"),
            ("pv_today", "pv_lifetime"),
            ("load_today", "load_lifetime"),
            ("import_today", "grid_import_lifetime"),
            ("export_today", "grid_export_lifetime"),
            ("inverter_time", "time"),
        ):
            self.set_arg(arg, [self.entity("sensor", name)])
        for arg, direction, field in (
            ("charge_start_time", "charge", "start_time"),
            ("charge_end_time", "charge", "end_time"),
            ("charge_limit", "charge", "target_soc"),
            ("scheduled_charge_enable", "charge", "enable"),
            ("charge_rate", "charge", "rate"),
            ("discharge_start_time", "export", "start_time"),
            ("discharge_end_time", "export", "end_time"),
            ("discharge_target_soc", "export", "target_soc"),
            ("scheduled_discharge_enable", "export", "enable"),
            ("discharge_rate", "export", "rate"),
            ("reserve", None, "reserve"),
        ):
            self.set_arg(arg, [self.control_info(direction, field)[0]])
        self.log("DummyInverter: automatic_config complete")

    def minutes_absolute(self):
        """Whole minutes since the epoch for the current time, so the simulation can step across midnight."""
        return int(self.now_utc.timestamp() // 60)

    async def run(self, seconds, first):
        """Advance the simulation to now and publish; on the first run also publish controls and wire Predbat up."""
        if first:
            self.publish_controls()
            if self.automatic:
                self.automatic_config()
        self.step(self.minutes_absolute())
        self.publish()
        self.update_success_timestamp()
        return True
