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

from annual import AnnualNullHA, apply_hardware, reset_sample_state, write_minimal_apps_yaml
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
    if my_predbat.inverter_hybrid is not True:
        print("  ERROR: inverter_hybrid should be True")
        failed = True

    print("Test: apply_hardware with no battery produces a zero-capacity system")
    apply_hardware(my_predbat, None, [{"kwp": 5.6}])
    if my_predbat.soc_max != 0.0 or my_predbat.soc_kw != 0.0:
        print("  ERROR: a battery-less run should have soc_max and soc_kw of 0, got {} / {}".format(my_predbat.soc_max, my_predbat.soc_kw))
        failed = True

    print("Test: reset_sample_state clears every field a previous sample could have left behind")
    my_predbat.dynamic_load_baseline = {5: 1.0}
    my_predbat.battery_rate_max_export = 99.0
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

    reset_sample_state(my_predbat)

    checks = [
        ("dynamic_load_baseline", {}),
        ("battery_rate_max_export", 0.0333),
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
    ]
    for name, expected in checks:
        actual = getattr(my_predbat, name)
        if actual != expected:
            print("  ERROR: reset_sample_state left {} as {}, expected {}".format(name, actual, expected))
            failed = True

    print("Test: reset_sample_state disables the live-system behaviours that make no sense offline")
    if my_predbat.octopus_intelligent_charging is not False:
        print("  ERROR: octopus_intelligent_charging should be disabled")
        failed = True
    if my_predbat.load_forecast_only is not True:
        print("  ERROR: load_forecast_only must be True so the load profile is taken from load_forecast")
        failed = True

    return failed
