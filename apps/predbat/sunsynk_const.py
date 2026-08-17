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

# VERIFY@SPIKE — cell-count rounding in SunsynkAPI.nominal_pack_voltage assumes every real
# pack is charged to (close enough to) 3.55V/cell that round(chargeVolt / 3.55) recovers the
# true cell count. It is exact for the values tested (56.8, 85.2, 113.6V) because those are
# exact multiples of 3.55, but a legitimate pack charged to a different per-cell target -
# e.g. 24 cells at 3.45V/cell = 82.8V - rounds to 23 cells instead of 24, silently giving a
# nominal voltage, capacity and battery_rate_max about 4% wrong. No live hardware has
# confirmed the real spread of charge targets in use. The remote tester should compare the
# derived battery_capacity against the battery's actual rated kWh before trusting it.

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
