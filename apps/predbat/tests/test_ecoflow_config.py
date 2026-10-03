# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test EcoFlow config and INVERTER_DEF registration
# -----------------------------------------------------------------------------

"""Tests for the EcoFlow component registration, INVERTER_DEF entry and APPS_SCHEMA keys."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST


def test_ecoflow_component_registered():
    """The component is registered in phase 1 with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("ecoflow")
    if not entry:
        print("ERROR: ecoflow not in COMPONENT_LIST")
        assert False, "test_ecoflow_component_registered"
    if entry.get("class") != "ecoflow.EcoFlowAPI":
        print(f"ERROR: class {entry.get('class')}")
        failed = True
    if entry.get("event_filter") != "predbat_ecoflow_":
        print(f"ERROR: event_filter {entry.get('event_filter')}")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')}")
        failed = True
    if not entry.get("inverter"):
        print("ERROR: ecoflow should be marked as an inverter component")
        failed = True
    if not entry.get("can_restart"):
        print("ERROR: ecoflow should be restartable")
        failed = True
    # Without required_or the component would start for every Predbat instance, since all
    # individual args are optional.
    if entry.get("required_or") != ["access_key"]:
        print(f"ERROR: required_or {entry.get('required_or')}")
        failed = True
    for arg, config_key, secret in [
        ("access_key", "ecoflow_access_key", True),
        ("secret_key", "ecoflow_secret_key", True),
        ("device_sn", "ecoflow_device_sn", False),
        ("automatic", "ecoflow_automatic", False),
        ("automatic_ignore_pv", "ecoflow_automatic_ignore_pv", False),
        ("control_enable", "ecoflow_control_enable", False),
        ("key_map", "ecoflow_key_map", False),
        ("battery_capacity", "ecoflow_battery_capacity", False),
        ("inverter_limit", "ecoflow_inverter_limit", False),
        ("api_delay", "ecoflow_api_delay", False),
        ("min_write_interval", "ecoflow_min_write_interval", False),
    ]:
        info = entry["args"].get(arg)
        if not info:
            print(f"ERROR: arg {arg} missing")
            failed = True
            continue
        if info.get("config") != config_key:
            print(f"ERROR: arg {arg} config {info.get('config')} != {config_key}")
            failed = True
        if bool(info.get("secret")) != secret:
            print(f"ERROR: arg {arg} secret {info.get('secret')} != {secret}")
            failed = True
    for arg, default in (("automatic", False), ("automatic_ignore_pv", False), ("api_delay", 2), ("min_write_interval", 300)):
        if entry["args"][arg].get("default") != default:
            print(f"ERROR: arg {arg} default {entry['args'][arg].get('default')} != {default}")
            failed = True
    assert not failed, "test_ecoflow_component_registered"


def test_ecoflow_control_enable_defaults_false():
    """control_enable defaults FALSE here, unlike every other cloud inverter component.

    Deliberate and load-bearing: alphaess and sunsynk default True because they can write, and
    this one cannot - the published documentation gives no Power Ocean cmdSet/id pair, so
    ECOFLOW_SCHEDULE_COMMANDS is empty and no control entity is bound. Defaulting True would
    advertise control that does not exist. Flip it with the command table, not before it.
    """
    failed = False
    info = COMPONENT_LIST["ecoflow"]["args"]["control_enable"]
    if info.get("default") is not False:
        print(f"ERROR: control_enable default {info.get('default')} should be False while there is no write command")
        failed = True
    assert not failed, "test_ecoflow_control_enable_defaults_false"


def test_ecoflow_inverter_def_is_monitor_only():
    """EcoFlowCloud declares every key the other cloud types declare, and no write capability.

    output_charge_control "none" and the False presence flags are what make inverter.py
    substitute a visibly inert dummy for each control, instead of Predbat believing it is
    driving a battery no write can reach.
    """
    failed = False
    entry = INVERTER_DEF.get("EcoFlowCloud")
    if not entry:
        print("ERROR: EcoFlowCloud not in INVERTER_DEF")
        assert False, "test_ecoflow_inverter_def_is_monitor_only"
    # Keys inverter.py reads through .get() with a default are opt-in capabilities a type is
    # meant to omit.
    optional_keys = {"support_feedin_first"}
    reference = set(INVERTER_DEF["AlphaESSCloud"].keys()) - optional_keys
    missing = reference - set(entry.keys())
    if missing:
        print(f"ERROR: EcoFlowCloud missing keys {sorted(missing)}")
        failed = True
    expected = {
        "has_rest_api": False,
        # False although the platform DOES push telemetry over MQTT: this flag describes what
        # Predbat actually uses, and the component polls /quota/all today.
        "has_mqtt_api": False,
        "output_charge_control": "none",
        "has_charge_enable_time": False,
        "has_discharge_enable_time": False,
        "has_target_soc": False,
        "has_reserve_soc": False,
        "has_timed_pause": False,
        "soc_units": "%",
        "time_button_press": False,
        "support_charge_freeze": False,
        "support_discharge_freeze": False,
        "can_span_midnight": False,
        "charge_discharge_with_rate": False,
        "charge_control_immediate": False,
        "target_soc_used_for_discharge": False,
    }
    for key, value in expected.items():
        if entry.get(key) != value:
            print(f"ERROR: EcoFlowCloud[{key}] = {entry.get(key)} != {value}")
            failed = True
    assert not failed, "test_ecoflow_inverter_def_is_monitor_only"


def test_ecoflow_apps_schema_keys():
    """Every ecoflow_* key a user may set is declared with the right type."""
    failed = False
    expected = {
        "ecoflow_access_key": "string",
        "ecoflow_secret_key": "string",
        "ecoflow_device_sn": "string|string_list",
        "ecoflow_automatic": "boolean",
        "ecoflow_automatic_ignore_pv": "boolean",
        "ecoflow_control_enable": "boolean",
        # A dict, because it maps Predbat's leaf names onto the device's own quota field names.
        "ecoflow_key_map": "dict",
        "ecoflow_battery_capacity": "float",
        "ecoflow_inverter_limit": "float",
        "ecoflow_api_delay": "float",
        "ecoflow_min_write_interval": "integer",
    }
    for key, kind in expected.items():
        entry = APPS_SCHEMA.get(key)
        if not entry:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
            continue
        if entry.get("type") != kind:
            print(f"ERROR: APPS_SCHEMA[{key}] type {entry.get('type')} != {kind}")
            failed = True
    # Every config key the registry references must exist in the schema, or a user setting it
    # gets an "unknown key" error for an arg the component genuinely reads.
    for arg, info in COMPONENT_LIST["ecoflow"]["args"].items():
        if info["config"] not in APPS_SCHEMA:
            print(f"ERROR: registry arg {arg} references undeclared config key {info['config']}")
            failed = True
    assert not failed, "test_ecoflow_apps_schema_keys"


def run_ecoflow_config_tests(my_predbat):
    """Run all EcoFlow config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("component_registered", test_ecoflow_component_registered),
        ("control_enable_default", test_ecoflow_control_enable_defaults_false),
        ("inverter_def", test_ecoflow_inverter_def_is_monitor_only),
        ("apps_schema", test_ecoflow_apps_schema_keys),
    ]:
        try:
            if fn():
                print(f"  FAILED: ecoflow_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in ecoflow_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
