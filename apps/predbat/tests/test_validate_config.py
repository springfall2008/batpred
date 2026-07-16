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

    print("**** test_validate_config PASSED ****")
    return False
