# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
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

    async def read_cid(self, inverter_sn, cid):
        """Mock read_cid - return cached value"""
        return self.cached_values.get(inverter_sn, {}).get(cid, "0")

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


def run_solis_tests(my_predbat):
    """
    Run all Solis API tests
    Returns False on success, True on failure
    """
    failed = False

    try:
        # Run tests
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

    # Verify storage mode was set to Self-Use (non-zero charge current)
    storage_mode_calls = api.set_storage_mode_calls
    assert len(storage_mode_calls) == 1, f"Expected 1 storage mode call, got {len(storage_mode_calls)}"
    assert storage_mode_calls[0]["mode"] == "Self-Use", "Storage mode should be Self-Use for non-zero charge current"

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
            "discharge_start_time": "16:00",
            "discharge_end_time": "19:00",
            "discharge_soc": 15,
            "discharge_current": 30,
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
    assert charge_enable["state"] == "1", "Charge slot 1 should be enabled"

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
    assert discharge_enable["state"] == "1", "Discharge slot 1 should be enabled"

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
    assert charge2_enable["state"] == "0", "Charge slot 2 should be disabled"

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
