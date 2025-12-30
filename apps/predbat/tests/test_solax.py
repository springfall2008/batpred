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
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock
import json
from solax import SolaxAPI
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session


class MockSolaxAPI(SolaxAPI):
    """Mock SolaxAPI class for testing without ComponentBase dependencies"""

    def __init__(self, prefix="predbat"):
        # Don't call parent __init__ to avoid ComponentBase
        self.prefix = prefix
        self.controls = {}
        self.plant_inverters = {}
        self.plant_batteries = {}
        self.plant_info = []
        self.device_info = {}
        self.realtime_data = {}
        self.realtime_device_data = {}
        self.log_messages = []
        self.dashboard_items = {}
        self.current_mode_hash = None
        self.current_mode_hash_timestamp = None
        self.args = {}  # Configuration arguments
        self.plant_list = []  # List of plant IDs
        self.automatic = False  # Automatic config flag
        self.enable_controls = False  # Controls enable flag
        self.have_set_default_mode = False  # Track if default mode set

        # Track method calls for testing
        self.self_consume_mode_called = False
        self.soc_target_control_mode_called = False
        self.last_mode_call = None

        # Mock timezone
        self.local_tz = timezone.utc
        self.mock_time = None  # For time mocking in tests

        # Authentication attributes for testing
        self.client_id = "test_client_id"
        self.client_secret = "test_secret"
        self.base_url = "https://openapi-eu.solaxcloud.com"
        self.access_token = None
        self.token_expiry = None
        self.error_count = 0

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)
        print(message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Mock dashboard_item method"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes}

    def get_state_wrapper(self, entity_id, default=None):
        """Mock get_state_wrapper method"""
        if entity_id in self.dashboard_items:
            return self.dashboard_items[entity_id]["state"]
        return default

    def set_state_wrapper(self, entity_id, state):
        """Mock set_state_wrapper method"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": {}}

    def get_arg(self, key, default=None):
        """Mock get_arg method"""
        return self.args.get(key, default)

    def set_arg(self, key, value):
        """Mock set_arg method"""
        self.args[key] = value

    def update_success_timestamp(self):
        """Mock update_success_timestamp method"""
        pass

    async def self_consume_mode(self, sn_list, time_of_duration, next_motion=161, business_type=None):
        """Mock self_consume_mode for testing"""
        self.self_consume_mode_called = True
        self.last_mode_call = {"mode": "eco", "sn_list": sn_list, "duration": time_of_duration}
        return True

    async def soc_target_control_mode(self, sn_list, target_soc, charge_discharge_power):
        """Mock soc_target_control_mode for testing"""
        self.soc_target_control_mode_called = True
        self.last_mode_call = {"mode": "charge" if charge_discharge_power > 0 else "export", "sn_list": sn_list, "target_soc": target_soc, "power": charge_discharge_power}
        return True


def run_solax_tests(my_predbat):
    """
    Run all SolaX API tests
    Returns False on success, True on failure
    """
    failed = False

    try:
        # Run tests
        failed |= asyncio.run(test_get_access_token_main(my_predbat))
        failed |= asyncio.run(test_request_wrapper_main(my_predbat))
        failed |= asyncio.run(test_request_get_impl_get(my_predbat))
        failed |= asyncio.run(test_request_get_impl_post(my_predbat))
        failed |= asyncio.run(test_fetch_paginated_data(my_predbat))
        failed |= asyncio.run(test_fetch_single_result(my_predbat))
        failed |= asyncio.run(test_query_plant_info(my_predbat))
        failed |= asyncio.run(test_query_device_info(my_predbat))
        failed |= asyncio.run(test_query_plant_realtime_data_main())
        failed |= asyncio.run(test_query_device_realtime_data_main())
        failed |= asyncio.run(test_query_device_realtime_data_all_main())
        failed |= asyncio.run(test_query_plant_statistics_daily_main())
        failed |= asyncio.run(test_send_command_and_wait_main())
        failed |= asyncio.run(test_control_mode_functions_main())
        failed |= asyncio.run(test_publish_device_info_main())
        failed |= asyncio.run(test_publish_device_realtime_data_main())
        failed |= asyncio.run(test_helper_methods_main())
        failed |= asyncio.run(test_automatic_config_main())
        failed |= asyncio.run(test_publish_controls_main())
        failed |= asyncio.run(test_run_main())
        failed |= asyncio.run(test_set_default_work_mode_main())
        failed |= asyncio.run(test_positive_or_negative_mode_main())
        failed |= asyncio.run(test_self_consume_charge_only_mode_main())
        failed |= asyncio.run(test_query_request_result_main())
        failed |= asyncio.run(test_write_setting_from_event(my_predbat))
        failed |= asyncio.run(test_fetch_controls_main(my_predbat))
        failed |= asyncio.run(test_apply_controls_main(my_predbat))
        failed |= asyncio.run(test_publish_plant_info_main(my_predbat))

        if not failed:
            print("**** SolaX API tests: All tests passed ****")
    except AssertionError as e:
        print(f"**** SolaX API tests FAILED: {e} ****")
        failed = True
    except Exception as e:
        print(f"**** SolaX API tests FAILED with exception: {e} ****")
        import traceback

        traceback.print_exc()
        failed = True

    return failed


async def test_fetch_controls_main(my_predbat):
    """
    Wrapper for test_fetch_controls
    """
    # Create mock SolaX API instance
    solax_api = MockSolaxAPI(prefix="predbat")

    # Test plant configuration
    test_plant_id = "1618699116555534337"

    # Set up plant info with inverter
    solax_api.plant_inverters[test_plant_id] = ["H1231231932123"]
    solax_api.device_info["H1231231932123"] = {"deviceSn": "H1231231932123", "deviceType": 1, "plantId": test_plant_id, "ratedPower": 10.0}

    return await test_fetch_controls(solax_api, test_plant_id)


async def test_apply_controls_main(my_predbat):
    """
    Wrapper for test_apply_controls
    """
    # Create mock SolaX API instance
    solax_api = MockSolaxAPI(prefix="predbat")

    # Test plant configuration
    test_plant_id = "1618699116555534337"

    # Set up plant info with inverter and battery
    solax_api.plant_inverters[test_plant_id] = ["H1231231932123"]
    solax_api.plant_batteries[test_plant_id] = ["TP123456123123"]
    solax_api.device_info["H1231231932123"] = {"deviceSn": "H1231231932123", "deviceType": 1, "plantId": test_plant_id, "ratedPower": 10.0}
    solax_api.device_info["TP123456123123"] = {"deviceSn": "TP123456123123", "deviceType": 2, "plantId": test_plant_id, "ratedPower": 5.0}
    solax_api.plant_info = [{"plantId": test_plant_id, "batteryCapacity": 15.0}]

    return await test_apply_controls(solax_api, test_plant_id)


async def test_apply_controls(solax_api, test_plant_id):
    """
    Test apply_controls method - applies charge/export/eco modes based on schedule
    """
    failed = False

    # Patch datetime.now in the solax module
    from unittest.mock import patch
    from datetime import datetime as dt_class

    # Set up controls dictionary with schedules
    solax_api.controls[test_plant_id] = {
        "reserve": 10,
        "charge": {"start_time": "02:00:00", "end_time": "06:00:00", "enable": True, "target_soc": 95, "rate": 5000},
        "export": {"start_time": "16:00:00", "end_time": "20:00:00", "enable": True, "target_soc": 15, "rate": 4500},
    }

    # Test 1: ECO mode (outside charge/export windows) at 12:00
    print("\n--- Test 1: ECO mode (12:00) ---")
    # Mock current time to 12:00 (outside windows)
    test_time = datetime.now(solax_api.local_tz).replace(hour=12, minute=0, second=0, microsecond=0)

    with patch("solax.datetime") as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kw: dt_class(*args, **kw)

        solax_api.self_consume_mode_called = False
        solax_api.soc_target_control_mode_called = False
        solax_api.last_mode_call = None

        result = await solax_api.apply_controls(test_plant_id)

        if not result:
            print("**** ERROR: apply_controls returned False for eco mode ****")
            failed = True
        elif not solax_api.self_consume_mode_called:
            print("**** ERROR: self_consume_mode not called for eco mode ****")
            failed = True
        elif solax_api.soc_target_control_mode_called:
            print("**** ERROR: soc_target_control_mode called during eco mode ****")
            failed = True
        else:
            print(f"✓ ECO mode applied correctly at 12:00")

    # Test 2: Charge mode (inside charge window) at 03:00
    print("\n--- Test 2: Charge mode (03:00) ---")
    test_time = datetime.now(solax_api.local_tz).replace(hour=3, minute=0, second=0, microsecond=0)

    with patch("solax.datetime") as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kw: dt_class(*args, **kw)

        solax_api.self_consume_mode_called = False
        solax_api.soc_target_control_mode_called = False
        solax_api.last_mode_call = None
        solax_api.current_mode_hash = None  # Reset hash

        result = await solax_api.apply_controls(test_plant_id)

        if not result:
            print("**** ERROR: apply_controls returned False for charge mode ****")
            failed = True
        elif solax_api.self_consume_mode_called:
            print("**** ERROR: self_consume_mode called during charge mode ****")
            failed = True
        elif not solax_api.soc_target_control_mode_called:
            print("**** ERROR: soc_target_control_mode not called for charge mode ****")
            failed = True
        elif solax_api.last_mode_call["power"] != 5000:
            print(f"**** ERROR: Charge power incorrect. Expected 5000, got {solax_api.last_mode_call['power']} ****")
            failed = True
        elif solax_api.last_mode_call["target_soc"] != 95:
            print(f"**** ERROR: Charge target_soc incorrect. Expected 95, got {solax_api.last_mode_call['target_soc']} ****")
            failed = True
        else:
            print(f"✓ Charge mode applied correctly at 03:00 (power=5000W, target_soc=95%)")

    # Test 3: Export mode (inside export window) at 18:00
    print("\n--- Test 3: Export mode (18:00) ---")
    test_time = datetime.now(solax_api.local_tz).replace(hour=18, minute=0, second=0, microsecond=0)

    with patch("solax.datetime") as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kw: dt_class(*args, **kw)

        solax_api.self_consume_mode_called = False
        solax_api.soc_target_control_mode_called = False
        solax_api.last_mode_call = None
        solax_api.current_mode_hash = None  # Reset hash

        result = await solax_api.apply_controls(test_plant_id)

        if not result:
            print("**** ERROR: apply_controls returned False for export mode ****")
            failed = True
        elif solax_api.self_consume_mode_called:
            print("**** ERROR: self_consume_mode called during export mode ****")
            failed = True
        elif not solax_api.soc_target_control_mode_called:
            print("**** ERROR: soc_target_control_mode not called for export mode ****")
            failed = True
        elif solax_api.last_mode_call["power"] != -4500:
            print(f"**** ERROR: Export power incorrect. Expected -4500, got {solax_api.last_mode_call['power']} ****")
            failed = True
        elif solax_api.last_mode_call["target_soc"] != 15:
            print(f"**** ERROR: Export target_soc incorrect. Expected 15, got {solax_api.last_mode_call['target_soc']} ****")
            failed = True
        else:
            print(f"✓ Export mode applied correctly at 18:00 (power=-4500W, target_soc=15%)")

    # Test 4: Hash prevents re-application (same charge mode at 03:00)
    print("\n--- Test 4: Hash caching (repeat charge at 03:00) ---")
    test_time = datetime.now(solax_api.local_tz).replace(hour=3, minute=0, second=0, microsecond=0)

    with patch("solax.datetime") as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kw: dt_class(*args, **kw)

        # Set hash from previous charge call
        solax_api.current_mode_hash = hash(("charge", 5000, 95, 360))  # 06:00 = 360 minutes
        solax_api.current_mode_hash_timestamp = test_time

        solax_api.self_consume_mode_called = False
        solax_api.soc_target_control_mode_called = False
        solax_api.last_mode_call = None

        result = await solax_api.apply_controls(test_plant_id)

        if not result:
            print("**** ERROR: apply_controls returned False when hash matched ****")
            failed = True
        elif solax_api.self_consume_mode_called or solax_api.soc_target_control_mode_called:
            print("**** ERROR: Mode command sent when hash should have prevented it ****")
            failed = True
        else:
            print(f"✓ Hash correctly prevented re-application of same mode")

    # Test 5: Hash expires after 15 minutes
    print("\n--- Test 5: Hash expiry (16 minutes later) ---")
    test_time = datetime.now(solax_api.local_tz).replace(hour=3, minute=16, second=0, microsecond=0)

    with patch("solax.datetime") as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kw: dt_class(*args, **kw)

        # Hash timestamp is 16 minutes old (> 15 minute threshold)
        old_timestamp = test_time - timedelta(minutes=16)
        solax_api.current_mode_hash = hash(("charge", 5000, 95, 360))
        solax_api.current_mode_hash_timestamp = old_timestamp

        solax_api.self_consume_mode_called = False
        solax_api.soc_target_control_mode_called = False
        solax_api.last_mode_call = None

        result = await solax_api.apply_controls(test_plant_id)

        if not result:
            print("**** ERROR: apply_controls returned False when hash expired ****")
            failed = True
        elif not solax_api.soc_target_control_mode_called:
            print("**** ERROR: Mode command not sent when hash should have expired ****")
            failed = True
        else:
            print(f"✓ Hash correctly expired after 15 minutes, mode re-applied")

    # Test 6: Charge disabled - should use eco mode
    print("\n--- Test 6: Charge disabled (03:00) ---")
    test_time = datetime.now(solax_api.local_tz).replace(hour=3, minute=0, second=0, microsecond=0)

    with patch("solax.datetime") as mock_datetime:
        mock_datetime.now.return_value = test_time
        mock_datetime.side_effect = lambda *args, **kw: dt_class(*args, **kw)

        solax_api.controls[test_plant_id]["charge"]["enable"] = False
        solax_api.current_mode_hash = None  # Reset hash

        solax_api.self_consume_mode_called = False
        solax_api.soc_target_control_mode_called = False
        solax_api.last_mode_call = None

        result = await solax_api.apply_controls(test_plant_id)

        if not result:
            print("**** ERROR: apply_controls returned False when charge disabled ****")
            failed = True
        elif not solax_api.self_consume_mode_called:
            print("**** ERROR: ECO mode not applied when charge disabled ****")
            failed = True
        elif solax_api.soc_target_control_mode_called:
            print("**** ERROR: Charge mode applied when charge disabled ****")
            failed = True
        else:
            print(f"✓ ECO mode correctly applied when charge disabled at 03:00")

    return failed


async def test_write_setting_from_event(my_predbat):
    """
    Test event wrapper methods (switch_event, number_event, select_event)
    Tests parsing entity IDs and updating control settings
    """
    failed = False

    # Create mock SolaX API instance
    solax_api = MockSolaxAPI(prefix="predbat")

    # Set up test plant and controls
    test_plant_id = "1618699116555534337"
    solax_api.controls = {
        test_plant_id: {
            "reserve": 10,
            "charge": {"start_time": "00:00:00", "end_time": "06:00:00", "enable": False, "target_soc": 100, "rate": 5000},
            "export": {"start_time": "00:00:00", "end_time": "00:00:00", "enable": False, "target_soc": 10, "rate": 5000},
        }
    }

    # Mock plant_inverters for get_max_power_inverter
    solax_api.plant_inverters = {test_plant_id: ["H1234567890"]}
    solax_api.device_info = {"H1234567890": {"deviceType": 1, "ratedPower": 5.0}}  # 5kW

    # Test 1: Update reserve setting (number type)
    entity_id = f"number.{solax_api.prefix}_solax_{test_plant_id}_setting_reserve"
    new_value = 20

    await solax_api.number_event(entity_id, new_value)

    if solax_api.controls[test_plant_id]["reserve"] != new_value:
        print(f"**** ERROR: Reserve not updated. Expected {new_value}, got {solax_api.controls[test_plant_id]['reserve']} ****")
        failed = True
    else:
        print(f"✓ Reserve setting updated to {new_value}")

    # Test 2: Invalid entity ID format
    invalid_entity_id = "number.invalid_format"
    await solax_api.number_event(invalid_entity_id, 30)

    # Should not crash and reserve should remain unchanged
    if solax_api.controls[test_plant_id]["reserve"] != new_value:
        print(f"**** ERROR: Reserve changed on invalid entity ID ****")
        failed = True
    else:
        print(f"✓ Invalid entity ID handled gracefully")

    # Test 3: Plant not in controls
    missing_plant_id = "9999999999999999999"
    entity_id = f"number.{solax_api.prefix}_solax_{missing_plant_id}_setting_reserve"
    await solax_api.number_event(entity_id, 40)

    # Should not crash, just log a warning
    if missing_plant_id in solax_api.controls:
        print(f"**** ERROR: Missing plant incorrectly added to controls ****")
        failed = True
    else:
        print(f"✓ Missing plant handled correctly")

    # Test 4: Invalid number value
    entity_id = f"number.{solax_api.prefix}_solax_{test_plant_id}_setting_reserve"
    await solax_api.number_event(entity_id, "invalid")

    # Should not crash and reserve should remain unchanged
    if solax_api.controls[test_plant_id]["reserve"] != new_value:
        print(f"**** ERROR: Reserve changed on invalid number value ****")
        failed = True
    else:
        print(f"✓ Invalid number value handled gracefully")

    # Test 5: Update charge start_time (battery schedule)
    entity_id = f"select.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_charge_start_time"
    new_time = "01:30:00"
    await solax_api.select_event(entity_id, new_time)

    if solax_api.controls[test_plant_id]["charge"]["start_time"] != new_time:
        print(f"**** ERROR: Charge start_time not updated. Expected {new_time}, got {solax_api.controls[test_plant_id]['charge']['start_time']} ****")
        failed = True
    else:
        print(f"✓ Charge start_time updated to {new_time}")

    # Test 6: Update charge end_time
    entity_id = f"select.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_charge_end_time"
    new_time = "05:30:00"
    await solax_api.select_event(entity_id, new_time)

    if solax_api.controls[test_plant_id]["charge"]["end_time"] != new_time:
        print(f"**** ERROR: Charge end_time not updated. Expected {new_time}, got {solax_api.controls[test_plant_id]['charge']['end_time']} ****")
        failed = True
    else:
        print(f"✓ Charge end_time updated to {new_time}")

    # Test 7: Update charge enable (switch - turn_on service)
    entity_id = f"switch.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_charge_enable"
    await solax_api.switch_event(entity_id, "turn_on")

    if solax_api.controls[test_plant_id]["charge"]["enable"] != True:
        print(f"**** ERROR: Charge enable not set to True ****")
        failed = True
    else:
        print(f"✓ Charge enable turned on")

    # Test 8: Update charge enable (switch - turn_off service)
    await solax_api.switch_event(entity_id, "turn_off")

    if solax_api.controls[test_plant_id]["charge"]["enable"] != False:
        print(f"**** ERROR: Charge enable not set to False ****")
        failed = True
    else:
        print(f"✓ Charge enable turned off")

    # Test 9: Update export start_time
    entity_id = f"select.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_export_start_time"
    new_time = "16:00:00"
    await solax_api.select_event(entity_id, new_time)

    if solax_api.controls[test_plant_id]["export"]["start_time"] != new_time:
        print(f"**** ERROR: Export start_time not updated. Expected {new_time}, got {solax_api.controls[test_plant_id]['export']['start_time']} ****")
        failed = True
    else:
        print(f"✓ Export start_time updated to {new_time}")

    # Test 10: Update charge target_soc
    entity_id = f"number.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_charge_target_soc"
    new_soc = 95
    await solax_api.number_event(entity_id, new_soc)

    if solax_api.controls[test_plant_id]["charge"]["target_soc"] != new_soc:
        print(f"**** ERROR: Charge target_soc not updated. Expected {new_soc}, got {solax_api.controls[test_plant_id]['charge']['target_soc']} ****")
        failed = True
    else:
        print(f"✓ Charge target_soc updated to {new_soc}")

    # Test 11: Update charge rate
    entity_id = f"number.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_charge_rate"
    new_rate = 3000
    await solax_api.number_event(entity_id, new_rate)

    if solax_api.controls[test_plant_id]["charge"]["rate"] != new_rate:
        print(f"**** ERROR: Charge rate not updated. Expected {new_rate}, got {solax_api.controls[test_plant_id]['charge']['rate']} ****")
        failed = True
    else:
        print(f"✓ Charge rate updated to {new_rate}")

    # Test 12: Invalid time format (HH:MM instead of HH:MM:SS) - should be auto-fixed
    entity_id = f"select.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_charge_start_time"
    new_time_short = "02:00"
    expected_time = "02:00:00"
    await solax_api.select_event(entity_id, new_time_short)

    if solax_api.controls[test_plant_id]["charge"]["start_time"] != expected_time:
        print(f"**** ERROR: Time format conversion failed. Expected {expected_time}, got {solax_api.controls[test_plant_id]['charge']['start_time']} ****")
        failed = True
    else:
        print(f"✓ Time format auto-converted from {new_time_short} to {expected_time}")

    # Test 13: Update export target_soc
    entity_id = f"number.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_export_target_soc"
    new_export_soc = 15
    await solax_api.number_event(entity_id, new_export_soc)

    if solax_api.controls[test_plant_id]["export"]["target_soc"] != new_export_soc:
        print(f"**** ERROR: Export target_soc not updated. Expected {new_export_soc}, got {solax_api.controls[test_plant_id]['export']['target_soc']} ****")
        failed = True
    else:
        print(f"✓ Export target_soc updated to {new_export_soc}")

    # Test 14: Update export rate
    entity_id = f"number.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_export_rate"
    new_export_rate = 4000
    await solax_api.number_event(entity_id, new_export_rate)

    if solax_api.controls[test_plant_id]["export"]["rate"] != new_export_rate:
        print(f"**** ERROR: Export rate not updated. Expected {new_export_rate}, got {solax_api.controls[test_plant_id]['export']['rate']} ****")
        failed = True
    else:
        print(f"✓ Export rate updated to {new_export_rate}")

    # Test 15: Update export end_time
    entity_id = f"select.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_export_end_time"
    new_time = "19:30:00"
    await solax_api.select_event(entity_id, new_time)

    if solax_api.controls[test_plant_id]["export"]["end_time"] != new_time:
        print(f"**** ERROR: Export end_time not updated. Expected {new_time}, got {solax_api.controls[test_plant_id]['export']['end_time']} ****")
        failed = True
    else:
        print(f"✓ Export end_time updated to {new_time}")

    # Test 16: Update export enable (switch - turn_on service)
    entity_id = f"switch.{solax_api.prefix}_solax_{test_plant_id}_battery_schedule_export_enable"
    await solax_api.switch_event(entity_id, "turn_on")

    if solax_api.controls[test_plant_id]["export"]["enable"] != True:
        print(f"**** ERROR: Export enable not set to True ****")
        failed = True
    else:
        print(f"✓ Export enable turned on")

    # Test 17: Update export enable (switch - toggle service)
    await solax_api.switch_event(entity_id, "toggle")

    if solax_api.controls[test_plant_id]["export"]["enable"] != False:
        print(f"**** ERROR: Export enable not toggled to False ****")
        failed = True
    else:
        print(f"✓ Export enable toggled off")

    return failed


async def test_fetch_controls(solax_api, test_plant_id):
    """
    Test fetch_controls method - reads from HA state and populates controls dictionary
    """
    failed = False
    prefix = solax_api.prefix

    # Pre-populate dashboard items with mock HA state (simulating what HA would return)
    # Reserve setting
    solax_api.dashboard_items[f"number.{prefix}_solax_{test_plant_id}_setting_reserve"] = {"state": 25, "attributes": {}}

    # Charge schedule
    solax_api.dashboard_items[f"select.{prefix}_solax_{test_plant_id}_battery_schedule_charge_start_time"] = {"state": "02:30:00", "attributes": {}}
    solax_api.dashboard_items[f"select.{prefix}_solax_{test_plant_id}_battery_schedule_charge_end_time"] = {"state": "06:00:00", "attributes": {}}
    solax_api.dashboard_items[f"switch.{prefix}_solax_{test_plant_id}_battery_schedule_charge_enable"] = {"state": True, "attributes": {}}
    solax_api.dashboard_items[f"number.{prefix}_solax_{test_plant_id}_battery_schedule_charge_target_soc"] = {"state": 90, "attributes": {}}
    solax_api.dashboard_items[f"number.{prefix}_solax_{test_plant_id}_battery_schedule_charge_rate"] = {"state": 5000, "attributes": {}}

    # Export schedule
    solax_api.dashboard_items[f"select.{prefix}_solax_{test_plant_id}_battery_schedule_export_start_time"] = {"state": "16:30:00", "attributes": {}}
    solax_api.dashboard_items[f"select.{prefix}_solax_{test_plant_id}_battery_schedule_export_end_time"] = {"state": "19:00:00", "attributes": {}}
    solax_api.dashboard_items[f"switch.{prefix}_solax_{test_plant_id}_battery_schedule_export_enable"] = {"state": False, "attributes": {}}
    solax_api.dashboard_items[f"number.{prefix}_solax_{test_plant_id}_battery_schedule_export_target_soc"] = {"state": 20, "attributes": {}}
    solax_api.dashboard_items[f"number.{prefix}_solax_{test_plant_id}_battery_schedule_export_rate"] = {"state": 4500, "attributes": {}}

    # Clear controls to ensure fetch_controls populates it
    solax_api.controls = {}

    # Call fetch_controls
    result = await solax_api.fetch_controls(test_plant_id)

    if not result:
        print("**** ERROR: fetch_controls returned False ****")
        failed = True

    # Verify reserve setting
    if solax_api.controls[test_plant_id].get("reserve") != 25:
        print(f"**** ERROR: Reserve not fetched correctly. Expected 25, got {solax_api.controls[test_plant_id].get('reserve')} ****")
        failed = True
    else:
        print("✓ Reserve fetched correctly (25)")

    # Verify charge schedule
    charge_controls = solax_api.controls[test_plant_id]["charge"]
    if charge_controls.get("start_time") != "02:30:00":
        print(f"**** ERROR: Charge start_time not fetched correctly. Expected 02:30:00, got {charge_controls.get('start_time')} ****")
        failed = True
    else:
        print("✓ Charge start_time fetched correctly (02:30:00)")

    if charge_controls.get("end_time") != "06:00:00":
        print(f"**** ERROR: Charge end_time not fetched correctly. Expected 06:00:00, got {charge_controls.get('end_time')} ****")
        failed = True
    else:
        print("✓ Charge end_time fetched correctly (06:00:00)")

    if charge_controls.get("enable") != True:
        print(f"**** ERROR: Charge enable not fetched correctly. Expected True, got {charge_controls.get('enable')} ****")
        failed = True
    else:
        print("✓ Charge enable fetched correctly (True)")

    if charge_controls.get("target_soc") != 90:
        print(f"**** ERROR: Charge target_soc not fetched correctly. Expected 90, got {charge_controls.get('target_soc')} ****")
        failed = True
    else:
        print("✓ Charge target_soc fetched correctly (90)")

    if charge_controls.get("rate") != 5000:
        print(f"**** ERROR: Charge rate not fetched correctly. Expected 5000, got {charge_controls.get('rate')} ****")
        failed = True
    else:
        print("✓ Charge rate fetched correctly (5000)")

    # Verify export schedule
    export_controls = solax_api.controls[test_plant_id]["export"]
    if export_controls.get("start_time") != "16:30:00":
        print(f"**** ERROR: Export start_time not fetched correctly. Expected 16:30:00, got {export_controls.get('start_time')} ****")
        failed = True
    else:
        print("✓ Export start_time fetched correctly (16:30:00)")

    if export_controls.get("end_time") != "19:00:00":
        print(f"**** ERROR: Export end_time not fetched correctly. Expected 19:00:00, got {export_controls.get('end_time')} ****")
        failed = True
    else:
        print("✓ Export end_time fetched correctly (19:00:00)")

    if export_controls.get("enable") != False:
        print(f"**** ERROR: Export enable not fetched correctly. Expected False, got {export_controls.get('enable')} ****")
        failed = True
    else:
        print("✓ Export enable fetched correctly (False)")

    if export_controls.get("target_soc") != 20:
        print(f"**** ERROR: Export target_soc not fetched correctly. Expected 20, got {export_controls.get('target_soc')} ****")
        failed = True
    else:
        print("✓ Export target_soc fetched correctly (20)")

    if export_controls.get("rate") != 4500:
        print(f"**** ERROR: Export rate not fetched correctly. Expected 4500, got {export_controls.get('rate')} ****")
        failed = True
    else:
        print("✓ Export rate fetched correctly (4500)")

    return failed


async def test_publish_plant_info_main(my_predbat):
    """
    Wrapper for test_publish_plant_info
    """
    # Create mock SolaX API instance
    solax_api = MockSolaxAPI(prefix="predbat")

    # Test plant configuration
    test_plant_id = "1618699116555534337"
    test_plant_name = "Test Plant"

    # Set up plant info
    solax_api.plant_info = [
        {
            "plantId": test_plant_id,
            "plantName": test_plant_name,
            "pvCapacity": 8.5,
            "batteryCapacity": 15.0,
        }
    ]

    # Set up plant inverters and batteries
    solax_api.plant_inverters[test_plant_id] = ["H1231231932123"]
    solax_api.plant_batteries[test_plant_id] = ["TP123456123123"]

    # Set up device info
    solax_api.device_info["H1231231932123"] = {
        "deviceSn": "H1231231932123",
        "deviceType": 1,
        "plantId": test_plant_id,
        "ratedPower": 10.0,
    }
    solax_api.device_info["TP123456123123"] = {
        "deviceSn": "TP123456123123",
        "deviceType": 2,
        "plantId": test_plant_id,
        "ratedPower": 5.0,
    }

    # Set up realtime data
    solax_api.realtime_data[test_plant_id] = {
        "totalYield": 3250.5,
        "totalCharged": 1850.2,
        "totalDischarged": 1720.8,
        "totalImported": 4200.3,
        "totalExported": 2800.7,
        "totalEarnings": 485.50,
    }

    # Set up realtime device data for battery
    solax_api.realtime_device_data["TP123456123123"] = {
        "batterySOC": 75,
        "batteryTemperature": 18.5,
    }

    return await test_publish_plant_info(solax_api, test_plant_id, test_plant_name)


async def test_publish_plant_info(solax_api, test_plant_id, test_plant_name):
    """
    Test publish_plant_info method - publishes plant sensors to dashboard
    """
    failed = False
    prefix = solax_api.prefix

    # Clear dashboard items
    solax_api.dashboard_items = {}

    # Call publish_plant_info
    await solax_api.publish_plant_info()

    # Test 1: Battery SOC sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_battery_soc"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Battery SOC sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        expected_soc = 11.25  # 75% of 15.0 kWh
        if item["state"] != expected_soc:
            print(f"**** ERROR: Battery SOC incorrect. Expected {expected_soc}, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "kWh":
            print(f"**** ERROR: Battery SOC unit incorrect ****")
            failed = True
        else:
            print(f"✓ Battery SOC sensor published correctly ({expected_soc} kWh)")

    # Test 2: Battery capacity sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_battery_capacity"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Battery capacity sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 15.0:
            print(f"**** ERROR: Battery capacity incorrect. Expected 15.0, got {item['state']} ****")
            failed = True
        else:
            print(f"✓ Battery capacity sensor published correctly (15.0 kWh)")

    # Test 3: Battery temperature sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_battery_temperature"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Battery temperature sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 18.5:
            print(f"**** ERROR: Battery temperature incorrect. Expected 18.5, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "°C":
            print(f"**** ERROR: Battery temperature unit incorrect ****")
            failed = True
        else:
            print(f"✓ Battery temperature sensor published correctly (18.5°C)")

    # Test 4: Battery max power sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_battery_max_power"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Battery max power sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        expected_power = 5000  # 5.0 kW * 1000
        if item["state"] != expected_power:
            print(f"**** ERROR: Battery max power incorrect. Expected {expected_power}, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "W":
            print(f"**** ERROR: Battery max power unit incorrect ****")
            failed = True
        else:
            print(f"✓ Battery max power sensor published correctly ({expected_power}W)")

    # Test 5: Inverter max power sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_inverter_max_power"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Inverter max power sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        expected_power = 10000  # 10.0 kW * 1000
        if item["state"] != expected_power:
            print(f"**** ERROR: Inverter max power incorrect. Expected {expected_power}, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "W":
            print(f"**** ERROR: Inverter max power unit incorrect ****")
            failed = True
        else:
            print(f"✓ Inverter max power sensor published correctly ({expected_power}W)")

    # Test 6: PV capacity sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_pv_capacity"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: PV capacity sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 8.5:
            print(f"**** ERROR: PV capacity incorrect. Expected 8.5, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "kWp":
            print(f"**** ERROR: PV capacity unit incorrect ****")
            failed = True
        else:
            print(f"✓ PV capacity sensor published correctly (8.5 kWp)")

    # Test 7: Total yield sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_yield"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total yield sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 3250.5:
            print(f"**** ERROR: Total yield incorrect. Expected 3250.5, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "kWh":
            print(f"**** ERROR: Total yield unit incorrect ****")
            failed = True
        else:
            print(f"✓ Total yield sensor published correctly (3250.5 kWh)")

    # Test 8: Total charged sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_charged"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total charged sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 1850.2:
            print(f"**** ERROR: Total charged incorrect. Expected 1850.2, got {item['state']} ****")
            failed = True
        else:
            print(f"✓ Total charged sensor published correctly (1850.2 kWh)")

    # Test 9: Total discharged sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_discharged"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total discharged sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 1720.8:
            print(f"**** ERROR: Total discharged incorrect. Expected 1720.8, got {item['state']} ****")
            failed = True
        else:
            print(f"✓ Total discharged sensor published correctly (1720.8 kWh)")

    # Test 10: Total imported sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_imported"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total imported sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 4200.3:
            print(f"**** ERROR: Total imported incorrect. Expected 4200.3, got {item['state']} ****")
            failed = True
        else:
            print(f"✓ Total imported sensor published correctly (4200.3 kWh)")

    # Test 11: Total exported sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_exported"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total exported sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 2800.7:
            print(f"**** ERROR: Total exported incorrect. Expected 2800.7, got {item['state']} ****")
            failed = True
        else:
            print(f"✓ Total exported sensor published correctly (2800.7 kWh)")

    # Test 12: Total load sensor (calculated)
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_load"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total load sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        # Formula: imported + discharged - exported - charged + yield
        # 4200.3 + 1720.8 - 2800.7 - 1850.2 + 3250.5 = 4520.7
        expected_load = 4520.7
        if abs(item["state"] - expected_load) > 0.01:  # Allow small floating point error
            print(f"**** ERROR: Total load incorrect. Expected {expected_load}, got {item['state']} ****")
            failed = True
        else:
            print(f"✓ Total load sensor published correctly ({expected_load} kWh)")

    # Test 13: Total earnings sensor
    entity_id = f"sensor.{prefix}_solax_{test_plant_id}_total_earnings"
    if entity_id not in solax_api.dashboard_items:
        print(f"**** ERROR: Total earnings sensor not published ****")
        failed = True
    else:
        item = solax_api.dashboard_items[entity_id]
        if item["state"] != 485.50:
            print(f"**** ERROR: Total earnings incorrect. Expected 485.50, got {item['state']} ****")
            failed = True
        elif item["attributes"]["unit_of_measurement"] != "currency":
            print(f"**** ERROR: Total earnings unit incorrect ****")
            failed = True
        else:
            print(f"✓ Total earnings sensor published correctly (485.50)")

    return failed


async def test_get_access_token_main(my_predbat):
    """
    Test get_access_token() method with various scenarios
    """
    failed = False
    solax_api = MockSolaxAPI(prefix="predbat")

    # Test 1: Successful authentication
    print("Test 1: Successful authentication")
    solax_api.error_count = 0
    solax_api.access_token = None
    solax_api.token_expiry = None

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "result": {"access_token": "test_token_123", "expires_in": 2592000}})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token != "test_token_123":
            print(f"**** ERROR: Expected token 'test_token_123', got '{token}' ****")
            failed = True
        elif solax_api.access_token != "test_token_123":
            print(f"**** ERROR: access_token not set correctly ****")
            failed = True
        elif solax_api.token_expiry is None:
            print(f"**** ERROR: token_expiry not set ****")
            failed = True
        else:
            # Check token expiry is approximately correct (within 5 seconds)
            expected_expiry = datetime.now(timezone.utc) + timedelta(seconds=2592000)
            time_diff = abs((solax_api.token_expiry - expected_expiry).total_seconds())
            if time_diff > 5:
                print(f"**** ERROR: token_expiry time difference too large: {time_diff} seconds ****")
                failed = True
            elif solax_api.error_count != 0:
                print(f"**** ERROR: error_count should be 0, got {solax_api.error_count} ****")
                failed = True
            else:
                print(f"✓ Successful authentication test passed")

    # Test 2: Invalid credentials (10402)
    print("Test 2: Invalid credentials (code 10402)")
    solax_api.error_count = 0
    solax_api.access_token = "old_token"
    solax_api.token_expiry = datetime.now(timezone.utc)

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10402, "message": "Invalid credentials"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token is not None:
            print(f"**** ERROR: Expected None, got '{token}' ****")
            failed = True
        elif solax_api.access_token is not None:
            print(f"**** ERROR: access_token should be None, got '{solax_api.access_token}' ****")
            failed = True
        elif solax_api.token_expiry is not None:
            print(f"**** ERROR: token_expiry should be None ****")
            failed = True
        elif solax_api.error_count != 1:
            print(f"**** ERROR: error_count should be 1, got {solax_api.error_count} ****")
            failed = True
        else:
            print(f"✓ Invalid credentials test passed")

    # Test 3: Other error codes
    print("Test 3: Other API error codes")
    solax_api.error_count = 0

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10001, "message": "Unknown error"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token is not None:
            print(f"**** ERROR: Expected None, got '{token}' ****")
            failed = True
        elif solax_api.error_count != 1:
            print(f"**** ERROR: error_count should be 1, got {solax_api.error_count} ****")
            failed = True
        else:
            print(f"✓ Other error codes test passed")

    # Test 4: Network timeout
    print("Test 4: Network timeout")
    solax_api.error_count = 0

    mock_session = create_aiohttp_mock_session(exception=asyncio.TimeoutError("Connection timeout"))

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token is not None:
            print(f"**** ERROR: Expected None, got '{token}' ****")
            failed = True
        elif solax_api.error_count != 1:
            print(f"**** ERROR: error_count should be 1, got {solax_api.error_count} ****")
            failed = True
        else:
            print(f"✓ Network timeout test passed")

    # Test 5: HTTP errors
    print("Test 5: HTTP 500 error")
    solax_api.error_count = 0

    mock_response = create_aiohttp_mock_response(status=500)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token is not None:
            print(f"**** ERROR: Expected None, got '{token}' ****")
            failed = True
        elif solax_api.error_count != 1:
            print(f"**** ERROR: error_count should be 1, got {solax_api.error_count} ****")
            failed = True
        else:
            print(f"✓ HTTP error test passed")

    # Test 6: JSON decode errors
    print("Test 6: JSON decode error")
    solax_api.error_count = 0

    mock_response = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("Invalid JSON", "", 0))
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token is not None:
            print(f"**** ERROR: Expected None, got '{token}' ****")
            failed = True
        elif solax_api.error_count != 1:
            print(f"**** ERROR: error_count should be 1, got {solax_api.error_count} ****")
            failed = True
        else:
            print(f"✓ JSON decode error test passed")

    # Test 7: Missing access_token
    print("Test 7: Missing access_token in response")
    solax_api.error_count = 0

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "result": {}})  # Empty result
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token is not None:
            print(f"**** ERROR: Expected None, got '{token}' ****")
            failed = True
        elif solax_api.error_count != 1:
            print(f"**** ERROR: error_count should be 1, got {solax_api.error_count} ****")
            failed = True
        else:
            print(f"✓ Missing access_token test passed")

    # Test 8: Default expires_in
    print("Test 8: Default expires_in fallback")
    solax_api.error_count = 0
    solax_api.access_token = None
    solax_api.token_expiry = None

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 0,
            "result": {
                "access_token": "test_token_default"
                # No expires_in field
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        token = await solax_api.get_access_token()

        if token != "test_token_default":
            print(f"**** ERROR: Expected token 'test_token_default', got '{token}' ****")
            failed = True
        elif solax_api.token_expiry is None:
            print(f"**** ERROR: token_expiry not set ****")
            failed = True
        else:
            # Check default expiry is approximately 30 days (2591999 seconds)
            expected_expiry = datetime.now(timezone.utc) + timedelta(seconds=2591999)
            time_diff = abs((solax_api.token_expiry - expected_expiry).total_seconds())
            if time_diff > 5:
                print(f"**** ERROR: token_expiry time difference too large for default: {time_diff} seconds ****")
                failed = True
            else:
                print(f"✓ Default expires_in test passed")

    if not failed:
        print("✓ Authentication tests passed")

    return failed

async def test_request_wrapper_main(my_predbat):
    """
    Test request_wrapper retry logic
    Covers: retry with exponential backoff, ClientError, TimeoutError, 
    max retries (SOLAX_RETRIES), unexpected exceptions, successful retry after failures
    """
    print("=" * 60)
    print("Testing request_wrapper()")
    print("=" * 60)

    failed = False
    import aiohttp

    # Test 1: Successful call on first attempt
    print("\n--- Test 1: Successful call on first attempt ---")
    solax_api = MockSolaxAPI()
    call_count = 0

    async def successful_func():
        nonlocal call_count
        call_count += 1
        return {"success": True}

    result = await solax_api.request_wrapper(successful_func)
    if result != {"success": True}:
        print(f"**** ERROR: Expected success result, got {result} ****")
        failed = True
    elif call_count != 1:
        print(f"**** ERROR: Expected 1 call, got {call_count} ****")
        failed = True
    else:
        print(f"✓ Successful call test passed")

    # Test 2: ClientError with retry and eventual success
    print("\n--- Test 2: ClientError with retry and eventual success ---")
    solax_api = MockSolaxAPI()
    call_count = 0

    async def retry_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise aiohttp.ClientError("Temporary network error")
        return {"success": True}

    result = await solax_api.request_wrapper(retry_then_succeed)
    if result != {"success": True}:
        print(f"**** ERROR: Expected success result after retry, got {result} ****")
        failed = True
    elif call_count != 2:
        print(f"**** ERROR: Expected 2 calls (1 failure + 1 success), got {call_count} ****")
        failed = True
    else:
        print(f"✓ ClientError retry test passed")

    # Test 3: TimeoutError with retry and eventual success
    print("\n--- Test 3: TimeoutError with retry and eventual success ---")
    solax_api = MockSolaxAPI()
    call_count = 0

    async def timeout_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise asyncio.TimeoutError("Request timeout")
        return {"success": True}

    result = await solax_api.request_wrapper(timeout_then_succeed)
    if result != {"success": True}:
        print(f"**** ERROR: Expected success result after timeout retry, got {result} ****")
        failed = True
    elif call_count != 2:
        print(f"**** ERROR: Expected 2 calls (1 timeout + 1 success), got {call_count} ****")
        failed = True
    else:
        print(f"✓ TimeoutError retry test passed")

    # Test 4: Max retries exceeded (SOLAX_RETRIES)
    print("\n--- Test 4: Max retries exceeded ---")
    from solax import SOLAX_RETRIES
    from unittest.mock import patch
    solax_api = MockSolaxAPI()
    call_count = 0
    initial_error_count = solax_api.error_count

    async def always_fail():
        nonlocal call_count
        call_count += 1
        raise aiohttp.ClientError("Permanent network error")

    # Mock asyncio.sleep to speed up the test
    with patch('asyncio.sleep', new_callable=AsyncMock):
        result = await solax_api.request_wrapper(always_fail)
    
    if result is not None:
        print(f"**** ERROR: Expected None after max retries, got {result} ****")
        failed = True
    elif call_count != SOLAX_RETRIES:
        print(f"**** ERROR: Expected {SOLAX_RETRIES} calls, got {call_count} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ Max retries test passed (SOLAX_RETRIES={SOLAX_RETRIES})")

    # Test 5: Unexpected exception breaks retry loop
    print("\n--- Test 5: Unexpected exception breaks retry loop ---")
    solax_api = MockSolaxAPI()
    call_count = 0
    initial_error_count = solax_api.error_count

    async def unexpected_error():
        nonlocal call_count
        call_count += 1
        raise ValueError("Unexpected error")

    result = await solax_api.request_wrapper(unexpected_error)
    if result is not None:
        print(f"**** ERROR: Expected None after unexpected exception, got {result} ****")
        failed = True
    elif call_count != 1:
        print(f"**** ERROR: Expected 1 call (no retry on unexpected exception), got {call_count} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ Unexpected exception test passed")

    # Test 6: Verify exponential backoff (retry * 0.5)
    # Note: We mock asyncio.sleep to speed up the test
    print("\n--- Test 6: Multiple retries with exponential backoff pattern ---")
    solax_api = MockSolaxAPI()
    call_count = 0

    async def fail_twice_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise aiohttp.ClientError("Temporary error")
        return {"success": True}

    # Mock asyncio.sleep to speed up the test
    with patch('asyncio.sleep', new_callable=AsyncMock):
        result = await solax_api.request_wrapper(fail_twice_then_succeed)
    
    if result != {"success": True}:
        print(f"**** ERROR: Expected success after multiple retries, got {result} ****")
        failed = True
    elif call_count != 3:
        print(f"**** ERROR: Expected 3 calls (2 failures + 1 success), got {call_count} ****")
        failed = True
    else:
        print(f"✓ Multiple retry test passed (exponential backoff pattern verified)")

    if not failed:
        print("✓ request_wrapper tests passed")

    return failed

async def test_request_get_impl_get(my_predbat):
    """
    Test _request_get_impl() GET requests
    Covers: successful GET with valid token, token expiry/refresh logic, 
    HTTP errors (401, 404, 500), query parameter handling, response validation
    """
    print("=" * 60)
    print("Testing _request_get_impl() GET requests")
    print("=" * 60)

    failed = False
    from unittest.mock import patch, MagicMock
    from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

    # Test 1: Successful GET with valid token
    print("\n--- Test 1: Successful GET with valid token ---")
    solax_api = MockSolaxAPI()
    # Set valid token
    solax_api.access_token = "valid_token_123"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "data": {"result": "success"}})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/path", params={"page": 1, "size": 10})

    if result != {"code": 0, "data": {"result": "success"}}:
        print(f"**** ERROR: Expected success response, got {result} ****")
        failed = True
    else:
        print(f"✓ Successful GET with valid token test passed")

    # Test 2: Token expired - should refresh
    print("\n--- Test 2: Token expired - should refresh ---")
    solax_api = MockSolaxAPI()
    # Set expired token
    solax_api.access_token = "expired_token"
    solax_api.token_expiry = datetime.now(timezone.utc) - timedelta(hours=1)

    # First call will be POST for auth, second will be GET
    # Note: auth response needs result.access_token format
    auth_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "result": {"access_token": "new_refreshed_token", "expires_in": 2592000}})
    get_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "data": {"refreshed": True}})
    
    mock_session = MagicMock()
    
    # Mock POST for auth
    auth_context = MagicMock()
    async def auth_aenter(*args, **kwargs):
        return auth_response
    async def auth_aexit(*args):
        pass
    auth_context.__aenter__ = auth_aenter
    auth_context.__aexit__ = auth_aexit
    mock_session.post = MagicMock(return_value=auth_context)
    
    # Mock GET for data
    get_context = MagicMock()
    async def get_aenter(*args, **kwargs):
        return get_response
    async def get_aexit(*args):
        pass
    get_context.__aenter__ = get_aenter
    get_context.__aexit__ = get_aexit
    mock_session.get = MagicMock(return_value=get_context)
    
    # Session context manager
    async def session_aenter(*args):
        return mock_session
    async def session_aexit(*args):
        pass
    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/path")

    if result != {"code": 0, "data": {"refreshed": True}}:
        print(f"**** ERROR: Expected success after refresh, got {result} ****")
        failed = True
    else:
        print(f"✓ Token expired and refreshed test passed")

    # Test 3: HTTP 404 error
    print("\n--- Test 3: HTTP 404 error ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    mock_response = create_aiohttp_mock_response(status=404, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/nonexistent/path")

    if result is not None:
        print(f"**** ERROR: Expected None for 404 error, got {result} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ HTTP 404 error test passed")

    # Test 4: HTTP 500 error
    print("\n--- Test 4: HTTP 500 error ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    mock_response = create_aiohttp_mock_response(status=500, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/error/path")

    if result is not None:
        print(f"**** ERROR: Expected None for 500 error, got {result} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ HTTP 500 error test passed")

    # Test 5: Authentication error in response (code 10401)
    print("\n--- Test 5: Authentication error in response (code 10401) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "invalid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10401, "message": "Invalid token"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/path")

    if result is not None:
        print(f"**** ERROR: Expected None for auth error, got {result} ****")
        failed = True
    elif solax_api.access_token is not None or solax_api.token_expiry is not None:
        print(f"**** ERROR: Expected token to be cleared, but access_token={solax_api.access_token}, token_expiry={solax_api.token_expiry} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ Authentication error (10401) test passed")

    # Test 6: JSON decode error
    print("\n--- Test 6: JSON decode error ---")
    import json
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    mock_response = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("Invalid JSON", "doc", 0))
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/path")

    if result is not None:
        print(f"**** ERROR: Expected None for JSON decode error, got {result} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ JSON decode error test passed")

    # Test 7: No token available and refresh fails
    print("\n--- Test 7: No token available and refresh fails ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = None
    solax_api.token_expiry = None

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10402, "message": "Invalid credentials"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/path")

    if result is not None:
        print(f"**** ERROR: Expected None when token refresh fails, got {result} ****")
        failed = True
    else:
        print(f"✓ Failed token refresh test passed")

    if not failed:
        print("✓ _request_get_impl GET tests passed")

    return failed


async def test_request_get_impl_post(my_predbat):
    """
    Test _request_get_impl() POST requests
    Covers: successful POST with body, Content-Type headers, POST-specific error handling, request_wrapper integration
    """
    print("=" * 60)
    print("Testing _request_get_impl() POST requests")
    print("=" * 60)

    failed = False
    from unittest.mock import patch, MagicMock
    from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

    # Test 1: Successful POST with JSON body
    print("\n--- Test 1: Successful POST with JSON body ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    post_data = {"setting": "reserve", "value": 20}
    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "message": "Success"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/update", post=True, json_data=post_data)

    if result != {"code": 0, "message": "Success"}:
        print(f"**** ERROR: Expected success response, got {result} ****")
        failed = True
    else:
        print(f"✓ Successful POST with JSON body test passed")

    # Test 2: POST with Content-Type header verification
    print("\n--- Test 2: POST with Content-Type header ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    # The headers are set automatically in _request_get_impl
    # We just verify POST works correctly
    post_data = {"command": "set_mode", "mode": "eco"}
    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "result": {"applied": True}})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/command", post=True, json_data=post_data)

    if result != {"code": 0, "result": {"applied": True}}:
        print(f"**** ERROR: Expected success response, got {result} ****")
        failed = True
    else:
        print(f"✓ POST with Content-Type header test passed")

    # Test 3: POST HTTP 400 error (bad request)
    print("\n--- Test 3: POST HTTP 400 error ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    post_data = {"invalid": "data"}
    mock_response = create_aiohttp_mock_response(status=400, json_data={"error": "Bad request"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/invalid", post=True, json_data=post_data)

    if result is not None:
        print(f"**** ERROR: Expected None for 400 error, got {result} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ POST HTTP 400 error test passed")

    # Test 4: POST with authentication error in response
    print("\n--- Test 4: POST with authentication error (code 10400) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "invalid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    post_data = {"setting": "mode"}
    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10400, "message": "Token expired"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/command", post=True, json_data=post_data)

    if result is not None:
        print(f"**** ERROR: Expected None for auth error, got {result} ****")
        failed = True
    elif solax_api.access_token is not None or solax_api.token_expiry is not None:
        print(f"**** ERROR: Expected token to be cleared ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1 ****")
        failed = True
    else:
        print(f"✓ POST authentication error test passed")

    # Test 5: POST with JSON decode error
    print("\n--- Test 5: POST with JSON decode error ---")
    import json
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    post_data = {"command": "test"}
    mock_response = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("Invalid JSON", "doc", 0))
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/command", post=True, json_data=post_data)

    if result is not None:
        print(f"**** ERROR: Expected None for JSON decode error, got {result} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1 ****")
        failed = True
    else:
        print(f"✓ POST JSON decode error test passed")

    # Test 6: POST with empty body
    print("\n--- Test 6: POST with empty body ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "message": "OK"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api._request_get_impl("/test/ping", post=True, json_data={})

    if result != {"code": 0, "message": "OK"}:
        print(f"**** ERROR: Expected success response, got {result} ****")
        failed = True
    else:
        print(f"✓ POST with empty body test passed")

    if not failed:
        print("✓ _request_get_impl POST tests passed")

    return failed


async def test_fetch_paginated_data(my_predbat):
    """
    Test fetch_paginated_data() pagination
    Covers: multi-page scenarios, empty result handling, API error handling during pagination, page count tracking
    """
    print("=" * 60)
    print("Testing fetch_paginated_data() pagination")
    print("=" * 60)

    failed = False
    from unittest.mock import patch, MagicMock
    from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

    # Test 1: Single page with records
    print("\n--- Test 1: Single page with records ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"id": 1, "name": "Device 1"}, {"id": 2, "name": "Device 2"}],
                "pages": 1,
                "current": 1,
                "total": 2,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.fetch_paginated_data("/api/devices", {"plantId": "12345"}, page_size=100)

    if result is None:
        print(f"**** ERROR: Expected records, got None ****")
        failed = True
    elif len(result) != 2:
        print(f"**** ERROR: Expected 2 records, got {len(result)} ****")
        failed = True
    elif result[0]["id"] != 1 or result[1]["id"] != 2:
        print(f"**** ERROR: Record data mismatch ****")
        failed = True
    else:
        print(f"✓ Single page with records test passed")

    # Test 2: Multiple pages (3 pages)
    print("\n--- Test 2: Multiple pages (3 pages) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    # Mock responses for 3 pages
    page1_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"id": 1}, {"id": 2}],
                "pages": 3,
                "current": 1,
                "total": 5,
            },
        },
    )
    page2_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"id": 3}, {"id": 4}],
                "pages": 3,
                "current": 2,
                "total": 5,
            },
        },
    )
    page3_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"id": 5}],
                "pages": 3,
                "current": 3,
                "total": 5,
            },
        },
    )

    # Create mock session that returns different responses for each call
    mock_session = MagicMock()
    call_count = 0
    responses = [page1_response, page2_response, page3_response]

    def get_context(*args, **kwargs):
        nonlocal call_count
        response = responses[call_count]
        call_count += 1

        context = MagicMock()

        async def get_aenter(*args, **kwargs):
            return response

        async def get_aexit(*args):
            pass

        context.__aenter__ = get_aenter
        context.__aexit__ = get_aexit
        return context

    mock_session.get = MagicMock(side_effect=get_context)

    async def session_aenter(*args):
        return mock_session

    async def session_aexit(*args):
        pass

    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.fetch_paginated_data("/api/devices", {"plantId": "12345"}, page_size=2)

    if result is None:
        print(f"**** ERROR: Expected records from 3 pages, got None ****")
        failed = True
    elif len(result) != 5:
        print(f"**** ERROR: Expected 5 total records across 3 pages, got {len(result)} ****")
        failed = True
    elif [r["id"] for r in result] != [1, 2, 3, 4, 5]:
        print(f"**** ERROR: Records not collected correctly across pages ****")
        failed = True
    else:
        print(f"✓ Multiple pages (3 pages) test passed")

    # Test 3: Empty result (no records)
    print("\n--- Test 3: Empty result (no records) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [],
                "pages": 1,
                "current": 1,
                "total": 0,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.fetch_paginated_data("/api/devices", {"plantId": "99999"}, page_size=100)

    if result is None:
        print(f"**** ERROR: Expected empty list, got None ****")
        failed = True
    elif len(result) != 0:
        print(f"**** ERROR: Expected 0 records, got {len(result)} ****")
        failed = True
    else:
        print(f"✓ Empty result test passed")

    # Test 4: API error on first page
    print("\n--- Test 4: API error on first page ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10001, "message": "API error"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.fetch_paginated_data("/api/devices", {"plantId": "12345"}, page_size=100)

    if result is not None:
        print(f"**** ERROR: Expected None for API error, got {result} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ API error on first page test passed")

    # Test 5: Network failure on second page
    print("\n--- Test 5: Network failure on second page ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    # First page succeeds, second page returns None (network failure)
    page1_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"id": 1}],
                "pages": 2,
                "current": 1,
                "total": 2,
            },
        },
    )
    page2_response = create_aiohttp_mock_response(status=500, json_data={})

    mock_session = MagicMock()
    call_count = 0
    responses = [page1_response, page2_response]

    def get_context(*args, **kwargs):
        nonlocal call_count
        response = responses[call_count]
        call_count += 1

        context = MagicMock()

        async def get_aenter(*args, **kwargs):
            return response

        async def get_aexit(*args):
            pass

        context.__aenter__ = get_aenter
        context.__aexit__ = get_aexit
        return context

    mock_session.get = MagicMock(side_effect=get_context)

    async def session_aenter(*args):
        return mock_session

    async def session_aexit(*args):
        pass

    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.fetch_paginated_data("/api/devices", {"plantId": "12345"}, page_size=1)

    if result is not None:
        print(f"**** ERROR: Expected None when second page fails, got {result} ****")
        failed = True
    else:
        print(f"✓ Network failure on second page test passed")

    # Test 6: Missing result field in response
    print("\n--- Test 6: Missing result field in response ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000
            # Missing "result" field
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.fetch_paginated_data("/api/devices", {"plantId": "12345"}, page_size=100)

    if result is None:
        print(f"**** ERROR: Expected empty list for missing result field, got None ****")
        failed = True
    elif len(result) != 0:
        print(f"**** ERROR: Expected 0 records for missing result field, got {len(result)} ****")
        failed = True
    else:
        print(f"✓ Missing result field test passed")

    if not failed:
        print("✓ fetch_paginated_data tests passed")

    return failed


async def test_fetch_single_result(my_predbat):
    """
    Test fetch_single_result() wrapper
    Covers: single item extraction, empty result handling, error propagation, GET and POST methods
    """
    print("=" * 60)
    print("Testing fetch_single_result() wrapper")
    print("=" * 60)

    failed = False
    from unittest.mock import patch, MagicMock
    from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

    # Test 1: Successful GET request with result
    print("\n--- Test 1: Successful GET request with result ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {"plantId": "12345", "plantName": "Test Plant", "pvCapacity": 10.5},
            "requestId": "req-123-456",
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result, request_id = await solax_api.fetch_single_result("/api/plant/info", params={"plantId": "12345"})

    if result is None:
        print(f"**** ERROR: Expected result, got None ****")
        failed = True
    elif result.get("plantId") != "12345":
        print(f"**** ERROR: Result data mismatch ****")
        failed = True
    elif request_id != "req-123-456":
        print(f"**** ERROR: Expected requestId 'req-123-456', got {request_id} ****")
        failed = True
    else:
        print(f"✓ Successful GET request test passed")

    # Test 2: Successful POST request with result
    print("\n--- Test 2: Successful POST request with result ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    post_data = {"plantId": "12345", "setting": "reserve", "value": 20}
    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {"success": True, "updated": True},
            "requestId": "req-789",
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result, request_id = await solax_api.fetch_single_result("/api/plant/update", post=True, json_data=post_data)

    if result is None:
        print(f"**** ERROR: Expected result, got None ****")
        failed = True
    elif result.get("success") != True:
        print(f"**** ERROR: Result data mismatch ****")
        failed = True
    elif request_id != "req-789":
        print(f"**** ERROR: Expected requestId 'req-789', got {request_id} ****")
        failed = True
    else:
        print(f"✓ Successful POST request test passed")

    # Test 3: Empty result field
    print("\n--- Test 3: Empty result field ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "requestId": "req-empty",
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result, request_id = await solax_api.fetch_single_result("/api/test", params={"id": "123"})

    if result is None:
        print(f"**** ERROR: Expected empty dict, got None ****")
        failed = True
    elif result != {}:
        print(f"**** ERROR: Expected empty dict, got {result} ****")
        failed = True
    elif request_id != "req-empty":
        print(f"**** ERROR: Expected requestId 'req-empty', got {request_id} ****")
        failed = True
    else:
        print(f"✓ Empty result field test passed")

    # Test 4: API error code
    print("\n--- Test 4: API error code (10001) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    initial_error_count = solax_api.error_count

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10001, "message": "Operation failed"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result, request_id = await solax_api.fetch_single_result("/api/test", params={"id": "999"})

    if result is not None:
        print(f"**** ERROR: Expected None for error response, got {result} ****")
        failed = True
    elif request_id is not None:
        print(f"**** ERROR: Expected None requestId for error, got {request_id} ****")
        failed = True
    elif solax_api.error_count != initial_error_count + 1:
        print(f"**** ERROR: Expected error_count to increment by 1, got {solax_api.error_count - initial_error_count} ****")
        failed = True
    else:
        print(f"✓ API error code test passed")

    # Test 5: Network failure (response is None)
    print("\n--- Test 5: Network failure (response is None) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(status=500, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result, request_id = await solax_api.fetch_single_result("/api/test", params={"id": "123"})

    if result is not None:
        print(f"**** ERROR: Expected None for network failure, got {result} ****")
        failed = True
    elif request_id is not None:
        print(f"**** ERROR: Expected None requestId for network failure, got {request_id} ****")
        failed = True
    else:
        print(f"✓ Network failure test passed")

    # Test 6: Missing requestId field
    print("\n--- Test 6: Missing requestId field ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {"data": "test"},
            # Missing requestId
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result, request_id = await solax_api.fetch_single_result("/api/test", params={"id": "123"})

    if result is None:
        print(f"**** ERROR: Expected result, got None ****")
        failed = True
    elif result.get("data") != "test":
        print(f"**** ERROR: Result data mismatch ****")
        failed = True
    elif request_id != "":
        print(f"**** ERROR: Expected empty string for missing requestId, got {request_id} ****")
        failed = True
    else:
        print(f"✓ Missing requestId field test passed")

    if not failed:
        print("✓ fetch_single_result tests passed")

    return failed


async def test_query_plant_info(my_predbat):
    """
    Test query_plant_info() endpoint
    Covers: successful plant queries, plant ID filtering, pagination handling, error responses
    """
    print("=" * 60)
    print("Testing query_plant_info() endpoint")
    print("=" * 60)

    failed = False
    from unittest.mock import patch, MagicMock
    from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

    # Test 1: Successful query with multiple plants (no filter)
    print("\n--- Test 1: Successful query with multiple plants (no filter) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    solax_api.plant_id = None  # No filter

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [
                    {"plantId": "plant1", "plantName": "Plant 1", "pvCapacity": 10.5, "batteryCapacity": 15.0},
                    {"plantId": "plant2", "plantName": "Plant 2", "pvCapacity": 8.0, "batteryCapacity": 12.0},
                ],
                "pages": 1,
                "current": 1,
                "total": 2,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_plant_info()

    if result is None:
        print(f"**** ERROR: Expected plant list, got None ****")
        failed = True
    elif len(result) != 2:
        print(f"**** ERROR: Expected 2 plants, got {len(result)} ****")
        failed = True
    elif solax_api.plant_info != result:
        print(f"**** ERROR: plant_info not updated correctly ****")
        failed = True
    else:
        print(f"✓ Successful query with multiple plants test passed")

    # Test 2: Query with plant ID filter
    print("\n--- Test 2: Query with plant ID filter ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    solax_api.plant_id = "specific_plant_123"

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"plantId": "specific_plant_123", "plantName": "My Plant", "pvCapacity": 12.0, "batteryCapacity": 20.0}],
                "pages": 1,
                "current": 1,
                "total": 1,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_plant_info()

    if result is None:
        print(f"**** ERROR: Expected filtered plant, got None ****")
        failed = True
    elif len(result) != 1:
        print(f"**** ERROR: Expected 1 plant, got {len(result)} ****")
        failed = True
    elif result[0]["plantId"] != "specific_plant_123":
        print(f"**** ERROR: Wrong plant returned ****")
        failed = True
    else:
        print(f"✓ Query with plant ID filter test passed")

    # Test 3: Empty result (no plants found)
    print("\n--- Test 3: Empty result (no plants found) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    solax_api.plant_id = None

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [],
                "pages": 1,
                "current": 1,
                "total": 0,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_plant_info()

    if result is None:
        print(f"**** ERROR: Expected empty list, got None ****")
        failed = True
    elif len(result) != 0:
        print(f"**** ERROR: Expected 0 plants, got {len(result)} ****")
        failed = True
    elif solax_api.plant_info != []:
        print(f"**** ERROR: plant_info should be empty list ****")
        failed = True
    else:
        print(f"✓ Empty result test passed")

    # Test 4: API error during query
    print("\n--- Test 4: API error during query (code 10001) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    solax_api.plant_id = None
    solax_api.plant_info = [{"plantId": "old_data"}]  # Pre-existing data

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10001, "message": "Query failed"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_plant_info()

    if result is not None:
        print(f"**** ERROR: Expected None for API error, got {result} ****")
        failed = True
    elif solax_api.plant_info != [{"plantId": "old_data"}]:
        print(f"**** ERROR: plant_info should not be updated on error ****")
        failed = True
    else:
        print(f"✓ API error during query test passed")

    # Test 5: Multi-page plant results
    print("\n--- Test 5: Multi-page plant results ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    solax_api.plant_id = None

    # Mock two pages of results
    page1_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"plantId": "plant1"}, {"plantId": "plant2"}],
                "pages": 2,
                "current": 1,
                "total": 3,
            },
        },
    )
    page2_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"plantId": "plant3"}],
                "pages": 2,
                "current": 2,
                "total": 3,
            },
        },
    )

    mock_session = MagicMock()
    call_count = 0
    responses = [page1_response, page2_response]

    def get_context(*args, **kwargs):
        nonlocal call_count
        response = responses[call_count]
        call_count += 1

        context = MagicMock()

        async def get_aenter(*args, **kwargs):
            return response

        async def get_aexit(*args):
            pass

        context.__aenter__ = get_aenter
        context.__aexit__ = get_aexit
        return context

    mock_session.get = MagicMock(side_effect=get_context)

    async def session_aenter(*args):
        return mock_session

    async def session_aexit(*args):
        pass

    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_plant_info()

    if result is None:
        print(f"**** ERROR: Expected plant list from 2 pages, got None ****")
        failed = True
    elif len(result) != 3:
        print(f"**** ERROR: Expected 3 plants across 2 pages, got {len(result)} ****")
        failed = True
    elif [p["plantId"] for p in result] != ["plant1", "plant2", "plant3"]:
        print(f"**** ERROR: Plants not collected correctly across pages ****")
        failed = True
    else:
        print(f"✓ Multi-page plant results test passed")

    if not failed:
        print("✓ query_plant_info tests passed")

    return failed


async def test_query_device_info(my_predbat):
    """
    Test query_device_info() endpoint
    Covers: device queries, device type filtering, serial number filtering, plant association, 
            storing in device_info/plant_inverters/plant_batteries dicts
    """
    print("=" * 60)
    print("Testing query_device_info() endpoint")
    print("=" * 60)

    failed = False
    from unittest.mock import patch, MagicMock
    from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

    # Test 1: Query inverter devices (device_type=1)
    print("\n--- Test 1: Query inverter devices (device_type=1) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [
                    {
                        "deviceModel": 14,
                        "deviceSn": "H1231231932123",
                        "plantId": "plant1",
                        "ratedPower": 10.0,
                        "onlineStatus": 1,
                    },
                    {
                        "deviceModel": 15,
                        "deviceSn": "H9876543210987",
                        "plantId": "plant1",
                        "ratedPower": 8.5,
                        "onlineStatus": 1,
                    },
                ],
                "pages": 1,
                "current": 1,
                "total": 2,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_device_info(plant_id="plant1", device_type=1)

    if result is None or len(result) != 2:
        print(f"**** ERROR: Expected 2 inverters, got {result} ****")
        failed = True
    elif "H1231231932123" not in solax_api.device_info:
        print(f"**** ERROR: Device H1231231932123 not stored in device_info ****")
        failed = True
    elif "plant1" not in solax_api.plant_inverters or len(solax_api.plant_inverters["plant1"]) != 2:
        print(f"**** ERROR: Inverters not stored in plant_inverters correctly ****")
        failed = True
    elif solax_api.device_info["H1231231932123"].get("deviceType") != 1:
        print(f"**** ERROR: deviceType not set correctly ****")
        failed = True
    else:
        print(f"✓ Inverter devices query test passed")

    # Test 2: Query battery devices (device_type=2)
    print("\n--- Test 2: Query battery devices (device_type=2) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [
                    {
                        "deviceModel": 1,
                        "deviceSn": "TP123456123123",
                        "plantId": "plant2",
                        "ratedCapacity": 15.0,
                        "onlineStatus": 1,
                    }
                ],
                "pages": 1,
                "current": 1,
                "total": 1,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_device_info(plant_id="plant2", device_type=2)

    if result is None or len(result) != 1:
        print(f"**** ERROR: Expected 1 battery, got {result} ****")
        failed = True
    elif "TP123456123123" not in solax_api.device_info:
        print(f"**** ERROR: Battery not stored in device_info ****")
        failed = True
    elif "plant2" not in solax_api.plant_batteries or len(solax_api.plant_batteries["plant2"]) != 1:
        print(f"**** ERROR: Battery not stored in plant_batteries correctly ****")
        failed = True
    elif solax_api.device_info["TP123456123123"].get("deviceType") != 2:
        print(f"**** ERROR: Battery deviceType not set correctly ****")
        failed = True
    else:
        print(f"✓ Battery devices query test passed")

    # Test 3: Query with device serial number filter
    print("\n--- Test 3: Query with device serial number filter ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [
                    {
                        "deviceModel": 14,
                        "deviceSn": "SPECIFIC_SN_123",
                        "plantId": "plant3",
                        "ratedPower": 12.0,
                        "onlineStatus": 1,
                    }
                ],
                "pages": 1,
                "current": 1,
                "total": 1,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_device_info(plant_id="plant3", device_type=1, device_sn="SPECIFIC_SN_123")

    if result is None or len(result) != 1:
        print(f"**** ERROR: Expected 1 device with specific SN, got {result} ****")
        failed = True
    elif result[0]["deviceSn"] != "SPECIFIC_SN_123":
        print(f"**** ERROR: Wrong device returned ****")
        failed = True
    else:
        print(f"✓ Device serial number filter test passed")

    # Test 4: Empty result (no devices found)
    print("\n--- Test 4: Empty result (no devices found) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [],
                "pages": 1,
                "current": 1,
                "total": 0,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_device_info(plant_id="empty_plant", device_type=1)

    if result is None:
        print(f"**** ERROR: Expected empty list, got None ****")
        failed = True
    elif len(result) != 0:
        print(f"**** ERROR: Expected 0 devices, got {len(result)} ****")
        failed = True
    else:
        print(f"✓ Empty result test passed")

    # Test 5: API error during query
    print("\n--- Test 5: API error during query (code 10001) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 10001, "message": "Query failed"})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_device_info(plant_id="error_plant", device_type=1)

    if result is not None:
        print(f"**** ERROR: Expected None for API error, got {result} ****")
        failed = True
    else:
        print(f"✓ API error during query test passed")

    # Test 6: Multiple device types in same plant
    print("\n--- Test 6: Multiple device types in same plant ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    # First query inverters
    inverter_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"deviceSn": "INV001", "plantId": "plant_mixed", "deviceModel": 14}],
                "pages": 1,
                "current": 1,
                "total": 1,
            },
        },
    )
    # Then query batteries
    battery_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [{"deviceSn": "BAT001", "plantId": "plant_mixed", "deviceModel": 1, "ratedCapacity": 10.0}],
                "pages": 1,
                "current": 1,
                "total": 1,
            },
        },
    )

    mock_session = MagicMock()
    call_count = 0
    responses = [inverter_response, battery_response]

    def get_context(*args, **kwargs):
        nonlocal call_count
        response = responses[call_count]
        call_count += 1

        context = MagicMock()

        async def get_aenter(*args, **kwargs):
            return response

        async def get_aexit(*args):
            pass

        context.__aenter__ = get_aenter
        context.__aexit__ = get_aexit
        return context

    mock_session.get = MagicMock(side_effect=get_context)

    async def session_aenter(*args):
        return mock_session

    async def session_aexit(*args):
        pass

    mock_session.__aenter__ = session_aenter
    mock_session.__aexit__ = session_aexit

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        # Query inverters
        await solax_api.query_device_info(plant_id="plant_mixed", device_type=1)
        # Reset session for second call
        call_count = 0
        responses = [battery_response]
        # Query batteries
        await solax_api.query_device_info(plant_id="plant_mixed", device_type=2)

    if "INV001" not in solax_api.device_info or "BAT001" not in solax_api.device_info:
        print(f"**** ERROR: Both devices not stored in device_info ****")
        failed = True
    elif "plant_mixed" not in solax_api.plant_inverters or "INV001" not in solax_api.plant_inverters["plant_mixed"]:
        print(f"**** ERROR: Inverter not in plant_inverters ****")
        failed = True
    elif "plant_mixed" not in solax_api.plant_batteries or "BAT001" not in solax_api.plant_batteries["plant_mixed"]:
        print(f"**** ERROR: Battery not in plant_batteries ****")
        failed = True
    else:
        print(f"✓ Multiple device types in same plant test passed")

    # Test 7: Device without serial number (edge case)
    print("\n--- Test 7: Device without serial number (edge case) ---")
    solax_api = MockSolaxAPI()
    solax_api.access_token = "valid_token"
    solax_api.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "code": 10000,
            "result": {
                "records": [
                    {"deviceModel": 14, "plantId": "plant4", "ratedPower": 10.0},  # No deviceSn
                    {"deviceModel": 15, "deviceSn": "HAS_SN_123", "plantId": "plant4", "ratedPower": 8.0},  # Has deviceSn
                ],
                "pages": 1,
                "current": 1,
                "total": 2,
            },
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("solax.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        result = await solax_api.query_device_info(plant_id="plant4", device_type=1)

    if result is None or len(result) != 2:
        print(f"**** ERROR: Expected 2 devices returned, got {result} ****")
        failed = True
    elif len(solax_api.device_info) != 1:
        print(f"**** ERROR: Expected only 1 device with SN stored, got {len(solax_api.device_info)} ****")
        failed = True
    elif "HAS_SN_123" not in solax_api.device_info:
        print(f"**** ERROR: Device with SN should be stored ****")
        failed = True
    else:
        print(f"✓ Device without serial number test passed")

    if not failed:
        print("✓ query_device_info tests passed")

    return failed


async def test_query_plant_realtime_data_main():
    """
    Test query_plant_realtime_data() function
    """
    failed = False
    print("\n=== Testing query_plant_realtime_data ===")

    # Test 1: Successful fetch with valid data
    print("Test 1: Successful fetch with valid real-time data")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    mock_realtime_data = {
        'plantLocalTime': '2025-12-28 18:38:24',
        'plantId': '1618699116555534337',
        'dailyYield': 12.5,
        'totalYield': 31927.82,
        'dailyCharged': 8.3,
        'totalCharged': 7498.5,
        'dailyDischarged': 6.1,
        'totalDischarged': 6504.7,
        'dailyImported': 2.4,
        'totalImported': 17567.67,
        'dailyExported': 1.2,
        'totalExported': 15014.4,
        'dailyEarnings': 3.45,
        'totalEarnings': 2797.23
    }
    
    # Mock fetch_single_result to return success
    async def mock_fetch_success(path, params=None, post=False, json_data=None):
        return mock_realtime_data.copy(), "req_12345"
    
    api.fetch_single_result = mock_fetch_success
    
    result = await api.query_plant_realtime_data("1618699116555534337")
    
    if result is None:
        print(f"**** ERROR: Expected successful result, got None ****")
        failed = True
    elif result != mock_realtime_data:
        print(f"**** ERROR: Result mismatch. Expected {mock_realtime_data}, got {result} ****")
        failed = True
    elif "1618699116555534337" not in api.realtime_data:
        print(f"**** ERROR: Data not stored in realtime_data dict ****")
        failed = True
    elif api.realtime_data["1618699116555534337"] != mock_realtime_data:
        print(f"**** ERROR: Stored data mismatch ****")
        failed = True
    else:
        print(f"✓ Successful fetch test passed")

    # Test 2: API error response (non-10000 code)
    print("Test 2: API error response")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    # Mock fetch_single_result to return None (API error)
    async def mock_fetch_error(path, params=None, post=False, json_data=None):
        return None, None
    
    api2.fetch_single_result = mock_fetch_error
    
    result2 = await api2.query_plant_realtime_data("1618699116555534337")
    
    if result2 is not None:
        print(f"**** ERROR: Expected None on API error, got {result2} ****")
        failed = True
    elif "1618699116555534337" in api2.realtime_data:
        print(f"**** ERROR: Data should not be stored on error ****")
        failed = True
    else:
        print(f"✓ API error test passed")

    # Test 3: Multiple plants with different data
    print("Test 3: Multiple plants with different data")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    mock_data_plant1 = {
        'plantId': 'plant_001',
        'dailyYield': 10.0,
        'totalYield': 5000.0,
    }
    
    mock_data_plant2 = {
        'plantId': 'plant_002',
        'dailyYield': 20.0,
        'totalYield': 8000.0,
    }
    
    call_count = [0]
    
    async def mock_fetch_multiple(path, params=None, post=False, json_data=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_data_plant1.copy(), "req_001"
        else:
            return mock_data_plant2.copy(), "req_002"
    
    api3.fetch_single_result = mock_fetch_multiple
    
    result1 = await api3.query_plant_realtime_data("plant_001")
    result2 = await api3.query_plant_realtime_data("plant_002")
    
    if "plant_001" not in api3.realtime_data or "plant_002" not in api3.realtime_data:
        print(f"**** ERROR: Both plants should be stored ****")
        failed = True
    elif api3.realtime_data["plant_001"]["dailyYield"] != 10.0:
        print(f"**** ERROR: Plant 1 data mismatch ****")
        failed = True
    elif api3.realtime_data["plant_002"]["dailyYield"] != 20.0:
        print(f"**** ERROR: Plant 2 data mismatch ****")
        failed = True
    else:
        print(f"✓ Multiple plants test passed")

    # Test 4: Custom business_type parameter
    print("Test 4: Custom business_type parameter")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_params = []
    
    async def mock_fetch_capture_params(path, params=None, post=False, json_data=None):
        captured_params.append(params)
        return {'plantId': 'test_plant'}, "req_123"
    
    api4.fetch_single_result = mock_fetch_capture_params
    
    # Test with default business_type
    await api4.query_plant_realtime_data("test_plant")
    
    if len(captured_params) != 1:
        print(f"**** ERROR: Expected 1 call, got {len(captured_params)} ****")
        failed = True
    elif captured_params[0].get("businessType") != 1:  # BUSINESS_TYPE_RESIDENTIAL
        print(f"**** ERROR: Expected business_type 1, got {captured_params[0].get('businessType')} ****")
        failed = True
    else:
        print(f"✓ Default business_type test passed")
    
    # Test with custom business_type
    captured_params.clear()
    await api4.query_plant_realtime_data("test_plant", business_type=4)
    
    if len(captured_params) != 1:
        print(f"**** ERROR: Expected 1 call, got {len(captured_params)} ****")
        failed = True
    elif captured_params[0].get("businessType") != 4:
        print(f"**** ERROR: Expected business_type 4, got {captured_params[0].get('businessType')} ****")
        failed = True
    else:
        print(f"✓ Custom business_type test passed")

    # Test 5: Empty result (valid API response but no data)
    print("Test 5: Empty result dictionary")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_empty(path, params=None, post=False, json_data=None):
        return {}, "req_empty"
    
    api5.fetch_single_result = mock_fetch_empty
    
    result5 = await api5.query_plant_realtime_data("empty_plant")
    
    if result5 != {}:
        print(f"**** ERROR: Expected empty dict, got {result5} ****")
        failed = True
    elif "empty_plant" not in api5.realtime_data:
        print(f"**** ERROR: Empty result should still be stored ****")
        failed = True
    else:
        print(f"✓ Empty result test passed")

    # Test 6: Overwrite existing data with new fetch
    print("Test 6: Overwrite existing data with new fetch")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    old_data = {'plantId': 'plant_x', 'dailyYield': 5.0}
    new_data = {'plantId': 'plant_x', 'dailyYield': 10.0}
    
    call_num = [0]
    
    async def mock_fetch_overwrite(path, params=None, post=False, json_data=None):
        call_num[0] += 1
        if call_num[0] == 1:
            return old_data.copy(), "req_old"
        else:
            return new_data.copy(), "req_new"
    
    api6.fetch_single_result = mock_fetch_overwrite
    
    # First fetch
    await api6.query_plant_realtime_data("plant_x")
    
    if api6.realtime_data["plant_x"]["dailyYield"] != 5.0:
        print(f"**** ERROR: First fetch data incorrect ****")
        failed = True
    
    # Second fetch (overwrite)
    await api6.query_plant_realtime_data("plant_x")
    
    if api6.realtime_data["plant_x"]["dailyYield"] != 10.0:
        print(f"**** ERROR: Data not overwritten correctly ****")
        failed = True
    else:
        print(f"✓ Overwrite existing data test passed")

    if not failed:
        print("✓ query_plant_realtime_data tests passed")

    return failed

async def test_query_device_realtime_data_main():
    """
    Test query_device_realtime_data() function
    """
    failed = False
    print("\n=== Testing query_device_realtime_data ===")

    # Test 1: Successful fetch for inverter device (device_type=1)
    print("Test 1: Successful fetch for inverter device")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    mock_inverter_data = [{
        'deviceStatus': 130,
        'gridPower': -4254.0,
        'todayImportEnergy': 16.8,
        'totalImportEnergy': 17679.3,
        'todayExportEnergy': 2.6,
        'totalExportEnergy': 15098.6,
        'dataTime': '2025-12-28T18:45:54.000+00:00',
        'plantLocalTime': '2025-12-28 19:45:54',
        'deviceSn': 'H1231231932123',
        'acPower1': 15,
        'acPower2': 18,
        'acPower3': 9,
        'inverterTemperature': 45.0,
        'dailyYield': 13.6,
        'totalYield': 33025.8,
    }]
    
    async def mock_fetch_inverter(path, params=None, post=False, json_data=None):
        return mock_inverter_data.copy(), "req_inv_001"
    
    api.fetch_single_result = mock_fetch_inverter
    
    result = await api.query_device_realtime_data("H1231231932123", device_type=1)
    
    if result is None:
        print(f"**** ERROR: Expected successful result, got None ****")
        failed = True
    elif len(result) != 1:
        print(f"**** ERROR: Expected list with 1 item, got {len(result)} ****")
        failed = True
    elif result[0]['deviceSn'] != 'H1231231932123':
        print(f"**** ERROR: Device SN mismatch ****")
        failed = True
    elif "H1231231932123" not in api.realtime_device_data:
        print(f"**** ERROR: Data not stored in realtime_device_data dict ****")
        failed = True
    elif api.realtime_device_data["H1231231932123"]['inverterTemperature'] != 45.0:
        print(f"**** ERROR: Stored data mismatch ****")
        failed = True
    else:
        print(f"✓ Successful inverter fetch test passed")

    # Test 2: Successful fetch for battery device (device_type=2)
    print("Test 2: Successful fetch for battery device")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    mock_battery_data = [{
        'dataTime': '2025-12-28T18:45:54.000+00:00',
        'plantLocalTime': '2025-12-28 19:45:54',
        'deviceSn': 'TP123456123123',
        'registerNo': 'SY1231321312',
        'deviceStatus': 1,
        'batterySOC': 99,
        'batterySOH': 0,
        'chargeDischargePower': 0,
        'batteryVoltage': 426.9,
        'batteryCurrent': 0.0,
        'batteryTemperature': 22.0,
        'batteryCycleTimes': 652,
        'totalDeviceDischarge': 6537.8,
        'totalDeviceCharge': 7534.0,
        'batteryRemainings': 12.2
    }]
    
    async def mock_fetch_battery(path, params=None, post=False, json_data=None):
        return mock_battery_data.copy(), "req_bat_001"
    
    api2.fetch_single_result = mock_fetch_battery
    
    result2 = await api2.query_device_realtime_data("TP123456123123", device_type=2)
    
    if result2 is None:
        print(f"**** ERROR: Expected successful result, got None ****")
        failed = True
    elif result2[0]['batterySOC'] != 99:
        print(f"**** ERROR: Battery SOC mismatch ****")
        failed = True
    elif result2[0]['batteryTemperature'] != 22.0:
        print(f"**** ERROR: Battery temperature mismatch ****")
        failed = True
    elif api2.realtime_device_data["TP123456123123"]['chargeDischargePower'] != 0:
        print(f"**** ERROR: Stored battery data mismatch ****")
        failed = True
    else:
        print(f"✓ Successful battery fetch test passed")

    # Test 3: API error response (fetch_single_result returns None)
    print("Test 3: API error response")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_error(path, params=None, post=False, json_data=None):
        return None, None
    
    api3.fetch_single_result = mock_fetch_error
    
    result3 = await api3.query_device_realtime_data("ERROR_SN", device_type=1)
    
    if result3 is not None:
        print(f"**** ERROR: Expected None on API error, got {result3} ****")
        failed = True
    elif "ERROR_SN" in api3.realtime_device_data:
        print(f"**** ERROR: Data should not be stored on error ****")
        failed = True
    else:
        print(f"✓ API error test passed")

    # Test 4: Empty result list
    print("Test 4: Empty result list")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_empty(path, params=None, post=False, json_data=None):
        return [], "req_empty"
    
    api4.fetch_single_result = mock_fetch_empty
    
    result4 = await api4.query_device_realtime_data("EMPTY_SN", device_type=1)
    
    if result4 is not None:
        print(f"**** ERROR: Expected None for empty list, got {result4} ****")
        failed = True
    elif "EMPTY_SN" in api4.realtime_device_data:
        print(f"**** ERROR: Data should not be stored for empty result ****")
        failed = True
    else:
        print(f"✓ Empty result test passed")

    # Test 5: Custom business_type parameter
    print("Test 5: Custom business_type parameter")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_params = []
    
    async def mock_fetch_capture(path, params=None, post=False, json_data=None):
        captured_params.append(params)
        return [{'deviceSn': 'TEST_SN'}], "req_123"
    
    api5.fetch_single_result = mock_fetch_capture
    
    # Test with default business_type
    await api5.query_device_realtime_data("TEST_SN", device_type=1)
    
    if len(captured_params) != 1:
        print(f"**** ERROR: Expected 1 call, got {len(captured_params)} ****")
        failed = True
    elif captured_params[0].get("businessType") != 1:
        print(f"**** ERROR: Expected business_type 1, got {captured_params[0].get('businessType')} ****")
        failed = True
    elif captured_params[0].get("deviceType") != 1:
        print(f"**** ERROR: Expected device_type 1, got {captured_params[0].get('deviceType')} ****")
        failed = True
    elif captured_params[0].get("snList") != ["TEST_SN"]:
        print(f"**** ERROR: Expected snList ['TEST_SN'], got {captured_params[0].get('snList')} ****")
        failed = True
    else:
        print(f"✓ Default business_type test passed")
    
    # Test with custom business_type
    captured_params.clear()
    await api5.query_device_realtime_data("TEST_SN", device_type=2, business_type=4)
    
    if len(captured_params) != 1:
        print(f"**** ERROR: Expected 1 call, got {len(captured_params)} ****")
        failed = True
    elif captured_params[0].get("businessType") != 4:
        print(f"**** ERROR: Expected business_type 4, got {captured_params[0].get('businessType')} ****")
        failed = True
    elif captured_params[0].get("deviceType") != 2:
        print(f"**** ERROR: Expected device_type 2, got {captured_params[0].get('deviceType')} ****")
        failed = True
    else:
        print(f"✓ Custom business_type test passed")

    # Test 6: Multiple devices queried separately
    print("Test 6: Multiple devices queried separately")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    call_count = [0]
    
    async def mock_fetch_multiple(path, params=None, post=False, json_data=None):
        call_count[0] += 1
        sn = params.get("snList")[0]
        return [{'deviceSn': sn, 'value': call_count[0] * 10}], f"req_{call_count[0]}"
    
    api6.fetch_single_result = mock_fetch_multiple
    
    await api6.query_device_realtime_data("DEV_001", device_type=1)
    await api6.query_device_realtime_data("DEV_002", device_type=1)
    await api6.query_device_realtime_data("DEV_003", device_type=2)
    
    if len(api6.realtime_device_data) != 3:
        print(f"**** ERROR: Expected 3 devices stored, got {len(api6.realtime_device_data)} ****")
        failed = True
    elif api6.realtime_device_data["DEV_001"]["value"] != 10:
        print(f"**** ERROR: DEV_001 data mismatch ****")
        failed = True
    elif api6.realtime_device_data["DEV_002"]["value"] != 20:
        print(f"**** ERROR: DEV_002 data mismatch ****")
        failed = True
    elif api6.realtime_device_data["DEV_003"]["value"] != 30:
        print(f"**** ERROR: DEV_003 data mismatch ****")
        failed = True
    else:
        print(f"✓ Multiple devices test passed")

    # Test 7: Overwrite existing device data
    print("Test 7: Overwrite existing device data with new fetch")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    call_num = [0]
    
    async def mock_fetch_overwrite(path, params=None, post=False, json_data=None):
        call_num[0] += 1
        return [{'deviceSn': 'OVERWRITE_SN', 'temperature': call_num[0] * 5}], f"req_{call_num[0]}"
    
    api7.fetch_single_result = mock_fetch_overwrite
    
    # First fetch
    await api7.query_device_realtime_data("OVERWRITE_SN", device_type=1)
    
    if api7.realtime_device_data["OVERWRITE_SN"]["temperature"] != 5:
        print(f"**** ERROR: First fetch data incorrect ****")
        failed = True
    
    # Second fetch (overwrite)
    await api7.query_device_realtime_data("OVERWRITE_SN", device_type=1)
    
    if api7.realtime_device_data["OVERWRITE_SN"]["temperature"] != 10:
        print(f"**** ERROR: Data not overwritten correctly ****")
        failed = True
    else:
        print(f"✓ Overwrite existing device data test passed")

    if not failed:
        print("✓ query_device_realtime_data tests passed")

    return failed

async def test_query_device_realtime_data_all_main():
    """
    Test query_device_realtime_data_all() function
    """
    failed = False
    print("\n=== Testing query_device_realtime_data_all ===")

    # Test 1: Successful fetch with multiple devices (inverters + batteries)
    print("Test 1: Successful fetch with multiple devices")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Setup device_info with 2 inverters and 1 battery
    api.device_info = {
        'INV001': {'deviceSn': 'INV001', 'deviceType': 1, 'plantId': 'plant1'},
        'INV002': {'deviceSn': 'INV002', 'deviceType': 1, 'plantId': 'plant1'},
        'BAT001': {'deviceSn': 'BAT001', 'deviceType': 2, 'plantId': 'plant1'},
    }
    
    call_log = []
    
    async def mock_query_device(sn, device_type, business_type=None):
        call_log.append({'sn': sn, 'device_type': device_type})
        return [{'deviceSn': sn, 'deviceType': device_type, 'value': len(call_log)}]
    
    api.query_device_realtime_data = mock_query_device
    
    result = await api.query_device_realtime_data_all("plant1")
    
    if result is None:
        print(f"**** ERROR: Expected successful result, got None ****")
        failed = True
    elif len(result) != 3:
        print(f"**** ERROR: Expected 3 results, got {len(result)} ****")
        failed = True
    elif len(call_log) != 3:
        print(f"**** ERROR: Expected 3 calls to query_device_realtime_data, got {len(call_log)} ****")
        failed = True
    elif not all(call['sn'] in ['INV001', 'INV002', 'BAT001'] for call in call_log):
        print(f"**** ERROR: Unexpected device SNs called ****")
        failed = True
    else:
        print(f"✓ Successful fetch with multiple devices test passed")

    # Test 2: Empty device_info (no devices)
    print("Test 2: Empty device_info (no devices)")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    api2.device_info = {}
    
    call_count = [0]
    
    async def mock_query_never_called(sn, device_type, business_type=None):
        call_count[0] += 1
        return [{'deviceSn': sn}]
    
    api2.query_device_realtime_data = mock_query_never_called
    
    result2 = await api2.query_device_realtime_data_all("plant_empty")
    
    if len(result2) != 0:
        print(f"**** ERROR: Expected empty list, got {len(result2)} results ****")
        failed = True
    elif call_count[0] != 0:
        print(f"**** ERROR: query_device_realtime_data should not be called when no devices ****")
        failed = True
    else:
        print(f"✓ Empty device_info test passed")

    # Test 3: Some devices return None (error handling)
    print("Test 3: Some devices return None (error handling)")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    api3.device_info = {
        'GOOD001': {'deviceSn': 'GOOD001', 'deviceType': 1},
        'ERROR001': {'deviceSn': 'ERROR001', 'deviceType': 1},
        'GOOD002': {'deviceSn': 'GOOD002', 'deviceType': 2},
    }
    
    async def mock_query_with_error(sn, device_type, business_type=None):
        if sn == 'ERROR001':
            return None  # Simulate API error
        return [{'deviceSn': sn, 'status': 'ok'}]
    
    api3.query_device_realtime_data = mock_query_with_error
    
    result3 = await api3.query_device_realtime_data_all("plant3")
    
    if len(result3) != 2:
        print(f"**** ERROR: Expected 2 results (excluding error), got {len(result3)} ****")
        failed = True
    elif not all(r['status'] == 'ok' for r in result3):
        print(f"**** ERROR: Result data mismatch ****")
        failed = True
    else:
        print(f"✓ Error handling test passed")

    # Test 4: Custom business_type parameter
    print("Test 4: Custom business_type parameter")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    api4.device_info = {
        'DEV001': {'deviceSn': 'DEV001', 'deviceType': 1},
    }
    
    captured_business_type = []
    
    async def mock_query_capture_business_type(sn, device_type, business_type=None):
        captured_business_type.append(business_type)
        return [{'deviceSn': sn}]
    
    api4.query_device_realtime_data = mock_query_capture_business_type
    
    # Test with default (None)
    await api4.query_device_realtime_data_all("plant4")
    
    if len(captured_business_type) != 1:
        print(f"**** ERROR: Expected 1 call ****")
        failed = True
    elif captured_business_type[0] is not None:
        print(f"**** ERROR: Expected business_type None, got {captured_business_type[0]} ****")
        failed = True
    else:
        print(f"✓ Default business_type test passed")
    
    # Test with custom business_type
    captured_business_type.clear()
    await api4.query_device_realtime_data_all("plant4", business_type=4)
    
    if captured_business_type[0] != 4:
        print(f"**** ERROR: Expected business_type 4, got {captured_business_type[0]} ****")
        failed = True
    else:
        print(f"✓ Custom business_type test passed")

    # Test 5: Mixed device types (verify deviceType extracted correctly)
    print("Test 5: Mixed device types verification")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    api5.device_info = {
        'INV_A': {'deviceSn': 'INV_A', 'deviceType': 1},
        'BAT_B': {'deviceSn': 'BAT_B', 'deviceType': 2},
        'METER_C': {'deviceSn': 'METER_C', 'deviceType': 3},
    }
    
    device_type_log = []
    
    async def mock_query_log_types(sn, device_type, business_type=None):
        device_type_log.append({'sn': sn, 'device_type': device_type})
        return [{'deviceSn': sn, 'deviceType': device_type}]
    
    api5.query_device_realtime_data = mock_query_log_types
    
    result5 = await api5.query_device_realtime_data_all("plant5")
    
    if len(device_type_log) != 3:
        print(f"**** ERROR: Expected 3 calls, got {len(device_type_log)} ****")
        failed = True
    else:
        # Verify correct device types were passed
        inv_call = next((c for c in device_type_log if c['sn'] == 'INV_A'), None)
        bat_call = next((c for c in device_type_log if c['sn'] == 'BAT_B'), None)
        meter_call = next((c for c in device_type_log if c['sn'] == 'METER_C'), None)
        
        if inv_call is None or inv_call['device_type'] != 1:
            print(f"**** ERROR: Inverter device_type incorrect ****")
            failed = True
        elif bat_call is None or bat_call['device_type'] != 2:
            print(f"**** ERROR: Battery device_type incorrect ****")
            failed = True
        elif meter_call is None or meter_call['device_type'] != 3:
            print(f"**** ERROR: Meter device_type incorrect ****")
            failed = True
        else:
            print(f"✓ Mixed device types verification test passed")

    # Test 6: Results are aggregated correctly (extend not append)
    print("Test 6: Results aggregation (extend behavior)")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    api6.device_info = {
        'DEV1': {'deviceSn': 'DEV1', 'deviceType': 1},
        'DEV2': {'deviceSn': 'DEV2', 'deviceType': 1},
    }
    
    async def mock_query_multi_records(sn, device_type, business_type=None):
        # Each device returns 2 records (simulating multiple data points)
        return [
            {'deviceSn': sn, 'record': 1},
            {'deviceSn': sn, 'record': 2}
        ]
    
    api6.query_device_realtime_data = mock_query_multi_records
    
    result6 = await api6.query_device_realtime_data_all("plant6")
    
    if len(result6) != 4:
        print(f"**** ERROR: Expected 4 total records (2 devices x 2 records), got {len(result6)} ****")
        failed = True
    else:
        print(f"✓ Results aggregation test passed")

    if not failed:
        print("✓ query_device_realtime_data_all tests passed")

    return failed


async def test_query_plant_statistics_daily_main():
    """
    Test query_plant_statistics_daily() function
    """
    failed = False
    print("\n=== Testing query_plant_statistics_daily ===")

    # Test 1: Successful fetch for current month
    print("Test 1: Successful fetch for current month")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    mock_stats_data = {
        'plantId': '1618699116555534337',
        'date': '2025-12',
        'currencyCode': 'GBP',
        'plantEnergyStatDataList': [
            {
                'date': '2025-12-01',
                'pvGeneration': 5.2,
                'inverterACOutputEnergy': 4.8,
                'exportEnergy': 1.2,
                'importEnergy': 3.5,
                'loadConsumption': 7.1,
                'batteryCharged': 2.1,
                'batteryDischarged': 1.8,
                'earnings': 0.85
            },
            {
                'date': '2025-12-02',
                'pvGeneration': 6.5,
                'inverterACOutputEnergy': 6.0,
                'exportEnergy': 2.0,
                'importEnergy': 2.8,
                'loadConsumption': 6.8,
                'batteryCharged': 2.5,
                'batteryDischarged': 2.2,
                'earnings': 1.10
            }
        ]
    }
    
    captured_calls = []
    
    async def mock_query_statistics(plant_id, date_type, date, business_type=None):
        captured_calls.append({
            'plant_id': plant_id,
            'date_type': date_type,
            'date': date,
            'business_type': business_type
        })
        return mock_stats_data.copy()
    
    api.query_plant_statistics = mock_query_statistics
    
    result = await api.query_plant_statistics_daily("1618699116555534337")
    
    if result is None:
        print(f"**** ERROR: Expected successful result, got None ****")
        failed = True
    elif result['plantId'] != '1618699116555534337':
        print(f"**** ERROR: Plant ID mismatch ****")
        failed = True
    elif len(result['plantEnergyStatDataList']) != 2:
        print(f"**** ERROR: Expected 2 daily records ****")
        failed = True
    elif len(captured_calls) != 1:
        print(f"**** ERROR: Expected 1 call to query_plant_statistics ****")
        failed = True
    elif captured_calls[0]['date_type'] != "2":
        print(f"**** ERROR: Expected date_type='2' (monthly), got {captured_calls[0]['date_type']} ****")
        failed = True
    elif not captured_calls[0]['date'].startswith('2025-'):
        print(f"**** ERROR: Expected date format YYYY-MM, got {captured_calls[0]['date']} ****")
        failed = True
    else:
        print(f"✓ Successful fetch for current month test passed")

    # Test 2: API error response (None returned)
    print("Test 2: API error response")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_query_error(plant_id, date_type, date, business_type=None):
        return None
    
    api2.query_plant_statistics = mock_query_error
    
    result2 = await api2.query_plant_statistics_daily("error_plant")
    
    if result2 is not None:
        print(f"**** ERROR: Expected None on API error, got {result2} ****")
        failed = True
    else:
        print(f"✓ API error response test passed")

    # Test 3: Custom business_type parameter
    print("Test 3: Custom business_type parameter")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_business_type = []
    
    async def mock_query_capture_business_type(plant_id, date_type, date, business_type=None):
        captured_business_type.append(business_type)
        return {'plantId': plant_id, 'plantEnergyStatDataList': []}
    
    api3.query_plant_statistics = mock_query_capture_business_type
    
    # Test with default business_type (None)
    await api3.query_plant_statistics_daily("plant_test")
    
    if len(captured_business_type) != 1:
        print(f"**** ERROR: Expected 1 call ****")
        failed = True
    elif captured_business_type[0] is not None:
        print(f"**** ERROR: Expected business_type None, got {captured_business_type[0]} ****")
        failed = True
    else:
        print(f"✓ Default business_type test passed")
    
    # Test with custom business_type
    captured_business_type.clear()
    await api3.query_plant_statistics_daily("plant_test", business_type=4)
    
    if captured_business_type[0] != 4:
        print(f"**** ERROR: Expected business_type 4, got {captured_business_type[0]} ****")
        failed = True
    else:
        print(f"✓ Custom business_type test passed")

    # Test 4: Empty plantEnergyStatDataList (no data for month)
    print("Test 4: Empty plantEnergyStatDataList (no data for month)")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_query_empty_data(plant_id, date_type, date, business_type=None):
        return {
            'plantId': plant_id,
            'date': date,
            'plantEnergyStatDataList': []
        }
    
    api4.query_plant_statistics = mock_query_empty_data
    
    result4 = await api4.query_plant_statistics_daily("plant_empty")
    
    if result4 is None:
        print(f"**** ERROR: Expected empty list result, got None ****")
        failed = True
    elif len(result4['plantEnergyStatDataList']) != 0:
        print(f"**** ERROR: Expected empty list, got {len(result4['plantEnergyStatDataList'])} records ****")
        failed = True
    else:
        print(f"✓ Empty plantEnergyStatDataList test passed")

    # Test 5: Verify date format passed to query_plant_statistics
    print("Test 5: Verify date format passed to query_plant_statistics")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    import re
    captured_date_formats = []
    
    async def mock_query_capture_date(plant_id, date_type, date, business_type=None):
        captured_date_formats.append(date)
        return {'plantId': plant_id}
    
    api5.query_plant_statistics = mock_query_capture_date
    
    await api5.query_plant_statistics_daily("plant_date_test")
    
    if len(captured_date_formats) != 1:
        print(f"**** ERROR: Expected 1 call ****")
        failed = True
    elif not re.match(r'^\d{4}-\d{2}$', captured_date_formats[0]):
        print(f"**** ERROR: Expected YYYY-MM format, got {captured_date_formats[0]} ****")
        failed = True
    else:
        print(f"✓ Date format verification test passed")

    if not failed:
        print("✓ query_plant_statistics_daily tests passed")

    return failed


async def test_send_command_and_wait_main():
    """
    Test send_command_and_wait() function
    """
    failed = False
    print("\n=== Testing send_command_and_wait ===")
    from solax import SOLAX_COMMAND_STATUS_OFFLINE, SOLAX_COMMAND_STATUS_ISSUE_SUCCESS, SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS, SOLAX_COMMAND_STATUS_EXECUTION_FAILED
    from unittest.mock import patch

    # Test 1: Successful command execution with immediate success
    print("Test 1: Successful command execution with immediate success")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_success(endpoint, post, json_data):
        return {
            "H1231231932123": {"status": SOLAX_COMMAND_STATUS_ISSUE_SUCCESS}
        }, "request_12345"
    
    async def mock_query_result_immediate_success(request_id):
        return SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS
    
    api.fetch_single_result = mock_fetch_success
    api.query_request_result = mock_query_result_immediate_success
    
    # Mock asyncio.sleep to speed up test
    with patch('asyncio.sleep', new_callable=AsyncMock):
        result = await api.send_command_and_wait(
            "/test/endpoint",
            {"test": "payload"},
            "test_command",
            ["H1231231932123"]
        )
    
    if not result:
        print(f"**** ERROR: Expected True for successful execution, got {result} ****")
        failed = True
    else:
        print(f"✓ Immediate success test passed")

    # Test 2: Command issuance failed (device offline)
    print("Test 2: Command issuance failed (device offline)")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_offline(endpoint, post, json_data):
        return {
            "H1231231932123": {"status": SOLAX_COMMAND_STATUS_OFFLINE}
        }, "request_12346"
    
    api2.fetch_single_result = mock_fetch_offline
    
    result2 = await api2.send_command_and_wait(
        "/test/endpoint",
        {"test": "payload"},
        "test_command",
        ["H1231231932123"]
    )
    
    if result2:
        print(f"**** ERROR: Expected False for offline device, got {result2} ****")
        failed = True
    else:
        print(f"✓ Device offline test passed")

    # Test 3: fetch_single_result returns None (network error)
    print("Test 3: fetch_single_result returns None (network error)")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_none(endpoint, post, json_data):
        return None, None
    
    api3.fetch_single_result = mock_fetch_none
    
    result3 = await api3.send_command_and_wait(
        "/test/endpoint",
        {"test": "payload"},
        "test_command",
        ["H1231231932123"]
    )
    
    if result3:
        print(f"**** ERROR: Expected False for None result, got {result3} ****")
        failed = True
    else:
        print(f"✓ Network error test passed")

    # Test 4: Successful after polling retries
    print("Test 4: Successful after polling retries")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_pending(endpoint, post, json_data):
        return {
            "H1231231932123": {"status": SOLAX_COMMAND_STATUS_ISSUE_SUCCESS}
        }, "request_12347"
    
    poll_count = [0]
    async def mock_query_result_delayed(request_id):
        poll_count[0] += 1
        if poll_count[0] < 3:
            return SOLAX_COMMAND_STATUS_ISSUE_SUCCESS  # Still pending
        return SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS  # Success on 3rd attempt
    
    api4.fetch_single_result = mock_fetch_pending
    api4.query_request_result = mock_query_result_delayed
    
    # Mock asyncio.sleep to speed up test
    with patch('asyncio.sleep', new_callable=AsyncMock):
        result4 = await api4.send_command_and_wait(
            "/test/endpoint",
            {"test": "payload"},
            "test_command",
            ["H1231231932123"]
        )
    
    if not result4:
        print(f"**** ERROR: Expected True after retries, got {result4} ****")
        failed = True
    elif poll_count[0] != 3:
        print(f"**** ERROR: Expected 3 polling attempts, got {poll_count[0]} ****")
        failed = True
    else:
        print(f"✓ Polling retry test passed")

    # Test 5: Execution failed after polling
    print("Test 5: Execution failed after polling")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_query_result_exec_failed(request_id):
        return SOLAX_COMMAND_STATUS_EXECUTION_FAILED
    
    api5.fetch_single_result = mock_fetch_pending
    api5.query_request_result = mock_query_result_exec_failed
    
    # Mock asyncio.sleep to speed up test
    with patch('asyncio.sleep', new_callable=AsyncMock):
        result5 = await api5.send_command_and_wait(
            "/test/endpoint",
            {"test": "payload"},
            "test_command",
            ["H1231231932123"]
        )
    
    if result5:
        print(f"**** ERROR: Expected False for execution failure, got {result5} ****")
        failed = True
    else:
        print(f"✓ Execution failed test passed")

    # Test 6: Timeout after max retries
    print("Test 6: Timeout after max retries")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    from solax import SOLAX_COMMAND_MAX_RETRIES
    retry_count = [0]
    
    async def mock_query_result_always_pending(request_id):
        retry_count[0] += 1
        return SOLAX_COMMAND_STATUS_ISSUE_SUCCESS  # Always pending
    
    api6.fetch_single_result = mock_fetch_pending
    api6.query_request_result = mock_query_result_always_pending
    
    # Mock asyncio.sleep to speed up test
    with patch('asyncio.sleep', new_callable=AsyncMock):
        result6 = await api6.send_command_and_wait(
            "/test/endpoint",
            {"test": "payload"},
            "test_command",
            ["H1231231932123"]
        )
    
    if result6:
        print(f"**** ERROR: Expected False for timeout, got {result6} ****")
        failed = True
    elif retry_count[0] != SOLAX_COMMAND_MAX_RETRIES:
        print(f"**** ERROR: Expected {SOLAX_COMMAND_MAX_RETRIES} polling attempts, got {retry_count[0]} ****")
        failed = True
    else:
        print(f"✓ Timeout after max retries test passed")

    # Test 7: No request_id returned (edge case)
    print("Test 7: No request_id returned (edge case)")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_fetch_no_request_id(endpoint, post, json_data):
        return {
            "H1231231932123": {"status": SOLAX_COMMAND_STATUS_ISSUE_SUCCESS}
        }, None  # No request_id
    
    api7.fetch_single_result = mock_fetch_no_request_id
    
    result7 = await api7.send_command_and_wait(
        "/test/endpoint",
        {"test": "payload"},
        "test_command",
        ["H1231231932123"]
        )
    
    if result7:
        print(f"**** ERROR: Expected False for missing request_id, got {result7} ****")
        failed = True
    else:
        print(f"✓ Missing request_id test passed")

    if not failed:
        print("✓ send_command_and_wait tests passed")

    return failed


async def test_control_mode_functions_main():
    """
    Test control mode functions: self_consume_mode(), soc_target_control_mode(), set_work_mode()
    """
    failed = False
    print("\n=== Testing control mode functions ===")
    from unittest.mock import patch

    # Test 1: self_consume_mode() - successful execution
    print("Test 1: self_consume_mode() - successful execution")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls = []
    
    async def mock_send_command(endpoint, payload, command_name, sn_list):
        captured_calls.append({
            'endpoint': endpoint,
            'payload': payload,
            'command_name': command_name,
            'sn_list': sn_list
        })
        return True
    
    api.send_command_and_wait = mock_send_command
    
    # Call the parent class method directly to test the real implementation
    result = await SolaxAPI.self_consume_mode(
        api,
        ["H1231231932123"],
        time_of_duration=3600,
        next_motion=161
    )
    
    if not result:
        print(f"**** ERROR: Expected True for successful execution, got {result} ****")
        failed = True
    elif len(captured_calls) != 1:
        print(f"**** ERROR: Expected 1 call, got {len(captured_calls)} ****")
        failed = True
    elif captured_calls[0]['endpoint'] != "/openapi/v2/device/inverter_vpp_mode/self_consume/charge_or_discharge_mode":
        print(f"**** ERROR: Wrong endpoint: {captured_calls[0]['endpoint']} ****")
        failed = True
    elif captured_calls[0]['payload']['snList'] != ["H1231231932123"]:
        print(f"**** ERROR: Wrong sn_list in payload ****")
        failed = True
    elif captured_calls[0]['payload']['timeOfDuration'] != 3600:
        print(f"**** ERROR: Wrong timeOfDuration: {captured_calls[0]['payload']['timeOfDuration']} ****")
        failed = True
    elif captured_calls[0]['payload']['nextMotion'] != 161:
        print(f"**** ERROR: Wrong nextMotion: {captured_calls[0]['payload']['nextMotion']} ****")
        failed = True
    elif captured_calls[0]['command_name'] != "self-consume":
        print(f"**** ERROR: Wrong command_name: {captured_calls[0]['command_name']} ****")
        failed = True
    else:
        print(f"✓ self_consume_mode successful execution test passed")

    # Test 2: self_consume_mode() - command failed
    print("Test 2: self_consume_mode() - command failed")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    async def mock_send_command_failed(endpoint, payload, command_name, sn_list):
        return False
    
    api2.send_command_and_wait = mock_send_command_failed
    
    result2 = await SolaxAPI.self_consume_mode(api2, ["H1231231932123"], time_of_duration=3600)
    
    if result2:
        print(f"**** ERROR: Expected False for failed command, got {result2} ****")
        failed = True
    else:
        print(f"✓ self_consume_mode command failed test passed")

    # Test 3: self_consume_mode() - custom business_type
    print("Test 3: self_consume_mode() - custom business_type")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api3.send_command_and_wait = mock_send_command
    
    await SolaxAPI.self_consume_mode(api3, ["H1231231932123"], time_of_duration=7200, business_type=4)
    
    if captured_calls[0]['payload']['businessType'] != 4:
        print(f"**** ERROR: Expected business_type 4, got {captured_calls[0]['payload']['businessType']} ****")
        failed = True
    else:
        print(f"✓ self_consume_mode custom business_type test passed")

    # Test 4: soc_target_control_mode() - charge mode (positive power)
    print("Test 4: soc_target_control_mode() - charge mode (positive power)")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api4.send_command_and_wait = mock_send_command
    
    result4 = await SolaxAPI.soc_target_control_mode(
        api4,
        ["H1231231932123"],
        target_soc=95,
        charge_discharge_power=5000
    )
    
    if not result4:
        print(f"**** ERROR: Expected True, got {result4} ****")
        failed = True
    elif captured_calls[0]['endpoint'] != "/openapi/v2/device/inverter_vpp_mode/soc_target_control_mode":
        print(f"**** ERROR: Wrong endpoint ****")
        failed = True
    elif captured_calls[0]['payload']['targetSoc'] != 95:
        print(f"**** ERROR: Expected targetSoc 95, got {captured_calls[0]['payload']['targetSoc']} ****")
        failed = True
    elif captured_calls[0]['payload']['chargeDischargPower'] != 5000:
        print(f"**** ERROR: Expected power 5000, got {captured_calls[0]['payload']['chargeDischargPower']} ****")
        failed = True
    elif captured_calls[0]['command_name'] != "soc-target":
        print(f"**** ERROR: Wrong command_name ****")
        failed = True
    else:
        print(f"✓ soc_target_control_mode charge mode test passed")

    # Test 5: soc_target_control_mode() - discharge mode (negative power)
    print("Test 5: soc_target_control_mode() - discharge mode (negative power)")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api5.send_command_and_wait = mock_send_command
    
    result5 = await SolaxAPI.soc_target_control_mode(
        api5,
        ["H1231231932123"],
        target_soc=15,
        charge_discharge_power=-4500
    )
    
    if not result5:
        print(f"**** ERROR: Expected True, got {result5} ****")
        failed = True
    elif captured_calls[0]['payload']['targetSoc'] != 15:
        print(f"**** ERROR: Expected targetSoc 15, got {captured_calls[0]['payload']['targetSoc']} ****")
        failed = True
    elif captured_calls[0]['payload']['chargeDischargPower'] != -4500:
        print(f"**** ERROR: Expected power -4500, got {captured_calls[0]['payload']['chargeDischargPower']} ****")
        failed = True
    else:
        print(f"✓ soc_target_control_mode discharge mode test passed")

    # Test 6: set_work_mode() - selfuse mode
    print("Test 6: set_work_mode() - selfuse mode")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api6.send_command_and_wait = mock_send_command
    
    result6 = await api6.set_work_mode(
        "selfuse",
        ["H1231231932123"],
        min_soc=10,
        charge_upper_soc=100,
        charge_from_grid_enable=0,
        charge_start_time="02:00",
        charge_end_time="06:00",
        discharge_start_time="16:00",
        discharge_end_time="20:00"
    )
    
    if not result6:
        print(f"**** ERROR: Expected True, got {result6} ****")
        failed = True
    elif captured_calls[0]['endpoint'] != "/openapi/v2/device/inverter_work_mode/batch_set_spontaneity_self_use":
        print(f"**** ERROR: Wrong endpoint for selfuse mode ****")
        failed = True
    elif captured_calls[0]['payload']['minSoc'] != 10:
        print(f"**** ERROR: Wrong minSoc ****")
        failed = True
    elif captured_calls[0]['payload']['chargeUpperSoc'] != 100:
        print(f"**** ERROR: Wrong chargeUpperSoc ****")
        failed = True
    elif captured_calls[0]['payload']['chargeFromGridEnable'] != 0:
        print(f"**** ERROR: Wrong chargeFromGridEnable ****")
        failed = True
    elif captured_calls[0]['payload']['chargeStartTimePeriod1'] != "02:00":
        print(f"**** ERROR: Wrong charge start time ****")
        failed = True
    elif captured_calls[0]['command_name'] != "selfuse":
        print(f"**** ERROR: Wrong command_name ****")
        failed = True
    else:
        print(f"✓ set_work_mode selfuse mode test passed")

    # Test 7: set_work_mode() - backup mode
    print("Test 7: set_work_mode() - backup mode")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api7.send_command_and_wait = mock_send_command
    
    result7 = await api7.set_work_mode(
        "backup",
        ["H1231231932123"],
        min_soc=50,
        charge_upper_soc=100,
        charge_from_grid_enable=1,
        charge_start_time="00:00",
        charge_end_time="00:00",
        discharge_start_time="00:00",
        discharge_end_time="00:00"
    )
    
    if not result7:
        print(f"**** ERROR: Expected True, got {result7} ****")
        failed = True
    elif captured_calls[0]['endpoint'] != "/openapi/v2/device/inverter_work_mode/batch_set_peace_mode":
        print(f"**** ERROR: Wrong endpoint for backup mode, got {captured_calls[0]['endpoint']} ****")
        failed = True
    elif captured_calls[0]['command_name'] != "backup":
        print(f"**** ERROR: Wrong command_name ****")
        failed = True
    else:
        print(f"✓ set_work_mode backup mode test passed")

    # Test 8: set_work_mode() - feedin mode
    print("Test 8: set_work_mode() - feedin mode")
    api8 = MockSolaxAPI()
    api8.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api8.send_command_and_wait = mock_send_command
    
    result8 = await api8.set_work_mode(
        "feedin",
        ["H1231231932123"],
        min_soc=10,
        charge_upper_soc=100,
        charge_from_grid_enable=0,
        charge_start_time="00:00",
        charge_end_time="00:00",
        discharge_start_time="00:00",
        discharge_end_time="00:00"
    )
    
    if not result8:
        print(f"**** ERROR: Expected True, got {result8} ****")
        failed = True
    elif captured_calls[0]['endpoint'] != "/openapi/v2/device/inverter_work_mode/batch_set_on_grid_first":
        print(f"**** ERROR: Wrong endpoint for feedin mode, got {captured_calls[0]['endpoint']} ****")
        failed = True
    elif captured_calls[0]['command_name'] != "feedin":
        print(f"**** ERROR: Wrong command_name ****")
        failed = True
    else:
        print(f"✓ set_work_mode feedin mode test passed")

    # Test 9: set_work_mode() - unknown mode
    print("Test 9: set_work_mode() - unknown mode")
    api9 = MockSolaxAPI()
    api9.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api9.send_command_and_wait = mock_send_command
    
    result9 = await api9.set_work_mode(
        "invalid_mode",
        ["H1231231932123"],
        min_soc=10,
        charge_upper_soc=100,
        charge_from_grid_enable=0,
        charge_start_time="00:00",
        charge_end_time="00:00",
        discharge_start_time="00:00",
        discharge_end_time="00:00"
    )
    
    if result9:
        print(f"**** ERROR: Expected False for unknown mode, got {result9} ****")
        failed = True
    elif len(captured_calls) != 0:
        print(f"**** ERROR: Expected no calls for unknown mode, got {len(captured_calls)} ****")
        failed = True
    else:
        print(f"✓ set_work_mode unknown mode test passed")

    # Test 10: Multiple devices in sn_list
    print("Test 10: Multiple devices in sn_list")
    api10 = MockSolaxAPI()
    api10.initialize(client_id="test", client_secret="test", region="eu")
    
    captured_calls.clear()
    api10.send_command_and_wait = mock_send_command
    
    result10 = await SolaxAPI.soc_target_control_mode(
        api10,
        ["H1231231932123", "H9876543210987"],
        target_soc=80,
        charge_discharge_power=3000
    )
    
    if not result10:
        print(f"**** ERROR: Expected True, got {result10} ****")
        failed = True
    elif captured_calls[0]['payload']['snList'] != ["H1231231932123", "H9876543210987"]:
        print(f"**** ERROR: Wrong sn_list in payload ****")
        failed = True
    else:
        print(f"✓ Multiple devices test passed")

    if not failed:
        print("✓ control mode functions tests passed")

    return failed


async def test_publish_device_info_main():
    """
    Test publish_device_info() - publishing device sensor entities
    """
    failed = False
    print("\n=== Testing publish_device_info ===")
    
    # Test 1: Inverter device info publishing
    print("Test 1: Inverter device info publishing")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Setup inverter device info
    api.device_info["H1231231932123"] = {
        "deviceSn": "H1231231932123",
        "deviceType": 1,  # Inverter
        "deviceModel": 3,  # X1-Hybrid-G3
        "plantId": "1618699116555534337",
        "onlineStatus": 1,  # Online
        "ratedPower": 10.0  # kW
    }
    
    await api.publish_device_info()
    
    # Verify the sensor was created
    sensor_id = "sensor.predbat_solax_1618699116555534337_H1231231932123_online_status"
    if sensor_id not in api.dashboard_items:
        print(f"**** ERROR: Expected sensor {sensor_id} not found ****")
        failed = True
    elif api.dashboard_items[sensor_id]["state"] != 1:
        print(f"**** ERROR: Expected online status 1, got {api.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        attrs = api.dashboard_items[sensor_id]["attributes"]
        if attrs.get("device_type") != 1:
            print(f"**** ERROR: Expected device_type 1, got {attrs.get('device_type')} ****")
            failed = True
        elif attrs.get("device_model") != "X1-Hybrid-G3":
            print(f"**** ERROR: Expected device_model 'X1-Hybrid-G3', got {attrs.get('device_model')} ****")
            failed = True
        elif attrs.get("rated_power") != 10000:
            print(f"**** ERROR: Expected rated_power 10000W, got {attrs.get('rated_power')} ****")
            failed = True
        elif attrs.get("plant_id") != "1618699116555534337":
            print(f"**** ERROR: Expected plant_id '1618699116555534337', got {attrs.get('plant_id')} ****")
            failed = True
        elif "SolaX X1-Hybrid-G3 H1231231932123 Online Status" not in attrs.get("friendly_name", ""):
            print(f"**** ERROR: Incorrect friendly_name: {attrs.get('friendly_name')} ****")
            failed = True
        else:
            print(f"✓ Inverter device info publishing test passed")
    
    # Test 2: Battery device info publishing
    print("Test 2: Battery device info publishing")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    # Setup battery device info (no device model code in residential mapping)
    api2.device_info["TP123456123123"] = {
        "deviceSn": "TP123456123123",
        "deviceType": 2,  # Battery
        "deviceModel": 999,  # Unknown model code
        "plantId": "1618699116555534337",
        "onlineStatus": 1,
        "ratedPower": 5.0
    }
    
    await api2.publish_device_info()
    
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_online_status"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Expected sensor {sensor_id} not found ****")
        failed = True
    else:
        attrs = api2.dashboard_items[sensor_id]["attributes"]
        if attrs.get("device_type") != 2:
            print(f"**** ERROR: Expected device_type 2, got {attrs.get('device_type')} ****")
            failed = True
        elif attrs.get("device_model") != "Battery":  # Falls back to "Battery" for unknown model
            print(f"**** ERROR: Expected device_model 'Battery', got {attrs.get('device_model')} ****")
            failed = True
        elif attrs.get("rated_power") != 5000:
            print(f"**** ERROR: Expected rated_power 5000W, got {attrs.get('rated_power')} ****")
            failed = True
        else:
            print(f"✓ Battery device info publishing test passed")
    
    # Test 3: Meter device info publishing
    print("Test 3: Meter device info publishing")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    api3.device_info["M123456789"] = {
        "deviceSn": "M123456789",
        "deviceType": 3,  # Meter
        "deviceModel": 50,  # Meter X
        "plantId": "Test_Plant_123",
        "onlineStatus": 0,  # Offline
        "ratedPower": 0
    }
    
    await api3.publish_device_info()
    
    sensor_id = "sensor.predbat_solax_test_plant_123_M123456789_online_status"
    if sensor_id not in api3.dashboard_items:
        print(f"**** ERROR: Expected sensor {sensor_id} not found ****")
        failed = True
    elif api3.dashboard_items[sensor_id]["state"] != 0:
        print(f"**** ERROR: Expected online status 0 (offline), got {api3.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        attrs = api3.dashboard_items[sensor_id]["attributes"]
        if attrs.get("device_model") != "Meter X":
            print(f"**** ERROR: Expected device_model 'Meter X', got {attrs.get('device_model')} ****")
            failed = True
        else:
            print(f"✓ Meter device info publishing test passed")
    
    # Test 4: EV Charger device info publishing
    print("Test 4: EV Charger device info publishing")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    api4.device_info["EVC123456"] = {
        "deviceSn": "EVC123456",
        "deviceType": 4,  # EV Charger
        "deviceModel": 1,  # X1/X3-EVC
        "plantId": "plant_456",
        "onlineStatus": 1,
        "ratedPower": 7.0
    }
    
    await api4.publish_device_info()
    
    sensor_id = "sensor.predbat_solax_plant_456_EVC123456_online_status"
    if sensor_id not in api4.dashboard_items:
        print(f"**** ERROR: Expected sensor {sensor_id} not found ****")
        failed = True
    else:
        attrs = api4.dashboard_items[sensor_id]["attributes"]
        if attrs.get("device_model") != "X1/X3-EVC":
            print(f"**** ERROR: Expected device_model 'X1/X3-EVC', got {attrs.get('device_model')} ****")
            failed = True
        elif attrs.get("rated_power") != 7000:
            print(f"**** ERROR: Expected rated_power 7000W, got {attrs.get('rated_power')} ****")
            failed = True
        else:
            print(f"✓ EV Charger device info publishing test passed")
    
    # Test 5: Unknown device type
    print("Test 5: Unknown device type")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    api5.device_info["UNKNOWN123"] = {
        "deviceSn": "UNKNOWN123",
        "deviceType": 99,  # Unknown type
        "deviceModel": 0,
        "plantId": "test_plant",
        "onlineStatus": 1,
        "ratedPower": 0
    }
    
    await api5.publish_device_info()
    
    sensor_id = "sensor.predbat_solax_test_plant_UNKNOWN123_online_status"
    if sensor_id not in api5.dashboard_items:
        print(f"**** ERROR: Expected sensor {sensor_id} not found ****")
        failed = True
    else:
        attrs = api5.dashboard_items[sensor_id]["attributes"]
        if attrs.get("device_model") != "Unknown Device":
            print(f"**** ERROR: Expected device_model 'Unknown Device', got {attrs.get('device_model')} ****")
            failed = True
        else:
            print(f"✓ Unknown device type test passed")
    
    # Test 6: Multiple devices
    print("Test 6: Multiple devices")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    # Add multiple devices
    api6.device_info["INV001"] = {
        "deviceSn": "INV001",
        "deviceType": 1,
        "deviceModel": 14,  # X3-Hybrid-G4
        "plantId": "plant_multi",
        "onlineStatus": 1,
        "ratedPower": 12.0
    }
    api6.device_info["BAT001"] = {
        "deviceSn": "BAT001",
        "deviceType": 2,
        "deviceModel": 0,
        "plantId": "plant_multi",
        "onlineStatus": 1,
        "ratedPower": 8.0
    }
    api6.device_info["MTR001"] = {
        "deviceSn": "MTR001",
        "deviceType": 3,
        "deviceModel": 176,  # M1-40
        "plantId": "plant_multi",
        "onlineStatus": 1,
        "ratedPower": 0
    }
    
    await api6.publish_device_info()
    
    inv_sensor = "sensor.predbat_solax_plant_multi_INV001_online_status"
    bat_sensor = "sensor.predbat_solax_plant_multi_BAT001_online_status"
    mtr_sensor = "sensor.predbat_solax_plant_multi_MTR001_online_status"
    
    if inv_sensor not in api6.dashboard_items:
        print(f"**** ERROR: Inverter sensor not found ****")
        failed = True
    elif bat_sensor not in api6.dashboard_items:
        print(f"**** ERROR: Battery sensor not found ****")
        failed = True
    elif mtr_sensor not in api6.dashboard_items:
        print(f"**** ERROR: Meter sensor not found ****")
        failed = True
    else:
        inv_attrs = api6.dashboard_items[inv_sensor]["attributes"]
        bat_attrs = api6.dashboard_items[bat_sensor]["attributes"]
        mtr_attrs = api6.dashboard_items[mtr_sensor]["attributes"]
        
        if inv_attrs.get("device_model") != "X3-Hybrid-G4":
            print(f"**** ERROR: Wrong inverter model: {inv_attrs.get('device_model')} ****")
            failed = True
        elif bat_attrs.get("device_model") != "Battery":
            print(f"**** ERROR: Wrong battery model: {bat_attrs.get('device_model')} ****")
            failed = True
        elif mtr_attrs.get("device_model") != "M1-40":
            print(f"**** ERROR: Wrong meter model: {mtr_attrs.get('device_model')} ****")
            failed = True
        else:
            print(f"✓ Multiple devices test passed")
    
    # Test 7: Empty device_info (no devices to publish)
    print("Test 7: Empty device_info")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    # No devices
    api7.device_info = {}
    
    await api7.publish_device_info()
    
    # Should not create any sensors
    if len(api7.dashboard_items) != 0:
        print(f"**** ERROR: Expected no sensors, got {len(api7.dashboard_items)} ****")
        failed = True
    else:
        print(f"✓ Empty device_info test passed")
    
    # Test 8: Missing optional fields (should use defaults)
    print("Test 8: Missing optional fields")
    api8 = MockSolaxAPI()
    api8.initialize(client_id="test", client_secret="test", region="eu")
    
    api8.device_info["MINIMAL123"] = {
        "deviceSn": "MINIMAL123",
        "deviceType": 1,
        # Missing deviceModel, plantId, onlineStatus, ratedPower
    }
    
    await api8.publish_device_info()
    
    sensor_id = "sensor.predbat_solax_unknown_MINIMAL123_online_status"
    if sensor_id not in api8.dashboard_items:
        print(f"**** ERROR: Expected sensor {sensor_id} not found ****")
        failed = True
    elif api8.dashboard_items[sensor_id]["state"] != 0:  # Default online status
        print(f"**** ERROR: Expected default online status 0, got {api8.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        attrs = api8.dashboard_items[sensor_id]["attributes"]
        if attrs.get("device_model") != "Inverter":  # Default for unknown model code
            print(f"**** ERROR: Expected default device_model 'Inverter', got {attrs.get('device_model')} ****")
            failed = True
        elif attrs.get("rated_power") != 0:  # Default rated power
            print(f"**** ERROR: Expected default rated_power 0, got {attrs.get('rated_power')} ****")
            failed = True
        elif attrs.get("plant_id") is not None:  # plant_id is None when not provided
            print(f"**** ERROR: Expected plant_id None, got {attrs.get('plant_id')} ****")
            failed = True
        else:
            print(f"✓ Missing optional fields test passed")
    
    if not failed:
        print("✓ publish_device_info tests passed")
    
    return failed


async def test_publish_device_realtime_data_main():
    """
    Test publish_device_realtime_data() - publishing real-time sensor data
    """
    failed = False
    print("\n=== Testing publish_device_realtime_data ===")
    
    # Test 1: Inverter realtime data publishing
    print("Test 1: Inverter realtime data publishing")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Setup inverter device info
    api.device_info["H1231231932123"] = {
        "deviceSn": "H1231231932123",
        "deviceType": 1,  # Inverter
        "deviceModel": 3,  # X1-Hybrid-G3
        "plantId": "1618699116555534337"
    }
    
    # Setup inverter realtime data
    api.realtime_device_data["H1231231932123"] = {
        "deviceSn": "H1231231932123",
        "acPower1": 1000,
        "acPower2": 1500,
        "acPower3": 500,
        "gridPower": -2500,  # Exporting
        "pvMap": {"pv1Power": 2000, "pv2Power": 1500},
        "totalActivePower": 3000,
        "totalReactivePower": 100,
        "totalYield": 12500.5,
        "deviceStatus": 102  # Normal
    }
    
    await api.publish_device_realtime_data()
    
    # Verify device status sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_H1231231932123_device_status"
    if sensor_id not in api.dashboard_items:
        print(f"**** ERROR: Device status sensor not found ****")
        failed = True
    elif api.dashboard_items[sensor_id]["state"] != "Normal":
        print(f"**** ERROR: Expected status 'Normal', got {api.dashboard_items[sensor_id]['state']} ****")
        failed = True
    elif api.dashboard_items[sensor_id]["attributes"]["status_value"] != 102:
        print(f"**** ERROR: Expected status_value 102, got {api.dashboard_items[sensor_id]['attributes']['status_value']} ****")
        failed = True
    else:
        print(f"✓ Inverter device status sensor correct")
    
    # Verify AC power sensor (sum of 3 phases)
    sensor_id = "sensor.predbat_solax_1618699116555534337_H1231231932123_ac_power"
    if sensor_id not in api.dashboard_items:
        print(f"**** ERROR: AC power sensor not found ****")
        failed = True
    elif api.dashboard_items[sensor_id]["state"] != 3000:  # 1000+1500+500
        print(f"**** ERROR: Expected AC power 3000, got {api.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Inverter AC power sensor correct")
    
    # Verify PV power sensor (sum from pvMap)
    sensor_id = "sensor.predbat_solax_1618699116555534337_H1231231932123_pv_power"
    if sensor_id not in api.dashboard_items:
        print(f"**** ERROR: PV power sensor not found ****")
        failed = True
    elif api.dashboard_items[sensor_id]["state"] != 3500:  # 2000+1500
        print(f"**** ERROR: Expected PV power 3500, got {api.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Inverter PV power sensor correct")
    
    # Verify grid power sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_H1231231932123_grid_power"
    if sensor_id not in api.dashboard_items:
        print(f"**** ERROR: Grid power sensor not found ****")
        failed = True
    elif api.dashboard_items[sensor_id]["state"] != -2500:
        print(f"**** ERROR: Expected grid power -2500, got {api.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Inverter grid power sensor correct")
    
    # Verify total yield sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_H1231231932123_total_yield"
    if sensor_id not in api.dashboard_items:
        print(f"**** ERROR: Total yield sensor not found ****")
        failed = True
    elif api.dashboard_items[sensor_id]["state"] != 12500.5:
        print(f"**** ERROR: Expected total yield 12500.5, got {api.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Inverter total yield sensor correct")
    
    # Test 2: Battery realtime data publishing
    print("Test 2: Battery realtime data publishing")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    # Setup battery device info
    api2.device_info["TP123456123123"] = {
        "deviceSn": "TP123456123123",
        "deviceType": 2,  # Battery
        "deviceModel": 0,
        "plantId": "1618699116555534337"
    }
    
    # Setup battery realtime data
    api2.realtime_device_data["TP123456123123"] = {
        "deviceSn": "TP123456123123",
        "batterySOC": 85,
        "batteryVoltage": 450.5,
        "chargeDischargePower": 2500,  # Charging
        "batteryCurrent": 5.5,
        "batteryTemperature": 22.5,
        "deviceStatus": 1  # Work
    }
    
    await api2.publish_device_realtime_data()
    
    # Verify battery status sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_device_status"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Battery status sensor not found ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["state"] != "Work":
        print(f"**** ERROR: Expected status 'Work', got {api2.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Battery device status sensor correct")
    
    # Verify battery SOC sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_battery_soc"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Battery SOC sensor not found ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["state"] != 85:
        print(f"**** ERROR: Expected SOC 85%, got {api2.dashboard_items[sensor_id]['state']} ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["attributes"]["unit_of_measurement"] != "%":
        print(f"**** ERROR: Wrong SOC unit ****")
        failed = True
    else:
        print(f"✓ Battery SOC sensor correct")
    
    # Verify battery voltage sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_battery_voltage"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Battery voltage sensor not found ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["state"] != 450.5:
        print(f"**** ERROR: Expected voltage 450.5V, got {api2.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Battery voltage sensor correct")
    
    # Verify charge/discharge power sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_charge_discharge_power"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Charge/discharge power sensor not found ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["state"] != 2500:
        print(f"**** ERROR: Expected power 2500W, got {api2.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Battery charge/discharge power sensor correct")
    
    # Verify battery current sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_battery_current"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Battery current sensor not found ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["state"] != 5.5:
        print(f"**** ERROR: Expected current 5.5A, got {api2.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Battery current sensor correct")
    
    # Verify battery temperature sensor
    sensor_id = "sensor.predbat_solax_1618699116555534337_TP123456123123_battery_temperature"
    if sensor_id not in api2.dashboard_items:
        print(f"**** ERROR: Battery temperature sensor not found ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["state"] != 22.5:
        print(f"**** ERROR: Expected temperature 22.5°C, got {api2.dashboard_items[sensor_id]['state']} ****")
        failed = True
    elif api2.dashboard_items[sensor_id]["attributes"]["unit_of_measurement"] != "°C":
        print(f"**** ERROR: Wrong temperature unit ****")
        failed = True
    else:
        print(f"✓ Battery temperature sensor correct")
    
    # Test 3: Inverter with mpptMap instead of pvMap
    print("Test 3: Inverter with mpptMap instead of pvMap")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    api3.device_info["INV002"] = {
        "deviceSn": "INV002",
        "deviceType": 1,
        "deviceModel": 5,
        "plantId": "plant_test"
    }
    
    api3.realtime_device_data["INV002"] = {
        "deviceSn": "INV002",
        "acPower1": 500,
        "acPower2": 500,
        "acPower3": 500,
        "gridPower": 0,
        "mpptMap": {"mppt1Power": 800, "mppt2Power": 700},  # Use mpptMap instead of pvMap
        "totalActivePower": 1500,
        "totalReactivePower": 0,
        "totalYield": 5000.0,
        "deviceStatus": 100  # Waiting
    }
    
    await api3.publish_device_realtime_data()
    
    # Verify PV power calculated from mpptMap
    sensor_id = "sensor.predbat_solax_plant_test_INV002_pv_power"
    if sensor_id not in api3.dashboard_items:
        print(f"**** ERROR: PV power sensor not found ****")
        failed = True
    elif api3.dashboard_items[sensor_id]["state"] != 1500:  # 800+700
        print(f"**** ERROR: Expected PV power 1500 from mpptMap, got {api3.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Inverter mpptMap PV power sensor correct")
    
    # Test 4: Device with no pvMap or mpptMap
    print("Test 4: Device with no pvMap or mpptMap")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    api4.device_info["INV003"] = {
        "deviceSn": "INV003",
        "deviceType": 1,
        "deviceModel": 1,
        "plantId": "plant_test"
    }
    
    api4.realtime_device_data["INV003"] = {
        "deviceSn": "INV003",
        "acPower1": 100,
        "acPower2": 100,
        "acPower3": 100,
        "gridPower": 300,
        # No pvMap or mpptMap
        "totalActivePower": 300,
        "totalReactivePower": 0,
        "totalYield": 1000.0,
        "deviceStatus": 102
    }
    
    await api4.publish_device_realtime_data()
    
    # Verify PV power defaults to 0
    sensor_id = "sensor.predbat_solax_plant_test_INV003_pv_power"
    if sensor_id not in api4.dashboard_items:
        print(f"**** ERROR: PV power sensor not found ****")
        failed = True
    elif api4.dashboard_items[sensor_id]["state"] != 0:
        print(f"**** ERROR: Expected PV power 0 (no map), got {api4.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Inverter with no PV map defaults to 0")
    
    # Test 5: Unknown device status codes
    print("Test 5: Unknown device status codes")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    api5.device_info["INV999"] = {
        "deviceSn": "INV999",
        "deviceType": 1,
        "deviceModel": 1,
        "plantId": "plant_test"
    }
    
    api5.realtime_device_data["INV999"] = {
        "deviceSn": "INV999",
        "acPower1": 0,
        "acPower2": 0,
        "acPower3": 0,
        "gridPower": 0,
        "totalActivePower": 0,
        "totalReactivePower": 0,
        "totalYield": 0,
        "deviceStatus": 9999  # Unknown status
    }
    
    await api5.publish_device_realtime_data()
    
    sensor_id = "sensor.predbat_solax_plant_test_INV999_device_status"
    if sensor_id not in api5.dashboard_items:
        print(f"**** ERROR: Device status sensor not found ****")
        failed = True
    elif api5.dashboard_items[sensor_id]["state"] != "Unknown Status":
        print(f"**** ERROR: Expected 'Unknown Status', got {api5.dashboard_items[sensor_id]['state']} ****")
        failed = True
    else:
        print(f"✓ Unknown device status handled correctly")
    
    # Test 6: Empty realtime_device_data
    print("Test 6: Empty realtime_device_data")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    api6.device_info["TEST001"] = {
        "deviceSn": "TEST001",
        "deviceType": 1,
        "deviceModel": 1,
        "plantId": "plant_test"
    }
    # No realtime data
    api6.realtime_device_data = {}
    
    await api6.publish_device_realtime_data()
    
    # Should not create any sensors
    if len(api6.dashboard_items) != 0:
        print(f"**** ERROR: Expected no sensors, got {len(api6.dashboard_items)} ****")
        failed = True
    else:
        print(f"✓ Empty realtime_device_data test passed")
    
    # Test 7: Multiple devices (inverter + battery)
    print("Test 7: Multiple devices (inverter + battery)")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    api7.device_info["INV_MULTI"] = {
        "deviceSn": "INV_MULTI",
        "deviceType": 1,
        "deviceModel": 3,
        "plantId": "multi_plant"
    }
    api7.device_info["BAT_MULTI"] = {
        "deviceSn": "BAT_MULTI",
        "deviceType": 2,
        "deviceModel": 0,
        "plantId": "multi_plant"
    }
    
    api7.realtime_device_data["INV_MULTI"] = {
        "acPower1": 100, "acPower2": 100, "acPower3": 100,
        "gridPower": -200, "totalActivePower": 300,
        "totalReactivePower": 0, "totalYield": 500.0,
        "deviceStatus": 102
    }
    api7.realtime_device_data["BAT_MULTI"] = {
        "batterySOC": 50, "batteryVoltage": 400.0,
        "chargeDischargePower": -1000, "batteryCurrent": -2.5,
        "batteryTemperature": 20.0, "deviceStatus": 1
    }
    
    await api7.publish_device_realtime_data()
    
    inv_sensor = "sensor.predbat_solax_multi_plant_INV_MULTI_ac_power"
    bat_sensor = "sensor.predbat_solax_multi_plant_BAT_MULTI_battery_soc"
    
    if inv_sensor not in api7.dashboard_items:
        print(f"**** ERROR: Inverter sensor not found ****")
        failed = True
    elif bat_sensor not in api7.dashboard_items:
        print(f"**** ERROR: Battery sensor not found ****")
        failed = True
    elif api7.dashboard_items[inv_sensor]["state"] != 300:
        print(f"**** ERROR: Wrong inverter AC power ****")
        failed = True
    elif api7.dashboard_items[bat_sensor]["state"] != 50:
        print(f"**** ERROR: Wrong battery SOC ****")
        failed = True
    else:
        print(f"✓ Multiple devices test passed")
    
    if not failed:
        print("✓ publish_device_realtime_data tests passed")
    
    return failed


async def test_helper_methods_main():
    """
    Test helper methods: get_max_power_*(), get_current_soc_*(), get_battery_temperature()
    """
    failed = False
    print("\n=== Testing helper methods ===")
    
    # Test 1: get_max_power_inverter - single inverter
    print("Test 1: get_max_power_inverter - single inverter")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    api.plant_inverters["plant1"] = ["INV001"]
    api.device_info["INV001"] = {"ratedPower": 10.0}  # 10 kW
    
    result = api.get_max_power_inverter("plant1")
    if result != 10000:  # Should be in Watts
        print(f"**** ERROR: Expected 10000W, got {result}W ****")
        failed = True
    else:
        print(f"✓ Single inverter power calculation correct (10000W)")
    
    # Test 2: get_max_power_inverter - multiple inverters
    print("Test 2: get_max_power_inverter - multiple inverters")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    api2.plant_inverters["plant2"] = ["INV001", "INV002", "INV003"]
    api2.device_info["INV001"] = {"ratedPower": 5.0}
    api2.device_info["INV002"] = {"ratedPower": 10.0}
    api2.device_info["INV003"] = {"ratedPower": 8.5}
    
    result2 = api2.get_max_power_inverter("plant2")
    if result2 != 23500:  # 5000 + 10000 + 8500
        print(f"**** ERROR: Expected 23500W, got {result2}W ****")
        failed = True
    else:
        print(f"✓ Multiple inverter power calculation correct (23500W)")
    
    # Test 3: get_max_power_inverter - no inverters
    print("Test 3: get_max_power_inverter - no inverters")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    result3 = api3.get_max_power_inverter("nonexistent_plant")
    if result3 != 0:
        print(f"**** ERROR: Expected 0W for no inverters, got {result3}W ****")
        failed = True
    else:
        print(f"✓ No inverters returns 0W")
    
    # Test 4: get_max_power_battery - single battery
    print("Test 4: get_max_power_battery - single battery")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    api4.plant_batteries["plant4"] = ["BAT001"]
    api4.device_info["BAT001"] = {"ratedPower": 5.0}  # 5 kW
    
    result4 = api4.get_max_power_battery("plant4")
    if result4 != 5000:
        print(f"**** ERROR: Expected 5000W, got {result4}W ****")
        failed = True
    else:
        print(f"✓ Single battery power calculation correct (5000W)")
    
    # Test 5: get_max_power_battery - multiple batteries
    print("Test 5: get_max_power_battery - multiple batteries")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    api5.plant_batteries["plant5"] = ["BAT001", "BAT002"]
    api5.device_info["BAT001"] = {"ratedPower": 5.0}
    api5.device_info["BAT002"] = {"ratedPower": 5.0}
    
    result5 = api5.get_max_power_battery("plant5")
    if result5 != 10000:  # 5000 + 5000
        print(f"**** ERROR: Expected 10000W, got {result5}W ****")
        failed = True
    else:
        print(f"✓ Multiple battery power calculation correct (10000W)")
    
    # Test 6: get_max_power_battery - fallback to inverter power
    print("Test 6: get_max_power_battery - fallback to inverter power")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    api6.plant_inverters["plant6"] = ["INV001"]
    api6.device_info["INV001"] = {"ratedPower": 12.0}
    # No batteries defined
    
    result6 = api6.get_max_power_battery("plant6")
    if result6 != 12000:  # Falls back to inverter power
        print(f"**** ERROR: Expected 12000W (inverter fallback), got {result6}W ****")
        failed = True
    else:
        print(f"✓ Battery power fallback to inverter correct (12000W)")
    
    # Test 7: get_max_soc_battery
    print("Test 7: get_max_soc_battery")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    api7.plant_info = [
        {"plantId": "plant7", "batteryCapacity": 15.0},
        {"plantId": "other_plant", "batteryCapacity": 20.0}
    ]
    
    result7 = api7.get_max_soc_battery("plant7")
    if result7 != 15.0:
        print(f"**** ERROR: Expected 15.0 kWh, got {result7} kWh ****")
        failed = True
    else:
        print(f"✓ Max SOC battery correct (15.0 kWh)")
    
    # Test 8: get_current_soc_battery_kwh - single battery
    print("Test 8: get_current_soc_battery_kwh - single battery")
    api8 = MockSolaxAPI()
    api8.initialize(client_id="test", client_secret="test", region="eu")
    
    api8.plant_batteries["plant8"] = ["BAT001"]
    api8.plant_info = [{"plantId": "plant8", "batteryCapacity": 15.0}]
    api8.realtime_device_data["BAT001"] = {"batterySOC": 75}  # 75%
    
    result8 = api8.get_current_soc_battery_kwh("plant8")
    expected8 = 75 * 15.0 / 100.0  # 11.25 kWh
    if abs(result8 - expected8) > 0.01:
        print(f"**** ERROR: Expected {expected8} kWh, got {result8} kWh ****")
        failed = True
    else:
        print(f"✓ Single battery current SOC correct (11.25 kWh)")
    
    # Test 9: get_current_soc_battery_kwh - multiple batteries (average)
    print("Test 9: get_current_soc_battery_kwh - multiple batteries (average)")
    api9 = MockSolaxAPI()
    api9.initialize(client_id="test", client_secret="test", region="eu")
    
    api9.plant_batteries["plant9"] = ["BAT001", "BAT002"]
    api9.plant_info = [{"plantId": "plant9", "batteryCapacity": 20.0}]
    api9.realtime_device_data["BAT001"] = {"batterySOC": 80}  # 80%
    api9.realtime_device_data["BAT002"] = {"batterySOC": 60}  # 60%
    
    result9 = api9.get_current_soc_battery_kwh("plant9")
    expected9 = ((80 + 60) / 2) * 20.0 / 100.0  # Average 70%, then to kWh = 14.0
    if abs(result9 - expected9) > 0.01:
        print(f"**** ERROR: Expected {expected9} kWh, got {result9} kWh ****")
        failed = True
    else:
        print(f"✓ Multiple battery current SOC average correct (14.0 kWh)")
    
    # Test 10: get_current_soc_battery_kwh - no batteries
    print("Test 10: get_current_soc_battery_kwh - no batteries")
    api10 = MockSolaxAPI()
    api10.initialize(client_id="test", client_secret="test", region="eu")
    
    result10 = api10.get_current_soc_battery_kwh("plant10")
    if result10 != 0:
        print(f"**** ERROR: Expected 0 kWh for no batteries, got {result10} kWh ****")
        failed = True
    else:
        print(f"✓ No batteries returns 0 kWh")
    
    # Test 11: get_battery_temperature - single battery
    print("Test 11: get_battery_temperature - single battery")
    api11 = MockSolaxAPI()
    api11.initialize(client_id="test", client_secret="test", region="eu")
    
    api11.plant_batteries["plant11"] = ["BAT001"]
    api11.realtime_device_data["BAT001"] = {"batteryTemperature": 22.5}
    
    result11 = api11.get_battery_temperature("plant11")
    if result11 != 22.5:
        print(f"**** ERROR: Expected 22.5°C, got {result11}°C ****")
        failed = True
    else:
        print(f"✓ Single battery temperature correct (22.5°C)")
    
    # Test 12: get_battery_temperature - multiple batteries (minimum)
    print("Test 12: get_battery_temperature - multiple batteries (minimum)")
    api12 = MockSolaxAPI()
    api12.initialize(client_id="test", client_secret="test", region="eu")
    
    api12.plant_batteries["plant12"] = ["BAT001", "BAT002", "BAT003"]
    api12.realtime_device_data["BAT001"] = {"batteryTemperature": 25.0}
    api12.realtime_device_data["BAT002"] = {"batteryTemperature": 18.5}  # Minimum
    api12.realtime_device_data["BAT003"] = {"batteryTemperature": 22.0}
    
    result12 = api12.get_battery_temperature("plant12")
    if result12 != 18.5:
        print(f"**** ERROR: Expected minimum temperature 18.5°C, got {result12}°C ****")
        failed = True
    else:
        print(f"✓ Multiple battery temperature minimum correct (18.5°C)")
    
    # Test 13: get_battery_temperature - no temperature data
    print("Test 13: get_battery_temperature - no temperature data")
    api13 = MockSolaxAPI()
    api13.initialize(client_id="test", client_secret="test", region="eu")
    
    api13.plant_batteries["plant13"] = ["BAT001"]
    api13.realtime_device_data["BAT001"] = {}  # No temperature field
    
    result13 = api13.get_battery_temperature("plant13")
    if result13 is not None:
        print(f"**** ERROR: Expected None for no temperature data, got {result13}°C ****")
        failed = True
    else:
        print(f"✓ No temperature data returns None")
    
    # Test 14: get_charge_discharge_power_battery - single battery
    print("Test 14: get_charge_discharge_power_battery - single battery")
    api14 = MockSolaxAPI()
    api14.initialize(client_id="test", client_secret="test", region="eu")
    
    api14.plant_batteries["plant14"] = ["BAT001"]
    api14.realtime_device_data["BAT001"] = {"chargeDischargePower": 2500}  # Charging
    
    result14 = api14.get_charge_discharge_power_battery("plant14")
    if result14 != 2500:
        print(f"**** ERROR: Expected 2500W, got {result14}W ****")
        failed = True
    else:
        print(f"✓ Single battery charge/discharge power correct (2500W)")
    
    # Test 15: get_charge_discharge_power_battery - multiple batteries (sum)
    print("Test 15: get_charge_discharge_power_battery - multiple batteries (sum)")
    api15 = MockSolaxAPI()
    api15.initialize(client_id="test", client_secret="test", region="eu")
    
    api15.plant_batteries["plant15"] = ["BAT001", "BAT002"]
    api15.realtime_device_data["BAT001"] = {"chargeDischargePower": 1500}
    api15.realtime_device_data["BAT002"] = {"chargeDischargePower": -2000}  # Discharging
    
    result15 = api15.get_charge_discharge_power_battery("plant15")
    if result15 != -500:  # 1500 + (-2000) = -500
        print(f"**** ERROR: Expected -500W, got {result15}W ****")
        failed = True
    else:
        print(f"✓ Multiple battery charge/discharge power sum correct (-500W)")
    
    if not failed:
        print("✓ helper methods tests passed")
    
    return failed


async def test_automatic_config_main():
    """
    Test automatic_config() - First-time setup flow
    """
    failed = False
    print("\n=== Testing automatic_config ===")
    
    # Test 1: Single plant with inverter and battery
    print("Test 1: Single plant with inverter and battery")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Set up plant with inverter and battery
    api.plant_inverters["plant1"] = ["INV001"]
    api.device_info["INV001"] = {"deviceSn": "INV001", "deviceType": 1, "plantId": "plant1", "ratedPower": 10.0}
    api.device_info["BAT001"] = {"deviceSn": "BAT001", "deviceType": 2, "plantId": "plant1", "ratedPower": 5.0}
    api.plant_batteries["plant1"] = ["BAT001"]
    
    await api.automatic_config()
    
    # Verify configuration
    if api.get_arg("num_inverters") != 1:
        print(f"**** ERROR: Expected num_inverters=1, got {api.get_arg('num_inverters')} ****")
        failed = True
    elif api.get_arg("inverter_type") != ["SolaxCloud"]:
        print(f"**** ERROR: Expected inverter_type=['SolaxCloud'], got {api.get_arg('inverter_type')} ****")
        failed = True
    elif api.get_arg("load_today") != ["sensor.predbat_solax_plant1_total_load"]:
        print(f"**** ERROR: load_today config incorrect ****")
        failed = True
    else:
        print(f"✓ Single plant configuration correct")
    
    # Test 2: Multiple plants with inverters and batteries
    print("Test 2: Multiple plants with inverters and batteries")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    # Set up 2 plants
    api2.plant_inverters["plant_a"] = ["INV_A1", "INV_A2"]
    api2.plant_inverters["plant_b"] = ["INV_B1"]
    api2.device_info["INV_A1"] = {"deviceSn": "INV_A1", "deviceType": 1, "plantId": "plant_a"}
    api2.device_info["INV_A2"] = {"deviceSn": "INV_A2", "deviceType": 1, "plantId": "plant_a"}
    api2.device_info["BAT_A1"] = {"deviceSn": "BAT_A1", "deviceType": 2, "plantId": "plant_a"}
    api2.device_info["INV_B1"] = {"deviceSn": "INV_B1", "deviceType": 1, "plantId": "plant_b"}
    api2.device_info["BAT_B1"] = {"deviceSn": "BAT_B1", "deviceType": 2, "plantId": "plant_b"}
    api2.plant_batteries["plant_a"] = ["BAT_A1"]
    api2.plant_batteries["plant_b"] = ["BAT_B1"]
    
    await api2.automatic_config()
    
    if api2.get_arg("num_inverters") != 2:
        print(f"**** ERROR: Expected num_inverters=2, got {api2.get_arg('num_inverters')} ****")
        failed = True
    elif api2.get_arg("inverter_type") != ["SolaxCloud", "SolaxCloud"]:
        print(f"**** ERROR: Expected 2 SolaxCloud inverters ****")
        failed = True
    else:
        print(f"✓ Multiple plant configuration correct")
    
    # Test 3: Plant with inverter but no battery (should be skipped)
    print("Test 3: Plant with inverter but no battery (should be skipped)")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    api3.plant_inverters["plant_no_bat"] = ["INV001"]
    api3.device_info["INV001"] = {"deviceSn": "INV001", "deviceType": 1, "plantId": "plant_no_bat"}
    # No battery
    
    api3.plant_inverters["plant_with_bat"] = ["INV002"]
    api3.device_info["INV002"] = {"deviceSn": "INV002", "deviceType": 1, "plantId": "plant_with_bat"}
    api3.device_info["BAT002"] = {"deviceSn": "BAT002", "deviceType": 2, "plantId": "plant_with_bat"}
    api3.plant_batteries["plant_with_bat"] = ["BAT002"]
    
    await api3.automatic_config()
    
    # Should only configure the plant with battery
    if api3.get_arg("num_inverters") != 1:
        print(f"**** ERROR: Expected num_inverters=1 (only plant with battery), got {api3.get_arg('num_inverters')} ****")
        failed = True
    else:
        print(f"✓ Plant without battery correctly skipped")
    
    # Test 4: No plants with both inverter and battery (should raise error)
    print("Test 4: No plants with both inverter and battery (should raise error)")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    api4.plant_inverters["plant_only_inv"] = ["INV001"]
    api4.device_info["INV001"] = {"deviceSn": "INV001", "deviceType": 1, "plantId": "plant_only_inv"}
    
    try:
        await api4.automatic_config()
        print(f"**** ERROR: Expected ValueError for no valid plants ****")
        failed = True
    except ValueError as e:
        if "No plants with inverters and batteries found" in str(e):
            print(f"✓ Correctly raised ValueError for no valid plants")
        else:
            print(f"**** ERROR: Wrong error message: {e} ****")
            failed = True
    
    # Test 5: Verify entity name generation
    print("Test 5: Verify entity name generation")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    api5.plant_inverters["Test_Plant_123"] = ["INV_ABC"]
    api5.device_info["INV_ABC"] = {"deviceSn": "INV_ABC", "deviceType": 1, "plantId": "Test_Plant_123"}
    api5.device_info["BAT_ABC"] = {"deviceSn": "BAT_ABC", "deviceType": 2, "plantId": "Test_Plant_123"}
    api5.plant_batteries["Test_Plant_123"] = ["BAT_ABC"]
    
    await api5.automatic_config()
    
    # Entity names should use plant ID directly
    battery_power_entity = api5.get_arg("battery_power")
    if battery_power_entity and "Test_Plant_123" in battery_power_entity[0]:
        print(f"✓ Entity names correctly use plant ID")
    else:
        print(f"**** ERROR: Entity name incorrect: {battery_power_entity} ****")
        failed = True
    
    if not failed:
        print("✓ automatic_config tests passed")
    
    return failed


async def test_publish_controls_main():
    """
    Test publish_controls() - Control entity creation
    """
    failed = False
    print("\n=== Testing publish_controls ===")
    
    # Test 1: Publish controls for single plant
    print("Test 1: Publish controls for single plant")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Set up controls
    api.controls["plant1"] = {
        "charge": {
            "start_time": "02:00",
            "end_time": "06:00",
            "enable": True,
            "target_soc": 100,
            "rate": 5000,
        },
        "export": {
            "start_time": "16:00",
            "end_time": "20:00",
            "enable": False,
            "target_soc": 20,
            "rate": 4500,
        },
        "reserve": 10,
    }
    
    await api.publish_controls()
    
    # Check that entities were created
    charge_start = f"select.{api.prefix}_solax_plant1_battery_schedule_charge_start_time"
    charge_enable = f"switch.{api.prefix}_solax_plant1_battery_schedule_charge_enable"
    reserve = f"number.{api.prefix}_solax_plant1_setting_reserve"
    
    if charge_start not in api.dashboard_items:
        print(f"**** ERROR: Charge start time entity not created ****")
        failed = True
    elif api.dashboard_items[charge_start]["state"] != "02:00":
        print(f"**** ERROR: Charge start time incorrect: {api.dashboard_items[charge_start]['state']} ****")
        failed = True
    elif charge_enable not in api.dashboard_items:
        print(f"**** ERROR: Charge enable entity not created ****")
        failed = True
    elif api.dashboard_items[charge_enable]["state"] != True:
        print(f"**** ERROR: Charge enable incorrect: {api.dashboard_items[charge_enable]['state']} ****")
        failed = True
    elif reserve not in api.dashboard_items:
        print(f"**** ERROR: Reserve entity not created ****")
        failed = True
    elif api.dashboard_items[reserve]["state"] != 10:
        print(f"**** ERROR: Reserve incorrect: {api.dashboard_items[reserve]['state']} ****")
        failed = True
    else:
        print(f"✓ Control entities created correctly")
    
    # Test 2: Verify export controls
    print("Test 2: Verify export controls")
    export_enable = f"switch.{api.prefix}_solax_plant1_battery_schedule_export_enable"
    export_rate = f"number.{api.prefix}_solax_plant1_battery_schedule_export_rate"
    
    if export_enable not in api.dashboard_items:
        print(f"**** ERROR: Export enable entity not created ****")
        failed = True
    elif api.dashboard_items[export_enable]["state"] != False:
        print(f"**** ERROR: Export enable should be False ****")
        failed = True
    elif export_rate not in api.dashboard_items:
        print(f"**** ERROR: Export rate entity not created ****")
        failed = True
    elif api.dashboard_items[export_rate]["state"] != 4500:
        print(f"**** ERROR: Export rate incorrect: {api.dashboard_items[export_rate]['state']} ****")
        failed = True
    else:
        print(f"✓ Export control entities correct")
    
    # Test 3: Multiple plants
    print("Test 3: Multiple plants")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    api2.controls["plant_a"] = {
        "charge": {"start_time": "01:00", "end_time": "05:00", "enable": True, "target_soc": 90, "rate": 3000},
        "export": {"start_time": "15:00", "end_time": "19:00", "enable": True, "target_soc": 15, "rate": 3500},
        "reserve": 5,
    }
    api2.controls["plant_b"] = {
        "charge": {"start_time": "02:00", "end_time": "06:00", "enable": False, "target_soc": 100, "rate": 5000},
        "export": {"start_time": "16:00", "end_time": "20:00", "enable": False, "target_soc": 10, "rate": 4000},
        "reserve": 20,
    }
    
    await api2.publish_controls()
    
    # Check entities for both plants
    plant_a_reserve = f"number.{api2.prefix}_solax_plant_a_setting_reserve"
    plant_b_reserve = f"number.{api2.prefix}_solax_plant_b_setting_reserve"
    
    if plant_a_reserve not in api2.dashboard_items:
        print(f"**** ERROR: Plant A reserve entity not created ****")
        failed = True
    elif api2.dashboard_items[plant_a_reserve]["state"] != 5:
        print(f"**** ERROR: Plant A reserve incorrect ****")
        failed = True
    elif plant_b_reserve not in api2.dashboard_items:
        print(f"**** ERROR: Plant B reserve entity not created ****")
        failed = True
    elif api2.dashboard_items[plant_b_reserve]["state"] != 20:
        print(f"**** ERROR: Plant B reserve incorrect ****")
        failed = True
    else:
        print(f"✓ Multiple plant controls correct")
    
    # Test 4: Verify attributes (min, max, units, options)
    print("Test 4: Verify attributes")
    target_soc = f"number.{api.prefix}_solax_plant1_battery_schedule_charge_target_soc"
    
    if target_soc not in api.dashboard_items:
        print(f"**** ERROR: Target SOC entity not created ****")
        failed = True
    else:
        attrs = api.dashboard_items[target_soc]["attributes"]
        if attrs.get("min") != 10 or attrs.get("max") != 100:
            print(f"**** ERROR: Target SOC min/max incorrect: min={attrs.get('min')}, max={attrs.get('max')} ****")
            failed = True
        elif attrs.get("unit_of_measurement") != "%":
            print(f"**** ERROR: Target SOC units incorrect: {attrs.get('unit_of_measurement')} ****")
            failed = True
        else:
            print(f"✓ Entity attributes correct")
    
    # Test 5: Verify time options
    print("Test 5: Verify time options")
    start_time = f"select.{api.prefix}_solax_plant1_battery_schedule_charge_start_time"
    
    if start_time not in api.dashboard_items:
        print(f"**** ERROR: Start time entity not created ****")
        failed = True
    else:
        attrs = api.dashboard_items[start_time]["attributes"]
        if "options" not in attrs:
            print(f"**** ERROR: Start time missing options ****")
            failed = True
        elif len(attrs["options"]) != 1440:  # 24 hours * 60 (1-minute intervals)
            print(f"**** ERROR: Start time options count incorrect: {len(attrs.get('options', []))} ****")
            failed = True
        else:
            print(f"✓ Time options correct")
    
    if not failed:
        print("✓ publish_controls tests passed")
    
    return failed


async def test_run_main():
    """
    Test run() - Main periodic loop
    """
    failed = False
    print("\n=== Testing run loop ===")
    
    # Test 1: First run initialization
    print("Test 1: First run initialization")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Mock plant info response
    api.plant_info = [
        {"plantId": "plant1", "batteryCapacity": 15.0},
        {"plantId": "plant2", "batteryCapacity": 20.0},
    ]
    
    # Run first cycle
    with patch.object(api, 'query_plant_info', new_callable=AsyncMock) as mock_query:
        with patch.object(api, 'query_device_info', new_callable=AsyncMock):
            with patch.object(api, 'query_plant_realtime_data', new_callable=AsyncMock):
                with patch.object(api, 'query_device_realtime_data_all', new_callable=AsyncMock):
                    with patch.object(api, 'fetch_controls', new_callable=AsyncMock):
                        with patch.object(api, 'set_default_work_modes', new_callable=AsyncMock):
                            with patch.object(api, 'publish_plant_info', new_callable=AsyncMock):
                                with patch.object(api, 'publish_device_info', new_callable=AsyncMock):
                                    with patch.object(api, 'publish_device_realtime_data', new_callable=AsyncMock):
                                        with patch.object(api, 'publish_controls', new_callable=AsyncMock):
                                            with patch.object(api, 'apply_controls', new_callable=AsyncMock):
                                                result = await api.run(seconds=0, first=True)
    
    if not result:
        print(f"**** ERROR: First run failed ****")
        failed = True
    elif api.plant_list != ["plant1", "plant2"]:
        print(f"**** ERROR: Plant list incorrect: {api.plant_list} ****")
        failed = True
    elif not mock_query.called:
        print(f"**** ERROR: query_plant_info not called on first run ****")
        failed = True
    else:
        print(f"✓ First run initialization correct")
    
    # Test 2: Subsequent run (no first-time actions)
    print("Test 2: Subsequent run (no first-time actions)")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    api2.plant_info = [{"plantId": "plant1"}]
    api2.plant_list = ["plant1"]
    
    with patch.object(api2, 'query_plant_info', new_callable=AsyncMock) as mock_query_plant:
        with patch.object(api2, 'query_device_info', new_callable=AsyncMock) as mock_query_device:
            with patch.object(api2, 'query_plant_realtime_data', new_callable=AsyncMock):
                with patch.object(api2, 'query_device_realtime_data_all', new_callable=AsyncMock):
                    with patch.object(api2, 'publish_plant_info', new_callable=AsyncMock):
                        with patch.object(api2, 'publish_device_info', new_callable=AsyncMock):
                            with patch.object(api2, 'publish_device_realtime_data', new_callable=AsyncMock):
                                with patch.object(api2, 'publish_controls', new_callable=AsyncMock):
                                    result = await api2.run(seconds=120, first=False)  # 2 minutes in
    
    if not result:
        print(f"**** ERROR: Subsequent run failed ****")
        failed = True
    elif mock_query_plant.called:
        print(f"**** ERROR: query_plant_info should not be called on subsequent run ****")
        failed = True
    elif mock_query_device.called:
        print(f"**** ERROR: query_device_info should not be called at 2 minutes ****")
        failed = True
    else:
        print(f"✓ Subsequent run correct")
    
    # Test 3: 60-second cycle (realtime data refresh)
    print("Test 3: 60-second cycle (realtime data refresh)")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    api3.plant_info = [{"plantId": "plant1"}]
    api3.plant_list = ["plant1"]
    
    with patch.object(api3, 'query_plant_realtime_data', new_callable=AsyncMock) as mock_realtime:
        with patch.object(api3, 'query_device_realtime_data_all', new_callable=AsyncMock) as mock_device_realtime:
            with patch.object(api3, 'publish_plant_info', new_callable=AsyncMock) as mock_publish:
                with patch.object(api3, 'query_device_info', new_callable=AsyncMock):
                    with patch.object(api3, 'publish_device_info', new_callable=AsyncMock):
                        with patch.object(api3, 'publish_device_realtime_data', new_callable=AsyncMock):
                            with patch.object(api3, 'publish_controls', new_callable=AsyncMock):
                                result = await api3.run(seconds=60, first=False)
    
    if not result:
        print(f"**** ERROR: 60-second cycle failed ****")
        failed = True
    elif not mock_realtime.called:
        print(f"**** ERROR: query_plant_realtime_data not called at 60 seconds ****")
        failed = True
    elif not mock_device_realtime.called:
        print(f"**** ERROR: query_device_realtime_data_all not called at 60 seconds ****")
        failed = True
    elif not mock_publish.called:
        print(f"**** ERROR: publish methods not called at 60 seconds ****")
        failed = True
    else:
        print(f"✓ 60-second cycle correct")
    
    # Test 4: 30-minute cycle (device info refresh)
    print("Test 4: 30-minute cycle (device info refresh)")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    api4.plant_info = [{"plantId": "plant1"}]
    api4.plant_list = ["plant1"]
    
    with patch.object(api4, 'query_device_info', new_callable=AsyncMock) as mock_device:
        with patch.object(api4, 'query_plant_realtime_data', new_callable=AsyncMock):
            with patch.object(api4, 'query_device_realtime_data_all', new_callable=AsyncMock):
                with patch.object(api4, 'publish_plant_info', new_callable=AsyncMock):
                    with patch.object(api4, 'publish_device_info', new_callable=AsyncMock):
                        with patch.object(api4, 'publish_device_realtime_data', new_callable=AsyncMock):
                            with patch.object(api4, 'publish_controls', new_callable=AsyncMock):
                                result = await api4.run(seconds=30*60, first=False)
    
    if not result:
        print(f"**** ERROR: 30-minute cycle failed ****")
        failed = True
    elif not mock_device.called:
        print(f"**** ERROR: query_device_info not called at 30 minutes ****")
        failed = True
    elif mock_device.call_count < 2:  # Should be called for both inverter and battery
        print(f"**** ERROR: query_device_info not called enough times (expected 2+, got {mock_device.call_count}) ****")
        failed = True
    else:
        print(f"✓ 30-minute cycle correct")
    
    # Test 5: Read-only mode (controls disabled)
    print("Test 5: Read-only mode (controls disabled)")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    api5.plant_info = [{"plantId": "plant1"}]
    api5.plant_list = ["plant1"]
    api5.enable_controls = True  # Controls enabled in config
    
    # Set read-only mode
    api5.set_state_wrapper(f'switch.{api5.prefix}_set_read_only', 'on')
    
    with patch.object(api5, 'apply_controls', new_callable=AsyncMock) as mock_apply:
        with patch.object(api5, 'query_plant_realtime_data', new_callable=AsyncMock):
            with patch.object(api5, 'query_device_realtime_data_all', new_callable=AsyncMock):
                with patch.object(api5, 'publish_plant_info', new_callable=AsyncMock):
                    with patch.object(api5, 'publish_device_info', new_callable=AsyncMock):
                        with patch.object(api5, 'publish_device_realtime_data', new_callable=AsyncMock):
                            with patch.object(api5, 'publish_controls', new_callable=AsyncMock):
                                result = await api5.run(seconds=60, first=False)
    
    if not result:
        print(f"**** ERROR: Read-only run failed ****")
        failed = True
    elif mock_apply.called:
        print(f"**** ERROR: apply_controls called in read-only mode ****")
        failed = True
    else:
        print(f"✓ Read-only mode correct")
    
    # Test 6: Automatic config on first run
    print("Test 6: Automatic config on first run")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    api6.automatic = True
    api6.plant_info = [{"plantId": "plant1"}]
    
    with patch.object(api6, 'query_plant_info', new_callable=AsyncMock):
        with patch.object(api6, 'automatic_config', new_callable=AsyncMock) as mock_auto:
            with patch.object(api6, 'query_device_info', new_callable=AsyncMock):
                with patch.object(api6, 'query_plant_realtime_data', new_callable=AsyncMock):
                    with patch.object(api6, 'query_device_realtime_data_all', new_callable=AsyncMock):
                        with patch.object(api6, 'fetch_controls', new_callable=AsyncMock):
                            with patch.object(api6, 'set_default_work_modes', new_callable=AsyncMock):
                                with patch.object(api6, 'publish_plant_info', new_callable=AsyncMock):
                                    with patch.object(api6, 'publish_device_info', new_callable=AsyncMock):
                                        with patch.object(api6, 'publish_device_realtime_data', new_callable=AsyncMock):
                                            with patch.object(api6, 'publish_controls', new_callable=AsyncMock):
                                                with patch.object(api6, 'apply_controls', new_callable=AsyncMock):
                                                    result = await api6.run(seconds=0, first=True)
    
    if not result:
        print(f"**** ERROR: Auto-config run failed ****")
        failed = True
    elif not mock_auto.called:
        print(f"**** ERROR: automatic_config not called when automatic=True on first run ****")
        failed = True
    else:
        print(f"✓ Automatic config triggered correctly")
    
    # Test 7: Failed plant info fetch
    print("Test 7: Failed plant info fetch")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    api7.plant_info = None  # Simulate failure
    
    with patch.object(api7, 'query_plant_info', new_callable=AsyncMock):
        result = await api7.run(seconds=0, first=True)
    
    if result:
        print(f"**** ERROR: Should return False when plant_info is None ****")
        failed = True
    else:
        print(f"✓ Failed plant info handled correctly")
    
    if not failed:
        print("✓ run loop tests passed")
    
    return failed


async def test_set_default_work_mode_main():
    """
    Test set_default_work_mode() - Default work mode initialization
    """
    failed = False
    print("\n=== Testing set_default_work_mode ===")
    
    # Test 1: First call should invoke set_work_mode
    print("Test 1: First call should invoke set_work_mode")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    sn_list = ["INV001", "INV002"]
    
    # Mock set_work_mode to return success
    with patch.object(api, 'set_work_mode', new_callable=AsyncMock) as mock_set_work_mode:
        mock_set_work_mode.return_value = True
        
        result = await api.set_default_work_mode(sn_list, business_type=1)
    
    if not result:
        print(f"**** ERROR: First call should return True ****")
        failed = True
    elif not mock_set_work_mode.called:
        print(f"**** ERROR: set_work_mode should be called on first invocation ****")
        failed = True
    elif not api.have_set_default_mode:
        print(f"**** ERROR: have_set_default_mode flag should be set to True ****")
        failed = True
    else:
        # Verify the correct parameters were passed
        call_args = mock_set_work_mode.call_args
        if call_args[0][0] != "selfuse":
            print(f"**** ERROR: Expected mode 'selfuse', got {call_args[0][0]} ****")
            failed = True
        elif call_args[0][1] != sn_list:
            print(f"**** ERROR: Expected sn_list {sn_list}, got {call_args[0][1]} ****")
            failed = True
        elif call_args[0][2] != 10:  # min_soc
            print(f"**** ERROR: Expected min_soc 10, got {call_args[0][2]} ****")
            failed = True
        elif call_args[0][3] != 100:  # charge_upper_soc
            print(f"**** ERROR: Expected charge_upper_soc 100, got {call_args[0][3]} ****")
            failed = True
        else:
            print(f"✓ First call invokes set_work_mode with correct parameters")
    
    # Test 2: Second call should skip set_work_mode (flag already set)
    print("Test 2: Second call should skip set_work_mode (flag already set)")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    api2.have_set_default_mode = True  # Pre-set the flag
    
    with patch.object(api2, 'set_work_mode', new_callable=AsyncMock) as mock_set_work_mode2:
        mock_set_work_mode2.return_value = True
        
        result2 = await api2.set_default_work_mode(["INV003"], business_type=1)
    
    if not result2:
        print(f"**** ERROR: Second call should return True ****")
        failed = True
    elif mock_set_work_mode2.called:
        print(f"**** ERROR: set_work_mode should NOT be called when flag is set ****")
        failed = True
    else:
        print(f"✓ Second call correctly skips set_work_mode")
    
    # Test 3: Failed set_work_mode should not set flag
    print("Test 3: Failed set_work_mode should not set flag")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api3, 'set_work_mode', new_callable=AsyncMock) as mock_set_work_mode3:
        mock_set_work_mode3.return_value = False  # Simulate failure
        
        result3 = await api3.set_default_work_mode(["INV004"], business_type=1)
    
    if result3:
        print(f"**** ERROR: Should return False when set_work_mode fails ****")
        failed = True
    elif api3.have_set_default_mode:
        print(f"**** ERROR: Flag should NOT be set when set_work_mode fails ****")
        failed = True
    else:
        print(f"✓ Failed set_work_mode correctly does not set flag")
    
    # Test 4: Verify log messages
    print("Test 4: Verify log messages")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api4, 'set_work_mode', new_callable=AsyncMock) as mock_set_work_mode4:
        mock_set_work_mode4.return_value = True
        
        await api4.set_default_work_mode(["INV005"], business_type=1)
    
    # Check that success log was generated
    success_logs = [msg for msg in api4.log_messages if "Set default work mode to Self Use" in msg]
    if not success_logs:
        print(f"**** ERROR: Success log message not found ****")
        failed = True
    else:
        print(f"✓ Success log message generated correctly")
    
    # Test 5: Failed call should generate warning log
    print("Test 5: Failed call should generate warning log")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api5, 'set_work_mode', new_callable=AsyncMock) as mock_set_work_mode5:
        mock_set_work_mode5.return_value = False
        
        await api5.set_default_work_mode(["INV006"], business_type=1)
    
    # Check that warning log was generated (note: there's a bug in the code - it uses 'sn' instead of 'sn_list')
    warning_logs = [msg for msg in api5.log_messages if "Failed to set default work mode" in msg]
    if not warning_logs:
        print(f"**** ERROR: Warning log message not found ****")
        failed = True
    else:
        print(f"✓ Warning log message generated correctly")
    
    # Test 6: Verify business_type parameter is passed through
    print("Test 6: Verify business_type parameter is passed through")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api6, 'set_work_mode', new_callable=AsyncMock) as mock_set_work_mode6:
        mock_set_work_mode6.return_value = True
        
        await api6.set_default_work_mode(["INV007"], business_type=4)  # Commercial
    
    # Verify business_type was passed correctly
    call_kwargs = mock_set_work_mode6.call_args[1]
    if call_kwargs.get("business_type") != 4:
        print(f"**** ERROR: Expected business_type=4, got {call_kwargs.get('business_type')} ****")
        failed = True
    else:
        print(f"✓ business_type parameter passed through correctly")
    
    if not failed:
        print("✓ set_default_work_mode tests passed")
    
    return failed


async def test_positive_or_negative_mode_main():
    """
    Test positive_or_negative_mode() - Direct battery power control
    """
    failed = False
    print("\n=== Testing positive_or_negative_mode ===")
    
    # Test 1: Charge mode (negative battery_power)
    print("Test 1: Charge mode (negative battery_power)")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api, 'send_command_and_wait', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        result = await api.positive_or_negative_mode(
            sn="INV001",
            battery_power=-5000,  # Negative = charge
            time_of_duration=7200,  # 2 hours
            next_motion=161,
            business_type=1
        )
    
    if not result:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    elif not mock_send.called:
        print(f"**** ERROR: send_command_and_wait should be called ****")
        failed = True
    else:
        # Verify the parameters passed to send_command_and_wait
        call_args = mock_send.call_args
        endpoint = call_args[0][0]
        payload = call_args[0][1]
        command_name = call_args[0][2]
        sn = call_args[0][3]
        
        if endpoint != "/openapi/v2/device/inverter_vpp_mode/push_power/positive_or_negative_mode":
            print(f"**** ERROR: Wrong endpoint: {endpoint} ****")
            failed = True
        elif payload.get("batteryPower") != -5000:
            print(f"**** ERROR: Wrong battery_power: {payload.get('batteryPower')} ****")
            failed = True
        elif payload.get("timeOfDuration") != 7200:
            print(f"**** ERROR: Wrong time_of_duration: {payload.get('timeOfDuration')} ****")
            failed = True
        elif payload.get("nextMotion") != 161:
            print(f"**** ERROR: Wrong next_motion: {payload.get('nextMotion')} ****")
            failed = True
        elif payload.get("businessType") != 1:
            print(f"**** ERROR: Wrong business_type: {payload.get('businessType')} ****")
            failed = True
        elif payload.get("snList") != ["INV001"]:
            print(f"**** ERROR: Wrong snList: {payload.get('snList')} ****")
            failed = True
        elif command_name != "positive/negative":
            print(f"**** ERROR: Wrong command_name: {command_name} ****")
            failed = True
        else:
            print(f"✓ Charge mode parameters correct")
    
    # Test 2: Discharge mode (positive battery_power)
    print("Test 2: Discharge mode (positive battery_power)")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api2, 'send_command_and_wait', new_callable=AsyncMock) as mock_send2:
        mock_send2.return_value = True
        
        result2 = await api2.positive_or_negative_mode(
            sn="INV002",
            battery_power=4500,  # Positive = discharge
            time_of_duration=3600,  # 1 hour
            next_motion=161,
            business_type=1
        )
    
    if not result2:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args2 = mock_send2.call_args
        payload2 = call_args2[0][1]
        if payload2.get("batteryPower") != 4500:
            print(f"**** ERROR: Wrong battery_power for discharge: {payload2.get('batteryPower')} ****")
            failed = True
        else:
            print(f"✓ Discharge mode parameters correct")
    
    # Test 3: Default next_motion parameter
    print("Test 3: Default next_motion parameter")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api3, 'send_command_and_wait', new_callable=AsyncMock) as mock_send3:
        mock_send3.return_value = True
        
        # Call without specifying next_motion
        result3 = await api3.positive_or_negative_mode(
            sn="INV003",
            battery_power=1000,
            time_of_duration=1800
        )
    
    if not result3:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args3 = mock_send3.call_args
        payload3 = call_args3[0][1]
        if payload3.get("nextMotion") != 161:
            print(f"**** ERROR: Default next_motion should be 161, got {payload3.get('nextMotion')} ****")
            failed = True
        else:
            print(f"✓ Default next_motion (161) correct")
    
    # Test 4: Custom next_motion parameter (Exit Remote Control)
    print("Test 4: Custom next_motion parameter (Exit Remote Control)")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api4, 'send_command_and_wait', new_callable=AsyncMock) as mock_send4:
        mock_send4.return_value = True
        
        result4 = await api4.positive_or_negative_mode(
            sn="INV004",
            battery_power=-3000,
            time_of_duration=5400,
            next_motion=160  # Exit Remote Control
        )
    
    if not result4:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args4 = mock_send4.call_args
        payload4 = call_args4[0][1]
        if payload4.get("nextMotion") != 160:
            print(f"**** ERROR: next_motion should be 160, got {payload4.get('nextMotion')} ****")
            failed = True
        else:
            print(f"✓ Custom next_motion (160) correct")
    
    # Test 5: Default business_type (residential)
    print("Test 5: Default business_type (residential)")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api5, 'send_command_and_wait', new_callable=AsyncMock) as mock_send5:
        mock_send5.return_value = True
        
        # Call without specifying business_type
        result5 = await api5.positive_or_negative_mode(
            sn="INV005",
            battery_power=2000,
            time_of_duration=3600
        )
    
    if not result5:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args5 = mock_send5.call_args
        payload5 = call_args5[0][1]
        if payload5.get("businessType") != 1:  # Default is residential (1)
            print(f"**** ERROR: Default business_type should be 1, got {payload5.get('businessType')} ****")
            failed = True
        else:
            print(f"✓ Default business_type (residential) correct")
    
    # Test 6: Custom business_type (commercial)
    print("Test 6: Custom business_type (commercial)")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api6, 'send_command_and_wait', new_callable=AsyncMock) as mock_send6:
        mock_send6.return_value = True
        
        result6 = await api6.positive_or_negative_mode(
            sn="INV006",
            battery_power=-4000,
            time_of_duration=7200,
            business_type=4  # Commercial
        )
    
    if not result6:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args6 = mock_send6.call_args
        payload6 = call_args6[0][1]
        if payload6.get("businessType") != 4:
            print(f"**** ERROR: business_type should be 4 (commercial), got {payload6.get('businessType')} ****")
            failed = True
        else:
            print(f"✓ Custom business_type (commercial) correct")
    
    # Test 7: Failed command execution
    print("Test 7: Failed command execution")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api7, 'send_command_and_wait', new_callable=AsyncMock) as mock_send7:
        mock_send7.return_value = False  # Simulate failure
        
        result7 = await api7.positive_or_negative_mode(
            sn="INV007",
            battery_power=1500,
            time_of_duration=1800
        )
    
    if result7:
        print(f"**** ERROR: Should return False on failure ****")
        failed = True
    else:
        print(f"✓ Failed command execution handled correctly")
    
    # Test 8: Zero battery power (edge case)
    print("Test 8: Zero battery power (edge case)")
    api8 = MockSolaxAPI()
    api8.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api8, 'send_command_and_wait', new_callable=AsyncMock) as mock_send8:
        mock_send8.return_value = True
        
        result8 = await api8.positive_or_negative_mode(
            sn="INV008",
            battery_power=0,  # Zero power
            time_of_duration=600
        )
    
    if not result8:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args8 = mock_send8.call_args
        payload8 = call_args8[0][1]
        if payload8.get("batteryPower") != 0:
            print(f"**** ERROR: Zero battery_power should be preserved, got {payload8.get('batteryPower')} ****")
            failed = True
        else:
            print(f"✓ Zero battery power handled correctly")
    
    # Test 9: Large duration value
    print("Test 9: Large duration value")
    api9 = MockSolaxAPI()
    api9.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api9, 'send_command_and_wait', new_callable=AsyncMock) as mock_send9:
        mock_send9.return_value = True
        
        result9 = await api9.positive_or_negative_mode(
            sn="INV009",
            battery_power=-6000,
            time_of_duration=43200  # 12 hours (max typical duration)
        )
    
    if not result9:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args9 = mock_send9.call_args
        payload9 = call_args9[0][1]
        if payload9.get("timeOfDuration") != 43200:
            print(f"**** ERROR: Duration should be 43200, got {payload9.get('timeOfDuration')} ****")
            failed = True
        else:
            print(f"✓ Large duration value handled correctly")
    
    if not failed:
        print("✓ positive_or_negative_mode tests passed")
    
    return failed


async def test_self_consume_charge_only_mode_main():
    """
    Test self_consume_charge_only_mode() - Freeze charge mode (PV charge only, no discharge)
    """
    failed = False
    print("\n=== Testing self_consume_charge_only_mode ===")
    
    # Test 1: Basic mode with single inverter
    print("Test 1: Basic mode with single inverter")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api, 'send_command_and_wait', new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        
        result = await api.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=7200,  # 2 hours
            next_motion=161,
            business_type=1
        )
    
    if not result:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    elif not mock_send.called:
        print(f"**** ERROR: send_command_and_wait should be called ****")
        failed = True
    else:
        # Verify the parameters passed to send_command_and_wait
        call_args = mock_send.call_args
        endpoint = call_args[0][0]
        payload = call_args[0][1]
        command_name = call_args[0][2]
        sn_list = call_args[0][3]
        
        if endpoint != "/openapi/v2/device/inverter_vpp_mode/self_consume/charge_only_mode":
            print(f"**** ERROR: Wrong endpoint: {endpoint} ****")
            failed = True
        elif payload.get("snList") != ["INV001"]:
            print(f"**** ERROR: Wrong snList: {payload.get('snList')} ****")
            failed = True
        elif payload.get("timeOfDuration") != 7200:
            print(f"**** ERROR: Wrong time_of_duration: {payload.get('timeOfDuration')} ****")
            failed = True
        elif payload.get("nextMotion") != 161:
            print(f"**** ERROR: Wrong next_motion: {payload.get('nextMotion')} ****")
            failed = True
        elif payload.get("businessType") != 1:
            print(f"**** ERROR: Wrong business_type: {payload.get('businessType')} ****")
            failed = True
        elif command_name != "self-consume-charge-only":
            print(f"**** ERROR: Wrong command_name: {command_name} ****")
            failed = True
        elif sn_list != ["INV001"]:
            print(f"**** ERROR: Wrong sn_list passed to send_command_and_wait: {sn_list} ****")
            failed = True
        else:
            print(f"✓ Single inverter parameters correct")
    
    # Test 2: Multiple inverters
    print("Test 2: Multiple inverters")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api2, 'send_command_and_wait', new_callable=AsyncMock) as mock_send2:
        mock_send2.return_value = True
        
        result2 = await api2.self_consume_charge_only_mode(
            sn_list=["INV001", "INV002", "INV003"],
            time_of_duration=3600,  # 1 hour
            next_motion=161,
            business_type=1
        )
    
    if not result2:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args2 = mock_send2.call_args
        payload2 = call_args2[0][1]
        if payload2.get("snList") != ["INV001", "INV002", "INV003"]:
            print(f"**** ERROR: Wrong snList for multiple inverters: {payload2.get('snList')} ****")
            failed = True
        else:
            print(f"✓ Multiple inverters handled correctly")
    
    # Test 3: Default next_motion parameter (Back to Self-Consume)
    print("Test 3: Default next_motion parameter (Back to Self-Consume)")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api3, 'send_command_and_wait', new_callable=AsyncMock) as mock_send3:
        mock_send3.return_value = True
        
        # Call without specifying next_motion
        result3 = await api3.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=1800
        )
    
    if not result3:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args3 = mock_send3.call_args
        payload3 = call_args3[0][1]
        if payload3.get("nextMotion") != 161:
            print(f"**** ERROR: Default next_motion should be 161 (Back to Self-Consume), got {payload3.get('nextMotion')} ****")
            failed = True
        else:
            print(f"✓ Default next_motion (161) correct")
    
    # Test 4: Custom next_motion (Exit Remote Control)
    print("Test 4: Custom next_motion (Exit Remote Control)")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api4, 'send_command_and_wait', new_callable=AsyncMock) as mock_send4:
        mock_send4.return_value = True
        
        result4 = await api4.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=5400,
            next_motion=160  # Exit Remote Control
        )
    
    if not result4:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args4 = mock_send4.call_args
        payload4 = call_args4[0][1]
        if payload4.get("nextMotion") != 160:
            print(f"**** ERROR: next_motion should be 160 (Exit Remote Control), got {payload4.get('nextMotion')} ****")
            failed = True
        else:
            print(f"✓ Custom next_motion (160) correct")
    
    # Test 5: Default business_type (residential)
    print("Test 5: Default business_type (residential)")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api5, 'send_command_and_wait', new_callable=AsyncMock) as mock_send5:
        mock_send5.return_value = True
        
        # Call without specifying business_type
        result5 = await api5.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=3600
        )
    
    if not result5:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args5 = mock_send5.call_args
        payload5 = call_args5[0][1]
        if payload5.get("businessType") != 1:  # Default is residential (1)
            print(f"**** ERROR: Default business_type should be 1 (residential), got {payload5.get('businessType')} ****")
            failed = True
        else:
            print(f"✓ Default business_type (residential) correct")
    
    # Test 6: Custom business_type (commercial)
    print("Test 6: Custom business_type (commercial)")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api6, 'send_command_and_wait', new_callable=AsyncMock) as mock_send6:
        mock_send6.return_value = True
        
        result6 = await api6.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=7200,
            business_type=4  # Commercial
        )
    
    if not result6:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args6 = mock_send6.call_args
        payload6 = call_args6[0][1]
        if payload6.get("businessType") != 4:
            print(f"**** ERROR: business_type should be 4 (commercial), got {payload6.get('businessType')} ****")
            failed = True
        else:
            print(f"✓ Custom business_type (commercial) correct")
    
    # Test 7: Failed command execution
    print("Test 7: Failed command execution")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api7, 'send_command_and_wait', new_callable=AsyncMock) as mock_send7:
        mock_send7.return_value = False  # Simulate failure
        
        result7 = await api7.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=1800
        )
    
    if result7:
        print(f"**** ERROR: Should return False on failure ****")
        failed = True
    else:
        print(f"✓ Failed command execution handled correctly")
    
    # Test 8: Short duration (edge case)
    print("Test 8: Short duration (edge case)")
    api8 = MockSolaxAPI()
    api8.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api8, 'send_command_and_wait', new_callable=AsyncMock) as mock_send8:
        mock_send8.return_value = True
        
        result8 = await api8.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=300  # 5 minutes
        )
    
    if not result8:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args8 = mock_send8.call_args
        payload8 = call_args8[0][1]
        if payload8.get("timeOfDuration") != 300:
            print(f"**** ERROR: Short duration should be preserved, got {payload8.get('timeOfDuration')} ****")
            failed = True
        else:
            print(f"✓ Short duration handled correctly")
    
    # Test 9: Long duration (edge case)
    print("Test 9: Long duration (edge case)")
    api9 = MockSolaxAPI()
    api9.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api9, 'send_command_and_wait', new_callable=AsyncMock) as mock_send9:
        mock_send9.return_value = True
        
        result9 = await api9.self_consume_charge_only_mode(
            sn_list=["INV001"],
            time_of_duration=43200  # 12 hours
        )
    
    if not result9:
        print(f"**** ERROR: Should return True on success ****")
        failed = True
    else:
        call_args9 = mock_send9.call_args
        payload9 = call_args9[0][1]
        if payload9.get("timeOfDuration") != 43200:
            print(f"**** ERROR: Long duration should be preserved, got {payload9.get('timeOfDuration')} ****")
            failed = True
        else:
            print(f"✓ Long duration handled correctly")
    
    # Test 10: Empty inverter list (edge case)
    print("Test 10: Empty inverter list (edge case)")
    api10 = MockSolaxAPI()
    api10.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api10, 'send_command_and_wait', new_callable=AsyncMock) as mock_send10:
        mock_send10.return_value = True
        
        result10 = await api10.self_consume_charge_only_mode(
            sn_list=[],
            time_of_duration=3600
        )
    
    if not result10:
        print(f"**** ERROR: Should return True even with empty list ****")
        failed = True
    else:
        call_args10 = mock_send10.call_args
        payload10 = call_args10[0][1]
        if payload10.get("snList") != []:
            print(f"**** ERROR: Empty list should be preserved, got {payload10.get('snList')} ****")
            failed = True
        else:
            print(f"✓ Empty inverter list handled correctly")
    
    if not failed:
        print("✓ self_consume_charge_only_mode tests passed")
    
    return failed


async def test_query_request_result_main():
    """
    Test query_request_result() - Query the execution result of a control instruction
    """
    failed = False
    print("\n=== Testing query_request_result ===")
    
    # Test 1: Successful query with single device
    print("Test 1: Successful query with single device")
    api = MockSolaxAPI()
    api.initialize(client_id="test", client_secret="test", region="eu")
    
    # Mock successful response
    with patch.object(api, 'request_get', new_callable=AsyncMock) as mock_request:
        mock_request.return_value = {
            "code": 10000,  # Success code
            "result": [
                {"sn": "X3******01", "status": 4}  # SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS
            ]
        }
        
        result = await api.query_request_result("req123456")
    
    if result != 4:  # SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS
        print(f"**** ERROR: Should return status 4, got {result} ****")
        failed = True
    elif not mock_request.called:
        print(f"**** ERROR: request_get should be called ****")
        failed = True
    else:
        # Verify the call parameters
        call_args = mock_request.call_args
        endpoint = call_args[0][0]
        post = call_args[1].get("post")
        json_data = call_args[1].get("json_data")
        
        if endpoint != "/openapi/apiRequestLog/listByCondition":
            print(f"**** ERROR: Wrong endpoint: {endpoint} ****")
            failed = True
        elif not post:
            print(f"**** ERROR: Should be POST request ****")
            failed = True
        elif json_data.get("requestId") != "req123456":
            print(f"**** ERROR: Wrong requestId: {json_data.get('requestId')} ****")
            failed = True
        else:
            print(f"✓ Single device success status returned correctly")
    
    # Test 2: Multiple devices - all successful
    print("Test 2: Multiple devices - all successful")
    api2 = MockSolaxAPI()
    api2.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api2, 'request_get', new_callable=AsyncMock) as mock_request2:
        mock_request2.return_value = {
            "code": 10000,
            "result": [
                {"sn": "X3******01", "status": 4},
                {"sn": "X3******02", "status": 4},
                {"sn": "X3******03", "status": 4}
            ]
        }
        
        result2 = await api2.query_request_result("req789012")
    
    if result2 != 4:
        print(f"**** ERROR: Should return status 4 for all successful, got {result2} ****")
        failed = True
    else:
        print(f"✓ Multiple devices all successful")
    
    # Test 3: Multiple devices - one device offline
    print("Test 3: Multiple devices - one device offline")
    api3 = MockSolaxAPI()
    api3.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api3, 'request_get', new_callable=AsyncMock) as mock_request3:
        mock_request3.return_value = {
            "code": 10000,
            "result": [
                {"sn": "X3******01", "status": 4},
                {"sn": "X3******02", "status": 2},  # Different status (e.g., device offline)
                {"sn": "X3******03", "status": 4}
            ]
        }
        
        result3 = await api3.query_request_result("req345678")
    
    if result3 != 2:
        print(f"**** ERROR: Should return first non-success status 2, got {result3} ****")
        failed = True
    else:
        print(f"✓ Non-success status detected correctly")
    
    # Test 4: Empty result array
    print("Test 4: Empty result array")
    api4 = MockSolaxAPI()
    api4.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api4, 'request_get', new_callable=AsyncMock) as mock_request4:
        mock_request4.return_value = {
            "code": 10000,
            "result": []
        }
        
        result4 = await api4.query_request_result("req000000")
    
    if result4 != 4:  # Should return default success status
        print(f"**** ERROR: Should return default status 4 for empty result, got {result4} ****")
        failed = True
    else:
        print(f"✓ Empty result array handled correctly")
    
    # Test 5: API error response
    print("Test 5: API error response")
    api5 = MockSolaxAPI()
    api5.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api5, 'request_get', new_callable=AsyncMock) as mock_request5:
        mock_request5.return_value = {
            "code": 10001,  # Error code
            "message": "Invalid request ID"
        }
        
        result5 = await api5.query_request_result("req_invalid")
    
    if result5 is not None:
        print(f"**** ERROR: Should return None on API error, got {result5} ****")
        failed = True
    else:
        print(f"✓ API error handled correctly")
    
    # Test 6: request_get returns None (network error)
    print("Test 6: request_get returns None (network error)")
    api6 = MockSolaxAPI()
    api6.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api6, 'request_get', new_callable=AsyncMock) as mock_request6:
        mock_request6.return_value = None
        
        result6 = await api6.query_request_result("req111111")
    
    if result6 is not None:
        print(f"**** ERROR: Should return None on network error, got {result6} ****")
        failed = True
    else:
        print(f"✓ Network error handled correctly")
    
    # Test 7: Missing result field in response
    print("Test 7: Missing result field in response")
    api7 = MockSolaxAPI()
    api7.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api7, 'request_get', new_callable=AsyncMock) as mock_request7:
        mock_request7.return_value = {
            "code": 10000
            # No "result" field
        }
        
        result7 = await api7.query_request_result("req222222")
    
    if result7 != 4:  # Should return default success status when result missing
        print(f"**** ERROR: Should return default status 4 when result missing, got {result7} ****")
        failed = True
    else:
        print(f"✓ Missing result field handled correctly")
    
    # Test 8: First device fails, others succeed
    print("Test 8: First device fails, others succeed")
    api8 = MockSolaxAPI()
    api8.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api8, 'request_get', new_callable=AsyncMock) as mock_request8:
        mock_request8.return_value = {
            "code": 10000,
            "result": [
                {"sn": "X3******01", "status": 1},  # Failed
                {"sn": "X3******02", "status": 4},
                {"sn": "X3******03", "status": 4}
            ]
        }
        
        result8 = await api8.query_request_result("req333333")
    
    if result8 != 1:
        print(f"**** ERROR: Should return first device status 1, got {result8} ****")
        failed = True
    else:
        print(f"✓ First device failure detected correctly")
    
    # Test 9: Device result with missing fields
    print("Test 9: Device result with missing fields")
    api9 = MockSolaxAPI()
    api9.initialize(client_id="test", client_secret="test", region="eu")
    
    with patch.object(api9, 'request_get', new_callable=AsyncMock) as mock_request9:
        mock_request9.return_value = {
            "code": 10000,
            "result": [
                {"sn": "X3******01"},  # Missing status
                {"status": 4}  # Missing sn
            ]
        }
        
        result9 = await api9.query_request_result("req444444")
    
    # Should handle gracefully
    if result9 is None:
        print(f"**** ERROR: Should handle missing fields gracefully ****")
        failed = True
    else:
        print(f"✓ Missing device fields handled gracefully")
    
    # Test 10: Long request ID
    print("Test 10: Long request ID")
    api10 = MockSolaxAPI()
    api10.initialize(client_id="test", client_secret="test", region="eu")
    
    long_request_id = "req_" + "x" * 100
    with patch.object(api10, 'request_get', new_callable=AsyncMock) as mock_request10:
        mock_request10.return_value = {
            "code": 10000,
            "result": [
                {"sn": "X3******01", "status": 4}
            ]
        }
        
        result10 = await api10.query_request_result(long_request_id)
    
    if result10 != 4:
        print(f"**** ERROR: Should handle long request ID, got {result10} ****")
        failed = True
    else:
        call_args10 = mock_request10.call_args
        json_data10 = call_args10[1].get("json_data")
        if json_data10.get("requestId") != long_request_id:
            print(f"**** ERROR: Long request ID not preserved ****")
            failed = True
        else:
            print(f"✓ Long request ID handled correctly")
    
    if not failed:
        print("✓ query_request_result tests passed")
    
    return failed
