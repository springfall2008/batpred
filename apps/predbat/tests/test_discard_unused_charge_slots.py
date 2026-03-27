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


def run_discard_unused_charge_slots_tests(my_predbat):
    """
    Tests for discard_unused_charge_slots method
    """
    failed = False
    failed |= test_discard_zero_limit(my_predbat)
    failed |= test_keep_nonzero_limit(my_predbat)
    failed |= test_combine_same_limit_contiguous(my_predbat)
    failed |= test_no_combine_different_limit(my_predbat)
    failed |= test_no_combine_manual_times(my_predbat)
    failed |= test_no_combine_active_keep(my_predbat)
    failed |= test_combine_same_price_higher_limit(my_predbat)
    failed |= test_no_combine_same_price_reserve_mismatch(my_predbat)
    failed |= test_no_combine_low_power_cheaper_rate(my_predbat)
    failed |= test_combine_low_power_reserve(my_predbat)
    failed |= test_mixed_slots(my_predbat)
    failed |= test_target_set_from_predict_soc(my_predbat)
    return failed


def make_window(start, end, average=10.0):
    return {"start": start, "end": end, "average": average}


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 0.5
    my_predbat.debug_enable = False
    my_predbat.minutes_now = 720
    my_predbat.predict_soc = {}
    my_predbat.manual_all_times = []
    my_predbat.all_active_keep = {}
    my_predbat.set_charge_low_power = False


def test_discard_zero_limit(my_predbat):
    """Slots with limit 0 should be discarded"""
    print("**** test_discard_zero_limit ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(750, 780)]
    limits = [0.0, 0.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 0:
        print("ERROR: Expected 0 slots but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_keep_nonzero_limit(my_predbat):
    """Slots with limit > 0 should be kept"""
    print("**** test_keep_nonzero_limit ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750), make_window(780, 810)]
    limits = [5.0, 3.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots but got {}".format(len(result_limits)))
        failed = True
    elif result_limits[0] != 5.0 or result_limits[1] != 3.0:
        print("ERROR: Expected limits [5.0, 3.0] but got {}".format(result_limits))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_combine_same_limit_contiguous(my_predbat):
    """Contiguous slots with same limit should be combined"""
    print("**** test_combine_same_limit_contiguous ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750, average=10.0), make_window(750, 780, average=10.0)]
    limits = [5.0, 5.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 1:
        print("ERROR: Expected 1 combined slot but got {}".format(len(result_limits)))
        failed = True
    elif result_windows[0]["start"] != 720 or result_windows[0]["end"] != 780:
        print("ERROR: Expected combined window 720-780 but got {}-{}".format(result_windows[0]["start"], result_windows[0]["end"]))
        failed = True
    elif result_limits[0] != 5.0:
        print("ERROR: Expected limit 5.0 but got {}".format(result_limits[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_different_limit(my_predbat):
    """Contiguous slots with different limits should not be combined (first merge path)"""
    print("**** test_no_combine_different_limit ****")
    failed = False
    setup(my_predbat)

    windows = [make_window(720, 750, average=10.0), make_window(750, 780, average=12.0)]
    limits = [5.0, 8.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_manual_times(my_predbat):
    """Slots at manual_all_times boundaries should not be combined"""
    print("**** test_no_combine_manual_times ****")
    failed = False
    setup(my_predbat)
    my_predbat.manual_all_times = [750]  # Boundary between the two windows

    windows = [make_window(720, 750, average=10.0), make_window(750, 780, average=10.0)]
    limits = [5.0, 5.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (not combined due to manual time) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_active_keep(my_predbat):
    """Slots at all_active_keep boundaries should not be combined"""
    print("**** test_no_combine_active_keep ****")
    failed = False
    setup(my_predbat)
    my_predbat.all_active_keep = {750: 1.0}

    windows = [make_window(720, 750, average=10.0), make_window(750, 780, average=10.0)]
    limits = [5.0, 5.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (not combined due to active_keep) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_combine_same_price_higher_limit(my_predbat):
    """Contiguous slots with same price, higher second limit, and first target < first limit should combine"""
    print("**** test_combine_same_price_higher_limit ****")
    failed = False
    setup(my_predbat)

    # First window: target needs to be < limit for the second merge path
    # Window 720-750: predict_minute_start=0, predict_minute_end=30
    # Set start_soc and end_soc below limit so target = end_soc < limit
    soc_data = {}
    for m in range(0, 65, 5):
        soc_data[m] = 3.0
    my_predbat.predict_soc = soc_data

    windows = [make_window(720, 750, average=10.0), make_window(750, 780, average=10.0)]
    limits = [5.0, 8.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 1:
        print("ERROR: Expected 1 combined slot but got {}".format(len(result_limits)))
        failed = True
    elif result_limits[0] != 8.0:
        print("ERROR: Expected combined limit 8.0 but got {}".format(result_limits[0]))
        failed = True
    elif result_windows[0]["start"] != 720 or result_windows[0]["end"] != 780:
        print("ERROR: Expected combined window 720-780 but got {}-{}".format(result_windows[0]["start"], result_windows[0]["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_same_price_reserve_mismatch(my_predbat):
    """Should not combine when second is non-reserve but first is reserve (prevents freeze+charge merge)"""
    print("**** test_no_combine_same_price_reserve_mismatch ****")
    failed = False
    setup(my_predbat)
    my_predbat.predict_soc = {0: 0.3, 5: 0.3, 10: 0.3}

    windows = [make_window(720, 750, average=10.0), make_window(750, 780, average=10.0)]
    limits = [my_predbat.reserve, 5.0]  # First is reserve (freeze charge), second is normal

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (reserve/non-reserve not combined) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_combine_low_power_cheaper_rate(my_predbat):
    """With low power mode on, should not combine when previous slot has lower average rate"""
    print("**** test_no_combine_low_power_cheaper_rate ****")
    failed = False
    setup(my_predbat)
    my_predbat.set_charge_low_power = True

    # Second slot has higher rate than first - average check fails (prev.average < window.average)
    windows = [make_window(720, 750, average=8.0), make_window(750, 780, average=12.0)]
    limits = [5.0, 5.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 slots (low power prevents combine with cheaper first slot) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_combine_low_power_reserve(my_predbat):
    """With low power mode on, reserve slots should still combine regardless of rate"""
    print("**** test_combine_low_power_reserve ****")
    failed = False
    setup(my_predbat)
    my_predbat.set_charge_low_power = True

    windows = [make_window(720, 750, average=8.0), make_window(750, 780, average=12.0)]
    limits = [my_predbat.reserve, my_predbat.reserve]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 1:
        print("ERROR: Expected 1 combined slot (reserve bypasses low power check) but got {}".format(len(result_limits)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_mixed_slots(my_predbat):
    """Mix of zero, nonzero, and combinable slots"""
    print("**** test_discard_mixed_slots ****")
    failed = False
    setup(my_predbat)

    windows = [
        make_window(720, 750, average=10.0),  # limit 0 -> discard
        make_window(750, 780, average=10.0),  # limit 5 -> keep
        make_window(780, 810, average=10.0),  # limit 5 -> combine with previous
        make_window(810, 840, average=10.0),  # limit 0 -> discard
        make_window(840, 870, average=10.0),  # limit 3 -> keep
    ]
    limits = [0.0, 5.0, 5.0, 0.0, 3.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 2:
        print("ERROR: Expected 2 remaining slots but got {}".format(len(result_limits)))
        failed = True
    elif result_limits[0] != 5.0 or result_limits[1] != 3.0:
        print("ERROR: Expected limits [5.0, 3.0] but got {}".format(result_limits))
        failed = True
    elif result_windows[0]["start"] != 750 or result_windows[0]["end"] != 810:
        print("ERROR: Expected first combined window 750-810 but got {}-{}".format(result_windows[0]["start"], result_windows[0]["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_target_set_from_predict_soc(my_predbat):
    """Target should be set based on predict_soc when end_soc > limit"""
    print("**** test_target_set_from_predict_soc ****")
    failed = False
    setup(my_predbat)

    # predict_soc: start and end SoC both above the limit -> target = limit
    # Window 720-750: predict_minute_start=0, predict_minute_end=30
    soc_above = {}
    for m in range(0, 35, 5):
        soc_above[m] = 8.0
    my_predbat.predict_soc = soc_above

    windows = [make_window(720, 750, average=10.0)]
    limits = [5.0]

    result_limits, result_windows = my_predbat.discard_unused_charge_slots(limits, windows, my_predbat.reserve)

    if len(result_limits) != 1:
        print("ERROR: Expected 1 slot but got {}".format(len(result_limits)))
        failed = True
    elif result_windows[0]["target"] != 5.0:
        print("ERROR: Expected target set to limit (5.0) when SoC > limit, but got {}".format(result_windows[0]["target"]))
        failed = True

    # Now test with end_soc below limit -> target = end_soc
    soc_below = {}
    for m in range(0, 35, 5):
        soc_below[m] = 2.0 + (m / 30.0) * 2.0  # ramps from 2.0 to 4.0
    my_predbat.predict_soc = soc_below

    windows2 = [make_window(720, 750, average=10.0)]
    limits2 = [5.0]

    result_limits2, result_windows2 = my_predbat.discard_unused_charge_slots(limits2, windows2, my_predbat.reserve)

    if len(result_limits2) != 1:
        print("ERROR: Expected 1 slot but got {}".format(len(result_limits2)))
        failed = True
    elif result_windows2[0]["target"] != soc_below[30]:
        print("ERROR: Expected target set to end_soc ({}) when SoC < limit, but got {}".format(soc_below[30], result_windows2[0]["target"]))
        failed = True

    if not failed:
        print("PASS")
    return failed
