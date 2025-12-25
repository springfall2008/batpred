# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Shared mock infrastructure for HAInterface unit tests.

This module provides common mock classes and helpers used across all HAInterface test files.
"""

import pytz
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock
from aiohttp import WSMsgType
import json


class MockComponents:
    """Mock components registry"""

    def __init__(self):
        self.components = {}

    def get_component(self, name):
        return self.components.get(name, None)

    def register_component(self, name, component):
        self.components[name] = component


class MockDatabaseManager:
    """
    Mock DatabaseManager for HAInterface testing.
    Provides simple state storage without real database operations.
    """

    def __init__(self):
        self.state_data = {}  # entity_id -> {"state": value, "attributes": dict, "last_changed": datetime}
        self.get_state_calls = []
        self.set_state_calls = []
        self.get_history_calls = []

    def get_state_db(self, entity_id):
        """Mock get_state_db - returns stored state or None"""
        self.get_state_calls.append(entity_id)
        if entity_id in self.state_data:
            return {
                "state": self.state_data[entity_id]["state"],
                "attributes": self.state_data[entity_id]["attributes"],
                "last_changed": self.state_data[entity_id]["last_changed"],
            }
        return None

    def set_state_db(self, entity_id, state, attributes, timestamp=None):
        """Mock set_state_db - stores state and returns item"""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        elif isinstance(timestamp, datetime):
            timestamp = timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

        self.set_state_calls.append({"entity_id": entity_id, "state": state, "attributes": attributes, "timestamp": timestamp})

        self.state_data[entity_id] = {"state": state, "attributes": attributes, "last_changed": timestamp}

        return {"state": state, "attributes": attributes, "last_changed": timestamp}

    def get_history_db(self, sensor, now, days=30):
        """Mock get_history_db - returns empty history"""
        self.get_history_calls.append({"sensor": sensor, "now": now, "days": days})
        # Return mock history format: [[{entry1}, {entry2}, ...]]
        return [[]]

    def get_all_entities_db(self):
        """Mock get_all_entities_db - returns list of entity IDs"""
        return list(self.state_data.keys())

    def get_history_db(self, sensor, now, days=30):
        """Mock get_history_db - returns empty list"""
        return []


class MockBase:
    """
    Mock base class for HAInterface testing.
    Provides minimal attributes and methods required by ComponentBase and HAInterface.
    """

    def __init__(self):
        self.components = MockComponents()
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.prefix = "predbat"
        self.args = {}
        self.CONFIG_ITEMS = []
        self.SERVICE_REGISTER_LIST = []
        self.update_pending = False
        self.callback_calls = []
        self.watch_list_calls = []
        self.ha_interface = None
        self.fatal_error_occurred_called = False
        self.fatal_error = False

    def log(self, message):
        """Log messages for test verification"""
        self.log_messages.append(message)

    async def trigger_callback(self, service_data):
        """Mock trigger_callback - tracks calls"""
        self.callback_calls.append(service_data)

    async def trigger_watch_list(self, entity_id, attribute, old_state, new_state):
        """Mock trigger_watch_list - tracks calls"""
        self.watch_list_calls.append({"entity_id": entity_id, "attribute": attribute, "old_state": old_state, "new_state": new_state})

    def fatal_error_occurred(self):
        """Mock fatal_error_occurred - tracks calls"""
        self.fatal_error_occurred_called = True


class MockWebsocket:
    """
    Simplified mock websocket that yields controlled message sequences.
    Used for testing socketLoop() event handling.
    """

    def __init__(self, messages=None):
        """
        Initialize mock websocket with message sequence.

        Args:
            messages: List of message dicts or None for empty sequence
        """
        self.messages = messages or []
        self.message_idx = 0
        self.sent_messages = []

    async def send_json(self, data):
        """Track sent JSON messages"""
        self.sent_messages.append(data)

    def __aiter__(self):
        """Async iterator support"""
        return self

    async def __anext__(self):
        """Return next message or stop iteration"""
        if self.message_idx < len(self.messages):
            msg = self.messages[self.message_idx]
            self.message_idx += 1
            return msg
        raise StopAsyncIteration

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, *args):
        """Async context manager exit"""
        pass


def create_websocket_message(message_type, data_dict):
    """
    Create a mock websocket message object.

    Args:
        message_type: WSMsgType enum (TEXT, CLOSED, ERROR)
        data_dict: Dictionary to serialize as JSON data

    Returns:
        Mock message object with type and data attributes
    """
    mock_message = MagicMock()
    mock_message.type = message_type
    if message_type == WSMsgType.TEXT:
        mock_message.data = json.dumps(data_dict)
    else:
        mock_message.data = None
    return mock_message


def create_state_changed_message(entity_id, old_state_value, new_state_value, attributes=None):
    """
    Create a state_changed event message.

    Args:
        entity_id: Entity ID that changed
        old_state_value: Old state value
        new_state_value: New state value
        attributes: Optional attributes dict

    Returns:
        Mock websocket message
    """
    if attributes is None:
        attributes = {}

    message_data = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "old_state": {"state": old_state_value, "entity_id": entity_id, "attributes": attributes} if old_state_value is not None else None,
                "new_state": {"state": new_state_value, "entity_id": entity_id, "attributes": attributes},
            },
        },
    }
    return create_websocket_message(WSMsgType.TEXT, message_data)


def create_call_service_message(domain, service, service_data):
    """
    Create a call_service event message.

    Args:
        domain: Service domain
        service: Service name
        service_data: Service data dict

    Returns:
        Mock websocket message
    """
    message_data = {"type": "event", "event": {"event_type": "call_service", "data": {"domain": domain, "service": service, "service_data": service_data}}}
    return create_websocket_message(WSMsgType.TEXT, message_data)


def create_result_message(success=True, result=None):
    """
    Create a result message.

    Args:
        success: Whether result was successful
        result: Optional result data dict

    Returns:
        Mock websocket message
    """
    message_data = {"type": "result", "success": success}
    if result is not None:
        message_data["result"] = result
    return create_websocket_message(WSMsgType.TEXT, message_data)


def create_auth_message(message_type):
    """
    Create an auth-related message.

    Args:
        message_type: One of 'auth_required', 'auth_ok', 'auth_invalid'

    Returns:
        Mock websocket message
    """
    message_data = {"type": message_type}
    return create_websocket_message(WSMsgType.TEXT, message_data)


def create_mock_requests_response(status_code=200, json_data=None, json_error=False, timeout=False):
    """
    Create a mock requests.Response object for testing api_call().

    Args:
        status_code: HTTP status code
        json_data: Data to return from .json() call
        json_error: If True, .json() raises JSONDecodeError
        timeout: If True, raises Timeout exception

    Returns:
        Mock Response object or exception
    """
    if timeout:
        import requests

        raise requests.Timeout("Mocked timeout")

    mock_response = Mock()
    mock_response.status_code = status_code

    if json_error:
        import requests

        mock_response.json.side_effect = requests.exceptions.JSONDecodeError("Mock error", "", 0)
    elif json_data is not None:
        mock_response.json.return_value = json_data
    else:
        mock_response.json.return_value = {}

    return mock_response


def create_mock_session_for_websocket(websocket_mock):
    """
    Create a mock aiohttp ClientSession that returns the given websocket.

    Args:
        websocket_mock: MockWebsocket instance

    Returns:
        Mock session with ws_connect method
    """
    mock_session = MagicMock()

    # ws_connect returns the websocket mock directly (it has __aenter__/__aexit__)
    mock_session.ws_connect.return_value = websocket_mock

    # Session itself needs context manager support
    async def session_aenter(*args):
        return mock_session

    async def session_aexit(*args):
        pass

    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit

    return mock_session


def create_ha_interface(mock_base, ha_url="http://test", ha_key=None, db_enable=False, db_mirror_ha=False, db_primary=False, skip_addon_check=True, websocket_active=False):
    """
    Helper to create HAInterface with initialization.

    Args:
        mock_base: MockBase instance
        ha_url: HA URL
        ha_key: HA API key (None for no API mode)
        db_enable: Enable database
        db_mirror_ha: Enable DB mirroring
        db_primary: DB primary mode
        skip_addon_check: If True, mock API calls to bypass addon/services check
        websocket_active: If True, set websocket_active flag

    Returns:
        Initialized HAInterface instance
    """
    from ha import HAInterface
    from unittest.mock import patch

    # If no ha_key and not db_primary, we can't initialize properly
    # Set db_primary=True for no-API mode
    if not ha_key and not db_primary and not db_enable:
        db_primary = True
        db_enable = True

    # Bypass ComponentBase.__init__ by creating instance without calling it
    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False
    ha_interface.last_success_timestamp = None
    ha_interface.local_tz = mock_base.local_tz
    ha_interface.prefix = mock_base.prefix
    ha_interface.args = mock_base.args
    ha_interface.count_errors = 0

    # Set db_manager from mock_base (required by update_state_item)
    if db_enable or db_mirror_ha or db_primary:
        ha_interface.db_manager = mock_base.components.get_component("DatabaseManager")
    else:
        ha_interface.db_manager = None

    # Now call initialize with proper parameters
    if skip_addon_check and ha_key:
        # Mock the API calls in initialize()
        with patch("ha.requests.get") as mock_get:
            # Mock services check to return valid data
            mock_get.return_value = create_mock_requests_response(200, [{"domain": "test"}])
            ha_interface.initialize(ha_url, ha_key, db_enable, db_mirror_ha, db_primary)
    else:
        ha_interface.initialize(ha_url, ha_key, db_enable, db_mirror_ha, db_primary)

    # Override websocket_active if requested
    if websocket_active:
        ha_interface.websocket_active = websocket_active
        ha_interface.ha_url = ha_url  # Ensure ha_url is set

    # Add fatal_error_occurred method
    ha_interface.fatal_error_occurred = mock_base.fatal_error_occurred

    return ha_interface
