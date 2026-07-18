# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Unit tests for the TeslemetryAPI component (Tesla Powerwall via Teslemetry)."""

import copy
from unittest.mock import MagicMock, patch, AsyncMock

from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session, run_async
from teslemetry import TeslemetryAPI, OPERATION_MODES, OPTIONS_TIME_FULL, DEFAULT_SCHEDULE


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
        self.site_info_done = False
        self.reconcile_done = False
        self.last_soc = None
        self.dashboard_items = {}
        self.log_messages = []
        self.entity_states = {}
        self.mock_responses = {}
        self.requests_made = []
        self._last_sent = {}
        self.schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.pending_schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.schedule_loaded = False
        self.automatic = False
        self.automatic_done = False

    def log(self, msg):
        """Capture log messages."""
        self.log_messages.append(msg)

    def dashboard_item(self, entity, state, attributes, app=None):
        """Capture dashboard items (mirrors production dashboard_item, which also calls through to
        set_state_wrapper - so entity_states reflects the latest write here too)."""
        self.dashboard_items[entity] = {"state": state, "attributes": attributes}
        self.entity_states[entity] = state

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

SITE_INFO_FULL = {
    "response": {
        "nameplate_energy": 13500,
        "nameplate_power": 11500,
        "max_site_meter_power_ac": 11500,
        "default_real_mode": "self_consumption",
        "backup_reserve_percent": 20,
    }
}

TARIFF_RATE_EXPORT_NOW = {"response": {"tariff_content_v2": {"version": 1, "utility": "Predbat", "code": "PREDBAT-EXPORT-NOW", "name": "Predbat (export_now)"}}}

TARIFF_RATE_NORMAL = {"response": {"tariff_content_v2": {"version": 1, "utility": "Predbat", "code": "PREDBAT-NORMAL", "name": "Predbat (normal)"}}}

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


def test_teslemetry_site_info_seeds_control_entity_states():
    """site_info seeds the operation_mode select and backup_reserve number entity states from the device (display only, no commands)."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    run_async(api.fetch_site_info())
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "self_consumption"
    assert api.entity_states["number.predbat_teslemetry_backup_reserve"] == 20
    assert api.requests_made == [("GET", "/api/1/energy_sites/123456/site_info", None)]


def test_teslemetry_energy_today_publishes_kwh():
    """calendar_history energy series is aggregated into daily kWh sensors."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    run_async(api.fetch_energy_today())
    assert api.dashboard_items["sensor.predbat_teslemetry_solar_today"]["state"] == 4.0
    assert api.dashboard_items["sensor.predbat_teslemetry_import_today"]["state"] == 2.5
    assert api.dashboard_items["sensor.predbat_teslemetry_export_today"]["state"] == 1.5
    assert api.dashboard_items["sensor.predbat_teslemetry_load_today"]["state"] == 4.2


def test_teslemetry_site_info_publishes_rate_and_limit():
    """site_info nameplate power and site AC limit are published as W sensors for automatic config."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_battery_rate_max"]["state"] == 11500
    assert api.dashboard_items["sensor.predbat_teslemetry_inverter_limit"]["state"] == 11500


def test_teslemetry_site_info_limit_kw_normalised():
    """A max_site_meter_power_ac reported in kW (small magnitude) is normalised to W."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_energy": 13500, "nameplate_power": 11500, "max_site_meter_power_ac": 11.5}}
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_inverter_limit"]["state"] == 11500


def test_teslemetry_live_status_tracks_last_soc():
    """fetch_live_status records the live SOC for the scheduler emulator."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    run_async(api.fetch_live_status())
    assert api.last_soc == 55.5


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


def test_teslemetry_select_operation_mode():
    """select_event on operation_mode POSTs /operation and updates entity state on success."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    assert ("POST", "/api/1/energy_sites/123456/operation", {"default_real_mode": "backup"}) in api.requests_made
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "backup"


def test_teslemetry_select_failure_keeps_state():
    """Failed command does not update entity state (write_and_poll will retry)."""
    api = MockTeslemetryAPI()
    # No mock response registered -> _request returns None -> command fails
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    assert "select.predbat_teslemetry_operation_mode" not in api.entity_states


def test_teslemetry_command_success_on_low_response_code():
    """_command treats a response.code below 400 as success."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    assert run_async(api._command("operation", {"default_real_mode": "backup"})) is True


def test_teslemetry_command_failure_on_application_error_key():
    """_command treats a body carrying an "error" key as a failure even though _request returned parsed JSON."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"error": "invalid_argument"}
    assert run_async(api._command("operation", {"default_real_mode": "backup"})) is False
    assert any("failed" in msg for msg in api.log_messages)


def test_teslemetry_command_failure_on_high_response_code():
    """_command treats an application-level response.code >= 400 as a failure despite a 2xx transport status."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 401}}
    assert run_async(api._command("operation", {"default_real_mode": "backup"})) is False
    assert any("failed" in msg for msg in api.log_messages)


def test_teslemetry_command_none_response_is_failure():
    """_command returns False when _request itself returns None (unchanged transport-failure behaviour)."""
    api = MockTeslemetryAPI()
    # No mock response registered -> _request returns None
    assert run_async(api._command("operation", {"default_real_mode": "backup"})) is False


def test_teslemetry_number_backup_reserve():
    """number_event on backup_reserve POSTs /backup with an integer percent."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/backup"] = {"response": {"code": 201}}
    run_async(api.number_event("number.predbat_teslemetry_backup_reserve", 100))
    assert ("POST", "/api/1/energy_sites/123456/backup", {"backup_reserve_percent": 100}) in api.requests_made
    assert api.entity_states["number.predbat_teslemetry_backup_reserve"] == 100


def test_teslemetry_switch_grid_charging():
    """switch_event maps turn_off to disallow grid charging."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.switch_event("switch.predbat_teslemetry_allow_charging_from_grid", "turn_off"))
    assert ("POST", "/api/1/energy_sites/123456/grid_import_export", {"disallow_charge_from_grid_with_solar_installed": True}) in api.requests_made
    assert api.entity_states["switch.predbat_teslemetry_allow_charging_from_grid"] == "off"


def test_teslemetry_select_export_rule():
    """select_event on allow_export POSTs the customer_preferred_export_rule."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_allow_export", "battery_ok"))
    assert ("POST", "/api/1/energy_sites/123456/grid_import_export", {"customer_preferred_export_rule": "battery_ok"}) in api.requests_made
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "battery_ok"


def test_teslemetry_site_info_latch():
    """site_info fetch is retried on every cycle until success; latch prevents redundant calls once successful."""
    api, calls = _make_run_api_with_fetch_capture(site_info=False, live_status=True, energy_today=True)
    # First cycle: site_info missing, fetch returns False, latch stays False
    assert run_async(api.run(0, True)) is True
    assert calls == ["site_info", "live_status", "energy_today"]
    assert api.site_info_done is False
    # Now provide the site_info mock and unbind fetch_site_info so the real one runs
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    api.fetch_site_info = TeslemetryAPI.fetch_site_info.__get__(api)
    # Re-create the fake live_status/energy_today so they still record and return True
    api.fetch_live_status = _make_run_api_with_fetch_capture(live_status=True)[0].fetch_live_status
    api.fetch_energy_today = _make_run_api_with_fetch_capture(energy_today=True)[0].fetch_energy_today
    # Run again: site_info_done latch should trigger fetch_site_info and succeed
    assert run_async(api.run(300, False)) is True
    # site_info_done should now be True and soc_max should be published
    assert api.site_info_done is True
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5


def test_teslemetry_build_tariff_export_now():
    """export_now tariff marks the current window ON_PEAK with a high sell rate."""
    from datetime import datetime

    api = MockTeslemetryAPI()
    now = datetime(2026, 7, 2, 14, 40)
    tariff = api.build_tariff("export_now", now=now)
    assert tariff["version"] == 1
    sell = tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]
    assert sell["ON_PEAK"] > sell["SUPER_OFF_PEAK"]
    periods = tariff["seasons"]["AllYear"]["tou_periods"]["ON_PEAK"]["periods"]
    assert periods[0]["fromHour"] == 14 and periods[0]["fromMinute"] == 30
    assert periods[0]["toHour"] == 15 and periods[0]["toMinute"] == 30


def test_teslemetry_build_tariff_normal_flat():
    """normal tariff is flat (single ALL rate, no ON_PEAK windows)."""
    api = MockTeslemetryAPI()
    tariff = api.build_tariff("normal")
    assert "ON_PEAK" not in tariff["seasons"]["AllYear"]["tou_periods"]
    assert tariff["energy_charges"]["AllYear"]["rates"]["SUPER_OFF_PEAK"] >= 0


def test_teslemetry_set_tariff_posts_tou_settings():
    """set_tariff wraps the tariff in tou_settings and POSTs it."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    result = run_async(api.set_tariff("normal"))
    assert result is True
    method, path, body = api.requests_made[-1]
    assert path == "/api/1/energy_sites/123456/time_of_use_settings"
    assert "tariff_content_v2" in body["tou_settings"]


def test_teslemetry_reconcile_on_start_device_marker_restores_normal():
    """Boot reconciliation reads the ACTUAL device tariff (not the local entity mirror); a PREDBAT-EXPORT-NOW
    marker on the device restores normal tariff + never export and updates the mirror entities on success."""
    api = MockTeslemetryAPI()
    # The entity mirror is always reseeded to "normal" by register_control_entities() on boot, so it must
    # be irrelevant here - only the device response drives the outcome.
    api.entity_states["select.predbat_teslemetry_tariff_mode"] = "normal"
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.reconcile_on_start())
    assert ("GET", "/api/1/energy_sites/123456/tariff_rate", None) in api.requests_made
    assert api.entity_states["select.predbat_teslemetry_tariff_mode"] == "normal"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "never"
    assert any("PREDBAT-EXPORT-NOW" in msg for msg in api.log_messages)


def test_teslemetry_reconcile_on_start_device_normal_is_noop():
    """Boot reconciliation issues zero command requests when the device tariff is not the export_now marker."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    run_async(api.reconcile_on_start())
    assert [req for req in api.requests_made if req[0] == "POST"] == []
    assert api.entity_states == {}


def test_teslemetry_reconcile_on_start_read_failure_skips_without_crash():
    """Boot reconciliation skips (no writes, no exception) when the device tariff read fails, logging a read failure."""
    api = MockTeslemetryAPI()
    # No mock response registered for tariff_rate -> _request returns None
    run_async(api.reconcile_on_start())
    assert [req for req in api.requests_made if req[0] == "POST"] == []
    assert api.entity_states == {}
    assert any("read failed" in msg for msg in api.log_messages)
    assert not any("no tariff code" in msg for msg in api.log_messages)


def test_teslemetry_reconcile_on_start_no_code_in_response_skips():
    """Boot reconciliation skips with a DISTINCT log when the tariff read succeeds but the response carries no code key."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = {"response": {"tariff_content_v2": {"version": 1, "utility": "SomeUtility"}}}
    run_async(api.reconcile_on_start())
    assert [req for req in api.requests_made if req[0] == "POST"] == []
    assert api.entity_states == {}
    assert any("no tariff code" in msg for msg in api.log_messages)
    assert not any("read failed" in msg for msg in api.log_messages)


def test_teslemetry_reconcile_on_start_read_only_mode_skips_writes():
    """When get_arg reports read-only, a device tariff still marked PREDBAT-EXPORT-NOW is logged but not written back."""
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    api.base = SimpleNamespace(set_read_only=True, get_arg=lambda arg, default=None, **kwargs: True if arg == "set_read_only" else default)
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.reconcile_on_start())
    assert [req for req in api.requests_made if req[0] == "POST"] == []
    assert api.entity_states == {}
    assert any("read-only" in msg.lower() for msg in api.log_messages)


def test_teslemetry_reconcile_on_start_ignores_boot_default_read_only_attribute():
    """The recovery write must NOT be gated on base.set_read_only: the constructor defaults that attribute to True
    and it is only refreshed from config in the fetch cycle AFTER phase-1 components start, so at reconcile time it
    is always True. With get_arg (the real config source) reporting False, the writes must go ahead regardless."""
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    # Simulate the real boot condition: stale constructor default True on the attribute, real config value False.
    api.base = SimpleNamespace(set_read_only=True, get_arg=lambda arg, default=None, **kwargs: False if arg == "set_read_only" else default)
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.reconcile_on_start())
    assert any(req[1].endswith("/time_of_use_settings") for req in api.requests_made if req[0] == "POST")
    assert any(req[1].endswith("/grid_import_export") for req in api.requests_made if req[0] == "POST")
    assert api.entity_states["select.predbat_teslemetry_tariff_mode"] == "normal"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "never"
    assert not any("read-only" in msg.lower() for msg in api.log_messages)


def _assert_tou_periods_partition_day(tou_periods):
    """Assert the tou_periods cover every minute of the (circular) day exactly once — no overlaps, no gaps."""
    covered = [0] * (24 * 60)
    for group in tou_periods.values():
        for period in group["periods"]:
            from_min = period["fromHour"] * 60 + period["fromMinute"]
            to_min = period["toHour"] * 60 + period["toMinute"]
            if to_min <= from_min:
                to_min += 24 * 60
            for minute in range(from_min, to_min):
                covered[minute % (24 * 60)] += 1
    assert max(covered) <= 1, "tou_periods overlap"
    assert min(covered) >= 1, "tou_periods leave a gap"


def test_teslemetry_build_tariff_export_now_midnight_wrap():
    """export_now at 23:40 wraps midnight: ON_PEAK 23:30-00:30 with a single non-overlapping off-peak complement."""
    from datetime import datetime

    api = MockTeslemetryAPI()
    tariff = api.build_tariff("export_now", now=datetime(2026, 7, 2, 23, 40))
    tou_periods = tariff["seasons"]["AllYear"]["tou_periods"]
    assert tou_periods["ON_PEAK"]["periods"] == [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 23, "fromMinute": 30, "toHour": 0, "toMinute": 30}]
    assert tou_periods["SUPER_OFF_PEAK"]["periods"] == [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 30, "toHour": 23, "toMinute": 30}]
    _assert_tou_periods_partition_day(tou_periods)


def test_teslemetry_build_tariff_export_now_midnight_exact():
    """export_now at 23:10 ends exactly at midnight: ON_PEAK 23:00-00:00 with a single off-peak complement."""
    from datetime import datetime

    api = MockTeslemetryAPI()
    tariff = api.build_tariff("export_now", now=datetime(2026, 7, 2, 23, 10))
    tou_periods = tariff["seasons"]["AllYear"]["tou_periods"]
    assert tou_periods["ON_PEAK"]["periods"] == [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 23, "fromMinute": 0, "toHour": 0, "toMinute": 0}]
    assert tou_periods["SUPER_OFF_PEAK"]["periods"] == [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": 23, "toMinute": 0}]
    _assert_tou_periods_partition_day(tou_periods)


def test_teslemetry_build_tariff_sell_clamp_above_export_rate():
    """ON_PEAK sell (and buy) stay above the live export rate even when it exceeds the static high rate."""
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    api.base = SimpleNamespace(minutes_now=600, rate_import={600: 30.0}, rate_export={600: 60.0})
    tariff = api.build_tariff("export_now")
    sell = tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]
    assert sell["SUPER_OFF_PEAK"] == 0.6
    assert sell["ON_PEAK"] > 0.6
    assert sell["ON_PEAK"] == 1.2
    # Buy-side ON_PEAK matches the high sell rate to discourage grid-charging during the export window
    assert tariff["energy_charges"]["AllYear"]["rates"]["ON_PEAK"] == sell["ON_PEAK"]


def test_teslemetry_reconcile_on_start_partial_failure():
    """Partial reconcile: tariff restore succeeds but the export-rule command fails, so only tariff_mode is updated."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    # grid_import_export mock ABSENT -> _request returns None -> set_export_rule fails
    run_async(api.reconcile_on_start())
    assert api.entity_states["select.predbat_teslemetry_tariff_mode"] == "normal"
    assert "select.predbat_teslemetry_allow_export" not in api.entity_states


def test_teslemetry_build_tariff_uses_site_local_time_not_utc():
    """build_tariff's export_now window must be expressed in the SITE'S LOCAL wall-clock time (the
    same clock Predbat schedules in), not raw UTC - the Powerwall applies tou_periods in local time.

    A base whose local time is 15:40 during BST (UTC+1) must yield an ON_PEAK window starting at
    15:30 local. A naive UTC-only implementation would instead read whatever the system clock (or,
    as sabotaged here, a mocked bogus UTC time) reports, producing the wrong window boundaries.
    """
    import pytz
    from datetime import datetime as real_datetime
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    london = pytz.timezone("Europe/London")
    local_now = london.localize(real_datetime(2026, 7, 2, 15, 40))
    api.base = SimpleNamespace(now_utc=local_now, local_tz=london, minutes_now=None, rate_import=None, rate_export=None)

    # Sabotage a naive implementation: if build_tariff ignores the base and calls datetime.now(timezone.utc)
    # directly, it will see this deliberately-wrong bogus time (03:05) instead of the real 15:40 local time above.
    with patch("teslemetry.datetime") as mock_datetime:
        mock_datetime.now.return_value = real_datetime(2026, 7, 2, 3, 5)
        tariff = api.build_tariff("export_now")

    periods = tariff["seasons"]["AllYear"]["tou_periods"]["ON_PEAK"]["periods"]
    assert periods[0]["fromHour"] == 15 and periods[0]["fromMinute"] == 30


def test_teslemetry_reconcile_latch_survives_auth_failed_first_cycle():
    """A first cycle that 401s (auth-failed, consuming `first`) must not permanently skip reconcile:
    once the API recovers on a later cycle, reconcile_on_start still runs and restores a stuck export
    state, because reconcile is gated on a `reconcile_done` latch rather than on `first`."""
    api, calls = _make_run_api_with_fetch_capture()
    api.api_auth_failed = True  # Simulate: the boot cycle already found the token dead.
    assert api.reconcile_done is False
    # Cycle 1 (first=True): auth-failed fast path, poll not yet due -> no HTTP traffic at all, no reconcile chance.
    assert run_async(api.run(0, True)) is False
    assert calls == []
    assert api.reconcile_done is False

    # Auth recovers (e.g. a live_status probe on an intervening cycle succeeded and cleared the flag).
    api.api_auth_failed = False
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    # Cycle 2 (first=False): reconcile must still run even though `first` was already consumed in cycle 1.
    assert run_async(api.run(300, False)) is True
    assert api.entity_states["select.predbat_teslemetry_tariff_mode"] == "normal"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "never"
    assert api.reconcile_done is True


def test_teslemetry_reconcile_latch_runs_once_on_healthy_boot():
    """A healthy first cycle runs reconcile exactly once; the latch prevents a redundant reconcile
    call (and its tariff_rate read) on every subsequent cycle."""
    api, calls = _make_run_api_with_fetch_capture()
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    assert run_async(api.run(0, True)) is True
    assert api.reconcile_done is True
    tariff_reads = [req for req in api.requests_made if req[1].endswith("/tariff_rate")]
    assert len(tariff_reads) == 1
    # A later cycle must not repeat the reconcile tariff_rate read.
    assert run_async(api.run(600, False)) is True
    tariff_reads = [req for req in api.requests_made if req[1].endswith("/tariff_rate")]
    assert len(tariff_reads) == 1


def test_teslemetry_site_info_returns_false_without_nameplate_so_soc_max_retries():
    """fetch_site_info must return False when nameplate_energy is absent/zero so run()'s
    site_info_done latch stays False and soc_max is retried on a later cycle - even though other
    fields (control-entity seeding) were published from the same response."""
    api = MockTeslemetryAPI()
    site_info_no_nameplate = {"response": {"nameplate_energy": 0, "default_real_mode": "self_consumption", "backup_reserve_percent": 20}}
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = site_info_no_nameplate
    assert run_async(api.fetch_site_info()) is False
    assert "sensor.predbat_teslemetry_soc_max" not in api.dashboard_items
    # Control-entity seeding still happens even though soc_max could not be published.
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "self_consumption"

    # Nameplate becomes available on a later cycle - now it must succeed and return True.
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    assert run_async(api.fetch_site_info()) is True
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5


def test_teslemetry_run_site_info_latch_stays_false_without_nameplate():
    """At the run() level, a site_info response without nameplate_energy must leave site_info_done
    False so the fetch is retried on a later cycle, rather than latching done with no soc_max ever
    published."""
    api = MockTeslemetryAPI()
    api.fetch_site_info = TeslemetryAPI.fetch_site_info.__get__(api)
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_energy": 0}}
    run_async(api.run(0, True))
    assert api.site_info_done is False


def test_teslemetry_energy_today_requests_kind_and_period():
    """fetch_energy_today must query calendar_history with kind=energy&period=day - the Fleet/Teslemetry
    endpoint requires both, and omitting them (the previous bare-path request) produces an invalid or
    empty response."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    run_async(api.fetch_energy_today())
    assert api.requests_made == [("GET", "/api/1/energy_sites/123456/calendar_history?kind=energy&period=day", None)]
    assert api.dashboard_items["sensor.predbat_teslemetry_solar_today"]["state"] == 4.0


def test_teslemetry_select_event_preserves_control_attributes():
    """select_event must not wipe the options/min/max/friendly_name attributes that
    register_control_entities set at init - every control write must re-apply them via
    publish_control, verified here through the dashboard_items capture (which records attributes,
    unlike the bare entity_states mirror)."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    item = api.dashboard_items["select.predbat_teslemetry_operation_mode"]
    assert item["state"] == "backup"
    assert item["attributes"].get("options") == OPERATION_MODES


def test_teslemetry_number_event_guards_null_value():
    """number_event must not crash on a null value (e.g. forwarded from a number.increment/decrement
    service call without an explicit value) - it should log a warning and post no command."""
    api = MockTeslemetryAPI()
    run_async(api.number_event("number.predbat_teslemetry_backup_reserve", None))
    assert api.requests_made == []
    assert "number.predbat_teslemetry_backup_reserve" not in api.entity_states
    assert any("invalid" in msg.lower() for msg in api.log_messages)


def test_teslemetry_dedupe_operation_mode_skips_repeat_post():
    """Sending the same operation_mode value twice via select_event posts only once (write-on-change)."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 1
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "backup"


def test_teslemetry_dedupe_operation_mode_resends_on_change():
    """A changed operation_mode value must still POST (dedupe only skips an unchanged value)."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "autonomous"))
    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 2
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "autonomous"


def test_teslemetry_dedupe_failed_post_not_cached_so_retries():
    """A failed POST must NOT update the dedupe cache, so the identical value is re-sent next cycle
    (failure-retry survives the write-on-change dedupe)."""
    api = MockTeslemetryAPI()
    # No mock response registered for /operation -> _request returns None -> command fails.
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    assert api.requests_made == [("POST", "/api/1/energy_sites/123456/operation", {"default_real_mode": "backup"})]
    assert "select.predbat_teslemetry_operation_mode" not in api.entity_states
    assert "operation_mode" not in api._last_sent
    # API recovers; the SAME value must still be sent - the cache was never populated on failure.
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 2
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "backup"


def test_teslemetry_dedupe_tariff_identical_body_skips_repeat_post():
    """Two normal-tariff builds with an identical body POST only once."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    run_async(api.set_tariff("normal"))
    run_async(api.set_tariff("normal"))
    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 1


def test_teslemetry_dedupe_tariff_resends_when_rates_change():
    """A tariff rebuild whose sell/buy price actually changed must re-POST rather than be deduped."""
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.base = SimpleNamespace(minutes_now=600, rate_import={600: 30.0}, rate_export={600: 15.0}, now_utc=None, local_tz=None)
    run_async(api.set_tariff("normal"))
    api.base.rate_export = {600: 60.0}  # Export rate jumps, so the sell-side signature changes.
    run_async(api.set_tariff("normal"))
    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 2


def test_teslemetry_drift_correction_refreshes_cache_and_reasserts():
    """A site_info poll reporting a device mode that differs from what Predbat last sent must refresh
    the dedupe cache to the ACTUAL device value, so the next assertion of Predbat's desired mode is not
    skipped - this is how externally-changed (Tesla app) drift gets corrected rather than silently kept."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    assert api._last_sent["operation_mode"] == "backup"
    # SITE_INFO reports default_real_mode = self_consumption, i.e. the user changed it externally.
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    run_async(api.fetch_site_info())
    assert api._last_sent["operation_mode"] == "self_consumption"
    # Predbat's desired mode is still "backup" - the drifted cache means this must re-POST.
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "backup"))
    posts = [req for req in api.requests_made if req[0] == "POST" and req[1].endswith("/operation")]
    assert len(posts) == 2


def test_teslemetry_drift_correction_no_spurious_resend_when_matching():
    """When site_info reports the SAME mode Predbat last sent, the dedupe cache is unchanged and the
    next assertion of that mode is still skipped - no spurious re-send from the drift-refresh itself."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "self_consumption"))
    assert api._last_sent["operation_mode"] == "self_consumption"
    # SITE_INFO's default_real_mode is also self_consumption - no drift.
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    run_async(api.fetch_site_info())
    assert api._last_sent["operation_mode"] == "self_consumption"
    run_async(api.select_event("select.predbat_teslemetry_operation_mode", "self_consumption"))
    posts = [req for req in api.requests_made if req[0] == "POST" and req[1].endswith("/operation")]
    assert len(posts) == 1


def test_teslemetry_dedupe_tariff_resends_on_window_advance():
    """A tariff rebuild whose live rate is UNCHANGED but whose export_now ON_PEAK window has
    advanced (a later base local time crosses into the next 30-minute-aligned window) must still
    re-POST - the dedupe signature is the full built tariff body (which embeds the window), not
    just the mode string, so a window-only move is not silently skipped.

    Would be RED under a hypothetical mode-string-keyed signature: if `set_tariff` deduped on
    `_apply_command("tariff", mode, ...)` instead of the built-tariff JSON, both calls below pass
    the identical mode "export_now", so `self._last_sent["tariff"] == "export_now"` would already
    match on the second call and the POST would be skipped (posts == 1) even though the device's
    ON_PEAK window moved from 14:30-15:30 to 15:00-16:00 - the customer would be left on a STALE,
    now-expired export window. GREEN as-built: the signature is
    `json.dumps(build_tariff(mode), sort_keys=True)`, which differs between the two builds purely
    because fromHour/fromMinute/toHour/toMinute moved, so both calls POST.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    # Live import/export rates held FIXED across both calls (same minutes_now, same rate tables) so
    # only the base local time - and therefore the window - differs between the two builds.
    api.base = SimpleNamespace(
        now_utc=datetime(2026, 7, 2, 14, 40, tzinfo=timezone.utc),
        local_tz=timezone.utc,
        minutes_now=600,
        rate_import={600: 28.0},
        rate_export={600: 15.0},
    )
    run_async(api.set_tariff("export_now"))

    # Advance base local time into the NEXT 30-minute window; rates untouched.
    api.base.now_utc = datetime(2026, 7, 2, 15, 10, tzinfo=timezone.utc)
    run_async(api.set_tariff("export_now"))

    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 2
    first_tariff = posts[0][2]["tou_settings"]["tariff_content_v2"]
    second_tariff = posts[1][2]["tou_settings"]["tariff_content_v2"]
    first_window = first_tariff["seasons"]["AllYear"]["tou_periods"]["ON_PEAK"]["periods"][0]
    second_window = second_tariff["seasons"]["AllYear"]["tou_periods"]["ON_PEAK"]["periods"][0]
    assert (first_window["fromHour"], first_window["fromMinute"]) == (14, 30)
    assert (second_window["fromHour"], second_window["fromMinute"]) == (15, 0)
    # Confirm the live rate really was constant - only the window differs between the two builds.
    first_on_peak_sell = first_tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]["ON_PEAK"]
    second_on_peak_sell = second_tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]["ON_PEAK"]
    assert first_on_peak_sell == second_on_peak_sell


def test_teslemetry_backup_reserve_drift_correction_refreshes_cache_and_reasserts():
    """A site_info poll reporting a device backup_reserve_percent that differs from what Predbat
    last sent must refresh the dedupe cache to the ACTUAL device value, so the next assertion of
    Predbat's desired reserve is not skipped - mirrors
    test_teslemetry_drift_correction_refreshes_cache_and_reasserts (operation_mode) but exercises
    the backup_reserve refresh path in fetch_site_info, which previously had no direct test."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/backup"] = {"response": {"code": 201}}
    run_async(api.number_event("number.predbat_teslemetry_backup_reserve", 20))
    assert api._last_sent["backup_reserve"] == 20
    # Device now reports backup_reserve_percent = 50, i.e. the user changed it externally.
    drifted_site_info = {"response": {"nameplate_energy": 13500, "default_real_mode": "self_consumption", "backup_reserve_percent": 50}}
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = drifted_site_info
    run_async(api.fetch_site_info())
    assert api._last_sent["backup_reserve"] == 50
    # Predbat's desired reserve is still 20 - the drifted cache means this must re-POST.
    run_async(api.number_event("number.predbat_teslemetry_backup_reserve", 20))
    posts = [req for req in api.requests_made if req[0] == "POST" and req[1].endswith("/backup")]
    assert len(posts) == 2


def test_teslemetry_backup_reserve_drift_correction_no_spurious_resend_when_matching():
    """When site_info reports the SAME backup_reserve Predbat last sent, the dedupe cache is
    unchanged and the next assertion of that value is still skipped - no spurious re-send from the
    drift-refresh itself."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/backup"] = {"response": {"code": 201}}
    run_async(api.number_event("number.predbat_teslemetry_backup_reserve", 20))
    assert api._last_sent["backup_reserve"] == 20
    # SITE_INFO's backup_reserve_percent is also 20 - no drift.
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    run_async(api.fetch_site_info())
    assert api._last_sent["backup_reserve"] == 20
    run_async(api.number_event("number.predbat_teslemetry_backup_reserve", 20))
    posts = [req for req in api.requests_made if req[0] == "POST" and req[1].endswith("/backup")]
    assert len(posts) == 1


def test_teslemetry_reconcile_forces_write_even_if_cache_preseeded():
    """reconcile_on_start's corrective writes must not be silently skipped by the dedupe cache even if
    it already holds a value matching what recovery is about to send (e.g. a future cache-preseed
    feature) - the recovery path always forces the POST regardless of cache state."""
    import json as _json

    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    # Pre-seed the dedupe cache as if "normal"/"never" had already been confirmed-sent - a naive
    # dedupe would treat the recovery writes below as no-ops and skip them entirely.
    api._last_sent["tariff"] = _json.dumps(api.build_tariff("normal"), sort_keys=True)
    api._last_sent["export_rule"] = "never"
    run_async(api.reconcile_on_start())
    assert any(req[1].endswith("/time_of_use_settings") for req in api.requests_made if req[0] == "POST")
    assert any(req[1].endswith("/grid_import_export") for req in api.requests_made if req[0] == "POST")
    assert api.entity_states["select.predbat_teslemetry_tariff_mode"] == "normal"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "never"


def test_teslemetry_inverter_def_tesla():
    """TESLA inverter type is registered with window control, no rate control and no export freeze."""
    from config import INVERTER_DEF

    tesla = INVERTER_DEF.get("TESLA")
    assert tesla is not None
    assert tesla["name"] == "Tesla Powerwall"
    assert tesla["has_charge_enable_time"] is True
    assert tesla["has_discharge_enable_time"] is True
    assert tesla["has_target_soc"] is True
    assert tesla["has_reserve_soc"] is True
    assert tesla["charge_time_entity_is_option"] is True
    assert tesla["charge_time_format"] == "HH:MM:SS"
    assert tesla["time_button_press"] is True
    assert tesla["output_charge_control"] == "none"
    assert tesla["support_charge_freeze"] is True
    assert tesla["support_discharge_freeze"] is False
    assert tesla["can_span_midnight"] is False
    assert tesla["target_soc_used_for_discharge"] is True
    # inverter.py reads the FoxCloud key set unconditionally - TESLA must not miss any of them
    for key in INVERTER_DEF["FoxCloud"]:
        assert key in tesla, "TESLA INVERTER_DEF missing key {}".format(key)


def test_teslemetry_component_registry_config():
    """Component registry exposes the automatic arg, can_restart, and the schema accepts teslemetry_automatic."""
    from components import COMPONENT_LIST
    from config import APPS_SCHEMA

    entry = COMPONENT_LIST["teslemetry"]
    assert entry["args"]["automatic"]["config"] == "teslemetry_automatic"
    assert entry["args"]["automatic"]["default"] is False
    assert entry["args"]["automatic"]["required"] is False
    assert entry.get("can_restart") is True
    assert APPS_SCHEMA["teslemetry_automatic"] == {"type": "boolean"}


def test_teslemetry_time_to_minutes():
    """HH:MM:SS strings convert to minutes since midnight; garbage converts to 0."""
    assert TeslemetryAPI.time_to_minutes("00:00:00") == 0
    assert TeslemetryAPI.time_to_minutes("05:30:00") == 330
    assert TeslemetryAPI.time_to_minutes("23:59:00") == 1439
    assert TeslemetryAPI.time_to_minutes("garbage") == 0


def test_teslemetry_in_window():
    """Window membership: inclusive start, exclusive end, disabled and midnight-wrap cases."""
    window = {"enable": 1, "start_time": "01:00:00", "end_time": "05:00:00"}
    assert TeslemetryAPI.in_window(60, window) is True
    assert TeslemetryAPI.in_window(299, window) is True
    assert TeslemetryAPI.in_window(300, window) is False
    assert TeslemetryAPI.in_window(0, window) is False
    assert TeslemetryAPI.in_window(60, {**window, "enable": 0}) is False
    assert TeslemetryAPI.in_window(60, {**window, "end_time": "01:00:00"}) is False
    wrap = {"enable": 1, "start_time": "23:00:00", "end_time": "01:00:00"}
    assert TeslemetryAPI.in_window(23 * 60 + 30, wrap) is True
    assert TeslemetryAPI.in_window(30, wrap) is True
    assert TeslemetryAPI.in_window(12 * 60, wrap) is False


def test_teslemetry_evaluate_schedule_states():
    """The five reachable device states: charging, hold-at-target, exporting, discharge floor, idle."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1},
        "discharge": {"start_time": "17:00:00", "end_time": "19:00:00", "soc": 30, "enable": 1},
    }
    assert api.evaluate_schedule(2 * 60, 50) == {"tariff_mode": "normal", "export_rule": "never", "grid_charging": True, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(2 * 60, 90) == {"tariff_mode": "normal", "export_rule": "never", "grid_charging": False, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(18 * 60, 80) == {"tariff_mode": "export_now", "export_rule": "battery_ok", "grid_charging": False, "reserve": 30, "mode": "autonomous"}
    assert api.evaluate_schedule(18 * 60, 30) == {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": False, "reserve": 30, "mode": "self_consumption"}
    assert api.evaluate_schedule(12 * 60, 60) == {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}


def test_teslemetry_evaluate_schedule_charge_precedence():
    """When charge and discharge windows overlap, charge wins (matches execute.py ordering)."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 100, "enable": 1},
        "discharge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 10, "enable": 1},
    }
    assert api.evaluate_schedule(2 * 60, 50)["mode"] == "backup"


def test_teslemetry_schedule_entities_published():
    """Schedule entities are published with option lists, ranges and safe defaults."""
    api = MockTeslemetryAPI()
    api.publish_schedule_entities()
    assert api.dashboard_items["select.predbat_teslemetry_schedule_charge_start_time"]["attributes"]["options"] == OPTIONS_TIME_FULL
    assert api.dashboard_items["select.predbat_teslemetry_schedule_discharge_end_time"]["state"] == "00:00:00"
    assert api.dashboard_items["number.predbat_teslemetry_schedule_reserve"]["state"] == 20
    assert api.dashboard_items["number.predbat_teslemetry_schedule_charge_soc"]["state"] == 100
    assert api.dashboard_items["number.predbat_teslemetry_schedule_discharge_soc"]["state"] == 10
    assert api.dashboard_items["switch.predbat_teslemetry_schedule_charge_enable"]["state"] == "off"
    assert api.dashboard_items["switch.predbat_teslemetry_schedule_write"]["state"] == "off"


def test_teslemetry_schedule_edits_stage_without_device_writes():
    """Entity writes accumulate in pending_schedule, mirror into entity state, and send nothing to the device."""
    api = MockTeslemetryAPI()
    run_async(api.select_event("select.predbat_teslemetry_schedule_charge_start_time", "01:30:00"))
    run_async(api.select_event("select.predbat_teslemetry_schedule_charge_end_time", "05:00:00"))
    run_async(api.number_event("number.predbat_teslemetry_schedule_charge_soc", 90))
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_charge_enable", "turn_on"))
    assert api.pending_schedule["charge"] == {"start_time": "01:30:00", "end_time": "05:00:00", "soc": 90, "enable": 1}
    assert api.schedule["charge"]["enable"] == 0
    assert api.requests_made == []
    assert api.entity_states["select.predbat_teslemetry_schedule_charge_start_time"] == "01:30:00"
    assert api.entity_states["switch.predbat_teslemetry_schedule_charge_enable"] == "on"


def test_teslemetry_schedule_write_button_commits():
    """The write button copies pending to committed and leaves the button off."""
    api = MockTeslemetryAPI()
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_discharge_enable", "turn_on"))
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_write", "turn_on"))
    assert api.schedule["discharge"]["enable"] == 1
    assert api.entity_states["switch.predbat_teslemetry_schedule_write"] == "off"


def test_teslemetry_schedule_invalid_values_rejected():
    """Garbage times and non-numeric SOC values are rejected or clamped without corrupting the schedule."""
    api = MockTeslemetryAPI()
    run_async(api.select_event("select.predbat_teslemetry_schedule_charge_start_time", "25:99:00"))
    assert api.pending_schedule["charge"]["start_time"] == "00:00:00"
    run_async(api.number_event("number.predbat_teslemetry_schedule_charge_soc", "banana"))
    assert api.pending_schedule["charge"]["soc"] == 100
    run_async(api.number_event("number.predbat_teslemetry_schedule_reserve", 150))
    assert api.pending_schedule["reserve"] == 100


def test_teslemetry_schedule_reserve_applies_immediately():
    """Reserve edits commit without the write button (fox parity) and persist into both schedules."""
    api = MockTeslemetryAPI()
    run_async(api.number_event("number.predbat_teslemetry_schedule_reserve", 35))
    assert api.pending_schedule["reserve"] == 35
    assert api.schedule["reserve"] == 35


def test_teslemetry(my_predbat=None):
    """Run all Teslemetry component tests (registry entry point).

    Args:
        my_predbat: Unused; accepted for compatibility with the TEST_REGISTRY calling convention in unit_test.py.
    """
    test_teslemetry_entity_names()
    test_teslemetry_live_status_publishes_sensors()
    test_teslemetry_site_info_publishes_soc_max()
    test_teslemetry_site_info_seeds_control_entity_states()
    test_teslemetry_energy_today_publishes_kwh()
    test_teslemetry_request_auth_failure()
    test_teslemetry_request_rate_limit_retry()
    test_teslemetry_request_success_clears_auth_flag()
    test_teslemetry_run_unconfigured_returns_false()
    test_teslemetry_run_first_success_returns_true()
    test_teslemetry_run_auth_failed_only_probes_live_status()
    test_teslemetry_select_operation_mode()
    test_teslemetry_select_failure_keeps_state()
    test_teslemetry_command_success_on_low_response_code()
    test_teslemetry_command_failure_on_application_error_key()
    test_teslemetry_command_failure_on_high_response_code()
    test_teslemetry_command_none_response_is_failure()
    test_teslemetry_number_backup_reserve()
    test_teslemetry_switch_grid_charging()
    test_teslemetry_select_export_rule()
    test_teslemetry_site_info_latch()
    test_teslemetry_build_tariff_export_now()
    test_teslemetry_build_tariff_normal_flat()
    test_teslemetry_set_tariff_posts_tou_settings()
    test_teslemetry_reconcile_on_start_device_marker_restores_normal()
    test_teslemetry_reconcile_on_start_device_normal_is_noop()
    test_teslemetry_reconcile_on_start_read_failure_skips_without_crash()
    test_teslemetry_reconcile_on_start_no_code_in_response_skips()
    test_teslemetry_reconcile_on_start_read_only_mode_skips_writes()
    test_teslemetry_reconcile_on_start_ignores_boot_default_read_only_attribute()
    test_teslemetry_build_tariff_export_now_midnight_wrap()
    test_teslemetry_build_tariff_export_now_midnight_exact()
    test_teslemetry_build_tariff_sell_clamp_above_export_rate()
    test_teslemetry_reconcile_on_start_partial_failure()
    test_teslemetry_build_tariff_uses_site_local_time_not_utc()
    test_teslemetry_reconcile_latch_survives_auth_failed_first_cycle()
    test_teslemetry_reconcile_latch_runs_once_on_healthy_boot()
    test_teslemetry_site_info_returns_false_without_nameplate_so_soc_max_retries()
    test_teslemetry_run_site_info_latch_stays_false_without_nameplate()
    test_teslemetry_energy_today_requests_kind_and_period()
    test_teslemetry_select_event_preserves_control_attributes()
    test_teslemetry_number_event_guards_null_value()
    test_teslemetry_dedupe_operation_mode_skips_repeat_post()
    test_teslemetry_dedupe_operation_mode_resends_on_change()
    test_teslemetry_dedupe_failed_post_not_cached_so_retries()
    test_teslemetry_dedupe_tariff_identical_body_skips_repeat_post()
    test_teslemetry_dedupe_tariff_resends_when_rates_change()
    test_teslemetry_drift_correction_refreshes_cache_and_reasserts()
    test_teslemetry_drift_correction_no_spurious_resend_when_matching()
    test_teslemetry_dedupe_tariff_resends_on_window_advance()
    test_teslemetry_backup_reserve_drift_correction_refreshes_cache_and_reasserts()
    test_teslemetry_backup_reserve_drift_correction_no_spurious_resend_when_matching()
    test_teslemetry_reconcile_forces_write_even_if_cache_preseeded()
    test_teslemetry_inverter_def_tesla()
    test_teslemetry_component_registry_config()
    test_teslemetry_site_info_publishes_rate_and_limit()
    test_teslemetry_site_info_limit_kw_normalised()
    test_teslemetry_live_status_tracks_last_soc()
    test_teslemetry_time_to_minutes()
    test_teslemetry_in_window()
    test_teslemetry_evaluate_schedule_states()
    test_teslemetry_evaluate_schedule_charge_precedence()
    test_teslemetry_schedule_entities_published()
    test_teslemetry_schedule_edits_stage_without_device_writes()
    test_teslemetry_schedule_write_button_commits()
    test_teslemetry_schedule_invalid_values_rejected()
    test_teslemetry_schedule_reserve_applies_immediately()
    print("**** Teslemetry tests passed ****")
    return 0
