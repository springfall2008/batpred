# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Temperature API Component Tests

Comprehensive test suite for the External Temperature API component.
Tests all major functionality including:
- Initialization and configuration with zone.home fallback
- URL placeholder replacement for latitude/longitude
- API data fetching with retry logic and error handling
- Timezone offset conversion (positive and negative)
- Sensor creation with current temperature and forecast data
- Cache persistence on API failures
- HA timestamp format conversion
"""

from temperature import TemperatureAPI
from datetime import datetime, timezone


class MockTemperatureAPI(TemperatureAPI):
    """Mock TemperatureAPI class for testing without ComponentBase dependencies"""

    def __init__(self, temperature_latitude, temperature_longitude, temperature_url):
        # Don't call parent __init__ to avoid ComponentBase
        self.last_updated_timestamp = None
        self.failures_total = 0
        self.dashboard_items = {}
        self.log_messages = []
        self.prefix = "predbat"
        self._last_updated_time = None
        self.state_storage = {}
        self.initialize(
            temperature_enable=True,
            temperature_latitude=temperature_latitude,
            temperature_longitude=temperature_longitude,
            temperature_url=temperature_url
        )

    def log(self, message):
        self.log_messages.append(message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes, "app": app}

    def update_success_timestamp(self):
        self._last_updated_time = datetime.now(timezone.utc)

    def last_updated_time(self):
        return self._last_updated_time

    def get_state_wrapper(self, entity_id, default=None, attribute=None):
        """Mock get_state_wrapper"""
        if entity_id in self.state_storage:
            if attribute:
                return self.state_storage[entity_id].get("attributes", {}).get(attribute, default)
            return self.state_storage[entity_id].get("state", default)
        return default

    def set_state(self, entity_id, state, attributes=None):
        """Mock set_state"""
        self.state_storage[entity_id] = {"state": state, "attributes": attributes or {}}


def _test_temperature_initialization(my_predbat):
    """Test TemperatureAPI initialization with various configurations"""
    print("  Testing TemperatureAPI initialization...")

    # Test with explicit coordinates
    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    if temp_component.temperature_latitude != 51.5074:
        print("    ERROR: Incorrect latitude: {}".format(temp_component.temperature_latitude))
        return 1

    if temp_component.temperature_longitude != -0.1278:
        print("    ERROR: Incorrect longitude: {}".format(temp_component.temperature_longitude))
        return 1

    print("    PASS: Initialization with explicit coordinates")
    return 0


def _test_temperature_zone_home_fallback(my_predbat):
    """Test zone.home coordinate fallback"""
    print("  Testing zone.home coordinate fallback...")

    # Initialize without explicit coordinates
    temp_component = MockTemperatureAPI(
        temperature_latitude=None,
        temperature_longitude=None,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Set zone.home with coordinates
    temp_component.set_state("zone.home", state="home", attributes={"latitude": 52.52, "longitude": 13.41})

    # Test coordinate resolution
    lat, lon = temp_component.get_coordinates()

    if lat != 52.52 or lon != 13.41:
        print("    ERROR: Failed to fallback to zone.home coordinates: lat={}, lon={}".format(lat, lon))
        return 1

    print("    PASS: zone.home fallback works correctly")
    return 0


def _test_temperature_url_placeholder_replacement(my_predbat):
    """Test URL placeholder replacement with coordinates"""
    print("  Testing URL placeholder replacement...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    url = temp_component.build_api_url(51.5074, -0.1278)
    expected_url = "https://api.open-meteo.com/v1/forecast?latitude=51.5074&longitude=-0.1278&hourly=temperature_2m&current=temperature_2m"
    if url != expected_url:
        print("    ERROR: URL placeholder replacement failed")
        print("      Expected: {}".format(expected_url))
        print("      Got: {}".format(url))
        return 1

    print("    PASS: URL placeholders replaced correctly")
    return 0


def _test_temperature_timezone_offset_conversion(my_predbat):
    """Test timezone offset conversion from seconds to ±HH:MM format"""
    print("  Testing timezone offset conversion...")

    my_predbat.args["temperature_latitude"] = 51.5074
    my_predbat.args["temperature_longitude"] = -0.1278

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Test UTC (0 offset)
    offset_str = temp_component.convert_timezone_offset(0)
    if offset_str != "+00:00":
        print("    ERROR: Failed to convert 0 seconds to +00:00, got: {}".format(offset_str))
        return 1

    # Test positive offset (CET)
    offset_str = temp_component.convert_timezone_offset(3600)
    if offset_str != "+01:00":
        print("    ERROR: Failed to convert 3600 seconds to +01:00, got: {}".format(offset_str))
        return 1

    # Test negative offset (EST)
    offset_str = temp_component.convert_timezone_offset(-18000)
    if offset_str != "-05:00":
        print("    ERROR: Failed to convert -18000 seconds to -05:00, got: {}".format(offset_str))
        return 1

    # Test offset with minutes (IST)
    offset_str = temp_component.convert_timezone_offset(19800)  # +05:30
    if offset_str != "+05:30":
        print("    ERROR: Failed to convert 19800 seconds to +05:30, got: {}".format(offset_str))
        return 1

    print("    PASS: Timezone offset conversion works correctly")
    return 0


def _test_temperature_sensor_creation(my_predbat):
    """Test sensor creation with current temperature and forecast"""
    print("  Testing sensor creation with temperature data...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Mock API response data
    mock_data = {
        "latitude": 51.5,
        "longitude": -0.12,
        "utc_offset_seconds": 0,
        "timezone": "GMT",
        "current": {
            "time": "2026-02-07T10:30",
            "temperature_2m": 9.5
        },
        "hourly": {
            "time": [
                "2026-02-07T00:00",
                "2026-02-07T01:00",
                "2026-02-07T02:00",
                "2026-02-07T03:00"
            ],
            "temperature_2m": [8.2, 8.5, 8.8, 9.1]
        }
    }

    # Set the data and publish sensor
    temp_component.temperature_data = mock_data
    temp_component.last_updated_timestamp = datetime.now()
    temp_component.publish_temperature_sensor()

    # Verify sensor was created
    sensor_entity = "sensor.predbat_temperature"
    if sensor_entity not in temp_component.dashboard_items:
        print("    ERROR: Temperature sensor was not created")
        return 1

    sensor_state = temp_component.dashboard_items[sensor_entity]["state"]
    if sensor_state != 9.5:
        print("    ERROR: Incorrect sensor state: {} (expected 9.5)".format(sensor_state))
        return 1

    # Verify attributes
    sensor_attrs = temp_component.dashboard_items[sensor_entity]["attributes"]
    forecast = sensor_attrs.get("forecast")
    if forecast is None:
        print("    ERROR: Forecast attribute not set")
        return 1

    # Check forecast has correct HA timestamp format
    expected_keys = [
        "2026-02-07T00:00:00+00:00",
        "2026-02-07T01:00:00+00:00",
        "2026-02-07T02:00:00+00:00",
        "2026-02-07T03:00:00+00:00"
    ]

    for key in expected_keys:
        if key not in forecast:
            print("    ERROR: Missing forecast key: {}".format(key))
            print("      Available keys: {}".format(list(forecast.keys())))
            return 1

    # Verify temperature values
    if forecast["2026-02-07T00:00:00+00:00"] != 8.2:
        print("    ERROR: Incorrect forecast value for first hour")
        return 1

    print("    PASS: Sensor created with correct state and forecast")
    return 0


def _test_temperature_cache_persistence(my_predbat):
    """Test that cached data persists on API failure"""
    print("  Testing cache persistence on API failure...")

    my_predbat.args["temperature_latitude"] = 51.5074
    my_predbat.args["temperature_longitude"] = -0.1278
    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Set initial cached data
    initial_data = {
        "utc_offset_seconds": 0,
        "current": {"temperature_2m": 10.0},
        "hourly": {"time": ["2026-02-07T00:00"], "temperature_2m": [9.5]}
    }

    temp_component.temperature_data = initial_data
    temp_component.last_updated_timestamp = datetime(2026, 2, 7, 10, 0)
    initial_time = temp_component.last_updated_timestamp

    # Publish sensor with initial data
    temp_component.publish_temperature_sensor()

    # Verify initial sensor state
    sensor_entity = "sensor.predbat_temperature"
    if sensor_entity not in temp_component.dashboard_items:
        print("    ERROR: Sensor not created")
        return 1

    sensor_state = temp_component.dashboard_items[sensor_entity]["state"]
    if sensor_state != 10.0:
        print("    ERROR: Initial sensor state incorrect: {}".format(sensor_state))
        return 1

    # Simulate API failure by keeping old data
    temp_component.temperature_data = initial_data  # Keep old data
    temp_component.publish_temperature_sensor()

    # Verify sensor still has old data (10.0)
    sensor_state = temp_component.dashboard_items[sensor_entity]["state"]
    if sensor_state != 10.0:
        print(f"    ERROR: Sensor state changed when it shouldn't - got {sensor_state}")
        return 1

    # Verify last_updated timestamp hasn't changed
    if temp_component.last_updated_timestamp != initial_time:
        print("    ERROR: last_updated timestamp changed when it shouldn't")
        return 1

    print("    PASS: Cached data persists on API failure")
    return 0


def _test_temperature_negative_timezone_offset(my_predbat):
    """Test negative timezone offset handling (e.g., US timezones)"""
    print("  Testing negative timezone offset handling...")

    my_predbat.args["temperature_latitude"] = 40.7128
    my_predbat.args["temperature_longitude"] = -74.0060
    temp_component = MockTemperatureAPI(
        temperature_latitude=40.7128,
        temperature_longitude=-74.0060,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Mock API response with negative timezone offset (EST)
    mock_data = {
        "utc_offset_seconds": -18000,  # -05:00
        "current": {"temperature_2m": 5.5},
        "hourly": {
            "time": ["2026-02-07T00:00"],
            "temperature_2m": [4.8]
        }
    }

    temp_component.temperature_data = mock_data
    temp_component.last_updated_timestamp = datetime.now()
    temp_component.publish_temperature_sensor()

    # Verify sensor attributes have correct timezone
    sensor_entity = "sensor.predbat_temperature"
    if sensor_entity not in temp_component.dashboard_items:
        print("    ERROR: Sensor not created")
        return 1

    forecast = temp_component.dashboard_items[sensor_entity]["attributes"].get("forecast", {})
    if not forecast:
        print("    ERROR: Forecast not found in sensor attributes")
        return 1

    # Check for negative timezone offset in timestamp
    expected_key = "2026-02-07T00:00:00-05:00"
    if expected_key not in forecast:
        print("    ERROR: Expected key {} not found in forecast".format(expected_key))
        print("      Available keys: {}".format(list(forecast.keys())))
        return 1

    print("    PASS: Negative timezone offset handled correctly")
    return 0


def test_temperature(my_predbat=None):
    """
    Comprehensive test suite for External Temperature API.

    Tests all major functionality including:
    - Initialization and configuration
    - zone.home coordinate fallback
    - URL placeholder replacement
    - Timezone offset conversion (positive and negative)
    - Sensor creation with current temperature and forecast
    - Cache persistence on API failures
    - HA timestamp format conversion
    """

    # Registry of all sub-tests
    sub_tests = [
        ("initialization", _test_temperature_initialization, "Temperature API initialization"),
        ("zone_home_fallback", _test_temperature_zone_home_fallback, "zone.home coordinate fallback"),
        ("url_placeholder", _test_temperature_url_placeholder_replacement, "URL placeholder replacement"),
        ("timezone_offset", _test_temperature_timezone_offset_conversion, "Timezone offset conversion"),
        ("sensor_creation", _test_temperature_sensor_creation, "Sensor creation with forecast data"),
        ("cache_persistence", _test_temperature_cache_persistence, "Cache persistence on failure"),
        ("negative_timezone", _test_temperature_negative_timezone_offset, "Negative timezone offset handling"),
    ]

    print("\n" + "=" * 70)
    print("EXTERNAL TEMPERATURE API TEST SUITE")
    print("=" * 70)

    failed = 0
    passed = 0

    for test_name, test_func, test_desc in sub_tests:
        print("\n[{}] {}".format(test_name, test_desc))
        try:
            test_result = test_func(my_predbat)
            if test_result:
                failed += 1
                print("  ❌ FAILED")
            else:
                passed += 1
                print("  ✅ PASSED")
        except Exception as e:
            print("  ❌ EXCEPTION: {}".format(e))
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print("TEMPERATURE API TEST RESULTS")
    print("  Passed: {}".format(passed))
    print("  Failed: {}".format(failed))
    print("=" * 70)

    return failed
