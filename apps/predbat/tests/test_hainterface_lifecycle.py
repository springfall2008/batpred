# fmt: off
"""
Unit tests for HAInterface lifecycle methods.

Tests cover:
- initialize() with different configurations
- is_alive() under various conditions
- wait_api_started() success and timeout
"""

from unittest.mock import patch

from tests.test_hainterface_common import MockBase, create_ha_interface, create_mock_requests_response
from tests.test_infra import run_async
from ha import HAInterface


def test_hainterface_initialize_ha_only(my_predbat=None):
    """Test initialize() with HA only (no DB)"""
    print("\n=== Testing HAInterface initialize() HA only ===")
    failed = 0

    mock_base = MockBase()

    # Manually create instance
    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, [{"domain": "test"}])
        ha_interface.initialize("http://test", "test_key", False, False, False)

    if ha_interface.ha_url != "http://test":
        print(f"ERROR: Expected ha_url 'http://test', got '{ha_interface.ha_url}'")
        failed += 1
    elif ha_interface.ha_key != "test_key":
        print(f"ERROR: Expected ha_key 'test_key'")
        failed += 1
    elif ha_interface.db_enable:
        print("ERROR: db_enable should be False")
        failed += 1
    elif ha_interface.websocket_active:
        print("ERROR: websocket_active should be False initially")
        failed += 1
    else:
        print("✓ Initialized with HA only correctly")

    return failed


def test_hainterface_initialize_db_primary(my_predbat=None):
    """Test initialize() with DB primary (no HA key)"""
    print("\n=== Testing HAInterface initialize() DB primary ===")
    failed = 0

    mock_base = MockBase()

    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False

    ha_interface.initialize("http://test", None, True, False, True)

    if ha_interface.ha_key is not None:
        print("ERROR: ha_key should be None")
        failed += 1
    elif not ha_interface.db_enable:
        print("ERROR: db_enable should be True")
        failed += 1
    elif not ha_interface.db_primary:
        print("ERROR: db_primary should be True")
        failed += 1
    else:
        print("✓ Initialized with DB primary correctly")

    if not any("SQL Lite database as primary" in log for log in mock_base.log_messages):
        print("ERROR: Should log DB primary mode")
        failed += 1
    else:
        print("✓ DB primary mode logged")

    return failed


def test_hainterface_initialize_no_key_no_db(my_predbat=None):
    """Test initialize() with no key and no DB raises ValueError"""
    print("\n=== Testing HAInterface initialize() no key/DB ===")
    failed = 0

    mock_base = MockBase()

    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False

    try:
        ha_interface.initialize("http://test", None, False, False, False)
        print("ERROR: Should raise ValueError")
        failed += 1
    except ValueError:
        print("✓ ValueError raised correctly")

    return failed


def test_hainterface_initialize_api_check_failed(my_predbat=None):
    """Test initialize() with failed API check raises ValueError"""
    print("\n=== Testing HAInterface initialize() API check failed ===")
    failed = 0

    mock_base = MockBase()

    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False

    with patch("ha.requests.get") as mock_get:
        # First call (addon check) returns None, second call (services check) returns None
        mock_get.return_value = create_mock_requests_response(500, None)

        try:
            ha_interface.initialize("http://test", "test_key", False, False, False)
            print("ERROR: Should raise ValueError")
            failed += 1
        except ValueError:
            print("✓ ValueError raised on API check failure")

    if not any("Unable to connect" in log for log in mock_base.log_messages):
        print("ERROR: Should log connection failure")
        failed += 1
    else:
        print("✓ Connection failure logged")

    return failed


def test_hainterface_initialize_db_mirror(my_predbat=None):
    """Test initialize() with DB mirroring enabled"""
    print("\n=== Testing HAInterface initialize() DB mirror ===")
    failed = 0

    mock_base = MockBase()

    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, [{"domain": "test"}])
        ha_interface.initialize("http://test", "test_key", True, True, False)

    if not ha_interface.db_enable:
        print("ERROR: db_enable should be True")
        failed += 1
    elif not ha_interface.db_mirror_ha:
        print("ERROR: db_mirror_ha should be True")
        failed += 1
    else:
        print("✓ DB mirroring enabled correctly")

    return failed


def test_hainterface_is_alive_not_started(my_predbat=None):
    """Test is_alive() when API not started"""
    print("\n=== Testing HAInterface is_alive() not started ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")
    ha_interface.api_started = False

    if ha_interface.is_alive():
        print("ERROR: Should return False when not started")
        failed += 1
    else:
        print("✓ Returns False when not started")

    return failed


def test_hainterface_is_alive_no_websocket(my_predbat=None):
    """Test is_alive() with ha_key but no websocket"""
    print("\n=== Testing HAInterface is_alive() no websocket ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")
    ha_interface.api_started = True
    ha_interface.websocket_active = False

    if ha_interface.is_alive():
        print("ERROR: Should return False with ha_key but no websocket")
        failed += 1
    else:
        print("✓ Returns False without websocket")

    return failed


def test_hainterface_is_alive_websocket_active(my_predbat=None):
    """Test is_alive() with websocket active"""
    print("\n=== Testing HAInterface is_alive() websocket active ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", websocket_active=True)
    ha_interface.api_started = True

    if not ha_interface.is_alive():
        print("ERROR: Should return True with websocket active")
        failed += 1
    else:
        print("✓ Returns True with websocket active")

    return failed


def test_hainterface_is_alive_db_only(my_predbat=None):
    """Test is_alive() in DB-only mode (no ha_key)"""
    print("\n=== Testing HAInterface is_alive() DB only ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key=None, db_enable=True, db_primary=True)
    ha_interface.api_started = True

    if not ha_interface.is_alive():
        print("ERROR: Should return True in DB-only mode")
        failed += 1
    else:
        print("✓ Returns True in DB-only mode")

    return failed


def test_hainterface_wait_api_started_success(my_predbat=None):
    """Test wait_api_started() successful start"""
    print("\n=== Testing HAInterface wait_api_started() success ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")
    ha_interface.api_started = True

    # Mock time.sleep to avoid actual waiting
    with patch("ha.time.sleep"):
        result = ha_interface.wait_api_started()

    if not result:
        print("ERROR: Should return True when already started")
        failed += 1
    else:
        print("✓ Returns True when API started")

    return failed


def test_hainterface_wait_api_started_timeout(my_predbat=None):
    """Test wait_api_started() timeout"""
    print("\n=== Testing HAInterface wait_api_started() timeout ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key")
    ha_interface.api_started = False

    # Mock time.sleep and make it count iterations
    sleep_count = [0]
    def mock_sleep(seconds):
        sleep_count[0] += 1
        if sleep_count[0] > 5:  # Limit iterations for test
            return

    with patch("ha.time.sleep", side_effect=mock_sleep):
        # Set max count to trigger timeout quickly
        original_count = 0
        result = ha_interface.wait_api_started()
        # After 240 iterations without api_started, should return False

    # Since api_started stays False, should timeout
    if result:
        print("ERROR: Should return False on timeout")
        failed += 1
    else:
        print("✓ Returns False on timeout")

    if not any("Failed to start" in log for log in mock_base.log_messages):
        print("ERROR: Should log timeout warning")
        failed += 1
    else:
        print("✓ Timeout warning logged")

    return failed


def test_hainterface_get_slug(my_predbat=None):
    """Test get_slug() returns addon slug"""
    print("\n=== Testing HAInterface get_slug() ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = object.__new__(HAInterface)
    ha_interface.base = mock_base
    ha_interface.log = mock_base.log
    ha_interface.api_started = False
    ha_interface.api_stop = False

    with patch("ha.requests.get") as mock_get:
        # Mock addon info response
        def mock_get_side_effect(url, *args, **kwargs):
            if "/addons/self/info" in url:
                return create_mock_requests_response(200, {"data": {"slug": "predbat_addon"}})
            else:
                return create_mock_requests_response(200, [{"domain": "test"}])

        mock_get.side_effect = mock_get_side_effect

        with patch("ha.os.environ.get", return_value="test_token"):
            ha_interface.initialize("http://test", "test_key", False, False, False)

    slug = ha_interface.get_slug()
    if slug != "predbat_addon":
        print(f"ERROR: Expected slug 'predbat_addon', got '{slug}'")
        failed += 1
    else:
        print("✓ Slug retrieved correctly")

    return failed


def test_hainterface_start_with_websocket(my_predbat=None):
    """Test HAInterface start() method with websocket"""
    print("\n=== Testing HAInterface start() with websocket ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", ha_url="http://localhost:8123")

    # Mock socketLoop to avoid actually running it
    socketloop_called = [False]
    async def mock_socketloop():
        socketloop_called[0] = True
        ha_interface.api_stop = True  # Exit immediately

    ha_interface.socketLoop = mock_socketloop

    # Run start()
    run_async(ha_interface.start())

    if not socketloop_called[0]:
        print("ERROR: socketLoop should be called")
        failed += 1
    else:
        print("✓ socketLoop called")

    if ha_interface.websocket_active != True:
        print("ERROR: websocket_active should be True")
        failed += 1
    else:
        print("✓ websocket_active set to True")

    if ha_interface.api_started != False:
        print("ERROR: api_started should be False after exit")
        failed += 1
    else:
        print("✓ api_started set to False on exit")

    if not any("Starting HA interface" in log for log in mock_base.log_messages):
        print("ERROR: Should log 'Starting HA interface'")
        failed += 1
    else:
        print("✓ Startup message logged")

    if not any("HA interface stopped" in log for log in mock_base.log_messages):
        print("ERROR: Should log 'HA interface stopped'")
        failed += 1
    else:
        print("✓ Stop message logged")

    return failed


def test_hainterface_start_dummy_mode(my_predbat=None):
    """Test HAInterface start() method in dummy mode (no ha_key)"""
    print("\n=== Testing HAInterface start() dummy mode ===")
    failed = 0

    mock_base = MockBase()
    # Create without ha_key to trigger dummy mode
    ha_interface = create_ha_interface(mock_base, ha_key=None, ha_url=None)

    # Track sleep calls
    sleep_count = [0]
    async def mock_sleep(delay):
        sleep_count[0] += 1
        if sleep_count[0] >= 3:  # After 3 sleeps (15 seconds), stop
            ha_interface.api_stop = True

    with patch("ha.asyncio.sleep", new=mock_sleep):
        run_async(ha_interface.start())

    if not any("Starting Dummy HA interface" in log for log in mock_base.log_messages):
        print("ERROR: Should log 'Starting Dummy HA interface'")
        failed += 1
    else:
        print("✓ Dummy startup message logged")

    if ha_interface.api_started != False:
        print("ERROR: api_started should be False after exit")
        failed += 1
    else:
        print("✓ api_started set to False on exit")

    if sleep_count[0] < 3:
        print(f"ERROR: Expected at least 3 sleep calls, got {sleep_count[0]}")
        failed += 1
    else:
        print(f"✓ Sleep called {sleep_count[0]} times")

    if not any("HA interface stopped" in log for log in mock_base.log_messages):
        print("ERROR: Should log 'HA interface stopped'")
        failed += 1
    else:
        print("✓ Stop message logged")

    return failed


def run_hainterface_lifecycle_tests(my_predbat):
    """Run all HAInterface lifecycle tests"""
    print("\n" + "=" * 80)
    print("HAInterface Lifecycle Tests")
    print("=" * 80)

    failed = 0
    failed += test_hainterface_initialize_ha_only(my_predbat)
    failed += test_hainterface_initialize_db_primary(my_predbat)
    failed += test_hainterface_initialize_no_key_no_db(my_predbat)
    failed += test_hainterface_initialize_api_check_failed(my_predbat)
    failed += test_hainterface_initialize_db_mirror(my_predbat)
    failed += test_hainterface_is_alive_not_started(my_predbat)
    failed += test_hainterface_is_alive_no_websocket(my_predbat)
    failed += test_hainterface_is_alive_websocket_active(my_predbat)
    failed += test_hainterface_is_alive_db_only(my_predbat)
    failed += test_hainterface_wait_api_started_success(my_predbat)
    failed += test_hainterface_wait_api_started_timeout(my_predbat)
    failed += test_hainterface_get_slug(my_predbat)
    failed += test_hainterface_start_with_websocket(my_predbat)
    failed += test_hainterface_start_dummy_mode(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAInterface lifecycle tests passed!")
    else:
        print(f"❌ {failed} HAInterface lifecycle test(s) failed")
    print("=" * 80)

    return failed
