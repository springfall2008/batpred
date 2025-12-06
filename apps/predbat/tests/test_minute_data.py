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

    print("**** minute_data tests completed ****")
    return failed
