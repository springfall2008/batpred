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
        self.site_info_done = False
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
    """Boot reconciliation skips silently (no writes, no exception) when the device tariff read fails."""
    api = MockTeslemetryAPI()
    # No mock response registered for tariff_rate -> _request returns None
    run_async(api.reconcile_on_start())
    assert [req for req in api.requests_made if req[0] == "POST"] == []
    assert api.entity_states == {}


def test_teslemetry_reconcile_on_start_read_only_mode_skips_writes():
    """In read-only mode, a device tariff still marked PREDBAT-EXPORT-NOW is logged but not written back."""
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    api.base = SimpleNamespace(set_read_only=True)
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_EXPORT_NOW
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.reconcile_on_start())
    assert [req for req in api.requests_made if req[0] == "POST"] == []
    assert api.entity_states == {}
    assert any("read-only" in msg.lower() for msg in api.log_messages)


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
    test_teslemetry_reconcile_on_start_read_only_mode_skips_writes()
    test_teslemetry_build_tariff_export_now_midnight_wrap()
    test_teslemetry_build_tariff_export_now_midnight_exact()
    test_teslemetry_build_tariff_sell_clamp_above_export_rate()
    test_teslemetry_reconcile_on_start_partial_failure()
    print("**** Teslemetry tests passed ****")
    return 0
