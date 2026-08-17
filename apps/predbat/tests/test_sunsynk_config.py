# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk automatic configuration and registration
# -----------------------------------------------------------------------------

"""Tests for Sunsynk automatic_config, INVERTER_DEF and component registration."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from unittest.mock import patch
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


class ConfigSunsynk(MockSunsynk):
    """MockSunsynk that records set_arg calls."""

    def __init__(self, **kwargs):
        """Set up the recorder."""
        super().__init__(**kwargs)
        self.args_set = {}

    def set_arg(self, key, value):
        """Record an arg assignment."""
        self.args_set[key] = value


def test_inverter_def_registered():
    """SunsynkCloud exists and declares the capabilities the control model relies on."""
    failed = False
    definition = INVERTER_DEF.get("SunsynkCloud")
    if not definition:
        print("ERROR: INVERTER_DEF['SunsynkCloud'] is missing")
        assert False, "test_inverter_def_registered"
    expected = {
        "output_charge_control": "power",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "%",
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "can_span_midnight": False,
        "target_soc_used_for_discharge": True,
    }
    for key, value in expected.items():
        if definition.get(key) != value:
            print(f"ERROR: SunsynkCloud {key} = {definition.get(key)!r}, expected {value!r}")
            failed = True
    assert not failed, "test_inverter_def_registered"


def test_component_registered():
    """The sunsynk component is registered with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("sunsynk")
    if not entry:
        print("ERROR: COMPONENT_LIST['sunsynk'] is missing")
        assert False, "test_component_registered"
    if entry.get("event_filter") != "predbat_sunsynk_":
        print(f"ERROR: event_filter {entry.get('event_filter')!r}")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')!r}, expected 1")
        failed = True
    # Activation must be gated on having at least one usable auth path; every individual
    # arg is optional so either auth mode can be configured alone.
    if sorted(entry.get("required_or", [])) != ["key", "username"]:
        print(f"ERROR: required_or {entry.get('required_or')!r}")
        failed = True
    for arg in ("username", "password", "key", "region", "auth_method", "inverter_sn", "automatic", "control_enable"):
        if arg not in entry.get("args", {}):
            print(f"ERROR: component arg {arg} not registered")
            failed = True
    assert not failed, "test_component_registered"


def test_apps_schema_keys():
    """Every sunsynk_* config key is declared for apps.yaml validation."""
    failed = False
    expected = {
        "sunsynk_username": "string",
        "sunsynk_password": "string",
        "sunsynk_key": "string",
        "sunsynk_region": "string",
        "sunsynk_auth_method": "string",
        "sunsynk_token_expires_at": "string",
        "sunsynk_token_hash": "string",
        "sunsynk_inverter_sn": "string|string_list",
        "sunsynk_automatic": "boolean",
        "sunsynk_automatic_ignore_pv": "boolean",
        "sunsynk_control_enable": "boolean",
        "sunsynk_battery_nominal_voltage": "float",
    }
    for key, kind in expected.items():
        entry = APPS_SCHEMA.get(key)
        if not entry:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
        elif entry.get("type") != kind:
            print(f"ERROR: {key} type {entry.get('type')!r}, expected {kind!r}")
            failed = True
    assert not failed, "test_apps_schema_keys"


def test_automatic_config_maps_control_entities():
    """Every inverter is registered as SunsynkCloud with its sensors and controls."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    for sn in s.device_list:
        s.device_values[sn] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
        s.device_energy[sn] = {"pv_today": 1.0, "import_today": 1.0, "export_today": 1.0, "load_today": 1.0, "battery_charge_today": 1.0, "battery_discharge_today": 1.0}
        s.device_rated_power[sn] = 8000.0
        s.device_settings[sn] = {"batteryLowCap": "10"}
    run_async_local(s.automatic_config())
    if s.args_set.get("inverter_type") != ["SunsynkCloud", "SunsynkCloud"]:
        print(f"ERROR: inverter_type {s.args_set.get('inverter_type')}")
        failed = True
    if s.args_set.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {s.args_set.get('num_inverters')}")
        failed = True
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "battery_rate_max", "inverter_limit", "battery_min_soc"):
        if arg not in s.args_set:
            print(f"ERROR: sensor arg {arg} not mapped")
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
        if arg not in s.args_set:
            print(f"ERROR: control arg {arg} not mapped")
            failed = True
    # Control args must point at the control entities, not sensors.
    if not str(s.args_set.get("charge_start_time", [""])[0]).startswith("select."):
        print(f"ERROR: charge_start_time should be a select entity, got {s.args_set.get('charge_start_time')}")
        failed = True
    assert not failed, "test_automatic_config_maps_control_entities"


def test_automatic_config_skips_partial_capabilities():
    """An arg is only mapped when every inverter reports the underlying value."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    # INV2 has no chargeVolt, so its capacity and rate cannot be derived.
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_values["INV2"] = {"soc": 50, "capacity": 280, "maxChargeCurrentLimit": 100}
    s.device_energy = {"INV1": {"pv_today": 1.0}, "INV2": {}}
    s.device_rated_power = {"INV1": 8000.0}
    run_async_local(s.automatic_config())
    for arg in ("soc_max", "battery_rate_max", "inverter_limit", "pv_today"):
        if arg in s.args_set:
            print(f"ERROR: {arg} was mapped although not every inverter reports it")
            failed = True
    if not any("manually" in str(m) for m in s.log_messages):
        print("ERROR: skipping an arg should warn the user to set it in apps.yaml")
        failed = True
    assert not failed, "test_automatic_config_skips_partial_capabilities"


def test_automatic_config_respects_ignore_pv():
    """automatic_ignore_pv leaves the PV args for another component to own."""
    failed = False
    s = ConfigSunsynk()
    s.automatic_ignore_pv = True
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_energy["INV1"] = {"pv_today": 1.0}
    s.device_rated_power["INV1"] = 8000.0
    run_async_local(s.automatic_config())
    for arg in ("pv_power", "pv_today"):
        if arg in s.args_set:
            print(f"ERROR: {arg} mapped despite automatic_ignore_pv")
            failed = True
    if "soc_percent" not in s.args_set:
        print("ERROR: non-PV args should still be mapped")
        failed = True
    assert not failed, "test_automatic_config_respects_ignore_pv"


def test_run_first_cycle_polls_and_publishes():
    """The first run restores caches, discovers, polls and publishes."""
    failed = False
    s = ConfigSunsynk()
    calls = []

    async def fake_restore():
        """Record the cache restore."""
        calls.append("restore")

    async def fake_token():
        """Record the login."""
        calls.append("token")
        return True

    async def fake_device_list():
        """Record discovery and return one inverter."""
        calls.append("discover")
        s.device_list = ["INV1"]
        return ["INV1"]

    async def fake_detail(sn):
        """Record the detail fetch."""
        calls.append("detail")
        return {"ratePower": 8000}

    async def fake_device_data(sn):
        """Record the telemetry poll."""
        calls.append("telemetry")
        s.device_values[sn] = {"soc": 50}
        return {"soc": 50}

    async def fake_settings(sn):
        """Record the settings read."""
        calls.append("settings")
        return {"batteryLowCap": "10"}

    async def fake_publish():
        """Record publishing."""
        calls.append("publish")

    with (
        patch.object(s, "restore_state", side_effect=fake_restore),
        patch.object(s, "fetch_token", side_effect=fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list),
        patch.object(s, "fetch_device_detail", side_effect=fake_detail),
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "publish_data", side_effect=fake_publish),
    ):
        run_async_local(s.run(0, True))
    for step in ("restore", "discover", "telemetry", "publish"):
        if step not in calls:
            print(f"ERROR: first cycle never did {step}; calls were {calls}")
            failed = True
    assert not failed, "test_run_first_cycle_polls_and_publishes"


def run_sunsynk_config_tests(my_predbat):
    """Run all Sunsynk configuration tests."""
    failed = False
    for name, fn in [
        ("inverter_def", test_inverter_def_registered),
        ("component_registered", test_component_registered),
        ("apps_schema", test_apps_schema_keys),
        ("automatic_config", test_automatic_config_maps_control_entities),
        ("partial_capabilities", test_automatic_config_skips_partial_capabilities),
        ("ignore_pv", test_automatic_config_respects_ignore_pv),
        ("run_first_cycle", test_run_first_cycle_polls_and_publishes),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
