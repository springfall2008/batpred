# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime


def test_rate_replicate(my_predbat):
    """
    Comprehensive test suite for rate_replicate function.

    Tests all major code paths including:
    - Missing slots replication from previous day
    - No previous day data fallback
    - Zero rates (free electricity)
    - Undefined negative minutes (KeyError bug #3158)
    - Intelligent Octopus rate_io
    - Future rate adjustments (import/export)
    - Rate offsets (import/export with negative clamping)
    - Gas rate handling
    """

    # Registry of all sub-tests
    sub_tests = [
        ("missing_slots", _test_missing_slots, "Missing 23:00 and 23:30 slots (Octopus Agile before 4PM)"),
        ("no_previous_day", _test_no_previous_day, "Missing slots with NO previous day data"),
        ("zero_rates", _test_zero_rates, "Zero rates are legitimate values (free electricity)"),
        ("undefined_negative", _test_undefined_negative_minutes, "Undefined negative minutes (KeyError -1440 bug #3158)"),
        ("rate_io", _test_rate_io, "Intelligent Octopus rate_io"),
        ("future_import", _test_future_rate_adjust_import, "Future rate adjust import"),
        ("future_export", _test_future_rate_adjust_export, "Future rate adjust export (negative clamping)"),
        ("import_offset", _test_import_offset, "Import rate offset"),
        ("export_offset", _test_export_offset_negative, "Export rate offset with negative clamping"),
        ("gas_rates", _test_gas_rates, "Gas rates (is_gas=True)"),
    ]

    print("\n" + "="*70)
    print("RATE_REPLICATE TEST SUITE")
    print("="*70)

    failed = 0
    passed = 0

    for test_name, test_func, test_desc in sub_tests:
        print(f"\n[{test_name}] {test_desc}")
        print("-" * 70)
        try:
            test_result = test_func(my_predbat)
            if test_result:
                print(f"✗ FAILED: {test_name}")
                failed += 1
            else:
                print(f"✓ PASSED: {test_name}")
                passed += 1
        except Exception as e:
            print(f"✗ EXCEPTION in {test_name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "="*70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("="*70)

    return failed


def _test_missing_slots(my_predbat):
    """
    Test for rate_replicate function handling missing 23:00 and 23:30 slots.

    This test reproduces the Octopus Agile issue where:
    - Current day rates only go up to 22:30 (before 4PM when tomorrow's rates are released)
    - Rates for 23:00 and 23:30 are missing from current day
    - Previous day has full rates including 23:00 and 23:30
    - Predbat should replicate previous day's 23:00 and 23:30 into current day's missing slots

    Bug: The rate_replicate function shows 0.00 for these slots instead of replicating from previous day.
    """
    failed = 0

    print("*** Test: Missing 23:00 and 23:30 slots (Octopus Agile before 4PM) ***")

    # Setup: Wednesday before 4PM scenario
    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880  # 48 hours forecast
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create rate dict simulating Octopus API response:
    # - Previous day (Tuesday) has FULL rates from -1440 to -1 including 23:00 (-60) and 23:30 (-30)
    # - Current day (Wednesday) has rates from 0 to 1350 (00:00 to 22:30)
    # - Current day is MISSING rates for 1380 (23:00) and 1410 (23:30)
    rates = {}

    # Previous day rates (Tuesday) - full day including 23:00 and 23:30
    for minute in range(-1440, 0):
        if minute == -60:  # 23:00 yesterday
            rates[minute] = 17.5  # Specific rate for testing
        elif minute == -30:  # 23:30 yesterday
            rates[minute] = 18.0  # Specific rate for testing
        else:
            rates[minute] = 20.0  # Default rate

    # Current day rates (Wednesday) - only up to 22:30
    for minute in range(0, 1351):  # 0 to 1350 (00:00 to 22:30)
        rates[minute] = 15.0  # Default rate for current day

    # Explicitly confirm 1380 and 1410 are missing
    assert 1380 not in rates, "Test setup error: minute 1380 should be missing"
    assert 1410 not in rates, "Test setup error: minute 1410 should be missing"

    # Verify previous day rates exist
    assert -60 in rates, "Test setup error: minute -60 should exist"
    assert -30 in rates, "Test setup error: minute -30 should exist"

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=False)

    # Expected behavior: 23:00 and 23:30 should be replicated from previous day
    if 1380 not in result:
        print(f"  ✗ ERROR: Minute 1380 (23:00) is still missing after rate_replicate")
        failed |= 1
    elif result[1380] != rates[-60]:
        print(f"  ✗ ERROR: Minute 1380 (23:00) should be {rates[-60]} (from minute -60), got {result[1380]}")
        failed |= 1
    elif result[1380] == 0.0:
        print(f"  ✗ ERROR: Minute 1380 (23:00) is 0.0 - this is the BUG we're fixing!")
        failed |= 1

    if 1410 not in result:
        print(f"  ✗ ERROR: Minute 1410 (23:30) is still missing after rate_replicate")
        failed |= 1
    elif result[1410] != rates[-30]:
        print(f"  ✗ ERROR: Minute 1410 (23:30) should be {rates[-30]} (from minute -30), got {result[1410]}")
        failed |= 1
    elif result[1410] == 0.0:
        print(f"  ✗ ERROR: Minute 1410 (23:30) is 0.0 - this is the BUG we're fixing!")
        failed |= 1

    # Check that replicated type is marked as "copy"
    if result_replicated.get(1380) != "copy":
        print(f"  ✗ ERROR: Minute 1380 should be marked as 'copy', got {result_replicated.get(1380)}")
        failed |= 1

    if result_replicated.get(1410) != "copy":
        print(f"  ✗ ERROR: Minute 1410 should be marked as 'copy', got {result_replicated.get(1410)}")
        failed |= 1
    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60
    return failed


def _test_no_previous_day(my_predbat):
    """Test for rate_replicate when there's NO previous day data at all."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create rate dict with NO previous day data
    # - Current day only: 0 to 1350 (00:00 to 22:30)
    # - Missing: 1380 (23:00) and 1410 (23:30)
    rates = {}
    for minute in range(0, 1351):
        rates[minute] = 15.0

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=False)

    # Without previous day data, the function should fall back to rate_last (last seen rate)
    # which should be rates[1350] = 15.0 (22:30's rate)
    if 1380 not in result:
        print(f"  ✗ ERROR: Minute 1380 should be filled (even without previous day data)")
        failed |= 1
    elif result[1380] == 0.0:
        print(f"  ✗ ERROR: Minute 1380 should not be 0.0 (should use rate_last fallback)")
        failed |= 1

    if 1410 not in result:
        print(f"  ✗ ERROR: Minute 1410 should be filled (even without previous day data)")
        failed |= 1
    elif result[1410] == 0.0:
        print(f"  ✗ ERROR: Minute 1410 should not be 0.0 (should use rate_last fallback)")
        failed |= 1

    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_zero_rates(my_predbat):
    """Test for rate_replicate with legitimate zero rates (free electricity)."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create rate dict with legitimate 0 rates
    # - Previous day with 0 rate at 23:00 (free electricity)
    # - Current day with normal rates up to 22:30
    # - Missing: current day 23:00 and 23:30
    rates = {}

    # Previous day rates with FREE ELECTRICITY at 23:00
    for minute in range(-1440, 0):
        if minute == -60:  # 23:00 yesterday - FREE ELECTRICITY
            rates[minute] = 0.0
        elif minute == -30:  # 23:30 yesterday - also free
            rates[minute] = 0.0
        else:
            rates[minute] = 20.0

    # Current day rates - only up to 22:30
    for minute in range(0, 1351):
        rates[minute] = 15.0

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=False)

    # Expected: 23:00 and 23:30 should be replicated from previous day's 0.0 rates
    if 1380 not in result:
        print(f"  ✗ ERROR: Minute 1380 (23:00) is missing after rate_replicate")
        failed |= 1
    elif result[1380] != 0.0:
        print(f"  ✗ ERROR: Minute 1380 (23:00) should be 0.0 (from minute -60), got {result[1380]}")
        failed |= 1

    if 1410 not in result:
        print(f"  ✗ ERROR: Minute 1410 (23:30) is missing after rate_replicate")
        failed |= 1
    elif result[1410] != 0.0:
        print(f"  ✗ ERROR: Minute 1410 (23:30) should be 0.0 (from minute -30), got {result[1410]}")
        failed |= 1

    # Also verify that previous day rates with 0 are still present
    if result.get(-60) != 0.0:
        print(f"  ✗ ERROR: Previous day minute -60 should still be 0.0, got {result.get(-60)}")
        failed |= 1
    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_undefined_negative_minutes(my_predbat):
    """Test for rate_replicate when rates start at minute 0 or later (no negative minutes).

    Bug reported in issue #3158: KeyError: -1440 in publish_rates after update to 8.31.5
    """
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create rate dict with NO negative minute data, starting from minute 0
    # This simulates the actual bug scenario from issue #3158
    rates = {}
    for minute in range(0, 1440):  # Only current day: 0 to 1439
        rates[minute] = 15.0 + (minute % 100) / 100.0  # Varying rates

    print(f"  Input rates: {len(rates)} entries (minutes 0 to 1439)")
    print(f"  Rate at -1440: {rates.get(-1440, 'MISSING')}")
    print(f"  Rate at 0: {rates.get(0, 'MISSING')}")
    print(f"  Rate at 1439: {rates.get(1439, 'MISSING')}")

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=False)

    print(f"  After rate_replicate: {len(result)} entries")

    # The bug: rate_replicate won't fill negative minutes when rate_last_valid is False
    # This happens because no rates at minute >= 0 have been seen
    missing_minutes = []
    for minute in range(-1440, 0):
        if minute not in result:
            missing_minutes.append(minute)

    if missing_minutes:
        print(f"  ✗ ERROR: {len(missing_minutes)} negative minutes are undefined after rate_replicate")
        print(f"    First missing: {missing_minutes[0]}, Last missing: {missing_minutes[-1]}")
        print(f"    This would cause KeyError in publish_rates when iterating from -1440")

        # Demonstrate the KeyError that would occur in publish_rates
        try:
            # Simulate what publish_rates does: access rates[minute] directly
            test_minute = -1440
            value = result[test_minute]  # This will raise KeyError
            print(f"    No KeyError at minute {test_minute} (unexpected!)")
        except KeyError as e:
            print(f"    ✓ Confirmed: KeyError accessing minute {test_minute}: {e}")
            failed |= 1
    else:
        print(f"  ✓ All negative minutes were filled correctly")

    # Also check if any positive minutes were created (there shouldn't be any with only negative input)
    positive_minutes = [m for m in result.keys() if m >= 0]
    if positive_minutes:
        print(f"  Note: {len(positive_minutes)} positive minutes were created despite no input data")
    else:
        print(f"  ✓ No positive minutes created (expected with only negative input)")

    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_rate_io(my_predbat):
    """Test rate_replicate with Intelligent Octopus rate_io adjustments."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create rates with Intelligent Octopus slot
    rates = {}
    for minute in range(0, 1440):
        if 120 <= minute < 300:  # 02:00-05:00 intelligent slot
            rates[minute] = 5.0  # Cheap rate
        else:
            rates[minute] = 25.0

    # Mark intelligent slots in rate_io
    rate_io = {}
    for minute in range(120, 300):
        rate_io[minute] = True

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, rate_io=rate_io, is_import=True, is_gas=False)

    # Check that next day's 02:00-05:00 is set to rate_max (not replicated from today)
    next_day_intelligent_start = 1440 + 120
    if next_day_intelligent_start in result:
        if result[next_day_intelligent_start] == my_predbat.rate_max:
            print(f"  ✓ Intelligent slot correctly replaced with rate_max: {result[next_day_intelligent_start]}")
        else:
            print(f"  ✗ ERROR: Next day intelligent slot should be {my_predbat.rate_max}, got {result[next_day_intelligent_start]}")
            failed |= 1
    else:
        print(f"  ✗ ERROR: Next day intelligent slot minute {next_day_intelligent_start} missing")
        failed |= 1

    # Normal rates should still replicate correctly
    normal_minute_next_day = 1440 + 600  # 10:00 next day
    if normal_minute_next_day in result and result[normal_minute_next_day] == 25.0:
        print(f"  ✓ Normal rates replicate correctly: {result[normal_minute_next_day]}")
    else:
        print(f"  ✗ ERROR: Normal rate replication failed at minute {normal_minute_next_day}")
        failed |= 1

    # Restore context
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_future_rate_adjust_import(my_predbat):
    """Test rate_replicate with futurerate_adjust_import feature."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Set futurerate_adjust_import to True via args
    my_predbat.args["futurerate_adjust_import"] = True

    # Create base rates for current day only
    rates = {}
    for minute in range(0, 1440):
        rates[minute] = 15.0

    # Set future rate adjustments for next day
    my_predbat.future_energy_rates_import = {}
    for minute in range(1440, 2880):
        my_predbat.future_energy_rates_import[minute] = 20.0  # Future rates higher
        my_predbat.future_energy_rates_import[minute % 1440] = 15.0  # Base rate

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=False)

    # Check that future rates are applied
    next_day_minute = 1440 + 600
    if next_day_minute in result:
        if result[next_day_minute] == 20.0:
            print(f"  ✓ Future rate adjustment applied: {result[next_day_minute]}")
            if result_replicated.get(next_day_minute) == "future":
                print(f"  ✓ Replicated type correctly marked as 'future'")
            else:
                print(f"  ✗ ERROR: Should be marked as 'future', got {result_replicated.get(next_day_minute)}")
                failed |= 1
        else:
            print(f"  ✗ ERROR: Future rate should be 20.0, got {result[next_day_minute]}")
            failed |= 1
    else:
        print(f"  ✗ ERROR: Future rate minute {next_day_minute} missing")
        failed |= 1

    # Restore context
    if "futurerate_adjust_import" in my_predbat.args:
        del my_predbat.args["futurerate_adjust_import"]
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_future_rate_adjust_export(my_predbat):
    """Test rate_replicate with futurerate_adjust_export feature."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Set futurerate_adjust_export to True via args
    my_predbat.args["futurerate_adjust_export"] = True

    # Create base export rates
    rates = {}
    for minute in range(0, 1440):
        rates[minute] = 10.0

    # Set future export rates including negative value
    my_predbat.future_energy_rates_export = {}
    for minute in range(1440, 2880):
        my_predbat.future_energy_rates_export[minute] = -5.0  # Negative future rate (should be clamped to 0)
        my_predbat.future_energy_rates_export[minute % 1440] = 10.0  # Base rate

    # Run rate_replicate for export
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=False, is_gas=False)

    # Check that negative future export rates are clamped to 0
    next_day_minute = 1440 + 600
    if next_day_minute in result:
        if result[next_day_minute] == 0.0:
            print(f"  ✓ Negative export rate clamped to 0: {result[next_day_minute]}")
            if result_replicated.get(next_day_minute) == "future":
                print(f"  ✓ Replicated type correctly marked as 'future'")
            else:
                print(f"  ✗ ERROR: Should be marked as 'future', got {result_replicated.get(next_day_minute)}")
                failed |= 1
        else:
            print(f"  ✗ ERROR: Negative export rate should be clamped to 0.0, got {result[next_day_minute]}")
            failed |= 1
    else:
        print(f"  ✗ ERROR: Future rate minute {next_day_minute} missing")
        failed |= 1

    # Restore context
    if "futurerate_adjust_export" in my_predbat.args:
        del my_predbat.args["futurerate_adjust_export"]
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_import_offset(my_predbat):
    """Test rate_replicate with metric_future_rate_offset_import."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 5.0  # Add 5p to future rates
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create base rates
    rates = {}
    for minute in range(0, 1440):
        rates[minute] = 15.0

    # Run rate_replicate
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=False)

    # Check that offset is applied to future days
    next_day_minute = 1440 + 600
    if next_day_minute in result:
        expected_rate = 15.0 + 5.0
        if result[next_day_minute] == expected_rate:
            print(f"  ✓ Import offset applied correctly: {result[next_day_minute]}")
            if result_replicated.get(next_day_minute) == "offset":
                print(f"  ✓ Replicated type correctly marked as 'offset'")
            else:
                print(f"  ✗ ERROR: Should be marked as 'offset', got {result_replicated.get(next_day_minute)}")
                failed |= 1
        else:
            print(f"  ✗ ERROR: Rate should be {expected_rate}, got {result[next_day_minute]}")
            failed |= 1

    # Current day should not have offset
    if result[600] == 15.0:
        print(f"  ✓ Current day rate unchanged: {result[600]}")
    else:
        print(f"  ✗ ERROR: Current day should be 15.0, got {result[600]}")
        failed |= 1

    # Restore context
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_export_offset_negative(my_predbat):
    """Test rate_replicate with metric_future_rate_offset_export and negative clamping."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = -8.0  # Subtract 8p from export rates
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create base export rates
    rates = {}
    for minute in range(0, 1440):
        rates[minute] = 5.0  # Low export rate

    # Run rate_replicate for export
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=False, is_gas=False)

    # Check that offset results in 0 (5 - 8 = -3, clamped to 0)
    next_day_minute = 1440 + 600
    if next_day_minute in result:
        if result[next_day_minute] == 0.0:
            print(f"  ✓ Export rate with offset clamped to 0: {result[next_day_minute]}")
            if result_replicated.get(next_day_minute) == "offset":
                print(f"  ✓ Replicated type correctly marked as 'offset'")
            else:
                print(f"  ✗ ERROR: Should be marked as 'offset', got {result_replicated.get(next_day_minute)}")
                failed |= 1
        else:
            print(f"  ✗ ERROR: Rate should be 0.0 (clamped), got {result[next_day_minute]}")
            failed |= 1

    # Test with higher rate that doesn't go negative
    rates2 = {}
    for minute in range(0, 1440):
        rates2[minute] = 15.0

    result2, result_replicated2 = my_predbat.rate_replicate(rates2, is_import=False, is_gas=False)
    expected_rate = 15.0 - 8.0
    if next_day_minute in result2 and result2[next_day_minute] == expected_rate:
        print(f"  ✓ Export rate with offset (not clamped): {result2[next_day_minute]}")
    else:
        print(f"  ✗ ERROR: Rate should be {expected_rate}, got {result2.get(next_day_minute)}")
        failed |= 1

    # Restore context
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed


def _test_gas_rates(my_predbat):
    """Test rate_replicate with is_gas=True to ensure gas rates are handled correctly."""
    failed = 0

    my_predbat.midnight = datetime.strptime("2025-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S")
    my_predbat.forecast_minutes = 2880
    my_predbat.metric_future_rate_offset_import = 3.0
    my_predbat.metric_future_rate_offset_export = 2.0
    my_predbat.future_energy_rates_import = {}
    my_predbat.future_energy_rates_export = {}
    my_predbat.rate_max = 99.0

    # Create base gas rates
    rates = {}
    for minute in range(0, 1440):
        rates[minute] = 10.0

    # Run rate_replicate for gas (is_import=True, is_gas=True)
    result, result_replicated = my_predbat.rate_replicate(rates, is_import=True, is_gas=True)

    # Check that gas rates DO get import offset when is_import=True
    # (the is_gas flag only prevents export offset, not import offset)
    next_day_minute = 1440 + 600
    expected_rate = 10.0 + 3.0  # Import offset is applied
    if next_day_minute in result:
        if result[next_day_minute] == expected_rate:
            print(f"  ✓ Gas import rates get import offset applied: {result[next_day_minute]}")
        else:
            print(f"  ✗ ERROR: Gas rate should be {expected_rate} (with import offset), got {result[next_day_minute]}")
            failed |= 1

    # Test export gas rates - should NOT get export offset due to is_gas check
    rates2 = {}
    for minute in range(0, 1440):
        rates2[minute] = 10.0

    result2, result_replicated2 = my_predbat.rate_replicate(rates2, is_import=False, is_gas=True)

    # Export gas rates should replicate WITHOUT export offset (is_gas bypasses export offset)
    if next_day_minute in result2:
        if result2[next_day_minute] == 10.0:
            print(f"  ✓ Gas export rates replicate without export offset: {result2[next_day_minute]}")
        else:
            print(f"  ✗ ERROR: Gas export rate should be 10.0 (no export offset due to is_gas), got {result2[next_day_minute]}")
            failed |= 1

    # Restore context
    my_predbat.metric_future_rate_offset_import = 0
    my_predbat.metric_future_rate_offset_export = 0
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    my_predbat.rate_max = 0
    my_predbat.forecast_minutes = 24*60

    return failed
