"""
Battery Prediction app
see Readme for information
"""

import copy
import os
import re
import time
import math
from datetime import datetime, timedelta

# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

# Import AppDaemon or our standalone wrapper
try:
    import adbase as ad
    import appdaemon.plugins.hass.hassapi as hass
except:
    import hass as hass

import pytz
import requests
import yaml
from multiprocessing import Pool, cpu_count, set_start_method
import asyncio
from aiohttp import web, ClientSession, WSMsgType
import json

# Only assign globals once to avoid re-creating them with processes are forked
if not "PRED_GLOBAL" in globals():
    PRED_GLOBAL = {}

THIS_VERSION = "v7.20.5"
PREDBAT_FILES = ["predbat.py"]
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
TIME_FORMAT_SOLIS = "%Y-%m-%d %H:%M:%S"
PREDICT_STEP = 5
RUN_EVERY = 5
CONFIG_ROOTS = ["/config", "/conf", "/homeassistant", "./"]
CONFIG_ROOTS_HA = ["/homeassistant", "/conf", "/config", "./"]
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"
TIMEOUT = 60 * 5
CONFIG_REFRESH_PERIOD = 60 * 8

# 240v x 100 amps x 3 phases / 1000 to kW / 60 minutes in an hour is the maximum kWh in a 1 minute period
MAX_INCREMENT = 240 * 100 * 3 / 1000 / 60
MINUTE_WATT = 60 * 1000

SIMULATE = False  # Debug option, when set don't write to entities but simulate each 30 min period
SIMULATE_LENGTH = 23 * 60  # How many periods to simulate, set to 0 for just current
INVERTER_TEST = False  # Run inverter control self test

# Create an array of times in the day in 5-minute intervals
BASE_TIME = datetime.strptime("00:00:00", "%H:%M:%S")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M:%S")) for minute in range(0, 24 * 60, 5)]

# Inverter modes
PREDBAT_MODE_OPTIONS = ["Monitor", "Control SOC only", "Control charge", "Control charge & discharge"]
PREDBAT_MODE_MONITOR = 0
PREDBAT_MODE_CONTROL_SOC = 1
PREDBAT_MODE_CONTROL_CHARGE = 2
PREDBAT_MODE_CONTROL_CHARGEDISCHARGE = 3

# Predbat update options
PREDBAT_UPDATE_OPTIONS = [f"{THIS_VERSION} Loading..."]
PREDBAT_SAVE_RESTORE = ["save current", "restore default"]

# Configuration options inside HA
CONFIG_ITEMS = [
    {
        "name": "version",
        "friendly_name": "Predbat Core Update",
        "type": "update",
        "title": "Predbat",
        "installed_version": THIS_VERSION,
        "release_url": f"https://github.com/springfall2008/batpred/releases/tag/{THIS_VERSION}",
        "entity_picture": "https://user-images.githubusercontent.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png",
        "restore": False,
        "default": False,
    },
    {
        "name": "expert_mode",
        "friendly_name": "Expert Mode",
        "type": "switch",
        "default": False,
    },
    {
        "name": "active",
        "friendly_name": "Predbat Active",
        "type": "switch",
        "default": False,
        "restore": False,
    },
    {
        "name": "pv_metric10_weight",
        "friendly_name": "Metric 10 Weight",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "*",
        "icon": "mdi:multiplication",
        "default": 0.15,
    },
    {
        "name": "pv_scaling",
        "friendly_name": "PV Scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.01,
        "unit": "*",
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
        "unit": "*",
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
        "unit": "*",
        "icon": "mdi:multiplication",
        "default": 1.1,
    },
    {
        "name": "load_scaling_saving",
        "friendly_name": "Load Scaling for saving sessions",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.01,
        "unit": "*",
        "icon": "mdi:multiplication",
        "default": 1.0,
    },
    {
        "name": "battery_rate_max_scaling",
        "friendly_name": "Battery rate max scaling charge",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "*",
        "icon": "mdi:multiplication",
        "default": 1.0,
    },
    {
        "name": "battery_rate_max_scaling_discharge",
        "friendly_name": "Battery rate max scaling discharge",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "*",
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
        "unit": "*",
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
        "unit": "*",
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
        "unit": "*",
        "icon": "mdi:call-split",
        "default": 0.04,
    },
    {
        "name": "inverter_hybrid",
        "friendly_name": "Inverter Hybrid",
        "type": "switch",
        "default": True,
    },
    {
        "name": "inverter_soc_reset",
        "friendly_name": "Inverter SOC Reset",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
    },
    {
        "name": "inverter_set_charge_before",
        "friendly_name": "Inverter Set charge window before start",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
    },
    {
        "name": "battery_capacity_nominal",
        "friendly_name": "Use the Battery Capacity Nominal size",
        "type": "switch",
        "enable": "expert_mode",
        "default": False,
    },
    {
        "name": "car_charging_energy_scale",
        "friendly_name": "Car charging energy scale",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "*",
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
        "unit": "*",
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
        "default": 0.5,
    },
    {
        "name": "metric_battery_value_scaling",
        "friendly_name": "Metric Battery Value Scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.1,
        "unit": "*",
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
        "unit": "*",
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
        "name": "metric_load_divergence_enable",
        "friendly_name": "Enable Load Divergence Model",
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
        "unit": "*",
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
        "unit": "*",
        "icon": "mdi:multiplication",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "combine_rate_threshold",
        "friendly_name": "Combine Rate Threshold",
        "type": "input_number",
        "min": 0,
        "max": 5.0,
        "step": 0.1,
        "unit": "p",
        "icon": "mdi:table-merge-cells",
        "enable": "expert_mode",
        "default": 0.0,
    },
    {
        "name": "car_charging_hold",
        "friendly_name": "Car charging hold",
        "type": "switch",
        "default": True,
        "reset_inverter": True,
    },
    {
        "name": "car_charging_manual_soc",
        "friendly_name": "Car charging manual SOC",
        "type": "switch",
        "default": False,
    },
    {
        "name": "car_charging_manual_soc_kwh",
        "friendly_name": "Car manual SOC kWh",
        "type": "input_number",
        "min": 0,
        "max": 100,
        "step": 0.01,
        "unit": "kWh",
        "icon": "mdi:ev-station",
        "enable": "car_charging_manual_soc",
        "default": 0.0,
        "restore": False,
    },
    {
        "name": "octopus_intelligent_charging",
        "friendly_name": "Octopus Intelligent Charging",
        "type": "switch",
        "default": True,
    },
    {
        "name": "octopus_intelligent_ignore_unplugged",
        "friendly_name": "Ignore Intelligent slots when car is unplugged",
        "type": "switch",
        "default": False,
        "enable": "expert_mode",
    },
    {
        "name": "car_charging_plan_smart",
        "friendly_name": "Car Charging Plan Smart",
        "type": "switch",
        "default": False,
    },
    {
        "name": "car_charging_plan_max_price",
        "friendly_name": "Car Charging Plan max price",
        "type": "input_number",
        "min": -99,
        "max": 99,
        "step": 1,
        "unit": "p",
        "icon": "mdi:ev-station",
        "default": 0,
    },
    {
        "name": "car_charging_from_battery",
        "friendly_name": "Allow car to charge from battery",
        "type": "switch",
        "default": False,
        "reset_inverter": True,
    },
    {
        "name": "calculate_discharge_oncharge",
        "friendly_name": "Calculate Discharge on charge slots",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
    },
    {
        "name": "calculate_second_pass",
        "friendly_name": "Calculate full second pass (slower)",
        "type": "switch",
        "enable": "expert_mode",
        "default": False,
    },
    {
        "name": "calculate_tweak_plan",
        "friendly_name": "Calculate tweak second pass",
        "type": "switch",
        "enable": "expert_mode",
        "default": False,
    },
    {
        "name": "calculate_secondary_order",
        "friendly_name": "Calculate secondary order slots",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
    },
    {
        "name": "calculate_inday_adjustment",
        "friendly_name": "Calculate in-day adjustment",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
    },
    {
        "name": "calculate_plan_every",
        "friendly_name": "Calculate plan every N minutes",
        "type": "input_number",
        "min": 5,
        "max": 60,
        "step": 5,
        "unit": "minutes",
        "icon": "mdi:clock-end",
        "enable": "expert_mode",
        "default": 10,
    },
    {
        "name": "combine_charge_slots",
        "friendly_name": "Combine Charge Slots",
        "type": "switch",
        "default": False,
    },
    {
        "name": "combine_discharge_slots",
        "friendly_name": "Combine Discharge Slots",
        "type": "switch",
        "enable": "expert_mode",
        "default": False,
    },
    {
        "name": "set_status_notify",
        "friendly_name": "Set Status Notify",
        "type": "switch",
        "default": True,
    },
    {
        "name": "set_inverter_notify",
        "friendly_name": "Set Inverter Notify",
        "type": "switch",
        "default": False,
    },
    {
        "name": "set_charge_freeze",
        "friendly_name": "Set Charge Freeze",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
        "reset_inverter": True,
    },
    {
        "name": "set_charge_low_power",
        "friendly_name": "Set Charge Low Power Mode",
        "type": "switch",
        "default": False,
        "reset_inverter": True,
    },
    {
        "name": "set_reserve_enable",
        "friendly_name": "Set Reserve Enable",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
        "reset_inverter": True,
    },
    {
        "name": "set_discharge_freeze_only",
        "friendly_name": "Set Discharge Freeze Only",
        "type": "switch",
        "enable": "expert_mode",
        "default": False,
        "reset_inverter": True,
    },
    {
        "name": "set_discharge_during_charge",
        "friendly_name": "Set Discharge During Charge",
        "type": "switch",
        "default": True,
    },
    {
        "name": "set_read_only",
        "friendly_name": "Read Only mode",
        "type": "switch",
        "default": False,
        "reset_inverter_force": True,
    },
    {
        "name": "balance_inverters_enable",
        "friendly_name": "Balance Inverters Enable (Beta)",
        "type": "switch",
        "default": False,
    },
    {
        "name": "balance_inverters_charge",
        "friendly_name": "Balance Inverters for charging",
        "type": "switch",
        "enable": "balance_inverters_enable",
        "default": True,
    },
    {
        "name": "balance_inverters_discharge",
        "friendly_name": "Balance Inverters for discharge",
        "type": "switch",
        "enable": "balance_inverters_enable",
        "default": True,
    },
    {
        "name": "balance_inverters_crosscharge",
        "friendly_name": "Balance Inverters for cross-charging",
        "type": "switch",
        "enable": "balance_inverters_enable",
        "default": True,
    },
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
    {
        "name": "debug_enable",
        "friendly_name": "Debug Enable",
        "type": "switch",
        "icon": "mdi:bug-outline",
        "default": False,
    },
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
        "restore": False,
    },
    {
        "name": "manual_charge",
        "friendly_name": "Manual force charge",
        "type": "select",
        "options": ["off"],
        "icon": "mdi:state-machine",
        "default": "off",
        "restore": False,
        "manual": True,
    },
    {
        "name": "manual_discharge",
        "friendly_name": "Manual force discharge",
        "type": "select",
        "options": ["off"],
        "icon": "mdi:state-machine",
        "default": "off",
        "restore": False,
        "manual": True,
    },
    {
        "name": "manual_idle",
        "friendly_name": "Manual force idle",
        "type": "select",
        "options": ["off"],
        "icon": "mdi:state-machine",
        "default": "off",
        "restore": False,
        "manual": True,
    },
    {
        "name": "manual_freeze_charge",
        "friendly_name": "Manual force charge freeze",
        "type": "select",
        "options": ["off"],
        "icon": "mdi:state-machine",
        "default": "off",
        "restore": False,
        "manual": True,
    },
    {
        "name": "manual_freeze_discharge",
        "friendly_name": "Manual force discharge freeze",
        "type": "select",
        "options": ["off"],
        "icon": "mdi:state-machine",
        "default": "off",
        "restore": False,
        "manual": True,
    },
    {
        "name": "saverestore",
        "friendly_name": "Save/restore settings",
        "type": "select",
        "options": PREDBAT_SAVE_RESTORE,
        "icon": "mdi:state-machine",
        "default": "",
        "restore": False,
    },
    {
        "name": "auto_update",
        "friendly_name": "Predbat automatic update enable",
        "type": "switch",
        "default": False,
    },
    {
        "name": "load_filter_modal",
        "friendly_name": "Apply modal filter historical load",
        "type": "switch",
        "enable": "expert_mode",
        "default": True,
    },
    {
        "name": "iboost_enable",
        "friendly_name": "iBoost enable",
        "type": "switch",
        "default": False,
    },
    {
        "name": "carbon_enable",
        "friendly_name": "Carbon enable",
        "type": "switch",
        "default": False,
    },
    {
        "name": "carbon_metric",
        "friendly_name": "Carbon Metric",
        "type": "input_number",
        "min": 0,
        "max": 500,
        "step": 1,
        "unit": "p/Kg",
        "icon": "mdi:molecule-co2",
        "default": 0,
        "enable": "carbon_enable",
    },
    {
        "name": "iboost_solar",
        "friendly_name": "iBoost on solar power",
        "type": "switch",
        "default": True,
    },
    {
        "name": "iboost_gas",
        "friendly_name": "iBoost when electricity cheaper than gas",
        "type": "switch",
        "default": False,
    },
    {
        "name": "iboost_charging",
        "friendly_name": "iBoost when battery charging",
        "type": "switch",
        "default": False,
    },
    {
        "name": "iboost_gas_scale",
        "friendly_name": "iBoost gas price scaling",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.1,
        "unit": "*",
        "icon": "mdi:multiplication",
        "enable": "iboost_enable",
        "default": 1.0,
    },
    {
        "name": "iboost_max_energy",
        "friendly_name": "iBoost max energy",
        "type": "input_number",
        "min": 0,
        "max": 20,
        "step": 0.1,
        "unit": "kWh",
        "enable": "iboost_enable",
        "default": 3.0,
    },
    {
        "name": "iboost_today",
        "friendly_name": "iBoost today",
        "type": "input_number",
        "min": 0,
        "max": 5,
        "step": 0.1,
        "unit": "kWh",
        "enable": "iboost_enable",
        "default": 0.0,
    },
    {
        "name": "iboost_max_power",
        "friendly_name": "iBoost max power",
        "type": "input_number",
        "min": 0,
        "max": 3500,
        "step": 100,
        "unit": "W",
        "enable": "iboost_enable",
        "default": 2400,
        "restore": False,
    },
    {
        "name": "iboost_min_power",
        "friendly_name": "iBoost min power",
        "type": "input_number",
        "min": 0,
        "max": 3500,
        "step": 100,
        "unit": "W",
        "enable": "iboost_enable",
        "default": 500,
    },
    {
        "name": "iboost_min_soc",
        "friendly_name": "iBoost min soc",
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
        "restore": False,
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
    {
        "name": "plan_debug",
        "friendly_name": "HTML Plan Debug",
        "type": "switch",
        "default": False,
        "enable": "expert_mode",
    },
]

"""
GE Inverters are the default but not all inverters have the same parameters so this constant
maps the parameters that are different between brands.

The approach is to attempt to mimic the GE model with dummy entities in HA so that predbat GE
code can be used with minimal modification.
"""
INVERTER_DEF = {
    "GE": {
        "name": "GivEnergy",
        "has_rest_api": True,
        "has_mqtt_api": False,
        "has_service_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": True,
        "has_discharge_enable_time": False,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "has_timed_pause": True,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "kWh",
        "num_load_entities": 1,
        "has_ge_inverter_mode": True,
        "time_button_press": False,
        "clock_time_format": "%H:%M:%S",
        "write_and_poll_sleep": 10,
        "has_time_window": True,
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "GEC": {
        "name": "GivEnergy Cloud",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "has_timed_pause": True,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "kWh",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%H:%M:%S",
        "write_and_poll_sleep": 10,
        "has_time_window": True,
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "GEE": {
        "name": "GivEnergy EMC",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "has_timed_pause": False,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "kWh",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%H:%M:%S",
        "write_and_poll_sleep": 10,
        "has_time_window": True,
        "support_charge_freeze": True,
        "support_discharge_freeze": False,
        "has_idle_time": True,
        "can_span_midnight": False,
    },
    "GS": {
        "name": "Ginlong Solis",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": False,
        "output_charge_control": "current",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "charge_time_format": "H M",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 2,
        "has_ge_inverter_mode": False,
        "time_button_press": True,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": True,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "SE": {
        "name": "SolarEdge",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": True,
        "output_charge_control": "power",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "SX4": {
        "name": "Solax Gen4 (Modbus Power Control)",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": True,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "SF": {
        "name": "Sofar HYD",
        "has_rest_api": False,
        "has_mqtt_api": True,
        "has_service_api": False,
        "output_charge_control": "none",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "HU": {
        "name": "Huawei Solar",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": True,
        "output_charge_control": "power",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
    "SK": {
        "name": "Sunsynk",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "has_service_api": True,
        "output_charge_control": "current",
        "current_dp": 0,
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": True,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "charge_time_format": "S",
        "charge_time_entity_is_option": False,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "time_button_press": False,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 5,
        "has_time_window": False,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": True,
    },
}

# Control modes for Solax inverters
SOLAX_SOLIS_MODES = {
    "Selfuse - No Grid Charging": 1,
    "Timed Charge/Discharge - No Grid Charging": 3,
    "Backup/Reserve - No Grid Charging": 17,
    "Selfuse": 33,
    "Timed Charge/Discharge": 35,
    "Off-Grid Mode": 37,
    "Battery Awaken": 41,
    "Battery Awaken + Timed Charge/Discharge": 43,
    "Backup/Reserve - No Timed Charge/Discharge": 49,
    "Backup/Reserve": 51,
    "Feed-in priority - No Grid Charging": 64,
    "Feed-in priority - No Timed Charge/Discharge": 96,
    "Feed-in priority": 98,
}
# New modes are from 2024.03.2 controlled with solax_modbus_new in apps.yaml
SOLAX_SOLIS_MODES_NEW = {
    "Self-Use - No Grid Charging": 1,
    "Timed Charge/Discharge - No Grid Charging": 3,
    "Backup/Reserve - No Grid Charging": 17,
    "Self-Use - No Timed Charge/Discharge": 33,
    "Self-Use": 35,
    "Off-Grid Mode": 37,
    "Battery Awaken": 41,
    "Battery Awaken + Timed Charge/Discharge": 43,
    "Backup/Reserve - No Timed Charge/Discharge": 49,
    "Backup/Reserve": 51,
    "Feed-in priority - No Grid Charging": 64,
    "Feed-in priority - No Timed Charge/Discharge": 96,
    "Feed-in priority": 98,
}


def remove_intersecting_windows(charge_limit_best, charge_window_best, discharge_limit_best, discharge_window_best):
    """
    Filters and removes intersecting charge windows
    """
    clip_again = True

    # For each charge window
    while clip_again:
        clip_again = False
        new_limit_best = []
        new_window_best = []
        for window_n in range(len(charge_limit_best)):
            window = charge_window_best[window_n]
            start = window["start"]
            end = window["end"]
            average = window["average"]
            limit = charge_limit_best[window_n]
            clipped = False

            # For each discharge window
            for dwindow_n in range(len(discharge_limit_best)):
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


def calc_percent_limit(charge_limit, soc_max):
    """
    Calculate a charge limit in percent
    """
    if isinstance(charge_limit, list):
        if soc_max <= 0:
            return [0 for i in range(len(charge_limit))]
        else:
            return [min(int((float(charge_limit[i]) / soc_max * 100.0) + 0.5), 100) for i in range(len(charge_limit))]
    else:
        if soc_max <= 0:
            return 0
        else:
            return min(int((float(charge_limit) / soc_max * 100.0) + 0.5), 100)


def get_charge_rate_curve(model, soc, charge_rate_setting):
    """
    Compute true charging rate from SOC and charge rate setting
    """
    soc_percent = calc_percent_limit(soc, model.soc_max)
    max_charge_rate = model.battery_rate_max_charge * model.battery_charge_power_curve.get(soc_percent, 1.0) * model.battery_rate_max_scaling
    return max(min(charge_rate_setting, max_charge_rate), model.battery_rate_min)


def get_discharge_rate_curve(model, soc, discharge_rate_setting):
    """
    Compute true discharging rate from SOC and charge rate setting
    """
    soc_percent = calc_percent_limit(soc, model.soc_max)
    max_discharge_rate = model.battery_rate_max_discharge * model.battery_discharge_power_curve.get(soc_percent, 1.0) * model.battery_rate_max_scaling_discharge
    return max(min(discharge_rate_setting, max_discharge_rate), model.battery_rate_min)


def find_charge_rate(model, minutes_now, soc, window, target_soc, max_rate, quiet=True):
    """
    Find the lowest charge rate that fits the charge slow
    """
    margin = 10
    if model.set_charge_low_power:
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
        rate_w = max_rate * MINUTE_WATT
        best_rate = max_rate
        while rate_w >= 400:
            rate = rate_w / MINUTE_WATT
            if rate >= min_rate:
                charge_now = soc
                minute = 0
                for minute in range(0, minutes_left, PREDICT_STEP):
                    rate_scale = get_charge_rate_curve(model, charge_now, rate)
                    charge_amount = rate_scale * PREDICT_STEP * model.battery_loss
                    charge_now += charge_amount
                    if charge_now >= target_soc:
                        best_rate = rate
                        break
            rate_w -= 125.0
        return best_rate
    else:
        return max_rate


"""
Used to mimic threads when they are disabled
"""


class DummyThread:
    def __init__(self, result):
        """
        Store the data into the class
        """
        self.result = result

    def get(self):
        """
        Return the result
        """
        return self.result


def wrapped_run_prediction_single(charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_single(charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step)


def wrapped_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record)


def wrapped_run_prediction_discharge(this_discharge_limit, start, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_discharge(this_discharge_limit, start, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record)


class Prediction:
    """
    Class to hold prediction input and output data and the run function
    """

    def __init__(self, base=None, pv_forecast_minute_step=None, pv_forecast_minute10_step=None, load_minutes_step=None, load_minutes_step10=None):
        global PRED_GLOBAL
        if base:
            self.minutes_now = base.minutes_now
            self.log = base.log
            self.forecast_minutes = base.forecast_minutes
            self.midnight_utc = base.midnight_utc
            self.soc_kw = base.soc_kw
            self.soc_max = base.soc_max
            self.export_today_now = base.export_today_now
            self.import_today_now = base.import_today_now
            self.load_minutes_now = base.load_minutes_now
            self.pv_today_now = base.pv_today_now
            self.iboost_today = base.iboost_today
            self.charge_rate_now = base.charge_rate_now
            self.discharge_rate_now = base.discharge_rate_now
            self.cost_today_sofar = base.cost_today_sofar
            self.carbon_today_sofar = base.carbon_today_sofar
            self.debug_enable = base.debug_enable
            self.num_cars = base.num_cars
            self.car_charging_soc = base.car_charging_soc
            self.car_charging_soc_next = base.car_charging_soc_next
            self.car_charging_loss = base.car_charging_loss
            self.reserve = base.reserve
            self.metric_standing_charge = base.metric_standing_charge
            self.set_charge_freeze = base.set_charge_freeze
            self.set_reserve_enable = base.set_reserve_enable
            self.set_discharge_freeze = base.set_discharge_freeze
            self.set_discharge_freeze_only = base.set_discharge_freeze_only
            self.set_discharge_during_charge = base.set_discharge_during_charge
            self.set_read_only = base.set_read_only
            self.set_charge_low_power = base.set_charge_low_power
            self.car_charging_slots = base.car_charging_slots
            self.car_charging_limit = base.car_charging_limit
            self.car_charging_from_battery = base.car_charging_from_battery
            self.iboost_enable = base.iboost_enable
            self.carbon_enable = base.carbon_enable
            self.iboost_next = base.iboost_next
            self.iboost_max_energy = base.iboost_max_energy
            self.iboost_gas = base.iboost_gas
            self.iboost_gas_scale = base.iboost_gas_scale
            self.iboost_max_power = base.iboost_max_power
            self.iboost_min_power = base.iboost_min_power
            self.iboost_min_soc = base.iboost_min_soc
            self.iboost_solar = base.iboost_solar
            self.iboost_charging = base.iboost_charging
            self.iboost_running = base.iboost_running
            self.inverter_loss = base.inverter_loss
            self.inverter_hybrid = base.inverter_hybrid
            self.inverter_limit = base.inverter_limit
            self.export_limit = base.export_limit
            self.battery_rate_min = base.battery_rate_min
            self.battery_rate_max_charge = base.battery_rate_max_charge
            self.battery_rate_max_discharge = base.battery_rate_max_discharge
            self.battery_rate_max_charge_scaled = base.battery_rate_max_charge_scaled
            self.battery_rate_max_discharge_scaled = base.battery_rate_max_discharge_scaled
            self.battery_charge_power_curve = base.battery_charge_power_curve
            self.battery_discharge_power_curve = base.battery_discharge_power_curve
            self.battery_rate_max_scaling = base.battery_rate_max_scaling
            self.battery_rate_max_scaling_discharge = base.battery_rate_max_scaling_discharge
            self.battery_loss = base.battery_loss
            self.battery_loss_discharge = base.battery_loss_discharge
            self.best_soc_keep = base.best_soc_keep
            self.car_charging_battery_size = base.car_charging_battery_size
            self.rate_gas = base.rate_gas
            self.rate_import = base.rate_import
            self.rate_export = base.rate_export
            self.pv_forecast_minute_step = pv_forecast_minute_step
            self.pv_forecast_minute10_step = pv_forecast_minute10_step
            self.load_minutes_step = load_minutes_step
            self.load_minutes_step10 = load_minutes_step10
            self.carbon_intensity = base.carbon_intensity

            # Store this dictionary in global so we can reconstruct it in the thread without passing the data
            PRED_GLOBAL["dict"] = self.__dict__.copy()

    def thread_run_prediction_single(self, charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step):
        """
        Run single prediction in a thread
        """
        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record=end_record, step=step
        )
        return (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g)

    def thread_run_prediction_charge(self, try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """

        try_charge_limit = charge_limit.copy()
        if all_n:
            for set_n in all_n:
                try_charge_limit[set_n] = try_soc
        else:
            try_charge_limit[window_n] = try_soc

        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            try_charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record=end_record
        )
        min_soc = 0
        max_soc = self.soc_max
        if not all_n:
            window = charge_window[window_n]
            predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
            if (predict_minute_start in self.predict_soc) and (predict_minute_end in self.predict_soc):
                min_soc = min(self.predict_soc[predict_minute_start], self.predict_soc[predict_minute_end])
            if (predict_minute_start in self.predict_soc) and (predict_minute_end in self.predict_soc):
                max_soc = max(self.predict_soc[predict_minute_start], self.predict_soc[predict_minute_end])

        return (
            cost,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
            min_soc,
            max_soc,
        )

    def thread_run_prediction_discharge(self, this_discharge_limit, start, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """
        # Store try value into the window
        if all_n:
            for window_id in all_n:
                discharge_limits[window_id] = this_discharge_limit
        else:
            discharge_limits[window_n] = this_discharge_limit
            # Adjust start
            window = discharge_window[window_n]
            start = min(start, window["end"] - 5)
            discharge_window[window_n]["start"] = start

        metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record=end_record
        )
        return metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g

    def find_charge_window_optimised(self, charge_windows):
        """
        Takes in an array of charge windows
        Returns a dictionary defining for each minute that is in the charge window will contain the window number
        """
        charge_window_optimised = {}
        for window_n in range(len(charge_windows)):
            for minute in range(charge_windows[window_n]["start"], charge_windows[window_n]["end"], PREDICT_STEP):
                charge_window_optimised[minute] = window_n
        return charge_window_optimised

    def in_car_slot(self, minute):
        """
        Is the given minute inside a car slot
        """
        load_amount = [0 for car_n in range(self.num_cars)]

        for car_n in range(self.num_cars):
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

    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, save=None, step=PREDICT_STEP):
        """
        Run a prediction scenario given a charge limit, return the results
        """

        # Fetch data from globals, optimised away from class to avoid passing it between threads
        if pv10:
            pv_forecast_minute_step = self.pv_forecast_minute10_step
            load_minutes_step = self.load_minutes_step10
        else:
            pv_forecast_minute_step = self.pv_forecast_minute_step
            load_minutes_step = self.load_minutes_step
        rate_gas = self.rate_gas
        rate_import = self.rate_import
        rate_export = self.rate_export

        # Data structures creating during the prediction
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.predict_metric_best = {}
        self.predict_iboost_best = {}
        self.predict_carbon_best = {}

        predict_export = {}
        predict_battery_power = {}
        predict_battery_cycle = {}
        predict_soc_time = {}
        predict_car_soc_time = [{} for car_n in range(self.num_cars)]
        predict_pv_power = {}
        predict_state = {}
        predict_grid_power = {}
        predict_load_power = {}
        predict_iboost = {}
        predict_carbon_g = {}
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
        carbon_g = self.carbon_today_sofar
        battery_cycle = 0
        metric_keep = 0
        four_hour_rule = True
        final_export_kwh = export_kwh
        final_import_kwh = import_kwh
        final_load_kwh = load_kwh
        final_pv_kwh = pv_kwh
        final_iboost_kwh = iboost_today_kwh
        final_import_kwh_house = import_kwh_house
        final_import_kwh_battery = import_kwh_battery
        final_battery_cycle = battery_cycle
        final_metric_keep = metric_keep
        final_carbon_g = carbon_g
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

        # Remove intersecting windows and optimise the data format of the charge/discharge window
        charge_limit, charge_window = remove_intersecting_windows(charge_limit, charge_window, discharge_limits, discharge_window)
        charge_window_optimised = self.find_charge_window_optimised(charge_window)
        discharge_window_optimised = self.find_charge_window_optimised(discharge_window)

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        record = True

        # Simulate each forward minute
        while minute < self.forecast_minutes:
            # Minute yesterday can wrap if days_previous is only 1
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
            prev_soc = soc
            reserve_expected = self.reserve

            # Once a force discharge is set the four hour rule is disabled
            if four_hour_rule:
                keep_minute_scaling = min((minute / (4 * 60)), 1.0) * 0.5
            else:
                keep_minute_scaling = 0.5

            # Find charge & discharge windows
            charge_window_n = charge_window_optimised.get(minute_absolute, -1)
            discharge_window_n = discharge_window_optimised.get(minute_absolute, -1)

            # Find charge limit
            charge_limit_n = 0
            if charge_window_n >= 0:
                charge_limit_n = charge_limit[charge_window_n]
                if charge_limit_n == 0:
                    charge_window_n = -1
                else:
                    if self.set_charge_freeze and (charge_limit_n == self.reserve):
                        # Charge freeze via reserve
                        charge_limit_n = soc

                    # When set reserve enable is on pretend the reserve is the charge limit minus the
                    # minimum battery rate modelled as it can leak a little
                    if self.set_reserve_enable:
                        reserve_expected = max(charge_limit_n - self.battery_rate_min * step, self.reserve)

            # Outside the recording window?
            if minute >= end_record and record:
                record = False

            # Store data before the next simulation step to align timestamps
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if self.debug_enable or save:
                predict_soc_time[stamp] = round(soc, 3)
                metric_time[stamp] = round(metric, 3)
                load_kwh_time[stamp] = round(load_kwh, 3)
                pv_kwh_time[stamp] = round(pv_kwh, 2)
                import_kwh_time[stamp] = round(import_kwh, 2)
                export_kwh_time[stamp] = round(export_kwh, 2)
                for car_n in range(self.num_cars):
                    predict_car_soc_time[car_n][stamp] = round(car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0, 2)
                predict_iboost[stamp] = iboost_today_kwh
                record_time[stamp] = 0 if record else self.soc_max

            # Save Soc prediction data as minutes for later use
            self.predict_soc[minute] = round(soc, 3)
            if save and save == "best":
                self.predict_soc_best[minute] = round(soc, 3)
                self.predict_metric_best[minute] = round(metric, 3)
                self.predict_iboost_best[minute] = iboost_today_kwh
                self.predict_carbon_best[minute] = carbon_g

            # Add in standing charge, only for the final plan when we save the results
            if (minute_absolute % (24 * 60)) < step and (save in ["best", "base", "base10", "best10"]):
                metric += self.metric_standing_charge

            # Get load and pv forecast, total up for all values in the step
            pv_now = 0
            load_yesterday = 0
            for offset in range(0, step, PREDICT_STEP):
                pv_now += pv_forecast_minute_step.get(minute + offset, 0)
                load_yesterday += load_minutes_step.get(minute + offset, 0)

            # Count PV kWh
            pv_kwh += pv_now
            if record:
                final_pv_kwh = pv_kwh

            # Simulate car charging
            car_load = self.in_car_slot(minute_absolute)

            # Car charging?
            car_freeze = False
            for car_n in range(self.num_cars):
                if car_load[car_n] > 0.0:
                    car_load_scale = car_load[car_n] * step / 60.0
                    car_load_scale = car_load_scale * self.car_charging_loss
                    car_load_scale = max(min(car_load_scale, self.car_charging_limit[car_n] - car_soc[car_n]), 0)
                    car_soc[car_n] = round(car_soc[car_n] + car_load_scale, 3)
                    load_yesterday += car_load_scale / self.car_charging_loss
                    # Model not allowing the car to charge from the battery
                    if not self.car_charging_from_battery:
                        discharge_rate_now = self.battery_rate_min  # 0
                        car_freeze = True

            # Reset modelled discharge rate if no car is charging
            if not self.car_charging_from_battery and not car_freeze:
                discharge_rate_now = self.battery_rate_max_discharge

            # IBoost solar diverter on load, don't do on discharge
            iboost_amount = 0
            if self.iboost_enable and (discharge_window_n < 0):
                if iboost_today_kwh < self.iboost_max_energy:
                    if self.iboost_gas:
                        if rate_gas:
                            # iBoost on cheap electric rates
                            gas_rate = rate_gas.get(minute_absolute, 99) * self.iboost_gas_scale
                            electric_rate = rate_import.get(minute_absolute, 0)
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

            # iBoost solar diverter model
            if self.iboost_enable:
                if iboost_today_kwh < self.iboost_max_energy and (
                    self.iboost_solar and pv_dc > (self.iboost_min_power * step) and ((soc * 100.0 / self.soc_max) >= self.iboost_min_soc)
                ):
                    iboost_amount = min(pv_dc, self.iboost_max_power * step)
                    pv_dc -= iboost_amount

                # Cumulative energy
                iboost_today_kwh += iboost_amount

                # Model iboost reset
                if (minute_absolute % (24 * 60)) == ((24 * 60) - step):
                    iboost_today_kwh = 0

                # Save iBoost next prediction
                if minute == 0 and save == "best":
                    scaled_boost = (iboost_amount / step) * RUN_EVERY
                    self.iboost_next = round(self.iboost_today + scaled_boost, 3)
                    if iboost_amount > 0:
                        self.iboost_running = True
                    else:
                        self.iboost_running = False

            # discharge freeze, reset charge rate by default
            if self.set_discharge_freeze:
                charge_rate_now = self.battery_rate_max_charge
                # Freeze mode
                if (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0:
                    if self.set_discharge_freeze_only or ((soc - step * self.battery_rate_max_discharge_scaled) < (self.soc_max * discharge_limits[discharge_window_n] / 100.0)):
                        charge_rate_now = self.battery_rate_min  # 0

            # Set discharge during charge?
            if not self.set_discharge_during_charge:
                if (charge_window_n >= 0) and ((soc >= charge_limit_n) or not self.set_reserve_enable):
                    discharge_rate_now = self.battery_rate_min  # 0
                elif not car_freeze:
                    # Reset discharge rate
                    discharge_rate_now = self.battery_rate_max_discharge

            # Battery behaviour
            battery_draw = 0
            charge_rate_now_curve = get_charge_rate_curve(self, soc, charge_rate_now)
            discharge_rate_now_curve = get_discharge_rate_curve(self, soc, discharge_rate_now)
            battery_to_min = max(soc - self.reserve, 0) * self.battery_loss_discharge * self.inverter_loss
            battery_to_max = max(self.soc_max - soc, 0) * self.battery_loss * self.inverter_loss

            if (
                not self.set_discharge_freeze_only
                and (discharge_window_n >= 0)
                and discharge_limits[discharge_window_n] < 100.0
                and (soc - step * self.battery_rate_max_discharge_scaled) > (self.soc_max * discharge_limits[discharge_window_n] / 100.0)
            ):
                # Discharge enable
                discharge_rate_now = self.battery_rate_max_discharge_scaled  # Assume discharge becomes enabled here
                discharge_rate_now_curve = get_discharge_rate_curve(self, soc, discharge_rate_now)

                # It's assumed if SOC hits the expected reserve then it's terminated
                reserve_expected = max((self.soc_max * discharge_limits[discharge_window_n]) / 100.0, self.reserve)
                battery_draw = min(discharge_rate_now_curve * step, battery_to_min)

                # Account for export limit, clip battery draw if possible to avoid going over
                diff_tmp = load_yesterday - (battery_draw + pv_dc + pv_ac)
                if diff_tmp < 0 and abs(diff_tmp) > (self.export_limit * step):
                    above_limit = abs(diff_tmp + self.export_limit * step)
                    battery_draw = max(-charge_rate_now_curve * step, battery_draw - above_limit)

                # Account for inverter limit, clip battery draw if possible to avoid going over
                if self.inverter_hybrid and diff_tmp < 0 and abs(diff_tmp) > (self.inverter_limit * step):
                    above_limit = abs(diff_tmp + self.inverter_limit * step)
                    battery_draw = max(-charge_rate_now_curve * step, battery_draw - above_limit)

                # If the battery is charging then solar will be used to charge as a priority
                # So move more of the PV into PV DC
                if battery_draw < 0 and pv_dc < abs(battery_draw):
                    extra_pv = min(abs(battery_draw) - pv_dc, pv_ac)
                    # Clamp to remaining energy to charge limit
                    if (extra_pv + pv_dc) > (charge_limit_n - soc):
                        extra_pv = max((charge_limit_n - soc) - pv_dc, 0)
                    pv_ac -= extra_pv
                    pv_dc += extra_pv

                battery_state = "f-"

                # Once force discharge starts the four hour rule is disabled
                four_hour_rule = False
            elif (charge_window_n >= 0) and soc < charge_limit_n:
                # Charge enable
                if save in ["best", "best10"]:
                    # Only tune charge rate on final plan not every simulation
                    charge_rate_now = (
                        find_charge_rate(self, minute_absolute, soc, charge_window[charge_window_n], charge_limit_n, self.battery_rate_max_charge) * self.battery_rate_max_scaling
                    )
                else:
                    charge_rate_now = self.battery_rate_max_charge  # Assume charge becomes enabled here

                # Apply the charging curve
                charge_rate_now_curve = get_charge_rate_curve(self, soc, charge_rate_now)

                # If the battery is charging then solar will be used to charge as a priority
                # So move more of the PV into PV DC
                if pv_dc < charge_rate_now_curve * step:
                    extra_pv = min(charge_rate_now_curve * step - pv_dc, pv_ac)
                    # Clamp to remaining energy to charge limit
                    if (extra_pv + pv_dc) > (charge_limit_n - soc):
                        extra_pv = max((charge_limit_n - soc) - pv_dc, 0)
                    pv_ac -= extra_pv
                    pv_dc += extra_pv

                # Remove inverter loss as it will be added back in again when calculating the SOC change
                charge_rate_now_curve /= self.inverter_loss
                battery_draw = -max(min(charge_rate_now_curve * step, charge_limit_n - soc), 0, -battery_to_max)
                battery_state = "f+"
                first_charge = min(first_charge, minute)
            else:
                # ECO Mode
                if load_yesterday - pv_ac - pv_dc > 0:
                    battery_draw = min(load_yesterday - pv_ac - pv_dc, discharge_rate_now_curve * step, self.inverter_limit * step - pv_ac, battery_to_min)
                    battery_state = "e-"
                else:
                    battery_draw = max(load_yesterday - pv_ac - pv_dc, -charge_rate_now_curve * step, -battery_to_max)
                    if battery_draw < 0:
                        battery_state = "e+"
                    else:
                        battery_state = "e~"

            # Account for inverter limit, clip battery draw if possible to avoid going over
            if self.inverter_hybrid:
                total_inverted = pv_ac + max(pv_dc + battery_draw, 0)
            else:
                total_inverted = pv_ac + pv_dc + abs(battery_draw)

            if total_inverted > self.inverter_limit * step:
                reduce_by = total_inverted - (self.inverter_limit * step)
                if battery_draw < 0:
                    pv_ac -= reduce_by
                    if not self.inverter_hybrid and pv_ac < 0:
                        pv_dc = max(pv_dc + pv_ac, 0)
                        pv_ac = 0
                else:
                    battery_draw = max(0, battery_draw - reduce_by)

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

                battery_draw = battery_draw_ac + battery_draw_dc

            # Rounding on SOC
            soc = round(soc, 6)

            # Count battery cycles
            battery_cycle = round(battery_cycle + abs(battery_draw), 4)

            # Work out left over energy after battery adjustment
            diff = round(load_yesterday - (battery_draw + pv_dc + pv_ac), 6)

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
                    metric_keep += rate_import[minute_absolute] * battery_draw * keep_minute_scaling
            elif soc < self.best_soc_keep:
                # It seems odd but the reason to add in metric keep when the battery is empty because otherwise you weight an empty battery quite heavily
                # and end up forcing it all to zero
                keep_diff = load_yesterday - (0 + pv_dc + pv_ac)
                if keep_diff > 0:
                    metric_keep += rate_import[minute_absolute] * keep_diff * keep_minute_scaling

            if diff > 0:
                # Import
                # All imports must go to home (no inverter loss) or to the battery (inverter loss accounted before above)
                import_kwh += diff

                if self.carbon_enable:
                    carbon_g += diff * self.carbon_intensity.get(minute, 0)

                if charge_window_n >= 0:
                    # If the battery is on charge anyhow then imports are at battery charging rate
                    import_kwh_battery += diff
                else:
                    # self.log("importing to minute %s amount %s kW total %s kWh total draw %s" % (minute, energy, import_kwh_house, diff))
                    import_kwh_house += diff

                metric += rate_import[minute_absolute] * diff
                grid_state = "<"
            else:
                # Export
                energy = -diff
                export_kwh += energy
                if self.carbon_enable:
                    carbon_g -= energy * self.carbon_intensity.get(minute, 0)

                if minute_absolute in rate_export:
                    metric -= rate_export[minute_absolute] * energy
                if diff != 0:
                    grid_state = ">"
                else:
                    grid_state = "~"

            # Rounding for next stage
            metric = round(metric, 4)
            import_kwh_battery = round(import_kwh_battery, 6)
            import_kwh_house = round(import_kwh_house, 6)
            export_kwh = round(export_kwh, 6)

            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = min(minute, minute_left)

            # Record final soc & metric
            if record:
                final_soc = soc
                for car_n in range(self.num_cars):
                    final_car_soc[car_n] = round(car_soc[car_n], 3)
                    if minute == 0:
                        # Next car SOC
                        self.car_charging_soc_next[car_n] = round(car_soc[car_n], 3)

                final_metric = metric
                final_import_kwh = import_kwh
                final_import_kwh_battery = import_kwh_battery
                final_import_kwh_house = import_kwh_house
                final_export_kwh = export_kwh
                final_iboost_kwh = iboost_today_kwh
                final_battery_cycle = battery_cycle
                final_metric_keep = metric_keep
                final_carbon_g = carbon_g

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
            if self.debug_enable or save:
                predict_state[stamp] = "g" + grid_state + "b" + battery_state
                predict_battery_power[stamp] = round(battery_draw * (60 / step), 3)
                predict_battery_cycle[stamp] = round(battery_cycle, 3)
                predict_pv_power[stamp] = round((pv_forecast_minute_step[minute] + pv_forecast_minute_step.get(minute + step, 0)) * (30 / step), 3)
                predict_grid_power[stamp] = round(diff * (60 / step), 3)
                predict_load_power[stamp] = round(load_yesterday * (60 / step), 3)
                if self.carbon_enable:
                    predict_carbon_g[stamp] = round(carbon_g, 3)

            # if save == "best" and self.debug_enable:
            #    self.log("Best plan, minute {} soc {} charge_limit_n {} battery_cycle {} metric {} metric_keep {} soc_min {} diff {} import_battery {} import_house {} export {}".format(minute, soc, charge_limit_n, battery_cycle, metric, metric_keep, soc_min, diff, import_kwh_battery, import_kwh_house, export_kwh))
            minute += step

        hours_left = minute_left / 60.0

        self.hours_left = hours_left
        self.final_car_soc = final_car_soc
        self.predict_car_soc_time = predict_car_soc_time
        self.final_soc = final_soc
        self.final_metric = final_metric
        self.final_metric_keep = final_metric_keep
        self.final_import_kwh = final_import_kwh
        self.final_import_kwh_battery = final_import_kwh_battery
        self.final_import_kwh_house = final_import_kwh_house
        self.final_export_kwh = final_export_kwh
        self.final_load_kwh = final_load_kwh
        self.final_pv_kwh = final_pv_kwh
        self.final_iboost_kwh = final_iboost_kwh
        self.final_battery_cycle = final_battery_cycle
        self.final_soc_min = soc_min
        self.final_soc_min_minute = soc_min_minute
        self.export_to_first_charge = export_to_first_charge
        self.predict_soc_time = predict_soc_time
        self.first_charge = first_charge
        self.first_charge_soc = first_charge_soc
        self.predict_state = predict_state
        self.predict_battery_power = predict_battery_power
        self.predict_battery_power = predict_battery_power
        self.predict_pv_power = predict_pv_power
        self.predict_grid_power = predict_grid_power
        self.predict_load_power = predict_load_power
        self.predict_iboost = predict_iboost
        self.predict_carbon_g = predict_carbon_g
        self.predict_export = predict_export
        self.metric_time = metric_time
        self.record_time = record_time
        self.predict_battery_cycle = predict_battery_cycle
        self.predict_battery_power = predict_battery_power
        self.pv_kwh_h0 = pv_kwh_h0
        self.import_kwh_h0 = import_kwh_h0
        self.export_kwh_h0 = export_kwh_h0
        self.load_kwh_h0 = load_kwh_h0
        self.load_kwh_time = load_kwh_time
        self.pv_kwh_time = pv_kwh_time
        self.import_kwh_time = import_kwh_time
        self.export_kwh_time = export_kwh_time

        return (
            round(final_metric, 4),
            round(import_kwh_battery, 4),
            round(import_kwh_house, 4),
            round(export_kwh, 4),
            soc_min,
            round(final_soc, 4),
            soc_min_minute,
            round(final_battery_cycle, 4),
            round(final_metric_keep, 4),
            round(final_iboost_kwh, 4),
            round(final_carbon_g, 4),
        )


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
            self.base.log("WARN: Inverter control auto restart already active, waiting...")
            return

        # Trigger restart
        self.base.log("WARN: Inverter control auto restart trigger: {}".format(reason))
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
            self.log("INFO: auto_restart not defined in apps.yaml, Predbat can't auto-restart inverter control")

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
                self.log("WARN: Inverter {}: inverter definition is not a dictionary".format(self.id))

        if self.inverter_type in INVERTER_DEF:
            self.log(f"Inverter {self.id}: Type {self.inverter_type} {INVERTER_DEF[self.inverter_type]['name']}")
        else:
            raise ValueError("Inverter type {} not defined".format(self.inverter_type))

        if self.inverter_type != "GE":
            self.log("WARN: Inverter {}: Using inverter type {} - not all features are available".format(self.id, self.inverter_type))

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
                        self.base.log("WARN: REST data reports Battery Capacity kWh as {} but nominal indicates {} - using nominal".format(self.soc_max, self.nominal_capacity))
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
                            self.log("WARN: Inverter is in calibration mode {}, Predbat will not function correctly and will be disabled".format(soc_force_adjust))
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
            self.base.log("ERROR: Reported battery size from REST is {}, but it must be >0".format(self.soc_max))
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
                    "WARN: Invertor time is {} AppDaemon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDaemon time zone".format(
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
                    self.log("ERROR: Inverter {} unable to read charge window time as neither REST, charge_start_time or charge_start_hour are set".format(self.id))
                    self.base.record_status(
                        "Error - Inverter {} unable to read charge window time as neither REST, charge_start_time or charge_start_hour are set".format(self.id), had_errors=True
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
            self.log("ERROR: Inverter {} unable to read Discharge window as neither REST or discharge_start_time are set".format(self.id))
            self.base.record_status("Error - Inverter {} unable to read Discharge window as neither REST or discharge_start_time are set".format(self.id), had_errors=True)
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
            self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, self.base.get_state_wrapper(entity_id=entity_id)))
            self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
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
            self.base.log(f"WARN: Inverter {self.id} Trying to write {new_value} to {name} didn't complete got {current_state}")
            self.base.record_status(f"Warn - Inverter {self.id} write to {name} failed", had_errors=True)
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
        self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, self.base.get_state_wrapper(entity_id, refresh=True)))
        self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
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
                self.log("WARN: Inverter {} unable read discharge window as neither REST, discharge_start_time or discharge_start_hour are set".format(self.id))
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
                    self.log("WARN: Inverter {} unable write discharge start time as neither REST or discharge_start_time are set".format(self.id))

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
                    self.log("WARN: Inverter {} unable write discharge end time as neither REST or discharge_end_time are set".format(self.id))

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
                self.log("WARN: Inverter {} unable to find service name for {}".format(self.id, service))
        else:
            self.log("WARN: Inverter {} unable to find service template for {}".format(self.id, service))

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
                    self.log("WARN: Inverter {} unable write charge window start as neither REST or charge_start_time are set".format(self.id))

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
                    self.log("WARN: Inverter {} unable write charge window end as neither REST, charge_end_hour or charge_end_time are set".format(self.id))

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
                    self.log("WARN: Inverter {} unable write charge window enable as neither REST or scheduled_charge_enable are set".format(self.id))

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
        self.base.log(f"WARN: Inverter {self.id} Trying to press {entity_id} didn't complete")
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
            self.base.log("ERROR: Exception raised {}".format(e))
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
        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new = int(self.rest_data["Control"]["Battery_Charge_Rate"])
            if abs(new - rate) < (self.battery_rate_max_charge * MINUTE_WATT / 12):
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
        for retry in range(5):
            r = requests.post(url, json=data)
            # time.sleep(10)
            self.rest_data = self.rest_runAll(self.rest_data)
            new = int(self.rest_data["Control"]["Battery_Discharge_Rate"])
            if abs(new - rate) < (self.battery_rate_max_discharge * MINUTE_WATT / 25):
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

        for retry in range(5):
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
        for retry in range(5):
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

        self.base.log("WARN: Set inverter {} charge schedule {} via REST failed got {}".format(self.id, enable, self.rest_data["Control"]["Enable_Charge_Schedule"]))
        self.base.record_status("Warn - Inverter {} REST failed to enableChargeSchedule".format(self.id), had_errors=True)
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

        self.base.log("WARN: Inverter {} set charge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeSlot1".format(self.id), had_errors=True)
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

        self.base.log("WARN: Inverter {} Set discharge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn - Inverter {} REST failed to setDischargeSlot1".format(self.id), had_errors=True)
        return False


class PredBat(hass.Hass):
    """
    The battery prediction class itself
    """

    def call_notify(self, message):
        """
        Sync wrapper for call_notify
        """
        for device in self.notify_devices:
            self.call_service_wrapper("notify/" + device, message=message)
        return True

    async def async_call_notify(self, message):
        """
        Send HA notifications
        """
        for device in self.notify_devices:
            await self.call_service_wrapper("notify/" + device, message=message)
        return True

    def resolve_arg(self, arg, value, default=None, indirect=True, combine=False, attribute=None, index=None, extra_args=None):
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
                    if isinstance(item, list):
                        final += item
                    else:
                        final.append(item)
                return final

        # Resolve templated data
        for repeat in range(2):
            if isinstance(value, str) and "{" in value:
                try:
                    if extra_args:
                        value = value.format(**self.args, **extra_args)
                    else:
                        value = value.format(**self.args)
                except KeyError:
                    self.log("WARN: can not resolve {} value {}".format(arg, value))
                    self.record_status("Warn - can not resolve {} value {}".format(arg, value), had_errors=True)
                    value = default

        # Resolve join list by name
        if isinstance(value, str) and value.startswith("+[") and value.endswith("]"):
            value = self.get_arg(value[2:-1], default=default, indirect=indirect, combine=False, attribute=attribute, index=index)

        # Resolve indirect instance
        if indirect and isinstance(value, str) and "." in value:
            if "$" in value:
                value, attribute = value.split("$")

            if attribute:
                value = self.get_state_wrapper(entity_id=value, default=default, attribute=attribute)
            else:
                value = self.get_state_wrapper(entity_id=value, default=default)
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
            url = "https://api.givenergy.cloud/v1/inverter/{}/data-points/{}?pageSize=4096".format(geserial, datestr)
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
        except Exception:
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
            new_version = False

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
                        new_version = True

            # Refresh the list
            self.expose_config("update", this_tag)
            self.expose_config("version", new_version, force=True)

        else:
            self.log("WARN: Unable to download Predbat version information from github, return code: {}".format(data))
            self.expose_config("version", False, force=True)

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
        for retry in range(3):
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

        peak_start = datetime.strptime(self.get_arg("futurerate_peak_start", "00:00:00"), "%H:%M:%S")
        peak_end = datetime.strptime(self.get_arg("futurerate_peak_end", "00:00:00"), "%H:%M:%S")
        peak_start_minutes = peak_start.minute + peak_start.hour * 60
        peak_end_minutes = peak_end.minute + peak_end.hour * 60
        if peak_end_minutes < peak_start_minutes:
            peak_end_minutes += 24 * 60

        peak_premium_import = self.get_arg("futurerate_peak_premium_import", 0)
        peak_premium_export = self.get_arg("futurerate_peak_premium_export", 0)

        self.log("Future rates - peak rate is {} - {} minutes premium import {} export {}".format(peak_start_minutes, peak_end_minutes, peak_premium_import, peak_premium_export))

        if pdata and "Rows" in pdata:
            for row in pdata["Rows"]:
                if "Name" in row:
                    rstart = row.get("StartTime", "") + now_offset
                    rend = row.get("EndTime", "") + now_offset
                    rname = row.get("Name", "")
                if "Columns" in row:
                    for column in row["Columns"]:
                        cname = column.get("Name", "")
                        cvalue = column.get("Value", "")
                        date_start, time_start = rstart.split("T")
                        date_end, time_end = rend.split("T")
                        if "-" in rname and "-" in cname and "," in cvalue and cname:
                            date_start = cname
                            date_end = cname
                            cvalue = cvalue.replace(",", ".")
                            cvalue = float(cvalue)
                            rstart = date_start + "T" + time_start
                            rend = date_end + "T" + time_end
                            TIME_FORMAT_NORD = "%d-%m-%YT%H:%M:%S%z"
                            time_date_start = datetime.strptime(rstart, TIME_FORMAT_NORD)
                            time_date_end = datetime.strptime(rend, TIME_FORMAT_NORD)
                            if time_date_end < time_date_start:
                                time_date_end += timedelta(days=1)
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

                            if time_date_start not in extracted_keys:
                                extracted_keys.append(time_date_start)
                                extracted_data[time_date_start] = item
                            else:
                                self.log("WARN: Duplicate key {} in extracted_keys".format(time_date_start))

        if extracted_keys:
            extracted_keys.sort()
            for key in extracted_keys:
                array_values.append(extracted_data[key])
            self.log("Loaded {} datapoints of futurerate analysis".format(len(extracted_keys)))
            mdata_import = self.minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_import", "from", backwards=False, to_key="to")
            mdata_export = self.minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_export", "from", backwards=False, to_key="to")

        future_data = []
        minute_now_hour = int(self.minutes_now / 60) * 60
        for minute in range(minute_now_hour, self.forecast_plan_hours * 60 + minute_now_hour, 60):
            if mdata_import.get(minute) or mdata_export.get(minute):
                future_data.append("{} => {} / {}".format(self.time_abs_str(minute), mdata_import.get(minute), mdata_export.get(minute)))

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
            if now_since_midnight.seconds > (9.5 * 60 * 60) and now_since_midnight.seconds < (11 * 60 * 60) and age.seconds > (0.5 * 60 * 60):
                needs_update = True
            if age.seconds > (12 * 60 * 60):
                needs_update = True

            if not needs_update:
                self.log("Return cached futurerate data for {} age {} minutes".format(url, self.dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for retry in range(3):
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
            self.car_charging_energy = self.minute_data_import_export(now_utc, "car_charging_energy", scale=self.car_charging_energy_scale, required_unit="kWh")
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold * 60.0))
        return self.car_charging_energy

    def get_history_ad(self, entity_id, days=30):
        """
        Wrapper for AppDaemon get history
        """
        return self.get_history(entity_id=entity_id, days=days)

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False):
        """
        Wrapper function to get state from HA
        """
        return self.ha_interface.get_state(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh)

    def set_state_wrapper(self, entity_id, state, attributes={}):
        """
        Wrapper function to get state from HA
        """
        return self.ha_interface.set_state(entity_id, state, attributes=attributes)

    def call_service_wrapper(self, service, **kwargs):
        """
        Wrapper function to call a HA service
        """
        return self.ha_interface.call_service(service, **kwargs)

    def get_history_wrapper(self, entity_id, days=30):
        """
        Wrapper function to get history from HA
        """
        history = self.ha_interface.get_history(entity_id, days=days, now=self.now)

        if history is None:
            self.log("Error: Failure to fetch history for {}".format(entity_id))
            raise ValueError
        else:
            return history

    def minute_data_import_export(self, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True):
        """
        Download one or more entities for import/export data
        """
        if "." not in key:
            entity_ids = self.get_arg(key, indirect=False)
        else:
            entity_ids = key

        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        import_today = {}
        for entity_id in entity_ids:
            try:
                history = self.get_history_wrapper(entity_id=entity_id, days=self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                import_today = self.minute_data(
                    history[0],
                    self.max_days_previous,
                    now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    smoothing=smoothing,
                    scale=scale,
                    clean_increment=increment,
                    accumulate=import_today,
                    required_unit=required_unit,
                )
            else:
                self.log("Error: Unable to fetch history for {}".format(entity_id))
                self.record_status("Error - Unable to fetch history from {}".format(entity_id), had_errors=True)
                raise ValueError

        return import_today

    def minute_data_load(self, now_utc, entity_name, max_days_previous, required_unit=None):
        """
        Download one or more entities for load data
        """
        entity_ids = self.get_arg(entity_name, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        load_minutes = {}
        age_days = None
        for entity_id in entity_ids:
            try:
                history = self.get_history_wrapper(entity_id=entity_id, days=max_days_previous)
            except ValueError:
                history = []

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
                    required_unit=required_unit,
                )
            else:
                self.log("ERROR: Unable to fetch history for {}".format(entity_id))
                self.record_status("Error - Unable to fetch history from {}".format(entity_id), had_errors=True)
                raise ValueError

        if age_days is None:
            age_days = 0
        return load_minutes, age_days

    def minute_data_state(self, history, days, now, state_key, last_updated_key):
        """
        Get historical data for state (e.g. predbat status)
        """
        mdata = {}
        prev_last_updated_time = None
        last_state = "unknown"
        newest_state = 0
        last_state = 0
        newest_age = 999999

        if not history:
            self.log("Warning, empty history passed to minute_data_state, ignoring (check your settings)...")
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

            state = item[state_key]
            last_updated_time = self.str2time(item[last_updated_key])

            # Update prev to the first if not set
            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

            timed = now - last_updated_time
            timed_to = now - prev_last_updated_time

            minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60 * 24)
            minutes = int(timed.seconds / 60) + int(timed.days * 60 * 24)

            minute = minutes
            while minute < minutes_to:
                mdata[minute] = last_state
                minute += 1

            # Store previous state
            prev_last_updated_time = last_updated_time
            last_state = state

            if minutes < newest_age:
                newest_age = minutes
                newest_state = state

        state = newest_state
        for minute in range(0, 60 * 24 * days):
            rindex = 60 * 24 * days - minute - 1
            state = mdata.get(rindex, state)
            mdata[rindex] = state
            minute += 1

        return mdata

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
        required_unit=None,
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

            # Find and converter units
            if required_unit and ("attributes" in item):
                if "unit_of_measurement" in item["attributes"]:
                    unit = item["attributes"]["unit_of_measurement"]
                    if unit != required_unit:
                        if required_unit in ["kW", "kWh", "kg", "kg/kWh"] and unit in ["W", "Wh", "g", "g/kWh"]:
                            state = state / 1000.0
                        elif required_unit in ["W", "Wh", "g", "g/kWh"] and unit in ["kW", "kWh", "kg", "kg/kWh"]:
                            state = state * 1000.0
                        else:
                            # Ignore data in wrong units if we can't converter
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
            for minute in range(60 * 24 * days):
                rindex = 60 * 24 * days - minute - 1
                state = mdata.get(rindex, state)
                mdata[rindex] = state
                minute += 1

        # Reverse data with smoothing
        if clean_increment:
            mdata = self.clean_incrementing_reverse(mdata, max_increment)

        # Accumulate to previous data?
        if accumulate:
            for minute in range(60 * 24 * days):
                if minute in mdata:
                    mdata[minute] += accumulate.get(minute, 0)
                else:
                    mdata[minute] = accumulate.get(minute, 0)

        if adjust_key:
            self.io_adjusted = adata

        # Rounding
        for minute in mdata.keys():
            mdata[minute] = self.dp4(mdata[minute])

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
        return round(value, 1)

    def dp2(self, value):
        """
        Round to 2 decimal places
        """
        return round(value, 2)

    def dp3(self, value):
        """
        Round to 3 decimal places
        """
        return round(value, 3)

    def dp4(self, value):
        """
        Round to 4 decimal places
        """
        return round(value, 4)

    def hit_charge_window(self, charge_window, start, end):
        """
        Determines if the given start and end time falls within any of the charge windows.

        Parameters:
        charge_window (list): A list of dictionaries representing charge windows, each containing "start" and "end" keys.
        start (int): The start time of the interval to check.
        end (int): The end time of the interval to check.

        Returns:
        int: The index of the charge window that the interval falls within, or -1 if it doesn't fall within any charge window.
        """
        window_n = 0
        for window in charge_window:
            if end > window["start"] and start < window["end"]:
                return window_n
            window_n += 1
        return -1

    def in_charge_window(self, charge_window, minute_abs):
        """
        Work out if this minute is within the a charge window

        Parameters:
        charge_window (list): A sorted list of dictionaries representing charge windows.
                                Each dictionary should have "start" and "end" keys
                                representing the start and end minutes of the window.

        minute_abs (int): The absolute minute value to check.

        Returns:
        int: The index of the charge window if the minute is within a window,
                otherwise -1.
        """
        window_n = 0
        for window in charge_window:
            if minute_abs >= window["start"] and minute_abs < window["end"]:
                return window_n
            elif window["start"] > minute_abs:
                # As windows are sorted, we can stop searching once we've passed the minute
                break
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

        for index in range(length):
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

        for offset in range(step):
            if historical:
                load_yesterday_raw += self.get_historical(data, minute_previous + offset)
            else:
                load_yesterday_raw += self.get_from_incrementing(data, minute_previous + offset)

        load_yesterday = load_yesterday_raw
        # Car charging hold
        if self.car_charging_hold and self.car_charging_energy:
            # Hold based on data
            car_energy = 0
            for offset in range(step):
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
            use_days = max(min(days, self.load_minutes_age), 1)
            sum_day = 0
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
            use_days = max(min(days, self.load_minutes_age), 1)
            num_gaps = 0
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

    def get_historical_base(self, data, minute, base_minutes):
        """
        Get historical data from base minute ago
        """
        # No data?
        if not data:
            return 0

        minute_previous = base_minutes - minute
        return self.get_from_incrementing(data, minute_previous)

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
            use_days = max(min(days, self.load_minutes_age), 1)
            weight = self.days_previous_weight[this_point]
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
            return max(data.get(index, 0) - data.get(index + 1, 0), 0)
        else:
            return max(data.get(index + 1, 0) - data.get(index, 0), 0)

    def record_length(self, charge_window, charge_limit, best_price):
        """
        Limit the forecast length to either the total forecast duration or the start of the last window that falls outside the forecast
        """
        next_charge_start = self.forecast_minutes + self.minutes_now
        if charge_window:
            for window_n in range(len(charge_window)):
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
            attributes={
                "friendly_name": "Status",
                "detail": extra,
                "icon": "mdi:information",
                "last_updated": str(datetime.now()),
                "debug": debug,
                "version": THIS_VERSION,
                "error": (had_errors or self.had_errors),
            },
        )

        self.log("Info: record_status {}".format(message + extra))
        print("Info: record_status {}".format(message + extra))

        self.previous_status = message
        if had_errors:
            self.had_errors = True

    def scenario_summary_title(self, record_time):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % 30
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, 30):
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            dstamp = minute_timestamp.strftime(TIME_FORMAT)
            stamp = minute_timestamp.strftime("%H:%M")
            if txt:
                txt += ", "
            txt += "%8s" % str(stamp)
            if record_time[dstamp] > 0:
                break
        return txt

    def scenario_summary(self, record_time, datap):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % 30
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, 30):
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = datap[stamp]
            if not isinstance(value, str):
                value = self.dp2(value)
                if value > 10000:
                    value = self.dp0(value)
            if txt:
                txt += ", "
            txt += "%8s" % str(value)
            if record_time[stamp] > 0:
                break
        return txt

    def scenario_summary_state(self, record_time):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % 30
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, 30):
            minute_relative_start = max(minute_absolute - self.minutes_now, 0)
            minute_relative_end = minute_relative_start + 30
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = ""

            charge_window_n = -1
            for try_minute in range(this_minute_absolute, minute_absolute + 30, 5):
                charge_window_n = self.in_charge_window(self.charge_window_best, try_minute)
                if charge_window_n >= 0:
                    break

            discharge_window_n = -1
            for try_minute in range(this_minute_absolute, minute_absolute + 30, 5):
                discharge_window_n = self.in_charge_window(self.discharge_window_best, try_minute)
                if discharge_window_n >= 0:
                    break

            soc_percent = calc_percent_limit(self.predict_soc_best.get(minute_relative_start, 0.0), self.soc_max)
            soc_percent_end = calc_percent_limit(self.predict_soc_best.get(minute_relative_end, 0.0), self.soc_max)
            soc_percent_max = max(soc_percent, soc_percent_end)
            soc_percent_min = min(soc_percent, soc_percent_end)

            if charge_window_n >= 0 and discharge_window_n >= 0:
                value = "Chrg/Dis"
            elif charge_window_n >= 0:
                charge_target = self.charge_limit_best[charge_window_n]
                if charge_target == self.reserve:
                    value = "FrzChrg"
                else:
                    value = "Chrg"
            elif discharge_window_n >= 0:
                discharge_target = self.discharge_limits_best[discharge_window_n]
                if discharge_target >= soc_percent_max:
                    value = "FrzDis"
                else:
                    value = "Dis"

            if record_time[stamp] > 0:
                break
            if txt:
                txt += ", "
            txt += "%8s" % str(value)
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
                for offset in range(step):
                    import_value_today += self.get_from_incrementing(import_minutes, minutes_now - minute - offset - 1)
                    load_value_today, load_value_today_raw = self.get_filtered_load_minute(load_minutes, minutes_now - minute - 1, historical=False, step=step)

            import_value_pred = 0
            forecast_value_pred = 0
            for offset in range(step):
                import_value_pred += self.get_historical(import_minutes, minute - minutes_now + offset)
                forecast_value_pred += self.get_from_incrementing(load_forecast, minute + offset, backwards=False)

            if self.load_forecast_only:
                load_value_pred, load_value_pred_raw = (0, 0)
            else:
                load_value_pred, load_value_pred_raw = self.get_filtered_load_minute(load_minutes, minute - minutes_now, historical=True, step=step)

            # Add in forecast load
            load_value_pred += forecast_value_pred
            load_value_pred_raw += forecast_value_pred

            # Ignore periods of import as assumed to be deliberate (battery charging periods overnight for example)
            car_value_actual = load_value_today_raw - load_value_today
            car_value_pred = load_value_pred_raw - load_value_pred
            if minute < minutes_now and import_value_today >= load_value_today_raw:
                import_ignored_load_actual += load_value_today
                load_value_today = 0
                import_ignored_load_pred += load_value_pred
                load_value_pred = 0

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
                "results": self.filtered_times(load_actual_stamp),
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
                "results": self.filtered_times(load_predict_stamp),
                "today": self.filtered_today(load_predict_stamp),
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
                "results": self.filtered_times(load_adjusted_stamp),
                "today": self.filtered_today(load_adjusted_stamp),
                "friendly_name": "Load energy prediction adjusted",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )

        return difference_cap

    def get_load_divergence(self, minutes_now, load_minutes):
        """
        Work out the divergence between peak and average load over the next period
        """
        load_total = 0
        load_count = 0
        load_min = 99999
        load_max = 0
        look_over = 60 * 4

        for minute in range(0, look_over, PREDICT_STEP):
            load, load_raw = self.get_filtered_load_minute(load_minutes, minute, historical=True, step=PREDICT_STEP)
            load *= 1000 * 60 / PREDICT_STEP
            load_total += load
            load_count += 1
            load_min = min(load_min, load)
            load_max = max(load_max, load)
        load_mean = load_total / load_count
        load_diff_total = 0
        for minute in range(0, look_over, PREDICT_STEP):
            load = 0
            load, load_raw = self.get_filtered_load_minute(load_minutes, minute, historical=True, step=PREDICT_STEP)
            load *= 1000 * 60 / PREDICT_STEP
            load_diff = abs(load - load_mean)
            load_diff *= load_diff
            load_diff_total += load_diff

        load_std_dev = math.sqrt(load_diff_total / load_count)
        load_divergence = load_std_dev / load_mean
        load_divergence = min(load_divergence, 2.0)
        self.log(
            "Load divergence over {} hours mean {} W, min {} W, max {} W, std dev {} W, divergence {}%".format(
                look_over / 60.0, self.dp2(load_mean), self.dp2(load_min), self.dp2(load_max), self.dp2(load_std_dev), self.dp2(load_divergence * 100.0)
            )
        )
        if self.metric_load_divergence_enable:
            return self.dp2(load_divergence)
        else:
            return None

    def get_cloud_factor(self, minutes_now, pv_data, pv_data10):
        """
        Work out approximated cloud factor
        """
        pv_total = 0
        pv_total10 = 0
        for minute in range(self.forecast_minutes):
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

    def step_data_history(
        self,
        item,
        minutes_now,
        forward,
        step=PREDICT_STEP,
        scale_today=1.0,
        scale_fixed=1.0,
        type_load=False,
        load_forecast={},
        cloud_factor=None,
        load_scaling_dynamic=None,
        base_offset=None,
    ):
        """
        Create cached step data for historical array
        """
        values = {}
        cloud_diff = 0

        for minute in range(0, self.forecast_minutes + 30, step):
            value = 0
            minute_absolute = minute + minutes_now

            scaling_dynamic = 1.0
            if load_scaling_dynamic:
                scaling_dynamic = load_scaling_dynamic.get(minute_absolute, scaling_dynamic)

            # Reset in-day adjustment for tomorrow
            if (minute + minutes_now) > 24 * 60:
                scale_today = 1.0

            if type_load and not forward:
                if self.load_forecast_only:
                    load_yesterday, load_yesterday_raw = (0, 0)
                else:
                    load_yesterday, load_yesterday_raw = self.get_filtered_load_minute(item, minute, historical=True, step=step)
                value += load_yesterday
            else:
                for offset in range(step):
                    if forward:
                        value += item.get(minute + minutes_now + offset, 0.0)
                    else:
                        if base_offset:
                            value += self.get_historical_base(item, minute + offset, base_offset)
                        else:
                            value += self.get_historical(item, minute + offset)

            # Extra load adding in (e.g. heat pump)
            load_extra = 0
            if load_forecast:
                for offset in range(step):
                    load_extra += self.get_from_incrementing(load_forecast, minute_absolute, backwards=False)
            values[minute] = self.dp4((value + load_extra) * scaling_dynamic * scale_today * scale_fixed)

        # Simple divergence model keeps the same total but brings PV/Load up and down every 5 minutes
        if cloud_factor and cloud_factor > 0:
            for minute in range(0, self.forecast_minutes, step):
                cloud_on = int((minute + self.minutes_now) / 5) % 2
                if cloud_on > 0:
                    cloud_diff += min(values[minute] * cloud_factor, values.get(minute + 5, 0) * cloud_factor)
                    values[minute] += cloud_diff
                else:
                    subtract = min(cloud_diff, values[minute])
                    values[minute] -= subtract
                    cloud_diff = 0
                values[minute] = self.dp4(values[minute])

        return values

    def find_charge_window_optimised(self, charge_windows):
        """
        Takes in an array of charge windows
        Returns a dictionary defining for each minute that is in the charge window will contain the window number
        """
        charge_window_optimised = {}
        for window_n in range(len(charge_windows)):
            for minute in range(charge_windows[window_n]["start"], charge_windows[window_n]["end"], PREDICT_STEP):
                charge_window_optimised[minute] = window_n
        return charge_window_optimised

    def filtered_today(self, time_data):
        """
        Grab figure for today (midnight)
        """
        today = self.midnight_utc
        tomorrow = today + timedelta(days=1)
        tomorrow_stamp = tomorrow.strftime(TIME_FORMAT)
        tomorrow_value = time_data.get(tomorrow_stamp, None)
        return tomorrow_value

    def filtered_times(self, time_data):
        """
        Filter out duplicate values in time series data
        """

        prev = None
        new_data = {}
        keys = list(time_data.keys())
        for id in range(len(keys)):
            stamp = keys[id]
            value = time_data[stamp]
            next_value = time_data[keys[id + 1]] if id + 1 < len(keys) else None
            if prev is None or value != prev or next_value != value:
                new_data[stamp] = value
            prev = value
            id += 1
        return new_data

    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, save=None, step=PREDICT_STEP):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """

        # Call the prediction model
        pred = self.prediction
        (
            final_metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            final_battery_cycle,
            final_metric_keep,
            final_iboost_kwh,
            final_carbon_g,
        ) = pred.run_prediction(charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, save, step)
        self.predict_soc = pred.predict_soc
        self.car_charging_soc_next = pred.car_charging_soc_next
        self.iboost_next = pred.iboost_next
        self.iboost_running = pred.iboost_running
        if save or self.debug_enable:
            predict_soc_time = pred.predict_soc_time
            first_charge = pred.first_charge
            first_charge_soc = pred.first_charge_soc
            predict_car_soc_time = pred.predict_car_soc_time
            predict_battery_power = pred.predict_battery_power
            predict_state = pred.predict_state
            predict_battery_cycle = pred.predict_battery_cycle
            predict_pv_power = pred.predict_pv_power
            predict_grid_power = pred.predict_grid_power
            predict_load_power = pred.predict_load_power
            final_export_kwh = pred.final_export_kwh
            export_kwh_h0 = pred.export_kwh_h0
            final_load_kwh = pred.final_load_kwh
            load_kwh_h0 = pred.load_kwh_h0
            metric_time = pred.metric_time
            record_time = pred.record_time
            predict_iboost = pred.predict_iboost
            predict_carbon_g = pred.predict_carbon_g
            load_kwh_time = pred.load_kwh_time
            pv_kwh_time = pred.pv_kwh_time
            import_kwh_time = pred.import_kwh_time
            export_kwh_time = pred.export_kwh_time
            final_pv_kwh = pred.final_pv_kwh
            export_to_first_charge = pred.export_to_first_charge
            pv_kwh_h0 = pred.pv_kwh_h0
            final_import_kwh = pred.final_import_kwh
            final_import_kwh_house = pred.final_import_kwh_house
            final_import_kwh_battery = pred.final_import_kwh_battery
            hours_left = pred.hours_left
            final_car_soc = pred.final_car_soc
            import_kwh_h0 = pred.import_kwh_h0
            predict_export = pred.predict_export

            if save == "best":
                self.predict_soc_best = pred.predict_soc_best
                self.predict_iboost_best = pred.predict_iboost_best
                self.predict_metric_best = pred.predict_metric_best
                self.predict_carbon_best = pred.predict_carbon_best

            if self.debug_enable or save:
                self.log(
                    "predict {} end_record {} final soc {} kWh metric {} {} metric_keep {} min_soc {} @ {} kWh load {} pv {}".format(
                        save,
                        self.time_abs_str(end_record + self.minutes_now),
                        round(final_soc, 2),
                        round(final_metric, 2),
                        self.currency_symbols[1],
                        round(final_metric_keep, 2),
                        round(soc_min, 2),
                        self.time_abs_str(soc_min_minute),
                        round(final_load_kwh, 2),
                        round(final_pv_kwh, 2),
                    )
                )
                self.log("         [{}]".format(self.scenario_summary_title(record_time)))
                self.log("    SOC: [{}]".format(self.scenario_summary(record_time, predict_soc_time)))
                self.log("    BAT: [{}]".format(self.scenario_summary(record_time, predict_state)))
                self.log("   LOAD: [{}]".format(self.scenario_summary(record_time, load_kwh_time)))
                self.log("     PV: [{}]".format(self.scenario_summary(record_time, pv_kwh_time)))
                self.log(" IMPORT: [{}]".format(self.scenario_summary(record_time, import_kwh_time)))
                self.log(" EXPORT: [{}]".format(self.scenario_summary(record_time, export_kwh_time)))
                if self.iboost_enable:
                    self.log(" IBOOST: [{}]".format(self.scenario_summary(record_time, predict_iboost)))
                if self.carbon_enable:
                    self.log(" CARBON: [{}]".format(self.scenario_summary(record_time, predict_carbon_g)))
                for car_n in range(self.num_cars):
                    self.log("   CAR{}: [{}]".format(car_n, self.scenario_summary(record_time, predict_car_soc_time[car_n])))
                self.log(" METRIC: [{}]".format(self.scenario_summary(record_time, metric_time)))
                if save == "best":
                    self.log(" STATE:  [{}]".format(self.scenario_summary_state(record_time)))

            # Save data to HA state
            if save and save == "base" and not SIMULATE:
                self.dashboard_item(
                    self.prefix + ".battery_hours_left",
                    state=self.dp2(hours_left),
                    attributes={"friendly_name": "Predicted Battery Hours left", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
                )
                postfix = ""
                for car_n in range(self.num_cars):
                    if car_n > 0:
                        postfix = "_" + str(car_n)
                    self.dashboard_item(
                        self.prefix + ".car_soc" + postfix,
                        state=self.dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                        attributes={
                            "results": self.filtered_times(predict_car_soc_time[car_n]),
                            "today": self.filtered_today(predict_car_soc_time[car_n]),
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
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
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
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
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
                        "results": self.filtered_times(predict_battery_cycle),
                        "today": self.filtered_today(predict_battery_cycle),
                        "friendly_name": "Predicted Battery Cycle",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".pv_power",
                    state=self.dp3(0),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
                        "friendly_name": "Predicted PV Power",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".grid_power",
                    state=self.dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
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
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
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
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
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
                        "results": self.filtered_times(load_kwh_time),
                        "today": self.filtered_today(load_kwh_time),
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
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
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
                    attributes={
                        "friendly_name": "Predicted import to battery",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
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
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".duration",
                    state=self.dp2(end_record / 60),
                    attributes={"friendly_name": "Prediction duration", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:arrow-split-vertical"},
                )
                if self.carbon_enable:
                    self.dashboard_item(
                        self.prefix + ".carbon",
                        state=self.dp2(final_carbon_g),
                        attributes={
                            "results": self.filtered_times(predict_carbon_g),
                            "today": self.filtered_today(predict_carbon_g),
                            "friendly_name": "Predicted Carbon energy",
                            "state_class": "measurement",
                            "unit_of_measurement": "g",
                            "icon": "mdi:molecule-co2",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".carbon_now",
                        state=self.dp2(self.carbon_intensity.get(0, 0)),
                        attributes={
                            "friendly_name": "Grid carbon intensity now",
                            "state_class": "measurement",
                            "unit_of_measurement": "g/kWh",
                            "icon": "mdi:molecule-co2",
                        },
                    )

            if save and save == "best" and not SIMULATE:
                self.dashboard_item(
                    self.prefix + ".best_battery_hours_left",
                    state=self.dp2(hours_left),
                    attributes={"friendly_name": "Predicted Battery Hours left best", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
                )
                postfix = ""
                for car_n in range(self.num_cars):
                    if car_n > 0:
                        postfix = "_" + str(car_n)
                    self.dashboard_item(
                        self.prefix + ".car_soc_best" + postfix,
                        state=self.dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                        attributes={
                            "results": self.filtered_times(predict_car_soc_time[car_n]),
                            "today": self.filtered_today(predict_car_soc_time[car_n]),
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
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
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
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
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
                        "results": self.filtered_times(predict_battery_cycle),
                        "today": self.filtered_today(predict_battery_cycle),
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
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
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
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
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
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
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
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
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
                        "results": self.filtered_times(load_kwh_time),
                        "today": self.filtered_today(load_kwh_time),
                        "friendly_name": "Predicted load best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:home-lightning-bolt",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_pv_energy",
                    state=self.dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy",
                    state=self.dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
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
                    attributes={
                        "friendly_name": "Predicted import to house best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_metric",
                    state=self.dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted best metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".record", state=0.0, attributes={"results": self.filtered_times(record_time), "friendly_name": "Prediction window", "state_class": "measurement"}
                )
                self.dashboard_item(
                    self.prefix + ".iboost_best",
                    state=self.dp2(final_iboost_kwh),
                    attributes={
                        "results": self.filtered_times(predict_iboost),
                        "today": self.filtered_today(predict_iboost),
                        "friendly_name": "Predicted iBoost energy best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:water-boiler",
                    },
                )
                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_iboost_active" + postfix,
                    state=self.iboost_running,
                    attributes={"friendly_name": "iBoost active", "icon": "mdi:water-boiler"},
                )
                self.find_spare_energy(self.predict_soc, predict_export, step, first_charge)
                if self.carbon_enable:
                    self.dashboard_item(
                        self.prefix + ".carbon_best",
                        state=self.dp2(final_carbon_g),
                        attributes={
                            "results": self.filtered_times(predict_carbon_g),
                            "today": self.filtered_today(predict_carbon_g),
                            "friendly_name": "Predicted Carbon energy best",
                            "state_class": "measurement",
                            "unit_of_measurement": "g",
                            "icon": "mdi:molecule-co2",
                        },
                    )

            if save and save == "debug" and not SIMULATE:
                self.dashboard_item(
                    self.prefix + ".pv_power_debug",
                    state=self.dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
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
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
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
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
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
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
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
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
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
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
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
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted best 10% metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_export_energy",
                    state=self.dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
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
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
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
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
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
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
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
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted base 10% metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_export_energy",
                    state=self.dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
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
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )

        return (
            final_metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            final_battery_cycle,
            final_metric_keep,
            final_iboost_kwh,
            final_carbon_g,
        )

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
        minute = -24 * 60
        rate_last = 0
        adjusted_rates = {}
        replicated_rates = {}

        # Add 48 extra hours to make sure the whole cycle repeats another day
        while minute < (self.forecast_minutes + 48 * 60):
            if minute not in rates:
                adjust_type = "copy"
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
                        adjust_type = "future"
                    elif (
                        (not is_import)
                        and (not is_gas)
                        and self.get_arg("futurerate_adjust_export", False)
                        and (minute in self.future_energy_rates_export)
                        and (minute_mod in self.future_energy_rates_export)
                    ):
                        rate_offset = max(rate_offset - self.future_energy_rates_export[minute_mod] + self.future_energy_rates_export[minute], 0)
                        adjust_type = "future"
                    elif is_import:
                        rate_offset = rate_offset + self.metric_future_rate_offset_import
                        if self.metric_future_rate_offset_import:
                            adjust_type = "offset"
                    elif (not is_import) and (not is_gas):
                        rate_offset = max(rate_offset + self.metric_future_rate_offset_export, 0)
                        if self.metric_future_rate_offset_export:
                            adjust_type = "offset"

                    adjusted_rates[minute] = True

                rates[minute] = rate_offset
                replicated_rates[minute] = adjust_type
            else:
                rate_last = rates[minute]
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
        while minute < stop_at and not (minute >= (self.forecast_minutes + self.minutes_now) and (rate_low_start < 0)):
            if minute in rates:
                rate = rates[minute]
                if (
                    ((not find_high) and (rate <= threshold_rate))
                    or (find_high and (rate >= threshold_rate) and (rate > 0))
                    or minute in self.manual_all_times
                    or rate_low_start in self.manual_all_times
                ):
                    rate_diff = abs(rate - rate_low_rate)
                    if (rate_low_start >= 0) and rate_diff > self.combine_rate_threshold:
                        # Refuse mixed rates that are different by more than threshold
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
                    if (rate_low_start in self.manual_all_times or minute in self.manual_all_times) and (rate_low_start >= 0) and ((minute - rate_low_start) >= 30):
                        # Manual slot
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

    def basic_rates(self, info, rtype, prev=None, rate_replicate={}):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        if prev:
            rates = prev.copy()
        else:
            # Set to zero
            for minute in range(24 * 60):
                rates[minute] = 0

        max_minute = max(rates) + 1
        midnight = datetime.strptime("00:00:00", "%H:%M:%S")
        for this_rate in info:
            if this_rate:
                start_str = this_rate.get("start", "00:00:00")
                start_str = self.resolve_arg("start", start_str, "00:00:00")
                end_str = this_rate.get("end", "00:00:00")
                end_str = self.resolve_arg("end", end_str, "00:00:00")
                load_scaling = this_rate.get("load_scaling", None)

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

                # Support increment to existing rates (for override)
                if "rate" in this_rate:
                    rate = this_rate.get("rate", 0.0)
                    rate_increment = False
                elif "rate_increment" in this_rate:
                    rate = this_rate.get("rate_increment", 0.0)
                    rate_increment = True
                else:
                    rate = 0
                    rate_increment = False

                # Resolve any sensor links
                rate = self.resolve_arg("rate", rate, 0.0)

                # Ensure the end result is a float
                try:
                    rate = float(rate)
                except ValueError:
                    self.log("WARN: Bad rate {} provided in energy rates".format(rate))
                    self.record_status("Bad rate {} provided in energy rates".format(rate), had_errors=True)
                    continue

                # Time in minutes
                start_minutes = max(self.minutes_to_time(start, midnight), 0)
                end_minutes = min(self.minutes_to_time(end, midnight), 24 * 60 - 1)

                self.log(
                    "Adding rate {}: {} => {} to {} @ {} date {} increment {}".format(
                        rtype, this_rate, self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), rate, date, rate_increment
                    )
                )

                # Make end > start
                if end_minutes <= start_minutes:
                    end_minutes += 24 * 60

                # Adjust for date if specified
                if date:
                    delta_minutes = self.minutes_to_time(date, self.midnight)
                    start_minutes += delta_minutes
                    end_minutes += delta_minutes

                # Store rates against range
                if end_minutes >= (-24 * 60) and start_minutes < max_minute:
                    for minute in range(start_minutes, end_minutes):
                        minute_mod = minute % max_minute
                        if (not date) or (minute >= (-24 * 60) and minute < max_minute):
                            minute_index = minute_mod
                            # For incremental adjustments we have to loop over 24-hour periods
                            while minute_index < max_minute:
                                if rate_increment:
                                    rates[minute_index] = rates.get(minute_index, 0.0) + rate
                                    rate_replicate[minute_index] = "increment"
                                else:
                                    rates[minute_index] = rate
                                    rate_replicate[minute_index] = "user"
                                if load_scaling is not None:
                                    self.load_scaling_dynamic[minute_index] = load_scaling
                                if date or not prev:
                                    break
                                minute_index += 24 * 60
                            if not date and not prev:
                                rates[minute_mod + max_minute] = rate
                                if load_scaling is not None:
                                    self.load_scaling_dynamic[minute_mod + max_minute] = load_scaling

        return rates

    def plan_car_charging(self, car_n, low_rates):
        """
        Plan when the car will charge, taking into account ready time and pricing
        """
        plan = []
        car_soc = self.car_charging_soc[car_n]
        max_price = self.car_charging_plan_max_price[car_n]

        if self.car_charging_plan_smart[car_n]:
            price_sorted = self.sort_window_by_price(low_rates, reverse_time=True)
            price_sorted.reverse()
        else:
            price_sorted = [n for n in range(len(low_rates))]

        try:
            ready_time = datetime.strptime(self.car_charging_plan_time[car_n], "%H:%M:%S")
        except ValueError:
            ready_time = datetime.strptime("07:00:00", "%H:%M:%S")
            self.log("WARN: Car charging plan time for car {} is invalid".format(car_n))

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
            price = window["average"]

            length = 0
            kwh = 0

            # Stop once we have enough charge, allow small margin for rounding
            if (car_soc + 0.1) >= self.car_charging_limit[car_n]:
                break

            # Skip past windows
            if end <= start:
                continue

            # Skip over prices when they are too high
            if (max_price != 0) and price > max_price:
                continue

            # Compute amount of charge
            length = end - start
            hours = length / 60
            kwh = self.car_charging_rate[car_n] * hours

            kwh_add = kwh * self.car_charging_loss
            kwh_left = max(self.car_charging_limit[car_n] - car_soc, 0)

            # Clamp length to required amount (shorten the window)
            if kwh_add > kwh_left:
                percent = kwh_left / kwh_add
                length = min(round(((length * percent) / 5) + 0.5, 0) * 5, end - start)
                end = start + length
                hours = length / 60
                kwh = self.car_charging_rate[car_n] * hours
                kwh_add = min(kwh * self.car_charging_loss, kwh_left)
                kwh = kwh_add / self.car_charging_loss

            # Work out charging amounts
            if kwh > 0:
                car_soc = self.dp3(car_soc + kwh_add)
                new_slot = {}
                new_slot["start"] = start
                new_slot["end"] = end
                new_slot["kwh"] = self.dp3(kwh)
                new_slot["average"] = window["average"]
                new_slot["cost"] = self.dp2(new_slot["average"] * kwh)
                plan.append(new_slot)

        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def add_now_to_octopus_slot(self, octopus_slots, now_utc):
        """
        For intelligent charging, add in if the car is charging now as a low rate slot (workaround for Ohme)
        """
        for car_n in range(self.num_cars):
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

    def load_saving_slot(self, octopus_saving_slots, export=False, rate_replicate={}):
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
                        self.load_scaling_dynamic[minute] = self.load_scaling_saving
                    rate_replicate[minute] = "saving"

    def decode_octopus_slot(self, slot, raw=False):
        """
        Decode IOG slot
        """
        if "start" in slot:
            start = datetime.strptime(slot["start"], TIME_FORMAT)
            end = datetime.strptime(slot["end"], TIME_FORMAT)
        else:
            start = datetime.strptime(slot["startDtUtc"], TIME_FORMAT_OCTOPUS)
            end = datetime.strptime(slot["endDtUtc"], TIME_FORMAT_OCTOPUS)

        source = slot.get("source", "")
        location = slot.get("location", "")

        start_minutes = self.minutes_to_time(start, self.midnight_utc)
        end_minutes = self.minutes_to_time(end, self.midnight_utc)
        org_minutes = end_minutes - start_minutes

        # Cap slot times into the forecast itself
        if not raw:
            start_minutes = max(start_minutes, 0)
            end_minutes = max(min(end_minutes, self.forecast_minutes + self.minutes_now), start_minutes)

        if start_minutes == end_minutes:
            return 0, 0, 0, source, location

        cap_minutes = end_minutes - start_minutes
        cap_hours = cap_minutes / 60

        # The load expected is stored in chargeKwh for the period in use
        if "charge_in_kwh" in slot:
            kwh = abs(float(slot.get("charge_in_kwh", 0.0)))
        else:
            kwh = abs(float(slot.get("chargeKwh", 0.0)))

        if not kwh:
            kwh = self.car_charging_rate[0] * cap_hours
        else:
            kwh = kwh * cap_minutes / org_minutes

        return start_minutes, end_minutes, kwh, source, location

    def load_octopus_slots(self, octopus_slots):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []

        for slot in octopus_slots:
            start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(slot)
            if (end_minutes > start_minutes) and (end_minutes > self.minutes_now) and (not location or location == "AT_HOME"):
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
                    state="",
                    attributes={
                        "friendly_name": "Predbat car charge start time car" + postfix,
                        "timestamp": None,
                        "minutes_to": self.forecast_minutes,
                        "state_class": None,
                        "unit_of_measurement": None,
                        "device_class": "timestamp",
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
                minutes_to = max(window["start"] - self.minutes_now, 0)
                self.dashboard_item(
                    self.prefix + ".car_charging_start" + postfix,
                    state=car_start_time_str,
                    attributes={
                        "friendly_name": "Predbat car charge start time car" + postfix,
                        "timestamp": car_startt.strftime(TIME_FORMAT),
                        "minutes_to": minutes_to,
                        "state_class": None,
                        "unit_of_measurement": None,
                        "device_class": "timestamp",
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
                rate_high_minutes_to_start = max(rate_high_start - self.minutes_now, 0)
                rate_high_minutes_to_end = max(rate_high_end - self.minutes_now, 0)

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
                            "minutes_to": rate_high_minutes_to_start,
                            "rate": self.dp2(rate_high_average),
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
                            "minutes_to": rate_high_minutes_to_end,
                            "rate": self.dp2(rate_high_average),
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_cost",
                        state=self.dp2(rate_high_average),
                        attributes={
                            "friendly_name": "Next high export rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
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
                            "rate": self.dp2(rate_high_average),
                            "minutes_to": rate_high_minutes_to_start,
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
                            "rate": self.dp2(rate_high_average),
                            "minutes_to": rate_high_minutes_to_end,
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_cost_2",
                        state=self.dp2(rate_high_average),
                        attributes={
                            "friendly_name": "Next+1 high export rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
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
                attributes={
                    "date": None,
                    "friendly_name": "Next high export rate start",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_end",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next high export rate end",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_cost",
                state=self.dp2(self.rate_export_average),
                attributes={
                    "friendly_name": "Next high export rate cost",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
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
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 high export rate start",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_end_2",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 high export rate end",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_cost_2",
                state=self.dp2(self.rate_export_average),
                attributes={
                    "friendly_name": "Next+1 high export rate cost",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
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

        for minute in range(self.forecast_minutes + self.minutes_now + 48 * 60):
            if minute in rates:
                rate = rates[minute]
            rate_array.append(rate)

        # Work out the min rate going forward
        for minute in range(self.minutes_now, self.forecast_minutes + 24 * 60 + self.minutes_now):
            rate_min_forward[minute] = min(rate_array[minute:])

        self.log("Rate min forward looking: now {} at end of forecast {}".format(self.dp2(rate_min_forward[self.minutes_now]), self.dp2(rate_min_forward[self.forecast_minutes])))

        return rate_min_forward

    def rate_scan_window(self, rates, rate_low_min_window, threshold_rate, find_high, return_raw=False):
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
                if (return_raw or (rate_low_end > self.minutes_now)) and (rate_low_end - rate_low_start) >= rate_low_min_window:
                    if rate_low_average < lowest:
                        lowest = rate_low_average
                    if rate_low_average > highest:
                        highest = rate_low_average

                    found_rates.append(window)
                minute = rate_low_end
            else:
                break

        return found_rates, lowest, highest

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
                start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(slot, raw=True)

                # Ignore bump-charge slots as their cost won't change
                if source != "bump-charge" and (not location or location == "AT_HOME"):
                    # Round slots to 30 minute boundary
                    start_minutes = int(round(start_minutes / 30, 0) * 30)
                    end_minutes = int(round(end_minutes / 30, 0) * 30)

                    self.log(
                        "Octopus Intelligent slot at {}-{} assumed price {} amount {} kWh location {} source {}".format(
                            self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), self.rate_min, kwh, location, source
                        )
                    )
                    for minute in range(start_minutes, end_minutes):
                        if minute >= (-24 * 60) and minute < self.forecast_minutes:
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
        self.rate_gas_min, self.rate_gas_max, self.rate_gas_average, self.rate_gas_min_minute, self.rate_gas_max_minute = self.rate_minmax(rates)

        if print:
            self.log("Gas rates min {} max {} average {}".format(self.rate_gas_min, self.rate_gas_max, self.rate_gas_average))

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
                rate_low_minutes_to_start = max(rate_low_start - self.minutes_now, 0)
                rate_low_minutes_to_end = max(rate_low_end - self.minutes_now, 0)

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
                            "minutes_to": rate_low_minutes_to_start,
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": self.dp2(rate_low_average),
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_end",
                        state=rate_low_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_end_date.strftime(TIME_FORMAT),
                            "minutes_to": rate_low_minutes_to_end,
                            "friendly_name": "Next low rate end",
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": self.dp2(rate_low_average),
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_cost",
                        state=self.dp2(rate_low_average),
                        attributes={
                            "friendly_name": "Next low rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
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
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": self.dp2(rate_low_average),
                            "minutes_to": rate_low_minutes_to_start,
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_end_2",
                        state=rate_low_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_end_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 low rate end",
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": self.dp2(rate_low_average),
                            "minutes_to": rate_low_minutes_to_end,
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_cost_2",
                        state=rate_low_average,
                        attributes={
                            "friendly_name": "Next+1 low rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
                    )
                window_n += 1

        self.log("Low import rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.low_rates and not SIMULATE:
            self.log("No low rate period found")
            self.dashboard_item(
                self.prefix + ".low_rate_start",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next low rate start",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_end",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next low rate end",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_cost",
                state=self.rate_average,
                attributes={"friendly_name": "Next low rate cost", "state_class": "measurement", "unit_of_measurement": self.currency_symbols[1], "icon": "mdi:currency-usd"},
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
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 low rate start",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_end_2",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 low rate end",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_cost_2",
                state=self.rate_average,
                attributes={"friendly_name": "Next+1 low rate cost", "state_class": "measurement", "unit_of_measurement": self.currency_symbols[1], "icon": "mdi:currency-usd"},
            )

    def adjust_symbol(self, adjust_type):
        """
        Returns an HTML symbol based on the adjust rate type.

        Parameters:
        - adjust_type (str): The type of adjustment.

        Returns:
        - symbol (str): The symbol corresponding to the adjust_type.
        """
        symbol = ""
        if adjust_type:
            if adjust_type == "offset":
                symbol = "? &#8518;"
            elif adjust_type == "future":
                symbol = "? &#x2696;"
            elif adjust_type == "user":
                symbol = "&#61;"
            elif adjust_type == "increment":
                symbol = "&#177;"
            elif adjust_type == "saving":
                symbol = "&dollar;"
            else:
                symbol = "?"
        return symbol

    def car_charge_slot_kwh(self, minute_start, minute_end):
        """
        Work out car charging amount in KWh for given 30-minute slot
        """
        car_charging_kwh = 0.0
        if self.num_cars > 0:
            for car_n in range(self.num_cars):
                for window in self.car_charging_slots[car_n]:
                    start = window["start"]
                    end = window["end"]
                    kwh = self.dp2(window["kwh"]) / (end - start)
                    for minute_offset in range(minute_start, minute_end, PREDICT_STEP):
                        if minute_offset >= start and minute_offset < end:
                            car_charging_kwh += kwh * PREDICT_STEP
            car_charging_kwh = self.dp2(car_charging_kwh)
        return car_charging_kwh

    def hit_car_window(self, window_start, window_end):
        """
        Does this window intersect a car charging window?
        """
        if self.num_cars > 0:
            for car_n in range(self.num_cars):
                for window in self.car_charging_slots[car_n]:
                    start = window["start"]
                    end = window["end"]
                    if end > window_start and start < window_end:
                        return True
        return False

    def get_html_plan_header(self, plan_debug):
        """
        Returns the header row for the HTML plan.
        """
        html = ""
        html += "<tr>"
        html += "<td><b>Time</b></td>"
        if plan_debug:
            html += "<td><b>Import {} (w/loss)</b></td>".format(self.currency_symbols[1])
            html += "<td><b>Export {} (w/loss)</b></td>".format(self.currency_symbols[1])
        else:
            html += "<td><b>Import {}</b></td>".format(self.currency_symbols[1])
            html += "<td><b>Export {}</b></td>".format(self.currency_symbols[1])
        html += "<td><b>State</b></td><td></td>"  # state can potentially be two cells for charging and discharging in the same slot
        html += "<td><b>Limit %</b></td>"
        if plan_debug:
            html += "<td><b>PV kWh (10%)</b></td>"
            html += "<td><b>Load kWh (10%)</b></td>"
        else:
            html += "<td><b>PV kWh</b></td>"
            html += "<td><b>Load kWh</b></td>"
        if self.num_cars > 0:
            html += "<td><b>Car kWh</b></td>"
        if self.iboost_enable:
            html += "<td><b>iBoost kWh</b></td>"
        html += "<td><b>SOC %</b></td>"
        html += "<td><b>Cost</b></td>"
        html += "<td><b>Total</b></td>"
        if self.carbon_enable:
            html += "<td><b>CO2 g/kWh</b></td>"
            html += "<td><b>CO2 kg</b></td>"
        html += "</tr>"
        return html

    def publish_html_plan(self, pv_forecast_minute_step, pv_forecast_minute_step10, load_minutes_step, load_minutes_step10, end_record):
        """
        Publish the current plan in HTML format
        """
        plan_debug = self.get_arg("plan_debug")
        html = "<table>"
        html += "<tr>"
        html += "<td colspan=10> Plan starts: {} last updated: {} version: {}</td>".format(
            self.now_utc.strftime("%Y-%m-%d %H:%M"), self.now_utc_real.strftime("%H:%M:%S"), THIS_VERSION
        )
        html += "</tr>"
        html += self.get_html_plan_header(plan_debug)
        minute_now_align = int(self.minutes_now / 30) * 30
        end_plan = min(end_record, self.forecast_minutes) + minute_now_align
        rowspan = 0
        in_span = False
        start_span = False
        for minute in range(minute_now_align, end_plan, 30):
            minute_relative = minute - self.minutes_now
            minute_relative_start = max(minute_relative, 0)
            minute_start = minute_relative_start + self.minutes_now
            minute_relative_end = minute_relative + 30
            minute_end = minute_relative_end + self.minutes_now
            minute_relative_slot_end = minute_relative_end
            minute_timestamp = self.midnight_utc + timedelta(minutes=(minute_relative_start + self.minutes_now))
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

            for try_minute in range(minute_start, minute_end, PREDICT_STEP):
                charge_window_n = self.in_charge_window(self.charge_window_best, try_minute)
                if charge_window_n >= 0:
                    break

            for try_minute in range(minute_start, minute_end, PREDICT_STEP):
                discharge_window_n = self.in_charge_window(self.discharge_window_best, try_minute)
                if discharge_window_n >= 0:
                    break

            start_span = False
            if in_span:
                rowspan = max(rowspan - 1, 0)
                if rowspan == 0:
                    in_span = False

            if charge_window_n >= 0 and not in_span:
                rowspan = int((self.charge_window_best[charge_window_n]["end"] - minute) / 30)
                if rowspan > 1 and (discharge_window_n < 0):
                    in_span = True
                    start_span = True
                    minute_relative_end = self.charge_window_best[charge_window_n]["end"] - minute_now_align
                else:
                    rowspan = 0

            if discharge_window_n >= 0 and not in_span:
                rowspan = int((self.discharge_window_best[discharge_window_n]["end"] - minute) / 30)
                start = self.discharge_window_best[discharge_window_n]["start"]
                if start <= minute and rowspan > 1 and (charge_window_n < 0):
                    in_span = True
                    start_span = True
                    minute_relative_end = self.discharge_window_best[discharge_window_n]["end"] - minute_now_align
                else:
                    rowspan = 0

            pv_forecast = 0
            load_forecast = 0
            pv_forecast10 = 0
            load_forecast10 = 0
            for offset in range(minute_relative_start, minute_relative_slot_end, PREDICT_STEP):
                pv_forecast += pv_forecast_minute_step.get(offset, 0.0)
                load_forecast += load_minutes_step.get(offset, 0.0)
                pv_forecast10 += pv_forecast_minute_step10.get(offset, 0.0)
                load_forecast10 += load_minutes_step10.get(offset, 0.0)

            pv_forecast = self.dp2(pv_forecast)
            load_forecast = self.dp2(load_forecast)
            pv_forecast10 = self.dp2(pv_forecast10)
            load_forecast10 = self.dp2(load_forecast10)

            soc_percent = calc_percent_limit(self.predict_soc_best.get(minute_relative_start, 0.0), self.soc_max)
            soc_percent_end = calc_percent_limit(self.predict_soc_best.get(minute_relative_slot_end, 0.0), self.soc_max)
            soc_percent_end_window = calc_percent_limit(self.predict_soc_best.get(minute_relative_end, 0.0), self.soc_max)
            soc_percent_max = max(soc_percent, soc_percent_end)
            soc_percent_min = min(soc_percent, soc_percent_end)
            soc_percent_max_window = max(soc_percent, soc_percent_end_window)
            soc_percent_min_window = min(soc_percent, soc_percent_end_window)
            soc_change = self.predict_soc_best.get(minute_relative_slot_end, 0.0) - self.predict_soc_best.get(minute_relative_start, 0.0)
            metric_start = self.predict_metric_best.get(minute_relative_start, 0.0)
            metric_end = self.predict_metric_best.get(minute_relative_slot_end, metric_start)
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
            if minute in self.manual_idle_times:
                state += " &#8526;"

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

            pv_forecast = str(pv_forecast)
            if plan_debug and pv_forecast10 > 0.0:
                pv_forecast += " (%s)" % (str(pv_forecast10))

            if load_forecast >= 0.5:
                load_color = "#F18261"
            elif load_forecast >= 0.25:
                load_color = "#FFFF00"
            elif load_forecast > 0.0:
                load_color = "#AAFFAA"

            load_forecast = str(load_forecast)
            if plan_debug and load_forecast10 > 0.0:
                load_forecast += " (%s)" % (str(load_forecast10))

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
                        state = "FrzChrg&rarr;"
                        state_color = "#EEEEEE"
                        limit_percent = soc_percent
                    elif limit_percent == soc_percent_min_window:
                        state = "HoldChrg&rarr;"
                        state_color = "#34DBEB"
                    elif limit_percent < soc_percent_min_window:
                        state = "NoChrg&searr;"
                        state_color = "#FFFFFF"
                    else:
                        state = "Chrg&nearr;"
                        state_color = "#3AEE85"

                    if self.charge_window_best[charge_window_n]["start"] in self.manual_charge_times:
                        state += " &#8526;"
                    elif self.charge_window_best[charge_window_n]["start"] in self.manual_freeze_charge_times:
                        state += " &#8526;"
                    show_limit = str(limit_percent)
            else:
                if discharge_window_n >= 0:
                    start = self.discharge_window_best[discharge_window_n]["start"]
                    if start > minute:
                        soc_change_this = self.predict_soc_best.get(max(start - self.minutes_now, 0), 0.0) - self.predict_soc_best.get(minute_relative_start, 0.0)
                        if soc_change_this >= 0:
                            state = " &nearr;"
                        elif soc_change_this < 0:
                            state = " &searr;"
                        else:
                            state = " &rarr;"
                        state_color = "#FFFFFF"
                        show_limit = ""

            if discharge_window_n >= 0:
                limit = self.discharge_limits_best[discharge_window_n]
                if limit < 100 and limit > soc_percent_max_window:
                    if state == soc_sym:
                        state = ""
                    if state:
                        state += "</td><td bgcolor=#AAAAAA>"  # charging and freeze discharging in same slot, split the state into two
                        split = True
                    else:
                        state_color = "#AAAAAA"
                    state += "FrzDis&rarr;"
                    show_limit = str(int(limit))
                elif limit < 100:
                    if state == soc_sym:
                        state = ""
                    if state:
                        state += "</td><td bgcolor=#FFFF00>"  # charging and discharging in the same slot
                        split = True
                    else:
                        state_color = "#FFFF00"
                    state += "Dis&searr;"
                    show_limit = str(int(limit))

                if self.discharge_window_best[discharge_window_n]["start"] in self.manual_discharge_times:
                    state += " &#8526;"
                elif self.discharge_window_best[discharge_window_n]["start"] in self.manual_freeze_discharge_times:
                    state += " &#8526;"

            # Import and export rates -> to string
            adjust_type = self.rate_import_replicated.get(minute, None)
            adjust_symbol = self.adjust_symbol(adjust_type)
            if adjust_symbol:
                rate_str_import = "<i>%02.02f %s</i>" % (rate_value_import, adjust_symbol)
            else:
                rate_str_import = "%02.02f" % (rate_value_import)

            if plan_debug:
                rate_str_import += " (%02.02f)" % (rate_value_import / self.battery_loss / self.inverter_loss + self.metric_battery_cycle)

            if charge_window_n >= 0:
                rate_str_import = "<b>" + rate_str_import + "</b>"

            adjust_type = self.rate_export_replicated.get(minute, None)
            adjust_symbol = self.adjust_symbol(adjust_type)
            if adjust_symbol:
                rate_str_export = "<i>%02.02f %s</i>" % (rate_value_export, adjust_symbol)
            else:
                rate_str_export = "%02.02f" % (rate_value_export)

            if plan_debug:
                rate_str_export += " (%02.02f)" % (rate_value_export * self.battery_loss_discharge * self.inverter_loss - self.metric_battery_cycle)

            if discharge_window_n >= 0:
                rate_str_export = "<b>" + rate_str_export + "</b>"

            # Total cost at start of slot, add leading minus if negative
            if metric_start >= 0:
                total_str = self.currency_symbols[0] + "%02.02f" % (metric_start / 100.0)
            else:
                total_str = "-" + self.currency_symbols[0] + "%02.02f" % (abs(metric_start) / 100.0)

            # Cost predicted for this slot
            if metric_change >= 10.0:
                cost_str = "+%d %s " % (int(metric_change), self.currency_symbols[1])
                cost_str += " &nearr;"
                cost_color = "#F18261"
            elif metric_change >= 0.5:
                cost_str = "+%d %s " % (int(metric_change), self.currency_symbols[1])
                cost_str += " &nearr;"
                cost_color = "#FFFF00"
            elif metric_change <= -0.5:
                cost_str = "-%d %s " % (int(abs(metric_change)), self.currency_symbols[1])
                cost_str += " &searr;"
                cost_color = "#3AEE85"
            else:
                cost_str = " &rarr;"
                cost_color = "#FFFFFF"

            # Car charging?
            if self.num_cars > 0:
                car_charging_kwh = self.car_charge_slot_kwh(minute_start, minute_end)
                if car_charging_kwh > 0.0:
                    car_charging_str = str(car_charging_kwh)
                    car_color = "FFFF00"
                else:
                    car_charging_str = ""
                    car_color = "#FFFFFF"

            # iBoost
            iboost_amount_str = ""
            iboost_color = "#FFFFFF"
            if self.iboost_enable:
                iboost_slot_end = minute_relative_slot_end
                iboost_amount = self.predict_iboost_best.get(minute_relative_start, 0)
                iboost_amount_end = self.predict_iboost_best.get(minute_relative_slot_end, 0)
                iboost_amount_prev = self.predict_iboost_best.get(minute_relative_slot_end - PREDICT_STEP, 0)
                if iboost_amount_prev > iboost_amount_end:
                    # Reset condition, scale to full slot size as last 5 minutes is missing in data
                    iboost_change = (
                        (iboost_amount_prev - iboost_amount)
                        * (minute_relative_slot_end - minute_relative_start)
                        / (minute_relative_slot_end - PREDICT_STEP - minute_relative_start)
                    )
                else:
                    iboost_change = max(iboost_amount_end - iboost_amount, 0.0)
                iboost_amount_str = str(self.dp2(iboost_change))
                if iboost_change > 0:
                    iboost_color = "#FFFF00"

            if self.carbon_enable:
                # Work out carbon intensity and carbon use
                carbon_amount = self.predict_carbon_best.get(minute_relative_start, 0)
                carbon_amount_end = self.predict_carbon_best.get(minute_relative_slot_end, 0)
                carbon_change = carbon_amount_end - carbon_amount
                carbon_change = self.dp2(carbon_change)
                carbon_intensity = self.dp0(self.carbon_intensity.get(minute_relative_start, 0))

                if carbon_intensity >= 450:
                    carbon_intensity_color = "#8B0000"
                elif carbon_intensity >= 290:
                    carbon_intensity_color = "#FF0000"
                elif carbon_intensity >= 200:
                    carbon_intensity_color = "#FFA500"
                elif carbon_intensity >= 120:
                    carbon_intensity_color = "#FFFF00"
                elif carbon_intensity >= 40:
                    carbon_intensity_color = "#90EE90"
                else:
                    carbon_intensity_color = "#00FF00"

                carbon_str = str(self.dp2(carbon_amount / 1000.0))
                if carbon_change >= 10:
                    carbon_str += " &nearr;"
                    carbon_color = "#FFAA00"
                elif carbon_change <= -10:
                    carbon_str += " &searr;"
                    carbon_color = "#00FF00"
                else:
                    carbon_str += " &rarr;"
                    carbon_color = "#FFFFFF"

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
            if self.carbon_enable:
                html += "<td bgcolor=" + carbon_intensity_color + ">" + str(carbon_intensity) + " </td>"
                html += "<td bgcolor=" + carbon_color + "> " + str(carbon_str) + " </td>"
            html += "</tr>"
        html += "</table>"
        self.dashboard_item(self.prefix + ".plan_html", state="", attributes={"html": html, "friendly_name": "Plan in HTML", "icon": "mdi:web-box"})

    def publish_rates(self, rates, export, gas=False):
        """
        Publish the rates for charts
        Create rates/time every 30 minutes
        """
        rates_time = {}
        for minute in range(-24 * 60, self.minutes_now + self.forecast_minutes + 24 * 60, 30):
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            rates_time[stamp] = self.dp2(rates[minute])

        if export:
            self.publish_rates_export()
        elif gas:
            pass
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
                        "results": self.filtered_times(rates_time),
                        "friendly_name": "Export rates",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
            elif gas:
                self.dashboard_item(
                    self.prefix + ".rates_gas",
                    state=self.dp2(rates[self.minutes_now]),
                    attributes={
                        "min": self.dp2(self.rate_gas_min),
                        "max": self.dp2(self.rate_gas_max),
                        "average": self.dp2(self.rate_gas_average),
                        "results": self.filtered_times(rates_time),
                        "friendly_name": "Gas rates",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
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
                        "results": self.filtered_times(rates_time),
                        "friendly_name": "Import rates",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
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
        day_carbon_time = {}
        carbon_g = 0

        for minute in range(self.minutes_now):
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

            if self.carbon_enable:
                carbon_g += self.carbon_history.get(minute_back, 0) * energy
                carbon_g -= self.carbon_history.get(minute_back, 0) * energy_export

            if (minute % 5) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = self.dp2(day_cost)
                day_cost_time_import[stamp] = self.dp2(day_cost_import)
                day_cost_time_export[stamp] = self.dp2(day_cost_export)
                day_carbon_time[stamp] = self.dp2(carbon_g)

        if not SIMULATE:
            self.dashboard_item(
                self.prefix + ".cost_today",
                state=self.dp2(day_cost),
                attributes={
                    "results": self.filtered_times(day_cost_time),
                    "friendly_name": "Cost so far today",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
            if self.carbon_enable:
                self.dashboard_item(
                    self.prefix + ".carbon_today",
                    state=self.dp2(carbon_g),
                    attributes={
                        "results": self.filtered_times(day_carbon_time),
                        "friendly_name": "Carbon today so far",
                        "state_class": "measurement",
                        "unit_of_measurement": "g",
                        "icon": "mdi:carbon-molecule",
                    },
                )
            self.dashboard_item(
                self.prefix + ".cost_today_import",
                state=self.dp2(day_cost_import),
                attributes={
                    "results": self.filtered_times(day_cost_time_import),
                    "friendly_name": "Cost so far today import",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(
                self.prefix + ".cost_today_export",
                state=self.dp2(day_cost_export),
                attributes={
                    "results": self.filtered_times(day_cost_time_export),
                    "friendly_name": "Cost so far today export",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
        self.log(
            "Todays energy import {} kWh export {} kWh cost {} {} import {} {} export {} {} carbon {} kg".format(
                self.dp2(day_energy),
                self.dp2(day_energy_export),
                self.dp2(day_cost),
                self.currency_symbols[1],
                self.dp2(day_cost_import),
                self.currency_symbols[1],
                self.dp2(day_cost_export),
                self.currency_symbols[1],
                self.dp2(carbon_g / 1000.0),
            )
        )
        return day_cost, carbon_g

    def publish_discharge_limit(self, discharge_window, discharge_limits, best):
        """
        Create entity to chart discharge limit

        Args:
            discharge_window (list): List of dictionaries representing the discharge window.
            discharge_limits (list): List of discharge limits in percent.
            best (bool): Flag indicating whether to push as base or as best

        Returns:
            None
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
            discharge_start_str = ""
            discharge_end_str = ""
            discharge_start_date = None
            discharge_end_date = None
            discharge_average = None
            discharge_start_in_minutes = self.forecast_minutes
            discharge_end_in_minutes = self.forecast_minutes

            if discharge_window and (discharge_window[0]["end"] < (24 * 60 + self.minutes_now)):
                discharge_start_minutes = discharge_window[0]["start"]
                discharge_end_minutes = discharge_window[0]["end"]
                discharge_average = discharge_window[0].get("average", None)
                discharge_start_in_minutes = max(discharge_start_minutes - self.minutes_now, 0)
                discharge_end_in_minutes = max(discharge_end_minutes - self.minutes_now, 0)

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
                        "results": self.filtered_times(discharge_limit_time_kw),
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
                        "results": self.filtered_times(discharge_limit_time),
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
                        "minutes_to": discharge_start_in_minutes,
                        "timestamp": discharge_start_date,
                        "friendly_name": "Predicted discharge start time best",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": discharge_average,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_discharge_end",
                    state=discharge_end_str,
                    attributes={
                        "minutes_to": discharge_end_in_minutes,
                        "timestamp": discharge_end_date,
                        "friendly_name": "Predicted discharge end time best",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": discharge_average,
                    },
                )
            else:
                self.dashboard_item(
                    self.prefix + ".discharge_limit_kw",
                    state=self.dp2(discharge_limit_soc),
                    attributes={
                        "results": self.filtered_times(discharge_limit_time_kw),
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
                        "results": self.filtered_times(discharge_limit_time),
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
                        "minutes_to": discharge_start_in_minutes,
                        "timestamp": discharge_start_date,
                        "friendly_name": "Predicted discharge start time",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": discharge_average,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".discharge_end",
                    state=discharge_end_str,
                    attributes={
                        "minutes_to": discharge_end_in_minutes,
                        "timestamp": discharge_end_date,
                        "friendly_name": "Predicted discharge end time",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": discharge_average,
                    },
                )

    def publish_charge_limit(self, charge_limit, charge_window, charge_limit_percent, best=False, soc={}):
        """
        Create entity to chart charge limit

        Parameters:

        - charge_limit (list): List of charge limits in kWh
        - charge_window (list): List of charge window dictionaries
        - charge_limit_percent (list): List of charge limit percentages
        - best (bool, optional): Flag indicating if we publish as base or as best
        - soc (dict, optional): Dictionary of the predicted SOC over time

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
            charge_start_str = ""
            charge_end_str = ""
            charge_start_date = None
            charge_end_date = None
            charge_start_in_minutes = self.forecast_days * 24 * 60
            charge_end_in_minutes = self.forecast_days * 24 * 60

            if charge_limit and charge_window[0]["end"] <= (24 * 60 + self.minutes_now):
                charge_limit_first = charge_limit[0]
                charge_limit_percent_first = charge_limit_percent[0]
                charge_start_minutes = charge_window[0]["start"]
                charge_end_minutes = charge_window[0]["end"]
                charge_average_first = charge_window[0].get("average", None)
                charge_start_in_minutes = max(charge_start_minutes - self.minutes_now, 0)
                charge_end_in_minutes = max(charge_end_minutes - self.minutes_now, 0)

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
                        "results": self.filtered_times(charge_limit_time_kw),
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
                        "results": self.filtered_times(charge_limit_time),
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
                        "minutes_to": charge_start_in_minutes,
                        "friendly_name": "Predicted charge start time best",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": charge_average_first,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_charge_end",
                    state=charge_end_str,
                    attributes={
                        "timestamp": charge_end_date,
                        "minutes_to": charge_end_in_minutes,
                        "friendly_name": "Predicted charge end time best",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": charge_average_first,
                    },
                )
            else:
                self.dashboard_item(
                    self.prefix + ".charge_limit_kw",
                    state=self.dp2(charge_limit_first),
                    attributes={
                        "results": self.filtered_times(charge_limit_time_kw),
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
                        "results": self.filtered_times(charge_limit_time),
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
                        "minutes_to": charge_start_in_minutes,
                        "friendly_name": "Predicted charge start time",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": charge_average_first,
                    },
                )
                self.dashboard_item(
                    self.prefix + ".charge_end",
                    state=charge_end_str,
                    attributes={
                        "timestamp": charge_end_date,
                        "minutes_to": charge_end_in_minutes,
                        "friendly_name": "Predicted charge end time",
                        "device_class": "timestamp",
                        "state_class": None,
                        "unit_of_measurement": None,
                        "icon": "mdi:table-clock",
                        "rate": charge_average_first,
                    },
                )

    def reset(self):
        """
        Init stub
        """
        global PRED_GLOBAL
        PRED_GLOBAL["dict"] = None

        self.define_service_list()
        self.stop_thread = False
        self.currency_symbols = self.args.get("currency_symbols", "£p")
        self.pool = None
        self.watch_list = []
        self.restart_active = False
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""
        self.manual_charge_times = []
        self.manual_discharge_times = []
        self.manual_freeze_charge_times = []
        self.manual_freeze_discharge_times = []
        self.manual_idle_times = []
        self.manual_all_times = []
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
        self.midnight_utc = None
        self.difference_minutes = 0
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
        self.rate_gas = {}
        self.rate_slots = []
        self.low_rates = []
        self.high_export_rates = []
        self.cost_today_sofar = 0
        self.carbon_today_sofar = 0
        self.octopus_slots = []
        self.car_charging_slots = []
        self.reserve = 0
        self.reserve_current = 0
        self.battery_loss = 1.0
        self.battery_loss_discharge = 1.0
        self.inverter_loss = 1.0
        self.inverter_hybrid = True
        self.inverter_soc_reset = False
        self.inverter_set_charge_before = True
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
        self.rate_gas_min = 0
        self.rate_gas_max = 0
        self.rate_gas_average = 0
        self.rate_gas_min_minute = 0
        self.rate_gas_max_minute = 0
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
        self.car_charging_soc_next = [None]
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
        self.battery_rate_max_scaling = 1.0
        self.battery_rate_max_scaling_discharge = 1.0
        self.charge_rate_now = 0
        self.discharge_rate_now = 0
        self.car_charging_hold = False
        self.car_charging_manual_soc = False
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
        self.load_scaling_dynamic = {}
        self.battery_charge_power_curve = {}
        self.battery_charge_power_curve_auto = False
        self.battery_discharge_power_curve = {}
        self.battery_discharge_power_curve_auto = False
        self.computed_charge_curve = False
        self.computed_discharge_curve = False
        self.isCharging = False
        self.isDischarging = False
        self.savings_today_predbat = 0.0
        self.savings_today_predbat_soc = 0.0
        self.savings_today_pvbat = 0.0
        self.savings_today_actual = 0.0
        self.yesterday_load_step = {}
        self.yesterday_pv_step = {}

    def optimise_charge_limit_price(
        self,
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        record_discharge_windows,
        try_charge_limit,
        charge_window,
        discharge_window,
        discharge_limits,
        end_record=None,
        region_start=None,
        region_end=None,
        fast=False,
        quiet=False,
        best_metric=9999999,
        best_cost=0,
        best_keep=0,
        best_soc_min=None,
        best_price_charge=None,
        best_price_discharge=None,
        best_cycle=0,
        best_carbon=0,
        tried_list=None,
    ):
        """
        Pick an import price threshold which gives the best results
        """
        loop_price = price_set[-1]
        best_price = loop_price
        try_discharge = discharge_limits.copy()
        best_limits = try_charge_limit.copy()
        best_discharge = try_discharge.copy()
        if best_soc_min is None:
            best_soc_min = self.reserve
        if best_price_charge is None:
            best_price_charge = price_set[-1]
        if best_price_discharge is None:
            best_price_discharge = price_set[0]
        step = PREDICT_STEP
        if fast:
            step = 30
        if tried_list is None:
            tried_list = {}

        if region_start:
            region_txt = "Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end))
        else:
            region_txt = "All regions"

        # Do we loop on discharge?
        if self.calculate_best_discharge and self.calculate_discharge_first:
            discharge_enable_options = [False, True]
        else:
            discharge_enable_options = [False]

        # Most expensive first
        all_prices = price_set[::] + [self.dp1(price_set[-1] - 1)]
        if not quiet:
            self.log("All prices {}".format(all_prices))
            if region_start:
                self.log("Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end)))
        window_prices = {}
        window_prices_discharge = {}
        for loop_price in all_prices:
            for modulo in [2, 3, 4, 6, 8, 16, 32]:
                for divide in [1, 2, 3, 4, 8, 16, 32, 48, 96]:
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

                    # Sort for print out
                    all_n.sort()
                    all_d.sort()

                    for discharge_enable in discharge_enable_options:
                        # This price band setting for charge
                        try_charge_limit = best_limits.copy()
                        for window_n in range(record_charge_windows):
                            if window_n >= len(charge_window):
                                continue

                            if region_start and (charge_window[window_n]["start"] > region_end or charge_window[window_n]["end"] < region_start):
                                continue

                            if window_n in all_n:
                                if window_prices[window_n] > highest_price_charge:
                                    highest_price_charge = window_prices[window_n]
                                try_charge_limit[window_n] = self.soc_max
                            else:
                                try_charge_limit[window_n] = 0

                        # Try discharge on/off
                        try_discharge = best_discharge.copy()
                        if discharge_enable:
                            if not all_d:
                                continue

                            for window_n in range(record_discharge_windows):
                                if window_n >= len(discharge_window):
                                    continue

                                if region_start and (discharge_window[window_n]["start"] > region_end or discharge_window[window_n]["end"] < region_start):
                                    continue

                                if window_n in all_d:
                                    hit_charge = self.hit_charge_window(
                                        self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                                    )
                                    if not self.calculate_discharge_oncharge and hit_charge >= 0 and try_charge_limit[hit_charge] > 0.0:
                                        continue
                                    if not self.car_charging_from_battery and self.hit_car_window(
                                        self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                                    ):
                                        continue
                                    if window_prices_discharge[window_n] < lowest_price_discharge:
                                        lowest_price_discharge = window_prices_discharge[window_n]
                                    try_discharge[window_n] = 0

                        # Skip this one as it's the same as selected already
                        try_hash = str(try_charge_limit) + "_d_" + str(try_discharge)
                        if try_hash in tried_list:
                            if self.debug_enable and 0:
                                self.log(
                                    "Skip this optimisation with divide {} windows {} discharge windows {} discharge_enable {} as it's the same as previous ones hash {}".format(
                                        divide, all_n, all_d, discharge_enable, try_hash
                                    )
                                )
                            continue
                        tried_list[try_hash] = True

                        # Turn off debug for this sim
                        was_debug = self.debug_enable
                        self.debug_enable = False

                        # Simulate with medium PV
                        (
                            cost,
                            import_kwh_battery,
                            import_kwh_house,
                            export_kwh,
                            soc_min,
                            soc,
                            soc_min_minute,
                            battery_cycle,
                            metric_keep,
                            final_iboost,
                            final_carbon_g,
                        ) = self.run_prediction(
                            try_charge_limit,
                            charge_window,
                            discharge_window,
                            try_discharge,
                            False,
                            end_record=end_record,
                            step=step,
                        )

                        # Debug re-enable if it was on
                        self.debug_enable = was_debug

                        metric, metric10 = self.compute_metric(
                            end_record, soc, soc, cost, cost, final_iboost, final_iboost, battery_cycle, battery_cycle, metric_keep, metric_keep, final_carbon_g, final_carbon_g
                        )

                        # Optimise
                        if self.debug_enable:
                            if discharge_enable:
                                self.log(
                                    "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} windows {} discharge on {}".format(
                                        loop_price, divide, modulo, self.dp4(metric), self.dp4(metric_keep), self.dp4(soc_min), all_n, all_d
                                    )
                                )
                            else:
                                self.log(
                                    "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} windows {} discharge off".format(
                                        loop_price, divide, modulo, self.dp4(metric), self.dp4(metric_keep), self.dp4(soc_min), all_n
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
                            best_cycle = battery_cycle
                            best_carbon = final_carbon_g
                            best_soc_min = soc_min
                            best_cost = cost
                            if 1 or not quiet:
                                self.log(
                                    "Optimise all charge found best buy/sell price band {} best price threshold {} at cost {} metric {} keep {} cycle {} carbon {} cost {} limits {} discharge {}".format(
                                        loop_price,
                                        best_price_charge,
                                        self.dp4(best_cost),
                                        self.dp4(best_metric),
                                        self.dp4(best_keep),
                                        self.dp4(best_cycle),
                                        self.dp0(best_carbon),
                                        self.dp4(best_cost),
                                        best_limits,
                                        best_discharge,
                                    )
                                )
        self.log(
            "Optimise all charge {} best price threshold {} total simulations {} charges at {} at cost {} metric {} keep {} cycle {} carbon {} cost {} soc_min {} limits {} discharge {}".format(
                region_txt,
                self.dp4(best_price),
                len(tried_list),
                self.dp4(best_price_charge),
                self.dp4(best_cost),
                self.dp4(best_metric),
                self.dp4(best_keep),
                self.dp4(best_cycle),
                self.dp0(best_carbon),
                self.dp4(best_cost),
                self.dp4(best_soc_min),
                best_limits,
                best_discharge,
            )
        )
        return best_limits, best_discharge, best_price_charge, best_price_discharge, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, tried_list

    def optimise_charge_limit_price_threads(
        self,
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        record_discharge_windows,
        try_charge_limit,
        charge_window,
        discharge_window,
        discharge_limits,
        end_record=None,
        region_start=None,
        region_end=None,
        fast=False,
        quiet=False,
        best_metric=9999999,
        best_cost=0,
        best_keep=0,
        best_soc_min=None,
        best_price_charge=None,
        best_price_discharge=None,
        best_cycle=0,
        best_carbon=0,
        tried_list=None,
    ):
        """
        Pick an import price threshold which gives the best results
        """
        loop_price = price_set[-1]
        best_price = loop_price
        try_discharge = discharge_limits.copy()
        best_limits = try_charge_limit.copy()
        best_discharge = try_discharge.copy()
        if best_soc_min is None:
            best_soc_min = self.reserve
        if best_price_charge is None:
            best_price_charge = price_set[-1]
        if best_price_discharge is None:
            best_price_discharge = price_set[0]
        step = PREDICT_STEP
        if fast:
            step = 30
        if tried_list is None:
            tried_list = {}

        if region_start:
            region_txt = "Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end))
        else:
            region_txt = "All regions"

        # Do we loop on discharge?
        if self.calculate_best_discharge and self.calculate_discharge_first:
            discharge_enable = True
        else:
            discharge_enable = False

        # Most expensive first
        all_prices = price_set[::] + [self.dp1(price_set[-1] - 1)]
        if not quiet:
            self.log("All prices {}".format(all_prices))
            if region_start:
                self.log("Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end)))
        window_prices = {}
        window_prices_discharge = {}

        for loop_price in all_prices:
            pred_table = []
            for modulo in [2, 3, 4, 6, 8, 16, 32]:
                for divide in [96, 48, 32, 16, 8, 4, 3, 2, 1]:
                    all_n = []
                    all_d = []
                    divide_count_d = 0
                    highest_price_charge = price_set[-1]
                    lowest_price_discharge = price_set[0]
                    for price in price_set:
                        links = price_links[price]
                        if loop_price >= price:
                            for key in links:
                                window_n = window_index[key]["id"]
                                typ = window_index[key]["type"]
                                if typ == "c":
                                    window_prices[window_n] = price
                                    all_n.append(window_n)
                        elif discharge_enable:
                            # For prices above threshold try discharge
                            for key in links:
                                typ = window_index[key]["type"]
                                window_n = window_index[key]["id"]
                                if typ == "d":
                                    window_prices_discharge[window_n] = price
                                    if (int(divide_count_d / divide) % modulo) == 0:
                                        all_d.append(window_n)
                                    divide_count_d += 1

                    # Sort for print out
                    all_n.sort()
                    all_d.sort()

                    # This price band setting for charge
                    try_charge_limit = best_limits.copy()
                    for window_n in range(record_charge_windows):
                        if window_n >= len(try_charge_limit):
                            continue

                        if region_start and (charge_window[window_n]["start"] > region_end or charge_window[window_n]["end"] < region_start):
                            continue

                        if window_n in all_n:
                            if window_prices[window_n] > highest_price_charge:
                                highest_price_charge = window_prices[window_n]
                            try_charge_limit[window_n] = self.soc_max
                        else:
                            try_charge_limit[window_n] = 0

                    # Try discharge on/off
                    try_discharge = best_discharge.copy()
                    for window_n in range(record_discharge_windows):
                        if window_n >= len(discharge_limits):
                            continue

                        if region_start and (discharge_window[window_n]["start"] > region_end or discharge_window[window_n]["end"] < region_start):
                            continue

                        try_discharge[window_n] = 100
                        if window_n in all_d:
                            hit_charge = self.hit_charge_window(self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"])
                            if not self.calculate_discharge_oncharge and hit_charge >= 0 and try_charge_limit[hit_charge] > 0.0:
                                continue
                            if not self.car_charging_from_battery and self.hit_car_window(
                                self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                            ):
                                continue
                            if window_prices_discharge[window_n] < lowest_price_discharge:
                                lowest_price_discharge = window_prices_discharge[window_n]
                            try_discharge[window_n] = 0

                    # Skip this one as it's the same as selected already
                    try_hash = str(try_charge_limit) + "_d_" + str(try_discharge)
                    if try_hash in tried_list:
                        if self.debug_enable and 0:
                            self.log(
                                "Skip this optimisation with divide {} windows {} discharge windows {} discharge_enable {} as it's the same as previous ones hash {}".format(
                                    divide, all_n, all_d, discharge_enable, try_hash
                                )
                            )
                        continue

                    tried_list[try_hash] = True

                    pred_handle = self.launch_run_prediction_single(try_charge_limit, charge_window, discharge_window, try_discharge, False, end_record=end_record, step=step)
                    pred_item = {}
                    pred_item["handle"] = pred_handle
                    pred_item["charge_limit"] = try_charge_limit.copy()
                    pred_item["discharge_limit"] = try_discharge.copy()
                    pred_item["highest_price_charge"] = highest_price_charge
                    pred_item["lowest_price_discharge"] = lowest_price_discharge
                    pred_item["loop_price"] = loop_price
                    pred_table.append(pred_item)

            for pred in pred_table:
                handle = pred["handle"]
                try_charge_limit = pred["charge_limit"]
                try_discharge = pred["discharge_limit"]
                highest_price_charge = pred["highest_price_charge"]
                lowest_price_discharge = pred["lowest_price_discharge"]
                loop_price = pred["loop_price"]
                cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = handle.get()

                metric, metric10 = self.compute_metric(
                    end_record, soc, soc, cost, cost, final_iboost, final_iboost, battery_cycle, battery_cycle, metric_keep, metric_keep, final_carbon_g, final_carbon_g
                )

                # Optimise
                if self.debug_enable:
                    if discharge_enable:
                        self.log(
                            "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} windows {} discharge on {}".format(
                                loop_price, divide, modulo, self.dp4(metric), self.dp4(metric_keep), self.dp4(soc_min), try_charge_limit, try_discharge
                            )
                        )
                    else:
                        self.log(
                            "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} windows {} discharge off".format(
                                loop_price, divide, modulo, self.dp4(metric), self.dp4(metric_keep), self.dp4(soc_min), try_charge_limit
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
                    best_cycle = battery_cycle
                    best_carbon = final_carbon_g
                    best_soc_min = soc_min
                    best_cost = cost
                    if 1 or not quiet:
                        self.log(
                            "Optimise all charge found best buy/sell price band {} best price threshold {} at cost {} metric {} keep {} cycle {} carbon {} cost {} limits {} discharge {}".format(
                                loop_price,
                                best_price_charge,
                                self.dp4(best_cost),
                                self.dp4(best_metric),
                                self.dp4(best_keep),
                                self.dp4(best_cycle),
                                self.dp0(best_carbon),
                                self.dp4(best_cost),
                                best_limits,
                                best_discharge,
                            )
                        )

        if self.debug_enable:
            self.log(
                "Optimise all charge {} best price threshold {} total simulations {} charges at {} at cost {} metric {} keep {} cycle {} carbon {} cost {} soc_min {} limits {} discharge {}".format(
                    region_txt,
                    self.dp4(best_price),
                    len(tried_list),
                    self.dp4(best_price_charge),
                    self.dp4(best_cost),
                    self.dp4(best_metric),
                    self.dp4(best_keep),
                    self.dp4(best_cycle),
                    self.dp0(best_carbon),
                    self.dp4(best_cost),
                    self.dp4(best_soc_min),
                    best_limits,
                    best_discharge,
                )
            )
        if 0 and not region_start:
            # Simulate with medium PV
            (
                cost,
                import_kwh_battery,
                import_kwh_house,
                export_kwh,
                soc_min,
                soc,
                soc_min_minute,
                battery_cycle,
                metric_keep,
                final_iboost,
                final_carbon_g,
            ) = self.run_prediction(best_limits, charge_window, discharge_window, best_discharge, False, end_record=end_record, step=PREDICT_STEP, save="level")
        return best_limits, best_discharge, best_price_charge, best_price_discharge, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, tried_list

    def launch_run_prediction_single(self, charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step=PREDICT_STEP):
        """
        Launch a thread to run a prediction
        """
        charge_limit = copy.deepcopy(charge_limit)
        discharge_limits = copy.deepcopy(discharge_limits)
        if self.pool:
            han = self.pool.apply_async(wrapped_run_prediction_single, (charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step))
        else:
            han = DummyThread(self.prediction.thread_run_prediction_single(charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step))
        return han

    def launch_run_prediction_charge(self, loop_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
        """
        Launch a thread to run a prediction
        """
        if self.pool:
            han = self.pool.apply_async(
                wrapped_run_prediction_charge, (loop_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record)
            )
        else:
            han = DummyThread(
                self.prediction.thread_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record)
            )
        return han

    def launch_run_prediction_discharge(self, this_discharge_limit, start, window_n, try_charge_limit, charge_window, try_discharge_window, try_discharge, pv10, all_n, end_record):
        """
        Launch a thread to run a prediction
        """
        if self.pool:
            han = self.pool.apply_async(
                wrapped_run_prediction_discharge,
                (this_discharge_limit, start, window_n, try_charge_limit, charge_window, try_discharge_window, try_discharge, pv10, all_n, end_record),
            )
        else:
            han = DummyThread(
                self.prediction.thread_run_prediction_discharge(
                    this_discharge_limit, start, window_n, try_charge_limit, charge_window, try_discharge_window, try_discharge, pv10, all_n, end_record
                )
            )
        return han

    def optimise_charge_limit_swap(self, charge_limit, charge_window, discharge_window, discharge_limits, record_charge_windows, end_record=None):
        """
        Re-optimise charge limits by swapping to cheaper slots
        """
        charge_limit = copy.deepcopy(charge_limit)

        (
            cost10,
            import_kwh_battery10,
            import_kwh_house10,
            export_kwh10,
            soc_min10,
            soc10,
            soc_min_minute10,
            battery_cycle10,
            metric_keep10,
            final_iboost10,
            final_carbon_g10,
        ) = self.run_prediction(
            charge_limit,
            charge_window,
            discharge_window,
            discharge_limits,
            True,
            end_record=self.end_record,
        )
        (
            cost,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
        ) = self.run_prediction(
            charge_limit,
            charge_window,
            discharge_window,
            discharge_limits,
            False,
            end_record=self.end_record,
        )
        metric, metric10 = self.compute_metric(
            end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, battery_cycle10, metric_keep, metric_keep10, final_carbon_g, final_carbon_g10
        )

        best_soc = soc
        best_soc_min = soc_min
        best_soc_min_minute = soc_min_minute
        best_metric = metric
        best_cost = cost
        best_keep = metric_keep
        best_cycle = 0
        best_carbon = 0

        price_sorted = self.sort_window_by_price(charge_window)
        price_sorted.reverse()
        # Windows listed in price order high to low

        self.log("Starting swap optimisation with metric {}".format(metric))

        for window_n in price_sorted:
            price_n = charge_window[window_n]["average"]
            limit_n = charge_limit[window_n]
            if window_n < record_charge_windows and limit_n > 0:
                for window_swap in price_sorted[window_n + 1 :]:
                    price_swap = charge_window[window_swap]["average"]
                    limit_swap = charge_limit[window_swap]
                    if window_swap < record_charge_windows and price_swap <= price_n and limit_swap < limit_n:
                        self.log(
                            "Attempt charge window swap window {} price {} limit {} -> window {} price {} limit {}".format(
                                window_n, price_n, limit_n, window_swap, price_swap, limit_swap
                            )
                        )
                        try_limit = charge_limit.copy()
                        try_value = try_limit[window_swap]
                        try_limit[window_swap] = try_limit[window_n]
                        try_limit[window_n] = try_value

                        (
                            cost10,
                            import_kwh_battery10,
                            import_kwh_house10,
                            export_kwh10,
                            soc_min10,
                            soc10,
                            soc_min_minute10,
                            battery_cycle10,
                            metric_keep10,
                            final_iboost10,
                            final_carbon_g10,
                        ) = self.run_prediction(
                            try_limit,
                            charge_window,
                            discharge_window,
                            discharge_limits,
                            True,
                            end_record=self.end_record,
                        )
                        (
                            cost,
                            import_kwh_battery,
                            import_kwh_house,
                            export_kwh,
                            soc_min,
                            soc,
                            soc_min_minute,
                            battery_cycle,
                            metric_keep,
                            final_iboost,
                            final_carbon_g,
                        ) = self.run_prediction(
                            try_limit,
                            charge_window,
                            discharge_window,
                            discharge_limits,
                            False,
                            end_record=self.end_record,
                        )
                        metric, metric10 = self.compute_metric(
                            end_record,
                            soc,
                            soc10,
                            cost,
                            cost10,
                            final_iboost,
                            final_iboost10,
                            battery_cycle,
                            battery_cycle10,
                            metric_keep,
                            metric_keep10,
                            final_carbon_g,
                            final_carbon_g10,
                        )

                        self.log("Swap optimisation with metric {}".format(metric))
                        if metric < best_metric:
                            best_metric = metric
                            best_cost = cost
                            best_keep = metric_keep
                            best_soc = soc
                            best_soc_min = soc_min
                            best_soc_min_minute = soc_min_minute
                            best_cycle = battery_cycle
                            best_carbon = final_carbon_g
                            charge_limit = try_limit.copy()
                            self.log("Swap optimisation window {} -> {} selected with metric {}".format(window_n, window_swap, metric))
        self.log("Swap optimisation finished with metric {}".format(best_metric))
        return charge_limit, best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon

    def compute_metric(
        self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, battery_cycle10, metric_keep, metric_keep10, final_carbon_g, final_carbon_g10
    ):
        """
        Compute the metric combing pv and pv10 data
        """
        # Store simulated mid value
        metric = cost
        metric10 = cost10

        # Balancing payment to account for battery left over
        # ie. how much extra battery is worth to us in future, assume it's the same as low rate
        rate_min = self.rate_min_forward.get(end_record, self.rate_min) / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
        rate_export_min = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min
        metric -= (soc + final_iboost) * max(rate_min, 1.0, rate_export_min) * self.metric_battery_value_scaling
        metric10 -= (soc10 + final_iboost10) * max(rate_min, 1.0, rate_export_min) * self.metric_battery_value_scaling
        # Metric adjustment based on 10% outcome weighting
        if metric10 > metric:
            metric_diff = metric10 - metric
            metric_diff *= self.pv_metric10_weight
            metric += metric_diff

        # Carbon metric
        if self.carbon_enable:
            metric += (final_carbon_g / 1000) * self.carbon_metric
            metric10 += (final_carbon_g10 / 1000) * self.carbon_metric

        # Adjustment for battery cycles metric
        metric += battery_cycle * self.metric_battery_cycle + metric_keep
        metric10 += battery_cycle10 * self.metric_battery_cycle + metric_keep10

        return self.dp4(metric), self.dp4(metric10)

    def optimise_charge_limit(self, window_n, record_charge_windows, charge_limit, charge_window, discharge_window, discharge_limits, all_n=None, end_record=None):
        """
        Optimise a single charging window for best SOC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_soc_min = 0
        best_soc_min_minute = 0
        best_metric = 9999999
        best_cost = 0
        best_soc_step = self.best_soc_step
        best_keep = 0
        best_cycle = 0
        best_carbon = 0
        all_max_soc = self.soc_max
        all_min_soc = 0
        try_charge_limit = copy.deepcopy(charge_limit)
        resultmid = {}
        result10 = {}

        # For single windows, if the size is 30 minutes or less then use a larger step
        min_improvement_scaled = self.metric_min_improvement
        if not all_n:
            start = charge_window[window_n]["start"]
            end = charge_window[window_n]["end"]
            window_size = end - start
            if window_size <= 30 and best_soc_step < 1:
                best_soc_step *= 2
            min_improvement_scaled = self.metric_min_improvement * window_size / 30.0

        # Start the loop at the max soc setting
        if self.best_soc_max > 0:
            loop_soc = min(loop_soc, self.best_soc_max)

        # Create min/max SOC to avoid simulating SOC that are not going have any impact
        # Can't do this for anything but a single window as the winder SOC impact isn't known
        if not all_n:
            hans = []
            all_max_soc = 0
            all_min_soc = self.soc_max
            hans.append(self.launch_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, False, all_n, end_record))
            hans.append(self.launch_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, True, all_n, end_record))
            hans.append(self.launch_run_prediction_charge(best_soc_min, window_n, charge_limit, charge_window, discharge_window, discharge_limits, False, all_n, end_record))
            hans.append(self.launch_run_prediction_charge(best_soc_min, window_n, charge_limit, charge_window, discharge_window, discharge_limits, True, all_n, end_record))
            id = 0
            for han in hans:
                (
                    cost,
                    import_kwh_battery,
                    import_kwh_house,
                    export_kwh,
                    soc_min,
                    soc,
                    soc_min_minute,
                    battery_cycle,
                    metric_keep,
                    final_iboost,
                    final_carbon_g,
                    min_soc,
                    max_soc,
                ) = han.get()
                all_min_soc = min(all_min_soc, min_soc)
                all_max_soc = max(all_max_soc, max_soc)
                if id == 0:
                    resultmid[loop_soc] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                        min_soc,
                        max_soc,
                    ]
                elif id == 1:
                    result10[loop_soc] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                        min_soc,
                        max_soc,
                    ]
                elif id == 2:
                    resultmid[best_soc_min] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                        min_soc,
                        max_soc,
                    ]
                elif id == 3:
                    result10[best_soc_min] = [
                        cost,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                        min_soc,
                        max_soc,
                    ]
                id += 1

        # Assemble list of SOCs to try
        try_socs = []
        loop_step = max(best_soc_step, 0.1)
        best_soc_min_setting = self.best_soc_min
        if best_soc_min_setting > 0:
            best_soc_min_setting = max(self.reserve, best_soc_min_setting)

        while loop_soc > self.reserve:
            skip = False
            try_soc = max(best_soc_min, loop_soc)
            try_soc = self.dp2(min(try_soc, self.soc_max))
            if try_soc > (all_max_soc + loop_step):
                skip = True
            if (try_soc > self.reserve) and (try_soc > self.best_soc_min) and (try_soc < (all_min_soc - loop_step)):
                skip = True
            # Keep those we already simulated
            if try_soc in resultmid:
                skip = False
            # Keep the current setting if different from the selected ones
            if not all_n and try_soc == charge_limit[window_n]:
                skip = False
            # All to the list
            if not skip and (try_soc not in try_socs) and (try_soc != self.reserve):
                try_socs.append(self.dp2(try_soc))
            loop_soc -= loop_step

        # Give priority to off to avoid spurious charge freezes
        if best_soc_min_setting not in try_socs:
            try_socs.append(best_soc_min_setting)
        if self.set_charge_freeze and (self.reserve not in try_socs):
            try_socs.append(self.reserve)

        # Run the simulations in parallel
        results = []
        results10 = []
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, False, all_n, end_record)
                results.append(hanres)
                hanres10 = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, True, all_n, end_record)
                results10.append(hanres10)

        # Get results from sims if we simulated them
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = results.pop(0)
                hanres10 = results10.pop(0)
                resultmid[try_soc] = hanres.get()
                result10[try_soc] = hanres10.get()

        window_results = {}
        # Now we have all the results, we can pick the best SOC
        for try_soc in try_socs:
            window = charge_window[window_n]

            # Store try value into the window, either all or just this one
            if all_n:
                for window_id in all_n:
                    try_charge_limit[window_id] = try_soc
            else:
                try_charge_limit[window_n] = try_soc

            # Simulate with medium PV
            (
                cost,
                import_kwh_battery,
                import_kwh_house,
                export_kwh,
                soc_min,
                soc,
                soc_min_minute,
                battery_cycle,
                metric_keep,
                final_iboost,
                final_carbon_g,
                min_soc,
                max_soc,
            ) = resultmid[try_soc]
            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
                min_soc10,
                max_soc10,
            ) = result10[try_soc]

            # Compute the metric from simulation results
            metric, metric10 = self.compute_metric(
                end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, battery_cycle10, metric_keep, metric_keep10, final_carbon_g, final_carbon_g10
            )

            # Metric adjustment based on current charge limit when inside the window
            # to try to avoid constant small changes to SOC target by forcing to keep the current % during a charge period
            # if changing it has little impact
            if not all_n and self.isCharging and (window_n == self.in_charge_window(charge_window, self.minutes_now)) and (try_soc != self.reserve):
                try_percent = calc_percent_limit(try_soc, self.soc_max)
                compare_with = max(self.current_charge_limit, self.reserve_percent)

                if compare_with == try_percent:
                    metric -= max(0.1, self.metric_min_improvement)

            if try_soc == best_soc_min_setting:
                # Minor weighting to 0%
                metric -= 0.002
            elif try_soc == self.soc_max or try_soc == self.reserve:
                # Minor weighting to 100% or freeze
                metric -= 0.001

            # Round metric to 4 DP
            metric = self.dp4(metric)

            if self.debug_enable:
                self.log(
                    "Sim: SOC {} window {} metric {} cost {} cost10 {} metric10 {} soc {} soc10 {} final_iboost {} final_iboost10 {} final_carbon_g {} metric_keep {} metric_keep10 {} cycle {} cycle10 {} carbon {} carbon10 {}".format(
                        try_soc,
                        window_n,
                        metric,
                        cost,
                        cost10,
                        metric10,
                        soc,
                        soc10,
                        final_iboost,
                        final_iboost10,
                        final_carbon_g,
                        metric_keep,
                        metric_keep10,
                        battery_cycle,
                        battery_cycle10,
                        final_carbon_g,
                        final_carbon_g10,
                    )
                )

            if self.debug_enable:
                self.log(
                    "Sim: SOC {} window {} imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} keep {} cost {} cost10 {} metric10 {}".format(
                        try_soc,
                        window_n,
                        self.dp4(import_kwh_battery),
                        self.dp4(import_kwh_house),
                        self.dp4(export_kwh),
                        self.dp4(soc_min),
                        self.time_abs_str(soc_min_minute),
                        self.dp4(soc),
                        self.dp4(cost),
                        self.dp4(metric),
                        self.dp4(metric_keep),
                        self.dp4(cost),
                        self.dp4(cost10),
                        self.dp4(metric10),
                    )
                )

            window_results[try_soc] = metric

            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold
            if (metric + min_improvement_scaled) <= best_metric:
                best_metric = metric
                best_soc = try_soc
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_keep = metric_keep
                best_cycle = battery_cycle
                best_carbon = final_carbon_g

        # Add margin last
        best_soc = min(best_soc + self.best_soc_margin, self.soc_max)

        if self.debug_enable:
            if not all_n:
                self.log(
                    "Try optimising charge window(s)    {}: {} - {} price {} cost {} metric {} keep {} cycle {} carbon {} selected {} was {} results {}".format(
                        window_n,
                        self.time_abs_str(window["start"]),
                        self.time_abs_str(window["end"]),
                        charge_window[window_n]["average"],
                        self.dp4(best_cost),
                        self.dp4(best_metric),
                        self.dp4(best_keep),
                        self.dp4(best_cycle),
                        self.dp0(best_carbon),
                        best_soc,
                        charge_limit[window_n],
                        window_results,
                    )
                )
            else:
                self.log(
                    "Try optimising charge window(s)    {}: price {} cost {} metric {} keep {} cycle {} carbon {} selected {} was {} results {}".format(
                        all_n,
                        charge_window[window_n]["average"],
                        self.dp2(best_cost),
                        self.dp2(best_metric),
                        self.dp2(best_keep),
                        self.dp2(best_cycle),
                        self.dp0(best_carbon),
                        best_soc,
                        charge_limit[window_n],
                        window_results,
                    )
                )
        return best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon

    def optimise_discharge(
        self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, discharge_limit, all_n=None, end_record=None, freeze_only=False
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
        best_cycle = 0
        best_carbon = 0
        this_discharge_limit = 100.0
        window = discharge_window[window_n]
        try_discharge_window = copy.deepcopy(discharge_window)
        try_discharge = copy.deepcopy(discharge_limit)
        best_start = window["start"]
        best_size = window["end"] - best_start
        discharge_step = 5

        # loop on each discharge option
        if self.set_discharge_freeze and freeze_only:
            loop_options = [100, 99]
        elif self.set_discharge_freeze and not self.set_discharge_freeze_only:
            # If we support freeze, try a 99% option which will freeze at any SOC level below this
            loop_options = [100, 99, 0]
        else:
            loop_options = [100, 0]

        # Collect all options
        results = []
        results10 = []
        try_options = []
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
                this_discharge_limit = max(calc_percent_limit(max(self.best_soc_min, self.reserve), self.soc_max), this_discharge_limit)
                try_options.append([start, this_discharge_limit])

                results.append(
                    self.launch_run_prediction_discharge(
                        this_discharge_limit, start, window_n, try_charge_limit, charge_window, try_discharge_window, try_discharge, False, all_n, end_record
                    )
                )
                results10.append(
                    self.launch_run_prediction_discharge(
                        this_discharge_limit, start, window_n, try_charge_limit, charge_window, try_discharge_window, try_discharge, True, all_n, end_record
                    )
                )

        # Get results from sims
        try_results = []
        for try_option in try_options:
            hanres = results.pop(0)
            hanres10 = results10.pop(0)
            result = hanres.get()
            result10 = hanres10.get()
            try_results.append(try_option + [result, result10])

        window_results = {}
        for try_option in try_results:
            start, this_discharge_limit, hanres, hanres10 = try_option

            # Simulate with medium PV
            cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = hanres
            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = hanres10

            # Compute the metric from simulation results
            metric, metric10 = self.compute_metric(
                end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, battery_cycle10, metric_keep, metric_keep10, final_carbon_g, final_carbon_g10
            )

            # Adjust to try to keep existing windows
            if window_n < 2 and this_discharge_limit < 99.0 and self.discharge_window and self.isDischarging:
                pwindow = discharge_window[window_n]
                dwindow = self.discharge_window[0]
                if (
                    self.minutes_now >= pwindow["start"]
                    and self.minutes_now < pwindow["end"]
                    and ((self.minutes_now >= dwindow["start"] and self.minutes_now < dwindow["end"]) or (dwindow["end"] == pwindow["start"]))
                ):
                    metric -= max(0.5, self.metric_min_improvement_discharge)

            # Round metric to 4 DP
            metric = self.dp4(metric)

            if self.debug_enable:
                self.log(
                    "Sim: Discharge {} window {} start {} end {}, imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} metric10 {} cycle {} end_record {}".format(
                        this_discharge_limit,
                        window_n,
                        self.time_abs_str(start),
                        self.time_abs_str(try_discharge_window[window_n]["end"]),
                        self.dp4(import_kwh_battery),
                        self.dp4(import_kwh_house),
                        self.dp4(export_kwh),
                        self.dp4(soc_min),
                        self.time_abs_str(soc_min_minute),
                        self.dp4(soc),
                        self.dp4(cost),
                        self.dp4(metric),
                        self.dp4(metric10),
                        self.dp4(battery_cycle * self.metric_battery_cycle),
                        end_record,
                    )
                )

            window_size = try_discharge_window[window_n]["end"] - start
            window_key = str(int(this_discharge_limit)) + "_" + str(window_size)
            window_results[window_key] = metric

            if all_n:
                min_improvement_scaled = self.metric_min_improvement_discharge
            else:
                min_improvement_scaled = self.metric_min_improvement_discharge * window_size / 30.0

            # Only select a discharge if it makes a notable improvement has defined by min_improvement (divided in M windows)
            if ((metric + min_improvement_scaled) <= off_metric) and (metric <= best_metric):
                best_metric = metric
                best_discharge = this_discharge_limit
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_start = start
                best_size = window_size
                best_keep = metric_keep
                best_cycle = battery_cycle
                best_carbon = final_carbon_g

            # Store the metric for discharge off
            if off_metric == 9999999:
                off_metric = metric

        if self.debug_enable:
            if not all_n:
                self.log(
                    "Try optimising discharge window(s) {}: {} - {} price {} cost {} metric {} keep {} selected {}% size {} was {}% results {}".format(
                        window_n,
                        self.time_abs_str(window["start"]),
                        self.time_abs_str(window["end"]),
                        window["average"],
                        self.dp4(best_cost),
                        self.dp4(best_metric),
                        self.dp4(best_keep),
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
                        self.dp4(best_cost),
                        self.dp4(best_metric),
                        self.dp4(best_keep),
                        best_discharge,
                        best_size,
                        window_results,
                    )
                )

        return best_discharge, best_start, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon

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
                    if self.carbon_enable:
                        carbon_intensity = self.carbon_intensity.get(window["start"] - self.minutes_now, 0)
                        average += carbon_intensity * self.carbon_metric / 1000.0
                if secondary_order:
                    average_export = self.dp2((self.rate_export.get(window["start"], 0) + self.rate_export.get(window["end"] - PREDICT_STEP, 0)) / 2)
                else:
                    average_export = 0
                sort_key = "%04.2f_%04.2f_%03d_c%02d" % (5000 - average, 5000 - average_export, 999 - id, id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = self.dp1(average)  # Round to nearest 0.1 penny to avoid too many bands
                window_links[sort_key]["average_secondary"] = self.dp1(average_export)  # Round to nearest 0.1 penny to avoid too many bands
                id += 1

        # Add discharge windows
        if self.calculate_best_discharge and not stand_alone:
            id = 0
            for window in discharge_windows:
                # Account for losses in average rate as it makes export value lower
                average = self.dp2(window["average"] * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle)
                if self.carbon_enable:
                    carbon_intensity = self.carbon_intensity.get(window["start"] - self.minutes_now, 0)
                    average += carbon_intensity * self.carbon_metric / 1000.0
                if secondary_order:
                    average_import = self.dp2((self.rate_import.get(window["start"], 0) + self.rate_import.get(window["end"] - PREDICT_STEP, 0)) / 2)
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
                window_links[sort_key]["average_secondary"] = self.dp1(average_import)  # Round to nearest 0.1 penny to avoid too many bands
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

    def discard_unused_charge_slots(self, charge_limit_best, charge_window_best, reserve):
        """
        Filter out unused charge slots (those set at 0)
        """
        new_limit_best = []
        new_window_best = []

        max_slots = len(charge_limit_best)

        for window_n in range(max_slots):
            # Only keep slots > than reserve, or keep the last one so we don't have zero slots
            # Also keep a slot if we are already inside it and charging is enabled
            window = charge_window_best[window_n].copy()
            start = window["start"]
            end = window["end"]
            limit = charge_limit_best[window_n]

            if (
                new_window_best
                and (start == new_window_best[-1]["end"])
                and (limit == new_limit_best[-1])
                and (start not in self.manual_all_times)
                and (new_window_best[-1]["start"] not in self.manual_all_times)
            ):
                new_window_best[-1]["end"] = end
                if self.debug_enable:
                    self.log(
                        "Combine charge slot {} with previous - target soc {} kWh slot {} start {} end {} limit {}".format(
                            window_n, new_limit_best[-1], new_window_best[-1], start, end, limit
                        )
                    )
            elif (
                (limit > 0 and self.set_charge_freeze)
                or (limit > self.reserve and (not self.set_charge_freeze))
                or (self.minutes_now >= start and self.minutes_now < end and self.charge_window and self.charge_window[0]["end"] == end)
            ):
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
        for window_n in range(min(record_charge_windows, len(charge_window_best))):
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
                    soc_min_percent = calc_percent_limit(soc_min, self.soc_max)
                    soc_max = max(soc_start, soc_end)

                    if self.debug_enable:
                        self.log(
                            "Examine charge window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(
                                window_n, window_start, window_end, predict_minute_start, limit, soc_start, soc_end
                            )
                        )

                    if (soc_min_percent > calc_percent_limit(charge_limit_best[window_n], self.soc_max)) and (charge_limit_best[window_n] != self.reserve):
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
        for window_n in range(min(record_discharge_windows, len(discharge_window_best))):
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
                        discharge_limits_best[window_n] = calc_percent_limit(limit_soc, self.soc_max)
                        if limit != discharge_limits_best[window_n] and self.debug_enable:
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
                            discharge_limits_best[window_n] = calc_percent_limit(limit_soc, self.soc_max)
                            if limit != discharge_limits_best[window_n] and self.debug_enable:
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
        for window_n in range(len(discharge_limits_best)):
            if discharge_limits_best[window_n] < 100.0:
                # Also merge contiguous enabled windows
                if (
                    new_best
                    and (discharge_window_best[window_n]["start"] == new_best[-1]["end"])
                    and (discharge_limits_best[window_n] == new_enable[-1])
                    and (discharge_window_best[window_n]["start"] not in self.manual_all_times)
                    and (new_best[-1]["start"] not in self.manual_all_times)
                ):
                    new_best[-1]["end"] = discharge_window_best[window_n]["end"]
                    if self.debug_enable:
                        self.log("Combine discharge slot {} with previous - percent {} slot {}".format(window_n, new_enable[-1], new_best[-1]))
                else:
                    new_best.append(copy.deepcopy(discharge_window_best[window_n]))
                    new_enable.append(discharge_limits_best[window_n])

        return new_enable, new_best

    def tweak_plan(self, end_record, best_metric, metric_keep):
        """
        Tweak existing plan only
        """
        record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
        record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)
        self.log("Tweak optimisation started")
        best_soc = self.soc_max
        best_cost = best_metric
        best_keep = metric_keep
        best_cycle = 0
        best_carbon = 0
        count = 0
        window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows])
        for key in window_sorted:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            if typ == "c":
                window_start = self.charge_window_best[window_n]["start"]
                if self.calculate_best_charge and (window_start not in self.manual_all_times):
                    best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_charge_limit(
                        window_n,
                        record_charge_windows,
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.discharge_window_best,
                        self.discharge_limits_best,
                        end_record=end_record,
                    )
                    self.charge_limit_best[window_n] = best_soc
            else:
                window_start = self.discharge_window_best[window_n]["start"]
                if self.calculate_best_discharge and (window_start not in self.manual_all_times):
                    if not self.calculate_discharge_oncharge:
                        hit_charge = self.hit_charge_window(self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"])
                        if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                            continue
                        if not self.car_charging_from_battery and self.hit_car_window(self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]):
                            continue
                    best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_discharge(
                        window_n,
                        record_discharge_windows,
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.discharge_window_best,
                        self.discharge_limits_best,
                        end_record=end_record,
                    )
                    self.discharge_limits_best[window_n] = best_soc
                    self.discharge_window_best[window_n]["start"] = best_start
            count += 1
            if count >= 8:
                break

        self.log(
            "Tweak optimisation finished metric {} cost {} metric_keep {} cycle {} carbon {}".format(
                self.dp2(best_metric), self.dp2(best_cost), self.dp2(best_keep), self.dp2(best_cycle), self.dp0(best_carbon)
            )
        )

    def optimise_all_windows(self, best_metric, metric_keep):
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
        best_cycle = 0
        best_carbon = 0
        best_price = 0
        best_price_discharge = 0
        fast_mode = True

        # Optimise all windows by picking a price threshold default
        if price_set and self.calculate_best_charge and self.charge_window_best:
            self.log("Optimise all windows, total charge {} discharge {}".format(record_charge_windows, record_discharge_windows))
            self.optimise_charge_windows_reset(reset_all=True)
            self.optimise_charge_windows_manual()
            (
                self.charge_limit_best,
                ignore_discharge_limits,
                best_price,
                best_price_discharge,
                best_metric,
                best_cost,
                best_keep,
                best_soc_min,
                best_cycle,
                best_carbon,
                tried_list,
            ) = self.optimise_charge_limit_price_threads(
                price_set,
                price_links,
                window_index,
                record_charge_windows,
                record_discharge_windows,
                self.charge_limit_best,
                self.charge_window_best,
                self.discharge_window_best,
                self.discharge_limits_best,
                end_record=self.end_record,
                fast=fast_mode,
                quiet=True,
            )
            if self.calculate_regions:
                self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price)
                region_size = int(16 * 60)
                while region_size >= 2 * 60:
                    self.log(">> Region optimisation pass width {}".format(region_size))
                    for region in range(0, self.end_record + self.minutes_now, region_size):
                        region_end = min(region + region_size, self.end_record + self.minutes_now)
                        if region_end < self.minutes_now:
                            continue
                        (
                            self.charge_limit_best,
                            ignore_discharge_limits,
                            ignore_best_price,
                            ignore_best_price_discharge,
                            best_metric,
                            best_cost,
                            best_keep,
                            best_soc_min,
                            best_cycle,
                            best_carbon,
                            tried_list,
                        ) = self.optimise_charge_limit_price_threads(
                            price_set,
                            price_links,
                            window_index,
                            record_charge_windows,
                            record_discharge_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.discharge_window_best,
                            ignore_discharge_limits,
                            end_record=self.end_record,
                            region_start=region,
                            region_end=region_end,
                            fast=fast_mode,
                            quiet=True,
                            best_metric=best_metric,
                            best_cost=best_cost,
                            best_keep=best_keep,
                            best_soc_min=best_soc_min,
                            best_price_charge=best_price,
                            best_price_discharge=best_price_discharge,
                            best_cycle=best_cycle,
                            best_carbon=best_carbon,
                            tried_list=tried_list,
                        )
                    region_size = int(region_size / 2)

        # Set the new end record and blackout period based on the levelling
        self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price)
        self.optimise_charge_windows_reset(reset_all=False)
        self.optimise_charge_windows_manual()
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_discharge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.discharge_window_best), 1)
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(
            self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows], secondary_order=self.calculate_secondary_order
        )

        self.rate_best_cost_threshold_charge = best_price
        self.rate_best_cost_threshold_discharge = best_price_discharge

        # Work out the lowest rate we charge at from the first pass
        lowest_price_charge = best_price
        for price_key in price_set:
            links = price_links[price_key]
            for key in links:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                price = window_index[key]["average"]
                if typ == "c" and (self.charge_limit_best[window_n] > self.reserve and price < lowest_price_charge):
                    lowest_price_charge = price

        # Optimise individual windows in the price band for charge/discharge
        # First optimise those at or below threshold highest to lowest (to turn down values)
        # then optimise those above the threshold lowest to highest (to turn up values)
        # Do the opposite for discharge.
        self.log(
            "Starting second optimisation end_record {} best_price {} best_price_discharge {} lowest_price_charge {} with charge limits {}".format(
                self.time_abs_str(self.end_record + self.minutes_now), best_price, best_price_discharge, lowest_price_charge, self.charge_limit_best
            )
        )
        for pass_type in ["freeze", "normal", "low"]:
            start_at_low = False
            if pass_type in ["low"]:
                price_set.reverse()
                start_at_low = True

            for price_key in price_set:
                links = price_links[price_key].copy()

                # Freeze pass should be done in time order (newest first)
                if pass_type in ["freeze"]:
                    links.reverse()

                printed_set = False

                for key in links:
                    typ = window_index[key]["type"]
                    window_n = window_index[key]["id"]
                    price = window_index[key]["average"]

                    if typ == "c":
                        # Store price set with window
                        self.charge_window_best[window_n]["set"] = price
                        window_start = self.charge_window_best[window_n]["start"]

                        # Freeze pass is just discharge freeze
                        if pass_type in ["freeze"]:
                            continue

                        # For start at high only tune down excess high slots
                        if (not start_at_low) and (price > best_price) and (self.charge_limit_best[window_n] != self.soc_max):
                            if self.debug_enable:
                                self.log("Skip start at high window {} best limit {}".format(window_n, (self.charge_limit_best[window_n])))
                            continue

                        if self.calculate_best_charge and (window_start not in self.manual_all_times):
                            if not printed_set:
                                self.log(
                                    "Optimise price set {} pass {} price {} start_at_low {} best_price {} best_metric {} best_cost {} best_cycle {} best_carbon {}".format(
                                        price_key,
                                        pass_type,
                                        price,
                                        start_at_low,
                                        best_price,
                                        self.dp2(best_metric),
                                        self.dp2(best_cost),
                                        self.dp2(best_cycle),
                                        self.dp0(best_carbon),
                                    )
                                )
                                printed_set = True
                            average = self.charge_window_best[window_n]["average"]

                            best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_charge_limit(
                                window_n,
                                record_charge_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.discharge_window_best,
                                self.discharge_limits_best,
                                end_record=self.end_record,
                            )
                            self.charge_limit_best[window_n] = best_soc

                            if self.debug_enable:
                                self.log(
                                    "Best charge limit window {} time {} - {} cost {} charge_limit {} (adjusted) min {} @ {} (margin added {} and min {} max {}) with metric {} cost {} cycle {} carbon {} windows {}".format(
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
                                        self.dp2(best_cycle),
                                        self.dp0(best_carbon),
                                        calc_percent_limit(self.charge_limit_best, self.soc_max),
                                    )
                                )
                    else:
                        # Store price set with window
                        self.discharge_window_best[window_n]["set"] = price
                        window_start = self.discharge_window_best[window_n]["start"]

                        # Ignore freeze pass if discharge freeze disabled
                        if not self.set_discharge_freeze and pass_type == "freeze":
                            continue

                        # Do highest price first
                        # Second pass to tune down any excess exports only
                        if pass_type == "low" and ((price > best_price) or (self.discharge_limits_best[window_n] == 100.0)):
                            continue

                        if self.calculate_best_discharge and (window_start not in self.manual_all_times):
                            if not self.calculate_discharge_oncharge:
                                hit_charge = self.hit_charge_window(
                                    self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                                )
                                if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                                    continue
                            if not self.car_charging_from_battery and self.hit_car_window(
                                self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]
                            ):
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
                                self.log(
                                    "Optimise price set {} pass {} price {} start_at_low {} best_price {} best_metric {} best_cost {} best_cycle {} best_carbon {}".format(
                                        price_key,
                                        pass_type,
                                        price,
                                        start_at_low,
                                        best_price,
                                        self.dp2(best_metric),
                                        self.dp2(best_cost),
                                        self.dp2(best_cycle),
                                        self.dp0(best_carbon),
                                    )
                                )
                                printed_set = True

                            best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_discharge(
                                window_n,
                                record_discharge_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.discharge_window_best,
                                self.discharge_limits_best,
                                end_record=self.end_record,
                                freeze_only=pass_type in ["freeze"],
                            )
                            self.discharge_limits_best[window_n] = best_soc
                            self.discharge_window_best[window_n]["start"] = best_start

                            if self.debug_enable:
                                self.log(
                                    "Best discharge limit window {} time {} - {} cost {} discharge_limit {} (adjusted) min {} @ {} (margin added {} and min {}) with metric {} cost {} cycle {} carbon {}".format(
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
                                        self.dp2(best_cycle),
                                        self.dp0(best_carbon),
                                    )
                                )

            # Log set of charge and discharge windows
            if self.calculate_best_charge:
                self.log(
                    "Best charge windows best_metric {} best_cost {} metric_keep {} end_record {} windows {}".format(
                        self.dp2(best_metric),
                        self.dp2(best_cost),
                        self.dp2(best_keep),
                        self.time_abs_str(self.end_record + self.minutes_now),
                        self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max), ignore_min=True),
                    )
                )

            if self.calculate_best_discharge:
                self.log(
                    "Best discharge windows best_metric {} best_cost {} metric_keep {} end_record {} windows {}".format(
                        self.dp2(best_metric),
                        self.dp2(best_cost),
                        self.dp2(best_keep),
                        self.time_abs_str(self.end_record + self.minutes_now),
                        self.window_as_text(self.discharge_window_best, self.discharge_limits_best, ignore_max=True),
                    )
                )

        # Re-compute end record
        self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price)
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_discharge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.discharge_window_best), 1)

        if 0:
            self.log("Swap optimisation started")
            self.charge_limit_best, best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_charge_limit_swap(
                self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, record_charge_windows, end_record=self.end_record
            )
            self.log(
                "Best after swap best_metric {} best_cost {} metric_keep {} end_record {}".format(
                    self.dp2(best_metric), self.dp2(best_cost), self.dp2(best_keep), self.time_abs_str(self.end_record + self.minutes_now)
                )
            )
            # Re-compute end record
            self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price)
            record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
            record_discharge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.discharge_window_best), 1)

        if self.calculate_second_pass:
            self.log("Second pass optimisation started")
            count = 0
            window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.discharge_window_best[:record_discharge_windows])
            for key in window_sorted:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                if typ == "c":
                    window_start = self.charge_window_best[window_n]["start"]
                    if self.calculate_best_charge and (window_start not in self.manual_all_times):
                        best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_charge_limit(
                            window_n,
                            record_charge_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.discharge_window_best,
                            self.discharge_limits_best,
                            end_record=self.end_record,
                        )
                        self.charge_limit_best[window_n] = best_soc
                else:
                    window_start = self.discharge_window_best[window_n]["start"]
                    if self.calculate_best_discharge and (window_start not in self.manual_all_times):
                        if not self.calculate_discharge_oncharge:
                            hit_charge = self.hit_charge_window(self.charge_window_best, self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"])
                            if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                                continue
                        if not self.car_charging_from_battery and self.hit_car_window(self.discharge_window_best[window_n]["start"], self.discharge_window_best[window_n]["end"]):
                            continue

                        average = self.discharge_window_best[window_n]["average"]
                        best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon = self.optimise_discharge(
                            window_n,
                            record_discharge_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.discharge_window_best,
                            self.discharge_limits_best,
                            end_record=self.end_record,
                        )
                        self.discharge_limits_best[window_n] = best_soc
                        self.discharge_window_best[window_n]["start"] = best_start
                if (count % 16) == 0:
                    self.log("Final optimisation type {} window {} metric {} metric_keep {} cost {}".format(typ, window_n, best_metric, self.dp2(best_keep), self.dp2(best_cost)))
                count += 1
            self.log(
                "Second pass optimisation finished metric {} cost {} metric_keep {} cycle {} carbon {}".format(
                    best_metric, self.dp2(best_cost), self.dp2(best_keep), self.dp2(best_cycle), self.dp0(best_carbon)
                )
            )

    def optimise_charge_windows_manual(self):
        """
        Manual window overrides
        """
        if self.charge_window_best and self.calculate_best_charge:
            for window_n in range(len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] in self.manual_idle_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_discharge_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_freeze_discharge_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_charge_times:
                    self.charge_limit_best[window_n] = self.soc_max
                elif self.charge_window_best[window_n]["start"] in self.manual_freeze_charge_times:
                    self.charge_limit_best[window_n] = self.reserve

        if self.discharge_window_best and self.calculate_best_discharge:
            for window_n in range(len(self.discharge_window_best)):
                if self.discharge_window_best[window_n]["start"] in self.manual_idle_times:
                    self.discharge_limits_best[window_n] = 100
                elif self.discharge_window_best[window_n]["start"] in self.manual_discharge_times:
                    self.discharge_limits_best[window_n] = 0
                elif self.discharge_window_best[window_n]["start"] in self.manual_freeze_discharge_times:
                    self.discharge_limits_best[window_n] = 99

    def optimise_charge_windows_reset(self, reset_all):
        """
        Reset the charge windows to min

        Parameters:
        - reset_all (bool): If True, reset all charge windows. If False, reset only the charge windows that are in the record window.

        Returns:
        None
        """
        if self.charge_window_best and self.calculate_best_charge:
            # Set all to max
            for window_n in range(len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] < (self.minutes_now + self.end_record):
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
        for window_n in range(len(windows)):
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
            txt += " @ {}{} {}%".format(self.dp2(average), self.currency_symbols[1], self.dp2(percent))
        txt += " ]"
        return txt

    def get_car_charging_planned(self):
        """
        Get the car attributes
        """
        self.car_charging_planned = [False for c in range(self.num_cars)]
        self.car_charging_now = [False for c in range(self.num_cars)]
        self.car_charging_plan_smart = [False for c in range(self.num_cars)]
        self.car_charging_plan_max_price = [False for c in range(self.num_cars)]
        self.car_charging_plan_time = [False for c in range(self.num_cars)]
        self.car_charging_battery_size = [100.0 for c in range(self.num_cars)]
        self.car_charging_limit = [100.0 for c in range(self.num_cars)]
        self.car_charging_rate = [7.4 for c in range(max(self.num_cars, 1))]
        self.car_charging_slots = [[] for c in range(self.num_cars)]

        self.car_charging_planned_response = self.get_arg("car_charging_planned_response", ["yes", "on", "enable", "true"])
        self.car_charging_now_response = self.get_arg("car_charging_now_response", ["yes", "on", "enable", "true"])
        self.car_charging_from_battery = self.get_arg("car_charging_from_battery")

        # Car charging planned sensor
        for car_n in range(self.num_cars):
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
            self.car_charging_plan_max_price[car_n] = self.get_arg("car_charging_plan_max_price", 0.0)
            self.car_charging_plan_time[car_n] = self.get_arg("car_charging_plan_time", "07:00:00")
            self.car_charging_battery_size[car_n] = float(self.get_arg("car_charging_battery_size", 100.0, index=car_n))
            self.car_charging_rate[car_n] = float(self.get_arg("car_charging_rate"))
            self.car_charging_limit[car_n] = self.dp3((float(self.get_arg("car_charging_limit", 100.0, index=car_n)) * self.car_charging_battery_size[car_n]) / 100.0)

        if self.num_cars > 0:
            self.log(
                "Cars {} charging from battery {} planned {}, charging_now {} smart {}, max_price {}, plan_time {}, battery size {}, limit {}, rate {}".format(
                    self.num_cars,
                    self.car_charging_from_battery,
                    self.car_charging_planned,
                    self.car_charging_now,
                    self.car_charging_plan_smart,
                    self.car_charging_plan_max_price,
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
                result = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                if not result:
                    attribute = "forecast"
                try:
                    data = self.get_state_wrapper(entity_id=self.get_arg(argname, indirect=False), attribute=attribute)
                except (ValueError, TypeError):
                    self.log("WARN: Unable to fetch solar forecast data from sensor {} check your setting of {}".format(self.get_arg(argname, indirect=False), argname))
                    self.record_status("Error - {} not be set correctly, check apps.yaml", debug=self.get_arg(argname, indirect=False), had_errors=True)

            # Solcast new vs old version
            # check the total vs the sum of 30 minute slots and work out scale factor
            if data:
                for entry in data:
                    total_data += entry["pv_estimate"]
                total_data = self.dp2(total_data)
                total_sensor = self.dp2(self.get_arg(argname, 1.0))
        return data, total_data, total_sensor

    def fetch_extra_load_forecast(self, now_utc):
        """
        Fetch extra load forecast, this is future load data
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
                    self.log("Loading extra load forecast from {} attribute {}".format(entity_id, attribute))
                    data = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                except (ValueError, TypeError) as e:
                    self.log("Error: Unable to fetch load forecast data from sensor {} exception {}".format(entity_id, e))
                    data = None

                # Convert format from dict to array
                if isinstance(data, dict):
                    data_array = []
                    for key, value in data.items():
                        data_array.append({"energy": value, "last_updated": key})
                    data = data_array

                # Load data
                load_forecast = self.minute_data(
                    data,
                    self.forecast_days,
                    self.midnight_utc,
                    "energy",
                    "last_updated",
                    backwards=False,
                    clean_increment=False,
                    smoothing=True,
                    divide_by=1.0,
                    scale=self.load_scaling,
                    required_unit="kWh",
                    accumulate=load_forecast,
                )

                if load_forecast:
                    self.log(
                        "Loaded load forecast from {} load from midnight {} to now {} to midnight {}".format(
                            entity_id, load_forecast.get(0, 0), load_forecast.get(self.minutes_now, 0), load_forecast.get(24 * 60, 0)
                        )
                    )
                else:
                    self.log("WARN: Unable to load load forecast from {}".format(entity_id))
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
            pv_estimate = self.get_arg("pv_estimate", default="")
            if pv_estimate is None:
                pv_estimate = "pv_estimate"
            else:
                pv_estimate = "pv_estimate" + str(pv_estimate)

            pv_forecast_minute = self.minute_data(
                pv_forecast_data,
                self.forecast_days + 1,
                self.midnight_utc,
                pv_estimate,
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
        self.update_time(print=False)

        # For each inverter get the details
        num_inverters = int(self.get_arg("num_inverters", 1))

        inverters = []
        for id in range(num_inverters):
            inverter = Inverter(self, id, quiet=True)
            inverter.update_status(self.minutes_now, quiet=True)
            if inverter.in_calibration:
                self.log("Inverter {} is in calibration mode, not balancing".format(id))
                return
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
            battery_max_rates.append(inverter.battery_rate_max_discharge * MINUTE_WATT)
            total_max_rate += inverter.battery_rate_max_discharge * MINUTE_WATT
            charge_rates.append(inverter.charge_rate_now * MINUTE_WATT)
            total_charge_rates += inverter.charge_rate_now * MINUTE_WATT
            discharge_rates.append(inverter.discharge_rate_now * MINUTE_WATT)
            total_discharge_rates += inverter.discharge_rate_now * MINUTE_WATT
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
        for id in range(num_inverters):
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
        for this_inverter in range(num_inverters):
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

        for id in range(num_inverters):
            if not balance_reset_charge.get(id, False) and total_charge_rates != 0 and charge_rates[id] == 0:
                self.log("BALANCE: Inverter {} reset charge rate to {} now balanced".format(id, inverter.battery_rate_max_charge * MINUTE_WATT))
                inverters[id].adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT, notify=False)
            if not balance_reset_discharge.get(id, False) and total_discharge_rates != 0 and discharge_rates[id] == 0:
                self.log("BALANCE: Inverter {} reset discharge rate to {} now balanced".format(id, inverter.battery_rate_max_discharge * MINUTE_WATT))
                inverters[id].adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT, notify=False)

        self.log("BALANCE: Completed this run")

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
        opts += "combine_rate_threshold({}) ".format(self.combine_rate_threshold)
        opts += "best_soc_min({} kWh) ".format(self.best_soc_min)
        opts += "best_soc_max({} kWh) ".format(self.best_soc_max)
        opts += "best_soc_keep({} kWh) ".format(self.best_soc_keep)
        opts += "inverter_loss({} %) ".format(int((1 - self.inverter_loss) * 100.0))
        opts += "battery_loss({} %) ".format(int((1 - self.battery_loss) * 100.0))
        opts += "battery_loss_discharge ({} %) ".format(int((1 - self.battery_loss_discharge) * 100.0))
        opts += "inverter_hybrid({}) ".format(self.inverter_hybrid)
        opts += "metric_min_improvement({} p) ".format(self.metric_min_improvement)
        opts += "metric_min_improvement_discharge({} p) ".format(self.metric_min_improvement_discharge)
        opts += "metric_battery_cycle({} p/kWh) ".format(self.metric_battery_cycle)
        opts += "metric_battery_value_scaling({} x) ".format(self.metric_battery_value_scaling)
        if self.carbon_enable:
            opts += "metric_carbon({} p/Kg) ".format(self.carbon_metric)
        self.log("Calculate Best options: " + opts)

    def history_to_future_rates(self, rates, offset):
        """
        Shift rates from the past into a future array
        """
        future_rates = {}
        for minute in range(0, self.forecast_minutes):
            future_rates[minute] = rates.get(minute - offset, 0.0)
        return future_rates

    def calculate_yesterday(self):
        """
        Calculate the base plan for yesterday
        """
        yesterday_load_step = self.step_data_history(self.load_minutes, 0, forward=False, scale_today=1.0, scale_fixed=1.0, base_offset=24 * 60 + self.minutes_now)
        yesterday_pv_step = self.step_data_history(self.pv_today, 0, forward=False, scale_today=1.0, scale_fixed=1.0, base_offset=24 * 60 + self.minutes_now)
        yesterday_pv_step_zero = self.step_data_history(None, 0, forward=False, scale_today=1.0, scale_fixed=1.0, base_offset=24 * 60 + self.minutes_now)

        # Get SOC history to find yesterday SOC
        soc_kwh_data = self.get_history_wrapper(entity_id=self.prefix + ".soc_kw_h0", days=2)
        if not soc_kwh_data:
            self.log("WARN: No SOC data found for yesterday")
            return
        soc_kwh = self.minute_data(
            soc_kwh_data[0],
            2,
            self.now_utc,
            "state",
            "last_updated",
            backwards=True,
            clean_increment=False,
            smoothing=False,
            divide_by=1.0,
            scale=1.0,
            required_unit="kWh",
        )
        try:
            soc_yesterday = float(self.get_state_wrapper(self.prefix + ".savings_total_soc", default=0.0))
        except (ValueError, TypeError):
            soc_yesterday = 0.0

        # Shift rates back
        past_rates = self.history_to_future_rates(self.rate_import, 24 * 60)
        past_rates_export = self.history_to_future_rates(self.rate_export, 24 * 60)

        # Assume user might charge at the lowest rate only, for fix tariff
        charge_window_best = []
        rate_low = min(past_rates.values())
        combine_charge = self.combine_charge_slots
        self.combine_charge_slots = True
        charge_window_best, lowest, highest = self.rate_scan_window(past_rates, 5, rate_low, False, return_raw=True)
        self.combine_charge_slots = combine_charge
        charge_limit_best = [self.soc_max for c in range(len(charge_window_best))]
        self.log("Yesterday basic charge window best: {} charge limit best: {}".format(charge_window_best, charge_limit_best))

        # Get Cost yesterday
        cost_today_data = self.get_history_wrapper(entity_id=self.prefix + ".cost_today", days=2)
        if not cost_today_data:
            self.log("WARN: No cost_today data for yesterday")
            return
        cost_data = self.minute_data(cost_today_data[0], 2, self.now_utc, "state", "last_updated", backwards=True, clean_increment=False, smoothing=False, divide_by=1.0, scale=1.0)
        cost_yesterday = cost_data.get(self.minutes_now + 5, 0.0)

        # Save state
        self.dashboard_item(
            self.prefix + ".cost_yesterday",
            state=self.dp2(cost_yesterday),
            attributes={
                "friendly_name": "Cost yesterday",
                "state_class": "measurement",
                "unit_of_measurement": "p",
                "icon": "mdi:currency-usd",
            },
        )

        # Save step data for debug
        if self.debug_enable:
            self.yesterday_load_step = yesterday_load_step
            self.yesterday_pv_step = yesterday_pv_step

        # Save state
        minutes_now = self.minutes_now
        midnight_utc = self.midnight_utc
        forecast_minutes = self.forecast_minutes
        cost_today_sofar = self.cost_today_sofar
        import_today_now = self.import_today_now
        export_today_now = self.export_today_now
        pv_today_now = self.pv_today_now
        carbon_today_sofar = self.carbon_today_sofar
        soc_kw = self.soc_kw
        car_charging_hold = self.car_charging_hold
        load_minutes_now = self.load_minutes_now
        soc_max = self.soc_max
        rate_import = self.rate_import
        rate_export = self.rate_export
        iboost_enable = self.iboost_enable
        num_cars = self.num_cars

        # Fake to yesterday state
        self.minutes_now = 0
        self.cost_today_sofar = 0
        self.import_today_now = 0
        self.export_today_now = 0
        self.carbon_today_sofar = 0
        self.midnight_utc = self.midnight_utc - timedelta(days=1)
        self.forecast_minutes = 24 * 60
        self.pv_today_now = 0
        self.soc_kw = soc_yesterday
        self.car_charging_hold = False
        self.load_minutes_now = 0
        self.rate_import = past_rates
        self.rate_export = past_rates_export
        self.iboost_enable = False
        self.num_cars = 0

        # Simulate yesterday
        self.prediction = Prediction(self, yesterday_pv_step, yesterday_pv_step, yesterday_load_step, yesterday_load_step)
        (
            metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
        ) = self.run_prediction(charge_limit_best, charge_window_best, [], [], False, end_record=(24 * 60), save="yesterday")
        # Add back in standing charge which will be in the historical data also
        metric += self.metric_standing_charge

        # Work out savings
        saving = metric - cost_yesterday
        self.log(
            "Yesterday: Predbat disabled was {}p vs real {}p saving {}p with import {} export {} battery_cycle {} start_soc {} final_soc {}".format(
                self.dp2(metric),
                self.dp2(cost_yesterday),
                self.dp2(saving),
                self.dp2(import_kwh_house + import_kwh_battery),
                self.dp2(export_kwh),
                self.dp2(battery_cycle),
                self.dp2(soc_yesterday),
                self.dp2(final_soc),
            )
        )
        self.savings_today_predbat = saving
        self.savings_today_predbat_soc = final_soc
        self.savings_today_actual = cost_yesterday

        # Save state
        self.dashboard_item(
            self.prefix + ".savings_yesterday_predbat",
            state=self.dp2(saving),
            attributes={
                "import": self.dp2(import_kwh_house + import_kwh_battery),
                "export": self.dp2(export_kwh),
                "battery_cycle": self.dp2(battery_cycle),
                "soc_yesterday": self.dp2(soc_yesterday),
                "final_soc": self.dp2(final_soc),
                "actual_cost": self.dp2(cost_yesterday),
                "predicted_cost": self.dp2(metric),
                "friendly_name": "Predbat savings yesterday",
                "state_class": "measurement",
                "unit_of_measurement": "p",
                "icon": "mdi:currency-usd",
            },
        )

        # Simulate no PV or battery
        self.soc_kw = 0
        self.soc_max = 0

        self.prediction = Prediction(self, yesterday_pv_step_zero, yesterday_pv_step_zero, yesterday_load_step, yesterday_load_step)
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            [], [], [], [], False, end_record=24 * 60
        )
        # Add back in standing charge which will be in the historical data also
        metric += self.metric_standing_charge

        # Work out savings
        saving = metric - cost_yesterday
        self.savings_today_pvbat = saving
        self.log(
            "Yesterday: No Battery/PV system cost predicted was {}p vs real {}p saving {}p with import {} export {}".format(
                self.dp2(metric), self.dp2(cost_yesterday), self.dp2(saving), self.dp2(import_kwh_house + import_kwh_battery), self.dp2(export_kwh)
            )
        )

        # Save state
        self.dashboard_item(
            self.prefix + ".savings_yesterday_pvbat",
            state=self.dp2(saving),
            attributes={
                "import": self.dp2(import_kwh_house + import_kwh_battery),
                "export": self.dp2(export_kwh),
                "battery_cycle": self.dp2(battery_cycle),
                "actual_cost": self.dp2(cost_yesterday),
                "predicted_cost": self.dp2(metric),
                "friendly_name": "PV/Battery system savings yesterday",
                "state_class": "measurement",
                "unit_of_measurement": "p",
                "icon": "mdi:currency-usd",
            },
        )

        # Restore state
        self.minutes_now = minutes_now
        self.midnight_utc = midnight_utc
        self.forecast_minutes = forecast_minutes
        self.cost_today_sofar = cost_today_sofar
        self.import_today_now = import_today_now
        self.export_today_now = export_today_now
        self.carbon_today_sofar = carbon_today_sofar
        self.pv_today_now = pv_today_now
        self.soc_kw = soc_kw
        self.car_charging_hold = car_charging_hold
        self.load_minutes_now = load_minutes_now
        self.soc_max = soc_max
        self.rate_import = rate_import
        self.rate_export = rate_export
        self.iboost_enable = iboost_enable
        self.num_cars = num_cars

    def calculate_plan(self, recompute=True):
        """
        Calculate the new plan (best)

        sets:
           self.charge_window_best
           self.charge_limit_best
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
            if self.low_rates and self.calculate_best_charge and self.set_charge_window:
                # If we are using calculated windows directly then save them
                self.charge_window_best = copy.deepcopy(self.low_rates)
            else:
                # Default best charge window as this one
                self.charge_window_best = self.charge_window

            # Calculate best discharge windows
            if self.high_export_rates:
                self.discharge_window_best = copy.deepcopy(self.high_export_rates)
            else:
                self.discharge_window_best = []

            # Pre-fill best charge limit with the current charge limit
            self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(len(self.charge_window_best))]
            self.charge_limit_percent_best = [self.current_charge_limit for i in range(len(self.charge_window_best))]

            # Pre-fill best discharge enable with Off
            self.discharge_limits_best = [100.0 for i in range(len(self.discharge_window_best))]

            self.end_record = self.forecast_minutes
        # Show best windows
        self.log("Best charge    window {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_percent_best)))
        self.log("Best discharge window {}".format(self.window_as_text(self.discharge_window_best, self.discharge_limits_best)))

        # Created optimised step data
        self.metric_cloud_coverage = self.get_cloud_factor(self.minutes_now, self.pv_forecast_minute, self.pv_forecast_minute10)
        self.metric_load_divergence = self.get_load_divergence(self.minutes_now, self.load_minutes)
        load_minutes_step = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=1.0,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=self.metric_load_divergence,
        )
        load_minutes_step10 = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=self.load_scaling10,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=min(self.metric_load_divergence + 0.5, 1.0) if self.metric_load_divergence else None,
        )
        pv_forecast_minute_step = self.step_data_history(self.pv_forecast_minute, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)
        pv_forecast_minute10_step = self.step_data_history(
            self.pv_forecast_minute10, self.minutes_now, forward=True, cloud_factor=min(self.metric_cloud_coverage + 0.2, 1.0) if self.metric_cloud_coverage else None
        )

        # Save step data for debug
        if self.debug_enable:
            self.load_minutes_step = load_minutes_step
            self.load_minutes_step10 = load_minutes_step10
            self.pv_forecast_minute_step = pv_forecast_minute_step
            self.pv_forecast_minute10_step = pv_forecast_minute10_step

        # Yesterday data
        if recompute and self.calculate_savings:
            self.calculate_yesterday()

        # Creation prediction object
        self.prediction = Prediction(self, pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10)

        # Create pool
        if not self.pool:
            threads = self.get_arg("threads", "auto")
            if threads == "auto":
                self.log("Creating pool of {} processes to match your CPU count".format(cpu_count()))
                self.pool = Pool(processes=cpu_count())
            elif threads:
                self.log("Creating pool of {} processes as per apps.yaml".format(int(threads)))
                self.pool = Pool(processes=int(threads))
            else:
                self.log("Not using threading as threads is set to 0 in apps.yaml")

        # Simulate current settings to get initial data
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, False, end_record=self.end_record
        )

        # Try different battery SOCs to get the best result
        if recompute:
            self.rate_best_cost_threshold_charge = None
            self.rate_best_cost_threshold_discharge = None

        if self.calculate_best and recompute:
            # Recomputing the plan
            self.log_option_best()

            # Full plan
            self.optimise_all_windows(metric, metric_keep)

            # Tweak plan
            if self.calculate_tweak_plan:
                self.tweak_plan(self.end_record, metric, metric_keep)

            # Remove charge windows that overlap with discharge windows
            self.charge_limit_best, self.charge_window_best = remove_intersecting_windows(
                self.charge_limit_best, self.charge_window_best, self.discharge_limits_best, self.discharge_window_best
            )

            # Filter out any unused discharge windows
            if self.calculate_best_discharge and self.discharge_window_best:
                # Filter out the windows we disabled
                self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)

                # Clipping windows
                if self.discharge_window_best:
                    # Re-run prediction to get data for clipping
                    (
                        best_metric,
                        import_kwh_battery,
                        import_kwh_house,
                        export_kwh,
                        soc_min,
                        soc,
                        soc_min_minute,
                        battery_cycle,
                        metric_keep,
                        final_iboost,
                        final_carbon_g,
                    ) = self.run_prediction(
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.discharge_window_best,
                        self.discharge_limits_best,
                        False,
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
                (
                    best_metric,
                    import_kwh_battery,
                    import_kwh_house,
                    export_kwh,
                    soc_min,
                    soc,
                    soc_min_minute,
                    battery_cycle,
                    metric_keep,
                    final_iboost,
                    final_carbon_g,
                ) = self.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.discharge_window_best,
                    self.discharge_limits_best,
                    False,
                    end_record=self.end_record,
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
                    self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                    self.log("Filtered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_percent_best), self.reserve))
                else:
                    self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_percent_best), self.reserve))

            # Plan is now valid
            self.plan_valid = True
            self.plan_last_updated = self.now_utc
            self.plan_last_updated_minutes = self.minutes_now

        # Final simulation of base
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, False, save="base", end_record=self.end_record
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
            final_carbon_g10,
        ) = self.run_prediction(
            self.charge_limit,
            self.charge_window,
            self.discharge_window,
            self.discharge_limits,
            True,
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
                final_carbon_g10,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.discharge_window_best,
                self.discharge_limits_best,
                True,
                save="best10",
                end_record=self.end_record,
            )
            (
                best_metric,
                import_kwh_battery,
                import_kwh_house,
                export_kwh,
                soc_min,
                soc,
                soc_min_minute,
                battery_cycle,
                metric_keep,
                final_iboost,
                final_carbon_g,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.discharge_window_best,
                self.discharge_limits_best,
                False,
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
            self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
            self.publish_charge_limit(self.charge_limit_best, self.charge_window_best, self.charge_limit_percent_best, best=True, soc=self.predict_soc_best)
            self.publish_discharge_limit(self.discharge_window_best, self.discharge_limits_best, best=True)

            # HTML data
            self.publish_html_plan(pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, self.end_record)

        # Destroy pool
        if self.pool:
            self.pool.close()
            self.pool.join()
            self.pool = None
        # Return if we recomputed or not
        return recompute

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
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
                    inverter.disable_charge_window()
                    inverter.adjust_battery_target(100.0, False)
                    inverter.adjust_pause_mode()
                    self.isCharging = False
                if self.set_charge_window or self.set_discharge_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_reserve(0)
                if self.set_discharge_window or (self.inverter_needs_reset_force in ["set_read_only", "mode"]):
                    inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
                    inverter.adjust_force_discharge(False)
                    self.isDischarging = False

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
            # Inverter is in calibration mode
            if inverter.in_calibration:
                status = "Calibration"
                inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
                inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
                inverter.adjust_battery_target(100.0, False)
                inverter.adjust_reserve(0)
                self.log("Inverter is in calibration mode, not executing plan and enabling charge/discharge at full rate.")
                break

            resetDischarge = False
            if (not self.set_discharge_during_charge) or (not self.car_charging_from_battery):
                # These options mess with discharge rate, so we must reset it when they aren't changing it
                resetDischarge = True

            resetReserve = False
            setReserve = False
            disabled_charge_window = False
            disabled_discharge = False

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

                # Span midnight allowed?
                if not inverter.inv_can_span_midnight:
                    if minutes_start < 24 * 60 and minutes_end >= 24 * 60:
                        minutes_end = 24 * 60 - 1

                # Check if start is within 24 hours of now and end is in the future
                if ((minutes_start - self.minutes_now) < (24 * 60)) and (minutes_end > self.minutes_now):
                    charge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                    charge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                    self.log(
                        "Charge window will be: {} - {} - current soc {} target {}".format(
                            charge_start_time, charge_end_time, inverter.soc_percent, self.charge_limit_percent_best[0]
                        )
                    )
                    # Are we actually charging?
                    if self.minutes_now >= minutes_start and self.minutes_now < minutes_end:
                        charge_rate = find_charge_rate(
                            self,
                            self.minutes_now,
                            inverter.soc_kw,
                            window,
                            self.charge_limit_percent_best[0] * inverter.soc_max / 100.0,
                            inverter.battery_rate_max_charge,
                            quiet=False,
                        )
                        inverter.adjust_charge_rate(int(charge_rate * MINUTE_WATT))

                        # Do we disable discharge during charge?
                        pausedDischarge = False
                        if not self.set_discharge_during_charge and (inverter.soc_percent >= self.charge_limit_percent_best[0] or not self.set_reserve_enable):
                            inverter.adjust_discharge_rate(0)
                            inverter.adjust_pause_mode(pause_discharge=True)
                            resetDischarge = False
                            pausedDischarge = True

                        if self.set_charge_freeze and (self.charge_limit_best[0] == self.reserve):
                            if self.set_soc_enable and ((self.set_reserve_enable and self.set_reserve_hold) or inverter.inv_has_timed_pause):
                                inverter.disable_charge_window()
                                disabled_charge_window = True
                                inverter.adjust_pause_mode(pause_discharge=True)
                                pausedDischarge = True

                            if not pausedDischarge:
                                inverter.adjust_pause_mode()
                            status = "Freeze charging"
                            status_extra = " target {}%".format(inverter.soc_percent)
                        else:
                            if (
                                self.set_soc_enable
                                and ((self.set_reserve_enable and self.set_reserve_hold) or inverter.inv_has_timed_pause)
                                and (inverter.soc_percent >= self.charge_limit_percent_best[0])
                                and (inverter.reserve_max >= inverter.soc_percent)
                            ):
                                status = "Hold charging"
                                inverter.disable_charge_window()
                                disabled_charge_window = True
                                inverter.adjust_pause_mode(pause_discharge=True)
                            else:
                                status = "Charging"
                                if not pausedDischarge:
                                    inverter.adjust_pause_mode()
                            status_extra = " target {}%-{}%".format(inverter.soc_percent, self.charge_limit_percent_best[0])
                        inverter.adjust_charge_immediate(self.charge_limit_percent_best[0])
                        isCharging = True

                    if not disabled_charge_window:
                        # Configure the charge window start/end times if in the time window to set them
                        if (self.minutes_now < minutes_end) and (
                            (minutes_start - self.minutes_now) <= self.set_window_minutes or (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes
                        ):
                            if ((minutes_start - self.minutes_now) > self.set_window_minutes) and (minutes_end - self.minutes_now) >= 24 * 60:
                                self.log("Charge window would wrap, disabling until later")
                                inverter.disable_charge_window()
                            else:
                                # We must re-program if we are about to start a new charge window or the currently configured window is about to start or has started
                                self.log(
                                    "Configuring charge window now (now {} target set_window_minutes {} charge start time {}".format(
                                        self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)
                                    )
                                )
                                inverter.adjust_charge_window(charge_start_time, charge_end_time, self.minutes_now)
                        else:
                            if not self.inverter_set_charge_before:
                                self.log(
                                    "Disabled charge window while waiting for schedule (now {} target set_window_minutes {} charge start time {})".format(
                                        self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)
                                    )
                                )
                                inverter.disable_charge_window()
                            else:
                                self.log(
                                    "Not setting charging window yet as not within the window (now {} target set_window_minutes {} charge start time {})".format(
                                        self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)
                                    )
                                )

                    # Set configured window minutes for the SOC adjustment routine
                    inverter.charge_start_time_minutes = minutes_start
                    inverter.charge_end_time_minutes = minutes_end
                elif ((minutes_start - self.minutes_now) >= (24 * 60)) and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes:
                    # No charging require in the next 24 hours
                    self.log("No charge window required for 24-hours, disabling before the start")
                    inverter.disable_charge_window()
                else:
                    if not self.inverter_set_charge_before:
                        self.log("No change to charge window yet, disabled while waiting for schedule.")
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
                    minutes_start = inverter.discharge_start_time_minutes
                    # Don't allow overlap with charge window
                    if minutes_start < inverter.charge_end_time_minutes and minutes_end >= inverter.charge_start_time_minutes:
                        minutes_start = window["start"]
                    else:
                        self.log(
                            "Include original discharge start {} with our start which is {} (charge start {} end {})".format(
                                self.time_abs_str(inverter.discharge_start_time_minutes),
                                self.time_abs_str(minutes_start),
                                self.time_abs_str(inverter.charge_start_time_minutes),
                                self.time_abs_str(inverter.charge_end_time_minutes),
                            )
                        )

                # Avoid having too long a period to configure as registers only support 24-hours
                if (minutes_start < self.minutes_now) and ((minutes_end - minutes_start) >= 24 * 60):
                    minutes_start = int(self.minutes_now / 30) * 30
                    self.log("Move on discharge window start time to avoid wrap - new start {}".format(self.time_abs_str(minutes_start)))

                discharge_adjust = 1
                # Span midnight allowed?
                if not inverter.inv_can_span_midnight:
                    if minutes_start < 24 * 60 and minutes_end >= 24 * 60:
                        minutes_end = 24 * 60 - 1
                    discharge_adjust = 0

                discharge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                discharge_end_time = self.midnight_utc + timedelta(minutes=(minutes_end + discharge_adjust))  # Add in 1 minute margin to allow Predbat to restore ECO mode
                discharge_soc = (self.discharge_limits_best[0] * self.soc_max) / 100.0
                self.log("Next discharge window will be: {} - {} at reserve {}".format(discharge_start_time, discharge_end_time, self.discharge_limits_best[0]))
                if (self.minutes_now >= minutes_start) and (self.minutes_now < minutes_end) and (self.discharge_limits_best[0] < 100.0):
                    if not self.set_discharge_freeze_only and ((self.soc_kw - PREDICT_STEP * inverter.battery_rate_max_discharge_scaled) >= discharge_soc):
                        self.log("Discharging now - current SOC {} and target {}".format(self.soc_kw, self.dp2(discharge_soc)))
                        inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
                        inverter.adjust_force_discharge(True, discharge_start_time, discharge_end_time)
                        resetDischarge = False
                        isDischarging = True

                        if self.set_reserve_enable:
                            inverter.adjust_reserve(self.discharge_limits_best[0])
                            setReserve = True

                        status = "Discharging"
                        status_extra = " target {}%-{}%".format(inverter.soc_percent, self.discharge_limits_best[0])
                        # Immediate discharge mode
                        inverter.adjust_discharge_immediate(self.discharge_limits_best[0])
                    else:
                        inverter.adjust_force_discharge(False)
                        disabled_discharge = True
                        if self.set_discharge_freeze:
                            # In discharge freeze mode we disable charging during discharge slots
                            inverter.adjust_charge_rate(0)
                            inverter.adjust_pause_mode(pause_charge=True)
                            self.log("Discharge Freeze as discharge is now at/below target - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                            status = "Freeze discharging"
                            status_extra = " target {}%-{}%".format(inverter.soc_percent, self.discharge_limits_best[0])
                            isDischarging = True
                        else:
                            status = "Hold discharging"
                            status_extra = " target {}%-{}%".format(inverter.soc_percent, self.discharge_limits_best[0])
                            self.log(
                                "Discharge Hold (ECO mode) as discharge is now at/below target or freeze only is set - current SOC {} and target {}".format(
                                    self.soc_kw, discharge_soc
                                )
                            )
                            inverter.adjust_pause_mode()
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

                    if self.set_discharge_freeze and not isCharging:
                        # In discharge freeze mode we disable charging during discharge slots, so turn it back on otherwise
                        inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            elif self.set_discharge_window:
                self.log("Setting ECO mode as no discharge window planned")
                inverter.adjust_force_discharge(False)
                resetReserve = True
                if self.set_discharge_freeze and not isCharging:
                    # In discharge freeze mode we disable charging during discharge slots, so turn it back on otherwise
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)

            # Car charging from battery disable?
            if not self.car_charging_from_battery:
                for car_n in range(self.num_cars):
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
                                inverter.adjust_pause_mode(pause_discharge=True)
                                resetDischarge = False
                                self.log("Disabling battery discharge while the car {} is charging".format(car_n))
                                if status != "Idle":
                                    status += ", Hold for car"
                                else:
                                    status = "Hold for car"
                            break

            # Charging/Discharging off via service
            if not isCharging and (not isDischarging or disabled_discharge) and self.set_charge_window:
                inverter.adjust_charge_immediate(0)

            # Pause charge off
            if not isCharging and not isDischarging:
                inverter.adjust_pause_mode()

            # Reset discharge rate?
            if resetDischarge:
                inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)

            # Set the SOC just before or within the charge window
            if self.set_soc_enable:
                if (isDischarging and not disabled_discharge) and not self.set_reserve_enable:
                    # If we are discharging and not setting reserve then we should reset the target SOC to 0%
                    # as some inverters can use this as a target for discharge
                    inverter.adjust_battery_target(self.discharge_limits_best[0], False)
                elif (
                    self.charge_limit_best
                    and (self.minutes_now < inverter.charge_end_time_minutes)
                    and ((inverter.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes)
                    and not (disabled_charge_window)
                ):
                    if inverter.inv_has_charge_enable_time or isCharging:
                        # In charge freeze hold the target SOC at the current value
                        if self.set_charge_freeze and (self.charge_limit_best[0] == self.reserve):
                            if isCharging:
                                self.log("Within charge freeze setting target soc to current soc {}".format(inverter.soc_percent))
                                inverter.adjust_battery_target(inverter.soc_percent, True)
                            else:
                                # Not yet in the freeze, hold at 100% target SOC
                                inverter.adjust_battery_target(100.0, False)
                        else:
                            # If not charging and not hybrid we should reset the target % to 100 to avoid losing solar
                            if not self.inverter_hybrid and self.inverter_soc_reset and not isCharging:
                                inverter.adjust_battery_target(100.0, False)
                            else:
                                inverter.adjust_battery_target(self.charge_limit_percent_best[0], isCharging)
                    else:
                        if not inverter.inv_has_target_soc:
                            # If the inverter doesn't support target soc and soc_enable is on then do that logic here:
                            inverter.mimic_target_soc(0)
                        elif not self.inverter_hybrid and self.inverter_soc_reset:
                            # AC Coupled, charge to 0 on solar
                            inverter.adjust_battery_target(100.0, False)
                        else:
                            # Hybrid, no charge timer, set target soc back to 0
                            inverter.adjust_battery_target(0, False)
                else:
                    if not inverter.inv_has_target_soc:
                        # If the inverter doesn't support target soc and soc_enable is on then do that logic here:
                        inverter.mimic_target_soc(0)
                    elif not self.inverter_hybrid and self.inverter_soc_reset:
                        if (
                            self.charge_limit_best
                            and (self.minutes_now >= inverter.charge_start_time_minutes)
                            and (self.minutes_now < inverter.charge_end_time_minutes)
                            and (not disabled_charge_window)
                        ):
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
                                "Resetting charging SOC as we are not within the window or charge is disabled and inverter_soc_reset is enabled (now {} target set_soc_minutes {} charge start time {})".format(
                                    self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)
                                )
                            )
                            inverter.adjust_battery_target(100.0, False)
                    else:
                        self.log(
                            "Not setting charging SOC as we are not within the window (now {} target set_soc_minutes {} charge start time {})".format(
                                self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)
                            )
                        )
                        if not inverter.inv_has_charge_enable_time:
                            inverter.adjust_battery_target(0, False)

            # If we should set reserve during charging
            if self.set_soc_enable and self.set_reserve_enable and not setReserve:
                if self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and (self.minutes_now >= inverter.charge_start_time_minutes):
                    # Compute limit to account for freeze setting
                    if self.charge_limit_best[0] == self.reserve:
                        if self.set_charge_freeze:
                            limit = inverter.soc_percent
                        else:
                            limit = 0
                    else:
                        limit = self.charge_limit_percent_best[0]

                    # Only set the reserve when we reach the desired percent
                    if (inverter.soc_percent > limit) or (not disabled_charge_window):
                        self.log("Adjust reserve to default as SOC {} % is above target {} % or charging active".format(inverter.soc_percent, limit))
                        inverter.adjust_reserve(0)
                    else:
                        self.log("Adjust reserve to target charge {} % (set_reserve_enable is true)".format(limit))
                        inverter.adjust_reserve(min(limit + 1, 100))
                    resetReserve = False
                else:
                    self.log("Adjust reserve to default (as set_reserve_enable is true)")
                    inverter.adjust_reserve(0)
                    resetReserve = False

            # Reset reserve as discharge is enable but not running right now
            if self.set_reserve_enable and resetReserve and not setReserve:
                inverter.adjust_reserve(0)

        # Set the charge/discharge status information
        self.set_charge_discharge_status(isCharging and not disabled_charge_window, isDischarging and not disabled_discharge)
        self.isCharging = isCharging
        self.isDischarging = isDischarging
        return status, status_extra

    def fetch_carbon_intensity(self, entity_id):
        """
        Fetch the carbon intensity from the sensor
        Returns a dictionary with the carbon intensity data for the next 24 hours
        And a secondary dictionary with the carbon intensity data for the last 24 hours

        :param entity_id: The entity_id of the sensor
        """
        data_all = []
        carbon_data = {}
        carbon_history = {}

        if entity_id:
            self.log("Fetching carbon intensity data from {}".format(entity_id))
            data_all = self.get_state_wrapper(entity_id=entity_id, attribute="forecast")
            if data_all:
                carbon_data = self.minute_data(data_all, self.forecast_days, self.now_utc, "intensity", "from", backwards=False, to_key="to")

        entity_id = self.prefix + ".carbon_now"
        state = self.get_state_wrapper(entity_id=entity_id)
        if state is not None:
            try:
                carbon_history = self.minute_data_import_export(self.now_utc, entity_id, required_unit="g/kWh", increment=False, smoothing=False)
            except ValueError:
                self.log("Warn: No carbon intensity history in sensor {}".format(entity_id))
        else:
            self.log("Warn: No carbon intensity history in sensor {}".format(entity_id))

        return carbon_data, carbon_history

    def fetch_octopus_rates(self, entity_id, adjust_key=None):
        """
        Fetch the Octopus rates from the sensor

        :param entity_id: The entity_id of the sensor
        :param adjust_key: The key use to find Octopus Intelligent adjusted rates
        """
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
                data_import = self.get_state_wrapper(entity_id=prev_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    prev_rate_id = entity_id.replace("_current_rate", "_previous_rate")
                    data_import = self.get_state_wrapper(entity_id=prev_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
                    else:
                        self.log("WARN: No Octopus data in sensor {} attribute 'all_rates'".format(prev_rate_id))

            # Current rates
            if "_current_rate" in entity_id:
                current_rate_id = entity_id.replace("_current_rate", "_current_day_rates").replace("sensor.", "event.")
            else:
                current_rate_id = entity_id

            data_import = (
                self.get_state_wrapper(entity_id=current_rate_id, attribute="rates")
                or self.get_state_wrapper(entity_id=current_rate_id, attribute="all_rates")
                or self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_today")
            )
            if data_import:
                data_all += data_import
            else:
                self.log("WARN: No Octopus data in sensor {} attribute 'all_rates' / 'rates' / 'raw_today'".format(current_rate_id))

            # Next rates
            if "_current_rate" in entity_id:
                next_rate_id = entity_id.replace("_current_rate", "_next_day_rates").replace("sensor.", "event.")
                data_import = self.get_state_wrapper(entity_id=next_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    next_rate_id = entity_id.replace("_current_rate", "_next_rate")
                    data_import = self.get_state_wrapper(entity_id=next_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
            else:
                # Nordpool tomorrow
                data_import = self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_tomorrow")
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
            if rate_key not in data_all[0]:
                rate_key = "value"
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
        self.carbon_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.pv_today = {}
        self.load_minutes = {}
        self.load_minutes_age = 0
        self.load_forecast = {}
        self.pv_forecast_minute = {}
        self.pv_forecast_minute10 = {}
        self.load_scaling_dynamic = {}
        self.carbon_intensity = {}
        self.carbon_history = {}

        # iBoost load data
        if self.iboost_enable and "iboost_energy_today" in self.args:
            self.iboost_energy_today, iboost_energy_age = self.minute_data_load(self.now_utc, "iboost_energy_today", 1, required_unit="kWh")
            if iboost_energy_age >= 1:
                self.iboost_today = self.dp2(abs(self.iboost_energy_today[0] - self.iboost_energy_today[self.minutes_now]))
                self.log("iBoost energy today from sensor reads {} kWh".format(self.iboost_today))

        # Fetch extra load forecast
        self.load_forecast = self.fetch_extra_load_forecast(self.now_utc)

        # Load previous load data
        if self.get_arg("ge_cloud_data", False):
            self.download_ge_data(self.now_utc)
        else:
            # Load data
            if "load_today" in self.args:
                self.load_minutes, self.load_minutes_age = self.minute_data_load(self.now_utc, "load_today", self.max_days_previous, required_unit="kWh")
                self.log("Found {} load_today datapoints going back {} days".format(len(self.load_minutes), self.load_minutes_age))
                self.load_minutes_now = max(self.load_minutes.get(0, 0) - self.load_minutes.get(self.minutes_now, 0), 0)
            else:
                if self.load_forecast:
                    self.log("Info: Using load forecast from load_forecast sensor")
                    self.load_minutes_now = self.load_forecast.get(0, 0)
                    self.load_minutes_age = 0
                else:
                    self.log("Error: You have not set load_today or load_forecast, you will have no load data")
                    self.record_status(message="Error - load_today not set correctly", had_errors=True)
                    raise ValueError

            # Load import today data
            if "import_today" in self.args:
                self.import_today = self.minute_data_import_export(self.now_utc, "import_today", scale=self.import_export_scaling, required_unit="kWh")
                self.import_today_now = max(self.import_today.get(0, 0) - self.import_today.get(self.minutes_now, 0), 0)
            else:
                self.log("WARN: You have not set import_today in apps.yaml, you will have no previous import data")

            # Load export today data
            if "export_today" in self.args:
                self.export_today = self.minute_data_import_export(self.now_utc, "export_today", scale=self.import_export_scaling, required_unit="kWh")
                self.export_today_now = max(self.export_today.get(0, 0) - self.export_today.get(self.minutes_now, 0), 0)
            else:
                self.log("WARN: You have not set export_today in apps.yaml, you will have no previous export data")

            # PV today data
            if "pv_today" in self.args:
                self.pv_today = self.minute_data_import_export(self.now_utc, "pv_today", required_unit="kWh")
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
        elif "rates_gas" in self.args:
            self.rate_gas = self.basic_rates(self.get_arg("rates_gas", [], indirect=False), "gas")
            self.rate_gas, self.rate_gas_replicated = self.rate_replicate(self.rate_gas, is_import=False, is_gas=False)
            self.rate_gas = self.rate_scan_gas(self.rate_gas, print=True)

        # Carbon intensity data
        if self.carbon_enable and ("carbon_intensity" in self.args):
            entity_id = self.get_arg("carbon_intensity", None, indirect=False)
            self.carbon_intensity, self.carbon_history = self.fetch_carbon_intensity(entity_id)

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
                completed = self.get_state_wrapper(entity_id=entity_id, attribute="completedDispatches") or self.get_state_wrapper(
                    entity_id=entity_id, attribute="completed_dispatches"
                )
                planned = self.get_state_wrapper(entity_id=entity_id, attribute="plannedDispatches") or self.get_state_wrapper(entity_id=entity_id, attribute="planned_dispatches")
                vehicle = self.get_state_wrapper(entity_id=entity_id, attribute="registeredKrakenflexDevice")
                vehicle_pref = self.get_state_wrapper(entity_id=entity_id, attribute="vehicleChargingPreferences")
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
                    size = self.get_state_wrapper(entity_id=entity_id, attribute="vehicle_battery_size_in_kwh")
                    rate = self.get_state_wrapper(entity_id=entity_id, attribute="charge_point_power_in_kw")
                    if size:
                        self.car_charging_battery_size[0] = size
                    if rate:
                        self.car_charging_rate[0] = rate

                # Get car charging limit again from car based on new battery size
                self.car_charging_limit[0] = self.dp3((float(self.get_arg("car_charging_limit", 100.0, index=0)) * self.car_charging_battery_size[0]) / 100.0)

                # Extract vehicle preference if we can get it
                if vehicle_pref and self.octopus_intelligent_charging:
                    octopus_limit = max(float(vehicle_pref.get("weekdayTargetSoc", 100)), float(vehicle_pref.get("weekendTargetSoc", 100)))
                    octopus_ready_time = vehicle_pref.get("weekdayTargetTime", None)
                    if not octopus_ready_time:
                        octopus_ready_time = self.car_charging_plan_time[0]
                    else:
                        octopus_ready_time += ":00"
                    self.car_charging_plan_time[0] = octopus_ready_time
                    octopus_limit = self.dp3(octopus_limit * self.car_charging_battery_size[0] / 100.0)
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
                        octopus_limit = self.dp3(float(octopus_limit) * self.car_charging_battery_size[0] / 100.0)
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

        # Work out car SOC and reset next
        self.car_charging_soc = [0.0 for car_n in range(self.num_cars)]
        self.car_charging_soc_next = [None for car_n in range(self.num_cars)]
        for car_n in range(self.num_cars):
            if (car_n == 0) and self.car_charging_manual_soc:
                self.car_charging_soc[car_n] = self.get_arg("car_charging_manual_soc_kwh")
            else:
                self.car_charging_soc[car_n] = (self.get_arg("car_charging_soc", 0.0, index=car_n) * self.car_charging_battery_size[car_n]) / 100.0
        if self.num_cars:
            self.log(
                "Current Car SOC kWh: {} Charge limit {} plan time {} battery size {}".format(
                    self.car_charging_soc, self.car_charging_limit, self.car_charging_plan_time, self.car_charging_battery_size
                )
            )

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

                joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")
                if not joined_events:
                    entity_id = entity_id.replace("binary_sensor.", "event.").replace("_sessions", "_session_events")
                    joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")

                available_events = self.get_state_wrapper(entity_id=entity_id, attribute="available_events")
                if available_events:
                    for event in available_events:
                        code = event.get("code", None)  # decode the available events structure for code, start/end time & rate
                        start = event.get("start", None)
                        end = event.get("end", None)
                        start_time = self.str2time(start)  # reformat the saving session start & end time for improved readability
                        end_time = self.str2time(end)
                        saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                        if code:  # Join the new Octopus saving event and send an alert
                            self.log(
                                "Joining Octopus saving event code {} {}-{} at rate {} p/kWh".format(
                                    code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate
                                )
                            )
                            self.call_service("octopus_energy/join_octoplus_saving_session_event", event_code=code, entity_id=entity_id)
                            self.call_notify(
                                "Predbat: Joined Octopus saving event {}-{}, {} p/kWh".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate)
                            )

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
            self.load_saving_slot(octopus_saving_slots, export=False, rate_replicate=self.rate_import_replicated)
            if "rates_import_override" in self.args:
                self.rate_import = self.basic_rates(self.get_arg("rates_import_override", [], indirect=False), "import", self.rate_import, self.rate_import_replicated)
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
                self.load_saving_slot(octopus_saving_slots, export=True, rate_replicate=self.rate_export_replicated)
            if "rates_export_override" in self.args:
                self.rate_export = self.basic_rates(self.get_arg("rates_export_override", [], indirect=False), "export", self.rate_export, self.rate_export_replicated)
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
        for car_n in range(self.num_cars):
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
            self.cost_today_sofar, self.carbon_today_sofar = self.today_cost(self.import_today, self.export_today)

        # Fetch PV forecast if enabled, today must be enabled, other days are optional
        self.pv_forecast_minute, self.pv_forecast_minute10 = self.fetch_pv_forecast()

        # Apply modal filter to historical data
        if self.load_minutes and not self.load_forecast_only:
            self.previous_days_modal_filter(self.load_minutes)
            self.log("Historical days now {} weight {}".format(self.days_previous, self.days_previous_weight))

        # Load today vs actual
        if self.load_minutes:
            self.load_inday_adjustment = self.load_today_comparison(self.load_minutes, self.load_forecast, self.car_charging_energy, self.import_today, self.minutes_now)
        else:
            self.load_inday_adjustment = 1.0

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

        # And gas
        if self.rate_gas:
            self.publish_rates(self.rate_gas, False, gas=True)

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
        for id in range(self.num_inverters):
            inverter = Inverter(self, id)
            inverter.update_status(self.minutes_now)

            if id == 0 and (not self.computed_charge_curve or self.battery_charge_power_curve_auto) and not self.battery_charge_power_curve:
                curve = inverter.find_charge_curve(discharge=False)
                if curve and self.battery_charge_power_curve_auto:
                    self.log("Saved computed battery charge power curve")
                    self.battery_charge_power_curve = curve
                self.computed_charge_curve = True
            if id == 0 and (not self.computed_discharge_curve or self.battery_discharge_power_curve_auto) and not self.battery_discharge_power_curve:
                curve = inverter.find_charge_curve(discharge=True)
                if curve and self.battery_discharge_power_curve_auto:
                    self.log("Saved computed battery discharge power curve")
                    self.battery_discharge_power_curve = curve
                self.computed_discharge_curve = True

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
                if not inverter.inv_support_charge_freeze:
                    # Force off unsupported feature
                    self.log("Note: Inverter does not support charge freeze - disabled")
                    self.set_charge_freeze = False
                if not inverter.inv_has_reserve_soc:
                    self.log("Note: Inverter does not support reserve - disabling reserve functions")
                    self.set_reserve_enable = False
                    self.set_reserve_hold = False
                    self.set_discharge_during_charge = False
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
        self.reserve_percent = calc_percent_limit(self.reserve, self.soc_max)
        self.reserve_current = self.dp2(self.reserve_current)
        self.reserve_current_percent = calc_percent_limit(self.reserve_current, self.soc_max)

        self.log(
            "Found {} inverters totals: min reserve {} current reserve {} soc_max {} soc {} charge rate {} kW discharge rate {} kW battery_rate_min {} w ac limit {} export limit {} kW loss charge {} % loss discharge {} % inverter loss {} %".format(
                len(self.inverters),
                self.reserve,
                self.reserve_current,
                self.soc_max,
                self.soc_kw,
                self.charge_rate_now * 60,
                self.discharge_rate_now * 60,
                self.battery_rate_min * MINUTE_WATT,
                self.dp2(self.inverter_limit * 60),
                self.dp2(self.export_limit * 60),
                100 - int(self.battery_loss * 100),
                100 - int(self.battery_loss_discharge * 100),
                100 - int(self.inverter_loss * 100),
            )
        )

        # Work out current charge limits and publish charge limit base
        self.charge_limit = [self.current_charge_limit * self.soc_max / 100.0 for i in range(len(self.charge_window))]
        self.charge_limit_percent = calc_percent_limit(self.charge_limit, self.soc_max)
        self.publish_charge_limit(self.charge_limit, self.charge_window, self.charge_limit_percent, best=False)

        self.log("Base charge    window {}".format(self.window_as_text(self.charge_window, self.charge_limit_percent)))
        self.log("Base discharge window {}".format(self.window_as_text(self.discharge_window, self.discharge_limits)))

    async def async_manual_select(self, config_item, value):
        """
        Async wrapper for selection on manual times dropdown
        """
        return await self.run_in_executor(self.manual_select, config_item, value)

    def manual_select(self, config_item, value):
        """
        Selection on manual times dropdown
        """
        item = self.config_index.get(config_item)
        if not item:
            return
        if not value:
            # Ignore null selections
            return
        if value.startswith("+"):
            # Ignore selections which are just the current value
            return
        values = item.get("value", "")
        if not values:
            values = ""
        values = values.replace("+", "")
        values_list = []
        exclude_list = []
        if values:
            values_list = values.split(",")
        if value == "off":
            values_list = []
        elif "[" in value:
            value = value.replace("[", "")
            value = value.replace("]", "")
            if value in values_list:
                values_list.remove(value)
        else:
            if value not in values_list:
                values_list.append(value)
                exclude_list.append(value)
        item_value = ",".join(values_list)
        if item_value:
            item_value = "+" + item_value

        if not item_value:
            item_value = "off"
        self.manual_times(config_item, new_value=item_value)

        # Update other drop downs that may need this time excluding
        for item in CONFIG_ITEMS:
            if item["name"] != config_item and item.get("manual"):
                value = item.get("value", "")
                if value and value != "reset" and exclude_list:
                    self.manual_times(item["name"], exclude=exclude_list)

    def manual_times(self, config_item, exclude=[], new_value=None):
        """
        Update manual times sensor
        """
        time_overrides = []
        minutes_now = int(self.minutes_now / 30) * 30
        manual_time_max = 18 * 60

        # Deconstruct the value into a list of minutes
        item = self.config_index.get(config_item)
        if new_value:
            values = new_value
        else:
            values = item.get("value", "")
        values = values.replace("+", "")
        values_list = []
        if values:
            values_list = values.split(",")
        for value in values_list:
            if value == "off":
                continue
            try:
                start_time = datetime.strptime(value, "%H:%M:%S")
            except ValueError:
                start_time = None
            if start_time:
                minutes = start_time.hour * 60 + start_time.minute
                if minutes < minutes_now:
                    minutes += 24 * 60
                if (minutes - minutes_now) < manual_time_max:
                    time_overrides.append(minutes)

        # Reconstruct the list in order based on minutes
        values_list = []
        for minute in time_overrides:
            minute_str = (self.midnight + timedelta(minutes=minute)).strftime("%H:%M:%S")
            if minute_str not in exclude:
                values_list.append(minute_str)
        values = ",".join(values_list)
        if values:
            values = "+" + values

        # Create the new dropdown
        time_values = []
        for minute in range(minutes_now, minutes_now + manual_time_max, 30):
            minute_str = (self.midnight + timedelta(minutes=minute)).strftime("%H:%M:%S")
            if minute in time_overrides:
                minute_str = "[" + minute_str + "]"
            time_values.append(minute_str)

        if values not in time_values:
            time_values.append(values)
        time_values.append("off")
        item["options"] = time_values
        if not values:
            values = "off"
        self.expose_config(config_item, values, force=True)

        if time_overrides:
            time_txt = []
            for minute in time_overrides:
                time_txt.append(self.time_abs_str(minute))
        return time_overrides

    def fetch_config_options(self):
        """
        Fetch all the configuration options
        """

        self.debug_enable = self.get_arg("debug_enable")
        self.previous_status = self.get_state_wrapper(self.prefix + ".status")
        forecast_hours = max(self.get_arg("forecast_hours", 48), 24)

        self.num_cars = self.get_arg("num_cars", 1)
        self.calculate_plan_every = max(self.get_arg("calculate_plan_every"), 5)

        self.log(
            "Configuration: forecast_hours {} num_cars {} debug enable is {} calculate_plan_every {}".format(
                forecast_hours, self.num_cars, self.debug_enable, self.calculate_plan_every
            )
        )

        # Days previous
        self.holiday_days_left = self.get_arg("holiday_days_left")
        self.load_forecast_only = self.get_arg("load_forecast_only", False)

        self.days_previous = self.get_arg("days_previous", [7])
        self.days_previous_weight = self.get_arg("days_previous_weight", [1 for i in range(len(self.days_previous))])
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(len(self.days_previous) - len(self.days_previous_weight))]
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
        self.load_scaling_saving = self.get_arg("load_scaling_saving")
        self.battery_rate_max_scaling = self.get_arg("battery_rate_max_scaling")
        self.battery_rate_max_scaling_discharge = self.get_arg("battery_rate_max_scaling_discharge")

        self.best_soc_step = 0.25
        self.metric_cloud_enable = self.get_arg("metric_cloud_enable")
        self.metric_load_divergence_enable = self.get_arg("metric_load_divergence_enable")

        # Battery charging options
        self.battery_capacity_nominal = self.get_arg("battery_capacity_nominal")
        self.battery_loss = 1.0 - self.get_arg("battery_loss")
        self.battery_loss_discharge = 1.0 - self.get_arg("battery_loss_discharge")
        self.inverter_loss = 1.0 - self.get_arg("inverter_loss")
        self.inverter_hybrid = self.get_arg("inverter_hybrid")

        # Charge curve
        if self.args.get("battery_charge_power_curve", "") == "auto":
            self.battery_charge_power_curve_auto = True
        else:
            self.battery_charge_power_curve_auto = False
            self.battery_charge_power_curve = self.args.get("battery_charge_power_curve", {})
            # Check power curve is a dictionary
            if not isinstance(self.battery_charge_power_curve, dict):
                self.battery_charge_power_curve = {}
                self.log("WARN: battery_charge_power_curve is incorrectly configured - ignoring")
                self.record_status("battery_charge_power_curve is incorrectly configured - ignoring", had_errors=True)

        # Discharge curve
        if self.args.get("battery_discharge_power_curve", "") == "auto":
            self.battery_discharge_power_curve_auto = True
        else:
            self.battery_discharge_power_curve_auto = False
            self.battery_discharge_power_curve = self.args.get("battery_discharge_power_curve", {})
            # Check power curve is a dictionary
            if not isinstance(self.battery_discharge_power_curve, dict):
                self.battery_discharge_power_curve = {}
                self.log("WARN: battery_discharge_power_curve is incorrectly configured - ignoring")
                self.record_status("battery_discharge_power_curve is incorrectly configured - ignoring", had_errors=True)

        self.import_export_scaling = self.get_arg("import_export_scaling", 1.0)
        self.best_soc_margin = 0.0
        self.best_soc_min = self.get_arg("best_soc_min")
        self.best_soc_max = self.get_arg("best_soc_max")
        self.best_soc_keep = self.get_arg("best_soc_keep")
        self.set_soc_minutes = 30
        self.set_window_minutes = 30
        self.inverter_set_charge_before = self.get_arg("inverter_set_charge_before")
        if not self.inverter_set_charge_before:
            self.set_soc_minutes = 0
            self.set_window_minutes = 0
        self.octopus_intelligent_charging = self.get_arg("octopus_intelligent_charging")
        self.octopus_intelligent_ignore_unplugged = self.get_arg("octopus_intelligent_ignore_unplugged")
        self.get_car_charging_planned()
        self.load_inday_adjustment = 1.0

        self.combine_rate_threshold = self.get_arg("combine_rate_threshold")
        self.combine_discharge_slots = self.get_arg("combine_discharge_slots")
        self.combine_charge_slots = self.get_arg("combine_charge_slots")
        self.charge_slot_split = 30
        self.discharge_slot_split = 30
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
            self.set_soc_enable = True
        elif self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGE]:
            self.calculate_best_charge = True
            self.calculate_best_discharge = False
            self.set_charge_window = True
            self.set_discharge_window = False
            self.set_soc_enable = True
        elif self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGEDISCHARGE]:
            self.calculate_best_charge = True
            self.calculate_best_discharge = True
            self.set_charge_window = True
            self.set_discharge_window = True
            self.set_soc_enable = True
        else:  # PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]
            self.calculate_best_charge = False
            self.calculate_best_discharge = False
            self.set_charge_window = False
            self.set_discharge_window = False
            self.predbat_mode = PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]
            self.set_soc_enable = False
            self.expose_config("mode", self.predbat_mode)

        self.log("Predbat mode is set to {}".format(self.predbat_mode))

        self.calculate_discharge_oncharge = self.get_arg("calculate_discharge_oncharge")
        self.calculate_second_pass = self.get_arg("calculate_second_pass")
        self.calculate_inday_adjustment = self.get_arg("calculate_inday_adjustment")
        self.calculate_tweak_plan = self.get_arg("calculate_tweak_plan")
        self.calculate_regions = True
        self.calculate_secondary_order = self.get_arg("calculate_secondary_order")

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

        # iBoost solar diverter model
        self.iboost_enable = self.get_arg("iboost_enable")
        self.calculate_savings = True
        self.carbon_enable = self.get_arg("carbon_enable")
        self.carbon_metric = self.get_arg("carbon_metric")
        self.iboost_solar = self.get_arg("iboost_solar")
        self.iboost_gas = self.get_arg("iboost_gas")
        self.iboost_charging = self.get_arg("iboost_charging")
        self.iboost_gas_scale = self.get_arg("iboost_gas_scale")

        self.iboost_max_energy = self.get_arg("iboost_max_energy")
        self.iboost_max_power = self.get_arg("iboost_max_power") / MINUTE_WATT
        self.iboost_min_power = self.get_arg("iboost_min_power") / MINUTE_WATT
        self.iboost_min_soc = self.get_arg("iboost_min_soc")
        self.iboost_today = self.get_arg("iboost_today")
        self.iboost_next = self.iboost_today
        self.iboost_running = False
        self.iboost_energy_today = {}

        # Car options
        self.car_charging_hold = self.get_arg("car_charging_hold")
        self.car_charging_manual_soc = self.get_arg("car_charging_manual_soc")
        self.car_charging_threshold = float(self.get_arg("car_charging_threshold")) / 60.0
        self.car_charging_energy_scale = self.get_arg("car_charging_energy_scale")

        # Update list of slot times
        self.manual_charge_times = self.manual_times("manual_charge")
        self.manual_discharge_times = self.manual_times("manual_discharge")
        self.manual_freeze_charge_times = self.manual_times("manual_freeze_charge")
        self.manual_freeze_discharge_times = self.manual_times("manual_freeze_discharge")
        self.manual_idle_times = self.manual_times("manual_idle")
        self.manual_all_times = (
            self.manual_charge_times + self.manual_discharge_times + self.manual_idle_times + self.manual_freeze_charge_times + self.manual_freeze_discharge_times
        )
        # Update list of config options to save/restore to
        self.update_save_restore_list()

    def update_time(self, print=True):
        """
        Update the current time/date
        """
        local_tz = pytz.timezone(self.args.get("timezone", "Europe/London"))
        skew = self.args.get("clock_skew", 0)
        if skew:
            self.log("WARN: Clock skew is set to {} minutes".format(skew))
        self.now_utc_real = datetime.now(local_tz)
        now_utc = self.now_utc_real + timedelta(minutes=skew)
        now = datetime.now() + timedelta(minutes=skew)
        now = now.replace(second=0, microsecond=0, minute=(now.minute - (now.minute % PREDICT_STEP)))
        now_utc = now_utc.replace(second=0, microsecond=0, minute=(now_utc.minute - (now_utc.minute % PREDICT_STEP)))
        if SIMULATE:
            now += timedelta(minutes=self.simulate_offset)
            now_utc += timedelta(minutes=self.simulate_offset)

        self.now_utc = now_utc
        self.now = now
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = self.minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        self.minutes_to_midnight = 24 * 60 - self.minutes_now
        self.log("--------------- PredBat - update at {} with clock skew {} minutes, minutes now {}".format(now_utc, skew, self.minutes_now))

    def update_pred(self, scheduled=True):
        """
        Update the prediction state, everything is called from here right now
        """
        status_extra = ""
        self.had_errors = False
        self.dashboard_index = []

        self.update_time()

        self.expose_config("active", True)

        # Check our version
        self.download_predbat_releases()

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
        recompute = self.calculate_plan(recompute=recompute)

        # Publish rate data
        self.publish_rate_and_threshold()

        # Execute the plan, re-read the inverter first if we had to calculate (as time passes during calculations)
        if recompute:
            self.fetch_inverter_data()
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
                # Calculate an updated plan, fetch the inverter data again and execute the plan
                self.calculate_plan(recompute=True)
                self.fetch_inverter_data()
                status, status_extra = self.execute_plan()
            else:
                self.log("Will not recompute the plan, it is {} minutes old and max age is {} minutes".format(self.dp1(plan_age_minutes), self.calculate_plan_every))

        # iBoost solar diverter model update state, only on 5 minute intervals
        if self.iboost_enable and scheduled:
            if self.iboost_energy_today:
                # If we have a realtime sensor just use that data
                self.iboost_next = self.iboost_today
            elif recompute and (self.minutes_now >= 0) and (self.minutes_now < self.calculate_plan_every):
                # Reset at midnight
                self.iboost_next = 0
            # Save next iBoost model value
            self.expose_config("iboost_today", self.iboost_next)
            self.log("iBoost model today updated to {}".format(self.iboost_next))

        if self.calculate_savings:
            # Get current totals
            savings_total_predbat = self.load_previous_value_from_ha(self.prefix + ".savings_total_predbat")
            try:
                savings_total_predbat = float(savings_total_predbat)
            except (ValueError, TypeError):
                savings_total_predbat = 0.0

            savings_total_pvbat = self.load_previous_value_from_ha(self.prefix + ".savings_total_pvbat")
            try:
                savings_total_pvbat = float(savings_total_pvbat)
            except (ValueError, TypeError):
                savings_total_pvbat = 0.0

            savings_total_soc = self.load_previous_value_from_ha(self.prefix + ".savings_total_soc")
            try:
                savings_total_soc = float(savings_total_soc)
            except (ValueError, TypeError):
                savings_total_soc = 0.0

            savings_total_actual = self.load_previous_value_from_ha(self.prefix + ".savings_total_actual")
            try:
                savings_total_actual = float(savings_total_actual)
            except (ValueError, TypeError):
                savings_total_actual = 0.0

            # Increment total at midnight for next day
            if (self.minutes_now >= 0) and (self.minutes_now < self.calculate_plan_every) and scheduled and recompute:
                savings_total_predbat += self.savings_today_predbat
                savings_total_pvbat += self.savings_today_pvbat
                savings_total_soc = self.savings_today_predbat_soc
                savings_total_actual += self.savings_today_actual

            self.dashboard_item(
                self.prefix + ".savings_total_predbat",
                state=self.dp2(savings_total_predbat),
                attributes={
                    "friendly_name": "Total Predbat savings",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "pounds": self.dp2(savings_total_predbat / 100.0),
                    "icon": "mdi:cash-multiple",
                },
            )
            self.dashboard_item(
                self.prefix + ".savings_total_soc",
                state=self.dp2(savings_total_soc),
                attributes={
                    "friendly_name": "Predbat savings, yesterday SOC",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-50",
                },
            )
            self.dashboard_item(
                self.prefix + ".savings_total_actual",
                state=self.dp2(savings_total_actual),
                attributes={
                    "friendly_name": "Actual total energy cost",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "pounds": self.dp2(savings_total_actual / 100.0),
                    "icon": "mdi:cash-multiple",
                },
            )
            self.dashboard_item(
                self.prefix + ".savings_total_pvbat",
                state=self.dp2(savings_total_pvbat),
                attributes={
                    "friendly_name": "Total Savings vs no PV/Battery system",
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "pounds": self.dp2(savings_total_pvbat / 100.0),
                    "icon": "mdi:cash-multiple",
                },
            )

        # Car SOC increment
        if scheduled:
            for car_n in range(self.num_cars):
                if (car_n == 0) and self.car_charging_manual_soc:
                    self.log("Car charging Manual SOC current is {} next is {}".format(self.car_charging_soc[car_n], self.car_charging_soc_next[car_n]))
                    if self.car_charging_soc_next[car_n] is not None:
                        self.expose_config("car_charging_manual_soc_kwh", round(self.car_charging_soc_next[car_n], 3))

        # Holiday days left countdown, subtract a day at midnight every day
        if scheduled and self.holiday_days_left > 0 and self.minutes_now < RUN_EVERY:
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
        self.expose_config("active", False)

    async def update_event(self, event, data, kwargs):
        """
        Update event.

        This function is called when an update event is triggered. It handles the logic for updating the application.

        Parameters:
        - event (str): The name of the event that triggered the update.
        - data (dict): Additional data associated with the update event.
        - kwargs (dict): Additional keyword arguments passed to the update event.

        Returns:
        None
        """
        self.log("Update event {} {} {}".format(event, data, kwargs))
        if data and data.get("service", "") == "install":
            service_data = data.get("service_data", {})
            if service_data.get("entity_id", "") == "update.predbat_version":
                latest = self.releases.get("latest", None)
                if latest:
                    self.log("Requested install of latest version {}".format(latest))
                    await self.async_download_predbat_version(latest)
        elif data and data.get("service", "") == "skip":
            self.log("Requested to skip the update, this is not yet supported...")

    def download_predbat_file_from_github(self, tag, filename, new_filename):
        """
        Downloads a predbat source file from github and returns the contents

        Args:
            tag (str): The tag to download from (e.g. v1.0.0)
            filename (str): The filename to download (e.g. predbat.py)
            new_filename (str): The new filename to save the file as
        Returns:
            str: The contents of the file
        """
        url = "https://raw.githubusercontent.com/springfall2008/batpred/" + tag + "/apps/predbat/{}".format(filename)
        self.log("Downloading Predbat file from {} to {}".format(url, new_filename))
        r = requests.get(url, headers={})
        if r.ok:
            data = r.text
            if new_filename:
                self.log("Write new version {} bytes to {}".format(len(data), new_filename))
                with open(new_filename, "w") as han:
                    han.write(data)
            return data
        else:
            self.log("WARN: Downloading Predbat file failed, URL {}".format(url))
            return None

    async def async_download_predbat_version(self, version):
        """
        Sync wrapper for async download_predbat_version
        """
        return await self.run_in_executor(self.download_predbat_version, version)

    def download_predbat_version(self, version):
        """
        Download a version of Predbat from GitHub

        Args:
            version (str): The version of Predbat to download.

        Returns:
            bool: True if the download and update were successful, False otherwise.
        """
        if version == THIS_VERSION:
            self.log("WARN: Predbat update requested for the same version as we are running ({}), no update required".format(version))
            return

        self.expose_config("version", True, force=True, in_progress=True)
        tag_split = version.split(" ")
        this_path = os.path.dirname(__file__)
        self.log("Split returns {}".format(tag_split))
        if tag_split:
            tag = tag_split[0]

            # Download predbat.py
            file = "predbat.py"
            predbat_code = self.download_predbat_file_from_github(tag, file, os.path.join(this_path, file + "." + tag))
            if predbat_code:
                # Get the list of other files to download by searching for PREDBAT_FILES in predbat.py
                files = ["predbat.py"]
                for line in predbat_code.split("\n"):
                    if line.startswith("PREDBAT_FILES"):
                        files = line.split("=")[1].strip()
                        files = files.replace("[", "")
                        files = files.replace("]", "")
                        files = files.replace('"', "")
                        files = files.split(",")
                        self.log("Predbat update files are {}".format(files))
                        break

                # Download the remaining files
                if files:
                    for file in files:
                        # Download the remaining files
                        if file != "predbat.py":
                            self.download_predbat_file_from_github(tag, file, os.path.join(this_path, file + "." + tag))

                # Kill the current threads
                if self.pool:
                    self.log("WARN: Killing current threads before update...")
                    self.pool.close()
                    self.pool.join()
                    self.pool = None

                # Notify that we are about to update
                self.call_notify("Predbat: update to: {}".format(version))

                # Perform the update
                self.log("Perform the update.....")
                cmd = ""
                for file in files:
                    cmd += "mv -f {} {} && ".format(os.path.join(this_path, file + "." + tag), os.path.join(this_path, file))
                cmd += "echo 'Update complete'"
                self.log("Performing update with command: {}".format(cmd))
                os.system(cmd)
                return True
            else:
                self.log("WARN: Predbat update failed to download Predbat version {}".format(version))
        return False

    async def select_event(self, event, data, kwargs):
        """
        Catch HA Input select updates

        Parameters:
        - event: The event triggered by the input select.
        - data: The data associated with the event.
        - kwargs: Additional keyword arguments.

        Returns:
        None

        Description:
        This method is used to handle Home Assistant input select updates.
        It extracts the necessary information from the data and performs different actions based on the selected option.
        The actions include calling update service, saving and restoring settings, performing manual selection, and exposing configuration.
        After performing the actions, it triggers an update by setting update_pending flag to True and plan_valid flag to False.
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
                    await self.async_download_predbat_version(value)
                elif item["name"] == "saverestore":
                    if value == "save current":
                        await self.async_update_save_restore_list()
                        await self.async_save_settings_yaml()
                    elif value == "restore default":
                        await self.async_restore_settings_yaml(None)
                    else:
                        await self.async_restore_settings_yaml(value)
                elif item.get("manual"):
                    await self.async_manual_select(item["name"], value)
                else:
                    await self.async_expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    async def number_event(self, event, data, kwargs):
        """
        Catch HA Input number updates

        This method is called when there is an update to a Home Assistant input number entity.
        It extracts the value and entity ID from the event data and processes it accordingly.
        If the entity ID matches any of the entities specified in the CONFIG_ITEMS list,
        it logs the entity and value, exposes the configuration item, and updates the pending plan.

        Args:
            event (str): The event name.
            data (dict): The event data.
            kwargs (dict): Additional keyword arguments.

        Returns:
            None
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
                await self.async_expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    async def watch_event(self, entity, attribute, old, new, kwargs):
        """
        Catch HA state changes for watched entities
        """
        self.log("Watched event: {} = {} will trigger re-plan".format(entity, new))
        self.update_pending = True
        self.plan_valid = False

    async def switch_event(self, event, data, kwargs):
        """
        Catch HA Switch toggle

        This method is called when a Home Assistant switch is toggled. It handles the logic for updating the state of the switch
        and triggering any necessary actions based on the switch state.

        Parameters:
        - event (str): The event triggered by the switch toggle.
        - data (dict): Additional data associated with the event.
        - kwargs (dict): Additional keyword arguments.

        Returns:
        - None

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
                await self.async_expose_config(item["name"], value, event=True)
                self.update_pending = True
                self.plan_valid = False

    def get_ha_config(self, name, default):
        """
        Get Home assistant config value, use default if not set

        Parameters:
        name (str): The name of the config value to retrieve.
        default: The default value to use if the config value is not set.

        Returns:
        value: The value of the config if it is set, otherwise the default value.
        default: The default value passed as an argument.
        """
        item = self.config_index.get(name)
        if item and item["name"] == name:
            value = item.get("value", None)
            if default is None:
                default = item.get("default", None)
            if value is None:
                value = default
            return value, default
        return None, default

    async def async_expose_config(self, name, value, quiet=True, event=False, force=False, in_progress=False):
        return await self.run_in_executor(self.expose_config, name, value, quiet, event, force, in_progress)

    def expose_config(self, name, value, quiet=True, event=False, force=False, in_progress=False):
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
                if entity and ((item.get("value") is None) or (value != item["value"]) or force):
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
                        """INPUT_NUMBER"""
                        icon = item.get("icon", "mdi:numeric")
                        unit = item["unit"]
                        unit = unit.replace("£", self.currency_symbols[0])
                        unit = unit.replace("p", self.currency_symbols[1])
                        self.set_state_wrapper(
                            entity_id=entity,
                            state=value,
                            attributes={
                                "friendly_name": item["friendly_name"],
                                "min": item["min"],
                                "max": item["max"],
                                "step": item["step"],
                                "unit_of_measurement": unit,
                                "icon": icon,
                            },
                        )
                    elif item["type"] == "switch":
                        """SWITCH"""
                        icon = item.get("icon", "mdi:light-switch")
                        self.set_state_wrapper(entity_id=entity, state=("on" if value else "off"), attributes={"friendly_name": item["friendly_name"], "icon": icon})
                    elif item["type"] == "select":
                        """SELECT"""
                        icon = item.get("icon", "mdi:format-list-bulleted")
                        if value is None:
                            value = item.get("default", "")
                        options = item["options"]
                        if value not in options:
                            options.append(value)
                        old_state = self.get_state_wrapper(entity_id=entity)
                        if old_state and old_state != value:
                            self.set_state_wrapper(entity_id=entity, state=old_state, attributes={"friendly_name": item["friendly_name"], "options": options, "icon": icon})
                        self.set_state_wrapper(entity_id=entity, state=value, attributes={"friendly_name": item["friendly_name"], "options": options, "icon": icon})
                    elif item["type"] == "update":
                        """UPDATE"""
                        summary = self.releases.get("latest_body", "")
                        latest = self.releases.get("latest", "check HACS")
                        state = "off"
                        if item["installed_version"] != latest:
                            state = "on"
                        self.set_state_wrapper(
                            entity_id=entity,
                            state=state,
                            attributes={
                                "friendly_name": item["friendly_name"],
                                "title": item["title"],
                                "in_progress": in_progress,
                                "auto_update": True,
                                "installed_version": item["installed_version"],
                                "latest_version": latest,
                                "entity_picture": item["entity_picture"],
                                "release_url": item["release_url"],
                                "release_summary": summary,
                                "skipped_version": None,
                                "supported_features": 1,
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
        self.set_state_wrapper(entity_id=entity, state=state, attributes=attributes)
        if entity not in self.dashboard_index:
            self.dashboard_index.append(entity)

    async def async_update_save_restore_list(self):
        return await self.run_in_executor(self.update_save_restore_list)

    def update_save_restore_list(self):
        """
        Update list of current Predbat settings
        """
        global PREDBAT_SAVE_RESTORE
        self.save_restore_dir = None
        for root in CONFIG_ROOTS_HA:
            if os.path.exists(root):
                self.save_restore_dir = root + "/predbat_save"
                break
        if not self.save_restore_dir:
            return

        if not os.path.exists(self.save_restore_dir):
            os.mkdir(self.save_restore_dir)

        PREDBAT_SAVE_RESTORE = ["save current", "restore default"]
        for root, dirs, files in os.walk(self.save_restore_dir):
            for name in files:
                filepath = os.path.join(root, name)
                if filepath.endswith(".yaml") and not name.startswith("."):
                    PREDBAT_SAVE_RESTORE.append(name)
        item = self.config_index.get("saverestore", None)
        item["options"] = PREDBAT_SAVE_RESTORE
        self.expose_config("saverestore", None)

    async def async_restore_settings_yaml(self, filename):
        """
        Restore settings from YAML file
        """
        self.save_restore_dir = None
        for root in CONFIG_ROOTS_HA:
            if os.path.exists(root):
                self.save_restore_dir = root + "/predbat_save"
                break
        if not self.save_restore_dir:
            return

        if filename != "previous.yaml":
            await self.async_save_settings_yaml("previous.yaml")

        if not filename:
            self.log("Restore settings to default")
            for item in CONFIG_ITEMS:
                if (item["value"] != item.get("default", None)) and item.get("restore", True):
                    self.log("Restore setting: {} = {} (was {})".format(item["name"], item["default"], item["value"]))
                    await self.async_expose_config(item["name"], item["default"], event=True)
            await self.async_call_notify("Predbat settings restored from default")
        else:
            filepath = os.path.join(self.save_restore_dir, filename)
            if os.path.exists(filepath):
                self.log("Restore settings from {}".format(filepath))
                with open(filepath, "r") as file:
                    settings = yaml.safe_load(file)
                    for item in settings:
                        current = self.config_index.get(item["name"], None)
                        if current and (current["value"] != item["value"]) and current.get("restore", True):
                            self.log("Restore setting: {} = {} (was {})".format(item["name"], item["value"], current["value"]))
                            await self.async_expose_config(item["name"], item["value"], event=True)
                await self.async_call_notify("Predbat settings restored from {}".format(filename))
        await self.async_expose_config("saverestore", None)

    async def async_save_settings_yaml(self, filename=None):
        """
        Save current Predbat settings
        """
        if not self.save_restore_dir:
            return

        if not filename:
            filename = self.now_utc.strftime("%y_%m_%d_%H_%M_%S")
            filename += ".yaml"
        filepath = os.path.join(self.save_restore_dir, filename)
        with open(filepath, "w") as file:
            yaml.dump(CONFIG_ITEMS, file)
        self.log("Saved Predbat settings to {}".format(filepath))
        await self.async_call_notify("Predbat settings saved to {}".format(filename))

    def create_debug_yaml(self):
        """
        Write out a debug info yaml
        """
        time_now = self.now_utc.strftime("%H_%M_%S")
        basename = "/debug/predbat_debug_{}.yaml".format(time_now)
        filename = None
        for root in CONFIG_ROOTS:
            if os.path.exists(root):
                filename = root + basename
                break
        if filename:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
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
            debug["load_forecast"] = self.load_forecast
            debug["load_minutes_step"] = self.load_minutes_step
            debug["load_minutes_step10"] = self.load_minutes_step10
            debug["pv_forecast_minute_step"] = self.pv_forecast_minute_step
            debug["pv_forecast_minute10_step"] = self.pv_forecast_minute10_step
            debug["yesterday_load_step"] = self.yesterday_load_step
            debug["yesterday_pv_step"] = self.yesterday_pv_step

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
                entity = item.get("entity", None)
                enable = item.get("enable", None)

                if entity and enable == try_enable and self.user_config_item_enabled(item):
                    text += "  - entity: " + entity + "\n"

        for entity in self.dashboard_index:
            text += "  - entity: " + entity + "\n"

        # Find path
        basename = "/predbat_dashboard.yaml"
        filename = None
        for root in CONFIG_ROOTS:
            if os.path.exists(root):
                filename = root + basename
                break

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

    def load_previous_value_from_ha(self, entity):
        """
        Load HA value either from state or from history if there is any
        """
        ha_value = self.get_state_wrapper(entity)
        if ha_value is not None:
            return ha_value
        history = self.get_history_wrapper(entity_id=entity)
        if history:
            history = history[0]
            ha_value = history[-1]["state"]
        return ha_value

    async def trigger_watch_list(self, entity_id, attribute, old, new):
        """
        Trigger a watch event for an entity
        """
        for entity in self.watch_list:
            if entity_id == entity:
                await self.watch_event(entity, attribute, old, new, None)

    async def trigger_callback(self, service_data):
        """
        Trigger a callback for a service via HA Interface
        """
        for item in self.EVENT_LISTEN_LIST:
            if item["domain"] == service_data.get("domain", "") and item["service"] == service_data.get("service", ""):
                await item["callback"](item["service"], service_data, None)

    def define_service_list(self):
        self.SERVICE_REGISTER_LIST = [
            {"domain": "input_number", "service": "set_value"},
            {"domain": "input_number", "service": "increment"},
            {"domain": "input_number", "service": "decrement"},
            {"domain": "switch", "service": "turn_on"},
            {"domain": "switch", "service": "turn_off"},
            {"domain": "switch", "service": "toggle"},
            {"domain": "select", "service": "select_option"},
            {"domain": "select", "service": "select_first"},
            {"domain": "select", "service": "select_last"},
            {"domain": "select", "service": "select_next"},
            {"domain": "select", "service": "select_previous"},
        ]
        self.EVENT_LISTEN_LIST = [
            {"domain": "switch", "service": "turn_on", "callback": self.switch_event},
            {"domain": "switch", "service": "turn_off", "callback": self.switch_event},
            {"domain": "switch", "service": "toggle", "callback": self.switch_event},
            {"domain": "input_number", "service": "set_value", "callback": self.number_event},
            {"domain": "input_number", "service": "increment", "callback": self.number_event},
            {"domain": "input_number", "service": "decrement", "callback": self.number_event},
            {"domain": "select", "service": "select_option", "callback": self.select_event},
            {"domain": "select", "service": "select_first", "callback": self.select_event},
            {"domain": "select", "service": "select_last", "callback": self.select_event},
            {"domain": "select", "service": "select_next", "callback": self.select_event},
            {"domain": "select", "service": "select_previous", "callback": self.select_event},
            {"domain": "update", "service": "install", "callback": self.update_event},
            {"domain": "update", "service": "skip", "callback": self.update_event},
        ]

    def load_user_config(self, quiet=True, register=False):
        """
        Load config from HA
        """
        self.config_index = {}

        # New install, used to set default of expert mode
        new_install = True
        current_status = self.load_previous_value_from_ha(self.prefix + ".status")
        if current_status:
            new_install = False
        else:
            self.log("New install detected")

        # Build config index
        for item in CONFIG_ITEMS:
            name = item["name"]
            self.config_index[name] = item

            if name == "mode" and new_install:
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
                ha_value = self.get_state_wrapper(entity)
                if ha_value is not None:
                    self.set_state_wrapper(entity_id=entity, state=ha_value, attributes={"friendly_name": "[Disabled] " + item["friendly_name"]})
                continue

            # Get from current state?
            ha_value = self.load_previous_value_from_ha(entity)

            # Update drop down menu
            if name == "update":
                if not ha_value:
                    # Construct this version information as it's not set correctly already
                    ha_value = THIS_VERSION + " Loading..."
                else:
                    # Leave current value until it's set during version discovery later
                    continue

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
                if item.get("manual"):
                    self.manual_times(name, new_value=ha_value)
                else:
                    self.expose_config(item["name"], ha_value, quiet=quiet)

        # Update the last time we refreshed the config
        self.set_state_wrapper(entity_id=self.prefix + ".config_refresh", state=self.now_utc.strftime(TIME_FORMAT))

        # Register HA services
        if register:
            self.watch_list = self.get_arg("watch_list", [], indirect=False)
            self.log("Watch list {}".format(self.watch_list))

            if not self.ha_interface.websocket_active:
                # Registering HA events as Websocket is not active
                for item in self.SERVICE_REGISTER_LIST:
                    self.fire_event("service_registered", domain=item["domain"], service=item["service"])
                for item in self.EVENT_LISTEN_LIST:
                    self.listen_select_handle = self.listen_event(item["callback"], event="call_service", domain=item["domain"], service=item["service"])

                for entity in self.watch_list:
                    if entity and isinstance(entity, str) and ("." in entity):
                        self.listen_state(self.watch_event, entity_id=entity)

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
        elif isinstance(arg_value, dict):
            new_dict = {}
            for item_name in arg_value:
                item_value = arg_value[item_name]
                item_matched, item_value = self.resolve_arg_re(arg, item_value, state_keys)
                if not item_matched:
                    self.log("WARN: Regular argument {} expression {} failed to match - disabling this item".format(arg, item_value))
                    new_dict[item_name] = None
                else:
                    new_dict[item_name] = item_value
            arg_value = new_dict
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

        states = self.get_state_wrapper()
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

        app_dir = config_dir + "/apps"
        appdaemon_config = config_dir + "/appdaemon.yaml"
        if config_dir and os.path.exists(appdaemon_config):
            with open(appdaemon_config, "r") as han:
                data = None
                try:
                    data = yaml.safe_load(han)
                except yaml.YAMLError:
                    self.log("ERROR: Unable to read /config/appdaemon.yaml file correctly!")
                    passed = False

                if data and ("appdaemon" in data):
                    sub_data = data["appdaemon"]
                    if "app_dir" in sub_data:
                        app_dir = sub_data["app_dir"]
                    if app_dir not in app_dirs:
                        app_dirs.append(app_dir)
                    self.log("Sanity: Got app_dir {}".format(app_dir))
                elif data:
                    self.log("WARN: appdaemon section is missing from appdaemon.yaml")
                    passed = False
        else:
            self.log("WARN: unable to find {} skipping checks as maybe outside AppDaemon".format(appdaemon_config))
            return

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
                except yaml.YAMLError:
                    self.log("ERROR: Unable to read {} file correctly!".format(filename))
                    passed = False
                if data and "pred_bat" in data:
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
                            foundVersion = True
                            if version != THIS_VERSION:
                                self.log("WARN: The version in predbat.py is {} but this code is version {} - please re-start appdaemon".format(version, THIS_VERSION))
                                passed = False
                            else:
                                self.log("Sanity: Confirmed correct version {} is in predbat.py".format(version))
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
        self.pool = None
        self.log("Predbat: Startup {}".format(__name__))
        self.update_time(print=False)
        run_every = RUN_EVERY * 60
        now = self.now

        try:
            self.reset()
            self.ha_interface = HAInterface(self)
            self.sanity()
            self.ha_interface.update_states()
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

        if SIMULATE and SIMULATE_LENGTH:
            # run once to get data
            SIMULATE = False
            self.update_pred(scheduled=False)
            soc_best = self.predict_soc_best.copy()
            self.log("Best SOC array {}".format(soc_best))
            SIMULATE = True

            for offset in range(0, SIMULATE_LENGTH, 30):
                self.simulate_offset = offset + 30 - (self.minutes_now % 30)
                self.sim_soc_kw = soc_best[int(self.simulate_offset / 5) * 5]
                self.log(">>>>>>>>>> Simulated offset {} soc {} <<<<<<<<<<<<".format(self.simulate_offset, self.sim_soc_kw))
                self.update_pred(scheduled=True)
        else:
            # Run every N minutes aligned to the minute
            seconds_now = (now - self.midnight).seconds

            # Calculate next run time to exactly align with the run_every time
            seconds_offset = seconds_now % run_every
            seconds_next = seconds_now + (run_every - seconds_offset)
            next_time = self.midnight + timedelta(seconds=seconds_next)
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
                next_time_balance = self.midnight + timedelta(seconds=seconds_next_balance)
                self.run_every(self.run_time_loop_balance, next_time_balance, run_every_balance, random_start=0, random_end=0)

    async def terminate(self):
        """
        Called once each time the app terminates
        """
        self.log("Predbat terminating")
        self.stop_thread = True
        await asyncio.sleep(0)
        if hasattr(self, "pool"):
            if self.pool:
                self.pool.close()
                self.pool.join()
                self.pool = None
        self.log("Predbat terminated")

    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        self.check_entity_refresh()
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

    def check_entity_refresh(self):
        """
        Check if we need to refresh the config entities with HA
        """
        # Check if we need to refresh the config entities with HA
        config_refresh = self.get_state_wrapper(entity_id=self.prefix + ".config_refresh")
        config_refresh_stamp = None
        if config_refresh:
            try:
                config_refresh_stamp = datetime.strptime(config_refresh, TIME_FORMAT)
            except (ValueError, TypeError):
                config_refresh_stamp = None

        age = CONFIG_REFRESH_PERIOD
        if config_refresh_stamp:
            tdiff = self.now_utc - config_refresh_stamp
            age = tdiff.seconds / 60 + tdiff.days * 60 * 24
            if age >= CONFIG_REFRESH_PERIOD:
                self.log("Info: Refresh config entities due to their age of {} minutes".format(age))
                self.update_pending = True
        else:
            self.log("Info: Refresh config entities as config_refresh state is unknown")
            self.update_pending = True

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.prediction_started:
            config_changed = False
            self.prediction_started = True
            self.ha_interface.update_states()
            self.check_entity_refresh()

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
                raise


class HAInterface:
    """
    Direct interface to Home Assistant
    """

    def __init__(self, base):
        """
        Initialize the interface to Home Assistant.
        """
        self.ha_url = base.args.get("ha_url", "http://supervisor/core")
        self.ha_key = base.args.get("ha_key", os.environ.get("SUPERVISOR_TOKEN", None))
        self.websocket_active = False

        self.base = base
        self.log = base.log
        self.state_data = {}
        if not self.ha_key:
            self.log("WARN: ha_key or SUPERVISOR_TOKEN not found, you can set ha_url/ha_key in apps.yaml. Will use direct HA API")
        else:
            check = self.api_call("/api/")
            if not check:
                self.log("WARN: Unable to connect directly to Home Assistant at {}, please check your configuration of ha_url/ha_key".format(self.ha_url))
                self.ha_key = None
            else:
                self.log("Info: Connected to Home Assistant at {}".format(self.ha_url))
                self.base.create_task(self.socketLoop())
                self.websocket_active = True
                self.log("Info: Web Socket task started")

    async def socketLoop(self):
        """
        Web socket loop for HA interface
        """
        while True:
            if self.base.stop_thread:
                self.log("Info: Web socket stopping")
                break

            url = "{}/api/websocket".format(self.ha_url)
            self.log("Info: Start socket for url {}".format(url))
            async with ClientSession() as session:
                try:
                    async with session.ws_connect(url) as websocket:
                        await websocket.send_json({"type": "auth", "access_token": self.ha_key})
                        sid = 1

                        # Subscribe to all state changes
                        await websocket.send_json({"id": sid, "type": "subscribe_events", "event_type": "state_changed"})
                        sid += 1

                        # Listen for services
                        await websocket.send_json({"id": sid, "type": "subscribe_events", "event_type": "call_service"})
                        sid += 1

                        # Fire events to say we have registered services
                        for item in self.base.SERVICE_REGISTER_LIST:
                            await websocket.send_json(
                                {"id": sid, "type": "fire_event", "event_type": "service_registered", "event_data": {"service": item["service"], "domain": item["domain"]}}
                            )
                            sid += 1

                        self.log("Info: Web Socket active")

                        async for message in websocket:
                            if self.base.stop_thread:
                                self.log("Info: Web socket stopping")
                                break

                            if message.type == WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                    if data:
                                        message_type = data.get("type", "")
                                        if message_type == "event":
                                            event_info = data.get("event", {})
                                            event_type = event_info.get("event_type", "")
                                            if event_type == "state_changed":
                                                event_data = event_info.get("data", {})
                                                old_state = event_data.get("old_state", {})
                                                new_state = event_data.get("new_state", {})
                                                if new_state:
                                                    self.update_state_item(new_state)
                                                    # Only trigger on value change or you get too many updates
                                                    if new_state.get("state", None) != old_state.get("state", None):
                                                        await self.base.trigger_watch_list(
                                                            new_state["entity_id"], event_data.get("attribute", None), event_data.get("old_state", None), new_state
                                                        )
                                            elif event_type == "call_service":
                                                service_data = event_info.get("data", {})
                                                await self.base.trigger_callback(service_data)
                                            else:
                                                self.log("Info: Web Socket unknown message {}".format(data))
                                        elif message_type == "result":
                                            success = data.get("success", False)
                                            if not success:
                                                self.log("Warn: Web Socket result failed {}".format(data))
                                        elif message_type == "auth_required":
                                            pass
                                        elif message_type == "auth_ok":
                                            pass
                                        elif message_type == "auth_invalid":
                                            self.log("Warn: Web Socket auth failed, check your ha_key setting")
                                            self.websocket_active = False
                                            raise Exception("Web Socket auth failed")
                                        else:
                                            self.log("Info: Web Socket unknown message {}".format(data))
                                except Exception as e:
                                    self.log("Warn Web Socket exception in update loop: {}".format(e))
                                    break

                            elif message.type == WSMsgType.CLOSED:
                                break
                            elif message.type == WSMsgType.ERROR:
                                break

                except Exception as e:
                    self.log("Warn: Web Socket exception in startup: {}".format(e))
                    continue

            if not self.base.stop_thread:
                self.log("Warn: Web Socket closed, will try to reconnect in 5 seconds")
                await asyncio.sleep(5)

    def get_state(self, entity_id=None, default=None, attribute=None, refresh=False):
        """
        Get state from cached HA data (or from appDaemon if used)
        """
        if not self.ha_key:
            return self.base.get_state(entity_id=entity_id, default=default, attribute=attribute)

        if not entity_id:
            return self.state_data
        elif entity_id.lower() in self.state_data:
            if refresh:
                self.update_state(entity_id)
            state_info = self.state_data[entity_id.lower()]
            if attribute:
                if attribute in state_info["attributes"]:
                    return state_info["attributes"][attribute]
                else:
                    return default
            else:
                return state_info["state"]
        else:
            return default

    def update_state(self, entity_id):
        """
        Update state for entity_id
        """
        if not self.ha_key:
            return
        item = self.api_call("/api/states/{}".format(entity_id))
        if item:
            self.update_state_item(item)

    def update_state_item(self, item):
        """
        Update state table for item
        """
        entity_id = item["entity_id"]
        attributes = item["attributes"]
        last_changed = item["last_changed"]
        state = item["state"]
        self.state_data[entity_id.lower()] = {"state": state, "attributes": attributes, "last_changed": last_changed}

    def update_states(self):
        """
        Update the state data from Home Assistant.
        """
        if not self.ha_key:
            return
        res = self.api_call("/api/states")
        if res:
            for item in res:
                self.update_state_item(item)
        else:
            self.log("WARN: Failed to update state data from HA")

    def get_history(self, sensor, now, days=30):
        """
        Get the history for a sensor from Home Assistant.

        :param sensor: The sensor to get the history for.
        :return: The history for the sensor.
        """
        if not self.ha_key:
            return self.base.get_history_ad(sensor, days=days)

        start = now - timedelta(days=days)
        end = now
        res = self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        return res

    def set_state(self, entity_id, state, attributes={}):
        """
        Set the state of an entity in Home Assistant.
        """
        if not self.ha_key:
            if attributes:
                return self.base.set_state(entity_id, state=state)
            else:
                return self.base.set_state(entity_id, state=state, attributes=attributes)

        data = {"state": state}
        if attributes:
            data["attributes"] = attributes
        self.api_call("/api/states/{}".format(entity_id), data, post=True)
        self.update_state(entity_id)

    def call_service(self, service, **kwargs):
        """
        Call a service in Home Assistant.
        """
        if not self.ha_key:
            return self.base.call_service(service, **kwargs)

        data = {}
        for key in kwargs:
            data[key] = kwargs[key]
        self.api_call("/api/services/{}".format(service), data, post=True)

    def api_call(self, endpoint, data_in=None, post=False):
        """
        Make an API call to Home Assistant.

        :param endpoint: The API endpoint to call.
        :param data_in: The data to send in the body of the request.
        :param post: True if this is a POST request, False for GET.
        :return: The response from the API.
        """
        url = self.ha_url + endpoint
        headers = {
            "Authorization": "Bearer " + self.ha_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if post:
            if data_in:
                response = requests.post(url, headers=headers, json=data_in, timeout=TIMEOUT)
            else:
                response = requests.post(url, headers=headers, timeout=TIMEOUT)
        else:
            if data_in:
                response = requests.get(url, headers=headers, params=data_in, timeout=TIMEOUT)
            else:
                response = requests.get(url, headers=headers, timeout=TIMEOUT)
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Failed to decode response {} from {}".format(response, url))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: Timeout from {}".format(url))
            data = None
        return data
