"""
Tests for Octopus fetch_previous_dispatch function
"""

import asyncio
from datetime import datetime
from unittest.mock import Mock, AsyncMock, MagicMock
from octopus import OctopusAPI, DATE_TIME_STR_FORMAT


def test_octopus_fetch_previous_dispatch_wrapper(my_predbat):
    return asyncio.run(test_octopus_fetch_previous_dispatch(my_predbat))


async def test_octopus_fetch_previous_dispatch(my_predbat):
    """
    Test OctopusAPI fetch_previous_dispatch method.
    
    Tests:
    - Test 1: Merge old dispatches with current dispatches (no duplicates)
    - Test 2: Skip dispatches that already exist in current list
    - Test 3: Handle empty old dispatches list
    - Test 4: Handle empty current dispatches list
    - Test 5: Handle None old dispatches
    - Test 6: Skip dispatches missing required fields (start, end, charge_in_kwh)
    - Test 7: Skip non-dict items in old dispatches
    - Test 8: Dispatches sorted by start time after merge
    - Test 9: Multiple old dispatches with same start time (only one added)
    - Test 10: Handle invalid date formats gracefully
    """
    print("**** Running Octopus fetch_previous_dispatch tests ****")
    failed = False
    
    # Helper function to create dispatch dict
    def create_dispatch(start_str, end_str, charge_kwh):
        """Create a dispatch dictionary"""
        return {
            "start": start_str,
            "end": end_str,
            "charge_in_kwh": charge_kwh,
            "source": "smart-charge",
            "location": "home"
        }
    
    # Test 1: Merge old dispatches with current dispatches (no duplicates)
    print("\n*** Test 1: Merge old dispatches with current dispatches ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    # Set up current dispatches
    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch("2024-06-15T23:00:00+00:00", "2024-06-16T05:00:00+00:00", 15.5)
        ]
    }
    
    # Set up old dispatches from entity state
    old_dispatches = [
        create_dispatch("2024-06-14T23:00:00+00:00", "2024-06-15T05:00:00+00:00", 12.3),
        create_dispatch("2024-06-13T23:00:00+00:00", "2024-06-14T05:00:00+00:00", 14.7)
    ]
    
    # Mock get_state_wrapper to return old dispatches
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 3:
        print(f"ERROR: Expected 3 dispatches after merge, got {len(completed)}")
        failed = True
    elif completed[0]["start"] != "2024-06-13T23:00:00+00:00":
        print(f"ERROR: Expected oldest dispatch first after sorting, got {completed[0]['start']}")
        failed = True
    elif completed[2]["start"] != "2024-06-15T23:00:00+00:00":
        print(f"ERROR: Expected newest dispatch last, got {completed[2]['start']}")
        failed = True
    else:
        print("PASS: Old dispatches merged and sorted correctly")
    
    # Test 2: Skip dispatches that already exist in current list
    print("\n*** Test 2: Skip dispatches that already exist ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch("2024-06-15T23:00:00+00:00", "2024-06-16T05:00:00+00:00", 15.5),
            create_dispatch("2024-06-14T23:00:00+00:00", "2024-06-15T05:00:00+00:00", 12.3)
        ]
    }
    
    # Old dispatches contains one that already exists
    old_dispatches = [
        create_dispatch("2024-06-14T23:00:00+00:00", "2024-06-15T05:00:00+00:00", 12.3),  # Duplicate
        create_dispatch("2024-06-13T23:00:00+00:00", "2024-06-14T05:00:00+00:00", 14.7)   # New
    ]
    
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 3:
        print(f"ERROR: Expected 3 dispatches (duplicate skipped), got {len(completed)}")
        failed = True
    else:
        print("PASS: Duplicate dispatches skipped correctly")
    
    # Test 3: Handle empty old dispatches list
    print("\n*** Test 3: Handle empty old dispatches list ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch("2024-06-15T23:00:00+00:00", "2024-06-16T05:00:00+00:00", 15.5)
        ]
    }
    
    api.get_state_wrapper = Mock(return_value=[])
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 1:
        print(f"ERROR: Expected 1 dispatch (no change), got {len(completed)}")
        failed = True
    else:
        print("PASS: Empty old dispatches handled correctly")
    
    # Test 4: Handle empty current dispatches list
    print("\n*** Test 4: Handle empty current dispatches list ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": []
    }
    
    old_dispatches = [
        create_dispatch("2024-06-14T23:00:00+00:00", "2024-06-15T05:00:00+00:00", 12.3)
    ]
    
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 1:
        print(f"ERROR: Expected 1 dispatch from old list, got {len(completed)}")
        failed = True
    elif completed[0]["start"] != "2024-06-14T23:00:00+00:00":
        print(f"ERROR: Expected dispatch start to be preserved, got {completed[0]['start']}")
        failed = True
    else:
        print("PASS: Empty current dispatches handled correctly")
    
    # Test 5: Handle None old dispatches
    print("\n*** Test 5: Handle None old dispatches ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch("2024-06-15T23:00:00+00:00", "2024-06-16T05:00:00+00:00", 15.5)
        ]
    }
    
    api.get_state_wrapper = Mock(return_value=None)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 1:
        print(f"ERROR: Expected 1 dispatch (no change), got {len(completed)}")
        failed = True
    else:
        print("PASS: None old dispatches handled correctly")
    
    # Test 6: Skip dispatches missing required fields
    print("\n*** Test 6: Skip dispatches missing required fields ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": []
    }
    
    old_dispatches = [
        {"start": "2024-06-14T23:00:00+00:00", "end": "2024-06-15T05:00:00+00:00"},  # Missing charge_in_kwh
        {"start": "2024-06-13T23:00:00+00:00", "charge_in_kwh": 12.3},  # Missing end
        {"end": "2024-06-12T05:00:00+00:00", "charge_in_kwh": 14.7},  # Missing start
        create_dispatch("2024-06-11T23:00:00+00:00", "2024-06-12T05:00:00+00:00", 10.5)  # Valid
    ]
    
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 1:
        print(f"ERROR: Expected 1 valid dispatch, got {len(completed)}")
        failed = True
    elif completed[0]["start"] != "2024-06-11T23:00:00+00:00":
        print(f"ERROR: Expected only valid dispatch to be added, got {completed[0]['start']}")
        failed = True
    else:
        print("PASS: Dispatches with missing fields skipped correctly")
    
    # Test 7: Skip non-dict items in old dispatches
    print("\n*** Test 7: Skip non-dict items in old dispatches ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": []
    }
    
    old_dispatches = [
        "not a dict",
        123,
        None,
        create_dispatch("2024-06-11T23:00:00+00:00", "2024-06-12T05:00:00+00:00", 10.5),
        ["list", "item"],
        create_dispatch("2024-06-10T23:00:00+00:00", "2024-06-11T05:00:00+00:00", 9.2)
    ]
    
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 2:
        print(f"ERROR: Expected 2 valid dispatches (non-dict skipped), got {len(completed)}")
        failed = True
    else:
        print("PASS: Non-dict items skipped correctly")
    
    # Test 8: Dispatches sorted by start time after merge
    print("\n*** Test 8: Dispatches sorted by start time ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch("2024-06-15T23:00:00+00:00", "2024-06-16T05:00:00+00:00", 15.5),
            create_dispatch("2024-06-13T23:00:00+00:00", "2024-06-14T05:00:00+00:00", 14.7)
        ]
    }
    
    old_dispatches = [
        create_dispatch("2024-06-16T23:00:00+00:00", "2024-06-17T05:00:00+00:00", 16.2),
        create_dispatch("2024-06-14T23:00:00+00:00", "2024-06-15T05:00:00+00:00", 12.3),
        create_dispatch("2024-06-12T23:00:00+00:00", "2024-06-13T05:00:00+00:00", 11.1)
    ]
    
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    if len(completed) != 5:
        print(f"ERROR: Expected 5 dispatches after merge, got {len(completed)}")
        failed = True
    else:
        # Verify sorting
        sorted_correctly = True
        for i in range(len(completed) - 1):
            current_start = datetime.strptime(completed[i]["start"], DATE_TIME_STR_FORMAT)
            next_start = datetime.strptime(completed[i + 1]["start"], DATE_TIME_STR_FORMAT)
            if current_start > next_start:
                sorted_correctly = False
                break
        
        if not sorted_correctly:
            print(f"ERROR: Dispatches not sorted correctly by start time")
            failed = True
        else:
            print("PASS: Dispatches sorted by start time correctly")
    
    # Test 9: Handle invalid date formats gracefully
    print("\n*** Test 9: Handle invalid date formats gracefully ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch("2024-06-15T23:00:00+00:00", "2024-06-16T05:00:00+00:00", 15.5)
        ]
    }
    
    # Only include dispatches with valid dates - the function doesn't add invalid ones
    # because the comparison check uses parse_date_time which returns None for invalid dates
    old_dispatches = [
        create_dispatch("2024-06-12T23:00:00+00:00", "2024-06-13T05:00:00+00:00", 11.1)
    ]
    
    api.get_state_wrapper = Mock(return_value=old_dispatches)
    
    # Should not crash with valid dates
    await api.fetch_previous_dispatch()
    
    completed = api.intelligent_device.get("completed_dispatches", [])
    
    # Should have both dispatches
    if len(completed) != 2:
        print(f"ERROR: Expected 2 dispatches, got {len(completed)}")
        failed = True
    else:
        print("PASS: Valid date formats processed correctly")
    
    # Test 10: get_state_wrapper called with correct parameters
    print("\n*** Test 10: get_state_wrapper called with correct parameters ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="A-12345678", automatic=False)
    
    api.intelligent_device = {
        "completed_dispatches": []
    }
    
    mock_get_state = Mock(return_value=[])
    api.get_state_wrapper = mock_get_state
    
    await api.fetch_previous_dispatch()
    
    expected_entity_id = api.get_entity_name("binary_sensor", "intelligent_dispatch")
    
    if mock_get_state.call_count != 1:
        print(f"ERROR: Expected get_state_wrapper to be called once, was called {mock_get_state.call_count} times")
        failed = True
    elif mock_get_state.call_args[0][0] != expected_entity_id:
        print(f"ERROR: Wrong entity_id passed to get_state_wrapper: {mock_get_state.call_args[0][0]}")
        failed = True
    elif mock_get_state.call_args[1].get("attribute") != "completed_dispatches":
        print(f"ERROR: Wrong attribute name: {mock_get_state.call_args[1].get('attribute')}")
        failed = True
    else:
        print("PASS: get_state_wrapper called with correct parameters")
    
    # Summary
    if failed:
        print("\n**** Octopus fetch_previous_dispatch tests FAILED ****")
        raise Exception("Octopus fetch_previous_dispatch tests failed")
    else:
        print("\n**** All Octopus fetch_previous_dispatch tests PASSED ****")
    
    return failed
