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
import hashlib
import os
import tempfile
import shutil
from octopus import OctopusAPI
from storage import StorageComponent, StorageLocalFiles


def test_fetch_url_cached(my_predbat):
    """
    Wrapper to run the async test function
    """
    return asyncio.run(test_fetch_url_cached_async(my_predbat))


class _MockComponents:
    """Minimal components mock that returns a pre-configured storage component."""

    def __init__(self, storage):
        """Initialise with a storage instance."""
        self._storage = storage

    def get_component(self, name):
        """Return the mocked storage for 'storage', None for others."""
        if name == "storage":
            return self._storage
        return None


def _attach_storage(api, temp_dir):
    """Attach a real StorageComponent backed by a temp directory to the api instance."""
    storage = StorageComponent(api.base)
    storage.backend = StorageLocalFiles(temp_dir, api.base.log)
    api.base.components = _MockComponents(storage)
    return storage


async def test_fetch_url_cached_async(my_predbat):
    """
    Test the fetch_url_cached function with storage-component-backed caching.

    Tests:
    - Missing/first call fetches data and returns it
    - Fresh hit: a second call within the fresh window does not re-download
    - Multiple distinct URLs cache independently
    - Invalid URL (download returns None) is handled gracefully
    - Data persists in the attached storage component
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

    # Mock function to replace async_download_octopus_url, with a call counter
    download_calls = {"count": 0}

    async def mock_download(url, **kwargs):
        """Mock download function that returns test data based on URL"""
        download_calls["count"] += 1
        if "VAR-22-11-01" in url:
            return mock_rate_data_var
        elif "AGILE-FLEX-22-11-25" in url:
            return mock_rate_data_agile
        elif "INVALID" in url:
            return None
        elif "empty-error" in url:
            # Mirrors async_download_octopus_url's real error return ({} on HTTP/parse errors)
            return {}
        else:
            # Return generic data for other URLs
            return [{"value_inc_vat": 10.0, "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-01-01T00:30:00Z"}]

    try:
        # Create OctopusAPI instance with a real storage component backed by a temp dir
        api = OctopusAPI(my_predbat, key="test_key", account_id="test_account", automatic=False)
        storage = _attach_storage(api, temp_dir)

        # Replace async_download_octopus_url with mock
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
        elif len(data1) != 3:
            print("ERROR Test 1: Expected 3 rate points, got {}".format(len(data1)))
            failed = True
        elif download_calls["count"] != 1:
            print("ERROR Test 1: Expected exactly 1 download, got {}".format(download_calls["count"]))
            failed = True
        else:
            print("Successfully fetched {} rate points (1 download)".format(len(data1)))

        # Test 1b: Data persisted in the attached storage component
        url_hash = hashlib.sha256(test_url.encode()).hexdigest()[:16]
        stored = await storage.load("octopus", url_hash)
        if stored != data1:
            print("ERROR Test 1b: Stored data does not match fetched data")
            failed = True
        else:
            print("Stored data round-tripped via storage component")

        # Test 2: Fetch same URL again within the fresh window (no re-download)
        print("*** Test 2: Fetch from fresh cache (< 30 min)")
        data2 = await api.fetch_url_cached(test_url)

        if not data2:
            print("ERROR Test 2: Failed to fetch from cache")
            failed = True
        elif data1 != data2:
            print("ERROR Test 2: Cached data differs from original")
            failed = True
        elif download_calls["count"] != 1:
            print("ERROR Test 2: Fresh hit should not re-download, download count is {}".format(download_calls["count"]))
            failed = True
        else:
            print("Successfully retrieved {} rate points from cache (no re-download)".format(len(data2)))

        # Test 3: Multiple different URLs cache independently
        print("*** Test 3: Multiple URLs with different caches")
        url2 = "https://api.octopus.energy/v1/products/AGILE-FLEX-22-11-25/electricity-tariffs/E-1R-AGILE-FLEX-22-11-25-A/standard-unit-rates/"

        data3 = await api.fetch_url_cached(url2)

        if not data3:
            print("ERROR Test 3: Failed to fetch second URL")
            failed = True
        elif data3 == data1:
            print("ERROR Test 3: Second URL returned the first URL's data")
            failed = True
        elif download_calls["count"] != 2:
            print("ERROR Test 3: Expected 2 downloads after second distinct URL, got {}".format(download_calls["count"]))
            failed = True
        else:
            # First URL still served from cache (no further download)
            data1_again = await api.fetch_url_cached(test_url)
            if data1_again != data1:
                print("ERROR Test 3: First URL cache corrupted after caching second URL")
                failed = True
            elif download_calls["count"] != 2:
                print("ERROR Test 3: First URL re-downloaded after second URL cached")
                failed = True
            else:
                print("Two URLs cached independently")

        # Test 4: Invalid URL (download returns None) handled gracefully
        print("*** Test 4: Invalid URL")
        invalid_url = "https://api.octopus.energy/v1/products/INVALID/electricity-tariffs/INVALID/standard-unit-rates/"

        data4 = await api.fetch_url_cached(invalid_url)

        if data4:
            print("ERROR Test 4: Expected falsy result for invalid URL, got {}".format(type(data4)))
            failed = True
        else:
            print("Successfully handled invalid URL")

        # Test 5: Download returns {} (the real error return) — must NOT be cached
        print("*** Test 5: Empty error response is not cached")
        empty_url = "https://api.octopus.energy/v1/products/empty-error/electricity-tariffs/empty-error/standard-unit-rates/"
        empty_hash = hashlib.sha256(empty_url.encode()).hexdigest()[:16]

        data5 = await api.fetch_url_cached(empty_url)
        cached5 = await storage.load("octopus", empty_hash)
        if data5:
            print("ERROR Test 5: Expected falsy result for empty error response, got {}".format(data5))
            failed = True
        elif cached5 is not None:
            print("ERROR Test 5: Empty error response should not be cached, found {}".format(cached5))
            failed = True
        else:
            print("Empty error response not cached")

    finally:
        # Clean up temporary directory
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

    if not failed:
        print("**** All fetch_url_cached tests PASSED ****")
    else:
        print("**** Some fetch_url_cached tests FAILED ****")

    return failed
