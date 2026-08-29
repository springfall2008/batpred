# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# AlphaESS Open API constants
# -----------------------------------------------------------------------------

"""Constants and pure helpers for the AlphaESS Open API component.

Kept separate from alphaess.py so the wire format, the documented return codes and the
15-minute time grid can be tested without constructing a component.
"""

ALPHAESS_BASE_URL = "https://openapi.alphaess.com/api"

ALPHAESS_ENDPOINTS = {
    "ess_list": "/getEssList",
    "last_power": "/getLastPowerData",
    "one_day_power": "/getOneDayPowerBySn",
    "one_date_energy": "/getOneDateEnergyBySn",
    "sum_data": "/getSumDataForCustomer",
    "charge_config": "/getChargeConfigInfo",
    "update_charge_config": "/updateChargeConfigInfo",
    "discharge_config": "/getDisChargeConfigInfo",
    "update_discharge_config": "/updateDisChargeConfigInfo",
    "time_charge": "/getTimeChargeBySn",
    "set_time_charge": "/setTimeChargeBySn",
    "verification_code": "/getVerificationCode",
    "bind": "/bindSn",
    "unbind": "/unBindSn",
}

# Published on the developer portal. 6017 and 10001 are returned by the live API but are
# not in the portal's table - kept here anyway because the component branches on 6017.
ALPHAESS_RETURN_CODES = {
    6001: "Parameter error",
    6002: "The SN is not bound to the user",
    6003: "You have bound this SN",
    6004: "CheckCode error",
    6005: "This appId is not bound to the SN",
    6006: "Timestamp error",
    6007: "Sign verification error",
    6008: "Set failed",
    6009: "Whitelist verification failed",
    6010: "Sign is empty",
    6011: "timestamp is empty",
    6012: "AppId is empty",
    6016: "Data does not exist or has been deleted",
    6017: "No operation permissions",
    6026: "internal error",
    6029: "operation failed",
    6038: "system sn does not exist",
    6042: "system offline",
    6046: "Verification code error",
    6053: "The request was too fast, please try again later",
    10001: "Parameter error (malformed request body)",
}

ALPHAESS_CODE_OK = 200
ALPHAESS_CODE_ALREADY_BOUND = 6003
ALPHAESS_CODE_NOT_BOUND = 6005
ALPHAESS_CODE_NO_PERMISSION = 6017
ALPHAESS_CODE_OFFLINE = 6042
ALPHAESS_CODE_TOO_FAST = 6053
ALPHAESS_CODE_TIMESTAMP = 6006
ALPHAESS_CODE_SIGN = (6007, 6010, 6012)

ALPHAESS_TIMEOUT = 30
ALPHAESS_RETRIES = 3
# The signature is only valid within 300 seconds of server time.
ALPHAESS_SIGN_WINDOW_SECONDS = 300

# Tier TTLs in minutes.
ALPHAESS_TTL_STATIC = 8 * 60
ALPHAESS_TTL_CONFIG = 30
ALPHAESS_TTL_POWER = 1
ALPHAESS_TTL_ENERGY = 5
# Power tier interval once a serial has been demoted to the history path - matches the
# resolution getOneDayPowerBySn actually has, and keeps the ~288-record payload off a
# 60-second loop.
ALPHAESS_TTL_POWER_DEMOTED = 5
# Consecutive getLastPowerData failures before a serial is demoted to history.
ALPHAESS_LIVE_FAIL_LIMIT = 3

ALPHAESS_STORAGE_MODULE = "alphaess"
ALPHAESS_CACHE_STATIC = "static"
ALPHAESS_CACHE_CONFIG = "config"
ALPHAESS_CACHE_RATINGS = "ratings"
ALPHAESS_CACHE_CONTROL = "control"

# Polls a written payload is considered pending for before an unexpected read-back counts
# as external interference (the phone app, or another Predbat instance).
ALPHAESS_SETTLE_POLLS = 3

# How long after a SUCCESSFUL write a further difference still counts as the rest of the
# SAME schedule update, and is therefore let through alphaess_min_write_interval.
#
# Predbat commits a schedule in STAGES - charge window, then the enable switch, then the
# target SoC - pressing the schedule write button after each one (INVERTER_DEF
# time_button_press). The first commit of a cycle therefore carries whatever target SoC the
# control entity still holds from the previous cycle, and the corrected value only arrives a
# few seconds later. Pacing that correction leaves the inverter running a schedule Predbat
# has already superseded - GH#4769, where a manual charge for the live slot went out as
# chargeLimit 10 and the corrected 100 was held for the full 300s, with the house on the grid.
#
# Comfortably longer than one inverter write sequence and far shorter than Predbat's
# five-minute run cadence, so two separate cycles can never merge into a single burst.
ALPHAESS_WRITE_SETTLE_SECONDS = 60
# Hard ceiling on the writes one settle burst may spend. Keeps the exemption a correction
# path rather than a write loop that escapes pacing entirely: the cost against the documented
# 24-hour write budget stays a small constant per pacing interval instead of being unbounded.
ALPHAESS_WRITE_BURST_MAX = 3

ALPHAESS_DEBUG_REDACT_KEYS = ("appSecret", "sign", "app_secret", "code", "checkCode")
ALPHAESS_DEBUG_REDACT_KEYS_RESPONSE = ("appSecret", "sign", "app_secret", "checkCode")

# getLastPowerData field -> published sensor leaf. Watts, except soc which is a percent.
ALPHAESS_TELEMETRY = {
    "soc": "soc",
    "battery_power": "pbat",
    "grid_power": "pgrid",
    "pv_power": "ppv",
    "load_power": "pload",
    "ev_power": "pev",
}

# pgrid is positive on IMPORT; Predbat wants negative on import, so this one is negated
# on publish. Listed explicitly rather than inferred so the intent survives a refactor.
ALPHAESS_TELEMETRY_NEGATE = ("grid_power",)

# Daily kWh counters. load_today comes from getSumDataForCustomer (eload); the rest come
# from getOneDateEnergyBySn. getOneDateEnergyBySn has no load field at all.
ALPHAESS_ENERGY = {
    "import_today": "eInput",
    "export_today": "eOutput",
    "pv_today": "epv",
    "battery_charge_today": "eCharge",
    "battery_discharge_today": "eDischarge",
    "grid_charge_today": "eGridCharge",
    # EV charger energy. Mapped to Predbat's car_charging_energy in automatic_config,
    # but only for serials that actually have a charger fitted - see ALPHAESS_EV_DETAIL.
    "ev_energy_today": "eChargingPile",
}
ALPHAESS_ENERGY_LOAD_FIELD = "eload"

# getOneDayPowerBySn sample field -> sensor leaf, used when getLastPowerData is unavailable.
# cbat carries SOC. The portal documents it as "cobat" and the live API returns "cbat" -
# reading the portal name silently yields None, which looks exactly like "no SOC here", so
# both spellings are tried, cbat first.
ALPHAESS_HISTORY = {
    "soc": ("cbat", "cobat"),
    "pv_power": ("ppv",),
    "load_power": ("load",),
    # The history endpoint spells the EV charger power differently from the live one
    # (pchargingPile here, pev there). Without this a demoted serial would silently
    # lose its ev_power sensor, which is exactly the trap battery_power fell into.
    "ev_power": ("pchargingPile",),
}
# Per-charger power keys inside getLastPowerData's pevDetail. The API documents these
# as "null when no charger is fitted", so a non-null value is the one documented
# signal that a charger physically exists - pev itself reads 0 either way.
ALPHAESS_EV_DETAIL = ("ev1Power", "ev2Power", "ev3Power", "ev4Power")

ALPHAESS_HISTORY_FEED_IN = "feedIn"
ALPHAESS_HISTORY_GRID_CHARGE = "gridCharge"

# Ships EMPTY on purpose. AlphaESS model names do not encode coupling the way GivEnergy's
# do (there is no "ac" substring to match), and the Home Assistant integration's known
# model list contains no confirmed AC-coupled entry. Inventing a table here would be a
# guess; entries go in only as testers confirm them.
ALPHAESS_AC_COUPLED_MODELS = []

ALPHAESS_TIME_STEP_MINUTES = 15
ALPHAESS_TIME_MAX = "23:45"
ALPHAESS_TIME_DISABLED = "00:00"


def hhmmss_to_hhmm(value):
    """Convert Predbat's HH:MM:SS control-entity value to the API's HH:mm.

    Returns "00:00" for anything unusable, which is the documented "disabled" value, so a
    missing or malformed entity disables a window rather than raising inside the poll loop.
    """
    text = str(value or "").strip()
    if not text:
        return ALPHAESS_TIME_DISABLED
    parts = text.split(":")
    if len(parts) < 2:
        return ALPHAESS_TIME_DISABLED
    try:
        return "{:02d}:{:02d}".format(int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        return ALPHAESS_TIME_DISABLED


def hm_to_minutes(hm):
    """Convert HH:mm to minutes since midnight, returning 0 for anything unusable."""
    text = hhmmss_to_hhmm(hm)
    hours, _, mins = text.partition(":")
    try:
        return int(hours) * 60 + int(mins)
    except (TypeError, ValueError):
        return 0


def snap_time_grid(value, direction):
    """Snap HH:mm onto the API's 15-minute grid, INWARD.

    Start times round up and end times round down, so a snapped window is never wider than
    the one Predbat asked for. Off-grid values are accepted by the API and then silently
    ignored by the inverter, so getting this wrong produces a window that appears to have
    been written and never runs - hence snapping here rather than trusting the caller.

    23:45 is the documented maximum, so a 24:00 end (Predbat's midnight) lands on 23:45.
    """
    minutes = hm_to_minutes(value)
    step = ALPHAESS_TIME_STEP_MINUTES
    if direction == "start":
        snapped = ((minutes + step - 1) // step) * step
    else:
        snapped = (minutes // step) * step
    max_minutes = hm_to_minutes(ALPHAESS_TIME_MAX)
    snapped = max(0, min(snapped, max_minutes))
    return "{:02d}:{:02d}".format(snapped // 60, snapped % 60)


def window_is_empty(start, end):
    """Return True when a window carries no time the inverter would act on.

    Covers both the documented "disabled" form (start == end) and a window that snapping
    has collapsed or inverted. An inverted window must NOT be written as a wrap-around:
    wrap behaviour is undocumented, so it is disabled instead.
    """
    return hm_to_minutes(end) <= hm_to_minutes(start)
