# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for Octopus.build_dispatch_timeline() and dispatch_timeline_should_log() - the #4516
Stage 1 diagnostic dispatch-timeline render and its heartbeat/change-triggered logging decision.
Purely observational (not used for any rate/plan decision yet), so these tests only check the
rendered status string and the logging decision, not any side effect on rates or the plan.
"""

from datetime import datetime, timedelta

import pytz

UTC = pytz.UTC
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _slot(start, end):
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "source": "smart-charge", "location": "AT_HOME"}


def _bare_slot(start, end):
    """A slot with only start/end - the real shape of a started_dispatches entry from the HA
    Octopus Energy integration (no kWh, source or location)."""
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT)}


def run_dispatch_timeline_tests(my_predbat):
    """
    Test for build_dispatch_timeline() - the #4516 Stage 1 diagnostic dispatch-timeline render.
    """
    failed = False
    print("**** Running dispatch_timeline tests ****")

    # Save state that will be mutated
    saved_now_utc = my_predbat.now_utc
    saved_midnight_utc = my_predbat.midnight_utc
    saved_minutes_now = my_predbat.minutes_now
    saved_dispatch_timeline_last = my_predbat.dispatch_timeline_last
    saved_charge_window_best = my_predbat.charge_window_best
    saved_charge_limit_best = my_predbat.charge_limit_best

    try:
        # Fixed, mutually-consistent midnight/now so this test doesn't depend on (or leak into)
        # whatever the ambient fixture state happens to be elsewhere in a full suite run.
        midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        my_predbat.midnight_utc = midnight_utc
        my_predbat.now_utc = midnight_utc + timedelta(hours=10)
        my_predbat.minutes_now = 10 * 60

        # Default window: -4h..+24h at 30-min step = 56 blocks. "now" (offset 0) sits at block 8 (4h * 2).
        NUM_BLOCKS = 56
        NOW_BLOCK = 8

        # No plan by default: the existing cases below assert uppercase letters, which only hold
        # when Predbat is not charging in those blocks
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []

        print("Test 1: nothing scheduled - all dots")
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if timeline != "." * NUM_BLOCKS:
            print("  ERROR: expected all dots, got {!r}".format(timeline))
            failed = True

        print("Test 2: a single future planned slot renders as P at the right offset")
        slot_start = midnight_utc + timedelta(hours=12)  # 2h from now (now=10:00) -> offset block NOW_BLOCK+4
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK + 4] = "P"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 3: a currently-active started slot renders as S")
        slot_start = midnight_utc + timedelta(hours=10)  # now
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [_slot(slot_start, slot_end)], [])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK] = "S"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 4: a past completed slot renders as C, placed before the 'now' column")
        slot_start = midnight_utc + timedelta(hours=9)  # 1h before now -> offset block NOW_BLOCK-2
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [_slot(slot_start, slot_end)], [], [])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK - 2] = "C"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 5: when completed, started and planned all cover the same block, completed wins")
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(minutes=30)
        slot = _slot(slot_start, slot_end)
        timeline = my_predbat.build_dispatch_timeline(0, [slot], [slot], [slot])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK + 4] = "C"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: expected completed to win over started/planned, got {!r}".format(timeline))
            failed = True

        print("Test 6: started wins over planned when completed is absent")
        timeline = my_predbat.build_dispatch_timeline(0, [], [slot], [slot])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK + 4] = "S"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: expected started to win over planned, got {!r}".format(timeline))
            failed = True

        print("Test 7: a slot entirely outside the window has no effect and does not crash")
        slot_start = midnight_utc + timedelta(days=3)  # far beyond +24h
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        if timeline != "." * NUM_BLOCKS:
            print("  ERROR: expected all dots for an out-of-window slot, got {!r}".format(timeline))
            failed = True

        print("Test 8: a slot spanning multiple 30-min blocks marks every block it touches")
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(hours=1, minutes=30)  # 3 blocks
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK + 4] = "P"
        expected[NOW_BLOCK + 5] = "P"
        expected[NOW_BLOCK + 6] = "P"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: expected 3 consecutive P blocks, got {!r}".format(timeline))
            failed = True
        print("Test 9: a planned slot Predbat charges in renders lowercase")
        slot_start = midnight_utc + timedelta(hours=12)  # 2h from now -> block NOW_BLOCK+4
        slot_end = slot_start + timedelta(minutes=30)
        my_predbat.charge_window_best = [{"start": 12 * 60, "end": 12 * 60 + 30}]
        my_predbat.charge_limit_best = [5.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK + 4] = "p"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: a slot Predbat plans to charge in should be 'p', expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 10: a charge window with a zero limit does not lowercase the slot")
        my_predbat.charge_limit_best = [0.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK + 4] = "P"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: a zero-limit window is not a charge, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 11: a bare started slot (start/end only, no kWh/source/location) still renders as S")
        # Copilot review on #4948: real started_dispatches entries from the HA Octopus Energy
        # integration carry only start/end. decode_octopus_slot()'s "remove empty slots" check
        # (no kWh, source or location) would otherwise decode a real one as empty, so 'S' could
        # never render for real data - every other test here uses _slot(), which adds source and
        # location and masked this.
        slot_start = midnight_utc + timedelta(hours=10)  # now
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [_bare_slot(slot_start, slot_end)], [])
        expected = list("." * NUM_BLOCKS)
        expected[NOW_BLOCK] = "S"
        expected = "".join(expected)
        if timeline != expected:
            print("  ERROR: a bare start/end-only started slot should still render as S, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 12: a charge window where there is no slot leaves dots untouched")
        my_predbat.charge_window_best = [{"start": 14 * 60, "end": 14 * 60 + 30}]
        my_predbat.charge_limit_best = [5.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if timeline != "." * NUM_BLOCKS:
            print("  ERROR: charging where no dispatch exists must not mark the timeline, got {!r}".format(timeline))
            failed = True

        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []

        # ------------------------------------------------------------------
        # dispatch_timeline_should_log() - #4948 review: a heartbeat every 30 minutes alone would
        # miss a provisional slot that appears and disappears entirely within one half-hour window.
        my_predbat.dispatch_timeline_last = {}

        print("Test 13: minute 0 (a 30-min boundary) always logs, unmarked, as a heartbeat")
        my_predbat.minutes_now = 10 * 60  # 10:00, a boundary
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "....")
        if not (should_log and marker == ""):
            print("  ERROR: expected a heartbeat to log with no marker, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 14: off-boundary with no change since the last log does not log")
        my_predbat.minutes_now = 10 * 60 + 5  # not a boundary
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "....")
        if should_log:
            print("  ERROR: an unchanged timeline off-boundary should not log, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 15: off-boundary with a change logs immediately, marked")
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "...P")
        if not (should_log and marker == " *"):
            print("  ERROR: a changed timeline off-boundary should log marked ' *', got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 16: the same changed timeline does not log again next cycle")
        my_predbat.minutes_now += 5
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "...P")
        if should_log:
            print("  ERROR: a timeline unchanged since the change-triggered log should not log again, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 17: cars are tracked independently")
        my_predbat.minutes_now = 11 * 60 + 5  # off-boundary, car 0 already logged "...P"
        should_log, marker = my_predbat.dispatch_timeline_should_log(1, "...P")
        if not (should_log and marker == " *"):
            print("  ERROR: a second car's first-seen timeline should log independently of car 0's, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        my_predbat.dispatch_timeline_last = {}

    finally:
        my_predbat.now_utc = saved_now_utc
        my_predbat.midnight_utc = saved_midnight_utc
        my_predbat.minutes_now = saved_minutes_now
        my_predbat.dispatch_timeline_last = saved_dispatch_timeline_last
        my_predbat.charge_window_best = saved_charge_window_best
        my_predbat.charge_limit_best = saved_charge_limit_best

    if failed:
        print("\n**** dispatch_timeline tests: FAILED ****")
    else:
        print("\n**** dispatch_timeline tests: PASSED ****")

    return failed
