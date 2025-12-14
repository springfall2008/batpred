# fmt: off
# pylint: disable=line-too-long
from datetime import datetime, timedelta
from unittest.mock import Mock, patch


def test_octopus_download_rates_wrapper(my_predbat):
    """
    Wrapper function to run the octopus download rates tests
    """
    return test_octopus_download_rates(my_predbat)


def test_octopus_download_rates(my_predbat):
    """
    Test suite for download_octopus_rates function which downloads Octopus Energy tariff rates

    Tests cover:
    1. Successful download with single page response
    2. Cache hit - returns cached data when fresh
    3. Cache miss - downloads when cache expired (>30 min)
    4. Download failure - returns stale cache when available
    5. Download failure - raises ValueError when no cache available
    6. Retry mechanism - succeeds on second attempt
    7. download_octopus_rates_func - successful single page
    8. download_octopus_rates_func - pagination (multiple pages)
    9. download_octopus_rates_func - ConnectionError
    10. download_octopus_rates_func - HTTP error status
    11. download_octopus_rates_func - JSON decode error
    12. download_octopus_rates_func - missing 'results' key
    """
    print("\n=== Test Octopus download_octopus_rates ===")
    failed = False

    # Test 1: Successful download with single page response
    print("\nTest 1: Successful download with single page response")
    my_predbat.octopus_url_cache = {}
    my_predbat.midnight_utc = datetime.strptime("2024-06-12T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0

    # Mock the download_octopus_rates_func to return rate data
    mock_rate_data = {
        0: 10.5,   # 00:00
        60: 12.0,  # 01:00
        120: 15.5  # 02:00
    }

    with patch.object(my_predbat, 'download_octopus_rates_func', return_value=mock_rate_data):
        result = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-24-04-03/electricity-tariffs/E-1R-AGILE-24-04-03-A/standard-unit-rates/")

    if result != mock_rate_data:
        print(f"✗ Test 1 failed - Expected rate data, got {result}")
        failed = True
    elif "https://api.octopus.energy/v1/products/AGILE-24-04-03/electricity-tariffs/E-1R-AGILE-24-04-03-A/standard-unit-rates/" not in my_predbat.octopus_url_cache:
        print("✗ Test 1 failed - URL not in cache")
        failed = True
    else:
        print("✓ Test 1 passed - Single page download successful")

    # Test 2: Cache hit - returns cached data when fresh
    print("\nTest 2: Cache hit - returns cached data when fresh")
    test_url = "https://api.octopus.energy/test"
    cached_data = {0: 5.0, 60: 6.0}
    fresh_timestamp = datetime.now() - timedelta(minutes=10)  # 10 minutes ago (fresh)

    my_predbat.octopus_url_cache = {
        test_url: {
            "stamp": fresh_timestamp,
            "data": cached_data
        }
    }

    # Mock download_octopus_rates_func - should NOT be called due to cache hit
    with patch.object(my_predbat, 'download_octopus_rates_func') as mock_download:
        result = my_predbat.download_octopus_rates(test_url)

    if result != cached_data:
        print(f"✗ Test 2 failed - Expected cached data {cached_data}, got {result}")
        failed = True
    elif mock_download.called:
        print("✗ Test 2 failed - download_octopus_rates_func should not be called")
        failed = True
    else:
        print("✓ Test 2 passed - Cache hit returns fresh data")

    # Test 3: Cache miss - downloads when cache expired (>30 min)
    print("\nTest 3: Cache miss - downloads when cache expired")
    test_url = "https://api.octopus.energy/test-stale"
    stale_data = {0: 5.0}
    new_data = {0: 10.0, 60: 12.0}
    stale_timestamp = datetime.now() - timedelta(minutes=40)  # 40 minutes ago (stale)

    my_predbat.octopus_url_cache = {
        test_url: {
            "stamp": stale_timestamp,
            "data": stale_data
        }
    }

    with patch.object(my_predbat, 'download_octopus_rates_func', return_value=new_data):
        result = my_predbat.download_octopus_rates(test_url)

    if result != new_data:
        print(f"✗ Test 3 failed - Expected new data {new_data}, got {result}")
        failed = True
    elif my_predbat.octopus_url_cache[test_url]["data"] != new_data:
        print("✗ Test 3 failed - Cache not updated with new data")
        failed = True
    else:
        print("✓ Test 3 passed - Cache miss triggers download")

    # Test 4: Download failure - returns stale cache when available
    print("\nTest 4: Download failure - returns stale cache when available")
    test_url = "https://api.octopus.energy/test-fail"
    cached_data = {0: 7.5}
    my_predbat.octopus_url_cache = {
        test_url: {
            "stamp": datetime.now() - timedelta(minutes=50),
            "data": cached_data
        }
    }

    # Mock download to return empty dict (failure)
    with patch.object(my_predbat, 'download_octopus_rates_func', return_value={}):
        result = my_predbat.download_octopus_rates(test_url)

    if result != cached_data:
        print(f"✗ Test 4 failed - Expected stale cached data {cached_data}, got {result}")
        failed = True
    else:
        print("✓ Test 4 passed - Download failure returns stale cache")

    # Test 5: Download failure - raises ValueError when no cache available
    print("\nTest 5: Download failure - raises ValueError when no cache")
    my_predbat.octopus_url_cache = {}  # No cache
    test_url = "https://api.octopus.energy/test-fail-no-cache"

    with patch.object(my_predbat, 'download_octopus_rates_func', return_value={}):
        try:
            result = my_predbat.download_octopus_rates(test_url)
            print("✗ Test 5 failed - Expected ValueError to be raised")
            failed = True
        except ValueError:
            print("✓ Test 5 passed - ValueError raised when no cache available")

    # Test 6: Retry mechanism - succeeds on second attempt
    print("\nTest 6: Retry mechanism - succeeds on second attempt")
    my_predbat.octopus_url_cache = {}
    test_url = "https://api.octopus.energy/test-retry"
    success_data = {0: 20.0}

    # First call returns empty, second call succeeds
    with patch.object(my_predbat, 'download_octopus_rates_func', side_effect=[{}, success_data]):
        result = my_predbat.download_octopus_rates(test_url)

    if result != success_data:
        print(f"✗ Test 6 failed - Expected success data {success_data}, got {result}")
        failed = True
    else:
        print("✓ Test 6 passed - Retry mechanism works correctly")

    # Test 7: download_octopus_rates_func - successful single page
    print("\nTest 7: download_octopus_rates_func - successful single page")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0
    my_predbat.midnight_utc = datetime.strptime("2024-06-12T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")

    test_url = "https://api.octopus.energy/test-func"

    # Mock requests.get
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {"valid_from": "2024-06-12T00:00:00Z", "valid_to": "2024-06-12T00:30:00Z", "value_inc_vat": 10.5},
            {"valid_from": "2024-06-12T00:30:00Z", "valid_to": "2024-06-12T01:00:00Z", "value_inc_vat": 12.0}
        ],
        "next": None
    }

    import requests
    with patch('requests.get', return_value=mock_response):
        result = my_predbat.download_octopus_rates_func(test_url)

    # minute_data expands to every minute in the range, so check key points
    if result.get(0) != 10.5 or result.get(29) != 10.5 or result.get(30) != 12.0 or result.get(59) != 12.0:
        print(f"✗ Test 7 failed - Expected rate data at key minutes, got {result.get(0)}, {result.get(29)}, {result.get(30)}, {result.get(59)}")
        failed = True
    else:
        print("✓ Test 7 passed - download_octopus_rates_func single page success")

    # Test 8: download_octopus_rates_func - pagination (multiple pages)
    print("\nTest 8: download_octopus_rates_func - pagination")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0

    test_url = "https://api.octopus.energy/test-pagination"

    # Mock two pages of results
    mock_response_1 = Mock()
    mock_response_1.status_code = 200
    mock_response_1.json.return_value = {
        "results": [
            {"valid_from": "2024-06-12T00:00:00Z", "valid_to": "2024-06-12T00:30:00Z", "value_inc_vat": 10.5}
        ],
        "next": "https://api.octopus.energy/test-pagination?page=2"
    }

    mock_response_2 = Mock()
    mock_response_2.status_code = 200
    mock_response_2.json.return_value = {
        "results": [
            {"valid_from": "2024-06-12T00:30:00Z", "valid_to": "2024-06-12T01:00:00Z", "value_inc_vat": 12.0}
        ],
        "next": None
    }

    with patch('requests.get', side_effect=[mock_response_1, mock_response_2]):
        result = my_predbat.download_octopus_rates_func(test_url)

    # Check that both pages were fetched and combined
    if result.get(0) != 10.5 or result.get(29) != 10.5 or result.get(30) != 12.0 or result.get(59) != 12.0:
        print(f"✗ Test 8 failed - Expected combined rate data, got minute 0: {result.get(0)}, minute 29: {result.get(29)}, minute 30: {result.get(30)}, minute 59: {result.get(59)}")
        failed = True
    else:
        print("✓ Test 8 passed - Pagination works correctly")

    # Test 9: download_octopus_rates_func - ConnectionError
    print("\nTest 9: download_octopus_rates_func - ConnectionError")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0
    test_url = "https://api.octopus.energy/test-connection-error"

    with patch('requests.get', side_effect=requests.exceptions.ConnectionError()):
        result = my_predbat.download_octopus_rates_func(test_url)

    if result != {}:
        print(f"✗ Test 9 failed - Expected empty dict, got {result}")
        failed = True
    else:
        print("✓ Test 9 passed - ConnectionError handled correctly")

    # Test 10: download_octopus_rates_func - HTTP error status
    print("\nTest 10: download_octopus_rates_func - HTTP error status")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0
    test_url = "https://api.octopus.energy/test-404"

    mock_response = Mock()
    mock_response.status_code = 404

    with patch('requests.get', return_value=mock_response):
        result = my_predbat.download_octopus_rates_func(test_url)

    if result != {}:
        print(f"✗ Test 10 failed - Expected empty dict, got {result}")
        failed = True
    else:
        print("✓ Test 10 passed - HTTP error status handled")

    # Test 11: download_octopus_rates_func - JSON decode error
    print("\nTest 11: download_octopus_rates_func - JSON decode error")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0
    test_url = "https://api.octopus.energy/test-json-error"

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.side_effect = requests.exceptions.JSONDecodeError("Invalid JSON", "", 0)

    with patch('requests.get', return_value=mock_response):
        result = my_predbat.download_octopus_rates_func(test_url)

    if result != {}:
        print(f"✗ Test 11 failed - Expected empty dict, got {result}")
        failed = True
    elif my_predbat.failures_total != 1:
        print(f"✗ Test 11 failed - Expected failures_total=1, got {my_predbat.failures_total}")
        failed = True
    else:
        print("✓ Test 11 passed - JSON decode error handled")

    # Test 12: download_octopus_rates_func - missing 'results' key
    print("\nTest 12: download_octopus_rates_func - missing 'results' key")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0
    test_url = "https://api.octopus.energy/test-no-results"

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": "some data"}  # Missing 'results' key

    with patch('requests.get', return_value=mock_response):
        result = my_predbat.download_octopus_rates_func(test_url)

    if result != {}:
        print(f"✗ Test 12 failed - Expected empty dict, got {result}")
        failed = True
    else:
        print("✓ Test 12 passed - Missing 'results' key handled")

    if failed:
        print("\n=== Some download_octopus_rates tests FAILED ===")
        return True
    else:
        print("\n=== All download_octopus_rates tests PASSED ===")
        return False
