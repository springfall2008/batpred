# fmt: off
"""
HAInterface API Tests

Tests for HAInterface API-related methods:
- api_call() - GET/POST requests with error handling
- initialize() - Addon/services checks
- get_history() - Historical data fetching
"""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import requests

from tests.test_hainterface_common import MockBase, MockDatabaseManager, create_ha_interface, create_mock_requests_response
from ha import HAInterface


def test_hainterface_api_call_get(my_predbat=None):
    """Test api_call() GET request"""
    print("\n=== Testing HAInterface api_call() GET ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, {"result": "success"})

        result = ha_interface.api_call("/api/states", post=False)

        # Verify GET called correctly
        if not mock_get.called:
            print("ERROR: requests.get should be called")
            failed += 1
        else:
            call_args = mock_get.call_args
            if "/api/states" not in call_args[0][0]:
                print("ERROR: Wrong URL called")
                failed += 1
            elif "Authorization" not in call_args[1]["headers"]:
                print("ERROR: Authorization header missing")
                failed += 1
            else:
                print("✓ GET request made correctly")

        # Verify result
        if result != {"result": "success"}:
            print(f"ERROR: Wrong result: {result}")
            failed += 1
        else:
            print("✓ Result returned correctly")

    return failed


def test_hainterface_api_call_post(my_predbat=None):
    """Test api_call() POST request"""
    print("\n=== Testing HAInterface api_call() POST ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.post") as mock_post:
        mock_post.return_value = create_mock_requests_response(200, {"status": "ok"})

        result = ha_interface.api_call("/api/services/test/action", data_in={"entity_id": "test.entity"}, post=True)

        # Verify POST called correctly
        if not mock_post.called:
            print("ERROR: requests.post should be called")
            failed += 1
        else:
            call_args = mock_post.call_args
            if "json" not in call_args[1]:
                print("ERROR: JSON data not passed")
                failed += 1
            elif call_args[1]["json"]["entity_id"] != "test.entity":
                print("ERROR: Wrong JSON data")
                failed += 1
            else:
                print("✓ POST request made correctly")

        # Verify result
        if result != {"status": "ok"}:
            print(f"ERROR: Wrong result: {result}")
            failed += 1
        else:
            print("✓ Result returned correctly")

    return failed


def test_hainterface_api_call_no_key(my_predbat=None):
    """Test api_call() returns None when no API key"""
    print("\n=== Testing HAInterface api_call() no key ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=True, db_mirror_ha=False, db_primary=True)

    result = ha_interface.api_call("/api/states", post=False)

    if result is not None:
        print(f"ERROR: Should return None, got {result}")
        failed += 1
    else:
        print("✓ Returned None when no API key")

    return failed


def test_hainterface_api_call_supervisor(my_predbat=None):
    """Test api_call() supervisor endpoint"""
    print("\n=== Testing HAInterface api_call() supervisor ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    # Mock SUPERVISOR_TOKEN environment variable
    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get:
        mock_env.return_value = "supervisor_token"
        mock_get.return_value = create_mock_requests_response(200, {"supervisor": "data"})

        result = ha_interface.api_call("/addons/self/info", core=False)

        # Verify supervisor URL used
        if not mock_get.called:
            print("ERROR: requests.get should be called")
            failed += 1
        else:
            call_args = mock_get.call_args
            if "http://supervisor" not in call_args[0][0]:
                print("ERROR: Supervisor URL not used")
                failed += 1
            elif "supervisor_token" not in call_args[1]["headers"]["Authorization"]:
                print("ERROR: Supervisor token not used")
                failed += 1
            else:
                print("✓ Supervisor endpoint called correctly")

    return failed


def test_hainterface_api_call_json_decode_error(my_predbat=None):
    """Test api_call() handles JSON decode errors"""
    print("\n=== Testing HAInterface api_call() JSON decode error ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.api_errors = 0

    with patch("ha.requests.get") as mock_get:
        # Mock response that raises JSONDecodeError
        mock_response = MagicMock()
        mock_response.json.side_effect = requests.exceptions.JSONDecodeError("msg", "doc", 0)
        mock_get.return_value = mock_response

        result = ha_interface.api_call("/api/states")

        # Verify error handled
        if result is not None:
            print(f"ERROR: Should return None on JSON error, got {result}")
            failed += 1
        else:
            print("✓ Returned None on JSON decode error")

        # Verify error count incremented
        if ha_interface.api_errors != 1:
            print(f"ERROR: api_errors should be 1, got {ha_interface.api_errors}")
            failed += 1
        else:
            print("✓ api_errors incremented")

    return failed


def test_hainterface_api_call_timeout(my_predbat=None):
    """Test api_call() handles timeout"""
    print("\n=== Testing HAInterface api_call() timeout ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.api_errors = 0

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.Timeout("Connection timeout")

        result = ha_interface.api_call("/api/states")

        # Verify error handled
        if result is not None:
            print(f"ERROR: Should return None on timeout, got {result}")
            failed += 1
        else:
            print("✓ Returned None on timeout")

        # Verify error count incremented
        if ha_interface.api_errors != 1:
            print(f"ERROR: api_errors should be 1, got {ha_interface.api_errors}")
            failed += 1
        else:
            print("✓ api_errors incremented")

    return failed


def test_hainterface_api_call_read_timeout(my_predbat=None):
    """Test api_call() handles ReadTimeout"""
    print("\n=== Testing HAInterface api_call() ReadTimeout ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.api_errors = 0

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.ReadTimeout("Read timeout")

        result = ha_interface.api_call("/api/states")

        # Verify error handled
        if result is not None:
            print(f"ERROR: Should return None on read timeout, got {result}")
            failed += 1
        else:
            print("✓ Returned None on ReadTimeout")

        # Verify error count incremented
        if ha_interface.api_errors != 1:
            print(f"ERROR: api_errors should be 1, got {ha_interface.api_errors}")
            failed += 1
        else:
            print("✓ api_errors incremented")

    return failed


def test_hainterface_api_call_silent_mode(my_predbat=None):
    """Test api_call() silent mode suppresses warnings"""
    print("\n=== Testing HAInterface api_call() silent mode ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.api_errors = 0
    log_called = [False]

    # Track log calls
    original_log = ha_interface.log

    def tracked_log(msg):
        if "Warn: Failed to decode" in msg:
            log_called[0] = True
        original_log(msg)

    ha_interface.log = tracked_log

    with patch("ha.requests.get") as mock_get:
        mock_response = MagicMock()
        mock_response.json.side_effect = requests.exceptions.JSONDecodeError("msg", "doc", 0)
        mock_get.return_value = mock_response

        # Call with silent=True
        result = ha_interface.api_call("/api/states", silent=True)

        # Verify warning not logged
        if log_called[0]:
            print("ERROR: Warning should be suppressed in silent mode")
            failed += 1
        else:
            print("✓ Warning suppressed in silent mode")

    return failed


def test_hainterface_api_call_error_limit(my_predbat=None):
    """Test api_call() triggers fatal error after 10 errors"""
    print("\n=== Testing HAInterface api_call() error limit ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.api_errors = 9  # Set to 9, next error will be 10th
    fatal_called = [False]

    def mock_fatal_error():
        fatal_called[0] = True

    ha_interface.fatal_error_occurred = mock_fatal_error

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.Timeout("Connection timeout")

        result = ha_interface.api_call("/api/states")

        # Verify fatal error triggered
        if not fatal_called[0]:
            print("ERROR: fatal_error_occurred should be called at 10 errors")
            failed += 1
        else:
            print("✓ fatal_error_occurred called at error limit")

    return failed


def test_hainterface_api_call_error_reset(my_predbat=None):
    """Test api_call() resets error count on success"""
    print("\n=== Testing HAInterface api_call() error reset ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    ha_interface.api_errors = 5

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, {"result": "success"})

        result = ha_interface.api_call("/api/states")

        # Verify error count reset
        if ha_interface.api_errors != 0:
            print(f"ERROR: api_errors should be reset to 0, got {ha_interface.api_errors}")
            failed += 1
        else:
            print("✓ api_errors reset on success")

    return failed


def test_hainterface_initialize_addon_check(my_predbat=None):
    """Test initialize() checks for addon/services"""
    print("\n=== Testing HAInterface initialize() addon check ===")
    failed = 0

    mock_base = MockBase()

    # Mock both addon info and services calls in initialize()
    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get:
        mock_env.return_value = "test_supervisor_token"  # Mock SUPERVISOR_TOKEN
        mock_get.side_effect = [
            create_mock_requests_response(200, {"data": {"slug": "predbat_addon"}}),  # addon info
            create_mock_requests_response(200, [{"domain": "homeassistant"}]),  # services
        ]

        # Must manually call initialize to use our mocked requests
        ha_interface = object.__new__(HAInterface)
        ha_interface.base = mock_base
        ha_interface.log = mock_base.log
        ha_interface.api_started = False
        ha_interface.api_stop = False
        ha_interface.last_success_timestamp = None
        ha_interface.local_tz = mock_base.local_tz
        ha_interface.prefix = mock_base.prefix
        ha_interface.args = mock_base.args
        ha_interface.count_errors = 0
        ha_interface.db_manager = None

        ha_interface.initialize("http://test:8123", "test_key", False, False, False)

        # Verify slug set
        if ha_interface.slug != "predbat_addon":
            print(f"ERROR: Slug should be 'predbat_addon', got {ha_interface.slug}")
            failed += 1
        else:
            print("✓ Addon slug detected correctly")

    return failed


def test_hainterface_initialize_no_addon(my_predbat=None):
    """Test initialize() handles missing addon gracefully"""
    print("\n=== Testing HAInterface initialize() no addon ===")
    failed = 0

    mock_base = MockBase()

    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get:
        # Mock supervisor token
        mock_env.return_value = "test_supervisor_token"
        # Mock addon call returns None (supervisor timeout), services call success
        mock_get.side_effect = [
            requests.Timeout("Supervisor timeout"),  # addon info fails with timeout
            create_mock_requests_response(200, [{"domain": "homeassistant"}]),  # services succeeds
        ]

        # Must manually call initialize to use our mocked requests
        ha_interface = object.__new__(HAInterface)
        ha_interface.base = mock_base
        ha_interface.log = mock_base.log
        ha_interface.api_started = False
        ha_interface.api_stop = False
        ha_interface.last_success_timestamp = None
        ha_interface.local_tz = mock_base.local_tz
        ha_interface.prefix = mock_base.prefix
        ha_interface.args = mock_base.args
        ha_interface.count_errors = 0
        ha_interface.db_manager = None

        ha_interface.initialize("http://test:8123", "test_key", False, False, False)

        # Verify slug is None
        if ha_interface.slug is not None:
            print(f"ERROR: Slug should be None, got {ha_interface.slug}")
            failed += 1
        else:
            print("✓ Missing addon handled gracefully")

    return failed


def test_hainterface_get_history_basic(my_predbat=None):
    """Test get_history() fetches data correctly"""
    print("\n=== Testing HAInterface get_history() basic ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    # Create mock history data
    now = datetime.now()
    history_data = [
        {
            "entity_id": "sensor.battery",
            "state": str(50 + i),
            "last_changed": (now - timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "attributes": {"unit": "kWh"},
        }
        for i in range(10)
    ]

    with patch("ha.requests.get") as mock_get:
        # HA API returns a list containing the history array
        mock_get.return_value = create_mock_requests_response(200, [history_data])

        result = ha_interface.get_history("sensor.battery", datetime.now(), days=1)

        # Verify API called
        if not mock_get.called:
            print("ERROR: API should be called")
            failed += 1
        else:
            print("✓ API called")

        # Verify result structure - get_history returns the list directly
        if not isinstance(result, list):
            print(f"ERROR: Should return list, got {type(result)}")
            failed += 1
        elif len(result) != 1:  # Returns list with one element (the history array)
            print(f"ERROR: Should return 1 list element, got {len(result)}")
            failed += 1
        elif len(result[0]) != 10:
            print(f"ERROR: History array should have 10 items, got {len(result[0])}")
            failed += 1
        else:
            print("✓ History data returned correctly")

    return failed


def test_hainterface_get_history_no_key(my_predbat=None):
    """Test get_history() uses DB when no API key in db_primary mode"""
    print("\n=== Testing HAInterface get_history() no key ===")
    failed = 0

    mock_base = MockBase()
    mock_db = MockDatabaseManager()
    mock_db.state_data["sensor.battery"] = {"state": "50", "attributes": {}, "last_changed": "2025-12-25T10:00:00Z"}

    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=True, db_mirror_ha=False, db_primary=True)
    ha_interface.db_manager = mock_db

    # When no API key and db_primary, should use database
    result = ha_interface.get_history("sensor.battery", datetime.now(), days=1)

    # DB returns empty list when get_history_db called (not implemented in mock)
    if result != []:
        print(f"ERROR: Should return empty list from DB, got {result}")
        failed += 1
    else:
        print("✓ Used database when no API key")

    return failed


def test_hainterface_get_history_api_error(my_predbat=None):
    """Test get_history() handles API errors"""
    print("\n=== Testing HAInterface get_history() API error ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.Timeout("Connection timeout")

        result = ha_interface.get_history("sensor.battery", datetime.now(), days=1)

        # Verify None returned on error
        if result is not None:
            print(f"ERROR: Should return None on API error, got {result}")
            failed += 1
        else:
            print("✓ Returned None on API error")

    return failed


def test_hainterface_get_history_from_time(my_predbat=None):
    """Test get_history() with from_time parameter"""
    print("\n=== Testing HAInterface get_history() with from_time ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    now = datetime.now()
    from_time = now - timedelta(hours=2)

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, [[]])

        result = ha_interface.get_history("sensor.battery", now, from_time=from_time)

        # Verify API called with from_time in path
        if not mock_get.called:
            print("ERROR: API should be called")
            failed += 1
        else:
            call_args = mock_get.call_args
            url = call_args[0][0]
            # from_time should be in the path as /api/history/period/{from_time}
            expected_time_str = from_time.strftime("%Y-%m-%dT%H:%M:%S")
            if expected_time_str not in url:
                print(f"ERROR: from_time {expected_time_str} not in URL: {url}")
                failed += 1
            else:
                print("✓ from_time parameter used correctly")

    return failed


def run_hainterface_api_tests(my_predbat):
    """Run all HAInterface API tests"""
    print("\n" + "=" * 80)
    print("HAInterface API Tests")
    print("=" * 80)

    failed = 0
    failed += test_hainterface_api_call_get(my_predbat)
    failed += test_hainterface_api_call_post(my_predbat)
    failed += test_hainterface_api_call_no_key(my_predbat)
    failed += test_hainterface_api_call_supervisor(my_predbat)
    failed += test_hainterface_api_call_json_decode_error(my_predbat)
    failed += test_hainterface_api_call_timeout(my_predbat)
    failed += test_hainterface_api_call_read_timeout(my_predbat)
    failed += test_hainterface_api_call_silent_mode(my_predbat)
    failed += test_hainterface_api_call_error_limit(my_predbat)
    failed += test_hainterface_api_call_error_reset(my_predbat)
    failed += test_hainterface_initialize_addon_check(my_predbat)
    failed += test_hainterface_initialize_no_addon(my_predbat)
    failed += test_hainterface_get_history_basic(my_predbat)
    failed += test_hainterface_get_history_no_key(my_predbat)
    failed += test_hainterface_get_history_api_error(my_predbat)
    failed += test_hainterface_get_history_from_time(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAInterface API tests passed!")
    else:
        print(f"❌ {failed} HAInterface API test(s) failed")
    print("=" * 80)

    return failed
