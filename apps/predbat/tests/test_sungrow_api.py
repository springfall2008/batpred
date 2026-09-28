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
from coordinator import CAPABILITY_KEYS, validate_report
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockSungrow(SungrowAPI):
    """Test double: build a SungrowAPI without the full component lifecycle."""

    def __init__(
        self, appkey="sungrow-test-appkey", access_key="sungrow-access-key", key="test-access-token", auth_method="oauth", inverter_sn=None, control_enable=True, automatic=False, min_write_interval=0, heartbeat_interval=300, forced_charge_schedule=False
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
    # PV appears in NO energy-storage inverter point, so it can only come from the plant.
    if client.telemetry("987654", "pv_power") != 2500:
        print(f"ERROR: pv_power {client.telemetry('987654', 'pv_power')}, expected the plant fallback 2500")
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
    """The OpenAPI reports energy in Wh throughout; Predbat works in kWh."""
    failed = False
    client = _discovered_client()
    for leaf, expected in (("load_today", 9.5), ("import_today", 4.2), ("export_today", 1.1), ("pv_today", 12.3)):
        if client.energy("987654", leaf) != expected:
            print(f"ERROR: {leaf} is {client.energy('987654', leaf)}, expected {expected} kWh")
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
    # export_limit is deliberately never bound - nothing reports the site's grid connection cap.
    if "export_limit" in args:
        print("ERROR: export_limit must not be auto-bound")
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


def run_sungrow_api_tests(my_predbat):
    """Run all Sungrow API tests."""
    failed = False
    for name, fn in [
        ("request_credentials", test_sungrow_request_sends_appkey_and_access_key),
        ("string_result_code", test_sungrow_request_treats_string_result_code_as_success),
        ("retry_on_401", test_sungrow_request_retries_once_on_401),
        ("skip_on_dead_token", test_sungrow_request_skips_the_call_when_refresh_fails),
        ("auth_and_rate_limit_codes", test_sungrow_auth_and_rate_limit_codes_are_reported_differently),
        ("device_realtime_device_type", test_sungrow_device_realtime_sends_the_device_type),
        ("redact", test_sungrow_redact_masks_credentials_but_not_errors),
        ("discovery", test_sungrow_discovery_finds_plants_and_energy_storage_inverters),
        ("discovery_failure_keeps_list", test_sungrow_discovery_failure_keeps_the_previous_device_list),
        ("serial_filter", test_sungrow_inverter_sn_filter),
        ("telemetry_prefers_device", test_sungrow_telemetry_prefers_the_device_read_over_the_plant_read),
        ("telemetry_falls_back", test_sungrow_telemetry_falls_back_when_the_device_read_fails),
        ("energy_units", test_sungrow_energy_counters_convert_watt_hours_to_kilowatt_hours),
        ("battery_capacity", test_sungrow_battery_capacity_sums_the_two_halves),
        ("battery_rate_max", test_sungrow_battery_rate_max_prefers_the_override),
        ("publish_omits_missing", test_sungrow_publish_omits_values_the_inverter_did_not_report),
        ("automatic_config", test_sungrow_automatic_config_binds_sensors_and_controls),
        ("automatic_ignore_pv", test_sungrow_automatic_config_respects_ignore_pv),
        ("record_round_trips", test_sungrow_discovery_record_round_trips_and_rebuilds_the_row),
        ("record_none_before_discovery", test_sungrow_discovery_record_is_none_before_discovery),
        ("record_no_solar_claim", test_sungrow_discovery_record_omits_solar_when_pv_was_never_reported),
        ("run_defers_without_telemetry", test_sungrow_run_defers_startup_without_telemetry),
        ("run_requires_credentials", test_sungrow_run_requires_credentials),
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
