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
from unittest.mock import patch, MagicMock, AsyncMock
import aiohttp
import asyncio


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
    results = sensor_attrs.get("results")
    if results is None:
        print("    ERROR: results attribute not set")
        return 1

    # Check forecast has correct HA timestamp format
    expected_keys = [
        "2026-02-07T00:00:00+00:00",
        "2026-02-07T01:00:00+00:00",
        "2026-02-07T02:00:00+00:00",
        "2026-02-07T03:00:00+00:00"
    ]

    for key in expected_keys:
        if key not in results:
            print("    ERROR: Missing results key: {}".format(key))
            print("      Available keys: {}".format(list(results.keys())))
            return 1

    # Verify temperature values
    if results["2026-02-07T00:00:00+00:00"] != 8.2:
        print("    ERROR: Incorrect results value for first hour")
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

    forecast = temp_component.dashboard_items[sensor_entity]["attributes"].get("results", {})
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


def _test_fetch_temperature_data_success(my_predbat):
    """Test successful fetch_temperature_data with valid API response"""
    print("  Testing fetch_temperature_data with successful API response...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Mock API response
    mock_response_data = {
        "latitude": 51.5,
        "longitude": -0.12,
        "utc_offset_seconds": 0,
        "timezone": "GMT",
        "current": {
            "time": "2026-02-07T10:30",
            "temperature_2m": 9.5
        },
        "hourly": {
            "time": ["2026-02-07T00:00", "2026-02-07T01:00"],
            "temperature_2m": [8.2, 8.5]
        }
    }

    async def run_test():
        # Create mock response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_response_data)
        
        # Mock the context manager for response
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        # Mock session.get to return our mock response
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        
        # Mock the context manager for session
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('aiohttp.ClientSession', return_value=mock_session):
            result = await temp_component.fetch_temperature_data()

            if result is None:
                print("    ERROR: fetch_temperature_data returned None")
                return 1

            if result != mock_response_data:
                print("    ERROR: Incorrect data returned")
                return 1

            # Check that success timestamp was updated
            if temp_component._last_updated_time is None:
                print("    ERROR: Success timestamp not updated")
                return 1

            print("    PASS: Successful fetch returns correct data")
            return 0

    return asyncio.run(run_test())


def _test_fetch_temperature_data_http_error_with_retry(my_predbat):
    """Test fetch_temperature_data handles HTTP errors with retry logic"""
    print("  Testing fetch_temperature_data with HTTP error and retry...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    call_count = [0]
    sleep_called = [False]

    async def mock_sleep(seconds):
        sleep_called[0] = True

    def create_mock_get():
        def mock_get_fn(url):
            call_count[0] += 1
            mock_response = MagicMock()
            if call_count[0] < 2:
                # First call fails with 500
                mock_response.status = 500
            else:
                # Second call succeeds
                mock_response.status = 200
                mock_response.json = AsyncMock(return_value={"current": {"temperature_2m": 10.0}})
            
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=None)
            return mock_response
        return mock_get_fn

    async def run_test():
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=create_mock_get())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        # Patch at module level where it's imported
        with patch('temperature.aiohttp.ClientSession', return_value=mock_session):
            with patch('temperature.asyncio.sleep', side_effect=mock_sleep):
                result = await temp_component.fetch_temperature_data()

                if result is None:
                    print("    ERROR: fetch_temperature_data returned None after retry (call_count={})".format(call_count[0]))
                    return 1

                if call_count[0] < 2:
                    print("    ERROR: Retry logic not triggered, call_count={}".format(call_count[0]))
                    return 1

                if not sleep_called[0]:
                    print("    ERROR: asyncio.sleep not called during retry")
                    return 1

                print("    PASS: HTTP error triggers retry and succeeds")
                return 0

    return asyncio.run(run_test())


def _test_fetch_temperature_data_max_retries_exceeded(my_predbat):
    """Test fetch_temperature_data returns None after max retries exceeded"""
    print("  Testing fetch_temperature_data with max retries exceeded...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    call_count = [0]

    def create_mock_response():
        call_count[0] += 1
        mock_response = MagicMock()
        mock_response.status = 503  # Service unavailable
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        return mock_response

    async def run_test():
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=lambda url: create_mock_response())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        initial_failures = temp_component.failures_total

        with patch('temperature.aiohttp.ClientSession', return_value=mock_session):
            with patch('temperature.asyncio.sleep', new_callable=AsyncMock):
                result = await temp_component.fetch_temperature_data()

                if result is not None:
                    print("    ERROR: Expected None after max retries, got data")
                    return 1

                # Should have attempted 3 times
                if call_count[0] != 3:
                    print("    ERROR: Expected 3 retry attempts, got {}".format(call_count[0]))
                    return 1

                # Failures counter should increment
                if temp_component.failures_total != initial_failures + 1:
                    print("    ERROR: failures_total not incremented correctly")
                    return 1

                print("    PASS: Max retries exceeded returns None and increments failure counter")
                return 0

    return asyncio.run(run_test())


def _test_fetch_temperature_data_network_error(my_predbat):
    """Test fetch_temperature_data handles network errors with retry"""
    print("  Testing fetch_temperature_data with network error...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    call_count = [0]

    def create_mock_response():
        call_count[0] += 1
        if call_count[0] < 2:
            # First call raises network error
            raise aiohttp.ClientError("Connection refused")
        else:
            # Second call succeeds
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value={"current": {"temperature_2m": 12.0}})
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=None)
            return mock_response

    async def run_test():
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=lambda url: create_mock_response())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('temperature.aiohttp.ClientSession', return_value=mock_session):
            with patch('temperature.asyncio.sleep', new_callable=AsyncMock):
                result = await temp_component.fetch_temperature_data()

                if result is None:
                    print("    ERROR: Expected data after network error retry")
                    return 1

                if call_count[0] < 2:
                    print("    ERROR: Retry not triggered after network error")
                    return 1

                print("    PASS: Network error triggers retry and succeeds")
                return 0

    return asyncio.run(run_test())


def _test_fetch_temperature_data_timeout_error(my_predbat):
    """Test fetch_temperature_data handles timeout errors with retry"""
    print("  Testing fetch_temperature_data with timeout error...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    call_count = [0]

    def create_mock_response():
        call_count[0] += 1
        if call_count[0] < 2:
            raise asyncio.TimeoutError("Request timed out")
        else:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value={"current": {"temperature_2m": 11.5}})
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=None)
            return mock_response

    async def run_test():
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=lambda url: create_mock_response())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('temperature.aiohttp.ClientSession', return_value=mock_session):
            with patch('temperature.asyncio.sleep', new_callable=AsyncMock):
                result = await temp_component.fetch_temperature_data()

                if result is None:
                    print("    ERROR: Expected data after timeout retry")
                    return 1

                if call_count[0] < 2:
                    print("    ERROR: Retry not triggered after timeout")
                    return 1

                print("    PASS: Timeout error triggers retry and succeeds")
                return 0

    return asyncio.run(run_test())


def _test_fetch_temperature_data_missing_coordinates(my_predbat):
    """Test fetch_temperature_data returns None when coordinates missing"""
    print("  Testing fetch_temperature_data with missing coordinates...")

    # Initialize without coordinates
    temp_component = MockTemperatureAPI(
        temperature_latitude=None,
        temperature_longitude=None,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    # Don't set zone.home either

    async def run_test():
        result = await temp_component.fetch_temperature_data()

        if result is not None:
            print("    ERROR: Expected None when coordinates missing")
            return 1

        print("    PASS: Returns None when coordinates missing")
        return 0

    return asyncio.run(run_test())


def _test_fetch_temperature_data_exponential_backoff(my_predbat):
    """Test that exponential backoff is used between retries"""
    print("  Testing exponential backoff between retries...")

    temp_component = MockTemperatureAPI(
        temperature_latitude=51.5074,
        temperature_longitude=-0.1278,
        temperature_url="https://api.open-meteo.com/v1/forecast?latitude=LATITUDE&longitude=LONGITUDE&hourly=temperature_2m&current=temperature_2m"
    )

    sleep_times = []

    async def mock_sleep(seconds):
        sleep_times.append(seconds)

    call_count = [0]

    def create_mock_response():
        call_count[0] += 1
        raise aiohttp.ClientError("Network error")

    async def run_test():
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=lambda url: create_mock_response())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch('temperature.aiohttp.ClientSession', return_value=mock_session):
            with patch('temperature.asyncio.sleep', side_effect=mock_sleep):
                result = await temp_component.fetch_temperature_data()

                # Should have 2 sleep calls (after 1st and 2nd attempts)
                if len(sleep_times) != 2:
                    print("    ERROR: Expected 2 sleep calls, got {}".format(len(sleep_times)))
                    return 1

                # Check exponential backoff: 2^0=1, 2^1=2
                if sleep_times[0] != 1:
                    print("    ERROR: First sleep should be 1 second, got {}".format(sleep_times[0]))
                    return 1

                if sleep_times[1] != 2:
                    print("    ERROR: Second sleep should be 2 seconds, got {}".format(sleep_times[1]))
                    return 1

                print("    PASS: Exponential backoff implemented correctly")
                return 0

    return asyncio.run(run_test())


def _test_run_disabled(my_predbat=None):
    """Test run method when temperature_enable is False"""

    def run_test():
        temp_api = MockTemperatureAPI(
            temperature_latitude=51.5074,
            temperature_longitude=-0.1278,
            temperature_url="http://example.com/api"
        )
        temp_api.temperature_enable = False
        temp_api.fetch_temperature_data = MagicMock()

        result = asyncio.run(temp_api.run(seconds=0, first=True))

        if not result:
            print("    ERROR: run should return True when disabled")
            return 1

        if temp_api.fetch_temperature_data.called:
            print("    ERROR: fetch_temperature_data should not be called when disabled")
            return 1

        print("    PASS: run returns True without fetching when disabled")
        return 0

    return run_test()


def _test_run_first_fetch(my_predbat=None):
    """Test run method on first run (first=True)"""

    def run_test():
        async def async_test():
            temp_api = MockTemperatureAPI(
                temperature_latitude=51.5074,
                temperature_longitude=-0.1278,
                temperature_url="http://example.com/api"
            )
            temp_api.temperature_enable = True
            temp_api.temperature_data = None
            temp_api.publish_temperature_sensor = MagicMock()
            temp_api.update_success_timestamp = MagicMock()

            # Mock fetch to return data
            mock_data = {"temperature": 15.5, "forecast": []}
            with patch.object(temp_api, 'fetch_temperature_data', new_callable=AsyncMock, return_value=mock_data):
                result = await temp_api.run(seconds=1800, first=True)  # Not at hourly boundary

                if not result:
                    print("    ERROR: run should return True")
                    return 1

                if temp_api.temperature_data != mock_data:
                    print("    ERROR: temperature_data should be updated")
                    return 1

                if temp_api.last_updated_timestamp is None:
                    print("    ERROR: last_updated_timestamp should be set")
                    return 1

                if temp_api.publish_temperature_sensor.call_count != 2:
                    print("    ERROR: publish_temperature_sensor should be called twice (after fetch and after success)")
                    return 1

                if not temp_api.update_success_timestamp.called:
                    print("    ERROR: update_success_timestamp should be called")
                    return 1

                print("    PASS: first run fetches and publishes data")
                return 0

        return asyncio.run(async_test())

    return run_test()


def _test_run_hourly_interval(my_predbat=None):
    """Test run method at hourly interval (seconds % 3600 == 0)"""

    def run_test():
        async def async_test():
            temp_api = MockTemperatureAPI(
                temperature_latitude=51.5074,
                temperature_longitude=-0.1278,
                temperature_url="http://example.com/api"
            )
            temp_api.temperature_enable = True
            temp_api.temperature_data = {"temperature": 10.0}
            temp_api.publish_temperature_sensor = MagicMock()
            temp_api.update_success_timestamp = MagicMock()

            mock_data = {"temperature": 15.5, "forecast": []}
            with patch.object(temp_api, 'fetch_temperature_data', new_callable=AsyncMock, return_value=mock_data):
                result = await temp_api.run(seconds=3600, first=False)  # Exactly 1 hour

                if not result:
                    print("    ERROR: run should return True")
                    return 1

                if temp_api.temperature_data != mock_data:
                    print("    ERROR: temperature_data should be updated at hourly interval")
                    return 1

                print("    PASS: run fetches data at hourly interval")
                return 0

        return asyncio.run(async_test())

    return run_test()


def _test_run_no_fetch_between_hours(my_predbat=None):
    """Test run method does not fetch between hourly intervals"""

    def run_test():
        async def async_test():
            temp_api = MockTemperatureAPI(
                temperature_latitude=51.5074,
                temperature_longitude=-0.1278,
                temperature_url="http://example.com/api"
            )
            temp_api.temperature_enable = True
            temp_api.temperature_data = {"temperature": 10.0}
            temp_api.publish_temperature_sensor = MagicMock()
            temp_api.update_success_timestamp = MagicMock()

            fetch_called = False

            async def mock_fetch():
                nonlocal fetch_called
                fetch_called = True
                return {"temperature": 20.0}

            with patch.object(temp_api, 'fetch_temperature_data', new_callable=AsyncMock, side_effect=mock_fetch):
                result = await temp_api.run(seconds=1800, first=False)  # 30 minutes

                if not result:
                    print("    ERROR: run should return True")
                    return 1

                if fetch_called:
                    print("    ERROR: fetch should not be called between hourly intervals")
                    return 1

                if not temp_api.publish_temperature_sensor.called:
                    print("    ERROR: publish_temperature_sensor should still be called for existing data")
                    return 1

                if not temp_api.update_success_timestamp.called:
                    print("    ERROR: update_success_timestamp should be called for existing data")
                    return 1

                print("    PASS: run does not fetch between hourly intervals but still publishes")
                return 0

        return asyncio.run(async_test())

    return run_test()


def _test_run_fetch_returns_none(my_predbat=None):
    """Test run method when fetch returns None"""

    def run_test():
        async def async_test():
            temp_api = MockTemperatureAPI(
                temperature_latitude=51.5074,
                temperature_longitude=-0.1278,
                temperature_url="http://example.com/api"
            )
            temp_api.temperature_enable = True
            temp_api.temperature_data = None
            temp_api.publish_temperature_sensor = MagicMock()
            temp_api.update_success_timestamp = MagicMock()

            with patch.object(temp_api, 'fetch_temperature_data', new_callable=AsyncMock, return_value=None):
                result = await temp_api.run(seconds=0, first=True)

                if not result:
                    print("    ERROR: run should return True even when fetch returns None")
                    return 1

                if temp_api.temperature_data is not None:
                    print("    ERROR: temperature_data should remain None")
                    return 1

                if temp_api.publish_temperature_sensor.called:
                    print("    ERROR: publish_temperature_sensor should not be called when no data")
                    return 1

                if temp_api.update_success_timestamp.called:
                    print("    ERROR: update_success_timestamp should not be called when no data")
                    return 1

                print("    PASS: run handles None return from fetch gracefully")
                return 0

        return asyncio.run(async_test())

    return run_test()


def _test_run_exception_handling(my_predbat=None):
    """Test run method exception handling"""

    def run_test():
        async def async_test():
            temp_api = MockTemperatureAPI(
                temperature_latitude=51.5074,
                temperature_longitude=-0.1278,
                temperature_url="http://example.com/api"
            )
            temp_api.temperature_enable = True
            temp_api.temperature_data = {"temperature": 10.0}
            temp_api.publish_temperature_sensor = MagicMock()
            temp_api.update_success_timestamp = MagicMock()
            temp_api.log = MagicMock()

            # Mock fetch to raise exception
            with patch.object(temp_api, 'fetch_temperature_data', new_callable=AsyncMock, side_effect=Exception("API error")):
                result = await temp_api.run(seconds=0, first=True)

                if not result:
                    print("    ERROR: run should return True even on exception")
                    return 1

                if not temp_api.log.called:
                    print("    ERROR: log should be called with warning")
                    return 1

                log_message = temp_api.log.call_args[0][0]
                if "Exception in run loop" not in log_message:
                    print("    ERROR: log should mention exception in run loop")
                    return 1

                if not temp_api.publish_temperature_sensor.called:
                    print("    ERROR: publish_temperature_sensor should be called to keep publishing old data")
                    return 1

                print("    PASS: run handles exceptions gracefully and keeps publishing old data")
                return 0

        return asyncio.run(async_test())

    return run_test()


def _test_run_exception_no_data(my_predbat=None):
    """Test run method exception handling when no temperature data exists"""

    def run_test():
        async def async_test():
            temp_api = MockTemperatureAPI(
                temperature_latitude=51.5074,
                temperature_longitude=-0.1278,
                temperature_url="http://example.com/api"
            )
            temp_api.temperature_enable = True
            temp_api.temperature_data = None
            temp_api.publish_temperature_sensor = MagicMock()
            temp_api.log = MagicMock()

            # Mock fetch to raise exception
            with patch.object(temp_api, 'fetch_temperature_data', new_callable=AsyncMock, side_effect=Exception("API error")):
                result = await temp_api.run(seconds=0, first=True)

                if not result:
                    print("    ERROR: run should return True even on exception with no data")
                    return 1

                if not temp_api.log.called:
                    print("    ERROR: log should be called with warning")
                    return 1

                if temp_api.publish_temperature_sensor.called:
                    print("    ERROR: publish_temperature_sensor should not be called when no data exists")
                    return 1

                print("    PASS: run handles exception with no data gracefully")
                return 0

        return asyncio.run(async_test())

    return run_test()


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
        ("fetch_success", _test_fetch_temperature_data_success, "fetch_temperature_data successful API call"),
        ("fetch_http_error", _test_fetch_temperature_data_http_error_with_retry, "fetch_temperature_data HTTP error retry"),
        ("fetch_max_retries", _test_fetch_temperature_data_max_retries_exceeded, "fetch_temperature_data max retries exceeded"),
        ("fetch_network_error", _test_fetch_temperature_data_network_error, "fetch_temperature_data network error retry"),
        ("fetch_timeout", _test_fetch_temperature_data_timeout_error, "fetch_temperature_data timeout retry"),
        ("fetch_no_coords", _test_fetch_temperature_data_missing_coordinates, "fetch_temperature_data missing coordinates"),
        ("fetch_backoff", _test_fetch_temperature_data_exponential_backoff, "fetch_temperature_data exponential backoff"),
        ("run_disabled", _test_run_disabled, "run method when disabled"),
        ("run_first", _test_run_first_fetch, "run method on first run"),
        ("run_hourly", _test_run_hourly_interval, "run method at hourly interval"),
        ("run_between_hours", _test_run_no_fetch_between_hours, "run method between hourly intervals"),
        ("run_fetch_none", _test_run_fetch_returns_none, "run method when fetch returns None"),
        ("run_exception", _test_run_exception_handling, "run method exception handling with data"),
        ("run_exception_nodata", _test_run_exception_no_data, "run method exception handling without data"),
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
