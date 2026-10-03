# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test EcoFlow constants and request signing
# -----------------------------------------------------------------------------

"""Tests for the EcoFlow constants module (ecoflow_const.py).

The signing tests are the most important in this component. With no EcoFlow account and no
hardware, the docs' published worked vector is the ONLY evidence that Predbat talks to
EcoFlow correctly, so it is asserted literally rather than against a hash this code produced.
"""

from ecoflow_const import (
    ECOFLOW_BASE_URL,
    ECOFLOW_ENDPOINTS,
    ECOFLOW_METHODS,
    ECOFLOW_REPLY_CODES,
    ECOFLOW_MQTT_HOST,
    ECOFLOW_MQTT_PORT,
    ECOFLOW_MQTT_TOPICS,
    ECOFLOW_SIGN_VECTOR,
    ECOFLOW_SCHEDULE_COMMANDS,
    ECOFLOW_TELEMETRY,
    ECOFLOW_TELEMETRY_NEGATE,
    ECOFLOW_TELEMETRY_UNITS,
    flatten_params,
    looks_like_home_battery,
    reply_text,
    resolve_telemetry,
    sign_request,
    sign_string,
)


def test_ecoflow_sign_matches_documented_vector():
    """sign_request reproduces the hash the EcoFlow docs publish for their worked example.

    THE test for this component. If this fails the signing implementation is wrong and every
    request would be rejected, so both the intermediate string and the final hash are checked -
    the string is what the docs actually publish, and comparing it is how a mismatch gets
    diagnosed rather than merely detected.
    """
    failed = False
    built = sign_string(ECOFLOW_SIGN_VECTOR["params"], ECOFLOW_SIGN_VECTOR["access_key"], ECOFLOW_SIGN_VECTOR["nonce"], ECOFLOW_SIGN_VECTOR["timestamp"])
    if built != ECOFLOW_SIGN_VECTOR["string"]:
        print(f"ERROR: signed string\n  got      {built}\n  expected {ECOFLOW_SIGN_VECTOR['string']}")
        failed = True
    signature = sign_request(ECOFLOW_SIGN_VECTOR["params"], ECOFLOW_SIGN_VECTOR["access_key"], ECOFLOW_SIGN_VECTOR["secret_key"], ECOFLOW_SIGN_VECTOR["nonce"], ECOFLOW_SIGN_VECTOR["timestamp"])
    if signature != ECOFLOW_SIGN_VECTOR["sign"]:
        print(f"ERROR: signature {signature} != documented {ECOFLOW_SIGN_VECTOR['sign']}")
        failed = True
    # Asserted literally as well as via the constant, so editing the constant to match a
    # broken implementation cannot make this test pass.
    if signature != "07c13b65e037faf3b153d51613638fa80003c4c38d2407379a7f52851af1473e":
        print(f"ERROR: signature {signature} does not match the literal documented hash")
        failed = True
    assert not failed, "test_ecoflow_sign_matches_documented_vector"


def test_ecoflow_sign_is_sensitive_to_every_input():
    """Changing any signed input changes the signature.

    A signing implementation that ignored, say, the nonce would still reproduce the worked
    vector, because the vector fixes every input at once. This pins each one down separately.
    """
    failed = False
    base = dict(params=ECOFLOW_SIGN_VECTOR["params"], access_key=ECOFLOW_SIGN_VECTOR["access_key"], secret_key=ECOFLOW_SIGN_VECTOR["secret_key"], nonce=ECOFLOW_SIGN_VECTOR["nonce"], timestamp=ECOFLOW_SIGN_VECTOR["timestamp"])
    reference = sign_request(**base)
    for field, value in [
        ("params", {"sn": "123456789", "params": {"cmdSet": 11, "id": 24, "eps": 1}}),
        ("access_key", "Fp4SvIprYSDPXtYJidEtUAd1p"),  # cspell:disable-line
        ("secret_key", "WIbFEKre0s6sLnh4ei7SPUeYnptHG6W"),  # cspell:disable-line
        ("nonce", "345165"),
        ("timestamp", "1671171709429"),
    ]:
        altered = dict(base)
        altered[field] = value
        if sign_request(**altered) == reference:
            print(f"ERROR: changing {field} did not change the signature")
            failed = True
    assert not failed, "test_ecoflow_sign_is_sensitive_to_every_input"


def test_flatten_params_uses_the_documented_nesting_forms():
    """All three documented flattening forms, plus the JSON rendering of bool and None.

    A nested object becomes parent.child, a list of scalars ids[0], and a list of objects
    deviceList[0].id. Bools render lower-case because the server signs the JSON it received,
    not what Python's str() would have printed - a Python-cased "True" verifies against nothing.
    """
    failed = False
    cases = [
        ({"deviceInfo": {"id": 1}}, [("deviceInfo.id", "1")]),
        ({"ids": [1, 2]}, [("ids[0]", "1"), ("ids[1]", "2")]),
        ({"deviceList": [{"id": 1}, {"id": 2}]}, [("deviceList[0].id", "1"), ("deviceList[1].id", "2")]),
        ({"enabled": True, "off": False}, [("enabled", "true"), ("off", "false")]),
        ({"empty": None}, [("empty", "")]),
    ]
    for params, expected in cases:
        got = flatten_params(params)
        if sorted(got) != sorted(expected):
            print(f"ERROR: flatten {params} gave {got}, expected {expected}")
            failed = True
    assert not failed, "test_flatten_params_uses_the_documented_nesting_forms"


def test_sign_string_appends_auth_values_last():
    """accessKey/nonce/timestamp are appended, not sorted in with the params.

    Sorted in, accessKey would land before params.* and the signature would never verify -
    and the failure would look exactly like a bad secret key, so it is pinned here.
    """
    failed = False
    built = sign_string({"zz": 1}, "AK", "111111", "222")
    if built != "zz=1&accessKey=AK&nonce=111111&timestamp=222":
        print(f"ERROR: {built}")
        failed = True
    # 'aa' sorts before 'accessKey', so a sorted-in implementation would put accessKey second
    # and still look plausible on the 'zz' case above.
    built = sign_string({"aa": 1}, "AK", "111111", "222")
    if built != "aa=1&accessKey=AK&nonce=111111&timestamp=222":
        print(f"ERROR: {built}")
        failed = True
    assert not failed, "test_sign_string_appends_auth_values_last"


def test_ecoflow_endpoints_and_methods_agree():
    """Every documented endpoint is declared, with the method the docs give it.

    The method matters as much as the path: /device/quota is a PUT to write and a POST to read
    a named subset, so a single path with the wrong verb is a silent read where a write was
    intended (or worse, the reverse).
    """
    failed = False
    expected = {
        "device_list": ("/iot-open/sign/device/list", "GET"),
        "quota_all": ("/iot-open/sign/device/quota/all", "GET"),
        "quota_get": ("/iot-open/sign/device/quota", "POST"),
        "quota_set": ("/iot-open/sign/device/quota", "PUT"),
        "certification": ("/iot-open/sign/certification", "GET"),
    }
    if ECOFLOW_BASE_URL != "https://api.ecoflow.com":
        print(f"ERROR: base url {ECOFLOW_BASE_URL}")
        failed = True
    for operation, (path, method) in expected.items():
        if ECOFLOW_ENDPOINTS.get(operation) != path:
            print(f"ERROR: endpoint {operation} = {ECOFLOW_ENDPOINTS.get(operation)} != {path}")
            failed = True
        if ECOFLOW_METHODS.get(operation) != method:
            print(f"ERROR: method {operation} = {ECOFLOW_METHODS.get(operation)} != {method}")
            failed = True
    assert not failed, "test_ecoflow_endpoints_and_methods_agree"


def test_ecoflow_reply_codes_and_text():
    """The three documented reply codes carry a human description."""
    failed = False
    for code in (0, -1, -2):
        if code not in ECOFLOW_REPLY_CODES:
            print(f"ERROR: reply code {code} missing")
            failed = True
    if "does not belong" not in reply_text(-1):
        print(f"ERROR: reply_text(-1) = {reply_text(-1)}")
        failed = True
    if "offline" not in reply_text(-2):
        print(f"ERROR: reply_text(-2) = {reply_text(-2)}")
        failed = True
    # An undocumented code must still render rather than raise - the write path logs it.
    if reply_text(-99) != "-99":
        print(f"ERROR: reply_text(-99) = {reply_text(-99)}")
        failed = True
    assert not failed, "test_ecoflow_reply_codes_and_text"


def test_ecoflow_mqtt_constants():
    """The MQTT broker and the six documented topic shapes are declared.

    Unused today - telemetry is polled - but declared so the push path lands on a wire format
    that is already pinned down rather than reinvented then.
    """
    failed = False
    if (ECOFLOW_MQTT_HOST, ECOFLOW_MQTT_PORT) != ("mqtt.ecoflow.com", 8883):
        print(f"ERROR: broker {ECOFLOW_MQTT_HOST}:{ECOFLOW_MQTT_PORT}")
        failed = True
    for name in ("quota", "set", "set_reply", "get", "get_reply", "status"):
        template = ECOFLOW_MQTT_TOPICS.get(name)
        if not template:
            print(f"ERROR: topic {name} missing")
            failed = True
            continue
        rendered = template.format(account="acct", sn="SN1")
        if rendered != "/open/acct/SN1/{}".format(name):
            print(f"ERROR: topic {name} renders {rendered}")
            failed = True
    assert not failed, "test_ecoflow_mqtt_constants"


def test_ecoflow_schedule_commands_ship_empty():
    """The write command table ships EMPTY, and that is the point of it.

    The docs give no Power Ocean cmdSet/id pair, so a populated table here would be a guess
    that silently sends the wrong command to somebody's house battery. This test is what stops
    one being added without the hardware confirmation that must come with it - and it is also
    what makes ecoflow_control_enable's False default correct rather than arbitrary.
    """
    failed = False
    if ECOFLOW_SCHEDULE_COMMANDS:
        print(f"ERROR: ECOFLOW_SCHEDULE_COMMANDS should ship empty until a cmdSet/id pair is confirmed against hardware, got {ECOFLOW_SCHEDULE_COMMANDS}")
        failed = True
    assert not failed, "test_ecoflow_schedule_commands_ship_empty"


def test_ecoflow_telemetry_table_shape():
    """Every telemetry leaf carries candidate keys and a unit, and none is silently negated."""
    failed = False
    for leaf in ("soc", "battery_power", "grid_power", "pv_power", "load_power"):
        if leaf not in ECOFLOW_TELEMETRY:
            print(f"ERROR: telemetry leaf {leaf} missing")
            failed = True
        if leaf not in ECOFLOW_TELEMETRY_UNITS:
            print(f"ERROR: telemetry unit for {leaf} missing")
            failed = True
    if ECOFLOW_TELEMETRY_UNITS.get("soc") != "%":
        print("ERROR: soc must be a percent")
        failed = True
    # Ships empty on purpose: the sign conventions are unverified, and negating a reading whose
    # sign is unknown has a worse failure mode than not negating it.
    if ECOFLOW_TELEMETRY_NEGATE:
        print(f"ERROR: ECOFLOW_TELEMETRY_NEGATE should ship empty until a real reading settles the sign, got {ECOFLOW_TELEMETRY_NEGATE}")
        failed = True
    assert not failed, "test_ecoflow_telemetry_table_shape"


def test_resolve_telemetry_omits_what_it_cannot_find():
    """An unresolved leaf is ABSENT, never zero, and a dotted quota key resolves on its tail.

    The absent-not-zero rule is the whole contract with an unverified key table: a permanent
    0 W reads exactly like a real measurement and would be wrong in every plan, whereas an
    absent sensor is visibly absent and automatic_config() declines to bind it.
    """
    failed = False
    values = resolve_telemetry({"bpSoc": 55, "bpPwr": -1200})
    if values.get("soc") != 55.0 or values.get("bpPwr") is not None:
        print(f"ERROR: resolve {values}")
        failed = True
    if "grid_power" in values:
        print("ERROR: an unmatched leaf must be omitted, not defaulted")
        failed = True
    # Real quota payloads frequently arrive with dotted module-prefixed keys.
    dotted = resolve_telemetry({"hs_yj751_pd_appshow_addr.bpSoc": 42})  # cspell:disable-line
    if dotted.get("soc") != 42.0:
        print(f"ERROR: dotted key did not resolve on its tail: {dotted}")
        failed = True
    # A non-numeric value is not a reading.
    if resolve_telemetry({"bpSoc": "unknown"}):
        print("ERROR: a non-numeric value should not resolve")
        failed = True
    assert not failed, "test_resolve_telemetry_omits_what_it_cannot_find"


def test_resolve_telemetry_honours_the_key_map_override():
    """A user's ecoflow_key_map replaces the unverified table outright.

    Replaces rather than extends: a user correcting a wrong name must not still have Predbat
    match the wrong one first, or the override would appear to do nothing.
    """
    failed = False
    values = resolve_telemetry({"bpSoc": 10, "myOwnSoc": 90}, {"soc": ("myOwnSoc",)})
    if values.get("soc") != 90.0:
        print(f"ERROR: key_map override gave {values}")
        failed = True
    assert not failed, "test_resolve_telemetry_honours_the_key_map_override"


def test_looks_like_home_battery_is_advisory():
    """Recognises the targeted product lines, and is tolerant of junk.

    Advisory only - get_device_list() logs on a miss and still polls the device - so this must
    never raise on an unexpected value.
    """
    failed = False
    for name, expected in [("PowerOcean", True), ("power ocean plus", True), ("STREAM Ultra", True), ("DELTA Pro 3", False), (None, False), (123, False)]:
        if looks_like_home_battery(name) is not expected:
            print(f"ERROR: looks_like_home_battery({name!r}) != {expected}")
            failed = True
    assert not failed, "test_looks_like_home_battery_is_advisory"


def run_ecoflow_const_tests(my_predbat):
    """Run all EcoFlow constants tests."""
    failed = False
    for name, fn in [
        ("sign_vector", test_ecoflow_sign_matches_documented_vector),
        ("sign_sensitivity", test_ecoflow_sign_is_sensitive_to_every_input),
        ("flatten_params", test_flatten_params_uses_the_documented_nesting_forms),
        ("sign_string_order", test_sign_string_appends_auth_values_last),
        ("endpoints", test_ecoflow_endpoints_and_methods_agree),
        ("reply_codes", test_ecoflow_reply_codes_and_text),
        ("mqtt", test_ecoflow_mqtt_constants),
        ("commands_empty", test_ecoflow_schedule_commands_ship_empty),
        ("telemetry_shape", test_ecoflow_telemetry_table_shape),
        ("resolve_telemetry", test_resolve_telemetry_omits_what_it_cannot_find),
        ("key_map_override", test_resolve_telemetry_honours_the_key_map_override),
        ("product_hint", test_looks_like_home_battery_is_advisory),
    ]:
        try:
            if fn():
                print(f"  FAILED: ecoflow_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in ecoflow_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
