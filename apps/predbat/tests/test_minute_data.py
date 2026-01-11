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
from utils import minute_data, dp4


def test_minute_data(my_predbat):
    """
    Test the minute_data function from the Fetch class
    """
    failed = False
    print("**** Testing minute_data function ****")

    # Create test datetime objects with timezone awareness
    import pytz

    utc = pytz.UTC
    now = datetime(2024, 10, 4, 12, 5, 0, tzinfo=utc)  # Fixed time for testing

    # Test 1: Basic functionality with simple history data
    print("Test 1: Basic functionality")
    history = [
        {"state": "0.0", "last_updated": "2024-10-04T10:30:00+00:00"},
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "20.0", "last_updated": "2024-10-04T11:30:00+00:00"},
        {"state": "30.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]

    result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False)

    points = [0, 5, 34, 35, 65]
    result_points = [result.get(p) for p in points]
    expected_points = [30.0, 20.0, 20.0, 10.0, 0.0]

    # Check that we have data and it's reasonable
    if len(result) != 24 * 60:
        print("ERROR: Basic test failed - no data returned")
        # Print result sorted by key for easier reading
        failed = True
    elif result_points != expected_points:
        print("ERROR: Basic test failed - values incorrect, data points were {} expected {}".format(result_points, expected_points))
        for key in sorted(result.keys()):
            print("  {}: {}".format(key, result[key]))
        failed = True

    # Test 2: Empty history
    print("Test 2: Empty history")
    empty_result, ignore_io = minute_data(history=[], days=1, now=now, state_key="state", last_updated_key="last_updated")

    if empty_result != {}:
        print("ERROR: Empty history test failed - should return empty dict")
        failed = True

    # Test 3: Scale parameter
    print("Test 3: Scale parameter")
    scaled_result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, scale=2.0)

    if 0 not in scaled_result:
        print("ERROR: Scale test failed - no data at minute 0")
        failed = True
    elif scaled_result[0] != result[0] * 2.0:
        print("ERROR: Scale test failed - scaling incorrect, got {} expected {}".format(scaled_result[0], result[0] * 2.0))
        failed = True

    # Test 4: Smoothing enabled vs disabled comparison
    print("Test 4: Smoothing")
    result, ignore_io = minute_data(
        history=history,
        days=1,
        now=now,
        state_key="state",
        last_updated_key="last_updated",
        backwards=True,
        smoothing=True,
    )

    points = [0, 5, 34, 35, 65]
    expected_points = [30.0, 30.0, dp4(20.0 + 10 / 30), 20.0, 10.0]
    result_points = [result.get(p) for p in points]
    if len(result) != 24 * 60:
        print("ERROR: Smoothing test failed - no data returned")
        failed = True
    elif result_points != expected_points:
        print("ERROR: Smoothing test - unsmoothed values incorrect, data points were {} expected {}".format(result_points, expected_points))
        for key in sorted(result.keys()):
            print("  {}: {}".format(key, result[key]))
        failed = True

    # Test 4.1: Smoothing clean increment
    print("Test 4.1: Smoothing clean increment")
    result, ignore_io = minute_data(
        history=history,
        days=1,
        now=now,
        state_key="state",
        last_updated_key="last_updated",
        backwards=True,
        smoothing=True,
        clean_increment=True,
        max_increment=0,
        interpolate=True,
    )
    points = [0, 5, 34, 35, 65]
    expected_points = [dp4(30.0 + 5 * (10 / 30)), 30.0, dp4(20.0 + 10 / 30), 20.0, 10.0]
    result_points = [result.get(p) for p in points]
    if len(result) != 24 * 60:
        print("ERROR: Smoothing test failed - no data returned")
        failed = True
    elif result_points != expected_points:
        print("ERROR: Smoothing test - unsmoothed values incorrect, data points were {} expected {}".format(result_points, expected_points))
        for key in sorted(result.keys()):
            print("  {}: {}".format(key, result[key]))
        failed = True

    # Test 5: Attributes mode
    print("Test 5: Attributes mode")
    history_with_attrs = [
        {"attributes": {"power": "15.0"}, "last_updated": "2024-10-04T11:00:00+00:00"},
        {"attributes": {"power": "25.0"}, "last_updated": "2024-10-04T11:30:00+00:00"},
    ]

    attrs_result, ignore_io = minute_data(history=history_with_attrs, days=1, now=now, state_key="power", last_updated_key="last_updated", backwards=True, attributes=True)

    if len(attrs_result) == 0:
        print("ERROR: Attributes test failed - no data returned")
        failed = True

    # Test 6: Unit conversion
    print("Test 6: Unit conversion")
    history_with_units = [
        {"state": "1000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]

    converted_result, ignore_io = minute_data(history=history_with_units, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")

    if len(converted_result) == 0:
        print("ERROR: Unit conversion test failed - no data returned")
        failed = True

    # Check if the conversion is correct
    expected_conversion = 1000.0 / 1000.0  # Convert from Wh to kWh
    if converted_result[0] != expected_conversion:
        print("ERROR: Unit conversion test failed - expected {} got {}".format(expected_conversion, converted_result[0]))
        failed = True

    # Test 7: Invalid/unavailable data filtering
    print("Test 7: Invalid data filtering")
    history_with_invalid = [
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "unavailable", "last_updated": "2024-10-04T11:15:00+00:00"},
        {"state": "unknown", "last_updated": "2024-10-04T11:30:00+00:00"},
        {"state": "20.0", "last_updated": "2024-10-04T11:45:00+00:00"},
    ]

    filtered_result, ignore_io = minute_data(history=history_with_invalid, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True)

    # Function should filter out invalid data
    if len(filtered_result) == 0:
        print("ERROR: Invalid data filtering test failed - no data returned")
        failed = True

    # Test 8: Accumulate parameter
    print("Test 8: Accumulate parameter")
    accumulate_data = {}
    for i in range(24 * 60):
        accumulate_data[i] = 1  # Accumulate 1 unit per minute
    result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False)
    accumulated_result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, accumulate=accumulate_data)

    if 0 not in accumulated_result:
        print("ERROR: Accumulate test failed - no data at minute 0")
        failed = True
    else:
        # Check accumulate data is result + 1
        if accumulated_result[0] != result[0] + 1:
            print("ERROR: Accumulate test failed - expected {} got {}".format(result[0] + 1, accumulated_result[0]))
            failed = True

    # Test 9: Forward time direction
    print("Test 9: Forward time direction")
    forward_result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=False)

    # Test 10: Missing keys handling
    print("Test 10: Missing keys handling")
    history_missing_keys = [
        {"state": "10.0"},  # Missing last_updated
        {"last_updated": "2024-10-04T11:30:00+00:00"},  # Missing state
    ]

    missing_keys_result, ignore_io = minute_data(history=history_missing_keys, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True)

    # Test 11: Different state key
    print("Test 11: Different state key")
    history_different_key = [
        {"value": "50.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"value": "60.0", "last_updated": "2024-10-04T11:30:00+00:00"},
    ]

    different_key_result, ignore_io = minute_data(history=history_different_key, days=1, now=now, state_key="value", last_updated_key="last_updated", backwards=True)

    if len(different_key_result) == 0:
        print("ERROR: Different state key test failed - no data returned")
        failed = True

    # Test 12: clean_increment=True with glitch filter (lines 319-337)
    print("Test 12: clean_increment with glitch filter")
    history_glitch = [
        {"state": "10.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "100.0", "last_updated": "2024-10-04T10:30:00+00:00"},  # Spike/glitch
        {"state": "15.0", "last_updated": "2024-10-04T11:00:00+00:00"},  # Back to normal
        {"state": "20.0", "last_updated": "2024-10-04T11:30:00+00:00"},
    ]
    glitch_result, ignore_io = minute_data(history=history_glitch, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=True, clean_increment=True, max_increment=50)
    if len(glitch_result) == 0:
        print("ERROR: clean_increment glitch filter test failed - no data returned")
        failed = True
    else:
        # The glitch (100.0 spike) should be filtered out by clean_increment
        # Values should not include the 100.0 spike, results should be smoothed between valid values
        values = list(glitch_result.values())
        if 100.0 in values:
            print(f"ERROR: clean_increment glitch filter test failed - spike value 100.0 should be filtered, got {values[:10]}")
            failed = True
        # Check that we have reasonable values (should be between 10 and 20 range, not 100)
        max_val = max(values)
        if max_val > 50:
            print(f"ERROR: clean_increment glitch filter test failed - max value {max_val} exceeds max_increment threshold")
            failed = True

    # Test 13: W to kWh unit conversion with integrate=True (lines 378-379)
    print("Test 13: W to kWh unit conversion")
    history_watts = [
        {"state": "1000.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
        {"state": "2000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]
    watts_result, ignore_io = minute_data(history=history_watts, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")
    if len(watts_result) == 0:
        print("ERROR: W to kWh conversion test failed - no data returned")
        failed = True
    else:
        # W to kWh conversion divides by 1000 and integrates over time
        # 2000W = 2kW integrated over 60 minutes = 2.0 kWh at minute 0
        val_at_0 = watts_result.get(0)
        if val_at_0 is None:
            print(f"ERROR: W to kWh conversion - no value at minute 0, keys: {list(watts_result.keys())[:5]}")
            failed = True
        elif abs(val_at_0 - 2.0) > 0.1:
            print(f"ERROR: W to kWh conversion - expected 2.0 at minute 0, got {val_at_0}")
            failed = True

    # Test 14: kW to kWh unit conversion
    print("Test 14: kW to kWh unit conversion")
    history_kw = [
        {"state": "1.5", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
        {"state": "2.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
    ]
    kw_result, ignore_io = minute_data(history=history_kw, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh", to_key=None)
    if len(kw_result) == 0:
        print("ERROR: kW to kWh conversion test failed - no data returned")
        failed = True
    else:
        # kW to kWh with integration over 60 minutes
        # 2.0kW integrated over 60 minutes = 2.0 kWh at minute 0
        val_at_0 = kw_result.get(0)
        if val_at_0 is None:
            print(f"ERROR: kW to kWh conversion - no value at minute 0, keys: {list(kw_result.keys())[:5]}")
            failed = True
        elif abs(val_at_0 - 2.0) > 0.1:
            print(f"ERROR: kW to kWh conversion - expected 2.0 at minute 0, got {val_at_0}")
            failed = True

    # Test 15: spreading parameter (lines 492-494)
    print("Test 15: spreading parameter")
    history_spreading = [
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
    ]
    spread_result, ignore_io = minute_data(history=history_spreading, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=False, spreading=30)  # Spread over 30 minutes
    # Check that data is spread across multiple minutes
    count_with_value = sum(1 for v in spread_result.values() if v == 10.0)
    if count_with_value < 10:
        print(f"ERROR: spreading test failed - expected multiple minutes with value 10.0, got {count_with_value}")
        failed = True

    # Test 16: divide_by parameter
    print("Test 16: divide_by parameter")
    history_divide = [
        {"state": "100.0", "last_updated": "2024-10-04T11:00:00+00:00"},
    ]
    divide_result, ignore_io = minute_data(history=history_divide, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, divide_by=2)
    if divide_result.get(0) != 50.0:
        print(f"ERROR: divide_by test failed - expected 50.0, got {divide_result.get(0)}")
        failed = True

    # Test 17: smoothing forward mode (backwards=False, smoothing=True) - lines 418-419
    print("Test 17: smoothing forward mode")
    history_forward = [
        {"state": "10.0", "last_updated": "2024-10-04T12:10:00+00:00"},  # 5 minutes in future
        {"state": "20.0", "last_updated": "2024-10-04T12:20:00+00:00"},  # 15 minutes in future
    ]
    forward_smooth_result, ignore_io = minute_data(history=history_forward, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=False, smoothing=True)
    if len(forward_smooth_result) == 0:
        print("ERROR: smoothing forward mode test failed - no data returned")
        failed = True

    # Test 18: W to kW unit conversion (lines 384-385)
    print("Test 18: W to kW unit conversion")
    history_w_to_kw = [
        {"state": "5000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]
    w_to_kw_result, ignore_io = minute_data(history=history_w_to_kw, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kW")
    if len(w_to_kw_result) == 0:
        print("ERROR: W to kW conversion test failed - no data returned")
        failed = True
    elif w_to_kw_result.get(0) != 5.0:
        print(f"ERROR: W to kW conversion test failed - expected 5.0, got {w_to_kw_result.get(0)}")
        failed = True

    # Test 19: kW to W unit conversion (line 385 reverse)
    print("Test 19: kW to W unit conversion")
    history_kw_to_w = [
        {"state": "2.5", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
    ]
    kw_to_w_result, ignore_io = minute_data(history=history_kw_to_w, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="W")
    if len(kw_to_w_result) == 0:
        print("ERROR: kW to W conversion test failed - no data returned")
        failed = True
    elif kw_to_w_result.get(0) != 2500.0:
        print(f"ERROR: kW to W conversion test failed - expected 2500.0, got {kw_to_w_result.get(0)}")
        failed = True

    # Test 20: Unsupported unit conversion is skipped (line 388)
    print("Test 20: Unsupported unit conversion is skipped")
    history_bad_unit = [
        {"state": "100.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "gallons"}},
        {"state": "50.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
    ]
    bad_unit_result, ignore_io = minute_data(history=history_bad_unit, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")
    # Should only have the kWh entry, gallons should be skipped
    if len(bad_unit_result) == 0:
        print("ERROR: Unsupported unit test failed - no data returned")
        failed = True

    # Test 21: clean_increment works with days with <1kWh of energy
    print("Test 21: smoothing works with days with <1kWh of energy #2925")
    history_small_day = [
        {"state": "0", "last_updated": "2024-10-03T00:00:03.337Z"},
        {"state": "0.1", "last_updated": "2024-10-03T10:15:05.447Z"},
        {"state": "0.2", "last_updated": "2024-10-03T10:40:50.523Z"},
        {"state": "0.3", "last_updated": "2024-10-03T10:56:05.601Z"},
        {"state": "0.4", "last_updated": "2024-10-03T11:19:35.675Z"},
        {"state": "0.5", "last_updated": "2024-10-03T11:45:50.757Z"},
        {"state": "0.6", "last_updated": "2024-10-03T12:16:06.460Z"},
        {"state": "0.7", "last_updated": "2024-10-03T12:41:36.553Z"},
        {"state": "0", "last_updated": "2024-10-04T00:00:10.437Z"},
        {"state": "0.1", "last_updated": "2024-10-04T07:10:59.088Z"},
        {"state": "0.2", "last_updated": "2024-10-04T08:00:44.250Z"},
        {"state": "0.3", "last_updated": "2024-10-04T08:29:29.324Z"},
        {"state": "0.4", "last_updated": "2024-10-04T08:55:44.430Z"},
        {"state": "0.5", "last_updated": "2024-10-04T09:28:14.534Z"},
        {"state": "0.6", "last_updated": "2024-10-04T10:05:45.016Z"},
        {"state": "0.7", "last_updated": "2024-10-04T10:45:15.217Z"},
        {"state": "0.8", "last_updated": "2024-10-04T11:12:45.405Z"},
        {"state": "0.9", "last_updated": "2024-10-04T11:37:15.525Z"},
        {"state": "1", "last_updated": "2024-10-04T12:00:00.612Z"},
    ]
    small_day_result, ignore_io = minute_data(history=history_small_day, days=2, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=True, clean_increment=True)
    if len(small_day_result) == 0:
        print("ERROR: smoothing with low energy day failed - no data returned")
        failed = True
    elif small_day_result.get(0) != 1.7:
        print(f"ERROR: smoothing with low energy day failed - expected second day total of 1.7kWh, but got {small_day_result.get(0)} ")
        failed = True

    # Test 22: kW to W conversion (matching find_charge_curve parameters)
    print("Test 22: kW to W unit conversion with backwards=True, smoothing=False")

    # Use a specific time for this test (12:00 exactly) to make minute calculations clear
    now_test22 = datetime(2024, 10, 4, 12, 0, 0, tzinfo=utc)

    # Create test history data in kW units
    # With backwards=True and smoothing=False, each value holds until the next timestamp
    history_kw = [
        {"state": "2.5", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
        {"state": "3.0", "last_updated": "2024-10-04T11:30:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
        {"state": "3.5", "last_updated": "2024-10-04T11:45:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
    ]

    # Call minute_data with the same parameters as find_charge_curve
    result_data, ignore_io = minute_data(
        history=history_kw,
        days=1,
        now=now_test22,
        state_key="state",
        last_updated_key="last_updated",
        backwards=True,
        clean_increment=False,
        smoothing=False,
        divide_by=1.0,
        scale=1.0,
        required_unit="W",
    )

    # Check that we got data
    if len(result_data) == 0:
        print("ERROR: kW to W conversion test failed - no data returned")
        failed = True
    else:
        # With backwards=True and smoothing=False, value holds from timestamp until next timestamp
        # So minute 15-29 holds the value from 11:30 (3.0 kW)
        # minute 30-59 holds the value from 11:00 (2.5 kW)
        # minute 60+ holds nothing (first data point has no to_time initially)

        # Check at minute 15 (between 11:45 and 11:30) should hold 11:30 value: 3.0 kW = 3000 W
        if 15 not in result_data:
            print("ERROR: kW to W conversion test failed - no data at minute 15")
            failed = True
        elif result_data[15] != 3000.0:
            print("ERROR: kW to W conversion test failed - expected 3000 W at minute 15, got {}".format(result_data[15]))
            failed = True

        # Check at minute 30 (between 11:30 and 11:00) should hold 11:00 value: 2.5 kW = 2500 W
        if 30 not in result_data:
            print("ERROR: kW to W conversion test failed - no data at minute 30")
            failed = True
        elif result_data[30] != 2500.0:
            print("ERROR: kW to W conversion test failed - expected 2500 W at minute 30, got {}".format(result_data[30]))
            failed = True

        # Check at minute 45 should also be 2500 W (still in the 11:00 to 11:30 range)
        if 45 not in result_data:
            print("ERROR: kW to W conversion test failed - no data at minute 45")
            failed = True
        elif result_data[45] != 2500.0:
            print("ERROR: kW to W conversion test failed - expected 2500 W at minute 45, got {}".format(result_data[45]))
            failed = True

    # Test 23: kW to MW unit conversion (lines 404-405)
    print("Test 23: kW to MW unit conversion")
    history_kw_to_mw = [
        {"state": "5000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
    ]
    kw_to_mw_result, ignore_io = minute_data(history=history_kw_to_mw, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="MW")
    if len(kw_to_mw_result) == 0:
        print("ERROR: kW to MW conversion test failed - no data returned")
        failed = True
    elif kw_to_mw_result.get(0) != 5.0:
        print(f"ERROR: kW to MW conversion test failed - expected 5.0, got {kw_to_mw_result.get(0)}")
        failed = True

    # Test 24: kWh to MWh unit conversion (lines 404-405)
    print("Test 24: kWh to MWh unit conversion")
    history_kwh_to_mwh = [
        {"state": "3500.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
    ]
    kwh_to_mwh_result, ignore_io = minute_data(history=history_kwh_to_mwh, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="MWh")
    if len(kwh_to_mwh_result) == 0:
        print("ERROR: kWh to MWh conversion test failed - no data returned")
        failed = True
    elif kwh_to_mwh_result.get(0) != 3.5:
        print(f"ERROR: kWh to MWh conversion test failed - expected 3.5, got {kwh_to_mwh_result.get(0)}")
        failed = True

    # Test 25: MW to kW unit conversion (lines 406-407)
    print("Test 25: MW to kW unit conversion")
    history_mw_to_kw = [
        {"state": "2.5", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "MW"}},
    ]
    mw_to_kw_result, ignore_io = minute_data(history=history_mw_to_kw, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kW")
    if len(mw_to_kw_result) == 0:
        print("ERROR: MW to kW conversion test failed - no data returned")
        failed = True
    elif mw_to_kw_result.get(0) != 2500.0:
        print(f"ERROR: MW to kW conversion test failed - expected 2500.0, got {mw_to_kw_result.get(0)}")
        failed = True

    # Test 26: MWh to kWh unit conversion (lines 406-407)
    print("Test 26: MWh to kWh unit conversion")
    history_mwh_to_kwh = [
        {"state": "1.25", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "MWh"}},
    ]
    mwh_to_kwh_result, ignore_io = minute_data(history=history_mwh_to_kwh, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")
    if len(mwh_to_kwh_result) == 0:
        print("ERROR: MWh to kWh conversion test failed - no data returned")
        failed = True
    elif mwh_to_kwh_result.get(0) != 1250.0:
        print(f"ERROR: MWh to kWh conversion test failed - expected 1250.0, got {mwh_to_kwh_result.get(0)}")
        failed = True

    print("**** minute_data tests completed ****")
    return failed


def test_minute_data_load(my_predbat):
    """
    Test the minute_data_load function from the Fetch class
    Focuses on error scenarios and entity handling
    """
    failed = False
    print("**** Testing minute_data_load function ****")

    # Create test datetime objects with timezone awareness
    import pytz

    utc = pytz.UTC
    now = datetime(2024, 10, 4, 12, 0, 0, tzinfo=utc)

    # Store original get_history_wrapper for restoration
    original_get_history = my_predbat.get_history_wrapper
    
    # Mock history store
    mock_history_store = {}
    
    def mock_get_history_wrapper(entity_id, days):
        """Mock get_history_wrapper that returns data from mock_history_store"""
        if entity_id in mock_history_store:
            return mock_history_store[entity_id]
        return []

    # Replace get_history_wrapper with mock
    my_predbat.get_history_wrapper = mock_get_history_wrapper

    # Test 1: Basic functionality with single entity
    print("Test 1: Basic functionality with single entity")
    my_predbat.args["test_load_entity"] = "sensor.test_load"
    
    # Mock history data for the entity
    history_data = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "5.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "10.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]
    mock_history_store["sensor.test_load"] = [history_data]
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_load_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    if len(load_minutes) == 0:
        print("ERROR: Basic test failed - no data returned")
        failed = True
    elif age_days < 0:
        print(f"ERROR: Basic test failed - invalid age_days {age_days}")
        failed = True
    else:
        print(f"SUCCESS: Basic test returned {len(load_minutes)} data points with age {age_days} days")

    # Test 2: Multiple entities (list)
    print("Test 2: Multiple entities")
    my_predbat.args["test_multi_entity"] = ["sensor.test_load1", "sensor.test_load2"]
    
    # Mock history for both entities
    history_data1 = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "3.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "6.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]
    history_data2 = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "2.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "4.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]
    mock_history_store["sensor.test_load1"] = [history_data1]
    mock_history_store["sensor.test_load2"] = [history_data2]
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_multi_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    if len(load_minutes) == 0:
        print("ERROR: Multiple entities test failed - no data returned")
        failed = True
    else:
        # Should accumulate both entities, so expect roughly double the value
        print(f"SUCCESS: Multiple entities test returned {len(load_minutes)} data points")

    # Test 3: Missing entity (not configured in args)
    print("Test 3: Missing entity configuration")
    # Don't set the entity in args
    if "test_missing_entity" in my_predbat.args:
        del my_predbat.args["test_missing_entity"]
    
    try:
        load_minutes, age_days = my_predbat.minute_data_load(
            now_utc=now,
            entity_name="test_missing_entity",
            max_days_previous=1,
            load_scaling=1.0,
            required_unit="kWh",
            interpolate=False
        )
        # Should raise TypeError when entity_ids is None
        print("ERROR: Missing entity test failed - expected TypeError but got no exception")
        failed = True
    except TypeError as e:
        if "'NoneType' object is not iterable" in str(e):
            print("SUCCESS: Missing entity handled correctly (raised TypeError as expected)")
        else:
            print(f"ERROR: Missing entity test failed - unexpected TypeError: {e}")
            failed = True

    # Test 4: History fetch returns None (error condition)
    print("Test 4: History fetch returns None (error)")
    my_predbat.args["test_error_entity"] = "sensor.error_entity"
    
    # Mock history to return None (simulates fetch error)
    mock_history_store["sensor.error_entity"] = None
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_error_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Should log warning and return empty results
    if len(load_minutes) != 0:
        print(f"ERROR: Error entity test failed - expected empty dict, got {len(load_minutes)} items")
        failed = True
    elif age_days != 0:
        print(f"ERROR: Error entity test failed - expected age_days=0, got {age_days}")
        failed = True
    else:
        print("SUCCESS: History fetch error handled correctly")

    # Test 5: History fetch returns empty list (no data)
    print("Test 5: History fetch returns empty list")
    my_predbat.args["test_empty_entity"] = "sensor.empty_entity"
    
    # Mock history to return empty list
    mock_history_store["sensor.empty_entity"] = []
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_empty_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Should log warning and return empty results
    if len(load_minutes) != 0:
        print(f"ERROR: Empty entity test failed - expected empty dict, got {len(load_minutes)} items")
        failed = True
    elif age_days != 0:
        print(f"ERROR: Empty entity test failed - expected age_days=0, got {age_days}")
        failed = True
    else:
        print("SUCCESS: Empty history handled correctly")

    # Test 6: ValueError/TypeError exception during history fetch
    print("Test 6: Exception during history fetch")
    my_predbat.args["test_exception_entity"] = "sensor.exception_entity"
    
    # Mock get_history_wrapper to raise ValueError
    def mock_get_history_error(entity_id, days):
        if entity_id == "sensor.exception_entity":
            raise ValueError("Simulated history fetch error")
        return mock_get_history_wrapper(entity_id, days)
    
    my_predbat.get_history_wrapper = mock_get_history_error
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_exception_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Restore mock
    my_predbat.get_history_wrapper = mock_get_history_wrapper
    
    # Should handle exception and return empty results
    if len(load_minutes) != 0:
        print(f"ERROR: Exception test failed - expected empty dict, got {len(load_minutes)} items")
        failed = True
    elif age_days != 0:
        print(f"ERROR: Exception test failed - expected age_days=0, got {age_days}")
        failed = True
    else:
        print("SUCCESS: History fetch exception handled correctly")

    # Test 7: Invalid last_updated format (error in str2time conversion)
    print("Test 7: Invalid last_updated format")
    my_predbat.args["test_invalid_time_entity"] = "sensor.invalid_time"
    
    history_bad_time = [
        [
            {"state": "10.0", "last_updated": "invalid-timestamp"},
            {"state": "20.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        ]
    ]
    mock_history_store["sensor.invalid_time"] = history_bad_time
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_invalid_time_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Should handle error and use now_utc as fallback, age_days should be 0
    if age_days != 0:
        print(f"ERROR: Invalid time test failed - expected age_days=0 when time conversion fails, got {age_days}")
        failed = True
    else:
        print("SUCCESS: Invalid last_updated format handled correctly")

    # Test 8: Load scaling parameter
    print("Test 8: Load scaling parameter")
    my_predbat.args["test_scaling_entity"] = "sensor.test_scaling"
    
    history_scaling = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "20.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]
    mock_history_store["sensor.test_scaling"] = [history_scaling]
    
    # Test with scaling factor of 2.0
    load_minutes_scaled, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_scaling_entity",
        max_days_previous=1,
        load_scaling=2.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Test with scaling factor of 1.0 for comparison
    load_minutes_normal, _ = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_scaling_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    if len(load_minutes_scaled) == 0:
        print("ERROR: Scaling test failed - no scaled data returned")
        failed = True
    elif len(load_minutes_normal) == 0:
        print("ERROR: Scaling test failed - no normal data returned")
        failed = True
    elif 0 in load_minutes_scaled and 0 in load_minutes_normal:
        # Check that scaling was applied
        if abs(load_minutes_scaled[0] - (load_minutes_normal[0] * 2.0)) > 0.01:
            print(f"ERROR: Scaling test failed - expected {load_minutes_normal[0] * 2.0}, got {load_minutes_scaled[0]}")
            failed = True
        else:
            print(f"SUCCESS: Scaling applied correctly ({load_minutes_normal[0]} -> {load_minutes_scaled[0]})")
    else:
        print("ERROR: Scaling test failed - missing data at minute 0")
        failed = True

    # Test 9: Multiple entities with different ages
    print("Test 9: Multiple entities with different ages (min age calculation)")
    my_predbat.args["test_age_entity"] = ["sensor.old_data", "sensor.new_data"]
    
    # Old data (2 days old)
    history_old = [
        {"state": "0.0", "last_updated": "2024-10-02T10:00:00+00:00"},
        {"state": "5.0", "last_updated": "2024-10-02T11:00:00+00:00"},
    ]
    # New data (same day)
    history_new = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "3.0", "last_updated": "2024-10-04T11:00:00+00:00"},
    ]
    
    mock_history_store["sensor.old_data"] = [history_old]
    mock_history_store["sensor.new_data"] = [history_new]
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_age_entity",
        max_days_previous=3,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Should return the minimum age (newest data age)
    if age_days != 0:
        print(f"ERROR: Age calculation test failed - expected min age of 0 days, got {age_days}")
        failed = True
    else:
        print(f"SUCCESS: Age calculation correct (min of multiple entities = {age_days} days)")

    # Test 10: Interpolate parameter
    print("Test 10: Interpolate parameter")
    my_predbat.args["test_interpolate_entity"] = "sensor.test_interpolate"
    
    history_sparse = [
        {"state": "0.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "10.0", "last_updated": "2024-10-04T12:00:00+00:00"},  # 2 hour gap
    ]
    mock_history_store["sensor.test_interpolate"] = [history_sparse]
    
    # Test with interpolate=True
    load_minutes_interp, _ = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_interpolate_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=True
    )
    
    # Test with interpolate=False
    load_minutes_no_interp, _ = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_interpolate_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    if len(load_minutes_interp) == 0:
        print("ERROR: Interpolate test failed - no interpolated data returned")
        failed = True
    elif len(load_minutes_no_interp) == 0:
        print("ERROR: Interpolate test failed - no non-interpolated data returned")
        failed = True
    else:
        print(f"SUCCESS: Interpolate parameter works (interp={len(load_minutes_interp)}, no_interp={len(load_minutes_no_interp)})")

    # Test 11: Unit conversion (W to kWh)
    print("Test 11: Unit conversion (W to kWh)")
    my_predbat.args["test_unit_entity"] = "sensor.test_watts"
    
    history_watts = [
        {"state": "1000.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
        {"state": "2000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]
    mock_history_store["sensor.test_watts"] = [history_watts]
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_unit_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    if len(load_minutes) == 0:
        print("ERROR: Unit conversion test failed - no data returned")
        failed = True
    else:
        print(f"SUCCESS: Unit conversion applied (W to kWh)")

    # Test 12: Entity list with one failing entity
    print("Test 12: Mixed success/failure in entity list")
    my_predbat.args["test_mixed_entity"] = ["sensor.good", "sensor.bad"]
    
    # Good entity has data
    history_good = [
        {"state": "5.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "10.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]
    mock_history_store["sensor.good"] = [history_good]
    # Bad entity returns None
    mock_history_store["sensor.bad"] = None
    
    load_minutes, age_days = my_predbat.minute_data_load(
        now_utc=now,
        entity_name="test_mixed_entity",
        max_days_previous=1,
        load_scaling=1.0,
        required_unit="kWh",
        interpolate=False
    )
    
    # Should process good entity and log warning for bad entity
    if len(load_minutes) == 0:
        print("ERROR: Mixed entity test failed - expected data from good entity")
        failed = True
    else:
        print(f"SUCCESS: Mixed success/failure handled (got {len(load_minutes)} data points from good entity)")

    # Test 13: History with nested list structure issue
    print("Test 13: History list structure validation")
    my_predbat.args["test_structure_entity"] = "sensor.structure"
    
    # Test with improperly structured history (not nested list)
    history_flat = [
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
    ]
    # This should be [[{...}]] but we're testing [{}]
    mock_history_store["sensor.structure"] = history_flat
    
    try:
        load_minutes, age_days = my_predbat.minute_data_load(
            now_utc=now,
            entity_name="test_structure_entity",
            max_days_previous=1,
            load_scaling=1.0,
            required_unit="kWh",
            interpolate=False
        )
        # If it doesn't crash, check what we got
        if len(load_minutes) != 0:
            print(f"WARN: Structure test - flat list should not process but got {len(load_minutes)} points")
        else:
            print("SUCCESS: Flat history structure handled gracefully")
    except (KeyError, TypeError, IndexError) as e:
        # Expected to fail with improperly structured history
        print(f"SUCCESS: Flat history structure rejected with {type(e).__name__} (expected behavior)")

    # Restore original get_history_wrapper
    my_predbat.get_history_wrapper = original_get_history

    print("**** minute_data_load tests completed ****")
    return failed
