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


def run_rate_add_io_slots_test(testname, my_predbat, slots, octopus_slot_low_rate, octopus_slot_max, expected_rates, expected_slots_per_day=None, confirmed=True):
    """
    Run a single test for rate_add_io_slots

    confirmed controls whether the passed-in slots are tagged _confirmed (as fetch_sensor_data_cars()
    tags a completed_dispatches entry - #4516) or not (as a planned_dispatches entry). Defaults True
    so tests 1-25, which predate #4516 and are about cap/dedup/low_rate/off-peak-pricing mechanics
    rather than confirmation gating, don't need to know about it - tests exercising
    trust_future_dynamic_iog_slots pass confirmed=False explicitly where that's the point being tested.
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

    # Run the function. trusted_dynamic_minutes is a per-cycle accumulator that fetch.py clears at
    # the start of each rate rebuild - clear it here too so one test's trusted minutes can't leak
    # into the next one's assertions on it.
    my_predbat.trusted_dynamic_minutes = set()
    tagged_slots = [dict(slot, _confirmed=confirmed) if "_confirmed" not in slot else slot for slot in slots]
    # Mirror fetch.py's sequence: octopus_slots is populated, then the cross-car shield is resolved
    # across every car, and only then does the per-car loop run. Resolving it here rather than
    # hand-setting it keeps these tests honest about the real call order (#5110 review).
    saved_octopus_slots = my_predbat.octopus_slots
    my_predbat.octopus_slots = [tagged_slots] + [[] for _ in range(max(0, my_predbat.num_cars - 1))]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    result_rates = my_predbat.rate_add_io_slots(0, rates, tagged_slots)
    my_predbat.octopus_slots = saved_octopus_slots

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

    # Test 10: Location not AT_HOME on a slot that hasn't happened yet should be ignored.
    # (minutes_now is 10:00, so a 14:00 slot is still to come.)  A genuinely-away car would
    # otherwise have the planner import against a cheap window that never materialises.
    print("\n**** Test 10: Non-home location on a future slot ignored ****")
    slot_start = midnight_utc + timedelta(hours=14)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AWAY"}]

    expected_rates = {}
    for minute in range(840, 870):
        expected_rates[minute] = 10.0  # Should stay at default (not AT_HOME, and not yet completed)

    failed |= run_rate_add_io_slots_test("test10_away_location_future", my_predbat, slots, True, 12, expected_rates)

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

    # Test 18: Zero kWh slot in the past does not consume cap budget or get the cheap rate.
    # A dispatch that delivered (or was withdrawn / never actually happened and simply hasn't been
    # metered as) zero kWh shouldn't eat into the day's slot cap the way a genuine dispatch does.
    # 13 slots yesterday (all fully in the past), index 4 is zero kWh: without the fix, all 13
    # compete for the 12-slot cap and the 13th (chronologically last) loses out; with the fix the
    # zero-kWh slot is skipped entirely, leaving 12 real slots - all of which fit under the cap.
    print("\n**** Test 18: Zero kWh slot in the past does not consume cap budget ****")
    slots = []
    expected_rates = {}
    for i in range(13):
        slot_start = midnight_utc - timedelta(days=1) + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        kwh = 0 if i == 4 else 2.5
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": kwh, "source": "smart-charge", "location": "AT_HOME"})
        yesterday_minute = -1440 + i * 30
        for minute in range(yesterday_minute, yesterday_minute + 30):
            expected_rates[minute] = 10.0 if i == 4 else 4.0  # the zero-kWh slot itself is left at the default rate

    failed |= run_rate_add_io_slots_test("test18_zero_kwh_past_excluded", my_predbat, slots, True, 12, expected_rates)

    # Test 19: Zero kWh slot in the future still counts toward the cap and gets the cheap rate -
    # the Test 18 exclusion is scoped to slots that have already happened, not ones still to come
    # (a future planned dispatch's kWh is usually synthesised rather than genuinely zero, but the
    # cap logic must not rely on that - it should treat a future zero-kWh slot the same as before).
    print("\n**** Test 19: Zero kWh slot in the future is not excluded ****")
    slots = []
    expected_rates = {}
    for i in range(13):  # 13 slots today, starting at 13:00 (after minutes_now=10:00, and after the
        # midday cap boundary so all 13 land in the same midday-to-midday period), zero-kwh at index 4
        slot_start = midnight_utc + timedelta(hours=13) + timedelta(minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        kwh = 0 if i == 4 else 2.5
        slots.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": kwh, "source": "smart-charge", "location": "AT_HOME"})
        start_minute = 780 + i * 30  # 13:00 = minute 780
        for minute in range(start_minute, start_minute + 30):
            expected_rates[minute] = 4.0 if i < 12 else 10.0  # the zero-kWh slot at i=4 still consumes a cap slot

    failed |= run_rate_add_io_slots_test("test19_zero_kwh_future_not_excluded", my_predbat, slots, True, 12, expected_rates)

    # Test 20: The midday cap boundary must not re-stamp the day-rate tail of an overnight window.
    # The cap counter is keyed on the current minute, so a dispatch window that starts in the evening
    # and runs past noon the next day fills its budget against day -1 (21:00-03:00) and is then handed
    # a *fresh* budget at 12:00, re-stamping 12:00-15:00 at the off-peak rate in the middle of the
    # day-rate block. Reported on Octopus Intelligent Go (8p off-peak / 33.72p day): the planner saw
    # "Best charge window [12:00-13:30 @ 8.0p]", exported the battery into a 10.08p outgoing window at
    # 11:00-12:00 and bought it back at the real 33.72p.
    # Here 4.0 stands for the off-peak rate and 10.0 for the day rate.
    print("\n**** Test 20: Midday reset must not stamp the day-rate tail of an overnight window ****")
    slot_start = midnight_utc - timedelta(hours=3)  # 21:00 yesterday, minute -180
    slot_end = midnight_utc + timedelta(hours=15)  # 15:00 today, minute 900
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 124.04, "source": "smart-charge", "location": "AT_HOME"}]

    expected_rates = {}
    for minute in range(-180, 180):  # 21:00 -> 03:00: the first 12 slots, legitimately off-peak
        expected_rates[minute] = 4.0
    for minute in range(720, 900):  # 12:00 -> 15:00: day rate, must NOT be re-stamped off-peak
        expected_rates[minute] = 10.0

    failed |= run_rate_add_io_slots_test("test20_midday_reset_stamps_day_rate", my_predbat, slots, True, 12, expected_rates)

    # Test 21: A completed dispatch is priced off-peak whatever location it is reported at (issue #4946).
    # Octopus bills completed smart-charge energy at the off-peak rate regardless of location, and the
    # label is not stable - the same completed dispatch can be relabelled AT_HOME to AWAY hours later,
    # which used to un-stamp the cheap rate and step the day's reported cost up on an hour with no import.
    print("\n**** Test 21: Completed AWAY dispatch is still off-peak (issue #4946) ****")
    slot_start = midnight_utc + timedelta(hours=2)  # 02:00, fully in the past (minutes_now is 10:00)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AWAY"}]

    expected_rates = {}
    for minute in range(120, 150):
        expected_rates[minute] = 4.0  # Completed, so priced the same as AT_HOME

    failed |= run_rate_add_io_slots_test("test21_completed_away_is_off_peak", my_predbat, slots, True, 12, expected_rates)

    # Test 22: The same holds for the other non-home label Octopus reports, UNABLE_TO_IDENTIFY
    print("\n**** Test 22: Completed UNABLE_TO_IDENTIFY dispatch is still off-peak ****")
    slot_start = midnight_utc + timedelta(hours=3)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "UNABLE_TO_IDENTIFY"}]

    expected_rates = {}
    for minute in range(180, 210):
        expected_rates[minute] = 4.0

    failed |= run_rate_add_io_slots_test("test22_completed_unknown_location_is_off_peak", my_predbat, slots, True, 12, expected_rates)

    # Test 23: An in-progress AWAY dispatch has not completed yet, so it keeps the location test -
    # the relaxation is scoped to slots that have finished, not ones that merely started.
    # Anchored on minutes_now rather than a wall-clock hour so the slot straddles "now" by
    # construction. (minutes_now is in fact a fixed 600 here - both it and the slot times are built
    # from the same aware midnight_utc, which despite its name is local midnight - but pinning the
    # boundary case to minutes_now keeps the test honest if that harness setup ever changes.)
    print("\n**** Test 23: In-progress AWAY dispatch still ignored ****")
    in_progress_start = my_predbat.minutes_now - 15  # started 15 minutes ago, runs 15 minutes more
    slot_start = midnight_utc + timedelta(minutes=in_progress_start)
    slot_end = slot_start + timedelta(minutes=30)
    slots = [{"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AWAY"}]

    expected_rates = {}
    block_start = (in_progress_start // 30) * 30
    block_end = ((in_progress_start + 30 + 29) // 30) * 30
    for minute in range(block_start, block_end):  # every 30-min block the slot would round out to
        expected_rates[minute] = 10.0  # Should stay at default - not completed, and not AT_HOME

    failed |= run_rate_add_io_slots_test("test23_in_progress_away_ignored", my_predbat, slots, True, 12, expected_rates)

    # Test 24: The bump-charge / BOOST exclusions are unaffected by the completed-dispatch relaxation -
    # their cost doesn't change, so they must stay excluded even once location no longer blocks them
    print("\n**** Test 24: Completed bump-charge / BOOST still excluded ****")
    slot_start = midnight_utc + timedelta(hours=4)
    slot_end = slot_start + timedelta(minutes=30)
    boost_start = midnight_utc + timedelta(hours=5)
    boost_end = boost_start + timedelta(minutes=30)
    slots = [
        {"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "bump-charge", "location": "AWAY"},
        {"start": boost_start.strftime(TIME_FORMAT), "end": boost_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "BOOST", "location": "AWAY"},
    ]

    expected_rates = {}
    for minute in range(240, 270):  # 04:00 - 04:30 bump-charge
        expected_rates[minute] = 10.0
    for minute in range(300, 330):  # 05:00 - 05:30 BOOST
        expected_rates[minute] = 10.0

    failed |= run_rate_add_io_slots_test("test24_completed_bump_charge_still_excluded", my_predbat, slots, True, 12, expected_rates)

    # Test 25: A completed non-home dispatch consumes the day's low-rate budget, so a later planned
    # AT_HOME slot in the same midday-to-midday period goes over the cap. This is the other half of
    # the #4946 relaxation: once a completed AWAY dispatch is billed off-peak it must also spend a
    # block, and load_octopus_slots() shares the same predicate so its counter agrees (test in
    # test_octopus_slots.py). Both slots sit in period -1: the cap is keyed on the slot start, so
    # 13:00 yesterday and 11:00 today are the same midday-to-midday window.
    print("\n**** Test 25: Completed AWAY dispatch consumes cap budget ****")
    completed_start = midnight_utc - timedelta(days=1) + timedelta(hours=13)  # 13:00 yesterday, minute -660
    completed_end = completed_start + timedelta(hours=1)  # two 30-min blocks, the whole cap
    planned_start = midnight_utc + timedelta(hours=11)  # 11:00 today, still ahead of the midday boundary
    planned_end = planned_start + timedelta(minutes=30)
    slots = [
        # completed_dispatches are merged ahead of planned_dispatches by fetch.py, and this
        # function does not sort, so the completed slot claims the budget first
        {"start": completed_start.strftime(TIME_FORMAT), "end": completed_end.strftime(TIME_FORMAT), "charge_in_kwh": 5.0, "source": "smart-charge", "location": "AWAY"},
        {"start": planned_start.strftime(TIME_FORMAT), "end": planned_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"},
    ]

    expected_rates = {}
    for minute in range(-660, -600):  # the completed AWAY dispatch is priced off-peak
        expected_rates[minute] = 4.0
    for minute in range(660, 690):  # ...and the planned slot is over the 2-block cap, so day rate
        expected_rates[minute] = 10.0

    failed |= run_rate_add_io_slots_test("test25_completed_away_consumes_cap", my_predbat, slots, True, 2, expected_rates)

    # Tests 26+ (#4482, #4516): octopus_intelligent_limit_future_slots (`needed`) and
    # trust_future_dynamic_iog_slots (`trusted`) - two independent gates a future out-of-window
    # dispatch slot must clear. Fully self-contained, fixed time setup (not derived from the shared
    # my_predbat fixture's ambient now_utc/midnight_utc) - another test running earlier in the suite
    # can leave now_utc/minutes_now inconsistent with midnight_utc, which silently shifts
    # current_block in rate_add_io_slots() and would break the trust-level tests, which depend on
    # minutes_now landing exactly on a specific 30-min block.
    saved_now_utc = my_predbat.now_utc
    saved_midnight_utc = my_predbat.midnight_utc
    saved_minutes_now = my_predbat.minutes_now
    saved_trust_dynamic = my_predbat.trust_future_dynamic_iog_slots
    saved_limit_future_slots = my_predbat.octopus_intelligent_limit_future_slots
    saved_trusted_dynamic_minutes = set(my_predbat.trusted_dynamic_minutes)
    # Saved beside trusted_dynamic_minutes for the same reason: these tests call rate_add_io_slots()
    # directly and so set the cross-car shield themselves, where production resolves it per cycle in
    # fetch.py. Restored at the end so nothing escapes into the shared fixture (#5079).
    saved_protected_dispatch_minutes = set(my_predbat.protected_dispatch_minutes)
    saved_car_charging_now = list(my_predbat.car_charging_now)
    saved_confirmed_slots = getattr(my_predbat, "car_charging_now_confirmed_slots", None)
    saved_args_car_charging_now = my_predbat.args.get("car_charging_now", None)
    saved_car_charging_slots = my_predbat.car_charging_slots[0]

    midnight_utc_26 = my_predbat.midnight_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight_utc = midnight_utc_26
    my_predbat.now_utc = midnight_utc_26 + timedelta(hours=10)
    my_predbat.minutes_now = 10 * 60  # 10:00 exactly, a 30-min block boundary

    # A real car_charging_now sensor must be configured for "started" not to degrade to "none" -
    # tests that specifically exercise the no-sensor fallback clear this again first.
    my_predbat.args["car_charging_now"] = "binary_sensor.fake_charging_now"

    # Test 26 (#4482): EV needs the first 2 of 5 future half-hour dispatches - only those two get the
    # low rate.
    print("\n**** Test 26: Only future blocks the car still needs get the low rate ****")
    slots_26 = []
    for i in range(5):
        slot_start = midnight_utc_26 + timedelta(hours=14, minutes=i * 30)
        slot_end = slot_start + timedelta(minutes=30)
        slots_26.append({"start": slot_start.strftime(TIME_FORMAT), "end": slot_end.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"})
    car_slot_start = int(((midnight_utc_26 + timedelta(hours=14)) - midnight_utc_26).total_seconds() / 60)  # 840
    my_predbat.car_charging_slots[0] = [
        {"start": car_slot_start, "end": car_slot_start + 30, "kwh": 2.5, "average": 4, "cost": 10, "soc": 5, "octopus": True},
        {"start": car_slot_start + 30, "end": car_slot_start + 60, "kwh": 2.5, "average": 4, "cost": 10, "soc": 10, "octopus": True},
        {"start": car_slot_start + 60, "end": car_slot_start + 90, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True},
        {"start": car_slot_start + 90, "end": car_slot_start + 120, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True},
        {"start": car_slot_start + 120, "end": car_slot_start + 150, "kwh": 0.0, "average": 4, "cost": 0, "soc": 10, "octopus": True},
    ]
    expected_rates_26 = {}
    for minute in range(car_slot_start, car_slot_start + 60):
        expected_rates_26[minute] = 4.0
    for minute in range(car_slot_start + 60, car_slot_start + 150):
        expected_rates_26[minute] = 10.0
    my_predbat.octopus_intelligent_limit_future_slots = True
    my_predbat.trust_future_dynamic_iog_slots = "planned"  # isolate the `needed` gate from `trusted`
    failed |= run_rate_add_io_slots_test("test26_only_needed_future_blocks_low_rate", my_predbat, slots_26, True, 12, expected_rates_26)

    # Test 27: a dispatch already underway (its slot_start is at-or-before minutes_now, 10:00) is
    # needed regardless of what car_charging_slots says about future need - only slots that haven't
    # started yet are gated.
    print("\n**** Test 27: Current/completed dispatch periods stay low rate regardless of need ****")
    slot_start_27a = midnight_utc_26 + timedelta(hours=9, minutes=30)  # 09:30, already underway at 10:00
    slot_end_27a = slot_start_27a + timedelta(minutes=30)
    slots_27 = [{"start": slot_start_27a.strftime(TIME_FORMAT), "end": slot_end_27a.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    my_predbat.car_charging_slots[0] = []  # Car charging plan says nothing is needed at all
    expected_rates_27 = {minute: 4.0 for minute in range(570, 600)}  # 09:30-10:00
    failed |= run_rate_add_io_slots_test("test27_current_dispatch_stays_low_rate", my_predbat, slots_27, True, 12, expected_rates_27)

    # Test 28: fixed IOG window unaffected regardless of future need.
    print("\n**** Test 28: Fixed IOG window unaffected regardless of future need ****")
    slot_start_28 = midnight_utc_26 + timedelta(hours=2)  # 02:00 - well inside 23:30-05:30
    slot_end_28 = slot_start_28 + timedelta(minutes=30)
    slots_28 = [{"start": slot_start_28.strftime(TIME_FORMAT), "end": slot_end_28.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    my_predbat.car_charging_slots[0] = []  # Car charging plan says nothing is needed at all
    expected_rates_28 = {minute: 4.0 for minute in range(120, 150)}
    failed |= run_rate_add_io_slots_test("test28_fixed_window_unaffected_by_need", my_predbat, slots_28, True, 12, expected_rates_28)

    my_predbat.octopus_intelligent_limit_future_slots = False
    my_predbat.car_charging_slots[0] = saved_car_charging_slots

    # Tests 29+ (#4516): trust_future_dynamic_iog_slots - a dynamic (out-of-window) daytime dispatch
    # slot is still Octopus's own provisional/revisable plan and can be moved or rescinded before it
    # occurs. Trust is graduated by source (confirmed via completed_dispatches/car_charging_now), not
    # by clock time - a slot merely reaching its scheduled start time is not evidence anything
    # actually happened, only that it was due to.

    print("\n**** Test 29: 'none' - an unconfirmed dynamic slot is never trusted ****")
    my_predbat.trust_future_dynamic_iog_slots = "none"
    slot_start_29 = midnight_utc_26 + timedelta(hours=14)  # 14:00 - well outside the fixed window
    slot_end_29 = slot_start_29 + timedelta(minutes=30)
    slots_29 = [{"start": slot_start_29.strftime(TIME_FORMAT), "end": slot_end_29.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    expected_rates_29 = {minute: 10.0 for minute in range(840, 870)}  # left at the normal rate
    failed |= run_rate_add_io_slots_test("test29_none_never_trusts_dynamic_slot", my_predbat, slots_29, True, 12, expected_rates_29, confirmed=False)

    print("\n**** Test 30: Fixed 23:30-05:30 window slot stays low rate at every trust level ****")
    my_predbat.trust_future_dynamic_iog_slots = "none"
    slot_start_30 = midnight_utc_26 + timedelta(hours=2)  # 02:00 - well inside 23:30-05:30
    slot_end_30 = slot_start_30 + timedelta(minutes=30)
    slots_30 = [{"start": slot_start_30.strftime(TIME_FORMAT), "end": slot_end_30.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    expected_rates_30 = {minute: 4.0 for minute in range(120, 150)}
    failed |= run_rate_add_io_slots_test("test30_fixed_window_always_trusted", my_predbat, slots_30, True, 12, expected_rates_30, confirmed=False)

    print("\n**** Test 31: 'started' - a slot Octopus has recorded as completed is trusted ****")
    # 'started' accepts Octopus's own metered record as well as the sensor: it is strictly stronger
    # evidence, even though it only ever arrives after the slot has ended (which is why there is no
    # completed-only level - it could never confirm a slot in time to plan around it).
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.car_charging_now_confirmed_slots = [set()]  # no sensor corroboration - the record alone
    expected_rates_31 = {minute: 4.0 for minute in range(840, 870)}
    failed |= run_rate_add_io_slots_test("test31_started_trusts_completed_record", my_predbat, slots_29, True, 12, expected_rates_31, confirmed=True)

    print("\n**** Test 32: 'started' - a still-unconfirmed slot is NOT trusted even once its start time has passed ****")
    # Directly disproves clock time alone as evidence: this slot's scheduled start (09:00) is well
    # before minutes_now (10:00), but it was never confirmed, so it must stay untrusted.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    slot_start_32 = midnight_utc_26 + timedelta(hours=9)  # 09:00 - before minutes_now (10:00), outside the fixed window
    slot_end_32 = slot_start_32 + timedelta(minutes=30)
    slots_32 = [{"start": slot_start_32.strftime(TIME_FORMAT), "end": slot_end_32.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "unknown", "location": "AT_HOME"}]
    expected_rates_32 = {minute: 10.0 for minute in range(540, 570)}
    failed |= run_rate_add_io_slots_test("test32_started_does_not_trust_by_clock_time_alone", my_predbat, slots_32, True, 12, expected_rates_32, confirmed=False)

    # Tests 33+ ('started'): a dynamic slot's current settlement period is trusted the moment
    # car_charging_now has been seen True at some point during THAT slot, with the per-slot
    # confirmation mechanism (get_car_charging_planned() in fetch.py) rather than a bare live read -
    # see const.py's CAR_CHARGING_NOW_CONFIRM_GUARD_MINUTES for the rollover-boundary reasoning these
    # tests exercise directly.

    print("\n**** Test 33: 'started' - a slot confirmed by car_charging_now_confirmed_slots is trusted ****")
    my_predbat.trust_future_dynamic_iog_slots = "started"
    slot_start_33 = midnight_utc_26 + timedelta(hours=10)  # 10:00 - the current settlement period (minutes_now=600)
    slot_end_33 = slot_start_33 + timedelta(minutes=30)
    slots_33 = [{"start": slot_start_33.strftime(TIME_FORMAT), "end": slot_end_33.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "unknown", "location": "AT_HOME"}]
    my_predbat.car_charging_now_confirmed_slots = [{600}]  # slot 10:00-10:30 confirmed
    expected_rates_33 = {minute: 4.0 for minute in range(600, 630)}
    failed |= run_rate_add_io_slots_test("test33_started_trusts_confirmed_slot", my_predbat, slots_33, True, 12, expected_rates_33, confirmed=False)

    print("\n**** Test 34: 'started' - confirmation of one slot does not carry over to a different slot ****")
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.car_charging_now_confirmed_slots = [{600}]  # only 10:00-10:30 confirmed
    expected_rates_34 = {minute: 10.0 for minute in range(540, 570)}  # slots_32 is 09:00, a different slot
    failed |= run_rate_add_io_slots_test("test34_started_confirmation_does_not_carry_to_other_slot", my_predbat, slots_32, True, 12, expected_rates_34, confirmed=False)

    print("\n**** Test 35: 'started' with no confirmed slots and no completed record trusts nothing ****")
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.car_charging_now_confirmed_slots = [set()]
    expected_rates_35 = {minute: 10.0 for minute in range(600, 630)}  # slots_33, but no confirmation this time
    failed |= run_rate_add_io_slots_test("test35_started_without_confirmation_trusts_nothing", my_predbat, slots_33, True, 12, expected_rates_35, confirmed=False)

    print("\n**** Test 36: 'started' falls back to 'none' for a car with no car_charging_now sensor configured ****")
    # #4917/Trefor review of #4885: 'started' leans on a live car-reported signal, which risks a
    # false positive (e.g. a kettle mistaken for the car) if there is no genuine charger/car-reported
    # sensor behind it - car_charging_now is exactly that genuine sensor. Without one configured at
    # all, 'started' must not silently do anything clever with some other signal - it falls back to
    # 'none', even though car_charging_now_confirmed_slots says this slot was
    # seen charging (a real install with no sensor configured could never have populated that set in
    # the first place - this simulates the gate independently of that).
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.car_charging_now_confirmed_slots = [{600}]  # would trust it if the sensor were configured
    del my_predbat.args["car_charging_now"]  # no sensor configured at all
    expected_rates_36 = {minute: 10.0 for minute in range(600, 630)}  # falls back to 'none', the slot stays untrusted
    failed |= run_rate_add_io_slots_test("test36_started_falls_back_without_sensor", my_predbat, slots_33, True, 12, expected_rates_36, confirmed=False)
    my_predbat.args["car_charging_now"] = "binary_sensor.fake_charging_now"  # restore for subsequent tests

    print("\n**** Test 37: 'started' falls back per-car in a multi-car install (car 1 has no sensor, car 0 does) ****")
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.args["car_charging_now"] = ["binary_sensor.car0_charging_now"]  # only car 0 has a sensor configured
    my_predbat.car_charging_now_confirmed_slots = [{600}, {600}]  # both cars' slots show confirmed charging
    my_predbat.trusted_dynamic_minutes = set()
    rates_37 = {}
    for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60)):
        rates_37[minute] = 10.0
    tagged_slots_37 = [dict(slot, _confirmed=False) for slot in slots_33]
    result_rates_car0 = my_predbat.rate_add_io_slots(0, dict(rates_37), tagged_slots_37)
    result_rates_car1 = my_predbat.rate_add_io_slots(1, dict(rates_37), tagged_slots_37)
    for minute in range(600, 630):
        if result_rates_car0.get(minute) != 4.0:
            print("ERROR: car 0 (has sensor) minute {} should be trusted (4.0), got {}".format(minute, result_rates_car0.get(minute)))
            failed = True
        if result_rates_car1.get(minute) != 10.0:
            print("ERROR: car 1 (no sensor) minute {} should fall back untrusted (10.0), got {}".format(minute, result_rates_car1.get(minute)))
            failed = True
    my_predbat.args["car_charging_now"] = "binary_sensor.fake_charging_now"  # restore for subsequent tests

    print("\n**** Test 38: 'planned' - trusts a dynamic slot unconditionally, even unconfirmed (old, pre-#4516 behaviour restored as an explicit opt-in) ****")
    my_predbat.trust_future_dynamic_iog_slots = "planned"
    expected_rates_38 = {minute: 4.0 for minute in range(840, 870)}
    failed |= run_rate_add_io_slots_test("test38_planned_trusts_unconfirmed_slot", my_predbat, slots_29, True, 12, expected_rates_38, confirmed=False)

    print("\n**** Test 39: a cap-rejected slot keeps the tariff's own feed-side discount ****")
    # octopus_slot_max is a budget on how much cheap-rate charging Predbat PLANS, not a claim about
    # what Octopus will bill - it is documented as per-car (docs/car-charging.md) while the rates
    # dict is install-wide, so it cannot govern install-wide beliefs about price. So the cap withholds
    # Predbat's OWN overlay for a slot beyond budget, but must not reach across and strip a discount
    # the tariff feed itself delivered (io_adjusted): that marker is a statement of billing fact.
    #
    # rate_add_io_slots()'s reject branch already declines to rewrite a cap-only rejection (#4483).
    # trusted_dynamic_minutes therefore has to record "would this have been trusted", not "did it get
    # the overlay", or exclude_dynamic_io_slots() strips the very minutes that branch just protected -
    # the two functions encoding opposite intents for one slot (#5110 review). Stripping it would be
    # strictly worse than either alternative: the minute loses the cheap rate AND the io_adjusted
    # marker that plan.py's IOG skew and prediction.py's PV10 worst-case use to hedge it.
    #
    # The previous version of this test used a uniform rates dict with no io_adjusted entries at all,
    # so it passed identically under either behaviour and only asserted the internal bookkeeping.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.car_charging_now_confirmed_slots = [{840, 870}]  # both blocks corroborated, so both are trusted
    slots_39 = []
    for i in range(2):
        slot_start_39 = midnight_utc_26 + timedelta(hours=14, minutes=i * 30)  # 14:00 and 14:30, both dynamic
        slot_end_39 = slot_start_39 + timedelta(minutes=30)
        slots_39.append({"start": slot_start_39.strftime(TIME_FORMAT), "end": slot_end_39.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False})

    feed_discount_39 = 3.5  # what the tariff feed itself already charged for both blocks
    rates_39 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    saved_io_adjusted_39 = dict(my_predbat.io_adjusted)
    for minute in range(840, 900):
        rates_39[minute] = feed_discount_39
        my_predbat.io_adjusted[minute] = True

    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 1  # only the first block fits the budget
    my_predbat.trusted_dynamic_minutes = set()
    saved_slots_39 = my_predbat.octopus_slots
    my_predbat.octopus_slots = [slots_39]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    rates_39 = my_predbat.rate_add_io_slots(0, rates_39, slots_39)
    my_predbat.octopus_slots = saved_slots_39
    # The install-wide pass fetch.py runs after every car - this is where the strip would happen.
    rates_39 = my_predbat.exclude_dynamic_io_slots(rates_39)

    for minute in range(840, 870):
        if rates_39.get(minute) != 4.0:
            print("ERROR: minute {} is within budget and should get Predbat's overlay rate 4.0, got {}".format(minute, rates_39.get(minute)))
            failed = True
    for minute in range(870, 900):
        if rates_39.get(minute) != feed_discount_39:
            print("ERROR: minute {} was rejected only by the cap and must keep the feed's own {} rate, got {} - the cap stripped a discount Octopus will actually bill".format(minute, feed_discount_39, rates_39.get(minute)))
            failed = True
        if not my_predbat.io_adjusted.get(minute):
            print("ERROR: minute {} lost its io_adjusted marker, so plan.py's IOG skew and prediction.py's PV10 hedge can no longer see it".format(minute))
            failed = True

    my_predbat.io_adjusted = saved_io_adjusted_39

    print("\n**** Test 40: an elapsed unconfirmed dynamic slot keeps the rate it actually charged ****")
    # `needed` exempts past slots (slot_start <= current_block) but `trusted` deliberately doesn't
    # (test32), so combining the two rejection reasons could otherwise let an unconfirmed *past*
    # dispatch have the cheap rate it genuinely charged overwritten with rate_max_base - inflating
    # today_cost()'s reported spend (output.py multiplies elapsed minutes by self.rate_import)
    # without changing the plan. Same reasoning as exclude_dynamic_io_slots() skipping past minutes.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.car_charging_now_confirmed_slots = [set()]  # no sensor corroboration for the elapsed slot
    slot_start_40 = midnight_utc_26 + timedelta(hours=9)  # 09:00-09:30, fully elapsed at minutes_now=10:00
    slot_end_40 = slot_start_40 + timedelta(minutes=30)
    slots_40 = [{"start": slot_start_40.strftime(TIME_FORMAT), "end": slot_end_40.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "unknown", "location": "AT_HOME", "_confirmed": False}]

    rates_40 = {}
    for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60)):
        rates_40[minute] = 10.0
    saved_io_adjusted_40 = dict(my_predbat.io_adjusted)
    for minute in range(540, 570):
        rates_40[minute] = 3.99  # the discounted rate the tariff genuinely charged for that half hour
        my_predbat.io_adjusted[minute] = True

    my_predbat.trusted_dynamic_minutes = set()
    result_rates_40 = my_predbat.rate_add_io_slots(0, rates_40, slots_40)

    for minute in range(540, 570):
        if result_rates_40.get(minute) != 3.99:
            print("ERROR: elapsed minute {} should keep the 3.99 actually charged, got {}".format(minute, result_rates_40.get(minute)))
            failed = True
        if minute not in my_predbat.io_adjusted:
            print("ERROR: elapsed minute {} should still be marked io_adjusted, was cleared".format(minute))
            failed = True

    my_predbat.io_adjusted = saved_io_adjusted_40

    print("\n**** Test 41: plan_interval_minutes=15 - a slot rounded off the 30-min boundary must still decide its own admission ****")
    # With the default 30-minute interval, every slot's own [start, end) range always contains its
    # slot_start minute, so the needed/trusted/cap decision always fires from within the slot itself.
    # At a 15-minute interval a slot can round to a sub-range that skips that minute entirely - a
    # 14:15-14:30 slot never contains minute 840 or 870 - which must not leave its admission
    # decision undecided (previously it silently rode on whatever slots_added_set state some other,
    # unrelated slot happened to have left behind for the same slot_start).
    my_predbat.trust_future_dynamic_iog_slots = "planned"
    my_predbat.plan_interval_minutes = 15
    slot_start_41 = midnight_utc_26 + timedelta(hours=14, minutes=15)  # 14:15-14:30, no 30-min boundary minute
    slot_end_41 = slot_start_41 + timedelta(minutes=15)
    slots_41 = [{"start": slot_start_41.strftime(TIME_FORMAT), "end": slot_end_41.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME"}]
    expected_rates_41 = {minute: 4.0 for minute in range(855, 870)}
    failed |= run_rate_add_io_slots_test("test41_sub_30min_interval_slot_decides_its_own_admission", my_predbat, slots_41, True, 12, expected_rates_41, confirmed=False)
    my_predbat.plan_interval_minutes = 30

    print("\n**** Test 42: exclude_dynamic_io_slots strips a feed-side discount rate_add_io_slots() never trusted ****")
    # rate_add_io_slots() only overlays a discount for minutes it explicitly adds to trusted_dynamic_
    # minutes. A real Intelligent tariff can hand back an already-discounted rate straight from the
    # feed for a future out-of-window minute (flagged via io_adjusted) that rate_add_io_slots() never
    # even visited this cycle - exclude_dynamic_io_slots() is what has to catch and undo that one.
    # "planned" is a deliberate no-op for this function (old pre-#4516 behaviour), so use "none" -
    # the strictest level, and the only one that actually walks io_adjusted below.
    my_predbat.trust_future_dynamic_iog_slots = "none"
    saved_io_adjusted_42 = dict(my_predbat.io_adjusted)
    my_predbat.trusted_dynamic_minutes = set()  # nothing trusted this cycle
    rates_42 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    for minute in range(840, 870):  # 14:00-14:30, well outside the 23:30-05:30 fixed window
        rates_42[minute] = 4.0
        my_predbat.io_adjusted[minute] = True
    result_rates_42 = my_predbat.exclude_dynamic_io_slots(rates_42)
    for minute in range(840, 870):
        if result_rates_42.get(minute) != my_predbat.rate_max_base:
            print("ERROR: untrusted feed-discounted minute {} should be restored to rate_max_base ({}), got {}".format(minute, my_predbat.rate_max_base, result_rates_42.get(minute)))
            failed = True
        if minute in my_predbat.io_adjusted:
            print("ERROR: untrusted feed-discounted minute {} should have its io_adjusted marker cleared".format(minute))
            failed = True
    my_predbat.io_adjusted = saved_io_adjusted_42

    print("\n**** Test 43: exclude_dynamic_io_slots preserves a feed-side discount rate_add_io_slots() did trust ****")
    my_predbat.trust_future_dynamic_iog_slots = "none"
    saved_io_adjusted_43 = dict(my_predbat.io_adjusted)
    rates_43 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    for minute in range(840, 870):
        rates_43[minute] = 4.0
        my_predbat.io_adjusted[minute] = True
    my_predbat.trusted_dynamic_minutes = set(range(840, 870))  # this cycle already trusted these minutes
    result_rates_43 = my_predbat.exclude_dynamic_io_slots(rates_43)
    for minute in range(840, 870):
        if result_rates_43.get(minute) != 4.0:
            print("ERROR: trusted feed-discounted minute {} should keep its 4.0 rate, got {}".format(minute, result_rates_43.get(minute)))
            failed = True
        if minute not in my_predbat.io_adjusted:
            print("ERROR: trusted feed-discounted minute {} should keep its io_adjusted marker".format(minute))
            failed = True
    my_predbat.io_adjusted = saved_io_adjusted_43
    my_predbat.trusted_dynamic_minutes = set()

    print("\n**** Test 44: exclude_dynamic_io_slots leaves a fixed-window minute alone even when untrusted ****")
    # 02:00 is inside the guaranteed-cheap 23:30-05:30 window - that discount comes from the tariff
    # itself, not a reclaimable dispatch, so it must survive regardless of trust_future_dynamic_iog_slots.
    my_predbat.trust_future_dynamic_iog_slots = "none"
    saved_io_adjusted_44 = dict(my_predbat.io_adjusted)
    rates_44 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    fixed_window_minute_44 = 26 * 60  # 02:00 the following day relative to midnight_utc_26, well ahead of minutes_now=600
    for minute in range(fixed_window_minute_44, fixed_window_minute_44 + 30):
        rates_44[minute] = 4.0
        my_predbat.io_adjusted[minute] = True
    my_predbat.trusted_dynamic_minutes = set()
    result_rates_44 = my_predbat.exclude_dynamic_io_slots(rates_44)
    for minute in range(fixed_window_minute_44, fixed_window_minute_44 + 30):
        if result_rates_44.get(minute) != 4.0:
            print("ERROR: fixed-window minute {} should keep its 4.0 rate regardless of trust, got {}".format(minute, result_rates_44.get(minute)))
            failed = True
        if minute not in my_predbat.io_adjusted:
            print("ERROR: fixed-window minute {} should keep its io_adjusted marker".format(minute))
            failed = True
    my_predbat.io_adjusted = saved_io_adjusted_44

    print("\n**** Test 45: a later car's rejected slot must not undo an earlier car's accepted slot at the same minute ****")
    # rate_add_io_slots() is called once per car (fetch.py), each call mutating the same shared rates
    # dict in sequence - unlike the old pre-#4516 function (purely additive, never restored a minute
    # once discounted), the reject-and-restore path this PR added (rates[minute] = rate_max_base) can
    # actively undo a discount a previous call already applied. Two IOG-enabled cars can genuinely have
    # overlapping dispatch windows, so car 1's own rejected/untrusted slot at the same minute car 0's
    # slot was accepted for must not clobber car 0's genuine discount (Copilot-style review on #5110).
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    my_predbat.car_charging_now_confirmed_slots = [{840}, set()]  # car 0 corroborated, car 1 not - keyed on slot START
    slot_start_45 = midnight_utc_26 + timedelta(hours=14)  # 14:00-14:30, both cars dispatched here
    slot_end_45 = slot_start_45 + timedelta(minutes=30)
    slots_car0_45 = [{"start": slot_start_45.strftime(TIME_FORMAT), "end": slot_end_45.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}]
    slots_car1_45 = [{"start": slot_start_45.strftime(TIME_FORMAT), "end": slot_end_45.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}]

    rates_45 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    my_predbat.trusted_dynamic_minutes = set()
    saved_slots_45, saved_cars_45 = my_predbat.octopus_slots, my_predbat.num_cars
    my_predbat.num_cars = 2
    my_predbat.octopus_slots = [slots_car0_45, slots_car1_45]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    rates_45 = my_predbat.rate_add_io_slots(0, rates_45, slots_car0_45)  # car 0: corroborated, accepted
    rates_45 = my_predbat.rate_add_io_slots(1, rates_45, slots_car1_45)  # car 1: not corroborated, rejected
    my_predbat.octopus_slots, my_predbat.num_cars = saved_slots_45, saved_cars_45

    for minute in range(840, 870):
        if rates_45.get(minute) != 4.0:
            print("ERROR: minute {} should keep car 0's accepted 4.0 rate after car 1's rejected pass, got {}".format(minute, rates_45.get(minute)))
            failed = True

    print("\n**** Test 46: a later corroborated slot must override an earlier rejected one for the same block ****")
    # slots_decided makes the FIRST entry visited for a 30-min block decide it permanently, and
    # saved_slots then skips every later entry's minutes outright. So when two entries cover one
    # block and disagree, the earlier verdict wins regardless of which is better evidence. Under
    # "started" the only per-ENTRY input to `trusted` is `confirmed` (a completed dispatch record),
    # since car_charging_now_confirmed_slot_starts is keyed on slot_start and therefore shared - so
    # an unconfirmed planned entry visited first rejects the block and the confirmed duplicate that
    # follows can never admit it (Copilot review on #5110, high). Same car, two entries, one block.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    # No sensor corroboration for this block, so `trusted` differs per ENTRY rather than per block:
    # only `confirmed` (slot["_confirmed"], set from a completed dispatch record) can make the two
    # disagree, since car_charging_now_confirmed_slot_starts is keyed on slot_start and so is shared.
    my_predbat.car_charging_now_confirmed_slots = [set()]
    slot_start_46 = midnight_utc_26 + timedelta(hours=14)  # 14:00-14:30
    slot_end_46 = slot_start_46 + timedelta(minutes=30)
    planned_46 = {"start": slot_start_46.strftime(TIME_FORMAT), "end": slot_end_46.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}
    live_46 = {"start": slot_start_46.strftime(TIME_FORMAT), "end": slot_end_46.strftime(TIME_FORMAT), "charge_in_kwh": 3.7, "source": "smart-charge", "location": "AT_HOME", "_confirmed": True}

    rates_46 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    my_predbat.trusted_dynamic_minutes = set()
    # Deliberately in the order fetch produces: planned first, live appended after.
    saved_slots_46 = my_predbat.octopus_slots
    my_predbat.octopus_slots = [[planned_46, live_46]]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    rates_46 = my_predbat.rate_add_io_slots(0, rates_46, [planned_46, live_46])
    my_predbat.octopus_slots = saved_slots_46

    for minute in range(840, 870):
        if rates_46.get(minute) != 4.0:
            print("ERROR: minute {} should be admitted at 4.0 by the confirmed entry, got {} - an earlier rejection blocked it".format(minute, rates_46.get(minute)))
            failed = True

    print("\n**** Test 47: an overridden block must still consume only one slot of the daily cap ****")
    # The override above must not let one block count twice against octopus_slot_max, or a pair of
    # duplicated entries would halve the day's budget.
    my_predbat.trust_future_dynamic_iog_slots = "planned"  # both entries trusted, so both would admit
    my_predbat.args["octopus_slot_max"] = 2
    base_47 = midnight_utc_26 + timedelta(hours=14)
    slots_47 = []
    for index in range(3):  # three distinct blocks, each duplicated
        start_47 = base_47 + timedelta(minutes=30 * index)
        end_47 = start_47 + timedelta(minutes=30)
        entry_47 = {"start": start_47.strftime(TIME_FORMAT), "end": end_47.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}
        slots_47.append(entry_47)
        slots_47.append(dict(entry_47))  # duplicate of the same block

    rates_47 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    my_predbat.trusted_dynamic_minutes = set()
    saved_slots_47 = my_predbat.octopus_slots
    my_predbat.octopus_slots = [slots_47]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    rates_47 = my_predbat.rate_add_io_slots(0, rates_47, slots_47)
    my_predbat.octopus_slots = saved_slots_47

    discounted_blocks_47 = {(minute // 30) * 30 for minute, rate in rates_47.items() if rate == 4.0 and 840 <= minute < 930}
    if len(discounted_blocks_47) != 2:
        print("ERROR: octopus_slot_max=2 should admit exactly 2 of the 3 duplicated blocks, got {}".format(sorted(discounted_blocks_47)))
        failed = True

    print("\n**** Test 48: a rejected slot must not become the assumed tariff price for a later accepted one ****")
    # With octopus_slot_low_rate off, assumed_price is the TARIFF price for the slot. Reading it from
    # the `rates` working dict was safe on main (nothing ever wrote rate_max_base into rates), but this
    # PR's reject path does - so car 0's rejection of a block would be read back by car 1 as though it
    # were the tariff, pricing car 1's genuinely confirmed dispatch at the day rate, and leaving
    # trusted_dynamic_minutes set so exclude_dynamic_io_slots() cannot repair it either. Swapping the
    # cars gave the correct answer, which is the tell (#5110 review). Read from rate_import_no_io - the
    # snapshot fetch takes before the per-car loop - instead.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.args["octopus_slot_low_rate"] = False  # assumed_price comes from the tariff, not rate_min_base
    my_predbat.args["octopus_slot_max"] = 12
    my_predbat.car_charging_now_confirmed_slots = [set(), set()]
    slot_start_48 = midnight_utc_26 + timedelta(hours=14)  # 14:00-14:30
    slot_end_48 = slot_start_48 + timedelta(minutes=30)
    # Car 0: planned only, unconfirmed -> rejected, writes rate_max_base over the block.
    slots_car0_48 = [{"start": slot_start_48.strftime(TIME_FORMAT), "end": slot_end_48.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}]
    # Car 1: a completed dispatch record for the same block -> trusted, must get the TARIFF price.
    slots_car1_48 = [{"start": slot_start_48.strftime(TIME_FORMAT), "end": slot_end_48.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": True}]

    tariff_price_48 = 9.0
    rates_48 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    for minute in range(840, 870):
        rates_48[minute] = tariff_price_48
    # The snapshot fetch takes before the per-car loop - what the tariff actually says.
    saved_no_io_48 = my_predbat.rate_import_no_io
    my_predbat.rate_import_no_io = dict(rates_48)
    my_predbat.trusted_dynamic_minutes = set()
    saved_slots_48, saved_cars_48 = my_predbat.octopus_slots, my_predbat.num_cars
    my_predbat.num_cars = 2
    my_predbat.octopus_slots = [slots_car0_48, slots_car1_48]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    rates_48 = my_predbat.rate_add_io_slots(0, rates_48, slots_car0_48)  # rejects, clobbers the block
    rates_48 = my_predbat.rate_add_io_slots(1, rates_48, slots_car1_48)  # confirmed, must be tariff priced
    my_predbat.octopus_slots, my_predbat.num_cars = saved_slots_48, saved_cars_48
    my_predbat.rate_import_no_io = saved_no_io_48

    for minute in range(840, 870):
        if rates_48.get(minute) != tariff_price_48:
            print("ERROR: minute {} should be the tariff price {} for car 1's confirmed dispatch, got {} - an earlier rejection was read back as the tariff".format(minute, tariff_price_48, rates_48.get(minute)))
            failed = True
    my_predbat.args["octopus_slot_low_rate"] = True

    print("\n**** Test 49: car order must not change the outcome, including the io_adjusted marker ****")
    # The reject path drops self.io_adjusted[minute] as well as rewriting the rate, and NOTHING ever
    # restores that marker - so a rejecting car running before the accepting one used to leave the
    # block without it even though the rate was later restored. The block then vanishes from
    # plan.py's IOG charge skew and prediction.py's PV10 worst case. Resolving the cross-car shield
    # before the loop makes both orders agree (#5110 review). Run the same pair both ways round.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    slot_start_49 = midnight_utc_26 + timedelta(hours=14)  # 14:00-14:30
    slot_end_49 = slot_start_49 + timedelta(minutes=30)
    entry_49 = {"start": slot_start_49.strftime(TIME_FORMAT), "end": slot_end_49.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}
    slots_accept_49 = [dict(entry_49, _confirmed=True)]  # a completed dispatch record - genuinely trusted
    slots_reject_49 = [dict(entry_49)]  # unconfirmed, uncorroborated - rejected

    saved_slots_49, saved_cars_49 = my_predbat.octopus_slots, my_predbat.num_cars
    saved_io_adjusted_49 = dict(my_predbat.io_adjusted)
    outcomes_49 = {}
    for label, car0, car1 in (("accept_first", slots_accept_49, slots_reject_49), ("reject_first", slots_reject_49, slots_accept_49)):
        rates_49 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
        my_predbat.io_adjusted = {}
        for minute in range(840, 870):
            rates_49[minute] = 3.5
            my_predbat.io_adjusted[minute] = True
        my_predbat.car_charging_now_confirmed_slots = [set(), set()]
        my_predbat.num_cars = 2
        my_predbat.octopus_slots = [car0, car1]
        my_predbat.trusted_dynamic_minutes = set()
        my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
        rates_49 = my_predbat.rate_add_io_slots(0, rates_49, car0)
        rates_49 = my_predbat.rate_add_io_slots(1, rates_49, car1)
        outcomes_49[label] = (rates_49.get(855), bool(my_predbat.io_adjusted.get(855)))
    my_predbat.octopus_slots, my_predbat.num_cars = saved_slots_49, saved_cars_49
    my_predbat.io_adjusted = saved_io_adjusted_49

    if outcomes_49["accept_first"] != outcomes_49["reject_first"]:
        print("ERROR: car order changed the outcome - accept_first gave (rate, io_adjusted)={} but reject_first gave {}".format(outcomes_49["accept_first"], outcomes_49["reject_first"]))
        failed = True
    if not outcomes_49["reject_first"][1]:
        print("ERROR: the io_adjusted marker was dropped when the rejecting car ran first - the block falls out of plan.py's IOG skew and prediction.py's PV10 hedge")
        failed = True

    print("\n**** Test 50: the cross-car shield must not be broader than what the loop itself accepts ****")
    # resolve_protected_dispatch_minutes() shields a minute from another car's rejection. If it
    # protects a block the loop would itself have rejected, a rescindable slot keeps a cheap rate -
    # the #4516 failure this PR exists to prevent - and exclude_dynamic_io_slots() is no backstop at
    # "planned" (it returns early). Two ways the shield was broader, both fixed by deriving it from
    # the same car_trusted_dispatch_blocks() the loop's gates use (#5110 review):
    #   a) at "planned" it dropped the `needed` gate, shielding a block the car no longer wants
    #   b) at "started" it skipped the per-car degrade to "none" for a car with no real sensor
    saved_limit_50 = my_predbat.octopus_intelligent_limit_future_slots
    saved_slots_50, saved_cars_50 = my_predbat.octopus_slots, my_predbat.num_cars
    saved_car_slots_50 = my_predbat.car_charging_slots[0]
    saved_args_ccn_50 = my_predbat.args.get("car_charging_now", None)
    saved_io_50 = dict(my_predbat.io_adjusted)

    slot_start_50 = midnight_utc_26 + timedelta(hours=14)  # 14:00-14:30, future at minutes_now=10:00
    slot_end_50 = slot_start_50 + timedelta(minutes=30)
    entry_50 = {"start": slot_start_50.strftime(TIME_FORMAT), "end": slot_end_50.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}

    # (a) "planned" + limit_future_slots on, and the car does NOT expect this block any more.
    my_predbat.trust_future_dynamic_iog_slots = "planned"
    my_predbat.octopus_intelligent_limit_future_slots = True
    my_predbat.car_charging_slots[0] = []  # no expected blocks - the car no longer needs it
    my_predbat.num_cars = 1
    my_predbat.octopus_slots = [[dict(entry_50)]]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    if any(minute in my_predbat.protected_dispatch_minutes for minute in range(840, 870)):
        print("ERROR: a block the car no longer needs is in the cross-car shield at 'planned' - it would keep a cheap rate the loop itself rejects")
        failed = True

    # (b) "started" with car_charging_now a literal rather than a real sensor: the loop degrades this
    # car to "none", so nothing of its should be shielded even when the slot set says corroborated.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.octopus_intelligent_limit_future_slots = False
    my_predbat.args["car_charging_now"] = "on"  # a literal, not an entity id
    my_predbat.car_charging_now_confirmed_slots = [{840}]
    my_predbat.octopus_slots = [[dict(entry_50)]]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    if any(minute in my_predbat.protected_dispatch_minutes for minute in range(840, 870)):
        print("ERROR: a car whose 'started' trust degrades to 'none' (no real car_charging_now sensor) still had its block shielded")
        failed = True

    # And the shield must still contain what the loop DOES accept, or a genuine dispatch is clobbered.
    my_predbat.args.pop("car_charging_now", None)
    my_predbat.args["car_charging_now"] = "binary_sensor.fake_charging_now"
    my_predbat.octopus_slots = [[dict(entry_50, _confirmed=True)]]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    if not all(minute in my_predbat.protected_dispatch_minutes for minute in range(840, 870)):
        print("ERROR: a confirmed dispatch the loop would accept is missing from the shield - another car's rejection could clobber it")
        failed = True

    my_predbat.octopus_intelligent_limit_future_slots = saved_limit_50
    my_predbat.octopus_slots, my_predbat.num_cars = saved_slots_50, saved_cars_50
    my_predbat.car_charging_slots[0] = saved_car_slots_50
    my_predbat.io_adjusted = saved_io_50
    if saved_args_ccn_50 is not None:
        my_predbat.args["car_charging_now"] = saved_args_ccn_50
    else:
        my_predbat.args.pop("car_charging_now", None)

    print("\n**** Test 51: the reject path is observable - a rejected block loses a distinct feed discount ****")
    # Most tests here run against reset_rates(my_predbat, 10, 5), which makes rate_max_base 10 while
    # the harness fills the baseline with 10.0 - so the reject branch writes the value already there
    # and those tests pass whether it fires or not (#5110 review). This one seeds a discount
    # distinguishable from rate_max_base, and asserts both halves of the restore: the rate goes back
    # to the day rate AND the io_adjusted marker is cleared.
    my_predbat.trust_future_dynamic_iog_slots = "started"
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    my_predbat.car_charging_now_confirmed_slots = [set()]  # no corroboration - the slot is rejected
    slot_start_51 = midnight_utc_26 + timedelta(hours=14)
    slot_end_51 = slot_start_51 + timedelta(minutes=30)
    slots_51 = [{"start": slot_start_51.strftime(TIME_FORMAT), "end": slot_end_51.strftime(TIME_FORMAT), "charge_in_kwh": 2.5, "source": "smart-charge", "location": "AT_HOME", "_confirmed": False}]

    saved_io_51 = dict(my_predbat.io_adjusted)
    saved_slots_51 = my_predbat.octopus_slots
    rates_51 = {minute: 10.0 for minute in range(-96 * 60, max(my_predbat.forecast_minutes, 3 * 24 * 60))}
    my_predbat.io_adjusted = {}
    for minute in range(840, 870):
        rates_51[minute] = 3.5  # a feed-side discount, distinct from rate_max_base
        my_predbat.io_adjusted[minute] = True

    my_predbat.trusted_dynamic_minutes = set()
    my_predbat.octopus_slots = [slots_51]
    my_predbat.protected_dispatch_minutes = my_predbat.resolve_protected_dispatch_minutes()
    rates_51 = my_predbat.rate_add_io_slots(0, rates_51, slots_51)
    my_predbat.octopus_slots = saved_slots_51

    for minute in range(840, 870):
        if rates_51.get(minute) != my_predbat.rate_max_base:
            print("ERROR: minute {} was rejected and must be restored to rate_max_base {}, got {}".format(minute, my_predbat.rate_max_base, rates_51.get(minute)))
            failed = True
        if my_predbat.io_adjusted.get(minute):
            print("ERROR: minute {} was rejected but kept its io_adjusted marker, so it still reads as discounted downstream".format(minute))
            failed = True
    my_predbat.io_adjusted = saved_io_51

    # Restore original state
    my_predbat.trust_future_dynamic_iog_slots = saved_trust_dynamic
    my_predbat.octopus_intelligent_limit_future_slots = saved_limit_future_slots
    my_predbat.trusted_dynamic_minutes = saved_trusted_dynamic_minutes
    my_predbat.protected_dispatch_minutes = saved_protected_dispatch_minutes
    my_predbat.car_charging_now = saved_car_charging_now
    if saved_confirmed_slots is not None:
        my_predbat.car_charging_now_confirmed_slots = saved_confirmed_slots
    elif hasattr(my_predbat, "car_charging_now_confirmed_slots"):
        delattr(my_predbat, "car_charging_now_confirmed_slots")
    if saved_args_car_charging_now is not None:
        my_predbat.args["car_charging_now"] = saved_args_car_charging_now
    elif "car_charging_now" in my_predbat.args:
        del my_predbat.args["car_charging_now"]
    my_predbat.car_charging_slots[0] = saved_car_charging_slots

    # Restore original forecast_minutes
    my_predbat.forecast_minutes = original_forecast_minutes

    # Restore original time state
    my_predbat.now_utc = saved_now_utc
    my_predbat.midnight_utc = saved_midnight_utc
    my_predbat.minutes_now = saved_minutes_now

    if failed:
        print("\n**** rate_add_io_slots tests: FAILED ****")
    else:
        print("\n**** rate_add_io_slots tests: PASSED ****")

    return failed
