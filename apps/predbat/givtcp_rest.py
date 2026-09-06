# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""GivTCP REST client used by Inverter for GivEnergy inverters.

Wraps the GivTCP add-on's local REST API: reading full inverter status and
writing charge/discharge rates, targets, modes, reserve, and time slots, each
with retry-until-verified semantics. Holds no state of its own beyond the
optional test-injection overrides - the live snapshot (`rest_data`) and the
configured base URL (`rest_api`) live on the owning Inverter, since other
Inverter code paths (still, pending a later phase of this refactor) read and
fall back on them directly alongside the HA-entity path.

The read-only properties/methods (charge_enable_time, soc_kwh, power_readings,
charge_window_times, ...) normalise that raw snapshot into plain values,
absorbing GivTCP-version differences (rest_v3) so callers don't have to walk
its JSON shape by hand. Each returns None when the relevant data hasn't been
read yet, mirroring the "REST configured but no data this cycle" case callers
already have to handle for the write path.
"""

import time

import requests

from const import INVERTER_MAX_RETRY_REST, INVERTER_REST_TIMEOUT
from utils import dp2, dp3, dp4, time_string_to_stamp


class InverterRestState:
    """
    Minimal stand-in for the subset of Inverter that GivTCPRest depends on: id, rest_api (the
    configured URL), rest_data (mutable last-read snapshot), a register-write counter, and a
    blocking sleep(). The GivTCP version is not among them - GivTCPRest decodes that from
    rest_data itself (see its rest_v3).

    Lets a caller that isn't a real Inverter (e.g. GivTCPComponent, which has no Inverter object
    to hand GivTCPRest) construct one of these instead, so GivTCPRest itself stays unchanged.

    battery_scaling stays 1.0 here and must not be plumbed through from the user's config: Inverter
    applies battery_scaling itself when it reads soc_kw (inverter.py), so passing the real value
    would apply it twice. What this object exposes is the unscaled reading.
    """

    def __init__(self, id, rest_api, battery_scaling=1.0):
        self.id = id
        self.rest_api = rest_api
        self.rest_data = None
        self.battery_scaling = battery_scaling
        self.count_register_writes = 0

    def sleep(self, seconds):
        time.sleep(seconds)


class GivTCPRest:
    """
    GivTCP REST client for a single inverter.

    Args:
        base: The main Predbat base object providing logging and status reporting
        inverter: The owning Inverter instance - source of truth for rest_api (base URL) and
            rest_data (last-read status snapshot), and for id/sleep/battery rate limits used
            while verifying writes
        rest_postCommand: Optional override for the low-level POST call (used by tests)
        rest_getData: Optional override for the low-level GET call (used by tests)
    """

    def __init__(self, base, inverter, rest_postCommand=None, rest_getData=None):
        self.base = base
        self.inverter = inverter
        if rest_postCommand:
            self.post_command = rest_postCommand
        if rest_getData:
            self.get_data = rest_getData

    @property
    def givtcp_version(self):
        """The GivTCP version string from the last status read, or "Unknown"."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return "Unknown"
        return rest_data.get("Stats", {}).get("GivTCP_Version", "Unknown")

    @property
    def rest_v3(self):
        """
        Whether GivTCP is version 3, decoded from the status snapshot rather than set from outside.

        This is a fact about the data already in hand, so deriving it here keeps the client
        self-contained: nothing has to remember to stamp the flag on before a snapshot is parsed,
        and a freshly re-probed endpoint cannot be decoded against the version of a previous one.
        """
        return self.givtcp_version.startswith("3")

    @property
    def firmware_version(self):
        """The inverter's firmware version from the last status read, or "Unknown"."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return "Unknown"
        return rest_data.get("raw", {}).get("invertor", {}).get("firmware_version", "Unknown")

    @property
    def serial_number(self):
        """The inverter's serial number from the last status read, or "Unknown"."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return "Unknown"
        return rest_data.get("raw", {}).get("invertor", {}).get("serial_number", "Unknown")

    @property
    def charge_enable_time(self):
        """Whether GivTCP's scheduled charge is enabled, or None if no status has been read yet."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        # None distinguishes "not reported" from "reported off": read_data only checks that a
        # top-level Control block exists, not its contents. Publishing an unknown as "off" would
        # tell Predbat the schedule is disabled and have it write to enable it.
        value = rest_data.get("Control", {}).get("Enable_Charge_Schedule", None)
        if value is None:
            return None
        return value == "enable"

    @property
    def charge_target_enabled(self):
        """
        Whether the inverter will act on Target_SOC, or None if GivTCP does not report the register.

        GivTCP's setChargeTarget writes CHARGE_TARGET_SOC (reg 116) but never enables
        ENABLE_CHARGE_TARGET (reg 20). With reg 20 off - GivTCP's default - the inverter ignores the
        SOC limit and charges to 100%, which was the root cause of Hold Charge not holding on AIO
        inverters (#4141). None distinguishes "reported as off" from "not reported at all", so a
        GivTCP without the field gets no control published rather than one whose write can never
        verify.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        value = rest_data.get("Control", {}).get("Enable_Charge_Target", None)
        if value is None:
            return None
        if isinstance(value, str):
            return value.lower() in ("enable", "on", "true")
        return bool(value)

    @property
    def discharge_enable_time(self):
        """Whether GivTCP's scheduled discharge is enabled, or None if no status has been read yet."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        # None distinguishes "not reported" from "reported off": read_data only checks that a
        # top-level Control block exists, not its contents. Publishing an unknown as "off" would
        # tell Predbat the schedule is disabled and have it write to enable it.
        value = rest_data.get("Control", {}).get("Enable_Discharge_Schedule", None)
        if value is None:
            return None
        return value == "enable"

    @property
    def pause_mode_supported(self):
        """
        Whether this inverter reports the battery-pause mode register, or None with no status yet.

        rest_v3 only says GivTCP itself is new enough to offer /setBatteryPauseMode - it says
        nothing about the inverter behind it, and a model with no pause support simply has no
        Control.Battery_pause_mode in its snapshot. Gating on the version alone published a
        "Disabled" fallback for a register that does not exist, which left
        Inverter.inv_has_timed_pause switched on against a control that can never be written.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        return rest_data.get("Control", {}).get("Battery_pause_mode", None) is not None

    @property
    def pause_slots_supported(self):
        """
        Whether this inverter reports the battery-pause time window, or None with no status yet.

        Separate from pause_mode_supported: an inverter can support pausing without supporting a
        scheduled window, and Inverter.adjust_pause_mode already copes with that by writing the
        mode alone. Both ends are required - _window_for_write needs the other end of the window
        to program either one, so a half-reported pair is no more usable than none at all.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        timeslots = rest_data.get("Timeslots", {})
        return timeslots.get("Battery_pause_start_time_slot", None) is not None and timeslots.get("Battery_pause_end_time_slot", None) is not None

    def energy_reading(self, period, name):
        """
        One of GivTCP's Energy counters in kWh, or None if it is not reported.

        period is "Today" for the day's accumulating totals - which Predbat reads the HISTORY of to
        build its load model, not just the current value - or "Total" for the lifetime counters.
        Both live in the same snapshot on both v2 and v3, so neither costs an extra read.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        value = rest_data.get("Energy", {}).get(period, {}).get(name, None)
        if value is None:
            return None
        try:
            return dp3(float(value))
        except (ValueError, TypeError):
            return None

    @property
    def soc_kwh(self):
        """Current battery SoC in kWh, or None if GivTCP hasn't reported it this cycle."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        value = rest_data.get("Power", {}).get("Power", {}).get("SOC_kWh", None)
        if value is None:
            return None
        return dp3(value * self.inverter.battery_scaling)

    @property
    def target_soc(self):
        """Currently applied charge target percent, or None if no status has been read yet."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        value = rest_data.get("Control", {}).get("Target_SOC", None)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def power_readings(self):
        """Battery/PV/grid/load power and battery voltage from GivTCP's Power block, or None if
        GivTCP hasn't reported a full Power block this cycle."""
        rest_data = self.inverter.rest_data
        if not rest_data or "Power" not in rest_data or "Power" not in rest_data["Power"]:
            return None
        ppdetails = rest_data["Power"]["Power"]
        # None when GivTCP does not report it - v2's Power block has no Battery_Voltage at all.
        # main filled that gap with get_arg("battery_voltage"), which read the user's own sensor.
        # Here that key is auto-configured to the sensor this value feeds, so reading it back would
        # close a loop: from the second poll onwards the component would republish its own last
        # value and the reading would freeze. The caller publishes nothing instead, leaving the
        # user's apps.yaml voltage sensor in place.
        battery_voltage = ppdetails.get("Battery_Voltage", None)
        battery_voltage = float(battery_voltage) if battery_voltage is not None else None
        return {
            "battery_power": float(ppdetails.get("Battery_Power", 0.0)),
            "pv_power": float(ppdetails.get("PV_Power", 0.0)),
            "grid_power": float(ppdetails.get("Grid_Power", 0.0)),
            "load_power": float(ppdetails.get("Load_Power", 0.0)),
            "battery_voltage": battery_voltage,
        }

    def inverter_details(self):
        """
        The inverter detail block, normalised across GivTCP versions.

        v2 puts it under "Invertor_Details"; v3 renames it to the inverter's own serial number, so
        an empty "Invertor_Details" on v3 is expected rather than a fault. Returns {} when neither
        is present.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return {}
        details = rest_data.get("Invertor_Details", {})
        if details:
            return details
        serial = rest_data.get("raw", {}).get("invertor", {}).get("serial_number", None)
        if serial and serial in rest_data:
            return rest_data[serial]
        return {}

    def battery_capacity_kwh(self):
        """Battery capacity in kWh as GivTCP reports it, or None if absent."""
        value = self.inverter_details().get("Battery_Capacity_kWh", None)
        return float(value) if value is not None else None

    def nominal_capacity(self):
        """
        Nominal (nameplate) battery capacity in kWh, or None if GivTCP does not report it.

        v2 reports this in Ah and needs scaling; v3 already reports kWh. 19.53125 is 1000/51.2, so
        the divisor converts Ah to kWh at the 51.2V nominal these GE packs use - main carried it as
        a back-calculated constant with an XXX asking where it came from. The v2 capture confirms
        it: 186 Ah x 51.2 V / 1000 == 9.5232 kWh == the reported Battery_Capacity_kWh. It assumes a
        51.2V pack, which is why it is not a general unit conversion.

        This is the design capacity, the same figure Battery_Capacity_kWh reports in kWh - not the
        battery's current health-adjusted capacity. See battery_soh() for that.
        """
        raw_value = self.inverter.rest_data.get("raw", {}).get("invertor", {}).get("battery_nominal_capacity", None) if self.inverter.rest_data else None
        if not raw_value:
            return None
        if self.rest_v3:
            return float(raw_value)
        return float(raw_value) / 19.53125

    def battery_temperature(self):
        """
        Mean BMS temperature across the battery packs, or None if no pack reports one.

        Packs report the field under different names depending on model/firmware, and some nest a
        further dict per pack, so all three shapes are averaged together the way Inverter.__init__
        did.
        """
        rest_data = self.inverter.rest_data
        if not rest_data or "Battery_Details" not in rest_data:
            return None
        total = 0.0
        count = 0
        for battery in rest_data["Battery_Details"]:
            details = rest_data["Battery_Details"][battery]
            if "BMS_Temperature" in details:
                total += float(details["BMS_Temperature"])
                count += 1
            elif "Battery_Temperature" in details:
                total += float(details["Battery_Temperature"])
                count += 1
            else:
                for item in details.values():
                    if isinstance(item, dict) and "Battery_Temperature" in item:
                        total += float(item["Battery_Temperature"])
                        count += 1
        if not count:
            return None
        return dp2(total / count)

    def inverter_time(self):
        """The inverter's own clock as GivTCP reports it, or None if absent."""
        return self.inverter_details().get("Invertor_Time", None)

    def max_battery_rate(self):
        """Maximum battery charge/discharge rate in W, or None if GivTCP does not report one."""
        details = self.inverter_details()
        for key in ("Invertor_Max_Bat_Rate", "Invertor_Max_Rate"):
            if key in details:
                return float(details[key])
        return None

    def write_tolerance_watts(self):
        """
        Reference rate in W for sizing rate write verification.

        Falls back to Inverter's own 2600W default when GivTCP reports no maximum, so an
        undiscovered rate behaves as it did before rather than accepting any write within 5kW.
        """
        return self.max_battery_rate() or 2600.0

    def battery_soh(self):
        """
        Battery state of health as full capacity / design capacity, or None if not reported.

        GivTCP reports both per battery module under Battery_Details, in Ah: flat on v2
        (Battery_Details/<serial>), nested one level under Battery_Stack_N on v3. Summed across
        modules and clamped at 1.0, matching GE Cloud's equivalent - a pack reporting above its
        nameplate is a BMS quirk, not spare capacity Predbat should plan to use.

        Deliberately NOT Battery_Capacity_kWh vs raw.invertor.battery_nominal_capacity: those are
        the same design figure in different units (Ah / 19.53125 == kWh), so their ratio is always
        1.0 and would silently pin scaling at 100%.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None

        full = 0.0
        design = 0.0
        stack = [rest_data.get("Battery_Details", {})]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if "Battery_Capacity" in node and "Battery_Design_Capacity" in node:
                try:
                    module_full = float(node["Battery_Capacity"])
                    module_design = float(node["Battery_Design_Capacity"])
                except (ValueError, TypeError):
                    continue
                if module_full > 0 and module_design > 0:
                    full += module_full
                    design += module_design
                continue
            stack.extend(node.values())

        if full <= 0 or design <= 0:
            return None
        return min(dp4(full / design), 1.0)

    def inverter_type(self):
        """
        GivTCP's readable inverter type (e.g. "Gen 1 Hybrid", "Gen2 Hybrid"), or "" if absent.

        Lives in the detail block on both versions, so inverter_details() normalises it. This is the
        only model identification v3 gives in words - raw.invertor.model is a numeric code there.
        """
        return str(self.inverter_details().get("Invertor_Type", "") or "")

    def max_inverter_rate(self):
        """Maximum inverter throughput in W, or None if GivTCP does not report one."""
        value = self.inverter_details().get("Invertor_Max_Inv_Rate", None)
        return float(value) if value is not None else None

    def in_calibration(self):
        """
        Whether the battery is currently being calibrated, during which Predbat cannot function.

        v3 exposes this directly as Control.Battery_Calibration; older GivTCP only has the raw
        soc_force_adjust register, where values 1-6 mean a calibration is in progress.
        """
        rest_data = self.inverter.rest_data
        if not rest_data:
            return False
        if self.rest_v3:
            return rest_data.get("Control", {}).get("Battery_Calibration", "Off") != "Off"
        soc_force_adjust = rest_data.get("raw", {}).get("invertor", {}).get("soc_force_adjust", None)
        if not soc_force_adjust:
            return False
        try:
            soc_force_adjust = int(soc_force_adjust)
        except (ValueError, TypeError):
            return False
        return 0 < soc_force_adjust < 7

    def charge_window_times(self):
        """Current charge window as (start, end) parsed timestamps, or None if no status has been
        read yet."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        start = time_string_to_stamp(rest_data["Timeslots"]["Charge_start_time_slot_1"])
        end = time_string_to_stamp(rest_data["Timeslots"]["Charge_end_time_slot_1"])
        return start, end

    def discharge_window_times(self):
        """Current discharge window as (start, end) parsed timestamps, or None if no status has
        been read yet."""
        rest_data = self.inverter.rest_data
        if not rest_data:
            return None
        start = time_string_to_stamp(rest_data["Timeslots"]["Discharge_start_time_slot_1"])
        end = time_string_to_stamp(rest_data["Timeslots"]["Discharge_end_time_slot_1"])
        return start, end

    def read_data(self, api="readData", retry=True):
        """
        Get inverter status

        :param api: The API endpoint to retrieve data from (default is "readData")
        :retry: if the REST GET fails then should the GET be retried? (default is True)
        :return: The JSON response containing the inverter status, or None if there was an error
        """
        inverter = self.inverter
        url = inverter.rest_api + "/" + api

        # repeatedly try to get inverter data via REST, sleeping after each failed attempt to enable GivTCP to re-get the data
        for loop in range(INVERTER_MAX_RETRY_REST):
            json = self.get_data(url)

            if json:
                if "Control" in json:
                    if loop == 0:
                        self.base.log("GivTCP: Inverter {} REST GET {} successful".format(inverter.id, url))
                    else:
                        self.base.log("Info: GivTCP: Inverter {} REST GET {} successful on retry {}".format(inverter.id, url, loop))
                    return json

            # if retry = False then don't retry further GET calls
            if not retry:
                break

            # firstly retry after a short delay to allow the REST endpoint to get the data, then try longer delays
            if loop == 0:
                delay = 20
            else:
                delay = 40

            self.base.log('Warn: GivTCP: inverter {} didn\'t receive JSON response from REST GET {}, received "{}". Waiting {}s then retrying'.format(inverter.id, url, json, delay))
            inverter.sleep(delay)

        # Exhausted retry attempts, fail REST GET and fallback to using HA entities (if they have been configured in apps.yaml)
        self.base.log("Warn: GivTCP: Inverter {} unable to read REST data from {} - REST will be skipped for this run".format(inverter.id, url))
        self.base.record_status("Warn: Inverter {} unable to read REST data from {} - REST will be skipped".format(inverter.id, url), had_errors=True)
        return None

    def run_all(self, old_data=None):
        """
        Updated and get inverter status
        """
        new_data = self.read_data(api="runAll", retry=False)
        if new_data:
            return new_data
        else:
            return old_data

    def post_command(self, url, json):
        """
        Send REST Command
        """
        try:
            r = requests.post(url, json=json, timeout=INVERTER_REST_TIMEOUT)
        except Exception as e:
            self.base.log("Warn: GivTCP: Inverter {} REST POST {} failed: {}".format(self.inverter.id, url, e))
            return None
        return r

    def get_data(self, url):
        """
        Get REST Data
        """
        r = None

        try:
            r = requests.get(url, timeout=INVERTER_REST_TIMEOUT)
        except Exception as e:
            self.base.log("Error: GivTCP: Exception raised {}".format(e))

        if r and (r.status_code == 200):
            return r.json()
        else:
            return None

    def enable_charge_target(self, enable):
        """
        Enable or disable the charge target SOC limit register via REST.
        Without this being enabled, CHARGE_TARGET_SOC (reg 116) is ignored by the inverter.
        No-ops if the register already matches the requested state.
        """
        inverter = self.inverter
        current = inverter.rest_data["Control"].get("Enable_Charge_Target", "disable")
        if isinstance(current, str):
            current = current.lower() in ["enable", "on", "true"]
        if current == enable:
            return True

        url = inverter.rest_api + "/enableChargeTarget"
        data = {"state": "enable" if enable else "disable"}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            new_value = inverter.rest_data["Control"].get("Enable_Charge_Target", "disable")
            if isinstance(new_value, str):
                new_value = new_value.lower() in ["enable", "on", "true"]
            if new_value == enable:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Set inverter {} charge target enable {} via REST successful on retry {}".format(inverter.id, enable, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Set inverter {} charge target enable {} via REST failed".format(inverter.id, enable))
        self.base.record_status("Warn: Inverter {} REST failed to enableChargeTarget".format(inverter.id), had_errors=True)
        return False

    def set_charge_target(self, target):
        """
        Configure charge target % via REST
        """
        inverter = self.inverter
        target = int(target)
        url = inverter.rest_api + "/setChargeTarget"
        data = {"chargeToPercent": target}
        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            if float(inverter.rest_data["Control"]["Target_SOC"]) == target:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} charge target {} via REST successful on retry {}".format(inverter.id, target, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} charge target {} via REST failed".format(inverter.id, target))
        self.base.record_status("Warn: Inverter {} REST failed to setChargeTarget".format(inverter.id), had_errors=True)
        return False

    def set_charge_rate(self, rate):
        """
        Configure charge target % via REST
        """
        inverter = self.inverter
        rate = int(rate)
        url = inverter.rest_api + "/setChargeRate"
        data = {"chargeRate": rate}
        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            new = int(inverter.rest_data["Control"]["Battery_Charge_Rate"])
            # Sized from the rate GivTCP reports, not a stored copy: the old form divided the rate by
            # MINUTE_WATT and multiplied it straight back, and the stored copy is what left this at 5000W.
            if abs(new - rate) < (self.write_tolerance_watts() / 12):
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} set charge rate {} via REST successful on retry {}".format(inverter.id, rate, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} set charge rate {} via REST failed got {}".format(inverter.id, rate, inverter.rest_data["Control"]["Battery_Charge_Rate"]))
        self.base.record_status("Warn: Inverter {} REST failed to setChargeRate".format(inverter.id), had_errors=True)
        return False

    def set_discharge_rate(self, rate):
        """
        Configure charge target % via REST
        """
        inverter = self.inverter
        rate = int(rate)
        url = inverter.rest_api + "/setDischargeRate"
        data = {"dischargeRate": rate}
        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            new = int(inverter.rest_data["Control"]["Battery_Discharge_Rate"])
            if abs(new - rate) < (self.write_tolerance_watts() / 25):
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} set discharge rate {} via REST successful on retry {}".format(inverter.id, rate, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} set discharge rate {} via REST failed got {}".format(inverter.id, rate, inverter.rest_data["Control"]["Battery_Discharge_Rate"]))
        self.base.record_status("Warn: Inverter {} REST failed to setDischargeRate to {} got {}".format(inverter.id, rate, inverter.rest_data["Control"]["Battery_Discharge_Rate"]), had_errors=True)
        return False

    def set_battery_mode(self, inverter_mode):
        """
        Configure invert mode via REST
        """
        inverter = self.inverter
        url = inverter.rest_api + "/setBatteryMode"
        data = {"mode": inverter_mode}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            if inverter_mode == inverter.rest_data["Control"]["Mode"]:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Set inverter {} mode {} via REST successful on retry {}".format(inverter.id, inverter_mode, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Set inverter {} mode {} via REST failed".format(inverter.id, inverter_mode))
        self.base.record_status("Warn: Inverter {} REST failed to setBatteryMode".format(inverter.id), had_errors=True)
        return False

    def set_battery_pause_mode(self, pause_mode):
        """
        Configure inverter pause mode via REST - v3.x+
        """
        inverter = self.inverter
        url = inverter.rest_api + "/setBatteryPauseMode"
        data = {"state": pause_mode}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            if pause_mode == inverter.rest_data["Control"]["Battery_pause_mode"]:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Set inverter {} pause mode {} via REST successful on retry {}".format(inverter.id, pause_mode, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Set inverter {} pause mode {} via REST failed".format(inverter.id, pause_mode))
        self.base.record_status("Warn: Inverter {} REST failed to setBatteryPauseMode got {}".format(inverter.id, inverter.rest_data["Control"]["Battery_pause_mode"]), had_errors=True)
        return False

    def set_reserve(self, target):
        """
        Configure reserve % via REST
        """
        inverter = self.inverter
        target = int(target)
        result = target
        url = inverter.rest_api + "/setBatteryReserve"
        data = {"reservePercent": target}
        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            result = int(float(inverter.rest_data["Control"]["Battery_Power_Reserve"]))
            if result == target:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Set inverter {} reserve {} via REST successful on retry {}".format(inverter.id, target, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Set inverter {} reserve {} via REST failed on retry {} got {}".format(inverter.id, target, retry, result))
        self.base.record_status("Warn: Inverter {} REST failed to setReserve to {} got {}".format(inverter.id, target, result), had_errors=True)
        return False

    def enable_charge_schedule(self, enable):
        """
        Configure enable charge schedule via REST
        """
        inverter = self.inverter
        url = inverter.rest_api + "/enableChargeSchedule"
        data = {"state": "enable" if enable else "disable"}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            new_value = inverter.rest_data["Control"]["Enable_Charge_Schedule"]
            if isinstance(new_value, str):
                if new_value.lower() in ["enable", "on", "true"]:
                    new_value = True
                else:
                    new_value = False
            if new_value == enable:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Set inverter {} charge schedule {} via REST successful on retry {}".format(inverter.id, enable, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Set inverter {} charge schedule {} via REST failed got {}".format(inverter.id, enable, inverter.rest_data["Control"]["Enable_Charge_Schedule"]))
        self.base.record_status("Warn: Inverter {} REST failed to enableChargeSchedule".format(inverter.id), had_errors=True)
        return False

    def enable_discharge_schedule(self, enable):
        """
        Configure enable discharge schedule via REST (V3.x+)
        """
        inverter = self.inverter
        url = inverter.rest_api + "/enableDischargeSchedule"
        data = {"state": "enable" if enable else "disable"}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            new_value = inverter.rest_data["Control"]["Enable_Discharge_Schedule"]
            if isinstance(new_value, str):
                if new_value.lower() in ["enable", "on", "true"]:
                    new_value = True
                else:
                    new_value = False
            if new_value == enable:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Set inverter {} discharge schedule {} via REST successful on retry {}".format(inverter.id, enable, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Set inverter {} discharge schedule {} via REST failed got {}".format(inverter.id, enable, inverter.rest_data["Control"]["Enable_Discharge_Schedule"]))
        self.base.record_status("Warn: Inverter {} REST failed to enableDischargeSchedule".format(inverter.id), had_errors=True)
        return False

    def set_pause_slot(self, start, finish):
        """
        Configure pause slot via REST - v3.x+
        """
        inverter = self.inverter
        url = inverter.rest_api + "/setPauseSlot"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            if inverter.rest_data["Timeslots"]["Battery_pause_start_time_slot"] == start and inverter.rest_data["Timeslots"]["Battery_pause_end_time_slot"] == finish:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} set pause slot {} via REST successful after retry {}".format(inverter.id, data, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} set pause slot {} via REST failed".format(inverter.id, data))
        self.base.record_status("Warn: Inverter {} REST failed to setPauseSlot".format(inverter.id), had_errors=True)
        return False

    def set_charge_slot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        inverter = self.inverter
        url = inverter.rest_api + "/setChargeSlot1"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            if inverter.rest_data["Timeslots"]["Charge_start_time_slot_1"] == start and inverter.rest_data["Timeslots"]["Charge_end_time_slot_1"] == finish:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} set charge slot 1 {} via REST successful after retry {}".format(inverter.id, data, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} set charge slot 1 {} via REST failed".format(inverter.id, data))
        self.base.record_status("Warn: Inverter {} REST failed to setChargeSlot1".format(inverter.id), had_errors=True)
        return False

    def read_discharge_target(self):
        """
        Read GivTCP's currently applied discharge target percent, or None if it can't be read.

        Mirrors set_discharge_target()'s own preference order: Control.Discharge_Target_SOC_1 is
        GivTCP's synchronous write-time signal, updated the moment a write is accepted, so it's
        checked first. raw.invertor.discharge_target_soc_1 is a fallback for GivTCP setups where
        Control doesn't expose the key - but on its own it's unreliable as a "did this actually
        change" signal, since it only refreshes on GivTCP's separate background self_run poll cycle,
        not synchronously with any write. A caller that reads raw.invertor alone to decide whether a
        write is even needed can end up re-writing every cycle on hardware where that field never
        catches up (#4421, #4517).
        """
        rest_data = self.inverter.rest_data
        if not isinstance(rest_data, dict):
            return None
        try:
            result = int(float(rest_data.get("Control", {}).get("Discharge_Target_SOC_1", None)))
        except (ValueError, TypeError):
            result = None
        if result is None:
            try:
                result = int(float(rest_data.get("raw", {}).get("invertor", {}).get("discharge_target_soc_1", None)))
            except (ValueError, TypeError):
                result = None
        return result

    def set_discharge_target(self, target):
        """
        Configure discharge to percent via REST
        """

        def to_int(value):
            """GivTCP reports these as strings, so coerce before comparing or a successful write
            reads back as '4' and never matches the int target."""
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return None

        inverter = self.inverter
        target = int(target)
        url = inverter.rest_api + "/setDischargeTarget"
        data = {"dischargeToPercent": target, "slot": 1}
        result = None

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            # GivTCP's write handler updates Control.Discharge_Target_SOC_1 synchronously the
            # moment it accepts the command (confirmed against GivTCP's own source - write.py's
            # setDischargeTarget() calls updateControlCache() straight after the Modbus write), so
            # it's checked first. raw.invertor.discharge_target_soc_1 is kept as a fallback, but on
            # its own it's an unreliable signal: it only refreshes on GivTCP's separate background
            # self_run poll cycle, which can be tens of seconds away, not synchronous with this
            # POST at all (#4421). A short settle delay still helps for the much smaller residual
            # gap - the physical inverter itself taking a moment to apply the write, which a
            # same-moment self_run poll could otherwise briefly overwrite Control with a stale read
            # of.
            inverter.sleep(1)
            inverter.rest_data = self.run_all(inverter.rest_data)
            result = to_int(inverter.rest_data.get("Control", {}).get("Discharge_Target_SOC_1", None))
            if result != target:
                result = to_int(inverter.rest_data.get("raw", {}).get("invertor", {}).get("discharge_target_soc_1", None))
            if result == target:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} Set export target slot 1 {} via REST successful after retry {}".format(inverter.id, data, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} Set export target slot 1 {} via REST failed got {}".format(inverter.id, data, result))
        self.base.record_status("Warn: Inverter {} REST failed to setExportTarget got {}".format(inverter.id, result), had_errors=True)
        return False

    def set_discharge_slot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        inverter = self.inverter
        url = inverter.rest_api + "/setDischargeSlot1"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(INVERTER_MAX_RETRY_REST):
            r = self.post_command(url, json=data)
            inverter.rest_data = self.run_all(inverter.rest_data)
            if inverter.rest_data["Timeslots"]["Discharge_start_time_slot_1"] == start and inverter.rest_data["Timeslots"]["Discharge_end_time_slot_1"] == finish:
                inverter.count_register_writes += 1
                self.base.log("GivTCP: Inverter {} Set discharge slot 1 {} via REST successful after retry {}".format(inverter.id, data, retry))
                return True
            inverter.sleep(2)

        self.base.log("Warn: GivTCP: Inverter {} Set discharge slot 1 {} via REST failed".format(inverter.id, data))
        self.base.record_status("Warn: Inverter {} REST failed to setDischargeSlot1".format(inverter.id), had_errors=True)
        return False
