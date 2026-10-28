# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt: on

from carbon import CarbonAPI
import aiohttp
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from const import TIME_FORMAT_HA
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockCarbonAPI(CarbonAPI):
    """Mock CarbonAPI class for testing without ComponentBase dependencies"""

    def __init__(self):
        # Don't call parent __init__ to avoid ComponentBase
        self.postcode = "BS16 1AB"
        self.automatic = False
        self.failures_total = 0
        self.last_updated_timestamp = None
        self.carbon_data_points = []
        self.dashboard_items = {}
        self.log_messages = []
        self.config_args = {}
        self.prefix = "predbat"
        self._now_utc = datetime.now(timezone.utc)
        self._last_updated_time = None

    @property
    def now_utc(self):
        return self._now_utc

    def log(self, message):
        self.log_messages.append(message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes, "app": app}

    def update_success_timestamp(self):
        self._last_updated_time = datetime.now(timezone.utc)

    def last_updated_time(self):
        return self._last_updated_time

    def set_arg(self, arg, value):
        """Mock set_arg for automatic_config"""
        self.config_args[arg] = value


# Mock API response data (minimal structure from real API)
MOCK_CARBON_API_RESPONSE = {
    "data": {
        "regionid": 11,
        "dnoregion": "WPD South West",
        "shortname": "South West England",
        "postcode": "BS16",
        "data": [
            {"from": "2025-12-20T14:00Z", "to": "2025-12-20T14:30Z", "intensity": {"forecast": 265, "index": "very high"}},
            {"from": "2025-12-20T14:30Z", "to": "2025-12-20T15:00Z", "intensity": {"forecast": 232, "index": "high"}},
            {"from": "2025-12-20T15:00Z", "to": "2025-12-20T15:30Z", "intensity": {"forecast": 292, "index": "very high"}},
        ],
    }
}

MOCK_CARBON_API_RESPONSE_EMPTY = {"data": {"regionid": 11, "dnoregion": "WPD South West", "shortname": "South West England", "postcode": "BS16", "data": []}}


def test_carbon(my_predbat=None):
    """
    Comprehensive test suite for Carbon Intensity API.

    Tests all major functionality including:
    - Initialisation and configuration
    - Data fetching (success, errors, timeouts)
    - Caching behavior
    - Data publishing
    - Time/date handling
    - Automatic configuration flow
    """

    # Registry of all sub-tests
    sub_tests = [
        ("initialisation", _test_carbon_initialization, "Carbon API initialisation"),
        ("fetch_success", _test_fetch_carbon_data_success, "Fetch carbon data success"),
        ("http_error", _test_fetch_carbon_data_http_error, "HTTP error handling (404, 500)"),
        ("timeout", _test_fetch_carbon_data_timeout, "Timeout handling"),
        ("json_error", _test_fetch_carbon_data_json_error, "JSON parsing error"),
        ("empty_data", _test_fetch_carbon_data_empty, "Empty data response"),
        ("cache_skip", _test_fetch_carbon_data_cache_skip, "Cache skip (<4 hours)"),
        ("cache_refresh", _test_fetch_carbon_data_cache_refresh, "Cache refresh (>4 hours)"),
        ("publish_current", _test_publish_carbon_data_current, "Publish current intensity"),
        ("publish_forecast", _test_publish_carbon_data_forecast, "Publish forecast data"),
        ("publish_unknown", _test_publish_carbon_data_unknown, "Publish unknown state"),
        ("postcode_strip", _test_postcode_stripping, "Postcode stripping"),
        ("single_fetch", _test_single_fw48h_fetch, "Single fw48h fetch in ISO8601 format"),
        ("time_format", _test_time_format_conversion, "Time format conversion"),
        ("timezone", _test_timezone_handling, "Timezone handling"),
        ("json_collection", _test_json_data_collection, "JSON data collection"),
        ("failure_counter", _test_failure_counter, "Failure counter"),
        ("run_first", _test_run_first_call, "run() first call"),
        ("run_interval", _test_run_15min_interval, "run() 15-minute interval"),
        ("auto_config", _test_automatic_config_flow, "Automatic config flow"),
        ("replicate_forward", _test_carbon_replicate_extends_forward, "Replicate forecast forward when data runs out"),
        ("replicate_no_data", _test_carbon_replicate_no_data, "Replicate with no data at all"),
        ("replicate_full", _test_carbon_replicate_full_coverage, "Replicate with full coverage"),
        ("replicate_gap", _test_carbon_replicate_gap, "Replicate fills a gap in the forecast"),
    ]

    print("\n" + "=" * 70)
    print("CARBON INTENSITY API TEST SUITE")
    print("=" * 70)

    failed = 0
    passed = 0

    for test_name, test_func, test_desc in sub_tests:
        print(f"\n[{test_name}] {test_desc}")
        print("-" * 70)
        try:
            test_result = test_func(my_predbat)
            if test_result:
                print(f"✗ FAILED: {test_name}")
                failed += 1
            else:
                print(f"✓ PASSED: {test_name}")
                passed += 1
        except Exception as e:
            print(f"✗ EXCEPTION in {test_name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed


# =============================================================================
# Phase 1: Core Functionality Tests (8 tests)
# =============================================================================


def _test_carbon_initialization(my_predbat=None):
    """Test CarbonAPI initialisation"""
    failed = 0

    api = MockCarbonAPI()
    api.postcode = "SW1A 1AA"
    api.automatic = True

    if api.postcode != "SW1A 1AA":
        print("  ✗ ERROR: Postcode not set correctly")
        failed = 1

    if api.automatic is not True:
        print("  ✗ ERROR: Automatic flag not set correctly")
        failed = 1

    if api.failures_total != 0:
        print("  ✗ ERROR: failures_total should be 0")
        failed = 1

    if api.carbon_data_points != []:
        print("  ✗ ERROR: carbon_data_points should be empty list")
        failed = 1

    if not failed:
        print("  ✓ CarbonAPI initialised correctly")
    return failed


def _test_fetch_carbon_data_success(my_predbat=None):
    """Test successful carbon data fetch"""
    failed = 0

    api = MockCarbonAPI()
    api.postcode = "BS16"

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Check data was collected (3 points from the single fw48h call)
        if len(api.carbon_data_points) != 3:
            print(f"ERROR: Expected 3 data points, got {len(api.carbon_data_points)}")
            return 1

        # Check data format
        first_point = api.carbon_data_points[0]
        if "from" not in first_point or "to" not in first_point or "intensity" not in first_point:
            print("ERROR: Data point missing required fields")
            return 1

        # Check time format conversion (should be TIME_FORMAT_HA: full datetime with timezone)
        if not first_point["from"].startswith("2025-12-20T14:00:00"):
            print(f"ERROR: Expected datetime format starting with '2025-12-20T14:00:00', got '{first_point['from']}'")
            return 1

        # Check intensity value
        if first_point["intensity"] != 265:
            print(f"ERROR: Expected intensity 265, got {first_point['intensity']}")
            return 1

    print("  ✓ Carbon data fetched successfully")
    return failed


def _test_fetch_carbon_data_http_error(my_predbat=None):
    """Test HTTP error handling (404, 500, etc.)"""
    print("Test: Carbon API HTTP error handling")

    api = MockCarbonAPI()
    api.postcode = "BS16"
    initial_failures = api.failures_total

    mock_response = create_aiohttp_mock_response(status=404)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Should increment failures_total once for the single API call
        if api.failures_total != initial_failures + 1:
            print(f"ERROR: Expected failures_total to increment by 1, got {api.failures_total}")
            return 1

        # Should not have collected any data
        if len(api.carbon_data_points) != 0:
            print(f"ERROR: Should not have collected data on HTTP error")
            return 1

    print("  ✓ HTTP error handled correctly")
    return 0


def _test_fetch_carbon_data_timeout(my_predbat=None):
    """Test request timeout handling"""
    print("Test: Carbon API timeout handling")

    api = MockCarbonAPI()
    api.postcode = "BS16"
    initial_failures = api.failures_total

    mock_session = create_aiohttp_mock_session(exception=aiohttp.ClientError("Connection timeout"))
    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Should increment failures_total once for the single API call
        if api.failures_total != initial_failures + 1:
            print(f"ERROR: Expected failures_total to increment by 1, got {api.failures_total}")
            return 1

        # Should not have collected any data
        if len(api.carbon_data_points) != 0:
            print(f"ERROR: Should not have collected data on timeout")
            return 1

    print("  ✓ Timeout handled correctly")
    return 0


def _test_fetch_carbon_data_json_error(my_predbat=None):
    """Test JSON parsing error handling"""
    print("Test: Carbon API JSON parsing error")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    mock_response = create_aiohttp_mock_response(status=200, json_exception=ValueError("Invalid JSON"))
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Should log warning about JSON parsing
        has_json_error_log = any("Failed to parse JSON" in msg for msg in api.log_messages)
        if not has_json_error_log:
            print("ERROR: Should log JSON parsing error")
            return 1

        # Should not have collected any data
        if len(api.carbon_data_points) != 0:
            print(f"ERROR: Should not have collected data on JSON error")
            return 1

    print("  ✓ JSON error handled correctly")
    return 0


def _test_fetch_carbon_data_empty(my_predbat=None):
    """Test empty data response handling"""
    print("Test: Carbon API empty data response")

    api = MockCarbonAPI()
    api.postcode = "BS16"
    initial_failures = api.failures_total

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE_EMPTY)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Should increment failures_total once for the single API call
        if api.failures_total != initial_failures + 1:
            print(f"ERROR: Expected failures_total to increment by 1, got {api.failures_total}")
            return 1

        # Should log warning about no data points
        has_empty_log = any("No data points found" in msg for msg in api.log_messages)
        if not has_empty_log:
            print("ERROR: Should log warning about empty data")
            return 1

    print("  ✓ Empty data handled correctly")
    return 0


def _test_fetch_carbon_data_cache_skip(my_predbat=None):
    """Test cache skip when data is less than 4 hours old"""
    print("Test: Carbon API cache skip (<4 hours)")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    # Set last updated to 2 hours ago
    api.last_updated_timestamp = datetime.now(timezone.utc) - timedelta(hours=2)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        run_async(api.fetch_carbon_data())

        # Should NOT make any API calls
        if mock_session_class.call_count != 0:
            print(f"ERROR: Expected 0 API calls (cache), got {mock_session_class.call_count}")
            return 1

    print("  ✓ Cache skip working correctly")
    return 0


def _test_fetch_carbon_data_cache_refresh(my_predbat=None):
    """Test cache refresh when data is more than 4 hours old"""
    print("Test: Carbon API cache refresh (>4 hours)")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    # Set last updated to 5 hours ago
    api.last_updated_timestamp = datetime.now(timezone.utc) - timedelta(hours=5)

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Should make the API call (cache expired)
        if mock_session.get.call_count != 1:
            print(f"ERROR: Expected 1 API call (cache expired), got {mock_session.get.call_count}")
            return 1

        # Should have collected data
        if len(api.carbon_data_points) == 0:
            print("ERROR: Should have collected data after cache refresh")
            return 1

    print("  ✓ Cache refresh working correctly")
    return 0


# =============================================================================
# Phase 2: Data Publishing Tests (3 tests)
# =============================================================================


def _test_publish_carbon_data_current(my_predbat=None):
    """Test publishing current carbon intensity"""
    print("Test: Carbon API publish current intensity")

    api = MockCarbonAPI()

    # Set current time to match one of the data points
    api._now_utc = datetime(2025, 12, 20, 14, 15, 0, tzinfo=timezone.utc)  # 14:15 UTC

    # Manually set carbon data points
    api.carbon_data_points = [{"from": "2025-12-20T14:00:00+0000", "to": "2025-12-20T14:30:00+0000", "intensity": 265}, {"from": "2025-12-20T14:30:00+0000", "to": "2025-12-20T15:00:00+0000", "intensity": 232}]

    # Mock datetime.now() in publish_carbon_data
    with patch("carbon.datetime") as mock_datetime:
        mock_datetime.now.return_value = api._now_utc
        mock_datetime.strptime = datetime.strptime

        api.publish_carbon_data()

        # Check dashboard item was created
        entity_id = "sensor.predbat_carbon_intensity"
        if entity_id not in api.dashboard_items:
            print(f"ERROR: Dashboard item '{entity_id}' not created")
            return 1

        item = api.dashboard_items[entity_id]

        # Check current value matches the time window
        if item["state"] != 265:
            print(f"ERROR: Expected state 265, got {item['state']}")
            return 1

        # Check attributes
        if "attributes" not in item:
            print("ERROR: Dashboard item missing attributes")
            return 1

        attrs = item["attributes"]
        if attrs.get("unit_of_measurement") != "gCO2/kWh":
            print(f"ERROR: Expected unit 'gCO2/kWh', got '{attrs.get('unit_of_measurement')}'")
            return 1

        if attrs.get("forecast") != api.carbon_data_points:
            print("ERROR: Forecast attribute doesn't match data points")
            return 1

    print("  ✓ Current intensity published correctly")
    return 0


def _test_publish_carbon_data_forecast(my_predbat=None):
    """Test forecast data in attributes"""
    print("Test: Carbon API publish forecast attribute")

    api = MockCarbonAPI()

    # Set carbon data points
    api.carbon_data_points = [
        {"from": "2025-12-20T14:00:00+0000", "to": "2025-12-20T14:30:00+0000", "intensity": 265},
        {"from": "2025-12-20T14:30:00+0000", "to": "2025-12-20T15:00:00+0000", "intensity": 232},
        {"from": "2025-12-20T15:00:00+0000", "to": "2025-12-20T15:30:00+0000", "intensity": 292},
    ]

    with patch("carbon.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2025, 12, 20, 14, 15, 0, tzinfo=timezone.utc)
        mock_datetime.strptime = datetime.strptime

        api.publish_carbon_data()

        entity_id = "sensor.predbat_carbon_intensity"
        item = api.dashboard_items[entity_id]

        # Check forecast is a list with correct structure
        forecast = item["attributes"]["forecast"]
        if not isinstance(forecast, list):
            print("ERROR: Forecast should be a list")
            return 1

        if len(forecast) != 3:
            print(f"ERROR: Expected 3 forecast items, got {len(forecast)}")
            return 1

        # Check first forecast item structure
        first_item = forecast[0]
        if "from" not in first_item or "to" not in first_item or "intensity" not in first_item:
            print("ERROR: Forecast item missing required fields")
            return 1

        if not first_item["from"].startswith("2025-12-20T14:00:00"):
            print(f"ERROR: Expected from to start with '2025-12-20T14:00:00', got '{first_item['from']}'")
            return 1

        if first_item["intensity"] != 265:
            print(f"ERROR: Expected intensity=265, got {first_item['intensity']}")
            return 1

    print("  ✓ Forecast attribute published correctly")
    return 0


def _test_publish_carbon_data_unknown(my_predbat=None):
    """Test unknown state when no current data matches"""
    print("Test: Carbon API publish unknown state")

    api = MockCarbonAPI()

    # Set current time outside any data point range
    api._now_utc = datetime(2025, 12, 20, 20, 0, 0, tzinfo=timezone.utc)  # 20:00 UTC

    # Set carbon data points that don't include current time
    api.carbon_data_points = [{"from": "2025-12-20T14:00:00+0000", "to": "2025-12-20T14:30:00+0000", "intensity": 265}, {"from": "2025-12-20T14:30:00+0000", "to": "2025-12-20T15:00:00+0000", "intensity": 232}]

    with patch("carbon.datetime") as mock_datetime:
        mock_datetime.now.return_value = api._now_utc
        mock_datetime.strptime = datetime.strptime

        api.publish_carbon_data()

        entity_id = "sensor.predbat_carbon_intensity"
        item = api.dashboard_items[entity_id]

        # State should be "unknown"
        if item["state"] != "unknown":
            print(f"ERROR: Expected state 'unknown', got '{item['state']}'")
            return 1

    print("  ✓ Unknown state handled correctly")
    return 0


# =============================================================================
# Phase 3: Edge Cases Tests (6 tests)
# =============================================================================


def _test_postcode_stripping(my_predbat=None):
    """Test postcode space stripping"""
    print("Test: Carbon API postcode stripping")

    api = MockCarbonAPI()
    api.postcode = "BS16 1AB"  # Full postcode with space

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # Check that API was called with stripped postcode
        calls = mock_session.get.call_args_list
        if len(calls) > 0:
            first_call_url = calls[0][0][0]
            if "BS16 1AB" in first_call_url:
                print("ERROR: Postcode should be stripped (no space)")
                return 1
            if "BS16" not in first_call_url:
                print("ERROR: Stripped postcode 'BS16' not found in URL")
                return 1

    print("  ✓ Postcode stripping working correctly")
    return 0


def _test_single_fw48h_fetch(my_predbat=None):
    """Test a single fw48h request is made from now in the ISO8601 format the API specifies"""
    print("Test: Carbon API single fw48h fetch")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        with patch("carbon.datetime") as mock_datetime:
            # Mock datetime.now() to return fixed time with timezone
            fixed_now = datetime(2025, 12, 20, 14, 0, 0, tzinfo=timezone.utc)
            mock_datetime.now.return_value = fixed_now
            mock_datetime.strptime = datetime.strptime
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

            run_async(api.fetch_carbon_data())

            # fw48h from now covers the whole published forecast horizon, so one call is enough
            if mock_session.get.call_count != 1:
                print(f"ERROR: Expected 1 API call, got {mock_session.get.call_count}")
                return 1

            url = mock_session.get.call_args_list[0][0][0]

            # The API specifies a YYYY-MM-DDThh:mmZ datetime, a bare date is not a documented input
            expected_url = "https://api.carbonintensity.org.uk/regional/intensity/2025-12-20T14:00Z/fw48h/postcode/BS16"
            if url != expected_url:
                print(f"ERROR: Expected URL '{expected_url}', got '{url}'")
                return 1

            # The request must be built in UTC, a local date is a day out around midnight under BST
            if mock_datetime.now.call_args[0] != (timezone.utc,):
                print(f"ERROR: Expected datetime.now(timezone.utc), got now{mock_datetime.now.call_args[0]}")
                return 1

    print("  ✓ Single fw48h fetch working correctly")
    return 0


def _test_time_format_conversion(my_predbat=None):
    """Test time format conversion from Carbon API to HA format"""
    print("Test: Carbon API time format conversion")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    # Response with specific time format
    mock_response_data = {"data": {"regionid": 11, "postcode": "BS16", "data": [{"from": "2025-12-20T14:00Z", "to": "2025-12-20T14:30Z", "intensity": {"forecast": 265}}]}}  # TIME_FORMAT_CARBON

    mock_response = create_aiohttp_mock_response(status=200, json_data=mock_response_data)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        if len(api.carbon_data_points) == 0:
            print("ERROR: No data points collected")
            return 1

        # Check converted format (should be TIME_FORMAT_HA: full datetime with timezone)
        point = api.carbon_data_points[0]
        if not point["from"].startswith("2025-12-20T14:00:00"):
            print(f"ERROR: Expected from to start with '2025-12-20T14:00:00', got '{point['from']}'")
            return 1

        if not point["to"].startswith("2025-12-20T14:30:00"):
            print(f"ERROR: Expected to to start with '2025-12-20T14:30:00', got '{point['to']}'")
            return 1

    print("  ✓ Time format conversion working correctly")
    return 0


def _test_timezone_handling(my_predbat=None):
    """Test UTC timezone preservation"""
    print("Test: Carbon API timezone handling")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    mock_response_data = {"data": {"regionid": 11, "postcode": "BS16", "data": [{"from": "2025-12-20T14:00Z", "to": "2025-12-20T14:30Z", "intensity": {"forecast": 265}}]}}

    mock_response = create_aiohttp_mock_response(status=200, json_data=mock_response_data)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        # The internal parsing should preserve UTC timezone
        # We can verify this by checking the stored data format is correct
        if len(api.carbon_data_points) == 0:
            print("ERROR: No data points collected")
            return 1

        point = api.carbon_data_points[0]

        # Parse back the stored time and verify it can be parsed with timezone
        try:
            parsed_time = datetime.strptime(point["from"], TIME_FORMAT_HA)
            if parsed_time.hour != 14 or parsed_time.minute != 0:
                print(f"ERROR: Time not preserved correctly: {parsed_time}")
                return 1
            # Check timezone is included
            if "+0000" not in point["from"] and "Z" not in point["from"]:
                print(f"ERROR: Timezone not preserved in stored time: {point['from']}")
                return 1
        except Exception as e:
            print(f"ERROR: Failed to parse stored time: {e}")
            return 1

    print("  ✓ Timezone handling working correctly")
    return 0


def _test_json_data_collection(my_predbat=None):
    """Test every data point in the API response is collected"""
    print("Test: Carbon API JSON data collection")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    response = {
        "data": {
            "regionid": 11,
            "postcode": "BS16",
            "data": [
                {"from": "2025-12-20T14:00Z", "to": "2025-12-20T14:30Z", "intensity": {"forecast": 265}},
                {"from": "2025-12-20T14:30Z", "to": "2025-12-20T15:00Z", "intensity": {"forecast": 232}},
                {"from": "2025-12-20T15:00Z", "to": "2025-12-20T15:30Z", "intensity": {"forecast": 300}},
            ],
        }
    }

    mock_response = create_aiohttp_mock_response(status=200, json_data=response)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())

        if len(api.carbon_data_points) != 3:
            print(f"ERROR: Expected 3 data points, got {len(api.carbon_data_points)}")
            return 1

        # Points must be kept in the order the API returned them
        intensities = [point["intensity"] for point in api.carbon_data_points]
        if intensities != [265, 232, 300]:
            print(f"ERROR: Expected intensities [265, 232, 300], got {intensities}")
            return 1

    print("  ✓ Data collection from the API response working correctly")
    return 0


def _test_failure_counter(my_predbat=None):
    """Test failures_total counter increments correctly"""
    print("Test: Carbon API failure counter")

    api = MockCarbonAPI()
    api.postcode = "BS16"
    api.failures_total = 0

    # Simulate multiple failure scenarios
    mock_response_404 = create_aiohttp_mock_response(status=404)
    mock_session = create_aiohttp_mock_session(mock_response_404)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())
        failures_after_404 = api.failures_total

        if failures_after_404 != 1:  # 1 API call, which fails
            print(f"ERROR: Expected failures_total=1 after HTTP errors, got {failures_after_404}")
            return 1

    # Test with timeout
    mock_session = create_aiohttp_mock_session(exception=aiohttp.ClientError("Timeout"))
    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.fetch_carbon_data())
        failures_after_timeout = api.failures_total

        if failures_after_timeout != 2:  # Previous 1 + new 1
            print(f"ERROR: Expected failures_total=2 after timeout, got {failures_after_timeout}")
            return 1

    print("  ✓ Failure counter working correctly")
    return 0


# =============================================================================
# Phase 4: Integration Tests (3 tests)
# =============================================================================


def _test_run_first_call(my_predbat=None):
    """Test run() calls fetch on first run"""
    print("Test: Carbon API run() first call")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        # Call run with first=True
        result = run_async(api.run(seconds=0, first=True))

        if result is not True:
            print(f"ERROR: run() should return True")
            return 1

        # Should have made API calls
        if mock_session.get.call_count != 1:
            print(f"ERROR: Expected 1 API call on first run, got {mock_session.get.call_count}")
            return 1

    print("  ✓ First run calling fetch correctly")
    return 0


def _test_run_15min_interval(my_predbat=None):
    """Test run() calls fetch every 15 minutes"""
    print("Test: Carbon API run() 15-minute interval")

    api = MockCarbonAPI()
    api.postcode = "BS16"

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        # Call at 15-minute interval (900 seconds)
        run_async(api.run(seconds=900, first=False))

        if mock_session.get.call_count != 1:
            print(f"ERROR: Expected 1 API call at 15-min interval, got {mock_session.get.call_count}")
            return 1

        mock_session.get.reset_mock()

        # Call at non-15-minute interval (300 seconds = 5 minutes)
        run_async(api.run(seconds=300, first=False))

        if mock_session.get.call_count != 0:
            print(f"ERROR: Expected 0 API calls at non-15-min interval, got {mock_session.get.call_count}")
            return 1

    print("  ✓ 15-minute interval working correctly")
    return 0


def _test_automatic_config_flow(my_predbat=None):
    """Test automatic_config flow"""
    print("Test: Carbon API automatic config flow")

    # Test with automatic=True and first=True
    api = MockCarbonAPI()
    api.postcode = "BS16"
    api.automatic = True

    mock_response = create_aiohttp_mock_response(status=200, json_data=MOCK_CARBON_API_RESPONSE)
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api.run(seconds=0, first=True))

        # Should have set carbon_intensity config
        if "carbon_intensity" not in api.config_args:
            print("ERROR: carbon_intensity config not set")
            return 1

        expected_entity = "sensor.predbat_carbon_intensity"
        if api.config_args["carbon_intensity"] != expected_entity:
            print(f"ERROR: Expected config '{expected_entity}', got '{api.config_args['carbon_intensity']}'")
            return 1

    # Test with automatic=False
    api2 = MockCarbonAPI()
    api2.postcode = "BS16"
    api2.automatic = False

    with patch("carbon.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = mock_session
        run_async(api2.run(seconds=0, first=True))

        # Should NOT have set carbon_intensity config
        if "carbon_intensity" in api2.config_args:
            print("ERROR: carbon_intensity config should not be set when automatic=False")
            return 1

    print("  ✓ Automatic config flow working correctly")
    return 0


# =============================================================================
# Forward replication tests (carbon data running out before the plan horizon)
# =============================================================================


def _test_carbon_replicate_extends_forward(my_predbat=None):
    """Test carbon data is replicated forward when the forecast runs out"""
    print("Test: Carbon replicate extends forward")

    saved_forecast_minutes = my_predbat.forecast_minutes
    try:
        my_predbat.forecast_minutes = 48 * 60

        # Only 15 hours of real data, as happens when the API forecast is truncated
        real_minutes = 15 * 60
        carbon_data = {minute: 100 + (minute // 60) * 10 for minute in range(0, real_minutes)}
        expected_last = carbon_data[real_minutes - 1]
        expected_day1 = dict(carbon_data)

        carbon_data, replicated = my_predbat.carbon_replicate(carbon_data)

        # The whole plan horizon must now be covered
        missing = [minute for minute in range(0, 48 * 60) if minute not in carbon_data]
        if missing:
            print(f"ERROR: {len(missing)} minutes still missing, first is {missing[0]}")
            return 1

        # Real data must not be touched
        for minute, value in expected_day1.items():
            if carbon_data[minute] != value:
                print(f"ERROR: Real data at minute {minute} changed from {value} to {carbon_data[minute]}")
                return 1
            if minute in replicated:
                print(f"ERROR: Real minute {minute} was marked as replicated")
                return 1

        # Tail of the first day holds the last known value, there is no 24h-previous point yet
        for minute in (real_minutes, real_minutes + 100, 24 * 60 - 1):
            if carbon_data[minute] != expected_last:
                print(f"ERROR: Minute {minute} should hold last value {expected_last}, got {carbon_data[minute]}")
                return 1
            if minute not in replicated:
                print(f"ERROR: Minute {minute} should be marked as replicated")
                return 1

        # Second day repeats the same time of day 24 hours earlier
        for minute in (0, 500, real_minutes - 1):
            if carbon_data[minute + 24 * 60] != expected_day1[minute]:
                print(f"ERROR: Minute {minute + 24 * 60} should repeat minute {minute} ({expected_day1[minute]}), got {carbon_data[minute + 24 * 60]}")
                return 1
    finally:
        my_predbat.forecast_minutes = saved_forecast_minutes

    print("  ✓ Carbon data replicated forward correctly")
    return 0


def _test_carbon_replicate_no_data(my_predbat=None):
    """Test carbon replicate invents nothing when there is no data at all"""
    print("Test: Carbon replicate with no data")

    carbon_data, replicated = my_predbat.carbon_replicate({})

    if carbon_data:
        print(f"ERROR: Expected no data to be invented, got {len(carbon_data)} minutes")
        return 1

    if replicated:
        print(f"ERROR: Expected nothing to be marked replicated, got {len(replicated)} minutes")
        return 1

    print("  ✓ No data left alone correctly")
    return 0


def _test_carbon_replicate_full_coverage(my_predbat=None):
    """Test carbon replicate is a no-op when the forecast already covers the plan"""
    print("Test: Carbon replicate with full coverage")

    saved_forecast_minutes = my_predbat.forecast_minutes
    try:
        my_predbat.forecast_minutes = 48 * 60
        carbon_data = {minute: 200 for minute in range(0, 48 * 60)}

        carbon_data, replicated = my_predbat.carbon_replicate(carbon_data)

        if replicated:
            print(f"ERROR: Expected no replication when data is complete, got {len(replicated)} minutes")
            return 1

        if len(carbon_data) != 48 * 60:
            print(f"ERROR: Expected {48 * 60} minutes, got {len(carbon_data)}")
            return 1
    finally:
        my_predbat.forecast_minutes = saved_forecast_minutes

    print("  ✓ Complete forecast left unchanged correctly")
    return 0


def _test_carbon_replicate_gap(my_predbat=None):
    """Test carbon replicate fills a hole in the middle of the forecast"""
    print("Test: Carbon replicate fills gap")

    saved_forecast_minutes = my_predbat.forecast_minutes
    try:
        my_predbat.forecast_minutes = 24 * 60
        carbon_data = {minute: 150 for minute in range(0, 24 * 60)}
        for minute in range(100, 200):
            del carbon_data[minute]
        carbon_data[99] = 175

        carbon_data, replicated = my_predbat.carbon_replicate(carbon_data)

        for minute in range(100, 200):
            if minute not in carbon_data:
                print(f"ERROR: Gap minute {minute} not filled")
                return 1
            if carbon_data[minute] != 175:
                print(f"ERROR: Gap minute {minute} should hold last value 175, got {carbon_data[minute]}")
                return 1
            if minute not in replicated:
                print(f"ERROR: Gap minute {minute} should be marked as replicated")
                return 1

        if len(replicated) != 100:
            print(f"ERROR: Expected exactly 100 replicated minutes, got {len(replicated)}")
            return 1
    finally:
        my_predbat.forecast_minutes = saved_forecast_minutes

    print("  ✓ Gap filled correctly")
    return 0
