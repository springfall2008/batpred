# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sunsynk Cloud API constants
# -----------------------------------------------------------------------------

"""Sunsynk Connect API constants and the RSA login helper.

Sunsynk publishes no API documentation, so values marked ``# VERIFY@SPIKE`` are
inferred from third-party clients (solarsynkv3 and synkctl) and have not been
confirmed against live hardware. Per the design spec, DEYE is assumed for
*semantics* (the six sequential time-of-use slots, the three work modes and what
they do) but never for *encoding* — every field name and wire value here comes
from a Sunsynk source. All component logic imports from this module so a
correction needs no downstream edits.
"""

SUNSYNK_REGIONS = {
    "sunsynk": {"host": "https://api.sunsynk.net", "source": "sunsynk"},
    "inteless": {"host": "https://pv.inteless.com", "source": "elinter"},
}

SUNSYNK_ENDPOINTS = {
    "public_key": "/anonymous/publicKey",
    "token": "/oauth/token/new",
    "token_legacy": "/oauth/token",
    "inverter_list": "/api/v1/inverters",
    "inverter_detail": "/api/v1/inverter/{sn}",
    "battery": "/api/v1/inverter/battery/{sn}/realtime",
    "grid": "/api/v1/inverter/grid/{sn}/realtime",
    "load": "/api/v1/inverter/load/{sn}/realtime",
    "input": "/api/v1/inverter/{sn}/realtime/input",
    "settings_read": "/api/v1/common/setting/{sn}/read",
    "settings_set": "/api/v1/common/setting/{sn}/set",
}

SUNSYNK_TIMEOUT = 30
SUNSYNK_RETRIES = 3
SUNSYNK_PAGE_SIZE = 10
SUNSYNK_CLIENT_ID = "csp-web"

TOU_SLOT_COUNT = 6
FREEZE_EXPORT_SOC = 99

# Distinct ascending start times used to pad a schedule out to TOU_SLOT_COUNT.
# Sunsynk's slots are sequential intervals ("from this start until the next slot's
# start"), so every start must be unique — duplicates create zero-length intervals.
# Seven options guarantee TOU_SLOT_COUNT distinct times survive even if all four
# of a schedule's own window boundaries collide with fillers.
TOU_FILLER_TIMES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00", "23:00"]

# VERIFY@SPIKE — that Sunsynk has these three modes is a semantic claim inherited
# from DEYE and is safe. That they are numbered 0/1/2 IN DEYE'S ORDER is an
# ENCODING claim and is the single highest-cost unknown in this integration:
# getting it wrong silently swaps export for charge. Confirm before enabling control.
SUNSYNK_WORKMODE = {
    "selling_first": "0",
    "zero_export_load": "1",
    "zero_export_ct": "2",
}

# Per-slot field name templates, rendered with n = 1..TOU_SLOT_COUNT.
TOU_FIELD = {
    "time": "sellTime{n}",
    "power": "sellTime{n}Pac",
    "soc": "cap{n}",
    "grid_charge": "time{n}on",
}

# Slot fields Predbat does NOT own and must preserve verbatim from the read.
TOU_FIELD_PRESERVED = ["genTime{n}on", "sellTime{n}Volt"]

SUNSYNK_DAY_FIELDS = ["mondayOn", "tuesdayOn", "wednesdayOn", "thursdayOn", "fridayOn", "saturdayOn", "sundayOn"]

# Top-level settings keys Predbat owns.
SUNSYNK_WORKMODE_FIELD = "sysWorkMode"
SUNSYNK_SOLAR_SELL_FIELD = "solarSell"
SUNSYNK_TOU_ENABLE_FIELD = "peakAndVallery"
SUNSYNK_SERIAL_FIELD = "sn"

# VERIFY@SPIKE — solarsynkv3 carries a ReplaceTRUE() helper that rewrites the string
# "true" to a bare true before posting, which is strong evidence the API needs real
# JSON booleans for the per-slot and day flags while numeric fields stay quoted
# strings. Declared per field here rather than guessed at each call site.
SUNSYNK_BOOL_FIELDS = frozenset([TOU_FIELD["grid_charge"].format(n=n) for n in range(1, TOU_SLOT_COUNT + 1)] + SUNSYNK_DAY_FIELDS)

# Values that mean False when Sunsynk hands a flag back as a string.
SUNSYNK_FALSE_STRINGS = frozenset(["false", "0", "", "none", "off", "no"])


def encode_setting(name, value):
    """Serialise one settings value the way Sunsynk expects it on the wire.

    Boolean fields (per-slot grid charge, day-of-week enables) go bare; every other
    field is quoted, because Sunsynk returns and accepts its numerics as strings.
    """
    if name in SUNSYNK_BOOL_FIELDS:
        if isinstance(value, str):
            return value.strip().lower() not in SUNSYNK_FALSE_STRINGS
        return bool(value)
    return str(value)


# Telemetry: Predbat sensor leaf -> (endpoint key, response field).
SUNSYNK_TELEMETRY = {
    "soc": ("battery", "soc"),
    "battery_power": ("battery", "power"),
    "battery_voltage": ("battery", "voltage"),
    "temperature": ("battery", "temp"),
    "grid_power": ("grid", "pac"),
    "load_power": ("load", "totalPower"),
    "pv_power": ("input", "pac"),
}

# Daily energy counters: Predbat arg -> (endpoint key, response field).
SUNSYNK_ENERGY = {
    "pv_today": ("input", "etoday"),
    "import_today": ("grid", "etodayFrom"),
    "export_today": ("grid", "etodayTo"),
    "load_today": ("load", "dailyUsed"),
    "battery_charge_today": ("battery", "etodayChg"),
    "battery_discharge_today": ("battery", "etodayDischg"),
}

# VERIFY@SPIKE — sign convention. DEYE reports battery power positive on discharge;
# if Sunsynk agrees this stays empty, otherwise add "battery_power" here.
SUNSYNK_TELEMETRY_NEGATE = ()

# Fields used to derive ratings rather than published directly.
SUNSYNK_CAPACITY_AH_FIELD = "capacity"  # battery realtime, amp-hours
SUNSYNK_PACK_VOLTAGE_FIELD = "voltage"  # battery realtime, live pack volts
SUNSYNK_CHARGE_VOLT_FIELD = "chargeVolt"  # battery realtime, BMS charge target
SUNSYNK_MAX_CHARGE_CURRENT_FIELD = "maxChargeCurrentLimit"  # battery realtime, amps
SUNSYNK_RATED_POWER_FIELD = "ratePower"  # inverter detail, watts
SUNSYNK_BATTERY_LOW_CAP_FIELD = "batteryLowCap"  # settings, percent floor

# LiFePO4 cell voltages used to infer the pack's nominal voltage from its BMS charge
# target, so an amp-hour capacity can become kWh. Same derivation deye.py uses.
LIFEPO4_CHARGE_VOLTS_PER_CELL = 3.55
LIFEPO4_NOMINAL_VOLTS_PER_CELL = 3.2

# Refresh cadence per class of state, in minutes. ComponentBase ticks run() every 60
# seconds; these are the maximum ages a cached tier may reach before it is re-polled.
SUNSYNK_TTL_STATIC = 8 * 60  # inverter list and detail — changes when hardware does
SUNSYNK_TTL_CONFIG = 15  # the settings object — installer settings, effectively static
SUNSYNK_TTL_LIVE = 5  # telemetry; four endpoint calls per inverter, so slower than DEYE's

# Restore bound in minutes for the applied-payload cache. It is a change-detection cache
# with no read-back, so restoring it asserts the inverter still holds what Predbat last
# wrote. If it was changed externally while Predbat was down that assertion is false, the
# next write is wrongly SKIPPED and the battery silently diverges from the plan. A
# redundant write is cheap; a skipped one is not.
SUNSYNK_RESTORE_MAX_CONTROL = 15

# Cycles a written payload may remain absent from the read-back before warning. Sunsynk
# acknowledges a write at the cloud, but the dongle only collects it on its next poll —
# typically one to five minutes — so divergence within this bound is normal latency.
SUNSYNK_SETTLE_POLLS = 3

SUNSYNK_STORAGE_MODULE = "sunsynk"
SUNSYNK_CACHE_STATIC = "static"  # inverter serials, detail
SUNSYNK_CACHE_CONFIG = "config"  # last-read settings object
SUNSYNK_CACHE_RATINGS = "ratings"  # derived capacity, pack voltage, rated power
SUNSYNK_CACHE_CONTROL = "control"  # last-applied payload for change detection

# Sunsynk answers an expired token with HTTP 200 carrying a body-level failure, so
# status-code-only handling never triggers a refresh and the component stays broken
# until restart. Matched lower-cased against the body's msg. Keep these narrow enough
# that a genuine non-auth failure is never retried as one.
SUNSYNK_AUTH_ERROR_MARKERS = (
    "invalid token",
    "token invalid",
    "token expired",
    "token is expired",
    "expired token",
    "unauthorized",
    "unauthorised",
    "auth failed",
    "not logged in",
)

# Maximum characters of a request/response body written to the log when debug tracing.
SUNSYNK_DEBUG_MAX_CHARS = 20000

# Body keys redacted from debug traces: these carry credentials or bearer tokens and
# the logs are routinely pasted into issue reports.
SUNSYNK_DEBUG_REDACT_KEYS = ("password", "access_token", "refresh_token", "token", "Authorization", "sign")
