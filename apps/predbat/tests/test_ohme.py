# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for Ohme EV charger integration
"""

import datetime
import time
from unittest.mock import patch, MagicMock, AsyncMock
from typing import Dict, Optional
from tests.test_infra import run_async

# Import ohme module components
import sys
import os

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ohme import (
    OhmeAPI,
    OhmeApiClient,
    ChargerStatus,
    ChargerMode,
    ChargeSlot,
    ChargerPower,
    time_next_occurs,
    slot_list,
    vehicle_to_name,
)


# ============================================================================
# Mock data constants
# ============================================================================

MOCK_LOGIN_RESPONSE = {
    "idToken": "test_firebase_token_abc123",
    "refreshToken": "test_refresh_token_xyz789",
    "expiresIn": "3600"
}

MOCK_DEVICE_INFO_RESPONSE = {
    "chargeDevices": [{
        "id": "TEST-SERIAL-123",
        "modelTypeDisplayName": "Ohme Home Pro",
        "firmwareVersionLabel": "1.2.3",
        "modelCapabilities": {
            "solarModes": ["solar_boost"],
            "ct_clamp": True
        },
        "optionalSettings": {
            "solar_enabled": False
        }
    }],
    "cars": [{
        "id": "car-123",
        "name": "Tesla Model 3",
        "model": {
            "make": "Tesla",
            "modelName": "Model 3",
            "availableFromYear": 2017,
            "brand": {"name": "Tesla"}
        }
    }],
    "userSettings": {
        "chargeSettings": [{
            "enabled": True,
            "value": 15.0
        }]
    },
    "tariff": None
}

MOCK_CHARGE_SESSION_RESPONSE = {
    "mode": "SMART_CHARGE",
    "power": {"watt": 7200, "amp": 32, "volt": 230},
    "batterySoc": {"wh": 15000, "percent": 75},
    "car": {"batterySoc": {"percent": 75}},
    "appliedRule": {
        "targetPercent": 80,
        "targetTime": 25200,  # 07:00 in seconds
        "preconditioningEnabled": True,
        "preconditionLengthMins": 30
    },
    "allSessionSlots": [
        {"startTimeMs": 1703030400000, "endTimeMs": 1703034000000, "watts": 7200},
        {"startTimeMs": 1703034000000, "endTimeMs": 1703037600000, "watts": 7200}
    ]
}

MOCK_NEXT_SESSION_RESPONSE = {
    "rule": {
        "id": "rule-123",
        "targetPercent": 80,
        "targetTime": 25200,
        "preconditioningEnabled": False,
        "preconditionLengthMins": 0
    }
}

MOCK_ADVANCED_SETTINGS_RESPONSE = {
    "online": True,
    "clampConnected": True,
    "clampAmps": 32.5
}


# ============================================================================
# Mock OhmeApiClient
# ============================================================================

class MockOhmeApiClient(OhmeApiClient):
    """Mock API client for testing without real HTTP calls"""

    def __init__(self, email="test@example.com", password="test_password"):
        """Initialize mock client"""
        # Mock log function
        self.log_messages = []
        def mock_log(msg):
            self.log_messages.append(msg)

        # Initialize parent with mock log
        super().__init__(email, password, mock_log)

        # Mock response data
        self.mock_responses = {}
        self.request_log = []

        # Set default mock responses
        self.setup_default_responses()

    def setup_default_responses(self):
        """Setup default mock responses"""
        self.mock_responses = {
            ("GET", "/v1/chargeSessions"): [MOCK_CHARGE_SESSION_RESPONSE],
            ("GET", "/v1/chargeSessions/nextSessionInfo"): MOCK_NEXT_SESSION_RESPONSE,
            ("GET", "/v1/users/me/account"): MOCK_DEVICE_INFO_RESPONSE,
            ("GET", "/v1/chargeDevices/TEST-SERIAL-123/advancedSettings"): MOCK_ADVANCED_SETTINGS_RESPONSE,
            ("PUT", "/v1/chargeSessions/TEST-SERIAL-123/approve?approve=true"): True,
            ("POST", "/v1/chargeSessions/TEST-SERIAL-123/stop"): "OK",
            ("POST", "/v1/chargeSessions/TEST-SERIAL-123/resume"): "OK",
        }

    def set_mock_response(self, method, url, response):
        """Set a specific mock response"""
        self.mock_responses[(method, url)] = response

    async def _make_request(self, method: str, url: str, data: Optional[Dict] = None, skip_json: bool = False):
        """Mock _make_request to return predefined responses"""
        # Log the request
        self.request_log.append({
            "method": method,
            "url": url,
            "data": data,
            "skip_json": skip_json
        })

        # Find matching response
        # Try exact URL match first
        key = (method, url)
        if key in self.mock_responses:
            return self.mock_responses[key]

        # Try pattern matching for parameterized URLs
        for (mock_method, mock_url), response in self.mock_responses.items():
            if method == mock_method:
                # Check if URL patterns match (e.g., with {serial} placeholder)
                if "/v1/chargeSessions/" in mock_url and "/v1/chargeSessions/" in url:
                    return response
                elif "/v1/chargeDevices/" in mock_url and "/v1/chargeDevices/" in url:
                    return response
                elif "/v1/chargeRules/" in mock_url and "/v1/chargeRules/" in url:
                    return response
                elif "/v1/car/" in mock_url and "/v1/car/" in url:
                    return response

        # Default response for PUT requests
        if method == "PUT":
            return True

        # Default response for POST requests with skip_json
        if method == "POST" and skip_json:
            return "OK"

        # If no match, return empty dict
        return {}


# ============================================================================
# Helper Function Tests
# ============================================================================


def test_ohme(my_predbat=None):
    """
    ======================================================================
    OHME EV CHARGER TEST SUITE
    ======================================================================
    Comprehensive test suite for Ohme EV charger integration including:
    - Helper functions (time_next_occurs, slot_list, vehicle_to_name)
    - OhmeApiClient methods (status, mode, power, slots, vehicles)
    - API operations (login, refresh, get_charge_session, update_device_info)
    - HTTP operations (_make_request with GET/POST/PUT)
    - OhmeAPI component (publish_data, run method, event handlers)
    - HA event handlers (select, number, switch)
    """
    print("\n" + "="*70)
    print("OHME EV CHARGER TEST SUITE")
    print("="*70)

    # Sub-test registry - each entry is (key, function, description)
    sub_tests = [
        ("time_next_today", _test_ohme_time_next_occurs_today, "time_next_occurs for a time later today"),
        ("time_next_tomorrow", _test_ohme_time_next_occurs_tomorrow, "time_next_occurs for a time tomorrow"),
        ("slot_list_empty", _test_ohme_slot_list_empty, "slot_list with empty slots"),
        ("slot_list_single", _test_ohme_slot_list_single, "slot_list with single slot"),
        ("slot_list_merged", _test_ohme_slot_list_merged, "slot_list with merged slots"),
        ("vehicle_name_custom", _test_ohme_vehicle_to_name_custom, "vehicle_to_name with custom name"),
        ("vehicle_name_model", _test_ohme_vehicle_to_name_model, "vehicle_to_name with model name"),
        ("status_charging", _test_ohme_client_status_charging, "OhmeApiClient status CHARGING"),
        ("status_unplugged", _test_ohme_client_status_unplugged, "OhmeApiClient status UNPLUGGED"),
        ("status_pending", _test_ohme_client_status_pending_approval, "OhmeApiClient status PENDING_APPROVAL"),
        ("mode_smart", _test_ohme_client_mode_smart_charge, "OhmeApiClient mode SMART_CHARGE"),
        ("mode_max", _test_ohme_client_mode_max_charge, "OhmeApiClient mode MAX_CHARGE"),
        ("power", _test_ohme_client_power, "OhmeApiClient power property"),
        ("target_soc_progress", _test_ohme_client_target_soc_in_progress, "OhmeApiClient target_soc in progress"),
        ("target_soc_paused", _test_ohme_client_target_soc_paused, "OhmeApiClient target_soc paused"),
        ("target_time", _test_ohme_client_target_time, "OhmeApiClient target_time"),
        ("slots", _test_ohme_client_slots, "OhmeApiClient slots property"),
        ("vehicles", _test_ohme_client_vehicles, "OhmeApiClient vehicles property"),
        ("current_vehicle", _test_ohme_client_current_vehicle, "OhmeApiClient current_vehicle"),
        ("pause_charge", _test_ohme_client_async_pause_charge, "async_pause_charge"),
        ("resume_charge", _test_ohme_client_async_resume_charge, "async_resume_charge"),
        ("approve_charge", _test_ohme_client_async_approve_charge, "async_approve_charge"),
        ("max_charge_enable", _test_ohme_client_async_max_charge_enable, "max_charge enable"),
        ("max_charge_disable", _test_ohme_client_async_max_charge_disable, "max_charge disable"),
        ("set_target", _test_ohme_client_async_set_target, "async_set_target"),
        ("get_session", _test_ohme_client_async_get_charge_session, "async_get_charge_session"),
        ("update_device", _test_ohme_client_async_update_device_info, "async_update_device_info"),
        ("login_success", _test_ohme_client_async_login_success, "login success"),
        ("refresh_no_token", _test_ohme_client_async_refresh_session_no_token, "refresh session no token"),
        ("refresh_recent", _test_ohme_client_async_refresh_session_recent_token, "refresh session recent token"),
        ("refresh_expired", _test_ohme_client_async_refresh_session_expired_token, "refresh session expired"),
        ("refresh_failure", _test_ohme_client_async_refresh_session_failure, "refresh session failure"),
        ("make_request_get", _test_ohme_client_make_request_get_success, "_make_request GET"),
        ("make_request_put", _test_ohme_client_make_request_put_success, "_make_request PUT"),
        ("make_request_post_json", _test_ohme_client_make_request_post_json, "_make_request POST JSON"),
        ("make_request_post_text", _test_ohme_client_make_request_post_skip_json, "_make_request POST text"),
        ("make_request_error", _test_ohme_client_make_request_api_error, "_make_request API error"),
        ("make_request_session", _test_ohme_client_make_request_creates_session, "_make_request creates session"),
        ("session_retry", _test_ohme_client_async_get_charge_session_retry, "session retry on CALCULATING"),
        ("set_mode_max", _test_ohme_client_async_set_mode_max_charge, "async_set_mode MAX_CHARGE"),
        ("set_mode_smart", _test_ohme_client_async_set_mode_smart_charge, "async_set_mode SMART_CHARGE"),
        ("set_mode_paused", _test_ohme_client_async_set_mode_paused, "async_set_mode PAUSED"),
        ("set_mode_string", _test_ohme_client_async_set_mode_string, "async_set_mode string"),
        ("set_vehicle_found", _test_ohme_client_async_set_vehicle_found, "async_set_vehicle found"),
        ("set_vehicle_not_found", _test_ohme_client_async_set_vehicle_not_found, "async_set_vehicle not found"),
        ("update_schedule_all", _test_ohme_client_async_update_schedule_all_params, "async_update_schedule all params"),
        ("update_schedule_partial", _test_ohme_client_async_update_schedule_partial_params, "async_update_schedule partial"),
        ("update_schedule_no_rule", _test_ohme_client_async_update_schedule_no_rule, "async_update_schedule no rule"),
        ("publish_data", _test_ohme_publish_data, "OhmeAPI publish_data"),
        ("publish_disconnected", _test_ohme_publish_data_disconnected, "OhmeAPI publish_data disconnected"),
        ("run_first", _test_ohme_run_first_call, "OhmeAPI run first call"),
        ("run_30min", _test_ohme_run_periodic_30min, "OhmeAPI run 30min periodic"),
        ("run_120s", _test_ohme_run_periodic_120s, "OhmeAPI run 120s periodic"),
        ("run_no_periodic", _test_ohme_run_no_periodic, "OhmeAPI run no periodic"),
        ("run_queued_events", _test_ohme_run_with_queued_events, "OhmeAPI run with queued events"),
        ("run_exception", _test_ohme_run_event_handler_exception, "OhmeAPI run event handler exception"),
        ("run_octopus", _test_ohme_run_first_with_octopus_intelligent, "OhmeAPI run with octopus intelligent"),
        ("select_target_time", _test_ohme_select_event_handler_target_time, "select_event_handler target_time"),
        ("select_invalid_time", _test_ohme_select_event_handler_invalid_time, "select_event_handler invalid time"),
        ("number_target_soc", _test_ohme_number_event_handler_target_soc, "number_event_handler target_soc"),
        ("number_target_soc_invalid", _test_ohme_number_event_handler_target_soc_invalid, "number_event_handler invalid SoC"),
        ("number_preconditioning", _test_ohme_number_event_handler_preconditioning, "number_event_handler preconditioning"),
        ("number_preconditioning_off", _test_ohme_number_event_handler_preconditioning_off, "number_event_handler preconditioning off"),
        ("number_preconditioning_invalid", _test_ohme_number_event_handler_preconditioning_invalid, "number_event_handler invalid preconditioning"),
        ("switch_max_charge_on", _test_ohme_switch_event_handler_max_charge_on, "switch_event_handler max_charge on"),
        ("switch_max_charge_off", _test_ohme_switch_event_handler_max_charge_off, "switch_event_handler max_charge off"),
        ("switch_approve_charge", _test_ohme_switch_event_handler_approve_charge, "switch_event_handler approve_charge"),
        ("switch_approve_wrong_status", _test_ohme_switch_event_handler_approve_charge_wrong_status, "switch_event_handler approve wrong status"),
    ]

    # Run all sub-tests
    passed = 0
    failed = 0
    for key, test_func, description in sub_tests:
        print(f"\n[{key}] {description}")
        print("-" * 70)
        try:
            result = test_func(my_predbat)
            if result:
                print(f"✗ FAILED: {key}")
                failed += 1
            else:
                print(f"✓ PASSED: {key}")
                passed += 1
        except Exception as e:
            print(f"✗ EXCEPTION in {key}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    # Print summary
    print("\n" + "="*70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("="*70)

    return failed > 0


def _test_ohme_time_next_occurs_today(my_predbat=None):
    """Test time_next_occurs for a time later today"""
    print("**** Running test_ohme_time_next_occurs_today ****")

    # Get current time
    now = datetime.datetime.now()

    # Calculate a time 2 hours from now
    future_time = now + datetime.timedelta(hours=2)
    hour = future_time.hour
    minute = future_time.minute

    # Get the next occurrence
    result = time_next_occurs(hour, minute)

    # Should be today (within 24 hours from now)
    assert result.date() == future_time.date(), f"Expected date {future_time.date()}, got {result.date()}"
    assert result.hour == hour, f"Expected hour {hour}, got {result.hour}"
    assert result.minute == minute, f"Expected minute {minute}, got {result.minute}"

    print("PASS: time_next_occurs correctly returns future time today")
    return 0


def _test_ohme_time_next_occurs_tomorrow(my_predbat=None):
    """Test time_next_occurs for a time that should be tomorrow"""
    print("**** Running test_ohme_time_next_occurs_tomorrow ****")

    # Get current time
    now = datetime.datetime.now()

    # Calculate a time 1 hour ago (should roll to tomorrow)
    past_time = now - datetime.timedelta(hours=1)
    hour = past_time.hour
    minute = past_time.minute

    # Get the next occurrence
    result = time_next_occurs(hour, minute)

    # Should be tomorrow
    expected_date = (now + datetime.timedelta(days=1)).date()
    assert result.date() == expected_date, f"Expected date {expected_date}, got {result.date()}"
    assert result.hour == hour, f"Expected hour {hour}, got {result.hour}"
    assert result.minute == minute, f"Expected minute {minute}, got {result.minute}"

    print("PASS: time_next_occurs correctly returns time tomorrow")
    return 0


def _test_ohme_slot_list_empty(my_predbat=None):
    """Test slot_list with no slots"""
    print("**** Running test_ohme_slot_list_empty ****")

    data = {"allSessionSlots": []}
    slots = slot_list(data)

    assert len(slots) == 0, f"Expected 0 slots, got {len(slots)}"

    print("PASS: slot_list returns empty list for no slots")
    return 0


def _test_ohme_slot_list_single(my_predbat=None):
    """Test slot_list with single slot"""
    print("**** Running test_ohme_slot_list_single ****")

    # Create a slot for 1 hour at 7200W
    start_ms = 1703030400000  # 2023-12-20 00:00:00 UTC
    end_ms = 1703034000000    # 2023-12-20 01:00:00 UTC

    data = {
        "allSessionSlots": [
            {"startTimeMs": start_ms, "endTimeMs": end_ms, "watts": 7200}
        ]
    }

    slots = slot_list(data)

    assert len(slots) == 1, f"Expected 1 slot, got {len(slots)}"

    slot = slots[0]
    assert slot.energy == 7.2, f"Expected 7.2 kWh, got {slot.energy}"

    print("PASS: slot_list correctly parses single slot")
    return 0


def _test_ohme_slot_list_merged(my_predbat=None):
    """Test slot_list merges adjacent slots"""
    print("**** Running test_ohme_slot_list_merged ****")

    # Create two adjacent 1-hour slots at 7200W each
    start_ms_1 = 1703030400000  # 2023-12-20 00:00:00 UTC
    end_ms_1 = 1703034000000    # 2023-12-20 01:00:00 UTC
    start_ms_2 = 1703034000000  # 2023-12-20 01:00:00 UTC (same as end_ms_1)
    end_ms_2 = 1703037600000    # 2023-12-20 02:00:00 UTC

    data = {
        "allSessionSlots": [
            {"startTimeMs": start_ms_1, "endTimeMs": end_ms_1, "watts": 7200},
            {"startTimeMs": start_ms_2, "endTimeMs": end_ms_2, "watts": 7200}
        ]
    }

    slots = slot_list(data)

    # Should be merged into 1 slot
    assert len(slots) == 1, f"Expected 1 merged slot, got {len(slots)}"

    slot = slots[0]
    # 2 hours at 7200W = 14.4 kWh
    assert slot.energy == 14.4, f"Expected 14.4 kWh, got {slot.energy}"

    print("PASS: slot_list correctly merges adjacent slots")
    return 0


def _test_ohme_vehicle_to_name_custom(my_predbat=None):
    """Test vehicle_to_name with custom name"""
    print("**** Running test_ohme_vehicle_to_name_custom ****")

    vehicle = {"name": "My Tesla"}
    result = vehicle_to_name(vehicle)

    assert result == "My Tesla", f"Expected 'My Tesla', got '{result}'"

    print("PASS: vehicle_to_name returns custom name")
    return 0


def _test_ohme_vehicle_to_name_model(my_predbat=None):
    """Test vehicle_to_name with model data"""
    print("**** Running test_ohme_vehicle_to_name_model ****")

    vehicle = {
        "model": {
            "brand": {"name": "Tesla"},
            "modelName": "Model 3",
            "availableFromYear": 2017,
            "availableToYear": 2023
        }
    }
    result = vehicle_to_name(vehicle)

    expected = "Tesla Model 3 (2017-2023)"
    assert result == expected, f"Expected '{expected}', got '{result}'"

    print("PASS: vehicle_to_name generates name from model data")
    return 0


# ============================================================================
# OhmeApiClient Property Tests
# ============================================================================

def _test_ohme_client_status_charging(my_predbat=None):
    """Test status property returns CHARGING"""
    print("**** Running test_ohme_client_status_charging ****")

    client = MockOhmeApiClient()
    client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200}
    }

    status = client.status
    assert status == ChargerStatus.CHARGING, f"Expected CHARGING, got {status}"

    print("PASS: status property returns CHARGING")
    return 0


def _test_ohme_client_status_unplugged(my_predbat=None):
    """Test status property returns UNPLUGGED"""
    print("**** Running test_ohme_client_status_unplugged ****")

    client = MockOhmeApiClient()
    client._charge_session = {"mode": "DISCONNECTED"}

    status = client.status
    assert status == ChargerStatus.UNPLUGGED, f"Expected UNPLUGGED, got {status}"

    print("PASS: status property returns UNPLUGGED")
    return 0


def _test_ohme_client_status_pending_approval(my_predbat=None):
    """Test status property returns PENDING_APPROVAL"""
    print("**** Running test_ohme_client_status_pending_approval ****")

    client = MockOhmeApiClient()
    client._charge_session = {"mode": "PENDING_APPROVAL"}

    status = client.status
    assert status == ChargerStatus.PENDING_APPROVAL, f"Expected PENDING_APPROVAL, got {status}"

    print("PASS: status property returns PENDING_APPROVAL")
    return 0


def _test_ohme_client_mode_smart_charge(my_predbat=None):
    """Test mode property returns SMART_CHARGE"""
    print("**** Running test_ohme_client_mode_smart_charge ****")

    client = MockOhmeApiClient()
    client._charge_session = {"mode": "SMART_CHARGE"}

    mode = client.mode
    assert mode == ChargerMode.SMART_CHARGE, f"Expected SMART_CHARGE, got {mode}"

    print("PASS: mode property returns SMART_CHARGE")
    return 0


def _test_ohme_client_mode_max_charge(my_predbat=None):
    """Test mode property returns MAX_CHARGE"""
    print("**** Running test_ohme_client_mode_max_charge ****")

    client = MockOhmeApiClient()
    client._charge_session = {"mode": "MAX_CHARGE"}

    mode = client.mode
    assert mode == ChargerMode.MAX_CHARGE, f"Expected MAX_CHARGE, got {mode}"

    print("PASS: mode property returns MAX_CHARGE")
    return 0


def _test_ohme_client_power(my_predbat=None):
    """Test power property returns ChargerPower"""
    print("**** Running test_ohme_client_power ****")

    client = MockOhmeApiClient()
    client._charge_session = {
        "power": {"watt": 7200, "amp": 32, "volt": 230}
    }
    client._advanced_settings = {"clampAmps": 30.5}

    power = client.power

    assert isinstance(power, ChargerPower), f"Expected ChargerPower, got {type(power)}"
    assert power.watts == 7200, f"Expected 7200W, got {power.watts}"
    assert power.amps == 32, f"Expected 32A, got {power.amps}"
    assert power.volts == 230, f"Expected 230V, got {power.volts}"
    assert power.ct_amps == 30.5, f"Expected 30.5A CT, got {power.ct_amps}"

    print("PASS: power property returns correct ChargerPower")
    return 0


def _test_ohme_client_target_soc_in_progress(my_predbat=None):
    """Test target_soc for charge in progress"""
    print("**** Running test_ohme_client_target_soc_in_progress ****")

    client = MockOhmeApiClient()
    client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200},
        "appliedRule": {"targetPercent": 85}
    }

    target = client.target_soc
    assert target == 85, f"Expected 85%, got {target}%"

    print("PASS: target_soc returns appliedRule target for charge in progress")
    return 0


def _test_ohme_client_target_soc_paused(my_predbat=None):
    """Test target_soc for paused charge with suspended rule"""
    print("**** Running test_ohme_client_target_soc_paused ****")

    client = MockOhmeApiClient()
    client._charge_session = {
        "mode": "STOPPED",
        "suspendedRule": {"targetPercent": 90}
    }

    target = client.target_soc
    assert target == 90, f"Expected 90%, got {target}%"

    print("PASS: target_soc returns suspendedRule target for paused charge")
    return 0


def _test_ohme_client_target_time(my_predbat=None):
    """Test target_time calculation"""
    print("**** Running test_ohme_client_target_time ****")

    client = MockOhmeApiClient()
    client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200},
        "appliedRule": {"targetTime": 25200}  # 07:00 (7 * 3600)
    }

    target = client.target_time
    assert target == (7, 0), f"Expected (7, 0), got {target}"

    print("PASS: target_time correctly converts seconds to (hour, minute)")
    return 0


def _test_ohme_client_slots(my_predbat=None):
    """Test slots property"""
    print("**** Running test_ohme_client_slots ****")

    client = MockOhmeApiClient()
    client._charge_session = {
        "allSessionSlots": [
            {"startTimeMs": 1703030400000, "endTimeMs": 1703034000000, "watts": 7200}
        ]
    }

    slots = client.slots
    assert len(slots) == 1, f"Expected 1 slot, got {len(slots)}"
    assert isinstance(slots[0], ChargeSlot), f"Expected ChargeSlot, got {type(slots[0])}"

    print("PASS: slots property returns list of ChargeSlot")
    return 0


def _test_ohme_client_vehicles(my_predbat=None):
    """Test vehicles property"""
    print("**** Running test_ohme_client_vehicles ****")

    client = MockOhmeApiClient()
    client._cars = [
        {"name": "Tesla Model 3"},
        {"name": "Nissan Leaf"}
    ]

    vehicles = client.vehicles
    assert len(vehicles) == 2, f"Expected 2 vehicles, got {len(vehicles)}"
    assert "Tesla Model 3" in vehicles, "Tesla Model 3 not in vehicles list"
    assert "Nissan Leaf" in vehicles, "Nissan Leaf not in vehicles list"

    print("PASS: vehicles property returns list of vehicle names")
    return 0


def _test_ohme_client_current_vehicle(my_predbat=None):
    """Test current_vehicle property"""
    print("**** Running test_ohme_client_current_vehicle ****")

    client = MockOhmeApiClient()
    client._cars = [
        {"name": "Tesla Model 3"},
        {"name": "Nissan Leaf"}
    ]

    vehicle = client.current_vehicle
    assert vehicle == "Tesla Model 3", f"Expected 'Tesla Model 3', got '{vehicle}'"

    print("PASS: current_vehicle returns first vehicle in list")
    return 0


# ============================================================================
# OhmeApiClient Push Method Tests
# ============================================================================

def _test_ohme_client_async_pause_charge(my_predbat=None):
    """Test async_pause_charge sends correct request"""
    print("**** Running test_ohme_client_async_pause_charge ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    result = run_async(client.async_pause_charge())

    assert result == True, f"Expected True, got {result}"

    # Check request was logged
    assert len(client.request_log) > 0, "No requests logged"
    last_request = client.request_log[-1]
    assert last_request["method"] == "POST", f"Expected POST, got {last_request['method']}"
    assert "/stop" in last_request["url"], f"Expected /stop in URL, got {last_request['url']}"

    print("PASS: async_pause_charge sends correct POST request")
    return 0


def _test_ohme_client_async_resume_charge(my_predbat=None):
    """Test async_resume_charge sends correct request"""
    print("**** Running test_ohme_client_async_resume_charge ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    result = run_async(client.async_resume_charge())

    assert result == True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert last_request["method"] == "POST", f"Expected POST, got {last_request['method']}"
    assert "/resume" in last_request["url"], f"Expected /resume in URL, got {last_request['url']}"

    print("PASS: async_resume_charge sends correct POST request")
    return 0


def _test_ohme_client_async_approve_charge(my_predbat=None):
    """Test async_approve_charge sends correct request"""
    print("**** Running test_ohme_client_async_approve_charge ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    result = run_async(client.async_approve_charge())

    assert result == True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "/approve" in last_request["url"], f"Expected /approve in URL, got {last_request['url']}"

    print("PASS: async_approve_charge sends correct PUT request")
    return 0


def _test_ohme_client_async_max_charge_enable(my_predbat=None):
    """Test async_max_charge enables max charge"""
    print("**** Running test_ohme_client_async_max_charge_enable ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    result = run_async(client.async_max_charge(True))

    assert result == True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "maxCharge=true" in last_request["url"], f"Expected maxCharge=true in URL, got {last_request['url']}"

    print("PASS: async_max_charge enables max charge")
    return 0


def _test_ohme_client_async_max_charge_disable(my_predbat=None):
    """Test async_max_charge disables max charge"""
    print("**** Running test_ohme_client_async_max_charge_disable ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    result = run_async(client.async_max_charge(False))

    assert result == True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert "maxCharge=false" in last_request["url"], f"Expected maxCharge=false in URL, got {last_request['url']}"

    print("PASS: async_max_charge disables max charge")
    return 0


def _test_ohme_client_async_set_target(my_predbat=None):
    """Test async_set_target for active session"""
    print("**** Running test_ohme_client_async_set_target ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"
    client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200},
        "appliedRule": {"targetPercent": 80}
    }
    client._last_rule = {"targetPercent": 80, "targetTime": 25200, "preconditioningEnabled": False}

    result = run_async(client.async_set_target(target_percent=90, target_time=(8, 30)))

    assert result == True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "toPercent=90" in last_request["url"], f"Expected toPercent=90 in URL, got {last_request['url']}"

    print("PASS: async_set_target sets target for active session")
    return 0


# ============================================================================
# OhmeApiClient Pull Method Tests
# ============================================================================

def _test_ohme_client_async_get_charge_session(my_predbat=None):
    """Test async_get_charge_session fetches and parses data"""
    print("**** Running test_ohme_client_async_get_charge_session ****")

    client = MockOhmeApiClient()

    run_async(client.async_get_charge_session())

    # Check that _charge_session was populated
    assert client._charge_session is not None, "_charge_session is None"
    assert client._charge_session.get("mode") == "SMART_CHARGE", f"Expected SMART_CHARGE mode, got {client._charge_session.get('mode')}"

    # Check that energy was set
    assert client.energy > 0, f"Expected energy > 0, got {client.energy}"

    # Check that battery was set
    assert client.battery == 75, f"Expected battery 75%, got {client.battery}%"

    print("PASS: async_get_charge_session fetches and parses data")
    return 0


def _test_ohme_client_async_update_device_info(my_predbat=None):
    """Test async_update_device_info fetches device details"""
    print("**** Running test_ohme_client_async_update_device_info ****")

    client = MockOhmeApiClient()

    result = run_async(client.async_update_device_info())

    assert result == True, f"Expected True, got {result}"

    # Check that serial was set
    assert client.serial == "TEST-SERIAL-123", f"Expected TEST-SERIAL-123, got {client.serial}"

    # Check that device_info was populated
    assert "name" in client.device_info, "device_info missing 'name' key"
    assert client.device_info["name"] == "Ohme Home Pro", f"Expected 'Ohme Home Pro', got {client.device_info['name']}"

    # Check that cars were populated
    assert len(client._cars) == 1, f"Expected 1 car, got {len(client._cars)}"

    print("PASS: async_update_device_info fetches device details")
    return 0


def _test_ohme_client_async_login_success(my_predbat=None):
    """Test async_login with successful authentication"""
    print("**** Running test_ohme_client_async_login_success ****")

    client = MockOhmeApiClient()

    # Mock the session.post for login
    with patch.object(client, '_session', None):
        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=MOCK_LOGIN_RESPONSE)

        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_response
        mock_context.__aexit__.return_value = None

        mock_session.post.return_value = mock_context

        with patch('aiohttp.ClientSession', return_value=mock_session):
            result = run_async(client.async_login())

        assert result == True, f"Expected True, got {result}"
        assert client._token is not None, "Token not set after login"
        assert client._refresh_token is not None, "Refresh token not set after login"

    print("PASS: async_login succeeds with correct credentials")
    return 0


def _test_ohme_client_async_refresh_session_no_token(my_predbat=None):
    """Test _async_refresh_session calls async_login when no token"""
    print("**** Running test_ohme_client_async_refresh_session_no_token ****")

    client = MockOhmeApiClient()
    client._token = None  # No token set

    # Mock async_login
    login_called = []
    async def mock_login():
        login_called.append(True)
        client._token = "new_token"
        client._refresh_token = "new_refresh"
        return True

    client.async_login = mock_login

    result = run_async(client._async_refresh_session())

    assert result == True, f"Expected True, got {result}"
    assert len(login_called) == 1, f"Expected async_login called once, got {len(login_called)}"

    print("PASS: _async_refresh_session calls async_login when no token")
    return 0


def _test_ohme_client_async_refresh_session_recent_token(my_predbat=None):
    """Test _async_refresh_session skips refresh for recent token"""
    print("**** Running test_ohme_client_async_refresh_session_recent_token ****")

    client = MockOhmeApiClient()
    client._token = "existing_token"
    client._refresh_token = "existing_refresh"
    client._token_birth = time.time() - 1000  # 1000 seconds ago (less than 45 mins = 2700s)

    # Mock async_login to ensure it's not called
    login_called = []
    async def mock_login():
        login_called.append(True)
        return True

    client.async_login = mock_login

    result = run_async(client._async_refresh_session())

    assert result == True, f"Expected True, got {result}"
    assert len(login_called) == 0, f"Expected async_login not called, got {len(login_called)}"

    print("PASS: _async_refresh_session skips refresh for recent token")
    return 0


def _test_ohme_client_async_refresh_session_expired_token(my_predbat=None):
    """Test _async_refresh_session refreshes expired token"""
    print("**** Running test_ohme_client_async_refresh_session_expired_token ****")

    client = MockOhmeApiClient()
    client._token = "old_token"
    client._refresh_token = "old_refresh"
    client._token_birth = time.time() - 3000  # 3000 seconds ago (over 45 mins = 2700s)

    # Mock the refresh token response
    mock_refresh_response = {
        "id_token": "refreshed_token",
        "refresh_token": "refreshed_refresh"
    }

    # Mock the session.post for token refresh
    mock_session = MagicMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_refresh_response)

    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_response
    mock_context.__aexit__.return_value = None

    mock_session.post.return_value = mock_context
    client._session = mock_session

    result = run_async(client._async_refresh_session())

    assert result == True, f"Expected True, got {result}"
    assert client._token == "refreshed_token", f"Expected token 'refreshed_token', got {client._token}"
    assert client._refresh_token == "refreshed_refresh", f"Expected refresh token 'refreshed_refresh', got {client._refresh_token}"

    # Verify the POST request was made
    assert mock_session.post.called, "Expected session.post to be called"
    call_args = mock_session.post.call_args
    assert "securetoken.googleapis.com" in call_args[0][0], f"Expected googleapis URL, got {call_args[0][0]}"

    print("PASS: _async_refresh_session refreshes expired token")
    return 0


def _test_ohme_client_async_refresh_session_failure(my_predbat=None):
    """Test _async_refresh_session handles refresh failure"""
    print("**** Running test_ohme_client_async_refresh_session_failure ****")

    client = MockOhmeApiClient()
    client._token = "old_token"
    client._refresh_token = "old_refresh"
    client._token_birth = time.time() - 3000  # Expired token

    # Mock the session.post for failed token refresh
    mock_session = MagicMock()
    mock_response = AsyncMock()
    mock_response.status = 401  # Unauthorized
    mock_response.text = AsyncMock(return_value="Invalid refresh token")

    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_response
    mock_context.__aexit__.return_value = None

    mock_session.post.return_value = mock_context
    client._session = mock_session

    # Should raise AuthException
    try:
        run_async(client._async_refresh_session())
        assert False, "Expected AuthException to be raised"
    except Exception as e:
        from ohme import AuthException
        assert isinstance(e, AuthException), f"Expected AuthException, got {type(e)}"
        assert "Ohme auth refresh" in str(e), f"Expected error message about auth refresh, got {str(e)}"

    print("PASS: _async_refresh_session handles refresh failure")
    return 0


def _test_ohme_client_make_request_get_success(my_predbat=None):
    """Test _make_request GET request returns JSON"""
    print("**** Running test_ohme_client_make_request_get_success ****")

    from unittest.mock import MagicMock, AsyncMock
    from ohme import OhmeApiClient

    # Use real OhmeApiClient (not mock) - need to provide log function
    def mock_log(msg):
        pass

    client = OhmeApiClient(email="test@example.com", password="password", log=mock_log)
    client._token = "test_token"
    client._token_birth = time.time()

    # Mock aiohttp.ClientSession
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"key": "value"})

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)

    mock_session.request.return_value = mock_context
    client._session = mock_session

    # Make GET request
    result = run_async(client._make_request("GET", "/v1/test"))

    # Verify result
    assert result == {"key": "value"}, f"Expected dict response, got {result}"

    # Verify request was made correctly
    mock_session.request.assert_called_once()
    call_args = mock_session.request.call_args
    assert call_args[1]["method"] == "GET"
    assert call_args[1]["url"] == "https://api.ohme.io/v1/test"
    assert call_args[1]["headers"]["Authorization"] == "Firebase test_token"
    assert call_args[1]["headers"]["Content-Type"] == "application/json"

    print("PASS: _make_request GET request returns JSON")
    return 0


def _test_ohme_client_make_request_put_success(my_predbat=None):
    """Test _make_request PUT request returns True"""
    print("**** Running test_ohme_client_make_request_put_success ****")

    from unittest.mock import MagicMock, AsyncMock
    from ohme import OhmeApiClient
    import json

    # Use real OhmeApiClient (not mock) - need to provide log function
    def mock_log(msg):
        pass

    client = OhmeApiClient(email="test@example.com", password="password", log=mock_log)
    client._token = "test_token"
    client._token_birth = time.time()

    # Mock aiohttp.ClientSession
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 200

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)

    mock_session.request.return_value = mock_context
    client._session = mock_session

    # Make PUT request with data
    data = {"setting": "value"}
    result = run_async(client._make_request("PUT", "/v1/test", data=data))

    # Verify result
    assert result is True, f"Expected True for PUT request, got {result}"

    # Verify request was made with correct JSON data
    call_args = mock_session.request.call_args
    assert call_args[1]["method"] == "PUT"
    assert call_args[1]["data"] == json.dumps(data)

    print("PASS: _make_request PUT request returns True")
    return 0


def _test_ohme_client_make_request_post_json(my_predbat=None):
    """Test _make_request POST request returns JSON by default"""
    print("**** Running test_ohme_client_make_request_post_json ****")

    from unittest.mock import MagicMock, AsyncMock
    from ohme import OhmeApiClient
    import json

    # Use real OhmeApiClient (not mock) - need to provide log function
    def mock_log(msg):
        pass

    client = OhmeApiClient(email="test@example.com", password="password", log=mock_log)
    client._token = "test_token"
    client._token_birth = time.time()

    # Mock aiohttp.ClientSession
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"result": "success"})

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)

    mock_session.request.return_value = mock_context
    client._session = mock_session

    # Make POST request with data
    data = {"action": "start"}
    result = run_async(client._make_request("POST", "/v1/test", data=data))

    # Verify result is JSON
    assert result == {"result": "success"}, f"Expected JSON response, got {result}"

    # Verify request was made with correct JSON data
    call_args = mock_session.request.call_args
    assert call_args[1]["method"] == "POST"
    assert call_args[1]["data"] == json.dumps(data)

    print("PASS: _make_request POST request returns JSON by default")
    return 0


def _test_ohme_client_make_request_post_skip_json(my_predbat=None):
    """Test _make_request POST request with skip_json returns text"""
    print("**** Running test_ohme_client_make_request_post_skip_json ****")

    from unittest.mock import MagicMock, AsyncMock
    from ohme import OhmeApiClient

    # Use real OhmeApiClient (not mock) - need to provide log function
    def mock_log(msg):
        pass

    client = OhmeApiClient(email="test@example.com", password="password", log=mock_log)
    client._token = "test_token"
    client._token_birth = time.time()

    # Mock aiohttp.ClientSession
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="OK")

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)

    mock_session.request.return_value = mock_context
    client._session = mock_session

    # Make POST request with skip_json=True
    result = run_async(client._make_request("POST", "/v1/test", skip_json=True))

    # Verify result is text
    assert result == "OK", f"Expected text response 'OK', got {result}"

    print("PASS: _make_request POST request with skip_json returns text")
    return 0


def _test_ohme_client_make_request_api_error(my_predbat=None):
    """Test _make_request raises ApiException on non-200 status"""
    print("**** Running test_ohme_client_make_request_api_error ****")

    from unittest.mock import MagicMock, AsyncMock
    from ohme import OhmeApiClient

    # Use real OhmeApiClient (not mock) - need to provide log function
    def mock_log(msg):
        pass

    client = OhmeApiClient(email="test@example.com", password="password", log=mock_log)
    client._token = "test_token"
    client._token_birth = time.time()

    # Mock aiohttp.ClientSession
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.text = AsyncMock(return_value="Unauthorized")

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)

    mock_session.request.return_value = mock_context
    client._session = mock_session

    # Should raise ApiException
    try:
        run_async(client._make_request("GET", "/v1/test"))
        assert False, "Expected ApiException to be raised"
    except Exception as e:
        from ohme import ApiException
        assert isinstance(e, ApiException), f"Expected ApiException, got {type(e)}"
        assert "401" in str(e), f"Expected status 401 in error, got {str(e)}"
        assert "Unauthorized" in str(e), f"Expected 'Unauthorized' in error, got {str(e)}"

    print("PASS: _make_request raises ApiException on non-200 status")
    return 0


def _test_ohme_client_make_request_creates_session(my_predbat=None):
    """Test _make_request creates session if none exists"""
    print("**** Running test_ohme_client_make_request_creates_session ****")

    from unittest.mock import patch, MagicMock, AsyncMock
    from ohme import OhmeApiClient

    # Use real OhmeApiClient (not mock) - need to provide log function
    def mock_log(msg):
        pass

    client = OhmeApiClient(email="test@example.com", password="password", log=mock_log)
    client._token = "test_token"
    client._token_birth = time.time()
    client._session = None  # No session

    # Mock aiohttp.ClientSession constructor
    mock_session_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={})

    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)

    mock_session_instance.request.return_value = mock_context

    with patch('aiohttp.ClientSession', return_value=mock_session_instance):
        result = run_async(client._make_request("GET", "/v1/test"))

        # Verify session was created
        assert client._session is not None, "Expected session to be created"
        assert client._close_session is True, "Expected close_session to be True"

    print("PASS: _make_request creates session if none exists")
    return 0


def _test_ohme_client_async_get_charge_session_retry(my_predbat=None):
    """Test async_get_charge_session retries on CALCULATING state"""
    print("**** Running test_ohme_client_async_get_charge_session_retry ****")

    client = MockOhmeApiClient()

    # Setup mock responses: first CALCULATING, then normal
    calculating_response = {"mode": "CALCULATING"}
    normal_response = MOCK_CHARGE_SESSION_RESPONSE.copy()

    # Track call count
    call_count = [0]
    original_make_request = client._make_request

    async def mock_make_request_with_retry(method, url, data=None, skip_json=False):
        if url == "/v1/chargeSessions":
            call_count[0] += 1
            if call_count[0] == 1:
                return [calculating_response]
            else:
                return [normal_response]
        return await original_make_request(method, url, data, skip_json)

    client._make_request = mock_make_request_with_retry

    run_async(client.async_get_charge_session())

    # Should have retried at least once
    assert call_count[0] >= 2, f"Expected at least 2 calls, got {call_count[0]}"
    assert client._charge_session["mode"] == "SMART_CHARGE", "Should eventually get normal response"

    print("PASS: async_get_charge_session retries on CALCULATING state")
    return 0


def _test_ohme_client_async_set_mode_max_charge(my_predbat=None):
    """Test async_set_mode with MAX_CHARGE mode"""
    print("**** Running test_ohme_client_async_set_mode_max_charge ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    # Call async_set_mode with MAX_CHARGE
    run_async(client.async_set_mode(ChargerMode.MAX_CHARGE))

    # Check request was made to enable max charge
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "maxCharge=true" in last_request["url"], f"Expected maxCharge=true in URL, got {last_request['url']}"

    print("PASS: async_set_mode correctly enables MAX_CHARGE")
    return 0


def _test_ohme_client_async_set_mode_smart_charge(my_predbat=None):
    """Test async_set_mode with SMART_CHARGE mode"""
    print("**** Running test_ohme_client_async_set_mode_smart_charge ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    # Call async_set_mode with SMART_CHARGE
    run_async(client.async_set_mode(ChargerMode.SMART_CHARGE))

    # Check request was made to disable max charge
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "maxCharge=false" in last_request["url"], f"Expected maxCharge=false in URL, got {last_request['url']}"

    print("PASS: async_set_mode correctly enables SMART_CHARGE")
    return 0


def _test_ohme_client_async_set_mode_paused(my_predbat=None):
    """Test async_set_mode with PAUSED mode"""
    print("**** Running test_ohme_client_async_set_mode_paused ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    # Call async_set_mode with PAUSED
    run_async(client.async_set_mode(ChargerMode.PAUSED))

    # Check request was made to pause charge
    last_request = client.request_log[-1]
    assert last_request["method"] == "POST", f"Expected POST, got {last_request['method']}"
    assert "/stop" in last_request["url"], f"Expected /stop in URL, got {last_request['url']}"

    print("PASS: async_set_mode correctly pauses charge")
    return 0


def _test_ohme_client_async_set_mode_string(my_predbat=None):
    """Test async_set_mode with string mode"""
    print("**** Running test_ohme_client_async_set_mode_string ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    # Call async_set_mode with string mode
    run_async(client.async_set_mode("smart_charge"))

    # Check request was made to disable max charge
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "maxCharge=false" in last_request["url"], f"Expected maxCharge=false in URL, got {last_request['url']}"

    print("PASS: async_set_mode correctly handles string mode")
    return 0


def _test_ohme_client_async_set_vehicle_found(my_predbat=None):
    """Test async_set_vehicle with matching vehicle"""
    print("**** Running test_ohme_client_async_set_vehicle_found ****")

    client = MockOhmeApiClient()
    client._cars = [
        {"id": "car-123", "name": "Tesla Model 3"},
        {"id": "car-456", "make": "Nissan", "model": "Leaf"}
    ]

    # Call async_set_vehicle with matching name
    result = run_async(client.async_set_vehicle("Tesla Model 3"))

    assert result == True, f"Expected True, got {result}"

    # Check request was made to select vehicle
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "/v1/car/car-123/select" in last_request["url"], f"Expected /v1/car/car-123/select in URL, got {last_request['url']}"

    print("PASS: async_set_vehicle correctly selects matching vehicle")
    return 0


def _test_ohme_client_async_set_vehicle_not_found(my_predbat=None):
    """Test async_set_vehicle with non-matching vehicle"""
    print("**** Running test_ohme_client_async_set_vehicle_not_found ****")

    client = MockOhmeApiClient()
    client._cars = [
        {"id": "car-123", "name": "Tesla Model 3"}
    ]

    # Call async_set_vehicle with non-matching name
    result = run_async(client.async_set_vehicle("BMW i3"))

    assert result == False, f"Expected False, got {result}"

    # Check no request was made
    assert len(client.request_log) == 0, f"Expected no requests, got {len(client.request_log)}"

    print("PASS: async_set_vehicle correctly returns False for non-matching vehicle")
    return 0


def _test_ohme_client_async_update_schedule_all_params(my_predbat=None):
    """Test async_update_schedule with all parameters"""
    print("**** Running test_ohme_client_async_update_schedule_all_params ****")

    client = MockOhmeApiClient()
    client._next_session = {
        "id": "rule-123",
        "targetPercent": 80,
        "targetTime": 25200,  # 07:00
        "preconditioningEnabled": False,
        "preconditionLengthMins": 30
    }

    # Call async_update_schedule with all parameters
    result = run_async(client.async_update_schedule(
        target_percent=90,
        target_time=(8, 30),
        pre_condition=True,
        pre_condition_length=45
    ))

    assert result == True, f"Expected True, got {result}"

    # Check rule was updated correctly
    assert client._next_session["targetPercent"] == 90, f"Expected 90%, got {client._next_session['targetPercent']}"
    assert client._next_session["targetTime"] == 30600, f"Expected 30600 seconds (8:30), got {client._next_session['targetTime']}"  # 8*3600 + 30*60
    assert client._next_session["preconditioningEnabled"] == True, f"Expected True, got {client._next_session['preconditioningEnabled']}"
    assert client._next_session["preconditionLengthMins"] == 45, f"Expected 45 mins, got {client._next_session['preconditionLengthMins']}"

    # Check PUT request was made
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "/v1/chargeRules/rule-123" in last_request["url"], f"Expected /v1/chargeRules/rule-123 in URL, got {last_request['url']}"

    print("PASS: async_update_schedule correctly updates all parameters")
    return 0


def _test_ohme_client_async_update_schedule_partial_params(my_predbat=None):
    """Test async_update_schedule with partial parameters"""
    print("**** Running test_ohme_client_async_update_schedule_partial_params ****")

    client = MockOhmeApiClient()
    client._next_session = {
        "id": "rule-456",
        "targetPercent": 80,
        "targetTime": 25200,
        "preconditioningEnabled": False,
        "preconditionLengthMins": 30
    }

    # Call async_update_schedule with only target_percent
    result = run_async(client.async_update_schedule(target_percent=85))

    assert result == True, f"Expected True, got {result}"

    # Check only target_percent was updated
    assert client._next_session["targetPercent"] == 85, f"Expected 85%, got {client._next_session['targetPercent']}"
    assert client._next_session["targetTime"] == 25200, f"Expected 25200 (unchanged), got {client._next_session['targetTime']}"
    assert client._next_session["preconditioningEnabled"] == False, f"Expected False (unchanged), got {client._next_session['preconditioningEnabled']}"

    print("PASS: async_update_schedule correctly updates only provided parameters")
    return 0


def _test_ohme_client_async_update_schedule_no_rule(my_predbat=None):
    """Test async_update_schedule with no rule"""
    print("**** Running test_ohme_client_async_update_schedule_no_rule ****")

    client = MockOhmeApiClient()
    client._next_session = None  # No rule

    # Call async_update_schedule
    result = run_async(client.async_update_schedule(target_percent=90))

    assert result == False, f"Expected False, got {result}"

    # Check no request was made
    assert len(client.request_log) == 0, f"Expected no requests, got {len(client.request_log)}"

    print("PASS: async_update_schedule correctly returns False when no rule exists")
    return 0


# ============================================================================
# OhmeAPI Component Tests
# ============================================================================

class MockOhmeAPI(OhmeAPI):
    """Mock OhmeAPI for testing publish_data without ComponentBase dependencies"""

    def __init__(self):
        """Initialize mock without calling parent __init__"""
        # Don't call parent __init__ to avoid ComponentBase initialization
        self.email = "test@example.com"
        self.password = "test_password"
        self.log_messages = []
        self.dashboard_items = {}
        self.queued_events = []
        self.ohme_automatic_octopus_intelligent = False

        # Create mock client
        self.client = MockOhmeApiClient()

    def log(self, message):
        """Mock log function"""
        self.log_messages.append(message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Mock dashboard_item to capture published entities"""
        self.dashboard_items[entity_id] = {
            "state": state,
            "attributes": attributes,
            "app": app
        }


def _test_ohme_publish_data(my_predbat=None):
    """Test publish_data publishes all entities correctly"""
    print("**** Running test_ohme_publish_data ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200, "amp": 32, "volt": 230},
        "appliedRule": {"targetPercent": 80, "targetTime": 25200},  # 07:00
        "batterySoc": {"wh": 15000, "percent": 75},
        "allSessionSlots": [
            {
                "startTimeMs": int((datetime.datetime.now() - datetime.timedelta(minutes=30)).timestamp() * 1000),
                "endTimeMs": int((datetime.datetime.now() + datetime.timedelta(minutes=30)).timestamp() * 1000),
                "watts": 7200
            },
            {
                "startTimeMs": int((datetime.datetime.now() + datetime.timedelta(hours=1)).timestamp() * 1000),
                "endTimeMs": int((datetime.datetime.now() + datetime.timedelta(hours=2)).timestamp() * 1000),
                "watts": 7200
            }
        ]
    }
    api.client._advanced_settings = {"online": True, "clampAmps": 30.5}
    api.client._next_session = {"targetPercent": 80, "targetTime": 25200, "preconditioningEnabled": True, "preconditionLengthMins": 30}
    api.client._last_rule = {"preconditioningEnabled": True, "preconditionLengthMins": 30}
    api.client._cars = [{"name": "Tesla Model 3"}]

    # Set energy and battery directly (these are normally set in async_get_charge_session)
    api.client.energy = 15000.0  # Energy in Wh
    api.client.battery = 75  # Battery percent

    # Call publish_data
    run_async(api.publish_data())

    # Verify all expected entities were published
    expected_entities = [
        "sensor.predbat_ohme_mode",
        "sensor.predbat_ohme_status",
        "sensor.predbat_ohme_power_watts",
        "sensor.predbat_ohme_power_amps",
        "sensor.predbat_ohme_power_volts",
        "switch.predbat_ohme_max_charge",
        "binary_sensor.predbat_ohme_available",
        "number.predbat_ohme_target_percent",
        "select.predbat_ohme_target_time",
        "number.predbat_ohme_preconditioning",
        "binary_sensor.predbat_ohme_slot_active",
        "sensor.predbat_ohme_energy",
        "sensor.predbat_ohme_battery_percent",
        "sensor.predbat_ohme_current_vehicle",
        "switch.predbat_ohme_approve_charge"
    ]

    for entity in expected_entities:
        assert entity in api.dashboard_items, f"Entity {entity} not published"

    # Verify mode and status
    assert api.dashboard_items["sensor.predbat_ohme_mode"]["state"] == "smart_charge", \
        f"Expected mode 'smart_charge', got {api.dashboard_items['sensor.predbat_ohme_mode']['state']}"
    assert api.dashboard_items["sensor.predbat_ohme_status"]["state"] == "charging", \
        f"Expected status 'charging', got {api.dashboard_items['sensor.predbat_ohme_status']['state']}"

    # Verify power data
    assert api.dashboard_items["sensor.predbat_ohme_power_watts"]["state"] == 7200, \
        f"Expected 7200W, got {api.dashboard_items['sensor.predbat_ohme_power_watts']['state']}"
    assert api.dashboard_items["sensor.predbat_ohme_power_amps"]["state"] == 32, \
        f"Expected 32A, got {api.dashboard_items['sensor.predbat_ohme_power_amps']['state']}"
    assert api.dashboard_items["sensor.predbat_ohme_power_volts"]["state"] == 230, \
        f"Expected 230V, got {api.dashboard_items['sensor.predbat_ohme_power_volts']['state']}"

    # Verify target data
    assert api.dashboard_items["number.predbat_ohme_target_percent"]["state"] == 80, \
        f"Expected target 80%, got {api.dashboard_items['number.predbat_ohme_target_percent']['state']}"
    assert api.dashboard_items["select.predbat_ohme_target_time"]["state"] == "07:00", \
        f"Expected target time '07:00', got {api.dashboard_items['select.predbat_ohme_target_time']['state']}"

    # Verify preconditioning
    assert api.dashboard_items["number.predbat_ohme_preconditioning"]["state"] == 30, \
        f"Expected preconditioning 30 mins, got {api.dashboard_items['number.predbat_ohme_preconditioning']['state']}"

    # Verify slot_active (should be True since we have an active slot)
    assert api.dashboard_items["binary_sensor.predbat_ohme_slot_active"]["state"] == True, \
        f"Expected slot_active True, got {api.dashboard_items['binary_sensor.predbat_ohme_slot_active']['state']}"

    # Verify planned_dispatches attribute exists
    slot_attributes = api.dashboard_items["binary_sensor.predbat_ohme_slot_active"]["attributes"]
    assert "planned_dispatches" in slot_attributes, "planned_dispatches not in slot attributes"
    assert "completed_dispatches" in slot_attributes, "completed_dispatches not in slot attributes"

    # Verify energy and battery
    assert api.dashboard_items["sensor.predbat_ohme_energy"]["state"] == 15000, \
        f"Expected energy 15000Wh, got {api.dashboard_items['sensor.predbat_ohme_energy']['state']}"
    assert api.dashboard_items["sensor.predbat_ohme_battery_percent"]["state"] == 75, \
        f"Expected battery 75%, got {api.dashboard_items['sensor.predbat_ohme_battery_percent']['state']}"

    # Verify vehicle
    assert api.dashboard_items["sensor.predbat_ohme_current_vehicle"]["state"] == "Tesla Model 3", \
        f"Expected vehicle 'Tesla Model 3', got {api.dashboard_items['sensor.predbat_ohme_current_vehicle']['state']}"

    # Verify all entities have the correct app
    for entity_id, data in api.dashboard_items.items():
        assert data["app"] == "ohme", f"Entity {entity_id} has wrong app: {data['app']}"

    print("PASS: publish_data correctly publishes all entities")
    return 0


def _test_ohme_publish_data_disconnected(my_predbat=None):
    """Test publish_data when charger is disconnected"""
    print("**** Running test_ohme_publish_data_disconnected ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with disconnected state
    api.client._charge_session = {"mode": "DISCONNECTED", "allSessionSlots": []}
    api.client._advanced_settings = {"online": False}
    api.client._next_session = {"targetPercent": 80, "targetTime": 25200}
    api.client._last_rule = {}
    api.client._cars = []
    api.client.energy = 0
    api.client.battery = 0

    # Call publish_data
    run_async(api.publish_data())

    # Verify mode is "disconnected"
    assert api.dashboard_items["sensor.predbat_ohme_mode"]["state"] == "disconnected", \
        f"Expected mode 'disconnected', got {api.dashboard_items['sensor.predbat_ohme_mode']['state']}"

    # Verify status is "unplugged"
    assert api.dashboard_items["sensor.predbat_ohme_status"]["state"] == "unplugged", \
        f"Expected status 'unplugged', got {api.dashboard_items['sensor.predbat_ohme_status']['state']}"

    # Verify slot_active is False (no slots)
    assert api.dashboard_items["binary_sensor.predbat_ohme_slot_active"]["state"] == False, \
        f"Expected slot_active False, got {api.dashboard_items['binary_sensor.predbat_ohme_slot_active']['state']}"

    # Verify available is "off"
    assert api.dashboard_items["binary_sensor.predbat_ohme_available"]["state"] == "off", \
        f"Expected available 'off', got {api.dashboard_items['binary_sensor.predbat_ohme_available']['state']}"

    print("PASS: publish_data correctly handles disconnected state")
    return 0


def _test_ohme_run_first_call(my_predbat=None):
    """Test run method on first call"""
    print("**** Running test_ohme_run_first_call ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"

    # Track method calls
    update_device_called = []
    get_session_called = []
    publish_data_called = []
    update_success_called = []

    async def mock_update_device():
        update_device_called.append(True)

    async def mock_get_session():
        get_session_called.append(True)

    async def mock_publish_data():
        publish_data_called.append(True)

    def mock_update_success():
        update_success_called.append(True)

    # Replace methods with mocks
    api.client.async_update_device_info = mock_update_device
    api.client.async_get_charge_session = mock_get_session
    api.publish_data = mock_publish_data
    api.update_success_timestamp = mock_update_success

    # Call run with first=True
    result = run_async(api.run(seconds=0, first=True))

    assert result == True, f"Expected True, got {result}"

    # Verify first call log
    assert any("Ohme API: Started" in msg for msg in api.log_messages), "Expected 'Started' log message"

    # Verify methods were called
    assert len(update_device_called) == 1, f"Expected async_update_device_info called once, got {len(update_device_called)}"
    assert len(get_session_called) == 1, f"Expected async_get_charge_session called once, got {len(get_session_called)}"
    assert len(publish_data_called) == 1, f"Expected publish_data called once, got {len(publish_data_called)}"
    assert len(update_success_called) == 1, f"Expected update_success_timestamp called once, got {len(update_success_called)}"

    print("PASS: run correctly handles first call")
    return 0


def _test_ohme_run_periodic_30min(my_predbat=None):
    """Test run method on 30-minute periodic call"""
    print("**** Running test_ohme_run_periodic_30min ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"

    # Track method calls
    update_device_called = []
    get_session_called = []
    publish_data_called = []

    async def mock_update_device():
        update_device_called.append(True)

    async def mock_get_session():
        get_session_called.append(True)

    async def mock_publish_data():
        publish_data_called.append(True)

    # Replace methods with mocks
    api.client.async_update_device_info = mock_update_device
    api.client.async_get_charge_session = mock_get_session
    api.publish_data = mock_publish_data
    api.update_success_timestamp = lambda: None

    # Call run with seconds=1800 (30 minutes)
    result = run_async(api.run(seconds=1800, first=False))

    assert result == True, f"Expected True, got {result}"

    # Verify async_update_device_info was called (30 min = 30*60 = 1800 seconds)
    assert len(update_device_called) == 1, f"Expected async_update_device_info called once, got {len(update_device_called)}"

    # Verify async_get_charge_session was called (120 seconds also divides 1800)
    assert len(get_session_called) == 1, f"Expected async_get_charge_session called once, got {len(get_session_called)}"
    assert len(publish_data_called) == 1, f"Expected publish_data called once, got {len(publish_data_called)}"

    print("PASS: run correctly handles 30-minute periodic call")
    return 0


def _test_ohme_run_periodic_120s(my_predbat=None):
    """Test run method on 120-second periodic call"""
    print("**** Running test_ohme_run_periodic_120s ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"

    # Track method calls
    update_device_called = []
    get_session_called = []
    publish_data_called = []

    async def mock_update_device():
        update_device_called.append(True)

    async def mock_get_session():
        get_session_called.append(True)

    async def mock_publish_data():
        publish_data_called.append(True)

    # Replace methods with mocks
    api.client.async_update_device_info = mock_update_device
    api.client.async_get_charge_session = mock_get_session
    api.publish_data = mock_publish_data
    api.update_success_timestamp = lambda: None

    # Call run with seconds=120 (2 minutes)
    result = run_async(api.run(seconds=120, first=False))

    assert result == True, f"Expected True, got {result}"

    # Verify async_update_device_info was NOT called (120 doesn't divide 1800)
    assert len(update_device_called) == 0, f"Expected async_update_device_info not called, got {len(update_device_called)}"

    # Verify async_get_charge_session WAS called (120 divides 120)
    assert len(get_session_called) == 1, f"Expected async_get_charge_session called once, got {len(get_session_called)}"
    assert len(publish_data_called) == 1, f"Expected publish_data called once, got {len(publish_data_called)}"

    print("PASS: run correctly handles 120-second periodic call")
    return 0


def _test_ohme_run_no_periodic(my_predbat=None):
    """Test run method on non-periodic call"""
    print("**** Running test_ohme_run_no_periodic ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"

    # Track method calls
    update_device_called = []
    get_session_called = []
    publish_data_called = []

    async def mock_update_device():
        update_device_called.append(True)

    async def mock_get_session():
        get_session_called.append(True)

    async def mock_publish_data():
        publish_data_called.append(True)

    # Replace methods with mocks
    api.client.async_update_device_info = mock_update_device
    api.client.async_get_charge_session = mock_get_session
    api.publish_data = mock_publish_data
    api.update_success_timestamp = lambda: None

    # Call run with seconds=60 (doesn't trigger periodic updates)
    result = run_async(api.run(seconds=60, first=False))

    assert result == True, f"Expected True, got {result}"

    # Verify no periodic methods were called
    assert len(update_device_called) == 0, f"Expected async_update_device_info not called, got {len(update_device_called)}"
    assert len(get_session_called) == 0, f"Expected async_get_charge_session not called, got {len(get_session_called)}"
    assert len(publish_data_called) == 0, f"Expected publish_data not called, got {len(publish_data_called)}"

    print("PASS: run correctly skips updates on non-periodic call")
    return 0


def _test_ohme_run_with_queued_events(my_predbat=None):
    """Test run method with queued events"""
    print("**** Running test_ohme_run_with_queued_events ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"

    # Track method calls
    handler_called = []
    update_device_called = []
    get_session_called = []
    publish_data_called = []

    async def mock_handler(entity_id, value):
        handler_called.append((entity_id, value))

    async def mock_update_device():
        update_device_called.append(True)

    async def mock_get_session():
        get_session_called.append(True)

    async def mock_publish_data():
        publish_data_called.append(True)

    # Replace methods with mocks
    api.client.async_update_device_info = mock_update_device
    api.client.async_get_charge_session = mock_get_session
    api.publish_data = mock_publish_data
    api.update_success_timestamp = lambda: None

    # Add queued event
    api.queued_events.append((mock_handler, "test.entity", 42))

    # Call run with seconds=60 (normally wouldn't trigger updates)
    result = run_async(api.run(seconds=60, first=False))

    assert result == True, f"Expected True, got {result}"

    # Verify handler was called
    assert len(handler_called) == 1, f"Expected handler called once, got {len(handler_called)}"
    assert handler_called[0] == ("test.entity", 42), f"Expected handler args ('test.entity', 42), got {handler_called[0]}"

    # Verify queued events were cleared
    assert len(api.queued_events) == 0, f"Expected queued_events cleared, got {len(api.queued_events)}"

    # Verify refresh triggered updates (because refresh=True after processing events)
    assert len(update_device_called) == 1, f"Expected async_update_device_info called due to refresh, got {len(update_device_called)}"
    assert len(get_session_called) == 1, f"Expected async_get_charge_session called due to refresh, got {len(get_session_called)}"

    print("PASS: run correctly processes queued events and triggers refresh")
    return 0


def _test_ohme_run_event_handler_exception(my_predbat=None):
    """Test run method handles event handler exceptions"""
    print("**** Running test_ohme_run_event_handler_exception ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"

    async def mock_handler_that_fails():
        from ohme import ApiException
        raise ApiException("Test error")

    async def mock_async_noop():
        pass

    # Replace methods with mocks
    api.client.async_update_device_info = mock_async_noop
    api.client.async_get_charge_session = mock_async_noop
    api.publish_data = mock_async_noop
    api.update_success_timestamp = lambda: None

    # Add queued event that will fail
    api.queued_events.append((mock_handler_that_fails,))

    # Call run - should not raise exception
    result = run_async(api.run(seconds=60, first=False))

    assert result == True, f"Expected True, got {result}"

    # Verify error was logged
    assert any("Event handler error" in msg for msg in api.log_messages), "Expected 'Event handler error' log message"

    print("PASS: run correctly handles event handler exceptions")
    return 0


def _test_ohme_run_first_with_octopus_intelligent(my_predbat=None):
    """Test run method on first call with octopus intelligent enabled"""
    print("**** Running test_ohme_run_first_with_octopus_intelligent ****")

    # Create mock OhmeAPI with tracking
    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    api.ohme_automatic_octopus_intelligent = True

    # Track method calls
    auto_config_called = []
    set_arg_calls = []

    async def mock_auto_config():
        auto_config_called.append(True)

    def mock_set_arg(key, value):
        set_arg_calls.append((key, value))

    async def mock_async_noop():
        pass

    # Replace methods with mocks
    api.client.async_update_device_info = mock_async_noop
    api.client.async_get_charge_session = mock_async_noop
    api.publish_data = mock_async_noop
    api.update_success_timestamp = lambda: None
    api.automatic_config_octopus_intelligent = mock_auto_config

    # Call run with first=True
    result = run_async(api.run(seconds=0, first=True))

    assert result == True, f"Expected True, got {result}"

    # Verify automatic_config_octopus_intelligent was called
    assert len(auto_config_called) == 1, f"Expected automatic_config_octopus_intelligent called once, got {len(auto_config_called)}"

    print("PASS: run correctly handles first call with octopus intelligent")
    return 0


# ============================================================================
# Event Handler Tests
# ============================================================================

def _test_ohme_select_event_handler_target_time(my_predbat=None):
    """Test select_event_handler for target_time"""
    print("**** Running test_ohme_select_event_handler_target_time ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"targetTime": 25200}  # 07:00

    # Call select_event_handler with valid target time
    run_async(api.select_event_handler("select.predbat_ohme_target_time", "08:30"))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PUT", f"Expected PUT request, got {request['method']}"
    assert "/v1/chargeSessions/" in request["url"], f"Expected chargeSessions URL, got {request['url']}"
    assert "targetTs=" in request["url"], f"Expected targetTs in URL, got {request['url']}"

    # Verify log message
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Set target time to 08:30" in api.log_messages[0], f"Expected target time log, got {api.log_messages[0]}"

    print("PASS: select_event_handler correctly handles target_time")
    return 0


def _test_ohme_select_event_handler_invalid_time(my_predbat=None):
    """Test select_event_handler with invalid target_time"""
    print("**** Running test_ohme_select_event_handler_invalid_time ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"targetTime": 25200}

    # Call select_event_handler with invalid target time (not in OPTIONS_TIME)
    run_async(api.select_event_handler("select.predbat_ohme_target_time", "25:99"))

    # Verify no request was made
    assert len(api.client.request_log) == 0, f"Expected 0 requests, got {len(api.client.request_log)}"

    # Verify warning log message
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Invalid target time value" in api.log_messages[0], f"Expected invalid time warning, got {api.log_messages[0]}"

    print("PASS: select_event_handler correctly rejects invalid target_time")
    return 0


def _test_ohme_number_event_handler_target_soc(my_predbat=None):
    """Test number_event_handler for target_soc"""
    print("**** Running test_ohme_number_event_handler_target_soc ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"targetPercent": 80}

    # Call number_event_handler with valid target SoC
    run_async(api.number_event_handler("number.predbat_ohme_target_soc", 90))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PUT", f"Expected PUT request, got {request['method']}"
    assert "toPercent=90" in request["url"], f"Expected toPercent=90 in URL, got {request['url']}"

    print("PASS: number_event_handler correctly handles target_soc")
    return 0


def _test_ohme_number_event_handler_target_soc_invalid(my_predbat=None):
    """Test number_event_handler with invalid target_soc"""
    print("**** Running test_ohme_number_event_handler_target_soc_invalid ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"targetPercent": 80}

    # Test with value > 100
    run_async(api.number_event_handler("number.predbat_ohme_target_soc", 150))

    # Verify no request was made
    assert len(api.client.request_log) == 0, f"Expected 0 requests, got {len(api.client.request_log)}"

    # Verify warning log
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Invalid target SoC value" in api.log_messages[0], f"Expected invalid SoC warning, got {api.log_messages[0]}"

    print("PASS: number_event_handler correctly rejects invalid target_soc")
    return 0


def _test_ohme_number_event_handler_preconditioning(my_predbat=None):
    """Test number_event_handler for preconditioning"""
    print("**** Running test_ohme_number_event_handler_preconditioning ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"preconditioningEnabled": False, "preconditionLengthMins": 30}

    # Call number_event_handler with preconditioning length
    run_async(api.number_event_handler("number.predbat_ohme_preconditioning", 45))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PUT", f"Expected PUT request, got {request['method']}"
    assert "preconditionLengthMins=45" in request["url"], f"Expected preconditionLengthMins=45, got {request['url']}"
    assert "enablePreconditioning=true" in request["url"], f"Expected enablePreconditioning=true, got {request['url']}"

    # Verify log message
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Set preconditioning length to 45 mins" in api.log_messages[0], f"Expected preconditioning log, got {api.log_messages[0]}"

    print("PASS: number_event_handler correctly handles preconditioning")
    return 0


def _test_ohme_number_event_handler_preconditioning_off(my_predbat=None):
    """Test number_event_handler for preconditioning set to 0 (off)"""
    print("**** Running test_ohme_number_event_handler_preconditioning_off ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"preconditioningEnabled": True, "preconditionLengthMins": 30}

    # Call number_event_handler with 0 to disable preconditioning
    run_async(api.number_event_handler("number.predbat_ohme_preconditioning", 0))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"

    # Verify log message
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Set preconditioning to off" in api.log_messages[0], f"Expected preconditioning off log, got {api.log_messages[0]}"

    print("PASS: number_event_handler correctly handles preconditioning off")
    return 0


def _test_ohme_number_event_handler_preconditioning_invalid(my_predbat=None):
    """Test number_event_handler with invalid preconditioning value"""
    print("**** Running test_ohme_number_event_handler_preconditioning_invalid ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}
    api.client._last_rule = {"preconditioningEnabled": False}

    # Call number_event_handler with invalid value
    run_async(api.number_event_handler("number.predbat_ohme_preconditioning", "invalid"))

    # Verify no request was made
    assert len(api.client.request_log) == 0, f"Expected 0 requests, got {len(api.client.request_log)}"

    # Verify warning log
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Invalid preconditioning value" in api.log_messages[0], f"Expected invalid value warning, got {api.log_messages[0]}"

    print("PASS: number_event_handler correctly rejects invalid preconditioning")
    return 0


def _test_ohme_switch_event_handler_max_charge_on(my_predbat=None):
    """Test switch_event_handler for max_charge turn_on"""
    print("**** Running test_ohme_switch_event_handler_max_charge_on ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "SMART_CHARGE"}

    # Call switch_event_handler to turn on max charge
    run_async(api.switch_event_handler("switch.predbat_ohme_max_charge", "turn_on"))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PUT", f"Expected PUT request, got {request['method']}"
    assert "maxCharge=true" in request["url"], f"Expected maxCharge=true, got {request['url']}"

    print("PASS: switch_event_handler correctly handles max_charge turn_on")
    return 0


def _test_ohme_switch_event_handler_max_charge_off(my_predbat=None):
    """Test switch_event_handler for max_charge turn_off"""
    print("**** Running test_ohme_switch_event_handler_max_charge_off ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data
    api.client._charge_session = {"mode": "MAX_CHARGE"}

    # Call switch_event_handler to turn off max charge
    run_async(api.switch_event_handler("switch.predbat_ohme_max_charge", "turn_off"))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PUT", f"Expected PUT request, got {request['method']}"
    assert "maxCharge=false" in request["url"], f"Expected maxCharge=false, got {request['url']}"

    print("PASS: switch_event_handler correctly handles max_charge turn_off")
    return 0


def _test_ohme_switch_event_handler_approve_charge(my_predbat=None):
    """Test switch_event_handler for approve_charge"""
    print("**** Running test_ohme_switch_event_handler_approve_charge ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data - status must be PENDING_APPROVAL
    api.client._charge_session = {"mode": "PENDING_APPROVAL"}

    # Call switch_event_handler to approve charge
    run_async(api.switch_event_handler("switch.predbat_ohme_approve_charge", "turn_on"))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PUT", f"Expected PUT request, got {request['method']}"
    assert "approve?approve=true" in request["url"], f"Expected approve?approve=true, got {request['url']}"

    # Verify log message
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "Approved charge" in api.log_messages[0], f"Expected approved charge log, got {api.log_messages[0]}"

    print("PASS: switch_event_handler correctly handles approve_charge")
    return 0


def _test_ohme_switch_event_handler_approve_charge_wrong_status(my_predbat=None):
    """Test switch_event_handler for approve_charge with wrong status"""
    print("**** Running test_ohme_switch_event_handler_approve_charge_wrong_status ****")

    # Create mock OhmeAPI
    api = MockOhmeAPI()

    # Setup client with test data - status is NOT PENDING_APPROVAL
    api.client._charge_session = {"mode": "SMART_CHARGE", "power": {"watt": 7200}}

    # Call switch_event_handler to approve charge
    run_async(api.switch_event_handler("switch.predbat_ohme_approve_charge", "turn_on"))

    # Verify no request was made
    assert len(api.client.request_log) == 0, f"Expected 0 requests, got {len(api.client.request_log)}"

    # Verify warning log
    assert len(api.log_messages) == 1, f"Expected 1 log message, got {len(api.log_messages)}"
    assert "not pending approval" in api.log_messages[0], f"Expected not pending approval warning, got {api.log_messages[0]}"

    print("PASS: switch_event_handler correctly rejects approve_charge when not pending")
    return 0
