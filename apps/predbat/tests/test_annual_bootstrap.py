# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction headless bootstrap and state reset."""

import os
import tempfile

import yaml

from annual import DEFAULT_CAR_RATE_KW, AnnualNullHA, apply_hardware, configure_offline_mode, create_headless_predbat, reset_sample_state, write_minimal_apps_yaml
from const import MINUTE_WATT


def test_annual_bootstrap(my_predbat):
    """Verify the minimal apps.yaml, the null HA interface, hardware mapping and state reset."""
    failed = False
    print("**** Testing annual bootstrap ****")

    print("Test: write_minimal_apps_yaml produces a parseable pred_bat config")
    with tempfile.TemporaryDirectory() as work_dir:
        path = write_minimal_apps_yaml(work_dir, "Europe/London")
        if not os.path.exists(path):
            print("  ERROR: apps.yaml was not written to {}".format(path))
            failed = True
        with open(path, "r") as handle:
            parsed = yaml.safe_load(handle)
        if "pred_bat" not in parsed:
            print("  ERROR: the written apps.yaml has no pred_bat key")
            failed = True
        else:
            section = parsed["pred_bat"]
            for key in ["module", "class", "prefix", "timezone", "currency_symbols", "threads"]:
                if key not in section:
                    print("  ERROR: the written apps.yaml is missing '{}'".format(key))
                    failed = True
            if section.get("threads") != 0:
                print("  ERROR: threads must be 0 so plan runs are deterministic, got {}".format(section.get("threads")))
                failed = True
            if section.get("timezone") != "Europe/London":
                print("  ERROR: the timezone should be written through, got {}".format(section.get("timezone")))
                failed = True

    print("Test: AnnualNullHA satisfies the interface PredBat calls without a Home Assistant")
    null_ha = AnnualNullHA()
    if null_ha.get_state("sensor.anything", default=7) != 7:
        print("  ERROR: get_state should return the supplied default")
        failed = True
    if null_ha.get_state(None) != {}:
        print("  ERROR: get_state with no entity should return an empty mapping of all states")
        failed = True
    if null_ha.get_history("sensor.anything") is not None:
        print("  ERROR: get_history should return None when no history exists")
        failed = True
    null_ha.set_state("sensor.written", "5", attributes={"unit": "kWh"})
    if null_ha.get_state("sensor.written") != "5":
        print("  ERROR: set_state then get_state should round trip")
        failed = True
    if null_ha.call_service("some/service", value=1) is not None:
        print("  ERROR: call_service should be a no-op returning None")
        failed = True

    print("Test: apply_hardware maps the battery block onto PredBat's internal units")
    battery = {"size_kwh": 9.5, "inverter_kw": 5.0, "export_limit_kw": 3.6, "hybrid": True, "charge_rate_kw": 3.7, "discharge_rate_kw": 4.2}
    apply_hardware(my_predbat, battery, [{"kwp": 5.6}])
    if abs(my_predbat.soc_max - 9.5) > 1e-9:
        print("  ERROR: soc_max expected 9.5, got {}".format(my_predbat.soc_max))
        failed = True
    if abs(my_predbat.soc_kw - 9.5) > 1e-9:
        print("  ERROR: soc_kw should be set deterministically to soc_max, got {}".format(my_predbat.soc_kw))
        failed = True
    if abs(my_predbat.inverter_limit - (5.0 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: inverter_limit should be in kW per minute, got {}".format(my_predbat.inverter_limit))
        failed = True
    if abs(my_predbat.export_limit - (3.6 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: export_limit should be in kW per minute, got {}".format(my_predbat.export_limit))
        failed = True
    if abs(my_predbat.battery_rate_max_charge - (3.7 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: battery_rate_max_charge should be in kW per minute, got {}".format(my_predbat.battery_rate_max_charge))
        failed = True
    if abs(my_predbat.battery_rate_max_discharge - (4.2 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: battery_rate_max_discharge should be in kW per minute, got {}".format(my_predbat.battery_rate_max_discharge))
        failed = True
    if abs(my_predbat.battery_rate_max_export - (4.2 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: battery_rate_max_export should equal the discharge rate, got {}".format(my_predbat.battery_rate_max_export))
        failed = True
    if my_predbat.inverter_hybrid is not True:
        print("  ERROR: inverter_hybrid should be True")
        failed = True

    print("Test: apply_hardware with no battery produces a zero-capacity system")
    apply_hardware(my_predbat, None, [{"kwp": 5.6}])
    if my_predbat.soc_max != 0.0 or my_predbat.soc_kw != 0.0:
        print("  ERROR: a battery-less run should have soc_max and soc_kw of 0, got {} / {}".format(my_predbat.soc_max, my_predbat.soc_kw))
        failed = True
    if my_predbat.battery_rate_max_charge != 0.0 or my_predbat.battery_rate_max_charge_dc != 0.0 or my_predbat.battery_rate_max_discharge != 0.0 or my_predbat.battery_rate_max_export != 0.0:
        print("  ERROR: a battery-less run should zero every rate field")
        failed = True
    if my_predbat.battery_rate_min != 0.0:
        print("  ERROR: a zero-capacity battery should have battery_rate_min of 0, got {}".format(my_predbat.battery_rate_min))
        failed = True
    if my_predbat.export_limit != my_predbat.inverter_limit:
        print("  ERROR: export_limit should match inverter_limit when there is no battery")
        failed = True
    if my_predbat.inverter_hybrid is not False:
        print("  ERROR: a battery-less run should not be a hybrid inverter")
        failed = True

    print("Test: apply_hardware with no battery sums kwp across every solar array, not just the first")
    apply_hardware(my_predbat, None, [{"kwp": 5.6}, {"kwp": 4.0}])
    expected_limit = (5.6 + 4.0) * 1000 / MINUTE_WATT
    if abs(my_predbat.inverter_limit - expected_limit) > 1e-9:
        print("  ERROR: inverter_limit should sum kwp across all arrays, expected {}, got {}".format(expected_limit, my_predbat.inverter_limit))
        failed = True
    if abs(my_predbat.export_limit - expected_limit) > 1e-9:
        print("  ERROR: export_limit should track the summed inverter_limit, got {}".format(my_predbat.export_limit))
        failed = True

    print("Test: reset_sample_state clears the leaked fields but leaves apply_hardware's output alone")
    apply_hardware(my_predbat, battery, [{"kwp": 5.6}])
    configured_battery_rate_max_export = my_predbat.battery_rate_max_export
    configured_inverter_limit = my_predbat.inverter_limit

    my_predbat.dynamic_load_baseline = {5: 1.0}
    my_predbat.soc_kw = 3.3
    my_predbat.manual_charge_times = [1, 2, 3]
    my_predbat.manual_export_times = [4]
    my_predbat.manual_all_times = [5]
    my_predbat.cost_today_sofar = 123.0
    my_predbat.import_today_now = 4.0
    my_predbat.export_today_now = 5.0
    my_predbat.iboost_today = 6.0
    my_predbat.carbon_today_sofar = 7.0
    my_predbat.load_minutes_now = 8.0
    my_predbat.pv_today_now = 9.0
    my_predbat.charge_limit_best = [1.0]
    my_predbat.charge_window_best = [{"start": 0, "end": 30}]
    my_predbat.export_window_best = [{"start": 0, "end": 30}]
    my_predbat.export_limits_best = [50.0]
    my_predbat.plan_valid = True
    my_predbat.low_rates = [{"start": 0, "end": 30}]
    my_predbat.high_export_rates = [{"start": 0, "end": 30}]
    my_predbat.rate_import = {0: 25.0}
    my_predbat.rate_export = {0: 15.0}
    my_predbat.rate_import_replicated = {0: 25.0}
    my_predbat.rate_export_replicated = {0: 15.0}
    my_predbat.rate_import_cost_threshold = 1.0
    my_predbat.rate_export_cost_threshold = 2.0
    my_predbat.rate_min = 5.0
    my_predbat.rate_max = 6.0
    my_predbat.rate_average = 7.0

    # Scenario 3's smart-car overrides (_run_scenarios() in annual.py), leaked here the way a
    # previous sample's with-car leg would leave them.
    my_predbat.car_charging_planned = [True]
    my_predbat.car_charging_limit = [20.0]
    my_predbat.car_charging_soc = [10.0]
    my_predbat.car_charging_rate = [22.0]
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.car_charging_plan_smart = [True]
    my_predbat.car_charging_from_battery = True

    reset_sample_state(my_predbat)

    checks = [
        ("dynamic_load_baseline", {}),
        ("soc_kw", 0.0),
        ("manual_charge_times", []),
        ("manual_export_times", []),
        ("manual_all_times", []),
        ("cost_today_sofar", 0),
        ("import_today_now", 0),
        ("export_today_now", 0),
        ("iboost_today", 0),
        ("carbon_today_sofar", 0),
        ("load_minutes_now", 0),
        ("pv_today_now", 0),
        ("charge_limit_best", []),
        ("charge_window_best", []),
        ("export_window_best", []),
        ("export_limits_best", []),
        ("plan_valid", False),
        ("low_rates", []),
        ("high_export_rates", []),
        ("rate_import", {}),
        ("rate_export", {}),
        ("rate_import_replicated", {}),
        ("rate_export_replicated", {}),
        ("rate_import_cost_threshold", 99),
        ("rate_export_cost_threshold", 99),
        ("rate_min", 0),
        ("rate_max", 0),
        ("rate_average", 0),
        ("car_charging_planned", [False]),
        ("car_charging_limit", [0.0]),
        ("car_charging_soc", [0.0]),
        ("car_charging_rate", [DEFAULT_CAR_RATE_KW]),
        ("car_charging_battery_size", [50.0]),
        ("car_charging_plan_smart", [False]),
        ("car_charging_from_battery", False),
    ]
    for name, expected in checks:
        actual = getattr(my_predbat, name)
        if actual != expected:
            print("  ERROR: reset_sample_state left {} as {}, expected {}".format(name, actual, expected))
            failed = True

    print("Test: reset_sample_state must not clobber the hardware-derived fields apply_hardware owns")
    if my_predbat.battery_rate_max_export != configured_battery_rate_max_export:
        print("  ERROR: reset_sample_state changed battery_rate_max_export from {} to {}; it must be apply_hardware's alone".format(configured_battery_rate_max_export, my_predbat.battery_rate_max_export))
        failed = True
    if my_predbat.inverter_limit != configured_inverter_limit:
        print("  ERROR: reset_sample_state changed inverter_limit from {} to {}; it must be apply_hardware's alone".format(configured_inverter_limit, my_predbat.inverter_limit))
        failed = True

    print("Test: configure_offline_mode applies the deliberate offline choices, separately from reset_sample_state")
    my_predbat.octopus_intelligent_charging = True
    my_predbat.load_forecast_only = False
    my_predbat.load_scaling = 0.5
    my_predbat.load_scaling10 = 0.5
    my_predbat.iboost_enable = True
    my_predbat.carbon_enable = True
    my_predbat.plan_debug = True
    my_predbat.debug_enable = True
    my_predbat.calculate_best_charge = False
    my_predbat.calculate_best_export = False
    my_predbat.set_charge_window = False
    my_predbat.set_export_window = False

    configure_offline_mode(my_predbat)

    offline_checks = [
        ("octopus_intelligent_charging", False),
        ("load_forecast_only", True),
        ("load_scaling", 1.0),
        ("load_scaling10", 1.0),
        ("iboost_enable", False),
        ("carbon_enable", False),
        ("plan_debug", False),
        ("debug_enable", False),
        # Without these, calculate_plan()'s charge/export-window branches never fire (they are
        # gated on calculate_best_charge/set_charge_window and the export equivalent), and the
        # "with Predbat" scenario would silently plan no charging or exporting at all.
        ("calculate_best_charge", True),
        ("calculate_best_export", True),
        ("set_charge_window", True),
        ("set_export_window", True),
    ]
    for name, expected in offline_checks:
        actual = getattr(my_predbat, name)
        if actual != expected:
            print("  ERROR: configure_offline_mode left {} as {}, expected {}".format(name, actual, expected))
            failed = True

    print("Test: reset_sample_state must not re-apply configure_offline_mode's choices")
    # my_predbat is the shared suite fixture, so anything set here outlives this test.
    # debug_enable in particular must be put back: kernel_supported() in
    # prediction_kernel.py requires `not pred.debug_enable`, so leaving it True makes every
    # later prediction in the whole suite bypass the C++ kernel and fall back to the Python
    # engine - roughly 8x slower per plan, which looks like a hang in the slow tests rather
    # than a leak from here.
    previous_octopus_intelligent = my_predbat.octopus_intelligent_charging
    previous_debug_enable = my_predbat.debug_enable
    try:
        my_predbat.octopus_intelligent_charging = True
        my_predbat.debug_enable = True
        reset_sample_state(my_predbat)
        if my_predbat.octopus_intelligent_charging is not True:
            print("  ERROR: reset_sample_state should not touch octopus_intelligent_charging any more")
            failed = True
        if my_predbat.debug_enable is not True:
            print("  ERROR: reset_sample_state should not touch debug_enable any more")
            failed = True
    finally:
        my_predbat.octopus_intelligent_charging = previous_octopus_intelligent
        my_predbat.debug_enable = previous_debug_enable

    print("Test: create_headless_predbat leaves the planner switched on, not the Monitor-mode default")
    with tempfile.TemporaryDirectory() as work_dir:
        headless = create_headless_predbat(work_dir, "Europe/London", print)
    planner_checks = [
        ("calculate_best_charge", True),
        ("calculate_best_export", True),
        ("set_charge_window", True),
        ("set_export_window", True),
    ]
    for name, expected in planner_checks:
        actual = getattr(headless, name)
        if actual != expected:
            print("  ERROR: create_headless_predbat left {} as {}, expected {} (the headless apps.yaml has no 'mode' key, so fetch_config_options() defaults to Monitor mode, which sets all four False)".format(name, actual, expected))
            failed = True

    print("Test: create_headless_predbat restores PREDBAT_APPS_FILE afterwards, even if it was previously unset")
    saved_env = os.environ.get("PREDBAT_APPS_FILE")
    try:
        os.environ["PREDBAT_APPS_FILE"] = "/tmp/should-not-leak-annual-bootstrap-test.yaml"
        with tempfile.TemporaryDirectory() as work_dir:
            create_headless_predbat(work_dir, "Europe/London", print)
        if os.environ.get("PREDBAT_APPS_FILE") != "/tmp/should-not-leak-annual-bootstrap-test.yaml":
            print("  ERROR: PREDBAT_APPS_FILE was not restored to its prior value, got {}".format(os.environ.get("PREDBAT_APPS_FILE")))
            failed = True

        os.environ.pop("PREDBAT_APPS_FILE", None)
        with tempfile.TemporaryDirectory() as work_dir:
            create_headless_predbat(work_dir, "Europe/London", print)
        if "PREDBAT_APPS_FILE" in os.environ:
            print("  ERROR: PREDBAT_APPS_FILE should be unset again, got {}".format(os.environ.get("PREDBAT_APPS_FILE")))
            failed = True
    finally:
        if saved_env is None:
            os.environ.pop("PREDBAT_APPS_FILE", None)
        else:
            os.environ["PREDBAT_APPS_FILE"] = saved_env

    return failed
