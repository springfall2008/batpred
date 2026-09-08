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

# DEYE executes one control order at a time per device. A second command sent while one is
# still running is rejected with this code — it means "retry shortly", not "rejected", so
# it is logged as back-pressure rather than a failure and the caller re-applies next cycle.
# Observed live as: {"code": "2104004", "msg": "command concurrent running", "success": false}
DEYE_BUSY_CODES = ("2104004",)
DEYE_BUSY_MARKERS = ("command concurrent running", "concurrent running", "order is running")

# Storage module name for every persisted DEYE cache file.
DEYE_STORAGE_MODULE = "deye"

# Refresh cadence per class of state, in minutes. ComponentBase ticks run() every 60
# seconds, so DEYE_TTL_LIVE of 1 means "every tick". These are the maximum ages a cached
# tier may reach before it is re-polled; the clocks are seeded from storage.age() at
# startup so the cadence survives a process restart rather than restarting with it.
DEYE_TTL_STATIC = 8 * 60  # station/device discovery, measure points — changes when hardware does
DEYE_TTL_CONFIG = 15  # config/battery — installer settings, effectively static
DEYE_TTL_LIVE = 1  # device/latest — telemetry and energy counters

# Restore bound in minutes for applied_payload: this is a change-detection cache with no
# API read-back, so restoring it
# asserts the inverter still holds what Predbat last wrote. If it was changed externally
# while Predbat was down that assertion is false, the next write is wrongly SKIPPED and the
# battery silently diverges from the plan. A redundant write is cheap; a skipped one is
# not. Bounded so quick restarts keep the benefit and any longer outage forces a rewrite.
DEYE_RESTORE_MAX_CONTROL = 15

# Cache file names under DEYE_STORAGE_MODULE. One file per tier so storage.age() gives each
# an independent clock; a single blob would need hand-rolled per-section timestamps, and
# rewriting it every minute for live data would reset the static tier's age to zero.
DEYE_CACHE_STATIC = "static"  # station_ids, device_list
DEYE_CACHE_CONFIG = "config"  # device_battery_config
DEYE_CACHE_RATINGS = "ratings"  # device_capacity, device_pack_voltage, device_rated_power
DEYE_CACHE_CONTROL = "control"  # applied_payload, pending_orders, order_poll_count
DEYE_CACHE_TOU = "tou"  # device_tou_config, the programme the inverter already holds

# Telemetry and the energy counters are deliberately NOT cached. The live tier polls every
# minute, so a cache would be written 1440 times a day to save at most one tick's gap at
# startup — a poor trade against that much file or network IO. Home Assistant already
# retains the last published value of every entity, and publish_data() only writes a
# sensor when it has a value, so a failed poll leaves the previous reading in place rather
# than overwriting it. Ratings are written by the same poll but ARE cached, because they
# are static and are what lets automatic_config() map soc_max/battery_rate_max/
# inverter_limit at startup; they are saved only when they actually change.

# DEYE does NOT answer an expired/invalid bearer token with HTTP 401 — it returns
# HTTP 200 carrying a body-level failure, e.g. {"success": false, "msg": "auth invalid
# token"}. Status-code-only handling therefore never triggers a refresh and the
# component stays broken until a restart, so the transport matches these lower-cased
# substrings against the body's msg/code to decide a token refresh is needed. Keep the
# markers narrow enough that a genuine non-auth failure is never retried as one.
DEYE_AUTH_ERROR_MARKERS = (
    "auth invalid token",
    "invalid token",
    "token invalid",
    "invalid access token",
    "token expired",
    "token is expired",
    "expired token",
    "token has expired",
    "auth failed",
    "unauthorized",
    "unauthorised",
)

# Maximum characters of a request/response body written to the log when API debug
# tracing is on — device/latest bodies run to tens of KB and would swamp the log.
DEYE_DEBUG_MAX_CHARS = 20000

# Body keys redacted from debug traces: these carry credentials or bearer tokens
# and the logs are routinely pasted into issue reports.
DEYE_DEBUG_REDACT_KEYS = ("accessToken", "refreshToken", "appSecret", "password", "token", "tokenHash")

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
    # Read-only discovery endpoints, confirmed in the official sample code
    # (device/obtain_device_measure_points.py, station/obtain_station_latest.py). Polled
    # once on the first cycle only: they are the remaining candidates for the battery
    # capability data that config/battery refuses to serve on some models.
    "device_measure_points": "/device/measurePoints",
    "station_latest": "/station/latest",
}

# Battery-size derivation from device/latest, used when config/battery answers
# "config point not supported" (code 2106001) and there is otherwise no soc_max source.
DEYE_CAPACITY_KEYS = {
    "rated_capacity_ah": "BatteryRatedCapacity",
    "bms_charge_voltage": "BMSChargeVoltage",
    "battery_voltage": "BatteryVoltage",
}

# The inverter's rated AC output in W, straight from device/latest (8000.00 live). This is
# exactly what Predbat's inverter_limit wants, so it needs no conversion.
DEYE_RATED_POWER_KEY = "RatedPower"

# LiFePO4 per-cell voltages used to turn the BMS charge target into a nominal pack
# voltage, so an Ah rating becomes kWh. A 57.6 V charge target is 16 cells at 3.6 V,
# giving a 51.2 V nominal pack: 1200 Ah x 51.2 V = 61.4 kWh.
LIFEPO4_CHARGE_VOLTS_PER_CELL = 3.6
LIFEPO4_NOMINAL_VOLTS_PER_CELL = 3.2
# The RESTING pack voltage must never be used to derive a nominal pack voltage: it climbs
# with SOC (~53.8 V at 94% but ~58 V at 100% on the same 16S pack), so inferring a cell
# count from it overstates capacity by a whole cell near full charge. The nominal comes
# from the BMS charge target, or from deye_battery_nominal_voltage. There is deliberately
# no fallback default -- see nominal_pack_voltage() in deye.py for why a guessed soc_max
# is worse than none.

# device/latest batches serials on this body key (max 10 per call).  # CONFIRMED
DEYE_LATEST_BODY_KEY = "deviceList"

DEYE_WORKMODE = {
    "selling_first": "SELLING_FIRST",
    "zero_export_load": "ZERO_EXPORT_TO_LOAD",
    "zero_export_ct": "ZERO_EXPORT_TO_CT",
}

# device/latest dataList[].key spellings — CONFIRMED against a live 3-phase hybrid
# (productId 0_5411_1) response. DEYE uses PascalCase display names, not the camelCase
# these were originally guessed as, and two carry literal spaces/hyphens
# ("Temperature- Battery"). A wrong key here is silent: the lookup misses and the
# metric publishes as 0.0, so DEYE_TELEMETRY_REQUIRED below makes a miss visible.
DEYE_TELEMETRY_KEYS = {
    "soc": "SOC",
    "battery_power": "BatteryPower",
    "grid_power": "TotalGridPower",
    "pv_power": "TotalSolarPower",
    "load_power": "TotalConsumptionPower",
    "temperature": "Temperature- Battery",
}

# Metrics whose sign must be flipped to reach Predbat's convention. DEYE reports grid
# power positive when IMPORTING (confirmed live: TotalGridPower +25 W with
# DailyGridFeedIn 0.00 kWh), whereas Predbat wants negative for import / positive for
# export. Battery power needs no flip — DEYE already reports negative while charging,
# matching Predbat's positive-for-discharge convention.
DEYE_TELEMETRY_NEGATE = ("grid_power",)

# Cumulative energy counters Predbat needs for its history-based load/rate learning, from
# the same device/latest dataList.
#
# The mapping is confirmed by DEYE's own daily figures balancing exactly:
#   DailyConsumption 12.80 = DailyActiveProduction 4.50 + DailyEnergyPurchased 7.00 - DailyGridFeedIn 0.00
#                            + (DailyDischargingEnergy 4.10 - DailyChargingEnergy 2.80)
# which only holds if ActiveProduction is PV generation alone, excluding battery discharge.
#
# These are the DAILY registers, not the lifetime "Total*" ones. The lifetime accumulators
# are firmware-derived and drift: on a live capture TotalConsumption read 14332.40 kWh where
# the other lifetime counters implied 15475.00 (buy 13579.20 + PV 2208.30 - sell 11.80 +
# discharge 5255.90 - charge 5556.60). That 7.4% shortfall fed straight into Predbat's
# learned load and under-sized every charge window. The Daily registers balance exactly on
# the same payload, so the whole set reads from them.
#
# A daily counter is safe despite resetting at midnight: minute_data() smooths the
# near-midnight drop (utils.py, the near_midnight branch) and clean_incrementing_reverse()
# re-bases on any reset to <= 0, so the nightly return to 0.00 is absorbed rather than read
# as a negative delta. Predbat only ever needs today-so-far plus history from these args.
DEYE_ENERGY_KEYS = {
    "import_today": "DailyEnergyPurchased",
    "export_today": "DailyGridFeedIn",
    "pv_today": "DailyActiveProduction",
    "load_today": "DailyConsumption",
}

# Metrics that must be present in every device/latest response. A key missing here means
# the model uses different spellings and every dependent Predbat calculation would run on
# a silent 0.0, so absence is logged rather than swallowed.
DEYE_TELEMETRY_REQUIRED = ("soc", "battery_power", "pv_power", "load_power")

# TimeUseSettingItem per-slot fields — CONFIRMED from the official Swagger definition
# (GET https://eu1-developer.deyecloud.com/v2/api-docs, the spec behind
# developer.deyecloud.com/api; definitions.TimeUseSettingItem).
#
# The SPEC is the authority here, NOT the sample code. Every sample in
# DeyeCloudDevelopers/deye-openapi-client-sample-code omits enableSell, which reads as if
# the field does not exist — but the spec lists it, and it is the same forced-export
# register bit Sunsynk exposes as sellTime{n}En, where a live write test proved an export
# slot does not arm without it. Each sample is a whole-day single behaviour, so none of
# them ever needed one slot to sell and another not to.
#
# Every field here goes on EVERY slot. Sunsynk — the same hardware behind a different
# cloud — validates the per-slot field set as a whole and silently discards the flags when
# it is incomplete: with sellTime{n}En absent, time{n}on vanished on six consecutive writes
# across every encoding tried, while the rest of each write persisted and the API reported
# success throughout.
#
# The one spec field deliberately NOT written is "voltage" ("If enabled battery voltage
# mode, this field should be set"). Predbat drives SOC targets, not voltage targets, so it
# has nothing to put there and a guessed value would be acted on. Sunsynk leaves its
# equivalent, sellTime{n}Volt, untouched for the same reason.
TOU_FIELD = {
    "time": "time",
    "power": "power",
    "soc": "soc",
    "grid_charge": "enableGridCharge",
    "generate": "enableGeneration",
    "sell": "enableSell",
}

# Days the TOU programme runs on. Sent with every control write, all seven of them.
#
# CONFIRMED from the official Swagger definition, which types it as an enum of the seven
# upper-case day names and documents it as "If action is on, fill this field with the days
# of the week you want to switch on"; all four of clientcode/strategy/dynamic_control_*.py
# send this exact list too.
# Predbat previously sent touAction without touDays, which left the active days at whatever
# the inverter already held — and a programme whose days are empty, or which omits the day
# the plan is for, is stored and never applied.
#
# All seven is the only correct value here: Predbat re-derives a 24h programme every cycle
# and has no notion of a day its plan should be dormant. Sunsynk sets its seven
# mondayOn..sundayOn flags true for the same reason (SUNSYNK_DAY_FIELDS).
DEYE_TOU_DAYS = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]

# config/battery response field names — CONFIRMED live:
#   {"maxChargeCurrent": 185, "maxDischargeCurrent": 185, "battLowCapacity": 14,
#    "battShutDownCapacity": 9, "battCapacity": 1200}
# battCapacity is Ah, NOT kWh: it reports 1200 for the same pack device/latest reports as
# BatteryRatedCapacity 1200 Ah. Publishing it directly as soc_max would tell Predbat the
# battery is 1200 kWh, so it must be scaled by the nominal pack voltage like the derived
# value. battLowCapacity is a percentage.
CONFIG_BATTERY_KEYS = {
    "capacity": "battCapacity",
    "reserve_min": "battLowCapacity",
    "max_charge_current": "maxChargeCurrent",
    "max_discharge_current": "maxDischargeCurrent",
}
