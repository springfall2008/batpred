# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime
import pytz


def test_minute_data_import_export(my_predbat):
    """
    Test the minute_data_import_export function from the Fetch class
    Tests that it correctly handles arrays of entity IDs including:
    - Real entities with history data
    - Fixed values like '0' that should be skipped
    - Empty/None values
    """
    failed = False
    print("**** Testing minute_data_import_export function ****")

    # Set up test environment
    utc = pytz.UTC
    now = datetime(2024, 10, 4, 12, 0, 0, tzinfo=utc)
    my_predbat.now_utc = now
    my_predbat.max_days_previous = 2

    # Test 1: Array with real entities and fixed values
    print("Test 1: Mixed array with real entities and fixed value '0'")

    # Create test history data for valid entities
    entity1_history = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
        {"state": "5.0", "last_updated": "2024-10-04T10:30:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
        {"state": "15.0", "last_updated": "2024-10-04T11:30:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
    ]

    entity2_history = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
        {"state": "3.0", "last_updated": "2024-10-04T10:30:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
        {"state": "6.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
        {"state": "9.0", "last_updated": "2024-10-04T11:30:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
    ]

    # Set up mock history data storage
    mock_history_store = {
        "sensor.import_1": [entity1_history],
        "sensor.import_2": [entity2_history],
    }

    # Store original get_history_wrapper for restoration
    original_get_history = my_predbat.get_history_wrapper

    def mock_get_history_wrapper(entity_id, days):
        """Mock get_history_wrapper that returns data from mock_history_store"""
        return mock_history_store.get(entity_id, None)

    # Replace get_history_wrapper with mock
    my_predbat.get_history_wrapper = mock_get_history_wrapper

    # Test with array containing real entities and '0' fixed value
    entity_ids = ["sensor.import_1", "0", "sensor.import_2"]

    result = my_predbat.minute_data_import_export(now_utc=now, key=entity_ids[0], scale=1.0, required_unit="kWh")  # Pass first entity directly

    # Verify we got data from entity1
    if len(result) == 0:
        print("ERROR: Test 1 failed - no data returned for single entity")
        failed = True

    # Now test with the config approach using an array
    my_predbat.args["import_today_test"] = entity_ids

    result = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test", scale=1.0, required_unit="kWh")

    # Verify data was accumulated from both real entities
    if len(result) == 0:
        print("ERROR: Test 1 failed - no data returned for array of entities")
        failed = True
    else:
        # Check that data is accumulated (should be sum of both entities)
        # At minute 0 (most recent), we should have 15 + 9 = 24
        if 0 in result:
            expected_value = 24.0  # sum of last values from both entities
            if abs(result[0] - expected_value) > 0.01:
                print("ERROR: Test 1 failed - expected accumulated value {} at minute 0, got {}".format(expected_value, result[0]))
                failed = True
        else:
            print("ERROR: Test 1 failed - no data at minute 0")
            failed = True

    # Test 2: Array with only fixed values (should return empty dict)
    print("Test 2: Array with only fixed values")

    my_predbat.args["import_today_test2"] = ["0", "1", "5"]

    result = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test2", scale=1.0, required_unit="kWh")

    if len(result) != 0:
        print("ERROR: Test 2 failed - should return empty dict for fixed values only, got {} entries".format(len(result)))
        failed = True

    # Test 3: Array with None and empty strings
    print("Test 3: Array with None and empty strings")

    my_predbat.args["import_today_test3"] = [None, "", "sensor.import_1"]

    result = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test3", scale=1.0, required_unit="kWh")

    # Should only get data from sensor.import_1
    if len(result) == 0:
        print("ERROR: Test 3 failed - should have data from valid entity")
        failed = True

    # Test 4: Verify scaling works with accumulated data
    print("Test 4: Scaling with accumulated data")

    result_scaled = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test", scale=2.0, required_unit="kWh")

    result_unscaled = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test", scale=1.0, required_unit="kWh")

    if 0 in result_scaled and 0 in result_unscaled:
        expected_scaled = result_unscaled[0] * 2.0
        if abs(result_scaled[0] - expected_scaled) > 0.01:
            print("ERROR: Test 4 failed - scaling incorrect, expected {} got {}".format(expected_scaled, result_scaled[0]))
            failed = True
    else:
        print("ERROR: Test 4 failed - missing data for scaling test")
        failed = True

    # Test 5: Single entity passed directly (not from config)
    print("Test 5: Single entity passed directly")

    result = my_predbat.minute_data_import_export(now_utc=now, key="sensor.import_1", scale=1.0, required_unit="kWh")

    if len(result) == 0:
        print("ERROR: Test 5 failed - no data returned for direct entity")
        failed = True

    # Test 6: Non-existent entity (should log warning but not crash)
    print("Test 6: Non-existent entity")

    my_predbat.args["import_today_test6"] = ["sensor.nonexistent", "sensor.import_1"]

    result = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test6", scale=1.0, required_unit="kWh")

    # Should still get data from sensor.import_1
    if len(result) == 0:
        print("ERROR: Test 6 failed - should have data from valid entity despite non-existent entity")
        failed = True

    # Test 7: String converted to array
    print("Test 7: Single string entity (should be converted to array)")

    my_predbat.args["import_today_test7"] = "sensor.import_1"

    result = my_predbat.minute_data_import_export(now_utc=now, key="import_today_test7", scale=1.0, required_unit="kWh")

    if len(result) == 0:
        print("ERROR: Test 7 failed - no data returned for single string entity")
        failed = True

    # Restore original get_history_wrapper
    my_predbat.get_history_wrapper = original_get_history

    if failed:
        print("ERROR: test_minute_data_import_export FAILED")
    else:
        print("test_minute_data_import_export PASSED")

    assert not failed
    return failed
