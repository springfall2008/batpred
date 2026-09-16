# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for Ohme EV charger integration
"""

import datetime
import time
import unittest.mock
import pytz
from unittest.mock import patch, MagicMock, AsyncMock
from typing import Dict, Optional
from tests.test_infra import run_async

# Import ohme module components
import sys
import os

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ohme import (
    CAR_DISCOVERY_ENTITY_SPEC,
    CHARGER_DISCOVERY_ENTITY_SPEC,
    ENERGY_TODAY_ENTITY,
    MAX_ENERGY_GAP_SECONDS,
    POWER_WATTS_ENTITY,
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
        ("energy_steady", _test_ohme_energy_today_steady_power, "energy_today integrates steady power"),
        ("energy_left_sum", _test_ohme_energy_today_left_riemann, "energy_today uses a left Riemann sum"),
        ("energy_gap", _test_ohme_energy_today_long_gap, "energy_today skips over-long gaps"),
        ("energy_midnight", _test_ohme_energy_today_midnight_rollover, "energy_today resets at midnight"),
        ("energy_gap_midnight", _test_ohme_energy_today_gap_across_midnight, "energy_today rolls the day over a long gap"),
        ("energy_restore", _test_ohme_energy_today_restore, "energy_today restores across a restart"),
        ("energy_restore_stale", _test_ohme_energy_today_restore_stale, "energy_today ignores a stale total"),
        ("energy_published", _test_ohme_energy_today_published, "publish_data publishes energy_today"),
        ("energy_timezone", _test_ohme_energy_today_uses_configured_timezone, "energy_today follows the configured timezone"),
        ("auto_config_energy", _test_ohme_auto_config_wires_car_charging_energy, "auto config wires car_charging_energy"),
        ("iog_flag_forces_on", _test_ohme_iog_flag_forces_on, "explicit flag forces Intelligent on"),
        ("iog_flag_forces_off", _test_ohme_iog_flag_forces_off, "explicit flag forces Intelligent off"),
        ("iog_autodetect", _test_ohme_iog_autodetected_from_octopus, "Intelligent auto-detected from Octopus"),
        ("iog_autodetect_gated", _test_ohme_iog_autodetect_needs_ohme_automatic, "auto-detect needs ohme_automatic"),
        ("iog_claims_slots", _test_ohme_iog_claims_car_slots, "Intelligent wiring claims the car slots"),
        ("connected_sensor", _test_ohme_connected_sensor, "connected binary sensor tracks plug state"),
        ("control_enable", _test_ohme_control_enable_rules, "ohme_control enable rules"),
        ("control_windows", _test_ohme_control_window_parsing, "control window parsing"),
        ("control_midnight", _test_ohme_control_window_year_rollover, "control windows across new year"),
        ("control_startup", _test_ohme_control_waits_for_plan, "control waits for a published plan"),
        ("control_edges", _test_ohme_control_edge_triggered, "control only acts on transitions"),
        ("control_drift", _test_ohme_control_reapplies_on_drift, "control re-applies after app changes"),
        ("control_read_only", _test_ohme_control_read_only_release, "read only releases the charger"),
        ("control_read_only_src", _test_ohme_control_read_only_effective, "read only uses the effective state"),
        ("control_target_restore", _test_ohme_control_restores_target, "release restores the charger target"),
        ("auto_config_keeps", _test_ohme_auto_config_keeps_existing_car_charging_energy, "auto config keeps a real charger sensor"),
        ("auto_config_power", _test_ohme_auto_config_wires_car_charging_power, "auto config wires car_charging_power"),
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
        ("number_entities_wired", _test_ohme_number_event_handler_matches_published_entities, "published number entities are all handled"),
        ("number_target_soc_invalid", _test_ohme_number_event_handler_target_soc_invalid, "number_event_handler invalid SoC"),
        ("number_preconditioning", _test_ohme_number_event_handler_preconditioning, "number_event_handler preconditioning"),
        ("number_preconditioning_off", _test_ohme_number_event_handler_preconditioning_off, "number_event_handler preconditioning off"),
        ("number_preconditioning_invalid", _test_ohme_number_event_handler_preconditioning_invalid, "number_event_handler invalid preconditioning"),
        ("switch_max_charge_on", _test_ohme_switch_event_handler_max_charge_on, "switch_event_handler max_charge on"),
        ("switch_max_charge_off", _test_ohme_switch_event_handler_max_charge_off, "switch_event_handler max_charge off"),
        ("switch_approve_charge", _test_ohme_switch_event_handler_approve_charge, "switch_event_handler approve_charge"),
        ("switch_approve_wrong_status", _test_ohme_switch_event_handler_approve_charge_wrong_status, "switch_event_handler approve wrong status"),
        ("discovery_split", _test_ohme_build_discovery_charger_and_car_split, "build_discovery produces cross-linked charger and car records"),
        ("discovery_stub_car", _test_ohme_build_discovery_stub_car_when_no_vehicle, "build_discovery stubs the car when no vehicle is known"),
        ("discovery_known_vehicle", _test_ohme_build_discovery_known_vehicle, "build_discovery reports a genuine car identity when a vehicle is known"),
        ("discovery_vehicle_real_parse", _test_ohme_build_discovery_vehicle_from_real_client_parse, "the non-stub car path works against the real client parsing pipeline"),
        ("discovery_entity_split", _test_ohme_build_discovery_entities_split_by_record, "charger and car entities land in the correct record"),
        ("discovery_entities_partial", _test_ohme_build_discovery_entities_only_when_published, "only published entities are reported"),
        ("discovery_no_max_power", _test_ohme_build_discovery_omits_unknown_ratings, "build_discovery reports no ratings when none are genuinely known"),
        ("discovery_automatic_flag", _test_ohme_build_discovery_records_automatic_flag, "build_discovery records self.ohme_automatic regardless of its value"),
        ("discovery_marker_incomplete", _test_ohme_discovery_report_not_advanced_while_entities_incomplete, "the reported-marker does not advance while entities are incomplete"),
        ("discovery_failure_retry", _test_ohme_discovery_report_failure_contained_and_retried, "a build_discovery failure is contained and retried"),
        ("discovery_run_unconditional", _test_ohme_discovery_report_retried_via_unconditional_run_call, "a first-cycle failure is retried by run()'s unconditional call"),
        ("discovery_automatic_false_run", _test_ohme_discovery_report_produced_when_automatic_false, "run() reports to the catalogue even when ohme_automatic is False"),
        ("discovery_no_serial_yet", _test_ohme_discovery_report_skipped_before_serial_known, "run() does not report before the client's serial is known"),
        ("discovery_round_trip", _test_ohme_build_discovery_round_trips_through_coordinator_and_redaction, "build_discovery round-trips through the real Coordinator and Redactor"),
        ("discovery_no_email_leak", _test_ohme_build_discovery_never_leaks_login_email, "the Ohme login email never enters the discovery report in any form"),
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

    # Pin to 14:00 so "2 hours from now" (16:00) is unambiguously today
    fixed_now = datetime.datetime(2026, 3, 15, 14, 0, 0)
    future_time = fixed_now + datetime.timedelta(hours=2)

    with unittest.mock.patch("ohme.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = fixed_now
        mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
        mock_dt.timedelta = datetime.timedelta
        result = time_next_occurs(future_time.hour, future_time.minute)

    assert result.date() == fixed_now.date(), f"Expected date {fixed_now.date()}, got {result.date()}"
    assert result.hour == future_time.hour, f"Expected hour {future_time.hour}, got {result.hour}"
    assert result.minute == future_time.minute, f"Expected minute {future_time.minute}, got {result.minute}"

    print("PASS: time_next_occurs correctly returns future time today")
    return 0


def _test_ohme_time_next_occurs_tomorrow(my_predbat=None):
    """Test time_next_occurs for a time that should be tomorrow"""
    print("**** Running test_ohme_time_next_occurs_tomorrow ****")

    # Pin to 14:00 so "1 hour ago" (13:00) is unambiguously in the past
    fixed_now = datetime.datetime(2026, 3, 15, 14, 0, 0)
    past_time = fixed_now - datetime.timedelta(hours=1)

    with unittest.mock.patch("ohme.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = fixed_now
        mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
        mock_dt.timedelta = datetime.timedelta
        result = time_next_occurs(past_time.hour, past_time.minute)

    expected_date = (fixed_now + datetime.timedelta(days=1)).date()
    assert result.date() == expected_date, f"Expected date {expected_date}, got {result.date()}"
    assert result.hour == past_time.hour, f"Expected hour {past_time.hour}, got {result.hour}"
    assert result.minute == past_time.minute, f"Expected minute {past_time.minute}, got {result.minute}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert last_request["method"] == "PUT", f"Expected PUT, got {last_request['method']}"
    assert "enabled=true" in last_request["url"], f"Expected enabled=true in URL, got {last_request['url']}"

    print("PASS: async_max_charge enables max charge")
    return 0


def _test_ohme_client_async_max_charge_disable(my_predbat=None):
    """Test async_max_charge disables max charge"""
    print("**** Running test_ohme_client_async_max_charge_disable ****")

    client = MockOhmeApiClient()
    client.serial = "TEST-SERIAL-123"

    result = run_async(client.async_max_charge(False))

    assert result is True, f"Expected True, got {result}"

    # Check request was logged
    last_request = client.request_log[-1]
    assert "enabled=false" in last_request["url"], f"Expected enabled=false in URL, got {last_request['url']}"

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
    client._last_rule = {"id": "RULE-456", "targetPercent": 80, "targetTime": 25200, "preconditioningEnabled": False}

    result = run_async(client.async_set_target(target_percent=90, target_time=(8, 30)))

    assert result is True, f"Expected True, got {result}"

    # Check request was logged - a PATCH against the rule id from _last_rule, with the changed
    # fields in the JSON body rather than the URL (#4719: the old PUT-with-query-params rule
    # endpoint was withdrawn upstream)
    last_request = client.request_log[-1]
    assert last_request["method"] == "PATCH", f"Expected PATCH, got {last_request['method']}"
    assert "/v2/users/me/charge-rules/RULE-456" in last_request["url"], f"Expected the rule id in the URL, got {last_request['url']}"
    assert last_request["data"]["targetPercent"] == 90, f"Expected targetPercent=90 in the body, got {last_request['data']}"
    assert last_request["data"]["targetTime"] == 30600, f"Expected targetTime=30600 in the body, got {last_request['data']}"

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

    assert result is True, f"Expected True, got {result}"

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

        assert result is True, f"Expected True, got {result}"
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

    assert result is True, f"Expected True, got {result}"
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

    assert result is True, f"Expected True, got {result}"
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

    assert result is True, f"Expected True, got {result}"
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
        run_async(client._make_request("GET", "/v1/test"))

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

    # async_get_charge_session waits a real second between attempts to give the charger time to
    # leave CALCULATING. What is under test is that it retries at all, not how long it pauses
    # first, so the wait is skipped rather than served.
    with patch("ohme.asyncio.sleep", new_callable=AsyncMock):
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
    assert "enabled=true" in last_request["url"], f"Expected enabled=true in URL, got {last_request['url']}"

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
    assert "enabled=false" in last_request["url"], f"Expected enabled=false in URL, got {last_request['url']}"

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
    assert "enabled=false" in last_request["url"], f"Expected enabled=false in URL, got {last_request['url']}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is False, f"Expected False, got {result}"

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

    assert result is True, f"Expected True, got {result}"

    # Check rule was updated correctly
    assert client._next_session["targetPercent"] == 90, f"Expected 90%, got {client._next_session['targetPercent']}"
    assert client._next_session["targetTime"] == 30600, f"Expected 30600 seconds (8:30), got {client._next_session['targetTime']}"  # 8*3600 + 30*60
    assert client._next_session["preconditioningEnabled"] is True, f"Expected True, got {client._next_session['preconditioningEnabled']}"
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

    assert result is True, f"Expected True, got {result}"

    # Check only target_percent was updated
    assert client._next_session["targetPercent"] == 85, f"Expected 85%, got {client._next_session['targetPercent']}"
    assert client._next_session["targetTime"] == 25200, f"Expected 25200 (unchanged), got {client._next_session['targetTime']}"
    assert client._next_session["preconditioningEnabled"] is False, f"Expected False (unchanged), got {client._next_session['preconditioningEnabled']}"

    print("PASS: async_update_schedule correctly updates only provided parameters")
    return 0


def _test_ohme_client_async_update_schedule_no_rule(my_predbat=None):
    """Test async_update_schedule with no rule"""
    print("**** Running test_ohme_client_async_update_schedule_no_rule ****")

    client = MockOhmeApiClient()
    client._next_session = None  # No rule

    # Call async_update_schedule
    result = run_async(client.async_update_schedule(target_percent=90))

    assert result is False, f"Expected False, got {result}"

    # Check no request was made
    assert len(client.request_log) == 0, f"Expected no requests, got {len(client.request_log)}"

    print("PASS: async_update_schedule correctly returns False when no rule exists")
    return 0


# ============================================================================
# OhmeAPI Component Tests
# ============================================================================

class MockOctopusComponent:
    """Stand-in for the OctopusAPI component, reporting a tariff code"""

    def __init__(self, tariff_code=None):
        """Initialize with the import tariff code to report"""
        self.tariffs = {"import": {"tariffCode": tariff_code}} if tariff_code else {}

    @staticmethod
    def is_intelligent_go_tariff(tariff_code):
        """Mirror of OctopusAPI.is_intelligent_go_tariff"""
        if not tariff_code:
            return False
        return ("INTELLI-" in tariff_code) or ("IOG-" in tariff_code)


class MockComponents:
    """Stand-in for the component registry"""

    def __init__(self):
        """Initialize with no components registered"""
        self.components = {}

    def get_component(self, name):
        """Return the registered stand-in component, if any"""
        return self.components.get(name)


class MockOhmeBase:
    """Stand-in for the PredBat base object, serving previously published entity values"""

    def __init__(self, api):
        """Initialize with a back-reference to the owning mock API"""
        self.api = api
        self.car_slot_owner = None
        self.components = MockComponents()

    def load_previous_value_from_ha(self, entity, attribute=None):
        """Return whatever the test staged for this entity/attribute"""
        return self.api.previous_values.get((entity, attribute))


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
        self.ohme_automatic = False
        self.ohme_automatic_octopus_intelligent = None
        self.ohme_control = False
        self.control_active = False
        self.control_windows = []
        self.control_charging = None
        self.control_read_only = None
        self.control_saved_target = None
        self.prefix = "predbat"
        self.local_tz = pytz.timezone("Europe/London")
        self.states = {}
        self.energy_today = 0.0
        self.energy_today_date = None
        self.energy_last_time = None
        self.energy_last_watts = 0.0
        self.energy_restored = False
        self.discovery_reported_for = None

        # Stand-in for the PredBat base object, so arg wiring and state read-back can be tested
        self.args = {}
        self.previous_values = {}
        self.base = MockOhmeBase(self)

        # Create mock client
        self.client = MockOhmeApiClient()

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Mock get_arg reading from the stub args"""
        return self.args.get(arg, default)

    def set_arg(self, arg, value):
        """Mock set_arg writing to the stub args"""
        self.args[arg] = value

    def set_arg_auto(self, arg, value):
        """Mock set_arg_auto - the real one logs then delegates to set_arg"""
        self.set_arg(arg, value)

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Mock state read, serving whatever the test staged"""
        return self.states.get((entity_id, attribute), default)

    @property
    def now_utc_exact(self):
        """Current local time, overridable by tests via _now_override"""
        return getattr(self, "_now_override", None) or datetime.datetime.now(self.local_tz)

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


def _ohme_control_api(windows=None, now=None, read_only=False):
    """Build a MockOhmeAPI with control enabled and a staged car charging plan"""
    api = MockOhmeAPI()
    api.ohme_automatic = True
    api.ohme_control = True
    api.control_active = True
    api.base.set_read_only = read_only
    api._now_override = now or datetime.datetime(2026, 8, 22, 23, 0, 0, tzinfo=datetime.timezone.utc).astimezone(api.local_tz)
    if windows is not None:
        api.states[("binary_sensor.predbat_car_charging_slot", "planned")] = windows
    # A plugged-in car so charger_mode() has something to read
    api.client._charge_session = {"mode": "SMART_CHARGE", "power": {"watt": 0}}
    return api


def _ohme_plan_window(start, end):
    """Build a planned window in the format Predbat publishes"""
    return {"start": start.strftime("%m-%d %H:%M:%S"), "end": end.strftime("%m-%d %H:%M:%S"), "kwh": 7.0}


def _test_ohme_control_enable_rules(my_predbat=None):
    """Test when Predbat-led charge control is allowed to run"""
    print("**** Running test_ohme_control_enable_rules ****")

    # Off by default
    api = MockOhmeAPI()
    api.enable_control(False)
    assert api.control_active is False, "Expected control off when ohme_control is not set"

    # Needs the car registered, or there is no plan to enforce
    api = MockOhmeAPI()
    api.ohme_control = True
    api.ohme_automatic = False
    api.enable_control(False)
    assert api.control_active is False, "Expected control to need ohme_automatic"
    assert any("needs ohme_automatic" in msg for msg in api.log_messages), f"Expected a warning, got {api.log_messages}"

    # Pointless alongside Intelligent - Octopus already schedules the charge
    api = MockOhmeAPI()
    api.ohme_control = True
    api.ohme_automatic = True
    api.enable_control(True)
    assert api.control_active is False, "Expected control to stand down in Intelligent mode"
    assert any("Octopus already schedules" in msg for msg in api.log_messages), f"Expected a warning, got {api.log_messages}"

    # Enabled when both conditions hold
    api = MockOhmeAPI()
    api.ohme_control = True
    api.ohme_automatic = True
    api.enable_control(False)
    assert api.control_active is True, "Expected control to enable"

    print("PASS: control enable rules held")
    return 0


def _test_ohme_control_window_parsing(my_predbat=None):
    """Test planned windows are parsed and matched against the clock"""
    print("**** Running test_ohme_control_window_parsing ****")

    tz = pytz.timezone("Europe/London")
    now = tz.localize(datetime.datetime(2026, 8, 22, 23, 30, 0))
    inside = _ohme_plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    api = _ohme_control_api(windows=[inside], now=now)

    assert api.refresh_car_windows() is True, "Expected the plan to be read"
    assert len(api.control_windows) == 1, f"Expected one window, got {api.control_windows}"
    assert api.should_charge_now() is True, "Expected 23:30 to fall inside a 23:00-01:00 window"

    # Just before the window starts, and exactly at the end, are both outside
    api._now_override = tz.localize(datetime.datetime(2026, 8, 22, 22, 59, 0))
    assert api.should_charge_now() is False, "Expected 22:59 to be outside the window"
    api._now_override = tz.localize(datetime.datetime(2026, 8, 23, 1, 0, 0))
    assert api.should_charge_now() is False, "Expected the window end to be exclusive"

    # A malformed entry is skipped rather than killing the whole plan
    api.states[("binary_sensor.predbat_car_charging_slot", "planned")] = [{"start": "nonsense"}, inside]
    assert api.refresh_car_windows() is True, "Expected a malformed entry to be tolerated"
    assert len(api.control_windows) == 1, f"Expected the good window to survive, got {api.control_windows}"

    print("PASS: control window parsing handled the plan")
    return 0


def _test_ohme_control_window_year_rollover(my_predbat=None):
    """Test windows that straddle new year are rebuilt around now"""
    print("**** Running test_ohme_control_window_year_rollover ****")

    tz = pytz.timezone("Europe/London")
    # 23:30 on new year's eve, charging into 01:30 on new year's day. The plan carries no year,
    # so the end parses as January of the *current* year and must be pushed forward
    now = tz.localize(datetime.datetime(2026, 12, 31, 23, 45, 0))
    window = {"start": "12-31 23:30:00", "end": "01-01 01:30:00", "kwh": 7.0}
    api = _ohme_control_api(windows=[window], now=now)

    api.refresh_car_windows()
    start, end = api.control_windows[0]
    assert end > start, f"Expected the window end to follow its start, got {start} to {end}"
    assert api.should_charge_now() is True, "Expected to be charging at 23:45 on new year's eve"

    print("PASS: new year window handled")
    return 0


def _test_ohme_control_waits_for_plan(my_predbat=None):
    """Test control sends nothing until a plan has actually been published"""
    print("**** Running test_ohme_control_waits_for_plan ****")

    # No plan sensor yet - pausing a car on no information would be the wrong default
    api = _ohme_control_api(windows=None)
    run_async(api.control_charge())

    assert len(api.client.request_log) == 0, f"Expected no commands before a plan exists, got {api.client.request_log}"
    assert api.control_charging is None, "Expected no tracked state before a plan exists"

    # An empty plan is a real answer, not a missing one - the charger is held paused
    api.states[("binary_sensor.predbat_car_charging_slot", "planned")] = []
    run_async(api.control_charge())
    assert len(api.client.request_log) == 1, f"Expected a pause once the plan is known, got {api.client.request_log}"
    assert "stop" in api.client.request_log[0]["url"], f"Expected a pause command, got {api.client.request_log[0]['url']}"

    print("PASS: control waited for a published plan")
    return 0


def _test_ohme_control_edge_triggered(my_predbat=None):
    """Test control commands the charger only when the desired state changes"""
    print("**** Running test_ohme_control_edge_triggered ****")

    tz = pytz.timezone("Europe/London")
    window = _ohme_plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    api = _ohme_control_api(windows=[window], now=tz.localize(datetime.datetime(2026, 8, 22, 23, 30)))

    # Entering the window sets max charge once
    run_async(api.control_charge())
    assert len(api.client.request_log) == 1, f"Expected one command, got {api.client.request_log}"
    assert "enabled=true" in api.client.request_log[0]["url"], f"Expected max charge, got {api.client.request_log[0]['url']}"

    # Still inside it, and the charger already agrees - no repeat command
    api.client._charge_session = {"mode": "MAX_CHARGE"}
    run_async(api.control_charge())
    run_async(api.control_charge())
    assert len(api.client.request_log) == 1, f"Expected no repeat commands, got {api.client.request_log}"

    # Leaving the window pauses once
    api._now_override = tz.localize(datetime.datetime(2026, 8, 23, 1, 30))
    run_async(api.control_charge())
    assert len(api.client.request_log) == 2, f"Expected a pause command, got {api.client.request_log}"
    assert "stop" in api.client.request_log[1]["url"], f"Expected a pause, got {api.client.request_log[1]['url']}"

    api.client._charge_session = {"mode": "STOPPED"}
    run_async(api.control_charge())
    assert len(api.client.request_log) == 2, f"Expected no repeat pause, got {api.client.request_log}"

    print("PASS: control was edge triggered")
    return 0


def _test_ohme_control_reapplies_on_drift(my_predbat=None):
    """Test control corrects the charger after someone changes it in the Ohme app"""
    print("**** Running test_ohme_control_reapplies_on_drift ****")

    tz = pytz.timezone("Europe/London")
    window = _ohme_plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    api = _ohme_control_api(windows=[window], now=tz.localize(datetime.datetime(2026, 8, 22, 23, 30)))

    run_async(api.control_charge())
    api.client._charge_session = {"mode": "MAX_CHARGE"}
    run_async(api.control_charge())
    assert len(api.client.request_log) == 1, "Expected a settled state before drifting"

    # Someone pauses it in the Ohme app while Predbat still wants it charging
    api.client._charge_session = {"mode": "STOPPED"}
    run_async(api.control_charge())

    assert len(api.client.request_log) == 2, f"Expected the change to be corrected, got {api.client.request_log}"
    assert "enabled=true" in api.client.request_log[1]["url"], f"Expected max charge re-applied, got {api.client.request_log[1]['url']}"
    assert any("changed away from what Predbat set" in msg for msg in api.log_messages), f"Expected a drift log, got {api.log_messages}"

    # An unplugged charger has nothing to correct
    api.client._charge_session = {"mode": "DISCONNECTED"}
    run_async(api.control_charge())
    assert len(api.client.request_log) == 2, f"Expected no command for an unplugged charger, got {api.client.request_log}"

    print("PASS: control re-applied after drift")
    return 0


def _test_ohme_control_read_only_release(my_predbat=None):
    """Test read only mode hands the charger back and control resumes when it clears"""
    print("**** Running test_ohme_control_read_only_release ****")

    tz = pytz.timezone("Europe/London")
    window = _ohme_plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    api = _ohme_control_api(windows=[window], now=tz.localize(datetime.datetime(2026, 8, 22, 23, 30)))

    run_async(api.control_charge())
    assert api.control_charging is True, "Expected Predbat to be holding the charger"

    # Read only - hand it back to Ohme's own schedule
    api.base.set_read_only = True
    run_async(api.control_charge())
    assert any("releasing the charger back to Ohme" in msg for msg in api.log_messages), f"Expected a release log, got {api.log_messages}"
    assert "enabled=false" in api.client.request_log[-1]["url"], f"Expected max charge cleared, got {api.client.request_log[-1]['url']}"
    assert api.control_charging is None, "Expected tracked state cleared after releasing"

    # Staying in read only must not keep sending commands
    count = len(api.client.request_log)
    run_async(api.control_charge())
    run_async(api.control_charge())
    assert len(api.client.request_log) == count, f"Expected no further commands while read only, got {api.client.request_log}"

    # Clearing read only resumes control
    api.base.set_read_only = False
    run_async(api.control_charge())
    assert len(api.client.request_log) == count + 1, f"Expected control to resume, got {api.client.request_log}"
    assert "enabled=true" in api.client.request_log[-1]["url"], f"Expected max charge re-applied, got {api.client.request_log[-1]['url']}"
    assert any("Read only mode cleared" in msg for msg in api.log_messages), f"Expected a resume log, got {api.log_messages}"

    print("PASS: read only released and resumed the charger")
    return 0


def _test_ohme_control_restores_target(my_predbat=None):
    """Test the user's charger target is put back when Predbat releases the charger"""
    print("**** Running test_ohme_control_restores_target ****")

    tz = pytz.timezone("Europe/London")
    window = _ohme_plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    api = _ohme_control_api(windows=[window], now=tz.localize(datetime.datetime(2026, 8, 22, 23, 30)))
    # The user's own target, which max charge overrides while Predbat is in control
    api.client._charge_session = {"mode": "SMART_CHARGE", "power": {"watt": 0}, "appliedRule": {"targetPercent": 70, "targetTime": 25200}}
    api.client._last_rule = {"id": "RULE-70", "targetPercent": 70}

    run_async(api.control_charge())
    assert api.control_saved_target == 70, f"Expected the target to be snapshotted before max charge, got {api.control_saved_target}"

    # Snapshot must be taken before the max charge command, not after it
    assert "enabled=true" in api.client.request_log[0]["url"], f"Expected max charge, got {api.client.request_log[0]['url']}"

    # Releasing puts the user's target back so Ohme's own schedule is left correct
    api.base.set_read_only = True
    run_async(api.control_charge())

    restore_requests = [request for request in api.client.request_log if request["method"] == "PATCH"]
    assert any(request["data"].get("targetPercent") == 70 for request in restore_requests), f"Expected the target to be restored, got {restore_requests}"
    assert api.control_saved_target is None, "Expected the saved target to be cleared after restoring"
    assert any("Restored the charger target to 70%" in msg for msg in api.log_messages), f"Expected a restore log, got {api.log_messages}"

    # Taking control again snapshots afresh rather than reusing the old value
    api.base.set_read_only = False
    api.client._charge_session = {"mode": "SMART_CHARGE", "power": {"watt": 0}, "appliedRule": {"targetPercent": 90, "targetTime": 25200}}
    run_async(api.control_charge())
    assert api.control_saved_target == 90, f"Expected a fresh snapshot, got {api.control_saved_target}"

    print("PASS: the charger target was restored on release")
    return 0


def _test_ohme_control_read_only_effective(my_predbat=None):
    """Test read only follows the effective state, not just the config switch"""
    print("**** Running test_ohme_control_read_only_effective ****")

    # axle_control forces read only via the attribute without touching the arg
    api = MockOhmeAPI()
    api.base.set_read_only = True
    api.args["set_read_only"] = False
    assert api.control_read_only_now() is True, "Expected the attribute to win over the arg"

    # Before the attribute is first set, fall back to the configured value
    api = MockOhmeAPI()
    api.base.set_read_only = None
    api.args["set_read_only"] = True
    assert api.control_read_only_now() is True, "Expected the arg to be used as a fallback"

    api.args["set_read_only"] = False
    assert api.control_read_only_now() is False, "Expected control to run when not read only"

    print("PASS: read only used the effective state")
    return 0


def _ohme_energy_api(start_watts=0.0):
    """Build a MockOhmeAPI with the energy accumulator primed at a known time"""
    api = MockOhmeAPI()
    now = datetime.datetime(2026, 8, 22, 23, 0, 0).astimezone()
    api.update_energy_today(start_watts, now)
    return api, now


def _test_ohme_energy_today_steady_power(my_predbat=None):
    """Test energy_today integrates a steady power reading into kWh"""
    print("**** Running test_ohme_energy_today_steady_power ****")

    # 7200W held across six 120s polls is 7200 * (720/3600) = 1.44 kWh
    api, now = _ohme_energy_api(start_watts=7200)
    for step in range(1, 7):
        energy = api.update_energy_today(7200, now + datetime.timedelta(seconds=120 * step))

    assert abs(energy - 1.44) < 1e-6, f"Expected 1.44 kWh, got {energy}"
    assert api.energy_today_date == now.date(), f"Expected date {now.date()}, got {api.energy_today_date}"

    print(f"PASS: energy_today integrated steady power to {energy} kWh")
    return 0


def _test_ohme_energy_today_left_riemann(my_predbat=None):
    """Test energy_today charges each interval at the power seen at its start"""
    print("**** Running test_ohme_energy_today_left_riemann ****")

    # Charger idle, then 7200W for one interval, then idle again. A left sum credits the
    # interval that *followed* the 7200W reading, so exactly one interval is counted
    api, now = _ohme_energy_api(start_watts=0)
    api.update_energy_today(7200, now + datetime.timedelta(seconds=120))  # idle interval -> 0
    energy = api.update_energy_today(0, now + datetime.timedelta(seconds=240))  # 7200W interval

    expected = 7200 * (120 / 3600.0) / 1000.0
    assert abs(energy - expected) < 1e-6, f"Expected {expected} kWh, got {energy}"

    # A further idle poll must not add anything
    energy = api.update_energy_today(0, now + datetime.timedelta(seconds=360))
    assert abs(energy - expected) < 1e-6, f"Expected {expected} kWh after idle poll, got {energy}"

    print(f"PASS: left Riemann sum counted one interval as {energy} kWh")
    return 0


def _test_ohme_energy_today_long_gap(my_predbat=None):
    """Test energy_today does not invent energy across an over-long polling gap"""
    print("**** Running test_ohme_energy_today_long_gap ****")

    # Predbat stalled - we have no evidence the charger ran, so the gap must not be counted
    api, now = _ohme_energy_api(start_watts=7200)
    energy = api.update_energy_today(7200, now + datetime.timedelta(seconds=MAX_ENERGY_GAP_SECONDS + 60))

    assert energy == 0.0, f"Expected 0 kWh across the gap, got {energy}"
    assert any("not counting that gap" in msg for msg in api.log_messages), f"Expected a gap warning, got {api.log_messages}"

    # Polling resumes normally afterwards
    energy = api.update_energy_today(7200, now + datetime.timedelta(seconds=MAX_ENERGY_GAP_SECONDS + 180))
    assert abs(energy - 7200 * (120 / 3600.0) / 1000.0) < 1e-6, f"Expected counting to resume, got {energy}"

    print("PASS: energy_today skipped the gap and resumed afterwards")
    return 0


def _test_ohme_energy_today_midnight_rollover(my_predbat=None):
    """Test energy_today resets at midnight and counts only the new day's share"""
    print("**** Running test_ohme_energy_today_midnight_rollover ****")

    # Charging at 7200W from 23:58, polled again at 00:02 - only the 120s after midnight counts
    api = MockOhmeAPI()
    before = datetime.datetime(2026, 8, 22, 23, 58, 0).astimezone()
    api.update_energy_today(7200, before)
    api.update_energy_today(7200, datetime.datetime(2026, 8, 22, 23, 59, 0).astimezone())
    assert api.energy_today > 0, "Expected energy to accumulate before midnight"

    after = datetime.datetime(2026, 8, 23, 0, 2, 0).astimezone()
    energy = api.update_energy_today(7200, after)

    expected = 7200 * (120 / 3600.0) / 1000.0
    assert abs(energy - expected) < 1e-6, f"Expected only the post-midnight {expected} kWh, got {energy}"
    assert api.energy_today_date == after.date(), f"Expected date to roll to {after.date()}, got {api.energy_today_date}"

    print(f"PASS: energy_today reset at midnight and counted {energy} kWh into the new day")
    return 0


def _test_ohme_energy_today_gap_across_midnight(my_predbat=None):
    """Test a stall spanning midnight still rolls the day over"""
    print("**** Running test_ohme_energy_today_gap_across_midnight ****")

    # Charging yesterday evening, then Predbat stalls until after midnight. The gap is too long to
    # integrate, but the day must still roll - otherwise the first publish of the new day reports
    # yesterday's total against yesterday's date
    api = MockOhmeAPI()
    before = datetime.datetime(2026, 8, 22, 23, 0, 0).astimezone()
    api.update_energy_today(7200, before)
    api.update_energy_today(7200, datetime.datetime(2026, 8, 22, 23, 2, 0).astimezone())
    assert api.energy_today > 0, "Expected energy to accumulate before the stall"

    after = datetime.datetime(2026, 8, 23, 0, 30, 0).astimezone()
    energy = api.update_energy_today(7200, after)

    assert energy == 0.0, f"Expected the new day to start at zero, got {energy}"
    assert api.energy_today_date == after.date(), f"Expected the date to roll to {after.date()}, got {api.energy_today_date}"
    assert any("not counting that gap" in msg for msg in api.log_messages), f"Expected the gap to still be skipped, got {api.log_messages}"

    # The stalled interval itself is not counted - we have no evidence the charger ran through it
    energy = api.update_energy_today(7200, after + datetime.timedelta(seconds=120))
    assert abs(energy - 7200 * (120 / 3600.0) / 1000.0) < 1e-6, f"Expected only the post-stall interval, got {energy}"

    print("PASS: the day rolled over across a long gap")
    return 0


def _test_ohme_energy_today_restore(my_predbat=None):
    """Test energy_today picks up today's total again after a restart"""
    print("**** Running test_ohme_energy_today_restore ****")

    api = MockOhmeAPI()
    now = datetime.datetime(2026, 8, 22, 18, 0, 0).astimezone()
    api.previous_values[(ENERGY_TODAY_ENTITY, "energy_date")] = now.date().isoformat()
    api.previous_values[(ENERGY_TODAY_ENTITY, None)] = "12.5"

    api.update_energy_today(7200, now)
    assert abs(api.energy_today - 12.5) < 1e-6, f"Expected 12.5 kWh restored, got {api.energy_today}"

    # And carries on from there
    energy = api.update_energy_today(7200, now + datetime.timedelta(seconds=120))
    assert abs(energy - (12.5 + 0.24)) < 1e-6, f"Expected 12.74 kWh, got {energy}"

    print(f"PASS: energy_today restored 12.5 kWh and continued to {energy} kWh")
    return 0


def _test_ohme_energy_today_restore_stale(my_predbat=None):
    """Test energy_today ignores a total published on an earlier day"""
    print("**** Running test_ohme_energy_today_restore_stale ****")

    api = MockOhmeAPI()
    now = datetime.datetime(2026, 8, 22, 6, 0, 0).astimezone()
    api.previous_values[(ENERGY_TODAY_ENTITY, "energy_date")] = "2026-08-21"
    api.previous_values[(ENERGY_TODAY_ENTITY, None)] = "30.0"

    api.update_energy_today(0, now)
    assert api.energy_today == 0.0, f"Expected yesterday's total to be ignored, got {api.energy_today}"

    print("PASS: energy_today ignored a stale total from an earlier day")
    return 0


def _test_ohme_energy_today_published(my_predbat=None):
    """Test publish_data publishes energy_today with the attributes Home Assistant needs"""
    print("**** Running test_ohme_energy_today_published ****")

    api = MockOhmeAPI()
    api.client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200, "amp": 32, "volt": 230},
        "appliedRule": {"targetPercent": 80, "targetTime": 25200},
        "batterySoc": {"wh": 15000, "percent": 75},
        "allSessionSlots": [],
    }
    api.client._next_session = {"targetPercent": 80, "targetTime": 25200}
    api.client._last_rule = {"targetPercent": 80}
    api.client._cars = [{"name": "Tesla Model 3"}]

    run_async(api.publish_data())

    assert ENERGY_TODAY_ENTITY in api.dashboard_items, f"Expected {ENERGY_TODAY_ENTITY} to be published, got {list(api.dashboard_items)}"
    item = api.dashboard_items[ENERGY_TODAY_ENTITY]
    assert item["attributes"]["unit_of_measurement"] == "kWh", f"Expected kWh, got {item['attributes']}"
    assert item["attributes"]["device_class"] == "energy", f"Expected device_class energy, got {item['attributes']}"
    # Compared against the accumulator's own date rather than a fresh now(), which would make the
    # test straddle midnight and fail on a correct publish. The date itself is covered by the
    # rollover tests
    assert item["attributes"]["energy_date"] == api.energy_today_date.isoformat(), f"Expected the published date to match the accumulator, got {item['attributes']}"

    # First publish has no prior reading to integrate over, so it starts at zero
    assert item["state"] == 0.0, f"Expected 0.0 kWh on the first publish, got {item['state']}"

    print("PASS: publish_data published energy_today correctly")
    return 0


def _ohme_api_with_octopus(tariff_code=None):
    """Build a MockOhmeAPI with an Octopus component reporting the given tariff code"""
    api = MockOhmeAPI()
    if tariff_code is not None:
        api.base.components.components["octopus"] = MockOctopusComponent(tariff_code)
    return api


def _test_ohme_iog_flag_forces_on(my_predbat=None):
    """Test an explicit flag turns Intelligent on even with no Octopus component"""
    print("**** Running test_ohme_iog_flag_forces_on ****")

    # The case that matters: no Predbat Octopus component to detect from, so the user says so
    api = MockOhmeAPI()
    api.ohme_automatic_octopus_intelligent = True
    assert api.octopus_intelligent_wanted() is True, "Expected the explicit flag to force Intelligent on"

    # And it still wins when ohme_automatic is off, preserving existing configurations
    api = MockOhmeAPI()
    api.ohme_automatic = False
    api.ohme_automatic_octopus_intelligent = True
    assert api.octopus_intelligent_wanted() is True, "Expected the flag to work without ohme_automatic"

    print("PASS: explicit flag forced Intelligent on")
    return 0


def _test_ohme_iog_flag_forces_off(my_predbat=None):
    """Test an explicit False beats auto-detection"""
    print("**** Running test_ohme_iog_flag_forces_off ****")

    # Detection would say yes, but the user wants the slots straight from Octopus
    api = _ohme_api_with_octopus("E-1R-INTELLI-VAR-22-10-14-A")
    api.ohme_automatic = True
    api.ohme_automatic_octopus_intelligent = False
    assert api.octopus_intelligent_wanted() is False, "Expected an explicit False to beat detection"

    print("PASS: explicit False beat auto-detection")
    return 0


def _test_ohme_iog_autodetected_from_octopus(my_predbat=None):
    """Test Intelligent is auto-detected from the Octopus component's tariff code"""
    print("**** Running test_ohme_iog_autodetected_from_octopus ****")

    api = _ohme_api_with_octopus("E-1R-INTELLI-VAR-22-10-14-A")
    api.ohme_automatic = True
    assert api.octopus_intelligent_wanted() is True, "Expected an INTELLI tariff to be detected"

    # A non-Intelligent tariff must not trigger it
    api = _ohme_api_with_octopus("E-1R-AGILE-24-10-01-A")
    api.ohme_automatic = True
    assert api.octopus_intelligent_wanted() is False, "Expected Agile not to be detected as Intelligent"

    # Neither must a missing Octopus component
    api = MockOhmeAPI()
    api.ohme_automatic = True
    assert api.octopus_intelligent_wanted() is False, "Expected no detection without an Octopus component"

    print("PASS: Intelligent auto-detected from the Octopus tariff code")
    return 0


def _test_ohme_iog_autodetect_needs_ohme_automatic(my_predbat=None):
    """Test auto-detection stays off for users who asked Predbat for nothing"""
    print("**** Running test_ohme_iog_autodetect_needs_ohme_automatic ****")

    # An existing user with neither flag set must keep getting no automatic wiring at all,
    # even though the tariff is Intelligent and we could detect it
    api = _ohme_api_with_octopus("E-1R-INTELLI-VAR-22-10-14-A")
    api.ohme_automatic = False
    assert api.octopus_intelligent_wanted() is False, "Expected no auto-detection without ohme_automatic"

    print("PASS: auto-detection stayed off without ohme_automatic")
    return 0


def _test_ohme_iog_claims_car_slots(my_predbat=None):
    """Test the Intelligent wiring claims the car slots so Octopus stops re-wiring them"""
    print("**** Running test_ohme_iog_claims_car_slots ****")

    api = MockOhmeAPI()
    assert api.base.car_slot_owner is None, "Expected no owner before configuring"

    run_async(api.automatic_config_octopus_intelligent())

    assert api.base.car_slot_owner == "ohme", f"Expected ohme to own the car slots, got {api.base.car_slot_owner}"
    assert api.args.get("octopus_intelligent_slot") == "binary_sensor.predbat_ohme_slot_active", f"Expected slot entity, got {api.args.get('octopus_intelligent_slot')}"
    assert api.args.get("octopus_ready_time") == "select.predbat_ohme_target_time", f"Expected ready time entity, got {api.args.get('octopus_ready_time')}"
    assert api.args.get("octopus_charge_limit") == "number.predbat_ohme_target_percent", f"Expected charge limit entity, got {api.args.get('octopus_charge_limit')}"

    # Car registration is a separate concern and must not have happened here
    assert api.args.get("car_charging_energy") is None, f"Expected no car registration, got {api.args.get('car_charging_energy')}"

    print("PASS: Intelligent wiring claimed the car slots")
    return 0


def _test_ohme_connected_sensor(my_predbat=None):
    """Test the connected binary sensor reflects whether a car wants charge"""
    print("**** Running test_ohme_connected_sensor ****")

    # Statuses that mean a car is plugged in and still wants charge
    for mode, session, expected in [
        ("SMART_CHARGE", {"mode": "SMART_CHARGE", "power": {"watt": 7200}}, "on"),  # charging
        ("SMART_CHARGE", {"mode": "SMART_CHARGE", "power": {"watt": 0}}, "on"),  # plugged in
        ("PENDING_APPROVAL", {"mode": "PENDING_APPROVAL"}, "on"),
        ("STOPPED", {"mode": "STOPPED"}, "on"),  # paused
        ("DISCONNECTED", {"mode": "DISCONNECTED"}, "off"),  # unplugged
        ("FINISHED_CHARGE", {"mode": "FINISHED_CHARGE"}, "off"),  # nothing left to take
    ]:
        api = MockOhmeAPI()
        # publish_data reads the applied rule for any session that is in progress
        api.client._charge_session = dict(session, appliedRule={"targetPercent": 80, "targetTime": 25200})
        api.client._next_session = {"targetPercent": 80, "targetTime": 25200}
        api.client._last_rule = {"targetPercent": 80}
        api.client._cars = []
        run_async(api.publish_data())

        state = api.dashboard_items["binary_sensor.predbat_ohme_connected"]["state"]
        assert state == expected, f"Expected {mode} to publish connected={expected}, got {state}"

    print("PASS: connected binary sensor tracked the plug state")
    return 0


def _test_ohme_energy_today_uses_configured_timezone(my_predbat=None):
    """Test the daily total follows Predbat's configured timezone, not the host clock"""
    print("**** Running test_ohme_energy_today_uses_configured_timezone ****")

    # A container running UTC with timezone: Europe/London configured must still reset at the
    # user's local midnight, so publish_data has to take its clock from Predbat rather than now()
    api = MockOhmeAPI()
    api._now_override = api.local_tz.localize(datetime.datetime(2026, 1, 2, 3, 4, 5))
    api.client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200, "amp": 32, "volt": 230},
        "appliedRule": {"targetPercent": 80, "targetTime": 25200},
        "allSessionSlots": [],
    }
    api.client._next_session = {"targetPercent": 80, "targetTime": 25200}
    api.client._last_rule = {"targetPercent": 80}
    api.client._cars = []

    run_async(api.publish_data())

    published = api.dashboard_items[ENERGY_TODAY_ENTITY]["attributes"]["energy_date"]
    assert published == "2026-01-02", f"Expected the configured clock's date, got {published}"
    assert api.energy_today_date == datetime.date(2026, 1, 2), f"Expected the accumulator to follow it too, got {api.energy_today_date}"

    print("PASS: energy_today followed the configured timezone")
    return 0


def _test_ohme_auto_config_wires_car_charging_energy(my_predbat=None):
    """Test car registration points car_charging_energy at the delivered-energy sensor"""
    print("**** Running test_ohme_auto_config_wires_car_charging_energy ****")

    # Nothing configured at all
    api = MockOhmeAPI()
    run_async(api.automatic_config())
    assert api.args.get("car_charging_energy") == ENERGY_TODAY_ENTITY, f"Expected {ENERGY_TODAY_ENTITY}, got {api.args.get('car_charging_energy')}"

    # The apps.yaml default regex, still unresolved because no Zappi or Wallbox matched it.
    # auto_config(final=True) has not run yet at this point in startup, so it is still present
    api = MockOhmeAPI()
    api.args["car_charging_energy"] = "re:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy)"
    run_async(api.automatic_config())
    assert api.args.get("car_charging_energy") == ENERGY_TODAY_ENTITY, f"Expected unmatched regex to be replaced, got {api.args.get('car_charging_energy')}"

    # The rest of the car registration happens too
    assert api.args.get("num_cars") == 1, f"Expected num_cars 1, got {api.args.get('num_cars')}"
    assert api.args.get("car_charging_planned") == ["binary_sensor.predbat_ohme_connected"], f"Expected connected sensor, got {api.args.get('car_charging_planned')}"
    assert api.args.get("car_charging_soc") == ["sensor.predbat_ohme_battery_percent"], f"Expected battery percent sensor, got {api.args.get('car_charging_soc')}"

    # ...but the Octopus Intelligent args are left to the separate method
    assert api.args.get("octopus_intelligent_slot") is None, f"Expected no slot wiring from car registration, got {api.args.get('octopus_intelligent_slot')}"

    print("PASS: auto config wired car_charging_energy")
    return 0


def _test_ohme_auto_config_keeps_existing_car_charging_energy(my_predbat=None):
    """Test auto config leaves a real charger's energy sensor alone"""
    print("**** Running test_ohme_auto_config_keeps_existing_car_charging_energy ****")

    # A resolved Zappi sensor means the user has another charger measuring car energy - taking
    # that over with an Ohme-only figure would lose the car charging it is already reporting
    api = MockOhmeAPI()
    api.args["car_charging_energy"] = "sensor.myenergi_zappi_1234_charge_added_session"
    run_async(api.automatic_config())

    assert api.args.get("car_charging_energy") == "sensor.myenergi_zappi_1234_charge_added_session", f"Expected the Zappi sensor to be kept, got {api.args.get('car_charging_energy')}"
    assert any("Leaving car_charging_energy" in msg for msg in api.log_messages), f"Expected a note about keeping it, got {api.log_messages}"

    print("PASS: auto config kept the existing car_charging_energy sensor")
    return 0


def _test_ohme_auto_config_wires_car_charging_power(my_predbat=None):
    """Test car registration points car_charging_power at the live charge power sensor"""
    print("**** Running test_ohme_auto_config_wires_car_charging_power ****")

    api = MockOhmeAPI()
    run_async(api.automatic_config())
    assert api.args.get("car_charging_power") == POWER_WATTS_ENTITY, f"Expected {POWER_WATTS_ENTITY}, got {api.args.get('car_charging_power')}"

    # The energy and power sensors have to describe the same charger, so when a real third-party
    # energy sensor is kept, Ohme's own power figure must not be wired in beside it
    api = MockOhmeAPI()
    api.args["car_charging_energy"] = "sensor.myenergi_zappi_1234_charge_added_session"
    run_async(api.automatic_config())
    assert api.args.get("car_charging_power") is None, f"Expected no power wiring when another charger owns the energy sensor, got {api.args.get('car_charging_power')}"

    # ...but Ohme's own energy entity, set explicitly in apps.yaml or left behind by an earlier
    # run, is still Ohme's charger, so the power sensor belongs with it (#4715 review)
    api = MockOhmeAPI()
    api.args["car_charging_energy"] = ENERGY_TODAY_ENTITY
    run_async(api.automatic_config())
    assert api.args.get("car_charging_power") == POWER_WATTS_ENTITY, f"Expected power wiring alongside Ohme's own energy entity, got {api.args.get('car_charging_power')}"

    # The same entity handed over as a single-item list, which is how it round-trips through apps.yaml
    api = MockOhmeAPI()
    api.args["car_charging_energy"] = [ENERGY_TODAY_ENTITY]
    run_async(api.automatic_config())
    assert api.args.get("car_charging_power") == POWER_WATTS_ENTITY, f"Expected power wiring for the list form, got {api.args.get('car_charging_power')}"

    print("PASS: auto config wired car_charging_power")
    return 0


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
    assert api.dashboard_items["binary_sensor.predbat_ohme_slot_active"]["state"] is True, \
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
    assert api.dashboard_items["binary_sensor.predbat_ohme_slot_active"]["state"] is False, \
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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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

    assert result is True, f"Expected True, got {result}"

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
    api.client._last_rule = {"id": "RULE-TIME", "targetTime": 25200}  # 07:00

    # Call select_event_handler with valid target time
    run_async(api.select_event_handler("select.predbat_ohme_target_time", "08:30"))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PATCH", f"Expected PATCH request, got {request['method']}"
    assert "/v2/users/me/charge-rules/RULE-TIME" in request["url"], f"Expected charge-rules URL, got {request['url']}"
    assert request["data"]["targetTime"] == 30600, f"Expected targetTime=30600 (08:30) in the body, got {request['data']}"

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
    api.client._last_rule = {"id": "RULE-80", "targetPercent": 80}

    # Call number_event_handler with valid target SoC
    run_async(api.number_event_handler("number.predbat_ohme_target_percent", 90))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PATCH", f"Expected PATCH request, got {request['method']}"
    assert request["data"]["targetPercent"] == 90, f"Expected targetPercent=90 in the body, got {request['data']}"

    print("PASS: number_event_handler correctly handles target_soc")
    return 0


def _test_ohme_number_event_handler_matches_published_entities(my_predbat=None):
    """Test every number entity published by publish_data is accepted by number_event_handler"""
    print("**** Running test_ohme_number_event_handler_matches_published_entities ****")

    # Publish the full entity set so we test against the real entity names rather than
    # hand-written ones - a mismatch between publish_data and the handler silently drops
    # the user's change, which is how number.predbat_ohme_target_percent was left inert
    api = MockOhmeAPI()
    api.client._charge_session = {
        "mode": "SMART_CHARGE",
        "power": {"watt": 7200, "amp": 32, "volt": 230},
        "appliedRule": {"targetPercent": 80, "targetTime": 25200},
        "batterySoc": {"wh": 15000, "percent": 75},
        "allSessionSlots": [],
    }
    api.client._next_session = {"id": "NEXT-80", "targetPercent": 80, "targetTime": 25200}
    api.client._last_rule = {"id": "RULE-80", "targetPercent": 80, "preconditioningEnabled": True, "preconditionLengthMins": 30}
    api.client._cars = [{"name": "Tesla Model 3"}]
    run_async(api.publish_data())

    published = [entity_id for entity_id in api.dashboard_items if entity_id.startswith("number.")]
    assert published, "Expected publish_data to publish at least one number entity"
    assert "number.predbat_ohme_target_percent" in published, f"Expected target percent entity to be published, got {published}"

    # Every published number entity must reach the API when changed
    for entity_id in published:
        api.client.request_log = []
        run_async(api.number_event_handler(entity_id, 50))
        assert len(api.client.request_log) == 1, f"Entity {entity_id} is published but number_event_handler made {len(api.client.request_log)} requests - it is not wired up"

    # Specifically check the target percent value reaches the rule
    api.client.request_log = []
    run_async(api.number_event_handler("number.predbat_ohme_target_percent", 65))
    assert api.client.request_log[0]["data"]["targetPercent"] == 65, f"Expected targetPercent=65 in the body, got {api.client.request_log[0]}"

    print(f"PASS: all {len(published)} published number entities are handled")
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
    run_async(api.number_event_handler("number.predbat_ohme_target_percent", 150))

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
    api.client._last_rule = {"id": "RULE-9", "preconditioningEnabled": False, "preconditionLengthMins": 30}

    # Call number_event_handler with preconditioning length
    run_async(api.number_event_handler("number.predbat_ohme_preconditioning", 45))

    # Verify request was made
    assert len(api.client.request_log) == 1, f"Expected 1 request, got {len(api.client.request_log)}"
    request = api.client.request_log[0]
    assert request["method"] == "PATCH", f"Expected PATCH request, got {request['method']}"
    assert request["data"]["preconditioning"]["lengthMins"] == 45, f"Expected preconditioning lengthMins=45, got {request['data']}"
    assert request["data"]["preconditioning"]["enabled"] is True, f"Expected preconditioning enabled=True, got {request['data']}"

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
    assert "enabled=true" in request["url"], f"Expected enabled=true, got {request['url']}"

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
    assert "enabled=false" in request["url"], f"Expected enabled=false, got {request['url']}"

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


# ============================================================================
# Discovery Catalogue Tests (build_discovery, _refresh_discovery_report)
# ============================================================================
#
# Task 8: the Ohme reporter is what proves the charger-and-car split the discovery catalogue
# design rests on - one charger record and one car record, cross-linked in both directions, with
# the car carrying a genuine identity when Ohme's own account data knows the vehicle and a stub
# otherwise. MockOhmeAPI's own states dict stands in for the state store here, exactly as
# _ohme_control_api() already stages entries in it for the control tests above -
# MockOhmeAPI.dashboard_item() does not feed published entities back into get_state_wrapper() the
# way the real ComponentBase/PredBat one does (see output.py's dashboard_item(), which calls
# set_state_wrapper()), so a test that needs build_discovery()'s entity-existence check to see an
# entity as published stages it into api.states directly.
# ============================================================================


def _stage_all_discovery_entities(api):
    """Publish every charger and car discovery entity into the mock state store."""
    for entity_id, _domain, _access in CHARGER_DISCOVERY_ENTITY_SPEC.values():
        api.states[(entity_id, None)] = "on"
    for entity_id, _domain, _access in CAR_DISCOVERY_ENTITY_SPEC.values():
        api.states[(entity_id, None)] = "on"


def _known_vehicle():
    """One realistic Ohme "cars" API entry, matching MOCK_DEVICE_INFO_RESPONSE's shape."""
    return {
        "id": "car-123",
        "name": "Tesla Model 3",
        "model": {
            "make": "Tesla",
            "modelName": "Model 3",
            "availableFromYear": 2017,
            "brand": {"name": "Tesla"},
        },
    }


async def _discovery_async_noop(*args, **kwargs):
    """Stand in for a network-facing async call whose real return value a run()-level discovery test does not depend on."""
    return None


def _stub_run_network_calls(api):
    """Replace run()'s network-facing async calls with no-ops, so a discovery test can drive a real run() cycle without touching the network."""
    api.client.async_update_device_info = _discovery_async_noop
    api.client.async_get_charge_session = _discovery_async_noop
    api.publish_data = _discovery_async_noop
    api.update_success_timestamp = lambda: None


def _test_ohme_build_discovery_charger_and_car_split(my_predbat=None):
    """One charger record and one car record are produced, cross-linked in both directions"""
    print("**** Running test_ohme_build_discovery_charger_and_car_split ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    _stage_all_discovery_entities(api)

    report = api.build_discovery()

    assert len(report["chargers"]) == 1, f"Expected exactly one charger record, got {len(report['chargers'])}"
    assert len(report["cars"]) == 1, f"Expected exactly one car record, got {len(report['cars'])}"
    charger = report["chargers"][0]
    car = report["cars"][0]
    assert charger["device_id"] == "ohme:TEST-SERIAL-123", f"Unexpected charger device_id: {charger['device_id']}"
    assert car["charged_by"] == charger["device_id"], f"car.charged_by should point back at the charger, got {car.get('charged_by')}"
    assert charger["serves_cars"] == [car["device_id"]], f"charger.serves_cars should list the car, got {charger.get('serves_cars')}"

    print("PASS: build_discovery produces one charger and one car record, cross-linked in both directions")
    return 0


def _test_ohme_build_discovery_stub_car_when_no_vehicle(my_predbat=None):
    """With no vehicle known the car record is a stub - device_id "ohme:{serial}:car", info.stub true"""
    print("**** Running test_ohme_build_discovery_stub_car_when_no_vehicle ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    api.client._cars = []
    _stage_all_discovery_entities(api)

    report = api.build_discovery()

    car = report["cars"][0]
    assert car["device_id"] == "ohme:TEST-SERIAL-123:car", f"Unexpected stub device_id: {car['device_id']}"
    assert car["info"].get("stub") is True, f"Expected info.stub True for a stub car, got {car.get('info')}"

    print("PASS: with no vehicle known, build_discovery stubs the car with info.stub True")
    return 0


def _test_ohme_build_discovery_known_vehicle(my_predbat=None):
    """With a vehicle known the car carries its make and model and a non-stub device_id"""
    print("**** Running test_ohme_build_discovery_known_vehicle ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    api.client._cars = [_known_vehicle()]
    _stage_all_discovery_entities(api)

    report = api.build_discovery()

    car = report["cars"][0]
    assert car["device_id"] == "ohme:car-123", f"Unexpected car device_id: {car['device_id']}"
    assert car["info"].get("make") == "Tesla", f"Expected info.make 'Tesla', got {car.get('info')}"
    assert car["info"].get("model") == "Model 3", f"Expected info.model 'Model 3', got {car.get('info')}"
    assert "stub" not in car["info"], f"Expected no info.stub for a known vehicle, got {car.get('info')}"

    print("PASS: with a vehicle known, build_discovery reports its make/model and a non-stub device_id")
    return 0


def _test_ohme_build_discovery_vehicle_from_real_client_parse(my_predbat=None):
    """The non-stub car path works against data parsed by the real client, not a hand-built _cars list"""
    print("**** Running test_ohme_build_discovery_vehicle_from_real_client_parse ****")

    api = MockOhmeAPI()
    # async_update_device_info() is the real OhmeApiClient method, parsing MockOhmeApiClient's
    # canned /v1/users/me/account response (MOCK_DEVICE_INFO_RESPONSE) exactly as it would parse a
    # real Ohme API response - nothing about _cars is hand-built here.
    run_async(api.client.async_update_device_info())
    _stage_all_discovery_entities(api)

    assert api.client.serial == "TEST-SERIAL-123", f"Expected the real parse to set serial, got {api.client.serial}"
    assert len(api.client._cars) == 1, f"Expected the real parse to populate one car, got {api.client._cars}"

    report = api.build_discovery()

    car = report["cars"][0]
    assert car["device_id"] == "ohme:car-123", f"Unexpected car device_id from real client parse: {car['device_id']}"
    assert car["info"].get("make") == "Tesla", f"Expected info.make 'Tesla' from real client parse, got {car.get('info')}"
    assert car["info"].get("model") == "Model 3", f"Expected info.model 'Model 3' from real client parse, got {car.get('info')}"

    print("PASS: the non-stub car path works end to end against data the real client parsed")
    return 0


def _test_ohme_build_discovery_entities_split_by_record(my_predbat=None):
    """Charger entities land on the charger record, car entities land on the car record"""
    print("**** Running test_ohme_build_discovery_entities_split_by_record ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    _stage_all_discovery_entities(api)

    report = api.build_discovery()

    charger_entities = set(report["chargers"][0]["entities"])
    car_entities = set(report["cars"][0]["entities"])

    assert charger_entities == set(CHARGER_DISCOVERY_ENTITY_SPEC), f"Unexpected charger entities: {charger_entities}"
    assert car_entities == set(CAR_DISCOVERY_ENTITY_SPEC), f"Unexpected car entities: {car_entities}"
    assert "car_charging_planned" in charger_entities and "car_charging_planned" not in car_entities, "car_charging_planned should be a charger fact"
    assert "car_charging_soc" in car_entities and "car_charging_soc" not in charger_entities, "car_charging_soc should be a car fact"
    assert report["chargers"][0]["entities"]["car_charging_planned"]["entity_id"] == "binary_sensor.predbat_ohme_connected"
    assert report["cars"][0]["entities"]["car_charging_soc"]["entity_id"] == "sensor.predbat_ohme_battery_percent"

    print("PASS: charger and car entities are split into the correct record")
    return 0


def _test_ohme_build_discovery_entities_only_when_published(my_predbat=None):
    """Only entities that actually exist in the state store are reported"""
    print("**** Running test_ohme_build_discovery_entities_only_when_published ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    # Only the connected sensor and the battery percent sensor have been published so far.
    api.states[("binary_sensor.predbat_ohme_connected", None)] = "on"
    api.states[("sensor.predbat_ohme_battery_percent", None)] = 75

    report = api.build_discovery()

    assert set(report["chargers"][0]["entities"]) == {"car_charging_planned"}, f"Expected only car_charging_planned, got {list(report['chargers'][0]['entities'])}"
    assert set(report["cars"][0]["entities"]) == {"car_charging_soc"}, f"Expected only car_charging_soc, got {list(report['cars'][0]['entities'])}"

    print("PASS: build_discovery reports only the entities that actually exist in the state store")
    return 0


def _test_ohme_build_discovery_omits_unknown_ratings(my_predbat=None):
    """No ratings.max_power_kw is invented when this client parses no such field"""
    print("**** Running test_ohme_build_discovery_omits_unknown_ratings ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    _stage_all_discovery_entities(api)

    report = api.build_discovery()

    assert "ratings" not in report["chargers"][0], f"Expected no fabricated ratings, got {report['chargers'][0].get('ratings')}"

    print("PASS: build_discovery reports no ratings rather than inventing a max_power_kw")
    return 0


def _test_ohme_build_discovery_records_automatic_flag(my_predbat=None):
    """The report's "automatic" flag mirrors self.ohme_automatic, and reporting is not gated on it"""
    print("**** Running test_ohme_build_discovery_records_automatic_flag ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    _stage_all_discovery_entities(api)

    api.ohme_automatic = False
    report_off = api.build_discovery()
    api.ohme_automatic = True
    report_on = api.build_discovery()

    assert report_off["automatic"] is False, f"Expected automatic False, got {report_off['automatic']}"
    assert report_on["automatic"] is True, f"Expected automatic True, got {report_on['automatic']}"
    assert len(report_off["chargers"]) == 1 and len(report_off["cars"]) == 1, "A report should still be produced with ohme_automatic False"

    print("PASS: build_discovery records self.ohme_automatic and is produced regardless of its value")
    return 0


def _test_ohme_discovery_report_not_advanced_while_entities_incomplete(my_predbat=None):
    """
    The reported-marker does not advance while the charger/car entity set is incomplete.

    Without this, a report built before publish_data() has ever run would be marked done forever,
    and the catalogue would permanently describe an incomplete charger and car.
    """
    print("**** Running test_ohme_discovery_report_not_advanced_while_entities_incomplete ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    api._refresh_discovery_report()

    assert api.discovery_reported_for is None, "The marker should not advance while entities are incomplete"
    assert len(reports) == 1, f"Expected exactly one report attempt, got {len(reports)}"

    _stage_all_discovery_entities(api)
    api._refresh_discovery_report()

    assert api.discovery_reported_for is not None, "The marker should advance once every entity is published"
    assert len(reports) == 2, f"Expected a second report attempt once entities were complete, got {len(reports)}"

    print("PASS: the reported-marker only advances once the charger and car entities are complete")
    return 0


def _test_ohme_discovery_report_failure_contained_and_retried(my_predbat=None):
    """A build_discovery failure must not propagate, and is retried the next time it is called"""
    print("**** Running test_ohme_discovery_report_failure_contained_and_retried ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    _stage_all_discovery_entities(api)
    real_build_discovery = api.build_discovery
    api.build_discovery = MagicMock(side_effect=Exception("boom"))
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    api._refresh_discovery_report()

    assert not reports, "No report should have been recorded on the failing attempt"
    assert api.discovery_reported_for is None, "A failed report must not be marked as reported"
    assert any("failed to report discovery" in msg for msg in api.log_messages), f"Expected a warning log, got {api.log_messages}"

    api.build_discovery = real_build_discovery
    api._refresh_discovery_report()

    assert len(reports) == 1, f"Expected the retried report to succeed, got {len(reports)} reports"
    assert api.discovery_reported_for is not None, "The marker should advance once the retried report succeeds"

    print("PASS: a build_discovery failure is contained and retried on the next call, not lost forever")
    return 0


def _test_ohme_discovery_report_retried_via_unconditional_run_call(my_predbat=None):
    """
    A build_discovery failure on the very first run() cycle is retried on a later, unchanging
    cycle via run()'s unconditional call to _refresh_discovery_report() - not lost for the life of
    the process, even though the "if first and self.client.serial:" block it sits beside only ever
    runs once ("first" flips to False forever the instant run() returns True).
    """
    print("**** Running test_ohme_discovery_report_retried_via_unconditional_run_call ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    _stage_all_discovery_entities(api)
    _stub_run_network_calls(api)

    real_build_discovery = api.build_discovery
    api.build_discovery = MagicMock(side_effect=Exception("boom"))
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    result1 = run_async(api.run(seconds=0, first=True))

    assert result1 is True, "run() should still succeed on a cycle where only the discovery report fails"
    assert not reports, f"No report should have succeeded on the failing first cycle, got {len(reports)}"
    assert api.discovery_reported_for is None, "A failed report must not be marked as reported"

    api.build_discovery = real_build_discovery
    result2 = run_async(api.run(seconds=600, first=False))

    assert result2 is True, "run() should succeed on the retried cycle"
    assert len(reports) == 1, f"Expected exactly one successful report after the retry, got {len(reports)}"
    assert api.discovery_reported_for is not None, "The marker should have advanced once the retried report succeeded"

    print("PASS: a first-cycle build_discovery failure is retried on a later cycle via run()'s unconditional call")
    return 0


def _test_ohme_discovery_report_produced_when_automatic_false(my_predbat=None):
    """
    An installation running ohme_automatic: false - precisely the manually-configured
    installation a discovery catalogue most wants to describe - still gets a discovery report from
    a real run() cycle, even though automatic_config() is gated on ohme_automatic and never runs.
    """
    print("**** Running test_ohme_discovery_report_produced_when_automatic_false ****")

    api = MockOhmeAPI()
    api.client.serial = "TEST-SERIAL-123"
    api.ohme_automatic = False
    _stage_all_discovery_entities(api)
    _stub_run_network_calls(api)
    automatic_config_calls = []

    async def _mock_automatic_config():
        """Record that automatic_config() was called, without performing its real apps.yaml wiring."""
        automatic_config_calls.append(True)

    api.automatic_config = _mock_automatic_config
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    result = run_async(api.run(seconds=0, first=True))

    assert result is True, "run() should succeed"
    assert not automatic_config_calls, "automatic_config() must not run when ohme_automatic is False"
    assert len(reports) == 1, f"Expected exactly one discovery report even with ohme_automatic False, got {len(reports)}"
    assert reports[0].get("automatic") is False, f"Expected the report to record automatic False, got {reports[0].get('automatic')}"
    assert api.discovery_reported_for is not None, "The marker should have advanced once the report succeeded"

    print("PASS: run() reports to the discovery catalogue even when ohme_automatic is False")
    return 0


def _test_ohme_discovery_report_skipped_before_serial_known(my_predbat=None):
    """
    A real run() cycle with no serial yet - the charger's very first cycle, before
    async_update_device_info() has ever identified it - must not report at all: neither
    build_discovery() nor report_discovery() is called, and no charger record with a missing
    serial reaches the coordinator.

    _refresh_discovery_report() now runs unconditionally every cycle rather than only inside "if
    first and self.client.serial:" (see run()'s comment on why), so this pins that its own falsy-
    serial guard is what stops it firing too early - not the gate it now sits outside.
    MockOhmeApiClient's serial defaults to "" (OhmeApiClient.__init__), matching the real state
    before the account has ever been fetched, so this test deliberately never sets it.
    """
    print("**** Running test_ohme_discovery_report_skipped_before_serial_known ****")

    api = MockOhmeAPI()
    assert not api.client.serial, f"Expected the client to start with no serial, got {api.client.serial!r}"
    _stub_run_network_calls(api)
    api.build_discovery = MagicMock(side_effect=AssertionError("build_discovery() should not be called before the serial is known"))
    reports = []
    api.report_discovery = lambda report: reports.append(report)

    result = run_async(api.run(seconds=0, first=True))

    assert result is True, "run() should still succeed on a cycle with no serial yet"
    assert not api.build_discovery.called, "build_discovery() must not be called before the client has a serial"
    assert not reports, f"No report should reach the coordinator before the serial is known, got {reports}"
    assert api.discovery_reported_for is None, "The marker must not advance before the serial is known"

    print("PASS: run() does not report to the discovery catalogue before the client's serial is known")
    return 0


def _test_ohme_build_discovery_round_trips_through_coordinator_and_redaction(my_predbat=None):
    """
    Feed build_discovery()'s output through the real Coordinator.report()/assemble() and then
    through the real Redactor, exactly as it will be at runtime.

    Checks two things: that nothing intended for a typed container was silently dropped by
    validation (the round-trip check that has caught a real bug in each of the previous
    reporters - a truncated option list, an integer dropped by a strings-only container, an
    identifier embedded in a transformed form), and that the redacted catalogue never publishes
    the Ohme login email in the clear in any form - see
    _test_ohme_build_discovery_never_leaks_login_email for that half in isolation, with more forms
    checked.
    """
    print("**** Running test_ohme_build_discovery_round_trips_through_coordinator_and_redaction ****")

    from coordinator import Coordinator
    from mock_base import MockBase as SharedMockBase

    api = MockOhmeAPI()
    api.client.serial = "OHME-SERIAL-99887"
    api.client.device_info = {"model": "Home Pro"}
    api.client._cars = [_known_vehicle()]
    _stage_all_discovery_entities(api)

    report = api.build_discovery()

    coordinator = Coordinator(SharedMockBase())
    coordinator.report("ohme", report)
    cleaned = coordinator.reports["ohme"]

    failed = 0

    def check(condition, message):
        """Record one failed assertion, printing its message, without aborting the remaining checks."""
        nonlocal failed
        if not condition:
            print("ERROR: " + message)
            failed += 1

    charger = next(r for r in cleaned["chargers"] if r["device_id"] == "ohme:OHME-SERIAL-99887")
    check(charger.get("hardware_ids", {}).get("serial") == "OHME-SERIAL-99887", "serial dropped or altered by validation: {}".format(charger.get("hardware_ids")))
    check(charger.get("info", {}).get("vendor") == "Ohme", "vendor dropped by validation: {}".format(charger.get("info")))
    check(charger.get("info", {}).get("model") == "Home Pro", "model dropped by validation: {}".format(charger.get("info")))
    check(charger.get("serves_cars") == ["ohme:car-123"], "serves_cars dropped or altered by validation: {}".format(charger.get("serves_cars")))
    check(len(charger.get("entities", {})) == len(CHARGER_DISCOVERY_ENTITY_SPEC), "not every charger entity survived validation: {}".format(charger.get("entities")))

    car = next(r for r in cleaned["cars"] if r["device_id"] == "ohme:car-123")
    check(car.get("charged_by") == "ohme:OHME-SERIAL-99887", "charged_by dropped or altered by validation: {}".format(car.get("charged_by")))
    check(car.get("info", {}).get("make") == "Tesla", "make dropped by validation: {}".format(car.get("info")))
    check(car.get("info", {}).get("model") == "Model 3", "model dropped by validation: {}".format(car.get("info")))
    check(len(car.get("entities", {})) == len(CAR_DISCOVERY_ENTITY_SPEC), "not every car entity survived validation: {}".format(car.get("entities")))

    coordinator.assemble()
    catalogue_text = str(coordinator.catalogue())

    check("OHME-SERIAL-99887" in catalogue_text, "the serial should survive in the clear (it is hardware_ids, not a credential), but is missing from the redacted catalogue")
    check("car-123" in catalogue_text, "the vehicle id should survive in the clear, but is missing from the redacted catalogue")
    check("Tesla" in catalogue_text and "Model 3" in catalogue_text, "make/model should survive in the clear, but are missing from the redacted catalogue")
    check(api.client.email not in catalogue_text, "the Ohme login email appears in the clear in the redacted catalogue")

    if failed == 0:
        print("PASS: build_discovery round-trips through the real Coordinator and Redactor - nothing intended was dropped, and no login email leaked")
    return failed


def _test_ohme_build_discovery_never_leaks_login_email(my_predbat=None):
    """
    The Ohme login email must never enter the catalogue - not in info, not in a device_id, not
    embedded in an entity name - in any form, not just its raw string.

    build_discovery() never reads self.client.email/_password at all, so this is a regression
    guard: it fails loudly if a future change ever starts threading the login identifier into any
    field this reporter populates, checking the raw address, its case-folded form and its local
    part in isolation (the fragment most likely to survive a careless partial redaction).
    """
    print("**** Running test_ohme_build_discovery_never_leaks_login_email ****")

    api = MockOhmeAPI()
    api.email = "Driver@Example.com"
    api.client.email = "Driver@Example.com"
    api.client.serial = "TEST-SERIAL-123"
    api.client._cars = [_known_vehicle()]
    _stage_all_discovery_entities(api)

    report = api.build_discovery()
    report_text = str(report)

    email = api.client.email
    local_part = email.split("@")[0]

    assert email not in report_text, "the raw login email appears in build_discovery()'s output"
    assert email.lower() not in report_text.lower(), "a case-folded form of the login email appears in build_discovery()'s output"
    assert local_part not in report_text, "the login email's local part appears in build_discovery()'s output"

    print("PASS: the Ohme login email does not enter the discovery report in any form")
    return 0
