# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fox API functions
# -----------------------------------------------------------------------------

import sys
import os
from datetime import datetime
import pytz
from fox import validate_schedule, minutes_to_schedule_time


def test_validate_schedule_empty(my_predbat):
    """
    Test validate_schedule with an empty schedule - should return a default SelfUse schedule
    """
    print("  - test_validate_schedule_empty")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = []
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # Should return a single entry with SelfUse mode for the whole day
    assert len(result) == 1
    assert result[0]["enable"] == 1
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 23
    assert result[0]["endMinute"] == 59
    assert result[0]["workMode"] == "SelfUse"
    assert result[0]["fdSoc"] == reserve
    assert result[0]["maxSoc"] == 100
    assert result[0]["fdPwr"] == fdPwr_max
    assert result[0]["minSocOnGrid"] == reserve
    return False


def test_validate_schedule_single_charge_midnight(my_predbat):
    """
    Test validate_schedule with a single charge window starting at midnight
    """
    print("  - test_validate_schedule_single_charge_midnight")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = [
        {
            "enable": 1,
            "startHour": 0,
            "startMinute": 0,
            "endHour": 5,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # Should return 2 entries: charge window + demand mode after
    assert len(result) == 2

    print(result)
    
    # First entry should be the charge window with adjusted end time (5:29 -> 5:28 inclusive)
    assert result[0]["enable"] == 1
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 5
    assert result[0]["endMinute"] == 29
    assert result[0]["workMode"] == "ForceCharge"
    
    # Second entry should be demand mode from 5:29 to 23:59
    assert result[1]["enable"] == 1
    assert result[1]["startHour"] == 5
    assert result[1]["startMinute"] == 30
    assert result[1]["endHour"] == 23
    assert result[1]["endMinute"] == 59
    assert result[1]["workMode"] == "SelfUse"
    assert result[1]["fdSoc"] == reserve
    assert result[1]["maxSoc"] == 100
    assert result[1]["fdPwr"] == fdPwr_max
    assert result[1]["minSocOnGrid"] == reserve
    return False


def test_validate_schedule_single_charge_midday(my_predbat):
    """
    Test validate_schedule with a single charge window in the middle of the day
    """
    print("  - test_validate_schedule_single_charge_midday")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # Should return 3 entries: demand before + charge + demand after
    assert len(result) == 3
    
    # First entry: demand mode from 0:00 to 2:29
    assert result[0]["enable"] == 1
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 2
    assert result[0]["endMinute"] == 29
    assert result[0]["workMode"] == "SelfUse"
    
    # Second entry: charge window from 2:30 to 5:28 (5:30 -> 5:28 inclusive)
    assert result[1]["enable"] == 1
    assert result[1]["startHour"] == 2
    assert result[1]["startMinute"] == 30
    assert result[1]["endHour"] == 5
    assert result[1]["endMinute"] == 29
    assert result[1]["workMode"] == "ForceCharge"
    
    # Third entry: demand mode from 5:29 to 23:59
    assert result[2]["enable"] == 1
    assert result[2]["startHour"] == 5
    assert result[2]["startMinute"] == 30
    assert result[2]["endHour"] == 23
    assert result[2]["endMinute"] == 59
    assert result[2]["workMode"] == "SelfUse"
    return False


def test_validate_schedule_discharge_window(my_predbat):
    """
    Test validate_schedule with a discharge window
    """
    print("  - test_validate_schedule_discharge_window")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = [
        {
            "enable": 1,
            "startHour": 16,
            "startMinute": 0,
            "endHour": 19,
            "endMinute": 0,
            "workMode": "ForceDischarge",
            "fdSoc": 10,
            "maxSoc": 90,
            "fdPwr": 5000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 12
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # Should return 3 entries: demand before + discharge + demand after
    assert len(result) == 3
    
    # First entry: demand mode from 0:00 to 15:59
    assert result[0]["enable"] == 1
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 15
    assert result[0]["endMinute"] == 59
    assert result[0]["fdSoc"] == 12
    assert result[0]["fdPwr"] == fdPwr_max
    assert result[0]["workMode"] == "SelfUse"
    assert result[0]["maxSoc"] == 100
    
    # Second entry: discharge window from 16:00 to 18:59 (19:00 -> 18:59 inclusive)
    assert result[1]["enable"] == 1
    assert result[1]["startHour"] == 16
    assert result[1]["startMinute"] == 0
    assert result[1]["endHour"] == 18
    assert result[1]["endMinute"] == 59
    assert result[1]["workMode"] == "ForceDischarge"
    assert result[1]["fdSoc"] == 10
    assert result[1]["fdPwr"] == 5000
    assert result[1]["maxSoc"] == 90
    
    # Third entry: demand mode from 19:00 to 23:59
    assert result[2]["enable"] == 1
    assert result[2]["startHour"] == 19
    assert result[2]["startMinute"] == 0
    assert result[2]["endHour"] == 23
    assert result[2]["endMinute"] == 59
    assert result[2]["fdSoc"] == 12
    assert result[2]["fdPwr"] == fdPwr_max
    assert result[2]["workMode"] == "SelfUse"
    assert result[2]["maxSoc"] == 100
    return False


def test_validate_schedule_full_day(my_predbat):
    """
    Test validate_schedule with a schedule covering the full day
    """
    print("  - test_validate_schedule_full_day")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = [
        {
            "enable": 1,
            "startHour": 0,
            "startMinute": 0,
            "endHour": 23,
            "endMinute": 59,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # Should return just the single entry adjusted to inclusive times
    assert len(result) == 1
    assert result[0]["enable"] == 1
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 23
    assert result[0]["endMinute"] == 59  # 23:59 -> 23:59 inclusive
    assert result[0]["workMode"] == "ForceCharge"
    return False


def test_validate_schedule_end_minute_zero(my_predbat):
    """
    Test validate_schedule with end minute of 0 (e.g., 5:00)
    """
    print("  - test_validate_schedule_end_minute_zero")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 0,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # Should return 3 entries
    assert len(result) == 3
    
    # Charge window should end at 4:59 (5:00 -> 4:59 inclusive)
    assert result[1]["endHour"] == 4
    assert result[1]["endMinute"] == 59
    
    # Demand after should start at 5:00
    assert result[2]["startHour"] == 5
    assert result[2]["startMinute"] == 0
    return False


def test_minutes_to_schedule_time(my_predbat):
    """
    Test the minutes_to_schedule_time helper function
    """
    print("  - test_minutes_to_schedule_time")
    
    # Test with time before minutes_now (should add 24 hours)
    result = minutes_to_schedule_time(2, 30, 3 * 60)  # 2:30 when current is 3:00
    assert result == (2 * 60 + 30 + 24 * 60) - (3 * 60)  # 23.5 hours
    
    # Test with time after minutes_now
    result = minutes_to_schedule_time(14, 30, 3 * 60)  # 14:30 when current is 3:00
    assert result == (14 * 60 + 30) - (3 * 60)  # 11.5 hours
    
    # Test with same time
    result = minutes_to_schedule_time(3, 0, 3 * 60)  # 3:00 when current is 3:00
    assert result == 0
    
    return False


def test_validate_schedule_multiple_windows(my_predbat):
    """
    Test that validate_schedule only keeps the first (nearest) window
    """
    print("  - test_validate_schedule_multiple_windows")
    local_tz = pytz.timezone("Europe/London")
    
    # Provide multiple windows - should only keep the first chronologically
    new_schedule = [
        {
            "enable": 1,
            "startHour": 16,
            "startMinute": 0,
            "endHour": 19,
            "endMinute": 0,
            "workMode": "ForceDischarge",
            "fdSoc": 10,
            "maxSoc": 100,
            "fdPwr": 5000,
            "minSocOnGrid": 10,
        },
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(local_tz, new_schedule, reserve, fdPwr_max)
    
    # The function should sort by time and keep only the first one
    # The first chronologically should be the 2:30-5:30 charge window
    # So we should have 3 entries total
    assert len(result) == 3
    
    # The middle entry should be the charge window (2:30-5:28)
    assert result[1]["workMode"] == "ForceCharge"
    assert result[1]["startHour"] == 2
    assert result[1]["startMinute"] == 30
    
    return False


def run_fox_api_tests(my_predbat):
    """
    Run all Fox API tests
    """
    print("Testing Fox API validate_schedule function")
    
    tests = [
        test_validate_schedule_empty,
        test_validate_schedule_single_charge_midnight,
        test_validate_schedule_single_charge_midday,
        test_validate_schedule_discharge_window,
        test_validate_schedule_full_day,
        test_validate_schedule_end_minute_zero,
        test_minutes_to_schedule_time,
        test_validate_schedule_multiple_windows,
    ]
    
    failed = False
    for test in tests:
        try:
            result = test(my_predbat)
            if result:
                failed = True
                break
        except Exception as e:
            print(f"    ERROR: Test {test.__name__} raised exception: {e}")
            import traceback
            traceback.print_exc()
            failed = True
            break
    
    return failed
