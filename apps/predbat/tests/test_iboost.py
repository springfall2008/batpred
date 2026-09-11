# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import reset_rates2, reset_inverter


def run_iboost_smart_test(test_name, my_predbat, today=0, max_energy=1, max_power=1, min_length=0, expect_cost=0, expect_kwh=0, expect_time=0):
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    my_predbat.iboost_smart = True
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = today
    my_predbat.iboost_max_energy = max_energy
    my_predbat.iboost_max_power = max_power / 60
    my_predbat.iboost_smart_min_length = min_length

    slots = my_predbat.plan_iboost_smart()
    total_kwh = 0
    total_cost = 0
    total_time = 0
    for slot in slots:
        total_kwh += slot["kwh"]
        total_cost += slot["cost"]
        total_time += slot["end"] - slot["start"]
    if total_time != expect_time:
        print("ERROR: Iboost total time should be {} got {}".format(expect_time, total_time))
        print(slots)
        failed = True
    if total_kwh != expect_kwh:
        print("ERROR: Iboost total kwh should be {} got {}".format(expect_kwh, total_kwh))
        print(slots)
        failed = True
    if total_cost != expect_cost:
        print(slots)
        print("ERROR: Iboost total cost should be {} got {}".format(expect_cost, total_cost))
        failed = True

    my_predbat.iboost_smart = False
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0

    return failed


def run_iboost_smart_average_test(my_predbat):
    """
    Regression test for plan_iboost_smart() averaging the rate across every sub-slot of an
    iboost_smart_min_length window, rather than repeating the rate at the window's start minute (GH#4817)
    """
    failed = False
    reset_inverter(my_predbat)

    orig_minutes_now = my_predbat.minutes_now
    orig_forecast_minutes = my_predbat.forecast_minutes
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 120

    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)

    my_predbat.iboost_smart = True
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0
    my_predbat.iboost_max_energy = 2
    my_predbat.iboost_max_power = 2 / 60
    my_predbat.iboost_smart_min_length = 60

    slots = my_predbat.plan_iboost_smart()

    total_kwh = 0
    total_cost = 0
    total_time = 0
    for slot in slots:
        total_kwh += slot["kwh"]
        total_cost += slot["cost"]
        total_time += slot["end"] - slot["start"]

    # The first window (minutes 0-60) is half an hour at import_rate and half an hour at import_rate * 2,
    # so its true average is import_rate * 1.5. The bug repeated the rate at the window's start minute
    # (import_rate) for every sub-slot instead, under-pricing the window.
    expect_kwh = 2
    expect_time = 60
    expect_cost = import_rate * 1.5 * expect_kwh

    if total_time != expect_time:
        print("ERROR: Iboost average test total time should be {} got {}".format(expect_time, total_time))
        print(slots)
        failed = True
    if total_kwh != expect_kwh:
        print("ERROR: Iboost average test total kwh should be {} got {}".format(expect_kwh, total_kwh))
        print(slots)
        failed = True
    if total_cost != expect_cost:
        print("ERROR: Iboost average test total cost should be {} got {} (rate averaging across the min-length window is broken)".format(expect_cost, total_cost))
        print(slots)
        failed = True

    my_predbat.iboost_smart = False
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0
    my_predbat.iboost_max_energy = my_predbat.get_arg("iboost_max_energy")
    my_predbat.iboost_max_power = my_predbat.get_arg("iboost_max_power") / (60 * 1000)
    my_predbat.iboost_smart_min_length = my_predbat.get_arg("iboost_smart_min_length")
    my_predbat.minutes_now = orig_minutes_now
    my_predbat.forecast_minutes = orig_forecast_minutes

    return failed


def run_iboost_smart_tests(my_predbat):
    """
    Test for Iboost smart
    """
    failed = False
    reset_inverter(my_predbat)

    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)
    my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)

    failed |= run_iboost_smart_test("iboost1", my_predbat, today=0, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * 5 * 2, expect_kwh=5 * 2, expect_time=5 * 2 * 60)
    failed |= run_iboost_smart_test("iboost2", my_predbat, today=4.9, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * (0.1 + 5), expect_kwh=(0.1 + 5), expect_time=10 + 5 * 60)
    failed |= run_iboost_smart_test("iboost3", my_predbat, today=4.95, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * (0.05 + 5), expect_kwh=(0.05 + 5), expect_time=5 + 5 * 60)
    failed |= run_iboost_smart_average_test(my_predbat)

    return failed
