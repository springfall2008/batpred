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
Unit tests for Octopus.build_dispatch_timeline() - the #4516 Stage 1 diagnostic dispatch-timeline
render helper. Purely observational (not used for any rate/plan decision yet), so these tests only
check the rendered status string, not any side effect on rates or the plan.
"""

from datetime import datetime, timedelta

import pytz

UTC = pytz.UTC
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _slot(start, end):
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "source": "smart-charge", "location": "AT_HOME"}


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
    finally:
        my_predbat.now_utc = saved_now_utc
        my_predbat.midnight_utc = saved_midnight_utc
        my_predbat.minutes_now = saved_minutes_now

    if failed:
        print("\n**** dispatch_timeline tests: FAILED ****")
    else:
        print("\n**** dispatch_timeline tests: PASSED ****")

    return failed
