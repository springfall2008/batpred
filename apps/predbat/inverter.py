# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
import os
import re
import time
import math
import pytz
import requests
from datetime import datetime, timedelta
from config import INVERTER_DEF, MINUTE_WATT, TIME_FORMAT, TIME_FORMAT_OCTOPUS, INVERTER_TEST, SIMULATE, SOLAX_SOLIS_MODES_NEW, TIME_FORMAT_SECONDS, SOLAX_SOLIS_MODES
from utils import calc_percent_limit


class Inverter:
    def self_test(self, minutes_now):
        self.base.log(f"======= INVERTER CONTROL SELF TEST START - REST={self.rest_api} ========")
        self.adjust_battery_target(99, False)
        self.adjust_battery_target(100, False)
        self.adjust_charge_rate(215)
        self.adjust_charge_rate(self.battery_rate_max_charge)
        self.adjust_discharge_rate(220)
        self.adjust_discharge_rate(self.battery_rate_max_discharge)
        self.adjust_reserve(100)
        self.adjust_reserve(6)
        self.adjust_reserve(4)
        self.adjust_pause_mode(pause_charge=True)
        self.adjust_pause_mode(pause_discharge=True)
        self.adjust_pause_mode(pause_charge=True, pause_discharge=True)
        self.adjust_pause_mode()
        self.disable_charge_window()
        timea = datetime.strptime("23:00:00", "%H:%M:%S")
        timeb = datetime.strptime("23:01:00", "%H:%M:%S")
        timec = datetime.strptime("05:00:00", "%H:%M:%S")
        timed = datetime.strptime("05:01:00", "%H:%M:%S")
        self.adjust_charge_window(timeb, timed, minutes_now)
        self.adjust_charge_window(timea, timec, minutes_now)
        self.adjust_force_discharge(False, timec, timed)
        self.adjust_force_discharge(True, timea, timeb)
        self.adjust_force_discharge(False)
        self.base.log("======= INVERTER CONTROL SELF TEST END ========")

        if self.rest_api:
            self.rest_api = None
            self.rest_data = None
            self.self_test(minutes_now)
        exit

    def auto_restart(self, reason):
        """
        Attempt to restart the services required
        """
        if self.base.restart_active:
            self.base.log("Warn: Inverter control auto restart already active, waiting...")
            return

        # Trigger restart
        self.base.log("Warn: Inverter control auto restart trigger: {}".format(reason))
        restart_command = self.base.get_arg("auto_restart", [])
        if restart_command:
            self.base.restart_active = True
            if isinstance(restart_command, dict):
                restart_command = [restart_command]
            for command in restart_command:
                shell = command.get("shell", None)
                service = command.get("service", None)
                addon = command.get("addon", None)
                if addon:
                    addon = self.base.resolve_arg(service, addon, indirect=False)
                entity_id = command.get("entity_id", None)
                if entity_id:
                    entity_id = self.base.resolve_arg(service, entity_id, indirect=False)
                if shell:
                    self.log("Calling restart shell command: {}".format(shell))
                    os.system(shell)
                if service:
                    if addon:
                        self.log("Calling restart service {} with addon {}".format(service, addon))
                        self.base.call_service_wrapper(service, addon=addon)
                    elif entity_id:
                        self.log("Calling restart service {} with entity_id {}".format(service, entity_id))
                        self.base.call_service_wrapper(service, entity_id=entity_id)
                    else:
                        self.log("Calling restart service {}".format(service))
                        self.base.call_service_wrapper(service)
                    self.base.call_notify("Auto-restart service {} called due to: {}".format(service, reason))
                    time.sleep(15)
            raise Exception("Auto-restart triggered")
        else:
            self.log("Info: auto_restart not defined in apps.yaml, Predbat can't auto-restart inverter control")

    def __init__(self, base, id=0, quiet=False):
        self.id = id
        self.base = base
        self.log = self.base.log
        self.charge_enable_time = False
        self.charge_start_time_minutes = self.base.forecast_minutes
        self.charge_start_end_minutes = self.base.forecast_minutes
        self.charge_window = []
        self.discharge_window = []
        self.discharge_limits = []
        self.current_charge_limit = 0.0
        self.soc_kw = 0
        self.soc_percent = 0
        self.rest_data = None
        self.inverter_limit = 7500.0
        self.export_limit = 99999.0
        self.inverter_time = None
        self.reserve_percent = self.base.get_arg("battery_min_soc", default=4.0, index=self.id)
        self.reserve_percent_current = self.base.get_arg("battery_min_soc", default=4.0, index=self.id)
        self.battery_scaling = self.base.get_arg("battery_scaling", default=1.0, index=self.id)

        self.reserve_max = 100
        self.battery_rate_max_raw = 0
        self.battery_rate_max_charge = 0
        self.battery_rate_max_discharge = 0
        self.battery_rate_max_charge_scaled = 0
        self.battery_rate_max_discharge_scaled = 0
        self.battery_power = 0
        self.pv_power = 0
        self.load_power = 0
        self.rest_api = None
        self.in_calibration = False

        self.inverter_type = self.base.get_arg("inverter_type", "GE", indirect=False, index=self.id)

        # Read user defined inverter type
        if "inverter" in self.base.args:
            if self.inverter_type not in INVERTER_DEF:
                INVERTER_DEF[self.inverter_type] = INVERTER_DEF["GE"].copy()

            inverter_def = self.base.args["inverter"]
            if isinstance(inverter_def, list):
                inverter_def = inverter_def[self.id]

            if isinstance(inverter_def, dict):
                for key in inverter_def:
                    INVERTER_DEF[self.inverter_type][key] = inverter_def[key]
            else:
                self.log("Warn: Inverter {}: inverter definition is not a dictionary".format(self.id))

        if self.inverter_type in INVERTER_DEF:
            self.log(f"Inverter {self.id}: Type {self.inverter_type} {INVERTER_DEF[self.inverter_type]['name']}")
        else:
            raise ValueError("Inverter type {} not defined".format(self.inverter_type))

        if self.inverter_type != "GE":
            self.log("Warn: Inverter {}: Using inverter type {} - not all features are available".format(self.id, self.inverter_type))

        # Load inverter brand definitions
        self.reserve_max = self.base.get_arg("inverter_reserve_max", 100)
        self.inv_has_rest_api = INVERTER_DEF[self.inverter_type]["has_rest_api"]
        self.inv_has_mqtt_api = INVERTER_DEF[self.inverter_type]["has_mqtt_api"]
        self.inv_has_service_api = INVERTER_DEF[self.inverter_type]["has_service_api"]
        self.inv_mqtt_topic = self.base.get_arg("mqtt_topic", "Sofar2mqtt")
        self.inv_output_charge_control = INVERTER_DEF[self.inverter_type]["output_charge_control"]
        self.inv_current_dp = INVERTER_DEF[self.inverter_type].get("current_dp", 1)
        self.inv_has_charge_enable_time = INVERTER_DEF[self.inverter_type]["has_charge_enable_time"]
        self.inv_has_discharge_enable_time = INVERTER_DEF[self.inverter_type]["has_discharge_enable_time"]
        self.inv_has_target_soc = INVERTER_DEF[self.inverter_type]["has_target_soc"]
        self.inv_has_reserve_soc = INVERTER_DEF[self.inverter_type]["has_reserve_soc"]
        self.inv_has_timed_pause = INVERTER_DEF[self.inverter_type]["has_timed_pause"]
        self.inv_charge_time_format = INVERTER_DEF[self.inverter_type]["charge_time_format"]
        self.inv_charge_time_entity_is_option = INVERTER_DEF[self.inverter_type]["charge_time_entity_is_option"]
        self.inv_clock_time_format = INVERTER_DEF[self.inverter_type]["clock_time_format"]
        self.inv_soc_units = INVERTER_DEF[self.inverter_type]["soc_units"]
        self.inv_time_button_press = INVERTER_DEF[self.inverter_type]["time_button_press"]
        self.inv_support_charge_freeze = INVERTER_DEF[self.inverter_type]["support_charge_freeze"]
        self.inv_support_discharge_freeze = INVERTER_DEF[self.inverter_type]["support_discharge_freeze"]
        self.inv_has_ge_inverter_mode = INVERTER_DEF[self.inverter_type]["has_ge_inverter_mode"]
        self.inv_num_load_entities = INVERTER_DEF[self.inverter_type]["num_load_entities"]
        self.inv_write_and_poll_sleep = INVERTER_DEF[self.inverter_type]["write_and_poll_sleep"]
        self.inv_has_idle_time = INVERTER_DEF[self.inverter_type]["has_idle_time"]
        self.inv_can_span_midnight = INVERTER_DEF[self.inverter_type]["can_span_midnight"]

        # If it's not a GE inverter then turn Quiet off
        if self.inverter_type != "GE":
            quiet = False

        # Rest API for GivEnergy
        if self.inverter_type == "GE":
            self.rest_api = self.base.get_arg("givtcp_rest", None, indirect=False, index=self.id)
            if self.rest_api:
                if not quiet:
                    self.base.log("Inverter {} using Rest API {}".format(self.id, self.rest_api))
                self.rest_data = self.rest_readData()
                if not self.rest_data:
                    self.auto_restart("REST read failure")

        # Timed pause support?
        if self.inv_has_timed_pause:
            entity_mode = self.base.get_arg("pause_mode", indirect=False, index=self.id)
            if entity_mode:
                old_pause_mode = self.base.get_state_wrapper(entity_mode)
                if old_pause_mode is None:
                    self.inv_has_timed_pause = False
                    self.log("Inverter {} does not have timed pause support enabled".format(self.id))
                else:
                    self.log("Inverter {} has timed pause support enabled".format(self.id))
            else:
                self.inv_has_timed_pause = False
                self.log("Inverter {} does not have timed pause support enabled".format(self.id))

        # Battery size, charge and discharge rates
        ivtime = None
        if self.rest_data and ("Invertor_Details" in self.rest_data):
            idetails = self.rest_data["Invertor_Details"]
            self.soc_max = float(idetails["Battery_Capacity_kWh"])
            self.nominal_capacity = self.soc_max
            if "raw" in self.rest_data:
                raw_data = self.rest_data["raw"]
                invname = "invertor"
                if invname not in raw_data:
                    invname = "inverter"
                if invname in raw_data and "battery_nominal_capacity" in raw_data[invname]:
                    self.nominal_capacity = (
                        float(raw_data[invname]["battery_nominal_capacity"]) / 19.53125
                    )  # XXX: Where does 19.53125 come from? I back calculated but why that number...
                if self.base.battery_capacity_nominal:
                    if abs(self.soc_max - self.nominal_capacity) > 1.0:
                        # XXX: Weird workaround for battery reporting wrong capacity issue
                        self.base.log("Warn: REST data reports Battery Capacity kWh as {} but nominal indicates {} - using nominal".format(self.soc_max, self.nominal_capacity))
                    self.soc_max = self.nominal_capacity
                if invname in raw_data and "soc_force_adjust" in raw_data[invname]:
                    soc_force_adjust = raw_data[invname]["soc_force_adjust"]
                    if soc_force_adjust:
                        try:
                            soc_force_adjust = int(soc_force_adjust)
                        except ValueError:
                            soc_force_adjust = 0
                        if (soc_force_adjust > 0) and (soc_force_adjust < 7):
                            self.in_calibration = True
                            self.log("Warn: Inverter is in calibration mode {}, Predbat will not function correctly and will be disabled".format(soc_force_adjust))
            self.soc_max *= self.battery_scaling

            # Max battery rate
            if "Invertor_Max_Bat_Rate" in idetails:
                self.battery_rate_max_raw = idetails["Invertor_Max_Bat_Rate"]
            elif "Invertor_Max_Rate" in idetails:
                self.battery_rate_max_raw = idetails["Invertor_Max_Rate"]
            else:
                self.battery_rate_max_raw = self.base.get_arg("charge_rate", attribute="max", index=self.id, default=2600.0)

            # Max invertor rate
            if "Invertor_Max_Inv_Rate" in idetails:
                self.inverter_limit = idetails["Invertor_Max_Inv_Rate"]

            # Inverter time
            if "Invertor_Time" in idetails:
                ivtime = idetails["Invertor_Time"]
        else:
            self.soc_max = self.base.get_arg("soc_max", default=10.0, index=self.id) * self.battery_scaling
            self.nominal_capacity = self.soc_max

            self.battery_voltage = 52.0
            if "battery_voltage" in self.base.args:
                self.base.get_arg("battery_voltage", index=self.id, default=52.0)

            if self.inverter_type in ["GE", "GEC", "GEE"]:
                self.battery_rate_max_raw = self.base.get_arg("charge_rate", attribute="max", index=self.id, default=2600.0)
            elif "battery_rate_max" in self.base.args:
                self.battery_rate_max_raw = self.base.get_arg("battery_rate_max", index=self.id, default=2600.0)
            else:
                self.battery_rate_max_raw = 2600.0

            ivtime = self.base.get_arg("inverter_time", index=self.id, default=None)

        # Battery can not be zero size
        if self.soc_max <= 0:
            self.base.log("Error: Reported battery size from REST is {}, but it must be >0".format(self.soc_max))
            raise ValueError

        # Battery rate max charge, discharge (all converted to kW/min)
        self.battery_rate_max_charge = min(self.base.get_arg("inverter_limit_charge", self.battery_rate_max_raw, index=self.id), self.battery_rate_max_raw) / MINUTE_WATT
        self.battery_rate_max_discharge = min(self.base.get_arg("inverter_limit_discharge", self.battery_rate_max_raw, index=self.id), self.battery_rate_max_raw) / MINUTE_WATT
        self.battery_rate_max_charge_scaled = self.battery_rate_max_charge * self.base.battery_rate_max_scaling
        self.battery_rate_max_discharge_scaled = self.battery_rate_max_discharge * self.base.battery_rate_max_scaling_discharge
        self.battery_rate_min = min(self.base.get_arg("inverter_battery_rate_min", 0, index=self.id), self.battery_rate_max_raw) / MINUTE_WATT

        # Convert inverter time into timestamp
        if ivtime:
            try:
                self.inverter_time = datetime.strptime(ivtime, TIME_FORMAT)
            except (ValueError, TypeError):
                try:
                    self.inverter_time = datetime.strptime(ivtime, TIME_FORMAT_OCTOPUS)
                except (ValueError, TypeError):
                    try:
                        tz = pytz.timezone(self.base.get_arg("timezone", "Europe/London"))
                        self.inverter_time = tz.localize(datetime.strptime(ivtime, self.inv_clock_time_format))
                    except (ValueError, TypeError):
                        self.base.log(f"Warn: Unable to read inverter time string {ivtime} using formats {[TIME_FORMAT, TIME_FORMAT_OCTOPUS, self.inv_clock_time_format]}")
                        self.inverter_time = None
                        self.auto_restart("Unable to read inverter time")

        # Check inverter time and confirm skew
        if self.inverter_time:
            # Fetch current time again as it may have changed since we run this inverter update
            local_tz = pytz.timezone(self.base.get_arg("timezone", "Europe/London"))
            now_utc = datetime.now(local_tz)

            tdiff = self.inverter_time - now_utc
            tdiff = self.base.dp2(tdiff.seconds / 60 + tdiff.days * 60 * 24)
            if not quiet:
                self.base.log("Invertor time {} AppDaemon time {} difference {} minutes".format(self.inverter_time, now_utc, tdiff))
            if abs(tdiff) >= 10:
                self.base.log(
                    "Warn: Invertor time is {} AppDaemon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDaemon time zone".format(
                        self.inverter_time, now_utc, tdiff
                    )
                )
                self.base.record_status(
                    "Invertor time is {} AppDaemon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDaemon time zone".format(
                        self.inverter_time, now_utc, tdiff
                    ),
                    had_errors=True,
                )
                # Trigger restart
                self.auto_restart("Clock skew >=10 minutes")
            else:
                self.base.restart_active = False

        # Get current reserve value
        if self.rest_data:
            self.reserve_percent_current = float(self.rest_data["Control"]["Battery_Power_Reserve"])
        else:
            self.reserve_percent_current = max(self.base.get_arg("reserve", default=0.0, index=self.id), self.base.get_arg("battery_min_soc", default=4.0, index=self.id))
        self.reserve_current = self.base.dp2(self.soc_max * self.reserve_percent_current / 100.0)

        # Get the expected minimum reserve value
        battery_min_soc = self.base.get_arg("battery_min_soc", default=4.0, index=self.id)
        self.reserve_min = self.base.get_arg("set_reserve_min")
        if self.reserve_min < battery_min_soc:
            self.base.log(f"Increasing set_reserve_min from {self.reserve_min}%  to battery_min_soc of {battery_min_soc}%")
            self.base.expose_config("set_reserve_min", battery_min_soc)
            self.reserve_min = battery_min_soc

        self.base.log(f"Reserve min: {self.reserve_min}% Battery_min:{battery_min_soc}%")
        if self.base.set_reserve_enable:
            self.reserve_percent = self.reserve_min
        else:
            self.reserve_percent = self.reserve_percent_current
        self.reserve = self.base.dp2(self.soc_max * self.reserve_percent / 100.0)

        # Max inverter rate override
        if "inverter_limit" in self.base.args:
            self.inverter_limit = self.base.get_arg("inverter_limit", self.inverter_limit, index=self.id) / MINUTE_WATT
        if "export_limit" in self.base.args:
            self.export_limit = self.base.get_arg("export_limit", self.inverter_limit, index=self.id) / MINUTE_WATT
        # Can't export more than the inverter limit
        self.export_limit = min(self.export_limit, self.inverter_limit)

        # Log inverter details
        if not quiet:
            self.base.log(
                "Inverter {} with soc_max {} kWh nominal_capacity {} kWh battery rate raw {} w charge rate {} kW discharge rate {} kW battery_rate_min {} w ac limit {} kW export limit {} kW reserve {} % current_reserve {} %".format(
                    self.id,
                    self.base.dp2(self.soc_max),
                    self.base.dp2(self.nominal_capacity),
                    self.base.dp2(self.battery_rate_max_raw),
                    self.base.dp2(self.battery_rate_max_charge * 60.0),
                    self.base.dp2(self.battery_rate_max_discharge * 60.0),
                    self.base.dp2(self.battery_rate_min * MINUTE_WATT),
                    self.base.dp2(self.inverter_limit * 60),
                    self.base.dp2(self.export_limit * 60),
                    self.reserve_percent,
                    self.reserve_percent_current,
                )
            )

        # Create some dummy entities if PredBat expects them but they don't exist for this Inverter Type:
        # Args are also set for these so that no entries are needed for the dummies in the config file
        if not self.inv_has_charge_enable_time:
            if ("scheduled_charge_enable" not in self.base.args) or (not isinstance(self.base.args["scheduled_charge_enable"], list)):
                self.base.args["scheduled_charge_enable"] = ["on", "on", "on", "on"]
            self.base.args["scheduled_charge_enable"][id] = self.create_entity("scheduled_charge_enable", "on")

        if not self.inv_has_discharge_enable_time:
            if ("scheduled_discharge_enable" not in self.base.args) or (not isinstance(self.base.args["scheduled_discharge_enable"], list)):
                self.base.args["scheduled_discharge_enable"] = ["on", "on", "on", "on"]
            self.base.args["scheduled_discharge_enable"][id] = self.create_entity("scheduled_discharge_enable", "on")

        if not self.inv_has_reserve_soc:
            self.base.args["reserve"] = self.create_entity("reserve", self.reserve, device_class="battery", uom="%")

        if not self.inv_has_target_soc:
            self.base.args["charge_limit"] = self.create_entity("charge_limit", 100, device_class="battery", uom="%")

        if self.inv_output_charge_control != "power":
            self.base.args["charge_rate"] = self.create_entity("charge_rate", int(self.battery_rate_max_charge * MINUTE_WATT), uom="W", device_class="power")
            self.base.args["discharge_rate"] = self.create_entity("discharge_rate", int(self.battery_rate_max_discharge * MINUTE_WATT), uom="W", device_class="power")

        if not self.inv_has_ge_inverter_mode:
            if "inverter_mode" not in self.base.args:
                self.base.args["inverter_mode"] = ["Eco", "Eco", "Eco", "Eco"]
            self.base.args["inverter_mode"][id] = self.create_entity("inverter_mode", "Eco")

        if self.inv_charge_time_format != "HH:MM:SS":
            for x in ["charge", "discharge"]:
                for y in ["start", "end"]:
                    self.base.args[f"{x}_{y}_time"] = self.create_entity(f"{x}_{y}_time", "23:59:00")

    def find_charge_curve(self, discharge):
        """
        Find expected charge curve
        """
        curve_type = "charge"
        if discharge:
            curve_type = "discharge"

        soc_kwh_sensor = self.base.get_arg("soc_kw", indirect=False, index=self.id)
        if discharge:
            charge_rate_sensor = self.base.get_arg("discharge_rate", indirect=False, index=self.id)
        else:
            charge_rate_sensor = self.base.get_arg("charge_rate", indirect=False, index=self.id)
        predbat_status_sensor = "predbat.status"
        battery_power_sensor = self.base.get_arg("battery_power", indirect=False, index=self.id)
        final_curve = {}
        final_curve_count = {}

        if discharge:
            max_power = int(self.battery_rate_max_discharge * MINUTE_WATT)
        else:
            max_power = int(self.battery_rate_max_charge * MINUTE_WATT)

        if soc_kwh_sensor and charge_rate_sensor and battery_power_sensor and predbat_status_sensor:
            battery_power_sensor = battery_power_sensor.replace("number.", "sensor.")  # Workaround as old template had number.
            self.log("Find {} curve with sensors {} and {} and {} and {}".format(curve_type, soc_kwh_sensor, charge_rate_sensor, predbat_status_sensor, battery_power_sensor))
            soc_kwh_data = self.base.get_history_wrapper(entity_id=soc_kwh_sensor, days=self.base.max_days_previous)
            charge_rate_data = self.base.get_history_wrapper(entity_id=charge_rate_sensor, days=self.base.max_days_previous)
            predbat_status_data = self.base.get_history_wrapper(entity_id=predbat_status_sensor, days=self.base.max_days_previous)
            battery_power_data = self.base.get_history_wrapper(entity_id=battery_power_sensor, days=self.base.max_days_previous)

            if soc_kwh_data and charge_rate_data and charge_rate_data and battery_power_data:
                soc_kwh = self.base.minute_data(
                    soc_kwh_data[0],
                    self.base.max_days_previous,
                    self.base.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=self.battery_scaling,
                    required_unit="kWh",
                )
                charge_rate = self.base.minute_data(
                    charge_rate_data[0],
                    self.base.max_days_previous,
                    self.base.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=1.0,
                    required_unit="W",
                )
                predbat_status = self.base.minute_data_state(predbat_status_data[0], self.base.max_days_previous, self.base.now_utc, "state", "last_updated")
                battery_power = self.base.minute_data(
                    battery_power_data[0],
                    self.base.max_days_previous,
                    self.base.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=1.0,
                    required_unit="W",
                )
                min_len = min(len(soc_kwh), len(charge_rate), len(predbat_status), len(battery_power))
                self.log("Find {} curve has {} days of data, max days {}".format(curve_type, min_len / 60 / 24.0, self.base.max_days_previous))

                soc_percent = {}
                for minute in range(0, min_len):
                    soc_percent[minute] = calc_percent_limit(soc_kwh.get(minute, 0), self.soc_max)

                if discharge:
                    search_range = range(5, 20, 1)
                else:
                    search_range = range(99, 85, -1)

                # Find 100% end points
                for data_point in search_range:
                    for minute in range(1, min_len):
                        # Start trigger is when the SOC just increased above the data point
                        if (
                            not discharge
                            and soc_percent.get(minute - 1, 0) == (data_point + 1)
                            and soc_percent.get(minute, 0) == data_point
                            and predbat_status.get(minute - 1, "") == "Charging"
                            and predbat_status.get(minute, "") == "Charging"
                            and predbat_status.get(minute + 1, "") == "Charging"
                            and charge_rate.get(minute - 1, 0) == max_power
                            and charge_rate.get(minute, 0) == max_power
                            and battery_power.get(minute, 0) < 0
                        ) or (
                            discharge
                            and soc_percent.get(minute - 1, 0) == (data_point - 1)
                            and soc_percent.get(minute, 0) == data_point
                            and predbat_status.get(minute - 1, "") == "Discharging"
                            and predbat_status.get(minute, "") == "Discharging"
                            and predbat_status.get(minute + 1, "") == "Discharging"
                            and charge_rate.get(minute - 1, 0) == max_power
                            and charge_rate.get(minute, 0) == max_power
                            and battery_power.get(minute, 0) > 0
                        ):
                            total_power = 0
                            total_count = 0
                            # Find a period where charging was at full rate and the SOC just drops below the data point
                            for target_minute in range(minute, min_len):
                                this_soc = soc_percent.get(target_minute, 0)
                                if not discharge and (
                                    predbat_status.get(target_minute, "") != "Charging" or charge_rate.get(minute, 0) != max_power or battery_power.get(minute, 0) >= 0
                                ):
                                    break
                                if discharge and (
                                    predbat_status.get(target_minute, "") != "Discharging" or charge_rate.get(minute, 0) != max_power or battery_power.get(minute, 0) <= 0
                                ):
                                    break

                                if (discharge and (this_soc > data_point)) or (not discharge and (this_soc < data_point)):
                                    if total_power == 0:
                                        # No power data, so skip this data point
                                        break
                                    if discharge:
                                        this_soc -= 1
                                    else:
                                        this_soc += 1
                                    # So the power for this data point average has been stored, it's possible we spanned more than one data point
                                    # if not all SOC %'s are represented for this battery size
                                    from_soc = soc_kwh.get(minute, 0)
                                    to_soc = soc_kwh.get(target_minute, 0)
                                    soc_charged = from_soc - to_soc
                                    average_power = total_power / total_count
                                    charge_curve = round(min(average_power / max_power / self.base.battery_loss, 1.0), 2)
                                    if self.base.debug_enable:
                                        self.log(
                                            "Curve Percent: {}-{} at {} took {} minutes charged {} curve {} average_power {}".format(
                                                data_point,
                                                this_soc,
                                                self.base.time_abs_str(self.base.minutes_now - minute),
                                                total_count,
                                                round(soc_charged, 2),
                                                charge_curve,
                                                average_power,
                                            )
                                        )
                                    # Store data points
                                    if discharge:
                                        store_range = range(this_soc, data_point - 1, -1)
                                    else:
                                        store_range = range(this_soc, data_point + 1)

                                    for point in store_range:
                                        if point not in final_curve:
                                            final_curve[point] = charge_curve
                                            final_curve_count[point] = 1
                                        else:
                                            final_curve[point] += charge_curve
                                            final_curve_count[point] += 1

                                    break
                                else:
                                    # Store data
                                    total_power += abs(battery_power.get(minute, 0))
                                    total_count += 1
                if final_curve:
                    # Average the data points
                    for index in final_curve:
                        if final_curve_count[index] > 0:
                            final_curve[index] = self.base.dp2(final_curve[index] / final_curve_count[index])

                    self.log("Curve before adjustment is: {}".format(final_curve))

                    # Find info for gap filling
                    found_required = False
                    if discharge:
                        fill_range = range(4, 21, 1)
                        value = min(final_curve.values())
                    else:
                        value = max(final_curve.values())
                        fill_range = range(85, 101)

                    # Pick the first value point for fill below
                    for point in fill_range:
                        if point in final_curve:
                            value = final_curve[point]
                            break

                    # Fill gaps
                    for point in fill_range:
                        if point not in final_curve:
                            final_curve[point] = value
                            found_required = True
                        else:
                            value = final_curve[point]

                    if found_required:
                        # Scale curve to 1.0
                        rate_scaling = 0

                        # Work out the maximum power to use as a scale factor
                        for index in final_curve:
                            rate_scaling = max(final_curve[index], rate_scaling)

                        # Scale the curve
                        if rate_scaling > 0:
                            for index in final_curve:
                                final_curve[index] = round(final_curve[index] / rate_scaling, 2)

                        # If we have the correct data then output it
                        if rate_scaling > 0:
                            if discharge:
                                text = "  battery_discharge_power_curve:\n"
                            else:
                                text = "  battery_charge_power_curve:\n"
                            keys = sorted(final_curve.keys())
                            keys.reverse()
                            first = True
                            for key in keys:
                                if (final_curve[key] < 1.0 or first) and final_curve[key] > 0.0:
                                    text += "    {} : {}\n".format(key, final_curve[key])
                                    first = False
                            if self.base.battery_charge_power_curve_auto:
                                self.log("Curve automatically computed as:\n" + text)
                            else:
                                self.log("Curve curve can be entered into apps.yaml or set to auto:\n" + text)
                            rate_scaling = round(rate_scaling, 2)
                            if discharge:
                                if rate_scaling != self.base.battery_rate_max_scaling_discharge:
                                    self.log(
                                        "Consider setting in HA: input_number.battery_rate_max_scaling_discharge: {} - currently {}".format(
                                            rate_scaling, self.base.battery_rate_max_scaling_discharge
                                        )
                                    )
                            else:
                                if rate_scaling != self.base.battery_rate_max_scaling:
                                    self.log(
                                        "Consider setting in HA: input_number.battery_rate_max_scaling: {} - currently {}".format(rate_scaling, self.base.battery_rate_max_scaling)
                                    )
                            return final_curve
                        else:
                            self.log("Note: Found incorrect battery charging curve (was 0), maybe try again when you have more data.")
                    else:
                        self.log("Note: Found incomplete battery charging curve (no data points), maybe try again when you have more data.")
                else:
                    self.log(
                        "Note: Can not find battery charge curve (no final curve), one of the required settings for predbat_status, soc_kw, battery_power and charge_rate do not have history, check apps.yaml"
                    )
            else:
                self.log(
                    "Note: Can not find battery charge curve (missing history), one of the required settings for predbat_status, soc_kw, battery_power and charge_rate do not have history, check apps.yaml"
                )
        else:
            self.log(
                "Note: Can not find battery charge curve (settings missing), one of the required settings for soc_kw, battery_power and charge_rate are missing from apps.yaml"
            )
        return {}

    def create_entity(self, entity_name, value, uom=None, device_class="None"):
        """
        Create dummy entities required by non GE inverters to mimic GE behaviour
        """
        if "prefix" in self.base.args:
            prefix = self.base.get_arg("prefix", indirect=False)
        else:
            prefix = "prefix"

        entity_id = f"sensor.{prefix}_{self.inverter_type}_{self.id}_{entity_name}"

        if self.base.get_state_wrapper(entity_id) is None:
            attributes = {
                "state_class": "measurement",
            }

            if uom is not None:
                attributes["unit_of_measurement"] = uom
            if device_class is not None:
                attributes["device_class"] = device_class

            self.base.set_state_wrapper(entity_id, state=value, attributes=attributes)
        return entity_id

    def update_status(self, minutes_now, quiet=False):
        """
        Update the following with inverter status.

        Inverter Class Parameters
        =========================

            Parameter                          Type    Units
            ---------                          ----    -----
            self.rest_data                     dict
            self.charge_enable_time            bool
            self.discharge_enable_time         bool
            self.charge_rate_now               float
            self.discharge_rate_now            float
            self.soc_kw                        float   kWh
            self.soc_percent                   float   %
            self.battery_power                 float   W
            self.pv_power                      float   W
            self.load_power                    float   W
            self.charge_start_time_minutes     int
            self.charge_end_time_minutes       int
            self.charge_window                 list of dicts
            self.discharge_start_time_minutes  int
            self.discharge_end_time_minutes    int
            self.discharge_window              list of dicts
            self.battery_voltage               float   V

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            None
        """

        # If it's not a GE inverter then turn Quiet off
        if self.inverter_type != "GE":
            quiet = False

        self.battery_power = 0
        self.pv_power = 0
        self.load_power = 0

        if self.rest_api:
            self.rest_data = self.rest_readData()

        if self.rest_data:
            self.charge_enable_time = self.rest_data["Control"]["Enable_Charge_Schedule"] == "enable"
            self.discharge_enable_time = self.rest_data["Control"]["Enable_Discharge_Schedule"] == "enable"
            self.charge_rate_now = self.rest_data["Control"]["Battery_Charge_Rate"] / MINUTE_WATT
            self.discharge_rate_now = self.rest_data["Control"]["Battery_Discharge_Rate"] / MINUTE_WATT
        else:
            self.log(
                "Inverter {} scheduled_charge_enable {} scheduled_discharge_enable {}".format(
                    self.id, self.base.get_arg("scheduled_charge_enable", "on", index=self.id), self.base.get_arg("scheduled_discharge_enable", "off", index=self.id)
                )
            )
            self.charge_enable_time = self.base.get_arg("scheduled_charge_enable", "on", index=self.id) == "on"
            self.discharge_enable_time = self.base.get_arg("scheduled_discharge_enable", "off", index=self.id) == "on"
            self.charge_rate_now = self.base.get_arg("charge_rate", index=self.id, default=2600.0) / MINUTE_WATT
            self.discharge_rate_now = self.base.get_arg("discharge_rate", index=self.id, default=2600.0) / MINUTE_WATT

        # Scale charge and discharge rates with battery scaling
        self.charge_rate_now = max(self.charge_rate_now * self.base.battery_rate_max_scaling, self.battery_rate_min)
        self.discharge_rate_now = max(self.discharge_rate_now * self.base.battery_rate_max_scaling_discharge, self.battery_rate_min)

        if SIMULATE:
            self.soc_kw = self.base.sim_soc_kw
        else:
            if self.rest_data:
                self.soc_kw = self.rest_data["Power"]["Power"]["SOC_kWh"] * self.battery_scaling
            else:
                if "soc_percent" in self.base.args:
                    self.soc_kw = self.base.get_arg("soc_percent", default=0.0, index=self.id) * self.soc_max * self.battery_scaling / 100.0
                else:
                    self.soc_kw = self.base.get_arg("soc_kw", default=0.0, index=self.id) * self.battery_scaling

        if self.soc_max <= 0.0:
            self.soc_percent = 0
        else:
            self.soc_percent = calc_percent_limit(self.soc_kw, self.soc_max)

        if self.rest_data and ("Power" in self.rest_data):
            pdetails = self.rest_data["Power"]
            if "Power" in pdetails:
                ppdetails = pdetails["Power"]
                self.battery_power = float(ppdetails.get("Battery_Power", 0.0))
                self.pv_power = float(ppdetails.get("PV_Power", 0.0))
                self.load_power = float(ppdetails.get("Load_Power", 0.0))
        else:
            self.battery_power = self.base.get_arg("battery_power", default=0.0, index=self.id)
            self.pv_power = self.base.get_arg("pv_power", default=0.0, index=self.id)
            self.load_power = self.base.get_arg("load_power", default=0.0, index=self.id)

            for i in range(1, self.inv_num_load_entities):
                self.load_power += self.base.get_arg(f"load_power_{i}", default=0.0, index=self.id)

        self.battery_voltage = self.base.get_arg("battery_voltage", default=52.0, index=self.id)

        if not quiet:
            self.base.log(
                "Inverter {} SOC: {} kW {} % Current charge rate {} w Current discharge rate {} w Current power {} w Current voltage {}".format(
                    self.id,
                    self.base.dp2(self.soc_kw),
                    self.soc_percent,
                    self.charge_rate_now * MINUTE_WATT,
                    self.discharge_rate_now * MINUTE_WATT,
                    self.battery_power,
                    self.battery_voltage,
                )
            )

        # If the battery is being charged then find the charge window
        if self.charge_enable_time or not self.inv_has_charge_enable_time:
            # Find current charge window
            if SIMULATE:
                charge_start_time = datetime.strptime(self.base.sim_charge_start_time, "%H:%M:%S")
                charge_end_time = datetime.strptime(self.base.sim_charge_end_time, "%H:%M:%S")
            else:
                if self.rest_data:
                    charge_start_time = datetime.strptime(self.rest_data["Timeslots"]["Charge_start_time_slot_1"], "%H:%M:%S")
                    charge_end_time = datetime.strptime(self.rest_data["Timeslots"]["Charge_end_time_slot_1"], "%H:%M:%S")
                elif "charge_start_time" in self.base.args:
                    charge_start_time = datetime.strptime(self.base.get_arg("charge_start_time", index=self.id), "%H:%M:%S")
                    charge_end_time = datetime.strptime(self.base.get_arg("charge_end_time", index=self.id), "%H:%M:%S")
                else:
                    self.log("Error: Inverter {} unable to read charge window time as neither REST, charge_start_time or charge_start_hour are set".format(self.id))
                    self.base.record_status(
                        "Error: Inverter {} unable to read charge window time as neither REST, charge_start_time or charge_start_hour are set".format(self.id), had_errors=True
                    )
                    raise ValueError

            # Update simulated charge enable time to match the charge window time.
            if not self.inv_has_charge_enable_time:
                if charge_start_time == charge_end_time:
                    self.charge_enable_time = False
                else:
                    self.charge_enable_time = True
                self.write_and_poll_switch("scheduled_charge_enable", self.base.get_arg("scheduled_charge_enable", indirect=False, index=self.id), self.charge_enable_time)
                self.log("Inverter {} scheduled_charge_enable set to {}".format(self.id, self.charge_enable_time))

            # Track charge start/end
            self.track_charge_start = charge_start_time.strftime("%H:%M:%S")
            self.track_charge_end = charge_end_time.strftime("%H:%M:%S")

            # Reverse clock skew
            charge_start_time -= timedelta(seconds=self.base.inverter_clock_skew_start * 60)
            charge_end_time -= timedelta(seconds=self.base.inverter_clock_skew_end * 60)

            # Compute charge window minutes start/end just for the next charge window
            self.charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
            self.charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute

            if self.charge_end_time_minutes < self.charge_start_time_minutes:
                # As windows wrap, if end is in the future then move start back, otherwise forward
                if self.charge_end_time_minutes > minutes_now:
                    self.charge_start_time_minutes -= 60 * 24
                else:
                    self.charge_end_time_minutes += 60 * 24

            # Window already passed, move it forward until the next one
            if self.charge_end_time_minutes < minutes_now:
                self.charge_start_time_minutes += 60 * 24
                self.charge_end_time_minutes += 60 * 24

        else:
            # If charging is disabled set a fake window outside
            self.charge_start_time_minutes = self.base.forecast_minutes
            self.charge_end_time_minutes = self.base.forecast_minutes
            self.track_charge_start = "00:00:00"
            self.track_charge_end = "00:00:00"

        # Construct charge window from the GivTCP settings
        self.charge_window = []

        if not quiet:
            self.base.log("Inverter {} scheduled charge enable is {}".format(self.id, self.charge_enable_time))

        if self.charge_enable_time:
            minute = max(0, self.charge_start_time_minutes)  # Max is here is start could be before midnight now
            minute_end = self.charge_end_time_minutes
            while minute < (self.base.forecast_minutes + minutes_now):
                window = {}
                window["start"] = minute
                window["end"] = minute_end
                window["average"] = 0  # Rates are not known yet
                self.charge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60

        if not quiet:
            self.base.log("Inverter {} charge windows currently {}".format(self.id, self.charge_window))

        # Work out existing charge limits and percent
        if self.charge_enable_time:
            if self.rest_data:
                self.current_charge_limit = float(self.rest_data["Control"]["Target_SOC"])
            else:
                self.current_charge_limit = float(self.base.get_arg("charge_limit", index=self.id, default=100.0))
        else:
            self.current_charge_limit = 0.0

        if not quiet:
            if self.charge_enable_time:
                self.base.log(
                    "Inverter {} Charge settings: {}-{} limit {} power {} kW".format(
                        self.id,
                        self.base.time_abs_str(self.charge_start_time_minutes),
                        self.base.time_abs_str(self.charge_end_time_minutes),
                        self.current_charge_limit,
                        self.charge_rate_now * 60.0,
                    )
                )
            else:
                self.base.log("Inverter {} Charge settings: timed charged is disabled, power {} kW".format(self.id, round(self.charge_rate_now * 60.0, 2)))

        # Construct discharge window from GivTCP settings
        self.discharge_window = []

        if self.rest_data:
            discharge_start = datetime.strptime(self.rest_data["Timeslots"]["Discharge_start_time_slot_1"], "%H:%M:%S")
            discharge_end = datetime.strptime(self.rest_data["Timeslots"]["Discharge_end_time_slot_1"], "%H:%M:%S")
        elif "discharge_start_time" in self.base.args:
            discharge_start = datetime.strptime(self.base.get_arg("discharge_start_time", index=self.id), "%H:%M:%S")
            discharge_end = datetime.strptime(self.base.get_arg("discharge_end_time", index=self.id), "%H:%M:%S")
        else:
            self.log("Error: Inverter {} unable to read Discharge window as neither REST or discharge_start_time are set".format(self.id))
            self.base.record_status("Error: Inverter {} unable to read Discharge window as neither REST or discharge_start_time are set".format(self.id), had_errors=True)
            raise ValueError

        # Update simulated discharge enable time to match the discharge window time.
        if not self.inv_has_discharge_enable_time:
            if discharge_start == discharge_end:
                self.discharge_enable_time = False
            else:
                self.discharge_enable_time = True
            entity_id = self.base.get_arg("scheduled_discharge_enable", indirect=False, index=self.id)
            self.write_and_poll_switch("scheduled_discharge_enable", entity_id, self.discharge_enable_time)
            self.log("Inverter {} {} set to {}".format(self.id, entity_id, self.discharge_enable_time))

        # Tracking for idle time
        if self.discharge_enable_time:
            self.track_discharge_start = discharge_start.strftime("%H:%M:%S")
            self.track_discharge_end = discharge_end.strftime("%H:%M:%S")
        else:
            self.track_discharge_start = "00:00:00"
            self.track_discharge_end = "00:00:00"

        # Reverse clock skew
        discharge_start -= timedelta(seconds=self.base.inverter_clock_skew_discharge_start * 60)
        discharge_end -= timedelta(seconds=self.base.inverter_clock_skew_discharge_end * 60)

        # Compute discharge window minutes start/end just for the next discharge window
        self.discharge_start_time_minutes = discharge_start.hour * 60 + discharge_start.minute
        self.discharge_end_time_minutes = discharge_end.hour * 60 + discharge_end.minute

        if self.discharge_end_time_minutes < self.discharge_start_time_minutes:
            # As windows wrap, if end is in the future then move start back, otherwise forward
            if self.discharge_end_time_minutes > minutes_now:
                self.discharge_start_time_minutes -= 60 * 24
            else:
                self.discharge_end_time_minutes += 60 * 24

        if not quiet:
            self.base.log("Inverter {} scheduled discharge enable is {}".format(self.id, self.discharge_enable_time))
        # Pre-fill current discharge window
        # Store it even when discharge timed isn't enabled as it won't be outside the actual slot
        if True:
            minute = max(0, self.discharge_start_time_minutes)  # Max is here is start could be before midnight now
            minute_end = self.discharge_end_time_minutes
            while minute < self.base.forecast_minutes:
                window = {}
                window["start"] = minute
                window["end"] = minute_end
                window["average"] = 0  # Rates are not known yet
                self.discharge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60

        # Pre-fill best discharge enables
        if self.discharge_enable_time:
            self.discharge_limits = [0.0 for i in range(len(self.discharge_window))]
        else:
            self.discharge_limits = [100.0 for i in range(len(self.discharge_window))]

        if not quiet:
            self.base.log("Inverter {} discharge windows currently {}".format(self.id, self.discharge_window))

        if INVERTER_TEST:
            self.self_test(minutes_now)

    def mimic_target_soc(self, current_charge_limit):
        """
        Function to turn on/off charging based on the current SOC and the set charge limit

        Parameters:
            current_charge_limit (float): The target SOC (State of Charge) limit for charging.

        Returns:
            None
        """
        charge_power = self.base.get_arg("charge_rate", index=self.id, default=2600.0)
        if current_charge_limit == 0:
            self.alt_charge_discharge_enable("eco", True)  # ECO Mode
        elif self.soc_percent > float(current_charge_limit):
            # If current SOC is above Target SOC, turn Grid Charging off
            self.alt_charge_discharge_enable("charge", False)
            self.base.log(f"Current SOC {self.soc_percent}% is greater than Target SOC {current_charge_limit}. Grid Charge disabled.")
        elif self.soc_percent == float(current_charge_limit):  # If SOC target is reached
            self.alt_charge_discharge_enable("charge", True)  # Make sure charging is on
            if self.inv_output_charge_control == "current":
                self.set_current_from_power("charge", (0))  # Set charge current to zero (i.e hold SOC)
                self.base.log(f"Current SOC {self.soc_percent}% is same as Target SOC {current_charge_limit}. Grid Charge enabled, Amps rate set to 0.")
        else:
            # If we drop below the target, turn grid charging back on and make sure the charge current is correct
            self.alt_charge_discharge_enable("charge", True)
            if self.inv_output_charge_control == "current":
                self.set_current_from_power("charge", charge_power)  # Write previous current setting to inverter
                self.base.log(f"Current SOC {self.soc_percent}% is less than Target SOC {current_charge_limit}. Grid Charge enabled, amp rate written to inverter.")
            self.base.log(
                f"Current SOC {self.soc_percent}% is less than Target SOC {current_charge_limit}. Grid charging enabled with charge current set to {self.base.get_arg('timed_charge_current', index=self.id, default=65):0.2f}"
            )

    def adjust_reserve(self, reserve):
        """
        Adjust the output reserve target %

        Inverter Class Parameters
        =========================

            None

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            reserve                            int           %

        """

        if SIMULATE:
            current_reserve = float(self.base.sim_reserve)
        else:
            if self.rest_data:
                current_reserve = float(self.rest_data["Control"]["Battery_Power_Reserve"])
            else:
                current_reserve = self.base.get_arg("reserve", index=self.id, default=0.0)

        # Round to integer and clamp to minimum
        reserve = int(reserve + 0.5)
        if reserve < self.reserve_percent:
            reserve = self.reserve_percent

        # Clamp reserve at max setting
        reserve = min(reserve, self.reserve_max)

        if current_reserve != reserve:
            self.base.log("Inverter {} Current Reserve is {} % and new target is {} %".format(self.id, current_reserve, reserve))
            if SIMULATE:
                self.base.sim_reserve = reserve
            else:
                if self.rest_data:
                    self.rest_setReserve(reserve)
                else:
                    self.write_and_poll_value("reserve", self.base.get_arg("reserve", indirect=False, index=self.id), reserve)
                if self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Target Reserve has been changed to {} at {}".format(self.id, reserve, self.base.time_now_str()))
            self.mqtt_message(topic="set/reserve", payload=reserve)
        else:
            self.base.log("Inverter {} Current reserve is {} already at target".format(self.id, current_reserve))

    def adjust_charge_rate(self, new_rate, notify=True):
        """
        Adjust charging rate

        Inverter Class Parameters
        =========================

            None

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            charge_rate                        float         W
            *timed_charge_current              float         A


        *If the inverter uses current rather than power we create a dummy entity for the power anyway but also write to the current entity
        """

        new_rate = int(new_rate + 0.5)

        if SIMULATE:
            current_rate = self.base.sim_charge_rate_now
        else:
            if self.rest_data:
                current_rate = int(self.rest_data["Control"]["Battery_Charge_Rate"])
            else:
                current_rate = self.base.get_arg("charge_rate", index=self.id, default=2600.0)

        if abs(current_rate - new_rate) > 100:
            self.base.log("Inverter {} current charge rate is {} and new target is {}".format(self.id, current_rate, new_rate))
            if SIMULATE:
                self.base.sim_charge_rate_now = new_rate
            else:
                if self.rest_data:
                    self.rest_setChargeRate(new_rate)
                else:
                    if "charge_rate" in self.base.args:
                        self.write_and_poll_value(
                            "charge_rate", self.base.get_arg("charge_rate", indirect=False, index=self.id), new_rate, fuzzy=(self.battery_rate_max_charge * MINUTE_WATT / 12)
                        )

                    if self.inv_output_charge_control == "current":
                        self.set_current_from_power("charge", new_rate)

                if notify and self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} charge rate changes to {} at {}".format(self.id, new_rate, self.base.time_now_str()))
            self.mqtt_message(topic="set/charge_rate", payload=new_rate)

    def adjust_discharge_rate(self, new_rate, notify=True):
        """
        Adjust discharging rate

        Inverter Class Parameters
        =========================

            None

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            discharge_rate                        float         W
            *timed_discharge_current              float         A

        *If the inverter uses current rather than power we create a dummy entity for the power anyway but also write to the current entity
        """
        new_rate = int(new_rate + 0.5)

        if SIMULATE:
            current_rate = self.base.sim_discharge_rate_now
        else:
            if self.rest_data:
                current_rate = self.rest_data["Control"]["Battery_Discharge_Rate"]
            else:
                current_rate = self.base.get_arg("discharge_rate", index=self.id, default=2600.0)

        if abs(current_rate - new_rate) > 100:
            self.base.log("Inverter {} current discharge rate is {} and new target is {}".format(self.id, current_rate, new_rate))
            if SIMULATE:
                self.base.sim_discharge_rate_now = new_rate
            else:
                if self.rest_data:
                    self.rest_setDischargeRate(new_rate)
                else:
                    if "discharge_rate" in self.base.args:
                        self.write_and_poll_value(
                            "discharge_rate",
                            self.base.get_arg("discharge_rate", indirect=False, index=self.id),
                            new_rate,
                            fuzzy=(self.battery_rate_max_discharge * MINUTE_WATT / 25),
                        )

                    if self.inv_output_charge_control == "current":
                        self.set_current_from_power("discharge", new_rate)

                if notify and self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} discharge rate changes to {} at {}".format(self.id, new_rate, self.base.time_now_str()))
            self.mqtt_message(topic="set/discharge_rate", payload=new_rate)

    def adjust_battery_target(self, soc, isCharging=False):
        """
        Adjust the battery charging target SOC % in GivTCP

        Inverter Class Parameters
        =========================

            None

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            charge_limit                       int           %

        """
        # SOC has no decimal places and clamp in min
        soc = int(max(soc, self.reserve_percent))

        # Check current setting and adjust
        if SIMULATE:
            current_soc = self.base.sim_soc
        else:
            if self.rest_data:
                current_soc = int(float(self.rest_data["Control"]["Target_SOC"]))
            else:
                current_soc = int(float(self.base.get_arg("charge_limit", index=self.id, default=100.0)))

        if current_soc != soc:
            self.base.log("Inverter {} Current charge limit is {} % and new target is {} %".format(self.id, current_soc, soc))
            self.current_charge_limit = soc
            if SIMULATE:
                self.base.sim_soc = soc
            else:
                if self.rest_data:
                    self.rest_setChargeTarget(soc)
                else:
                    self.write_and_poll_value("charge_limit", self.base.get_arg("charge_limit", indirect=False, index=self.id), soc)

                if self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Target SOC has been changed to {} % at {}".format(self.id, soc, self.base.time_now_str()))
            self.mqtt_message(topic="set/target_soc", payload=soc)
        else:
            self.base.log("Inverter {} Current Target SOC is {} already at target".format(self.id, current_soc))

        # Inverters that need on/off controls rather than target SOC
        if not self.inv_has_target_soc:
            if isCharging:
                self.mimic_target_soc(soc)
            else:
                self.mimic_target_soc(0)

    def write_and_poll_switch(self, name, entity_id, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        # Re-written to minimise writes
        domain, entity_name = entity_id.split(".")

        current_state = self.base.get_state_wrapper(entity_id=entity_id)
        if isinstance(current_state, str):
            current_state = current_state.lower() in ["on", "enable", "true"]

        retry = 0
        while current_state != new_value and retry < 6:
            retry += 1
            if domain == "sensor":
                if new_value:
                    self.base.set_state_wrapper(state="on", entity_id=entity_id)
                else:
                    self.base.set_state_wrapper(state="off", entity_id=entity_id)
            else:
                base_entity = entity_id.split(".")[0]
                service = base_entity + "/turn_" + ("on" if new_value else "off")
                self.base.call_service_wrapper(service, entity_id=entity_id)

            time.sleep(self.inv_write_and_poll_sleep)
            current_state = self.base.get_state_wrapper(entity_id=entity_id, refresh=True)
            self.log("Switch {} is now {}".format(entity_id, current_state))
            if isinstance(current_state, str):
                current_state = current_state.lower() in ["on", "enable", "true"]

        if current_state == new_value:
            self.base.log("Inverter {} Wrote {} to {} successfully and got {}".format(self.id, name, new_value, self.base.get_state_wrapper(entity_id=entity_id)))
            return True
        else:
            self.base.log("Warn: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, self.base.get_state_wrapper(entity_id=entity_id)))
            self.base.record_status("Warn: Inverter {} write to {} failed".format(self.id, name), had_errors=True)
            return False

    def write_and_poll_value(self, name, entity_id, new_value, fuzzy=0):
        # Modified to cope with sensor entities and writing strings
        # Re-written to minimise writes
        domain, entity_name = entity_id.split(".")
        current_state = self.base.get_state_wrapper(entity_id)

        if isinstance(new_value, str):
            matched = current_state == new_value
        else:
            matched = abs(float(current_state) - new_value) <= fuzzy

        retry = 0
        while (not matched) and (retry < 6):
            retry += 1
            if domain == "sensor":
                self.base.set_state_wrapper(entity_id, state=new_value)
            else:
                entity_base = entity_id.split(".")[0]
                service = entity_base + "/set_value"

                self.base.call_service_wrapper(service, value=new_value, entity_id=entity_id)

            time.sleep(self.inv_write_and_poll_sleep)
            current_state = self.base.get_state_wrapper(entity_id, refresh=True)
            if isinstance(new_value, str):
                matched = current_state == new_value
            else:
                matched = abs(float(current_state) - new_value) <= fuzzy

        if retry == 0:
            self.base.log(f"Inverter {self.id} No write needed for {name}: {new_value} == {current_state}")
            return True
        elif matched:
            self.base.log(f"Inverter {self.id} Wrote {new_value} to {name}, successfully now {current_state}")
            return True
        else:
            self.base.log(f"Warn: Inverter {self.id} Trying to write {new_value} to {name} didn't complete got {current_state}")
            self.base.record_status(f"Warn: Inverter {self.id} write to {name} failed", had_errors=True)
            return False

    def write_and_poll_option(self, name, entity_id, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        for retry in range(6):
            entity_base = entity_id.split(".")[0]
            service = entity_base + "/select_option"
            self.base.call_service_wrapper(service, option=new_value, entity_id=entity_id)
            time.sleep(self.inv_write_and_poll_sleep)
            old_value = self.base.get_state_wrapper(entity_id, refresh=True)
            if old_value == new_value:
                self.base.log("Inverter {} Wrote {} to {} successfully".format(self.id, name, new_value))
                return True
        self.base.log("Warn: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, self.base.get_state_wrapper(entity_id, refresh=True)))
        self.base.record_status("Warn: Inverter {} write to {} failed".format(self.id, name), had_errors=True)
        return False

    def adjust_pause_mode(self, pause_charge=False, pause_discharge=False):
        """
        Inverter control for Pause mode
        """

        # Ignore if inverter doesn't have pause mode
        if not self.inv_has_timed_pause:
            return

        entity_mode = self.base.get_arg("pause_mode", indirect=False, index=self.id)
        entity_start = self.base.get_arg("pause_start_time", indirect=False, index=self.id)
        entity_end = self.base.get_arg("pause_end_time", indirect=False, index=self.id)
        old_pause_mode = None
        old_start_time = None
        old_end_time = None

        # As not all inverters have these options we need to gracefully give up if its missing
        if entity_mode:
            old_pause_mode = self.base.get_state_wrapper(entity_mode)
            if old_pause_mode is None:
                entity_mode = None

        if entity_start:
            old_start_time = self.base.get_state_wrapper(entity_start)
            if old_start_time is None:
                entity_start = None
                self.log("Note: Inverter {} does not have pause_start_time entity".format(self.id))

        if entity_end:
            old_end_time = self.base.get_state_wrapper(entity_end)
            if old_end_time is None:
                self.log("Note: Inverter {} does not have pause_end_time entity".format(self.id))
                entity_end = None

        if not entity_mode:
            self.log("Warn: Inverter {} does not have pause_mode entity configured correctly".format(self.id))
            return

        # Some inverters have start/end time registers
        new_start_time = "00:00:00"
        new_end_time = "23:59:00"

        if pause_charge and pause_discharge:
            new_pause_mode = "PauseBoth"
        elif pause_charge:
            new_pause_mode = "PauseCharge"
        elif pause_discharge:
            new_pause_mode = "PauseDischarge"
        else:
            new_pause_mode = "Disabled"

        if old_start_time and old_start_time != new_start_time:
            # Don't poll as inverters with no registers will fail
            self.base.set_state_wrapper(entity_start, state=new_start_time)
            self.base.log("Inverter {} set pause start time to {}".format(self.id, new_start_time))
        if old_end_time and old_end_time != new_end_time:
            # Don't poll as inverters with no registers will fail
            self.base.set_state_wrapper(entity_end, state=new_end_time)
            self.base.log("Inverter {} set pause end time to {}".format(self.id, new_end_time))

        # Set the mode
        if new_pause_mode != old_pause_mode:
            self.write_and_poll_option("pause_mode", entity_mode, new_pause_mode)

            if self.base.set_inverter_notify:
                self.base.call_notify("Predbat: Inverter {} pause mode to set {} at time {}".format(self.id, new_pause_mode, self.base.time_now_str()))

            self.base.log("Inverter {} set pause mode to {}".format(self.id, new_pause_mode))

    def adjust_inverter_mode(self, force_discharge, changed_start_end=False):
        """
        Adjust inverter mode between force discharge and ECO

        Inverter Class Parameters
        =========================

            None

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            inverter_mode                      string

        """
        if SIMULATE:
            old_inverter_mode = self.base.sim_inverter_mode
        else:
            if self.rest_data:
                old_inverter_mode = self.rest_data["Control"]["Mode"]
            else:
                # Inverter mode
                if changed_start_end and not self.rest_data:
                    # XXX: Workaround for GivTCP window state update time to take effort
                    self.base.log("Sleeping (workaround) as start/end of discharge window was just adjusted")
                    time.sleep(30)
                old_inverter_mode = self.base.get_arg("inverter_mode", index=self.id)

        # For the purpose of this function consider Eco Paused as the same as Eco (it's a difference in reserve setting)
        if old_inverter_mode == "Eco (Paused)":
            old_inverter_mode = "Eco"

        # Force discharge or Eco mode?
        if force_discharge:
            new_inverter_mode = "Timed Export"
        else:
            new_inverter_mode = "Eco"

        # Change inverter mode
        if old_inverter_mode != new_inverter_mode:
            if SIMULATE:
                self.base.sim_inverter_mode = new_inverter_mode
            else:
                if self.rest_data:
                    self.rest_setBatteryMode(new_inverter_mode)
                else:
                    entity_id = self.base.get_arg("inverter_mode", indirect=False, index=self.id)
                    if self.inv_has_ge_inverter_mode:
                        self.write_and_poll_option("inverter_mode", entity_id, new_inverter_mode)
                    else:
                        self.write_and_poll_value("inverter_mode", entity_id, new_inverter_mode)

                # Notify
                if self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Force discharge set to {} at time {}".format(self.id, force_discharge, self.base.time_now_str()))

            self.base.log("Inverter {} set force discharge to {}".format(self.id, force_discharge))

    def adjust_idle_time(self, charge_start=None, charge_end=None, discharge_start=None, discharge_end=None):
        """
        Adjust inverter idle time based on charge/discharge times
        """
        if charge_start:
            self.track_charge_start = charge_start
        if charge_end:
            self.track_charge_end = charge_end
        if discharge_start:
            self.track_discharge_start = discharge_start
        if discharge_end:
            self.track_discharge_end = discharge_end

        self.log("Adjust idle time, charge {}-{} discharge {}-{}".format(self.track_charge_start, self.track_charge_end, self.track_discharge_start, self.track_discharge_end))

        minutes_now = self.base.minutes_now
        charge_start_minutes, charge_end_minutes = self.window2minutes(self.track_charge_start, self.track_charge_end, "%H:%M:%S", minutes_now)
        discharge_start_minutes, discharge_end_minutes = self.window2minutes(self.track_discharge_start, self.track_discharge_end, "%H:%M:%S", minutes_now)

        # Idle from now until midnight
        idle_start_minutes = minutes_now
        idle_end_minutes = 2 * 24 * 60 - 1

        if charge_start_minutes <= minutes_now and charge_end_minutes > minutes_now:
            # We are in a charge window so move on the idle start
            idle_start_minutes = max(idle_start_minutes, charge_end_minutes)
            # self.log("Clamp idle start until after charge start - idle start now {}".format(idle_start_minutes))

        if idle_end_minutes > charge_start_minutes and idle_start_minutes < charge_start_minutes:
            # Avoid the end running over the charge start
            idle_end_minutes = min(idle_end_minutes, charge_start_minutes)
            # self.log("Clamp idle end until before start charge - idle end now {}".format(idle_end_minutes))

        if discharge_start_minutes <= minutes_now and discharge_end_minutes > minutes_now:
            # We are in a discharge window so move on the idle start
            idle_start_minutes = max(idle_start_minutes, discharge_end_minutes)
            # self.log("Clamp idle start until after discharge start - idle start now {}".format(idle_start_minutes))

        if idle_end_minutes > discharge_start_minutes and idle_start_minutes < discharge_start_minutes:
            # Avoid the end running over the discharge start
            idle_end_minutes = min(idle_end_minutes, discharge_start_minutes)
            # self.log("Clamp idle end until before discharge charge - idle end now {}".format(idle_end_minutes))

        # Avoid midnight span
        if idle_start_minutes < 24 * 60:
            idle_end_minutes = min(24 * 60 - 1, idle_end_minutes)
            # self.log("clamp idle end at midnight, now {}".format(idle_end_minutes))

        if idle_start_minutes >= idle_end_minutes:
            # Not until tomorrow so skip for now
            idle_start_minutes = 0
            idle_end_minutes = 0
            # self.log("Reset idle start/end due to being no window")

        idle_start_time = self.base.midnight_utc + timedelta(minutes=idle_start_minutes)
        idle_end_time = self.base.midnight_utc + timedelta(minutes=idle_end_minutes)
        idle_start = idle_start_time.strftime("%H:%M:%S")
        idle_end = idle_end_time.strftime("%H:%M:%S")

        self.base.log("Adjust idle time computed idle is {}-{}".format(idle_start, idle_end))

        # Write idle start/end time
        if self.inv_has_idle_time:
            idle_start_time_id = self.base.get_arg("idle_start_time", indirect=False, index=self.id)
            idle_end_time_id = self.base.get_arg("idle_end_time", indirect=False, index=self.id)

            if idle_start_time_id and idle_end_time_id:
                old_start = self.base.get_arg("idle_start_time", index=self.id)
                old_end = self.base.get_arg("idle_end_time", index=self.id)

                if old_start != idle_start:
                    self.base.log("Inverter {} set new idle start time to {} was {}".format(self.id, idle_start, old_start))
                    self.write_and_poll_option("idle_start_time", idle_start_time_id, idle_start)
                if old_end != idle_end:
                    self.base.log("Inverter {} set new idle end time to {} was {}".format(self.id, idle_end, old_end))
                    self.write_and_poll_option("idle_end_time", idle_end_time_id, idle_end)

    def window2minutes(self, start, end, format, minutes_now):
        """
        Convert time start/end window string into minutes
        """
        start = datetime.strptime(start, format)
        end = datetime.strptime(end, format)
        start_minute = start.hour * 60 + start.minute
        end_minute = end.hour * 60 + end.minute

        if end_minute < start_minute:
            # As windows wrap, if end is in the future then move start back, otherwise forward
            if end_minute > minutes_now:
                start_minute -= 60 * 24
            else:
                end_minute += 60 * 24

        # Window already passed, move it forward until the next one
        if end_minute < minutes_now:
            start_minute += 60 * 24
            end_minute += 60 * 24
        return start_minute, end_minute

    def adjust_force_discharge(self, force_discharge, new_start_time=None, new_end_time=None):
        """
        Adjust force discharge on/off and set the time window correctly

        Inverter Class Parameters
        =========================

            None

        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            discharge_start_time               string
            discharge_end_time                 string
            *discharge_start_hour              int
            *discharge_start_minute            int
            *discharge_end_hour                int
            *discharge_end_minute              int
            *charge_discharge_update_button    button

        """

        if SIMULATE:
            old_start = self.base.sim_discharge_start
            old_end = self.base.sim_discharge_end
            old_discharge_enable = False
        else:
            if self.rest_data:
                old_start = self.rest_data["Timeslots"]["Discharge_start_time_slot_1"]
                old_end = self.rest_data["Timeslots"]["Discharge_end_time_slot_1"]
                old_discharge_enable = self.rest_data["Control"]["Enable_Discharge_Schedule"]
            elif "discharge_start_time" in self.base.args:
                old_start = self.base.get_arg("discharge_start_time", index=self.id)
                old_end = self.base.get_arg("discharge_end_time", index=self.id)
                old_discharge_enable = self.base.get_arg("scheduled_discharge_enable", "off", index=self.id) == "on"
            else:
                self.log("Warn: Inverter {} unable read discharge window as neither REST, discharge_start_time or discharge_start_hour are set".format(self.id))
                return False

        # If the inverter doesn't have a discharge enable time then use midnight-midnight as an alternative disable
        if not self.inv_has_discharge_enable_time and not force_discharge:
            new_start_time = self.base.midnight_utc
            new_end_time = self.base.midnight_utc

        # Start time to correct format
        if new_start_time:
            new_start_time += timedelta(seconds=self.base.inverter_clock_skew_discharge_start * 60)
            new_start = new_start_time.strftime("%H:%M:%S")
        else:
            new_start = None

        # End time to correct format
        if new_end_time:
            new_end_time += timedelta(seconds=self.base.inverter_clock_skew_discharge_end * 60)
            new_end = new_end_time.strftime("%H:%M:%S")
        else:
            new_end = None

        # Eco mode, turn it on before we change the discharge window
        if not force_discharge:
            self.adjust_inverter_mode(force_discharge)

        self.base.log("Inverter {} Adjust force discharge to {}, change times from {} - {} to {} - {}".format(self.id, force_discharge, old_start, old_end, new_start, new_end))
        changed_start_end = False

        # Some inverters have an idle time setting
        if force_discharge:
            self.adjust_idle_time(discharge_start=new_start, discharge_end=new_end)
        else:
            self.adjust_idle_time(discharge_start="00:00:00", discharge_end="00:00:00")

        # Change start time
        if new_start and new_start != old_start:
            self.base.log("Inverter {} set new start time to {}".format(self.id, new_start))
            if SIMULATE:
                self.base.sim_discharge_start = new_start
            else:
                if self.rest_data:
                    pass  # REST writes as a single start/end time

                elif "discharge_start_time" in self.base.args:
                    # Always write to this as it is the GE default
                    changed_start_end = True
                    entity_discharge_start_time_id = self.base.get_arg("discharge_start_time", indirect=False, index=self.id)
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("discharge_start_time", entity_discharge_start_time_id, new_start)
                    else:
                        self.write_and_poll_value("discharge_start_time", entity_discharge_start_time_id, new_start)

                    if self.inv_charge_time_format == "H M":
                        # If the inverter uses hours and minutes then write to these entities too
                        self.write_and_poll_value("discharge_start_hour", self.base.get_arg("discharge_start_hour", indirect=False, index=self.id), int(new_start[:2]))
                        self.write_and_poll_value("discharge_start_minute", self.base.get_arg("discharge_start_minute", indirect=False, index=self.id), int(new_start[3:5]))
                else:
                    self.log("Warn: Inverter {} unable write discharge start time as neither REST or discharge_start_time are set".format(self.id))

        # Change end time
        if new_end and new_end != old_end:
            self.base.log("Inverter {} Set new end time to {} was {}".format(self.id, new_end, old_end))
            if SIMULATE:
                self.base.sim_discharge_end = new_end
            else:
                if self.rest_data:
                    pass  # REST writes as a single start/end time
                elif "discharge_end_time" in self.base.args:
                    # Always write to this as it is the GE default
                    changed_start_end = True
                    entity_discharge_end_time_id = self.base.get_arg("discharge_end_time", indirect=False, index=self.id)
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("discharge_end_time", entity_discharge_end_time_id, new_end)
                        # If the inverter uses hours and minutes then write to these entities too
                    else:
                        self.write_and_poll_value("discharge_end_time", entity_discharge_end_time_id, new_end)

                    if self.inv_charge_time_format == "H M":
                        self.write_and_poll_value("discharge_end_hour", self.base.get_arg("discharge_end_hour", indirect=False, index=self.id), int(new_end[:2]))
                        self.write_and_poll_value("discharge_end_minute", self.base.get_arg("discharge_end_minute", indirect=False, index=self.id), int(new_end[3:5]))
                else:
                    self.log("Warn: Inverter {} unable write discharge end time as neither REST or discharge_end_time are set".format(self.id))

        if ((new_end != old_end) or (new_start != old_start)) and self.inv_time_button_press:
            entity_id = self.base.get_arg("charge_discharge_update_button", indirect=False, index=self.id)
            self.press_and_poll_button(entity_id)

        # Change scheduled discharge enable
        if force_discharge and not old_discharge_enable:
            if not SIMULATE:
                self.write_and_poll_switch("scheduled_discharge_enable", self.base.get_arg("scheduled_discharge_enable", indirect=False, index=self.id), True)
                self.log("Inverter {} Turning on scheduled discharge".format(self.id))
        elif not force_discharge and old_discharge_enable:
            if not SIMULATE:
                self.write_and_poll_switch("scheduled_discharge_enable", self.base.get_arg("scheduled_discharge_enable", indirect=False, index=self.id), False)
                self.log("Inverter {} Turning off scheduled discharge".format(self.id))

        # REST version of writing slot
        if self.rest_data and new_start and new_end and ((new_start != old_start) or (new_end != old_end)):
            changed_start_end = True
            if not SIMULATE:
                self.rest_setDischargeSlot1(new_start, new_end)

        # Force discharge, turn it on after we change the window
        if force_discharge:
            self.adjust_inverter_mode(force_discharge, changed_start_end=changed_start_end)

        # Notify
        if changed_start_end:
            if self.base.set_inverter_notify:
                self.base.call_notify("Predbat: Inverter {} Discharge time slot set to {} - {} at time {}".format(self.id, new_start, new_end, self.base.time_now_str()))

    def disable_charge_window(self, notify=True):
        """
        Disable charge window
        """
        """
        Adjust force discharge on/off and set the time window correctly

        Inverter Class Parameters
        =========================

            Parameter                          Type          Units
            ----------                         ----          -----
            self.charge_enable_time            boole


        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            scheduled_charge_enable            bool
            *charge_start_hour                 int
            *charge_start_minute               int
            *charge_end_hour                   int
            *charge_end_minute                 int
            *charge_discharge_update_button    button

        """
        if SIMULATE:
            old_charge_schedule_enable = self.base.sim_charge_schedule_enable
        else:
            if self.rest_data:
                old_charge_schedule_enable = self.rest_data["Control"]["Enable_Charge_Schedule"]
            else:
                old_charge_schedule_enable = self.base.get_arg("scheduled_charge_enable", "on", index=self.id)

        self.adjust_idle_time(charge_start="00:00:00", charge_end="00:00:00")

        if old_charge_schedule_enable == "on" or old_charge_schedule_enable == "enable":
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_data:
                    self.rest_enableChargeSchedule(False)
                else:
                    self.write_and_poll_switch("scheduled_charge_enable", self.base.get_arg("scheduled_charge_enable", indirect=False, index=self.id), False)
                    # If there's no charge enable switch then we can enable using start and end time
                    if not self.inv_has_charge_enable_time and (self.inv_output_charge_control == "current"):
                        self.enable_charge_discharge_with_time_current("charge", False)
                    else:
                        self.adjust_charge_window(self.base.midnight_utc, self.base.midnight_utc, self.base.minutes_now)

                if self.base.set_inverter_notify and notify:
                    self.base.call_notify("Predbat: Inverter {} Disabled scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = "off"

            self.base.log("Inverter {} Turning off scheduled charge".format(self.id))

        # Updated cached status to disabled
        self.charge_enable_time = False
        self.charge_start_time_minutes = self.base.forecast_minutes
        self.charge_end_time_minutes = self.base.forecast_minutes

    def alt_charge_discharge_enable(self, direction, enable):
        """
        Alternative enable and disable of timed charging for non-GE inverters
        """

        if self.inverter_type == "GS":
            # Solis just has a single switch for both directions
            # Need to check the logic of how this is called if both charging and discharging

            solax_modes = SOLAX_SOLIS_MODES_NEW if self.base.get_arg("solax_modbus_new", True) else SOLAX_SOLIS_MODES

            entity_id = self.base.get_arg("energy_control_switch", indirect=False, index=self.id)
            switch = solax_modes.get(self.base.get_state_wrapper(entity_id), 0)

            if direction == "charge":
                if enable:
                    new_switch = 35
                else:
                    new_switch = 33
            elif direction == "discharge":
                if enable:
                    new_switch = 35
                else:
                    new_switch = 33
            else:
                # ECO
                new_switch = 35

            # Find mode names
            old_mode = {solax_modes[x]: x for x in solax_modes}[switch]
            new_mode = {solax_modes[x]: x for x in solax_modes}[new_switch]

            if new_switch != switch:
                self.base.log(f"Setting Solis Energy Control Switch to {new_switch} {new_mode} from {switch} {old_mode} for {direction} {enable}")
                self.write_and_poll_option(name=entity_id, entity=entity_id, new_value=new_mode)
            else:
                self.base.log(f"Solis Energy Control Switch setting {switch} {new_mode} unchanged for {direction} {enable}")

        # MQTT
        if direction == "charge" and enable:
            self.mqtt_message("set/charge", payload=int(self.battery_rate_max_charge * MINUTE_WATT))
        elif direction == "discharge" and enable:
            self.mqtt_message("set/discharge", payload=int(self.battery_rate_max_discharge * MINUTE_WATT))
        else:
            self.mqtt_message("set/auto", payload="true")

    def mqtt_message(self, topic, payload):
        """
        Send an MQTT message via service
        """
        if self.inv_has_mqtt_api:
            self.base.call_service_wrapper("mqtt/publish", qos=1, retain=True, topic=(self.inv_mqtt_topic + "/" + topic), payload=payload)

    def enable_charge_discharge_with_time_current(self, direction, enable):
        """
        Enable or disable timed charge/discharge
        """
        # To enable we set the current based on the required power
        if enable:
            power = self.base.get_arg(f"{direction}_rate", index=self.id, default=2600.0)
            self.set_current_from_power(direction, power)
        else:
            if self.inv_charge_time_format == "H M":
                # To disable we set both times to 00:00
                for x in ["start", "end"]:
                    for y in ["hour", "minute"]:
                        name = f"{direction}_{x}_{y}"
                        self.write_and_poll_value(name, self.base.get_arg(name, indirect=False, index=self.id), 0)
            else:
                self.set_current_from_power(direction, 0)

    def set_current_from_power(self, direction, power):
        """
        Set the timed charge/discharge current setting by converting power to current
        """
        new_current = round(power / self.battery_voltage, self.inv_current_dp)
        self.write_and_poll_value(f"timed_{direction}_current", self.base.get_arg(f"timed_{direction}_current", indirect=False, index=self.id), new_current, fuzzy=1)

    def call_service_template(self, service, data):
        """
        Call a service template with data
        """
        service_template = self.base.args.get(service, "")
        self.log("Inverter {} Call service template {} = {}".format(self.id, service, service_template))
        service_data = {}
        service_name = ""
        if service_template:
            if isinstance(service_template, str):
                service_name = service
                service_data = data
            else:
                for key in service_template:
                    if key == "service":
                        service_name = service_template[key]
                    else:
                        value = service_template[key]
                        value = self.base.resolve_arg(service_template, value, indirect=False, index=self.id, default="", extra_args=data)
                        if value:
                            service_data[key] = value

            if service_name:
                service_name = service_name.replace(".", "/")
                self.log("Inverter {} Call service {} with data {}".format(self.id, service_name, service_data))
                self.base.call_service_wrapper(service_name, **service_data)
            else:
                self.log("Warn: Inverter {} unable to find service name for {}".format(self.id, service))
        else:
            self.log("Warn: Inverter {} unable to find service template for {}".format(self.id, service))

    def adjust_charge_immediate(self, target_soc):
        """
        Adjust from charging or not charging based on passed target soc
        """
        if self.inv_has_service_api:
            if target_soc > 0:
                service_data = {
                    "device_id": self.base.get_arg("device_id", index=self.id, default=""),
                    "target_soc": target_soc,
                    "power": int(self.battery_rate_max_charge * MINUTE_WATT),
                }
                self.call_service_template("charge_start_service", service_data)
            else:
                service_data = {"device_id": self.base.get_arg("device_id", index=self.id, default="")}
                self.call_service_template("charge_stop_service", service_data)

    def adjust_discharge_immediate(self, target_soc):
        """
        Adjust from discharging or not discharging based on passed target soc
        """
        if self.inv_has_service_api:
            if target_soc > 0:
                service_data = {
                    "device_id": self.base.get_arg("device_id", index=self.id, default=""),
                    "target_soc": target_soc,
                    "power": int(self.battery_rate_max_discharge * MINUTE_WATT),
                }
                self.call_service_template("discharge_start_service", service_data)
            else:
                service_data = {"device_id": self.base.get_arg("device_id", index=self.id, default="")}
                self.call_service_template("charge_stop_service", service_data)

    def adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
        """
        Adjust the charging window times (start and end) in GivTCP

        Inverter Class Parameters
        =========================

            Parameter                          Type          Units
            ----------                         ----          -----
            self.charge_enable_time            boole


        Output Entities:
        ================

            Config arg                         Type          Units
            ----------                         ----          -----
            scheduled_charge_enable            bool
            charge_start_time                  string
            charge_end_time                    string
            *charge_start_hour                 int
            *charge_start_minute               int
            *charge_end_hour                   int
            *charge_end_minute                 int
            *charge_discharge_update_button    button

        """

        if SIMULATE:
            old_start = self.base.sim_charge_start_time
            old_end = self.base.sim_charge_end_time
            old_charge_schedule_enable = self.base.sim_charge_schedule_enable
        else:
            if self.rest_data:
                old_start = self.rest_data["Timeslots"]["Charge_start_time_slot_1"]
                old_end = self.rest_data["Timeslots"]["Charge_end_time_slot_1"]
                old_charge_schedule_enable = self.rest_data["Control"]["Enable_Charge_Schedule"]
            elif "charge_start_time" in self.base.args:
                old_start = self.base.get_arg("charge_start_time", index=self.id)
                old_end = self.base.get_arg("charge_end_time", index=self.id)
                old_charge_schedule_enable = self.base.get_arg("scheduled_charge_enable", "on", index=self.id)
            else:
                self.log("Warn: Inverter {} unable read charge window as neither REST or discharge_start_time".format(self.id))

        # Apply clock skew
        charge_start_time += timedelta(seconds=self.base.inverter_clock_skew_start * 60)
        charge_end_time += timedelta(seconds=self.base.inverter_clock_skew_end * 60)

        # Convert to string
        new_start = charge_start_time.strftime("%H:%M:%S")
        new_end = charge_end_time.strftime("%H:%M:%S")

        # Disable scheduled charge during change of window to avoid a blip in charging if not required
        have_disabled = False
        in_new_window = False

        # Work out window time in minutes from midnight
        new_start_minutes = charge_start_time.hour * 60 + charge_start_time.minute
        new_end_minutes = charge_end_time.hour * 60 + charge_end_time.minute

        # If we are in the new window no need to disable
        if minutes_now >= new_start_minutes and minutes_now < new_end_minutes:
            in_new_window = True

        # Some inverters have an idle time setting
        self.adjust_idle_time(charge_start=new_start, charge_end=new_end)

        # Disable charging if required, for REST no need as we change start and end together anyhow
        if not in_new_window and not self.rest_data and ((new_start != old_start) or (new_end != old_end)) and self.inv_has_charge_enable_time:
            self.disable_charge_window(notify=False)
            have_disabled = True
        if new_start != old_start:
            if SIMULATE:
                self.base.sim_charge_start_time = new_start
                self.base.log("Simulate sim_charge_start_time now {}".format(new_start))
            else:
                if self.rest_data:
                    pass  # REST will be written as start/end together
                elif "charge_start_time" in self.base.args:
                    # Always write to this as it is the GE default
                    entity_id_start = self.base.get_arg("charge_start_time", indirect=False, index=self.id)
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("charge_start_time", entity_id_start, new_start)
                    else:
                        self.write_and_poll_value("charge_start_time", entity_id_start, new_start)

                    if self.inv_charge_time_format == "H M":
                        # If the inverter uses hours and minutes then write to these entities too
                        self.write_and_poll_value("charge_start_hour", self.base.get_arg("charge_start_hour", indirect=False, index=self.id), int(new_start[:2]))
                        self.write_and_poll_value("charge_start_minute", self.base.get_arg("charge_start_minute", indirect=False, index=self.id), int(new_start[3:5]))
                else:
                    self.log("Warn: Inverter {} unable write charge window start as neither REST or charge_start_time are set".format(self.id))

        # Program end slot
        if new_end != old_end:
            if SIMULATE:
                self.base.sim_charge_end_time = new_end
                self.base.log("Simulate sim_charge_end_time now {}".format(new_end))
            else:
                if self.rest_data:
                    pass  # REST will be written as start/end together
                elif "charge_end_time" in self.base.args:
                    # Always write to this as it is the GE default
                    entity_id_end = self.base.get_arg("charge_end_time", indirect=False, index=self.id)
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("charge_end_time", entity_id_end, new_end)
                    else:
                        self.write_and_poll_value("charge_end_time", entity_id_end, new_end)

                    if self.inv_charge_time_format == "H M":
                        self.write_and_poll_value("charge_end_hour", self.base.get_arg("charge_end_hour", indirect=False, index=self.id), int(new_end[:2]))
                        self.write_and_poll_value("charge_end_minute", self.base.get_arg("charge_end_hour", indirect=False, index=self.id), int(new_end[3:5]))
                else:
                    self.log("Warn: Inverter {} unable write charge window end as neither REST, charge_end_hour or charge_end_time are set".format(self.id))

        if new_start != old_start or new_end != old_end:
            if self.rest_data and not SIMULATE:
                self.rest_setChargeSlot1(new_start, new_end)

            # For Solis inverters we also have to press the update_charge_discharge button to send the times to the inverter
            if self.inv_time_button_press:
                entity_id = self.base.get_arg("charge_discharge_update_button", indirect=False, index=self.id)
                self.press_and_poll_button(entity_id)

            if self.base.set_inverter_notify and not SIMULATE:
                self.base.call_notify("Predbat: Inverter {} Charge window change to: {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            self.base.log("Inverter {} Updated start and end charge window to {} - {} (old {} - {})".format(self.id, new_start, new_end, old_start, old_end))

        if old_charge_schedule_enable == "off" or old_charge_schedule_enable == "disable" or have_disabled:
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_data:
                    self.rest_enableChargeSchedule(True)
                elif "scheduled_charge_enable" in self.base.args:
                    self.write_and_poll_switch("scheduled_charge_enable", self.base.get_arg("scheduled_charge_enable", indirect=False, index=self.id), True)
                    if not self.inv_has_charge_enable_time and (self.inv_output_charge_control == "current"):
                        self.enable_charge_discharge_with_time_current("charge", True)
                else:
                    self.log("Warn: Inverter {} unable write charge window enable as neither REST or scheduled_charge_enable are set".format(self.id))

                # Only notify if it's a real change and not a temporary one
                if (old_charge_schedule_enable == "off" or old_charge_schedule_enable == "disable") and self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Enabling scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = "on"

            self.charge_enable_time = True

            if old_charge_schedule_enable == "off" or old_charge_schedule_enable == "disable":
                self.base.log("Inverter {} Turning on scheduled charge".format(self.id))

    def press_and_poll_button(self, entity_id):
        """
        Call a button press service (Solis) and wait for the data to update
        """
        for retry in range(6):
            self.base.call_service_wrapper("button/press", entity_id=entity_id)
            time.sleep(self.inv_write_and_poll_sleep)
            time_pressed = datetime.strptime(self.base.get_state_wrapper(entity_id, refresh=True), TIME_FORMAT_SECONDS)

            if (pytz.timezone("UTC").localize(datetime.now()) - time_pressed).seconds < 10:
                self.base.log(f"Successfully pressed button {entity_id} on Inverter {self.id}")
                return True
        self.base.log(f"Warn: Inverter {self.id} Trying to press {entity_id} didn't complete")
        self.base.record_status(f"Warn: Inverter {self.id} Trying to press {entity_id} didn't complete")
        return False

    def rest_readData(self, api="readData", retry=True):
        """
        Get inverter status

        :param api: The API endpoint to retrieve data from (default is "readData")
        :return: The JSON response containing the inverter status, or None if there was an error
        """
        url = self.rest_api + "/" + api
        try:
            r = requests.get(url)
        except Exception as e:
            self.base.log("Error: Exception raised {}".format(e))
            r = None

        if r and (r.status_code == 200):
            json = r.json()
            if "Control" in json:
                return json
            else:
                if retry:
                    # If this is the first call in error then try to re-read the data
                    return self.rest_runAll()
                else:
                    self.base.log("Warn: Inverter {} read bad REST data from {} - REST will be disabled".format(self.id, url))
                    self.base.record_status("Inverter {} read bad REST data from {} - REST will be disabled".format(self.id, url), had_errors=True)
                    return None
        else:
            self.base.log("Warn: Inverter {} unable to read REST data from {} - REST will be disabled".format(self.id, url))
            self.base.record_status("Inverter {} unable to read REST data from {} - REST will be disabled".format(self.id, url), had_errors=True)
            return None

    def rest_runAll(self, old_data=None):
        """
        Updated and get inverter status
        """
        new_data = self.rest_readData(api="runAll", retry=False)
        if new_data:
            return new_data
        else:
            return old_data

    def rest_setChargeTarget(self, target):
        """
        Configure charge target % via REST
        """
        target = int(target)
        url = self.rest_api + "/setChargeTarget"
        data = {"chargeToPercent": target}
        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if float(self.rest_data["Control"]["Target_SOC"]) == target:
                self.base.log("Inverter {} charge target {} via REST successful on retry {}".format(self.id, target, retry))
                return True

        self.base.log("Warn: Inverter {} charge target {} via REST failed".format(self.id, target))
        self.base.record_status("Warn: Inverter {} REST failed to setChargeTarget".format(self.id), had_errors=True)
        return False

    def rest_setChargeRate(self, rate):
        """
        Configure charge target % via REST
        """
        rate = int(rate)
        url = self.rest_api + "/setChargeRate"
        data = {"chargeRate": rate}
        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new = int(self.rest_data["Control"]["Battery_Charge_Rate"])
            if abs(new - rate) < (self.battery_rate_max_charge * MINUTE_WATT / 12):
                self.base.log("Inverter {} set charge rate {} via REST successful on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("Warn: Inverter {} set charge rate {} via REST failed got {}".format(self.id, rate, self.rest_data["Control"]["Battery_Charge_Rate"]))
        self.base.record_status("Warn: Inverter {} REST failed to setChargeRate".format(self.id), had_errors=True)
        return False

    def rest_setDischargeRate(self, rate):
        """
        Configure charge target % via REST
        """
        rate = int(rate)
        url = self.rest_api + "/setDischargeRate"
        data = {"dischargeRate": rate}
        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new = int(self.rest_data["Control"]["Battery_Discharge_Rate"])
            if abs(new - rate) < (self.battery_rate_max_discharge * MINUTE_WATT / 25):
                self.base.log("Inverter {} set discharge rate {} via REST successful on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("Warn: Inverter {} set discharge rate {} via REST failed got {}".format(self.id, rate, self.rest_data["Control"]["Battery_Discharge_Rate"]))
        self.base.record_status(
            "Warn: Inverter {} REST failed to setDischargeRate to {} got {}".format(self.id, rate, self.rest_data["Control"]["Battery_Discharge_Rate"]), had_errors=True
        )
        return False

    def rest_setBatteryMode(self, inverter_mode):
        """
        Configure invert mode via REST
        """
        url = self.rest_api + "/setBatteryMode"
        data = {"mode": inverter_mode}

        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if inverter_mode == self.rest_data["Control"]["Mode"]:
                self.base.log("Set inverter {} mode {} via REST successful on retry {}".format(self.id, inverter_mode, retry))
                return True

        self.base.log("Warn: Set inverter {} mode {} via REST failed".format(self.id, inverter_mode))
        self.base.record_status("Warn: Inverter {} REST failed to setBatteryMode".format(self.id), had_errors=True)
        return False

    def rest_setReserve(self, target):
        """
        Configure reserve % via REST
        """
        target = int(target)
        result = target
        url = self.rest_api + "/setBatteryReserve"
        data = {"reservePercent": target}
        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            result = int(float(self.rest_data["Control"]["Battery_Power_Reserve"]))
            if result == target:
                self.base.log("Set inverter {} reserve {} via REST successful on retry {}".format(self.id, target, retry))
                return True

        self.base.log("Warn: Set inverter {} reserve {} via REST failed on retry {} got {}".format(self.id, target, retry, result))
        self.base.record_status("Warn: Inverter {} REST failed to setReserve to {} got {}".format(self.id, target, result), had_errors=True)
        return False

    def rest_enableChargeSchedule(self, enable):
        """
        Configure reserve % via REST
        """
        url = self.rest_api + "/enableChargeSchedule"
        data = {"state": "enable" if enable else "disable"}

        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new_value = self.rest_data["Control"]["Enable_Charge_Schedule"]
            if isinstance(new_value, str):
                if new_value.lower() in ["enable", "on", "true"]:
                    new_value = True
                else:
                    new_value = False
            if new_value == enable:
                self.base.log("Set inverter {} charge schedule {} via REST successful on retry {}".format(self.id, enable, retry))
                return True

        self.base.log("Warn: Set inverter {} charge schedule {} via REST failed got {}".format(self.id, enable, self.rest_data["Control"]["Enable_Charge_Schedule"]))
        self.base.record_status("Warn: Inverter {} REST failed to enableChargeSchedule".format(self.id), had_errors=True)
        return False

    def rest_setChargeSlot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        url = self.rest_api + "/setChargeSlot1"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if self.rest_data["Timeslots"]["Charge_start_time_slot_1"] == start and self.rest_data["Timeslots"]["Charge_end_time_slot_1"] == finish:
                self.base.log("Inverter {} set charge slot 1 {} via REST successful after retry {}".format(self.id, data, retry))
                return True

        self.base.log("Warn: Inverter {} set charge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn: Inverter {} REST failed to setChargeSlot1".format(self.id), had_errors=True)
        return False

    def rest_setDischargeSlot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        url = self.rest_api + "/setDischargeSlot1"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if self.rest_data["Timeslots"]["Discharge_start_time_slot_1"] == start and self.rest_data["Timeslots"]["Discharge_end_time_slot_1"] == finish:
                self.base.log("Inverter {} Set discharge slot 1 {} via REST successful after retry {}".format(self.id, data, retry))
                return True

        self.base.log("Warn: Inverter {} Set discharge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn: Inverter {} REST failed to setDischargeSlot1".format(self.id), had_errors=True)
        return False
