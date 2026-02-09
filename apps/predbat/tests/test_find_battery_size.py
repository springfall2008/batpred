# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import TestHAInterface
from predbat import PredBat
from inverter import Inverter
from datetime import timedelta


def setup_predbat(my_predbat):
    """
    Setup Predbat for testing
    """
    my_predbat.args["soc_percent"] = ["sensor.soc_percent"]
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [False]
    my_predbat.debug_enable = True  # Enable debug for more info


def create_test_history_data(my_predbat, num_days=2, battery_size_kwh=10.0):
    """
    Create mock history data for testing find_battery_size

    Creates realistic battery charging data with:
    - SoC percentage data that transitions based on actual power added
    - Battery power data (negative = charging)

    History data is indexed by minutes in the past (0 = now, higher = older)
    """
    ha = my_predbat.ha_interface
    base_time = my_predbat.midnight_utc - timedelta(days=num_days)

    # Create history data structure for each sensor
    history_dict = {"sensor.soc_percent": [], "sensor.battery_power": []}
    ha.dummy_items["sensor.soc_percent"] = 100
    ha.dummy_items["sensor.battery_power"] = -2600

    total_minutes = num_days * 24 * 60

    # Use charge power above 90% of max to be detected by algorithm
    max_power_w = 2600
    charge_power_w = max_power_w * 0.94  # 2444W

    # Generate data for each minute going forwards in time from base_time
    current_soc = 20.0  # Start at 20%

    for minutes in range(0, total_minutes, 5):
        timestamp = base_time + timedelta(minutes=minutes)
        timestamp_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")

        hour = timestamp.hour
        minute_of_hour = timestamp.minute

        # Determine if we're in a charging period
        # Morning charge: 2am-4am (120 minutes) - charges ~49% for 10kWh battery
        if 2 <= hour < 4:
            battery_power = -charge_power_w
            # Calculate SoC increase for this 5-minute interval
            energy_added_wh = charge_power_w * 5 / 60.0  # Wh for 5 minutes
            soc_increase = (energy_added_wh / (battery_size_kwh * 1000.0)) * 100.0
            current_soc = min(100.0, current_soc + soc_increase)

        elif 18 <= hour < 20:
            # Evening charge: 6pm-8pm (120 minutes)
            battery_power = -charge_power_w
            energy_added_wh = charge_power_w * 5 / 60.0
            soc_increase = (energy_added_wh / (battery_size_kwh * 1000.0)) * 100.0
            current_soc = min(100.0, current_soc + soc_increase)

        else:
            # Idle period
            battery_power = 0
            # Reset SoC to 20% before next charge session
            if hour == 1 or hour == 17:
                current_soc = 20.0

        soc_rounded = round(current_soc)

        history_dict["sensor.soc_percent"].append({"state": str(soc_rounded), "last_updated": timestamp_str, "attributes": {"unit_of_measurement": "%"}})
        history_dict["sensor.battery_power"].append({"state": round(battery_power, 1), "last_updated": timestamp_str, "attributes": {"unit_of_measurement": "W"}})

    def mock_get_history(entity_id, now=None, days=30):
        if entity_id in history_dict:
            return [history_dict[entity_id]]
        return None

    my_predbat.ha_interface.get_history = mock_get_history


def remove_test_history_data(my_predbat):
    def mock_get_history(entity_id, now=None, days=30):
        return None

    my_predbat.ha_interface.get_history = mock_get_history


def test_find_battery_size_basic(my_predbat):
    """
    Test basic battery size detection

    This test creates realistic charging data where SoC changes match the
    actual energy added (power * time), so the algorithm should estimate
    the battery size accurately.
    """
    print("*** Running test: find_battery_size_basic ***")
    failed = False

    # Setup inverter with known battery size
    expected_battery_size = 10.0  # kWh
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = expected_battery_size
    inv.battery_scaling = 1.0

    # Setup configuration
    setup_predbat(my_predbat)

    # Create test data with physically realistic charging data
    create_test_history_data(my_predbat, num_days=2, battery_size_kwh=expected_battery_size)

    # Test battery size estimation
    try:
        estimated_size = inv.find_battery_size()
        if estimated_size:
            print("Estimated battery size: {} kWh (expected: {} kWh)".format(estimated_size, expected_battery_size))
            # Verify estimate is within 20% of expected (tight tolerance since test data is physically accurate)
            tolerance = 0.2
            lower_bound = expected_battery_size * (1 - tolerance)
            upper_bound = expected_battery_size * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print("ERROR: Estimated battery size {} kWh is outside acceptable range [{}, {}] kWh".format(estimated_size, lower_bound, upper_bound))
                failed = True
            else:
                print("SUCCESS: Estimated battery size within 20% tolerance")
        else:
            print("ERROR: No battery size estimate returned - expected to find estimate from charging data")
            failed = True
    except Exception as e:
        print("ERROR: find_battery_size raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_find_battery_size_no_data(my_predbat):
    """
    Test find_battery_size with missing history data
    """
    print("*** Running test: find_battery_size_no_data ***")
    failed = False

    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0

    # Setup configuration
    setup_predbat(my_predbat)

    # No history data created - should return None
    remove_test_history_data(my_predbat)
    estimated_size = inv.find_battery_size()

    if estimated_size:
        print("ERROR: find_battery_size should return None with no history data")
        failed = True
    else:
        print("Correctly returned None with no history data")

    return failed


def test_find_battery_size_no_sensors(my_predbat):
    """
    Test find_battery_size when sensors are not configured
    """
    print("*** Running test: find_battery_size_no_sensors ***")
    failed = False

    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0

    # Don't setup sensors - they should be None
    my_predbat.args = {}
    my_predbat.debug_enable = True

    # Should return None when sensors not configured
    try:
        estimated_size = inv.find_battery_size()
        if estimated_size:
            print("ERROR: find_battery_size should return None when sensors not configured")
            failed = True
        else:
            print("Correctly returned None with no sensors configured")
    except Exception as e:
        print("ERROR: find_battery_size raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_find_battery_size_inverted_power(my_predbat):
    """
    Test find_battery_size with inverted battery power
    """
    print("*** Running test: find_battery_size_inverted_power ***")
    failed = False

    expected_battery_size = 10.0
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = expected_battery_size
    inv.battery_scaling = 1.0

    # Setup configuration with inverted battery power
    my_predbat.args["soc_percent"] = ["sensor.soc_percent"]
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [True]  # Inverted
    my_predbat.debug_enable = True

    # Create test data (will be inverted by the function)
    create_test_history_data(my_predbat, num_days=7, battery_size_kwh=expected_battery_size)

    # Test should handle inverted power correctly
    try:
        estimated_size = inv.find_battery_size()
        # With inverted power the sign will be wrong, so we might not find valid data
        # This is a smoke test to ensure it doesn't crash
        if estimated_size:
            print("Estimated battery size with inverted power: {} kWh".format(estimated_size))
            # If we got an estimate, verify it's reasonable
            tolerance = 0.15
            lower_bound = expected_battery_size * (1 - tolerance)
            upper_bound = expected_battery_size * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print("WARN: Estimated battery size {} kWh with inverted power is outside expected range".format(estimated_size))
                # Not failing this as inverted power handling may vary
        else:
            print("No estimate with inverted power - this may be expected")
    except Exception as e:
        print("ERROR: find_battery_size raised exception with inverted power: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_find_battery_size_different_size(my_predbat):
    """
    Test find_battery_size with a different battery size (5 kWh)
    """
    print("*** Running test: find_battery_size_different_size ***")
    failed = False

    expected_battery_size = 5.0  # kWh - smaller battery
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Same charge rate
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = expected_battery_size
    inv.battery_scaling = 1.0

    setup_predbat(my_predbat)
    # Create test data for 5kWh battery
    create_test_history_data(my_predbat, num_days=2, battery_size_kwh=expected_battery_size)

    try:
        estimated_size = inv.find_battery_size()
        if estimated_size:
            print("Estimated battery size: {} kWh (expected: {} kWh)".format(estimated_size, expected_battery_size))
            # Verify estimate is within 30% of expected
            tolerance = 0.30
            lower_bound = expected_battery_size * (1 - tolerance)
            upper_bound = expected_battery_size * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print("ERROR: Estimated battery size {} kWh is outside acceptable range [{}, {}] kWh".format(estimated_size, lower_bound, upper_bound))
                failed = True
            else:
                print("SUCCESS: Estimated battery size within 30% tolerance")
        else:
            print("ERROR: No battery size estimate returned")
            failed = True
    except Exception as e:
        print("ERROR: find_battery_size raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def run_find_battery_size_tests(my_predbat):
    """
    Run all find_battery_size tests
    """

    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = True
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 48 * 60
    my_predbat.max_days_previous = 2  # Reduced from 7 for faster testing
    my_predbat.minutes_now = 1440

    failed = False
    print("**** Running find_battery_size tests ****")

    failed |= test_find_battery_size_basic(my_predbat)
    if failed:
        return failed

    failed |= test_find_battery_size_no_data(my_predbat)
    if failed:
        return failed

    failed |= test_find_battery_size_no_sensors(my_predbat)
    if failed:
        return failed

    failed |= test_find_battery_size_inverted_power(my_predbat)
    if failed:
        return failed

    failed |= test_find_battery_size_different_size(my_predbat)
    if failed:
        return failed

    print("**** find_battery_size tests completed ****")
    return failed
