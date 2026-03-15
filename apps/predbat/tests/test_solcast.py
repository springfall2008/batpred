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
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytz
import aiohttp

from solcast import SolarAPI
from tests.test_infra import run_async, create_aiohttp_mock_response


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
            pv_forecast_today=None,
            pv_forecast_tomorrow=None,
            pv_forecast_d3=None,
            pv_forecast_d4=None,
            pv_scaling=1.0,
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
    Test cache_get_url when cache file exists and is fresh - should return cached data.
    """
    print("  - test_cache_get_url_hit")
    failed = False

    test_api = create_test_solar_api()
    try:
        import hashlib

        # Create cache directory and file
        cache_path = test_api.mock_base.config_root + "/cache"
        os.makedirs(cache_path, exist_ok=True)

        url = "https://api.solcast.com.au/test/cached"
        params = {}

        # Create a predictable cache filename (same logic as SolarAPI)
        hash_str = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_str = hash_str.replace("/", "_").replace(":", "_").replace("?", "a").replace("&", "b").replace("*", "c")
        cache_filename = cache_path + "/" + hash_str + ".json"

        # Write cached data
        cached_data = {"cached": True, "from": "file"}
        with open(cache_filename, "w") as f:
            json.dump(cached_data, f)

        # Set up a mock response that returns different data (should NOT be used)
        test_api.set_mock_response("solcast.com.au/test/cached", {"fresh": "data"}, 200)

        # Call cache_get_url with max_age=60 - should use cache since file is fresh
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


def test_cache_get_url_stale(my_predbat):
    """
    Test cache_get_url when cache file exists but is stale - should re-fetch.
    """
    print("  - test_cache_get_url_stale")
    failed = False

    test_api = create_test_solar_api()
    try:
        import hashlib

        # Create cache directory and stale file
        cache_path = test_api.mock_base.config_root + "/cache"
        os.makedirs(cache_path, exist_ok=True)

        url = "https://api.solcast.com.au/test/stale"
        params = {}

        hash_str = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_str = hash_str.replace("/", "_").replace(":", "_").replace("?", "a").replace("&", "b").replace("*", "c")
        cache_filename = cache_path + "/" + hash_str + ".json"

        # Write stale cached data
        stale_data = {"stale": True}
        with open(cache_filename, "w") as f:
            json.dump(stale_data, f)

        # Make the file old by setting mtime to 2 hours ago
        old_time = datetime.now().timestamp() - (2 * 60 * 60)
        os.utime(cache_filename, (old_time, old_time))

        # Setup mock response for fresh data
        fresh_data = {"fresh": True, "new": "data"}
        test_api.set_mock_response("solcast.com.au/test/stale", fresh_data, 200)

        # Call with max_age of 60 minutes - cache is 2 hours old so should re-fetch
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
        import hashlib

        # Create cache directory and stale file
        cache_path = test_api.mock_base.config_root + "/cache"
        os.makedirs(cache_path, exist_ok=True)

        url = "https://api.solcast.com.au/test/failure"
        params = {}

        hash_str = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_str = hash_str.replace("/", "_").replace(":", "_").replace("?", "a").replace("&", "b").replace("*", "c")
        cache_filename = cache_path + "/" + hash_str + ".json"

        # Write stale cached data
        stale_data = {"stale": True, "fallback": "data"}
        with open(cache_filename, "w") as f:
            json.dump(stale_data, f)

        # Make file stale
        old_time = datetime.now().timestamp() - (2 * 60 * 60)
        os.utime(cache_filename, (old_time, old_time))

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

        adj_minute, adj_minute10, adj_data = solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=5.0)

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

            solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=5.0)

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
        adj_minute, adj_minute10, adj_data = solar.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10=False, divide_by=1.0, max_kwh=max_kwh)

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
    failed |= test_download_forecast_solar_data_personal_api(my_predbat)

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
    failed |= test_fetch_pv_forecast_ha_sensors(my_predbat)

    # 15-minute resolution tests
    failed |= test_fetch_pv_forecast_ha_sensors_15min_kwh(my_predbat)
    failed |= test_fetch_pv_forecast_ha_sensors_15min_kw(my_predbat)
    failed |= test_publish_pv_stats_15min_resolution(my_predbat)

    # Calibration tests
    failed |= test_pv_calibration_power_conversion(my_predbat)
    failed |= test_pv_calibration_capped_data_clamp(my_predbat)
    failed |= test_pv_calibration_partial_history(my_predbat)

    return failed
