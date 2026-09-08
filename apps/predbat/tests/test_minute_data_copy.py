# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for when minute_data copies the history it is given.

minute_data used to deep-copy every history list it was not explicitly allowed to modify, which
on a plan cycle is ~150k deepcopy calls for the two calculate_yesterday calls alone. The only code
that writes to history is the glitch filter, and that runs only for clean_increment and backwards,
so the copy is now taken only when the filter can actually run.

That makes "did it copy?" a behaviour worth pinning in both directions: no copy when nothing can be
written, and the caller's data still untouched when the filter does run. A dict subclass counts its
own deep copies so the first of those is observable rather than merely faster.
"""
from datetime import datetime

import pytz

from utils import minute_data

UTC = pytz.UTC
NOW = datetime(2024, 10, 4, 12, 5, 0, tzinfo=UTC)


class CountingHistoryItem(dict):
    """A history entry that records how many times it has been deep-copied"""

    deepcopy_count = 0

    def __deepcopy__(self, memo):
        """Record the copy and return an independent duplicate"""
        CountingHistoryItem.deepcopy_count += 1
        duplicate = CountingHistoryItem(self)
        memo[id(self)] = duplicate
        return duplicate


def glitch_history():
    """History whose middle sample is a spike the glitch filter rewrites.

    prev_prev=10, prev=50, value=11 satisfies every arm of the filter's test, so it rewrites the
    50 sample down to 11 - which is the write the copy exists to keep away from the caller.
    """
    return [
        CountingHistoryItem({"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"}),
        CountingHistoryItem({"state": "50.0", "last_updated": "2024-10-04T11:20:00+00:00"}),
        CountingHistoryItem({"state": "11.0", "last_updated": "2024-10-04T11:40:00+00:00"}),
        CountingHistoryItem({"state": "12.0", "last_updated": "2024-10-04T12:00:00+00:00"}),
    ]


def run_minute_data_copy_tests(my_predbat):
    """Run the minute_data history copying tests"""
    failed = False
    failed |= test_no_copy_when_the_glitch_filter_cannot_run()
    failed |= test_no_copy_when_clean_increment_is_not_backwards()
    failed |= test_glitch_filter_does_not_write_to_the_callers_history()
    failed |= test_can_modify_history_writes_through_to_the_caller()
    return failed


def test_no_copy_when_the_glitch_filter_cannot_run():
    """With clean_increment off nothing can write to history, so it is not copied"""
    print("**** test_no_copy_when_the_glitch_filter_cannot_run ****")
    failed = False

    history = glitch_history()
    CountingHistoryItem.deepcopy_count = 0
    minute_data(history=history, days=1, now=NOW, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False, clean_increment=False)

    if CountingHistoryItem.deepcopy_count != 0:
        print("ERROR: history was deep-copied {} times when the glitch filter could not run".format(CountingHistoryItem.deepcopy_count))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_copy_when_clean_increment_is_not_backwards():
    """The glitch filter needs both clean_increment and backwards, so forward data is not copied"""
    print("**** test_no_copy_when_clean_increment_is_not_backwards ****")
    failed = False

    history = glitch_history()
    CountingHistoryItem.deepcopy_count = 0
    minute_data(history=history, days=1, now=NOW, state_key="state", last_updated_key="last_updated", backwards=False, smoothing=False, clean_increment=True)

    if CountingHistoryItem.deepcopy_count != 0:
        print("ERROR: history was deep-copied {} times for a forward clean_increment run".format(CountingHistoryItem.deepcopy_count))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_glitch_filter_does_not_write_to_the_callers_history():
    """When the filter does run it must rewrite a copy, leaving the caller's samples alone"""
    print("**** test_glitch_filter_does_not_write_to_the_callers_history ****")
    failed = False

    history = glitch_history()
    minute_data(history=history, days=1, now=NOW, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False, clean_increment=True)

    states = [item["state"] for item in history]
    expect = ["10.0", "50.0", "11.0", "12.0"]
    if states != expect:
        print("ERROR: the caller's history was modified, expected {} but got {}".format(expect, states))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_can_modify_history_writes_through_to_the_caller():
    """can_modify_history is the caller promising it will not read the data again.

    Pinned so that "always copy" cannot come back as a fix - the callers passing this flag do so to
    avoid the copy, and the flag silently doing nothing would be invisible otherwise.
    """
    print("**** test_can_modify_history_writes_through_to_the_caller ****")
    failed = False

    history = glitch_history()
    CountingHistoryItem.deepcopy_count = 0
    minute_data(history=history, days=1, now=NOW, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False, clean_increment=True, can_modify_history=True)

    if CountingHistoryItem.deepcopy_count != 0:
        print("ERROR: history was copied {} times despite can_modify_history".format(CountingHistoryItem.deepcopy_count))
        failed = True
    if history[1]["state"] != 11.0:
        print("ERROR: expected the glitch sample to be rewritten to 11.0 in place, got {}".format(history[1]["state"]))
        failed = True

    if not failed:
        print("PASS")
    return failed
