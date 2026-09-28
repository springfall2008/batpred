# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# EcoFlow IoT Developer Platform constants
# -----------------------------------------------------------------------------

"""Constants and pure helpers for the EcoFlow IoT Developer Platform component.

Kept separate from ecoflow.py so the request signature - the one part of this integration
that can be proved correct without hardware, because the docs publish a worked vector -
can be tested without constructing a component.

WHAT IS CONFIRMED AND WHAT IS NOT
---------------------------------
Confirmed from the published documentation: the host, the five endpoint paths, the header
set, the signing algorithm (proved against ECOFLOW_SIGN_VECTOR below), the MQTT broker and
topic shapes, and the set_reply codes.

NOT confirmed, because nobody on the project has an EcoFlow account or a Power Ocean:
every telemetry key name in ECOFLOW_TELEMETRY, and every write command id. Those are
flagged individually below. ECOFLOW_SCHEDULE_COMMANDS deliberately ships EMPTY rather than
carrying a guess - see its comment.
"""

import hashlib
import hmac

ECOFLOW_BASE_URL = "https://api.ecoflow.com"

# Documented paths. Note the method matters as much as the path here: /device/quota is a
# PUT to write and a POST to read a named subset, which is why the endpoint map keys on the
# operation rather than on the path.
ECOFLOW_ENDPOINTS = {
    "device_list": "/iot-open/sign/device/list",
    "quota_all": "/iot-open/sign/device/quota/all",
    "quota_get": "/iot-open/sign/device/quota",
    "quota_set": "/iot-open/sign/device/quota",
    "certification": "/iot-open/sign/certification",
}

ECOFLOW_METHODS = {
    "device_list": "GET",
    "quota_all": "GET",
    "quota_get": "POST",
    "quota_set": "PUT",
    "certification": "GET",
}

# The nonce is documented as six random digits. Stated as a constant because the signature
# covers the nonce verbatim, so a mismatch between the header and the signed string is
# indistinguishable from a bad secret key.
ECOFLOW_NONCE_DIGITS = 6

# The REST envelope's success code. EcoFlow return it as the STRING "0" in the published
# examples, while the MQTT set_reply carries an integer 0, so both forms are accepted
# wherever a code is compared - see ecoflow.py:_code_is_ok.
ECOFLOW_CODE_OK = "0"

# Documented set_reply codes. These are the MQTT reply codes; the REST envelope reuses 0
# for success and otherwise carries its own message text rather than a code from this table.
ECOFLOW_REPLY_OK = 0
ECOFLOW_REPLY_NOT_OWNED = -1
ECOFLOW_REPLY_OFFLINE = -2
ECOFLOW_REPLY_CODES = {
    0: "Success",
    -1: "The device does not belong to this account",
    -2: "The device is offline",
}

ECOFLOW_TIMEOUT = 30
ECOFLOW_RETRIES = 3

# MQTT push telemetry. Not used by this component yet - telemetry is polled over HTTP - but
# the credentials endpoint and the topic shapes are declared here so the push path can be
# added without moving the wire format around. The broker is TLS-only.
ECOFLOW_MQTT_HOST = "mqtt.ecoflow.com"
ECOFLOW_MQTT_PORT = 8883
ECOFLOW_MQTT_SCHEME = "mqtts"
ECOFLOW_MQTT_TOPICS = {
    "quota": "/open/{account}/{sn}/quota",
    "set": "/open/{account}/{sn}/set",
    "set_reply": "/open/{account}/{sn}/set_reply",
    "get": "/open/{account}/{sn}/get",
    "get_reply": "/open/{account}/{sn}/get_reply",
    "status": "/open/{account}/{sn}/status",
}

# The worked example the developer docs publish, kept here so the signing test asserts
# against the documented figures rather than against a hash this code produced. If
# sign_request() ever stops reproducing this hash the implementation is wrong, not the test.
ECOFLOW_SIGN_VECTOR = {
    "access_key": "Fp4SvIprYSDPXtYJidEtUAd1o",  # cspell:disable-line
    "secret_key": "WIbFEKre0s6sLnh4ei7SPUeYnptHG6V",  # cspell:disable-line
    "nonce": "345164",
    "timestamp": "1671171709428",
    "params": {"sn": "123456789", "params": {"cmdSet": 11, "id": 24, "eps": 0}},
    "string": "params.cmdSet=11&params.eps=0&params.id=24&sn=123456789&accessKey=Fp4SvIprYSDPXtYJidEtUAd1o&nonce=345164&timestamp=1671171709428",  # cspell:disable-line
    "sign": "07c13b65e037faf3b153d51613638fa80003c4c38d2407379a7f52851af1473e",
}

# Product lines this component is aimed at. Used only to log which of a BYOK account's
# devices look like a home battery, never to filter: an unrecognised productName still gets
# a quota poll, and whether Predbat can use the device is then decided by the telemetry it
# actually returns. Deciding on a name table is how a new product silently stops working.
ECOFLOW_HOME_BATTERY_HINTS = ("powerocean", "power ocean", "stream")

# ---------------------------------------------------------------------------
# UNVERIFIED AGAINST HARDWARE - every key name below is a candidate, not a fact
# ---------------------------------------------------------------------------
# The captured spec (docs/ecoflow.md in the vendor notes) documents the endpoints and the
# signature but publishes no Power Ocean quota key list, and nobody on the project has the
# hardware or an account. The names below are candidates recalled from EcoFlow's public
# Power Ocean documentation; NONE has been seen on a device.
#
# Each Predbat leaf therefore maps to a TUPLE of candidate keys, tried in order, and
# publish_data() emits a sensor only for a leaf that actually resolved. A leaf that resolves
# to nothing is absent rather than zero, because a permanent 0 W is indistinguishable from a
# real reading and would be silently wrong in every plan.
#
# The first tester's job is to read the ecoflow_<sn>_quota_keys diagnostic sensor this
# component publishes, which lists the keys the device actually returned, and correct this
# table - or set ecoflow_key_map in apps.yaml, which overrides it without a code change.
ECOFLOW_TELEMETRY = {
    "soc": ("bpSoc", "soc"),
    "battery_power": ("bpPwr",),
    "grid_power": ("sysGridPwr",),
    "pv_power": ("mpptPwr",),
    "load_power": ("sysLoadPwr",),
}

# Sign conventions are ALSO unverified. Predbat wants grid_power negative on import and
# battery_power positive on discharge (see the sign conventions in pb_ent docs). Listed
# explicitly so a tester can flip one entry rather than hunting through publish_data(), and
# so the intent survives a refactor. Empty until a real reading settles it: negating a
# reading whose sign is unknown is a guess with a worse failure mode than not negating it,
# because the plan then works against the house rather than merely under-using the battery.
ECOFLOW_TELEMETRY_NEGATE = ()

# Units for each published telemetry leaf. soc is a percent, the rest are watts - EcoFlow
# publish power figures in watts for Power Ocean, which is the one unit claim here that a
# wrong guess would make every reading a thousand times out.
ECOFLOW_TELEMETRY_UNITS = {
    "soc": "%",
    "battery_power": "W",
    "grid_power": "W",
    "pv_power": "W",
    "load_power": "W",
}

# ---------------------------------------------------------------------------
# WRITE COMMANDS - SHIPS EMPTY ON PURPOSE
# ---------------------------------------------------------------------------
# PUT /iot-open/sign/device/quota takes {"sn": ..., "params": {"cmdSet": N, "id": M, ...}}.
# The captured spec documents that envelope and nothing else: it gives no cmdSet/id pair for
# a Power Ocean charge window, target SoC, or work mode. (The 11/24 pair in
# ECOFLOW_SIGN_VECTOR is the docs' own signing example for a DELTA-family EPS toggle, not a
# home-battery schedule command, and must not be reused here.)
#
# So this table ships EMPTY. An empty table is not a missing feature - it is the reason
# ecoflow_control_enable defaults to False and automatic_config() binds no control entity:
# Predbat must not be handed an inverter it believes it is driving and is not. The generic
# writer set_device_quota() is complete and tested, and fills in the moment a real
# cmdSet/id pair is confirmed against hardware.
#
# Expected shape once known, one entry per Predbat control:
#   "charge_window": {"cmdSet": <int>, "id": <int>, "fields": {...}}
ECOFLOW_SCHEDULE_COMMANDS = {}

ECOFLOW_STORAGE_MODULE = "ecoflow"
ECOFLOW_CACHE_STATIC = "static"
ECOFLOW_CACHE_CONTROL = "control"

# Tier TTLs in minutes. Static discovery is cheap and rarely changes; the quota poll is the
# only call made on a normal cycle, which is why it can sit near Predbat's own cadence.
ECOFLOW_TTL_STATIC = 8 * 60
ECOFLOW_TTL_QUOTA = 1

ECOFLOW_DEBUG_REDACT_KEYS = ("secretKey", "accessKey", "sign", "certificatePassword", "certificateAccount")

# Every key redacted in a request is redacted in a response too, plus nothing else: message
# and code are the only diagnostics a tester can send back, so they are never masked.
ECOFLOW_DEBUG_REDACT_KEYS_RESPONSE = ECOFLOW_DEBUG_REDACT_KEYS


def flatten_params(value, prefix=""):
    """Flatten one request's params into the (key, value) pairs the signature covers.

    The documented rules, all three of which the worked vector exercises or implies:
    a nested object becomes ``parent.child``, a list of scalars becomes ``ids[0]``, and a
    list of objects becomes ``deviceList[0].id``. Returned as a list of pairs rather than a
    dict so a duplicate flattened key cannot silently drop one side.

    A bool is rendered lower-case (``true``/``false``) because that is what JSON puts on the
    wire, and the server signs what it received, not what Python would have printed. None
    becomes an empty value for the same reason.
    """
    pairs = []
    if isinstance(value, dict):
        for key, item in value.items():
            pairs.extend(flatten_params(item, "{}.{}".format(prefix, key) if prefix else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            pairs.extend(flatten_params(item, "{}[{}]".format(prefix, index)))
    elif isinstance(value, bool):
        pairs.append((prefix, "true" if value else "false"))
    elif value is None:
        pairs.append((prefix, ""))
    else:
        pairs.append((prefix, str(value)))
    return pairs


def sign_string(params, access_key, nonce, timestamp):
    """Build the exact string the signature is taken over.

    Params sorted by ASCII on the flattened key and joined ``k=v&``, then the three auth
    values appended in the documented order. The auth values are appended rather than sorted
    in with the rest - ``accessKey`` would otherwise sort before ``params.*`` and the
    signature would never verify.

    Exposed separately from sign_request() so a failing signature can be diagnosed by
    comparing the string, which is what the docs' worked vector actually publishes.
    """
    pairs = sorted(flatten_params(params or {}), key=lambda pair: pair[0])
    parts = ["{}={}".format(key, value) for key, value in pairs]
    parts.append("accessKey={}".format(access_key))
    parts.append("nonce={}".format(nonce))
    parts.append("timestamp={}".format(timestamp))
    return "&".join(parts)


def sign_request(params, access_key, secret_key, nonce, timestamp):
    """Return the lower-case hex HMAC-SHA256 signature for one request.

    Proved against the docs' published worked vector in test_ecoflow_const.py. That test is
    the only evidence this integration has that it talks to EcoFlow correctly, since there is
    no account to try it against, so it asserts the documented hash literally.
    """
    raw = sign_string(params, access_key, nonce, timestamp)
    return hmac.new(secret_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def reply_text(code):
    """Return 'code (description)' for a documented reply code, or just the code."""
    try:
        description = ECOFLOW_REPLY_CODES.get(int(code))
    except (TypeError, ValueError):
        description = None
    return "{} ({})".format(code, description) if description else str(code)


def looks_like_home_battery(product_name):
    """Return True when a device's product name matches a targeted home-battery line.

    Advisory only - used for logging, never to drop a device. A name table that filtered
    would silently stop working the day EcoFlow ship a new home battery.
    """
    text = str(product_name or "").lower().replace("-", "").replace("_", "")
    return any(hint.replace(" ", "") in text.replace(" ", "") for hint in ECOFLOW_HOME_BATTERY_HINTS)


def resolve_telemetry(quota, key_map=None):
    """Extract Predbat's telemetry leaves from one device's flat quota dictionary.

    ``quota`` is whatever ``/quota/all`` returned, whose keys are frequently dotted
    (``module.field``) rather than nested, so the tail of a dotted key is tried as well as
    the whole key. ``key_map`` overrides ECOFLOW_TELEMETRY per install, which is the escape
    hatch a tester uses when the candidate names below turn out to be wrong.

    A leaf that resolves to nothing is OMITTED, not defaulted to zero: a permanent 0 W reads
    exactly like a real reading and would be wrong in every plan, whereas an absent sensor is
    visibly absent and automatic_config() declines to bind it.
    """
    table = key_map if key_map else ECOFLOW_TELEMETRY
    tails = {}
    for key, value in (quota or {}).items():
        tails.setdefault(str(key).rpartition(".")[2], value)
    values = {}
    for leaf, candidates in table.items():
        names = (candidates,) if isinstance(candidates, str) else tuple(candidates)
        for name in names:
            found = (quota or {}).get(name, tails.get(name))
            if found is None:
                continue
            try:
                number = float(found)
            except (TypeError, ValueError):
                continue
            values[leaf] = -number if leaf in ECOFLOW_TELEMETRY_NEGATE else number
            break
    return values
