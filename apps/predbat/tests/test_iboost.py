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


def set_rate_profile(my_predbat, profile, default_rate=20.0, export_rate=0.0):
    """
    Set a custom import rate profile for iBoost tests

    profile is a list of (start, end, rate) tuples applied over a flat default_rate; rates are
    filled a couple of hours past the forecast end so sliding windows never fall off the data.
    """
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 120):
        my_predbat.rate_import[minute] = default_rate
        my_predbat.rate_export[minute] = export_rate
    for start, end, rate in profile:
        for minute in range(start, end):
            my_predbat.rate_import[minute] = rate
    my_predbat.rate_export_min = export_rate
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan_export(my_predbat.rate_export, print=False)


def check_slot_invariants(test_name, slots):
    """
    Check the invariants all iBoost plans must hold: sorted by time, no duplicate slot starts
    """
    failed = False
    starts = [slot["start"] for slot in slots]
    if starts != sorted(starts):
        print("ERROR: {} iBoost slots are not sorted by time: {}".format(test_name, slots))
        failed = True
    if len(starts) != len(set(starts)):
        print("ERROR: {} iBoost plan contains duplicate slot starts: {}".format(test_name, slots))
        failed = True
    return failed


def run_iboost_smart_test(test_name, my_predbat, today=0, max_energy=1, max_power=1, min_length=0, expect_cost=0, expect_kwh=0, expect_time=0, expect_first_average=None):
    """
    Run a single iBoost smart planner test case and check the resulting plan totals
    """
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
    if expect_first_average is not None and (not slots or slots[0]["average"] != expect_first_average):
        print("ERROR: Iboost first slot average should be {} got {}".format(expect_first_average, slots))
        failed = True
    failed |= check_slot_invariants(test_name, slots)

    my_predbat.iboost_smart = False
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0

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

    # Non-flat profile with 60-minute windows: the window average must cover every interval in the
    # window (5.5 = mean of 5 and 6, not the first interval's 5) and overlapping windows must not
    # double-book a slot start (window 1410-1470 and window 1440-1500 both cover minute 1440).
    set_rate_profile(my_predbat, [(840, 870, 5.0), (870, 900, 6.0)], default_rate=20.0)
    failed |= run_iboost_smart_test("iboost_window_pricing", my_predbat, today=0, max_energy=1, max_power=1, min_length=60, expect_cost=2.75 + 2.75 + 10.0 + 10.0, expect_kwh=2.0, expect_time=120, expect_first_average=5.5)

    return failed
