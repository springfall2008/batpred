# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fronius config and INVERTER_DEF registration
# -----------------------------------------------------------------------------

"""Tests for the Fronius component registration, INVERTER_DEF entry and APPS_SCHEMA keys."""

import inspect

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from coordinator import CAPABILITY_KEYS
from fronius import FroniusCloud, FRONIUS_CAPABILITIES


def test_fronius_component_registered():
    """The component is registered as an inverter in phase 1, gated on having a key."""
    entry = COMPONENT_LIST.get("fronius")
    assert entry, "fronius not in COMPONENT_LIST"
    assert entry.get("class") == "fronius.FroniusCloud", entry.get("class")
    assert entry.get("event_filter") == "predbat_fronius_"
    assert entry.get("phase") == 1 and entry.get("inverter") is True
    # Without required_or the component would start for every instance, since every arg is optional.
    assert entry.get("required_or") == ["access_key_id"], entry.get("required_or")


def test_fronius_config_contract_names():
    """The four contract keys are spelled exactly as other builds depend on them."""
    args = COMPONENT_LIST["fronius"]["args"]
    for arg, config_key, default in [
        ("access_key_id", "fronius_access_key_id", None),
        ("access_key_value", "fronius_access_key_value", None),
        ("pv_system_id", "fronius_pv_system_id", None),
        ("automatic", "fronius_automatic", False),
        ("automatic_ignore_pv", "fronius_automatic_ignore_pv", False),
        ("control_enable", "fronius_control_enable", True),
        ("battery_rate_max", "fronius_battery_rate_max", None),
        ("grid_export_limit", "fronius_grid_export_limit", None),
        ("energy_interval", "fronius_energy_interval", 30),
        ("query_url", "fronius_query_url", None),
        ("control_url", "fronius_control_url", None),
    ]:
        info = args.get(arg)
        assert info, "arg {} missing".format(arg)
        assert info.get("config") == config_key, (arg, info)
        if default is not None:
            assert info.get("default") == default, (arg, info)


def test_fronius_registry_args_match_initialize():
    """Every registry arg is an initialize() parameter, so none is silently swallowed by **kwargs."""
    parameters = set(inspect.signature(FroniusCloud.initialize).parameters) - {"self", "kwargs"}
    assert set(COMPONENT_LIST["fronius"]["args"]) == parameters, set(COMPONENT_LIST["fronius"]["args"]) ^ parameters


def test_fronius_credentials_are_secret():
    """Both halves of the key are redacted from dumps and the web UI."""
    args = COMPONENT_LIST["fronius"]["args"]
    assert args["access_key_id"].get("secret") and args["access_key_value"].get("secret")


def test_fronius_apps_schema_keys():
    """Every fronius_* key the registry names is declared in APPS_SCHEMA with the right type."""
    for key, expected in [
        ("fronius_access_key_id", "string"),
        ("fronius_access_key_value", "string"),
        ("fronius_pv_system_id", "string"),
        ("fronius_automatic", "boolean"),
        ("fronius_automatic_ignore_pv", "boolean"),
        ("fronius_control_enable", "boolean"),
        ("fronius_battery_rate_max", "float"),
        ("fronius_grid_export_limit", "float"),
        ("fronius_energy_interval", "integer"),
        ("fronius_query_url", "string"),
        ("fronius_control_url", "string"),
    ]:
        entry = APPS_SCHEMA.get(key)
        assert entry is not None, "{} missing from APPS_SCHEMA".format(key)
        assert entry.get("type") == expected, (key, entry)
    for arg, info in COMPONENT_LIST["fronius"]["args"].items():
        assert info.get("config") in APPS_SCHEMA, "registry arg {} names {}, which is not in APPS_SCHEMA".format(arg, info.get("config"))


def test_fronius_inverter_def_row():
    """The FroniusCloud row describes what the component can actually do."""
    row = INVERTER_DEF.get("FroniusCloud")
    assert row is not None, "FroniusCloud missing from INVERTER_DEF"
    for field, expected in [
        ("name", "FroniusCloud"),
        ("has_rest_api", False),
        ("has_mqtt_api", False),
        ("output_charge_control", "power"),
        ("charge_control_immediate", False),
        ("has_charge_enable_time", True),
        ("has_discharge_enable_time", True),
        ("has_target_soc", True),
        # The API cannot set a minimum SoC; Predbat supplies its own dummy reserve.
        ("has_reserve_soc", False),
        ("has_timed_pause", False),
        ("charge_time_format", "HH:MM:SS"),
        ("charge_time_entity_is_option", True),
        ("soc_units", "%"),
        ("time_button_press", True),
        ("support_charge_freeze", True),
        ("support_discharge_freeze", True),
        ("has_idle_time", False),
        ("can_span_midnight", False),
        ("target_soc_used_for_discharge", True),
    ]:
        assert row.get(field) == expected, "FroniusCloud {} is {!r}, expected {!r}".format(field, row.get(field), expected)


def test_fronius_capabilities_rebuild_the_inverter_def_row():
    """FRONIUS_CAPABILITIES agrees with INVERTER_DEF for every capability key."""
    row = INVERTER_DEF["FroniusCloud"]
    row_defaults = {"support_feedin_first": False, "charge_discharge_with_rate": False, "target_soc_used_for_discharge": True}
    for key in CAPABILITY_KEYS:
        assert key in FRONIUS_CAPABILITIES, "capability {} missing".format(key)
        expected = row[key] if key in row else row_defaults[key]
        assert FRONIUS_CAPABILITIES[key] == expected, (key, FRONIUS_CAPABILITIES[key], expected)


def run_fronius_config_tests(my_predbat):
    """Run all Fronius config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("component_registered", test_fronius_component_registered),
        ("contract_names", test_fronius_config_contract_names),
        ("args_match_initialize", test_fronius_registry_args_match_initialize),
        ("credentials_secret", test_fronius_credentials_are_secret),
        ("apps_schema", test_fronius_apps_schema_keys),
        ("inverter_def_row", test_fronius_inverter_def_row),
        ("capabilities_agree", test_fronius_capabilities_rebuild_the_inverter_def_row),
    ]:
        try:
            if fn():
                print(f"  FAILED: fronius_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in fronius_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
