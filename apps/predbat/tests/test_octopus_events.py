"""
Tests for Octopus event handler functions
"""

import asyncio
from unittest.mock import AsyncMock
from octopus import OctopusAPI


def test_octopus_events_wrapper(my_predbat):
    return asyncio.run(test_octopus_events(my_predbat))


async def test_octopus_events(my_predbat):
    """
    Test OctopusAPI event handlers queue correct commands.

    Tests:
    - Test 1: select_event for intelligent_target_time queues correct command
    - Test 2: select_event for saving_session_join queues correct command
    - Test 3: select_event for unknown entity does nothing
    - Test 4: number_event for intelligent_target_soc queues correct command with valid integer
    - Test 5: number_event for intelligent_target_soc handles invalid value gracefully
    - Test 6: number_event for unknown entity does nothing
    - Test 7: switch_event does nothing (not implemented)
    - Test 8: Multiple events queue multiple commands in order
    - Test 9: process_commands with set_intelligent_target_percentage
    - Test 10: process_commands with set_intelligent_target_time
    - Test 11: process_commands with join_saving_session_event
    - Test 12: process_commands with multiple commands
    - Test 13: process_commands with empty command queue
    - Test 14: process_commands clears command queue after processing
    """
    print("**** Running Octopus event handler tests ****")
    failed = False

    # Test 1: select_event for intelligent_target_time
    print("\n*** Test 1: select_event for intelligent_target_time ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.commands = []

    entity_id = api.get_entity_name("select", "intelligent_target_time")
    await api.select_event(entity_id, "06:30")

    if len(api.commands) != 1:
        print(f"ERROR: Expected 1 command, got {len(api.commands)}")
        failed = True
    elif api.commands[0] != {"command": "set_intelligent_target_time", "value": "06:30"}:
        print(f"ERROR: Expected set_intelligent_target_time command, got {api.commands[0]}")
        failed = True
    else:
        print("PASS: intelligent_target_time command queued correctly")

    # Test 2: select_event for saving_session_join
    print("\n*** Test 2: select_event for saving_session_join ***")
    api.commands = []

    entity_id = api.get_entity_name("select", "saving_session_join")
    await api.select_event(entity_id, "event-code-123")

    if len(api.commands) != 1:
        print(f"ERROR: Expected 1 command, got {len(api.commands)}")
        failed = True
    elif api.commands[0] != {"command": "join_saving_session_event", "event_code": "event-code-123"}:
        print(f"ERROR: Expected join_saving_session_event command, got {api.commands[0]}")
        failed = True
    else:
        print("PASS: saving_session_join command queued correctly")

    # Test 3: select_event for unknown entity
    print("\n*** Test 3: select_event for unknown entity ***")
    api.commands = []

    await api.select_event("select.unknown_entity", "some_value")

    if len(api.commands) != 0:
        print(f"ERROR: Expected 0 commands for unknown entity, got {len(api.commands)}")
        failed = True
    else:
        print("PASS: Unknown entity ignored correctly")

    # Test 4: number_event for intelligent_target_soc with valid value
    print("\n*** Test 4: number_event for intelligent_target_soc with valid value ***")
    api.commands = []

    entity_id = api.get_entity_name("number", "intelligent_target_soc")
    await api.number_event(entity_id, 80)

    if len(api.commands) != 1:
        print(f"ERROR: Expected 1 command, got {len(api.commands)}")
        failed = True
    elif api.commands[0] != {"command": "set_intelligent_target_percentage", "value": 80}:
        print(f"ERROR: Expected set_intelligent_target_percentage command with value 80, got {api.commands[0]}")
        failed = True
    else:
        print("PASS: intelligent_target_soc command queued correctly with integer value")

    # Test 5: number_event with string that converts to int
    print("\n*** Test 5: number_event with string value ***")
    api.commands = []

    entity_id = api.get_entity_name("number", "intelligent_target_soc")
    await api.number_event(entity_id, "75")

    if len(api.commands) != 1:
        print(f"ERROR: Expected 1 command, got {len(api.commands)}")
        failed = True
    elif api.commands[0] != {"command": "set_intelligent_target_percentage", "value": 75}:
        print(f"ERROR: Expected set_intelligent_target_percentage command with value 75, got {api.commands[0]}")
        failed = True
    else:
        print("PASS: intelligent_target_soc command queued correctly with string value converted to int")

    # Test 6: number_event with invalid value
    print("\n*** Test 6: number_event with invalid value ***")
    api.commands = []

    entity_id = api.get_entity_name("number", "intelligent_target_soc")
    await api.number_event(entity_id, "invalid")

    if len(api.commands) != 0:
        print(f"ERROR: Expected 0 commands for invalid value, got {len(api.commands)}")
        failed = True
    else:
        print("PASS: Invalid value handled gracefully, no command queued")

    # Test 7: number_event for unknown entity
    print("\n*** Test 7: number_event for unknown entity ***")
    api.commands = []

    await api.number_event("number.unknown_entity", 50)

    if len(api.commands) != 0:
        print(f"ERROR: Expected 0 commands for unknown entity, got {len(api.commands)}")
        failed = True
    else:
        print("PASS: Unknown number entity ignored correctly")

    # Test 8: switch_event does nothing
    print("\n*** Test 8: switch_event does nothing ***")
    api.commands = []

    await api.switch_event("switch.some_entity", "turn_on")

    if len(api.commands) != 0:
        print(f"ERROR: Expected 0 commands from switch_event, got {len(api.commands)}")
        failed = True
    else:
        print("PASS: switch_event does nothing as expected")

    # Test 9: Multiple events queue multiple commands in order
    print("\n*** Test 9: Multiple events queue multiple commands in order ***")
    api.commands = []

    # Queue multiple commands
    await api.select_event(api.get_entity_name("select", "intelligent_target_time"), "05:30")
    await api.number_event(api.get_entity_name("number", "intelligent_target_soc"), 90)
    await api.select_event(api.get_entity_name("select", "saving_session_join"), "event-xyz")

    expected_commands = [{"command": "set_intelligent_target_time", "value": "05:30"}, {"command": "set_intelligent_target_percentage", "value": 90}, {"command": "join_saving_session_event", "event_code": "event-xyz"}]

    if len(api.commands) != 3:
        print(f"ERROR: Expected 3 commands, got {len(api.commands)}")
        failed = True
    elif api.commands != expected_commands:
        print(f"ERROR: Commands don't match expected order")
        print(f"  Expected: {expected_commands}")
        print(f"  Got: {api.commands}")
        failed = True
    else:
        print("PASS: Multiple commands queued in correct order")

    # Test 10: process_commands with set_intelligent_target_percentage
    print("\n*** Test 10: process_commands with set_intelligent_target_percentage ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account-123", automatic=False)
    api.commands = [{"command": "set_intelligent_target_percentage", "value": 85}]

    # Mock the async method that gets called
    mock_set_schedule = AsyncMock()
    api.async_set_intelligent_target_schedule = mock_set_schedule

    result = await api.process_commands("test-account-123")

    if not result:
        print("ERROR: process_commands should return True when commands are processed")
        failed = True
    elif len(api.commands) != 0:
        print(f"ERROR: Command queue should be empty after processing, got {len(api.commands)} commands")
        failed = True
    elif mock_set_schedule.call_count != 1:
        print(f"ERROR: async_set_intelligent_target_schedule should be called once, was called {mock_set_schedule.call_count} times")
        failed = True
    elif mock_set_schedule.call_args[0][0] != "test-account-123":
        print(f"ERROR: Wrong account_id passed: {mock_set_schedule.call_args[0][0]}")
        failed = True
    elif mock_set_schedule.call_args[1].get("target_percentage") != 85:
        print(f"ERROR: Wrong target_percentage passed: {mock_set_schedule.call_args[1].get('target_percentage')}")
        failed = True
    else:
        print("PASS: set_intelligent_target_percentage command processed correctly")

    # Test 11: process_commands with set_intelligent_target_time
    print("\n*** Test 11: process_commands with set_intelligent_target_time ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account-456", automatic=False)
    api.commands = [{"command": "set_intelligent_target_time", "value": "06:00"}]

    mock_set_schedule = AsyncMock()
    api.async_set_intelligent_target_schedule = mock_set_schedule

    result = await api.process_commands("test-account-456")

    if not result:
        print("ERROR: process_commands should return True when commands are processed")
        failed = True
    elif len(api.commands) != 0:
        print(f"ERROR: Command queue should be empty after processing, got {len(api.commands)} commands")
        failed = True
    elif mock_set_schedule.call_count != 1:
        print(f"ERROR: async_set_intelligent_target_schedule should be called once, was called {mock_set_schedule.call_count} times")
        failed = True
    elif mock_set_schedule.call_args[1].get("target_time") != "06:00":
        print(f"ERROR: Wrong target_time passed: {mock_set_schedule.call_args[1].get('target_time')}")
        failed = True
    else:
        print("PASS: set_intelligent_target_time command processed correctly")

    # Test 12: process_commands with join_saving_session_event
    print("\n*** Test 12: process_commands with join_saving_session_event ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account-789", automatic=False)
    api.commands = [{"command": "join_saving_session_event", "event_code": "EVENT-2024-001"}]

    mock_join_event = AsyncMock()
    api.async_join_saving_session_events = mock_join_event

    result = await api.process_commands("test-account-789")

    if not result:
        print("ERROR: process_commands should return True when commands are processed")
        failed = True
    elif len(api.commands) != 0:
        print(f"ERROR: Command queue should be empty after processing, got {len(api.commands)} commands")
        failed = True
    elif mock_join_event.call_count != 1:
        print(f"ERROR: async_join_saving_session_events should be called once, was called {mock_join_event.call_count} times")
        failed = True
    elif mock_join_event.call_args[0] != ("test-account-789", "EVENT-2024-001"):
        print(f"ERROR: Wrong arguments passed: {mock_join_event.call_args[0]}")
        failed = True
    else:
        print("PASS: join_saving_session_event command processed correctly")

    # Test 13: process_commands with multiple commands
    print("\n*** Test 13: process_commands with multiple commands ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account-multi", automatic=False)
    api.commands = [{"command": "set_intelligent_target_percentage", "value": 90}, {"command": "set_intelligent_target_time", "value": "07:30"}, {"command": "join_saving_session_event", "event_code": "EVENT-2024-002"}]

    mock_set_schedule = AsyncMock()
    mock_join_event = AsyncMock()
    api.async_set_intelligent_target_schedule = mock_set_schedule
    api.async_join_saving_session_events = mock_join_event

    result = await api.process_commands("test-account-multi")

    if not result:
        print("ERROR: process_commands should return True when commands are processed")
        failed = True
    elif len(api.commands) != 0:
        print(f"ERROR: Command queue should be empty after processing, got {len(api.commands)} commands")
        failed = True
    elif mock_set_schedule.call_count != 2:
        print(f"ERROR: async_set_intelligent_target_schedule should be called twice, was called {mock_set_schedule.call_count} times")
        failed = True
    elif mock_join_event.call_count != 1:
        print(f"ERROR: async_join_saving_session_events should be called once, was called {mock_join_event.call_count} times")
        failed = True
    else:
        print("PASS: Multiple commands processed correctly")

    # Test 14: process_commands with empty command queue
    print("\n*** Test 14: process_commands with empty command queue ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account-empty", automatic=False)
    api.commands = []

    mock_set_schedule = AsyncMock()
    mock_join_event = AsyncMock()
    api.async_set_intelligent_target_schedule = mock_set_schedule
    api.async_join_saving_session_events = mock_join_event

    result = await api.process_commands("test-account-empty")

    if result:
        print("ERROR: process_commands should return False when no commands are processed")
        failed = True
    elif mock_set_schedule.call_count != 0:
        print(f"ERROR: async_set_intelligent_target_schedule should not be called, was called {mock_set_schedule.call_count} times")
        failed = True
    elif mock_join_event.call_count != 0:
        print(f"ERROR: async_join_saving_session_events should not be called, was called {mock_join_event.call_count} times")
        failed = True
    else:
        print("PASS: Empty command queue handled correctly")

    # Test 15: process_commands clears queue even with unknown commands
    print("\n*** Test 15: process_commands clears queue even with unknown commands ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account-unknown", automatic=False)
    api.commands = [{"command": "unknown_command", "value": "test"}, {"command": "set_intelligent_target_percentage", "value": 75}]

    mock_set_schedule = AsyncMock()
    api.async_set_intelligent_target_schedule = mock_set_schedule

    result = await api.process_commands("test-account-unknown")

    if not result:
        print("ERROR: process_commands should return True when at least one known command is processed")
        failed = True
    elif len(api.commands) != 0:
        print(f"ERROR: Command queue should be empty after processing, got {len(api.commands)} commands")
        failed = True
    elif mock_set_schedule.call_count != 1:
        print(f"ERROR: async_set_intelligent_target_schedule should be called once, was called {mock_set_schedule.call_count} times")
        failed = True
    else:
        print("PASS: Queue cleared correctly with mix of known and unknown commands")

    # Summary
    if failed:
        print("\n**** Octopus event handler tests FAILED ****")
        raise Exception("Octopus event handler tests failed")
    else:
        print("\n**** All Octopus event handler tests PASSED ****")

    return failed
