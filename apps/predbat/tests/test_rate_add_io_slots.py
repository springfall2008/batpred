# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
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
    result_rates = my_predbat.rate_add_io_slots(0, rates, slots)

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
    my_predbat.rate_min_base = 4

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

    # Test 7: octopus_slot_low_rate=False must derive the assumed price from the `rates` argument, not
    # self.rate_import. Regression tied to fetch's atomic publish: self.rate_import now holds the
    # previous cycle's data during the rebuild. Stage a stale self.rate_import (99p) distinct from the
    # 10p working rates and require the slot to keep the 10p working value.
    print("\n**** Test 7: octopus_slot_low_rate=False uses rates arg, not self.rate_import ****")
    saved_rate_import, saved_rate_min = my_predbat.rate_import, my_predbat.rate_min
    my_predbat.rate_import = {minute: 99.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    my_predbat.rate_min = 99.0  # so even the fallback is 99: the only way to see 10 is reading `rates`
    slot_start = midnight_utc + timedelta(hours=2)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    expected_rates = {minute: 10.0 for minute in range(120, 150)}  # from the 10p rates arg, not the 99p stale self.rate_import
    failed |= run_rate_add_io_slots_test("test7_low_rate_false_uses_rates_arg", my_predbat, slots, False, 12, expected_rates)
    my_predbat.rate_import, my_predbat.rate_min = saved_rate_import, saved_rate_min

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

    # Test 13: Partial slot spanning multiple 30-min periods (Issue #3328)
    # IOG slot from 19:30 to 20:15 (45 mins) should make BOTH 19:30-20:00 AND 20:00-20:30 off-peak
    print("\n**** Test 13: Partial slot spanning multiple 30-min periods (Issue #3328) ****")
    slot_start = midnight_utc + timedelta(hours=19, minutes=30)  # 19:30
    slot_end = slot_start + timedelta(minutes=45)  # 20:15 (45 mins = spans into second 30-min slot)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 5.76, "source": "smart-charge", "location": "AT_HOME"}]

    expected_rates = {}
    # First 30-min slot: 19:30-20:00 (minutes 1170-1199) - fully covered, should be cheap
    for minute in range(1170, 1200):
        expected_rates[minute] = 4.0
    # Second 30-min slot: 20:00-20:30 (minutes 1200-1229) - partially covered (20:00-20:15), should ALSO be cheap
    for minute in range(1200, 1230):
        expected_rates[minute] = 4.0  # Expected behavior: entire 30-min slot should be off-peak

    failed |= run_rate_add_io_slots_test("test13_partial_slot_issue3328", my_predbat, slots, True, 12, expected_rates)

    # Test 14: Another partial slot example - tiny overlap within a 30-min slot should make entire slot off-peak
    print("\n**** Test 14: Tiny overlap (within one 30-min slot) should still make entire 30-min slot off-peak ****")
    slot_start = midnight_utc + timedelta(hours=13, minutes=37, seconds=11)  # 13:37:11
    slot_end = slot_start + timedelta(minutes=1, seconds=16)  # 13:38:27 (fully within the same 5-min and 30-min slot)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 0.16, "source": "smart-charge", "location": "AT_HOME"}]

    expected_rates = {}
    # 13:30-14:00 slot (minutes 810-839) - should be cheap because IOG slot touches it
    for minute in range(810, 840):
        expected_rates[minute] = 4.0

    failed |= run_rate_add_io_slots_test("test14_tiny_overlap_issue3328", my_predbat, slots, True, 12, expected_rates)

    # Test 15: Midday-to-midday cap boundary
    # 14 slots starting at 22:00 today (minute 1320) and crossing midnight into the early hours of tomorrow.
    # All slots fall within the same midday-to-midday period (noon today → noon tomorrow), so the
    # 12-slot cap applies across midnight and only the first 12 slots should be cheap.
    # Under the old midnight-to-midnight logic, today would have 4 cheap slots and tomorrow 10
    # cheap slots (each under the limit), so ALL 14 would be cheap — the opposite of what we want.
    print("\n**** Test 15: Midday-to-midday cap spans midnight ****")
    slots = []
    expected_rates = {}
    for i in range(14):
        slot_start = midnight_utc + timedelta(minutes=1320 + i * 30)  # From 22:00, each 30 min
        slot_end = slot_start + timedelta(minutes=30)
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
        start_minute = 1320 + i * 30
        for minute in range(start_minute, start_minute + 30):
            if i < 12:  # First 12 slots (22:00 – 04:00) are cheap; last 2 are not
                expected_rates[minute] = 4.0
            else:
                expected_rates[minute] = 10.0

    failed |= run_rate_add_io_slots_test("test15_midday_to_midday_boundary", my_predbat, slots, True, 12, expected_rates)

    # Test 16: Duplicate slot detection — completed (location=AT_HOME) and planned (no location)
    # for the same time range.  The planned slot arrives second in the list; because the
    # completed slot already claimed every minute via saved_slots, the planned slot should
    # be a no-op: rates unchanged, cap counter not incremented a second time.
    print("\n**** Test 16: Completed slot overrides duplicate planned slot ****")
    slot_start_16 = midnight_utc + timedelta(hours=14, minutes=30)  # 14:30
    slot_end_16 = slot_start_16 + timedelta(minutes=30)  # 15:00
    slots = [
        # Completed dispatch (location set) — arrives first, is the authoritative record
        {"start": slot_start_16.strftime(TIME_FORMAT), "end": slot_end_16.strftime(TIME_FORMAT), "charge_in_kwh": 2.49, "source": "unknown", "location": "AT_HOME"},
        # Planned dispatch (no location) for the same block — should be fully suppressed
        {"start": slot_start_16.strftime(TIME_FORMAT), "end": slot_end_16.strftime(TIME_FORMAT), "charge_in_kwh": 3.72, "source": "SMART", "location": ""},
    ]
    expected_rates_16 = {}
    for minute in range(870, 900):  # 14:30–15:00 should be cheap
        expected_rates_16[minute] = 4.0
    failed |= run_rate_add_io_slots_test("test16_completed_overrides_planned", my_predbat, slots, True, 12, expected_rates_16)

    # Test 17: Duplicate slot does not consume a second slot from the cap budget.
    # Cap is set to 2.  First slot (14:30–15:00) comes as completed+planned duplicate.
    # Second distinct slot (15:00–15:30) is purely planned.
    # Without dedup, the duplicate would spend 2 of the 2 cap slots and the second
    # distinct slot would be priced at rate_max.  With dedup, only 1 cap slot is used
    # for 14:30–15:00, leaving room for 15:00–15:30 to also be cheap.
    print("\n**** Test 17: Duplicate slot does not consume extra cap budget ****")
    slot_start_17a = midnight_utc + timedelta(hours=14, minutes=30)
    slot_end_17a = slot_start_17a + timedelta(minutes=30)
    slot_start_17b = slot_end_17a
    slot_end_17b = slot_start_17b + timedelta(minutes=30)
    slots_17 = [
        {"start": slot_start_17a.strftime(TIME_FORMAT), "end": slot_end_17a.strftime(TIME_FORMAT), "charge_in_kwh": 2.49, "source": "unknown", "location": "AT_HOME"},
        {"start": slot_start_17a.strftime(TIME_FORMAT), "end": slot_end_17a.strftime(TIME_FORMAT), "charge_in_kwh": 3.72, "source": "SMART", "location": ""},
        {"start": slot_start_17b.strftime(TIME_FORMAT), "end": slot_end_17b.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "SMART", "location": ""},
    ]
    expected_rates_17 = {}
    for minute in range(870, 930):  # Both 14:30–15:00 and 15:00–15:30 should be cheap
        expected_rates_17[minute] = 4.0
    failed |= run_rate_add_io_slots_test("test17_dup_does_not_waste_cap", my_predbat, slots_17, True, 2, expected_rates_17)

    # Tests 18-23 (#4482): octopus_intelligent_limit_future_slots - Octopus often grants more
    # daytime dispatch slots than the car actually needs (it can't see the car's real SoC, only
    # Predbat can). These check that a future out-of-window slot outside what car_charging_slots
    # still lists a positive kwh for doesn't get the low rate, since it's heading for the same
    # rescission risk as an unconfirmed slot.
    saved_car_charging_slots = my_predbat.car_charging_slots[0]

    # Test 18: EV needs the first 2 of 5 future half-hour dispatches - only those two modify rates.
    print("\n**** Test 18: Only future blocks the car still needs get the low rate ****")
    slots_18 = []
    for i in range(5):
        slot_start = midnight_utc + timedelta(hours=14, minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots_18.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
    car_slot_start = int(((midnight_utc + timedelta(hours=14)) - midnight_utc).total_seconds() / 60)  # 840
    my_predbat.car_charging_slots[0] = [
        {"start": car_slot_start, "end": car_slot_start + 30, "kwh": 2.5, "average": 4, "cost": 10, "soc": 5, "octopus": True},
        {"start": car_slot_start + 30, "end": car_slot_start + 60, "kwh": 2.5, "average": 4, "cost": 10, "soc": 10, "octopus": True},
        {"start": car_slot_start + 60, "end": car_slot_start + 90, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True},
        {"start": car_slot_start + 90, "end": car_slot_start + 120, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True},
        {"start": car_slot_start + 120, "end": car_slot_start + 150, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True},
    ]
    expected_rates_18 = {}
    for minute in range(car_slot_start, car_slot_start + 60):
        expected_rates_18[minute] = 4.0
    for minute in range(car_slot_start + 60, car_slot_start + 150):
        expected_rates_18[minute] = 10.0
    my_predbat.octopus_intelligent_limit_future_slots = True
    failed |= run_rate_add_io_slots_test("test18_only_needed_future_blocks_low_rate", my_predbat, slots_18, True, 12, expected_rates_18)

    # Test 19: the final required charge ends part-way through a dispatch (14:15 of a 14:00-14:30
    # slot) - the whole touched 30-min settlement period should still be treated as needed, not
    # just the covered minutes, matching how the slot rounding elsewhere in this function works.
    print("\n**** Test 19: Partially-needed settlement period stays fully low rate ****")
    my_predbat.car_charging_slots[0] = [
        {"start": car_slot_start, "end": car_slot_start + 15, "kwh": 1.25, "average": 4, "cost": 5, "soc": 5, "octopus": True},
        {"start": car_slot_start + 15, "end": car_slot_start + 30, "kwh": 0.0, "average": 4, "cost": 0, "soc": 5, "octopus": True},
        {"start": car_slot_start + 30, "end": car_slot_start + 150, "kwh": 0.0, "average": 4, "cost": 0, "soc": 5, "octopus": True},
    ]
    expected_rates_19 = {}
    for minute in range(car_slot_start, car_slot_start + 30):
        expected_rates_19[minute] = 4.0
    for minute in range(car_slot_start + 30, car_slot_start + 150):
        expected_rates_19[minute] = 10.0
    failed |= run_rate_add_io_slots_test("test19_partial_settlement_period_stays_low_rate", my_predbat, slots_18, True, 12, expected_rates_19)

    # Test 20: car already at its limit (car_charging_slots has nothing but zero-kwh entries) - no
    # dispatch that hasn't started modifies the tariff.
    print("\n**** Test 20: Car already full - no future dispatch gets the low rate ****")
    my_predbat.car_charging_slots[0] = [{"start": car_slot_start, "end": car_slot_start + 150, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True}]
    expected_rates_20 = {minute: 10.0 for minute in range(car_slot_start, car_slot_start + 150)}
    failed |= run_rate_add_io_slots_test("test20_car_already_full_no_low_rate", my_predbat, slots_18, True, 12, expected_rates_20)

    # Test 21: a dispatch already underway (its slot_start is at-or-before minutes_now, 10:00) is
    # trusted regardless of what car_charging_slots says about future need - only slots that
    # haven't started yet are gated.
    print("\n**** Test 21: Current/completed dispatch periods stay low rate regardless ****")
    slot_start_21 = midnight_utc + timedelta(hours=9, minutes=30)  # 09:30, already underway at 10:00
    slot_end_21 = slot_start_21 + timedelta(minutes=30)
    slots_21 = [{"start": slot_start_21.strftime(TIME_FORMAT), "end": slot_end_21.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    my_predbat.car_charging_slots[0] = []  # Car charging plan says nothing is needed at all
    expected_rates_21 = {minute: 4.0 for minute in range(570, 600)}  # 09:30-10:00
    failed |= run_rate_add_io_slots_test("test21_current_dispatch_stays_low_rate", my_predbat, slots_21, True, 12, expected_rates_21)

    # Test 22: feature disabled - existing (unconditional) behaviour is unchanged even with an
    # empty car_charging_slots that would otherwise have excluded every block.
    print("\n**** Test 22: Switch off restores unconditional low rate ****")
    my_predbat.octopus_intelligent_limit_future_slots = False
    my_predbat.car_charging_slots[0] = []
    expected_rates_22 = {}
    for minute in range(car_slot_start, car_slot_start + 150):
        expected_rates_22[minute] = 4.0
    failed |= run_rate_add_io_slots_test("test22_switch_off_restores_old_behaviour", my_predbat, slots_18, True, 12, expected_rates_22)

    # Test 23: a future out-of-window slot the car doesn't need is excluded, but a slot inside the
    # fixed 23:30-05:30 window is never affected regardless, since it's guaranteed cheap by the
    # tariff itself, not the dispatch mechanism.
    print("\n**** Test 23: Fixed IOG window unaffected regardless of future need ****")
    slot_start_23 = midnight_utc + timedelta(hours=2)  # 02:00 - well inside 23:30-05:30
    slot_end_23 = slot_start_23 + timedelta(minutes=30)
    slots_23 = [{"start": slot_start_23.strftime(TIME_FORMAT), "end": slot_end_23.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    my_predbat.octopus_intelligent_limit_future_slots = True
    my_predbat.car_charging_slots[0] = []  # Car charging plan says nothing is needed at all
    expected_rates_23 = {minute: 4.0 for minute in range(120, 150)}
    failed |= run_rate_add_io_slots_test("test23_fixed_window_unaffected", my_predbat, slots_23, True, 12, expected_rates_23)

    # Test 24 (#4483 review follow-up): a rejected future slot must actively restore
    # rates[minute] to rate_max_base, not just skip adding a new low rate. For a genuine Octopus
    # Intelligent tariff, fetch_octopus_rates() can receive the dispatch-discounted rate directly
    # before rate_add_io_slots() ever runs (rate_replicate() only gap-fills minutes with no real
    # fetched value, so it never touches this one) - simulate that by pre-seeding the rejected
    # slot's minutes with a low rate and self.io_adjusted, then confirm rejection restores both.
    print("\n**** Test 24: Rejected slot restores an already-discounted fetched rate ****")
    my_predbat.car_charging_slots[0] = []  # Car charging plan says nothing is needed at all

    slot_start_24 = midnight_utc + timedelta(hours=14)  # future, out-of-window, car doesn't need it
    slot_end_24 = slot_start_24 + timedelta(minutes=30)
    slots_24 = [{"start": slot_start_24.strftime(TIME_FORMAT), "end": slot_end_24.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    slot_start_minute_24 = int((slot_start_24 - midnight_utc).total_seconds() / 60)

    rates_24 = {}
    for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60)):
        rates_24[minute] = 10.0
    saved_io_adjusted = dict(my_predbat.io_adjusted)
    for minute in range(slot_start_minute_24, slot_start_minute_24 + 30):
        rates_24[minute] = 3.99  # already-discounted, as if fetched directly for a real dispatch
        my_predbat.io_adjusted[minute] = True  # minute_data() marks every minute in the block

    result_rates_24 = my_predbat.rate_add_io_slots(0, rates_24, slots_24)

    for minute in range(slot_start_minute_24, slot_start_minute_24 + 30):
        if result_rates_24.get(minute) != my_predbat.rate_max_base:
            print("ERROR: Minute {} should be restored to rate_max_base {} but got {}".format(minute, my_predbat.rate_max_base, result_rates_24.get(minute)))
            failed = True
        if minute in my_predbat.io_adjusted:
            print("ERROR: Minute {} should have been cleared from io_adjusted, still present".format(minute))
            failed = True

    my_predbat.io_adjusted = saved_io_adjusted
    my_predbat.octopus_intelligent_limit_future_slots = False  # Restore default for any subsequent tests

    my_predbat.car_charging_slots[0] = saved_car_charging_slots

    # Test 25 (#4483 review follow-up, Speshman): a slot rejected purely because octopus_slot_max
    # was already reached (needed stays True throughout - this happens even with
    # octopus_intelligent_limit_future_slots Off, since the daily cap is a pre-existing, unrelated
    # mechanism) must NOT destructively restore rates[minute] - that active restore is reserved
    # for the needed=False case (#4482) above, where Predbat has positive reason to believe
    # Octopus has rescinded the slot. A cap-rejected slot may still be a genuine live
    # dispatch/tariff event; Predbat is only choosing not to count it against its own budget.
    print("\n**** Test 25: Cap-only rejection leaves an already-discounted fetched rate alone ****")
    my_predbat.octopus_intelligent_limit_future_slots = False  # feature off - needed is always True regardless

    slot_start_25a = midnight_utc + timedelta(hours=1)  # 01:00-01:30, consumes the only cap slot for the day
    slot_end_25a = slot_start_25a + timedelta(minutes=30)
    slot_start_25b = slot_end_25a  # 01:30-02:00, rejected purely because the cap is already spent
    slot_end_25b = slot_start_25b + timedelta(minutes=30)
    slots_25 = [
        {"start": slot_start_25a.strftime(TIME_FORMAT), "end": slot_end_25a.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"},
        {"start": slot_start_25b.strftime(TIME_FORMAT), "end": slot_end_25b.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"},
    ]
    slot_start_minute_25a = int((slot_start_25a - midnight_utc).total_seconds() / 60)
    slot_start_minute_25b = int((slot_start_25b - midnight_utc).total_seconds() / 60)

    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 1
    rates_25 = {}
    for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60)):
        rates_25[minute] = 10.0
    saved_io_adjusted_25 = dict(my_predbat.io_adjusted)
    for minute in range(slot_start_minute_25b, slot_start_minute_25b + 30):
        rates_25[minute] = 3.99  # already-discounted, as if fetched directly for a real dispatch
        my_predbat.io_adjusted[minute] = True  # minute_data() marks every minute in the block

    result_rates_25 = my_predbat.rate_add_io_slots(0, rates_25, slots_25)

    for minute in range(slot_start_minute_25a, slot_start_minute_25a + 30):
        if result_rates_25.get(minute) != my_predbat.rate_min_base:
            print("ERROR: Minute {} (first, within cap) should be the low rate {} but got {}".format(minute, my_predbat.rate_min_base, result_rates_25.get(minute)))
            failed = True
    for minute in range(slot_start_minute_25b, slot_start_minute_25b + 30):
        if result_rates_25.get(minute) != 3.99:
            print("ERROR: Minute {} (second, cap-only rejection) should be left at the already-discounted 3.99, got {}".format(minute, result_rates_25.get(minute)))
            failed = True
        if minute not in my_predbat.io_adjusted:
            print("ERROR: Minute {} should still be marked io_adjusted (cap-only rejection), was cleared".format(minute))
            failed = True

    my_predbat.io_adjusted = saved_io_adjusted_25

    # Test 26 (#4483 review follow-up, Speshman): the same cap-only-rejection preservation
    # applies to a slot inside the guaranteed 23:30-05:30 fixed window too - it's still just a
    # cap-driven rejection (needed stays True via the fixed-window clause), not a needed=False
    # rescission, so the destructive restore must not fire there either.
    print("\n**** Test 26: Cap-only rejection inside the fixed window also leaves the rate alone ****")
    my_predbat.octopus_intelligent_limit_future_slots = True  # needed forced True here via the fixed window, not the "off" shortcut
    my_predbat.car_charging_slots[0] = []  # car charging plan says nothing is needed - irrelevant, fixed window forces needed=True anyway

    slot_start_26a = midnight_utc + timedelta(hours=1)  # 01:00-01:30, inside 23:30-05:30, consumes the only cap slot
    slot_end_26a = slot_start_26a + timedelta(minutes=30)
    slot_start_26b = slot_end_26a  # 01:30-02:00, also inside the fixed window, rejected purely by the cap
    slot_end_26b = slot_start_26b + timedelta(minutes=30)
    slots_26 = [
        {"start": slot_start_26a.strftime(TIME_FORMAT), "end": slot_end_26a.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"},
        {"start": slot_start_26b.strftime(TIME_FORMAT), "end": slot_end_26b.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"},
    ]
    slot_start_minute_26b = int((slot_start_26b - midnight_utc).total_seconds() / 60)

    rates_26 = {}
    for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60)):
        rates_26[minute] = 10.0
    saved_io_adjusted_26 = dict(my_predbat.io_adjusted)
    for minute in range(slot_start_minute_26b, slot_start_minute_26b + 30):
        rates_26[minute] = 3.99
        my_predbat.io_adjusted[minute] = True

    result_rates_26 = my_predbat.rate_add_io_slots(0, rates_26, slots_26)

    for minute in range(slot_start_minute_26b, slot_start_minute_26b + 30):
        if result_rates_26.get(minute) != 3.99:
            print("ERROR: Minute {} (fixed-window, cap-only rejection) should be left at 3.99, got {}".format(minute, result_rates_26.get(minute)))
            failed = True
        if minute not in my_predbat.io_adjusted:
            print("ERROR: Minute {} (fixed-window) should still be marked io_adjusted, was cleared".format(minute))
            failed = True

    my_predbat.io_adjusted = saved_io_adjusted_26
    my_predbat.octopus_intelligent_limit_future_slots = False
    my_predbat.car_charging_slots[0] = saved_car_charging_slots

    # Tests 27-30 (#4483 review follow-up): octopus_slot_count_zero_kwh - a zero-kWh dispatch
    # entry (e.g. a plug-independent SMART grid-flex event that delivers no energy to the car)
    # is a real tariff discount, but not a car-charging dispatch. Switching this off exempts it
    # from both the #4482 "does the car still need this" check and the octopus_slot_max cap; the
    # switch defaults on, so a household only gets that exemption if it opts in (#4483 review
    # follow-up, Speshman - defaulting it off risked silently loosening the cap for everyone).

    print("\n**** Test 27: Zero-kWh slot gets the low rate even when the car doesn't need it, once exempted ****")
    my_predbat.octopus_slot_count_zero_kwh = False
    my_predbat.octopus_intelligent_limit_future_slots = True
    my_predbat.car_charging_slots[0] = []  # car doesn't need anything - would normally reject a future out-of-window slot
    slot_start_27 = midnight_utc + timedelta(hours=14)  # future, out-of-window
    slot_end_27 = slot_start_27 + timedelta(minutes=30)
    slots_27 = [{"start": slot_start_27.strftime(TIME_FORMAT), "end": slot_end_27.strftime(TIME_FORMAT), "charge_in_kwh": 0.0, "source": "SMART", "location": ""}]
    slot_start_minute_27 = int((slot_start_27 - midnight_utc).total_seconds() / 60)
    expected_rates_27 = {minute: 4.0 for minute in range(slot_start_minute_27, slot_start_minute_27 + 30)}
    failed |= run_rate_add_io_slots_test("test27_zero_kwh_exempt_when_switched_off", my_predbat, slots_27, True, 12, expected_rates_27)

    print("\n**** Test 28: Zero-kWh slot does not consume octopus_slot_max budget, once exempted ****")
    my_predbat.octopus_intelligent_limit_future_slots = False
    slot_start_28a = midnight_utc + timedelta(hours=1)  # zero-kWh, first
    slot_end_28a = slot_start_28a + timedelta(minutes=30)
    slot_start_28b = slot_end_28a  # genuine, kwh>0, second - must still fit under cap=1
    slot_end_28b = slot_start_28b + timedelta(minutes=30)
    slots_28 = [
        {"start": slot_start_28a.strftime(TIME_FORMAT), "end": slot_end_28a.strftime(TIME_FORMAT), "charge_in_kwh": 0.0, "source": "SMART", "location": ""},
        {"start": slot_start_28b.strftime(TIME_FORMAT), "end": slot_end_28b.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"},
    ]
    slot_start_minute_28a = int((slot_start_28a - midnight_utc).total_seconds() / 60)
    expected_rates_28 = {minute: 4.0 for minute in range(slot_start_minute_28a, slot_start_minute_28a + 60)}  # both slots cheap
    failed |= run_rate_add_io_slots_test("test28_zero_kwh_does_not_spend_cap_when_switched_off", my_predbat, slots_28, True, 1, expected_rates_28)

    print("\n**** Test 29: Default behaviour - zero-kWh slots count like any other ****")
    my_predbat.octopus_slot_count_zero_kwh = True
    my_predbat.octopus_intelligent_limit_future_slots = True
    my_predbat.car_charging_slots[0] = []  # car doesn't need anything
    slot_start_29 = midnight_utc + timedelta(hours=14)  # future, out-of-window, zero-kWh
    slot_end_29 = slot_start_29 + timedelta(minutes=30)
    slots_29 = [{"start": slot_start_29.strftime(TIME_FORMAT), "end": slot_end_29.strftime(TIME_FORMAT), "charge_in_kwh": 0.0, "source": "SMART", "location": ""}]
    slot_start_minute_29 = int((slot_start_29 - midnight_utc).total_seconds() / 60)
    expected_rates_29 = {minute: 10.0 for minute in range(slot_start_minute_29, slot_start_minute_29 + 30)}  # rejected, not needed, and counted by default
    failed |= run_rate_add_io_slots_test("test29_default_zero_kwh_subject_to_needed_gate", my_predbat, slots_29, True, 12, expected_rates_29)
    my_predbat.octopus_slot_count_zero_kwh = False  # test30 exercises the exemption's source scoping, needs it switched off

    # Test 30 (#4483 review follow-up, Speshman): the zero-kWh exemption is scoped to
    # source == "SMART" - decode_octopus_slot() silently coerces malformed/unparseable
    # charge_in_kwh input to 0.0, indistinguishable by value alone from a genuine zero-kWh
    # SMART event. A zero-kWh entry with any other source must not get the exemption - it goes
    # through the same needed/cap gate as any other slot, and here the car doesn't need it.
    print("\n**** Test 30: Zero-kWh slot with a non-SMART source is not exempt ****")
    my_predbat.octopus_intelligent_limit_future_slots = True
    my_predbat.car_charging_slots[0] = []  # car doesn't need anything
    slot_start_30 = midnight_utc + timedelta(hours=14)  # future, out-of-window, zero-kWh
    slot_end_30 = slot_start_30 + timedelta(minutes=30)
    slots_30 = [{"start": slot_start_30.strftime(TIME_FORMAT), "end": slot_end_30.strftime(TIME_FORMAT), "charge_in_kwh": 0.0, "source": "smart-charge", "location": "AT_HOME"}]
    slot_start_minute_30 = int((slot_start_30 - midnight_utc).total_seconds() / 60)
    expected_rates_30 = {minute: 10.0 for minute in range(slot_start_minute_30, slot_start_minute_30 + 30)}  # rejected, not needed, not exempt
    failed |= run_rate_add_io_slots_test("test30_zero_kwh_non_smart_source_not_exempt", my_predbat, slots_30, True, 12, expected_rates_30)

    # Test 31 (#4807): a malformed charge_in_kwh (not a genuine zero-kWh event) must not get the
    # exemption even when its source is "SMART" - decode_octopus_slot() coerces the unparseable
    # value to kwh==0 the same way a real zero-kWh entry would, so source alone can't tell them
    # apart. kwh_valid closes that gap.
    print("\n**** Test 31: Malformed charge_in_kwh with source SMART is not exempt ****")
    slot_start_31 = midnight_utc + timedelta(hours=14)  # future, out-of-window
    slot_end_31 = slot_start_31 + timedelta(minutes=30)
    slots_31 = [{"start": slot_start_31.strftime(TIME_FORMAT), "end": slot_end_31.strftime(TIME_FORMAT), "charge_in_kwh": "not-a-number", "source": "SMART", "location": ""}]
    slot_start_minute_31 = int((slot_start_31 - midnight_utc).total_seconds() / 60)
    expected_rates_31 = {minute: 10.0 for minute in range(slot_start_minute_31, slot_start_minute_31 + 30)}  # rejected, not needed, not exempt
    failed |= run_rate_add_io_slots_test("test31_malformed_smart_kwh_not_exempt", my_predbat, slots_31, True, 12, expected_rates_31)

    my_predbat.octopus_slot_count_zero_kwh = True  # Restore default for any subsequent tests
    my_predbat.octopus_intelligent_limit_future_slots = False  # Restore default for any subsequent tests
    my_predbat.car_charging_slots[0] = saved_car_charging_slots

    # Restore original forecast_minutes
    my_predbat.forecast_minutes = original_forecast_minutes

    if failed:
        print("\n**** rate_add_io_slots tests: FAILED ****")
    else:
        print("\n**** rate_add_io_slots tests: PASSED ****")

    return failed
