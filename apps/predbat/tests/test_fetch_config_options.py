# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import MockConfigProvider


def test_fetch_config_options(my_predbat):
    """
    Test the fetch_config_options function in isolation with mocked dependencies

    This test verifies that fetch_config_options correctly:
    - Retrieves configuration values via get_arg
    - Sets instance attributes based on configuration
    - Handles Axle control logic
    - Sets read-only modes correctly
    - Configures operation modes (Monitor, Control SOC, Control charge, Control charge & discharge)
    """
    print("**** Running fetch_config_options test ****")

    # Save original methods to restore at end
    original_get_arg = my_predbat.get_arg
    original_manual_times = my_predbat.manual_times
    original_manual_rates = my_predbat.manual_rates
    original_api_select_update = my_predbat.api_select_update
    original_get_state_wrapper = my_predbat.get_state_wrapper
    original_update_save_restore_list = my_predbat.update_save_restore_list
    original_expose_config = my_predbat.expose_config
    original_args = my_predbat.args

    # Create mock config provider
    mock_config = MockConfigProvider()
    # Set axle_session entity_id in config
    mock_config.config["axle_session"] = "binary_sensor.predbat_axle_event"

    # Mock get_arg to use config provider
    my_predbat.get_arg = mock_config.get_arg

    # Mock manual_times to return empty lists
    def mock_manual_times(key):
        return []

    # Mock manual_rates to return empty dicts
    def mock_manual_rates(key, default_rate=None):
        return {}

    # Mock api_select_update to return empty list
    def mock_api_select_update(key):
        return []

    # Mock get_state_wrapper to control Axle sensor state and previous status
    # Axle sensor state: "off" = no active event, "on" = active event
    axle_sensor_state = {"state": "off"}  # Mutable dict to allow updates in tests

    def mock_get_state_wrapper(entity_id, default=None, attribute=None):
        if entity_id == "predbat.status":
            return "Demand"
        elif entity_id == "binary_sensor.predbat_axle_event":
            return axle_sensor_state["state"]
        return default

    # Mock update_save_restore_list
    def mock_update_save_restore_list():
        pass

    # Mock expose_config
    def mock_expose_config(key, value):
        pass

    # Apply mocks
    my_predbat.manual_times = mock_manual_times
    my_predbat.manual_rates = mock_manual_rates
    my_predbat.api_select_update = mock_api_select_update
    my_predbat.get_state_wrapper = mock_get_state_wrapper
    my_predbat.update_save_restore_list = mock_update_save_restore_list
    my_predbat.expose_config = mock_expose_config

    # Mock the args dict for battery curves
    my_predbat.args = {
        "battery_charge_power_curve": {},
        "battery_charge_power_curve_default": {},
        "battery_discharge_power_curve": {},
        "battery_discharge_power_curve_default": {},
        "battery_temperature_charge_curve": {},
        "battery_temperature_discharge_curve": {},
    }

    # Test 1: Basic configuration loading
    print("\n*** Test 1: Basic configuration loading ***")

    # Axle sensor is "off" by default (no active event)
    my_predbat.fetch_config_options()

    # Verify basic configuration was loaded
    assert my_predbat.debug_enable == True, "debug_enable should be True"
    assert my_predbat.plan_debug == False, "plan_debug should be False"
    assert my_predbat.forecast_days == 2, "forecast_days should be 2 (48 hours / 24)"
    assert my_predbat.forecast_minutes == 48 * 60, "forecast_minutes should be 2880"
    assert my_predbat.num_cars == 2, "num_cars should be 2"
    assert my_predbat.calculate_plan_every == 10, "calculate_plan_every should be 10"

    # Verify days_previous configuration
    assert my_predbat.days_previous == [7, 14], "days_previous should be [7, 14]"
    assert my_predbat.days_previous_weight == [1.0, 0.5], "days_previous_weight should be [1.0, 0.5]"
    assert my_predbat.max_days_previous == 15, "max_days_previous should be 15 (max of days_previous + 1)"

    # Verify metric configuration
    assert my_predbat.metric_min_improvement == 0.1, "metric_min_improvement should be 0.1"
    assert my_predbat.metric_battery_cycle == 0.6, "metric_battery_cycle should be 0.6"
    assert my_predbat.rate_low_threshold == 0.75, "rate_low_threshold should be 0.75"
    assert my_predbat.rate_high_threshold == 1.25, "rate_high_threshold should be 1.25"

    # Verify battery configuration
    assert my_predbat.battery_capacity_nominal == 10.0, "battery_capacity_nominal should be 10.0"
    assert my_predbat.battery_loss == 0.95, "battery_loss should be 0.95 (1 - 0.05)"
    assert my_predbat.battery_loss_discharge == 0.95, "battery_loss_discharge should be 0.95"
    assert my_predbat.inverter_loss == 0.95, "inverter_loss should be 0.95"
    assert my_predbat.inverter_hybrid == False, "inverter_hybrid should be False"
    assert my_predbat.base_load == 0.1, "base_load should be 0.1 (100 / 1000)"

    # Verify SOC configuration
    assert my_predbat.best_soc_min == 0.0, "best_soc_min should be 0.0"
    assert my_predbat.best_soc_max == 10.0, "best_soc_max should be 10.0"
    assert my_predbat.best_soc_step == 0.25, "best_soc_step should be 0.25"

    # Verify car configuration
    assert my_predbat.car_charging_from_battery == False, "car_charging_from_battery should be False"
    assert my_predbat.car_charging_hold == True, "car_charging_hold should be True"
    assert my_predbat.car_charging_threshold == 1.0, "car_charging_threshold should be 1.0 (60 / 60)"

    # Verify read-only mode (should be False since axle_control is False)
    assert my_predbat.set_read_only == False, "set_read_only should be False"
    assert my_predbat.set_read_only_axle == False, "set_read_only_axle should be False"

    # Verify mode configuration (Control charge & discharge)
    assert my_predbat.calculate_best_charge == True, "calculate_best_charge should be True"
    assert my_predbat.calculate_best_export == True, "calculate_best_export should be True"
    assert my_predbat.set_charge_window == True, "set_charge_window should be True"
    assert my_predbat.set_export_window == True, "set_export_window should be True"
    assert my_predbat.set_soc_enable == True, "set_soc_enable should be True"

    # Verify iBoost configuration
    assert my_predbat.iboost_enable == False, "iboost_enable should be False"
    assert my_predbat.iboost_gas == 4.0, "iboost_gas should be 4.0"
    assert my_predbat.iboost_max_power == 0.05, "iboost_max_power should be 0.05 (3000 / 60000)"
    assert abs(my_predbat.iboost_min_power - 0.00833) < 0.001, "iboost_min_power should be ~0.00833 (500 / 60000)"

    print("✓ Basic configuration test passed")

    # Test 2: Axle control enabled with active event
    print("\n*** Test 2: Axle control enabled with active event ***")

    # Reset config and enable axle_control
    mock_config.config["axle_control"] = True
    mock_config.config["set_read_only"] = False
    # Set Axle sensor to "on" (active event)
    axle_sensor_state["state"] = "on"

    my_predbat.fetch_config_options()

    # Verify read-only mode was enabled by Axle
    assert my_predbat.set_read_only == True, "set_read_only should be True when Axle event is active"
    assert my_predbat.set_read_only_axle == True, "set_read_only_axle should be True when Axle event is active"

    print("✓ Axle control with active event test passed")

    # Test 3: Axle control enabled but no active event
    print("\n*** Test 3: Axle control enabled but no active event ***")

    # Set Axle sensor to "off" (no active event)
    axle_sensor_state["state"] = "off"

    my_predbat.fetch_config_options()

    # Verify read-only mode was NOT enabled
    assert my_predbat.set_read_only == False, "set_read_only should be False when no Axle event is active"
    assert my_predbat.set_read_only_axle == False, "set_read_only_axle should be False when no Axle event is active"

    print("✓ Axle control with no active event test passed")

    # Test 4: Manual read-only mode (overrides Axle)
    print("\n*** Test 4: Manual read-only mode (overrides Axle) ***")

    mock_config.config["set_read_only"] = True
    mock_config.config["axle_control"] = True
    # Set Axle sensor to "on" (should be ignored due to manual read-only)
    axle_sensor_state["state"] = "on"

    my_predbat.fetch_config_options()

    # Verify manual read-only takes precedence (Axle logic skipped)
    assert my_predbat.set_read_only == True, "set_read_only should be True from manual setting"
    assert my_predbat.set_read_only_axle == False, "set_read_only_axle should be False when manual read-only is set"

    print("✓ Manual read-only mode test passed")

    # Test 5: Monitor mode configuration
    print("\n*** Test 5: Monitor mode configuration ***")

    mock_config.config["mode"] = "Monitor"
    mock_config.config["set_read_only"] = False
    mock_config.config["axle_control"] = False
    axle_sensor_state["state"] = "off"

    my_predbat.fetch_config_options()

    # Verify Monitor mode settings
    assert my_predbat.predbat_mode == "Monitor", "predbat_mode should be Monitor"
    assert my_predbat.calculate_best_charge == False, "calculate_best_charge should be False in Monitor mode"
    assert my_predbat.calculate_best_export == False, "calculate_best_export should be False in Monitor mode"
    assert my_predbat.set_charge_window == False, "set_charge_window should be False in Monitor mode"
    assert my_predbat.set_export_window == False, "set_export_window should be False in Monitor mode"
    assert my_predbat.set_soc_enable == False, "set_soc_enable should be False in Monitor mode"

    print("✓ Monitor mode configuration test passed")

    # Test 6: Control SOC only mode
    print("\n*** Test 6: Control SOC only mode ***")

    mock_config.config["mode"] = "Control SOC only"

    my_predbat.fetch_config_options()

    assert my_predbat.predbat_mode == "Control SOC only", "predbat_mode should be Control SOC only"
    assert my_predbat.calculate_best_charge == True, "calculate_best_charge should be True"
    assert my_predbat.calculate_best_export == False, "calculate_best_export should be False"
    assert my_predbat.set_charge_window == False, "set_charge_window should be False"
    assert my_predbat.set_export_window == False, "set_export_window should be False"
    assert my_predbat.set_soc_enable == True, "set_soc_enable should be True"

    print("✓ Control SOC only mode test passed")

    # Test 7: Control charge mode
    print("\n*** Test 7: Control charge mode ***")

    mock_config.config["mode"] = "Control charge"

    my_predbat.fetch_config_options()

    assert my_predbat.predbat_mode == "Control charge", "predbat_mode should be Control charge"
    assert my_predbat.calculate_best_charge == True, "calculate_best_charge should be True"
    assert my_predbat.calculate_best_export == False, "calculate_best_export should be False"
    assert my_predbat.set_charge_window == True, "set_charge_window should be True"
    assert my_predbat.set_export_window == False, "set_export_window should be False"
    assert my_predbat.set_soc_enable == True, "set_soc_enable should be True"

    print("✓ Control charge mode test passed")

    # Test 8: Control charge & discharge mode
    print("\n*** Test 8: Control charge & discharge mode ***")

    mock_config.config["mode"] = "Control charge & discharge"

    my_predbat.fetch_config_options()

    assert my_predbat.predbat_mode == "Control charge & discharge", "predbat_mode should be Control charge & discharge"
    assert my_predbat.calculate_best_charge == True, "calculate_best_charge should be True"
    assert my_predbat.calculate_best_export == True, "calculate_best_export should be True"
    assert my_predbat.set_charge_window == True, "set_charge_window should be True"
    assert my_predbat.set_export_window == True, "set_export_window should be True"
    assert my_predbat.set_soc_enable == True, "set_soc_enable should be True"

    print("✓ Control charge & discharge mode test passed")

    # Test 9: Invalid mode defaults to Monitor
    print("\n*** Test 9: Invalid mode defaults to Monitor ***")

    mock_config.config["mode"] = "Invalid Mode"

    my_predbat.fetch_config_options()

    assert my_predbat.predbat_mode == "Monitor", "Invalid mode should default to Monitor"
    assert my_predbat.calculate_best_charge == False, "calculate_best_charge should be False for invalid mode"
    assert my_predbat.set_soc_enable == False, "set_soc_enable should be False for invalid mode"

    print("✓ Invalid mode defaults to Monitor test passed")

    # Test 10: Holiday mode override
    print("\n*** Test 10: Holiday mode configuration ***")

    mock_config.config["holiday_days_left"] = 5
    mock_config.config["days_previous"] = [7, 14]
    mock_config.config["mode"] = "Control charge & discharge"

    my_predbat.fetch_config_options()

    # Verify holiday mode overrides days_previous
    assert my_predbat.holiday_days_left == 5, "holiday_days_left should be 5"
    assert my_predbat.days_previous == [1], "days_previous should be [1] in holiday mode"
    assert my_predbat.max_days_previous == 2, "max_days_previous should be 2 (1 + 1) in holiday mode"

    print("✓ Holiday mode configuration test passed")

    # Test 11: Forecast calculations
    print("\n*** Test 11: Forecast calculations ***")

    mock_config.config["forecast_hours"] = 25
    mock_config.config["holiday_days_left"] = 0
    mock_config.config["days_previous"] = [7]

    my_predbat.fetch_config_options()

    # Verify forecast day rounding (25 + 23) / 24 = 2
    assert my_predbat.forecast_days == 2, "forecast_days should be 2 for 25 hours"
    assert my_predbat.forecast_minutes == 25 * 60, "forecast_minutes should be 1500"

    print("✓ Forecast calculations test passed")

    # Restore original methods
    my_predbat.get_arg = original_get_arg
    my_predbat.manual_times = original_manual_times
    my_predbat.manual_rates = original_manual_rates
    my_predbat.api_select_update = original_api_select_update
    my_predbat.get_state_wrapper = original_get_state_wrapper
    my_predbat.update_save_restore_list = original_update_save_restore_list
    my_predbat.expose_config = original_expose_config
    my_predbat.args = original_args

    print("\n**** All fetch_config_options tests passed! ****")
    return False
