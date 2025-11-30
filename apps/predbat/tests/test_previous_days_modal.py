# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import dp2, dp4
from config import PREDICT_STEP

def add_incrementing_sensor_total(data):
    max_entry = max(data.keys()) if data else 0
    total = 0
    for minute in range(0, max_entry, PREDICT_STEP):
        increment = max(data.get(minute, 0) - data.get(minute + PREDICT_STEP, 0), 0)
        total += increment
    return total

def test_previous_days_modal_filter(my_predbat):
    """
    Test the previous_days_modal_filter function
    """
    print("**** Running previous_days_modal_filter tests ****")
    failed = False

    # Set up test environment
    my_predbat.load_minutes_age = 7  # 7 days of data
    my_predbat.days_previous = [1, 2]  # Test with 3 days
    my_predbat.days_previous_weight = [1.0, 1.0]  # Equal weighting
    number_of_days = 2
    my_predbat.load_filter_modal = True  # Enable modal filtering
    my_predbat.car_charging_hold = False
    my_predbat.car_charging_energy = None
    my_predbat.iboost_energy_subtract = False
    my_predbat.iboost_energy_today = None
    my_predbat.base_load = 0.0

    # Mock the get_arg method
    original_get_arg = my_predbat.get_arg

    def mock_get_arg(key, default=None):
        if key == "load_filter_threshold":
            return 30
        return original_get_arg(key, default)

    my_predbat.get_arg = mock_get_arg

    # Test 1: Empty data set - should be filled with gap filling logic
    print("Test 1: Empty data set")
    test_data = {}

    # Call the function
    my_predbat.previous_days_modal_filter(test_data)
    data_length = max(test_data.keys()) if test_data else 0
    data_length_days = data_length / (24 * 60)
    print("Data length after processing: {} minutes ({} days)".format(data_length, data_length_days))

    # After gap filling, data should have been populated for the empty gaps
    # Check that data has been filled for at least some minutes
    total_filled_data = dp2(add_incrementing_sensor_total(test_data))
    expected_total_per_day = 24.0  # 24 kWh per day as default when no data

    print("Total filled data: {} kWh".format(dp2(total_filled_data)))

    # With 2 days and complete gaps, should use 24kWh default for each day
    if total_filled_data != expected_total_per_day * number_of_days:
        print("ERROR: Expected gap filling to add approximately {} kWh, got {} kWh".format(expected_total_per_day * number_of_days, total_filled_data))
        for minute in range(0, data_length, 30):
            print("  Minute {}: {}".format(minute, test_data.get(minute, 0)))
        failed = True

    # Test 2: Data with gaps - should be filled to create 1 kWh per hour pattern
    print("Test 2: Data with some gaps")
    test_data = {}

    # Create partial data for 1 day (1 kWh per hour = incrementing total)
    # PREDICT_STEP is 5 minutes, so 12 steps per hour
    # Each step should increment by 1/12 kWh to get 1 kWh per hour
    step_increment = 1.0 / 60
    running_total = 0

    # Fill first half of day with proper incremental data
    for minute in range(0, 12 * 60):  # 12 hours worth
        running_total += step_increment
        test_data[24 * 60 - minute - 1] = dp4(running_total)  # Backwards indexing as used in function
    for minute in range(12 * 60, 24 * 60):  # remainder hours worth
        test_data[24 * 60 - minute - 1] = dp4(running_total)  # Backwards indexing as used in function

    # Leave second half empty to test gap filling

    # Set up days_previous for this test
    my_predbat.days_previous = [1]  # Only test with 1 day
    my_predbat.days_previous_weight = [1.0]

    initial_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Initial partial data sum: {} kWh".format(dp2(initial_data_sum)))

    # Call the function
    my_predbat.previous_days_modal_filter(test_data)

    final_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Final data sum after gap filling: {} kWh".format(dp2(final_data_sum)))

    # Should now have approximately 24 kWh total (1 kWh per hour for 24 hours)
    expected_final_total = 24.0
    if final_data_sum != expected_final_total:
        print("ERROR: Expected final total around {} kWh, got {} kWh".format(expected_final_total, final_data_sum))
        for minute in range(0, 24 * 60, 15):
            print("  Minute {}: {}".format(minute, test_data.get(minute, 0)))
        failed = True
    else:
        print("Gap filling successful: filled from {} kWh to {} kWh".format(dp2(initial_data_sum), dp2(final_data_sum)))

    # Test 2.1 alternate gaps in one hour intervals
    print("Test 2.1: Data with some gaps")
    test_data = {}

    running_total = 0
    step_increment = 1.0 / 60
    for minute in range(0, 24 * 60):
        hour = int(minute / 60)
        if hour % 2 == 0:
            running_total += step_increment  # Increment only in alternate hours
        test_data[24 * 60 - minute - 1] = dp4(running_total)  # Backwards indexing as used in function

    initial_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Initial partial data sum: {} kWh".format(dp2(initial_data_sum)))

    # Call the function
    my_predbat.previous_days_modal_filter(test_data)

    final_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Final data sum after gap filling: {} kWh".format(dp2(final_data_sum)))

    # Should now have approximately 24 kWh total (1 kWh per hour for 24 hours)
    expected_final_total = 24.0
    if abs(final_data_sum - expected_final_total) > 1.0:  # Allow 1 kWh tolerance
        print("ERROR: Expected final total around {} kWh, got {} kWh".format(expected_final_total, final_data_sum))
        for minute in range(0, 24 * 60, 15):
            print("  Minute {}: {}".format(minute, test_data.get(minute, 0)))
        failed = True
    else:
        print("Gap filling successful: filled from {} kWh to {} kWh".format(dp2(initial_data_sum), dp2(final_data_sum)))

    # Test 3: Modal filtering - remove lowest consumption day
    print("Test 3: Modal filtering removes lowest day")

    # Reset for modal filter test
    my_predbat.days_previous = [1, 2, 3]
    my_predbat.days_previous_weight = [1.0, 1.0, 1.0]
    original_days_count = len(my_predbat.days_previous)

    # Create test data with different consumption per day
    test_data = {}

    # Day 1: Low consumption (10 kWh total)
    day1_total = 10.0
    step_increment_day1 = day1_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day1
        test_data[24 * 60 - minute] = running_total

    # Day 2: Medium consumption (20 kWh total)
    day2_total = 20.0
    step_increment_day2 = day2_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day2
        test_data[2 * 24 * 60 - minute] = running_total

    # Day 3: High consumption (30 kWh total)
    day3_total = 30.0
    step_increment_day3 = day3_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day3
        test_data[3 * 24 * 60 - minute] = running_total

    print("Created test data with day totals: {} kWh, {} kWh, {} kWh".format(day1_total, day2_total, day3_total))

    # Call the function - should remove day 1 (lowest consumption)
    my_predbat.previous_days_modal_filter(test_data)
    print(my_predbat.days_previous)

    # Check that one day was removed
    final_days_count = len(my_predbat.days_previous)
    if final_days_count != original_days_count - 1:
        print("ERROR: Expected modal filter to remove 1 day, had {} days, now have {} days".format(original_days_count, final_days_count))
        failed = True
    elif 1 in my_predbat.days_previous:
        print("ERROR: Expected modal filter to remove day 1 (lowest consumption), but it's still present")
        failed = True
    else:
        print("Modal filter correctly removed lowest consumption day")

    # Restore original get_arg method
    my_predbat.get_arg = original_get_arg

    return failed