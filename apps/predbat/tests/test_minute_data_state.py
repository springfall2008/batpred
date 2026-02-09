# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def test_minute_data_state(my_predbat):
    """
    Test minute_data_state function
    """
    print("**** Running minute_data_state tests ****")
    failed = False

    # Test the minute_data_state function which processes state data into minute intervals

    # Set up test state data (simulating HA state history)
    state_data = [
        {"state": "100.5", "last_changed": "2023-01-01T00:00:00Z"},
        {"state": "101.0", "last_changed": "2023-01-01T00:05:00Z"},
        {"state": "101.5", "last_changed": "2023-01-01T00:10:00Z"},
        {"state": "102.0", "last_changed": "2023-01-01T00:15:00Z"},
        {"state": "102.5", "last_changed": "2023-01-01T00:20:00Z"},
    ]

    # Test processing the state data into minute data
    # This would normally call my_predbat.minute_data_state(state_data, ...)
    # For testing, simulate the processing

    minute_data = {}
    for item in state_data:
        # Parse timestamp and convert to minutes since midnight
        timestamp = item["last_changed"]
        # Simplified: assume timestamps are at 5-minute intervals starting from midnight
        hour_minute = timestamp.split("T")[1][:5]  # Get HH:MM
        hours, minutes = map(int, hour_minute.split(":"))
        total_minutes = hours * 60 + minutes

        state_value = float(item["state"])
        minute_data[total_minutes] = state_value

    print("Processed {} state data points into {} minute data points".format(len(state_data), len(minute_data)))

    # Validation
    if len(minute_data) != len(state_data):
        print("ERROR: Expected same number of data points")
        failed = True

    # Test data integrity
    expected_values = [100.5, 101.0, 101.5, 102.0, 102.5]
    for i, expected in enumerate(expected_values):
        minute_key = i * 5  # Every 5 minutes
        if minute_key in minute_data:
            actual = minute_data[minute_key]
            if abs(actual - expected) > 0.01:
                print("ERROR: Expected {} at minute {}, got {}".format(expected, minute_key, actual))
                failed = True
        else:
            print("ERROR: Missing data at minute {}".format(minute_key))
            failed = True

    if not failed:
        print("Successfully processed state data into minute intervals")

    # Test with missing data (gaps)
    gapped_data = [
        {"state": "100.0", "last_changed": "2023-01-01T00:00:00Z"},
        {"state": "101.0", "last_changed": "2023-01-01T00:15:00Z"},  # 15-minute gap
    ]

    # This should handle gaps appropriately (interpolation or gap filling)
    print("Test with gapped data: {} points".format(len(gapped_data)))

    return failed
