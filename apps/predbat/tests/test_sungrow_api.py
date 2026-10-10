# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sungrow iSolarCloud OpenAPI component
# -----------------------------------------------------------------------------

"""Tests for the Sungrow iSolarCloud OpenAPI component (``sungrow.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from sungrow import SungrowAPI, SUNGROW_CAPABILITIES
from sungrow_const import SUNGROW_NO_EXPORT_LIMIT, SUNGROW_TTL_STATIC
from coordinator import CAPABILITY_KEYS, validate_report
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockSungrow(SungrowAPI):
    """Test double: build a SungrowAPI without the full component lifecycle."""

    def __init__(
        self,
        appkey="sungrow-test-appkey",
        access_key="sungrow-access-key",
        key="test-access-token",
        auth_method="oauth",
        inverter_sn=None,
        control_enable=True,
        automatic=False,
        min_write_interval=0,
        heartbeat_interval=300,
        forced_charge_schedule=False,
        inverter_limit=None,
        export_limit=None,
    ):
        """Set up a minimal SungrowAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-sungrow-1"}
        # A real clock, not a MagicMock attribute: ComponentBase.minutes_now derives from
        # base.now_utc, so the mock base has to carry a coherent now_utc/midnight_utc pair.
        # set_mock_clock() below moves it; writing base.minutes_now alone does nothing.
        self.base.now_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.midnight_utc = self.base.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.minutes_now = 0
        self.api_stop = False
        self.state = {}
        self.published = {}
        # 0 rather than initialize()'s real 2s default: api_delay is a courtesy pause between
        # consecutive calls to a rate-limited cloud, and nothing here talks to the real cloud.
        # Left at 2 it is several real seconds of dead wall-clock per test.
        self.initialize(
            appkey=appkey,
            access_key=access_key,
            key=key,
            auth_method=auth_method,
            inverter_sn=inverter_sn,
            automatic=automatic,
            control_enable=control_enable,
            heartbeat_interval=heartbeat_interval,
            forced_charge_schedule=forced_charge_schedule,
            api_delay=0,
            min_write_interval=min_write_interval,
            inverter_limit=inverter_limit,
            export_limit=export_limit,
        )

    def set_mock_clock(self, minutes_now):
        """Move the mock base's clock to minutes_now past midnight."""
        self.base.now_utc = self.base.midnight_utc + timedelta(minutes=minutes_now)
        self.base.minutes_now = minutes_now

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass

    def refresh_discovery(self):
        """No-op for tests - build_discovery is exercised directly."""
        pass

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity instead of reaching Home Assistant."""
        self.published[entity] = {"state": state, "attributes": attributes}
        self.state[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back whatever the test (or dashboard_item) put in self.state."""
        return self.state.get(entity_id, default)

    def set_arg_auto(self, arg, value):
        """Record an auto-discovered apps.yaml binding."""
        self.base.args[arg] = value

    @property
    def storage(self):
        """No Storage component in unit tests - matches a standalone CLI run."""
        return None


def _envelope(result_data=None, code="1", message="success"):
    """Build an iSolarCloud response envelope.

    result_code is a STRING in every observed response, which is the whole reason the component
    coerces before comparing - a helper that emitted an int here would let a bug through.
    """
    return {"req_serial_num": "test", "result_code": code, "result_msg": message, "result_data": result_data}


PLANT_LIST = {"pageList": [{"ps_id": 1234567, "ps_name": "Test House"}]}
DEVICE_LIST = {"pageList": [{"uuid": 987654, "device_sn": "A2211TEST01", "device_type": 14, "device_name": "SH10RT", "device_model": "SH10RT", "ps_key": "1234567_14_1_1"}]}
PLANT_REALTIME = {
    "device_point_list": [
        {
            "ps_id": 1234567,
            "p83067": "2500",  # PV power, W
            "p83106": "800",  # load power, W
            "p83549": "-1700",  # grid active power, W
            "p83252": "64",  # battery level SoC, %
            "p83235": "3600",  # chargeable energy, Wh
            "p83236": "6400",  # dischargeable energy, Wh
            "p83233": "5000",  # max rechargeable power, W
            "p83234": "5000",  # max dischargeable power, W
            "p83118": "9500",  # daily load consumption, Wh
            "p83102": "4200",  # energy purchased today, Wh
            "p83072": "1100",  # feed-in energy today, Wh
            "p83022": "12300",  # daily yield, Wh
        }
    ]
}
DEVICE_REALTIME = {
    "device_point_list": [
        {
            "device_sn": "A2211TEST01",
            "p13141": "64",  # battery SoC, %
            "p13142": "98",  # battery SoH, %
            "p13126": "0",  # battery charge power, W
            "p13150": "1200",  # battery discharge power, W
            "p13119": "800",  # load power, W
            "p13121": "0",  # feed-in power, W
            "p13149": "400",  # purchased power, W
            "p13003": "2400",  # total DC (PV) power, W
            "p13112": "12000",  # daily PV yield, Wh
            "p13199": "9000",  # daily load consumption, Wh
            "p13147": "4000",  # energy purchased today, Wh
            "p13122": "1000",  # feed-in energy today, Wh
            "p13140": "9.6",  # battery capacity as reported - UNIT DISPUTED
        }
    ]
}


def _mock_post(responses):
    """Patch aiohttp.ClientSession so successive POSTs return the given JSON bodies in order."""
    mocks = [create_aiohttp_mock_response(json_data=body) for body in responses]
    return patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(mocks))


def test_sungrow_request_sends_appkey_and_access_key():
    """appkey travels in the BODY and x-access-key in the HEADERS, alongside the bearer token."""
    failed = False
    client = MockSungrow()
    headers = client._headers()
    if headers.get("x-access-key") != "sungrow-access-key":
        print(f"ERROR: x-access-key header {headers.get('x-access-key')}")
        failed = True
    if headers.get("Authorization") != "Bearer test-access-token":
        print(f"ERROR: Authorization header {headers.get('Authorization')}")
        failed = True
    if "appkey" in headers:
        print("ERROR: appkey must travel in the body, not the headers")
        failed = True

    captured = {}
    mock_response = create_aiohttp_mock_response(json_data=_envelope(PLANT_LIST))
    session = create_aiohttp_mock_session(mock_response)

    original_post = session.post

    def capture_post(url, headers=None, json=None):
        """Record the outgoing body then defer to the mock session."""
        captured["url"] = url
        captured["json"] = json
        return original_post(url, headers=headers, json=json)

    session.post = capture_post
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client._request("plant_list", {"page": 1}))
    if captured.get("json", {}).get("appkey") != "sungrow-test-appkey":
        print(f"ERROR: appkey not in the body: {captured.get('json')}")
        failed = True
    if not str(captured.get("url", "")).endswith("/openapi/platform/queryPowerStationList"):
        print(f"ERROR: url {captured.get('url')}")
        failed = True
    assert not failed, "test_sungrow_request_sends_appkey_and_access_key"


def test_sungrow_request_treats_string_result_code_as_success():
    """result_code is a string; comparing it to an int would make every success look like a failure."""
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST)]):
        ok, data = run_async_local(client._request("plant_list", {"page": 1}))
    if not ok:
        print("ERROR: a result_code of '1' was not treated as success")
        failed = True
    if data != PLANT_LIST:
        print(f"ERROR: result_data {data}")
        failed = True
    with _mock_post([_envelope(None, code="E00000", message="appkey invalid")]):
        ok, data = run_async_local(client._request("plant_list", {"page": 1}))
    if ok:
        print("ERROR: an error envelope was treated as success")
        failed = True
    if "appkey invalid" not in client.last_api_error:
        print(f"ERROR: last_api_error {client.last_api_error!r} does not carry the server's message")
        failed = True
    assert not failed, "test_sungrow_request_treats_string_result_code_as_success"


def test_sungrow_request_survives_a_body_that_is_not_json():
    """A gateway error page is a failed call, not an exception that unwinds the caller."""
    failed = False
    client = MockSungrow()
    response = create_aiohttp_mock_response(json_data=_envelope(PLANT_LIST))

    async def bad_json(*args, **kwargs):
        """Fail the way aiohttp does on a body that will not parse."""
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    response.json = bad_json
    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        ok, data = run_async_local(client._request("plant_list", {"page": 1}))
    if ok is not False or data is not None:
        print(f"ERROR: a non-JSON body returned ({ok}, {data})")
        failed = True
    assert not failed, "test_sungrow_request_survives_a_body_that_is_not_json"


def test_sungrow_request_retries_once_on_401():
    """A 401 refreshes the token exactly once and retries, as fox.py does."""
    failed = False
    client = MockSungrow()
    refreshed = {"count": 0}

    async def fake_refresh():
        """Pretend the oauth-refresh edge function handed back a new token."""
        refreshed["count"] += 1
        client.access_token = "new-token"
        return True

    client.handle_oauth_401 = fake_refresh
    responses = [create_aiohttp_mock_response(status=401, json_data={}), create_aiohttp_mock_response(json_data=_envelope(PLANT_LIST))]
    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok, data = run_async_local(client._request("plant_list", {"page": 1}))
    if not ok:
        print("ERROR: the retry after refresh did not succeed")
        failed = True
    if refreshed["count"] != 1:
        print(f"ERROR: refresh called {refreshed['count']} times, expected exactly 1")
        failed = True
    assert not failed, "test_sungrow_request_retries_once_on_401"


def test_sungrow_request_skips_the_call_when_refresh_fails():
    """A token that needs re-authorisation stops the call rather than sending a dead one."""
    failed = False
    client = MockSungrow()

    async def fake_check():
        """Pretend the token is unusable and cannot be refreshed."""
        return False

    client.check_and_refresh_oauth_token = fake_check
    with _mock_post([_envelope(PLANT_LIST)]):
        ok, _ = run_async_local(client._request("plant_list", {"page": 1}))
    if ok:
        print("ERROR: the call went ahead despite an unusable token")
        failed = True
    assert not failed, "test_sungrow_request_skips_the_call_when_refresh_fails"


def test_sungrow_token_auth_sends_the_pasted_token_and_never_refreshes():
    """A self-hosted user supplies a token from Sungrow's own flow; predbat.com cannot refresh it."""
    failed = False
    client = MockSungrow(auth_method="token", key="pasted-token")
    if client._headers().get("Authorization") != "Bearer pasted-token":
        print(f"ERROR: Authorization header {client._headers().get('Authorization')}")
        failed = True
    refreshes = []

    async def no_refresh():
        """Record any attempt to reach the refresh function."""
        refreshes.append(1)
        return False

    client._do_refresh = no_refresh
    with _mock_post([_envelope(PLANT_LIST)]):
        ok, _ = run_async_local(client._request("plant_list", {"page": 1}))
    if not ok or refreshes:
        print(f"ERROR: token auth gave ok={ok} with {len(refreshes)} refresh attempt(s)")
        failed = True
    assert not failed, "test_sungrow_token_auth_sends_the_pasted_token_and_never_refreshes"


def test_sungrow_auth_and_rate_limit_codes_are_reported_differently():
    """A credential rejection points at the credentials; a rate limit is a pacing signal.

    Both otherwise read as an ordinary API failure, which sends people looking at the inverter
    when the problem is the appkey, or treats a busy account as a malfunction.
    """
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(None, code="E00003", message="token invalid")]):
        run_async_local(client._request("plant_list", {"page": 1}))
    if not any("sungrow_appkey" in message for message in client.log_messages):
        print("ERROR: an auth error did not point at the credentials")
        failed = True

    client = MockSungrow()
    with _mock_post([_envelope(None, code="E999", message="too many requests")]):
        run_async_local(client._request("plant_list", {"page": 1}))
    rate_logs = [message for message in client.log_messages if "rate-limited" in message]
    if not rate_logs:
        print("ERROR: a rate-limit code was not recognised")
        failed = True
    elif not rate_logs[0].startswith("Info:"):
        print(f"ERROR: a rate limit was logged as {rate_logs[0][:20]!r}, expected Info - it is a pacing signal, not a fault")
        failed = True
    assert not failed, "test_sungrow_auth_and_rate_limit_codes_are_reported_differently"


def test_sungrow_device_realtime_sends_the_device_type():
    """getDeviceRealTimeData answers for ONE device type, so the request must name it.

    Omitting it is not an error the API reports usefully - it simply answers with nothing,
    which looks identical to a model that does not serve the endpoint.
    """
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    captured = {}
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope(DEVICE_REALTIME)))
    original_post = session.post

    def capture_post(url, headers=None, json=None):
        """Record the outgoing body so the device read's fields can be asserted."""
        captured["json"] = json
        return original_post(url, headers=headers, json=json)

    session.post = capture_post
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client.fetch_device_realtime("987654"))
    body = captured.get("json", {})
    if body.get("device_type") != "14":
        print(f"ERROR: device_type {body.get('device_type')}, expected the discovered 14")
        failed = True
    if body.get("ps_key_list") != ["1234567_14_1_1"]:
        print(f"ERROR: ps_key_list {body.get('ps_key_list')}")
        failed = True
    # point_id_list is capped at 100 by the API.
    if len(body.get("point_id_list") or []) > 100:
        print("ERROR: the point list exceeds the documented cap of 100")
        failed = True
    assert not failed, "test_sungrow_device_realtime_sends_the_device_type"


def test_sungrow_plant_list_asks_for_connected_plants_too():
    """valid_flag defaults to 1 (Normal) alone, so a Connected plant is silently missing.

    That looks exactly like an account with no plants and is not something a user could
    diagnose, so the request asks for both states explicitly.
    """
    failed = False
    client = MockSungrow()
    captured = {}
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope(PLANT_LIST)))
    original_post = session.post

    def capture_post(url, headers=None, json=None):
        """Record the outgoing body so the plant filter can be asserted."""
        captured["json"] = json
        return original_post(url, headers=headers, json=json)

    session.post = capture_post
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client.get_plants())
    flag = str(captured.get("json", {}).get("valid_flag", ""))
    if "3" not in flag:
        print(f"ERROR: valid_flag {flag!r} does not ask for Connected (3) plants")
        failed = True
    if "1" not in flag:
        print(f"ERROR: valid_flag {flag!r} does not ask for Normal (1) plants")
        failed = True
    assert not failed, "test_sungrow_plant_list_asks_for_connected_plants_too"


def test_sungrow_energy_prefers_the_device_counters():
    """The inverter's own daily counters describe THIS inverter; the plant's aggregate the site."""
    failed = False
    client = _discovered_client()
    # The fixtures differ on purpose: device 13199 says 9.0 kWh, plant 83118 says 9.5 kWh.
    if client.energy("987654", "load_today") != 9.0:
        print(f"ERROR: load_today {client.energy('987654', 'load_today')}, expected the device value 9.0")
        failed = True
    if client.energy("987654", "pv_today") != 12.0:
        print(f"ERROR: pv_today {client.energy('987654', 'pv_today')}, expected the device value 12.0")
        failed = True
    if client.telemetry("987654", "pv_power") != 2400:
        print(f"ERROR: pv_power {client.telemetry('987654', 'pv_power')}, expected the device value 2400")
        failed = True
    assert not failed, "test_sungrow_energy_prefers_the_device_counters"


def test_sungrow_reported_capacity_is_diagnostic_only():
    """13140 is documented as both kWh and Wh, so it must never reach soc_max."""
    failed = False
    client = _discovered_client()
    run_async_local(client.publish_data())
    published = client.published.get("sensor.predbat_sungrow_a2211test01_battery_capacity_reported")
    if published is None:
        print("ERROR: the reported capacity was not published as a diagnostic")
        failed = True
    elif "UNVERIFIED" not in published["attributes"].get("friendly_name", ""):
        print("ERROR: the reported capacity does not warn that its unit is unverified")
        failed = True
    # soc_max must still come from the unambiguous Wh derivation, not from 13140's 9.6.
    if client.battery_capacity("987654") != 10.0:
        print(f"ERROR: battery_capacity {client.battery_capacity('987654')}, expected the derived 10.0 rather than 13140")
        failed = True
    assert not failed, "test_sungrow_reported_capacity_is_diagnostic_only"


def test_sungrow_redact_masks_credentials_but_not_errors():
    """Credentials are masked; result_msg is not, because it is the only diagnostic we get."""
    failed = False
    redacted = SungrowAPI.redact({"appkey": "secret", "access_key": "secret", "result_msg": "appkey invalid", "result_code": "E001"})
    if redacted["appkey"] != "***" or redacted["access_key"] != "***":
        print(f"ERROR: credentials not masked: {redacted}")
        failed = True
    if redacted["result_msg"] != "appkey invalid" or redacted["result_code"] != "E001":
        print("ERROR: the server's own error text was masked, which defeats api_debug")
        failed = True
    assert not failed, "test_sungrow_redact_masks_credentials_but_not_errors"


def test_sungrow_redact_reaches_nested_fields_and_personal_data():
    """Plant listings carry an address and coordinates several levels down; logs get shared."""
    failed = False
    payload = {"result_code": "1", "result_data": {"pageList": [{"ps_id": 1, "ps_location": "1 Example Road", "latitude": 51.5, "longitude": -0.1, "ps_name": "Home"}], "token": "secret"}}
    safe = SungrowAPI.redact(payload)
    row = safe["result_data"]["pageList"][0]
    if row["ps_location"] != "***" or row["latitude"] != "***" or row["longitude"] != "***" or safe["result_data"]["token"] != "***":
        print(f"ERROR: nested fields were not masked: {safe}")
        failed = True
    if row["ps_id"] != 1 or safe["result_code"] != "1":
        print("ERROR: diagnostic fields were masked")
        failed = True
    if payload["result_data"]["pageList"][0]["ps_location"] != "1 Example Road":
        print("ERROR: redact modified the payload it was given")
        failed = True
    assert not failed, "test_sungrow_redact_reaches_nested_fields_and_personal_data"


def test_sungrow_debug_trace_redacts_even_when_json_fails():
    """A payload json cannot render still goes through redact before it reaches the log."""
    failed = False
    client = MockSungrow()
    # A tuple key is not a valid JSON object key, so json.dumps raises TypeError and the fallback runs.
    client.debug_api("response", "/test", {"token": "super-secret", ("a", "b"): 1})
    logged = " ".join(client.log_messages)
    if "/test" not in logged:
        print("ERROR: the fallback trace was not logged at all")
        failed = True
    if "super-secret" in logged:
        print("ERROR: the fallback trace logged a credential unmasked")
        failed = True
    assert not failed, "test_sungrow_debug_trace_redacts_even_when_json_fails"


def test_sungrow_discovery_finds_plants_and_energy_storage_inverters():
    """Discovery lists plants, then the energy-storage devices in each, keyed by UUID."""
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        ok = run_async_local(client.refresh_static())
    if not ok:
        print("ERROR: refresh_static reported failure")
        failed = True
    if client.plant_list != ["1234567"]:
        print(f"ERROR: plant_list {client.plant_list}")
        failed = True
    if client.device_list != ["987654"]:
        print(f"ERROR: device_list {client.device_list}")
        failed = True
    if client._serial("987654") != "A2211TEST01":
        print(f"ERROR: serial {client._serial('987654')}")
        failed = True
    if client.device_detail["987654"].get("ps_id") != "1234567":
        print("ERROR: the device was not tied back to its plant")
        failed = True
    assert not failed, "test_sungrow_discovery_finds_plants_and_energy_storage_inverters"


def test_sungrow_discovery_failure_keeps_the_previous_device_list():
    """One transient failure must not take a working component down for a whole static TTL.

    Absence of a result is not a result: assigning the empty list would also stamp the tier
    fresh, so a restart would restore nothing and skip re-discovery for the full eight hours.
    """
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    with _mock_post([_envelope(None, code="E00000", message="server busy")]):
        ok = run_async_local(client.refresh_static())
    if ok:
        print("ERROR: a failed discovery reported success")
        failed = True
    if client.device_list != ["987654"]:
        print(f"ERROR: the working device list was lost: {client.device_list}")
        failed = True
    assert not failed, "test_sungrow_discovery_failure_keeps_the_previous_device_list"


def test_sungrow_inverter_sn_filter():
    """A serial filter keeps only the requested inverter."""
    failed = False
    client = MockSungrow(inverter_sn="SOMEONE-ELSE")
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    if client.device_list:
        print(f"ERROR: the filter let through {client.device_list}")
        failed = True
    client = MockSungrow(inverter_sn="a2211test01")
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    if client.device_list != ["987654"]:
        print("ERROR: the filter is case sensitive")
        failed = True
    assert not failed, "test_sungrow_inverter_sn_filter"


def _discovered_client(**kwargs):
    """Return a client that has completed discovery and one telemetry poll."""
    client = MockSungrow(**kwargs)
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    with _mock_post([_envelope(PLANT_REALTIME), _envelope(DEVICE_REALTIME)]):
        run_async_local(client.refresh_power())
    return client


# Later tasks' tests use this name; it is the same client (discovery plus one telemetry poll).
_polled_client = _discovered_client


def test_sungrow_plant_figures_are_shared_between_inverters_in_one_plant():
    """Plant points describe the whole site; Predbat sums per-inverter sensors, so they must be split."""
    failed = False
    client = _polled_client()
    first = client.device_list[0]
    second = "111222"
    client.device_list.append(second)
    client.device_detail[second] = {"ps_id": client.device_detail[first]["ps_id"], "serial": "A2211TEST02"}
    if client.battery_capacity(first) != 5.0 or client.battery_capacity(second) != 5.0:
        print(f"ERROR: capacities {client.battery_capacity(first)}/{client.battery_capacity(second)}, expected the 10 kWh plant split 5/5")
        failed = True
    if client.battery_rate_max(second) != 2500.0:
        print(f"ERROR: rate max {client.battery_rate_max(second)}, expected 2500")
        failed = True
    # The second inverter has no device read. A plant total is not its load.
    if client.telemetry(second, "load_power") is not None or client.energy(second, "load_today") is not None:
        print("ERROR: a plant total was published as one inverter's own figure")
        failed = True
    # SoC is a percentage, so the plant value is still the best available estimate.
    if client.telemetry(second, "soc") != 64:
        print(f"ERROR: soc fallback {client.telemetry(second, 'soc')}")
        failed = True
    assert not failed, "test_sungrow_plant_figures_are_shared_between_inverters_in_one_plant"


def test_sungrow_telemetry_prefers_the_device_read_over_the_plant_read():
    """The device read describes THIS inverter; the plant read aggregates the whole site."""
    failed = False
    client = _discovered_client()
    # 13149 purchased 400 W, 13121 feed-in 0 W -> -400 W, which differs from the plant's -1700
    # precisely so the preference is observable.
    if client.telemetry("987654", "grid_power") != -400:
        print(f"ERROR: grid_power {client.telemetry('987654', 'grid_power')}, expected the device value -400")
        failed = True
    # 13126 charge 0, 13150 discharge 1200 -> +1200 (positive on discharge).
    if client.telemetry("987654", "battery_power") != 1200:
        print(f"ERROR: battery_power {client.telemetry('987654', 'battery_power')}")
        failed = True
    # 13003 Total DC Power is the inverter's own PV figure. The plant reports 2500 for the whole
    # site, so preferring the device's 2400 is what is being asserted here.
    if client.telemetry("987654", "pv_power") != 2400:
        print(f"ERROR: pv_power {client.telemetry('987654', 'pv_power')}, expected the device value 2400")
        failed = True
    if client.telemetry("987654", "battery_soh") != 98:
        print("ERROR: battery SoH did not come through from the device read")
        failed = True
    assert not failed, "test_sungrow_telemetry_prefers_the_device_read_over_the_plant_read"


def test_sungrow_telemetry_falls_back_when_the_device_read_fails():
    """getDeviceRealTimeData's request shape is INFERRED, so a failure there must not lose the plan."""
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    with _mock_post([_envelope(PLANT_REALTIME), _envelope(None, code="E00000", message="unknown interface")]):
        ok = run_async_local(client.refresh_power())
    if not ok:
        print("ERROR: the telemetry tier failed even though the plant read succeeded")
        failed = True
    for leaf, expected in (("soc", 64), ("pv_power", 2500), ("load_power", 800), ("grid_power", -1700)):
        if client.telemetry("987654", leaf) != expected:
            print(f"ERROR: {leaf} fell back to {client.telemetry('987654', leaf)}, expected {expected}")
            failed = True
    assert not failed, "test_sungrow_telemetry_falls_back_when_the_device_read_fails"


def test_sungrow_energy_counters_convert_watt_hours_to_kilowatt_hours():
    """The OpenAPI reports energy in Wh throughout; Predbat works in kWh.

    Checked on both sources, because they are converted in two different places - the device
    counters in fetch_device_realtime and the plant ones in fetch_plant_realtime - and a unit
    slip in either would be a 1000x error in Predbat's load and import history.
    """
    failed = False
    client = _discovered_client()
    for leaf, expected in (("load_today", 9.0), ("import_today", 4.0), ("export_today", 1.0), ("pv_today", 12.0)):
        if client.energy("987654", leaf) != expected:
            print(f"ERROR: device {leaf} is {client.energy('987654', leaf)}, expected {expected} kWh")
            failed = True

    # Now the plant fallback, with the device read unavailable.
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    with _mock_post([_envelope(PLANT_REALTIME), _envelope(None, code="E00000")]):
        run_async_local(client.refresh_power())
    for leaf, expected in (("load_today", 9.5), ("import_today", 4.2), ("export_today", 1.1), ("pv_today", 12.3)):
        if client.energy("987654", leaf) != expected:
            print(f"ERROR: plant {leaf} is {client.energy('987654', leaf)}, expected {expected} kWh")
            failed = True
    assert not failed, "test_sungrow_energy_counters_convert_watt_hours_to_kilowatt_hours"


def test_sungrow_battery_capacity_sums_the_two_halves():
    """Sungrow report no nameplate capacity, so it is derived from chargeable + dischargeable."""
    failed = False
    client = _discovered_client()
    # 3600 Wh still to go in + 6400 Wh still to come out == a 10 kWh usable pack.
    if client.battery_capacity("987654") != 10.0:
        print(f"ERROR: capacity {client.battery_capacity('987654')}, expected 10.0 kWh")
        failed = True
    # Missing either half must leave soc_max unbound rather than half-sized.
    client.plant_values["1234567"].pop("chargeable_energy")
    if client.battery_capacity("987654") != 0.0:
        print("ERROR: a missing half produced a capacity anyway")
        failed = True
    assert not failed, "test_sungrow_battery_capacity_sums_the_two_halves"


def test_sungrow_battery_rate_max_prefers_the_override():
    """The user's stated pack limit wins; otherwise the reported maximum powers are used."""
    failed = False
    client = _discovered_client()
    if client.battery_rate_max("987654") != 5000:
        print(f"ERROR: rate max {client.battery_rate_max('987654')}")
        failed = True
    client.battery_rate_max_override = 3600.0
    if client.battery_rate_max("987654") != 3600.0:
        print("ERROR: the override did not win")
        failed = True
    assert not failed, "test_sungrow_battery_rate_max_prefers_the_override"


def test_sungrow_publish_omits_values_the_inverter_did_not_report():
    """A missing point is absent, not a fabricated zero.

    Publishing 0 W of PV on an AC-coupled battery looks exactly like night time, and an arg
    pointing at a sensor that never appears is worse than an absent arg the user can fill in.
    """
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    sparse = {"device_point_list": [{"ps_id": 1234567, "p83252": "50", "p83106": "700"}]}
    with _mock_post([_envelope(sparse), _envelope(None, code="E00000")]):
        run_async_local(client.refresh_power())
    run_async_local(client.publish_data())
    if "sensor.predbat_sungrow_a2211test01_soc" not in client.published:
        print("ERROR: SoC was not published")
        failed = True
    if "sensor.predbat_sungrow_a2211test01_pv_power" in client.published:
        print("ERROR: an unreported PV power was published as a value")
        failed = True
    if "sensor.predbat_sungrow_a2211test01_battery_capacity" in client.published:
        print("ERROR: a capacity was published without both energy halves")
        failed = True
    assert not failed, "test_sungrow_publish_omits_values_the_inverter_did_not_report"


def test_sungrow_automatic_config_binds_sensors_and_controls():
    """Every arg Predbat needs is bound, and the sign flags are owned rather than inherited."""
    failed = False
    client = _discovered_client(automatic=True)
    run_async_local(client.automatic_config())
    args = client.base.args
    if args.get("inverter_type") != ["SungrowCloud"]:
        print(f"ERROR: inverter_type {args.get('inverter_type')}")
        failed = True
    if args.get("num_inverters") != 1:
        print(f"ERROR: num_inverters {args.get('num_inverters')}")
        failed = True
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "battery_rate_max", "load_today", "import_today", "export_today", "pv_today"):
        if not args.get(arg):
            print(f"ERROR: {arg} was not bound")
            failed = True
    for arg in (
        "reserve",
        "charge_start_time",
        "charge_end_time",
        "charge_limit",
        "charge_rate",
        "scheduled_charge_enable",
        "discharge_start_time",
        "discharge_end_time",
        "discharge_target_soc",
        "discharge_rate",
        "scheduled_discharge_enable",
        "schedule_write_button",
    ):
        if not args.get(arg):
            print(f"ERROR: control arg {arg} was not bound")
            failed = True
    # base.args is shared and NOT namespaced per inverter type, so a component that inverts its
    # own grid sensor leaves the flag set for every index. Owning them keeps an export from
    # reading as an import.
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        if args.get(flag) != [False]:
            print(f"ERROR: {flag} is {args.get(flag)}, expected [False]")
            failed = True
    # Nothing here has read back the feed-in limitation (see test_sungrow_automatic_config_binds_the_limits),
    # so export_limit must not be bound on an unknown value.
    if "export_limit" in args:
        print("ERROR: export_limit must not be auto-bound when it is not known")
        failed = True
    assert not failed, "test_sungrow_automatic_config_binds_sensors_and_controls"


def test_sungrow_automatic_config_respects_ignore_pv():
    """automatic_ignore_pv is the user's opt-out, so pv_power and pv_today are left unbound."""
    failed = False
    client = _discovered_client(automatic=True)
    client.automatic_ignore_pv = True
    run_async_local(client.automatic_config())
    if "pv_power" in client.base.args or "pv_today" in client.base.args:
        print("ERROR: PV args were bound despite automatic_ignore_pv")
        failed = True
    assert not failed, "test_sungrow_automatic_config_respects_ignore_pv"


def test_sungrow_automatic_config_binds_battery_temperature_when_reported():
    """The sensor is published already; Predbat's temperature model only sees it once it is bound."""
    failed = False
    client = _polled_client(automatic=True)
    uuid = client.device_list[0]
    run_async_local(client.automatic_config())
    if "battery_temperature" in client.base.args:
        print("ERROR: battery_temperature was bound although the inverter does not report it")
        failed = True
    client.device_values[uuid]["battery_temperature"] = 21.5
    run_async_local(client.automatic_config())
    expected = "sensor.predbat_sungrow_a2211test01_battery_temperature"
    if client.base.args.get("battery_temperature") != [expected] or client.base.args.get("battery_temperature_history") != expected:
        print(f"ERROR: temperature args {client.base.args.get('battery_temperature')} / {client.base.args.get('battery_temperature_history')}")
        failed = True
    assert not failed, "test_sungrow_automatic_config_binds_battery_temperature_when_reported"


def test_sungrow_automatic_config_only_binds_power_sensors_that_exist():
    """An arg pointing at a sensor that is never published is worse than no arg at all."""
    failed = False
    client = _polled_client(automatic=True)
    uuid = client.device_list[0]
    client.device_values[uuid].pop("pv_power", None)
    client.plant_values["1234567"].pop("total_active_power_of_pv", None)
    run_async_local(client.automatic_config())
    if "pv_power" in client.base.args:
        print(f"ERROR: pv_power was bound to {client.base.args['pv_power']} although nothing reports it")
        failed = True
    if client.base.args.get("soc_percent") != ["sensor.predbat_sungrow_a2211test01_soc"]:
        print(f"ERROR: soc_percent arg {client.base.args.get('soc_percent')}")
        failed = True
    assert not failed, "test_sungrow_automatic_config_only_binds_power_sensors_that_exist"


def test_sungrow_discovery_record_round_trips_and_rebuilds_the_row():
    """The record validates and carries every capability key with the INVERTER_DEF value."""
    failed = False
    client = _discovered_client()
    report = client.build_discovery()
    if report is None:
        print("ERROR: build_discovery returned None after discovery")
        assert False, "test_sungrow_discovery_record_round_trips_and_rebuilds_the_row"
    logs = []
    cleaned = validate_report(report, "sungrow", logs.append)
    records = cleaned.get("inverters") or []
    if len(records) != 1:
        print(f"ERROR: validate_report kept {len(records)} of 1 records: {logs}")
        failed = True
        assert not failed, "test_sungrow_discovery_record_round_trips_and_rebuilds_the_row"
    record = records[0]
    if record.get("inverter_type") != "SungrowCloud":
        print(f"ERROR: inverter_type {record.get('inverter_type')}")
        failed = True
    for key in CAPABILITY_KEYS:
        if record["capabilities"].get(key) != SUNGROW_CAPABILITIES[key]:
            print(f"ERROR: capability {key} did not survive the record")
            failed = True
    if record.get("ratings", {}).get("soc_max") != 10.0:
        print(f"ERROR: soc_max rating {record.get('ratings')}")
        failed = True
    # The site reported PV power, so it has solar.
    if "solar" not in record.get("functions", []):
        print(f"ERROR: functions {record.get('functions')}")
        failed = True
    assert not failed, "test_sungrow_discovery_record_round_trips_and_rebuilds_the_row"


def test_sungrow_discovery_record_is_none_before_discovery():
    """Nothing discovered means nothing to report, not an empty report."""
    failed = False
    client = MockSungrow()
    if client.build_discovery() is not None:
        print("ERROR: a report was built before anything was discovered")
        failed = True
    assert not failed, "test_sungrow_discovery_record_is_none_before_discovery"


def test_sungrow_discovery_record_omits_solar_when_pv_was_never_reported():
    """An AC-coupled site is recorded as battery-only rather than guessed at."""
    failed = False
    client = MockSungrow()
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    no_pv = {"device_point_list": [{"ps_id": 1234567, "p83252": "50", "p83235": "3600", "p83236": "6400"}]}
    with _mock_post([_envelope(no_pv), _envelope(None, code="E00000")]):
        run_async_local(client.refresh_power())
    record = client.build_discovery()["inverters"][0]
    if "solar" in record.get("functions", []):
        print("ERROR: solar was claimed without any PV point")
        failed = True
    if "battery" not in record.get("functions", []):
        print("ERROR: battery was not claimed")
        failed = True
    assert not failed, "test_sungrow_discovery_record_omits_solar_when_pv_was_never_reported"


def test_sungrow_run_defers_startup_without_telemetry():
    """automatic_config() runs on the first cycle alone, so a cold start without telemetry defers.

    Otherwise it would map only the args backed by nothing and permanently skip soc_max and the
    energy args for the whole session.
    """
    failed = False
    client = MockSungrow(automatic=True)

    async def no_limits():
        """Skip the feed-in limitation read-back; it is exercised on its own."""
        return None

    client.refresh_limits = no_limits
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST), _envelope(None, code="E00000"), _envelope(None, code="E00000")]):
        result = run_async_local(client.run(0, True))
    if result is not False:
        print(f"ERROR: run returned {result!r}, expected False to defer startup")
        failed = True
    if "soc_max" in client.base.args:
        print("ERROR: automatic_config ran despite the deferred startup")
        failed = True
    assert not failed, "test_sungrow_run_defers_startup_without_telemetry"


def test_sungrow_run_requires_credentials():
    """Missing application credentials is a configuration error, reported once and not retried blindly."""
    failed = False
    client = MockSungrow(appkey="", access_key="")
    if run_async_local(client.run(0, True)) is not False:
        print("ERROR: run succeeded without credentials")
        failed = True
    if not any("sungrow_appkey" in message for message in client.log_messages):
        print("ERROR: the missing-credential log does not name the config key")
        failed = True
    client = MockSungrow(key="")
    if run_async_local(client.run(0, True)) is not False:
        print("ERROR: run succeeded without an OAuth token")
        failed = True
    assert not failed, "test_sungrow_run_requires_credentials"


def test_sungrow_stale_telemetry_is_reported_without_failing_the_cycle():
    """Stale telemetry makes the component unhealthy; it must not fail run(), which would flag all of Predbat."""
    failed = False
    client = _polled_client()
    client.api_started = True
    stamps = []
    client.update_success_timestamp = lambda: stamps.append(1)

    async def no_telemetry():
        """Stand in for a telemetry refresh that reaches nothing."""
        return False

    client.refresh_power = no_telemetry

    async def no_limits():
        """Skip the feed-in limitation read-back; it is exercised on its own."""
        return None

    client.refresh_limits = no_limits
    client.mark_refreshed("power", age_minutes=6)
    if run_async_local(client.run(0, False)) is not True or len(stamps) != 1 or not client.is_alive() or client.health_message() is not None:
        print("ERROR: a single missed telemetry poll was treated as stale")
        failed = True
    client.mark_refreshed("power", age_minutes=20)
    result = run_async_local(client.run(0, False))
    if result is not True:
        print(f"ERROR: run returned {result!r} on stale telemetry; False here marks the whole of Predbat as errored")
        failed = True
    if len(stamps) != 1:
        print("ERROR: a cycle with 20-minute-old telemetry was stamped as a success")
        failed = True
    if client.is_alive() or "telemetry" not in str(client.health_message()):
        print(f"ERROR: stale telemetry was not reported: alive={client.is_alive()} message={client.health_message()}")
        failed = True
    run_async_local(client.run(0, False))
    if sum(1 for message in client.log_messages if "telemetry is" in message and "stale" in message) != 1:
        print("ERROR: the stale warning was not logged exactly once for the episode")
        failed = True
    assert not failed, "test_sungrow_stale_telemetry_is_reported_without_failing_the_cycle"


def test_sungrow_run_saves_discovery_when_it_refreshes():
    """The static cache must not depend on a clean shutdown to exist."""
    failed = False
    client = MockSungrow()
    saved = []

    async def capture(name, data):
        """Record which cache blobs are written."""
        saved.append(name)

    client.save_cache = capture

    async def no_limits():
        """Skip the feed-in limitation read-back; it is exercised on its own."""
        return None

    client.refresh_limits = no_limits
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST), _envelope(PLANT_REALTIME), _envelope(DEVICE_REALTIME)]):
        run_async_local(client.run(0, True))
    if "static" not in saved:
        print(f"ERROR: discovery was not cached when it ran; saved {saved}")
        failed = True
    assert not failed, "test_sungrow_run_saves_discovery_when_it_refreshes"


def test_sungrow_inverter_limit_comes_from_the_model_or_the_override():
    """The fixture inverter is an SH10RT, which is 10 kW; a configured value wins."""
    failed = False
    client = _polled_client()
    uuid = client.device_list[0]
    if client.inverter_limit(uuid) != 10000.0:
        print(f"ERROR: inverter limit {client.inverter_limit(uuid)}, expected 10000 from the model name")
        failed = True
    client.inverter_limit_override = 8000.0
    if client.inverter_limit(uuid) != 8000.0:
        print("ERROR: sungrow_inverter_limit did not override the model")
        failed = True
    client.inverter_limit_override = 0.0
    client.device_detail[uuid]["device_model"] = "Mystery"
    client.device_detail[uuid]["device_name"] = "Mystery"
    if client.inverter_limit(uuid) != 0.0:
        print("ERROR: an unrecognised model produced a rating")
        failed = True
    assert not failed, "test_sungrow_inverter_limit_comes_from_the_model_or_the_override"


def test_sungrow_export_limit_is_only_reported_when_it_can_be_trusted():
    """10013 has no documented unit, so it must agree with ratio x rating before it is believed."""
    failed = False
    client = _polled_client()
    uuid = client.device_list[0]
    if client.export_limit(uuid) is not None:
        print("ERROR: an export limit was reported before anything was read")
        failed = True
    client.device_limits[uuid] = {"feed_in_enabled": False, "feed_in_value": None, "feed_in_ratio": None}
    if client.export_limit(uuid) != SUNGROW_NO_EXPORT_LIMIT:
        print("ERROR: a disabled feed-in limitation was not reported as no limit")
        failed = True
    # 36.8% of the 10 kW rating is 3680 W: the value and the ratio agree.
    client.device_limits[uuid] = {"feed_in_enabled": True, "feed_in_value": 3680.0, "feed_in_ratio": 36.8}
    if client.export_limit(uuid) != 3680.0:
        print(f"ERROR: an agreeing limit gave {client.export_limit(uuid)}")
        failed = True
    # The same site if 10013 were in tens of watts: 368 against 3680 - do not guess.
    client.device_limits[uuid] = {"feed_in_enabled": True, "feed_in_value": 368.0, "feed_in_ratio": 36.8}
    if client.export_limit(uuid) is not None:
        print(f"ERROR: a limit that disagrees with its own ratio was reported as {client.export_limit(uuid)}")
        failed = True
    # A zero-export site agrees with itself at zero.
    client.device_limits[uuid] = {"feed_in_enabled": True, "feed_in_value": 0.0, "feed_in_ratio": 0.0}
    if client.export_limit(uuid) != 0.0:
        print(f"ERROR: a zero-export limit gave {client.export_limit(uuid)}")
        failed = True
    client.export_limit_override = 5000.0
    if client.export_limit(uuid) != 5000.0:
        print("ERROR: sungrow_export_limit did not override the read-back")
        failed = True
    assert not failed, "test_sungrow_export_limit_is_only_reported_when_it_can_be_trusted"


def test_sungrow_refresh_limits_reads_the_feed_in_parameters():
    """The limitation is read with the read-back flow; a failed read keeps what was known."""
    failed = False
    client = _polled_client()
    uuid = client.device_list[0]
    answers = [{"10012": "170", "10013": "3680", "10014": "368"}, None]

    async def fake_read(device, codes):
        """Return the next canned read-back."""
        return answers.pop(0)

    client.read_params = fake_read
    run_async_local(client.refresh_limits())
    expected = {"feed_in_enabled": True, "feed_in_value": 3680.0, "feed_in_ratio": 36.8}
    if client.device_limits.get(uuid) != expected:
        print(f"ERROR: limits read back as {client.device_limits.get(uuid)}")
        failed = True
    client._tier_refreshed.pop("limits", None)
    run_async_local(client.refresh_limits())
    if client.device_limits.get(uuid) != expected:
        print("ERROR: a failed read-back discarded the limits already known")
        failed = True
    assert not failed, "test_sungrow_refresh_limits_reads_the_feed_in_parameters"


def test_sungrow_automatic_config_binds_the_limits():
    """inverter_limit and export_limit are bound when known, and export_limit never on a guess."""
    failed = False
    client = _polled_client(automatic=True)
    uuid = client.device_list[0]
    run_async_local(client.automatic_config())
    if client.base.args.get("inverter_limit") != ["sensor.predbat_sungrow_a2211test01_inverter_limit"]:
        print(f"ERROR: inverter_limit arg {client.base.args.get('inverter_limit')}")
        failed = True
    if "export_limit" in client.base.args or not any("sungrow_export_limit" in message for message in client.log_messages):
        print("ERROR: an unknown export limit was bound, or the user was not told how to set it")
        failed = True
    client.device_limits[uuid] = {"feed_in_enabled": True, "feed_in_value": 3680.0, "feed_in_ratio": 36.8}
    run_async_local(client.publish_data())
    run_async_local(client.automatic_config())
    entity = "sensor.predbat_sungrow_a2211test01_export_limit"
    if client.base.args.get("export_limit") != [entity] or client.published.get(entity, {}).get("state") != 3680:
        print(f"ERROR: export_limit arg {client.base.args.get('export_limit')} sensor {client.published.get(entity)}")
        failed = True
    client.device_limits[uuid] = {"feed_in_enabled": False, "feed_in_value": None, "feed_in_ratio": None}
    run_async_local(client.automatic_config())
    if client.base.args.get("export_limit") != [SUNGROW_NO_EXPORT_LIMIT]:
        print(f"ERROR: a disabled limitation bound {client.base.args.get('export_limit')}")
        failed = True
    assert not failed, "test_sungrow_automatic_config_binds_the_limits"


def test_sungrow_failed_limits_read_backs_off():
    """A read-back nobody answers is not attempted again until the retry window has passed."""
    failed = False
    client = _polled_client()
    attempts = []

    async def failing_read(device, codes):
        """Record the attempt and report that the read-back did not complete."""
        attempts.append(device)
        return None

    client.read_params = failing_read
    run_async_local(client.refresh_limits())
    run_async_local(client.refresh_limits())
    if len(attempts) != 1:
        print(f"ERROR: a failed read-back was attempted {len(attempts)} times in a row, expected 1")
        failed = True
    # Just inside the retry window: still waiting.
    client.mark_refreshed("limits", age_minutes=SUNGROW_TTL_STATIC - 1)
    run_async_local(client.refresh_limits())
    if len(attempts) != 1:
        print("ERROR: the read-back was retried before the tier came due")
        failed = True
    client.mark_refreshed("limits", age_minutes=SUNGROW_TTL_STATIC + 1)
    run_async_local(client.refresh_limits())
    if len(attempts) != 2:
        print("ERROR: the read-back was never retried after the tier came due")
        failed = True
    assert not failed, "test_sungrow_failed_limits_read_backs_off"


def test_sungrow_run_reads_limits_after_the_control_loop():
    """The limits read-back must not sit in front of the battery's command or its sensors."""
    failed = False
    client = _polled_client()
    order = []

    async def record_limits():
        """Record when the limits read-back runs."""
        order.append("limits")

    async def record_reconcile(uuid):
        """Record when the control reconcile runs."""
        order.append("control")

    async def record_publish():
        """Record when the sensors are published."""
        order.append("publish")

    client.refresh_limits = record_limits
    client._reconcile_control = record_reconcile
    client.publish_data = record_publish
    run_async_local(client.run(0, False))
    if order != ["control", "limits", "publish"]:
        print(f"ERROR: run() order was {order}, expected control, limits, publish")
        failed = True
    assert not failed, "test_sungrow_run_reads_limits_after_the_control_loop"


def test_sungrow_run_binds_a_setting_that_becomes_available_later():
    """A point missing on the first cycle must not stay unbound until the next restart."""
    failed = False
    client = _polled_client(automatic=True)

    async def no_limits():
        """Skip the feed-in limitation read-back; it is exercised on its own."""
        return None

    client.refresh_limits = no_limits
    client.plant_values["1234567"].pop("chargeable_energy", None)
    run_async_local(client.run(0, True))
    if "soc_max" in client.base.args:
        print("ERROR: soc_max was bound without a capacity")
        failed = True
    runs = []
    original = client.automatic_config

    async def counting_config():
        """Count re-runs of automatic_config."""
        runs.append(1)
        await original()

    client.automatic_config = counting_config
    client.plant_values["1234567"]["chargeable_energy"] = 3600.0
    run_async_local(client.run(0, False))
    if client.base.args.get("soc_max") != ["sensor.predbat_sungrow_a2211test01_battery_capacity"] or len(runs) != 1:
        print(f"ERROR: after the capacity appeared soc_max is {client.base.args.get('soc_max')} with {len(runs)} re-run(s)")
        failed = True
    run_async_local(client.run(0, False))
    if len(runs) != 1:
        print(f"ERROR: automatic_config re-ran with nothing new to bind ({len(runs)} runs)")
        failed = True
    assert not failed, "test_sungrow_run_binds_a_setting_that_becomes_available_later"


def test_sungrow_ignore_pv_never_triggers_a_rerun():
    """With PV opted out, PV turning up later is not something automatic_config would bind, so it must not re-run."""
    failed = False
    client = _polled_client(automatic=True)
    client.automatic_ignore_pv = True
    uuid = client.device_list[0]

    async def no_limits():
        """Skip the feed-in limitation read-back; it is exercised on its own."""
        return None

    client.refresh_limits = no_limits
    pv_power = client.device_values[uuid].pop("pv_power", None)
    plant_pv = client.plant_values["1234567"].pop("total_active_power_of_pv", None)
    run_async_local(client.run(0, True))
    runs = []
    original = client.automatic_config

    async def counting_config():
        """Count re-runs of automatic_config."""
        runs.append(1)
        await original()

    client.automatic_config = counting_config
    client.device_values[uuid]["pv_power"] = pv_power if pv_power is not None else 2400.0
    client.plant_values["1234567"]["total_active_power_of_pv"] = plant_pv if plant_pv is not None else 2500.0
    run_async_local(client.run(0, False))
    if runs:
        print(f"ERROR: automatic_config re-ran {len(runs)} time(s) for PV the user opted out of")
        failed = True
    if "pv_power" in client.base.args:
        print("ERROR: pv_power was bound despite automatic_ignore_pv")
        failed = True
    assert not failed, "test_sungrow_ignore_pv_never_triggers_a_rerun"


def run_sungrow_api_tests(my_predbat):
    """Run all Sungrow API tests."""
    failed = False
    for name, fn in [
        ("request_credentials", test_sungrow_request_sends_appkey_and_access_key),
        ("string_result_code", test_sungrow_request_treats_string_result_code_as_success),
        ("non_json_body", test_sungrow_request_survives_a_body_that_is_not_json),
        ("retry_on_401", test_sungrow_request_retries_once_on_401),
        ("skip_on_dead_token", test_sungrow_request_skips_the_call_when_refresh_fails),
        ("token_auth", test_sungrow_token_auth_sends_the_pasted_token_and_never_refreshes),
        ("auth_and_rate_limit_codes", test_sungrow_auth_and_rate_limit_codes_are_reported_differently),
        ("device_realtime_device_type", test_sungrow_device_realtime_sends_the_device_type),
        ("plant_list_valid_flag", test_sungrow_plant_list_asks_for_connected_plants_too),
        ("energy_prefers_device", test_sungrow_energy_prefers_the_device_counters),
        ("capacity_diagnostic_only", test_sungrow_reported_capacity_is_diagnostic_only),
        ("redact", test_sungrow_redact_masks_credentials_but_not_errors),
        ("redact_nested", test_sungrow_redact_reaches_nested_fields_and_personal_data),
        ("debug_fallback_redacts", test_sungrow_debug_trace_redacts_even_when_json_fails),
        ("discovery", test_sungrow_discovery_finds_plants_and_energy_storage_inverters),
        ("discovery_failure_keeps_list", test_sungrow_discovery_failure_keeps_the_previous_device_list),
        ("serial_filter", test_sungrow_inverter_sn_filter),
        ("telemetry_prefers_device", test_sungrow_telemetry_prefers_the_device_read_over_the_plant_read),
        ("telemetry_falls_back", test_sungrow_telemetry_falls_back_when_the_device_read_fails),
        ("energy_units", test_sungrow_energy_counters_convert_watt_hours_to_kilowatt_hours),
        ("battery_capacity", test_sungrow_battery_capacity_sums_the_two_halves),
        ("battery_rate_max", test_sungrow_battery_rate_max_prefers_the_override),
        ("multi_inverter_plant", test_sungrow_plant_figures_are_shared_between_inverters_in_one_plant),
        ("publish_omits_missing", test_sungrow_publish_omits_values_the_inverter_did_not_report),
        ("automatic_config", test_sungrow_automatic_config_binds_sensors_and_controls),
        ("automatic_ignore_pv", test_sungrow_automatic_config_respects_ignore_pv),
        ("temperature_bound", test_sungrow_automatic_config_binds_battery_temperature_when_reported),
        ("power_args_conditional", test_sungrow_automatic_config_only_binds_power_sensors_that_exist),
        ("record_round_trips", test_sungrow_discovery_record_round_trips_and_rebuilds_the_row),
        ("record_none_before_discovery", test_sungrow_discovery_record_is_none_before_discovery),
        ("record_no_solar_claim", test_sungrow_discovery_record_omits_solar_when_pv_was_never_reported),
        ("run_defers_without_telemetry", test_sungrow_run_defers_startup_without_telemetry),
        ("run_requires_credentials", test_sungrow_run_requires_credentials),
        ("stale_telemetry", test_sungrow_stale_telemetry_is_reported_without_failing_the_cycle),
        ("static_saved_on_discovery", test_sungrow_run_saves_discovery_when_it_refreshes),
        ("inverter_limit", test_sungrow_inverter_limit_comes_from_the_model_or_the_override),
        ("export_limit_trust", test_sungrow_export_limit_is_only_reported_when_it_can_be_trusted),
        ("limits_read_back", test_sungrow_refresh_limits_reads_the_feed_in_parameters),
        ("limits_bound", test_sungrow_automatic_config_binds_the_limits),
        ("limits_backoff", test_sungrow_failed_limits_read_backs_off),
        ("limits_after_control", test_sungrow_run_reads_limits_after_the_control_loop),
        ("late_binding", test_sungrow_run_binds_a_setting_that_becomes_available_later),
        ("ignore_pv_no_rerun", test_sungrow_ignore_pv_never_triggers_a_rerun),
    ]:
        try:
            if fn():
                print(f"  FAILED: sungrow_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sungrow_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
