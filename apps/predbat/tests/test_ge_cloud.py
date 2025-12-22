# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on

from gecloud import GECloudDirect, GECloudData, regname_to_ha
from gecloud import GE_API_DEVICES
import time
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import tempfile
import os
from datetime import datetime, timedelta


def run_async(coro):
    """Helper function to run async coroutines in sync test functions"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class MockGECloudDirect(GECloudDirect):
    """Mock GECloudDirect class for testing without ComponentBase dependencies"""

    def __init__(self):
        # Don't call parent __init__ to avoid ComponentBase
        self.api_key = "test_api_key"
        self.automatic = True
        self.mock_api_responses = {}
        self.dashboard_items = {}
        self.log_messages = []
        self.config_args = {}

        # Initialize instance variables that GECloudDirect expects
        self.requests_total = 0
        self.failures_total = 0
        self.register_list = {}
        self.settings = {}
        self.status = {}
        self.meter = {}
        self.info = {}
        self.device_list = []
        self.evc_device_list = []
        self.evc_device = {}
        self.evc_data = {}
        self.evc_sessions = {}
        self.pending_writes = {}
        self.register_entity_map = {}
        self.polling_mode = False
        self.devices_dict = {}
        self.evc_devices_dict = []
        self.ems_device = None
        self.gateway_device = None
        self._now_utc_exact = datetime.now()

    @property
    def now_utc_exact(self):
        """Mock now_utc_exact property"""
        return self._now_utc_exact

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Mock dashboard_item - tracks calls"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes}

    def get_arg(self, name, default=None):
        """Mock get_arg"""
        return self.config_args.get(name, default)

    def set_arg(self, name, value):
        """Mock set_arg"""
        self.config_args[name] = value

    def update_success_timestamp(self):
        """Mock update_success_timestamp"""
        pass


class MockGECloudData(GECloudData):
    """Mock GECloudData class for testing without ComponentBase dependencies"""

    def __init__(self, config_root="/tmp"):
        # Don't call parent initialize
        self.ge_cloud_key = "test_api_key"
        self.ge_cloud_serial_config_item = "ge_cloud_serial"
        self.ge_cloud_serial = "test123"
        self.days_previous = [7, 30]
        self.max_days_previous = 31
        self.api_fatal = False
        self.ge_url_cache = {}
        self.ge_cloud_data = True
        self.mdata = []
        self.requests_total = 0
        self.failures_total = 0
        self.oldest_data_time = None
        self._config_root = config_root
        self.log_messages = []
        self.config_args = {}
        self._now_utc_exact = datetime.now()

    @property
    def config_root(self):
        """Mock config_root property"""
        return self._config_root

    @property
    def now_utc_exact(self):
        """Mock now_utc_exact property"""
        return self._now_utc_exact

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)

    def get_arg(self, name, default=None):
        """Mock get_arg"""
        return self.config_args.get(name, default)

    def update_success_timestamp(self):
        """Mock update_success_timestamp"""
        pass


# =============================================================================
# API Infrastructure Tests
# =============================================================================


def test_async_get_inverter_data_success(my_predbat):
    """Test successful API call"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Mock successful response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"serial": "test123", "status": "NORMAL"}}

        with patch("gecloud.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_to_thread.return_value = mock_response

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result != {"serial": "test123", "status": "NORMAL"}:
                    print("ERROR: Expected dict with data, got {}".format(result))
                    return 1
                if ge_cloud.requests_total != 1:
                    print("ERROR: Expected requests_total=1, got {}".format(ge_cloud.requests_total))
                    return 1
                if ge_cloud.failures_total != 0:
                    print("ERROR: Expected failures_total=0, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def test_async_get_inverter_data_auth_error(my_predbat):
    """Test authentication error (401)"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"error": "Unauthorized"}

        with patch("gecloud.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_to_thread.return_value = mock_response

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result != {}:
                    print("ERROR: Expected empty dict for 401, got {}".format(result))
                    return 1
                if ge_cloud.failures_total != 1:
                    print("ERROR: Expected failures_total=1, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def test_async_get_inverter_data_rate_limit(my_predbat):
    """Test rate limiting (429)"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.json.return_value = {"error": "Too many requests"}

        with patch("gecloud.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_to_thread.return_value = mock_response

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result is not None:
                    print("ERROR: Expected None for 429, got {}".format(result))
                    return 1
                if ge_cloud.failures_total != 1:
                    print("ERROR: Expected failures_total=1, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def test_async_get_inverter_data_timeout(my_predbat):
    """Test timeout error"""

    async def test():
        ge_cloud = MockGECloudDirect()

        import requests

        with patch("gecloud.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_to_thread.side_effect = requests.exceptions.Timeout("Timeout")

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result is not None:
                    print("ERROR: Expected None for timeout, got {}".format(result))
                    return 1
                if ge_cloud.failures_total != 1:
                    print("ERROR: Expected failures_total=1, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def test_async_get_inverter_data_json_error(my_predbat):
    """Test JSON decode error

    NOTE: Potential bug found in gecloud.py lines 1323-1341:
    When JSONDecodeError occurs, data is set to None (line 1325), but then at
    line 1339-1341, if status_code is 200 and data is None, it returns {} instead
    of None. This means JSON errors are silently ignored and treated as successful
    empty responses. Should JSONDecodeError return None or raise an error?
    """

    async def test():
        ge_cloud = MockGECloudDirect()

        import requests

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = requests.exceptions.JSONDecodeError("Invalid JSON", "", 0)

        with patch("gecloud.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_to_thread.return_value = mock_response

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                # BUG: Currently returns {} instead of None for JSON errors
                if result != {}:
                    print("ERROR: Expected {{}} for JSON error (current behavior), got {}".format(result))
                    return 1
        return 0

    return run_async(test())


def test_async_get_inverter_data_retry(my_predbat):
    """Test retry logic"""

    async def test():
        ge_cloud = MockGECloudDirect()

        call_count = [0]

        async def mock_get_data(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return None  # Fail first 2 times
            return {"serial": "test123"}

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_get_inverter_data_retry(GE_API_DEVICES)

            if result != {"serial": "test123"}:
                print("ERROR: Expected data after retry, got {}".format(result))
                return 1
            if call_count[0] != 3:
                print("ERROR: Expected 3 calls (2 retries), got {}".format(call_count[0]))
                return 1
            if mock_sleep.call_count != 2:
                print("ERROR: Expected 2 sleep calls, got {}".format(mock_sleep.call_count))
                return 1
        return 0

    return run_async(test())


# =============================================================================
# Device Discovery Tests
# =============================================================================


def test_async_get_devices_with_ems(my_predbat):
    """Test device discovery with EMS device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [{"inverter": {"serial": "ems001", "info": {"model": "Plant EMS"}, "connections": {"batteries": []}}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["ems"] != "ems001":
                print("ERROR: Expected ems='ems001', got {}".format(result))
                return 1
            if result["battery"] != []:
                print("ERROR: Expected empty battery list, got {}".format(result))
                return 1
        return 0

    return run_async(test())


def test_async_get_devices_with_gateway(my_predbat):
    """Test device discovery with Gateway device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [{"inverter": {"serial": "gw001", "info": {"model": "Gateway"}, "connections": {"batteries": [{"serial": "bat1"}]}}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["gateway"] != "gw001":
                print("ERROR: Expected gateway='gw001', got {}".format(result))
                return 1
        return 0

    return run_async(test())


def test_async_get_devices_with_batteries(my_predbat):
    """Test device discovery with battery inverters"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [
            {"inverter": {"serial": "inv001", "info": {"model": "All-In-One"}, "connections": {"batteries": [{"serial": "bat1"}]}}},
            {"inverter": {"serial": "inv002", "info": {"model": "Hybrid"}, "connections": {"batteries": [{"serial": "bat2"}]}}},
        ]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["battery"] != ["inv001", "inv002"]:
                print("ERROR: Expected battery=['inv001', 'inv002'], got {}".format(result))
                return 1
        return 0

    return run_async(test())


def test_async_get_devices_empty(my_predbat):
    """Test device discovery with no devices"""

    async def test():
        ge_cloud = MockGECloudDirect()

        async def mock_retry(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result != {"gateway": None, "ems": None, "battery": []}:
                print("ERROR: Expected empty result dict, got {}".format(result))
                return 1
        return 0

    return run_async(test())


def test_async_get_evc_devices(my_predbat):
    """Test getting EV charger devices"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful device list
        mock_devices = [{"uuid": "evc-001", "alias": "Home Charger", "other_data": {"some_field": "value"}}, {"uuid": "evc-002", "alias": "Garage Charger", "other_data": {}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_evc_devices()

            if len(result) != 2:
                print("ERROR: Expected 2 devices, got {}".format(len(result)))
                return 1
            if result[0]["uuid"] != "evc-001" or result[0]["alias"] != "Home Charger":
                print("ERROR: Expected first device with uuid='evc-001', alias='Home Charger', got {}".format(result[0]))
                return 1
            if result[1]["uuid"] != "evc-002" or result[1]["alias"] != "Garage Charger":
                print("ERROR: Expected second device with uuid='evc-002', alias='Garage Charger', got {}".format(result[1]))
                return 1

        # Test with None response (API failure) - should return previous
        async def mock_retry_fail(*args, **kwargs):
            return None

        previous_devices = [{"uuid": "old-001", "alias": "Old Charger"}]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_fail

            result = await ge_cloud.async_get_evc_devices(previous=previous_devices)

            if result != previous_devices:
                print("ERROR: Expected fallback to previous devices, got {}".format(result))
                return 1

        # Test with empty list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_evc_devices()

            if result != []:
                print("ERROR: Expected empty list, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def test_async_get_smart_devices(my_predbat):
    """Test getting smart devices"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful device list
        mock_devices = [
            {"uuid": "smart-001", "alias": "Smart Plug 1", "other_data": {"local_key": "abc123xyz"}},
            {"uuid": "smart-002", "alias": "Smart Plug 2", "other_data": {"local_key": "def456uvw"}},
            {"uuid": "smart-003", "alias": "Smart Switch", "other_data": {}},  # No local_key
        ]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_smart_devices()

            if len(result) != 3:
                print("ERROR: Expected 3 devices, got {}".format(len(result)))
                return 1
            if result[0]["uuid"] != "smart-001" or result[0]["alias"] != "Smart Plug 1" or result[0]["local_key"] != "abc123xyz":
                print("ERROR: Expected first device with uuid='smart-001', alias='Smart Plug 1', local_key='abc123xyz', got {}".format(result[0]))
                return 1
            if result[1]["uuid"] != "smart-002" or result[1]["alias"] != "Smart Plug 2" or result[1]["local_key"] != "def456uvw":
                print("ERROR: Expected second device with uuid='smart-002', alias='Smart Plug 2', local_key='def456uvw', got {}".format(result[1]))
                return 1
            if result[2]["uuid"] != "smart-003" or result[2]["alias"] != "Smart Switch" or result[2]["local_key"] is not None:
                print("ERROR: Expected third device with uuid='smart-003', alias='Smart Switch', local_key=None, got {}".format(result[2]))
                return 1

        # Test with None response (API failure) - should return previous
        async def mock_retry_fail(*args, **kwargs):
            return None

        previous_devices = [{"uuid": "old-001", "alias": "Old Device", "local_key": "old123"}]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_fail

            result = await ge_cloud.async_get_smart_devices(previous=previous_devices)

            if result != previous_devices:
                print("ERROR: Expected fallback to previous devices, got {}".format(result))
                return 1

        # Test with empty list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_smart_devices()

            if result != []:
                print("ERROR: Expected empty list, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def test_async_get_evc_commands(my_predbat):
    """Test getting EV charger commands"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful command retrieval and blacklist filtering
        test_uuid = "evc-001"
        mock_commands = ["start-charging", "stop-charging", "set-charge-limit", "perform-factory-reset", "installation-mode"]
        mock_command_data = {"start-charging": {"type": "button", "description": "Start charging"}, "stop-charging": {"type": "button", "description": "Stop charging"}, "set-charge-limit": {"type": "number", "min": 0, "max": 100}}

        call_count = [0]

        async def mock_retry(*args, **kwargs):
            call_count[0] += 1
            # First call is for getting commands list
            if call_count[0] == 1:
                return mock_commands.copy()  # Return a copy since function modifies it
            # Subsequent calls are for command data
            command = kwargs.get("command")
            if command in mock_command_data:
                return mock_command_data[command]
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_evc_commands(test_uuid)

            # Check that blacklisted commands were removed
            if "perform-factory-reset" in result:
                print("ERROR: Blacklisted command 'perform-factory-reset' should be removed, got {}".format(result))
                return 1
            if "installation-mode" in result:
                print("ERROR: Blacklisted command 'installation-mode' should be removed, got {}".format(result))
                return 1

            # Check that valid commands are present
            if "start-charging" not in result:
                print("ERROR: Expected 'start-charging' in result, got {}".format(result))
                return 1
            if result["start-charging"] != mock_command_data["start-charging"]:
                print("ERROR: Expected start-charging data {}, got {}".format(mock_command_data["start-charging"], result["start-charging"]))
                return 1
            if "stop-charging" not in result:
                print("ERROR: Expected 'stop-charging' in result, got {}".format(result))
                return 1
            if "set-charge-limit" not in result:
                print("ERROR: Expected 'set-charge-limit' in result, got {}".format(result))
                return 1

        # Test with empty commands list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_evc_commands(test_uuid)

            if result != {}:
                print("ERROR: Expected empty dict for empty commands, got {}".format(result))
                return 1

        # Test with only blacklisted commands
        async def mock_retry_blacklist_only(*args, **kwargs):
            return ["installation-mode", "perform-factory-reset", "delete-id-tags"]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_blacklist_only

            result = await ge_cloud.async_get_evc_commands(test_uuid)

            if result != {}:
                print("ERROR: Expected empty dict when all commands blacklisted, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def test_async_get_smart_device(my_predbat):
    """Test getting a single smart device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful device retrieval
        test_uuid = "smart-001"
        mock_device = {"uuid": "smart-001", "alias": "Living Room Smart Plug", "other_data": {"local_key": "abc123xyz", "asset_id": "asset-456", "hardware_id": "hw-789"}}

        async def mock_retry(*args, **kwargs):
            return mock_device

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_smart_device(test_uuid)

            if result["uuid"] != "smart-001":
                print("ERROR: Expected uuid='smart-001', got {}".format(result))
                return 1
            if result["alias"] != "Living Room Smart Plug":
                print("ERROR: Expected alias='Living Room Smart Plug', got {}".format(result))
                return 1
            if result["local_key"] != "abc123xyz":
                print("ERROR: Expected local_key='abc123xyz', got {}".format(result))
                return 1
            if result["asset_id"] != "asset-456":
                print("ERROR: Expected asset_id='asset-456', got {}".format(result))
                return 1
            if result["hardware_id"] != "hw-789":
                print("ERROR: Expected hardware_id='hw-789', got {}".format(result))
                return 1

        # Test with device missing some optional fields
        mock_device_partial = {
            "uuid": "smart-002",
            "alias": "Bedroom Switch",
            "other_data": {
                "local_key": "def456uvw"
                # No asset_id or hardware_id
            },
        }

        async def mock_retry_partial(*args, **kwargs):
            return mock_device_partial

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_partial

            result = await ge_cloud.async_get_smart_device(test_uuid)

            if result["uuid"] != "smart-002":
                print("ERROR: Expected uuid='smart-002', got {}".format(result))
                return 1
            if result["alias"] != "Bedroom Switch":
                print("ERROR: Expected alias='Bedroom Switch', got {}".format(result))
                return 1
            if result["local_key"] != "def456uvw":
                print("ERROR: Expected local_key='def456uvw', got {}".format(result))
                return 1
            if result["asset_id"] is not None:
                print("ERROR: Expected asset_id=None, got {}".format(result))
                return 1
            if result["hardware_id"] is not None:
                print("ERROR: Expected hardware_id=None, got {}".format(result))
                return 1

        # Test with None response (API failure) - should return empty dict
        async def mock_retry_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_none

            result = await ge_cloud.async_get_smart_device(test_uuid)

            if result != {}:
                print("ERROR: Expected empty dict for None response, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def test_async_get_evc_sessions(my_predbat):
    """Test getting EV charger sessions"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful session list retrieval
        test_uuid = "evc-001"
        mock_sessions = [
            {"session_id": "session-1", "start_time": "2025-12-17T10:00:00Z", "end_time": "2025-12-17T12:00:00Z", "energy_kwh": 25.5, "cost": 5.10},
            {"session_id": "session-2", "start_time": "2025-12-18T06:00:00Z", "end_time": "2025-12-18T07:30:00Z", "energy_kwh": 18.2, "cost": 3.64},
        ]

        async def mock_retry(*args, **kwargs):
            return mock_sessions

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_evc_sessions(test_uuid)

            if not isinstance(result, list):
                print("ERROR: Expected list, got {}".format(type(result)))
                return 1
            if len(result) != 2:
                print("ERROR: Expected 2 sessions, got {}".format(len(result)))
                return 1
            if result[0]["session_id"] != "session-1":
                print("ERROR: Expected session_id='session-1', got {}".format(result[0]))
                return 1
            if result[0]["energy_kwh"] != 25.5:
                print("ERROR: Expected energy_kwh=25.5, got {}".format(result[0]))
                return 1
            if result[1]["session_id"] != "session-2":
                print("ERROR: Expected session_id='session-2', got {}".format(result[1]))
                return 1

        # Test with empty session list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_evc_sessions(test_uuid)

            if result != []:
                print("ERROR: Expected empty list, got {}".format(result))
                return 1

        # Test with non-list response (API failure) - should return previous
        async def mock_retry_dict(*args, **kwargs):
            return {"error": "Invalid request"}

        previous_sessions = [{"session_id": "old-session", "energy_kwh": 10.0}]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_dict

            result = await ge_cloud.async_get_evc_sessions(test_uuid, previous=previous_sessions)

            if result != previous_sessions:
                print("ERROR: Expected fallback to previous sessions, got {}".format(result))
                return 1

        # Test with None response - should return previous
        async def mock_retry_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_none

            result = await ge_cloud.async_get_evc_sessions(test_uuid, previous=previous_sessions)

            if result != previous_sessions:
                print("ERROR: Expected fallback to previous when None, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def test_run_method(my_predbat):
    """Test GECloudDirect run method calls functions in correct order"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.automatic = True

        # Track function calls
        call_order = []

        # Mock all the async functions called by run()
        async def mock_get_devices():
            call_order.append("async_get_devices")
            return {"battery": ["inv001"], "ems": None, "gateway": None}

        async def mock_get_evc_devices():
            call_order.append("async_get_evc_devices")
            return [{"uuid": "evc-001", "alias": "Charger"}]

        async def mock_get_inverter_status(device, previous):
            call_order.append(f"async_get_inverter_status:{device}")
            return {"power": 1000}

        async def mock_publish_status(device, status):
            call_order.append(f"publish_status:{device}")

        async def mock_get_inverter_meter(device, previous):
            call_order.append(f"async_get_inverter_meter:{device}")
            return {"today": {"solar": 10}}

        async def mock_publish_meter(device, meter):
            call_order.append(f"publish_meter:{device}")

        async def mock_get_device_info(device, previous):
            call_order.append(f"async_get_device_info:{device}")
            return {"serial": device}

        async def mock_publish_info(device, info):
            call_order.append(f"publish_info:{device}")

        async def mock_get_evc_device(uuid, previous):
            call_order.append(f"async_get_evc_device:{uuid}")
            return {"serial_number": "evc-serial-001"}

        async def mock_get_evc_device_data(uuid, previous):
            call_order.append(f"async_get_evc_device_data:{uuid}")
            return {"power": 5000}

        async def mock_get_evc_sessions(uuid, previous):
            call_order.append(f"async_get_evc_sessions:{uuid}")
            return []

        async def mock_publish_evc_data(serial, data):
            call_order.append(f"publish_evc_data:{serial}")

        async def mock_get_inverter_settings(device, first, previous):
            call_order.append(f"async_get_inverter_settings:{device}")
            return {}

        async def mock_publish_registers(device, settings):
            call_order.append(f"publish_registers:{device}")

        async def mock_automatic_config(devices_dict):
            call_order.append("async_automatic_config")

        async def mock_enable_default_options(device, settings):
            call_order.append(f"enable_default_options:{device}")

        # Assign all mocks
        ge_cloud.async_get_devices = mock_get_devices
        ge_cloud.async_get_evc_devices = mock_get_evc_devices
        ge_cloud.async_get_inverter_status = mock_get_inverter_status
        ge_cloud.publish_status = mock_publish_status
        ge_cloud.async_get_inverter_meter = mock_get_inverter_meter
        ge_cloud.publish_meter = mock_publish_meter
        ge_cloud.async_get_device_info = mock_get_device_info
        ge_cloud.publish_info = mock_publish_info
        ge_cloud.async_get_evc_device = mock_get_evc_device
        ge_cloud.async_get_evc_device_data = mock_get_evc_device_data
        ge_cloud.async_get_evc_sessions = mock_get_evc_sessions
        ge_cloud.publish_evc_data = mock_publish_evc_data
        ge_cloud.async_get_inverter_settings = mock_get_inverter_settings
        ge_cloud.publish_registers = mock_publish_registers
        ge_cloud.async_automatic_config = mock_automatic_config
        ge_cloud.enable_default_options = mock_enable_default_options

        # Test first run (first=True, seconds=0)
        call_order = []
        result = await ge_cloud.run(seconds=0, first=True)

        if not result:
            print("ERROR: run() should return True on success")
            return 1

        # Verify expected call order for first run
        expected_order = [
            "async_get_devices",
            "async_get_evc_devices",
            # Device polling (every 60 seconds, also on first)
            "async_get_inverter_status:inv001",
            "publish_status:inv001",
            "async_get_inverter_meter:inv001",
            "publish_meter:inv001",
            "async_get_device_info:inv001",
            "publish_info:inv001",
            # EVC device polling
            "async_get_evc_device:evc-001",
            "async_get_evc_device_data:evc-001",
            "async_get_evc_sessions:evc-001",
            "publish_evc_data:evc-serial-001",
            # Settings (every 10 minutes, also on first)
            "async_get_inverter_settings:inv001",
            "publish_registers:inv001",
            # One-shot tasks (only on first)
            "async_automatic_config",
            "enable_default_options:inv001",
        ]

        if call_order != expected_order:
            print("ERROR: Call order mismatch on first run")
            print("Expected: {}".format(expected_order))
            print("Got:      {}".format(call_order))
            return 1

        # Test subsequent run at seconds=60 (not first, but divisible by 60)
        call_order = []
        result = await ge_cloud.run(seconds=60, first=False)

        if not result:
            print("ERROR: run() should return True on success")
            return 1

        # Should only do device polling, not device discovery or one-shot tasks
        expected_order_60 = [
            "async_get_inverter_status:inv001",
            "publish_status:inv001",
            "async_get_inverter_meter:inv001",
            "publish_meter:inv001",
            "async_get_device_info:inv001",
            "publish_info:inv001",
            "async_get_evc_device:evc-001",
            "async_get_evc_device_data:evc-001",
            "async_get_evc_sessions:evc-001",
            "publish_evc_data:evc-serial-001",
        ]

        if call_order != expected_order_60:
            print("ERROR: Call order mismatch at seconds=60")
            print("Expected: {}".format(expected_order_60))
            print("Got:      {}".format(call_order))
            return 1

        # Test run at seconds=600 (10 minutes, should also fetch settings)
        call_order = []
        result = await ge_cloud.run(seconds=600, first=False)

        expected_order_600 = [
            "async_get_inverter_status:inv001",
            "publish_status:inv001",
            "async_get_inverter_meter:inv001",
            "publish_meter:inv001",
            "async_get_device_info:inv001",
            "publish_info:inv001",
            "async_get_evc_device:evc-001",
            "async_get_evc_device_data:evc-001",
            "async_get_evc_sessions:evc-001",
            "publish_evc_data:evc-serial-001",
            "async_get_inverter_settings:inv001",
            "publish_registers:inv001",
        ]

        if call_order != expected_order_600:
            print("ERROR: Call order mismatch at seconds=600")
            print("Expected: {}".format(expected_order_600))
            print("Got:      {}".format(call_order))
            return 1

        # Test run at seconds=30 (not divisible by 60, should do nothing)
        call_order = []
        result = await ge_cloud.run(seconds=30, first=False)

        if call_order != []:
            print("ERROR: At seconds=30, no functions should be called, got {}".format(call_order))
            return 1

        return 0

    return run_async(test())


# =============================================================================
# Data Fetching Tests
# =============================================================================


def test_async_get_inverter_status(my_predbat):
    """Test getting inverter status"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_status = {"power": 1500, "soc": 75, "temperature": 25}

        async def mock_retry(*args, **kwargs):
            return mock_status

        ge_cloud.async_get_inverter_data_retry = mock_retry

        result = await ge_cloud.async_get_inverter_status("test123")

        if result != mock_status:
            print("ERROR: Expected status dict, got {}".format(result))
            return 1

        # Test fallback to previous on failure
        async def mock_retry_fail(*args, **kwargs):
            return None

        ge_cloud.async_get_inverter_data_retry = mock_retry_fail
        result2 = await ge_cloud.async_get_inverter_status("test123", previous={"old": "data"})

        if result2 != {"old": "data"}:
            print("ERROR: Expected fallback to previous, got {}".format(result2))
            return 1
        return 0

    return run_async(test())


def test_async_get_inverter_meter(my_predbat):
    """Test getting inverter meter data"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_meter = {"today": {"solar": 15.5, "grid": {"import": 5.2, "export": 10.3}}}

        async def mock_retry(*args, **kwargs):
            return mock_meter

        ge_cloud.async_get_inverter_data_retry = mock_retry

        result = await ge_cloud.async_get_inverter_meter("test123")

        if result != mock_meter:
            print("ERROR: Expected meter dict, got {}".format(result))
            return 1
        return 0

    return run_async(test())


def test_async_get_device_info(my_predbat):
    """Test getting device info"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [{"inverter": {"serial": "test123", "info": {"battery": {"nominal_capacity": 52}, "max_charge_rate": 6000}}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        ge_cloud.async_get_inverter_data_retry = mock_retry

        result = await ge_cloud.async_get_device_info("test123")

        # Result should be the inverter dict with serial and info
        if "info" not in result or "battery" not in result["info"] or result["info"]["battery"]["nominal_capacity"] != 52:
            print("ERROR: Expected device info with battery data, got {}".format(result))
            return 1
        if result["serial"] != "test123":
            print("ERROR: Expected serial test123, got {}".format(result.get("serial")))
            return 1
        return 0

    return run_async(test())


def test_async_get_inverter_settings_success(my_predbat):
    """Test getting inverter settings with parallel fetch"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Mock register list
        ge_cloud.register_list["test123"] = [
            {"id": 1, "name": "battery_soc", "validation": {}, "validation_rules": {}},
            {"id": 2, "name": "charge_power", "validation": {}, "validation_rules": {}},
        ]

        # Mock read setting responses
        read_count = [0]

        async def mock_read_setting(serial, setting_id):
            read_count[0] += 1
            if setting_id == 1:
                return {"value": "75"}
            elif setting_id == 2:
                return {"value": "3000"}
            return None

        ge_cloud.async_read_inverter_setting = mock_read_setting

        result = await ge_cloud.async_get_inverter_settings("test123")

        # Result structure: {sid: {"name": ..., "value": ..., "validation_rules": ..., "validation": ...}}
        if 1 not in result or result[1]["value"] != "75":
            print("ERROR: Expected setting 1 value='75', got {}".format(result.get(1)))
            return 1
        if 2 not in result or result[2]["value"] != "3000":
            print("ERROR: Expected setting 2 value='3000', got {}".format(result.get(2)))
            return 1
        if read_count[0] != 2:
            print("ERROR: Expected 2 read calls, got {}".format(read_count[0]))
            return 1
        return 0

    return run_async(test())


def test_async_get_inverter_settings_partial_failure(my_predbat):
    """Test getting inverter settings with some failures"""

    async def test():
        ge_cloud = MockGECloudDirect()

        ge_cloud.register_list["test123"] = [
            {"id": 1, "name": "battery_soc", "validation": {}, "validation_rules": {}},
            {"id": 2, "name": "charge_power", "validation": {}, "validation_rules": {}},
        ]

        async def mock_read_setting(serial, setting_id):
            if setting_id == 1:
                return {"value": "75"}
            return None  # Fail setting 2

        ge_cloud.async_read_inverter_setting = mock_read_setting

        # Provide previous data - previous dict structure matches result structure
        previous = {2: {"name": "charge_power", "value": "2500", "validation": {}, "validation_rules": {}}}
        result = await ge_cloud.async_get_inverter_settings("test123", previous=previous)

        if result[1]["value"] != "75":
            print("ERROR: Expected setting 1 value='75', got {}".format(result.get(1)))
            return 1
        if result[2]["value"] != "2500":
            print("ERROR: Expected setting 2 value='2500' (from previous), got {}".format(result.get(2)))
            return 1
        return 0

    return run_async(test())


# =============================================================================
# Write Operation Tests
# =============================================================================


def test_async_read_inverter_setting_success(my_predbat):
    """Test reading inverter setting successfully"""

    async def test():
        ge_cloud = MockGECloudDirect()

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            mock_response = {"value": "100"}

            async def mock_get_data(*args, **kwargs):
                return mock_response

            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_read_inverter_setting("test123", 77)

            if result != mock_response:
                print("ERROR: Expected response dict, got {}".format(result))
                return 1
            return 0

    return run_async(test())


def test_async_read_inverter_setting_error_codes(my_predbat):
    """Test error code handling in read inverter setting"""

    async def test():
        ge_cloud = MockGECloudDirect()

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Test fatal error code (-3)
            async def mock_get_data_fatal(*args, **kwargs):
                return {"data": {"value": -3}}

            ge_cloud.async_get_inverter_data = mock_get_data_fatal

            result = await ge_cloud.async_read_inverter_setting("test123", 77)

            # Returns None for fatal errors after trying all retries
            if result is not None:
                print("ERROR: Expected None for fatal error, got {}".format(result))
                return 1

            # Should have retried MAX_RETRIES times (default 3)
            if mock_sleep.call_count == 0:
                print("ERROR: Expected retries with sleeps, sleep called 0 times")
                return 1

        return 0

    return run_async(test())


def test_async_write_inverter_setting_success(my_predbat):
    """Test writing inverter setting successfully"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.pending_writes["test123"] = []  # Initialize pending writes for serial

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):

            async def mock_get_data(*args, **kwargs):
                return {"success": True}

            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_write_inverter_setting("test123", 77, "100")

            if result != {"success": True}:
                print("ERROR: Expected success response, got {}".format(result))
                return 1

            # Check pending_writes was updated
            if "test123" not in ge_cloud.pending_writes:
                print("ERROR: Expected pending_writes to be updated")
                return 1
            if len(ge_cloud.pending_writes["test123"]) == 0:
                print("ERROR: Expected entry in pending_writes")
                return 1
            if ge_cloud.pending_writes["test123"][0]["setting_id"] != 77:
                print("ERROR: Expected setting_id 77, got {}".format(ge_cloud.pending_writes["test123"][0].get("setting_id")))
                return 1
            if ge_cloud.pending_writes["test123"][0]["value"] != "100":
                print("ERROR: Expected value 100, got {}".format(ge_cloud.pending_writes["test123"][0].get("value")))
                return 1

            return 0

    return run_async(test())


def test_async_write_inverter_setting_failure(my_predbat):
    """Test writing inverter setting failure

    NOTE: Potential bug found in gecloud.py lines 938-939:
    When response has no 'success' key, data is not set to None, so function returns
    the error response instead of None and doesn't retry. Should missing 'success' key
    trigger retries like {"success": False} does?
    """

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.pending_writes["test123"] = []  # Initialize pending writes for serial

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            call_count = [0]

            async def mock_get_data(*args, **kwargs):
                call_count[0] += 1
                return {"success": False}  # Return explicit failure to trigger retry

            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_write_inverter_setting("test123", 77, "100")

            if result is not None:
                print("ERROR: Expected None after all retries, got {}".format(result))
                return 1

            # Should have retried 10 times
            if call_count[0] != 10:
                print("ERROR: Expected 10 retry attempts, got {}".format(call_count[0]))
                return 1

            return 0

    return run_async(test())


# =============================================================================
# Event Handler Tests
# =============================================================================


def test_switch_event(my_predbat):
    """Test switch event handler"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Set up entity mapping and settings
        ge_cloud.register_entity_map["switch.predbat_gecloud_test123_ac_charge_enable"] = {"key": 56, "device": "test123"}
        ge_cloud.settings["test123"] = {56: {"value": False, "validation_rules": []}}

        write_calls = []

        async def mock_write(serial, setting_id, value):
            write_calls.append({"serial": serial, "id": setting_id, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_write_inverter_setting = mock_write
            ge_cloud.publish_registers = mock_publish

            # Test turn_on
            await ge_cloud.switch_event("switch.predbat_gecloud_test123_ac_charge_enable", "turn_on")

            if len(write_calls) != 1:
                print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
                return 1
            if write_calls[0]["value"] != True:
                print("ERROR: Expected value=True for turn_on, got {}".format(write_calls[0]["value"]))
                return 1

            # Test turn_off
            await ge_cloud.switch_event("switch.predbat_gecloud_test123_ac_charge_enable", "turn_off")

            if len(write_calls) != 2:
                print("ERROR: Expected 2 write calls, got {}".format(len(write_calls)))
                return 1
            if write_calls[1]["value"] != False:
                print("ERROR: Expected value=False for turn_off, got {}".format(write_calls[1]["value"]))
                return 1

        return 0

    return run_async(test())


def test_number_event(my_predbat):
    """Test number event handler with validation"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Set up entity mapping with validation
        ge_cloud.register_entity_map["number.predbat_gecloud_test123_battery_reserve"] = {"key": 66, "device": "test123"}
        ge_cloud.settings["test123"] = {66: {"value": "10", "validation_rules": ["between:0,100"]}}

        write_calls = []

        async def mock_write(serial, setting_id, value):
            write_calls.append({"serial": serial, "id": setting_id, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_write_inverter_setting = mock_write
            ge_cloud.publish_registers = mock_publish

            # Test valid value
            await ge_cloud.number_event("number.predbat_gecloud_test123_battery_reserve", 20)

            if len(write_calls) != 1:
                print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
                return 1
            if write_calls[0]["value"] != 20.0:
                print("ERROR: Expected value=20.0, got {}".format(write_calls[0]["value"]))
                return 1

        return 0

    return run_async(test())


def test_select_event(my_predbat):
    """Test select event handler with options validation

    NOTE: Potential bug found in gecloud.py line 324:
    Code calls validation.startswith() without checking if validation is None first.
    Should check 'if validation and validation.startswith...' to avoid AttributeError.
    """

    async def test():
        ge_cloud = MockGECloudDirect()

        # Set up entity mapping with options
        ge_cloud.register_entity_map["select.predbat_gecloud_test123_charge_start_time"] = {"key": 56, "device": "test123"}
        ge_cloud.settings["test123"] = {56: {"value": "00:00", "validation": "Value must be one of: (00:00, 00:30, 01:00, 01:30)", "validation_rules": []}}

        write_calls = []

        async def mock_write(serial, setting_id, value):
            write_calls.append({"serial": serial, "id": setting_id, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_write_inverter_setting = mock_write
            ge_cloud.publish_registers = mock_publish

            # Test valid option
            await ge_cloud.select_event("select.predbat_gecloud_test123_charge_start_time", "01:30")

            if len(write_calls) != 1:
                print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
                return 1
            if write_calls[0]["value"] != "01:30":
                print("ERROR: Expected value='01:30', got {}".format(write_calls[0]["value"]))
                return 1

        return 0

    return run_async(test())


# =============================================================================
# Publishing Tests
# =============================================================================


def test_publish_status(my_predbat):
    """Test publishing status entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    status_data = {"power": 1500, "battery": {"percent": 75}, "solar": {"power": 2000}, "grid": {"power": -500}}

    ge_cloud.status["test123"] = status_data
    run_async(ge_cloud.publish_status("test123", status_data))

    # Check dashboard_item was called for status entities
    expected_entities = ["sensor.predbat_gecloud_test123_battery_power", "sensor.predbat_gecloud_test123_battery_percent", "sensor.predbat_gecloud_test123_solar_power", "sensor.predbat_gecloud_test123_grid_power"]

    for entity in expected_entities:
        if entity not in ge_cloud.dashboard_items:
            print("ERROR: Expected entity {} to be published".format(entity))
            return 1

    return 0


def test_publish_meter(my_predbat):
    """Test publishing meter entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    meter_data = {
        "today": {"solar": 15.5, "grid": {"import": 5.2, "export": 10.3}, "battery": {"charge": 8.0, "discharge": 6.5}, "consumption": 12.7},
        "total": {"solar": 6539.5, "grid": {"import": 19508.4, "export": 3230.3}, "battery": {"charge": 7290.95, "discharge": 7290.95}, "consumption": 21566.6},
    }

    ge_cloud.meter["test123"] = meter_data
    run_async(ge_cloud.publish_meter("test123", meter_data))

    # Check 'today' entities were published
    if "sensor.predbat_gecloud_test123_solar_today" not in ge_cloud.dashboard_items:
        print("ERROR: Expected solar_today to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_solar_today"]["state"] != 15.5:
        print("ERROR: Expected solar_today=15.5")
        return 1
    if "sensor.predbat_gecloud_test123_grid_import_today" not in ge_cloud.dashboard_items:
        print("ERROR: Expected grid_import_today to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_grid_import_today"]["state"] != 5.2:
        print("ERROR: Expected grid_import_today=5.2")
        return 1
    if "sensor.predbat_gecloud_test123_battery_charge_today" not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_charge_today to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_charge_today"]["state"] != 8.0:
        print("ERROR: Expected battery_charge_today=8.0")
        return 1

    # Check 'total' entities were published
    if "sensor.predbat_gecloud_test123_solar_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected solar_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_solar_total"]["state"] != 6539.5:
        print("ERROR: Expected solar_total=6539.5")
        return 1
    if "sensor.predbat_gecloud_test123_grid_import_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected grid_import_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_grid_import_total"]["state"] != 19508.4:
        print("ERROR: Expected grid_import_total=19508.4")
        return 1
    if "sensor.predbat_gecloud_test123_battery_charge_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_charge_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_charge_total"]["state"] != 7290.95:
        print("ERROR: Expected battery_charge_total=7290.95")
        return 1
    if "sensor.predbat_gecloud_test123_consumption_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected consumption_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_consumption_total"]["state"] != 21566.6:
        print("ERROR: Expected consumption_total=21566.6")
        return 1

    return 0


def test_publish_info(my_predbat):
    """Test publishing info entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    info_data = {"info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2}, "max_charge_rate": 6000}}

    ge_cloud.info["test123"] = info_data
    run_async(ge_cloud.publish_info("test123", info_data))

    # Check battery size entity (capacity is calculated as nominal_capacity * nominal_voltage / 1000)
    # 186 * 51.2 / 1000 = 9.5232 rounded to 9.52
    if "sensor.predbat_gecloud_test123_battery_size" not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_size to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_size"]["state"] != 9.52:
        print("ERROR: Expected battery size=9.52, got {}".format(ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_size"]["state"]))
        return 1

    return 0


def test_publish_registers(my_predbat):
    """Test publishing register entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    # Mock register dict (keyed by register ID) with different types
    registers = {
        56: {"name": "Enable AC Charge", "validation_rules": ["boolean"], "validation": "", "value": "1"},
        66: {"name": "Battery Reserve Percent Limit", "validation_rules": ["between:0,100"], "validation": "", "value": "20"},
        77: {"name": "AC Charge 1 Start Time", "validation_rules": ["date_format:H:i"], "validation": "", "value": "23:30:00"},
    }

    ge_cloud.register_list["test123"] = registers

    ge_cloud.settings["test123"] = {56: "1", 66: "20", 77: "23:30:00"}

    run_async(ge_cloud.publish_registers("test123", registers))

    # Check switch entity
    if "switch.predbat_gecloud_test123_enable_ac_charge" not in ge_cloud.dashboard_items:
        print("ERROR: Expected switch entity to be published")
        return 1

    # Check number entity
    if "number.predbat_gecloud_test123_battery_reserve_percent_limit" not in ge_cloud.dashboard_items:
        print("ERROR: Expected number entity to be published")
        return 1

    # Check select entity
    if "select.predbat_gecloud_test123_ac_charge_1_start_time" not in ge_cloud.dashboard_items:
        print("ERROR: Expected select entity to be published")
        return 1

    return 0


def test_publish_evc_data(my_predbat):
    """Test publishing EV charger data"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test data with various measurand types
        evc_data = {
            1: 16.5,  # Current.Import
            2: 32.0,  # Current.Offered
            4: 150.5,  # Energy.Active.Import.Register
            11: 50.0,  # Frequency
            13: 7200,  # Power.Active.Import
            14: 0.95,  # Power.Factor
            15: 7400,  # Power.Offered
            19: 75,  # SoC
            20: 28.5,  # Temperature
            21: 230,  # Voltage
        }

        serial = "EVC-001"

        await ge_cloud.publish_evc_data(serial, evc_data)

        # Verify entities were created with correct names and attributes
        expected_entities = {
            "sensor.predbat_gecloud_evc-001_evc_current_import": {"state": 16.5, "friendly_name": "EV Charger Current Import", "unit": "A", "device_class": "current"},
            "sensor.predbat_gecloud_evc-001_evc_current_offered": {"state": 32.0, "friendly_name": "EV Charger Current Offered", "unit": "A", "device_class": "current"},
            "sensor.predbat_gecloud_evc-001_evc_energy_active_import_register": {"state": 150.5, "friendly_name": "EV Charger Total Import", "unit": "kWh", "device_class": "energy"},
            "sensor.predbat_gecloud_evc-001_evc_frequency": {"state": 50.0, "friendly_name": "EV Charger Frequency", "unit": "Hz", "device_class": "frequency"},
            "sensor.predbat_gecloud_evc-001_evc_power_active_import": {"state": 7200, "friendly_name": "EV Charger Import Power", "unit": "W", "device_class": "power"},
            "sensor.predbat_gecloud_evc-001_evc_power_factor": {"state": 0.95, "friendly_name": "EV Charger Power Factor", "unit": "*", "device_class": "power_factor"},
            "sensor.predbat_gecloud_evc-001_evc_power_offered": {"state": 7400, "friendly_name": "EV Charger Power Offered", "unit": "W", "device_class": "power"},
            "sensor.predbat_gecloud_evc-001_evc_soc": {"state": 75, "friendly_name": "EV Charger State of Charge", "unit": "%", "device_class": "battery"},
            "sensor.predbat_gecloud_evc-001_evc_temperature": {"state": 28.5, "friendly_name": "EV Charger Temperature", "unit": "°C", "device_class": "temperature"},
            "sensor.predbat_gecloud_evc-001_evc_voltage": {"state": 230, "friendly_name": "EV Charger Voltage", "unit": "V", "device_class": "voltage"},
        }

        for entity_id, expected in expected_entities.items():
            if entity_id not in ge_cloud.dashboard_items:
                print("ERROR: Entity {} not created".format(entity_id))
                return 1

            item = ge_cloud.dashboard_items[entity_id]
            if item["state"] != expected["state"]:
                print("ERROR: Entity {} has state {}, expected {}".format(entity_id, item["state"], expected["state"]))
                return 1

            attrs = item["attributes"]
            if attrs["friendly_name"] != expected["friendly_name"]:
                print("ERROR: Entity {} has friendly_name '{}', expected '{}'".format(entity_id, attrs["friendly_name"], expected["friendly_name"]))
                return 1

            if attrs["unit_of_measurement"] != expected["unit"]:
                print("ERROR: Entity {} has unit '{}', expected '{}'".format(entity_id, attrs["unit_of_measurement"], expected["unit"]))
                return 1

            if attrs["device_class"] != expected["device_class"]:
                print("ERROR: Entity {} has device_class '{}', expected '{}'".format(entity_id, attrs["device_class"], expected["device_class"]))
                return 1

        # Test with empty data
        ge_cloud.dashboard_items.clear()
        await ge_cloud.publish_evc_data(serial, {})

        if len(ge_cloud.dashboard_items) != 0:
            print("ERROR: Expected no entities for empty data, got {}".format(len(ge_cloud.dashboard_items)))
            return 1

        # Test with unknown measurand (should be ignored)
        ge_cloud.dashboard_items.clear()
        await ge_cloud.publish_evc_data(serial, {999: 123.45})

        if len(ge_cloud.dashboard_items) != 0:
            print("ERROR: Unknown measurand should be ignored, got {} entities".format(len(ge_cloud.dashboard_items)))
            return 1

        return 0

    return run_async(test())


def test_async_automatic_config(my_predbat):
    """Test automatic configuration of Predbat based on GE Cloud devices"""

    async def test():
        ge = MockGECloudDirect()
        ge.config_args = {}

        # Test 1: Single battery with all features
        ge.settings = {"BATTERY001": {"reg1": {"name": "Inverter_Charge_Power_Percentage"}, "reg2": {"name": "Pause_Battery"}, "reg3": {"name": "Pause_Battery_Start_Time"}, "reg4": {"name": "DC_Discharge_1_Lower_SOC_Percent_Limit"}}}

        devices = {"ems": None, "gateway": None, "battery": ["BATTERY001"]}

        await ge.async_automatic_config(devices)

        # Verify basic configuration
        assert ge.config_args.get("inverter_type") == ["GEC"], "inverter_type should be set to GEC"
        assert ge.config_args.get("num_inverters") == 1, "num_inverters should be 1"
        assert ge.config_args.get("ge_cloud_serial") == "BATTERY001", "ge_cloud_serial should be first battery"
        assert ge.config_args.get("givtcp_rest") is None, "givtcp_rest should be None"

        # Verify sensor entities
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_BATTERY001_consumption_today"]
        assert ge.config_args.get("import_today") == ["sensor.predbat_gecloud_BATTERY001_grid_import_today"]
        assert ge.config_args.get("export_today") == ["sensor.predbat_gecloud_BATTERY001_grid_export_today"]
        assert ge.config_args.get("pv_today") == ["sensor.predbat_gecloud_BATTERY001_solar_today"]
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_BATTERY001_battery_power"]
        assert ge.config_args.get("pv_power") == ["sensor.predbat_gecloud_BATTERY001_solar_power"]
        assert ge.config_args.get("load_power") == ["sensor.predbat_gecloud_BATTERY001_consumption_power"]
        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_BATTERY001_grid_power"]
        assert ge.config_args.get("soc_percent") == ["sensor.predbat_gecloud_BATTERY001_battery_percent"]

        # Verify control entities
        assert ge.config_args.get("charge_rate") == ["number.predbat_gecloud_BATTERY001_battery_charge_power"]
        assert ge.config_args.get("discharge_rate") == ["number.predbat_gecloud_BATTERY001_battery_discharge_power"]
        assert ge.config_args.get("reserve") == ["number.predbat_gecloud_BATTERY001_battery_reserve_percent_limit"]
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_BATTERY001_ac_charge_upper_percent_limit"]

        # Verify time controls
        assert ge.config_args.get("charge_start_time") == ["select.predbat_gecloud_BATTERY001_ac_charge_1_start_time"]
        assert ge.config_args.get("charge_end_time") == ["select.predbat_gecloud_BATTERY001_ac_charge_1_end_time"]
        assert ge.config_args.get("discharge_start_time") == ["select.predbat_gecloud_BATTERY001_dc_discharge_1_start_time"]
        assert ge.config_args.get("discharge_end_time") == ["select.predbat_gecloud_BATTERY001_dc_discharge_1_end_time"]

        # Verify feature flags
        assert ge.config_args.get("pause_mode") == ["select.predbat_gecloud_BATTERY001_pause_battery"], "pause_mode should be set"
        assert ge.config_args.get("pause_start_time") == ["select.predbat_gecloud_BATTERY001_pause_battery_start_time"], "pause_start_time should be set"
        assert ge.config_args.get("pause_end_time") == ["select.predbat_gecloud_BATTERY001_pause_battery_end_time"], "pause_end_time should be set"
        assert ge.config_args.get("discharge_target_soc") == ["number.predbat_gecloud_BATTERY001_dc_discharge_1_lower_soc_percent_limit"], "discharge_target_soc should be set"
        assert ge.config_args.get("charge_rate_percent") == ["number.predbat_gecloud_BATTERY001_inverter_charge_power_percentage"], "charge_rate_percent should be set"
        assert ge.config_args.get("discharge_rate_percent") == ["number.predbat_gecloud_BATTERY001_inverter_discharge_power_percentage"], "discharge_rate_percent should be set"

        # Test 2: Battery without optional features
        ge.config_args = {}
        ge.settings = {"BATTERY002": {}}

        devices = {"ems": None, "gateway": None, "battery": ["BATTERY002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("pause_mode") is None, "pause_mode should be None when feature not detected"
        assert ge.config_args.get("pause_start_time") is None, "pause_start_time should be None"
        assert ge.config_args.get("pause_end_time") is None, "pause_end_time should be None"
        assert ge.config_args.get("discharge_target_soc") is None, "discharge_target_soc should be None"
        assert ge.config_args.get("charge_rate_percent") is None, "charge_rate_percent should be None"
        assert ge.config_args.get("discharge_rate_percent") is None, "discharge_rate_percent should be None"

        # Test 3: Multiple batteries
        ge.config_args = {}
        ge.settings = {"BATTERY001": {}, "BATTERY002": {}}

        devices = {"ems": None, "gateway": None, "battery": ["BATTERY001", "BATTERY002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 2, "num_inverters should be 2"
        assert ge.config_args.get("inverter_type") == ["GEC", "GEC"], "inverter_type should have 2 entries"
        assert len(ge.config_args.get("load_today")) == 2, "load_today should have 2 entries"
        assert ge.config_args.get("load_today")[0] == "sensor.predbat_gecloud_BATTERY001_consumption_today"
        assert ge.config_args.get("load_today")[1] == "sensor.predbat_gecloud_BATTERY002_consumption_today"

        # Test 4: EMS configuration
        ge.config_args = {}
        ge.settings = {"EMS001": {}, "BATTERY001": {}}

        devices = {"ems": "EMS001", "gateway": None, "battery": ["BATTERY001"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("inverter_type") == ["GEE"], "inverter_type should be GEE for EMS"
        assert ge.config_args.get("ge_cloud_serial") == "EMS001", "ge_cloud_serial should be EMS"
        assert ge.config_args.get("ge_cloud_data") is False, "ge_cloud_data should be False for EMS"

        # EMS-specific controls
        assert ge.config_args.get("charge_start_time") == ["select.predbat_gecloud_EMS001_charge_start_time_slot_1"]
        assert ge.config_args.get("charge_end_time") == ["select.predbat_gecloud_EMS001_charge_end_time_slot_1"]
        assert ge.config_args.get("idle_start_time") == ["select.predbat_gecloud_EMS001_discharge_start_time_slot_1"]
        assert ge.config_args.get("idle_end_time") == ["select.predbat_gecloud_EMS001_discharge_end_time_slot_1"]
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_EMS001_charge_soc_percent_limit_1"]
        assert ge.config_args.get("discharge_start_time") == ["select.predbat_gecloud_EMS001_export_start_time_slot_1"]
        assert ge.config_args.get("discharge_end_time") == ["select.predbat_gecloud_EMS001_export_end_time_slot_1"]

        # EMS-specific sensors
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_EMS001_consumption_today"]
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_EMS001_battery_power"]
        assert ge.config_args.get("pv_power") == ["sensor.predbat_gecloud_EMS001_solar_power"]
        assert ge.config_args.get("load_power") == ["sensor.predbat_gecloud_EMS001_consumption_power"]
        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_EMS001_grid_power"]

        # Test 5: Multiple batteries with gateway (should use gateway as control)
        ge.config_args = {}
        ge.settings = {"GATEWAY001": {}, "BATTERY001": {}, "BATTERY002": {}}

        devices = {"ems": None, "gateway": "GATEWAY001", "battery": ["BATTERY001", "BATTERY002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 1, "num_inverters should be 1 when using gateway"
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_GATEWAY001_consumption_today"], "Should use gateway for control"

        # Test 6: No battery devices (should log error and return)
        ge.config_args = {}
        devices = {"ems": None, "gateway": None, "battery": []}

        await ge.async_automatic_config(devices)

        assert len(ge.config_args) == 0, "config_args should be empty when no batteries"

        # Test 7: None devices (should log error and return)
        ge.config_args = {}
        await ge.async_automatic_config(None)

        assert len(ge.config_args) == 0, "config_args should be empty when devices is None"

        # Test 8: EMS with multiple inverters
        ge.config_args = {}
        ge.settings = {"EMS001": {}, "BATTERY001": {}, "BATTERY002": {}}

        devices = {"ems": "EMS001", "gateway": None, "battery": ["BATTERY001", "BATTERY002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 2, "num_inverters should be 2 for EMS with multiple batteries"
        assert ge.config_args.get("inverter_type") == ["GEE", "GEE"], "inverter_type should have 2 GEE entries"
        # EMS produces data for all inverters, so additional inverters get 0
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_EMS001_battery_power", 0], "Second inverter should get 0 for battery_power"
        assert ge.config_args.get("pv_power") == ["sensor.predbat_gecloud_EMS001_solar_power", 0], "Second inverter should get 0 for pv_power"

        return 0

    return run_async(test())


def test_enable_default_options(my_predbat):
    """Test enabling default options for inverter settings"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.settings = {"test123": {}}

        # Test 1: Export SOC percent limit that needs fixing (value > 4)
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 10, "validation_rules": []}}

        write_calls = []

        async def mock_write(device, key, value):
            write_calls.append({"device": device, "key": key, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        ge_cloud.async_write_inverter_setting = mock_write
        ge_cloud.publish_registers = mock_publish

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting export SOC limit")
            return 1
        if len(write_calls) != 1:
            print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for export SOC limit, got {}".format(write_calls[0]["value"]))
            return 1
        if registers[100]["value"] != 4:
            print("ERROR: Register value should be updated to 4, got {}".format(registers[100]["value"]))
            return 1

        # Test 2: Export SOC percent limit already at correct value (4)
        write_calls = []
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 4, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when no changes needed")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Expected 0 write calls when value already correct, got {}".format(len(write_calls)))
            return 1

        # Test 3: Discharge SOC percent limit that needs fixing (None value)
        write_calls = []
        registers = {101: {"name": "DC_Discharge_SOC_Percent_Limit", "value": None, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting discharge SOC limit")
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for discharge SOC limit, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 4: AC charge upper percent limit that needs fixing (value < 100)
        write_calls = []
        registers = {102: {"name": "AC_Charge_Upper_Percent_Limit", "value": 95, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting AC charge limit")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for AC charge limit, got {}".format(write_calls[0]["value"]))
            return 1
        if registers[102]["value"] != 100:
            print("ERROR: Register value should be updated to 100, got {}".format(registers[102]["value"]))
            return 1

        # Test 5: Inverter max output active power percent that needs fixing
        write_calls = []
        registers = {103: {"name": "Inverter_Max_Output_Active_Power_Percent", "value": 80, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting max output power")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for max output power, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 6: Skip enable_ settings (like enable_ac_charge_upper_percent_limit)
        write_calls = []
        registers = {104: {"name": "Enable_AC_Charge_Upper_Percent_Limit", "value": 50, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should skip enable_ settings")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write to enable_ settings, got {} calls".format(len(write_calls)))
            return 1

        # Test 7: Real-time control needs enabling (value is False/None)
        write_calls = []
        registers = {105: {"name": "Real_Time_Control", "value": False, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when enabling real-time control")
            return 1
        if write_calls[0]["value"] != True:
            print("ERROR: Expected value=True for real-time control, got {}".format(write_calls[0]["value"]))
            return 1
        if registers[105]["value"] != True:
            print("ERROR: Register value should be updated to True, got {}".format(registers[105]["value"]))
            return 1

        # Test 8: Real-time control already enabled
        write_calls = []
        registers = {105: {"name": "Real_Time_Control", "value": True, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when real-time control already enabled")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write when real-time control already enabled, got {} calls".format(len(write_calls)))
            return 1

        # Test 9: Write failure should return False
        write_calls = []
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 10, "validation_rules": []}}

        async def mock_write_fail(device, key, value):
            write_calls.append({"device": device, "key": key, "value": value})
            return None  # Simulate write failure

        ge_cloud.async_write_inverter_setting = mock_write_fail

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when write fails")
            return 1

        # Test 10: Multiple settings - should process first match only
        write_calls = []
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 10, "validation_rules": []}, 102: {"name": "AC_Charge_Upper_Percent_Limit", "value": 80, "validation_rules": []}}

        ge_cloud.async_write_inverter_setting = mock_write

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True after processing first match")
            return 1
        # Should only process the first matching setting (export SOC limit)
        if len(write_calls) != 1:
            print("ERROR: Should only process first matching setting, got {} calls".format(len(write_calls)))
            return 1
        if write_calls[0]["key"] != 100:
            print("ERROR: Should process first setting (key 100), got key {}".format(write_calls[0]["key"]))
            return 1

        # Test 11: AC charge slot 2 start time needs resetting
        write_calls = []
        registers = {200: {"name": "AC_Charge_2_Start_Time", "value": "05:30", "validation_rules": []}}
        ge_cloud.async_write_inverter_setting = mock_write

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting AC charge 2 start time")
            return 1
        if len(write_calls) != 1:
            print("ERROR: Expected 1 write call for AC charge 2 start time, got {}".format(len(write_calls)))
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for AC charge 2 start time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 12: AC charge slot 5 end time needs resetting
        write_calls = []
        registers = {201: {"name": "AC_Charge_5_End_Time", "value": "08:00", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting AC charge 5 end time")
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for AC charge 5 end time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 13: DC discharge slot 3 start time needs resetting
        write_calls = []
        registers = {202: {"name": "DC_Discharge_3_Start_Time", "value": "14:00", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting DC discharge 3 start time")
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for DC discharge 3 start time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 14: DC discharge slot 10 end time needs resetting
        write_calls = []
        registers = {203: {"name": "DC_Discharge_10_End_Time", "value": "22:30", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting DC discharge 10 end time")
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for DC discharge 10 end time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 15: AC charge slot 2 start time already at 00:00 - should not write
        write_calls = []
        registers = {200: {"name": "AC_Charge_2_Start_Time", "value": "00:00", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when AC charge 2 start time already 00:00")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write when time already 00:00, got {} calls".format(len(write_calls)))
            return 1

        # Test 16: AC charge slot 2 start time is None - should not write
        write_calls = []
        registers = {200: {"name": "AC_Charge_2_Start_Time", "value": None, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when AC charge 2 start time is None")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write when time is None, got {} calls".format(len(write_calls)))
            return 1

        # Test 17: AC charge slot 1 should NOT be reset (slots 2-10 only)
        write_calls = []
        registers = {210: {"name": "AC_Charge_1_Start_Time", "value": "05:30", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False for AC charge 1 (not in range 2-10)")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write to AC charge 1 slot, got {} calls".format(len(write_calls)))
            return 1

        # Test 18: Lower SOC percent limit needs fixing
        write_calls = []
        registers = {220: {"name": "Lower_SOC_Percent_Limit", "value": 10, "validation_rules": []}}
        ge_cloud.async_write_inverter_setting = mock_write

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting lower SOC limit")
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for lower SOC limit, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 19: Upper SOC percent limit needs fixing
        write_calls = []
        registers = {221: {"name": "DC_Discharge_Upper_SOC_Percent_Limit", "value": 95, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting upper SOC limit")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for upper SOC limit, got {}".format(write_calls[0]["value"]))
            return 1

        return 0

    return run_async(test())


# =============================================================================
# GECloudData Download and Caching Tests
# =============================================================================


def test_download_ge_data_single_day(my_predbat):
    """Test downloading data for a single day"""

    async def test():
        with tempfile.TemporaryDirectory() as tmpdir:
            ge_data = MockGECloudData(config_root=tmpdir)
            ge_data.max_days_previous = 0

            # Mock API response
            with patch("requests.get") as mock_get:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"data": [{"time": "2024-12-17T10:00:00Z", "total": {"consumption": 1.5, "grid": {"import": 0.5, "export": 0.0}, "solar": 2.0}}]}
                mock_get.return_value = mock_response

                now = datetime(2024, 12, 17, 12, 0, 0)
                result = await ge_data.download_ge_data(now)

                if not result:
                    print("ERROR: Expected download to succeed")
                    return 1
                if len(ge_data.mdata) == 0:
                    print("ERROR: Expected data to be parsed")
                    return 1
                if ge_data.mdata[0]["consumption"] != 1.5:
                    print("ERROR: Expected consumption=1.5, got {}".format(ge_data.mdata[0]["consumption"]))
                    return 1

                return 0

    return run_async(test())


def test_download_ge_data_multi_day(my_predbat):
    """Test downloading data for multiple days"""

    async def test():
        with tempfile.TemporaryDirectory() as tmpdir:
            ge_data = MockGECloudData(config_root=tmpdir)
            ge_data.max_days_previous = 2

            call_count = [0]

            def mock_response_fn(*args, **kwargs):
                call_count[0] += 1
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"data": [{"time": "2024-12-{:02d}T10:00:00Z".format(15 + call_count[0]), "total": {"consumption": 1.0 * call_count[0], "grid": {"import": 0.5, "export": 0.0}, "solar": 1.5}}]}
                return mock_response

            with patch("requests.get") as mock_get:
                mock_get.side_effect = mock_response_fn

                now = datetime(2024, 12, 17, 12, 0, 0)
                result = await ge_data.download_ge_data(now)

                if not result:
                    print("ERROR: Expected download to succeed")
                    return 1

                # Should fetch 3 days (today + 2 previous)
                if call_count[0] < 3:
                    print("ERROR: Expected at least 3 API calls, got {}".format(call_count[0]))
                    return 1

                return 0

    return run_async(test())


def test_download_ge_data_pagination(my_predbat):
    """Test downloading data with pagination"""

    async def test():
        with tempfile.TemporaryDirectory() as tmpdir:
            ge_data = MockGECloudData(config_root=tmpdir)
            ge_data.max_days_previous = 0

            call_count = [0]

            def mock_response_fn(*args, **kwargs):
                call_count[0] += 1
                mock_response = MagicMock()
                mock_response.status_code = 200

                if call_count[0] == 1:
                    # First page with next link
                    mock_response.json.return_value = {
                        "data": [{"time": "2024-12-17T10:00:00Z", "total": {"consumption": 1.0, "grid": {"import": 0.5, "export": 0.0}, "solar": 1.5}}],
                        "links": {"next": "https://api.givenergy.cloud/v1/inverter/test123/data-points/2024-12-17?page=2"},
                    }
                else:
                    # Second page without next link
                    mock_response.json.return_value = {"data": [{"time": "2024-12-17T11:00:00Z", "total": {"consumption": 2.0, "grid": {"import": 0.5, "export": 0.0}, "solar": 2.5}}]}
                return mock_response

            with patch("requests.get") as mock_get:
                mock_get.side_effect = mock_response_fn

                now = datetime(2024, 12, 17, 12, 0, 0)
                result = await ge_data.download_ge_data(now)

                if not result:
                    print("ERROR: Expected download to succeed")
                    return 1
                if call_count[0] != 2:
                    print("ERROR: Expected 2 API calls for pagination, got {}".format(call_count[0]))
                    return 1
                if len(ge_data.mdata) != 2:
                    print("ERROR: Expected 2 data points, got {}".format(len(ge_data.mdata)))
                    return 1

                return 0

    return run_async(test())


def test_get_ge_url_cache_hit(my_predbat):
    """Test cache hit when data is fresh"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        url = "https://api.givenergy.cloud/v1/test"
        now = datetime(2024, 12, 17, 12, 0, 0)

        # Pre-populate cache with fresh data
        ge_data.ge_url_cache[url] = {"stamp": now - timedelta(minutes=10), "data": [{"test": "cached"}], "next": None}  # 10 minutes old

        with patch("requests.get") as mock_get:
            data, url_next = ge_data.get_ge_url(url, {}, now, max_age_minutes=30)

            if data != [{"test": "cached"}]:
                print("ERROR: Expected cached data, got {}".format(data))
                return 1
            if mock_get.called:
                print("ERROR: Should not have called API for cache hit")
                return 1

        return 0


def test_get_ge_url_cache_miss(my_predbat):
    """Test cache miss when data is stale"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        url = "https://api.givenergy.cloud/v1/test"
        now = datetime(2024, 12, 17, 12, 0, 0)

        # Pre-populate cache with stale data
        ge_data.ge_url_cache[url] = {"stamp": now - timedelta(minutes=40), "data": [{"test": "old"}], "next": None}  # 40 minutes old

        with patch("requests.get") as mock_get:
            # Mock fresh API response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": [{"time": "2024-12-17T12:00:00Z", "total": {"consumption": 1.5, "grid": {"import": 0.5, "export": 0.2}, "solar": 2.0}}]}
            mock_get.return_value = mock_response

            data, url_next = ge_data.get_ge_url(url, {}, now, max_age_minutes=30)

            if not data or len(data) == 0:
                print("ERROR: Expected fresh data, got {}".format(data))
                return 1
            if data[0]["consumption"] != 1.5:
                print("ERROR: Expected consumption=1.5, got {}".format(data[0]))
                return 1
            if not mock_get.called:
                print("ERROR: Should have called API for cache miss")
                return 1

        return 0


def test_clean_ge_url_cache(my_predbat):
    """Test cleaning old cache entries"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        now = datetime(2024, 12, 17, 12, 0, 0)

        # Add mix of fresh and old entries
        ge_data.ge_url_cache["url1"] = {"stamp": now - timedelta(hours=1), "data": [{"test": "fresh"}], "next": None}  # Fresh
        ge_data.ge_url_cache["url2"] = {"stamp": now - timedelta(hours=25), "data": [{"test": "old"}], "next": None}  # Old (>24 hours)
        ge_data.ge_url_cache["url3"] = {"stamp": now - timedelta(hours=2), "data": [{"test": "fresh2"}], "next": None}  # Fresh

        ge_data.clean_ge_url_cache(now)

        if "url1" not in ge_data.ge_url_cache:
            print("ERROR: Fresh entry url1 should not be removed")
            return 1
        if "url2" in ge_data.ge_url_cache:
            print("ERROR: Old entry url2 should be removed")
            return 1
        if "url3" not in ge_data.ge_url_cache:
            print("ERROR: Fresh entry url3 should not be removed")
            return 1

        return 0


# =============================================================================
# Cache Persistence Tests
# =============================================================================


def test_load_save_ge_cache(my_predbat):
    """Test saving and loading cache to/from disk"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        now = datetime(2024, 12, 17, 12, 0, 0)

        # Populate cache
        ge_data.ge_url_cache["test_url"] = {"stamp": now, "data": [{"test": "data"}], "next": None}

        # Save to disk
        ge_data.save_ge_cache()

        # Check file exists
        cache_file = ge_data.get_ge_cache_filename()
        if not os.path.exists(cache_file):
            print("ERROR: Cache file should exist at {}".format(cache_file))
            return 1

        # Create new instance and load
        ge_data2 = MockGECloudData(config_root=tmpdir)
        ge_data2.load_ge_cache()

        if "test_url" not in ge_data2.ge_url_cache:
            print("ERROR: Cache should be loaded from disk")
            return 1
        if ge_data2.ge_url_cache["test_url"]["data"] != [{"test": "data"}]:
            print("ERROR: Cached data mismatch")
            return 1

        return 0


def test_load_ge_cache_corrupt_file(my_predbat):
    """Test loading cache with corrupt/missing file"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        # Create corrupt cache file
        cache_file = ge_data.get_ge_cache_filename()
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "w") as f:
            f.write("invalid: yaml: content: [[[")

        # Should handle corrupt file gracefully
        ge_data.load_ge_cache()

        if ge_data.ge_url_cache != {}:
            print("ERROR: Should initialize empty cache for corrupt file")
            return 1

        return 0


# =============================================================================
# Helper Function Tests
# =============================================================================


def test_regname_to_ha(my_predbat):
    """Test register name to HA entity conversion"""
    # NOTE: Potential bug found - dots are NOT replaced with underscores
    # This could create invalid HA entity names if register names contain dots
    test_cases = [
        ("Battery SOC", "battery_soc"),
        ("AC Charge Enable", "ac_charge_enable"),
        ("Battery Reserve Percent Limit", "battery_reserve_percent_limit"),
        ("AC Charge 1 Start Time", "ac_charge_1_start_time"),
        ("Test-Name With_Mixed.Case", "test_name_with_mixed.case"),  # Dots NOT replaced (potential bug)
    ]

    for input_name, expected in test_cases:
        result = regname_to_ha(input_name)
        if result != expected:
            print("ERROR: regname_to_ha('{}') expected '{}', got '{}'".format(input_name, expected, result))
            return 1

    return 0


def test_get_data(my_predbat):
    """Test GECloudData.get_data() method"""
    ge_data = MockGECloudData()

    test_time = datetime(2024, 12, 17, 10, 0, 0)
    ge_data.mdata = [{"consumption": 1.5}]
    ge_data.oldest_data_time = test_time

    mdata, oldest = ge_data.get_data()

    if mdata != [{"consumption": 1.5}]:
        print("ERROR: Expected mdata, got {}".format(mdata))
        return 1
    if oldest != test_time:
        print("ERROR: Expected oldest_data_time, got {}".format(oldest))
        return 1

    return 0


# =============================================================================
# Integration Test (Existing)
# =============================================================================


def run_test_ge_cloud(my_predbat):
    """
    GE Cloud integration test (requires real API key)
    """
    failed = False

    ge_cloud_direct = GECloudDirect(my_predbat)
    ge_cloud_direct_task = my_predbat.create_task(ge_cloud_direct.start())
    while not "devices" in ge_cloud_direct.__dict__:
        time.sleep(1)
    devices = ge_cloud_direct.devices
    if not devices:
        print("ERROR: No devices found")
        failed = True
    else:
        for device in devices:
            print("Device {} found:".format(device))
            while not ge_cloud_direct.settings.get(device):
                time.sleep(1)
            print("Device {} synced".format(device))

        my_predbat.create_task(ge_cloud_direct.switch_event("switch.predbat_gecloud_sa2243g277_ac_charge_enable", "turn_on"))
        time.sleep(1)
    print("Stopping cloud")
    ge_cloud_direct.stop_cloud = True
    time.sleep(1)

    return failed
