# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime


def test_load_free_slot(my_predbat):
    """
    Test the load_free_slot function

    Tests various scenarios:
    - Basic free slot setting import rates to 0
    - Free slot setting export rates to 0
    - Multiple free slots
    - Free slots spanning midnight
    - Invalid/missing start/end times
    - Slots outside forecast window
    - Slots with different rates
    - Rate_replicate tracking
    - Load scaling for free sessions
    """
    print("**** Running load_free_slot tests ****")
    failed = False

    # Setup test environment
    old_forecast_minutes = my_predbat.forecast_minutes
    old_midnight_utc = my_predbat.midnight_utc
    old_rate_import = my_predbat.rate_import
    old_rate_export = my_predbat.rate_export
    old_load_scaling_dynamic = my_predbat.load_scaling_dynamic
    old_load_scaling_free = my_predbat.load_scaling_free
    old_minutes_now = my_predbat.minutes_now

    my_predbat.forecast_minutes = 48 * 60  # 2 days
    my_predbat.midnight_utc = datetime.strptime("2025-01-15T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    # forecast_minutes is a duration from minutes_now, so the plan window ends at
    # minutes_now + forecast_minutes measured from midnight. Pin minutes_now rather than
    # inheriting the wall clock, or these boundary tests mean something different every run.
    my_predbat.minutes_now = 18 * 60  # 18:00 on day 1
    my_predbat.load_scaling_free = 0.5  # Free session load scaling
    window_end = my_predbat.forecast_minutes + my_predbat.minutes_now

    # Initialize rate arrays with base rates. rate_replicate() fills well past the plan window in
    # production (forecast_minutes + 48h), so size these the same way rather than stopping short.
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Test 1: Basic free slot - sets import rates to 0
    print("*** Test 1: Basic free slot setting import rates to 0")

    free_slots = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(free_slots, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Check rates were set to 0 for the hour (10:00-11:00 = minute 600-660)
    start_min = 10 * 60  # 600
    end_min = 11 * 60  # 660

    for minute in range(start_min, end_min):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Expected rate_import[{}] to be 0.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break
        if my_predbat.load_scaling_dynamic[minute] != 0.5:
            print("ERROR: Expected load_scaling_dynamic[{}] to be 0.5, got {}".format(minute, my_predbat.load_scaling_dynamic[minute]))
            failed = True
            break
        if rate_replicate.get(minute) != "saving":
            print("ERROR: Expected rate_replicate[{}] to be 'saving', got {}".format(minute, rate_replicate.get(minute)))
            failed = True
            break

    # Check rates outside slot weren't changed
    if my_predbat.rate_import[start_min - 1] != 20.0:
        print("ERROR: Rate before slot should be unchanged at 20.0, got {}".format(my_predbat.rate_import[start_min - 1]))
        failed = True

    if not failed:
        print("Test 1 passed - import rates set to 0 during free slot")

    # Reset for next test
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Test 2: Free slot setting export rates
    print("*** Test 2: Free slot setting export rates to 0")

    rate_replicate = {}
    my_predbat.load_free_slot(free_slots, my_predbat.rate_export, export=True, rate_replicate=rate_replicate)

    for minute in range(start_min, end_min):
        if my_predbat.rate_export[minute] != 0.0:
            print("ERROR: Expected rate_export[{}] to be 0.0, got {}".format(minute, my_predbat.rate_export[minute]))
            failed = True
            break
        # Export mode shouldn't change load_scaling_dynamic
        if my_predbat.load_scaling_dynamic[minute] != 1.0:
            print("ERROR: Expected load_scaling_dynamic[{}] to remain 1.0 for export, got {}".format(minute, my_predbat.load_scaling_dynamic[minute]))
            failed = True
            break

    if not failed:
        print("Test 2 passed - export rates set to 0 during free slot")

    # Reset for next test
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Test 3: Multiple free slots
    print("*** Test 3: Multiple free slots")

    multi_slots = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}, {"start": "2025-01-15T14:00:00+00:00", "end": "2025-01-15T15:30:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(multi_slots, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Check first slot (10:00-11:00)
    for minute in range(10 * 60, 11 * 60):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: First slot rate_import[{}] should be 0.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    # Check second slot (14:00-15:30)
    for minute in range(14 * 60, 15 * 60 + 30):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Second slot rate_import[{}] should be 0.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    # Check between slots (12:00) should be unchanged
    if my_predbat.rate_import[12 * 60] != 20.0:
        print("ERROR: Rate between slots should be 20.0, got {}".format(my_predbat.rate_import[12 * 60]))
        failed = True

    if not failed:
        print("Test 3 passed - multiple free slots handled correctly")

    # Reset for next test
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Test 4: Free slot spanning midnight
    print("*** Test 4: Free slot spanning midnight")

    midnight_slot = [{"start": "2025-01-15T23:00:00+00:00", "end": "2025-01-16T01:00:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(midnight_slot, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Check 23:00-00:00 (day 1)
    for minute in range(23 * 60, 24 * 60):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Midnight slot before midnight rate_import[{}] should be 0.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    # Check 00:00-01:00 (day 2)
    for minute in range(24 * 60, 25 * 60):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Midnight slot after midnight rate_import[{}] should be 0.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    if not failed:
        print("Test 4 passed - midnight-spanning slot handled correctly")

    # Reset for next test
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Test 5: Invalid start/end times - should be skipped
    print("*** Test 5: Invalid start/end times")

    invalid_slots = [{"start": "invalid-time", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}, {"start": "2025-01-15T12:00:00+00:00", "end": "also-invalid", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(invalid_slots, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Rates should remain unchanged
    if my_predbat.rate_import[10 * 60] != 20.0 or my_predbat.rate_import[12 * 60] != 20.0:
        print("ERROR: Invalid slots should not change rates")
        failed = True
    else:
        print("Test 5 passed - invalid times ignored")

    # Test 5b: a good slot followed by an undecodable one - the bad slot must not
    # re-apply its rate over the good slot's minute range (it has no range of its own)
    print("*** Test 5b: undecodable slot after a good one keeps the good slot's rate")

    # Reset
    my_predbat.rate_export = {n: 5.0 for n in range(0, window_end + 24 * 60)}

    good_then_bad = [
        {"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0},
        {"start": "not-a-time", "end": "also-not-a-time", "rate": 3.0},
    ]

    rate_replicate = {}
    my_predbat.load_free_slot(good_then_bad, my_predbat.rate_export, export=True, rate_replicate=rate_replicate)

    for minute in range(10 * 60, 11 * 60):
        if my_predbat.rate_export[minute] != 0.0:
            print("ERROR: Good slot rate_export[{}] should stay 0.0, got {}".format(minute, my_predbat.rate_export[minute]))
            failed = True
            break

    if not failed:
        print("Test 5b passed - undecodable slot skipped, good slot's rate untouched")

    # Test 6: Slot outside forecast window
    print("*** Test 6: Slot outside forecast window")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Genuinely beyond the plan: starts after minutes_now + forecast_minutes (minute 3960)
    future_slot = [{"start": "2025-01-18T10:00:00+00:00", "end": "2025-01-18T11:00:00+00:00", "rate": 0.0}]  # Day 4, past the window

    rate_replicate = {}
    my_predbat.load_free_slot(future_slot, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    if any(my_predbat.rate_import[n] != 20.0 for n in my_predbat.rate_import):
        print("ERROR: Slot outside forecast window should not change rates")
        failed = True
    else:
        print("Test 6 passed - slot outside forecast window ignored")

    # Test 6b: the free session two days out that the plan DOES reach (#4931)
    # From 18:00 on day 1 the plan runs to minute 3960, so a session at minute 3540 is inside it.
    # This was dropped when the bound was compared against forecast_minutes alone.
    print("*** Test 6b: Free session two days ahead, inside the plan window")

    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    sunday_slot = [{"start": "2025-01-17T11:00:00+00:00", "end": "2025-01-17T12:00:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(sunday_slot, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    for minute in range(2 * 24 * 60 + 11 * 60, 2 * 24 * 60 + 12 * 60):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Two-day-ahead free session rate_import[{}] should be 0.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break
        if rate_replicate.get(minute) != "saving":
            print("ERROR: Expected rate_replicate[{}] to be 'saving', got {}".format(minute, rate_replicate.get(minute)))
            failed = True
            break

    if not failed:
        print("Test 6b passed - free session two days ahead applied to the plan")

    # Test 7: Slot partially outside forecast window
    print("*** Test 7: Slot partially outside forecast window (capped)")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    # Slot straddling the end of the plan window: window_end = forecast_minutes + minutes_now
    # = 2880 + 1080 = minute 3960 (18:00 on day 3, measured from midnight day 1)
    partial_slot = [{"start": "2025-01-17T17:00:00+00:00", "end": "2025-01-17T19:00:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(partial_slot, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # 17:00 on day 3 = minute 65*60 = 3900; the end is capped at window_end (3960)
    start_partial = 65 * 60
    for minute in range(start_partial, window_end):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Partial slot rate_import[{}] should be 0.0 (within forecast), got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    if my_predbat.rate_import[window_end] != 20.0:
        print("ERROR: Partial slot should stop at the window end, got {} at minute {}".format(my_predbat.rate_import[window_end], window_end))
        failed = True

    if not failed:
        print("Test 7 passed - slot capped at forecast window boundary")

    # Test 8: Non-zero rate for free slot (e.g., negative export bonus)
    print("*** Test 8: Non-zero rate in free slot")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    bonus_slot = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": -5.0}]  # Negative rate (you get paid to use electricity)

    rate_replicate = {}
    my_predbat.load_free_slot(bonus_slot, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Check the rate is set correctly (min of existing rate and slot rate)
    for minute in range(10 * 60, 11 * 60):
        if my_predbat.rate_import[minute] != -5.0:
            print("ERROR: Expected rate_import[{}] to be -5.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    if not failed:
        print("Test 8 passed - non-zero rate handled correctly")

    # Test 9: Rate takes minimum when slot overlaps existing lower rate
    print("*** Test 9: Rate takes minimum of existing and slot rate")

    # Reset with a lower rate already set
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}
    # Set some minutes to a very low rate
    for minute in range(10 * 60, 10 * 60 + 30):
        my_predbat.rate_import[minute] = 1.0  # Very cheap already

    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, window_end + 24 * 60)}

    overlap_slot = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 5.0}]  # Higher than existing 1.0 for first 30 mins

    rate_replicate = {}
    my_predbat.load_free_slot(overlap_slot, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # First 30 minutes should keep lower rate of 1.0
    for minute in range(10 * 60, 10 * 60 + 30):
        if my_predbat.rate_import[minute] != 1.0:
            print("ERROR: Expected rate_import[{}] to remain 1.0 (minimum), got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    # Next 30 minutes should take slot rate of 5.0
    for minute in range(10 * 60 + 30, 11 * 60):
        if my_predbat.rate_import[minute] != 5.0:
            print("ERROR: Expected rate_import[{}] to be 5.0, got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    if not failed:
        print("Test 9 passed - takes minimum of existing and slot rate")

    # Test 10: Empty slots list
    print("*** Test 10: Empty slots list")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}

    rate_replicate = {}
    my_predbat.load_free_slot([], my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Nothing should change
    if my_predbat.rate_import[10 * 60] != 20.0:
        print("ERROR: Empty slots should not change rates")
        failed = True
    else:
        print("Test 10 passed - empty slots list handled")

    # Test 11: Slots with missing start or end
    print("*** Test 11: Slots with None start or end")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, window_end + 24 * 60)}

    none_slots = [{"start": None, "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}, {"start": "2025-01-15T12:00:00+00:00", "end": None, "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(none_slots, my_predbat.rate_import, export=False, rate_replicate=rate_replicate)

    # Rates should remain unchanged
    if my_predbat.rate_import[10 * 60] != 20.0 or my_predbat.rate_import[12 * 60] != 20.0:
        print("ERROR: Slots with None values should be ignored")
        failed = True
    else:
        print("Test 11 passed - slots with None start/end ignored")

    # Restore original values
    my_predbat.forecast_minutes = old_forecast_minutes
    my_predbat.midnight_utc = old_midnight_utc
    my_predbat.rate_import = old_rate_import
    my_predbat.rate_export = old_rate_export
    my_predbat.load_scaling_dynamic = old_load_scaling_dynamic
    my_predbat.load_scaling_free = old_load_scaling_free
    my_predbat.minutes_now = old_minutes_now

    if not failed:
        print("**** All load_free_slot tests PASSED ****")
    else:
        print("**** Some load_free_slot tests FAILED ****")

    return failed
