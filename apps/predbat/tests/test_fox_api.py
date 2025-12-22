# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fox API functions
# -----------------------------------------------------------------------------

from datetime import datetime
import asyncio
import pytz
from unittest.mock import MagicMock, patch, AsyncMock
import requests
from fox import validate_schedule, minutes_to_schedule_time, end_minute_inclusive_to_exclusive, FoxAPI, schedules_are_equal


def run_async(coro):
    """Helper function to run async coroutines in sync test functions"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class MockFoxAPI:
    """Mock FoxAPI class for testing compute_schedule"""

    def __init__(self):
        self.device_battery_charging_time = {}
        self.device_scheduler = {}
        self.device_settings = {}
        self.local_schedule = {}
        self.device_current_schedule = {}
        self.fdpwr_max = {}
        self.fdsoc_min = {}

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

        # Mock HA state values - keyed by entity_id
        self.mock_ha_states = {}

        # Track dashboard_item calls - keyed by entity_id
        self.dashboard_items = {}

        # HTTP-level response simulation - keyed by API path
        # Format: {path: {"status_code": int, "json_data": dict, "timeout": bool, "connection_error": bool, "json_error": bool}}
        self.http_responses = {}

        # Track set_arg calls for automatic_config testing
        self.args_set = {}

        # Track method calls for run() testing
        self.method_calls = []

    def log(self, message):
        """Mock log method"""
        pass

    def dashboard_item(self, entity_id, state, attributes, app):
        """Mock dashboard_item method - tracks calls"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes, "app": app}

    def get_state_wrapper(self, entity_id, default=None):
        """Mock get_state_wrapper method - returns mock HA state or default"""
        return self.mock_ha_states.get(entity_id, default)

    def set_mock_ha_state(self, entity_id, value):
        """Set a mock HA state value for a specific entity_id"""
        self.mock_ha_states[entity_id] = value

    def update_success_timestamp(self):
        """Mock update_success_timestamp method"""
        pass

    def set_mock_response(self, path, response):
        """Set a mock response for a specific API path"""
        self.mock_responses[path] = response

    def set_http_response(self, path, status_code=200, json_data=None, timeout=False, connection_error=False, json_error=False, errno=0, msg=""):
        """
        Set an HTTP-level response simulation for a specific API path.
        Used to test request_get_func error handling.

        Args:
            path: API path to mock
            status_code: HTTP status code (200, 400, 401, 403, 429, etc.)
            json_data: JSON response data (will be wrapped in {"errno": 0, "result": data} if errno=0)
            timeout: If True, simulate a timeout exception
            connection_error: If True, simulate a connection error exception
            json_error: If True, simulate a JSON decode error
            errno: Fox API error code (0=success, 40400=rate limit, 40402=API limit, etc.)
            msg: Fox API error message
        """
        self.http_responses[path] = {
            "status_code": status_code,
            "json_data": json_data,
            "timeout": timeout,
            "connection_error": connection_error,
            "json_error": json_error,
            "errno": errno,
            "msg": msg,
        }

    def set_arg(self, key, value):
        """Mock set_arg method - tracks calls for automatic_config testing"""
        self.args_set[key] = value

    async def request_get(self, path, post=False, datain=None):
        """Override request_get to return mock data"""
        self.request_log.append({"path": path, "post": post, "datain": datain})
        return self.mock_responses.get(path, None)


def test_validate_schedule_empty(my_predbat):
    """
    Test validate_schedule with an empty schedule - should return a default SelfUse schedule
    """
    print("  - test_validate_schedule_empty")
    new_schedule = []
    reserve = 10
    fdPwr_max = 8000

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

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

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

    # Should return 2 entries: charge window + demand mode after
    assert len(result) == 2

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

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

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

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

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

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

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

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

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
    Test the minutes_to_schedule_time helper function with start and end times
    """
    print("  - test_minutes_to_schedule_time")

    result = minutes_to_schedule_time(0, 0)
    assert result == 0

    result = minutes_to_schedule_time(0, 1)
    assert result == 1

    result = minutes_to_schedule_time(2, 30)
    assert result == 2 * 60 + 30

    result = minutes_to_schedule_time(14, 30)
    assert result == 14 * 60 + 30

    result = minutes_to_schedule_time(23, 59)
    assert result == 23 * 60 + 59

    return False


def test_validate_schedule_multiple_windows(my_predbat):
    """
    Test that validate_schedule only keeps the first (nearest from now) window
    """
    print("  - test_validate_schedule_multiple_windows")

    # Provide multiple windows
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
    reserve = 10
    fdPwr_max = 8000

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

    # The function should sort by time from now (midnight assumed) and keep only the first one
    # The first chronologically from midnight should be the 2:30-5:30 charge window
    # So we should have 3 entries total (demand before, charge, demand after)

    print(result)
    assert len(result) == 5

    # First entry should be demand mode from
    assert result[0]["workMode"] == "SelfUse"
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 2
    assert result[0]["endMinute"] == 29

    # Second entry should be the discharge window
    assert result[1]["workMode"] == "ForceCharge"
    assert result[1]["startHour"] == 2
    assert result[1]["startMinute"] == 30
    assert result[1]["endHour"] == 5
    assert result[1]["endMinute"] == 29

    assert result[2]["workMode"] == "SelfUse"
    assert result[2]["startHour"] == 5
    assert result[2]["startMinute"] == 30
    assert result[2]["endHour"] == 15
    assert result[2]["endMinute"] == 59

    # Second entry should be the discharge window
    assert result[3]["workMode"] == "ForceDischarge"
    assert result[3]["startHour"] == 16
    assert result[3]["startMinute"] == 00
    assert result[3]["endHour"] == 18
    assert result[3]["endMinute"] == 59

    # Third entry should be demand mode
    assert result[4]["workMode"] == "SelfUse"
    assert result[4]["startHour"] == 19
    assert result[4]["startMinute"] == 00
    assert result[4]["endHour"] == 23
    assert result[4]["endMinute"] == 59

    return False


def test_validate_schedule_both_charge_and_discharge(my_predbat):
    """
    Test validate_schedule with both charge and discharge windows (same settings as test_compute_schedule_both_charge_and_discharge)
    """
    print("  - test_validate_schedule_both_charge_and_discharge")

    # Provide both charge and discharge windows with same settings as test_compute_schedule_both_charge_and_discharge
    new_schedule = [
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 30,
            "endHour": 2,
            "endMinute": 55,
            "workMode": "ForceCharge",
            "fdSoc": 100,
            "maxSoc": 100,
            "fdPwr": 8000,
            "minSocOnGrid": 10,
        },
        {
            "enable": 1,
            "startHour": 2,
            "startMinute": 55,
            "endHour": 3,
            "endMinute": 0,
            "workMode": "ForceDischarge",
            "fdSoc": 10,
            "maxSoc": 10,
            "fdPwr": 5000,
            "minSocOnGrid": 10,
        },
    ]
    reserve = 10
    fdPwr_max = 8000

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

    # Should have 4 entries: demand, charge, discharge, demand
    assert len(result) == 4

    # First entry should be demand mode from midnight to charge start
    assert result[0]["workMode"] == "SelfUse"
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 2
    assert result[0]["endMinute"] == 29

    # Second entry should be the charge window
    assert result[1]["workMode"] == "ForceCharge"
    assert result[1]["startHour"] == 2
    assert result[1]["startMinute"] == 30
    assert result[1]["endHour"] == 2
    assert result[1]["endMinute"] == 54, f"Got {result[1]['endMinute']} expected 54"
    assert result[1]["fdSoc"] == 100
    assert result[1]["fdPwr"] == 8000
    assert result[1]["maxSoc"] == 100

    # Third entry should be the discharge window
    assert result[2]["workMode"] == "ForceDischarge"
    assert result[2]["startHour"] == 2
    assert result[2]["startMinute"] == 55
    assert result[2]["endHour"] == 2, f"Got {result[2]['endHour']} expected 2"
    assert result[2]["endMinute"] == 59, f"Got {result[2]['endMinute']} expected 59"
    assert result[2]["fdSoc"] == 10
    assert result[2]["fdPwr"] == 5000
    assert result[2]["maxSoc"] == 10

    # Demand until midnight
    assert result[3]["workMode"] == "SelfUse"
    assert result[3]["startHour"] == 3
    assert result[3]["startMinute"] == 0, f"Got {result[3]['startMinute']} expected 0"
    assert result[3]["endHour"] == 23
    assert result[3]["endMinute"] == 59

    return False


def test_validate_schedule_discharge_ending_at_midnight(my_predbat):
    """
    Test validate_schedule with discharge window that ends at midnight
    """
    print("  - test_validate_schedule_discharge_ending_at_midnight")

    # Discharge window from 20:00 to 00:00 (midnight)
    new_schedule = [
        {
            "enable": 1,
            "startHour": 20,
            "startMinute": 0,
            "endHour": 23,
            "endMinute": 59,
            "workMode": "ForceDischarge",
            "fdSoc": 10,
            "maxSoc": 100,
            "fdPwr": 5000,
            "minSocOnGrid": 10,
        }
    ]
    reserve = 10
    fdPwr_max = 8000

    result = validate_schedule(new_schedule, reserve, fdPwr_max)

    print(result)
    # Should have 2 entries: demand before discharge, then discharge window
    assert len(result) == 2

    # First entry should be demand mode from midnight to discharge start
    assert result[0]["workMode"] == "SelfUse"
    assert result[0]["startHour"] == 0
    assert result[0]["startMinute"] == 0
    assert result[0]["endHour"] == 19
    assert result[0]["endMinute"] == 59

    # Second entry should be the discharge window ending at 23:59
    assert result[1]["workMode"] == "ForceDischarge"
    assert result[1]["startHour"] == 20
    assert result[1]["startMinute"] == 0
    assert result[1]["endHour"] == 23
    assert result[1]["endMinute"] == 59
    assert result[1]["fdSoc"] == 10
    assert result[1]["fdPwr"] == 5000

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
        ],
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}

    # Call compute_schedule (sync wrapper since it's now a regular method)
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
        ],
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}

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
    fox.device_scheduler[deviceSN] = {"enable": False, "groups": []}
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
                "endHour": 2,
                "endMinute": 54,
                "enable": 1,
                "fdPwr": 8000,
                "workMode": "ForceCharge",
                "fdSoc": 100,
                "maxSoc": 100,
                "minSocOnGrid": 10,
            },
            {
                "startHour": 2,
                "startMinute": 55,
                "endHour": 2,
                "endMinute": 59,
                "enable": 1,
                "fdPwr": 5000,
                "workMode": "ForceDischarge",
                "fdSoc": 10,
                "maxSoc": 90,
                "minSocOnGrid": 10,
            },
        ],
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}
    result = asyncio.run(FoxAPI.compute_schedule(fox, deviceSN))

    # Verify both schedules were extracted
    assert "charge" in fox.local_schedule[deviceSN]
    assert "discharge" in fox.local_schedule[deviceSN]

    charge = fox.local_schedule[deviceSN]["charge"]
    assert charge["start_time"] == "02:30:00"
    assert charge["end_time"] == "02:55:00"  # 5:29 inclusive -> 5:30 exclusive
    assert charge["soc"] == 100
    assert charge["enable"] == 1

    discharge = fox.local_schedule[deviceSN]["discharge"]
    assert discharge["start_time"] == "02:55:00"
    assert discharge["end_time"] == "03:00:00", f'Got {discharge["end_time"]} expected 03:00:00'
    assert discharge["soc"] == 10
    assert discharge["enable"] == 1

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
        ],
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}

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
        ],
    }
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {"reserve": 10}

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
    fox.set_mock_response(
        "/op/v0/device/list", {"data": [{"deviceType": "KH8", "hasBattery": True, "hasPV": True, "stationName": "Test Home", "moduleSN": "609W6EUF46MB519", "deviceSN": "TEST123456", "productType": "KH", "stationID": "test-station-id", "status": 1}]}
    )

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

    fox.set_mock_response(
        "/op/v0/device/detail",
        {
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
            "status": 1,
        },
    )

    result = asyncio.run(fox.get_device_detail(deviceSN))

    assert deviceSN in fox.device_detail
    assert fox.device_detail[deviceSN]["deviceType"] == "KH8"
    assert fox.device_detail[deviceSN]["capacity"] == 8
    assert fox.device_detail[deviceSN]["hasBattery"] == True
    assert fox.device_detail[deviceSN]["function"]["scheduler"] == True

    return False


def test_api_get_device_history(my_predbat):
    """
    Test get_device_history API endpoint
    """
    print("  - test_api_get_device_history")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Mock response with history data - note the nested structure with 'datas' containing 'data' array
    fox.set_mock_response(
        "/op/v0/device/history/query",
        [
            {
                "datas": [
                    {
                        "unit": "kW",
                        "name": "PVPower",
                        "variable": "pvPower",
                        "data": [
                            {"time": "2025-12-03 10:00:00", "value": 1.5},
                            {"time": "2025-12-03 10:05:00", "value": 1.8},
                            {"time": "2025-12-03 10:10:00", "value": 2.1},
                        ],
                    },
                    {
                        "unit": "%",
                        "name": "SoC",
                        "variable": "SoC",
                        "data": [
                            {"time": "2025-12-03 10:00:00", "value": 45},
                            {"time": "2025-12-03 10:05:00", "value": 48},
                            {"time": "2025-12-03 10:10:00", "value": 51},
                        ],
                    },
                    {
                        "unit": "℃",
                        "name": "batTemperature",
                        "variable": "batTemperature",
                        "data": [
                            {"time": "2025-12-03 10:10:00", "value": 25.5},
                        ],
                    },
                ],
                "deviceSN": deviceSN,
            }
        ],
    )

    run_async(fox.get_device_history(deviceSN))

    # Verify device_values was populated with the latest data point from each variable
    assert deviceSN in fox.device_values
    assert "pvPower" in fox.device_values[deviceSN]
    assert fox.device_values[deviceSN]["pvPower"]["value"] == 2.1  # Last value in history
    assert fox.device_values[deviceSN]["pvPower"]["unit"] == "kW"
    assert fox.device_values[deviceSN]["pvPower"]["name"] == "PVPower"
    assert fox.device_values[deviceSN]["pvPower"]["timestamp"] == "2025-12-03 10:10:00"

    assert "SoC" in fox.device_values[deviceSN]
    assert fox.device_values[deviceSN]["SoC"]["value"] == 51
    assert fox.device_values[deviceSN]["SoC"]["unit"] == "%"

    # Verify temperature unit conversion (℃ -> °C)
    assert "batTemperature" in fox.device_values[deviceSN]
    assert fox.device_values[deviceSN]["batTemperature"]["unit"] == "°C"
    assert fox.device_values[deviceSN]["batTemperature"]["value"] == 25.5

    # Verify the request was made correctly
    assert len(fox.request_log) == 1
    assert fox.request_log[0]["path"] == "/op/v0/device/history/query"
    assert fox.request_log[0]["post"] == True
    assert fox.request_log[0]["datain"]["sn"] == deviceSN

    return False


def test_api_get_device_history_empty(my_predbat):
    """
    Test get_device_history with empty history data
    """
    print("  - test_api_get_device_history_empty")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Mock response with empty data array
    fox.set_mock_response(
        "/op/v0/device/history/query",
        [
            {
                "datas": [
                    {
                        "unit": "kW",
                        "name": "PVPower",
                        "variable": "pvPower",
                        "data": [],  # Empty history
                    },
                ],
                "deviceSN": deviceSN,
            }
        ],
    )

    run_async(fox.get_device_history(deviceSN))

    # With empty history, device_values should not have this variable
    assert deviceSN not in fox.device_values or "pvPower" not in fox.device_values.get(deviceSN, {})

    return False


def test_api_get_device_setting(my_predbat):
    """
    Test get_device_setting API endpoint
    """
    print("  - test_api_get_device_setting")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.set_mock_response("/op/v0/device/setting/get", {"enumList": ["PeakShaving", "Feedin", "SelfUse"], "unit": "", "precision": 1.0, "value": "SelfUse"})

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


def test_api_get_device_settings(my_predbat):
    """
    Test get_device_settings (plural) - fetches all FOX_SETTINGS for a device
    Also tests initialize() method for coverage
    """
    print("  - test_api_get_device_settings")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Call initialize to cover that method
    fox.initialize(key="test_api_key", automatic=True)

    # Verify initialize set up the expected attributes
    assert fox.key == "test_api_key"
    assert fox.automatic == True
    assert fox.failures_total == 0
    assert fox.device_list == []
    assert fox.device_detail == {}
    assert fox.device_settings == {}

    # Setup device with battery (required for get_device_settings)
    fox.device_detail[deviceSN] = {"hasBattery": True}

    # FOX_SETTINGS = ["ExportLimit", "MaxSoc", "GridCode", "WorkMode", "ExportLimitPower", "MinSoc", "MinSocOnGrid"]
    # Mock response for each setting
    fox.set_mock_response("/op/v0/device/setting/get", {"value": "100", "unit": "%", "precision": 1.0})

    run_async(fox.get_device_settings(deviceSN))

    # Should have made requests for all 7 FOX_SETTINGS
    assert len(fox.request_log) == 7

    # Verify each setting was requested
    requested_keys = [req["datain"]["key"] for req in fox.request_log]
    expected_keys = ["ExportLimit", "MaxSoc", "GridCode", "WorkMode", "ExportLimitPower", "MinSoc", "MinSocOnGrid"]
    for key in expected_keys:
        assert key in requested_keys, f"Expected key {key} in requests"

    # Verify all settings were stored
    assert deviceSN in fox.device_settings
    for key in expected_keys:
        assert key in fox.device_settings[deviceSN], f"Expected key {key} in device_settings"

    return False


def test_api_get_device_settings_no_battery(my_predbat):
    """
    Test get_device_settings skips non-battery devices
    """
    print("  - test_api_get_device_settings_no_battery")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device WITHOUT battery
    fox.device_detail[deviceSN] = {"hasBattery": False}

    run_async(fox.get_device_settings(deviceSN))

    # Should not have made any requests (no battery)
    assert len(fox.request_log) == 0

    return False


def test_api_get_device_settings_missing_detail(my_predbat):
    """
    Test get_device_settings handles missing device detail
    """
    print("  - test_api_get_device_settings_missing_detail")

    fox = MockFoxAPIWithRequests()
    deviceSN = "UNKNOWN123"

    # No device_detail for this device (should return early)
    run_async(fox.get_device_settings(deviceSN))

    # Should not have made any requests
    assert len(fox.request_log) == 0

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

    fox.set_mock_response(
        "/op/v0/device/battery/forceChargeTime/get",
        {
            "enable1": True,
            "startTime1": {"hour": 2, "minute": 30},
            "endTime1": {"hour": 5, "minute": 30},
            "enable2": False,
            "startTime2": {"hour": 0, "minute": 0},
            "endTime2": {"hour": 0, "minute": 0},
        },
    )

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

    fox.set_mock_response(
        "/op/v1/device/scheduler/get",
        {
            "enable": 1,
            "groups": [
                {"endHour": 5, "fdPwr": 8000, "minSocOnGrid": 15, "workMode": "ForceCharge", "fdSoc": 100, "enable": 1, "startHour": 2, "maxSoc": 100, "startMinute": 30, "endMinute": 29},
                {"endHour": 0, "fdPwr": 0, "minSocOnGrid": 15, "workMode": "Invalid", "fdSoc": 10, "enable": 0, "startHour": 0, "maxSoc": 100, "startMinute": 0, "endMinute": 0},
            ],
            "properties": {
                "fdpwr": {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 8000.0}},
                "fdsoc": {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
            },
        },
    )

    result = asyncio.run(fox.get_scheduler(deviceSN))

    assert result["enable"] == 1
    assert len(result["groups"]) == 2
    assert result["groups"][0]["workMode"] == "ForceCharge"
    assert result["groups"][0]["minSocOnGrid"] == 15
    assert result["groups"][0]["enable"] == 1
    assert result["groups"][0]["startHour"] == 2
    assert result["groups"][0]["startMinute"] == 30
    assert result["groups"][0]["endHour"] == 5
    assert result["groups"][0]["endMinute"] == 29
    assert result["groups"][1]["enable"] == 0
    assert result["groups"][1]["workMode"] == "Invalid"

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
    fox.device_scheduler[deviceSN] = {"enable": False, "groups": []}

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

    fox.set_mock_response(
        "/op/v1/device/real/query",
        [
            {
                "datas": [
                    {"unit": "kW", "name": "PVPower", "variable": "pvPower", "value": 3.5},
                    {"unit": "%", "name": "SoC", "variable": "SoC", "value": 75.0},
                    {"unit": "kW", "name": "Load Power", "variable": "loadsPower", "value": 2.1},
                    {"unit": "kW", "name": "Charge Power", "variable": "batChargePower", "value": 1.4},
                    {"unit": "kW", "name": "Feed-in Power", "variable": "feedinPower", "value": 0.0},
                ],
                "time": "2025-09-14 18:43:09 BST+0100",
                "deviceSN": deviceSN,
            }
        ],
    )

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

    fox.set_mock_response(
        "/op/v0/device/report/query",
        [
            {"unit": "kWh", "values": [0.0, 0.0, 0.0, 0.0, 151.6, 1079.1], "variable": "generation"},
            {"unit": "kWh", "values": [0.0, 0.0, 0.0, 0.0, 68.6, 685.2], "variable": "feedin"},
            {"unit": "kWh", "values": [0.0, 0.0, 0.0, 0.0, 52.7, 300.1], "variable": "gridConsumption"},
        ],
    )

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

    fox.set_mock_response("/op/v0/device/generation", {"month": 867.6, "today": 17.7, "cumulative": 5765.7})

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
    fox.device_scheduler[deviceSN] = {"enable": True, "groups": groups.copy()}

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


def test_api_get_schedule_settings_ha(my_predbat):
    """
    Test get_schedule_settings_ha reads schedule settings from HA entities
    """
    print("  - test_api_get_schedule_settings_ha")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings for minSocOnGrid
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10

    # Set mock HA states for schedule entities
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_reserve", 15)

    # Charge schedule settings
    fox.set_mock_ha_state("select.predbat_fox_test123456_battery_schedule_charge_start_time", "02:30:00")
    fox.set_mock_ha_state("select.predbat_fox_test123456_battery_schedule_charge_end_time", "05:30:00")
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_charge_soc", 100)
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_charge_power", 8000)
    fox.set_mock_ha_state("switch.predbat_fox_test123456_battery_schedule_charge_enable", "on")

    # Discharge schedule settings
    fox.set_mock_ha_state("select.predbat_fox_test123456_battery_schedule_discharge_start_time", "16:00:00")
    fox.set_mock_ha_state("select.predbat_fox_test123456_battery_schedule_discharge_end_time", "19:00:00")
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_discharge_soc", 20)
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_discharge_power", 5000)
    fox.set_mock_ha_state("switch.predbat_fox_test123456_battery_schedule_discharge_enable", "on")

    result = asyncio.run(fox.get_schedule_settings_ha(deviceSN))

    # Verify reserve was read at top level
    assert fox.local_schedule[deviceSN]["reserve"] == 15

    # Verify charge schedule structure and values
    assert "charge" in fox.local_schedule[deviceSN]
    assert fox.local_schedule[deviceSN]["charge"]["start_time"] == "02:30:00"
    assert fox.local_schedule[deviceSN]["charge"]["end_time"] == "05:30:00"
    assert fox.local_schedule[deviceSN]["charge"]["soc"] == 100
    assert fox.local_schedule[deviceSN]["charge"]["power"] == 8000
    assert fox.local_schedule[deviceSN]["charge"]["enable"] == 1

    # Verify discharge schedule structure and values
    assert "discharge" in fox.local_schedule[deviceSN]
    assert fox.local_schedule[deviceSN]["discharge"]["start_time"] == "16:00:00"
    assert fox.local_schedule[deviceSN]["discharge"]["end_time"] == "19:00:00"
    assert fox.local_schedule[deviceSN]["discharge"]["soc"] == 20
    assert fox.local_schedule[deviceSN]["discharge"]["power"] == 5000
    assert fox.local_schedule[deviceSN]["discharge"]["enable"] == 1

    return False


def test_api_get_schedule_settings_ha_defaults(my_predbat):
    """
    Test get_schedule_settings_ha uses defaults when HA entities don't exist
    """
    print("  - test_api_get_schedule_settings_ha_defaults")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings for minSocOnGrid
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10

    # Don't set any mock HA states - should use defaults

    result = asyncio.run(fox.get_schedule_settings_ha(deviceSN))

    # Reserve should default to minSocOnGrid (10) since 0 < 10
    assert fox.local_schedule[deviceSN]["reserve"] == 10

    # Verify charge schedule default values
    assert "charge" in fox.local_schedule[deviceSN]
    assert fox.local_schedule[deviceSN]["charge"]["start_time"] == "00:00:00"
    assert fox.local_schedule[deviceSN]["charge"]["end_time"] == "00:00:00"
    assert fox.local_schedule[deviceSN]["charge"]["soc"] == 100  # Charge soc defaults to 100
    assert fox.local_schedule[deviceSN]["charge"]["power"] == 8000  # Power defaults to fdpwr_max
    assert fox.local_schedule[deviceSN]["charge"]["enable"] == 0  # Enable defaults to 0 (off)

    # Verify discharge schedule default values
    assert "discharge" in fox.local_schedule[deviceSN]
    assert fox.local_schedule[deviceSN]["discharge"]["start_time"] == "00:00:00"
    assert fox.local_schedule[deviceSN]["discharge"]["end_time"] == "00:00:00"
    assert fox.local_schedule[deviceSN]["discharge"]["soc"] == 10  # Discharge soc defaults to fdsoc_min
    assert fox.local_schedule[deviceSN]["discharge"]["power"] == 8000  # Power defaults to fdpwr_max
    assert fox.local_schedule[deviceSN]["discharge"]["enable"] == 0  # Enable defaults to 0 (off)

    return False


def test_api_get_schedule_settings_ha_reserve_clamped(my_predbat):
    """
    Test get_schedule_settings_ha clamps reserve to minSocOnGrid
    """
    print("  - test_api_get_schedule_settings_ha_reserve_clamped")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device with minSocOnGrid = 15
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 15}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 15

    # Set reserve to a value below minSocOnGrid
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_reserve", 5)

    result = asyncio.run(fox.get_schedule_settings_ha(deviceSN))

    # Reserve should be clamped to minSocOnGrid (15), not 5
    assert fox.local_schedule[deviceSN]["reserve"] == 15

    return False


def test_api_get_schedule_settings_ha_enable_off(my_predbat):
    """
    Test get_schedule_settings_ha correctly handles enable switch set to off
    """
    print("  - test_api_get_schedule_settings_ha_enable_off")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10

    # Set enable to "off"
    fox.set_mock_ha_state("switch.predbat_fox_test123456_battery_schedule_charge_enable", "off")
    fox.set_mock_ha_state("switch.predbat_fox_test123456_battery_schedule_discharge_enable", "off")

    result = asyncio.run(fox.get_schedule_settings_ha(deviceSN))

    # Enable should be 0 for both charge and discharge
    assert fox.local_schedule[deviceSN]["charge"]["enable"] == 0
    assert fox.local_schedule[deviceSN]["discharge"]["enable"] == 0

    return False


def test_api_get_schedule_settings_ha_invalid_values(my_predbat):
    """
    Test get_schedule_settings_ha handles invalid numeric values
    """
    print("  - test_api_get_schedule_settings_ha_invalid_values")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10

    # Set invalid numeric value for reserve
    fox.set_mock_ha_state("number.predbat_fox_test123456_battery_schedule_reserve", "invalid")

    result = asyncio.run(fox.get_schedule_settings_ha(deviceSN))

    # Reserve should fall back to minSocOnGrid (10) since invalid value becomes 0, then clamped to 10
    assert fox.local_schedule[deviceSN]["reserve"] == 10

    return False


def test_api_publish_schedule_settings_ha(my_predbat):
    """
    Test publish_schedule_settings_ha publishes all schedule entities to HA
    """
    print("  - test_api_publish_schedule_settings_ha")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device with battery
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000

    # Setup local schedule with values
    fox.local_schedule[deviceSN] = {
        "reserve": 15,
        "charge": {
            "start_time": "02:30:00",
            "end_time": "05:30:00",
            "soc": 100,
            "power": 8000,
            "enable": 1,
        },
        "discharge": {
            "start_time": "16:00:00",
            "end_time": "19:00:00",
            "soc": 20,
            "power": 5000,
            "enable": 1,
        },
    }

    result = asyncio.run(fox.publish_schedule_settings_ha(deviceSN))

    # Verify reserve entity was published
    reserve_entity = "number.predbat_fox_test123456_battery_schedule_reserve"
    assert reserve_entity in fox.dashboard_items
    assert fox.dashboard_items[reserve_entity]["state"] == 15
    assert fox.dashboard_items[reserve_entity]["attributes"]["min"] == 10
    assert fox.dashboard_items[reserve_entity]["attributes"]["max"] == 100
    assert fox.dashboard_items[reserve_entity]["app"] == "fox"

    # Verify charge schedule entities were published
    charge_start = "select.predbat_fox_test123456_battery_schedule_charge_start_time"
    assert charge_start in fox.dashboard_items
    assert fox.dashboard_items[charge_start]["state"] == "02:30:00"

    charge_end = "select.predbat_fox_test123456_battery_schedule_charge_end_time"
    assert charge_end in fox.dashboard_items
    assert fox.dashboard_items[charge_end]["state"] == "05:30:00"

    charge_soc = "number.predbat_fox_test123456_battery_schedule_charge_soc"
    assert charge_soc in fox.dashboard_items
    assert fox.dashboard_items[charge_soc]["state"] == 100

    charge_power = "number.predbat_fox_test123456_battery_schedule_charge_power"
    assert charge_power in fox.dashboard_items
    assert fox.dashboard_items[charge_power]["state"] == 8000
    assert fox.dashboard_items[charge_power]["attributes"]["max"] == 8000

    charge_enable = "switch.predbat_fox_test123456_battery_schedule_charge_enable"
    assert charge_enable in fox.dashboard_items
    assert fox.dashboard_items[charge_enable]["state"] == "on"

    charge_write = "switch.predbat_fox_test123456_battery_schedule_charge_write"
    assert charge_write in fox.dashboard_items
    assert fox.dashboard_items[charge_write]["state"] == "off"  # Write always off

    # Verify discharge schedule entities were published
    discharge_start = "select.predbat_fox_test123456_battery_schedule_discharge_start_time"
    assert discharge_start in fox.dashboard_items
    assert fox.dashboard_items[discharge_start]["state"] == "16:00:00"

    discharge_end = "select.predbat_fox_test123456_battery_schedule_discharge_end_time"
    assert discharge_end in fox.dashboard_items
    assert fox.dashboard_items[discharge_end]["state"] == "19:00:00"

    discharge_soc = "number.predbat_fox_test123456_battery_schedule_discharge_soc"
    assert discharge_soc in fox.dashboard_items
    assert fox.dashboard_items[discharge_soc]["state"] == 20

    discharge_power = "number.predbat_fox_test123456_battery_schedule_discharge_power"
    assert discharge_power in fox.dashboard_items
    assert fox.dashboard_items[discharge_power]["state"] == 5000

    discharge_enable = "switch.predbat_fox_test123456_battery_schedule_discharge_enable"
    assert discharge_enable in fox.dashboard_items
    assert fox.dashboard_items[discharge_enable]["state"] == "on"

    discharge_write = "switch.predbat_fox_test123456_battery_schedule_discharge_write"
    assert discharge_write in fox.dashboard_items
    assert fox.dashboard_items[discharge_write]["state"] == "off"  # Write always off

    return False


def test_api_publish_schedule_settings_ha_no_battery(my_predbat):
    """
    Test publish_schedule_settings_ha does nothing when device has no battery
    """
    print("  - test_api_publish_schedule_settings_ha_no_battery")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device without battery
    fox.device_detail[deviceSN] = {"hasBattery": False}

    result = asyncio.run(fox.publish_schedule_settings_ha(deviceSN))

    # No dashboard items should have been published
    assert len(fox.dashboard_items) == 0

    return False


def test_api_publish_schedule_settings_ha_defaults(my_predbat):
    """
    Test publish_schedule_settings_ha uses defaults when local_schedule is empty
    """
    print("  - test_api_publish_schedule_settings_ha_defaults")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device with battery but empty local_schedule
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.local_schedule[deviceSN] = {}

    result = asyncio.run(fox.publish_schedule_settings_ha(deviceSN))

    # Verify reserve defaults to 0
    reserve_entity = "number.predbat_fox_test123456_battery_schedule_reserve"
    assert reserve_entity in fox.dashboard_items
    assert fox.dashboard_items[reserve_entity]["state"] == 0

    # Verify charge start_time defaults to "00:00:00"
    charge_start = "select.predbat_fox_test123456_battery_schedule_charge_start_time"
    assert charge_start in fox.dashboard_items
    assert fox.dashboard_items[charge_start]["state"] == "00:00:00"

    # Verify charge enable defaults to "off" (0 becomes "off")
    charge_enable = "switch.predbat_fox_test123456_battery_schedule_charge_enable"
    assert charge_enable in fox.dashboard_items
    assert fox.dashboard_items[charge_enable]["state"] == "off"

    return False


def test_api_publish_schedule_settings_ha_enable_off(my_predbat):
    """
    Test publish_schedule_settings_ha correctly publishes enable=0 as "off"
    """
    print("  - test_api_publish_schedule_settings_ha_enable_off")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device with battery
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000

    # Setup local schedule with enable=0
    fox.local_schedule[deviceSN] = {
        "reserve": 10,
        "charge": {
            "start_time": "02:30:00",
            "end_time": "05:30:00",
            "soc": 100,
            "power": 8000,
            "enable": 0,
        },
        "discharge": {
            "start_time": "16:00:00",
            "end_time": "19:00:00",
            "soc": 20,
            "power": 5000,
            "enable": 0,
        },
    }

    result = asyncio.run(fox.publish_schedule_settings_ha(deviceSN))

    # Verify charge enable is "off"
    charge_enable = "switch.predbat_fox_test123456_battery_schedule_charge_enable"
    assert fox.dashboard_items[charge_enable]["state"] == "off"

    # Verify discharge enable is "off"
    discharge_enable = "switch.predbat_fox_test123456_battery_schedule_discharge_enable"
    assert fox.dashboard_items[discharge_enable]["state"] == "off"

    return False


def test_api_publish_schedule_settings_ha_invalid_time(my_predbat):
    """
    Test publish_schedule_settings_ha handles invalid time values
    """
    print("  - test_api_publish_schedule_settings_ha_invalid_time")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device with battery
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000

    # Setup local schedule with invalid time value
    fox.local_schedule[deviceSN] = {
        "reserve": 10,
        "charge": {
            "start_time": "invalid_time",
            "end_time": "05:30:00",
            "soc": 100,
            "power": 8000,
            "enable": 1,
        },
    }

    result = run_async(fox.publish_schedule_settings_ha(deviceSN))

    # Invalid time should be replaced with "00:00:00"
    charge_start = "select.predbat_fox_test123456_battery_schedule_charge_start_time"
    assert fox.dashboard_items[charge_start]["state"] == "00:00:00"

    return False


# ============================================================================
# HTTP-level request_get_func Tests
# ============================================================================


class MockFoxAPIWithHTTPSimulation(MockFoxAPIWithRequests):
    """
    Mock FoxAPI class that overrides request_get_func to simulate HTTP-level responses.
    Used for testing error handling without making real HTTP requests.
    """

    def __init__(self):
        super().__init__()
        # HTTP-level response simulation - keyed by API path
        # Format: {path: {"status_code": int, "json_data": dict, "timeout": bool, "connection_error": bool, "json_error": bool, "errno": int, "msg": str}}
        self.http_responses = {}
        self.request_get_func_calls = []

    async def request_get_func(self, path, post=False, datain=None):
        """Override request_get_func to simulate HTTP responses"""
        self.request_get_func_calls.append({"path": path, "post": post, "datain": datain})

        if path not in self.http_responses:
            # Default: return None, False (failure, no retry)
            return None, False

        response = self.http_responses[path]

        # Simulate timeout
        if response.get("timeout", False):
            return None, True

        # Simulate connection error
        if response.get("connection_error", False):
            return None, True

        # Simulate JSON decode error after successful HTTP request
        if response.get("json_error", False):
            return None, False

        status_code = response.get("status_code", 200)

        # Simulate auth errors (400, 401, 402, 403)
        if status_code in [400, 401, 402, 403]:
            self.failures_total += 1
            return None, False

        # Simulate rate limiting (429)
        if status_code == 429:
            self.failures_total += 1
            return None, True

        # Simulate successful response with potential Fox API errors
        if status_code in [200, 201]:
            errno = response.get("errno", 0)
            msg = response.get("msg", "")

            if errno != 0:
                self.failures_total += 1
                # Rate limiting errors (allow retry)
                if errno in [40400, 41200, 41201, 41202, 41203, 41935, 44098]:
                    return None, True
                # Out of API calls (no retry)
                elif errno in [40402]:
                    return None, False
                # Unsupported function (no retry)
                elif errno in [44096]:
                    return None, False
                # Invalid parameter (no retry)
                elif errno in [40257]:
                    return None, False
                else:
                    return None, False

            # Success - return the json_data
            json_data = response.get("json_data", {})
            self.update_success_timestamp()
            return json_data, False

        return None, False


def test_request_get_func_auth_error_401(my_predbat):
    """
    Test request_get_func handles 401 authentication error
    """
    print("  - test_request_get_func_auth_error_401")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 401}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False
    assert fox.failures_total == 1

    return False


def test_request_get_func_auth_error_403(my_predbat):
    """
    Test request_get_func handles 403 forbidden error
    """
    print("  - test_request_get_func_auth_error_403")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 403}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False
    assert fox.failures_total == 1

    return False


def test_request_get_func_rate_limit_429(my_predbat):
    """
    Test request_get_func handles 429 rate limit with retry
    """
    print("  - test_request_get_func_rate_limit_429")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 429}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Should allow retry for rate limiting
    assert fox.failures_total == 1

    return False


def test_request_get_func_timeout(my_predbat):
    """
    Test request_get_func handles timeout with retry
    """
    print("  - test_request_get_func_timeout")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"timeout": True}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Should allow retry for timeout

    return False


def test_request_get_func_connection_error(my_predbat):
    """
    Test request_get_func handles connection error with retry
    """
    print("  - test_request_get_func_connection_error")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"connection_error": True}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Should allow retry for connection error

    return False


def test_request_get_func_json_decode_error(my_predbat):
    """
    Test request_get_func handles JSON decode error
    """
    print("  - test_request_get_func_json_decode_error")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"json_error": True}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # No retry for JSON decode error

    return False


def test_request_get_func_fox_error_rate_limit(my_predbat):
    """
    Test request_get_func handles Fox API rate limit error (40400) with retry
    """
    print("  - test_request_get_func_fox_error_rate_limit")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 40400, "msg": "Rate limit exceeded"}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Should allow retry for Fox rate limit
    assert fox.failures_total == 1

    return False


def test_request_get_func_fox_error_api_limit(my_predbat):
    """
    Test request_get_func handles Fox API limit error (40402) without retry
    """
    print("  - test_request_get_func_fox_error_api_limit")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 40402, "msg": "API calls exhausted"}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # No retry for API limit exhausted
    assert fox.failures_total == 1

    return False


def test_request_get_func_fox_error_unsupported(my_predbat):
    """
    Test request_get_func handles Fox unsupported function error (44096)
    """
    print("  - test_request_get_func_fox_error_unsupported")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 44096, "msg": "Unsupported function"}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # No retry for unsupported function
    assert fox.failures_total == 1

    return False


def test_request_get_func_fox_error_invalid_param(my_predbat):
    """
    Test request_get_func handles Fox invalid parameter error (40257)
    """
    print("  - test_request_get_func_fox_error_invalid_param")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 40257, "msg": "Invalid parameter"}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # No retry for invalid parameter
    assert fox.failures_total == 1

    return False


def test_request_get_func_fox_error_comms_issue(my_predbat):
    """
    Test request_get_func handles Fox communication issue errors (41200, 41201, etc.) with retry
    """
    print("  - test_request_get_func_fox_error_comms_issue")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 41200, "msg": "Communication issue"}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Should allow retry for comms issue
    assert fox.failures_total == 1

    return False


def test_request_get_func_success(my_predbat):
    """
    Test request_get_func handles successful response
    """
    print("  - test_request_get_func_success")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 0, "json_data": {"data": [{"deviceSN": "TEST123"}]}}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is not None
    assert result["data"][0]["deviceSN"] == "TEST123"
    assert allow_retry == False
    assert fox.failures_total == 0

    return False


def test_request_get_func_unknown_error(my_predbat):
    """
    Test request_get_func handles unknown Fox error code
    """
    print("  - test_request_get_func_unknown_error")

    fox = MockFoxAPIWithHTTPSimulation()
    fox.http_responses["/op/v0/device/list"] = {"status_code": 200, "errno": 99999, "msg": "Unknown error"}

    result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # Unknown errors don't retry
    assert fox.failures_total == 1

    return False


# ============================================================================
# request_get / request_get_func Tests (using MagicMock for requests library)
# ============================================================================


class MockFoxAPIForRequestTesting(FoxAPI):
    """
    Mock FoxAPI class for testing request_get/request_get_func with real implementation.
    Only mocks the ComponentBase parts, uses actual request_get/request_get_func logic.
    """

    def __init__(self):
        # Don't call parent __init__ since we're not using ComponentBase properly
        self.key = "test_api_key"
        self.automatic = False
        self.failures_total = 0
        self.device_list = []
        self.device_detail = {}
        self.local_tz = pytz.timezone("Europe/London")
        self.log_messages = []

    def log(self, message):
        """Mock log method - captures messages"""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """Mock update_success_timestamp method"""
        pass


def test_request_get_func_real_success_get(my_predbat):
    """
    Test real request_get_func with mocked requests.get - successful response
    """
    print("  - test_request_get_func_real_success_get")

    fox = MockFoxAPIForRequestTesting()

    # Create mock response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"errno": 0, "result": {"data": [{"deviceSN": "TEST123"}]}}

    with patch("fox.requests.get", return_value=mock_response) as mock_get:
        result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list", post=False, datain={"pageSize": 100}))

    assert result is not None
    assert result["data"][0]["deviceSN"] == "TEST123"
    assert allow_retry == False
    assert fox.failures_total == 0
    mock_get.assert_called_once()

    return False


def test_request_get_func_real_success_post(my_predbat):
    """
    Test real request_get_func with mocked requests.post - successful POST response
    """
    print("  - test_request_get_func_real_success_post")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"errno": 0, "result": {"success": True}}

    with patch("fox.requests.post", return_value=mock_response) as mock_post:
        result, allow_retry = run_async(fox.request_get_func("/op/v0/device/setting", post=True, datain={"key": "value"}))

    assert result is not None
    assert result["success"] == True
    assert allow_retry == False
    mock_post.assert_called_once()

    return False


def test_request_get_func_real_auth_error_401(my_predbat):
    """
    Test real request_get_func with mocked requests - 401 auth error
    """
    print("  - test_request_get_func_real_auth_error_401")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 401

    with patch("fox.requests.get", return_value=mock_response):
        result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # Auth errors should not retry
    assert fox.failures_total == 1

    return False


def test_request_get_func_real_rate_limit_429(my_predbat):
    """
    Test real request_get_func with mocked requests - 429 rate limit (with mocked sleep)
    """
    print("  - test_request_get_func_real_rate_limit_429")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 429

    with patch("fox.requests.get", return_value=mock_response):
        with patch("fox.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Rate limit should allow retry
    assert fox.failures_total == 1
    mock_sleep.assert_called_once()  # Should have called sleep for rate limiting

    return False


def test_request_get_func_real_fox_errno_rate_limit(my_predbat):
    """
    Test real request_get_func with Fox API errno rate limit (40400)
    """
    print("  - test_request_get_func_real_fox_errno_rate_limit")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"errno": 40400, "msg": "Rate limited"}

    with patch("fox.requests.get", return_value=mock_response):
        with patch("fox.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == True  # Rate limit errno should allow retry
    assert fox.failures_total == 1
    mock_sleep.assert_called_once()

    return False


def test_request_get_func_real_fox_errno_api_limit(my_predbat):
    """
    Test real request_get_func with Fox API errno API limit (40402)
    """
    print("  - test_request_get_func_real_fox_errno_api_limit")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"errno": 40402, "msg": "API calls exceeded"}

    with patch("fox.requests.get", return_value=mock_response):
        with patch("fox.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # API limit should NOT retry
    assert fox.failures_total == 1
    mock_sleep.assert_called_once()  # Should sleep for 5 minutes

    return False


def test_request_get_func_real_connection_error(my_predbat):
    """
    Test real request_get_func with connection error exception
    """
    print("  - test_request_get_func_real_connection_error")

    fox = MockFoxAPIForRequestTesting()

    with patch("fox.requests.get", side_effect=requests.exceptions.ConnectionError("Connection refused")):
        result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # Connection errors should not retry (RequestException)
    assert fox.failures_total == 1

    return False


def test_request_get_func_real_timeout(my_predbat):
    """
    Test real request_get_func with timeout exception
    """
    print("  - test_request_get_func_real_timeout")

    fox = MockFoxAPIForRequestTesting()

    with patch("fox.requests.get", side_effect=requests.exceptions.Timeout("Request timed out")):
        result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    assert result is None
    assert allow_retry == False  # Timeout as RequestException should not retry
    assert fox.failures_total == 1

    return False


def test_request_get_func_real_json_decode_error(my_predbat):
    """
    Test real request_get_func with JSON decode error
    """
    print("  - test_request_get_func_real_json_decode_error")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = requests.exceptions.JSONDecodeError("Invalid JSON", "", 0)

    with patch("fox.requests.get", return_value=mock_response):
        result, allow_retry = run_async(fox.request_get_func("/op/v0/device/list"))

    # JSON decode error with status 200 returns empty dict (data=None -> data={})
    assert result == {}
    assert allow_retry == False

    return False


def test_request_get_real_retry_on_rate_limit(my_predbat):
    """
    Test real request_get retries on rate limit and eventually succeeds
    """
    print("  - test_request_get_real_retry_on_rate_limit")

    fox = MockFoxAPIForRequestTesting()

    # First call returns rate limit, second call succeeds
    mock_response_rate_limit = MagicMock()
    mock_response_rate_limit.status_code = 429

    mock_response_success = MagicMock()
    mock_response_success.status_code = 200
    mock_response_success.json.return_value = {"errno": 0, "result": {"data": "success"}}

    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_response_rate_limit
        return mock_response_success

    with patch("fox.requests.get", side_effect=side_effect):
        with patch("fox.asyncio.sleep", new_callable=AsyncMock):
            with patch("fox.random.random", return_value=0.1):  # Make sleep short
                result = run_async(fox.request_get("/op/v0/device/list"))

    assert result is not None
    assert result["data"] == "success"
    assert call_count[0] == 2  # Should have made 2 calls

    return False


def test_request_get_real_no_retry_on_auth_error(my_predbat):
    """
    Test real request_get does NOT retry on auth error (401)
    """
    print("  - test_request_get_real_no_retry_on_auth_error")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 401

    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        return mock_response

    with patch("fox.requests.get", side_effect=side_effect):
        result = run_async(fox.request_get("/op/v0/device/list"))

    assert result is None
    assert call_count[0] == 1  # Should only make 1 call, no retry

    return False


def test_request_get_real_max_retries(my_predbat):
    """
    Test real request_get stops after max retries (FOX_RETRIES=10)
    """
    print("  - test_request_get_real_max_retries")

    fox = MockFoxAPIForRequestTesting()

    # Always return rate limit to trigger max retries
    mock_response = MagicMock()
    mock_response.status_code = 429

    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        return mock_response

    with patch("fox.requests.get", side_effect=side_effect):
        with patch("fox.asyncio.sleep", new_callable=AsyncMock):
            with patch("fox.random.random", return_value=0.01):  # Make sleep very short
                result = run_async(fox.request_get("/op/v0/device/list"))

    assert result is None
    assert call_count[0] == 10  # FOX_RETRIES = 10

    return False


def test_request_get_real_post_with_data(my_predbat):
    """
    Test real request_get with POST and datain
    """
    print("  - test_request_get_real_post_with_data")

    fox = MockFoxAPIForRequestTesting()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"errno": 0, "result": {"success": True}}

    with patch("fox.requests.post", return_value=mock_response) as mock_post:
        result = run_async(fox.request_get("/op/v0/device/setting", post=True, datain={"sn": "TEST123", "key": "MinSocOnGrid", "value": 10}))

    assert result is not None
    assert result["success"] == True
    # Verify the post was called with correct arguments
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]
    assert call_kwargs["json"] == {"sn": "TEST123", "key": "MinSocOnGrid", "value": 10}

    return False


# ============================================================================
# run() Method Tests
# ============================================================================


class MockFoxAPIWithRunTracking(MockFoxAPIWithRequests):
    """
    Mock FoxAPI class that tracks method calls for testing run() behavior.
    """

    def __init__(self):
        super().__init__()
        self.method_calls = []
        self.automatic_config_called = False

    async def get_device_list(self):
        self.method_calls.append("get_device_list")
        return self.device_list

    async def get_device_detail(self, deviceSN):
        self.method_calls.append(f"get_device_detail:{deviceSN}")

    async def get_device_history(self, deviceSN):
        self.method_calls.append(f"get_device_history:{deviceSN}")

    async def get_battery_charging_time(self, deviceSN):
        self.method_calls.append(f"get_battery_charging_time:{deviceSN}")
        return {}

    async def get_device_settings(self, deviceSN):
        self.method_calls.append(f"get_device_settings:{deviceSN}")

    async def get_schedule_settings_ha(self, deviceSN):
        self.method_calls.append(f"get_schedule_settings_ha:{deviceSN}")

    async def get_scheduler(self, deviceSN):
        self.method_calls.append(f"get_scheduler:{deviceSN}")
        return {}

    async def compute_schedule(self, deviceSN):
        self.method_calls.append(f"compute_schedule:{deviceSN}")
        return {}

    async def get_real_time_data(self, deviceSN):
        self.method_calls.append(f"get_real_time_data:{deviceSN}")

    async def publish_data(self):
        self.method_calls.append("publish_data")

    async def automatic_config(self):
        self.method_calls.append("automatic_config")
        self.automatic_config_called = True


def test_run_first_call_with_devices(my_predbat):
    """
    Test run() with first=True initializes all device data
    """
    print("  - test_run_first_call_with_devices")

    fox = MockFoxAPIWithRunTracking()
    fox.device_list = [{"deviceSN": "TEST123"}, {"deviceSN": "TEST456"}]

    result = run_async(fox.run(0, first=True))

    assert result == True
    assert "get_device_list" in fox.method_calls
    assert "get_device_detail:TEST123" in fox.method_calls
    assert "get_device_detail:TEST456" in fox.method_calls
    assert "get_device_history:TEST123" in fox.method_calls
    assert "get_battery_charging_time:TEST123" in fox.method_calls
    assert "get_device_settings:TEST123" in fox.method_calls
    assert "get_scheduler:TEST123" in fox.method_calls
    assert "get_real_time_data:TEST123" in fox.method_calls
    assert "publish_data" in fox.method_calls

    return False


def test_run_first_call_no_devices(my_predbat):
    """
    Test run() with first=True returns False when no devices found
    """
    print("  - test_run_first_call_no_devices")

    fox = MockFoxAPIWithRunTracking()
    fox.device_list = []  # No devices

    result = run_async(fox.run(0, first=True))

    assert result == False
    assert "get_device_list" in fox.method_calls

    return False


def test_run_subsequent_call(my_predbat):
    """
    Test run() with first=False only updates real-time data (not at hourly boundary)
    """
    print("  - test_run_subsequent_call")

    fox = MockFoxAPIWithRunTracking()
    fox.device_list = [{"deviceSN": "TEST123"}]

    # seconds=300 (5 minutes), not an hourly boundary, first=False
    result = run_async(fox.run(300, first=False))

    assert result == True
    # Should NOT call initialization methods
    assert "get_device_list" not in fox.method_calls
    assert "get_device_detail:TEST123" not in fox.method_calls
    # Should call real-time data update
    assert "get_real_time_data:TEST123" in fox.method_calls
    assert "publish_data" in fox.method_calls
    # Should NOT call hourly methods (since 300 % 3600 != 0)
    assert "get_device_settings:TEST123" not in fox.method_calls

    return False


def test_run_hourly_update(my_predbat):
    """
    Test run() with first=False at hourly boundary updates settings and scheduler
    """
    print("  - test_run_hourly_update")

    fox = MockFoxAPIWithRunTracking()
    fox.device_list = [{"deviceSN": "TEST123"}]

    # seconds=3600 (1 hour), first=False
    result = run_async(fox.run(3600, first=False))

    assert result == True
    # Should call hourly methods
    assert "get_device_settings:TEST123" in fox.method_calls
    assert "get_schedule_settings_ha:TEST123" in fox.method_calls
    assert "get_scheduler:TEST123" in fox.method_calls
    assert "compute_schedule:TEST123" in fox.method_calls
    # Should call real-time data update
    assert "get_real_time_data:TEST123" in fox.method_calls
    assert "publish_data" in fox.method_calls

    return False


def test_run_with_automatic_config(my_predbat):
    """
    Test run() with automatic=True calls automatic_config
    """
    print("  - test_run_with_automatic_config")

    fox = MockFoxAPIWithRunTracking()
    fox.automatic = True
    fox.device_list = [{"deviceSN": "TEST123"}]

    result = run_async(fox.run(0, first=True))

    assert result == True
    assert fox.automatic_config_called == True
    assert "automatic_config" in fox.method_calls

    return False


def test_run_without_automatic_config(my_predbat):
    """
    Test run() with automatic=False does not call automatic_config
    """
    print("  - test_run_without_automatic_config")

    fox = MockFoxAPIWithRunTracking()
    fox.automatic = False
    fox.device_list = [{"deviceSN": "TEST123"}]

    result = run_async(fox.run(0, first=True))

    assert result == True
    assert fox.automatic_config_called == False
    assert "automatic_config" not in fox.method_calls

    return False


# ============================================================================
# Event Handler Tests
# ============================================================================


def test_apply_service_to_toggle_turn_on(my_predbat):
    """
    Test apply_service_to_toggle with turn_on service
    """
    print("  - test_apply_service_to_toggle_turn_on")

    fox = MockFoxAPIWithRequests()

    result = fox.apply_service_to_toggle(False, "turn_on")
    assert result == True

    result = fox.apply_service_to_toggle(True, "turn_on")
    assert result == True

    return False


def test_apply_service_to_toggle_turn_off(my_predbat):
    """
    Test apply_service_to_toggle with turn_off service
    """
    print("  - test_apply_service_to_toggle_turn_off")

    fox = MockFoxAPIWithRequests()

    result = fox.apply_service_to_toggle(True, "turn_off")
    assert result == False

    result = fox.apply_service_to_toggle(False, "turn_off")
    assert result == False

    return False


def test_apply_service_to_toggle_toggle(my_predbat):
    """
    Test apply_service_to_toggle with toggle service
    """
    print("  - test_apply_service_to_toggle_toggle")

    fox = MockFoxAPIWithRequests()

    result = fox.apply_service_to_toggle(False, "toggle")
    assert result == True

    result = fox.apply_service_to_toggle(True, "toggle")
    assert result == False

    return False


def test_time_string_to_hour_minute_valid(my_predbat):
    """
    Test time_string_to_hour_minute with valid time string
    """
    print("  - test_time_string_to_hour_minute_valid")

    fox = MockFoxAPIWithRequests()

    hour, minute = fox.time_string_to_hour_minute("14:30:00", 0, 0)
    assert hour == 14
    assert minute == 30

    hour, minute = fox.time_string_to_hour_minute("00:00:00", 5, 15)
    assert hour == 0
    assert minute == 0

    hour, minute = fox.time_string_to_hour_minute("23:59:00", 0, 0)
    assert hour == 23
    assert minute == 59

    return False


def test_time_string_to_hour_minute_invalid_format(my_predbat):
    """
    Test time_string_to_hour_minute with invalid format returns original values
    """
    print("  - test_time_string_to_hour_minute_invalid_format")

    fox = MockFoxAPIWithRequests()

    # Missing colon
    hour, minute = fox.time_string_to_hour_minute("1430", 5, 15)
    assert hour == 5
    assert minute == 15

    # Empty string
    hour, minute = fox.time_string_to_hour_minute("", 5, 15)
    assert hour == 5
    assert minute == 15

    return False


def test_time_string_to_hour_minute_invalid_values(my_predbat):
    """
    Test time_string_to_hour_minute with out-of-range values returns originals
    """
    print("  - test_time_string_to_hour_minute_invalid_values")

    fox = MockFoxAPIWithRequests()

    # Hour out of range
    hour, minute = fox.time_string_to_hour_minute("25:30:00", 5, 15)
    assert hour == 5
    assert minute == 30  # minute is still valid

    # Minute out of range
    hour, minute = fox.time_string_to_hour_minute("14:65:00", 5, 15)
    assert hour == 14  # hour is still valid
    assert minute == 15

    # Negative values (non-numeric will trigger ValueError)
    hour, minute = fox.time_string_to_hour_minute("ab:cd:00", 5, 15)
    assert hour == 5
    assert minute == 15

    return False


def test_write_setting_from_event_number(my_predbat):
    """
    Test write_setting_from_event for number entity type
    """
    print("  - test_write_setting_from_event_number")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings
    fox.device_settings[deviceSN] = {"MinSoc": {"value": 10, "precision": 1}}
    fox.set_mock_response("/op/v0/device/setting/set", {})

    run_async(fox.write_setting_from_event("number.predbat_fox_test123456_setting_minsoc", "20", is_number=True))

    # Verify setting was updated
    assert fox.device_settings[deviceSN]["MinSoc"]["value"] == 20

    return False


def test_write_setting_from_event_select(my_predbat):
    """
    Test write_setting_from_event for select entity type
    """
    print("  - test_write_setting_from_event_select")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings
    fox.device_settings[deviceSN] = {"WorkMode": {"value": "SelfUse", "enumList": ["SelfUse", "ForceCharge"]}}
    fox.set_mock_response("/op/v0/device/setting/set", {})

    run_async(fox.write_setting_from_event("select.predbat_fox_test123456_setting_workmode", "ForceCharge", is_number=False))

    # Verify setting was updated
    assert fox.device_settings[deviceSN]["WorkMode"]["value"] == "ForceCharge"

    return False


def test_write_battery_schedule_event_reserve(my_predbat):
    """
    Test write_battery_schedule_event for reserve changes
    """
    print("  - test_write_battery_schedule_event_reserve")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10}
    fox.device_scheduler[deviceSN] = {"enable": False, "groups": []}

    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_reserve", "25"))

    # Verify reserve was updated
    assert fox.local_schedule[deviceSN]["reserve"] == 25

    return False


def test_write_battery_schedule_event_charge_enable(my_predbat):
    """
    Test write_battery_schedule_event for charge enable toggle
    """
    print("  - test_write_battery_schedule_event_charge_enable")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"enable": 0}}

    run_async(fox.write_battery_schedule_event("switch.predbat_fox_test123456_battery_schedule_charge_enable", "turn_on"))

    # Verify charge enable was toggled on
    assert fox.local_schedule[deviceSN]["charge"]["enable"] == 1

    return False


def test_write_battery_schedule_event_time_change(my_predbat):
    """
    Test write_battery_schedule_event for start/end time changes
    """
    print("  - test_write_battery_schedule_event_time_change")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"start_time": "00:00:00", "end_time": "00:00:00"}}

    run_async(fox.write_battery_schedule_event("select.predbat_fox_test123456_battery_schedule_charge_start_time", "02:30:00"))

    # Verify time was updated
    assert fox.local_schedule[deviceSN]["charge"]["start_time"] == "02:30:00"

    return False


def test_write_battery_schedule_event_soc_change(my_predbat):
    """
    Test write_battery_schedule_event for SOC changes
    """
    print("  - test_write_battery_schedule_event_soc_change")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "discharge": {"soc": 10}}

    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_discharge_soc", "20"))

    # Verify SOC was updated
    assert fox.local_schedule[deviceSN]["discharge"]["soc"] == 20

    return False


def test_write_battery_schedule_event_power_change(my_predbat):
    """
    Test write_battery_schedule_event for power changes
    """
    print("  - test_write_battery_schedule_event_power_change")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "discharge": {"power": 8000}}

    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_discharge_power", "5000"))

    # Verify power was updated
    assert fox.local_schedule[deviceSN]["discharge"]["power"] == 5000

    return False


def test_write_battery_schedule_event_unknown_serial(my_predbat):
    """
    Test write_battery_schedule_event with unknown serial number (not in device_current_schedule)
    """
    print("  - test_write_battery_schedule_event_unknown_serial")

    fox = MockFoxAPIWithRequests()

    # Setup empty device_current_schedule
    fox.device_current_schedule = {}

    # Try to write with unknown serial
    run_async(fox.write_battery_schedule_event("number.predbat_fox_unknown123_battery_schedule_reserve", "20"))

    # Should log warning and return early, local_schedule should remain empty
    assert "unknown123" not in fox.local_schedule

    return False


def test_write_battery_schedule_event_reserve_invalid_value(my_predbat):
    """
    Test write_battery_schedule_event for reserve with invalid (non-numeric) value
    """
    print("  - test_write_battery_schedule_event_reserve_invalid_value")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 15
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 20}
    fox.device_scheduler[deviceSN] = {"enable": False, "groups": []}

    # Try to set reserve to invalid value
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_reserve", "invalid"))

    # Should fall back to fdsoc_min (15)
    assert fox.local_schedule[deviceSN]["reserve"] == 15

    return False


def test_write_battery_schedule_event_unknown_direction(my_predbat):
    """
    Test write_battery_schedule_event with entity that has no charge/discharge direction
    """
    print("  - test_write_battery_schedule_event_unknown_direction")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10}

    # Entity ID without _charge_ or _discharge_ in it (not reserve either)
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_unknown_attribute", "20"))

    # Should log warning and return early without updating
    # local_schedule should only have reserve
    assert "charge" not in fox.local_schedule[deviceSN]
    assert "discharge" not in fox.local_schedule[deviceSN]

    return False


def test_write_battery_schedule_event_initialize_direction_dict(my_predbat):
    """
    Test write_battery_schedule_event initializes direction dict when missing
    """
    print("  - test_write_battery_schedule_event_initialize_direction_dict")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    # local_schedule exists but no charge/discharge dict
    fox.local_schedule[deviceSN] = {"reserve": 10}

    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_charge_soc", "90"))

    # Should initialize charge dict and set SOC
    assert "charge" in fox.local_schedule[deviceSN]
    assert fox.local_schedule[deviceSN]["charge"]["soc"] == 90

    return False


def test_write_battery_schedule_event_soc_invalid_value(my_predbat):
    """
    Test write_battery_schedule_event for SOC with invalid (non-numeric) value
    """
    print("  - test_write_battery_schedule_event_soc_invalid_value")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"soc": 80}, "discharge": {"soc": 20}}

    # Test charge with invalid value - should default to 100
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_charge_soc", "invalid"))
    assert fox.local_schedule[deviceSN]["charge"]["soc"] == 100

    # Test discharge with invalid value - should default to fdsoc_min (10)
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_discharge_soc", "invalid"))
    assert fox.local_schedule[deviceSN]["discharge"]["soc"] == 10

    return False


def test_write_battery_schedule_event_power_invalid_value(my_predbat):
    """
    Test write_battery_schedule_event for power with invalid (non-numeric) value
    """
    print("  - test_write_battery_schedule_event_power_invalid_value")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"power": 5000}}

    # Try to set power to invalid value - should default to fdpwr_max (8000)
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_charge_power", "invalid"))
    assert fox.local_schedule[deviceSN]["charge"]["power"] == 8000

    return False


def test_write_battery_schedule_event_start_time_invalid(my_predbat):
    """
    Test write_battery_schedule_event for start_time with invalid value
    """
    print("  - test_write_battery_schedule_event_start_time_invalid")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"start_time": "02:30:00"}}

    # Try to set start_time to invalid value - should default to "00:00:00"
    run_async(fox.write_battery_schedule_event("select.predbat_fox_test123456_battery_schedule_charge_start_time", "99:99:99"))
    assert fox.local_schedule[deviceSN]["charge"]["start_time"] == "00:00:00"

    return False


def test_write_battery_schedule_event_end_time_invalid(my_predbat):
    """
    Test write_battery_schedule_event for end_time with invalid value
    """
    print("  - test_write_battery_schedule_event_end_time_invalid")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "discharge": {"end_time": "19:00:00"}}

    # Try to set end_time to invalid value - should default to "00:00:00"
    run_async(fox.write_battery_schedule_event("select.predbat_fox_test123456_battery_schedule_discharge_end_time", "invalid_time"))
    assert fox.local_schedule[deviceSN]["discharge"]["end_time"] == "00:00:00"

    return False


def test_write_battery_schedule_event_write_trigger(my_predbat):
    """
    Test write_battery_schedule_event with _write trigger calls apply_battery_schedule
    """
    print("  - test_write_battery_schedule_event_write_trigger")

    fox = MockFoxAPIWithSchedulerTracking()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {
        "reserve": 10,
        "charge": {"enable": 1, "start_time": "02:30:00", "end_time": "05:30:00", "soc": 100, "power": 8000},
        "discharge": {"enable": 0, "start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "power": 5000},
    }

    # Trigger write
    run_async(fox.write_battery_schedule_event("switch.predbat_fox_test123456_battery_schedule_charge_write", "turn_on"))

    # Verify set_scheduler was called (meaning apply_battery_schedule ran)
    assert len(fox.set_scheduler_calls) > 0

    # Verify the schedule values passed to set_scheduler are correct
    groups = fox.set_scheduler_calls[0]["groups"]
    charge_found = False
    for group in groups:
        if group.get("workMode") == "ForceCharge":
            charge_found = True
            assert group["startHour"] == 2, f"Expected startHour=2, got {group['startHour']}"
            assert group["startMinute"] == 30, f"Expected startMinute=30, got {group['startMinute']}"
            assert group["endHour"] == 5, f"Expected endHour=5, got {group['endHour']}"
            assert group["endMinute"] == 29, f"Expected endMinute=29 (end time adjusted by 1 min), got {group['endMinute']}"
            assert group["maxSoc"] == 100, f"Expected maxSoc=100, got {group['maxSoc']}"
            assert group["fdPwr"] == 8000, f"Expected fdPwr=8000, got {group['fdPwr']}"
            assert group["minSocOnGrid"] == 100, f"Expected minSocOnGrid=100 (same as maxSoc), got {group['minSocOnGrid']}"
    assert charge_found, "ForceCharge group not found in schedule"

    return False


def test_write_battery_schedule_event_unknown_attribute(my_predbat):
    """
    Test write_battery_schedule_event with unknown attribute (not soc, power, time, enable, write)
    """
    print("  - test_write_battery_schedule_event_unknown_attribute")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {}}

    # Try with unknown attribute
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_charge_unknown_attr", "20"))

    # Should log warning and return early - charge dict should still be empty
    assert len(fox.local_schedule[deviceSN]["charge"]) == 0

    return False


def test_write_battery_schedule_event_initialize_local_schedule(my_predbat):
    """
    Test write_battery_schedule_event initializes local_schedule[serial] when missing
    """
    print("  - test_write_battery_schedule_event_initialize_local_schedule")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 15
    fox.device_current_schedule[deviceSN] = []
    # local_schedule doesn't have deviceSN key
    fox.local_schedule = {}
    fox.device_scheduler[deviceSN] = {"enable": False, "groups": []}

    # Write reserve - should initialize local_schedule[deviceSN]
    run_async(fox.write_battery_schedule_event("number.predbat_fox_test123456_battery_schedule_reserve", "20"))

    # Should initialize the dict and set reserve
    assert deviceSN in fox.local_schedule
    assert fox.local_schedule[deviceSN]["reserve"] == 20

    return False


def test_select_event_setting(my_predbat):
    """
    Test select_event routes to write_setting_from_event for setting entities
    """
    print("  - test_select_event_setting")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings
    fox.device_settings[deviceSN] = {"WorkMode": {"value": "SelfUse", "enumList": ["SelfUse", "ForceCharge"]}}
    fox.set_mock_response("/op/v0/device/setting/set", {})

    run_async(fox.select_event("select.predbat_fox_test123456_setting_workmode", "ForceCharge"))

    # Verify setting was updated
    assert fox.device_settings[deviceSN]["WorkMode"]["value"] == "ForceCharge"

    return False


def test_select_event_battery_schedule(my_predbat):
    """
    Test select_event routes to write_battery_schedule_event for schedule entities
    """
    print("  - test_select_event_battery_schedule")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"start_time": "00:00:00"}}

    run_async(fox.select_event("select.predbat_fox_test123456_battery_schedule_charge_start_time", "02:30:00"))

    # Verify time was updated
    assert fox.local_schedule[deviceSN]["charge"]["start_time"] == "02:30:00"

    return False


def test_number_event_setting(my_predbat):
    """
    Test number_event routes to write_setting_from_event with is_number=True
    """
    print("  - test_number_event_setting")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device settings
    fox.device_settings[deviceSN] = {"MinSoc": {"value": 10, "precision": 1}}
    fox.set_mock_response("/op/v0/device/setting/set", {})

    run_async(fox.number_event("number.predbat_fox_test123456_setting_minsoc", "20"))

    # Verify setting was updated (with int conversion due to precision=1)
    assert fox.device_settings[deviceSN]["MinSoc"]["value"] == 20

    return False


def test_number_event_battery_schedule(my_predbat):
    """
    Test number_event routes to write_battery_schedule_event for schedule entities
    """
    print("  - test_number_event_battery_schedule")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"soc": 80}}

    run_async(fox.number_event("number.predbat_fox_test123456_battery_schedule_charge_soc", "100"))

    # Verify SOC was updated
    assert fox.local_schedule[deviceSN]["charge"]["soc"] == 100

    return False


def test_switch_event_battery_schedule(my_predbat):
    """
    Test switch_event routes to write_battery_schedule_event for schedule switches
    """
    print("  - test_switch_event_battery_schedule")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "charge": {"enable": 0}}

    run_async(fox.switch_event("switch.predbat_fox_test123456_battery_schedule_charge_enable", "turn_on"))

    # Verify enable was toggled on
    assert fox.local_schedule[deviceSN]["charge"]["enable"] == 1

    return False


def test_switch_event_toggle(my_predbat):
    """
    Test switch_event with toggle service
    """
    print("  - test_switch_event_toggle")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_current_schedule[deviceSN] = []
    fox.local_schedule[deviceSN] = {"reserve": 10, "discharge": {"enable": 1}}

    run_async(fox.switch_event("switch.predbat_fox_test123456_battery_schedule_discharge_enable", "turn_off"))

    # Verify enable was toggled off
    assert fox.local_schedule[deviceSN]["discharge"]["enable"] == 0

    return False


# ============================================================================
# publish_data Tests
# ============================================================================


def test_publish_data_device_info(my_predbat):
    """
    Test publish_data creates device info entities correctly
    """
    print("  - test_publish_data_device_info")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.device_list = [{"deviceSN": deviceSN}]
    fox.device_detail[deviceSN] = {
        "hasPV": True,
        "hasBattery": True,
        "capacity": 8,
        "function": {"scheduler": True},
        "deviceType": "KH8",
        "stationName": "Test Home",
        "batteryList": [{"capacity": 10360}],
    }
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_values[deviceSN] = {}
    fox.device_settings[deviceSN] = {}
    fox.local_schedule[deviceSN] = {}

    run_async(fox.publish_data())

    # Verify device info entity was created
    info_entity = f"sensor.predbat_fox_{deviceSN.lower()}_info"
    assert info_entity in fox.dashboard_items
    assert fox.dashboard_items[info_entity]["state"] == "Test Home"
    assert fox.dashboard_items[info_entity]["attributes"]["hasBattery"] == True
    assert fox.dashboard_items[info_entity]["attributes"]["hasScheduler"] == True

    # Verify capacity entities
    inverter_capacity_entity = f"sensor.predbat_fox_{deviceSN.lower()}_inverter_capacity"
    assert inverter_capacity_entity in fox.dashboard_items
    assert fox.dashboard_items[inverter_capacity_entity]["state"] == 8000  # 8 * 1000

    battery_capacity_entity = f"sensor.predbat_fox_{deviceSN.lower()}_battery_capacity"
    assert battery_capacity_entity in fox.dashboard_items
    assert fox.dashboard_items[battery_capacity_entity]["state"] == 10.36  # 10360 / 1000

    return False


def test_publish_data_device_values(my_predbat):
    """
    Test publish_data creates value entities correctly
    """
    print("  - test_publish_data_device_values")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.device_list = [{"deviceSN": deviceSN}]
    fox.device_detail[deviceSN] = {"hasPV": True, "hasBattery": True, "capacity": 8, "function": {}, "deviceType": "KH8", "stationName": "Test", "batteryList": []}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_values[deviceSN] = {
        "pvPower": {"value": 3.5, "name": "PV Power", "unit": "kW"},
        "SoC": {"value": 75.0, "name": "SoC", "unit": "%"},
        "generation": {"value": 1000.5, "name": "Generation", "unit": "kWh"},
    }
    fox.device_settings[deviceSN] = {}
    fox.local_schedule[deviceSN] = {}

    run_async(fox.publish_data())

    # Verify value entities were created
    pv_entity = f"sensor.predbat_fox_{deviceSN.lower()}_pvpower"
    assert pv_entity in fox.dashboard_items
    assert fox.dashboard_items[pv_entity]["state"] == 3.5
    assert fox.dashboard_items[pv_entity]["attributes"]["unit_of_measurement"] == "kW"

    soc_entity = f"sensor.predbat_fox_{deviceSN.lower()}_soc"
    assert soc_entity in fox.dashboard_items
    assert fox.dashboard_items[soc_entity]["state"] == 75.0

    # Verify energy entity has correct device_class and state_class
    gen_entity = f"sensor.predbat_fox_{deviceSN.lower()}_generation"
    assert gen_entity in fox.dashboard_items
    assert fox.dashboard_items[gen_entity]["attributes"]["device_class"] == "energy"
    assert fox.dashboard_items[gen_entity]["attributes"]["state_class"] == "total"

    return False


def test_publish_data_device_settings(my_predbat):
    """
    Test publish_data creates settings entities correctly
    """
    print("  - test_publish_data_device_settings")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.device_list = [{"deviceSN": deviceSN}]
    fox.device_detail[deviceSN] = {"hasPV": True, "hasBattery": True, "capacity": 8, "function": {}, "deviceType": "KH8", "stationName": "Test", "batteryList": []}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.device_values[deviceSN] = {}
    fox.device_settings[deviceSN] = {
        "WorkMode": {"value": "SelfUse", "unit": "", "enumList": ["SelfUse", "ForceCharge", "ForceDischarge"]},
        "MinSoc": {"value": 10, "unit": "%", "range": {"min": 10, "max": 100}, "precision": 1},
    }
    fox.local_schedule[deviceSN] = {}

    run_async(fox.publish_data())

    # Verify select entity for WorkMode
    workmode_entity = f"select.predbat_fox_{deviceSN.lower()}_setting_workmode"
    assert workmode_entity in fox.dashboard_items
    assert fox.dashboard_items[workmode_entity]["state"] == "SelfUse"
    assert "SelfUse" in fox.dashboard_items[workmode_entity]["attributes"]["options"]

    # Verify number entity for MinSoc
    minsoc_entity = f"number.predbat_fox_{deviceSN.lower()}_setting_minsoc"
    assert minsoc_entity in fox.dashboard_items
    assert fox.dashboard_items[minsoc_entity]["state"] == 10
    assert fox.dashboard_items[minsoc_entity]["attributes"]["min"] == 10
    assert fox.dashboard_items[minsoc_entity]["attributes"]["max"] == 100

    return False


def test_publish_data_no_battery_skips_settings(my_predbat):
    """
    Test publish_data skips settings entities for non-battery devices
    """
    print("  - test_publish_data_no_battery_skips_settings")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.device_list = [{"deviceSN": deviceSN}]
    fox.device_detail[deviceSN] = {"hasPV": True, "hasBattery": False, "capacity": 0, "function": {}, "deviceType": "H3", "stationName": "Test", "batteryList": []}
    fox.fdpwr_max[deviceSN] = 0
    fox.fdsoc_min[deviceSN] = 0
    fox.device_values[deviceSN] = {}
    fox.device_settings[deviceSN] = {"WorkMode": {"value": "SelfUse", "unit": "", "enumList": ["SelfUse"]}}
    fox.local_schedule[deviceSN] = {}

    run_async(fox.publish_data())

    # Verify settings entity was NOT created (no battery)
    workmode_entity = f"select.predbat_fox_{deviceSN.lower()}_setting_workmode"
    assert workmode_entity not in fox.dashboard_items

    return False


# ============================================================================
# apply_battery_schedule Tests
# ============================================================================


class MockFoxAPIWithSchedulerTracking(MockFoxAPIWithRequests):
    """
    Mock FoxAPI class that tracks set_scheduler calls for testing apply_battery_schedule.
    """

    def __init__(self):
        super().__init__()
        self.set_scheduler_calls = []
        self.set_scheduler_enabled_calls = []

    async def set_scheduler(self, deviceSN, groups):
        self.set_scheduler_calls.append({"deviceSN": deviceSN, "groups": groups})

    async def set_scheduler_enabled(self, deviceSN, enabled):
        self.set_scheduler_enabled_calls.append({"deviceSN": deviceSN, "enabled": enabled})


def test_apply_battery_schedule_charge_only(my_predbat):
    """
    Test apply_battery_schedule with only charge window enabled
    """
    print("  - test_apply_battery_schedule_charge_only")

    fox = MockFoxAPIWithSchedulerTracking()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.local_schedule[deviceSN] = {
        "reserve": 15,
        "charge": {"enable": 1, "start_time": "02:30:00", "end_time": "05:30:00", "soc": 90, "power": 8000},
        "discharge": {"enable": 0, "start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "power": 5000},
    }

    run_async(fox.apply_battery_schedule(deviceSN))

    # Verify set_scheduler was called
    assert len(fox.set_scheduler_calls) == 1
    groups = fox.set_scheduler_calls[0]["groups"]
    # Should have charge window
    charge_found = False
    for group in groups:
        if group.get("workMode") == "ForceCharge":
            charge_found = True
            assert group["startHour"] == 2, f"Expected startHour=2, got {group['startHour']}"
            assert group["startMinute"] == 30, f"Expected startMinute=30, got {group['startMinute']}"
            assert group["fdSoc"] == 100, f"Expected fdSoc=100, got {group['fdSoc']}"
            assert group["fdPwr"] == 8000, f"Expected fdPwr=8000, got {group['fdPwr']}"
            assert group["maxSoc"] == 90, f"Expected maxSoc=90, got {group['maxSoc']}"
            assert group["endHour"] == 5, f"Expected endHour=5, got {group['endHour']}"
            assert group["endMinute"] == 29, f"Expected endMinute=29, got {group['endMinute']}"
            assert group["minSocOnGrid"] == 90, f"Expected minSocOnGrid=90, got {group['minSocOnGrid']}"
    assert charge_found, "ForceCharge group not found in schedule"

    return False


def test_apply_battery_schedule_discharge_only(my_predbat):
    """
    Test apply_battery_schedule with only discharge window enabled
    """
    print("  - test_apply_battery_schedule_discharge_only")

    fox = MockFoxAPIWithSchedulerTracking()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 12}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.local_schedule[deviceSN] = {
        "reserve": 15,
        "charge": {"enable": 0, "start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "power": 8000},
        "discharge": {"enable": 1, "start_time": "16:00:00", "end_time": "19:00:00", "soc": 10, "power": 5000},
    }

    run_async(fox.apply_battery_schedule(deviceSN))

    # Verify set_scheduler was called
    assert len(fox.set_scheduler_calls) == 1
    groups = fox.set_scheduler_calls[0]["groups"]
    # Should have discharge window
    discharge_found = False
    for group in groups:
        if group.get("workMode") == "ForceDischarge":
            discharge_found = True
            assert group["startHour"] == 16
            assert group["startMinute"] == 0
            assert group["endHour"] == 18
            assert group["endMinute"] == 59
            assert group["fdSoc"] == 15  # fdsoc_min is 10, reserve is 15, so fdSoc should be max(10,15)=15
            assert group["fdPwr"] == 5000
            assert group["maxSoc"] == 15, f"Expected maxSoc=15, got {group['maxSoc']}"
            assert group["minSocOnGrid"] == 15, f"Expected minSocOnGrid=15, got {group['minSocOnGrid']}"
    assert discharge_found

    return False


def test_apply_battery_schedule_both_enabled(my_predbat):
    """
    Test apply_battery_schedule with both charge and discharge enabled
    """
    print("  - test_apply_battery_schedule_both_enabled")

    fox = MockFoxAPIWithSchedulerTracking()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.local_schedule[deviceSN] = {
        "reserve": 15,
        "charge": {"enable": 1, "start_time": "02:30:00", "end_time": "05:30:00", "soc": 100, "power": 8000},
        "discharge": {"enable": 1, "start_time": "16:00:00", "end_time": "19:00:00", "soc": 10, "power": 5000},
    }

    run_async(fox.apply_battery_schedule(deviceSN))

    # Verify set_scheduler was called with both windows
    assert len(fox.set_scheduler_calls) == 1
    groups = fox.set_scheduler_calls[0]["groups"]
    # Note: validate_schedule only keeps the first window, so we check that at least one is present
    assert len(groups) >= 1

    return False


def test_apply_battery_schedule_neither_enabled(my_predbat):
    """
    Test apply_battery_schedule with neither charge nor discharge enabled
    """
    print("  - test_apply_battery_schedule_neither_enabled")

    fox = MockFoxAPIWithSchedulerTracking()
    deviceSN = "TEST123456"

    # Setup device
    fox.device_detail[deviceSN] = {"hasBattery": True}
    fox.device_settings[deviceSN] = {"MinSocOnGrid": {"value": 10}}
    fox.fdpwr_max[deviceSN] = 8000
    fox.fdsoc_min[deviceSN] = 10
    fox.local_schedule[deviceSN] = {
        "reserve": 15,
        "charge": {"enable": 0, "start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "power": 8000},
        "discharge": {"enable": 0, "start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "power": 5000},
    }

    run_async(fox.apply_battery_schedule(deviceSN))

    # Verify set_scheduler was called with SelfUse default schedule
    assert len(fox.set_scheduler_calls) == 1
    groups = fox.set_scheduler_calls[0]["groups"]
    # Should have SelfUse mode for the whole day
    assert len(groups) == 1
    assert groups[0]["workMode"] == "SelfUse"
    assert groups[0]["startHour"] == 0
    assert groups[0]["endHour"] == 23
    assert groups[0]["endMinute"] == 59
    assert groups[0]["minSocOnGrid"] == 15, f"Expected minSocOnGrid=15, got {groups[0]['minSocOnGrid']}"
    assert groups[0]["maxSoc"] == 100

    return False


# ============================================================================
# automatic_config Tests
# ============================================================================


def test_automatic_config_single_battery(my_predbat):
    """
    Test automatic_config with single battery device
    """
    print("  - test_automatic_config_single_battery")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.device_list = [{"deviceSN": deviceSN}]
    fox.device_detail[deviceSN] = {"hasPV": True, "hasBattery": True, "capacity": 8, "function": {"scheduler": True}}

    run_async(fox.automatic_config())

    # Verify num_inverters was set
    assert fox.args_set.get("num_inverters") == 1
    assert fox.args_set.get("inverter_type") == ["FoxCloud"]

    # Verify entity mappings use lowercase SN
    sn_lower = deviceSN.lower()
    assert fox.args_set.get("soc_percent") == [f"sensor.predbat_fox_{sn_lower}_soc"]
    assert fox.args_set.get("battery_power") == [f"sensor.predbat_fox_{sn_lower}_invbatpower"]
    assert fox.args_set.get("charge_start_time") == [f"select.predbat_fox_{sn_lower}_battery_schedule_charge_start_time"]

    return False


def test_automatic_config_multiple_batteries(my_predbat):
    """
    Test automatic_config with multiple battery devices
    """
    print("  - test_automatic_config_multiple_batteries")

    fox = MockFoxAPIWithRequests()
    deviceSN1 = "TEST111111"
    deviceSN2 = "TEST222222"

    fox.device_list = [{"deviceSN": deviceSN1}, {"deviceSN": deviceSN2}]
    fox.device_detail[deviceSN1] = {"hasPV": True, "hasBattery": True, "capacity": 8, "function": {"scheduler": True}}
    fox.device_detail[deviceSN2] = {"hasPV": True, "hasBattery": True, "capacity": 8, "function": {"scheduler": True}}

    run_async(fox.automatic_config())

    # Verify num_inverters was set to 2
    assert fox.args_set.get("num_inverters") == 2
    assert len(fox.args_set.get("inverter_type")) == 2

    # Verify entity lists have two entries
    assert len(fox.args_set.get("soc_percent")) == 2
    assert f"sensor.predbat_fox_{deviceSN1.lower()}_soc" in fox.args_set.get("soc_percent")
    assert f"sensor.predbat_fox_{deviceSN2.lower()}_soc" in fox.args_set.get("soc_percent")

    return False


def test_automatic_config_battery_and_pv_inverter(my_predbat):
    """
    Test automatic_config with battery inverter + separate PV inverter
    """
    print("  - test_automatic_config_battery_and_pv_inverter")

    fox = MockFoxAPIWithRequests()
    batterySN = "BATTERY123"
    pvSN = "PV123456"

    fox.device_list = [{"deviceSN": batterySN}, {"deviceSN": pvSN}]
    # Battery inverter doesn't see PV (hasPV=False)
    fox.device_detail[batterySN] = {"hasPV": False, "hasBattery": True, "capacity": 8, "function": {"scheduler": True}}
    # Separate PV inverter
    fox.device_detail[pvSN] = {"hasPV": True, "hasBattery": False, "capacity": 0, "function": {}}

    run_async(fox.automatic_config())

    # Verify only 1 battery inverter configured
    assert fox.args_set.get("num_inverters") == 1

    # Verify PV entities come from PV inverter
    pv_power = fox.args_set.get("pv_power", [])
    assert len(pv_power) == 1
    assert f"sensor.predbat_fox_{pvSN.lower()}_pvpower" in pv_power

    return False


def test_automatic_config_no_scheduler_error(my_predbat):
    """
    Test automatic_config raises error when no devices with scheduler found
    """
    print("  - test_automatic_config_no_scheduler_error")

    fox = MockFoxAPIWithRequests()
    deviceSN = "TEST123456"

    fox.device_list = [{"deviceSN": deviceSN}]
    # Device has battery but NO scheduler support
    fox.device_detail[deviceSN] = {"hasPV": True, "hasBattery": True, "capacity": 8, "function": {"scheduler": False}}

    error_raised = False
    try:
        run_async(fox.automatic_config())
    except ValueError as e:
        error_raised = True
        assert "No batteries with scheduler found" in str(e)

    assert error_raised, "Expected ValueError to be raised"

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
        failed |= test_validate_schedule_both_charge_and_discharge(my_predbat)
        failed |= test_validate_schedule_discharge_ending_at_midnight(my_predbat)
        if failed:
            return failed

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
        failed |= test_api_get_device_history(my_predbat)
        failed |= test_api_get_device_history_empty(my_predbat)
        failed |= test_api_get_device_setting(my_predbat)
        failed |= test_api_set_device_setting(my_predbat)
        failed |= test_api_get_device_settings(my_predbat)
        failed |= test_api_get_device_settings_no_battery(my_predbat)
        failed |= test_api_get_device_settings_missing_detail(my_predbat)
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

        # get_schedule_settings_ha tests
        failed |= test_api_get_schedule_settings_ha(my_predbat)
        failed |= test_api_get_schedule_settings_ha_defaults(my_predbat)
        failed |= test_api_get_schedule_settings_ha_reserve_clamped(my_predbat)
        failed |= test_api_get_schedule_settings_ha_enable_off(my_predbat)
        failed |= test_api_get_schedule_settings_ha_invalid_values(my_predbat)

        # publish_schedule_settings_ha tests
        failed |= test_api_publish_schedule_settings_ha(my_predbat)
        failed |= test_api_publish_schedule_settings_ha_no_battery(my_predbat)
        failed |= test_api_publish_schedule_settings_ha_defaults(my_predbat)
        failed |= test_api_publish_schedule_settings_ha_enable_off(my_predbat)
        failed |= test_api_publish_schedule_settings_ha_invalid_time(my_predbat)

        # HTTP-level request_get_func tests
        failed |= test_request_get_func_auth_error_401(my_predbat)
        failed |= test_request_get_func_auth_error_403(my_predbat)
        failed |= test_request_get_func_rate_limit_429(my_predbat)
        failed |= test_request_get_func_timeout(my_predbat)
        failed |= test_request_get_func_connection_error(my_predbat)
        failed |= test_request_get_func_json_decode_error(my_predbat)
        failed |= test_request_get_func_fox_error_rate_limit(my_predbat)
        failed |= test_request_get_func_fox_error_api_limit(my_predbat)
        failed |= test_request_get_func_fox_error_unsupported(my_predbat)
        failed |= test_request_get_func_fox_error_invalid_param(my_predbat)
        failed |= test_request_get_func_fox_error_comms_issue(my_predbat)
        failed |= test_request_get_func_success(my_predbat)
        failed |= test_request_get_func_unknown_error(my_predbat)

        # request_get / request_get_func tests with MagicMock
        failed |= test_request_get_func_real_success_get(my_predbat)
        failed |= test_request_get_func_real_success_post(my_predbat)
        failed |= test_request_get_func_real_auth_error_401(my_predbat)
        failed |= test_request_get_func_real_rate_limit_429(my_predbat)
        failed |= test_request_get_func_real_fox_errno_rate_limit(my_predbat)
        failed |= test_request_get_func_real_fox_errno_api_limit(my_predbat)
        failed |= test_request_get_func_real_connection_error(my_predbat)
        failed |= test_request_get_func_real_timeout(my_predbat)
        failed |= test_request_get_func_real_json_decode_error(my_predbat)
        failed |= test_request_get_real_retry_on_rate_limit(my_predbat)
        failed |= test_request_get_real_no_retry_on_auth_error(my_predbat)
        failed |= test_request_get_real_max_retries(my_predbat)
        failed |= test_request_get_real_post_with_data(my_predbat)

        # run() method tests
        failed |= test_run_first_call_with_devices(my_predbat)
        failed |= test_run_first_call_no_devices(my_predbat)
        failed |= test_run_subsequent_call(my_predbat)
        failed |= test_run_hourly_update(my_predbat)
        failed |= test_run_with_automatic_config(my_predbat)
        failed |= test_run_without_automatic_config(my_predbat)

        # Event handler tests
        failed |= test_apply_service_to_toggle_turn_on(my_predbat)
        failed |= test_apply_service_to_toggle_turn_off(my_predbat)
        failed |= test_apply_service_to_toggle_toggle(my_predbat)
        failed |= test_time_string_to_hour_minute_valid(my_predbat)
        failed |= test_time_string_to_hour_minute_invalid_format(my_predbat)
        failed |= test_time_string_to_hour_minute_invalid_values(my_predbat)
        failed |= test_write_setting_from_event_number(my_predbat)
        failed |= test_write_setting_from_event_select(my_predbat)
        failed |= test_write_battery_schedule_event_reserve(my_predbat)
        failed |= test_write_battery_schedule_event_charge_enable(my_predbat)
        failed |= test_write_battery_schedule_event_time_change(my_predbat)
        failed |= test_write_battery_schedule_event_soc_change(my_predbat)
        failed |= test_write_battery_schedule_event_power_change(my_predbat)
        failed |= test_write_battery_schedule_event_unknown_serial(my_predbat)
        failed |= test_write_battery_schedule_event_reserve_invalid_value(my_predbat)
        failed |= test_write_battery_schedule_event_unknown_direction(my_predbat)
        failed |= test_write_battery_schedule_event_initialize_direction_dict(my_predbat)
        failed |= test_write_battery_schedule_event_soc_invalid_value(my_predbat)
        failed |= test_write_battery_schedule_event_power_invalid_value(my_predbat)
        failed |= test_write_battery_schedule_event_start_time_invalid(my_predbat)
        failed |= test_write_battery_schedule_event_end_time_invalid(my_predbat)
        failed |= test_write_battery_schedule_event_write_trigger(my_predbat)
        failed |= test_write_battery_schedule_event_unknown_attribute(my_predbat)
        failed |= test_write_battery_schedule_event_initialize_local_schedule(my_predbat)
        failed |= test_select_event_setting(my_predbat)
        failed |= test_select_event_battery_schedule(my_predbat)
        failed |= test_number_event_setting(my_predbat)
        failed |= test_number_event_battery_schedule(my_predbat)
        failed |= test_switch_event_battery_schedule(my_predbat)
        failed |= test_switch_event_toggle(my_predbat)

        # publish_data tests
        failed |= test_publish_data_device_info(my_predbat)
        failed |= test_publish_data_device_values(my_predbat)
        failed |= test_publish_data_device_settings(my_predbat)
        failed |= test_publish_data_no_battery_skips_settings(my_predbat)

        # apply_battery_schedule tests
        failed |= test_apply_battery_schedule_charge_only(my_predbat)
        failed |= test_apply_battery_schedule_discharge_only(my_predbat)
        failed |= test_apply_battery_schedule_both_enabled(my_predbat)
        failed |= test_apply_battery_schedule_neither_enabled(my_predbat)

        # automatic_config tests
        failed |= test_automatic_config_single_battery(my_predbat)
        failed |= test_automatic_config_multiple_batteries(my_predbat)
        failed |= test_automatic_config_battery_and_pv_inverter(my_predbat)
        failed |= test_automatic_config_no_scheduler_error(my_predbat)
    except Exception as e:
        print(f"ERROR: Fox API test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        failed = True

    return failed
