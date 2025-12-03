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
import asyncio
import pytz
from fox import validate_schedule, minutes_to_schedule_time, end_minute_inclusive_to_exclusive, FoxAPI, schedules_are_equal


class MockFoxAPI:
    """Mock FoxAPI class for testing compute_schedule"""
    def __init__(self):
        self.device_battery_charging_time = {}
        self.device_scheduler = {}
        self.device_settings = {}
        self.local_schedule = {}
        self.device_current_schedule = {}
        self.fdpwr_max = {}
        
    def getMinSocOnGrid(self, deviceSN):
        """Mock implementation of getMinSocOnGrid"""
        return self.device_settings.get(deviceSN, {}).get("MinSocOnGrid", {}).get("value", 10)


class MockFoxAPIWithRequests(FoxAPI):
    """
    Mock FoxAPI class that overrides request_get to return mock data.
    Used for testing API endpoint methods without making real HTTP requests.
    """
    def __init__(self):
        # Don't call parent __init__ since we're not using ComponentBase properly
        self.key = "test_api_key"
        self.automatic = False
        self.failures_total = 0
        self.device_list = []
        self.device_detail = {}
        self.device_power_generation = {}
        self.available_variables = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_production = {}
        self.device_battery_charging_time = {}
        self.device_scheduler = {}
        self.device_current_schedule = {}
        self.local_schedule = {}
        self.fdpwr_max = {}
        self.fdsoc_min = {}
        self.local_tz = pytz.timezone("Europe/London")
        
        # Mock request responses - keyed by API path
        self.mock_responses = {}
        self.request_log = []  # Track all requests made
        
    def log(self, message):
        """Mock log method"""
        pass
        
    def dashboard_item(self, entity_id, state, attributes, app):
        """Mock dashboard_item method"""
        pass
        
    def get_state_wrapper(self, entity_id, default=None):
        """Mock get_state_wrapper method"""
        return default
        
    def update_success_timestamp(self):
        """Mock update_success_timestamp method"""
        pass
        
    def set_mock_response(self, path, response):
        """Set a mock response for a specific API path"""
        self.mock_responses[path] = response
        
    async def request_get(self, path, post=False, datain=None):
        """Override request_get to return mock data"""
        self.request_log.append({"path": path, "post": post, "datain": datain})
        return self.mock_responses.get(path, None)


def test_validate_schedule_empty(my_predbat):
    """
    Test validate_schedule with an empty schedule - should return a default SelfUse schedule
    """
    print("  - test_validate_schedule_empty")
    local_tz = pytz.timezone("Europe/London")
    new_schedule = []
    reserve = 10
    fdPwr_max = 8000

    timenow = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
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
    timenow = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
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
    
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
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
    timenow = datetime.now(local_tz).replace(hour=4, minute=0, second=0, microsecond=0)
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
    
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
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
    timenow = datetime.now(local_tz).replace(hour=4, minute=0, second=0, microsecond=0)
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
    
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
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
    timenow = datetime.now(local_tz).replace(hour=4, minute=0, second=0, microsecond=0)
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
    
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
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
    timenow = datetime.now(local_tz).replace(hour=4, minute=0, second=0, microsecond=0)
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
    
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
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
    Test that validate_schedule only keeps the first (nearest from now) window
    """
    print("  - test_validate_schedule_multiple_windows")
    now = pytz.timezone("Europe/London")
    timenow = datetime.now(now).replace(hour=14, minute=0, second=0, microsecond=0)
    
    # Provide multiple windows - should only keep the first chronologically
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
        ,
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
    ]
    reserve = 10
    fdPwr_max = 8000
    
    result = validate_schedule(timenow, new_schedule, reserve, fdPwr_max)
    
    # The function should sort by time from now (midnight assumed) and keep only the first one
    # The first chronologically from midnight should be the 2:30-5:30 charge window
    # So we should have 3 entries total (demand before, charge, demand after)
    assert len(result) == 3

    print(result)
    
    # First entry should be demand mode from
    assert result[0]["workMode"] == "SelfUse"
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 15
    assert result[0]["endMinute"] == 59
    
    # Second entry should be the discharge window
    assert result[1]["workMode"] == "ForceDischarge"
    assert result[1]["startHour"] == 16
    assert result[1]["startMinute"] == 00
    assert result[1]["endHour"] == 18
    assert result[1]["endMinute"] == 59 
    
    # Third entry should be demand mode 
    assert result[2]["workMode"] == "SelfUse"
    assert result[2]["startHour"] == 19
    assert result[2]["startMinute"] == 00
    assert result[2]["endHour"] == 23
    assert result[2]["endMinute"] == 59
    
    return False


def test_compute_schedule_scheduler_enabled_charge(my_predbat):
    """
    Test compute_schedule with scheduler enabled and a charge window
    """
    print("  - test_compute_schedule_scheduler_enabled_charge")
    
    fox = MockFoxAPI()
    deviceSN = "TEST123"
    
    # Setup device with scheduler enabled
    fox.device_scheduler[deviceSN] = {
        "enable": True,
        "groups": [
            {
                "startHour": 2,
                "startMinute": 30,
                "endHour": 5,
                "endMinute": 29,
                "enable": 1,
                "fdPwr": 8000,
                "workMode": "ForceCharge",
                "fdSoc": 100,
                "maxSoc": 100,
                "minSocOnGrid": 10,
            }
        ]
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    
    # Call compute_schedule (sync wrapper since it's now a regular method)
    import asyncio
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))
    
    # Verify charge schedule was extracted
    assert deviceSN in fox.local_schedule
    assert "charge" in fox.local_schedule[deviceSN]
    
    charge = fox.local_schedule[deviceSN]["charge"]
    assert charge["start_time"] == "02:30:00"
    assert charge["end_time"] == "05:30:00"  # 5:29 inclusive -> 5:30 exclusive
    assert charge["soc"] == 100
    assert charge["power"] == 8000
    assert charge["enable"] == 1
    
    return False


def test_compute_schedule_scheduler_enabled_discharge(my_predbat):
    """
    Test compute_schedule with scheduler enabled and a discharge window
    """
    print("  - test_compute_schedule_scheduler_enabled_discharge")
    
    fox = MockFoxAPI()
    deviceSN = "TEST123"
    
    # Setup device with scheduler enabled and discharge window
    fox.device_scheduler[deviceSN] = {
        "enable": True,
        "groups": [
            {
                "startHour": 16,
                "startMinute": 0,
                "endHour": 18,
                "endMinute": 59,
                "enable": 1,
                "fdPwr": 5000,
                "workMode": "ForceDischarge",
                "fdSoc": 10,
                "maxSoc": 90,
                "minSocOnGrid": 10,
            }
        ]
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    
    import asyncio
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))
    
    # Verify discharge schedule was extracted
    assert deviceSN in fox.local_schedule
    assert "discharge" in fox.local_schedule[deviceSN]
    
    discharge = fox.local_schedule[deviceSN]["discharge"]
    assert discharge["start_time"] == "16:00:00"
    assert discharge["end_time"] == "19:00:00"
    assert discharge["soc"] == 10
    assert discharge["power"] == 5000
    assert discharge["enable"] == 1
    
    return False


def test_compute_schedule_scheduler_disabled_battery_times(my_predbat):
    """
    Test compute_schedule with scheduler disabled, using battery charging times
    """
    print("  - test_compute_schedule_scheduler_disabled_battery_times")
    
    fox = MockFoxAPI()
    deviceSN = "TEST123"
    
    # Setup device with scheduler disabled and battery charging times
    fox.device_scheduler[deviceSN] = {
        "enable": False,
        "groups": []
    }
    fox.device_battery_charging_time[deviceSN] = {
        "enable1": True,
        "startTime1": {"hour": 2, "minute": 30},
        "endTime1": {"hour": 5, "minute": 30},
        "enable2": False,
        "startTime2": {"hour": 0, "minute": 0},
        "endTime2": {"hour": 0, "minute": 0},
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    
    import asyncio
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))
    
    # Verify charge schedule was created from battery times
    assert deviceSN in fox.local_schedule
    assert "charge" in fox.local_schedule[deviceSN]
    
    charge = fox.local_schedule[deviceSN]["charge"]
    assert charge["start_time"] == "02:30:00"
    assert charge["end_time"] == "05:31:00"  # 5:30 inclusive -> 5:31 exclusive
    assert charge["soc"] == 100  # maxSoc from battery slot
    assert charge["enable"] == 1
    
    return False


def test_compute_schedule_both_charge_and_discharge(my_predbat):
    """
    Test compute_schedule with both charge and discharge windows
    """
    print("  - test_compute_schedule_both_charge_and_discharge")
    
    fox = MockFoxAPI()
    deviceSN = "TEST123"
    
    # Setup device with both charge and discharge windows
    fox.device_scheduler[deviceSN] = {
        "enable": True,
        "groups": [
            {
                "startHour": 2,
                "startMinute": 30,
                "endHour": 5,
                "endMinute": 29,
                "enable": 1,
                "fdPwr": 8000,
                "workMode": "ForceCharge",
                "fdSoc": 100,
                "maxSoc": 100,
                "minSocOnGrid": 10,
            },
            {
                "startHour": 16,
                "startMinute": 0,
                "endHour": 18,
                "endMinute": 59,
                "enable": 1,
                "fdPwr": 5000,
                "workMode": "ForceDischarge",
                "fdSoc": 10,
                "maxSoc": 90,
                "minSocOnGrid": 10,
            }
        ]
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    
    import asyncio
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))
    
    # Verify both schedules were extracted
    assert "charge" in fox.local_schedule[deviceSN]
    assert "discharge" in fox.local_schedule[deviceSN]
    
    charge = fox.local_schedule[deviceSN]["charge"]
    assert charge["start_time"] == "02:30:00"
    assert charge["end_time"] == "05:30:00"  # 5:29 inclusive -> 5:30 exclusive
    assert charge["soc"] == 100
    assert charge['enable'] == 1
    
    discharge = fox.local_schedule[deviceSN]["discharge"]
    assert discharge["start_time"] == "16:00:00"
    assert discharge["end_time"] == "19:00:00"  # 18:59 inclusive -> 19:00 exclusive
    assert discharge["soc"] == 10
    assert discharge['enable'] == 1
    
    return False


def test_compute_schedule_no_enabled_windows(my_predbat):
    """
    Test compute_schedule with no enabled windows
    """
    print("  - test_compute_schedule_no_enabled_windows")
    
    fox = MockFoxAPI()
    deviceSN = "TEST123"
    
    # Setup device with disabled windows
    fox.device_scheduler[deviceSN] = {
        "enable": True,
        "groups": [
            {
                "startHour": 2,
                "startMinute": 30,
                "endHour": 5,
                "endMinute": 30,
                "enable": 0,  # Disabled
                "fdPwr": 8000,
                "workMode": "ForceCharge",
                "fdSoc": 100,
                "maxSoc": 100,
                "minSocOnGrid": 10,
            }
        ]
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    
    import asyncio
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))
    
    # Verify no charge or discharge schedules were created
    assert "charge" not in fox.local_schedule[deviceSN]
    assert "discharge" not in fox.local_schedule[deviceSN]
    
    return False


def test_compute_schedule_end_time_midnight(my_predbat):
    """
    Test compute_schedule with end time at midnight (0:00)
    """
    print("  - test_compute_schedule_end_time_midnight")
    
    fox = MockFoxAPI()
    deviceSN = "TEST123"
    
    # Setup device with charge ending at midnight
    fox.device_scheduler[deviceSN] = {
        "enable": True,
        "groups": [
            {
                "startHour": 23,
                "startMinute": 30,
                "endHour": 23,
                "endMinute": 59,
                "enable": 1,
                "fdPwr": 8000,
                "workMode": "ForceCharge",
                "fdSoc": 100,
                "maxSoc": 100,
                "minSocOnGrid": 10,
            }
        ]
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    
    import asyncio
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))
    
    # Verify charge schedule with midnight handling
    charge = fox.local_schedule[deviceSN]["charge"]
    assert charge["start_time"] == "23:30:00"
    # 0:00 inclusive should become 23:59 (previous day end)
    # But the function should handle the rollback to 23:59
    assert charge["end_time"] == "23:59:00"
    
    return False


def test_end_minute_inclusive_to_exclusive(my_predbat):
    """
    Test the end_minute_inclusive_to_exclusive helper function
    """
    print("  - test_end_minute_inclusive_to_exclusive")
    
    # Test normal case - add 1 minute
    hour, minute = end_minute_inclusive_to_exclusive(5, 30)
    assert hour == 5
    assert minute == 31
    
    # Test hour rollover at 59 minutes
    hour, minute = end_minute_inclusive_to_exclusive(5, 59)
    assert hour == 6
    assert minute == 0
    
    # Test end of day (23:59 stays 23:59)
    hour, minute = end_minute_inclusive_to_exclusive(23, 59)
    assert hour == 23
    assert minute == 59
    
    # Test minute 0 (no change)
    hour, minute = end_minute_inclusive_to_exclusive(5, 0)
    assert hour == 5
    assert minute == 0
    
    return False


def test_schedules_are_equal_identical(my_predbat):
    """
    Test schedules_are_equal with identical schedules
    """
    print("  - test_schedules_are_equal_identical")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
    schedule2 = [
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
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == True, "Identical schedules should be equal"
    return False


def test_schedules_are_equal_different_length(my_predbat):
    """
    Test schedules_are_equal with different length schedules
    """
    print("  - test_schedules_are_equal_different_length")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
    schedule2 = [
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
        },
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
    ]
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == False, "Different length schedules should not be equal"
    return False


def test_schedules_are_equal_different_values(my_predbat):
    """
    Test schedules_are_equal with different values in schedules
    """
    print("  - test_schedules_are_equal_different_values")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
    schedule2 = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "fdSoc": 80,  # Different fdSoc
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == False, "Schedules with different values should not be equal"
    return False


def test_schedules_are_equal_different_work_mode(my_predbat):
    """
    Test schedules_are_equal with different work modes
    """
    print("  - test_schedules_are_equal_different_work_mode")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
    schedule2 = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 30,
            "workMode": "SelfUse",  # Different workMode
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == False, "Schedules with different work modes should not be equal"
    return False


def test_schedules_are_equal_different_times(my_predbat):
    """
    Test schedules_are_equal with different times
    """
    print("  - test_schedules_are_equal_different_times")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
    schedule2 = [
        {
            "enable": 1,
            "startHour": 3,  # Different start time
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
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == False, "Schedules with different times should not be equal"
    return False


def test_schedules_are_equal_both_empty(my_predbat):
    """
    Test schedules_are_equal with both empty schedules
    """
    print("  - test_schedules_are_equal_both_empty")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = []
    schedule2 = []
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == True, "Both empty schedules should be equal"
    return False


def test_schedules_are_equal_one_empty(my_predbat):
    """
    Test schedules_are_equal with one empty schedule
    """
    print("  - test_schedules_are_equal_one_empty")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
    schedule2 = []
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == False, "One empty schedule should not be equal to non-empty"
    return False


def test_schedules_are_equal_same_but_different_order(my_predbat):
    """
    Test schedules_are_equal with same entries but different order - should sort and be equal
    """
    print("  - test_schedules_are_equal_same_but_different_order")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
        },
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
    ]
    # Same entries but in reverse order
    schedule2 = [
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
        },
    ]
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == True, "Same entries in different order should be equal after sorting"
    return False


def test_schedules_are_equal_missing_key(my_predbat):
    """
    Test schedules_are_equal when one schedule has a key the other doesn't
    """
    print("  - test_schedules_are_equal_missing_key")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    schedule1 = [
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
            # Missing minSocOnGrid
        }
    ]
    schedule2 = [
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
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == False, "Schedules with missing keys should not be equal"
    return False


def test_schedules_are_equal_disabled_entries_stripped(my_predbat):
    """
    Test schedules_are_equal strips disabled entries before comparison
    """
    print("  - test_schedules_are_equal_disabled_entries_stripped")
    local_tz = pytz.timezone("Europe/London")
    timenow = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    
    # Schedule with one enabled entry
    schedule1 = [
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
    # Same schedule but with an additional disabled entry
    schedule2 = [
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
        },
        {
            "enable": 0,  # Disabled entry
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
    ]
    
    result = schedules_are_equal(timenow, schedule1, schedule2)
    assert result == True, "Disabled entries should be stripped before comparison"
    return False


# ============================================================================
# API Endpoint Tests with Mocked request_get
# ============================================================================

def test_api_get_device_list(my_predbat):
    """
    Test get_device_list API endpoint
    """
    print("  - test_api_get_device_list")
    
    fox = MockFoxAPIWithRequests()
    fox.set_mock_response("/op/v0/device/list", {
        "data": [
            {
                "deviceType": "KH8",
                "hasBattery": True,
                "hasPV": True,
                "stationName": "Test Home",
                "moduleSN": "609W6EUF46MB519",
                "deviceSN": "TEST123456",
                "productType": "KH",
                "stationID": "test-station-id",
                "status": 1
            }
        ]
    })
    
    result = asyncio.run(fox.get_device_list())
    
    assert len(result) == 1
    assert result[0]["deviceSN"] == "TEST123456"
    assert result[0]["deviceType"] == "KH8"
    assert result[0]["hasBattery"] == True
    assert fox.device_list == result
    
    # Verify request was made correctly
    assert len(fox.request_log) == 1
    assert fox.request_log[0]["path"] == "/op/v0/device/list"
    assert fox.request_log[0]["post"] == True
    
    return False


def test_api_get_device_detail(my_predbat):
    """
    Test get_device_detail API endpoint
    """
    print("  - test_api_get_device_detail")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    fox.set_mock_response("/op/v0/device/detail", {
        "deviceType": "KH8",
        "masterVersion": "1.34",
        "hasPV": True,
        "deviceSN": deviceSN,
        "capacity": 8,
        "hasBattery": True,
        "function": {"scheduler": True},
        "stationName": "Test Home",
        "batteryList": [
            {"batterySN": "BAT001", "model": "EP11", "type": "bcu", "version": "1.005"},
            {"batterySN": "BAT002", "model": "EP11", "type": "bmu", "version": "1.05", "capacity": 10360},
        ],
        "productType": "KH",
        "status": 1
    })
    
    result = asyncio.run(fox.get_device_detail(deviceSN))
    
    assert deviceSN in fox.device_detail
    assert fox.device_detail[deviceSN]["deviceType"] == "KH8"
    assert fox.device_detail[deviceSN]["capacity"] == 8
    assert fox.device_detail[deviceSN]["hasBattery"] == True
    assert fox.device_detail[deviceSN]["function"]["scheduler"] == True
    
    return False


def test_api_get_device_setting(my_predbat):
    """
    Test get_device_setting API endpoint
    """
    print("  - test_api_get_device_setting")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    fox.set_mock_response("/op/v0/device/setting/get", {
        "enumList": ["PeakShaving", "Feedin", "SelfUse"],
        "unit": "",
        "precision": 1.0,
        "value": "SelfUse"
    })
    
    result = asyncio.run(fox.get_device_setting(deviceSN, "WorkMode"))
    
    assert result is not None
    assert result["value"] == "SelfUse"
    assert "SelfUse" in result["enumList"]
    assert deviceSN in fox.device_settings
    assert fox.device_settings[deviceSN]["WorkMode"]["value"] == "SelfUse"
    
    return False


def test_api_set_device_setting(my_predbat):
    """
    Test set_device_setting API endpoint
    """
    print("  - test_api_set_device_setting")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Mock successful response (empty dict means success)
    fox.set_mock_response("/op/v0/device/setting/set", {})
    
    result = asyncio.run(fox.set_device_setting(deviceSN, "MinSoc", 20))
    
    assert result == True
    
    # Verify request was made with correct data
    assert len(fox.request_log) == 1
    assert fox.request_log[0]["path"] == "/op/v0/device/setting/set"
    assert fox.request_log[0]["post"] == True
    assert fox.request_log[0]["datain"]["sn"] == deviceSN
    assert fox.request_log[0]["datain"]["key"] == "MinSoc"
    assert fox.request_log[0]["datain"]["value"] == 20
    
    return False


def test_api_get_battery_charging_time(my_predbat):
    """
    Test get_battery_charging_time API endpoint
    """
    print("  - test_api_get_battery_charging_time")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup device with battery
    fox.device_detail[deviceSN] = {"hasBattery": True}
    
    fox.set_mock_response("/op/v0/device/battery/forceChargeTime/get", {
        "enable1": True,
        "startTime1": {"hour": 2, "minute": 30},
        "endTime1": {"hour": 5, "minute": 30},
        "enable2": False,
        "startTime2": {"hour": 0, "minute": 0},
        "endTime2": {"hour": 0, "minute": 0},
    })
    
    result = asyncio.run(fox.get_battery_charging_time(deviceSN))
    
    assert result["enable1"] == True
    assert result["startTime1"]["hour"] == 2
    assert result["startTime1"]["minute"] == 30
    assert result["endTime1"]["hour"] == 5
    assert result["endTime1"]["minute"] == 30
    assert deviceSN in fox.device_battery_charging_time
    
    return False


def test_api_set_battery_charging_time(my_predbat):
    """
    Test set_battery_charging_time API endpoint
    """
    print("  - test_api_set_battery_charging_time")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    fox.set_mock_response("/op/v0/device/battery/forceChargeTime/set", {})
    
    setting = {
        "enable1": True,
        "startTime1": {"hour": 1, "minute": 0},
        "endTime1": {"hour": 4, "minute": 0},
    }
    
    result = asyncio.run(fox.set_battery_charging_time(deviceSN, setting))
    
    assert result == True
    
    # Verify request data
    assert fox.request_log[0]["datain"]["sn"] == deviceSN
    assert fox.request_log[0]["datain"]["enable1"] == True
    
    return False


def test_api_get_scheduler(my_predbat):
    """
    Test get_scheduler API endpoint
    """
    print("  - test_api_get_scheduler")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup device with battery
    fox.device_detail[deviceSN] = {"hasBattery": True, "capacity": 8}
    
    fox.set_mock_response("/op/v1/device/scheduler/get", {
        "enable": 1,
        "groups": [
            {"endHour": 5, "fdPwr": 8000, "minSocOnGrid": 10, "workMode": "ForceCharge", "fdSoc": 100, "enable": 1, "startHour": 2, "maxSoc": 100, "startMinute": 30, "endMinute": 29},
            {"endHour": 0, "fdPwr": 0, "minSocOnGrid": 10, "workMode": "Invalid", "fdSoc": 10, "enable": 0, "startHour": 0, "maxSoc": 100, "startMinute": 0, "endMinute": 0},
        ],
        "properties": {
            "fdpwr": {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 8000.0}},
            "fdsoc": {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
        }
    })
    
    result = asyncio.run(fox.get_scheduler(deviceSN))
    
    assert result["enable"] == 1
    assert len(result["groups"]) == 2
    assert result["groups"][0]["workMode"] == "ForceCharge"
    assert fox.fdpwr_max[deviceSN] == 8000
    assert fox.fdsoc_min[deviceSN] == 10
    
    return False


def test_api_set_scheduler(my_predbat):
    """
    Test set_scheduler API endpoint
    """
    print("  - test_api_set_scheduler")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup existing scheduler state
    fox.device_scheduler[deviceSN] = {
        "enable": False,
        "groups": []
    }
    
    fox.set_mock_response("/op/v1/device/scheduler/enable", {})
    
    groups = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 29,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    
    result = asyncio.run(fox.set_scheduler(deviceSN, groups))
    
    # Verify request was made
    assert len(fox.request_log) == 1
    assert fox.request_log[0]["path"] == "/op/v1/device/scheduler/enable"
    assert fox.request_log[0]["datain"]["deviceSN"] == deviceSN
    assert fox.request_log[0]["datain"]["groups"] == groups
    
    return False


def test_api_set_scheduler_enabled(my_predbat):
    """
    Test set_scheduler_enabled API endpoint
    """
    print("  - test_api_set_scheduler_enabled")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup scheduler as disabled
    fox.device_scheduler[deviceSN] = {"enable": False}
    
    fox.set_mock_response("/op/v1/device/scheduler/set/flag", {})
    
    result = asyncio.run(fox.set_scheduler_enabled(deviceSN, True))
    
    # Verify request was made
    assert len(fox.request_log) == 1
    assert fox.request_log[0]["path"] == "/op/v1/device/scheduler/set/flag"
    assert fox.request_log[0]["datain"]["deviceSN"] == deviceSN
    assert fox.request_log[0]["datain"]["enable"] == 1
    
    # Verify local state was updated
    assert fox.device_scheduler[deviceSN]["enable"] == 1
    
    return False


def test_api_get_real_time_data(my_predbat):
    """
    Test get_real_time_data API endpoint
    """
    print("  - test_api_get_real_time_data")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    fox.set_mock_response("/op/v1/device/real/query", [
        {
            "datas": [
                {"unit": "kW", "name": "PVPower", "variable": "pvPower", "value": 3.5},
                {"unit": "%", "name": "SoC", "variable": "SoC", "value": 75.0},
                {"unit": "kW", "name": "Load Power", "variable": "loadsPower", "value": 2.1},
                {"unit": "kW", "name": "Charge Power", "variable": "batChargePower", "value": 1.4},
                {"unit": "kW", "name": "Feed-in Power", "variable": "feedinPower", "value": 0.0},
            ],
            "time": "2025-09-14 18:43:09 BST+0100",
            "deviceSN": deviceSN
        }
    ])
    
    result = asyncio.run(fox.get_real_time_data(deviceSN))
    
    assert deviceSN in fox.device_values
    assert fox.device_values[deviceSN]["pvPower"]["value"] == 3.5
    assert fox.device_values[deviceSN]["SoC"]["value"] == 75.0
    assert fox.device_values[deviceSN]["loadsPower"]["value"] == 2.1
    
    return False


def test_api_get_device_production(my_predbat):
    """
    Test get_device_production API endpoint
    """
    print("  - test_api_get_device_production")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    fox.set_mock_response("/op/v0/device/report/query", [
        {"unit": "kWh", "values": [0.0, 0.0, 0.0, 0.0, 151.6, 1079.1], "variable": "generation"},
        {"unit": "kWh", "values": [0.0, 0.0, 0.0, 0.0, 68.6, 685.2], "variable": "feedin"},
        {"unit": "kWh", "values": [0.0, 0.0, 0.0, 0.0, 52.7, 300.1], "variable": "gridConsumption"},
    ])
    
    result = asyncio.run(fox.get_device_production(deviceSN))
    
    assert deviceSN in fox.device_production
    assert len(fox.device_production[deviceSN]) == 3
    
    return False


def test_api_get_device_power_generation(my_predbat):
    """
    Test get_device_power_generation API endpoint
    """
    print("  - test_api_get_device_power_generation")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    fox.set_mock_response("/op/v0/device/generation", {
        "month": 867.6,
        "today": 17.7,
        "cumulative": 5765.7
    })
    
    result = asyncio.run(fox.get_device_power_generation(deviceSN))
    
    assert deviceSN in fox.device_power_generation
    assert fox.device_power_generation[deviceSN]["today"] == 17.7
    assert fox.device_power_generation[deviceSN]["month"] == 867.6
    assert fox.device_power_generation[deviceSN]["cumulative"] == 5765.7
    
    return False


def test_api_request_failure(my_predbat):
    """
    Test API behavior when request_get returns None (failure)
    """
    print("  - test_api_request_failure")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Don't set any mock response - will return None
    
    result = asyncio.run(fox.get_device_setting(deviceSN, "MinSoc"))
    
    assert result is None
    
    return False


def test_api_set_device_setting_failure(my_predbat):
    """
    Test set_device_setting when API returns None (failure)
    """
    print("  - test_api_set_device_setting_failure")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup existing setting so failure is detected
    fox.device_settings[deviceSN] = {"MinSoc": {"value": 10}}
    
    # Don't set mock response - will return None (failure)
    
    result = asyncio.run(fox.set_device_setting(deviceSN, "MinSoc", 20))
    
    assert result == False
    
    return False


def test_api_get_battery_charging_time_no_battery(my_predbat):
    """
    Test get_battery_charging_time returns empty dict when device has no battery
    """
    print("  - test_api_get_battery_charging_time_no_battery")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup device without battery
    fox.device_detail[deviceSN] = {"hasBattery": False}
    
    result = asyncio.run(fox.get_battery_charging_time(deviceSN))
    
    assert result == {}
    # Should not have made any API request
    assert len(fox.request_log) == 0
    
    return False


def test_api_set_scheduler_no_change(my_predbat):
    """
    Test set_scheduler doesn't make request when schedule hasn't changed
    """
    print("  - test_api_set_scheduler_no_change")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    groups = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 5,
            "endMinute": 29,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        }
    ]
    
    # Setup existing scheduler with same groups
    fox.device_scheduler[deviceSN] = {
        "enable": True,
        "groups": groups.copy()
    }
    
    result = asyncio.run(fox.set_scheduler(deviceSN, groups))
    
    # Should not have made any API request since schedule is the same
    assert len(fox.request_log) == 0
    
    return False


def test_api_set_scheduler_disable_when_empty(my_predbat):
    """
    Test set_scheduler disables scheduler when empty groups provided
    """
    print("  - test_api_set_scheduler_disable_when_empty")
    
    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"
    
    # Setup scheduler as enabled
    fox.device_scheduler[deviceSN] = {"enable": True, "groups": []}
    
    fox.set_mock_response("/op/v1/device/scheduler/set/flag", {})
    
    # Call with empty groups
    result = asyncio.run(fox.set_scheduler(deviceSN, []))
    
    # Should have called set_scheduler_enabled to disable
    assert len(fox.request_log) == 1
    assert fox.request_log[0]["path"] == "/op/v1/device/scheduler/set/flag"
    assert fox.request_log[0]["datain"]["enable"] == 0
    
    return False


def run_fox_api_tests(my_predbat):
    """
    Run all Fox API tests
    """
    print("Fox API Tests:")
    
    failed = False
    try:
        # schedules_are_equal tests
        failed |= test_schedules_are_equal_identical(my_predbat)
        failed |= test_schedules_are_equal_different_length(my_predbat)
        failed |= test_schedules_are_equal_different_values(my_predbat)
        failed |= test_schedules_are_equal_different_work_mode(my_predbat)
        failed |= test_schedules_are_equal_different_times(my_predbat)
        failed |= test_schedules_are_equal_both_empty(my_predbat)
        failed |= test_schedules_are_equal_one_empty(my_predbat)
        failed |= test_schedules_are_equal_same_but_different_order(my_predbat)
        failed |= test_schedules_are_equal_missing_key(my_predbat)
        failed |= test_schedules_are_equal_disabled_entries_stripped(my_predbat)

        # validate_schedule tests
        failed |= test_validate_schedule_empty(my_predbat)
        failed |= test_validate_schedule_single_charge_midnight(my_predbat)
        failed |= test_validate_schedule_single_charge_midday(my_predbat)
        failed |= test_validate_schedule_discharge_window(my_predbat)
        failed |= test_validate_schedule_full_day(my_predbat)
        failed |= test_validate_schedule_end_minute_zero(my_predbat)
        failed |= test_minutes_to_schedule_time(my_predbat)
        failed |= test_validate_schedule_multiple_windows(my_predbat)
        
        # compute_schedule tests
        failed |= test_compute_schedule_scheduler_enabled_charge(my_predbat)
        failed |= test_compute_schedule_scheduler_enabled_discharge(my_predbat)
        failed |= test_compute_schedule_scheduler_disabled_battery_times(my_predbat)
        failed |= test_compute_schedule_both_charge_and_discharge(my_predbat)
        failed |= test_compute_schedule_no_enabled_windows(my_predbat)
        failed |= test_compute_schedule_end_time_midnight(my_predbat)
        
        # Helper function tests
        failed |= test_end_minute_inclusive_to_exclusive(my_predbat)
        
        # API endpoint tests with mocked request_get
        failed |= test_api_get_device_list(my_predbat)
        failed |= test_api_get_device_detail(my_predbat)
        failed |= test_api_get_device_setting(my_predbat)
        failed |= test_api_set_device_setting(my_predbat)
        failed |= test_api_get_battery_charging_time(my_predbat)
        failed |= test_api_set_battery_charging_time(my_predbat)
        failed |= test_api_get_scheduler(my_predbat)
        failed |= test_api_set_scheduler(my_predbat)
        failed |= test_api_set_scheduler_enabled(my_predbat)
        failed |= test_api_get_real_time_data(my_predbat)
        failed |= test_api_get_device_production(my_predbat)
        failed |= test_api_get_device_power_generation(my_predbat)
        failed |= test_api_request_failure(my_predbat)
        failed |= test_api_set_device_setting_failure(my_predbat)
        failed |= test_api_get_battery_charging_time_no_battery(my_predbat)
        failed |= test_api_set_scheduler_no_change(my_predbat)
        failed |= test_api_set_scheduler_disable_when_empty(my_predbat)
    except Exception as e:
        print(f"ERROR: Fox API test failed with exception: {e}")
        import traceback
        traceback.print_exc()
        failed = True
    
    return failed

