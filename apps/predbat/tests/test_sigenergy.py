# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the Sigenergy Cloud API integration component."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sigenergy import (
    SigenergyAPI,
    SIGENERGY_ACTIVE_MODE_CHARGE,
    SIGENERGY_ACTIVE_MODE_DISCHARGE,
    SIGENERGY_ACTIVE_MODE_SELF,
    SIGENERGY_CODE_IN_OTHER_VPP,
    SIGENERGY_CODE_SYSTEM_PENDING_REVIEW,
    SIGENERGY_MODE_MSC,
    SIGENERGY_MODE_VPP,
    SIGENERGY_OPTIONS_TIME,
    _safe_float,
    _safe_int,
)
from tests.test_infra import run_async as _base_run_async


def run_async(coro):
    """Like test_infra.run_async but makes all sleeps in sigenergy instant.

    Patches the retry/rate-limit delay constants to 0 so that asyncio.sleep(0)
    completes in a single event loop tick.  Also patches asyncio.sleep itself
    with AsyncMock as a belt-and-suspenders measure.
    """
    with patch("sigenergy.SIGENERGY_COMMAND_RETRY_DELAY", 0):
        with patch("sigenergy.SIGENERGY_MIN_REQUEST_INTERVAL", 0):
            with patch("sigenergy.asyncio.sleep", new_callable=AsyncMock):
                return _base_run_async(coro)


def _make_mock_response(status=200, json_data=None):
    """Create a mock aiohttp response that accepts json(content_type=...) kwargs."""
    mock_resp = MagicMock()
    mock_resp.status = status

    async def return_json(*args, **kwargs):
        return json_data or {}

    mock_resp.json = return_json

    async def aenter(*args, **kwargs):
        return mock_resp

    async def aexit(*args, **kwargs):
        pass

    mock_resp.__aenter__ = aenter
    mock_resp.__aexit__ = aexit
    return mock_resp


def _make_mock_session(mock_response):
    """Create a mock aiohttp ClientSession for sigenergy tests (supports get/post/put)."""
    mock_ctx = MagicMock()

    async def ctx_aenter(*args, **kwargs):
        return mock_response

    async def ctx_aexit(*args, **kwargs):
        pass

    mock_ctx.__aenter__ = ctx_aenter
    mock_ctx.__aexit__ = ctx_aexit

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_ctx)
    mock_session.post = MagicMock(return_value=mock_ctx)
    mock_session.put = MagicMock(return_value=mock_ctx)

    async def session_aenter(*args):
        return mock_session

    async def session_aexit(*args):
        pass

    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit
    return mock_session


# ---------------------------------------------------------------------------
# Mock class
# ---------------------------------------------------------------------------


class MockSigenergyAPI(SigenergyAPI):
    """Minimal SigenergyAPI subclass that bypasses ComponentBase initialisation."""

    def __init__(self, prefix="predbat"):
        # Manually initialise attributes that ComponentBase would provide
        self.prefix = prefix
        self.local_tz = timezone.utc
        self.log_messages = []
        self.dashboard_items = {}
        self.set_args = {}
        self.args = {}

        # Now call the SigenergyAPI initialize directly
        self.initialize(
            app_key="test_app_key",
            app_secret="test_app_secret",
            system_id=None,
            automatic=False,
            enable_controls=True,
        )
        # ComponentBase attributes not set by initialize() — wire them manually
        self.api_started = False
        self.api_stop = False
        # Skip mode-switch → command delay in unit tests
        self._command_delay = 0

    def log(self, message):
        """Capture log messages for assertion."""
        self.log_messages.append(message)

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Capture dashboard item publishes."""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes or {}}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return dashboard state or default."""
        if entity_id in self.dashboard_items:
            return self.dashboard_items[entity_id]["state"]
        return default

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Store state."""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes or {}}

    def get_arg(self, key, default=None):
        """Return stored arg or default."""
        return self.args.get(key, default)

    def set_arg(self, key, value):
        """Capture set_arg calls."""
        self.set_args[key] = value
        self.args[key] = value

    def update_success_timestamp(self):
        """No-op for tests."""
        pass

    async def _publish_mqtt(self, topic, payload_dict):
        """Mock MQTT publish — records calls and returns success."""
        if not hasattr(self, "mqtt_publishes"):
            self.mqtt_publishes = []
        self.mqtt_publishes.append((topic, payload_dict))
        return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sigenergy_helper_functions(my_predbat):
    """Test _safe_float and _safe_int helper functions."""
    failed = False

    # _safe_float
    assert _safe_float(3.14) == 3.14, "_safe_float: float passthrough"
    assert _safe_float("2.5") == 2.5, "_safe_float: string to float"
    assert _safe_float(None) == 0.0, "_safe_float: None → 0.0"
    assert _safe_float("abc") == 0.0, "_safe_float: invalid string → 0.0"
    assert _safe_float(None, default=99.0) == 99.0, "_safe_float: None with custom default"

    # _safe_int
    assert _safe_int(42) == 42, "_safe_int: int passthrough"
    assert _safe_int("7") == 7, "_safe_int: string to int"
    assert _safe_int(None) == 0, "_safe_int: None → 0"
    assert _safe_int("bad") == 0, "_safe_int: invalid → 0"
    assert _safe_int(None, default=5) == 5, "_safe_int: None with custom default"

    return failed


def test_sigenergy_initialize(my_predbat):
    """Test SigenergyAPI initialisation state."""
    failed = False
    api = MockSigenergyAPI()

    assert api.app_key == "test_app_key", "app_key stored"
    assert api.app_secret == "test_app_secret", "app_secret stored"
    assert api.access_token is None, "No token initially"
    assert api.token_expires_at == 0.0, "Token not yet obtained"
    assert api.systems == {}, "No systems initially"
    assert api.devices == {}, "No devices initially"
    assert api.controls == {}, "No controls initially"
    assert api.system_id_filter == set(), "No filter when system_id=None"

    # System ID filter — string
    api2 = MockSigenergyAPI()
    api2.initialize(app_key="k", app_secret="s", system_id="sys-1")
    assert api2.system_id_filter == {"sys-1"}, "Single system ID filter"

    # System ID filter — list
    api3 = MockSigenergyAPI()
    api3.initialize(app_key="k", app_secret="s", system_id=["sys-1", "sys-2"])
    assert api3.system_id_filter == {"sys-1", "sys-2"}, "Multi system ID filter"

    return failed


def test_sigenergy_system_slug(my_predbat):
    """Test _system_slug generates safe, short identifiers."""
    failed = False
    api = MockSigenergyAPI()

    # Long ID → last 12 chars
    slug = api._system_slug("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert len(slug) <= 12, "Slug max 12 chars: {}".format(slug)

    # Hyphens replaced
    api.systems["my-system-id"] = {"systemName": "Test"}
    slug = api._system_slug("my-system-id")
    assert "-" not in slug, "Hyphens removed: {}".format(slug)

    return failed


def test_sigenergy_battery_max_power(my_predbat):
    """Test _get_battery_max_power_kw with capping at inverter power."""
    failed = False
    api = MockSigenergyAPI()

    # Case 1: battery power below inverter limit — uncapped
    api.devices["sys1"] = [
        {"deviceType": "Battery", "attrMap": {"ratedChargePower": 6.0}},
        {"deviceType": "Inverter", "attrMap": {"ratedActivePower": 10.0}},
    ]
    assert api._get_battery_max_power_kw("sys1") == 6.0, "Battery power below inverter limit: not capped"

    # Case 2: battery power exceeds inverter limit — capped at inverter power
    api.devices["sys2"] = [
        {"deviceType": "Battery", "attrMap": {"ratedChargePower": 15.0}},
        {"deviceType": "Inverter", "attrMap": {"ratedActivePower": 10.0}},
    ]
    assert api._get_battery_max_power_kw("sys2") == 10.0, "Battery power capped at inverter limit"

    # Case 3: no battery device, falls back to inverter power
    api.devices["sys3"] = [
        {"deviceType": "Inverter", "attrMap": {"ratedActivePower": 8.0}},
    ]
    assert api._get_battery_max_power_kw("sys3") == 8.0, "Fallback to inverter power when no battery device"

    # Case 4: no inverter device — battery power returned uncapped
    api.devices["sys4"] = [
        {"deviceType": "Battery", "attrMap": {"ratedChargePower": 7.0}},
    ]
    assert api._get_battery_max_power_kw("sys4") == 7.0, "No inverter: battery power returned uncapped"

    # Case 5: multiple batteries — sum capped at inverter
    api.devices["sys5"] = [
        {"deviceType": "Battery", "attrMap": {"ratedChargePower": 6.0}},
        {"deviceType": "Battery", "attrMap": {"ratedChargePower": 6.0}},
        {"deviceType": "Inverter", "attrMap": {"ratedActivePower": 10.0}},
    ]
    assert api._get_battery_max_power_kw("sys5") == 10.0, "Sum of two batteries (12kW) capped at inverter (10kW)"

    return failed


def test_sigenergy_battery_capacity(my_predbat):
    """Test _get_battery_capacity_kwh falls back to device data."""
    failed = False
    api = MockSigenergyAPI()

    # From system info
    api.systems["sys1"] = {"batteryCapacity": 12.5}
    assert api._get_battery_capacity_kwh("sys1") == 12.5, "Capacity from system info"

    # Fallback to device attrMap — ratedEnergy is in Ah, converted via nominal voltage 28.8V
    # e.g. 314 Ah × 28.8V / 1000 = 9.0432 kWh per battery
    api.systems["sys2"] = {}
    api.devices["sys2"] = [
        {"deviceType": "Battery", "attrMap": {"ratedEnergy": 314}},
        {"deviceType": "Battery", "attrMap": {"ratedEnergy": 314}},
        {"deviceType": "Inverter", "attrMap": {"ratedActivePower": 5.0}},
    ]
    expected_kwh = 2 * 314 * 28.8 / 1000  # = 18.0864
    actual_kwh = api._get_battery_capacity_kwh("sys2")
    assert abs(actual_kwh - expected_kwh) < 0.001, "Capacity summed from Battery devices, expected {:.4f} got {:.4f}".format(expected_kwh, actual_kwh)

    return failed


def test_sigenergy_publish_system_entities(my_predbat):
    """Test publish_system_entities creates expected HA entities."""
    failed = False
    api = MockSigenergyAPI()

    system_id = "SIG12345"
    slug = api._system_slug(system_id)
    api.systems[system_id] = {"systemName": "My Site", "batteryCapacity": 10.0, "status": "online"}
    api.devices[system_id] = [{"deviceType": "Inverter", "attrMap": {"ratedActivePower": 5.0}}]
    api.energy_flow[system_id] = {
        "batterySoc": 60.0,
        "batteryPower": 2.0,       # kW charging
        "pvPower": 3.5,             # kW
        "gridPower": 1.0,           # kW export (positive=export, will be inverted to negative)
        "loadPower": 4.5,
        "evPower": 0.0,
    }
    api.daily_summary[system_id] = {"dailyPowerGeneration": 12.3}

    run_async(api.publish_system_entities(system_id))

    soc_key = "sensor.predbat_sigenergy_{}_battery_soc".format(slug)
    battery_key = "sensor.predbat_sigenergy_{}_battery_power".format(slug)
    grid_key = "sensor.predbat_sigenergy_{}_grid_power".format(slug)
    pv_key = "sensor.predbat_sigenergy_{}_pv_power".format(slug)
    today_key = "sensor.predbat_sigenergy_{}_pv_today".format(slug)

    assert soc_key in api.dashboard_items, "Battery SOC entity published"
    soc_kwh = api.dashboard_items[soc_key]["state"]
    assert abs(soc_kwh - 6.0) < 0.01, "SOC kWh = 60% × 10kWh = 6.0, got {}".format(soc_kwh)

    assert battery_key in api.dashboard_items, "Battery power entity published"
    assert api.dashboard_items[battery_key]["state"] == 2000, "Battery 2kW = 2000W"

    assert grid_key in api.dashboard_items, "Grid power entity published"
    # API gridPower +1.0 (export) → Predbat −1000 W (import-negative)
    assert api.dashboard_items[grid_key]["state"] == -1000, "Grid power inverted: export 1kW → -1000W"

    assert pv_key in api.dashboard_items, "PV power entity published"
    assert api.dashboard_items[pv_key]["state"] == 3500, "PV 3.5kW = 3500W"

    assert today_key in api.dashboard_items, "PV today entity published"
    assert abs(api.dashboard_items[today_key]["state"] - 12.3) < 0.01, "PV today correct"

    return failed


def test_sigenergy_automatic_config(my_predbat):
    """Test automatic_config wires the expected Predbat args."""
    failed = False
    api = MockSigenergyAPI()
    api.automatic = True

    api.systems = {"SIG001": {"systemName": "Home"}, "SIG002": {"systemName": "Office"}}

    run_async(api.automatic_config())

    assert "num_inverters" in api.set_args, "num_inverters set"
    assert api.set_args["num_inverters"] == 2, "num_inverters == 2"
    assert api.set_args.get("inverter_type") == ["SIGCLOUD", "SIGCLOUD"], "inverter_type wired"
    assert "soc_kw" in api.set_args, "soc_kw wired"
    assert "battery_power" in api.set_args, "battery_power wired"
    assert "pv_power" in api.set_args, "pv_power wired"
    assert "grid_power" in api.set_args, "grid_power wired"
    assert "inverter_time" in api.set_args, "inverter_time wired"
    assert len(api.set_args["inverter_time"]) == 2, "inverter_time has one entry per system"

    return failed


def test_sigenergy_fetch_controls(my_predbat):
    """Test fetch_controls reads default values when entities have no state."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = []

    run_async(api.fetch_controls(system_id))

    assert system_id in api.controls, "Controls entry created"
    assert "charge" in api.controls[system_id], "charge key present"
    assert "export" in api.controls[system_id], "export key present"
    assert api.controls[system_id]["charge"].get("enable") is False, "charge enable defaults off"
    assert api.controls[system_id]["export"].get("enable") is False, "export enable defaults off"

    return failed


def test_sigenergy_publish_controls(my_predbat):
    """Test publish_controls creates HA switch/select/number entities."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = []
    api.controls[system_id] = {
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "enable": False, "target_soc": 100, "rate": 2000},
        "export": {"start_time": "17:00:00", "end_time": "19:00:00", "enable": False, "target_soc": 20, "rate": 2000},
        "reserve": 10,
    }

    run_async(api.publish_controls(system_id))

    slug = api._system_slug(system_id)
    charge_enable_key = "switch.predbat_sigenergy_{}_charge_enable".format(slug)
    export_start_key = "select.predbat_sigenergy_{}_export_start_time".format(slug)
    reserve_key = "number.predbat_sigenergy_{}_reserve".format(slug)

    assert charge_enable_key in api.dashboard_items, "Charge enable switch published: {}".format(charge_enable_key)
    assert export_start_key in api.dashboard_items, "Export start time select published"
    assert reserve_key in api.dashboard_items, "Reserve number published"

    # Options must be HH:MM:SS to match what inverter.adjust_charge_window writes
    export_start_opts = api.dashboard_items[export_start_key]["attributes"].get("options", [])
    assert "17:00:00" in export_start_opts, "HH:MM:SS option present in select options"
    assert "17:00" not in export_start_opts, "Plain HH:MM must not appear in select options"

    return failed


def test_sigenergy_parse_entity_system(my_predbat):
    """Test _parse_entity_system correctly decodes entity IDs."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG12345"
    api.systems[system_id] = {}
    api.controls[system_id] = {}

    slug = api._system_slug(system_id)

    entity_id = "switch.predbat_sigenergy_{}_charge_enable".format(slug)
    sid, direction, field = api._parse_entity_system(entity_id)
    assert sid == system_id, "System ID parsed: got {}".format(sid)
    assert direction == "charge", "Direction parsed"
    assert field == "enable", "Field parsed"

    entity_id2 = "number.predbat_sigenergy_{}_reserve".format(slug)
    sid2, direction2, field2 = api._parse_entity_system(entity_id2)
    assert sid2 == system_id, "System ID parsed for global field"
    assert direction2 is None, "No direction for global field"
    assert field2 == "reserve", "Global field name parsed"

    return failed


def test_sigenergy_apply_service_to_toggle(my_predbat):
    """Test _apply_service_to_toggle correctly maps service strings."""
    failed = False
    api = MockSigenergyAPI()

    assert api._apply_service_to_toggle(False, "turn_on") is True, "turn_on → True"
    assert api._apply_service_to_toggle(True, "turn_off") is False, "turn_off → False"
    assert api._apply_service_to_toggle(False, "toggle") is True, "toggle False → True"
    assert api._apply_service_to_toggle(True, "toggle") is False, "toggle True → False"
    assert api._apply_service_to_toggle(True, "unknown") is True, "unknown keeps current"

    return failed


def test_sigenergy_get_access_token_success(my_predbat):
    """Test get_access_token caches the token on success."""
    failed = False
    api = MockSigenergyAPI()

    fake_response = {
        "code": 0,
        "data": {
            "accessToken": "test_token_abc",
            "expiresIn": 43200,
            "tokenType": "Bearer",
        },
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        token = run_async(api.get_access_token())

    assert token == "test_token_abc", "Token returned: {}".format(token)
    assert api.access_token == "test_token_abc", "Token cached"
    assert api.token_expires_at > 0, "Expiry set"

    # Second call should use cache without hitting the network
    token2 = run_async(api.get_access_token())
    assert token2 == "test_token_abc", "Cached token returned on second call"

    return failed


def test_sigenergy_get_access_token_failure(my_predbat):
    """Test get_access_token returns None on API error."""
    failed = False
    api = MockSigenergyAPI()

    fake_response = {"code": 10001, "msg": "Invalid key"}

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        token = run_async(api.get_access_token())

    assert token is None, "None returned on API error"
    assert api.access_token is None, "Token not cached on failure"

    return failed


def test_sigenergy_get_access_token_retry(my_predbat):
    """Test get_access_token retries on transient errors then succeeds."""
    failed = False
    api = MockSigenergyAPI()

    attempt_count = {"n": 0}

    # First two calls raise a timeout; third succeeds
    success_response = _make_mock_response(status=200, json_data={"code": 0, "data": {"accessToken": "retried_token", "expiresIn": 43200}})
    success_session = _make_mock_session(success_response)

    call_log = []

    original_class = __import__("sigenergy").aiohttp.ClientSession

    class SequencedSession:
        """Return failure sessions then success session."""
        def __init__(self, *args, **kwargs):
            attempt_count["n"] += 1
            self._n = attempt_count["n"]

        async def __aenter__(self):
            call_log.append(self._n)
            if self._n < 3:
                raise asyncio.TimeoutError()
            return await success_session.__aenter__()

        async def __aexit__(self, *args):
            if self._n >= 3:
                await success_session.__aexit__(*args)

    with patch("sigenergy.aiohttp.ClientSession", SequencedSession):
        token = run_async(api.get_access_token())

    assert token == "retried_token", "Token returned after retry: {}".format(token)
    assert attempt_count["n"] == 3, "Exactly 3 attempts made, got {}".format(attempt_count["n"])
    assert any("timed out" in m for m in api.log_messages), "Timeout warning logged"

    return failed


def test_sigenergy_get_access_token_no_retry_on_api_error(my_predbat):
    """Test get_access_token does not retry after a permanent API rejection."""
    failed = False
    api = MockSigenergyAPI()

    attempt_count = {"n": 0}
    fake_response = {"code": 11003, "msg": "authentication failed"}

    mock_response = _make_mock_response(status=200, json_data=fake_response)

    class CountingSession:
        """Count how many times a session is created."""
        def __init__(self, *args, **kwargs):
            attempt_count["n"] += 1

        async def __aenter__(self):
            return await _make_mock_session(mock_response).__aenter__()

        async def __aexit__(self, *args):
            pass

    with patch("sigenergy.aiohttp.ClientSession", CountingSession):
        token = run_async(api.get_access_token())

    assert token is None, "None returned on permanent API error"
    assert attempt_count["n"] == 1, "Only one attempt made for API rejection, got {}".format(attempt_count["n"])

    return failed


def test_sigenergy_fetch_system_list(my_predbat):
    """Test fetch_system_list populates self.systems."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0  # ensure no rate-limit delay

    fake_response = {
        "code": 0,
        "data": [
            {"systemId": "SIG001", "systemName": "Home", "batteryCapacity": 10.0, "pvCapacity": 6.0, "status": "online"},
            {"systemId": "SIG002", "systemName": "Office", "batteryCapacity": 20.0, "pvCapacity": 12.0, "status": "offline"},
        ],
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_system_list())

    assert ok is True, "fetch_system_list should return True, got {}".format(ok)
    assert "SIG001" in api.systems, "SIG001 stored"
    assert "SIG002" in api.systems, "SIG002 stored"
    assert api.systems["SIG001"]["systemName"] == "Home", "System name correct"

    return failed


def test_sigenergy_fetch_system_list_with_filter(my_predbat):
    """Test fetch_system_list respects system_id_filter."""
    failed = False
    api = MockSigenergyAPI()
    api.system_id_filter = {"SIG001"}
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    fake_response = {
        "code": 0,
        "data": [
            {"systemId": "SIG001", "systemName": "Home", "batteryCapacity": 10.0},
            {"systemId": "SIG002", "systemName": "Office", "batteryCapacity": 20.0},
        ],
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_system_list())

    assert ok is True, "fetch_system_list should return True with filter, got {}".format(ok)
    assert "SIG001" in api.systems, "Filtered system included"
    assert "SIG002" not in api.systems, "Non-matching system excluded"

    return failed


def test_sigenergy_apply_controls_charge_mode(my_predbat):
    """Test apply_controls selects charge command during active charge window."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = [{"deviceType": "Battery", "attrMap": {"ratedChargePower": 3.0}}]

    # SOC at 50%, charge window active now, target 90%
    api.energy_flow[system_id] = {"batterySoc": 50.0}
    now = datetime.now(timezone.utc)
    start_str = (now - timedelta(hours=1)).strftime("%H:%M:%S")
    end_str = (now + timedelta(hours=2)).strftime("%H:%M:%S")
    api.controls[system_id] = {
        "charge": {"enable": True, "start_time": start_str, "end_time": end_str, "target_soc": 90, "rate": 3000},
        "export": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 20, "rate": 3000},
        "reserve": 10,
    }

    commands_sent = []

    async def mock_set_operating_mode(sid, mode):
        commands_sent.append(("set_mode", sid, mode))
        return True

    async def mock_send_battery_command(sid, active_mode, duration_min, charging_power_kw=None, **kwargs):
        commands_sent.append(("battery_cmd", sid, active_mode, duration_min, charging_power_kw))
        return True

    api.set_operating_mode = mock_set_operating_mode
    api.send_battery_command = mock_send_battery_command

    ok = run_async(api.apply_controls(system_id))
    assert ok is True, "apply_controls returned True"

    bat_cmds = [c for c in commands_sent if c[0] == "battery_cmd"]
    assert len(bat_cmds) >= 1, "send_battery_command called"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_CHARGE, "Charge active mode sent"

    return failed


def test_sigenergy_apply_controls_eco_mode(my_predbat):
    """Test apply_controls sends eco command when no window is active."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = []
    api.energy_flow[system_id] = {"batterySoc": 70.0}
    api.controls[system_id] = {
        "charge": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 100, "rate": 3000},
        "export": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 0, "rate": 3000},
        "reserve": 10,
    }

    commands_sent = []

    async def mock_set_operating_mode(sid, mode):
        commands_sent.append(("set_mode", sid, mode))
        return True

    async def mock_send_battery_command(sid, active_mode, duration_min, charging_power_kw=None, **kwargs):
        commands_sent.append(("battery_cmd", sid, active_mode, duration_min, charging_power_kw))
        return True

    api.set_operating_mode = mock_set_operating_mode
    api.send_battery_command = mock_send_battery_command

    ok = run_async(api.apply_controls(system_id))
    assert ok is True, "apply_controls eco returned True"

    bat_cmds = [c for c in commands_sent if c[0] == "battery_cmd"]
    assert len(bat_cmds) >= 1, "send_battery_command called for eco"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_SELF, "selfConsumption sent for eco"

    return failed


def test_sigenergy_apply_controls_deduplication(my_predbat):
    """Test that send_battery_command skips redundant commands within 5 minutes."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = []
    api.energy_flow[system_id] = {"batterySoc": 70.0}
    api.controls[system_id] = {
        "charge": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 100, "rate": 3000},
        "export": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 0, "rate": 3000},
        "reserve": 10,
    }
    # Provide a valid token so send_battery_command doesn't bail early
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999

    publish_count = {"count": 0}

    async def mock_publish_mqtt(topic, payload_dict):
        publish_count["count"] += 1
        return True

    api._publish_mqtt = mock_publish_mqtt

    # First call — should publish
    run_async(api.apply_controls(system_id))
    first_count = publish_count["count"]
    assert first_count >= 1, "Command published on first call"

    # Second call immediately after — same command, should be de-duplicated
    run_async(api.apply_controls(system_id))
    second_count = publish_count["count"]
    assert second_count == first_count, "No additional publish within 5-min dedup window (first={}, second={})".format(first_count, second_count)

    return failed


def test_sigenergy_apply_controls_export_mode(my_predbat):
    """Test apply_controls sends discharge command during export window."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = [{"deviceType": "Battery", "attrMap": {"ratedChargePower": 3.0}}]
    api.energy_flow[system_id] = {"batterySoc": 80.0}

    now = datetime.now(timezone.utc)
    start_str = (now - timedelta(hours=1)).strftime("%H:%M:%S")
    end_str = (now + timedelta(hours=1)).strftime("%H:%M:%S")
    api.controls[system_id] = {
        "charge": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 100, "rate": 3000},
        "export": {"enable": True, "start_time": start_str, "end_time": end_str, "target_soc": 10, "rate": 3000},
        "reserve": 10,
    }

    commands_sent = []

    async def mock_set_operating_mode(sid, mode):
        commands_sent.append(("set_mode", sid, mode))
        return True

    async def mock_send_battery_command(sid, active_mode, duration_min, charging_power_kw=None, **kwargs):
        commands_sent.append(("battery_cmd", sid, active_mode, duration_min, charging_power_kw))
        return True

    api.set_operating_mode = mock_set_operating_mode
    api.send_battery_command = mock_send_battery_command

    ok = run_async(api.apply_controls(system_id))
    assert ok is True, "apply_controls export returned True"

    bat_cmds = [c for c in commands_sent if c[0] == "battery_cmd"]
    assert len(bat_cmds) >= 1, "send_battery_command called for export"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_DISCHARGE, "discharge mode sent for export"

    return failed


# ---------------------------------------------------------------------------
# MQTT tests
# ---------------------------------------------------------------------------


def _make_mock_aiomqtt_client():
    """Create a mock aiomqtt.Client context manager that records publishes."""
    publishes = []

    mock_client = MagicMock()
    mock_client.publishes = publishes

    async def mock_publish(topic, payload=None, qos=0, **kwargs):
        publishes.append((topic, payload))

    mock_client.publish = mock_publish

    async def client_aenter(*args, **kwargs):
        return mock_client

    async def client_aexit(*args, **kwargs):
        pass

    mock_client.__aenter__ = client_aenter
    mock_client.__aexit__ = client_aexit
    return mock_client


def test_sigenergy_publish_mqtt_success(my_predbat):
    """Test _publish_mqtt connects to the broker and publishes JSON payload."""
    failed = False
    api = MockSigenergyAPI()
    # Use the real _publish_mqtt (not the mock override) by calling via super/direct
    api.access_token = "tok123"
    api.mqtt_host = "openapi-eu.sigencloud.com" # cspell:disable-line
    api.mqtt_port = 8883

    mock_client = _make_mock_aiomqtt_client()

    with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
        with patch("sigenergy.aiomqtt.Client", return_value=mock_client):
            ok = run_async(SigenergyAPI._publish_mqtt(api, "openapi/instruction/command", {"activeMode": "charge", "systemId": "SIG1"}))

    assert ok is True, "_publish_mqtt should return True on success"
    assert len(mock_client.publishes) == 1, "Exactly one publish call expected"
    topic, payload = mock_client.publishes[0]
    assert topic == "openapi/instruction/command", "Topic correct"
    import json
    decoded = json.loads(payload)
    assert decoded["activeMode"] == "charge", "Payload content correct"
    assert decoded["systemId"] == "SIG1", "systemId in payload"

    return failed


def test_sigenergy_publish_mqtt_failure(my_predbat):
    """Test _publish_mqtt returns False when the broker connection raises."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "tok123"
    api.mqtt_host = "openapi-eu.sigencloud.com" # cspell:disable-line
    api.mqtt_port = 8883

    def raise_error(*args, **kwargs):
        raise ConnectionRefusedError("broker unavailable")

    with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
        with patch("sigenergy.aiomqtt.Client", side_effect=raise_error):
            ok = run_async(SigenergyAPI._publish_mqtt(api, "openapi/instruction/command", {}))

    assert ok is False, "_publish_mqtt should return False on connection error"
    assert any("MQTT publish" in m and "failed" in m for m in api.log_messages), "Error logged on failure"

    return failed


def test_sigenergy_send_battery_command_mqtt(my_predbat):
    """Test send_battery_command publishes the correct MQTT payload."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "reused_token"
    api.token_expires_at = 9_999_999_999  # token still valid

    published = []

    async def mock_publish_mqtt(topic, payload_dict):
        published.append((topic, payload_dict))
        return True

    api._publish_mqtt = mock_publish_mqtt

    ok = run_async(api.send_battery_command("SIG001", "charge", 60, charging_power_kw=3.5))

    assert ok is True, "send_battery_command should return True"
    assert len(published) == 1, "One MQTT publish expected"
    topic, payload = published[0]
    assert topic == "openapi/instruction/command", "Correct MQTT topic"
    assert payload["accessToken"] == "reused_token", "Token in payload"
    cmd = payload["commands"][0]
    assert cmd["systemId"] == "SIG001", "systemId in commands[0]"
    assert cmd["activeMode"] == "charge", "activeMode in commands[0]"
    assert cmd["duration"] == 60, "duration in commands[0]"
    assert abs(payload["chargingPower"] - 3.5) < 0.01, "chargingPower in payload"

    return failed


def test_sigenergy_send_battery_command_no_token(my_predbat):
    """Test send_battery_command returns False when token cannot be obtained."""
    failed = False
    api = MockSigenergyAPI()
    # Force get_access_token to fail by returning None
    api.access_token = None
    api.token_expires_at = 0.0

    # Patch get_access_token to always return None
    async def mock_get_access_token():
        return None

    api.get_access_token = mock_get_access_token

    ok = run_async(api.send_battery_command("SIG001", "charge", 60, charging_power_kw=3.5))
    assert ok is False, "send_battery_command should return False when no token"
    assert any("No access token" in m for m in api.log_messages), "No-token error logged"

    return failed


def test_sigenergy_handle_mqtt_period(my_predbat):
    """Test _handle_mqtt_period populates energy_flow correctly from a period message."""
    failed = False
    api = MockSigenergyAPI()

    value_dict = {
        "storageSOC%": "79.7",
        "storageChargeDischargePowerW": "-2927.0",   # negative = discharging
        "PV power": "0.0",
        "gridActivePowerW": "3.0",
        "inverterActivePowerW": "2681.0",
        "storageChargeCapacityWh": "9520.0",
        "storageDischargeCapacityWh": "37410.0",
        "batteryMaxChargePowerW": "22032.0",
        "batteryMaxDischargePowerW": "36051.0",
        "operationalMode": "6.0",
        "systemStatus": "1.0",
    }

    api._handle_mqtt_period("SYS1", value_dict)

    # Realtime power fields land in energy_flow
    flow = api.energy_flow.get("SYS1", {})
    assert abs(flow["batterySoc"] - 79.7) < 0.01, "batterySoc = 79.7%"
    # storageChargeDischargePowerW -2927 W = -2.927 kW (discharging, negative = energyFlow convention)
    assert abs(flow["batteryPower"] - (-2.927)) < 0.01, "batteryPower = -2.927 kW"
    assert abs(flow["pvPower"] - 0.0) < 0.001, "pvPower = 0.0"
    assert abs(flow["gridPower"] - 0.003) < 0.001, "gridPower = 0.003 kW"
    # loadPower = pv - bat - grid = 0 - (-2.927) - 0.003 = 2.924
    assert abs(flow["loadPower"] - 2.924) < 0.01, "loadPower derived = 2.924 kW"
    assert abs(flow["inverterPower"] - 2.681) < 0.001, "inverterPower = 2.681 kW"
    assert "chargeCapacityKwh" not in flow, "capacity/status fields must not be in energy_flow"

    # Capacity, power and mode fields land in system_status (not energy_flow)
    status = api.system_status.get("SYS1", {})
    assert abs(status["chargeCapacity"] - 9.52) < 0.01, "chargeCapacity = 9.52 kWh"
    assert abs(status["dischargeCapacity"] - 37.41) < 0.01, "dischargeCapacity = 37.41 kWh"
    assert abs(status["ratedChargePower"] - 22.032) < 0.01, "ratedChargePower = 22.032 kW"
    assert abs(status["ratedDischargePower"] - 36.051) < 0.01, "ratedDischargePower = 36.051 kW"
    assert status["operationalMode"] == 6.0, "operationalMode = 6.0"
    assert status["systemStatus"] == 1.0, "systemStatus = 1.0"
    assert any("MQTT period" in m and "80" in m for m in api.log_messages), "Period data logged"

    return failed


def test_sigenergy_handle_mqtt_change(my_predbat):
    """Test _handle_mqtt_change updates controls and systems from a change message."""
    failed = False
    api = MockSigenergyAPI()
    api.systems["SYS1"] = {"systemName": "Test System"}

    value_dict = {
        "batteryRatedChargePowerW": "22000.0",
        "batteryRatedCapabilityWh": "45200.0",
        "backupCutOffSOC%": "15.0",
        "batteryRatedDischargePowerW": "24000.0",
        "inverterMaxActivePowerW": "12000.0",
        "dischargeCutOffSOC%": "5.0",
        "chargeCutOffSOC%": "100.0",
        "gridMaxBackfeedPowerW": "5000.0",
    }

    api._handle_mqtt_change("SYS1", value_dict)

    # Controls
    assert api.controls["SYS1"]["reserve"] == 15, "reserve = 15 (backupCutOffSOC%)"
    assert api.controls["SYS1"]["charge"]["target_soc"] == 100, "charge target_soc = 100 (chargeCutOffSOC%)"
    assert api.controls["SYS1"]["export"]["target_soc"] == 5, "export target_soc = 5 (dischargeCutOffSOC%)"

    # System capacity and power limits
    sys = api.systems["SYS1"]
    assert abs(sys["batteryCapacity"] - 45.2) < 0.01, "batteryCapacity = 45.2 kWh"
    assert abs(sys["ratedChargePower"] - 22.0) < 0.01, "ratedChargePower = 22.0"
    assert abs(sys["ratedDischargePower"] - 24.0) < 0.01, "ratedDischargePower = 24.0"
    assert abs(sys["ratedActivePower"] - 12.0) < 0.01, "ratedActivePower = 12.0"
    assert abs(sys["gridMaxBackfeedPower"] - 5.0) < 0.01, "gridMaxBackfeedPower = 5.0"
    assert any("MQTT change" in m for m in api.log_messages), "Change data logged"

    return failed


def test_sigenergy_handle_mqtt_alarm(my_predbat):
    """Test _handle_mqtt_alarm logs a warning."""
    failed = False
    api = MockSigenergyAPI()
    api._handle_mqtt_alarm("SYS1", [{"alarmCode": "E001", "alarmMsg": "Overvoltage"}])
    assert any("alarm" in m.lower() and "SYS1" in m for m in api.log_messages), "Alarm warning logged"
    return failed


def test_sigenergy_mqtt_listener_loop(my_predbat):
    """Test _mqtt_listener_loop dispatches period and change messages and stops on api_stop."""
    failed = False
    import json as _json
    api = MockSigenergyAPI()
    api.access_token = "tok"
    api.token_expires_at = 9_999_999_999
    api.systems["XRTKQ1773829273"] = {"systemName": "Test"}
    api.api_stop = False

    period_payload = _json.dumps([{
        "deviceType": "system",
        "systemId": "XRTKQ1773829273",
        "value": {
            "storageSOC%": "55.0",
            "storageChargeDischargePowerW": "1000.0",
            "PV power": "2000.0",
            "gridActivePowerW": "500.0",
        },
    }]).encode()

    change_payload = _json.dumps([{
        "deviceType": "system",
        "systemId": "XRTKQ1773829273",
        "value": {
            "backupCutOffSOC%": "20.0",
            "chargeCutOffSOC%": "95.0",
            "dischargeCutOffSOC%": "10.0",
        },
    }]).encode()

    # Build fake MQTT messages
    class FakeMessage:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload
            self.qos = 0
            self.retain = False

    messages_to_deliver = [
        FakeMessage("openapi/period/test_app_key/XRTKQ1773829273", period_payload),
        FakeMessage("openapi/change/test_app_key/XRTKQ1773829273", change_payload),
    ]

    publishes = []

    class FakeMQTTClient:
        """Async context manager that yields two messages then exits cleanly."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def subscribe(self, topic):
            pass

        async def publish(self, topic, payload=None, qos=0, **kwargs):
            publishes.append((topic, payload))

        # Make client.messages an async iterable that yields the two messages
        # and then sets api_stop so the outer loop exits after one connection cycle.
        class _Messages:
            def __init__(self, msgs, api):
                self._msgs = iter(msgs)
                self._api = api

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._msgs)
                except StopIteration:
                    # Signal the outer loop to stop, then end this iteration
                    self._api.api_stop = True
                    raise StopAsyncIteration

        @property
        def messages(self):
            return FakeMQTTClient._Messages(messages_to_deliver, api)

    with patch("sigenergy.aiomqtt.Client", return_value=FakeMQTTClient()):
        with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
            run_async(api._mqtt_listener_loop())

    # Period message: energy_flow updated
    flow = api.energy_flow.get("XRTKQ1773829273", {})
    assert abs(flow.get("batterySoc", 0) - 55.0) < 0.01, "batterySoc from MQTT period = 55%"
    assert abs(flow.get("batteryPower", 0) - 1.0) < 0.01, "batteryPower = 1.0 kW (charging)"

    # Change message: controls updated
    ctrl = api.controls.get("XRTKQ1773829273", {})
    assert ctrl.get("reserve") == 20, "reserve = 20 from MQTT change"
    assert ctrl.get("charge", {}).get("target_soc") == 95, "charge target_soc = 95"
    assert ctrl.get("export", {}).get("target_soc") == 10, "export target_soc = 10"

    # Subscription requests published (3: period, change, alarm)
    sub_topics = [t for t, _ in publishes]
    assert "openapi/subscription/period" in sub_topics, "period subscription published"
    assert "openapi/subscription/change" in sub_topics, "change subscription published"
    assert "openapi/subscription/alarm" in sub_topics, "alarm subscription published"

    # last_mqtt_update was set per system
    assert api.last_mqtt_update.get("XRTKQ1773829273", 0) > 0, "last_mqtt_update was set for system"

    return failed


def test_sigenergy_fetch_inverter_realtime(my_predbat):
    """Test fetch_inverter_realtime maps realtimeInfo fields to energy_flow correctly."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    # Populate a minimal device list with one Inverter
    api.devices["SYS1"] = [
        {"deviceType": "Inverter", "serialNumber": "INV001"},
        {"deviceType": "Battery", "serialNumber": "BAT001"},
    ]

    # realtimeInfo response: batPower positive=discharging (3.0 kW discharging)
    # activePower positive=export (1.5 kW export)
    # pvPower 5.0 kW
    # batSoc 72.0 %
    # pvEnergyDaily 12.5 kWh
    fake_response = {
        "code": 0,
        "data": {
            "systemId": "SYS1",
            "serialNumber": "INV001",
            "deviceType": "Inverter",
            "realTimeInfo": {
                "batSoc": 72.0,
                "batPower": 3.0,   # discharging → batteryPower should be -3.0
                "pvPower": 5.0,
                "activePower": 1.5,  # export → gridPower = 1.5
                "pvEnergyDaily": 12.5,
            },
        },
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_inverter_realtime("SYS1"))

    assert ok is True, "fetch_inverter_realtime should return True"
    flow = api.energy_flow.get("SYS1", {})

    assert flow.get("batterySoc") == 72.0, "batterySoc = 72.0"
    # batPower was 3.0 (discharging) → batteryPower should be -3.0 (discharging in energyFlow convention)
    assert flow.get("batteryPower") == -3.0, "batteryPower = -3.0 (discharging, sign negated)"
    assert flow.get("pvPower") == 5.0, "pvPower = 5.0"
    assert flow.get("gridPower") == 1.5, "gridPower = 1.5 (export)"
    # loadPower = pv + battery_discharge - grid_export = 5.0 + 3.0 - 1.5 = 6.5
    assert flow.get("loadPower") == 6.5, "loadPower = 6.5 (derived)"
    assert flow.get("evPower") == 0.0, "evPower = 0.0 (not available)"

    # pvEnergyDaily should update daily_summary
    daily = api.daily_summary.get("SYS1", {})
    assert daily.get("dailyPowerGeneration") == 12.5, "daily PV yield updated from pvEnergyDaily"

    return failed


def test_sigenergy_fetch_inverter_realtime_no_inverter(my_predbat):
    """Test fetch_inverter_realtime returns False when no inverter device is found."""
    failed = False
    api = MockSigenergyAPI()
    api.devices["SYS1"] = [
        {"deviceType": "Battery", "serialNumber": "BAT001"},
    ]

    ok = run_async(api.fetch_inverter_realtime("SYS1"))
    assert ok is False, "Should return False when no inverter in device list"
    assert any("No inverter" in m for m in api.log_messages), "Warning logged about missing inverter"

    return failed


def test_sigenergy_get_inverter_serial(my_predbat):
    """Test _get_inverter_serial finds Inverter and AIO device types."""
    failed = False
    api = MockSigenergyAPI()

    # No devices → None
    api.devices["SYS1"] = []
    assert api._get_inverter_serial("SYS1") is None, "Empty device list returns None"

    # Only battery → None
    api.devices["SYS1"] = [{"deviceType": "Battery", "serialNumber": "BAT001"}]
    assert api._get_inverter_serial("SYS1") is None, "Battery-only list returns None"

    # Inverter type → found
    api.devices["SYS1"] = [
        {"deviceType": "Battery", "serialNumber": "BAT001"},
        {"deviceType": "Inverter", "serialNumber": "INV001"},
    ]
    assert api._get_inverter_serial("SYS1") == "INV001", "Inverter serial returned"

    # AIO type → found
    api.devices["SYS2"] = [{"deviceType": "AIO", "serialNumber": "AIO001"}]
    assert api._get_inverter_serial("SYS2") == "AIO001", "AIO serial returned"

    return failed


# ---------------------------------------------------------------------------
# Test registration entry point
def test_sigenergy_build_tls_context(my_predbat):
    """Test _build_tls_context builds an SSL context from PEM text content."""
    import os
    import ssl as ssl_mod
    import glob

    failed = False

    # Locate the real PEM files relative to this test file's repo root
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    key_dir = os.path.join(repo_root, "sigenergy_mqtt_key")
    ca_pem_path = os.path.join(key_dir, "ca.pem")
    client_pem_path = os.path.join(key_dir, "client.pem")
    client_key_path = os.path.join(key_dir, "client.key")

    if not os.path.exists(ca_pem_path):
        # No real certs available — test the no-cert path only
        api = MockSigenergyAPI()
        ctx = api._build_tls_context()
        assert isinstance(ctx, ssl_mod.SSLContext), "Default context returned when no certs"
        return failed

    with open(ca_pem_path) as f:
        ca_text = f.read()
    with open(client_pem_path) as f:
        client_cert_text = f.read()
    with open(client_key_path) as f:
        client_key_text = f.read()

    # Test with CA cert text only
    api = MockSigenergyAPI()
    api.ca_cert = ca_text
    ctx = api._build_tls_context()
    assert isinstance(ctx, ssl_mod.SSLContext), "SSLContext built from CA cert text"

    # Test with all three — CA + client cert + key
    api2 = MockSigenergyAPI()
    api2.ca_cert = ca_text
    api2.client_cert = client_cert_text
    api2.client_key = client_key_text
    ctx2 = api2._build_tls_context()
    assert isinstance(ctx2, ssl_mod.SSLContext), "SSLContext built from CA + client cert + key text"

    # Confirm no temp files were left behind
    leftover = glob.glob("/tmp/*.pem")
    assert not any("sigenergy" in p for p in leftover), "No temp PEM files left behind"

    return failed


def test_sigenergy_set_operating_mode(my_predbat):
    """Test set_operating_mode publishes MQTT and returns True on success, False on failure."""
    failed = False
    api = MockSigenergyAPI()

    async def mock_token():
        return "test-token"

    api.get_access_token = mock_token

    # Success: MQTT publish succeeds
    async def mock_publish_ok(topic, payload_dict):
        return True

    api._publish_mqtt = mock_publish_ok
    ok = run_async(api.set_operating_mode("SIG001", 0))
    assert ok is True, "set_operating_mode should return True when MQTT publish succeeds"
    assert not any("Failed" in m for m in api.log_messages), "No failure logged on success"

    # Failure: MQTT publish fails
    api.log_messages = []

    async def mock_publish_fail(topic, payload_dict):
        return False

    api._publish_mqtt = mock_publish_fail
    ok = run_async(api.set_operating_mode("SIG001", 0))
    assert ok is False, "set_operating_mode should return False when MQTT publish fails"
    assert any("Failed" in m for m in api.log_messages), "Failure warning logged when publish fails"

    # Failure: unknown mode int
    api.log_messages = []
    api._publish_mqtt = mock_publish_ok
    ok = run_async(api.set_operating_mode("SIG001", 99))
    assert ok is False, "set_operating_mode should return False for unknown mode int"

    return failed


def _make_api_with_system(system_id="SIG001"):
    """Return a MockSigenergyAPI pre-loaded with one system and its device list."""
    api = MockSigenergyAPI()
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = []
    return api


def test_sigenergy_manage_vpp_registration_switch_to_msc(my_predbat):
    """Readonly=True + VPP active → set_operating_mode(MSC) called, returns False."""
    from sigenergy import SIGENERGY_MODE_MSC
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_VPP

    mode_calls = []

    async def mock_set_mode(system_id, mode_int):
        mode_calls.append((system_id, mode_int))
        return True

    api.set_operating_mode = mock_set_mode

    result = run_async(api._manage_vpp_registration(sid, is_readonly=True))
    assert result is False, "Should return False after switching to MSC"
    assert len(mode_calls) == 1, "set_operating_mode called once"
    assert mode_calls[0] == (sid, SIGENERGY_MODE_MSC), "Mode switched to MSC"
    assert any("MSC" in m for m in api.log_messages), "MSC switch logged"

    return failed


def test_sigenergy_manage_vpp_registration_switch_to_vpp(my_predbat):
    """Readonly=False + not VPP → set_operating_mode(VPP) called, returns False (activating async)."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = 0  # MSC — not VPP

    mode_calls = []

    async def mock_set_mode(system_id, mode_int):
        mode_calls.append((system_id, mode_int))
        return True

    api.set_operating_mode = mock_set_mode

    result = run_async(api._manage_vpp_registration(sid, is_readonly=False))
    assert result is False, "Should return False (VPP not yet confirmed, activating async)"
    assert len(mode_calls) == 1, "set_operating_mode called once"
    assert mode_calls[0] == (sid, SIGENERGY_MODE_VPP), "Mode switched to VPP"
    assert any("VPP" in m for m in api.log_messages), "VPP switch logged"

    return failed


def test_sigenergy_onboard_systems_pending_per_item(my_predbat):
    """onboard_systems: real API per-item response result=False codeList=[1116] → returns None and logs warning."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.access_token = "tok"
    api.token_expires_at = 9_999_999_999

    # Real Sigenergy API response: [{'systemId': '...', 'result': False, 'codeList': [1116]}]
    async def mock_request(method, path, json_data=None, params=None, retries=3):
        return [{"systemId": sid, "result": False, "codeList": [SIGENERGY_CODE_SYSTEM_PENDING_REVIEW]}]

    api._request = mock_request

    result = run_async(api.onboard_systems([sid]))
    assert result is None, "Per-item code 1116 should return None (pending)"
    assert any("pending" in m.lower() for m in api.log_messages), "Pending approval warning logged"

    return failed


def test_sigenergy_onboard_systems_other_vpp(my_predbat):
    """onboard_systems: per-item codeList=[1103] (other VPP) → returns False and logs warning."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.access_token = "tok"
    api.token_expires_at = 9_999_999_999

    async def mock_request(method, path, json_data=None, params=None, retries=3):
        return [{"systemId": sid, "result": False, "codeList": [SIGENERGY_CODE_IN_OTHER_VPP]}]

    api._request = mock_request

    result = run_async(api.onboard_systems([sid]))
    assert result is False, "Other-VPP code should return False"
    assert any("another VPP" in m for m in api.log_messages), "Other-VPP warning logged"

    return failed


def test_sigenergy_manage_vpp_registration_ready(my_predbat):
    """Readonly=False + VPP active → no onboard/offboard, returns True."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_VPP

    called = []

    async def mock_onboard(system_ids):
        called.append("onboard")
        return None

    async def mock_offboard(system_ids):
        called.append("offboard")
        return None

    api.onboard_systems = mock_onboard
    api.offboard_systems = mock_offboard

    result = run_async(api._manage_vpp_registration(sid, is_readonly=False))
    assert result is True, "Should return True when already in VPP"
    assert not called, "Neither onboard nor offboard should be called"

    return failed


def test_sigenergy_manage_vpp_registration_readonly_no_vpp(my_predbat):
    """Readonly=True + VPP not active → nothing to do, returns False."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = 0  # not VPP, and readonly — nothing to do

    called = []

    async def mock_onboard(system_ids):
        called.append("onboard")
        return None

    async def mock_offboard(system_ids):
        called.append("offboard")
        return None

    api.onboard_systems = mock_onboard
    api.offboard_systems = mock_offboard

    result = run_async(api._manage_vpp_registration(sid, is_readonly=True))
    assert result is False, "Should return False (readonly, not in VPP)"
    assert not called, "No board operations when readonly and not in VPP"

    return failed


def test_sigenergy_mqtt_period_updates_current_mode(my_predbat):
    """_handle_mqtt_period should update current_mode from operationalMode."""
    failed = False
    api = MockSigenergyAPI()

    # VPP mode (6)
    api._handle_mqtt_period("SYS1", {"storageSOC%": "50.0", "operationalMode": "6.0"})
    assert api.current_mode.get("SYS1") == SIGENERGY_MODE_VPP, "current_mode updated to VPP (6)"

    # Switch to MSC (0)
    api._handle_mqtt_period("SYS1", {"storageSOC%": "50.0", "operationalMode": "0.0"})
    assert api.current_mode.get("SYS1") == 0, "current_mode updated to MSC (0)"

    # Missing operationalMode — current_mode should not change
    api._handle_mqtt_period("SYS1", {"storageSOC%": "50.0"})
    assert api.current_mode.get("SYS1") == 0, "current_mode unchanged when operationalMode absent"

    return failed


def test_sigenergy_apply_controls_skipped_when_not_vpp(my_predbat):
    """Controls should be skipped with a warning when system is not in VPP mode."""
    failed = False
    api = MockSigenergyAPI()
    sid = "SIG001"
    api.systems[sid] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[sid] = []
    api.energy_flow[sid] = {"batterySoc": 60.0}
    api.controls[sid] = {
        "charge": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 100, "rate": 3000},
        "export": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 0, "rate": 3000},
        "reserve": 10,
    }
    api.current_mode[sid] = 0  # MSC, not VPP
    api.enable_controls = True

    apply_calls = []

    async def mock_apply_controls(system_id):
        apply_calls.append(system_id)
        return True

    api.apply_controls = mock_apply_controls

    # Simulate the apply-controls block from run() directly
    async def run_controls_block():
        for s in list(api.systems.keys()):
            if api.current_mode.get(s) != SIGENERGY_MODE_VPP:
                api.log("Warn: SigenergyAPI: System {} is not in VPP mode — controls skipped".format(s))
                continue
            await api.apply_controls(s)

    run_async(run_controls_block())

    assert not apply_calls, "apply_controls must not be called when not in VPP mode"
    assert any("VPP" in m and "skipped" in m for m in api.log_messages), "VPP skip warning logged"

    # Now set VPP mode — controls should fire
    api.current_mode[sid] = SIGENERGY_MODE_VPP
    run_async(run_controls_block())
    assert apply_calls == [sid], "apply_controls called once system is in VPP mode"

    return failed


def test_sigenergy_options_time_format(my_predbat):
    """Test SIGENERGY_OPTIONS_TIME entries are HH:MM:SS, not HH:MM."""
    failed = False

    assert len(SIGENERGY_OPTIONS_TIME) == 1440, "1440 entries (one per minute): got {}".format(len(SIGENERGY_OPTIONS_TIME))
    assert SIGENERGY_OPTIONS_TIME[0] == "00:00:00", "First entry is 00:00:00, got {}".format(SIGENERGY_OPTIONS_TIME[0])
    assert SIGENERGY_OPTIONS_TIME[60] == "01:00:00", "60th entry is 01:00:00, got {}".format(SIGENERGY_OPTIONS_TIME[60])
    assert SIGENERGY_OPTIONS_TIME[-1] == "23:59:00", "Last entry is 23:59:00, got {}".format(SIGENERGY_OPTIONS_TIME[-1])
    # Plain HH:MM must not appear — inverter writes HH:MM:SS and validation must match
    assert "01:00" not in SIGENERGY_OPTIONS_TIME, "Plain HH:MM must not be in options"
    assert "01:00:00" in SIGENERGY_OPTIONS_TIME, "HH:MM:SS must be in options"

    return failed


def test_sigenergy_update_control_time_validation(my_predbat):
    """Test _update_control accepts HH:MM:SS times and rejects plain HH:MM."""
    failed = False
    api = MockSigenergyAPI()
    system_id = "SIG001"
    api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
    api.devices[system_id] = []
    api.controls[system_id] = {
        "charge": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 100, "rate": 3000},
        "export": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 0, "rate": 3000},
        "reserve": 10,
    }

    slug = api._system_slug(system_id)
    entity_id = "select.predbat_sigenergy_{}_charge_start_time".format(slug)

    # HH:MM:SS (what inverter.adjust_charge_window writes) must be accepted
    run_async(api._update_control(entity_id, "01:00:00", "charge", "start_time", system_id))
    assert api.controls[system_id]["charge"]["start_time"] == "01:00:00", "HH:MM:SS accepted and stored"

    # Plain HH:MM must be rejected — value should remain unchanged
    run_async(api._update_control(entity_id, "02:00", "charge", "start_time", system_id))
    assert api.controls[system_id]["charge"]["start_time"] == "01:00:00", "Plain HH:MM rejected, value unchanged"
    assert any("Invalid time" in m for m in api.log_messages), "Rejection warning logged for HH:MM"

    return failed


def test_sigenergy_offboard_toggle_in_vpp(my_predbat):
    """offboard=True → return False immediately regardless of VPP state (no mode switch)."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_VPP

    modes_set = []

    async def mock_set_mode(system_id, mode_int):
        modes_set.append(mode_int)
        return True

    api.set_operating_mode = mock_set_mode

    result = run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert result is False, "offboard=True should return False"
    assert not modes_set, "Should not call set_operating_mode — offboard API already changes mode"

    return failed


def test_sigenergy_offboard_toggle_not_in_vpp(my_predbat):
    """offboard=True + not in VPP → return False, no mode switch."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_MSC

    modes_set = []

    async def mock_set_mode(system_id, mode_int):
        modes_set.append(mode_int)
        return True

    api.set_operating_mode = mock_set_mode

    result = run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert result is False, "offboard=True should return False"
    assert not modes_set, "No mode switch needed"

    return failed


def test_sigenergy_offboard_toggle_switch_event(my_predbat):
    """Turning on the offboard switch triggers offboard_systems (no mode switch)."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.controls[sid] = {"offboard": False}

    offboarded = []
    modes_set = []

    async def mock_offboard(system_ids):
        offboarded.append(system_ids)
        return True

    async def mock_set_mode(system_id, mode_int):
        modes_set.append((system_id, mode_int))
        return True

    async def mock_publish_controls(system_id=None):
        pass

    api.offboard_systems = mock_offboard
    api.set_operating_mode = mock_set_mode
    api.publish_controls = mock_publish_controls

    run_async(api._update_control("switch.predbat_sigenergy_sig001_offboard", "turn_on", None, "offboard", sid))

    assert api.controls[sid]["offboard"] is True, "offboard control should be True after turn_on"
    assert offboarded, "offboard_systems should be called"
    assert not modes_set, "set_operating_mode should NOT be called — offboard API changes the mode"

    return failed


# ---------------------------------------------------------------------------


def run_sigenergy_tests(my_predbat):
    """Run all Sigenergy API unit tests.

    Returns:
        False on success (all tests passed), True if any test failed.
    """
    failed = False
    tests = [
        ("helper_functions", test_sigenergy_helper_functions),
        ("initialize", test_sigenergy_initialize),
        ("system_slug", test_sigenergy_system_slug),
        ("battery_capacity", test_sigenergy_battery_capacity),
        ("publish_system_entities", test_sigenergy_publish_system_entities),
        ("automatic_config", test_sigenergy_automatic_config),
        ("fetch_controls", test_sigenergy_fetch_controls),
        ("publish_controls", test_sigenergy_publish_controls),
        ("parse_entity_system", test_sigenergy_parse_entity_system),
        ("apply_service_to_toggle", test_sigenergy_apply_service_to_toggle),
        ("get_access_token_success", test_sigenergy_get_access_token_success),
        ("get_access_token_failure", test_sigenergy_get_access_token_failure),
        ("get_access_token_retry", test_sigenergy_get_access_token_retry),
        ("get_access_token_no_retry_on_api_error", test_sigenergy_get_access_token_no_retry_on_api_error),
        ("fetch_system_list", test_sigenergy_fetch_system_list),
        ("fetch_system_list_with_filter", test_sigenergy_fetch_system_list_with_filter),
        ("apply_controls_charge_mode", test_sigenergy_apply_controls_charge_mode),
        ("apply_controls_eco_mode", test_sigenergy_apply_controls_eco_mode),
        ("apply_controls_deduplication", test_sigenergy_apply_controls_deduplication),
        ("apply_controls_export_mode", test_sigenergy_apply_controls_export_mode),
        ("publish_mqtt_success", test_sigenergy_publish_mqtt_success),
        ("publish_mqtt_failure", test_sigenergy_publish_mqtt_failure),
        ("send_battery_command_mqtt", test_sigenergy_send_battery_command_mqtt),
        ("send_battery_command_no_token", test_sigenergy_send_battery_command_no_token),
        ("handle_mqtt_period", test_sigenergy_handle_mqtt_period),
        ("handle_mqtt_change", test_sigenergy_handle_mqtt_change),
        ("handle_mqtt_alarm", test_sigenergy_handle_mqtt_alarm),
        ("mqtt_listener_loop", test_sigenergy_mqtt_listener_loop),
        ("fetch_inverter_realtime", test_sigenergy_fetch_inverter_realtime),
        ("fetch_inverter_realtime_no_inverter", test_sigenergy_fetch_inverter_realtime_no_inverter),
        ("get_inverter_serial", test_sigenergy_get_inverter_serial),
        ("build_tls_context", test_sigenergy_build_tls_context),
        ("battery_max_power", test_sigenergy_battery_max_power),
        ("options_time_format", test_sigenergy_options_time_format),
        ("update_control_time_validation", test_sigenergy_update_control_time_validation),
        ("set_operating_mode", test_sigenergy_set_operating_mode),
        ("manage_vpp_registration_switch_to_msc", test_sigenergy_manage_vpp_registration_switch_to_msc),
        ("manage_vpp_registration_switch_to_vpp", test_sigenergy_manage_vpp_registration_switch_to_vpp),
        ("onboard_systems_pending_per_item", test_sigenergy_onboard_systems_pending_per_item),
        ("onboard_systems_other_vpp", test_sigenergy_onboard_systems_other_vpp),
        ("manage_vpp_registration_ready", test_sigenergy_manage_vpp_registration_ready),
        ("manage_vpp_registration_readonly_no_vpp", test_sigenergy_manage_vpp_registration_readonly_no_vpp),
        ("mqtt_period_updates_current_mode", test_sigenergy_mqtt_period_updates_current_mode),
        ("apply_controls_skipped_when_not_vpp", test_sigenergy_apply_controls_skipped_when_not_vpp),
        ("offboard_toggle_in_vpp", test_sigenergy_offboard_toggle_in_vpp),
        ("offboard_toggle_not_in_vpp", test_sigenergy_offboard_toggle_not_in_vpp),
        ("offboard_toggle_switch_event", test_sigenergy_offboard_toggle_switch_event),
    ]

    for name, fn in tests:
        try:
            result = fn(my_predbat)
            if result:
                print("FAIL: test_sigenergy_{}".format(name))
                failed = True
            else:
                print("PASS: test_sigenergy_{}".format(name))
        except (AssertionError, Exception) as e:
            print("FAIL: test_sigenergy_{} — {}".format(name, e))
            import traceback
            traceback.print_exc()
            failed = True

    return failed
