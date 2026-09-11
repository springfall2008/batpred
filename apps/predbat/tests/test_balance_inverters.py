# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from inverter import Inverter


def dummy_sleep(seconds):
    """
    Dummy sleep function
    """
    pass


def run_balance_inverters_tests(my_predbat):
    """
    Test the balance_inverters function with various scenarios
    """
    print("**** Running balance_inverters tests ****\n")
    failed = False

    # Test 1: Two inverters, one low SoC during discharge
    print("Test 1: Two inverters, one low SoC during discharge")
    failed |= test_balance_discharge_low_soc(my_predbat)
    if failed:
        return failed

    # Test 2: Two inverters, one high SoC during charge
    print("Test 2: Two inverters, one high SoC during charge")
    failed |= test_balance_charge_high_soc(my_predbat)
    if failed:
        return failed

    # Test 3: Cross charging during discharge
    print("Test 3.1: Cross charging during discharge")
    failed |= test_balance_cross_charging1(my_predbat)
    if failed:
        return failed
    # Test 3.2: Cross charging during discharge
    print("Test 3.2: Cross charging during discharge")
    failed |= test_balance_cross_charging2(my_predbat)
    if failed:
        return failed

    # Test 4: Cross discharging during charge
    print("Test 4: Cross discharging during charge")
    failed |= test_balance_cross_discharging(my_predbat)
    if failed:
        return failed

    # Test 5: Already balanced inverters
    print("Test 5: Already balanced inverters")
    failed |= test_balance_already_balanced(my_predbat)
    if failed:
        return failed

    # Test 6: Below threshold difference
    print("Test 6: Below threshold difference")
    failed |= test_balance_below_threshold(my_predbat)
    if failed:
        return failed

    # Test 7: One inverter at reserve
    print("Test 7: One inverter at reserve during discharge")
    failed |= test_balance_at_reserve(my_predbat)
    if failed:
        return failed

    # Test 8: Balance disabled
    print("Test 8: Balance disabled")
    failed |= test_balance_disabled(my_predbat)
    if failed:
        return failed

    # Test 9: Inverter in calibration mode
    print("Test 9: Inverter in calibration mode")
    failed |= test_balance_calibration_mode(my_predbat)
    if failed:
        return failed

    # Test 10: Insufficient power to balance
    print("Test 10: Insufficient power to balance")
    failed |= test_balance_insufficient_power(my_predbat)
    if failed:
        return failed

    # Test 11: Reset balance
    print("Test 11: Reset balance when already balanced, charge")
    failed |= test_balance_reset_balanced_charge(my_predbat)
    if failed:
        return failed

    # Test 11: Reset balance
    print("Test 12: Reset balance when already balanced, discharge")
    failed |= test_balance_reset_balanced_discharge(my_predbat)
    if failed:
        return failed

    return failed


def setup_two_inverters(my_predbat, soc1=50, soc2=50, reserve1=4, reserve2=4, battery_power1=0, battery_power2=0, pv_power1=0, pv_power2=0, load_power1=500, load_power2=500, charge_rate1=2600, charge_rate2=2600, discharge_rate1=2600, discharge_rate2=2600):
    """
    Helper function to set up two inverters with specified parameters
    """

    ha = my_predbat.ha_interface
    ha.service_store_enable = True
    ha.service_store = []

    # Set up inverter 0
    ha.dummy_items["sensor.soc_percent"] = soc1
    ha.dummy_items["sensor.battery_power"] = battery_power1
    ha.dummy_items["sensor.pv_power"] = pv_power1
    ha.dummy_items["sensor.load_power"] = load_power1
    ha.dummy_items["number.charge_rate"] = charge_rate1
    ha.dummy_items["number.discharge_rate"] = discharge_rate1
    ha.dummy_items["number.reserve"] = reserve1

    # Set up inverter 1
    ha.dummy_items["sensor.soc_percent_2"] = soc2
    ha.dummy_items["sensor.battery_power_2"] = battery_power2
    ha.dummy_items["sensor.pv_power_2"] = pv_power2
    ha.dummy_items["sensor.load_power_2"] = load_power2
    ha.dummy_items["number.charge_rate_2"] = charge_rate2
    ha.dummy_items["number.discharge_rate_2"] = discharge_rate2
    ha.dummy_items["number.reserve_2"] = reserve2

    # Common settings
    ha.dummy_items["sensor.soc_max"] = 10.0
    ha.dummy_items["sensor.soc_max_2"] = 10.0
    ha.dummy_items["sensor.battery_rate_max"] = 2600

    # Charge/discharge window settings for inverter 0
    ha.dummy_items["select.charge_start_time"] = "01:00:00"
    ha.dummy_items["select.charge_end_time"] = "05:00:00"
    ha.dummy_items["select.discharge_start_time"] = "00:00:00"
    ha.dummy_items["select.discharge_end_time"] = "00:00:00"
    ha.dummy_items["switch.scheduled_charge_enable"] = "off"
    ha.dummy_items["switch.scheduled_discharge_enable"] = "off"

    # Charge/discharge window settings for inverter 1
    ha.dummy_items["select.charge_start_time_2"] = "01:00:00"
    ha.dummy_items["select.charge_end_time_2"] = "05:00:00"
    ha.dummy_items["select.discharge_start_time_2"] = "00:00:00"
    ha.dummy_items["select.discharge_end_time_2"] = "00:00:00"
    ha.dummy_items["switch.scheduled_charge_enable_2"] = "off"
    ha.dummy_items["switch.scheduled_discharge_enable_2"] = "off"

    ha.dummy_items["sensor.grid_power"] = 0
    ha.dummy_items["sensor.grid_power_2"] = 0

    # Configure args for my_predbat BEFORE creating inverters
    my_predbat.args["num_inverters"] = 2
    my_predbat.num_inverters = 2
    # Set up entity args for both inverters (lists)
    my_predbat.args["soc_percent"] = ["sensor.soc_percent", "sensor.soc_percent_2"]
    my_predbat.args["battery_power"] = ["sensor.battery_power", "sensor.battery_power_2"]
    my_predbat.args["pv_power"] = ["sensor.pv_power", "sensor.pv_power_2"]
    my_predbat.args["load_power"] = ["sensor.load_power", "sensor.load_power_2"]
    my_predbat.args["charge_rate"] = ["number.charge_rate", "number.charge_rate_2"]
    my_predbat.args["discharge_rate"] = ["number.discharge_rate", "number.discharge_rate_2"]
    my_predbat.args["grid_power"] = ["sensor.grid_power", "sensor.grid_power_2"]
    my_predbat.args["reserve"] = ["number.reserve", "number.reserve_2"]
    my_predbat.args["soc_max"] = ["sensor.soc_max", "sensor.soc_max_2"]
    my_predbat.args["battery_rate_max"] = ["sensor.battery_rate_max", "sensor.battery_rate_max"]
    my_predbat.args["charge_start_time"] = ["select.charge_start_time", "select.charge_start_time_2"]
    my_predbat.args["charge_end_time"] = ["select.charge_end_time", "select.charge_end_time_2"]
    my_predbat.args["discharge_start_time"] = ["select.discharge_start_time", "select.discharge_start_time_2"]
    my_predbat.args["discharge_end_time"] = ["select.discharge_end_time", "select.discharge_end_time_2"]
    my_predbat.args["scheduled_charge_enable"] = ["switch.scheduled_charge_enable", "switch.scheduled_charge_enable_2"]
    my_predbat.args["scheduled_discharge_enable"] = ["switch.scheduled_discharge_enable", "switch.scheduled_discharge_enable_2"]
    my_predbat.args["battery_scaling"] = [1.0, 1.0]
    my_predbat.args["battery_temperature"] = [20.0, 20.0]
    my_predbat.args["inverter_limit"] = [5000, 5000]
    my_predbat.args["inverter_battery_rate_min"] = [100, 100]
    my_predbat.args["inverter_limit_charge"] = [2600, 2600]
    my_predbat.args["inverter_limit_discharge"] = [2600, 2600]
    if "pause_mode" in my_predbat.args:
        # Remove arg
        del my_predbat.args["pause_mode"]
    if "inverter_time" in my_predbat.args:
        # Remove arg
        del my_predbat.args["inverter_time"]
    if "soc_kw" in my_predbat.args:
        # Remove arg
        del my_predbat.args["soc_kw"]
    if "battery_power_invert" in my_predbat.args:
        # Remove arg
        del my_predbat.args["battery_power_invert"]

    # Create inverters
    my_predbat.inverters = []
    for id in range(2):
        inverter = Inverter(my_predbat, id, quiet=True)
        inverter.sleep = dummy_sleep
        inverter.update_status(my_predbat.minutes_now, quiet=True)
        my_predbat.inverters.append(inverter)


def test_balance_discharge_low_soc(my_predbat):
    """
    Test balancing when one inverter has low SoC during discharge
    Expected: Low SoC inverter discharge rate should be set to 0
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=1000,  # Discharging (positive = discharging)
        battery_power2=1000,
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.args["balance_inverters_discharge"] = True
    my_predbat.args["balance_inverters_charge"] = False
    my_predbat.args["balance_inverters_crosscharge"] = False
    my_predbat.args["balance_inverters_threshold_charge"] = 5
    my_predbat.args["balance_inverters_threshold_discharge"] = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    print(services)
    ha.service_store_enable = False

    # Should have set discharge rate to 0 for inverter 0
    discharge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate" and kwargs.get("value") == 0:
            discharge_set = True
            break

    if not discharge_set:
        print("ERROR: Expected inverter 0 discharge rate to be set to 0")
        return True

    print("✓ Test passed: Low SoC inverter discharge rate set to 0")
    return False


def test_balance_charge_high_soc(my_predbat):
    """
    Test balancing when one inverter has high SoC during charge
    Expected: High SoC inverter charge rate should be set to 0
    """
    # Setup: Inverter 0 at 80%, Inverter 1 at 60%, both charging
    setup_two_inverters(
        my_predbat,
        soc1=80,
        soc2=60,
        battery_power1=-1000,  # Charging (negative = charging)
        battery_power2=-1000,
        charge_rate1=2600,
        charge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should have set charge rate to 0 for inverter 0
    charge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate" and kwargs.get("value") == 0:
            charge_set = True
            break

    if not charge_set:
        print("ERROR: Expected inverter 0 charge rate to be set to 0")
        return True

    print("✓ Test passed: High SoC inverter charge rate set to 0")
    return False


def test_balance_cross_charging1(my_predbat):
    """
    Test balancing when one inverter is cross-charging during discharge
    Expected: Charging inverter should have charge rate set to 0
    """
    # Setup: Inverter 0 discharging, Inverter 1 charging during discharge
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=40,
        battery_power1=1000,  # Discharging
        battery_power2=-100,  # Charging (cross-charge)
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should turn off charge for inverter 2
    charge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate_2" and kwargs.get("value") == 0:
            charge_set = True
            break

    if not charge_set:
        print("ERROR: Expected inverter 2 charge rate to be set to 0 to stop cross-charging")
        return True

    print("✓ Test passed: Cross-charging inverter charge rate set to 0")
    return False


def test_balance_cross_charging2(my_predbat):
    """
    Test balancing when one inverter is cross-charging during discharge
    Expected: Charging inverter should have charge rate set to 0
    """
    # Setup: Inverter 0 discharging, Inverter 1 charging during discharge
    setup_two_inverters(
        my_predbat,
        soc1=40,
        soc2=50,
        battery_power1=1000,  # Discharging
        battery_power2=-100,  # Charging (cross-charge)
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should turn off discharge for inverter 0
    charge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate" and kwargs.get("value") == 0:
            charge_set = True
            break

    if not charge_set:
        print("ERROR: Expected inverter 1 discharge rate to be set to 0 to stop cross-charging")
        return True

    print("✓ Test passed: Cross-charging inverter discharge rate set to 0")
    return False


def test_balance_cross_discharging(my_predbat):
    """
    Test balancing when one inverter is cross-discharging during charge
    Expected: Discharging inverter should have discharge rate set to 0
    """
    # Setup: Inverter 0 charging, Inverter 1 discharging during charge
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=60,
        battery_power1=-1000,  # Charging
        battery_power2=100,  # Discharging (cross-discharge)
        charge_rate1=2600,
        charge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should have set discharge rate to 0 for inverter 1
    discharge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate_2" and kwargs.get("value") == 0:
            discharge_set = True
            break

    if not discharge_set:
        print("ERROR: Expected inverter 1 discharge rate to be set to 0 to stop cross-discharging")
        return True

    print("✓ Test passed: Cross-discharging inverter discharge rate set to 0")
    return False


def test_balance_already_balanced(my_predbat):
    """
    Test when inverters are already balanced
    Expected: No rate adjustments should be made
    """
    # Setup: Both inverters at 50%
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=50,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")) and kwargs.get("value") == 0:
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when inverters are balanced")
        return True

    print("✓ Test passed: No adjustments made when already balanced")
    return False


def test_balance_reset_balanced_charge(my_predbat):
    """
    Test when inverters are already balanced, resetting charge/discharge rates
    Expected: Rate goes back to full
    """
    # Setup: Both inverters at 50%
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=50,
        battery_power1=0,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    my_predbat.inverters[0].charge_rate_now = 0  # Simulate previously balanced state
    ha.dummy_items["number.charge_rate"] = 0

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    charge_reset = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate" and kwargs.get("value") == 2600:
            charge_reset = True
            break

    if not charge_reset:
        print("ERROR: Expected rate adjustments when inverters are balanced")
        return True

    print("✓ Test passed: Rate adjustments made when already balanced")
    return False


def test_balance_reset_balanced_discharge(my_predbat):
    """
    Test when inverters are already balanced, resetting charge/discharge rates
    Expected: Rate goes back to full
    """
    # Setup: Both inverters at 50%
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=50,
        battery_power1=1000,
        battery_power2=0,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    my_predbat.inverters[1].discharge_rate_now = 0  # Simulate previously balanced state
    ha.dummy_items["number.discharge_rate_2"] = 0

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    discharge_reset = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate_2" and kwargs.get("value") == 2600:
            discharge_reset = True
            break

    if not discharge_reset:
        print("ERROR: Expected rate adjustments when inverters are balanced")
        return True

    print("✓ Test passed: Rate adjustments made when already balanced")
    return False


def test_balance_below_threshold(my_predbat):
    """
    Test when SoC difference is below threshold
    Expected: No rate adjustments should be made
    """
    # Setup: Inverter 0 at 48%, Inverter 1 at 52% (4% difference, below 5% threshold)
    setup_two_inverters(
        my_predbat,
        soc1=48,
        soc2=52,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance with 5% threshold
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")) and kwargs.get("value") == 0:
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when difference below threshold")
        return True

    print("✓ Test passed: No adjustments made when below threshold")
    return False


def test_balance_at_reserve(my_predbat):
    """
    Test when one inverter is at reserve level
    Expected: Inverter at reserve should not be forced to discharge
    """
    # Setup: Inverter 0 at reserve (4%), Inverter 1 at 50%, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=4,
        soc2=50,
        reserve1=4,
        reserve2=4,
        battery_power1=100,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should have set discharge rate to 0 for inverter 0 (at reserve)
    discharge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate" and kwargs.get("value") == 0:
            discharge_set = True
            break

    if not discharge_set:
        print("ERROR: Expected inverter 0 at reserve to have discharge stopped")
        return True

    print("✓ Test passed: Inverter at reserve has discharge stopped")
    return False


def test_balance_disabled(my_predbat):
    """
    Test when balancing is disabled
    Expected: No rate adjustments should be made
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Disable all balance features
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")):
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when balancing disabled")
        return True

    print("✓ Test passed: No adjustments made when balancing disabled")
    return False


def test_balance_calibration_mode(my_predbat):
    """
    Test when one inverter is in calibration mode
    Expected: Balance function should return early without adjustments
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Set inverter 0 in calibration mode
    my_predbat.inverters[0].in_calibration = True

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")):
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when in calibration mode")
        return True

    # Reset calibration mode
    my_predbat.inverters[0].in_calibration = False

    print("✓ Test passed: No adjustments made when in calibration mode")
    return False


def test_balance_insufficient_power(my_predbat):
    """
    Test when inverter power is below threshold for balancing
    Expected: No rate adjustments when power too low
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%, but low power (< 50W)
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=20,  # Below 50W threshold
        battery_power2=20,
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.args["balance_inverters_discharge"] = True
    my_predbat.args["balance_inverters_charge"] = False
    my_predbat.args["balance_inverters_crosscharge"] = False
    my_predbat.args["balance_inverters_threshold_charge"] = 5
    my_predbat.args["balance_inverters_threshold_discharge"] = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    my_predbat.balance_inverters(test_mode=True)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted rates due to insufficient power
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")) and kwargs.get("value") == 0:
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when power below threshold")
        return True

    print("✓ Test passed: No adjustments made when power insufficient")
    return False
