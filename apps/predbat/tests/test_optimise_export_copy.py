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
