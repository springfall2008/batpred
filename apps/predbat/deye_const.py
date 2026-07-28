# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# DEYE Cloud API constants
# -----------------------------------------------------------------------------


"""DEYE Cloud OpenAPI constants.

Values marked ``# VERIFY@SPIKE`` are best-known defaults from the DeyeCloud
developer docs and the hass-deyecloud reference; confirm against a live
inverter (see plan Task 0) and correct here. All component logic imports from
this module so a value change here needs no downstream edits.
"""

DEYE_BASE_URLS = {
    "eu": "https://eu1-developer.deyecloud.com/v1.0",
    "am": "https://us1-developer.deyecloud.com/v1.0",
    "india": "https://india-developer.deyecloud.com/v1.0",
}

DEYE_TIMEOUT = 30
DEYE_RETRIES = 3
TOU_SLOT_COUNT = 6
FREEZE_EXPORT_SOC = 99

# Distinct, ascending self-use start times used to pad a schedule out to
# TOU_SLOT_COUNT slots. DEYE's 6 TOU slots are sequential intervals ("from this
# start until the next slot's start"), so every slot must have a UNIQUE start
# time — duplicate times create zero-length/ambiguous intervals the API may
# reject. Seven options guarantee at least TOU_SLOT_COUNT distinct times remain
# after removing any that collide with the schedule's own window boundaries.
TOU_FILLER_TIMES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00", "23:00"]
# Number of run() cycles a control order may stay unconfirmed before the applied-payload
# cache is invalidated and the next apply is forced to re-write.
DEYE_ORDER_MAX_POLLS = 3

# Endpoint paths CONFIRMED against DeyeCloudDevelopers/deye-openapi-client-sample-code.
DEYE_ENDPOINTS = {
    "token": "/account/token",
    "station_list": "/station/list",
    "station_device": "/station/device",
    "device_latest": "/device/latest",
    "config_battery": "/config/battery",
    "config_tou": "/config/tou",
    "tou_update": "/order/sys/tou/update",
    "dynamic_control": "/strategy/dynamicControl",  # camelCase — confirmed in sample code
    "order_result": "/order/",  # GET {base}/order/{orderId} — confirmed
}

# device/latest batches serials on this body key (max 10 per call).  # CONFIRMED
DEYE_LATEST_BODY_KEY = "deviceList"

DEYE_WORKMODE = {
    "selling_first": "SELLING_FIRST",
    "zero_export_load": "ZERO_EXPORT_TO_LOAD",
    "zero_export_ct": "ZERO_EXPORT_TO_CT",
}

# device/latest dataList[].key spellings — CONFIRMED 2026-07-28 against a live
# SUN-8K three-phase hybrid (88 dataList entries). The earlier guessed spellings
# (batterySOC/batteryPower/gridPower/pvPower/loadPower/batteryTemperature) matched
# NOTHING, and because _as_float() coerces an absent key to 0.0 that published a
# full set of zeros rather than failing. Do not "tidy" these names: the casing is
# DEYE's, and the temperature key really does contain a space after the hyphen.
DEYE_TELEMETRY_KEYS = {
    "soc": "SOC",
    "battery_power": "BatteryPower",
    "grid_power": "TotalGridPower",
    "pv_power": "TotalSolarPower",
    "load_power": "TotalConsumptionPower",
    "temperature": "Temperature- Battery",
}

# Cumulative daily energy counters (kWh), same device/latest dataList.  # CONFIRMED
# 2026-07-28. Predbat needs these for load_today/import_today/export_today/pv_today;
# without load_today fetch_sensor_data() raises ValueError and the whole run aborts.
DEYE_ENERGY_KEYS = {
    "load_today": "DailyConsumption",
    "import_today": "DailyEnergyPurchased",
    "export_today": "DailyGridFeedIn",
    "pv_today": "DailyActiveProduction",
}

# Rated battery capacity is reported in AMP-HOURS, not kWh — config/battery
# battCapacity=1200 alongside dataList BatteryRatedCapacity='1200' unit='Ah'.
# Multiply by the nominal pack voltage to get kWh. DEYE low-voltage hybrids are
# 48 V nominal (16S LiFePO4 ~51.2 V); the observed pack floats ~58 V at 99% SOC,
# so the resting voltage must NOT be used for this conversion.
DEYE_NOMINAL_BATTERY_VOLTAGE = 51.2

# TimeUseSettingItem per-slot fields — CONFIRMED from official sample
# clientcode/commission/sys_tou_update.py and the strategy/* samples.
TOU_FIELD = {
    "time": "time",
    "power": "power",
    "soc": "soc",
    "grid_charge": "enableGridCharge",
    "generate": "enableGeneration",
}

# config/battery response field names — best-known defaults, not yet seen against a live
# response.  # VERIFY@SPIKE (field names AND units — esp. whether battCapacity is kWh or Ah)
CONFIG_BATTERY_KEYS = {
    "capacity": "battCapacity",
    "reserve_min": "battLowCapacity",
    "max_charge_current": "maxChargeCurrent",
    "max_discharge_current": "maxDischargeCurrent",
}
