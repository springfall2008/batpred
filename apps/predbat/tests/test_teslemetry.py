# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Unit tests for the TeslemetryAPI component (Tesla Powerwall via Teslemetry)."""

from tests.test_infra import run_async
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


def test_teslemetry(my_predbat=None):
    """Run all Teslemetry component tests (registry entry point).

    Args:
        my_predbat: Unused; accepted for compatibility with the TEST_REGISTRY calling convention in unit_test.py.
    """
    test_teslemetry_entity_names()
    test_teslemetry_live_status_publishes_sensors()
    test_teslemetry_site_info_publishes_soc_max()
    test_teslemetry_energy_today_publishes_kwh()
    print("**** Teslemetry tests passed ****")
    return 0
