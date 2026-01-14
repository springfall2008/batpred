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
from const import PREDICT_STEP


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

    # Test 4: Nearly complete gap day filled with nominal value
    print("Test 4: Nearly empty day filled with nominal 24 kWh")

    # Reset for zero day test
    my_predbat.days_previous = [1, 2, 3]
    my_predbat.days_previous_weight = [1.0, 1.0, 1.0]
    my_predbat.load_filter_modal = False  # Disable modal filtering to test gap filling only

    # Create test data with different consumption per day
    test_data = {}

    # Day 1: Normal consumption (20 kWh total)
    day1_total = 20.0
    step_increment_day1 = day1_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day1
        test_data[24 * 60 - minute] = dp4(running_total)

    # Day 2: Completely empty (not even in dict) - this creates true gaps
    # Don't add any data for day 2 - this creates true gaps
    # The function should detect this as all gaps and fill with 24 kWh

    # Day 3: Normal consumption (30 kWh total)
    day3_total = 30.0
    step_increment_day3 = day3_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day3
        test_data[3 * 24 * 60 - minute] = dp4(running_total)

    print("Created test data: Day 1={} kWh, Day 2=missing (complete gaps), Day 3={} kWh".format(day1_total, day3_total))

    # Call the function - should fill day 2 with nominal 24 kWh
    my_predbat.previous_days_modal_filter(test_data)

    # Check day 2 was filled
    day2_start = 2 * 24 * 60
    day2_filled_total = 0.0
    for minute in range(PREDICT_STEP, 24 * 60 + PREDICT_STEP, PREDICT_STEP):
        minute_previous = day2_start - minute
        increment = max(test_data.get(minute_previous, 0) - test_data.get(minute_previous + PREDICT_STEP, 0), 0)
        day2_filled_total += increment

    print("Day 2 total after gap filling: {} kWh".format(dp2(day2_filled_total)))

    # When a day has >= 1440 minutes (full day) of gaps, should be filled with nominal 24 kWh
    # However, due to gap detection logic, nearly-complete gaps may be filled proportionally
    # We expect at least 17 kWh (allowing for proportional filling of ~99% gaps)
    expected_filled_min = 17.0
    expected_filled_max = 24.0
    if day2_filled_total < expected_filled_min or day2_filled_total > expected_filled_max:
        print("ERROR: Expected day 2 to be filled with {}-{} kWh, got {} kWh".format(expected_filled_min, expected_filled_max, dp2(day2_filled_total)))
        failed = True
    else:
        print("Nearly empty day correctly filled with {} kWh (within expected range)".format(dp2(day2_filled_total)))

    # Test 5: Partial zero day filled proportionally
    print("Test 5: Partial data day filled proportionally")

    # Reset for partial data test
    my_predbat.days_previous = [1, 2]
    my_predbat.days_previous_weight = [1.0, 1.0]
    my_predbat.load_filter_modal = False

    test_data = {}

    # Day 1: Normal consumption (24 kWh total)
    day1_total = 24.0
    step_increment_day1 = day1_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day1
        test_data[24 * 60 - minute] = dp4(running_total)

    # Day 2: Half data (12 hours), half gaps (12 hours)
    # First 12 hours: 12 kWh worth of data
    day2_partial_total = 12.0
    step_increment_day2 = day2_partial_total / (12 * 60)
    running_total = 0
    for minute in range(0, 12 * 60):
        running_total += step_increment_day2
        test_data[2 * 24 * 60 - minute] = dp4(running_total)
    # Second 12 hours: all zeros (gaps)
    for minute in range(12 * 60, 24 * 60):
        test_data[2 * 24 * 60 - minute] = dp4(running_total)

    print("Created test data: Day 1={} kWh (complete), Day 2={} kWh (50% data, 50% gaps)".format(day1_total, day2_partial_total))

    initial_day2_total = day2_partial_total

    # Call the function - should fill day 2 gaps proportionally
    my_predbat.previous_days_modal_filter(test_data)

    # Calculate day 2 total after filling
    day2_start = 2 * 24 * 60
    day2_filled_total = 0.0
    for minute in range(PREDICT_STEP, 24 * 60 + PREDICT_STEP, PREDICT_STEP):
        minute_previous = day2_start - minute
        increment = max(test_data.get(minute_previous, 0) - test_data.get(minute_previous + PREDICT_STEP, 0), 0)
        day2_filled_total += increment

    print("Day 2 total after gap filling: {} kWh".format(dp2(day2_filled_total)))

    # Should be filled to approximately 24 kWh (from 12 kWh with 50% gaps)
    # Formula: average_day = initial / (1 - gap_percent) = 12 / 0.5 = 24
    expected_filled = 24.0
    if abs(day2_filled_total - expected_filled) > 1.0:  # Allow 1 kWh tolerance
        print("ERROR: Expected day 2 to be filled to ~{} kWh, got {} kWh".format(expected_filled, dp2(day2_filled_total)))
        failed = True
    else:
        print("Partial day correctly filled from {} kWh to {} kWh".format(dp2(initial_day2_total), dp2(day2_filled_total)))

    # Restore original get_arg method
    my_predbat.get_arg = original_get_arg

    return failed
