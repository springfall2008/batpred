"""
Test fetch_pv_forecast function
"""

import sys
import os
from datetime import datetime, timedelta, timezone

# Add the apps/predbat directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "apps", "predbat"))

from fetch import Fetch
from const import TIME_FORMAT


class TestFetch(Fetch):
    """Test class that inherits from Fetch to access its methods"""

    def __init__(self, midnight_utc=None, prefix="predbat"):
        # Initialize minimal required attributes
        self.log_messages = []
        self.forecast_minutes = 24 * 60  # 24 hours
        self.plan_interval_minutes = 30
        self.prefix = prefix
        if midnight_utc is None:
            self.midnight_utc = datetime(2025, 6, 15, 0, 0, 0)
        else:
            self.midnight_utc = midnight_utc
        self.mock_state = {}

    def log(self, message):
        """Capture log messages"""
        self.log_messages.append(message)
        print(message)

    def get_arg(self, name, default=None):
        """Mock get_arg to return default values"""
        return default

    def get_state_wrapper(self, entity_id=None, attribute=None, default=None):
        """Mock get_state_wrapper to return test data"""
        if entity_id not in self.mock_state:
            return default
        if attribute is None:
            return self.mock_state[entity_id].get("state", default)
        return self.mock_state[entity_id].get("attributes", {}).get(attribute, default)

    def set_mock_state(self, entity_id, state=None, attributes=None):
        """Set mock state for testing"""
        if entity_id not in self.mock_state:
            self.mock_state[entity_id] = {"state": state, "attributes": attributes or {}}
        else:
            if state is not None:
                self.mock_state[entity_id]["state"] = state
            if attributes is not None:
                self.mock_state[entity_id]["attributes"] = attributes


def test_fetch_pv_forecast_with_relative_time():
    """
    Test fetch_pv_forecast with relative_time attribute set to a time different from midnight_utc
    This tests that the minute_offset calculation correctly aligns the forecast data
    """
    print("\n=== Test 1: fetch_pv_forecast with relative_time offset ===")

    # Set midnight_utc to 2025-06-15 00:00:00
    midnight_utc = datetime(2025, 6, 15, 0, 0, 0)
    fetch = TestFetch(midnight_utc=midnight_utc)

    # Set relative_time to 2 hours before midnight_utc
    # This simulates forecast data that was generated for a different time base
    relative_time = midnight_utc - timedelta(hours=2)
    # Need to add timezone info to match TIME_FORMAT which requires %z
    relative_time = relative_time.replace(tzinfo=timezone.utc)
    midnight_utc = midnight_utc.replace(tzinfo=timezone.utc)
    fetch.midnight_utc = midnight_utc
    relative_time_str = relative_time.strftime(TIME_FORMAT)

    # Create mock forecast data
    # Keys are minutes from relative_time
    forecast_data = {
        "0": 0.0,
        "60": 0.5,  # 1 hour from relative_time
        "120": 1.0,  # 2 hours from relative_time (= midnight_utc)
        "180": 1.5,  # 3 hours from relative_time (= 1 hour after midnight_utc)
        "240": 2.0,  # 4 hours from relative_time (= 2 hours after midnight_utc)
    }

    forecast10_data = {
        "0": 0.0,
        "60": 0.4,
        "120": 0.8,
        "180": 1.2,
        "240": 1.6,
    }

    # Set up mock sensor state
    entity_id = f"sensor.{fetch.prefix}_pv_forecast_raw"
    fetch.set_mock_state(
        entity_id,
        state="test",
        attributes={
            "forecast": forecast_data,
            "forecast10": forecast10_data,
            "relative_time": relative_time_str,
        },
    )

    # Call fetch_pv_forecast
    pv_forecast_minute, pv_forecast_minute10 = fetch.fetch_pv_forecast()

    # With a 120 minute offset (relative_time is 2 hours before midnight_utc):
    # forecast minute 0 → output minute 120
    # forecast minute 60 → output minute 180
    # forecast minute 120 → output minute 240
    # forecast minute 180 → output minute 300
    # forecast minute 240 → output minute 360

    # Verify the offset is correctly applied
    assert pv_forecast_minute[120] == 0.0, f"Expected minute 120 to be 0.0, got {pv_forecast_minute[120]}"
    assert pv_forecast_minute[180] == 0.5, f"Expected minute 180 to be 0.5, got {pv_forecast_minute[180]}"
    assert pv_forecast_minute[240] == 1.0, f"Expected minute 240 to be 1.0, got {pv_forecast_minute[240]}"
    assert pv_forecast_minute[300] == 1.5, f"Expected minute 300 to be 1.5, got {pv_forecast_minute[300]}"
    assert pv_forecast_minute[360] == 2.0, f"Expected minute 360 to be 2.0, got {pv_forecast_minute[360]}"

    # Verify forecast10 data is also correctly offset
    assert pv_forecast_minute10[120] == 0.0, f"Expected forecast10 minute 120 to be 0.0, got {pv_forecast_minute10[120]}"
    assert pv_forecast_minute10[180] == 0.4, f"Expected forecast10 minute 180 to be 0.4, got {pv_forecast_minute10[180]}"
    assert pv_forecast_minute10[240] == 0.8, f"Expected forecast10 minute 240 to be 0.8, got {pv_forecast_minute10[240]}"
    assert pv_forecast_minute10[300] == 1.2, f"Expected forecast10 minute 300 to be 1.2, got {pv_forecast_minute10[300]}"
    assert pv_forecast_minute10[360] == 1.6, f"Expected forecast10 minute 360 to be 1.6, got {pv_forecast_minute10[360]}"

    print(f"✓ Forecast data correctly offset by 120 minutes")
    print(f"✓ pv_forecast_minute[120]={pv_forecast_minute[120]}, [180]={pv_forecast_minute[180]}, [240]={pv_forecast_minute[240]}")
    print(f"✓ pv_forecast_minute10[120]={pv_forecast_minute10[120]}, [180]={pv_forecast_minute10[180]}, [240]={pv_forecast_minute10[240]}")
    print("Test 1 PASSED")


def test_fetch_pv_forecast_no_relative_time():
    """
    Test fetch_pv_forecast when relative_time attribute is missing or invalid
    Should fall back to midnight_utc (no offset)
    """
    print("\n=== Test 2: fetch_pv_forecast with missing relative_time ===")

    midnight_utc = datetime(2025, 6, 15, 0, 0, 0)
    fetch = TestFetch(midnight_utc=midnight_utc)

    # Create mock forecast data
    forecast_data = {
        "0": 0.0,
        "60": 0.5,
        "120": 1.0,
        "180": 1.5,
    }

    forecast10_data = {
        "0": 0.0,
        "60": 0.4,
        "120": 0.8,
        "180": 1.2,
    }

    # Set up mock sensor state without relative_time attribute
    entity_id = f"sensor.{fetch.prefix}_pv_forecast_raw"
    fetch.set_mock_state(
        entity_id,
        state="test",
        attributes={
            "forecast": forecast_data,
            "forecast10": forecast10_data,
            # No relative_time attribute
        },
    )

    # Call fetch_pv_forecast
    pv_forecast_minute, pv_forecast_minute10 = fetch.fetch_pv_forecast()

    # With no relative_time, it should fall back to midnight_utc
    # minute_offset = 0, so forecast data should map directly
    assert pv_forecast_minute[0] == 0.0, f"Expected minute 0 to be 0.0, got {pv_forecast_minute[0]}"
    assert pv_forecast_minute[60] == 0.5, f"Expected minute 60 to be 0.5, got {pv_forecast_minute[60]}"
    assert pv_forecast_minute[120] == 1.0, f"Expected minute 120 to be 1.0, got {pv_forecast_minute[120]}"
    assert pv_forecast_minute[180] == 1.5, f"Expected minute 180 to be 1.5, got {pv_forecast_minute[180]}"

    assert pv_forecast_minute10[0] == 0.0, f"Expected forecast10 minute 0 to be 0.0, got {pv_forecast_minute10[0]}"
    assert pv_forecast_minute10[60] == 0.4, f"Expected forecast10 minute 60 to be 0.4, got {pv_forecast_minute10[60]}"
    assert pv_forecast_minute10[120] == 0.8, f"Expected forecast10 minute 120 to be 0.8, got {pv_forecast_minute10[120]}"
    assert pv_forecast_minute10[180] == 1.2, f"Expected forecast10 minute 180 to be 1.2, got {pv_forecast_minute10[180]}"

    print(f"✓ Forecast data used with no offset (relative_time missing)")
    print(f"✓ pv_forecast_minute[0]={pv_forecast_minute[0]}, [60]={pv_forecast_minute[60]}, [120]={pv_forecast_minute[120]}")
    print("Test 2 PASSED")


def test_fetch_pv_forecast_invalid_relative_time():
    """
    Test fetch_pv_forecast when relative_time attribute is invalid (not a valid datetime)
    Should fall back to midnight_utc (no offset)
    """
    print("\n=== Test 3: fetch_pv_forecast with invalid relative_time ===")

    midnight_utc = datetime(2025, 6, 15, 0, 0, 0)
    fetch = TestFetch(midnight_utc=midnight_utc)

    # Create mock forecast data
    forecast_data = {
        "0": 0.0,
        "60": 0.5,
        "120": 1.0,
    }

    forecast10_data = {
        "0": 0.0,
        "60": 0.4,
        "120": 0.8,
    }

    # Set up mock sensor state with invalid relative_time
    entity_id = f"sensor.{fetch.prefix}_pv_forecast_raw"
    fetch.set_mock_state(
        entity_id,
        state="test",
        attributes={
            "forecast": forecast_data,
            "forecast10": forecast10_data,
            "relative_time": "invalid_datetime_string",
        },
    )

    # Call fetch_pv_forecast
    pv_forecast_minute, pv_forecast_minute10 = fetch.fetch_pv_forecast()

    # With invalid relative_time, it should fall back to midnight_utc
    # minute_offset = 0
    assert pv_forecast_minute[0] == 0.0, f"Expected minute 0 to be 0.0, got {pv_forecast_minute[0]}"
    assert pv_forecast_minute[60] == 0.5, f"Expected minute 60 to be 0.5, got {pv_forecast_minute[60]}"
    assert pv_forecast_minute[120] == 1.0, f"Expected minute 120 to be 1.0, got {pv_forecast_minute[120]}"

    print(f"✓ Forecast data used with no offset (invalid relative_time)")
    print(f"✓ pv_forecast_minute[0]={pv_forecast_minute[0]}, [60]={pv_forecast_minute[60]}, [120]={pv_forecast_minute[120]}")
    print("Test 3 PASSED")


def test_fetch_pv_forecast_relative_time_same_as_midnight():
    """
    Test fetch_pv_forecast when relative_time equals midnight_utc (no offset needed)
    """
    print("\n=== Test 4: fetch_pv_forecast with relative_time = midnight_utc ===")

    midnight_utc = datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    fetch = TestFetch(midnight_utc=midnight_utc)

    # relative_time = midnight_utc
    relative_time_str = midnight_utc.strftime(TIME_FORMAT)

    # Create mock forecast data
    forecast_data = {
        "0": 0.0,
        "60": 0.5,
        "120": 1.0,
        "180": 1.5,
    }

    forecast10_data = {
        "0": 0.0,
        "60": 0.4,
        "120": 0.8,
        "180": 1.2,
    }

    # Set up mock sensor state
    entity_id = f"sensor.{fetch.prefix}_pv_forecast_raw"
    fetch.set_mock_state(
        entity_id,
        state="test",
        attributes={
            "forecast": forecast_data,
            "forecast10": forecast10_data,
            "relative_time": relative_time_str,
        },
    )

    # Call fetch_pv_forecast
    pv_forecast_minute, pv_forecast_minute10 = fetch.fetch_pv_forecast()

    # minute_offset = 0 (same time), so data maps directly
    assert pv_forecast_minute[0] == 0.0, f"Expected minute 0 to be 0.0, got {pv_forecast_minute[0]}"
    assert pv_forecast_minute[60] == 0.5, f"Expected minute 60 to be 0.5, got {pv_forecast_minute[60]}"
    assert pv_forecast_minute[120] == 1.0, f"Expected minute 120 to be 1.0, got {pv_forecast_minute[120]}"
    assert pv_forecast_minute[180] == 1.5, f"Expected minute 180 to be 1.5, got {pv_forecast_minute[180]}"

    print(f"✓ Forecast data maps directly when relative_time = midnight_utc")
    print(f"✓ pv_forecast_minute[0]={pv_forecast_minute[0]}, [60]={pv_forecast_minute[60]}, [120]={pv_forecast_minute[120]}")
    print("Test 4 PASSED")


def test_fetch_pv_forecast_previous_day():
    """
    Test fetch_pv_forecast with relative_time from the previous day (24 hours before)
    This is the most realistic scenario - forecast data generated yesterday
    """
    print("\n=== Test 5: fetch_pv_forecast with relative_time from previous day ===")

    midnight_utc = datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    fetch = TestFetch(midnight_utc=midnight_utc)

    # Set relative_time to 24 hours before midnight_utc (previous day)
    relative_time = midnight_utc - timedelta(hours=24)
    relative_time_str = relative_time.strftime(TIME_FORMAT)

    # Create mock forecast data
    # Simulate forecast that spans from yesterday into today
    forecast_data = {
        "0": 0.0,  # Yesterday midnight (24 hours before midnight_utc)
        "360": 0.5,  # Yesterday 6am (18 hours before midnight_utc)
        "720": 1.0,  # Yesterday noon (12 hours before midnight_utc)
        "1080": 1.5,  # Yesterday 6pm (6 hours before midnight_utc)
        "1440": 2.0,  # Today midnight (= midnight_utc)
        "1500": 2.5,  # Today 1am (1 hour after midnight_utc)
    }

    forecast10_data = {
        "0": 0.0,
        "360": 0.4,
        "720": 0.8,
        "1080": 1.2,
        "1440": 1.6,
        "1500": 2.0,
    }

    # Set up mock sensor state
    entity_id = f"sensor.{fetch.prefix}_pv_forecast_raw"
    fetch.set_mock_state(
        entity_id,
        state="test",
        attributes={
            "forecast": forecast_data,
            "forecast10": forecast10_data,
            "relative_time": relative_time_str,
        },
    )

    # Call fetch_pv_forecast
    pv_forecast_minute, pv_forecast_minute10 = fetch.fetch_pv_forecast()

    # Expected minute_offset = (midnight_utc - relative_time) = 24 hours = 1440 minutes
    # So forecast data at minute 0 should map to minute 1440 in pv_forecast_minute
    # forecast data at minute 1440 should map to minute 2880 in pv_forecast_minute

    # Verify the offset is correctly applied
    # Yesterday's data (forecast minute 0) should appear at output minute 1440 (today midnight)
    assert pv_forecast_minute[1440] == 0.0, f"Expected minute 1440 to be 0.0, got {pv_forecast_minute[1440]}"
    # Yesterday 6am (forecast minute 360) should appear at output minute 1800
    assert pv_forecast_minute[1800] == 0.5, f"Expected minute 1800 to be 0.5, got {pv_forecast_minute[1800]}"
    # Yesterday noon (forecast minute 720) should appear at output minute 2160
    assert pv_forecast_minute[2160] == 1.0, f"Expected minute 2160 to be 1.0, got {pv_forecast_minute[2160]}"
    # Yesterday 6pm (forecast minute 1080) should appear at output minute 2520
    assert pv_forecast_minute[2520] == 1.5, f"Expected minute 2520 to be 1.5, got {pv_forecast_minute[2520]}"
    # Today midnight (forecast minute 1440) should appear at output minute 2880
    assert pv_forecast_minute[2880] == 2.0, f"Expected minute 2880 to be 2.0, got {pv_forecast_minute[2880]}"
    # Today 1am (forecast minute 1500) should appear at output minute 2940
    assert pv_forecast_minute[2940] == 2.5, f"Expected minute 2940 to be 2.5, got {pv_forecast_minute[2940]}"

    # Verify forecast10 data
    assert pv_forecast_minute10[1440] == 0.0, f"Expected forecast10 minute 1440 to be 0.0, got {pv_forecast_minute10[1440]}"
    assert pv_forecast_minute10[1800] == 0.4, f"Expected forecast10 minute 1800 to be 0.4, got {pv_forecast_minute10[1800]}"
    assert pv_forecast_minute10[2160] == 0.8, f"Expected forecast10 minute 2160 to be 0.8, got {pv_forecast_minute10[2160]}"
    assert pv_forecast_minute10[2880] == 1.6, f"Expected forecast10 minute 2880 to be 1.6, got {pv_forecast_minute10[2880]}"

    print(f"✓ Forecast data correctly offset by 1440 minutes (24 hours)")
    print(f"✓ pv_forecast_minute[1440]={pv_forecast_minute[1440]}, [2160]={pv_forecast_minute[2160]}, [2880]={pv_forecast_minute[2880]}")
    print("Test 5 PASSED")


def test_fetch_pv_forecast_negative_offset():
    """
    Test fetch_pv_forecast with relative_time after midnight_utc (negative offset)
    This should result in forecast data being placed at earlier minutes
    """
    print("\n=== Test 6: fetch_pv_forecast with negative offset (relative_time > midnight_utc) ===")

    midnight_utc = datetime(2025, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    fetch = TestFetch(midnight_utc=midnight_utc)

    # Set relative_time to 1 hour after midnight_utc
    relative_time = midnight_utc + timedelta(hours=1)
    relative_time_str = relative_time.strftime(TIME_FORMAT)

    # Create mock forecast data
    # Start from minute 60 (1 hour) to avoid negative minutes
    forecast_data = {
        "60": 0.0,  # 60 minutes from relative_time
        "120": 0.5,  # 2 hours from relative_time
        "180": 1.0,  # 3 hours from relative_time
        "240": 1.5,  # 4 hours from relative_time
    }

    forecast10_data = {
        "60": 0.0,
        "120": 0.4,
        "180": 0.8,
        "240": 1.2,
    }

    # Set up mock sensor state
    entity_id = f"sensor.{fetch.prefix}_pv_forecast_raw"
    fetch.set_mock_state(
        entity_id,
        state="test",
        attributes={
            "forecast": forecast_data,
            "forecast10": forecast10_data,
            "relative_time": relative_time_str,
        },
    )

    # Call fetch_pv_forecast
    pv_forecast_minute, pv_forecast_minute10 = fetch.fetch_pv_forecast()

    # Expected minute_offset = (midnight_utc - relative_time) = -60 minutes
    # So forecast data at minute 60 should map to minute 0 in pv_forecast_minute
    # forecast data at minute 120 should map to minute 60 in pv_forecast_minute
    # forecast data at minute 180 should map to minute 120 in pv_forecast_minute
    # forecast data at minute 240 should map to minute 180 in pv_forecast_minute

    # Check that data is mapped correctly with negative offset
    assert pv_forecast_minute[0] == 0.0, f"Expected minute 0 to be 0.0, got {pv_forecast_minute[0]}"
    assert pv_forecast_minute[60] == 0.5, f"Expected minute 60 to be 0.5, got {pv_forecast_minute[60]}"
    assert pv_forecast_minute[120] == 1.0, f"Expected minute 120 to be 1.0, got {pv_forecast_minute[120]}"
    assert pv_forecast_minute[180] == 1.5, f"Expected minute 180 to be 1.5, got {pv_forecast_minute[180]}"

    # Verify forecast10 data
    assert pv_forecast_minute10[0] == 0.0, f"Expected forecast10 minute 0 to be 0.0, got {pv_forecast_minute10[0]}"
    assert pv_forecast_minute10[60] == 0.4, f"Expected forecast10 minute 60 to be 0.4, got {pv_forecast_minute10[60]}"
    assert pv_forecast_minute10[120] == 0.8, f"Expected forecast10 minute 120 to be 0.8, got {pv_forecast_minute10[120]}"
    assert pv_forecast_minute10[180] == 1.2, f"Expected forecast10 minute 180 to be 1.2, got {pv_forecast_minute10[180]}"

    print(f"✓ Forecast data correctly offset by -60 minutes")
    print(f"✓ pv_forecast_minute[0]={pv_forecast_minute[0]}, [60]={pv_forecast_minute[60]}, [120]={pv_forecast_minute[120]}")
    print("Test 6 PASSED")


def run_all_tests(my_predbat=None):
    """Run all fetch_pv_forecast tests"""
    print("\n" + "=" * 60)
    print("Running fetch_pv_forecast tests")
    print("=" * 60)

    failed = False

    try:
        test_fetch_pv_forecast_with_relative_time()
    except AssertionError as e:
        print(f"FAILED: test_fetch_pv_forecast_with_relative_time - {e}")
        failed = True

    try:
        test_fetch_pv_forecast_no_relative_time()
    except AssertionError as e:
        print(f"FAILED: test_fetch_pv_forecast_no_relative_time - {e}")
        failed = True

    try:
        test_fetch_pv_forecast_invalid_relative_time()
    except AssertionError as e:
        print(f"FAILED: test_fetch_pv_forecast_invalid_relative_time - {e}")
        failed = True

    try:
        test_fetch_pv_forecast_relative_time_same_as_midnight()
    except AssertionError as e:
        print(f"FAILED: test_fetch_pv_forecast_relative_time_same_as_midnight - {e}")
        failed = True

    try:
        test_fetch_pv_forecast_previous_day()
    except AssertionError as e:
        print(f"FAILED: test_fetch_pv_forecast_previous_day - {e}")
        failed = True

    try:
        test_fetch_pv_forecast_negative_offset()
    except AssertionError as e:
        print(f"FAILED: test_fetch_pv_forecast_negative_offset - {e}")
        failed = True

    if not failed:
        print("\n" + "=" * 60)
        print("All fetch_pv_forecast tests PASSED")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("Some fetch_pv_forecast tests FAILED")
        print("=" * 60)

    return failed


if __name__ == "__main__":
    failed = run_all_tests()
    sys.exit(1 if failed else 0)
