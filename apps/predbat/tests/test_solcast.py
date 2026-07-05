# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import json
import hashlib
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import pytz
import aiohttp

from solcast import SolarAPI
from storage import StorageLocalFiles
from tests.test_infra import run_async, create_aiohttp_mock_response


class MockComponents:
    """Minimal components stub that provides a StorageLocalFiles backend."""

    def __init__(self, storage_backend):
        """Initialise with a storage backend."""
        self._storage = storage_backend

    def get_component(self, name):
        """Return the named component, or None if unknown."""
        if name == "storage":
            return self._storage
        return None

    def __bool__(self):
        """Always truthy so the storage property guard passes."""
        return True


class MockBase:
    """
    Mock base object that provides attributes normally provided by PredBat.
    ComponentBase.__init__ copies some attributes from base and delegates others via properties.
    """

    def __init__(self):
        self.config_root = tempfile.mkdtemp()
        self.plan_interval_minutes = 5
        self.prefix = "predbat"
        self.local_tz = pytz.timezone("Europe/London")
        self.now_utc = datetime(2025, 6, 15, 12, 0, 0, tzinfo=pytz.utc)
        self.now_utc_exact = datetime(2025, 6, 15, 12, 0, 0, tzinfo=pytz.utc)
        self.midnight_utc = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
        self.minutes_now = 12 * 60  # 12:00
        self.forecast_days = 4
        self.dashboard_items = {}
        self.mock_ha_states = {}
        self.mock_history = {}
        self.fatal_error = False
        self.args = {}
        self.currency_symbols = ["p", "£"]
        self.arg_errors = []
        self.components = MockComponents(StorageLocalFiles(self.config_root, self.log))

    def log(self, message):
        """Mock log - print for debugging"""
        if "DEBUG:" in str(message):
            print(message)

    def call_notify(self, message):
        """Mock notify method"""
        self.log_messages.append("Alert: " + message)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Track dashboard_item calls"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes}

    def minute_data_import_export(self, max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True):
        """Return empty history - no historical PV data in tests"""
        return {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Mock get_state_wrapper"""
        if entity_id in self.mock_ha_states:
            result = self.mock_ha_states[entity_id]
            if raw:
                return result
            elif isinstance(result, dict):
                if attribute:
                    return result.get(attribute, default)
                return result.get("state", default)
            if attribute:
                return default
            return result
        return default

    def set_state_wrapper(self, entity_id, state, attributes={}, required_unit=None):
        """Mock set_state_wrapper"""
        self.mock_ha_states[entity_id] = {"state": state, "attributes": attributes}

    def get_history_wrapper(self, entity_id, days=30, required=False, tracked=True):
        """Mock get_history_wrapper"""
        return self.mock_history.get(entity_id, [])

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Mock get_arg - looks in mock_config then returns default"""
        if hasattr(self, "mock_config") and arg in self.mock_config:
            return self.mock_config[arg]
        return default

    def set_arg(self, arg, value):
        """Mock set_arg"""
        if not hasattr(self, "mock_config"):
            self.mock_config = {}
        self.mock_config[arg] = value

    def get_ha_config(self, name, default):
        """Mock get_ha_config"""
        return default


class TestSolarAPI:
    """
    Test wrapper that creates a real SolarAPI instance with mocked HTTP.
    Tests the actual SolarAPI code, not copies.

    ComponentBase.__init__ does:
        self.base = base
        self.log = base.log         <- direct copy
        self.local_tz = base.local_tz
        self.prefix = base.prefix
        self.args = base.args
        self.api_started = False
        self.api_stop = False
        etc.

    We bypass ComponentBase.__init__ and set these manually.
    """

    def __init__(self):
        # Create mock base that provides all needed methods/attributes
        self.mock_base = MockBase()

        # Create real SolarAPI without calling __init__ chain
        self.solar = SolarAPI.__new__(SolarAPI)

        # Set up attributes that ComponentBase.__init__ would normally set
        self.solar.base = self.mock_base
        self.solar.log = self.mock_base.log  # ComponentBase copies this
        self.solar.local_tz = self.mock_base.local_tz
        self.solar.prefix = self.mock_base.prefix
        self.solar.args = self.mock_base.args
        self.solar.api_started = False
        self.solar.api_stop = False
        self.solar.last_success_timestamp = None
        self.solar.count_errors = 0

        # Now call SolarAPI.initialize() - the real initialization code
        self.solar.initialize(
            solcast_host=None,
            solcast_api_key=None,
            solcast_sites=None,
            solcast_poll_hours=4,
            forecast_solar=None,
            forecast_solar_max_age=4,
            forecast_solar_open_meteo_backup=False,
            pv_forecast_today=None,
            pv_forecast_tomorrow=None,
            pv_forecast_d3=None,
            pv_forecast_d4=None,
            pv_scaling=1.0,
            open_meteo_forecast=None,
            open_meteo_forecast_max_age=1.0,
        )

        # Mock HTTP responses storage
        self.mock_responses = {}
        self.request_log = []

    def cleanup(self):
        """Clean up temp directory"""
        if self.mock_base.config_root and os.path.exists(self.mock_base.config_root):
            shutil.rmtree(self.mock_base.config_root)

    def patch_now_utc_exact(self):
        """Return a context manager that patches now_utc_exact to use mock_base value"""
        return patch.object(type(self.solar), "now_utc_exact", new_callable=lambda: property(lambda self: self.base.now_utc_exact))

    def set_mock_response(self, url_substring, response, status_code=200):
        """Set a mock HTTP response for URLs containing the substring"""
        self.mock_responses[url_substring] = {"data": response, "status_code": status_code}

    def set_mock_ha_state(self, entity_id, value):
        """Set a mock HA state"""
        self.mock_base.mock_ha_states[entity_id] = value

    def set_mock_history(self, entity_id, history):
        """Set mock history for an entity"""
        self.mock_base.mock_history[entity_id] = history

    @property
    def dashboard_items(self):
        return self.mock_base.dashboard_items

    def mock_aiohttp_session(self, url=None, params=None):
        """Create mock aiohttp ClientSession that returns configured responses based on URL"""
        # Create a mock session that will return different responses based on URL
        mock_session = MagicMock()

        def get_side_effect(request_url, params=None):
            """Track request and return appropriate mock response"""
            self.request_log.append({"url": request_url, "params": params})

            # Find matching mock response
            for url_substring, mock in self.mock_responses.items():
                if url_substring in request_url:
                    mock_response = create_aiohttp_mock_response(status=mock["status_code"], json_data=mock["data"])
                    # Create mock context manager for the response
                    mock_context = MagicMock()

                    async def aenter(*args, **kwargs):
                        return mock_response

                    async def aexit(*args):
                        return None

                    mock_context.__aenter__ = aenter
                    mock_context.__aexit__ = aexit
                    return mock_context

            # No mock found - simulate connection error
            raise aiohttp.ClientError(f"No mock for URL: {request_url}")

        mock_session.get = MagicMock(side_effect=get_side_effect)
        mock_session.post = MagicMock(side_effect=get_side_effect)

        # Setup session context manager
        async def session_aenter(*args):
            return mock_session

        async def session_aexit(*args):
            return None

        mock_session.__aenter__ = session_aenter
        mock_session.__aexit__ = session_aexit

        return mock_session


def create_test_solar_api():
    """Create a TestSolarAPI instance"""
    return TestSolarAPI()


# ============================================================================
# Pure Function Tests
# ============================================================================


def test_convert_azimuth(my_predbat):
    """
    Test azimuth conversion from Solcast format to Forecast.Solar format.
    Solcast: 0=North, -90=East, 90=West, 180=South
    Forecast.Solar: 0=South, -90=East, 90=West, 180=North
    """
    print("  - test_convert_azimuth")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Test cases: (solcast_azimuth, expected_forecast_solar_azimuth)
        test_cases = [
            (0, 180),  # North -> North (180 in forecast.solar)
            (180, 0),  # South -> South (0 in forecast.solar)
            (90, 90),  # West -> West
            (-90, -90),  # East -> East
            (45, 135),  # NW -> NW
            (-45, -135),  # NE -> NE
            (-180, 0),  # South (negative) -> South
        ]

        for solcast_az, expected in test_cases:
            result = test_api.solar.convert_azimuth(solcast_az)
            if result != expected:
                print(f"ERROR: convert_azimuth({solcast_az}) = {result}, expected {expected}")
                failed = True
    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Cache Tests
# ============================================================================


def test_cache_get_url_miss(my_predbat):
    """
    Test cache_get_url when cache file doesn't exist - should fetch and cache.
    """
    print("  - test_cache_get_url_miss")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Set mock response for this URL
        mock_data = {"test": "data", "value": 123}
        test_api.set_mock_response("solcast.com.au/test", mock_data, 200)

        url = "https://api.solcast.com.au/test/endpoint"
        params = {"param1": "value1"}

        # Patch requests.get to use our mock
        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.cache_get_url(url, params, max_age=60))

        # Verify result
        if result != mock_data:
            print(f"ERROR: Expected {mock_data}, got {result}")
            failed = True

        # Verify metrics incremented for Solcast
        if test_api.solar.solcast_requests_total != 1:
            print(f"ERROR: solcast_requests_total should be 1, got {test_api.solar.solcast_requests_total}")
            failed = True

        # Verify cache file was written
        cache_path = test_api.mock_base.config_root + "/cache"
        if not os.path.exists(cache_path):
            print(f"ERROR: Cache directory not created")
            failed = True

        # Check that at least one cache file exists
        cache_files = os.listdir(cache_path) if os.path.exists(cache_path) else []
        if len(cache_files) == 0:
            print(f"ERROR: No cache file was written")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_cache_get_url_hit(my_predbat):
    """
    Test cache_get_url when storage has a fresh entry - should return cached data.
    """
    print("  - test_cache_get_url_hit")
    failed = False

    test_api = create_test_solar_api()
    try:
        url = "https://api.solcast.com.au/test/cached"
        params = {}

        # Compute hash key same as cache_get_url
        hash_key = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_key = hash_key.replace("/", "_").replace(":", "_").replace("?", "a").replace("&", "b").replace("*", "c")

        # Pre-populate storage with a non-expired, recently-created entry (age ~0 < max_age)
        cached_data = {"cached": True, "from": "file"}
        expiry = datetime.now(timezone.utc) + timedelta(days=7)
        run_async(test_api.solar.storage.save("solar", hash_key, cached_data, format="json", expiry=expiry))

        # Set up a mock response that returns different data (should NOT be used)
        test_api.set_mock_response("solcast.com.au/test/cached", {"fresh": "data"}, 200)

        # Call cache_get_url with max_age=60 - should use storage since entry is fresh
        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.cache_get_url(url, params, max_age=60))

        # Verify cached data was returned
        if result != cached_data:
            print(f"ERROR: Expected cached data {cached_data}, got {result}")
            failed = True

        # Verify HTTP request was NOT made (cache hit)
        if len(test_api.request_log) > 0:
            print(f"ERROR: HTTP should not have been called for fresh cache, got {len(test_api.request_log)} requests")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def _backdate_storage_meta(storage_backend, module, filename, hours_ago):
    """Backdate the created timestamp in a storage meta file to simulate stale data."""
    meta_path = storage_backend._meta_path(module, filename)
    with open(meta_path, "r") as f:
        meta = json.load(f)
    meta["created"] = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with open(meta_path, "w") as f:
        json.dump(meta, f)


def test_cache_get_url_stale(my_predbat):
    """
    Test cache_get_url when cached data is older than max_age - should re-fetch and return fresh data.
    """
    print("  - test_cache_get_url_stale")
    failed = False

    test_api = create_test_solar_api()
    try:
        url = "https://api.solcast.com.au/test/stale"
        params = {}

        hash_key = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_key = hash_key.replace("/", "_").replace(":", "_").replace("?", "a").replace("&", "b").replace("*", "c")

        # Save data with 7-day expiry so it won't be removed, then backdate creation to 2 hours ago
        old_data = {"stale": True}
        run_async(test_api.solar.storage.save("solar", hash_key, old_data, format="json", expiry=datetime.now(timezone.utc) + timedelta(days=7)))
        _backdate_storage_meta(test_api.solar.storage, "solar", hash_key, hours_ago=2)

        # Setup mock response for fresh data
        fresh_data = {"fresh": True, "new": "data"}
        test_api.set_mock_response("solcast.com.au/test/stale", fresh_data, 200)

        # Call with max_age=60 minutes - cached data is 2 hours old so should re-fetch
        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.cache_get_url(url, params, max_age=60))

        # Verify fresh data was returned
        if result != fresh_data:
            print(f"ERROR: Expected fresh data {fresh_data}, got {result}")
            failed = True

        # Verify HTTP was called
        if len(test_api.request_log) == 0:
            print(f"ERROR: HTTP should have been called for stale cache")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_cache_get_url_failure_with_stale_cache(my_predbat):
    """
    Test cache_get_url when HTTP fails but stale cache exists - should return stale data.
    """
    print("  - test_cache_get_url_failure_with_stale_cache")
    failed = False

    test_api = create_test_solar_api()
    try:
        url = "https://api.solcast.com.au/test/failure"
        params = {}

        hash_key = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_key = hash_key.replace("/", "_").replace(":", "_").replace("?", "a").replace("&", "b").replace("*", "c")

        # Save data with 7-day expiry then backdate creation to 2 hours ago to make it stale
        stale_data = {"stale": True, "fallback": "data"}
        run_async(test_api.solar.storage.save("solar", hash_key, stale_data, format="json", expiry=datetime.now(timezone.utc) + timedelta(days=7)))
        _backdate_storage_meta(test_api.solar.storage, "solar", hash_key, hours_ago=2)

        # Don't set mock response - will cause ConnectionError
        test_api.solar.solcast_requests_total = 0
        test_api.solar.solcast_failures_total = 0

        # Call cache_get_url - should return stale data on failure
        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.cache_get_url(url, params, max_age=60))

        # Verify failure counter incremented
        if test_api.solar.solcast_failures_total != 1:
            print(f"ERROR: solcast_failures_total should be 1, got {test_api.solar.solcast_failures_total}")
            failed = True

        # Stale data should be returned
        if result != stale_data:
            print(f"ERROR: Expected stale data {stale_data}, got {result}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_cache_get_url_metrics_forecast_solar(my_predbat):
    """
    Test that forecast.solar URLs increment the correct metrics.
    """
    print("  - test_cache_get_url_metrics_forecast_solar")
    failed = False

    test_api = create_test_solar_api()
    try:
        mock_data = {"result": {"watts": {}}, "message": {"info": {"time": "2025-06-15T12:00:00+0000"}}}
        test_api.set_mock_response("forecast.solar", mock_data, 200)

        url = "https://api.forecast.solar/estimate/51.5/-0.1/30/0/3"
        params = {}

        test_api.solar.forecast_solar_requests_total = 0
        test_api.solar.forecast_solar_failures_total = 0
        test_api.solar.solcast_requests_total = 0

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.cache_get_url(url, params, max_age=60))

        # Should increment forecast_solar metrics, not solcast
        if test_api.solar.forecast_solar_requests_total != 1:
            print(f"ERROR: forecast_solar_requests_total should be 1, got {test_api.solar.forecast_solar_requests_total}")
            failed = True

        if test_api.solar.solcast_requests_total != 0:
            print(f"ERROR: solcast_requests_total should be 0, got {test_api.solar.solcast_requests_total}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Solcast Data Download Tests
# ============================================================================


def test_download_solcast_data(my_predbat):
    """
    Test download_solcast_data with mock API responses.
    """
    print("  - test_download_solcast_data")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Configure for Solcast API
        test_api.solar.solcast_host = "https://api.solcast.com.au"
        test_api.solar.solcast_api_key = "test_key"
        test_api.solar.solcast_sites = ["site123"]  # Pre-configure sites

        # Mock the forecast response
        forecast_response = {
            "forecasts": [
                {
                    "period_end": "2025-06-15T12:30:00.0000000Z",
                    "period": "PT30M",
                    "pv_estimate": 2.5,
                    "pv_estimate10": 1.5,
                    "pv_estimate90": 3.5,
                },
                {
                    "period_end": "2025-06-15T13:00:00.0000000Z",
                    "period": "PT30M",
                    "pv_estimate": 3.0,
                    "pv_estimate10": 2.0,
                    "pv_estimate90": 4.0,
                },
            ]
        }
        test_api.set_mock_response("forecasts", forecast_response, 200)

        # Call download_solcast_data
        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.download_solcast_data())

        # Verify we got data back
        if result is None or len(result) == 0:
            print(f"ERROR: Expected forecast data, got {result}")
            failed = True

        # Verify data structure and values
        if result and len(result) > 0:
            first_entry = result[0]
            if "period_start" not in first_entry:
                print(f"ERROR: Expected 'period_start' in forecast entry, got {first_entry.keys()}")
                failed = True
            if "pv_estimate" not in first_entry:
                print(f"ERROR: Expected 'pv_estimate' in forecast entry")
                failed = True

            # Verify we got 2 forecast entries
            if len(result) != 2:
                print(f"ERROR: Expected 2 forecast entries, got {len(result)}")
                failed = True

            # Verify actual values - period is 30 min, so pv_estimate is scaled by 30/60 = 0.5
            # First entry: 2.5 kW * 0.5 = 1.25 kWh
            expected_first_pv = 2.5 * 30 / 60  # 1.25
            actual_first_pv = first_entry.get("pv_estimate", 0)
            if abs(actual_first_pv - expected_first_pv) > 0.01:
                print(f"ERROR: Expected first pv_estimate ~{expected_first_pv}, got {actual_first_pv}")
                failed = True

            # Second entry: 3.0 kW * 0.5 = 1.5 kWh
            if len(result) >= 2:
                second_entry = result[1]
                expected_second_pv = 3.0 * 30 / 60  # 1.5
                actual_second_pv = second_entry.get("pv_estimate", 0)
                if abs(actual_second_pv - expected_second_pv) > 0.01:
                    print(f"ERROR: Expected second pv_estimate ~{expected_second_pv}, got {actual_second_pv}")
                    failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_solcast_data_multi_site(my_predbat):
    """
    Test download_solcast_data aggregates data from multiple sites.
    """
    print("  - test_download_solcast_data_multi_site")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.solcast_host = "https://api.solcast.com.au"
        test_api.solar.solcast_api_key = "test_key"
        test_api.solar.solcast_sites = ["site1", "site2"]

        # Mock responses - each site returns same time periods
        forecast_response = {
            "forecasts": [
                {
                    "period_end": "2025-06-15T12:30:00.0000000Z",
                    "period": "PT30M",
                    "pv_estimate": 1.0,  # 1 kW per site
                    "pv_estimate10": 0.5,
                    "pv_estimate90": 1.5,
                },
            ]
        }
        test_api.set_mock_response("forecasts", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.download_solcast_data())

        # Verify data was fetched for both sites (2 requests)
        if len(test_api.request_log) < 2:
            print(f"ERROR: Expected at least 2 API calls for 2 sites, got {len(test_api.request_log)}")
            failed = True

        # Verify aggregation - values should be summed
        if result and len(result) > 0:
            first_entry = result[0]
            # With 2 sites each returning 1.0 kW, aggregate should be 2.0
            # But the value is scaled by period_minutes/60, so 1.0 * 30/60 = 0.5 per site = 1.0 total
            expected_pv = 1.0
            actual_pv = first_entry.get("pv_estimate", 0)
            if abs(actual_pv - expected_pv) > 0.1:
                print(f"ERROR: Expected aggregated pv_estimate ~{expected_pv}, got {actual_pv}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Forecast.Solar Data Download Tests
# ============================================================================


def test_download_forecast_solar_data(my_predbat):
    """
    Test download_forecast_solar_data with mock API response.
    """
    print("  - test_download_forecast_solar_data")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Configure for Forecast.Solar API
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,  # South in Solcast format
                "kwp": 3.0,
                "efficiency": 0.9,
            }
        ]

        # Mock response - using TIME_FORMAT compatible timestamps
        forecast_response = {
            "result": {
                "watts": {
                    "2025-06-15T12:00:00+0000": 500,
                    "2025-06-15T12:30:00+0000": 600,
                    "2025-06-15T13:00:00+0000": 700,
                }
            },
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        # Verify we got data
        if result is None or len(result) == 0:
            print(f"ERROR: Expected forecast data, got {result}")
            failed = True

        # Verify max_kwh calculation (kwp * efficiency)
        expected_max_kwh = 3.0 * 0.9
        if abs(max_kwh - expected_max_kwh) > 0.01:
            print(f"ERROR: Expected max_kwh {expected_max_kwh}, got {max_kwh}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_forecast_solar_data_with_postcode(my_predbat):
    """
    Test download_forecast_solar_data resolves postcode to lat/lon.
    """
    print("  - test_download_forecast_solar_data_with_postcode")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "postcode": "SW1A 1AA",
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "efficiency": 1.0,
            }
        ]

        # Mock responses
        postcode_response = {
            "result": {
                "latitude": 51.501,
                "longitude": -0.141,
            }
        }
        test_api.set_mock_response("postcodes.io", postcode_response, 200)

        forecast_response = {
            "result": {
                "watts": {
                    "2025-06-15T12:00:00+0000": 500,
                }
            },
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        # Verify we received a sensible maximum kWh value from the forecast
        if max_kwh <= 0:
            print("ERROR: Expected positive max_kwh from forecast.solar data")
            failed = True
        # Verify postcode API was called
        postcode_calls = [r for r in test_api.request_log if "postcodes.io" in r["url"]]
        if len(postcode_calls) == 0:
            print(f"ERROR: Expected postcode API call")
            failed = True

        # Verify forecast was fetched
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) == 0:
            print(f"ERROR: Expected forecast.solar API call")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_forecast_solar_data_with_postcode_lookup_failure(my_predbat):
    """
    Test download_forecast_solar_data handles postcode lookup returning no data.
    """
    print("  - test_download_forecast_solar_data_with_postcode_lookup_failure")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "postcode": "SW1A 1AA",
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "efficiency": 1.0,
            }
        ]

        # postcode API returns error (cache_get_url returns None)
        test_api.set_mock_response("postcodes.io", {"error": "rate limit"}, 429)
        forecast_response = {"result": {"watts": {"2025-06-15T12:00:00+0000": 500}}, "message": {"info": {"time": "2025-06-15T11:30:00+0000"}}}
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        if result is None:
            print("ERROR: Expected forecast data dict when postcode lookup fails, got None")
            failed = True
        elif len(result) == 0:
            print("ERROR: Expected non-empty forecast data when postcode lookup fails")
            failed = True
        expected_max_kwh = 3.0 * 1.0
        if abs(max_kwh - expected_max_kwh) > 0.01:
            print(f"ERROR: Expected max_kwh {expected_max_kwh} when postcode lookup fails, got {max_kwh}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_forecast_solar_data_personal_api(my_predbat):
    """
    Test download_forecast_solar_data uses personal API URL when api_key provided.
    """
    print("  - test_download_forecast_solar_data_personal_api")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "api_key": "personal_key_123",
            }
        ]

        forecast_response = {"result": {"watts": {"2025-06-15T12:00:00+0000": 500}}, "message": {"info": {"time": "2025-06-15T11:30:00+0000"}}}
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        # Basic sanity check on returned data so variables are actually used
        if result is None or max_kwh is None:
            print("ERROR: download_forecast_solar_data returned None result or max_kwh")
            failed = True
        # Verify personal API URL was used (contains api_key in path)
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) > 0:
            url = forecast_calls[0]["url"]
            if "personal_key_123" not in url:
                print(f"ERROR: Expected personal API URL with api_key, got {url}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_forecast_solar_data_dual_plane(my_predbat):
    """
    Test download_forecast_solar_data issues a single dual-plane URL when two consecutive
    personal-API planes share the same lat/lon, and that efficiency is baked into the kwp
    values sent in the URL.
    """
    print("  - test_download_forecast_solar_data_dual_plane")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Two planes at same location with same api_key => should be one dual-plane request
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,  # South in Solcast convention
                "kwp": 4.0,
                "efficiency": 0.9,
                "api_key": "personal_key_abc",
            },
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 15,
                "azimuth": -90,  # East
                "kwp": 2.0,
                "efficiency": 0.8,
                "api_key": "personal_key_abc",
            },
        ]

        forecast_response = {
            "result": {
                "watts": {
                    "2025-06-15T12:00:00+0000": 1000,
                    "2025-06-15T12:30:00+0000": 1200,
                }
            },
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        # Should have made exactly ONE request (dual-plane, not two separate requests)
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) != 1:
            print(f"ERROR: Expected exactly 1 dual-plane request, got {len(forecast_calls)}")
            failed = True

        if len(forecast_calls) > 0:
            url = forecast_calls[0]["url"]
            # Must contain api_key in path (personal URL)
            if "personal_key_abc" not in url:
                print(f"ERROR: Expected personal API key in URL, got {url}")
                failed = True
            # Must contain dec1 and dec2 (dual-plane path segments)
            # URL format: /estimate/{lat}/{lon}/{dec1}/{az1}/{kwp1}/{dec2}/{az2}/{kwp2}
            # kwp1 = 4.0 * 0.9 = 3.6, kwp2 = 2.0 * 0.8 = 1.6
            expected_kwp1 = 4.0 * 0.9  # 3.6
            expected_kwp2 = 2.0 * 0.8  # 1.6
            if str(expected_kwp1) not in url:
                print(f"ERROR: Expected kwp1={expected_kwp1} (efficiency baked in) in URL, got {url}")
                failed = True
            if str(expected_kwp2) not in url:
                print(f"ERROR: Expected kwp2={expected_kwp2} (efficiency baked in) in URL, got {url}")
                failed = True
            # Must NOT be a single-plane URL (single-plane URLs don't have dec2/az2/kwp2 segments)
            # The dual-plane URL has 9 path segments after /estimate/ vs 5 for single-plane
            if url.count("/") < 12:
                print(f"ERROR: URL does not appear to be dual-plane (too few path segments): {url}")
                failed = True

        # max_kwh = kwp1*eff1 + kwp2*eff2 = 4.0*0.9 + 2.0*0.8 = 3.6 + 1.6 = 5.2
        expected_max_kwh = 4.0 * 0.9 + 2.0 * 0.8
        if abs(max_kwh - expected_max_kwh) > 0.01:
            print(f"ERROR: Expected max_kwh={expected_max_kwh}, got {max_kwh}")
            failed = True

        # Should have returned some forecast data
        if result is None or len(result) == 0:
            print(f"ERROR: Expected forecast data, got {result}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_forecast_solar_data_dual_plane_not_paired_different_location(my_predbat):
    """
    Test that two personal-API planes at different lat/lon are NOT paired into a dual-plane
    call — they should each produce a separate HTTP request.
    """
    print("  - test_download_forecast_solar_data_dual_plane_not_paired_different_location")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "api_key": "personal_key_abc",
            },
            {
                "latitude": 52.0,  # Different location
                "longitude": -1.0,
                "declination": 25,
                "azimuth": 0,
                "kwp": 2.0,
                "api_key": "personal_key_abc",
            },
        ]

        forecast_response = {
            "result": {"watts": {"2025-06-15T12:00:00+0000": 500}},
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        # Should have made exactly TWO separate requests (different locations cannot be paired)
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) != 2:
            print(f"ERROR: Expected 2 separate requests for different locations, got {len(forecast_calls)}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_forecast_solar_data_rate_limited_no_cache(my_predbat):
    """
    Test download_forecast_solar_data handles forecast.solar 429 with no cache.
    """
    print("  - test_download_forecast_solar_data_rate_limited_no_cache")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "efficiency": 1.0,
            }
        ]
        test_api.set_mock_response("forecast.solar", {"error": "rate limit"}, 429)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        if result != []:
            print(f"ERROR: Expected empty list result for 429/no-cache, got {result}")
            failed = True
        # With the multi-config rate-limit fix, the function returns ([], 0) immediately
        # when a 429 is received, so max_kwh is 0 (not accumulated from the config loop).
        if max_kwh != 0:
            print(f"ERROR: Expected max_kwh=0 when forecast.solar is rate limited with no cache, got {max_kwh}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_forecast_solar_rate_limit_suppresses_fetch(my_predbat):
    """
    Test that after a 429 response the rate-limit is set and subsequent calls to
    download_forecast_solar_data return ([], 0) immediately without making HTTP requests.
    """
    print("  - test_forecast_solar_rate_limit_suppresses_fetch")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "efficiency": 1.0,
            }
        ]

        # Step 1: first call gets a 429 → rate limit should be stored
        test_api.set_mock_response("forecast.solar", {"error": "rate limit"}, 429)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.download_forecast_solar_data())

        if test_api.solar.forecast_solar_rate_limit_until is None:
            print("ERROR: forecast_solar_rate_limit_until should be set after 429")
            failed = True

        rate_limit_time = test_api.solar.forecast_solar_rate_limit_until

        # Verify retry time is 60-120 minutes in the future
        now_utc = datetime.now(timezone.utc)
        if rate_limit_time is not None:
            delta_minutes = (rate_limit_time - now_utc).total_seconds() / 60
            if not (59 <= delta_minutes <= 121):
                print(f"ERROR: Rate limit retry should be 60-120 minutes from now, got {delta_minutes:.1f} minutes")
                failed = True

        # Step 2: second call should be suppressed - no HTTP request, returns ([], 0)
        test_api.request_log.clear()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        if result != []:
            print(f"ERROR: Expected empty list during rate limit backoff, got {result}")
            failed = True

        if max_kwh != 0:
            print(f"ERROR: Expected max_kwh=0 during rate limit backoff, got {max_kwh}")
            failed = True

        if len(test_api.request_log) != 0:
            print(f"ERROR: Expected no HTTP requests during rate limit backoff, got {len(test_api.request_log)}")
            failed = True

        # Rate limit should still be set (not expired yet)
        if test_api.solar.forecast_solar_rate_limit_until != rate_limit_time:
            print("ERROR: Rate limit time should not change while still active")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_forecast_solar_rate_limit_expires(my_predbat):
    """
    Test that once the rate-limit window has passed, download_forecast_solar_data
    clears the rate limit and resumes fetching from forecast.solar.
    """
    print("  - test_forecast_solar_rate_limit_expires")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [
            {
                "latitude": 51.5,
                "longitude": -0.1,
                "declination": 30,
                "azimuth": 0,
                "kwp": 3.0,
                "efficiency": 1.0,
            }
        ]

        # Simulate a rate-limit window that has already expired (1 second in the past)
        from datetime import datetime, timezone, timedelta

        test_api.solar.forecast_solar_rate_limit_until = datetime.now(timezone.utc) - timedelta(seconds=1)

        # Provide a successful mock response for the resumed fetch
        forecast_response = {
            "result": {
                "watts": {
                    "2025-06-15T12:00:00+0000": 500,
                    "2025-06-15T13:00:00+0000": 600,
                }
            },
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

        # Rate limit should have been cleared
        if test_api.solar.forecast_solar_rate_limit_until is not None:
            print("ERROR: forecast_solar_rate_limit_until should be cleared after expiry")
            failed = True

        # HTTP request should have been made
        if len(test_api.request_log) == 0:
            print("ERROR: Expected HTTP request after rate limit expired, got none")
            failed = True

        # Result should contain forecast data (not empty)
        if not result:
            print(f"ERROR: Expected forecast data after rate limit expired, got {result}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# HA Sensor Datapoints Tests
# ============================================================================


def test_fetch_pv_datapoints_detailed_forecast(my_predbat):
    """
    Test fetch_pv_datapoints extracts data from detailedForecast attribute.
    """
    print("  - test_fetch_pv_datapoints_detailed_forecast")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup mock sensor with detailedForecast attribute
        test_api.set_mock_ha_state(
            "sensor.solcast_forecast_today",
            {
                "state": "5.5",
                "detailedForecast": [
                    {"period_start": "2025-06-15T12:00:00+0000", "pv_estimate": 1.0},
                    {"period_start": "2025-06-15T12:30:00+0000", "pv_estimate": 1.5},
                ],
            },
        )

        data, total_data, total_sensor = test_api.solar.fetch_pv_datapoints("pv_forecast_today", "sensor.solcast_forecast_today")

        # Verify data was extracted
        if data is None or len(data) == 0:
            print(f"ERROR: Expected forecast data from sensor, got {data}")
            failed = True

        if len(data) != 2:
            print(f"ERROR: Expected 2 data points, got {len(data)}")
            failed = True

        # Verify actual data values
        if data and len(data) >= 2:
            # Check first data point
            first = data[0]
            if "period_start" not in first:
                print(f"ERROR: Expected 'period_start' in first data point, got {first.keys()}")
                failed = True
            if first.get("pv_estimate") != 1.0:
                print(f"ERROR: Expected first pv_estimate=1.0, got {first.get('pv_estimate')}")
                failed = True

            # Check second data point
            second = data[1]
            if second.get("pv_estimate") != 1.5:
                print(f"ERROR: Expected second pv_estimate=1.5, got {second.get('pv_estimate')}")
                failed = True

        # Verify totals
        if total_sensor != 5.5:
            print(f"ERROR: Expected total_sensor 5.5, got {total_sensor}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_datapoints_forecast_fallback(my_predbat):
    """
    Test fetch_pv_datapoints falls back to 'forecast' attribute if 'detailedForecast' missing.
    """
    print("  - test_fetch_pv_datapoints_forecast_fallback")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup mock sensor with only 'forecast' attribute (old format)
        test_api.set_mock_ha_state(
            "sensor.solcast_forecast_today",
            {
                "state": "4.0",
                "forecast": [
                    {"period_start": "2025-06-15T12:00:00+0000", "pv_estimate": 2.0},
                ],
            },
        )

        data, total_data, total_sensor = test_api.solar.fetch_pv_datapoints("pv_forecast_today", "sensor.solcast_forecast_today")

        # Verify data was extracted from 'forecast' attribute
        if data is None or len(data) == 0:
            print(f"ERROR: Expected forecast data from 'forecast' attribute, got {data}")
            failed = True

        # Verify actual data values
        if data and len(data) >= 1:
            first = data[0]
            if "period_start" not in first:
                print(f"ERROR: Expected 'period_start' in data point, got {first.keys()}")
                failed = True
            if first.get("pv_estimate") != 2.0:
                print(f"ERROR: Expected pv_estimate=2.0, got {first.get('pv_estimate')}")
                failed = True

        # Verify total_sensor matches state
        if total_sensor != 4.0:
            print(f"ERROR: Expected total_sensor 4.0, got {total_sensor}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Publish PV Stats Tests
# ============================================================================


def test_publish_pv_stats(my_predbat):
    """
    Test publish_pv_stats publishes correct dashboard entities.
    """
    print("  - test_publish_pv_stats")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Create sample forecast data spanning 2 days
        pv_forecast_data = [
            {"period_start": "2025-06-15T06:00:00+0000", "pv_estimate": 0.5, "pv_estimate10": 0.3, "pv_estimate90": 0.7},
            {"period_start": "2025-06-15T12:00:00+0000", "pv_estimate": 2.0, "pv_estimate10": 1.5, "pv_estimate90": 2.5},
            {"period_start": "2025-06-15T18:00:00+0000", "pv_estimate": 0.5, "pv_estimate10": 0.3, "pv_estimate90": 0.7},
            {"period_start": "2025-06-16T12:00:00+0000", "pv_estimate": 2.5, "pv_estimate10": 2.0, "pv_estimate90": 3.0},
        ]

        test_api.solar.publish_pv_stats(pv_forecast_data, divide_by=1.0, period=30)

        # Verify today's entity was published
        today_entity = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if today_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {today_entity} to be published")
            failed = True
        else:
            today_item = test_api.dashboard_items[today_entity]
            if "total" not in today_item["attributes"]:
                print(f"ERROR: Expected 'total' in today's attributes")
                failed = True
            if "remaining" not in today_item["attributes"]:
                print(f"ERROR: Expected 'remaining' in today's attributes")
                failed = True

            # Verify actual values - today has 3 data points: 0.5 + 2.0 + 0.5 = 3.0 kWh
            total = today_item["attributes"].get("total", 0)
            expected_total = 3.0  # Sum of pv_estimate values for today
            if abs(total - expected_total) > 0.1:
                print(f"ERROR: Expected today total ~{expected_total}, got {total}")
                failed = True

        # Verify tomorrow's entity was published
        tomorrow_entity = f"sensor.{test_api.mock_base.prefix}_pv_tomorrow"
        if tomorrow_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {tomorrow_entity} to be published")
            failed = True
        else:
            tomorrow_item = test_api.dashboard_items[tomorrow_entity]
            # Tomorrow has 1 data point: 2.5 kWh
            total_tomorrow = tomorrow_item["attributes"].get("total", 0)
            expected_tomorrow = 2.5
            if abs(total_tomorrow - expected_tomorrow) > 0.1:
                print(f"ERROR: Expected tomorrow total ~{expected_tomorrow}, got {total_tomorrow}")
                failed = True

        # Verify forecast now entity
        now_entity = f"sensor.{test_api.mock_base.prefix}_pv_forecast_h0"
        if now_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {now_entity} to be published")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_publish_pv_stats_remaining_calculation(my_predbat):
    """
    Test publish_pv_stats correctly calculates remaining PV for today.
    """
    print("  - test_publish_pv_stats_remaining_calculation")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Set time to noon - need to patch the property since it computes datetime.now()
        noon_utc = datetime(2025, 6, 15, 12, 0, 0, tzinfo=pytz.utc)
        test_api.mock_base.now_utc = noon_utc
        test_api.mock_base.midnight_utc = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)

        # Forecast data - morning slot already passed, afternoon slot remaining
        pv_forecast_data = [
            {"period_start": "2025-06-15T06:00:00+0000", "pv_estimate": 1.0},  # Past
            {"period_start": "2025-06-15T14:00:00+0000", "pv_estimate": 2.0},  # Future
            {"period_start": "2025-06-15T18:00:00+0000", "pv_estimate": 1.0},  # Future
        ]

        # Mock now_utc_exact property to return our fixed time
        with patch.object(type(test_api.solar), "now_utc_exact", new_callable=lambda: property(lambda self: noon_utc)):
            test_api.solar.publish_pv_stats(pv_forecast_data, divide_by=1.0, period=30)

        today_entity = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if today_entity in test_api.dashboard_items:
            attrs = test_api.dashboard_items[today_entity]["attributes"]
            total = attrs.get("total", 0)
            remaining = attrs.get("remaining", 0)

            # Total should be sum of all today's PV: 1.0 + 2.0 + 1.0 = 4.0 kWh
            expected_total = 4.0
            if abs(total - expected_total) > 0.1:
                print(f"ERROR: Expected total ~{expected_total}, got {total}")
                failed = True

            # Remaining should only include future slots (after noon): 2.0 + 1.0 = 3.0 kWh
            expected_remaining = 3.0
            if abs(remaining - expected_remaining) > 0.1:
                print(f"ERROR: Expected remaining ~{expected_remaining}, got {remaining}")
                failed = True

            # Remaining should not exceed total
            if remaining > total:
                print(f"ERROR: Remaining ({remaining}) should not exceed total ({total})")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_publish_pv_stats_missing_day_zero(my_predbat):
    """
    Test publish_pv_stats handles missing day 0 forecast data without KeyError.
    This regression test ensures the fix for the KeyError when accessing total_day[0]
    when day 0 has no forecast data.
    """
    print("  - test_publish_pv_stats_missing_day_zero")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Create forecast data that starts on day 1 (tomorrow), skipping day 0 (today)
        # This can happen if today's forecast data is stale or missing
        pv_forecast_data = [
            {"period_start": "2025-06-16T06:00:00+0000", "pv_estimate": 0.5, "pv_estimate10": 0.3, "pv_estimate90": 0.7},
            {"period_start": "2025-06-16T12:00:00+0000", "pv_estimate": 2.0, "pv_estimate10": 1.5, "pv_estimate90": 2.5},
            {"period_start": "2025-06-17T12:00:00+0000", "pv_estimate": 1.5, "pv_estimate10": 1.0, "pv_estimate90": 2.0},
        ]

        # This should not raise KeyError
        test_api.solar.publish_pv_stats(pv_forecast_data, divide_by=1.0, period=30)

        # Verify today's entity was published with zero values
        today_entity = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if today_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {today_entity} to be published even with no data")
            failed = True
        else:
            today_item = test_api.dashboard_items[today_entity]
            total = today_item["attributes"].get("total", -1)
            # Today should have 0 kWh since no forecast data exists for day 0
            if total != 0:
                print(f"ERROR: Expected today total to be 0 (no data), got {total}")
                failed = True

        # Verify tomorrow's entity was published with actual data
        tomorrow_entity = f"sensor.{test_api.mock_base.prefix}_pv_tomorrow"
        if tomorrow_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {tomorrow_entity} to be published")
            failed = True
        else:
            tomorrow_item = test_api.dashboard_items[tomorrow_entity]
            total_tomorrow = tomorrow_item["attributes"].get("total", 0)
            expected_tomorrow = 2.5  # Sum of day 1 data
            if abs(total_tomorrow - expected_tomorrow) > 0.1:
                print(f"ERROR: Expected tomorrow total ~{expected_tomorrow}, got {total_tomorrow}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Pack and Store Forecast Tests
# ============================================================================


def check_packed_value(packed, minute, expected, name="packed"):
    """Helper to check a value in a packed forecast dict."""
    if minute not in packed:
        print(f"ERROR: Expected minute {minute} in {name} forecast")
        return True  # failed
    elif abs(packed[minute] - expected) > 0.01:
        print(f"ERROR: Expected {name}[{minute}] ~{expected}, got {packed[minute]}")
        return True  # failed
    return False  # passed


def test_pack_and_store_forecast(my_predbat):
    """
    Test pack_and_store_forecast creates compressed forecast data.
    """
    print("  - test_pack_and_store_forecast")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Create forecast data with some constant and some varying values
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        for minute in range(0, 4 * 24 * 60):
            # Varies every hour
            pv_forecast_minute[minute] = 1.0 if (minute // 60) % 2 == 0 else 2.0
            pv_forecast_minute10[minute] = 0.5 if (minute // 60) % 2 == 0 else 1.0

        test_api.solar.pack_and_store_forecast(pv_forecast_minute, pv_forecast_minute10)

        # Verify entity was published
        forecast_entity = f"sensor.{test_api.mock_base.prefix}_pv_forecast_raw"
        if forecast_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {forecast_entity} to be published")
            failed = True
        else:
            attrs = test_api.dashboard_items[forecast_entity]["attributes"]
            if "forecast" not in attrs:
                print(f"ERROR: Expected 'forecast' in attributes")
                failed = True
            if "forecast10" not in attrs:
                print(f"ERROR: Expected 'forecast10' in attributes")
                failed = True

            # Packed forecast should have fewer entries than original (compression)
            # Format is {minute: value} dict with only changed values
            packed = attrs["forecast"]
            packed10 = attrs["forecast10"]
            original_count = len(pv_forecast_minute)
            packed_count = len(packed)
            if packed_count >= original_count:
                print(f"ERROR: Packed forecast ({packed_count}) should have fewer entries than original ({original_count})")
                failed = True

            # Verify packed data structure and values
            # Even hours have value 1.0, odd hours have value 2.0
            failed |= check_packed_value(packed, 0, 1.0, "packed")
            failed |= check_packed_value(packed, 60, 2.0, "packed")
            failed |= check_packed_value(packed, 120, 1.0, "packed")

            # Check forecast10 has same structure (even=0.5, odd=1.0)
            failed |= check_packed_value(packed10, 0, 0.5, "packed10")
            failed |= check_packed_value(packed10, 60, 1.0, "packed10")

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Integration Tests (one per mode)
# ============================================================================


def test_fetch_pv_forecast_solcast_direct(my_predbat):
    """
    Integration test: fetch_pv_forecast using Solcast direct API.
    """
    print("  - test_fetch_pv_forecast_solcast_direct")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Configure for Solcast direct API
        test_api.solar.solcast_host = "https://api.solcast.com.au"
        test_api.solar.solcast_api_key = "test_key"
        test_api.solar.solcast_sites = ["site123"]
        test_api.solar.forecast_solar = None

        forecast_response = {
            "forecasts": [
                {
                    "period_end": "2025-06-15T12:30:00.0000000Z",
                    "period": "PT30M",
                    "pv_estimate": 2.0,
                    "pv_estimate10": 1.0,
                    "pv_estimate90": 3.0,
                },
                {
                    "period_end": "2025-06-15T13:00:00.0000000Z",
                    "period": "PT30M",
                    "pv_estimate": 2.5,
                    "pv_estimate10": 1.5,
                    "pv_estimate90": 3.5,
                },
            ]
        }
        test_api.set_mock_response("forecasts", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Verify dashboard items were published
        if f"sensor.{test_api.mock_base.prefix}_pv_today" not in test_api.dashboard_items:
            print(f"ERROR: Expected pv_today entity to be published")
            failed = True

        if f"sensor.{test_api.mock_base.prefix}_pv_forecast_raw" not in test_api.dashboard_items:
            print(f"ERROR: Expected pv_forecast_raw entity to be published")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_forecast_solar(my_predbat):
    """
    Integration test: fetch_pv_forecast using Forecast.Solar API.
    """
    print("  - test_fetch_pv_forecast_forecast_solar")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Configure for Forecast.Solar API
        test_api.solar.solcast_host = None
        test_api.solar.solcast_api_key = None
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]

        forecast_response = {
            "result": {
                "watts": {
                    "2025-06-15T12:00:00+0000": 500,
                    "2025-06-15T12:30:00+0000": 600,
                }
            },
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Verify Forecast.Solar API was called
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) == 0:
            print(f"ERROR: Expected Forecast.Solar API call")
            failed = True

        # Verify dashboard items were published
        if f"sensor.{test_api.mock_base.prefix}_pv_today" not in test_api.dashboard_items:
            print(f"ERROR: Expected pv_today entity to be published")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_forecast_solar_open_meteo_backup_on_failure(my_predbat):
    """
    When forecast.solar returns no data and forecast_solar_open_meteo_backup is True,
    fetch_pv_forecast falls back to Open-Meteo.
    """
    print("  - test_fetch_pv_forecast_forecast_solar_open_meteo_backup_on_failure")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.forecast_solar_open_meteo_backup = True
        test_api.solar.open_meteo_forecast_max_age = 1.0
        # forecast.solar returns a server error — download_forecast_solar_data returns ([], 0)
        test_api.set_mock_response("forecast.solar", {"error": "server error"}, 500)
        # Open-Meteo returns valid hourly data
        test_api.set_mock_response(
            "api.open-meteo.com",
            {
                "hourly": {
                    "time": ["2025-06-15T12:00", "2025-06-15T13:00", "2025-06-15T14:00"],
                    "global_tilted_irradiance": [500.0, 600.0, 550.0],
                    "temperature_2m": [25.0, 25.0, 25.0],
                    "wind_speed_10m": [1.0, 1.0, 1.0],
                }
            },
        )
        test_api.set_mock_response(
            "ensemble-api.open-meteo.com",
            {
                "hourly": {
                    "time": ["2025-06-15T12:00", "2025-06-15T13:00", "2025-06-15T14:00"],
                    "global_tilted_irradiance_member01": [400.0, 480.0, 440.0],
                }
            },
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Open-Meteo should have been called (fallback activated)
        open_meteo_calls = [r for r in test_api.request_log if "open-meteo.com" in r["url"]]
        if len(open_meteo_calls) == 0:
            print("ERROR: Expected Open-Meteo API call during fallback, got none")
            failed = True

        # Forecast data should have been published (came from Open-Meteo)
        if f"sensor.{test_api.mock_base.prefix}_pv_today" not in test_api.dashboard_items:
            print("ERROR: Expected pv_today sensor to be published after Open-Meteo fallback")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_forecast_solar_open_meteo_backup_not_used_on_success(my_predbat):
    """
    When forecast.solar returns data successfully, Open-Meteo backup is not called
    even when forecast_solar_open_meteo_backup is True.
    """
    print("  - test_fetch_pv_forecast_forecast_solar_open_meteo_backup_not_used_on_success")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.forecast_solar_open_meteo_backup = True
        # forecast.solar returns valid data
        test_api.set_mock_response(
            "forecast.solar",
            {
                "result": {
                    "watts": {
                        "2025-06-15T12:00:00+0000": 500,
                        "2025-06-15T12:30:00+0000": 600,
                    }
                },
                "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
            },
            200,
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Open-Meteo should NOT have been called
        open_meteo_calls = [r for r in test_api.request_log if "open-meteo.com" in r["url"]]
        if len(open_meteo_calls) != 0:
            print(f"ERROR: Expected no Open-Meteo calls when forecast.solar succeeds, got {len(open_meteo_calls)}")
            failed = True

        # Forecast.Solar should have been called and data published
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) == 0:
            print("ERROR: Expected Forecast.Solar API call, got none")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_ha_sensors(my_predbat):
    """
    Integration test: fetch_pv_forecast using HA sensors (Solcast integration).
    """
    print("  - test_fetch_pv_forecast_ha_sensors")
    failed = False

    test_api = create_test_solar_api()
    try:
        # No direct API configured - should use HA sensors
        test_api.solar.solcast_host = None
        test_api.solar.solcast_api_key = None
        test_api.solar.forecast_solar = None
        test_api.solar.pv_forecast_today = "sensor.solcast_pv_forecast_today"
        test_api.solar.pv_forecast_tomorrow = "sensor.solcast_pv_forecast_tomorrow"

        # Setup mock sensors
        test_api.set_mock_ha_state(
            "sensor.solcast_pv_forecast_today",
            {
                "state": "5.5",
                "detailedForecast": [
                    {"period_start": "2025-06-15T12:00:00+0000", "pv_estimate": 2.0},
                    {"period_start": "2025-06-15T12:30:00+0000", "pv_estimate": 2.5},
                ],
            },
        )
        test_api.set_mock_ha_state(
            "sensor.solcast_pv_forecast_tomorrow",
            {
                "state": "6.0",
                "detailedForecast": [
                    {"period_start": "2025-06-16T12:00:00+0000", "pv_estimate": 3.0},
                ],
            },
        )

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Verify no external API calls were made
        if len(test_api.request_log) > 0:
            print(f"ERROR: Expected no external API calls when using HA sensors, got {len(test_api.request_log)}")
            failed = True

        # Verify dashboard items were published
        if f"sensor.{test_api.mock_base.prefix}_pv_today" not in test_api.dashboard_items:
            print(f"ERROR: Expected pv_today entity to be published from HA sensors")
            failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# 15-minute resolution tests
# ============================================================================


def test_fetch_pv_forecast_ha_sensors_15min_kwh(my_predbat):
    """
    Integration test: fetch_pv_forecast using HA sensors with 15-minute kWh resolution data.
    Verifies that the energy totals are correct (not halved) when 15-minute data is used.
    Each pv_estimate entry is in kWh for the 15-minute period.
    """
    print("  - test_fetch_pv_forecast_ha_sensors_15min_kwh")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.solcast_host = None
        test_api.solar.solcast_api_key = None
        test_api.solar.forecast_solar = None
        test_api.solar.pv_forecast_today = "sensor.pv_forecast_today"
        test_api.solar.pv_forecast_tomorrow = None

        # 15-minute resolution data - pv_estimate is kWh per 15-min slot
        # 4 slots of 0.25 kWh each = 1.0 kWh total (matching sensor state)
        forecast_data_15min = [
            {"period_start": "2025-06-15T10:00:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T10:15:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T10:30:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T10:45:00+0000", "pv_estimate": 0.25},
        ]
        # Sensor state = total daily kWh = 1.0
        test_api.set_mock_ha_state(
            "sensor.pv_forecast_today",
            {
                "state": "1.0",
                "detailedForecast": forecast_data_15min,
            },
        )

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Verify dashboard items were published
        today_entity = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if today_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {today_entity} to be published")
            failed = True
        else:
            today_item = test_api.dashboard_items[today_entity]
            total = today_item["attributes"].get("total", 0)
            # Total should be 4 * 0.25 = 1.0 kWh, NOT 0.5 kWh (which would indicate the bug)
            expected_total = 1.0
            if abs(total - expected_total) > 0.05:
                print(f"ERROR: Expected today total ~{expected_total} kWh with 15-min data, got {total} kWh (possible 15-min handling bug)")
                failed = True
            else:
                print(f"  15-min kWh data: total={total} kWh (expected {expected_total}) - correct!")

        # Verify forecast_raw entity was published
        if f"sensor.{test_api.mock_base.prefix}_pv_forecast_raw" not in test_api.dashboard_items:
            print(f"ERROR: Expected pv_forecast_raw entity to be published")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_ha_sensors_15min_kw(my_predbat):
    """
    Integration test: fetch_pv_forecast using HA sensors with 15-minute kW resolution data.
    Verifies that energy totals are correct when pv_estimate is in kW (new Solcast style)
    with 15-minute period resolution.
    """
    print("  - test_fetch_pv_forecast_ha_sensors_15min_kw")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.solcast_host = None
        test_api.solar.solcast_api_key = None
        test_api.solar.forecast_solar = None
        test_api.solar.pv_forecast_today = "sensor.pv_forecast_today"
        test_api.solar.pv_forecast_tomorrow = None

        # 15-minute resolution data - pv_estimate is kW (power), not kWh
        # 4 slots of 1.0 kW each = 4 * 0.25h * 1.0 kW = 1.0 kWh total
        # sum(pv_estimates) = 4.0, sensor state = 1.0 kWh => factor = 4.0
        forecast_data_15min_kw = [
            {"period_start": "2025-06-15T10:00:00+0000", "pv_estimate": 1.0},
            {"period_start": "2025-06-15T10:15:00+0000", "pv_estimate": 1.0},
            {"period_start": "2025-06-15T10:30:00+0000", "pv_estimate": 1.0},
            {"period_start": "2025-06-15T10:45:00+0000", "pv_estimate": 1.0},
        ]
        # Sensor state = total daily kWh = 1.0
        test_api.set_mock_ha_state(
            "sensor.pv_forecast_today",
            {
                "state": "1.0",
                "detailedForecast": forecast_data_15min_kw,
            },
        )

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        today_entity = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if today_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {today_entity} to be published")
            failed = True
        else:
            today_item = test_api.dashboard_items[today_entity]
            total = today_item["attributes"].get("total", 0)
            # Total should be 1.0 kWh (4 slots * 1.0 kW * 0.25h), NOT 0.5 kWh
            expected_total = 1.0
            if abs(total - expected_total) > 0.05:
                print(f"ERROR: Expected today total ~{expected_total} kWh with 15-min kW data, got {total} kWh (possible 15-min handling bug)")
                failed = True
            else:
                print(f"  15-min kW data: total={total} kWh (expected {expected_total}) - correct!")

        if f"sensor.{test_api.mock_base.prefix}_pv_forecast_raw" not in test_api.dashboard_items:
            print(f"ERROR: Expected pv_forecast_raw entity to be published")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_publish_pv_stats_15min_resolution(my_predbat):
    """
    Test publish_pv_stats with 15-minute period data correctly uses point_gap=15
    and computes totals correctly.
    """
    print("  - test_publish_pv_stats_15min_resolution")
    failed = False

    test_api = create_test_solar_api()
    try:
        # 15-minute resolution data - divide_by=1.0, period=15
        # pv_estimate already in kWh per slot: 0.25 kWh each
        pv_forecast_data = [
            {"period_start": "2025-06-15T06:00:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T06:15:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T06:30:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T06:45:00+0000", "pv_estimate": 0.25},
            {"period_start": "2025-06-15T12:00:00+0000", "pv_estimate": 0.5},
            {"period_start": "2025-06-15T12:15:00+0000", "pv_estimate": 0.5},
        ]

        test_api.solar.publish_pv_stats(pv_forecast_data, divide_by=1.0, period=15)

        today_entity = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if today_entity not in test_api.dashboard_items:
            print(f"ERROR: Expected {today_entity} to be published")
            failed = True
        else:
            today_item = test_api.dashboard_items[today_entity]
            total = today_item["attributes"].get("total", 0)
            # Total = 4*0.25 + 2*0.5 = 1.0 + 1.0 = 2.0 kWh
            expected_total = 2.0
            if abs(total - expected_total) > 0.05:
                print(f"ERROR: Expected today total ~{expected_total}, got {total}")
                failed = True
            else:
                print(f"  publish_pv_stats 15-min: total={total} kWh (expected {expected_total}) - correct!")

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Run Function Tests
# ============================================================================


def test_run_at_plan_interval(my_predbat):
    """
    Test SolarAPI.run() calls fetch_pv_forecast when seconds matches plan_interval_minutes.
    """
    print("  - test_run_at_plan_interval")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup: plan_interval_minutes = 5, so should fetch at seconds % (5*60) == 0
        test_api.mock_base.plan_interval_minutes = 5
        test_api.solar.last_fetched_timestamp = None

        # Patch now_utc_exact to return a fixed value from mock_base
        with test_api.patch_now_utc_exact():
            # Mock fetch_pv_forecast to track if it was called
            fetch_called = []

            async def mock_fetch_pv_forecast():
                fetch_called.append(True)

            test_api.solar.fetch_pv_forecast = mock_fetch_pv_forecast

            # Test 1: seconds = 300 (5 minutes) should trigger fetch
            result = run_async(test_api.solar.run(seconds=300, first=False))
            if not result:
                print(f"ERROR: run() returned False, expected True")
                failed = True
            if len(fetch_called) != 1:
                print(f"ERROR: fetch_pv_forecast should be called at seconds=300, call count: {len(fetch_called)}")
                failed = True

            # Test 2: seconds = 150 (2.5 minutes) should NOT trigger fetch
            # Set a recent timestamp so fetch_age is small
            from datetime import timedelta

            test_api.solar.last_fetched_timestamp = test_api.mock_base.now_utc_exact - timedelta(minutes=1)
            fetch_called.clear()
            result = run_async(test_api.solar.run(seconds=150, first=False))
            if not result:
                print(f"ERROR: run() returned False, expected True")
                failed = True
            if len(fetch_called) != 0:
                print(f"ERROR: fetch_pv_forecast should NOT be called at seconds=150, call count: {len(fetch_called)}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_run_new_day_trigger(my_predbat):
    """
    Test SolarAPI.run() fetches data when it's a new day.
    """
    print("  - test_run_new_day_trigger")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup: plan_interval_minutes = 5
        test_api.mock_base.plan_interval_minutes = 5

        with test_api.patch_now_utc_exact():
            # Mock fetch_pv_forecast to track if it was called
            fetch_called = []

            async def mock_fetch_pv_forecast():
                fetch_called.append(True)

            test_api.solar.fetch_pv_forecast = mock_fetch_pv_forecast

            # Set last_fetched_timestamp to yesterday
            from datetime import timedelta

            test_api.solar.last_fetched_timestamp = test_api.mock_base.now_utc_exact - timedelta(days=1)

            # Test: seconds not at interval (e.g., 150), but new day should trigger fetch
            result = run_async(test_api.solar.run(seconds=150, first=False))
            if not result:
                print(f"ERROR: run() returned False, expected True")
                failed = True
            if len(fetch_called) != 1:
                print(f"ERROR: fetch_pv_forecast should be called on new day, call count: {len(fetch_called)}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_run_data_older_than_60_minutes(my_predbat):
    """
    Test SolarAPI.run() fetches data when last fetch was over 60 minutes ago.
    """
    print("  - test_run_data_older_than_60_minutes")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup: plan_interval_minutes = 5
        test_api.mock_base.plan_interval_minutes = 5

        with test_api.patch_now_utc_exact():
            # Mock fetch_pv_forecast to track if it was called
            fetch_called = []

            async def mock_fetch_pv_forecast():
                fetch_called.append(True)

            test_api.solar.fetch_pv_forecast = mock_fetch_pv_forecast

            # Set last_fetched_timestamp to 61 minutes ago (same day)
            from datetime import timedelta

            test_api.solar.last_fetched_timestamp = test_api.mock_base.now_utc_exact - timedelta(minutes=61)

            # Test: seconds not at interval (e.g., 150), but data older than 60 min should trigger fetch
            result = run_async(test_api.solar.run(seconds=150, first=False))
            if not result:
                print(f"ERROR: run() returned False, expected True")
                failed = True
            if len(fetch_called) != 1:
                print(f"ERROR: fetch_pv_forecast should be called when data > 60 min old, call count: {len(fetch_called)}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_run_no_fetch_when_recent(my_predbat):
    """
    Test SolarAPI.run() does NOT fetch when data is recent and not at interval.
    """
    print("  - test_run_no_fetch_when_recent")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup: plan_interval_minutes = 5
        test_api.mock_base.plan_interval_minutes = 5

        with test_api.patch_now_utc_exact():
            # Mock fetch_pv_forecast to track if it was called
            fetch_called = []

            async def mock_fetch_pv_forecast():
                fetch_called.append(True)

            test_api.solar.fetch_pv_forecast = mock_fetch_pv_forecast

            # Set last_fetched_timestamp to 30 minutes ago (same day, within 60 min)
            from datetime import timedelta

            test_api.solar.last_fetched_timestamp = test_api.mock_base.now_utc_exact - timedelta(minutes=30)

            # Test: seconds not at interval (e.g., 150), data is recent, should NOT trigger fetch
            result = run_async(test_api.solar.run(seconds=150, first=False))
            if not result:
                print(f"ERROR: run() returned False, expected True")
                failed = True
            if len(fetch_called) != 0:
                print(f"ERROR: fetch_pv_forecast should NOT be called when data is recent, call count: {len(fetch_called)}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_run_first_fetch_when_no_timestamp(my_predbat):
    """
    Test SolarAPI.run() fetches data when last_fetched_timestamp is None.
    """
    print("  - test_run_first_fetch_when_no_timestamp")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Setup: plan_interval_minutes = 5
        test_api.mock_base.plan_interval_minutes = 5
        test_api.solar.last_fetched_timestamp = None

        with test_api.patch_now_utc_exact():
            # Mock fetch_pv_forecast to track if it was called
            fetch_called = []

            async def mock_fetch_pv_forecast():
                fetch_called.append(True)

            test_api.solar.fetch_pv_forecast = mock_fetch_pv_forecast

            # Test: seconds not at interval (e.g., 150), but no timestamp, should trigger fetch (fetch_age > 60)
            result = run_async(test_api.solar.run(seconds=150, first=False))
            if not result:
                print(f"ERROR: run() returned False, expected True")
                failed = True
            if len(fetch_called) != 1:
                print(f"ERROR: fetch_pv_forecast should be called when last_fetched_timestamp is None, call count: {len(fetch_called)}")
                failed = True

    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Calibration tests
# ============================================================================


def test_pv_calibration_power_conversion(my_predbat):
    """
    Test that pv_calibration correctly converts cumulative pv_today kWh history
    into per-minute power (kW) values and uses them to form slot adjustments.

    Setup: Supply 5 days of synthetic pv_today cumulative-kWh data where each
    previous day produced exactly 2 kWh in a single midday slot.  The forecast
    history (h0 sensor) is set to the same 2 kWh per day so the slot adjustment
    should be ~1.0 (actual ≈ forecast).  We verify:
      - pv_calibration returns without error
      - pv_calibration_total_adjustment is close to 1.0
      - The returned pv_forecast_minute_adjusted values are non-negative
    """
    print("  - test_pv_calibration_power_conversion")
    failed = False

    test_api = create_test_solar_api()
    try:
        solar = test_api.solar
        base = test_api.mock_base

        plan_interval = base.plan_interval_minutes  # 5
        minutes_now = base.minutes_now  # 720 (12:00)

        # Build synthetic cumulative pv_today history.
        # We represent 5 previous days.  Each day, the panel produces 2 kWh between
        # minute 600-660 (10:00–11:00 UTC).  The cumulative sensor counts from
        # minute 0 (now) backwards, so higher minute indices = further in past.
        # pv_today_hist[minute_previous] = cumulative kWh at that moment (backwards).
        pv_today_hist = {}
        days_back = 5
        for day in range(1, days_back + 1):
            day_offset = day * 24 * 60  # minutes back to start of that day
            # Simulate 2 kWh generated between minute 600–660 of that day.
            # After the generation window the sensor stays at 2 kWh for the rest of the day.
            gen_start = day_offset + (24 * 60 - 660)  # relative to minutes_now window
            gen_end = day_offset + (24 * 60 - 600)
            for m in range(day_offset, day_offset + 24 * 60):
                # Cumulative value: 0 before gen_start, ramps to 2 after gen_end
                if m < gen_start:
                    pv_today_hist[m] = 0.0
                elif m < gen_end:
                    pv_today_hist[m] = 2.0 * (m - gen_start) / (gen_end - gen_start)
                else:
                    pv_today_hist[m] = 2.0

        # Override minute_data_import_export to return this synthetic history
        base.mock_pv_today_hist = pv_today_hist

        def mock_minute_data_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True):
            if key == "pv_today":
                return base.mock_pv_today_hist
            return {}

        base.minute_data_import_export = mock_minute_data_import_export

        # No forecast history → enabled_calibration will be False (< 3 days), but power
        # conversion and capped_data paths still execute.
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        # Build a simple pv_forecast_minute: constant 0.05 kW per minute for 4 days
        total_minutes = 4 * 24 * 60
        pv_forecast_minute = {m: 0.05 for m in range(total_minutes)}
        pv_forecast_minute10 = {m: 0.04 for m in range(total_minutes)}
        pv_forecast_data = [{"period_start": base.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": 0.05}]

        adj_minute, adj_minute10, adj_data = solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)

        # Returned minute data must be non-negative
        if any(v < 0 for v in adj_minute.values()):
            print("ERROR: pv_calibration returned negative adjusted forecast values")
            failed = True

        # total_adjustment should be set (even if 1.0 due to disabled calibration)
        if not hasattr(solar, "pv_calibration_total_adjustment"):
            print("ERROR: pv_calibration_total_adjustment was not set")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_sparse_recent_history_no_crash(my_predbat):
    """
    Regression test: pv_today history that has data but no entry at the most
    recent 5-minute boundaries (e.g. a freshly added sensor with almost no
    history yet) used to crash pv_calibration with:
      TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType'
    because current_value/next_value stayed None until a real data point was
    found walking backwards through the history. pv_calibration must instead
    skip those undated minutes and fall back to an uncalibrated forecast.
    """
    print("  - test_pv_calibration_sparse_recent_history_no_crash")
    failed = False

    test_api = create_test_solar_api()
    try:
        solar = test_api.solar
        base = test_api.mock_base

        # Only a single, old data point - no entry near "now" (minutes 0-10), so the
        # backwards walk starts with several None/None lookups before finding data.
        pv_today_hist = {10: 5.2}

        def mock_minute_data_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True):
            if key == "pv_today":
                return pv_today_hist
            return {}

        base.minute_data_import_export = mock_minute_data_import_export
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        total_minutes = 4 * 24 * 60
        pv_forecast_minute = {m: 0.05 for m in range(total_minutes)}
        pv_forecast_minute10 = {m: 0.04 for m in range(total_minutes)}
        pv_forecast_data = [{"period_start": base.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": 0.05}]

        try:
            adj_minute, adj_minute10, adj_data = solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)
        except TypeError as e:
            print("ERROR: pv_calibration raised TypeError with sparse recent history: {}".format(e))
            failed = True
            return failed

        if any(v < 0 for v in adj_minute.values()):
            print("ERROR: pv_calibration returned negative adjusted forecast values")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_partial_history(my_predbat):
    """
    Test that pv_calibration enables/disables based on the true available history
    length returned by minute_data_import_export (consistent with pad=False).

    With pad=False, the actual number of days of pv_today history is used to
    determine pv_today_hist_days.  Two sub-cases are exercised:

    Case 1 – 2 days of history (hist_days < 3):
        Calibration must be DISABLED.  Scaling factors must be the hard-coded
        defaults: worst_scaling=0.7, best_scaling=1.3, total_adjustment=1.0.

    Case 2 – 5 days of history (hist_days >= 3):
        Calibration must be ENABLED.  The scaling factors are computed from
        actual vs forecast data, so they must NOT be the disabled defaults 0.7
        and 1.3.
    """
    print("  - test_pv_calibration_partial_history")
    failed = False

    def make_pv_today_hist(days_back, minutes_now=720):
        """
        Build synthetic per-minute cumulative pv_today kWh readings for days_back days.

        Convention: key = minutes-ago from noon (minutes_now=720).
        midnight_ago for day D = D*1440 + 720 (because noon – midnight = 720 min).

        Each past day generates 0.5 kWh between 10:00-11:00 UTC (actual_min
        600-660, where actual_min counts from midnight of that day).
        max(keys()) = days_back * 1440 + 720, so pv_today_hist_days = int(max/1440) = days_back.
        """
        hist = {}
        for day in range(1, days_back + 1):
            midnight_ago = day * 1440 + minutes_now  # minutes-ago for midnight of this past day
            for step in range(0, 24 * 60, 5):
                minute_ago = midnight_ago - step
                if minute_ago < 0:
                    continue
                actual_min = step  # minute-of-day (0=midnight, 600=10:00, 660=11:00)
                if actual_min < 600:
                    cumulative = 0.0
                elif actual_min < 660:
                    cumulative = 0.5 * (actual_min - 600) / 60.0
                else:
                    cumulative = 0.5
                hist[minute_ago] = cumulative
        return hist

    def make_h0_ha_history(now_utc, days_back):
        """
        Build HA-format h0 forecast history (kW sensor) spanning days_back days.
        Returns 1.0 kW forecast during 10:00-11:00 UTC for each past day so that
        the oldest timestamp is days_back days ago → pv_forecast_hist_days = days_back.
        """
        entries = []
        for day in range(days_back, 0, -1):
            ref = (now_utc - timedelta(days=day)).replace(hour=10, minute=0, second=0, microsecond=0)
            entries.append({"last_updated": ref.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": "1.0"})
            ref30 = ref + timedelta(minutes=30)
            entries.append({"last_updated": ref30.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": "1.0"})
            ref_end = ref + timedelta(hours=1)
            entries.append({"last_updated": ref_end.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": "0.0"})
        return [entries]

    for days_back, expect_enabled in [(2, False), (5, True)]:
        test_api = create_test_solar_api()
        try:
            solar = test_api.solar
            base = test_api.mock_base

            pv_today_hist = make_pv_today_hist(days_back)
            h0_ha_history = make_h0_ha_history(base.now_utc_exact, days_back)

            def mock_minute_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=pv_today_hist):
                if key == "pv_today":
                    if pad:
                        # Simulate the real pad=True behavior: extend the dict so that
                        # max(keys()) == max_days_previous * 1440, making pv_today_hist_days
                        # report the full requested window regardless of actual data length.
                        padded = dict(_hist)
                        max_actual = max(_hist.keys()) if _hist else 0
                        target_max = max_days_previous * 24 * 60
                        last_val = _hist.get(max_actual, 0) if _hist else 0
                        for m in range(max_actual + 1, target_max + 1, 5):
                            padded[m] = last_val
                        return padded
                    else:
                        return dict(_hist)
                return {}

            def mock_get_history(entity_id, days, required=False, _h0=h0_ha_history):
                if "pv_forecast_h0" in entity_id:
                    return _h0
                return []

            base.minute_data_import_export = mock_minute_import_export
            solar.get_history_wrapper = mock_get_history

            total_minutes = 4 * 24 * 60
            pv_forecast_minute = {m: 0.02 for m in range(total_minutes)}
            pv_forecast_minute10 = {m: 0.01 for m in range(total_minutes)}
            pv_forecast_data = [{"period_start": "2025-06-15T00:00:00+0000", "pv_estimate": 0.5}]

            # Build synthetic h0 forecast history: 1.0 kW during 10:00-11:00 UTC for each past day.
            # From noon (minutes_now=720): day d at 10:00 is (d*1440+120) min ago, 11:00 is (d*1440+60) min ago.
            pv_forecast_hist = {}
            for d in range(1, days_back + 1):
                for m_ago in range(d * 1440 + 60, d * 1440 + 121):
                    pv_forecast_hist[m_ago] = 1.0

            with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days_back)):
                solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)

            worst = solar.pv_calibration_worst_scaling
            best = solar.pv_calibration_best_scaling
            total = solar.pv_calibration_total_adjustment

            if not expect_enabled:
                # 2-day history → calibration disabled → hard-coded defaults must be used
                if abs(worst - 0.7) > 0.001:
                    print("ERROR: {}-day history (disabled): worst_scaling should be 0.7, got {}".format(days_back, worst))
                    failed = True
                if abs(best - 1.3) > 0.001:
                    print("ERROR: {}-day history (disabled): best_scaling should be 1.3, got {}".format(days_back, best))
                    failed = True
                if total != 1.0:
                    print("ERROR: {}-day history (disabled): total_adjustment should be 1.0, got {}".format(days_back, total))
                    failed = True
            else:
                # 5-day history → calibration enabled → must NOT produce the disabled-default values
                # (actual 0.5 kWh/day < forecast 1.0 kWh/day → worst and best both < 1.0, not 0.7/1.3)
                if abs(worst - 0.7) < 0.001:
                    print("ERROR: {}-day history (enabled): worst_scaling is 0.7 (disabled default) – calibration appears disabled".format(days_back))
                    failed = True
                if abs(best - 1.3) < 0.001:
                    print("ERROR: {}-day history (enabled): best_scaling is 1.3 (disabled default) – calibration appears disabled".format(days_back))
                    failed = True

        finally:
            test_api.cleanup()

    return failed


def test_pv_calibration_capped_data_clamp(my_predbat):
    """
    Test that the capped_data clamp in pv_calibration correctly limits the
    calibrated slot estimates when max historical power is lower than the forecast.

    Setup: Historical power is 1 kW max; forecast is 3 kW max; max_kwh panel
    limit is 2 kW.  After calibration the capped_data should be
    min(max(1, 3), 2) * plan_interval / 60 per slot, and every pv_estimateCL
    value written back into pv_forecast_data must be ≤ capped_data * divide_by.
    """
    print("  - test_pv_calibration_capped_data_clamp")
    failed = False

    test_api = create_test_solar_api()
    try:
        solar = test_api.solar
        base = test_api.mock_base
        plan_interval = base.plan_interval_minutes  # 5

        # Historical data: max power is 1 kW (= 1000 W), 5 days back
        # Cumulative kWh sensor increments by 1/60 kWh per minute during a 1-hour window
        pv_today_hist = {}
        for day in range(1, 6):
            day_offset = day * 24 * 60
            gen_start = day_offset + (24 * 60 - 660)
            gen_end = day_offset + (24 * 60 - 600)
            for m in range(day_offset, day_offset + 24 * 60):
                if m < gen_start:
                    pv_today_hist[m] = 0.0
                elif m < gen_end:
                    pv_today_hist[m] = 1.0 * (m - gen_start) / (gen_end - gen_start)
                else:
                    pv_today_hist[m] = 1.0

        def mock_minute_data_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True):
            if key == "pv_today":
                return pv_today_hist
            return {}

        base.minute_data_import_export = mock_minute_data_import_export
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        # Forecast: 3 kW constant (above historical and above max_kwh)
        total_minutes = 4 * 24 * 60
        pv_forecast_minute = {m: 3.0 / 60 for m in range(total_minutes)}  # kWh per minute
        pv_forecast_minute10 = {m: 2.0 / 60 for m in range(total_minutes)}

        # Build forecast data entries — one per plan_interval over 1 day
        from datetime import timedelta
        import pytz

        midnight = base.midnight_utc.replace(tzinfo=pytz.utc)
        pv_forecast_data = []
        for slot in range(0, 24 * 60, plan_interval):
            ts = midnight + timedelta(minutes=slot)
            pv_forecast_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": 3.0 * plan_interval / 60})

        max_kwh = 2.0  # panel peak output cap in kW
        adj_minute, adj_minute10, adj_data = solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=max_kwh, forecast_days=solar.forecast_days)

        # capped_data = min(max(max_pv_power_hist, max_pv_power_forecast), max_kwh) * plan_interval / 60
        # max_pv_power_hist ≈ 1 kW (per minute), max_pv_power_forecast ≈ 3/60 kW per minute
        # The cap applied per-slot is min(max_kwh, max_hist_or_forecast) / 60 * plan_interval
        expected_cap = max_kwh / 60 * plan_interval  # max_kwh limits here

        for entry in adj_data:
            cl = entry.get("pv_estimateCL", None)
            if cl is not None and cl > expected_cap * 1.01:  # 1% tolerance
                print("ERROR: pv_estimateCL {} exceeds expected cap {}".format(cl, expected_cap))
                failed = True
                break

    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_no_history_not_zeroed(my_predbat):
    """
    Regression test: when there is no valid historical data (e.g. all days excluded as
    "down days") both max_pv_power_hist and max_pv_power_forecast are 0. The capped_data
    clamp must NOT then zero out the calibrated/10/90 forecast - it should fall back to
    the inverter rating (max_kwh) cap instead. Previously capped_data became 0 and every
    pv_estimateCL / pv_estimate10 / pv_estimate90 was clamped to 0, so the published PV
    forecast sensors all reported 0 kWh despite a valid raw forecast.
    """
    print("  - test_pv_calibration_no_history_not_zeroed")
    failed = False

    test_api = create_test_solar_api()
    try:
        solar = test_api.solar
        base = test_api.mock_base
        plan_interval = base.plan_interval_minutes  # 5

        # No historical actual production and no forecast history at all → no valid days,
        # so max_pv_power_hist = max_pv_power_forecast = 0 and calibration is disabled.
        def mock_minute_data_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True):
            return {}

        base.minute_data_import_export = mock_minute_data_import_export
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        # Future forecast: 1 kW constant
        total_minutes = 4 * 24 * 60
        pv_forecast_minute = {m: 1.0 / 60 for m in range(total_minutes)}  # kWh per minute
        pv_forecast_minute10 = {m: 0.7 / 60 for m in range(total_minutes)}

        from datetime import timedelta
        import pytz

        midnight = base.midnight_utc.replace(tzinfo=pytz.utc)
        pv_forecast_data = []
        for slot in range(0, 24 * 60, plan_interval):
            ts = midnight + timedelta(minutes=slot)
            pv_forecast_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": 1.0 * plan_interval / 60})

        max_kwh = 3.0  # inverter rating - the cap should fall back to this
        solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=True, divide_by=1.0, max_kwh=max_kwh, forecast_days=solar.forecast_days)

        # At least one calibrated value should be non-zero where the input forecast was non-zero.
        any_nonzero_cl = any(entry.get("pv_estimateCL", 0) > 0 for entry in pv_forecast_data)
        any_nonzero_10 = any(entry.get("pv_estimate10", 0) > 0 for entry in pv_forecast_data)
        any_nonzero_90 = any(entry.get("pv_estimate90", 0) > 0 for entry in pv_forecast_data)
        if not any_nonzero_cl:
            print("ERROR: all pv_estimateCL values were zeroed despite a valid forecast and no history")
            failed = True
        if not any_nonzero_10:
            print("ERROR: all pv_estimate10 values were zeroed despite a valid forecast and no history")
            failed = True
        if not any_nonzero_90:
            print("ERROR: all pv_estimate90 values were zeroed despite a valid forecast and no history")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_synthetic_values(my_predbat):
    """
    Test pv_calibration with fully controlled synthetic data and verify all key
    output values numerically.  Two sub-cases exercise the new algorithm:

    Sub-case A – uniform 0.5x underperformance (5 days, actual=0.5 kWh, forecast=1.0 kWh):
      - total_adjustment ≈ 0.5
      - average_day_scaling ≈ 0.5
      - worst / best day scaling = 1.0 (no day-to-day variance)
      - calibrated gen-slot minute ≈ 0.5 × input
      - pv_estimate10 / pv_estimate90 ≈ pv_estimateCL (worst=best=1.0 → no spread)

    Sub-case B – variable performance (3 days: actual = 0.5, 1.0, 1.5 kWh each, forecast=1.0):
      - average_day_scaling ≈ 0.963  (weighted: (0.5×1.0 + 1.0×0.9 + 1.5×0.8) / 2.7 ≈ 0.963)
      - total_adjustment ≈ 0.963  (slot averages are recency-weighted the same way, so with a
        uniform per-day forecast this lands on the same weighted ratio as average_day_scaling)
      - worst_day_scaling ≈ 0.519  (min/weighted_avg = 0.5/0.963, above floor of 0.5)
      - best_day_scaling  ≈ 1.558  (max/weighted_avg = 1.5/0.963)
      - point estimate (pv_estimateCL) ≈ 0.963 × input (aggregate weighted ratio)
      - pv_estimate10 ≈ 0.519 × pv_estimateCL
      - pv_estimate90 ≈ 1.558 × pv_estimateCL (may be capped to capped_data)
    """
    print("  - test_pv_calibration_synthetic_values")
    failed = False

    GEN_START = 600  # 10:00 UTC in minutes since midnight
    GEN_END = 660  # 11:00 UTC
    FORECAST_KW = 1.0  # h0 forecast power (kW) during gen window
    TOL = 0.02  # 2% tolerance for floating-point comparisons

    def build_pv_today_hist(actual_per_day, minutes_now=720):
        """
        Build cumulative pv_today kWh dict keyed by minutes-ago from now (noon).
        actual_per_day: list of kWh produced in GEN_START–GEN_END; index 0 = yesterday.
        All energy is generated linearly across [GEN_START, GEN_END] minutes of each past day.
        """
        hist = {}
        for day_idx, actual_kwh in enumerate(actual_per_day):
            day = day_idx + 1
            midnight_ago = day * 1440 + minutes_now
            for step in range(0, 24 * 60, 5):
                minute_ago = midnight_ago - step
                if minute_ago < 0:
                    continue
                actual_min = step  # minute-of-day counting from midnight
                if actual_min < GEN_START:
                    cumulative = 0.0
                elif actual_min < GEN_END:
                    cumulative = actual_kwh * (actual_min - GEN_START) / (GEN_END - GEN_START)
                else:
                    cumulative = actual_kwh
                hist[minute_ago] = cumulative
        return hist

    def build_forecast_inputs(plan_interval, total_days=4):
        """
        Future forecast: FORECAST_KW kW in [GEN_START, GEN_END] for day 0 only.
        Returns (pv_forecast_minute, pv_forecast_minute10, pv_forecast_data).
        """
        total_minutes = total_days * 24 * 60
        pv_m = {}
        pv_m10 = {}
        for m in range(total_minutes):
            val = (FORECAST_KW / 60.0) if GEN_START <= m < GEN_END else 0.0
            pv_m[m] = val
            pv_m10[m] = val

        midnight = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
        pv_data = []
        for slot in range(GEN_START, GEN_END, plan_interval):
            ts = midnight + timedelta(minutes=slot)
            pv_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": FORECAST_KW * plan_interval / 60.0})
        return pv_m, pv_m10, pv_data

    def run_scenario(actual_per_day):
        test_api = create_test_solar_api()
        solar = test_api.solar
        base = test_api.mock_base
        days_back = len(actual_per_day)
        minutes_now = base.minutes_now  # 720 (noon)

        hist = build_pv_today_hist(actual_per_day)

        # Build pv_forecast minute dict directly (bypass full h0 pipeline).
        # pv_calibration maps minute N → minute_absolute = minutes_now - N.
        # For day D's gen window (GEN_START..GEN_END-1 min-of-day):
        #   minute_ago = D*1440 + (minutes_now - m_of_day)
        # We provide FORECAST_KW kW at each gen-window minute for all past days.
        pv_forecast_hist = {}
        for day_num in range(1, days_back + 1):
            for m_of_day in range(GEN_START, GEN_END):
                minutes_ago = day_num * 1440 + (minutes_now - m_of_day)
                pv_forecast_hist[minutes_ago] = float(FORECAST_KW)

        def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
            return dict(_hist) if key == "pv_today" else {}

        base.minute_data_import_export = mock_minute_import_export
        # No h0 fetch needed; history_attribute_to_minute_data is mocked below.
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        pv_m, pv_m10, pv_data = build_forecast_inputs(base.plan_interval_minutes)

        # Patch history_attribute_to_minute_data so pv_calibration receives the
        # synthetic pv_forecast dict without going through the real h0 pipeline
        # (which relies on now_utc_exact returning the mocked time).
        with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days_back)):
            adj_m, adj_m10, adj_data = solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=True, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)
        result = {
            "total_adj": solar.pv_calibration_total_adjustment,
            "avg_scaling": getattr(solar, "pv_calibration_average_scaling", None),
            "worst": solar.pv_calibration_worst_scaling,
            "best": solar.pv_calibration_best_scaling,
            "adj_m": adj_m,
            "adj_data": adj_data,
        }
        test_api.cleanup()
        return result

    # --- Sub-case A: 5 days all at 0.5x ---
    r = run_scenario([0.5, 0.5, 0.5, 0.5, 0.5])

    if abs(r["total_adj"] - 0.5) > TOL:
        print("ERROR [A]: total_adjustment should be ~0.5, got {}".format(r["total_adj"]))
        failed = True

    if r["avg_scaling"] is not None and abs(r["avg_scaling"] - 0.5) > TOL:
        print("ERROR [A]: average_day_scaling should be ~0.5, got {}".format(r["avg_scaling"]))
        failed = True

    # Uniform underperformance, zero day-to-day variance: relative worst = min/avg = 0.5/0.5 = 1.0.
    if abs(r["worst"] - 1.0) > TOL:
        print("ERROR [A]: worst_day_scaling should be 1.0 (no day variance), got {}".format(r["worst"]))
        failed = True

    # Uniform underperformance, zero day-to-day variance: relative best = max/avg = 0.5/0.5 = 1.0.
    # (worst/best are seeded from the first observed day's ratio, not a hardcoded 1.0, so a run of
    # days all on the same side of 1.0x does not spuriously widen the best/worst spread.)
    if abs(r["best"] - 1.0) > TOL:
        print("ERROR [A]: best_day_scaling should be 1.0 (no day variance), got {}".format(r["best"]))
        failed = True

    # Calibrated gen-slot minute should be approximately total_adj × raw (within 15%).
    # Small deviation arises from days_use_scaling correcting for the boundary-slot
    # artifact (slot 655 gets clamped slot_adj due to step-function PV test data).
    got = r["adj_m"].get(630, None)
    if got is None:
        print("ERROR [A]: adj_m[630] missing")
        failed = True
    else:
        pv_raw = FORECAST_KW / 60.0
        ratio = got / pv_raw if pv_raw else 0
        if abs(ratio - r["total_adj"]) > 0.15:
            print("ERROR [A]: adj_minute[630] / raw = {:.4f}, expected ~{:.4f} (total_adj ± 15%)".format(ratio, r["total_adj"]))
            failed = True

    # pv_estimate10 should use worst scaling (=1.0) → equal to pv_estimateCL.
    # pv_estimate90 should use best scaling (=1.0) → equal to pv_estimateCL too (no spread).
    for entry in r["adj_data"]:
        cl = entry.get("pv_estimateCL")
        e10 = entry.get("pv_estimate10")
        e90 = entry.get("pv_estimate90")
        if cl is not None and cl > 0:
            if e10 is not None and abs(e10 - cl) > TOL * cl:
                print("ERROR [A]: pv_estimate10 ({}) should equal pv_estimateCL ({}) when worst=1.0".format(e10, cl))
                failed = True
                break
            if e90 is not None and abs(e90 - cl) > TOL * cl:
                print("ERROR [A]: pv_estimate90 ({}) should equal pv_estimateCL ({}) when best=1.0".format(e90, cl))
                failed = True
                break

    # --- Sub-case B: 3 days at 0.5x, 1.0x, 1.5x → weighted avg=0.963, worst=0.519, best=1.558 ---
    r = run_scenario([0.5, 1.0, 1.5])

    if abs(r["total_adj"] - 0.963) > TOL:
        print("ERROR [B]: total_adjustment should be ~0.963 (recency-weighted ratio), got {}".format(r["total_adj"]))
        failed = True

    if r["avg_scaling"] is not None and abs(r["avg_scaling"] - 0.963) > TOL:
        print("ERROR [B]: average_day_scaling should be ~0.963 (weighted avg), got {}".format(r["avg_scaling"]))
        failed = True

    # worst = min_ratio / weighted_avg = 0.5 / 0.963 ≈ 0.519 (above the clamp floor of 0.5)
    if abs(r["worst"] - 0.519) > TOL:
        print("ERROR [B]: worst_day_scaling should be ~0.519 (relative to weighted avg=0.963), got {}".format(r["worst"]))
        failed = True

    # best = max_ratio / weighted_avg = 1.5 / 0.963 ≈ 1.558 (below the clamp ceiling of 1.7)
    if abs(r["best"] - 1.558) > TOL:
        print("ERROR [B]: best_day_scaling should be ~1.558 (relative to weighted avg=0.963), got {}".format(r["best"]))
        failed = True

    # Calibrated gen-slot minute should be approximately total_adj × raw (within 15%).
    got = r["adj_m"].get(630, None)
    if got is None:
        print("ERROR [B]: adj_m[630] missing")
        failed = True
    else:
        pv_raw = FORECAST_KW / 60.0
        ratio = got / pv_raw if pv_raw else 0
        if abs(ratio - r["total_adj"]) > 0.15:
            print("ERROR [B]: adj_minute[630] / raw = {:.4f}, expected ~{:.4f} (total_adj ± 15%)".format(ratio, r["total_adj"]))
            failed = True

    # pv_estimate10 = pv_estimateCL × worst (=0.5); pv_estimate90 ≥ pv_estimateCL (best=1.5)
    for entry in r["adj_data"]:
        cl = entry.get("pv_estimateCL")
        e10 = entry.get("pv_estimate10")
        e90 = entry.get("pv_estimate90")
        if cl is not None and cl > 0:
            if e10 is not None:
                expected_e10 = cl * 0.519
                if abs(e10 - expected_e10) > 0.05 * cl:
                    print("ERROR [B]: pv_estimate10 ({}) should be ~0.519×pv_estimateCL ({}) = {:.5f}".format(e10, cl, expected_e10))
                    failed = True
                    break
            if e90 is not None:
                # best=1.558, so e90 = min(cl×1.558, capped_data) ≥ cl
                if e90 < cl * (1.0 - TOL):
                    print("ERROR [B]: pv_estimate90 ({}) should be ≥ pv_estimateCL ({})".format(e90, cl))
                    failed = True
                    break
                if e90 > cl * 1.558 * (1.0 + TOL):
                    print("ERROR [B]: pv_estimate90 ({}) should be ≤ 1.558 × pv_estimateCL ({})".format(e90, cl))
                    failed = True
                    break

    return failed


def test_pv_calibration_average_day_scaling_ratio_of_sums(my_predbat):
    """
    average_day_scaling must be a weighted ratio-of-sums (sum(actual*weight) / sum(forecast*weight)),
    not a weighted average of per-day ratios (average(actual_i/forecast_i, weight_i)). The two methods
    coincide when every day's forecast total is the same size, but diverge once forecast totals vary:
    a day with a small forecast total produces a noisy/extreme ratio that the average-of-ratios method
    weights identically (recency-only) to a day representing far more actual energy, biasing the result.

    3 days (index0=day1=most recent, weight 1.0/0.9/0.8):
      day1: forecast=10.0 kWh, actual=13.0 kWh -> ratio 1.3
      day2: forecast=10.0 kWh, actual=13.0 kWh -> ratio 1.3
      day3: forecast=0.5  kWh, actual=1.5  kWh -> ratio 3.0 (tiny forecast, noisy ratio)

    Weighted average-of-ratios (old, biased method) would give:
      (1.3*1.0 + 1.3*0.9 + 3.0*0.8) / (1.0+0.9+0.8) = 4.87/2.7 ~= 1.8037
    Weighted ratio-of-sums (current method) gives:
      (13*1.0 + 13*0.9 + 1.5*0.8) / (10*1.0 + 10*0.9 + 0.5*0.8) = 25.9/19.4 ~= 1.3351
    """
    print("  - test_pv_calibration_average_day_scaling_ratio_of_sums")
    failed = False

    GEN_START = 600  # 10:00 UTC in minutes since midnight
    GEN_END = 660  # 11:00 UTC
    TOL = 0.01

    def build_pv_today_hist(actual_per_day, minutes_now):
        hist = {}
        for day_idx, actual_kwh in enumerate(actual_per_day):
            day = day_idx + 1
            midnight_ago = day * 1440 + minutes_now
            for step in range(0, 24 * 60, 5):
                minute_ago = midnight_ago - step
                if minute_ago < 0:
                    continue
                actual_min = step
                if actual_min < GEN_START:
                    cumulative = 0.0
                elif actual_min < GEN_END:
                    cumulative = actual_kwh * (actual_min - GEN_START) / (GEN_END - GEN_START)
                else:
                    cumulative = actual_kwh
                hist[minute_ago] = cumulative
        return hist

    def build_pv_forecast_hist(forecast_kwh_per_day, minutes_now):
        forecast_hist = {}
        for day_idx, forecast_kwh in enumerate(forecast_kwh_per_day):
            day = day_idx + 1
            for m_of_day in range(GEN_START, GEN_END):
                minutes_ago = day * 1440 + (minutes_now - m_of_day)
                forecast_hist[minutes_ago] = float(forecast_kwh)  # 1-hour window -> kW == kWh
        return forecast_hist

    forecast_per_day = [10.0, 10.0, 0.5]
    actual_per_day = [13.0, 13.0, 1.5]
    weights = [1.0, 0.9, 0.8]

    old_biased_average = sum((a / f) * w for a, f, w in zip(actual_per_day, forecast_per_day, weights)) / sum(weights)
    expected_average = sum(a * w for a, w in zip(actual_per_day, weights)) / sum(f * w for f, w in zip(forecast_per_day, weights))

    test_api = create_test_solar_api()
    average = None
    try:
        solar = test_api.solar
        base = test_api.mock_base
        days_back = len(actual_per_day)
        minutes_now = base.minutes_now  # 720 (noon)

        hist = build_pv_today_hist(actual_per_day, minutes_now)
        forecast_hist = build_pv_forecast_hist(forecast_per_day, minutes_now)

        def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
            return dict(_hist) if key == "pv_today" else {}

        base.minute_data_import_export = mock_minute_import_export
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        total_minutes = 4 * 24 * 60
        pv_m = {m: 0.0 for m in range(total_minutes)}
        pv_m10 = {m: 0.0 for m in range(total_minutes)}
        pv_data = []

        with patch("solcast.history_attribute_to_minute_data", return_value=(forecast_hist, days_back)):
            solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)

        average = getattr(solar, "pv_calibration_average_scaling", None)
    finally:
        test_api.cleanup()

    if average is None:
        print("ERROR: pv_calibration_average_scaling was not set")
        return True

    if abs(average - expected_average) > TOL:
        print("ERROR: average_day_scaling should be the weighted ratio-of-sums ~{:.4f}, got {}".format(expected_average, average))
        failed = True

    if abs(average - old_biased_average) < TOL:
        print("ERROR: average_day_scaling ({}) matches the old biased average-of-ratios result ({:.4f}) - ratio-of-sums fix appears reverted".format(average, old_biased_average))
        failed = True

    return failed


def test_pv_calibration_total_adjustment_recency_weighted(my_predbat):
    """
    total_adjustment (and slot_adjustment, which both drive the calibrated median forecast -
    pv_estimateCL) must weight more recent days higher, using the same recency weight as
    average_day_scaling/worst/best, rather than a flat unweighted average across the whole
    history window. Otherwise a recent change in system performance (e.g. panel cleaning,
    seasonal trend) is diluted by older, less-relevant days.

    3 days (day1=most recent, weight 1.0/0.9/0.8), uniform forecast=1.0 kWh/day:
      day1: actual=2.0 kWh (ratio 2.0)
      day2: actual=1.0 kWh (ratio 1.0)
      day3: actual=0.5 kWh (ratio 0.5)

    Flat/unweighted ratio (what the old code produced): (2.0+1.0+0.5) / (1.0+1.0+1.0) = 3.5/3 ~= 1.1667
    Recency-weighted ratio (current code): (2.0*1.0+1.0*0.9+0.5*0.8) / (1.0*1.0+1.0*0.9+1.0*0.8) = 3.3/2.7 ~= 1.2222
    """
    print("  - test_pv_calibration_total_adjustment_recency_weighted")
    failed = False

    GEN_START = 600  # 10:00 UTC in minutes since midnight
    GEN_END = 660  # 11:00 UTC
    TOL = 0.01

    def build_pv_today_hist(actual_per_day, minutes_now):
        hist = {}
        for day_idx, actual_kwh in enumerate(actual_per_day):
            day = day_idx + 1
            midnight_ago = day * 1440 + minutes_now
            for step in range(0, 24 * 60, 5):
                minute_ago = midnight_ago - step
                if minute_ago < 0:
                    continue
                actual_min = step
                if actual_min < GEN_START:
                    cumulative = 0.0
                elif actual_min < GEN_END:
                    cumulative = actual_kwh * (actual_min - GEN_START) / (GEN_END - GEN_START)
                else:
                    cumulative = actual_kwh
                hist[minute_ago] = cumulative
        return hist

    def build_pv_forecast_hist(forecast_kwh_per_day, minutes_now):
        forecast_hist = {}
        for day_idx, forecast_kwh in enumerate(forecast_kwh_per_day):
            day = day_idx + 1
            for m_of_day in range(GEN_START, GEN_END):
                minutes_ago = day * 1440 + (minutes_now - m_of_day)
                forecast_hist[minutes_ago] = float(forecast_kwh)  # 1-hour window -> kW == kWh
        return forecast_hist

    actual_per_day = [2.0, 1.0, 0.5]
    forecast_per_day = [1.0, 1.0, 1.0]
    weights = [1.0, 0.9, 0.8]

    flat_average = sum(actual_per_day) / sum(forecast_per_day)
    expected_weighted = sum(a * w for a, w in zip(actual_per_day, weights)) / sum(f * w for f, w in zip(forecast_per_day, weights))

    test_api = create_test_solar_api()
    total_adjustment = None
    try:
        solar = test_api.solar
        base = test_api.mock_base
        days_back = len(actual_per_day)
        minutes_now = base.minutes_now  # 720 (noon)

        hist = build_pv_today_hist(actual_per_day, minutes_now)
        forecast_hist = build_pv_forecast_hist(forecast_per_day, minutes_now)

        def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
            return dict(_hist) if key == "pv_today" else {}

        base.minute_data_import_export = mock_minute_import_export
        solar.get_history_wrapper = lambda entity_id, days, required=False: []

        total_minutes = 4 * 24 * 60
        pv_m = {m: 0.0 for m in range(total_minutes)}
        pv_m10 = {m: 0.0 for m in range(total_minutes)}
        pv_data = []

        with patch("solcast.history_attribute_to_minute_data", return_value=(forecast_hist, days_back)):
            solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)

        total_adjustment = solar.pv_calibration_total_adjustment
    finally:
        test_api.cleanup()

    if total_adjustment is None:
        print("ERROR: pv_calibration_total_adjustment was not set")
        return True

    if abs(total_adjustment - expected_weighted) > TOL:
        print("ERROR: total_adjustment should be the recency-weighted ratio ~{:.4f}, got {}".format(expected_weighted, total_adjustment))
        failed = True

    if abs(total_adjustment - flat_average) < TOL:
        print("ERROR: total_adjustment ({}) matches the old flat/unweighted average ({:.4f}) - recency weighting appears reverted".format(total_adjustment, flat_average))
        failed = True

    return failed


def test_pv_calibration_60min_period(my_predbat):
    """
    Test that pv_calibration correctly annotates pv_forecast_data entries when the
    forecast period (60 min, as used by Open-Meteo) is coarser than the plan interval
    (30 min by default).

    Before the fix, each 60-min entry was annotated with only the first 30-min plan
    slot's calibrated value, producing values that were approximately half the correct
    amount.  After the fix, both 30-min slots within the 60-min window are summed.
    """
    print("  - test_pv_calibration_60min_period")
    failed = False

    GEN_START = 480  # 8:00 UTC in minutes since midnight
    GEN_END = 600  # 10:00 UTC (2 hours = 2 × 60-min entries; safely before noon)
    FORECAST_KW = 2.0  # kW during generation window
    PLAN_INTERVAL = 30  # minutes
    FORECAST_PERIOD = 60  # minutes (Open-Meteo resolution)
    TOL = 0.15  # 15% tolerance (matches other calibration tests; allows for minor slot-boundary effects)

    test_api = create_test_solar_api()
    solar = test_api.solar
    base = test_api.mock_base
    base.plan_interval_minutes = PLAN_INTERVAL

    # Flat historical production matching the forecast → calibration ratio ~1.0
    # so the calibrated values should be very close to the raw forecast values.
    hist = {}
    days = 5
    minutes_now = base.minutes_now  # 720 (noon)
    for day_idx in range(days):
        day = day_idx + 1
        midnight_ago = day * 1440 + minutes_now
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago < 0:
                continue
            actual_min = step
            if actual_min < GEN_START:
                cumulative = 0.0
            elif actual_min < GEN_END:
                cumulative = FORECAST_KW * (actual_min - GEN_START) / 60.0
            else:
                cumulative = FORECAST_KW * (GEN_END - GEN_START) / 60.0
            hist[minute_ago] = cumulative

    def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
        """Mock historical PV data."""
        return dict(_hist) if key == "pv_today" else {}

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = lambda entity_id, days, required=False: []

    # Build per-minute forecast arrays (as minute_data() would produce)
    # Each minute in the gen window has FORECAST_KW / 60 kWh/min
    total_minutes = 4 * 24 * 60
    pv_m = {}
    pv_m10 = {}
    for m in range(total_minutes):
        val = (FORECAST_KW / 60.0) if GEN_START <= m < GEN_END else 0.0
        pv_m[m] = val
        pv_m10[m] = val

    # Build 60-min forecast data entries (like Open-Meteo would produce).
    # Each entry covers FORECAST_PERIOD minutes and holds FORECAST_KW * (FORECAST_PERIOD/60) kWh.
    midnight = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
    pv_data = []
    for slot in range(GEN_START, GEN_END, FORECAST_PERIOD):
        ts = midnight + timedelta(minutes=slot)
        kwh_per_entry = FORECAST_KW * FORECAST_PERIOD / 60.0
        pv_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": kwh_per_entry, "pv_estimate10": kwh_per_entry, "pv_estimate90": kwh_per_entry})

    # divide_by passed to pv_calibration = divide_by_full / period = factor.
    # For kWh entries factor = 1.0.
    divide_by_factor = 1.0

    # Build historic forecast dict (per-minute power in kW, keyed by minutes-ago)
    pv_forecast_hist = {}
    for day_num in range(1, days + 1):
        for m_of_day in range(GEN_START, GEN_END):
            minutes_ago = day_num * 1440 + (minutes_now - m_of_day)
            pv_forecast_hist[minutes_ago] = float(FORECAST_KW)

    with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days)):
        adj_m, adj_m10, adj_data = solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=True, divide_by=divide_by_factor, max_kwh=10.0, forecast_days=solar.forecast_days, period=FORECAST_PERIOD)

    # Each annotated entry should cover the full FORECAST_PERIOD minutes.
    # Expected calibrated kWh per entry ≈ FORECAST_KW * FORECAST_PERIOD / 60 = 2.0 kWh.
    # With the pre-fix bug, only the first 30-min plan slot was summed, giving ~1.0 kWh (half).
    expected_kwh = FORECAST_KW * FORECAST_PERIOD / 60.0
    half_expected = expected_kwh / 2.0

    entries_validated = 0
    for entry in adj_data:
        cl = entry.get("pv_estimateCL")
        e10 = entry.get("pv_estimate10")
        e90 = entry.get("pv_estimate90")

        if cl is None or cl == 0:
            continue

        entries_validated += 1

        # The calibrated value must be close to the full 60-min kWh (not the buggy half-period value).
        if abs(cl - expected_kwh) > TOL * expected_kwh:
            print("ERROR: pv_estimateCL ({:.4f}) should be ~{:.4f} (full 60-min period kWh); half-period bug would give ~{:.4f}".format(cl, expected_kwh, half_expected))
            failed = True
            break

        if e10 is not None and e10 > 0:
            # e10 is worst-day scaling × CL; must be > 0 and plausibly related to CL
            if e10 > cl * 2.0:
                print("ERROR: pv_estimate10 ({:.4f}) is unexpectedly much larger than pv_estimateCL ({:.4f})".format(e10, cl))
                failed = True
                break

        if e90 is not None and e90 < cl * (1.0 - TOL):
            print("ERROR: pv_estimate90 ({:.4f}) should be >= pv_estimateCL ({:.4f})".format(e90, cl))
            failed = True
            break

    if entries_validated == 0:
        print("ERROR: pv_calibration() annotated no entries with pv_estimateCL; annotation step may have regressed")
        failed = True

    test_api.cleanup()
    return failed


def test_pv_calibration_15min_period(my_predbat):
    """
    Test that pv_calibration correctly annotates pv_forecast_data entries when the
    forecast period (15 min) is finer than the plan interval (30 min by default).

    Each 15-min entry covers half a 30-min plan slot, so slots_per_period=1 and only
    the single plan slot that starts at the entry's timestamp is used.  The annotated
    pv_estimateCL should therefore be close to FORECAST_KW * 15/60 kWh (not double).
    """
    print("  - test_pv_calibration_15min_period")
    failed = False

    GEN_START = 480  # 8:00 UTC in minutes since midnight
    GEN_END = 600  # 10:00 UTC (2 hours = 8 × 15-min entries)
    FORECAST_KW = 2.0  # kW during generation window
    PLAN_INTERVAL = 30  # minutes (production default)
    FORECAST_PERIOD = 15  # minutes (forecast.solar / fine-resolution Solcast)
    TOL = 0.15  # 15% tolerance

    test_api = create_test_solar_api()
    solar = test_api.solar
    base = test_api.mock_base
    base.plan_interval_minutes = PLAN_INTERVAL

    # Flat historical production matching the forecast → calibration ratio ~1.0
    hist = {}
    days = 5
    minutes_now = base.minutes_now  # 720 (noon)
    for day_idx in range(days):
        day = day_idx + 1
        midnight_ago = day * 1440 + minutes_now
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago < 0:
                continue
            actual_min = step
            if actual_min < GEN_START:
                cumulative = 0.0
            elif actual_min < GEN_END:
                cumulative = FORECAST_KW * (actual_min - GEN_START) / 60.0
            else:
                cumulative = FORECAST_KW * (GEN_END - GEN_START) / 60.0
            hist[minute_ago] = cumulative

    def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
        """Mock historical PV data."""
        return dict(_hist) if key == "pv_today" else {}

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = lambda entity_id, days, required=False: []

    # Build per-minute forecast arrays
    total_minutes = 4 * 24 * 60
    pv_m = {}
    pv_m10 = {}
    for m in range(total_minutes):
        val = (FORECAST_KW / 60.0) if GEN_START <= m < GEN_END else 0.0
        pv_m[m] = val
        pv_m10[m] = val

    # Build 15-min forecast data entries.
    # Each entry covers FORECAST_PERIOD minutes and holds FORECAST_KW * (FORECAST_PERIOD/60) kWh.
    midnight = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
    pv_data = []
    for slot in range(GEN_START, GEN_END, FORECAST_PERIOD):
        ts = midnight + timedelta(minutes=slot)
        kwh_per_entry = FORECAST_KW * FORECAST_PERIOD / 60.0
        pv_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": kwh_per_entry, "pv_estimate10": kwh_per_entry, "pv_estimate90": kwh_per_entry})

    divide_by_factor = 1.0

    pv_forecast_hist = {}
    for day_num in range(1, days + 1):
        for m_of_day in range(GEN_START, GEN_END):
            minutes_ago = day_num * 1440 + (minutes_now - m_of_day)
            pv_forecast_hist[minutes_ago] = float(FORECAST_KW)

    with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days)):
        adj_m, adj_m10, adj_data = solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=True, divide_by=divide_by_factor, max_kwh=10.0, forecast_days=solar.forecast_days, period=FORECAST_PERIOD)

    # Each 15-min entry should be annotated with the single 30-min plan slot that
    # starts at the entry timestamp.  slots_per_period=max(1,round(15/30))=1, so
    # the value should be one plan-slot's worth of calibrated kWh ≈ FORECAST_KW*30/60=1.0.
    # (The plan slot is 30 min wide; each 15-min entry maps to one such slot.)
    expected_kwh_per_slot = FORECAST_KW * PLAN_INTERVAL / 60.0  # 1.0 kWh

    entries_validated = 0
    for entry in adj_data:
        cl = entry.get("pv_estimateCL")
        e10 = entry.get("pv_estimate10")
        e90 = entry.get("pv_estimate90")

        if cl is None or cl == 0:
            continue

        entries_validated += 1

        # pv_estimateCL must not be doubled (which would happen if slots_per_period were
        # incorrectly set to 2 for a 15-min forecast with a 30-min plan interval).
        double_expected = expected_kwh_per_slot * 2.0
        if cl > double_expected * (1.0 + TOL):
            print("ERROR: pv_estimateCL ({:.4f}) is larger than double the expected slot kWh ({:.4f}); slots may be over-accumulated".format(cl, expected_kwh_per_slot))
            failed = True
            break

        if cl < expected_kwh_per_slot * (1.0 - TOL):
            print("ERROR: pv_estimateCL ({:.4f}) is less than expected slot kWh ({:.4f})".format(cl, expected_kwh_per_slot))
            failed = True
            break

        if e10 is not None and e10 > cl * 2.0:
            print("ERROR: pv_estimate10 ({:.4f}) is unexpectedly much larger than pv_estimateCL ({:.4f})".format(e10, cl))
            failed = True
            break

        if e90 is not None and e90 < cl * (1.0 - TOL):
            print("ERROR: pv_estimate90 ({:.4f}) should be >= pv_estimateCL ({:.4f})".format(e90, cl))
            failed = True
            break

    if entries_validated == 0:
        print("ERROR: pv_calibration() annotated no entries with pv_estimateCL; annotation step may have regressed")
        failed = True

    test_api.cleanup()
    return failed


# ============================================================================
# azimuth_zero_south tests
# ============================================================================


def test_download_forecast_solar_data_azimuth_zero_south(my_predbat):
    """
    When azimuth_zero_south is True the azimuth is passed to forecast.solar
    as-is (0=South convention); when False (default) convert_azimuth is applied first.
    """
    print("  - test_download_forecast_solar_data_azimuth_zero_south")
    failed = False

    forecast_response = {
        "result": {"watts": {"2025-06-15T12:00:00+0000": 500}},
        "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
    }

    def create_mock_session(*args, **kwargs):
        return test_api.mock_aiohttp_session()

    # --- Case 1: azimuth_zero_south=True, azimuth=0 (South in forecast.solar convention) ---
    # URL path should contain /0/ for azimuth
    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0, "efficiency": 1.0, "azimuth_zero_south": True}]
        test_api.set_mock_response("forecast.solar", forecast_response, 200)
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.download_forecast_solar_data())
        solar_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if not solar_calls:
            print("ERROR: No forecast.solar API call made (azimuth_zero_south=True)")
            failed = True
        elif "/0/" not in solar_calls[0]["url"]:
            print(f"ERROR: Expected /0/ in URL with azimuth_zero_south=True, got: {solar_calls[0]['url']}")
            failed = True
    finally:
        test_api.cleanup()

    # --- Case 2: azimuth_zero_south=False (default), azimuth=0 (North in Predbat convention) ---
    # convert_azimuth(0) → 180; URL path should contain /180/
    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0, "efficiency": 1.0}]
        test_api.set_mock_response("forecast.solar", forecast_response, 200)
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.download_forecast_solar_data())
        solar_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if not solar_calls:
            print("ERROR: No forecast.solar API call made (azimuth_zero_south=False)")
            failed = True
        elif "/180/" not in solar_calls[0]["url"]:
            print(f"ERROR: Expected /180/ in URL without azimuth_zero_south, got: {solar_calls[0]['url']}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_skips_system_down_days(my_predbat):
    """
    Test that pv_calibration ignores days where actual production is less than 10% of
    forecast.  When a system is offline (HA restart, inverter fault, etc.) no production
    data is stored, so the cumulative sensor stays at zero for that day.  Without the
    guard the near-zero actual would produce a very small scaling factor and incorrectly
    drag the average downward, causing the forecast to be under-estimated.

    Scenario: 5 days of history.
      - Days 2-5: actual = forecast = 1.0 kWh  → scaling factor = 1.0 each
      - Day 1 (yesterday): actual = 0.03 kWh, forecast = 1.0 kWh (3% → should be skipped)

    Expected outcome:
      - average_day_scaling ≈ 1.0 (only the 4 good days are used)
      - total_adjustment ≈ 1.0
    If the bad day were included, average_day_scaling would be pulled well below 1.0.
    """
    print("  - test_pv_calibration_skips_system_down_days")
    failed = False

    GEN_START = 600  # 10:00 UTC
    GEN_END = 660  # 11:00 UTC
    FORECAST_KW = 1.0
    TOL = 0.10  # 10% tolerance

    minutes_now = 720  # noon

    def build_cumulative_hist(actual_per_day):
        """Build cumulative pv_today kWh dict keyed by minutes-ago from now."""
        hist = {}
        for day_idx, actual_kwh in enumerate(actual_per_day):
            day = day_idx + 1
            midnight_ago = day * 1440 + minutes_now
            for step in range(0, 24 * 60, 5):
                minute_ago = midnight_ago - step
                if minute_ago < 0:
                    continue
                actual_min = step
                if actual_min < GEN_START:
                    cumulative = 0.0
                elif actual_min < GEN_END:
                    cumulative = actual_kwh * (actual_min - GEN_START) / (GEN_END - GEN_START)
                else:
                    cumulative = actual_kwh
                hist[minute_ago] = cumulative
        return hist

    # Day 1 (yesterday) was down – only 3% of expected production recorded
    actual_per_day = [0.03, 1.0, 1.0, 1.0, 1.0]
    hist = build_cumulative_hist(actual_per_day)

    test_api = create_test_solar_api()
    solar = test_api.solar
    base = test_api.mock_base
    base.plan_interval_minutes = 5

    def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
        """Return synthetic cumulative PV history."""
        return dict(_hist) if key == "pv_today" else {}

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = lambda entity_id, days, required=False: []

    # Future forecast: FORECAST_KW in gen window for day 0
    total_minutes = 4 * 24 * 60
    pv_m = {m: (FORECAST_KW / 60.0 if GEN_START <= m < GEN_END else 0.0) for m in range(total_minutes)}
    pv_m10 = dict(pv_m)

    midnight = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
    pv_data = []
    for slot in range(GEN_START, GEN_END, base.plan_interval_minutes):
        ts = midnight + timedelta(minutes=slot)
        pv_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": FORECAST_KW * base.plan_interval_minutes / 60.0})

    # Past forecast history: FORECAST_KW for every day in the gen window
    days_back = len(actual_per_day)
    pv_forecast_hist = {}
    for day_num in range(1, days_back + 1):
        for m_of_day in range(GEN_START, GEN_END):
            minutes_ago = day_num * 1440 + (minutes_now - m_of_day)
            pv_forecast_hist[minutes_ago] = float(FORECAST_KW)

    try:
        with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days_back)):
            solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)

        avg = getattr(solar, "pv_calibration_average_scaling", None)
        total_adj = solar.pv_calibration_total_adjustment

        if avg is None:
            print("ERROR: pv_calibration_average_scaling was not set")
            failed = True
        elif abs(avg - 1.0) > TOL:
            print("ERROR: average_day_scaling should be ~1.0 (bad day skipped), got {:.4f}".format(avg))
            failed = True

        if abs(total_adj - 1.0) > TOL:
            print("ERROR: total_adjustment should be ~1.0 (slot averages recomputed without bad day), got {:.4f}".format(total_adj))
            failed = True

        # Sanity check: if the bad day were NOT skipped, average_day_scaling would be
        # approximately (0.03×1.0 + 1.0×0.9 + 1.0×0.8 + 1.0×0.7 + 1.0×0.6) / (1.0+0.9+0.8+0.7+0.6) ≈ 0.61
        # and total_adjustment would be approximately (0.03+4×1.0)/5 = 0.806
        # both clearly below 1.0, so our tolerance of 0.10 correctly distinguishes pass from fail.

    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_all_days_down(my_predbat):
    """
    Edge case: all historical days have near-zero actual production (system was down the
    whole time).  calibration should not crash and should fall back to adjustment = 1.0.
    """
    print("  - test_pv_calibration_all_days_down")
    failed = False

    GEN_START = 600
    GEN_END = 660
    FORECAST_KW = 1.0
    minutes_now = 720

    # All 5 days at 2% of forecast — all should be skipped
    actual_per_day = [0.02, 0.02, 0.02, 0.02, 0.02]
    hist = {}
    for day_idx, actual_kwh in enumerate(actual_per_day):
        day = day_idx + 1
        midnight_ago = day * 1440 + minutes_now
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago < 0:
                continue
            actual_min = step
            cumulative = actual_kwh if actual_min >= GEN_END else (actual_kwh * max(0, actual_min - GEN_START) / (GEN_END - GEN_START) if actual_min >= GEN_START else 0.0)
            hist[minute_ago] = cumulative

    test_api = create_test_solar_api()
    solar = test_api.solar
    base = test_api.mock_base
    base.plan_interval_minutes = 5

    def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
        return dict(_hist) if key == "pv_today" else {}

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = lambda entity_id, days, required=False: []

    total_minutes = 4 * 24 * 60
    pv_m = {m: (FORECAST_KW / 60.0 if GEN_START <= m < GEN_END else 0.0) for m in range(total_minutes)}
    pv_m10 = dict(pv_m)

    midnight = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
    pv_data = [{"period_start": (midnight + timedelta(minutes=s)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": FORECAST_KW * base.plan_interval_minutes / 60.0} for s in range(GEN_START, GEN_END, base.plan_interval_minutes)]

    days_back = len(actual_per_day)
    pv_forecast_hist = {}
    for day_num in range(1, days_back + 1):
        for m_of_day in range(GEN_START, GEN_END):
            pv_forecast_hist[day_num * 1440 + (minutes_now - m_of_day)] = float(FORECAST_KW)

    try:
        with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days_back)):
            # Must not raise ZeroDivisionError or any other exception
            solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=False, divide_by=1.0, max_kwh=5.0, forecast_days=solar.forecast_days)

        total_adj = solar.pv_calibration_total_adjustment
        if abs(total_adj - 1.0) > 0.01:
            print("ERROR: total_adjustment should be 1.0 when all days skipped (no valid data), got {:.4f}".format(total_adj))
            failed = True

    except ZeroDivisionError:
        print("ERROR: pv_calibration raised ZeroDivisionError when all history days were skipped")
        failed = True
    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Main Test Runner
# ============================================================================


def run_solcast_tests(my_predbat):
    """
    Run all Solcast API tests.
    """
    print("Running Solcast API tests...")
    failed = False

    # Pure function tests
    failed |= test_convert_azimuth(my_predbat)

    # Cache tests
    failed |= test_cache_get_url_miss(my_predbat)
    failed |= test_cache_get_url_hit(my_predbat)
    failed |= test_cache_get_url_stale(my_predbat)
    failed |= test_cache_get_url_failure_with_stale_cache(my_predbat)
    failed |= test_cache_get_url_metrics_forecast_solar(my_predbat)

    # Solcast download tests
    failed |= test_download_solcast_data(my_predbat)
    failed |= test_download_solcast_data_multi_site(my_predbat)

    # Forecast.Solar download tests
    failed |= test_download_forecast_solar_data(my_predbat)
    failed |= test_download_forecast_solar_data_with_postcode(my_predbat)
    failed |= test_download_forecast_solar_data_with_postcode_lookup_failure(my_predbat)
    failed |= test_download_forecast_solar_data_personal_api(my_predbat)
    failed |= test_download_forecast_solar_data_dual_plane(my_predbat)
    failed |= test_download_forecast_solar_data_dual_plane_not_paired_different_location(my_predbat)
    failed |= test_download_forecast_solar_data_rate_limited_no_cache(my_predbat)
    failed |= test_forecast_solar_rate_limit_suppresses_fetch(my_predbat)
    failed |= test_forecast_solar_rate_limit_expires(my_predbat)
    failed |= test_download_forecast_solar_data_azimuth_zero_south(my_predbat)

    # HA sensor tests
    failed |= test_fetch_pv_datapoints_detailed_forecast(my_predbat)
    failed |= test_fetch_pv_datapoints_forecast_fallback(my_predbat)

    # Publish stats tests
    failed |= test_publish_pv_stats(my_predbat)
    failed |= test_publish_pv_stats_remaining_calculation(my_predbat)
    failed |= test_publish_pv_stats_missing_day_zero(my_predbat)

    # Pack and store tests
    failed |= test_pack_and_store_forecast(my_predbat)

    # Run function tests
    failed |= test_run_at_plan_interval(my_predbat)
    failed |= test_run_new_day_trigger(my_predbat)
    failed |= test_run_data_older_than_60_minutes(my_predbat)
    failed |= test_run_no_fetch_when_recent(my_predbat)
    failed |= test_run_first_fetch_when_no_timestamp(my_predbat)

    # Integration tests (one per mode)
    failed |= test_fetch_pv_forecast_solcast_direct(my_predbat)
    failed |= test_fetch_pv_forecast_forecast_solar(my_predbat)
    failed |= test_fetch_pv_forecast_forecast_solar_open_meteo_backup_on_failure(my_predbat)
    failed |= test_fetch_pv_forecast_forecast_solar_open_meteo_backup_not_used_on_success(my_predbat)
    failed |= test_fetch_pv_forecast_ha_sensors(my_predbat)

    # 15-minute resolution tests
    failed |= test_fetch_pv_forecast_ha_sensors_15min_kwh(my_predbat)
    failed |= test_fetch_pv_forecast_ha_sensors_15min_kw(my_predbat)
    failed |= test_publish_pv_stats_15min_resolution(my_predbat)

    # Calibration tests
    failed |= test_pv_calibration_power_conversion(my_predbat)
    failed |= test_pv_calibration_sparse_recent_history_no_crash(my_predbat)
    failed |= test_pv_calibration_capped_data_clamp(my_predbat)
    failed |= test_pv_calibration_no_history_not_zeroed(my_predbat)
    failed |= test_pv_calibration_partial_history(my_predbat)
    failed |= test_pv_calibration_synthetic_values(my_predbat)
    failed |= test_pv_calibration_average_day_scaling_ratio_of_sums(my_predbat)
    failed |= test_pv_calibration_total_adjustment_recency_weighted(my_predbat)
    failed |= test_pv_calibration_60min_period(my_predbat)
    failed |= test_pv_calibration_15min_period(my_predbat)
    failed |= test_pv_calibration_skips_system_down_days(my_predbat)
    failed |= test_pv_calibration_all_days_down(my_predbat)

    return failed
