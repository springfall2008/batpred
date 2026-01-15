# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import window2minutes, compute_window_minutes
from datetime import datetime


def test_window2minutes(my_predbat):
    """
    Test the window2minutes and compute_window_minutes functions from utils
    """
    failed = False
    print("**** Testing window2minutes and compute_window_minutes functions ****")

    # Test 1: Normal window within same day (e.g., 10:00-14:00 when it's 08:00)
    print("Test 1: Normal window within same day - future window")
    minutes_now = 8 * 60  # 08:00
    start, end = window2minutes("10:00:00", "14:00:00", minutes_now)
    if start != 10 * 60 or end != 14 * 60:
        print(f"ERROR: Test 1 failed - expected (600, 840) got ({start}, {end})")
        failed = True

    # Test 2: Normal window that has already passed (e.g., 02:00-04:00 when it's 10:00)
    print("Test 2: Normal window that has already passed - should move to next day")
    minutes_now = 10 * 60  # 10:00
    start, end = window2minutes("02:00:00", "04:00:00", minutes_now)
    expected_start = 2 * 60 + 24 * 60  # 02:00 next day
    expected_end = 4 * 60 + 24 * 60  # 04:00 next day
    if start != expected_start or end != expected_end:
        print(f"ERROR: Test 2 failed - expected ({expected_start}, {expected_end}) got ({start}, {end})")
        failed = True

    # Test 3: Midnight-spanning window - before midnight, window spans midnight (23:00-02:00)
    print("Test 3: Midnight-spanning window - currently before start")
    minutes_now = 20 * 60  # 20:00
    start, end = window2minutes("23:00:00", "02:00:00", minutes_now)
    expected_start = 23 * 60  # 23:00 today
    expected_end = 2 * 60 + 24 * 60  # 02:00 tomorrow (26 hours from midnight)
    if start != expected_start or end != expected_end:
        print(f"ERROR: Test 3 failed - expected ({expected_start}, {expected_end}) got ({start}, {end})")
        failed = True

    # Test 4: Midnight-spanning window - after midnight but before end (23:00-02:00 when it's 01:00)
    print("Test 4: Midnight-spanning window - currently after midnight but before end")
    minutes_now = 1 * 60  # 01:00
    start, end = window2minutes("23:00:00", "02:00:00", minutes_now)
    expected_start = 23 * 60 - 24 * 60  # 23:00 yesterday (-60)
    expected_end = 2 * 60  # 02:00 today
    if start != expected_start or end != expected_end:
        print(f"ERROR: Test 4 failed - expected ({expected_start}, {expected_end}) got ({start}, {end})")
        failed = True

    # Test 5: Midnight-spanning window - end has passed (23:00-02:00 when it's 03:00)
    print("Test 5: Midnight-spanning window - end has passed")
    minutes_now = 3 * 60  # 03:00
    start, end = window2minutes("23:00:00", "02:00:00", minutes_now)
    expected_start = 23 * 60  # 23:00 today
    expected_end = 2 * 60 + 24 * 60  # 02:00 tomorrow
    if start != expected_start or end != expected_end:
        print(f"ERROR: Test 5 failed - expected ({expected_start}, {expected_end}) got ({start}, {end})")
        failed = True

    # Test 6: Window ending exactly at current time - should move to next day
    print("Test 6: Window ending exactly at current time")
    minutes_now = 14 * 60  # 14:00
    start, end = window2minutes("10:00:00", "14:00:00", minutes_now)
    expected_start = 10 * 60 + 24 * 60  # 10:00 tomorrow
    expected_end = 14 * 60 + 24 * 60  # 14:00 tomorrow
    if start != expected_start or end != expected_end:
        print(f"ERROR: Test 6 failed - expected ({expected_start}, {expected_end}) got ({start}, {end})")
        failed = True

    # Test 7: Window starting at midnight (00:00-04:00 when it's 22:00)
    print("Test 7: Window starting at midnight - future window")
    minutes_now = 22 * 60  # 22:00
    start, end = window2minutes("00:00:00", "04:00:00", minutes_now)
    expected_start = 0 + 24 * 60  # 00:00 tomorrow
    expected_end = 4 * 60 + 24 * 60  # 04:00 tomorrow
    if start != expected_start or end != expected_end:
        print(f"ERROR: Test 7 failed - expected ({expected_start}, {expected_end}) got ({start}, {end})")
        failed = True

    # Test 8: Short time strings (HH:MM format)
    print("Test 8: Short time strings (HH:MM format)")
    minutes_now = 8 * 60  # 08:00
    start, end = window2minutes("10:00", "14:00", minutes_now)
    if start != 10 * 60 or end != 14 * 60:
        print(f"ERROR: Test 8 failed - expected (600, 840) got ({start}, {end})")
        failed = True

    # Test 9: Window currently active (10:00-14:00 when it's 12:00)
    print("Test 9: Window currently active")
    minutes_now = 12 * 60  # 12:00
    start, end = window2minutes("10:00:00", "14:00:00", minutes_now)
    if start != 10 * 60 or end != 14 * 60:
        print(f"ERROR: Test 9 failed - expected (600, 840) got ({start}, {end})")
        failed = True

    # Test 10: compute_window_minutes with datetime objects
    print("Test 10: compute_window_minutes with datetime objects")
    minutes_now = 8 * 60
    start_time = datetime(2024, 1, 1, 10, 30, 0)
    end_time = datetime(2024, 1, 1, 14, 45, 0)
    start, end = compute_window_minutes(start_time, end_time, minutes_now)
    if start != 10 * 60 + 30 or end != 14 * 60 + 45:
        print(f"ERROR: Test 10 failed - expected (630, 885) got ({start}, {end})")
        failed = True

    # Test 11: window2minutes with None time strings - should return (0, 0)
    print("Test 11: window2minutes with None time strings")
    minutes_now = 8 * 60
    start, end = window2minutes(None, "14:00:00", minutes_now)
    if start != 0 or end != 0:
        print(f"ERROR: Test 11a failed - expected (0, 0) got ({start}, {end})")
        failed = True
    start, end = window2minutes("10:00:00", None, minutes_now)
    if start != 0 or end != 0:
        print(f"ERROR: Test 11b failed - expected (0, 0) got ({start}, {end})")
        failed = True
    start, end = window2minutes(None, None, minutes_now)
    if start != 0 or end != 0:
        print(f"ERROR: Test 11c failed - expected (0, 0) got ({start}, {end})")
        failed = True

    # Test 12: window2minutes with "unknown" time string - should return (0, 0)
    print("Test 12: window2minutes with 'unknown' time string")
    minutes_now = 8 * 60
    start, end = window2minutes("unknown", "14:00:00", minutes_now)
    if start != 0 or end != 0:
        print(f"ERROR: Test 12a failed - expected (0, 0) got ({start}, {end})")
        failed = True
    start, end = window2minutes("10:00:00", "unknown", minutes_now)
    if start != 0 or end != 0:
        print(f"ERROR: Test 12b failed - expected (0, 0) got ({start}, {end})")
        failed = True

    print("**** window2minutes tests completed ****")
    return failed
