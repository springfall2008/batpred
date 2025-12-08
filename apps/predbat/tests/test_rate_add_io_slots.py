# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import timedelta
from tests.test_infra import reset_rates


def run_rate_add_io_slots_test(testname, my_predbat, slots, octopus_slot_low_rate, octopus_slot_max, expected_rates, expected_slots_per_day=None):
    """
    Run a single test for rate_add_io_slots
    """
    failed = False
    print("**** Running Test: rate_add_io_slots {} ****".format(testname))

    # Setup
    my_predbat.args["octopus_slot_low_rate"] = octopus_slot_low_rate
    my_predbat.args["octopus_slot_max"] = octopus_slot_max

    # Create a fresh rates dict with default values (10p/kWh)
    # Extend to cover 3 days to handle multi-day tests
    rates = {}
    for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60)):
        rates[minute] = 10.0

    # Run the function
    result_rates = my_predbat.rate_add_io_slots(rates, slots)

    # Check that expected rates were applied
    for minute, expected_rate in expected_rates.items():
        actual_rate = result_rates.get(minute, None)
        if actual_rate != expected_rate:
            print("ERROR: Minute {} should have rate {} but got {}".format(minute, expected_rate, actual_rate))
            failed = True

    return failed


def run_rate_add_io_slots_tests(my_predbat):
    """
    Test for rate_add_io_slots - the function that adds Octopus Intelligent slots to rates
    and enforces the 6-hour (12 x 30-min slot) daily limit
    """
    failed = 0

    TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
    now_utc = my_predbat.now_utc
    now_utc = now_utc.replace(minute=0, second=0, microsecond=0, hour=10)
    my_predbat.minutes_now = int((now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    midnight_utc = my_predbat.midnight_utc

    # Save original forecast_minutes and extend it for multi-day tests
    original_forecast_minutes = my_predbat.forecast_minutes
    my_predbat.forecast_minutes = 3 * 24 * 60  # 3 days

    reset_rates(my_predbat, 10, 5)
    my_predbat.rate_min = 4

    # Test 1: Simple single slot within limit
    print("\n**** Test 1: Single 30-min slot ****")
    slot_start = midnight_utc + timedelta(hours=2)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]

    expected_rates = {}
    for minute in range(120, 150):  # 02:00 - 02:30
        expected_rates[minute] = 4.0  # Should be rate_min

    failed |= run_rate_add_io_slots_test("test1_single_slot", my_predbat, slots, True, 12, expected_rates)

    # Test 2: Multiple slots under the daily limit (6 hours = 12 x 30-min slots)
    print("\n**** Test 2: Multiple slots under daily limit ****")
    slots = []
    expected_rates = {}
    for i in range(6):  # 6 x 30-min slots = 3 hours, well under limit
        slot_start = midnight_utc + timedelta(hours=i)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        for minute in range(i * 60, i * 60 + 30):
            expected_rates[minute] = 4.0

    failed |= run_rate_add_io_slots_test("test2_under_limit", my_predbat, slots, True, 12, expected_rates)

    # Test 3: Exactly at the daily limit (12 x 30-min slots = 6 hours)
    print("\n**** Test 3: Exactly at daily limit (12 slots) ****")
    slots = []
    expected_rates = {}
    for i in range(12):  # 12 x 30-min slots = 6 hours
        slot_start = midnight_utc + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        for minute in range(i * 30, (i + 1) * 30):
            expected_rates[minute] = 4.0

    failed |= run_rate_add_io_slots_test("test3_at_limit", my_predbat, slots, True, 12, expected_rates)

    # Test 4: Over the daily limit - 14 slots, only first 12 should be cheap
    print("\n**** Test 4: Over daily limit (14 slots, only 12 should be cheap) ****")
    slots = []
    expected_rates = {}
    for i in range(14):  # 14 x 30-min slots = 7 hours, over limit
        slot_start = midnight_utc + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        for minute in range(i * 30, (i + 1) * 30):
            if i < 12:  # Only first 12 slots get cheap rate
                expected_rates[minute] = 4.0
            else:
                expected_rates[minute] = 10.0  # Stays at default rate

    failed |= run_rate_add_io_slots_test("test4_over_limit", my_predbat, slots, True, 12, expected_rates)

    # Test 5: Bump-charge slots should be ignored (not count toward limit)
    print("\n**** Test 5: Bump-charge slots ignored ****")
    slot_start = midnight_utc + timedelta(hours=2)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "bump-charge", "location": "AT_HOME"}]

    # Bump-charge slots should not modify rates
    expected_rates = {}
    for minute in range(120, 150):
        expected_rates[minute] = 10.0  # Should stay at default, not changed

    failed |= run_rate_add_io_slots_test("test5_bump_charge", my_predbat, slots, True, 12, expected_rates)

    # Test 6: octopus_slot_low_rate=False - rates not modified
    print("\n**** Test 6: octopus_slot_low_rate=False ****")
    slot_start = midnight_utc + timedelta(hours=2)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]

    # With octopus_slot_low_rate=False, rates should not be changed
    expected_rates = {}
    for minute in range(120, 150):
        expected_rates[minute] = 10.0  # Should stay at default

    failed |= run_rate_add_io_slots_test("test6_low_rate_false", my_predbat, slots, False, 12, expected_rates)

    # Test 7: Custom octopus_slot_max value (e.g., 6 slots = 3 hours)
    print("\n**** Test 7: Custom slot max (6 slots) ****")
    slots = []
    expected_rates = {}
    for i in range(10):  # 10 x 30-min slots
        slot_start = midnight_utc + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        for minute in range(i * 30, (i + 1) * 30):
            if i < 6:  # Only first 6 slots get cheap rate with custom limit
                expected_rates[minute] = 4.0
            else:
                expected_rates[minute] = 10.0

    failed |= run_rate_add_io_slots_test("test7_custom_max", my_predbat, slots, True, 6, expected_rates)

    # Test 8: Slots spanning multiple days - each day has its own limit
    print("\n**** Test 8: Slots spanning multiple days ****")
    slots = []
    expected_rates = {}

    # Add 8 slots on day 0 (today)
    for i in range(8):
        slot_start = midnight_utc + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        for minute in range(i * 30, (i + 1) * 30):
            expected_rates[minute] = 4.0  # All 8 should be cheap (under 12 limit)

    # Add 8 slots on day 1 (tomorrow)
    for i in range(8):
        slot_start = midnight_utc + timedelta(days=1, minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        day1_minute = 1440 + i * 30
        for minute in range(day1_minute, day1_minute + 30):
            expected_rates[minute] = 4.0  # All 8 should be cheap (separate day limit)

    failed |= run_rate_add_io_slots_test("test8_multi_day", my_predbat, slots, True, 12, expected_rates)

    # Test 9: Yesterday slots (negative minutes) - day -1 has its own limit
    print("\n**** Test 9: Yesterday slots (negative minutes) ****")
    slots = []
    expected_rates = {}

    # Add 14 slots yesterday (should only get 12 cheap)
    for i in range(14):
        slot_start = midnight_utc - timedelta(days=1) + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        yesterday_minute = -1440 + i * 30
        for minute in range(yesterday_minute, yesterday_minute + 30):
            if i < 12:  # Only first 12 slots get cheap rate
                expected_rates[minute] = 4.0
            else:
                expected_rates[minute] = 10.0

    failed |= run_rate_add_io_slots_test("test9_yesterday", my_predbat, slots, True, 12, expected_rates)

    # Test 10: Location not AT_HOME should be ignored
    print("\n**** Test 10: Non-home location ignored ****")
    slot_start = midnight_utc + timedelta(hours=2)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AWAY"}]

    expected_rates = {}
    for minute in range(120, 150):
        expected_rates[minute] = 10.0  # Should stay at default (not AT_HOME)

    failed |= run_rate_add_io_slots_test("test10_away_location", my_predbat, slots, True, 12, expected_rates)

    # Test 11: Day boundary test - slot exactly at midnight
    print("\n**** Test 11: Slot at midnight boundary ****")
    slot_start = midnight_utc  # Exactly midnight
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]

    expected_rates = {}
    for minute in range(0, 30):  # 00:00 - 00:30 is day 0
        expected_rates[minute] = 4.0

    failed |= run_rate_add_io_slots_test("test11_midnight_boundary", my_predbat, slots, True, 12, expected_rates)

    # Test 12: Verify minute -1 is day -1, not day -2 (floor division test)
    print("\n**** Test 12: Minute -1 should be day -1 ****")
    # Create a slot from 23:30 yesterday to 00:00 today
    slot_start = midnight_utc - timedelta(minutes=30)  # 23:30 yesterday
    slot_end = midnight_utc  # 00:00 today
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]

    expected_rates = {}
    for minute in range(-30, 0):  # -30 to -1 should all be day -1
        expected_rates[minute] = 4.0

    failed |= run_rate_add_io_slots_test("test12_floor_division", my_predbat, slots, True, 12, expected_rates)

    # Restore original forecast_minutes
    my_predbat.forecast_minutes = original_forecast_minutes

    if failed:
        print("\n**** rate_add_io_slots tests: FAILED ****")
    else:
        print("\n**** rate_add_io_slots tests: PASSED ****")

    return failed
