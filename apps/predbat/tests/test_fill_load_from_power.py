"""
Test fill_load_from_power function
"""

import sys
import os

# Add the apps/predbat directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "apps", "predbat"))

from fetch import Fetch
from utils import dp4


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
