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


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self):
        """Create the empty in-memory store."""
        self.saved = {}

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record saved data keyed on (module, filename)."""
        self.saved[(module, filename)] = data
        return True

    async def load(self, module, filename):
        """Return previously saved data or None."""
        return self.saved.get((module, filename))


class MockTeslemetryAPI(TeslemetryAPI):
    """TeslemetryAPI test double that avoids ComponentBase initialisation."""

    mock_storage = None

    def __init__(self):
        """Initialise the mock with captured state instead of ComponentBase wiring."""
        self.api_key = "test_token"
        self.site_id = "123456"
        self.site_filter = []
        self.base_url = "https://api.teslemetry.com"
        self.prefix = "predbat"
        self.api_auth_failed = False
        self.last_live_poll = 0
        self.last_energy_poll = 0
        self.site_info_done = False
        self.last_soc = None
        self.soc_max_real = False
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
        self.args_set = {}

    @property
    def storage(self):
        """Return the fake storage component for tests (None by default)."""
        return self.mock_storage

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

    def set_arg(self, arg, value):
        """Capture set_arg calls for automatic_config assertions."""
        self.args_set[arg] = value


def _rate_base(import_p, export_p):
    """A minimal base double exposing flat import/export rate dicts and a local clock for build_tariff.

    get_arg's keyword-only "d" never matches the "default=" keyword ComponentBase.get_arg forwards
    with, so it always falls through to its own None default (never the caller's default) - which
    reads as not read-only since bool(None) is False. This matches how
    test_teslemetry_run_boots_without_reconcile wires its own base double - so _is_read_only() (consulted
    by sync_tariff) can be called against this double without every test needing its own override.
    """
    from types import SimpleNamespace
    from datetime import datetime

    rate_import = {m: import_p for m in range(0, 2880)}
    rate_export = {m: export_p for m in range(0, 2880)}
    return SimpleNamespace(rate_import=rate_import, rate_export=rate_export, minutes_now=0, now=datetime(2026, 7, 20, 12, 0), local_tz=None, get_arg=lambda a, d=None, **k: d)


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
        "tariff_content_v2": {"code": "PREDBAT-NORMAL"},
    }
}

SITE_INFO_FULL = {
    "response": {
        "nameplate_energy": 13500,
        "nameplate_power": 11500,
        "max_site_meter_power_ac": 11500,
        "default_real_mode": "self_consumption",
        "backup_reserve_percent": 20,
        "tariff_content_v2": {"code": "PREDBAT-NORMAL"},
    }
}

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
        # Raw response is logged for live cross-checking.
        assert any('"percentage_charged": 50' in msg and "response:" in msg for msg in api.log_messages)

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
    """run() returns False (not None) when the key is missing or a site cannot be resolved, so ComponentBase treats it as a failed cycle."""
    api, calls = _make_run_api_with_fetch_capture()
    api.api_key = ""
    assert run_async(api.run(0, True)) is False
    assert calls == []
    # No site resolved and /api/1/products returns nothing (no mock registered) -> discovery fails -> run() False.
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


def test_teslemetry_run_boots_without_reconcile():
    """A healthy first cycle asserts device state directly, with no tariff-read reconcile call."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.base.get_arg = lambda a, d=None, **k: d  # not read-only
    api.mock_responses["/api/1/products"] = {"response": [{"energy_site_id": 123456}]}
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/backup"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.run(seconds=0, first=True))
    assert not hasattr(api, "reconcile_done")
    assert not any("reconcile" in m.lower() for m in api.log_messages)


def test_teslemetry_boot_resumes_from_persisted_schedule():
    """With a persisted mid-discharge schedule and empty dedupe cache, the first cycle asserts autonomous export."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.base.get_arg = lambda a, d=None, **k: d
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": 0},
        "discharge": {"start_time": "00:00:00", "end_time": "23:59:00", "soc": 10, "enable": 1},
    }
    api.schedule_loaded = True
    api.last_soc = 80
    for path in ("operation", "backup", "grid_import_export", "time_of_use_settings"):
        api.mock_responses["/api/1/energy_sites/123456/" + path] = {"response": {"code": 201}}
    run_async(api.assert_device_state(api.evaluate_schedule(12 * 60, 80)))
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "autonomous"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "battery_ok"


def test_teslemetry_run_exports_with_aligned_tariff_boost():
    """A committed discharge window active NOW with soc > target fires both per-cycle levers in
    agreement within one cycle: the mode/export-rule commands go autonomous/battery_ok AND the
    synced tariff carries an ON_PEAK sell boost that strictly dominates every real band.

    Drives sync_tariff() followed by assert_device_state(evaluate_schedule(...)) - exactly what
    run() does each healthy cycle - rather than the full run() path, so this does not also need to
    mock the site_info/live_status/energy_today fetch endpoints that are irrelevant here.
    """
    api = MockTeslemetryAPI()
    api.base = _rate_base(28.0, 15.0)  # import 28p, export 15p
    api.base.get_arg = lambda a, d=None, **k: d  # not read-only
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": 0},
        "discharge": {"start_time": "00:00:00", "end_time": "23:59:00", "soc": 10, "enable": 1},
    }
    api.schedule_loaded = True
    api.last_soc = 80
    for path in ("operation", "backup", "grid_import_export", "time_of_use_settings"):
        api.mock_responses["/api/1/energy_sites/123456/" + path] = {"response": {"code": 201}}

    run_async(api.sync_tariff())
    run_async(api.assert_device_state(api.evaluate_schedule(api.get_minutes_now(), api.last_soc)))

    # Lever 1: mode/export-rule commands agree on an active export window (soc 80 > target 10).
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "autonomous"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "battery_ok"

    # Lever 2: the synced tariff's sell side carries an ON_PEAK boost, non-empty and strictly above
    # every non-ON_PEAK sell price - proving the boost is present and aligned with the export window.
    tariff_posts = [req for req in api.requests_made if req[0] == "POST" and req[1].endswith("/time_of_use_settings")]
    assert len(tariff_posts) == 1
    tariff = tariff_posts[0][2]["tou_settings"]["tariff_content_v2"]
    sell_periods = tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]
    assert sell_periods.get("ON_PEAK", {}).get("periods")
    sell_rates = tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]
    boost = sell_rates["ON_PEAK"]
    assert all(boost > price for tier, price in sell_rates.items() if tier != "ON_PEAK")


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


def test_teslemetry_build_tariff_single_code_real_bands():
    """build_tariff() with no window yields one PREDBAT tariff, GBP, with real bands and no ON_PEAK."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)  # helper below
    tariff = api.build_tariff(None)
    assert tariff["code"] == "PREDBAT"
    assert tariff["currency"] == "GBP"
    assert "ON_PEAK" not in tariff["seasons"]["AllYear"]["tou_periods"]
    assert "ON_PEAK" not in tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]


def test_teslemetry_build_tariff_boost_is_strict_max_on_today_dow():
    """A discharge window adds ON_PEAK above every real band, on today's DOW only (both sides)."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    tariff = api.build_tariff((1020, 1080), now_min=600)  # 17:00-18:00 window, now 10:00 -> today
    sell_periods = tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]
    today_dow = api._tesla_dow(api.base.now.weekday())
    assert set(p["fromDayOfWeek"] for p in sell_periods["ON_PEAK"]["periods"]) == {today_dow}
    boost = tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]["ON_PEAK"]
    real = [v for t, v in tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"].items() if t != "ON_PEAK"]
    assert all(boost > v for v in real)
    assert tariff["energy_charges"]["AllYear"]["rates"]["ON_PEAK"] == boost  # buy mirror


def test_teslemetry_build_tariff_fallback_flat_when_no_rates():
    """No base/rates -> flat tariff via the ALL field, still schema-valid, boost still overlays."""
    api = MockTeslemetryAPI()
    api.base = None
    tariff = api.build_tariff(None)
    assert tariff["energy_charges"]["ALL"]["rates"]["ALL"] >= 0


def test_teslemetry_build_tariff_boost_clamps_above_high_rates():
    """Boost is 2x the highest real band when that exceeds the static EXPORT_SELL_RATE floor, and the
    buy-side ON_PEAK mirrors the sell-side boost so grid-charging is discouraged during the window."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=30.0, export_p=60.0)
    tariff = api.build_tariff((1020, 1080), now_min=600)
    sell = tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]
    assert sell["SUPER_OFF_PEAK"] == 0.6
    assert sell["ON_PEAK"] == 1.2
    assert tariff["energy_charges"]["AllYear"]["rates"]["ON_PEAK"] == sell["ON_PEAK"]


def test_teslemetry_build_tariff_periods_partition_each_day():
    """Every day-of-week's rendered periods (buy and sell) partition [0, 1440) exactly, boost included."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    tariff = api.build_tariff((1020, 1080), now_min=600)
    for tou_periods in (tariff["seasons"]["AllYear"]["tou_periods"], tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]):
        for day in range(7):
            day_periods = {tier: {"periods": [p for p in block["periods"] if p["fromDayOfWeek"] == day]} for tier, block in tou_periods.items()}
            _assert_tou_periods_partition_day(day_periods)


def test_teslemetry_set_tariff_posts_tou_settings():
    """set_tariff wraps a prebuilt tariff in tou_settings and POSTs it."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    t = api.build_tariff(None)
    result = run_async(api.set_tariff(t))
    assert result is True
    method, path, body = api.requests_made[-1]
    assert path == "/api/1/energy_sites/123456/time_of_use_settings"
    assert "tariff_content_v2" in body["tou_settings"]


def test_teslemetry_sync_tariff_dedupes_unchanged():
    """Two syncs with identical inputs push the tariff exactly once (monthly API-call budget)."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    run_async(api.sync_tariff())
    run_async(api.sync_tariff())
    posts = [r for r in api.requests_made if r[0] == "POST" and r[1].endswith("/time_of_use_settings")]
    assert len(posts) == 1


def test_teslemetry_sync_tariff_pushes_on_window_change():
    """Enabling a discharge window changes the tariff and triggers a second push."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    run_async(api.sync_tariff())
    api.schedule["discharge"] = {"start_time": "17:00:00", "end_time": "18:00:00", "soc": 30, "enable": 1}
    run_async(api.sync_tariff())
    posts = [r for r in api.requests_made if r[0] == "POST" and r[1].endswith("/time_of_use_settings")]
    assert len(posts) == 2


def test_teslemetry_sync_tariff_read_only_no_push():
    """Read-only mode sends no tariff command."""
    from types import SimpleNamespace

    api = MockTeslemetryAPI()
    api.base = SimpleNamespace(rate_import={m: 28.0 for m in range(2880)}, rate_export={m: 15.0 for m in range(2880)}, minutes_now=0, now=None, local_tz=None, get_arg=lambda a, d=None, **k: True if a == "set_read_only" else d)
    run_async(api.sync_tariff())
    assert not [r for r in api.requests_made if r[0] == "POST"]


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


def test_teslemetry_emulator_failure_does_not_fail_run():
    """A healthy data path with every command endpoint failing must still let run() return True:
    the scheduler emulator's writes are best-effort and self-retrying, and must never fail the
    data-fetch cycle that ComponentBase's health monitoring depends on."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    # Deliberately NOT calling command_ok_responses(api): every emulator POST (operation, backup,
    # grid_import_export) has no mock response registered, so _command's `result is None` branch
    # makes every one of them fail.
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    api.get_minutes_now = lambda: 12 * 60
    assert run_async(api.run(seconds=0, first=True)) is True
    # export_rule/grid_charging have no drift-correction cache pre-seed (unlike operation_mode/
    # backup_reserve, which SITE_INFO_FULL happens to already match the idle target for, so those
    # two would be deduped away with no POST at all) - so the emulator's grid_import_export write is
    # always attempted on a fresh boot, confirming it really tried and failed rather than being
    # silently skipped.
    posts = [req[1] for req in api.requests_made if req[0] == "POST"]
    assert "/api/1/energy_sites/123456/grid_import_export" in posts


def test_teslemetry_site_info_latches_without_nameplate_soc_max_from_live_status():
    """fetch_site_info returns True on any response (so site_info_done latches and auto-config is not
    blocked) even when nameplate_energy is absent; soc_max instead comes from live_status
    total_pack_energy - the reliable capacity source on PW3, whose site_info omits nameplate_energy."""
    api = MockTeslemetryAPI()
    site_info_no_nameplate = {"response": {"nameplate_energy": 0, "nameplate_power": 11500, "default_real_mode": "self_consumption", "backup_reserve_percent": 20}}
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = site_info_no_nameplate
    assert run_async(api.fetch_site_info()) is True
    assert "sensor.predbat_teslemetry_soc_max" not in api.dashboard_items
    # Control-entity seeding still happens even though soc_max was not published from site_info.
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "self_consumption"

    # soc_max is published from live_status total_pack_energy (Wh -> kWh).
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = {"response": {"percentage_charged": 50, "total_pack_energy": 13500}}
    run_async(api.fetch_live_status())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5


def test_teslemetry_run_site_info_latches_on_any_response():
    """At the run() level, a site_info response latches site_info_done True even without nameplate_energy:
    soc_max no longer gates the latch (it comes from live_status), so automatic_config is not blocked."""
    api = MockTeslemetryAPI()
    api.fetch_site_info = TeslemetryAPI.fetch_site_info.__get__(api)
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_energy": 0}}
    run_async(api.run(0, True))
    assert api.site_info_done is True


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
    """Two identical prebuilt tariff pushes POST only once."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    tariff = api.build_tariff(None)
    run_async(api.set_tariff(tariff))
    run_async(api.set_tariff(tariff))
    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 1


def test_teslemetry_dedupe_tariff_resends_when_rates_change():
    """A tariff rebuild whose sell/buy price actually changed must re-POST rather than be deduped."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    run_async(api.set_tariff(api.build_tariff(None)))
    api.base = _rate_base(import_p=28.0, export_p=60.0)  # Export rate jumps, so the sell-side signature changes.
    run_async(api.set_tariff(api.build_tariff(None)))
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
    """A tariff rebuild whose live rate is UNCHANGED but whose boost window has rolled from today to
    tomorrow (now_min has passed the window's end) must still re-POST - the dedupe signature is the
    full built tariff body (which embeds the boost's day-of-week placement), not just the rates, so a
    window-only roll is not silently skipped.

    Would be RED under a hypothetical rate-only signature: if `set_tariff` deduped on just the
    energy_charges values, both builds below have identical rates (same base throughout), so a naive
    signature would match on the second call and the POST would be skipped even though the boost
    moved from today's day-of-week to tomorrow's - the customer would be left exporting on the wrong
    day. GREEN as-built: the signature is the full `json.dumps(tariff, sort_keys=True)`, which differs
    between the two builds purely because the ON_PEAK periods' fromDayOfWeek moved, so both calls POST.
    """
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    window = (1020, 1080)  # 17:00-18:00
    run_async(api.set_tariff(api.build_tariff(window, now_min=600)))  # 10:00 -> window still today
    run_async(api.set_tariff(api.build_tariff(window, now_min=1100)))  # 18:20 -> window rolled to tomorrow

    posts = [req for req in api.requests_made if req[0] == "POST"]
    assert len(posts) == 2
    first_tariff = posts[0][2]["tou_settings"]["tariff_content_v2"]
    second_tariff = posts[1][2]["tou_settings"]["tariff_content_v2"]
    first_dow = first_tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]["ON_PEAK"]["periods"][0]["fromDayOfWeek"]
    second_dow = second_tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]["ON_PEAK"]["periods"][0]["fromDayOfWeek"]
    assert first_dow != second_dow
    # Confirm the live rate really was constant - only the boost's day-of-week placement differs.
    first_rate = first_tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]["ON_PEAK"]
    second_rate = second_tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]["ON_PEAK"]
    assert first_rate == second_rate


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
    """The five reachable device states without the removed tariff_mode lever."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1},
        "discharge": {"start_time": "17:00:00", "end_time": "19:00:00", "soc": 30, "enable": 1},
    }
    assert api.evaluate_schedule(2 * 60, 50) == {"export_rule": "pv_only", "grid_charging": True, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(2 * 60, 90) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(18 * 60, 80) == {"export_rule": "battery_ok", "grid_charging": False, "reserve": 30, "mode": "autonomous"}
    assert api.evaluate_schedule(18 * 60, 30) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 30, "mode": "self_consumption"}
    assert api.evaluate_schedule(12 * 60, 60) == {"export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}
    # Invariant: export_rule is pv_only in every state except the active grid-export window (battery_ok),
    # so the rule is only rewritten on entry/exit of an export window - never on an ordinary charge cycle.
    non_export_states = [api.evaluate_schedule(2 * 60, 50), api.evaluate_schedule(2 * 60, 90), api.evaluate_schedule(18 * 60, 30), api.evaluate_schedule(12 * 60, 60)]
    assert all(state["export_rule"] == "pv_only" for state in non_export_states)


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


def test_teslemetry_schedule_persistence_roundtrip():
    """apply_schedule persists the committed schedule; load_schedule restores it and resets pending."""
    api = MockTeslemetryAPI()
    api.mock_storage = FakeStorage()
    api.pending_schedule["charge"]["enable"] = 1
    api.pending_schedule["charge"]["start_time"] = "02:00:00"
    run_async(api.apply_schedule())
    assert api.mock_storage.saved[("teslemetry", "schedule")]["charge"]["enable"] == 1
    api2 = MockTeslemetryAPI()
    api2.mock_storage = api.mock_storage
    run_async(api2.load_schedule())
    assert api2.schedule["charge"]["start_time"] == "02:00:00"
    assert api2.pending_schedule == api2.schedule


def test_teslemetry_schedule_load_without_storage_is_safe():
    """With no storage component available, load_schedule keeps the safe defaults."""
    api = MockTeslemetryAPI()
    run_async(api.load_schedule())
    assert api.schedule == DEFAULT_SCHEDULE


def command_ok_responses(api):
    """Register success responses for all four Powerwall command endpoints."""
    for path in ["operation", "backup", "grid_import_export", "time_of_use_settings"]:
        api.mock_responses["/api/1/energy_sites/123456/{}".format(path)] = {"response": {}}


def test_teslemetry_assert_device_state_posts_commands():
    """assert_device_state issues THREE device commands (export-rule + grid-charging share one grid_import_export POST; tariff is synced separately) and mirrors success into the diagnostic entities."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    desired = {"export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}
    assert run_async(api.assert_device_state(desired)) is True
    posts = [(req[1], req[2]) for req in api.requests_made if req[0] == "POST"]
    paths = [path for path, _body in posts]
    assert len(paths) == 3
    assert "/api/1/energy_sites/123456/time_of_use_settings" not in paths
    assert "/api/1/energy_sites/123456/backup" in paths
    assert "/api/1/energy_sites/123456/operation" in paths
    # Exactly ONE grid_import_export POST, carrying BOTH fields (not two single-field POSTs).
    grid_posts = [body for path, body in posts if path.endswith("/grid_import_export")]
    assert len(grid_posts) == 1
    assert grid_posts[0] == {"customer_preferred_export_rule": "pv_only", "disallow_charge_from_grid_with_solar_installed": False}
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "self_consumption"
    assert api.entity_states["number.predbat_teslemetry_backup_reserve"] == 20
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "pv_only"
    assert api.entity_states["switch.predbat_teslemetry_allow_charging_from_grid"] == "on"


def test_teslemetry_set_grid_import_export_single_post_and_coherent():
    """The combined setter writes both fields in one POST, dedupes on the pair, and shares the per-field caches."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    # Entering an export window changes BOTH fields -> exactly one POST carrying both.
    assert run_async(api.set_grid_import_export("battery_ok", False)) is True
    grid_posts = [req[2] for req in api.requests_made if req[0] == "POST" and req[1].endswith("/grid_import_export")]
    assert grid_posts == [{"customer_preferred_export_rule": "battery_ok", "disallow_charge_from_grid_with_solar_installed": True}]
    assert api._last_sent["export_rule"] == "battery_ok" and api._last_sent["grid_charging"] is False
    # Unchanged pair -> deduped, no second POST.
    run_async(api.set_grid_import_export("battery_ok", False))
    assert len([r for r in api.requests_made if r[0] == "POST"]) == 1
    # Cache coherence: a manual single-field setter sees the pair the combined setter left behind.
    assert run_async(api.set_export_rule("battery_ok")) is True  # matches cached export_rule -> deduped
    assert len([r for r in api.requests_made if r[0] == "POST"]) == 1
    # Changing only grid charging via the combined setter re-POSTs (one call) and updates both caches.
    assert run_async(api.set_grid_import_export("battery_ok", True)) is True
    assert len([r for r in api.requests_made if r[0] == "POST"]) == 2
    assert api._last_sent["grid_charging"] is True


def test_teslemetry_assert_device_state_dedupes_repeat():
    """Asserting an unchanged desired state issues no further REST commands (write-on-change)."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    desired = {"export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}
    run_async(api.assert_device_state(desired))
    first_count = len(api.requests_made)
    run_async(api.assert_device_state(desired))
    assert len(api.requests_made) == first_count


def test_teslemetry_apply_schedule_asserts_immediately():
    """Committing a schedule mid-window asserts the device state without waiting for the next run cycle."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.last_soc = 50
    api.get_minutes_now = lambda: 2 * 60
    api.pending_schedule["charge"] = {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_write", "turn_on"))
    posts = [(req[1], req[2]) for req in api.requests_made if req[0] == "POST"]
    assert ("/api/1/energy_sites/123456/operation", {"default_real_mode": "backup"}) in posts
    assert ("/api/1/energy_sites/123456/backup", {"backup_reserve_percent": 90}) in posts


def test_teslemetry_run_asserts_schedule_each_cycle():
    """A healthy run cycle evaluates the committed schedule and asserts the device state."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    # Deliberately out of sync with the idle-state default schedule (mode/reserve), unlike
    # SITE_INFO_FULL: fetch_site_info's drift-correction (see fetch_site_info) refreshes the
    # dedupe cache from whatever site_info reports, and if that already matched the emulator's
    # desired idle tuple ("self_consumption"/20, as SITE_INFO_FULL happens to), the subsequent
    # emulator assert would be correctly deduped away with no POST at all - a true negative that
    # would make this test pass for the wrong reason (or fail) regardless of whether run() actually
    # wires the emulator in. Using values that differ guarantees the assert has real work to do.
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_energy": 13500, "nameplate_power": 11500, "max_site_meter_power_ac": 11500, "default_real_mode": "backup", "backup_reserve_percent": 5}}
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.get_minutes_now = lambda: 12 * 60
    assert run_async(api.run(seconds=0, first=True)) is True
    posts = [req[1] for req in api.requests_made if req[0] == "POST"]
    assert "/api/1/energy_sites/123456/operation" in posts


def test_teslemetry_run_skips_assert_when_read_only():
    """Read-only mode gates all emulator device writes."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    api._is_read_only = lambda: True
    run_async(api.run(seconds=0, first=True))
    assert [req for req in api.requests_made if req[0] == "POST"] == []


def test_teslemetry_run_skips_assert_without_soc():
    """The emulator never asserts before a live SOC reading exists (no blind mode changes)."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    # live_status has no mock response -> fetch fails -> last_soc stays None
    run_async(api.run(seconds=0, first=True))
    assert [req for req in api.requests_made if req[0] == "POST"] == []


def test_teslemetry_automatic_config_sets_args():
    """automatic_config wires every inverter arg to this component's published entities.

    battery_rate_max/inverter_limit are wired only once fetch_site_info has actually published
    those sensors (see test_teslemetry_automatic_config_skips_unpublished_rate_sensors for the
    absent-fields case), so this test seeds them via SITE_INFO_FULL first.
    """
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    assert run_async(api.fetch_site_info()) is True
    run_async(api.automatic_config())
    assert api.args_set["inverter_type"] == ["TESLA"]
    assert api.args_set["num_inverters"] == 1
    assert api.args_set["inverter_reserve_max"] == 80
    assert api.args_set["soc_percent"] == ["sensor.predbat_teslemetry_soc"]
    assert api.args_set["soc_max"] == ["sensor.predbat_teslemetry_soc_max"]
    assert api.args_set["battery_power"] == ["sensor.predbat_teslemetry_battery_power"]
    assert api.args_set["battery_power_invert"] == [False]
    assert api.args_set["grid_power"] == ["sensor.predbat_teslemetry_grid_power"]
    assert api.args_set["grid_power_invert"] == [True]
    assert api.args_set["load_power"] == ["sensor.predbat_teslemetry_load_power"]
    assert api.args_set["pv_power"] == ["sensor.predbat_teslemetry_solar_power"]
    assert api.args_set["load_today"] == ["sensor.predbat_teslemetry_load_today"]
    assert api.args_set["import_today"] == ["sensor.predbat_teslemetry_import_today"]
    assert api.args_set["export_today"] == ["sensor.predbat_teslemetry_export_today"]
    assert api.args_set["pv_today"] == ["sensor.predbat_teslemetry_solar_today"]
    assert api.args_set["battery_rate_max"] == ["sensor.predbat_teslemetry_battery_rate_max"]
    assert api.args_set["inverter_limit"] == ["sensor.predbat_teslemetry_inverter_limit"]
    assert api.args_set["reserve"] == ["number.predbat_teslemetry_schedule_reserve"]
    assert api.args_set["charge_start_time"] == ["select.predbat_teslemetry_schedule_charge_start_time"]
    assert api.args_set["charge_end_time"] == ["select.predbat_teslemetry_schedule_charge_end_time"]
    assert api.args_set["charge_limit"] == ["number.predbat_teslemetry_schedule_charge_soc"]
    assert api.args_set["scheduled_charge_enable"] == ["switch.predbat_teslemetry_schedule_charge_enable"]
    assert api.args_set["discharge_start_time"] == ["select.predbat_teslemetry_schedule_discharge_start_time"]
    assert api.args_set["discharge_end_time"] == ["select.predbat_teslemetry_schedule_discharge_end_time"]
    assert api.args_set["discharge_target_soc"] == ["number.predbat_teslemetry_schedule_discharge_soc"]
    assert api.args_set["scheduled_discharge_enable"] == ["switch.predbat_teslemetry_schedule_discharge_enable"]
    assert api.args_set["schedule_write_button"] == ["switch.predbat_teslemetry_schedule_write"]


def test_teslemetry_automatic_config_skips_unpublished_rate_sensors():
    """automatic_config must not wire battery_rate_max/inverter_limit args when fetch_site_info
    never published those sensors (site missing nameplate_power/max_site_meter_power_ac) - doing
    so unconditionally would point Predbat at entities that never exist. Other args (e.g. soc_max)
    must still be wired normally."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    # Only nameplate_energy present -> soc_max is published but neither battery_rate_max nor
    # inverter_limit is (see fetch_site_info: both are conditional on nameplate_power/max_site_meter_power_ac).
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_energy": 13500, "default_real_mode": "self_consumption", "backup_reserve_percent": 20}}
    assert run_async(api.fetch_site_info()) is True
    assert "sensor.predbat_teslemetry_battery_rate_max" not in api.entity_states
    assert "sensor.predbat_teslemetry_inverter_limit" not in api.entity_states
    run_async(api.automatic_config())
    assert "battery_rate_max" not in api.args_set
    assert "inverter_limit" not in api.args_set
    assert api.args_set["soc_max"] == ["sensor.predbat_teslemetry_soc_max"]


def test_teslemetry_automatic_config_references_published_entities():
    """Every entity automatic_config references is actually published by the component."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.register_control_entities()
    run_async(api.fetch_live_status())
    run_async(api.fetch_site_info())
    run_async(api.fetch_energy_today())
    run_async(api.automatic_config())
    published = set(api.dashboard_items.keys())
    for arg, value in api.args_set.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and "." in item:
                    assert item in published, "automatic_config references unpublished entity {} (arg {})".format(item, arg)


def test_teslemetry_run_triggers_automatic_config_once_after_site_info():
    """run() calls automatic_config exactly once, only when automatic is enabled and site_info succeeded."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    api.automatic = True
    run_async(api.run(seconds=0, first=True))
    assert api.args_set.get("inverter_type") == ["TESLA"]
    api.args_set.clear()
    run_async(api.run(seconds=120, first=False))
    assert "inverter_type" not in api.args_set

    api_off = MockTeslemetryAPI()
    api_off.register_control_entities()
    command_ok_responses(api_off)
    api_off.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api_off.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api_off.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api_off.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    run_async(api_off.run(seconds=0, first=True))
    assert api_off.args_set == {}


def test_teslemetry_mock_base_get_arg_consults_args():
    """The CLI harness MockBase.get_arg reflects configured args so set_read_only defaults the standalone test to read-only (controls unchanged)."""
    from teslemetry import MockBase

    base = MockBase()
    # Default (no set_read_only configured): the CLI harness sets this itself, but the raw default must be False.
    assert base.get_arg("set_read_only", False) is False
    # Read-only test mode: set_read_only True must be reflected back so _is_read_only() gates control writes.
    base.args["set_read_only"] = True
    assert base.get_arg("set_read_only", False) is True


def test_teslemetry_discover_site_uses_first_and_filters():
    """discover_site reads /api/1/products, ignores non-energy products, applies the site_id filter and picks the first match."""
    products = {"response": [{"vehicle_id": 900}, {"energy_site_id": 111, "resource_type": "battery"}, {"energy_site_id": 222, "resource_type": "battery"}]}

    # No filter: first energy site is used, vehicle ignored.
    api = MockTeslemetryAPI()
    api.site_id = ""
    api.mock_responses["/api/1/products"] = products
    assert run_async(api.discover_site()) is True
    assert api.site_id == "111"

    # Filter selects a specific site (as a string id).
    api = MockTeslemetryAPI()
    api.site_id = ""
    api.site_filter = ["222"]
    api.mock_responses["/api/1/products"] = products
    assert run_async(api.discover_site()) is True
    assert api.site_id == "222"


def test_teslemetry_discover_site_no_match_returns_false():
    """discover_site returns False and leaves site_id empty when the filter matches nothing or the read fails."""
    products = {"response": [{"energy_site_id": 111, "resource_type": "battery"}]}

    api = MockTeslemetryAPI()
    api.site_id = ""
    api.site_filter = ["999"]
    api.mock_responses["/api/1/products"] = products
    assert run_async(api.discover_site()) is False
    assert api.site_id == ""

    # products read fails (no mock response) -> False.
    api = MockTeslemetryAPI()
    api.site_id = ""
    assert run_async(api.discover_site()) is False
    assert api.site_id == ""


def test_teslemetry_run_discovers_site_before_polling():
    """run() resolves the site via /api/1/products when none is configured, then proceeds to poll and report."""
    api = MockTeslemetryAPI()
    api.site_id = ""
    api.site_filter = []
    api.mock_responses["/api/1/products"] = {"response": [{"energy_site_id": 777, "resource_type": "battery"}]}
    api.mock_responses["/api/1/energy_sites/777/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/777/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/777/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/777/tariff_rate"] = TARIFF_RATE_NORMAL
    assert run_async(api.run(0, True)) is True
    assert api.site_id == "777"
    assert any(req[1] == "/api/1/products" for req in api.requests_made)
    assert api.dashboard_items["sensor.predbat_teslemetry_soc"]["state"] == LIVE_STATUS["response"]["percentage_charged"]


def test_teslemetry_summarize_for_log_hides_bulk():
    """The log summariser hides the history time_series and tariff blocks (which are huge) while
    keeping small fields and reporting counts/codes so the endpoint output stays readable."""
    data = {
        "response": {
            "serial_number": "TG123",
            "period": "day",
            "time_series": [{"x": 1}, {"x": 2}, {"x": 3}],
            "SmartBreakerEnergyLogs": [{"a": 1}],
            "tariff_content_v2": {"code": "PREDBAT-NORMAL", "energy_charges": {"lots": "of data"}},
            "tariff_content": {"code": "CUSTOMER-TARIFF", "energy_charges": {"lots": "of data"}},
        }
    }
    summary = TeslemetryAPI._summarize_for_log(data)["response"]
    assert summary["serial_number"] == "TG123"
    assert summary["period"] == "day"
    assert summary["time_series"] == "[3 entries hidden]"
    assert summary["SmartBreakerEnergyLogs"] == "[1 entries hidden]"
    assert summary["tariff_content_v2"] == "[hidden, code=PREDBAT-NORMAL]"
    assert summary["tariff_content"] == "[hidden, code=CUSTOMER-TARIFF]"
    # Small responses (e.g. live_status) pass through unchanged.
    small = {"response": {"percentage_charged": 50}}
    assert TeslemetryAPI._summarize_for_log(small) == small


def test_teslemetry_soc_rounded_to_2dp():
    """The published SoC is rounded to 2 decimal places rather than the raw high-precision device value."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = {"response": {"percentage_charged": 51.58628278012103}}
    run_async(api.fetch_live_status())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc"]["state"] == 51.59


def test_teslemetry_soc_max_estimated_from_battery_count_when_no_capacity_field():
    """When the API exposes no capacity field (observed on PW3), soc_max is estimated from battery_count,
    and a later real value from live_status (total_pack_energy) upgrades the estimate."""
    api = MockTeslemetryAPI()
    # site_info like the real PW3: nameplate_power + battery_count, but no nameplate_energy/total_pack_energy.
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_power": 11500, "battery_count": 1, "default_real_mode": "autonomous", "backup_reserve_percent": 0}}
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5
    assert api.soc_max_real is False

    # A real total_pack_energy from live_status upgrades the estimate.
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = {"response": {"percentage_charged": 50, "total_pack_energy": 14000}}
    run_async(api.fetch_live_status())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 14.0
    assert api.soc_max_real is True


def test_teslemetry_soc_max_estimate_distinguishes_powerwall_1():
    """A Powerwall 1 (low ~3.3 kW inverter) is estimated at 6.4 kWh; a Powerwall 2/3 at 13.5 kWh - told
    apart by per-unit nameplate power, and scaled by battery_count."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_power": 3300, "battery_count": 1, "components": {"gateways": [{"part_name": "Powerwall"}]}}}
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 6.4

    # Two Powerwall 2 units (5 kW each) -> 2 x 13.5 = 27 kWh.
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_power": 10000, "battery_count": 2, "components": {"gateways": [{"part_name": "Powerwall 2"}]}}}
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 27.0


def test_teslemetry_soc_max_derived_from_energy_left():
    """soc_max is derived from energy_left / percentage_charged when total_pack_energy is absent but energy_left is present."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = {"response": {"percentage_charged": 50, "energy_left": 6750}}
    run_async(api.fetch_live_status())
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5


def test_teslemetry_inverter_limit_sentinel_clamped_to_nameplate():
    """An 'unlimited' max_site_meter_power_ac sentinel (e.g. 1e9) is ignored; inverter_limit falls back to nameplate_power."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_power": 11500, "max_site_meter_power_ac": 1000000000}}
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_inverter_limit"]["state"] == 11500


def test_teslemetry_run_auto_config_fires_with_soc_max_from_live_status():
    """automatic_config fires even when site_info lacks nameplate_energy, because soc_max is published
    from live_status total_pack_energy before the (reordered) auto-config gate is evaluated."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    api.automatic = True
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_power": 11500, "default_real_mode": "self_consumption", "backup_reserve_percent": 20, "tariff_content_v2": {"code": "PREDBAT-NORMAL"}}}
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = {"response": {"percentage_charged": 42, "total_pack_energy": 13500}}
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    run_async(api.run(0, True))
    assert api.dashboard_items["sensor.predbat_teslemetry_soc_max"]["state"] == 13.5
    assert api.args_set.get("inverter_type") == ["TESLA"]
    assert api.args_set["soc_max"] == ["sensor.predbat_teslemetry_soc_max"]


def test_teslemetry_cli_harness_signals_failure_on_auth_error():
    """The standalone CLI harness returns False on an auth failure (so main() exits non-zero) and True on a healthy run - a broken connection must not look like a pass."""
    import io
    import contextlib
    import teslemetry

    async def auth_fail_request(self, method, path, json_body=None):
        """Simulate every request 401ing (bad token)."""
        self.api_auth_failed = True
        return None

    async def healthy_request(self, method, path, json_body=None):
        """Simulate a healthy, connected Powerwall for every endpoint."""
        if method == "POST":
            return {"response": {}}
        if "products" in path:
            return {"response": [{"energy_site_id": "site123", "resource_type": "battery"}]}
        if "site_info" in path:
            return {"response": {"nameplate_energy": 13500, "nameplate_power": 11500, "max_site_meter_power_ac": 11500, "default_real_mode": "self_consumption", "backup_reserve_percent": 20}}
        if "live_status" in path:
            return {"response": {"percentage_charged": 50}}
        if "calendar_history" in path:
            return {"response": {"time_series": []}}
        if "tariff_rate" in path:
            return {"response": {"tariff_content_v2": {"code": "PREDBAT-NORMAL"}}}
        return None

    original = teslemetry.TeslemetryAPI._request
    try:
        teslemetry.TeslemetryAPI._request = auth_fail_request
        with contextlib.redirect_stdout(io.StringIO()):
            failed = run_async(teslemetry.test_teslemetry_api("bad-token", "site123"))
        assert failed is False

        teslemetry.TeslemetryAPI._request = healthy_request
        with contextlib.redirect_stdout(io.StringIO()):
            ok = run_async(teslemetry.test_teslemetry_api("good-token", "site123"))
        assert ok is True
    finally:
        teslemetry.TeslemetryAPI._request = original


def test_teslemetry_quantise_flat_single_tier():
    """A flat rate collapses to one tier priced in GBP whole pence, all 48 slots the same."""
    rates = {m: 28.0 for m in range(0, 2880)}  # 28p flat
    prices, today, tomorrow = TeslemetryAPI._quantise_side(rates, 28.0)
    assert prices == {"SUPER_OFF_PEAK": 0.28}
    assert today == ["SUPER_OFF_PEAK"] * 48
    assert tomorrow == ["SUPER_OFF_PEAK"] * 48


def test_teslemetry_quantise_two_distinct_exact():
    """Economy-7 (two distinct rates) uses two tiers, cheapest -> SUPER_OFF_PEAK, exact prices."""
    rates = {}
    for m in range(0, 2880):
        local = m % 1440
        rates[m] = 8.0 if (0 <= local < 300) else 30.0  # cheap 00:00-05:00, else dear
    prices, today, tomorrow = TeslemetryAPI._quantise_side(rates, 30.0)
    assert prices == {"SUPER_OFF_PEAK": 0.08, "OFF_PEAK": 0.30}
    assert today[0] == "SUPER_OFF_PEAK" and today[10] == "OFF_PEAK"  # slot 10 = 05:00


def test_teslemetry_quantise_agile_three_bands_clamped_rounded():
    """A series with >3 distinct prices drives the equal-width BANDING branch (not exact-mapping): the
    [min,max] range splits into 3 bands, each priced at its members' mean, with negatives clamped to 0.

    Six equally-sized price steps (8 of the 48 slots each), one negative, give six distinct post-round
    GBP values {0.00, 0.10, 0.20, 0.30, 0.40, 0.60} -> 6 > 3 -> the else branch runs. Range 0.00..0.60,
    width 0.20, so the bands and their exact means are:
      band 0 [0.00,0.20): {0.00, 0.10} -> mean 0.05  (0.00 is the clamped -5p, proving the clamp)
      band 1 [0.20,0.40): {0.20, 0.30} -> mean 0.25
      band 2 [0.40,0.60]: {0.40, 0.60} -> mean 0.50
    """
    steps_pence = [-5.0, 10.0, 20.0, 30.0, 40.0, 60.0]  # incl. a negative that must clamp to 0
    rates = {}
    for m in range(0, 2880):
        slot = (m % 1440) // 30  # 0..47
        rates[m] = steps_pence[slot // 8]  # 6 equal groups of 8 slots
    prices, today, tomorrow = TeslemetryAPI._quantise_side(rates, 20.0)
    # (a) exactly the 3 real tier names present
    assert set(prices) == {"SUPER_OFF_PEAK", "OFF_PEAK", "PARTIAL_PEAK"}
    # (b) lowest band price is the MEAN of its members mean(0.00, 0.10) = 0.05 (mean-of-bucket, not just membership)
    assert prices["SUPER_OFF_PEAK"] == 0.05
    assert prices["OFF_PEAK"] == 0.25  # mean(0.20, 0.30)
    assert prices["PARTIAL_PEAK"] == 0.50  # mean(0.40, 0.60)
    # (c) strictly increasing across the bands
    assert prices["SUPER_OFF_PEAK"] < prices["OFF_PEAK"] < prices["PARTIAL_PEAK"]
    # (d) every price is whole pence (2dp)
    assert all(p == round(p, 2) for p in prices.values())
    # (e) the negative-priced slot 0 clamped to 0 and landed in the lowest band
    assert today[0] == "SUPER_OFF_PEAK"
    # matched sets: every slot tier is priced, both days sized to 48 slots
    assert set(today) <= set(prices) and set(tomorrow) <= set(prices)
    assert len(today) == 48 and len(tomorrow) == 48


def test_teslemetry_tesla_dow_sunday_zero():
    """Python weekday (Mon=0..Sun=6) maps to Tesla fromDayOfWeek (Sun=0..Sat=6)."""
    assert TeslemetryAPI._tesla_dow(6) == 0  # Sunday
    assert TeslemetryAPI._tesla_dow(0) == 1  # Monday
    assert TeslemetryAPI._tesla_dow(5) == 6  # Saturday


def test_teslemetry_boost_price_floor_wins_on_low_rates():
    """When 2x the highest real band is below the static EXPORT_SELL_RATE floor, the floor wins."""
    boost = TeslemetryAPI._boost_price({"SUPER_OFF_PEAK": 0.05, "OFF_PEAK": 0.10}, {"SUPER_OFF_PEAK": 0.08})
    assert boost == 0.50


def test_teslemetry_side_layout_partitions_every_day():
    """Every day-of-week's intervals tile [0,1440) with no gaps or overlaps."""
    today = ["SUPER_OFF_PEAK"] * 10 + ["OFF_PEAK"] * 38
    tomorrow = ["OFF_PEAK"] * 48
    layout = TeslemetryAPI._side_layout(today, tomorrow, today_dow=2)
    assert set(layout) == set(range(7))
    for day, intervals in layout.items():
        covered = 0
        for frm, to, _tier in sorted(intervals):
            assert frm == covered  # no gap/overlap
            covered = to
        assert covered == 1440
    # today's shape only on dow 2; the rest carry tomorrow's flat shape
    assert layout[2][0] == (0, 300, "SUPER_OFF_PEAK")
    assert layout[3] == [(0, 1440, "OFF_PEAK")]


def test_teslemetry_render_side_matched_sets_and_day_end():
    """Rendered rates name exactly the tiers used in periods; day-end shows 00:00."""
    layout = {day: [(0, 1440, "OFF_PEAK")] for day in range(7)}
    charges, periods = TeslemetryAPI._render_side(layout, {"OFF_PEAK": 0.30, "SUPER_OFF_PEAK": 0.08})
    assert set(charges["AllYear"]["rates"]) == {"OFF_PEAK"}  # SUPER_OFF_PEAK unused -> dropped (matched sets)
    assert set(periods) == {"OFF_PEAK"}
    assert charges["ALL"] == {"rates": {"ALL": 0}}
    sample = periods["OFF_PEAK"]["periods"][0]
    assert (sample["fromHour"], sample["fromMinute"], sample["toHour"], sample["toMinute"]) == (0, 0, 0, 0)


def test_teslemetry_carve_interval_splits_and_partitions():
    """Carving a mid-day window splits the covering interval and still partitions the day."""
    day = [(0, 1440, "OFF_PEAK")]
    out = TeslemetryAPI._carve_interval(day, 1020, 1080, "ON_PEAK")  # 17:00-18:00
    assert out == [(0, 1020, "OFF_PEAK"), (1020, 1080, "ON_PEAK"), (1080, 1440, "OFF_PEAK")]


def test_teslemetry_boost_segments_today_vs_tomorrow():
    """A same-day window ending in the future is today; one already ended is tomorrow; wrap splits."""
    assert TeslemetryAPI._boost_segments((1020, 1080), now_min=600) == [(0, 1020, 1080)]  # 10:00, 17-18 upcoming -> today
    assert TeslemetryAPI._boost_segments((540, 660), now_min=600) == [(0, 540, 660)]  # in progress (09-11 @10:00) -> today
    assert TeslemetryAPI._boost_segments((300, 420), now_min=600) == [(1, 300, 420)]  # 05-07 ended by 10:00 -> tomorrow
    assert TeslemetryAPI._boost_segments((1380, 60), now_min=720) == [(0, 1380, 1440), (1, 0, 60)]  # 23-01 upcoming -> today+tomorrow
    assert TeslemetryAPI._boost_segments((1380, 60), now_min=30) == [(0, 0, 60)]  # 00:30 inside the 23-01 tail -> today head


def test_teslemetry_apply_boost_places_segments_on_offset_days():
    """Each (offset, from, to) segment carves BOOST_TIER onto (today_dow + offset) % 7, both sides."""
    buy = {d: [(0, 1440, "OFF_PEAK")] for d in range(7)}
    sell = {d: [(0, 1440, "SUPER_OFF_PEAK")] for d in range(7)}
    TeslemetryAPI._apply_boost(buy, sell, [(0, 1020, 1080)], today_dow=3)  # today = Tesla dow 3
    assert (1020, 1080, "ON_PEAK") in buy[3]
    assert (1020, 1080, "ON_PEAK") in sell[3]
    assert all(seg[2] != "ON_PEAK" for d in range(7) if d != 3 for seg in buy[d])


def test_teslemetry_apply_boost_wrap_segments_span_two_days():
    """A two-segment wrap carves the tail on today's DOW and the head on tomorrow's DOW."""
    buy = {d: [(0, 1440, "OFF_PEAK")] for d in range(7)}
    sell = {d: [(0, 1440, "OFF_PEAK")] for d in range(7)}
    TeslemetryAPI._apply_boost(buy, sell, [(0, 1380, 1440), (1, 0, 60)], today_dow=6)  # tomorrow = (6+1)%7 = 0
    assert (1380, 1440, "ON_PEAK") in buy[6]  # today
    assert (0, 60, "ON_PEAK") in buy[0]  # tomorrow
    assert (1380, 1440, "ON_PEAK") in sell[6]  # today, sell mirrors buy
    assert (0, 60, "ON_PEAK") in sell[0]  # tomorrow, sell mirrors buy
    assert all(seg[2] != "ON_PEAK" for seg in buy[1])  # an unrelated day untouched


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
    test_teslemetry_run_boots_without_reconcile()
    test_teslemetry_boot_resumes_from_persisted_schedule()
    test_teslemetry_run_exports_with_aligned_tariff_boost()
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
    test_teslemetry_build_tariff_single_code_real_bands()
    test_teslemetry_build_tariff_boost_is_strict_max_on_today_dow()
    test_teslemetry_build_tariff_fallback_flat_when_no_rates()
    test_teslemetry_build_tariff_boost_clamps_above_high_rates()
    test_teslemetry_build_tariff_periods_partition_each_day()
    test_teslemetry_set_tariff_posts_tou_settings()
    test_teslemetry_sync_tariff_dedupes_unchanged()
    test_teslemetry_sync_tariff_pushes_on_window_change()
    test_teslemetry_sync_tariff_read_only_no_push()
    test_teslemetry_site_info_latches_without_nameplate_soc_max_from_live_status()
    test_teslemetry_run_site_info_latches_on_any_response()
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
    test_teslemetry_schedule_persistence_roundtrip()
    test_teslemetry_schedule_load_without_storage_is_safe()
    test_teslemetry_assert_device_state_posts_commands()
    test_teslemetry_set_grid_import_export_single_post_and_coherent()
    test_teslemetry_assert_device_state_dedupes_repeat()
    test_teslemetry_apply_schedule_asserts_immediately()
    test_teslemetry_run_asserts_schedule_each_cycle()
    test_teslemetry_run_skips_assert_when_read_only()
    test_teslemetry_run_skips_assert_without_soc()
    test_teslemetry_automatic_config_sets_args()
    test_teslemetry_automatic_config_references_published_entities()
    test_teslemetry_run_triggers_automatic_config_once_after_site_info()
    test_teslemetry_emulator_failure_does_not_fail_run()
    test_teslemetry_automatic_config_skips_unpublished_rate_sensors()
    test_teslemetry_mock_base_get_arg_consults_args()
    test_teslemetry_cli_harness_signals_failure_on_auth_error()
    test_teslemetry_discover_site_uses_first_and_filters()
    test_teslemetry_discover_site_no_match_returns_false()
    test_teslemetry_run_discovers_site_before_polling()
    test_teslemetry_summarize_for_log_hides_bulk()
    test_teslemetry_soc_rounded_to_2dp()
    test_teslemetry_soc_max_estimated_from_battery_count_when_no_capacity_field()
    test_teslemetry_soc_max_estimate_distinguishes_powerwall_1()
    test_teslemetry_soc_max_derived_from_energy_left()
    test_teslemetry_inverter_limit_sentinel_clamped_to_nameplate()
    test_teslemetry_run_auto_config_fires_with_soc_max_from_live_status()
    test_teslemetry_quantise_flat_single_tier()
    test_teslemetry_quantise_two_distinct_exact()
    test_teslemetry_quantise_agile_three_bands_clamped_rounded()
    test_teslemetry_tesla_dow_sunday_zero()
    test_teslemetry_boost_price_floor_wins_on_low_rates()
    test_teslemetry_side_layout_partitions_every_day()
    test_teslemetry_render_side_matched_sets_and_day_end()
    test_teslemetry_carve_interval_splits_and_partitions()
    test_teslemetry_boost_segments_today_vs_tomorrow()
    test_teslemetry_apply_boost_places_segments_on_offset_days()
    test_teslemetry_apply_boost_wrap_segments_span_two_days()
    print("**** Teslemetry tests passed ****")
    return 0
