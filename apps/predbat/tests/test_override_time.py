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
from utils import get_override_time_from_string


def test_get_override_time_from_string(my_predbat):
    """
    Test the get_override_time_from_string function from utils
    """
    failed = False
    print("**** Testing get_override_time_from_string function ****")

    # Create test datetime objects with timezone awareness
    import pytz

    utc = pytz.UTC
    now = datetime(2024, 11, 26, 14, 23, 0, tzinfo=utc)  # Tuesday, 26 Nov 2024, 14:23

    # Test 1: Day with time format - future time today
    print("Test 1: Day with time format - future time same day")
    result = get_override_time_from_string(now, "Tue 15:30", 30)
    expected = datetime(2024, 11, 26, 15, 30, 0, tzinfo=utc)
    if result != expected:
        print("ERROR: Test 1 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 2: Day with time format - past day this week (should go to next week)
    print("Test 2: Day with time format - past day this week")
    result = get_override_time_from_string(now, "Mon 10:00", 30)
    expected = datetime(2024, 12, 2, 10, 0, 0, tzinfo=utc)  # Next Monday
    if result != expected:
        print("ERROR: Test 2 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 3: Day with time format - future day this week
    print("Test 3: Day with time format - future day this week")
    result = get_override_time_from_string(now, "Wed 09:15", 30)
    expected = datetime(2024, 11, 27, 9, 0, 0, tzinfo=utc)  # Next day (rounded down to 9:00)
    if result != expected:
        print("ERROR: Test 3 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 4: Time-only format - future time today
    print("Test 4: Time-only format - future time today")
    result = get_override_time_from_string(now, "16:45", 30)
    expected = datetime(2024, 11, 26, 16, 30, 0, tzinfo=utc)  # Today, rounded down to 16:30
    if result != expected:
        print("ERROR: Test 4 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 5: Time-only format - past time today (should go to tomorrow)
    print("Test 5: Time-only format - past time today (goes to tomorrow)")
    result = get_override_time_from_string(now, "10:00", 30)
    expected = datetime(2024, 11, 27, 10, 0, 0, tzinfo=utc)  # Tomorrow
    if result != expected:
        print("ERROR: Test 5 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 6: Time-only format - requested time equals current time
    # When user selects a time that equals current time (after rounding), it should select
    # today's slot since we're within the current active slot
    print("Test 6: Time-only format - requested time equals current time, within current slot")
    result = get_override_time_from_string(now, "14:23", 30)
    expected = datetime(2024, 11, 26, 14, 0, 0, tzinfo=utc)  # Today, rounded down to 14:00 (current slot)
    if result != expected:
        print("ERROR: Test 6 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 7: Rounding to plan_interval_minutes (15 min intervals)
    print("Test 7: Rounding with 15-minute intervals")
    result = get_override_time_from_string(now, "Fri 13:47", 15)
    expected = datetime(2024, 11, 29, 13, 45, 0, tzinfo=utc)  # Friday, rounded to 13:45
    if result != expected:
        print("ERROR: Test 7 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 8: Rounding to plan_interval_minutes (5 min intervals)
    print("Test 8: Rounding with 5-minute intervals")
    result = get_override_time_from_string(now, "20:33", 5)
    expected = datetime(2024, 11, 26, 20, 30, 0, tzinfo=utc)  # Today (future), rounded to 20:30
    if result != expected:
        print("ERROR: Test 8 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 9: Invalid format - should return None
    print("Test 9: Invalid format")
    result = get_override_time_from_string(now, "invalid", 30)
    if result is not None:
        print("ERROR: Test 9 failed - expected None for invalid format, got {}".format(result))
        failed = True

    # Test 10: Sunday (week boundary test)
    print("Test 10: Sunday - day with time format")
    result = get_override_time_from_string(now, "Sun 12:00", 30)
    expected = datetime(2024, 12, 1, 12, 0, 0, tzinfo=utc)  # Next Sunday
    if result != expected:
        print("ERROR: Test 10 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 11: Edge case - time just after midnight
    print("Test 11: Time-only format - just after midnight")
    now_late = datetime(2024, 11, 26, 23, 50, 0, tzinfo=utc)
    result = get_override_time_from_string(now_late, "00:15", 30)
    expected = datetime(2024, 11, 27, 0, 0, 0, tzinfo=utc)  # Tomorrow at midnight
    if result != expected:
        print("ERROR: Test 11 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 12: Edge case - time-only exactly at midnight (past, so tomorrow)
    print("Test 12: Time-only format - midnight when it's currently late")
    result = get_override_time_from_string(now_late, "00:00", 30)
    expected = datetime(2024, 11, 27, 0, 0, 0, tzinfo=utc)  # Tomorrow at midnight
    if result != expected:
        print("ERROR: Test 12 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 13: Bug case - current time slot is active (within plan_interval_minutes of slot start)
    # Scenario: slot started at 23:30, current time is 23:35, should select TODAY's 23:30 slot
    print("Test 13: Time-only format - within current active time slot (23:30 slot, now 23:35)")
    now_in_slot = datetime(2024, 11, 26, 23, 35, 0, tzinfo=utc)
    result = get_override_time_from_string(now_in_slot, "23:30", 30)
    expected = datetime(2024, 11, 26, 23, 30, 0, tzinfo=utc)  # Today's 23:30 slot (NOT tomorrow)
    if result != expected:
        print("ERROR: Test 13 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 14: Similar case with 5-minute intervals
    # Scenario: slot started at 14:20, current time is 14:23, should select TODAY's 14:20 slot
    print("Test 14: Time-only format - within current active time slot (14:20 slot, now 14:23, 5-min intervals)")
    now_in_slot_5min = datetime(2024, 11, 26, 14, 23, 0, tzinfo=utc)
    result = get_override_time_from_string(now_in_slot_5min, "14:20", 5)
    expected = datetime(2024, 11, 26, 14, 20, 0, tzinfo=utc)  # Today's 14:20 slot (NOT tomorrow)
    if result != expected:
        print("ERROR: Test 14 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 15: Edge case - exactly at plan_interval_minutes boundary
    # Scenario: slot started at 14:00, current time is 14:30 (exactly at next slot start), should be tomorrow
    print("Test 15: Time-only format - exactly at next slot boundary (14:00 slot, now 14:30)")
    now_at_boundary = datetime(2024, 11, 26, 14, 30, 0, tzinfo=utc)
    result = get_override_time_from_string(now_at_boundary, "14:00", 30)
    expected = datetime(2024, 11, 27, 14, 0, 0, tzinfo=utc)  # Tomorrow's 14:00 slot
    if result != expected:
        print("ERROR: Test 15 failed - expected {} got {}".format(expected, result))
        failed = True

    print("**** get_override_time_from_string tests completed ****")
    return failed
