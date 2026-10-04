# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for PredBat._capture_debug_history()'s throttling/force-capture logic (#4417).

Mirrors test_plan_persistence.py's pattern for testing storage-backed predbat.py
methods: a real StorageComponent/StorageLocalFiles backend wrapped in a minimal
_MockComponents shim, rather than mocking the storage calls themselves.
"""

import os
import shutil
import tempfile
from datetime import timedelta

from debug_history import list_snapshots
from storage import StorageComponent, StorageLocalFiles
from tests.test_infra import run_async


class _MockComponents:
    """Minimal components mock returning a pre-configured storage component."""

    def __init__(self, storage):
        """Initialise with a storage instance (may be None to simulate unavailable)."""
        self._storage = storage

    def get_component(self, name):
        """Return the mocked storage for 'storage', None for everything else."""
        if name == "storage":
            return self._storage
        return None


def _make_storage(predbat, tmpdir):
    """Create a StorageComponent backed by a real local-file backend in tmpdir."""
    storage = StorageComponent(predbat)
    storage.backend = StorageLocalFiles(tmpdir, predbat.log)
    return storage


def test_debug_history_capture(my_predbat):
    """Test _capture_debug_history()'s interval throttle, off-switch, and force-capture override."""
    failed = 0
    print("--- Debug history capture tests ---")

    tmpdir = tempfile.mkdtemp()
    try:
        storage = _make_storage(my_predbat, tmpdir)
        my_predbat.components = _MockComponents(storage)
        # debug_history_enable/count/interval/force_capture are switch/input_number CONFIG_ITEMS -
        # get_arg() resolves those via config_index (see get_ha_config()), not self.args
        # directly, so they must be set the same way the running app itself would (and the
        # same way _capture_debug_history() resets the force-capture switch): expose_config().
        my_predbat.expose_config("debug_history_enable", True)
        my_predbat.expose_config("debug_history_count", 15)
        my_predbat.expose_config("debug_history_interval", 3)
        my_predbat.expose_config("debug_history_force_capture", False)
        my_predbat.debug_history_last_capture = None

        print("Test 1: first call (no prior capture) captures immediately")
        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        if len(index) != 1:
            print("  FAILED: expected 1 snapshot after the first call, got {}".format(len(index)))
            failed += 1
        if my_predbat.debug_history_last_capture != my_predbat.now_utc:
            print("  FAILED: debug_history_last_capture should be updated to now_utc")
            failed += 1
        debug_dir = os.path.join(tmpdir, "debug")
        mirrored = [f for f in os.listdir(debug_dir)] if os.path.isdir(debug_dir) else []
        if len(mirrored) != 1:
            print("  FAILED: expected _capture_debug_history() to also write one file into config_root/debug/ (#4720), got {}".format(mirrored))
            failed += 1

        print("Test 2: a second call well within the interval is skipped")
        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        if len(index) != 1:
            print("  FAILED: expected still 1 snapshot (throttled), got {}".format(len(index)))
            failed += 1

        print("Test 3: a call once the interval has elapsed captures again")
        my_predbat.now_utc = my_predbat.now_utc + timedelta(hours=4)
        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        if len(index) != 2:
            print("  FAILED: expected 2 snapshots after the interval elapsed, got {}".format(len(index)))
            failed += 1

        print("Test 4: debug_history_enable off disables routine capture entirely (zero storage calls)")
        my_predbat.expose_config("debug_history_enable", False)
        my_predbat.now_utc = my_predbat.now_utc + timedelta(hours=4)
        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        if len(index) != 2:
            print("  FAILED: debug_history_enable off should not add a snapshot, got {} entries".format(len(index)))
            failed += 1

        print("Test 5: debug_history_force_capture fires immediately even with debug_history_enable off and mid-interval, and resets the switch")
        my_predbat.expose_config("debug_history_force_capture", True)
        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        # debug_history_count (15) never changed, only the enable switch did - a forced capture
        # adds to the existing ring rather than truncating it, same as any other force capture.
        if len(index) != 3:
            print("  FAILED: forced capture with debug_history_enable off should add a 3rd snapshot, got {} entries".format(len(index)))
            failed += 1
        if my_predbat.get_arg("debug_history_force_capture", None) is not False:
            print("  FAILED: the force-capture switch should read back False after firing, got {}".format(my_predbat.get_arg("debug_history_force_capture", None)))
            failed += 1

        print("Test 6: a forced capture also resets the routine interval clock")
        if my_predbat.debug_history_last_capture != my_predbat.now_utc:
            print("  FAILED: a forced capture should update debug_history_last_capture too")
            failed += 1

        print("Test 6b: re-enabling debug_history_enable lets routine capture resume")
        # Advance by a full plan slot, not just any amount - the captured timestamp is floored to
        # the plan_interval_minutes grid (see _capture_debug_history()), so a smaller advance could
        # floor right back to test 5's slot and collide instead of producing a distinct id.
        my_predbat.now_utc = my_predbat.now_utc + timedelta(hours=4) + timedelta(minutes=my_predbat.plan_interval_minutes)
        my_predbat.expose_config("debug_history_enable", True)
        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        if len(index) != 4:
            print("  FAILED: routine capture should resume once debug_history_enable is back on, got {} entries".format(len(index)))
            failed += 1

        print("Test 7: no storage component available is handled without raising")
        my_predbat.components = _MockComponents(None)
        my_predbat.debug_history_last_capture = None  # force past the interval throttle so the storage-unavailable branch is actually exercised
        my_predbat.debug_history_storage_warned = None
        try:
            my_predbat._capture_debug_history()
        except Exception as e:
            print("  FAILED: _capture_debug_history() raised with no storage component: {}".format(e))
            failed += 1

        print("Test 8: the storage-unavailable warning is throttled, not re-logged every cycle (#4438 review item 2)")
        if my_predbat.debug_history_storage_warned != my_predbat.now_utc:
            print("  FAILED: expected debug_history_storage_warned to be set on the first no-storage call")
            failed += 1
        first_warned = my_predbat.debug_history_storage_warned
        my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=5)
        my_predbat._capture_debug_history()
        if my_predbat.debug_history_storage_warned != first_warned:
            print("  FAILED: expected debug_history_storage_warned to stay unchanged well within the interval, got a new value")
            failed += 1
        my_predbat.now_utc = my_predbat.now_utc + timedelta(hours=4)
        my_predbat._capture_debug_history()
        if my_predbat.debug_history_storage_warned != my_predbat.now_utc:
            print("  FAILED: expected debug_history_storage_warned to advance once the interval has elapsed")
            failed += 1

        print("Test 9: a failed capture does not advance debug_history_last_capture (#4438 review item 1)")
        storage = _make_storage(my_predbat, tmpdir)
        my_predbat.components = _MockComponents(storage)
        my_predbat.expose_config("debug_history_enable", True)
        my_predbat.now_utc = my_predbat.now_utc + timedelta(minutes=5)
        my_predbat.debug_history_last_capture = None
        last_capture_before = my_predbat.debug_history_last_capture
        original_create_debug_yaml = my_predbat.create_debug_yaml

        def _raise(*args, **kwargs):
            raise ValueError("simulated capture failure")

        my_predbat.create_debug_yaml = _raise
        try:
            my_predbat._capture_debug_history()
        finally:
            my_predbat.create_debug_yaml = original_create_debug_yaml
        if my_predbat.debug_history_last_capture != last_capture_before:
            print("  FAILED: expected debug_history_last_capture to stay unchanged after a failed capture, got {}".format(my_predbat.debug_history_last_capture))
            failed += 1

        print("Test 10: a failed forced capture leaves the switch on and retries next cycle (#4438 review item 3)")
        my_predbat.expose_config("debug_history_force_capture", True)
        my_predbat.create_debug_yaml = _raise
        try:
            my_predbat._capture_debug_history()
        finally:
            my_predbat.create_debug_yaml = original_create_debug_yaml
        if my_predbat.get_arg("debug_history_force_capture", None) is not True:
            print("  FAILED: expected the force-capture switch to stay on after a failed attempt, got {}".format(my_predbat.get_arg("debug_history_force_capture", None)))
            failed += 1
        # Now let it genuinely succeed - the still-on switch should retry and this time reset.
        my_predbat._capture_debug_history()
        if my_predbat.get_arg("debug_history_force_capture", None) is not False:
            print("  FAILED: expected the force-capture switch to reset once a retry actually succeeds")
            failed += 1

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed


def test_debug_history_capture_slot_alignment(my_predbat):
    """
    Test that a snapshot's stored timestamp is floored to the plan's own slot grid
    (midnight_utc + N * plan_interval_minutes), not the arbitrary moment the ~5-minute cycle
    happened to trigger the capture.

    This is what lets the plan History view match a snapshot to exactly the one row it
    corresponds to by a direct timestamp comparison, rather than a fuzzy nearest-within-a-window
    search that could let several adjacent rows all claim the same snapshot.
    """
    failed = 0
    print("--- Debug history capture slot-alignment test ---")

    tmpdir = tempfile.mkdtemp()
    try:
        storage = _make_storage(my_predbat, tmpdir)
        my_predbat.components = _MockComponents(storage)
        my_predbat.expose_config("debug_history_enable", True)
        my_predbat.expose_config("debug_history_count", 15)
        my_predbat.expose_config("debug_history_interval", 3)
        my_predbat.expose_config("debug_history_force_capture", False)
        my_predbat.debug_history_last_capture = None

        # Land deliberately mid-slot, not on a slot boundary, so a capture that skipped flooring
        # would produce a different timestamp than what's being asserted below.
        slot_minutes = my_predbat.plan_interval_minutes
        minutes_since_midnight = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() // 60)
        floored_minutes = (minutes_since_midnight // slot_minutes) * slot_minutes
        expected_slot_start = my_predbat.midnight_utc + timedelta(minutes=floored_minutes)
        my_predbat.now_utc = expected_slot_start + timedelta(minutes=min(7, slot_minutes - 1))

        my_predbat._capture_debug_history()
        index = run_async(list_snapshots(storage))
        if len(index) != 1:
            print("  FAILED: expected exactly 1 snapshot, got {}".format(len(index)))
            failed += 1
        else:
            stored_timestamp = index[0]["timestamp"]
            if stored_timestamp != expected_slot_start.isoformat():
                print("  FAILED: expected snapshot timestamp {} (floored to the slot boundary), got {}".format(expected_slot_start.isoformat(), stored_timestamp))
                failed += 1
            # The throttle-tracking timestamp is deliberately the real capture moment, not the
            # floored label - it governs how long until the next routine capture is due, which
            # should track real elapsed time regardless of how the snapshot itself gets labelled.
            if my_predbat.debug_history_last_capture != my_predbat.now_utc:
                print("  FAILED: debug_history_last_capture should track the real capture moment, not the floored slot start")
                failed += 1

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed


def test_debug_history_count_range(my_predbat):
    """Test debug_history_count's schema range reaches a multi-week window (#5070) and still clamps above it."""
    failed = 0
    print("--- Debug history snapshot count range tests ---")

    # The window a user can ask for is debug_history_count x debug_history_interval, and the
    # interval is itself capped at 24 hours, so the count is the only thing standing between the
    # default ~2-day buffer and the fortnight #5070 needed to audit intermittent optimiser
    # behaviour. 336 is that report's own worked example: 336 snapshots at a 1-hour interval.
    fortnight_hourly = 336

    item = my_predbat.config_index.get("debug_history_count", None)
    if item is None:
        print("  FAILED: debug_history_count config item not found")
        return failed + 1

    print("Test 1: the declared range reaches a fortnight of hourly snapshots while the default stays small")
    if item.get("max", 0) < fortnight_hourly:
        print("  FAILED: debug_history_count max should allow at least {} snapshots (14 days hourly), got {}".format(fortnight_hourly, item.get("max", None)))
        failed += 1
    # The default deliberately does not move with the maximum - raising the ceiling must not cost
    # storage on an install that never asked for a longer history (#5070 review discussion).
    if item.get("default", None) != 15:
        print("  FAILED: debug_history_count default should stay at 15, got {}".format(item.get("default", None)))
        failed += 1
    if item.get("min", None) != 1:
        print("  FAILED: debug_history_count min should stay at 1 (the enable switch is the off-switch), got {}".format(item.get("min", None)))
        failed += 1

    original_had_errors = my_predbat.had_errors
    original_value = item.get("value", item.get("default", 15))

    try:
        # load_user_config() clamps input_number items to their declared min/max, including values
        # that arrived via an apps.yaml override (which bypasses the HA entity's own enforcement),
        # so the schema bound above is what a user actually runs into - assert through that path
        # rather than on the dict alone, and read the result back the way _capture_debug_history()
        # does. See test_config_item_range_clamp in test_integer_config.py for the same pattern.
        print("Test 2: a fortnight's worth of hourly snapshots survives a config refresh unclamped")
        my_predbat.expose_config("debug_history_count", fortnight_hourly, force_ha=True)
        my_predbat.had_errors = False
        my_predbat.load_user_config()
        resolved = my_predbat.get_arg("debug_history_count", 15)
        if resolved != fortnight_hourly:
            print("  FAILED: expected debug_history_count {} to pass through unclamped, got {}".format(fortnight_hourly, resolved))
            failed += 1
        if my_predbat.had_errors is not False:
            print("  FAILED: an in-range debug_history_count should not flag had_errors")
            failed += 1

        print("Test 3: the ceiling is still enforced above the declared maximum")
        item_max = item.get("max", 0)
        my_predbat.expose_config("debug_history_count", item_max + 1, force_ha=True)
        my_predbat.had_errors = False
        my_predbat.load_user_config()
        resolved = my_predbat.get_arg("debug_history_count", 15)
        if resolved != item_max:
            print("  FAILED: expected a value above the maximum to clamp to {}, got {}".format(item_max, resolved))
            failed += 1
        if my_predbat.had_errors is not True:
            print("  FAILED: an above-maximum debug_history_count should flag had_errors")
            failed += 1
    finally:
        my_predbat.expose_config("debug_history_count", original_value, force_ha=True)
        my_predbat.had_errors = original_had_errors

    return failed
