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
    response_logs = [msg for msg in client.log_messages if "response" in msg.lower() or "6008" in msg]
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


def test_alphaess_refresh_static_never_clears_a_working_device_list():
    """Absence of a result is not a result.

    This tier re-runs every 8 hours in a long-lived process; one transient failure must
    not take a working component down until the next success, and it must not write an
    empty list to the cache and then stamp it fresh.
    """
    failed = False
    client = MockAlphaESS()
    ok = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok)):
        run_async_local(client.refresh_static())
    before = list(client.device_list)
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        run_async_local(client.refresh_static())
    if client.device_list != before:
        print(f"ERROR: device_list cleared by a transient failure: {client.device_list} != {before}")
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
        ("refresh_static_keeps_list", test_alphaess_refresh_static_never_clears_a_working_device_list),
        ("empty_vs_failed_discovery", test_alphaess_discovery_distinguishes_empty_account_from_failure),
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
