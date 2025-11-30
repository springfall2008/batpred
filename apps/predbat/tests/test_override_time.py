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

    # Test 6: Time-only format - exact current time (should go to tomorrow)
    print("Test 6: Time-only format - exact current time")
    result = get_override_time_from_string(now, "14:23", 30)
    expected = datetime(2024, 11, 27, 14, 0, 0, tzinfo=utc)  # Tomorrow, rounded down to 14:00
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

    print("**** get_override_time_from_string tests completed ****")
    return failed
