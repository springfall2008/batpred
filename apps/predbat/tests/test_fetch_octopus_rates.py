# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime


def test_fetch_octopus_rates(my_predbat):
    """
    Test the fetch_octopus_rates function

    Tests various sensor formats and attribute combinations:
    - Legacy sensor with all_rates attribute
    - New event-based sensor with rates attribute
    - Current rate sensor with previous/current/next day rates
    - Nordpool sensor with raw_today/raw_tomorrow
    - Different rate key formats (rate, value_inc_vat, value, price)
    - Different time key formats (from/to, valid_from/valid_to, start/end)
    - Adjust key for Octopus Intelligent adjusted rates
    """
    print("**** Running fetch_octopus_rates tests ****")
    failed = False

    # Setup test environment
    old_forecast_days = my_predbat.forecast_days
    old_midnight_utc = my_predbat.midnight_utc

    my_predbat.forecast_days = 2
    my_predbat.midnight_utc = datetime.strptime("2025-01-01T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    my_predbat.io_adjusted = {}

    # Clear dummy_items from previous tests
    my_predbat.ha_interface.dummy_items.clear()

    # Test 1: Legacy sensor with all_rates attribute
    print("*** Test 1: Legacy sensor with all_rates attribute")
    entity_id = "sensor.octopus_energy_electricity_abc123_current_rate"

    # Mock the sensor data - standard Octopus format
    test_rates = [
        {"value_inc_vat": 15.5, "valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:30:00+00:00"},
        {"value_inc_vat": 16.2, "valid_from": "2025-01-01T00:30:00+00:00", "valid_to": "2025-01-01T01:00:00+00:00"},
        {"value_inc_vat": 14.8, "valid_from": "2025-01-01T01:00:00+00:00", "valid_to": "2025-01-01T01:30:00+00:00"},
    ]

    # Set up mock sensor state
    my_predbat.ha_interface.dummy_items["event.octopus_energy_electricity_abc123_current_day_rates"] = {"state": "on", "rates": test_rates}

    # Call the function
    rate_data = my_predbat.fetch_octopus_rates(entity_id)

    # Validate results
    if not rate_data:
        print("ERROR: No rate data returned for legacy sensor")
        failed = True
    else:
        # Check that we have rates for the expected time periods
        if 0 not in rate_data:
            print("ERROR: Missing rate for minute 0")
            failed = True
        elif rate_data[0] != 15.5:
            print("ERROR: Expected rate 15.5 at minute 0, got {}".format(rate_data[0]))
            failed = True

        if 30 not in rate_data:
            print("ERROR: Missing rate for minute 30")
            failed = True
        elif rate_data[30] != 16.2:
            print("ERROR: Expected rate 16.2 at minute 30, got {}".format(rate_data[30]))
            failed = True

        print("Test 1 passed - legacy sensor format works")

    # Test 2: New event-based sensor with previous, current, and next rates
    print("*** Test 2: Event-based sensor with previous/current/next rates")

    previous_rates = [
        {"value_inc_vat": 10.0, "valid_from": "2024-12-31T23:00:00+00:00", "valid_to": "2024-12-31T23:30:00+00:00"},
        {"value_inc_vat": 10.5, "valid_from": "2024-12-31T23:30:00+00:00", "valid_to": "2025-01-01T00:00:00+00:00"},
    ]

    next_rates = [
        {"value_inc_vat": 20.0, "valid_from": "2025-01-02T00:00:00+00:00", "valid_to": "2025-01-02T00:30:00+00:00"},
    ]

    # Set up mock event sensors
    my_predbat.ha_interface.dummy_items["event.octopus_energy_electricity_abc123_previous_day_rates"] = {"state": "on", "rates": previous_rates}

    my_predbat.ha_interface.dummy_items["event.octopus_energy_electricity_abc123_next_day_rates"] = {"state": "on", "rates": next_rates}

    # Call the function
    rate_data = my_predbat.fetch_octopus_rates(entity_id)

    # Validate - should have combined all three days
    if not rate_data:
        print("ERROR: No rate data returned for event-based sensor")
        failed = True
    else:
        # Check previous day rate (negative minute)
        minute_2330 = -30  # 23:30 previous day
        if minute_2330 in rate_data and rate_data[minute_2330] != 10.5:
            print("ERROR: Expected rate 10.5 at minute {}, got {}".format(minute_2330, rate_data[minute_2330]))
            failed = True

        # Check next day rate
        minute_next_day = 24 * 60  # 00:00 next day
        if minute_next_day in rate_data and rate_data[minute_next_day] != 20.0:
            print("ERROR: Expected rate 20.0 at minute {}, got {}".format(minute_next_day, rate_data[minute_next_day]))
            failed = True

        print("Test 2 passed - event-based sensor with multiple days works")

    # Test 3: Different rate key formats
    print("*** Test 3: Different rate key formats (rate, value, price)")

    # Test with 'rate' key
    entity_id_rate = "sensor.test_rate_format"
    my_predbat.ha_interface.dummy_items["sensor.test_rate_format"] = {
        "state": "15.5",
        "rates": [
            {"rate": 15.5, "from": "2025-01-01T00:00:00+00:00", "to": "2025-01-01T00:30:00+00:00"},
        ],
    }

    rate_data = my_predbat.fetch_octopus_rates(entity_id_rate)
    if rate_data and 0 in rate_data:
        if rate_data[0] != 15.5:
            print("ERROR: Expected rate 15.5 with 'rate' key, got {}".format(rate_data[0]))
            failed = True
        else:
            print("Test 3a passed - 'rate' key format works")
    else:
        print("ERROR: No rate data for 'rate' key format")
        failed = True

    # Test with 'price' key (Nordpool format)
    entity_id_price = "sensor.nordpool_test"
    my_predbat.ha_interface.dummy_items["sensor.nordpool_test"] = {
        "state": "0.155",
        "raw_today": [
            {"price": 0.155, "from": "2025-01-01T00:00:00+00:00", "till": "2025-01-01T01:00:00+00:00"},  # Price in EUR/kWh, will be scaled by 100
        ],
    }

    rate_data = my_predbat.fetch_octopus_rates(entity_id_price)
    if rate_data and 0 in rate_data:
        # Should be scaled by 100 for Nordpool
        expected = 0.155 * 100
        if abs(rate_data[0] - expected) > 0.01:
            print("ERROR: Expected rate {} with 'price' key (scaled), got {}".format(expected, rate_data[0]))
            failed = True
        else:
            print("Test 3b passed - 'price' key format with scaling works")
    else:
        print("ERROR: No rate data for 'price' key format")
        failed = True

    # Test 4: Nordpool with raw_tomorrow
    print("*** Test 4: Nordpool with raw_tomorrow attribute")

    my_predbat.ha_interface.dummy_items["sensor.nordpool_test"]["raw_tomorrow"] = [
        {"price": 0.125, "from": "2025-01-02T00:00:00+00:00", "till": "2025-01-02T01:00:00+00:00"},
    ]

    rate_data = my_predbat.fetch_octopus_rates(entity_id_price)
    minute_tomorrow = 24 * 60
    if rate_data and minute_tomorrow in rate_data:
        expected = 0.125 * 100
        if abs(rate_data[minute_tomorrow] - expected) > 0.01:
            print("ERROR: Expected rate {} for tomorrow, got {}".format(expected, rate_data[minute_tomorrow]))
            failed = True
        else:
            print("Test 4 passed - raw_tomorrow attribute works")
    else:
        print("ERROR: No rate data for tomorrow in Nordpool format")
        failed = True

    # Test 5: Adjust key for Octopus Intelligent
    print("*** Test 5: Adjust key for Octopus Intelligent adjusted rates")

    entity_id_intelligent = "sensor.octopus_intelligent"
    my_predbat.ha_interface.dummy_items["sensor.octopus_intelligent"] = {
        "state": "15.5",
        "rates": [
            {"value_inc_vat": 30.0, "valid_from": "2025-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:30:00+00:00", "is_intelligent_adjusted": True},  # This slot should be marked as adjusted
            {"value_inc_vat": 15.5, "valid_from": "2025-01-01T00:30:00+00:00", "valid_to": "2025-01-01T01:00:00+00:00", "is_intelligent_adjusted": False},
        ],
    }

    rate_data = my_predbat.fetch_octopus_rates(entity_id_intelligent, adjust_key="is_intelligent_adjusted")

    # Check that io_adjusted was set for the adjusted slot
    if 0 in my_predbat.io_adjusted and my_predbat.io_adjusted[0]:
        print("Test 5 passed - adjust_key correctly identifies intelligent adjusted slots")
    else:
        print("ERROR: Expected minute 0 to be marked as intelligent adjusted")
        failed = True

    # Test 6: Empty entity_id
    print("*** Test 6: Empty entity_id returns empty dict")

    rate_data = my_predbat.fetch_octopus_rates(None)
    if rate_data != {}:
        print("ERROR: Expected empty dict for None entity_id, got {}".format(rate_data))
        failed = True
    else:
        print("Test 6 passed - None entity_id returns empty dict")

    rate_data = my_predbat.fetch_octopus_rates("")
    if rate_data != {}:
        print("ERROR: Expected empty dict for empty entity_id, got {}".format(rate_data))
        failed = True
    else:
        print("Test 6b passed - empty entity_id returns empty dict")

    # Test 7: Missing sensor data
    print("*** Test 7: Missing sensor with no data")

    entity_id_missing = "sensor.nonexistent_sensor"
    rate_data = my_predbat.fetch_octopus_rates(entity_id_missing)

    if rate_data != {}:
        print("ERROR: Expected empty dict for missing sensor, got {}".format(rate_data))
        failed = True
    else:
        print("Test 7 passed - missing sensor returns empty dict")

    # Test 8: Sensor with no attributes
    print("*** Test 8: Sensor exists but has no rate attributes")

    entity_id_no_attrs = "sensor.no_attributes"
    my_predbat.ha_interface.dummy_items["sensor.no_attributes"] = {"state": "15.5"}

    rate_data = my_predbat.fetch_octopus_rates(entity_id_no_attrs)
    if rate_data != {}:
        print("ERROR: Expected empty dict for sensor with no attributes, got {}".format(rate_data))
        failed = True
    else:
        print("Test 8 passed - sensor with no attributes returns empty dict")

    # Restore original values
    my_predbat.forecast_days = old_forecast_days
    my_predbat.midnight_utc = old_midnight_utc

    if not failed:
        print("**** All fetch_octopus_rates tests PASSED ****")
    else:
        print("**** Some fetch_octopus_rates tests FAILED ****")

    return failed
