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

import base64
import os

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

# Hard ceiling on inverter-list discovery pages (SunsynkAPI.get_device_list), independent
# of the server-reported `total` field and of whether each page keeps contributing serials
# not seen before. `total` is entirely server-controlled and never otherwise sanity-checked,
# so a corrupt or absurd value must not be able to hammer the API indefinitely. At
# SUNSYNK_PAGE_SIZE per page this covers 5000 inverters - far beyond any real installation -
# while bounding the worst case to hundreds rather than millions of HTTP calls. Hitting it
# is logged as a warning so a genuinely large account is diagnosable rather than silently
# truncated.
SUNSYNK_MAX_DISCOVERY_PAGES = 500

TOU_SLOT_COUNT = 6
FREEZE_EXPORT_SOC = 99

# Distinct ascending start times used to pad a schedule out to TOU_SLOT_COUNT.
#
# CONFIRMED by Sunsynk's own documentation ("Avoiding conflicts in the System Mode timer"):
# the six slots are sequential chronological intervals, each running until the next slot
# begins, and "Timers MUST be set chronologically from Timer 1 to Timer 6". Timer 6 is the
# only one permitted to roll over midnight and continue until Timer 1 restarts. So distinct
# ascending starts are a real requirement, not a Predbat preference.
#
# A slot is an interval regardless of its grid-charge flag: the documented factory default
# runs all six timers as ranges with Grid Charge ticked on only two of them. That is what
# makes Predbat's padding work - a filler slot with grid charge off still TERMINATES the
# charge window before it.
#
# Note an inverter can nonetheless be found sitting with all six slots at 00:00 (an
# unconfigured default). The API stores that happily; it is simply not a chronological
# programme, so no conclusion about valid schedules should be drawn from it.
#
# Seven options guarantee TOU_SLOT_COUNT distinct times survive even if all four of a
# schedule's own window boundaries collide with fillers.
TOU_FILLER_TIMES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00", "23:00"]

# CONFIRMED live (inverter 2405116013, 2026-08-18). The Sunsynk app's System Mode screen
# lists Work Mode as Selling First / Zero-Export + Limit To Load Only / Limited to Home, and
# with "Limited to Home" selected the settings object reported sysWorkMode '2' - so the
# numbering follows the app's own order, which is also DEYE's. "Limited to Home" is
# Sunsynk's name for zero-export-to-CT.
#
# The mode does NOT by itself decide whether the system exports: the separate Solar Export
# toggle (solarSell) does. The same inverter exported 11.1 kWh on the day this was confirmed
# while sitting in "Limited to Home", because solarSell was '1'.
SUNSYNK_WORKMODE = {
    "selling_first": "0",
    "zero_export_load": "1",
    "zero_export_ct": "2",
}

# Per-slot field name templates, rendered with n = 1..TOU_SLOT_COUNT.
# Per-slot fields Predbat writes. CONFIRMED live (2026-08-19) that all of "sell",
# "grid_charge" and their siblings must be present in the payload together: with
# sellTime{n}En absent, time{n}on was silently discarded on six consecutive writes across
# every encoding tried, while every other field of the same write persisted. Including it
# made time{n}on stick immediately. The API evidently validates the per-slot field set as a
# whole and drops the flags if it is incomplete.
#
# The three flags are INDEPENDENT once all are present - proven by setting grid charge on a
# slot whose sell flag is 0, and the sell flag on a slot whose grid charge is 0, in one
# write: both landed exactly as sent.
TOU_FIELD = {
    "time": "sellTime{n}",
    "power": "sellTime{n}Pac",
    "soc": "cap{n}",
    "grid_charge": "time{n}on",
    "sell": "sellTime{n}En",
}

# NEVER write this. time{n}On (capital O) is server-derived: it changed from '0' to '65' on
# a write that did not mention it at all, and writing '1' to it also produced '65'. It is
# not the boolean it resembles, and the writable grid-charge flag is time{n}on (lower case).
SUNSYNK_DERIVED_SLOT_FIELDS = tuple(f"time{n}On" for n in range(1, TOU_SLOT_COUNT + 1))

SUNSYNK_DAY_FIELDS = ["mondayOn", "tuesdayOn", "wednesdayOn", "thursdayOn", "fridayOn", "saturdayOn", "sundayOn"]

# The settings/set endpoint accepts ONLY the "System Mode" group of fields. CONFIRMED
# live (inverter 2405116013, 2026-08-19): posting the full 350-key object returned
# {"code":0,"msg":"Success","success":true} and changed NOTHING, twice, including a probe
# that altered a single field and preserved every original string type. Posting just these
# 53 keys with the same single change persisted immediately.
#
# So the whole-object read-modify-write this component originally used could never have
# worked - every write was silently accepted and discarded. Predbat now sends this group
# only, carrying through the fields inside it that it does not own (safetyType, battMode,
# energyMode, zeroExportPower, solarMaxSellPower, pvMaxLimit, sellTime{n}Volt,
# genTime{n}on). Everything outside the group - battery, grid, generator settings - is
# never transmitted at all, so it cannot be disturbed.
#
# Field list taken from solarsynkv3's DetermineSettingCategory, which posts the same group.
SUNSYNK_SYSTEM_MODE_FIELDS = frozenset(
    ["sn", "safetyType", "battMode", "solarSell", "pvMaxLimit", "energyMode", "peakAndVallery", "sysWorkMode", "zeroExportPower", "solarMaxSellPower"]
    + [f"sellTime{n}" for n in range(1, TOU_SLOT_COUNT + 1)]
    + [f"sellTime{n}Pac" for n in range(1, TOU_SLOT_COUNT + 1)]
    + [f"sellTime{n}Volt" for n in range(1, TOU_SLOT_COUNT + 1)]
    + [f"sellTime{n}En" for n in range(1, TOU_SLOT_COUNT + 1)]
    + [f"cap{n}" for n in range(1, TOU_SLOT_COUNT + 1)]
    + ["mondayOn", "tuesdayOn", "wednesdayOn", "thursdayOn", "fridayOn", "saturdayOn", "sundayOn"]
    + [f"time{n}on" for n in range(1, TOU_SLOT_COUNT + 1)]
    + [f"genTime{n}on" for n in range(1, TOU_SLOT_COUNT + 1)]
)

# Top-level settings keys Predbat owns.
SUNSYNK_WORKMODE_FIELD = "sysWorkMode"
SUNSYNK_SOLAR_SELL_FIELD = "solarSell"
SUNSYNK_TOU_ENABLE_FIELD = "peakAndVallery"
SUNSYNK_SERIAL_FIELD = "sn"

# CONFIRMED live (inverter 2405116013, 2026-08-19) that these flags must be sent as the
# STRINGS "true"/"false", not as bare JSON booleans.
#
# A write carrying bare booleans was accepted and every other field of it persisted - slot
# times, target SoCs and powers all landed - while time1on and time2on alone were silently
# discarded and read back at their previous values. Sending them quoted, exactly as the read
# returns them, makes them stick.
#
# This is the opposite of what solarsynkv3's ReplaceTRUE() helper implied. That was the
# original basis for guessing bare booleans, and it was wrong.
# sellTime{n}En is deliberately NOT here: it is written as the numeric string "1"/"0",
# which is how the API returns it, unlike time{n}on which uses "true"/"false".
SUNSYNK_BOOL_FIELDS = frozenset([TOU_FIELD["grid_charge"].format(n=n) for n in range(1, TOU_SLOT_COUNT + 1)] + SUNSYNK_DAY_FIELDS)

# Values that mean False when Sunsynk hands a flag back as a string.
SUNSYNK_FALSE_STRINGS = frozenset(["false", "0", "", "none", "off", "no"])


def encode_setting(name, value):
    """Serialise one settings value the way Sunsynk expects it on the wire.

    Everything is quoted. The boolean fields (per-slot grid charge, day-of-week enables)
    become the strings "true"/"false" rather than bare JSON booleans: the API silently
    discards a bare boolean while accepting the rest of the same write. See
    SUNSYNK_BOOL_FIELDS.
    """
    if name in SUNSYNK_BOOL_FIELDS:
        truthy = value.strip().lower() not in SUNSYNK_FALSE_STRINGS if isinstance(value, str) else bool(value)
        return "true" if truthy else "false"
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

# Metrics whose sign must be flipped to reach Predbat's convention (battery positive on
# discharge, grid negative on import).
#
# battery_power CONFIRMED live: the app's power-flow diagram showed 478 W flowing
# battery -> house with PV at 0 W while the API reported power +484, so positive already
# means discharging, matching DEYE and Predbat. No flip.
#
# grid_power CONFIRMED live (inverter 2405116013, 2026-08-20 11:25) and negated, which
# settles the VERIFY@SPIKE this line used to carry: Sunsynk reports `pac` NEGATIVE when
# exporting, so it must be flipped to reach Predbat's negative-for-import convention. The
# DEYE-parity guess was right after all.
#
# The sample that settles it, and the trap to avoid repeating: pac -1939 W at a moment when
# PV 2708 W served a 117 W load and charged the battery at 544 W - a 2047 W surplus, whose
# magnitude matches - with etodayFrom (import) sitting at 0.0 kWh for the whole day against
# etodayTo (export) at 2.8 kWh and climbing. Only a sample with real power flowing can show
# this. Two earlier readings taken near the balance point (pac +19 W and +20 W against a
# computed surplus of ~0 W) look like the opposite conclusion and are pure noise; do not
# re-decide this from a sample under a few hundred watts.
#
# `pac` is also the right FIELD: it read 0 W at the same moment the app did, while grid
# vip[0].power read -418 W and is evidently something else (a CT or per-phase sense, not
# whole-house flow).
SUNSYNK_TELEMETRY_NEGATE = ("grid_power",)

# Fields used to derive ratings rather than published directly.
SUNSYNK_CAPACITY_AH_FIELD = "capacity"  # battery realtime, amp-hours
SUNSYNK_CHARGE_VOLT_FIELD = "chargeVolt"  # battery realtime, BMS charge target
SUNSYNK_RATED_POWER_FIELD = "ratePower"  # inverter detail, watts
SUNSYNK_BATTERY_LOW_CAP_FIELD = "batteryLowCap"  # settings, percent floor

# Output power cap. CONFIRMED live that this can sit BELOW the hardware rating: ratePower
# 8000 with pvMaxLimit 7000, so using the rating alone would have Predbat plan a kilowatt
# the inverter will never deliver.
#
# The EXPORT limit. The Sunsynk app shows this same value under two labels with an identical
# 500-16000W range - "Inverter Power Limiter" (System Mode) and "Export power limiter" (Grid
# Settings) - which is why pvMaxLimit was the only field holding the 7000 both screens
# displayed. Per Sunsynk's documentation (confirmed by the system owner) the control despite
# its System Mode label caps EXPORT, not the inverter's AC output: the inverter can still
# deliver its full ratePower to the house. So this backs export_limit, and inverter_limit
# stays on ratePower.
#
# Not to be confused with solarMaxSellPower (SUNSYNK_MAX_SOLAR_FIELD), a separate setting.
SUNSYNK_EXPORT_LIMIT_FIELD = "pvMaxLimit"  # settings, watts

# Grid import cap - "Import power limiter" in the app's Grid Settings. CONFIRMED live:
# app 10350 W, settings importPower '10350'. Not consumed yet; recorded so the mapping is
# not lost, since it bounds how fast Predbat can charge from the grid.
SUNSYNK_IMPORT_LIMIT_FIELD = "importPower"  # settings, watts

# "Max Solar Power" in the app - CONFIRMED live by the system owner: app 9200, settings
# solarMaxSellPower '9200'. Recorded rather than consumed: it is a PV-side cap, not the AC
# export cap Predbat's export_limit wants (see automatic_config for why that is left unset).
SUNSYNK_MAX_SOLAR_FIELD = "solarMaxSellPower"  # settings, watts

# Charge-current limit candidates, in priority order: the FIRST field with a positive value
# wins. CONFIRMED live that a real system reports maxChargeCurrentLimit 0.0 while
# chargeCurrentLimit 216.0 carries the actual limit - reading only the max field derived
# battery_rate_max as 0, so automatic_config skipped it and Predbat ran with no charge-rate
# limit at all. Both names are kept because it is unknown which other firmware populates.
SUNSYNK_CHARGE_CURRENT_FIELDS = ("chargeCurrentLimit", "maxChargeCurrentLimit")

# LiFePO4 pack geometry, used to turn an amp-hour capacity into kWh.
#
# Cell count is inferred from the BMS charge target. Dividing by one assumed volts-per-cell
# is wrong at both ends of the legitimate range: 3.55 turns a 24-cell pack charged at
# 3.65V/cell into 25 cells, and 3.65 turns a 16-cell pack charged at 3.45V/cell into 15.
# Either error is silent, about 4%, and lands in soc_max. Instead the standard stack sizes
# are tried and the one whose implied volts-per-cell falls inside the charge window wins,
# breaking ties toward the typical value - exact for every combination of these stack sizes
# with a 3.45-3.65V/cell target.
#
# CONFIRMED live: chargeVolt 58.4 -> 16 cells at exactly 3.65V/cell -> 51.2V nominal ->
# 200Ah = 10.24 kWh, matching the pack's rating and its bmsVolt of 52.3V at 42% SoC.
LIFEPO4_CELL_COUNTS = (8, 15, 16, 24, 32)
LIFEPO4_CHARGE_VOLTS_MIN = 3.40
LIFEPO4_CHARGE_VOLTS_MAX = 3.75
LIFEPO4_CHARGE_VOLTS_TYPICAL = 3.55
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


def _read_tlv(data, offset):
    """Read one DER tag-length-value at offset, returning (tag, value, next_offset).

    Raises ValueError if the tag/length header or the declared value would read past
    the end of ``data``. Python slicing truncates silently instead of raising, so
    without this check a truncated DER key would "parse" successfully with a
    corrupted trailing field — see ``parse_rsa_public_key`` for why that matters.
    """
    if offset + 2 > len(data):
        raise ValueError("Sunsynk DER TLV header runs past the end of the data")
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        if offset + count > len(data):
            raise ValueError("Sunsynk DER TLV long-form length runs past the end of the data")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    if offset + length > len(data):
        raise ValueError("Sunsynk DER TLV declares more data than is present")
    return tag, data[offset : offset + length], offset + length


def parse_rsa_public_key(der_bytes):
    """Extract (modulus, exponent) from a DER SubjectPublicKeyInfo RSA public key.

    Sunsynk's /anonymous/publicKey returns the key base64-encoded with no PEM armour,
    so it is decoded and walked directly: SEQUENCE { AlgorithmIdentifier, BIT STRING {
    RSAPublicKey SEQUENCE { INTEGER modulus, INTEGER exponent } } }. Raises ValueError
    on anything that is not that structure rather than returning a bogus key — a
    silently wrong modulus would encrypt the password to something unrecoverable.
    """
    if not der_bytes:
        raise ValueError("Sunsynk public key response was empty")
    tag, spki, _ = _read_tlv(der_bytes, 0)
    if tag != 0x30:
        raise ValueError("Sunsynk public key is not a DER SEQUENCE")
    tag, _algorithm, offset = _read_tlv(spki, 0)
    if tag != 0x30:
        raise ValueError("Sunsynk public key has no AlgorithmIdentifier")
    tag, bit_string, _ = _read_tlv(spki, offset)
    if tag != 0x03:
        raise ValueError("Sunsynk public key has no BIT STRING")
    # The BIT STRING's first byte counts unused trailing bits and is always 0 here.
    tag, rsa_key, _ = _read_tlv(bit_string[1:], 0)
    if tag != 0x30:
        raise ValueError("Sunsynk public key BIT STRING is not an RSAPublicKey")
    tag, modulus, offset = _read_tlv(rsa_key, 0)
    if tag != 0x02:
        raise ValueError("Sunsynk public key has no modulus")
    tag, exponent, _ = _read_tlv(rsa_key, offset)
    if tag != 0x02:
        raise ValueError("Sunsynk public key has no exponent")
    return int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


def rsa_encrypt_pkcs1v15(public_key_b64, plaintext):
    """RSA-encrypt plaintext with PKCS#1 v1.5 type-2 padding, returning base64 ciphertext.

    This replaces a `cryptography` dependency, which would add a Rust-built binary wheel
    to every architecture the add-on targets. Only public-key encryption happens here —
    there is no private key and no secret-dependent branch, so the usual cautions about
    hand-rolled crypto (timing side channels, padding oracles) do not apply.
    """
    modulus, exponent = parse_rsa_public_key(base64.b64decode(public_key_b64))
    size = (modulus.bit_length() + 7) // 8
    message = plaintext.encode("utf-8")
    # PKCS#1 v1.5 needs 3 framing bytes and at least 8 padding bytes.
    if len(message) > size - 11:
        raise ValueError(f"Sunsynk password is too long for a {size * 8} bit key")
    needed = size - len(message) - 3
    padding = bytearray()
    # Padding must be non-zero: a zero byte would be read as the message separator and
    # truncate the password. Rejection-sample until enough non-zero bytes are collected.
    while len(padding) < needed:
        padding.extend(byte for byte in os.urandom(needed) if byte)
    block = b"\x00\x02" + bytes(padding[:needed]) + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), exponent, modulus)
    return base64.b64encode(cipher.to_bytes(size, "big")).decode("ascii")
