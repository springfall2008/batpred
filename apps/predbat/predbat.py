"""
Battery Prediction app
see Readme for information
"""
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta
import math
import re
import time
import pytz
import requests
import copy
import appdaemon.plugins.hass.hassapi as hass
import adbase as ad
import os
import yaml

THIS_VERSION = "v7.14.30"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
TIME_FORMAT_SOLIS = "%Y-%m-%d %H:%M:%S"
PREDICT_STEP = 5
RUN_EVERY = 5
CONFIG_ROOTS = ["/config", "/conf", "/homeassistant"]

# 240v x 100 amps x 3 phases / 1000 to kW / 60 minutes in an hour is the maximum kWh in a 1 minute period
MAX_INCREMENT = 240 * 100 * 3 / 1000 / 60

SIMULATE = False  # Debug option, when set don't write to entities but simulate each 30 min period
SIMULATE_LENGTH = 23 * 60  # How many periods to simulate, set to 0 for just current
INVERTER_TEST = False  # Run inverter control self test

"""
Create an array of times
"""
OPTIONS_TIME = []
BASE_TIME = datetime.strptime("00:00:00", "%H:%M:%S")
for minute in range(0, 24 * 60, 5):
    timeobj = BASE_TIME + timedelta(seconds=minute * 60)
    timestr = timeobj.strftime("%H:%M:%S")
    OPTIONS_TIME.append(timestr)

INVERTER_TYPES = {"GE": "GivEnergy", "GS": "Ginlong Solis", "SE": "SolarEdge", "SX4": "Solax Gen4 (Modbus Power Control)", "SF": "Sofar HYD"}

# Inverter modes
PREDBAT_MODE_OPTIONS = ["Monitor", "Control SOC only", "Control charge", "Control charge & discharge"]
PREDBAT_MODE_MONITOR = 0
PREDBAT_MODE_CONTROL_SOC = 1
PREDBAT_MODE_CONTROL_CHARGE = 2
PREDBAT_MODE_CONTROL_CHARGEDISCHARGE = 3

# Predbat update options
PREDBAT_UPDATE_OPTIONS = [THIS_VERSION + " Loading..."]

# Configuration options inside HA
CONFIG_ITEMS = [
    {
        "name": "version",
        "friendly_name": "Predbat Core Update",
        "type": "update",
        "title": "Predbat",
        "installed_version": THIS_VERSION,
        "release_url": "https://github.com/springfall2008/batpred/releases/tag/" + THIS_VERSION,
        "entity_picture": "https://user-images.githubusercontent.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png",
    },
    {"name": "expert_mode", "friendly_name": "Expert Mode", "type": "switch", "default": True},
    {
        "name": "pv_metric10_weight",
        "friendly_name": "Metric 10 Weight",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:percent",
        "default": 0.15,
    },
    {
        "name": "pv_scaling",
        "friendly_name": "PV Scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.01,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "default": 1.0,
    },
    {
        "name": "load_scaling",
        "friendly_name": "Load Scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.01,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "default": 1.0,
    },
    {
        "name": "load_scaling10",
        "friendly_name": "Load Scaling PV10%",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.01,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "default": 1.1,
    },
    {
        "name": "battery_rate_max_scaling",
        "friendly_name": "Battery rate max scaling",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "default": 1.0,
    },
    {
        "name": "battery_loss",
        "friendly_name": "Battery loss charge ",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:call-split",
        "default": 0.03,
    },
    {
        "name": "battery_loss_discharge",
        "friendly_name": "Battery loss discharge",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:call-split",
        "default": 0.03,
    },
    {
        "name": "inverter_loss",
        "friendly_name": "Inverter Loss",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:call-split",
        "default": 0.04,
    },
    {"name": "inverter_hybrid", "friendly_name": "Inverter Hybrid", "type": "switch", "default": True},
    {"name": "inverter_soc_reset", "friendly_name": "Inverter SOC Reset", "type": "switch", "enable": "expert_mode", "default": False},
    {"name": "battery_capacity_nominal", "friendly_name": "Use the Battery Capacity Nominal size", "type": "switch", "enable": "expert_mode", "default": False},
    {
        "name": "car_charging_energy_scale",
        "friendly_name": "Car charging energy scale",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:multiplication",
        "default": 1.0,
    },
    {
        "name": "car_charging_threshold",
        "friendly_name": "Car charging threshold",
        "type": "input_number",
        "min": 4,
        "max": 8.5,
        "step": 0.10,
        "unit": "kW",
        "icon": "mdi:ev-station",
        "default": 6.0,
    },
    {
        "name": "car_charging_rate",
        "friendly_name": "Car charging rate",
        "type": "input_number",
        "min": 1,
        "max": 8.5,
        "step": 0.10,
        "unit": "kW",
        "icon": "mdi:ev-station",
        "default": 7.4,
    },
    {
        "name": "car_charging_loss",
        "friendly_name": "Car charging loss",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "fraction",
        "icon": "mdi:call-split",
        "default": 0.08,
    },
    {
        "name": "best_soc_min",
        "friendly_name": "Best SOC Min",
        "type": "input_number",
        "min": 0,
        "max": 30.0,
        "step": 0.10,
        "unit": "kWh",
        "icon": "mdi:battery-50",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "best_soc_max",
        "friendly_name": "Best SOC Max",
        "type": "input_number",
        "min": 0,
        "max": 30.0,
        "step": 0.10,
        "unit": "kWh",
        "icon": "mdi:battery-50",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "best_soc_keep",
        "friendly_name": "Best SOC Keep",
        "type": "input_number",
        "min": 0,
        "max": 30.0,
        "step": 0.10,
        "unit": "kWh",
        "icon": "mdi:battery-50",
        "default": 0.5,
    },
    {
        "name": "metric_min_improvement",
        "friendly_name": "Metric Min Improvement",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p",
        "icon": "mdi:currency-usd",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "metric_min_improvement_discharge",
        "friendly_name": "Metric Min Improvement Discharge",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p",
        "icon": "mdi:currency-usd",
        "enable": "expert_mode",
        "default": 0.1,
    },
    {
        "name": "metric_battery_cycle",
        "friendly_name": "Metric Battery Cycle Cost",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p/kWh",
        "icon": "mdi:currency-usd",
        "enable": "expert_mode",
        "default": 2.0,
    },
    {
        "name": "metric_battery_value_scaling",
        "friendly_name": "Metric Battery Value Scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.1,
        "unit": "fraction",
        "icon": "mdi:multiplication",
        "enable": "expert_mode",
        "default": 1.0,
    },
    {
        "name": "metric_future_rate_offset_import",
        "friendly_name": "Metric Future Rate Offset Import",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p/kWh",
        "icon": "mdi:currency-usd",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "metric_future_rate_offset_export",
        "friendly_name": "Metric Future Rate Offset Export",
        "type": "input_number",
        "min": -50,
        "max": 50.0,
        "step": 0.1,
        "unit": "p/kWh",
        "icon": "mdi:currency-usd",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "metric_inday_adjust_damping",
        "friendly_name": "In-day adjustment damping factor",
        "type": "input_number",
        "min": 0.5,
        "max": 2.0,
        "step": 0.05,
        "unit": "fraction",
        "icon": "mdi:call-split",
        "enable": "expert_mode",
        "default": 0.95,
    },
    {
        "name": "metric_cloud_enable",
        "friendly_name": "Enable Cloud Model",
        "type": "switch",
        "default": True,
        "enable": "expert_mode",
    },
    {
        "name": "set_reserve_min",
        "friendly_name": "Set Reserve Min",
        "type": "input_number",
        "min": 4,
        "max": 100,
        "step": 1,
        "unit": "%",
        "icon": "mdi:percent",
        "default": 4.0,
        "reset_inverter": True,
    },
    {
        "name": "rate_low_threshold",
        "friendly_name": "Rate Low Threshold",
        "type": "input_number",
        "min": 0.00,
        "max": 2.00,
        "step": 0.05,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "rate_high_threshold",
        "friendly_name": "Rate High Threshold",
        "type": "input_number",
        "min": 0.00,
        "max": 2.00,
        "step": 0.05,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {"name": "car_charging_hold", "friendly_name": "Car charging hold", "type": "switch", "default": True, "reset_inverter": True},
    {"name": "octopus_intelligent_charging", "friendly_name": "Octopus Intelligent Charging", "type": "switch", "default": True},
    {
        "name": "octopus_intelligent_ignore_unplugged",
        "friendly_name": "Ignore Intelligent slots when car is unplugged",
        "type": "switch",
        "default": False,
        "enable": "expert_mode",
    },
    {"name": "car_charging_plan_smart", "friendly_name": "Car Charging Plan Smart", "type": "switch", "default": False},
    {"name": "car_charging_from_battery", "friendly_name": "Allow car to charge from battery", "type": "switch", "default": False, "reset_inverter": True},
    {"name": "calculate_discharge_oncharge", "friendly_name": "Calculate Discharge on charge slots", "type": "switch", "enable": "expert_mode", "default": True},
    {"name": "calculate_fast_plan", "friendly_name": "Calculate plan faster (less accurate)", "type": "switch", "enable": "expert_mode", "default": False},
    {"name": "calculate_second_pass", "friendly_name": "Calculate full second pass (slower)", "type": "switch", "enable": "expert_mode", "default": False},
    {"name": "calculate_tweak_plan", "friendly_name": "Calculate tweak second pass", "type": "switch", "enable": "expert_mode", "default": False},
    {"name": "calculate_inday_adjustment", "friendly_name": "Calculate in-day adjustment", "type": "switch", "enable": "expert_mode", "default": True},
    {
        "name": "calculate_plan_every",
        "friendly_name": "Calculate plan every N minutes",
        "type": "input_number",
        "min": 5,
        "max": 60,
        "step": 5,
        "unit": "kWh",
        "icon": "mdi:clock-end",
        "enable": "expert_mode",
        "default": 10,
    },
    {"name": "combine_charge_slots", "friendly_name": "Combine Charge Slots", "type": "switch", "default": True},
    {"name": "combine_discharge_slots", "friendly_name": "Combine Discharge Slots", "type": "switch", "enable": "expert_mode", "default": False},
    {"name": "set_status_notify", "friendly_name": "Set Status Notify", "type": "switch", "default": True},
    {"name": "set_inverter_notify", "friendly_name": "Set Inverter Notify", "type": "switch", "default": False},
    {"name": "set_charge_freeze", "friendly_name": "Set Charge Freeze", "type": "switch", "enable": "expert_mode", "default": True, "reset_inverter": True},
    {"name": "set_charge_low_power", "friendly_name": "Set Charge Low Power Mode", "type": "switch", "default": False, "reset_inverter": True},
    {"name": "set_reserve_enable", "friendly_name": "Set Reserve Enable", "type": "switch", "enable": "expert_mode", "default": True, "reset_inverter": True},
    {"name": "set_discharge_freeze_only", "friendly_name": "Set Discharge Freeze Only", "type": "switch", "enable": "expert_mode", "default": False, "reset_inverter": True},
    {"name": "set_discharge_during_charge", "friendly_name": "Set Discharge During Charge", "type": "switch", "default": True},
    {"name": "set_read_only", "friendly_name": "Read Only mode", "type": "switch", "default": False, "reset_inverter_force": True},
    {"name": "balance_inverters_enable", "friendly_name": "Balance Inverters Enable (Beta)", "type": "switch", "default": False},
    {"name": "balance_inverters_charge", "friendly_name": "Balance Inverters for charging", "type": "switch", "enable": "balance_inverters_enable", "default": True},
    {"name": "balance_inverters_discharge", "friendly_name": "Balance Inverters for discharge", "type": "switch", "enable": "balance_inverters_enable", "default": True},
    {"name": "balance_inverters_crosscharge", "friendly_name": "Balance Inverters for cross-charging", "type": "switch", "enable": "balance_inverters_enable", "default": True},
    {
        "name": "balance_inverters_threshold_charge",
        "friendly_name": "Balance Inverters threshold charge",
        "type": "input_number",
        "min": 1,
        "max": 20,
        "step": 1,
        "unit": "%",
        "icon": "mdi:percent",
        "enable": "balance_inverters_enable",
        "default": 1.0,
    },
    {
        "name": "balance_inverters_threshold_discharge",
        "friendly_name": "Balance Inverters threshold discharge",
        "type": "input_number",
        "min": 1,
        "max": 20,
        "step": 1,
        "unit": "%",
        "icon": "mdi:percent",
        "enable": "balance_inverters_enable",
        "default": 1.0,
    },
    {"name": "debug_enable", "friendly_name": "Debug Enable", "type": "switch", "icon": "mdi:bug-outline", "default": False},
    {
        "name": "car_charging_plan_time",
        "friendly_name": "Car charging planned ready time",
        "type": "select",
        "options": OPTIONS_TIME,
        "icon": "mdi:clock-end",
        "default": "07:00:00",
    },
    {
        "name": "mode",
        "friendly_name": "Predbat mode",
        "type": "select",
        "options": PREDBAT_MODE_OPTIONS,
        "icon": "mdi:state-machine",
        "default": PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGEDISCHARGE],
        "reset_inverter_force": True,
    },
    {
        "name": "update",
        "friendly_name": "Predbat update",
        "type": "select",
        "options": PREDBAT_UPDATE_OPTIONS,
        "icon": "mdi:state-machine",
        "default": None,
    },
    {"name": "auto_update", "friendly_name": "Predbat automatic update enable", "type": "switch", "default": False},
    {"name": "load_filter_modal", "friendly_name": "Apply modal filter historical load", "type": "switch", "enable": "expert_mode", "default": True},
    {"name": "iboost_enable", "friendly_name": "IBoost enable", "type": "switch", "default": False},
    {"name": "iboost_solar", "friendly_name": "IBoost on solar power", "type": "switch", "default": True},
    {"name": "iboost_gas", "friendly_name": "IBoost when cheaper than gas", "type": "switch", "default": False},
    {"name": "iboost_charging", "friendly_name": "IBoost when battery charging", "type": "switch", "default": False},
    {
        "name": "iboost_gas_scale",
        "friendly_name": "IBoost gas price scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.1,
        "unit": "multiple",
        "icon": "mdi:multiplication",
        "enable": "iboost_enable",
        "default": 1.0,
    },
    {
        "name": "iboost_max_energy",
        "friendly_name": "IBoost max energy",
        "type": "input_number",
        "min": 0,
        "max": 5,
        "step": 0.1,
        "unit": "kWh",
        "enable": "iboost_enable",
        "default": 3.0,
    },
    {"name": "iboost_today", "friendly_name": "IBoost today", "type": "input_number", "min": 0, "max": 5, "step": 0.1, "unit": "kWh", "enable": "iboost_enable", "default": 0.0},
    {
        "name": "iboost_max_power",
        "friendly_name": "IBoost max power",
        "type": "input_number",
        "min": 0,
        "max": 3500,
        "step": 100,
        "unit": "w",
        "enable": "iboost_enable",
        "default": 2400,
    },
    {
        "name": "iboost_min_power",
        "friendly_name": "IBoost min power",
        "type": "input_number",
        "min": 0,
        "max": 3500,
        "step": 100,
        "unit": "w",
        "enable": "iboost_enable",
        "default": 500,
    },
    {
        "name": "iboost_min_soc",
        "friendly_name": "IBoost min soc",
        "type": "input_number",
        "min": 0,
        "max": 100,
        "step": 5,
        "unit": "%",
        "icon": "mdi:percent",
        "enable": "iboost_enable",
        "default": 0.0,
    },
    {
        "name": "holiday_days_left",
        "friendly_name": "Holiday days left",
        "type": "input_number",
        "min": 0,
        "max": 28,
        "step": 1,
        "unit": "days",
        "icon": "mdi:clock-end",
        "default": 0,
    },
    {
        "name": "forecast_plan_hours",
        "friendly_name": "Plan forecast hours",
        "type": "input_number",
        "min": 8,
        "max": 96,
        "step": 1,
        "unit": "hours",
        "icon": "mdi:clock-end",
        "enable": "expert_mode",
        "default": 24,
    },
    {"name": "plan_debug", "friendly_name": "HTML Plan Debug", "type": "switch", "default": False, "enable": "expert_mode"},
]

"""
GE Inverters are the default but not all inverters have the same parameters so this constant
maps the parameters that are different between brands.

The approach is to attempt to mimic the GE model with dummy entities in HA so that predbat GE
code can be used with minimal modification.
"""
INVERTER_DEF = {
    "GE": {
        "has_rest_api": True,
        "has_mqtt_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "kWh",
        "num_load_entities": 1,
        "has_ge_inverter_mode": True,
        "time_button_press": False,
        "clock_time_format": "%H:%M:%S",
        "write_and_poll_sleep": 10,
        "has_time_window": True,
        "support_discharge_freeze": True,
    },
    "GS": {
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "current",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": True,
        "charge_time_format": "H M",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 2,
        "has_ge_inverter_mode": False,
        "time_button_press": True,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": True,
        "support_discharge_freeze": False,
    },
    "SX4": {
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": True,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_discharge_freeze": False,
    },
    "SF": {
        "has_rest_api": False,
        "has_mqtt_api": True,
        "output_charge_control": "none",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_discharge_freeze": False,
    },
}


SOLAX_SOLIS_MODES = {
    "Selfuse - No Grid Charging": 1,
    "Timed Charge/Discharge - No Grid Charging": 3,
    "Selfuse": 33,
    "Timed Charge/Discharge": 35,
    "Off-Grid Mode": 37,
    "Battery Awaken": 41,
    "Battery Awaken + Timed Charge/Discharge": 43,
    "Backup/Reserve": 51,
}


class Inverter:
    def self_test(self, minutes_now):
        self.base.log("======= INVERTER CONTROL SELF TEST START - REST={} ========".format(self.rest_api))
        self.adjust_battery_target(99)
        self.adjust_battery_target(100)
        self.adjust_reserve(100)
        self.adjust_reserve(6)
        self.adjust_reserve(4)
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

        self.inverter_type = self.base.get_arg("inverter_type", "GE", indirect=False)

        # Load inverter brand definitions
        self.reserve_max = self.base.get_arg("inverter_reserve_max", 100)
        self.inv_has_rest_api = INVERTER_DEF[self.inverter_type]["has_rest_api"]
        self.inv_has_mqtt_api = INVERTER_DEF[self.inverter_type]["has_mqtt_api"]
        self.inv_mqtt_topic = self.base.get_arg("mqtt_topic", "Sofar2mqtt")
        self.inv_output_charge_control = INVERTER_DEF[self.inverter_type]["output_charge_control"]
        self.inv_has_charge_enable_time = INVERTER_DEF[self.inverter_type]["has_charge_enable_time"]
        self.inv_has_discharge_enable_time = INVERTER_DEF[self.inverter_type]["has_discharge_enable_time"]
        self.inv_has_target_soc = INVERTER_DEF[self.inverter_type]["has_target_soc"]
        self.inv_has_reserve_soc = INVERTER_DEF[self.inverter_type]["has_reserve_soc"]
        self.inv_charge_time_format = INVERTER_DEF[self.inverter_type]["charge_time_format"]
        self.inv_charge_time_entity_is_option = INVERTER_DEF[self.inverter_type]["charge_time_entity_is_option"]
        self.inv_clock_time_format = INVERTER_DEF[self.inverter_type]["clock_time_format"]
        self.inv_soc_units = INVERTER_DEF[self.inverter_type]["soc_units"]
        self.inv_time_button_press = INVERTER_DEF[self.inverter_type]["time_button_press"]
        self.inv_support_discharge_freeze = INVERTER_DEF[self.inverter_type]["support_discharge_freeze"]
        self.inv_has_ge_inverter_mode = INVERTER_DEF[self.inverter_type]["has_ge_inverter_mode"]
        self.inv_num_load_entities = INVERTER_DEF[self.inverter_type]["num_load_entities"]
        self.inv_write_and_poll_sleep = INVERTER_DEF[self.inverter_type]["write_and_poll_sleep"]

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

        # Battery size, charge and discharge rates
        ivtime = None
        if self.rest_data and ("Invertor_Details" in self.rest_data):
            idetails = self.rest_data["Invertor_Details"]
            self.soc_max = float(idetails["Battery_Capacity_kWh"])
            self.nominal_capacity = self.soc_max
            if "raw" in self.rest_data:
                self.nominal_capacity = (
                    float(self.rest_data["raw"]["invertor"]["battery_nominal_capacity"]) / 19.53125
                )  # XXX: Where does 19.53125 come from? I back calculated but why that number...
                if self.base.battery_capacity_nominal:
                    if abs(self.soc_max - self.nominal_capacity) > 1.0:
                        # XXX: Weird workaround for battery reporting wrong capacity issue
                        self.base.log("WARN: REST data reports Battery Capacity kWh as {} but nominal indicates {} - using nominal".format(self.soc_max, self.nominal_capacity))
                    self.soc_max = self.nominal_capacity
            self.soc_max *= self.base.battery_scaling

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
            self.soc_max = self.base.get_arg("soc_max", default=10.0, index=self.id) * self.base.battery_scaling
            self.nominal_capacity = self.soc_max

            self.battery_voltage = 52.0
            if "battery_voltage" in self.base.args:
                self.base.get_arg("battery_voltage", index=self.id, default=52.0)

            if self.inverter_type == "GE":
                self.battery_rate_max_raw = self.base.get_arg("charge_rate", attribute="max", index=self.id, default=2600.0)
            elif "battery_rate_max" in self.base.args:
                self.battery_rate_max_raw = self.base.get_arg("battery_rate_max", index=self.id, default=2600.0)
            else:
                self.battery_rate_max_raw = 2600.0

            ivtime = self.base.get_arg("inverter_time", index=self.id, default=None)

        # Battery can not be zero size
        if self.soc_max <= 0:
            self.base.log("ERROR: Reported battery size from REST is {}, but it must be >0".format(self.soc_max))
            raise ValueError

        # Battery rate max charge, discharge (all converted to kW/min)
        self.battery_rate_max_charge = min(self.base.get_arg("inverter_limit_charge", self.battery_rate_max_raw, index=self.id), self.battery_rate_max_raw) / 60.0 / 1000.0
        self.battery_rate_max_discharge = min(self.base.get_arg("inverter_limit_discharge", self.battery_rate_max_raw, index=self.id), self.battery_rate_max_raw) / 60.0 / 1000.0
        self.battery_rate_max_charge_scaled = self.battery_rate_max_charge * self.base.battery_rate_max_scaling
        self.battery_rate_max_discharge_scaled = self.battery_rate_max_discharge * self.base.battery_rate_max_scaling
        self.battery_rate_min = min(self.base.get_arg("inverter_battery_rate_min", 0, index=self.id), self.battery_rate_max_raw) / 60.0 / 1000.0

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

        # Check inverter time and confirm skew
        if self.inverter_time:
            tdiff = self.inverter_time - self.base.now_utc
            tdiff = self.base.dp2(tdiff.seconds / 60 + tdiff.days * 60 * 24)
            if not quiet:
                self.base.log("Invertor time {} AppDaemon time {} difference {} minutes".format(self.inverter_time, self.base.now_utc, tdiff))
            if abs(tdiff) >= 5:
                self.base.log(
                    "WARN: Invertor time is {} AppDaemon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDaemon time zone".format(
                        self.inverter_time, self.base.now_utc, tdiff
                    )
                )
                self.base.record_status(
                    "Invertor time is {} AppDaemon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDaemon time zone".format(
                        self.inverter_time, self.base.now_utc, tdiff
                    ),
                    had_errors=True,
                )

        # Get current reserve value
        if self.rest_data:
            self.reserve_percent_current = float(self.rest_data["Control"]["Battery_Power_Reserve"])
        else:
            self.reserve_percent_current = max(self.base.get_arg("reserve", default=0.0, index=self.id), self.base.get_arg("battery_min_soc", default=4.0, index=self.id))
        self.reserve_current = self.base.dp2(self.soc_max * self.reserve_percent_current / 100.0)

        # Get the expected minimum reserve value
        battery_min_soc = self.base.get_arg("battery_min_soc", default=4.0, index=self.id)
        self.reserve_min = self.base.get_arg("set_reserve_min", 4.0)
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
            self.inverter_limit = self.base.get_arg("inverter_limit", self.inverter_limit, index=self.id) / (1000 * 60.0)
        if "export_limit" in self.base.args:
            self.export_limit = self.base.get_arg("export_limit", self.inverter_limit, index=self.id) / (1000 * 60.0)
        # Can't export more than the inverter limit
        self.export_limit = min(self.export_limit, self.inverter_limit)

        # Log inverter details
        if not quiet:
            self.base.log(
                "New Inverter {} with soc_max {} kWh nominal_capacity {} kWh battery rate raw {} w charge rate {} kW discharge rate {} kW battery_rate_min {} w ac limit {} kW export limit {} kW reserve {} % current_reserve {} %".format(
                    self.id,
                    self.base.dp2(self.soc_max),
                    self.base.dp2(self.nominal_capacity),
                    self.base.dp2(self.battery_rate_max_raw),
                    self.base.dp2(self.battery_rate_max_charge * 60.0),
                    self.base.dp2(self.battery_rate_max_discharge * 60.0),
                    self.base.dp2(self.battery_rate_min * 60.0 * 1000.0),
                    self.base.dp2(self.inverter_limit * 60),
                    self.base.dp2(self.export_limit * 60),
                    self.reserve_percent,
                    self.reserve_percent_current,
                )
            )

        # Create some dummy entities if PredBat expects them but they don't exist for this Inverter Type:
        # Args are also set for these so that no entries are needed for the dummies in the config file

        if not self.inv_has_charge_enable_time:
            self.base.args["scheduled_charge_enable"] = self.create_entity("scheduled_charge_enable", False)

        if not self.inv_has_discharge_enable_time:
            self.base.args["scheduled_discharge_enable"] = self.create_entity("scheduled_discharge_enable", False)

        if not self.inv_has_reserve_soc:
            self.base.args["reserve"] = self.create_entity("reserve", self.reserve, device_class="battery", uom="%")

        if not self.inv_has_target_soc:
            self.base.args["charge_limit"] = self.create_entity("charge_limit", 100, device_class="battery", uom="%")

        if self.inv_output_charge_control != "power":
            self.base.args["charge_rate"] = self.create_entity("charge_rate", int(self.battery_rate_max_charge * 60 * 1000), uom="W", device_class="power")
            self.base.args["discharge_rate"] = self.create_entity("discharge_rate", int(self.battery_rate_max_discharge * 60 * 1000), uom="W", device_class="power")

        if not self.inv_has_ge_inverter_mode:
            self.base.args["inverter_mode"] = self.create_entity("inverter_mode", "Eco")

        if self.inv_charge_time_format != "HH:MM:SS":
            for x in ["charge", "discharge"]:
                for y in ["start", "end"]:
                    self.base.args[f"{x}_{y}_time"] = self.create_entity(f"{x}_{y}_time", "23:59:00")

    def create_entity(self, entity_name, value, uom=None, device_class="None"):
        """
        Create dummy entities required by non GE inverters to mimic GE behaviour
        """
        if "prefix" in self.base.args:
            prefix = self.base.get_arg("prefix", indirect=False)
        else:
            prefix = "prefix"

        entity_id = f"sensor.{prefix}_{self.inverter_type}_{entity_name}"

        if not self.base.entity_exists(entity_id):
            attributes = {
                "state_class": "measurement",
            }

            if uom is not None:
                attributes["unit_of_measurement"] = uom
            if device_class is not None:
                attributes["device_class"] = device_class

            entity = self.base.get_entity(entity_id)
            entity.set_state(state=value, attributes=attributes)
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
            self.charge_rate_now = self.rest_data["Control"]["Battery_Charge_Rate"] / 1000.0 / 60.0
            self.discharge_rate_now = self.rest_data["Control"]["Battery_Discharge_Rate"] / 1000.0 / 60.0
        else:
            self.charge_enable_time = self.base.get_arg("scheduled_charge_enable", "on", index=self.id) == "on"
            self.discharge_enable_time = self.base.get_arg("scheduled_discharge_enable", "off", index=self.id) == "on"
            self.charge_rate_now = self.base.get_arg("charge_rate", index=self.id, default=2600.0) / 1000.0 / 60.0
            self.discharge_rate_now = self.base.get_arg("discharge_rate", index=self.id, default=2600.0) / 1000.0 / 60.0

        # Scale charge and discharge rates with battery scaling
        self.charge_rate_now = max(self.charge_rate_now * self.base.battery_rate_max_scaling, self.battery_rate_min)
        self.discharge_rate_now = max(self.discharge_rate_now * self.base.battery_rate_max_scaling, self.battery_rate_min)

        if SIMULATE:
            self.soc_kw = self.base.sim_soc_kw
        else:
            if self.rest_data:
                self.soc_kw = self.rest_data["Power"]["Power"]["SOC_kWh"] * self.base.battery_scaling
            else:
                if "soc_percent" in self.base.args:
                    self.soc_kw = self.base.get_arg("soc_percent", default=0.0, index=self.id) * self.soc_max * self.base.battery_scaling / 100.0
                else:
                    self.soc_kw = self.base.get_arg("soc_kw", default=0.0, index=self.id) * self.base.battery_scaling

        if self.soc_max <= 0.0:
            self.soc_percent = 0
        else:
            self.soc_percent = round((self.soc_kw / self.soc_max) * 100.0)

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
                    self.charge_rate_now * 60 * 1000,
                    self.discharge_rate_now * 60 * 1000.0,
                    self.battery_power,
                    self.battery_voltage,
                )
            )

        # If the battery is being charged then find the charge window
        if self.charge_enable_time:
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
                    self.log("WARN: Inverter {} unable to read charge window time as neither REST, charge_start_time or charge_start_hour are set".format(self.id))

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
                self.base.log("Inverter {} Charge settings: timed charged is disabled, power {} kW".format(self.id, self.charge_rate_now * 60.0))

        # Construct discharge window from GivTCP settings
        self.discharge_window = []

        if self.rest_data:
            discharge_start = datetime.strptime(self.rest_data["Timeslots"]["Discharge_start_time_slot_1"], "%H:%M:%S")
            discharge_end = datetime.strptime(self.rest_data["Timeslots"]["Discharge_end_time_slot_1"], "%H:%M:%S")
        elif "discharge_start_time" in self.base.args:
            discharge_start = datetime.strptime(self.base.get_arg("discharge_start_time", index=self.id), "%H:%M:%S")
            discharge_end = datetime.strptime(self.base.get_arg("discharge_end_time", index=self.id), "%H:%M:%S")
        else:
            self.base.log("WARN: Inverter {} unable to read Discharge window as neither REST or discharge_start_time are set".format(self.id))

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
            self.discharge_limits = [0.0 for i in range(0, len(self.discharge_window))]
        else:
            self.discharge_limits = [100.0 for i in range(0, len(self.discharge_window))]

        if not quiet:
            self.base.log("Inverter {} discharge windows currently {}".format(self.id, self.discharge_window))

        if INVERTER_TEST:
            self.self_test(minutes_now)

    def mimic_target_soc(self, current_charge_limit):
        """
        Function to turn on/off charging based on the current SOC and the set charge limit
        """
        if self.soc_percent >= float(current_charge_limit):
            # If current SOC is above Target SOC, turn Grid Charging off
            self.alt_charge_discharge_enable("charge", False, grid=True, timed=False)
            self.base.log(f"Current SOC {self.soc_percent}% is greater than Target SOC {current_charge_limit}. Grid Charge disabled.")
        else:
            # If we drop below the target, turn grid charging back on and make sure the charge current is correct
            self.alt_charge_discharge_enable("charge", True, grid=True, timed=False)
            if self.inv_output_charge_control == "current":
                self.set_current_from_power("charge", self.battery_rate_max_charge * 60 * 1000)
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
                if self.rest_api:
                    self.rest_setReserve(reserve)
                else:
                    entity_soc = self.base.get_entity(self.base.get_arg("reserve", indirect=False, index=self.id))
                    self.write_and_poll_value("reserve", entity_soc, reserve)
                if self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Target Reserve has been changed to {} at {}".format(self.id, reserve, self.base.time_now_str()))
                self.base.record_status("Inverter {} set reserve to {}".format(self.id, reserve))
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
                current_rate = self.rest_data["Control"]["Battery_Charge_Rate"]
            else:
                current_rate = self.base.get_arg("charge_rate", index=self.id, default=2600.0)

        if abs(current_rate - new_rate) > 100:
            self.base.log("Inverter {} current charge rate is {} and new target is {}".format(self.id, current_rate, new_rate))
            if SIMULATE:
                self.base.sim_charge_rate_now = new_rate
            else:
                if self.rest_api:
                    self.rest_setChargeRate(new_rate)
                else:
                    entity = self.base.get_entity(self.base.get_arg("charge_rate", indirect=False, index=self.id))
                    # Write
                    self.write_and_poll_value("charge_rate", entity, new_rate, fuzzy=100)
                    if self.inv_output_charge_control == "current":
                        self.set_current_from_power("charge", new_rate)

                if notify and self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} charge rate changes to {} at {}".format(self.id, new_rate, self.base.time_now_str()))
            if notify:
                self.base.record_status("Inverter {} charge rate changed to {}".format(self.id, new_rate))
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
                if self.rest_api:
                    self.rest_setDischargeRate(new_rate)
                else:
                    entity = self.base.get_entity(self.base.get_arg("discharge_rate", indirect=False, index=self.id))
                    self.write_and_poll_value("discharge_rate", entity, new_rate, fuzzy=100)

                    if self.inv_output_charge_control == "current":
                        self.set_current_from_power("discharge", new_rate)

                if notify and self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} discharge rate changes to {} at {}".format(self.id, new_rate, self.base.time_now_str()))
            if notify:
                self.base.record_status("Inverter {} discharge rate changed to {}".format(self.id, new_rate))
            self.mqtt_message(topic="set/discharge_rate", payload=new_rate)

    def adjust_battery_target(self, soc):
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
        soc = int(soc)
        soc = max(soc, self.reserve_percent)

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
                if self.rest_api:
                    self.rest_setChargeTarget(soc)
                else:
                    entity_soc = self.base.get_entity(self.base.get_arg("charge_limit", indirect=False, index=self.id))
                    self.write_and_poll_value("charge_limit", entity_soc, soc)

                if self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Target SOC has been changed to {} % at {}".format(self.id, soc, self.base.time_now_str()))
            self.base.record_status("Inverter {} set soc to {}".format(self.id, soc))
            self.mqtt_message(topic="set/target_soc", payload=soc)
        else:
            self.base.log("Inverter {} Current Target SOC is {} already at target".format(self.id, current_soc))

        # Inverters that need on/off controls rather than target SOC
        if not self.inv_has_target_soc:
            self.mimic_target_soc(soc)

    def write_and_poll_switch(self, name, entity, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        # Re-written to minimise writes
        domain = entity.domain

        current_state = entity.get_state()
        if isinstance(current_state, str):
            current_state = current_state.lower() in ["on", "enable", "true"]

        retry = 0
        while current_state != new_value and retry < 6:
            retry += 1
            if domain == "sensor":
                if new_value:
                    entity.set_state(state="on")
                else:
                    entity.set_state(state="off")
            else:
                if new_value:
                    entity.call_service("turn_on")
                else:
                    entity.call_service("turn_off")

            time.sleep(self.inv_write_and_poll_sleep)
            current_state = entity.get_state()
            if isinstance(current_state, str):
                current_state = current_state.lower() in ["on", "enable", "true"]

        if current_state == new_value:
            self.base.log("Inverter {} Wrote {} to {} successfully and got {}".format(self.id, name, new_value, entity.get_state()))
            return True
        else:
            self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, entity.get_state()))
            self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
            return False

    def write_and_poll_value(self, name, entity, new_value, fuzzy=0):
        # Modified to cope with sensor entities and writing strings
        # Re-written to minimise writes
        domain = entity.domain
        current_state = entity.get_state()

        if isinstance(new_value, str):
            matched = current_state == new_value
        else:
            matched = abs(float(current_state) - new_value) <= fuzzy

        retry = 0
        while (not matched) and (retry < 6):
            retry += 1
            if domain == "sensor":
                entity.set_state(state=new_value)
            else:
                # if isinstance(new_value, str):
                entity.call_service("set_value", value=new_value)

            time.sleep(self.inv_write_and_poll_sleep)
            current_state = entity.get_state()
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
            self.base.log(f"WARN: Inverter {self.id} Trying to write {new_value} to {name} didn't complete got {current_state}")
            self.base.record_status(f"Warn - Inverter {self.id} write to {name} failed", had_errors=True)
            return False

    def write_and_poll_option(self, name, entity, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        for retry in range(0, 6):
            entity.call_service("select_option", option=new_value)
            time.sleep(self.inv_write_and_poll_sleep)
            old_value = entity.get_state()
            if old_value == new_value:
                self.base.log("Inverter {} Wrote {} to {} successfully".format(self.id, name, new_value))
                return True
        self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, entity.get_state()))
        self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
        return False

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
                if changed_start_end and not self.rest_api:
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
                if self.rest_api:
                    self.rest_setBatteryMode(new_inverter_mode)
                else:
                    entity = self.base.get_entity(self.base.get_arg("inverter_mode", indirect=False, index=self.id))
                    if self.inv_has_ge_inverter_mode:
                        self.write_and_poll_option("inverter_mode", entity, new_inverter_mode)
                    else:
                        self.write_and_poll_value("inverter_mode", entity, new_inverter_mode)

                # Notify
                if self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Force discharge set to {} at time {}".format(self.id, force_discharge, self.base.time_now_str()))

            self.base.record_status("Inverter {} Set discharge mode to {}".format(self.id, new_inverter_mode))
            self.base.log("Inverter {} set force discharge to {}".format(self.id, force_discharge))

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
        else:
            if self.rest_data:
                old_start = self.rest_data["Timeslots"]["Discharge_start_time_slot_1"]
                old_end = self.rest_data["Timeslots"]["Discharge_end_time_slot_1"]
            elif "discharge_start_time" in self.base.args:
                old_start = self.base.get_arg("discharge_start_time", index=self.id)
                old_end = self.base.get_arg("discharge_end_time", index=self.id)
            else:
                self.log("WARN: Inverter {} unable read discharge window as neither REST, discharge_start_time or discharge_start_hour are set".format(self.id))

        # If the inverter doesn't have a discharge enable time then use midnight-midnight as an alternative disable
        if not self.inv_has_discharge_enable_time and not new_start_time:
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

        # Change start time
        if new_start and new_start != old_start:
            self.base.log("Inverter {} set new start time to {}".format(self.id, new_start))
            if SIMULATE:
                self.base.sim_discharge_start = new_start
            else:
                if self.rest_api:
                    pass  # REST writes as a single start/end time

                elif "discharge_start_time" in self.base.args:
                    # Always write to this as it is the GE default
                    changed_start_end = True
                    entity_discharge_start_time = self.base.get_entity(self.base.get_arg("discharge_start_time", indirect=False, index=self.id))
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("discharge_start_time", entity_discharge_start_time, new_start)
                    else:
                        self.write_and_poll_value("discharge_start_time", entity_discharge_start_time, new_start)

                    if self.inv_charge_time_format == "H M":
                        # If the inverter uses hours and minutes then write to these entities too
                        entity = self.base.get_entity(self.base.get_arg("discharge_start_hour", indirect=False, index=self.id))
                        self.write_and_poll_value("discharge_start_hour", entity, int(new_start[:2]))
                        entity = self.base.get_entity(self.base.get_arg("discharge_start_minute", indirect=False, index=self.id))
                        self.write_and_poll_value("discharge_start_minute", entity, int(new_start[3:5]))
                else:
                    self.log("WARN: Inverter {} unable write discharge start time as neither REST or discharge_start_time are set".format(self.id))

        # Change end time
        if new_end and new_end != old_end:
            self.base.log("Inverter {} Set new end time to {} was {}".format(self.id, new_end, old_end))
            if SIMULATE:
                self.base.sim_discharge_end = new_end
            else:
                if self.rest_api:
                    pass  # REST writes as a single start/end time
                elif "discharge_end_time" in self.base.args:
                    # Always write to this as it is the GE default
                    changed_start_end = True
                    entity_discharge_end_time = self.base.get_entity(self.base.get_arg("discharge_end_time", indirect=False, index=self.id))
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("discharge_end_time", entity_discharge_end_time, new_end)
                        # If the inverter uses hours and minutes then write to these entities too
                    else:
                        self.write_and_poll_value("discharge_end_time", entity_discharge_end_time, new_end)

                    if self.inv_charge_time_format == "H M":
                        entity = self.base.get_entity(self.base.get_arg("discharge_end_hour", indirect=False, index=self.id))
                        self.write_and_poll_value("discharge_end_hour", entity, int(new_end[:2]))
                        entity = self.base.get_entity(self.base.get_arg("discharge_end_minute", indirect=False, index=self.id))
                        self.write_and_poll_value("discharge_end_minute", entity, int(new_end[3:5]))
                else:
                    self.log("WARN: Inverter {} unable write discharge end time as neither REST or discharge_end_time are set".format(self.id))

        if (new_end != old_end) or (new_start != old_start):
            # For Solis inverters we also have to press the update_charge_discharge button to send the times to the inverter
            if self.inv_time_button_press:
                entity_id = self.base.get_arg("charge_discharge_update_button", indirect=False, index=self.id)
                self.press_and_poll_button(entity_id)

        # REST version of writing slot
        if self.rest_api and new_start and new_end and ((new_start != old_start) or (new_end != old_end)):
            changed_start_end = True
            if not SIMULATE:
                self.rest_setDischargeSlot1(new_start, new_end)

        # Force discharge, turn it on after we change the window
        if force_discharge:
            self.adjust_inverter_mode(force_discharge, changed_start_end=changed_start_end)

        # Notify
        if changed_start_end:
            self.base.record_status("Inverter {} set discharge slot to {} - {}".format(self.id, new_start, new_end))
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

        if old_charge_schedule_enable == "on" or old_charge_schedule_enable == "enable":
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_api:
                    self.rest_enableChargeSchedule(False)
                else:
                    entity = self.base.get_entity(self.base.get_arg("scheduled_charge_enable", indirect=False, index=self.id))
                    self.write_and_poll_switch("scheduled_charge_enable", entity, False)
                    # If there's no charge enable switch then we can enable using start and end time
                    if not self.inv_has_charge_enable_time and (self.inv_output_charge_control == "current"):
                        self.enable_charge_discharge_with_time_current("charge", False)
                if self.base.set_inverter_notify and notify:
                    self.base.call_notify("Predbat: Inverter {} Disabled scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = "off"

            if notify:
                self.base.record_status("Inverter {} Turned off scheduled charge".format(self.id))
            self.base.log("Inverter {} Turning off scheduled charge".format(self.id))

        # Updated cached status to disabled
        self.charge_enable_time = False
        self.charge_start_time_minutes = self.base.forecast_minutes
        self.charge_end_time_minutes = self.base.forecast_minutes

    def alt_charge_discharge_enable(self, direction, enable, grid=True, timed=False):
        """
        Alternative enable and disable of timed charging for non-GE inverters
        """
        if not grid and not timed:
            self.base.log("WARN: Unable to set Solis Energy Controls Switch: both Grid and Timed are False")
            return False

        if enable:
            str_enable = "enable"
        else:
            str_enable = "disable"

        str_type = ""
        if grid:
            str_type += "grid "
        if timed:
            if grid:
                str += "and "
            str += "timed "

        if self.inverter_type == "GS":
            # Solis just has a single switch for both directions
            # Need to check the logic of how this is called if both charging and discharging

            entity_id = self.base.get_arg("energy_control_switch", indirect=False, index=self.id)
            entity = self.base.get_entity(entity_id)
            switch = SOLAX_SOLIS_MODES.get(entity.get_state(), 0)
            # Grid charging is Bit 1(2) and Timed Charging is Bit 5(32)
            mask = 2 * timed + 32 * grid
            if switch > 0:
                # Timed charging is Bit 1 so we OR with 2 to set and AND with ~2 to clear
                if enable:
                    new_switch = switch | mask
                else:
                    new_switch = switch & ~mask

                if new_switch != switch:
                    self.base.log(f"Setting Solis Energy Control Switch to {new_switch} from {switch} to {str_enable} {str_type} charging")
                    # Now lookup the new mode in an inverted dict:
                    new_mode = {SOLAX_SOLIS_MODES[x]: x for x in SOLAX_SOLIS_MODES}[new_switch]

                    self.write_and_poll_option(name=entity_id, entity=entity, new_value=new_mode)
                    return True
                else:
                    self.base.log(f"Solis Energy Control Switch setting {switch} is correct for {str_enable} {str_type} charging")
                    return True
            else:
                self.base.log(f"WARN: Unable to read current value of Solis Mode switch {entity_id}. Unable to {str_enable} {str_type} charging")
                return False

        # MQTT
        if direction == "charge" and enable:
            self.mqtt_message("set/charge", payload=int(self.battery_rate_max_charge * 60 * 1000))
        elif direction == "discharge" and enable:
            self.mqtt_message("set/discharge", payload=int(self.battery_rate_max_discharge * 60 * 1000))
        else:
            self.mqtt_message("set/auto", payload="true")

    def mqtt_message(self, topic, payload):
        """
        Send an MQTT message via service
        """
        if self.inv_has_mqtt_api:
            self.base.call_service("mqtt/publish", qos=1, retain=True, topic=(self.inv_mqtt_topic + "/" + topic), payload=payload)

    def enable_charge_discharge_with_time_current(self, direction, enable):
        """
        Enable or disable timed charge/discharge
        """
        # To enable we set the current based on the required power
        if enable:
            power = self.base.get_arg(f"{direction}_rate", index=self.id, default=2600.0)
            self.set_current_from_power(direction, power)
        else:
            # To disable we set both times to 00:00
            for x in ["start", "end"]:
                for y in ["hour", "minute"]:
                    name = f"{direction}_{x}_{y}"
                    entity = self.base.get_entity(self.base.get_arg(name, indirect=False, index=self.id))
                    self.write_and_poll_value(name, entity, 0)

    def set_current_from_power(self, direction, power):
        """
        Set the timed charge/discharge current setting by converting power to current
        """
        new_current = round(power / self.battery_voltage, 1)
        entity = self.base.get_entity(self.base.get_arg(f"timed_{direction}_current", indirect=False, index=self.id))
        self.write_and_poll_value(f"timed_{direction}_current", entity, new_current, fuzzy=1)

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
                self.log("WARN: Inverter {} unable read charge window as neither REST or discharge_start_time".format(self.id))

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

        # Disable charging if required, for REST no need as we change start and end together anyhow
        if not in_new_window and not self.rest_api and ((new_start != old_start) or (new_end != old_end)):
            if self.inv_has_charge_enable_time:
                self.disable_charge_window(notify=False)
                have_disabled = True

        # Program start slot
        if new_start != old_start:
            if SIMULATE:
                self.base.sim_charge_start_time = new_start
                self.base.log("Simulate sim_charge_start_time now {}".format(new_start))
            else:
                if self.rest_api:
                    pass  # REST will be written as start/end together
                elif "charge_start_time" in self.base.args:
                    # Always write to this as it is the GE default
                    entity_start = self.base.get_entity(self.base.get_arg("charge_start_time", indirect=False, index=self.id))
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("charge_start_time", entity_start, new_start)
                    else:
                        self.write_and_poll_value("charge_start_time", entity_start, new_start)

                    if self.inv_charge_time_format == "H M":
                        # If the inverter uses hours and minutes then write to these entities too
                        entity = self.base.get_entity(self.base.get_arg("charge_start_hour", indirect=False, index=self.id))
                        self.write_and_poll_value("charge_start_hour", entity, int(new_start[:2]))
                        entity = self.base.get_entity(self.base.get_arg("charge_start_minute", indirect=False, index=self.id))
                        self.write_and_poll_value("charge_start_minute", entity, int(new_start[3:5]))
                else:
                    self.log("WARN: Inverter {} unable write charge window start as neither REST or charge_start_time are set".format(self.id))

        # Program end slot
        if new_end != old_end:
            if SIMULATE:
                self.base.sim_charge_end_time = new_end
                self.base.log("Simulate sim_charge_end_time now {}".format(new_end))
            else:
                if self.rest_api:
                    pass  # REST will be written as start/end together
                elif "charge_end_time" in self.base.args:
                    # Always write to this as it is the GE default
                    entity_end = self.base.get_entity(self.base.get_arg("charge_end_time", indirect=False, index=self.id))
                    if self.inv_charge_time_entity_is_option:
                        self.write_and_poll_option("charge_end_time", entity_end, new_end)
                    else:
                        self.write_and_poll_value("charge_end_time", entity_end, new_end)

                    if self.inv_charge_time_format == "H M":
                        entity = self.base.get_entity(self.base.get_arg("charge_end_hour", indirect=False, index=self.id))
                        self.write_and_poll_value("charge_end_hour", entity, int(new_end[:2]))
                        entity = self.base.get_entity(self.base.get_arg("charge_end_minute", indirect=False, index=self.id))
                        self.write_and_poll_value("charge_end_minute", entity, int(new_end[3:5]))
                else:
                    self.log("WARN: Inverter {} unable write charge window end as neither REST, charge_end_hour or charge_end_time are set".format(self.id))

        if new_start != old_start or new_end != old_end:
            if self.rest_api and not SIMULATE:
                self.rest_setChargeSlot1(new_start, new_end)

            # For Solis inverters we also have to press the update_charge_discharge button to send the times to the inverter
            if self.inv_time_button_press:
                entity_id = self.base.get_arg("charge_discharge_update_button", indirect=False, index=self.id)
                self.press_and_poll_button(entity_id)

            if self.base.set_inverter_notify and not SIMULATE:
                self.base.call_notify("Predbat: Inverter {} Charge window change to: {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            self.base.record_status("Inverter {} Charge window change to: {} - {}".format(self.id, new_start, new_end))
            self.base.log("Inverter {} Updated start and end charge window to {} - {} (old {} - {})".format(self.id, new_start, new_end, old_start, old_end))

        if old_charge_schedule_enable == "off" or old_charge_schedule_enable == "disable" or have_disabled:
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_api:
                    self.rest_enableChargeSchedule(True)
                elif "scheduled_charge_enable" in self.base.args:
                    entity = self.base.get_entity(self.base.get_arg("scheduled_charge_enable", indirect=False, index=self.id))
                    self.write_and_poll_switch("scheduled_charge_enable", entity, True)
                    if not self.inv_has_charge_enable_time and (self.inv_output_charge_control == "current"):
                        self.enable_charge_discharge_with_time_current("charge", True)
                else:
                    self.log("WARN: Inverter {} unable write charge window enable as neither REST or scheduled_charge_enable are set".format(self.id))

                # Only notify if it's a real change and not a temporary one
                if old_charge_schedule_enable == "off" or old_charge_schedule_enable == "disable" and self.base.set_inverter_notify:
                    self.base.call_notify("Predbat: Inverter {} Enabling scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = "on"

            self.charge_enable_time = True

            self.base.record_status("Inverter {} Turned on charge enable".format(self.id))

            if old_charge_schedule_enable == "off" or old_charge_schedule_enable == "disable":
                self.base.log("Inverter {} Turning on scheduled charge".format(self.id))

    def press_and_poll_button(self, entity_id):
        for retry in range(0, 6):
            self.base.call_service("button/press", entity_id=entity_id)
            entity = self.base.get_entity(entity_id)
            time.sleep(self.inv_write_and_poll_sleep)
            time_pressed = datetime.strptime(entity.get_state(), TIME_FORMAT_SECONDS)

            if (pytz.timezone("UTC").localize(datetime.now()) - time_pressed).seconds < 10:
                self.base.log(f"Successfully pressed button {entity_id} on Inverter {self.id}")
                return True
        self.base.log(f"WARN: Inverter {self.id} Trying to press {entity_id} didn't complete")
        self.base.record_status(f"Warn: Inverter {self.id} Trying to press {entity_id} didn't complete")
        return False

    def rest_readData(self, api="readData"):
        """
        Get inverter status
        """
        url = self.rest_api + "/" + api
        try:
            r = requests.get(url)
        except Exception as e:
            self.base.log("ERROR: Exception raised {}".format(e))
            r = None

        if r and (r.status_code == 200):
            json = r.json()
            if "Control" in json:
                return json
            else:
                self.base.log("WARN: Inverter {} read bad REST data from {} - REST will be disabled".format(self.id, url))
                self.base.record_status("Inverter {} read bad REST data from {} - REST will be disabled".format(self.id, url), had_errors=True)
                return None
        else:
            self.base.log("WARN: Inverter {} unable to read REST data from {} - REST will be disabled".format(self.id, url))
            self.base.record_status("Inverter {} unable to read REST data from {} - REST will be disabled".format(self.id, url), had_errors=True)
            return None

    def rest_runAll(self, old_data=None):
        """
        Updated and get inverter status
        """
        new_data = self.rest_readData(api="runAll")
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
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if float(self.rest_data["Control"]["Target_SOC"]) == target:
                self.base.log("Inverter {} charge target {} via REST successful on retry {}".format(self.id, target, retry))
                return True

        self.base.log("WARN: Inverter {} charge target {} via REST failed".format(self.id, target))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeTarget".format(self.id), had_errors=True)
        return False

    def rest_setChargeRate(self, rate):
        """
        Configure charge target % via REST
        """
        rate = int(rate)
        url = self.rest_api + "/setChargeRate"
        data = {"chargeRate": rate}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new = self.rest_data["Control"]["Battery_Charge_Rate"]
            if abs(new - rate) < 100:
                self.base.log("Inverter {} set charge rate {} via REST successful on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("WARN: Inverter {} set charge rate {} via REST failed got {}".format(self.id, rate, self.rest_data["Control"]["Battery_Charge_Rate"]))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeRate".format(self.id), had_errors=True)
        return False

    def rest_setDischargeRate(self, rate):
        """
        Configure charge target % via REST
        """
        rate = int(rate)
        url = self.rest_api + "/setDischargeRate"
        data = {"dischargeRate": rate}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new = self.rest_data["Control"]["Battery_Discharge_Rate"]
            if abs(new - rate) < 100:
                self.base.log("Inverter {} set discharge rate {} via REST successful on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("WARN: Inverter {} set discharge rate {} via REST failed got {}".format(self.id, rate, self.rest_data["Control"]["Battery_Discharge_Rate"]))
        self.base.record_status(
            "Warn - Inverter {} REST failed to setDischargeRate to {} got {}".format(self.id, rate, self.rest_data["Control"]["Battery_Discharge_Rate"]), had_errors=True
        )
        return False

    def rest_setBatteryMode(self, inverter_mode):
        """
        Configure invert mode via REST
        """
        url = self.rest_api + "/setBatteryMode"
        data = {"mode": inverter_mode}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if inverter_mode == self.rest_data["Control"]["Mode"]:
                self.base.log("Set inverter {} mode {} via REST successful on retry {}".format(self.id, inverter_mode, retry))
                return True

        self.base.log("WARN: Set inverter {} mode {} via REST failed".format(self.id, inverter_mode))
        self.base.record_status("Warn - Inverter {} REST failed to setBatteryMode".format(self.id), had_errors=True)
        return False

    def rest_setReserve(self, target):
        """
        Configure reserve % via REST
        """
        target = int(target)
        result = target
        url = self.rest_api + "/setBatteryReserve"
        data = {"reservePercent": target}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            result = int(float(self.rest_data["Control"]["Battery_Power_Reserve"]))
            if result == target:
                self.base.log("Set inverter {} reserve {} via REST successful on retry {}".format(self.id, target, retry))
                return True

        self.base.log("WARN: Set inverter {} reserve {} via REST failed on retry {} got {}".format(self.id, target, retry, result))
        self.base.record_status("Warn - Inverter {} REST failed to setReserve to {} got {}".format(self.id, target, result), had_errors=True)
        return False

    def rest_enableChargeSchedule(self, enable):
        """
        Configure reserve % via REST
        """
        url = self.rest_api + "/enableChargeSchedule"
        data = {"state": "enable" if enable else "disable"}

        for retry in range(0, 5):
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

        self.base.log("WARN: Set inverter {} charge schedule {} via REST failed got {}".format(self.id, enable, self.rest_data["Control"]["Enable_Charge_Schedule"]))
        self.base.record_status("Warn - Inverter {} REST failed to enableChargeSchedule".format(self.id), had_errors=True)
        return False

    def rest_setChargeSlot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        url = self.rest_api + "/setChargeSlot1"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if self.rest_data["Timeslots"]["Charge_start_time_slot_1"] == start and self.rest_data["Timeslots"]["Charge_end_time_slot_1"] == finish:
                self.base.log("Inverter {} set charge slot 1 {} via REST successful after retry {}".format(self.id, data, retry))
                return True

        self.base.log("WARN: Inverter {} set charge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeSlot1".format(self.id), had_errors=True)
        return False

    def rest_setDischargeSlot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        url = self.rest_api + "/setDischargeSlot1"
        data = {"start": start[:5], "finish": finish[:5]}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            if self.rest_data["Timeslots"]["Discharge_start_time_slot_1"] == start and self.rest_data["Timeslots"]["Discharge_end_time_slot_1"] == finish:
                self.base.log("Inverter {} Set discharge slot 1 {} via REST successful after retry {}".format(self.id, data, retry))
                return True

        self.base.log("WARN: Inverter {} Set discharge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn - Inverter {} REST failed to setDischargeSlot1".format(self.id), had_errors=True)
        return False


class PredBat(hass.Hass):
    """
    The battery prediction class itself
    """

    def call_notify(self, message):
        """
        Send HA notifications
        """
        for device in self.notify_devices:
            self.call_service("notify/" + device, message=message)

    def resolve_arg(self, arg, value, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Resolve argument templates and state instances
        """
        if isinstance(value, list) and (index is not None):
            if index < len(value):
                value = value[index]
            else:
                self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))
                value = None
            index = None

        if index:
            self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))

        # If we have a list of items get each and add them up or return them as a list
        if isinstance(value, list):
            if combine:
                final = 0
                for item in value:
                    got = self.resolve_arg(arg, item, default=default, indirect=True)
                    try:
                        final += float(got)
                    except (ValueError, TypeError):
                        self.log("WARN: Return bad value {} from {} arg {}".format(got, item, arg))
                        self.record_status("Warn - Return bad value {} from {} arg {}".format(got, item, arg), had_errors=True)
                return final
            else:
                final = []
                for item in value:
                    item = self.resolve_arg(arg, item, default=default, indirect=indirect)
                    final.append(item)
                return final

        # Resolve templated data
        for repeat in range(0, 2):
            if isinstance(value, str) and "{" in value:
                try:
                    value = value.format(**self.args)
                except KeyError:
                    self.log("WARN: can not resolve {} value {}".format(arg, value))
                    self.record_status("Warn - can not resolve {} value {}".format(arg, value), had_errors=True)
                    value = default

        # Resolve indirect instance
        if indirect and isinstance(value, str) and "." in value:
            nattribute = None
            if "$" in value:
                value, attribute = value.split("$")

            if attribute:
                value = self.get_state(entity_id=value, default=default, attribute=attribute)
            else:
                value = self.get_state(entity_id=value, default=default)
        return value

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Argument getter that can use HA state as well as fixed values
        """
        value = None

        # Get From HA config
        value, default = self.get_ha_config(arg, default)

        # Resolve locally if no HA config
        if value is None:
            if (arg not in self.args) and default and (index is not None):
                # Allow default to apply to all indices if there is not config item set
                index = None
            value = self.args.get(arg, default)
            value = self.resolve_arg(arg, value, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index)

        if isinstance(default, float):
            # Convert to float?
            try:
                value = float(value)
            except (ValueError, TypeError):
                self.log("WARN: Return bad float value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn - Return bad float value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, int) and not isinstance(default, bool):
            # Convert to int?
            try:
                value = int(float(value))
            except (ValueError, TypeError):
                self.log("WARN: Return bad int value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn - Return bad int value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, bool) and isinstance(value, str):
            # Convert to Boolean
            if value.lower() in ["on", "true", "yes", "enabled", "enable", "connected"]:
                value = True
            else:
                value = False
        elif isinstance(default, list):
            # Convert to list?
            if not isinstance(value, list):
                value = [value]

        return value

    def get_ge_url(self, url, headers, now_utc):
        """
        Get data from GE Cloud
        """
        if url in self.ge_url_cache:
            stamp = self.ge_url_cache[url]["stamp"]
            pdata = self.ge_url_cache[url]["data"]
            age = now_utc - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached GE data for {} age {} minutes".format(url, self.dp1(age.seconds / 60)))
                return pdata

        self.log("Fetching {}".format(url))
        r = requests.get(url, headers=headers)
        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("WARN: Error downloading GE data from url {}".format(url))
            self.record_status("Warn - Error downloading GE data from cloud", debug=url, had_errors=True)
            return False

        self.ge_url_cache[url] = {}
        self.ge_url_cache[url]["stamp"] = now_utc
        self.ge_url_cache[url]["data"] = data
        return data

    def download_ge_data(self, now_utc):
        """
        Download consumption data from GE Cloud
        """
        geserial = self.get_arg("ge_cloud_serial")
        gekey = self.args.get("ge_cloud_key", None)

        if not geserial:
            self.log("ERROR: GE Cloud has been enabled but ge_cloud_serial is not set to your serial")
            self.record_status("Warn - GE Cloud has been enabled but ge_cloud_serial is not set to your serial", had_errors=True)
            return False
        if not gekey:
            self.log("ERROR: GE Cloud has been enabled but ge_cloud_key is not set to your appkey")
            self.record_status("Warn - GE Cloud has been enabled but ge_cloud_key is not set to your appkey", had_errors=True)
            return False

        headers = {"Authorization": "Bearer  " + gekey, "Content-Type": "application/json", "Accept": "application/json"}
        mdata = []
        days_prev = 0
        while days_prev <= self.max_days_previous:
            time_value = now_utc - timedelta(days=(self.max_days_previous - days_prev))
            datestr = time_value.strftime("%Y-%m-%d")
            url = "https://api.givenergy.cloud/v1/inverter/{}/data-points/{}?pageSize=1024".format(geserial, datestr)
            while url:
                data = self.get_ge_url(url, headers, now_utc)

                darray = data.get("data", None)
                if darray is None:
                    self.log("WARN: Error downloading GE data from url {}".format(url))
                    self.record_status("Warn - Error downloading GE data from cloud", debug=url)
                    return False

                for item in darray:
                    timestamp = item["time"]
                    consumption = item["total"]["consumption"]
                    dimport = item["total"]["grid"]["import"]
                    dexport = item["total"]["grid"]["export"]
                    dpv = item["total"]["solar"]

                    new_data = {}
                    new_data["last_updated"] = timestamp
                    new_data["consumption"] = consumption
                    new_data["import"] = dimport
                    new_data["export"] = dexport
                    new_data["pv"] = dpv
                    mdata.append(new_data)
                url = data["links"].get("next", None)
            days_prev += 1

        # Find how old the data is
        item = mdata[0]
        try:
            last_updated_time = self.str2time(item["last_updated"])
        except (ValueError, TypeError):
            last_updated_time = now_utc

        age = now_utc - last_updated_time
        self.load_minutes_age = age.days
        self.load_minutes = self.minute_data(
            mdata, self.max_days_previous, now_utc, "consumption", "last_updated", backwards=True, smoothing=True, scale=self.load_scaling, clean_increment=True
        )
        self.import_today = self.minute_data(
            mdata, self.max_days_previous, now_utc, "import", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True
        )
        self.export_today = self.minute_data(
            mdata, self.max_days_previous, now_utc, "export", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True
        )
        self.pv_today = self.minute_data(
            mdata, self.max_days_previous, now_utc, "pv", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True
        )

        self.load_minutes_now = self.load_minutes.get(0, 0) - self.load_minutes.get(self.minutes_now, 0)
        self.import_today_now = self.import_today.get(0, 0) - self.import_today.get(self.minutes_now, 0)
        self.export_today_now = self.export_today.get(0, 0) - self.export_today.get(self.minutes_now, 0)
        self.pv_today_now = self.pv_today.get(0, 0) - self.pv_today.get(self.minutes_now, 0)
        self.log("Downloaded {} datapoints from GE going back {} days".format(len(self.load_minutes), self.load_minutes_age))
        return True

    def download_predbat_releases_url(self, url):
        """
        Download release data from github, but use the cache for 2 hours
        """
        releases = []

        # Check the cache first
        now = datetime.now()
        if url in self.github_url_cache:
            stamp = self.github_url_cache[url]["stamp"]
            pdata = self.github_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (120 * 60):
                self.log("Using cached GITHub data for {} age {} minutes".format(url, self.dp1(age.seconds / 60)))
                return pdata

        try:
            r = requests.get(url)
        except:
            self.log("WARN: Unable to load data from Github url: {}".format(url))
            return []

        try:
            pdata = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("WARN: Unable to decode data from Github url: {}".format(url))
            return []

        # Save to cache
        self.github_url_cache[url] = {}
        self.github_url_cache[url]["stamp"] = now
        self.github_url_cache[url]["data"] = pdata

        return pdata

    def download_predbat_releases(self):
        """
        Download release data
        """
        global PREDBAT_UPDATE_OPTIONS
        auto_update = self.get_arg("auto_update")
        url = "https://api.github.com/repos/springfall2008/batpred/releases"
        data = self.download_predbat_releases_url(url)
        self.releases = {}
        if data and isinstance(data, list):
            found_latest = False
            found_latest_beta = False

            release = data[0]
            self.releases["this"] = THIS_VERSION
            self.releases["latest"] = "Unknown"
            self.releases["latest_beta"] = "Unknown"

            for release in data:
                if release.get("tag_name", "Unknown") == THIS_VERSION:
                    self.releases["this_name"] = release.get("name", "Unknown")
                    self.releases["this_body"] = release.get("body", "Unknown")

                if not found_latest and not release.get("prerelease", True):
                    self.releases["latest"] = release.get("tag_name", "Unknown")
                    self.releases["latest_name"] = release.get("name", "Unknown")
                    self.releases["latest_body"] = release.get("body", "Unknown")
                    found_latest = True

                if not found_latest_beta:
                    self.releases["latest_beta"] = release.get("tag_name", "Unknown")
                    self.releases["latest_beta_name"] = release.get("name", "Unknown")
                    self.releases["latest_beta_body"] = release.get("body", "Unknown")
                    found_latest_beta = True

            self.log(
                "Predbat {} version {} currently running, latest version is {} latest beta {}".format(
                    __file__, self.releases["this"], self.releases["latest"], self.releases["latest_beta"]
                )
            )
            PREDBAT_UPDATE_OPTIONS = ["main"]
            this_tag = THIS_VERSION

            # Find all versions for the dropdown menu
            for release in data:
                prerelease = release.get("prerelease", True)
                tag = release.get("tag_name", None)
                if tag:
                    if prerelease:
                        full_name = tag + " (beta) " + release.get("name", "")
                    else:
                        full_name = tag + " " + release.get("name", "")
                    PREDBAT_UPDATE_OPTIONS.append(full_name)
                    if this_tag == tag:
                        this_tag = full_name
                if len(PREDBAT_UPDATE_OPTIONS) >= 10:
                    break

            # Update the drop down menu
            item = self.config_index.get("update", None)
            if item:
                item["options"] = PREDBAT_UPDATE_OPTIONS
                item["value"] = None

            # See what version we are on and auto-update
            if this_tag not in PREDBAT_UPDATE_OPTIONS:
                this_tag = this_tag + " (?)"
                PREDBAT_UPDATE_OPTIONS.append(this_tag)
                self.log("Autoupdate: Currently on unknown version {}".format(this_tag))
            else:
                if self.releases["this"] == self.releases["latest"]:
                    self.log("Autoupdate: Currently up to date")
                elif self.releases["this"] == self.releases["latest_beta"]:
                    self.log("Autoupdate: Currently on latest beta")
                else:
                    latest_version = self.releases["latest"] + " " + self.releases["latest_name"]
                    if auto_update:
                        self.log("Autoupdate: There is an update pending {} - auto update triggered!".format(latest_version))
                        self.download_predbat_version(latest_version)
                    else:
                        self.log("Autoupdate: There is an update pending {} - auto update is off".format(latest_version))

            # Refresh the list
            self.expose_config("update", this_tag)

        else:
            self.log("WARN: Unable to download Predbat version information from github, return code: {}".format(data))

        return self.releases

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, self.dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for retry in range(0, 3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("WARN: Unable to download Octopus data from URL {}".format(url))
            self.record_status("Warn - Unable to download Octopus data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.octopus_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        # Cache New Octopus data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["data"] = pdata
        return pdata

    def futurerate_analysis(self):
        """
        Analyse futurerate energy data
        """

        url = None
        if "futurerate_url" in self.args:
            url = self.get_arg("futurerate_url", indirect=False)
        self.log("Fetching futurerate data from {}".format(url))
        if not url:
            return {}, {}

        try:
            pdata = self.download_futurerate_data(url)
        except ValueError:
            return {}, {}

        nord_tz = pytz.timezone("Europe/Oslo")
        now_offset = datetime.now(nord_tz).strftime("%z")
        extracted_data = {}
        extracted_keys = []
        array_values = []
        mdata = {}

        peak_start = datetime.strptime(self.get_arg("futurerate_peak_start", "00:00:00"), "%H:%M:%S")
        peak_end = datetime.strptime(self.get_arg("futurerate_peak_end", "00:00:00"), "%H:%M:%S")
        peak_start_minutes = peak_start.minute + peak_start.hour * 60
        peak_end_minutes = peak_end.minute + peak_end.hour * 60
        if peak_end_minutes < peak_start_minutes:
            peak_end_minutes += 24 * 60

        peak_premium_import = self.get_arg("futurerate_peak_premium_import", 0)
        peak_premium_export = self.get_arg("futurerate_peak_premium_export", 0)

        self.log("Future rates - peak rate is {} - {} minutes premium import {} export {}".format(peak_start_minutes, peak_end_minutes, peak_premium_import, peak_premium_export))

        if pdata:
            if "Rows" in pdata:
                for row in pdata["Rows"]:
                    if "Name" in row:
                        rname = row.get("Name", "")
                        rstart = row.get("StartTime", "") + now_offset
                        rend = row.get("EndTime", "") + now_offset
                    if "Columns" in row:
                        for column in row["Columns"]:
                            cname = column.get("Name", "")
                            cvalue = column.get("Value", "")
                            date_start, time_start = rstart.split("T")
                            date_end, time_end = rend.split("T")
                            if "-" in cname and "," in cvalue and cname:
                                date_start = cname
                                date_end = cname
                                cvalue = cvalue.replace(",", ".")
                                cvalue = float(cvalue)
                                rstart = date_start + "T" + time_start
                                rend = date_end + "T" + time_end
                                TIME_FORMAT_NORD = "%d-%m-%YT%H:%M:%S%z"
                                time_date_start = datetime.strptime(rstart, TIME_FORMAT_NORD)
                                time_date_end = datetime.strptime(rend, TIME_FORMAT_NORD)
                                delta_start = time_date_start - self.midnight_utc
                                delta_end = time_date_end - self.midnight_utc

                                minutes_start = delta_start.seconds / 60
                                minutes_end = delta_end.seconds / 60
                                if minutes_end < minutes_start:
                                    minutes_end += 24 * 60

                                # Convert to pence with Agile formula, starts in pounds per Megawatt hour
                                rate_import = (cvalue / 10) * 2.2
                                rate_export = (cvalue / 10) * 0.95
                                if minutes_start >= peak_start_minutes and minutes_end <= peak_end_minutes:
                                    rate_import += peak_premium_import
                                    rate_export += peak_premium_export
                                rate_import = min(rate_import, 95)  # Cap
                                rate_export = max(rate_export, 0)  # Cap
                                rate_import = rate_import * 1.05  # Vat only on import

                                item = {}
                                item["from"] = time_date_start.strftime(TIME_FORMAT)
                                item["to"] = time_date_end.strftime(TIME_FORMAT)
                                item["rate_import"] = self.dp2(rate_import)
                                item["rate_export"] = self.dp2(rate_export)
                                extracted_data[time_date_start] = item

                                if time_date_start not in extracted_keys:
                                    extracted_keys.append(time_date_start)

        if extracted_keys:
            extracted_keys.sort()
            for key in extracted_keys:
                array_values.append(extracted_data[key])
            self.log("Loaded {} datapoints of futurerate analysis".format(len(extracted_keys)))
            mdata_import = self.minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_import", "from", backwards=False, to_key="to")
            mdata_export = self.minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_export", "from", backwards=False, to_key="to")

        future_data = []
        for minute in range(self.minutes_now, self.forecast_plan_hours * 60 + self.minutes_now, 60):
            if mdata_import.get(minute) or mdata_export.get(minute):
                future_data.append("{} => {} / {}".format(minute, mdata_import.get(minute), mdata_export.get(minute)))

        self.log("Predicted future rates: {}".format(future_data))
        return mdata_import, mdata_export

    def download_futurerate_data(self, url):
        """
        Download futurerate data directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        # Check the cache first
        now = datetime.now()
        if url in self.futurerate_url_cache:
            stamp = self.futurerate_url_cache[url]["stamp"]
            pdata = self.futurerate_url_cache[url]["data"]
            update_time_since_midnight = stamp - self.midnight
            now_since_midnight = now - self.midnight
            age = now - stamp
            needs_update = False

            # Update if last data was yesterday
            if update_time_since_midnight.seconds < 0:
                needs_update = True

            # data updates at 11am CET so update every 30 minutes during this period
            if now_since_midnight.seconds > (9.5 * 60 * 60) and now_since_midnight.seconds < (11 * 60 * 60):
                if age.seconds > (0.5 * 60 * 60):
                    needs_update = True
            if age.seconds > (12 * 60 * 60):
                needs_update = True

            if not needs_update:
                self.log("Return cached futurerate data for {} age {} minutes".format(url, self.dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for retry in range(0, 3):
            pdata = self.download_futurerate_data_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("WARN: Unable to download futurerate data from URL {}".format(url))
            self.record_status("Warn - Unable to download futurerate data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.futurerate_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        # Cache New Octopus data
        self.futurerate_url_cache[url] = {}
        self.futurerate_url_cache[url]["stamp"] = now
        self.futurerate_url_cache[url]["data"] = pdata
        return pdata

    def download_futurerate_data_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = {}

        if self.debug_enable:
            self.log("Download {}".format(url))
        r = requests.get(url)
        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("WARN: Error downloading futurerate data from url {}".format(url))
            self.record_status("Warn - Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}
        if "data" in data:
            mdata = data["data"]
        else:
            self.log("WARN: Error downloading futurerate data from url {}".format(url))
            self.record_status("Warn - Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}
        return mdata

    def download_octopus_rates_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0

        while url and pages < 3:
            if self.debug_enable:
                self.log("Download {}".format(url))
            r = requests.get(url)
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                self.record_status("Warn - Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if "results" in data:
                mdata += data["results"]
            else:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                self.record_status("Warn - Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            url = data.get("next", None)
            pages += 1
        pdata = self.minute_data(mdata, self.forecast_days + 1, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return pdata

    def minutes_to_time(self, updated, now):
        """
        Compute the number of minutes between a time (now) and the updated time
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
        return minutes

    def str2time(self, str):
        if "." in str:
            tdata = datetime.strptime(str, TIME_FORMAT_SECONDS)
        elif "T" in str:
            tdata = datetime.strptime(str, TIME_FORMAT)
        else:
            tdata = datetime.strptime(str, TIME_FORMAT_OCTOPUS)
        return tdata

    def load_car_energy(self, now_utc):
        """
        Load previous car charging energy data
        """
        self.car_charging_energy = {}
        if "car_charging_energy" in self.args:
            self.car_charging_energy = self.minute_data_import_export(now_utc, "car_charging_energy", scale=self.car_charging_energy_scale)
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold * 60.0))
        return self.car_charging_energy

    def minute_data_import_export(self, now_utc, key, scale=1.0):
        """
        Download one or more entities for import/export data
        """
        entity_ids = self.get_arg(key, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        import_today = {}
        for entity_id in entity_ids:
            try:
                history = self.get_history(entity_id=entity_id, days=self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                import_today = self.minute_data(
                    history[0], self.max_days_previous, now_utc, "state", "last_updated", backwards=True, smoothing=True, scale=scale, clean_increment=True, accumulate=import_today
                )
            else:
                self.log("Error: Unable to fetch history for {}".format(entity_id))
                self.record_status("Error - Unable to fetch history from {}".format(entity_id), had_errors=True)
                raise ValueError

        return import_today

    def minute_data_load(self, now_utc, entity_name, max_days_previous):
        """
        Download one or more entities for load data
        """
        entity_ids = self.get_arg(entity_name, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        load_minutes = {}
        age_days = None
        for entity_id in entity_ids:
            history = self.get_history(entity_id=entity_id, days=max_days_previous)
            if history:
                item = history[0][0]
                try:
                    last_updated_time = self.str2time(item["last_updated"])
                except (ValueError, TypeError):
                    last_updated_time = now_utc
                age = now_utc - last_updated_time
                if age_days is None:
                    age_days = age.days
                else:
                    age_days = min(age_days, age.days)
                load_minutes = self.minute_data(
                    history[0],
                    max_days_previous,
                    now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    smoothing=True,
                    scale=self.load_scaling,
                    clean_increment=True,
                    accumulate=load_minutes,
                )
            else:
                self.log("ERROR: Unable to fetch history for {}".format(entity_id))
                self.record_status("Error - Unable to fetch history from {}".format(entity_id), had_errors=True)
                raise ValueError

        if age_days is None:
            age_days = 0
        return load_minutes, age_days

    def minute_data(
        self,
        history,
        days,
        now,
        state_key,
        last_updated_key,
        backwards=False,
        to_key=None,
        smoothing=False,
        clean_increment=False,
        divide_by=0,
        scale=1.0,
        accumulate=[],
        adjust_key=None,
        spreading=None,
    ):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        adata = {}
        newest_state = 0
        last_state = 0
        newest_age = 999999
        prev_last_updated_time = None
        max_increment = MAX_INCREMENT

        # Check history is valid
        if not history:
            self.log("Warning, empty history passed to minute_data, ignoring (check your settings)...")
            return mdata

        # Process history
        for item in history:
            # Ignore data without correct keys
            if state_key not in item:
                continue
            if last_updated_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue

            # Get the numerical key and the timestamp and ignore if in error
            try:
                state = float(item[state_key]) * scale
                last_updated_time = self.str2time(item[last_updated_key])
            except (ValueError, TypeError):
                continue

            # Divide down the state if required
            if divide_by:
                state /= divide_by

            # Update prev to the first if not set
            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

            # Intelligent adjusted?
            if adjust_key:
                adjusted = item.get(adjust_key, False)
            else:
                adjusted = False

            # Work out end of time period
            # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
            if to_key:
                to_value = item[to_key]
                if not to_value:
                    to_time = now + timedelta(minutes=24 * 60 * self.forecast_days)
                else:
                    to_time = self.str2time(item[to_key])
            else:
                if backwards:
                    to_time = prev_last_updated_time
                else:
                    if smoothing:
                        to_time = last_updated_time
                        last_updated_time = prev_last_updated_time
                    else:
                        to_time = None

            if backwards:
                timed = now - last_updated_time
                if to_time:
                    timed_to = now - to_time
            else:
                timed = last_updated_time - now
                if to_time:
                    timed_to = to_time - now

            minutes = int(timed.seconds / 60) + int(timed.days * 60 * 24)
            if to_time:
                minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60 * 24)

            if minutes < newest_age:
                newest_age = minutes
                newest_state = state

            if to_time:
                minute = minutes
                if minute == minutes_to:
                    mdata[minute] = state
                else:
                    if smoothing:
                        # Reset to zero, sometimes not exactly zero
                        if clean_increment and state < last_state and (state <= (last_state / 10.0)):
                            while minute < minutes_to:
                                mdata[minute] = state
                                minute += 1
                        else:
                            # Can't really go backwards as incrementing data
                            if clean_increment and state < last_state:
                                state = last_state

                            # Create linear function
                            diff = (state - last_state) / (minutes_to - minute)

                            # If the spike is too big don't smooth it, it will removed in the clean function later
                            if clean_increment and max_increment > 0 and diff > max_increment:
                                diff = 0

                            index = 0
                            while minute < minutes_to:
                                if backwards:
                                    mdata[minute] = state - diff * index
                                else:
                                    mdata[minute] = last_state + diff * index
                                minute += 1
                                index += 1
                    else:
                        while minute < minutes_to:
                            if backwards:
                                mdata[minute] = last_state
                            else:
                                mdata[minute] = state

                            if adjusted:
                                adata[minute] = True
                            minute += 1
            else:
                if spreading:
                    for minute in range(minutes, minutes + spreading):
                        mdata[minute] = state
                else:
                    mdata[minutes] = state

            # Store previous time & state
            if to_time and not backwards:
                prev_last_updated_time = to_time
            else:
                prev_last_updated_time = last_updated_time
            last_state = state

        # If we only have a start time then fill the gaps with the last values
        if not to_key:
            state = newest_state
            for minute in range(0, 60 * 24 * days):
                rindex = 60 * 24 * days - minute - 1
                state = mdata.get(rindex, state)
                mdata[rindex] = state
                minute += 1

        # Reverse data with smoothing
        if clean_increment:
            mdata = self.clean_incrementing_reverse(mdata, max_increment)

        # Accumulate to previous data?
        if accumulate:
            for minute in range(0, 60 * 24 * days):
                if minute in mdata:
                    mdata[minute] += accumulate.get(minute, 0)
                else:
                    mdata[minute] = accumulate.get(minute, 0)

        if adjust_key:
            self.io_adjusted = adata
        return mdata

    def minutes_since_yesterday(self, now):
        """
        Calculate the number of minutes since 23:59 yesterday
        """
        yesterday = now - timedelta(days=1)
        yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
        difference = now - yesterday_at_2359
        difference_minutes = int((difference.seconds + 59) / 60)
        return difference_minutes

    def dp0(self, value):
        """
        Round to 0 decimal places
        """
        return round(value)

    def dp1(self, value):
        """
        Round to 1 decimal place
        """
        return round(value * 10) / 10

    def dp2(self, value):
        """
        Round to 2 decimal places
        """
        return round(value * 100) / 100

    def dp3(self, value):
        """
        Round to 3 decimal places
        """
        return round(value * 1000) / 1000

    def hit_charge_window(self, charge_window, start, end):
        window_n = 0
        for window in charge_window:
            if end > window["start"] and start <= window["end"]:
                return window_n
            window_n += 1
        return -1

    def in_charge_window(self, charge_window, minute_abs):
        """
        Work out if this minute is within the a charge window
        """
        window_n = 0
        for window in charge_window:
            if minute_abs >= window["start"] and minute_abs < window["end"]:
                return window_n
            window_n += 1
        return -1

    def clean_incrementing_reverse(self, data, max_increment=0):
        """
        Cleanup an incrementing sensor data that runs backwards in time to remove the
        resets (where it goes back to 0) and make it always increment
        """
        new_data = {}
        length = max(data) + 1

        increment = 0
        last = data[length - 1]

        for index in range(0, length):
            rindex = length - index - 1
            nxt = data.get(rindex, last)
            if nxt >= last:
                if (max_increment > 0) and ((nxt - last) > max_increment):
                    # Smooth out big spikes
                    pass
                else:
                    increment += nxt - last
            last = nxt
            new_data[rindex] = increment

        return new_data

    def get_filtered_load_minute(self, data, minute_previous, historical, step=1):
        """
        Gets a previous load minute after filtering for car charging
        """
        load_yesterday_raw = 0

        for offset in range(0, step):
            if historical:
                load_yesterday_raw += self.get_historical(data, minute_previous + offset)
            else:
                load_yesterday_raw += self.get_from_incrementing(data, minute_previous + offset)

        load_yesterday = load_yesterday_raw
        # Car charging hold
        if self.car_charging_hold and self.car_charging_energy:
            # Hold based on data
            car_energy = 0
            for offset in range(0, step):
                if historical:
                    car_energy += self.get_historical(self.car_charging_energy, minute_previous + offset)
                else:
                    car_energy += self.get_from_incrementing(self.car_charging_energy, minute_previous + offset)
            load_yesterday = max(0, load_yesterday - car_energy)
        elif self.car_charging_hold and (load_yesterday >= (self.car_charging_threshold * step)):
            # Car charging hold - ignore car charging in computation based on threshold
            load_yesterday = max(load_yesterday - (self.car_charging_rate[0] * step / 60.0), 0)

        return load_yesterday, load_yesterday_raw

    def previous_days_modal_filter(self, data):
        """
        Look at the data from previous days and discard the best case one
        """

        total_points = len(self.days_previous)
        sum_days = []
        sum_days_id = {}
        min_sum = 99999999
        min_sum_day = 0

        idx = 0
        for days in self.days_previous:
            use_days = min(days, self.load_minutes_age)
            sum_day = 0
            if use_days > 0:
                full_days = 24 * 60 * (use_days - 1)
                for minute in range(0, 24 * 60, PREDICT_STEP):
                    minute_previous = 24 * 60 - minute + full_days
                    load_yesterday, load_yesterday_raw = self.get_filtered_load_minute(data, minute_previous, historical=False, step=PREDICT_STEP)
                    sum_day += load_yesterday
            sum_days.append(self.dp2(sum_day))
            sum_days_id[days] = sum_day
            if sum_day < min_sum:
                min_sum_day = days
                min_sum_day_idx = idx
                min_sum = self.dp2(sum_day)
            idx += 1

        self.log("Historical data totals for days {} are {} - min {}".format(self.days_previous, sum_days, min_sum))
        if self.load_filter_modal and total_points >= 3 and (min_sum_day > 0):
            self.log("Model filter enabled - Discarding day {} as it is the lowest of the {} datapoints".format(min_sum_day, len(self.days_previous)))
            del self.days_previous[min_sum_day_idx]
            del self.days_previous_weight[min_sum_day_idx]

        # Gap filling
        gap_size = max(self.get_arg("load_filter_threshold", 30), 5)
        for days in self.days_previous:
            use_days = min(days, self.load_minutes_age)
            num_gaps = 0
            if use_days > 0:
                full_days = 24 * 60 * (use_days - 1)
                for minute in range(0, 24 * 60, PREDICT_STEP):
                    minute_previous = 24 * 60 - minute + full_days
                    if data.get(minute_previous, 0) == data.get(minute_previous + gap_size, 0):
                        num_gaps += PREDICT_STEP

                # If we have some gaps
                if num_gaps > 0:
                    average_day = sum_days_id[days]
                    if (average_day == 0) or (num_gaps >= 24 * 60):
                        self.log("WARN: Historical day {} has no data, unable to fill gaps normally using nominal 24kWh - you should fix your system!".format(days))
                        average_day = 24.0
                    else:
                        real_data_percent = ((24 * 60) - num_gaps) / (24 * 60)
                        average_day /= real_data_percent
                        self.log(
                            "WARN: Historical day {} has {} minutes of gap in the data, filled from {} kWh to make new average {} kWh (percent {}%)".format(
                                days, num_gaps, self.dp2(sum_days_id[days]), self.dp2(average_day), self.dp0(real_data_percent * 100.0)
                            )
                        )

                    # Do the filling
                    per_minute_increment = average_day / (24 * 60)
                    for minute in range(0, 24 * 60, PREDICT_STEP):
                        minute_previous = 24 * 60 - minute + full_days
                        if data.get(minute_previous, 0) == data.get(minute_previous + gap_size, 0):
                            for offset in range(minute_previous, 0, -1):
                                if offset in data:
                                    data[offset] += per_minute_increment * PREDICT_STEP
                                else:
                                    data[offset] = per_minute_increment * PREDICT_STEP

    def get_historical(self, data, minute):
        """
        Get historical data across N previous days in days_previous array based on current minute
        """
        total = 0
        total_weight = 0
        this_point = 0

        # No data?
        if not data:
            return 0

        for days in self.days_previous:
            use_days = min(days, self.load_minutes_age)
            weight = self.days_previous_weight[this_point]
            if use_days > 0:
                full_days = 24 * 60 * (use_days - 1)
                minute_previous = 24 * 60 - minute + full_days
                value = self.get_from_incrementing(data, minute_previous)
                total += value * weight
                total_weight += weight
            this_point += 1

        # Zero data?
        if total_weight == 0:
            return 0
        else:
            return total / total_weight

    def get_from_incrementing(self, data, index, backwards=True):
        """
        Get a single value from an incrementing series e.g. kWh today -> kWh this minute
        """
        while index < 0:
            index += 24 * 60
        if backwards:
            return data.get(index, 0) - data.get(index + 1, 0)
        else:
            return data.get(index + 1, 0) - data.get(index, 0)

    def record_length(self, charge_window, charge_limit, best_price):
        """
        Limit the forecast length to either the total forecast duration or the start of the last window that falls outside the forecast
        """
        next_charge_start = self.forecast_minutes + self.minutes_now
        if charge_window:
            for window_n in range(0, len(charge_window)):
                if charge_limit[window_n] > 0 and charge_window[window_n]["average"] <= best_price:
                    next_charge_start = charge_window[window_n]["start"]
                    if next_charge_start < self.minutes_now:
                        next_charge_start = charge_window[window_n]["end"]
                    break

        end_record = min(self.forecast_plan_hours * 60 + next_charge_start, self.forecast_minutes + self.minutes_now)
        max_windows = self.max_charge_windows(end_record, charge_window)
        if len(charge_window) > max_windows:
            end_record = min(end_record, charge_window[max_windows]["start"])
            # If we are within this window then push to the end of it
            if end_record < self.minutes_now:
                end_record = charge_window[max_windows]["end"]

        self.log("Calculated end_record as {}".format(self.time_abs_str(end_record)))
        return end_record - self.minutes_now

    def max_charge_windows(self, end_record_abs, charge_window):
        """
        Work out how many charge windows the time period covers
        """
        charge_windows = 0
        window_n = 0
        for window in charge_window:
            if end_record_abs >= window["end"]:
                charge_windows = window_n + 1
            window_n += 1
        return charge_windows

    def record_status(self, message, debug="", had_errors=False, notify=False, extra=""):
        """
        Records status to HA sensor
        """
        if not extra:
            extra = ""

        if notify and self.previous_status != message and self.set_status_notify:
            self.call_notify("Predbat status change to: " + message + extra)

        self.dashboard_item(
            self.prefix + ".status",
            state=message,
            attributes={"friendly_name": "Status", "icon": "mdi:information", "last_updated": datetime.now(), "debug": debug, "version": THIS_VERSION},
        )
        self.previous_status = message
        if had_errors:
            self.had_errors = True

    def scenario_summary_title(self, record_time):
        txt = ""
        for minute in range(0, self.forecast_minutes, 60):
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
            dstamp = minute_timestamp.strftime(TIME_FORMAT)
            stamp = minute_timestamp.strftime("%H:%M")
            if record_time[dstamp] > 0:
                break
            if txt:
                txt += ", "
            txt += "%7s" % str(stamp)
        return txt

    def scenario_summary(self, record_time, datap):
        txt = ""
        for minute in range(0, self.forecast_minutes, 60):
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = datap[stamp]
            if not isinstance(value, str):
                value = self.dp2(value)
            if record_time[stamp] > 0:
                break
            if txt:
                txt += ", "
            txt += "%7s" % str(value)
        return txt

    def load_today_comparison(self, load_minutes, load_forecast, car_minutes, import_minutes, minutes_now, step=5):
        """
        Compare predicted vs actual load
        """
        load_total_pred = 0
        load_total_pred_now = 0
        car_total_pred = 0
        car_total_actual = 0
        car_value_pred = 0
        car_value_actual = 0
        actual_total_now = 0
        actual_total_today = 0
        import_ignored_load_pred = 0
        import_ignored_load_actual = 0
        load_predict_stamp = {}
        load_actual_stamp = {}
        load_predict_data = {}
        total_forecast_value_pred = 0
        total_forecast_value_pred_now = 0

        for minute in range(0, 24 * 60, step):
            import_value_today = 0
            load_value_today = 0
            load_value_today_raw = 0

            if minute < minutes_now:
                for offset in range(0, step):
                    import_value_today += self.get_from_incrementing(import_minutes, minutes_now - minute - offset - 1)
                load_value_today, load_value_today_raw = self.get_filtered_load_minute(load_minutes, minutes_now - minute - 1, historical=False, step=step)

            import_value_pred = 0
            forecast_value_pred = 0
            for offset in range(0, step):
                import_value_pred += self.get_historical(import_minutes, minute - minutes_now + offset)
                forecast_value_pred += self.get_from_incrementing(load_forecast, minute + offset, backwards=False)

            load_value_pred, load_value_pred_raw = self.get_filtered_load_minute(load_minutes, minute - minutes_now, historical=True, step=step)

            # Ignore periods of import as assumed to be deliberate (battery charging periods overnight for example)
            car_value_actual = load_value_today_raw - load_value_today
            if import_value_today >= load_value_today_raw:
                import_ignored_load_actual += load_value_today
                load_value_today = 0

            # Ignore periods of import as assumed to be deliberate (battery charging periods overnight for example)
            car_value_pred = load_value_pred_raw - load_value_pred
            if import_value_pred >= load_value_pred_raw:
                import_ignored_load_pred += load_value_pred
                load_value_pred = 0

            # Add in forecast load
            load_value_pred += forecast_value_pred

            # Only count totals until now
            if minute < minutes_now:
                load_total_pred_now += load_value_pred
                car_total_pred += car_value_pred
                actual_total_now += load_value_today
                car_total_actual += car_value_actual
                actual_total_today += load_value_today
                total_forecast_value_pred_now += forecast_value_pred
            else:
                actual_total_today += load_value_pred

            load_total_pred += load_value_pred
            total_forecast_value_pred += forecast_value_pred

            load_predict_data[minute] = load_value_pred

            # Store for charts
            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                load_predict_stamp[stamp] = self.dp3(load_total_pred)
                load_actual_stamp[stamp] = self.dp3(actual_total_today)

        difference = 1.0
        if minutes_now >= 180 and actual_total_now >= 1.0 and actual_total_today > 0.0:
            # Make a ratio only if we have enough data to consider the outcome
            difference = 1.0 + ((actual_total_today - load_total_pred) / actual_total_today)

        # Work out divergence
        if not self.calculate_inday_adjustment:
            difference_cap = 1.0
        else:
            # Apply damping factor to adjustment
            difference_cap = (difference - 1.0) * self.metric_inday_adjust_damping + 1.0

            # Cap adjustment within 1/2 to 2x
            difference_cap = max(difference_cap, 0.5)
            difference_cap = min(difference_cap, 2.0)

        self.log(
            "Today's load divergence {} % in-day adjustment {} % damping {}x, Predicted so far {} kWh with {} kWh car excluded and {} kWh import ignored and {} forecast extra, Actual so far {} kWh with {} kWh car excluded and {} kWh import ignored".format(
                self.dp2(difference * 100.0),
                self.dp2(difference_cap * 100.0),
                self.metric_inday_adjust_damping,
                self.dp2(load_total_pred_now),
                self.dp2(car_total_pred),
                self.dp2(import_ignored_load_pred),
                self.dp2(total_forecast_value_pred_now),
                self.dp2(actual_total_now),
                self.dp2(car_total_actual),
                self.dp2(import_ignored_load_actual),
            )
        )

        # Create adjusted curve
        load_adjusted_stamp = {}
        load_adjusted = actual_total_now
        for minute in range(0, 24 * 60, step):
            if minute >= minutes_now:
                load = load_predict_data[minute] * difference_cap
                load_adjusted += load
                if (minute % 10) == 0:
                    minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute)
                    stamp = minute_timestamp.strftime(TIME_FORMAT)
                    load_adjusted_stamp[stamp] = load_adjusted

        self.dashboard_item(
            self.prefix + ".load_inday_adjustment",
            state=self.dp2(difference_cap * 100.0),
            attributes={
                "damping": self.metric_inday_adjust_damping,
                "friendly_name": "Load in-day adjustment factor",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "icon": "mdi:percent",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_energy_actual",
            state=self.dp3(actual_total_today),
            attributes={
                "results": load_actual_stamp,
                "friendly_name": "Load energy actual (filtered)",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_energy_predicted",
            state=self.dp3(load_total_pred),
            attributes={
                "results": load_predict_stamp,
                "friendly_name": "Load energy predicted (filtered)",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_energy_adjusted",
            state=self.dp3(load_adjusted),
            attributes={
                "results": load_adjusted_stamp,
                "friendly_name": "Load energy prediction adjusted",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )

        return difference_cap

    def get_cloud_factor(self, minutes_now, pv_data, pv_data10):
        """
        Work out approximated cloud factor
        """
        pv_total = 0
        pv_total10 = 0
        for minute in range(0, self.forecast_minutes):
            pv_total += pv_data.get(minute + minutes_now, 0.0)
            pv_total10 += pv_data10.get(minute + minutes_now, 0.0)

        pv_factor = None
        if pv_total > 0 and (pv_total > pv_total10):
            pv_diff = pv_total - pv_total10
            pv_factor = self.dp2(pv_diff / pv_total)
            pv_factor = min(pv_factor, 1.0)

        if self.metric_cloud_enable:
            pv_factor_round = pv_factor
            if pv_factor_round:
                pv_factor_round = self.dp1(pv_factor_round)
            self.log("PV Forecast {} kWh and 10% Forecast {} kWh pv cloud factor {}".format(self.dp1(pv_total), self.dp1(pv_total10), pv_factor_round))
            return pv_factor
        else:
            return None

    def step_data_history(self, item, minutes_now, forward, step=PREDICT_STEP, scale_today=1.0, type_load=False, load_forecast={}, cloud_factor=None):
        """
        Create cached step data for historical array
        """
        values = {}
        cloud_diff = 0
        for minute in range(0, self.forecast_minutes, step):
            value = 0
            minute_absolute = minute + minutes_now

            # Reset in-day adjustment for tomorrow
            if (minute + minutes_now) > 24 * 60:
                scale_today = 1.0

            if type_load and not forward:
                load_yesterday, load_yesterday_raw = self.get_filtered_load_minute(item, minute, historical=True, step=step)
                value += load_yesterday
            else:
                for offset in range(0, step):
                    if forward:
                        value += item.get(minute + minutes_now + offset, 0.0)
                    else:
                        value += self.get_historical(item, minute + offset)

            # Extra load adding in (e.g. heat pump)
            load_extra = 0
            if load_forecast:
                for offset in range(0, step):
                    load_extra += self.get_from_incrementing(load_forecast, minute_absolute, backwards=False)
            values[minute] = value * scale_today + load_extra

            # Simple cloud model keeps the same generation but brings PV generation up and down every 5 minutes
            if cloud_factor and cloud_factor > 0:
                cloud_on = int(minute / 5) % 2
                if cloud_on > 0:
                    cloud_diff += values[minute] * cloud_factor
                    values[minute] += cloud_diff
                else:
                    cloud_diff = min(cloud_diff, values[minute])
                    values[minute] -= cloud_diff
                    cloud_diff = 0

        return values

    def calc_percent_limit(self, charge_limit):
        """
        Calculate a charge limit in percent
        """
        if isinstance(charge_limit, list):
            if self.soc_max <= 0:
                return [0 for i in range(0, len(charge_limit))]
            else:
                return [min(int((float(charge_limit[i]) / self.soc_max * 100.0) + 0.5), 100) for i in range(0, len(charge_limit))]
        else:
            if self.soc_max <= 0:
                return 0
            else:
                return min(int((float(charge_limit) / self.soc_max * 100.0) + 0.5), 100)

    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute_step, end_record, save=None, step=PREDICT_STEP):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """
        predict_soc = {}
        predict_export = {}
        predict_battery_power = {}
        predict_battery_cycle = {}
        predict_soc_time = {}
        predict_car_soc_time = [{} for car_n in range(0, self.num_cars)]
        predict_pv_power = {}
        predict_state = {}
        predict_grid_power = {}
        predict_load_power = {}
        predict_iboost = {}
        minute = 0
        minute_left = self.forecast_minutes
        soc = self.soc_kw
        soc_min = self.soc_max
        soc_min_minute = self.minutes_now
        charge_has_run = False
        charge_has_started = False
        discharge_has_run = False
        export_kwh = self.export_today_now
        export_kwh_h0 = export_kwh
        import_kwh = self.import_today_now
        import_kwh_h0 = import_kwh
        load_kwh = self.load_minutes_now
        load_kwh_h0 = load_kwh
        pv_kwh = self.pv_today_now
        pv_kwh_h0 = pv_kwh
        iboost_today_kwh = self.iboost_today
        import_kwh_house = 0
        import_kwh_battery = 0
        battery_cycle = 0
        metric_keep = 0
        final_export_kwh = export_kwh
        final_import_kwh = import_kwh
        final_load_kwh = load_kwh
        final_pv_kwh = pv_kwh
        final_iboost_kwh = iboost_today_kwh
        final_import_kwh_house = import_kwh_house
        final_import_kwh_battery = import_kwh_battery
        final_battery_cycle = battery_cycle
        final_metric_keep = metric_keep
        metric = self.cost_today_sofar
        final_soc = soc
        first_charge_soc = soc
        prev_soc = soc
        final_metric = metric
        metric_time = {}
        load_kwh_time = {}
        pv_kwh_time = {}
        export_kwh_time = {}
        import_kwh_time = {}
        record_time = {}
        car_soc = self.car_charging_soc[:]
        final_car_soc = car_soc[:]
        charge_rate_now = self.charge_rate_now
        discharge_rate_now = self.discharge_rate_now
        battery_state = "-"
        grid_state = "-"
        first_charge = end_record
        export_to_first_charge = 0

        # self.log("Sim discharge window {} enable {}".format(discharge_window, discharge_limits))
        charge_limit, charge_window = self.remove_intersecting_windows(charge_limit, charge_window, discharge_limits, discharge_window)

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        record = True

        # Simulate each forward minute
        while minute < self.forecast_minutes:
            # Minute yesterday can wrap if days_previous is only 1
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
            charge_window_n = self.in_charge_window(charge_window, minute_absolute)
            charge_limit_n = 0
            prev_soc = soc

            reserve_expected = self.reserve
            if charge_window_n >= 0:
                charge_limit_n = charge_limit[charge_window_n]
                if charge_limit_n == 0:
                    charge_window_n = -1
                else:
                    if self.set_charge_freeze and (charge_limit_n == self.reserve):
                        # Charge freeze via reserve
                        charge_limit_n = soc
                    if self.set_reserve_enable:
                        reserve_expected = max(charge_limit_n, self.reserve)

            discharge_window_n = self.in_charge_window(discharge_window, minute_absolute)

            # Add in standing charge, only for the final plan when we save the results
            if (minute_absolute % (24 * 60)) < step and save:
                metric += self.metric_standing_charge

            # Outside the recording window?
            if minute >= end_record and record:
                record = False

            # Store data before the next simulation step to align timestamps
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if (minute % 10) == 0:
                predict_soc_time[stamp] = self.dp3(soc)
                metric_time[stamp] = self.dp2(metric)
                load_kwh_time[stamp] = self.dp3(load_kwh)
                pv_kwh_time[stamp] = self.dp2(pv_kwh)
                import_kwh_time[stamp] = self.dp2(import_kwh)
                export_kwh_time[stamp] = self.dp2(export_kwh)
                for car_n in range(0, self.num_cars):
                    predict_car_soc_time[car_n][stamp] = self.dp2(car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0)
                record_time[stamp] = 0 if record else self.soc_max
                predict_iboost[stamp] = iboost_today_kwh

            # Save Soc prediction data as minutes for later use
            self.predict_soc[minute] = self.dp3(soc)
            if save and save == "best":
                self.predict_soc_best[minute] = self.dp3(soc)
                self.predict_metric_best[minute] = self.dp2(metric)
                self.predict_iboost_best[minute] = iboost_today_kwh

            # Get load and pv forecast, total up for all values in the step
            pv_now = pv_forecast_minute_step[minute]
            load_yesterday = load_minutes_step[minute]

            # Count PV kWh
            pv_kwh += pv_now
            if record:
                final_pv_kwh = pv_kwh

            # Simulate car charging
            car_load = self.in_car_slot(minute_absolute)

            # Car charging?
            car_freeze = False
            for car_n in range(0, self.num_cars):
                if car_load[car_n] > 0.0:
                    car_load_scale = car_load[car_n] * step / 60.0
                    car_load_scale = car_load_scale * self.car_charging_loss
                    car_load_scale = max(min(car_load_scale, self.car_charging_limit[car_n] - car_soc[car_n]), 0)
                    car_soc[car_n] += car_load_scale
                    load_yesterday += car_load_scale / self.car_charging_loss
                    # Model not allowing the car to charge from the battery
                    if not self.car_charging_from_battery:
                        discharge_rate_now = self.battery_rate_min  # 0
                        car_freeze = True

            # Reset modelled discharge rate if no car is charging
            if not self.car_charging_from_battery and not car_freeze:
                discharge_rate_now = self.battery_rate_max_discharge_scaled

            # IBoost on load
            if self.iboost_enable:
                iboost_amount = 0
                if iboost_today_kwh < self.iboost_max_energy:
                    if self.iboost_gas:
                        if self.rate_gas:
                            # Iboost on cheap electric rates
                            gas_rate = self.rate_gas.get(minute_absolute, 99) * self.iboost_gas_scale
                            electric_rate = self.rate_import.get(minute_absolute, 0)
                            if (electric_rate < gas_rate) and (charge_window_n >= 0 or not self.iboost_charging):
                                iboost_amount = self.iboost_max_power * step
                                load_yesterday += iboost_amount
                    elif self.iboost_charging:
                        if charge_window_n >= 0:
                            iboost_amount = self.iboost_max_power * step
                            load_yesterday += iboost_amount

            # Count load
            load_kwh += load_yesterday
            if record:
                final_load_kwh = load_kwh

            # Work out how much PV is used to satisfy home demand
            pv_ac = min(load_yesterday / self.inverter_loss, pv_now, self.inverter_limit * step)

            # And hence how much maybe left for DC charging
            pv_dc = pv_now - pv_ac

            # Scale down PV AC and DC for inverter loss (if hybrid we will reverse the DC loss later)
            pv_ac *= self.inverter_loss
            pv_dc *= self.inverter_loss

            # IBoost model
            if self.iboost_enable:
                if iboost_today_kwh < self.iboost_max_energy:
                    if self.iboost_solar and pv_dc > (self.iboost_min_power * step) and ((soc * 100.0 / self.soc_max) >= self.iboost_min_soc):
                        # Iboost on solar import
                        iboost_amount = min(pv_dc, self.iboost_max_power * step)
                        pv_dc -= iboost_amount

                # Cumulative energy
                iboost_today_kwh += iboost_amount

                # Model Iboost reset
                if (minute_absolute % (24 * 60)) >= (23 * 60 + 30):
                    iboost_today_kwh = 0

                # Save Iboost next prediction
                if minute == 0 and save == "best":
                    scaled_boost = (iboost_amount / step) * RUN_EVERY
                    self.iboost_next = self.dp3(self.iboost_today + scaled_boost)
                    if iboost_amount > 0:
                        self.iboost_running = True
                    else:
                        self.iboost_running = False
                    self.log(
                        "IBoost model predicts usage {} in this run period taking total to {} solar {} gas {} charging {}".format(
                            self.dp2(scaled_boost), self.iboost_next, self.iboost_solar, self.iboost_gas, self.iboost_charging
                        )
                    )

            # discharge freeze?
            if self.set_discharge_freeze:
                charge_rate_now = self.battery_rate_max_charge_scaled
                if (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0:
                    # Freeze mode
                    charge_rate_now = self.battery_rate_min  # 0

            # Set discharge during charge?
            if not self.set_discharge_during_charge:
                if (charge_window_n >= 0) and (soc >= charge_limit_n):
                    discharge_rate_now = self.battery_rate_min  # 0
                elif not car_freeze:
                    # Reset discharge rate
                    discharge_rate_now = self.battery_rate_max_discharge_scaled

            # Battery behaviour
            battery_draw = 0
            soc_percent = self.calc_percent_limit(soc)
            charge_rate_now_curve = charge_rate_now * self.battery_charge_power_curve.get(soc_percent, 1.0)
            if (
                not self.set_discharge_freeze_only
                and (discharge_window_n >= 0)
                and discharge_limits[discharge_window_n] < 100.0
                and (soc - step * self.battery_rate_max_discharge_scaled) > (self.soc_max * discharge_limits[discharge_window_n] / 100.0)
            ):
                # Discharge enable
                discharge_rate_now = self.battery_rate_max_discharge_scaled  # Assume discharge becomes enabled here

                # It's assumed if SOC hits the expected reserve then it's terminated
                reserve_expected = max((self.soc_max * discharge_limits[discharge_window_n]) / 100.0, self.reserve)
                battery_draw = discharge_rate_now * step
                if (soc - reserve_expected) < battery_draw:
                    battery_draw = max(soc - reserve_expected, 0)

                # Account for export limit, clip battery draw if possible to avoid going over
                diff_tmp = load_yesterday - (battery_draw + pv_dc + pv_ac)
                if diff_tmp < 0:
                    if abs(diff_tmp) > (self.export_limit * step):
                        above_limit = abs(diff_tmp + self.export_limit * step)
                        battery_draw = max(0, battery_draw - above_limit)

                # Account for inverter limit, clip battery draw if possible to avoid going over
                total_inverted = pv_ac + pv_dc + battery_draw
                if total_inverted > self.inverter_limit * step:
                    reduce_by = total_inverted - (self.inverter_limit * step)
                    battery_draw = max(0, battery_draw - reduce_by)

                battery_state = "f-"
            elif (charge_window_n >= 0) and soc < charge_limit_n:
                # Charge enable
                if save in ["best", "best10"]:
                    # Only tune charge rate on final plan not every simulation
                    charge_rate_now = (
                        self.find_charge_rate(minute_absolute, soc, charge_window[charge_window_n], charge_limit_n, self.battery_rate_max_charge) * self.battery_rate_max_scaling
                    )
                else:
                    charge_rate_now = self.battery_rate_max_charge_scaled  # Assume charge becomes enabled here

                # Apply the charging curve
                charge_rate_now_curve = charge_rate_now * self.battery_charge_power_curve.get(soc_percent, 1.0)

                # Remove inverter loss as it will be added back in again when calculating the SOC change
                charge_rate_now_curve /= self.inverter_loss
                battery_draw = -max(min(charge_rate_now_curve * step, charge_limit_n - soc), 0)
                battery_state = "f+"
                first_charge = min(first_charge, minute)
            else:
                # ECO Mode
                if load_yesterday - pv_ac - pv_dc > 0:
                    battery_draw = min(load_yesterday - pv_ac - pv_dc, discharge_rate_now * step, self.inverter_limit * step - pv_ac)
                    battery_state = "e-"
                else:
                    battery_draw = max(load_yesterday - pv_ac - pv_dc, -charge_rate_now_curve * step)
                    if battery_draw < 0:
                        battery_state = "e+"
                    else:
                        battery_state = "e~"

            # Clamp battery at reserve for discharge
            if battery_draw > 0:
                # All battery discharge must go through the inverter too
                soc -= battery_draw / (self.battery_loss_discharge * self.inverter_loss)
                if soc < reserve_expected:
                    battery_draw -= (reserve_expected - soc) * self.battery_loss_discharge * self.inverter_loss
                    soc = reserve_expected

            # Clamp battery at max when charging
            if battery_draw < 0:
                battery_draw_dc = max(-pv_dc, battery_draw)
                battery_draw_ac = battery_draw - battery_draw_dc

                if self.inverter_hybrid:
                    inverter_loss = self.inverter_loss
                else:
                    inverter_loss = 1.0

                # In the hybrid case only we remove the inverter loss for PV charging (as it's DC to DC), and inverter loss was already applied
                soc -= battery_draw_dc * self.battery_loss / inverter_loss
                if soc > self.soc_max:
                    battery_draw_dc += ((soc - self.soc_max) / self.battery_loss) * inverter_loss
                    soc = self.soc_max

                # The rest of this charging must be from the grid (pv_dc was the left over PV)
                soc -= battery_draw_ac * self.battery_loss * self.inverter_loss
                if soc > self.soc_max:
                    battery_draw_ac += (soc - self.soc_max) / (self.battery_loss * self.inverter_loss)
                    soc = self.soc_max

                # if (minute % 30) == 0:
                #    self.log("Minute {} pv_ac {} pv_dc {} battery_ac {} battery_dc {} battery b4 {} after {} soc {}".format(minute, pv_ac, pv_dc, battery_draw_ac, battery_draw_dc, battery_draw, battery_draw_ac + battery_draw_dc, soc))

                battery_draw = battery_draw_ac + battery_draw_dc

            # Count battery cycles
            battery_cycle += abs(battery_draw)

            # Work out left over energy after battery adjustment
            diff = load_yesterday - (battery_draw + pv_dc + pv_ac)
            if diff < 0:
                # Can not export over inverter limit, load must be taken out first from the inverter limit
                # All exports must come from PV or from the battery, so inverter loss is already accounted for in both cases
                inverter_left = self.inverter_limit * step - load_yesterday
                if inverter_left < 0:
                    diff += -inverter_left
                else:
                    diff = max(diff, -inverter_left)
            if diff < 0:
                # Can not export over export limit, so cap at that
                diff = max(diff, -self.export_limit * step)

            # Metric keep - pretend the battery is empty and you have to import instead of using the battery
            if (soc < self.best_soc_keep) and (soc > self.reserve):
                # Apply keep as a percentage of the time in the future so it gets stronger over an 4 hour period
                # Weight to 50% chance of the scenario
                if battery_draw > 0:
                    minute_scaling = min((minute / (4 * 60)), 1.0) * 0.5
                    metric_keep += self.rate_import[minute_absolute] * battery_draw * minute_scaling
            elif soc < self.best_soc_keep:
                # It seems odd but the reason to add in metric keep when the battery is empty because otherwise you weight an empty battery quite heavily
                # and end up forcing it all to zero
                minute_scaling = min((minute / (4 * 60)), 1.0) * 0.5
                keep_diff = load_yesterday - (0 + pv_dc + pv_ac)
                if keep_diff > 0:
                    metric_keep += self.rate_import[minute_absolute] * keep_diff * minute_scaling

            if diff > 0:
                # Import
                # All imports must go to home (no inverter loss) or to the battery (inverter loss accounted before above)
                import_kwh += diff
                if charge_window_n >= 0:
                    # If the battery is on charge anyhow then imports are at battery charging rate
                    import_kwh_battery += diff
                else:
                    # self.log("importing to minute %s amount %s kW total %s kWh total draw %s" % (minute, energy, import_kwh_house, diff))
                    import_kwh_house += diff

                metric += self.rate_import[minute_absolute] * diff
                grid_state = "<"
            else:
                # Export
                energy = -diff
                export_kwh += energy
                if minute_absolute in self.rate_export:
                    metric -= self.rate_export[minute_absolute] * energy
                if diff != 0:
                    grid_state = ">"
                else:
                    grid_state = "~"

            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = min(minute, minute_left)

            # Record final soc & metric
            if record:
                final_soc = soc
                for car_n in range(0, self.num_cars):
                    final_car_soc[car_n] = car_soc[car_n]

                final_metric = metric
                final_import_kwh = import_kwh
                final_import_kwh_battery = import_kwh_battery
                final_import_kwh_house = import_kwh_house
                final_export_kwh = export_kwh
                final_iboost_kwh = iboost_today_kwh
                final_battery_cycle = battery_cycle
                final_metric_keep = metric_keep

                # Store export data
                if diff < 0:
                    predict_export[minute] = energy
                    if minute <= first_charge:
                        export_to_first_charge += energy
                else:
                    predict_export[minute] = 0

                # Soc at next charge start
                if minute <= first_charge:
                    first_charge_soc = prev_soc

            # Have we past the charging or discharging time?
            if charge_window_n >= 0:
                charge_has_started = True
            if charge_has_started and (charge_window_n < 0):
                charge_has_run = True
            if (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0:
                discharge_has_run = True

            # Record soc min
            if record and (discharge_has_run or charge_has_run or not charge_window):
                if soc < soc_min:
                    soc_min_minute = minute_absolute
                soc_min = min(soc_min, soc)

            # Record state
            if (minute % 10) == 0:
                predict_state[stamp] = "g" + grid_state + "b" + battery_state
                predict_battery_power[stamp] = self.dp3(battery_draw * (60 / step))
                predict_battery_cycle[stamp] = self.dp3(battery_cycle)
                predict_pv_power[stamp] = self.dp3((pv_forecast_minute_step[minute] + pv_forecast_minute_step[minute + step]) * (30 / step))
                predict_grid_power[stamp] = self.dp3(diff * (60 / step))
                predict_load_power[stamp] = self.dp3(load_yesterday * (60 / step))

            minute += step

        hours_left = minute_left / 60.0

        if self.debug_enable or save:
            self.log(
                "predict {} end_record {} final soc {} kWh metric {} p metric_keep {} min_soc {} @ {} kWh load {} pv {}".format(
                    save,
                    self.time_abs_str(end_record + self.minutes_now),
                    self.dp2(final_soc),
                    self.dp2(final_metric),
                    self.dp2(final_metric_keep),
                    self.dp2(soc_min),
                    self.time_abs_str(soc_min_minute),
                    self.dp2(final_load_kwh),
                    self.dp2(final_pv_kwh),
                )
            )
            self.log("         [{}]".format(self.scenario_summary_title(record_time)))
            self.log("    SOC: [{}]".format(self.scenario_summary(record_time, predict_soc_time)))
            self.log("  STATE: [{}]".format(self.scenario_summary(record_time, predict_state)))
            self.log("   LOAD: [{}]".format(self.scenario_summary(record_time, load_kwh_time)))
            self.log("     PV: [{}]".format(self.scenario_summary(record_time, pv_kwh_time)))
            self.log(" IMPORT: [{}]".format(self.scenario_summary(record_time, import_kwh_time)))
            self.log(" EXPORT: [{}]".format(self.scenario_summary(record_time, export_kwh_time)))
            if self.iboost_enable:
                self.log(" IBOOST: [{}]".format(self.scenario_summary(record_time, predict_iboost)))
            for car_n in range(0, self.num_cars):
                self.log("   CAR{}: [{}]".format(car_n, self.scenario_summary(record_time, predict_car_soc_time[car_n])))
            self.log(" METRIC: [{}]".format(self.scenario_summary(record_time, metric_time)))

        # Save data to HA state
        if save and save == "base" and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".battery_hours_left",
                state=self.dp2(hours_left),
                attributes={"friendly_name": "Predicted Battery Hours left", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
            )
            postfix = ""
            for car_n in range(0, self.num_cars):
                if car_n > 0:
                    postfix = "_" + str(car_n)
                self.dashboard_item(
                    self.prefix + ".car_soc" + postfix,
                    state=self.dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                    attributes={
                        "results": predict_car_soc_time[car_n],
                        "friendly_name": "Car " + str(car_n) + " battery SOC",
                        "state_class": "measurement",
                        "unit_of_measurement": "%",
                        "icon": "mdi:battery",
                    },
                )
            self.dashboard_item(
                self.prefix + ".soc_kw_h0",
                state=self.dp3(self.predict_soc[0]),
                attributes={"friendly_name": "Current SOC kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
            )
            self.dashboard_item(
                self.prefix + ".soc_kw",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_soc_time,
                    "friendly_name": "Predicted SOC kWh",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "first_charge_kwh": first_charge_soc,
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".battery_power",
                state=self.dp3(0),
                attributes={
                    "results": predict_battery_power,
                    "friendly_name": "Predicted Battery Power",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".battery_cycle",
                state=self.dp3(final_battery_cycle),
                attributes={
                    "results": predict_battery_cycle,
                    "friendly_name": "Predicted Battery Cycle",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".pv_power",
                state=self.dp3(0),
                attributes={"results": predict_pv_power, "friendly_name": "Predicted PV Power", "state_class": "measurement", "unit_of_measurement": "kW", "icon": "mdi:battery"},
            )
            self.dashboard_item(
                self.prefix + ".grid_power",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_grid_power,
                    "friendly_name": "Predicted Grid Power",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".load_power",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_load_power,
                    "friendly_name": "Predicted Load Power",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".soc_min_kwh",
                state=self.dp3(soc_min),
                attributes={
                    "time": self.time_abs_str(soc_min_minute),
                    "friendly_name": "Predicted minimum SOC best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-arrow-down-outline",
                },
            )
            self.dashboard_item(
                self.prefix + ".export_energy",
                state=self.dp3(final_export_kwh),
                attributes={
                    "results": export_kwh_time,
                    "export_until_charge_kwh": export_to_first_charge,
                    "friendly_name": "Predicted exports",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-export",
                },
            )
            self.dashboard_item(
                self.prefix + ".export_energy_h0",
                state=self.dp3(export_kwh_h0),
                attributes={"friendly_name": "Current export kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-export"},
            )
            self.dashboard_item(
                self.prefix + ".load_energy",
                state=self.dp3(final_load_kwh),
                attributes={
                    "results": load_kwh_time,
                    "friendly_name": "Predicted load",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:home-lightning-bolt",
                },
            )
            self.dashboard_item(
                self.prefix + ".load_energy_h0",
                state=self.dp3(load_kwh_h0),
                attributes={"friendly_name": "Current load kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
            )
            self.dashboard_item(
                self.prefix + ".pv_energy",
                state=self.dp3(final_pv_kwh),
                attributes={"results": pv_kwh_time, "friendly_name": "Predicted PV", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
            )
            self.dashboard_item(
                self.prefix + ".pv_energy_h0",
                state=self.dp3(pv_kwh_h0),
                attributes={"friendly_name": "Current PV kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
            )
            self.dashboard_item(
                self.prefix + ".import_energy",
                state=self.dp3(final_import_kwh),
                attributes={
                    "results": import_kwh_time,
                    "friendly_name": "Predicted imports",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-import",
                },
            )
            self.dashboard_item(
                self.prefix + ".import_energy_h0",
                state=self.dp3(import_kwh_h0),
                attributes={"friendly_name": "Current import kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
            )
            self.dashboard_item(
                self.prefix + ".import_energy_battery",
                state=self.dp3(final_import_kwh_battery),
                attributes={"friendly_name": "Predicted import to battery", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
            )
            self.dashboard_item(
                self.prefix + ".import_energy_house",
                state=self.dp3(final_import_kwh_house),
                attributes={"friendly_name": "Predicted import to house", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
            )
            self.log("Battery has {} hours left - now at {}".format(self.dp2(hours_left), self.dp2(self.soc_kw)))
            self.dashboard_item(
                self.prefix + ".metric",
                state=self.dp2(final_metric),
                attributes={
                    "results": metric_time,
                    "friendly_name": "Predicted metric (cost)",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(
                self.prefix + ".duration",
                state=self.dp2(end_record / 60),
                attributes={"friendly_name": "Prediction duration", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:arrow-split-vertical"},
            )

        if save and save == "best" and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".best_battery_hours_left",
                state=self.dp2(hours_left),
                attributes={"friendly_name": "Predicted Battery Hours left best", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
            )
            postfix = ""
            for car_n in range(0, self.num_cars):
                if car_n > 0:
                    postfix = "_" + str(car_n)
                self.dashboard_item(
                    self.prefix + ".car_soc_best" + postfix,
                    state=self.dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                    attributes={
                        "results": predict_car_soc_time[car_n],
                        "friendly_name": "Car " + str(car_n) + " battery SOC best",
                        "state_class": "measurement",
                        "unit_of_measurement": "%",
                        "icon": "mdi:battery",
                    },
                )
            self.dashboard_item(
                self.prefix + ".soc_kw_best",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_soc_time,
                    "friendly_name": "Battery SOC kWh best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "first_charge_kwh": first_charge_soc,
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".battery_power_best",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_battery_power,
                    "friendly_name": "Predicted Battery Power Best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".battery_cycle_best",
                state=self.dp3(final_battery_cycle),
                attributes={
                    "results": predict_battery_cycle,
                    "friendly_name": "Predicted Battery Cycle Best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".pv_power_best",
                state=self.dp3(0),
                attributes={
                    "results": predict_pv_power,
                    "friendly_name": "Predicted PV Power Best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".grid_power_best",
                state=self.dp3(0),
                attributes={
                    "results": predict_grid_power,
                    "friendly_name": "Predicted Grid Power Best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".load_power_best",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_load_power,
                    "friendly_name": "Predicted Load Power Best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".soc_kw_best_h1",
                state=self.dp3(self.predict_soc[60]),
                attributes={"friendly_name": "Predicted SOC kWh best + 1h", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
            )
            self.dashboard_item(
                self.prefix + ".soc_kw_best_h8",
                state=self.dp3(self.predict_soc[60 * 8]),
                attributes={"friendly_name": "Predicted SOC kWh best + 8h", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
            )
            self.dashboard_item(
                self.prefix + ".soc_kw_best_h12",
                state=self.dp3(self.predict_soc[60 * 12]),
                attributes={"friendly_name": "Predicted SOC kWh best + 12h", "state_class": "measurement", "unit _of_measurement": "kWh", "icon": "mdi:battery"},
            )
            self.dashboard_item(
                self.prefix + ".best_soc_min_kwh",
                state=self.dp3(soc_min),
                attributes={
                    "time": self.time_abs_str(soc_min_minute),
                    "friendly_name": "Predicted minimum SOC best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-arrow-down-outline",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_export_energy",
                state=self.dp3(final_export_kwh),
                attributes={
                    "results": export_kwh_time,
                    "export_until_charge_kwh": export_to_first_charge,
                    "friendly_name": "Predicted exports best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-export",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_load_energy",
                state=self.dp3(final_load_kwh),
                attributes={
                    "results": load_kwh_time,
                    "friendly_name": "Predicted load best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:home-lightning-bolt",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_pv_energy",
                state=self.dp3(final_pv_kwh),
                attributes={"results": pv_kwh_time, "friendly_name": "Predicted PV best", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
            )
            self.dashboard_item(
                self.prefix + ".best_import_energy",
                state=self.dp3(final_import_kwh),
                attributes={
                    "results": import_kwh_time,
                    "friendly_name": "Predicted imports best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-import",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_import_energy_battery",
                state=self.dp3(final_import_kwh_battery),
                attributes={
                    "friendly_name": "Predicted import to battery best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-import",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_import_energy_house",
                state=self.dp3(final_import_kwh_house),
                attributes={"friendly_name": "Predicted import to house best", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
            )
            self.dashboard_item(
                self.prefix + ".best_metric",
                state=self.dp2(final_metric),
                attributes={
                    "results": metric_time,
                    "friendly_name": "Predicted best metric (cost)",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(self.prefix + ".record", state=0.0, attributes={"results": record_time, "friendly_name": "Prediction window", "state_class": "measurement"})
            self.dashboard_item(
                self.prefix + ".iboost_best",
                state=self.dp2(final_iboost_kwh),
                attributes={
                    "results": predict_iboost,
                    "friendly_name": "Predicted IBoost energy best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:water-boiler",
                },
            )
            self.dashboard_item(
                "binary_sensor." + self.prefix + "_iboost_active" + postfix,
                state=self.iboost_running,
                attributes={"friendly_name": "IBoost active", "icon": "mdi:water-boiler"},
            )
            self.find_spare_energy(predict_soc, predict_export, step, first_charge)

        if save and save == "debug" and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".pv_power_debug",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_pv_power,
                    "friendly_name": "Predicted PV Power Debug",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".grid_power_debug",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_grid_power,
                    "friendly_name": "Predicted Grid Power Debug",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".load_power_debug",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_load_power,
                    "friendly_name": "Predicted Load Power Debug",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".battery_power_debug",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_battery_power,
                    "friendly_name": "Predicted Battery Power Debug",
                    "state_class": "measurement",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:battery",
                },
            )

        if save and save == "best10" and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".soc_kw_best10",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_soc_time,
                    "friendly_name": "Battery SOC kWh best 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "first_charge_kwh": first_charge_soc,
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".best10_pv_energy",
                state=self.dp3(final_pv_kwh),
                attributes={
                    "results": pv_kwh_time,
                    "friendly_name": "Predicted PV best 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:solar-power",
                },
            )
            self.dashboard_item(
                self.prefix + ".best10_metric",
                state=self.dp2(final_metric),
                attributes={
                    "results": metric_time,
                    "friendly_name": "Predicted best 10% metric (cost)",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(
                self.prefix + ".best10_export_energy",
                state=self.dp3(final_export_kwh),
                attributes={
                    "results": export_kwh_time,
                    "export_until_charge_kwh": export_to_first_charge,
                    "friendly_name": "Predicted exports best 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-export",
                },
            )
            self.dashboard_item(
                self.prefix + ".best10_load_energy",
                state=self.dp3(final_load_kwh),
                attributes={"friendly_name": "Predicted load best 10%", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
            )
            self.dashboard_item(
                self.prefix + ".best10_import_energy",
                state=self.dp3(final_import_kwh),
                attributes={
                    "results": import_kwh_time,
                    "friendly_name": "Predicted imports best 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-import",
                },
            )

        if save and save == "base10" and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".soc_kw_base10",
                state=self.dp3(final_soc),
                attributes={
                    "results": predict_soc_time,
                    "friendly_name": "Battery SOC kWh base 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery",
                },
            )
            self.dashboard_item(
                self.prefix + ".base10_pv_energy",
                state=self.dp3(final_pv_kwh),
                attributes={
                    "results": pv_kwh_time,
                    "friendly_name": "Predicted PV base 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:solar-power",
                },
            )
            self.dashboard_item(
                self.prefix + ".base10_metric",
                state=self.dp2(final_metric),
                attributes={
                    "results": metric_time,
                    "friendly_name": "Predicted base 10% metric (cost)",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(
                self.prefix + ".base10_export_energy",
                state=self.dp3(final_export_kwh),
                attributes={
                    "results": export_kwh_time,
                    "export_until_charge_kwh": export_to_first_charge,
                    "friendly_name": "Predicted exports base 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-export",
                },
            )
            self.dashboard_item(
                self.prefix + ".base10_load_energy",
                state=self.dp3(final_load_kwh),
                attributes={"friendly_name": "Predicted load base 10%", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
            )
            self.dashboard_item(
                self.prefix + ".base10_import_energy",
                state=self.dp3(final_import_kwh),
                attributes={
                    "results": import_kwh_time,
                    "friendly_name": "Predicted imports base 10%",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:transmission-tower-import",
                },
            )

        return final_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, final_soc, soc_min_minute, final_battery_cycle, final_metric_keep, final_iboost_kwh

    def time_now_str(self):
        """
        Return time now as human string
        """
        return (self.midnight + timedelta(minutes=self.minutes_now)).strftime("%H:%M:%S")

    def time_abs_str(self, minute):
        """
        Return time absolute as human string
        """
        return (self.midnight + timedelta(minutes=minute)).strftime("%m-%d %H:%M:%S")

    def rate_replicate(self, rates, rate_io={}, is_import=True, is_gas=False):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = 0
        rate_last = 0
        adjusted_rates = {}
        replicated_rates = {}

        # Add 48 extra hours to make sure the whole cycle repeats another day
        while minute < (self.forecast_minutes + 48 * 60):
            if minute not in rates:
                # Take 24-hours previous if missing rate
                if (minute >= 24 * 60) and ((minute - 24 * 60) in rates):
                    minute_mod = minute - 24 * 60
                else:
                    minute_mod = minute % (24 * 60)

                if (minute_mod in rate_io) and rate_io[minute_mod]:
                    # Dont replicate Intelligent rates into the next day as it will be different
                    rate_offset = self.rate_max
                elif minute_mod in rates:
                    rate_offset = rates[minute_mod]
                else:
                    # Missing rate within 24 hours - fill with dummy last rate
                    rate_offset = rate_last

                # Only offset once not every day
                if minute_mod not in adjusted_rates:
                    if (
                        is_import
                        and self.get_arg("futurerate_adjust_import", False)
                        and (minute in self.future_energy_rates_import)
                        and (minute_mod in self.future_energy_rates_import)
                    ):
                        prev_rate = rate_offset
                        rate_offset = rate_offset - self.future_energy_rates_import[minute_mod] + self.future_energy_rates_import[minute]
                    elif (
                        (not is_import)
                        and (not is_gas)
                        and self.get_arg("futurerate_adjust_export", False)
                        and (minute in self.future_energy_rates_export)
                        and (minute_mod in self.future_energy_rates_export)
                    ):
                        prev_rate = rate_offset
                        rate_offset = max(rate_offset - self.future_energy_rates_export[minute_mod] + self.future_energy_rates_export[minute], 0)
                    elif is_import:
                        rate_offset = rate_offset + self.metric_future_rate_offset_import
                    elif (not is_import) and (not is_gas):
                        rate_offset = max(rate_offset + self.metric_future_rate_offset_export, 0)

                    adjusted_rates[minute] = True

                rates[minute] = rate_offset
                replicated_rates[minute] = True
            else:
                rate_last = rates[minute]
                replicated_rates[minute] = False
            minute += 1
        return rates, replicated_rates

    def find_charge_window(self, rates, minute, threshold_rate, find_high):
        """
        Find the charging windows based on the low rate threshold (percent below average)
        """
        rate_low_start = -1
        rate_low_end = -1
        rate_low_average = 0
        rate_low_rate = 0
        rate_low_count = 0

        stop_at = self.forecast_minutes + self.minutes_now + 12 * 60
        # Scan for lower rate start and end
        while minute < stop_at:
            # Don't allow starts beyond the forecast window
            if minute >= (self.forecast_minutes + self.minutes_now) and (rate_low_start < 0):
                break

            if minute in rates:
                rate = rates[minute]
                if ((not find_high) and (rate <= threshold_rate)) or (find_high and (rate >= threshold_rate) and (rate > 0)):
                    if (not self.combine_mixed_rates) and (rate_low_start >= 0) and (int(self.dp2(rate) + 0.5) != int(self.dp2(rate_low_rate) + 0.5)):
                        # Refuse mixed rates that are different by more than 0.5p
                        rate_low_end = minute
                        break
                    if find_high and (not self.combine_discharge_slots) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.discharge_slot_split):
                        # If combine is disabled, for export slots make them all N minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if (not find_high) and (not self.combine_charge_slots) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.charge_slot_split):
                        # If combine is disabled, for import slots make them all N minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if find_high and (rate_low_start >= 0) and ((minute - rate_low_start) >= 60 * 4):
                        # Export slot can never be bigger than 4 hours
                        rate_low_end = minute
                        break
                    if rate_low_start < 0:
                        rate_low_start = minute
                        rate_low_end = stop_at
                        rate_low_count = 1
                        rate_low_average = rate
                        rate_low_rate = rate
                    elif rate_low_end > minute:
                        rate_low_average += rate
                        rate_low_count += 1
                else:
                    if rate_low_start >= 0:
                        rate_low_end = minute
                        break
            else:
                if rate_low_start >= 0 and rate_low_end >= minute:
                    rate_low_end = minute
                break
            minute += 1

        if rate_low_count:
            rate_low_average = self.dp2(rate_low_average / rate_low_count)
        return rate_low_start, rate_low_end, rate_low_average

    def basic_rates(self, info, rtype, prev=None):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        if prev:
            rates = prev.copy()
        else:
            # Set to zero
            for minute in range(0, 24 * 60):
                rates[minute] = 0

        max_minute = max(rates) + 1
        midnight = datetime.strptime("00:00:00", "%H:%M:%S")
        for this_rate in info:
            if this_rate:
                start_str = this_rate.get("start", "00:00:00")
                start_str = self.resolve_arg("start", start_str, "00:00:00")
                end_str = this_rate.get("end", "00:00:00")
                end_str = self.resolve_arg("end", end_str, "00:00:00")

                if start_str.count(":") < 2:
                    start_str += ":00"
                if end_str.count(":") < 2:
                    end_str += ":00"

                try:
                    start = datetime.strptime(start_str, "%H:%M:%S")
                except ValueError:
                    self.log("WARN: Bad start time {} provided in energy rates".format(start_str))
                    self.record_status("Bad start time {} provided in energy rates".format(start_str), had_errors=True)
                    continue

                try:
                    end = datetime.strptime(end_str, "%H:%M:%S")
                except ValueError:
                    self.log("WARN: Bad end time {} provided in energy rates".format(end_str))
                    self.record_status("Bad end time {} provided in energy rates".format(end_str), had_errors=True)
                    continue

                date = None
                if "date" in this_rate:
                    date_str = self.resolve_arg("date", this_rate["date"])
                    try:
                        date = datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        self.log("WARN: Bad date {} provided in energy rates".format(this_rate["date"]))
                        self.record_status("Bad date {} provided in energy rates".format(this_rate["date"]), had_errors=True)
                        continue

                rate = this_rate.get("rate", 0.0)
                rate = self.resolve_arg("rate", rate, 0.0)
                try:
                    rate = float(rate)
                except ValueError:
                    self.log("WARN: Bad rate {} provided in energy rates".format(rate))
                    self.record_status("Bad rate {} provided in energy rates".format(rate), had_errors=True)
                    continue

                # Time in minutes
                start_minutes = max(self.minutes_to_time(start, midnight), 0)
                end_minutes = min(self.minutes_to_time(end, midnight), 24 * 60 - 1)

                self.log("Adding rate {} => {} to {} @ {} date {}".format(this_rate, self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), rate, date))

                # Make end > start
                if end_minutes <= start_minutes:
                    end_minutes += 24 * 60

                # Adjust for date if specified
                if date:
                    delta_minutes = self.minutes_to_time(date, self.midnight)
                    start_minutes += delta_minutes
                    end_minutes += delta_minutes

                # Store rates against range
                if end_minutes >= 0 and start_minutes < max_minute:
                    for minute in range(start_minutes, end_minutes):
                        if (not date) or (minute >= 0 and minute < max_minute):
                            rates[minute % max_minute] = rate
                            if not date and not prev:
                                rates[(minute % max_minute) + max_minute] = rate

        return rates

    def plan_car_charging(self, car_n, low_rates):
        """
        Plan when the car will charge, taking into account ready time and pricing
        """
        plan = []
        car_soc = self.car_charging_soc[car_n]

        if self.car_charging_plan_smart[car_n]:
            price_sorted = self.sort_window_by_price(low_rates)
            price_sorted.reverse()
        else:
            price_sorted = [n for n in range(0, len(low_rates))]

        ready_time = datetime.strptime(self.car_charging_plan_time[car_n], "%H:%M:%S")
        ready_minutes = ready_time.hour * 60 + ready_time.minute

        # Ready minutes wrap?
        if ready_minutes < self.minutes_now:
            ready_minutes += 24 * 60

        # Car charging now override
        extra_slot = {}
        if self.car_charging_now[car_n]:
            start = int(self.minutes_now / 30) * 30
            end = start + 30
            extra_slot["start"] = start
            extra_slot["end"] = end
            extra_slot["average"] = self.rate_import.get(start, self.rate_min)
            self.log("Car is charging now slot {}".format(extra_slot))

            for window_p in price_sorted:
                window = low_rates[window_p]
                if window["start"] == start:
                    price_sorted.remove(window_p)
                    self.log("Remove old window {}".format(window_p))
                    break

            price_sorted = [-1] + price_sorted

        for window_n in price_sorted:
            if window_n == -1:
                window = extra_slot
            else:
                window = low_rates[window_n]

            start = max(window["start"], self.minutes_now)
            end = min(window["end"], ready_minutes)
            length = 0
            kwh = 0

            if car_soc >= self.car_charging_limit[car_n]:
                break

            if end <= start:
                continue

            length = end - start
            hours = length / 60
            kwh = self.car_charging_rate[car_n] * hours

            kwh_add = kwh * self.car_charging_loss
            kwh_left = max(self.car_charging_limit[car_n] - car_soc, 0)

            # Clamp length to required amount (shorten the window)
            if kwh_add > kwh_left:
                percent = kwh_left / kwh_add
                length = min(int(((length * percent) / 5) + 2.5) * 5, end - start)
                end = start + length
                hours = length / 60
                kwh = self.car_charging_rate[car_n] * hours
                kwh_add = kwh * self.car_charging_loss

            # Work out how much to add to the battery, include losses
            kwh_add = max(min(kwh_add, self.car_charging_limit[car_n] - car_soc), 0)
            kwh = kwh_add / self.car_charging_loss

            # Work out charging amounts
            if kwh > 0:
                car_soc += kwh_add
                new_slot = {}
                new_slot["start"] = start
                new_slot["end"] = end
                new_slot["kwh"] = kwh
                new_slot["average"] = window["average"]
                new_slot["cost"] = new_slot["average"] * kwh
                plan.append(new_slot)

        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def add_now_to_octopus_slot(self, octopus_slots, now_utc):
        """
        For intelligent charging, add in if the car is charging now as a low rate slot (workaround for Ohme)
        """
        for car_n in range(0, self.num_cars):
            if self.car_charging_now[car_n]:
                minutes_start_slot = int(self.minutes_now / 30) * 30
                minutes_end_slot = minutes_start_slot + 30
                slot_start_date = self.midnight_utc + timedelta(minutes=minutes_start_slot)
                slot_end_date = self.midnight_utc + timedelta(minutes=minutes_end_slot)
                slot = {}
                slot["start"] = slot_start_date.strftime(TIME_FORMAT)
                slot["end"] = slot_end_date.strftime(TIME_FORMAT)
                octopus_slots.append(slot)
                self.log("Car is charging now - added new IO slot {}".format(slot))
        return octopus_slots

    def load_saving_slot(self, octopus_saving_slots, export=False):
        """
        Load octopus saving session slot
        """
        start_minutes = 0
        end_minutes = 0

        for octopus_saving_slot in octopus_saving_slots:
            start = octopus_saving_slot["start"]
            end = octopus_saving_slot["end"]
            rate = octopus_saving_slot["rate"]
            state = octopus_saving_slot["state"]

            if start and end:
                try:
                    start = self.str2time(start)
                    end = self.str2time(end)
                except ValueError:
                    start = None
                    end = None
                    self.log("WARN: Unable to decode Octopus saving session start/end time")
            if state and (not start or not end):
                self.log("Currently in saving session, assume current 30 minute slot")
                start_minutes = int(self.minutes_now / 30) * 30
                end_minutes = start_minutes + 30
            elif start and end:
                start_minutes = self.minutes_to_time(start, self.midnight_utc)
                end_minutes = min(self.minutes_to_time(end, self.midnight_utc), self.forecast_minutes)

            if start_minutes >= 0 and end_minutes != start_minutes and start_minutes < self.forecast_minutes:
                self.log("Setting Octopus saving session in range {} - {} export {} rate {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), export, rate))
                for minute in range(start_minutes, end_minutes):
                    if export:
                        self.rate_export[minute] += rate
                    else:
                        self.rate_import[minute] += rate

    def load_octopus_slots(self, octopus_slots):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []

        for slot in octopus_slots:
            if "start" in slot:
                start = datetime.strptime(slot["start"], TIME_FORMAT)
                end = datetime.strptime(slot["end"], TIME_FORMAT)
            else:
                start = datetime.strptime(slot["startDtUtc"], TIME_FORMAT_OCTOPUS)
                end = datetime.strptime(slot["endDtUtc"], TIME_FORMAT_OCTOPUS)
            source = slot.get("source", "")
            start_minutes = max(self.minutes_to_time(start, self.midnight_utc), 0)
            end_minutes = min(self.minutes_to_time(end, self.midnight_utc), self.forecast_minutes)
            slot_minutes = end_minutes - start_minutes
            slot_hours = slot_minutes / 60.0

            # The load expected is stored in chargeKwh for the period in use
            if "charge_in_kwh" in slot:
                kwh = abs(float(slot.get("charge_in_kwh", self.car_charging_rate[0] * slot_hours)))
            else:
                kwh = abs(float(slot.get("chargeKwh", self.car_charging_rate[0] * slot_hours)))

            if end_minutes > self.minutes_now:
                new_slot = {}
                new_slot["start"] = start_minutes
                new_slot["end"] = end_minutes
                new_slot["kwh"] = kwh
                if source != "bump-charge":
                    new_slot["average"] = self.rate_min  # Assume price in min
                else:
                    new_slot["average"] = self.rate_max  # Assume price is max
                new_slot["cost"] = new_slot["average"] * kwh
                new_slots.append(new_slot)
        return new_slots

    def in_car_slot(self, minute):
        """
        Is the given minute inside a car slot
        """
        load_amount = [0 for car_n in range(0, self.num_cars)]

        for car_n in range(0, self.num_cars):
            if self.car_charging_slots[car_n]:
                for slot in self.car_charging_slots[car_n]:
                    start_minutes = slot["start"]
                    end_minutes = slot["end"]
                    kwh = slot["kwh"]
                    slot_minutes = end_minutes - start_minutes
                    slot_hours = slot_minutes / 60.0

                    # Return the load in that slot
                    if minute >= start_minutes and minute < end_minutes:
                        load_amount[car_n] = abs(kwh / slot_hours)
                        break
        return load_amount

    def rate_scan_export(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """

        self.rate_export_min, self.rate_export_max, self.rate_export_average, self.rate_export_min_minute, self.rate_export_max_minute = self.rate_minmax(rates)
        if print:
            self.log("Export rates min {} max {} average {}".format(self.rate_export_min, self.rate_export_max, self.rate_export_average))
        return rates

    def publish_car_plan(self):
        """
        Publish the car charging plan
        """
        plan = []
        postfix = ""
        for car_n in range(self.num_cars):
            if car_n > 0:
                postfix = "_" + str(car_n)
            if not self.car_charging_slots[car_n]:
                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_car_charging_slot" + postfix,
                    state="off",
                    attributes={"planned": plan, "cost": None, "kWh": None, "friendly_name": "Predbat car charging slot" + postfix, "icon": "mdi:home-lightning-bolt-outline"},
                )
                self.dashboard_item(
                    self.prefix + ".car_charging_start" + postfix,
                    state="undefined",
                    attributes={
                        "friendly_name": "Predbat car charge start time car" + postfix,
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                    },
                )
            else:
                window = self.car_charging_slots[car_n][0]
                if self.minutes_now >= window["start"] and self.minutes_now < window["end"]:
                    slot = True
                else:
                    slot = False

                time_format_time = "%H:%M:%S"
                car_startt = self.midnight_utc + timedelta(minutes=window["start"])
                car_start_time_str = car_startt.strftime(time_format_time)
                self.dashboard_item(
                    self.prefix + ".car_charging_start" + postfix,
                    state=car_start_time_str,
                    attributes={
                        "friendly_name": "Predbat car charge start time car" + postfix,
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                    },
                )

                total_kwh = 0
                total_cost = 0
                for window in self.car_charging_slots[car_n]:
                    start = self.time_abs_str(window["start"])
                    end = self.time_abs_str(window["end"])
                    kwh = self.dp2(window["kwh"])
                    average = self.dp2(window["average"])
                    cost = self.dp2(window["cost"])

                    show = {}
                    show["start"] = start
                    show["end"] = end
                    show["kwh"] = kwh
                    show["average"] = average
                    show["cost"] = cost
                    total_cost += cost
                    total_kwh += kwh
                    plan.append(show)

                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_car_charging_slot" + postfix,
                    state="on" if slot else "off",
                    attributes={
                        "planned": plan,
                        "cost": self.dp2(total_cost),
                        "kwh": self.dp2(total_kwh),
                        "friendly_name": "Predbat car charging slot" + postfix,
                        "icon": "mdi:home-lightning-bolt-outline",
                    },
                )

    def publish_rates_export(self):
        """
        Publish the export rates
        """
        window_str = ""
        if self.high_export_rates:
            window_n = 0
            for window in self.high_export_rates:
                rate_high_start = window["start"]
                rate_high_end = window["end"]
                rate_high_average = window["average"]

                if window_str:
                    window_str += ", "
                window_str += "{}: {} - {} @ {}".format(window_n, self.time_abs_str(rate_high_start), self.time_abs_str(rate_high_end), rate_high_average)

                rate_high_start_date = self.midnight_utc + timedelta(minutes=rate_high_start)
                rate_high_end_date = self.midnight_utc + timedelta(minutes=rate_high_end)

                time_format_time = "%H:%M:%S"

                if window_n == 0 and not SIMULATE:
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_start",
                        state=rate_high_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next high export rate start",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_end",
                        state=rate_high_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_end_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next high export rate end",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_cost",
                        state=self.dp2(rate_high_average),
                        attributes={"friendly_name": "Next high export rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
                    )
                    in_high_rate = self.minutes_now >= rate_high_start and self.minutes_now <= rate_high_end
                    self.dashboard_item(
                        "binary_sensor." + self.prefix + "_high_rate_export_slot",
                        state="on" if in_high_rate else "off",
                        attributes={"friendly_name": "Predbat high rate slot", "icon": "mdi:home-lightning-bolt-outline"},
                    )
                    high_rate_minutes = (rate_high_end - self.minutes_now) if in_high_rate else (rate_high_end - rate_high_start)
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_duration",
                        state=high_rate_minutes,
                        attributes={"friendly_name": "Next high export rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
                    )
                if window_n == 1 and not SIMULATE:
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_start_2",
                        state=rate_high_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 high export rate start",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_end_2",
                        state=rate_high_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_end_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 high export rate end",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_cost_2",
                        state=self.dp2(rate_high_average),
                        attributes={"friendly_name": "Next+1 high export rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
                    )
                window_n += 1

        if window_str:
            self.log("High export rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.high_export_rates and not SIMULATE:
            self.log("No high rate period found")
            self.dashboard_item(
                self.prefix + ".high_rate_export_start",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next high export rate start", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_end",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next high export rate end", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_cost",
                state=self.dp2(self.rate_export_average),
                attributes={"friendly_name": "Next high export rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
            )
            self.dashboard_item(
                "binary_sensor." + self.prefix + "_high_rate_export_slot",
                state="off",
                attributes={"friendly_name": "Predbat high export rate slot", "icon": "mdi:home-lightning-bolt-outline"},
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_duration",
                state=0,
                attributes={"friendly_name": "Next high export rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
            )
        if len(self.high_export_rates) < 2 and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".high_rate_export_start_2",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next+1 high export rate start", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_end_2",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next+1 high export rate end", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_cost_2",
                state=self.dp2(self.rate_export_average),
                attributes={"friendly_name": "Next+1 high export rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
            )

    def rate_minmax(self, rates):
        """
        Work out min and max rates
        """
        rate_min = 99999
        rate_min_minute = 0
        rate_max_minute = 0
        rate_max = 0
        rate_average = 0
        rate_n = 0
        rate = 0

        # Scan rates and find min/max/average
        if rates:
            rate = rates.get(self.minutes_now, 0)
            for minute in range(self.minutes_now, self.forecast_minutes + self.minutes_now):
                if minute in rates:
                    rate = rates[minute]
                    if rate > rate_max:
                        rate_max = rate
                        rate_max_minute = minute
                    if rate < rate_min:
                        rate_min = rate
                        rate_min_minute = minute
                    rate_average += rate
                    rate_n += 1
                minute += 1
        if rate_n:
            rate_average /= rate_n

        return self.dp2(rate_min), self.dp2(rate_max), self.dp2(rate_average), rate_min_minute, rate_max_minute

    def rate_min_forward_calc(self, rates):
        """
        Work out lowest rate from time forwards
        """
        rate_array = []
        rate_min_forward = {}
        rate = self.rate_min

        for minute in range(0, self.forecast_minutes + self.minutes_now + 48 * 60):
            if minute in rates:
                rate = rates[minute]
            rate_array.append(rate)

        # Work out the min rate going forward
        for minute in range(self.minutes_now, self.forecast_minutes + 24 * 60 + self.minutes_now):
            rate_min_forward[minute] = min(rate_array[minute:])

        self.log("Rate min forward looking: now {} at end of forecast {}".format(rate_min_forward[self.minutes_now], self.dp2(rate_min_forward[self.forecast_minutes])))

        return rate_min_forward

    def rate_scan_window(self, rates, rate_low_min_window, threshold_rate, find_high):
        """
        Scan for the next high/low rate window
        """
        minute = 0
        found_rates = []
        lowest = 99
        highest = -99
        upcoming_period = self.minutes_now + 4 * 60

        while True:
            rate_low_start, rate_low_end, rate_low_average = self.find_charge_window(rates, minute, threshold_rate, find_high)
            window = {}
            window["start"] = rate_low_start
            window["end"] = rate_low_end
            window["average"] = rate_low_average

            if rate_low_start >= 0:
                if rate_low_end > self.minutes_now and (rate_low_end - rate_low_start) >= rate_low_min_window:
                    found_rates.append(window)
                minute = rate_low_end
            else:
                break

        # Sort all windows by price
        selected_rates = []
        total = 0
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(found_rates, [], stand_alone=True)
        if not find_high:
            price_set.reverse()

        # For each price set in order, take windows newest and then oldest picks
        take_enable = True
        for loop_price in price_set:
            these_items = price_links[loop_price].copy()
            take_front = not find_high
            take_position = 0
            take_counter = 0
            while these_items:
                remaining = len(these_items)
                take_position = take_position % remaining
                if take_front:
                    from_pos = take_position
                    key = these_items.pop(take_position)
                else:
                    from_pos = remaining - (take_position) - 1
                    key = these_items.pop(from_pos)

                window_id = window_index[key]["id"]
                window_price = found_rates[window_id]["average"]
                window_start = found_rates[window_id]["start"]

                # Only count those starting inside the window, those outside appear for 'free' as they won't be optimised
                if window_start >= (self.minutes_now + self.forecast_minutes):
                    selected_rates.append(window_id)
                elif take_enable or (not find_high and window_start < upcoming_period):
                    if window_price < lowest:
                        lowest = window_price
                    if window_price > highest:
                        highest = window_price
                    selected_rates.append(window_id)
                    total += 1

                # Take 60 minutes together and then move on to another group
                if take_counter >= 2:
                    take_position += 31
                    take_counter = 0
                else:
                    take_counter += 1

                if total >= self.calculate_max_windows:
                    take_enable = False
        selected_rates.sort()
        final_rates = []
        for window_id in selected_rates:
            final_rates.append(found_rates[window_id])
        return final_rates, lowest, highest

    def set_rate_thresholds(self):
        """
        Set the high and low rate thresholds
        """
        if self.rate_low_threshold > 0:
            self.rate_import_cost_threshold = self.dp2(self.rate_average * self.rate_low_threshold)
        else:
            # In automatic mode select the only rate or everything but the most expensive
            if (self.rate_max == self.rate_min) or (self.rate_export_max > self.rate_max):
                self.rate_import_cost_threshold = self.rate_max
            else:
                self.rate_import_cost_threshold = self.rate_max - 0.5

        # Compute the export rate threshold
        if self.rate_high_threshold > 0:
            self.rate_export_cost_threshold = self.dp2(self.rate_export_average * self.rate_high_threshold)
        else:
            # In automatic mode select the only rate or everything but the most cheapest
            if (self.rate_export_max == self.rate_export_min) or (self.rate_export_min > self.rate_min):
                self.rate_export_cost_threshold = self.rate_export_min
            else:
                self.rate_export_cost_threshold = self.rate_export_min + 0.5

        # Rule out exports if the import rate is already higher unless it's a variable export tariff
        if self.rate_export_max == self.rate_export_min:
            self.rate_export_cost_threshold = max(self.rate_export_cost_threshold, self.dp2(self.rate_min))

        self.log(
            "Rate thresholds (for charge/discharge) are import {}p ({}) export {}p ({})".format(
                self.rate_import_cost_threshold, self.rate_low_threshold, self.rate_export_cost_threshold, self.rate_high_threshold
            )
        )

    def rate_add_io_slots(self, rates, octopus_slots):
        """
        # Add in any planned octopus slots
        """
        if octopus_slots:
            # Add in IO slots
            for slot in octopus_slots:
                if "start" in slot:
                    start = datetime.strptime(slot["start"], TIME_FORMAT)
                    end = datetime.strptime(slot["end"], TIME_FORMAT)
                else:
                    start = datetime.strptime(slot["startDtUtc"], TIME_FORMAT_OCTOPUS)
                    end = datetime.strptime(slot["endDtUtc"], TIME_FORMAT_OCTOPUS)
                source = slot.get("source", "")
                # Ignore bump-charge slots as their cost won't change
                if source != "bump-charge":
                    start_minutes = max(self.minutes_to_time(start, self.midnight_utc), 0)
                    end_minutes = max(min(self.minutes_to_time(end, self.midnight_utc), self.forecast_minutes), 0)
                    if end_minutes > start_minutes:
                        self.log("Octopus Intelligent slot at {}-{} assumed price {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), self.rate_min))
                        for minute in range(start_minutes, end_minutes):
                            rates[minute] = self.rate_min

        return rates

    def rate_scan(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """
        self.low_rates = []

        self.rate_min, self.rate_max, self.rate_average, self.rate_min_minute, self.rate_max_minute = self.rate_minmax(rates)

        if print:
            # Calculate minimum forward rates only once rate replicate has run (when print is True)
            self.rate_min_forward = self.rate_min_forward_calc(self.rate_import)
            self.log("Import rates min {} max {} average {}".format(self.rate_min, self.rate_max, self.rate_average))

        return rates

    def rate_scan_gas(self, rates, print=True):
        """
        Scan the gas rates and work out min/max
        """
        rate_gas_min, rate_gas_max, rate_gas_average, rate_gas_min_minute, rate_gas_max_minute = self.rate_minmax(rates)

        if print:
            self.log("Gas rates min {} max {} average {}".format(rate_gas_min, rate_gas_max, rate_gas_average))

        return rates

    def publish_rates_import(self):
        """
        Publish the import rates
        """
        window_str = ""
        # Output rate info
        if self.low_rates:
            window_n = 0
            for window in self.low_rates:
                rate_low_start = window["start"]
                rate_low_end = window["end"]
                rate_low_average = window["average"]

                if window_str:
                    window_str += ", "
                window_str += "{}: {} - {} @ {}".format(window_n, self.time_abs_str(rate_low_start), self.time_abs_str(rate_low_end), rate_low_average)

                rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
                rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)

                time_format_time = "%H:%M:%S"
                if window_n == 0 and not SIMULATE:
                    self.dashboard_item(
                        self.prefix + ".low_rate_start",
                        state=rate_low_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next low rate start",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_end",
                        state=rate_low_end_date.strftime(time_format_time),
                        attributes={"date": rate_low_end_date.strftime(TIME_FORMAT), "friendly_name": "Next low rate end", "state_class": "timestamp", "icon": "mdi:table-clock"},
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_cost",
                        state=rate_low_average,
                        attributes={"friendly_name": "Next low rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
                    )
                    in_low_rate = self.minutes_now >= rate_low_start and self.minutes_now <= rate_low_end
                    self.dashboard_item(
                        "binary_sensor." + self.prefix + "_low_rate_slot",
                        state="on" if in_low_rate else "off",
                        attributes={"friendly_name": "Predbat low rate slot", "icon": "mdi:home-lightning-bolt-outline"},
                    )
                    low_rate_minutes = (rate_low_end - self.minutes_now) if in_low_rate else (rate_low_end - rate_low_start)
                    self.dashboard_item(
                        self.prefix + ".low_rate_duration",
                        state=low_rate_minutes,
                        attributes={"friendly_name": "Next low rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
                    )
                if window_n == 1 and not SIMULATE:
                    self.dashboard_item(
                        self.prefix + ".low_rate_start_2",
                        state=rate_low_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 low rate start",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_end_2",
                        state=rate_low_end_date.strftime(time_format_time),
                        attributes={"date": rate_low_end_date.strftime(TIME_FORMAT), "friendly_name": "Next+1 low rate end", "state_class": "timestamp", "icon": "mdi:table-clock"},
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_cost_2",
                        state=rate_low_average,
                        attributes={"friendly_name": "Next+1 low rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
                    )
                window_n += 1

        self.log("Low import rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.low_rates and not SIMULATE:
            self.log("No low rate period found")
            self.dashboard_item(
                self.prefix + ".low_rate_start",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next low rate start", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".low_rate_end",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next low rate end", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".low_rate_cost",
                state=self.rate_average,
                attributes={"friendly_name": "Next low rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
            )
            self.dashboard_item(
                self.prefix + ".low_rate_duration",
                state=0,
                attributes={"friendly_name": "Next low rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                "binary_sensor." + self.prefix + "_low_rate_slot", state="off", attributes={"friendly_name": "Predbat low rate slot", "icon": "mdi:home-lightning-bolt-outline"}
            )
        if len(self.low_rates) < 2 and not SIMULATE:
            self.dashboard_item(
                self.prefix + ".low_rate_start_2",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next+1 low rate start", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".low_rate_end_2",
                state="undefined",
                attributes={"date": None, "friendly_name": "Next+1 low rate end", "device_class": "timestamp", "icon": "mdi:table-clock"},
            )
            self.dashboard_item(
                self.prefix + ".low_rate_cost_2",
                state=self.rate_average,
                attributes={"friendly_name": "Next+1 low rate cost", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
            )

    def publish_html_plan(self, pv_forecast_minute_step, load_minutes_step, end_record):
        """
        Publish the current plan in HTML format
        """
        plan_debug = self.get_arg("plan_debug")
        html = "<table>"
        html += "<tr>"
        html += "<td><b>Time</b></td>"
        if plan_debug:
            html += "<td><b>Import p (w/loss)</b></td>"
            html += "<td><b>Export p (w/loss)</b></td>"
        else:
            html += "<td><b>Import p</b></td>"
            html += "<td><b>Export p</b></td>"
        html += "<td><b>State</b></td><td></td>"  # state can potentially be two cells for charging and discharging in the same slot
        html += "<td><b>Limit %</b></td>"
        html += "<td><b>PV kWh</b></td>"
        html += "<td><b>Load kWh</b></td>"
        if self.num_cars > 0:
            html += "<td><b>Car kWh</b></td>"
        if self.iboost_enable:
            html += "<td><b>IBoost kWh</b></td>"
        html += "<td><b>SOC %</b></td>"
        html += "<td><b>Cost</b></td>"
        html += "<td><b>Total</b></td>"
        html += "</tr>"

        minute_now_align = int(self.minutes_now / 30) * 30
        end_plan = min(end_record, self.forecast_minutes) + minute_now_align
        rowspan = 0
        in_span = False
        start_span = False
        for minute in range(minute_now_align, end_plan, 30):
            minute_relative = max(minute - self.minutes_now, 0)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            rate_start = minute_timestamp
            rate_value_import = self.dp2(self.rate_import.get(minute, 0))
            rate_value_export = self.dp2(self.rate_export.get(minute, 0))
            charge_window_n = -1
            discharge_window_n = -1

            import_cost_threshold = self.rate_import_cost_threshold
            export_cost_threshold = self.rate_export_cost_threshold

            if self.rate_best_cost_threshold_charge:
                import_cost_threshold = self.rate_best_cost_threshold_charge
            if self.rate_best_cost_threshold_discharge:
                export_cost_threshold = self.rate_best_cost_threshold_discharge

            show_limit = ""

            for try_minute in range(minute, minute + 30, 5):
                charge_window_n = self.in_charge_window(self.charge_window_best, try_minute)
                if charge_window_n >= 0:
                    break

            start_span = False
            if in_span:
                rowspan = max(rowspan - 1, 0)
                if rowspan == 0:
                    in_span = False

            if charge_window_n >= 0 and not in_span:
                rowspan = int((self.charge_window_best[charge_window_n]["end"] - minute) / 30)
                if rowspan > 1:
                    in_span = True
                    start_span = True
                else:
                    rowspan = 0

            for try_minute in range(minute, minute + 30, 5):
                discharge_window_n = self.in_charge_window(self.discharge_window_best, try_minute)
                if discharge_window_n >= 0:
                    break

            pv_forecast = 0
            load_forecast = 0
            for offset in range(0, 30, PREDICT_STEP):
                pv_forecast += pv_forecast_minute_step.get(minute_relative + offset, 0.0)
                load_forecast += load_minutes_step.get(minute_relative + offset, 0.0)
            pv_forecast = self.dp2(pv_forecast)
            load_forecast = self.dp2(load_forecast)

            soc_percent = int(self.dp2((self.predict_soc_best.get(minute_relative, 0.0) / self.soc_max) * 100.0) + 0.5)
            soc_percent_end = int(self.dp2((self.predict_soc_best.get(minute_relative + 30, 0.0) / self.soc_max) * 100.0) + 0.5)
            soc_percent_max = max(soc_percent, soc_percent_end)
            soc_percent_min = min(soc_percent, soc_percent_end)
            soc_change = self.predict_soc_best.get(minute_relative + 30, 0.0) - self.predict_soc_best.get(minute_relative, 0.0)
            metric_start = self.predict_metric_best.get(minute_relative, 0.0)
            metric_end = self.predict_metric_best.get(minute_relative + 30, metric_start)
            metric_change = metric_end - metric_start

            soc_sym = ""
            if abs(soc_change) < 0.05:
                soc_sym = "&rarr;"
            elif soc_change >= 0:
                soc_sym = "&nearr;"
            else:
                soc_sym = "&searr;"

            state = soc_sym
            state_color = "#FFFFFF"
            pv_color = "#BCBCBC"
            load_color = "#FFFFFF"
            rate_color_import = "#FFFFAA"
            rate_color_export = "#FFFFFF"
            soc_color = "#3AEE85"
            pv_symbol = ""
            split = False

            if soc_percent < 20.0:
                soc_color = "#F18261"
            elif soc_percent < 50.0:
                soc_color = "#FFFF00"

            if pv_forecast >= 0.2:
                pv_color = "#FFAAAA"
                pv_symbol = "&#9728;"
            elif pv_forecast >= 0.1:
                pv_color = "#FFFF00"
                pv_symbol = "&#9728;"
            elif pv_forecast == 0.0:
                pv_forecast = ""

            if load_forecast >= 0.5:
                load_color = "#F18261"
            elif load_forecast >= 0.25:
                load_color = "#FFFF00"
            elif load_forecast > 0.0:
                load_color = "#AAFFAA"

            if rate_value_import <= 0:  # colour the import rate, blue for negative, then green, yellow and red
                rate_color_import = "#74C1FF"
            elif rate_value_import <= import_cost_threshold:
                rate_color_import = "#3AEE85"
            elif rate_value_import > (import_cost_threshold * 1.5):
                rate_color_import = "#F18261"

            if rate_value_export >= (1.5 * export_cost_threshold):
                rate_color_export = "#F18261"
            elif rate_value_export >= export_cost_threshold:
                rate_color_export = "#FFFFAA"

            if charge_window_n >= 0:
                limit = self.charge_limit_best[charge_window_n]
                limit_percent = int(self.charge_limit_percent_best[charge_window_n])
                if limit > 0.0:
                    if self.set_charge_freeze and (limit == self.reserve):
                        state = "FreezeChrg&rarr;"
                        state_color = "#EEEEEE"
                        limit_percent = soc_percent
                    elif limit_percent == soc_percent_min:
                        state = "HoldChrg&rarr;"
                        state_color = "#34DBEB"
                    elif limit_percent < soc_percent_min:
                        state = "NoCharge&searr;"
                        state_color = "#FFFFFF"
                    else:
                        state = "Charge&nearr;"
                        state_color = "#3AEE85"
                    show_limit = str(limit_percent)

            if discharge_window_n >= 0:
                limit = self.discharge_limits_best[discharge_window_n]
                if limit < 100 and limit > soc_percent_max:
                    if state == soc_sym:
                        state = ""
                    if state:
                        state += "</td><td bgcolor=#AAAAAA>"  # charging and freeze discharging in same slot, split the state into two
                        split = True
                    else:
                        state_color = "#AAAAAA"
                    state += "FreezeDis&rarr;"
                elif limit < 100:
                    if state == soc_sym:
                        state = ""
                    if state:
                        state += "</td><td bgcolor=#FFFF00>"  # charging and discharging in the same slot
                        split = True
                    else:
                        state_color = "#FFFF00"
                    state += "Discharge&searr;"
                    show_limit = str(int(limit))

            # Import and export rates -> to string
            if self.rate_import_replicated.get(minute, False):
                rate_str_import = "<i>%02.02f ?</i>" % (rate_value_import)
            else:
                rate_str_import = "%02.02f" % rate_value_import

            if plan_debug:
                rate_str_import += " (%02.02f)" % (rate_value_import / self.battery_loss / self.inverter_loss + self.metric_battery_cycle)

            if charge_window_n >= 0:
                rate_str_import = "<b>" + rate_str_import + "</b>"

            if self.rate_export_replicated.get(minute, False):
                rate_str_export = "<i>%02.02f ?</i>" % (rate_value_export)
            else:
                rate_str_export = "%2.02f" % (rate_value_export)

            if plan_debug:
                rate_str_export += " (%02.02f)" % (rate_value_export * self.battery_loss_discharge * self.inverter_loss - self.metric_battery_cycle)

            if discharge_window_n >= 0:
                rate_str_export = "<b>" + rate_str_export + "</b>"

            # Cost
            total_str = "£%02.02f" % (metric_start / 100.0)
            if metric_change >= 10.0:
                cost_str = "+%d p " % int(metric_change)
                cost_str += " &nearr;"
                cost_color = "#F18261"
            elif metric_change >= 0.5:
                cost_str = "+%d p " % int(metric_change)
                cost_str += " &nearr;"
                cost_color = "#FFFF00"
            elif metric_change <= -0.5:
                cost_str = "-%d p " % int(abs(metric_change))
                cost_str += " &searr;"
                cost_color = "#3AEE85"
            else:
                cost_str = " &rarr;"
                cost_color = "#FFFFFF"

            # Car charging?
            if self.num_cars > 0:
                car_charging_kwh = 0.0
                for car_n in range(0, self.num_cars):
                    for window in self.car_charging_slots[car_n]:
                        start = window["start"]
                        end = window["end"]
                        kwh = self.dp2(window["kwh"]) / (end - start)
                        for offset in range(0, 30, PREDICT_STEP):
                            minute_offset = minute + offset
                            if minute_offset >= start and minute_offset < end:
                                car_charging_kwh += kwh * PREDICT_STEP
                car_charging_kwh = self.dp2(car_charging_kwh)
                if car_charging_kwh > 0.0:
                    car_charging_str = str(car_charging_kwh)
                    car_color = "FFFF00"
                else:
                    car_charging_str = ""
                    car_color = "#FFFFFF"

            # IBoost
            iboost_amount_str = ""
            iboost_color = "#FFFFFF"
            if self.iboost_enable:
                iboost_amount = self.predict_iboost_best.get(minute_relative, 0)
                iboost_change = self.predict_iboost_best.get(minute_relative + 30, 0) - iboost_amount
                iboost_amount_str = str(self.dp2(iboost_amount))
                if iboost_change > 0:
                    iboost_color = "#FFFF00"
                    iboost_amount_str += " &nearr;"

            # Table row
            html += '<tr style="color:black">'
            html += "<td bgcolor=#FFFFFF>" + rate_start.strftime("%a %H:%M") + "</td>"
            html += "<td bgcolor=" + rate_color_import + ">" + str(rate_str_import) + " </td>"
            html += "<td bgcolor=" + rate_color_export + ">" + str(rate_str_export) + " </td>"
            if start_span:
                if split:  # for slots that are both charging and discharging, just output the (split cell) state
                    html += "<td "
                else:  # otherwise (non-split slots), display the state spanning over two cells
                    html += "<td colspan=2 "
                html += "rowspan=" + str(rowspan) + " bgcolor=" + state_color + ">" + state + "</td>"
                html += "<td rowspan=" + str(rowspan) + " bgcolor=#FFFFFF> " + show_limit + "</td>"
            elif not in_span:
                if split:
                    html += "<td "
                else:
                    html += "<td colspan=2 "
                html += "bgcolor=" + state_color + ">" + state + "</td>"
                html += "<td bgcolor=#FFFFFF> " + show_limit + "</td>"
            html += "<td bgcolor=" + pv_color + ">" + str(pv_forecast) + pv_symbol + "</td>"
            html += "<td bgcolor=" + load_color + ">" + str(load_forecast) + "</td>"
            if self.num_cars > 0:  # Don't display car charging data if there's no car
                html += "<td bgcolor=" + car_color + ">" + car_charging_str + "</td>"
            if self.iboost_enable:
                html += "<td bgcolor=" + iboost_color + ">" + iboost_amount_str + " </td>"
            html += "<td bgcolor=" + soc_color + ">" + str(soc_percent) + soc_sym + "</td>"
            html += "<td bgcolor=" + cost_color + ">" + str(cost_str) + "</td>"
            html += "<td bgcolor=#FFFFFF>" + str(total_str) + "</td>"
            html += "</tr>"
        html += "</table>"
        self.dashboard_item(self.prefix + ".plan_html", state="", attributes={"html": html, "friendly_name": "Plan in HTML", "icon": "mdi:web-box"})

    def publish_rates(self, rates, export):
        """
        Publish the rates for charts
        Create rates/time every 30 minutes
        """
        rates_time = {}
        for minute in range(0, self.minutes_now + self.forecast_minutes + 24 * 60, 30):
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            rates_time[stamp] = self.dp2(rates[minute])

        if export:
            self.publish_rates_export()
        else:
            self.publish_rates_import()

        if not SIMULATE:
            if export:
                self.dashboard_item(
                    self.prefix + ".rates_export",
                    state=self.dp2(rates[self.minutes_now]),
                    attributes={
                        "min": self.dp2(self.rate_export_min),
                        "max": self.dp2(self.rate_export_max),
                        "average": self.dp2(self.rate_export_average),
                        "threshold": self.dp2(self.rate_export_cost_threshold),
                        "results": rates_time,
                        "friendly_name": "Export rates",
                        "state_class": "measurement",
                        "unit_of_measurement": "p",
                        "icon": "mdi:currency-usd",
                    },
                )
            else:
                self.dashboard_item(
                    self.prefix + ".rates",
                    state=self.dp2(rates[self.minutes_now]),
                    attributes={
                        "min": self.dp2(self.rate_min),
                        "max": self.dp2(self.rate_max),
                        "average": self.dp2(self.rate_average),
                        "threshold": self.dp2(self.rate_import_cost_threshold),
                        "results": rates_time,
                        "friendly_name": "Import rates",
                        "state_class": "measurement",
                        "unit_of_measurement": "p",
                        "icon": "mdi:currency-usd",
                    },
                )
        return rates

    def today_cost(self, import_today, export_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_cost_import = 0
        day_cost_export = 0
        day_energy = 0
        day_energy_export = 0
        day_cost_time = {}
        day_cost_time_import = {}
        day_cost_time_export = {}

        for minute in range(0, self.minutes_now):
            # Add in standing charge
            if (minute % (24 * 60)) == 0:
                day_cost += self.metric_standing_charge
                day_cost_import += self.metric_standing_charge

            minute_back = self.minutes_now - minute - 1
            energy = 0
            energy = self.get_from_incrementing(import_today, minute_back)
            if export_today:
                energy_export = self.get_from_incrementing(export_today, minute_back)
            else:
                energy_export = 0
            day_energy += energy
            day_energy_export += energy_export

            if self.rate_import:
                day_cost += self.rate_import[minute] * energy
                day_cost_import += self.rate_import[minute] * energy

            if self.rate_export:
                day_cost -= self.rate_export[minute] * energy_export
                day_cost_export -= self.rate_export[minute] * energy_export

            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = self.dp2(day_cost)
                day_cost_time_import[stamp] = self.dp2(day_cost_import)
                day_cost_time_export[stamp] = self.dp2(day_cost_export)

        if not SIMULATE:
            self.dashboard_item(
                self.prefix + ".cost_today",
                state=self.dp2(day_cost),
                attributes={"results": day_cost_time, "friendly_name": "Cost so far today", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"},
            )
            self.dashboard_item(
                self.prefix + ".cost_today_import",
                state=self.dp2(day_cost_import),
                attributes={
                    "results": day_cost_time_import,
                    "friendly_name": "Cost so far today import",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(
                self.prefix + ".cost_today_export",
                state=self.dp2(day_cost_export),
                attributes={
                    "results": day_cost_time_export,
                    "friendly_name": "Cost so far today export",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi:currency-usd",
                },
            )
        self.log(
            "Todays energy import {} kWh export {} kWh cost {} p import {} p export {} p".format(
                self.dp2(day_energy), self.dp2(day_energy_export), self.dp2(day_cost), self.dp2(day_cost_import), self.dp2(day_cost_export)
            )
        )
        return day_cost

    def publish_discharge_limit(self, discharge_window, discharge_limits, best):
        """
        Create entity to chart discharge limit
        """
        discharge_limit_time = {}
        discharge_limit_time_kw = {}

        discharge_limit_soc = self.soc_max
        discharge_limit_percent = 100
        discharge_limit_first = False
        prev_limit = -1

        for minute in range(0, self.forecast_minutes + self.minutes_now, 5):
            window_n = self.in_charge_window(discharge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window_n >= 0 and (discharge_limits[window_n] < 100.0):
                soc_perc = discharge_limits[window_n]
                soc_kw = (soc_perc * self.soc_max) / 100.0
                if not discharge_limit_first:
                    discharge_limit_soc = soc_kw
                    discharge_limit_percent = discharge_limits[window_n]
                    discharge_limit_first = True
            else:
                soc_perc = 100
                soc_kw = self.soc_max
            if prev_limit != soc_perc:
                discharge_limit_time[stamp] = soc_perc
                discharge_limit_time_kw[stamp] = self.dp2(soc_kw)
            prev_limit = soc_perc

        if not SIMULATE:
            discharge_start_str = "undefined"
            discharge_end_str = "undefined"
            discharge_start_date = None
            discharge_end_date = None
            discharge_average = None

            if discharge_window and (discharge_window[0]["end"] < (24 * 60 + self.minutes_now)):
                discharge_start_minutes = discharge_window[0]["start"]
                discharge_end_minutes = discharge_window[0]["end"]
                discharge_average = discharge_window[0].get("average", None)

                time_format_time = "%H:%M:%S"
                discharge_startt = self.midnight_utc + timedelta(minutes=discharge_start_minutes)
                discharge_endt = self.midnight_utc + timedelta(minutes=discharge_end_minutes)
                discharge_start_str = discharge_startt.strftime(time_format_time)
                discharge_end_str = discharge_endt.strftime(time_format_time)
                discharge_start_date = discharge_startt.strftime(TIME_FORMAT)
                discharge_end_date = discharge_endt.strftime(TIME_FORMAT)

            if best:
                self.dashboard_item(
                    self.prefix + ".best_discharge_limit_kw",
                    state=self.dp2(discharge_limit_soc),
                    attributes={
                        "results": discharge_limit_time_kw,
                        "friendly_name": "Predicted discharge limit kWh best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-charging",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_discharge_limit",
                    state=discharge_limit_percent,
                    attributes={
                        "results": discharge_limit_time,
                        "rate": discharge_average,
                        "friendly_name": "Predicted discharge limit best",
                        "state_class": "measurement",
                        "unit_of_measurement": "%",
                        "icon": "mdi:battery-charging",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_discharge_start",
                    state=discharge_start_str,
                    attributes={
                        "timestamp": discharge_start_date,
                        "friendly_name": "Predicted discharge start time best",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_discharge_end",
                    state=discharge_end_str,
                    attributes={
                        "timestamp": discharge_end_date,
                        "friendly_name": "Predicted discharge end time best",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )
            else:
                self.dashboard_item(
                    self.prefix + ".discharge_limit_kw",
                    state=self.dp2(discharge_limit_soc),
                    attributes={
                        "results": discharge_limit_time_kw,
                        "friendly_name": "Predicted discharge limit kWh",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-charging",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".discharge_limit",
                    state=discharge_limit_percent,
                    attributes={
                        "results": discharge_limit_time,
                        "rate": discharge_average,
                        "friendly_name": "Predicted discharge limit",
                        "state_class": "measurement",
                        "unit_of_measurement": "%",
                        "icon": "mdi:battery-charging",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".discharge_start",
                    state=discharge_start_str,
                    attributes={
                        "timestamp": discharge_start_date,
                        "friendly_name": "Predicted discharge start time",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".discharge_end",
                    state=discharge_end_str,
                    attributes={
                        "timestamp": discharge_end_date,
                        "friendly_name": "Predicted discharge end time",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )

    def publish_charge_limit(self, charge_limit, charge_window, charge_limit_percent, best=False, soc={}):
        """
        Create entity to chart charge limit
        """
        charge_limit_time = {}
        charge_limit_time_kw = {}
        prev_perc = -1

        for minute in range(0, self.forecast_minutes + self.minutes_now, 5):
            window = self.in_charge_window(charge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window >= 0:
                soc_perc = charge_limit_percent[window]
                soc_kw = charge_limit[window]
            else:
                soc_perc = 0
                soc_kw = 0

            # Convert % of charge freeze to current SOC number
            if self.set_charge_freeze and (soc_perc == self.reserve_percent):
                offset = int((minute - self.minutes_now) / 5) * 5
                soc_kw = soc.get(offset, soc_kw)

            if prev_perc != soc_perc:
                charge_limit_time[stamp] = soc_perc
                charge_limit_time_kw[stamp] = soc_kw
            prev_perc = soc_perc

        if not SIMULATE:
            charge_limit_first = 0
            charge_limit_percent_first = 0
            charge_average_first = None
            charge_start_str = "undefined"
            charge_end_str = "undefined"
            charge_start_date = None
            charge_end_date = None

            if charge_limit:
                # Ignore charge windows beyond 24 hours away as they won't apply right now
                if charge_window[0]["end"] <= (24 * 60 + self.minutes_now):
                    charge_limit_first = charge_limit[0]
                    charge_limit_percent_first = charge_limit_percent[0]
                    charge_start_minutes = charge_window[0]["start"]
                    charge_end_minutes = charge_window[0]["end"]
                    charge_average_first = charge_window[0].get("average", None)

                    time_format_time = "%H:%M:%S"
                    charge_startt = self.midnight_utc + timedelta(minutes=charge_start_minutes)
                    charge_endt = self.midnight_utc + timedelta(minutes=charge_end_minutes)
                    charge_start_str = charge_startt.strftime(time_format_time)
                    charge_end_str = charge_endt.strftime(time_format_time)
                    charge_start_date = charge_startt.strftime(TIME_FORMAT)
                    charge_end_date = charge_endt.strftime(TIME_FORMAT)

            if best:
                self.dashboard_item(
                    self.prefix + ".best_charge_limit_kw",
                    state=self.dp2(charge_limit_first),
                    attributes={
                        "results": charge_limit_time_kw,
                        "friendly_name": "Predicted charge limit kWh best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-charging",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_charge_limit",
                    state=charge_limit_percent_first,
                    attributes={
                        "results": charge_limit_time,
                        "friendly_name": "Predicted charge limit best",
                        "state_class": "measurement",
                        "unit_of_measurement": "%",
                        "icon": "mdi:battery-charging",
                        "rate": charge_average_first,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_charge_start",
                    state=charge_start_str,
                    attributes={
                        "timestamp": charge_start_date,
                        "friendly_name": "Predicted charge start time best",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_charge_end",
                    state=charge_end_str,
                    attributes={
                        "timestamp": charge_end_date,
                        "friendly_name": "Predicted charge end time best",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )
            else:
                self.dashboard_item(
                    self.prefix + ".charge_limit_kw",
                    state=self.dp2(charge_limit_first),
                    attributes={
                        "results": charge_limit_time_kw,
                        "friendly_name": "Predicted charge limit kWh",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-charging",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".charge_limit",
                    state=charge_limit_percent_first,
                    attributes={
                        "results": charge_limit_time,
                        "friendly_name": "Predicted charge limit",
                        "state_class": "measurement",
                        "unit_of_measurement": "%",
                        "icon": "mdi:battery-charging",
                        "rate": charge_average_first,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".charge_start",
                    state=charge_start_str,
                    attributes={
                        "timestamp": charge_start_date,
                        "friendly_name": "Predicted charge start time",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".charge_end",
                    state=charge_end_str,
                    attributes={
                        "timestamp": charge_end_date,
                        "friendly_name": "Predicted charge end time",
                        "state_class": "measurement",
                        "state_class": "timestamp",
                        "icon": "mdi:table-clock",
                        "unit_of_measurement": None,
                    },
                )

    def reset(self):
        """
        Init stub
        """
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""
        self.config_index = {}
        self.dashboard_index = []
        self.prefix = self.args.get("prefix", "predbat")
        self.previous_status = None
        self.had_errors = False
        self.expert_mode = False
        self.plan_valid = False
        self.plan_last_updated = None
        self.plan_last_updated_minutes = 0
        self.calculate_plan_every = 5
        self.prediction_started = False
        self.update_pending = True
        self.midnight = None
        self.midnight_utc = None
        self.difference_minutes = 0
        self.minutes_now = 0
        self.minutes_to_midnight = 0
        self.days_previous = [7]
        self.days_previous_weight = [1]
        self.forecast_days = 0
        self.forecast_minutes = 0
        self.soc_kw = 0
        self.soc_max = 10.0
        self.end_record = 24 * 60 * 2
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.predict_iboost_best = {}
        self.predict_metric_best = {}
        self.metric_min_improvement = 0.0
        self.metric_min_improvement_discharge = 0.0
        self.metric_battery_cycle = 0.0
        self.metric_battery_value_scaling = 1.0
        self.metric_future_rate_offset_import = 0.0
        self.metric_future_rate_offset_export = 0.0
        self.metric_inday_adjust_damping = 1.0
        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.high_export_rates = []
        self.cost_today_sofar = 0
        self.octopus_slots = []
        self.car_charging_slots = []
        self.reserve = 0
        self.reserve_current = 0
        self.battery_loss = 1.0
        self.battery_loss_discharge = 1.0
        self.inverter_loss = 1.0
        self.inverter_hybrid = True
        self.inverter_soc_reset = False
        self.battery_scaling = 1.0
        self.best_soc_min = 0
        self.best_soc_max = 0
        self.best_soc_margin = 0
        self.best_soc_keep = 0
        self.rate_min = 0
        self.rate_min_minute = 0
        self.rate_min_forward = {}
        self.rate_max = 0
        self.rate_max_minute = 0
        self.rate_export_cost_threshold = 99
        self.rate_import_cost_threshold = 99
        self.rate_best_cost_threshold_charge = None
        self.rate_best_cost_threshold_discharge = None
        self.rate_average = 0
        self.rate_export_min = 0
        self.rate_export_min_minute = 0
        self.rate_export_max = 0
        self.rate_export_max_minute = 0
        self.rate_export_average = 0
        self.set_soc_minutes = 30
        self.set_window_minutes = 30
        self.debug_enable = False
        self.import_today = {}
        self.import_today_now = 0
        self.export_today = {}
        self.export_today_now = 0
        self.pv_today = {}
        self.pv_today_now = 0
        self.io_adjusted = {}
        self.current_charge_limit = 0.0
        self.charge_limit = []
        self.charge_limit_percent = []
        self.charge_limit_best = []
        self.charge_limit_best_percent = []
        self.charge_window = []
        self.charge_window_best = []
        self.car_charging_battery_size = [100]
        self.car_charging_limit = [100]
        self.car_charging_soc = [0]
        self.car_charging_rate = [7.4]
        self.car_charging_loss = 1.0
        self.discharge_window = []
        self.discharge_limits = []
        self.discharge_limits_best = []
        self.discharge_window_best = []
        self.battery_rate_max_charge = 0
        self.battery_rate_max_discharge = 0
        self.battery_rate_max_charge_scaled = 0
        self.battery_rate_max_discharge_scaled = 0
        self.battery_rate_min = 0
        self.charge_rate_now = 0
        self.discharge_rate_now = 0
        self.car_charging_hold = False
        self.car_charging_threshold = 99
        self.car_charging_energy = {}
        self.simulate_offset = 0
        self.sim_soc = 0
        self.sim_soc_kw = 0
        self.sim_reserve = 4
        self.sim_inverter_mode = "Eco"
        self.sim_charge_start_time = "00:00:00"
        self.sim_charge_end_time = "00:00:00"
        self.sim_discharge_start = "00:00"
        self.sim_discharge_end = "23:59"
        self.sim_charge_schedule_enable = "on"
        self.sim_charge_rate_now = 2600
        self.sim_discharge_rate_now = 2600
        self.sim_soc_charge = []
        self.notify_devices = ["notify"]
        self.octopus_url_cache = {}
        self.futurerate_url_cache = {}
        self.ge_url_cache = {}
        self.github_url_cache = {}
        self.load_minutes = {}
        self.load_minutes_now = 0
        self.load_minutes_age = 0
        self.battery_capacity_nominal = False
        self.releases = {}
        self.balance_inverters_enable = False
        self.balance_inverters_charge = True
        self.balance_inverters_discharge = True
        self.balance_inverters_crosscharge = True
        self.balance_inverters_threshold_charge = 1.0
        self.balance_inverters_threshold_discharge = 1.0
        self.load_inday_adjustment = 1.0
        self.set_read_only = True
        self.metric_cloud_coverage = 0.0
        self.future_energy_rates_import = {}
        self.future_energy_rates_export = {}

    def optimise_charge_limit_price(
        self,
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        try_charge_limit,
        charge_window,
        discharge_window,
        discharge_limits,
        load_minutes_step,
        load_minutes_step10,
        pv_forecast_minute_step,
        pv_forecast_minute10_step,
        end_record=None,
    ):
        """
        Pick an import price threshold which gives the best results
        """
        loop_price = price_set[-1]
        best_price = loop_price
        best_metric = 9999999
        best_keep = 0
        try_discharge = discharge_limits.copy()
        best_limits = try_charge_limit.copy()
        best_discharge = try_discharge.copy()
        best_soc_min = self.reserve
        best_cost = 0
        best_price_charge = price_set[-1]
        best_price_discharge = price_set[0]
        tried_list = {}

        # Do we loop on discharge?
        if self.calculate_best_discharge and self.calculate_discharge_first:
            discharge_enable_options = [False, True]
        else:
            discharge_enable_options = [False]

        # Most expensive first
        all_prices = price_set[::] + [self.dp1(price_set[-1] - 1)]
        self.log("All prices {}".format(all_prices))
        window_prices = {}
        window_prices_discharge = {}
        for loop_price in all_prices:
            for modulo in [2, 3, 4, 8, 16]:
                for divide in [1, 2, 3, 4, 8, 16, 96]:
                    all_n = []
                    all_d = []
                    highest_price_charge = price_set[-1]
                    lowest_price_discharge = price_set[0]
                    divide_count_c = 0
                    divide_count_d = 0
                    first_charge = True
                    first_discharge = True

                    for price in price_set:
                        links = price_links[price]
                        if loop_price >= price:
                            for key in links:
                                window_n = window_index[key]["id"]
                                typ = window_index[key]["type"]
                                if typ == "c":
                                    window_prices[window_n] = price
                                    if first_charge:
                                        if (int(divide_count_c / divide) % modulo) == 0:
                                            all_n.append(window_n)
                                        divide_count_c += 1
                                    else:
                                        all_n.append(window_n)
                            if all_n:
                                first_charge = False
                        else:
                            # For prices above threshold try discharge
                            for key in links:
                                typ = window_index[key]["type"]
                                window_n = window_index[key]["id"]
                                if typ == "d":
                                    window_prices_discharge[window_n] = price
                                    if first_discharge:
                                        if (int(divide_count_d / divide) % modulo) == 0:
                                            all_d.append(window_n)
                                        divide_count_d += 1
                                    else:
                                        all_d.append(window_n)
                            if all_d:
                                first_discharge = False

                    for discharge_enable in discharge_enable_options:
                        # This price band setting for charge
                        try_charge_limit = best_limits.copy()
                        for window_n in range(0, record_charge_windows):
                            if window_n in all_n:
                                if window_prices[window_n] > highest_price_charge:
                                    highest_price_charge = window_prices[window_n]
                                try_charge_limit[window_n] = self.soc_max
                            else:
                                try_charge_limit[window_n] = 0

                        # Try discharge on/off
                        try_discharge = discharge_limits.copy()
                        if discharge_enable:
                            if not all_d:
                                continue

                            for window_n in all_d:
                                hit_charge = self.hit_charge_window(
                                    self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                                )
                                if not self.calculate_discharge_oncharge and hit_charge >= 0 and try_charge_limit[hit_charge] > 0.0:
                                    continue
                                if window_prices_discharge[window_n] < lowest_price_discharge:
                                    lowest_price_discharge = window_prices_discharge[window_n]
                                try_discharge[window_n] = 0

                        # Skip this one as it's the same as selected already
                        try_hash = str(try_charge_limit) + "_d_" + str(try_discharge)
                        if try_hash in tried_list:
                            if self.debug_enable and 0:
                                self.log(
                                    "Skip this optimisation with divide {} windows {} discharge windows {} discharge_enable {} as it's the same as previous ones".format(
                                        divide, all_n, all_d, discharge_enable
                                    )
                                )
                            continue
                        tried_list[try_hash] = True

                        # Turn off debug for this sim
                        was_debug = self.debug_enable
                        self.debug_enable = False

                        # Simulate with medium PV
                        metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                            try_charge_limit, charge_window, discharge_window, try_discharge, load_minutes_step, pv_forecast_minute_step, end_record=end_record
                        )

                        # Debug re-enable if it was on
                        self.debug_enable = was_debug

                        # Store simulated mid value
                        metric = metricmid
                        cost = metricmid

                        # Balancing payment to account for battery left over
                        # ie. how much extra battery is worth to us in future, assume it's the same as low rate
                        rate_min = self.rate_min_forward.get(end_record, self.rate_min) / self.inverter_loss / self.battery_loss
                        metric -= (
                            (soc + final_iboost) * max(rate_min, 1.0, self.rate_export_min * self.inverter_loss * self.battery_loss_discharge) * self.metric_battery_value_scaling
                        )

                        # Adjustment for battery cycles metric
                        metric += battery_cycle * self.metric_battery_cycle + metric_keep

                        # Optimise
                        if self.debug_enable and 0:
                            if discharge_enable:
                                self.log(
                                    "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} windows {} discharge on {}".format(
                                        loop_price, divide, modulo, self.dp2(metric), self.dp2(metric_keep), self.dp2(soc_min), all_n, all_d
                                    )
                                )
                            else:
                                self.log(
                                    "Optimise all for buy/sell price band <= {} metric {} keep {} soc_min {} windows {} discharge off".format(
                                        loop_price, divide, modulo, self.dp2(metric), self.dp2(metric_keep), self.dp2(soc_min), all_n
                                    )
                                )

                        # For the first pass just pick the most cost effective threshold, consider soc keep later
                        if metric < best_metric:
                            best_metric = metric
                            best_keep = metric_keep
                            best_price = loop_price
                            best_price_charge = highest_price_charge
                            best_price_discharge = lowest_price_discharge
                            best_limits = try_charge_limit.copy()
                            best_discharge = try_discharge.copy()
                            best_soc_min = soc_min
                            best_cost = cost
                            self.log(
                                "Optimise all charge found best buy/sell price band {} best price threshold {} at metric {} keep {} cost {} limits {} discharge {}".format(
                                    loop_price, best_price_charge, self.dp2(best_metric), self.dp2(best_keep), self.dp2(best_cost), best_limits, best_discharge
                                )
                            )
        self.log(
            "Optimise all charge for all bands best price threshold {} charges at {} at metric {} keep {} cost {} soc_min {} limits {} discharge {}".format(
                self.dp2(best_price),
                self.dp2(best_price_charge),
                self.dp2(best_metric),
                self.dp2(best_keep),
                self.dp2(best_cost),
                self.dp2(best_soc_min),
                best_limits,
                best_discharge,
            )
        )
        return best_limits, best_discharge, best_price_charge, best_price_discharge

    def optimise_charge_limit(
        self,
        window_n,
        record_charge_windows,
        charge_limit,
        charge_window,
        discharge_window,
        discharge_limits,
        load_minutes_step,
        load_minutes_step10,
        pv_forecast_minute_step,
        pv_forecast_minute10_step,
        all_n=None,
        end_record=None,
    ):
        """
        Optimise a single charging window for best SOC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_soc_min = 0
        best_soc_min_minute = 0
        best_metric = 9999999
        best_cost = 0
        prev_soc = self.soc_max + 1
        prev_metric = 9999999
        best_soc_step = self.best_soc_step
        best_keep = 0
        max_soc = self.soc_max
        min_soc = 0
        try_charge_limit = copy.deepcopy(charge_limit)

        # For single windows, if the size is 30 minutes or less then use a larger step
        if not all_n:
            start = charge_window[window_n]["start"]
            end = charge_window[window_n]["end"]
            if (end - start) <= 30 and best_soc_step < 1:
                best_soc_step *= 2

        # Start the loop at the max soc setting
        if self.best_soc_max > 0:
            loop_soc = min(loop_soc, self.best_soc_max)

        # Assemble list of SOCs to try
        try_socs = []
        loop_step = max(best_soc_step, 0.1)
        best_soc_min = self.best_soc_min
        if best_soc_min > 0:
            best_soc_min = max(self.reserve, best_soc_min)
        while loop_soc > self.reserve:
            try_soc = max(best_soc_min, loop_soc)
            try_soc = self.dp2(min(try_soc, self.soc_max))
            if try_soc not in try_socs:
                try_socs.append(self.dp2(try_soc))
            loop_soc -= loop_step
        # Give priority to off to avoid spurious charge freezes
        if best_soc_min not in try_socs:
            try_socs.append(best_soc_min)
        if self.set_charge_freeze and (self.reserve not in try_socs):
            try_socs.append(self.reserve)

        window_results = {}
        first_window = True
        for try_soc in try_socs:
            was_debug = self.debug_enable
            self.debug_enable = False

            if not first_window and not all_n:
                # If the SOC is already saturated then those values greater than what was achieved but not 100% won't do anything
                if try_soc > (max_soc + loop_step):
                    self.debug_enable = was_debug
                    if self.debug_enable and 0:
                        self.log("Skip redundant try soc {} for window {} min_soc {} max_soc {}".format(try_soc, window_n, min_soc, max_soc))
                    continue
                if (try_soc > self.reserve) and (try_soc > self.best_soc_min) and (try_soc < (min_soc - loop_step)):
                    self.debug_enable = was_debug
                    if self.debug_enable and 0:
                        self.log("Skip redundant try soc {} for window {} min_soc {} max_soc {}".format(try_soc, window_n, max_soc, min_soc))
                    continue

            # Get data for 'off' also
            if first_window and not all_n:
                try_charge_limit[window_n] = 0
                metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                    try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute_step, end_record=end_record
                )
                predict_soc_nom = self.predict_soc.copy()
                (
                    metric10,
                    import_kwh_battery10,
                    import_kwh_house10,
                    export_kwh10,
                    soc_min10,
                    soc10,
                    soc_min_minute10,
                    battery_cycle10,
                    metric_keep10,
                    final_iboost10,
                ) = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step10, pv_forecast_minute10_step, end_record=end_record)

                window = charge_window[window_n]
                predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
                if (predict_minute_start in self.predict_soc) and (predict_minute_end in self.predict_soc):
                    min_soc = min(
                        self.predict_soc[predict_minute_start], self.predict_soc[predict_minute_end], predict_soc_nom[predict_minute_start], predict_soc_nom[predict_minute_end]
                    )

            # Store try value into the window, either all or just this one
            if all_n:
                for window_id in all_n:
                    try_charge_limit[window_id] = try_soc
            else:
                try_charge_limit[window_n] = try_soc

            # Simulate with medium PV
            metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute_step, end_record=end_record
            )
            predict_soc_nom = self.predict_soc.copy()

            # Simulate with 10% PV
            (
                metric10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
            ) = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step10, pv_forecast_minute10_step, end_record=end_record)

            # Get data for fully on
            if first_window and not all_n:
                window = charge_window[window_n]
                predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
                if (predict_minute_start in self.predict_soc) and (predict_minute_end in self.predict_soc):
                    max_soc = max(
                        self.predict_soc[predict_minute_start], self.predict_soc[predict_minute_end], predict_soc_nom[predict_minute_start], predict_soc_nom[predict_minute_end]
                    )

            # Store simulated mid value
            metric = metricmid
            cost = metricmid

            # Balancing payment to account for battery left over
            # ie. how much extra battery is worth to us in future, assume it's the same as low rate
            rate_min = self.rate_min_forward.get(end_record, self.rate_min) / self.inverter_loss / self.battery_loss
            metric -= (soc + final_iboost) * max(rate_min, 1.0, self.rate_export_min * self.inverter_loss * self.battery_loss_discharge) * self.metric_battery_value_scaling
            metric10 -= (soc10 + final_iboost10) * max(rate_min, 1.0, self.rate_export_min * self.inverter_loss * self.battery_loss_discharge) * self.metric_battery_value_scaling

            # Metric adjustment based on 10% outcome weighting
            if metric10 > metric:
                metric_diff = metric10 - metric
                metric_diff *= self.pv_metric10_weight
                metric += metric_diff

            # Adjustment for battery cycles metric
            metric += battery_cycle * self.metric_battery_cycle + metric_keep
            metric10 += battery_cycle10 * self.metric_battery_cycle + metric_keep10

            # Metric adjustment based on current charge limit when inside the window
            # to try to avoid constant small changes to SOC target
            if not all_n and (window_n == self.in_charge_window(charge_window, self.minutes_now)):
                try_percent = self.calc_percent_limit(try_soc)
                compare_with = max(self.current_charge_limit, self.reserve_percent)

                if abs(compare_with - try_percent) <= 2:
                    metric -= max(0.5, self.metric_min_improvement)

            # Minor weighting against charge freeze to avoid spurious ones
            if self.set_charge_freeze and try_soc == self.reserve:
                metric += 0.1

            # Preference to 100%
            if try_soc == self.soc_max:
                metric -= 0.01

            self.debug_enable = was_debug
            if self.debug_enable and 0:
                self.log(
                    "Sim: SOC {} window {} imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} keep {} metricmid {} metric10 {}".format(
                        try_soc,
                        window_n,
                        self.dp2(import_kwh_battery),
                        self.dp2(import_kwh_house),
                        self.dp2(export_kwh),
                        self.dp2(soc_min),
                        self.time_abs_str(soc_min_minute),
                        self.dp2(soc),
                        self.dp2(cost),
                        self.dp2(metric),
                        self.dp2(metric_keep),
                        self.dp2(metricmid),
                        self.dp2(metric10),
                    )
                )

            window_results[try_soc] = self.dp2(metric)

            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold
            if (metric + self.metric_min_improvement) <= best_metric:
                best_metric = metric
                best_soc = try_soc
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_keep = metric_keep

            prev_soc = try_soc
            prev_metric = metric
            first_window = False

        # Add margin last
        best_soc = min(best_soc + self.best_soc_margin, self.soc_max)

        if not all_n:
            self.log(
                "Try optimising charge window(s)    {}: {} - {} price {} cost {} metric {} keep {} selected {} was {} results {}".format(
                    window_n,
                    self.time_abs_str(window["start"]),
                    self.time_abs_str(window["end"]),
                    charge_window[window_n]["average"],
                    self.dp2(best_cost),
                    self.dp2(best_metric),
                    self.dp2(best_keep),
                    best_soc,
                    charge_limit[window_n],
                    window_results,
                )
            )
        else:
            self.log(
                "Try optimising charge window(s)    {}: price {} cost {} metric {} keep {} selected {} was {} results {}".format(
                    all_n, charge_window[window_n]["average"], self.dp2(best_cost), self.dp2(best_metric), self.dp2(best_keep), best_soc, charge_limit[window_n], window_results
                )
            )
        return best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep

    def optimise_discharge(
        self,
        window_n,
        record_charge_windows,
        try_charge_limit,
        charge_window,
        discharge_window,
        discharge_limit,
        load_minutes_step,
        load_minutes_step10,
        pv_forecast_minute_step,
        pv_forecast_minute10_step,
        all_n=None,
        end_record=None,
    ):
        """
        Optimise a single discharging window for best discharge %
        """
        best_discharge = False
        best_metric = 9999999
        off_metric = 9999999
        best_cost = 0
        best_soc_min = 0
        best_soc_min_minute = 0
        best_keep = 0
        this_discharge_limit = 100.0
        prev_discharge_limit = 0.0
        window = discharge_window[window_n]
        try_discharge_window = copy.deepcopy(discharge_window)
        try_discharge = copy.deepcopy(discharge_limit)
        best_start = window["start"]
        best_size = window["end"] - best_start
        discharge_step = 5

        # loop on each discharge option
        if self.set_discharge_freeze and not self.set_discharge_freeze_only:
            # If we support freeze, try a 99% option which will freeze at any SOC level below this
            loop_options = [100, 99, 0]
        else:
            loop_options = [100, 0]

        window_results = {}
        for loop_limit in loop_options:
            # Loop on window size
            loop_start = window["end"] - 10  # Minimum discharge window size 10 minutes
            while loop_start >= window["start"]:
                this_discharge_limit = loop_limit
                start = loop_start

                # Move the loop start back to full size
                loop_start -= discharge_step

                # Can't optimise all window start slot
                if all_n and (start != window["start"]):
                    continue

                # Don't optimise start of disabled windows or freeze only windows, just for discharge ones
                if (this_discharge_limit in [100.0, 99.0]) and (start != window["start"]):
                    continue

                # Never go below the minimum level
                this_discharge_limit = max(self.calc_percent_limit(max(self.best_soc_min, self.reserve)), this_discharge_limit)

                # Store try value into the window
                if all_n:
                    for window_id in all_n:
                        try_discharge[window_id] = this_discharge_limit
                else:
                    try_discharge[window_n] = this_discharge_limit
                    # Adjust start
                    start = min(start, window["end"] - 5)
                    try_discharge_window[window_n]["start"] = start

                was_debug = self.debug_enable
                self.debug_enable = False

                # Simulate with medium PV
                metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                    try_charge_limit, charge_window, try_discharge_window, try_discharge, load_minutes_step, pv_forecast_minute_step, end_record=end_record
                )

                # Simulate with 10% PV
                (
                    metric10,
                    import_kwh_battery10,
                    import_kwh_house10,
                    export_kwh10,
                    soc_min10,
                    soc10,
                    soc_min_minute10,
                    battery_cycle10,
                    metric_keep10,
                    final_iboost10,
                ) = self.run_prediction(try_charge_limit, charge_window, try_discharge_window, try_discharge, load_minutes_step10, pv_forecast_minute10_step, end_record=end_record)

                # Put back debug enable
                self.debug_enable = was_debug

                # Store simulated mid value
                metric = metricmid
                cost = metricmid

                # Balancing payment to account for battery left over
                # ie. how much extra battery is worth to us in future, assume it's the same as low rate
                rate_min = self.rate_min_forward.get(end_record, self.rate_min) / self.inverter_loss / self.battery_loss
                metric -= (soc + final_iboost) * max(rate_min, 1.0, self.rate_export_min * self.inverter_loss * self.battery_loss_discharge) * self.metric_battery_value_scaling
                metric10 -= (
                    (soc10 + final_iboost10) * max(rate_min, 1.0, self.rate_export_min * self.inverter_loss * self.battery_loss_discharge) * self.metric_battery_value_scaling
                )

                # Metric adjustment based on 10% outcome weighting
                if metric10 > metric:
                    metric_diff = metric10 - metric
                    metric_diff *= self.pv_metric10_weight
                    metric += metric_diff
                    metric = self.dp2(metric)

                # Adjustment for battery cycles metric
                metric += battery_cycle * self.metric_battery_cycle + metric_keep
                metric10 += battery_cycle * self.metric_battery_cycle + metric_keep10

                # Adjust to try to keep existing windows
                if window_n < 2 and this_discharge_limit < 100.0 and self.discharge_window:
                    pwindow = discharge_window[window_n]
                    dwindow = self.discharge_window[0]
                    if self.minutes_now >= pwindow["start"] and self.minutes_now < pwindow["end"]:
                        if (self.minutes_now >= dwindow["start"] and self.minutes_now < dwindow["end"]) or (dwindow["end"] == pwindow["start"]):
                            metric -= max(0.5, self.metric_min_improvement_discharge)

                if self.debug_enable:
                    self.log(
                        "Sim: Discharge {} window {} start {} end {}, imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} metricmid {} metric10 {} cycle {} end_record {}".format(
                            this_discharge_limit,
                            window_n,
                            self.time_abs_str(try_discharge_window[window_n]["start"]),
                            self.time_abs_str(try_discharge_window[window_n]["end"]),
                            self.dp2(import_kwh_battery),
                            self.dp2(import_kwh_house),
                            self.dp2(export_kwh),
                            self.dp2(soc_min),
                            self.time_abs_str(soc_min_minute),
                            self.dp2(soc),
                            self.dp2(cost),
                            self.dp2(metric),
                            self.dp2(metricmid),
                            self.dp2(metric10),
                            self.dp2(battery_cycle * self.metric_battery_cycle),
                            end_record,
                        )
                    )

                window_size = try_discharge_window[window_n]["end"] - start
                window_key = str(int(this_discharge_limit)) + "_" + str(window_size)
                window_results[window_key] = self.dp2(metric)

                # Only select a discharge if it makes a notable improvement has defined by min_improvement (divided in M windows)
                if ((metric + self.metric_min_improvement_discharge) <= off_metric) and (metric <= best_metric):
                    best_metric = metric
                    best_discharge = this_discharge_limit
                    best_cost = cost
                    best_soc_min = soc_min
                    best_soc_min_minute = soc_min_minute
                    best_start = start
                    best_size = window_size
                    best_keep = metric_keep

                # Store the metric for discharge off
                if off_metric == 9999999:
                    off_metric = metric

        if not all_n:
            self.log(
                "Try optimising discharge window(s) {}: {} - {} price {} cost {} metric {} keep {} selected {}% size {} was {}% results {}".format(
                    window_n,
                    self.time_abs_str(window["start"]),
                    self.time_abs_str(window["end"]),
                    window["average"],
                    self.dp2(best_cost),
                    self.dp2(best_metric),
                    self.dp2(best_keep),
                    best_discharge,
                    best_size,
                    discharge_limit[window_n],
                    window_results,
                )
            )
        else:
            self.log(
                "Try optimising discharge window(s) {} price {} selected {}% size {} cost {} metric {} keep {} results {}".format(
                    all_n,
                    window["average"],
                    self.dp2(best_cost),
                    self.dp2(best_metric),
                    self.dp2(best_keep),
                    best_discharge,
                    best_size,
                    window_results,
                )
            )

        return best_discharge, best_start, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep

    def window_sort_func(self, window):
        """
        Helper sort index function
        """
        return float(window["key"])

    def window_sort_func_start(self, window):
        """
        Helper sort index function
        """
        return float(window["start"])

    def sort_window_by_time(self, windows):
        """
        Sort windows in start time order, return a new list of windows
        """
        window_sorted = copy.deepcopy(windows)
        window_sorted.sort(key=self.window_sort_func_start)
        return window_sorted

    def sort_window_by_price_combined(self, charge_windows, discharge_windows, stand_alone=False, secondary_order=False):
        """
        Sort windows into price sets
        """
        window_sort = []
        window_links = {}
        price_set = []
        price_links = {}

        # Add charge windows
        if self.calculate_best_charge or stand_alone:
            id = 0
            for window in charge_windows:
                # Account for losses in average rate as it makes import higher
                if stand_alone:
                    average = self.dp2(window["average"])
                else:
                    average = self.dp2(window["average"] / self.inverter_loss / self.battery_loss + self.metric_battery_cycle)
                if secondary_order:
                    average_export = (self.rate_export.get(window["start"], 0) + self.rate_export.get(window["end"], 0) - 5) / 2
                else:
                    average_export = 0
                sort_key = "%04.2f_%04.2f_%03d_c%02d" % (5000 - average, 5000 - average_export, 999 - id, id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = self.dp1(average)  # Round to nearest 0.1 penny to avoid too many bands
                id += 1

        # Add discharge windows
        if self.calculate_best_discharge and not stand_alone:
            id = 0
            for window in discharge_windows:
                # Account for losses in average rate as it makes export value lower
                average = self.dp2(window["average"] * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle)
                if secondary_order:
                    average_import = (self.rate_import.get(window["start"], 0) + self.rate_import.get(window["end"], 0) - 5) / 2
                else:
                    average_import = 0
                sort_key = "%04.2f_%04.2f_%03d_d%02d" % (5000 - average, 5000 - average_import, 999 - id, id)
                if not self.calculate_discharge_first:
                    # Push discharge last if first is not set
                    sort_key = "zz_" + sort_key
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "d"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = self.dp1(average)  # Round to nearest 0.1 penny to avoid too many bands
                id += 1

        if window_sort:
            window_sort.sort()

        # Create price ordered links by set
        for key in window_sort:
            average = window_links[key]["average"]
            if average not in price_set:
                price_set.append(average)
                price_links[average] = []
            price_links[average].append(key)

        return window_sort, window_links, price_set, price_links

    def sort_window_by_time_combined(self, charge_windows, discharge_windows):
        window_sort = []
        window_links = {}

        # Add charge windows
        if self.calculate_best_charge:
            id = 0
            for window in charge_windows:
                sort_key = "%04d_%03d_c" % (window["start"], id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                id += 1

        # Add discharge windows
        if self.calculate_best_discharge:
            id = 0
            for window in discharge_windows:
                sort_key = "%04d_%03d_d" % (window["start"], id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "d"
                window_links[sort_key]["id"] = id
                id += 1

        if window_sort:
            window_sort.sort()

        return window_sort, window_links

    def sort_window_by_price(self, windows, reverse_time=False):
        """
        Sort the charge windows by highest price first, return a list of window IDs
        """
        window_with_id = copy.deepcopy(windows)
        wid = 0
        for window in window_with_id:
            window["id"] = wid
            if reverse_time:
                window["key"] = "%04.2f%02d" % (5000 - window["average"], 999 - window["id"])
            else:
                window["key"] = "%04.2f%02d" % (5000 - window["average"], window["id"])
            wid += 1
        window_with_id.sort(key=self.window_sort_func)
        id_list = []
        for window in window_with_id:
            id_list.append(window["id"])
        return id_list

    def remove_intersecting_windows(self, charge_limit_best, charge_window_best, discharge_limit_best, discharge_window_best):
        """
        Filters and removes intersecting charge windows
        """
        clip_again = True

        # For each charge window
        while clip_again:
            clip_again = False
            new_limit_best = []
            new_window_best = []
            for window_n in range(0, len(charge_limit_best)):
                window = charge_window_best[window_n]
                start = window["start"]
                end = window["end"]
                average = window["average"]
                limit = charge_limit_best[window_n]
                clipped = False

                # For each discharge window
                for dwindow_n in range(0, len(discharge_limit_best)):
                    dwindow = discharge_window_best[dwindow_n]
                    dlimit = discharge_limit_best[dwindow_n]
                    dstart = dwindow["start"]
                    dend = dwindow["end"]

                    # Overlapping window with enabled discharge?
                    if (limit > 0.0) and (dlimit < 100.0) and (dstart < end) and (dend >= start):
                        if dstart <= start:
                            if start != dend:
                                start = dend
                                clipped = True
                        elif dend >= end:
                            if end != dstart:
                                end = dstart
                                clipped = True
                        else:
                            # Two segments
                            if (dstart - start) >= 5:
                                new_window = {}
                                new_window["start"] = start
                                new_window["end"] = dstart
                                new_window["average"] = average
                                new_window_best.append(new_window)
                                new_limit_best.append(limit)
                            start = dend
                            clipped = True
                            if (end - start) >= 5:
                                clip_again = True

                if not clipped or ((end - start) >= 5):
                    new_window = {}
                    new_window["start"] = start
                    new_window["end"] = end
                    new_window["average"] = average
                    new_window_best.append(new_window)
                    new_limit_best.append(limit)

            if clip_again:
                charge_window_best = new_window_best.copy()
                charge_limit_best = new_limit_best.copy()

        return new_limit_best, new_window_best

    def discard_unused_charge_slots(self, charge_limit_best, charge_window_best, reserve):
        """
        Filter out unused charge slots (those set at 0)
        """
        new_limit_best = []
        new_window_best = []

        max_slots = len(charge_limit_best)

        for window_n in range(0, max_slots):
            # Only keep slots > than reserve, or keep the last one so we don't have zero slots
            # Also keep a slot if we are already inside it and charging is enabled
            window = charge_window_best[window_n].copy()
            start = window["start"]
            end = window["end"]
            limit = charge_limit_best[window_n]

            if new_window_best and (start == new_window_best[-1]["end"]) and (limit == new_limit_best[-1]):
                new_window_best[-1]["end"] = end
                if self.debug_enable:
                    self.log(
                        "Combine charge slot {} with previous - target soc {} kWh slot {} start {} end {} limit {}".format(
                            window_n, new_limit_best[-1], new_window_best[-1], start, end, limit
                        )
                    )
            elif (limit > 0) or (self.minutes_now >= start and self.minutes_now < end and self.charge_window and self.charge_window[0]["end"] == end):
                new_limit_best.append(limit)
                new_window_best.append(window)
            else:
                if self.debug_enable:
                    self.log("Clip off charge slot {} limit {}".format(window, limit))
        return new_limit_best, new_window_best

    def find_spare_energy(self, predict_soc, predict_export, step, first_charge):
        """
        Find spare energy and set triggers
        """
        triggers = self.args.get("export_triggers", [])
        if not isinstance(triggers, list):
            return

        # Only run if we have export data
        if not predict_export:
            return

        # Check each trigger
        for trigger in triggers:
            total_energy = 0
            name = trigger.get("name", "trigger")
            minutes = trigger.get("minutes", 60.0)
            minutes = min(max(minutes, 0), first_charge)
            energy = trigger.get("energy", 1.0)
            try:
                energy = float(energy)
            except (ValueError, TypeError):
                energy = 0.0
                self.log("WARN: Bad energy value {} provided via trigger {}".format(energy, name))
                self.record_status("ERROR: Bad energy value {} provided via trigger {}".format(energy, name), had_errors=True)

            for minute in range(0, minutes, step):
                total_energy += predict_export[minute]
            sensor_name = "binary_sensor." + self.prefix + "_export_trigger_" + name
            if total_energy >= energy:
                state = "on"
            else:
                state = "off"
            self.log("Evaluate trigger {} results {} total_energy {}".format(trigger, state, self.dp2(total_energy)))
            self.dashboard_item(
                sensor_name,
                state=state,
                attributes={
                    "friendly_name": "Predbat export trigger " + name,
                    "required": energy,
                    "available": self.dp2(total_energy),
                    "minutes": minutes,
                    "icon": "mdi:clock-start",
                },
            )

    def set_charge_discharge_status(self, isCharging, isDischarging):
        """
        Reports status on charging/discharging to binary sensor
        """
        self.dashboard_item(
            "binary_sensor." + self.prefix + "_charging", state="on" if isCharging else "off", attributes={"friendly_name": "Predbat is charging", "icon": "mdi:battery-arrow-up"}
        )
        self.dashboard_item(
            "binary_sensor." + self.prefix + "_discharging",
            state="on" if isDischarging else "off",
            attributes={"friendly_name": "Predbat is discharging", "icon": "mdi:battery-arrow-down"},
        )

    def clip_charge_slots(self, minutes_now, predict_soc, charge_window_best, charge_limit_best, record_charge_windows, step):
        """
        Clip charge slots that are useless as they don't charge at all
        """
        for window_n in range(0, min(record_charge_windows, len(charge_window_best))):
            window = charge_window_best[window_n]
            limit = charge_limit_best[window_n]
            limit_soc = self.soc_max * limit / 100.0
            window_start = max(window["start"], minutes_now)
            window_end = max(window["end"], minutes_now)
            window_length = window_end - window_start

            if limit <= 0.0:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = max(int((window_start - minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window_end - minutes_now) / 5) * 5

                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    soc_start = predict_soc[predict_minute_start]
                    soc_end = predict_soc[predict_minute_end]
                    soc_min = min(soc_start, soc_end)
                    soc_min_percent = self.calc_percent_limit(soc_min)
                    soc_max = max(soc_start, soc_end)

                    if self.debug_enable:
                        self.log(
                            "Examine charge window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(
                                window_n, window_start, window_end, predict_minute_start, limit, soc_start, soc_end
                            )
                        )

                    if (soc_min_percent > self.calc_percent_limit(charge_limit_best[window_n])) and (charge_limit_best[window_n] != self.reserve):
                        charge_limit_best[window_n] = self.best_soc_min
                        self.log(
                            "Clip off charge window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n])
                        )
                    elif soc_max < charge_limit_best[window_n]:
                        # Clip down charge window to what can be achieved in the time
                        limit_soc = min(self.soc_max, soc_max + 10 * self.battery_rate_max_charge_scaled, charge_limit_best[window_n])
                        if self.best_soc_max > 0:
                            limit_soc = min(limit_soc, self.best_soc_max)
                        new_limit = max(limit_soc, self.best_soc_min)
                        if new_limit != charge_limit_best[window_n]:
                            charge_limit_best[window_n] = new_limit
                            if self.debug_enable:
                                self.log(
                                    "Clip down charge window {} from {} - {} from limit {} to new limit {}".format(
                                        window_n, window_start, window_end, limit, charge_limit_best[window_n]
                                    )
                                )

            else:
                self.log("WARN: Clip charge window {} as it's already passed".format(window_n))
                charge_limit_best[window_n] = self.best_soc_min
        return charge_window_best, charge_limit_best

    def clip_discharge_slots(self, minutes_now, predict_soc, discharge_window_best, discharge_limits_best, record_discharge_windows, step):
        """
        Clip discharge slots to the right length
        """
        for window_n in range(0, min(record_discharge_windows, len(discharge_window_best))):
            window = discharge_window_best[window_n]
            limit = discharge_limits_best[window_n]
            limit_soc = self.soc_max * limit / 100.0
            window_start = max(window["start"], minutes_now)
            window_end = max(window["end"], minutes_now)
            window_length = window_end - window_start

            if limit == 100:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = max(int((window_start - minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window_end - minutes_now) / 5) * 5
                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    soc_start = predict_soc[predict_minute_start]
                    soc_end = predict_soc[predict_minute_end]
                    soc_min = min(soc_start, soc_end)
                    soc_max = max(soc_start, soc_end)

                    if self.debug_enable:
                        self.log(
                            "Examine window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(
                                window_n, window_start, window_end, predict_minute_start, limit, soc_start, soc_end
                            )
                        )

                    # Discharge level adjustments for safety
                    if soc_min > limit_soc:
                        # Give it 10 minute margin
                        limit_soc = max(limit_soc, soc_min - 10 * self.battery_rate_max_discharge_scaled)
                        discharge_limits_best[window_n] = self.calc_percent_limit(limit_soc)
                        if limit != discharge_limits_best[window_n]:
                            if self.debug_enable:
                                self.log(
                                    "Clip up discharge window {} from {} - {} from limit {} to new limit {}".format(
                                        window_n, window_start, window_end, limit, discharge_limits_best[window_n]
                                    )
                                )
                    elif soc_max < limit_soc:
                        # Bring down limit to match predicted soc for freeze only mode
                        if self.set_discharge_freeze:
                            # Get it 5 minute margin upwards
                            limit_soc = min(limit_soc, soc_max + 5 * self.battery_rate_max_discharge_scaled)
                            discharge_limits_best[window_n] = self.calc_percent_limit(limit_soc)
                            if limit != discharge_limits_best[window_n]:
                                if self.debug_enable:
                                    self.log(
                                        "Clip down discharge window {} from {} - {} from limit {} to new limit {}".format(
                                            window_n, window_start, window_end, limit, discharge_limits_best[window_n]
                                        )
                                    )
            else:
                self.log("WARN: Clip discharge window {} as it's already passed".format(window_n))
                discharge_limits_best[window_n] = 100
        return discharge_window_best, discharge_limits_best

    def discard_unused_discharge_slots(self, discharge_limits_best, discharge_window_best):
        """
        Filter out the windows we disabled
        """
        new_best = []
        new_enable = []
        for window_n in range(0, len(discharge_limits_best)):
            if discharge_limits_best[window_n] < 100.0:
                # Also merge contiguous enabled windows
                if new_best and (discharge_window_best[window_n]["start"] == new_best[-1]["end"]) and (discharge_limits_best[window_n] == new_enable[-1]):
                    new_best[-1]["end"] = discharge_window_best[window_n]["end"]
                    if self.debug_enable:
                        self.log("Combine discharge slot {} with previous - percent {} slot {}".format(window_n, new_enable[-1], new_best[-1]))
                else:
                    new_best.append(copy.deepcopy(discharge_window_best[window_n]))
                    new_enable.append(discharge_limits_best[window_n])

        return new_enable, new_best

    def tweak_plan(self, end_record, load_minutes_step, load_minutes_step10, pv_forecast_minute_step, pv_forecast_minute10_step, best_metric, metric_keep):
        """
        Tweak existing plan only
        """
        record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
        record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)
        self.log("Tweak optimisation started")
        best_soc = self.soc_max
        best_cost = best_metric
        best_keep = metric_keep
        count = 0
        window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows])
        for key in window_sorted:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            if typ == "c":
                if self.calculate_best_charge:
                    best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_charge_limit(
                        window_n,
                        record_charge_windows,
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.discharge_window_best,
                        self.discharge_limits_best,
                        load_minutes_step,
                        load_minutes_step10,
                        pv_forecast_minute_step,
                        pv_forecast_minute10_step,
                        end_record=end_record,
                    )
                    self.charge_limit_best[window_n] = best_soc
            else:
                if self.calculate_best_discharge:
                    if not self.calculate_discharge_oncharge:
                        hit_charge = self.hit_charge_window(self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"])
                        if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                            continue
                    average = self.discharge_window_best[window_n]["average"]
                    best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_discharge(
                        window_n,
                        record_discharge_windows,
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.discharge_window_best,
                        self.discharge_limits_best,
                        load_minutes_step,
                        load_minutes_step10,
                        pv_forecast_minute_step,
                        pv_forecast_minute10_step,
                        end_record=end_record,
                    )
                    self.discharge_limits_best[window_n] = best_soc
                    self.discharge_window_best[window_n]["start"] = best_start
            count += 1
            if count >= 8:
                break

        self.log("Tweak optimisation finished metric {} cost {} metric_keep {}".format(self.dp2(best_metric), self.dp2(best_cost), self.dp2(best_keep)))

    def optimise_all_windows(self, load_minutes_step, load_minutes_step10, pv_forecast_minute_step, pv_forecast_minute10_step, best_metric, metric_keep):
        """
        Optimise all windows, both charge and discharge in rate order
        """
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_discharge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.discharge_window_best), 1)
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(
            self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows]
        )
        best_soc = self.soc_max
        best_cost = best_metric
        best_keep = metric_keep
        best_price = 0
        best_price_discharge = 0

        # Optimise all windows by picking a price threshold default
        if price_set and self.calculate_best_charge and self.charge_window_best:
            self.log("Optimise all windows, total charge {} discharge {}".format(record_charge_windows, record_discharge_windows))
            self.optimise_charge_windows_reset(reset_all=True)
            self.charge_limit_best, ignore_discharge_limits, best_price, best_price_discharge = self.optimise_charge_limit_price(
                price_set,
                price_links,
                window_index,
                record_charge_windows,
                self.charge_limit_best,
                self.charge_window_best,
                self.discharge_window_best,
                self.discharge_limits_best,
                load_minutes_step,
                load_minutes_step10,
                pv_forecast_minute_step,
                pv_forecast_minute10_step,
                end_record=self.end_record,
            )

        # Set the new end record and blackout period based on the levelling
        self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price)
        self.optimise_charge_windows_reset(reset_all=False)
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_discharge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.discharge_window_best), 1)
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(
            self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows], secondary_order=True
        )

        self.rate_best_cost_threshold_charge = best_price
        self.rate_best_cost_threshold_discharge = best_price_discharge

        # Work out the lowest rate we charge at from the first pass
        lowest_price_charge = best_price
        for price in price_set:
            links = price_links[price]
            for key in links:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                if typ == "c":
                    if self.charge_limit_best[window_n] > self.reserve:
                        if price < lowest_price_charge:
                            lowest_price_charge = price

        # Optimise individual windows in the price band for charge/discharge
        # First optimise those at or below threshold highest to lowest (to turn down values)
        # then optimise those above the threshold lowest to highest (to turn up values)
        # Do the opposite for discharge.
        self.log(
            "Starting second optimisation best_price {} best_price_discharge {} lowest_price_charge {} with charge limits {} based on".format(
                best_price, best_price_discharge, lowest_price_charge, self.charge_limit_best
            )
        )
        for start_at_low in [False, True]:
            if start_at_low:
                price_set.reverse()

            for price in price_set:
                links = price_links[price]
                printed_set = False

                for key in links:
                    typ = window_index[key]["type"]
                    window_n = window_index[key]["id"]

                    if typ == "c":
                        # Store price set with window
                        self.charge_window_best[window_n]["set"] = price

                        # For start at high only tune down excess high slots
                        if (not start_at_low) and (price > best_price) and (self.charge_limit_best[window_n] != self.soc_max):
                            if self.debug_enable:
                                self.log("Skip start at high window {} best limit {}".format(window_n, (self.charge_limit_best[window_n])))
                            continue

                        if self.calculate_best_charge:
                            if not printed_set:
                                self.log("Optimise price set {} start_at_low {} best_price {}".format(price, start_at_low, best_price))
                                printed_set = True
                            average = self.charge_window_best[window_n]["average"]

                            best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_charge_limit(
                                window_n,
                                record_charge_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.discharge_window_best,
                                self.discharge_limits_best,
                                load_minutes_step,
                                load_minutes_step10,
                                pv_forecast_minute_step,
                                pv_forecast_minute10_step,
                                end_record=self.end_record,
                            )
                            self.charge_limit_best[window_n] = best_soc

                            if 0:
                                # Find all adjacent windows in price range
                                all_n = [window_n]
                                end_n = self.charge_window_best[window_n]["end"]
                                loop_n = window_n + 1
                                while (loop_n < len(self.charge_limit_best)) and (len(all_n) < 4):
                                    if end_n == self.charge_window_best[loop_n]["start"] and (abs(self.charge_window_best[loop_n]["average"] - average) <= 1.0):
                                        all_n.append(loop_n)
                                        end_n = self.charge_window_best[loop_n]["end"]
                                    else:
                                        break
                                    loop_n += 1

                                if len(all_n) > 1:
                                    self.log("Optimise charge group {}".format(all_n))
                                    prev_best_metric = best_metric
                                    best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_charge_limit(
                                        window_n,
                                        record_charge_windows,
                                        self.charge_limit_best,
                                        self.charge_window_best,
                                        self.discharge_window_best,
                                        self.discharge_limits_best,
                                        load_minutes_step,
                                        load_minutes_step10,
                                        pv_forecast_minute_step,
                                        pv_forecast_minute10_step,
                                        end_record=self.end_record,
                                        all_n=all_n,
                                    )
                                    self.log("Optimised charge group metric {} previous {} soc {}".format(best_metric, prev_best_metric, best_soc))
                                    if best_metric < prev_best_metric:
                                        for loop_n in all_n:
                                            self.charge_limit_best[loop_n] = best_soc

                            if self.debug_enable:
                                self.log(
                                    "Best charge limit window {} time {} - {} cost {} charge_limit {} (adjusted) min {} @ {} (margin added {} and min {} max {}) with metric {} cost {} windows {}".format(
                                        window_n,
                                        self.time_abs_str(self.charge_window_best[window_n]["start"]),
                                        self.time_abs_str(self.charge_window_best[window_n]["end"]),
                                        average,
                                        self.dp2(best_soc),
                                        self.dp2(soc_min),
                                        self.time_abs_str(soc_min_minute),
                                        self.best_soc_margin,
                                        self.best_soc_min,
                                        self.best_soc_max,
                                        self.dp2(best_metric),
                                        self.dp2(best_cost),
                                        self.charge_limit_best,
                                    )
                                )
                    else:
                        # Store price set with window
                        self.discharge_window_best[window_n]["set"] = price

                        # Do highest price first
                        # Second pass to tune down any excess exports only
                        if start_at_low and ((price > best_price) or (self.discharge_limits_best[window_n] == 100.0)):
                            continue

                        if self.calculate_best_discharge:
                            if not self.calculate_discharge_oncharge:
                                hit_charge = self.hit_charge_window(
                                    self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                                )
                                if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                                    continue

                            average = self.discharge_window_best[window_n]["average"]
                            if price < lowest_price_charge:
                                if self.debug_enable and 0:
                                    self.log(
                                        "Skipping discharge optimisation on rate {} as it is unlikely to be profitable (threshold {} real rate {})".format(
                                            price, best_price, self.dp2(average)
                                        )
                                    )
                                continue

                            if not printed_set:
                                self.log("Optimise price set {} start_at_low {} best_price {}".format(price, start_at_low, best_price))
                                printed_set = True

                            best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_discharge(
                                window_n,
                                record_discharge_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.discharge_window_best,
                                self.discharge_limits_best,
                                load_minutes_step,
                                load_minutes_step10,
                                pv_forecast_minute_step,
                                pv_forecast_minute10_step,
                                end_record=self.end_record,
                            )
                            self.discharge_limits_best[window_n] = best_soc
                            self.discharge_window_best[window_n]["start"] = best_start

                            if self.debug_enable:
                                self.log(
                                    "Best discharge limit window {} time {} - {} cost {} discharge_limit {} (adjusted) min {} @ {} (margin added {} and min {}) with metric {} cost {}".format(
                                        window_n,
                                        self.time_abs_str(self.discharge_window_best[window_n]["start"]),
                                        self.time_abs_str(self.discharge_window_best[window_n]["end"]),
                                        average,
                                        best_soc,
                                        self.dp2(soc_min),
                                        self.time_abs_str(soc_min_minute),
                                        self.best_soc_margin,
                                        self.best_soc_min,
                                        self.dp2(best_metric),
                                        self.dp2(best_cost),
                                    )
                                )

            # Log set of charge and discharge windows
            if self.calculate_best_charge:
                self.log(
                    "Best charge windows in price group {} best_metric {} best_cost {} metric_keep {} windows {}".format(
                        price,
                        self.dp2(best_metric),
                        self.dp2(best_cost),
                        self.dp2(best_keep),
                        self.window_as_text(self.charge_window_best, self.charge_limit_best, ignore_min=True),
                    )
                )

            if self.calculate_best_discharge:
                self.log(
                    "Best discharge windows in price group {} best_metric {} best_cost {} metric_keep {} windows {}".format(
                        price,
                        self.dp2(best_metric),
                        self.dp2(best_cost),
                        self.dp2(best_keep),
                        self.window_as_text(self.discharge_window_best, self.discharge_limits_best, ignore_max=True),
                    )
                )

        if self.calculate_second_pass:
            self.log("Second pass optimisation started")
            count = 0
            window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows])
            for key in window_sorted:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                if typ == "c":
                    if self.calculate_best_charge:
                        best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_charge_limit(
                            window_n,
                            record_charge_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.discharge_window_best,
                            self.discharge_limits_best,
                            load_minutes_step,
                            load_minutes_step10,
                            pv_forecast_minute_step,
                            pv_forecast_minute10_step,
                            end_record=self.end_record,
                        )
                        self.charge_limit_best[window_n] = best_soc
                else:
                    if self.calculate_best_discharge:
                        if not self.calculate_discharge_oncharge:
                            hit_charge = self.hit_charge_window(self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"])
                            if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                                continue
                        average = self.discharge_window_best[window_n]["average"]
                        best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep = self.optimise_discharge(
                            window_n,
                            record_discharge_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.discharge_window_best,
                            self.discharge_limits_best,
                            load_minutes_step,
                            load_minutes_step10,
                            pv_forecast_minute_step,
                            pv_forecast_minute10_step,
                            end_record=self.end_record,
                        )
                        self.discharge_limits_best[window_n] = best_soc
                        self.discharge_window_best[window_n]["start"] = best_start
                if (count % 16) == 0:
                    self.log("Final optimisation type {} window {} metric {} metric_keep {} cost {}".format(typ, window_n, best_metric, self.dp2(best_keep), self.dp2(best_cost)))
                count += 1
            self.log("Second pass optimisation finished metric {} cost {} metric_keep {}".format(best_metric, self.dp2(best_cost), self.dp2(best_keep)))

    def optimise_charge_windows_reset(self, reset_all):
        """
        Reset the charge windows to min
        """
        if self.charge_window_best and self.calculate_best_charge:
            # Set all to max
            for window_n in range(0, len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] < self.end_record:
                    if reset_all:
                        self.charge_limit_best[window_n] = 0.0
                else:
                    self.charge_limit_best[window_n] = self.soc_max

    def window_as_text(self, windows, percents, ignore_min=False, ignore_max=False):
        """
        Convert window in minutes to text string
        """
        txt = "[ "
        first_window = True
        for window_n in range(0, len(windows)):
            window = windows[window_n]
            percent = percents[window_n]
            average = window["average"]

            if ignore_min and percent == 0.0:
                continue
            if ignore_max and percent == 100.0:
                continue

            if not first_window:
                txt += ", "
            first_window = False
            start_timestamp = self.midnight_utc + timedelta(minutes=window["start"])
            start_time = start_timestamp.strftime("%d-%m %H:%M:%S")
            end_timestamp = self.midnight_utc + timedelta(minutes=window["end"])
            end_time = end_timestamp.strftime("%d-%m %H:%M:%S")
            txt += start_time + " - "
            txt += end_time
            txt += " @ {}p {}%".format(self.dp2(average), self.dp2(percent))
        txt += " ]"
        return txt

    def get_car_charging_planned(self):
        """
        Get the car attributes
        """
        self.car_charging_planned = [False for c in range(0, self.num_cars)]
        self.car_charging_now = [False for c in range(0, self.num_cars)]
        self.car_charging_plan_smart = [False for c in range(0, self.num_cars)]
        self.car_charging_plan_time = [False for c in range(0, self.num_cars)]
        self.car_charging_battery_size = [100.0 for c in range(0, self.num_cars)]
        self.car_charging_limit = [100.0 for c in range(0, self.num_cars)]
        self.car_charging_rate = [7.4 for c in range(0, max(self.num_cars, 1))]
        self.car_charging_slots = [[] for c in range(0, self.num_cars)]

        self.car_charging_planned_response = self.get_arg("car_charging_planned_response", ["yes", "on", "enable", "true"])
        self.car_charging_now_response = self.get_arg("car_charging_now_response", ["yes", "on", "enable", "true"])

        # Car charging planned sensor
        for car_n in range(0, self.num_cars):
            # Get car N planned status
            planned = self.get_arg("car_charging_planned", "no", index=car_n)
            if isinstance(planned, str):
                if planned.lower() in self.car_charging_planned_response:
                    planned = True
                else:
                    planned = False
            elif not isinstance(planned, bool):
                planned = False
            self.car_charging_planned[car_n] = planned

            # Car is charging now sensor
            charging_now = self.get_arg("car_charging_now", "no", index=car_n)
            if isinstance(charging_now, str):
                if charging_now.lower() in self.car_charging_now_response:
                    charging_now = True
                else:
                    charging_now = False
            elif not isinstance(charging_now, bool):
                charging_now = False
            self.car_charging_now[car_n] = charging_now

            # Other car related configuration
            self.car_charging_plan_smart[car_n] = self.get_arg("car_charging_plan_smart", False)
            self.car_charging_plan_time[car_n] = self.get_arg("car_charging_plan_time", "07:00:00")
            self.car_charging_battery_size[car_n] = float(self.get_arg("car_charging_battery_size", 100.0, index=car_n))
            self.car_charging_rate[car_n] = float(self.get_arg("car_charging_rate"))
            self.car_charging_limit[car_n] = (float(self.get_arg("car_charging_limit", 100.0, index=car_n)) * self.car_charging_battery_size[car_n]) / 100.0

        self.car_charging_from_battery = self.get_arg("car_charging_from_battery")
        if self.num_cars > 0:
            self.log(
                "Cars {} charging from battery {} planned {}, charging_now {} smart {}, plan_time {}, battery size {}, limit {}, rate {}".format(
                    self.num_cars,
                    self.car_charging_from_battery,
                    self.car_charging_planned,
                    self.car_charging_now,
                    self.car_charging_plan_smart,
                    self.car_charging_plan_time,
                    self.car_charging_battery_size,
                    self.car_charging_limit,
                    self.car_charging_rate,
                )
            )

    def fetch_pv_datapoints(self, argname):
        """
        Get some solcast data from argname argument
        """
        data = []
        total_data = 0
        total_sensor = 0

        if argname in self.args:
            # Found out if detailedForecast is present or not, then set the attribute name
            # in newer solcast plugins only forecast is used
            attribute = "detailedForecast"
            entity_id = self.get_arg(argname, None, indirect=False)
            if entity_id:
                result = self.get_state(entity_id=entity_id, attribute=attribute)
                if not result:
                    attribute = "forecast"
                try:
                    data = self.get_state(entity_id=self.get_arg(argname, indirect=False), attribute=attribute)
                except (ValueError, TypeError):
                    self.log("WARN: Unable to fetch solar forecast data from sensor {} check your setting of {}".format(self.get_arg(argname, indirect=False), argname))
                    self.record_status("Error - {} not be set correctly, check apps.yaml", debug=self.get_arg(argname, indirect=False), had_errors=True)

            # Solcast new vs old version
            # check the total vs the sum of 30 minute slots and work out scale factor
            expected = 0.0
            factor = 1.0
            if data:
                for entry in data:
                    total_data += entry["pv_estimate"]
                total_data = self.dp2(total_data)
                total_sensor = self.dp2(self.get_arg(argname, 1.0))
        return data, total_data, total_sensor

    def fetch_extra_load_forecast(self, now_utc):
        """
        Fetch extra load forecast
        """
        load_forecast = {}
        if "load_forecast" in self.args:
            entity_ids = self.get_arg("load_forecast", indirect=False)
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]

            for entity_id in entity_ids:
                attribute = None
                if "$" in entity_id:
                    entity_id, attribute = entity_id.split("$")
                try:
                    data = self.get_state(entity_id=entity_id, attribute=attribute)
                except (ValueError, TypeError):
                    data = None

                load_forecast = self.minute_data(
                    data, self.forecast_days, self.midnight_utc, "energy", "last_updated", backwards=False, clean_increment=False, smoothing=True, divide_by=1.0, scale=1.0
                )

        return load_forecast

    def fetch_pv_forecast(self):
        """
        Fetch the PV Forecast data from Solcast
        """
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        pv_forecast_data = []
        pv_forecast_total_data = 0
        pv_forecast_total_sensor = 0

        # Fetch data from each sensor
        for argname in ["pv_forecast_today", "pv_forecast_tomorrow", "pv_forecast_d3", "pv_forecast_d4"]:
            data, total_data, total_sensor = self.fetch_pv_datapoints(argname)
            if data:
                self.log("PV Data for {} total {} kWh".format(argname, total_sensor))
                pv_forecast_data += data
                pv_forecast_total_data += total_data
                pv_forecast_total_sensor += total_sensor

        # Work out data scale factor so it adds up (New Solcast is in kW but old was kWH)
        factor = 1.0
        if pv_forecast_total_data > 0.0 and pv_forecast_total_sensor > 0.0:
            factor = round((pv_forecast_total_data / pv_forecast_total_sensor), 1)
        # We want to divide the data into single minute slots
        divide_by = self.dp2(30 * factor)

        if factor != 1.0 and factor != 2.0:
            self.log(
                "WARN: PV Forecast data adds up to {} kWh but total sensors add up to {} kWh, this is unexpected and hence data maybe misleading (factor {})".format(
                    pv_forecast_total_data, pv_forecast_total_sensor, factor
                )
            )

        if pv_forecast_data:
            pv_forecast_minute = self.minute_data(
                pv_forecast_data,
                self.forecast_days + 1,
                self.midnight_utc,
                "pv_estimate" + str(self.get_arg("pv_estimate", "")),
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=30,
            )
            pv_forecast_minute10 = self.minute_data(
                pv_forecast_data,
                self.forecast_days + 1,
                self.midnight_utc,
                "pv_estimate10",
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=30,
            )
        else:
            self.log("WARN: No solar data has been configured.")

        return pv_forecast_minute, pv_forecast_minute10

    def balance_inverters(self):
        """
        Attempt to balance multiple inverters
        """
        # Charge rate resets
        balance_reset_charge = {}
        balance_reset_discharge = {}

        self.log(
            "BALANCE: Enabled balance charge {} discharge {} crosscharge {} threshold charge {} discharge {}".format(
                self.balance_inverters_charge,
                self.balance_inverters_discharge,
                self.balance_inverters_crosscharge,
                self.balance_inverters_threshold_charge,
                self.balance_inverters_threshold_discharge,
            )
        )

        # For each inverter get the details
        skew = self.get_arg("clock_skew", 0)
        local_tz = pytz.timezone(self.get_arg("timezone", "Europe/London"))
        now_utc = datetime.now(local_tz) + timedelta(minutes=skew)
        now = datetime.now() + timedelta(minutes=skew)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_now = int((now - midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        num_inverters = int(self.get_arg("num_inverters", 1))
        self.now_utc = now_utc
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = int((now - self.midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        self.minutes_to_midnight = 24 * 60 - self.minutes_now

        inverters = []
        for id in range(0, num_inverters):
            inverter = Inverter(self, id, quiet=True)
            inverter.update_status(minutes_now, quiet=True)
            inverters.append(inverter)

        out_of_balance = False  # Are all the SOC % the same
        total_battery_power = 0  # Total battery power across inverters
        total_max_rate = 0  # Total battery max rate across inverters
        total_charge_rates = 0  # Current total charge rates
        total_discharge_rates = 0  # Current total discharge rates
        total_pv_power = 0  # Current total PV power
        total_load_power = 0  # Current load power
        socs = []
        reserves = []
        battery_powers = []
        pv_powers = []
        battery_max_rates = []
        charge_rates = []
        discharge_rates = []
        load_powers = []
        for inverter in inverters:
            socs.append(inverter.soc_percent)
            reserves.append(inverter.reserve_current)
            if inverter.soc_percent != inverters[0].soc_percent:
                out_of_balance = True
            battery_powers.append(inverter.battery_power)
            pv_powers.append(inverter.pv_power)
            load_powers.append(inverter.load_power)
            total_battery_power += inverter.battery_power
            total_pv_power += inverter.pv_power
            total_load_power += inverter.load_power
            battery_max_rates.append(inverter.battery_rate_max_discharge * 60 * 1000.0)
            total_max_rate += inverter.battery_rate_max_discharge * 60 * 1000.0
            charge_rates.append(inverter.charge_rate_now * 60 * 1000.0)
            total_charge_rates += inverter.charge_rate_now * 60 * 1000.0
            discharge_rates.append(inverter.discharge_rate_now * 60 * 1000.0)
            total_discharge_rates += inverter.discharge_rate_now * 60 * 1000.0
        self.log(
            "BALANCE: socs {} reserves {} battery_powers {} total {} battery_max_rates {} charge_rates {} pv_power {} load_power {} total {} discharge_rates {} total {}".format(
                socs,
                reserves,
                battery_powers,
                total_battery_power,
                battery_max_rates,
                charge_rates,
                pv_powers,
                load_powers,
                total_charge_rates,
                discharge_rates,
                total_discharge_rates,
            )
        )

        # Are we discharging
        during_discharge = total_battery_power >= 0.0
        during_charge = total_battery_power < 0.0

        # Work out min and max socs
        soc_min = min(socs)
        soc_max = max(socs)

        # Work out which inverters have low and high Soc
        soc_low = []
        soc_high = []
        for inverter in inverters:
            soc_low.append(inverter.soc_percent < soc_max and (abs(inverter.soc_percent - soc_max) >= self.balance_inverters_discharge))
            soc_high.append(inverter.soc_percent > soc_min and (abs(inverter.soc_percent - soc_min) >= self.balance_inverters_charge))

        above_reserve = []  # Is the battery above reserve?
        below_full = []  # Is the battery below full?
        can_power_house = []  # Could this inverter power the house alone?
        can_store_pv = []  # Can store the PV for the house alone?
        power_enough_discharge = []  # Inverter drawing enough power to be worth balancing
        power_enough_charge = []  # Inverter drawing enough power to be worth balancing
        for id in range(0, num_inverters):
            above_reserve.append((socs[id] - reserves[id]) >= 4.0)
            below_full.append(socs[id] < 100.0)
            can_power_house.append((total_discharge_rates - discharge_rates[id] - 200) >= total_battery_power)
            can_store_pv.append(total_pv_power <= (total_charge_rates - charge_rates[id]))
            power_enough_discharge.append(battery_powers[id] >= 50.0)
            power_enough_charge.append(inverters[id].battery_power <= -50.0)

        self.log(
            "BALANCE: out_of_balance {} above_reserve {} below_full {} can_power_house {} can_store_pv {} power_enough_discharge {} power_enough_charge {} soc_low {} soc_high {}".format(
                out_of_balance, above_reserve, below_full, can_power_house, can_store_pv, power_enough_discharge, power_enough_charge, soc_low, soc_high
            )
        )
        for this_inverter in range(0, num_inverters):
            other_inverter = (this_inverter + 1) % num_inverters
            if (
                self.balance_inverters_discharge
                and total_discharge_rates > 0
                and out_of_balance
                and during_discharge
                and soc_low[this_inverter]
                and above_reserve[other_inverter]
                and can_power_house[this_inverter]
                and (power_enough_discharge[this_inverter] or discharge_rates[this_inverter] == 0)
            ):
                self.log("BALANCE: Inverter {} is out of balance low - during discharge, attempting to balance it using inverter {}".format(this_inverter, other_inverter))
                balance_reset_discharge[this_inverter] = True
                inverters[this_inverter].adjust_discharge_rate(0, notify=False)
            elif (
                self.balance_inverters_charge
                and total_charge_rates > 0
                and out_of_balance
                and during_charge
                and soc_high[this_inverter]
                and below_full[other_inverter]
                and can_store_pv[this_inverter]
                and (power_enough_charge[this_inverter] or charge_rates[this_inverter] == 0)
            ):
                self.log("BALANCE: Inverter {} is out of balance high - during charge, attempting to balance it".format(this_inverter))
                balance_reset_charge[this_inverter] = True
                inverters[this_inverter].adjust_charge_rate(0, notify=False)
            elif self.balance_inverters_crosscharge and during_discharge and total_discharge_rates > 0 and power_enough_charge[this_inverter]:
                self.log("BALANCE: Inverter {} is cross charging during discharge, attempting to balance it".format(this_inverter))
                if soc_low[this_inverter] and can_power_house[other_inverter]:
                    balance_reset_discharge[this_inverter] = True
                    inverters[this_inverter].adjust_discharge_rate(0, notify=False)
                else:
                    balance_reset_charge[this_inverter] = True
                    inverters[this_inverter].adjust_charge_rate(0, notify=False)
            elif self.balance_inverters_crosscharge and during_charge and total_charge_rates > 0 and power_enough_discharge[this_inverter]:
                self.log("BALANCE: Inverter {} is cross discharging during charge, attempting to balance it".format(this_inverter))
                balance_reset_discharge[this_inverter] = True
                inverters[this_inverter].adjust_discharge_rate(0, notify=False)

        for id in range(0, num_inverters):
            if not balance_reset_charge.get(id, False) and total_charge_rates != 0 and charge_rates[id] == 0:
                self.log("BALANCE: Inverter {} reset charge rate to {} now balanced".format(id, inverter.battery_rate_max_charge * 60 * 1000))
                inverters[id].adjust_charge_rate(inverter.battery_rate_max_charge * 60 * 1000, notify=False)
            if not balance_reset_discharge.get(id, False) and total_discharge_rates != 0 and discharge_rates[id] == 0:
                self.log("BALANCE: Inverter {} reset discharge rate to {} now balanced".format(id, inverter.battery_rate_max_discharge * 60 * 1000))
                inverters[id].adjust_discharge_rate(inverter.battery_rate_max_discharge * 60 * 1000, notify=False)

        self.log("BALANCE: Completed this run")

    def find_charge_rate(self, minutes_now, soc, window, target_soc, max_rate):
        """
        Find the lowest charge rate that fits the charge slow
        """
        margin = 20
        if self.set_charge_low_power:
            minutes_left = window["end"] - minutes_now - margin

            # If we don't have enough minutes left go to max
            if minutes_left < 0:
                return max_rate

            # If we already have reached target go back to max
            if soc >= target_soc:
                return max_rate

            # Work out the charge left in kw
            charge_left = target_soc - soc

            # If we can never hit the target then go to max
            if max_rate * minutes_left < charge_left:
                return max_rate

            # What's the lowest we could go?
            min_rate = charge_left / minutes_left

            # Apply the curve at each rate to pick one that works
            rate_w = max_rate * 60.0 * 1000.0
            best_rate = max_rate
            while rate_w >= 400:
                rate = rate_w / 60.0 / 1000.0
                if rate >= min_rate:
                    charge_now = soc
                    minute = 0
                    for minute in range(0, minutes_left, PREDICT_STEP):
                        charge_now_percent = self.calc_percent_limit(charge_now)
                        rate_scale = self.battery_charge_power_curve.get(charge_now_percent, 1.0) * self.battery_rate_max_scaling
                        charge_amount = rate * rate_scale * PREDICT_STEP * self.battery_loss
                        charge_now += charge_amount
                        if charge_now >= target_soc:
                            best_rate = rate
                            break
                rate_w -= 200.0
            # self.log("Find charge rate now {} soc {} window {} target_soc {} max_rate {} min_rate {} returns {}".format(minutes_now, soc, window, target_soc, int(max_rate * 60.0 * 1000.0), int(min_rate * 60.0 * 1000.0), int(best_rate * 60.0 * 1000.0)))
            return best_rate
        else:
            return max_rate

    def log_option_best(self):
        """
        Log options
        """
        opts = ""
        opts += "mode({}) ".format(self.predbat_mode)
        opts += "calculate_discharge_oncharge({}) ".format(self.calculate_discharge_oncharge)
        opts += "set_discharge_freeze_only({}) ".format(self.set_discharge_freeze_only)
        opts += "set_discharge_during_charge({}) ".format(self.set_discharge_during_charge)
        opts += "combine_charge_slots({}) ".format(self.combine_charge_slots)
        opts += "combine_discharge_slots({}) ".format(self.combine_discharge_slots)
        opts += "best_soc_min({} kWh) ".format(self.best_soc_min)
        opts += "best_soc_max({} kWh) ".format(self.best_soc_max)
        opts += "best_soc_keep({} kWh) ".format(self.best_soc_keep)
        opts += "inverter_loss({} %) ".format(int((1 - self.inverter_loss) * 100.0))
        opts += "battery_loss({} %) ".format(int((1 - self.battery_loss) * 100.0))
        opts += "battery_loss_discharge ({} %) ".format(int((1 - self.battery_loss_discharge) * 100.0))
        opts += "inverter_hybrid({}) ".format(self.inverter_hybrid)
        opts += "metric_min_improvement({} p) ".format(self.metric_min_improvement)
        opts += "metric_min_improvement_discharge({} p) ".format(self.metric_min_improvement_discharge)
        opts += "metric_battery_cycle({} p/kWh)".format(self.metric_battery_cycle)
        opts += "metric_battery_value_scaling({} x)".format(self.metric_battery_value_scaling)
        self.log("Calculate Best options: " + opts)

    def calculate_plan(self, recompute=True):
        """
        Calculate the new plan (best)

        sets:
           self.charge_window_best
           self.charge_limits_best
           self.charge_limit_percent_best
           self.discharge_window_best
           self.discharge_limits_best
        """

        # Re-compute plan due to time wrap
        if self.plan_last_updated_minutes > self.minutes_now:
            self.log("Force recompute due to start of day")
            recompute = True

        # Shift onto next charge window if required
        while self.charge_window_best and not recompute:
            window = self.charge_window_best[0]
            if window["end"] <= self.minutes_now:
                del self.charge_window_best[0]
                del self.charge_limit_best[0]
                del self.charge_limit_percent_best[0]
                self.log("Current charge window has expired, removing it")
            else:
                break

        # Shift onto next discharge window if required
        while self.discharge_window_best and not recompute:
            window = self.discharge_window_best[0]
            if window["end"] <= self.minutes_now:
                del self.discharge_window_best[0]
                del self.discharge_limits_best[0]
                self.log("Current discharge window has expired, removing it")
            else:
                break

        # Recompute?
        if recompute:
            self.plan_valid = False  # In case of crash, plan is now invalid

            # Calculate best charge windows
            if self.low_rates:
                # If we are using calculated windows directly then save them
                self.charge_window_best = copy.deepcopy(self.low_rates)
            else:
                # Default best charge window as this one
                self.charge_window_best = self.charge_window

            if self.set_soc_enable and not self.set_charge_window:
                # If we can't control the charge window, but we can control the SOC then don't calculate a new window or the calculated SOC will be wrong
                self.log("Note: Set SOC is enabled, but set charge window is disabled, so using the existing charge window only")
                self.charge_window_best = self.charge_window

            # Calculate best discharge windows
            if self.high_export_rates:
                self.discharge_window_best = copy.deepcopy(self.high_export_rates)
            else:
                self.discharge_window_best = []

            # Pre-fill best charge limit with the current charge limit
            self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window_best))]
            self.charge_limit_percent_best = [self.current_charge_limit for i in range(0, len(self.charge_window_best))]

            # Pre-fill best discharge enable with Off
            self.discharge_limits_best = [100.0 for i in range(0, len(self.discharge_window_best))]

            self.end_record = self.forecast_minutes
        # Show best windows
        self.log("Best charge    window {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_best)))
        self.log("Best discharge window {}".format(self.window_as_text(self.discharge_window_best, self.discharge_limits_best)))

        # Created optimised step data
        self.metric_cloud_coverage = self.get_cloud_factor(self.minutes_now, self.pv_forecast_minute, self.pv_forecast_minute10)
        load_minutes_step = self.step_data_history(
            self.load_minutes, self.minutes_now, forward=False, scale_today=self.load_inday_adjustment, type_load=True, load_forecast=self.load_forecast
        )
        load_minutes_step10 = self.step_data_history(
            self.load_minutes, self.minutes_now, forward=False, scale_today=self.load_inday_adjustment * self.load_scaling10, type_load=True, load_forecast=self.load_forecast
        )
        pv_forecast_minute_step = self.step_data_history(self.pv_forecast_minute, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)
        pv_forecast_minute10_step = self.step_data_history(self.pv_forecast_minute10, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)

        # Simulate current settings to get initial data
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
            self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, load_minutes_step, pv_forecast_minute_step, end_record=self.end_record
        )

        # Try different battery SOCs to get the best result
        if recompute:
            self.rate_best_cost_threshold_charge = None
            self.rate_best_cost_threshold_discharge = None

        if self.calculate_best and recompute:
            # Recomputing the plan
            self.log_option_best()

            # Full plan
            self.optimise_all_windows(load_minutes_step, load_minutes_step10, pv_forecast_minute_step, pv_forecast_minute10_step, metric, metric_keep)

            # Tweak plan
            if self.calculate_tweak_plan:
                self.tweak_plan(self.end_record, load_minutes_step, load_minutes_step10, pv_forecast_minute_step, pv_forecast_minute10_step, metric, metric_keep)

            # Remove charge windows that overlap with discharge windows
            self.charge_limit_best, self.charge_window_best = self.remove_intersecting_windows(
                self.charge_limit_best, self.charge_window_best, self.discharge_limits_best, self.discharge_window_best
            )

            # Filter out any unused discharge windows
            if self.calculate_best_discharge and self.discharge_window_best:
                # Filter out the windows we disabled
                self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)

                # Clipping windows
                if self.discharge_window_best:
                    # Re-run prediction to get data for clipping
                    best_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.discharge_window_best,
                        self.discharge_limits_best,
                        load_minutes_step,
                        pv_forecast_minute_step,
                        end_record=self.end_record,
                    )

                    # Work out record windows
                    record_discharge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.discharge_window_best), 1)

                    # Discharge slot clipping
                    self.discharge_window_best, self.discharge_limits_best = self.clip_discharge_slots(
                        self.minutes_now, self.predict_soc, self.discharge_window_best, self.discharge_limits_best, record_discharge_windows, PREDICT_STEP
                    )

                    # Filter out the windows we disabled during clipping
                    self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)
                self.log("Discharge windows filtered {}".format(self.window_as_text(self.discharge_window_best, self.discharge_limits_best)))

            # Filter out any unused charge slots
            if self.calculate_best_charge and self.charge_window_best:
                # Re-run prediction to get data for clipping
                best_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.discharge_window_best,
                    self.discharge_limits_best,
                    load_minutes_step,
                    pv_forecast_minute_step,
                    end_record=self.end_record,
                    save="debug",
                )
                # Initial charge slot filter
                if self.set_charge_window:
                    record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)

                # Charge slot clipping
                record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
                self.charge_window_best, self.charge_limit_best = self.clip_charge_slots(
                    self.minutes_now, self.predict_soc, self.charge_window_best, self.charge_limit_best, record_charge_windows, PREDICT_STEP
                )
                if self.set_charge_window:
                    # Filter out the windows we disabled during clipping
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)
                    self.charge_limit_percent_best = self.calc_percent_limit(self.charge_limit_best)
                    self.log("Filtered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_percent_best), self.reserve))
                else:
                    self.charge_limit_percent_best = self.calc_percent_limit(self.charge_limit_best)
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_percent_best), self.reserve))

            # Plan is now valid
            self.plan_valid = True
            self.plan_last_updated = self.now_utc
            self.plan_last_updated_minutes = self.minutes_now

        # Final simulation of base
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
            self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, load_minutes_step, pv_forecast_minute_step, save="base", end_record=self.end_record
        )
        (
            metricb10,
            import_kwh_batteryb10,
            import_kwh_houseb10,
            export_kwhb10,
            soc_minb10,
            socb10,
            soc_min_minuteb10,
            battery_cycle10,
            metric_keep10,
            final_iboost10,
        ) = self.run_prediction(
            self.charge_limit,
            self.charge_window,
            self.discharge_window,
            self.discharge_limits,
            load_minutes_step10,
            pv_forecast_minute10_step,
            save="base10",
            end_record=self.end_record,
        )

        if self.calculate_best:
            # Final simulation of best, do 10% and normal scenario
            (
                best_metric10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.discharge_window_best,
                self.discharge_limits_best,
                load_minutes_step10,
                pv_forecast_minute10_step,
                save="best10",
                end_record=self.end_record,
            )
            best_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.discharge_window_best,
                self.discharge_limits_best,
                load_minutes_step,
                pv_forecast_minute_step,
                save="best",
                end_record=self.end_record,
            )
            # round charge_limit_best (kWh) to 2 decimal places and discharge_limits_best (percentage) to nearest whole number
            self.charge_limit_best = [self.dp2(elem) for elem in self.charge_limit_best]
            self.discharge_limits_best = [self.dp0(elem) for elem in self.discharge_limits_best]

            self.log(
                "Best charging limit socs {} export {} gives import battery {} house {} export {} metric {} metric10 {}".format(
                    self.charge_limit_best,
                    self.discharge_limits_best,
                    self.dp2(import_kwh_battery),
                    self.dp2(import_kwh_house),
                    self.dp2(export_kwh),
                    self.dp2(best_metric),
                    self.dp2(best_metric10),
                )
            )

            # Publish charge and discharge window best
            self.charge_limit_percent_best = self.calc_percent_limit(self.charge_limit_best)
            self.publish_charge_limit(self.charge_limit_best, self.charge_window_best, self.charge_limit_percent_best, best=True, soc=self.predict_soc_best)
            self.publish_discharge_limit(self.discharge_window_best, self.discharge_limits_best, best=True)

            # HTML data
            self.publish_html_plan(pv_forecast_minute_step, load_minutes_step, self.end_record)

    def reset_inverter(self):
        """
        Reset inverter to safe mode
        """
        if not self.set_read_only or (self.inverter_needs_reset_force in ["set_read_only"]):
            # Don't reset in read only mode unless forced
            for inverter in self.inverters:
                self.log(
                    "Reset inverter settings to safe mode (set_charge_window={} set_discharge_window={} force={})".format(
                        self.set_charge_window, self.set_discharge_window, self.inverter_needs_reset_force
                    )
                )
                if self.set_charge_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * 60.0 * 1000.0)
                    inverter.disable_charge_window()
                    inverter.adjust_battery_target(100.0)
                if self.set_charge_window or self.set_discharge_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_reserve(0)
                if self.set_discharge_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * 60 * 1000)
                    inverter.adjust_force_discharge(False)
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""

    def execute_plan(self):
        status_extra = ""

        if self.holiday_days_left > 0:
            status = "Idle (Holiday)"
        else:
            status = "Idle"

        if self.inverter_needs_reset:
            self.reset_inverter()

        isCharging = False
        isDischarging = False
        for inverter in self.inverters:
            # Read-only mode
            if self.set_read_only:
                status = "Read-Only"
                continue

            resetDischarge = False
            if (not self.set_discharge_during_charge) or (not self.car_charging_from_battery):
                # These options mess with discharge rate, so we must reset it when they aren't changing it
                resetDischarge = True

            resetReserve = False
            setReserve = False

            # Re-programme charge window based on low rates?
            if self.set_charge_window and self.charge_window_best:
                # Find the next best window and save it
                window = self.charge_window_best[0]
                minutes_start = window["start"]
                minutes_end = window["end"]

                # Combine contiguous windows
                for windows in self.charge_window_best:
                    if minutes_end == windows["start"]:
                        minutes_end = windows["end"]
                        if self.debug_enable:
                            self.log("Combine window with next window {}-{}".format(self.time_abs_str(windows["start"]), self.time_abs_str(windows["end"])))

                # Avoid adjust avoid start time forward when it's already started
                if (inverter.charge_start_time_minutes < self.minutes_now) and (self.minutes_now >= minutes_start):
                    self.log(
                        "Include original charge start {}, keeping this instead of new start {}".format(
                            self.time_abs_str(inverter.charge_start_time_minutes), self.time_abs_str(minutes_start)
                        )
                    )
                    minutes_start = inverter.charge_start_time_minutes

                # Avoid having too long a period to configure as registers only support 24-hours
                if (minutes_start < self.minutes_now) and ((minutes_end - minutes_start) >= 24 * 60):
                    minutes_start = int(self.minutes_now / 30) * 30
                    self.log("Move on charge window start time to avoid wrap - new start {}".format(self.time_abs_str(minutes_start)))

                # Check if start is within 24 hours of now and end is in the future
                if ((minutes_start - self.minutes_now) < (24 * 60)) and minutes_end > self.minutes_now:
                    charge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                    charge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                    self.log(
                        "Charge window will be: {} - {} - current soc {} target {}".format(
                            charge_start_time, charge_end_time, inverter.soc_percent, self.charge_limit_percent_best[0]
                        )
                    )
                    disabled_charge_window = False

                    # Are we actually charging?
                    if self.minutes_now >= minutes_start and self.minutes_now < minutes_end:
                        charge_rate = self.find_charge_rate(
                            self.minutes_now, inverter.soc_kw, window, self.charge_limit_percent_best[0] * inverter.soc_max / 100.0, inverter.battery_rate_max_charge
                        )
                        inverter.adjust_charge_rate(int(charge_rate * 60.0 * 1000.0))

                        # Do we disable discharge during charge?
                        if not self.set_discharge_during_charge and (inverter.soc_percent >= self.charge_limit_percent_best[0]):
                            inverter.adjust_discharge_rate(0)
                            resetDischarge = False

                        if self.set_charge_freeze and self.charge_limit_best[0] == self.reserve:
                            if self.set_soc_enable and self.set_reserve_enable and self.set_reserve_hold:
                                inverter.disable_charge_window()
                                disabled_charge_window = True
                            status = "Freeze charging"
                            status_extra = " target {}%".format(inverter.soc_percent)
                        else:
                            if self.set_soc_enable and self.set_reserve_enable and self.set_reserve_hold and ((inverter.soc_percent + 1) >= self.charge_limit_percent_best[0]):
                                status = "Hold charging"
                                inverter.disable_charge_window()
                                disabled_charge_window = True
                            else:
                                status = "Charging"
                            status_extra = " target {}%".format(self.charge_limit_percent_best[0])

                        isCharging = True

                    if not disabled_charge_window:
                        # Configure the charge window start/end times if in the time window to set them
                        if (self.minutes_now < minutes_end) and (
                            (minutes_start - self.minutes_now) <= self.set_window_minutes or (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes
                        ):
                            # We must re-program if we are about to start a new charge window or the currently configured window is about to start or has started
                            self.log(
                                "Configuring charge window now (now {} target set_window_minutes {} charge start time {}".format(
                                    self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)
                                )
                            )
                            inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)
                        else:
                            self.log(
                                "Not setting charging window yet as not within the window (now {} target set_window_minutes {} charge start time {}".format(
                                    self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)
                                )
                            )

                    # Set configured window minutes for the SOC adjustment routine
                    inverter.charge_start_time_minutes = minutes_start
                    inverter.charge_end_time_minutes = minutes_end
                elif ((minutes_start - self.minutes_now) >= (24 * 60)) and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes:
                    # No charging require in the next 24 hours
                    self.log("No charge window required, disabling before the start")
                    inverter.disable_charge_window()
                else:
                    self.log("No change to charge window yet, waiting for schedule.")
            elif self.set_charge_window and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes:
                # No charge windows
                self.log("No charge windows found, disabling before the start")
                inverter.disable_charge_window()
            elif self.set_charge_window:
                self.log("No change to charge window yet, waiting for schedule.")

            # Set discharge modes/window?
            if self.set_discharge_window and self.discharge_window_best:
                window = self.discharge_window_best[0]
                minutes_start = window["start"]
                minutes_end = window["end"]

                # Avoid adjust avoid start time forward when it's already started
                if (inverter.discharge_start_time_minutes < self.minutes_now) and (self.minutes_now >= minutes_start):
                    self.log(
                        "Include original discharge start {} with our start which is {}".format(
                            self.time_abs_str(inverter.discharge_start_time_minutes), self.time_abs_str(minutes_start)
                        )
                    )
                    minutes_start = inverter.discharge_start_time_minutes

                # Avoid having too long a period to configure as registers only support 24-hours
                if (minutes_start < self.minutes_now) and ((minutes_end - minutes_start) >= 24 * 60):
                    minutes_start = int(self.minutes_now / 30) * 30
                    self.log("Move on discharge window start time to avoid wrap - new start {}".format(self.time_abs_str(minutes_start)))

                discharge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                discharge_end_time = self.midnight_utc + timedelta(minutes=(minutes_end + 1))  # Add in 1 minute margin to allow Predbat to restore ECO mode
                discharge_soc = (self.discharge_limits_best[0] * self.soc_max) / 100.0
                self.log("Next discharge window will be: {} - {} at reserve {}".format(discharge_start_time, discharge_end_time, self.discharge_limits_best[0]))
                if (self.minutes_now >= minutes_start) and (self.minutes_now < minutes_end) and (self.discharge_limits_best[0] < 100.0):
                    if not self.set_discharge_freeze_only and ((self.soc_kw - PREDICT_STEP * inverter.battery_rate_max_discharge_scaled) >= discharge_soc):
                        self.log("Discharging now - current SOC {} and target {}".format(self.soc_kw, self.dp2(discharge_soc)))
                        inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * 60 * 1000)
                        inverter.adjust_force_discharge(True, discharge_start_time, discharge_end_time)
                        resetDischarge = False
                        isDischarging = True

                        if self.set_reserve_enable:
                            inverter.adjust_reserve(self.discharge_limits_best[0])
                            setReserve = True
                        status = "Discharging"
                        status_extra = " target {}%".format(self.discharge_limits_best[0])
                        if self.set_discharge_freeze:
                            # In discharge freeze mode we disable charging during discharge slots
                            inverter.adjust_charge_rate(0)
                    else:
                        inverter.adjust_force_discharge(False)
                        if self.set_discharge_freeze:
                            # In discharge freeze mode we disable charging during discharge slots
                            inverter.adjust_charge_rate(0)
                            self.log("Discharge Freeze as discharge is now at/below target - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                            status = "Freeze discharging"
                            status_extra = " target {}%".format(self.discharge_limits_best[0])
                        else:
                            status = "Hold discharging"
                            status_extra = " target {}%".format(self.discharge_limits_best[0])
                            self.log(
                                "Discharge Hold (ECO mode) as discharge is now at/below target or freeze only is set - current SOC {} and target {}".format(
                                    self.soc_kw, discharge_soc
                                )
                            )
                        resetReserve = True
                else:
                    if (self.minutes_now < minutes_end) and ((minutes_start - self.minutes_now) <= self.set_window_minutes) and self.discharge_limits_best[0]:
                        inverter.adjust_force_discharge(False, discharge_start_time, discharge_end_time)
                        resetReserve = True
                    else:
                        self.log(
                            "Setting ECO mode as we are not yet within the discharge window - next time is {} - {}".format(
                                self.time_abs_str(minutes_start), self.time_abs_str(minutes_end)
                            )
                        )
                        inverter.adjust_force_discharge(False)
                        resetReserve = True

                    if self.set_discharge_freeze:
                        # In discharge freeze mode we disable charging during discharge slots, so turn it back on otherwise
                        inverter.adjust_charge_rate(inverter.battery_rate_max_charge * 60 * 1000)
            elif self.set_discharge_window:
                self.log("Setting ECO mode as no discharge window planned")
                inverter.adjust_force_discharge(False)
                resetReserve = True
                if self.set_discharge_freeze and not isCharging:
                    # In discharge freeze mode we disable charging during discharge slots, so turn it back on otherwise
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * 60 * 1000)

            # Car charging from battery disable?
            if not self.car_charging_from_battery:
                for car_n in range(0, self.num_cars):
                    if self.car_charging_slots[car_n]:
                        window = self.car_charging_slots[car_n][0]
                        self.log(
                            "Car charging from battery is off, next slot for car {} is {} - {}".format(car_n, self.time_abs_str(window["start"]), self.time_abs_str(window["end"]))
                        )
                        if self.minutes_now >= window["start"] and self.minutes_now < window["end"]:
                            # Don't disable discharge during force charge/discharge slots but otherwise turn it off to prevent
                            # from draining the battery
                            if status not in ["Discharging", "Charging"]:
                                inverter.adjust_discharge_rate(0)
                                resetDischarge = False
                                self.log("Disabling battery discharge while the car {} is charging".format(car_n))
                                if status != "Idle":
                                    status += ", Hold for car"
                                else:
                                    status = "Hold for car"
                            break

            # Reset discharge rate?
            if resetDischarge:
                inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * 60 * 1000)

            # Set the SOC just before or within the charge window
            if self.set_soc_enable:
                if (
                    self.charge_limit_best
                    and (self.minutes_now < inverter.charge_end_time_minutes)
                    and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes
                ):
                    if self.set_charge_freeze and (self.charge_limit_best[0] == self.reserve):
                        # In charge freeze hold the target SOC at the current value
                        self.log("Within charge freeze setting target soc to current soc {}".format(inverter.soc_percent))
                        inverter.adjust_battery_target(inverter.soc_percent)
                    else:
                        inverter.adjust_battery_target(self.charge_limit_percent_best[0])
                else:
                    if not inverter.inv_has_target_soc:
                        # If the inverter doesn't support target soc and soc_enable is on then do that logic here:
                        inverter.mimic_target_soc(0)
                    elif not self.inverter_hybrid and self.inverter_soc_reset:
                        if self.charge_limit_best and self.minutes_now >= inverter.charge_start_time_minutes and self.minutes_now < inverter.charge_end_time_minutes:
                            self.log(
                                "Within the charge window, holding SOC setting {} (now {} target set_soc_minutes {} charge start time {})".format(
                                    self.charge_limit_percent_best[0],
                                    self.time_abs_str(self.minutes_now),
                                    self.set_soc_minutes,
                                    self.time_abs_str(inverter.charge_start_time_minutes),
                                )
                            )
                        else:
                            self.log(
                                "Resetting charging SOC as we are not within the window and inverter_soc_reset is enabled (now {} target set_soc_minutes {} charge start time {})".format(
                                    self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)
                                )
                            )
                            inverter.adjust_battery_target(100.0)
                    else:
                        self.log(
                            "Not setting charging SOC as we are not within the window (now {} target set_soc_minutes {} charge start time {})".format(
                                self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)
                            )
                        )

            # If we should set reserve?
            if self.set_soc_enable and self.set_reserve_enable and not setReserve:
                # In the window then set it, otherwise put it back
                if self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and (self.minutes_now >= inverter.charge_start_time_minutes):
                    if self.set_charge_freeze and (self.charge_limit_best[0] == self.reserve):
                        self.log("Adjust reserve to hold current soc {} % (set_reserve_enable is true)".format(inverter.soc_percent))
                        inverter.adjust_reserve(min(inverter.soc_percent + 1, 100))
                    elif inverter.soc_percent >= self.charge_limit_percent_best[0]:
                        self.log("Adjust reserve to hold target charge {} % (set_reserve_enable is true)".format(self.charge_limit_percent_best[0]))
                        inverter.adjust_reserve(min(self.charge_limit_percent_best[0] + 1, 100))
                    else:
                        self.log("Adjust reserve to target charge {} % (set_reserve_enable is true)".format(self.charge_limit_percent_best[0]))
                        inverter.adjust_reserve(self.charge_limit_percent_best[0])
                    resetReserve = False
                else:
                    self.log("Adjust reserve to default (as set_reserve_enable is true)")
                    inverter.adjust_reserve(0)
                    resetReserve = False

            # Reset reserve as discharge is enable but not running right now
            if self.set_reserve_enable and resetReserve and not setReserve:
                inverter.adjust_reserve(0)

        # Set the charge/discharge status information
        self.set_charge_discharge_status(isCharging, isDischarging)
        return status, status_extra

    def fetch_octopus_rates(self, entity_id, adjust_key=None):
        data_all = []
        rate_data = {}

        if entity_id:
            # From 9.0.0 of the Octopus plugin the data is split between previous rate, current rate and next rate
            # and the sensor is replaced with an event - try to support the old settings and find the new events

            if self.debug_enable:
                self.log("Info: Fetch Octopus rates from {}".format(entity_id))

            # Previous rates
            if "_current_rate" in entity_id:
                # Try as event
                prev_rate_id = entity_id.replace("_current_rate", "_previous_day_rates").replace("sensor.", "event.")
                data_import = self.get_state(entity_id=prev_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    prev_rate_id = entity_id.replace("_current_rate", "_previous_rate")
                    data_import = self.get_state(entity_id=prev_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
                    else:
                        self.log("WARN: No Octopus data in sensor {} attribute 'all_rates'".format(prev_rate_id))

            # Current rates
            if "_current_rate" in entity_id:
                current_rate_id = entity_id.replace("_current_rate", "_current_day_rates").replace("sensor.", "event.")
            else:
                current_rate_id = entity_id

            data_import = self.get_state(entity_id=current_rate_id, attribute="rates")
            if data_import:
                data_all += data_import
            else:
                data_import = self.get_state(entity_id=current_rate_id, attribute="all_rates")
                if data_import:
                    data_all += data_import
                else:
                    self.log("WARN: No Octopus data in sensor {} attribute 'all_rates'".format(current_rate_id))

            # Next rates
            if "_current_rate" in entity_id:
                next_rate_id = entity_id.replace("_current_rate", "_next_day_rates").replace("sensor.", "event.")
                data_import = self.get_state(entity_id=next_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    next_rate_id = entity_id.replace("_current_rate", "_next_rate")
                    data_import = self.get_state(entity_id=next_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import

        if data_all:
            rate_key = "rate"
            from_key = "from"
            to_key = "to"
            scale = 1.0
            if rate_key not in data_all[0]:
                rate_key = "value_inc_vat"
                from_key = "valid_from"
                to_key = "valid_to"
            if from_key not in data_all[0]:
                from_key = "start"
                to_key = "end"
                scale = 100.0
            rate_data = self.minute_data(
                data_all, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key, adjust_key=adjust_key, scale=scale
            )

        return rate_data

    def fetch_sensor_data(self):
        """
        Fetch all the data, e.g. energy rates, load, PV predictions, car plan etc.
        """
        self.rate_import = {}
        self.rate_import_replicated = {}
        self.rate_export = {}
        self.rate_export_replicated = {}
        self.rate_slots = []
        self.io_adjusted = {}
        self.low_rates = []
        self.high_export_rates = []
        self.octopus_slots = []
        self.cost_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.pv_today = {}
        self.load_minutes = {}
        self.load_minutes_age = 0
        self.load_forecast = {}
        self.pv_forecast_minute = {}
        self.pv_forecast_minute10 = {}

        # Iboost load data
        if self.iboost_enable:
            if "iboost_energy_today" in self.args:
                self.iboost_energy_today, iboost_energy_age = self.minute_data_load(self.now_utc, "iboost_energy_today", 1)
                if iboost_energy_age >= 1:
                    self.iboost_today = self.dp2(abs(self.iboost_energy_today[0] - self.iboost_energy_today[self.minutes_now]))
                    self.log("IBoost energy today from sensor reads {} kWh".format(self.iboost_today))

        # Load previous load data
        if self.get_arg("ge_cloud_data", False):
            self.download_ge_data(self.now_utc)
        else:
            # Load data
            if "load_today" in self.args:
                self.load_minutes, self.load_minutes_age = self.minute_data_load(self.now_utc, "load_today", self.max_days_previous)
                self.log("Found {} load_today datapoints going back {} days".format(len(self.load_minutes), self.load_minutes_age))
                self.load_minutes_now = max(self.load_minutes.get(0, 0) - self.load_minutes.get(self.minutes_now, 0), 0)
            else:
                self.log("Error: You have not set load_today, you will have no load data")
                self.record_status(message="Error - load_today not set correctly", had_errors=True)
                raise ValueError

            # Load import today data
            if "import_today" in self.args:
                self.import_today = self.minute_data_import_export(self.now_utc, "import_today", scale=self.import_export_scaling)
                self.import_today_now = max(self.import_today.get(0, 0) - self.import_today.get(self.minutes_now, 0), 0)
            else:
                self.log("WARN: You have not set import_today in apps.yaml, you will have no previous import data")

            # Load export today data
            if "export_today" in self.args:
                self.export_today = self.minute_data_import_export(self.now_utc, "export_today", scale=self.import_export_scaling)
                self.export_today_now = max(self.export_today.get(0, 0) - self.export_today.get(self.minutes_now, 0), 0)
            else:
                self.log("WARN: You have not set export_today in apps.yaml, you will have no previous export data")

            # PV today data
            if "pv_today" in self.args:
                self.pv_today = self.minute_data_import_export(self.now_utc, "pv_today")
                self.pv_today_now = max(self.pv_today.get(0, 0) - self.pv_today.get(self.minutes_now, 0), 0)
            else:
                self.log("WARN: You have not set pv_today in apps.yaml, you will have no previous pv data")

        # Car charging hold - when enabled battery is held during car charging in simulation
        self.car_charging_energy = self.load_car_energy(self.now_utc)

        # Log current values
        self.log(
            "Current data so far today: load {} kWh import {} kWh export {} kWh pv {} kWh".format(
                self.dp2(self.load_minutes_now), self.dp2(self.import_today_now), self.dp2(self.export_today_now), self.dp2(self.pv_today_now)
            )
        )

        # futurerate data
        self.future_energy_rates_import, self.future_energy_rates_export = self.futurerate_analysis()

        if "rates_import_octopus_url" in self.args:
            # Fixed URL for rate import
            self.log("Downloading import rates directly from url {}".format(self.get_arg("rates_import_octopus_url", indirect=False)))
            self.rate_import = self.download_octopus_rates(self.get_arg("rates_import_octopus_url", indirect=False))
        elif "metric_octopus_import" in self.args:
            # Octopus import rates
            entity_id = self.get_arg("metric_octopus_import", None, indirect=False)
            self.rate_import = self.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")
            if not self.rate_import:
                self.log("Error: metric_octopus_import is not set correctly or no energy rates can be read")
                self.record_status(message="Error - metric_octopus_import not set correctly or no energy rates can be read", had_errors=True)
                raise ValueError
        else:
            # Basic rates defined by user over time
            self.rate_import = self.basic_rates(self.get_arg("rates_import", [], indirect=False), "import")

        # Gas rates if set
        if "metric_octopus_gas" in self.args:
            entity_id = self.get_arg("metric_octopus_gas", None, indirect=False)
            self.rate_gas = self.fetch_octopus_rates(entity_id)
            if not self.rate_gas:
                self.log("Error: metric_octopus_gas is not set correctly or no energy rates can be read")
                self.record_status(message="Error - rate_import_gas not set correctly or no energy rates can be read", had_errors=True)
                raise ValueError
            self.rate_gas, self.rate_gas_replicated = self.rate_replicate(self.rate_gas, is_import=False, is_gas=False)
            self.rate_gas = self.rate_scan_gas(self.rate_gas, print=True)

        # Work out current car SOC and limit
        self.car_charging_loss = 1 - float(self.get_arg("car_charging_loss"))

        # Octopus intelligent slots
        if "octopus_intelligent_slot" in self.args:
            completed = []
            planned = []
            vehicle = {}
            vehicle_pref = {}
            entity_id = self.get_arg("octopus_intelligent_slot", indirect=False)
            try:
                completed = self.get_state(entity_id=entity_id, attribute="completedDispatches")
                if not completed:
                    completed = self.get_state(entity_id=entity_id, attribute="completed_dispatches")
                planned = self.get_state(entity_id=entity_id, attribute="plannedDispatches")
                if not planned:
                    planned = self.get_state(entity_id=entity_id, attribute="planned_dispatches")
                vehicle = self.get_state(entity_id=entity_id, attribute="registeredKrakenflexDevice")
                vehicle_pref = self.get_state(entity_id=entity_id, attribute="vehicleChargingPreferences")
            except (ValueError, TypeError):
                self.log("WARN: Unable to get data from {} - octopus_intelligent_slot may not be set correctly".format(entity_id))
                self.record_status(message="Error - octopus_intelligent_slot not set correctly", had_errors=True)

            # Completed and planned slots
            if completed:
                self.octopus_slots += completed
            if planned:
                self.octopus_slots += planned

            # Get rate for import to compute charging costs
            if self.rate_import:
                self.rate_import = self.rate_scan(self.rate_import, print=False)

            if self.num_cars >= 1:
                # Extract vehicle data if we can get it
                if vehicle:
                    self.car_charging_battery_size[0] = float(vehicle.get("vehicleBatterySizeInKwh", self.car_charging_battery_size[0]))
                    self.car_charging_rate[0] = float(vehicle.get("chargePointPowerInKw", self.car_charging_rate[0]))
                else:
                    size = self.get_state(entity_id=entity_id, attribute="vehicle_battery_size_in_kwh")
                    rate = self.get_state(entity_id=entity_id, attribute="charge_point_power_in_kw")
                    if size:
                        self.car_charging_battery_size[0] = size
                    if rate:
                        self.car_charging_rate[0] = rate

                # Get car charging limit again from car based on new battery size
                self.car_charging_limit[0] = (float(self.get_arg("car_charging_limit", 100.0, index=0)) * self.car_charging_battery_size[0]) / 100.0

                # Extract vehicle preference if we can get it
                if vehicle_pref and self.octopus_intelligent_charging:
                    octopus_limit = max(float(vehicle_pref.get("weekdayTargetSoc", 100)), float(vehicle_pref.get("weekendTargetSoc", 100)))
                    octopus_ready_time = vehicle_pref.get("weekdayTargetTime", None)
                    if not octopus_ready_time:
                        octopus_ready_time = self.car_charging_plan_time[0]
                    else:
                        octopus_ready_time += ":00"
                    self.car_charging_plan_time[0] = octopus_ready_time
                    octopus_limit = self.dp2(octopus_limit * self.car_charging_battery_size[0] / 100.0)
                    self.car_charging_limit[0] = min(self.car_charging_limit[0], octopus_limit)
                elif self.octopus_intelligent_charging:
                    octopus_ready_time = self.get_arg("octopus_ready_time", None)
                    octopus_limit = self.get_arg("octopus_charge_limit", None)
                    if octopus_limit:
                        try:
                            octopus_limit = float(octopus_limit)
                        except ValueError:
                            self.log("Warn: octopus_limit is set to a bad value {} in apps.yaml, must be a number".format(octopus_limit))
                            octopus_limit = None
                    if octopus_limit:
                        octopus_limit = self.dp2(float(octopus_limit) * self.car_charging_battery_size[0] / 100.0)
                        self.car_charging_limit[0] = min(self.car_charging_limit[0], octopus_limit)
                    if octopus_ready_time:
                        self.car_charging_plan_time[0] = octopus_ready_time

                # Use octopus slots for charging?
                if self.octopus_intelligent_charging:
                    self.octopus_slots = self.add_now_to_octopus_slot(self.octopus_slots, self.now_utc)
                    if not self.octopus_intelligent_ignore_unplugged or self.car_charging_planned[0]:
                        self.car_charging_slots[0] = self.load_octopus_slots(self.octopus_slots)
                        self.log(
                            "Car 0 using Octopus Intelligent, charging limit {}, ready time {} - battery size {}".format(
                                self.car_charging_limit[0], self.car_charging_plan_time[0], self.car_charging_battery_size[0]
                            )
                        )
                    else:
                        self.log("Car 0 using Octopus Intelligent is unplugged")
        else:
            # Disable octopus charging if we don't have the slot sensor
            self.octopus_intelligent_charging = False

        # Work out car SOC
        self.car_charging_soc = [0.0 for car_n in range(0, self.num_cars)]
        for car_n in range(0, self.num_cars):
            self.car_charging_soc[car_n] = (self.get_arg("car_charging_soc", 0.0, index=car_n) * self.car_charging_battery_size[car_n]) / 100.0
        if self.num_cars:
            self.log("Current Car SOC kWh: {}".format(self.car_charging_soc))

        if "rates_export_octopus_url" in self.args:
            # Fixed URL for rate export
            self.log("Downloading export rates directly from url {}".format(self.get_arg("rates_export_octopus_url", indirect=False)))
            self.rate_export = self.download_octopus_rates(self.get_arg("rates_export_octopus_url", indirect=False))
        elif "metric_octopus_export" in self.args:
            # Octopus export rates
            entity_id = self.get_arg("metric_octopus_export", None, indirect=False)
            self.rate_export = self.fetch_octopus_rates(entity_id)
            if not self.rate_export:
                self.log("Warning: metric_octopus_export is not set correctly or no energy rates can be read")
                self.record_status(message="Error - metric_octopus_export not set correctly or no energy rates can be read", had_errors=True)
        else:
            # Basic rates defined by user over time
            self.rate_export = self.basic_rates(self.get_arg("rates_export", [], indirect=False), "export")

        # Octopus saving session
        octopus_saving_slots = []
        if "octopus_saving_session" in self.args:
            saving_rate = 200  # Default rate if not reported
            octopoints_per_penny = self.get_arg("octopus_saving_session_octopoints_per_penny", 8)  # Default 8 octopoints per found

            entity_id = self.get_arg("octopus_saving_session", indirect=False)
            if entity_id:
                state = self.get_arg("octopus_saving_session", False)

                joined_events = self.get_state(entity_id=entity_id, attribute="joined_events")
                if not joined_events:
                    entity_id = entity_id.replace("binary_sensor.", "event.").replace("_sessions", "_session_events")
                    joined_events = self.get_state(entity_id=entity_id, attribute="joined_events")

                available_events = self.get_state(entity_id=entity_id, attribute="available_events")
                if available_events:
                    for event in available_events:
                        code = event.get("code", None)
                        start = event.get("start", None)
                        end = event.get("end", None)
                        saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                        if code:  # Join the new Octopus saving event and send an alert
                            self.log("Joining Octopus saving event code {} start {} end {} price per kWh {}".format(code, start, end, saving_rate))
                            self.call_service("octopus_energy/join_octoplus_saving_session_event", event_code=code, entity_id=entity_id)
                            self.call_notify("Predbat: Joined Octopus saving event: start {}, end {}, price per kWh {}".format(start, end, saving_rate))

                if joined_events:
                    for event in joined_events:
                        start = event.get("start", None)
                        end = event.get("end", None)
                        saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                        if start and end and saving_rate > 0:
                            # Save the saving slot?
                            try:
                                start_time = self.str2time(start)
                                end_time = self.str2time(end)
                                diff_time = start_time - self.now_utc
                                if abs(diff_time.days) <= 3:
                                    self.log(
                                        "Joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(
                                            start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state
                                        )
                                    )

                                    # Save the slot
                                    octopus_saving_slot = {}
                                    octopus_saving_slot["start"] = start
                                    octopus_saving_slot["end"] = end
                                    octopus_saving_slot["rate"] = saving_rate
                                    octopus_saving_slot["state"] = state
                                    octopus_saving_slots.append(octopus_saving_slot)
                            except ValueError:
                                self.log(
                                    "Warn: Bad start time for joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(
                                        start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state
                                    )
                                )

                    # In saving session that's not reported, assumed 30-minutes
                    if state and not joined_events:
                        octopus_saving_slot = {}
                        octopus_saving_slot["start"] = None
                        octopus_saving_slot["end"] = None
                        octopus_saving_slot["rate"] = saving_rate
                        octopus_saving_slot["state"] = state
                        octopus_saving_slots.append(octopus_saving_slot)
                    if state:
                        self.log("Octopus Saving session is active!")

        # Standing charge
        self.metric_standing_charge = self.get_arg("metric_standing_charge", 0.0) * 100.0
        self.log("Standing charge is set to {} p".format(self.metric_standing_charge))

        # Replicate and scan import rates
        if self.rate_import:
            self.rate_import = self.rate_scan(self.rate_import, print=False)
            self.rate_import, self.rate_import_replicated = self.rate_replicate(self.rate_import, self.io_adjusted, is_import=True)
            self.rate_import = self.rate_add_io_slots(self.rate_import, self.octopus_slots)
            self.load_saving_slot(octopus_saving_slots, export=False)
            if "rates_import_override" in self.args:
                self.rate_import = self.basic_rates(self.get_arg("rates_import_override", [], indirect=False), "import", self.rate_import)
            self.rate_import = self.rate_scan(self.rate_import, print=True)
        else:
            self.log("Warning: No import rate data provided")
            self.record_status(message="Error - No import rate data provided", had_errors=True)

        # Replicate and scan export rates
        if self.rate_export:
            self.rate_export = self.rate_scan_export(self.rate_export, print=False)
            self.rate_export, self.rate_export_replicated = self.rate_replicate(self.rate_export, is_import=False)
            # For export tariff only load the saving session if enabled
            if self.rate_export_max > 0:
                self.load_saving_slot(octopus_saving_slots, export=True)
            if "rates_export_override" in self.args:
                self.rate_export = self.basic_rates(self.get_arg("rates_export_override", [], indirect=False), "export", self.rate_export)
            self.rate_export = self.rate_scan_export(self.rate_export, print=True)
        else:
            self.log("Warning: No export rate data provided")
            self.record_status(message="Error - No export rate data provided", had_errors=True)

        # Set rate thresholds
        if self.rate_import or self.rate_export:
            self.set_rate_thresholds()

        # Find discharging windows
        if self.rate_export:
            self.high_export_rates, lowest, highest = self.rate_scan_window(self.rate_export, 5, self.rate_export_cost_threshold, True)
            # Update threshold automatically
            self.log("High export rate found rates in range {} to {}".format(lowest, highest))
            if self.rate_high_threshold == 0 and lowest <= self.rate_export_max:
                self.rate_export_cost_threshold = lowest

        # Find charging windows
        if self.rate_import:
            # Find charging window
            self.low_rates, lowest, highest = self.rate_scan_window(self.rate_import, 5, self.rate_import_cost_threshold, False)
            self.log("Low Import rate found rates in range {} to {}".format(lowest, highest))
            # Update threshold automatically
            if self.rate_low_threshold == 0 and highest >= self.rate_min:
                self.rate_import_cost_threshold = highest

        # Work out car plan?
        for car_n in range(0, self.num_cars):
            if self.octopus_intelligent_charging and car_n == 0:
                self.log("Car 0 is using Octopus intelligent schedule")
            elif self.car_charging_planned[car_n] or self.car_charging_now[car_n]:
                self.log(
                    "Plan car {} charging from {} to {} with slots {} from soc {} to {} ready by {}".format(
                        car_n,
                        self.car_charging_soc[car_n],
                        self.car_charging_limit[car_n],
                        self.low_rates,
                        self.car_charging_soc[car_n],
                        self.car_charging_limit[car_n],
                        self.car_charging_plan_time[car_n],
                    )
                )
                self.car_charging_slots[car_n] = self.plan_car_charging(car_n, self.low_rates)
            else:
                self.log("Not planning car charging for car {} - car charging planned is False".format(car_n))

            # Log the charging plan
            if self.car_charging_slots[car_n]:
                self.log("Car {} charging plan is: {}".format(car_n, self.car_charging_slots[car_n]))

        # Publish the car plan
        self.publish_car_plan()

        # Work out cost today
        if self.import_today:
            self.cost_today_sofar = self.today_cost(self.import_today, self.export_today)

        # Fetch PV forecast if enabled, today must be enabled, other days are optional
        self.pv_forecast_minute, self.pv_forecast_minute10 = self.fetch_pv_forecast()

        # Fetch extra load forecast
        self.load_forecast = self.fetch_extra_load_forecast(self.now_utc)

        # Apply modal filter to historical data
        self.previous_days_modal_filter(self.load_minutes)
        self.log("Historical days now {} weight {}".format(self.days_previous, self.days_previous_weight))

        # Load today vs actual
        if self.load_minutes:
            self.load_inday_adjustment = self.load_today_comparison(self.load_minutes, self.load_forecast, self.car_charging_energy, self.import_today, self.minutes_now)

    def publish_rate_and_threshold(self):
        """
        Publish energy rate data and thresholds
        """
        # Find discharging windows
        if self.rate_export:
            if self.rate_best_cost_threshold_discharge:
                self.rate_export_cost_threshold = self.rate_best_cost_threshold_discharge
                self.log("Export threshold used for optimisation was {}p".format(self.rate_export_cost_threshold))
            self.publish_rates(self.rate_export, True)

        # Find charging windows
        if self.rate_import:
            if self.rate_best_cost_threshold_charge:
                self.rate_import_cost_threshold = self.rate_best_cost_threshold_charge
                self.log("Import threshold used for optimisation was {}p".format(self.rate_import_cost_threshold))
            self.publish_rates(self.rate_import, False)

    def fetch_inverter_data(self):
        """
        Fetch data about the inverters
        """
        # Find the inverters
        self.num_inverters = int(self.get_arg("num_inverters", 1))
        self.inverter_limit = 0.0
        self.export_limit = 0.0
        self.inverters = []
        self.charge_window = []
        self.discharge_window = []
        self.discharge_limits = []
        self.current_charge_limit = 0.0
        self.soc_kw = 0.0
        self.soc_max = 0.0
        self.reserve = 0.0
        self.reserve_percent = 0.0
        self.reserve_current = 0.0
        self.reserve_current_percent = 0.0
        self.battery_rate_max_charge = 0.0
        self.battery_rate_max_discharge = 0.0
        self.battery_rate_max_charge_scaled = 0.0
        self.battery_rate_max_discharge_scaled = 0.0
        self.battery_rate_min = 0
        self.charge_rate_now = 0.0
        self.discharge_rate_now = 0.0
        found_first = False

        # For each inverter get the details
        for id in range(0, self.num_inverters):
            inverter = Inverter(self, id)
            inverter.update_status(self.minutes_now)

            # As the inverters will run in lockstep, we will initially look at the programming of the first enabled one for the current window setting
            if not found_first:
                found_first = True
                self.current_charge_limit = inverter.current_charge_limit
                self.charge_window = inverter.charge_window
                self.discharge_window = inverter.discharge_window
                self.discharge_limits = inverter.discharge_limits
                if not inverter.inv_support_discharge_freeze:
                    # Force off unsupported feature
                    self.log("Note: Inverter does not support discharge freeze - disabled")
                    self.set_discharge_freeze = False
                    self.set_discharge_freeze_only = False
                if not inverter.inv_has_reserve_soc:
                    self.log("Note: Inverter does not support reserve - disabled")
                    self.set_reserve_enable = False
                    self.set_reserve_hold = False
            self.soc_max += inverter.soc_max
            self.soc_kw += inverter.soc_kw
            self.reserve += inverter.reserve
            self.reserve_current += inverter.reserve_current
            self.battery_rate_max_charge += inverter.battery_rate_max_charge
            self.battery_rate_max_discharge += inverter.battery_rate_max_discharge
            self.battery_rate_max_charge_scaled += inverter.battery_rate_max_charge_scaled
            self.battery_rate_max_discharge_scaled += inverter.battery_rate_max_discharge_scaled
            self.charge_rate_now += inverter.charge_rate_now
            self.discharge_rate_now += inverter.discharge_rate_now
            self.battery_rate_min += inverter.battery_rate_min
            self.inverters.append(inverter)
            self.inverter_limit += inverter.inverter_limit
            self.export_limit += inverter.export_limit

        # Remove extra decimals
        self.soc_max = self.dp2(self.soc_max)
        self.soc_kw = self.dp2(self.soc_kw)
        self.reserve = self.dp2(self.reserve)
        self.reserve_percent = int(self.reserve / self.soc_max * 100.0 + 0.5)
        self.reserve_current = self.dp2(self.reserve_current)
        self.reserve_current_percent = int(self.reserve_current / self.soc_max * 100.0 + 0.5)

        self.log(
            "Found {} inverters totals: min reserve {} current reserve {} soc_max {} soc {} charge rate {} kW discharge rate {} kW battery_rate_min {} w ac limit {} export limit {} kW loss charge {} % loss discharge {} % inverter loss {} %".format(
                len(self.inverters),
                self.reserve,
                self.reserve_current,
                self.soc_max,
                self.soc_kw,
                self.charge_rate_now * 60,
                self.discharge_rate_now * 60,
                self.battery_rate_min * 60 * 1000,
                self.dp2(self.inverter_limit * 60),
                self.dp2(self.export_limit * 60),
                100 - int(self.battery_loss * 100),
                100 - int(self.battery_loss_discharge * 100),
                100 - int(self.inverter_loss * 100),
            )
        )

        # Work out current charge limits and publish charge limit base
        self.charge_limit = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window))]
        self.charge_limit_percent = self.calc_percent_limit(self.charge_limit)
        self.publish_charge_limit(self.charge_limit, self.charge_window, self.charge_limit_percent, best=False)

        self.log("Base charge    window {}".format(self.window_as_text(self.charge_window, self.charge_limit)))
        self.log("Base discharge window {}".format(self.window_as_text(self.discharge_window, self.discharge_limits)))

    def fetch_config_options(self):
        """
        Fetch all the configuration options
        """

        self.debug_enable = self.get_arg("debug_enable")
        self.previous_status = self.get_state(self.prefix + ".status")
        forecast_hours = self.get_arg("forecast_hours", 48)
        self.calculate_fast_plan = self.get_arg("calculate_fast_plan")

        if self.calculate_fast_plan:
            self.calculate_max_windows = max(int(forecast_hours), 12)
        else:
            self.calculate_max_windows = max(int(forecast_hours * 2), 24)

        self.num_cars = self.get_arg("num_cars", 1)
        self.inverter_type = self.get_arg("inverter_type", "GE", indirect=False)
        self.calculate_plan_every = max(self.get_arg("calculate_plan_every"), 5)

        self.log(
            "Inverter type {} forecast_hours {} max_windows {} num_cars {} debug enable is {} calculate_plan_every {}".format(
                self.inverter_type, forecast_hours, self.calculate_max_windows, self.num_cars, self.debug_enable, self.calculate_plan_every
            )
        )

        # Days previous
        self.holiday_days_left = self.get_arg("holiday_days_left")
        self.days_previous = self.get_arg("days_previous", [7])
        self.days_previous_weight = self.get_arg("days_previous_weight", [1 for i in range(0, len(self.days_previous))])
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(0, len(self.days_previous) - len(self.days_previous_weight))]
        if self.holiday_days_left > 0:
            self.days_previous = [1]
            self.log("Holiday mode is active, {} days remaining, setting days previous to 1".format(self.holiday_days_left))
        self.max_days_previous = max(self.days_previous) + 1

        self.forecast_days = int((forecast_hours + 23) / 24)
        self.forecast_minutes = forecast_hours * 60
        self.forecast_plan_hours = max(min(self.get_arg("forecast_plan_hours", 96), forecast_hours), 8)
        self.inverter_clock_skew_start = self.get_arg("inverter_clock_skew_start", 0)
        self.inverter_clock_skew_end = self.get_arg("inverter_clock_skew_end", 0)
        self.inverter_clock_skew_discharge_start = self.get_arg("inverter_clock_skew_discharge_start", 0)
        self.inverter_clock_skew_discharge_end = self.get_arg("inverter_clock_skew_discharge_end", 0)

        # Log clock skew
        if self.inverter_clock_skew_start != 0 or self.inverter_clock_skew_end != 0:
            self.log("Inverter clock skew start {} end {} applied".format(self.inverter_clock_skew_start, self.inverter_clock_skew_end))
        if self.inverter_clock_skew_discharge_start != 0 or self.inverter_clock_skew_discharge_end != 0:
            self.log("Inverter clock skew discharge start {} end {} applied".format(self.inverter_clock_skew_discharge_start, self.inverter_clock_skew_discharge_end))

        # Metric config
        self.metric_min_improvement = self.get_arg("metric_min_improvement")
        self.metric_min_improvement_discharge = self.get_arg("metric_min_improvement_discharge")
        self.metric_battery_cycle = self.get_arg("metric_battery_cycle")
        self.metric_future_rate_offset_import = self.get_arg("metric_future_rate_offset_import")
        self.metric_future_rate_offset_export = self.get_arg("metric_future_rate_offset_export")
        self.metric_inday_adjust_damping = self.get_arg("metric_inday_adjust_damping")
        self.rate_low_threshold = self.get_arg("rate_low_threshold")
        self.rate_high_threshold = self.get_arg("rate_high_threshold")
        self.inverter_soc_reset = self.get_arg("inverter_soc_reset")

        self.metric_battery_value_scaling = self.get_arg("metric_battery_value_scaling")
        self.notify_devices = self.get_arg("notify_devices", ["notify"])
        self.pv_scaling = self.get_arg("pv_scaling")
        self.pv_metric10_weight = self.get_arg("pv_metric10_weight")
        self.load_scaling = self.get_arg("load_scaling")
        self.load_scaling10 = self.get_arg("load_scaling10")
        self.battery_rate_max_scaling = self.get_arg("battery_rate_max_scaling")

        self.best_soc_step = 0.25
        self.metric_cloud_enable = self.get_arg("metric_cloud_enable")

        # Battery charging options
        self.battery_capacity_nominal = self.get_arg("battery_capacity_nominal")
        self.battery_loss = 1.0 - self.get_arg("battery_loss")
        self.battery_loss_discharge = 1.0 - self.get_arg("battery_loss_discharge")
        self.inverter_loss = 1.0 - self.get_arg("inverter_loss")
        self.inverter_hybrid = self.get_arg("inverter_hybrid")
        self.battery_scaling = self.get_arg("battery_scaling", 1.0)
        self.battery_charge_power_curve = self.args.get("battery_charge_power_curve", {})
        # Check power curve is a dictionary
        if not isinstance(self.battery_charge_power_curve, dict):
            self.battery_charge_power_curve = {}
            self.log("WARN: battery_power_curve is incorrectly configured - ignoring")
            self.record_status("battery_power_curve is incorrectly configured - ignoring", had_errors=True)
        self.import_export_scaling = self.get_arg("import_export_scaling", 1.0)
        self.best_soc_margin = 0.0
        self.best_soc_min = self.get_arg("best_soc_min")
        self.best_soc_max = self.get_arg("best_soc_max")
        self.best_soc_keep = self.get_arg("best_soc_keep")
        self.set_soc_minutes = 30
        self.set_window_minutes = 30
        self.octopus_intelligent_charging = self.get_arg("octopus_intelligent_charging")
        self.octopus_intelligent_ignore_unplugged = self.get_arg("octopus_intelligent_ignore_unplugged")
        self.get_car_charging_planned()
        self.load_inday_adjustment = 1.0

        self.combine_mixed_rates = False
        self.combine_discharge_slots = self.get_arg("combine_discharge_slots")
        self.combine_charge_slots = self.get_arg("combine_charge_slots")
        self.charge_slot_split = 60
        self.discharge_slot_split = 60 if self.calculate_fast_plan else 30

        # Enables
        if self.inverter_type != "GE":
            self.log("WARN: Using experimental inverter type {} - not all features are available".format(self.inverter_type))

        self.calculate_best = True
        self.set_read_only = self.get_arg("set_read_only")

        # hard wired options, can be configured per inverter later on
        self.set_soc_enable = True
        self.set_reserve_enable = self.get_arg("set_reserve_enable")
        self.set_reserve_hold = True
        self.set_discharge_freeze = True
        self.set_charge_freeze = self.get_arg("set_charge_freeze")
        self.set_charge_low_power = self.get_arg("set_charge_low_power")
        self.calculate_discharge_first = True

        self.set_status_notify = self.get_arg("set_status_notify")
        self.set_inverter_notify = self.get_arg("set_inverter_notify")
        self.set_discharge_freeze_only = self.get_arg("set_discharge_freeze_only")
        self.set_discharge_during_charge = self.get_arg("set_discharge_during_charge")

        # Mode
        self.predbat_mode = self.get_arg("mode")
        if self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_SOC]:
            self.calculate_best_charge = True
            self.calculate_best_discharge = False
            self.set_charge_window = False
            self.set_discharge_window = False
        elif self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGE]:
            self.calculate_best_charge = True
            self.calculate_best_discharge = False
            self.set_charge_window = True
            self.set_discharge_window = False
        elif self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGEDISCHARGE]:
            self.calculate_best_charge = True
            self.calculate_best_discharge = True
            self.set_charge_window = True
            self.set_discharge_window = True
        else:  # PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]
            self.calculate_best_charge = False
            self.calculate_best_discharge = False
            self.set_charge_window = False
            self.set_discharge_window = False
            self.predbat_mode = PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]
            self.expose_config("mode", self.predbat_mode)

        self.log("Predbat mode is set to {}".format(self.predbat_mode))

        self.calculate_discharge_oncharge = self.get_arg("calculate_discharge_oncharge")
        self.calculate_second_pass = self.get_arg("calculate_second_pass")
        self.calculate_inday_adjustment = self.get_arg("calculate_inday_adjustment")
        self.calculate_tweak_plan = self.get_arg("calculate_tweak_plan")

        self.balance_inverters_enable = self.get_arg("balance_inverters_enable")
        self.balance_inverters_charge = self.get_arg("balance_inverters_charge")
        self.balance_inverters_discharge = self.get_arg("balance_inverters_discharge")
        self.balance_inverters_crosscharge = self.get_arg("balance_inverters_crosscharge")
        self.balance_inverters_threshold_charge = max(self.get_arg("balance_inverters_threshold_charge"), 1.0)
        self.balance_inverters_threshold_discharge = max(self.get_arg("balance_inverters_threshold_discharge"), 1.0)

        if self.set_read_only:
            self.log("NOTE: Read-only mode is enabled, the inverter controls will not be used!!")

        # Enable load filtering
        self.load_filter_modal = self.get_arg("load_filter_modal")

        # Iboost model
        self.iboost_enable = self.get_arg("iboost_enable")
        self.iboost_solar = self.get_arg("iboost_solar")
        self.iboost_gas = self.get_arg("iboost_gas")
        self.iboost_charging = self.get_arg("iboost_charging")
        self.iboost_gas_scale = self.get_arg("iboost_gas_scale")

        self.iboost_max_energy = self.get_arg("iboost_max_energy")
        self.iboost_max_power = self.get_arg("iboost_max_power") / 1000 / 60.0
        self.iboost_min_power = self.get_arg("iboost_min_power") / 1000 / 60.0
        self.iboost_min_soc = self.get_arg("iboost_min_soc")
        self.iboost_today = self.get_arg("iboost_today")
        self.iboost_next = self.iboost_today
        self.iboost_running = False
        self.iboost_energy_today = {}

        # Car options
        self.car_charging_hold = self.get_arg("car_charging_hold")
        self.car_charging_threshold = float(self.get_arg("car_charging_threshold")) / 60.0
        self.car_charging_energy_scale = self.get_arg("car_charging_energy_scale")

    @ad.app_lock
    def update_pred(self, scheduled=True):
        """
        Update the prediction state, everything is called from here right now
        """
        status_extra = ""
        self.had_errors = False
        self.dashboard_index = []
        local_tz = pytz.timezone(self.get_arg("timezone", "Europe/London"))
        skew = self.get_arg("clock_skew", 0)
        if skew:
            self.log("WARN: Clock skew is set to {} minutes".format(skew))
        now_utc = datetime.now(local_tz) + timedelta(minutes=skew)
        now = datetime.now() + timedelta(minutes=skew)
        if SIMULATE:
            now += timedelta(minutes=self.simulate_offset)
            now_utc += timedelta(minutes=self.simulate_offset)

        self.now_utc = now_utc
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = self.minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        self.minutes_to_midnight = 24 * 60 - self.minutes_now

        self.log("--------------- PredBat - update at {} with clock skew {} minutes, minutes now {}".format(now_utc, skew, self.minutes_now))

        # Check our version
        self.download_predbat_releases()
        self.expose_config("version", None)

        self.fetch_config_options()
        self.fetch_sensor_data()
        self.fetch_inverter_data()

        recompute = False
        if not scheduled or not self.plan_valid:
            self.log("Will recompute the plan as it is invalid")
            recompute = True
        else:
            plan_age = self.now_utc - self.plan_last_updated
            plan_age_minutes = plan_age.seconds / 60.0
            self.log("Plan was last updated on {} and is now {} minutes old".format(self.plan_last_updated, self.dp1(plan_age_minutes)))

        # Calculate the new plan (or re-use existing)
        self.calculate_plan(recompute=recompute)

        # Publish rate data
        self.publish_rate_and_threshold()

        # Execute the plan
        status, status_extra = self.execute_plan()

        # If the plan was not updated, and the time has expired lets update it now
        if not recompute:
            plan_age = self.now_utc - self.plan_last_updated
            plan_age_minutes = plan_age.seconds / 60.0

            if (plan_age_minutes + RUN_EVERY) > self.calculate_plan_every:
                self.log(
                    "Will recompute the plan as it is now {} minutes old and will exceed the max age of {} minutes before the next run".format(
                        self.dp1(plan_age_minutes), self.calculate_plan_every
                    )
                )
                self.calculate_plan(recompute=True)
                status, status_extra = self.execute_plan()
            else:
                self.log("Will not recompute the plan, it is {} minutes old and max age is {} minutes".format(self.dp1(plan_age_minutes), self.calculate_plan_every))

        # IBoost model update state, only on 5 minute intervals
        if self.iboost_enable and scheduled:
            if self.iboost_energy_today:
                # If we have a realtime sensor just use that data
                self.iboost_next = self.iboost_today
            elif self.minutes_now >= (23 * 60 + 30):
                # Reset after 11:30pm
                self.iboost_next = 0
            # Save next IBoost model value
            self.expose_config("iboost_today", self.iboost_next)
            self.log("IBoost model today updated to {}".format(self.iboost_next))

        # Holiday days left countdown, subtract a day at midnight every day
        if scheduled and self.holiday_days_left > 0:
            if self.minutes_now < RUN_EVERY:
                self.holiday_days_left -= 1
                self.expose_config("holiday_days_left", self.holiday_days_left)
                self.log("Holiday days left is now {}".format(self.holiday_days_left))

        if self.debug_enable:
            self.create_debug_yaml()

        if self.had_errors:
            self.log("Completed run status {} with Errors reported (check log)".format(status))
        else:
            self.log("Completed run status {}".format(status))
            self.record_status(
                status,
                debug="best_charge_limit={} best_charge_window={} best_discharge_limit= {} best_discharge_window={}".format(
                    self.charge_limit_best, self.charge_window_best, self.discharge_limits_best, self.discharge_window_best
                ),
                notify=True,
                extra=status_extra,
            )

    def update_event(self, event, data, kwargs):
        """
        Update event
        """
        self.log("Update event {} {} {}".format(event, data, kwargs))

    def download_predbat_version(self, version):
        """
        Download a version of Predbat
        """
        tag_split = version.split(" ")
        self.log("Split returns {}".format(tag_split))
        if tag_split:
            tag = tag_split[0]
            url = "https://raw.githubusercontent.com/springfall2008/batpred/" + tag + "/apps/predbat/predbat.py"
            self.log("Downloading Predbat version {} from {}".format(tag, url))
            r = requests.get(url, headers={})
            if r.ok:
                data = r.text
                new_filename = __file__ + "." + tag
                size = len(data)
                if size >= 10000:
                    self.log("Write new version {} bytes to {}".format(len(data), new_filename))
                    with open(new_filename, "w") as han:
                        han.write(data)
                    self.call_notify("Predbat: update to: {}".format(version))
                    self.log("Perform the update.....")
                    os.system("mv -f {} {}".format(new_filename, __file__))
                    return True
                else:
                    self.log("File is too small, update failed")
                    return False
            else:
                self.log("WARN: Downloading Predbat failed, URL {}".format(url))
                return False

    def select_event(self, event, data, kwargs):
        """
        Catch HA Input select updates
        """
        service_data = data.get("service_data", {})
        value = service_data.get("option", None)
        entities = service_data.get("entity_id", [])

        # Can be a string or an array
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ("entity" in item) and (item["entity"] in entities):
                entity = item["entity"]
                self.log("select_event: {}, {} = {}".format(item["name"], entity, value))
                if item["name"] == "update":
                    self.log("Calling update service for {}".format(value))
                    self.download_predbat_version(value)
                else:
                    self.expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    def number_event(self, event, data, kwargs):
        """
        Catch HA Input number updates
        """
        service_data = data.get("service_data", {})
        value = service_data.get("value", None)
        entities = service_data.get("entity_id", [])

        # Can be a string or an array
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ("entity" in item) and (item["entity"] in entities):
                entity = item["entity"]
                self.log("number_event: {} = {}".format(entity, value))
                self.expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    def switch_event(self, event, data, kwargs):
        """
        Catch HA Switch toggle
        """
        service = data.get("service", None)
        service_data = data.get("service_data", {})
        entities = service_data.get("entity_id", [])

        # Can be a string or an array
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ("entity" in item) and (item["entity"] in entities):
                value = item["value"]
                entity = item["entity"]

                if service == "turn_on":
                    value = True
                elif service == "turn_off":
                    value = False
                elif service == "toggle" and isinstance(value, bool):
                    value = not value

                self.log("switch_event: {} = {}".format(entity, value))
                self.expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    def get_ha_config(self, name, default):
        """
        Get Home assistant config value, use default if not set
        """
        item = self.config_index.get(name)
        if item:
            if item["name"] == name:
                value = item.get("value", None)
                if default is None:
                    default = item.get("default", None)
                if value is None:
                    value = default
                return value, default
        return None, default

    def expose_config(self, name, value, quiet=True, event=False):
        """
        Share the config with HA
        """
        item = self.config_index.get(name, None)
        if item:
            enabled = self.user_config_item_enabled(item)
            if not enabled:
                self.log("Not updating HA config {} to {} as disabled".format(name, value))
                item["value"] = None
            else:
                entity = item.get("entity")
                if entity and ((item.get("value") is None) or (value != item["value"])):
                    if item.get("reset_inverter", False):
                        self.inverter_needs_reset = True
                        self.log("Set reset inverter true due to reset_inverter on item {}".format(name))
                    if item.get("reset_inverter_force", False):
                        self.inverter_needs_reset = True
                        self.log("Set reset inverter true due to reset_inverter_force on item {}".format(name))
                        if event:
                            self.inverter_needs_reset_force = name
                            self.log("Set reset inverter force true due to reset_inverter_force on item {}".format(name))
                    item["value"] = value
                    if not quiet:
                        self.log("Updating HA config {} to {}".format(name, value))
                    if item["type"] == "input_number":
                        icon = item.get("icon", "mdi:numeric")
                        self.set_state(
                            entity_id=entity,
                            state=value,
                            attributes={"friendly_name": item["friendly_name"], "min": item["min"], "max": item["max"], "step": item["step"], "icon": icon},
                        )
                    elif item["type"] == "switch":
                        icon = item.get("icon", "mdi:light-switch")
                        self.set_state(entity_id=entity, state=("on" if value else "off"), attributes={"friendly_name": item["friendly_name"], "icon": icon})
                    elif item["type"] == "select":
                        icon = item.get("icon", "mdi:format-list-bulleted")
                        self.set_state(entity_id=entity, state=value, attributes={"friendly_name": item["friendly_name"], "options": item["options"], "icon": icon})
                    elif item["type"] == "update":
                        summary = self.releases.get("this_body", "")
                        latest = self.releases.get("latest", "check HACS")
                        state = "off"
                        if item["installed_version"] != latest:
                            state = "on"
                        self.set_state(
                            entity_id=entity,
                            state=state,
                            attributes={
                                "friendly_name": item["friendly_name"],
                                "title": item["title"],
                                "in_progress": False,
                                "auto_update": True,
                                "installed_version": item["installed_version"],
                                "latest_version": latest,
                                "entity_picture": item["entity_picture"],
                                "release_url": item["release_url"],
                                "skipped_version": False,
                                "release_summary": summary,
                            },
                        )

    def user_config_item_enabled(self, item):
        """
        Check if user config item is enable
        """
        enable = item.get("enable", None)
        if enable:
            citem = self.config_index.get(enable, None)
            if citem:
                enabled_value = citem.get("value", True)
                if not enabled_value:
                    return False
                else:
                    return True
            else:
                self.log("WARN: Badly formed CONFIG enable item {}, please raise a Github ticket".format(item["name"]))
        return True

    def dashboard_item(self, entity, state, attributes):
        """
        Publish state and log dashboard item
        """
        self.set_state(entity_id=entity, state=state, attributes=attributes)
        if entity not in self.dashboard_index:
            self.dashboard_index.append(entity)

    def create_debug_yaml(self):
        """
        Write out a debug info yaml
        """
        basename = "/predbat_debug.yaml"
        filename = None
        for root in CONFIG_ROOTS:
            if os.path.exists(root):
                filename = root + basename
        if filename:
            debug = {}
            debug["TIME"] = self.time_now_str()
            debug["THIS_VERSION"] = THIS_VERSION
            debug["CONFIG_ITEMS"] = CONFIG_ITEMS
            debug["args"] = self.args
            debug["charge_window_best"] = self.charge_window_best
            debug["charge_limit_best"] = self.charge_limit_best
            debug["discharge_window_best"] = self.discharge_window_best
            debug["discharge_limits_best"] = self.discharge_limits_best
            debug["low_rates"] = self.low_rates
            debug["high_export_rates"] = self.high_export_rates
            with open(filename, "w") as file:
                yaml.dump(debug, file)
            self.log("Wrote debug yaml to {}".format(filename))

    def create_entity_list(self):
        """
        Create the standard entity list
        """

        text = ""
        text += "# Predbat Dashboard - {}\n".format(THIS_VERSION)
        text += "type: entities\n"
        text += "Title: Predbat\n"
        text += "entities:\n"
        enable_list = [None]
        for item in CONFIG_ITEMS:
            enable = item.get("enable", None)
            if enable and enable not in enable_list:
                enable_list.append(enable)

        for try_enable in enable_list:
            for item in CONFIG_ITEMS:
                entity = item["entity"]
                enable = item.get("enable", None)

                if enable == try_enable and self.user_config_item_enabled(item):
                    text += "  - entity: " + entity + "\n"

        for entity in self.dashboard_index:
            text += "  - entity: " + entity + "\n"

        # Find path
        basename = "/predbat_dashboard.yaml"
        filename = None
        for root in CONFIG_ROOTS:
            if os.path.exists(root):
                filename = root + basename

        # Write
        if filename:
            han = open(filename, "w")
            if han:
                self.log("Creating predbat dashboard at {}".format(filename))
                han.write(text)
                han.close()
            else:
                self.log("Failed to write predbat dashboard to {}".format(filename))
        else:
            self.log("Failed to write predbat dashboard as can not find config root in {}".format(CONFIG_ROOTS))

    def load_user_config(self, quiet=True, register=False):
        """
        Load config from HA
        """
        self.config_index = {}

        # New install, used to set default of expert mode
        new_install = True
        current_status = self.get_state("predbat.status")
        if current_status:
            new_install = False

        # Build config index
        for item in CONFIG_ITEMS:
            name = item["name"]
            self.config_index[name] = item

            # Set the default for expert mode to False for new installs only
            if name == "expert_mode":
                if new_install:
                    item["default"] = False
            elif name == "mode":
                if new_install:
                    item["default"] = PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]

        # Find values and monitor config
        for item in CONFIG_ITEMS:
            name = item["name"]
            type = item["type"]
            enabled = self.user_config_item_enabled(item)

            entity = type + "." + self.prefix + "_" + name
            item["entity"] = entity
            ha_value = None

            if not enabled:
                if not quiet:
                    self.log("Note: Disabled configuration item {}".format(name))
                item["value"] = None

                # Remove the state if the entity still exists
                ha_value = self.get_state(entity)
                if ha_value is not None:
                    self.set_state(entity_id=entity, state=ha_value, attributes={"friendly_name": "[Disabled] " + item["friendly_name"]})
                continue

            # Get from current state?
            ha_value = self.get_state(entity)

            # Update drop down menu
            if name == "update":
                if not ha_value:
                    # Construct this version information as it's not set correctly already
                    ha_value = THIS_VERSION + " Loading..."
                else:
                    # Leave current value until it's set during version discovery later
                    continue

            # Get from history?
            if ha_value is None:
                history = self.get_history(entity_id=entity)
                if history:
                    history = history[0]
                    ha_value = history[-1]["state"]

            # Default?
            if ha_value is None:
                ha_value = item.get("default", None)

            # Switch convert to text
            if type == "switch" and isinstance(ha_value, str):
                if ha_value.lower() in ["on", "true", "enable"]:
                    ha_value = True
                else:
                    ha_value = False

            if type == "input_number" and ha_value is not None:
                try:
                    ha_value = float(ha_value)
                except (ValueError, TypeError):
                    ha_value = None

            if type == "update":
                ha_value = None

            # Push back into current state
            if ha_value is not None:
                self.expose_config(item["name"], ha_value, quiet=quiet)

        # Register HA services
        if register:
            self.fire_event("service_registered", domain="input_number", service="set_value")
            self.fire_event("service_registered", domain="input_number", service="increment")
            self.fire_event("service_registered", domain="input_number", service="decrement")
            self.fire_event("service_registered", domain="switch", service="turn_on")
            self.fire_event("service_registered", domain="switch", service="turn_off")
            self.fire_event("service_registered", domain="switch", service="toggle")
            self.fire_event("service_registered", domain="select", service="select_option")
            self.fire_event("service_registered", domain="select", service="select_first")
            self.fire_event("service_registered", domain="select", service="select_last")
            self.fire_event("service_registered", domain="select", service="select_next")
            self.fire_event("service_registered", domain="select", service="select_previous")
            self.listen_select_handle = self.listen_event(self.switch_event, event="call_service", domain="switch", service="turn_on")
            self.listen_select_handle = self.listen_event(self.switch_event, event="call_service", domain="switch", service="turn_off")
            self.listen_select_handle = self.listen_event(self.switch_event, event="call_service", domain="switch", service="toggle")
            self.listen_select_handle = self.listen_event(self.number_event, event="call_service", domain="input_number", service="set_value")
            self.listen_select_handle = self.listen_event(self.number_event, event="call_service", domain="input_number", service="increment")
            self.listen_select_handle = self.listen_event(self.number_event, event="call_service", domain="input_number", service="decrement")
            self.listen_select_handle = self.listen_event(self.select_event, event="call_service", domain="select", service="select_option")
            self.listen_select_handle = self.listen_event(self.select_event, event="call_service", domain="select", service="select_first")
            self.listen_select_handle = self.listen_event(self.select_event, event="call_service", domain="select", service="select_last")
            self.listen_select_handle = self.listen_event(self.select_event, event="call_service", domain="select", service="select_next")
            self.listen_select_handle = self.listen_event(self.select_event, event="call_service", domain="select", service="select_previous")

    def resolve_arg_re(self, arg, arg_value, state_keys):
        """
        Resolve argument regular expression on list or string
        """
        matched = True

        if isinstance(arg_value, list):
            new_list = []
            for item_value in arg_value:
                item_matched, item_value = self.resolve_arg_re(arg, item_value, state_keys)
                if not item_matched:
                    self.log("WARN: Regular argument {} expression {} failed to match - disabling this item".format(arg, item_value))
                    new_list.append(None)
                else:
                    new_list.append(item_value)
            arg_value = new_list
        elif isinstance(arg_value, str) and arg_value.startswith("re:"):
            matched = False
            my_re = "^" + arg_value[3:] + "$"
            for key in state_keys:
                res = re.search(my_re, key)
                if res:
                    if len(res.groups()) > 0:
                        self.log("Regular expression argument {} matched {} with {}".format(arg, my_re, res.group(1)))
                        arg_value = res.group(1)
                        matched = True
                        break
                    else:
                        self.log("Regular expression argument {} Matched {} with {}".format(arg, my_re, res.group(0)))
                        arg_value = res.group(0)
                        matched = True
                        break
        return matched, arg_value

    def auto_config(self):
        """
        Auto configure
        match arguments with sensors
        """

        states = self.get_state()
        state_keys = states.keys()
        disabled = []

        if 0:
            predbat_keys = []
            for key in state_keys:
                if "predbat" in str(key):
                    predbat_keys.append(key)
            predbat_keys.sort()
            self.log("Keys:\n  - entity: {}".format("\n  - entity: ".join(predbat_keys)))

        # Find each arg re to match
        for arg in self.args:
            arg_value = self.args[arg]
            matched, arg_value = self.resolve_arg_re(arg, arg_value, state_keys)
            if not matched:
                self.log("WARN: Regular expression argument: {} unable to match {}, now will disable".format(arg, arg_value))
                disabled.append(arg)
            else:
                self.args[arg] = arg_value

        # Remove unmatched keys
        for key in disabled:
            del self.args[key]

    def sanity(self):
        """
        Sanity check appdaemon setup
        """
        self.log("Sanity check:")
        config_dir = ""
        passed = True
        app_dirs = []

        for root in CONFIG_ROOTS:
            if os.path.exists(root):
                config_dir = root
                self.log("Sanity scan files in '{}' : {}".format(config_dir, os.listdir(config_dir)))
                break

        if not config_dir:
            self.log("WARN: Unable to find config directory in roots {}".format(CONFIG_ROOTS))
            passed = False
        else:
            app_dirs.append(config_dir)

        appdaemon_config = config_dir + "/appdaemon.yaml"
        if config_dir and os.path.exists(appdaemon_config):
            with open(appdaemon_config, "r") as han:
                data = None
                try:
                    data = yaml.safe_load(han)
                except yaml.YAMLError as exc:
                    self.log("ERROR: Unable to read /config/appdaemon.yaml file correctly!")
                    passed = False

                if data and ("appdaemon" in data):
                    sub_data = data["appdaemon"]
                    if "app_dir" in sub_data:
                        app_dir = sub_data["app_dir"]
                        if app_dir not in app_dirs:
                            app_dirs.append(app_dir)
                        self.log("Sanity: Got app_dir {}".format(app_dir))
                    else:
                        self.log("WARN: app_dir is not set in appdaemon.yaml")
                        passed = False
                elif data:
                    self.log("WARN: appdaemon section is missing from appdaemon.yaml")
                    passed = False
        else:
            self.log("WARN: unable to find {}".format(appdaemon_config))
            passed = False

        self.log("Sanity: Scanning app_dirs: {}".format(app_dirs))
        apps_yaml = []
        predbat_py = []
        for dir in app_dirs:
            for root, dirs, files in os.walk(dir):
                for name in files:
                    filepath = os.path.join(root, name)
                    if name == "apps.yaml":
                        self.log("Sanity: Got apps.yaml in location {}".format(filepath))
                        apps_yaml.append(filepath)
                    elif name == "predbat.py":
                        self.log("Sanity: Got predbat.py in location {}".format(filepath))
                        predbat_py.append(filepath)

        if not apps_yaml:
            self.log("WARN: Unable to find any apps.yaml files, please check your configuration")
            passed = False

        # Check apps.yaml to find predbat configuration
        validPred = 0
        for filename in apps_yaml:
            with open(filename, "r") as han:
                data = None
                try:
                    data = yaml.safe_load(han)
                except yaml.YAMLError as exc:
                    self.log("ERROR: Unable to read {} file correctly!".format(filename))
                    passed = False
                if data:
                    if "pred_bat" in data:
                        self.log("Sanity: {} is a valid pred_bat configuration".format(filename))
                        validPred += 1
        if not validPred:
            self.log("WARN: Unable to find any valid Predbat configurations")
            passed = False
        if validPred > 1:
            self.log("WARN: You have multiple valid Predbat configurations")
            passed = False

        # Check predbat.py
        if not predbat_py:
            self.log("WARN: Unable to find predbat.py, please check your configuration")
            passed = False
        elif len(predbat_py) > 1:
            self.log("WARN: Found multiple predbat.py files, please check your configuration")
            passed = False
        else:
            filename = predbat_py[0]
            foundVersion = False
            with open(filename, "r") as han:
                for line in han:
                    if "THIS_VERSION" in line:
                        res = re.search('THIS_VERSION\s+=\s+"([0-9.v]+)"', line)
                        if res:
                            version = res.group(1)
                            if version != THIS_VERSION:
                                self.log("WARN: The version in predbat.py is {} but this code is version {} - please re-start appdaemon".format(version, THIS_VERSION))
                                passed = False
                            else:
                                self.log("Sanity: Confirmed correct version {} is in predbat.py".format(version))
                                foundVersion = True
            if not foundVersion:
                self.log("WARN: Unable to find THIS_VERSION in Predbat.py file, please check your setup")
                passed = False

        if passed:
            self.log("Sanity check has passed")
        else:
            self.log("Sanity check FAILED!")
            self.record_status("WARN: Sanity startup checked has FAILED, see your logs for details")
        return passed

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        global SIMULATE
        self.log("Predbat: Startup")

        try:
            self.reset()
            self.sanity()
            self.auto_config()
            self.load_user_config(quiet=False, register=True)
        except Exception as e:
            self.log("ERROR: Exception raised {}".format(e))
            self.record_status("ERROR: Exception raised {}".format(e))
            raise e

        # Catch template configurations and exit
        if self.get_arg("template", False):
            self.log("ERROR: You still have a template configuration, please edit apps.yaml or restart AppDaemon if you just updated with HACS")
            self.record_status("ERROR: You still have a template configuration, please edit apps.yaml or restart AppDaemon if you just updated with HACS")
            return

        self.inverter_type = self.get_arg("inverter_type", "GE", indirect=False)
        self.log(f"Inverter Type: {self.inverter_type} ({INVERTER_TYPES[self.inverter_type]})")

        if SIMULATE and SIMULATE_LENGTH:
            # run once to get data
            SIMULATE = False
            self.update_pred(scheduled=False)
            soc_best = self.predict_soc_best.copy()
            self.log("Best SOC array {}".format(soc_best))
            SIMULATE = True

            skew = self.get_arg("clock_skew", 0)
            now = datetime.now() + timedelta(minutes=skew)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            minutes_now = int((now - midnight).seconds / 60)

            for offset in range(0, SIMULATE_LENGTH, 30):
                self.simulate_offset = offset + 30 - (minutes_now % 30)
                self.sim_soc_kw = soc_best[int(self.simulate_offset / 5) * 5]
                self.log(">>>>>>>>>> Simulated offset {} soc {} <<<<<<<<<<<<".format(self.simulate_offset, self.sim_soc_kw))
                self.update_pred(scheduled=True)
        else:
            # Run every N minutes aligned to the minute
            skew = self.get_arg("clock_skew", 0)
            if skew:
                self.log("WARN: Clock skew is set to {} minutes".format(skew))
            run_every = RUN_EVERY * 60
            skew = skew % (run_every / 60)
            now = datetime.now() + timedelta(minutes=skew)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_now = (now - midnight).seconds

            # Calculate next run time to exactly align with the run_every time
            seconds_offset = seconds_now % run_every
            seconds_next = seconds_now + (run_every - seconds_offset)
            next_time = midnight + timedelta(seconds=seconds_next)
            self.log("Predbat: Next run time will be {} and then every {} seconds".format(next_time, run_every))

            # First run is now
            self.update_pending = True

            # And then every N minutes
            if not INVERTER_TEST:
                self.run_every(self.run_time_loop, next_time, run_every, random_start=0, random_end=0)
                self.run_every(self.update_time_loop, datetime.now(), 15, random_start=0, random_end=0)
            else:
                self.update_time_loop(None)

            # Balance inverters
            run_every_balance = self.get_arg("balance_inverters_seconds", 60)
            if run_every_balance > 0:
                self.log("Balance inverters will run every {} seconds (if enabled)".format(run_every_balance))
                seconds_offset_balance = seconds_now % run_every_balance
                seconds_next_balance = seconds_now + (run_every_balance - seconds_offset_balance) + 15  # Offset to start after Predbat update task
                next_time_balance = midnight + timedelta(seconds=seconds_next_balance)
                self.run_every(self.run_time_loop_balance, next_time_balance, run_every_balance, random_start=0, random_end=0)

    @ad.app_lock
    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        if self.update_pending and not self.prediction_started:
            self.prediction_started = True
            self.load_user_config()
            self.update_pending = False
            try:
                self.update_pred(scheduled=False)
                self.create_entity_list()
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status("ERROR: Exception raised {}".format(e))
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False

    @ad.app_lock
    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.prediction_started:
            config_changed = False
            self.prediction_started = True
            if self.update_pending:
                self.load_user_config()
                config_changed = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=True)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status("ERROR: Exception raised {}".format(e))
                raise e
            finally:
                self.prediction_started = False
            if config_changed:
                self.create_entity_list()
            self.prediction_started = False

    def run_time_loop_balance(self, cb_args):
        """
        Called every N second for balance inverters
        """
        if not self.prediction_started and self.balance_inverters_enable and not self.set_read_only:
            try:
                self.balance_inverters()
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status("ERROR: Exception raised {}".format(e))
                raise e
