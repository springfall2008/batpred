# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on

from axle import AxleAPI
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from const import TIME_FORMAT
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session, run_async


class MockAxleAPI(AxleAPI):
    """Mock AxleAPI class for testing without ComponentBase dependencies"""

    def __init__(self):
        # Don't call parent __init__ to avoid ComponentBase
        self.api_key = "test_api_key"
        self.pence_per_kwh = 100
        self.mock_api_responses = {}
        self.dashboard_items = {}
        self.log_messages = []
        self.config_args = {}
        self.prefix = "predbat"

        # Initialize instance variables that AxleAPI expects
        self.failures_total = 0
        self.last_updated_timestamp = None
        self.event_history = []
        self.current_event = {
            "start_time": None,
            "end_time": None,
            "import_export": None,
            "pence_per_kwh": None,
        }
        self.updated_at = None
        self.history_loaded = False
        self._now_utc = datetime.now(timezone.utc)
        self._state_store = {}  # Mock state storage

    @property
    def now_utc(self):
        """Mock now_utc property"""
        return self._now_utc

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)

    def call_notify(self, message):
        """Mock notify method"""
        self.log_messages.append("Alert: " + message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Mock dashboard_item - tracks calls and stores in state"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes, "app": app}
        self._state_store[entity_id] = {"state": state, "attributes": attributes}

    def get_state_wrapper(self, entity_id, default=None, attribute=None):
        """Mock get_state_wrapper - retrieves from state store"""
        if entity_id not in self._state_store:
            return default

        if attribute:
            return self._state_store[entity_id]["attributes"].get(attribute, default)
        return self._state_store[entity_id]["state"]

    def get_arg(self, arg, default=None, indirect=False):
        """Mock get_arg - retrieves from config_args"""
        return self.config_args.get(arg, default)

    def update_success_timestamp(self):
        """Mock update_success_timestamp"""
        pass

    def last_updated_time(self):
        """Mock last_updated_time"""
        return datetime.now(timezone.utc)


def test_axle(my_predbat=None):
    """
    ======================================================================
    AXLE ENERGY VPP TEST SUITE
    ======================================================================
    Comprehensive test suite for Axle Energy Virtual Power Plant integration including:
    - Initialization and configuration
    - Event fetching (active, future, past, no events)
    - Error handling (HTTP errors, request exceptions, JSON parse errors)
    - Retry logic and datetime parsing
    - History loading and cleanup
    - Fetch sessions and load slot export
    - Active status checking
    """
    print("\n" + "=" * 70)
    print("AXLE ENERGY VPP TEST SUITE")
    print("=" * 70)

    # Sub-test registry - each entry is (key, function, description)
    sub_tests = [
        ("initialization", _test_axle_initialization, "Axle API initialization"),
        ("active_event", _test_axle_fetch_with_active_event, "Fetch with active event"),
        ("future_event", _test_axle_fetch_with_future_event, "Fetch with future event"),
        ("past_event", _test_axle_fetch_with_past_event, "Fetch with past event"),
        ("no_event", _test_axle_fetch_no_event, "Fetch with no event"),
        ("http_error", _test_axle_http_error, "HTTP error handling"),
        ("request_exception", _test_axle_request_exception, "Request exception handling"),
        ("retry_success", _test_axle_retry_success_after_failure, "Retry success after failure"),
        ("datetime_parsing", _test_axle_datetime_parsing_variations, "Datetime parsing variations"),
        ("json_parse_error", _test_axle_json_parse_error, "JSON parse error handling"),
        ("run_method", _test_axle_run_method, "Run method execution"),
        ("history_loading", _test_axle_history_loading, "History loading from state"),
        ("history_cleanup", _test_axle_history_cleanup, "History cleanup old events"),
        ("fetch_sessions", _test_axle_fetch_sessions, "Fetch sessions from API"),
        ("load_slot_export", _test_axle_load_slot_export, "Load slot export integration"),
        ("active_function", _test_axle_active_function, "Active status checking"),
    ]

    # Run all sub-tests
    passed = 0
    failed = 0
    for key, test_func, description in sub_tests:
        print(f"\n[{key}] {description}")
        print("-" * 70)
        try:
            result = test_func(my_predbat)
            if result:
                print(f"✗ FAILED: {key}")
                failed += 1
            else:
                print(f"✓ PASSED: {key}")
                passed += 1
        except Exception as e:
            print(f"✗ EXCEPTION in {key}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    # Print summary
    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed > 0


def _test_axle_initialization(my_predbat=None):
    """Test AxleAPI component initialization"""
    print("Test: Axle API initialization")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key_12345", pence_per_kwh=150, automatic=False)

    assert axle.api_key == "test_key_12345", "API key not set correctly"
    assert axle.pence_per_kwh == 150, "Pence per kWh not set correctly"
    assert axle.failures_total == 0, "Failures should be 0 on init"
    assert axle.last_updated_timestamp is None, "Last updated should be None on init"
    assert axle.current_event["start_time"] is None, "Current event should be None on init"
    assert axle.event_history == [], "Event history should be empty on init"
    assert axle.history_loaded is False, "History should not be loaded on init"

    print("  ✓ Initialization successful")
    return False


def _test_axle_fetch_with_active_event(my_predbat=None):
    """Test fetching an active VPP event from API"""
    print("Test: Axle API fetch with active event")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Set current time
    now = datetime(2025, 12, 20, 14, 30, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Mock aiohttp response
    json_data = {"start_time": "2025-12-20T14:00:00Z", "end_time": "2025-12-20T16:00:00Z", "import_export": "export", "updated_at": "2025-12-20T13:45:00Z"}
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        run_async(axle.fetch_axle_event())

    # Verify current event data was parsed correctly (stored as strings)
    assert axle.current_event["start_time"] == "2025-12-20T14:00:00+0000"
    assert axle.current_event["end_time"] == "2025-12-20T16:00:00+0000"
    assert axle.current_event["import_export"] == "export"
    assert axle.current_event["pence_per_kwh"] == 100, "Pence per kWh should be set"

    # Event SHOULD be in history (added as soon as it starts)
    assert len(axle.event_history) == 1, "Active event should be added to history when it starts"
    assert axle.event_history[0]["start_time"] == "2025-12-20T14:00:00+0000"
    assert axle.event_history[0]["import_export"] == "export"
    assert axle.event_history[0]["pence_per_kwh"] == 100, "Event history should include pence_per_kwh"

    # Verify binary sensor was published with "on" state (event is active)
    sensor_id = "binary_sensor.predbat_axle_event"
    assert sensor_id in axle.dashboard_items, "Binary sensor not published"
    sensor = axle.dashboard_items[sensor_id]
    assert sensor["state"] == "on", "Sensor should be ON during active event"
    assert len(sensor["attributes"]["event_current"]) == 1, "Should have current event"
    assert sensor["attributes"]["event_current"][0]["start_time"] == "2025-12-20T14:00:00+0000"
    assert sensor["attributes"]["event_current"][0]["end_time"] == "2025-12-20T16:00:00+0000"
    assert sensor["attributes"]["event_current"][0]["import_export"] == "export"
    assert sensor["attributes"]["event_current"][0]["pence_per_kwh"] == 100, "Current event should include pence_per_kwh"
    assert len(sensor["attributes"]["event_history"]) == 1, "History should contain the active event"

    assert axle.failures_total == 0, "No failures should be recorded"

    print("  ✓ Active event fetched and published correctly")
    return False


def _test_axle_fetch_with_future_event(my_predbat=None):
    """Test fetching a future VPP event (not yet active)"""
    print("Test: Axle API fetch with future event")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Set current time before event starts
    now = datetime(2025, 12, 20, 13, 30, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Mock API response with a future event
    json_data = {"start_time": "2025-12-20T14:00:00Z", "end_time": "2025-12-20T16:00:00Z", "import_export": "import", "updated_at": "2025-12-20T13:25:00Z"}
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        run_async(axle.fetch_axle_event())

    # Verify event data was parsed (stored as strings)
    assert axle.current_event["start_time"] == "2025-12-20T14:00:00+0000"
    assert axle.current_event["import_export"] == "import"

    # Event should NOT be in history (not ended)
    assert len(axle.event_history) == 0, "Future event should not be in history"

    # Verify binary sensor is OFF (event not yet active)
    sensor = axle.dashboard_items["binary_sensor.predbat_axle_event"]
    assert sensor["state"] == "off", "Sensor should be OFF before event starts"
    assert len(sensor["attributes"]["event_current"]) == 1, "Should have current event in list"
    assert len(sensor["attributes"]["event_history"]) == 0, "Future event not in history"

    print("  ✓ Future event shows OFF state correctly")
    return False


def _test_axle_fetch_with_past_event(my_predbat=None):
    """Test fetching a past VPP event (already ended) - should be added to history"""
    print("Test: Axle API fetch with past event")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Set current time after event ends
    now = datetime(2025, 12, 20, 17, 0, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Mock API response with a past event
    json_data = {"start_time": "2025-12-20T14:00:00Z", "end_time": "2025-12-20T16:00:00Z", "import_export": "export", "updated_at": "2025-12-20T13:45:00Z"}
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        run_async(axle.fetch_axle_event())

    # Event should be added to history (ended)
    assert len(axle.event_history) == 1, "Past event should be in history"
    assert axle.event_history[0]["import_export"] == "export"

    # Verify binary sensor is OFF (event already ended)
    sensor = axle.dashboard_items["binary_sensor.predbat_axle_event"]
    assert sensor["state"] == "off", "Sensor should be OFF after event ends"
    assert len(sensor["attributes"]["event_current"]) == 1, "Should have current event"
    assert len(sensor["attributes"]["event_history"]) == 1, "Past event in history"

    print("  ✓ Past event added to history correctly")
    return False


def _test_axle_fetch_no_event(my_predbat=None):
    """Test API response when no event is scheduled"""
    print("Test: Axle API fetch with no event")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Mock API response with empty/null data
    mock_response = create_aiohttp_mock_response(status=200, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        run_async(axle.fetch_axle_event())

    # Verify all event data is None
    assert axle.current_event["start_time"] is None
    assert axle.current_event["end_time"] is None
    assert axle.current_event["import_export"] is None

    # Verify binary sensor is OFF with empty event_current
    sensor = axle.dashboard_items["binary_sensor.predbat_axle_event"]
    assert sensor["state"] == "off", "Sensor should be OFF when no event"
    assert len(sensor["attributes"]["event_current"]) == 0, "Should have empty event_current list"
    assert len(sensor["attributes"]["event_history"]) == 0, "Should have empty history"

    assert axle.failures_total == 0, "No failures for empty response"

    print("  ✓ No event handled correctly")
    return False


def _test_axle_http_error(my_predbat=None):
    """Test handling of HTTP errors (no retry on non-200 status)"""
    print("Test: Axle API HTTP error handling")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Mock API response with error status
    mock_response = create_aiohttp_mock_response(status=401)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with patch("asyncio.sleep") as mock_sleep:
            run_async(axle.fetch_axle_event())

            # Should only call once - no retries for non-200 status
            assert mock_sleep.call_count == 0, "Should not sleep on first failure with status code"

    assert axle.failures_total == 1, "Failure should be recorded for HTTP error"
    assert any("status code 401" in msg for msg in axle.log_messages), "Error should be logged"

    print("  ✓ HTTP error handled correctly (no retry)")
    return False


def _test_axle_request_exception(my_predbat=None):
    """Test handling of request exceptions with retry mechanism"""
    print("Test: Axle API request exception handling with retries")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Mock request exception
    import aiohttp

    mock_session = create_aiohttp_mock_session(exception=aiohttp.ClientError("Connection timeout"))

    call_count = [0]
    original_aenter = mock_session.__aenter__

    async def count_calls(self):
        call_count[0] += 1
        return await original_aenter()

    mock_session.__aenter__ = count_calls

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with patch("asyncio.sleep") as mock_sleep:
            run_async(axle.fetch_axle_event())

            # Should retry 3 times total
            assert call_count[0] == 3, f"Should retry 3 times on ClientError, got {call_count[0]}"

            # Should sleep twice (between retries): 1s, 2s
            assert mock_sleep.call_count == 2, "Should sleep between retries"

            # Verify exponential backoff: 2^0=1s, 2^1=2s
            sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
            assert sleep_calls == [1, 2], f"Expected [1, 2] second sleeps, got {sleep_calls}"

    assert axle.failures_total == 1, "Failure should be recorded for request exception"
    assert any("Request failed after 3 attempts" in msg for msg in axle.log_messages), "Exception should be logged with retry count"
    assert any("Retrying in" in msg for msg in axle.log_messages), "Retry messages should be logged"

    print("  ✓ Request exception handled correctly with 3 retries and exponential backoff")
    return False


def _test_axle_retry_success_after_failure(my_predbat=None):
    """Test successful request after initial failures (retry succeeds)"""
    print("Test: Axle API retry success after failures")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Set current time
    now = datetime(2025, 12, 20, 14, 30, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Mock API response - fail twice, then succeed
    import aiohttp

    json_data = {"start_time": "2025-12-20T14:00:00Z", "end_time": "2025-12-20T16:00:00Z", "import_export": "export", "updated_at": "2025-12-20T13:45:00Z"}

    # Create sessions - first two raise exceptions, third succeeds
    call_count = [0]

    def get_session(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            return create_aiohttp_mock_session(exception=aiohttp.ClientError(f"Error {call_count[0]}"))
        else:
            mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
            return create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", side_effect=get_session):
        with patch("asyncio.sleep") as mock_sleep:
            run_async(axle.fetch_axle_event())

            # Should try 3 times total (2 failures, 1 success)
            assert call_count[0] == 3, f"Should retry until success, got {call_count[0]} attempts"

            # Should sleep twice (between first 2 failures)
            assert mock_sleep.call_count == 2, "Should sleep between retries"

            # Verify exponential backoff: 2^0=1s, 2^1=2s
            sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
            assert sleep_calls == [1, 2], f"Expected [1, 2] second sleeps, got {sleep_calls}"

    # Verify event was successfully fetched after retries
    assert axle.current_event["start_time"] == "2025-12-20T14:00:00+0000", "Event should be fetched after retry"
    assert axle.current_event["import_export"] == "export"
    assert axle.failures_total == 0, "No failure should be recorded when retry succeeds"
    assert any("Successfully fetched event data" in msg for msg in axle.log_messages), "Success should be logged"
    assert any("Retrying in" in msg for msg in axle.log_messages), "Retry attempts should be logged"

    print("  ✓ Retry mechanism succeeds after initial failures")
    return False


def _test_axle_datetime_parsing_variations(my_predbat=None):
    """Test parsing of different ISO 8601 datetime formats"""
    print("Test: Axle API datetime parsing variations")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Set time after event
    axle._now_utc = datetime(2025, 12, 20, 17, 0, 0, tzinfo=timezone.utc)

    # Test with Z suffix (already handled)
    json_data = {"start_time": "2025-12-20T14:00:00Z", "end_time": "2025-12-20T16:00:00+00:00", "import_export": "export", "updated_at": "2025-12-20T13:45:00Z"}  # Alternative format
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        run_async(axle.fetch_axle_event())

    # Timestamps should be converted to strings in TIME_FORMAT
    assert axle.current_event["start_time"] is not None, "Should parse Z suffix"
    assert axle.current_event["end_time"] is not None, "Should parse +00:00 suffix"
    # Verify they are strings, not datetime objects
    assert isinstance(axle.current_event["start_time"], str), "Should be stored as string"
    assert isinstance(axle.current_event["end_time"], str), "Should be stored as string"

    print("  ✓ Different datetime formats parsed correctly")
    return False


def _test_axle_json_parse_error(my_predbat=None):
    """Test handling of JSON parsing errors (no retry)"""
    print("Test: Axle API JSON parsing error handling")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Mock API response with invalid JSON
    mock_response = create_aiohttp_mock_response(status=200, json_exception=ValueError("Invalid JSON"))
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        with patch("asyncio.sleep") as mock_sleep:
            run_async(axle.fetch_axle_event())

            # Should only call once - no retries for JSON parse errors
            assert mock_sleep.call_count == 0, "Should not sleep on JSON parse error"

    assert axle.failures_total == 1, "Failure should be recorded for JSON parse error"
    assert any("Failed to parse JSON response" in msg for msg in axle.log_messages), "Parse error should be logged"

    print("  ✓ JSON parse error handled correctly (no retry)")
    return False


def _test_axle_run_method(my_predbat=None):
    """Test the run method polling logic and history loading"""
    print("Test: Axle API run method")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Mock the fetch method
    fetch_called = []

    async def mock_fetch():
        fetch_called.append(True)

    axle.fetch_axle_event = mock_fetch

    # Test first run - should load history and fetch
    result = run_async(axle.run(seconds=0, first=True))
    assert result is True, "Run should return True"
    assert axle.history_loaded is True, "Should load history on first run"
    assert len(fetch_called) == 1, "Should fetch on first run"

    # Test run at 600 seconds (10 minutes)
    fetch_called.clear()
    result = run_async(axle.run(seconds=600, first=False))
    assert len(fetch_called) == 1, "Should fetch at 10 minute mark"

    # Test run at non-fetch time
    fetch_called.clear()
    result = run_async(axle.run(seconds=300, first=False))
    assert len(fetch_called) == 0, "Should not fetch at 5 minute mark"

    print("  ✓ Run method polling logic correct")
    return False


def _test_axle_history_loading(my_predbat=None):
    """Test loading event history from sensor on startup"""
    print("Test: Axle API history loading")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    now = datetime(2025, 12, 20, 17, 0, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Simulate existing sensor state with history
    past_event_1 = {
        "start_time": datetime(2025, 12, 19, 14, 0, 0, tzinfo=timezone.utc),
        "end_time": datetime(2025, 12, 19, 16, 0, 0, tzinfo=timezone.utc),
        "import_export": "import",
    }
    future_event = {
        "start_time": datetime(2025, 12, 21, 14, 0, 0, tzinfo=timezone.utc),
        "end_time": datetime(2025, 12, 21, 16, 0, 0, tzinfo=timezone.utc),
        "import_export": "export",
    }

    # Store in mock state
    sensor_id = "binary_sensor.predbat_axle_event"
    axle._state_store[sensor_id] = {
        "state": "off",
        "attributes": {
            "event_history": [past_event_1, future_event],  # Mix of past and future
        },
    }

    # Load history
    axle.load_event_history()

    # Should only load past events
    assert len(axle.event_history) == 1, "Should only load past events"
    assert axle.event_history[0]["import_export"] == "import"
    assert axle.history_loaded is True

    print("  ✓ History loading filters future events correctly")
    return False


def _test_axle_history_cleanup(my_predbat=None):
    """Test cleanup of events older than 7 days"""
    print("Test: Axle API history cleanup")

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    now = datetime(2025, 12, 20, 17, 0, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Add events at different ages (as strings - how they're stored in sensor)
    old_event = {
        "start_time": "2025-12-10T14:00:00+0000",
        "end_time": "2025-12-10T16:00:00+0000",
        "import_export": "import",
    }
    recent_event = {
        "start_time": "2025-12-19T14:00:00+0000",
        "end_time": "2025-12-19T16:00:00+0000",
        "import_export": "export",
    }

    axle.event_history = [old_event, recent_event]

    # Run cleanup
    axle.cleanup_event_history()

    # Should only keep events within 7 days
    assert len(axle.event_history) == 1, "Should remove events older than 7 days"
    assert axle.event_history[0]["import_export"] == "export"

    print("  ✓ History cleanup removes old events correctly")
    return False


def _test_axle_fetch_sessions(my_predbat=None):
    """Test fetching Axle sessions from sensor (current + history)"""
    print("Test: Axle API fetch sessions")

    from axle import fetch_axle_sessions

    axle = MockAxleAPI()
    axle.initialize(api_key="test_key", pence_per_kwh=100, automatic=False)

    # Configure axle_session in config_args
    sensor_id = "binary_sensor.predbat_axle_event"
    axle.config_args["axle_session"] = sensor_id

    # Set current time
    now = datetime(2025, 12, 20, 15, 0, 0, tzinfo=timezone.utc)
    axle._now_utc = now

    # Simulate sensor state with both current event and history (as strings)
    current_event = {
        "start_time": "2025-12-20T14:00:00+0000",
        "end_time": "2025-12-20T16:00:00+0000",
        "import_export": "export",
        "pence_per_kwh": 100,
    }

    past_event_1 = {
        "start_time": "2025-12-19T10:00:00+0000",
        "end_time": "2025-12-19T12:00:00+0000",
        "import_export": "import",
        "pence_per_kwh": 100,
    }

    past_event_2 = {
        "start_time": "2025-12-18T14:00:00+0000",
        "end_time": "2025-12-18T16:00:00+0000",
        "import_export": "export",
        "pence_per_kwh": 100,
    }

    # Store in mock state
    axle._state_store[sensor_id] = {
        "state": "on",
        "attributes": {
            "event_current": [current_event],
            "event_history": [past_event_1, past_event_2],
        },
    }

    # Fetch sessions
    sessions = fetch_axle_sessions(axle)

    # Should return all events (current + history)
    assert len(sessions) == 3, "Should return current event + 2 history events"

    # Verify current event is included (as strings)
    assert sessions[0]["start_time"] == "2025-12-20T14:00:00+0000"
    assert sessions[0]["import_export"] == "export"
    assert sessions[0]["pence_per_kwh"] == 100

    # Verify history events are included (as strings)
    assert sessions[1]["start_time"] == "2025-12-19T10:00:00+0000"
    assert sessions[1]["import_export"] == "import"

    assert sessions[2]["start_time"] == "2025-12-18T14:00:00+0000"
    assert sessions[2]["import_export"] == "export"

    print("  ✓ Fetch sessions returns current + history correctly")
    return False


def _test_axle_load_slot_export(my_predbat=None):
    """
    Test that load_axle_slot increases export rates by pence_per_kwh for export events
    """
    from axle import load_axle_slot
    from datetime import datetime, timezone

    print("Testing load_axle_slot export rate increase...")

    # Create a mock base object with necessary attributes
    class MockBase:
        def __init__(self):
            self.midnight_utc = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            self.now_utc = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
            self.minutes_now = 10 * 60  # 10:00 AM
            self.forecast_minutes = 24 * 60  # 24 hours
            self.prefix = "predbat"

            # Initialize rate_export with base rates for each minute
            self.rate_export = {}
            for minute in range(self.forecast_minutes):
                self.rate_export[minute] = 5.0  # Base rate of 5p/kWh

            self.rate_import = {}
            self.load_scaling_dynamic = {}
            self.load_scaling_saving = 0.5

        def log(self, message):
            print(f"  [LOG] {message}")

        def call_notify(self, message):
            """Mock notify method"""
            self.log_messages.append("Alert: " + message)

        def time_abs_str(self, minutes):
            return f"{minutes//60:02d}:{minutes%60:02d}"

        def get_arg(self, name, indirect=True):
            return None

    base = MockBase()

    # Create an export event from 14:00 to 16:00 (2 hours) with 100p/kWh payment
    # Use ISO 8601 string format since load_axle_slot expects strings
    start_time = "2024-01-01T14:00:00+00:00"
    end_time = "2024-01-01T16:00:00+00:00"

    axle_sessions = [
        {
            "start_time": start_time,
            "end_time": end_time,
            "import_export": "export",
            "pence_per_kwh": 100.0,
        }
    ]

    # Get the rate before the event
    start_minutes = 14 * 60  # 14:00 = 840 minutes
    end_minutes = 16 * 60  # 16:00 = 960 minutes

    original_rate_before = base.rate_export[start_minutes]
    original_rate_during = base.rate_export[start_minutes + 30]  # 14:30
    original_rate_after = base.rate_export[end_minutes]  # Should not be modified

    # Load the Axle export slot
    rate_replicate = {}
    load_axle_slot(base, axle_sessions, export=True, rate_replicate=rate_replicate)

    # Verify rates were increased by 100p/kWh during the event period
    for minute in range(start_minutes, end_minutes):
        expected_rate = original_rate_before + 100.0  # 5.0 + 100.0 = 105.0
        actual_rate = base.rate_export[minute]
        assert actual_rate == expected_rate, f"Rate at minute {minute} should be {expected_rate}, got {actual_rate}"
        assert rate_replicate.get(minute) == "saving", f"Minute {minute} should be marked as 'saving' in rate_replicate"

    # Verify rates outside the event period were NOT modified
    assert base.rate_export[start_minutes - 1] == 5.0, "Rate before event should be unchanged"
    assert base.rate_export[end_minutes] == 5.0, "Rate at end_minutes (not inclusive) should be unchanged"

    # Verify the event was logged
    print("  ✓ Export rates increased by 100p/kWh for 2-hour period (14:00-16:00)")
    print(f"  ✓ Rate at 14:00 changed from {original_rate_before} to {base.rate_export[start_minutes]}")
    print(f"  ✓ Rate at 13:59 unchanged: {base.rate_export[start_minutes - 1]}")
    print(f"  ✓ Rate at 16:00 unchanged: {base.rate_export[end_minutes]}")
    print(f"  ✓ {len(rate_replicate)} minutes marked as 'saving' events")

    return False


def _test_axle_active_function(my_predbat=None):
    """Test fetch_axle_active function to check if VPP event is currently active"""
    print("Testing fetch_axle_active function...")

    # Test with active event
    now = datetime.now(timezone.utc)
    active_event = {
        "id": "evt_active_123",
        "start_time": (now - timedelta(hours=1)).strftime(TIME_FORMAT),
        "end_time": (now + timedelta(hours=1)).strftime(TIME_FORMAT),
        "import_export": "export",
        "status": "active",
        "payment_pence_per_kwh": 150,
    }

    # Create test interface with active event sensor
    test_interface = MockAxleAPI()
    test_interface.config_args["axle_session"] = "binary_sensor.predbat_axle_event"

    # Manually set the sensor state to "on" since fetch_axle_event hasn't been called yet
    test_interface.dashboard_item(
        entity_id="binary_sensor.predbat_axle_event",
        state="on",
        attributes={"event_current": [active_event], "event_history": []},
    )

    # Test fetch_axle_active returns True when event is active
    from axle import fetch_axle_active

    result = fetch_axle_active(test_interface)
    assert result is True, "fetch_axle_active should return True when sensor is 'on'"

    # Test with no active event
    test_interface.dashboard_item(
        entity_id="binary_sensor.predbat_axle_event",
        state="off",
        attributes={"event_current": [], "event_history": []},
    )

    result = fetch_axle_active(test_interface)
    assert result is False, "fetch_axle_active should return False when sensor is 'off'"

    # Test with missing sensor (no axle_session config)
    test_interface_no_config = MockAxleAPI()
    test_interface_no_config.config_args["axle_session"] = None

    result = fetch_axle_active(test_interface_no_config)
    assert result is False, "fetch_axle_active should return False when axle_session is not configured"

    print("✓ fetch_axle_active function test passed")


# Run all tests
if __name__ == "__main__":
    print("\n=== Axle API Component Tests ===\n")

    test_axle_initialization()
    test_axle_fetch_with_active_event()
    test_axle_fetch_with_future_event()
    test_axle_fetch_with_past_event()
    test_axle_fetch_no_event()
    test_axle_http_error()
    test_axle_request_exception()
    test_axle_retry_success_after_failure()
    test_axle_json_parse_error()
    test_axle_datetime_parsing_variations()
    test_axle_run_method()
    test_axle_history_loading()
    test_axle_history_cleanup()
    test_axle_fetch_sessions()
    test_axle_load_slot_export()
    test_axle_active_function()

    print("\n=== All Axle API tests passed! ===\n")
