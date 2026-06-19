# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import reset_inverter


def run_clipping_tests(my_predbat):
    """
    Tests for inject_clipping_export_windows method
    """
    failed = False
    failed |= test_inject_aborts_if_disabled(my_predbat)
    failed |= test_inject_aborts_if_empty_forecast(my_predbat)
    failed |= test_inject_creates_contiguous_window(my_predbat)
    failed |= test_inject_cleans_fragmented_windows(my_predbat)
    return failed


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast_kwh = {}
    my_predbat.minutes_now = 0
    my_predbat.export_rate = {}
    my_predbat.export_window_best = []
    my_predbat.high_export_rates = []
    # Adding log mock to avoid exceptions if not using MockBase
    if not hasattr(my_predbat, "log"):
        my_predbat.log = lambda x: print(x)
    if not hasattr(my_predbat, "time_abs_str"):
        my_predbat.time_abs_str = lambda x: str(x)


def test_inject_aborts_if_disabled(my_predbat):
    print("**** test_inject_aborts_if_disabled ****")
    failed = False
    setup(my_predbat)
    my_predbat.clipping_buffer_enable = False
    my_predbat.clipping_buffer_forecast_kwh = {720: 1.0}

    my_predbat.inject_clipping_export_windows()

    if len(my_predbat.export_window_best) > 0:
        print("ERROR: Window was injected when clipping was disabled!")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_aborts_if_empty_forecast(my_predbat):
    print("**** test_inject_aborts_if_empty_forecast ****")
    failed = False
    setup(my_predbat)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast_kwh = {}

    my_predbat.inject_clipping_export_windows()

    if len(my_predbat.export_window_best) > 0:
        print("ERROR: Window was injected when forecast was empty!")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_creates_contiguous_window(my_predbat):
    print("**** test_inject_creates_contiguous_window ****")
    failed = False
    setup(my_predbat)
    my_predbat.minutes_now = 240  # 04:00
    # Peak from 13:00 to 14:00 (780 to 840)
    my_predbat.clipping_buffer_forecast_kwh = {780: 1.0, 810: 2.0}

    my_predbat.inject_clipping_export_windows()

    if len(my_predbat.export_window_best) != 1:
        print("ERROR: Expected exactly 1 window injected, got {}".format(len(my_predbat.export_window_best)))
        return True

    w = my_predbat.export_window_best[0]

    if w["start"] != 240:
        print("ERROR: Expected window start at 240, got {}".format(w["start"]))
        failed = True

    if w["end"] != 840:
        print("ERROR: Expected window end at 840, got {}".format(w["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_cleans_fragmented_windows(my_predbat):
    print("**** test_inject_cleans_fragmented_windows ****")
    failed = False
    setup(my_predbat)
    my_predbat.minutes_now = 0
    # Peak from 780 to 810
    my_predbat.clipping_buffer_forecast_kwh = {
        780: 1.0,
    }

    # Inject intersecting fragmented windows
    my_predbat.export_window_best = [
        {"start": 30, "end": 50, "average": 0},  # Before morning_start (60) - should KEEP
        {"start": 100, "end": 120, "average": 0},  # Inside the new window - should DROP
        {"start": 700, "end": 800, "average": 0},  # Intersecting the new window - should DROP
        {"start": 900, "end": 960, "average": 0},  # After peak_end - should KEEP
    ]

    my_predbat.inject_clipping_export_windows()

    # We expect 3 windows: 2 kept + 1 injected
    if len(my_predbat.export_window_best) != 3:
        print("ERROR: Expected 3 windows (2 kept + 1 new), got {}".format(len(my_predbat.export_window_best)))
        return True

    starts = [w["start"] for w in my_predbat.export_window_best]
    if 100 in starts or 700 in starts:
        print("ERROR: Fragmented windows were not cleaned!")
        failed = True

    if 60 not in starts:  # Injected window start
        print("ERROR: Injected window not found!")
        failed = True

    if not failed:
        print("PASS")
    return failed
