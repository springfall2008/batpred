# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sungrow config and INVERTER_DEF registration
# -----------------------------------------------------------------------------

"""Tests for the Sungrow component registration, INVERTER_DEF entry and APPS_SCHEMA keys."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from sungrow import SUNGROW_CAPABILITIES
from coordinator import CAPABILITY_KEYS


def test_sungrow_component_registered():
    """The component is registered in phase 1 with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("sungrow")
    if not entry:
        print("ERROR: sungrow not in COMPONENT_LIST")
        assert False, "test_sungrow_component_registered"
    if entry.get("class") != "sungrow.SungrowAPI":
        print(f"ERROR: class {entry.get('class')}")
        failed = True
    if entry.get("event_filter") != "predbat_sungrow_":
        print(f"ERROR: event_filter {entry.get('event_filter')}")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')}")
        failed = True
    if not entry.get("inverter"):
        print("ERROR: sungrow should be marked as an inverter component")
        failed = True
    # Without required_or the component would start for every Predbat instance, since all
    # individual args are optional to allow the SaaS token-injection path alongside a
    # self-hosted one.
    if entry.get("required_or") != ["appkey"]:
        print(f"ERROR: required_or {entry.get('required_or')}")
        failed = True
    for arg, config_key, default in [
        ("appkey", "sungrow_appkey", None),
        ("access_key", "sungrow_access_key", None),
        ("key", "sungrow_key", None),
        ("auth_method", "sungrow_auth_method", "oauth"),
        ("token_expires_at", "sungrow_token_expires_at", None),
        ("token_hash", "sungrow_token_hash", None),
        ("gateway", "sungrow_gateway", "europe"),
        ("inverter_sn", "sungrow_inverter_sn", None),
        ("automatic", "sungrow_automatic", False),
        ("automatic_ignore_pv", "sungrow_automatic_ignore_pv", False),
        ("control_enable", "sungrow_control_enable", True),
        ("heartbeat_interval", "sungrow_heartbeat_interval", 300),
        ("forced_charge_schedule", "sungrow_forced_charge_schedule", False),
        ("battery_rate_max", "sungrow_battery_rate_max", None),
        ("api_delay", "sungrow_api_delay", 2),
        ("min_write_interval", "sungrow_min_write_interval", 300),
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
    assert not failed, "test_sungrow_component_registered"


def test_sungrow_credentials_are_marked_secret():
    """Every credential is flagged secret so it is redacted from dumps and the web UI."""
    failed = False
    args = COMPONENT_LIST["sungrow"]["args"]
    for arg in ("appkey", "access_key", "key", "token_hash"):
        if not args[arg].get("secret"):
            print(f"ERROR: {arg} is not marked secret")
            failed = True
    assert not failed, "test_sungrow_credentials_are_marked_secret"


def test_sungrow_key_is_registered_for_the_saas_token_path():
    """Predbat.com injects the access token as sungrow_key.

    Without this entry the token is dropped by the registry and every API call is rejected as
    unauthorised - the exact trap deye and sunsynk both hit.
    """
    failed = False
    if "key" not in COMPONENT_LIST["sungrow"]["args"]:
        print("ERROR: sungrow_key is not a registered arg")
        failed = True
    if "sungrow_key" not in APPS_SCHEMA:
        print("ERROR: sungrow_key is not in APPS_SCHEMA, so apps.yaml validation would reject it")
        failed = True
    assert not failed, "test_sungrow_key_is_registered_for_the_saas_token_path"


def test_sungrow_forced_charge_schedule_defaults_off():
    """The in-inverter forced-charging window defeats the heartbeat's safe revert, so it is opt-in.

    The external-dispatch command lapses back to self-consumption when Predbat stops. The
    forced-charging window does not - it is stored in the inverter and keeps running. Defaulting
    it on would quietly remove the safety property the whole component is built around.
    """
    failed = False
    if COMPONENT_LIST["sungrow"]["args"]["forced_charge_schedule"].get("default") is not False:
        print("ERROR: forced_charge_schedule must default to False")
        failed = True
    assert not failed, "test_sungrow_forced_charge_schedule_defaults_off"


def test_sungrow_apps_schema_keys():
    """Every sungrow_* config key the registry names is declared in APPS_SCHEMA with a type."""
    failed = False
    for key, expected in [
        ("sungrow_appkey", "string"),
        ("sungrow_access_key", "string"),
        ("sungrow_key", "string"),
        ("sungrow_auth_method", "string"),
        ("sungrow_token_expires_at", "string"),
        ("sungrow_token_hash", "string"),
        ("sungrow_gateway", "string"),
        ("sungrow_inverter_sn", "string|string_list"),
        ("sungrow_automatic", "boolean"),
        ("sungrow_automatic_ignore_pv", "boolean"),
        ("sungrow_control_enable", "boolean"),
        ("sungrow_heartbeat_interval", "integer"),
        ("sungrow_forced_charge_schedule", "boolean"),
        ("sungrow_battery_rate_max", "float"),
        ("sungrow_api_delay", "float"),
        ("sungrow_min_write_interval", "integer"),
    ]:
        entry = APPS_SCHEMA.get(key)
        if entry is None:
            print(f"ERROR: {key} missing from APPS_SCHEMA")
            failed = True
            continue
        if entry.get("type") != expected:
            print(f"ERROR: {key} type {entry.get('type')} != {expected}")
            failed = True
    # Every registry config key must be declared, or apps.yaml validation rejects it.
    for arg, info in COMPONENT_LIST["sungrow"]["args"].items():
        config_key = info.get("config")
        if config_key and config_key not in APPS_SCHEMA:
            print(f"ERROR: registry arg {arg} names {config_key}, which is not in APPS_SCHEMA")
            failed = True
    assert not failed, "test_sungrow_apps_schema_keys"


def test_sungrow_inverter_def_row():
    """The SungrowCloud row describes what the component can actually do."""
    failed = False
    row = INVERTER_DEF.get("SungrowCloud")
    if row is None:
        print("ERROR: SungrowCloud missing from INVERTER_DEF")
        assert False, "test_sungrow_inverter_def_row"
    for field, expected in [
        ("name", "SungrowCloud"),
        ("has_rest_api", False),
        ("has_mqtt_api", False),
        # 10005 is a power in watts, so Predbat's rate entities map straight onto it.
        ("output_charge_control", "power"),
        ("has_charge_enable_time", True),
        ("has_discharge_enable_time", True),
        ("has_target_soc", True),
        ("has_reserve_soc", True),
        # No pause parameter exists; freeze is expressed via the rate entities and turned into
        # the inverter's own stop command.
        ("has_timed_pause", False),
        # Anything other than HH:MM:SS makes inverter.py substitute dummy entities and the
        # window never reaches the component.
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
        if row.get(field) != expected:
            print(f"ERROR: SungrowCloud {field} is {row.get(field)!r}, expected {expected!r}")
            failed = True
    assert not failed, "test_sungrow_inverter_def_row"


def test_sungrow_capabilities_rebuild_the_inverter_def_row():
    """SUNGROW_CAPABILITIES agrees with INVERTER_DEF for every capability key.

    The discovery record carries these as a literal rather than reading the row back, so this is
    the only thing keeping the two from drifting apart.
    """
    failed = False
    row = INVERTER_DEF["SungrowCloud"]
    # inverter.py's own defaults for the fields a row may leave out.
    row_defaults = {"support_feedin_first": False, "charge_discharge_with_rate": False, "target_soc_used_for_discharge": True}
    for key in CAPABILITY_KEYS:
        if key not in SUNGROW_CAPABILITIES:
            print(f"ERROR: capability {key} missing from SUNGROW_CAPABILITIES")
            failed = True
            continue
        expected = row[key] if key in row else row_defaults[key]
        if SUNGROW_CAPABILITIES[key] != expected:
            print(f"ERROR: capability {key} is {SUNGROW_CAPABILITIES[key]}, INVERTER_DEF says {expected}")
            failed = True
    assert not failed, "test_sungrow_capabilities_rebuild_the_inverter_def_row"


def run_sungrow_config_tests(my_predbat):
    """Run all Sungrow config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("component_registered", test_sungrow_component_registered),
        ("credentials_secret", test_sungrow_credentials_are_marked_secret),
        ("saas_key_registered", test_sungrow_key_is_registered_for_the_saas_token_path),
        ("forced_schedule_off", test_sungrow_forced_charge_schedule_defaults_off),
        ("apps_schema", test_sungrow_apps_schema_keys),
        ("inverter_def_row", test_sungrow_inverter_def_row),
        ("capabilities_agree", test_sungrow_capabilities_rebuild_the_inverter_def_row),
    ]:
        try:
            if fn():
                print(f"  FAILED: sungrow_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sungrow_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
