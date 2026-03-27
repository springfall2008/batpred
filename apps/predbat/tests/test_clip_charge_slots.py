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


def run_clip_charge_slots_tests(my_predbat):
    """
    Tests for clip_charge_slots method
    """
    failed = False
    failed |= test_disabled_window_ignored(my_predbat)
    failed |= test_passed_window_clipped(my_predbat)
    failed |= test_clip_off_soc_above_limit(my_predbat)
    failed |= test_clip_off_preserves_reserve(my_predbat)
    failed |= test_clip_up_soc_max_below_limit(my_predbat)
    failed |= test_clip_up_soc_max_equals_limit_and_dropping(my_predbat)
    failed |= test_freeze_charge_to_charge_at_100_soc(my_predbat)
    failed |= test_freeze_charge_kept_below_100_soc(my_predbat)
    failed |= test_normal_window_unchanged(my_predbat)
    failed |= test_multiple_windows_mixed(my_predbat)
    return failed


def make_window(start, end, average=10.0):
    return {"start": start, "end": end, "average": average}


def make_predict_soc(minutes_now, soc_value, duration_minutes=60):
    """Build a predict_soc dict with constant SoC from minute 0 to duration_minutes"""
    predict_soc = {}
    for minute in range(0, duration_minutes + 5, 5):
        predict_soc[minute] = soc_value
    return predict_soc


def make_predict_soc_ramp(minutes_now, soc_start, soc_end, duration_minutes=60):
    """Build a predict_soc dict with linearly ramping SoC"""
    predict_soc = {}
    steps = duration_minutes // 5
    for i in range(steps + 1):
        minute = i * 5
        if steps > 0:
            predict_soc[minute] = soc_start + (soc_end - soc_start) * i / steps
        else:
            predict_soc[minute] = soc_start
    return predict_soc


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 0.5
    my_predbat.debug_enable = False


def test_disabled_window_ignored(my_predbat):
    """Windows with limit <= 0 should be left unchanged"""
    print("**** test_clip_charge_disabled_window_ignored ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [0.0]
    predict_soc = make_predict_soc(minutes_now, 5.0, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 0.0:
        print("ERROR: Disabled window limit changed from 0.0 to {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_passed_window_clipped(my_predbat):
    """Windows that have already passed should be set to 0"""
    print("**** test_clip_charge_passed_window_clipped ****")
    failed = False
    setup(my_predbat)

    minutes_now = 780  # After the window
    windows = [make_window(720, 750)]
    limits = [5.0]
    predict_soc = make_predict_soc(minutes_now, 5.0, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 0:
        print("ERROR: Passed window not clipped, limit is {} expected 0".format(result_limits[0]))
        failed = True
    if result_windows[0]["target"] != 0:
        print("ERROR: Passed window target is {} expected 0".format(result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_clip_off_soc_above_limit(my_predbat):
    """Charge window should be clipped off when soc_min is well above the charge limit"""
    print("**** test_clip_charge_off_soc_above_limit ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [3.0]  # Charge to 3 kWh (30% of 10)
    # SoC at 8 kWh (80%) which is well above 30% + 1%
    predict_soc = make_predict_soc(minutes_now, 8.0, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 0:
        print("ERROR: Expected charge window clipped off (0) but got {}".format(result_limits[0]))
        failed = True
    if result_windows[0]["target"] != 0:
        print("ERROR: Expected target 0 but got {}".format(result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_clip_off_preserves_reserve(my_predbat):
    """Charge window at reserve level should NOT be clipped off even if SoC is above it"""
    print("**** test_clip_charge_off_preserves_reserve ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [my_predbat.reserve]  # Freeze charge (limit == reserve)
    # SoC well above reserve
    predict_soc = make_predict_soc(minutes_now, 8.0, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    # Reserve slots should not be clipped off by the soc_min > limit check
    if result_limits[0] == 0:
        print("ERROR: Reserve slot was clipped off, should be preserved")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_clip_up_soc_max_below_limit(my_predbat):
    """When soc_max < limit, the window should be clipped up (limit set to soc_max, target set to soc_max)"""
    print("**** test_clip_charge_up_soc_max_below_limit ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [8.0]  # Want to charge to 8 kWh
    # But SoC only reaches 5.0 in the window
    predict_soc = make_predict_soc(minutes_now, 5.0, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != my_predbat.soc_max:
        print("ERROR: Expected limit clipped up to soc_max ({}) but got {}".format(my_predbat.soc_max, result_limits[0]))
        failed = True
    if result_windows[0]["target"] != 5.0:
        print("ERROR: Expected target set to soc_max in window (5.0) but got {}".format(result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_clip_up_soc_max_equals_limit_and_dropping(my_predbat):
    """When soc_max == limit and soc_max > soc at end-1, clip up"""
    print("**** test_clip_charge_up_soc_max_equals_limit_dropping ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [6.0]  # Want to charge to 6 kWh
    # SoC peaks at 6.0 then drops to 5.0 at end-1
    predict_soc = make_predict_soc_ramp(minutes_now, 6.0, 5.0, 30)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != my_predbat.soc_max:
        print("ERROR: Expected limit clipped up to soc_max ({}) but got {}".format(my_predbat.soc_max, result_limits[0]))
        failed = True
    if result_windows[0]["target"] != 6.0:
        print("ERROR: Expected target 6.0 but got {}".format(result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_freeze_charge_to_charge_at_100_soc(my_predbat):
    """Freeze charge (limit==reserve) at 100% SoC should be changed to a full charge"""
    print("**** test_freeze_charge_to_charge_at_100_soc ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [my_predbat.reserve]  # Freeze charge
    # SoC at soc_max throughout
    predict_soc = make_predict_soc(minutes_now, my_predbat.soc_max, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != my_predbat.soc_max:
        print("ERROR: Expected freeze charge changed to soc_max ({}) but got {}".format(my_predbat.soc_max, result_limits[0]))
        failed = True
    if result_windows[0]["target"] != my_predbat.soc_max:
        print("ERROR: Expected target set to soc_max ({}) but got {}".format(my_predbat.soc_max, result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_freeze_charge_kept_below_100_soc(my_predbat):
    """Freeze charge (limit==reserve) below 100% SoC should be preserved"""
    print("**** test_freeze_charge_kept_below_100_soc ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [my_predbat.reserve]  # Freeze charge
    # SoC at 80% - not at max
    predict_soc = make_predict_soc(minutes_now, 8.0, 60)

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    # Should NOT be changed to soc_max since we're not at 100%
    if result_limits[0] == my_predbat.soc_max:
        print("ERROR: Freeze charge below 100% was converted to full charge")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_normal_window_unchanged(my_predbat):
    """A charge window where SoC is near the limit should be left unchanged"""
    print("**** test_clip_charge_normal_window_unchanged ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [5.0]  # Charge to 5 kWh (50%)
    # SoC at 4.5 which is close to limit but below - soc_min_percent (45) is not > limit_percent+1 (51)
    # and soc_max (4.5) < limit (5.0) triggers clip-up
    # Use SoC at exactly the limit so none of the special branches trigger
    predict_soc = {}
    for minute in range(0, 35, 5):
        predict_soc[minute] = 5.0  # soc_min == soc_max == limit
    # soc_m1 == soc_max so the (soc_max > soc_m1) branch won't trigger either
    # soc_min_percent (50) is not > limit_percent+1 (51) so clip-off doesn't trigger
    # soc_max (5.0) is not < limit (5.0) so clip-up-below doesn't trigger

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 5.0:
        print("ERROR: Expected limit unchanged at 5.0 but got {}".format(result_limits[0]))
        failed = True
    if result_windows[0]["target"] != 5.0:
        print("ERROR: Expected target unchanged at 5.0 but got {}".format(result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_multiple_windows_mixed(my_predbat):
    """Test multiple charge windows with different scenarios"""
    print("**** test_clip_charge_multiple_windows_mixed ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [
        make_window(720, 750),  # Window 0: SoC well above limit -> clip off
        make_window(780, 810),  # Window 1: freeze charge at 100% SoC -> convert to full charge
        make_window(840, 870),  # Window 2: SoC below limit -> clip up
    ]
    limits = [3.0, my_predbat.reserve, 8.0]

    predict_soc = {}
    # Window 0: relative minutes 0-30, SoC at 8.0, limit is 3.0 (30%) -> clip off
    for minute in range(0, 35, 5):
        predict_soc[minute] = 8.0
    # Window 1: relative minutes 60-90, SoC at soc_max -> freeze charge to full charge
    for minute in range(60, 95, 5):
        predict_soc[minute] = my_predbat.soc_max
    # Window 2: relative minutes 120-150, SoC at 5.0, limit 8.0 -> clip up
    for minute in range(120, 155, 5):
        predict_soc[minute] = 5.0

    result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 3, 5)

    # Window 0: clipped off
    if result_limits[0] != 0:
        print("ERROR: Window 0 expected clipped off (0) but got {}".format(result_limits[0]))
        failed = True

    # Window 1: freeze charge at 100% -> converted to soc_max
    if result_limits[1] != my_predbat.soc_max:
        print("ERROR: Window 1 expected converted to soc_max ({}) but got {}".format(my_predbat.soc_max, result_limits[1]))
        failed = True

    # Window 2: clipped up
    if result_limits[2] != my_predbat.soc_max:
        print("ERROR: Window 2 expected clipped up to soc_max ({}) but got {}".format(my_predbat.soc_max, result_limits[2]))
        failed = True
    if result_windows[2]["target"] != 5.0:
        print("ERROR: Window 2 expected target 5.0 but got {}".format(result_windows[2]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed
