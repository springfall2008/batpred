# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
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
from datetime import datetime
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
        """Mock log - silent"""
        pass

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Track dashboard_item calls"""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes}

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
        mock_data = {"result": {"watt_hours_period": {}}, "message": {"info": {"time": "2025-06-15T12:00:00+0000"}}}
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
                "watt_hours_period": {
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
                "watt_hours_period": {
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

        forecast_response = {"result": {"watt_hours_period": {"2025-06-15T12:00:00+0000": 500}}, "message": {"info": {"time": "2025-06-15T11:30:00+0000"}}}
        test_api.set_mock_response("forecast.solar", forecast_response, 200)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result, max_kwh = run_async(test_api.solar.download_forecast_solar_data())

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
                "watt_hours_period": {
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

    # Pack and store tests
    failed |= test_pack_and_store_forecast(my_predbat)

    # Integration tests (one per mode)
    failed |= test_fetch_pv_forecast_solcast_direct(my_predbat)
    failed |= test_fetch_pv_forecast_forecast_solar(my_predbat)
    failed |= test_fetch_pv_forecast_ha_sensors(my_predbat)

    return failed
