# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
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

    my_predbat.forecast_minutes = 48 * 60  # 2 days
    my_predbat.midnight_utc = datetime.strptime("2025-01-15T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    my_predbat.load_scaling_free = 0.5  # Free session load scaling

    # Initialize rate arrays with base rates
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Test 1: Basic free slot - sets import rates to 0
    print("*** Test 1: Basic free slot setting import rates to 0")

    free_slots = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(free_slots, export=False, rate_replicate=rate_replicate)

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
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Test 2: Free slot setting export rates
    print("*** Test 2: Free slot setting export rates to 0")

    rate_replicate = {}
    my_predbat.load_free_slot(free_slots, export=True, rate_replicate=rate_replicate)

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
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Test 3: Multiple free slots
    print("*** Test 3: Multiple free slots")

    multi_slots = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}, {"start": "2025-01-15T14:00:00+00:00", "end": "2025-01-15T15:30:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(multi_slots, export=False, rate_replicate=rate_replicate)

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
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Test 4: Free slot spanning midnight
    print("*** Test 4: Free slot spanning midnight")

    midnight_slot = [{"start": "2025-01-15T23:00:00+00:00", "end": "2025-01-16T01:00:00+00:00", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(midnight_slot, export=False, rate_replicate=rate_replicate)

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
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.rate_export = {n: 5.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Test 5: Invalid start/end times - should be skipped
    print("*** Test 5: Invalid start/end times")

    invalid_slots = [{"start": "invalid-time", "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}, {"start": "2025-01-15T12:00:00+00:00", "end": "also-invalid", "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(invalid_slots, export=False, rate_replicate=rate_replicate)

    # Rates should remain unchanged
    if my_predbat.rate_import[10 * 60] != 20.0 or my_predbat.rate_import[12 * 60] != 20.0:
        print("ERROR: Invalid slots should not change rates")
        failed = True
    else:
        print("Test 5 passed - invalid times ignored")

    # Test 6: Slot outside forecast window
    print("*** Test 6: Slot outside forecast window")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Slot starting after forecast ends (48 hours = 2880 minutes)
    future_slot = [{"start": "2025-01-17T10:00:00+00:00", "end": "2025-01-17T11:00:00+00:00", "rate": 0.0}]  # Day 3, outside forecast

    rate_replicate = {}
    my_predbat.load_free_slot(future_slot, export=False, rate_replicate=rate_replicate)

    # No rates should change since slot is outside forecast window
    unchanged = all(my_predbat.rate_import[n] == 20.0 for n in range(0, min(100, my_predbat.forecast_minutes)))
    if not unchanged:
        print("ERROR: Slot outside forecast window should not change rates")
        failed = True
    else:
        print("Test 6 passed - slot outside forecast window ignored")

    # Test 7: Slot partially outside forecast window
    print("*** Test 7: Slot partially outside forecast window (capped)")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    # Slot ending beyond forecast window
    partial_slot = [{"start": "2025-01-16T23:00:00+00:00", "end": "2025-01-17T02:00:00+00:00", "rate": 0.0}]  # Near end of day 2  # Extends into day 3 (beyond forecast)

    rate_replicate = {}
    my_predbat.load_free_slot(partial_slot, export=False, rate_replicate=rate_replicate)

    # Should apply to minutes within forecast window only
    # 23:00 on day 2 = minute 47*60 = 2820
    # End should be capped at forecast_minutes (2880)
    start_partial = 47 * 60
    for minute in range(start_partial, my_predbat.forecast_minutes):
        if my_predbat.rate_import[minute] != 0.0:
            print("ERROR: Partial slot rate_import[{}] should be 0.0 (within forecast), got {}".format(minute, my_predbat.rate_import[minute]))
            failed = True
            break

    if not failed:
        print("Test 7 passed - slot capped at forecast window boundary")

    # Test 8: Non-zero rate for free slot (e.g., negative export bonus)
    print("*** Test 8: Non-zero rate in free slot")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    bonus_slot = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": -5.0}]  # Negative rate (you get paid to use electricity)

    rate_replicate = {}
    my_predbat.load_free_slot(bonus_slot, export=False, rate_replicate=rate_replicate)

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
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}
    # Set some minutes to a very low rate
    for minute in range(10 * 60, 10 * 60 + 30):
        my_predbat.rate_import[minute] = 1.0  # Very cheap already

    my_predbat.load_scaling_dynamic = {n: 1.0 for n in range(0, my_predbat.forecast_minutes)}

    overlap_slot = [{"start": "2025-01-15T10:00:00+00:00", "end": "2025-01-15T11:00:00+00:00", "rate": 5.0}]  # Higher than existing 1.0 for first 30 mins

    rate_replicate = {}
    my_predbat.load_free_slot(overlap_slot, export=False, rate_replicate=rate_replicate)

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
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}

    rate_replicate = {}
    my_predbat.load_free_slot([], export=False, rate_replicate=rate_replicate)

    # Nothing should change
    if my_predbat.rate_import[10 * 60] != 20.0:
        print("ERROR: Empty slots should not change rates")
        failed = True
    else:
        print("Test 10 passed - empty slots list handled")

    # Test 11: Slots with missing start or end
    print("*** Test 11: Slots with None start or end")

    # Reset
    my_predbat.rate_import = {n: 20.0 for n in range(0, my_predbat.forecast_minutes)}

    none_slots = [{"start": None, "end": "2025-01-15T11:00:00+00:00", "rate": 0.0}, {"start": "2025-01-15T12:00:00+00:00", "end": None, "rate": 0.0}]

    rate_replicate = {}
    my_predbat.load_free_slot(none_slots, export=False, rate_replicate=rate_replicate)

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

    if not failed:
        print("**** All load_free_slot tests PASSED ****")
    else:
        print("**** Some load_free_slot tests FAILED ****")

    return failed
