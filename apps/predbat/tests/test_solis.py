# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
from datetime import timezone
from unittest.mock import patch, MagicMock
from solis import SolisAPI, SOLIS_CID_CHARGE_ENABLE_BASE, SOLIS_CID_CHARGE_TIME, SOLIS_CID_CHARGE_SOC_BASE, SOLIS_CID_CHARGE_CURRENT, SOLIS_CID_DISCHARGE_ENABLE_BASE
from solis import SOLIS_CID_BATTERY_FORCE_CHARGE_SOC, SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC, SOLIS_CID_CHARGE_DISCHARGE_SETTINGS
from solis import SOLIS_CID_STORAGE_MODE, SOLIS_BIT_GRID_CHARGING, SOLIS_BIT_TOU_MODE
from solis import SOLIS_CID_ALLOW_EXPORT, SOLIS_ALLOW_EXPORT_ON, SOLIS_ALLOW_EXPORT_OFF, SOLIS_CID_BATTERY_RESERVE_SOC
from solis import SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT
from solis import SOLIS_CID_POWER_LIMIT, SOLIS_STORAGE_MODES, SOLIS_BIT_BACKUP_MODE


class MockBase:
    """Mock base object for get_arg calls"""

    def __init__(self, prefix="predbat"):
        self.prefix = prefix

    def get_arg(self, key, default=None):
        """Mock get_arg method"""
        if key == "prefix":
            return self.prefix
        return default


class MockSolisAPI(SolisAPI):
    """Mock SolisAPI class for testing without ComponentBase dependencies"""

    def __init__(self, prefix="predbat"):
        # Don't call parent __init__ to avoid ComponentBase
        self.prefix = prefix
        self.api_key = "test_key"
        self.api_secret = "test_secret"
        self.base_url = "https://api.soliscloud.com"
        self.automatic = False
        self.session = None
        self.nominal_voltage = 48.4
        self.control_enable = True
        self.inverter_sn = []

        # Mock base object for get_arg calls
        self.base = MockBase(prefix)

        # Cache structures
        self.cached_values = {}
        self.inverter_details = {}
        self.storage_modes = {}
        self.parallel_battery_count = {}
        self.max_charge_current = {}
        self.max_discharge_current = {}
        self.charge_discharge_time_windows = {}
        self.slots_reset = set()

        # Logging
        self.log_messages = []
        self.dashboard_items = {}

        # Track method calls
        self.read_and_write_cid_calls = []
        self.set_storage_mode_calls = []

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)
        print(message)

    def call_notify(self, message):
        """Mock notify method"""
        self.log_messages.append("Alert: " + message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Mock dashboard_item method"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes}

    def get_state_wrapper(self, entity_id, default=None):
        """Mock get_state_wrapper method"""
        if entity_id in self.dashboard_items:
            return self.dashboard_items[entity_id]["state"]
        return default

    def set_arg(self, key, value):
        """Mock set_arg method"""
        pass

    def is_tou_v2_mode(self, sn):
        """Mock is_tou_v2_mode - can be overridden in tests"""
        # Default to V1 mode unless overridden
        return getattr(self, "_test_v2_mode", False)

    async def read_and_write_cid(self, inverter_sn, cid, value, field_description=None):
        """Mock read_and_write_cid - track calls"""
        self.read_and_write_cid_calls.append({"inverter_sn": inverter_sn, "cid": cid, "value": str(value), "field_description": field_description})  # Convert to string like real implementation

        # Update cache to simulate successful write
        if inverter_sn not in self.cached_values:
            self.cached_values[inverter_sn] = {}
        self.cached_values[inverter_sn][cid] = str(value)

        return True

    # Note: read_cid and _with_retry are NOT mocked - use real implementations from SolisAPI

    async def set_storage_mode_if_needed(self, inverter_sn, mode):
        """Override to track calls for write_time_windows tests, or use real implementation"""
        if hasattr(self, "_mock_storage_mode") and self._mock_storage_mode:
            # Mock mode: just track the call
            self.set_storage_mode_calls.append({"inverter_sn": inverter_sn, "mode": mode})
            return True
        else:
            # Real mode: use parent implementation
            return await SolisAPI.set_storage_mode_if_needed(self, inverter_sn, mode)

    # Note: encode_time_windows and set_storage_mode are NOT mocked - use real implementation from SolisAPI


async def test_fetch_entity_data():
    """Test fetch_entity_data fetches values from HA entities and updates cache"""
    print("\n=== Test: fetch_entity_data ===")

    api = MockSolisAPI()
    inverter_sn = "010262229130043"
    api.inverter_sn = [inverter_sn]
    api.nominal_voltage = 48.4
    api.max_charge_current[inverter_sn] = 62
    api.max_discharge_current[inverter_sn] = 62

    # Pre-populate time windows with some slots (simulating decode_time_windows output)
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_enable": 0,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
        },
        2: {
            "charge_enable": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
        },
    }

    # Mock HA entity states - simulate what publish_entities would have created
    # Charge slot 1
    api.dashboard_items[f"switch.predbat_solis_{inverter_sn}_charge_slot1_enable"] = {"state": "on", "attributes": {}}
    api.dashboard_items[f"select.predbat_solis_{inverter_sn}_charge_slot1_start_time"] = {"state": "02:30:00", "attributes": {}}
    api.dashboard_items[f"select.predbat_solis_{inverter_sn}_charge_slot1_end_time"] = {"state": "05:30:00", "attributes": {}}
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_charge_slot1_soc"] = {"state": "95", "attributes": {}}
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_charge_slot1_power"] = {"state": "3000", "attributes": {}}  # 3000W = ~62A at 48.4V

    # Discharge slot 1
    api.dashboard_items[f"switch.predbat_solis_{inverter_sn}_discharge_slot1_enable"] = {"state": "off", "attributes": {}}
    api.dashboard_items[f"select.predbat_solis_{inverter_sn}_discharge_slot1_start_time"] = {"state": "16:00:00", "attributes": {}}
    api.dashboard_items[f"select.predbat_solis_{inverter_sn}_discharge_slot1_end_time"] = {"state": "19:00:00", "attributes": {}}
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_discharge_slot1_soc"] = {"state": "20", "attributes": {}}
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_discharge_slot1_power"] = {"state": "2400", "attributes": {}}  # 2400W = ~49A at 48.4V

    # Charge slot 2 - partial data
    api.dashboard_items[f"switch.predbat_solis_{inverter_sn}_charge_slot2_enable"] = {"state": "off", "attributes": {}}
    api.dashboard_items[f"select.predbat_solis_{inverter_sn}_charge_slot2_start_time"] = {"state": "00:00:00", "attributes": {}}
    # No end_time entity exists (None return case)

    # Call fetch_entity_data
    await api.fetch_entity_data(inverter_sn)

    # Verify charge slot 1 values were fetched
    slot1 = api.charge_discharge_time_windows[inverter_sn][1]
    assert slot1["charge_enable"] == 1, f"Expected charge_enable=1, got {slot1['charge_enable']}"
    assert slot1["charge_start_time"] == "02:30", f"Expected charge_start_time='02:30', got {slot1['charge_start_time']}"
    assert slot1["charge_end_time"] == "05:30", f"Expected charge_end_time='05:30', got {slot1['charge_end_time']}"
    assert slot1["charge_soc"] == 95, f"Expected charge_soc=95, got {slot1['charge_soc']}"
    # Power converted to amps: 3000W / 48.4V = 61.98A, clamped to max 62A
    expected_charge_current = int(3000 / 48.4)  # ~61A
    assert slot1["charge_current"] == expected_charge_current, f"Expected charge_current={expected_charge_current}, got {slot1['charge_current']}"

    # Verify discharge slot 1 values were fetched
    assert slot1["discharge_enable"] == 0, f"Expected discharge_enable=0, got {slot1['discharge_enable']}"
    assert slot1["discharge_start_time"] == "16:00", f"Expected discharge_start_time='16:00', got {slot1['discharge_start_time']}"
    assert slot1["discharge_end_time"] == "19:00", f"Expected discharge_end_time='19:00', got {slot1['discharge_end_time']}"
    assert slot1["discharge_soc"] == 20, f"Expected discharge_soc=20, got {slot1['discharge_soc']}"
    # Power converted to amps: 2400W / 48.4V = 49.58A
    expected_discharge_current = int(2400 / 48.4)  # ~49A
    assert slot1["discharge_current"] == expected_discharge_current, f"Expected discharge_current={expected_discharge_current}, got {slot1['discharge_current']}"

    # Verify slot 2 values (only enable and start_time were available)
    slot2 = api.charge_discharge_time_windows[inverter_sn][2]
    assert slot2["charge_enable"] == 0, f"Expected charge_enable=0 for slot 2, got {slot2['charge_enable']}"
    assert slot2["charge_start_time"] == "00:00", f"Expected charge_start_time='00:00' for slot 2, got {slot2['charge_start_time']}"
    # end_time should be unchanged (original value) since entity didn't exist
    assert slot2["charge_end_time"] == "00:00", f"Expected charge_end_time unchanged for slot 2"

    print("PASSED: fetch_entity_data correctly fetches and converts HA entity states")
    return False


async def test_fetch_entity_data_power_clamping():
    """Test fetch_entity_data clamps power to max current limits"""
    print("\n=== Test: fetch_entity_data power clamping ===")

    api = MockSolisAPI()
    inverter_sn = "TEST12345"
    api.inverter_sn = [inverter_sn]
    api.nominal_voltage = 48.0  # Simplified voltage
    api.max_charge_current[inverter_sn] = 50  # Max 50A
    api.max_discharge_current[inverter_sn] = 40  # Max 40A

    # Pre-populate time window
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_enable": 0,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
        }
    }

    # Set power values that would exceed max current when converted
    # Charge: 4800W / 48V = 100A, but max is 50A
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_charge_slot1_power"] = {"state": "4800", "attributes": {}}
    # Discharge: 3000W / 48V = 62.5A, but max is 40A
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_discharge_slot1_power"] = {"state": "3000", "attributes": {}}

    # Call fetch_entity_data
    await api.fetch_entity_data(inverter_sn)

    # Verify values were clamped to max
    slot1 = api.charge_discharge_time_windows[inverter_sn][1]
    assert slot1["charge_current"] == 50, f"Expected charge_current clamped to 50A, got {slot1['charge_current']}"
    assert slot1["discharge_current"] == 40, f"Expected discharge_current clamped to 40A, got {slot1['discharge_current']}"

    print("PASSED: fetch_entity_data correctly clamps power to max current limits")
    return False


async def test_fetch_entity_data_invalid_values():
    """Test fetch_entity_data handles invalid/non-numeric values gracefully"""
    print("\n=== Test: fetch_entity_data invalid values ===")

    api = MockSolisAPI()
    inverter_sn = "BAD_DATA"
    api.inverter_sn = [inverter_sn]
    api.nominal_voltage = 48.4
    api.max_charge_current[inverter_sn] = 62
    api.max_discharge_current[inverter_sn] = 62

    # Pre-populate time window with initial values
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_soc": 10,
            "discharge_current": 0,
        }
    }

    # Set invalid values
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_charge_slot1_soc"] = {"state": "not_a_number", "attributes": {}}
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_charge_slot1_power"] = {"state": "invalid", "attributes": {}}
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_discharge_slot1_soc"] = {"state": None, "attributes": {}}  # None state
    api.dashboard_items[f"number.predbat_solis_{inverter_sn}_discharge_slot1_power"] = {"state": "", "attributes": {}}  # Empty string

    # Call fetch_entity_data - should not crash
    await api.fetch_entity_data(inverter_sn)

    # Verify original values are unchanged (exception caught and passed)
    slot1 = api.charge_discharge_time_windows[inverter_sn][1]
    assert slot1["charge_soc"] == 100, f"Expected charge_soc unchanged at 100, got {slot1['charge_soc']}"
    assert slot1["charge_current"] == 0, f"Expected charge_current unchanged at 0, got {slot1['charge_current']}"
    assert slot1["discharge_soc"] == 10, f"Expected discharge_soc unchanged at 10, got {slot1['discharge_soc']}"
    assert slot1["discharge_current"] == 0, f"Expected discharge_current unchanged at 0, got {slot1['discharge_current']}"

    print("PASSED: fetch_entity_data handles invalid values gracefully")
    return False


def run_solis_tests(my_predbat):
    """
    Run all Solis API tests
    Returns False on success, True on failure
    """
    failed = False

    try:
        # Run tests
        failed |= asyncio.run(test_read_cid())
        failed |= asyncio.run(test_read_batch())
        failed |= asyncio.run(test_read_and_write_cid())
        failed |= asyncio.run(test_write_cid())
        failed |= asyncio.run(test_decode_time_windows_v2())
        failed |= asyncio.run(test_write_time_windows_v2_mode())
        failed |= asyncio.run(test_write_time_windows_v1_mode())
        failed |= asyncio.run(test_write_time_windows_v2_no_changes())
        failed |= asyncio.run(test_write_time_windows_zero_charge_current())
        failed |= asyncio.run(test_write_time_windows_v1_slot_detection())
        failed |= asyncio.run(test_encode_time_windows_variant1())
        failed |= asyncio.run(test_encode_time_windows_variant2())
        failed |= asyncio.run(test_encode_time_windows_empty())
        failed |= asyncio.run(test_encode_time_windows_defaults())
        failed |= asyncio.run(test_encode_decode_roundtrip_variant1())
        failed |= asyncio.run(test_encode_decode_roundtrip_variant2())
        failed |= asyncio.run(test_publish_entities())
        failed |= asyncio.run(test_select_event_storage_mode())
        failed |= asyncio.run(test_select_event_charge_time())
        failed |= asyncio.run(test_select_event_discharge_time())
        failed |= asyncio.run(test_select_event_unknown_inverter())
        failed |= asyncio.run(test_switch_event_charge_enable())
        failed |= asyncio.run(test_switch_event_discharge_enable())
        failed |= asyncio.run(test_switch_event_battery_reserve())
        failed |= asyncio.run(test_switch_event_allow_grid_charging())
        failed |= asyncio.run(test_switch_event_time_of_use())
        failed |= asyncio.run(test_switch_event_allow_export())
        failed |= asyncio.run(test_switch_event_unknown_service())
        failed |= asyncio.run(test_number_event_charge_soc())
        failed |= asyncio.run(test_number_event_charge_power())
        failed |= asyncio.run(test_number_event_discharge_soc())
        failed |= asyncio.run(test_number_event_discharge_power())
        failed |= asyncio.run(test_number_event_battery_soc_limits())
        failed |= asyncio.run(test_number_event_max_power())
        failed |= asyncio.run(test_number_event_power_controls())
        failed |= asyncio.run(test_number_event_unknown_inverter())
        failed |= asyncio.run(test_set_storage_mode_self_use())
        failed |= asyncio.run(test_set_storage_mode_feed_in_priority())
        failed |= asyncio.run(test_set_storage_mode_unknown_mode())
        failed |= asyncio.run(test_set_storage_mode_if_needed_changes())
        failed |= asyncio.run(test_set_storage_mode_if_needed_no_changes())
        failed |= asyncio.run(test_set_storage_mode_if_needed_all_modes())
        failed |= asyncio.run(test_fetch_entity_data())
        failed |= asyncio.run(test_fetch_entity_data_power_clamping())
        failed |= asyncio.run(test_fetch_entity_data_invalid_values())
        failed |= asyncio.run(test_automatic_config())

    except Exception as e:
        print(f"Error running Solis tests: {e}")
        import traceback

        traceback.print_exc()
        failed = True

    if failed:
        print("ERROR: One or more Solis tests failed")
    else:
        print("PASSED: All Solis tests")

    return failed


async def test_read_cid():
    """Test read_cid method reads single CID value successfully"""
    print("\n=== Test: read_cid ===")

    api = MockSolisAPI()
    inverter_sn = "TEST123456"

    # Mock _execute_request to return test data
    async def mock_execute_request(endpoint, payload):
        # Verify the endpoint and payload
        assert endpoint == "/v2/api/atRead", f"Unexpected endpoint: {endpoint}"
        assert "inverterSn" in payload, "Missing inverterSn in payload"
        assert "cid" in payload, "Missing cid in payload"
        assert payload["inverterSn"] == inverter_sn, f"Expected inverterSn {inverter_sn}, got {payload['inverterSn']}"

        # Return mock response based on CID
        cid = payload["cid"]
        if cid == 103:
            return {"msg": "50"}  # Battery SOC
        elif cid == 633:
            return {"msg": "33"}  # Storage mode
        else:
            return {"msg": "0"}

    api._execute_request = mock_execute_request

    # Test reading battery SOC
    result = await api.read_cid(inverter_sn, 103)
    assert result == "50", f"Expected '50', got '{result}'"
    print("PASSED: read_cid returns correct value for battery SOC")

    # Test reading storage mode
    result = await api.read_cid(inverter_sn, 633)
    assert result == "33", f"Expected '33', got '{result}'"
    print("PASSED: read_cid returns correct value for storage mode")

    # Test error handling - missing data field
    # Mock asyncio.sleep and time.monotonic to avoid delays during retry testing
    import asyncio
    import time

    original_sleep = asyncio.sleep
    original_monotonic = time.monotonic

    mock_time = [0]  # Use list so we can modify in nested function

    def mock_monotonic():
        """Mock monotonic that advances time with each call"""
        mock_time[0] += 100  # Advance by 100 seconds to exceed max_retry_time
        return mock_time[0]

    async def mock_sleep(delay):
        """Mock sleep that returns immediately"""
        pass

    asyncio.sleep = mock_sleep
    time.monotonic = mock_monotonic

    async def mock_execute_request_no_data(endpoint, payload):
        return None

    api._execute_request = mock_execute_request_no_data

    try:
        await api.read_cid(inverter_sn, 103)
        assert False, "Should have raised SolisAPIError for missing data"
    except Exception as e:
        assert "missing 'data' field" in str(e), f"Unexpected error message: {e}"
        print("PASSED: read_cid handles missing data field correctly")

    # Test error handling - missing msg field
    async def mock_execute_request_no_msg(endpoint, payload):
        return {}  # Empty dict, no msg field

    api._execute_request = mock_execute_request_no_msg

    try:
        await api.read_cid(inverter_sn, 103)
        assert False, "Should have raised SolisAPIError for missing msg"
    except Exception as e:
        assert "missing 'msg' field" in str(e), f"Unexpected error message: {e}"
        print("PASSED: read_cid handles missing msg field correctly")

    # Test retry logic - API fails first 2 times then succeeds
    mock_time[0] = 0  # Reset mock time
    call_count = [0]  # Track number of calls

    def mock_monotonic_gradual():
        """Mock monotonic that advances time gradually"""
        mock_time[0] += 0.1  # Advance by 0.1 seconds to stay within max_retry_time
        return mock_time[0]

    time.monotonic = mock_monotonic_gradual

    async def mock_execute_request_with_retries(endpoint, payload):
        call_count[0] += 1
        if call_count[0] <= 2:
            # First 2 calls fail
            from solis import SolisAPIError

            raise SolisAPIError(f"API error on attempt {call_count[0]}")
        else:
            # Third call succeeds
            cid = payload["cid"]
            if cid == 103:
                return {"msg": "50"}
            else:
                return {"msg": "0"}

    api._execute_request = mock_execute_request_with_retries

    result = await api.read_cid(inverter_sn, 103)
    assert result == "50", f"Expected '50' after retries, got '{result}'"
    assert call_count[0] == 3, f"Expected 3 API calls (2 failures + 1 success), got {call_count[0]}"
    print("PASSED: read_cid retries after API failures and eventually succeeds")

    # Restore original sleep and monotonic
    asyncio.sleep = original_sleep
    time.monotonic = original_monotonic

    return False


async def test_read_batch():
    """Test read_batch method reads multiple CID values successfully"""
    print("\n=== Test: read_batch ===")

    api = MockSolisAPI()
    inverter_sn = "TEST123456"

    # Mock _execute_request to return batch test data
    async def mock_execute_request(endpoint, payload):
        # Verify the endpoint and payload
        assert endpoint == "/v2/api/atReadBatch", f"Unexpected endpoint: {endpoint}"
        assert "inverterSn" in payload, "Missing inverterSn in payload"
        assert "cids" in payload, "Missing cids in payload"
        assert payload["inverterSn"] == inverter_sn, f"Expected inverterSn {inverter_sn}, got {payload['inverterSn']}"

        # Return mock nested response arrays: [[{"cid": "103", "msg": "50"}, {"cid": "633", "msg": "33"}]]
        return [[{"cid": "103", "msg": "50"}, {"cid": "633", "msg": "33"}, {"cid": "104", "msg": "100"}]]

    api._execute_request = mock_execute_request

    # Test reading multiple CIDs
    result = await api.read_batch(inverter_sn, [103, 633, 104])
    assert isinstance(result, dict), f"Expected dict result, got {type(result)}"
    assert result[103] == "50", f"Expected '50' for CID 103, got '{result.get(103)}'"
    assert result[633] == "33", f"Expected '33' for CID 633, got '{result.get(633)}'"
    assert result[104] == "100", f"Expected '100' for CID 104, got '{result.get(104)}'"
    assert len(result) == 3, f"Expected 3 values, got {len(result)}"
    print("PASSED: read_batch returns correct values for multiple CIDs")

    # Test error handling - missing data field
    import asyncio
    import time

    original_sleep = asyncio.sleep
    original_monotonic = time.monotonic

    mock_time = [0]

    def mock_monotonic():
        """Mock monotonic that advances time with each call"""
        mock_time[0] += 100  # Advance by 100 seconds to exceed max_retry_time
        return mock_time[0]

    async def mock_sleep(delay):
        """Mock sleep that returns immediately"""
        pass

    asyncio.sleep = mock_sleep
    time.monotonic = mock_monotonic

    async def mock_execute_request_no_data(endpoint, payload):
        return None

    api._execute_request = mock_execute_request_no_data

    try:
        await api.read_batch(inverter_sn, [103, 633])
        assert False, "Should have raised SolisAPIError for missing data"
    except Exception as e:
        assert "missing 'data' field" in str(e), f"Unexpected error message: {e}"
        print("PASSED: read_batch handles missing data field correctly")

    # Test empty result handling
    async def mock_execute_request_empty(endpoint, payload):
        return []

    api._execute_request = mock_execute_request_empty

    result = await api.read_batch(inverter_sn, [103, 633])
    assert result == {}, f"Expected empty dict for empty response, got {result}"
    print("PASSED: read_batch handles empty response correctly")

    # Test retry logic - API fails first 2 times then succeeds
    mock_time[0] = 0  # Reset mock time
    call_count = [0]

    def mock_monotonic_gradual():
        """Mock monotonic that advances time gradually"""
        mock_time[0] += 0.1  # Advance by 0.1 seconds to stay within max_retry_time
        return mock_time[0]

    time.monotonic = mock_monotonic_gradual

    async def mock_execute_request_with_retries(endpoint, payload):
        call_count[0] += 1
        if call_count[0] <= 2:
            # First 2 calls fail
            from solis import SolisAPIError

            raise SolisAPIError(f"Batch API error on attempt {call_count[0]}")
        else:
            # Third call succeeds
            return [[{"cid": "103", "msg": "50"}, {"cid": "633", "msg": "33"}]]

    api._execute_request = mock_execute_request_with_retries

    result = await api.read_batch(inverter_sn, [103, 633])
    assert result[103] == "50", f"Expected '50' after retries, got '{result.get(103)}'"
    assert result[633] == "33", f"Expected '33' after retries, got '{result.get(633)}'"
    assert call_count[0] == 3, f"Expected 3 API calls (2 failures + 1 success), got {call_count[0]}"
    print("PASSED: read_batch retries after API failures and eventually succeeds")

    # Restore original sleep and monotonic
    asyncio.sleep = original_sleep
    time.monotonic = original_monotonic

    return False


async def test_read_and_write_cid():
    """Test read_and_write_cid method reads, verifies, and writes CID values"""
    print("\n=== Test: read_and_write_cid ===")

    api = MockSolisAPI()
    inverter_sn = "TEST123456"

    # Track calls to read_cid and write_cid
    read_calls = []
    write_calls = []

    async def mock_read_cid(inv_sn, cid):
        read_calls.append({"inverter_sn": inv_sn, "cid": cid})
        # Return current value
        if cid == 103:
            return "50"  # Current battery SOC
        elif cid == 633:
            return "33"  # Current storage mode
        else:
            return "0"

    async def mock_write_cid(inv_sn, cid, value, old_value=None, field_description=None):
        write_calls.append({"inverter_sn": inv_sn, "cid": cid, "value": value, "old_value": old_value, "field_description": field_description})
        return True

    # Replace methods with mocks - need to use real read_and_write_cid from SolisAPI
    original_read_cid = api.read_cid
    original_write_cid = api.write_cid
    original_read_and_write_cid = api.read_and_write_cid
    api.read_cid = mock_read_cid
    api.write_cid = mock_write_cid
    # Use the real implementation from SolisAPI parent class
    from solis import SolisAPI

    api.read_and_write_cid = SolisAPI.read_and_write_cid.__get__(api, MockSolisAPI)

    # Test 1: Value changes - should read then write
    result = await api.read_and_write_cid(inverter_sn, 103, "80", field_description="battery SOC")
    assert result == True, "Expected True for successful write"
    assert len(read_calls) == 1, f"Expected 1 read call, got {len(read_calls)}"
    assert read_calls[0]["cid"] == 103, f"Expected read CID 103, got {read_calls[0]['cid']}"
    assert len(write_calls) == 1, f"Expected 1 write call, got {len(write_calls)}"
    assert write_calls[0]["cid"] == 103, f"Expected write CID 103, got {write_calls[0]['cid']}"
    assert write_calls[0]["value"] == "80", f"Expected write value '80', got '{write_calls[0]['value']}'"
    assert write_calls[0]["old_value"] == "50", f"Expected old_value '50', got '{write_calls[0]['old_value']}'"
    assert write_calls[0]["field_description"] == "battery SOC", "Expected field_description to be passed"
    print("PASSED: read_and_write_cid reads current value then writes new value")

    # Test 2: Value unchanged - should read but not write
    read_calls.clear()
    write_calls.clear()
    result = await api.read_and_write_cid(inverter_sn, 633, "33", field_description="storage mode")
    assert result == True, "Expected True when value unchanged"
    assert len(read_calls) == 1, f"Expected 1 read call, got {len(read_calls)}"
    assert len(write_calls) == 0, f"Expected 0 write calls when value unchanged, got {len(write_calls)}"
    print("PASSED: read_and_write_cid skips write when value already matches")

    # Test 3: Write without field_description
    read_calls.clear()
    write_calls.clear()
    result = await api.read_and_write_cid(inverter_sn, 104, "100")
    assert result == True, "Expected True for successful write"
    assert len(read_calls) == 1, f"Expected 1 read call, got {len(read_calls)}"
    assert len(write_calls) == 1, f"Expected 1 write call, got {len(write_calls)}"
    assert write_calls[0]["field_description"] is None, "Expected no field_description when not provided"
    print("PASSED: read_and_write_cid works without field_description")

    # Test 4: Read failure - should return False and log warning
    read_calls.clear()
    write_calls.clear()

    async def mock_read_cid_fail(inv_sn, cid):
        from solis import SolisAPIError

        raise SolisAPIError("Read failed")

    api.read_cid = mock_read_cid_fail

    result = await api.read_and_write_cid(inverter_sn, 105, "50", field_description="test field")
    assert result == False, "Expected False when read fails"
    assert len(write_calls) == 0, f"Expected 0 write calls when read fails, got {len(write_calls)}"
    # Check that warning was logged
    assert any("Failed to read and set test field" in msg for msg in api.log_messages), "Expected warning log message"
    print("PASSED: read_and_write_cid returns False and logs warning on read failure")

    # Test 5: Write failure - should return False
    read_calls.clear()
    write_calls.clear()
    api.log_messages.clear()
    api.read_cid = mock_read_cid  # Restore successful read

    async def mock_write_cid_fail(inv_sn, cid, value, old_value=None, field_description=None):
        return False  # Write fails

    api.write_cid = mock_write_cid_fail

    result = await api.read_and_write_cid(inverter_sn, 106, "60")
    assert result == False, "Expected False when write fails"
    print("PASSED: read_and_write_cid returns False when write fails")

    # Restore original methods
    api.read_cid = original_read_cid
    api.write_cid = original_write_cid
    api.read_and_write_cid = original_read_and_write_cid

    return False


async def test_write_cid():
    """Test write_cid method writes CID values with optional old_value verification"""
    print("\n=== Test: write_cid ===")

    api = MockSolisAPI()
    inverter_sn = "TEST123456"

    # Track _execute_request calls
    execute_calls = []

    async def mock_execute_request_success(endpoint, payload):
        execute_calls.append({"endpoint": endpoint, "payload": payload})
        # Verify expected endpoint
        assert endpoint == "/v2/api/control", f"Expected /v2/api/control endpoint, got {endpoint}"

        # Return success response
        return [{"code": "0", "msg": "Success"}]

    api._execute_request = mock_execute_request_success

    # Test 1: Write without old_value verification
    result = await api.write_cid(inverter_sn, 103, "80", field_description="battery SOC")
    assert result == True, "Expected True for successful write"
    assert len(execute_calls) == 1, f"Expected 1 API call, got {len(execute_calls)}"
    assert execute_calls[0]["payload"]["inverterSn"] == inverter_sn, "Expected correct inverter SN"
    assert execute_calls[0]["payload"]["cid"] == 103, "Expected correct CID"
    assert execute_calls[0]["payload"]["value"] == "80", "Expected correct value"
    assert "yuanzhi" not in execute_calls[0]["payload"], "Expected no yuanzhi (old_value) when not provided"
    assert any("Set battery SOC on TEST123456" in msg for msg in api.log_messages), "Expected success log with field description"
    print("PASSED: write_cid writes value without old_value verification")

    # Test 2: Write with old_value verification
    execute_calls.clear()
    api.log_messages.clear()
    result = await api.write_cid(inverter_sn, 633, "35", old_value="33", field_description="storage mode")
    assert result == True, "Expected True for successful write"
    assert len(execute_calls) == 1, f"Expected 1 API call, got {len(execute_calls)}"
    assert execute_calls[0]["payload"]["yuanzhi"] == "33", "Expected old_value in yuanzhi field"
    assert execute_calls[0]["payload"]["value"] == "35", "Expected new value"
    print("PASSED: write_cid writes value with old_value verification")

    # Test 3: Write without field_description
    execute_calls.clear()
    api.log_messages.clear()
    result = await api.write_cid(inverter_sn, 104, "100")
    assert result == True, "Expected True for successful write"
    assert any(f"Set CID 104 to 100 on {inverter_sn}" in msg for msg in api.log_messages), "Expected success log without field description"
    print("PASSED: write_cid works without field_description")

    # Mock time for retry testing (do this before tests that cause retries)
    import asyncio
    import time

    original_sleep = asyncio.sleep
    original_monotonic = time.monotonic

    mock_time = [0]

    def mock_monotonic():
        mock_time[0] += 100  # Advance rapidly to exceed max_retry_time
        return mock_time[0]

    async def mock_sleep(delay):
        pass

    asyncio.sleep = mock_sleep
    time.monotonic = mock_monotonic

    # Test 4: API returns error code
    execute_calls.clear()
    api.log_messages.clear()

    async def mock_execute_request_error_code(endpoint, payload):
        # Return error response with non-zero code
        return [{"code": "1", "msg": "Invalid value"}]

    api._execute_request = mock_execute_request_error_code

    result = await api.write_cid(inverter_sn, 105, "50", field_description="test field")
    assert result == False, "Expected False when API returns error code"
    assert any("Failed to set test field" in msg for msg in api.log_messages), "Expected warning log on failure"
    print("PASSED: write_cid handles API error codes correctly")

    # Test 5: API returns missing data field
    execute_calls.clear()
    api.log_messages.clear()
    mock_time[0] = 0  # Reset time

    async def mock_execute_request_no_data(endpoint, payload):
        return None

    api._execute_request = mock_execute_request_no_data

    result = await api.write_cid(inverter_sn, 106, "60")
    assert result == False, "Expected False when API returns None data"
    print("PASSED: write_cid handles missing data field correctly")

    # Test 6: API returns non-array data
    execute_calls.clear()
    api.log_messages.clear()
    mock_time[0] = 0  # Reset time

    async def mock_execute_request_bad_format(endpoint, payload):
        return "not an array"

    api._execute_request = mock_execute_request_bad_format

    result = await api.write_cid(inverter_sn, 107, "70")
    assert result == False, "Expected False when API returns non-array data"
    print("PASSED: write_cid handles non-array data field correctly")

    # Test 7: Retry logic - fails twice then succeeds
    execute_calls.clear()
    api.log_messages.clear()
    mock_time[0] = 0  # Reset time
    call_count = [0]

    # Create new mock_monotonic for gradual time advance
    gradual_time = [0]

    def mock_monotonic_gradual():
        gradual_time[0] += 0.1
        return gradual_time[0]

    # Replace monotonic with gradual version
    time.monotonic = mock_monotonic_gradual

    async def mock_execute_request_retry(endpoint, payload):
        call_count[0] += 1
        if call_count[0] < 3:
            from solis import SolisAPIError

            raise SolisAPIError(f"API error on attempt {call_count[0]}")
        # Third attempt succeeds
        return [{"code": "0", "msg": "Success"}]

    api._execute_request = mock_execute_request_retry

    result = await api.write_cid(inverter_sn, 108, "80", field_description="retry test")
    assert result == True, f"Expected True after retries succeed, got {result}"
    assert call_count[0] == 3, f"Expected 3 attempts, got {call_count[0]}"
    assert any("retry 1" in msg for msg in api.log_messages), "Expected retry warning for attempt 1"
    assert any("retry 2" in msg for msg in api.log_messages), "Expected retry warning for attempt 2"
    print("PASSED: write_cid retries after failures and eventually succeeds")

    # Test 8: Cache is updated on success
    api.cached_values = {}  # Clear cache
    api._execute_request = mock_execute_request_success

    result = await api.write_cid(inverter_sn, 109, "90")
    assert result == True, "Expected True for successful write"
    assert inverter_sn in api.cached_values, "Expected inverter in cache"
    assert 109 in api.cached_values[inverter_sn], "Expected CID in cache"
    assert api.cached_values[inverter_sn][109] == "90", "Expected cached value to match written value"
    print("PASSED: write_cid updates cache on success")

    # Restore original sleep and monotonic
    asyncio.sleep = original_sleep
    time.monotonic = original_monotonic

    return False


async def test_decode_time_windows_v2():
    """Test decode_time_windows_v2 reads V2 split registers and decodes correctly"""
    print("\n=== Test: decode_time_windows_v2 ===")

    api = MockSolisAPI()
    inverter_sn = "TEST123456"

    # Set up cache with V2 time window data for 6 slots
    # Slot 1: Full data with charging and discharging
    api.cached_values[inverter_sn] = {
        # Slot 1
        5948: "62",  # charge current
        5967: "45",  # discharge current
        5946: "02:00-05:30",  # charge time
        5964: "16:00-19:00",  # discharge time
        5928: "95",  # charge SOC (base + 0)
        5965: "10",  # discharge SOC
        5916: "1",  # charge enable (base + 0)
        5922: "1",  # discharge enable (base + 0)
        # Slot 2: Partial data with charge only
        5951: "30",  # charge current
        5971: "0",  # discharge current
        5949: "23:00-01:00",  # charge time
        5968: "00:00-00:00",  # discharge time (disabled)
        5929: "80",  # charge SOC (base + 1)
        5969: "0",  # discharge SOC
        5917: "1",  # charge enable (base + 1)
        5923: "0",  # discharge enable (base + 1)
        # Slot 3: Empty/disabled slot
        5954: "0",  # charge current
        5975: "0",  # discharge current
        5952: "00:00-00:00",  # charge time
        5972: "00:00-00:00",  # discharge time
        5930: "0",  # charge SOC (base + 2)
        5973: "0",  # discharge SOC
        5918: "0",  # charge enable (base + 2)
        5924: "0",  # discharge enable (base + 2)
        # Slots 4-6: Similar to slot 3 (disabled)
        5957: "0",
        5979: "0",
        5955: "00:00-00:00",
        5976: "00:00-00:00",
        5931: "0",
        5977: "0",
        5919: "0",
        5925: "0",
        5960: "0",
        5983: "0",
        5958: "00:00-00:00",
        5980: "00:00-00:00",
        5932: "0",
        5981: "0",
        5920: "0",
        5926: "0",
        5963: "0",
        5986: "0",
        5961: "00:00-00:00",
        5987: "00:00-00:00",
        5933: "0",
        5984: "0",
        5921: "0",
        5927: "0",
    }

    # Decode the time windows
    result = await api.decode_time_windows_v2(inverter_sn)

    # Verify result structure
    assert result is not None, "Expected result dict"
    assert len(result) == 6, f"Expected 6 slots, got {len(result)}"

    # Test Slot 1 - full data
    slot1 = result[1]
    assert slot1["charge_current"] == 62.0, f"Expected charge_current 62.0, got {slot1['charge_current']}"
    assert slot1["discharge_current"] == 45.0, f"Expected discharge_current 45.0, got {slot1['discharge_current']}"
    assert slot1["charge_start_time"] == "02:00", f"Expected charge_start_time '02:00', got '{slot1['charge_start_time']}'"
    assert slot1["charge_end_time"] == "05:30", f"Expected charge_end_time '05:30', got '{slot1['charge_end_time']}'"
    assert slot1["discharge_start_time"] == "16:00", f"Expected discharge_start_time '16:00', got '{slot1['discharge_start_time']}'"
    assert slot1["discharge_end_time"] == "19:00", f"Expected discharge_end_time '19:00', got '{slot1['discharge_end_time']}'"
    assert slot1["charge_soc"] == 95.0, f"Expected charge_soc 95.0, got {slot1['charge_soc']}"
    assert slot1["discharge_soc"] == 10.0, f"Expected discharge_soc 10.0, got {slot1['discharge_soc']}"
    assert slot1["charge_enable"] == 1, f"Expected charge_enable 1, got {slot1['charge_enable']}"
    assert slot1["discharge_enable"] == 1, f"Expected discharge_enable 1, got {slot1['discharge_enable']}"
    assert slot1["field_length"] == 0, "Expected field_length 0 for V2 format"
    print("PASSED: decode_time_windows_v2 correctly decodes slot 1 with full data")

    # Test Slot 2 - charge only
    slot2 = result[2]
    assert slot2["charge_current"] == 30.0, f"Expected charge_current 30.0, got {slot2['charge_current']}"
    assert slot2["discharge_current"] == 0.0, f"Expected discharge_current 0.0, got {slot2['discharge_current']}"
    assert slot2["charge_start_time"] == "23:00", f"Expected charge_start_time '23:00', got '{slot2['charge_start_time']}'"
    assert slot2["charge_end_time"] == "01:00", f"Expected charge_end_time '01:00', got '{slot2['charge_end_time']}'"
    assert slot2["discharge_start_time"] == "00:00", f"Expected discharge_start_time '00:00', got '{slot2['discharge_start_time']}'"
    assert slot2["discharge_end_time"] == "00:00", f"Expected discharge_end_time '00:00', got '{slot2['discharge_end_time']}'"
    assert slot2["charge_soc"] == 80.0, f"Expected charge_soc 80.0, got {slot2['charge_soc']}"
    assert slot2["charge_enable"] == 1, f"Expected charge_enable 1, got {slot2['charge_enable']}"
    assert slot2["discharge_enable"] == 0, f"Expected discharge_enable 0, got {slot2['discharge_enable']}"
    print("PASSED: decode_time_windows_v2 correctly decodes slot 2 with charge only")

    # Test Slot 3 - disabled slot
    slot3 = result[3]
    assert slot3["charge_current"] == 0.0, f"Expected charge_current 0.0, got {slot3['charge_current']}"
    assert slot3["discharge_current"] == 0.0, f"Expected discharge_current 0.0, got {slot3['discharge_current']}"
    assert slot3["charge_enable"] == 0, f"Expected charge_enable 0, got {slot3['charge_enable']}"
    assert slot3["discharge_enable"] == 0, f"Expected discharge_enable 0, got {slot3['discharge_enable']}"
    print("PASSED: decode_time_windows_v2 correctly decodes disabled slot 3")

    # Verify all slots exist
    for slot_num in range(1, 7):
        assert slot_num in result, f"Expected slot {slot_num} in result"
    print("PASSED: decode_time_windows_v2 creates all 6 slots")

    # Verify data is stored in charge_discharge_time_windows
    assert inverter_sn in api.charge_discharge_time_windows, "Expected inverter_sn in charge_discharge_time_windows"
    assert api.charge_discharge_time_windows[inverter_sn] == result, "Expected stored data to match result"
    print("PASSED: decode_time_windows_v2 stores data in charge_discharge_time_windows")

    # Test with missing cache data (defaults to 0)
    api2 = MockSolisAPI()
    inverter_sn2 = "TEST789"
    api2.cached_values[inverter_sn2] = {}  # Empty cache

    result2 = await api2.decode_time_windows_v2(inverter_sn2)

    # Should return all slots with default values
    assert len(result2) == 6, f"Expected 6 slots with defaults, got {len(result2)}"
    slot1_defaults = result2[1]
    assert slot1_defaults["charge_current"] == 0.0, "Expected default charge_current 0.0"
    assert slot1_defaults["discharge_current"] == 0.0, "Expected default discharge_current 0.0"
    assert slot1_defaults["charge_start_time"] == "00:00", "Expected default charge_start_time '00:00'"
    assert slot1_defaults["charge_end_time"] == "00:00", "Expected default charge_end_time '00:00'"
    assert slot1_defaults["charge_soc"] == 0.0, "Expected default charge_soc 0.0"
    assert slot1_defaults["discharge_soc"] == 0.0, "Expected default discharge_soc 0.0"
    assert slot1_defaults["charge_enable"] == 0, "Expected default charge_enable 0"
    assert slot1_defaults["discharge_enable"] == 0, "Expected default discharge_enable 0"
    print("PASSED: decode_time_windows_v2 handles missing cache with defaults")

    # Test with malformed time format (no dash)
    api3 = MockSolisAPI()
    inverter_sn3 = "TEST999"
    api3.cached_values[inverter_sn3] = {
        5948: "50",
        5967: "40",
        5946: "02:00",  # Missing dash separator
        5964: "16:00",  # Missing dash separator
        5928: "90",
        5965: "15",
        5916: "1",
        5922: "1",
    }

    result3 = await api3.decode_time_windows_v2(inverter_sn3)
    slot1_malformed = result3[1]
    # When there's no dash, the split returns single element list, so start = first element, end = default 00:00
    assert slot1_malformed["charge_start_time"] == "00:00", f"Expected '00:00' when no dash, got '{slot1_malformed['charge_start_time']}'"
    assert slot1_malformed["charge_end_time"] == "00:00", f"Expected '00:00' when no dash, got '{slot1_malformed['charge_end_time']}'"
    assert slot1_malformed["discharge_start_time"] == "00:00", f"Expected '00:00' when no dash, got '{slot1_malformed['discharge_start_time']}'"
    assert slot1_malformed["discharge_end_time"] == "00:00", f"Expected '00:00' when no dash, got '{slot1_malformed['discharge_end_time']}'"
    print("PASSED: decode_time_windows_v2 handles malformed time format")

    return False


async def test_write_time_windows_v2_mode():
    """Test write_time_windows_if_changed in V2 mode"""
    print("\n=== Test: write_time_windows_if_changed V2 mode ===")

    api = MockSolisAPI()
    api._test_v2_mode = True  # Enable V2 mode
    api._mock_storage_mode = True  # Mock storage mode tracking
    inverter_sn = "TEST123"
    api.inverter_sn = [inverter_sn]

    # Setup time windows with all fields for V2 mode
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 1,
            "charge_start_time": "02:00",
            "charge_end_time": "05:00",
            "charge_soc": 100,
            "charge_current": 50,
            "discharge_enable": 0,
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "discharge_soc": 10,
            "discharge_current": 30,
        }
    }

    # Initialize cache to empty (no previous values)
    api.cached_values[inverter_sn] = {}

    # Call the function
    result = await api.write_time_windows_if_changed(inverter_sn)

    # Verify results
    assert result == True, "write_time_windows_if_changed should return True"

    # Check that read_and_write_cid was called for all V2 fields
    calls = api.read_and_write_cid_calls
    # Should write: charge_enable, charge_time, charge_soc, charge_current, discharge_enable, discharge_time, discharge_soc, discharge_current
    # = 8 calls (not 10, since we only have discharge enable/time/soc/current, not a separate enable write)
    assert len(calls) == 8, f"Expected 8 calls for V2 mode (4 charge + 4 discharge fields), got {len(calls)}"

    # Verify charge enable was written
    charge_enable_call = next((c for c in calls if c["cid"] == SOLIS_CID_CHARGE_ENABLE_BASE), None)
    assert charge_enable_call is not None, "Charge enable should be written"
    assert charge_enable_call["value"] == "1", "Charge enable should be 1"

    # Verify charge time was written
    charge_time_call = next((c for c in calls if c["cid"] == SOLIS_CID_CHARGE_TIME[0]), None)
    assert charge_time_call is not None, "Charge time should be written"
    assert charge_time_call["value"] == "02:00-05:00", "Charge time should be 02:00-05:00"

    # Verify charge SOC was written
    charge_soc_call = next((c for c in calls if c["cid"] == SOLIS_CID_CHARGE_SOC_BASE), None)
    assert charge_soc_call is not None, "Charge SOC should be written"
    assert charge_soc_call["value"] == "100", "Charge SOC should be 100"

    # Verify charge current was written
    charge_current_call = next((c for c in calls if c["cid"] == SOLIS_CID_CHARGE_CURRENT[0]), None)
    assert charge_current_call is not None, "Charge current should be written"
    assert charge_current_call["value"] == "50", "Charge current should be 50"

    # Verify storage mode was set to Self-Use (non-zero charge current)
    storage_mode_calls = api.set_storage_mode_calls
    assert len(storage_mode_calls) == 1, f"Expected 1 storage mode call, got {len(storage_mode_calls)}"
    assert storage_mode_calls[0]["mode"] == "Self-Use", "Storage mode should be Self-Use for non-zero charge current"

    print("PASSED: V2 mode writes all fields correctly and sets storage mode")
    return False


async def test_write_time_windows_v1_mode():
    """Test write_time_windows_if_changed in V1 mode"""
    print("\n=== Test: write_time_windows_if_changed V1 mode ===")

    api = MockSolisAPI()
    api._test_v2_mode = False  # V1 mode
    api._mock_storage_mode = True  # Mock storage mode tracking
    inverter_sn = "TEST123"
    api.inverter_sn = [inverter_sn]

    # Setup time windows for V1 mode
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_start_time": "02:00",
            "charge_end_time": "05:00",
            "charge_soc": 100,
            "charge_current": 50,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
            "field_length": 18,
        },
        2: {
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
            "field_length": 18,
        },
        3: {
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
            "field_length": 18,
        },
    }

    # Initialize cache with different values to force writes
    api.cached_values[inverter_sn] = {SOLIS_CID_CHARGE_DISCHARGE_SETTINGS: "50,50,00:00,00:00,00:00,00:00,0,0,00:00,00:00,00:00,00:00,0,0,00:00,00:00,00:00,00:00"}  # Different

    # Call the function
    result = await api.write_time_windows_if_changed(inverter_sn)

    # Verify results
    assert result == True, "write_time_windows_if_changed should return True"

    # Check that read_and_write_cid was called for CID 103 only
    # V1 mode no longer writes global SOC values - it only encodes and writes CID 103
    calls = api.read_and_write_cid_calls
    assert len(calls) == 1, f"Expected 1 call for V1 mode (CID 103 only), got {len(calls)}"

    # Verify CID 103 was written
    cid_103_call = next((c for c in calls if c["cid"] == SOLIS_CID_CHARGE_DISCHARGE_SETTINGS), None)
    assert cid_103_call is not None, "CID 103 should be written"

    # Verify the encoded value looks reasonable (should contain time data)
    encoded_value = cid_103_call["value"]
    assert "," in encoded_value, "Encoded value should contain commas"
    assert "00:00" in encoded_value, "Encoded value should contain time values"

    # Verify storage mode was set to Self-Use - No Timed Charge/Discharge (outside of charge slots)
    storage_mode_calls = api.set_storage_mode_calls
    assert len(storage_mode_calls) == 1, f"Expected 1 storage mode call, got {len(storage_mode_calls)}"
    assert storage_mode_calls[0]["mode"] == "Self-Use - No Timed Charge/Discharge", "Storage mode should be Self-Use - No Timed Charge/Discharge when outside charge slots"

    print("PASSED: V1 mode writes CID 103 and sets storage mode")
    return False


async def test_write_time_windows_v2_no_changes():
    """Test write_time_windows_if_changed in V2 mode with no changes"""
    print("\n=== Test: write_time_windows_if_changed V2 mode no changes ===")

    api = MockSolisAPI()
    api._test_v2_mode = True  # Enable V2 mode
    api._mock_storage_mode = True  # Mock storage mode tracking
    inverter_sn = "TEST123"
    api.inverter_sn = [inverter_sn]

    # Setup time windows
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 1,
            "charge_start_time": "02:00",
            "charge_end_time": "05:00",
            "charge_soc": 100,
            "charge_current": 50,
        }
    }

    # Initialize cache with same values (no changes)
    api.cached_values[inverter_sn] = {
        SOLIS_CID_CHARGE_ENABLE_BASE: "1",
        SOLIS_CID_CHARGE_TIME[0]: "02:00-05:00",
        SOLIS_CID_CHARGE_SOC_BASE: "100",
        SOLIS_CID_CHARGE_CURRENT[0]: "50",
    }

    # Call the function
    result = await api.write_time_windows_if_changed(inverter_sn)

    # Verify results
    assert result == True, "write_time_windows_if_changed should return True"

    # Check that read_and_write_cid was NOT called (no changes)
    calls = api.read_and_write_cid_calls
    assert len(calls) == 0, f"Expected 0 calls when no changes, got {len(calls)}"

    # Verify storage mode was still set (always called)
    storage_mode_calls = api.set_storage_mode_calls
    assert len(storage_mode_calls) == 1, f"Expected 1 storage mode call, got {len(storage_mode_calls)}"
    assert storage_mode_calls[0]["mode"] == "Self-Use", "Storage mode should be Self-Use for non-zero charge current"

    print("PASSED: V2 mode skips writes when no changes (but sets storage mode)")
    return False


async def test_write_time_windows_zero_charge_current():
    """Test write_time_windows_if_changed with zero charge current sets Feed-in priority"""
    print("\n=== Test: write_time_windows_if_changed zero charge current ===")

    api = MockSolisAPI()
    api._test_v2_mode = True  # V2 mode
    api._mock_storage_mode = True  # Mock storage mode tracking
    inverter_sn = "TEST123"
    api.inverter_sn = [inverter_sn]

    # Setup time windows with ZERO charge current
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,  # Zero charge current
            "discharge_enable": 1,
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "discharge_soc": 10,
            "discharge_current": 30,
        }
    }

    # Initialize cache
    api.cached_values[inverter_sn] = {}

    # Call the function
    result = await api.write_time_windows_if_changed(inverter_sn)

    # Verify results
    assert result == True, "write_time_windows_if_changed should return True"

    # Verify storage mode was set to Feed-in priority (zero charge current)
    storage_mode_calls = api.set_storage_mode_calls
    assert len(storage_mode_calls) == 1, f"Expected 1 storage mode call, got {len(storage_mode_calls)}"
    assert storage_mode_calls[0]["mode"] == "Feed-in priority", "Storage mode should be Feed-in priority for zero charge current"
    assert storage_mode_calls[0]["inverter_sn"] == inverter_sn, "Storage mode should be set for correct inverter"

    print("PASSED: Zero charge current sets Feed-in priority storage mode")
    return False


async def test_write_time_windows_v1_slot_detection():
    """Test V1 mode slot detection for active charge/discharge windows"""
    print("\n=== Test: write_time_windows_if_changed V1 mode slot detection ===")

    api = MockSolisAPI()
    api._test_v2_mode = False  # V1 mode
    api._mock_storage_mode = True  # Mock storage mode tracking
    inverter_sn = "TEST123"
    api.inverter_sn = [inverter_sn]

    # Setup time windows with charge slot 1 active from 02:00-05:00
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_start_time": "02:00",
            "charge_end_time": "05:00",
            "charge_soc": 95,
            "charge_current": 50,
            "charge_enable": 1,
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "discharge_soc": 15,
            "discharge_current": 30,
            "discharge_enable": 1,
            "field_length": 18,
        },
        2: {
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
            "field_length": 18,
        },
    }

    # Initialize cache
    api.cached_values[inverter_sn] = {}

    # Mock current time to be during charge slot (03:00)
    with patch("solis.datetime") as mock_datetime:
        mock_now = MagicMock()
        mock_now.strftime.return_value = "03:00"
        mock_datetime.now.return_value = mock_now
        mock_datetime.UTC = timezone.utc

        # Call the function
        result = await api.write_time_windows_if_changed(inverter_sn)

    # Verify results
    assert result == True, "write_time_windows_if_changed should return True"

    # Check that charge_soc_to_write was detected and logged
    charge_log = any("In charge slot" in msg and "95%" in msg for msg in api.log_messages)
    assert charge_log, "Should log that we're in charge slot with target SOC"

    print("PASSED: V1 mode detects active time slots correctly")
    return False


async def test_encode_time_windows_variant1():
    """Test encoding time windows in variant 1 format (18 fields, 6 per slot)"""
    print("\n=== Test: encode_time_windows variant 1 ===")

    api = MockSolisAPI()
    inverter_sn = "TEST123"

    # Setup time windows with variant 1 format (18 fields)
    time_windows = {
        1: {
            "charge_current": 62,
            "discharge_current": 45,
            "charge_start_time": "02:00",
            "charge_end_time": "05:30",
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "field_length": 18,
        },
        2: {
            "charge_current": 30,
            "discharge_current": 20,
            "charge_start_time": "23:00",
            "charge_end_time": "01:00",
            "discharge_start_time": "12:00",
            "discharge_end_time": "14:00",
            "field_length": 18,
        },
        3: {
            "charge_current": 0,
            "discharge_current": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "field_length": 18,
        },
    }

    # Call encode_time_windows
    result = await api.encode_time_windows(inverter_sn, time_windows)

    # Expected format: charge_current, discharge_current, charge_start, charge_end, discharge_start, discharge_end (repeated 3 times)
    expected = "62,45,02:00,05:30,16:00,19:00,30,20,23:00,01:00,12:00,14:00,0,0,00:00,00:00,00:00,00:00"

    assert result == expected, f"Expected '{expected}', got '{result}'"

    # Verify log message
    log_msg = any("Encoding time windows variant 1 (18 fields)" in msg for msg in api.log_messages)
    assert log_msg, "Should log variant 1 encoding"

    print("PASSED: Variant 1 encoding works correctly")
    return False


async def test_encode_time_windows_variant2():
    """Test encoding time windows in variant 2 format (12 fields, 4 per slot)"""
    print("\n=== Test: encode_time_windows variant 2 ===")

    api = MockSolisAPI()
    inverter_sn = "TEST456"

    # Setup time windows with variant 2 format (12 fields)
    time_windows = {
        1: {
            "charge_current": 62,
            "discharge_current": 45,
            "charge_start_time": "02:00",
            "charge_end_time": "05:30",
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "field_length": 12,
        },
        2: {
            "charge_current": 30,
            "discharge_current": 20,
            "charge_start_time": "23:00",
            "charge_end_time": "01:00",
            "discharge_start_time": "12:00",
            "discharge_end_time": "14:00",
            "field_length": 12,
        },
        3: {
            "charge_current": 0,
            "discharge_current": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "field_length": 12,
        },
    }

    # Call encode_time_windows
    result = await api.encode_time_windows(inverter_sn, time_windows)

    # Expected format: charge_current, discharge_current, charge_time_slot, discharge_time_slot (repeated 3 times)
    expected = "62,45,02:00-05:30,16:00-19:00,30,20,23:00-01:00,12:00-14:00,0,0,00:00-00:00,00:00-00:00"

    assert result == expected, f"Expected '{expected}', got '{result}'"

    # Verify log message
    log_msg = any("Encoding time windows variant 2 (12 fields)" in msg for msg in api.log_messages)
    assert log_msg, "Should log variant 2 encoding"

    print("PASSED: Variant 2 encoding works correctly")
    return False


async def test_encode_time_windows_empty():
    """Test encoding with empty time windows"""
    print("\n=== Test: encode_time_windows empty input ===")

    api = MockSolisAPI()
    inverter_sn = "TEST789"

    # Call with empty dict
    result = await api.encode_time_windows(inverter_sn, {})

    # Should return None and log warning
    assert result is None, "Should return None for empty time_windows"

    warn_msg = any("No time windows data provided" in msg for msg in api.log_messages)
    assert warn_msg, "Should log warning for empty time_windows"

    print("PASSED: Empty time windows handled correctly")
    return False


async def test_encode_time_windows_defaults():
    """Test encoding with missing fields uses defaults"""
    print("\n=== Test: encode_time_windows with defaults ===")

    api = MockSolisAPI()
    inverter_sn = "TEST999"

    # Setup time windows with only field_length, all other fields should use defaults
    time_windows = {
        1: {
            "field_length": 18,
        },
        2: {
            "charge_current": 50,  # Only some fields
            "field_length": 18,
        },
        3: {
            "field_length": 18,
        },
    }

    # Call encode_time_windows
    result = await api.encode_time_windows(inverter_sn, time_windows)

    # Expected: all missing values default to 0 for current, "00:00" for times
    expected = "0,0,00:00,00:00,00:00,00:00,50,0,00:00,00:00,00:00,00:00,0,0,00:00,00:00,00:00,00:00"

    assert result == expected, f"Expected '{expected}', got '{result}'"

    print("PASSED: Default values handled correctly")
    return False


async def test_encode_decode_roundtrip_variant1():
    """Test that encode -> decode -> encode produces the same result (variant 1)"""
    print("\n=== Test: encode/decode roundtrip variant 1 ===")

    api = MockSolisAPI()
    inverter_sn = "ROUNDTRIP1"

    # Original time windows with variant 1 format
    original_windows = {
        1: {
            "charge_current": 62,
            "discharge_current": 45,
            "charge_start_time": "02:00",
            "charge_end_time": "05:30",
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "field_length": 18,
        },
        2: {
            "charge_current": 30,
            "discharge_current": 20,
            "charge_start_time": "23:00",
            "charge_end_time": "01:00",
            "discharge_start_time": "12:00",
            "discharge_end_time": "14:00",
            "field_length": 18,
        },
        3: {
            "charge_current": 0,
            "discharge_current": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "field_length": 18,
        },
    }

    # Encode the original windows
    encoded1 = await api.encode_time_windows(inverter_sn, original_windows)
    assert encoded1 is not None, "First encoding should succeed"

    # Store encoded result in cache (decode_time_windows reads from cache)
    api.cached_values[inverter_sn] = {SOLIS_CID_CHARGE_DISCHARGE_SETTINGS: encoded1, SOLIS_CID_BATTERY_FORCE_CHARGE_SOC: "95", SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC: "10"}

    # Decode the encoded result
    decoded_windows = await api.decode_time_windows(inverter_sn)
    assert decoded_windows is not None, "Decoding should succeed"

    # Encode the decoded result
    encoded2 = await api.encode_time_windows(inverter_sn, decoded_windows)
    assert encoded2 is not None, "Second encoding should succeed"

    # Compare the two encoded strings
    assert encoded1 == encoded2, f"Roundtrip encoding mismatch:\n  Original: {encoded1}\n  Roundtrip: {encoded2}"

    print("PASSED: Variant 1 roundtrip encoding matches")
    return False


async def test_encode_decode_roundtrip_variant2():
    """Test that encode -> decode -> encode produces the same result (variant 2)"""
    print("\n=== Test: encode/decode roundtrip variant 2 ===")

    api = MockSolisAPI()
    inverter_sn = "ROUNDTRIP2"

    # Original time windows with variant 2 format
    original_windows = {
        1: {
            "charge_current": 62,
            "discharge_current": 45,
            "charge_start_time": "02:00",
            "charge_end_time": "05:30",
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "field_length": 12,
        },
        2: {
            "charge_current": 30,
            "discharge_current": 20,
            "charge_start_time": "23:00",
            "charge_end_time": "01:00",
            "discharge_start_time": "12:00",
            "discharge_end_time": "14:00",
            "field_length": 12,
        },
        3: {
            "charge_current": 0,
            "discharge_current": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "field_length": 12,
        },
    }

    # Encode the original windows
    encoded1 = await api.encode_time_windows(inverter_sn, original_windows)
    assert encoded1 is not None, "First encoding should succeed"

    # Store encoded result in cache (decode_time_windows reads from cache)
    api.cached_values[inverter_sn] = {SOLIS_CID_CHARGE_DISCHARGE_SETTINGS: encoded1, SOLIS_CID_BATTERY_FORCE_CHARGE_SOC: "95", SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC: "10"}

    # Decode the encoded result
    decoded_windows = await api.decode_time_windows(inverter_sn)
    assert decoded_windows is not None, "Decoding should succeed"

    # Encode the decoded result
    encoded2 = await api.encode_time_windows(inverter_sn, decoded_windows)
    assert encoded2 is not None, "Second encoding should succeed"

    # Compare the two encoded strings
    assert encoded1 == encoded2, f"Roundtrip encoding mismatch:\n  Original: {encoded1}\n  Roundtrip: {encoded2}"

    print("PASSED: Variant 2 roundtrip encoding matches")
    return False


async def test_publish_entities():
    """Test publish_entities creates all expected Home Assistant entities"""
    print("\n=== Test: publish_entities ===")

    api = MockSolisAPI()
    inverter_sn = "TEST_PUBLISH"
    api.inverter_sn = [inverter_sn]

    # Setup inverter details (from detail API)
    api.inverter_details[inverter_sn] = {
        "inverterName": "Test Inverter",
        "homeLoadTotalEnergy": 1234.5,
        "gridSellTodayEnergy": 12.3,
        "gridSellTodayEnergyStr": "kWh",
        "gridPurchasedTodayEnergy": 8.7,
        "gridPurchasedTodayEnergyStr": "kWh",
        "batteryCapacitySoc": 85,
        "maxChargePowerW": 5000,
        "eTotal": 9876.5,
        "eTotalStr": "kWh",
        "pac": 2.5,
        "pacStr": "kW",
        "productModel": "Solis-5G-Hybrid",
        "inverterTemperature": 35.2,
        "batteryPower": 1.5,
        "batteryPowerStr": "kW",
        "batteryVoltage": 52.3,
        "batteryVoltageStr": "V",
        "batteryCurrent": 28.7,
        "batteryCurrentStr": "A",
        "familyLoadPower": 3.2,
        "familyLoadPowerStr": "kW",
        "psum": -1.2,
        "psumStr": "kW",
    }

    # Setup cached CID values
    from solis import (
        SOLIS_CID_STORAGE_MODE,
        SOLIS_CID_ALLOW_EXPORT,
        SOLIS_CID_BATTERY_RESERVE_SOC,
        SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC,
        SOLIS_CID_BATTERY_FORCE_CHARGE_SOC,
        SOLIS_CID_BATTERY_RECOVERY_SOC,
        SOLIS_CID_BATTERY_MAX_CHARGE_SOC,
        SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT,
        SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT,
        SOLIS_CID_POWER_LIMIT,
        SOLIS_CID_MAX_OUTPUT_POWER,
        SOLIS_CID_MAX_EXPORT_POWER,
        SOLIS_CID_BATTERY_CAPACITY,
    )

    api.cached_values[inverter_sn] = {
        SOLIS_CID_STORAGE_MODE: "35",  # Self-use mode with TOU
        SOLIS_CID_ALLOW_EXPORT: "0",  # Export allowed
        SOLIS_CID_BATTERY_RESERVE_SOC: "10",
        SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC: "15",
        SOLIS_CID_BATTERY_FORCE_CHARGE_SOC: "95",
        SOLIS_CID_BATTERY_RECOVERY_SOC: "20",
        SOLIS_CID_BATTERY_MAX_CHARGE_SOC: "100",
        SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT: "50",
        SOLIS_CID_BATTERY_MAX_DISCHARGE_CURRENT: "50",
        SOLIS_CID_POWER_LIMIT: "100",
        SOLIS_CID_MAX_OUTPUT_POWER: "100",
        SOLIS_CID_MAX_EXPORT_POWER: "5000",
        SOLIS_CID_BATTERY_CAPACITY: "100",  # 100 Ah
    }

    # Setup time windows (slot 1 active, others disabled)
    api.charge_discharge_time_windows[inverter_sn] = {
        1: {
            "charge_enable": 1,
            "charge_start_time": "02:00",
            "charge_end_time": "05:00",
            "charge_soc": 95,
            "charge_current": 50,
            "discharge_enable": 1,
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "discharge_soc": 15,
            "discharge_current": 30,
        },
        2: {
            "charge_enable": 0,
            "charge_start_time": "00:00",
            "charge_end_time": "00:00",
            "charge_soc": 100,
            "charge_current": 0,
            "discharge_enable": 0,
            "discharge_start_time": "00:00",
            "discharge_end_time": "00:00",
            "discharge_soc": 10,
            "discharge_current": 0,
        },
    }

    # Setup max currents
    api.max_charge_current[inverter_sn] = 50
    api.max_discharge_current[inverter_sn] = 50

    # Call publish_entities
    await api.publish_entities()

    # Verify key entities were created
    prefix = api.prefix

    # Check some sensor entities
    assert f"sensor.{prefix}_solis_{inverter_sn}_battery_soc" in api.dashboard_items, "Battery SOC sensor should be published"
    battery_soc_item = api.dashboard_items[f"sensor.{prefix}_solis_{inverter_sn}_battery_soc"]
    assert battery_soc_item["state"] == 85, f"Battery SOC should be 85, got {battery_soc_item['state']}"
    assert battery_soc_item["attributes"]["unit_of_measurement"] == "%", "Battery SOC should have % unit"

    # Check charge slot 1 controls
    assert f"switch.{prefix}_solis_{inverter_sn}_charge_slot1_enable" in api.dashboard_items, "Charge slot 1 enable switch should be published"
    charge_enable = api.dashboard_items[f"switch.{prefix}_solis_{inverter_sn}_charge_slot1_enable"]
    assert charge_enable["state"] == "on", "Charge slot 1 should be enabled"

    assert f"select.{prefix}_solis_{inverter_sn}_charge_slot1_start_time" in api.dashboard_items, "Charge slot 1 start time should be published"
    charge_start = api.dashboard_items[f"select.{prefix}_solis_{inverter_sn}_charge_slot1_start_time"]
    assert charge_start["state"] == "02:00:00", f"Charge start time should be 02:00:00, got {charge_start['state']}"

    assert f"number.{prefix}_solis_{inverter_sn}_charge_slot1_soc" in api.dashboard_items, "Charge slot 1 SOC should be published"
    charge_soc = api.dashboard_items[f"number.{prefix}_solis_{inverter_sn}_charge_slot1_soc"]
    assert charge_soc["state"] == 95, f"Charge SOC should be 95, got {charge_soc['state']}"

    # Check power conversion (amps to watts)
    assert f"number.{prefix}_solis_{inverter_sn}_charge_slot1_power" in api.dashboard_items, "Charge slot 1 power should be published"
    charge_power = api.dashboard_items[f"number.{prefix}_solis_{inverter_sn}_charge_slot1_power"]
    expected_power = int(50 * api.nominal_voltage)  # 50A * 48.4V = 2420W
    assert charge_power["state"] == expected_power, f"Charge power should be {expected_power}W, got {charge_power['state']}"
    assert charge_power["attributes"]["unit_of_measurement"] == "W", "Charge power should have W unit"

    # Check discharge slot 1 controls
    assert f"switch.{prefix}_solis_{inverter_sn}_discharge_slot1_enable" in api.dashboard_items, "Discharge slot 1 enable switch should be published"
    discharge_enable = api.dashboard_items[f"switch.{prefix}_solis_{inverter_sn}_discharge_slot1_enable"]
    assert discharge_enable["state"] == "on", "Discharge slot 1 should be enabled"

    # Check storage mode selector
    assert f"select.{prefix}_solis_{inverter_sn}_storage_mode" in api.dashboard_items, "Storage mode selector should be published"

    # Check switches (battery reserve, grid charging, TOU, export)
    assert f"switch.{prefix}_solis_{inverter_sn}_battery_reserve" in api.dashboard_items, "Battery reserve switch should be published"
    assert f"switch.{prefix}_solis_{inverter_sn}_allow_grid_charging" in api.dashboard_items, "Grid charging switch should be published"
    assert f"switch.{prefix}_solis_{inverter_sn}_time_of_use" in api.dashboard_items, "TOU switch should be published"
    assert f"switch.{prefix}_solis_{inverter_sn}_allow_export" in api.dashboard_items, "Export switch should be published"

    # Check SOC limit numbers
    assert f"number.{prefix}_solis_{inverter_sn}_reserve_soc" in api.dashboard_items, "Reserve SOC should be published"
    reserve_soc = api.dashboard_items[f"number.{prefix}_solis_{inverter_sn}_reserve_soc"]
    assert reserve_soc["state"] == "10", f"Reserve SOC should be 10, got {reserve_soc['state']}"

    # Check max power numbers (converted from amps)
    assert f"number.{prefix}_solis_{inverter_sn}_max_charge_power" in api.dashboard_items, "Max charge power should be published"
    max_charge = api.dashboard_items[f"number.{prefix}_solis_{inverter_sn}_max_charge_power"]
    expected_max_power = int(50 * api.nominal_voltage)  # 50A * 48.4V
    assert max_charge["state"] == expected_max_power, f"Max charge power should be {expected_max_power}W, got {max_charge['state']}"

    # Check battery capacity calculation (Ah to kWh)
    assert f"sensor.{prefix}_solis_{inverter_sn}_battery_capacity" in api.dashboard_items, "Battery capacity should be published"
    battery_cap = api.dashboard_items[f"sensor.{prefix}_solis_{inverter_sn}_battery_capacity"]
    expected_kwh = round(100 * api.nominal_voltage / 1000.0, 2)  # 100Ah * 48.4V / 1000 = 4.84 kWh
    assert battery_cap["state"] == expected_kwh, f"Battery capacity should be {expected_kwh}kWh, got {battery_cap['state']}"
    assert battery_cap["attributes"]["unit_of_measurement"] == "kWh", "Battery capacity should have kWh unit"

    # Check that slot 2 entities are also published (even if disabled)
    assert f"switch.{prefix}_solis_{inverter_sn}_charge_slot2_enable" in api.dashboard_items, "Charge slot 2 should be published"
    charge2_enable = api.dashboard_items[f"switch.{prefix}_solis_{inverter_sn}_charge_slot2_enable"]
    assert charge2_enable["state"] == "off", "Charge slot 2 should be disabled"

    # Check detail API sensors
    assert f"sensor.{prefix}_solis_{inverter_sn}_pv_power" in api.dashboard_items, "PV power should be published"
    pv_power = api.dashboard_items[f"sensor.{prefix}_solis_{inverter_sn}_pv_power"]
    assert pv_power["state"] == 2.5, f"PV power should be 2.5, got {pv_power['state']}"

    assert f"sensor.{prefix}_solis_{inverter_sn}_battery_power" in api.dashboard_items, "Battery power should be published"
    assert f"sensor.{prefix}_solis_{inverter_sn}_load_power" in api.dashboard_items, "Load power should be published"
    assert f"sensor.{prefix}_solis_{inverter_sn}_grid_power" in api.dashboard_items, "Grid power should be published"

    print(f"PASSED: publish_entities created {len(api.dashboard_items)} entities correctly")
    return False


async def test_select_event_storage_mode():
    """Test select_event for storage mode changes"""
    print("\n=== Test: select_event storage mode ===")

    api = MockSolisAPI()
    inverter_sn = "123456"  # Use a simple SN without underscores
    api.inverter_sn = [inverter_sn]

    api.cached_values[inverter_sn] = {SOLIS_CID_STORAGE_MODE: "33"}

    # Setup time windows (required for publish_entities)
    api.charge_discharge_time_windows[inverter_sn] = {1: {"charge_enable": 1, "charge_start_time": "02:00", "charge_end_time": "05:00", "charge_soc": 95, "charge_current": 50}}

    api.max_charge_current[inverter_sn] = 50
    api.max_discharge_current[inverter_sn] = 50

    # Mock set_storage_mode
    api.set_storage_mode_calls = []

    async def mock_set_storage_mode(sn, value):
        api.set_storage_mode_calls.append({"sn": sn, "value": value})
        return True

    api.set_storage_mode = mock_set_storage_mode

    # Call select_event with storage mode change
    entity_id = f"select.predbat_solis_{inverter_sn}_storage_mode"
    value = "Self-Use"  # Text value from SOLIS_STORAGE_MODES (note: with hyphen, not "Self Use")

    await api.select_event(entity_id, value)

    # Verify set_storage_mode was called
    assert len(api.set_storage_mode_calls) == 1, "set_storage_mode should be called once"
    call = api.set_storage_mode_calls[0]
    assert call["sn"] == inverter_sn, f"Expected inverter_sn {inverter_sn}, got {call['sn']}"

    # The value should be converted from "Self-Use" to "35"
    expected_value = "35"  # SOLIS_STORAGE_MODES["Self-Use"] = 35
    assert call["value"] == expected_value, f"Expected value {expected_value}, got {call['value']}"

    print("PASSED: Storage mode select event handled correctly")
    return False


async def test_select_event_charge_time():
    """Test select_event for charge slot time changes"""
    print("\n=== Test: select_event charge time ===")

    api = MockSolisAPI()
    inverter_sn = "123456"
    api.inverter_sn = [inverter_sn]

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {1: {"charge_enable": 1, "charge_start_time": "00:00", "charge_end_time": "00:00", "charge_soc": 95, "charge_current": 50}}

    api.max_charge_current[inverter_sn] = 50
    api.max_discharge_current[inverter_sn] = 50

    # Call select_event for start time
    entity_id = f"select.predbat_solis_{inverter_sn}_charge_slot1_start_time"
    value = "02:00:00"  # HH:MM:SS format from select

    await api.select_event(entity_id, value)

    # Verify time was updated (should strip seconds)
    slot_data = api.charge_discharge_time_windows[inverter_sn][1]
    assert slot_data["charge_start_time"] == "02:00", f"Expected 02:00, got {slot_data['charge_start_time']}"

    # Call select_event for end time
    entity_id = f"select.predbat_solis_{inverter_sn}_charge_slot1_end_time"
    value = "05:30:00"

    await api.select_event(entity_id, value)

    # Verify end time was updated
    slot_data = api.charge_discharge_time_windows[inverter_sn][1]
    assert slot_data["charge_end_time"] == "05:30", f"Expected 05:30, got {slot_data['charge_end_time']}"

    print("PASSED: Charge time select events handled correctly")
    return False


async def test_select_event_discharge_time():
    """Test select_event for discharge slot time changes"""
    print("\n=== Test: select_event discharge time ===")

    api = MockSolisAPI()
    inverter_sn = "789012"
    api.inverter_sn = [inverter_sn]

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {2: {"discharge_enable": 1, "discharge_start_time": "00:00", "discharge_end_time": "00:00", "discharge_soc": 15, "discharge_current": 30}}

    api.max_charge_current[inverter_sn] = 50
    api.max_discharge_current[inverter_sn] = 50

    # Call select_event for discharge start time
    entity_id = f"select.predbat_solis_{inverter_sn}_discharge_slot2_start_time"
    value = "16:00:00"

    await api.select_event(entity_id, value)

    # Verify time was updated
    slot_data = api.charge_discharge_time_windows[inverter_sn][2]
    assert slot_data["discharge_start_time"] == "16:00", f"Expected 16:00, got {slot_data['discharge_start_time']}"

    # Call select_event for discharge end time
    entity_id = f"select.predbat_solis_{inverter_sn}_discharge_slot2_end_time"
    value = "19:30:00"

    await api.select_event(entity_id, value)

    # Verify end time was updated
    slot_data = api.charge_discharge_time_windows[inverter_sn][2]
    assert slot_data["discharge_end_time"] == "19:30", f"Expected 19:30, got {slot_data['discharge_end_time']}"

    print("PASSED: Discharge time select events handled correctly")
    return False


async def test_select_event_unknown_inverter():
    """Test select_event with unknown inverter (should log warning)"""
    print("\n=== Test: select_event unknown inverter ===")

    api = MockSolisAPI()
    api.inverter_sn = ["999999"]

    # Call select_event with unknown inverter
    entity_id = "select.predbat_solis_888888_charge_slot1_start_time"  # 888888 is unknown
    value = "02:00:00"

    await api.select_event(entity_id, value)

    # Verify warning was logged
    warn_log = any("Unknown inverter" in msg and "888888" in msg for msg in api.log_messages)
    assert warn_log, "Should log warning for unknown inverter"

    print("PASSED: Unknown inverter handled correctly")
    return False


async def test_switch_event_charge_enable():
    """Test switch_event for charge slot enable/disable"""
    print("\n=== Test: switch_event charge enable ===")

    api = MockSolisAPI()
    inverter_sn = "123456"
    api.inverter_sn = [inverter_sn]

    # Setup cached values for charge enable CID
    enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE  # Slot 1 = base + 0
    api.cached_values[inverter_sn] = {enable_cid: "0"}  # Initially disabled

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {1: {"charge_enable": 0, "charge_start_time": "02:00", "charge_end_time": "05:00", "charge_soc": 95, "charge_current": 50}}

    # Test turn_on service
    entity_id = f"switch.predbat_solis_{inverter_sn}_charge_slot1_enable"
    await api.switch_event(entity_id, "turn_on")

    # Verify enable was updated in cache
    assert api.charge_discharge_time_windows[inverter_sn][1]["charge_enable"] == 1, "Charge enable should be 1 after turn_on"

    # Test turn_off service
    await api.switch_event(entity_id, "turn_off")
    assert api.charge_discharge_time_windows[inverter_sn][1]["charge_enable"] == 0, "Charge enable should be 0 after turn_off"

    # Test toggle service - update cached_values to reflect current state
    api.cached_values[inverter_sn][enable_cid] = "0"
    await api.switch_event(entity_id, "toggle")
    assert api.charge_discharge_time_windows[inverter_sn][1]["charge_enable"] == 1, "Charge enable should be 1 after toggle from 0"

    # Toggle again
    api.cached_values[inverter_sn][enable_cid] = "1"
    await api.switch_event(entity_id, "toggle")
    assert api.charge_discharge_time_windows[inverter_sn][1]["charge_enable"] == 0, "Charge enable should be 0 after toggle from 1"

    print("PASSED: Charge slot enable switch handled correctly")
    return False


async def test_switch_event_discharge_enable():
    """Test switch_event for discharge slot enable/disable"""
    print("\n=== Test: switch_event discharge enable ===")

    api = MockSolisAPI()
    inverter_sn = "789012"
    api.inverter_sn = [inverter_sn]

    # Setup cached values for discharge enable CID (slot 2 = base + 1)
    enable_cid = SOLIS_CID_DISCHARGE_ENABLE_BASE + 1
    api.cached_values[inverter_sn] = {enable_cid: "1"}  # Initially enabled

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {2: {"discharge_enable": 1, "discharge_start_time": "16:00", "discharge_end_time": "19:00", "discharge_soc": 15, "discharge_current": 30}}

    # Test turn_off service
    entity_id = f"switch.predbat_solis_{inverter_sn}_discharge_slot2_enable"
    await api.switch_event(entity_id, "turn_off")

    # Verify enable was updated in cache
    assert api.charge_discharge_time_windows[inverter_sn][2]["discharge_enable"] == 0, "Discharge enable should be 0 after turn_off"

    # Test turn_on service
    await api.switch_event(entity_id, "turn_on")
    assert api.charge_discharge_time_windows[inverter_sn][2]["discharge_enable"] == 1, "Discharge enable should be 1 after turn_on"

    print("PASSED: Discharge slot enable switch handled correctly")
    return False


async def test_switch_event_battery_reserve():
    """Test switch_event for battery reserve (backup mode bit manipulation)"""
    print("\n=== Test: switch_event battery reserve ===")

    api = MockSolisAPI()
    inverter_sn = "345678"
    api.inverter_sn = [inverter_sn]

    # Setup initial storage mode (bit 2 = 0, backup off)
    api.cached_values[inverter_sn] = {SOLIS_CID_STORAGE_MODE: "33"}  # Binary: 100001, bit 2 is 0

    # Mock read_and_write_cid to capture calls
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value, "description": field_description})
        # Update cache
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test turn_on service (set bit 2)
    entity_id = f"switch.predbat_solis_{inverter_sn}_battery_reserve"
    await api.switch_event(entity_id, "turn_on")

    # Verify read_and_write_cid was called with correct value
    assert len(api.read_and_write_cid_calls) == 1, "read_and_write_cid should be called once"
    call = api.read_and_write_cid_calls[0]
    expected_value = 33 | (1 << SOLIS_BIT_BACKUP_MODE)  # Set bit 2: 33 | 4 = 37
    assert call["value"] == str(expected_value), f"Expected value {expected_value}, got {call['value']}"
    assert call["cid"] == SOLIS_CID_STORAGE_MODE, f"Expected CID {SOLIS_CID_STORAGE_MODE}, got {call['cid']}"

    # Test turn_off service (clear bit 2)
    api.read_and_write_cid_calls = []
    await api.switch_event(entity_id, "turn_off")

    call = api.read_and_write_cid_calls[0]
    expected_value = 37 & ~(1 << SOLIS_BIT_BACKUP_MODE)  # Clear bit 2: 37 & ~4 = 33
    assert call["value"] == str(expected_value), f"Expected value {expected_value}, got {call['value']}"

    # Test toggle service
    api.read_and_write_cid_calls = []
    await api.switch_event(entity_id, "toggle")

    call = api.read_and_write_cid_calls[0]
    expected_value = 33 ^ (1 << SOLIS_BIT_BACKUP_MODE)  # Toggle bit 2: 33 ^ 4 = 37
    assert call["value"] == str(expected_value), f"Expected value {expected_value}, got {call['value']}"

    print("PASSED: Battery reserve switch handled correctly")
    return False


async def test_switch_event_allow_grid_charging():
    """Test switch_event for allow grid charging (bit 4 manipulation)"""
    print("\n=== Test: switch_event allow grid charging ===")

    api = MockSolisAPI()
    inverter_sn = "456789"
    api.inverter_sn = [inverter_sn]

    # Setup initial storage mode (bit 4 = 0, grid charging off)
    api.cached_values[inverter_sn] = {SOLIS_CID_STORAGE_MODE: "35"}  # Binary: 100011, bit 4 is 0

    # Mock read_and_write_cid
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value})
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test turn_on service (set bit 4)
    entity_id = f"switch.predbat_solis_{inverter_sn}_allow_grid_charging"
    await api.switch_event(entity_id, "turn_on")

    call = api.read_and_write_cid_calls[0]
    expected_value = 35 | (1 << SOLIS_BIT_GRID_CHARGING)  # Set bit 4: 35 | 16 = 51
    assert call["value"] == str(expected_value), f"Expected value {expected_value}, got {call['value']}"

    print("PASSED: Allow grid charging switch handled correctly")
    return False


async def test_switch_event_time_of_use():
    """Test switch_event for time of use mode (bit 6 manipulation)"""
    print("\n=== Test: switch_event time of use ===")

    api = MockSolisAPI()
    inverter_sn = "567890"
    api.inverter_sn = [inverter_sn]

    # Setup initial storage mode (bit 6 = 0, TOU off)
    api.cached_values[inverter_sn] = {SOLIS_CID_STORAGE_MODE: "35"}

    # Mock read_and_write_cid
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value})
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test turn_on service (set bit 6)
    entity_id = f"switch.predbat_solis_{inverter_sn}_time_of_use"
    await api.switch_event(entity_id, "turn_on")

    call = api.read_and_write_cid_calls[0]
    expected_value = 35 | (1 << SOLIS_BIT_TOU_MODE)  # Set bit 6: 35 | 64 = 99
    assert call["value"] == str(expected_value), f"Expected value {expected_value}, got {call['value']}"

    print("PASSED: Time of use switch handled correctly")
    return False


async def test_switch_event_allow_export():
    """Test switch_event for allow export (inverted logic: 0=on, 1=off)"""
    print("\n=== Test: switch_event allow export ===")

    api = MockSolisAPI()
    inverter_sn = "678901"
    api.inverter_sn = [inverter_sn]

    # Setup initial state (allow export off = "1")
    api.cached_values[inverter_sn] = {SOLIS_CID_ALLOW_EXPORT: SOLIS_ALLOW_EXPORT_OFF}  # "1" = block export

    # Mock read_and_write_cid
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value})
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test turn_on service (should set to "0" = allow export)
    entity_id = f"switch.predbat_solis_{inverter_sn}_allow_export"
    await api.switch_event(entity_id, "turn_on")

    call = api.read_and_write_cid_calls[0]
    assert call["value"] == SOLIS_ALLOW_EXPORT_ON, f"Expected {SOLIS_ALLOW_EXPORT_ON}, got {call['value']}"

    # Test turn_off service (should set to "1" = block export)
    api.read_and_write_cid_calls = []
    await api.switch_event(entity_id, "turn_off")

    call = api.read_and_write_cid_calls[0]
    assert call["value"] == SOLIS_ALLOW_EXPORT_OFF, f"Expected {SOLIS_ALLOW_EXPORT_OFF}, got {call['value']}"

    # Test toggle service
    api.read_and_write_cid_calls = []
    await api.switch_event(entity_id, "toggle")

    call = api.read_and_write_cid_calls[0]
    assert call["value"] == SOLIS_ALLOW_EXPORT_ON, f"Expected {SOLIS_ALLOW_EXPORT_ON}, got {call['value']}"

    print("PASSED: Allow export switch handled correctly")
    return False


async def test_switch_event_unknown_service():
    """Test switch_event with unknown service (should log warning)"""
    print("\n=== Test: switch_event unknown service ===")

    api = MockSolisAPI()
    inverter_sn = "111222"
    api.inverter_sn = [inverter_sn]

    # Setup cached values
    enable_cid = SOLIS_CID_CHARGE_ENABLE_BASE
    api.cached_values[inverter_sn] = {enable_cid: "0"}

    # Setup time windows
    api.charge_discharge_time_windows[inverter_sn] = {1: {"charge_enable": 0}}

    # Call switch_event with unknown service
    entity_id = f"switch.predbat_solis_{inverter_sn}_charge_slot1_enable"
    await api.switch_event(entity_id, "unknown_service")

    # Verify warning was logged
    warn_log = any("Unknown service" in msg and "unknown_service" in msg for msg in api.log_messages)
    assert warn_log, "Should log warning for unknown service"

    # Verify value wasn't changed
    assert api.charge_discharge_time_windows[inverter_sn][1]["charge_enable"] == 0, "Value should not change for unknown service"

    print("PASSED: Unknown service handled correctly")
    return False


async def test_number_event_charge_soc():
    """Test number_event for charge slot SOC changes"""
    print("\n=== Test: number_event charge SOC ===")

    api = MockSolisAPI()
    inverter_sn = "123456"
    api.inverter_sn = [inverter_sn]

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {1: {"charge_soc": 90.0, "charge_current": 50}}

    # Test updating charge SOC
    entity_id = f"number.predbat_solis_{inverter_sn}_charge_slot1_soc"
    await api.number_event(entity_id, 95)

    # Verify SOC was updated
    assert api.charge_discharge_time_windows[inverter_sn][1]["charge_soc"] == 95.0, f"Expected 95.0, got {api.charge_discharge_time_windows[inverter_sn][1]['charge_soc']}"

    print("PASSED: Charge SOC number event handled correctly")
    return False


async def test_number_event_charge_power():
    """Test number_event for charge slot power (converts watts to amps)"""
    print("\n=== Test: number_event charge power ===")

    api = MockSolisAPI()
    inverter_sn = "234567"
    api.inverter_sn = [inverter_sn]

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {2: {"charge_current": 0}}

    # Test updating charge power (watts -> amps conversion)
    # nominal_voltage = 48.4V, so 2420W = 50A
    entity_id = f"number.predbat_solis_{inverter_sn}_charge_slot2_power"
    await api.number_event(entity_id, 2420)

    # Verify current was updated (2420W / 48.4V = 50A)
    expected_amps = int(2420 / 48.4)  # = 50A
    assert api.charge_discharge_time_windows[inverter_sn][2]["charge_current"] == float(expected_amps), f"Expected {expected_amps}, got {api.charge_discharge_time_windows[inverter_sn][2]['charge_current']}"

    print("PASSED: Charge power number event handled correctly")
    return False


async def test_number_event_discharge_soc():
    """Test number_event for discharge slot SOC changes"""
    print("\n=== Test: number_event discharge SOC ===")

    api = MockSolisAPI()
    inverter_sn = "345678"
    api.inverter_sn = [inverter_sn]

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {3: {"discharge_soc": 20.0}}

    # Test updating discharge SOC
    entity_id = f"number.predbat_solis_{inverter_sn}_discharge_slot3_soc"
    await api.number_event(entity_id, 15)

    # Verify SOC was updated
    assert api.charge_discharge_time_windows[inverter_sn][3]["discharge_soc"] == 15.0, f"Expected 15.0, got {api.charge_discharge_time_windows[inverter_sn][3]['discharge_soc']}"

    print("PASSED: Discharge SOC number event handled correctly")
    return False


async def test_number_event_discharge_power():
    """Test number_event for discharge slot power (converts watts to amps)"""
    print("\n=== Test: number_event discharge power ===")

    api = MockSolisAPI()
    inverter_sn = "456789"
    api.inverter_sn = [inverter_sn]

    # Setup initial time windows
    api.charge_discharge_time_windows[inverter_sn] = {4: {"discharge_current": 0}}

    # Test updating discharge power (watts -> amps conversion)
    # nominal_voltage = 48.4V, so 1452W = 30A
    entity_id = f"number.predbat_solis_{inverter_sn}_discharge_slot4_power"
    await api.number_event(entity_id, 1452)

    # Verify current was updated (1452W / 48.4V = 30A)
    expected_amps = int(1452 / 48.4)  # = 30A
    assert api.charge_discharge_time_windows[inverter_sn][4]["discharge_current"] == float(expected_amps), f"Expected {expected_amps}, got {api.charge_discharge_time_windows[inverter_sn][4]['discharge_current']}"

    print("PASSED: Discharge power number event handled correctly")
    return False


async def test_number_event_battery_soc_limits():
    """Test number_event for battery SOC limits (reserve, over_discharge, etc.)"""
    print("\n=== Test: number_event battery SOC limits ===")

    api = MockSolisAPI()
    inverter_sn = "567890"
    api.inverter_sn = [inverter_sn]

    # Setup cached values
    api.cached_values[inverter_sn] = {}

    # Mock read_and_write_cid
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value, "description": field_description})
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test reserve_soc
    entity_id = f"number.predbat_solis_{inverter_sn}_reserve_soc"
    await api.number_event(entity_id, 10)

    assert len(api.read_and_write_cid_calls) == 1, "Should call read_and_write_cid once"
    call = api.read_and_write_cid_calls[0]
    assert call["cid"] == SOLIS_CID_BATTERY_RESERVE_SOC, f"Expected CID {SOLIS_CID_BATTERY_RESERVE_SOC}, got {call['cid']}"
    assert call["value"] == "10", f"Expected '10', got {call['value']}"

    # Test over_discharge_soc
    api.read_and_write_cid_calls = []
    entity_id = f"number.predbat_solis_{inverter_sn}_over_discharge_soc"
    await api.number_event(entity_id, 5)

    call = api.read_and_write_cid_calls[0]
    assert call["cid"] == SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC, f"Expected CID {SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC}, got {call['cid']}"
    assert call["value"] == "5", f"Expected '5', got {call['value']}"

    print("PASSED: Battery SOC limits number event handled correctly")
    return False


async def test_number_event_max_power():
    """Test number_event for max charge/discharge power (converts watts to amps)"""
    print("\n=== Test: number_event max power ===")

    api = MockSolisAPI()
    inverter_sn = "678901"
    api.inverter_sn = [inverter_sn]

    # Setup cached values
    api.cached_values[inverter_sn] = {}

    # Mock read_and_write_cid
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value, "description": field_description})
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test max_charge_power (watts -> amps conversion)
    # nominal_voltage = 48.4V, so 4840W = 100A
    entity_id = f"number.predbat_solis_{inverter_sn}_max_charge_power"
    await api.number_event(entity_id, 4840)

    assert len(api.read_and_write_cid_calls) == 1, "Should call read_and_write_cid once"
    call = api.read_and_write_cid_calls[0]
    assert call["cid"] == SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT, f"Expected CID {SOLIS_CID_BATTERY_MAX_CHARGE_CURRENT}, got {call['cid']}"
    expected_amps = str(int(4840 / 48.4))  # = "100"
    assert call["value"] == expected_amps, f"Expected '{expected_amps}', got {call['value']}"

    print("PASSED: Max power number event handled correctly")
    return False


async def test_number_event_power_controls():
    """Test number_event for power control limits"""
    print("\n=== Test: number_event power controls ===")

    api = MockSolisAPI()
    inverter_sn = "789012"
    api.inverter_sn = [inverter_sn]

    # Setup cached values
    api.cached_values[inverter_sn] = {}

    # Mock read_and_write_cid
    api.read_and_write_cid_calls = []

    async def mock_read_and_write_cid(sn, cid, value, field_description=None):
        api.read_and_write_cid_calls.append({"sn": sn, "cid": cid, "value": value, "description": field_description})
        api.cached_values[sn][cid] = value
        return True

    api.read_and_write_cid = mock_read_and_write_cid

    # Test power_limit
    entity_id = f"number.predbat_solis_{inverter_sn}_power_limit"
    await api.number_event(entity_id, 3000)

    assert len(api.read_and_write_cid_calls) == 1, "Should call read_and_write_cid once"
    call = api.read_and_write_cid_calls[0]
    assert call["cid"] == SOLIS_CID_POWER_LIMIT, f"Expected CID {SOLIS_CID_POWER_LIMIT}, got {call['cid']}"
    assert call["value"] == "3000", f"Expected '3000', got {call['value']}"

    print("PASSED: Power controls number event handled correctly")
    return False


async def test_number_event_unknown_inverter():
    """Test number_event with unknown inverter (should log warning)"""
    print("\n=== Test: number_event unknown inverter ===")

    api = MockSolisAPI()
    api.inverter_sn = ["999999"]

    # Call number_event with unknown inverter
    entity_id = "number.predbat_solis_888888_charge_slot1_soc"
    await api.number_event(entity_id, 95)

    # Verify warning was logged
    warn_log = any("Unknown inverter" in msg and "888888" in msg for msg in api.log_messages)
    assert warn_log, "Should log warning for unknown inverter"

    print("PASSED: Unknown inverter handled correctly")
    return False


async def test_set_storage_mode_self_use():
    """Test set_storage_mode writes correct CID value for Self-Use mode"""
    print("\n=== Test: set_storage_mode Self-Use ===")

    api = MockSolisAPI()
    inverter_sn = "345678"
    api.inverter_sn = [inverter_sn]

    # Setup storage modes
    api.storage_modes[inverter_sn] = SOLIS_STORAGE_MODES

    # Call set_storage_mode with mode name
    await api.set_storage_mode(inverter_sn, "Self-Use")

    # Verify read_and_write_cid was called
    calls = api.read_and_write_cid_calls
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"

    # Verify correct CID and value
    call = calls[0]
    assert call["cid"] == SOLIS_CID_STORAGE_MODE, f"Expected CID {SOLIS_CID_STORAGE_MODE}, got {call['cid']}"
    assert call["value"] == "35", f"Expected value '35' (Self-Use), got {call['value']}"
    assert "storage mode to Self-Use" in call["field_description"], "Field description should mention Self-Use"

    print("PASSED: set_storage_mode writes Self-Use correctly")
    return False


async def test_set_storage_mode_feed_in_priority():
    """Test set_storage_mode writes correct CID value for Feed-in priority mode"""
    print("\n=== Test: set_storage_mode Feed-in priority ===")

    api = MockSolisAPI()
    inverter_sn = "456789"
    api.inverter_sn = [inverter_sn]

    # Setup storage modes
    api.storage_modes[inverter_sn] = SOLIS_STORAGE_MODES

    # Call set_storage_mode with mode name
    await api.set_storage_mode(inverter_sn, "Feed-in priority")

    # Verify read_and_write_cid was called
    calls = api.read_and_write_cid_calls
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"

    # Verify correct CID and value
    call = calls[0]
    assert call["cid"] == SOLIS_CID_STORAGE_MODE, f"Expected CID {SOLIS_CID_STORAGE_MODE}, got {call['cid']}"
    assert call["value"] == "98", f"Expected value '98' (Feed-in priority), got {call['value']}"
    assert "storage mode to Feed-in priority" in call["field_description"], "Field description should mention Feed-in priority"

    print("PASSED: set_storage_mode writes Feed-in priority correctly")
    return False


async def test_set_storage_mode_unknown_mode():
    """Test set_storage_mode with unknown mode (should log error and not write)"""
    print("\n=== Test: set_storage_mode unknown mode ===")

    api = MockSolisAPI()
    inverter_sn = "567890"
    api.inverter_sn = [inverter_sn]

    # Setup storage modes
    api.storage_modes[inverter_sn] = SOLIS_STORAGE_MODES

    # Call set_storage_mode with unknown mode
    await api.set_storage_mode(inverter_sn, "Invalid Mode")

    # Verify no write was attempted
    calls = api.read_and_write_cid_calls
    assert len(calls) == 0, f"Expected 0 calls for unknown mode, got {len(calls)}"

    # Verify error was logged
    error_log = any("Unknown storage mode" in msg and "Invalid Mode" in msg for msg in api.log_messages)
    assert error_log, "Should log error for unknown storage mode"

    print("PASSED: Unknown storage mode handled correctly")
    return False


async def test_set_storage_mode_if_needed_changes():
    """Test set_storage_mode_if_needed writes when mode differs from cache"""
    print("\n=== Test: set_storage_mode_if_needed changes ===")

    api = MockSolisAPI()
    inverter_sn = "678901"
    api.inverter_sn = [inverter_sn]

    # Setup storage modes
    api.storage_modes[inverter_sn] = SOLIS_STORAGE_MODES

    # Setup cached value (currently Self-Use - No Grid Charging = 1)
    api.cached_values[inverter_sn] = {SOLIS_CID_STORAGE_MODE: "1"}

    # Call set_storage_mode_if_needed to change to Self-Use (35)
    await api.set_storage_mode_if_needed(inverter_sn, "Self-Use")

    # Verify read_and_write_cid was called
    calls = api.read_and_write_cid_calls
    assert len(calls) == 1, f"Expected 1 call when mode changes, got {len(calls)}"

    # Verify correct CID and value
    call = calls[0]
    assert call["cid"] == SOLIS_CID_STORAGE_MODE, f"Expected CID {SOLIS_CID_STORAGE_MODE}, got {call['cid']}"
    assert call["value"] == "35", f"Expected value '35' (Self-Use), got {call['value']}"

    print("PASSED: set_storage_mode_if_needed writes when mode changes")
    return False


async def test_set_storage_mode_if_needed_no_changes():
    """Test set_storage_mode_if_needed skips write when mode matches cache"""
    print("\n=== Test: set_storage_mode_if_needed no changes ===")

    api = MockSolisAPI()
    inverter_sn = "789012"
    api.inverter_sn = [inverter_sn]

    # Setup storage modes
    api.storage_modes[inverter_sn] = SOLIS_STORAGE_MODES

    # Setup cached value (currently Self-Use = 35)
    api.cached_values[inverter_sn] = {SOLIS_CID_STORAGE_MODE: "35"}

    # Call set_storage_mode_if_needed with same mode
    await api.set_storage_mode_if_needed(inverter_sn, "Self-Use")

    # Verify NO write was attempted
    calls = api.read_and_write_cid_calls
    assert len(calls) == 0, f"Expected 0 calls when mode unchanged, got {len(calls)}"

    print("PASSED: set_storage_mode_if_needed skips write when mode unchanged")
    return False


async def test_set_storage_mode_if_needed_all_modes():
    """Test set_storage_mode_if_needed with multiple mode transitions"""
    print("\n=== Test: set_storage_mode_if_needed all modes ===")

    api = MockSolisAPI()
    inverter_sn = "890123"
    api.inverter_sn = [inverter_sn]

    # Setup storage modes
    api.storage_modes[inverter_sn] = SOLIS_STORAGE_MODES

    # Initialize cache
    api.cached_values[inverter_sn] = {}

    # Test mode transitions
    test_modes = [
        ("Self-Use", "35"),
        ("Feed-in priority", "98"),
        ("Backup/Reserve", "51"),
        ("Self-Use - No Grid Charging", "1"),
    ]

    for mode_name, expected_value in test_modes:
        # Clear call log
        api.read_and_write_cid_calls = []

        # Change mode
        await api.set_storage_mode_if_needed(inverter_sn, mode_name)

        # Verify write occurred
        calls = api.read_and_write_cid_calls
        assert len(calls) == 1, f"Expected 1 call for mode '{mode_name}', got {len(calls)}"

        # Verify correct value
        call = calls[0]
        assert call["value"] == expected_value, f"Expected value '{expected_value}' for {mode_name}, got {call['value']}"

        # Update cache to simulate successful write
        api.cached_values[inverter_sn][SOLIS_CID_STORAGE_MODE] = expected_value

        # Call again - should NOT write (already set)
        api.read_and_write_cid_calls = []
        await api.set_storage_mode_if_needed(inverter_sn, mode_name)
        assert len(api.read_and_write_cid_calls) == 0, f"Should not write when {mode_name} already set"

    print("PASSED: Multiple mode transitions handled correctly")
    return False


async def test_automatic_config():
    """Test automatic_config method configures Predbat correctly"""
    print("Testing automatic_config...")

    # Create API with multiple inverters
    api = MockSolisAPI(prefix="predbat")
    api.inverter_sn = ["ABC123", "DEF456"]

    # Track set_arg calls
    set_arg_calls = {}

    def mock_set_arg(key, value):
        set_arg_calls[key] = value

    api.set_arg = mock_set_arg

    # Run automatic_config
    await api.automatic_config()

    # Verify inverter_type configured correctly
    assert "inverter_type" in set_arg_calls, "inverter_type not configured"
    assert set_arg_calls["inverter_type"] == ["SolisCloud", "SolisCloud"], f"Expected ['SolisCloud', 'SolisCloud'], got {set_arg_calls['inverter_type']}"

    # Verify num_inverters
    assert "num_inverters" in set_arg_calls, "num_inverters not configured"
    assert set_arg_calls["num_inverters"] == 2, f"Expected 2 inverters, got {set_arg_calls['num_inverters']}"

    # Verify SOC entities use lowercase serial numbers
    assert "soc_percent" in set_arg_calls, "soc_percent not configured"
    expected_soc = ["sensor.predbat_solis_abc123_battery_soc", "sensor.predbat_solis_def456_battery_soc"]
    assert set_arg_calls["soc_percent"] == expected_soc, f"Expected {expected_soc}, got {set_arg_calls['soc_percent']}"

    # Verify battery_power configured correctly
    assert "battery_power" in set_arg_calls, "battery_power not configured"
    expected_battery_power = ["sensor.predbat_solis_abc123_battery_power", "sensor.predbat_solis_def456_battery_power"]
    assert set_arg_calls["battery_power"] == expected_battery_power, f"Expected {expected_battery_power}, got {set_arg_calls['battery_power']}"

    # Verify battery_power_invert set to True for all inverters
    assert "battery_power_invert" in set_arg_calls, "battery_power_invert not configured"
    assert set_arg_calls["battery_power_invert"] == ["True", "True"], f"Expected ['True', 'True'], got {set_arg_calls['battery_power_invert']}"

    # Verify charge controls point to slot1
    assert "charge_start_time" in set_arg_calls, "charge_start_time not configured"
    expected_charge_start = ["select.predbat_solis_abc123_charge_slot1_start_time", "select.predbat_solis_def456_charge_slot1_start_time"]
    assert set_arg_calls["charge_start_time"] == expected_charge_start, f"Expected {expected_charge_start}, got {set_arg_calls['charge_start_time']}"

    assert "charge_limit" in set_arg_calls, "charge_limit not configured"
    expected_charge_limit = ["number.predbat_solis_abc123_charge_slot1_soc", "number.predbat_solis_def456_charge_slot1_soc"]
    assert set_arg_calls["charge_limit"] == expected_charge_limit, f"Expected {expected_charge_limit}, got {set_arg_calls['charge_limit']}"

    # Verify discharge controls point to slot1
    assert "discharge_start_time" in set_arg_calls, "discharge_start_time not configured"
    expected_discharge_start = ["select.predbat_solis_abc123_discharge_slot1_start_time", "select.predbat_solis_def456_discharge_slot1_start_time"]
    assert set_arg_calls["discharge_start_time"] == expected_discharge_start, f"Expected {expected_discharge_start}, got {set_arg_calls['discharge_start_time']}"

    # Verify grid, load, and PV power entities
    assert "grid_power" in set_arg_calls, "grid_power not configured"
    assert "load_power" in set_arg_calls, "load_power not configured"
    assert "pv_power" in set_arg_calls, "pv_power not configured"

    # Verify energy entities configured
    assert "load_today" in set_arg_calls, "load_today not configured"
    assert "import_today" in set_arg_calls, "import_today not configured"
    assert "export_today" in set_arg_calls, "export_today not configured"
    assert "pv_today" in set_arg_calls, "pv_today not configured"

    # Verify reserve and limits configured
    assert "reserve" in set_arg_calls, "reserve not configured"
    assert "battery_min_soc" in set_arg_calls, "battery_min_soc not configured"

    # Verify rate controls configured
    assert "battery_rate_max" in set_arg_calls, "battery_rate_max not configured"
    assert "inverter_limit" in set_arg_calls, "inverter_limit not configured"
    assert "export_limit" in set_arg_calls, "export_limit not configured"

    print("PASSED: automatic_config configures all entities correctly")

    # Test with single inverter
    api2 = MockSolisAPI(prefix="test_prefix")
    api2.inverter_sn = ["SINGLE123"]

    set_arg_calls2 = {}

    def mock_set_arg2(key, value):
        set_arg_calls2[key] = value

    api2.set_arg = mock_set_arg2

    await api2.automatic_config()

    # Verify single inverter config
    assert set_arg_calls2["num_inverters"] == 1, f"Expected 1 inverter, got {set_arg_calls2['num_inverters']}"
    assert set_arg_calls2["soc_percent"] == ["sensor.test_prefix_solis_single123_battery_soc"], f"Unexpected soc_percent: {set_arg_calls2['soc_percent']}"

    print("PASSED: automatic_config works with single inverter and custom prefix")

    # Test with empty inverter list
    api3 = MockSolisAPI()
    api3.inverter_sn = []

    set_arg_calls3 = {}

    def mock_set_arg3(key, value):
        set_arg_calls3[key] = value

    api3.set_arg = mock_set_arg3

    await api3.automatic_config()

    # Should log warning and not configure anything
    assert len(set_arg_calls3) == 0, "Should not configure entities when no inverters present"
    assert any("No inverters to configure" in msg for msg in api3.log_messages), "Should log warning about no inverters"

    print("PASSED: automatic_config handles empty inverter list")

    return False
