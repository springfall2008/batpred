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
    SIGENERGY_ACTIVE_MODE_SELF_GRID,
    SIGENERGY_CODE_IN_OTHER_VPP,
    SIGENERGY_CODE_SYSTEM_PENDING_REVIEW,
    SIGENERGY_LOG_REDACT_KEYS,
    SIGENERGY_MODE_MSC,
    SIGENERGY_MODE_NBI,
    SIGENERGY_MODE_VPP,
    SIGENERGY_VPP_RECLAIM_INTERVAL,
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


class FakeStorage:
    """In-memory fake of the Storage component, for _save_cache/_load_cache/load_cached_data tests."""

    def __init__(self):
        """Initialise an empty in-memory store keyed by (module, filename)."""
        self.data = {}

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Store data under (module, filename), mirroring StorageBase.save's signature and return value."""
        self.data[(module, filename)] = data
        return True

    async def load(self, module, filename):
        """Return previously saved data for (module, filename), or None if absent."""
        return self.data.get((module, filename))

    async def age(self, module, filename):
        """Return 0 (fresh) if data exists for (module, filename), else None."""
        return 0 if (module, filename) in self.data else None


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
        # ComponentBase.storage looks at self.base.components, which this mock doesn't set up —
        # override it directly so tests can plug in a FakeStorage via self._mock_storage.
        self._mock_storage = None

    @property
    def storage(self):
        """Override ComponentBase.storage — tests set self._mock_storage directly."""
        return self._mock_storage

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

    def get_arg(self, key, default=None, **kwargs):
        """Return stored arg or default.

        Accepts and ignores the wider ComponentBase.get_arg keyword arguments (indirect,
        combine, attribute, index, domain, can_override, required_unit) so helpers that
        pass them — such as fetch_axle_active — work against this mock.
        """
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

    # Current responses give ratedEnergy in kWh, as documented - live device list of a 5-module
    # system, whose system-list batteryCapacity was 45.2. Read as Ah this came to ~1.3kWh.
    api.systems["sys3"] = {}
    api.devices["sys3"] = [{"deviceType": "Inverter", "attrMap": {"ratedActivePower": 12.0}}] + [
        {"deviceType": "Battery", "attrMap": {"ratedEnergy": 9.04, "ratedChargePower": 4.4, "ratedDischargePower": 4.8}} for _ in range(5)
    ]
    actual_kwh = api._get_battery_capacity_kwh("sys3")
    assert abs(actual_kwh - 45.2) < 0.001, "kWh ratedEnergy summed unconverted, expected 45.2 got {:.4f}".format(actual_kwh)

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
        "batteryPower": 2.0,       # kW discharging (Predbat convention: positive=discharge)
        "pvPower": 3.5,             # kW
        "gridPower": 1.0,           # kW export (Predbat convention: positive=export)
        "loadPower": 4.5,
        "evPower": 0.0,
    }
    api.daily_summary[system_id] = {"dailyPowerGeneration": 12.3}
    api.history_totals[system_id] = {"TO_LOAD": 987.6, "FROM_GRID": 500.5, "TO_GRID": 400.4, "FROM_THIRD_PARTY_INV": 55.5}

    run_async(api.publish_system_entities(system_id))

    soc_key = "sensor.predbat_sigenergy_{}_battery_soc".format(slug)
    battery_key = "sensor.predbat_sigenergy_{}_battery_power".format(slug)
    grid_key = "sensor.predbat_sigenergy_{}_grid_power".format(slug)
    pv_key = "sensor.predbat_sigenergy_{}_pv_power".format(slug)
    today_key = "sensor.predbat_sigenergy_{}_pv_today".format(slug)
    load_lifetime_key = "sensor.predbat_sigenergy_{}_load_lifetime".format(slug)
    grid_import_lifetime_key = "sensor.predbat_sigenergy_{}_grid_import_lifetime".format(slug)
    third_party_pv_lifetime_key = "sensor.predbat_sigenergy_{}_third_party_pv_lifetime".format(slug)

    assert soc_key in api.dashboard_items, "Battery SOC entity published"
    soc_kwh = api.dashboard_items[soc_key]["state"]
    assert abs(soc_kwh - 6.0) < 0.01, "SOC kWh = 60% × 10kWh = 6.0, got {}".format(soc_kwh)

    assert battery_key in api.dashboard_items, "Battery power entity published"
    assert api.dashboard_items[battery_key]["state"] == 2000, "Battery 2kW = 2000W"

    assert grid_key in api.dashboard_items, "Grid power entity published"
    # flow["gridPower"] is already in Predbat convention (positive=export) at ingestion - published as-is
    assert api.dashboard_items[grid_key]["state"] == 1000, "Grid power published unchanged: export 1kW -> 1000W"

    assert pv_key in api.dashboard_items, "PV power entity published"
    assert api.dashboard_items[pv_key]["state"] == 3500, "PV 3.5kW = 3500W"

    assert today_key in api.dashboard_items, "PV today entity published"
    assert abs(api.dashboard_items[today_key]["state"] - 12.3) < 0.01, "PV today correct"

    assert load_lifetime_key in api.dashboard_items, "Load lifetime entity published"
    assert abs(api.dashboard_items[load_lifetime_key]["state"] - 987.6) < 0.01, "Load lifetime correct"
    assert grid_import_lifetime_key in api.dashboard_items, "Grid import lifetime entity published"
    assert abs(api.dashboard_items[grid_import_lifetime_key]["state"] - 500.5) < 0.01, "Grid import lifetime correct"
    assert third_party_pv_lifetime_key in api.dashboard_items, "Third-party PV lifetime entity published"
    assert abs(api.dashboard_items[third_party_pv_lifetime_key]["state"] - 55.5) < 0.01, "Third-party PV lifetime correct"

    return failed


def test_sigenergy_publish_system_entities_operational_mode_fallback(my_predbat):
    """Test operational_mode falls back to the REST-sourced current_mode when MQTT hasn't reported yet.

    self.system_status is only ever populated by _handle_mqtt_period (MQTT). Before the first MQTT
    message arrives (e.g. at startup, or if MQTT can't connect), it stays empty and the operational
    mode sensor must not simply read "Unknown" when fetch_current_mode has already obtained a real
    value via REST.
    """
    failed = False
    api = MockSigenergyAPI()

    system_id = "SIG12345"
    slug = api._system_slug(system_id)
    api.systems[system_id] = {"systemName": "My Site", "batteryCapacity": 10.0, "status": "online"}
    api.devices[system_id] = [{"deviceType": "Inverter", "attrMap": {"ratedActivePower": 5.0}}]
    api.energy_flow[system_id] = {"batterySoc": 60.0, "batteryPower": 2.0, "pvPower": 3.5, "gridPower": 1.0, "loadPower": 4.5, "evPower": 0.0}
    api.daily_summary[system_id] = {"dailyPowerGeneration": 12.3}
    api.history_totals[system_id] = {}
    # No system_status entry — MQTT has not delivered a 'period' message yet.
    api.current_mode[system_id] = SIGENERGY_MODE_VPP

    run_async(api.publish_system_entities(system_id))

    mode_key = "sensor.predbat_sigenergy_{}_operational_mode".format(slug)
    assert mode_key in api.dashboard_items, "Operational mode entity published"
    assert api.dashboard_items[mode_key]["state"] == "VPP", "Falls back to REST current_mode, got {}".format(api.dashboard_items[mode_key]["state"])
    assert api.dashboard_items[mode_key]["attributes"]["mode_id"] == SIGENERGY_MODE_VPP, "mode_id reflects fallback current_mode"

    # Once MQTT reports a value, it takes priority over the REST-sourced current_mode.
    api.system_status[system_id] = {"operationalMode": SIGENERGY_MODE_MSC, "systemStatus": 1}
    run_async(api.publish_system_entities(system_id))
    assert api.dashboard_items[mode_key]["state"] == "Maximum Self-Consumption", "MQTT value takes priority once available, got {}".format(api.dashboard_items[mode_key]["state"])

    return failed


def test_sigenergy_publish_system_entities_operational_mode_invalid_value(my_predbat):
    """Test operational_mode reads 'Unknown' rather than mode 0 when the MQTT value is unparsable.

    _safe_float(value) with no explicit default falls back to 0.0 on a bad value, which maps to a
    real mode (0 == Maximum Self-Consumption) instead of Unknown. Both operationalMode and
    systemStatus must pass an explicit default of -1 so an invalid-but-present value still reads
    as Unknown rather than silently reporting a specific (wrong) mode/status.
    """
    failed = False
    api = MockSigenergyAPI()

    system_id = "SIG12345"
    slug = api._system_slug(system_id)
    api.systems[system_id] = {"systemName": "My Site", "batteryCapacity": 10.0, "status": "online"}
    api.devices[system_id] = [{"deviceType": "Inverter", "attrMap": {"ratedActivePower": 5.0}}]
    api.energy_flow[system_id] = {"batterySoc": 60.0, "batteryPower": 2.0, "pvPower": 3.5, "gridPower": 1.0, "loadPower": 4.5, "evPower": 0.0}
    api.daily_summary[system_id] = {"dailyPowerGeneration": 12.3}
    api.history_totals[system_id] = {}
    # Present but unparsable — must not be treated as mode/status 0.
    api.system_status[system_id] = {"operationalMode": "", "systemStatus": None}

    run_async(api.publish_system_entities(system_id))

    mode_key = "sensor.predbat_sigenergy_{}_operational_mode".format(slug)
    assert api.dashboard_items[mode_key]["state"] == "Unknown", "Unparsable operationalMode must read Unknown, not mode 0, got {}".format(api.dashboard_items[mode_key]["state"])
    assert api.dashboard_items[mode_key]["attributes"]["mode_id"] is None, "mode_id must be None, not 0, got {}".format(api.dashboard_items[mode_key]["attributes"]["mode_id"])
    assert api.dashboard_items[mode_key]["attributes"]["system_status"] == "Unknown", "Unparsable systemStatus must read Unknown, not status 0, got {}".format(
        api.dashboard_items[mode_key]["attributes"]["system_status"]
    )
    assert api.dashboard_items[mode_key]["attributes"]["system_status_id"] is None, "system_status_id must be None, not 0, got {}".format(
        api.dashboard_items[mode_key]["attributes"]["system_status_id"]
    )

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
    assert api.set_args.get("pv_today") == [
        "sensor.predbat_sigenergy_sig001_pv_lifetime",
        "sensor.predbat_sigenergy_sig002_pv_lifetime",
        "sensor.predbat_sigenergy_sig001_third_party_pv_lifetime",
        "sensor.predbat_sigenergy_sig002_third_party_pv_lifetime",
    ], "pv_today wired to native and third-party PV lifetime totals"
    assert api.set_args.get("load_today") == ["sensor.predbat_sigenergy_sig001_load_lifetime", "sensor.predbat_sigenergy_sig002_load_lifetime"], "load_today wired to lifetime totals"
    assert api.set_args.get("import_today") == ["sensor.predbat_sigenergy_sig001_grid_import_lifetime", "sensor.predbat_sigenergy_sig002_grid_import_lifetime"], "import_today wired to lifetime totals"
    assert api.set_args.get("export_today") == ["sensor.predbat_sigenergy_sig001_grid_export_lifetime", "sensor.predbat_sigenergy_sig002_grid_export_lifetime"], "export_today wired to lifetime totals"

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


def test_sigenergy_fetch_history_totals(my_predbat):
    """Test fetch_history_totals parses Sankey node totals from the /v1/history endpoint."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    fake_response = {
        "code": 0,
        "data": {
            "sankeyData": {
                "nodes": [
                    {"id": "FROM_SOLAR", "value": 1234.5},
                    {"id": "FROM_BATTERY", "value": 111.1},
                    {"id": "TO_BATTERY", "value": 222.2},
                    {"id": "TO_EVDC", "value": 0.0},
                    {"id": "FROM_EVDC", "value": 3.3},
                    {"id": "FROM_GRID", "value": 500.5},
                    {"id": "TO_LOAD", "value": 987.6},
                    {"id": "TO_GRID", "value": 400.4},
                ],
                "links": [],
            },
        },
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_history_totals("SIG001"))

    assert ok is True, "fetch_history_totals should return True, got {}".format(ok)
    totals = api.history_totals.get("SIG001", {})
    assert totals.get("TO_LOAD") == 987.6, "TO_LOAD lifetime total captured"
    assert totals.get("FROM_GRID") == 500.5, "FROM_GRID lifetime total captured"
    assert totals.get("TO_GRID") == 400.4, "TO_GRID lifetime total captured"
    assert totals.get("FROM_SOLAR") == 1234.5, "FROM_SOLAR lifetime total captured"

    return failed


def test_sigenergy_fetch_history_totals_empty_data(my_predbat):
    """Test fetch_history_totals handles code=0 with a null/missing data field without crashing.

    _request() returns the _SIGENERGY_OK sentinel (not a dict) in this case — fetch_history_totals
    must not call .get() on it.
    """
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    fake_response = {"code": 0, "msg": "success"}  # no 'data' field

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_history_totals("SIG001"))

    assert ok is False, "fetch_history_totals should return False for empty data, got {}".format(ok)
    assert "SIG001" not in api.history_totals, "history_totals not populated on failure"

    return failed


def test_sigenergy_fetch_history_totals_clamps_dip(my_predbat):
    """Test fetch_history_totals ignores a transient dip in the API's cumulative totals.

    The Sigenergy Cloud API has been observed to briefly report a lower "Lifetime"
    total for roughly the first 30 minutes after local midnight each night before
    correcting itself. These are one-directional cumulative energy counters that
    can only legitimately increase, so a lower reading must be clamped to the last
    known value rather than published (and overwrite it) — otherwise HA's history
    for these sensors would show a false dip every night.
    """
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.history_totals["SIG001"] = {"TO_LOAD": 7028.33, "FROM_GRID": 500.5}

    fake_response = {
        "code": 0,
        "data": {
            "sankeyData": {
                "nodes": [
                    {"id": "TO_LOAD", "value": 7015.29},  # dip below the previous 7028.33
                    {"id": "FROM_GRID", "value": 501.1},  # normal increase, should pass through
                ],
                "links": [],
            },
        },
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_history_totals("SIG001"))

    assert ok is True, "fetch_history_totals should return True, got {}".format(ok)
    totals = api.history_totals.get("SIG001", {})
    assert totals.get("TO_LOAD") == 7028.33, "TO_LOAD dip should be clamped to the previous value, got {}".format(totals.get("TO_LOAD"))
    assert totals.get("FROM_GRID") == 501.1, "FROM_GRID increase should pass through, got {}".format(totals.get("FROM_GRID"))

    return failed


def test_sigenergy_fetch_history_totals_keeps_missing_node(my_predbat):
    """Test fetch_history_totals preserves a node ID that is transiently absent from the response.

    Merging by only iterating new_totals.items() would drop any previously-seen node ID missing
    from the latest response entirely. publish_system_entities() defaults a missing node to 0, so
    that would publish a large false drop for it — exactly what the dip clamp is meant to prevent.
    """
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.history_totals["SIG001"] = {"TO_LOAD": 7028.33, "FROM_GRID": 500.5}

    fake_response = {
        "code": 0,
        "data": {
            "sankeyData": {
                "nodes": [{"id": "TO_LOAD", "value": 7030.1}],  # FROM_GRID is absent from this response
                "links": [],
            },
        },
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_history_totals("SIG001"))

    assert ok is True, "fetch_history_totals should return True, got {}".format(ok)
    totals = api.history_totals.get("SIG001", {})
    assert totals.get("TO_LOAD") == 7030.1, "TO_LOAD present in the response should update normally, got {}".format(totals.get("TO_LOAD"))
    assert totals.get("FROM_GRID") == 500.5, "FROM_GRID missing from the response should keep its last known value, got {}".format(totals.get("FROM_GRID"))

    return failed


def test_sigenergy_save_load_cache_no_storage(my_predbat):
    """Test _save_cache/_load_cache no-op cleanly when no storage component is available."""
    failed = False
    api = MockSigenergyAPI()
    assert api.storage is None, "No storage wired up by default"

    run_async(api._save_cache("history_totals", {"TO_LOAD": 100.0}))  # must not raise
    result = run_async(api._load_cache("history_totals"))
    assert result is None, "_load_cache returns None when there is no storage, got {}".format(result)

    return failed


def test_sigenergy_save_load_cache_round_trip(my_predbat):
    """Test _save_cache/_load_cache round-trip data through the storage component."""
    failed = False
    api = MockSigenergyAPI()
    api._mock_storage = FakeStorage()

    run_async(api._save_cache("history_totals", {"TO_LOAD": 987.6}))
    result = run_async(api._load_cache("history_totals"))
    assert result == {"TO_LOAD": 987.6}, "Round-tripped data should match what was saved, got {}".format(result)

    missing = run_async(api._load_cache("daily_summary"))
    assert missing is None, "Unsaved key should load as None, got {}".format(missing)

    return failed


def test_sigenergy_load_cached_data(my_predbat):
    """Test load_cached_data restores energy_flow/daily_summary/history_totals/onboard_status."""
    failed = False
    api = MockSigenergyAPI()
    api._mock_storage = FakeStorage()

    run_async(api._save_cache("energy_flow", {"SIG001": {"batterySoc": 42.0}}))
    run_async(api._save_cache("daily_summary", {"SIG001": {"dailyPowerGeneration": 5.5}}))
    run_async(api._save_cache("history_totals", {"SIG001": {"TO_LOAD": 987.6}}))
    run_async(api._save_cache("onboard_status", {"SIG001": "active"}))

    run_async(api.load_cached_data())

    assert api.energy_flow == {"SIG001": {"batterySoc": 42.0}}, "energy_flow restored, got {}".format(api.energy_flow)
    assert api.daily_summary == {"SIG001": {"dailyPowerGeneration": 5.5}}, "daily_summary restored, got {}".format(api.daily_summary)
    assert api.history_totals == {"SIG001": {"TO_LOAD": 987.6}}, "history_totals restored, got {}".format(api.history_totals)
    assert api.onboard_status == {"SIG001": "active"}, "onboard_status restored, got {}".format(api.onboard_status)

    return failed


def test_sigenergy_load_cached_data_no_storage_leaves_defaults(my_predbat):
    """Test load_cached_data is a no-op (keeps empty defaults) when there is no storage component."""
    failed = False
    api = MockSigenergyAPI()

    run_async(api.load_cached_data())

    assert api.energy_flow == {}, "energy_flow left at its default when there is no storage"
    assert api.history_totals == {}, "history_totals left at its default when there is no storage"

    return failed


def test_sigenergy_needs_refresh(my_predbat):
    """Test _data_age_minutes/_needs_refresh against fabricated data_age timestamps."""
    failed = False
    api = MockSigenergyAPI()

    assert api._data_age_minutes("history_totals") is None, "No age recorded yet"
    assert api._needs_refresh("history_totals", 5.0) is True, "Missing age always needs a refresh"

    now = datetime.now(api.local_tz)
    api.data_age["history_totals"] = now - timedelta(minutes=1)
    assert api._needs_refresh("history_totals", 5.0) is False, "1 minute old data is within a 5 minute window"

    api.data_age["history_totals"] = now - timedelta(minutes=10)
    assert api._needs_refresh("history_totals", 5.0) is True, "10 minute old data has exceeded a 5 minute window"

    return failed


def test_sigenergy_run_skips_refresh_when_cache_fresh(my_predbat):
    """run() skips REST energy/summary/history polling when the cached data is still fresh.

    Mirrors FoxAPI's age-based refresh gating: right after a restart, if load_cached_data()
    restored data that's still within SIGENERGY_POLL_INTERVAL, run() should not immediately
    re-poll the Sigenergy API — and, since nothing was fetched, must not bump data_age either
    (an unconditional save would falsely renew the freshness window and defeat the gate).
    """
    failed = False
    sid = "SIG001"

    def _make_api():
        api = MockSigenergyAPI()
        api.systems = {sid: {"deviceList": []}}
        api.current_mode = {sid: SIGENERGY_MODE_VPP}
        api.system_id_filter = {sid}
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        api._mqtt_task = task
        api._manage_vpp_registration = AsyncMock(return_value=True)
        api.fetch_inverter_realtime = AsyncMock(return_value=True)
        api.fetch_energy_flow = AsyncMock(return_value=True)
        api.fetch_daily_summary = AsyncMock(return_value=True)
        api.fetch_history_totals = AsyncMock(return_value=True)
        api.publish_system_entities = AsyncMock()
        api.apply_controls = AsyncMock()
        return api

    # Fresh cache (as if just restored moments ago) — REST polling should be skipped entirely,
    # and the pre-existing data_age timestamps must be left untouched.
    api_fresh = _make_api()
    stale_ts = datetime.now(api_fresh.local_tz) - timedelta(minutes=1)
    api_fresh.data_age = {"energy_flow": stale_ts, "daily_summary": stale_ts, "history_totals": stale_ts}

    run_async(api_fresh.run(seconds=300, first=False))

    api_fresh.fetch_inverter_realtime.assert_not_called()
    api_fresh.fetch_energy_flow.assert_not_called()
    api_fresh.fetch_daily_summary.assert_not_called()
    api_fresh.fetch_history_totals.assert_not_called()
    assert api_fresh.data_age["energy_flow"] == stale_ts, "energy_flow age must not be bumped when nothing was fetched"
    assert api_fresh.data_age["daily_summary"] == stale_ts, "daily_summary age must not be bumped when nothing was fetched"
    assert api_fresh.data_age["history_totals"] == stale_ts, "history_totals age must not be bumped when nothing was fetched"

    # No cached age at all (cold start) — everything should be fetched, and data_age advanced.
    api_cold = _make_api()

    run_async(api_cold.run(seconds=300, first=False))

    api_cold.fetch_energy_flow.assert_called_once_with(sid)
    api_cold.fetch_inverter_realtime.assert_not_called()
    api_cold.fetch_daily_summary.assert_called_once_with(sid)
    api_cold.fetch_history_totals.assert_called_once_with(sid)
    assert "energy_flow" in api_cold.data_age, "energy_flow age recorded after a real fetch"
    assert "daily_summary" in api_cold.data_age, "daily_summary age recorded after a real fetch"
    assert "history_totals" in api_cold.data_age, "history_totals age recorded after a real fetch"

    return failed


def test_sigenergy_run_energy_flow_not_bumped_on_fetch_failure(my_predbat):
    """run() must not bump energy_flow's data_age when both realtime fetches fail.

    If fetch_energy_flow and its fetch_inverter_realtime fallback both fail, self.energy_flow is
    unchanged — treating that as "updated" would falsely renew the refresh_energy freshness
    window and could mask a persistently broken realtime poll.
    """
    failed = False
    sid = "SIG001"

    api = MockSigenergyAPI()
    api.systems = {sid: {"deviceList": []}}
    api.current_mode = {sid: SIGENERGY_MODE_VPP}
    api.system_id_filter = {sid}
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    api._mqtt_task = task
    api._manage_vpp_registration = AsyncMock(return_value=True)
    api.fetch_inverter_realtime = AsyncMock(return_value=False)
    api.fetch_energy_flow = AsyncMock(return_value=False)
    api.fetch_daily_summary = AsyncMock(return_value=True)
    api.fetch_history_totals = AsyncMock(return_value=True)
    api.publish_system_entities = AsyncMock()
    api.apply_controls = AsyncMock()

    run_async(api.run(seconds=300, first=False))

    api.fetch_inverter_realtime.assert_called_once_with(sid)
    api.fetch_energy_flow.assert_called_once_with(sid)
    assert "energy_flow" not in api.data_age, "energy_flow age must not be recorded when both fetches fail"

    return failed


def test_sigenergy_history_totals_clamp_survives_restart(my_predbat):
    """Test the monotonic dip-clamp in fetch_history_totals uses the persisted baseline after a restart.

    Simulates a Predbat restart landing inside the API's nightly dip window (see
    fetch_history_totals): a fresh SigenergyAPI instance with an empty in-memory
    history_totals would otherwise accept the dip as if it were the first-ever reading.
    load_cached_data() must restore the pre-restart baseline before that first fetch runs.
    """
    failed = False

    # "Before restart": persist a known-good baseline to a storage backend that survives the restart.
    shared_storage = FakeStorage()
    run_async(shared_storage.save("sigenergy", "history_totals", {"SIG001": {"TO_LOAD": 7028.33}}))

    # "After restart": a brand new instance, wired to the same persisted storage.
    api = MockSigenergyAPI()
    api._mock_storage = shared_storage
    run_async(api.load_cached_data())
    assert api.history_totals.get("SIG001", {}).get("TO_LOAD") == 7028.33, "Baseline restored before any fetch"

    fake_response = {
        "code": 0,
        "data": {"sankeyData": {"nodes": [{"id": "TO_LOAD", "value": 7015.29}], "links": []}},  # the nightly dip
    }
    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        run_async(api.fetch_history_totals("SIG001"))

    assert api.history_totals["SIG001"]["TO_LOAD"] == 7028.33, "Dip clamped to the restored pre-restart baseline, got {}".format(api.history_totals["SIG001"]["TO_LOAD"])

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
    assert bat_cmds[0][4] == 3.0, "configured export rate (3000W) sent as charging_power_kw, got {}".format(bat_cmds[0][4])

    return failed


def test_sigenergy_apply_controls_freeze_export_no_window(my_predbat):
    """Test apply_controls sends Self-Consumption Grid for a freeze export left by execute.py."""
    failed = False

    def run_case(charge_rate, export_rate):
        """Apply controls with the given rates and no active window, returning the battery commands."""
        api = MockSigenergyAPI()
        system_id = "SIG001"
        api.systems[system_id] = {"systemName": "Home", "batteryCapacity": 10.0}
        api.devices[system_id] = [{"deviceType": "Battery", "attrMap": {"ratedChargePower": 3.0}}]
        api.energy_flow[system_id] = {"batterySoc": 80.0}
        # This is the control state execute.py actually leaves behind for a Freeze Export on
        # SIGCLOUD: adjust_force_export(False) has already cleared the export window, so the
        # only remaining signal is adjust_charge_rate(0) against a reset export rate.
        api.controls[system_id] = {
            "charge": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 100, "rate": charge_rate},
            "export": {"enable": False, "start_time": "00:00:00", "end_time": "00:00:00", "target_soc": 0, "rate": export_rate},
            "reserve": 10,
        }

        commands_sent = []

        async def mock_send_battery_command(sid, active_mode, duration_min, charging_power_kw=None, **kwargs):
            commands_sent.append(("battery_cmd", sid, active_mode, duration_min, charging_power_kw))
            return True

        api.send_battery_command = mock_send_battery_command

        ok = run_async(api.apply_controls(system_id))
        assert ok is True, "apply_controls freeze export returned True"
        return [c for c in commands_sent if c[0] == "battery_cmd"]

    # Charge rate pinned to zero with the export rate still live — a freeze export
    bat_cmds = run_case(0, 3000)
    assert len(bat_cmds) >= 1, "send_battery_command called for freeze export"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_SELF_GRID, "selfConsumption-grid sent for freeze export, got {}".format(bat_cmds[0][2])
    # chargingPower is bidirectional on this API, so a freeze export must not cap it — that
    # would also stop the battery serving the house, which is a freeze charge not a freeze export
    assert bat_cmds[0][4] is None, "no charging power cap sent for freeze export, got {}".format(bat_cmds[0][4])

    # Both rates zero — a freeze export with a car-charging or iBoost discharge hold on top.
    # selfConsumption-grid would let the battery discharge to serve the house, which the hold
    # forbids, so this is plain self-consumption with the bidirectional power pinned to zero
    bat_cmds = run_case(0, 0)
    assert len(bat_cmds) >= 1, "send_battery_command called for freeze export with a discharge hold"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_SELF, "selfConsumption sent for freeze export with a discharge hold, got {}".format(bat_cmds[0][2])
    assert bat_cmds[0][4] == 0, "charging power pinned to 0 for freeze export with a discharge hold, got {}".format(bat_cmds[0][4])

    # A discharge hold on its own, with charging still allowed, is not a freeze and must not be
    # capped — the bidirectional power cannot express "charge allowed, discharge blocked"
    bat_cmds = run_case(3000, 0)
    assert len(bat_cmds) >= 1, "send_battery_command called for a discharge hold alone"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_SELF, "selfConsumption sent for a discharge hold alone, got {}".format(bat_cmds[0][2])
    assert bat_cmds[0][4] is None, "no charging power cap sent for a discharge hold alone, got {}".format(bat_cmds[0][4])

    # A normal demand period with both rates live stays in demand
    bat_cmds = run_case(3000, 3000)
    assert len(bat_cmds) >= 1, "send_battery_command called for demand"
    assert bat_cmds[0][2] == SIGENERGY_ACTIVE_MODE_SELF, "selfConsumption sent for demand, got {}".format(bat_cmds[0][2])

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


def test_sigenergy_redact(my_predbat):
    """Test redact masks credential keys at any depth and leaves the rest alone."""
    failed = False

    redacted = SigenergyAPI.redact({"accessToken": "live-token", "systemId": "SIG1"})
    assert redacted["accessToken"] == "<redacted>", "accessToken masked"
    assert redacted["systemId"] == "SIG1", "Non-credential key untouched"

    # Nested inside a list, as the battery command payload nests its commands
    nested = SigenergyAPI.redact({"commands": [{"systemId": "SIG1", "password": "hunter2"}]})
    assert nested["commands"][0]["password"] == "<redacted>", "Credential masked inside a nested list"
    assert nested["commands"][0]["systemId"] == "SIG1", "Nested non-credential key untouched"

    # Nested inside a dict, exercising the dict-value recursion branch
    nested_dict = SigenergyAPI.redact({"outer": {"accessToken": "live-token", "systemId": "SIG1"}})
    assert nested_dict["outer"]["accessToken"] == "<redacted>", "Credential masked inside a nested dict"
    assert nested_dict["outer"]["systemId"] == "SIG1", "Nested-dict non-credential key untouched"

    # Every documented credential key is covered — iterate the constant so keys added
    # to SIGENERGY_LOG_REDACT_KEYS later are automatically tested too
    for key in SIGENERGY_LOG_REDACT_KEYS:
        assert SigenergyAPI.redact({key: "secret"})[key] == "<redacted>", "Key {} masked".format(key)

    # json.dumps() serialises tuples as arrays, so redaction must cover them or a
    # tuple-shaped payload would be published as JSON yet logged unmasked
    nested_tuple = SigenergyAPI.redact({"commands": ({"password": "hunter2"},)})
    assert nested_tuple["commands"][0]["password"] == "<redacted>", "Credential masked inside a nested tuple"

    # Scalars and lists of scalars pass straight through
    assert SigenergyAPI.redact("plain") == "plain", "String passthrough"
    assert SigenergyAPI.redact([1, 2]) == [1, 2], "List passthrough"
    assert SigenergyAPI.redact(None) is None, "None passthrough"

    return failed


def test_sigenergy_publish_mqtt_redacts_token(my_predbat):
    """Test _publish_mqtt keeps the live token on the wire but masks it in the log (#4920)."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "tok123"
    api.mqtt_host = "openapi-eu.sigencloud.com" # cspell:disable-line
    api.mqtt_port = 8883

    mock_client = _make_mock_aiomqtt_client()
    # The nested command carries a credential too, mirroring the shape of the #4920 leak:
    # a top-level-only redaction would mask accessToken but still leak the nested password
    payload = {"accessToken": "live-secret-token", "commands": [{"systemId": "SIG1", "activeMode": "charge", "password": "nested-secret"}]}

    with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
        with patch("sigenergy.aiomqtt.Client", return_value=mock_client):
            ok = run_async(SigenergyAPI._publish_mqtt(api, "openapi/instruction/command", payload))

    assert ok is True, "_publish_mqtt should return True on success"

    # The broker still receives the real token — redaction is log-only
    topic, wire_payload = mock_client.publishes[0]
    import json

    assert topic == "openapi/instruction/command", "Published to the command topic"
    assert json.loads(wire_payload)["accessToken"] == "live-secret-token", "Real token still published to the broker"
    assert json.loads(wire_payload)["commands"][0]["password"] == "nested-secret", "Nested credential still published to the broker"

    published_logs = [m for m in api.log_messages if "MQTT published" in m]
    assert len(published_logs) == 1, "Exactly one publish log line expected"
    assert "live-secret-token" not in published_logs[0], "Token must not appear in the log"
    assert "nested-secret" not in published_logs[0], "Nested credential must not appear in the log"
    assert "<redacted>" in published_logs[0], "Token replaced with the redaction marker"
    assert "SIG1" in published_logs[0], "Non-credential payload content still logged"

    # The caller's payload dict is not mutated by redaction
    assert payload["accessToken"] == "live-secret-token", "Caller payload left unmodified"

    return failed


def test_sigenergy_request_log_redacts_credentials(my_predbat):
    """Test _request masks credential-bearing keys in its request and response log lines."""
    failed = False
    api = MockSigenergyAPI()
    api.get_access_token = AsyncMock(return_value="tok123")

    fake_response = {"code": 0, "msg": "ok", "data": {"accessToken": "resp-token", "systemId": "SIG1"}}

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.SIGENERGY_MIN_REQUEST_INTERVAL", 0):
        with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
            result = run_async(SigenergyAPI._request(api, "POST", "/openapi/test", params={"token": "query-secret"}, json_data={"password": "hunter2", "systemId": "SIG1"}))

    assert result == {"accessToken": "resp-token", "systemId": "SIG1"}, "Response data returned unchanged"

    request_logs = [m for m in api.log_messages if "Requesting" in m]
    assert len(request_logs) == 1, "Exactly one request log line expected"
    assert "query-secret" not in request_logs[0], "params credential must not appear in the request log"
    assert "hunter2" not in request_logs[0], "json_data credential must not appear in the request log"
    assert "SIG1" in request_logs[0], "Non-credential request content still logged"

    response_logs = [m for m in api.log_messages if "Response from" in m]
    assert len(response_logs) == 1, "Exactly one response log line expected"
    assert "resp-token" not in response_logs[0], "Response credential must not appear in the response log"
    assert "SIG1" in response_logs[0], "Non-credential response content still logged"

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
    assert abs(cmd["chargingPower"] - 3.5) < 0.01, "chargingPower in commands[0]"
    assert set(payload.keys()) == {"accessToken", "commands"}, "nothing but accessToken and commands at the top level, got {}".format(sorted(payload.keys()))
    assert set(cmd.keys()) == {"systemId", "activeMode", "startTime", "duration", "chargingPower"}, "optional fields that were not passed are omitted, got {}".format(sorted(cmd.keys()))

    # Every optional field belongs inside the command object - the API ignores them at the top level (GH#5376)
    published.clear()
    ok = run_async(
        api.send_battery_command(
            "SIG002",
            "discharge",
            30,
            charging_power_kw=1.234,
            pv_power_kw=1.8,
            max_sell_power_kw=4.0,
            max_purchase_power_kw=6.5,
            charge_priority_type="GRID",
            discharge_priority_type="PV",
        )
    )
    assert ok is True, "send_battery_command with every optional field should return True"
    assert len(published) == 1, "One MQTT publish expected for the second command"
    topic, payload = published[0]
    assert set(payload.keys()) == {"accessToken", "commands"}, "nothing but accessToken and commands at the top level, got {}".format(sorted(payload.keys()))
    assert len(payload["commands"]) == 1, "One command expected"
    cmd = payload["commands"][0]
    expected = {"chargingPower": 1.23, "pvPower": 1.8, "maxSellPower": 4.0, "maxPurchasePower": 6.5, "chargePriorityType": "GRID", "dischargePriorityType": "PV"}
    for key, value in expected.items():
        assert cmd.get(key) == value, "{} in commands[0] should be {}, got {}".format(key, value, cmd.get(key))

    # A zero power limit is a real instruction (hold the battery), so it must be sent rather than dropped
    published.clear()
    ok = run_async(api.send_battery_command("SIG003", "selfConsumption", 30, charging_power_kw=0.0))
    assert ok is True, "send_battery_command with a zero power limit should return True"
    cmd = published[0][1]["commands"][0]
    assert cmd.get("chargingPower") == 0.0, "chargingPower 0 in commands[0], got {}".format(cmd.get("chargingPower"))

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
    # storageChargeDischargePowerW -2927 W (negative=discharging), negated to positive=discharging (Predbat convention)
    assert abs(flow["batteryPower"] - 2.927) < 0.01, "batteryPower = 2.927 kW (discharging)"
    assert abs(flow["pvPower"] - 0.0) < 0.001, "pvPower = 0.0"
    # gridActivePowerW is positive=import, negated to positive=export (Predbat convention)
    assert abs(flow["gridPower"] - (-0.003)) < 0.001, "gridPower = -0.003 kW (3W import negated to export convention)"
    # loadPower = pv + bat_discharge - grid_export = 0 + 2.927 - (-0.003) = 2.930
    assert abs(flow["loadPower"] - 2.930) < 0.01, "loadPower derived = 2.930 kW"
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


def test_sigenergy_handle_mqtt_period_partial_update(my_predbat):
    """Test _handle_mqtt_period merges partial (delta) messages onto previous values.

    The broker only includes fields that changed since the last message. A
    second message that omits e.g. 'PV power' must not reset pvPower to 0 -
    it should keep reading back the last known value.
    """
    failed = False
    api = MockSigenergyAPI()

    full_message = {
        "storageSOC%": "79.7",
        "storageChargeDischargePowerW": "-2927.0",
        "PV power": "5000.0",
        "gridActivePowerW": "3.0",
        "inverterActivePowerW": "2681.0",
        "storageChargeCapacityWh": "9520.0",
        "storageDischargeCapacityWh": "37410.0",
        "batteryMaxChargePowerW": "22032.0",
        "batteryMaxDischargePowerW": "36051.0",
        "operationalMode": "6.0",
        "systemStatus": "1.0",
    }
    api._handle_mqtt_period("SYS1", full_message)

    # Second message only reports that the battery power changed - PV power,
    # grid power etc. are omitted because they haven't changed.
    partial_message = {"storageChargeDischargePowerW": "-1000.0"}
    api._handle_mqtt_period("SYS1", partial_message)

    flow = api.energy_flow.get("SYS1", {})
    assert abs(flow["batteryPower"] - 1.0) < 0.001, "batteryPower updated from partial message = 1.0 kW (discharging)"
    assert abs(flow["pvPower"] - 5.0) < 0.001, "pvPower retained from previous full message = 5.0 kW, not reset to 0"
    assert abs(flow["gridPower"] - (-0.003)) < 0.001, "gridPower retained from previous full message"
    assert abs(flow["batterySoc"] - 79.7) < 0.01, "batterySoc retained from previous full message"

    status = api.system_status.get("SYS1", {})
    assert status["operationalMode"] == 6.0, "operationalMode retained from previous full message"

    return failed


def test_sigenergy_handle_mqtt_period_never_reported_field(my_predbat):
    """Regression test for #4663: a field the broker has never sent must not zero the REST value.

    The partial-update merge above only helps once a field has been seen at least once. A system
    whose period messages carry just "PV power" leaves storageSOC% absent from the merged state
    forever, and defaulting it to 0 overwrote the SoC the REST poll had already fetched.

    Live effect: the reporter's battery read 30.84kWh/85% from REST, a period message arrived
    carrying only PV, and Predbat immediately replanned against a 0% battery in the middle of an
    export window.
    """
    failed = False
    api = MockSigenergyAPI()

    # REST poll has populated a good, complete picture
    api.energy_flow["SYS1"] = {
        "batterySoc": 85.0,
        "batteryPower": -4.127,
        "pvPower": 4.42,
        "gridPower": -0.27,
        "loadPower": 3.857,
        "evPower": 0.0,
        "inverterPower": 2.5,
    }

    # The only period message this system ever sends carries PV power and nothing else
    api._handle_mqtt_period("SYS1", {"PV power": "4420.0"})

    flow = api.energy_flow.get("SYS1", {})
    if abs(flow["batterySoc"] - 85.0) > 0.01:
        print("ERROR: batterySoc should be retained from the REST poll, got {}".format(flow["batterySoc"]))
        failed = True
    if abs(flow["batteryPower"] - (-4.127)) > 0.001:
        print("ERROR: batteryPower should be retained from the REST poll, got {}".format(flow["batteryPower"]))
        failed = True
    if abs(flow["gridPower"] - (-0.27)) > 0.001:
        print("ERROR: gridPower should be retained from the REST poll, got {}".format(flow["gridPower"]))
        failed = True
    # The field the message did carry must still be applied
    if abs(flow["pvPower"] - 4.42) > 0.001:
        print("ERROR: pvPower should come from the period message, got {}".format(flow["pvPower"]))
        failed = True

    # With nothing previously known either, an absent field is still 0 rather than raising
    api2 = MockSigenergyAPI()
    api2._handle_mqtt_period("SYS2", {"PV power": "1000.0"})
    flow2 = api2.energy_flow.get("SYS2", {})
    if flow2["batterySoc"] != 0.0:
        print("ERROR: batterySoc with no prior value should default to 0, got {}".format(flow2["batterySoc"]))
        failed = True

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
    subscribed_topics = []

    class FakeMQTTClient:
        """Async context manager that yields two messages then exits cleanly."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def subscribe(self, topic):
            subscribed_topics.append(topic)

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
    # storageChargeDischargePowerW +1000W (charging), negated to -1.0 kW (Predbat convention: negative=charging)
    assert abs(flow.get("batteryPower", 0) - (-1.0)) < 0.01, "batteryPower = -1.0 kW (charging)"

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

    # MQTT topic subscriptions are scoped to the managed system, not a "#" wildcard
    # across every system on the app_key.
    assert len(subscribed_topics) == 3, "one topic per message type for the single managed system, got {}".format(subscribed_topics)
    assert all("XRTKQ1773829273" in t for t in subscribed_topics), "topics scoped to the managed system_id: {}".format(subscribed_topics)
    assert not any("#" in t for t in subscribed_topics), "no wildcard system_id in subscribed topics: {}".format(subscribed_topics)

    return failed


def test_sigenergy_mqtt_listener_loop_ignores_other_systems(my_predbat):
    """Test _mqtt_listener_loop ignores MQTT messages for systems outside self.systems.

    Subscriptions are scoped to self.systems, but this is a defensive test for
    the belt-and-suspenders filter in case a stray message still arrives for a
    system outside our discovered/filtered set (e.g. excluded by system_id_filter).
    Only api.systems (the discovered/filtered set) should end up populated.
    """
    failed = False
    import json as _json

    api = MockSigenergyAPI()
    api.access_token = "tok"
    api.token_expires_at = 9_999_999_999
    # Only one system is in scope - the account may have others.
    api.systems["ZOO1755782498"] = {"systemName": "In scope"}
    api.api_stop = False

    period_payload_other_system = _json.dumps([{
        "deviceType": "system",
        "systemId": "XRTKQ1773829273",  # not in api.systems - must be ignored
        "value": {
            "storageSOC%": "80.0",
            "storageChargeDischargePowerW": "1000.0",
            "PV power": "2000.0",
            "gridActivePowerW": "500.0",
        },
    }]).encode()

    class FakeMessage:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload
            self.qos = 0
            self.retain = False

    messages_to_deliver = [
        FakeMessage("openapi/period/test_app_key/XRTKQ1773829273", period_payload_other_system),
    ]

    class FakeMQTTClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def subscribe(self, topic):
            pass

        async def publish(self, topic, payload=None, qos=0, **kwargs):
            pass

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
                    self._api.api_stop = True
                    raise StopAsyncIteration

        @property
        def messages(self):
            return FakeMQTTClient._Messages(messages_to_deliver, api)

    with patch("sigenergy.aiomqtt.Client", return_value=FakeMQTTClient()):
        with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
            run_async(api._mqtt_listener_loop())

    assert "XRTKQ1773829273" not in api.energy_flow, "energy_flow not populated for out-of-scope system"
    assert "XRTKQ1773829273" not in api.last_mqtt_update, "last_mqtt_update not set for out-of-scope system"
    other_slug = api._system_slug("XRTKQ1773829273")
    assert "sensor.predbat_sigenergy_{}_battery_soc".format(other_slug) not in api.dashboard_items, "no entities published for out-of-scope system"

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
    # activePower is the inverter's AC output, not the grid - it must not reach gridPower
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
                "activePower": 1.5,  # inverter AC output - ignored
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
    # batPower 3.0 (discharging) already matches Predbat convention (positive=discharge) - no negation
    assert flow.get("batteryPower") == 3.0, "batteryPower = 3.0 (discharging, unchanged)"
    assert flow.get("pvPower") == 5.0, "pvPower = 5.0"
    # No earlier reading to hold, and realtimeInfo has no grid figure - so grid and load stay 0
    assert flow.get("gridPower") == 0.0, "gridPower not taken from activePower, got {}".format(flow.get("gridPower"))
    assert flow.get("loadPower") == 0.0, "loadPower not derived without a grid figure, got {}".format(flow.get("loadPower"))
    assert flow.get("evPower") == 0.0, "evPower = 0.0 (not available)"

    # pvEnergyDaily should update daily_summary
    daily = api.daily_summary.get("SYS1", {})
    assert daily.get("dailyPowerGeneration") == 12.5, "daily PV yield updated from pvEnergyDaily"

    return failed


def test_sigenergy_fetch_inverter_realtime_pv_power_key_fallback(my_predbat):
    """Regression test for #4663: realtimeInfo spells PV power "pVPower" (capital V), not the
    natural-looking "pvPower" read by the code before the fix - which silently defaulted PV to 0
    forever. Covers the fallback chain pVPower -> pvTotalPower -> pvPower in priority order."""
    failed = False

    def _fetch_pv(realtime_info):
        api = MockSigenergyAPI()
        api.access_token = "fake_token"
        api.token_expires_at = 9_999_999_999
        api._last_request_time = 0
        api.devices["SYS1"] = [{"deviceType": "Inverter", "serialNumber": "INV001"}]

        fake_response = {
            "code": 0,
            "data": {
                "systemId": "SYS1",
                "serialNumber": "INV001",
                "deviceType": "Inverter",
                "realTimeInfo": realtime_info,
            },
        }
        mock_response = _make_mock_response(status=200, json_data=fake_response)
        mock_session = _make_mock_session(mock_response)
        with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
            ok = run_async(api.fetch_inverter_realtime("SYS1"))
        assert ok is True, "fetch_inverter_realtime should return True"
        return api.energy_flow.get("SYS1", {}).get("pvPower")

    # pVPower is primary - used even when the other two spellings are also present and disagree.
    pv = _fetch_pv({"batSoc": 50.0, "pVPower": 4.66, "pvTotalPower": 4.38, "pvPower": 0.0})
    assert pv == 4.66, "pVPower should be read as the primary PV key, got {}".format(pv)

    # No pVPower - falls back to pvTotalPower.
    pv = _fetch_pv({"batSoc": 50.0, "pvTotalPower": 4.38, "pvPower": 0.0})
    assert pv == 4.38, "should fall back to pvTotalPower when pVPower is absent, got {}".format(pv)

    # Neither pVPower nor pvTotalPower - falls back to pvPower.
    pv = _fetch_pv({"batSoc": 50.0, "pvPower": 2.1})
    assert pv == 2.1, "should fall back to pvPower when the other two are absent, got {}".format(pv)

    # None of the three present - the #4663 regression case: silently stuck at 0, not an error.
    pv = _fetch_pv({"batSoc": 50.0})
    assert pv == 0.0, "should read 0.0, not error, when no PV key is present at all, got {}".format(pv)

    return failed


def test_sigenergy_fetch_energy_flow(my_predbat):
    """Test fetch_energy_flow negates the raw batteryPower to Predbat convention.

    The raw /energyFlow API returns batteryPower positive=charging, but Predbat
    (and every other sigenergy.py ingestion path) expects positive=discharging.
    gridPower/pvPower/loadPower/evPower already match Predbat's convention and
    must pass through unchanged.
    """
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    fake_response = {
        "code": 0,
        "data": {
            "pvPower": 10.1,
            "gridPower": 2.5,
            "evPower": 0.4,
            "loadPower": 3.2,
            "batteryPower": 4.0,  # charging (raw convention) -> should be negated to -4.0
            "batterySoc": 88.0,
        },
    }

    mock_response = _make_mock_response(status=200, json_data=fake_response)
    mock_session = _make_mock_session(mock_response)

    with patch("sigenergy.aiohttp.ClientSession", return_value=mock_session):
        ok = run_async(api.fetch_energy_flow("SYS1"))

    assert ok is True, "fetch_energy_flow should return True"
    flow = api.energy_flow.get("SYS1", {})
    assert flow.get("batterySoc") == 88.0, "batterySoc = 88.0"
    assert flow.get("batteryPower") == -4.0, "batteryPower negated to -4.0 (charging, Predbat convention)"
    assert flow.get("pvPower") == 10.1, "pvPower = 10.1 (unchanged)"
    assert flow.get("gridPower") == 2.5, "gridPower = 2.5 (unchanged, already Predbat convention)"
    assert flow.get("loadPower") == 3.2, "loadPower = 3.2 (unchanged)"
    assert flow.get("evPower") == 0.4, "evPower = 0.4 (unchanged)"

    return failed


def test_sigenergy_run_falls_back_to_realtime_when_energy_flow_fails(my_predbat):
    """run() polls energyFlow first and only calls realtimeInfo when energyFlow fails."""
    failed = False
    sid = "SIG001"

    def _make_api(energy_flow_ok):
        api = MockSigenergyAPI()
        api.systems = {sid: {"deviceList": []}}
        api.current_mode = {sid: SIGENERGY_MODE_VPP}
        api.system_id_filter = {sid}
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        api._mqtt_task = task
        api._manage_vpp_registration = AsyncMock(return_value=True)
        api.fetch_energy_flow = AsyncMock(return_value=energy_flow_ok)
        api.fetch_inverter_realtime = AsyncMock(return_value=True)
        api.fetch_daily_summary = AsyncMock(return_value=True)
        api.fetch_history_totals = AsyncMock(return_value=True)
        api.publish_system_entities = AsyncMock()
        api.apply_controls = AsyncMock()
        return api

    api = _make_api(energy_flow_ok=True)
    run_async(api.run(seconds=300, first=False))
    api.fetch_energy_flow.assert_called_once_with(sid)
    api.fetch_inverter_realtime.assert_not_called()
    assert "energy_flow" in api.data_age, "energy_flow age recorded"

    api = _make_api(energy_flow_ok=False)
    run_async(api.run(seconds=300, first=False))
    api.fetch_energy_flow.assert_called_once_with(sid)
    api.fetch_inverter_realtime.assert_called_once_with(sid)
    assert "energy_flow" in api.data_age, "energy_flow age recorded after the realtime fallback succeeded"
    return failed


def test_sigenergy_fetch_inverter_realtime_live_sample_holds_grid_and_load(my_predbat):
    """realtimeInfo's activePower is inverter output; grid and load hold the last energyFlow values.

    Regression from a live VPP-mode system on 2026-10-20 while MQTT was down: realtimeInfo gave
    activePower 1.19 (== pcsActivePower, == phase V x I 245.72 x 4.89), which was read as 1.19kW
    export and gave a derived load of 0, while energyFlow polled 90s apart reported grid 0 and a
    house load being met by the battery.
    """
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.devices["MPV1"] = [{"deviceType": "Inverter", "serialNumber": "INV001"}]
    # Last energyFlow reading for the system: battery discharging 1.1kW into a 1.1kW load, grid 0
    api.energy_flow["MPV1"] = {"batterySoc": 30.9, "batteryPower": 1.1, "pvPower": 0.0, "gridPower": 0.0, "loadPower": 1.1, "evPower": 0.0}

    fake_response = {
        "code": 0,
        "data": {
            "systemId": "MPV1",
            "serialNumber": "INV001",
            "deviceType": "Inverter",
            "realTimeInfo": {
                "activePower": "1.19",
                "pcsActivePower": "1.19",
                "aPhaseVoltage": "245.72",
                "aPhaseCurrent": "4.89",
                "pVPower": 0.085,
                "pvTotalPower": "0.00",
                "batSoc": "31.10",
                "batPower": -0.638,
                "pvEnergyDaily": "0.05",
            },
        },
    }
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=fake_response))):
        ok = run_async(api.fetch_inverter_realtime("MPV1"))

    assert ok is True, "fetch_inverter_realtime should return True"
    flow = api.energy_flow["MPV1"]
    assert flow["gridPower"] == 0.0, "grid held from energyFlow, not activePower 1.19 - got {}".format(flow["gridPower"])
    assert flow["loadPower"] == 1.1, "load held from energyFlow, not derived as 0 - got {}".format(flow["loadPower"])
    assert abs(flow["batterySoc"] - 31.1) < 0.01, "SOC still updated from realtimeInfo"
    assert abs(flow["pvPower"] - 0.085) < 1e-9, "PV still updated from pVPower"
    return failed


def test_sigenergy_fetch_energy_flow_live_sample_balances(my_predbat):
    """energyFlow from the same live system balances: load = pv + battery discharge + grid import.

    Raw batteryPower -1.1 (raw convention positive=charging) is a 1.1kW discharge, which with no
    PV and no grid exactly meets the reported 1.1kW load - the cross-check that makes energyFlow
    the trustworthy REST source.
    """
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0

    fake_response = {"code": 0, "data": '{"pvPower":0.0,"gridPower":0.0,"evPower":0.0,"acPower":0.0,"loadPower":1.1,"heatPumpPower":0.0,"batteryPower":-1.1,"batterySoc":30.9}'}
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=fake_response))):
        ok = run_async(api.fetch_energy_flow("MPV1"))

    assert ok is True, "fetch_energy_flow should return True"
    flow = api.energy_flow["MPV1"]
    assert flow["batteryPower"] == 1.1, "raw -1.1 is a 1.1kW discharge in Predbat convention, got {}".format(flow["batteryPower"])
    # Predbat convention: grid +export/-import, so import = -gridPower
    assert abs(flow["loadPower"] - (flow["pvPower"] + flow["batteryPower"] - flow["gridPower"])) < 0.01, "energy balance holds: {}".format(flow)
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
    """offboard=True + VPP active waits for observed MSC before offboarding."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_VPP

    modes_set = []

    async def mock_set_mode(system_id, mode_int):
        modes_set.append(mode_int)
        return True

    api.set_operating_mode = mock_set_mode
    api.offboard_systems = AsyncMock(return_value=[])

    result = run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert result is False, "offboard=True should return False"
    assert modes_set == [SIGENERGY_MODE_MSC], "Should leave VPP explicitly rather than assume offboard does it"
    api.offboard_systems.assert_not_awaited()

    # MQTT publish success is not mode confirmation. Only observed telemetry allows the
    # REST offboard to follow, preventing a race between the two transports.
    api.current_mode[sid] = SIGENERGY_MODE_MSC
    result = run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert result is False, "offboard=True should still return False"
    assert modes_set == [SIGENERGY_MODE_MSC], "A confirmed VPP exit should not be repeated"
    api.offboard_systems.assert_awaited_once_with(sid)

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
    api.offboard_systems = AsyncMock(return_value=[])

    result = run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert result is False, "offboard=True should return False"
    assert not modes_set, "No mode switch needed"
    api.offboard_systems.assert_awaited_once_with(sid)

    return failed


def test_sigenergy_offboard_overrides_axle_control(my_predbat):
    """An explicit offboard exits Axle's NBI mode before removing authorisation."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_NBI
    api.args["axle_control"] = True
    api.args["axle_session"] = "binary_sensor.predbat_axle_event"
    api.dashboard_items["binary_sensor.predbat_axle_event"] = {"state": "on"}
    api.set_operating_mode = AsyncMock(return_value=True)
    api.offboard_systems = AsyncMock(return_value=[])

    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))

    assert api.set_operating_mode.await_count == 2, "MSC is retried while telemetry still reports NBI"
    api.set_operating_mode.assert_awaited_with(sid, SIGENERGY_MODE_MSC)
    api.offboard_systems.assert_not_awaited()

    api.current_mode[sid] = SIGENERGY_MODE_MSC
    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    api.offboard_systems.assert_awaited_once_with(sid)

    return failed


def test_sigenergy_offboard_defers_when_the_mode_switch_fails(my_predbat):
    """A failed VPP exit must NOT offboard — that is the lockout this guards against."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.controls[sid] = {"offboard": False}
    api.current_mode[sid] = SIGENERGY_MODE_VPP

    offboarded = []
    mode_attempts = []

    async def mock_set_mode(system_id, mode_int):
        mode_attempts.append(mode_int)
        return False  # broker down / token expired

    async def mock_offboard(system_ids):
        offboarded.append(system_ids)
        return True

    async def mock_publish_controls(system_id=None):
        pass

    api.set_operating_mode = mock_set_mode
    api.offboard_systems = mock_offboard
    api.publish_controls = mock_publish_controls

    run_async(api._update_control("switch.predbat_sigenergy_sig001_offboard", "turn_on", None, "offboard", sid))
    assert not offboarded, "Must not revoke authorisation while the system is still in VPP"

    # Retried by the periodic check. A successful MQTT publish still is not enough.
    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert len(mode_attempts) == 2, "A failed VPP exit must be retried, not latched"
    assert not offboarded, "Still no offboard while the exit keeps failing"

    api.set_operating_mode = _make_ok_set_mode(mode_attempts)
    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert not offboarded, "MQTT acceptance alone must not allow offboarding"

    api.current_mode[sid] = SIGENERGY_MODE_MSC
    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert offboarded, "Offboard proceeds once telemetry confirms the system is out of VPP"

    return failed


def _make_ok_set_mode(recorder):
    """Return a set_operating_mode stub that records and succeeds."""

    async def _ok(system_id, mode_int):
        recorder.append(mode_int)
        return True

    return _ok


def test_sigenergy_offboard_retries_when_the_api_call_fails(my_predbat):
    """A failed offboard is retried rather than reported as done."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_MSC  # already out of VPP

    attempts = []

    async def mock_offboard(system_ids):
        attempts.append(system_ids)
        return None if len(attempts) == 1 else []

    api.offboard_systems = mock_offboard

    done = run_async(api._offboard_system_if_needed(sid))
    assert done is False, "A failed offboard must not be latched as complete"

    done = run_async(api._offboard_system_if_needed(sid))
    assert done is True, "The retry succeeds"
    assert len(attempts) == 2, "Offboard retried exactly once after the failure"

    run_async(api._offboard_system_if_needed(sid))
    assert len(attempts) == 2, "A completed offboard is not repeated"

    return failed


def test_sigenergy_offboard_retries_per_item_failure(my_predbat):
    """A per-system failure payload must not be latched as a successful offboard."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.current_mode[sid] = SIGENERGY_MODE_MSC
    api.offboard_systems = AsyncMock(side_effect=[[{"systemId": sid, "result": False, "codeList": [1200]}], []])

    assert run_async(api._offboard_system_if_needed(sid)) is False
    assert sid not in api._offboard_done, "Per-item failure must leave the completion latch clear"
    assert run_async(api._offboard_system_if_needed(sid)) is True
    assert api.offboard_systems.await_count == 2, "Per-item failure is retried"

    return failed


def test_sigenergy_offboard_unknown_mode_defers(my_predbat):
    """An absent or unrecognised mode is not proof that the owner's app has control."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.offboard_systems = AsyncMock(return_value=[])

    assert run_async(api._offboard_system_if_needed(sid)) is False
    api.offboard_systems.assert_not_awaited()
    assert sid not in api._offboard_vpp_exit_done, "Unknown mode must not latch the VPP exit"

    api.current_mode[sid] = 42
    assert run_async(api._offboard_system_if_needed(sid)) is False
    api.offboard_systems.assert_not_awaited()
    assert sid not in api._offboard_vpp_exit_done, "Unrecognised mode must not latch the VPP exit"

    return failed


def test_sigenergy_offboard_switch_survives_restart(my_predbat):
    """A restart must not re-onboard a system the owner deliberately left.

    The offboard switch is a control entity, so its state is restored on startup like
    every other one — no separate durable latch is needed, and re-onboarding would cost
    the owner a fresh approval email.
    """
    failed = False
    sid = "SIG001"
    api = MockSigenergyAPI()
    api.system_id_filter = {sid}
    api.systems = {}
    slug = api._system_slug(sid)
    api.dashboard_items["switch.predbat_sigenergy_{}_offboard".format(slug)] = {"state": "on"}

    onboarded = []

    async def mock_onboard(system_ids):
        onboarded.append(system_ids)
        return True

    api.onboard_systems = mock_onboard
    # Without a token run() stops at authentication and never reaches the switch check
    api.get_access_token = AsyncMock(return_value="tok")
    # Not authorised: the device list is refused with HTTP 403
    _reject_device_list(api)
    api.fetch_system_list = AsyncMock(return_value=False)

    run_async(api.run(seconds=0, first=True))
    assert api._not_onboarded == {sid}, "403 marks it not onboarded, got {}".format(api._not_onboarded)
    api.fetch_system_list.assert_not_awaited()
    assert not onboarded, "A restart must not re-onboard a system whose offboard switch is on"

    return failed


def test_sigenergy_offboard_toggle_switch_event(my_predbat):
    """Turning on offboard leaves VPP, then waits for telemetry before offboarding."""
    failed = False
    sid = "SIG001"
    api = _make_api_with_system(sid)
    api.controls[sid] = {"offboard": False}
    api.current_mode[sid] = SIGENERGY_MODE_VPP

    offboarded = []
    modes_set = []
    order = []

    async def mock_offboard(system_ids):
        offboarded.append(system_ids)
        order.append("offboard")
        return True

    async def mock_set_mode(system_id, mode_int):
        modes_set.append((system_id, mode_int))
        order.append("mode")
        return True

    async def mock_publish_controls(system_id=None):
        pass

    api.offboard_systems = mock_offboard
    api.set_operating_mode = mock_set_mode
    api.publish_controls = mock_publish_controls

    run_async(api._update_control("switch.predbat_sigenergy_sig001_offboard", "turn_on", None, "offboard", sid))

    assert api.controls[sid]["offboard"] is True, "offboard control should be True after turn_on"
    assert not offboarded, "offboard_systems must wait for telemetry confirmation"
    assert modes_set == [(sid, SIGENERGY_MODE_MSC)], "Should hand control back to the owner's app explicitly"
    assert order == ["mode"], "Only the mode request is sent while telemetry still says VPP"

    api.current_mode[sid] = SIGENERGY_MODE_MSC
    run_async(api._manage_vpp_registration(sid, is_readonly=False, is_offboard=True))
    assert offboarded, "offboard_systems follows once MSC is observed"
    assert order == ["mode", "offboard"], "Must confirm the VPP exit before offboarding"

    # Toggling back off clears the latch so a later offboard exits VPP again.
    api.current_mode[sid] = SIGENERGY_MODE_VPP
    run_async(api._update_control("switch.predbat_sigenergy_sig001_offboard", "turn_off", None, "offboard", sid))
    assert api.controls[sid]["offboard"] is False, "offboard control should be False after turn_off"
    run_async(api._update_control("switch.predbat_sigenergy_sig001_offboard", "turn_on", None, "offboard", sid))
    assert modes_set == [(sid, SIGENERGY_MODE_MSC), (sid, SIGENERGY_MODE_MSC)], "Re-onboard then offboard should exit VPP again"
    assert order == ["mode", "offboard", "mode"], "Second offboard also waits for live confirmation"

    return failed


# ---------------------------------------------------------------------------


def test_sigenergy_onboard_status(my_predbat):
    """onboard_systems records the user-facing onboarding status per result code."""
    failed = False
    api = MockSigenergyAPI()
    assert api.onboard_status == {}, "onboard_status starts empty"

    def _api_error(target, code):
        """A _request stand-in that fails with an API error code, setting it as the real _request does."""
        async def _request(*args, **kwargs):
            target._last_api_code = code
            return None
        return _request

    # Pending approval (1116) → pending_approval, returns None
    api._request = _api_error(api, SIGENERGY_CODE_SYSTEM_PENDING_REVIEW)
    result = run_async(api.onboard_systems(["sys-1"]))
    assert result is None, "pending review returns None"
    assert api.onboard_status["sys-1"] == "pending_approval", "pending_approval status set"

    # Registered to another VPP (1103) → in_other_vpp, returns False
    api2 = MockSigenergyAPI()
    api2._request = _api_error(api2, SIGENERGY_CODE_IN_OTHER_VPP)
    result2 = run_async(api2.onboard_systems("sys-2"))
    assert result2 is False, "in-other-vpp returns False"
    assert api2.onboard_status["sys-2"] == "in_other_vpp", "in_other_vpp status set"

    return failed


def test_sigenergy_publish_onboard_status_sensors(my_predbat):
    """_publish_onboard_status publishes the correct sensor state for each status value."""
    failed = False

    # active — system in VPP mode
    api = MockSigenergyAPI()
    api.system_id_filter = {"SIG001"}
    api.onboard_status = {"SIG001": "active"}
    api.current_mode = {"SIG001": SIGENERGY_MODE_VPP}
    api._publish_onboard_status()
    key = "sensor.predbat_sigenergy_sig001_onboard_status"
    assert api.dashboard_items[key]["state"] == "active", "active state published"
    assert api.dashboard_items[key]["attributes"]["in_vpp"] is True, "in_vpp True for VPP mode"
    assert api.dashboard_items[key]["attributes"]["system_id"] == "SIG001", "system_id in attributes"

    # offboarded — system present but toggled off
    api2 = MockSigenergyAPI()
    api2.system_id_filter = {"SIG002"}
    api2.onboard_status = {"SIG002": "offboarded"}
    api2.current_mode = {"SIG002": SIGENERGY_MODE_MSC}
    api2._publish_onboard_status()
    key2 = "sensor.predbat_sigenergy_sig002_onboard_status"
    assert api2.dashboard_items[key2]["state"] == "offboarded", "offboarded state published"
    assert api2.dashboard_items[key2]["attributes"]["in_vpp"] is False, "in_vpp False for MSC mode"

    # pending_approval — visible system not yet in VPP
    api3 = MockSigenergyAPI()
    api3.system_id_filter = {"SIG003"}
    api3.onboard_status = {"SIG003": "pending_approval"}
    api3.current_mode = {}
    api3._publish_onboard_status()
    key3 = "sensor.predbat_sigenergy_sig003_onboard_status"
    assert api3.dashboard_items[key3]["state"] == "pending_approval", "pending_approval state published"

    # not_onboarded — system in filter but onboard_status not yet set
    api4 = MockSigenergyAPI()
    api4.system_id_filter = {"SIG004"}
    api4.onboard_status = {}
    api4.current_mode = {}
    api4._publish_onboard_status()
    key4 = "sensor.predbat_sigenergy_sig004_onboard_status"
    assert api4.dashboard_items[key4]["state"] == "not_onboarded", "missing status defaults to not_onboarded"

    return failed


def test_sigenergy_run_derives_onboard_status(my_predbat):
    """run() correctly derives and publishes onboard_status for visible systems on each poll cycle."""
    failed = False
    sid = "SIG001"

    def _make_api(mode, offboard_on=False):
        api = MockSigenergyAPI()
        api.systems = {sid: {"deviceList": []}}
        api.current_mode = {sid: mode}
        api.system_id_filter = {sid}
        # Fake a running MQTT task so run() does not try to start one
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        api._mqtt_task = task
        if offboard_on:
            slug = api._system_slug(sid)
            api.dashboard_items["switch.predbat_sigenergy_{}_offboard".format(slug)] = {"state": "on"}
        # Stub async helpers called by run()
        api._manage_vpp_registration = AsyncMock(return_value=True)
        api.fetch_energy_flow = AsyncMock(return_value=True)
        api.fetch_inverter_realtime = AsyncMock(return_value=True)
        api.fetch_daily_summary = AsyncMock()
        api.fetch_history_totals = AsyncMock()
        api.publish_system_entities = AsyncMock()
        api.apply_controls = AsyncMock()
        return api

    # System in VPP mode → active
    api_active = _make_api(SIGENERGY_MODE_VPP)
    ok = run_async(api_active.run(seconds=300, first=False))
    assert ok is True, "run() returns True on success"
    assert api_active.onboard_status[sid] == "active", "active derived from VPP mode"
    sensor_key = "sensor.predbat_sigenergy_sig001_onboard_status"
    assert api_active.dashboard_items[sensor_key]["state"] == "active", "active sensor published"
    assert api_active.dashboard_items[sensor_key]["attributes"]["in_vpp"] is True

    # System in MSC mode, not offboarded → pending_approval
    api_pending = _make_api(SIGENERGY_MODE_MSC)
    run_async(api_pending.run(seconds=300, first=False))
    assert api_pending.onboard_status[sid] == "pending_approval", "pending_approval derived from MSC mode"
    assert api_pending.dashboard_items[sensor_key]["state"] == "pending_approval"

    # Offboard COMPLETED → offboarded regardless of mode.
    api_offboard = _make_api(SIGENERGY_MODE_VPP, offboard_on=True)
    api_offboard._offboard_done.add(sid)
    run_async(api_offboard.run(seconds=300, first=False))
    assert api_offboard.onboard_status[sid] == "offboarded", "offboarded once the offboard has landed"
    assert api_offboard.dashboard_items[sensor_key]["state"] == "offboarded"

    # Toggle on but the offboard has NOT landed yet (mode switch or API call still
    # failing): the status must not claim offboarded, or support reads a lie while
    # the system is still live on the platform.
    api_inflight = _make_api(SIGENERGY_MODE_MSC, offboard_on=True)
    run_async(api_inflight.run(seconds=300, first=False))
    assert api_inflight.onboard_status[sid] == "active", "pending offboard stays active rather than falsely requesting onboarding approval"

    return failed


def test_sigenergy_run_pending_publishes_before_early_exit(my_predbat):
    """When onboard_systems returns None (pending approval), sensor is published before run() returns False."""
    failed = False
    sid = "SIG001"

    api = MockSigenergyAPI()
    api.system_id_filter = {sid}
    api.get_access_token = AsyncMock(return_value="tok")
    _reject_device_list(api)  # HTTP 403 - not authorised yet
    api.onboard_systems = AsyncMock(return_value=None)

    result = run_async(api.run(seconds=0, first=True))

    assert result is False, "run() returns False when onboarding is pending"
    sensor_key = "sensor.predbat_sigenergy_sig001_onboard_status"
    assert sensor_key in api.dashboard_items, "sensor published before early return"
    # Status was set to not_onboarded by the setdefault call in run() before onboard_systems
    assert api.dashboard_items[sensor_key]["state"] in ("not_onboarded", "pending_approval"), "sensor has a known status value"

    return failed


def _make_contended_api(sid, mode, axle_control=False, axle_event="off"):
    """Build a MockSigenergyAPI whose system sits in the given operating mode.

    Args:
        sid: System ID to register.
        mode: Operating mode integer to report as the system's current mode.
        axle_control: Value of the axle_control option.
        axle_event: State of the Axle event binary sensor ("on" or "off").

    Returns:
        A MockSigenergyAPI with run()'s async helpers stubbed out.
    """
    api = MockSigenergyAPI()
    api.systems = {sid: {"deviceList": []}}
    api.current_mode = {sid: mode}
    api.system_id_filter = {sid}
    api.args["axle_control"] = axle_control
    api.args["axle_session"] = "binary_sensor.predbat_axle_event"
    api.dashboard_items["binary_sensor.predbat_axle_event"] = {"state": axle_event}
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    api._mqtt_task = task
    api.set_operating_mode = AsyncMock(return_value=True)
    api.fetch_energy_flow = AsyncMock(return_value=True)
    api.fetch_inverter_realtime = AsyncMock(return_value=True)
    api.fetch_daily_summary = AsyncMock()
    api.fetch_history_totals = AsyncMock()
    api.publish_system_entities = AsyncMock()
    api.apply_controls = AsyncMock()
    return api


def test_sigenergy_reclaims_vpp_from_third_party_controller(my_predbat):
    """Without axle_control, a system taken into NBI is reclaimed into VPP, and says so."""
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI)
    run_async(api._manage_vpp_registration(sid, is_readonly=False))

    api.set_operating_mode.assert_awaited_once_with(sid, SIGENERGY_MODE_VPP)
    assert api.last_contended_by[sid] == "Northbound Integration", "records which controller displaced Predbat"

    reclaim_logs = [m for m in api.log_messages if "reclaiming VPP mode" in m]
    assert len(reclaim_logs) == 1, "reclaim is logged once, got {}".format(api.log_messages)
    assert "Northbound Integration" in reclaim_logs[0], "log names the displacing controller"
    assert "onboard" not in reclaim_logs[0].lower(), "reclaim log must not blame onboarding"

    return failed


def test_sigenergy_contention_does_not_report_pending_approval(my_predbat):
    """Contention must not surface as pending_approval — that shows a false 'approve in app' banner."""
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI)
    api._manage_vpp_registration = AsyncMock(return_value=False)
    run_async(api.run(seconds=300, first=False))
    assert api.onboard_status[sid] == "active", "contended system stays active, not pending_approval"

    api_msc = _make_contended_api(sid, SIGENERGY_MODE_MSC)
    api_msc._manage_vpp_registration = AsyncMock(return_value=False)
    run_async(api_msc.run(seconds=300, first=False))
    assert api_msc.onboard_status[sid] == "pending_approval", "MSC still means pending approval"

    return failed


def test_sigenergy_contention_marker_published_before_recovery(my_predbat):
    """A contention episode shorter than a poll interval must still reach the sensor.

    Regression test: the marker used to be cleared on recovery while the sensor only
    published every 5 minutes, so brief contention was never visible to support.
    """
    failed = False
    sid = "SIG001"
    sensor_key = "sensor.predbat_sigenergy_sig001_onboard_status"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI)
    # Minute tick during contention — the real _manage_vpp_registration records the marker.
    run_async(api.run(seconds=60, first=False))
    assert api.dashboard_items[sensor_key]["attributes"]["last_contended_by"] == "Northbound Integration", "contention published on the minute tick"

    # Mode recovers. The marker must survive so the episode remains visible.
    api.current_mode[sid] = SIGENERGY_MODE_VPP
    run_async(api.run(seconds=120, first=False))
    assert api.dashboard_items[sensor_key]["attributes"]["in_vpp"] is True, "in_vpp reports the live state"
    assert api.dashboard_items[sensor_key]["attributes"]["last_contended_by"] == "Northbound Integration", "marker is not cleared on recovery"

    return failed


def test_sigenergy_reclaim_runs_on_the_minute(my_predbat):
    """The reclaim check runs every minute, so a displaced system is not left for a full poll."""
    failed = False
    sid = "SIG001"

    assert SIGENERGY_VPP_RECLAIM_INTERVAL == 60, "reclaim cadence is one minute"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI)
    api._manage_vpp_registration = AsyncMock(return_value=False)
    run_async(api.run(seconds=60, first=False))
    api._manage_vpp_registration.assert_awaited_once()

    return failed


def test_sigenergy_controls_skipped_message_distinguishes_contention(my_predbat):
    """Skipping controls because another controller holds the system must not blame onboarding."""
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI)
    api._manage_vpp_registration = AsyncMock(return_value=False)
    run_async(api.run(seconds=60, first=False))

    skip_logs = [m for m in api.log_messages if "controls skipped" in m]
    assert skip_logs, "controls-skipped message is logged"
    assert any("held by another controller" in m for m in skip_logs), "message names contention, got {}".format(skip_logs)
    assert not any("onboard is approved" in m for m in skip_logs), "must not tell the user to approve onboarding"
    api.apply_controls.assert_not_awaited()

    return failed


def test_sigenergy_axle_control_off_reclaims_as_before(my_predbat):
    """With axle_control unset, an active Axle event does not change behaviour."""
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI, axle_control=False, axle_event="on")
    assert api._axle_has_control() is False, "axle_control off means Predbat keeps ownership"
    run_async(api._manage_vpp_registration(sid, is_readonly=False))
    api.set_operating_mode.assert_awaited_once_with(sid, SIGENERGY_MODE_VPP)

    return failed


def test_sigenergy_axle_event_leaves_mode_alone(my_predbat):
    """With axle_control on, Predbat must not touch the mode in either direction."""
    failed = False
    sid = "SIG001"

    # Axle has already taken the system into NBI — leave it there.
    api_nbi = _make_contended_api(sid, SIGENERGY_MODE_NBI, axle_control=True, axle_event="on")
    result = run_async(api_nbi._manage_vpp_registration(sid, is_readonly=False))
    assert result is False, "controls do not proceed while Axle owns the inverter"
    api_nbi.set_operating_mode.assert_not_awaited()
    assert any("standing down" in m for m in api_nbi.log_messages), "stand-down is logged, got {}".format(api_nbi.log_messages)

    # Event started but Axle has not switched yet — do NOT drop to MSC, which would hand
    # control to the owner's app rather than to Axle.
    api_vpp = _make_contended_api(sid, SIGENERGY_MODE_VPP, axle_control=True, axle_event="on")
    run_async(api_vpp._manage_vpp_registration(sid, is_readonly=False))
    api_vpp.set_operating_mode.assert_not_awaited()

    return failed


def test_sigenergy_axle_event_skips_controls(my_predbat):
    """Predbat must not issue battery commands while Axle is dispatching."""
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_VPP, axle_control=True, axle_event="on")
    api._manage_vpp_registration = AsyncMock(return_value=False)
    run_async(api.run(seconds=60, first=False))
    api.apply_controls.assert_not_awaited()

    api_off = _make_contended_api(sid, SIGENERGY_MODE_VPP, axle_control=True, axle_event="off")
    api_off._manage_vpp_registration = AsyncMock(return_value=True)
    run_async(api_off.run(seconds=60, first=False))
    api_off.apply_controls.assert_awaited_once_with(sid)

    return failed


def test_sigenergy_axle_event_end_resumes_control(my_predbat):
    """When the event ends Predbat reclaims VPP and says it has resumed."""
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI, axle_control=True, axle_event="on")
    run_async(api._manage_vpp_registration(sid, is_readonly=False))
    api.set_operating_mode.assert_not_awaited()

    # Event ends — the live sensor flips, no prediction cycle required.
    api.dashboard_items["binary_sensor.predbat_axle_event"] = {"state": "off"}
    run_async(api._manage_vpp_registration(sid, is_readonly=False))
    api.set_operating_mode.assert_awaited_once_with(sid, SIGENERGY_MODE_VPP)
    assert any("resuming control" in m for m in api.log_messages), "resume is logged, got {}".format(api.log_messages)

    return failed


def test_sigenergy_axle_standoff_survives_restart(my_predbat):
    """A restart mid-event must not reclaim VPP.

    Regression test: reading the cached set_read_only_axle flag failed here, because
    reset() leaves it False and the component starts before the first update_pred().
    Evaluating axle_control and the event sensor live is what makes first=True safe.
    """
    failed = False
    sid = "SIG001"

    api = _make_contended_api(sid, SIGENERGY_MODE_NBI, axle_control=True, axle_event="on")
    # Simulate the parent having the stale post-reset value the prediction loop has not
    # yet refreshed — the component must not depend on it.
    api.base = MagicMock()
    api.base.set_read_only_axle = False

    assert api._axle_has_control() is True, "live evaluation sees the event despite the stale flag"
    run_async(api._manage_vpp_registration(sid, is_readonly=False))
    api.set_operating_mode.assert_not_awaited()

    return failed

# ---------------------------------------------------------------------------
# MQTT command probe (--command-test)
# ---------------------------------------------------------------------------


def _reason(packet_type, identifier):
    """Build a real paho-mqtt ReasonCode, as aiomqtt hands back from SUBACK/PUBACK."""
    from paho.mqtt.reasoncodes import ReasonCode
    return ReasonCode(packet_type, identifier=identifier)


class FakeProbeClient:
    """Fake aiomqtt.Client for mqtt_command_probe: fires paho's on_publish hook and yields queued messages.

    Args:
        protocol: The ProtocolVersion the probe asked for.
        refuse_connect: When True, entering the context raises MqttError (broker rejected the connection).
        suback: Reason code identifier returned for every subscribe.
        puback: Callable(topic) -> reason code identifier delivered via on_publish, or None to send no ack callback.
        messages: Payload list yielded by client.messages; when None the iterator blocks until cancelled.
    """

    instances = []

    def __init__(self, protocol, refuse_connect=False, suback=1, puback=None, messages=None, paho=True):
        """Record construction so tests can assert on protocol fallback; paho=False hides the paho client."""
        self.protocol = protocol
        self.refuse_connect = refuse_connect
        self.suback = suback
        self.puback = puback or (lambda topic: 0)
        self._messages = messages
        self.subscribed = []
        self.published = []
        self._client = MagicMock() if paho else None
        if paho:
            self._client.on_publish = MagicMock()
        FakeProbeClient.instances.append(self)

    async def __aenter__(self):
        """Connect, or refuse to."""
        if self.refuse_connect:
            import aiomqtt
            raise aiomqtt.MqttError("[code:132] Unsupported protocol version")
        return self

    async def __aexit__(self, *args):
        """Disconnect."""
        return False

    async def subscribe(self, topic, *args, **kwargs):
        """Record the subscription and return its SUBACK reason codes."""
        from paho.mqtt.packettypes import PacketTypes
        self.subscribed.append(topic)
        return [_reason(PacketTypes.SUBACK, self.suback)]

    async def publish(self, topic, payload=None, qos=0, **kwargs):
        """Record the publish and deliver its PUBACK through paho's on_publish, as the network loop would."""
        from paho.mqtt.packettypes import PacketTypes
        self.published.append((topic, payload, qos))
        code = self.puback(topic)
        if code is not None and self._client is not None:
            self._client.on_publish(None, None, len(self.published), _reason(PacketTypes.PUBACK, code), None)

    @property
    def messages(self):
        """Async iterator over the queued messages, or one that never yields."""
        queued = self._messages

        class _Iter:
            def __init__(self):
                self._items = iter(queued or [])

            def __aiter__(self):
                return self

            async def __anext__(self):
                if queued is None:
                    await asyncio.Event().wait()
                try:
                    topic, payload = next(self._items)
                except StopIteration:
                    raise StopAsyncIteration
                msg = MagicMock()
                msg.topic = topic
                msg.payload = payload
                return msg

        return _Iter()


def _run_probe(api, system_ids, factory, **kwargs):
    """Run mqtt_command_probe with aiomqtt.Client replaced by factory(protocol=...)."""
    FakeProbeClient.instances = []

    def _client(*args, **client_kwargs):
        return factory(client_kwargs["protocol"])

    with patch("sigenergy.aiomqtt.Client", side_effect=_client):
        with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
            return run_async(api.mqtt_command_probe(system_ids, **kwargs))


def _probe_api():
    """A MockSigenergyAPI with a valid token, ready to probe."""
    api = MockSigenergyAPI()
    api.access_token = "probe_token"
    api.token_expires_at = 9_999_999_999
    return api


def test_sigenergy_mqtt_ack_result(my_predbat):
    """_mqtt_ack_result treats reason codes >= 0x80 as refusals and a missing code as acknowledged."""
    from paho.mqtt.packettypes import PacketTypes
    from sigenergy import _mqtt_ack_result
    failed = False

    ok, text = _mqtt_ack_result(None)
    assert ok is True and "no reason code" in text, "missing code (MQTT 3.1.1 PUBACK) is an acknowledgement, got {} {}".format(ok, text)
    ok, text = _mqtt_ack_result(_reason(PacketTypes.PUBACK, 0))
    assert ok is True and text == "Success (0x00)", "PUBACK success, got {} {}".format(ok, text)
    ok, text = _mqtt_ack_result(_reason(PacketTypes.PUBACK, 0x87))
    assert ok is False and text == "Not authorized (0x87)", "PUBACK not authorised is a refusal, got {} {}".format(ok, text)
    ok, text = _mqtt_ack_result(_reason(PacketTypes.SUBACK, 1))
    assert ok is True and "Granted QoS 1" in text, "SUBACK granted, got {} {}".format(ok, text)
    ok, text = _mqtt_ack_result(_reason(PacketTypes.SUBACK, 0x80))
    assert ok is False, "MQTT 3.1.1 SUBACK failure (0x80) is a refusal, got {} {}".format(ok, text)
    ok, text = _mqtt_ack_result(0x80)
    assert ok is False and "0x80" in text, "a raw integer code is interpreted too, got {} {}".format(ok, text)
    ok, text = _mqtt_ack_result("garbage")
    assert ok is False, "an unreadable code is not treated as success, got {} {}".format(ok, text)
    return failed


def test_sigenergy_mqtt_command_probe_success(my_predbat):
    """The probe subscribes, requests push data, sends one command per system and records acks and messages."""
    import json
    from sigenergy import command_probe_verdict
    failed = False
    api = _probe_api()
    messages = [("openapi/period/test_app_key/SIG001", b'[{"systemId": "SIG001", "value": {}}]')]

    report = _run_probe(api, ["SIG001", "SIG002"], lambda protocol: FakeProbeClient(protocol, messages=messages), active_mode="idle", duration_minutes=3, charging_power_kw=0.0, listen_seconds=5)

    assert report["connected"] is True and report["error"] is None, "connected cleanly, got {}".format(report)
    assert report["protocol"] == "V5", "MQTT 5 is tried first, got {}".format(report["protocol"])
    assert len(FakeProbeClient.instances) == 1, "no fallback needed"
    client = FakeProbeClient.instances[0]

    assert len(client.subscribed) == 6, "change/period/alarm for each of two systems, got {}".format(client.subscribed)
    assert all(sub["ok"] for sub in report["subscribe"]), "all subscriptions granted"
    assert not any("test_app_key" in sub["topic"] for sub in report["subscribe"]), "app_key masked in report topics: {}".format(report["subscribe"])

    topics = [t for t, _, _ in client.published]
    assert topics[:3] == ["openapi/subscription/period", "openapi/subscription/change", "openapi/subscription/alarm"], "push-data requests first, got {}".format(topics)
    assert topics[3:] == ["openapi/instruction/command", "openapi/instruction/command"], "one command per system, got {}".format(topics)
    assert all(qos == 1 for _, _, qos in client.published), "every publish is QoS 1 so the broker must acknowledge it"
    cmd = json.loads(client.published[3][1])
    assert cmd["accessToken"] == "probe_token", "command carries the token"
    assert cmd["commands"][0]["systemId"] == "SIG001" and cmd["commands"][0]["activeMode"] == "idle", "command for the first system, got {}".format(cmd)
    assert cmd["commands"][0]["duration"] == 3 and cmd["commands"][0]["chargingPower"] == 0.0, "duration and power passed through, got {}".format(cmd)
    assert json.loads(client.published[4][1])["commands"][0]["systemId"] == "SIG002", "second command for the second system"

    assert len(report["publish"]) == 5 and all(pub["ok"] for pub in report["publish"]), "all publishes acknowledged, got {}".format(report["publish"])
    assert report["publish"][3]["reason"] == "Success (0x00)", "PUBACK reason code captured from paho, got {}".format(report["publish"][3])
    assert client._client.on_publish is not None, "on_publish hook installed"

    assert len(report["messages"]) == 1 and "<redacted>" in report["messages"][0]["topic"], "message recorded with app_key masked, got {}".format(report["messages"])
    assert not any("probe_token" in m for m in api.log_messages), "token never logged"

    ok, lines = command_probe_verdict(report)
    assert ok is True, "verdict passes, got {}".format(lines)
    assert any("accepted 2 command(s)" in line for line in lines), "MQTT 5 acceptance reported, got {}".format(lines)
    return failed


def test_sigenergy_mqtt_command_probe_refused_command(my_predbat):
    """A command refused by the broker (MQTT 5 PUBACK 0x87) fails the verdict even though data still flows."""
    from sigenergy import command_probe_verdict
    failed = False
    api = _probe_api()

    def _factory(protocol):
        return FakeProbeClient(protocol, puback=lambda topic: 0x87 if topic == "openapi/instruction/command" else 0, messages=[("openapi/period/test_app_key/SIG001", b"{}")])

    report = _run_probe(api, ["SIG001"], _factory, listen_seconds=5)
    command = [pub for pub in report["publish"] if pub["topic"] == "openapi/instruction/command"]
    assert len(command) == 1 and command[0]["ok"] is False, "command refusal recorded, got {}".format(report["publish"])
    assert "Not authorized" in command[0]["reason"], "reason given, got {}".format(command[0])

    ok, lines = command_probe_verdict(report)
    assert ok is False, "verdict fails on a refused command"
    assert any("refused publish to openapi/instruction/command" in line and "Not authorized" in line for line in lines), "refusal explained, got {}".format(lines)
    assert any("REFUSED" in m for m in api.log_messages), "refusal logged, got {}".format(api.log_messages)
    return failed


def test_sigenergy_mqtt_command_probe_no_subscribers(my_predbat):
    """MQTT 5 PUBACK 0x10 (no matching subscribers) is accepted but flagged, since Sigenergy's backend should be listening."""
    from sigenergy import command_probe_verdict
    failed = False
    api = _probe_api()

    def _factory(protocol):
        return FakeProbeClient(protocol, puback=lambda topic: 0x10, messages=[("openapi/period/test_app_key/SIG001", b"{}")])

    report = _run_probe(api, ["SIG001"], _factory, listen_seconds=5)
    assert all(pub["ok"] and pub["no_subscribers"] for pub in report["publish"]), "0x10 recorded as accepted with no subscribers, got {}".format(report["publish"])
    assert "No matching subscribers (0x10)" in report["publish"][-1]["reason"], "reason given, got {}".format(report["publish"][-1])

    ok, lines = command_probe_verdict(report)
    warning = [line for line in lines if "no subscribers (0x10)" in line]
    assert len(warning) == 1, "one 0x10 warning line, got {}".format(lines)
    assert "openapi/instruction/command" in warning[0] and "openapi/subscription/period" in warning[0], "names the unheard topics, got {}".format(warning)
    assert ok is True, "a warning, not a failure, while data still flows, got {}".format(lines)

    # A normal success code does not trigger the warning
    report = _run_probe(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol, messages=[("t/test_app_key/SIG001", b"{}")]), listen_seconds=5)
    assert not any(pub["no_subscribers"] for pub in report["publish"]), "0x00 is not flagged"
    assert not any("0x10" in line for line in command_probe_verdict(report)[1]), "no warning on 0x00"
    return failed


def test_sigenergy_mqtt_command_probe_v311_fallback(my_predbat):
    """A broker that rejects MQTT 5 is retried on 3.1.1; refused subscriptions and silence fail the verdict."""
    from sigenergy import command_probe_verdict
    failed = False
    api = _probe_api()

    def _factory(protocol):
        return FakeProbeClient(protocol, refuse_connect=(protocol.name == "V5"), suback=0x80, messages=[])

    report = _run_probe(api, ["SIG001"], _factory, listen_seconds=5)
    assert [c.protocol.name for c in FakeProbeClient.instances] == ["V5", "V311"], "V5 then V311, got {}".format([c.protocol for c in FakeProbeClient.instances])
    assert report["protocol"] == "V311" and report["connected"] is True, "connected on 3.1.1, got {}".format(report)
    assert report["error"] is None, "the V5 rejection does not linger as an error once 3.1.1 connects, got {}".format(report["error"])
    assert len(report["subscribe"]) == 3 and not any(sub["ok"] for sub in report["subscribe"]), "SUBACK 0x80 recorded as refused, got {}".format(report["subscribe"])

    ok, lines = command_probe_verdict(report)
    assert ok is False, "verdict fails"
    assert any("refused subscription" in line for line in lines), "subscription refusal explained, got {}".format(lines)
    assert any("MQTT 3.1.1 cannot report a refused publish" in line for line in lines), "3.1.1 caveat on the command ack, got {}".format(lines)
    assert any("No messages received" in line for line in lines), "silence reported, got {}".format(lines)
    return failed


def test_sigenergy_mqtt_command_probe_connect_refused(my_predbat):
    """When every protocol is refused the report carries the broker error and no publish is attempted."""
    from sigenergy import command_probe_verdict
    failed = False
    api = _probe_api()

    report = _run_probe(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol, refuse_connect=True), listen_seconds=5)
    assert report["connected"] is False, "never connected"
    assert "Unsupported protocol version" in report["error"], "broker error kept, got {}".format(report["error"])
    assert report["publish"] == [] and report["subscribe"] == [], "nothing sent"

    ok, lines = command_probe_verdict(report)
    assert ok is False and lines[0].startswith("x Could not connect"), "connect failure explained, got {}".format(lines)
    return failed


def test_sigenergy_mqtt_command_probe_listen_timeout(my_predbat):
    """A broker that pushes nothing ends the listen window on the timeout rather than hanging."""
    failed = False
    api = _probe_api()

    report = _run_probe(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol, messages=None), listen_seconds=0.05)
    assert report["connected"] is True and report["messages"] == [], "listen window closed with no messages, got {}".format(report)
    assert len(report["publish"]) == 4, "push-data requests and the command were still published, got {}".format(report["publish"])
    return failed


def test_sigenergy_mqtt_command_probe_no_token(my_predbat):
    """Without an access token the probe stops before touching the broker."""
    failed = False
    api = MockSigenergyAPI()

    async def _no_token():
        return None

    api.get_access_token = _no_token
    report = _run_probe(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol))
    assert report["connected"] is False and "no access token" in report["error"], "token failure reported, got {}".format(report)
    assert FakeProbeClient.instances == [], "no MQTT connection attempted"
    return failed


def test_sigenergy_command_probe_verdict_no_command(my_predbat):
    """A connected report with no command publish fails rather than passing on data alone."""
    from sigenergy import command_probe_verdict
    failed = False
    report = {"connected": True, "protocol": "V5", "error": None, "subscribe": [], "publish": [], "messages": [{"topic": "t", "payload": "{}"}], "listen_seconds": 5}
    ok, lines = command_probe_verdict(report)
    assert ok is False and any("No command was published" in line for line in lines), "missing command explained, got {}".format(lines)

    report["error"] = "[code:7] The connection was lost."
    report["publish"] = [{"topic": "openapi/instruction/command", "ok": True, "reason": "Success (0x00)"}]
    ok, lines = command_probe_verdict(report)
    assert ok is False and any("Connection dropped" in line for line in lines), "mid-test drop fails the verdict, got {}".format(lines)
    return failed


# ---------------------------------------------------------------------------
# Discovery from the device list (avoids the globally throttled system list)
# ---------------------------------------------------------------------------


def _reject_device_list(api):
    """Make fetch_device_list fail the way an unauthorised system does: HTTP 403 (verified live)."""
    calls = []

    async def _rejected(system_id):
        calls.append(system_id)
        api._last_http_status = 403
        return False

    api.fetch_device_list = _rejected
    return calls


def _device_list_error(api, code):
    """Make fetch_device_list fail with an API error code (None: the request itself failed)."""
    calls = []

    async def _failed(system_id):
        calls.append(system_id)
        api._last_http_status = None
        api._last_api_code = code
        return False

    api.fetch_device_list = _failed
    return calls


def _device_list_ok(api, devices=None):
    """Make fetch_device_list succeed, storing the given inventory."""
    calls = []

    async def _ok(system_id):
        calls.append(system_id)
        api.devices[system_id] = devices if devices is not None else [{"deviceType": "Inverter", "serialNumber": "INV1", "status": "Normal"}]
        return True

    api.fetch_device_list = _ok
    return calls


def _make_discovery_api(sid="SIG001"):
    """MockSigenergyAPI configured for one system and stubbed past discovery."""
    api = MockSigenergyAPI()
    api.system_id_filter = {sid}
    api.get_access_token = AsyncMock(return_value="tok")
    api.fetch_current_mode = AsyncMock(return_value=True)
    api._manage_vpp_registration = AsyncMock(return_value=True)
    api.fetch_controls = AsyncMock()
    api.publish_controls = AsyncMock()
    api.fetch_energy_flow = AsyncMock(return_value=True)
    api.fetch_daily_summary = AsyncMock(return_value=True)
    api.fetch_history_totals = AsyncMock(return_value=True)
    api.publish_system_entities = AsyncMock()
    api.apply_controls = AsyncMock()
    api.onboard_systems = AsyncMock(return_value=True)
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    api._mqtt_task = task
    return api


def test_sigenergy_discover_systems_from_device_list(my_predbat):
    """A configured system is confirmed from its device list; the system list is never read."""
    failed = False
    api = _make_discovery_api()
    calls = _device_list_ok(api)
    api.fetch_system_list = AsyncMock(return_value=True)

    found = run_async(api.discover_systems())
    assert found is True and "SIG001" in api.systems, "system confirmed, got {}".format(api.systems)
    assert calls == ["SIG001"], "one device-list read, got {}".format(calls)
    api.fetch_system_list.assert_not_awaited()
    assert api._unconfirmed_systems == set(), "nothing left unconfirmed"

    # A retried discovery does not spend the system's five-minute device-list allowance again
    run_async(api.discover_systems())
    assert calls == ["SIG001"], "already-confirmed system skipped on retry, got {}".format(calls)

    # No system_id configured: only the system list can enumerate systems
    api = _make_discovery_api()
    api.system_id_filter = set()
    calls = _device_list_ok(api)
    api.fetch_system_list = AsyncMock(return_value=True)
    run_async(api.discover_systems())
    api.fetch_system_list.assert_awaited_once()
    assert calls == [], "device list not used for discovery without a system_id"
    return failed


def test_sigenergy_discover_systems_throttled_does_not_onboard(my_predbat):
    """Anything but a 403 - throttle, failed request, other API error - leaves the system unconfirmed: retry, never onboard."""
    failed = False
    for code in (1201, None, 1001, 1502):
        api = _make_discovery_api()
        _device_list_error(api, code)
        api.fetch_system_list = AsyncMock(return_value=True)

        result = run_async(api.run(seconds=0, first=True))
        assert result is False, "first run fails so discovery retries on the startup backoff (code {})".format(code)
        assert api._unconfirmed_systems == {"SIG001"}, "system unconfirmed (code {}), got {}".format(code, api._unconfirmed_systems)
        assert api._not_onboarded == set(), "not treated as unauthorised (code {})".format(code)
        api.fetch_system_list.assert_not_awaited()
        api.onboard_systems.assert_not_awaited()
        assert any("Could not confirm system" in m for m in api.log_messages), "retry logged (code {})".format(code)
    return failed


def test_sigenergy_discover_systems_403_onboards_directly(my_predbat):
    """A 403 device list goes straight to onboard_systems(), whose result decides; the system list is never read."""
    failed = False

    # Onboarded, and its device list is readable straight after: confirmed in the same run
    api = _make_discovery_api()
    responses = [False, True]
    reads = []

    async def _device_list(system_id):
        reads.append(system_id)
        ok = responses.pop(0)
        if ok:
            api.devices[system_id] = [{"deviceType": "Inverter", "serialNumber": "INV1", "status": "Normal"}]
        else:
            api._last_http_status = 403
        return ok

    api.fetch_device_list = _device_list
    api.fetch_system_list = AsyncMock(return_value=True)
    result = run_async(api.run(seconds=0, first=True))
    api.onboard_systems.assert_awaited_once_with(["SIG001"])
    api.fetch_system_list.assert_not_awaited()
    assert reads == ["SIG001", "SIG001"], "device list re-read after onboarding, got {}".format(reads)
    assert result is True and "SIG001" in api.systems, "confirmed after onboarding, got {} {}".format(result, api.systems)

    # Onboarded, but the device list is not readable yet: retry discovery
    api = _make_discovery_api()
    _reject_device_list(api)
    result = run_async(api.run(seconds=0, first=True))
    api.onboard_systems.assert_awaited_once_with(["SIG001"])
    assert result is False and "SIG001" not in api.systems, "retries discovery, got {} {}".format(result, api.systems)
    assert any("device list not readable yet" in m for m in api.log_messages), "logged"

    # Refused (e.g. 1103, registered to another VPP) or pending approval: stop and retry
    for outcome in (False, None):
        api = _make_discovery_api()
        calls = _reject_device_list(api)
        api.onboard_systems = AsyncMock(return_value=outcome)
        result = run_async(api.run(seconds=0, first=True))
        assert result is False, "run fails while onboarding is {}".format(outcome)
        assert calls == ["SIG001"], "no device-list re-read when onboarding did not succeed, got {}".format(calls)
    return failed


def test_sigenergy_fetch_device_list_empty_is_not_a_confirmation(my_predbat):
    """A device list answered with no devices returns False, keeps the inventory held, and reads as 'answered'."""
    failed = False
    held = [{"deviceType": "Inverter", "serialNumber": "INV1", "status": "Normal"}]
    for label, data in (("null data", None), ("empty list", []), ("empty wrapped list", {"list": []})):
        api = MockSigenergyAPI()
        api.access_token = "fake_token"
        api.token_expires_at = 9_999_999_999
        api._last_request_time = 0
        api.devices["SIG001"] = list(held)
        api._last_api_code = None
        response = {"code": 0, "msg": "success", "data": data}
        with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=response))):
            ok = run_async(api.fetch_device_list("SIG001"))
        assert ok is False, "{}: not a confirmation, got {}".format(label, ok)
        assert api.devices["SIG001"] == held, "{}: inventory already held is kept, got {}".format(label, api.devices["SIG001"])
        assert any("is empty" in m for m in api.log_messages), "{}: logged".format(label)
    return failed


def test_sigenergy_discover_systems_empty_device_list_retries(my_predbat):
    """An empty device list (200, code 0) is neither a confirmation nor a refusal: retry, never onboard."""
    failed = False
    api = _make_discovery_api()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.fetch_system_list = AsyncMock(return_value=False)
    response = {"code": 0, "msg": "success", "data": []}
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=response))):
        result = run_async(api.run(seconds=0, first=True))

    assert result is False and "SIG001" not in api.systems, "not confirmed, got {} {}".format(result, api.systems)
    assert api._unconfirmed_systems == {"SIG001"}, "left unconfirmed, got {}".format(api._unconfirmed_systems)
    api.fetch_system_list.assert_not_awaited()
    api.onboard_systems.assert_not_awaited()
    return failed


def test_sigenergy_request_403_is_not_retried(my_predbat):
    """HTTP 403 is a refusal: one attempt, status recorded. Other HTTP errors are still retried."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    session = _make_mock_session(_make_mock_response(status=403, json_data={}))
    with patch("sigenergy.aiohttp.ClientSession", return_value=session):
        result = run_async(api._request("GET", "/openapi/system/dummy/devices"))
    assert result is None, "403 fails the request"
    assert session.get.call_count == 1, "403 not retried, got {} attempts".format(session.get.call_count)
    assert api._last_http_status == 403, "status recorded, got {}".format(api._last_http_status)

    session = _make_mock_session(_make_mock_response(status=500, json_data={}))
    with patch("sigenergy.aiohttp.ClientSession", return_value=session):
        result = run_async(api._request("GET", "/openapi/system/SIG001/devices", retries=3))
    assert result is None and session.get.call_count == 3, "500 still retried, got {} attempts".format(session.get.call_count)
    assert api._last_http_status == 500, "status recorded, got {}".format(api._last_http_status)

    # A later successful request clears it
    session = _make_mock_session(_make_mock_response(status=200, json_data={"code": 0, "data": [{"deviceType": "Inverter"}]}))
    with patch("sigenergy.aiohttp.ClientSession", return_value=session):
        run_async(api._request("GET", "/openapi/system/SIG001/devices"))
    assert api._last_http_status is None, "cleared by a successful request, got {}".format(api._last_http_status)
    return failed


def test_sigenergy_discover_systems_403_end_to_end(my_predbat):
    """Through the real _request: a 403 device list marks the system not onboarded and run() onboards it."""
    failed = False
    api = _make_discovery_api()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.fetch_system_list = AsyncMock(return_value=True)
    api.onboard_systems = AsyncMock(return_value=False)  # e.g. 1103, registered to another VPP
    session = _make_mock_session(_make_mock_response(status=403, json_data={}))
    with patch("sigenergy.aiohttp.ClientSession", return_value=session):
        result = run_async(api.run(seconds=0, first=True))
    assert api._not_onboarded == {"SIG001"} and api._unconfirmed_systems == set(), "403 is not onboarded, got {} / {}".format(api._not_onboarded, api._unconfirmed_systems)
    assert session.get.call_count == 1, "the 403 is not retried, got {} calls".format(session.get.call_count)
    api.onboard_systems.assert_awaited_once_with(["SIG001"])
    api.fetch_system_list.assert_not_awaited()
    assert result is False, "run fails while the onboard is refused"
    return failed


def test_sigenergy_onboard_systems_ignores_stale_api_code(my_predbat):
    """onboard_systems() must not read a code left by an earlier call as its own outcome."""
    failed = False
    api = MockSigenergyAPI()
    api._last_api_code = 1116  # left over from an earlier call - would read as 'pending approval'

    async def _http_failure(*args, **kwargs):
        api._last_http_status = 503
        return None  # an HTTP failure sets no API code

    api._request = _http_failure
    result = run_async(api.onboard_systems(["SIG001"]))
    assert result is False, "plain failure, not pending approval, got {}".format(result)
    assert api.onboard_status.get("SIG001") != "pending_approval", "status not set from the stale code"

    # The live response for a system in another VPP (2026-10-20): code 0, per-item result False, codeList [1103]
    async def _other_vpp(*args, **kwargs):
        return [{"systemId": "SIG001", "result": False, "codeList": [1103]}]

    api._request = _other_vpp
    assert run_async(api.onboard_systems(["SIG001"])) is False, "refused"
    assert api.onboard_status["SIG001"] == "in_other_vpp", "status from the per-item code, got {}".format(api.onboard_status)
    return failed


def test_sigenergy_run_first_reads_device_list_once(my_predbat):
    """The first run's device refresh skips a system discovery has just read (five-minute restriction)."""
    failed = False
    api = _make_discovery_api()
    calls = _device_list_ok(api)
    api.fetch_system_list = AsyncMock(return_value=True)

    result = run_async(api.run(seconds=0, first=True))
    assert result is True, "first run succeeds"
    assert calls == ["SIG001"], "device list read once on the first run, got {}".format(calls)
    api.fetch_system_list.assert_not_awaited()

    # The periodic refresh still runs afterwards
    run_async(api.run(seconds=1800, first=False))
    assert calls == ["SIG001", "SIG001"], "30-minute refresh reads it again, got {}".format(calls)
    return failed


def test_sigenergy_system_status_from_inverter_device(my_predbat):
    """Status comes from the system list when present, else from the Inverter/AIO device."""
    failed = False
    api = MockSigenergyAPI()
    api.systems["S1"] = {"status": "Disconnection"}
    api.devices["S1"] = [{"deviceType": "Inverter", "status": "Normal"}]
    assert api._system_status("S1") == "Disconnection", "system-list status preferred"

    api.systems["S2"] = {}
    api.devices["S2"] = [{"deviceType": "Battery", "status": "Fault"}, {"deviceType": "AIO", "status": "Normal"}]
    assert api._system_status("S2") == "Normal", "inverter/AIO device status used, not a battery's"

    api.systems["S3"] = {}
    assert api._system_status("S3") == "Unknown", "unknown without either"
    return failed


def test_sigenergy_fetch_system_list_does_not_log_other_systems(my_predbat):
    """The system list carries every system on the app key; neither the body nor unmatched names are logged."""
    import json
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.system_id_filter = {"MINE1"}
    listing = [
        {"systemId": "MINE1", "systemName": "My House", "addr": "1 My Road"},
        {"systemId": "OTHER1", "systemName": "Someone Else", "addr": "2 Their Road"},
    ]
    response = {"code": 0, "msg": "success", "data": json.dumps(listing)}
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=response))):
        ok = run_async(api.fetch_system_list())

    assert ok is True and list(api.systems) == ["MINE1"], "own system matched, got {}".format(api.systems)
    joined = "\n".join(api.log_messages)
    assert "Someone Else" not in joined and "Their Road" not in joined and "OTHER1" not in joined, "other customers never logged:\n{}".format(joined)
    assert "1 My Road" not in joined, "addresses never logged"
    assert "returned 2 system(s), 1 matched" in joined, "count logged instead, got:\n{}".format(joined)

    # An error response is still logged in full - it carries no system data and is needed for diagnosis
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data={"code": 1502, "msg": "internal", "data": {}}))):
        ok = run_async(api.fetch_system_list())
    assert ok is None, "an unreadable list returns None, not False, got {}".format(ok)
    assert any("Response from GET /openapi/system" in m and "1502" in m for m in api.log_messages), "error body logged"
    return failed


def test_sigenergy_rate_limit_last_attempt_does_not_claim_to_wait(my_predbat):
    """On the final 1201 attempt the log says it gave up, not that it is waiting to retry."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    response = {"code": 1201, "msg": "Access restriction", "data": {}}
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=response))):
        result = run_async(api._request("GET", "/openapi/system", retries=2))

    assert result is None, "throttled request fails"
    waits = [m for m in api.log_messages if "waiting" in m]
    assert len(waits) == 1 and "attempt 1/2" in waits[0], "only the first attempt waits, got {}".format(waits)
    assert any("giving up after 2 attempts" in m for m in api.log_messages), "final attempt logs giving up, got {}".format(api.log_messages)
    return failed


# ---------------------------------------------------------------------------
# MQTT topic scan (--mqtt-scan)
# ---------------------------------------------------------------------------


def _props(packet_type, **values):
    """Build real paho-mqtt Properties carrying the given MQTT 5 fields."""
    from paho.mqtt.properties import Properties
    props = Properties(packet_type)
    for key, value in values.items():
        setattr(props, key, value)
    return props


class FakeScanClient:
    """Fake aiomqtt.Client for mqtt_topic_scan: replays paho's connect/subscribe/publish callbacks.

    Args:
        protocol: The ProtocolVersion asked for.
        suback: Callable(filter) -> (reason identifier, Properties or None).
        drop_on: A filter whose subscription drops the connection (raises MqttError).
        messages: List of (topic, payload, retain) delivered once subscribed.
        connack_props: Properties delivered with the CONNACK.
    """

    instances = []

    def __init__(self, protocol, suback=None, drop_on=None, messages=None, connack_props=None, paho=True):
        """Record construction; paho's callbacks are MagicMocks the scan chains in front of (paho=False hides them)."""
        self.protocol = protocol
        self.suback = suback or (lambda topic_filter: (0, None))
        self.drop_on = drop_on
        self._messages = messages or []
        self.connack_props = connack_props
        self.subscribed = []
        self.published = []
        self._client = MagicMock() if paho else None
        if paho:
            self._client.on_connect = MagicMock()
            self._client.on_subscribe = MagicMock()
            self._client.on_publish = MagicMock()
        FakeScanClient.instances.append(self)

    async def __aenter__(self):
        """Connect, delivering the CONNACK through on_connect as paho would."""
        from paho.mqtt.packettypes import PacketTypes
        if self._client is not None:
            self._client.on_connect(None, None, {}, _reason(PacketTypes.CONNACK, 0), self.connack_props)
        return self

    async def __aexit__(self, *args):
        """Disconnect."""
        return False

    async def subscribe(self, topic_filter, *args, **kwargs):
        """Deliver the SUBACK (with properties) through on_subscribe, or drop the connection."""
        import aiomqtt
        from paho.mqtt.packettypes import PacketTypes
        self.subscribed.append(topic_filter)
        if topic_filter == self.drop_on:
            raise aiomqtt.MqttError("[code:7] The connection was lost.")
        identifier, props = self.suback(topic_filter)
        code = _reason(PacketTypes.SUBACK, identifier)
        if self._client is not None:
            self._client.on_subscribe(None, None, len(self.subscribed), [code], props)
        return [code]

    async def publish(self, topic, payload=None, qos=0, **kwargs):
        """Deliver a PUBACK 0x10 with a reason string through on_publish."""
        from paho.mqtt.packettypes import PacketTypes
        self.published.append(topic)
        if self._client is not None:
            self._client.on_publish(None, None, len(self.published), _reason(PacketTypes.PUBACK, 0x10), _props(PacketTypes.PUBACK, ReasonString="no subscribers"))

    @property
    def messages(self):
        """Async iterator over the queued messages."""
        queued = list(self._messages)

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not queued:
                    raise StopAsyncIteration
                topic, payload, retain = queued.pop(0)
                msg = MagicMock()
                msg.topic = topic
                msg.payload = payload
                msg.retain = retain
                return msg

        return _Iter()


def _run_scan(api, system_ids, factory, **kwargs):
    """Run mqtt_topic_scan with aiomqtt.Client replaced by factory(protocol)."""
    FakeScanClient.instances = []

    def _client(*args, **client_kwargs):
        return factory(client_kwargs["protocol"])

    with patch("sigenergy.aiomqtt.Client", side_effect=_client):
        with patch("sigenergy.ssl.create_default_context", return_value=MagicMock()):
            return run_async(api.mqtt_topic_scan(system_ids, **kwargs))


def test_sigenergy_mqtt_properties_text(my_predbat):
    """MQTT 5 properties render as key=value text; none (MQTT 3.1.1) renders as empty."""
    from paho.mqtt.packettypes import PacketTypes
    from sigenergy import _mqtt_properties_text
    failed = False
    assert _mqtt_properties_text(None) == "", "no properties"
    assert _mqtt_properties_text(_props(PacketTypes.PUBACK)) == "", "empty properties"
    assert _mqtt_properties_text(_props(PacketTypes.PUBACK, ReasonString="not allowed")) == "ReasonString=not allowed", "reason string"
    text = _mqtt_properties_text(_props(PacketTypes.CONNACK, ServerReference="mqtt-eu2.example", ReasonString="moved"))
    assert "ServerReference=mqtt-eu2.example" in text and "ReasonString=moved" in text, "several fields, got {}".format(text)
    return failed


def test_sigenergy_scan_filters_order(my_predbat):
    """Caller's filters first, then per-system and app-key filters, the broad wildcards last; no duplicates."""
    failed = False
    api = MockSigenergyAPI()
    filters = api._scan_filters(["S1", "S2"], extra_filters=["custom/#", "openapi/#"])
    assert filters[:2] == ["custom/#", "openapi/#"], "caller's filters first, got {}".format(filters)
    assert filters.count("openapi/#") == 1, "no duplicates, got {}".format(filters)
    for sid in ("S1", "S2"):
        for f in ("openapi/+/+/{}".format(sid), "openapi/{}/#".format(sid), "+/{}/#".format(sid)):
            assert f in filters, "{} present".format(f)
    assert "openapi/+/test_app_key/#" in filters and "+/test_app_key/#" in filters, "app-key filters present"
    assert filters[-2:] == ["#", "$SYS/#"], "broadest last, got {}".format(filters[-2:])
    assert filters.index("openapi/+/test_app_key/#") < filters.index("#"), "narrow before broad"
    return failed


def test_sigenergy_mqtt_topic_scan(my_predbat):
    """The scan reports each filter's SUBACK with its reason string, the CONNACK properties, and topics seen."""
    from paho.mqtt.packettypes import PacketTypes
    from sigenergy import topic_scan_summary
    failed = False
    api = _probe_api()

    def _suback(topic_filter):
        if topic_filter in ("#", "$SYS/#", "openapi/#"):
            return 0x87, _props(PacketTypes.SUBACK, ReasonString="ACL deny")
        return 0, None

    messages = [
        ("openapi/v2/period/test_app_key/SIG001", b"{\"soc\": 50}", True),
        ("openapi/v2/period/test_app_key/SIG001", b"{\"soc\": 51}", False),
        ("openapi/SIG001/status", b"{}", False),
    ]
    connack = _props(PacketTypes.CONNACK, ServerReference="mqtt-eu2.example")
    report = _run_scan(api, ["SIG001"], lambda protocol: FakeScanClient(protocol, suback=_suback, messages=messages, connack_props=connack), extra_filters=["mine/#"], listen_seconds=5)

    assert report["connected"] is True and report["protocol"] == "V5" and report["error"] is None, "connected, got {}".format(report)
    assert report["connack"] == "ServerReference=mqtt-eu2.example", "CONNACK properties kept, got {}".format(report["connack"])
    client = FakeScanClient.instances[0]
    assert client.subscribed[0] == "mine/#" and client.subscribed[-1] == "$SYS/#", "order, got {}".format(client.subscribed)

    by_filter = {sub["filter"]: sub for sub in report["subscribe"]}
    assert by_filter["#"]["ok"] is False and "Not authorized (0x87)" in by_filter["#"]["reason"], "refusal recorded, got {}".format(by_filter["#"])
    assert "ReasonString=ACL deny" in by_filter["#"]["reason"], "SUBACK reason string kept, got {}".format(by_filter["#"])
    assert by_filter["openapi/+/<redacted>/#"]["ok"] is True, "app key masked in the filter, got {}".format(list(by_filter))
    assert not any("test_app_key" in f for f in by_filter), "app key never in the report"

    assert [pub["topic"] for pub in report["publish"]] == ["openapi/subscription/period", "openapi/subscription/change", "openapi/subscription/alarm"], "data-push requests only"
    assert "No matching subscribers (0x10) [ReasonString=no subscribers]" == report["publish"][0]["reason"], "PUBACK reason string kept, got {}".format(report["publish"][0])

    topics = report["topics"]
    assert topics["openapi/v2/period/<redacted>/SIG001"] == {"count": 2, "retained": True}, "counted and retained flagged, got {}".format(topics)
    assert topics["openapi/SIG001/status"]["count"] == 1, "second topic counted"
    assert not any("probe_token" in m for m in api.log_messages), "token never logged"

    lines = topic_scan_summary(report)
    assert any("CONNACK ServerReference=mqtt-eu2.example" in line for line in lines), "CONNACK in summary, got {}".format(lines)
    assert any("granted" in line and "refused" in line for line in lines), "subscription tally, got {}".format(lines)
    assert any("(retained)" in line and "openapi/v2/period" in line for line in lines), "retained topic shown, got {}".format(lines)
    return failed


def test_sigenergy_mqtt_topic_scan_dropped_on_filter(my_predbat):
    """A broker that drops the connection on a refused filter is reported with the filter that triggered it."""
    from sigenergy import topic_scan_summary
    failed = False
    api = _probe_api()
    report = _run_scan(api, ["SIG001"], lambda protocol: FakeScanClient(protocol, drop_on="#"), listen_seconds=5)
    assert report["connected"] is True, "had connected"
    assert "while subscribing to #" in report["error"], "names the filter, got {}".format(report["error"])
    assert len(FakeScanClient.instances) == 1, "no protocol fallback once connected"
    assert all(sub["ok"] for sub in report["subscribe"]) and "openapi/#" in [sub["filter"] for sub in report["subscribe"]], "filters before it recorded, got {}".format(report["subscribe"])
    lines = topic_scan_summary(report)
    assert any("Connection dropped" in line and "while subscribing to #" in line for line in lines), "drop explained, got {}".format(lines)
    assert any("No messages" in line for line in lines), "silence reported"

    # Not connected at all
    report = _run_scan(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol, refuse_connect=True), listen_seconds=5)
    assert report["connected"] is False and topic_scan_summary(report)[0].startswith("x Could not connect"), "connect failure, got {}".format(report)
    return failed


def test_sigenergy_ack_capture_failure_is_not_silent(my_predbat):
    """If paho's callbacks cannot be hooked, the probe and scan say so instead of passing on uncaptured codes."""
    from sigenergy import command_probe_verdict, topic_scan_summary
    failed = False

    # Probe: data still flows, but with no PUBACK codes captured the verdict must not pass
    api = _probe_api()
    report = _run_probe(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol, paho=False, messages=[("openapi/period/test_app_key/SIG001", b"{}")]), listen_seconds=5)
    assert report["ack_capture"] is False, "capture failure recorded"
    assert all(pub["reason"] == "acknowledged (no reason code)" for pub in report["publish"]), "codes genuinely missing, got {}".format(report["publish"])
    ok, lines = command_probe_verdict(report)
    assert ok is False, "verdict fails rather than passing on uncaptured codes, got {}".format(lines)
    assert any("Could not capture the broker's PUBACK reason codes" in line for line in lines), "explained, got {}".format(lines)
    assert any(m.startswith("Warn:") and "could not hook" in m for m in api.log_messages), "warned, got {}".format(api.log_messages)

    # Probe with the hook in place is unaffected
    report = _run_probe(api, ["SIG001"], lambda protocol: FakeProbeClient(protocol, messages=[("t/test_app_key/SIG001", b"{}")]), listen_seconds=5)
    assert report["ack_capture"] is True and command_probe_verdict(report)[0] is True, "normal run still passes"

    # Scan: same disclosure
    api = _probe_api()
    report = _run_scan(api, ["SIG001"], lambda protocol: FakeScanClient(protocol, paho=False), listen_seconds=5)
    assert report["ack_capture"] is False, "scan records the capture failure"
    assert any("Could not hook paho's callbacks" in line for line in topic_scan_summary(report)), "scan summary explains it"
    return failed


def test_sigenergy_realtime_fallback_unknown_grid_load(my_predbat):
    """With no measured grid/load to hold, the fallback flags its 0s as unknown, warns once, and says so in the log."""
    failed = False
    api = MockSigenergyAPI()
    api.access_token = "fake_token"
    api.token_expires_at = 9_999_999_999
    api._last_request_time = 0
    api.devices["SIG001"] = [{"deviceType": "Inverter", "serialNumber": "INV001"}]
    realtime = {"code": 0, "data": {"realTimeInfo": {"batSoc": 40.0, "batPower": 1.0, "pVPower": 0.5, "activePower": 1.5}}}

    def _realtime():
        with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=realtime))):
            assert run_async(api.fetch_inverter_realtime("SIG001")) is True

    # Fresh install: nothing to hold
    _realtime()
    flow = api.energy_flow["SIG001"]
    assert flow["gridPower"] == 0.0 and flow["loadPower"] == 0.0 and flow.get("gridLoadUnknown") is True, "placeholder 0s flagged, got {}".format(flow)
    warnings = [m for m in api.log_messages if m.startswith("Warn:") and "no grid/load reading yet" in m]
    assert len(warnings) == 1, "warned, got {}".format(api.log_messages)
    assert any("grid and load unknown, published as 0" in m for m in api.log_messages), "log line does not claim a held reading"
    assert not any("held from last reading" in m for m in api.log_messages), "no false 'held' claim"

    # A second fallback still knows its 0s are placeholders, and does not warn again
    api.log_messages.clear()
    _realtime()
    assert api.energy_flow["SIG001"].get("gridLoadUnknown") is True, "still unknown - a placeholder is not a reading"
    assert not any("no grid/load reading yet" in m for m in api.log_messages), "warned once only"

    # energyFlow measures it: the next fallback holds a real reading, without the flag or the wording
    energy = {"code": 0, "data": '{"pvPower":0.5,"gridPower":-0.3,"loadPower":1.8,"batteryPower":-1.0,"batterySoc":40}'}
    with patch("sigenergy.aiohttp.ClientSession", return_value=_make_mock_session(_make_mock_response(status=200, json_data=energy))):
        assert run_async(api.fetch_energy_flow("SIG001")) is True
    assert "gridLoadUnknown" not in api.energy_flow["SIG001"], "a measured reading carries no flag"
    api.log_messages.clear()
    _realtime()
    flow = api.energy_flow["SIG001"]
    assert flow["gridPower"] == -0.3 and flow["loadPower"] == 1.8 and "gridLoadUnknown" not in flow, "real reading held, got {}".format(flow)
    assert any("held from last reading" in m for m in api.log_messages), "held wording when it is true"

    # Losing it again after a measurement warns afresh
    api.energy_flow["SIG001"] = {}
    _realtime()
    assert sum("no grid/load reading yet" in m for m in api.log_messages) == 1, "warns again after energyFlow had recovered"
    return failed


def run_sigenergy_tests(my_predbat):
    """Run all Sigenergy API unit tests.

    Returns:
        False on success (all tests passed), True if any test failed.
    """
    failed = False
    tests = [
        ("helper_functions", test_sigenergy_helper_functions),
        ("initialize", test_sigenergy_initialize),
        ("onboard_status", test_sigenergy_onboard_status),
        ("system_slug", test_sigenergy_system_slug),
        ("battery_capacity", test_sigenergy_battery_capacity),
        ("publish_system_entities", test_sigenergy_publish_system_entities),
        ("publish_system_entities_operational_mode_fallback", test_sigenergy_publish_system_entities_operational_mode_fallback),
        ("publish_system_entities_operational_mode_invalid_value", test_sigenergy_publish_system_entities_operational_mode_invalid_value),
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
        ("fetch_history_totals", test_sigenergy_fetch_history_totals),
        ("fetch_history_totals_empty_data", test_sigenergy_fetch_history_totals_empty_data),
        ("fetch_history_totals_clamps_dip", test_sigenergy_fetch_history_totals_clamps_dip),
        ("fetch_history_totals_keeps_missing_node", test_sigenergy_fetch_history_totals_keeps_missing_node),
        ("save_load_cache_no_storage", test_sigenergy_save_load_cache_no_storage),
        ("save_load_cache_round_trip", test_sigenergy_save_load_cache_round_trip),
        ("load_cached_data", test_sigenergy_load_cached_data),
        ("load_cached_data_no_storage_leaves_defaults", test_sigenergy_load_cached_data_no_storage_leaves_defaults),
        ("history_totals_clamp_survives_restart", test_sigenergy_history_totals_clamp_survives_restart),
        ("needs_refresh", test_sigenergy_needs_refresh),
        ("run_skips_refresh_when_cache_fresh", test_sigenergy_run_skips_refresh_when_cache_fresh),
        ("run_energy_flow_not_bumped_on_fetch_failure", test_sigenergy_run_energy_flow_not_bumped_on_fetch_failure),
        ("apply_controls_charge_mode", test_sigenergy_apply_controls_charge_mode),
        ("apply_controls_eco_mode", test_sigenergy_apply_controls_eco_mode),
        ("apply_controls_deduplication", test_sigenergy_apply_controls_deduplication),
        ("apply_controls_export_mode", test_sigenergy_apply_controls_export_mode),
        ("apply_controls_freeze_export_no_window", test_sigenergy_apply_controls_freeze_export_no_window),
        ("publish_mqtt_success", test_sigenergy_publish_mqtt_success),
        ("redact", test_sigenergy_redact),
        ("publish_mqtt_redacts_token", test_sigenergy_publish_mqtt_redacts_token),
        ("request_log_redacts_credentials", test_sigenergy_request_log_redacts_credentials),
        ("publish_mqtt_failure", test_sigenergy_publish_mqtt_failure),
        ("send_battery_command_mqtt", test_sigenergy_send_battery_command_mqtt),
        ("send_battery_command_no_token", test_sigenergy_send_battery_command_no_token),
        ("handle_mqtt_period", test_sigenergy_handle_mqtt_period),
        ("handle_mqtt_period_partial_update", test_sigenergy_handle_mqtt_period_partial_update),
        ("handle_mqtt_period_never_reported_field", test_sigenergy_handle_mqtt_period_never_reported_field),
        ("handle_mqtt_change", test_sigenergy_handle_mqtt_change),
        ("handle_mqtt_alarm", test_sigenergy_handle_mqtt_alarm),
        ("mqtt_listener_loop", test_sigenergy_mqtt_listener_loop),
        ("mqtt_listener_loop_ignores_other_systems", test_sigenergy_mqtt_listener_loop_ignores_other_systems),
        ("fetch_inverter_realtime", test_sigenergy_fetch_inverter_realtime),
        ("fetch_inverter_realtime_pv_power_key_fallback", test_sigenergy_fetch_inverter_realtime_pv_power_key_fallback),
        ("fetch_energy_flow", test_sigenergy_fetch_energy_flow),
        ("fetch_inverter_realtime_no_inverter", test_sigenergy_fetch_inverter_realtime_no_inverter),
        ("run_falls_back_to_realtime_when_energy_flow_fails", test_sigenergy_run_falls_back_to_realtime_when_energy_flow_fails),
        ("fetch_inverter_realtime_live_sample_holds_grid_and_load", test_sigenergy_fetch_inverter_realtime_live_sample_holds_grid_and_load),
        ("fetch_energy_flow_live_sample_balances", test_sigenergy_fetch_energy_flow_live_sample_balances),
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
        ("offboard_overrides_axle_control", test_sigenergy_offboard_overrides_axle_control),
        ("offboard_toggle_switch_event", test_sigenergy_offboard_toggle_switch_event),
        ("offboard_defers_when_mode_switch_fails", test_sigenergy_offboard_defers_when_the_mode_switch_fails),
        ("offboard_retries_when_api_call_fails", test_sigenergy_offboard_retries_when_the_api_call_fails),
        ("offboard_retries_per_item_failure", test_sigenergy_offboard_retries_per_item_failure),
        ("offboard_unknown_mode_defers", test_sigenergy_offboard_unknown_mode_defers),
        ("offboard_switch_survives_restart", test_sigenergy_offboard_switch_survives_restart),
        ("publish_onboard_status_sensors", test_sigenergy_publish_onboard_status_sensors),
        ("run_derives_onboard_status", test_sigenergy_run_derives_onboard_status),
        ("run_pending_publishes_before_early_exit", test_sigenergy_run_pending_publishes_before_early_exit),
        ("reclaims_vpp_from_third_party_controller", test_sigenergy_reclaims_vpp_from_third_party_controller),
        ("contention_does_not_report_pending_approval", test_sigenergy_contention_does_not_report_pending_approval),
        ("contention_marker_published_before_recovery", test_sigenergy_contention_marker_published_before_recovery),
        ("reclaim_runs_on_the_minute", test_sigenergy_reclaim_runs_on_the_minute),
        ("controls_skipped_message_distinguishes_contention", test_sigenergy_controls_skipped_message_distinguishes_contention),
        ("axle_control_off_reclaims_as_before", test_sigenergy_axle_control_off_reclaims_as_before),
        ("axle_event_leaves_mode_alone", test_sigenergy_axle_event_leaves_mode_alone),
        ("axle_event_skips_controls", test_sigenergy_axle_event_skips_controls),
        ("axle_event_end_resumes_control", test_sigenergy_axle_event_end_resumes_control),
        ("axle_standoff_survives_restart", test_sigenergy_axle_standoff_survives_restart),
        ("mqtt_ack_result", test_sigenergy_mqtt_ack_result),
        ("mqtt_command_probe_success", test_sigenergy_mqtt_command_probe_success),
        ("mqtt_command_probe_refused_command", test_sigenergy_mqtt_command_probe_refused_command),
        ("mqtt_command_probe_no_subscribers", test_sigenergy_mqtt_command_probe_no_subscribers),
        ("mqtt_command_probe_v311_fallback", test_sigenergy_mqtt_command_probe_v311_fallback),
        ("mqtt_command_probe_connect_refused", test_sigenergy_mqtt_command_probe_connect_refused),
        ("mqtt_command_probe_listen_timeout", test_sigenergy_mqtt_command_probe_listen_timeout),
        ("mqtt_command_probe_no_token", test_sigenergy_mqtt_command_probe_no_token),
        ("command_probe_verdict_no_command", test_sigenergy_command_probe_verdict_no_command),
        ("mqtt_properties_text", test_sigenergy_mqtt_properties_text),
        ("scan_filters_order", test_sigenergy_scan_filters_order),
        ("mqtt_topic_scan", test_sigenergy_mqtt_topic_scan),
        ("mqtt_topic_scan_dropped_on_filter", test_sigenergy_mqtt_topic_scan_dropped_on_filter),
        ("ack_capture_failure_is_not_silent", test_sigenergy_ack_capture_failure_is_not_silent),
        ("realtime_fallback_unknown_grid_load", test_sigenergy_realtime_fallback_unknown_grid_load),
        ("discover_systems_from_device_list", test_sigenergy_discover_systems_from_device_list),
        ("discover_systems_throttled_does_not_onboard", test_sigenergy_discover_systems_throttled_does_not_onboard),
        ("discover_systems_403_onboards_directly", test_sigenergy_discover_systems_403_onboards_directly),
        ("fetch_device_list_empty_is_not_a_confirmation", test_sigenergy_fetch_device_list_empty_is_not_a_confirmation),
        ("discover_systems_empty_device_list_retries", test_sigenergy_discover_systems_empty_device_list_retries),
        ("request_403_is_not_retried", test_sigenergy_request_403_is_not_retried),
        ("discover_systems_403_end_to_end", test_sigenergy_discover_systems_403_end_to_end),
        ("onboard_systems_ignores_stale_api_code", test_sigenergy_onboard_systems_ignores_stale_api_code),
        ("run_first_reads_device_list_once", test_sigenergy_run_first_reads_device_list_once),
        ("system_status_from_inverter_device", test_sigenergy_system_status_from_inverter_device),
        ("fetch_system_list_does_not_log_other_systems", test_sigenergy_fetch_system_list_does_not_log_other_systems),
        ("rate_limit_last_attempt_does_not_claim_to_wait", test_sigenergy_rate_limit_last_attempt_does_not_claim_to_wait),
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
