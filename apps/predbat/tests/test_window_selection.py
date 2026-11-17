# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for select_window_candidates and distinct_slot_trials, the window picker and slot sweep in plan.py.

optimise_charge_limit_price_threads picks charge and export windows by walking a price-ordered
candidate list and taking the first max_slots that qualify. The search tries many slot counts
against the same threshold, so the picker is memoised on the unbounded selection and sliced per
slot count. That is only sound because of the prefix property: capping at max_slots takes the
first max_slots of the unbounded selection and nothing else. test_prefix_property_matches_capped_scan
pins exactly that against a reference implementation of the original capped loop, so if a future
change makes the cap interact with the dedup or the accept filter, it fails here rather than
silently changing plans.

The sweep itself then skips slot counts that would only select the same windows again
(distinct_slot_trials). test_distinct_slot_trials_match_full_sweep pins that against the full
four-deep sweep: the trials that survive must be exactly the ones that were the first to select each
set of windows, in the same order, because the search breaks ties on the first trial it sees.
"""

from plan import distinct_slot_trials, select_window_candidates, slots_around

# The slot length lists optimise_charge_limit_price_threads sweeps (local to that function)
COARSE_SLOT_LENGTHS = [32, 16, 8, 4, 2, 1, 0]
FINE_SLOT_LENGTHS = [48, 32, 24, 16, 14, 12, 10, 8, 6, 5, 4, 3, 2, 1, 0]


def run_window_selection_tests(my_predbat):
    """Run the window selection picker tests"""
    failed = False
    failed |= test_takes_windows_passing_the_threshold_in_order()
    failed |= test_skips_freeze_windows_when_freeze_not_allowed()
    failed |= test_deduplicates_keeping_the_first_occurrence()
    failed |= test_rejected_window_is_retried_on_a_later_entry()
    failed |= test_prefix_property_matches_capped_scan()
    failed |= test_distinct_slot_trials_drop_repeats()
    failed |= test_distinct_slot_trials_match_full_sweep()
    return failed


def capped_reference(entries, price_selected, allow_freeze, max_slots, accept=None):
    """The original capped selection loop from optimise_charge_limit_price_threads.

    Mirrors the predicate order of the export scan exactly - cap first, then dedup, then the
    accept filter - so the prefix property is checked against what the optimiser used to do
    rather than against a restatement of the new implementation.
    """
    chosen = []
    mods = {}
    count = 0
    for price, window_n, freeze in entries:
        if price_selected(price):
            if freeze and not allow_freeze:
                pass
            elif count < max_slots and (window_n not in mods):
                if accept is not None and not accept(window_n):
                    pass
                else:
                    chosen.append(window_n)
                    mods[window_n] = freeze
                    count += 1
    return chosen, mods


def test_takes_windows_passing_the_threshold_in_order():
    """Windows whose price passes the threshold are taken in candidate-list order"""
    print("**** test_takes_windows_passing_the_threshold_in_order ****")
    failed = False

    entries = [[10.0, 0, False], [12.0, 1, False], [8.0, 2, False]]
    # Charge side semantics: take everything at or below the loop price
    got = select_window_candidates(entries, lambda price: 10.0 >= price, True)
    expect = [(0, False), (2, False)]
    if got != expect:
        print("ERROR: expected {} but got {}".format(expect, got))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_skips_freeze_windows_when_freeze_not_allowed():
    """A freeze candidate is only taken when freeze is enabled for this trial"""
    print("**** test_skips_freeze_windows_when_freeze_not_allowed ****")
    failed = False

    entries = [[10.0, 0, True], [10.0, 1, False]]

    got = select_window_candidates(entries, lambda price: True, False)
    if got != [(1, False)]:
        print("ERROR: freeze disabled should drop window 0, got {}".format(got))
        failed = True

    got = select_window_candidates(entries, lambda price: True, True)
    if got != [(0, True), (1, False)]:
        print("ERROR: freeze enabled should keep both, got {}".format(got))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_deduplicates_keeping_the_first_occurrence():
    """A window listed twice is taken once, with the freeze flag of its first entry"""
    print("**** test_deduplicates_keeping_the_first_occurrence ****")
    failed = False

    entries = [[10.0, 5, False], [11.0, 6, False], [12.0, 5, True]]
    got = select_window_candidates(entries, lambda price: True, True)
    expect = [(5, False), (6, False)]
    if got != expect:
        print("ERROR: expected {} but got {}".format(expect, got))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_rejected_window_is_retried_on_a_later_entry():
    """The accept filter runs after the dedup check, so a window it rejects is not marked as seen.

    The original loop only recorded accepted windows, so a rejected window was re-tested when it
    appeared again. Recording rejects as seen would be a behaviour change, so it is pinned here.
    """
    print("**** test_rejected_window_is_retried_on_a_later_entry ****")
    failed = False

    calls = []

    def accept(window_n):
        """Reject window 7 the first time it is offered, accept it afterwards"""
        calls.append(window_n)
        return not (window_n == 7 and calls.count(7) == 1)

    entries = [[10.0, 7, False], [11.0, 7, False]]
    got = select_window_candidates(entries, lambda price: True, True, accept=accept)
    if got != [(7, False)]:
        print("ERROR: expected the second entry to be retried and taken, got {}".format(got))
        failed = True
    if calls != [7, 7]:
        print("ERROR: expected the accept filter to be offered window 7 twice, got {}".format(calls))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prefix_property_matches_capped_scan():
    """Capping at max_slots takes exactly the first max_slots of the unbounded selection.

    This is the invariant the memoisation depends on: the optimiser slices one unbounded list per
    price threshold instead of rescanning per slot count. Checked against capped_reference across
    duplicate windows, freeze flags and a rejecting accept filter, since those are the three things
    that could make the cap interact with the rest of the scan.
    """
    print("**** test_prefix_property_matches_capped_scan ****")
    failed = False

    entries = [
        [5.0, 0, False],
        [6.0, 1, True],
        [7.0, 2, False],
        [7.0, 0, True],
        [8.0, 3, False],
        [9.0, 4, True],
        [10.0, 5, False],
        [11.0, 2, False],
        [12.0, 6, False],
    ]

    def reject_even(window_n):
        """Reject window 4 so the accept filter is exercised inside the cap"""
        return window_n != 4

    for allow_freeze in (True, False):
        for accept in (None, reject_even):
            unbounded = select_window_candidates(entries, lambda price: 12.0 >= price, allow_freeze, accept=accept)
            for max_slots in range(0, 10):
                expect_chosen, expect_mods = capped_reference(entries, lambda price: 12.0 >= price, allow_freeze, max_slots, accept=accept)
                sliced = unbounded[:max_slots]
                got_chosen = [window_n for window_n, _ in sliced]
                got_mods = dict(sliced)
                if got_chosen != expect_chosen or got_mods != expect_mods:
                    print("ERROR: freeze={} accept={} max_slots={} prefix gave {} / {} but capped scan gave {} / {}".format(allow_freeze, accept is not None, max_slots, got_chosen, got_mods, expect_chosen, expect_mods))
                    failed = True

    if not failed:
        print("PASS")
    return failed


def test_distinct_slot_trials_drop_repeats():
    """Slot counts at or above the candidate count collapse onto the first one that reaches it"""
    print("**** test_distinct_slot_trials_drop_repeats ****")
    failed = False

    cases = [
        # Three candidates: 32, 16, 8 and 4 all take all three, so only 32 is kept
        ("one freeze option", COARSE_SLOT_LENGTHS, {False: 3}, [(32, [False]), (2, [False]), (1, [False]), (0, [False])]),
        # No candidates: every slot count selects nothing, the first one stands for all of them
        ("no candidates", COARSE_SLOT_LENGTHS, {False: 0}, [(32, [False])]),
        # More candidates than the longest slot count: nothing repeats
        ("nothing to drop", COARSE_SLOT_LENGTHS, {False: 40}, [(slots, [False]) for slots in COARSE_SLOT_LENGTHS]),
        # Freeze adds candidates, so 4 is new with freeze (4 of 5) and a repeat without (2 of 2)
        ("per freeze option", [8, 4, 2, 1, 0], {True: 5, False: 2}, [(8, [True, False]), (4, [True]), (2, [True]), (1, [True, False]), (0, [True, False])]),
        ("no slot choices", [], {True: 5, False: 2}, []),
    ]
    for name, slot_choices, candidate_counts, expect in cases:
        got = distinct_slot_trials(slot_choices, candidate_counts)
        if got != expect:
            print("ERROR: {}: expected {} but got {}".format(name, expect, got))
            failed = True

    if not failed:
        print("PASS")
    return failed


def full_sweep_first_trials(charge_choices, export_choices, charge_counts, export_counts):
    """The trials the original four-deep sweep ran predictions for, in order.

    Walks every (charge slots, export slots, charge freeze, export freeze) combination the way
    optimise_charge_limit_price_threads used to and keeps the first one to reach each selection, which
    is what its tried_list check let through. A selection is identified by the effective slot count and
    freeze option on each side, since that is all the selected windows depend on.
    """
    seen = set()
    trials = []
    for max_charge_slots in charge_choices:
        for max_export_slots in export_choices:
            for charge_freeze in charge_counts:
                for export_freeze in export_counts:
                    selection = (min(max_charge_slots, charge_counts[charge_freeze]), charge_freeze, min(max_export_slots, export_counts[export_freeze]), export_freeze)
                    if selection not in seen:
                        seen.add(selection)
                        trials.append((max_charge_slots, max_export_slots, charge_freeze, export_freeze))
    return trials


def test_distinct_slot_trials_match_full_sweep():
    """The reduced sweep runs exactly the trials the full sweep ran predictions for, in the same order.

    Both the trial and its position matter. The recorded slot counts of the winning trial centre the
    fine pass (slots_around), and equal-metric trials are settled by which came first, so a reduced
    sweep that kept a later repeat instead of the first would change plans without changing the set
    of scenarios it simulated.
    """
    print("**** test_distinct_slot_trials_match_full_sweep ****")
    failed = False

    slot_lists = [COARSE_SLOT_LENGTHS, FINE_SLOT_LENGTHS, slots_around(0, FINE_SLOT_LENGTHS), slots_around(3, FINE_SLOT_LENGTHS), slots_around(16, FINE_SLOT_LENGTHS)]
    count_sets = [{False: 0}, {False: 3}, {False: 60}, {True: 0}, {True: 7}, {True: 5, False: 2}, {True: 13, False: 13}, {True: 50, False: 9}, {True: 1, False: 0}]

    checked = 0
    for charge_choices in slot_lists:
        for export_choices in slot_lists:
            for charge_counts in count_sets:
                for export_counts in count_sets:
                    expect = full_sweep_first_trials(charge_choices, export_choices, charge_counts, export_counts)
                    got = []
                    for max_charge_slots, charge_freeze_trials in distinct_slot_trials(charge_choices, charge_counts):
                        for max_export_slots, export_freeze_trials in distinct_slot_trials(export_choices, export_counts):
                            for charge_freeze in charge_freeze_trials:
                                for export_freeze in export_freeze_trials:
                                    got.append((max_charge_slots, max_export_slots, charge_freeze, export_freeze))
                    checked += 1
                    if got != expect:
                        print("ERROR: charge {} / {} export {} / {} reduced sweep ran {} but the full sweep ran {}".format(charge_choices, charge_counts, export_choices, export_counts, got, expect))
                        failed = True

    if checked != len(slot_lists) ** 2 * len(count_sets) ** 2:
        print("ERROR: expected to check {} combinations but checked {}".format(len(slot_lists) ** 2 * len(count_sets) ** 2, checked))
        failed = True

    if not failed:
        print("PASS")
    return failed
