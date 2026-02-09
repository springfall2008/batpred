# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
from datetime import datetime, timedelta
import os
import tempfile
import shutil
from octopus import OctopusAPI


def test_fetch_url_cached(my_predbat):
    """
    Wrapper to run the async test function
    """
    return asyncio.run(test_fetch_url_cached_async(my_predbat))


async def test_fetch_url_cached_async(my_predbat):
    """
    Test the fetch_url_cached function with stale-while-revalidate caching

    Tests various scenarios:
    - Fresh cache (< 30 minutes)
    - Stale cache (30-35 minutes) with stale-while-revalidate
    - Too stale cache (> 35 minutes)
    - Missing cache
    - Multiple URLs
    - Cache file structure and hash-based filenames
    - clean_url_cache removes old entries (> 24 hours)
    """
    print("**** Running fetch_url_cached tests ****")
    failed = False

    # Create temporary cache directory for testing
    temp_dir = tempfile.mkdtemp()

    # Mock data for different URLs
    mock_rate_data_var = [
        {"value_inc_vat": 15.5, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z"},
        {"value_inc_vat": 16.2, "valid_from": "2025-01-01T00:30:00Z", "valid_to": "2025-01-01T01:00:00Z"},
        {"value_inc_vat": 14.8, "valid_from": "2025-01-01T01:00:00Z", "valid_to": "2025-01-01T01:30:00Z"},
    ]

    mock_rate_data_agile = [
        {"value_inc_vat": 12.3, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z"},
        {"value_inc_vat": 13.1, "valid_from": "2025-01-01T00:30:00Z", "valid_to": "2025-01-01T01:00:00Z"},
        {"value_inc_vat": 11.9, "valid_from": "2025-01-01T01:00:00Z", "valid_to": "2025-01-01T01:30:00Z"},
    ]

    # Mock function to replace async_download_octopus_url
    async def mock_download(url):
        """Mock download function that returns test data based on URL"""
        if "VAR-22-11-01" in url:
            return mock_rate_data_var
        elif "AGILE-FLEX-22-11-25" in url:
            return mock_rate_data_agile
        elif "INVALID" in url:
            return None
        else:
            # Return generic data for other URLs
            return [{"value_inc_vat": 10.0, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z"}]

    try:
        # Create OctopusAPI instance with temporary cache directory
        api = OctopusAPI(my_predbat, key="test_key", account_id="test_account", automatic=False)
        api.urls_cache_path = temp_dir

        # Replace async_download_octopus_url with mock
        original_download = api.async_download_octopus_url
        api.async_download_octopus_url = mock_download

        # Test URL for VAR-22-11-01 tariff (electricity rates)
        test_url = "https://api.octopus.energy/v1/products/VAR-22-11-01/electricity-tariffs/E-2R-VAR-22-11-01-A/standard-unit-rates/"

        # Test 1: Fetch URL with empty cache (should download and cache)
        print("*** Test 1: Fetch URL with empty cache")
        data1 = await api.fetch_url_cached(test_url)

        if not data1:
            print("ERROR Test 1: Failed to fetch data from URL")
            failed = True
        elif not isinstance(data1, list):
            print("ERROR Test 1: Expected list, got {}".format(type(data1)))
            failed = True
        elif len(data1) == 0:
            print("ERROR Test 1: Expected non-empty data")
            failed = True
        elif len(data1) != 3:
            print("ERROR Test 1: Expected 3 rate points, got {}".format(len(data1)))
            failed = True
        else:
            print("Successfully fetched {} rate points".format(len(data1)))

        # Verify cache file was created
        import hashlib

        url_hash = hashlib.sha256(test_url.encode()).hexdigest()[:16]
        cache_file = os.path.join(temp_dir, f"{url_hash}.yaml")

        if not os.path.exists(cache_file):
            print("ERROR Test 1: Cache file not created at {}".format(cache_file))
            failed = True

        # Test 2: Fetch same URL again (should return cached data without download)
        print("*** Test 2: Fetch from fresh cache (< 30 min)")
        data2 = await api.fetch_url_cached(test_url)

        if not data2:
            print("ERROR Test 2: Failed to fetch from cache")
            failed = True
        elif data1 != data2:
            print("ERROR Test 2: Cached data differs from original")
            failed = True
        else:
            print("Successfully retrieved {} rate points from cache".format(len(data2)))

        # Test 3: Test stale cache (30-35 minutes old)
        print("*** Test 3: Simulate stale cache (30-35 min)")
        # Manually modify cache timestamp to be 32 minutes old
        cached_data = api.load_url_from_cache(test_url)
        if cached_data:
            old_stamp = datetime.now() - timedelta(minutes=32)
            cached_data["stamp"] = old_stamp
            api.save_url_to_cache(test_url, cached_data)

            # Fetch should return stale data immediately
            data3 = await api.fetch_url_cached(test_url)

            if not data3:
                print("ERROR Test 3: Failed to fetch stale data")
                failed = True
            elif len(data3) == 0:
                print("ERROR Test 3: Stale data is empty")
                failed = True
            else:
                print("Successfully retrieved stale data with {} rate points".format(len(data3)))

        # Test 4: Test too stale cache (> 35 minutes old)
        print("*** Test 4: Simulate too stale cache (> 35 min)")
        # Manually modify cache timestamp to be 40 minutes old
        cached_data = api.load_url_from_cache(test_url)
        if cached_data:
            old_stamp = datetime.now() - timedelta(minutes=40)
            cached_data["stamp"] = old_stamp
            api.save_url_to_cache(test_url, cached_data)

            # Fetch should download fresh data
            data4 = await api.fetch_url_cached(test_url)

            if not data4:
                print("ERROR Test 4: Failed to fetch fresh data after stale cache")
                failed = True
            else:
                print("Successfully refreshed data with {} rate points".format(len(data4)))

                # Verify cache was updated with fresh timestamp
                updated_cache = api.load_url_from_cache(test_url)
                if updated_cache:
                    age = datetime.now() - updated_cache["stamp"]
                    if age.seconds > 10:
                        print("ERROR Test 4: Cache timestamp not updated (age: {} seconds)".format(age.seconds))
                        failed = True

        # Test 5: Multiple different URLs
        print("*** Test 5: Multiple URLs with different caches")
        url2 = "https://api.octopus.energy/v1/products/AGILE-FLEX-22-11-25/electricity-tariffs/E-1R-AGILE-FLEX-22-11-25-A/standard-unit-rates/"

        data5 = await api.fetch_url_cached(url2)

        if not data5:
            print("ERROR Test 5: Failed to fetch second URL")
            failed = True
        else:
            print("Successfully fetched {} rate points from second URL".format(len(data5)))

            # Verify both caches exist
            url2_hash = hashlib.sha256(url2.encode()).hexdigest()[:16]
            cache_file2 = os.path.join(temp_dir, f"{url2_hash}.yaml")

            if not os.path.exists(cache_file):
                print("ERROR Test 5: First cache file disappeared")
                failed = True
            if not os.path.exists(cache_file2):
                print("ERROR Test 5: Second cache file not created")
                failed = True

        # Test 6: Invalid URL (should handle gracefully)
        print("*** Test 6: Invalid URL")
        invalid_url = "https://api.octopus.energy/v1/products/INVALID/electricity-tariffs/INVALID/standard-unit-rates/"

        data6 = await api.fetch_url_cached(invalid_url)

        if data6 is not None:
            print("ERROR Test 6: Expected None for invalid URL, got {}".format(type(data6)))
            failed = True
        else:
            print("Successfully handled invalid URL")

        # Test 7: Verify cache data structure
        print("*** Test 7: Verify cache data structure")
        cached = api.load_url_from_cache(test_url)

        if not cached:
            print("ERROR Test 7: Failed to load cache")
            failed = True
        elif "stamp" not in cached:
            print("ERROR Test 7: Cache missing 'stamp' field")
            failed = True
        elif "data" not in cached:
            print("ERROR Test 7: Cache missing 'data' field")
            failed = True
        elif not isinstance(cached["stamp"], datetime):
            print("ERROR Test 7: Stamp should be datetime, got {}".format(type(cached["stamp"])))
            failed = True
        elif not isinstance(cached["data"], list):
            print("ERROR Test 7: Data should be list, got {}".format(type(cached["data"])))
            failed = True
        else:
            print("Cache structure validated")

        # Test 8: Verify hash-based filenames
        print("*** Test 8: Verify hash-based filenames")
        files = os.listdir(temp_dir)
        yaml_files = [f for f in files if f.endswith(".yaml")]

        if len(yaml_files) < 2:
            print("ERROR Test 8: Expected at least 2 cache files, found {}".format(len(yaml_files)))
            failed = True
        else:
            print("Found {} cache files with hash-based names".format(len(yaml_files)))

            # Verify filename format (16-char hash + .yaml)
            for filename in yaml_files:
                if len(filename) != 21:  # 16 chars + '.yaml' (5 chars)
                    print("ERROR Test 8: Invalid filename length: {}".format(filename))
                    failed = True

        # Test 9: Test clean_url_cache removes old entries
        print("*** Test 9: Test clean_url_cache removes old entries")

        # Create some test URLs with different ages
        old_url = "https://api.octopus.energy/v1/products/OLD-TARIFF/electricity-tariffs/E-1R-OLD-TARIFF-A/standard-unit-rates/"
        recent_url = "https://api.octopus.energy/v1/products/RECENT-TARIFF/electricity-tariffs/E-1R-RECENT-TARIFF-A/standard-unit-rates/"

        # Save old cache entry (25 hours old - should be cleaned)
        old_data = {"stamp": datetime.now() - timedelta(hours=25), "data": [{"value_inc_vat": 99.9, "valid_from": "2025-01-01T00:00:00Z"}]}
        api.save_url_to_cache(old_url, old_data)

        # Save recent cache entry (2 hours old - should be kept)
        recent_data = {"stamp": datetime.now() - timedelta(hours=2), "data": [{"value_inc_vat": 88.8, "valid_from": "2025-01-01T00:00:00Z"}]}
        api.save_url_to_cache(recent_url, recent_data)

        # Count files before cleaning
        files_before = [f for f in os.listdir(temp_dir) if f.endswith(".yaml")]
        cache_count_before = len(files_before)

        # Run clean_url_cache
        await api.clean_url_cache()

        # Count files after cleaning
        files_after = [f for f in os.listdir(temp_dir) if f.endswith(".yaml")]
        cache_count_after = len(files_after)

        # Verify old cache was removed
        old_cache = api.load_url_from_cache(old_url)
        recent_cache = api.load_url_from_cache(recent_url)

        if old_cache is not None:
            print("ERROR Test 9: Old cache (25 hours) should have been cleaned but still exists")
            failed = True

        if recent_cache is None:
            print("ERROR Test 9: Recent cache (2 hours) should have been kept but was cleaned")
            failed = True

        if cache_count_after >= cache_count_before:
            print("ERROR Test 9: Expected fewer cache files after cleaning, before: {}, after: {}".format(cache_count_before, cache_count_after))
            failed = True
        else:
            cleaned_count = cache_count_before - cache_count_after
            print("Successfully cleaned {} old cache file(s), {} files remaining".format(cleaned_count, cache_count_after))

    finally:
        # Restore original function
        if "api" in locals() and "original_download" in locals():
            api.async_download_octopus_url = original_download

        # Clean up temporary directory
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

    if not failed:
        print("**** All fetch_url_cached tests PASSED ****")
    else:
        print("**** Some fetch_url_cached tests FAILED ****")

    return failed
