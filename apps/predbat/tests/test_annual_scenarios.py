# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction three-scenario day runner."""

from annual import DAY_MINUTES, PLAN_MINUTES, add_car_to_load, timer_charge_window


def flat_rates(cheap_start, cheap_end, cheap_rate, peak_rate):
    """Build a 48 hour import rate dict with one cheap overnight band per day."""
    rates = {}
    for minute in range(PLAN_MINUTES):
        in_day = minute % DAY_MINUTES
        rates[minute] = cheap_rate if cheap_start <= in_day < cheap_end else peak_rate
    return rates


def test_annual_scenarios(my_predbat):
    """Verify the timer charge window and car load insertion helpers."""
    failed = False
    print("**** Testing annual scenario helpers ****")

    print("Test: timer_charge_window finds the cheapest band and sizes it to the car's energy")
    rates = flat_rates(cheap_start=30, cheap_end=330, cheap_rate=7.0, peak_rate=30.0)
    window = timer_charge_window(rates, car_kwh=14.8, car_rate_kw=7.4)
    if not window:
        print("  ERROR: expected a charge window")
        failed = True
    else:
        first = window[0]
        if first["start"] != 30:
            print("  ERROR: the window should start at the cheap band start 30, got {}".format(first["start"]))
            failed = True
        # 14.8 kWh at 7.4 kW is 2 hours
        if first["end"] - first["start"] != 120:
            print("  ERROR: expected a 120 minute window for 14.8 kWh at 7.4 kW, got {}".format(first["end"] - first["start"]))
            failed = True

    print("Test: a car needing more than the cheap band gets a window extended beyond it")
    long_window = timer_charge_window(rates, car_kwh=74.0, car_rate_kw=7.4)
    if long_window[0]["end"] - long_window[0]["start"] < 300:
        print("  ERROR: a 10 hour charge should extend past the 5 hour cheap band, got {} minutes".format(long_window[0]["end"] - long_window[0]["start"]))
        failed = True

    print("Test: zero car energy produces no window")
    if timer_charge_window(rates, car_kwh=0.0, car_rate_kw=7.4) != []:
        print("  ERROR: no car energy should produce no window")
        failed = True

    print("Test: add_car_to_load inserts exactly the car's energy and stays cumulative")
    base = {minute: 0.01 * minute for minute in range(PLAN_MINUTES + 1)}
    with_car = add_car_to_load(base, window, car_kwh=14.8)
    added = with_car[PLAN_MINUTES] - base[PLAN_MINUTES]
    if abs(added - 14.8) > 1e-6:
        print("  ERROR: expected 14.8 kWh added over the window, got {}".format(added))
        failed = True
    for minute in range(1, PLAN_MINUTES + 1):
        if with_car[minute] < with_car[minute - 1] - 1e-12:
            print("  ERROR: the series stopped being cumulative at minute {}".format(minute))
            failed = True
            break

    print("Test: add_car_to_load leaves minutes before the window untouched")
    if abs(with_car[10] - base[10]) > 1e-12:
        print("  ERROR: minutes before the window must be unchanged")
        failed = True

    print("Test: add_car_to_load does not mutate its input")
    if abs(base[PLAN_MINUTES] - 0.01 * PLAN_MINUTES) > 1e-9:
        print("  ERROR: add_car_to_load must not mutate the input series")
        failed = True

    print("Test: the car repeats on the second day of the window")
    second_day = [entry for entry in window if entry["start"] >= DAY_MINUTES]
    if not second_day:
        print("  ERROR: the timer should also charge on day two of the 48 hour window")
        failed = True

    return failed
