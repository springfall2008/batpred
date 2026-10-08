# fmt: off
"""
HAInterface API Tests

Tests for HAInterface API-related methods:
- api_call() - GET/POST requests with error handling
- initialize() - App/services checks
- get_history() - Historical data fetching
"""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import requests

from tests.test_hainterface_common import MockBase, MockDatabaseManager, create_ha_interface, create_mock_requests_response, make_fake_time
from ha import HAInterface, HA_OUTAGE_LIMIT_SECONDS, HA_RETRY_SECONDS, HA_RETRY_MAX_SECONDS


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

        ha_interface.api_call("/addons/self/info", core=False)

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

        # Verify an outage has been recorded
        if ha_interface.outage.since is None:
            print("ERROR: unavailable_since should be set")
            failed += 1
        else:
            print("✓ outage recorded")

    return failed


def test_hainterface_api_call_timeout(my_predbat=None):
    """Test api_call() handles timeout"""
    print("\n=== Testing HAInterface api_call() timeout ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.Timeout("Connection timeout")

        result = ha_interface.api_call("/api/states")

        # Verify error handled
        if result is not None:
            print(f"ERROR: Should return None on timeout, got {result}")
            failed += 1
        else:
            print("✓ Returned None on timeout")

        # Verify an outage has been recorded
        if ha_interface.outage.since is None:
            print("ERROR: unavailable_since should be set")
            failed += 1
        else:
            print("✓ outage recorded")

    return failed


def test_hainterface_api_call_read_timeout(my_predbat=None):
    """Test api_call() handles ReadTimeout"""
    print("\n=== Testing HAInterface api_call() ReadTimeout ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.ReadTimeout("Read timeout")

        result = ha_interface.api_call("/api/states")

        # Verify error handled
        if result is not None:
            print(f"ERROR: Should return None on read timeout, got {result}")
            failed += 1
        else:
            print("✓ Returned None on ReadTimeout")

        # Verify an outage has been recorded
        if ha_interface.outage.since is None:
            print("ERROR: unavailable_since should be set")
            failed += 1
        else:
            print("✓ outage recorded")

    return failed


def test_hainterface_api_call_silent_mode(my_predbat=None):
    """Test api_call() silent mode suppresses warnings"""
    print("\n=== Testing HAInterface api_call() silent mode ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
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
        ha_interface.api_call("/api/states", silent=True)

        # Verify warning not logged
        if log_called[0]:
            print("ERROR: Warning should be suppressed in silent mode")
            failed += 1
        else:
            print("✓ Warning suppressed in silent mode")

    return failed


def test_hainterface_api_call_outage_tolerated(my_predbat=None):
    """Test api_call() rides out a burst of failures instead of stopping after 10 (#5437, #5354)"""
    print("\n=== Testing HAInterface api_call() outage tolerated ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    fatal_called = [False]
    ha_interface.fatal_error_occurred = lambda: fatal_called.__setitem__(0, True)

    clock = [1000.0]
    with patch("ha.requests.get") as mock_get, patch("ha.time", make_fake_time(clock)):
        mock_get.side_effect = requests.Timeout("Connection timeout")

        # Far more failures than the old limit of 10, all within a few seconds (an HA restart)
        for _ in range(50):
            ha_interface.api_call("/api/states")
            clock[0] += 1.0

        if fatal_called[0]:
            print("ERROR: 50 failures in under a minute should not be fatal")
            failed += 1
        else:
            print("✓ burst of failures is not fatal")

        # Still failing, now every 5 minutes, up to just inside the outage limit
        while clock[0] + 300 < 1000.0 + HA_OUTAGE_LIMIT_SECONDS:
            clock[0] += 300
            ha_interface.api_call("/api/states")
        if fatal_called[0]:
            print("ERROR: should not be fatal before the outage limit")
            failed += 1
        else:
            print("✓ not fatal just inside the outage limit")

        clock[0] = 1000.0 + HA_OUTAGE_LIMIT_SECONDS
        ha_interface.api_call("/api/states")
        if not fatal_called[0]:
            print("ERROR: fatal_error_occurred should be called once the outage reaches the limit")
            failed += 1
        else:
            print("✓ fatal_error_occurred called at the outage limit")

    return failed


def test_hainterface_api_call_outage_cleared(my_predbat=None):
    """Test api_call() success ends the outage so the limit is measured from the next failure"""
    print("\n=== Testing HAInterface api_call() outage cleared ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    fatal_called = [False]
    ha_interface.fatal_error_occurred = lambda: fatal_called.__setitem__(0, True)

    clock = [1000.0]
    with patch("ha.requests.get") as mock_get, patch("ha.time", make_fake_time(clock)):
        mock_get.side_effect = requests.Timeout("Connection timeout")
        ha_interface.api_call("/api/states")
        if ha_interface.outage.since != 1000.0:
            print(f"ERROR: outage should start at 1000.0, got {ha_interface.outage.since}")
            failed += 1

        clock[0] = 1100.0
        mock_get.side_effect = None
        mock_get.return_value = create_mock_requests_response(200, {"result": "success"})
        ha_interface.api_call("/api/states")
        if ha_interface.outage.since is not None:
            print("ERROR: unavailable_since should be cleared by a successful call")
            failed += 1
        else:
            print("✓ outage cleared on success")

        # A later failure starts a new outage, so a long gap since the first one does not count
        clock[0] = 1000.0 + 2 * HA_OUTAGE_LIMIT_SECONDS
        mock_get.side_effect = requests.Timeout("Connection timeout")
        ha_interface.api_call("/api/states")
        if fatal_called[0]:
            print("ERROR: a new outage should not inherit the earlier one's start time")
            failed += 1
        else:
            print("✓ new outage measured from its own start")

    return failed


def test_hainterface_api_call_supervisor_failure_not_outage(my_predbat=None):
    """Test a failing Supervisor call (expected in Docker installs) is not counted as Home Assistant being down"""
    print("\n=== Testing HAInterface api_call() Supervisor failure ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get:
        mock_env.return_value = "test_supervisor_token"
        mock_get.side_effect = requests.exceptions.ConnectionError("no supervisor")
        ha_interface.api_call("/addons/self/info", core=False, silent=True)

    if ha_interface.outage.since is not None:
        print("ERROR: a Supervisor failure must not start an outage")
        failed += 1
    else:
        print("✓ Supervisor failure ignored")

    return failed


def test_hainterface_initialize_retries_until_ready(my_predbat=None):
    """Test initialize() waits for Home Assistant to start serving its API rather than failing at once (#5437, #5134)"""
    print("\n=== Testing HAInterface initialize() retries until ready ===")
    failed = 0

    mock_base = MockBase()
    sleeps = []

    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get, patch("ha.time", make_fake_time([1000.0], sleeps)):
        mock_env.return_value = "test_supervisor_token"
        mock_get.side_effect = [
            requests.exceptions.ConnectionError("supervisor down"),  # app info
            create_mock_requests_response(502, json_error=True),  # /api/services: HTML 502 page, not JSON
            requests.Timeout("still starting"),
            create_mock_requests_response(200, [{"domain": "homeassistant"}]),
        ]

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

        try:
            ha_interface.initialize("http://test:8123", "test_key", False, False, False)
        except ValueError:
            print("ERROR: initialize() should have waited for Home Assistant, not raised")
            failed += 1
            return failed

    if sleeps != [HA_RETRY_SECONDS, HA_RETRY_SECONDS * 2]:
        print(f"ERROR: expected two backed-off pauses, got {sleeps}")
        failed += 1
    else:
        print("✓ retried with backoff")

    if mock_base.ha_interface is not ha_interface:
        print("ERROR: base.ha_interface should be set once Home Assistant answers")
        failed += 1
    else:
        print("✓ interface registered after the retries")

    if ha_interface.outage.since is not None:
        print("ERROR: the outage should have ended")
        failed += 1

    return failed


def test_hainterface_initialize_gives_up_after_limit(my_predbat=None):
    """Test initialize() still raises once Home Assistant has been unreachable for the outage limit"""
    print("\n=== Testing HAInterface initialize() gives up after the outage limit ===")
    failed = 0

    mock_base = MockBase()
    clock = [1000.0]

    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get, patch("ha.time", make_fake_time(clock)):
        mock_env.return_value = "test_supervisor_token"
        mock_get.side_effect = requests.exceptions.ConnectionError("down")

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

        raised = False
        try:
            ha_interface.initialize("http://test:8123", "test_key", False, False, False)
        except ValueError:
            raised = True

    if not raised:
        print("ERROR: initialize() should raise ValueError when Home Assistant never answers")
        failed += 1
    else:
        print("✓ raised after the outage limit")

    waited = clock[0] - 1000.0
    if waited < HA_OUTAGE_LIMIT_SECONDS - HA_RETRY_MAX_SECONDS or waited > HA_OUTAGE_LIMIT_SECONDS:
        print(f"ERROR: should wait close to the {HA_OUTAGE_LIMIT_SECONDS}s limit, waited {waited}s")
        failed += 1
    else:
        print(f"✓ waited {waited:.0f}s before giving up")

    return failed


def test_hainterface_api_call_request_exception_is_outage(my_predbat=None):
    """Test api_call() treats other requests failures seen while HA goes down (e.g. a reset mid-body) as an outage, not a crash"""
    print("\n=== Testing HAInterface api_call() request exception ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.ChunkedEncodingError("Connection reset by peer")
        try:
            result = ha_interface.api_call("/api/states")
        except requests.exceptions.RequestException:
            print("ERROR: the exception should not escape api_call()")
            return failed + 1

    if result is not None or ha_interface.outage.since is None:
        print(f"ERROR: expected None and a recorded outage, got {result} / {ha_interface.outage.since}")
        failed += 1
    else:
        print("✓ request exception recorded as an outage")

    return failed


def test_hainterface_api_call_auth_rejected(my_predbat=None):
    """Test api_call() flags a rejected ha_key instead of treating it as Home Assistant being down"""
    print("\n=== Testing HAInterface api_call() auth rejected ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(401, json_error=True)
        result = ha_interface.api_call("/api/states")

    if result is not None or not ha_interface.auth_rejected:
        print(f"ERROR: expected None and auth_rejected, got {result} / {ha_interface.auth_rejected}")
        failed += 1
    elif ha_interface.outage.since is not None:
        print("ERROR: a rejected key is not an outage")
        failed += 1
    elif not any("rejected the ha_key" in log for log in mock_base.log_messages):
        print("ERROR: should log that the key was rejected")
        failed += 1
    else:
        print("✓ rejected key flagged and logged")

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, {"ok": True})
        ha_interface.api_call("/api/states")
    if ha_interface.auth_rejected:
        print("ERROR: a successful call should clear auth_rejected")
        failed += 1
    else:
        print("✓ cleared by a successful call")

    return failed


def test_hainterface_initialize_auth_rejected_fails_fast(my_predbat=None):
    """Test initialize() does not wait 15 minutes for a ha_key that Home Assistant rejects"""
    print("\n=== Testing HAInterface initialize() auth rejected ===")
    failed = 0

    mock_base = MockBase()
    sleeps = []

    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get, patch("ha.time", make_fake_time([1000.0], sleeps)):
        mock_env.return_value = None  # no Supervisor
        mock_get.return_value = create_mock_requests_response(401, json_error=True)

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

        raised = False
        try:
            ha_interface.initialize("http://test:8123", "bad_key", False, False, False)
        except ValueError:
            raised = True

    if not raised or sleeps:
        print(f"ERROR: expected an immediate ValueError, raised={raised} sleeps={sleeps}")
        failed += 1
    else:
        print("✓ failed immediately")

    return failed


def test_hainterface_api_call_outage_gap_starts_new_outage(my_predbat=None):
    """Test two failures far apart are separate outages, not one 15 minute outage with nothing known about the gap"""
    print("\n=== Testing HAInterface api_call() outage gap ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    fatal_called = [False]
    ha_interface.fatal_error_occurred = lambda: fatal_called.__setitem__(0, True)

    clock = [1000.0]
    with patch("ha.requests.get") as mock_get, patch("ha.time", make_fake_time(clock)):
        mock_get.side_effect = requests.Timeout("Connection timeout")
        ha_interface.api_call("/api/states")

        # Nothing calls Home Assistant for 20 minutes, then one more transient failure
        clock[0] += 2 * HA_OUTAGE_LIMIT_SECONDS // 3 + 60
        ha_interface.api_call("/api/states")
        if fatal_called[0]:
            print("ERROR: a failure after a long silence must not inherit the earlier failure's outage")
            failed += 1
        elif ha_interface.outage.since != clock[0]:
            print(f"ERROR: expected a new outage starting at {clock[0]}, got {ha_interface.outage.since}")
            failed += 1
        else:
            print("✓ new outage started after the gap")

        # Failures that keep coming within the gap are still one outage
        for _ in range(HA_OUTAGE_LIMIT_SECONDS // 300):
            clock[0] += 300
            ha_interface.api_call("/api/states")
        if not fatal_called[0]:
            print("ERROR: failures every 5 minutes for the outage limit should be fatal")
            failed += 1
        else:
            print("✓ failures within the gap accumulate")

    return failed


def test_hainterface_api_call_auth_rejected_ends_outage(my_predbat=None):
    """Test a 401 proves Home Assistant is up, so it ends an outage in progress"""
    print("\n=== Testing HAInterface api_call() 401 ends outage ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.side_effect = requests.Timeout("Connection timeout")
        ha_interface.api_call("/api/states")
        if ha_interface.outage.since is None:
            print("ERROR: outage should have started")
            failed += 1

        mock_get.side_effect = None
        mock_get.return_value = create_mock_requests_response(401, json_error=True)
        ha_interface.api_call("/api/states")

    if ha_interface.outage.since is not None or not ha_interface.auth_rejected:
        print(f"ERROR: expected the outage ended and auth_rejected set, got {ha_interface.outage.since} / {ha_interface.auth_rejected}")
        failed += 1
    else:
        print("✓ 401 ended the outage")

    return failed


def test_hainterface_api_call_auth_rejected_repeatedly_is_fatal(my_predbat=None):
    """Test a ha_key rejected on 10 calls in a row (revoked while running) stops Predbat, as before, even with a JSON body"""
    print("\n=== Testing HAInterface api_call() repeated 401 ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)
    fatal_called = [False]
    ha_interface.fatal_error_occurred = lambda: fatal_called.__setitem__(0, True)

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(401, {"message": "Unauthorized"})
        for _ in range(9):
            ha_interface.api_call("/api/states")
        if fatal_called[0] or not ha_interface.auth_rejected:
            print(f"ERROR: 9 rejections should flag auth_rejected without being fatal, fatal={fatal_called[0]} flagged={ha_interface.auth_rejected}")
            failed += 1
        ha_interface.api_call("/api/states")
        if not fatal_called[0]:
            print("ERROR: the 10th rejection should be fatal")
            failed += 1
        else:
            print("✓ fatal after 10 rejected calls")

        # Any good reply starts the count again
        fatal_called[0] = False
        mock_get.return_value = create_mock_requests_response(200, {"ok": True})
        ha_interface.api_call("/api/states")
        if ha_interface.auth_failures != 0 or ha_interface.auth_rejected:
            print("ERROR: a successful call should reset the rejection count")
            failed += 1
        else:
            print("✓ count reset by a successful call")

    return failed


def test_hainterface_api_call_gateway_status_is_outage(my_predbat=None):
    """Test a 502/503/504 counts as an outage even when the gateway's body happens to be JSON"""
    print("\n=== Testing HAInterface api_call() gateway status ===")
    failed = 0

    mock_base = MockBase()
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)

    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(503, {"message": "Service Unavailable"})
        result = ha_interface.api_call("/api/states")

    if result is not None or ha_interface.outage.since is None:
        print(f"ERROR: expected None and a recorded outage, got {result} / {ha_interface.outage.since}")
        failed += 1
    else:
        print("✓ JSON 503 recorded as an outage")

    # And it is not cleared by the next 503, only by a real reply
    with patch("ha.requests.get") as mock_get:
        mock_get.return_value = create_mock_requests_response(200, {"ok": True})
        ha_interface.api_call("/api/states")
    if ha_interface.outage.since is not None:
        print("ERROR: a 200 should end the outage")
        failed += 1
    else:
        print("✓ ended by a real reply")

    return failed


def test_hainterface_initialize_app_check(my_predbat=None):
    """Test initialize() checks for app/services"""
    print("\n=== Testing HAInterface initialize() app check ===")
    failed = 0

    mock_base = MockBase()

    # Mock both app info and services calls in initialize()
    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get:
        mock_env.return_value = "test_supervisor_token"  # Mock SUPERVISOR_TOKEN
        mock_get.side_effect = [
            create_mock_requests_response(200, {"data": {"slug": "predbat_app"}}),  # app info
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
        if ha_interface.slug != "predbat_app":
            print(f"ERROR: Slug should be 'predbat_app', got {ha_interface.slug}")
            failed += 1
        else:
            print("✓ App slug detected correctly")

    return failed


def test_hainterface_initialize_no_app(my_predbat=None):
    """Test initialize() handles missing app gracefully"""
    print("\n=== Testing HAInterface initialize() no app ===")
    failed = 0

    mock_base = MockBase()

    with patch("ha.os.environ.get") as mock_env, patch("ha.requests.get") as mock_get:
        # Mock supervisor token
        mock_env.return_value = "test_supervisor_token"
        # Mock app call returns None (supervisor timeout), services call success
        mock_get.side_effect = [
            requests.Timeout("Supervisor timeout"),  # app info fails with timeout
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
            print("✓ Missing app handled gracefully")

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

        ha_interface.get_history("sensor.battery", now, from_time=from_time)

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
    failed += test_hainterface_api_call_outage_tolerated(my_predbat)
    failed += test_hainterface_api_call_outage_cleared(my_predbat)
    failed += test_hainterface_api_call_supervisor_failure_not_outage(my_predbat)
    failed += test_hainterface_api_call_request_exception_is_outage(my_predbat)
    failed += test_hainterface_api_call_auth_rejected(my_predbat)
    failed += test_hainterface_api_call_auth_rejected_ends_outage(my_predbat)
    failed += test_hainterface_api_call_auth_rejected_repeatedly_is_fatal(my_predbat)
    failed += test_hainterface_api_call_gateway_status_is_outage(my_predbat)
    failed += test_hainterface_api_call_outage_gap_starts_new_outage(my_predbat)
    failed += test_hainterface_initialize_app_check(my_predbat)
    failed += test_hainterface_initialize_no_app(my_predbat)
    failed += test_hainterface_initialize_retries_until_ready(my_predbat)
    failed += test_hainterface_initialize_gives_up_after_limit(my_predbat)
    failed += test_hainterface_initialize_auth_rejected_fails_fast(my_predbat)
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
