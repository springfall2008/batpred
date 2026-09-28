# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sungrow iSolarCloud OpenAPI constants
# -----------------------------------------------------------------------------

"""Constants and pure helpers for the Sungrow iSolarCloud OpenAPI component.

Kept separate from sungrow.py so the wire format, the published control-parameter table,
the measuring-point maps and the scaling arithmetic can be tested without constructing a
component.

Two Sungrow APIs exist and they are NOT the same thing. This module describes the OFFICIAL
OpenAPI (developer-api.isolarcloud.com, the regional gateway hosts below, paths under
``/openapi/platform/``). The app/web API that GoSungrow and the user-account half of
pysolarcloud speak is a different, unofficial API with different paths; nothing here
applies to it.
"""

# -----------------------------------------------------------------------------
# Gateways
# -----------------------------------------------------------------------------

# Regional gateways, from the developer portal's "Getting Started" page. The account is
# tied to one region, so pointing at the wrong one authenticates and then finds no plants,
# which looks exactly like an empty account - hence sungrow_gateway is a first-class config
# key rather than something inferred.
SUNGROW_GATEWAYS = {
    "china": "https://gateway.isolarcloud.com",
    "international": "https://gateway.isolarcloud.com.hk",
    "europe": "https://gateway.isolarcloud.eu",
    "australia": "https://augateway.isolarcloud.com",
    "india": "https://gateway.isolarcloud.in",
}
SUNGROW_DEFAULT_GATEWAY = SUNGROW_GATEWAYS["europe"]

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

# Every OpenAPI call is a POST carrying a JSON body; there are no GET endpoints and no
# path or query parameters. The body always carries appkey, and the headers always carry
# x-access-key and the OAuth bearer token.
SUNGROW_ENDPOINTS = {
    "plant_list": "/openapi/platform/queryPowerStationList",
    "plant_detail": "/openapi/platform/getPowerStationDetail",
    "device_list": "/openapi/platform/getDeviceListByPsId",
    "plant_realtime": "/openapi/platform/getPowerStationRealTimeData",
    "device_realtime": "/openapi/platform/getDeviceRealTimeData",
    # Grid Control. Writes are an ASYNCHRONOUS three-step flow, not a single PUT:
    # paramSettingCheck asks whether this device supports the operation at all,
    # paramSetting queues a task and hands back a task_id, and getParamSettingTask is
    # polled until the task leaves the running state. See SUNGROW_TASK_* below.
    "param_check": "/openapi/platform/paramSettingCheck",
    "param_setting": "/openapi/platform/paramSetting",
    "param_task": "/openapi/platform/getParamSettingTask",
}

SUNGROW_LANG = "_en_US"

SUNGROW_TIMEOUT = 30
SUNGROW_RETRIES = 3

# -----------------------------------------------------------------------------
# Response envelope
# -----------------------------------------------------------------------------

# result_code is a STRING in every observed response, not an integer, and "1" is success.
# Comparing it to 1 silently treats every success as a failure, so the component compares
# against this constant and coerces to str first.
SUNGROW_RESULT_OK = "1"

# Codes worth branching on. The portal's full error appendix could not be read (its document
# viewer is a JavaScript application that would not render for us), so this is deliberately a
# SHORT list of codes observed in the wild by the pysolarcloud library rather than a
# half-invented table. Anything not listed is logged with the server's own result_msg, which
# is more useful than a wrong description.
#
# Auth codes mean the credentials or the token are the problem, not the request. They are
# called out separately because "appkey invalid" and "token expired" otherwise read as an
# ordinary API failure and send people looking in the wrong place.
SUNGROW_AUTH_ERROR_CODES = ("E00003", "E900", "E912", "E914", "E919")
# Rate limiting is a pacing signal, not a fault. Logging it at Warn would read as a genuine
# malfunction on an account that is simply being polled hard.
SUNGROW_RATE_LIMIT_CODES = ("E998", "E999")

# getDeviceRealTimeData caps point_id_list at 100 and answers result_code 010 when the list is
# longer. The maps here are far smaller, so nothing is chunked - but a future addition that
# crossed the cap would produce a rejection that looks like an unsupported endpoint rather
# than an oversized request, so the limit is named and checked.
SUNGROW_MAX_POINTS_PER_REQUEST = 100

# paramSettingCheck / paramSetting report a top-level check_result as well as the envelope
# result_code, and a per-device verdict inside dev_result_list.
SUNGROW_CHECK_OK = "1"
SUNGROW_CHECK_SUPPORTED = "1"
SUNGROW_CHECK_UNSUPPORTED = "0"
SUNGROW_DEV_RESULT_OK = "1"

# getParamSettingTask command_status. 2 means the task is still queued or in flight and the
# caller must poll again; 8 is the terminal success state that carries param_list. Anything
# else is a terminal failure. These are integers in the response, unlike result_code.
SUNGROW_TASK_RUNNING = 2
SUNGROW_TASK_DONE = 8

# How the component paces its polling of a dispatched task. The first poll is deliberately
# delayed: a task queued microseconds ago is always still running, so polling immediately
# just spends a request to be told so.
SUNGROW_TASK_FIRST_POLL_SECONDS = 2
SUNGROW_TASK_POLL_SECONDS = 5
# Ceiling on how long one dispatch is chased before it is given up on. Longer than
# expire_second below so a task that the cloud itself expires is seen to expire rather than
# being abandoned first, and short enough that a wedged task cannot stall the component's
# tick past its run_timeout.
SUNGROW_TASK_TIMEOUT_SECONDS = 90
# Passed to paramSetting as expire_second: how long the cloud should keep trying to deliver
# the task to the inverter before dropping it. A stale setpoint is worse than no setpoint,
# so this is deliberately shorter than Predbat's five-minute plan cadence - a task that has
# not landed within two minutes describes a plan Predbat is about to replace anyway.
SUNGROW_TASK_EXPIRE_SECONDS = 120

# paramSetting set_type: 0 writes, 2 reads back. The same endpoint serves both, and the
# only thing distinguishing a read from a write is this field plus an empty set_value.
SUNGROW_SET_TYPE_WRITE = 0
SUNGROW_SET_TYPE_READ = 2

# -----------------------------------------------------------------------------
# Device types
# -----------------------------------------------------------------------------

# getDeviceListByPsId device_type values. Only the ones this component acts on are named.
SUNGROW_DEVICE_INVERTER = 1
SUNGROW_DEVICE_METER = 7
SUNGROW_DEVICE_ESS = 14
SUNGROW_DEVICE_ESS_2 = 37
SUNGROW_DEVICE_BATTERY = 43

# The device types that carry a battery Predbat can drive. A residential SH hybrid appears
# as an energy-storage inverter; 37 is the second energy-storage type the portal defines
# and is included so a plant that reports it is not silently skipped.
SUNGROW_ESS_DEVICE_TYPES = (SUNGROW_DEVICE_ESS, SUNGROW_DEVICE_ESS_2)

# -----------------------------------------------------------------------------
# Control parameters (Appendix 10, Control Parameter Definitions)
# -----------------------------------------------------------------------------

SUNGROW_PARAM_SOC_UPPER = "10001"
SUNGROW_PARAM_SOC_LOWER = "10002"
SUNGROW_PARAM_EMS_MODE = "10003"
SUNGROW_PARAM_COMMAND = "10004"
SUNGROW_PARAM_POWER = "10005"
SUNGROW_PARAM_BOOT = "10011"
SUNGROW_PARAM_FEED_IN_LIMIT = "10012"
SUNGROW_PARAM_FEED_IN_LIMIT_VALUE = "10013"
SUNGROW_PARAM_FEED_IN_LIMIT_RATIO = "10014"
SUNGROW_PARAM_HEARTBEAT = "10017"
SUNGROW_PARAM_BATTERY_FIRST = "10024"
SUNGROW_PARAM_FORCED_CHARGING = "10065"
SUNGROW_PARAM_FORCED_VALID_TIME = "10066"
SUNGROW_PARAM_FORCED_START_HOUR_1 = "10067"
SUNGROW_PARAM_FORCED_START_MIN_1 = "10068"
SUNGROW_PARAM_FORCED_END_HOUR_1 = "10069"
SUNGROW_PARAM_FORCED_END_MIN_1 = "10070"
SUNGROW_PARAM_FORCED_TARGET_SOC_1 = "10071"
SUNGROW_PARAM_FORCED_START_HOUR_2 = "10072"
SUNGROW_PARAM_FORCED_START_MIN_2 = "10073"
SUNGROW_PARAM_FORCED_END_HOUR_2 = "10074"
SUNGROW_PARAM_FORCED_END_MIN_2 = "10075"
SUNGROW_PARAM_FORCED_TARGET_SOC_2 = "10076"
SUNGROW_PARAM_MAX_CHARGE_POWER = "10091"
SUNGROW_PARAM_MAX_DISCHARGE_POWER = "10092"

# The EMS-side equivalents for C&I hardware (Logger1000, iHomeManager). NOT written by this
# component - residential SH hybrids use the 100xx codes above - but named so a log line
# about an unexpected param_code is readable rather than a bare number.
SUNGROW_PARAM_EMS_COMMAND = "10082"
SUNGROW_PARAM_EMS_POWER = "10083"
SUNGROW_PARAM_EMS_POWER_LIMIT = "10084"
SUNGROW_PARAM_EMS_HEARTBEAT = "10085"
SUNGROW_PARAM_EMS_MODE_CI = "10086"

SUNGROW_PARAM_NAMES = {
    SUNGROW_PARAM_SOC_UPPER: "soc_upper_limit",
    SUNGROW_PARAM_SOC_LOWER: "soc_lower_limit",
    SUNGROW_PARAM_EMS_MODE: "energy_management_mode",
    SUNGROW_PARAM_COMMAND: "charge_discharge_command",
    SUNGROW_PARAM_POWER: "charge_discharge_power",
    SUNGROW_PARAM_BOOT: "boot_shutdown",
    SUNGROW_PARAM_FEED_IN_LIMIT: "feed_in_limitation",
    SUNGROW_PARAM_FEED_IN_LIMIT_VALUE: "feed_in_limitation_value",
    SUNGROW_PARAM_FEED_IN_LIMIT_RATIO: "feed_in_limitation_ratio",
    SUNGROW_PARAM_HEARTBEAT: "external_ems_heartbeat",
    SUNGROW_PARAM_BATTERY_FIRST: "battery_first",
    SUNGROW_PARAM_FORCED_CHARGING: "forced_charging",
    SUNGROW_PARAM_FORCED_VALID_TIME: "forced_charging_valid_time",
    SUNGROW_PARAM_FORCED_START_HOUR_1: "forced_charging_start_time_1_hour",
    SUNGROW_PARAM_FORCED_START_MIN_1: "forced_charging_start_time_1_minute",
    SUNGROW_PARAM_FORCED_END_HOUR_1: "forced_charging_end_time_1_hour",
    SUNGROW_PARAM_FORCED_END_MIN_1: "forced_charging_end_time_1_minute",
    SUNGROW_PARAM_FORCED_TARGET_SOC_1: "forced_charging_target_soc_1",
    SUNGROW_PARAM_FORCED_START_HOUR_2: "forced_charging_start_time_2_hour",
    SUNGROW_PARAM_FORCED_START_MIN_2: "forced_charging_start_time_2_minute",
    SUNGROW_PARAM_FORCED_END_HOUR_2: "forced_charging_end_time_2_hour",
    SUNGROW_PARAM_FORCED_END_MIN_2: "forced_charging_end_time_2_minute",
    SUNGROW_PARAM_FORCED_TARGET_SOC_2: "forced_charging_target_soc_2",
    SUNGROW_PARAM_MAX_CHARGE_POWER: "max_charging_power",
    SUNGROW_PARAM_MAX_DISCHARGE_POWER: "max_discharging_power",
    SUNGROW_PARAM_EMS_COMMAND: "ems_charge_discharge_command",
    SUNGROW_PARAM_EMS_POWER: "ems_charge_discharge_power",
    SUNGROW_PARAM_EMS_POWER_LIMIT: "ems_power_limiting_command",
    SUNGROW_PARAM_EMS_HEARTBEAT: "ems_heartbeat",
    SUNGROW_PARAM_EMS_MODE_CI: "ems_energy_management_mode",
}

# Energy Management Mode (10003) values.
SUNGROW_EMS_SELF_CONSUMPTION = 0
SUNGROW_EMS_COMPULSORY = 2
SUNGROW_EMS_EXTERNAL_DISPATCH = 3
SUNGROW_EMS_VPP = 4

# Charging/Discharging Command (10004) values. Sungrow use these three magic bytes across
# the whole parameter table for enable/disable/stop-style enumerations.
SUNGROW_CMD_CHARGE = 170
SUNGROW_CMD_DISCHARGE = 187
SUNGROW_CMD_STOP = 204

# The generic enable/disable pair used by 10065 forced charging, 10012 feed-in limitation,
# 10024 battery first and others.
SUNGROW_ENABLE = 170
SUNGROW_DISABLE = 85

# Forced Charging Valid Time (10066).
SUNGROW_FORCED_WEEKDAYS = 0
SUNGROW_FORCED_EVERYDAY = 1

# Boot/Shutdown (10011).
SUNGROW_BOOT = 207
SUNGROW_SHUTDOWN = 206
SUNGROW_REBOOT = 174

# -----------------------------------------------------------------------------
# !!! UNVERIFIED SCALING COEFFICIENTS !!!
# -----------------------------------------------------------------------------
#
# Appendix 10 does not publish the coefficients. Its own words are "refer to the external
# communication protocols for inverters to determine the coefficients", and those protocols
# are per-model. So every entry below is an ASSUMPTION, and a wrong one is silently 10x or
# 100x out - a 5000 W charge request landing as 500 W, or a 90% SoC ceiling landing as 9%.
#
# Two of them are not really guesses: Appendix 10 states the accepted RANGE for the two SoC
# limits as 700-1000 for 70-100% (10001) and 0-500 for 0-50% (10002), which pins those at a
# factor of ten. The rest are read off the same published ranges - 10005 is documented as
# "0-5000 W (default 1000)", so a watt value goes out unscaled - and are corroborated by the
# third-party pysolarcloud library, which is NOT a Sungrow source.
#
# Anything not listed here is written unscaled. If a tester reports a setpoint landing at
# the wrong order of magnitude, THIS TABLE is the first place to look.
SUNGROW_PARAM_SCALE_UNVERIFIED = {
    SUNGROW_PARAM_SOC_UPPER: 10,  # documented range 700-1000 == 70-100%
    SUNGROW_PARAM_SOC_LOWER: 10,  # documented range 0-500 == 0-50%
    SUNGROW_PARAM_POWER: 1,  # documented range 0-5000 W
    SUNGROW_PARAM_FORCED_TARGET_SOC_1: 1,  # documented range 0-100
    SUNGROW_PARAM_FORCED_TARGET_SOC_2: 1,
    SUNGROW_PARAM_FEED_IN_LIMIT_RATIO: 10,
    SUNGROW_PARAM_MAX_CHARGE_POWER: 1,
    SUNGROW_PARAM_MAX_DISCHARGE_POWER: 1,
}

# Documented accepted ranges, in ENGINEERING units (percent, watts) rather than raw wire
# units. Clamping here rather than at the call site means a plan asking for more than the
# inverter accepts is trimmed to something it will take, instead of the whole write being
# rejected and the battery left on the previous setpoint.
SUNGROW_PARAM_RANGE = {
    SUNGROW_PARAM_SOC_UPPER: (70, 100),
    SUNGROW_PARAM_SOC_LOWER: (0, 50),
    SUNGROW_PARAM_POWER: (0, 5000),
    SUNGROW_PARAM_FORCED_TARGET_SOC_1: (0, 100),
    SUNGROW_PARAM_FORCED_TARGET_SOC_2: (0, 100),
    SUNGROW_PARAM_HEARTBEAT: (1, 1000),
}

# -----------------------------------------------------------------------------
# The heartbeat
# -----------------------------------------------------------------------------
#
# 10017 External EMS Heartbeat, 1-1000 seconds. The portal is explicit: "When switching EMS
# modes through the API, make sure to send the heartbeat signal simultaneously." It is a
# DEAD-MAN'S SWITCH. Predbat writes the interval it promises to keep beating at, and if the
# beats stop the inverter drops external dispatch and returns to self-consumption on its own.
#
# That revert is the SAFE failure and the reason the heartbeat is a first-class part of this
# component rather than something bolted on. If Predbat is killed, crashes, loses its network
# or is simply stopped mid-charge, the battery falls back to ordinary self-consumption within
# one heartbeat interval. It does NOT sit stopped, and it does NOT hold the last forced
# setpoint indefinitely. Nothing in this component should ever try to defeat that.
SUNGROW_HEARTBEAT_DEFAULT_SECONDS = 300
# Beats go out at a fraction of the promised interval so a single lost request, a slow
# dispatch or one skipped component tick does not spend the whole budget and drop control.
SUNGROW_HEARTBEAT_MARGIN = 0.5
# Floor on the derived send cadence. Without it a user setting sungrow_heartbeat_interval to
# the documented minimum of 1 second would have the component dispatching a task every half
# second forever, which is a self-inflicted rate-limit rather than tighter control.
SUNGROW_HEARTBEAT_MIN_SEND_SECONDS = 30

# -----------------------------------------------------------------------------
# Measuring points
# -----------------------------------------------------------------------------
#
# Sungrow publish their measuring-point enumerations, which is why this component maps real
# point ids instead of guessing at field names. The maps below are keyed by the point id as
# a STRING because that is how it appears in point_dict, and the realtime response spells
# the same id with a "p" prefix in the data rows (p13141), which the component strips.
#
# SOURCING: the plant-level (83xxx) map is transcribed from the pysolarcloud library, which
# reads it straight out of the portal's plant measuring-point page. The device-level maps
# (13xxx energy-storage inverter, 58xxx battery, 80xx energy meter) were taken second-hand
# from a third-party integration's documentation that cites the portal's "Common ...
# Measuring Points" appendices. NEITHER was read directly off the developer portal, whose
# document viewer is a JavaScript application that would not render for us. They are
# therefore CORROBORATED BUT NOT FIRST-HAND, and a point that does not appear in a tester's
# point_dict should be assumed wrong here rather than absent on the hardware.
#
# Not every model reports every point. The component treats a missing point as absent rather
# than zero, because publishing a fabricated 0 W looks identical to a real zero.

# Energy-storage (hybrid) inverter. This is the device a residential SH install controls.
SUNGROW_ESS_POINTS = {
    "13141": "battery_soc",  # %
    "13142": "battery_soh",  # %
    "13126": "battery_charge_power",  # W, unsigned
    "13150": "battery_discharge_power",  # W, unsigned
    "13034": "battery_total_charge_energy",  # Wh
    "13035": "battery_total_discharge_energy",  # Wh
    "13119": "load_power",  # W
    "13121": "feed_in_power",  # W, unsigned - export to grid
    "13149": "purchased_power",  # W, unsigned - import from grid
    "13146": "inverter_operating_status",
}

# Battery pack, where the plant exposes one as a device in its own right.
SUNGROW_BATTERY_POINTS = {
    "58601": "battery_voltage",  # V
    "58602": "battery_current",  # A
    "58603": "battery_temperature",  # degC
    "58604": "battery_level",  # %
    "58605": "battery_soh",  # %
    "58606": "battery_total_charge_energy",  # Wh
    "58607": "battery_total_discharge_energy",  # Wh
}

# Energy meter at the grid connection point.
SUNGROW_METER_POINTS = {
    "8014": "meter_power_factor",
    "8018": "meter_active_power",  # W
    "8026": "meter_apparent_power",  # VA
    "8030": "meter_forward_active_energy",  # Wh - lifetime import
    "8031": "meter_reverse_active_energy",  # Wh - lifetime export
    "8062": "meter_daily_forward_active_energy",  # Wh - today's import
    "8063": "meter_daily_reverse_active_energy",  # Wh - today's export
    "8064": "meter_frequency",  # Hz
}

# Plant-level points. The residential case is one plant containing one hybrid inverter, and
# the plant read is the only place PV power and the daily energy counters appear at all -
# the energy-storage inverter's own point list carries no PV or yield point. So this is not
# a convenience duplicate of the device read; it is the source for several values Predbat
# cannot plan without.
SUNGROW_PLANT_POINTS = {
    "83067": "total_active_power_of_pv",  # W
    "83106": "load_power",  # W
    "83549": "grid_active_power",  # W
    "83238": "storage_active_power",  # W
    "83232": "storage_soc",  # %
    "83129": "battery_soc",  # %
    "83252": "battery_level_soc",  # %
    "83235": "chargeable_energy",  # Wh
    "83236": "dischargeable_energy",  # Wh
    "83233": "max_rechargeable_power",  # W
    "83234": "max_dischargeable_power",  # W
    "83118": "daily_load_consumption",  # Wh
    "83102": "energy_purchased_today",  # Wh
    "83072": "feed_in_energy_today",  # Wh
    "83022": "daily_yield",  # Wh
    "83243": "daily_charge_capacity",  # Wh
    "83244": "daily_discharge_capacity",  # Wh
}

# Predbat sensor leaf -> the plant point that supplies it, when the device read cannot.
# Watts on the wire, converted where Predbat wants kWh.
SUNGROW_PLANT_TELEMETRY = {
    "pv_power": "total_active_power_of_pv",
    "load_power": "load_power",
    "grid_power": "grid_active_power",
    "battery_power": "storage_active_power",
    "soc": "battery_level_soc",
}

# Daily energy counters, all Wh on the wire and published as kWh.
SUNGROW_PLANT_ENERGY = {
    "load_today": "daily_load_consumption",
    "import_today": "energy_purchased_today",
    "export_today": "feed_in_energy_today",
    "pv_today": "daily_yield",
    "battery_charge_today": "daily_charge_capacity",
    "battery_discharge_today": "daily_discharge_capacity",
}

# Wh -> kWh. The OpenAPI reports energy in watt-hours throughout; Predbat works in kWh.
SUNGROW_WH_TO_KWH = 0.001

# -----------------------------------------------------------------------------
# Caching
# -----------------------------------------------------------------------------

SUNGROW_STORAGE_MODULE = "sungrow"
SUNGROW_CACHE_STATIC = "static"
SUNGROW_CACHE_CONTROL = "control"

# Tier TTLs in minutes. iSolarCloud refreshes its stored telemetry every five minutes, so
# polling faster than that spends requests to read the same numbers back.
SUNGROW_TTL_STATIC = 8 * 60
SUNGROW_TTL_POWER = 5

SUNGROW_DEBUG_REDACT_KEYS = ("appkey", "access_key", "x-access-key", "token", "access_token", "Authorization")

# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------


def gateway_url(value):
    """Resolve a configured gateway to a base URL.

    Accepts a region name ("europe") or a full URL, so a user who copies the host out of the
    portal gets the same result as one who names their region. An unrecognised value is
    returned unchanged rather than replaced with the default: silently sending a
    misconfigured account's requests to Europe would authenticate and then find no plants,
    which reads as an empty account rather than as a configuration error.
    """
    text = str(value or "").strip()
    if not text:
        return SUNGROW_DEFAULT_GATEWAY
    lowered = text.lower()
    if lowered in SUNGROW_GATEWAYS:
        return SUNGROW_GATEWAYS[lowered]
    return text.rstrip("/")


def strip_point_prefix(key):
    """Return the bare point id from a realtime data key.

    The realtime responses spell a point as "p13141" in the data rows while point_dict keys
    it as "13141", so the two cannot be joined without this. Returns None for a key that is
    not a point at all (time_stamp, ps_id, device_sn and friends), which is how callers tell
    metadata apart from measurements.
    """
    text = str(key or "")
    if len(text) < 2 or text[0] != "p":
        return None
    body = text[1:]
    return body if body.isdigit() else None


def as_float(value, default=None):
    """Coerce an API value to float, returning default for None/'--'/junk.

    iSolarCloud writes an unavailable point as "--" rather than omitting it, so a plain
    float() would raise inside the poll loop on perfectly ordinary data.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp_range(param_code, value):
    """Clamp an engineering-unit value to the range Appendix 10 documents for that parameter.

    A parameter with no documented range passes through untouched. Clamping rather than
    rejecting is deliberate: an out-of-range write is refused outright by the cloud, which
    leaves the inverter running the PREVIOUS setpoint, and a slightly trimmed setpoint is
    much closer to the plan than a stale one.
    """
    bounds = SUNGROW_PARAM_RANGE.get(str(param_code))
    if bounds is None:
        return value
    low, high = bounds
    return max(low, min(high, value))


def scale_for_write(param_code, value):
    """Convert an engineering-unit value to the raw string paramSetting expects.

    Clamps first, then applies SUNGROW_PARAM_SCALE_UNVERIFIED - see the banner on that table,
    every coefficient in it is an assumption. The result is a string because param_list
    entries carry set_value as a string and an int sent as a JSON number is rejected.

    Integral results lose their ".0" so a 90% SoC ceiling goes out as "900" rather than
    "900.0", which is the form the portal's own examples use.
    """
    numeric = as_float(value, 0.0)
    numeric = clamp_range(param_code, numeric)
    scaled = numeric * SUNGROW_PARAM_SCALE_UNVERIFIED.get(str(param_code), 1)
    if abs(scaled - round(scaled)) < 1e-9:
        return str(int(round(scaled)))
    return str(scaled)


def scale_from_read(param_code, value):
    """Convert a raw paramSetting read-back to engineering units.

    The inverse of scale_for_write, so a read-back can be compared against what was asked
    for. Returns None for an unreadable value rather than 0, because a parameter that could
    not be read is not a parameter that reads zero.
    """
    numeric = as_float(value, None)
    if numeric is None:
        return None
    return numeric / SUNGROW_PARAM_SCALE_UNVERIFIED.get(str(param_code), 1)


def heartbeat_send_interval(interval_seconds):
    """Return how often to actually send the heartbeat, given the interval promised to 10017.

    Sending exactly at the promised interval is a coin flip: one lost request, one slow task
    dispatch or one skipped component tick and the inverter has already reverted. Beating at
    a fraction of the window buys a free retry, at the cost of a few extra dispatches an hour.

    Floored at SUNGROW_HEARTBEAT_MIN_SEND_SECONDS so the documented minimum interval of one
    second cannot turn into a dispatch every half second.
    """
    promised = clamp_range(SUNGROW_PARAM_HEARTBEAT, as_float(interval_seconds, SUNGROW_HEARTBEAT_DEFAULT_SECONDS))
    return max(SUNGROW_HEARTBEAT_MIN_SEND_SECONDS, promised * SUNGROW_HEARTBEAT_MARGIN)


def signed_battery_power(charge_w, discharge_w):
    """Combine the two unsigned battery power points into one signed value.

    The energy-storage inverter reports charge and discharge as SEPARATE unsigned points
    (13126 and 13150), only one of which is non-zero at a time. Predbat wants a single
    sensor, positive on discharge and negative on charge, matching the convention every
    other cloud component publishes.

    Returns None when neither point was reported, so an inverter that publishes neither is
    left without the sensor rather than pinned at a fabricated zero.
    """
    charge = as_float(charge_w, None)
    discharge = as_float(discharge_w, None)
    if charge is None and discharge is None:
        return None
    return (discharge or 0.0) - (charge or 0.0)


def signed_grid_power(purchased_w, feed_in_w):
    """Combine the two unsigned grid power points into one signed value.

    13149 purchased_power and 13121 feed_in_power are both unsigned. Predbat's grid_power is
    NEGATIVE on import and positive on export - the same convention alphaess.py reaches by
    negating pgrid - so import is subtracted here rather than added. Getting this backwards
    makes every export read as an import and silently inverts the whole plan.

    Returns None when neither point was reported.
    """
    purchased = as_float(purchased_w, None)
    feed_in = as_float(feed_in_w, None)
    if purchased is None and feed_in is None:
        return None
    return (feed_in or 0.0) - (purchased or 0.0)


def hhmmss_to_hour_minute(value):
    """Split Predbat's HH:MM:SS control-entity value into (hour, minute) integers.

    The forced-charging window parameters take the hour and the minute as four SEPARATE
    parameter codes rather than a time string, so the split happens once here instead of at
    each of the eight call sites. Returns (0, 0) for anything unusable, which pairs with an
    equal start and end and therefore disables the window rather than raising in the poll loop.
    """
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return 0, 0
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return 0, 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        # 24:00 is Predbat's midnight end. The parameters have no way to express it, so it
        # is pulled back to the last minute of the day rather than wrapping to 00:00, which
        # would read as an empty window and cancel the charge.
        if hour >= 24:
            return 23, 59
        return 0, 0
    return hour, minute


def window_is_empty(start, end):
    """Return True when a forced-charging window carries no time the inverter would act on.

    Covers the disabled form (start == end) and an inverted window. An inverted window must
    NOT be written as a wrap-around: nothing in Appendix 10 says the inverter treats
    22:00-02:00 as crossing midnight, so it is disabled instead of guessed at.
    """
    start_hour, start_minute = hhmmss_to_hour_minute(start)
    end_hour, end_minute = hhmmss_to_hour_minute(end)
    return (end_hour * 60 + end_minute) <= (start_hour * 60 + start_minute)


def param_entry(param_code, set_value=""):
    """Build one param_list entry for paramSetting.

    set_value is an empty string for a read (set_type 2) and the raw scaled string for a
    write, which is the only structural difference between the two operations.
    """
    return {"param_code": str(param_code), "set_value": str(set_value)}


def describe_param(param_code):
    """Return 'code (name)' for a control parameter, or just the code when unknown."""
    code = str(param_code)
    name = SUNGROW_PARAM_NAMES.get(code)
    return "{} ({})".format(code, name) if name else code


def hm_to_minutes(value):
    """Convert Predbat's HH:MM:SS control-entity value to minutes since midnight.

    Hours above 23 are kept rather than clamped, because Predbat represents a window
    crossing midnight as (for example) 23:00-25:00 and the caller needs to see that.
    Returns 0 for anything unusable.
    """
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return 0
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (TypeError, ValueError):
        return 0
