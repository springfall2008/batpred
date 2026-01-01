# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime


def test_rate_replicate_missing_slots(my_predbat):
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


def test_rate_replicate_no_previous_day(my_predbat):
    """
    Test for rate_replicate when there's NO previous day data at all.

    This tests the edge case where the API doesn't return any previous day rates.
    In this case, the function should use modulo to find the same time from current day,
    or use rate_last as a fallback.
    """
    failed = 0

    print("\n*** Test: Missing 23:00 and 23:30 with NO previous day data ***")

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


def test_rate_replicate_with_zero_rates(my_predbat):
    """
    Test for rate_replicate with legitimate zero rates (free electricity).

    Verifies that legitimate 0 rates (e.g., free electricity periods) are preserved
    and not treated as missing data. This is important for scenarios like:
    - Free electricity promotions
    - Negative pricing events
    - Special tariff periods
    """
    failed = 0

    print("\n*** Test: Zero rates are legitimate values (free electricity) ***")

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
