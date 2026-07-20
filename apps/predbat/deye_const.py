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

# device/latest dataList[].key spellings — the one item to confirm from a live
# response (request body/shape is confirmed; exact value keys are not in the
# sample).  # VERIFY@SPIKE (values only)
DEYE_TELEMETRY_KEYS = {
    "soc": "batterySOC",
    "battery_power": "batteryPower",
    "grid_power": "gridPower",
    "pv_power": "pvPower",
    "load_power": "loadPower",
    "temperature": "batteryTemperature",
}

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
