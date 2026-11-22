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
from utils import in_iboost_slot


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


def run_iboost_smart_duplicate_slots_test(my_predbat):
    """
    Regression test for plan_iboost_smart() booking the same slot more than once when iboost_smart_min_length is
    longer than the plan interval (#6309). The windows then overlap, and the duplicate check tested a variable left
    over from an earlier loop, so it never fired. Each copy counted against iboost_max_energy, but the prediction
    (in_iboost_slot()) applies only the first, so the plan boosted less than the maximum.
    """
    failed = False
    print("**** Running Test: iboost_duplicate_slots ****")
    reset_inverter(my_predbat)

    orig_minutes_now = my_predbat.minutes_now
    orig_forecast_minutes = my_predbat.forecast_minutes
    orig_rate_import = my_predbat.rate_import
    orig_rate_export = my_predbat.rate_export
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 6 * 60

    # A flat rate, so every window costs the same and they are taken in time order, overlapping
    import_rate = 10.0
    my_predbat.rate_import = {minute: import_rate for minute in range(0, 48 * 60)}
    my_predbat.rate_export = {minute: 0 for minute in range(0, 48 * 60)}

    my_predbat.iboost_smart = True
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0
    my_predbat.iboost_max_energy = 3
    my_predbat.iboost_max_power = 1 / 60

    try:
        for min_length in [0, 120]:
            my_predbat.iboost_smart_min_length = min_length
            slots = my_predbat.plan_iboost_smart()
            starts = [slot["start"] for slot in slots]
            planned_kwh = round(sum(slot["kwh"] for slot in slots), 3)
            # The energy the prediction applies, minute by minute
            applied_kwh = round(sum(in_iboost_slot(minute, slots) for minute in range(0, my_predbat.forecast_minutes)) / 60, 3)
            if len(starts) != len(set(starts)):
                print("ERROR: iboost_duplicate_slots min_length {}: a slot was booked more than once: {}".format(min_length, slots))
                failed = True
            if planned_kwh != 3 or applied_kwh != 3:
                print("ERROR: iboost_duplicate_slots min_length {}: expected 3 kWh planned and applied, got {} planned and {} applied: {}".format(min_length, planned_kwh, applied_kwh, slots))
                failed = True
    finally:
        my_predbat.iboost_smart = False
        my_predbat.iboost_slots = []
        my_predbat.iboost_today = 0
        my_predbat.iboost_max_energy = my_predbat.get_arg("iboost_max_energy")
        my_predbat.iboost_max_power = my_predbat.get_arg("iboost_max_power") / (60 * 1000)
        my_predbat.iboost_smart_min_length = my_predbat.get_arg("iboost_smart_min_length")
        my_predbat.minutes_now = orig_minutes_now
        my_predbat.forecast_minutes = orig_forecast_minutes
        my_predbat.rate_import = orig_rate_import
        my_predbat.rate_export = orig_rate_export

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
    failed |= run_iboost_smart_duplicate_slots_test(my_predbat)

    return failed
