# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for validate_config() — the APPS_SCHEMA validator in predbat.py.

Each sub-test isolates a single arg, calls validate_config(), and checks
whether that arg appears (or not) in arg_errors.  The total error count is
not asserted because unrelated args in the test fixture may already have
issues; only the field under test is inspected.

Types covered (see APPS_SCHEMA in config.py):
  string, string_list, string with empty/allowed constraints
  integer, integer_list, integer with zero:False constraint
  float, float_list
  boolean
  dict, dict_list
  int_float_dict (with and without or_auto)
  sensor (single entity, various sensor_type values)
  sensor_list (with entries, modify, none|string sensor_type)
"""

import os
import tempfile


def _run(my_predbat, extra_args, extra_states=None, expect_errors=(), expect_clean=()):
    """Inject args/states, run validate_config, assert per-field expectations.

    Args:
        my_predbat: the PredBat fixture instance.
        extra_args: dict of args to add/override for this test run.
        extra_states: dict of entity_id -> state to add to ha_interface.dummy_items.
        expect_errors: iterable of arg names that MUST appear in arg_errors.
        expect_clean: iterable of arg names that must NOT appear in arg_errors.
    """
    saved_args = my_predbat.args.copy()
    saved_states = my_predbat.ha_interface.dummy_items.copy()
    try:
        my_predbat.args.update(extra_args)
        if extra_states:
            my_predbat.ha_interface.dummy_items.update(extra_states)
        my_predbat.validate_config()
        for name in expect_errors:
            assert name in my_predbat.arg_errors, f"Expected validation error for '{name}' but none raised. arg_errors={my_predbat.arg_errors}"
        for name in expect_clean:
            assert name not in my_predbat.arg_errors, f"Unexpected validation error for '{name}': {my_predbat.arg_errors.get(name)}"
    finally:
        my_predbat.args = saved_args
        my_predbat.ha_interface.dummy_items = saved_states


def test_validate_config(my_predbat):
    """Comprehensive validator tests covering every validation type in APPS_SCHEMA."""
    print("**** test_validate_config ****")

    # ==========================================================================
    # STRING type  (gateway_mqtt_host: {"type": "string", "empty": False})
    # ==========================================================================
    print("  [string] plain hostname passes")
    _run(my_predbat, {"gateway_mqtt_host": "mqtt.predbat.com"}, expect_clean=["gateway_mqtt_host"])

    print("  [string] bare hostname without dots passes")
    _run(my_predbat, {"gateway_mqtt_host": "localhost"}, expect_clean=["gateway_mqtt_host"])

    print("  [string] integer value fails")
    _run(my_predbat, {"gateway_mqtt_host": 1234}, expect_errors=["gateway_mqtt_host"])

    print("  [string] dict value fails")
    _run(my_predbat, {"gateway_mqtt_host": {"host": "mqtt.predbat.com"}}, expect_errors=["gateway_mqtt_host"])

    # empty flag behaviour: "empty: True" in spec means the validator WILL check
    # for emptiness and error; "empty: False" means no empty check is performed.
    # predbat_repository: {"type": "string", "empty": True}
    print("  [string empty:True] non-empty value passes")
    _run(my_predbat, {"predbat_repository": "https://github.com/myrepo"}, expect_clean=["predbat_repository"])

    print("  [string empty:True] empty string fails")
    _run(my_predbat, {"predbat_repository": ""}, expect_errors=["predbat_repository"])

    # ==========================================================================
    # STRING_LIST type  (notify_devices: {"type": "string_list"})
    # ==========================================================================
    print("  [string_list] list of strings passes")
    _run(my_predbat, {"notify_devices": ["mobile_app_phone", "mobile_app_tablet"]}, expect_clean=["notify_devices"])

    print("  [string_list] list with integer element fails")
    _run(my_predbat, {"notify_devices": [123]}, expect_errors=["notify_devices"])

    print("  [string_list] integer value fails (get_arg wraps it but item is not a string)")
    _run(my_predbat, {"notify_devices": 42}, expect_errors=["notify_devices"])

    # ==========================================================================
    # STRING with allowed list  (threads: {"type": "string|integer", "allowed": [...]})
    # ==========================================================================
    print("  [string|integer allowed] 'auto' string passes")
    _run(my_predbat, {"threads": "auto"}, expect_clean=["threads"])

    print("  [string|integer allowed] integer 4 passes")
    _run(my_predbat, {"threads": 4}, expect_clean=["threads"])

    print("  [string|integer allowed] string not in allowed list fails")
    _run(my_predbat, {"threads": "one_hundred"}, expect_errors=["threads"])

    # ==========================================================================
    # INTEGER type  (db_days: {"type": "integer"})
    # ==========================================================================
    print("  [integer] integer value passes")
    _run(my_predbat, {"db_days": 30}, expect_clean=["db_days"])

    print("  [integer] non-numeric string fails")
    _run(my_predbat, {"db_days": "thirty"}, expect_errors=["db_days"])

    print("  [integer] float fails (validator requires isinstance int)")
    _run(my_predbat, {"db_days": 7.5}, expect_errors=["db_days"])

    # zero:False constraint  (gateway_mqtt_port: {"type": "integer", "zero": False})
    print("  [integer zero:False] non-zero integer passes")
    _run(my_predbat, {"gateway_mqtt_port": 1883}, expect_clean=["gateway_mqtt_port"])

    print("  [integer zero:False] zero fails")
    _run(my_predbat, {"gateway_mqtt_port": 0}, expect_errors=["gateway_mqtt_port"])

    # ==========================================================================
    # INTEGER_LIST type  (days_previous: {"type": "integer_list"})
    # ==========================================================================
    print("  [integer_list] list of integers passes")
    _run(my_predbat, {"days_previous": [7, 14, 28]}, expect_clean=["days_previous"])

    print("  [integer_list] list with non-integer string element fails")
    _run(my_predbat, {"days_previous": ["seven"]}, expect_errors=["days_previous"])

    # ==========================================================================
    # FLOAT type  (import_export_scaling: {"type": "float"})
    # ==========================================================================
    print("  [float] float value passes")
    _run(my_predbat, {"import_export_scaling": 1.05}, expect_clean=["import_export_scaling"])

    print("  [float] integer value also passes (int is a valid float)")
    _run(my_predbat, {"import_export_scaling": 1}, expect_clean=["import_export_scaling"])

    print("  [float] non-numeric string fails")
    _run(my_predbat, {"import_export_scaling": "lots"}, expect_errors=["import_export_scaling"])

    # ==========================================================================
    # FLOAT_LIST type  (days_previous_weight: {"type": "float_list"})
    # ==========================================================================
    print("  [float_list] list of floats passes")
    _run(my_predbat, {"days_previous_weight": [1.0, 0.5, 0.25]}, expect_clean=["days_previous_weight"])

    print("  [float_list] list with non-numeric element fails")
    _run(my_predbat, {"days_previous_weight": ["heavy"]}, expect_errors=["days_previous_weight"])

    # ==========================================================================
    # BOOLEAN type  (db_enable: {"type": "boolean"})
    # ==========================================================================
    print("  [boolean] True passes")
    _run(my_predbat, {"db_enable": True}, expect_clean=["db_enable"])

    print("  [boolean] False passes")
    _run(my_predbat, {"db_enable": False}, expect_clean=["db_enable"])

    print("  [boolean] 'on' string passes")
    _run(my_predbat, {"db_enable": "on"}, expect_clean=["db_enable"])

    print("  [boolean] 'off' string passes")
    _run(my_predbat, {"db_enable": "off"}, expect_clean=["db_enable"])

    # ==========================================================================
    # DICT type  (alerts: {"type": "dict"})
    # ==========================================================================
    print("  [dict] dict value passes")
    _run(my_predbat, {"alerts": {"low_soc": 10}}, expect_clean=["alerts"])

    print("  [dict] string value fails")
    _run(my_predbat, {"alerts": "not_a_dict"}, expect_errors=["alerts"])

    print("  [dict] list of non-dict items fails")
    _run(my_predbat, {"alerts": ["not_a_dict"]}, expect_errors=["alerts"])

    # ==========================================================================
    # DICT_LIST type  (rates_import: {"type": "dict_list"})
    # ==========================================================================
    print("  [dict_list] list of dicts passes")
    _run(my_predbat, {"rates_import": [{"start": "00:00", "end": "05:00", "rate": 0.07}]}, expect_clean=["rates_import"])

    print("  [dict_list] list with string element fails")
    _run(my_predbat, {"rates_import": ["not_a_dict"]}, expect_errors=["rates_import"])

    # ==========================================================================
    # INT_FLOAT_DICT type  (battery_charge_power_curve: {"type": "int_float_dict", "or_auto": True})
    # ==========================================================================
    print("  [int_float_dict] valid {int: float} dict passes")
    _run(my_predbat, {"battery_charge_power_curve": {0: 0.5, 50: 0.85, 100: 1.0}}, expect_clean=["battery_charge_power_curve"])

    print("  [int_float_dict or_auto] 'auto' string passes")
    _run(my_predbat, {"battery_charge_power_curve": "auto"}, expect_clean=["battery_charge_power_curve"])

    print("  [int_float_dict] non-integer key fails")
    _run(my_predbat, {"battery_charge_power_curve": {"high": 1.0}}, expect_errors=["battery_charge_power_curve"])

    print("  [int_float_dict] non-float value fails")
    _run(my_predbat, {"battery_charge_power_curve": {50: "high"}}, expect_errors=["battery_charge_power_curve"])

    # without or_auto  (battery_charge_power_curve_default: {"type": "int_float_dict"})
    print("  [int_float_dict no or_auto] 'auto' string fails")
    _run(my_predbat, {"battery_charge_power_curve_default": "auto"}, expect_errors=["battery_charge_power_curve_default"])

    # ==========================================================================
    # STRING_LIST with entries constraint  (givtcp_rest: {"type": "string_list", "entries": "num_inverters"})
    # ==========================================================================
    print("  [string_list entries] list length matches num_inverters passes")
    _run(my_predbat, {"givtcp_rest": ["http://192.168.1.100"], "num_inverters": 1}, expect_clean=["givtcp_rest"])

    print("  [string_list entries] list shorter than num_inverters fails")
    _run(my_predbat, {"givtcp_rest": ["http://192.168.1.100"], "num_inverters": 2}, expect_errors=["givtcp_rest"])

    print("  [string_list entries] list longer than num_inverters is auto-trimmed (passes)")
    _run(
        my_predbat,
        {"givtcp_rest": ["http://192.168.1.100", "http://192.168.1.101"], "num_inverters": 1},
        expect_clean=["givtcp_rest"],
    )

    # ==========================================================================
    # SENSOR type — single entity  (pv_forecast_today: {"type": "sensor", "sensor_type": "float"})
    # ==========================================================================
    print("  [sensor float] entity with float state passes")
    _run(
        my_predbat,
        {"pv_forecast_today": "sensor.test_solar_today"},
        extra_states={"sensor.test_solar_today": 3.5},
        expect_clean=["pv_forecast_today"],
    )

    print("  [sensor float] entity with integer state passes (int is float-compatible)")
    _run(
        my_predbat,
        {"pv_forecast_today": "sensor.test_solar_today"},
        extra_states={"sensor.test_solar_today": 4},
        expect_clean=["pv_forecast_today"],
    )

    print("  [sensor float] entity with string state fails")
    _run(
        my_predbat,
        {"pv_forecast_today": "sensor.test_solar_today"},
        extra_states={"sensor.test_solar_today": "unknown"},
        expect_errors=["pv_forecast_today"],
    )

    print("  [sensor float] entity returning None fails (no 'none' in sensor_type)")
    _run(my_predbat, {"pv_forecast_today": "sensor.test_entity_missing_xyz"}, expect_errors=["pv_forecast_today"])

    print("  [sensor] entity_id without a dot fails")
    _run(my_predbat, {"pv_forecast_today": "no_dot_entity"}, expect_errors=["pv_forecast_today"])

    # ==========================================================================
    # SENSOR_LIST with sensor_type "none|string"
    # (pause_start_time: {"type": "sensor_list", "sensor_type": "none|string",
    #                      "modify": True, "entries": "num_inverters"})
    # ==========================================================================
    print("  [sensor_list none|string] entity returning None passes (none in sensor_type)")
    _run(
        my_predbat,
        {"pause_start_time": ["input_number.test_pause_start"], "num_inverters": 1},
        extra_states={"input_number.test_pause_start": None},
        expect_clean=["pause_start_time"],
    )

    print("  [sensor_list none|string] entity returning string passes")
    _run(
        my_predbat,
        {"pause_start_time": ["input_number.test_pause_start"], "num_inverters": 1},
        extra_states={"input_number.test_pause_start": "09:00"},
        expect_clean=["pause_start_time"],
    )

    # ==========================================================================
    # SENSOR_LIST with modify constraint
    # (charge_rate: {"type": "sensor_list", "sensor_type": "float",
    #                "modify": True, "entries": "num_inverters"})
    # ==========================================================================
    print("  [sensor_list modify] number. prefix is allowed for modification")
    _run(
        my_predbat,
        {"charge_rate": ["number.test_charge_rate"], "num_inverters": 1},
        extra_states={"number.test_charge_rate": 2.5},
        expect_clean=["charge_rate"],
    )

    print("  [sensor_list modify] sensor. prefix (non-predbat) fails modification check")
    _run(
        my_predbat,
        {"charge_rate": ["sensor.test_charge_rate"], "num_inverters": 1},
        extra_states={"sensor.test_charge_rate": 2.5},
        expect_errors=["charge_rate"],
    )

    print("  [sensor_list modify] sensor.predbat_ prefix is exempt from modify restriction")
    _run(
        my_predbat,
        {"charge_rate": ["sensor.predbat_charge_rate"], "num_inverters": 1},
        extra_states={"sensor.predbat_charge_rate": 2.5},
        expect_clean=["charge_rate"],
    )

    print("  [sensor_list modify] select. prefix is allowed for modification")
    _run(
        my_predbat,
        {"charge_rate": ["select.test_charge_rate"], "num_inverters": 1},
        extra_states={"select.test_charge_rate": 2.5},
        expect_clean=["charge_rate"],
    )

    # ==========================================================================
    # SENSOR_LIST boolean sensor_type — gateway EVC fields
    # (car_charging_planned: {"type": "sensor|sensor_list",
    #                          "sensor_type": "string|boolean", "entries": "num_cars"})
    # ==========================================================================
    print("  [sensor boolean] entity returning bool True passes")
    _run(
        my_predbat,
        {"car_charging_planned": ["binary_sensor.test_ev_connected"], "num_cars": 1},
        extra_states={"binary_sensor.test_ev_connected": True},
        expect_clean=["car_charging_planned"],
    )

    print("  [sensor boolean] entity returning bool False passes")
    _run(
        my_predbat,
        {"car_charging_planned": ["binary_sensor.test_ev_connected"], "num_cars": 1},
        extra_states={"binary_sensor.test_ev_connected": False},
        expect_clean=["car_charging_planned"],
    )

    print("  [sensor boolean] entity returning string 'on' still passes (string|boolean)")
    _run(
        my_predbat,
        {"car_charging_planned": ["binary_sensor.test_ev_connected"], "num_cars": 1},
        extra_states={"binary_sensor.test_ev_connected": "on"},
        expect_clean=["car_charging_planned"],
    )

    print("  [sensor boolean] car_charging_now with bool False passes")
    _run(
        my_predbat,
        {"car_charging_now": ["binary_sensor.test_ev_session"], "num_cars": 1},
        extra_states={"binary_sensor.test_ev_session": False},
        expect_clean=["car_charging_now"],
    )

    print("  [sensor boolean] car_charging_now with string 'off' passes")
    _run(
        my_predbat,
        {"car_charging_now": ["binary_sensor.test_ev_session"], "num_cars": 1},
        extra_states={"binary_sensor.test_ev_session": "off"},
        expect_clean=["car_charging_now"],
    )

    # ==========================================================================
    # transient_ok  (car_charging_energy, car_charging_power)
    #
    # An EV charger routinely reports 'unavailable' or 'unknown' with nothing
    # plugged into it. minute_data() already skips those samples (utils.py), and
    # update_car_charging_power() reads them as zero, so the reading being absent
    # right now is normal rather than a misconfiguration - but validate_config
    # reads the same entity by its own path and required the state to parse as a
    # float, leaving the whole run reporting errors for a car sitting on the
    # driveway. transient_ok on those two schema entries allows the placeholder
    # states without loosening anything else.
    # ==========================================================================
    for name in ("car_charging_energy", "car_charging_power"):
        print(f"  [transient_ok] {name} reading 'unavailable' passes")
        _run(my_predbat, {name: "sensor.test_charger_energy"}, extra_states={"sensor.test_charger_energy": "unavailable"}, expect_clean=[name])

        print(f"  [transient_ok] {name} reading 'unknown' passes")
        _run(my_predbat, {name: "sensor.test_charger_energy"}, extra_states={"sensor.test_charger_energy": "unknown"}, expect_clean=[name])

        print(f"  [transient_ok] {name} reading a real number still passes")
        _run(my_predbat, {name: "sensor.test_charger_energy"}, extra_states={"sensor.test_charger_energy": 4.2}, expect_clean=[name])

        print(f"  [transient_ok] {name} reading a non-numeric value still fails")
        _run(my_predbat, {name: "sensor.test_charger_energy"}, extra_states={"sensor.test_charger_energy": "banana"}, expect_errors=[name])

        print(f"  [transient_ok] {name} pointing at an entity that does not exist still fails")
        _run(my_predbat, {name: "sensor.test_charger_typo"}, expect_errors=[name])

    print("**** test_validate_config PASSED ****")
    return False


def test_validate_config_secrets(my_predbat):
    """
    Tests check_apps_yaml_secrets() (#4787) - re-reads apps.yaml with the ruamel round-trip
    loader and flags credential-like values that are stored in plain text rather than
    referenced via the '!secret' mechanism.

    Writes a real temp apps.yaml so the round-trip loader has something genuine to parse:
    provenance (whether a value came from a '!secret' tag or was written inline) only
    survives in the raw file, not in self.args, which is why the check has to re-read it
    rather than working off the already-resolved config the rest of validate_config() uses.
    check_apps_yaml_secrets() takes an explicit path so this is independent of
    PREDBAT_APPS_FILE and the working directory the test suite happens to run from.
    """
    print("**** test_validate_config_secrets ****")

    apps_yaml_content = """
pred_bat:
  module: predbat
  class: PredBat
  mcp_secret: !secret my_mcp_secret
  ohme_password: plaintext_password
  kraken_key: ""
  forecast_solar:
    api_key: plaintext_nested_key
  gateway_mqtt_host: mqtt.example.com
  rates_import:
    - start: "00:00"
      end: "05:00"
      rate: 0.07
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        temp_path = f.name
        f.write(apps_yaml_content)

    try:
        my_predbat.check_apps_yaml_secrets(apps_yaml_path=temp_path)
        warnings = my_predbat.arg_warnings

        print("  Inline credential value is flagged")
        assert "ohme_password" in warnings, f"Expected inline ohme_password to be flagged, got {warnings}"

        print("  Nested inline credential value is flagged")
        assert "forecast_solar.api_key" in warnings, f"Expected nested forecast_solar.api_key to be flagged, got {warnings}"

        print("  '!secret'-referenced value is not flagged")
        assert "mcp_secret" not in warnings, f"'!secret'-referenced mcp_secret must not be flagged, got {warnings}"

        print("  Empty credential-like value is not flagged")
        assert "kraken_key" not in warnings, f"Empty string value must not be flagged, got {warnings}"

        print("  Non-credential key is not flagged")
        assert "gateway_mqtt_host" not in warnings, f"Non-credential key must not be flagged, got {warnings}"
        assert not any(w.startswith("rates_import") for w in warnings), f"Non-credential nested list must not be flagged, got {warnings}"
    finally:
        os.remove(temp_path)

    print("  A missing apps.yaml is handled without error")
    my_predbat.check_apps_yaml_secrets(apps_yaml_path="/tmp/does_not_exist_predbat_test_4787.yaml")
    assert my_predbat.arg_warnings == {}, f"Expected no warnings for a missing file, got {my_predbat.arg_warnings}"

    print("**** test_validate_config_secrets PASSED ****")
    return False


def test_validate_config_retry(my_predbat):
    """
    Tests validate_config_schedule_retry()/validate_config_check_retry() - the retry mechanism
    added for #4379 so a validation failure that self-heals on its own (e.g. a slow-starting
    integration's sensor not populated yet) clears its own error status without needing a
    manual restart, instead of sitting stale until the next restart/config change.

    Stubs validate_config() itself with a controlled sequence of results rather than relying on
    real apps.yaml validation reaching a clean state - the test fixture's own baseline args
    already carry pre-existing validation warnings unrelated to this feature, so "clean" can't
    be reached just by fixing one deliberately-broken field. This isolates the retry-scheduling
    logic under test from that ambient noise.
    """
    from datetime import timedelta

    print("**** test_validate_config_retry ****")

    saved_args = my_predbat.args.copy()
    saved_retries_remaining = my_predbat.validate_config_retries_remaining
    saved_next_retry_time = my_predbat.validate_config_next_retry_time
    saved_now_utc = my_predbat.now_utc
    saved_validate_config = my_predbat.validate_config

    def _should_not_be_called():
        raise AssertionError("validate_config() should not have been called here")

    try:
        # A clean validation should never arm a retry sequence
        my_predbat.validate_config_retries_remaining = 0
        my_predbat.validate_config_next_retry_time = None
        my_predbat.validate_config_schedule_retry(0)
        assert my_predbat.validate_config_retries_remaining == 0, "Clean validation should not arm a retry"
        assert my_predbat.validate_config_next_retry_time is None

        # A failing validation arms the default (2 retries, 1 minute)
        my_predbat.args.pop("validate_config_retries", None)
        my_predbat.args.pop("validate_config_retry_minutes", None)
        my_predbat.validate_config_schedule_retry(1)
        assert my_predbat.validate_config_retries_remaining == 2, f"Expected 2 retries armed by default, got {my_predbat.validate_config_retries_remaining}"
        assert my_predbat.validate_config_next_retry_time == my_predbat.now_utc + timedelta(minutes=1)

        # check_retry() is a no-op before the retry time is due - must not even call validate_config()
        my_predbat.validate_config = _should_not_be_called
        my_predbat.validate_config_check_retry()
        assert my_predbat.validate_config_retries_remaining == 2, "Should not have retried before the due time"

        # Once due, a still-failing re-validation decrements the counter and reschedules
        my_predbat.now_utc = saved_now_utc + timedelta(minutes=1)
        my_predbat.validate_config = lambda: 1  # simulate validation still failing
        my_predbat.validate_config_check_retry()
        assert my_predbat.validate_config_retries_remaining == 1, f"Expected 1 retry remaining, got {my_predbat.validate_config_retries_remaining}"
        assert my_predbat.validate_config_next_retry_time == my_predbat.now_utc + timedelta(minutes=1)

        # Exhausting the final retry while still failing stops the sequence cleanly
        my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=1)
        my_predbat.validate_config_check_retry()
        assert my_predbat.validate_config_retries_remaining == 0, "Should give up after the last retry"
        assert my_predbat.validate_config_next_retry_time is None

        # No further retries happen once the sequence has stopped, however much time passes
        my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=10)
        my_predbat.validate_config = _should_not_be_called
        my_predbat.validate_config_check_retry()
        assert my_predbat.validate_config_retries_remaining == 0

        # A retry that succeeds clears the sequence immediately, not just decrements it
        my_predbat.validate_config = lambda: 1
        my_predbat.validate_config_schedule_retry(1)
        assert my_predbat.validate_config_retries_remaining == 2
        my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=1)
        my_predbat.validate_config = lambda: 0  # simulate the underlying issue having self-healed
        my_predbat.validate_config_check_retry()
        assert my_predbat.validate_config_retries_remaining == 0, "A successful retry should clear the sequence, not just decrement it"
        assert my_predbat.validate_config_next_retry_time is None

        # validate_config_retries: 0 disables the feature entirely (and cancels any armed retry sequence)
        my_predbat.validate_config_retries_remaining = 2
        my_predbat.validate_config_next_retry_time = my_predbat.now_utc + timedelta(minutes=1)
        my_predbat.args["validate_config_retries"] = 0
        my_predbat.validate_config_schedule_retry(1)
        assert my_predbat.validate_config_retries_remaining == 0, "validate_config_retries=0 should disable retries"
        assert my_predbat.validate_config_next_retry_time is None
        # A custom retry count/interval is respected
        my_predbat.args["validate_config_retries"] = 5
        my_predbat.args["validate_config_retry_minutes"] = 3
        my_predbat.validate_config_schedule_retry(1)
        assert my_predbat.validate_config_retries_remaining == 5
        assert my_predbat.validate_config_next_retry_time == my_predbat.now_utc + timedelta(minutes=3)

        print("**** test_validate_config_retry PASSED ****")
        return False
    finally:
        my_predbat.args = saved_args
        my_predbat.validate_config = saved_validate_config
        my_predbat.validate_config_retries_remaining = saved_retries_remaining
        my_predbat.validate_config_next_retry_time = saved_next_retry_time
        my_predbat.now_utc = saved_now_utc
