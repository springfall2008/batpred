# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS Cloud API component (``alphaess.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import hashlib
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from alphaess import AlphaESSAPI
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockAlphaESS(AlphaESSAPI):
    """Test double: build an AlphaESSAPI without the full component lifecycle."""

    def __init__(self, app_id="alphatestappid00000", app_secret="secret0000000000", inverter_sn=None, control_enable=True, automatic=False):  # cspell:disable-line
        """Set up a minimal AlphaESSAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-alphaess-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.minutes_now = 0
        self.state = {}
        self.published = {}
        self.external_state = {}
        self.initialize(
            app_id=app_id,
            app_secret=app_secret,
            inverter_sn=inverter_sn,
            automatic=automatic,
            control_enable=control_enable,
        )

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity instead of reaching Home Assistant."""
        self.published[entity] = {"state": state, "attributes": attributes}
        self.state[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back whatever the test (or dashboard_item) put in self.state."""
        return self.state.get(entity_id, default)

    async def set_state_external(self, entity_id, state, attributes={}):
        """Record a Predbat CONFIG_ITEMS switch change instead of reaching Home Assistant."""
        self.external_state[entity_id] = state

    def set_arg_auto(self, arg, value):
        """Record an auto-discovered apps.yaml binding."""
        self.base.args[arg] = value

    @property
    def storage(self):
        """No Storage component in unit tests - matches a standalone CLI run."""
        return None


def _envelope(code=200, data=None, msg=None, exp_msg=None):
    """Build an AlphaESS response envelope.

    msg defaults to "Success" ONLY for code 200. The client treats msg == "Success" as
    success regardless of code (the periodic endpoints report status in msg/info rather
    than code), so a helper that defaulted every envelope to "Success" would make a
    failure envelope read as a success and let tests pass for the wrong reason.
    """
    if msg is None:
        msg = "Success" if code == 200 else "Failed"
    return {"code": code, "msg": msg, "expMsg": exp_msg, "extra": None, "data": data}


ESS_LIST_SAMPLE = [
    {"sysSn": "AL70110230306xx", "popv": 9.0, "minv": "SMILE5-INV", "poinv": 5.0, "cobat": 13.34, "mbat": "SMILE-BAT-13.3P", "surplusCobat": 13.34, "usCapacity": 100.0, "emsStatus": "Normal"},
    {"sysSn": "AL70110230302xx", "popv": 5.0, "minv": "SMILE5-INV", "poinv": 5.0, "cobat": 10.1, "mbat": "SMILE-BAT-10.1P", "surplusCobat": 9.09, "usCapacity": 90.0, "emsStatus": "Normal"},
]


def test_alphaess_sign_matches_the_documented_algorithm():
    """sign is sha512(appId + appSecret + timeStamp), lower-case hex, with both spellings sent."""
    failed = False
    client = MockAlphaESS(app_id="alphaef7900ee81dbbce9", app_secret="c2d2ef6c047c49678e2c332fb2d74c3c")  # cspell:disable-line
    with patch("alphaess.time.time", return_value=1676353875):
        headers = client._headers()
    expect = hashlib.sha512(b"alphaef7900ee81dbbce9c2d2ef6c047c49678e2c332fb2d74c3c1676353875").hexdigest()  # cspell:disable-line
    if headers.get("sign") != expect:
        print(f"ERROR: sign {headers.get('sign')} != {expect}")
        failed = True
    if headers.get("appId") != "alphaef7900ee81dbbce9":  # cspell:disable-line
        print(f"ERROR: appId {headers.get('appId')}")
        failed = True
    # The reference client sends both spellings; the API is documented with timeStamp.
    if headers.get("timeStamp") != "1676353875" or headers.get("timestamp") != "1676353875":
        print(f"ERROR: timestamp headers {headers}")
        failed = True
    assert not failed, "test_alphaess_sign_matches_the_documented_algorithm"


def test_alphaess_request_returns_code_and_data():
    """_request surfaces the envelope code, which is the only way to judge a write."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, [{"sysSn": "AL70"}]))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._get("ess_list"))
    if code != 200:
        print(f"ERROR: code {code}")
        failed = True
    if not data or data[0].get("sysSn") != "AL70":
        print(f"ERROR: data {data}")
        failed = True
    assert not failed, "test_alphaess_request_returns_code_and_data"


def test_alphaess_write_failure_is_distinguishable_from_success():
    """A write answers data:null either way, so the code alone separates 6008 from 200."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._post("update_charge_config", body={"sysSn": "AL70"}))
    if code != 6008:
        print(f"ERROR: code {code} should be 6008")
        failed = True
    if data is not None:
        print(f"ERROR: data {data} should be None")
        failed = True
    if "Set failed" not in client.last_api_error:
        print(f"ERROR: last_api_error {client.last_api_error!r}")
        failed = True
    assert not failed, "test_alphaess_write_failure_is_distinguishable_from_success"


def test_alphaess_info_field_counts_as_success():
    """The periodic endpoints report status in `info`, not `msg`."""
    failed = False
    client = MockAlphaESS()
    body = {"code": 200, "info": "Success", "expMsg": None, "data": {"sysSn": "AL70"}}
    response = create_aiohttp_mock_response(status=200, json_data=body)
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._get("time_charge", params={"sysSn": "AL70"}))
    if code != 200 or not data:
        print(f"ERROR: code {code} data {data}")
        failed = True
    assert not failed, "test_alphaess_info_field_counts_as_success"


def test_alphaess_clock_skew_is_reported_as_a_clock_problem():
    """6006 must not read as bad credentials - the symptoms are otherwise identical."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6006, None, msg="Timestamp error"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, _ = run_async_local(client._get("ess_list"))
    if code != 6006:
        print(f"ERROR: code {code}")
        failed = True
    if not any("clock" in message.lower() for message in client.log_messages):
        print(f"ERROR: no clock-skew log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_clock_skew_is_reported_as_a_clock_problem"


def test_alphaess_expmsg_is_logged_when_present():
    """expMsg is the only field that names the bad parameter; msg just says 'Parameter error'."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6001, None, msg="Parameter error", exp_msg="time list is null"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client._post("set_time_charge", body={"sysSn": "AL70"}))
    if not any("time list is null" in message for message in client.log_messages):
        print(f"ERROR: expMsg not logged, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_expmsg_is_logged_when_present"


def test_alphaess_secret_never_reaches_the_log():
    """api_debug traces every call, so the redaction has to actually work."""
    failed = False
    client = MockAlphaESS(app_secret="hunter2secretvalue")  # cspell:disable-line
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client._get("ess_list"))
    for message in client.log_messages:
        if "hunter2secretvalue" in message:  # cspell:disable-line
            print(f"ERROR: secret leaked in log: {message}")
            failed = True
    assert not failed, "test_alphaess_secret_never_reaches_the_log"


def test_alphaess_transport_failure_returns_minus_one():
    """A transport error is not an API verdict; it must be distinguishable from one."""
    failed = False
    client = MockAlphaESS()
    session = create_aiohttp_mock_session(exception=Exception("connection reset"))
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._get("ess_list"))
    if code != -1:
        print(f"ERROR: transport failure code {code} should be -1")
        failed = True
    if data is not None:
        print(f"ERROR: data {data} should be None")
        failed = True
    assert not failed, "test_alphaess_transport_failure_returns_minus_one"


def test_alphaess_redact_function_masks_secrets_but_preserves_ordinary_keys():
    """redact() masks secrets in requests but preserves other keys unchanged."""
    failed = False
    # Request redaction should mask secrets
    request_payload = {
        "appSecret": "hunter2secretvalue",  # cspell:disable-line
        "app_secret": "alsoasecret",  # cspell:disable-line
        "sign": "deadbeef",
        "code": "onetime123",
        "checkCode": "another",
        "sysSn": "AL70",
        "normal_field": 42,
    }
    redacted_request = AlphaESSAPI.redact(request_payload, direction="request")
    if redacted_request.get("appSecret") != "***":
        print(f"ERROR: appSecret not masked in request: {redacted_request.get('appSecret')}")
        failed = True
    if redacted_request.get("app_secret") != "***":
        print(f"ERROR: app_secret not masked in request: {redacted_request.get('app_secret')}")
        failed = True
    if redacted_request.get("sign") != "***":
        print(f"ERROR: sign not masked in request: {redacted_request.get('sign')}")
        failed = True
    if redacted_request.get("code") != "***":
        print(f"ERROR: code not masked in request: {redacted_request.get('code')}")
        failed = True
    if redacted_request.get("checkCode") != "***":
        print(f"ERROR: checkCode not masked in request: {redacted_request.get('checkCode')}")
        failed = True
    if redacted_request.get("sysSn") != "AL70":
        print(f"ERROR: sysSn should not be masked: {redacted_request.get('sysSn')}")
        failed = True
    if redacted_request.get("normal_field") != 42:
        print(f"ERROR: normal_field should not be masked: {redacted_request.get('normal_field')}")
        failed = True

    # Response redaction should NOT mask code/msg/info/expMsg
    response_payload = {
        "appSecret": "hunter2secretvalue",  # cspell:disable-line
        "code": 200,
        "msg": "Success",
        "info": "Success",
        "expMsg": "parameter xyz is invalid",
        "sign": "deadbeef",
        "data": {"sysSn": "AL70"},
    }
    redacted_response = AlphaESSAPI.redact(response_payload, direction="response")
    if redacted_response.get("appSecret") != "***":
        print(f"ERROR: appSecret not masked in response: {redacted_response.get('appSecret')}")
        failed = True
    if redacted_response.get("sign") != "***":
        print(f"ERROR: sign not masked in response: {redacted_response.get('sign')}")
        failed = True
    if redacted_response.get("code") != 200:
        print(f"ERROR: code should NOT be masked in response: {redacted_response.get('code')}")
        failed = True
    if redacted_response.get("msg") != "Success":
        print(f"ERROR: msg should NOT be masked in response: {redacted_response.get('msg')}")
        failed = True
    if redacted_response.get("info") != "Success":
        print(f"ERROR: info should NOT be masked in response: {redacted_response.get('info')}")
        failed = True
    if redacted_response.get("expMsg") != "parameter xyz is invalid":
        print(f"ERROR: expMsg should NOT be masked in response: {redacted_response.get('expMsg')}")
        failed = True
    assert not failed, "test_alphaess_redact_function_masks_secrets_but_preserves_ordinary_keys"


def test_alphaess_response_code_is_logged_even_with_debug_on():
    """Response codes are preserved in logs so debug evidence is not destroyed."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client._post("update_charge_config", body={"sysSn": "AL70"}))
    # Check that the response code 6008 was logged (not masked as ***)
    if not any("6008" in msg for msg in client.log_messages):
        print(f"ERROR: code 6008 not logged, got {client.log_messages}")
        failed = True
    if any('"code": "***"' in msg for msg in client.log_messages):
        print(f"ERROR: response code was masked in log: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_response_code_is_logged_even_with_debug_on"


def test_alphaess_discovery_maps_ratings():
    """cobat becomes soc_max kWh and poinv becomes inverter_limit W."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["AL70110230306xx", "AL70110230302xx"]:
        print(f"ERROR: serials {serials}")
        failed = True
    if abs(client.battery_capacity("AL70110230306xx") - 13.34) > 0.001:
        print(f"ERROR: capacity {client.battery_capacity('AL70110230306xx')}")
        failed = True
    if abs(client.inverter_limit("AL70110230306xx") - 5000.0) > 0.1:
        print(f"ERROR: inverter_limit {client.inverter_limit('AL70110230306xx')}")
        failed = True
    if client.discovery_ok is not True:
        print(f"ERROR: discovery_ok {client.discovery_ok}")
        failed = True
    assert not failed, "test_alphaess_discovery_maps_ratings"


def test_alphaess_battery_rate_max_defaults_to_the_inverter_limit():
    """Leaving battery_rate_max unmapped is NOT neutral - inverter.py:410 falls back to
    a hard-coded 2600W, roughly half a SMILE5's rate, silently, on every plan. poinv is
    the best available estimate and an over-estimate self-reports via
    battery_rate_max_scaling, so it is used rather than nothing.
    """
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client.get_device_list())
    if abs(client.battery_rate_max("AL70110230306xx") - 5000.0) > 0.1:
        print(f"ERROR: derived battery_rate_max {client.battery_rate_max('AL70110230306xx')}")
        failed = True
    # The explicit override wins outright.
    override = MockAlphaESS()
    override.battery_rate_max_override = 3600.0
    override.device_detail = client.device_detail
    if abs(override.battery_rate_max("AL70110230306xx") - 3600.0) > 0.1:
        print(f"ERROR: override battery_rate_max {override.battery_rate_max('AL70110230306xx')}")
        failed = True
    assert not failed, "test_alphaess_battery_rate_max_defaults_to_the_inverter_limit"


def test_alphaess_systems_without_a_battery_are_skipped():
    """AlphaESS also sells plug-in solar, which has nothing for Predbat to drive.

    A capability filter rather than a model filter, so it catches every non-battery
    product AlphaESS ships now or later with no table to maintain.
    """
    failed = False
    client = MockAlphaESS()
    payload = list(ESS_LIST_SAMPLE) + [
        {"sysSn": "VT100000000001", "minv": "VT1000", "poinv": 0.8, "popv": 0.8, "cobat": 0},
        {"sysSn": "VT100000000002", "minv": "VT1000", "poinv": 0.8, "popv": 0.8},
    ]
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, payload))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if "VT100000000001" in serials or "VT100000000002" in serials:
        print(f"ERROR: battery-less systems registered: {serials}")
        failed = True
    if len(serials) != 2:
        print(f"ERROR: expected 2 serials, got {serials}")
        failed = True
    # Logged once by serial and model, so the user can see it was recognised and passed
    # over rather than silently missing.
    if not any("VT100000000001" in message and "batter" in message.lower() for message in client.log_messages):
        print(f"ERROR: no skip log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_systems_without_a_battery_are_skipped"


def test_alphaess_serial_filter_restricts_discovery():
    """alphaess_inverter_sn narrows discovery, case-insensitively."""
    failed = False
    client = MockAlphaESS(inverter_sn="al70110230302XX")
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["AL70110230302xx"]:
        print(f"ERROR: filtered serials {serials}")
        failed = True
    assert not failed, "test_alphaess_serial_filter_restricts_discovery"


def test_alphaess_serial_filter_mismatch_reports_the_filter_not_the_account():
    """When a filter is configured but matches nothing, log names the filter not the account.

    Misdirecting to "no battery systems" hides the real cause - the filter typo.
    refresh_static returns False but discovery_ok is True (the call succeeded).
    """
    failed = False
    client = MockAlphaESS(inverter_sn="NONEXISTENT")
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        result = run_async_local(client.refresh_static())
    if result is not False:
        print(f"ERROR: refresh_static should return False when filter matches nothing, got {result}")
        failed = True
    if client.discovery_ok is not True:
        print(f"ERROR: discovery succeeded, discovery_ok should be True, got {client.discovery_ok}")
        failed = True
    if not any("NONEXISTENT" in message and "filter" in message.lower() for message in client.log_messages):
        print(f"ERROR: log should mention the filter value, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_serial_filter_mismatch_reports_the_filter_not_the_account"


def test_alphaess_refresh_static_never_clears_a_working_device_list():
    """Absence of a result is not a result.

    This tier re-runs every 8 hours in a long-lived process; one transient failure must
    not take a working component down until the next success, and it must not write an
    empty list to the cache and then stamp it fresh. refresh_static must return False,
    the tier clock must not advance, and device_list must be unchanged.
    """
    failed = False
    client = MockAlphaESS()
    ok = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok)):
        result = run_async_local(client.refresh_static())
    if result is not True:
        print(f"ERROR: initial refresh_static should return True, got {result}")
        failed = True
    before_list = list(client.device_list)
    before_tier = client._tier_refreshed.get("static")
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        result = run_async_local(client.refresh_static())
    if result is not False:
        print(f"ERROR: refresh_static should return False on failure, got {result}")
        failed = True
    if client.device_list != before_list:
        print(f"ERROR: device_list cleared by a transient failure: {client.device_list} != {before_list}")
        failed = True
    after_tier = client._tier_refreshed.get("static")
    if after_tier != before_tier:
        print(f"ERROR: tier clock advanced on failure: {after_tier} != {before_tier}")
        failed = True
    assert not failed, "test_alphaess_refresh_static_never_clears_a_working_device_list"


def test_alphaess_discovery_distinguishes_empty_account_from_failure():
    """An empty account and a failed call look identical without this, and the CLI needs
    to name which one actually happened."""
    failed = False
    empty = MockAlphaESS()
    ok = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok)):
        run_async_local(empty.get_device_list())
    if empty.discovery_ok is not True:
        print(f"ERROR: empty account discovery_ok {empty.discovery_ok} should be True")
        failed = True
    broken = MockAlphaESS()
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6007, None, msg="Sign verification error"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        run_async_local(broken.get_device_list())
    if broken.discovery_ok is not False:
        print(f"ERROR: failed discovery_ok {broken.discovery_ok} should be False")
        failed = True
    assert not failed, "test_alphaess_discovery_distinguishes_empty_account_from_failure"


LAST_POWER_SAMPLE = {
    "ppv": 0.0,
    "ppvDetail": {"ppv1": 0.0, "ppv2": 0.0, "ppv3": 0.0, "ppv4": 0.0, "pmeterDc": 0.0},
    "soc": 56.0,
    "pev": 0,
    "pevDetail": {"ev1Power": None, "ev2Power": None, "ev3Power": None, "ev4Power": None},
    "prealL1": 1159.0,
    "pgrid": 11.0,
    "pgridDetail": {"pmeterL1": 11.0, "pmeterL2": 0.0, "pmeterL3": 0.0},
    "pload": 1275.0,
    "pbat": 1264.0,
}


def test_alphaess_telemetry_applies_predbat_sign_conventions():
    """pgrid is positive on IMPORT and Predbat wants negative on import, so it is negated.

    pbat needs NO negation: the API doc's own live sample balances as
    pgrid 11 + pbat 1264 = pload 1275 with ppv 0, so a positive pbat is discharge, which
    is already Predbat's convention.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, LAST_POWER_SAMPLE))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        ok = run_async_local(client.fetch_device_data("AL70"))
    values = client.device_values.get("AL70", {})
    if not ok:
        print("ERROR: fetch_device_data returned False")
        failed = True
    for leaf, expect in (("soc", 56.0), ("battery_power", 1264.0), ("grid_power", -11.0), ("pv_power", 0.0), ("load_power", 1275.0), ("ev_power", 0.0)):
        if abs(values.get(leaf, 0.0) - expect) > 0.001:
            print(f"ERROR: {leaf} {values.get(leaf)} != {expect}")
            failed = True
    assert not failed, "test_alphaess_telemetry_applies_predbat_sign_conventions"


def test_alphaess_ppv_detail_all_null_signal():
    """ppv_detail_all_null is the whole hybrid-vs-AC-coupled discriminator.

    Null strings mean no DC strings fitted; a hybrid AT NIGHT reports zeros, not nulls.
    A field-name typo (``"ppv_{}"`` instead of ``"ppv{}"``) or an inverted all/any would
    pass every test that only injects ``ppv_detail_all_null`` by hand - this one drives
    real payload shapes through ``_apply_live_payload`` and checks the computed value.
    """
    failed = False
    client = MockAlphaESS()
    base = {"soc": 56.0, "pbat": 0.0, "pgrid": 0.0, "ppv": 0.0, "pload": 0.0, "pev": 0.0}

    cases = (
        ("all null - no DC strings fitted", {"ppv1": None, "ppv2": None, "ppv3": None, "ppv4": None}, True),
        ("all zero - a hybrid at night, must NOT read as AC-coupled", {"ppv1": 0.0, "ppv2": 0.0, "ppv3": 0.0, "ppv4": 0.0}, False),
        ("some non-zero - daytime generation", {"ppv1": 340.0, "ppv2": 0.0, "ppv3": 0.0, "ppv4": 0.0}, False),
        ("mixed null and zero - not EVERY string is null", {"ppv1": None, "ppv2": 0.0, "ppv3": None, "ppv4": None}, False),
        ("ppvDetail is an empty dict", {}, False),
    )
    for name, detail, expect in cases:
        payload = dict(base, ppvDetail=detail)
        client._apply_live_payload("AL70", payload)
        actual = client.device_values.get("AL70", {}).get("ppv_detail_all_null")
        if actual != expect:
            print(f"ERROR: {name}: ppv_detail_all_null {actual} != {expect}")
            failed = True

    # ppvDetail absent from the payload entirely - the bool(pv_detail) guard.
    payload = dict(base)
    client._apply_live_payload("AL70", payload)
    actual = client.device_values.get("AL70", {}).get("ppv_detail_all_null")
    if actual is not False:
        print(f"ERROR: ppvDetail absent: ppv_detail_all_null {actual} != False")
        failed = True

    assert not failed, "test_alphaess_ppv_detail_all_null_signal"


def test_alphaess_falls_back_to_history_when_live_data_is_unavailable():
    """Not every system serves getLastPowerData (Storion-S5 is the known case).

    Decided on BEHAVIOUR, not on a model list: a list only covers models someone already
    wrote down, while this covers Storion-S5, any unlisted model with the same gap, and a
    system that simply stops answering.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    history = [
        {"sysSn": "AL70", "uploadTime": "2026-08-22 20:04:04", "ppv": 10.0, "load": 900.0, "cbat": 40.0, "feedIn": 0.0, "gridCharge": 100.0},
        {"sysSn": "AL70", "uploadTime": "2026-08-22 20:09:04", "ppv": 0.0, "load": 1218.0, "cbat": 56.8, "feedIn": 0.0, "gridCharge": 2.0},
    ]
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, history)),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_device_data("AL70"))
    values = client.device_values.get("AL70", {})
    if not ok:
        print("ERROR: history fallback returned False")
        failed = True
    # The MOST RECENT sample, not the first.
    if abs(values.get("soc", 0.0) - 56.8) > 0.001:
        print(f"ERROR: soc from history {values.get('soc')} != 56.8")
        failed = True
    # feedIn and gridCharge are separate positive-only fields, so grid is reconstructed
    # as gridCharge - feedIn and THEN negated for Predbat.
    if abs(values.get("grid_power", 0.0) - (-2.0)) > 0.001:
        print(f"ERROR: grid_power from history {values.get('grid_power')} != -2.0")
        failed = True
    if abs(values.get("load_power", 0.0) - 1218.0) > 0.001:
        print(f"ERROR: load_power from history {values.get('load_power')}")
        failed = True
    assert not failed, "test_alphaess_falls_back_to_history_when_live_data_is_unavailable"


def test_alphaess_history_reads_cbat_not_the_portal_spelling():
    """The portal documents the SOC field as cobat; the live API returns cbat.

    Reading the portal name silently yields None, which looks exactly like "this system
    has no SOC either" and would send a working serial down the skip path.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    for field in ("cbat", "cobat"):
        sample = [{"uploadTime": "2026-08-22 20:09:04", "ppv": 0.0, "load": 100.0, field: 42.0, "feedIn": 0.0, "gridCharge": 0.0}]
        responses = [
            create_aiohttp_mock_response(status=200, json_data=_envelope(-1, None)),
            create_aiohttp_mock_response(status=200, json_data=_envelope(200, sample)),
        ]
        client.device_values = {}
        client._live_fail_count = {}
        client._live_ok = {}
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
            run_async_local(client.fetch_device_data("AL70"))
        if abs(client.device_values.get("AL70", {}).get("soc", 0.0) - 42.0) > 0.001:
            print(f"ERROR: soc from {field} = {client.device_values.get('AL70', {}).get('soc')}")
            failed = True
    assert not failed, "test_alphaess_history_reads_cbat_not_the_portal_spelling"


def test_alphaess_history_derives_battery_power_from_the_energy_balance():
    """getOneDayPowerBySn has no pbat field, so battery_power must be derived rather than
    left unmapped - automatic_config() maps battery_power unconditionally for every
    inverter, including one that starts life on the history path (a restart landing
    straight on a demoted serial), and an arg pointed at a sensor that never appears is
    worse than an absent one: it silently zeroes find_battery_size and battery-curve
    learning for the whole session.

    Two cases, both directions, so a sign error in the derivation cannot slip through:
    discharging to cover a PV shortfall, and charging from PV surplus, both with no grid
    interaction so the expected value is unambiguous.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]

    # PV covers only part of the load; the rest comes from the battery (discharging), so
    # battery_power (positive-on-discharge, matching the live path's pbat) must be +500.
    discharging = [{"uploadTime": "2026-08-22 20:09:04", "ppv": 1000.0, "load": 1500.0, "cbat": 50.0, "feedIn": 0.0, "gridCharge": 0.0}]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, discharging)))):
        run_async_local(client.fetch_device_history("AL70"))
    battery_power = client.device_values.get("AL70", {}).get("battery_power")
    if battery_power is None or abs(battery_power - 500.0) > 0.001:
        print(f"ERROR: discharging battery_power {battery_power} != 500.0")
        failed = True

    # PV surplus over load charges the battery, so battery_power must be -2500.
    charging = [{"uploadTime": "2026-08-22 20:14:04", "ppv": 3000.0, "load": 500.0, "cbat": 55.0, "feedIn": 0.0, "gridCharge": 0.0}]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, charging)))):
        run_async_local(client.fetch_device_history("AL70"))
    battery_power = client.device_values.get("AL70", {}).get("battery_power")
    if battery_power is None or abs(battery_power - (-2500.0)) > 0.001:
        print(f"ERROR: charging battery_power {battery_power} != -2500.0")
        failed = True
    assert not failed, "test_alphaess_history_derives_battery_power_from_the_energy_balance"


def test_alphaess_live_demotion_latches_and_is_reversible():
    """~288 records is too big for a 60-second loop, so demotion latches after N failures
    and is re-probed on the config tier so a transient failure self-heals."""
    failed = False
    from alphaess_const import ALPHAESS_LIVE_FAIL_LIMIT

    client = MockAlphaESS()
    client.device_list = ["AL70"]
    history = [{"uploadTime": "2026-08-22 20:09:04", "ppv": 0.0, "load": 10.0, "cbat": 50.0, "feedIn": 0.0, "gridCharge": 0.0}]
    for _ in range(ALPHAESS_LIVE_FAIL_LIMIT):
        responses = [
            create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None)),
            create_aiohttp_mock_response(status=200, json_data=_envelope(200, history)),
        ]
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
            run_async_local(client.fetch_device_data("AL70"))
    if client._live_ok.get("AL70") is not False:
        print(f"ERROR: not demoted after {ALPHAESS_LIVE_FAIL_LIMIT} failures: {client._live_ok}")
        failed = True
    # Once demoted, the live endpoint is not called again on the power tier: a single
    # history response is enough to satisfy the whole call.
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, history)))):
        ok = run_async_local(client.fetch_device_data("AL70"))
    if not ok:
        print("ERROR: demoted fetch failed")
        failed = True
    # The config tier re-probes, and success restores 60-second live data.
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, LAST_POWER_SAMPLE)))):
        run_async_local(client.reprobe_live("AL70"))
    if client._live_ok.get("AL70") is not True:
        print(f"ERROR: re-probe did not restore live data: {client._live_ok}")
        failed = True
    assert not failed, "test_alphaess_live_demotion_latches_and_is_reversible"


def test_alphaess_serial_with_no_soc_on_either_path_is_reported():
    """No SOC on either path means the serial cannot be driven - say so, do not invent one."""
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, [])),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_device_data("AL70"))
    if ok:
        print("ERROR: fetch_device_data claimed success with no SOC")
        failed = True
    if "soc" in client.device_values.get("AL70", {}):
        print(f"ERROR: a SOC was invented: {client.device_values}")
        failed = True
    if not any("AL70" in message and "soc" in message.lower() for message in client.log_messages):
        print(f"ERROR: no explanatory log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_serial_with_no_soc_on_either_path_is_reported"


def test_alphaess_power_tier_ttl_returns_correct_interval_based_on_demotion_state():
    """power_tier_ttl returns the longer interval once any serial is demoted to history.

    getOneDayPowerBySn returns ~288 records for a full day, so it must not sit on a
    60-second loop. The interval doubles when any serial enters the history path.
    """
    from alphaess_const import ALPHAESS_TTL_POWER, ALPHAESS_TTL_POWER_DEMOTED

    failed = False
    client = MockAlphaESS()

    # No serials demoted - empty _live_ok dict
    if client.power_tier_ttl() != ALPHAESS_TTL_POWER:
        print(f"ERROR: empty _live_ok should return short interval {ALPHAESS_TTL_POWER}, got {client.power_tier_ttl()}")
        failed = True

    # No serials demoted - all True
    client._live_ok = {"AL70": True, "AL71": True}
    if client.power_tier_ttl() != ALPHAESS_TTL_POWER:
        print(f"ERROR: all True _live_ok should return short interval {ALPHAESS_TTL_POWER}, got {client.power_tier_ttl()}")
        failed = True

    # At least one demoted
    client._live_ok = {"AL70": False}
    if client.power_tier_ttl() != ALPHAESS_TTL_POWER_DEMOTED:
        print(f"ERROR: demoted serial should return long interval {ALPHAESS_TTL_POWER_DEMOTED}, got {client.power_tier_ttl()}")
        failed = True

    # Mix of demoted and healthy - should still return demoted (longer) interval
    client._live_ok = {"AL70": True, "AL71": False, "AL72": True}
    if client.power_tier_ttl() != ALPHAESS_TTL_POWER_DEMOTED:
        print(f"ERROR: mix with one demoted should return long interval {ALPHAESS_TTL_POWER_DEMOTED}, got {client.power_tier_ttl()}")
        failed = True

    assert not failed, "test_alphaess_power_tier_ttl_returns_correct_interval_based_on_demotion_state"


def test_alphaess_reprobe_live_failure_leaves_serial_demoted():
    """A failed reprobe_live does not restore the serial or reset the fail count."""
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    # Mark it as demoted
    client._live_ok["AL70"] = False
    client._live_fail_count["AL70"] = 5
    # Try to reprobe but it still fails
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        ok = run_async_local(client.reprobe_live("AL70"))
    if ok:
        print("ERROR: reprobe_live returned True when live data still unavailable")
        failed = True
    if client._live_ok.get("AL70") is not False:
        print(f"ERROR: reprobe failure should keep serial demoted, got {client._live_ok.get('AL70')}")
        failed = True
    if client._live_fail_count.get("AL70") != 5:
        print(f"ERROR: reprobe failure should not reset fail count, got {client._live_fail_count.get('AL70')} != 5")
        failed = True
    assert not failed, "test_alphaess_reprobe_live_failure_leaves_serial_demoted"


ONE_DATE_ENERGY_SAMPLE = {"sysSn": "AL70", "theDate": "2026-08-22", "eCharge": 12.2, "epv": 10.6, "eOutput": 0.42, "eInput": 14.41, "eGridCharge": 9.1, "eDischarge": 7.1, "eChargingPile": 0.0}


def test_alphaess_energy_counters_map_to_predbat_args():
    """The daily kWh counters that feed Predbat's load/import/export/PV learning."""
    failed = False
    client = MockAlphaESS()
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, ONE_DATE_ENERGY_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, {"eload": 19.49, "epvtoday": 10.6})),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_device_energy("AL70"))
    energy = client.device_energy.get("AL70", {})
    if not ok:
        print("ERROR: fetch_device_energy returned False")
        failed = True
    for leaf, expect in (("import_today", 14.41), ("export_today", 0.42), ("pv_today", 10.6), ("load_today", 19.49)):
        if abs(energy.get(leaf, -1) - expect) > 0.001:
            print(f"ERROR: {leaf} {energy.get(leaf)} != {expect}")
            failed = True
    assert not failed, "test_alphaess_energy_counters_map_to_predbat_args"


def test_alphaess_load_today_falls_back_to_the_energy_balance():
    """getOneDateEnergyBySn has no load field at all, and most SumData fields come back
    null without a configured tariff, so eload needs a fallback."""
    failed = False
    client = MockAlphaESS()
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, ONE_DATE_ENERGY_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, {"eload": None})),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        run_async_local(client.fetch_device_energy("AL70"))
    # epv + eInput - eOutput - eCharge + eDischarge = 10.6 + 14.41 - 0.42 - 12.2 + 7.1
    expect = 10.6 + 14.41 - 0.42 - 12.2 + 7.1
    got = client.device_energy.get("AL70", {}).get("load_today")
    if got is None or abs(got - expect) > 0.001:
        print(f"ERROR: load_today fallback {got} != {expect}")
        failed = True
    assert not failed, "test_alphaess_load_today_falls_back_to_the_energy_balance"


CHARGE_CONFIG_SAMPLE = {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
DISCHARGE_CONFIG_SAMPLE = {"sysSn": "AL70", "ctrDis": 0, "timeDisf1": "00:00", "timeDise1": "00:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 20}


def test_alphaess_fetch_config_populates_both_directions_when_both_reads_succeed():
    """Both getChargeConfigInfo and getDisChargeConfigInfo landing populates device_config
    and returns True - nothing reads device_config as a write baseline today (both payload
    builders are full replacements built from the schedule), but Tasks 10b and 11 will."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_config("AL70"))
    if not ok:
        print("ERROR: fetch_config returned False when both reads succeeded")
        failed = True
    entry = client.device_config.get("AL70", {})
    if entry.get("charge") != CHARGE_CONFIG_SAMPLE:
        print(f"ERROR: charge config not stored: {entry.get('charge')}")
        failed = True
    if entry.get("discharge") != DISCHARGE_CONFIG_SAMPLE:
        print(f"ERROR: discharge config not stored: {entry.get('discharge')}")
        failed = True
    assert not failed, "test_alphaess_fetch_config_populates_both_directions_when_both_reads_succeed"


def test_alphaess_fetch_config_partial_failure_keeps_the_stale_half_and_returns_false():
    """Only the charge read succeeding must not report overall success, and must not wipe
    out whatever discharge value was already known from a previous cycle - fetch_config
    keeps partial data rather than blanking a field it could not refresh this time."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    client.device_config["AL70"] = {"discharge": DISCHARGE_CONFIG_SAMPLE}
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_config("AL70"))
    if ok:
        print("ERROR: fetch_config returned True with only one of two reads succeeding")
        failed = True
    entry = client.device_config.get("AL70", {})
    if entry.get("charge") != CHARGE_CONFIG_SAMPLE:
        print(f"ERROR: the successful charge read was not stored: {entry.get('charge')}")
        failed = True
    # The failed discharge read must keep whatever was already known, not be cleared.
    if entry.get("discharge") != DISCHARGE_CONFIG_SAMPLE:
        print(f"ERROR: the previous cycle's discharge value was lost on a failed read: {entry.get('discharge')}")
        failed = True
    if not any("discharge" in message.lower() and "AL70" in message for message in client.log_messages):
        print(f"ERROR: the failed half was not named in the log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_fetch_config_partial_failure_keeps_the_stale_half_and_returns_false"


def test_alphaess_fetch_config_both_fail_returns_false():
    """Neither read succeeding is a plain failure with nothing to store."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    fail_response = create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(fail_response)):
        ok = run_async_local(client.fetch_config("AL70"))
    if ok:
        print("ERROR: fetch_config returned True when both reads failed")
        failed = True
    assert not failed, "test_alphaess_fetch_config_both_fail_returns_false"


def test_alphaess_fetch_config_reads_the_periodic_schedule_for_an_entitled_system():
    """Once a serial is entitled to the periodic API, fetch_config must ALSO read
    getTimeChargeBySn - otherwise note_external_change("charge"/"discharge", ...) can
    never find a match against applied_payload[sn]["periodic"] (writes for an entitled
    serial only ever populate that key), and interference on the serial's REAL write path
    goes undetected forever."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    client._periodic_ok["AL70"] = True
    periodic_sample = {
        "sysSn": "AL70",
        "gridChargeCycle": 1,
        "ctrDisCycle": 0,
        "chargeTimeList": [{"beginTime": "01:00", "endTime": "05:00", "chargeLimit": 90, "chargePower": 3000}],
        "dischargeTimeList": [{"beginTime": "00:00", "endTime": "00:00", "chargeLimit": 10}],
    }
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, periodic_sample)),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)) as session:
        ok = run_async_local(client.fetch_config("AL70"))
    if not ok:
        print("ERROR: fetch_config returned False when all three reads succeeded")
        failed = True
    if client.device_config.get("AL70", {}).get("periodic") != periodic_sample:
        print(f"ERROR: periodic schedule not stored: {client.device_config.get('AL70')}")
        failed = True
    calls = session.return_value.get.call_args_list
    if len(calls) != 3:
        print(f"ERROR: expected exactly 3 GETs (charge, discharge, periodic) for an entitled serial, got {len(calls)}: {calls}")
        failed = True
    elif "getTimeChargeBySn" not in calls[2].args[0]:
        print(f"ERROR: third GET was not the periodic schedule read: {calls[2]}")
        failed = True
    assert not failed, "test_alphaess_fetch_config_reads_the_periodic_schedule_for_an_entitled_system"


def test_alphaess_fetch_config_skips_the_periodic_read_when_not_entitled():
    """The un-entitled (legacy) case must cost exactly the two legacy reads - no wasted
    call to getTimeChargeBySn, which would just answer 6017 again."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)) as session:
        ok = run_async_local(client.fetch_config("AL70"))
    if not ok:
        print("ERROR: fetch_config returned False for a plain (non-entitled) serial")
        failed = True
    calls = session.return_value.get.call_args_list
    if len(calls) != 2:
        print(f"ERROR: expected exactly 2 GETs when not entitled, got {len(calls)}: {calls}")
        failed = True
    assert not failed, "test_alphaess_fetch_config_skips_the_periodic_read_when_not_entitled"


def test_alphaess_fetch_config_periodic_read_failure_does_not_stall_the_tier():
    """The periodic schedule read is supplementary, layered on top of the two legacy reads
    that already govern every serial. A transient failure on it alone must not make
    fetch_config report overall failure - it is not on the write path at all, and gating on
    it would stall the 30-minute config tier clock and repeat all three GETs on every
    60-second poll until it recovers."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    client._periodic_ok["AL70"] = True
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_config("AL70"))
    if not ok:
        print("ERROR: fetch_config returned False for a transient periodic-only failure")
        failed = True
    if "periodic" in client.device_config.get("AL70", {}):
        print(f"ERROR: a failed periodic read should not have stored anything: {client.device_config.get('AL70')}")
        failed = True
    if not any("periodic" in message.lower() and "AL70" in message for message in client.log_messages):
        print(f"ERROR: no warning naming the failed periodic read, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_fetch_config_periodic_read_failure_does_not_stall_the_tier"


def test_alphaess_refresh_config_tier_advances_despite_a_periodic_read_failure():
    """got_any (and therefore the 30-minute config tier clock) must depend only on the two
    legacy reads that govern every serial - not on the periodic read, which is entitled-
    serials-only and supplementary."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    client.device_list = ["AL70"]
    client._periodic_ok["AL70"] = True
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        got_any = run_async_local(client.refresh_config())
    if not got_any:
        print("ERROR: refresh_config reported failure despite both legacy reads succeeding")
        failed = True
    if client._tier_refreshed.get("config") is None:
        print("ERROR: the config tier clock was not started despite both legacy reads succeeding")
        failed = True
    assert not failed, "test_alphaess_refresh_config_tier_advances_despite_a_periodic_read_failure"


def test_alphaess_refresh_config_does_not_mark_refreshed_when_nothing_fully_succeeds():
    """A half-successful (or fully failed) read must not start the 30-minute config tier
    clock, or the stale half is left unrefreshed for a full TTL - the same class of bug
    Task 4's refresh_static had (marking a tier fresh on an unsuccessful poll)."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    client.device_list = ["AL70"]
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
        # refresh_config also probes periodic entitlement (getTimeChargeInfo) for a serial
        # not yet in _periodic_ok, which AL70 is not here. Without this third response the
        # mock queue is exhausted on that call: session.get() raises StopIteration, which
        # _request's transport-failure handler retries 3 times with a real (unmocked)
        # asyncio.sleep(1)/sleep(2) backoff - several real seconds for no test value, since
        # the probe's own result is not asserted on here.
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        got_any = run_async_local(client.refresh_config())
    if got_any:
        print("ERROR: refresh_config reported success with only a partial read")
        failed = True
    if client._tier_refreshed.get("config") is not None:
        print("ERROR: the config tier clock was started on a partial read")
        failed = True
    assert not failed, "test_alphaess_refresh_config_does_not_mark_refreshed_when_nothing_fully_succeeds"


def test_alphaess_refresh_config_marks_refreshed_when_at_least_one_serial_fully_succeeds():
    """One fully-successful serial is enough to start the tier clock, even if a sibling
    serial's read fails outright."""
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    client.device_list = ["AL70", "AL71"]
    responses = [
        # AL70: both reads succeed.
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
        # AL71: both reads fail.
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
        # refresh_config also probes periodic entitlement (getTimeChargeInfo) once per
        # serial not yet in _periodic_ok - both AL70 and AL71 qualify here. Without these
        # two extra responses the mock queue is exhausted on AL70's probe, and every GET
        # after that (AL71's discharge read and its own probe) hits the same exhausted-
        # queue-triggers-StopIteration-triggers-a-real-retry-sleep problem described above.
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        got_any = run_async_local(client.refresh_config())
    if not got_any:
        print("ERROR: refresh_config reported failure when one serial fully succeeded")
        failed = True
    if client._tier_refreshed.get("config") is None:
        print("ERROR: the config tier clock was not started despite one serial fully succeeding")
        failed = True
    assert not failed, "test_alphaess_refresh_config_marks_refreshed_when_at_least_one_serial_fully_succeeds"


def test_alphaess_bind_treats_already_bound_as_success():
    """bindSn answers data:null on success AND on failure, so only the code separates them.

    6003 "You have bound this SN" is an idempotent success, not an error to show a user.
    """
    failed = False
    for code, expect_ok in ((200, True), (6003, True), (6046, False), (6038, False)):
        client = MockAlphaESS()
        response = create_aiohttp_mock_response(status=200, json_data=_envelope(code, None))
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
            ok, message = run_async_local(client.bind_system("AL70", "123456"))
        if ok is not expect_ok:
            print(f"ERROR: bind code {code} -> ok {ok}, expected {expect_ok}")
            failed = True
        if not message:
            print(f"ERROR: bind code {code} produced no message")
            failed = True
    assert not failed, "test_alphaess_bind_treats_already_bound_as_success"


def test_alphaess_unbind_treats_not_bound_as_success():
    """6005 means the AppID was not bound to that SN in the first place - already gone."""
    failed = False
    for code, expect_ok in ((200, True), (6005, True), (6042, False)):
        client = MockAlphaESS()
        response = create_aiohttp_mock_response(status=200, json_data=_envelope(code, None))
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
            ok, _ = run_async_local(client.unbind_system("AL70"))
        if ok is not expect_ok:
            print(f"ERROR: unbind code {code} -> ok {ok}, expected {expect_ok}")
            failed = True
    assert not failed, "test_alphaess_unbind_treats_not_bound_as_success"


def test_alphaess_verification_code_is_a_get():
    """The portal describes a JSON body, which reads like a POST. It is GET with query
    parameters - a POST returns HTTP 405."""
    failed = False
    client = MockAlphaESS()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        ok, _ = run_async_local(client.request_verification_code("AL70", "CHECKCODE"))
    if not ok:
        print("ERROR: verification request failed")
        failed = True
    if session.post.called:
        print("ERROR: getVerificationCode was sent as a POST; it is GET only")
        failed = True
    assert not failed, "test_alphaess_verification_code_is_a_get"


def test_alphaess_bind_code_is_never_logged():
    """The one-time code is a credential-grade secret while it is valid."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        run_async_local(client.bind_system("AL70", "987654"))
    for message in client.log_messages:
        if "987654" in message:
            print(f"ERROR: bind code leaked in log: {message}")
            failed = True
    assert not failed, "test_alphaess_bind_code_is_never_logged"


def test_alphaess_run_defers_startup_without_telemetry():
    """automatic_config() runs on the FIRST cycle alone.

    Returning True with no telemetry would map only the args backed by cached ratings and
    permanently skip soc_max and the energy args for the whole session. Returning False
    leaves ComponentBase's `first` flag set so the whole startup path is retried.
    """
    failed = False
    client = MockAlphaESS(automatic=True)
    client.api_delay = 0
    # A full run() cycle over ESS_LIST_SAMPLE's 2 systems issues up to ~13 GET calls
    # (discovery, then per-serial config/probe/power/history/energy). The queue is padded
    # well past that so it is never exhausted: an exhausted MagicMock side_effect list
    # raises StopIteration on the next call, which _request's transport-failure handler
    # retries 3 times with real (unmocked) asyncio.sleep(1)/sleep(2) backoff between
    # attempts - costing tens of real seconds per exhausted call for no test value, since
    # every response after the first is a failure anyway and produces the same "no
    # telemetry" outcome this test wants.
    responses = [create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))] + [create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")) for _ in range(20)]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.run(seconds=0, first=True))
    if ok:
        print("ERROR: run() reported success with no telemetry on the first cycle")
        failed = True
    if "inverter_type" in client.base.args:
        print("ERROR: automatic_config ran without telemetry")
        failed = True
    assert not failed, "test_alphaess_run_defers_startup_without_telemetry"


def test_alphaess_run_returns_false_when_the_account_has_no_systems():
    """Deliberately explicit rather than falling through to an implicit None.

    ComponentBase only clears its `first` flag on a truthy return, so an accidental None
    would strand the component in the ever-growing startup backoff forever.
    """
    failed = False
    client = MockAlphaESS()
    empty = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(empty)):
        ok = run_async_local(client.run(seconds=0, first=True))
    if ok is not False:
        print(f"ERROR: run() returned {ok!r}, must be an explicit False")
        failed = True
    assert not failed, "test_alphaess_run_returns_false_when_the_account_has_no_systems"


def test_alphaess_run_restores_on_the_first_cycle_when_not_yet_restored():
    """The ordinary case: _cache_restored starts False, so the very first cycle must still
    call restore_state - the gate added to stop a redundant re-restore must not
    accidentally skip the genuinely first restore too."""
    failed = False
    client = MockAlphaESS()
    calls = []

    async def spy_restore_state():
        """Record that restore_state ran, then behave like a completed restore."""
        calls.append(1)
        client._cache_restored = True

    client.restore_state = spy_restore_state
    empty = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(empty)):
        run_async_local(client.run(seconds=0, first=True))
    if not calls:
        print("ERROR: restore_state was never called on the very first cycle")
        failed = True
    assert not failed, "test_alphaess_run_restores_on_the_first_cycle_when_not_yet_restored"


def test_alphaess_run_does_not_re_restore_once_cache_restored():
    """_cache_restored is written and asserted in storage tests but was never actually
    READ anywhere in production - run() called restore_state() on every cycle where
    `first` was still True, regardless of whether a previous attempt already fully
    restored it. A deferred startup (no systems found yet, telemetry not in) returns
    False and leaves `first` set, so every retry re-ran restore_state() and re-read
    storage for nothing, and - had anything already progressed in memory since the first
    successful restore - would have overwritten it with the stale on-disk copy. Gating on
    _cache_restored makes the field load-bearing rather than a write-only flag.
    """
    failed = False
    client = MockAlphaESS()
    client._cache_restored = True
    calls = []

    async def spy_restore_state():
        """Record whether restore_state was invoked."""
        calls.append(1)

    client.restore_state = spy_restore_state
    # No systems discovered, so run() returns False quickly - but that must not matter to
    # whether restore_state is (re)called, since _cache_restored is already True.
    empty = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(empty)):
        run_async_local(client.run(seconds=0, first=True))
    if calls:
        print(f"ERROR: restore_state was called again despite _cache_restored already being True: {calls}")
        failed = True
    assert not failed, "test_alphaess_run_does_not_re_restore_once_cache_restored"


def test_alphaess_run_reads_control_entities_on_every_tick_including_the_first():
    """Home Assistant retains the control entities across a Predbat restart, so on restart
    they already hold the live plan.

    Seeding local_schedule from _empty_schedule() and publishing that back would reach
    set_state_wrapper and cancel an in-flight charge until Predbat next replanned.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    client.mark_refreshed("static")
    client.mark_refreshed("config")
    client.mark_refreshed("power")
    client.mark_refreshed("energy")
    client.device_values["AL70"] = {"soc": 50.0}
    # A retained plan, as HA would hold it after a restart.
    client.state["switch.predbat_alphaess_al70_battery_schedule_charge_enable"] = "on"
    client.state["number.predbat_alphaess_al70_battery_schedule_charge_soc"] = 88
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        ok = run_async_local(client.run(seconds=0, first=True))
    # Deliberately `is True`, not merely truthy: this is the happy-path return that was
    # previously untested anywhere in this file - a mutation to `return None` on the final
    # successful-cycle path passed the whole suite until this assertion was added, even
    # though it is the most-hit line in production (every working cycle after the first
    # goes through it) and exactly the failure mode the "explicit True, never None"
    # rationale in run()'s own docstring exists to prevent.
    if ok is not True:
        print(f"ERROR: run() returned {ok!r} on a successful cycle, must be exactly True")
        failed = True
    schedule = client.local_schedule.get("AL70", {})
    if schedule.get("charge", {}).get("soc") != 88:
        print(f"ERROR: retained plan was overwritten: {schedule}")
        failed = True
    assert not failed, "test_alphaess_run_reads_control_entities_on_every_tick_including_the_first"


def test_alphaess_run_only_calls_automatic_config_on_the_first_cycle():
    """The `first and self.automatic` gate must not re-run automatic_config every tick.

    automatic_config() re-derives every apps.yaml binding from current state via
    set_arg_auto, which is idempotent for unchanged inputs - so checking "the args did
    not change on the second cycle" would not actually prove the gate exists, since it
    could pass by coincidence even with the guard weakened to `if self.automatic:` (no
    `first`). Spy on automatic_config directly instead, so the assertion is about whether
    it ran at all, not what it happened to write.
    """
    failed = False
    client = MockAlphaESS(automatic=True)
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    client.mark_refreshed("static")
    client.mark_refreshed("config")
    client.mark_refreshed("power")
    client.mark_refreshed("energy")
    client.device_values["AL70"] = {"soc": 50.0}
    calls = []

    async def fake_automatic_config():
        """Record a call instead of running the real automatic_config."""
        calls.append(1)

    client.automatic_config = fake_automatic_config
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        run_async_local(client.run(seconds=0, first=True))
        run_async_local(client.run(seconds=60, first=False))
    if len(calls) != 1:
        print(f"ERROR: automatic_config called {len(calls)} times across two cycles, expected exactly 1 (first cycle only)")
        failed = True
    assert not failed, "test_alphaess_run_only_calls_automatic_config_on_the_first_cycle"


def test_alphaess_final_persists_all_four_caches():
    """final() is the shutdown path and has no direct coverage otherwise - the storage
    tests call save_static/save_config/save_ratings/save_control individually, so a
    dropped or reordered call inside final() itself would go undetected.

    First confirms final() is safe to call on a freshly initialised client with nothing
    discovered and no Storage component configured (the standalone-CLI case) - none of
    the four save_* helpers should raise. Then spies on all four to confirm final() calls
    every one of them exactly once.
    """
    failed = False
    client = MockAlphaESS()
    try:
        run_async_local(client.final())
    except Exception as error:
        print(f"ERROR: final() raised on a fresh client with nothing discovered: {error}")
        failed = True

    calls = []
    for name in ("save_static", "save_config", "save_ratings", "save_control"):

        def make_spy(name=name):
            """Build a spy that records its own save_* name when awaited."""

            async def spy():
                """Record that this save_* method was called."""
                calls.append(name)

            return spy

        setattr(client, name, make_spy())
    run_async_local(client.final())
    expected = {"save_static", "save_config", "save_ratings", "save_control"}
    if set(calls) != expected or len(calls) != 4:
        print(f"ERROR: final() called {calls}, expected each of {sorted(expected)} exactly once")
        failed = True
    assert not failed, "test_alphaess_final_persists_all_four_caches"


def test_alphaess_ev_energy_sourced_from_echargingpile():
    """ev_energy_today must be SOURCED from eChargingPile, not merely publishable.

    Driving fetch_device_energy rather than seeding device_energy by hand is the point:
    a publish-only test passes even if the field is missing from ALPHAESS_ENERGY entirely.
    """
    failed = False
    client = MockAlphaESS()
    client.api_delay = 0
    payload = dict(ONE_DATE_ENERGY_SAMPLE, eChargingPile=6.4)
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, payload)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, {"eload": 19.49})),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        run_async_local(client.fetch_device_energy("AL70"))
    got = client.device_energy.get("AL70", {}).get("ev_energy_today")
    if got != 6.4:
        print(f"ERROR: ev_energy_today {got} != 6.4 - is eChargingPile mapped in ALPHAESS_ENERGY?")
        failed = True
    assert not failed, "test_alphaess_ev_energy_sourced_from_echargingpile"


def test_alphaess_history_carries_ev_power():
    """The history endpoint spells EV power pchargingPile, not pev.

    Without the mapping a demoted serial silently loses its ev_power sensor - the same
    trap battery_power fell into. A sample with no charger field must NOT fabricate a 0.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    with_ev = [{"uploadTime": "2026-08-23 10:00:00", "ppv": 0.0, "load": 900.0, "cbat": 50.0, "feedIn": 0.0, "gridCharge": 0.0, "pchargingPile": 7200.0}]
    if not client._apply_history_payload("AL70", with_ev):
        print("ERROR: history payload rejected")
        failed = True
    if client.device_values.get("AL70", {}).get("ev_power") != 7200.0:
        print(f"ERROR: ev_power from history {client.device_values.get('AL70', {}).get('ev_power')} != 7200.0")
        failed = True

    without = [{"uploadTime": "2026-08-23 10:00:00", "ppv": 0.0, "load": 900.0, "cbat": 50.0, "feedIn": 0.0, "gridCharge": 0.0}]
    client._apply_history_payload("AL70", without)
    if "ev_power" in client.device_values.get("AL70", {}):
        print("ERROR: a fabricated ev_power was published for a sample with no charger field")
        failed = True
    assert not failed, "test_alphaess_history_carries_ev_power"


def run_alphaess_api_tests(my_predbat):
    """Run all AlphaESS API tests."""
    failed = False
    for name, fn in [
        ("sign", test_alphaess_sign_matches_the_documented_algorithm),
        ("request_code_and_data", test_alphaess_request_returns_code_and_data),
        ("write_failure_distinguishable", test_alphaess_write_failure_is_distinguishable_from_success),
        ("info_field_success", test_alphaess_info_field_counts_as_success),
        ("clock_skew", test_alphaess_clock_skew_is_reported_as_a_clock_problem),
        ("expmsg_logged", test_alphaess_expmsg_is_logged_when_present),
        ("secret_redacted", test_alphaess_secret_never_reaches_the_log),
        ("transport_failure", test_alphaess_transport_failure_returns_minus_one),
        ("redact_function", test_alphaess_redact_function_masks_secrets_but_preserves_ordinary_keys),
        ("response_code_logged", test_alphaess_response_code_is_logged_even_with_debug_on),
        ("discovery_ratings", test_alphaess_discovery_maps_ratings),
        ("battery_rate_max_from_poinv", test_alphaess_battery_rate_max_defaults_to_the_inverter_limit),
        ("no_battery_skipped", test_alphaess_systems_without_a_battery_are_skipped),
        ("serial_filter", test_alphaess_serial_filter_restricts_discovery),
        ("serial_filter_mismatch", test_alphaess_serial_filter_mismatch_reports_the_filter_not_the_account),
        ("refresh_static_keeps_list", test_alphaess_refresh_static_never_clears_a_working_device_list),
        ("empty_vs_failed_discovery", test_alphaess_discovery_distinguishes_empty_account_from_failure),
        ("telemetry_signs", test_alphaess_telemetry_applies_predbat_sign_conventions),
        ("ppv_detail_all_null_signal", test_alphaess_ppv_detail_all_null_signal),
        ("history_fallback", test_alphaess_falls_back_to_history_when_live_data_is_unavailable),
        ("history_cbat_spelling", test_alphaess_history_reads_cbat_not_the_portal_spelling),
        ("history_derives_battery_power", test_alphaess_history_derives_battery_power_from_the_energy_balance),
        ("live_demotion_reversible", test_alphaess_live_demotion_latches_and_is_reversible),
        ("no_soc_reported", test_alphaess_serial_with_no_soc_on_either_path_is_reported),
        ("power_tier_ttl", test_alphaess_power_tier_ttl_returns_correct_interval_based_on_demotion_state),
        ("reprobe_live_failure", test_alphaess_reprobe_live_failure_leaves_serial_demoted),
        ("energy_counters", test_alphaess_energy_counters_map_to_predbat_args),
        ("load_today_fallback", test_alphaess_load_today_falls_back_to_the_energy_balance),
        ("ev_energy_sourced", test_alphaess_ev_energy_sourced_from_echargingpile),
        ("history_ev_power", test_alphaess_history_carries_ev_power),
        ("fetch_config_both_succeed", test_alphaess_fetch_config_populates_both_directions_when_both_reads_succeed),
        ("fetch_config_partial_failure", test_alphaess_fetch_config_partial_failure_keeps_the_stale_half_and_returns_false),
        ("fetch_config_both_fail", test_alphaess_fetch_config_both_fail_returns_false),
        ("fetch_config_reads_periodic_when_entitled", test_alphaess_fetch_config_reads_the_periodic_schedule_for_an_entitled_system),
        ("fetch_config_skips_periodic_when_not_entitled", test_alphaess_fetch_config_skips_the_periodic_read_when_not_entitled),
        ("fetch_config_periodic_failure_not_stalling", test_alphaess_fetch_config_periodic_read_failure_does_not_stall_the_tier),
        ("refresh_config_advances_despite_periodic_failure", test_alphaess_refresh_config_tier_advances_despite_a_periodic_read_failure),
        ("refresh_config_not_marked_on_partial", test_alphaess_refresh_config_does_not_mark_refreshed_when_nothing_fully_succeeds),
        ("refresh_config_marked_on_one_full_success", test_alphaess_refresh_config_marks_refreshed_when_at_least_one_serial_fully_succeeds),
        ("bind_already_bound_ok", test_alphaess_bind_treats_already_bound_as_success),
        ("unbind_not_bound_ok", test_alphaess_unbind_treats_not_bound_as_success),
        ("verification_is_get", test_alphaess_verification_code_is_a_get),
        ("bind_code_redacted", test_alphaess_bind_code_is_never_logged),
        ("run_defers_without_telemetry", test_alphaess_run_defers_startup_without_telemetry),
        ("run_empty_account", test_alphaess_run_returns_false_when_the_account_has_no_systems),
        ("run_restores_first_cycle", test_alphaess_run_restores_on_the_first_cycle_when_not_yet_restored),
        ("run_does_not_re_restore", test_alphaess_run_does_not_re_restore_once_cache_restored),
        ("run_reads_controls_first_tick", test_alphaess_run_reads_control_entities_on_every_tick_including_the_first),
        ("run_automatic_config_first_cycle_only", test_alphaess_run_only_calls_automatic_config_on_the_first_cycle),
        ("final_persists_all_caches", test_alphaess_final_persists_all_four_caches),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
