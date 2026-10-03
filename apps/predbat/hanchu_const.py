# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Hanchu ESS cloud API constants
# -----------------------------------------------------------------------------

"""Constants and pure helpers for the Hanchu ESS cloud component.

Kept separate from hanchu.py so the wire format, the parameter ranges and the
seconds-since-midnight time encoding can be tested without constructing a component.

PROVENANCE - READ THIS BEFORE TRUSTING ANY NAME IN THIS FILE
============================================================
Hanchu publish NO API specification. Every endpoint, parameter and field below was learned by
READING three MIT-licensed Home Assistant integrations. None of their code, structure, comments
or docstrings is reproduced here - only the wire facts, re-expressed in Predbat's own idiom.

The sources, and the short names used in the annotations below:

  UPTON     https://github.com/upton68/hanchu-ess-ha  - the actively maintained fork (~281
            commits). Treated as authoritative wherever the three disagree.
  GUOXIA    https://github.com/guoxiatech/hanchu-ess-ha - the upstream UPTON forked from
            (~128 commits). Its api.py is a near-identical earlier copy, which makes it a weak
            independent witness for the shared request layer but a genuine one for the
            endpoints it also carries.
  BLUSTERY  https://github.com/Blustery7752/hanchu-ess - a thinner third integration. It talks
            to a DIFFERENT API surface (the web app's, not the Home Assistant one), so where it
            agrees on a PARAMETER NAME that is strong independent corroboration - the same
            control key reached over a different path.

GUOXIA is Hanchu's OWN integration (Guoxiatech is the company trading as Hanchu), so the shared
login and request layer in GUOXIA and UPTON is the vendor's code, not a third-party guess. It was
read-only; UPTON added the control side. (Confirmed by UPTON's maintainer on
springfall2008/batpred#5305.)

Every entry carries one of four markers:

  HARDWARE    confirmed on a real Hanchu iESS system running Predbat, by UPTON's maintainer on
              springfall2008/batpred#5305. One system only, so other models may still differ.
  OBSERVED    read in exactly one repo, at the file named.
  CORROBORATED read in two or more repos, files named.
  INFERRED    NOT read anywhere. Derived by reasoning, or assumed from how Predbat needs to use
              it. Treat as a guess until hardware says otherwise.

Anything marked INFERRED is the first thing a real connection will disprove.
"""

# --- Base URL ---------------------------------------------------------------------------------
# CORROBORATED: UPTON const.py BASE_URL, GUOXIA const.py BASE_URL (both "https://iess3.hanchuess.com",
# both overridable by a HANCHUESS_URL environment variable, which is a Home Assistant testing
# convenience and is deliberately not reproduced). BLUSTERY const.py DEFAULT_BASE_URL is the same
# host with a trailing "/gateway/", i.e. the same origin reached with the path prefix folded in.
HANCHU_BASE_URL = "https://iess3.hanchuess.com"

# --- Endpoints --------------------------------------------------------------------------------
# All are POST. The "/gateway/app/ha/..." family is a surface the vendor appears to have added for
# the Home Assistant integration: it takes and returns PLAIN JSON.
#
# BLUSTERY reaches equivalent operations through "/gateway/platform/..." instead
# (platform/deviceNew/iotGet, platform/deviceNew/iotSet, platform/remoteContrDtu/fastChargeDischarge,
# platform/homePage/stationInfo) with AES-CBC-encrypted bodies. We use the app/ha family because it
# is the maintained one AND because it needs no crypto - see HANCHU_ENCRYPTED_ENDPOINTS_NOT_USED.
HANCHU_ENDPOINTS = {
    # CORROBORATED: UPTON api.py async_login, GUOXIA api.py async_login. Body {"account", "pwd"},
    # password in PLAINTEXT over TLS. Response data is the bare token STRING, not an object.
    # DISAGREEMENT: BLUSTERY api.py async_login posts to "identify/auth/login/account" with the
    # password RSA-encrypted (PKCS1 v1.5) and the whole body AES-encrypted. See the module docstring
    # of hanchu.py - we follow UPTON.
    "login": "/gateway/identify/auth/token",
    # CORROBORATED: UPTON api.py async_refresh_token, GUOXIA api.py async_refresh_token.
    # Body {"token": <current token>}, response data is the new token string.
    "refresh": "/gateway/identify/auth/token/refresh",
    # CORROBORATED: UPTON api.py async_get_devices, GUOXIA api.py async_get_devices. Empty body {}.
    "device_list": "/gateway/app/ha/getDeviceList",
    # CORROBORATED: UPTON api.py async_get_device_status, GUOXIA api.py async_get_device_status.
    "device_status": "/gateway/app/ha/getDeviceStatus",
    # CORROBORATED: UPTON api.py async_get_device_statistics, GUOXIA api.py async_get_device_statistics.
    "device_statistics": "/gateway/app/ha/getDeviceStatistics",
    # CORROBORATED: UPTON api.py async_get_menu, GUOXIA api.py async_get_menu. Returns the phone
    # app's settings menu, which is where the per-device min/max for each control key lives.
    "menu": "/gateway/app/ha/menu",
    # CORROBORATED: UPTON api.py async_iot_get, GUOXIA api.py async_iot_get; the same operation on
    # BLUSTERY's surface is platform/deviceNew/iotGet (api.py get_device_settings), and it sends the
    # SAME three fields {devType, sn, keys}, which is why the request shape is corroborated rather
    # than merely observed.
    "iot_get": "/gateway/app/ha/iotGet",
    # CORROBORATED: UPTON api.py async_device_control, GUOXIA api.py async_device_control;
    # BLUSTERY api.py set_device_setting uses platform/deviceNew/iotSet with the same
    # {devType, sn, value} shape.
    "iot_set": "/gateway/app/ha/iotSet",
    # CORROBORATED: UPTON api.py async_fast_charge_discharge, GUOXIA api.py async_fast_charge_discharge,
    # BLUSTERY api.py fast_charge_discharge (as platform/remoteContrDtu/fastChargeDischarge). All three
    # send {sn, act, duration}.
    "fast_charge": "/gateway/app/ha/fastChargeDischarge",
}

# Endpoints this component deliberately does NOT call, recorded so a later reader does not have to
# rediscover them:
#   /gateway/platform/station/detail                       (OBSERVED: UPTON api.py async_get_station_detail)
#   /gateway/platform/bmsInfo/queryBatteryDataDivisions    (OBSERVED: UPTON api.py async_get_battery_data)
# Both take an AES-CBC-encrypted, Base64 body keyed with a FIXED key and IV hard-coded in the
# integration (OBSERVED: UPTON const.py AES_IV / AES_SECRET_KEY, crypto.py). They carry per-pack BMS
# detail - cell temperatures, pack voltage/current, SoH - that Predbat does not plan with. Skipping
# them keeps a hard-coded vendor key out of Predbat and removes a crypto dependency, at the cost of
# per-pack temperature and SoH sensors. Revisit only if Predbat gains a use for them.
HANCHU_ENCRYPTED_ENDPOINTS_NOT_USED = ("/gateway/platform/station/detail", "/gateway/platform/bmsInfo/queryBatteryDataDivisions")

# --- Request/response envelope ----------------------------------------------------------------
# CORROBORATED: UPTON api.py _headers, GUOXIA api.py _headers. "appPlat: ha" identifies the client
# to the server; access-token carries the session token; locale is normalised to "en" or "zh"
# (anything starting "zh" becomes "zh", everything else "en").
HANCHU_HEADER_PLATFORM = "ha"
HANCHU_HEADER_TOKEN = "access-token"
HANCHU_HEADER_LOCALE = "en"

# CORROBORATED: UPTON api.py _request, GUOXIA api.py _request. The envelope is
# {"code": int, "success": bool, "msg": str, "data": ...}. Success is `success: true`; HTTP 200 with
# code 401 means the token has expired; code 100 is a body-level failure whose msg names the reason.
HANCHU_CODE_OK = 200
HANCHU_CODE_UNAUTHORISED = 401
HANCHU_CODE_FAILED = 100

# OBSERVED: UPTON api.py ReauthRequired is raised when the refresh endpoint answers code 100, and
# the component then asks Home Assistant to re-authenticate. Note the INTERNAL INCONSISTENCY in the
# source: that exception's own docstring says the trigger is code 90076, while the code that raises
# it tests for 100. We follow the CODE, not the docstring, and fall back to a full re-login rather
# than giving up - Predbat holds the account credentials, so it can always log in again.
HANCHU_CODE_REFRESH_REJECTED = 100

# OBSERVED: UPTON api.py TOKEN_REFRESH_DAYS = 25; GUOXIA api.py carries the identical constant, but
# as a near-copy of the same file it is weak corroboration. The real server-side lifetime is unknown -
# 25 days is the reference integration's own safety margin, not a documented figure. We refresh
# proactively at this age AND re-login reactively on a 401, so being wrong costs one extra request.
HANCHU_TOKEN_REFRESH_DAYS = 25
HANCHU_TOKEN_REFRESH_SECONDS = HANCHU_TOKEN_REFRESH_DAYS * 24 * 3600
# OBSERVED: UPTON api.py async_refresh_token - a forced refresh within 30 seconds of the previous
# attempt is suppressed, so a burst of 401s cannot become a refresh storm.
HANCHU_REFRESH_MIN_INTERVAL = 30

# OBSERVED: UPTON api.py _request uses a 15-second timeout and 3 attempts with a 2s, 4s backoff.
# GUOXIA's copy uses 10 seconds and a single attempt. UPTON's is the maintained figure.
HANCHU_TIMEOUT = 15
HANCHU_RETRIES = 3

# --- Device type ------------------------------------------------------------------------------
# CORROBORATED: UPTON config_flow.py filters the device list on devType == "2" and defaults to "2";
# BLUSTERY api.py names the same value DEVICE_TYPE_DTU = "2". A STRING, not an int, in every repo.
# The DTU is the datalogger dongle, and it is what both iotGet and iotSet are addressed to.
HANCHU_DEV_TYPE_DTU = "2"

# --- Device list fields -----------------------------------------------------------------------
# OBSERVED: UPTON config_flow.py reads d["sn"] and d.get("devType") from each getDeviceList entry.
# Nothing else about an entry is read by any of the three repos, so the list's own model name,
# station id or capacity fields (if it has any) are UNKNOWN. This is why the component takes its
# ratings from getDeviceStatus instead.
HANCHU_DEVICE_SN_FIELD = "sn"
HANCHU_DEVICE_TYPE_FIELD = "devType"

# --- Telemetry: getDeviceStatus ---------------------------------------------------------------
# Predbat sensor leaf -> getDeviceStatus field.
# All OBSERVED in UPTON sensor.py SENSORS. GUOXIA sensor.py carries batSoc, batP, pvTtPwr, meterPPwr
# and loadPwr under the same names, so those five are CORROBORATED; the rest are single-source.
HANCHU_TELEMETRY = {
    # CORROBORATED (UPTON sensor.py "battery_soc", GUOXIA sensor.py). Carries a FRACTION, not a
    # percent: UPTON applies "scale": 100 to it. See HANCHU_SOC_SCALE.
    "soc": "batSoc",
    # CORROBORATED (UPTON sensor.py "battery_power", GUOXIA sensor.py). Signed, POSITIVE ON CHARGE -
    # UPTON derives battery_charge_power from it with derive_mode "positive" and
    # battery_discharge_power with "negative_as_positive". Predbat wants the opposite sign, so this
    # is negated on publish (HANCHU_TELEMETRY_NEGATE).
    "battery_power": "batP",
    # CORROBORATED (UPTON sensor.py "grid_power", GUOXIA sensor.py). Signed, POSITIVE ON IMPORT -
    # UPTON derives grid_import_power with derive_mode "positive". Negated on publish.
    "grid_power": "meterPPwr",
    # CORROBORATED (UPTON sensor.py "pv_power", GUOXIA sensor.py). DC string total.
    "pv_power": "pvTtPwr",
    # CORROBORATED (UPTON sensor.py "load_power", GUOXIA sensor.py). House load.
    "load_power": "loadPwr",
    # OBSERVED (UPTON sensor.py "ac_coupled_pv_power"). AC-coupled PV measured through the bypass
    # meter. Published for visibility; deliberately NOT added into pv_power, since whether it is
    # already included in pvTtPwr is unknown.
    "ac_pv_power": "bypMeterTotalPower",
}

# Fields whose sign is opposite to Predbat's convention (battery NEGATIVE on charge, grid NEGATIVE
# on import - see inverter.py's battery_power_invert handling and sunsynk_const.py's own note).
# Listed explicitly rather than inferred so the intent survives a refactor.
HANCHU_TELEMETRY_NEGATE = ("battery_power", "grid_power")

# OBSERVED: UPTON sensor.py "battery_soc" carries "scale": 100 and its native_value multiplies by
# it, so batSoc is a 0..1 fraction on the wire. Applied on publish so Predbat sees a percent.
HANCHU_SOC_SCALE = 100

# OBSERVED: UPTON sensor.py _scale_auto_watt. Several power fields carry a SIBLING unit string
# naming their own unit ("W" or "kW"), because the API reports some of them in kW. Leaf -> unit field.
# A field with no entry here, or whose unit string is missing or unrecognised, falls back to the
# magnitude heuristic below.
HANCHU_TELEMETRY_UNIT_FIELD = {
    "battery_power": "batPUnit",
    "grid_power": "meterPPwrUnit",
    "pv_power": "pvTtPwrUnit",
    "ac_pv_power": "bypMeterTotalPowerUnit",
}

# OBSERVED: UPTON sensor.py _scale_auto_watt - with no usable unit string, a magnitude below 10 is
# assumed to be kW. INFERRED consequence, stated because it matters: this misreads a genuine
# sub-10 W reading as kW. It only applies to fields with no unit sibling (loadPwr is the one in
# HANCHU_TELEMETRY), and a house load under 10 W is implausible, so the heuristic is kept - but it
# is a heuristic, and a real connection may show loadPwr carries a unit field after all.
HANCHU_WATT_HEURISTIC_THRESHOLD = 10

# OBSERVED (UPTON sensor.py "battery_capacity"); HARDWARE: in kWh, usable as soc_max without
# conversion. Usable battery capacity as the BMS designs it. The only capacity figure on the
# plain-JSON surface.
HANCHU_CAPACITY_FIELD = "bmsDesignCap"

# OBSERVED: UPTON __init__.py _resolve_station_id reads getDeviceStatus["stationId"]. Not needed by
# this component (we call no station endpoint) but published as a diagnostic, and reported to the
# discovery catalogue as an account id.
HANCHU_STATION_FIELD = "stationId"

# --- Daily energy: getDeviceStatistics --------------------------------------------------------
# Predbat sensor leaf -> getDeviceStatistics field. All OBSERVED in UPTON sensor.py
# STATISTICS_SENSORS; GUOXIA sensor.py carries the same six names, so all six are CORROBORATED.
# kWh, TOTAL_INCREASING, resetting at midnight - Predbat's minute_data absorbs the reset.
HANCHU_ENERGY = {
    "load_today": "load",
    "import_today": "gridImport",
    "export_today": "gridExport",
    "pv_today": "pv",
    "battery_charge_today": "batCharge",
    "battery_discharge_today": "batDisCharge",
}

# --- Control keys (iotGet / iotSet) -----------------------------------------------------------
# These are the strongest-evidenced names in this file: all three repos name them identically, and
# BLUSTERY reaches them over a DIFFERENT endpoint, so agreement is not just a copied file.
#
# CORROBORATED: UPTON select.py WORK_MODES + number.py NUMBERS + time.py TIME_SLOTS,
# GUOXIA number.py/switch.py, BLUSTERY api.py DEFAULT_DEVICE_SETTING_KEYS (which lists
# WORK_MODE_CMB, CHG_PWR_LMT, DSCHG_PWR_LMT, DTU_AC_CHG_SOC_LMT, CHG_BAT_SOC_LMT,
# DSCHG_BAT_SOC_LMT and all twelve TCT_/TDT_ slot keys).
HANCHU_KEY_WORK_MODE = "WORK_MODE_CMB"
HANCHU_KEY_CHARGE_POWER = "CHG_PWR_LMT"
HANCHU_KEY_DISCHARGE_POWER = "DSCHG_PWR_LMT"
HANCHU_KEY_CHARGE_SOC = "CHG_BAT_SOC_LMT"
HANCHU_KEY_DISCHARGE_SOC = "DSCHG_BAT_SOC_LMT"
HANCHU_KEY_GRID_CHARGE_SOC = "DTU_AC_CHG_SOC_LMT"

# CORROBORATED: UPTON time.py TIME_SLOTS names all twelve; BLUSTERY api.py lists the same twelve.
# THREE charge slots and THREE discharge slots, numbered from 1.
HANCHU_SLOT_COUNT = 3
HANCHU_KEY_CHARGE_START = "TCT_START_{}"
HANCHU_KEY_CHARGE_END = "TCT_END_{}"
HANCHU_KEY_DISCHARGE_START = "TDT_START_{}"
HANCHU_KEY_DISCHARGE_END = "TDT_END_{}"

# Every key the component reads back at startup and on each config tier, in one iotGet.
# OBSERVED: UPTON __init__.py async_setup_entry fetches exactly this set in a single call.
HANCHU_CONTROL_KEYS = (
    [HANCHU_KEY_WORK_MODE, HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER, HANCHU_KEY_CHARGE_SOC, HANCHU_KEY_DISCHARGE_SOC, HANCHU_KEY_GRID_CHARGE_SOC]
    + [HANCHU_KEY_CHARGE_START.format(index) for index in range(1, HANCHU_SLOT_COUNT + 1)]
    + [HANCHU_KEY_CHARGE_END.format(index) for index in range(1, HANCHU_SLOT_COUNT + 1)]
    + [HANCHU_KEY_DISCHARGE_START.format(index) for index in range(1, HANCHU_SLOT_COUNT + 1)]
    + [HANCHU_KEY_DISCHARGE_END.format(index) for index in range(1, HANCHU_SLOT_COUNT + 1)]
)

# --- Work modes -------------------------------------------------------------------------------
# OBSERVED: UPTON select.py WORK_MODES maps the four option names to STRING values "1".."4".
# GUOXIA ships no select.py at all (its select platform file is empty), so this is single-source.
# The values are strings on the wire; UPTON's websocket iotSet handler coerces numeric strings to
# int before sending, so the server evidently accepts either. We send int, matching that handler.
HANCHU_WORK_MODE_SELF_CONSUMPTION = 1
HANCHU_WORK_MODE_BACKUP = 2
HANCHU_WORK_MODE_USER_DEFINED = 3
HANCHU_WORK_MODE_OFF_GRID = 4
HANCHU_WORK_MODES = {
    HANCHU_WORK_MODE_SELF_CONSUMPTION: "Self-consumption",
    HANCHU_WORK_MODE_BACKUP: "Backup Energy",
    HANCHU_WORK_MODE_USER_DEFINED: "User-defined",
    HANCHU_WORK_MODE_OFF_GRID: "Off-grid",
}

# HARDWARE: work mode 3, "User-defined", is the mode that honours the TCT_/TDT_ timed slots. No
# repo links the mode to the slots, so this was originally inferred from the name; it has since been
# confirmed on a real system. The component drives the mode to User-defined whenever it writes a
# window, and leaves it alone otherwise. hanchu_work_mode_control: False disables this and leaves
# the mode entirely to the user.
HANCHU_WORK_MODE_FOR_SCHEDULE = HANCHU_WORK_MODE_USER_DEFINED

# --- Parameter ranges -------------------------------------------------------------------------
# OBSERVED: UPTON __init__.py async_setup_entry seeds exactly these five fallback ranges, then
# OVERRIDES them per device from the menu endpoint (item minVal/maxVal keyed by itemCodeSignal).
# They are therefore defaults, not hard limits - the component asks the menu too, and prefers what
# it says. Key -> (min, max).
HANCHU_RANGES = {
    HANCHU_KEY_CHARGE_POWER: (0, 5000),
    HANCHU_KEY_DISCHARGE_POWER: (0, 5000),
    # 50 is a real floor, and a real limitation for Predbat: a charge TARGET below 50% cannot be
    # expressed at all. See hanchu.py's clamp logging.
    HANCHU_KEY_CHARGE_SOC: (50, 100),
    # 45 is a real ceiling, and the mirror-image limitation: an export/discharge target above 45%
    # cannot be expressed.
    HANCHU_KEY_DISCHARGE_SOC: (5, 45),
    HANCHU_KEY_GRID_CHARGE_SOC: (20, 100),
}

# OBSERVED: UPTON number.py gives both power controls "step": 100. Used for the published number
# entities so Predbat's own UI steps match what the device accepts.
HANCHU_POWER_STEP = 100

# --- Menu parsing -----------------------------------------------------------------------------
# OBSERVED: UPTON __init__.py walks menu_data["data"]["energy"]["items"] - a list of GROUPS, each a
# list of items - and matches item["itemCodeSignal"] against the control key, reading "minVal" and
# "maxVal". Field names recorded here so the walk in hanchu.py does not bury them.
HANCHU_MENU_DATA = "data"
HANCHU_MENU_SECTION = "energy"
HANCHU_MENU_ITEMS = "items"
HANCHU_MENU_SIGNAL = "itemCodeSignal"
HANCHU_MENU_MIN = "minVal"
HANCHU_MENU_MAX = "maxVal"

# --- iotSet acknowledgement -------------------------------------------------------------------
# OBSERVED: BLUSTERY api.py set_device_setting reads data["responseSemMap"], a per-key map in which
# "1" means the device accepted that key and anything else means it rejected it. UPTON and GUOXIA
# both ignore this and treat envelope success alone as success. Single-source, but a per-key reject
# signal is worth having, so it is read WHEN PRESENT and its absence is never treated as a failure.
HANCHU_ACK_MAP = "responseSemMap"
HANCHU_ACK_OK = "1"

# --- Fast charge / discharge ------------------------------------------------------------------
# CORROBORATED: UPTON switch.py (2 / -2 / 3 / -3 with a duration in SECONDS, and 0 duration to
# stop), BLUSTERY api.py fast_charge_discharge docstring (identical act values and the same
# "duration required to start, ignored to stop"), GUOXIA switch.py. Also bounded to -3..3 by
# UPTON's own service schema.
#
# NOT USED by this component. Predbat drives the battery through the timed slots, which are
# declarative and idempotent; a duration-based imperative command would have to be re-issued before
# it lapsed and would fight the schedule. Recorded because it is the only way to get an immediate
# action out of this API, and a future "charge now" feature would want it.
HANCHU_FAST_CHARGE_START = 2
HANCHU_FAST_CHARGE_STOP = -2
HANCHU_FAST_DISCHARGE_START = 3
HANCHU_FAST_DISCHARGE_STOP = -3

# --- Time encoding ----------------------------------------------------------------------------
# OBSERVED: UPTON time.py _decode_time_value divides the raw value by 3600 for hours and takes
# (raw % 3600) // 60 for minutes, and async_set_value encodes hour * 3600 + minute * 60. So the
# slot keys carry SECONDS SINCE MIDNIGHT as an integer. Seconds are always zero in practice because
# the phone app only offers minutes.
HANCHU_SECONDS_PER_DAY = 24 * 3600

# HARDWARE: a slot is disabled by setting both its start and end to 00:00. There is no separate
# per-slot enable key anywhere in the twelve.
#
# User-defined mode runs ALL THREE charge and ALL THREE discharge slots. So once Predbat drives an
# inverter it owns every slot, and writes 00:00-00:00 into each one its plan does not use. A slot
# left over from the Hanchu app would otherwise keep charging or discharging the battery on its
# own timetable, against the plan. Nothing is written until Predbat presses the schedule write
# button for that inverter, so an inverter Predbat is not driving is never touched.
HANCHU_SLOT_DISABLED = 0

# --- Caching and pacing -----------------------------------------------------------------------
HANCHU_STORAGE_MODULE = "hanchu"
HANCHU_CACHE_STATIC = "static"
HANCHU_CACHE_CONFIG = "config"
HANCHU_CACHE_CONTROL = "control"

# Tier TTLs in minutes. No published rate limit exists. UPTON's default poll intervals are 60s for
# realtime and 300s for statistics (OBSERVED: UPTON const.py DEFAULT_REALTIME_INTERVAL,
# DEFAULT_STATISTICS_INTERVAL), which these mirror. The static and config tiers are Predbat's own
# choice.
HANCHU_TTL_STATIC = 8 * 60
HANCHU_TTL_CONFIG = 30
HANCHU_TTL_POWER = 1
HANCHU_TTL_ENERGY = 5

# INFERRED, and the reason the write path is batched at all. UPTON stages every control change in
# memory and flushes the whole lot in ONE iotSet behind an explicit "Write Settings" button,
# and its own docstring says this exists to avoid rate limiting (OBSERVED: UPTON staging.py module
# docstring, __init__.py async_flush_staged). No numeric limit is documented anywhere. Predbat
# re-plans every few minutes and would otherwise send a write per setting per cycle, so a default
# pacing interval is applied on top of the batching. Sixty seconds is a deliberately mild default -
# unlike AlphaESS there is no evidence of a 24-hour budget - and it is user-tunable.
HANCHU_MIN_WRITE_INTERVAL = 60

# OBSERVED: UPTON __init__.py async_flush_staged retries a failed flush ONCE after 5 seconds before
# giving up and leaving the staged changes intact for the user to retry.
HANCHU_WRITE_RETRY_DELAY = 5

# A write that FAILED is retried on the next cycle rather than held for the full pacing interval:
# a rejected shorten leaves the inverter running past the plan, so waiting costs real energy. The
# reconcile loop runs about once a minute, so this still means at most one attempt per cycle.
HANCHU_FAILED_WRITE_INTERVAL = 30

# HARDWARE (batpred#5305): after this many consecutive rejected writes the user is notified,
# because the inverter may no longer match the plan and may need stepping in on by hand.
HANCHU_WRITE_FAILURE_NOTIFY = 3

# Keys never written to the log. The login body's password, and the token in every header and in
# both auth responses. `msg`, `code` and `data` for non-auth endpoints are always logged: nobody on
# the project has Hanchu hardware, so a tester's log is the only evidence available.
HANCHU_DEBUG_REDACT_KEYS = ("pwd", "password", "token", "access-token", "accessToken")


def seconds_to_hhmmss(value):
    """Convert a slot's seconds-since-midnight value to Predbat's HH:MM:SS control-entity form.

    Returns "00:00:00" for anything unusable, which is this API's only expression of a disabled
    slot, so a missing or malformed value reads as "no window" rather than raising inside the poll
    loop. Values at or beyond a full day wrap, because the wire format is a time of day.
    """
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return "00:00:00"
    total = total % HANCHU_SECONDS_PER_DAY if total >= 0 else 0
    return "{:02d}:{:02d}:{:02d}".format(total // 3600, (total % 3600) // 60, total % 60)


def hhmmss_to_seconds(value):
    """Convert Predbat's HH:MM:SS control-entity value to seconds since midnight.

    Returns 0 for anything unusable. A 24:00:00 end - Predbat's way of saying midnight - is clamped
    to one second before midnight rather than wrapping to 0, because 0 is how a disabled slot is
    written and a full-day window must not collapse into one.
    """
    text = str(value or "").strip()
    if not text:
        return 0
    parts = text.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        seconds = int(float(parts[2])) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        return 0
    total = hours * 3600 + minutes * 60 + seconds
    if total >= HANCHU_SECONDS_PER_DAY:
        return HANCHU_SECONDS_PER_DAY - 60
    return max(0, total)


def split_window_seconds(start, end):
    """Split an HH:MM:SS window at midnight into up to two seconds-since-midnight slot pairs.

    INVERTER_DEF sets can_span_midnight False, so Predbat itself avoids asking for a wrap-around,
    but it still expresses "until midnight" as an end hour of 24 or more. The second pair carries
    whatever runs past midnight and is (0, 0) - disabled - when nothing does.

    Returns ((start1, end1), (start2, end2)).
    """
    start_seconds = hhmmss_to_seconds(start)
    text = str(end or "").strip()
    parts = text.split(":")
    try:
        end_seconds = int(parts[0]) * 3600 + (int(parts[1]) * 60 if len(parts) > 1 else 0)
    except (TypeError, ValueError):
        end_seconds = start_seconds
    if end_seconds <= HANCHU_SECONDS_PER_DAY:
        return (start_seconds, end_seconds), (HANCHU_SLOT_DISABLED, HANCHU_SLOT_DISABLED)
    return (start_seconds, HANCHU_SECONDS_PER_DAY - 60), (HANCHU_SLOT_DISABLED, end_seconds - HANCHU_SECONDS_PER_DAY)


def window_is_empty(start_seconds, end_seconds):
    """Return True when a slot pair carries no time the inverter would act on.

    Covers both the disabled form (start == end) and a window an earlier step has inverted. An
    inverted window is never written as a wrap-around: wrap behaviour is undocumented, so it is
    written as disabled instead.
    """
    return end_seconds <= start_seconds


def clamp_range(key, value, ranges=None):
    """Clamp a control value into the range recorded for its key, returning (value, clamped).

    `clamped` is True when the value had to move, so the caller can log it once rather than
    silently sending something other than what Predbat asked for. A key with no recorded range is
    returned untouched. `ranges` lets a caller pass the per-device limits read from the menu
    endpoint in place of the built-in defaults.
    """
    limits = (ranges or HANCHU_RANGES).get(key)
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = 0
    if not limits:
        return number, False
    low, high = int(limits[0]), int(limits[1])
    bounded = max(low, min(high, number))
    return bounded, bounded != number


def scale_to_watts(value, unit=None, threshold=HANCHU_WATT_HEURISTIC_THRESHOLD):
    """Normalise one power reading to watts, returning None when it cannot be read as a number.

    The sibling unit string is trusted when it is present and recognised ("kW" or "W"). With no
    usable unit, a magnitude below `threshold` is assumed to be kW - the reference integration's
    own fallback for the fields the API does not tag. See HANCHU_WATT_HEURISTIC_THRESHOLD for why
    that heuristic is kept despite being a heuristic.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if unit:
        text = str(unit).strip().lower()
        if text == "kw":
            return round(number * 1000.0, 1)
        if text == "w":
            return round(number, 1)
    if abs(number) < threshold:
        return round(number * 1000.0, 1)
    return round(number, 1)
