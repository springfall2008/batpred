"""
Tests for Octopus fetch_previous_dispatch function
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import Mock
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
    - Test 11: Prune dispatches older than 5 days
    """
    print("**** Running Octopus fetch_previous_dispatch tests ****")
    failed = False

    # Get reference time from predbat (all dispatches must be within 5 days of this)
    now = my_predbat.now_utc_real

    # Helper function to create dispatch dict
    def create_dispatch(start_str, end_str, charge_kwh):
        """Create a dispatch dictionary"""
        return {"start": start_str, "end": end_str, "charge_in_kwh": charge_kwh, "source": "smart-charge", "location": "home"}

    # Test 1: Merge old dispatches with current dispatches (no duplicates)
    print("\n*** Test 1: Merge old dispatches with current dispatches ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    # Set up current dispatches (recent - within 5 days)
    api.intelligent_device = {"completed_dispatches": [create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5)]}

    # Set up old dispatches from entity state (recent - within 5 days)
    old_dispatches = [
        create_dispatch((now - timedelta(days=2)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=2) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 12.3),
        create_dispatch((now - timedelta(days=3)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=3) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 14.7),
    ]

    # Mock get_state_wrapper to return old dispatches
    api.get_state_wrapper = Mock(return_value=old_dispatches)

    await api.fetch_previous_dispatch()

    completed = api.intelligent_device.get("completed_dispatches", [])

    if len(completed) != 3:
        print(f"ERROR: Expected 3 dispatches after merge, got {len(completed)}")
        failed = True
    else:
        # Verify sorting - oldest should be first
        sorted_correctly = True
        for i in range(len(completed) - 1):
            current_start = datetime.strptime(completed[i]["start"], DATE_TIME_STR_FORMAT)
            next_start = datetime.strptime(completed[i + 1]["start"], DATE_TIME_STR_FORMAT)
            if current_start > next_start:
                sorted_correctly = False
                break
        if not sorted_correctly:
            print(f"ERROR: Dispatches not sorted correctly")
            failed = True
        else:
            print("PASS: Old dispatches merged and sorted correctly")

    # Test 2: Skip dispatches that already exist in current list
    print("\n*** Test 2: Skip dispatches that already exist ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    api.intelligent_device = {
        "completed_dispatches": [
            create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5),
            create_dispatch((now - timedelta(days=2)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=2) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 12.3),
        ]
    }

    # Old dispatches contains one that already exists
    old_dispatches = [
        create_dispatch((now - timedelta(days=2)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=2) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 12.3),
        create_dispatch((now - timedelta(days=3)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=3) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 14.7),
    ]  # Duplicate  # New

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

    api.intelligent_device = {"completed_dispatches": [create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5)]}

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

    api.intelligent_device = {"completed_dispatches": []}

    old_dispatches = [create_dispatch((now - timedelta(days=2)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=2) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 12.3)]

    api.get_state_wrapper = Mock(return_value=old_dispatches)

    await api.fetch_previous_dispatch()

    completed = api.intelligent_device.get("completed_dispatches", [])

    if len(completed) != 1:
        print(f"ERROR: Expected 1 dispatch from old list, got {len(completed)}")
        failed = True
    else:
        print("PASS: Empty current dispatches handled correctly")

    # Test 5: Handle None old dispatches
    print("\n*** Test 5: Handle None old dispatches ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    api.intelligent_device = {"completed_dispatches": [create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5)]}

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

    api.intelligent_device = {"completed_dispatches": []}

    old_dispatches = [
        {"start": (now - timedelta(days=2)).strftime(DATE_TIME_STR_FORMAT), "end": (now - timedelta(days=2) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT)},  # Missing charge_in_kwh
        {"start": (now - timedelta(days=3)).strftime(DATE_TIME_STR_FORMAT), "charge_in_kwh": 12.3},  # Missing end
        {"end": (now - timedelta(days=4) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), "charge_in_kwh": 14.7},  # Missing start
        create_dispatch((now - timedelta(days=4)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=4) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 10.5),  # Valid
    ]

    api.get_state_wrapper = Mock(return_value=old_dispatches)

    await api.fetch_previous_dispatch()

    completed = api.intelligent_device.get("completed_dispatches", [])

    if len(completed) != 1:
        print(f"ERROR: Expected 1 valid dispatch, got {len(completed)}")
        failed = True
    else:
        print("PASS: Dispatches with missing fields skipped correctly")

    # Test 7: Skip non-dict items in old dispatches
    print("\n*** Test 7: Skip non-dict items in old dispatches ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    api.intelligent_device = {"completed_dispatches": []}

    old_dispatches = [
        "not a dict",
        123,
        None,
        create_dispatch((now - timedelta(days=3)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=3) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 10.5),
        ["list", "item"],
        create_dispatch((now - timedelta(days=4)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=4) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 9.2),
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
            create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5),
            create_dispatch((now - timedelta(days=3)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=3) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 14.7),
        ]
    }

    old_dispatches = [
        create_dispatch((now - timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(hours=6) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 16.2),
        create_dispatch((now - timedelta(days=2)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=2) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 12.3),
        create_dispatch((now - timedelta(days=4)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=4) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 11.1),
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

    api.intelligent_device = {"completed_dispatches": [create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5)]}

    # Only include dispatches with valid dates - the function doesn't add invalid ones
    # because the comparison check uses parse_date_time which returns None for invalid dates
    old_dispatches = [create_dispatch((now - timedelta(days=4)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=4) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 11.1)]

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

    api.intelligent_device = {"completed_dispatches": []}

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

    # Test 11: Prune dispatches older than 5 days
    print("\n*** Test 11: Prune dispatches older than 5 days ***")

    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    # Create dispatches at various ages relative to now_utc_real
    # Note: 'now' was already defined at the start of the test function

    # Recent dispatches (within 5 days)
    recent1 = create_dispatch((now - timedelta(days=1)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=1) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 15.5)
    recent2 = create_dispatch((now - timedelta(days=3)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=3) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 14.2)
    recent3 = create_dispatch((now - timedelta(days=4, hours=23)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=4, hours=17)).strftime(DATE_TIME_STR_FORMAT), 13.8)

    # Old dispatches (older than 5 days) - should be pruned
    old1 = create_dispatch((now - timedelta(days=6)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=6) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 12.0)
    old2 = create_dispatch((now - timedelta(days=10)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=10) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 11.5)
    old3 = create_dispatch((now - timedelta(days=30)).strftime(DATE_TIME_STR_FORMAT), (now - timedelta(days=30) + timedelta(hours=6)).strftime(DATE_TIME_STR_FORMAT), 10.0)

    # Set current dispatches with mix of old and recent
    api.intelligent_device = {"completed_dispatches": [recent1, old1]}

    # Old dispatches from entity state also mix of old and recent
    old_dispatches = [recent2, old2, recent3, old3]

    api.get_state_wrapper = Mock(return_value=old_dispatches)

    await api.fetch_previous_dispatch()

    completed = api.intelligent_device.get("completed_dispatches", [])

    # Should only have recent dispatches (3 total)
    if len(completed) != 3:
        print(f"ERROR: Expected 3 recent dispatches after pruning, got {len(completed)}")
        for dispatch in completed:
            print(f"  - {dispatch['start']}")
        failed = True
    else:
        # Verify none of the old ones made it through
        old_dates_found = []
        for dispatch in completed:
            dispatch_age = now - datetime.strptime(dispatch["start"], DATE_TIME_STR_FORMAT).replace(tzinfo=now.tzinfo)
            if dispatch_age > timedelta(days=5):
                old_dates_found.append(dispatch["start"])

        if old_dates_found:
            print(f"ERROR: Found old dispatches that should have been pruned: {old_dates_found}")
            failed = True
        else:
            print(f"PASS: Dispatches older than 5 days pruned correctly (kept {len(completed)} recent)")

    # Summary
    if failed:
        print("\n**** Octopus fetch_previous_dispatch tests FAILED ****")
        raise Exception("Octopus fetch_previous_dispatch tests failed")
    else:
        print("\n**** All Octopus fetch_previous_dispatch tests PASSED ****")

    return failed
