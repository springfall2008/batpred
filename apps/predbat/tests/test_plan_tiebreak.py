# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the near-tie plan fragmentation tie-break used in calculate_plan().

When a freshly optimised plan is not better than the incumbent by metric_min_improvement_plan,
Predbat keeps the incumbent. Under flat export rates that locks in a fragmented (split) export
schedule even though the fresh plan is a cleaner single block. The tie-break lets the cleaner
plan win a near-tie provided it is no worse on cost.
"""


def _check(cond, message, failures):
    """Record a failure message if the condition is not met."""
    if not cond:
        print("ERROR: {}".format(message))
        failures.append(message)


def _fragmentation_tests(my_predbat, failures):
    """plan_fragmentation counts contiguous active (charge/export) segments split by gaps/mode changes."""
    my_predbat.reserve = 0.4

    # Three adjacent 30-minute export windows all discharging = one contiguous block = 1 segment.
    ew = [{"start": 0, "end": 30}, {"start": 30, "end": 60}, {"start": 60, "end": 90}]
    _check(my_predbat.plan_fragmentation([], [], ew, [0.0, 0.0, 0.0]) == 1, "contiguous export block should be 1 segment", failures)

    # Same span but the middle slot is off (100) -> a time gap -> two separate export runs = 2 segments.
    _check(my_predbat.plan_fragmentation([], [], ew, [0.0, 100.0, 0.0]) == 2, "export split by an idle slot should be 2 segments", failures)

    # Staircase targets (all discharging, all adjacent) is still one block - differing depth is not a split.
    _check(my_predbat.plan_fragmentation([], [], ew, [28.0, 7.0, 6.0]) == 1, "adjacent discharge windows are one segment regardless of depth", failures)

    # Freeze (99) and off (100) are not battery export, so they are inactive -> 0 segments.
    _check(my_predbat.plan_fragmentation([], [], ew, [99.0, 100.0, 99.0]) == 0, "freeze/off export windows are inactive", failures)

    # A charge window (target above reserve) adjacent to an export window is a mode change = 2 segments.
    cw = [{"start": 0, "end": 30}]
    ew2 = [{"start": 30, "end": 60}]
    _check(my_predbat.plan_fragmentation(cw, [5.0], ew2, [0.0]) == 2, "adjacent charge then export is a mode change = 2 segments", failures)

    # A charge window at/below reserve is not charging -> inactive.
    _check(my_predbat.plan_fragmentation([{"start": 0, "end": 30}], [0.4], [], []) == 0, "charge target at reserve is inactive", failures)

    # The real scenario: split evening (3 runs with gaps) must be more fragmented than the merged block (1 run).
    split_ew = [{"start": 1250, "end": 1350}, {"start": 1370, "end": 1410}, {"start": 1425, "end": 1440}]
    merged_ew = [{"start": 1250, "end": 1410}]
    frag_split = my_predbat.plan_fragmentation([], [], split_ew, [28.0, 7.0, 6.0])
    frag_merged = my_predbat.plan_fragmentation([], [], merged_ew, [0.0])
    _check(frag_split > frag_merged, "split evening ({}) should be more fragmented than merged ({})".format(frag_split, frag_merged), failures)


def _tiebreak_decision_tests(my_predbat, failures):
    """should_replace_plan: adopt new on a clear win, or on a near-tie if no worse and strictly cleaner."""
    my_predbat.metric_min_improvement_plan = 2.0

    # Clearly better (gap >= threshold) always adopts the new plan, regardless of fragmentation.
    _check(my_predbat.should_replace_plan(-100.0, -103.0, 1, 5) is True, "clear improvement adopts new plan", failures)

    # Real near-tie from the trefor2 case: prev split -224.78, new merged -226.57 (1.79p better), cleaner.
    _check(my_predbat.should_replace_plan(-224.78, -226.57, 5, 1) is True, "near-tie cleaner+cheaper plan adopts new", failures)

    # Near-tie but equally fragmented -> keep previous (preserves anti-jitter hysteresis).
    _check(my_predbat.should_replace_plan(-224.78, -226.57, 3, 3) is False, "near-tie equal fragmentation keeps previous", failures)

    # Near-tie, new is cleaner but MORE expensive (gap < 0) -> keep previous (never raise cost).
    _check(my_predbat.should_replace_plan(-226.57, -224.78, 5, 1) is False, "cleaner but costlier plan is rejected", failures)

    # Near-tie, new is cheaper but MORE fragmented -> keep previous.
    _check(my_predbat.should_replace_plan(-224.78, -225.0, 1, 4) is False, "cheaper but more fragmented plan is rejected", failures)

    # Exactly cost-neutral (gap == 0) but cleaner -> adopt new.
    _check(my_predbat.should_replace_plan(-200.0, -200.0, 4, 2) is True, "cost-neutral cleaner plan adopts new", failures)


def run_plan_tiebreak_tests(my_predbat):
    """Run the plan fragmentation tie-break tests. Returns True on failure."""
    print("**** Running plan tie-break tests ****")
    failures = []
    _fragmentation_tests(my_predbat, failures)
    _tiebreak_decision_tests(my_predbat, failures)
    return len(failures) > 0
