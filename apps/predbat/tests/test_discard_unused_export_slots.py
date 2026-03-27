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


def run_discard_unused_export_slots_tests(my_predbat):
    """
    Tests for discard_unused_export_slots method
    """
    failed = False
    failed |= test_discard_disabled(my_predbat)
    failed |= test_keep_enabled(my_predbat)
    failed |= test_combine_contiguous_same_limit(my_predbat)
    failed |= test_no_combine_different_limit(my_predbat)
    failed |= test_no_combine_non_contiguous(my_predbat)
    failed |= test_no_combine_manual_times_start(my_predbat)
    failed |= test_no_combine_manual_times_prev(my_predbat)
    failed |= test_mixed_slots(my_predbat)
    failed |= test_all_disabled(my_predbat)
    failed |= test_freeze_export_kept(my_predbat)
    return failed


def make_window(start, end, average=10.0, target=None):
    w = {"start": start, "end": end, "average": average}
    if target is not None:
        w["target"] = target
    return w


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.debug_enable = False
    my_predbat.manual_all_times = []


def test_discard_disabled(my_predbat):
    """Slots with limit == 100 should be discarded"""
    print("**** test_discard_export_disabled ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(750, 780)]
    limits = [100.0, 100.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 0:
        print("ERROR: Expected 0 slots but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_keep_enabled(my_predbat):
    """Slots with limit < 100 should be kept"""
    print("**** test_keep_export_enabled ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(780, 810)]
    limits = [50.0, 30.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots but got {}".format(len(result_limits)))
        failed = True
    elif result_limits[0] != 50.0 or result_limits[1] != 30.0:
        print("ERROR: Expected limits [50.0, 30.0] but got {}".format(result_limits))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_combine_contiguous_same_limit(my_predbat):
    """Contiguous slots with same limit should be combined"""
    print("**** test_combine_export_contiguous_same_limit ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750, target=50.0), make_window(750, 780, target=50.0)]
    limits = [50.0, 50.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 1:
        print("ERROR: Expected 1 combined slot but got {}".format(len(result_limits)))
        failed = True
    elif result_windows[0]["start"] != 720 or result_windows[0]["end"] != 780:
        print("ERROR: Expected combined window 720-780 but got {}-{}".format(result_windows[0]["start"], result_windows[0]["end"]))
        failed = True
    elif result_limits[0] != 50.0:
        print("ERROR: Expected limit 50.0 but got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_different_limit(my_predbat):
    """Contiguous slots with different limits should not be combined"""
    print("**** test_no_combine_export_different_limit ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(750, 780)]
    limits = [50.0, 30.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_non_contiguous(my_predbat):
    """Non-contiguous slots with same limit should not be combined"""
    print("**** test_no_combine_export_non_contiguous ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(780, 810)]  # Gap between 750 and 780
    limits = [50.0, 50.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (non-contiguous) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_manual_times_start(my_predbat):
    """Slots should not combine if second window start is in manual_all_times"""
    print("**** test_no_combine_export_manual_times_start ****")
    failed = False
    setup(my_predbat)
    my_predbat.manual_all_times = [750]

    windows = [make_window(720, 750), make_window(750, 780)]
    limits = [50.0, 50.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (manual time boundary) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_manual_times_prev(my_predbat):
    """Slots should not combine if previous window start is in manual_all_times"""
    print("**** test_no_combine_export_manual_times_prev ****")
    failed = False
    setup(my_predbat)
    my_predbat.manual_all_times = [720]

    windows = [make_window(720, 750), make_window(750, 780)]
    limits = [50.0, 50.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (prev start is manual time) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_mixed_slots(my_predbat):
    """Mix of disabled, enabled, and combinable slots"""
    print("**** test_discard_export_mixed_slots ****")
    failed = False
    setup(my_predbat)

    windows = [
        make_window(720, 750, target=100.0),  # disabled
        make_window(750, 780, target=50.0),  # keep
        make_window(780, 810, target=50.0),  # combine with previous
        make_window(810, 840, target=100.0),  # disabled
        make_window(840, 870, target=30.0),  # keep
    ]
    limits = [100.0, 50.0, 50.0, 100.0, 30.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 remaining slots but got {}".format(len(result_limits)))
        failed = True
    elif result_limits[0] != 50.0 or result_limits[1] != 30.0:
        print("ERROR: Expected limits [50.0, 30.0] but got {}".format(result_limits))
        failed = True
    elif result_windows[0]["start"] != 750 or result_windows[0]["end"] != 810:
        print("ERROR: Expected first combined window 750-810 but got {}-{}".format(result_windows[0]["start"], result_windows[0]["end"]))
        failed = True
    elif result_windows[1]["start"] != 840 or result_windows[1]["end"] != 870:
        print("ERROR: Expected second window 840-870 but got {}-{}".format(result_windows[1]["start"], result_windows[1]["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_all_disabled(my_predbat):
    """All slots disabled should return empty lists"""
    print("**** test_discard_export_all_disabled ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(750, 780), make_window(780, 810)]
    limits = [100.0, 100.0, 100.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 0 or len(result_windows) != 0:
        print("ERROR: Expected empty lists but got {} limits and {} windows".format(len(result_limits), len(result_windows)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_freeze_export_kept(my_predbat):
    """Freeze export slots (limit=99) should be kept and combinable"""
    print("**** test_freeze_export_kept ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750, target=99.0), make_window(750, 780, target=99.0)]
    limits = [99.0, 99.0]

    result_limits, result_windows = my_predbat.discard_unused_export_slots(limits, windows)

    if len(result_limits) != 1:
        print("ERROR: Expected 1 combined freeze export slot but got {}".format(len(result_limits)))
        failed = True
    elif result_limits[0] != 99.0:
        print("ERROR: Expected limit 99.0 but got {}".format(result_limits[0]))
        failed = True
    elif result_windows[0]["start"] != 720 or result_windows[0]["end"] != 780:
        print("ERROR: Expected combined window 720-780 but got {}-{}".format(result_windows[0]["start"], result_windows[0]["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed
