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
from solax import SolaxAPI


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

        # Track method calls for testing
        self.self_consume_mode_called = False
        self.soc_target_control_mode_called = False
        self.last_mode_call = None

        # Mock timezone
        self.local_tz = timezone.utc
        self.mock_time = None  # For time mocking in tests

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
