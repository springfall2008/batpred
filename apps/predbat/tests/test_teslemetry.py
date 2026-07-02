# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Unit tests for the TeslemetryAPI component (Tesla Powerwall via Teslemetry)."""

from unittest.mock import MagicMock, patch, AsyncMock

from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session, run_async
from teslemetry import TeslemetryAPI


class MockTeslemetryAPI(TeslemetryAPI):
    """TeslemetryAPI test double that avoids ComponentBase initialisation."""

    def __init__(self):
        """Initialise the mock with captured state instead of ComponentBase wiring."""
        self.api_key = "test_token"
        self.site_id = "123456"
        self.base_url = "https://api.teslemetry.com"
        self.prefix = "predbat"
        self.api_auth_failed = False
        self.last_live_poll = 0
        self.last_energy_poll = 0
        self.dashboard_items = {}
        self.log_messages = []
        self.entity_states = {}
        self.mock_responses = {}
        self.requests_made = []

    def log(self, msg):
        """Capture log messages."""
        self.log_messages.append(msg)

    def dashboard_item(self, entity, state, attributes, app=None):
        """Capture dashboard items."""
        self.dashboard_items[entity] = {"state": state, "attributes": attributes}

    def set_state_wrapper(self, entity_id, state, attributes={}):
        """Capture entity state updates."""
        self.entity_states[entity_id] = state

    def get_state_wrapper(self, entity_id=None, default=None, **kwargs):
        """Return captured entity state."""
        return self.entity_states.get(entity_id, default)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass

    async def _request(self, method, path, json_body=None):
        """Return canned responses instead of real HTTP."""
        self.requests_made.append((method, path, json_body))
        return self.mock_responses.get(path, None)


LIVE_STATUS = {
    "response": {
        "percentage_charged": 55.5,
        "battery_power": 1200,
        "solar_power": 800,
        "load_power": 600,
        "grid_power": -1400,
        "island_status": "on_grid",
    }
}

SITE_INFO = {
    "response": {
        "nameplate_energy": 13500,
        "default_real_mode": "self_consumption",
        "backup_reserve_percent": 20,
    }
}

ENERGY_HISTORY = {
    "response": {
        "time_series": [
            {
                "solar_energy_exported": 4000,
                "grid_energy_imported": 2500,
                "grid_energy_exported_from_solar": 1000,
                "grid_energy_exported_from_battery": 500,
                "grid_energy_exported_from_generator": 0,
                "consumer_energy_imported_from_grid": 2000,
                "consumer_energy_imported_from_solar": 1500,
                "consumer_energy_imported_from_battery": 700,
                "consumer_energy_imported_from_generator": 0,
            }
        ]
    }
}


def test_teslemetry_entity_names():
    """Entity helper builds prefixed ids."""
    api = MockTeslemetryAPI()
    assert api.entity("soc") == "sensor.predbat_teslemetry_soc"
    assert api.entity("operation_mode", domain="select") == "select.predbat_teslemetry_operation_mode"


def test_teslemetry_live_status_publishes_sensors():
    """live_status data is published as power/SOC sensors."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    run_async(api.fetch_live_status())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc"]["state"] == 55.5
    assert api.dashboard_items["sensor.predbat_teslemetry_battery_power"]["state"] == 1200
    assert api.dashboard_items["sensor.predbat_teslemetry_grid_power"]["state"] == -1400
    assert api.dashboard_items["sensor.predbat_teslemetry_solar_power"]["state"] == 800
    assert api.dashboard_items["sensor.predbat_teslemetry_load_power"]["state"] == 600


def test_teslemetry_site_info_publishes_soc_max():
    """site_info nameplate energy is published as soc_max in kWh."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5


def test_teslemetry_energy_today_publishes_kwh():
    """calendar_history energy series is aggregated into daily kWh sensors."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/calendar_history"] = ENERGY_HISTORY
    run_async(api.fetch_energy_today())
    assert api.dashboard_items["sensor.predbat_teslemetry_solar_today"]["state"] == 4.0
    assert api.dashboard_items["sensor.predbat_teslemetry_import_today"]["state"] == 2.5
    assert api.dashboard_items["sensor.predbat_teslemetry_export_today"]["state"] == 1.5
    assert api.dashboard_items["sensor.predbat_teslemetry_load_today"]["state"] == 4.2


def _make_real_request_api():
    """Build a MockTeslemetryAPI with the REAL TeslemetryAPI._request bound back in place of the canned override."""
    api = MockTeslemetryAPI()
    api._request = TeslemetryAPI._request.__get__(api)
    return api


def test_teslemetry_request_auth_failure():
    """A 401 response makes the real _request return None and latch api_auth_failed."""

    async def test():
        """Drive the real _request against a mocked 401 response."""
        api = _make_real_request_api()
        mock_response = create_aiohttp_mock_response(status=401, json_data={"error": "unauthorised"})
        mock_session = create_aiohttp_mock_session(mock_response)
        mock_session.request = mock_session.get
        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("teslemetry.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session
                result = await api._request("GET", "/api/1/energy_sites/123456/live_status")
        assert result is None
        assert api.api_auth_failed is True
        assert mock_session.request.call_count == 1  # No retries on auth failure
        assert any("auth failed" in msg for msg in api.log_messages)

    run_async(test())


def test_teslemetry_request_rate_limit_retry():
    """A 429 response is retried with backoff and the follow-up 200 JSON is returned."""

    async def test():
        """Drive the real _request against a mocked 429-then-200 response sequence."""
        api = _make_real_request_api()
        resp_429 = create_aiohttp_mock_response(status=429, json_data={"error": "rate limited"})
        resp_200 = create_aiohttp_mock_response(status=200, json_data={"response": {"ok": 1}})
        mock_session = create_aiohttp_mock_session(resp_200)
        mock_session.request = MagicMock(side_effect=[resp_429, resp_200])
        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("teslemetry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                mock_session_class.return_value = mock_session
                result = await api._request("GET", "/api/1/energy_sites/123456/site_info")
        assert result == {"response": {"ok": 1}}
        assert mock_session.request.call_count == 2
        assert mock_sleep.await_count == 1
        assert api.api_auth_failed is False

    run_async(test())


def test_teslemetry_request_success_clears_auth_flag():
    """A 200 response clears a previously latched api_auth_failed flag."""

    async def test():
        """Drive the real _request against a mocked 200 response with the auth flag pre-latched."""
        api = _make_real_request_api()
        api.api_auth_failed = True
        mock_response = create_aiohttp_mock_response(status=200, json_data={"response": {"percentage_charged": 50}})
        mock_session = create_aiohttp_mock_session(mock_response)
        mock_session.request = mock_session.get
        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("teslemetry.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session
                result = await api._request("GET", "/api/1/energy_sites/123456/live_status")
        assert result == {"response": {"percentage_charged": 50}}
        assert api.api_auth_failed is False

    run_async(test())


def _make_run_api_with_fetch_capture(site_info=True, live_status=True, energy_today=True):
    """Build a MockTeslemetryAPI whose fetch methods are stubbed to record calls, returning (api, calls list)."""
    api = MockTeslemetryAPI()
    calls = []

    async def fake_site_info():
        """Record a site_info fetch."""
        calls.append("site_info")
        return site_info

    async def fake_live_status():
        """Record a live_status fetch."""
        calls.append("live_status")
        return live_status

    async def fake_energy_today():
        """Record an energy_today fetch."""
        calls.append("energy_today")
        return energy_today

    api.fetch_site_info = fake_site_info
    api.fetch_live_status = fake_live_status
    api.fetch_energy_today = fake_energy_today
    return api, calls


def test_teslemetry_run_unconfigured_returns_false():
    """run() returns False (not None) when key or site_id is missing, so ComponentBase treats it as a failed cycle."""
    api, calls = _make_run_api_with_fetch_capture()
    api.api_key = ""
    assert run_async(api.run(0, True)) is False
    assert calls == []
    api = MockTeslemetryAPI()
    api.site_id = ""
    assert run_async(api.run(0, True)) is False


def test_teslemetry_run_first_success_returns_true():
    """A successful first run fetches everything and returns True; a gated-out steady-state cycle is a successful no-op."""
    api, calls = _make_run_api_with_fetch_capture()
    assert run_async(api.run(0, True)) is True
    assert calls == ["site_info", "live_status", "energy_today"]
    # Gated-out 60s tick: nothing due yet, but the cycle itself is healthy
    calls.clear()
    assert run_async(api.run(60, False)) is True
    assert calls == []
    # Failed live_status poll propagates as a failed cycle
    api, calls = _make_run_api_with_fetch_capture(live_status=False)
    assert run_async(api.run(0, True)) is False


def test_teslemetry_run_auth_failed_only_probes_live_status():
    """While auth-failed, run() probes ONLY live_status on the poll cadence (no site_info/energy, even when first)."""
    api, calls = _make_run_api_with_fetch_capture()
    api.api_auth_failed = True
    # Gated-out tick: no API traffic at all, cycle reports failure to keep backoff
    assert run_async(api.run(60, False)) is False
    assert calls == []
    # Poll due (first=True must NOT sneak in site_info while auth-failed)
    assert run_async(api.run(120, True)) is True
    assert calls == ["live_status"]


def test_teslemetry(my_predbat=None):
    """Run all Teslemetry component tests (registry entry point).

    Args:
        my_predbat: Unused; accepted for compatibility with the TEST_REGISTRY calling convention in unit_test.py.
    """
    test_teslemetry_entity_names()
    test_teslemetry_live_status_publishes_sensors()
    test_teslemetry_site_info_publishes_soc_max()
    test_teslemetry_energy_today_publishes_kwh()
    test_teslemetry_request_auth_failure()
    test_teslemetry_request_rate_limit_retry()
    test_teslemetry_request_success_clears_auth_flag()
    test_teslemetry_run_unconfigured_returns_false()
    test_teslemetry_run_first_success_returns_true()
    test_teslemetry_run_auth_failed_only_probes_live_status()
    print("**** Teslemetry tests passed ****")
    return 0
