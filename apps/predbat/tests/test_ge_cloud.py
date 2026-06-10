# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on

from gecloud import GECloudDirect, GECloudData, regname_to_ha
from gecloud import GE_API_DEVICES, GE_API_EVC_SEND_COMMAND
from utils import dp4
import asyncio
import json
from unittest.mock import MagicMock, patch, AsyncMock
import tempfile
from datetime import datetime, timedelta, timezone
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session, run_async
from storage import StorageLocalFiles


class MockGECloudDirect(GECloudDirect):
    """Mock GECloudDirect class for testing without ComponentBase dependencies"""

    def __init__(self):
        # Don't call parent __init__ to avoid ComponentBase
        self.api_key = "test_api_key"
        self.automatic = True
        self.mock_api_responses = {}
        self.dashboard_items = {}
        self.log_messages = []
        self.config_args = {}
        self.prefix = "predbat"  # Add prefix attribute for entity naming

        # Initialise instance variables that GECloudDirect expects
        self.requests_total = 0
        self.failures_total = 0
        self.api_auth_failed = False
        self.auth_denied_reported = False
        self.register_list = {}
        self.settings = {}
        self.status = {}
        self.meter = {}
        self.info = {}
        self.device_list = []
        self.evc_device_list = []
        self.evc_device = {}
        self.evc_data = {}
        self.evc_sessions = {}
        self.pending_writes = {}
        self.register_entity_map = {}
        self.polling_mode = False
        self.devices_dict = {}
        self.evc_devices_dict = []
        self.ems_device = None
        self.gateway_device = None
        self._now_utc_exact = datetime.now(timezone.utc)
        self.settings_from_cache = False
        self.default_options_done = False
        self._read_only = False

        class MockHAInterface:
            def __init__(self):
                self.external_states = {}

            async def set_state_external(self, entity_id, state):
                self.external_states[entity_id] = state

        class MockBase:
            def __init__(self):
                self.ha_interface = MockHAInterface()

        self.base = MockBase()

    @property
    def now_utc_exact(self):
        """Mock now_utc_exact property"""
        return self._now_utc_exact

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Mock dashboard_item - tracks calls"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes}

    def get_arg(self, name, default=None):
        """Mock get_arg"""
        return self.config_args.get(name, default)

    def set_arg(self, name, value):
        """Mock set_arg"""
        self.config_args[name] = value

    def get_state_wrapper(self, entity_id, default=None):
        """Mock get_state_wrapper"""
        if "_set_read_only" in entity_id:
            return "on" if self._read_only else "off"
        return default

    def update_success_timestamp(self):
        """Mock update_success_timestamp"""
        pass

    @property
    def storage(self):
        """Mock storage property - returns injected mock or None"""
        return getattr(self, "_mock_storage", None)


class MockGECloudData(GECloudData):
    """Mock GECloudData class for testing without ComponentBase dependencies"""

    def __init__(self, config_root="/tmp"):
        # Don't call parent initialise
        self.ge_cloud_key = "test_api_key"
        self.ge_cloud_serial_config_item = "ge_cloud_serial"
        self.ge_cloud_serial = "test123"
        self.days_previous = [7, 30]
        self.max_days_previous = 31
        self.api_fatal = False
        self.ge_url_cache = {}
        self.ge_cloud_data = True
        self.mdata = []
        self.requests_total = 0
        self.failures_total = 0
        self.oldest_data_time = None
        self._config_root = config_root
        self.log_messages = []
        self.config_args = {}
        self._now_utc_exact = datetime.now(timezone.utc)

    @property
    def config_root(self):
        """Mock config_root property"""
        return self._config_root

    @property
    def now_utc_exact(self):
        """Mock now_utc_exact property"""
        return self._now_utc_exact

    def log(self, message):
        """Mock log method"""
        self.log_messages.append(message)

    def get_arg(self, name, default=None):
        """Mock get_arg"""
        return self.config_args.get(name, default)

    def update_success_timestamp(self):
        """Mock update_success_timestamp"""
        pass

    @property
    def storage(self):
        """Mock storage property - returns injected mock or None"""
        return getattr(self, "_mock_storage", None)


# =============================================================================


def test_ge_cloud(my_predbat=None):
    """
    ======================================================================
    GE CLOUD API TEST SUITE
    ======================================================================
    Comprehensive test suite for GivEnergy Cloud integration including:
    - API infrastructure (success, auth errors, rate limits, timeouts, JSON errors, retry logic)
    - Device management (EMS, gateway, batteries, EV chargers, smart devices)
    - EVC operations (commands, device data, sessions)
    - Inverter operations (status, meter, device info, settings read/write)
    - Event handlers (switch, number, select)
    - Data publishing (status, meter, info, registers, EVC data)
    - Configuration (automatic config, enable defaults)
    - Data download (single day, multi-day, pagination)
    - Cache management (hit, miss, clean, persist, corrupt file)
    - Utility functions (regname_to_ha, get_data)
    """
    print("\n" + "=" * 70)
    print("GE CLOUD API TEST SUITE")
    print("=" * 70)

    # Sub-test registry - each entry is (key, function, description)
    sub_tests = [
        ("api_success", _test_async_get_inverter_data_success, "API call success"),
        ("api_auth_error", _test_async_get_inverter_data_auth_error, "API auth error handling"),
        ("api_auth_sets_flag", _test_async_get_inverter_data_auth_sets_flag, "API 403 sets api_auth_failed flag"),
        ("api_success_clears_auth_flag", _test_async_get_inverter_data_success_clears_auth_flag, "API success clears api_auth_failed flag"),
        ("publish_status_auth_unavailable", _test_publish_status_auth_failure_marks_time_unavailable, "Auth failure marks time sensor unavailable"),
        ("run_auth_denied_scoped", _test_run_reports_auth_denied_scoped, "Auth-denied status scoped to inverter poll cycle"),
        ("non_auth_clears_auth_flag", _test_async_get_inverter_data_non_auth_clears_flag, "Non-401/403 response clears api_auth_failed"),
        ("auth_status_transition_only", _test_run_auth_status_reported_on_transition_only, "Auth-denied status reported once per episode"),
        ("api_rate_limit", _test_async_get_inverter_data_rate_limit, "API rate limit handling"),
        ("api_timeout", _test_async_get_inverter_data_timeout, "API timeout handling"),
        ("api_json_error", _test_async_get_inverter_data_json_error, "API JSON error handling"),
        ("api_retry", _test_async_get_inverter_data_retry, "API retry logic"),
        ("api_post", _test_async_get_inverter_data_post, "API POST with/without datain"),
        ("devices_ems", _test_async_get_devices_with_ems, "Get devices with EMS"),
        ("devices_gateway", _test_async_get_devices_with_gateway, "Get devices with Gateway"),
        ("devices_batteries", _test_async_get_devices_with_batteries, "Get devices with batteries"),
        ("devices_legacy_battery", _test_async_get_devices_legacy_battery, "Get devices with legacy battery (empty connections)"),
        ("devices_empty", _test_async_get_devices_empty, "Get empty devices"),
        ("evc_devices", _test_async_get_evc_devices, "Get EV charger devices"),
        ("smart_devices", _test_async_get_smart_devices, "Get smart devices"),
        ("evc_commands", _test_async_get_evc_commands, "Get EV charger commands"),
        ("evc_device_data", _test_async_get_evc_device_data, "Get EV charger device data"),
        ("evc_device", _test_async_get_evc_device, "Get EV charger device"),
        ("send_evc_command", _test_async_send_evc_command, "Send EV charger command"),
        ("smart_device", _test_async_get_smart_device, "Get smart device"),
        ("evc_sessions", _test_async_get_evc_sessions, "Get EV charger sessions"),
        ("run_method", _test_run_method, "Run method execution"),
        ("settings_saved_to_storage", _test_settings_saved_to_storage, "Settings saved to storage after poll"),
        ("settings_restored_from_cache", _test_settings_restored_from_fresh_cache, "Settings restored from fresh storage cache"),
        ("inverter_status", _test_async_get_inverter_status, "Get inverter status"),
        ("inverter_meter", _test_async_get_inverter_meter, "Get inverter meter"),
        ("device_info", _test_async_get_device_info, "Get device info"),
        ("settings_success", _test_async_get_inverter_settings_success, "Get inverter settings success"),
        ("settings_partial", _test_async_get_inverter_settings_partial_failure, "Get inverter settings partial failure"),
        ("read_setting", _test_async_read_inverter_setting_success, "Read inverter setting success"),
        ("read_errors", _test_async_read_inverter_setting_error_codes, "Read inverter setting error codes"),
        ("write_success", _test_async_write_inverter_setting_success, "Write inverter setting success"),
        ("write_failure", _test_async_write_inverter_setting_failure, "Write inverter setting failure"),
        ("switch_event", _test_switch_event, "Switch event handler"),
        ("number_event", _test_number_event, "Number event handler"),
        ("select_event", _test_select_event, "Select event handler"),
        ("publish_status", _test_publish_status, "Publish status"),
        ("publish_meter", _test_publish_meter, "Publish meter"),
        ("publish_info", _test_publish_info, "Publish info"),
        ("publish_info_soh", _test_publish_info_soh, "Publish info SOH calculation"),
        ("publish_registers", _test_publish_registers, "Publish registers"),
        ("publish_evc_data", _test_publish_evc_data, "Publish EVC data"),
        ("automatic_config", _test_async_automatic_config, "Automatic config"),
        ("hybrid_detection", _test_hybrid_detection, "Hybrid inverter detection"),
        ("enable_defaults", _test_enable_default_options, "Enable default options"),
        ("enable_defaults_read_only", _test_run_read_only_skips_reset, "Enable defaults skipped in read-only mode"),
        ("enable_defaults_after_read_only", _test_run_enables_reset_after_read_only, "Enable defaults on first non-read-only run"),
        ("download_single", _test_download_ge_data_single_day, "Download single day"),
        ("download_multi", _test_download_ge_data_multi_day, "Download multi-day"),
        ("download_pagination", _test_download_ge_data_pagination, "Download pagination"),
        ("cache_hit", _test_get_ge_url_cache_hit, "Cache hit"),
        ("cache_miss", _test_get_ge_url_cache_miss, "Cache miss"),
        ("cache_clean", _test_clean_ge_url_cache, "Cache cleanup"),
        ("cache_persist", _test_load_save_ge_cache, "Cache persistence"),
        ("cache_corrupt", _test_load_ge_cache_corrupt_file, "Cache corrupt file"),
        ("regname_to_ha", _test_regname_to_ha, "Regname to HA conversion"),
        ("get_data", _test_get_data, "Get data method"),
        ("filter_data", _test_filter_data, "Filter data method"),
        ("max_inverter_rate", _test_get_max_inverter_rate_from_model, "Get max inverter rate from model"),
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


def _test_async_get_inverter_data_success(my_predbat):
    """Test successful API call"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Mock successful response
        mock_response = create_aiohttp_mock_response(status=200, json_data={"data": {"serial": "test123", "status": "NORMAL"}})
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result != {"serial": "test123", "status": "NORMAL"}:
                    print("ERROR: Expected dict with data, got {}".format(result))
                    return 1
                if ge_cloud.requests_total != 1:
                    print("ERROR: Expected requests_total=1, got {}".format(ge_cloud.requests_total))
                    return 1
                if ge_cloud.failures_total != 0:
                    print("ERROR: Expected failures_total=0, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_auth_error(my_predbat):
    """Test authentication error (401)"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_response = create_aiohttp_mock_response(status=401, json_data={"error": "Unauthorized"})
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result != {}:
                    print("ERROR: Expected empty dict for 401, got {}".format(result))
                    return 1
                if ge_cloud.failures_total != 1:
                    print("ERROR: Expected failures_total=1, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_auth_sets_flag(my_predbat):
    """Test that a 403 (e.g. GivEnergy Premium / subscription required) sets the api_auth_failed flag"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.api_auth_failed = False

        mock_response = create_aiohttp_mock_response(status=403, json_data={"error": "Forbidden"})
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result != {}:
                    print("ERROR: Expected empty dict for 403, got {}".format(result))
                    return 1
                if not ge_cloud.api_auth_failed:
                    print("ERROR: Expected api_auth_failed=True after 403")
                    return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_success_clears_auth_flag(my_predbat):
    """Test that a successful call clears a previously-set api_auth_failed flag"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.api_auth_failed = True

        mock_response = create_aiohttp_mock_response(status=200, json_data={"data": {"ok": 1}})
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if ge_cloud.api_auth_failed:
                    print("ERROR: Expected api_auth_failed=False after a successful call")
                    return 1
        return 0

    return run_async(test())


def _test_publish_status_auth_failure_marks_time_unavailable(my_predbat):
    """Test that when auth has failed and no fresh status is available, the inverter time
    sensor is marked unavailable rather than left holding a stale (frozen) timestamp.

    A frozen timestamp drifts against wall-clock and is misdiagnosed downstream as inverter
    clock skew, triggering false warnings and auto-restart loops.
    """

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.api_auth_failed = True

        await ge_cloud.publish_status("ABC123", {})

        entity = "sensor.predbat_gecloud_abc123_time"
        item = ge_cloud.dashboard_items.get(entity, {})
        if item.get("state") != "unavailable":
            print("ERROR: Expected {} state 'unavailable', got {}".format(entity, item.get("state")))
            return 1
        return 0

    return run_async(test())


def _test_run_reports_auth_denied_scoped(my_predbat):
    """run() must report GivEnergy access-denied only on a poll cycle and only for INVERTER auth
    failures — not from a stale flag on an in-between 60s tick, and not from an EVC/other-endpoint
    403 while inverter polling succeeded.
    """

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.automatic = False
        ge_cloud.device_list = ["inv001"]
        ge_cloud.evc_device_list = ["evc-001"]
        ge_cloud.pending_writes = {"inv001": []}

        status_messages = []

        def capture_status(message, had_errors=False, **kwargs):
            status_messages.append(message)

        ge_cloud.base.record_status = capture_status

        async def benign(*args, **kwargs):
            return {}

        async def status_ok(device, previous):
            ge_cloud.api_auth_failed = False
            return {"power": 1}

        async def status_auth_fail(device, previous):
            ge_cloud.api_auth_failed = True
            return {}

        async def evc_clears_flag(uuid, previous):
            ge_cloud.api_auth_failed = False
            return {"serial_number": "evc-serial"}

        async def evc_sets_flag(uuid, previous):
            ge_cloud.api_auth_failed = True
            return {"serial_number": "evc-serial"}

        ge_cloud.publish_status = benign
        ge_cloud.async_get_inverter_meter = benign
        ge_cloud.publish_meter = benign
        ge_cloud.async_get_device_info = benign
        ge_cloud.publish_info = benign
        ge_cloud.async_get_evc_device_data = benign
        ge_cloud.async_get_evc_sessions = benign
        ge_cloud.publish_evc_data = benign

        denied = "access denied"

        # Case 1: inverter status auth-fails on a poll cycle -> report denied, even though the later
        # EVC call clears the global flag.
        ge_cloud.async_get_inverter_status = status_auth_fail
        ge_cloud.async_get_evc_device = evc_clears_flag
        status_messages.clear()
        await ge_cloud.run(seconds=120, first=False)
        if not any(denied in m for m in status_messages):
            print("ERROR: inverter auth failure on a poll cycle should report access-denied, got {}".format(status_messages))
            return 1

        # Case 2 (cadence): a stale flag on a non-poll 60s tick must NOT re-report.
        ge_cloud.api_auth_failed = True
        status_messages.clear()
        await ge_cloud.run(seconds=60, first=False)
        if any(denied in m for m in status_messages):
            print("ERROR: non-poll 60s tick must not re-report denied from a stale flag, got {}".format(status_messages))
            return 1

        # Case 3 (scope): inverter ok but an EVC-only 403 must NOT report inverter access-denied.
        ge_cloud.api_auth_failed = False
        ge_cloud.async_get_inverter_status = status_ok
        ge_cloud.async_get_evc_device = evc_sets_flag
        status_messages.clear()
        await ge_cloud.run(seconds=120, first=False)
        if any(denied in m for m in status_messages):
            print("ERROR: an EVC-only auth failure must not report inverter access-denied, got {}".format(status_messages))
            return 1

        return 0

    return run_async(test())


def _test_async_get_inverter_data_non_auth_clears_flag(my_predbat):
    """A non-401/403 response (404/429/5xx) must clear a previously-set api_auth_failed flag, so a
    transient failure after an auth failure is not reported as an ongoing access-denied condition."""

    async def test():
        ge_cloud = MockGECloudDirect()

        for status in (404, 429, 500):
            ge_cloud.api_auth_failed = True
            mock_response = create_aiohttp_mock_response(status=status, json_data={"error": "x"})
            mock_session = create_aiohttp_mock_session(mock_response)

            with patch("aiohttp.ClientSession") as mock_session_class:
                with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                    mock_session_class.return_value = mock_session

                    await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                    if ge_cloud.api_auth_failed:
                        print("ERROR: a {} response should clear api_auth_failed (not an auth failure)".format(status))
                        return 1
        return 0

    return run_async(test())


def _test_run_auth_status_reported_on_transition_only(my_predbat):
    """A persistent inverter auth failure must report access-denied only once (on transition), not
    on every poll cycle, to avoid inflating the HA error_count. A new episode after recovery reports
    again."""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.automatic = False
        ge_cloud.device_list = ["inv001"]
        ge_cloud.evc_device_list = []
        ge_cloud.pending_writes = {"inv001": []}

        status_messages = []
        ge_cloud.base.record_status = lambda message, had_errors=False, **kwargs: status_messages.append(message)

        async def benign(*args, **kwargs):
            return {}

        async def status_auth_fail(device, previous):
            ge_cloud.api_auth_failed = True
            return {}

        async def status_ok(device, previous):
            ge_cloud.api_auth_failed = False
            return {"power": 1}

        ge_cloud.publish_status = benign
        ge_cloud.async_get_inverter_meter = benign
        ge_cloud.publish_meter = benign
        ge_cloud.async_get_device_info = benign
        ge_cloud.publish_info = benign

        denied = "access denied"

        # Two consecutive denied poll cycles -> reported only once.
        ge_cloud.async_get_inverter_status = status_auth_fail
        await ge_cloud.run(seconds=120, first=False)
        await ge_cloud.run(seconds=240, first=False)
        count = sum(1 for m in status_messages if denied in m)
        if count != 1:
            print("ERROR: a persistent auth denial should report once (transition), got {} reports".format(count))
            return 1

        # Recover, then deny again -> reported again (new episode).
        ge_cloud.async_get_inverter_status = status_ok
        await ge_cloud.run(seconds=360, first=False)
        ge_cloud.async_get_inverter_status = status_auth_fail
        status_messages.clear()
        await ge_cloud.run(seconds=480, first=False)
        if not any(denied in m for m in status_messages):
            print("ERROR: a new denial episode after recovery should report again")
            return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_rate_limit(my_predbat):
    """Test rate limiting (429)"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_response = create_aiohttp_mock_response(status=429, json_data={"error": "Too many requests"})
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result is not None:
                    print("ERROR: Expected None for 429, got {}".format(result))
                    return 1
                if ge_cloud.failures_total != 1:
                    print("ERROR: Expected failures_total=1, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_timeout(my_predbat):
    """Test timeout error"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Mock session that raises timeout
        mock_session = MagicMock()
        mock_session.get.side_effect = asyncio.TimeoutError("Timeout")

        async def session_aenter(self):
            return mock_session

        async def session_aexit(self, *args):
            pass

        mock_session.__aenter__ = session_aenter
        mock_session.__aexit__ = session_aexit

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                if result is not None:
                    print("ERROR: Expected None for timeout, got {}".format(result))
                    return 1
                if ge_cloud.failures_total != 1:
                    print("ERROR: Expected failures_total=1, got {}".format(ge_cloud.failures_total))
                    return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_json_error(my_predbat):
    """Test JSON decode error

    Note: Potential bug found in gecloud.py lines 1323-1341:
    When JSONDecodeError occurs, data is set to None (line 1325), but then at
    line 1339-1341, if status_code is 200 and data is None, it returns {} instead
    of None. This means JSON errors are silently ignored and treated as successful
    empty responses. Should JSONDecodeError return None or raise an error?
    """

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_response = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("Invalid JSON", "", 0))
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES)

                # BUG: Currently returns {} instead of None for JSON errors
                if result != {}:
                    print("ERROR: Expected {{}} for JSON error (current behavior), got {}".format(result))
                    return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_data_retry(my_predbat):
    """Test retry logic"""

    async def test():
        ge_cloud = MockGECloudDirect()

        call_count = [0]

        async def mock_get_data(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return None  # Fail first 2 times
            return {"serial": "test123"}

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_get_inverter_data_retry(GE_API_DEVICES)

            if result != {"serial": "test123"}:
                print("ERROR: Expected data after retry, got {}".format(result))
                return 1
            if call_count[0] != 3:
                print("ERROR: Expected 3 calls (2 retries), got {}".format(call_count[0]))
                return 1
            if mock_sleep.call_count != 2:
                print("ERROR: Expected 2 sleep calls, got {}".format(mock_sleep.call_count))
                return 1
        return 0

    return run_async(test())


# =============================================================================
# Device Discovery Tests
# =============================================================================


def _test_async_get_inverter_data_post(my_predbat):
    """Test POST requests with and without datain parameter"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Scenario 1: POST request with datain (JSON payload)
        test_data_in = {"command": "start-charging", "params": {"target_soc": 80}}
        mock_response = create_aiohttp_mock_response(status=200, json_data={"data": {"success": True, "result": "command_sent"}})
        mock_session = create_aiohttp_mock_session(mock_response)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session

                result = await ge_cloud.async_get_inverter_data(GE_API_EVC_SEND_COMMAND, uuid="evc-001", command="start-charging", post=True, datain=test_data_in)

                # Verify POST was called with json parameter
                if not mock_session.post.called:
                    print("ERROR: Expected session.post to be called")
                    return 1

                # Check that json parameter was passed
                call_kwargs = mock_session.post.call_args[1]
                if "json" not in call_kwargs:
                    print("ERROR: Expected json parameter in POST call")
                    return 1
                if call_kwargs["json"] != test_data_in:
                    print(f"ERROR: Expected json={test_data_in}, got {call_kwargs['json']}")
                    return 1

                # Verify response
                if result != {"success": True, "result": "command_sent"}:
                    print(f"ERROR: Expected success response, got {result}")
                    return 1

        # Scenario 2: POST request without datain (no JSON payload)
        mock_response2 = create_aiohttp_mock_response(status=201, json_data={"data": {"status": "accepted"}})
        mock_session2 = create_aiohttp_mock_session(mock_response2)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session2

                result = await ge_cloud.async_get_inverter_data(GE_API_EVC_SEND_COMMAND, uuid="evc-002", command="stop-charging", post=True, datain=None)

                # Verify POST was called without json parameter
                if not mock_session2.post.called:
                    print("ERROR: Expected session.post to be called")
                    return 1

                # Check that json parameter was NOT passed
                call_kwargs = mock_session2.post.call_args[1]
                if "json" in call_kwargs:
                    print("ERROR: Expected no json parameter in POST call without datain")
                    return 1

                # Verify response
                if result != {"status": "accepted"}:
                    print(f"ERROR: Expected status response, got {result}")
                    return 1

        # Scenario 3: POST request with JSON decode error and datain
        mock_response3 = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("test", "doc", 0))
        mock_session3 = create_aiohttp_mock_session(mock_response3)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session3

                result = await ge_cloud.async_get_inverter_data(GE_API_EVC_SEND_COMMAND, uuid="evc-003", command="test", post=True, datain={"test": "data"})

                # Verify returns empty dict for successful status with JSON error
                if result != {}:
                    print(f"ERROR: Expected empty dict for JSON decode error with 200, got {result}")
                    return 1

        # Scenario 4: POST request with JSON decode error but WITHOUT datain
        mock_response4 = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("test", "doc", 0))
        mock_session4 = create_aiohttp_mock_session(mock_response4)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session4

                result = await ge_cloud.async_get_inverter_data(GE_API_EVC_SEND_COMMAND, uuid="evc-004", command="test", post=True, datain=None)

                # Verify returns empty dict for successful status with JSON error (no datain)
                if result != {}:
                    print(f"ERROR: Expected empty dict for JSON decode error with 200 (no datain), got {result}")
                    return 1

                # Verify POST was called without json parameter
                call_kwargs = mock_session4.post.call_args[1]
                if "json" in call_kwargs:
                    print("ERROR: Expected no json parameter in POST call without datain")
                    return 1

        # Scenario 5: Verify GET method still works (not POST)
        mock_response5 = create_aiohttp_mock_response(status=200, json_data={"data": [{"serial": "test456"}]})
        mock_session5 = create_aiohttp_mock_session(mock_response5)

        with patch("aiohttp.ClientSession") as mock_session_class:
            with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
                mock_session_class.return_value = mock_session5

                result = await ge_cloud.async_get_inverter_data(GE_API_DEVICES, post=False)

                # Verify GET was called, not POST
                if not mock_session5.get.called:
                    print("ERROR: Expected session.get to be called")
                    return 1
                if mock_session5.post.called:
                    print("ERROR: Did not expect session.post to be called")
                    return 1

                # Verify response
                if result != [{"serial": "test456"}]:
                    print(f"ERROR: Expected device list, got {result}")
                    return 1

        return 0

    return run_async(test())


def _test_async_get_devices_with_ems(my_predbat):
    """Test device discovery with EMS device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [{"inverter": {"serial": "ems001", "info": {"model": "Plant EMS"}, "connections": {"batteries": []}}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["ems"] != "ems001":
                print("ERROR: Expected ems='ems001', got {}".format(result))
                return 1
            if result["battery"] != []:
                print("ERROR: Expected empty battery list, got {}".format(result))
                return 1
        return 0

    return run_async(test())


def _test_async_get_devices_with_gateway(my_predbat):
    """Test device discovery with Gateway device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [{"inverter": {"serial": "gw001", "info": {"model": "Gateway"}, "connections": {"batteries": [{"serial": "bat1"}]}}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["gateway"] != "gw001":
                print("ERROR: Expected gateway='gw001', got {}".format(result))
                return 1
        return 0

    return run_async(test())


def _test_async_get_devices_with_batteries(my_predbat):
    """Test device discovery with battery inverters"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [
            {"inverter": {"serial": "inv001", "info": {"model": "All-In-One"}, "connections": {"batteries": [{"serial": "bat1"}], "meters": []}}},
            {"inverter": {"serial": "inv002", "info": {"model": "Hybrid"}, "connections": {"batteries": [{"serial": "bat2"}], "meters": [{"serial_number": 12345}]}}},
        ]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["battery"] != ["inv001", "inv002"]:
                print("ERROR: Expected battery=['inv001', 'inv002'], got {}".format(result))
                return 1
            # Verify battery_meters: inv001 has no meters, inv002 has meter serial 12345
            if result["battery_meters"].get("inv001") != []:
                print("ERROR: Expected battery_meters['inv001']=[], got {}".format(result["battery_meters"]))
                return 1
            if result["battery_meters"].get("inv002") != [12345]:
                print("ERROR: Expected battery_meters['inv002']=[12345], got {}".format(result["battery_meters"]))
                return 1
        return 0

    return run_async(test())


def _test_async_get_devices_legacy_battery(my_predbat):
    """Test device discovery with legacy inverter that has empty connections.batteries but has info.battery set (e.g. GIV-HY3.6)"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Mirrors the real GIV-HY3.6 device structure seen in production
        mock_devices = [
            {
                "serial_number": "WE1913G005",
                "inverter": {
                    "serial": "SA1919G001",
                    "info": {
                        "battery_type": "LITHIUM",
                        "battery": {"nominal_capacity": 204, "nominal_voltage": 51.2, "depth_of_discharge": 1},
                        "model": "GIV-HY3.6",
                        "max_charge_rate": 2600,
                        "max_discharge_rate": 2600,
                    },
                    "connections": {"batteries": [], "meters": []},
                },
            }
        ]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result["battery"] != ["sa1919g001"]:
                print("ERROR: Expected battery=['sa1919g001'] for legacy device, got {}".format(result))
                return 1
            if result["ems"] is not None:
                print("ERROR: Expected ems=None, got {}".format(result["ems"]))
                return 1
            if result["gateway"] is not None:
                print("ERROR: Expected gateway=None, got {}".format(result["gateway"]))
                return 1
        return 0

    return run_async(test())


def _test_async_get_devices_empty(my_predbat):
    """Test device discovery with no devices"""

    async def test():
        ge_cloud = MockGECloudDirect()

        async def mock_retry(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_devices()

            if result != {"gateway": None, "ems": None, "battery": [], "battery_meters": {}}:
                print("ERROR: Expected empty result dict, got {}".format(result))
                return 1
        return 0

    return run_async(test())


def _test_async_get_evc_devices(my_predbat):
    """Test getting EV charger devices"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful device list
        mock_devices = [{"uuid": "evc-001", "alias": "Home Charger", "other_data": {"some_field": "value"}}, {"uuid": "evc-002", "alias": "Garage Charger", "other_data": {}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_evc_devices()

            if len(result) != 2:
                print("ERROR: Expected 2 devices, got {}".format(len(result)))
                return 1
            if result[0]["uuid"] != "evc-001" or result[0]["alias"] != "Home Charger":
                print("ERROR: Expected first device with uuid='evc-001', alias='Home Charger', got {}".format(result[0]))
                return 1
            if result[1]["uuid"] != "evc-002" or result[1]["alias"] != "Garage Charger":
                print("ERROR: Expected second device with uuid='evc-002', alias='Garage Charger', got {}".format(result[1]))
                return 1

        # Test with None response (API failure) - should return previous
        async def mock_retry_fail(*args, **kwargs):
            return None

        previous_devices = [{"uuid": "old-001", "alias": "Old Charger"}]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_fail

            result = await ge_cloud.async_get_evc_devices(previous=previous_devices)

            if result != previous_devices:
                print("ERROR: Expected fallback to previous devices, got {}".format(result))
                return 1

        # Test with empty list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_evc_devices()

            if result != []:
                print("ERROR: Expected empty list, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def _test_async_get_smart_devices(my_predbat):
    """Test getting smart devices"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful device list
        mock_devices = [
            {"uuid": "smart-001", "alias": "Smart Plug 1", "other_data": {"local_key": "abc123xyz"}},
            {"uuid": "smart-002", "alias": "Smart Plug 2", "other_data": {"local_key": "def456uvw"}},
            {"uuid": "smart-003", "alias": "Smart Switch", "other_data": {}},  # No local_key
        ]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_smart_devices()

            if len(result) != 3:
                print("ERROR: Expected 3 devices, got {}".format(len(result)))
                return 1
            if result[0]["uuid"] != "smart-001" or result[0]["alias"] != "Smart Plug 1" or result[0]["local_key"] != "abc123xyz":
                print("ERROR: Expected first device with uuid='smart-001', alias='Smart Plug 1', local_key='abc123xyz', got {}".format(result[0]))
                return 1
            if result[1]["uuid"] != "smart-002" or result[1]["alias"] != "Smart Plug 2" or result[1]["local_key"] != "def456uvw":
                print("ERROR: Expected second device with uuid='smart-002', alias='Smart Plug 2', local_key='def456uvw', got {}".format(result[1]))
                return 1
            if result[2]["uuid"] != "smart-003" or result[2]["alias"] != "Smart Switch" or result[2]["local_key"] is not None:
                print("ERROR: Expected third device with uuid='smart-003', alias='Smart Switch', local_key=None, got {}".format(result[2]))
                return 1

        # Test with None response (API failure) - should return previous
        async def mock_retry_fail(*args, **kwargs):
            return None

        previous_devices = [{"uuid": "old-001", "alias": "Old Device", "local_key": "old123"}]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_fail

            result = await ge_cloud.async_get_smart_devices(previous=previous_devices)

            if result != previous_devices:
                print("ERROR: Expected fallback to previous devices, got {}".format(result))
                return 1

        # Test with empty list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_smart_devices()

            if result != []:
                print("ERROR: Expected empty list, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def _test_async_get_evc_commands(my_predbat):
    """Test getting EV charger commands"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful command retrieval and blacklist filtering
        test_uuid = "evc-001"
        mock_commands = ["start-charging", "stop-charging", "set-charge-limit", "perform-factory-reset", "installation-mode"]
        mock_command_data = {"start-charging": {"type": "button", "description": "Start charging"}, "stop-charging": {"type": "button", "description": "Stop charging"}, "set-charge-limit": {"type": "number", "min": 0, "max": 100}}

        call_count = [0]

        async def mock_retry(*args, **kwargs):
            call_count[0] += 1
            # First call is for getting commands list
            if call_count[0] == 1:
                return mock_commands.copy()  # Return a copy since function modifies it
            # Subsequent calls are for command data
            command = kwargs.get("command")
            if command in mock_command_data:
                return mock_command_data[command]
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_evc_commands(test_uuid)

            # Check that blacklisted commands were removed
            if "perform-factory-reset" in result:
                print("ERROR: Blacklisted command 'perform-factory-reset' should be removed, got {}".format(result))
                return 1
            if "installation-mode" in result:
                print("ERROR: Blacklisted command 'installation-mode' should be removed, got {}".format(result))
                return 1

            # Check that valid commands are present
            if "start-charging" not in result:
                print("ERROR: Expected 'start-charging' in result, got {}".format(result))
                return 1
            if result["start-charging"] != mock_command_data["start-charging"]:
                print("ERROR: Expected start-charging data {}, got {}".format(mock_command_data["start-charging"], result["start-charging"]))
                return 1
            if "stop-charging" not in result:
                print("ERROR: Expected 'stop-charging' in result, got {}".format(result))
                return 1
            if "set-charge-limit" not in result:
                print("ERROR: Expected 'set-charge-limit' in result, got {}".format(result))
                return 1

        # Test with empty commands list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_evc_commands(test_uuid)

            if result != {}:
                print("ERROR: Expected empty dict for empty commands, got {}".format(result))
                return 1

        # Test with only blacklisted commands
        async def mock_retry_blacklist_only(*args, **kwargs):
            return ["installation-mode", "perform-factory-reset", "delete-id-tags"]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_blacklist_only

            result = await ge_cloud.async_get_evc_commands(test_uuid)

            if result != {}:
                print("ERROR: Expected empty dict when all commands blacklisted, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def _test_async_send_evc_command(my_predbat):
    """Test sending EV charger commands"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test successful command send
        test_uuid = "evc-001"
        test_command = "start-charging"
        test_params = {"target_soc": 80}

        mock_success_response = {"success": True, "data": {"status": "accepted"}}

        async def mock_get_inverter_data(*args, **kwargs):
            return mock_success_response

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data = mock_get_inverter_data

            result = await ge_cloud.async_send_evc_command(test_uuid, test_command, test_params)

            if result != mock_success_response:
                print("ERROR: Expected success response {}, got {}".format(mock_success_response, result))
                return 1
            if result["success"] != True:
                print("ERROR: Expected success=True in response")
                return 1

        # Test command with success=False in response
        mock_failed_response = {"success": False, "error": "Invalid command"}

        async def mock_get_inverter_data_failed(*args, **kwargs):
            return mock_failed_response

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data = mock_get_inverter_data_failed

            result = await ge_cloud.async_send_evc_command(test_uuid, test_command, test_params)

            if result is not None:
                print("ERROR: Expected None for failed command, got {}".format(result))
                return 1

        # Test command with no response
        async def mock_get_inverter_data_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data = mock_get_inverter_data_none

            result = await ge_cloud.async_send_evc_command(test_uuid, test_command, test_params)

            if result is not None:
                print("ERROR: Expected None for no response, got {}".format(result))
                return 1

        # Test command with retry logic
        call_count = [0]

        async def mock_get_inverter_data_retry(*args, **kwargs):
            call_count[0] += 1
            # Fail first 2 attempts, succeed on 3rd
            if call_count[0] < 3:
                return None
            return mock_success_response

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data = mock_get_inverter_data_retry

            result = await ge_cloud.async_send_evc_command(test_uuid, test_command, test_params)

            if call_count[0] != 3:
                print("ERROR: Expected 3 retry attempts, got {}".format(call_count[0]))
                return 1
            if result != mock_success_response:
                print("ERROR: Expected success after retries, got {}".format(result))
                return 1

        # Test command with all retries failing
        async def mock_get_inverter_data_all_fail(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data = mock_get_inverter_data_all_fail

            result = await ge_cloud.async_send_evc_command(test_uuid, test_command, test_params)

            if result is not None:
                print("ERROR: Expected None after all retries failed, got {}".format(result))
                return 1

            # Check that error was logged
            error_logged = any("Failed to send EVC command" in msg for msg in ge_cloud.log_messages)
            if not error_logged:
                print("ERROR: Expected error log message after failed retries")
                return 1

        return 0

    return run_async(test())


def _test_async_get_evc_device_data(my_predbat):
    """Test getting EV charger device data"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Scenario 1: Successful device data retrieval with valid measurements
        test_uuid = "evc-001"
        mock_data = {
            "data": [
                {
                    "meter_id": 0,  # EVC_METER_CHARGER
                    "timestamp": "2025-12-24T12:00:00Z",
                    "measurements": [
                        {"measurand": 1, "value": 32.5, "unit": "A"},  # Current.Import
                        {"measurand": 13, "value": 7360, "unit": "W"},  # Power.Active.Import
                        {"measurand": 19, "value": 75, "unit": "%"},  # SoC
                        {"measurand": 21, "value": 230, "unit": "V"},  # Voltage
                    ],
                },
                {
                    "meter_id": 0,
                    "timestamp": "2025-12-24T12:05:00Z",
                    "measurements": [
                        {"measurand": 1, "value": 30.0, "unit": "A"},
                        {"measurand": 13, "value": 6900, "unit": "W"},
                        {"measurand": 19, "value": 80, "unit": "%"},
                        {"measurand": 21, "value": 230, "unit": "V"},
                    ],
                },
            ]
        }

        async def mock_get_data_success(*args, **kwargs):
            return mock_data

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_data_success

            result = await ge_cloud.async_get_evc_device_data(test_uuid, {})

            # Check that we got the latest measurements (from second data point)
            if result.get(1) != 30.0:  # Current.Import
                print(f"ERROR: Expected Current.Import 30.0, got {result.get(1)}")
                return 1
            if result.get(13) != 6900:  # Power.Active.Import
                print(f"ERROR: Expected Power.Active.Import 6900, got {result.get(13)}")
                return 1
            if result.get(19) != 80:  # SoC
                print(f"ERROR: Expected SoC 80, got {result.get(19)}")
                return 1
            if result.get(21) != 230:  # Voltage
                print(f"ERROR: Expected Voltage 230, got {result.get(21)}")
                return 1

        # Scenario 2: No data returned (network issue) - should return previous data
        previous_data = {1: 25.0, 13: 5750, 19: 70}

        async def mock_get_data_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_data_none

            result = await ge_cloud.async_get_evc_device_data(test_uuid, previous_data)

            # Should return previous data when API fails
            if result != previous_data:
                print(f"ERROR: Expected previous data to be returned, got {result}")
                return 1

        # Scenario 3: Empty data array - should return previous data (bug fix)
        async def mock_get_data_empty(*args, **kwargs):
            return {"data": []}

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_data_empty

            result = await ge_cloud.async_get_evc_device_data(test_uuid, previous_data)

            # Should return previous data when data array is empty (bug fix)
            if result != previous_data:
                print(f"ERROR: Expected previous data with empty data array, got {result}")
                return 1

        # Scenario 4: Wrong meter_id (not EVC_METER_CHARGER) - should return empty result
        mock_data_wrong_meter = {
            "data": [
                {
                    "meter_id": 1,  # Grid Meter, not EVC
                    "timestamp": "2025-12-24T12:00:00Z",
                    "measurements": [
                        {"measurand": 1, "value": 10.0, "unit": "A"},
                    ],
                }
            ]
        }

        async def mock_get_data_wrong_meter(*args, **kwargs):
            return mock_data_wrong_meter

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_data_wrong_meter

            result = await ge_cloud.async_get_evc_device_data(test_uuid, {})

            # Should return empty result when meter_id doesn't match
            if result != {}:
                print(f"ERROR: Expected empty result for wrong meter_id, got {result}")
                return 1

        # Scenario 5: Measurements with invalid measurand (not in EVC_DATA_POINTS) - should be filtered out
        mock_data_invalid_measurand = {
            "data": [
                {
                    "meter_id": 0,
                    "timestamp": "2025-12-24T12:00:00Z",
                    "measurements": [
                        {"measurand": 1, "value": 32.5, "unit": "A"},  # Valid
                        {"measurand": 99, "value": 999, "unit": "X"},  # Invalid measurand
                        {"measurand": 19, "value": 75, "unit": "%"},  # Valid
                    ],
                }
            ]
        }

        async def mock_get_data_invalid_measurand(*args, **kwargs):
            return mock_data_invalid_measurand

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_data_invalid_measurand

            result = await ge_cloud.async_get_evc_device_data(test_uuid, {})

            # Should only include valid measurands
            if 99 in result:
                print(f"ERROR: Invalid measurand 99 should not be in result")
                return 1
            if result.get(1) != 32.5:
                print(f"ERROR: Expected measurand 1 to be 32.5, got {result.get(1)}")
                return 1
            if result.get(19) != 75:
                print(f"ERROR: Expected measurand 19 to be 75, got {result.get(19)}")
                return 1
            if len(result) != 2:
                print(f"ERROR: Expected 2 valid measurands, got {len(result)}")
                return 1

        # Scenario 6: BST timezone - verify UTC times are sent to API
        # After clocks move to BST (UTC+1), now_utc_exact returns BST time.
        # The API expects UTC times (EVC always works on GMT), so we must convert.
        bst_tz = timezone(timedelta(hours=1))
        bst_time = datetime(2026, 3, 29, 10, 37, 15, tzinfo=bst_tz)  # 10:37:15 BST = 09:37:15 UTC
        ge_cloud._now_utc_exact = bst_time
        captured_kwargs = {}

        async def mock_get_data_capture(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return {"data": []}

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_data_capture

            await ge_cloud.async_get_evc_device_data(test_uuid, previous_data)

            # API must receive UTC times, not BST times
            expected_end_utc = "2026-03-29T09:37:15Z"
            expected_start_utc = "2026-03-29T09:27:15Z"
            if captured_kwargs.get("end_time") != expected_end_utc:
                print(f"ERROR: BST test: Expected end_time {expected_end_utc}, got {captured_kwargs.get('end_time')}")
                return 1
            if captured_kwargs.get("start_time") != expected_start_utc:
                print(f"ERROR: BST test: Expected start_time {expected_start_utc}, got {captured_kwargs.get('start_time')}")
                return 1

        return 0

    return run_async(test())


def _test_async_get_evc_device(my_predbat):
    """Test getting a single EV charger device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Scenario 1: Successful device retrieval
        test_uuid = "evc-001"
        mock_device = {
            "uuid": "evc-001",
            "alias": "Home EV Charger",
            "serial_number": "EVC123456",
            "online": True,
            "went_offline_at": None,
            "status": "charging",
            "type": "ev_charger",
        }

        async def mock_get_device_success(*args, **kwargs):
            return mock_device

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_device_success

            result = await ge_cloud.async_get_evc_device(test_uuid, {})

            # Check all fields are correctly extracted
            if result["uuid"] != "evc-001":
                print(f"ERROR: Expected uuid evc-001, got {result['uuid']}")
                return 1
            if result["alias"] != "Home EV Charger":
                print(f"ERROR: Expected alias 'Home EV Charger', got {result['alias']}")
                return 1
            if result["serial_number"] != "EVC123456":
                print(f"ERROR: Expected serial_number EVC123456, got {result['serial_number']}")
                return 1
            if result["online"] != True:
                print(f"ERROR: Expected online True, got {result['online']}")
                return 1
            if result["status"] != "charging":
                print(f"ERROR: Expected status 'charging', got {result['status']}")
                return 1
            if result["type"] != "ev_charger":
                print(f"ERROR: Expected type 'ev_charger', got {result['type']}")
                return 1

        # Scenario 2: Device offline with went_offline_at timestamp
        mock_device_offline = {
            "uuid": "evc-002",
            "alias": "Garage Charger",
            "serial_number": "EVC789012",
            "online": False,
            "went_offline_at": "2025-12-24T10:30:00Z",
            "status": "offline",
            "type": "ev_charger",
        }

        async def mock_get_device_offline(*args, **kwargs):
            return mock_device_offline

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_device_offline

            result = await ge_cloud.async_get_evc_device(test_uuid, {})

            if result["online"] != False:
                print(f"ERROR: Expected online False, got {result['online']}")
                return 1
            if result["went_offline_at"] != "2025-12-24T10:30:00Z":
                print(f"ERROR: Expected went_offline_at timestamp, got {result['went_offline_at']}")
                return 1

        # Scenario 3: No device returned (API failure) - should return previous data
        previous_device = {
            "uuid": "evc-003",
            "alias": "Previous Charger",
            "serial_number": "PREV123",
            "online": True,
            "went_offline_at": None,
            "status": "idle",
            "type": "ev_charger",
        }

        async def mock_get_device_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_get_device_none

            result = await ge_cloud.async_get_evc_device(test_uuid, previous_device)

            # Should return previous data when API fails
            if result != previous_device:
                print(f"ERROR: Expected previous device data, got {result}")
                return 1

        return 0

    return run_async(test())


def _test_async_get_smart_device(my_predbat):
    """Test getting a single smart device"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful device retrieval
        test_uuid = "smart-001"
        mock_device = {"uuid": "smart-001", "alias": "Living Room Smart Plug", "other_data": {"local_key": "abc123xyz", "asset_id": "asset-456", "hardware_id": "hw-789"}}

        async def mock_retry(*args, **kwargs):
            return mock_device

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_smart_device(test_uuid)

            if result["uuid"] != "smart-001":
                print("ERROR: Expected uuid='smart-001', got {}".format(result))
                return 1
            if result["alias"] != "Living Room Smart Plug":
                print("ERROR: Expected alias='Living Room Smart Plug', got {}".format(result))
                return 1
            if result["local_key"] != "abc123xyz":
                print("ERROR: Expected local_key='abc123xyz', got {}".format(result))
                return 1
            if result["asset_id"] != "asset-456":
                print("ERROR: Expected asset_id='asset-456', got {}".format(result))
                return 1
            if result["hardware_id"] != "hw-789":
                print("ERROR: Expected hardware_id='hw-789', got {}".format(result))
                return 1

        # Test with device missing some optional fields
        mock_device_partial = {
            "uuid": "smart-002",
            "alias": "Bedroom Switch",
            "other_data": {
                "local_key": "def456uvw"
                # No asset_id or hardware_id
            },
        }

        async def mock_retry_partial(*args, **kwargs):
            return mock_device_partial

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_partial

            result = await ge_cloud.async_get_smart_device(test_uuid)

            if result["uuid"] != "smart-002":
                print("ERROR: Expected uuid='smart-002', got {}".format(result))
                return 1
            if result["alias"] != "Bedroom Switch":
                print("ERROR: Expected alias='Bedroom Switch', got {}".format(result))
                return 1
            if result["local_key"] != "def456uvw":
                print("ERROR: Expected local_key='def456uvw', got {}".format(result))
                return 1
            if result["asset_id"] is not None:
                print("ERROR: Expected asset_id=None, got {}".format(result))
                return 1
            if result["hardware_id"] is not None:
                print("ERROR: Expected hardware_id=None, got {}".format(result))
                return 1

        # Test with None response (API failure) - should return empty dict
        async def mock_retry_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_none

            result = await ge_cloud.async_get_smart_device(test_uuid)

            if result != {}:
                print("ERROR: Expected empty dict for None response, got {}".format(result))
                return 1

        return 0

    return run_async(test())


def _test_async_get_evc_sessions(my_predbat):
    """Test getting EV charger sessions"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test with successful session list retrieval
        test_uuid = "evc-001"
        mock_sessions = [
            {"session_id": "session-1", "start_time": "2025-12-17T10:00:00Z", "end_time": "2025-12-17T12:00:00Z", "energy_kwh": 25.5, "cost": 5.10},
            {"session_id": "session-2", "start_time": "2025-12-18T06:00:00Z", "end_time": "2025-12-18T07:30:00Z", "energy_kwh": 18.2, "cost": 3.64},
        ]

        async def mock_retry(*args, **kwargs):
            return mock_sessions

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry

            result = await ge_cloud.async_get_evc_sessions(test_uuid)

            if not isinstance(result, list):
                print("ERROR: Expected list, got {}".format(type(result)))
                return 1
            if len(result) != 2:
                print("ERROR: Expected 2 sessions, got {}".format(len(result)))
                return 1
            if result[0]["session_id"] != "session-1":
                print("ERROR: Expected session_id='session-1', got {}".format(result[0]))
                return 1
            if result[0]["energy_kwh"] != 25.5:
                print("ERROR: Expected energy_kwh=25.5, got {}".format(result[0]))
                return 1
            if result[1]["session_id"] != "session-2":
                print("ERROR: Expected session_id='session-2', got {}".format(result[1]))
                return 1

        # Test with empty session list
        async def mock_retry_empty(*args, **kwargs):
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_empty

            result = await ge_cloud.async_get_evc_sessions(test_uuid)

            if result != []:
                print("ERROR: Expected empty list, got {}".format(result))
                return 1

        # Test with non-list response (API failure) - should return previous
        async def mock_retry_dict(*args, **kwargs):
            return {"error": "Invalid request"}

        previous_sessions = [{"session_id": "old-session", "energy_kwh": 10.0}]

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_dict

            result = await ge_cloud.async_get_evc_sessions(test_uuid, previous=previous_sessions)

            if result != previous_sessions:
                print("ERROR: Expected fallback to previous sessions, got {}".format(result))
                return 1

        # Test with None response - should return previous
        async def mock_retry_none(*args, **kwargs):
            return None

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_none

            result = await ge_cloud.async_get_evc_sessions(test_uuid, previous=previous_sessions)

            if result != previous_sessions:
                print("ERROR: Expected fallback to previous when None, got {}".format(result))
                return 1

        # Test BST timezone - verify UTC times are sent to API
        # After clocks move to BST (UTC+1), now_utc_exact returns BST time.
        # The EVC always works on GMT so the API expects UTC times.
        bst_tz = timezone(timedelta(hours=1))
        bst_time = datetime(2026, 3, 29, 10, 37, 15, tzinfo=bst_tz)  # 10:37:15 BST = 09:37:15 UTC
        ge_cloud._now_utc_exact = bst_time
        captured_kwargs = {}

        async def mock_retry_bst_capture(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_get_inverter_data_retry = mock_retry_bst_capture

            await ge_cloud.async_get_evc_sessions(test_uuid)

            # API must receive UTC times, not BST times
            expected_end_utc = "2026-03-29T09:37:15Z"
            expected_start_utc = "2026-03-28T09:37:15Z"  # 24 hours earlier
            if captured_kwargs.get("end_time") != expected_end_utc:
                print("ERROR: BST test: Expected end_time {}, got {}".format(expected_end_utc, captured_kwargs.get("end_time")))
                return 1
            if captured_kwargs.get("start_time") != expected_start_utc:
                print("ERROR: BST test: Expected start_time {}, got {}".format(expected_start_utc, captured_kwargs.get("start_time")))
                return 1

        return 0

    return run_async(test())


def _make_async_storage_mock():
    """Create an in-memory mock storage that tracks save/load calls without filesystem I/O."""

    class _AsyncStorageMock:
        def __init__(self):
            self._data = {}
            self.save_calls = []

        async def load(self, module, filename, format="json"):
            import copy

            return copy.deepcopy(self._data.get((module, filename)))

        async def save(self, module, filename, data, format="json", expiry=None):
            import copy

            self._data[(module, filename)] = copy.deepcopy(data)
            self.save_calls.append({"module": module, "filename": filename, "data": copy.deepcopy(data), "format": format})
            return True

        async def age(self, module, filename):
            # Return 0 (fresh) for any stored key, None for missing keys
            return 0 if (module, filename) in self._data else None

    return _AsyncStorageMock()


def _test_settings_saved_to_storage(my_predbat):
    """Settings are saved to storage after a successful poll."""

    async def test():
        storage = _make_async_storage_mock()
        ge_cloud = MockGECloudDirect()
        ge_cloud._mock_storage = storage
        ge_cloud.automatic = False

        expected_settings = {"sid-1": {"name": "Battery Reserve", "value": 4, "validation_rules": ["between:0,100"], "validation": ""}}

        async def mock_get_devices():
            return {"battery": ["inv001"], "ems": None, "gateway": None}

        async def mock_get_evc_devices():
            return []

        async def mock_get_inverter_status(device, previous):
            return {}

        async def mock_publish_status(device, status):
            pass

        async def mock_get_inverter_meter(device, previous):
            return {}

        async def mock_publish_meter(device, meter):
            pass

        async def mock_get_device_info(device, previous):
            return {}

        async def mock_publish_info(device, info):
            pass

        async def mock_get_inverter_settings(device, first, previous):
            return expected_settings

        async def mock_publish_registers(device, settings):
            pass

        async def mock_enable_default_options(device, settings):
            pass

        ge_cloud.async_get_devices = mock_get_devices
        ge_cloud.async_get_evc_devices = mock_get_evc_devices
        ge_cloud.async_get_inverter_status = mock_get_inverter_status
        ge_cloud.publish_status = mock_publish_status
        ge_cloud.async_get_inverter_meter = mock_get_inverter_meter
        ge_cloud.publish_meter = mock_publish_meter
        ge_cloud.async_get_device_info = mock_get_device_info
        ge_cloud.publish_info = mock_publish_info
        ge_cloud.async_get_inverter_settings = mock_get_inverter_settings
        ge_cloud.publish_registers = mock_publish_registers
        ge_cloud.enable_default_options = mock_enable_default_options

        result = await ge_cloud.run(seconds=0, first=True)
        if not result:
            print("ERROR: run() should return True")
            return 1

        # Verify storage.save was called with the right arguments
        if not storage.save_calls:
            print("ERROR: Expected storage.save to be called, got no calls")
            return 1
        saved = storage.save_calls[0]
        if saved["module"] != "gecloud" or saved["filename"] != "settings":
            print("ERROR: Expected save for gecloud/settings, got {}/{}".format(saved["module"], saved["filename"]))
            return 1
        if saved["data"].get("inv001") != expected_settings:
            print("ERROR: Saved settings mismatch: {}".format(saved["data"]))
            return 1
        return 0

    return run_async(test())


def _test_settings_restored_from_fresh_cache(my_predbat):
    """Settings poll is skipped on first run when storage cache is < 10 minutes old."""

    async def test():
        storage = _make_async_storage_mock()

        # Pre-populate in-memory storage with settings (age() will return 0 = fresh)
        cached_settings = {"inv001": {"sid-1": {"name": "Battery Reserve", "value": 4, "validation_rules": ["between:0,100"], "validation": ""}}}
        await storage.save("gecloud", "settings", cached_settings)

        ge_cloud = MockGECloudDirect()
        ge_cloud._mock_storage = storage
        ge_cloud.automatic = False

        poll_calls = []

        async def mock_get_devices():
            return {"battery": ["inv001"], "ems": None, "gateway": None}

        async def mock_get_evc_devices():
            return []

        async def mock_get_inverter_status(device, previous):
            return {}

        async def mock_publish_status(device, status):
            pass

        async def mock_get_inverter_meter(device, previous):
            return {}

        async def mock_publish_meter(device, meter):
            pass

        async def mock_get_device_info(device, previous):
            return {}

        async def mock_publish_info(device, info):
            pass

        async def mock_get_inverter_settings(device, first, previous):
            poll_calls.append(device)
            return {}

        async def mock_publish_registers(device, settings):
            pass

        async def mock_enable_default_options(device, settings):
            pass

        ge_cloud.async_get_devices = mock_get_devices
        ge_cloud.async_get_evc_devices = mock_get_evc_devices
        ge_cloud.async_get_inverter_status = mock_get_inverter_status
        ge_cloud.publish_status = mock_publish_status
        ge_cloud.async_get_inverter_meter = mock_get_inverter_meter
        ge_cloud.publish_meter = mock_publish_meter
        ge_cloud.async_get_device_info = mock_get_device_info
        ge_cloud.publish_info = mock_publish_info
        ge_cloud.async_get_inverter_settings = mock_get_inverter_settings
        ge_cloud.publish_registers = mock_publish_registers
        ge_cloud.enable_default_options = mock_enable_default_options

        result = await ge_cloud.run(seconds=0, first=True)
        if not result:
            print("ERROR: run() should return True")
            return 1

        if poll_calls:
            print("ERROR: Expected settings poll to be skipped (fresh cache), but got calls: {}".format(poll_calls))
            return 1

        if ge_cloud.settings.get("inv001") != cached_settings["inv001"]:
            print("ERROR: Expected settings to be restored from cache, got: {}".format(ge_cloud.settings))
            return 1

        if not ge_cloud.settings_from_cache:
            print("ERROR: Expected settings_from_cache to be True")
            return 1
        return 0

    return run_async(test())


def _test_run_method(my_predbat):
    """Test GECloudDirect run method calls functions in correct order"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.automatic = True

        # Track function calls
        call_order = []

        # Mock all the async functions called by run()
        async def mock_get_devices():
            call_order.append("async_get_devices")
            return {"battery": ["inv001"], "ems": None, "gateway": None}

        async def mock_get_evc_devices():
            call_order.append("async_get_evc_devices")
            return [{"uuid": "evc-001", "alias": "Charger"}]

        async def mock_get_inverter_status(device, previous):
            call_order.append(f"async_get_inverter_status:{device}")
            return {"power": 1000}

        async def mock_publish_status(device, status):
            call_order.append(f"publish_status:{device}")

        async def mock_get_inverter_meter(device, previous):
            call_order.append(f"async_get_inverter_meter:{device}")
            return {"today": {"solar": 10}}

        async def mock_publish_meter(device, meter):
            call_order.append(f"publish_meter:{device}")

        async def mock_get_device_info(device, previous):
            call_order.append(f"async_get_device_info:{device}")
            return {"serial": device}

        async def mock_publish_info(device, info):
            call_order.append(f"publish_info:{device}")

        async def mock_get_evc_device(uuid, previous):
            call_order.append(f"async_get_evc_device:{uuid}")
            return {"serial_number": "evc-serial-001"}

        async def mock_get_evc_device_data(uuid, previous):
            call_order.append(f"async_get_evc_device_data:{uuid}")
            return {"power": 5000}

        async def mock_get_evc_sessions(uuid, previous):
            call_order.append(f"async_get_evc_sessions:{uuid}")
            return []

        async def mock_publish_evc_data(serial, data):
            call_order.append(f"publish_evc_data:{serial}")

        async def mock_get_inverter_settings(device, first, previous):
            call_order.append(f"async_get_inverter_settings:{device}")
            return {}

        async def mock_publish_registers(device, settings):
            call_order.append(f"publish_registers:{device}")

        async def mock_automatic_config(devices_dict):
            call_order.append("async_automatic_config")

        async def mock_enable_default_options(device, settings):
            call_order.append(f"enable_default_options:{device}")

        # Assign all mocks
        ge_cloud.async_get_devices = mock_get_devices
        ge_cloud.async_get_evc_devices = mock_get_evc_devices
        ge_cloud.async_get_inverter_status = mock_get_inverter_status
        ge_cloud.publish_status = mock_publish_status
        ge_cloud.async_get_inverter_meter = mock_get_inverter_meter
        ge_cloud.publish_meter = mock_publish_meter
        ge_cloud.async_get_device_info = mock_get_device_info
        ge_cloud.publish_info = mock_publish_info
        ge_cloud.async_get_evc_device = mock_get_evc_device
        ge_cloud.async_get_evc_device_data = mock_get_evc_device_data
        ge_cloud.async_get_evc_sessions = mock_get_evc_sessions
        ge_cloud.publish_evc_data = mock_publish_evc_data
        ge_cloud.async_get_inverter_settings = mock_get_inverter_settings
        ge_cloud.publish_registers = mock_publish_registers
        ge_cloud.async_automatic_config = mock_automatic_config
        ge_cloud.enable_default_options = mock_enable_default_options

        # Test first run (first=True, seconds=0)
        call_order = []
        result = await ge_cloud.run(seconds=0, first=True)

        if not result:
            print("ERROR: run() should return True on success")
            return 1

        # Verify expected call order for first run
        expected_order = [
            "async_get_devices",
            "async_get_evc_devices",
            # Device polling (every 60 seconds, also on first)
            "async_get_inverter_status:inv001",
            "publish_status:inv001",
            "async_get_inverter_meter:inv001",
            "publish_meter:inv001",
            "async_get_device_info:inv001",
            "publish_info:inv001",
            # EVC device polling
            "async_get_evc_device:evc-001",
            "async_get_evc_device_data:evc-001",
            "async_get_evc_sessions:evc-001",
            "publish_evc_data:evc-serial-001",
            # Settings (every 10 minutes, also on first)
            "async_get_inverter_settings:inv001",
            "publish_registers:inv001",
            # One-shot tasks (only on first)
            "async_automatic_config",
            "enable_default_options:inv001",
        ]

        if call_order != expected_order:
            print("ERROR: Call order mismatch on first run")
            print("Expected: {}".format(expected_order))
            print("Got:      {}".format(call_order))
            return 1

        # Test subsequent run at seconds=120 (not first, but divisible by 120)
        call_order = []
        result = await ge_cloud.run(seconds=120, first=False)

        if not result:
            print("ERROR: run() should return True on success")
            return 1

        # Should only do device polling, not device discovery or one-shot tasks
        expected_order_120 = [
            "async_get_inverter_status:inv001",
            "publish_status:inv001",
            "async_get_inverter_meter:inv001",
            "publish_meter:inv001",
            "async_get_device_info:inv001",
            "publish_info:inv001",
            "async_get_evc_device:evc-001",
            "async_get_evc_device_data:evc-001",
            "async_get_evc_sessions:evc-001",
            "publish_evc_data:evc-serial-001",
        ]

        if call_order != expected_order_120:
            print("ERROR: Call order mismatch at seconds=120")
            print("Expected: {}".format(expected_order_120))
            print("Got:      {}".format(call_order))
            return 1

        # Test run at seconds=600 (10 minutes, should also fetch settings)
        call_order = []
        result = await ge_cloud.run(seconds=600, first=False)

        expected_order_600 = [
            "async_get_inverter_status:inv001",
            "publish_status:inv001",
            "async_get_inverter_meter:inv001",
            "publish_meter:inv001",
            "async_get_device_info:inv001",
            "publish_info:inv001",
            "async_get_evc_device:evc-001",
            "async_get_evc_device_data:evc-001",
            "async_get_evc_sessions:evc-001",
            "publish_evc_data:evc-serial-001",
            "async_get_inverter_settings:inv001",
            "publish_registers:inv001",
        ]

        if call_order != expected_order_600:
            print("ERROR: Call order mismatch at seconds=600")
            print("Expected: {}".format(expected_order_600))
            print("Got:      {}".format(call_order))
            return 1

        # Test run at seconds=30 (not divisible by 60, should do nothing)
        call_order = []
        result = await ge_cloud.run(seconds=30, first=False)

        if call_order != []:
            print("ERROR: At seconds=30, no functions should be called, got {}".format(call_order))
            return 1

        return 0

    return run_async(test())


# =============================================================================
# Data Fetching Tests
# =============================================================================


def _test_async_get_inverter_status(my_predbat):
    """Test getting inverter status"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_status = {"power": 1500, "soc": 75, "temperature": 25}

        async def mock_retry(*args, **kwargs):
            return mock_status

        ge_cloud.async_get_inverter_data_retry = mock_retry

        result = await ge_cloud.async_get_inverter_status("test123")

        if result != mock_status:
            print("ERROR: Expected status dict, got {}".format(result))
            return 1

        # Test fallback to previous on failure
        async def mock_retry_fail(*args, **kwargs):
            return None

        ge_cloud.async_get_inverter_data_retry = mock_retry_fail
        result2 = await ge_cloud.async_get_inverter_status("test123", previous={"old": "data"})

        if result2 != {"old": "data"}:
            print("ERROR: Expected fallback to previous, got {}".format(result2))
            return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_meter(my_predbat):
    """Test getting inverter meter data"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_meter = {"today": {"solar": 15.5, "grid": {"import": 5.2, "export": 10.3}}}

        async def mock_retry(*args, **kwargs):
            return mock_meter

        ge_cloud.async_get_inverter_data_retry = mock_retry

        result = await ge_cloud.async_get_inverter_meter("test123")

        if result != mock_meter:
            print("ERROR: Expected meter dict, got {}".format(result))
            return 1
        return 0

    return run_async(test())


def _test_async_get_device_info(my_predbat):
    """Test getting device info"""

    async def test():
        ge_cloud = MockGECloudDirect()

        mock_devices = [{"inverter": {"serial": "test123", "info": {"battery": {"nominal_capacity": 52}, "max_charge_rate": 6000}}}]

        async def mock_retry(*args, **kwargs):
            return mock_devices

        ge_cloud.async_get_inverter_data_retry = mock_retry

        result = await ge_cloud.async_get_device_info("test123")

        # Result should be the inverter dict with serial and info
        if "info" not in result or "battery" not in result["info"] or result["info"]["battery"]["nominal_capacity"] != 52:
            print("ERROR: Expected device info with battery data, got {}".format(result))
            return 1
        if result["serial"] != "test123":
            print("ERROR: Expected serial test123, got {}".format(result.get("serial")))
            return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_settings_success(my_predbat):
    """Test getting inverter settings with parallel fetch"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Mock register list
        ge_cloud.register_list["test123"] = [
            {"id": 1, "name": "battery_soc", "validation": {}, "validation_rules": {}},
            {"id": 2, "name": "charge_power", "validation": {}, "validation_rules": {}},
        ]

        # Mock read setting responses
        read_count = [0]

        async def mock_read_setting(serial, setting_id):
            read_count[0] += 1
            if setting_id == 1:
                return {"value": "75"}
            elif setting_id == 2:
                return {"value": "3000"}
            return None

        ge_cloud.async_read_inverter_setting = mock_read_setting

        result = await ge_cloud.async_get_inverter_settings("test123")

        # Result structure: {sid: {"name": ..., "value": ..., "validation_rules": ..., "validation": ...}}
        if 1 not in result or result[1]["value"] != "75":
            print("ERROR: Expected setting 1 value='75', got {}".format(result.get(1)))
            return 1
        if 2 not in result or result[2]["value"] != "3000":
            print("ERROR: Expected setting 2 value='3000', got {}".format(result.get(2)))
            return 1
        if read_count[0] != 2:
            print("ERROR: Expected 2 read calls, got {}".format(read_count[0]))
            return 1
        return 0

    return run_async(test())


def _test_async_get_inverter_settings_partial_failure(my_predbat):
    """Test getting inverter settings with some failures"""

    async def test():
        ge_cloud = MockGECloudDirect()

        ge_cloud.register_list["test123"] = [
            {"id": 1, "name": "battery_soc", "validation": {}, "validation_rules": {}},
            {"id": 2, "name": "charge_power", "validation": {}, "validation_rules": {}},
        ]

        async def mock_read_setting(serial, setting_id):
            if setting_id == 1:
                return {"value": "75"}
            return None  # Fail setting 2

        ge_cloud.async_read_inverter_setting = mock_read_setting

        # Provide previous data - previous dict structure matches result structure
        previous = {2: {"name": "charge_power", "value": "2500", "validation": {}, "validation_rules": {}}}
        result = await ge_cloud.async_get_inverter_settings("test123", previous=previous)

        if result[1]["value"] != "75":
            print("ERROR: Expected setting 1 value='75', got {}".format(result.get(1)))
            return 1
        if result[2]["value"] != "2500":
            print("ERROR: Expected setting 2 value='2500' (from previous), got {}".format(result.get(2)))
            return 1
        return 0

    return run_async(test())


# =============================================================================
# Write Operation Tests
# =============================================================================


def _test_async_read_inverter_setting_success(my_predbat):
    """Test reading inverter setting successfully"""

    async def test():
        ge_cloud = MockGECloudDirect()

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            mock_response = {"value": "100"}

            async def mock_get_data(*args, **kwargs):
                return mock_response

            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_read_inverter_setting("test123", 77)

            if result != mock_response:
                print("ERROR: Expected response dict, got {}".format(result))
                return 1
            return 0

    return run_async(test())


def _test_async_read_inverter_setting_error_codes(my_predbat):
    """Test error code handling in read inverter setting"""

    async def test():
        ge_cloud = MockGECloudDirect()

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Test fatal error code (-3)
            async def mock_get_data_fatal(*args, **kwargs):
                return {"data": {"value": -3}}

            ge_cloud.async_get_inverter_data = mock_get_data_fatal

            result = await ge_cloud.async_read_inverter_setting("test123", 77)

            # Returns None for fatal errors after trying all retries
            if result is not None:
                print("ERROR: Expected None for fatal error, got {}".format(result))
                return 1

            # Should have retried MAX_RETRIES times (default 3)
            if mock_sleep.call_count == 0:
                print("ERROR: Expected retries with sleeps, sleep called 0 times")
                return 1

        return 0

    return run_async(test())


def _test_async_write_inverter_setting_success(my_predbat):
    """Test writing inverter setting successfully"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.pending_writes["test123"] = []  # Initialise pending writes for serial

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):

            async def mock_get_data(*args, **kwargs):
                # Include a valid value (not an error code) so the write succeeds
                return {"success": True, "value": 100}

            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_write_inverter_setting("test123", 77, "100")

            if result != {"success": True, "value": 100}:
                print("ERROR: Expected success response, got {}".format(result))
                return 1

            # Check pending_writes was updated
            if "test123" not in ge_cloud.pending_writes:
                print("ERROR: Expected pending_writes to be updated")
                return 1
            if len(ge_cloud.pending_writes["test123"]) == 0:
                print("ERROR: Expected entry in pending_writes")
                return 1
            if ge_cloud.pending_writes["test123"][0]["setting_id"] != 77:
                print("ERROR: Expected setting_id 77, got {}".format(ge_cloud.pending_writes["test123"][0].get("setting_id")))
                return 1
            if ge_cloud.pending_writes["test123"][0]["value"] != 100:
                print("ERROR: Expected value 100, got {}".format(ge_cloud.pending_writes["test123"][0].get("value")))
                return 1

        # Test that a response with an error value code (-1) causes a retry and ultimately fails
        ge_cloud2 = MockGECloudDirect()
        ge_cloud2.pending_writes["test123"] = []

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            call_count = [0]

            async def mock_get_data_timeout(*args, **kwargs):
                call_count[0] += 1
                return {"success": True, "value": -1}  # Inverter timeout code

            ge_cloud2.async_get_inverter_data = mock_get_data_timeout

            result2 = await ge_cloud2.async_write_inverter_setting("test123", 77, "100")

            if result2 is not None:
                print("ERROR: Expected None when value is error code, got {}".format(result2))
                return 1

            if call_count[0] != 10:
                print("ERROR: Expected 10 retry attempts for error value code, got {}".format(call_count[0]))
                return 1

            return 0

    return run_async(test())


def _test_async_write_inverter_setting_failure(my_predbat):
    """Test writing inverter setting failure

    Note: Potential bug found in gecloud.py lines 938-939:
    When response has no 'success' key, data is not set to None, so function returns
    the error response instead of None and doesn't retry. Should missing 'success' key
    trigger retries like {"success": False} does?
    """

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.pending_writes["test123"] = []  # Initialise pending writes for serial

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            call_count = [0]

            async def mock_get_data(*args, **kwargs):
                call_count[0] += 1
                return {"success": False}  # Return explicit failure to trigger retry

            ge_cloud.async_get_inverter_data = mock_get_data

            result = await ge_cloud.async_write_inverter_setting("test123", 77, "100")

            if result is not None:
                print("ERROR: Expected None after all retries, got {}".format(result))
                return 1

            # Should have retried 10 times
            if call_count[0] != 10:
                print("ERROR: Expected 10 retry attempts, got {}".format(call_count[0]))
                return 1

            return 0

    return run_async(test())


# =============================================================================
# Event Handler Tests
# =============================================================================


def _test_switch_event(my_predbat):
    """Test switch event handler"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Set up entity mapping and settings
        ge_cloud.register_entity_map["switch.predbat_gecloud_test123_ac_charge_enable"] = {"key": 56, "device": "test123"}
        ge_cloud.settings["test123"] = {56: {"value": False, "validation_rules": []}}

        write_calls = []

        async def mock_write(serial, setting_id, value):
            write_calls.append({"serial": serial, "id": setting_id, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_write_inverter_setting = mock_write
            ge_cloud.publish_registers = mock_publish

            # Test turn_on
            await ge_cloud.switch_event("switch.predbat_gecloud_test123_ac_charge_enable", "turn_on")

            if len(write_calls) != 1:
                print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
                return 1
            if write_calls[0]["value"] != True:
                print("ERROR: Expected value=True for turn_on, got {}".format(write_calls[0]["value"]))
                return 1

            # Test turn_off
            await ge_cloud.switch_event("switch.predbat_gecloud_test123_ac_charge_enable", "turn_off")

            if len(write_calls) != 2:
                print("ERROR: Expected 2 write calls, got {}".format(len(write_calls)))
                return 1
            if write_calls[1]["value"] != False:
                print("ERROR: Expected value=False for turn_off, got {}".format(write_calls[1]["value"]))
                return 1

        return 0

    return run_async(test())


def _test_number_event(my_predbat):
    """Test number event handler with validation"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Set up entity mapping with validation
        ge_cloud.register_entity_map["number.predbat_gecloud_test123_battery_reserve"] = {"key": 66, "device": "test123"}
        ge_cloud.settings["test123"] = {66: {"value": "10", "validation_rules": ["between:0,100"]}}

        write_calls = []

        async def mock_write(serial, setting_id, value):
            write_calls.append({"serial": serial, "id": setting_id, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_write_inverter_setting = mock_write
            ge_cloud.publish_registers = mock_publish

            # Test valid value
            await ge_cloud.number_event("number.predbat_gecloud_test123_battery_reserve", 20)

            if len(write_calls) != 1:
                print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
                return 1
            if write_calls[0]["value"] != 20.0:
                print("ERROR: Expected value=20.0, got {}".format(write_calls[0]["value"]))
                return 1

        return 0

    return run_async(test())


def _test_select_event(my_predbat):
    """Test select event handler with options validation

    Note: Potential bug found in gecloud.py line 324:
    Code calls validation.startswith() without checking if validation is None first.
    Should check 'if validation and validation.startswith...' to avoid AttributeError.
    """

    async def test():
        ge_cloud = MockGECloudDirect()

        # Set up entity mapping with options
        ge_cloud.register_entity_map["select.predbat_gecloud_test123_charge_start_time"] = {"key": 56, "device": "test123"}
        ge_cloud.settings["test123"] = {56: {"value": "00:00", "validation": "Value must be one of: (00:00, 00:30, 01:00, 01:30)", "validation_rules": []}}

        write_calls = []

        async def mock_write(serial, setting_id, value):
            write_calls.append({"serial": serial, "id": setting_id, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        with patch("gecloud.asyncio.sleep", new_callable=AsyncMock):
            ge_cloud.async_write_inverter_setting = mock_write
            ge_cloud.publish_registers = mock_publish

            # Test valid option
            await ge_cloud.select_event("select.predbat_gecloud_test123_charge_start_time", "01:30")

            if len(write_calls) != 1:
                print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
                return 1
            if write_calls[0]["value"] != "01:30":
                print("ERROR: Expected value='01:30', got {}".format(write_calls[0]["value"]))
                return 1

        return 0

    return run_async(test())


# =============================================================================
# Publishing Tests
# =============================================================================


def _test_publish_status(my_predbat):
    """Test publishing status entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    status_data = {"power": 1500, "battery": {"percent": 75}, "solar": {"power": 2000}, "grid": {"power": -500}}

    ge_cloud.status["test123"] = status_data
    run_async(ge_cloud.publish_status("test123", status_data))

    # Check dashboard_item was called for status entities
    expected_entities = ["sensor.predbat_gecloud_test123_battery_power", "sensor.predbat_gecloud_test123_battery_percent", "sensor.predbat_gecloud_test123_solar_power", "sensor.predbat_gecloud_test123_grid_power"]

    for entity in expected_entities:
        if entity not in ge_cloud.dashboard_items:
            print("ERROR: Expected entity {} to be published".format(entity))
            return 1

    return 0


def _test_publish_meter(my_predbat):
    """Test publishing meter entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    meter_data = {
        "today": {"solar": 15.5, "grid": {"import": 5.2, "export": 10.3}, "battery": {"charge": 8.0, "discharge": 6.5}, "consumption": 12.7},
        "total": {"solar": 6539.5, "grid": {"import": 19508.4, "export": 3230.3}, "battery": {"charge": 7290.95, "discharge": 7290.95}, "consumption": 21566.6},
    }

    ge_cloud.meter["test123"] = meter_data
    run_async(ge_cloud.publish_meter("test123", meter_data))

    # Check 'today' entities were published
    if "sensor.predbat_gecloud_test123_solar_today" not in ge_cloud.dashboard_items:
        print("ERROR: Expected solar_today to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_solar_today"]["state"] != 15.5:
        print("ERROR: Expected solar_today=15.5")
        return 1
    if "sensor.predbat_gecloud_test123_grid_import_today" not in ge_cloud.dashboard_items:
        print("ERROR: Expected grid_import_today to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_grid_import_today"]["state"] != 5.2:
        print("ERROR: Expected grid_import_today=5.2")
        return 1
    if "sensor.predbat_gecloud_test123_battery_charge_today" not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_charge_today to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_charge_today"]["state"] != 8.0:
        print("ERROR: Expected battery_charge_today=8.0")
        return 1

    # Check 'total' entities were published
    if "sensor.predbat_gecloud_test123_solar_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected solar_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_solar_total"]["state"] != 6539.5:
        print("ERROR: Expected solar_total=6539.5")
        return 1
    if "sensor.predbat_gecloud_test123_grid_import_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected grid_import_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_grid_import_total"]["state"] != 19508.4:
        print("ERROR: Expected grid_import_total=19508.4")
        return 1
    if "sensor.predbat_gecloud_test123_battery_charge_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_charge_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_charge_total"]["state"] != 7290.95:
        print("ERROR: Expected battery_charge_total=7290.95")
        return 1
    if "sensor.predbat_gecloud_test123_consumption_total" not in ge_cloud.dashboard_items:
        print("ERROR: Expected consumption_total to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_consumption_total"]["state"] != 21566.6:
        print("ERROR: Expected consumption_total=21566.6")
        return 1

    return 0


def _test_publish_info(my_predbat):
    """Test publishing info entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    info_data = {
        "info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2, "depth_of_discharge": 0.9}, "model": "GIV-HY3.6", "max_charge_rate": 6000},
        "connections": {"batteries": [{"capacity": {"full": 184.82, "design": 186}}]},
    }

    ge_cloud.info["test123"] = info_data
    run_async(ge_cloud.publish_info("test123", info_data))

    # Check battery size entity (capacity is calculated as nominal_capacity * nominal_voltage / 1000)
    # 186 * 51.2 / 1000 = 9.5232 rounded to 9.52
    if "sensor.predbat_gecloud_test123_battery_size" not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_size to be published")
        return 1
    if ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_size"]["state"] != 9.52:
        print("ERROR: Expected battery size=9.52, got {}".format(ge_cloud.dashboard_items["sensor.predbat_gecloud_test123_battery_size"]["state"]))
        return 1

    # Check battery_soh entity is published (184.82 / 186 ≈ 0.9936)
    soh_entity = "sensor.predbat_gecloud_test123_battery_soh"
    if soh_entity not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_soh to be published")
        return 1
    expected_soh = dp4(184.82 / 186)
    actual_soh = ge_cloud.dashboard_items[soh_entity]["state"]
    if abs(actual_soh - expected_soh) > 1e-9:
        print("ERROR: Expected battery_soh={}, got {}".format(expected_soh, actual_soh))
        return 1

    # Check battery_dod_soh entity is published (dod * soh = 0.9 * (184.82/186))
    dod_soh_entity = "sensor.predbat_gecloud_test123_battery_dod_soh"
    if dod_soh_entity not in ge_cloud.dashboard_items:
        print("ERROR: Expected battery_dod_soh to be published")
        return 1
    expected_dod_soh = dp4(0.9 * expected_soh)
    actual_dod_soh = ge_cloud.dashboard_items[dod_soh_entity]["state"]
    if abs(actual_dod_soh - expected_dod_soh) > 1e-9:
        print("ERROR: Expected battery_dod_soh={}, got {}".format(expected_dod_soh, actual_dod_soh))
        return 1

    # Check model entity is published with the model name and device_info as details attribute
    model_entity = "sensor.predbat_gecloud_test123_model"
    if model_entity not in ge_cloud.dashboard_items:
        print("ERROR: Expected model entity to be published")
        return 1
    if ge_cloud.dashboard_items[model_entity]["state"] != "GIV-HY3.6":
        print("ERROR: Expected model=GIV-HY3.6, got {}".format(ge_cloud.dashboard_items[model_entity]["state"]))
        return 1
    if ge_cloud.dashboard_items[model_entity]["attributes"].get("details") != info_data:
        print("ERROR: Expected model details attribute to contain the full device_info")
        return 1

    # Check model entity uses unknown fallback when model key is absent
    ge_cloud.dashboard_items.clear()
    info_no_model = {"info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2}, "max_charge_rate": 6000}}
    run_async(ge_cloud.publish_info("test123", info_no_model))
    if ge_cloud.dashboard_items.get(model_entity, {}).get("state") != "Unknown":
        print("ERROR: Expected model='Unknown' when model key absent, got {}".format(ge_cloud.dashboard_items.get(model_entity, {}).get("state")))
        return 1

    return 0


def _test_publish_registers(my_predbat):
    """Test publishing register entities"""
    ge_cloud = MockGECloudDirect()
    ge_cloud.config_args["prefix"] = "predbat"

    # Mock register dict (keyed by register ID) with different types
    registers = {
        56: {"name": "Enable AC Charge", "validation_rules": ["boolean"], "validation": "", "value": "1"},
        66: {"name": "Battery Reserve Percent Limit", "validation_rules": ["between:0,100"], "validation": "", "value": "20"},
        77: {"name": "AC Charge 1 Start Time", "validation_rules": ["date_format:H:i"], "validation": "", "value": "23:30:00"},
        88: {"name": "Charge Power Rate", "validation_rules": ["between:0,100"], "validation": "", "value": "50"},
    }

    ge_cloud.register_list["test123"] = registers

    ge_cloud.settings["test123"] = {56: "1", 66: "20", 77: "23:30:00", 88: "50"}

    run_async(ge_cloud.publish_registers("test123", registers))

    # Check switch entity
    if "switch.predbat_gecloud_test123_enable_ac_charge" not in ge_cloud.dashboard_items:
        print("ERROR: Expected switch entity to be published")
        return 1

    # Check number entity
    if "number.predbat_gecloud_test123_battery_reserve_percent_limit" not in ge_cloud.dashboard_items:
        print("ERROR: Expected number entity to be published")
        return 1

    # Check select entity
    if "select.predbat_gecloud_test123_ac_charge_1_start_time" not in ge_cloud.dashboard_items:
        print("ERROR: Expected select entity to be published")
        return 1

    # Check power rate number entity has percent attributes
    entity_id = "number.predbat_gecloud_test123_charge_power_rate"
    if entity_id not in ge_cloud.dashboard_items:
        print("ERROR: Expected charge power rate number entity to be published")
        return 1
    attrs = ge_cloud.dashboard_items[entity_id]["attributes"]
    if attrs.get("unit_of_measurement") != "%":
        print("ERROR: Expected charge power rate unit '%', got '{}'".format(attrs.get("unit_of_measurement")))
        return 1
    if attrs.get("device_class") != "power_factor":
        print("ERROR: Expected charge power rate device_class 'power_factor', got '{}'".format(attrs.get("device_class")))
        return 1

    return 0


def _test_publish_evc_data(my_predbat):
    """Test publishing EV charger data"""

    async def test():
        ge_cloud = MockGECloudDirect()

        # Test data with various measurand types
        evc_data = {
            1: 16.5,  # Current.Import
            2: 32.0,  # Current.Offered
            4: 150500,  # Energy.Active.Import.Register - updated test to scale result to watts which is what the EVC returns, GEcloud component returns kW
            11: 50.0,  # Frequency
            13: 7.2,  # Power.Active.Import - updated test to scale result to kW from the EVC, component returns W
            14: 0.95,  # Power.Factor
            15: 7.4,  # Power.Offered - scale result to kW from the EVC, component returns W
            19: 75,  # SoC
            20: 28.5,  # Temperature
            21: 230,  # Voltage
        }

        serial = "EVC-001"

        await ge_cloud.publish_evc_data(serial, evc_data)

        # Verify entities were created with correct names and attributes
        expected_entities = {
            "sensor.predbat_gecloud_evc-001_evc_current_import": {"state": 16.5, "friendly_name": "EV Charger Current Import", "unit": "A", "device_class": "current"},
            "sensor.predbat_gecloud_evc-001_evc_current_offered": {"state": 32.0, "friendly_name": "EV Charger Current Offered", "unit": "A", "device_class": "current"},
            "sensor.predbat_gecloud_evc-001_evc_energy_active_import_register": {"state": 150.5, "friendly_name": "EV Charger Total Import", "unit": "kWh", "device_class": "energy"},
            "sensor.predbat_gecloud_evc-001_evc_frequency": {"state": 50.0, "friendly_name": "EV Charger Frequency", "unit": "Hz", "device_class": "frequency"},
            "sensor.predbat_gecloud_evc-001_evc_power_active_import": {"state": 7200, "friendly_name": "EV Charger Import Power", "unit": "W", "device_class": "power"},
            "sensor.predbat_gecloud_evc-001_evc_power_factor": {"state": 0.95, "friendly_name": "EV Charger Power Factor", "unit": "*", "device_class": "power_factor"},
            "sensor.predbat_gecloud_evc-001_evc_power_offered": {"state": 7400, "friendly_name": "EV Charger Power Offered", "unit": "W", "device_class": "power"},
            "sensor.predbat_gecloud_evc-001_evc_soc": {"state": 75, "friendly_name": "EV Charger State of Charge", "unit": "%", "device_class": "battery"},
            "sensor.predbat_gecloud_evc-001_evc_temperature": {"state": 28.5, "friendly_name": "EV Charger Temperature", "unit": "°C", "device_class": "temperature"},
            "sensor.predbat_gecloud_evc-001_evc_voltage": {"state": 230, "friendly_name": "EV Charger Voltage", "unit": "V", "device_class": "voltage"},
        }

        for entity_id, expected in expected_entities.items():
            if entity_id not in ge_cloud.dashboard_items:
                print("ERROR: Entity {} not created".format(entity_id))
                return 1

            item = ge_cloud.dashboard_items[entity_id]
            if item["state"] != expected["state"]:
                print("ERROR: Entity {} has state {}, expected {}".format(entity_id, item["state"], expected["state"]))
                return 1

            attrs = item["attributes"]
            if attrs["friendly_name"] != expected["friendly_name"]:
                print("ERROR: Entity {} has friendly_name '{}', expected '{}'".format(entity_id, attrs["friendly_name"], expected["friendly_name"]))
                return 1

            if attrs["unit_of_measurement"] != expected["unit"]:
                print("ERROR: Entity {} has unit '{}', expected '{}'".format(entity_id, attrs["unit_of_measurement"], expected["unit"]))
                return 1

            if attrs["device_class"] != expected["device_class"]:
                print("ERROR: Entity {} has device_class '{}', expected '{}'".format(entity_id, attrs["device_class"], expected["device_class"]))
                return 1

        # Test with empty data
        ge_cloud.dashboard_items.clear()
        await ge_cloud.publish_evc_data(serial, {})

        if len(ge_cloud.dashboard_items) != 0:
            print("ERROR: Expected no entities for empty data, got {}".format(len(ge_cloud.dashboard_items)))
            return 1

        # Test with unknown measurand (should be ignored)
        ge_cloud.dashboard_items.clear()
        await ge_cloud.publish_evc_data(serial, {999: 123.45})

        if len(ge_cloud.dashboard_items) != 0:
            print("ERROR: Unknown measurand should be ignored, got {} entities".format(len(ge_cloud.dashboard_items)))
            return 1

        return 0

    return run_async(test())


def _test_async_automatic_config(my_predbat):
    """Test automatic configuration of Predbat based on GE Cloud devices"""

    async def test():
        ge = MockGECloudDirect()
        ge.config_args = {}

        # Test 1: Single battery with all features
        ge.settings = {
            "battery001": {
                "reg1": {"name": "Inverter_Charge_Power_Percentage"},
                "reg2": {"name": "Pause_Battery"},
                "reg3": {"name": "Pause_Battery_Start_Time"},
                "reg4": {"name": "DC_Discharge_1_Lower_SOC_Percent_Limit"},
                "reg5": {"name": "Enable_Eco_Mode"},
                "reg6": {"name": "Battery_Charge_Power"},
                "reg7": {"name": "Battery_Discharge_Power"},
                "reg8": {"name": "Battery_Reserve_Percent_Limit"},
                "reg9": {"name": "AC_Charge_Upper_Percent_Limit"},
                "reg10": {"name": "Inverter_Discharge_Power_Percentage"},
            }
        }

        devices = {"ems": None, "gateway": None, "battery": ["battery001"]}

        await ge.async_automatic_config(devices)

        # Verify basic configuration
        assert ge.config_args.get("inverter_type") == ["GEC"], "inverter_type should be set to GEC"
        assert ge.config_args.get("num_inverters") == 1, "num_inverters should be 1"
        assert ge.config_args.get("ge_cloud_serial") == "battery001", "ge_cloud_serial should be first battery"
        assert ge.config_args.get("givtcp_rest") is None, "givtcp_rest should be None"

        # Verify eco mode toggle is configured as inverter_mode
        assert ge.config_args.get("inverter_mode") == ["switch.predbat_gecloud_battery001_enable_eco_mode"], "inverter_mode should point to eco toggle switch"

        # Verify sensor entities
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_battery001_consumption_total"]
        assert ge.config_args.get("import_today") == ["sensor.predbat_gecloud_battery001_grid_import_total"]
        assert ge.config_args.get("export_today") == ["sensor.predbat_gecloud_battery001_grid_export_total"]
        assert ge.config_args.get("pv_today") == ["sensor.predbat_gecloud_battery001_solar_total"]
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_battery001_battery_power"]
        assert ge.config_args.get("pv_power") == ["sensor.predbat_gecloud_battery001_solar_power"]
        assert ge.config_args.get("load_power") == ["sensor.predbat_gecloud_battery001_consumption_power"]
        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_battery001_grid_power"]
        assert ge.config_args.get("soc_percent") == ["sensor.predbat_gecloud_battery001_battery_percent"]

        # Verify control entities
        assert ge.config_args.get("charge_rate") == ["number.predbat_gecloud_battery001_battery_charge_power"]
        assert ge.config_args.get("discharge_rate") == ["number.predbat_gecloud_battery001_battery_discharge_power"]
        assert ge.config_args.get("reserve") == ["number.predbat_gecloud_battery001_battery_reserve_percent_limit"]
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_battery001_ac_charge_upper_percent_limit"]
        assert ge.config_args.get("charge_limit_enable") is None, "charge_limit_enable should be None when enable register is absent"

        # Verify time controls
        assert ge.config_args.get("charge_start_time") == ["select.predbat_gecloud_battery001_ac_charge_1_start_time"]
        assert ge.config_args.get("charge_end_time") == ["select.predbat_gecloud_battery001_ac_charge_1_end_time"]
        assert ge.config_args.get("discharge_start_time") == ["select.predbat_gecloud_battery001_dc_discharge_1_start_time"]
        assert ge.config_args.get("discharge_end_time") == ["select.predbat_gecloud_battery001_dc_discharge_1_end_time"]

        # Verify feature flags
        assert ge.config_args.get("pause_mode") == ["select.predbat_gecloud_battery001_pause_battery"], "pause_mode should be set"
        assert ge.config_args.get("pause_start_time") == ["select.predbat_gecloud_battery001_pause_battery_start_time"], "pause_start_time should be set"
        assert ge.config_args.get("pause_end_time") == ["select.predbat_gecloud_battery001_pause_battery_end_time"], "pause_end_time should be set"
        assert ge.config_args.get("discharge_target_soc") == ["number.predbat_gecloud_battery001_dc_discharge_1_lower_soc_percent_limit"], "discharge_target_soc should be set"
        assert ge.config_args.get("charge_rate_percent") == ["number.predbat_gecloud_battery001_inverter_charge_power_percentage"], "charge_rate_percent should be set"
        assert ge.config_args.get("discharge_rate_percent") == ["number.predbat_gecloud_battery001_inverter_discharge_power_percentage"], "discharge_rate_percent should be set"

        # Test 2: Battery without optional features
        ge.config_args = {}
        ge.settings = {"battery002": {}}

        devices = {"ems": None, "gateway": None, "battery": ["battery002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("pause_mode") is None, "pause_mode should be None when feature not detected"
        assert ge.config_args.get("pause_start_time") is None, "pause_start_time should be None"
        assert ge.config_args.get("pause_end_time") is None, "pause_end_time should be None"
        assert ge.config_args.get("discharge_target_soc") is None, "discharge_target_soc should be None"
        assert ge.config_args.get("charge_rate_percent") is None, "charge_rate_percent should be None"
        assert ge.config_args.get("discharge_rate_percent") is None, "discharge_rate_percent should be None"
        assert ge.config_args.get("inverter_mode") is None, "inverter_mode should be None when eco toggle switch is not available"

        # Test 3: Multiple batteries with no battery_meters (default: shared CT — no dedicated meters detected)
        # When battery_meters is absent or all batteries have empty meters, shared CT is assumed and
        # grid/load sensors should use only the first battery to avoid double-counting.
        ge.config_args = {}
        ge.settings = {"battery001": {"reg1": {"name": "Enable_Eco_Mode"}}, "battery002": {"reg1": {"name": "Enable_Eco_Mode"}}}

        devices = {"ems": None, "gateway": None, "battery": ["battery001", "battery002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 2, "num_inverters should be 2"
        assert ge.config_args.get("inverter_type") == ["GEC", "GEC"], "inverter_type should have 2 entries"
        # Shared CT: load_today, import_today, export_today use first battery only to avoid double-counting
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_battery001_consumption_total"], "load_today should use first battery only (shared CT)"
        assert ge.config_args.get("import_today") == ["sensor.predbat_gecloud_battery001_grid_import_total"], "import_today should use first battery only (shared CT)"
        assert ge.config_args.get("export_today") == ["sensor.predbat_gecloud_battery001_grid_export_total"], "export_today should use first battery only (shared CT)"
        # Shared CT: grid_power and load_power use first battery + zeros
        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_battery001_grid_power", 0], "grid_power should use first battery + zero (shared CT)"
        assert ge.config_args.get("load_power") == ["sensor.predbat_gecloud_battery001_consumption_power", 0], "load_power should use first battery + zero (shared CT)"
        # Per-inverter sensors should still use all batteries
        assert ge.config_args.get("pv_today") == ["sensor.predbat_gecloud_battery001_solar_total", "sensor.predbat_gecloud_battery002_solar_total"], "pv_today should use all batteries"
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_battery001_battery_power", "sensor.predbat_gecloud_battery002_battery_power"], "battery_power should use all batteries"
        assert ge.config_args.get("inverter_mode") == ["switch.predbat_gecloud_battery001_enable_eco_mode", "switch.predbat_gecloud_battery002_enable_eco_mode"], "inverter_mode should have 2 eco toggle entries"

        # Test 3b: Multiple batteries with unique dedicated meters (independent CT clamps)
        # When each battery has a distinct dedicated meter serial, per-inverter readings are used.
        ge.config_args = {}
        ge.settings = {"battery001": {"reg1": {"name": "Enable_Eco_Mode"}}, "battery002": {"reg1": {"name": "Enable_Eco_Mode"}}}

        devices = {
            "ems": None,
            "gateway": None,
            "battery": ["battery001", "battery002"],
            "battery_meters": {"battery001": [1001], "battery002": [1002]},
        }

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 2, "num_inverters should be 2 with unique meters"
        # Unique meters: all sensors use both batteries (no shared-CT fixup applied)
        assert ge.config_args.get("import_today") == ["sensor.predbat_gecloud_battery001_grid_import_total", "sensor.predbat_gecloud_battery002_grid_import_total"], "import_today should use both batteries when meters are unique"
        assert ge.config_args.get("export_today") == ["sensor.predbat_gecloud_battery001_grid_export_total", "sensor.predbat_gecloud_battery002_grid_export_total"], "export_today should use both batteries when meters are unique"
        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_battery001_grid_power", "sensor.predbat_gecloud_battery002_grid_power"], "grid_power should use both batteries when meters are unique"

        # Test 3c: Multiple batteries with shared/duplicate meter serial (same CT clamp explicitly detected)
        ge.config_args = {}
        ge.settings = {"battery001": {"reg1": {"name": "Enable_Eco_Mode"}}, "battery002": {"reg1": {"name": "Enable_Eco_Mode"}}}

        devices = {
            "ems": None,
            "gateway": None,
            "battery": ["battery001", "battery002"],
            "battery_meters": {"battery001": [9999], "battery002": [9999]},  # same serial = shared meter
        }

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_battery001_grid_power", 0], "grid_power should use first battery + zero when meter serial is shared"
        assert ge.config_args.get("import_today") == ["sensor.predbat_gecloud_battery001_grid_import_total"], "import_today should use first battery only when meter serial is shared"

        # Test 3d: Three-phase alternative names should be auto-selected when default names do not exist
        ge.config_args = {}
        ge.settings = {
            "battery003": {
                "reg1": {"name": "Charge_Power_Rate"},
                "reg2": {"name": "Discharge_Power_Rate"},
                "reg3": {"name": "Battery_Reserve_Percent"},
                "reg4": {"name": "AC_Charge_1_Upper_SOC_Percent_Limit"},
                "reg5": {"name": "Enable_AC_Charge"},
                "reg6": {"name": "Enable_Force_Discharge"},
            }
        }
        devices = {"ems": None, "gateway": None, "battery": ["battery003"]}
        await ge.async_automatic_config(devices)

        assert ge.config_args.get("charge_rate") is None
        assert ge.config_args.get("discharge_rate") is None
        assert ge.config_args.get("charge_rate_percent") == ["number.predbat_gecloud_battery003_charge_power_rate"]
        assert ge.config_args.get("discharge_rate_percent") == ["number.predbat_gecloud_battery003_discharge_power_rate"]
        assert ge.config_args.get("reserve") == ["number.predbat_gecloud_battery003_battery_reserve_percent"]
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_battery003_ac_charge_1_upper_soc_percent_limit"]
        assert ge.config_args.get("charge_limit_enable") is None, "charge_limit_enable should be None when enable register is absent"
        assert ge.config_args.get("scheduled_charge_enable") == ["switch.predbat_gecloud_battery003_enable_ac_charge"]
        assert ge.config_args.get("scheduled_discharge_enable") == ["switch.predbat_gecloud_battery003_enable_force_discharge"]
        assert ge.config_args.get("inverter_mode") is None, "inverter_mode should be None when eco toggle is not available"

        # Test 4: EMS configuration
        ge.config_args = {}
        ge.settings = {"ems001": {}, "battery001": {}}

        devices = {"ems": "ems001", "gateway": None, "battery": ["battery001"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("inverter_type") == ["GEE"], "inverter_type should be GEE for EMS"
        assert ge.config_args.get("ge_cloud_serial") == "ems001", "ge_cloud_serial should be EMS"
        assert ge.config_args.get("ge_cloud_data") is False, "ge_cloud_data should be False for EMS"

        # EMS-specific controls
        assert ge.config_args.get("charge_start_time") == ["select.predbat_gecloud_ems001_charge_start_time_slot_1"]
        assert ge.config_args.get("charge_end_time") == ["select.predbat_gecloud_ems001_charge_end_time_slot_1"]
        assert ge.config_args.get("idle_start_time") == ["select.predbat_gecloud_ems001_discharge_start_time_slot_1"]
        assert ge.config_args.get("idle_end_time") == ["select.predbat_gecloud_ems001_discharge_end_time_slot_1"]
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_ems001_charge_soc_percent_limit_1"]
        assert ge.config_args.get("discharge_start_time") == ["select.predbat_gecloud_ems001_export_start_time_slot_1"]
        assert ge.config_args.get("discharge_end_time") == ["select.predbat_gecloud_ems001_export_end_time_slot_1"]

        # EMS-specific sensors
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_ems001_consumption_total"]
        assert ge.config_args.get("pv_today") == ["sensor.predbat_gecloud_ems001_solar_total"]
        assert ge.config_args.get("import_today") == ["sensor.predbat_gecloud_ems001_grid_import_total"]
        assert ge.config_args.get("export_today") == ["sensor.predbat_gecloud_ems001_grid_export_total"]
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_ems001_battery_power"]
        assert ge.config_args.get("pv_power") == ["sensor.predbat_gecloud_ems001_solar_power"]
        assert ge.config_args.get("load_power") == ["sensor.predbat_gecloud_ems001_consumption_power"]
        assert ge.config_args.get("grid_power") == ["sensor.predbat_gecloud_ems001_grid_power"]

        # Test 5: Multiple batteries with gateway (should use gateway as control)
        ge.config_args = {}
        ge.settings = {"gateway001": {}, "battery001": {}, "battery002": {}}

        devices = {"ems": None, "gateway": "gateway001", "battery": ["battery001", "battery002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 1, "num_inverters should be 1 when using gateway"
        assert ge.config_args.get("load_today") == ["sensor.predbat_gecloud_gateway001_consumption_total"], "Should use gateway for control"

        # Test 6: No battery devices (should log error and return)
        ge.config_args = {}
        devices = {"ems": None, "gateway": None, "battery": []}

        await ge.async_automatic_config(devices)

        assert len(ge.config_args) == 0, "config_args should be empty when no batteries"

        # Test 7: None devices (should log error and return)
        ge.config_args = {}
        await ge.async_automatic_config(None)

        assert len(ge.config_args) == 0, "config_args should be empty when devices is None"

        # Test 8: EMS with multiple inverters
        ge.config_args = {}
        ge.settings = {"ems001": {}, "battery001": {}, "battery002": {}}

        devices = {"ems": "ems001", "gateway": None, "battery": ["battery001", "battery002"]}

        await ge.async_automatic_config(devices)

        assert ge.config_args.get("num_inverters") == 2, "num_inverters should be 2 for EMS with multiple batteries"
        assert ge.config_args.get("inverter_type") == ["GEE", "GEE"], "inverter_type should have 2 GEE entries"
        # EMS produces data for all inverters, so additional inverters get 0
        assert ge.config_args.get("battery_power") == ["sensor.predbat_gecloud_ems001_battery_power", 0], "Second inverter should get 0 for battery_power"
        assert ge.config_args.get("pv_power") == ["sensor.predbat_gecloud_ems001_solar_power", 0], "Second inverter should get 0 for pv_power"

        # Test 9: charge_limit_enable entity configuration
        # Test 9a: battery with Enable_AC_Charge_Upper_Percent_Limit register -> primary switch entity
        ge.config_args = {}
        ge.settings = {
            "battery001": {
                "reg1": {"name": "AC_Charge_Upper_Percent_Limit"},
                "reg2": {"name": "Enable_AC_Charge_Upper_Percent_Limit"},
            }
        }
        devices = {"ems": None, "gateway": None, "battery": ["battery001"]}
        await ge.async_automatic_config(devices)
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_battery001_ac_charge_upper_percent_limit"], "charge_limit should use primary register"
        assert ge.config_args.get("charge_limit_enable") == ["switch.predbat_gecloud_battery001_enable_ac_charge_upper_percent_limit"], "charge_limit_enable should use primary enable switch"

        # Test 9b: battery with Enable_AC_Charge_1_Upper_SOC_Percent_Limit (alternative) -> fallback entity
        ge.config_args = {}
        ge.settings = {
            "battery002": {
                "reg1": {"name": "AC_Charge_1_Upper_SOC_Percent_Limit"},
                "reg2": {"name": "Enable_AC_Charge_1_Upper_SOC_Percent_Limit"},
            }
        }
        devices = {"ems": None, "gateway": None, "battery": ["battery002"]}
        await ge.async_automatic_config(devices)
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_battery002_ac_charge_1_upper_soc_percent_limit"], "charge_limit should use alternative register"
        assert ge.config_args.get("charge_limit_enable") == ["switch.predbat_gecloud_battery002_enable_ac_charge_1_upper_soc_percent_limit"], "charge_limit_enable should use alternative enable switch"

        # Test 9c: multi-battery system where each battery uses the primary enable switch
        ge.config_args = {}
        ge.settings = {
            "battery001": {
                "reg1": {"name": "AC_Charge_Upper_Percent_Limit"},
                "reg2": {"name": "Enable_AC_Charge_Upper_Percent_Limit"},
            },
            "battery002": {
                "reg1": {"name": "AC_Charge_Upper_Percent_Limit"},
                "reg2": {"name": "Enable_AC_Charge_Upper_Percent_Limit"},
            },
        }
        devices = {"ems": None, "gateway": None, "battery": ["battery001", "battery002"]}
        await ge.async_automatic_config(devices)
        assert ge.config_args.get("charge_limit_enable") == [
            "switch.predbat_gecloud_battery001_enable_ac_charge_upper_percent_limit",
            "switch.predbat_gecloud_battery002_enable_ac_charge_upper_percent_limit",
        ], "charge_limit_enable should have one entry per battery"

        # Test 9d: battery with charge limit register but no enable register -> charge_limit_enable is None
        ge.config_args = {}
        ge.settings = {
            "battery001": {
                "reg1": {"name": "AC_Charge_Upper_Percent_Limit"},
            }
        }
        devices = {"ems": None, "gateway": None, "battery": ["battery001"]}
        await ge.async_automatic_config(devices)
        assert ge.config_args.get("charge_limit") == ["number.predbat_gecloud_battery001_ac_charge_upper_percent_limit"]
        assert ge.config_args.get("charge_limit_enable") is None, "charge_limit_enable should be None when enable register is absent"

        return 0

    return run_async(test())


def _test_hybrid_detection(my_predbat):
    """Test hybrid vs AC-coupled inverter detection in async_automatic_config"""

    async def test():
        hybrid_entity = "switch.predbat_inverter_hybrid"

        # Helper to run auto_config with given battery info and return the entity state
        async def run(batteries_info):
            ge = MockGECloudDirect()
            ge.settings = {serial: {} for serial in batteries_info}
            ge.info = {serial: {"info": {"model": model}} for serial, model in batteries_info.items()}
            devices = {"ems": None, "gateway": None, "battery": list(batteries_info.keys())}
            await ge.async_automatic_config(devices)
            return ge.base.ha_interface.external_states.get(hybrid_entity)

        # Standard hybrid inverter (no 'ac' or 'aio') -> switch ON (not ac_coupled)
        state = await run({"INV001": "GIV-HY3.6"})
        assert state is True, "Hybrid (GIV-HY3.6) should set inverter_hybrid ON, got {}".format(state)

        # AC-coupled inverter ('ac' in model name) -> switch OFF
        state = await run({"INV001": "GIV-AC3.0"})
        assert state is False, "AC-coupled model should set inverter_hybrid OFF, got {}".format(state)

        # All-In-One inverter ('aio' substring in model) -> switch OFF
        state = await run({"INV001": "GIV-AIO3.6"})
        assert state is False, "AIO model should set inverter_hybrid OFF, got {}".format(state)

        # Mixed fleet: first is hybrid, second is AC-coupled -> detects AC-coupled (breaks early)
        state = await run({"INV001": "GIV-HY3.6", "INV002": "GIV-AC3.0"})
        # INV001 is hybrid (no ac/aio), INV002 has 'ac'. Loop hits INV001 first with no match,
        # then INV002 triggers ac_coupled=True — result depends on iteration order.
        # The dict preserves insertion order in Python 3.7+, so INV001 runs first without break,
        # then INV002 triggers ac_coupled and breaks -> False.
        assert state is False, "Fleet with an AC model should set inverter_hybrid OFF, got {}".format(state)

        # Unknown/missing model (empty string) -> no match, stays hybrid
        state = await run({"INV001": ""})
        assert state is True, "Unknown/empty model should default to hybrid (ON), got {}".format(state)

        # Model string is case-insensitive: uppercase 'AC' should still flag ac_coupled
        state = await run({"INV001": "GIV-AC3.0-RACK"})
        assert state is False, "Model with uppercase AC should set inverter_hybrid OFF, got {}".format(state)

        return 0

    return run_async(test())


def _test_enable_default_options(my_predbat):
    """Test enabling default options for inverter settings"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.settings = {"test123": {}}

        # Test 1: Export SOC percent limit that needs fixing (value > 4)
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 10, "validation_rules": []}}

        write_calls = []

        async def mock_write(device, key, value):
            write_calls.append({"device": device, "key": key, "value": value})
            return {"value": value}

        async def mock_publish(*args, **kwargs):
            pass

        ge_cloud.async_write_inverter_setting = mock_write
        ge_cloud.publish_registers = mock_publish

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting export SOC limit")
            return 1
        if len(write_calls) != 1:
            print("ERROR: Expected 1 write call, got {}".format(len(write_calls)))
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for export SOC limit, got {}".format(write_calls[0]["value"]))
            return 1
        if registers[100]["value"] != 4:
            print("ERROR: Register value should be updated to 4, got {}".format(registers[100]["value"]))
            return 1

        # Test 2: Export SOC percent limit already at correct value (4)
        write_calls = []
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 4, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when no changes needed")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Expected 0 write calls when value already correct, got {}".format(len(write_calls)))
            return 1

        # Test 3: Discharge SOC percent limit that needs fixing (None value)
        write_calls = []
        registers = {101: {"name": "DC_Discharge_SOC_Percent_Limit", "value": None, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting discharge SOC limit")
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for discharge SOC limit, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 4: AC charge upper percent limit that needs fixing (value < 100)
        write_calls = []
        registers = {102: {"name": "AC_Charge_Upper_Percent_Limit", "value": 95, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting AC charge limit")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for AC charge limit, got {}".format(write_calls[0]["value"]))
            return 1
        if registers[102]["value"] != 100:
            print("ERROR: Register value should be updated to 100, got {}".format(registers[102]["value"]))
            return 1

        # Test 5: Inverter max output active power percent that needs fixing
        write_calls = []
        registers = {103: {"name": "Inverter_Max_Output_Active_Power_Percent", "value": 80, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting max output power")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for max output power, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 6: Skip enable_ settings (like enable_ac_charge_upper_percent_limit)
        write_calls = []
        registers = {104: {"name": "Enable_AC_Charge_Upper_Percent_Limit", "value": 50, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should skip enable_ settings")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write to enable_ settings, got {} calls".format(len(write_calls)))
            return 1

        # Test 7: Real-time control needs enabling (value is False/None)
        write_calls = []
        registers = {105: {"name": "Real_Time_Control", "value": False, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when enabling real-time control")
            return 1
        if write_calls[0]["value"] != True:
            print("ERROR: Expected value=True for real-time control, got {}".format(write_calls[0]["value"]))
            return 1
        if registers[105]["value"] != True:
            print("ERROR: Register value should be updated to True, got {}".format(registers[105]["value"]))
            return 1

        # Test 8: Real-time control already enabled
        write_calls = []
        registers = {105: {"name": "Real_Time_Control", "value": True, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when real-time control already enabled")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write when real-time control already enabled, got {} calls".format(len(write_calls)))
            return 1

        # Test 9: Write failure should return False
        write_calls = []
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 10, "validation_rules": []}}

        async def mock_write_fail(device, key, value):
            write_calls.append({"device": device, "key": key, "value": value})
            return None  # Simulate write failure

        ge_cloud.async_write_inverter_setting = mock_write_fail

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when write fails")
            return 1

        # Test 10: Multiple settings - should process first match only
        write_calls = []
        registers = {100: {"name": "Export_SOC_Percent_Limit", "value": 10, "validation_rules": []}, 102: {"name": "AC_Charge_Upper_Percent_Limit", "value": 80, "validation_rules": []}}

        ge_cloud.async_write_inverter_setting = mock_write

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True after processing first match")
            return 1
        # Should only process the first matching setting (export SOC limit)
        if len(write_calls) != 1:
            print("ERROR: Should only process first matching setting, got {} calls".format(len(write_calls)))
            return 1
        if write_calls[0]["key"] != 100:
            print("ERROR: Should process first setting (key 100), got key {}".format(write_calls[0]["key"]))
            return 1

        # Test 11: AC charge slot 2 start time needs resetting
        write_calls = []
        registers = {200: {"name": "AC_Charge_2_Start_Time", "value": "05:30", "validation_rules": []}}
        ge_cloud.async_write_inverter_setting = mock_write

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting AC charge 2 start time")
            return 1
        if len(write_calls) != 1:
            print("ERROR: Expected 1 write call for AC charge 2 start time, got {}".format(len(write_calls)))
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for AC charge 2 start time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 12: AC charge slot 5 end time needs resetting
        write_calls = []
        registers = {201: {"name": "AC_Charge_5_End_Time", "value": "08:00", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting AC charge 5 end time")
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for AC charge 5 end time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 13: DC discharge slot 3 start time needs resetting
        write_calls = []
        registers = {202: {"name": "DC_Discharge_3_Start_Time", "value": "14:00", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting DC discharge 3 start time")
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for DC discharge 3 start time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 14: DC discharge slot 10 end time needs resetting
        write_calls = []
        registers = {203: {"name": "DC_Discharge_10_End_Time", "value": "22:30", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when resetting DC discharge 10 end time")
            return 1
        if write_calls[0]["value"] != "00:00":
            print("ERROR: Expected value='00:00' for DC discharge 10 end time, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 15: AC charge slot 2 start time already at 00:00 - should not write
        write_calls = []
        registers = {200: {"name": "AC_Charge_2_Start_Time", "value": "00:00", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when AC charge 2 start time already 00:00")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write when time already 00:00, got {} calls".format(len(write_calls)))
            return 1

        # Test 16: AC charge slot 2 start time is None - should not write
        write_calls = []
        registers = {200: {"name": "AC_Charge_2_Start_Time", "value": None, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False when AC charge 2 start time is None")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write when time is None, got {} calls".format(len(write_calls)))
            return 1

        # Test 17: AC charge slot 1 should NOT be reset (slots 2-10 only)
        write_calls = []
        registers = {210: {"name": "AC_Charge_1_Start_Time", "value": "05:30", "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if result:
            print("ERROR: enable_default_options should return False for AC charge 1 (not in range 2-10)")
            return 1
        if len(write_calls) != 0:
            print("ERROR: Should not write to AC charge 1 slot, got {} calls".format(len(write_calls)))
            return 1

        # Test 18: Lower SOC percent limit needs fixing
        write_calls = []
        registers = {220: {"name": "Lower_SOC_Percent_Limit", "value": 10, "validation_rules": []}}
        ge_cloud.async_write_inverter_setting = mock_write

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting lower SOC limit")
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for lower SOC limit, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 19: Upper SOC percent limit needs fixing
        write_calls = []
        registers = {221: {"name": "DC_Discharge_Upper_SOC_Percent_Limit", "value": 95, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting upper SOC limit")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for upper SOC limit, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 20: Charge Up To Percent needs fixing
        write_calls = []
        registers = {222: {"name": "Charge_Up_To_Percent", "value": 80, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting charge up to percent")
            return 1
        if write_calls[0]["value"] != 100:
            print("ERROR: Expected value=100 for charge up to percent, got {}".format(write_calls[0]["value"]))
            return 1

        # Test 21: Discharge Down To Percent needs fixing
        write_calls = []
        registers = {223: {"name": "Discharge_Down_To_Percent", "value": 15, "validation_rules": []}}

        result = await ge_cloud.enable_default_options("test123", registers)

        if not result:
            print("ERROR: enable_default_options should return True when setting discharge down to percent")
            return 1
        if write_calls[0]["value"] != 4:
            print("ERROR: Expected value=4 for discharge down to percent, got {}".format(write_calls[0]["value"]))
            return 1

        return 0

    return run_async(test())


def _make_run_mocks(ge_cloud, enable_default_calls=None):
    """Attach minimal async mocks to ge_cloud so run() can execute without real I/O."""

    async def mock_get_devices():
        return {"battery": ["inv001"], "ems": None, "gateway": None}

    async def mock_get_evc_devices():
        return []

    async def mock_get_inverter_status(_device, _previous):
        return {}

    async def mock_publish_status(_device, _status):
        pass

    async def mock_get_inverter_meter(_device, _previous):
        return {}

    async def mock_publish_meter(_device, _meter):
        pass

    async def mock_get_device_info(_device, _previous):
        return {}

    async def mock_publish_info(_device, _info):
        pass

    async def mock_get_inverter_settings(_device, **_kwargs):
        return {}

    async def mock_publish_registers(_device, _settings):
        pass

    async def mock_automatic_config(_devices_dict):
        pass

    async def mock_enable_default_options(device, _settings):
        if enable_default_calls is not None:
            enable_default_calls.append(device)

    ge_cloud.async_get_devices = mock_get_devices
    ge_cloud.async_get_evc_devices = mock_get_evc_devices
    ge_cloud.async_get_inverter_status = mock_get_inverter_status
    ge_cloud.publish_status = mock_publish_status
    ge_cloud.async_get_inverter_meter = mock_get_inverter_meter
    ge_cloud.publish_meter = mock_publish_meter
    ge_cloud.async_get_device_info = mock_get_device_info
    ge_cloud.publish_info = mock_publish_info
    ge_cloud.async_get_inverter_settings = mock_get_inverter_settings
    ge_cloud.publish_registers = mock_publish_registers
    ge_cloud.async_automatic_config = mock_automatic_config
    ge_cloud.enable_default_options = mock_enable_default_options


def _test_run_read_only_skips_reset(my_predbat):
    """enable_default_options is NOT called when predbat is in read-only mode"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.automatic = False
        ge_cloud._read_only = True

        enable_default_calls = []
        _make_run_mocks(ge_cloud, enable_default_calls)

        result = await ge_cloud.run(seconds=0, first=True)
        if not result:
            print("ERROR: run() should return True")
            return 1

        if enable_default_calls:
            print("ERROR: enable_default_options should NOT be called in read-only mode, got calls for: {}".format(enable_default_calls))
            return 1

        if ge_cloud.default_options_done:
            print("ERROR: default_options_done should remain False when skipped due to read-only mode")
            return 1

        return 0

    return run_async(test())


def _test_run_enables_reset_after_read_only(my_predbat):
    """enable_default_options IS called on the first run after read-only mode is disabled"""

    async def test():
        ge_cloud = MockGECloudDirect()
        ge_cloud.automatic = False
        ge_cloud._read_only = True

        enable_default_calls = []
        _make_run_mocks(ge_cloud, enable_default_calls)

        # First run in read-only mode — reset should be skipped
        result = await ge_cloud.run(seconds=0, first=True)
        if not result:
            print("ERROR: run() should return True on first run")
            return 1

        if enable_default_calls:
            print("ERROR: enable_default_options should NOT be called in read-only mode, got: {}".format(enable_default_calls))
            return 1
        if ge_cloud.default_options_done:
            print("ERROR: default_options_done should be False after read-only first run")
            return 1

        # Disable read-only — next 10-minute settings tick should trigger the reset
        ge_cloud._read_only = False
        result = await ge_cloud.run(seconds=600, first=False)
        if not result:
            print("ERROR: run() should return True on second run")
            return 1

        if not enable_default_calls:
            print("ERROR: enable_default_options should be called once read-only is disabled")
            return 1
        if enable_default_calls != ["inv001"]:
            print("ERROR: Expected enable_default_options called for inv001, got: {}".format(enable_default_calls))
            return 1
        if not ge_cloud.default_options_done:
            print("ERROR: default_options_done should be True after reset ran")
            return 1

        # Verify the reset does not run again on subsequent ticks
        enable_default_calls.clear()
        await ge_cloud.run(seconds=1200, first=False)
        if enable_default_calls:
            print("ERROR: enable_default_options should not be called again after default_options_done=True")
            return 1

        return 0

    return run_async(test())


# =============================================================================
# GECloudData Download and Caching Tests
# =============================================================================


def _test_download_ge_data_single_day(my_predbat):
    """Test downloading data for a single day"""

    async def test():
        with tempfile.TemporaryDirectory() as tmpdir:
            ge_data = MockGECloudData(config_root=tmpdir)
            ge_data.max_days_previous = 0

            # Mock API response
            with patch("aiohttp.ClientSession") as mock_session_class:
                mock_response = create_aiohttp_mock_response(status=200, json_data={"data": [{"time": "2024-12-17T10:00:00Z", "total": {"consumption": 1.5, "grid": {"import": 0.5, "export": 0.0}, "solar": 2.0}}]})
                mock_session = create_aiohttp_mock_session(mock_response)
                mock_session_class.return_value = mock_session

                now = datetime(2024, 12, 17, 12, 0, 0)
                result = await ge_data.download_ge_data(now)

                if not result:
                    print("ERROR: Expected download to succeed")
                    return 1
                if len(ge_data.mdata) == 0:
                    print("ERROR: Expected data to be parsed")
                    return 1
                if ge_data.mdata[0]["consumption"] != 1.5:
                    print("ERROR: Expected consumption=1.5, got {}".format(ge_data.mdata[0]["consumption"]))
                    return 1

                return 0

    return run_async(test())


def _test_download_ge_data_multi_day(my_predbat):
    """Test downloading data for multiple days"""

    async def test():
        with tempfile.TemporaryDirectory() as tmpdir:
            ge_data = MockGECloudData(config_root=tmpdir)
            ge_data.max_days_previous = 2

            call_count = [0]

            def mock_response_fn(*args, **kwargs):
                call_count[0] += 1
                mock_response = create_aiohttp_mock_response(
                    status=200, json_data={"data": [{"time": "2024-12-{:02d}T10:00:00Z".format(15 + call_count[0]), "total": {"consumption": 1.0 * call_count[0], "grid": {"import": 0.5, "export": 0.0}, "solar": 1.5}}]}
                )
                mock_session = create_aiohttp_mock_session(mock_response)
                return mock_session

            with patch("aiohttp.ClientSession") as mock_session_class:
                mock_session_class.side_effect = mock_response_fn

                now = datetime(2024, 12, 17, 12, 0, 0)
                result = await ge_data.download_ge_data(now)

                if not result:
                    print("ERROR: Expected download to succeed")
                    return 1

                # Should fetch 3 days (today + 2 previous)
                if call_count[0] < 3:
                    print("ERROR: Expected at least 3 API calls, got {}".format(call_count[0]))
                    return 1

                return 0

    return run_async(test())


def _test_download_ge_data_pagination(my_predbat):
    """Test downloading data with pagination"""

    async def test():
        with tempfile.TemporaryDirectory() as tmpdir:
            ge_data = MockGECloudData(config_root=tmpdir)
            ge_data.max_days_previous = 0

            call_count = [0]

            def mock_response_fn(*args, **kwargs):
                call_count[0] += 1

                if call_count[0] == 1:
                    # First page with next link
                    mock_response = create_aiohttp_mock_response(
                        status=200,
                        json_data={
                            "data": [{"time": "2024-12-17T10:00:00Z", "total": {"consumption": 1.0, "grid": {"import": 0.5, "export": 0.0}, "solar": 1.5}}],
                            "links": {"next": "https://api.givenergy.cloud/v1/inverter/test123/data-points/2024-12-17?page=2"},
                        },
                    )
                else:
                    # Second page without next link
                    mock_response = create_aiohttp_mock_response(status=200, json_data={"data": [{"time": "2024-12-17T11:00:00Z", "total": {"consumption": 2.0, "grid": {"import": 0.5, "export": 0.0}, "solar": 2.5}}]})
                mock_session = create_aiohttp_mock_session(mock_response)
                return mock_session

            with patch("aiohttp.ClientSession") as mock_session_class:
                mock_session_class.side_effect = mock_response_fn

                now = datetime(2024, 12, 17, 12, 0, 0)
                result = await ge_data.download_ge_data(now)

                if not result:
                    print("ERROR: Expected download to succeed")
                    return 1
                if call_count[0] != 2:
                    print("ERROR: Expected 2 API calls for pagination, got {}".format(call_count[0]))
                    return 1
                if len(ge_data.mdata) != 2:
                    print("ERROR: Expected 2 data points, got {}".format(len(ge_data.mdata)))
                    return 1

                return 0

    return run_async(test())


def _test_get_ge_url_cache_hit(my_predbat):
    """Test cache hit when data is fresh"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        url = "https://api.givenergy.cloud/v1/test"
        now = datetime(2024, 12, 17, 12, 0, 0)

        # Pre-populate cache with fresh data
        ge_data.ge_url_cache[url] = {"stamp": now - timedelta(minutes=10), "data": [{"test": "cached"}], "next": None}  # 10 minutes old

        with patch("aiohttp.ClientSession") as mock_session_class:
            data, url_next = run_async(ge_data.get_ge_url(url, {}, now, max_age_minutes=30))

            if data != [{"test": "cached"}]:
                print("ERROR: Expected cached data, got {}".format(data))
                return 1
            if mock_session_class.called:
                print("ERROR: Should not have called API for cache hit")
                return 1

        return 0


def _test_get_ge_url_cache_miss(my_predbat):
    """Test cache miss when data is stale"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        url = "https://api.givenergy.cloud/v1/test"
        now = datetime(2024, 12, 17, 12, 0, 0)

        # Pre-populate cache with stale data
        ge_data.ge_url_cache[url] = {"stamp": now - timedelta(minutes=40), "data": [{"test": "old"}], "next": None}  # 40 minutes old

        with patch("aiohttp.ClientSession") as mock_session_class:
            # Mock fresh API response
            mock_response = create_aiohttp_mock_response(status=200, json_data={"data": [{"time": "2024-12-17T12:00:00Z", "total": {"consumption": 1.5, "grid": {"import": 0.5, "export": 0.2}, "solar": 2.0}}]})
            mock_session = create_aiohttp_mock_session(mock_response)
            mock_session_class.return_value = mock_session

            data, url_next = run_async(ge_data.get_ge_url(url, {}, now, max_age_minutes=30))

            if not data or len(data) == 0:
                print("ERROR: Expected fresh data, got {}".format(data))
                return 1
            if data[0]["consumption"] != 1.5:
                print("ERROR: Expected consumption=1.5, got {}".format(data[0]))
                return 1
            if not mock_session_class.called:
                print("ERROR: Should have called API for cache miss")
                return 1

        return 0


def _test_clean_ge_url_cache(my_predbat):
    """Test cleaning old cache entries"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ge_data = MockGECloudData(config_root=tmpdir)

        now = datetime(2024, 12, 17, 12, 0, 0)

        # Add mix of fresh and old entries
        ge_data.ge_url_cache["url1"] = {"stamp": now - timedelta(hours=1), "data": [{"test": "fresh"}], "next": None}  # Fresh
        ge_data.ge_url_cache["url2"] = {"stamp": now - timedelta(hours=25), "data": [{"test": "old"}], "next": None}  # Old (>24 hours)
        ge_data.ge_url_cache["url3"] = {"stamp": now - timedelta(hours=2), "data": [{"test": "fresh2"}], "next": None}  # Fresh

        ge_data.clean_ge_url_cache(now)

        if "url1" not in ge_data.ge_url_cache:
            print("ERROR: Fresh entry url1 should not be removed")
            return 1
        if "url2" in ge_data.ge_url_cache:
            print("ERROR: Old entry url2 should be removed")
            return 1
        if "url3" not in ge_data.ge_url_cache:
            print("ERROR: Fresh entry url3 should not be removed")
            return 1

        return 0


# =============================================================================
# Cache Persistence Tests
# =============================================================================


def _test_load_save_ge_cache(my_predbat):
    """Test saving and loading ge_url_cache via storage component"""
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = StorageLocalFiles(tmpdir, print)
        ge_data = MockGECloudData(config_root=tmpdir)
        ge_data._mock_storage = storage

        now = datetime(2024, 12, 17, 12, 0, 0)
        test_cache = {"test_url": {"stamp": now.isoformat(), "data": [{"test": "data"}], "next": None}}

        # Save via storage
        result = run_async(storage.save("gecloud_data", "ge_url_cache", test_cache, format="yaml", expiry=None))
        if not result:
            print("ERROR: Storage save failed")
            return 1

        # Load via storage into a new instance
        loaded = run_async(storage.load("gecloud_data", "ge_url_cache"))
        if loaded is None:
            print("ERROR: Storage load returned None")
            return 1

        if "test_url" not in loaded:
            print("ERROR: Cache should be loaded from storage")
            return 1

        if loaded["test_url"]["data"] != [{"test": "data"}]:
            print("ERROR: Cached data mismatch: {}".format(loaded["test_url"]["data"]))
            return 1

        return 0


def _test_load_ge_cache_corrupt_file(my_predbat):
    """Test that missing/corrupt storage returns empty cache dict"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # When storage.load returns None (missing file), download_ge_data sets ge_url_cache = {}
        storage = StorageLocalFiles(tmpdir, print)
        loaded = run_async(storage.load("gecloud_data", "ge_url_cache"))
        ge_url_cache = loaded or {}

        if ge_url_cache != {}:
            print("ERROR: Should initialise empty cache when storage returns None")
            return 1

        return 0


# =============================================================================
# Helper Function Tests
# =============================================================================


def _test_regname_to_ha(my_predbat):
    """Test register name to HA entity conversion"""
    # Note: Potential bug found - dots are NOT replaced with underscores
    # This could create invalid HA entity names if register names contain dots
    test_cases = [
        ("Battery SOC", "battery_soc"),
        ("AC Charge Enable", "ac_charge_enable"),
        ("Battery Reserve Percent Limit", "battery_reserve_percent_limit"),
        ("AC Charge 1 Start Time", "ac_charge_1_start_time"),
        ("Test-Name With_Mixed.Case", "test_name_with_mixed.case"),  # Dots NOT replaced (potential bug)
    ]

    for input_name, expected in test_cases:
        result = regname_to_ha(input_name)
        if result != expected:
            print("ERROR: regname_to_ha('{}') expected '{}', got '{}'".format(input_name, expected, result))
            return 1

    return 0


def _test_get_data(my_predbat):
    """Test GECloudData.get_data() method"""
    ge_data = MockGECloudData()

    test_time = datetime(2024, 12, 17, 10, 0, 0)
    ge_data.mdata = [{"consumption": 1.5}]
    ge_data.oldest_data_time = test_time

    mdata, oldest = ge_data.get_data()

    if mdata != [{"consumption": 1.5}]:
        print("ERROR: Expected mdata, got {}".format(mdata))
        return 1
    if oldest != test_time:
        print("ERROR: Expected oldest_data_time, got {}".format(oldest))
        return 1

    return 0


def _test_filter_data(my_predbat):
    """Test GECloudData.filter_data() method"""
    ge_data = MockGECloudData()

    # Test 1: Strictly increasing values — all kept
    mdata = [
        {"last_updated": "T1", "consumption": 100},
        {"last_updated": "T2", "consumption": 110},
        {"last_updated": "T3", "consumption": 120},
    ]
    result = ge_data.filter_data(mdata, "consumption")
    if len(result) != 3:
        print("ERROR test1: Expected 3 items, got {}".format(len(result)))
        return 1
    if [r["consumption"] for r in result] != [100, 110, 120]:
        print("ERROR test1: Expected [100, 110, 120], got {}".format([r["consumption"] for r in result]))
        return 1

    # Test 2: Flat/duplicate values — first kept, intermediates dropped, last always kept
    mdata = [
        {"last_updated": "T1", "consumption": 100},
        {"last_updated": "T2", "consumption": 100},
        {"last_updated": "T3", "consumption": 100},
    ]
    result = ge_data.filter_data(mdata, "consumption")
    if len(result) != 2:
        print("ERROR test2: Expected 2 items (first and last), got {}".format(len(result)))
        return 1
    if result[0]["last_updated"] != "T1" or result[-1]["last_updated"] != "T3":
        print("ERROR test2: Expected T1 and T3, got {} and {}".format(result[0]["last_updated"], result[-1]["last_updated"]))
        return 1

    # Test 3: Decrementing values — skipped unless last
    mdata = [
        {"last_updated": "T1", "consumption": 110},
        {"last_updated": "T2", "consumption": 100},
        {"last_updated": "T3", "consumption": 120},
    ]
    result = ge_data.filter_data(mdata, "consumption")
    # T1 kept (increase from -1), T2 skipped (decrease), T3 kept (increase from 110)
    if len(result) != 2:
        print("ERROR test3: Expected 2 items, got {}".format(len(result)))
        return 1
    if result[0]["last_updated"] != "T1" or result[1]["last_updated"] != "T3":
        print("ERROR test3: Expected T1 and T3, got {} and {}".format(result[0]["last_updated"], result[1]["last_updated"]))
        return 1

    # Test 4: Last item always kept even when it decrements (the bug-fix case)
    mdata = [
        {"last_updated": "T1", "consumption": 100},
        {"last_updated": "T2", "consumption": 99},
    ]
    result = ge_data.filter_data(mdata, "consumption")
    if len(result) != 2:
        print("ERROR test4: Expected 2 items (both kept), got {}".format(len(result)))
        return 1
    if result[0]["last_updated"] != "T1" or result[1]["last_updated"] != "T2":
        print("ERROR test4: Expected T1 and T2, got {} and {}".format(result[0]["last_updated"], result[1]["last_updated"]))
        return 1

    # Test 5: Empty input — returns empty list
    result = ge_data.filter_data([], "consumption")
    if result != []:
        print("ERROR test5: Expected [], got {}".format(result))
        return 1

    # Test 6: Items missing the measurement key — skipped
    mdata = [
        {"last_updated": "T1", "import": 50},
        {"last_updated": "T2", "consumption": 100},
    ]
    result = ge_data.filter_data(mdata, "consumption")
    if len(result) != 1 or result[0]["last_updated"] != "T2":
        print("ERROR test6: Expected only T2, got {}".format(result))
        return 1

    # Test 7: Different measurement key (e.g. import)
    mdata = [
        {"last_updated": "T1", "import": 10},
        {"last_updated": "T2", "import": 20},
        {"last_updated": "T3", "import": 15},
    ]
    result = ge_data.filter_data(mdata, "import")
    # T1 kept, T2 kept (increase), T3 kept (last, even though decrease)
    if len(result) != 3:
        print("ERROR test7: Expected 3 items, got {}".format(len(result)))
        return 1
    if result[-1]["last_updated"] != "T3":
        print("ERROR test7: Expected last item T3, got {}".format(result[-1]["last_updated"]))
        return 1

    return 0


def _test_publish_info_soh(my_predbat):
    """Test SOH calculation in publish_info with various connection/battery configurations"""
    ge_cloud = MockGECloudDirect()

    # --- Case 1: single battery, SOH < 1 ---
    info_data = {
        "info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2, "depth_of_discharge": 0.9}, "model": "GIV-HY3.6", "max_charge_rate": 6000},
        "connections": {"batteries": [{"capacity": {"full": 180.0, "design": 200.0}}]},
    }
    run_async(ge_cloud.publish_info("dev1", info_data))

    expected_soh = dp4(180.0 / 200.0)  # 0.9
    actual_soh = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev1_battery_soh", {}).get("state")
    if actual_soh is None or abs(actual_soh - expected_soh) > 1e-9:
        print("ERROR case1: expected soh={}, got {}".format(expected_soh, actual_soh))
        return 1

    expected_dod_soh = dp4(0.9 * expected_soh)  # 0.81
    actual_dod_soh = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev1_battery_dod_soh", {}).get("state")
    if actual_dod_soh is None or abs(actual_dod_soh - expected_dod_soh) > 1e-9:
        print("ERROR case1: expected dod_soh={}, got {}".format(expected_dod_soh, actual_dod_soh))
        return 1
    print("OK case1: single battery SOH={}, dod_soh={}".format(actual_soh, actual_dod_soh))

    # --- Case 2: multiple batteries, SOH is summed full / summed design ---
    ge_cloud.dashboard_items.clear()
    info_multi = {
        "info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2, "depth_of_discharge": 1.0}, "model": "GIV-HY3.6", "max_charge_rate": 6000},
        "connections": {
            "batteries": [
                {"capacity": {"full": 90.0, "design": 100.0}},
                {"capacity": {"full": 85.0, "design": 100.0}},
            ]
        },
    }
    run_async(ge_cloud.publish_info("dev2", info_multi))

    expected_soh_multi = (90.0 + 85.0) / (100.0 + 100.0)  # 0.875
    actual_soh_multi = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev2_battery_soh", {}).get("state")
    if actual_soh_multi is None or abs(actual_soh_multi - expected_soh_multi) > 1e-9:
        print("ERROR case2: expected soh={}, got {}".format(expected_soh_multi, actual_soh_multi))
        return 1
    expected_dod_soh_multi = 1.0 * expected_soh_multi
    actual_dod_soh_multi = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev2_battery_dod_soh", {}).get("state")
    if actual_dod_soh_multi is None or abs(actual_dod_soh_multi - expected_dod_soh_multi) > 1e-9:
        print("ERROR case2: expected dod_soh={}, got {}".format(expected_dod_soh_multi, actual_dod_soh_multi))
        return 1
    print("OK case2: multiple batteries SOH={}, dod_soh={}".format(actual_soh_multi, actual_dod_soh_multi))

    # --- Case 3: no connections section — SOH should default to 1.0 ---
    ge_cloud.dashboard_items.clear()
    info_no_conn = {
        "info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2, "depth_of_discharge": 0.8}, "model": "GIV-HY3.6", "max_charge_rate": 6000},
    }
    run_async(ge_cloud.publish_info("dev3", info_no_conn))

    actual_soh_no_conn = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev3_battery_soh", {}).get("state")
    if actual_soh_no_conn != 1.0:
        print("ERROR case3: expected soh=1.0 (no connections), got {}".format(actual_soh_no_conn))
        return 1
    expected_dod_soh_no_conn = 0.8 * 1.0
    actual_dod_soh_no_conn = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev3_battery_dod_soh", {}).get("state")
    if actual_dod_soh_no_conn is None or abs(actual_dod_soh_no_conn - expected_dod_soh_no_conn) > 1e-9:
        print("ERROR case3: expected dod_soh={}, got {}".format(expected_dod_soh_no_conn, actual_dod_soh_no_conn))
        return 1
    print("OK case3: no connections SOH=1.0, dod_soh={}".format(actual_dod_soh_no_conn))

    # --- Case 4: connections present but batteries list is empty — SOH should default to 1.0 ---
    ge_cloud.dashboard_items.clear()
    info_empty_batt = {
        "info": {"battery": {"nominal_capacity": 186, "nominal_voltage": 51.2, "depth_of_discharge": 0.9}, "model": "GIV-HY3.6", "max_charge_rate": 6000},
        "connections": {"batteries": []},
    }
    run_async(ge_cloud.publish_info("dev4", info_empty_batt))

    actual_soh_empty = ge_cloud.dashboard_items.get("sensor.predbat_gecloud_dev4_battery_soh", {}).get("state")
    if actual_soh_empty != 1.0:
        print("ERROR case4: expected soh=1.0 (empty battery list), got {}".format(actual_soh_empty))
        return 1
    print("OK case4: empty battery list SOH=1.0")

    # --- Case 5: battery_scaling config uses battery_dod_soh entity after async_automatic_config ---
    ge_cloud.dashboard_items.clear()
    ge_cloud.config_args = {}
    ge_cloud.settings = {"battery001": {}}
    devices = {"ems": None, "gateway": None, "battery": ["battery001"]}

    async def _check_battery_scaling():
        await ge_cloud.async_automatic_config(devices)

    run_async(_check_battery_scaling())
    battery_scaling = ge_cloud.config_args.get("battery_scaling", [])
    if not battery_scaling or "battery_dod_soh" not in battery_scaling[0]:
        print("ERROR case5: expected battery_scaling to use battery_dod_soh entity, got {}".format(battery_scaling))
        return 1
    print("OK case5: battery_scaling uses battery_dod_soh entity: {}".format(battery_scaling))

    return 0


def _test_get_max_inverter_rate_from_model(my_predbat):
    """Test get_max_inverter_rate_from_model with all docstring cases"""
    ge_cloud = MockGECloudDirect()

    test_cases = [
        # (model, max_charge_rate, expected, description)
        # Rating at end of string
        ("GIV-AC3.0", None, 3000, "GIV-AC3.0 => 3kW"),
        ("GIV-HY3.6", None, 3600, "GIV-HY3.6 => 3.6kW"),
        ("GIV-HY5.0", None, 5000, "GIV-HY5.0 => 5kW"),
        # Rating mid-string (suffix is non-numeric)
        ("GIV-HY-10.0-G3-HV", None, 10000, "GIV-HY-10.0-G3-HV => 10kW"),
        ("GIV-HY-8.0-G3-HV", None, 8000, "GIV-HY-8.0-G3-HV => 8kW"),
        # Multiple decimals - last one wins
        ("GIV-AIO-AC-13.5-12.0", None, 12000, "GIV-AIO-AC-13.5-12.0 => 12kW (last decimal wins)"),
        # All-In-One: no decimal extractable for inverter power, falls back to max_charge_rate
        ("All-In-One", 6000, 6000, "All-In-One => fallback to max_charge_rate"),
        # No number at all - returns None when no max_charge_rate provided
        ("Gateway", None, None, "Gateway => None (no number)"),
        ("Plant EMS", None, None, "Plant EMS => None (no number)"),
        # No number, but max_charge_rate provided as fallback
        ("Gateway", 2600, 2600, "Gateway => fallback to max_charge_rate"),
    ]

    failed = 0
    for model, max_charge_rate, expected, description in test_cases:
        ge_cloud.log_messages = []
        result = ge_cloud.get_max_inverter_rate_from_model(model, max_charge_rate)
        if result != expected:
            print("ERROR {}: expected {}, got {}".format(description, expected, result))
            failed += 1
        else:
            print("OK {}: got {}".format(description, result))

    return 1 if failed else 0
