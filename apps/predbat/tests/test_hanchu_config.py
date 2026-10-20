# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Hanchu config and INVERTER_DEF registration
# -----------------------------------------------------------------------------

"""Tests for the Hanchu component registration, INVERTER_DEF entry and APPS_SCHEMA keys."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from components import COMPONENT_LIST
from config import APPS_SCHEMA, INVERTER_DEF


def test_hanchu_component_registered():
    """The component is registered in phase 1 with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("hanchu")
    if not entry:
        print("ERROR: hanchu not in COMPONENT_LIST")
        assert False, "test_hanchu_component_registered"
    if entry.get("class") != "hanchu.HanchuAPI":
        print(f"ERROR: class {entry.get('class')}")
        failed = True
    if entry.get("event_filter") != "predbat_hanchu_":
        print(f"ERROR: event_filter {entry.get('event_filter')}")
        failed = True
    if not entry.get("inverter"):
        print("ERROR: hanchu must be flagged as an inverter component")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')}")
        failed = True
    if not entry.get("can_restart"):
        print("ERROR: hanchu should be restartable")
        failed = True
    # Without required_or the component would start for every Predbat instance, since all
    # individual args are optional.
    if entry.get("required_or") != ["account"]:
        print(f"ERROR: required_or {entry.get('required_or')}")
        failed = True
    for arg, config_key, default in [
        ("account", "hanchu_account", None),
        ("password", "hanchu_password", None),
        ("inverter_sn", "hanchu_inverter_sn", None),
        ("automatic", "hanchu_automatic", False),
        ("automatic_ignore_pv", "hanchu_automatic_ignore_pv", False),
        ("control_enable", "hanchu_control_enable", True),
        ("work_mode_control", "hanchu_work_mode_control", True),
        ("battery_rate_max", "hanchu_battery_rate_max", None),
        ("inverter_limit", "hanchu_inverter_limit", None),
        ("api_delay", "hanchu_api_delay", 1),
        ("min_write_interval", "hanchu_min_write_interval", 60),
    ]:
        info = entry["args"].get(arg)
        if not info:
            print(f"ERROR: arg {arg} missing")
            failed = True
            continue
        if info.get("config") != config_key:
            print(f"ERROR: arg {arg} config {info.get('config')} != {config_key}")
            failed = True
        if default is not None and info.get("default") != default:
            print(f"ERROR: arg {arg} default {info.get('default')} != {default}")
            failed = True
    assert not failed, "test_hanchu_component_registered"


def test_hanchu_credentials_are_marked_secret():
    """The account e-mail and password are the whole credential, so both must be masked."""
    failed = False
    args = COMPONENT_LIST["hanchu"]["args"]
    for arg in ("account", "password"):
        if not args[arg].get("secret"):
            print(f"ERROR: arg {arg} must be marked secret - it is a Hanchu app login credential")
            failed = True
    assert not failed, "test_hanchu_credentials_are_marked_secret"


def test_hanchu_control_enable_defaults_true():
    """An inverter component that does not drive the inverter is not what a user expects."""
    failed = False
    if COMPONENT_LIST["hanchu"]["args"]["control_enable"].get("default") is not True:
        print("ERROR: control_enable default should be True")
        failed = True
    # The work-mode escape hatch defaults ON, i.e. Predbat does assert the mode. The deduction is
    # that this is needed for the slots to run at all, so defaulting it off would ship a component
    # that quietly does nothing on most installs.
    if COMPONENT_LIST["hanchu"]["args"]["work_mode_control"].get("default") is not True:
        print("ERROR: work_mode_control default should be True")
        failed = True
    assert not failed, "test_hanchu_control_enable_defaults_true"


def test_hanchu_inverter_def_complete():
    """HanchuCloud declares every key the other cloud inverter types declare."""
    failed = False
    entry = INVERTER_DEF.get("HanchuCloud")
    if not entry:
        print("ERROR: HanchuCloud not in INVERTER_DEF")
        assert False, "test_hanchu_inverter_def_complete"
    # Keys inverter.py reads through .get() with a default are opt-in capabilities a type is meant
    # to omit. There is no feed-in-first work mode among the four this API exposes, so HanchuCloud
    # must NOT declare support_feedin_first.
    optional_keys = {"support_feedin_first"}
    reference = set(INVERTER_DEF["SunsynkCloud"].keys()) - optional_keys
    missing = reference - set(entry.keys())
    if missing:
        print(f"ERROR: HanchuCloud missing keys {sorted(missing)}")
        failed = True
    if "support_feedin_first" in entry:
        print("ERROR: HanchuCloud must not declare support_feedin_first - this API has no feed-in-first mode")
        failed = True
    expected = {
        "name": "HanchuCloud",
        "has_rest_api": False,
        "has_mqtt_api": False,
        # CHG_PWR_LMT / DSCHG_PWR_LMT are real watt setpoints, and zero means freeze.
        "output_charge_control": "power",
        # Everything goes through the timed slot table; fastChargeDischarge is deliberately unused.
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        # No pause operation exists anywhere in this API.
        "has_timed_pause": False,
        # Anything else makes inverter.py replace the published select entities with its own
        # dummies and the window never reaches the component.
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "%",
        "time_button_press": True,
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        # Wrap-around behaviour is undocumented for the slot pairs, so Predbat splits.
        "can_span_midnight": False,
        "charge_discharge_with_rate": False,
        "target_soc_used_for_discharge": True,
    }
    for key, value in expected.items():
        if entry.get(key) != value:
            print(f"ERROR: HanchuCloud[{key}] = {entry.get(key)} != {value}")
            failed = True
    assert not failed, "test_hanchu_inverter_def_complete"


def test_hanchu_apps_schema_keys():
    """Every hanchu_* key a user may set is declared with the right type."""
    failed = False
    expected = {
        "hanchu_account": "string",
        "hanchu_password": "string",
        "hanchu_inverter_sn": "string|string_list",
        "hanchu_automatic": "boolean",
        "hanchu_automatic_ignore_pv": "boolean",
        "hanchu_control_enable": "boolean",
        "hanchu_work_mode_control": "boolean",
        "hanchu_battery_rate_max": "float",
        "hanchu_inverter_limit": "float",
        "hanchu_api_delay": "float",
        "hanchu_min_write_interval": "integer",
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
    # Every config key the registry references must exist in the schema, or a user setting it would
    # be rejected as unknown by the validator.
    for arg, info in COMPONENT_LIST["hanchu"]["args"].items():
        key = info.get("config")
        if key and key not in APPS_SCHEMA:
            print(f"ERROR: registry arg {arg} points at {key}, which APPS_SCHEMA does not declare")
            failed = True
    assert not failed, "test_hanchu_apps_schema_keys"


def run_hanchu_config_tests(my_predbat):
    """Run all Hanchu config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("component_registered", test_hanchu_component_registered),
        ("credentials_secret", test_hanchu_credentials_are_marked_secret),
        ("control_enable_default", test_hanchu_control_enable_defaults_true),
        ("inverter_def", test_hanchu_inverter_def_complete),
        ("apps_schema", test_hanchu_apps_schema_keys),
    ]:
        try:
            if fn():
                print(f"  FAILED: hanchu_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in hanchu_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
