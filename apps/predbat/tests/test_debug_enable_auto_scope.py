# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for PredBat._debug_enable_auto_scope() - bounds how long switch.predbat_debug_enable's
raw per-cycle debug.yaml disk write (and the slower non-kernel prediction path it forces, #4453)
can run unattended, per Trefor's #4438 review: the original bug ("unbounded disk growth if left
on") was still present even after the rolling debug-history buffer (#4417) shipped, since that
buffer runs alongside the raw write rather than replacing it.
"""

from datetime import timedelta

from const import DEBUG_ENABLE_MAX_HOURS


def test_debug_enable_auto_scope(my_predbat):
    """Verify the raw write fires while debug_enable is on and within the auto-off window, the
    switch is force-disabled (and the timer reset) once the window elapses, and turning the
    switch off manually (or never on) resets the tracked start time with no write.
    """
    failed = 0
    print("--- debug_enable auto-scope tests ---")

    original_debug_enable = my_predbat.debug_enable
    original_started = my_predbat.debug_enable_started
    original_create_debug_yaml = my_predbat.create_debug_yaml
    calls = []
    my_predbat.create_debug_yaml = lambda *args, **kwargs: calls.append(my_predbat.now_utc)

    try:
        print("Test 1: debug_enable off - no write, start time stays unset")
        my_predbat.debug_enable = False
        my_predbat.debug_enable_started = None
        my_predbat._debug_enable_auto_scope()
        if calls:
            print("  FAILED: expected no write while debug_enable is off, got {} call(s)".format(len(calls)))
            failed += 1
        if my_predbat.debug_enable_started is not None:
            print("  FAILED: expected debug_enable_started to stay None while off")
            failed += 1

        print("Test 2: debug_enable just turned on - writes, and starts tracking from now")
        my_predbat.debug_enable = True
        start_time = my_predbat.now_utc
        my_predbat._debug_enable_auto_scope()
        if len(calls) != 1:
            print("  FAILED: expected exactly 1 write on first enable, got {}".format(len(calls)))
            failed += 1
        if my_predbat.debug_enable_started != start_time:
            print("  FAILED: expected debug_enable_started to be set to now_utc on first enable, got {}".format(my_predbat.debug_enable_started))
            failed += 1

        print("Test 3: still within the auto-off window - keeps writing, switch stays on")
        my_predbat.now_utc = start_time + timedelta(hours=DEBUG_ENABLE_MAX_HOURS - 1)
        my_predbat._debug_enable_auto_scope()
        if len(calls) != 2:
            print("  FAILED: expected a second write within the window, got {} total".format(len(calls)))
            failed += 1
        if not my_predbat.debug_enable:
            print("  FAILED: switch should still be on within the auto-off window")
            failed += 1

        print("Test 4: window elapsed - auto-disables, resets the tracked start, no write this cycle")
        my_predbat.now_utc = start_time + timedelta(hours=DEBUG_ENABLE_MAX_HOURS)
        my_predbat._debug_enable_auto_scope()
        if len(calls) != 2:
            print("  FAILED: expected no additional write once the window has elapsed, got {} total".format(len(calls)))
            failed += 1
        if my_predbat.debug_enable:
            print("  FAILED: expected debug_enable to be auto-disabled once the window elapsed")
            failed += 1
        if my_predbat.debug_enable_started is not None:
            print("  FAILED: expected debug_enable_started to be reset to None after auto-disable")
            failed += 1

        print("Test 5: re-enabling after an auto-disable starts a fresh window")
        my_predbat.debug_enable = True
        fresh_start = my_predbat.now_utc
        my_predbat._debug_enable_auto_scope()
        if len(calls) != 3:
            print("  FAILED: expected a write on the fresh re-enable, got {} total".format(len(calls)))
            failed += 1
        if my_predbat.debug_enable_started != fresh_start:
            print("  FAILED: expected a fresh debug_enable_started on re-enable, got {}".format(my_predbat.debug_enable_started))
            failed += 1

    finally:
        my_predbat.create_debug_yaml = original_create_debug_yaml
        my_predbat.debug_enable = original_debug_enable
        my_predbat.debug_enable_started = original_started

    return failed
