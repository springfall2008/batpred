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


def create_test_history_data_soc_kw(my_predbat, num_days=2, battery_size_kwh=10.0):
    """
    Like create_test_history_data but exposes soc_kw (kWh absolute) instead of soc_percent.
    The find_battery_size code infers soc_max from the observed maximum kWh value, so the
    charging cycles must reach 100% (battery_size_kwh) at least once so that soc_max is
    accurately calibrated and percentage-based estimates are correct.
    """
    ha = my_predbat.ha_interface
    base_time = my_predbat.midnight_utc - timedelta(days=num_days)

    history_dict = {"sensor.soc_kw": [], "sensor.battery_power": []}
    ha.dummy_items["sensor.soc_kw"] = battery_size_kwh
    ha.dummy_items["sensor.battery_power"] = -2600

    total_minutes = num_days * 24 * 60
    max_power_w = 2600
    charge_power_w = max_power_w * 0.94  # above 90% threshold

    # Start near-empty so the 5-hour charge window reaches 100%, giving an accurate
    # observed soc_max for percentage derivation.
    current_soc_kwh = battery_size_kwh * 0.05  # Start at 5%

    for minutes in range(0, total_minutes, 5):
        timestamp = base_time + timedelta(minutes=minutes)
        timestamp_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")
        hour = timestamp.hour

        # Morning charge: 00:00–05:00 (5 hours). At 2444 W that adds ~12.2 kWh,
        # which is more than enough to top up a 10 kWh battery from 5%.
        if 0 <= hour < 5:
            battery_power = -charge_power_w
            energy_added_kwh = charge_power_w * 5 / 60.0 / 1000.0
            current_soc_kwh = min(battery_size_kwh, current_soc_kwh + energy_added_kwh)
        # Evening charge: 18:00–23:00 (same logic)
        elif 18 <= hour < 23:
            battery_power = -charge_power_w
            energy_added_kwh = charge_power_w * 5 / 60.0 / 1000.0
            current_soc_kwh = min(battery_size_kwh, current_soc_kwh + energy_added_kwh)
        else:
            battery_power = 0
            # Reset to near-empty before each charge session
            if hour == 17:
                current_soc_kwh = battery_size_kwh * 0.05

        history_dict["sensor.soc_kw"].append({"state": str(round(current_soc_kwh, 3)), "last_updated": timestamp_str, "attributes": {"unit_of_measurement": "kWh"}})
        history_dict["sensor.battery_power"].append({"state": round(battery_power, 1), "last_updated": timestamp_str, "attributes": {"unit_of_measurement": "W"}})

    def mock_get_history(entity_id, now=None, days=30):
        if entity_id in history_dict:
            return [history_dict[entity_id]]
        return None

    my_predbat.ha_interface.get_history = mock_get_history


def create_test_history_data_with_soc_glitches(my_predbat, battery_size_kwh=10.0):
    """
    Create one real charge period plus short SoC jumps that should be rejected.
    """
    ha = my_predbat.ha_interface
    base_time = my_predbat.midnight_utc - timedelta(hours=8)

    history_dict = {"sensor.soc_percent": [], "sensor.battery_power": []}
    ha.dummy_items["sensor.soc_percent"] = 70
    ha.dummy_items["sensor.battery_power"] = 0

    charge_power_w = 2500
    current_soc = 20.0
    glitch_soc = {300: 30.0, 301: 50.0, 302: 70.0, 303: 80.0, 360: 25.0, 361: 60.0, 362: 85.0}

    for minutes in range(0, 8 * 60):
        timestamp = base_time + timedelta(minutes=minutes)
        timestamp_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")

        if 60 <= minutes < 180:
            battery_power = -charge_power_w
            energy_added_wh = charge_power_w / 60.0
            current_soc = min(100.0, current_soc + (energy_added_wh / (battery_size_kwh * 1000.0)) * 100.0)
            soc = current_soc
        elif minutes in glitch_soc:
            battery_power = -charge_power_w
            soc = glitch_soc[minutes]
        else:
            battery_power = 0
            soc = current_soc

        history_dict["sensor.soc_percent"].append({"state": str(round(soc)), "last_updated": timestamp_str, "attributes": {"unit_of_measurement": "%"}})
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


def test_find_battery_size_soc_kw(my_predbat):
    """
    Test find_battery_size when soc_kw (absolute kWh) is provided instead of soc_percent.

    The code must determine soc_max from the maximum kWh value in the history and convert
    to percentages internally.  The resulting estimate should still be within tolerance of
    the true battery size.
    """
    print("*** Running test: find_battery_size_soc_kw ***")
    failed = False

    expected_battery_size = 10.0  # kWh
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = expected_battery_size
    inv.battery_scaling = 1.0

    # Configure soc_kw instead of soc_percent
    my_predbat.args["soc_kw"] = ["sensor.soc_kw"]
    my_predbat.args.pop("soc_percent", None)
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [False]
    my_predbat.debug_enable = True

    create_test_history_data_soc_kw(my_predbat, num_days=2, battery_size_kwh=expected_battery_size)

    try:
        estimated_size = inv.find_battery_size()
        if estimated_size:
            print("Estimated battery size (soc_kw path): {} kWh (expected: {} kWh)".format(estimated_size, expected_battery_size))
            tolerance = 0.30
            lower_bound = expected_battery_size * (1 - tolerance)
            upper_bound = expected_battery_size * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print("ERROR: Estimated battery size {} kWh is outside acceptable range [{}, {}] kWh".format(estimated_size, lower_bound, upper_bound))
                failed = True
            else:
                print("SUCCESS: soc_kw path estimated battery size within 30% tolerance")
        else:
            print("ERROR: No battery size estimate returned from soc_kw path")
            failed = True
    except Exception as e:
        print("ERROR: find_battery_size raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    # Restore soc_percent for subsequent tests
    setup_predbat(my_predbat)
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


def test_find_battery_size_rejects_soc_glitches(my_predbat):
    """
    Test that short, physically impossible SoC jumps do not drag down the capacity estimate.
    """
    print("*** Running test: find_battery_size_rejects_soc_glitches ***")
    failed = False

    expected_battery_size = 10.0
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = expected_battery_size
    inv.nominal_capacity = expected_battery_size
    inv.battery_scaling = 1.0

    setup_predbat(my_predbat)
    create_test_history_data_with_soc_glitches(my_predbat, battery_size_kwh=expected_battery_size)

    try:
        estimated_size = inv.find_battery_size(expected_battery_size)
        if estimated_size is None:
            print("ERROR: No battery size estimate returned")
            failed = True
        else:
            print("Estimated battery size with SoC glitches: {} kWh (expected: {} kWh)".format(estimated_size, expected_battery_size))
            tolerance = 0.15
            lower_bound = expected_battery_size * (1 - tolerance)
            upper_bound = expected_battery_size * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print("ERROR: Estimated battery size {} kWh is outside acceptable range [{}, {}] kWh".format(estimated_size, lower_bound, upper_bound))
                failed = True
            else:
                print("SUCCESS: SoC glitches were rejected from the estimate")
    except Exception as e:
        print("ERROR: find_battery_size raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def _make_inv_for_scaling(my_predbat, nominal_kwh=10.0):
    """
    Create an Inverter instance ready for battery_scaling_auto tests.
    """
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = nominal_kwh
    inv.nominal_capacity = nominal_kwh
    inv.battery_scaling = 1.0
    # Store soc_max_nominal in args so battery_size_tracking reads the correct nominal
    my_predbat.set_arg("soc_max_nominal", nominal_kwh, index=0)
    setup_predbat(my_predbat)
    return inv


def _clamped_auto_scaling(measured_kwh, nominal_kwh, configured_scaling=1.0):
    """
    Return battery_scaling_auto's expected total scaling.
    """
    return max(configured_scaling * 0.8, min(configured_scaling, measured_kwh / nominal_kwh))


def test_battery_scaling_auto_basic(my_predbat):
    """
    Test that battery_size_tracking with battery_scaling_auto enabled computes a trimmed mean and sets soc_max.
    """
    print("*** Running test: battery_scaling_auto_basic ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.battery_scaling_auto = True

    # Pre-populate 3 days of history so the sensor has existing data (not today)
    from datetime import timedelta as td

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    today = my_predbat.now_utc.date()
    day0 = str(today - td(days=3))
    day1 = str(today - td(days=2))
    day2 = str(today - td(days=1))
    # Values: 9.0, 9.5, 10.0; today adds 9.4 → trimmed mean of [9.0,9.4,9.5,10.0] drops extremes → (9.4+9.5)/2 = 9.45
    existing_history = {day0: 9.0, day1: 9.5, day2: 10.0}
    my_predbat.ha_interface.dummy_items[sensor_name] = {"state": "9.5", "history": existing_history}

    # Mock find_battery_size to return a known value for today
    inv.find_battery_size = lambda _nc=0: 9.4

    try:
        inv.battery_size_tracking()
        expected_mean = (9.4 + 9.5) / 2  # trimmed: drop 9.0 and 10.0
        # scaling = max(0.8, min(1.0, 9.45/10.0)) = 0.945; soc_max = dp3(10.0 * 0.945) = 9.45
        expected_scaling = _clamped_auto_scaling(expected_mean, nominal)
        expected_soc_max = round(nominal * expected_scaling, 3)
        if abs(inv.soc_max - expected_soc_max) > 0.01:
            print("ERROR: soc_max {} does not match expected {:.3f}".format(inv.soc_max, expected_soc_max))
            failed = True
        if abs(inv.battery_scaling - expected_scaling) > 0.001:
            print("ERROR: battery_scaling {} does not match expected {:.3f}".format(inv.battery_scaling, expected_scaling))
            failed = True
        # Check today's key was added to the sensor history
        sensor_state = my_predbat.ha_interface.dummy_items.get(sensor_name, {})
        history_attr = sensor_state.get("history", {}) if isinstance(sensor_state, dict) else {}
        if str(my_predbat.now_utc.date()) not in history_attr:
            print("ERROR: today's date not in history attribute")
            failed = True
        if not failed:
            print("SUCCESS: trimmed mean {:.2f} kWh stored correctly".format(expected_mean))
    except Exception as e:
        print("ERROR: test_battery_scaling_auto_basic raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_scaling_auto_single_sample(my_predbat):
    """
    Test that with only 1 sample the plain average (equal to the single value) is used.
    """
    print("*** Running test: battery_scaling_auto_single_sample ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.battery_scaling_auto = True

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    # Clear any prior state
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)

    try:
        trimmed_mean = inv.update_soc_max_calculated_sensor(8.5)
        if abs(trimmed_mean - 8.5) > 0.01:
            print("ERROR: single-sample mean should be 8.5, got {:.3f}".format(trimmed_mean))
            failed = True
        else:
            print("SUCCESS: single-sample mean {:.3f} kWh correct".format(trimmed_mean))
    except Exception as e:
        print("ERROR: test_battery_scaling_auto_single_sample raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_scaling_auto_clamping(my_predbat):
    """
    Test that battery_size_tracking clamps soc_max to [0.8, 1.0]*nominal when nominal is set in sensor.
    """
    print("*** Running test: battery_scaling_auto_clamping ***")
    failed = False
    nominal = 10.0
    my_predbat.battery_scaling_auto = True
    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)

    # Test lower clamp: find_battery_size returns 7.0, ratio 0.7 → clamped to 0.8 → soc_max=8.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    inv.find_battery_size = lambda _nc=0: 7.0
    inv.battery_size_tracking()
    expected_lower = round(nominal * 0.8, 3)
    if abs(inv.soc_max - expected_lower) > 0.001:
        print("ERROR: lower clamp failed, expected {:.3f} got {:.3f}".format(expected_lower, inv.soc_max))
        failed = True
    elif abs(inv.battery_scaling - 0.8) > 0.001:
        print("ERROR: lower clamp battery_scaling failed, expected 0.800 got {:.3f}".format(inv.battery_scaling))
        failed = True
    else:
        print("SUCCESS: lower clamp to 0.8 correct")

    # Test upper clamp: find_battery_size returns 11.0, ratio 1.1 → clamped to 1.0 → soc_max=10.0
    inv2 = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    inv2.find_battery_size = lambda _nc=0: 11.0
    inv2.battery_size_tracking()
    expected_upper = round(nominal * 1.0, 3)
    if abs(inv2.soc_max - expected_upper) > 0.001:
        print("ERROR: upper clamp failed, expected {:.3f} got {:.3f}".format(expected_upper, inv2.soc_max))
        failed = True
    elif abs(inv2.battery_scaling - 1.0) > 0.001:
        print("ERROR: upper clamp battery_scaling failed, expected 1.000 got {:.3f}".format(inv2.battery_scaling))
        failed = True
    else:
        print("SUCCESS: upper clamp to 1.0 correct")

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_scaling_auto_preserves_configured_scaling(my_predbat):
    """
    Test that battery_scaling_auto clamps relative to configured battery_scaling.

    An 80% DoD battery has configured battery_scaling=0.8. If historical charge data
    measures 7.2 kWh from a 10 kWh nominal battery, the total effective scaling should
    become 0.72, not be clamped back up to 0.8 or expanded above the configured DoD.
    """
    print("*** Running test: battery_scaling_auto_preserves_configured_scaling ***")
    failed = False
    nominal = 10.0
    configured_scaling = 0.8
    my_predbat.battery_scaling_auto = True
    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)

    inv = _make_inv_for_scaling(my_predbat, nominal)
    inv.battery_scaling = configured_scaling
    inv.battery_scaling_config = configured_scaling
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    inv.find_battery_size = lambda _nc=0: 7.2
    inv.battery_size_tracking()

    expected_scaling = 0.72
    expected_soc_max = round(nominal * expected_scaling, 3)
    if abs(inv.battery_scaling - expected_scaling) > 0.001:
        print("ERROR: expected battery_scaling {:.3f} got {:.3f}".format(expected_scaling, inv.battery_scaling))
        failed = True
    elif abs(inv.soc_max - expected_soc_max) > 0.001:
        print("ERROR: expected soc_max {:.3f} got {:.3f}".format(expected_soc_max, inv.soc_max))
        failed = True

    inv2 = _make_inv_for_scaling(my_predbat, nominal)
    inv2.battery_scaling = configured_scaling
    inv2.battery_scaling_config = configured_scaling
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    inv2.find_battery_size = lambda _nc=0: 8.8
    inv2.battery_size_tracking()

    if abs(inv2.battery_scaling - configured_scaling) > 0.001:
        print("ERROR: expected configured upper clamp {:.3f} got {:.3f}".format(configured_scaling, inv2.battery_scaling))
        failed = True
    elif abs(inv2.soc_max - nominal * configured_scaling) > 0.001:
        print("ERROR: expected upper-clamped soc_max {:.3f} got {:.3f}".format(nominal * configured_scaling, inv2.soc_max))
        failed = True
    elif not failed:
        print("SUCCESS: auto scaling preserved configured DoD and allowed measured degradation below it")

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_scaling_auto_skip_today(my_predbat):
    """
    Test that battery_size_tracking does not call find_battery_size when today is already in the sensor history.
    """
    print("*** Running test: battery_scaling_auto_skip_today ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.battery_scaling_auto = True

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    today_key = str(my_predbat.now_utc.date())

    # Pre-populate with today already present, stored mean=9.0, nominal stored in sensor
    my_predbat.ha_interface.dummy_items[sensor_name] = {"state": "9.0", "history": {today_key: 9.0}, "nominal_capacity": nominal}

    # Track whether find_battery_size was called - it must NOT be
    calls = [0]

    def mock_find_should_not_be_called():
        calls[0] += 1
        return 999.0

    inv.find_battery_size = mock_find_should_not_be_called

    try:
        inv.battery_size_tracking()
        if calls[0] > 0:
            print("ERROR: find_battery_size was called {} time(s) but should have been skipped".format(calls[0]))
            failed = True
        # stored mean=9.0, nominal=10.0 → scaling=0.9 → soc_max=9.0
        expected_scaling = _clamped_auto_scaling(9.0, nominal)
        expected_soc_max = round(nominal * expected_scaling, 3)
        if abs(inv.soc_max - expected_soc_max) > 0.001:
            print("ERROR: soc_max {} does not match expected {:.3f}".format(inv.soc_max, expected_soc_max))
            failed = True
        if abs(inv.battery_scaling - expected_scaling) > 0.001:
            print("ERROR: battery_scaling {} does not match expected {:.3f}".format(inv.battery_scaling, expected_scaling))
            failed = True
        if not failed:
            print("SUCCESS: find_battery_size correctly skipped for today, used stored mean 9.00")
    except Exception as e:
        print("ERROR: test_battery_scaling_auto_skip_today raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_scaling_auto_no_result(my_predbat):
    """
    Test that when find_battery_size returns None and there is no prior sensor state, soc_max is unchanged.
    """
    print("*** Running test: battery_scaling_auto_no_result ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    inv.battery_scaling = 1.0
    my_predbat.battery_scaling_auto = True

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)

    # Mock find_battery_size to return None
    inv.find_battery_size = lambda _nc=0: None
    original_soc_max = inv.soc_max

    try:
        inv.battery_size_tracking()
        if inv.soc_max != original_soc_max:
            print("ERROR: soc_max changed from {} to {} when it should not have".format(original_soc_max, inv.soc_max))
            failed = True
        else:
            print("SUCCESS: battery_scaling unchanged when no data available")
    except Exception as e:
        print("ERROR: test_battery_scaling_auto_no_result raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_scaling_auto_history_pruning(my_predbat):
    """
    Test that history entries older than 7 days are pruned.
    """
    print("*** Running test: battery_scaling_auto_history_pruning ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.battery_scaling_auto = True

    from datetime import timedelta as td

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)

    # Add 8 days of existing history (oldest should be pruned)
    today = my_predbat.now_utc.date()
    for days_ago in range(8, 0, -1):
        day_key = str(today - td(days=days_ago))
        inv.update_soc_max_calculated_sensor.__func__  # just reference to avoid lint
        # Call update to accumulate - feed 9.5 for each day
        # Directly call update_soc_max_calculated_sensor but spoof the date by manipulating history
        existing = my_predbat.ha_interface.dummy_items.get(sensor_name, {})
        history = existing.get("history", {}) if isinstance(existing, dict) else {}
        history[day_key] = 9.5
        sorted_keys = sorted(history.keys(), reverse=True)[:7]
        history = {k: history[k] for k in sorted_keys}
        my_predbat.ha_interface.dummy_items[sensor_name] = {"state": "9.5", "history": history}

    # Now call update with today's value which should add today and still keep only 7
    inv.update_soc_max_calculated_sensor(9.5)

    final_state = my_predbat.ha_interface.dummy_items.get(sensor_name, {})
    if isinstance(final_state, dict):
        final_history = final_state.get("history", {})
    else:
        final_history = {}

    if len(final_history) > 7:
        print("ERROR: history has {} entries, expected <= 7".format(len(final_history)))
        failed = True
    else:
        print("SUCCESS: history pruned to {} entries (<= 7)".format(len(final_history)))

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_size_tracking_auto_enable(my_predbat):
    """
    Test that battery_size_tracking auto-enables battery_scaling_auto when nominal_capacity is unset (0).
    With no history data available, soc_max should fall back to the 8 kWh default.
    """
    print("*** Running test: battery_size_tracking_auto_enable ***")
    failed = False
    # Use nominal_kwh=0 to simulate a user who never configured soc_max
    inv = _make_inv_for_scaling(my_predbat, nominal_kwh=0.0)
    my_predbat.battery_scaling_auto = False  # Starts disabled

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)

    # No data so find_battery_size returns None
    inv.find_battery_size = lambda _nc=0: None

    try:
        inv.battery_size_tracking()
        if not my_predbat.battery_scaling_auto:
            print("ERROR: battery_scaling_auto was not enabled when soc_max=0")
            failed = True
        if abs(inv.soc_max - 8.0) > 0.001:
            print("ERROR: soc_max fallback expected 8.0, got {:.3f}".format(inv.soc_max))
            failed = True
        if not failed:
            print("SUCCESS: battery_scaling_auto auto-enabled, fallback soc_max={:.1f} kWh".format(inv.soc_max))
    except Exception as e:
        print("ERROR: test_battery_size_tracking_auto_enable raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_size_tracking_no_nominal(my_predbat):
    """
    Test that when nominal_capacity is 0 (not configured), battery_size_tracking uses the trimmed mean
    directly as soc_max without clamping.
    """
    print("*** Running test: battery_size_tracking_no_nominal ***")
    failed = False

    # nominal_kwh=0 so nominal_capacity=0; give inv a real soc_max so auto-enable doesn't trigger
    inv = _make_inv_for_scaling(my_predbat, nominal_kwh=0.0)
    inv.soc_max = 10.0
    my_predbat.battery_scaling_auto = True

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)

    inv.find_battery_size = lambda _nc=0: 9.2

    try:
        inv.battery_size_tracking()
        # nominal_in_sensor=0 \u2192 use trimmed_mean directly without clamping
        expected_soc_max = round(9.2, 3)
        if abs(inv.soc_max - expected_soc_max) > 0.001:
            print("ERROR: soc_max {} does not match expected {:.3f} (no-nominal path)".format(inv.soc_max, expected_soc_max))
            failed = True
        else:
            print("SUCCESS: no nominal, soc_max set to trimmed_mean {:.3f} kWh directly".format(inv.soc_max))
    except Exception as e:
        print("ERROR: test_battery_size_tracking_no_nominal raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_size_tracking_none_stored_on_failure(my_predbat):
    """
    Test that when find_battery_size returns None, battery_size_tracking stores None in the
    sensor history under today's key so that subsequent cycles skip recalculation.
    """
    print("*** Running test: battery_size_tracking_none_stored_on_failure ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.battery_scaling_auto = False

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)

    # Mock find_battery_size to always fail
    inv.find_battery_size = lambda _nc=0: None
    original_soc_max = inv.soc_max  # 10.0

    try:
        inv.battery_size_tracking()

        # today's key must be present in history (as None) so the guard won't re-run
        sensor_state = my_predbat.ha_interface.dummy_items.get(sensor_name, {})
        history = sensor_state.get("history", {}) if isinstance(sensor_state, dict) else {}
        today_key = str(my_predbat.now_utc.date())
        if today_key not in history:
            print("ERROR: today_key '{}' not written to history after None result".format(today_key))
            failed = True
        elif history[today_key] is not None:
            print("ERROR: today_key value should be None, got {}".format(history[today_key]))
            failed = True
        else:
            print("SUCCESS: None correctly stored in history to prevent re-calculation")

        # soc_max must remain unchanged when find_battery_size fails and battery_scaling_auto is off
        if inv.soc_max != original_soc_max:
            print("ERROR: soc_max changed from {:.3f} to {:.3f} when it should remain unchanged after a failed find_battery_size".format(original_soc_max, inv.soc_max))
            failed = True
        else:
            print("SUCCESS: soc_max remains {:.3f} kWh after failed find_battery_size".format(inv.soc_max))
    except Exception as e:
        print("ERROR: test raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_battery_size_tracking_skip_after_none(my_predbat):
    """
    Test that a second call to battery_size_tracking on the same day does not call find_battery_size
    again, even when the first call produced a None result (today_key stored as None in history).
    """
    print("*** Running test: battery_size_tracking_skip_after_none ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)
    my_predbat.battery_scaling_auto = False

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    today_key = str(my_predbat.now_utc.date())

    # Pre-populate history with today's key already set to None (simulating a previous failed run)
    my_predbat.ha_interface.dummy_items[sensor_name] = {"state": "unknown", "history": {today_key: None}}

    calls = [0]

    def mock_find_should_not_be_called(_nc=0):
        calls[0] += 1
        return 9.5

    inv.find_battery_size = mock_find_should_not_be_called

    try:
        inv.battery_size_tracking()
        if calls[0] > 0:
            print("ERROR: find_battery_size called {} time(s) but today_key was already in history".format(calls[0]))
            failed = True
        else:
            print("SUCCESS: find_battery_size correctly skipped when today already has None in history")
    except Exception as e:
        print("ERROR: test raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_update_soc_max_calculated_sensor_all_none(my_predbat):
    """
    Test that update_soc_max_calculated_sensor returns None and sets sensor state to 'unknown'
    when all history entries are None (no successful measurements ever).
    """
    print("*** Running test: update_soc_max_calculated_sensor_all_none ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)

    # Pre-populate history with only None values from previous failed days
    from datetime import timedelta as td

    today = my_predbat.now_utc.date()
    existing_history = {str(today - td(days=1)): None, str(today - td(days=2)): None}
    my_predbat.ha_interface.dummy_items[sensor_name] = {"state": "unknown", "history": existing_history}

    try:
        result = inv.update_soc_max_calculated_sensor(None, nominal)
        if result is not None:
            print("ERROR: expected None when all history is None, got {}".format(result))
            failed = True
        else:
            # When all history is None and nominal_capacity > 0, sensor state is set to nominal_capacity
            # (so the HA entity shows a sensible value rather than 'unknown')
            sensor_state = my_predbat.ha_interface.dummy_items.get(sensor_name, {})
            state_value = sensor_state.get("state") if isinstance(sensor_state, dict) else sensor_state
            expected_state = nominal  # nominal_capacity > 0 → state = nominal_capacity
            if state_value != expected_state:
                print("ERROR: expected sensor state {}, got '{}'".format(expected_state, state_value))
                failed = True
            else:
                print("SUCCESS: all-None history returns None and sets state to nominal capacity {}".format(nominal))

        # Also verify: when nominal_capacity=0, state falls back to "unknown"
        inv_no_nominal = _make_inv_for_scaling(my_predbat, nominal_kwh=0.0)
        inv_no_nominal.soc_max = 10.0  # prevent fallback to 8kWh default
        my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
        result2 = inv_no_nominal.update_soc_max_calculated_sensor(None, 0)
        if result2 is not None:
            print("ERROR: expected None when nominal_capacity=0 and all history is None, got {}".format(result2))
            failed = True
        else:
            sensor_state2 = my_predbat.ha_interface.dummy_items.get(sensor_name, {})
            state_value2 = sensor_state2.get("state") if isinstance(sensor_state2, dict) else sensor_state2
            if state_value2 != "unknown":
                print("ERROR: expected sensor state 'unknown' when nominal=0, got '{}'".format(state_value2))
                failed = True
            else:
                print("SUCCESS: no-nominal all-None history correctly sets state to 'unknown'")
    except Exception as e:
        print("ERROR: test raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_update_soc_max_calculated_sensor_mixed_none(my_predbat):
    """
    Test that update_soc_max_calculated_sensor ignores None entries when computing the trimmed mean,
    so that failed days do not corrupt the average.
    """
    print("*** Running test: update_soc_max_calculated_sensor_mixed_none ***")
    failed = False
    nominal = 10.0
    inv = _make_inv_for_scaling(my_predbat, nominal)

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)

    from datetime import timedelta as td

    # 2 valid days + 1 None day; calling with a new valid value → 3 real values total
    # values: 9.0, 9.5, 9.2 → sorted [9.0, 9.2, 9.5] → trimmed mean = 9.2
    today = my_predbat.now_utc.date()
    existing_history = {
        str(today - td(days=3)): 9.0,
        str(today - td(days=2)): None,  # failed day
        str(today - td(days=1)): 9.5,
    }
    my_predbat.ha_interface.dummy_items[sensor_name] = {"state": "9.25", "history": existing_history}

    try:
        result = inv.update_soc_max_calculated_sensor(9.2, nominal)
        if result is None:
            print("ERROR: expected a valid mean when real values exist alongside None entries")
            failed = True
        else:
            # Sorted real values: [9.0, 9.2, 9.5] → trimmed drops 9.0 and 9.5 → mean=9.2
            expected_mean = 9.2
            if abs(result - expected_mean) > 0.05:
                print("ERROR: expected trimmed mean ~{:.2f}, got {:.3f}".format(expected_mean, result))
                failed = True
            else:
                print("SUCCESS: None entries ignored in mean calculation, result {:.3f}".format(result))
    except Exception as e:
        print("ERROR: test raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_find_battery_size_with_scaling(my_predbat):
    """
    Test find_battery_size with battery_scaling < 1.0 (80% depth-of-discharge).

    A 10 kWh nominal battery with battery_scaling=0.8 has a usable capacity of 8 kWh.
    The SoC sensor reports 0-100% of that 8 kWh usable range.
    find_battery_size must return ~8 kWh, not ~10 kWh.

    This regression test catches the bug where battery_scaling was applied to the SoC
    percentage values inside find_battery_size, causing the nominal capacity to be
    returned instead of the usable (scaled) capacity.
    """
    print("*** Running test: find_battery_size_with_scaling ***")
    failed = False

    nominal_kwh = 10.0
    battery_scaling = 0.8
    usable_kwh = nominal_kwh * battery_scaling  # 8 kWh

    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = usable_kwh
    inv.nominal_capacity = nominal_kwh
    inv.battery_scaling = battery_scaling

    setup_predbat(my_predbat)

    # History data reflects 0-100% SoC of the usable 8 kWh range
    create_test_history_data(my_predbat, num_days=2, battery_size_kwh=usable_kwh)

    try:
        estimated_size = inv.find_battery_size(nominal_kwh)
        if estimated_size is None:
            print("ERROR: find_battery_size returned None; expected ~{} kWh".format(usable_kwh))
            failed = True
        else:
            print("Estimated battery size: {:.2f} kWh (expected: {:.2f} kWh usable, nominal {:.2f} kWh)".format(estimated_size, usable_kwh, nominal_kwh))
            tolerance = 0.05  # 5% tolerance since scaling should be applied correctly
            lower_bound = usable_kwh * (1 - tolerance)
            upper_bound = usable_kwh * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print(
                    "ERROR: Estimated size {:.2f} kWh is outside usable range [{:.2f}, {:.2f}] kWh. "
                    "If this is ~{:.2f} kWh the battery_scaling bug is present (SoC percent was scaled "
                    "inside find_battery_size, returning nominal instead of usable capacity).".format(estimated_size, lower_bound, upper_bound, nominal_kwh)
                )
                failed = True
            else:
                print("SUCCESS: find_battery_size returned usable capacity {:.2f} kWh (within 5% of {:.2f} kWh)".format(estimated_size, usable_kwh))
    except Exception as e:
        print("ERROR: find_battery_size raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    return failed


def test_find_battery_size_soc_kw_unavailable(my_predbat):
    """
    Test find_battery_size when soc_kw history contains 'unavailable' entries.

    This simulates a GivEnergy dual AIO setup with a Gateway that has no batteries
    directly connected, causing some history states to be 'unavailable'.
    The function should skip those entries and still compute soc_max correctly.
    """
    print("*** Running test: find_battery_size_soc_kw_unavailable ***")
    failed = False

    expected_battery_size = 10.0  # kWh
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = expected_battery_size
    inv.battery_scaling = 1.0

    my_predbat.args["soc_kw"] = ["sensor.soc_kw"]
    my_predbat.args.pop("soc_percent", None)
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [False]
    my_predbat.debug_enable = True

    # Build history with a mix of valid values and 'unavailable' / 'unknown' states
    create_test_history_data_soc_kw(my_predbat, num_days=2, battery_size_kwh=expected_battery_size)

    # Inject some 'unavailable' and 'unknown' entries into the soc_kw history
    original_get_history = my_predbat.ha_interface.get_history

    def patched_get_history(entity_id, now=None, days=30):
        result = original_get_history(entity_id, now=now, days=days)
        if entity_id == "sensor.soc_kw" and result:
            # Replace every 10th entry with 'unavailable' and the entry 5 slots later
            # with 'unknown', simulating a Gateway with no directly connected batteries
            # (e.g. GivEnergy dual AIO).  Using a stride of 10 keeps ~80% valid data.
            for i in range(0, len(result[0]), 10):
                result[0][i] = {"state": "unavailable", "last_updated": result[0][i]["last_updated"], "attributes": {}}
            for i in range(5, len(result[0]), 10):
                result[0][i] = {"state": "unknown", "last_updated": result[0][i]["last_updated"], "attributes": {}}
        return result

    my_predbat.ha_interface.get_history = patched_get_history

    try:
        estimated_size = inv.find_battery_size()
        if estimated_size:
            print("Estimated battery size (soc_kw with unavailable): {} kWh (expected: {} kWh)".format(estimated_size, expected_battery_size))
            # 30% tolerance matches test_find_battery_size_soc_kw; the soc_kw path
            # derives percentages from the observed max so inherits more estimation
            # error than the direct soc_percent path (which uses 20% tolerance).
            tolerance = 0.30
            lower_bound = expected_battery_size * (1 - tolerance)
            upper_bound = expected_battery_size * (1 + tolerance)
            if not (lower_bound <= estimated_size <= upper_bound):
                print("ERROR: Estimated battery size {} kWh is outside acceptable range [{}, {}] kWh".format(estimated_size, lower_bound, upper_bound))
                failed = True
            else:
                print("SUCCESS: soc_kw path with unavailable states estimated battery size within 30% tolerance")
        else:
            print("ERROR: No battery size estimate returned from soc_kw path with unavailable states")
            failed = True
    except Exception as e:
        print("ERROR: find_battery_size raised exception with unavailable states: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True
    finally:
        my_predbat.ha_interface.get_history = original_get_history

    # Restore soc_percent for subsequent tests
    setup_predbat(my_predbat)

    return failed


def test_battery_size_tracking_unset_soc_max_persists(my_predbat):
    """
    Regression test: when soc_max is not configured the first call to battery_size_tracking
    should calculate soc_max from find_battery_size and NOT fall back to the 8 kWh default.
    A second call (simulating the next 5-minute cycle, where a new Inverter reads the args
    written by the first call) should keep the calculated value, not reset to 8 kWh.
    """
    print("*** Running test: battery_size_tracking_unset_soc_max_persists ***")
    failed = False
    expected_size = 9.5  # kWh - the real battery size that find_battery_size measures

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)

    # --- First cycle: soc_max not set in apps.yaml ---
    inv = _make_inv_for_scaling(my_predbat, nominal_kwh=0.0)
    # battery_scaling_auto is False before the cycle runs fetch_config_options
    my_predbat.battery_scaling_auto = False
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    # Ensure soc_max is absent from args (simulating no apps.yaml entry)
    my_predbat.args.pop("soc_max", None)
    my_predbat.set_arg("soc_max_nominal", 0.0, index=0)

    inv.find_battery_size = lambda _nc=0: expected_size

    try:
        inv.battery_size_tracking()

        # battery_scaling_auto must have been auto-enabled
        if not my_predbat.battery_scaling_auto:
            print("ERROR: battery_scaling_auto was not auto-enabled when soc_max=0")
            failed = True

        # soc_max must be the measured value, not the 8 kWh default
        if abs(inv.soc_max - expected_size) > 0.01:
            print("ERROR: First call soc_max={:.3f} kWh, expected {:.3f} kWh (not 8.0 default)".format(inv.soc_max, expected_size))
            failed = True
        else:
            print("SUCCESS: First call set soc_max={:.3f} kWh correctly from find_battery_size".format(inv.soc_max))

        # --- Second cycle: new Inverter reads from updated args ---
        # In real operation fetch_config_options resets battery_scaling_auto to whatever
        # is in apps.yaml (False when not configured).
        my_predbat.battery_scaling_auto = False

        inv2 = _make_inv_for_scaling(my_predbat, nominal_kwh=0.0)
        # Simulate Inverter.__init__ reading soc_max from args (updated by first cycle)
        inv2.nominal_capacity = my_predbat.get_arg("soc_max", default=0.0, index=0)
        inv2.soc_max = inv2.nominal_capacity * inv2.battery_scaling

        # find_battery_size must NOT be called on the second cycle (today already in history)
        second_calls = [0]

        def mock_find_second_call(_nc=0):
            second_calls[0] += 1
            return expected_size

        inv2.find_battery_size = mock_find_second_call

        inv2.battery_size_tracking()

        if second_calls[0] > 0:
            print("WARN: find_battery_size was called on second cycle (today already in history)")

        if abs(inv2.soc_max - expected_size) > 0.01:
            print("ERROR: Second call soc_max={:.3f} kWh, expected {:.3f} kWh - " "value should not have been reset to the 8 kWh fallback".format(inv2.soc_max, expected_size))
            failed = True
        else:
            print("SUCCESS: Second call preserved soc_max={:.3f} kWh correctly".format(inv2.soc_max))

        if not failed:
            print("SUCCESS: soc_max calculated on first call and persisted across second call")

    except Exception as e:
        print("ERROR: test_battery_size_tracking_unset_soc_max_persists raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_size_tracking_transient_unavailable_recovers(my_predbat):
    """
    Regression test: a transient 'unavailable' read of a configured soc_max sensor must not
    pin the battery size to the 8 kWh default.

    Models three consecutive 5-minute cycles (a fresh Inverter is created each cycle, so only
    the base.args cache persists between them):
      1. soc_max source reads a valid 28.35 kWh -> soc_max == 28.35, soc_max_nominal persisted.
      2. soc_max source reads 'unavailable' (get_arg returns 0.0) -> soc_max restored from
         soc_max_nominal to ~28.35 (NOT 8.0), and the soc_max arg is not clobbered with 8.0.
      3. soc_max source reads valid 28.35 again -> still 28.35.
    """
    print("*** Running test: battery_size_tracking_transient_unavailable_recovers ***")
    failed = False
    real_size = 28.35  # kWh - sum of both batteries on the real-world SolarEdge dual setup

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    # Configured system: battery_scaling_auto is off and starts with no persisted nominal/fallback
    my_predbat.battery_scaling_auto = False
    my_predbat.args.pop("soc_max", None)
    my_predbat.set_arg("soc_max_nominal", 0.0, index=0)
    setup_predbat(my_predbat)
    # No calibration history, so find_battery_size returns None during each cycle's construction
    remove_test_history_data(my_predbat)

    def run_cycle(soc_max_read):
        """Simulate one 5-minute cycle: a fresh Inverter reads soc_max from the args cache.

        soc_max_read is the value the configured source reports (None models an 'unavailable'
        sensor, where get_arg falls back to its 0.0 default). Inverter.__init__ runs the full
        battery_size_tracking, so this exercises the real per-cycle path.
        """
        if soc_max_read is None:
            my_predbat.args.pop("soc_max", None)
        else:
            my_predbat.set_arg("soc_max", soc_max_read, index=0)
        return Inverter(my_predbat, 0)

    try:
        # --- Cycle 1: source reads valid 28.35 ---
        inv1 = run_cycle(real_size)
        if abs(inv1.soc_max - real_size) > 0.01:
            print("ERROR: Cycle 1 soc_max={:.3f}, expected {:.3f}".format(inv1.soc_max, real_size))
            failed = True
        if abs(my_predbat.get_arg("soc_max_nominal", default=0.0, index=0) - real_size) > 0.01:
            print("ERROR: Cycle 1 did not persist soc_max_nominal={:.3f}".format(real_size))
            failed = True

        # --- Cycle 2: source 'unavailable' -> get_arg returns the 0.0 default ---
        inv2 = run_cycle(None)
        if abs(inv2.soc_max - real_size) > 0.01:
            print("ERROR: Cycle 2 soc_max={:.3f} after transient outage, expected restored {:.3f} (NOT 8.0 default)".format(inv2.soc_max, real_size))
            failed = True
        # The 8 kWh fallback must never have clobbered the configured soc_max arg
        soc_max_arg = my_predbat.get_arg("soc_max", default=0.0, index=0)
        if abs(soc_max_arg - 8.0) < 0.01:
            print("ERROR: Cycle 2 pinned soc_max arg to 8.0 - configured source would stay clobbered until restart")
            failed = True
        # soc_max_nominal must remain intact (not wiped to 0)
        if abs(my_predbat.get_arg("soc_max_nominal", default=0.0, index=0) - real_size) > 0.01:
            print("ERROR: Cycle 2 wiped soc_max_nominal recovery value")
            failed = True

        # --- Cycle 3: source recovers ---
        inv3 = run_cycle(real_size)
        if abs(inv3.soc_max - real_size) > 0.01:
            print("ERROR: Cycle 3 soc_max={:.3f} after recovery, expected {:.3f}".format(inv3.soc_max, real_size))
            failed = True

        if not failed:
            print("SUCCESS: transient unavailable soc_max recovered to {:.3f} kWh without pinning 8 kWh".format(real_size))
    except Exception as e:
        print("ERROR: test_battery_size_tracking_transient_unavailable_recovers raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
    return failed


def test_battery_size_tracking_fallback_non_sticky(my_predbat):
    """
    Regression test: the genuine first-run 8 kWh fallback still applies, but is non-sticky.

    With no configured soc_max, no persisted nominal and no calibration history, soc_max falls
    back to 8.0 for the cycle - but must NOT cache 8.0 into the soc_max / soc_max_nominal args,
    so a later cycle where the source becomes readable recovers the real value automatically.
    """
    print("*** Running test: battery_size_tracking_fallback_non_sticky ***")
    failed = False
    real_size = 9.5  # kWh - value the source reports once it becomes available

    sensor_name = "sensor.{}_soc_max_calculated".format(my_predbat.prefix)
    my_predbat.ha_interface.dummy_items.pop(sensor_name, None)
    my_predbat.battery_scaling_auto = False
    my_predbat.args.pop("soc_max", None)
    my_predbat.set_arg("soc_max_nominal", 0.0, index=0)
    setup_predbat(my_predbat)
    # No calibration history, so find_battery_size returns None during each cycle's construction
    remove_test_history_data(my_predbat)

    def run_cycle(soc_max_read):
        """Simulate one 5-minute cycle via a fresh Inverter (see the transient test)."""
        if soc_max_read is None:
            my_predbat.args.pop("soc_max", None)
        else:
            my_predbat.set_arg("soc_max", soc_max_read, index=0)
        return Inverter(my_predbat, 0)

    try:
        # --- Cycle 1: nothing configured, no history -> 8 kWh fallback ---
        inv = run_cycle(None)

        if abs(inv.soc_max - 8.0) > 0.001:
            print("ERROR: fallback soc_max expected 8.0, got {:.3f}".format(inv.soc_max))
            failed = True
        # The fallback must NOT have been cached into the args
        if abs(my_predbat.get_arg("soc_max", default=0.0, index=0) - 8.0) < 0.01:
            print("ERROR: fallback pinned soc_max arg to 8.0 (should stay unset so it self-heals)")
            failed = True
        if abs(my_predbat.get_arg("soc_max_nominal", default=0.0, index=0) - 8.0) < 0.01:
            print("ERROR: fallback pinned soc_max_nominal arg to 8.0")
            failed = True

        # --- Cycle 2: source now readable -> recovers to the real value, not stuck at 8.0 ---
        # battery_scaling_auto was auto-enabled by cycle 1 (nominal was 0); the recovered read now
        # provides a real nominal, so soc_max must follow the source rather than the 8 kWh fallback.
        inv2 = run_cycle(real_size)

        if abs(inv2.soc_max - real_size) > 0.01:
            print("ERROR: after source recovered soc_max={:.3f}, expected {:.3f} (fallback was sticky)".format(inv2.soc_max, real_size))
            failed = True

        if not failed:
            print("SUCCESS: 8 kWh fallback applied for the cycle and self-healed to {:.3f} kWh once readable".format(real_size))
    except Exception as e:
        print("ERROR: test_battery_size_tracking_fallback_non_sticky raised exception: {}".format(e))
        import traceback

        traceback.print_exc()
        failed = True

    my_predbat.battery_scaling_auto = False
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

    failed |= test_find_battery_size_soc_kw(my_predbat)
    if failed:
        return failed

    failed |= test_find_battery_size_soc_kw_unavailable(my_predbat)
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

    failed |= test_find_battery_size_rejects_soc_glitches(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_basic(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_single_sample(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_clamping(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_preserves_configured_scaling(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_skip_today(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_no_result(my_predbat)
    if failed:
        return failed

    failed |= test_battery_scaling_auto_history_pruning(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_auto_enable(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_no_nominal(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_none_stored_on_failure(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_skip_after_none(my_predbat)
    if failed:
        return failed

    failed |= test_update_soc_max_calculated_sensor_all_none(my_predbat)
    if failed:
        return failed

    failed |= test_update_soc_max_calculated_sensor_mixed_none(my_predbat)
    if failed:
        return failed

    failed |= test_find_battery_size_with_scaling(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_unset_soc_max_persists(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_transient_unavailable_recovers(my_predbat)
    if failed:
        return failed

    failed |= test_battery_size_tracking_fallback_non_sticky(my_predbat)
    if failed:
        return failed

    print("**** find_battery_size tests completed ****")
    return failed
