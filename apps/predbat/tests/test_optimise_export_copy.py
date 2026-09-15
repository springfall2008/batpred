# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for how optimise_export copies the export window list it is given.

optimise_export took a deepcopy of the whole export window list on entry - every window dict, on
every call - which on the heaviest benchmark scenario was 2.5 million deepcopy calls and the single
largest block of copying in a plan.

It does not need one. The trial start is the only thing written, and _prepare_export already applies
it copy-on-write: it takes its own list and replaces the one window it changes with dict(window,
start=start). Nothing in optimise_export writes to a window dict at all, so a shallow list copy is
enough to keep the caller from reordering the list underneath a pending batch.

Both halves are pinned here: that no deep copy is taken, and that the caller's window dicts still
come back untouched - the guarantee the deepcopy was there to provide.
"""
from tests.test_export_commitment import setup_single_export_window


class CountingWindow(dict):
    """An export window that records how many times it has been deep-copied"""

    deepcopy_count = 0

    def __deepcopy__(self, memo):
        """Record the copy and return an independent duplicate"""
        CountingWindow.deepcopy_count += 1
        duplicate = CountingWindow(self)
        memo[id(self)] = duplicate
        return duplicate


def run_optimise_export_copy_tests(my_predbat):
    """Run the optimise_export window copying tests"""
    failed = False
    failed |= test_optimise_export_does_not_deepcopy_the_windows(my_predbat)
    failed |= test_optimise_export_leaves_the_callers_windows_untouched(my_predbat)
    failed |= test_optimise_export_floor_clamped_below_reserved_range(my_predbat)
    return failed


def build_windows(my_predbat):
    """Set up a single-export-window scenario whose windows count their own deep copies"""
    export_window_best, record_export_windows, end_record = setup_single_export_window(my_predbat)
    windows = [CountingWindow(window) for window in export_window_best]
    return windows, record_export_windows, end_record


def test_optimise_export_does_not_deepcopy_the_windows(my_predbat):
    """The export window list is copied shallowly, so the window dicts are not duplicated"""
    print("**** test_optimise_export_does_not_deepcopy_the_windows ****")
    failed = False

    windows, record_export_windows, end_record = build_windows(my_predbat)
    my_predbat.export_window_best = windows
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []

    CountingWindow.deepcopy_count = 0
    my_predbat.optimise_export(0, record_export_windows, [], [], windows, [0.0], end_record=end_record)

    if CountingWindow.deepcopy_count != 0:
        print("ERROR: optimise_export deep-copied the export windows {} times".format(CountingWindow.deepcopy_count))
        failed = True

    my_predbat.export_window_best = []
    if not failed:
        print("PASS")
    return failed


def test_optimise_export_floor_clamped_below_reserved_range(my_predbat):
    """optimise_export's own floor clamp (plan.py, GH#4914) must never hand a candidate of 99 to
    the simulation.

    calc_percent_limit(best_soc_min, soc_max) rounding to 99 would otherwise raise the floor to 99,
    which packs with a low-power rung's fraction into [99.0, 100.0) - a value that reads as neither
    a freeze (not == 99.0) nor a forced export (not < 99.0), silently disabling the window. This
    hits the ladder's floor rather than clip_export_slots' post-simulation clamp (already covered in
    test_clip_export_slots.py), so it needs its own case.

    launch_run_prediction_export is monkeypatched to record every this_export_limit it is asked to
    simulate, rather than asserting on the optimiser's eventual winner - the winner is a metric-based
    choice among many candidates, but every candidate that reaches simulation must already be clamped.
    """
    print("**** test_optimise_export_floor_clamped_below_reserved_range ****")
    failed = False

    windows, record_export_windows, end_record = build_windows(my_predbat)
    my_predbat.export_window_best = windows
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.set_export_freeze = True
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = True
    # 9.9 on the fixture's 10.0 kWh battery rounds to calc_percent_limit -> 99, the boundary case
    # the clamp exists for.
    my_predbat.best_soc_min = 9.9

    simulated_limits = []
    real_launch = my_predbat.launch_run_prediction_export

    def fake_launch(this_export_limit, *args, **kwargs):
        simulated_limits.append(this_export_limit)
        return real_launch(this_export_limit, *args, **kwargs)

    my_predbat.launch_run_prediction_export = fake_launch
    try:
        my_predbat.optimise_export(0, record_export_windows, [], [], windows, [0.0], end_record=end_record)
    finally:
        my_predbat.launch_run_prediction_export = real_launch

    reserved = [limit for limit in simulated_limits if 99.0 < limit < 100.0]
    if reserved:
        print("ERROR: optimise_export simulated candidate(s) in the reserved range: {}".format(sorted(set(reserved))))
        failed = True

    # The clamp exists to be reached, not just avoided: confirm a real candidate was actually
    # floored to 98 rather than the whole 99-floor branch going untested.
    if not any(98.0 <= limit < 99.0 for limit in simulated_limits):
        print("ERROR: no candidate reached the 98% clamped floor - test setup does not exercise the fix, got {}".format(sorted(set(simulated_limits))))
        failed = True

    my_predbat.export_window_best = []
    my_predbat.best_soc_min = 0
    if not failed:
        print("PASS")
    return failed


def test_optimise_export_leaves_the_callers_windows_untouched(my_predbat):
    """The caller's window dicts must come back exactly as they went in.

    This is what the deepcopy was defending. _prepare_export now applies the trial start to its own
    copy instead, so if that ever reverts to an in-place write this fails rather than silently
    corrupting every other trial sharing the list.
    """
    print("**** test_optimise_export_leaves_the_callers_windows_untouched ****")
    failed = False

    windows, record_export_windows, end_record = build_windows(my_predbat)
    my_predbat.export_window_best = windows
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []

    before = [dict(window) for window in windows]
    identities = [id(window) for window in windows]

    my_predbat.optimise_export(0, record_export_windows, [], [], windows, [0.0], end_record=end_record)

    after = [dict(window) for window in windows]
    if after != before:
        print("ERROR: optimise_export modified the caller's windows")
        print("  before {}".format(before))
        print("  after  {}".format(after))
        failed = True
    if [id(window) for window in windows] != identities:
        print("ERROR: optimise_export replaced entries in the caller's window list")
        failed = True

    my_predbat.export_window_best = []
    if not failed:
        print("PASS")
    return failed
