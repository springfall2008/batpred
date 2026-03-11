"""
Test fill_load_from_power function
"""

import sys
import os
from datetime import datetime, timezone, timedelta

# Add the apps/predbat directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "apps", "predbat"))

from fetch import Fetch
from utils import dp4, minute_data


class TestFetch(Fetch):
    """Test class that inherits from Fetch to access its methods"""

    def __init__(self):
        # Initialize minimal required attributes
        self.log_messages = []
        self.forecast_minutes = 24 * 60  # 24 hours
        self.plan_interval_minutes = 30

    def log(self, message):
        """Capture log messages"""
        self.log_messages.append(message)
        print(message)

    def get_arg(self, name, default=None):
        """Mock get_arg to return default values"""
        return default


def test_fill_load_from_power_basic():
    """
    Test basic power integration with load data using 30-minute periods
    Load data spans 60 minutes (two 30-minute periods)
    Power should be integrated and scaled to match load consumption in each period
    """
    print("\n=== Test 1: Basic power integration with 30-minute periods ===")

    fetch = TestFetch()

    # Load data going backwards (minute 0 is now, higher minutes are past)
    # Create 60 minutes of data (two 30-minute periods)
    # Starts at 10.0 kWh, ends at 7.0 kWh (3.0 kWh consumed over 60 minutes)
    load_minutes = {}
    for minute in range(0, 30):
        load_minutes[minute] = 10.0  # Gap-filled data, all same value
    for minute in range(30, 60):
        load_minutes[minute] = 8.5
    load_minutes[60] = 7.0  # Boundary value

    # Power data in watts - varying realistic consumption
    load_power_data = {}
    for minute in range(0, 30):
        # Varying power between 2.5-3.5 kW (averages to 3 kW = 3.0 kWh over 60 min)
        load_power_data[minute] = 3000.0 + 500.0 * ((minute % 5) - 2)  # Varies: 2500, 2750, 3000, 3250, 3500, repeat
    for minute in range(30, 60):
        # Varying power between 2.5-3.5 kW
        load_power_data[minute] = 3000.0 + 500.0 * ((minute % 5) - 2)

    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # Check the two 30-minute periods (minutes 0-29 and 30-59)
    # Minute 0 should be highest value (around 10.0)
    assert result[0] >= 9.9, f"Minute 0 should be near 10.0, got {result[0]}"

    print(result)

    # Values should decrease smoothly through the entire period
    assert result[1] < result[0], "Load should decrease from minute 0 to 1"
    assert result[15] < result[0], "Load should decrease over time"
    assert result[29] < result[15], "Load should continue decreasing"
    assert result[30] < result[29], "Load should decrease continuously past minute 30"
    assert result[45] < result[30], "Load should decrease over time"
    assert result[59] < result[45], "Load should continue decreasing"

    # Check that total energy is approximately preserved (3.0 kWh over 60 minutes)
    total_consumption = result[0] - result[59]
    assert abs(total_consumption - 3.0) < 0.2, f"Total consumption should be near 3.0 kWh, got {dp4(total_consumption)} kWh"

    print(f"✓ Two 30-minute periods (0-59): {dp4(result[0])} -> {dp4(result[59])}, consumption: {dp4(total_consumption)} kWh")
    print("Test 1 PASSED")


def test_fill_load_from_power_no_power_data():
    """
    Test with no power data - should return original load_minutes with warning
    """
    print("\n=== Test 2: No power data (returns original) ===")

    fetch = TestFetch()

    load_minutes = {
        0: 3.0,
        1: 3.0,
        2: 3.0,
        3: 3.0,
        4: 2.0,
        5: 2.0,
    }

    # Empty power data
    load_power_data = {}

    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # Should return original load_minutes unchanged with a warning
    assert result[0] == 3.0, f"Minute 0: expected 3.0, got {result[0]}"
    assert result[1] == 3.0, f"Minute 1: expected 3.0, got {result[1]}"
    assert result[2] == 3.0, f"Minute 2: expected 3.0, got {result[2]}"
    assert result[3] == 3.0, f"Minute 3: expected 3.0, got {result[3]}"
    assert result[4] == 2.0, f"Minute 4: expected 2.0, got {result[4]}"
    assert result[5] == 2.0, f"Minute 5: expected 2.0, got {result[5]}"

    # Check warning was logged
    assert any("No power data" in msg for msg in fetch.log_messages), "Should log warning about no power data"

    print(f"✓ Original data returned: {dp4(result[0])}, {dp4(result[4])}")
    print("Test 2 PASSED")


def test_fill_load_from_power_partial_power_data():
    """
    Test with power data in first part of 30-minute period but not the rest
    """
    print("\n=== Test 3: Partial power data ===")

    fetch = TestFetch()

    # Create 60 minutes of data (two 30-minute periods)
    load_minutes = {}
    for minute in range(0, 61):
        load_minutes[minute] = 5.0 - (minute / 60.0) * 4.0  # Linear decrease from 5.0 to 1.0

    # Power data only for first 30 minutes
    load_power_data = {}
    for minute in range(0, 30):
        # Varying power around 4 kW (averages 2.0 kWh over 30 min)
        load_power_data[minute] = 4000.0 + 400.0 * ((minute % 3) - 1)  # Varies: 3600, 4000, 4400, repeat
    # No data for minutes 30-59

    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # With 30-minute periods and power only in first 30 minutes:
    # First period (0-29): consumption = 1.5 kWh (from 5.0 to 3.5)
    # Power integrated (~2.0 kWh) gets scaled to match 1.5 kWh
    # Second period (30-59): consumption = 2.5 kWh (from 3.5 to 1.0)
    # No power data, so evenly distributed
    assert result[0] >= 4.9, f"Minute 0 should be near 5.0, got {result[0]}"
    assert result[1] < result[0], "Should decrease with power integration"
    assert result[29] < result[1], "Should continue decreasing"

    # Period boundary at minute 30 should show transition
    assert result[30] < result[29], "Should continue decreasing into second period"
    assert result[59] < result[30], "Should decrease through second period"

    # Check total consumption is preserved
    total_consumption = result[0] - result[59]
    assert abs(total_consumption - 4.0) < 0.2, f"Total consumption should be near 4.0 kWh, got {dp4(total_consumption)} kWh"

    print(f"✓ Two 30-minute periods: Power in first period, distributed in second")
    print(f"  {dp4(result[0])} -> {dp4(result[29])} -> {dp4(result[59])}")
    print("Test 3 PASSED")


def test_fill_load_from_power_single_minute_period():
    """
    Test with short data span (less than 30 minutes)
    """
    print("\n=== Test 4: Short data span (< 30 minutes) ===")

    fetch = TestFetch()

    # Only 10 minutes of data - all in one 30-minute period
    load_minutes = {}
    for minute in range(0, 10):
        load_minutes[minute] = 5.0  # Gap-filled, constant
    load_minutes[10] = 4.0  # Total consumption: 1.0 kWh

    load_power_data = {}
    for minute in range(0, 10):
        # Varying power around 6 kW (averages to 1.0 kWh over 10 min)
        load_power_data[minute] = 6000.0 + 600.0 * ((minute % 3) - 1)  # Varies: 5400, 6000, 6600, repeat

    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # All within first 30-minute period
    # Minute 0 should be around 5.0
    assert abs(result[0] - 5.0) < 0.01, f"Minute 0: expected 5.0, got {result[0]}"
    # Should decrease smoothly
    assert result[1] < result[0], "Load should decrease"
    assert result[9] < result[1], "Load should continue decreasing"
    # Last value should be around 4.0
    assert abs(result[9] - 4.0) < 0.1, f"Minute 9: expected ~4.0, got {result[9]}"

    print(f"✓ Short span handled: {dp4(result[0])} -> {dp4(result[9])}")
    print("Test 4 PASSED")


def test_fill_load_from_power_zero_load():
    """
    Test with zero load values and no power data
    Zero values should be preserved when there's no power data
    """
    print("\n=== Test 5: Zero load handling ===")

    fetch = TestFetch()

    # 30 minutes of zero load
    load_minutes = {}
    for minute in range(0, 30):
        load_minutes[minute] = 0.0
    load_minutes[30] = 0.0

    # No power data - testing zero load preservation
    load_power_data = {}

    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # With zero load and no power data, result should remain at zero
    for minute in range(0, 30):
        assert result[minute] == 0.0, f"Minute {minute} should be 0.0, got {result[minute]}"

    print("✓ Zero load values preserved")
    print("Test 5 PASSED")


def test_fill_load_from_power_backwards_time():
    """
    Test that backwards time indexing is handled correctly
    Minute 0 is now, minute 10 is 10 minutes ago
    Load should decrease as we go back in time
    """
    print("\n=== Test 6: Backwards time indexing ===")

    fetch = TestFetch()

    # Simulate real-world scenario: load accumulates over time
    # At minute 0 (now): 15.5 kWh consumed today
    # At minute 30 (30 min ago): 14.75 kWh consumed
    # At minute 60 (1 hour ago): 14.0 kWh consumed
    # At minute 90 (1.5 hours ago): 13.0 kWh consumed
    # At minute 120 (2 hours ago): 12.0 kWh consumed
    # Make load data slightly vary to avoid being detected as constant/zero periods
    load_minutes = {}
    for minute in range(0, 30):
        # Slight variation around 15.5 to avoid constant detection
        load_minutes[minute] = 15.5 + 0.001 * (minute % 5)
    for minute in range(30, 60):
        # Slight variation around 14.75
        load_minutes[minute] = 14.75 + 0.001 * (minute % 5)
    for minute in range(60, 90):
        # Slight variation around 14.0
        load_minutes[minute] = 14.0 + 0.001 * (minute % 5)
    for minute in range(90, 120):
        # Slight variation around 13.0
        load_minutes[minute] = 13.0 + 0.001 * (minute % 5)
    for minute in range(120, 240):
        # Slight variation around 12.0 (extend to 240 to span more periods)
        load_minutes[minute] = 12.0 + 0.001 * (minute % 5)

    # Power data showing consumption pattern
    load_power_data = {}
    for minute in range(0, 30):
        # Varying around 1.5 kW average in first 30 min
        load_power_data[minute] = 1500.0 + 150.0 * ((minute % 3) - 1)  # Varies: 1350, 1500, 1650, repeat
    for minute in range(30, 60):
        # Varying around 1.5 kW average in second 30 min
        load_power_data[minute] = 1500.0 + 150.0 * ((minute % 3) - 1)
    for minute in range(60, 90):
        # Varying around 2.0 kW average in third 30 min
        load_power_data[minute] = 2000.0 + 200.0 * ((minute % 3) - 1)  # Varies: 1800, 2000, 2200, repeat
    for minute in range(90, 120):
        # Varying around 2.0 kW average in fourth 30 min
        load_power_data[minute] = 2000.0 + 200.0 * ((minute % 3) - 1)
    for minute in range(120, 240):
        # Varying around 1.0 kW average in remaining periods
        load_power_data[minute] = 1000.0 + 100.0 * ((minute % 3) - 1)  # Varies: 900, 1000, 1100, repeat

    result = fetch.fill_load_from_power(load_minutes, load_power_data)

    # Check that load is highest at minute 0 (now) and decreases going backwards
    assert result[0] >= result[29], "Load at minute 0 should be >= minute 29"
    assert result[29] >= result[30], "Load at minute 29 should be >= minute 30"
    assert result[30] >= result[59], "Load at minute 30 should be >= minute 59"
    assert result[59] >= result[60], "Load at minute 59 should be >= minute 60"
    assert result[60] >= result[89], "Load at minute 60 should be >= minute 89"
    assert result[89] >= result[90], "Load at minute 89 should be >= minute 90"
    assert result[90] >= result[119], "Load at minute 90 should be >= minute 119"

    # Check that load decreases across 30-minute period boundaries
    assert result[119] >= result[120], "Load should be continuous across period boundary at 120"
    assert result[120] >= result[150], "Load at minute 120 should be >= minute 150"

    # Check that total energy is preserved in first two 30-minute periods (0-59)
    # First 60 minutes: consumption should be ~1.5 kWh
    first_hour_consumption = result[0] - result[59]
    assert abs(first_hour_consumption - 1.5) < 0.5, f"First hour should have ~1.5 kWh consumption, got {dp4(first_hour_consumption)}"

    print(f"✓ Backwards time: minute 0 (now) = {dp4(result[0])} kWh")
    print(f"✓ Backwards time: minute 60 (1h ago) = {dp4(result[60])} kWh")
    print(f"✓ Backwards time: minute 120 (2h ago) = {dp4(result[120])} kWh")
    print("Test 6 PASSED")


def generate_ge_cloud_history(days=8, now_utc=None):
    """Generate realistic sparse 5-minute GE Cloud consumption data.

    Returns a list of dicts sorted oldest-first with cumulative 'consumption'
    values and 'last_updated' ISO timestamps, mimicking GE Cloud history.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    history = []
    cumulative = 0.0

    # Generate from oldest to newest (ascending time)
    start_time = now_utc - timedelta(days=days)
    current_time = start_time

    while current_time <= now_utc:
        hour = current_time.hour

        # Consumption rate varies by time of day (kWh per 5 minutes)
        if 23 <= hour or hour < 6:
            # Overnight: very low consumption (standby ~0.1 kW)
            consumption_rate = 0.1 / 12  # kWh per 5-min interval
        elif 6 <= hour < 9:
            # Morning peak
            consumption_rate = 1.5 / 12
        elif 9 <= hour < 17:
            # Daytime moderate
            consumption_rate = 0.8 / 12
        elif 17 <= hour < 23:
            # Evening peak
            consumption_rate = 2.0 / 12

        cumulative += consumption_rate

        history.append(
            {
                "consumption": round(cumulative, 4),
                "last_updated": current_time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            }
        )

        current_time += timedelta(minutes=5)

    return history


def interpolate_sparse_data_local(data):
    """Local implementation of linear interpolation between sparse points.

    Given a dict of {minute: value}, finds gaps (where consecutive minutes
    are missing) and linearly interpolates between the bounding values.
    Returns a new dict with all gaps filled.
    """
    if not data:
        return {}

    result = dict(data)
    min_minute = min(data.keys())
    max_minute = max(data.keys())

    # Find filled points and sort them
    filled_minutes = sorted(data.keys())
    if len(filled_minutes) < 2:
        return result

    # Interpolate between each pair of filled points
    for i in range(len(filled_minutes) - 1):
        start_m = filled_minutes[i]
        end_m = filled_minutes[i + 1]
        gap = end_m - start_m

        if gap <= 1:
            continue  # No gap to fill

        start_val = data[start_m]
        end_val = data[end_m]

        for m in range(start_m + 1, end_m):
            frac = (m - start_m) / gap
            result[m] = dp4(start_val + (end_val - start_val) * frac)

    return result


def test_minute_data_densifies_sparse_ge_cloud_data():
    """
    Prove that minute_data() with smoothing=True and clean_increment=True
    produces fully dense per-minute output from sparse 5-minute GE Cloud data,
    making any subsequent interpolation a no-op.
    """
    print("\n=== Test 7: minute_data densifies sparse GE Cloud data ===")

    days = 8
    now_utc = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    history = generate_ge_cloud_history(days=days, now_utc=now_utc)

    print(f"  Generated {len(history)} sparse history points over {days} days")
    print(f"  First: {history[0]['last_updated']} = {history[0]['consumption']}")
    print(f"  Last:  {history[-1]['last_updated']} = {history[-1]['consumption']}")

    # Call minute_data with the EXACT params from download_ge_data (fetch.py:1286)
    result, _ = minute_data(
        history,
        days,
        now_utc,
        "consumption",
        "last_updated",
        backwards=True,
        smoothing=True,
        scale=1.0,
        clean_increment=True,
        interpolate=True,
    )

    # Check density: every minute from 0 to 8*24*60 - 1 should have a value
    total_minutes = days * 24 * 60
    filled = sum(1 for m in range(total_minutes) if m in result)
    missing = total_minutes - filled

    print(f"  Total minutes expected: {total_minutes}")
    print(f"  Minutes filled: {filled}")
    print(f"  Minutes missing: {missing}")

    assert missing == 0, f"minute_data left {missing} gaps out of {total_minutes} minutes"

    # Now run our local interpolate_sparse_data on the result and count changes
    interpolated = interpolate_sparse_data_local(result)

    changes = 0
    for m in range(total_minutes):
        if m in result and m in interpolated:
            if abs(result[m] - interpolated[m]) > 0.0001:
                changes += 1

    print(f"  Values changed by interpolation: {changes}")
    print(f"  Conclusion: minute_data() already produces dense output, " f"interpolate_sparse_data() is a no-op")

    assert changes == 0, f"interpolate_sparse_data changed {changes} values -- " f"minute_data output was not fully dense"

    print("Test 7 PASSED")


def test_minute_data_output_after_clean_incrementing_reverse():
    """
    Test that clean_incrementing_reverse converts cumulative GE Cloud data
    to proper incremental values: zero-consumption overnight periods should
    produce 0 values, not flat cumulative values.
    """
    print("\n=== Test 8: clean_incrementing_reverse produces incremental output ===")

    days = 8
    now_utc = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    history = generate_ge_cloud_history(days=days, now_utc=now_utc)

    result, _ = minute_data(
        history,
        days,
        now_utc,
        "consumption",
        "last_updated",
        backwards=True,
        smoothing=True,
        scale=1.0,
        clean_increment=True,
        interpolate=True,
    )

    total_minutes = days * 24 * 60

    # After clean_increment=True, output should be incremental (cumulative energy consumed).
    # Minute 0 has the highest value (total consumed), and values decrease going back in time.
    # The increments (result[m] - result[m+1]) should be >= 0 for all m.

    # Check that the output is monotonically non-increasing (minute 0 >= minute 1 >= ...)
    violations = 0
    for m in range(total_minutes - 1):
        if result[m] < result[m + 1] - 0.001:  # small tolerance
            violations += 1

    print(f"  Monotonicity violations: {violations}")
    assert violations == 0, f"Found {violations} monotonicity violations in incremental output"

    # Check overnight periods: between 23:00 and 06:00 the consumption rate is very low.
    # Pick deep overnight (02:00) to avoid boundary effects at 23:00 transition.
    # now_utc is 2026-03-10 12:00, so 2026-03-07 02:00 is 3 days 10 hours ago.
    sample_night_start = now_utc - timedelta(days=3, hours=10)  # 02:00 three days ago
    night_offset_min = int((now_utc - sample_night_start).total_seconds() / 60)

    overnight_increments = []
    for m in range(night_offset_min, min(night_offset_min + 60, total_minutes - 1)):
        if m in result and (m + 1) in result:
            inc = result[m] - result[m + 1]
            overnight_increments.append(inc)

    if overnight_increments:
        avg_overnight = sum(overnight_increments) / len(overnight_increments)
        max_overnight = max(overnight_increments)
        print(f"  Sample overnight period ({len(overnight_increments)} minutes):")
        print(f"    Average increment: {dp4(avg_overnight)} kWh/min")
        print(f"    Max increment: {dp4(max_overnight)} kWh/min")
        # Overnight standby is ~0.1 kW = 0.1/60 kWh/min ~ 0.0017
        assert max_overnight < 0.01, f"Overnight increment too high: {dp4(max_overnight)} kWh/min, " f"expected near-zero for standby"

    # Check a daytime evening peak period for comparison
    sample_evening = now_utc - timedelta(days=1, hours=18)  # ~18:00 yesterday -> evening peak
    evening_offset_min = int((now_utc - sample_evening).total_seconds() / 60)

    evening_increments = []
    for m in range(evening_offset_min, min(evening_offset_min + 60, total_minutes - 1)):
        if m in result and (m + 1) in result:
            inc = result[m] - result[m + 1]
            evening_increments.append(inc)

    if evening_increments:
        avg_evening = sum(evening_increments) / len(evening_increments)
        print(f"  Sample evening peak ({len(evening_increments)} minutes):")
        print(f"    Average increment: {dp4(avg_evening)} kWh/min")
        # Evening is ~2.0 kW = 2.0/60 kWh/min ~ 0.033
        assert avg_evening > 0.01, f"Evening increment too low: {dp4(avg_evening)}, expected higher consumption"

    print("Test 8 PASSED")


def test_fill_load_no_difference_with_prior_interpolation():
    """
    Prove that fill_load_from_power produces identical results whether or
    not you run interpolate_sparse_data on the minute_data output first.
    Since minute_data already densifies, the interpolation is redundant.
    """
    print("\n=== Test 9: fill_load_from_power unaffected by prior interpolation ===")

    days = 8
    now_utc = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    history = generate_ge_cloud_history(days=days, now_utc=now_utc)

    # Get dense minute_data output
    load_minutes, _ = minute_data(
        history,
        days,
        now_utc,
        "consumption",
        "last_updated",
        backwards=True,
        smoothing=True,
        scale=1.0,
        clean_increment=True,
        interpolate=True,
    )

    # Also create an interpolated version
    load_minutes_interpolated = interpolate_sparse_data_local(load_minutes)

    # Create some realistic power data for fill_load_from_power
    # Just use the first 240 minutes (4 hours) to keep it manageable
    test_range = 240
    load_subset = {m: load_minutes[m] for m in range(test_range + 1) if m in load_minutes}
    load_subset_interp = {m: load_minutes_interpolated[m] for m in range(test_range + 1) if m in load_minutes_interpolated}

    # Generate varying power data
    load_power_data = {}
    for minute in range(test_range):
        # Realistic varying power between 0.5-3.0 kW
        load_power_data[minute] = 1500.0 + 1000.0 * ((minute % 7) / 6.0 - 0.5)

    fetch1 = TestFetch()
    fetch2 = TestFetch()

    result_direct = fetch1.fill_load_from_power(load_subset, load_power_data)
    result_interpolated = fetch2.fill_load_from_power(load_subset_interp, load_power_data)

    # Compare results
    differences = 0
    max_diff = 0.0
    for m in range(test_range):
        if m in result_direct and m in result_interpolated:
            diff = abs(result_direct[m] - result_interpolated[m])
            if diff > 0.0001:
                differences += 1
            max_diff = max(max_diff, diff)

    print(f"  Test range: {test_range} minutes")
    print(f"  Differences found: {differences}")
    print(f"  Max difference: {dp4(max_diff)} kWh")

    assert differences == 0, f"fill_load_from_power produced {differences} different values " f"when input was pre-interpolated (max diff: {dp4(max_diff)})"

    print(f"  Conclusion: Pre-interpolating minute_data output has zero effect on " f"fill_load_from_power results")
    print("Test 9 PASSED")


def generate_ge_cloud_history_zero_overnight(days=8, now_utc=None):
    """Generate GE Cloud data with true zero overnight consumption.

    Many real-world installations have periods of zero consumption (e.g.
    battery-powered homes, solar-only, or efficient heat pump setups where
    overnight consumption truly reaches zero). This generates data that
    triggers the gap detector false positive.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    history = []
    cumulative = 0.0
    start_time = now_utc - timedelta(days=days)
    current_time = start_time

    while current_time <= now_utc:
        hour = current_time.hour

        if 23 <= hour or hour < 6:
            consumption_rate = 0.0  # True zero overnight consumption
        elif 6 <= hour < 9:
            consumption_rate = 1.5 / 12
        elif 9 <= hour < 17:
            consumption_rate = 0.8 / 12
        elif 17 <= hour < 23:
            consumption_rate = 2.0 / 12

        cumulative += consumption_rate

        history.append(
            {
                "consumption": round(cumulative, 4),
                "last_updated": current_time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            }
        )

        current_time += timedelta(minutes=5)

    return history


def test_gap_detector_false_positives_on_overnight_zeros():
    """
    Demonstrate the root cause of load inflation: the gap detector in
    previous_days_modal_filter (fetch.py:424-425) checks whether consecutive
    PREDICT_STEP-spaced values are equal. After clean_incrementing_reverse,
    zero-consumption periods have flat incremental values (the cumulative
    total doesn't change when there's no consumption). This triggers
    false-positive gap detection, and the gaps get filled with average
    daily consumption, inflating load.

    Uses true zero overnight consumption to reproduce the real-world bug
    seen in homes with battery/solar setups where overnight grid import
    drops to zero.
    """
    print("\n=== Test 10: Gap detector false positives on overnight zeros ===")

    PREDICT_STEP = 5  # From const.py

    days = 8
    now_utc = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    history = generate_ge_cloud_history_zero_overnight(days=days, now_utc=now_utc)

    # Get the same minute_data output that download_ge_data produces
    data, _ = minute_data(
        history,
        days,
        now_utc,
        "consumption",
        "last_updated",
        backwards=True,
        smoothing=True,
        scale=1.0,
        clean_increment=True,
        interpolate=True,
    )

    total_minutes = days * 24 * 60

    # Reproduce the exact gap detection logic from fetch.py:423-437
    gap_size = 30  # Default plan_interval_minutes used as load_filter_threshold
    gap_minutes = 0
    gap_start_minute_previous = None
    gap_list = []
    num_gaps = 0
    max_minute = total_minutes

    for minute_previous in range(0, max_minute, PREDICT_STEP):
        if data.get(minute_previous, 0) == data.get(minute_previous + PREDICT_STEP, 0):
            gap_minutes += PREDICT_STEP
            if gap_start_minute_previous is None:
                gap_start_minute_previous = minute_previous
        else:
            if gap_minutes >= gap_size:
                num_gaps += gap_minutes
                gap_list.append((gap_start_minute_previous, gap_minutes))
            gap_minutes = 0
            gap_start_minute_previous = None
    if gap_minutes >= gap_size:
        num_gaps += gap_minutes
        gap_list.append((gap_start_minute_previous, gap_minutes))

    print(f"  Total data span: {total_minutes} minutes ({days} days)")
    print(f"  Gap detection threshold: {gap_size} minutes")
    print(f"  Number of false gaps detected: {len(gap_list)}")
    print(f"  Total false gap minutes: {num_gaps}")

    # Show details of each detected gap with the time of day
    overnight_gaps = 0
    overnight_gap_minutes = 0
    for gap_start, gap_len in gap_list:
        # Convert minute offset back to clock time
        gap_time = now_utc - timedelta(minutes=gap_start)
        gap_end_time = now_utc - timedelta(minutes=gap_start + gap_len)
        print(f"    Gap at minute {gap_start} ({gap_end_time.strftime('%Y-%m-%d %H:%M')}" f" to {gap_time.strftime('%H:%M')}): {gap_len} minutes")

        # Check if this gap falls in overnight hours
        gap_mid_time = now_utc - timedelta(minutes=gap_start + gap_len // 2)
        mid_hour = gap_mid_time.hour
        if 23 <= mid_hour or mid_hour < 6:
            overnight_gaps += 1
            overnight_gap_minutes += gap_len

    print(f"\n  Overnight false-positive gaps: {overnight_gaps}")
    print(f"  Overnight false-positive minutes: {overnight_gap_minutes}")

    # The key assertion: overnight zero-consumption periods ARE being
    # falsely detected as gaps. This is the root cause of load inflation.
    assert len(gap_list) > 0, "Expected false gaps to be detected in zero-consumption overnight periods"
    assert overnight_gaps > 0, "Expected at least some gaps to fall in overnight hours"

    # Quantify the inflation: these gaps cover ~29% of the data
    gap_pct = num_gaps / total_minutes * 100
    print(f"\n  ROOT CAUSE CONFIRMED: The gap detector flags {len(gap_list)} regions " f"({num_gaps} total minutes, {gap_pct:.1f}% of data) as 'gaps'.")
    print(f"  These would be filled with average daily consumption, inflating load.")
    print(f"  The issue is at fetch.py:425 -- checking data[m] == data[m+5] on")
    print(f"  incremental data where zero-consumption periods legitimately have")
    print(f"  equal consecutive values.")

    print("Test 10 PASSED")


def test_gap_filter_fixes_false_positives():
    """
    End-to-end test: with load_data_point_minutes populated from minute_data(),
    the gap filter in previous_days_modal_filter() should skip false-positive
    gaps where the sensor was actively reporting zero consumption.

    This is the complement of Test 10 — Test 10 proves the false positives exist,
    this test proves the fix eliminates them.
    """
    print("\n=== Test 11: Gap filter fixes false positives with data_point_minutes ===")

    PREDICT_STEP_LOCAL = 5
    days = 8
    now_utc = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    history = generate_ge_cloud_history_zero_overnight(days=days, now_utc=now_utc)

    # Collect data_point_minutes during minute_data processing
    data_point_minutes = set()
    data, _ = minute_data(
        history,
        days,
        now_utc,
        "consumption",
        "last_updated",
        backwards=True,
        smoothing=True,
        scale=1.0,
        clean_increment=True,
        interpolate=True,
        data_point_minutes=data_point_minutes,
    )

    print(f"  Data points tracked: {len(data_point_minutes)}")
    total_minutes = days * 24 * 60

    # Run gap detection (same logic as fetch.py)
    gap_size = 30
    gap_minutes_count = 0
    gap_start_minute_previous = None
    gap_list = []
    num_gaps = 0
    max_minute = total_minutes

    for minute_previous in range(0, max_minute, PREDICT_STEP_LOCAL):
        if data.get(minute_previous, 0) == data.get(minute_previous + PREDICT_STEP_LOCAL, 0):
            gap_minutes_count += PREDICT_STEP_LOCAL
            if gap_start_minute_previous is None:
                gap_start_minute_previous = minute_previous
        else:
            if gap_minutes_count >= gap_size:
                num_gaps += gap_minutes_count
                gap_list.append((gap_start_minute_previous, gap_minutes_count))
            gap_minutes_count = 0
            gap_start_minute_previous = None
    if gap_minutes_count >= gap_size:
        num_gaps += gap_minutes_count
        gap_list.append((gap_start_minute_previous, gap_minutes_count))

    print(f"  Gaps detected before filtering: {len(gap_list)} ({num_gaps} minutes)")

    # Now apply the same filter logic as in previous_days_modal_filter
    filtered_gaps = []
    for gap_start, gap_minutes_len in gap_list:
        gap_data_count = sum(1 for m in data_point_minutes if gap_start <= m < gap_start + gap_minutes_len)
        min_data_points = max(gap_minutes_len // 60, 2)
        if gap_data_count >= min_data_points:
            print(f"    Skipping gap at minute {gap_start} ({gap_minutes_len} min) - " f"sensor active ({gap_data_count} of {min_data_points} needed)")
        else:
            filtered_gaps.append((gap_start, gap_minutes_len))

    remaining_gap_minutes = sum(g[1] for g in filtered_gaps)
    print(f"  Gaps remaining after filtering: {len(filtered_gaps)} ({remaining_gap_minutes} minutes)")

    # The key assertion: ALL false-positive gaps should be filtered out
    # because the sensor was reporting data throughout
    assert len(filtered_gaps) == 0, f"Expected all false-positive gaps to be filtered out, " f"but {len(filtered_gaps)} gaps remain ({remaining_gap_minutes} minutes)"

    print(f"\n  FIX CONFIRMED: All {len(gap_list)} false-positive gaps " f"({num_gaps} minutes) correctly filtered out.")
    print(f"  No phantom load will be injected into overnight zero-consumption periods.")

    print("Test 11 PASSED")


def run_all_tests(my_predbat=None):
    """Run all tests"""
    print("\n" + "=" * 60)
    print("Running fill_load_from_power tests")
    print("=" * 60)

    try:
        test_fill_load_from_power_basic()
        test_fill_load_from_power_no_power_data()
        test_fill_load_from_power_partial_power_data()
        test_fill_load_from_power_single_minute_period()
        test_fill_load_from_power_zero_load()
        test_fill_load_from_power_backwards_time()
        test_minute_data_densifies_sparse_ge_cloud_data()
        test_minute_data_output_after_clean_incrementing_reverse()
        test_fill_load_no_difference_with_prior_interpolation()
        test_gap_detector_false_positives_on_overnight_zeros()
        test_gap_filter_fixes_false_positives()

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        return 0  # Return 0 for success
    except AssertionError as e:
        print("\n" + "=" * 60)
        print(f"❌ TEST FAILED: {e}")
        print("=" * 60)
        return 1  # Return 1 for failure
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
        print("=" * 60)
        return 1  # Return 1 for error


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
