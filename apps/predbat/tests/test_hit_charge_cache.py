# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the export-to-charge window collision cache in optimise_charge_limit_price_threads.

The cache is only sound because window geometry is immutable for the life of that call - the
optimiser turns windows on and off but never moves a start or end. These tests pin both halves:
hit_charge_window's own contract, and the invariant that lets its result be reused.
"""
from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates


def run_hit_charge_cache_tests(my_predbat):
    """Run the hit_charge_window and collision-cache invariant tests"""
    failed = False
    failed |= test_hit_charge_window_finds_overlap(my_predbat)
    failed |= test_hit_charge_window_boundaries_do_not_overlap(my_predbat)
    failed |= test_hit_charge_window_returns_first_match(my_predbat)
    failed |= test_optimise_threads_does_not_move_window_bounds(my_predbat)
    return failed


def make_window(start, end, average=10.0):
    """Build a minimal window dict"""
    return {"start": start, "end": end, "average": average}


def test_hit_charge_window_finds_overlap(my_predbat):
    """hit_charge_window returns the index of the charge window an interval overlaps, else -1"""
    print("**** test_hit_charge_window_finds_overlap ****")
    failed = False
    reset_inverter(my_predbat)

    windows = [make_window(0, 30), make_window(60, 90), make_window(120, 180)]

    for start, end, expect in ((0, 30, 0), (10, 20, 0), (25, 65, 0), (70, 80, 1), (150, 200, 2), (30, 60, -1), (200, 300, -1)):
        got = my_predbat.hit_charge_window(windows, start, end)
        if got != expect:
            print("ERROR: hit_charge_window({}, {}) expected {} but got {}".format(start, end, expect, got))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_hit_charge_window_boundaries_do_not_overlap(my_predbat):
    """An interval that merely touches a charge window at a boundary is not a collision"""
    print("**** test_hit_charge_window_boundaries_do_not_overlap ****")
    failed = False
    reset_inverter(my_predbat)

    windows = [make_window(60, 90)]

    if my_predbat.hit_charge_window(windows, 30, 60) != -1:
        print("ERROR: interval ending exactly at the window start reported a collision")
        failed = True
    if my_predbat.hit_charge_window(windows, 90, 120) != -1:
        print("ERROR: interval starting exactly at the window end reported a collision")
        failed = True
    if my_predbat.hit_charge_window(windows, 59, 61) != 0:
        print("ERROR: interval straddling the window start did not report a collision")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_hit_charge_window_returns_first_match(my_predbat):
    """When several charge windows overlap the interval, the lowest index is returned - the cache
    stores this answer, so it has to be deterministic rather than any-match"""
    print("**** test_hit_charge_window_returns_first_match ****")
    failed = False
    reset_inverter(my_predbat)

    windows = [make_window(0, 30), make_window(30, 60), make_window(60, 90)]
    got = my_predbat.hit_charge_window(windows, 10, 80)
    if got != 0:
        print("ERROR: Expected the first overlapping window (0) but got {}".format(got))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_optimise_threads_does_not_move_window_bounds(my_predbat):
    """The collision cache assumes optimise_charge_limit_price_threads never moves a window's start
    or end - it only enables and disables windows. Run the optimiser and assert the geometry it was
    given comes back untouched; if a future change starts moving windows, this fails rather than the
    cache silently returning stale collisions."""
    print("**** test_optimise_threads_does_not_move_window_bounds ****")
    failed = False
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)

    # Set the metric config this path reads rather than depending on whatever earlier tests left
    # behind, and restore it afterwards so this test neither depends on nor leaks ambient state
    saved = {name: getattr(my_predbat, name) for name in ("metric_battery_value_export_scaling", "metric_battery_value_scaling", "pv_metric10_weight", "pv_metric90_weight")}
    my_predbat.metric_battery_value_export_scaling = 1.0
    my_predbat.metric_battery_value_scaling = 1.0
    my_predbat.pv_metric10_weight = 0.0
    my_predbat.pv_metric90_weight = 0.0

    # The optimiser simulates, so it needs a prediction with flat PV/load data behind it
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 0.1
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.load_minutes_step = load_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.args["threads"] = 0

    my_predbat.calculate_export_oncharge = False
    charge_window = [make_window(my_predbat.minutes_now + 60 * n, my_predbat.minutes_now + 60 * n + 30, 10.0) for n in range(4)]
    export_window = [make_window(my_predbat.minutes_now + 60 * n + 30, my_predbat.minutes_now + 60 * n + 60, 15.0) for n in range(4)]
    charge_limit = [my_predbat.soc_max] * len(charge_window)
    export_limits = [100.0] * len(export_window)

    my_predbat.charge_window_best = charge_window
    my_predbat.charge_limit_best = charge_limit
    my_predbat.export_window_best = export_window
    my_predbat.export_limits_best = export_limits

    before_charge = [(w["start"], w["end"]) for w in charge_window]
    before_export = [(w["start"], w["end"]) for w in export_window]

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window, export_window)
    try:
        my_predbat.optimise_charge_limit_price_threads(
            price_set,
            price_links,
            window_index,
            len(charge_window),
            len(export_window),
            charge_limit,
            charge_window,
            export_window,
            export_limits,
            end_record=my_predbat.end_record,
            quiet=True,
        )
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)

    after_charge = [(w["start"], w["end"]) for w in my_predbat.charge_window_best]
    after_export = [(w["start"], w["end"]) for w in my_predbat.export_window_best]
    if after_charge != before_charge:
        print("ERROR: charge window bounds moved during optimisation: {} -> {}".format(before_charge, after_charge))
        failed = True
    if after_export != before_export:
        print("ERROR: export window bounds moved during optimisation: {} -> {}".format(before_export, after_export))
        failed = True

    if not failed:
        print("PASS")
    return failed
