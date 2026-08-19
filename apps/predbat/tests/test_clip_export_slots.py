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


def run_clip_export_slots_tests(my_predbat):
    """
    Tests for clip_export_slots method
    """
    failed = False
    failed |= test_freeze_export_left_alone_at_100_soc(my_predbat)
    failed |= test_freeze_export_kept_when_soc_below_max(my_predbat)
    failed |= test_normal_export_left_alone_when_soc_below_limit(my_predbat)
    failed |= test_normal_export_clipped_up_when_soc_above_limit(my_predbat)
    failed |= test_normal_export_clipped_up_when_soc_above_reserve_with_zero_limit(my_predbat)
    failed |= test_normal_export_clipped_up_when_soc_flat_above_limit(my_predbat)
    failed |= test_disabled_window_ignored(my_predbat)
    failed |= test_passed_window_clipped(my_predbat)
    failed |= test_multiple_windows_mixed(my_predbat)
    return failed


def make_window(start, end, average=10.0):
    return {"start": start, "end": end, "average": average}


def make_predict_soc(minutes_now, soc_value, duration_minutes=60):
    """Build a predict_soc dict with constant SoC from minute 0 to duration_minutes (relative to minutes_now)"""
    predict_soc = {}
    for minute in range(0, duration_minutes + 5, 5):
        predict_soc[minute] = soc_value
    return predict_soc


def make_predict_soc_falling(minutes_now, soc_start, soc_end, duration_minutes=60):
    """Build a predict_soc dict with SoC falling linearly from soc_start to soc_end over duration_minutes,
    the physical signature of an export that is genuinely moving energy"""
    predict_soc = {}
    steps = duration_minutes // 5
    for i, minute in enumerate(range(0, duration_minutes + 5, 5)):
        predict_soc[minute] = soc_start + (soc_end - soc_start) * min(i, steps) / steps
    return predict_soc


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.debug_enable = False
    my_predbat.battery_rate_max_discharge = 1 / 60.0
    my_predbat.battery_rate_max_scaling_discharge = 1.0
    my_predbat.manual_freeze_export_times = []
    my_predbat.manual_export_times = []


def test_freeze_export_left_alone_at_100_soc(my_predbat):
    """Clipping no longer removes a freeze export that sits at 100% SoC - deciding a slot achieves
    nothing is prune_dead_plan_slots' job now, so clipping must leave the limit untouched"""
    print("**** test_freeze_export_left_alone_at_100_soc ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720  # 12:00
    windows = [make_window(720, 750)]
    limits = [99.0]
    predict_soc = make_predict_soc(minutes_now, my_predbat.soc_max, 60)

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 99.0:
        print("ERROR: Freeze export was modified by clipping, expected 99.0 but got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_freeze_export_kept_when_soc_below_max(my_predbat):
    """Freeze export (limit=99) should be kept when SoC is not at soc_max"""
    print("**** test_freeze_export_kept_when_soc_below_max ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [99.0]
    # SoC at 80% of max
    predict_soc = make_predict_soc(minutes_now, my_predbat.soc_max * 0.8, 60)

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 99.0:
        print("ERROR: Freeze export was clipped when SoC below max! Expected 99.0 but got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_normal_export_left_alone_when_soc_below_limit(my_predbat):
    """A window whose SoC never reaches its export target is no longer clipped off here - removal is
    prune_dead_plan_slots' job. Clipping only narrows limits towards what is achievable, and there is
    nothing to narrow when the battery is below the target."""
    print("**** test_normal_export_left_alone_when_soc_below_limit ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [50.0]  # Export at 50% (limit_soc = 5.0)
    predict_soc = make_predict_soc(minutes_now, 2.0, 60)  # SoC well below the target

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 50.0:
        print("ERROR: Export limit was modified by clipping, expected 50.0 but got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_normal_export_clipped_up_when_soc_above_limit(my_predbat):
    """Normal export window limit should be clipped up when soc_min > limit_soc"""
    print("**** test_normal_export_clipped_up_when_soc_above_limit ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [20.0]  # Export at 20% (limit_soc = 10 * 20/100 = 2.0)
    # SoC falling from 8.0 to 6.0 (a genuine export in progress), soc_min 6.0 > limit_soc 2.0
    predict_soc = make_predict_soc_falling(minutes_now, 8.0, 6.0, 60)

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    # Should be clipped up - limit should be higher than original 20.0
    if result_limits[0] <= 20.0:
        print("ERROR: Expected export limit to be clipped up from 20.0 but got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_normal_export_clipped_up_when_soc_above_reserve_with_zero_limit(my_predbat):
    """A 0% export request should still be clipped up (not off) when SoC genuinely rises above reserve,
    so the reserve-floor check doesn't swallow the existing clip-up behaviour"""
    print("**** test_normal_export_clipped_up_when_soc_above_reserve_with_zero_limit ****")
    failed = False
    setup(my_predbat)
    my_predbat.reserve = 0.5

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [0.0]  # Export to 0% (drain to empty)
    # SoC well above reserve and falling - there is real energy available and it is moving
    predict_soc = make_predict_soc_falling(minutes_now, 8.0, 6.0, 60)

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] == 100.0:
        print("ERROR: Export was clipped off despite SoC being above reserve")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_normal_export_clipped_up_when_soc_flat_above_limit(my_predbat):
    """A phantom-shaped window (SoC flat, above the requested limit) now only has its limit narrowed by
    clipping - removing it is prune_dead_plan_slots' job, which asks the model rather than the SoC trace"""
    print("**** test_normal_export_clipped_up_when_soc_flat_above_limit ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [50.0]  # limit_soc = 5.0
    predict_soc = make_predict_soc(minutes_now, 8.0, 60)  # flat, above the target

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] in (99.0, 100.0):
        print("ERROR: Clipping removed/converted the window instead of narrowing it, got {}".format(result_limits[0]))
        failed = True
    if result_limits[0] <= 50.0:
        print("ERROR: Expected the limit to be clipped up from 50.0, got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_disabled_window_ignored(my_predbat):
    """Windows with limit=100 should be ignored and left unchanged"""
    print("**** test_disabled_window_ignored ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [make_window(720, 750)]
    limits = [100.0]
    predict_soc = make_predict_soc(minutes_now, 5.0, 60)

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 100.0:
        print("ERROR: Disabled window limit changed from 100.0 to {}".format(result_limits[0]))
        failed = True
    if result_windows[0]["target"] != 100.0:
        print("ERROR: Disabled window target changed to {}".format(result_windows[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_passed_window_clipped(my_predbat):
    """Windows that have already passed (zero length) should be set to 100"""
    print("**** test_passed_window_clipped ****")
    failed = False
    setup(my_predbat)

    minutes_now = 780  # 13:00 - after the window
    windows = [make_window(720, 750)]
    limits = [50.0]
    predict_soc = make_predict_soc(minutes_now, 5.0, 60)

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5)

    if result_limits[0] != 100.0:
        print("ERROR: Passed window not clipped, limit is {} expected 100.0".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_multiple_windows_mixed(my_predbat):
    """Test multiple windows with different scenarios in one call"""
    print("**** test_multiple_windows_mixed ****")
    failed = False
    setup(my_predbat)

    minutes_now = 720
    windows = [
        make_window(720, 750),  # Window 0: freeze export at 100% SoC (left alone)
        make_window(750, 780),  # Window 1: manual freeze export at 100% SoC (left alone)
        make_window(780, 810),  # Window 2: normal export (limit clipped up)
    ]
    limits = [99.0, 99.0, 50.0]

    # Mark window 1 as manual
    my_predbat.manual_freeze_export_times = [750]

    # SoC at max through windows 0 and 1, then falling through window 2 (a genuine export in progress)
    predict_soc = make_predict_soc(minutes_now, my_predbat.soc_max, 120)
    for i, minute in enumerate(range(60, 95, 5)):
        predict_soc[minute] = my_predbat.soc_max - 2.0 * i / 6

    result_windows, result_limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 3, 5)

    # Window 0: freeze export is never modified by clipping now
    if result_limits[0] != 99.0:
        print("ERROR: Window 0 (freeze at 100% SoC) expected 99.0 but got {}".format(result_limits[0]))
        failed = True

    # Window 1: manual freeze export likewise untouched
    if result_limits[1] != 99.0:
        print("ERROR: Window 1 (manual freeze at 100% SoC) expected 99.0 but got {}".format(result_limits[1]))
        failed = True

    # Window 2: normal export at 50% while SoC is at max (10.0) -> soc_min (10.0) > limit_soc (5.0) -> clipped up
    if result_limits[2] <= 50.0:
        print("ERROR: Window 2 (normal export) expected limit clipped up from 50.0 but got {}".format(result_limits[2]))
        failed = True

    if not failed:
        print("PASS")
    return failed
