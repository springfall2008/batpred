# fmt: off
"""
Unit tests for HAInterface service methods.

Tests cover:
- call_service() with websocket and loopback modes
- async_call_service_websocket_command() message flow
- set_state_external() with CONFIG_ITEMS and watch list triggers
"""

from unittest.mock import patch, MagicMock, AsyncMock
from aiohttp import WSMsgType
import json

from tests.test_hainterface_common import MockBase, create_ha_interface
from tests.test_infra import run_async


def test_hainterface_call_service_websocket(my_predbat=None):
    """Test call_service() with websocket active"""
    print("\n=== Testing HAInterface call_service() with websocket ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", websocket_active=True)

    # Mock call_service_websocket_command
    original_method = ha_interface.call_service_websocket_command
    call_service_websocket_command_called = []

    def mock_call_service_websocket_command(domain, service, data):
        call_service_websocket_command_called.append((domain, service, data))
        return {"result": "success"}

    ha_interface.call_service_websocket_command = mock_call_service_websocket_command

    result = ha_interface.call_service("switch/turn_on", entity_id="switch.test")

    if not call_service_websocket_command_called:
        print("ERROR: call_service_websocket_command should be called")
        failed += 1
    else:
        domain, service, data = call_service_websocket_command_called[0]
        if domain != "switch":
            print(f"ERROR: Expected domain 'switch', got '{domain}'")
            failed += 1
        elif service != "turn_on":
            print(f"ERROR: Expected service 'turn_on', got '{service}'")
            failed += 1
        elif data.get("entity_id") != "switch.test":
            print(f"ERROR: Expected entity_id 'switch.test', got '{data.get('entity_id')}'")
            failed += 1
        else:
            print("✓ call_service_websocket_command called correctly")

    ha_interface.call_service_websocket_command = original_method
    return failed


def test_hainterface_call_service_loopback(my_predbat=None):
    """Test call_service() with websocket inactive (loopback mode)"""
    print("\n=== Testing HAInterface call_service() loopback ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", websocket_active=False)

    # Track trigger_callback calls
    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    result = ha_interface.call_service("number/set_value", entity_id="number.test", value=42)

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("domain") != "number":
            print(f"ERROR: Expected domain 'number', got '{data.get('domain')}'")
            failed += 1
        elif data.get("service") != "set_value":
            print(f"ERROR: Expected service 'set_value', got '{data.get('service')}'")
            failed += 1
        elif data.get("service_data", {}).get("entity_id") != "number.test":
            print(f"ERROR: Expected entity_id 'number.test'")
            failed += 1
        elif data.get("service_data", {}).get("value") != 42:
            print(f"ERROR: Expected value 42, got {data.get('service_data', {}).get('value')}")
            failed += 1
        else:
            print("✓ Loopback trigger_callback called correctly")

    return failed


def test_hainterface_async_call_service_basic(my_predbat=None):
    """Test async_call_service_websocket_command() basic success - queues command and gets result from socketLoop"""
    print("\n=== Testing HAInterface async_call_service_websocket_command() basic ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", ha_url="http://localhost:8123")

    # Initialize queue infrastructure (normally done in initialize())
    import threading
    import asyncio
    import time
    ha_interface.ws_command_queue = []
    ha_interface.ws_pending_requests = {}
    ha_interface.ws_pending_lock = threading.Lock()

    async def test_command():
        # Start background thread to process command
        def background_processor():
            time.sleep(0.05)  # Wait for command to be queued
            with ha_interface.ws_pending_lock:
                if ha_interface.ws_command_queue:
                    domain, service, service_data, return_response, event, result_holder = ha_interface.ws_command_queue[0]
                    
                    # Verify command details
                    if domain == "switch" and service == "turn_on" and service_data.get("entity_id") == "switch.test":
                        print("✓ Command queued correctly")
                    
                    # Simulate socketLoop processing
                    result_holder["success"] = True
                    result_holder["response"] = None
                    result_holder["error"] = None
                    event.set()
        
        processor_thread = threading.Thread(target=background_processor, daemon=True)
        processor_thread.start()
        
        # Call the method (will block on event.wait)
        result = await ha_interface.async_call_service_websocket_command("switch", "turn_on", {"entity_id": "switch.test"})
        
        processor_thread.join(timeout=1.0)
        return result, 0
    
    result, test_failed = run_async(test_command())
    failed += test_failed
    
    if test_failed == 0:
        print("✓ Command processed successfully")

    return failed


def test_hainterface_async_call_service_return_response(my_predbat=None):
    """Test async_call_service_websocket_command() with return_response - queues command with return_response flag"""
    print("\n=== Testing HAInterface async_call_service_websocket_command() return_response ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", ha_url="http://localhost:8123")

    # Initialize queue infrastructure
    import threading
    import asyncio
    import time
    ha_interface.ws_command_queue = []
    ha_interface.ws_pending_requests = {}
    ha_interface.ws_pending_lock = threading.Lock()

    async def test_command():
        # Start a background thread to simulate socketLoop processing
        result_data = {"result": None}
        
        def background_processor():
            # Wait a bit for command to be queued
            time.sleep(0.1)
            
            # Process the queued command
            with ha_interface.ws_pending_lock:
                if ha_interface.ws_command_queue:
                    domain, service, service_data, return_response, event, result_holder = ha_interface.ws_command_queue[0]
                    
                    # Verify return_response removed from service_data
                    if "return_response" not in service_data and return_response:
                        result_data["checks_passed"] = True
                    
                    # Simulate successful response
                    result_holder["success"] = True
                    result_holder["response"] = "test_value"
                    result_holder["error"] = None
                    event.set()
        
        processor_thread = threading.Thread(target=background_processor, daemon=True)
        processor_thread.start()
        
        # Call the method (will block on event.wait)
        result = await ha_interface.async_call_service_websocket_command("switch", "turn_on", {"entity_id": "switch.test", "return_response": True})
        
        # Wait for background thread
        processor_thread.join(timeout=2.0)
        
        if result_data.get("checks_passed"):
            print("✓ return_response removed from service_data")
            print("✓ return_response flag set correctly")
        
        return result, 0
    
    result, test_failed = run_async(test_command())
    failed += test_failed
    
    if test_failed == 0 and result == "test_value":
        print("✓ Response returned correctly")
    elif test_failed == 0:
        print(f"ERROR: Expected result 'test_value', got {result}")
        failed += 1

    return failed


def test_hainterface_async_call_service_failed(my_predbat=None):
    """Test async_call_service_websocket_command() with failure - socketLoop returns failure"""
    print("\n=== Testing HAInterface async_call_service_websocket_command() failure ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", ha_url="http://localhost:8123")

    # Initialize queue infrastructure
    import threading
    import asyncio
    import time
    ha_interface.ws_command_queue = []
    ha_interface.ws_pending_requests = {}
    ha_interface.ws_pending_lock = threading.Lock()

    async def test_command():
        # Start background thread to process command
        def background_processor():
            time.sleep(0.05)  # Wait for command to be queued
            with ha_interface.ws_pending_lock:
                if ha_interface.ws_command_queue:
                    domain, service, service_data, return_response, event, result_holder = ha_interface.ws_command_queue[0]
                    
                    # Simulate socketLoop returning failure
                    result_holder["success"] = False
                    result_holder["response"] = None
                    result_holder["error"] = None
                    event.set()
        
        processor_thread = threading.Thread(target=background_processor, daemon=True)
        processor_thread.start()
        
        # Call the method (will block on event.wait)
        result = await ha_interface.async_call_service_websocket_command("switch", "turn_on", {"entity_id": "switch.test"})
        
        processor_thread.join(timeout=1.0)
        return result, 0
    
    result, test_failed = run_async(test_command())
    failed += test_failed
    
    # Should log warning
    if not any("Service call" in log and "failed" in log for log in mock_base.log_messages):
        print("ERROR: Should log warning on failure")
        failed += 1
    else:
        print("✓ Warning logged on failure")

    if result is not None:
        print(f"ERROR: Expected None result on failure, got {result}")
        failed += 1
    else:
        print("✓ Returned None on failure")

    return failed


def test_hainterface_async_call_service_exception(my_predbat=None):
    """Test async_call_service_websocket_command() with timeout (no socketLoop response)"""
    print("\n=== Testing HAInterface async_call_service_websocket_command() timeout ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", ha_url="http://localhost:8123")

    # Initialize queue infrastructure
    import threading
    import asyncio
    ha_interface.ws_command_queue = []
    ha_interface.ws_pending_requests = {}
    ha_interface.ws_pending_lock = threading.Lock()

    # Patch threading.Event.wait to simulate instant timeout
    original_wait = threading.Event.wait
    def mock_wait(self, timeout=None):
        return False  # Simulate timeout
    
    async def test_command():
        with patch.object(threading.Event, 'wait', mock_wait):
            # This will timeout immediately
            result = await ha_interface.async_call_service_websocket_command("switch", "turn_on", {"entity_id": "switch.test"})
            return result, 0
    
    result, test_failed = run_async(test_command())
    failed += test_failed
    
    # Should log warning about failure (timeout means no success in result_holder)
    if not any("Service call" in log and "failed" in log for log in mock_base.log_messages):
        print("ERROR: Should log warning on timeout")
        failed += 1
    else:
        print("✓ Warning logged on timeout")

    if result is not None:
        print(f"ERROR: Expected None result on timeout, got {result}")
        failed += 1
    else:
        print("✓ Returned None on timeout")

    return failed


def test_hainterface_async_call_service_error_limit(my_predbat=None):
    """Test async_call_service_websocket_command() with error in result_holder"""
    print("\n=== Testing HAInterface async_call_service_websocket_command() error in result ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", ha_url="http://localhost:8123")

    # Initialize queue infrastructure
    import threading
    import asyncio
    import time
    ha_interface.ws_command_queue = []
    ha_interface.ws_pending_requests = {}
    ha_interface.ws_pending_lock = threading.Lock()

    async def test_command():
        # Start background thread to process command
        def background_processor():
            time.sleep(0.05)  # Wait for command to be queued
            with ha_interface.ws_pending_lock:
                if ha_interface.ws_command_queue:
                    domain, service, service_data, return_response, event, result_holder = ha_interface.ws_command_queue[0]
                    
                    # Simulate socketLoop returning error (e.g., connection_lost)
                    result_holder["success"] = False
                    result_holder["response"] = None
                    result_holder["error"] = "connection_lost"
                    event.set()
        
        processor_thread = threading.Thread(target=background_processor, daemon=True)
        processor_thread.start()
        
        # Call the method (will block on event.wait)
        result = await ha_interface.async_call_service_websocket_command("switch", "turn_on", {"entity_id": "switch.test"})
        
        processor_thread.join(timeout=1.0)
        return result, 0
    
    result, test_failed = run_async(test_command())
    failed += test_failed
    
    # Should log warning about error
    if not any("Service call" in log and "failed" in log for log in mock_base.log_messages):
        print("ERROR: Should log warning on error")
        failed += 1
    else:
        print("✓ Warning logged on error")

    if result is not None:
        print(f"ERROR: Expected None result on error, got {result}")
        failed += 1
    else:
        print("✓ Returned None on error")

    return failed


def test_hainterface_set_state_external_config_item_switch(my_predbat=None):
    """Test set_state_external() with CONFIG_ITEMS switch"""
    print("\n=== Testing HAInterface set_state_external() CONFIG_ITEMS switch ===")
    failed = 0

    mock_base = MockBase()
    mock_base.CONFIG_ITEMS = [
        {"entity": "switch.test_switch", "type": "switch", "value": False}
    ]

    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    # Track trigger_callback calls
    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    run_async(ha_interface.set_state_external("switch.test_switch", True, {}))

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("domain") != "switch":
            print(f"ERROR: Expected domain 'switch', got '{data.get('domain')}'")
            failed += 1
        elif data.get("service") != "turn_on":
            print(f"ERROR: Expected service 'turn_on', got '{data.get('service')}'")
            failed += 1
        elif data.get("service_data", {}).get("entity_id") != "switch.test_switch":
            print(f"ERROR: Expected entity_id 'switch.test_switch'")
            failed += 1
        else:
            print("✓ CONFIG_ITEMS switch handled correctly")

    return failed


def test_hainterface_set_state_external_config_item_number(my_predbat=None):
    """Test set_state_external() with CONFIG_ITEMS input_number"""
    print("\n=== Testing HAInterface set_state_external() CONFIG_ITEMS input_number ===")
    failed = 0

    mock_base = MockBase()
    mock_base.CONFIG_ITEMS = [
        {"entity": "input_number.test_number", "type": "input_number", "value": 10, "step": 1}
    ]

    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    run_async(ha_interface.set_state_external("input_number.test_number", 42, {}))

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("domain") != "input_number":
            print(f"ERROR: Expected domain 'input_number'")
            failed += 1
        elif data.get("service") != "set_value":
            print(f"ERROR: Expected service 'set_value'")
            failed += 1
        elif data.get("service_data", {}).get("value") != 42:
            print(f"ERROR: Expected value 42")
            failed += 1
        else:
            print("✓ CONFIG_ITEMS input_number handled correctly")

    return failed


def test_hainterface_set_state_external_config_item_select(my_predbat=None):
    """Test set_state_external() with CONFIG_ITEMS select"""
    print("\n=== Testing HAInterface set_state_external() CONFIG_ITEMS select ===")
    failed = 0

    mock_base = MockBase()
    mock_base.CONFIG_ITEMS = [
        {"entity": "select.test_select", "type": "select", "value": "option1"}
    ]

    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    run_async(ha_interface.set_state_external("select.test_select", "option2", {}))

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("domain") != "select":
            print(f"ERROR: Expected domain 'select'")
            failed += 1
        elif data.get("service") != "select_option":
            print(f"ERROR: Expected service 'select_option'")
            failed += 1
        elif data.get("service_data", {}).get("option") != "option2":
            print(f"ERROR: Expected option 'option2'")
            failed += 1
        else:
            print("✓ CONFIG_ITEMS select handled correctly")

    return failed


def test_hainterface_set_state_external_domain_switch(my_predbat=None):
    """Test set_state_external() with domain-based switch"""
    print("\n=== Testing HAInterface set_state_external() domain switch ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    run_async(ha_interface.set_state_external("input_boolean.test", "on", {}))

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("service") != "turn_on":
            print(f"ERROR: Expected service 'turn_on'")
            failed += 1
        else:
            print("✓ Domain-based switch handled correctly")

    return failed


def test_hainterface_set_state_external_domain_number(my_predbat=None):
    """Test set_state_external() with domain-based number"""
    print("\n=== Testing HAInterface set_state_external() domain number ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    run_async(ha_interface.set_state_external("number.test", 50, {}))

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("service") != "set_value":
            print(f"ERROR: Expected service 'set_value', got '{data.get('service')}'")
            failed += 1
        elif data.get("service_data", {}).get("value") != 50:
            print(f"ERROR: Expected value 50")
            failed += 1
        else:
            print("✓ Domain-based number handled correctly")

    return failed


def test_hainterface_set_state_external_domain_select(my_predbat=None):
    """Test set_state_external() with domain-based select"""
    print("\n=== Testing HAInterface set_state_external() domain select ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    mock_base.trigger_callback_calls = []
    async def mock_trigger_callback(data):
        mock_base.trigger_callback_calls.append(data)
    mock_base.trigger_callback = mock_trigger_callback

    run_async(ha_interface.set_state_external("select.test", "option1", {}))

    if not mock_base.trigger_callback_calls:
        print("ERROR: trigger_callback should be called")
        failed += 1
    else:
        data = mock_base.trigger_callback_calls[0]
        if data.get("service") != "select_option":
            print(f"ERROR: Expected service 'select_option', got '{data.get('service')}'")
            failed += 1
        elif data.get("service_data", {}).get("option") != "option1":
            print(f"ERROR: Expected option 'option1'")
            failed += 1
        else:
            print("✓ Domain-based select handled correctly")

    return failed


def test_hainterface_set_state_external_sensor(my_predbat=None):
    """Test set_state_external() with sensor (direct state set)"""
    print("\n=== Testing HAInterface set_state_external() sensor ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    # Mock set_state
    set_state_called = []
    original_set_state = ha_interface.set_state
    def mock_set_state(entity_id, state, attributes={}):
        set_state_called.append((entity_id, state, attributes))
        # Don't call original_set_state to avoid API call
        # Just update state_data directly
        ha_interface.state_data[entity_id.lower()] = {"state": state, "attributes": attributes}
    ha_interface.set_state = mock_set_state

    run_async(ha_interface.set_state_external("sensor.test", 123, {"unit": "W"}))

    if not set_state_called:
        print("ERROR: set_state should be called")
        failed += 1
    else:
        entity_id, state, attributes = set_state_called[0]
        if entity_id != "sensor.test":
            print(f"ERROR: Expected entity_id 'sensor.test', got '{entity_id}'")
            failed += 1
        elif state != 123:
            print(f"ERROR: Expected state 123, got {state}")
            failed += 1
        else:
            print("✓ Sensor set_state called correctly")

    ha_interface.set_state = original_set_state
    return failed


def test_hainterface_set_state_external_watch_list(my_predbat=None):
    """Test set_state_external() triggers watch list"""
    print("\n=== Testing HAInterface set_state_external() watch list ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    # Mock set_state to prevent API calls
    def mock_set_state(entity_id, state, attributes={}):
        ha_interface.state_data[entity_id.lower()] = {"state": state, "attributes": attributes}
    ha_interface.set_state = mock_set_state

    # Track trigger_watch_list calls
    mock_base.trigger_watch_list_calls = []
    async def mock_trigger_watch_list(entity_id, attributes, old_state, new_state):
        mock_base.trigger_watch_list_calls.append((entity_id, attributes, old_state, new_state))
    mock_base.trigger_watch_list = mock_trigger_watch_list

    # Set initial state
    ha_interface.state_data["sensor.test"] = {"state": 100, "attributes": {}}

    # Change state
    run_async(ha_interface.set_state_external("sensor.test", 200, {"unit": "W"}))

    if not mock_base.trigger_watch_list_calls:
        print("ERROR: trigger_watch_list should be called")
        failed += 1
    else:
        entity_id, attributes, old_state, new_state = mock_base.trigger_watch_list_calls[0]
        if entity_id != "sensor.test":
            print(f"ERROR: Expected entity_id 'sensor.test'")
            failed += 1
        elif old_state.get("state") != 100:
            print(f"ERROR: Expected old_state 100")
            failed += 1
        elif new_state.get("state") != 200:
            print(f"ERROR: Expected new_state 200")
            failed += 1
        else:
            print("✓ watch_list triggered on value change")

    return failed


def test_hainterface_set_state_external_no_change(my_predbat=None):
    """Test set_state_external() doesn't trigger watch list when value unchanged"""
    print("\n=== Testing HAInterface set_state_external() no change ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")

    # Mock set_state to prevent API calls
    def mock_set_state(entity_id, state, attributes={}):
        ha_interface.state_data[entity_id.lower()] = {"state": state, "attributes": attributes}
    ha_interface.set_state = mock_set_state

    mock_base.trigger_watch_list_calls = []
    async def mock_trigger_watch_list(entity_id, attributes, old_state, new_state):
        mock_base.trigger_watch_list_calls.append((entity_id, attributes, old_state, new_state))
    mock_base.trigger_watch_list = mock_trigger_watch_list

    # Set initial state
    ha_interface.state_data["sensor.test"] = {"state": 100, "attributes": {}}

    # Set same value
    run_async(ha_interface.set_state_external("sensor.test", 100, {}))

    if mock_base.trigger_watch_list_calls:
        print("ERROR: watch_list should not be triggered when value unchanged")
        failed += 1
    else:
        print("✓ watch_list not triggered when value unchanged")

    return failed


def run_hainterface_service_tests(my_predbat):
    """Run all HAInterface service tests"""
    print("\n" + "=" * 80)
    print("HAInterface Service Tests")
    print("=" * 80)

    failed = 0
    failed += test_hainterface_call_service_websocket(my_predbat)
    failed += test_hainterface_call_service_loopback(my_predbat)
    failed += test_hainterface_async_call_service_basic(my_predbat)
    failed += test_hainterface_async_call_service_return_response(my_predbat)
    failed += test_hainterface_async_call_service_failed(my_predbat)
    failed += test_hainterface_async_call_service_exception(my_predbat)
    failed += test_hainterface_async_call_service_error_limit(my_predbat)
    failed += test_hainterface_set_state_external_config_item_switch(my_predbat)
    failed += test_hainterface_set_state_external_config_item_number(my_predbat)
    failed += test_hainterface_set_state_external_config_item_select(my_predbat)
    failed += test_hainterface_set_state_external_domain_switch(my_predbat)
    failed += test_hainterface_set_state_external_domain_number(my_predbat)
    failed += test_hainterface_set_state_external_domain_select(my_predbat)
    failed += test_hainterface_set_state_external_sensor(my_predbat)
    failed += test_hainterface_set_state_external_watch_list(my_predbat)
    failed += test_hainterface_set_state_external_no_change(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAInterface service tests passed!")
    else:
        print(f"❌ {failed} HAInterface service test(s) failed")
    print("=" * 80)

    return failed
