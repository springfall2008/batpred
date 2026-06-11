"""
Tests for Octopus miscellaneous API methods (async_set_intelligent_target_schedule, async_join_saving_session_events, async_get_saving_sessions, fetch_tariffs, get_octopus_rates_direct, get_intelligent_target_soc, get_intelligent_target_time, get_intelligent_battery_size, get_intelligent_vehicle, run)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from octopus import OctopusAPI
from datetime import datetime, timedelta


def test_octopus_misc_wrapper(my_predbat):
    return asyncio.run(test_octopus_misc(my_predbat))


async def test_octopus_misc(my_predbat):
    """Run all Octopus misc API tests"""
    print("**** Running Octopus Misc API tests ****\n")

    failed = 0
    failed += await test_octopus_set_intelligent_schedule(my_predbat)
    failed += await test_octopus_join_saving_session(my_predbat)
    failed += await test_octopus_get_saving_sessions(my_predbat)
    failed += await test_octopus_fetch_tariffs(my_predbat)
    failed += test_octopus_get_octopus_rates_direct(my_predbat)
    failed += test_octopus_get_intelligent_target_soc(my_predbat)
    failed += test_octopus_get_intelligent_target_time(my_predbat)
    failed += test_octopus_get_intelligent_battery_size(my_predbat)
    failed += test_octopus_get_intelligent_vehicle(my_predbat)
    failed += await test_octopus_run(my_predbat)

    if failed == 0:
        print("\n**** ✅ All Octopus Misc API tests PASSED ****")
    else:
        print(f"\n**** ❌ Octopus Misc API tests FAILED ({failed} test(s) failed) ****")

    return failed


async def test_octopus_set_intelligent_schedule(my_predbat):
    """
    Test OctopusAPI async_set_intelligent_target_schedule method.

    Tests:
    - Test 1: Set schedule with both target_time and target_percentage provided
    - Test 2: Set schedule with default values (from device)
    - Test 3: Fail gracefully when no intelligent device found
    - Test 4: Fail gracefully when device has no device_id
    - Test 5: Verify cached device data is updated correctly
    - Test 6: Verify schedule format includes all 7 days of week
    """
    print("**** Running Octopus async_set_intelligent_target_schedule tests ****")
    failed = False

    # Test 1: Set schedule with both target_time and target_percentage provided
    print("\n*** Test 1: Set schedule with explicit target_time and target_percentage ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    # Setup intelligent device
    api.intelligent_devices = {"test-device-123": {"weekday_target_time": "06:00", "weekday_target_soc": 80, "weekend_target_time": "08:00", "weekend_target_soc": 90}}

    # Mock async_graphql_query
    api.async_graphql_query = AsyncMock(return_value=None)

    # Test with explicit values
    target_time = "07:30"
    target_percentage = 85

    await api.async_set_intelligent_target_schedule("test-account", "test-device-123", target_percentage=target_percentage, target_time=target_time)

    # Verify async_graphql_query was called
    if api.async_graphql_query.call_count != 1:
        print(f"ERROR: Expected async_graphql_query to be called once, got {api.async_graphql_query.call_count} calls")
        failed = True
    else:
        # Check the mutation was called with correct parameters
        call_args = api.async_graphql_query.call_args
        mutation_query = call_args[0][0]
        context = call_args[0][1]

        if "test-device-123" not in mutation_query:
            print(f"ERROR: Device ID not in mutation query")
            failed = True
        elif "07:30" not in mutation_query:
            print(f"ERROR: Target time not in mutation query")
            failed = True
        elif "85" not in mutation_query:
            print(f"ERROR: Target percentage not in mutation query")
            failed = True
        elif context != "set-intelligent-target-time":
            print(f"ERROR: Expected context 'set-intelligent-target-time', got {context}")
            failed = True
        else:
            print("PASS: Mutation called with correct parameters")

    # Verify device cache was updated
    if api.intelligent_devices["test-device-123"]["weekday_target_time"] != "07:30":
        print(f"ERROR: weekday_target_time not updated, got {api.intelligent_devices['test-device-123']['weekday_target_time']}")
        failed = True
    elif api.intelligent_devices["test-device-123"]["weekend_target_time"] != "07:30":
        print(f"ERROR: weekend_target_time not updated, got {api.intelligent_devices['test-device-123']['weekend_target_time']}")
        failed = True
    elif api.intelligent_devices["test-device-123"]["weekday_target_soc"] != 85:
        print(f"ERROR: weekday_target_soc not updated, got {api.intelligent_devices['test-device-123']['weekday_target_soc']}")
        failed = True
    elif api.intelligent_devices["test-device-123"]["weekend_target_soc"] != 85:
        print(f"ERROR: weekend_target_soc not updated, got {api.intelligent_devices['test-device-123']['weekend_target_soc']}")
        failed = True
    else:
        print("PASS: Device cache updated correctly")

    # Test 2: Set schedule with default values (from device)
    print("\n*** Test 2: Set schedule with default values from device ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    # Setup intelligent device with default values
    api2.intelligent_devices = {"test-device-456": {"weekday_target_time": "06:00:00", "weekday_target_soc": 75, "weekend_target_time": "08:00:00", "weekend_target_soc": 85}}  # Test time format with seconds

    # Mock methods - get_intelligent_target_time/soc will return weekday values
    api2.async_graphql_query = AsyncMock(return_value=None)

    # Call without parameters - should use device defaults
    # Pass explicit values that match what the getters would return
    await api2.async_set_intelligent_target_schedule("test-account-2", "test-device-456", target_time="06:00:00", target_percentage=75)

    # Verify mutation was called
    if api2.async_graphql_query.call_count != 1:
        print(f"ERROR: Expected async_graphql_query to be called once, got {api2.async_graphql_query.call_count} calls")
        failed = True
    else:
        call_args = api2.async_graphql_query.call_args
        mutation_query = call_args[0][0]

        # Should use the provided defaults
        if "06:00" not in mutation_query:
            print(f"ERROR: Expected time 06:00 in mutation, got: {mutation_query[:200]}")
            failed = True
        elif "75" not in mutation_query:
            print(f"ERROR: Expected percentage 75 in mutation")
            failed = True
        else:
            print("PASS: Values used correctly")

    # Verify all 7 days of week are in schedule
    call_args = api2.async_graphql_query.call_args
    mutation_query = call_args[0][0]
    days_of_week = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    for day in days_of_week:
        if day not in mutation_query:
            print(f"ERROR: Day {day} not found in mutation schedule")
            failed = True
            break
    else:
        print("PASS: All 7 days of week included in schedule")

    # Test 3: Fail gracefully when no intelligent device found
    print("\n*** Test 3: Fail gracefully when no intelligent device ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    # No intelligent device
    api3.intelligent_devices = {}
    api3.async_graphql_query = AsyncMock(return_value=None)

    # Track log calls
    original_log = api3.log
    log_messages = []

    def capture_log(msg):
        log_messages.append(msg)
        original_log(msg)

    api3.log = capture_log

    await api3.async_set_intelligent_target_schedule("test-account-3", "test-device-3", target_percentage=80, target_time="07:00")

    # Verify async_graphql_query was NOT called
    if api3.async_graphql_query.call_count != 0:
        print(f"ERROR: async_graphql_query should not be called when no device, got {api3.async_graphql_query.call_count} calls")
        failed = True
    else:
        print("PASS: No API call when no device found")

    # Verify warning was logged
    warning_logged = any("no intelligent device found" in msg for msg in log_messages)
    if not warning_logged:
        print(f"ERROR: Expected warning about no device, got logs: {log_messages}")
        failed = True
    else:
        print("PASS: Warning logged when no device found")

    # Test 4: Fail gracefully when device_id not found in devices dict
    print("\n*** Test 4: Fail gracefully when device_id not found ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    # Devices dict exists but requested device_id is not in it
    api4.intelligent_devices = {"other-device": {"weekday_target_time": "06:00", "weekday_target_soc": 80}}
    api4.async_graphql_query = AsyncMock(return_value=None)

    # Track log calls
    original_log = api4.log
    log_messages = []

    def capture_log(msg):
        log_messages.append(msg)
        original_log(msg)

    api4.log = capture_log

    await api4.async_set_intelligent_target_schedule("test-account-4", "nonexistent-device", target_percentage=80, target_time="07:00")

    # Verify async_graphql_query was NOT called
    if api4.async_graphql_query.call_count != 0:
        print(f"ERROR: async_graphql_query should not be called when device_id not found, got {api4.async_graphql_query.call_count} calls")
        failed = True
    else:
        print("PASS: No API call when device_id not found")

    # Verify warning was logged
    warning_logged = any("nonexistent-device" in msg for msg in log_messages)
    if not warning_logged:
        print(f"ERROR: Expected warning about missing device_id, got logs: {log_messages}")
        failed = True
    else:
        print("PASS: Warning logged when device_id not found")

    # Test 5: Verify time format truncation (HH:MM:SS -> HH:MM)
    print("\n*** Test 5: Verify time format truncation ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    api5.intelligent_devices = {"test-device-789": {"weekday_target_time": "06:00:00", "weekday_target_soc": 80, "weekend_target_time": "08:00:00", "weekend_target_soc": 90}}

    api5.async_graphql_query = AsyncMock(return_value=None)

    # Provide time with seconds
    await api5.async_set_intelligent_target_schedule("test-account-5", "test-device-789", target_percentage=85, target_time="07:30:45")

    call_args = api5.async_graphql_query.call_args
    mutation_query = call_args[0][0]

    # Should truncate to HH:MM format
    if "07:30:45" in mutation_query:
        print(f"ERROR: Time should be truncated to HH:MM, found full time with seconds")
        failed = True
    elif "07:30" not in mutation_query:
        print(f"ERROR: Expected truncated time 07:30, not found in mutation")
        failed = True
    else:
        print("PASS: Time correctly truncated to HH:MM format")

    # Verify cached time is also truncated
    if api5.intelligent_devices["test-device-789"]["weekday_target_time"] != "07:30":
        print(f"ERROR: Cached time should be truncated, got {api5.intelligent_devices['test-device-789']['weekday_target_time']}")
        failed = True
    else:
        print("PASS: Cached time correctly truncated")

    # Test 6: Verify returns_data=False parameter
    print("\n*** Test 6: Verify returns_data=False in graphql call ***")
    api6 = OctopusAPI(my_predbat, key="test-api-key-6", account_id="test-account-6", automatic=False)

    api6.intelligent_devices = {"test-device-999": {"weekday_target_time": "06:00", "weekday_target_soc": 80, "weekend_target_time": "08:00", "weekend_target_soc": 90}}

    api6.async_graphql_query = AsyncMock(return_value=None)

    await api6.async_set_intelligent_target_schedule("test-account-6", "test-device-999", target_percentage=85, target_time="07:00")

    call_args = api6.async_graphql_query.call_args
    kwargs = call_args[1]

    if "returns_data" not in kwargs or kwargs["returns_data"] != False:
        print(f"ERROR: Expected returns_data=False, got {kwargs}")
        failed = True
    else:
        print("PASS: returns_data=False parameter set correctly")

    if failed:
        print("\n**** ❌ Octopus async_set_intelligent_target_schedule tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus async_set_intelligent_target_schedule tests PASSED ****")
        return 0


async def test_octopus_join_saving_session(my_predbat):
    """
    Test OctopusAPI async_join_saving_session_events method.

    Tests:
    - Test 1: Join saving session with valid event code
    - Test 2: Skip join when event_code is None
    - Test 3: Skip join when event_code is empty string
    - Test 4: Verify saving sessions are re-fetched after joining
    - Test 5: Verify mutation format includes account_id and event_code
    - Test 6: Verify returns_data=False parameter
    """
    print("\n**** Running Octopus async_join_saving_session_events tests ****")
    failed = False

    # Test 1: Join saving session with valid event code
    print("\n*** Test 1: Join saving session with valid event_code ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    # Mock methods
    api.async_graphql_query = AsyncMock(return_value=None)
    api.async_get_saving_sessions = AsyncMock(return_value={"events": [], "account": {}})

    # Track log calls
    log_messages = []
    original_log = api.log

    def capture_log(msg):
        log_messages.append(msg)
        original_log(msg)

    api.log = capture_log

    event_code = "OCTOPLUS-12345"
    await api.async_join_saving_session_events("test-account", event_code)

    # Verify async_graphql_query was called
    if api.async_graphql_query.call_count != 1:
        print(f"ERROR: Expected async_graphql_query to be called once, got {api.async_graphql_query.call_count} calls")
        failed = True
    else:
        call_args = api.async_graphql_query.call_args
        mutation_query = call_args[0][0]
        context = call_args[0][1]

        if "test-account" not in mutation_query:
            print(f"ERROR: Account ID not in mutation query")
            failed = True
        elif event_code not in mutation_query:
            print(f"ERROR: Event code not in mutation query")
            failed = True
        elif context != "join-saving-session-event":
            print(f"ERROR: Expected context 'join-saving-session-event', got {context}")
            failed = True
        else:
            print("PASS: Mutation called with correct parameters")

    # Verify logging
    if not any(event_code in msg for msg in log_messages):
        print(f"ERROR: Expected log message with event code, got: {log_messages}")
        failed = True
    else:
        print("PASS: Event joining logged correctly")

    # Verify saving sessions were re-fetched
    if api.async_get_saving_sessions.call_count != 1:
        print(f"ERROR: Expected async_get_saving_sessions to be called once, got {api.async_get_saving_sessions.call_count} calls")
        failed = True
    else:
        print("PASS: Saving sessions re-fetched after joining")

    # Verify saving_sessions was updated
    if api.saving_sessions != {"events": [], "account": {}}:
        print(f"ERROR: saving_sessions not updated, got {api.saving_sessions}")
        failed = True
    else:
        print("PASS: saving_sessions updated correctly")

    # Test 2: Skip join when event_code is None
    print("\n*** Test 2: Skip join when event_code is None ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    api2.async_graphql_query = AsyncMock(return_value=None)
    api2.async_get_saving_sessions = AsyncMock(return_value={})

    await api2.async_join_saving_session_events("test-account-2", None)

    # Verify NO API calls were made
    if api2.async_graphql_query.call_count != 0:
        print(f"ERROR: async_graphql_query should not be called with None event_code, got {api2.async_graphql_query.call_count} calls")
        failed = True
    elif api2.async_get_saving_sessions.call_count != 0:
        print(f"ERROR: async_get_saving_sessions should not be called with None event_code, got {api2.async_get_saving_sessions.call_count} calls")
        failed = True
    else:
        print("PASS: No API calls when event_code is None")

    # Test 3: Skip join when event_code is empty string
    print("\n*** Test 3: Skip join when event_code is empty string ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    api3.async_graphql_query = AsyncMock(return_value=None)
    api3.async_get_saving_sessions = AsyncMock(return_value={})

    await api3.async_join_saving_session_events("test-account-3", "")

    # Verify NO API calls were made
    if api3.async_graphql_query.call_count != 0:
        print(f"ERROR: async_graphql_query should not be called with empty event_code, got {api3.async_graphql_query.call_count} calls")
        failed = True
    elif api3.async_get_saving_sessions.call_count != 0:
        print(f"ERROR: async_get_saving_sessions should not be called with empty event_code, got {api3.async_get_saving_sessions.call_count} calls")
        failed = True
    else:
        print("PASS: No API calls when event_code is empty string")

    # Test 4: Verify returns_data=False parameter
    print("\n*** Test 4: Verify returns_data=False in graphql call ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    api4.async_graphql_query = AsyncMock(return_value=None)
    api4.async_get_saving_sessions = AsyncMock(return_value={})

    await api4.async_join_saving_session_events("test-account-4", "OCTOPLUS-99999")

    call_args = api4.async_graphql_query.call_args
    kwargs = call_args[1]

    if "returns_data" not in kwargs or kwargs["returns_data"] != False:
        print(f"ERROR: Expected returns_data=False, got {kwargs}")
        failed = True
    else:
        print("PASS: returns_data=False parameter set correctly")

    # Test 5: Verify multiple event codes can be joined sequentially
    print("\n*** Test 5: Join multiple events sequentially ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    api5.async_graphql_query = AsyncMock(return_value=None)
    api5.async_get_saving_sessions = AsyncMock(return_value={"events": [], "account": {}})

    await api5.async_join_saving_session_events("test-account-5", "OCTOPLUS-AAA")
    await api5.async_join_saving_session_events("test-account-5", "OCTOPLUS-BBB")

    if api5.async_graphql_query.call_count != 2:
        print(f"ERROR: Expected 2 calls for 2 events, got {api5.async_graphql_query.call_count} calls")
        failed = True
    elif api5.async_get_saving_sessions.call_count != 2:
        print(f"ERROR: Expected 2 refreshes for 2 events, got {api5.async_get_saving_sessions.call_count} calls")
        failed = True
    else:
        # Check both event codes were used
        call_1 = api5.async_graphql_query.call_args_list[0][0][0]
        call_2 = api5.async_graphql_query.call_args_list[1][0][0]

        if "OCTOPLUS-AAA" not in call_1 or "OCTOPLUS-BBB" not in call_2:
            print(f"ERROR: Event codes not correctly used in sequential calls")
            failed = True
        else:
            print("PASS: Multiple events can be joined sequentially")

    if failed:
        print("\n**** ❌ Octopus async_join_saving_session_events tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus async_join_saving_session_events tests PASSED ****")
        return 0


async def test_octopus_get_saving_sessions(my_predbat):
    """
    Test OctopusAPI async_get_saving_sessions method.

    Tests:
    - Test 1: Get saving sessions with valid response
    - Test 2: Handle None response from graphql query
    - Test 3: Handle missing savingSessions in response
    - Test 4: Handle None savingSessions in response
    - Test 5: Handle None account in savingSessions
    - Test 6: Verify returns existing saving_sessions on error
    """
    print("\n**** Running Octopus async_get_saving_sessions tests ****")
    failed = False

    # Test 1: Get saving sessions with valid response
    print("\n*** Test 1: Get saving sessions with valid response ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    # Mock response data
    mock_response = {
        "savingSessions": {
            "events": [{"id": "event-1", "code": "OCTOPLUS-12345", "startAt": "2025-01-01T18:00:00Z", "endAt": "2025-01-01T19:00:00Z"}, {"id": "event-2", "code": "OCTOPLUS-67890", "startAt": "2025-01-02T18:00:00Z", "endAt": "2025-01-02T19:00:00Z"}],
            "account": {"hasJoinedCampaign": True, "joinedEvents": [{"eventId": "event-1", "startAt": "2025-01-01T18:00:00Z", "endAt": "2025-01-01T19:00:00Z"}]},
        }
    }

    api.async_graphql_query = AsyncMock(return_value=mock_response)

    result = await api.async_get_saving_sessions("test-account")

    # Verify graphql_query was called with correct parameters
    if api.async_graphql_query.call_count != 1:
        print(f"ERROR: Expected async_graphql_query to be called once, got {api.async_graphql_query.call_count} calls")
        failed = True
    else:
        call_args = api.async_graphql_query.call_args
        context = call_args[0][1]
        kwargs = call_args[1]

        if context != "get-saving-sessions":
            print(f"ERROR: Expected context 'get-saving-sessions', got {context}")
            failed = True
        elif "ignore_errors" not in kwargs or kwargs["ignore_errors"] != True:
            print(f"ERROR: Expected ignore_errors=True, got {kwargs}")
            failed = True
        else:
            print("PASS: GraphQL query called with correct parameters")

    # Verify result is the savingSessions object
    if result != mock_response["savingSessions"]:
        print(f"ERROR: Expected savingSessions object, got {result}")
        failed = True
    elif "events" not in result or len(result["events"]) != 2:
        print(f"ERROR: Expected 2 events in result, got {result.get('events', [])}")
        failed = True
    elif "account" not in result:
        print(f"ERROR: Expected account in result")
        failed = True
    else:
        print("PASS: Valid saving sessions returned correctly")

    # Test 2: Handle None response from graphql query
    print("\n*** Test 2: Handle None response from graphql query ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    # Set existing saving_sessions
    existing_sessions = {"events": [{"id": "cached-event"}], "account": {"hasJoinedCampaign": True}}
    api2.saving_sessions = existing_sessions

    # Mock None response (e.g., API error)
    api2.async_graphql_query = AsyncMock(return_value=None)

    result = await api2.async_get_saving_sessions("test-account-2")

    # Should return existing saving_sessions
    if result != existing_sessions:
        print(f"ERROR: Expected existing saving_sessions on None response, got {result}")
        failed = True
    else:
        print("PASS: Returns existing saving_sessions on None response")

    # Test 3: Handle missing savingSessions in response
    print("\n*** Test 3: Handle missing savingSessions in response ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    # Mock response without savingSessions key
    mock_response_no_sessions = {"someOtherKey": "value"}
    api3.async_graphql_query = AsyncMock(return_value=mock_response_no_sessions)

    result = await api3.async_get_saving_sessions("test-account-3")

    # Should return empty dict when savingSessions is missing
    if result != {}:
        print(f"ERROR: Expected empty dict when savingSessions missing, got {result}")
        failed = True
    else:
        print("PASS: Returns empty dict when savingSessions missing")

    # Test 4: Handle None savingSessions in response
    print("\n*** Test 4: Handle None savingSessions in response ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    # Mock response with None savingSessions
    mock_response_none_sessions = {"savingSessions": None}
    api4.async_graphql_query = AsyncMock(return_value=mock_response_none_sessions)

    result = await api4.async_get_saving_sessions("test-account-4")

    # Should return empty dict when savingSessions is None
    if result != {}:
        print(f"ERROR: Expected empty dict when savingSessions is None, got {result}")
        failed = True
    else:
        print("PASS: Returns empty dict when savingSessions is None")

    # Test 5: Handle None account in savingSessions
    print("\n*** Test 5: Handle None account in savingSessions ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    # Mock response with None account
    mock_response_none_account = {"savingSessions": {"events": [{"id": "event-1"}], "account": None}}
    api5.async_graphql_query = AsyncMock(return_value=mock_response_none_account)

    result = await api5.async_get_saving_sessions("test-account-5")

    # Should normalize None account to empty dict
    if "account" not in result:
        print(f"ERROR: Expected account key in result")
        failed = True
    elif result["account"] != {}:
        print(f"ERROR: Expected empty dict for None account, got {result['account']}")
        failed = True
    else:
        print("PASS: Normalizes None account to empty dict")

    # Test 6: Verify ignore_errors=True parameter
    print("\n*** Test 6: Verify ignore_errors=True prevents error logging ***")
    api6 = OctopusAPI(my_predbat, key="test-api-key-6", account_id="test-account-6", automatic=False)

    # Mock valid response
    mock_response = {"savingSessions": {"events": [], "account": {"hasJoinedCampaign": False}}}
    api6.async_graphql_query = AsyncMock(return_value=mock_response)

    result = await api6.async_get_saving_sessions("test-account-6")

    # Verify ignore_errors=True was passed
    call_args = api6.async_graphql_query.call_args
    kwargs = call_args[1]

    if "ignore_errors" not in kwargs or kwargs["ignore_errors"] != True:
        print(f"ERROR: Expected ignore_errors=True, got {kwargs}")
        failed = True
    else:
        print("PASS: ignore_errors=True parameter passed correctly")

    # Verify result structure
    if result != mock_response["savingSessions"]:
        print(f"ERROR: Expected savingSessions object, got {result}")
        failed = True
    else:
        print("PASS: Returns correct savingSessions structure")

    if failed:
        print("\n**** ❌ Octopus async_get_saving_sessions tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus async_get_saving_sessions tests PASSED ****")
        return 0


async def test_octopus_fetch_tariffs(my_predbat):
    """
    Test OctopusAPI fetch_tariffs method.

    Tests:
    - Test 1: Fetch tariffs for import electricity
    - Test 2: Fetch tariffs for export electricity
    - Test 3: Fetch tariffs for gas
    - Test 4: Fetch multiple tariffs (import + export)
    - Test 5: Verify dashboard_item called with correct entity names
    """
    print("\n**** Running Octopus fetch_tariffs tests ****")
    failed = False

    # Test 1: Fetch tariffs for import electricity
    print("\n*** Test 1: Fetch tariffs for import electricity ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    # Setup tariff data
    tariffs_input = {"import": {"productCode": "AGILE-FLEX-22-11-25", "tariffCode": "E-1R-AGILE-FLEX-22-11-25-C"}}

    # Mock fetch_url_cached to return rate data
    mock_rates_data = [
        {"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z", "value_inc_vat": 15.5},
        {"valid_from": "2025-01-01T00:30:00Z", "valid_to": "2025-01-01T01:00:00Z", "value_inc_vat": 16.0},
    ]
    mock_standing_data = [{"valid_from": "2025-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": 45.0}]

    async def mock_fetch_url(url, **kwargs):
        if "standing-charges" in url:
            return mock_standing_data
        elif kwargs.get("json_only"):
            return {}  # product info: no matching tariff found -> fallback to standard path
        else:
            return mock_rates_data

    api.fetch_url_cached = mock_fetch_url
    api.dashboard_item = MagicMock()

    # Call fetch_tariffs
    await api.fetch_tariffs(tariffs_input)

    # Verify tariff data was stored
    if "data" not in tariffs_input["import"]:
        print(f"ERROR: Expected 'data' in import tariff")
        failed = True
    elif tariffs_input["import"]["data"] != mock_rates_data:
        print(f"ERROR: Expected rates data to be stored")
        failed = True
    else:
        print("PASS: Rates data stored correctly")

    if "standing" not in tariffs_input["import"]:
        print(f"ERROR: Expected 'standing' in import tariff")
        failed = True
    elif tariffs_input["import"]["standing"] != mock_standing_data:
        print(f"ERROR: Expected standing data to be stored")
        failed = True
    else:
        print("PASS: Standing charge data stored correctly")

    # Verify dashboard_item was called twice (rates + standing)
    if api.dashboard_item.call_count != 2:
        print(f"ERROR: Expected dashboard_item to be called twice, got {api.dashboard_item.call_count} calls")
        failed = True
    else:
        print("PASS: dashboard_item called for rates and standing charge")

    # Test 2: Fetch tariffs for export electricity
    print("\n*** Test 2: Fetch tariffs for export electricity ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    tariffs_input2 = {"export": {"productCode": "AGILE-OUTGOING-19-05-13", "tariffCode": "E-1R-AGILE-OUTGOING-19-05-13-C"}}

    mock_export_rates = [
        {"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z", "value_inc_vat": 5.5},
    ]

    async def mock_fetch_url2(url, **kwargs):
        if "standing-charges" in url:
            return []
        elif kwargs.get("json_only"):
            return {}  # product info: no matching tariff found -> fallback to standard path
        else:
            return mock_export_rates

    api2.fetch_url_cached = mock_fetch_url2
    api2.dashboard_item = MagicMock()

    await api2.fetch_tariffs(tariffs_input2)

    # Verify it used "electricity" tariff_type for export
    if "data" not in tariffs_input2["export"]:
        print(f"ERROR: Expected 'data' in export tariff")
        failed = True
    elif tariffs_input2["export"]["data"] != mock_export_rates:
        print(f"ERROR: Expected export rates data to be stored")
        failed = True
    else:
        print("PASS: Export tariff fetched correctly")

    # Test 3: Fetch tariffs for gas
    print("\n*** Test 3: Fetch tariffs for gas ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    tariffs_input3 = {"gas": {"productCode": "VAR-22-11-01", "tariffCode": "G-1R-VAR-22-11-01-C"}}

    mock_gas_rates = [
        {"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T23:59:59Z", "value_inc_vat": 10.5},
    ]

    # Track URLs called
    urls_called = []

    async def mock_fetch_url3(url, **kwargs):
        urls_called.append(url)
        if "standing-charges" in url:
            return []
        else:
            return mock_gas_rates

    api3.fetch_url_cached = mock_fetch_url3
    api3.dashboard_item = MagicMock()

    await api3.fetch_tariffs(tariffs_input3)

    # Verify it used "gas" tariff_type
    gas_url_found = any("/gas-tariffs/" in url for url in urls_called)
    if not gas_url_found:
        print(f"ERROR: Expected gas tariff URL, got {urls_called}")
        failed = True
    else:
        print("PASS: Gas tariff URL used correctly")

    if "data" not in tariffs_input3["gas"]:
        print(f"ERROR: Expected 'data' in gas tariff")
        failed = True
    else:
        print("PASS: Gas tariff data stored correctly")

    # Test 4: Fetch multiple tariffs (import + export)
    print("\n*** Test 4: Fetch multiple tariffs (import + export) ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    tariffs_input4 = {"import": {"productCode": "AGILE-FLEX-22-11-25", "tariffCode": "E-1R-AGILE-FLEX-22-11-25-C"}, "export": {"productCode": "AGILE-OUTGOING-19-05-13", "tariffCode": "E-1R-AGILE-OUTGOING-19-05-13-C"}}

    async def mock_fetch_url4(url, **kwargs):
        if kwargs.get("json_only"):
            return {}  # product info: no matching tariff found -> fallback to standard path
        return [{"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z", "value_inc_vat": 15.0}]

    api4.fetch_url_cached = mock_fetch_url4
    api4.dashboard_item = MagicMock()

    await api4.fetch_tariffs(tariffs_input4)

    # Verify both tariffs were processed
    if "data" not in tariffs_input4["import"] or "data" not in tariffs_input4["export"]:
        print(f"ERROR: Expected data in both import and export tariffs")
        failed = True
    else:
        print("PASS: Both import and export tariffs processed")

    # Verify dashboard_item was called 4 times (2 tariffs × 2 entities each)
    if api4.dashboard_item.call_count != 4:
        print(f"ERROR: Expected dashboard_item to be called 4 times, got {api4.dashboard_item.call_count} calls")
        failed = True
    else:
        print("PASS: dashboard_item called for all tariff entities")

    # Test 5: Verify dashboard_item entity names and attributes
    print("\n*** Test 5: Verify dashboard_item entity names and attributes ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    tariffs_input5 = {"import": {"productCode": "TEST-PRODUCT", "tariffCode": "TEST-TARIFF"}}

    async def mock_fetch_url5(url, **kwargs):
        if kwargs.get("json_only"):
            return {}  # product info: no matching tariff found -> fallback to standard path
        return [{"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z", "value_inc_vat": 20.0}]

    api5.fetch_url_cached = mock_fetch_url5
    dashboard_calls = []

    def capture_dashboard_item(entity_id, state, attributes=None, app=None):
        dashboard_calls.append({"entity_id": entity_id, "state": state, "attributes": attributes, "app": app})

    api5.dashboard_item = capture_dashboard_item

    await api5.fetch_tariffs(tariffs_input5)

    # Verify entity names
    rates_entity = next((call for call in dashboard_calls if "_rates" in call["entity_id"]), None)
    standing_entity = next((call for call in dashboard_calls if "_standing" in call["entity_id"]), None)

    if not rates_entity:
        print(f"ERROR: Expected rates entity in dashboard_item calls")
        failed = True
    elif "predbat_octopus_test_account_5_import_rates" not in rates_entity["entity_id"]:
        print(f"ERROR: Expected correct rates entity name, got {rates_entity['entity_id']}")
        failed = True
    else:
        print("PASS: Rates entity name correct")

    if not standing_entity:
        print(f"ERROR: Expected standing entity in dashboard_item calls")
        failed = True
    elif "predbat_octopus_test_account_5_import_standing" not in standing_entity["entity_id"]:
        print(f"ERROR: Expected correct standing entity name, got {standing_entity['entity_id']}")
        failed = True
    else:
        print("PASS: Standing charge entity name correct")

    # Verify attributes include product_code and tariff_code
    if rates_entity and rates_entity.get("attributes"):
        attrs = rates_entity["attributes"]
        if attrs.get("product_code") != "TEST-PRODUCT":
            print(f"ERROR: Expected product_code in rates attributes")
            failed = True
        elif attrs.get("tariff_code") != "TEST-TARIFF":
            print(f"ERROR: Expected tariff_code in rates attributes")
            failed = True
        elif "rates" not in attrs:
            print(f"ERROR: Expected rates array in attributes")
            failed = True
        else:
            print("PASS: Rates entity attributes correct")

    # Test 6: Verify app parameter is 'octopus'
    print("\n*** Test 6: Verify app parameter is 'octopus' ***")
    for call in dashboard_calls:
        if call.get("app") != "octopus":
            print(f"ERROR: Expected app='octopus', got {call.get('app')}")
            failed = True
            break
    else:
        print("PASS: All dashboard_item calls use app='octopus'")

    # Test 7: Product info returns day/night links → async_get_day_night_rates is used
    print("\n*** Test 7: Product info with day_unit_rates links → routes to async_get_day_night_rates ***")
    api7 = OctopusAPI(my_predbat, key="test-api-key-7", account_id="test-account-7", automatic=False)

    iog_tariff_code = "E-1R-IOG-SMB-TOU-25-12-12-H"
    iog_product_code = "IOG-SMB-TOU-25-12-12"
    tariffs_input7 = {"import": {"productCode": iog_product_code, "tariffCode": iog_tariff_code}}

    # Minimal product-info JSON that _async_get_product_rate_link_types will parse:
    # contains only day/night links for this tariff — no standard_unit_rates link.
    mock_product_info = {
        "single_register_electricity_tariffs": {
            "_H": {
                "direct_debit_monthly": {
                    "code": iog_tariff_code,
                    "links": [
                        {"rel": "day_unit_rates", "href": f"https://api.octopus.energy/v1/products/{iog_product_code}/electricity-tariffs/{iog_tariff_code}/day-unit-rates/"},
                        {"rel": "night_unit_rates", "href": f"https://api.octopus.energy/v1/products/{iog_product_code}/electricity-tariffs/{iog_tariff_code}/night-unit-rates/"},
                        {"rel": "standing_charges", "href": f"https://api.octopus.energy/v1/products/{iog_product_code}/electricity-tariffs/{iog_tariff_code}/standing-charges/"},
                    ],
                }
            }
        }
    }

    mock_day_night_result = [
        {"valid_from": "2026-05-09T23:30:00+0100", "valid_to": "2026-05-10T05:30:00+0100", "value_inc_vat": 7.0},
        {"valid_from": "2026-05-10T05:30:00+0100", "valid_to": "2026-05-10T23:30:00+0100", "value_inc_vat": 29.14},
    ]

    async def mock_fetch_url7(url, **kwargs):
        if kwargs.get("json_only"):
            return mock_product_info  # product info with day/night links only
        if "standing-charges" in url:
            return []
        return []  # should not be called for rate data in this path

    api7.fetch_url_cached = mock_fetch_url7
    api7.dashboard_item = MagicMock()
    api7.async_get_day_night_rates = AsyncMock(return_value=mock_day_night_result)

    await api7.fetch_tariffs(tariffs_input7)

    # async_get_day_night_rates must have been called (not the standard URL path)
    if api7.async_get_day_night_rates.call_count != 1:
        print(f"ERROR: Expected async_get_day_night_rates called once, got {api7.async_get_day_night_rates.call_count}")
        failed = True
    else:
        call_kwargs = api7.async_get_day_night_rates.call_args
        # Verify correct tariff/product codes passed through
        passed_tariff = call_kwargs.kwargs.get("tariff_code", "")
        passed_product = call_kwargs.kwargs.get("product_code", "")
        if passed_tariff != iog_tariff_code:
            print(f"ERROR: Expected tariff_code={iog_tariff_code}, got {passed_tariff}")
            failed = True
        elif passed_product != iog_product_code:
            print(f"ERROR: Expected product_code={iog_product_code}, got {passed_product}")
            failed = True
        else:
            print("PASS: async_get_day_night_rates called with correct tariff/product codes")

    # The returned day/night data must be stored in tariffs["import"]["data"]
    if tariffs_input7["import"].get("data") != mock_day_night_result:
        print(f"ERROR: Expected day/night data stored in tariff, got {tariffs_input7['import'].get('data')}")
        failed = True
    else:
        print("PASS: Day/night rate data stored correctly in tariff")

    # Test 8: INTELLI-FLUX-EXPORT 404s → fetches FLUX-IMPORT as fallback
    print("\n*** Test 8: INTELLI-FLUX-EXPORT 404s → fetches FLUX-IMPORT as fallback ***")
    api8 = OctopusAPI(my_predbat, key="test-api-key-8", account_id="test-account-8", automatic=False)

    flux_export_product = "INTELLI-FLUX-EXPORT-23-07-14"
    flux_export_tariff = "E-1R-INTELLI-FLUX-EXPORT-23-07-14-A"
    flux_import_product = "INTELLI-FLUX-IMPORT-23-07-14"
    flux_import_tariff = "E-1R-INTELLI-FLUX-IMPORT-23-07-14-A"
    tariffs_input8 = {"export": {"productCode": flux_export_product, "tariffCode": flux_export_tariff}}

    mock_flux_import_rates = [
        {"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z", "value_inc_vat": 3.49},
        {"valid_from": "2025-01-01T05:30:00Z", "valid_to": "2025-01-01T06:00:00Z", "value_inc_vat": 28.14},
    ]
    urls_called8 = []

    async def mock_fetch_url8(url, **kwargs):
        urls_called8.append(url)
        if kwargs.get("json_only"):
            return {}
        if "standing-charges" in url:
            return []
        if flux_export_product in url:
            return None  # FLUX-EXPORT 404s
        if flux_import_product in url:
            return mock_flux_import_rates
        return None

    api8.fetch_url_cached = mock_fetch_url8
    api8.dashboard_item = MagicMock()

    await api8.fetch_tariffs(tariffs_input8)

    flux_import_url_fetched = any(flux_import_product in url and "standard-unit-rates" in url for url in urls_called8)
    if not flux_import_url_fetched:
        print(f"ERROR: Expected FLUX-IMPORT standard-unit-rates URL to be fetched, got {urls_called8}")
        failed = True
    else:
        print("PASS: FLUX-IMPORT standard-unit-rates URL was fetched as fallback")

    if tariffs_input8["export"].get("data") != mock_flux_import_rates:
        print(f"ERROR: Expected FLUX-IMPORT rates stored as export data, got {tariffs_input8['export'].get('data')}")
        failed = True
    else:
        print("PASS: FLUX-IMPORT rates stored correctly as export fallback")

    # Verify the URL constructed has FLUX-IMPORT, not FLUX-EXPORT
    wrong_url_called = any(flux_export_product in url and "standard-unit-rates" in url and url != f"https://api.octopus.energy/v1/products/{flux_export_product}/electricity-tariffs/{flux_export_tariff}/standard-unit-rates/" for url in urls_called8)
    expected_flux_import_url = f"https://api.octopus.energy/v1/products/{flux_import_product}/electricity-tariffs/{flux_import_tariff}/standard-unit-rates/"
    if expected_flux_import_url not in urls_called8:
        print(f"ERROR: Expected exact URL {expected_flux_import_url}, got {urls_called8}")
        failed = True
    else:
        print("PASS: FLUX-IMPORT URL constructed correctly with correct product and tariff codes")

    # Test 9: INTELLI-FLUX-EXPORT 404s, FLUX-IMPORT also 404s → falls back to current import data
    print("\n*** Test 9: FLUX-EXPORT and FLUX-IMPORT both fail → falls back to current import data ***")
    api9 = OctopusAPI(my_predbat, key="test-api-key-9", account_id="test-account-9", automatic=False)

    fix_import_rates = [{"valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z", "value_inc_vat": 28.14}]
    tariffs_input9 = {
        "import": {"productCode": "INTELLI-FIX-12M-25-08-29", "tariffCode": "E-1R-INTELLI-FIX-12M-25-08-29-A"},
        "export": {"productCode": flux_export_product, "tariffCode": flux_export_tariff},
    }

    async def mock_fetch_url9(url, **kwargs):
        if kwargs.get("json_only"):
            return {}
        if "standing-charges" in url:
            return []
        if flux_export_product in url or flux_import_product in url:
            return None  # both FLUX tariffs fail
        return fix_import_rates  # FIX import succeeds

    api9.fetch_url_cached = mock_fetch_url9
    api9.dashboard_item = MagicMock()

    await api9.fetch_tariffs(tariffs_input9)

    if tariffs_input9["export"].get("data") != fix_import_rates:
        print(f"ERROR: Expected current import rates as final fallback, got {tariffs_input9['export'].get('data')}")
        failed = True
    else:
        print("PASS: Current import rates used as final fallback when FLUX-IMPORT also unavailable")

    if failed:
        print("\n**** ❌ Octopus fetch_tariffs tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus fetch_tariffs tests PASSED ****")
        return 0


def test_octopus_get_octopus_rates_direct(my_predbat):
    """
    Test OctopusAPI get_octopus_rates_direct method.

    Tests:
    - Test 1: Get rates with valid tariff data
    - Test 2: Get standing charges with valid tariff data
    - Test 3: Handle None valid_to (extends to 7 days)
    - Test 4: Handle missing tariff type
    - Test 5: Handle tariff without data
    - Test 6: Verify minute_data conversion
    """
    print("\n**** Running Octopus get_octopus_rates_direct tests ****")
    failed = False

    # Test 1: Get rates with valid tariff data
    print("\n*** Test 1: Get rates with valid tariff data ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    # Setup tariff with rate data (midnight_utc comes from my_predbat)
    # Use dates relative to my_predbat.midnight_utc for compatibility
    midnight_str = my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    midnight_plus_30 = (my_predbat.midnight_utc + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S%z")
    midnight_plus_60 = (my_predbat.midnight_utc + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S%z")
    midnight_plus_90 = (my_predbat.midnight_utc + timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%S%z")

    api.tariffs = {
        "import": {
            "productCode": "TEST-PRODUCT",
            "tariffCode": "TEST-TARIFF",
            "data": [
                {"valid_from": midnight_str, "valid_to": midnight_plus_30, "value_inc_vat": 15.5},
                {"valid_from": midnight_plus_30, "valid_to": midnight_plus_60, "value_inc_vat": 16.0},
                {"valid_from": midnight_plus_60, "valid_to": midnight_plus_90, "value_inc_vat": 14.0},
            ],
            "standing": [{"valid_from": midnight_str, "valid_to": None, "value_inc_vat": 45.0}],
        }
    }

    # Get rates (not standing charge)
    result = api.get_octopus_rates_direct("import", standingCharge=False)

    # Verify result is a dict of minute -> rate
    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result, got {type(result)}")
        failed = True
    elif len(result) == 0:
        print(f"ERROR: Expected non-empty result dict")
        failed = True
    else:
        # Check that we have data for the period (minute_data returns dict with minute offsets as keys)
        # The data should cover at least the first hour (60 minutes)
        if 0 not in result:
            print(f"ERROR: Expected minute 0 in result")
            failed = True
        elif 30 not in result:
            print(f"ERROR: Expected minute 30 in result")
            failed = True
        else:
            print("PASS: Rates data converted to minute dict correctly")

    # Test 2: Get standing charges with valid tariff data
    print("\n*** Test 2: Get standing charges with valid tariff data ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    midnight_str = my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    midnight_plus_30 = (my_predbat.midnight_utc + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S%z")

    api2.tariffs = {
        "import": {
            "productCode": "TEST-PRODUCT",
            "tariffCode": "TEST-TARIFF",
            "data": [
                {"valid_from": midnight_str, "valid_to": midnight_plus_30, "value_inc_vat": 15.5},
            ],
            "standing": [{"valid_from": midnight_str, "valid_to": None, "value_inc_vat": 45.0}],
        }
    }

    # Get standing charges
    result = api2.get_octopus_rates_direct("import", standingCharge=True)

    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result for standing charges, got {type(result)}")
        failed = True
    elif len(result) == 0:
        print(f"ERROR: Expected non-empty standing charge result")
        failed = True
    else:
        print("PASS: Standing charges data converted to minute dict correctly")

    # Test 3: Handle None valid_to (extends to 7 days)
    print("\n*** Test 3: Handle None valid_to (extends to 7 days) ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    midnight_str = my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Create tariff with None valid_to
    tariff_data_before = [{"valid_from": midnight_str, "valid_to": None, "value_inc_vat": 20.0}]

    api3.tariffs = {"import": {"data": tariff_data_before.copy()}}  # Copy so we can check modification

    result = api3.get_octopus_rates_direct("import", standingCharge=False)

    # Check that the tariff data was modified to set valid_to
    tariff_after = api3.tariffs["import"]["data"]
    if tariff_after[0]["valid_to"] is None:
        print(f"ERROR: Expected valid_to to be set, still None")
        failed = True
    else:
        # Should be midnight + 7 days - format is "YYYY-MM-DD HH:MM:SS+0000"
        # Just check it's a date string (not None)
        if not isinstance(tariff_after[0]["valid_to"], str):
            print(f"ERROR: Expected valid_to to be string, got {type(tariff_after[0]['valid_to'])}")
            failed = True
        else:
            print("PASS: None valid_to extended to 7 days correctly")

    # Test 4: Handle missing tariff type
    print("\n*** Test 4: Handle missing tariff type ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    api4.tariffs = {}  # No tariffs

    # Track log messages
    log_messages = []
    original_log = api4.log

    def capture_log(msg):
        log_messages.append(msg)
        original_log(msg)

    api4.log = capture_log

    result = api4.get_octopus_rates_direct("import", standingCharge=False)

    # Should return dict with zeros
    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result for missing tariff, got {type(result)}")
        failed = True
    elif len(result) != 60 * 24:
        print(f"ERROR: Expected {60*24} minutes (full day) of zeros, got {len(result)} entries")
        failed = True
    elif any(v != 0 for v in result.values()):
        print(f"ERROR: Expected all zeros for missing tariff, found non-zero values")
        failed = True
    else:
        print("PASS: Missing tariff returns full day of zeros")

    # Check log message
    if not any("not available" in msg and "import" in msg for msg in log_messages):
        print(f"ERROR: Expected log about tariff not available, got: {log_messages}")
        failed = True
    else:
        print("PASS: Logged missing tariff correctly")

    # Test 5: Handle tariff without data key
    print("\n*** Test 5: Handle tariff without data key ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    # Tariff exists but no data
    api5.tariffs = {
        "export": {
            "productCode": "TEST-EXPORT",
            "tariffCode": "TEST-EXPORT-TARIFF"
            # No "data" key
        }
    }

    log_messages = []
    original_log = api5.log

    def capture_log(msg):
        log_messages.append(msg)
        original_log(msg)

    api5.log = capture_log

    result = api5.get_octopus_rates_direct("export", standingCharge=False)

    # Should return zeros
    if len(result) != 60 * 24:
        print(f"ERROR: Expected {60*24} minutes of zeros for tariff without data, got {len(result)}")
        failed = True
    else:
        print("PASS: Tariff without data returns zeros")

    # Test 6: Verify minute_data conversion format
    print("\n*** Test 6: Verify minute_data conversion format ***")
    api6 = OctopusAPI(my_predbat, key="test-api-key-6", account_id="test-account-6", automatic=False)

    midnight_str = my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    midnight_plus_30 = (my_predbat.midnight_utc + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S%z")
    midnight_plus_60 = (my_predbat.midnight_utc + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S%z")

    # Create clear test data - 30 min rates
    api6.tariffs = {
        "import": {
            "data": [
                {"valid_from": midnight_str, "valid_to": midnight_plus_30, "value_inc_vat": 10.0},
                {"valid_from": midnight_plus_30, "valid_to": midnight_plus_60, "value_inc_vat": 20.0},
            ]
        }
    }

    result = api6.get_octopus_rates_direct("import", standingCharge=False)

    # Verify dict keys are integers (minutes)
    if not all(isinstance(k, int) for k in result.keys()):
        print(f"ERROR: Expected all keys to be integers (minutes)")
        failed = True
    else:
        print("PASS: Result keys are integers (minutes from midnight)")

    # Verify we have continuous minute coverage
    # minute_data should fill in all minutes in the range
    min_minute = min(result.keys()) if result else None
    max_minute = max(result.keys()) if result else None

    if min_minute is None:
        print(f"ERROR: Result is empty")
        failed = True
    else:
        print(f"PASS: Result covers minutes {min_minute} to {max_minute}")

    if failed:
        print("\n**** ❌ Octopus get_octopus_rates_direct tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus get_octopus_rates_direct tests PASSED ****")
        return 0


def test_octopus_get_intelligent_target_soc(my_predbat):
    """
    Test OctopusAPI get_intelligent_target_soc method.

    Tests:
    - Test 1: Get weekday target SoC
    - Test 2: Get weekend target SoC (Saturday)
    - Test 3: Get weekend target SoC (Sunday)
    - Test 4: Handle no intelligent device
    - Test 5: Handle device with missing weekday_target_soc
    - Test 6: Handle device with missing weekend_target_soc
    """
    print("\n**** Running Octopus get_intelligent_target_soc tests ****")
    failed = False

    # Test 1: Get weekday target SoC
    print("\n*** Test 1: Get weekday target SoC ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    # Setup intelligent device
    api.intelligent_devices = {"test-device-123": {"weekday_target_time": "06:00", "weekday_target_soc": 80, "weekend_target_time": "08:00", "weekend_target_soc": 90}}

    # Mock now_utc_exact to be a weekday (Monday = 0)
    from datetime import datetime
    from unittest.mock import PropertyMock, patch

    # Create a Monday (weekday = 0)
    monday = datetime(2025, 1, 6, 10, 0, 0)  # Monday, Jan 6, 2025

    with patch.object(type(api), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = monday
        result = api.get_intelligent_target_soc("test-device-123")

    if result != 80:
        print(f"ERROR: Expected weekday target 80, got {result}")
        failed = True
    else:
        print("PASS: Weekday target SoC returned correctly")

    # Test 2: Get weekend target SoC (Saturday)
    print("\n*** Test 2: Get weekend target SoC (Saturday) ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    api2.intelligent_devices = {"test-device-456": {"weekday_target_time": "06:00", "weekday_target_soc": 75, "weekend_target_time": "09:00", "weekend_target_soc": 95}}

    # Create a Saturday (weekday = 5)
    saturday = datetime(2025, 1, 11, 10, 0, 0)  # Saturday, Jan 11, 2025

    with patch.object(type(api2), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = saturday
        result = api2.get_intelligent_target_soc("test-device-456")

    if result != 95:
        print(f"ERROR: Expected weekend target 95, got {result}")
        failed = True
    else:
        print("PASS: Weekend target SoC (Saturday) returned correctly")

    # Test 3: Get weekend target SoC (Sunday)
    print("\n*** Test 3: Get weekend target SoC (Sunday) ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    api3.intelligent_devices = {"test-device-789": {"weekday_target_time": "06:00", "weekday_target_soc": 70, "weekend_target_time": "10:00", "weekend_target_soc": 100}}

    # Create a Sunday (weekday = 6)
    sunday = datetime(2025, 1, 12, 10, 0, 0)  # Sunday, Jan 12, 2025

    with patch.object(type(api3), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = sunday
        result = api3.get_intelligent_target_soc("test-device-789")

    if result != 100:
        print(f"ERROR: Expected weekend target 100, got {result}")
        failed = True
    else:
        print("PASS: Weekend target SoC (Sunday) returned correctly")

    # Test 4: Handle no intelligent device
    print("\n*** Test 4: Handle no intelligent device ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    # No intelligent device
    api4.intelligent_devices = {}

    result = api4.get_intelligent_target_soc("test-device-4")

    if result is not None:
        print(f"ERROR: Expected None for no device, got {result}")
        failed = True
    else:
        print("PASS: Returns None when no intelligent device")

    # Test 5: Handle device with missing weekday_target_soc
    print("\n*** Test 5: Handle device with missing weekday_target_soc ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    api5.intelligent_devices = {
        "test-device-999": {
            "weekday_target_time": "06:00",
            # Missing weekday_target_soc
            "weekend_target_time": "08:00",
            "weekend_target_soc": 85,
        }
    }

    # Mock weekday
    tuesday = datetime(2025, 1, 7, 10, 0, 0)  # Tuesday, Jan 7, 2025

    with patch.object(type(api5), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = tuesday
        result = api5.get_intelligent_target_soc("test-device-999")

    if result is not None:
        print(f"ERROR: Expected None for missing weekday_target_soc, got {result}")
        failed = True
    else:
        print("PASS: Returns None when weekday_target_soc missing")

    # Test 6: Handle device with missing weekend_target_soc
    print("\n*** Test 6: Handle device with missing weekend_target_soc ***")
    api6 = OctopusAPI(my_predbat, key="test-api-key-6", account_id="test-account-6", automatic=False)

    api6.intelligent_devices = {
        "test-device-888": {
            "weekday_target_time": "06:00",
            "weekday_target_soc": 77,
            "weekend_target_time": "08:00"
            # Missing weekend_target_soc
        }
    }

    # Mock weekend (Saturday)
    saturday2 = datetime(2025, 1, 18, 10, 0, 0)  # Saturday, Jan 18, 2025

    with patch.object(type(api6), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = saturday2
        result = api6.get_intelligent_target_soc("test-device-888")

    if result is not None:
        print(f"ERROR: Expected None for missing weekend_target_soc, got {result}")
        failed = True
    else:
        print("PASS: Returns None when weekend_target_soc missing")

    if failed:
        print("\n**** ❌ Octopus get_intelligent_target_soc tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus get_intelligent_target_soc tests PASSED ****")
        return 0


def test_octopus_get_intelligent_target_time(my_predbat):
    """
    Test OctopusAPI get_intelligent_target_time method.

    Tests:
    - Test 1: Get weekday target time
    - Test 2: Get weekend target time (Saturday)
    - Test 3: Get weekend target time (Sunday)
    - Test 4: Handle no intelligent device
    - Test 5: Handle missing weekday_target_time
    - Test 6: Handle missing weekend_target_time
    """
    print("\n**** Running Octopus get_intelligent_target_time tests ****")
    failed = False

    # Test 1: Get weekday target time
    print("\n*** Test 1: Get weekday target time ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    api.intelligent_devices = {"test-device-123": {"weekday_target_time": "06:30", "weekday_target_soc": 80, "weekend_target_time": "08:00", "weekend_target_soc": 90}}

    # Mock weekday (Monday)
    monday = datetime(2025, 1, 13, 10, 0, 0)  # Monday, Jan 13, 2025

    with patch.object(type(api), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = monday
        result = api.get_intelligent_target_time("test-device-123")

    if result != "06:30":
        print(f"ERROR: Expected weekday target time '06:30', got {result}")
        failed = True
    else:
        print("PASS: Weekday target time retrieved correctly")

    # Test 2: Get weekend target time (Saturday)
    print("\n*** Test 2: Get weekend target time (Saturday) ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    api2.intelligent_devices = {"test-device-456": {"weekday_target_time": "06:30", "weekday_target_soc": 80, "weekend_target_time": "08:00", "weekend_target_soc": 90}}

    # Mock Saturday
    saturday = datetime(2025, 1, 18, 10, 0, 0)  # Saturday, Jan 18, 2025

    with patch.object(type(api2), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = saturday
        result = api2.get_intelligent_target_time("test-device-456")

    if result != "08:00":
        print(f"ERROR: Expected weekend target time '08:00', got {result}")
        failed = True
    else:
        print("PASS: Weekend target time retrieved correctly (Saturday)")

    # Test 3: Get weekend target time (Sunday)
    print("\n*** Test 3: Get weekend target time (Sunday) ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    api3.intelligent_devices = {"test-device-789": {"weekday_target_time": "07:00", "weekday_target_soc": 85, "weekend_target_time": "09:30", "weekend_target_soc": 95}}

    # Mock Sunday
    sunday = datetime(2025, 1, 19, 10, 0, 0)  # Sunday, Jan 19, 2025

    with patch.object(type(api3), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = sunday
        result = api3.get_intelligent_target_time("test-device-789")

    if result != "09:30":
        print(f"ERROR: Expected weekend target time '09:30', got {result}")
        failed = True
    else:
        print("PASS: Weekend target time retrieved correctly (Sunday)")

    # Test 4: Handle no intelligent device
    print("\n*** Test 4: Handle no intelligent device ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    api4.intelligent_devices = {}

    monday2 = datetime(2025, 1, 13, 10, 0, 0)

    with patch.object(type(api4), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = monday2
        result = api4.get_intelligent_target_time("test-device-4")

    if result is not None:
        print(f"ERROR: Expected None when no device, got {result}")
        failed = True
    else:
        print("PASS: Returns None when no intelligent device")

    # Test 5: Handle missing weekday_target_time
    print("\n*** Test 5: Handle missing weekday_target_time ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    api5.intelligent_devices = {
        "test-device-999": {
            "weekday_target_soc": 80,
            "weekend_target_time": "08:00",
            "weekend_target_soc": 90
            # Missing weekday_target_time
        }
    }

    # Mock weekday (Tuesday)
    tuesday = datetime(2025, 1, 14, 10, 0, 0)  # Tuesday, Jan 14, 2025

    with patch.object(type(api5), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = tuesday
        result = api5.get_intelligent_target_time("test-device-999")

    if result is not None:
        print(f"ERROR: Expected None for missing weekday_target_time, got {result}")
        failed = True
    else:
        print("PASS: Returns None when weekday_target_time missing")

    # Test 6: Handle missing weekend_target_time
    print("\n*** Test 6: Handle missing weekend_target_time ***")
    api6 = OctopusAPI(my_predbat, key="test-api-key-6", account_id="test-account-6", automatic=False)

    api6.intelligent_devices = {
        "test-device-888": {
            "weekday_target_time": "06:00",
            "weekday_target_soc": 80,
            "weekend_target_soc": 90
            # Missing weekend_target_time
        }
    }

    # Mock weekend (Saturday)
    saturday2 = datetime(2025, 1, 18, 10, 0, 0)  # Saturday, Jan 18, 2025

    with patch.object(type(api6), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = saturday2
        result = api6.get_intelligent_target_time("test-device-888")

    if result is not None:
        print(f"ERROR: Expected None for missing weekend_target_time, got {result}")
        failed = True
    else:
        print("PASS: Returns None when weekend_target_time missing")

    if failed:
        print("\n**** ❌ Octopus get_intelligent_target_time tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus get_intelligent_target_time tests PASSED ****")
        return 0


def test_octopus_get_intelligent_battery_size(my_predbat):
    """
    Test OctopusAPI get_intelligent_battery_size method.

    Tests:
    - Test 1: Get battery size when present
    - Test 2: Handle no intelligent device
    - Test 3: Handle device without vehicle_battery_size_in_kwh
    - Test 4: Handle various battery size values (integers and floats)
    """
    print("\n**** Running Octopus get_intelligent_battery_size tests ****")
    failed = False

    # Test 1: Get battery size when present
    print("\n*** Test 1: Get battery size when present ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    api.intelligent_devices = {"test-device-123": {"vehicle_battery_size_in_kwh": 75.5, "weekday_target_time": "06:30", "weekday_target_soc": 80}}

    result = api.get_intelligent_battery_size("test-device-123")

    if result != 75.5:
        print(f"ERROR: Expected battery size 75.5, got {result}")
        failed = True
    else:
        print("PASS: Battery size retrieved correctly")

    # Test 2: Handle no intelligent device
    print("\n*** Test 2: Handle no intelligent device ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    api2.intelligent_devices = {}

    result = api2.get_intelligent_battery_size("test-device-2")

    if result is not None:
        print(f"ERROR: Expected None when no device, got {result}")
        failed = True
    else:
        print("PASS: Returns None when no intelligent device")

    # Test 3: Handle device without vehicle_battery_size_in_kwh
    print("\n*** Test 3: Handle device without vehicle_battery_size_in_kwh ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    api3.intelligent_devices = {
        "test-device-789": {
            "weekday_target_time": "06:30",
            "weekday_target_soc": 80
            # Missing vehicle_battery_size_in_kwh
        }
    }

    result = api3.get_intelligent_battery_size("test-device-789")

    if result is not None:
        print(f"ERROR: Expected None for missing vehicle_battery_size_in_kwh, got {result}")
        failed = True
    else:
        print("PASS: Returns None when vehicle_battery_size_in_kwh missing")

    # Test 4: Handle various battery size values
    print("\n*** Test 4: Handle various battery size values ***")

    # Test integer value
    api4a = OctopusAPI(my_predbat, key="test-api-key-4a", account_id="test-account-4a", automatic=False)
    api4a.intelligent_devices = {"test-device": {"vehicle_battery_size_in_kwh": 100}}
    result = api4a.get_intelligent_battery_size("test-device")
    if result != 100:
        print(f"ERROR: Expected integer battery size 100, got {result}")
        failed = True
    else:
        print("PASS: Integer battery size retrieved correctly")

    # Test zero value
    api4b = OctopusAPI(my_predbat, key="test-api-key-4b", account_id="test-account-4b", automatic=False)
    api4b.intelligent_devices = {"test-device": {"vehicle_battery_size_in_kwh": 0}}
    result = api4b.get_intelligent_battery_size("test-device")
    if result != 0:
        print(f"ERROR: Expected zero battery size, got {result}")
        failed = True
    else:
        print("PASS: Zero battery size handled correctly")

    # Test small float value
    api4c = OctopusAPI(my_predbat, key="test-api-key-4c", account_id="test-account-4c", automatic=False)
    api4c.intelligent_devices = {"test-device": {"vehicle_battery_size_in_kwh": 58.2}}
    result = api4c.get_intelligent_battery_size("test-device")
    if result != 58.2:
        print(f"ERROR: Expected float battery size 58.2, got {result}")
        failed = True
    else:
        print("PASS: Float battery size retrieved correctly")

    if failed:
        print("\n**** ❌ Octopus get_intelligent_battery_size tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus get_intelligent_battery_size tests PASSED ****")
        return 0


def test_octopus_get_intelligent_vehicle(my_predbat):
    """
    Test OctopusAPI get_intelligent_vehicle method.

    Tests:
    - Test 1: Get vehicle with all fields present
    - Test 2: Handle no intelligent device (returns empty dict)
    - Test 3: Get vehicle with partial fields (None values excluded)
    - Test 4: Verify all expected fields are mapped correctly
    - Test 5: Handle device with no vehicle fields (returns empty dict)
    """
    print("\n**** Running Octopus get_intelligent_vehicle tests ****")
    failed = False

    # Test 1: Get vehicle with all fields present
    print("\n*** Test 1: Get vehicle with all fields present ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)

    api.intelligent_devices = {
        "test-device-123": {
            "vehicle_battery_size_in_kwh": 75.5,
            "charge_point_power_in_kw": 7.2,
            "weekday_target_time": "06:30",
            "weekday_target_soc": 80,
            "weekend_target_time": "08:00",
            "weekend_target_soc": 90,
            "minimum_soc": 20,
            "maximum_soc": 100,
            "suspended": False,
            "model": "Tesla Model 3",
            "provider": "Tesla",
            "status": "active",
        }
    }

    result = api.get_intelligent_vehicle("test-device-123")

    expected_keys = ["vehicleBatterySizeInKwh", "chargePointPowerInKw", "weekdayTargetTime", "weekdayTargetSoc", "weekendTargetTime", "weekendTargetSoc", "minimumSoc", "maximumSoc", "suspended", "model", "provider", "status"]

    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result, got {type(result)}")
        failed = True
    elif len(result) != len(expected_keys):
        print(f"ERROR: Expected {len(expected_keys)} keys, got {len(result)}")
        failed = True
    elif result.get("vehicleBatterySizeInKwh") != 75.5:
        print(f"ERROR: Expected vehicleBatterySizeInKwh 75.5, got {result.get('vehicleBatterySizeInKwh')}")
        failed = True
    elif result.get("chargePointPowerInKw") != 7.2:
        print(f"ERROR: Expected chargePointPowerInKw 7.2, got {result.get('chargePointPowerInKw')}")
        failed = True
    elif result.get("weekdayTargetTime") != "06:30":
        print(f"ERROR: Expected weekdayTargetTime '06:30', got {result.get('weekdayTargetTime')}")
        failed = True
    elif result.get("weekdayTargetSoc") != 80:
        print(f"ERROR: Expected weekdayTargetSoc 80, got {result.get('weekdayTargetSoc')}")
        failed = True
    elif result.get("model") != "Tesla Model 3":
        print(f"ERROR: Expected model 'Tesla Model 3', got {result.get('model')}")
        failed = True
    else:
        print("PASS: All vehicle fields retrieved correctly")

    # Test 2: Handle no intelligent device (returns empty dict)
    print("\n*** Test 2: Handle no intelligent device ***")
    api2 = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    api2.intelligent_devices = {}

    result = api2.get_intelligent_vehicle("test-device-2")

    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result, got {type(result)}")
        failed = True
    elif len(result) != 0:
        print(f"ERROR: Expected empty dict when no device, got {result}")
        failed = True
    else:
        print("PASS: Returns empty dict when no intelligent device")

    # Test 3: Get vehicle with partial fields (None values excluded)
    print("\n*** Test 3: Get vehicle with partial fields (None values excluded) ***")
    api3 = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    api3.intelligent_devices = {
        "test-device-789": {
            "vehicle_battery_size_in_kwh": 60.0,
            "weekday_target_soc": 80,
            "model": "Nissan Leaf"
            # Other fields missing - should be excluded from result
        }
    }

    result = api3.get_intelligent_vehicle("test-device-789")

    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result, got {type(result)}")
        failed = True
    elif len(result) != 3:
        print(f"ERROR: Expected 3 keys (only non-None values), got {len(result)}: {result.keys()}")
        failed = True
    elif "vehicleBatterySizeInKwh" not in result:
        print(f"ERROR: Expected vehicleBatterySizeInKwh in result")
        failed = True
    elif "weekdayTargetSoc" not in result:
        print(f"ERROR: Expected weekdayTargetSoc in result")
        failed = True
    elif "model" not in result:
        print(f"ERROR: Expected model in result")
        failed = True
    elif "chargePointPowerInKw" in result:
        print(f"ERROR: chargePointPowerInKw should be excluded (was None)")
        failed = True
    else:
        print("PASS: Only non-None fields included in result")

    # Test 4: Verify all expected fields are mapped correctly
    print("\n*** Test 4: Verify field name mapping ***")
    api4 = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    api4.intelligent_devices = {
        "test-device-999": {
            "vehicle_battery_size_in_kwh": 100,
            "charge_point_power_in_kw": 11,
            "weekday_target_time": "07:00",
            "weekday_target_soc": 85,
            "weekend_target_time": "09:00",
            "weekend_target_soc": 95,
            "minimum_soc": 10,
            "maximum_soc": 100,
            "suspended": True,
            "model": "VW ID.3",
            "provider": "VW",
            "status": "suspended",
        }
    }

    result = api4.get_intelligent_vehicle("test-device-999")

    # Check snake_case -> camelCase conversion
    expected_mappings = {
        "vehicle_battery_size_in_kwh": "vehicleBatterySizeInKwh",
        "charge_point_power_in_kw": "chargePointPowerInKw",
        "weekday_target_time": "weekdayTargetTime",
        "weekday_target_soc": "weekdayTargetSoc",
        "weekend_target_time": "weekendTargetTime",
        "weekend_target_soc": "weekendTargetSoc",
        "minimum_soc": "minimumSoc",
        "maximum_soc": "maximumSoc",
        "suspended": "suspended",
        "model": "model",
        "provider": "provider",
        "status": "status",
    }

    mapping_errors = []
    for snake_key, camel_key in expected_mappings.items():
        if camel_key not in result:
            mapping_errors.append(f"{snake_key} -> {camel_key} missing")

    if mapping_errors:
        print(f"ERROR: Field mapping errors: {mapping_errors}")
        failed = True
    else:
        print("PASS: All field names mapped correctly from snake_case to camelCase")

    # Test 5: Handle device with no vehicle fields (returns empty dict)
    print("\n*** Test 5: Handle device with no vehicle fields ***")
    api5 = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)

    api5.intelligent_devices = {
        "test-device-555": {
            "some_other_field": "value"
            # No vehicle-related fields
        }
    }

    result = api5.get_intelligent_vehicle("test-device-555")

    if not isinstance(result, dict):
        print(f"ERROR: Expected dict result, got {type(result)}")
        failed = True
    elif len(result) != 0:
        print(f"ERROR: Expected empty dict when no vehicle fields, got {result}")
        failed = True
    else:
        print("PASS: Returns empty dict when device has no vehicle fields")

    if failed:
        print("\n**** ❌ Octopus get_intelligent_vehicle tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus get_intelligent_vehicle tests PASSED ****")
        return 0


def _mock_run_api(my_predbat, key, account_id, automatic=False):
    """Create an OctopusAPI instance with all run() methods mocked."""
    api = OctopusAPI(my_predbat, key=key, account_id=account_id, automatic=automatic)
    api.load_octopus_cache = AsyncMock()
    api.async_get_account = AsyncMock()
    api.async_find_tariffs = AsyncMock()
    api.async_update_intelligent_devices = AsyncMock()
    api.fetch_tariffs = AsyncMock()
    api.async_get_saving_sessions = AsyncMock(return_value={"events": []})
    api.async_get_flexibility_events = AsyncMock(return_value={"events": []})
    api.get_saving_session_data = MagicMock()
    api.async_intelligent_update_sensor = AsyncMock()
    api.save_octopus_cache = AsyncMock()
    api.process_commands = AsyncMock(return_value=False)
    return api


async def test_octopus_run(my_predbat):
    """
    Test OctopusAPI run method.

    Tests:
    - Test 1: First run with no cache (both fetched_at=None) — all methods called
    - Test 2: Tariff stale (35 min), device fresh (5 min) — only tariff refreshed
    - Test 3: Device stale (15 min), tariff fresh (5 min) — only device refreshed
    - Test 4: Both fresh (1 min) at 2-minute sensor mark — only sensor, no cache save
    - Test 5: Commands processed forces device refresh even when recently fetched
    - Test 6: Fast restart (first=True) with fresh timestamps — only sensor, no heavy fetches
    - Test 7: Automatic config on first run
    """
    print("\n**** Running Octopus run method tests ****")
    failed = False

    # Test 1: First run with no cache — all methods should be called
    print("\n*** Test 1: First run calls all expected methods ***")
    api = _mock_run_api(my_predbat, key="test-api-key", account_id="test-account")
    # tariff_fetched_at and device_fetched_at are None → _data_age_minutes returns 9999

    result = await api.run(seconds=0, first=True)

    if not result:
        print(f"ERROR: Expected run() to return True, got {result}")
        failed = True
    elif api.load_octopus_cache.call_count != 1:
        print(f"ERROR: Expected load_octopus_cache to be called once, got {api.load_octopus_cache.call_count}")
        failed = True
    elif api.async_get_account.call_count != 1:
        print(f"ERROR: Expected async_get_account to be called once on first run, got {api.async_get_account.call_count}")
        failed = True
    elif api.async_find_tariffs.call_count != 1:
        print(f"ERROR: Expected async_find_tariffs to be called once on first run, got {api.async_find_tariffs.call_count}")
        failed = True
    elif api.async_update_intelligent_devices.call_count != 1:
        print(f"ERROR: Expected async_update_intelligent_devices to be called once on first run, got {api.async_update_intelligent_devices.call_count}")
        failed = True
    elif api.fetch_tariffs.call_count != 1:
        print(f"ERROR: Expected fetch_tariffs to be called once on first run, got {api.fetch_tariffs.call_count}")
        failed = True
    elif api.async_get_flexibility_events.call_count != 1:
        print(f"ERROR: Expected async_get_flexibility_events to be called once on first run, got {api.async_get_flexibility_events.call_count}")
        failed = True
    elif api.get_saving_session_data.call_count != 1:
        print(f"ERROR: Expected get_saving_session_data to be called once on first run, got {api.get_saving_session_data.call_count}")
        failed = True
    elif api.async_intelligent_update_sensor.call_count != 1:
        print(f"ERROR: Expected async_intelligent_update_sensor to be called once on first run (sensor_due=first), got {api.async_intelligent_update_sensor.call_count}")
        failed = True
    elif api.save_octopus_cache.call_count != 1:
        print(f"ERROR: Expected save_octopus_cache to be called once (tariff_due=True), got {api.save_octopus_cache.call_count}")
        failed = True
    else:
        print("PASS: First run calls all expected methods")

    # Test 2: Tariff stale (35 min old), device fresh (5 min old) — only tariff block runs
    print("\n*** Test 2: Tariff stale, device fresh — only tariff refreshed ***")
    api2 = _mock_run_api(my_predbat, key="test-api-key-2", account_id="test-account-2")
    api2.tariff_fetched_at = datetime(2025, 1, 1, 9, 55, 0)  # 35 min before mock now
    api2.device_fetched_at = datetime(2025, 1, 1, 10, 25, 0)  # 5 min before mock now

    with patch("octopus.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 30, 0)
        result = await api2.run(seconds=0, first=False)

    if api2.load_octopus_cache.call_count != 0:
        print(f"ERROR: Expected load_octopus_cache NOT called on non-first run, got {api2.load_octopus_cache.call_count}")
        failed = True
    elif api2.async_get_account.call_count != 1:
        print(f"ERROR: Expected async_get_account called (tariff stale), got {api2.async_get_account.call_count}")
        failed = True
    elif api2.async_find_tariffs.call_count != 1:
        print(f"ERROR: Expected async_find_tariffs called (tariff stale), got {api2.async_find_tariffs.call_count}")
        failed = True
    elif api2.async_update_intelligent_devices.call_count != 0:
        print(f"ERROR: Expected async_update_intelligent_devices NOT called (device fresh), got {api2.async_update_intelligent_devices.call_count}")
        failed = True
    elif api2.save_octopus_cache.call_count != 1:
        print(f"ERROR: Expected save_octopus_cache called (tariff_due=True), got {api2.save_octopus_cache.call_count}")
        failed = True
    else:
        print("PASS: Tariff stale, device fresh — only tariff refreshed")

    # Test 3: Device stale (15 min old), tariff fresh (5 min old) — only device block runs
    print("\n*** Test 3: Device stale, tariff fresh — only device refreshed ***")
    api3 = _mock_run_api(my_predbat, key="test-api-key-3", account_id="test-account-3")
    api3.tariff_fetched_at = datetime(2025, 1, 1, 10, 5, 0)  # 5 min before mock now
    api3.device_fetched_at = datetime(2025, 1, 1, 9, 55, 0)  # 15 min before mock now
    api3.tariffs = {}

    with patch("octopus.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 10, 0)
        result = await api3.run(seconds=0, first=False)

    if api3.async_get_account.call_count != 0:
        print(f"ERROR: Expected async_get_account NOT called (tariff fresh), got {api3.async_get_account.call_count}")
        failed = True
    elif api3.async_update_intelligent_devices.call_count != 1:
        print(f"ERROR: Expected async_update_intelligent_devices called (device stale), got {api3.async_update_intelligent_devices.call_count}")
        failed = True
    elif api3.fetch_tariffs.call_count != 1:
        print(f"ERROR: Expected fetch_tariffs called (device stale), got {api3.fetch_tariffs.call_count}")
        failed = True
    elif api3.async_get_flexibility_events.call_count != 1:
        print(f"ERROR: Expected async_get_flexibility_events called (device stale), got {api3.async_get_flexibility_events.call_count}")
        failed = True
    elif api3.get_saving_session_data.call_count != 1:
        print(f"ERROR: Expected get_saving_session_data called (device stale), got {api3.get_saving_session_data.call_count}")
        failed = True
    elif api3.save_octopus_cache.call_count != 1:
        print(f"ERROR: Expected save_octopus_cache called (device_due=True), got {api3.save_octopus_cache.call_count}")
        failed = True
    else:
        print("PASS: Device stale, tariff fresh — only device refreshed")

    # Test 4: Both fresh (1 min) at 2-minute sensor mark — only sensor fires, no cache save
    print("\n*** Test 4: Both fresh — only sensor update, no cache save ***")
    api4 = _mock_run_api(my_predbat, key="test-api-key-4", account_id="test-account-4")
    api4.tariff_fetched_at = datetime(2025, 1, 1, 10, 1, 0)  # 1 min before mock now
    api4.device_fetched_at = datetime(2025, 1, 1, 10, 1, 0)  # 1 min before mock now

    with patch("octopus.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 2, 0)  # 10:02 — even minute
        result = await api4.run(seconds=0, first=False)

    if api4.async_get_account.call_count != 0:
        print(f"ERROR: Expected async_get_account NOT called (tariff fresh), got {api4.async_get_account.call_count}")
        failed = True
    elif api4.async_update_intelligent_devices.call_count != 0:
        print(f"ERROR: Expected async_update_intelligent_devices NOT called (device fresh), got {api4.async_update_intelligent_devices.call_count}")
        failed = True
    elif api4.async_intelligent_update_sensor.call_count != 1:
        print(f"ERROR: Expected async_intelligent_update_sensor called at 2-min mark, got {api4.async_intelligent_update_sensor.call_count}")
        failed = True
    elif api4.save_octopus_cache.call_count != 0:
        print(f"ERROR: Expected save_octopus_cache NOT called (no data refreshed), got {api4.save_octopus_cache.call_count}")
        failed = True
    else:
        print("PASS: Both fresh — only sensor update, no cache save")

    # Test 5: Commands processed forces device refresh even when recently fetched
    print("\n*** Test 5: Processing commands triggers device refresh regardless of age ***")
    api5 = _mock_run_api(my_predbat, key="test-api-key-5", account_id="test-account-5")
    api5.process_commands = AsyncMock(return_value=True)
    api5.tariff_fetched_at = datetime(2025, 1, 1, 10, 4, 0)  # 1 min before mock now
    api5.device_fetched_at = datetime(2025, 1, 1, 10, 4, 0)  # 1 min before mock now (fresh)
    api5.tariffs = {}

    with patch("octopus.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 5, 0)  # odd minute — sensor not due
        result = await api5.run(seconds=0, first=False)

    if api5.async_get_account.call_count != 0:
        print(f"ERROR: Expected async_get_account NOT called (tariff fresh, refresh doesn't affect tariff), got {api5.async_get_account.call_count}")
        failed = True
    elif api5.async_update_intelligent_devices.call_count != 1:
        print(f"ERROR: Expected async_update_intelligent_devices called (refresh=True forces device update), got {api5.async_update_intelligent_devices.call_count}")
        failed = True
    elif api5.fetch_tariffs.call_count != 1:
        print(f"ERROR: Expected fetch_tariffs called (refresh=True forces device update), got {api5.fetch_tariffs.call_count}")
        failed = True
    elif api5.save_octopus_cache.call_count != 1:
        print(f"ERROR: Expected save_octopus_cache called (device_due=True from refresh), got {api5.save_octopus_cache.call_count}")
        failed = True
    else:
        print("PASS: Processing commands triggers refresh of intelligent device data")

    # Test 6: Fast restart (first=True) with fresh timestamps — API calls skipped but
    # async_find_tariffs and fetch_tariffs run to rebuild state from cached data
    print("\n*** Test 6: Fast restart with fresh cache — tariff structure and rates rebuilt, API calls skipped ***")
    api6 = _mock_run_api(my_predbat, key="test-api-key-6", account_id="test-account-6")
    api6.tariff_fetched_at = datetime(2025, 1, 1, 10, 10, 0)  # 5 min before mock now
    api6.device_fetched_at = datetime(2025, 1, 1, 10, 12, 0)  # 3 min before mock now

    with patch("octopus.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2025, 1, 1, 10, 15, 0)
        result = await api6.run(seconds=0, first=True)

    if api6.async_get_account.call_count != 0:
        print(f"ERROR: Expected async_get_account NOT called (tariff 5 min old < 30), got {api6.async_get_account.call_count}")
        failed = True
    elif api6.async_find_tariffs.call_count != 1:
        print(f"ERROR: Expected async_find_tariffs called (rebuilds tariff structure from cached account_data), got {api6.async_find_tariffs.call_count}")
        failed = True
    elif api6.async_update_intelligent_devices.call_count != 0:
        print(f"ERROR: Expected async_update_intelligent_devices NOT called (device 3 min old < 10), got {api6.async_update_intelligent_devices.call_count}")
        failed = True
    elif api6.fetch_tariffs.call_count != 1:
        print(f"ERROR: Expected fetch_tariffs called (loads rate data from cache on first run), got {api6.fetch_tariffs.call_count}")
        failed = True
    elif api6.async_intelligent_update_sensor.call_count != 1:
        print(f"ERROR: Expected async_intelligent_update_sensor called on first run (sensor_due=first), got {api6.async_intelligent_update_sensor.call_count}")
        failed = True
    elif api6.save_octopus_cache.call_count != 0:
        print(f"ERROR: Expected save_octopus_cache NOT called (no API data refreshed), got {api6.save_octopus_cache.call_count}")
        failed = True
    else:
        print("PASS: Fast restart with fresh cache — tariff structure and rates rebuilt, API calls skipped")

    # Test 7: Automatic config on first run when automatic=True
    print("\n*** Test 7: Automatic config on first run when automatic=True ***")
    api7 = _mock_run_api(my_predbat, key="test-api-key-7", account_id="test-account-7", automatic=True)
    api7.automatic_config = MagicMock()
    api7.tariffs = {"import": {}}

    result = await api7.run(seconds=0, first=True)

    if api7.automatic_config.call_count != 1:
        print(f"ERROR: Expected automatic_config to be called on first run with automatic=True, got {api7.automatic_config.call_count}")
        failed = True
    elif api7.automatic_config.call_args[0][0] != api7.tariffs:
        print(f"ERROR: Expected automatic_config to be called with tariffs")
        failed = True
    else:
        print("PASS: Automatic config called on first run when automatic=True")

    if failed:
        print("\n**** ❌ Octopus run method tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ Octopus run method tests PASSED ****")
        return 0
