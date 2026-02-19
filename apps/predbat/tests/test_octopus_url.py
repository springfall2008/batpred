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
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
from octopus import OctopusAPI, DATE_TIME_STR_FORMAT
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session


def test_octopus_url(my_predbat=None):
    """
    OCTOPUS URL API TEST SUITE

    Comprehensive test suite for Octopus Energy URL/API operations covering:
    - URL downloads: Rate data fetching, pagination, error handling
    - Day/night rates: Tariff parsing, time window extraction
    - Saving sessions: Event retrieval, joining events, data parsing
    - Intelligent dispatch: Planned/completed dispatches, vehicle data
    - Tariff finding: Product search, tariff code extraction
    - EDF FreePhase Dynamic: Special tariff handling

    Total: 6 sub-tests
    """

    # Registry of all Octopus URL tests
    sub_tests = [
        ("download_url", _test_download_octopus_url_wrapper, "Octopus URL download (pagination, errors)"),
        ("day_night_rates", _test_async_get_day_night_rates_wrapper, "Day/night rates parsing and time windows"),
        ("saving_sessions", _test_get_saving_session_data, "Saving session events (available, joined)"),
        ("intelligent_dispatch", _test_async_intelligent_update_sensor_wrapper, "Intelligent dispatch (planned, completed, vehicle)"),
        ("find_tariffs", _test_async_find_tariffs_wrapper, "Find tariffs (product search, codes)"),
        ("edf_freephase", _test_edf_freephase_dynamic_url_wrapper, "EDF FreePhase Dynamic tariff handling"),
    ]

    print("\n" + "=" * 70)
    print("OCTOPUS URL API TEST SUITE")
    print("=" * 70)

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

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed > 0


def _test_download_octopus_url_wrapper(my_predbat):
    """
    Wrapper to run the async test function
    """
    return asyncio.run(test_download_octopus_url(my_predbat))


async def test_download_octopus_url(my_predbat):
    """
    Test the async_download_octopus_url function with various response scenarios

    Tests:
    - Test 1: Successful download with paginated results
    - Test 2: Non-200/201/400 status code (e.g., 500)
    - Test 3: JSONDecodeError
    - Test 4: 400 status with "day and night rates" detail
    - Test 5: 400 status with other error detail
    - Test 6: Missing "results" key in response
    - Test 7: Pagination (multiple pages)
    """
    print("**** Running async_download_octopus_url tests ****")
    failed = False

    # Create API instance
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)

    # Test 1: Successful download
    print("\n*** Test 1: Successful download with single page ***")
    json_data = {
        "results": [{"value_inc_vat": 15.5, "valid_from": "2024-01-01T00:00:00Z", "valid_to": "2024-01-01T00:30:00Z"}, {"value_inc_vat": 16.0, "valid_from": "2024-01-01T00:30:00Z", "valid_to": "2024-01-01T01:00:00Z"}],
        "next": None,
    }
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if len(result) != 2:
            print("ERROR: Expected 2 results, got {}".format(len(result)))
            failed = True
        else:
            print("PASS: Got 2 results as expected")

    # Test 2: Non-200/201/400 status code
    print("\n*** Test 2: Non-200/201/400 status code (500) ***")
    mock_response = create_aiohttp_mock_response(status=500)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if result != {}:
            print("ERROR: Expected empty dict for 500 error, got {}".format(result))
            failed = True
        else:
            print("PASS: Got empty dict for 500 error")

    # Test 3: JSONDecodeError
    print("\n*** Test 3: JSONDecodeError ***")
    mock_response = create_aiohttp_mock_response(status=200, json_exception=json.JSONDecodeError("msg", "doc", 0))
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if result != {}:
            print("ERROR: Expected empty dict for JSON error, got {}".format(result))
            failed = True
        else:
            print("PASS: Got empty dict for JSON decode error")

    # Test 4: 400 status with "day and night rates" detail
    print("\n*** Test 4: 400 status with 'day and night rates' detail ***")
    json_data = {"detail": "This tariff has day and night rates"}
    mock_response = create_aiohttp_mock_response(status=400, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    # Mock async_get_day_night_rates to return data
    async def mock_day_night_rates(url):
        return [{"rate": "day"}, {"rate": "night"}]

    api.async_get_day_night_rates = mock_day_night_rates

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if len(result) != 2:
            print("ERROR: Expected 2 results from day/night rates, got {}".format(len(result)))
            failed = True
        else:
            print("PASS: Got day/night rates (2 results)")

    # Test 5: 400 status with "day and night rates" but no data returned
    print("\n*** Test 5: 400 status with 'day and night rates' but empty result ***")
    json_data = {"detail": "This tariff has day and night rates"}
    mock_response = create_aiohttp_mock_response(status=400, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    # Mock async_get_day_night_rates to return empty
    async def mock_day_night_rates_empty(url):
        return []

    api.async_get_day_night_rates = mock_day_night_rates_empty

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if result != {}:
            print("ERROR: Expected empty dict when day/night rates fail, got {}".format(result))
            failed = True
        else:
            print("PASS: Got empty dict when day/night rates return empty")

    # Test 6: 400 status with other error detail
    print("\n*** Test 6: 400 status with other error detail ***")
    json_data = {"detail": "Invalid tariff code"}
    mock_response = create_aiohttp_mock_response(status=400, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if result != {}:
            print("ERROR: Expected empty dict for 400 error, got {}".format(result))
            failed = True
        else:
            print("PASS: Got empty dict for 400 error with detail")

    # Test 7: Missing "results" key in response
    print("\n*** Test 7: Missing 'results' key in response ***")
    json_data = {"detail": "Some other data", "next": None}
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if result != {}:
            print("ERROR: Expected empty dict when 'results' missing, got {}".format(result))
            failed = True
        else:
            print("PASS: Got empty dict when 'results' key missing")

    # Test 8: Pagination (multiple pages)
    print("\n*** Test 8: Pagination with multiple pages ***")
    # First page
    json_data_page1 = {"results": [{"value": 1}, {"value": 2}], "next": "https://example.com/rates?page=2"}
    mock_response_page1 = create_aiohttp_mock_response(status=200, json_data=json_data_page1)

    # Second page
    json_data_page2 = {"results": [{"value": 3}, {"value": 4}], "next": None}
    mock_response_page2 = create_aiohttp_mock_response(status=200, json_data=json_data_page2)

    # Create sessions for each page
    mock_session_page1 = create_aiohttp_mock_session(mock_response=mock_response_page1)
    mock_session_page2 = create_aiohttp_mock_session(mock_response=mock_response_page2)

    # Configure mock to return different sessions
    with patch("aiohttp.ClientSession", side_effect=[mock_session_page1, mock_session_page2]):
        result = await api.async_download_octopus_url("https://example.com/rates")

        if len(result) != 4:
            print("ERROR: Expected 4 results from 2 pages, got {}".format(len(result)))
            failed = True
        else:
            print("PASS: Got 4 results from pagination (2 pages)")

    if not failed:
        print("\n**** All async_download_octopus_url tests PASSED ****")

    return failed


def _test_async_get_day_night_rates_wrapper(my_predbat):
    """
    Wrapper to run the async test function for day/night rates
    """
    return asyncio.run(test_async_get_day_night_rates(my_predbat))


async def test_async_get_day_night_rates(my_predbat):
    """
    Test the async_get_day_night_rates function with various scenarios

    Tests:
    - Test 1: Successful fetch with valid day and night rates
    - Test 2: Missing day rate (only night rate found)
    - Test 3: Missing night rate (only day rate found)
    - Test 4: Both rates missing (empty results)
    - Test 5: Multiple rates with different valid_from timestamps
    - Test 6: Verify URL transformation (standard -> day/night)
    - Test 7: Verify generated rate schedule (8 days, 16 entries)
    """
    print("**** Running async_get_day_night_rates tests ****")
    failed = False

    # Create API instance
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)

    # Set a fixed current time for predictable testing via my_predbat mock
    fixed_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    my_predbat.now_utc_exact = fixed_time

    # Test 1: Successful fetch with valid day and night rates
    print("\n*** Test 1: Successful fetch with valid day and night rates ***")

    async def mock_fetch_url_cached_success(url):
        if "day-unit-rates" in url:
            return [{"valid_from": "2024-06-14T00:00:00+00:00", "value_inc_vat": 25.5}, {"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 26.0}]
        elif "night-unit-rates" in url:
            return [{"valid_from": "2024-06-14T00:00:00+00:00", "value_inc_vat": 12.5}, {"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 13.0}]
        return []

    api.fetch_url_cached = mock_fetch_url_cached_success

    result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates")

    if len(result) != 16:  # 8 days * 2 rates per day (day + night)
        print("ERROR: Expected 16 rate entries (8 days), got {}".format(len(result)))
        failed = True
    else:
        # Verify we have alternating night/day rates
        has_day_rate = any(r["value_inc_vat"] == 26.0 for r in result)
        has_night_rate = any(r["value_inc_vat"] == 13.0 for r in result)
        if has_day_rate and has_night_rate:
            print("PASS: Got 16 rate entries with correct day (26.0) and night (13.0) rates")
        else:
            print("ERROR: Missing expected day or night rate values")
            failed = True

    # Test 2: Missing day rate (only night rate found)
    print("\n*** Test 2: Missing day rate (only night rate found) ***")

    async def mock_fetch_url_cached_no_day(url):
        if "day-unit-rates" in url:
            return []  # No day rates
        elif "night-unit-rates" in url:
            return [{"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 13.0}]
        return []

    api.fetch_url_cached = mock_fetch_url_cached_no_day

    result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates")

    if len(result) != 0:
        print("ERROR: Expected empty result when day rate missing, got {} entries".format(len(result)))
        failed = True
    else:
        print("PASS: Got empty result when day rate missing")

    # Test 3: Missing night rate (only day rate found)
    print("\n*** Test 3: Missing night rate (only day rate found) ***")

    async def mock_fetch_url_cached_no_night(url):
        if "day-unit-rates" in url:
            return [{"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 26.0}]
        elif "night-unit-rates" in url:
            return []  # No night rates
        return []

    api.fetch_url_cached = mock_fetch_url_cached_no_night

    result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates")

    if len(result) != 0:
        print("ERROR: Expected empty result when night rate missing, got {} entries".format(len(result)))
        failed = True
    else:
        print("PASS: Got empty result when night rate missing")

    # Test 4: Both rates missing (empty results)
    print("\n*** Test 4: Both rates missing (empty results) ***")

    async def mock_fetch_url_cached_empty(url):
        return []

    api.fetch_url_cached = mock_fetch_url_cached_empty

    result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates")

    if len(result) != 0:
        print("ERROR: Expected empty result when both rates missing, got {} entries".format(len(result)))
        failed = True
    else:
        print("PASS: Got empty result when both rates missing")

    # Test 5: Multiple rates with different valid_from timestamps (should pick latest before now, ignore future)
    print("\n*** Test 5: Multiple rates - should pick latest before current time and ignore future rates ***")

    # Patch now_utc_exact to return a fixed time
    test5_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    # Test with rates in past, present, and FUTURE relative to test5_time (2024-06-15 12:00)
    # Should select the latest rate that is <= now, and ignore any future rates
    async def mock_fetch_url_cached_multiple(url):
        if "day-unit-rates" in url:
            return [
                {"valid_from": "2024-06-10T00:00:00+00:00", "value_inc_vat": 20.0},  # Old rate (past)
                {"valid_from": "2024-06-12T00:00:00+00:00", "value_inc_vat": 22.0},  # Older rate (past)
                {"valid_from": "2024-06-14T00:00:00+00:00", "value_inc_vat": 25.0},  # Latest rate before now (should be selected)
                {"valid_from": "2024-06-16T00:00:00+00:00", "value_inc_vat": 30.0},  # Future rate (should be ignored)
                {"valid_from": "2024-06-17T00:00:00+00:00", "value_inc_vat": 32.0},  # Future rate (should be ignored)
            ]
        elif "night-unit-rates" in url:
            return [
                {"valid_from": "2024-06-10T00:00:00+00:00", "value_inc_vat": 10.0},  # Old rate (past)
                {"valid_from": "2024-06-12T00:00:00+00:00", "value_inc_vat": 11.0},  # Older rate (past)
                {"valid_from": "2024-06-14T00:00:00+00:00", "value_inc_vat": 12.0},  # Latest rate before now (should be selected)
                {"valid_from": "2024-06-16T00:00:00+00:00", "value_inc_vat": 15.0},  # Future rate (should be ignored)
                {"valid_from": "2024-06-17T00:00:00+00:00", "value_inc_vat": 16.0},  # Future rate (should be ignored)
            ]
        return []

    api.fetch_url_cached = mock_fetch_url_cached_multiple

    # Use patch to mock the now_utc_exact property
    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: test5_time)):
        result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates")

    if len(result) != 16:
        print("ERROR: Expected 16 rate entries, got {}".format(len(result)))
        failed = True
    else:
        # Should use 25.0 for day and 12.0 for night (latest rates before now)
        has_correct_day = any(r["value_inc_vat"] == 25.0 for r in result)
        has_correct_night = any(r["value_inc_vat"] == 12.0 for r in result)
        has_old_rate = any(r["value_inc_vat"] in [20.0, 22.0, 10.0, 11.0] for r in result)
        has_future_rate = any(r["value_inc_vat"] in [30.0, 32.0, 15.0, 16.0] for r in result)

        if has_correct_day and has_correct_night and not has_old_rate and not has_future_rate:
            print("PASS: Correctly selected latest rates (day: 25.0, night: 12.0) and ignored future rates")
        else:
            print("ERROR: Did not select correct rates. Has day 25.0: {}, Has night 12.0: {}, Has old rates: {}, Has future rates: {}".format(has_correct_day, has_correct_night, has_old_rate, has_future_rate))
            failed = True

    # Test 6: Verify URL transformation
    print("\n*** Test 6: Verify URL transformation (standard -> day/night) ***")

    url_calls = []

    async def mock_fetch_url_cached_track_urls(url):
        url_calls.append(url)
        if "day-unit-rates" in url:
            return [{"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 26.0}]
        elif "night-unit-rates" in url:
            return [{"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 13.0}]
        return []

    api.fetch_url_cached = mock_fetch_url_cached_track_urls

    result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates/tariff")

    if len(url_calls) != 2:
        print("ERROR: Expected 2 URL calls, got {}".format(len(url_calls)))
        failed = True
    elif "day-unit-rates" not in url_calls[0] or "night-unit-rates" not in url_calls[1]:
        print("ERROR: URL transformation incorrect. Got: {}".format(url_calls))
        failed = True
    else:
        print("PASS: URLs correctly transformed to day-unit-rates and night-unit-rates")

    # Test 7: Verify rate schedule structure and timing
    print("\n*** Test 7: Verify rate schedule structure (times and alternation) ***")

    async def mock_fetch_url_cached_verify_structure(url):
        if "day-unit-rates" in url:
            return [{"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 25.5}]
        elif "night-unit-rates" in url:
            return [{"valid_from": "2024-06-15T00:00:00+00:00", "value_inc_vat": 12.5}]
        return []

    api.fetch_url_cached = mock_fetch_url_cached_verify_structure

    result = await api.async_get_day_night_rates("https://example.com/standard-unit-rates")

    if len(result) != 16:
        print("ERROR: Expected 16 entries, got {}".format(len(result)))
        failed = True
    else:
        # Verify alternating pattern: night, day, night, day...
        # Night rate: 00:30-07:30, Day rate: 07:30-00:30 (next day)
        alternates_correctly = True
        for i in range(0, len(result), 2):
            night_entry = result[i]
            day_entry = result[i + 1]

            # Night entry should have night rate value
            if night_entry["value_inc_vat"] != 12.5:
                alternates_correctly = False
                print("ERROR: Entry {} should be night rate (12.5), got {}".format(i, night_entry["value_inc_vat"]))
                break

            # Day entry should have day rate value
            if day_entry["value_inc_vat"] != 25.5:
                alternates_correctly = False
                print("ERROR: Entry {} should be day rate (25.5), got {}".format(i + 1, day_entry["value_inc_vat"]))
                break

        if alternates_correctly:
            print("PASS: Rate schedule correctly alternates night (12.5) and day (25.5) rates")
        else:
            failed = True

    if not failed:
        print("\n**** All async_get_day_night_rates tests PASSED ****")

    return failed


def _test_get_saving_session_data(my_predbat):
    """
    Test the get_saving_session_data function with various scenarios

    Tests:
    - Test 1: User hasn't joined campaign (hasJoinedCampaign = False)
    - Test 2: Available events only (not joined any)
    - Test 3: Joined events only (no available events)
    - Test 4: Mix of available and joined events
    - Test 5: Expired available events (should be filtered out)
    - Test 6: Active event (currently running)
    - Test 7: Events with all data fields (rewards, codes, IDs)
    """
    print("**** Running get_saving_session_data tests ****")
    failed = False

    # Create API instance
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)

    # Set a fixed current time for predictable testing
    fixed_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    my_predbat.now_utc_exact = fixed_time

    # Test 1: User hasn't joined campaign
    print("\n*** Test 1: User hasn't joined campaign (hasJoinedCampaign = False) ***")
    api.saving_sessions = {"account": {"hasJoinedCampaign": False, "joinedEvents": []}, "events": [{"id": "event1", "code": "CODE1", "startAt": "2024-06-16T17:00:00+00:00", "endAt": "2024-06-16T18:00:00+00:00", "rewardPerKwhInOctoPoints": 100}]}

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    if len(available) != 0 or len(joined) != 0:
        print("ERROR: Expected empty lists when campaign not joined, got available={}, joined={}".format(len(available), len(joined)))
        failed = True
    else:
        print("PASS: Got empty lists when user hasn't joined campaign")

    # Test 2: Available events only (not joined any)
    print("\n*** Test 2: Available events only (not joined any) ***")
    api.saving_sessions = {
        "account": {"hasJoinedCampaign": True, "joinedEvents": []},
        "events": [
            {"id": "event1", "code": "CODE1", "startAt": "2024-06-16T17:00:00+00:00", "endAt": "2024-06-16T18:00:00+00:00", "rewardPerKwhInOctoPoints": 100},
            {"id": "event2", "code": "CODE2", "startAt": "2024-06-17T18:00:00+00:00", "endAt": "2024-06-17T19:00:00+00:00", "rewardPerKwhInOctoPoints": 150},
        ],
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    if len(available) != 2 or len(joined) != 0:
        print("ERROR: Expected 2 available, 0 joined, got available={}, joined={}".format(len(available), len(joined)))
        failed = True
    elif available[0]["id"] != "event1" or available[0]["code"] != "CODE1" or available[0]["octopoints_per_kwh"] != 100:
        print("ERROR: Available event data incorrect: {}".format(available[0]))
        failed = True
    else:
        print("PASS: Got 2 available events with correct data")

    # Test 3: Joined events only
    print("\n*** Test 3: Joined events only (no available events) ***")
    api.saving_sessions = {
        "account": {"hasJoinedCampaign": True, "joinedEvents": [{"eventId": "event1", "startAt": "2024-06-10T17:00:00+00:00", "endAt": "2024-06-10T18:00:00+00:00", "rewardGivenInOctoPoints": 500}]},
        "events": [{"id": "event1", "code": "CODE1", "rewardPerKwhInOctoPoints": 100}],
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    if len(available) != 0 or len(joined) != 1:
        print("ERROR: Expected 0 available, 1 joined, got available={}, joined={}".format(len(available), len(joined)))
        failed = True
    elif joined[0]["id"] != "event1" or joined[0]["rewarded_octopoints"] != 500:
        print("ERROR: Joined event data incorrect: {}".format(joined[0]))
        failed = True
    else:
        print("PASS: Got 1 joined event with correct reward data")

    # Test 4: Mix of available and joined events
    print("\n*** Test 4: Mix of available and joined events ***")
    api.saving_sessions = {
        "account": {"hasJoinedCampaign": True, "joinedEvents": [{"eventId": "event1", "startAt": "2024-06-10T17:00:00+00:00", "endAt": "2024-06-10T18:00:00+00:00", "rewardGivenInOctoPoints": 500}]},
        "events": [
            {"id": "event1", "code": "CODE1", "startAt": "2024-06-10T17:00:00+00:00", "endAt": "2024-06-10T18:00:00+00:00", "rewardPerKwhInOctoPoints": 100},
            {"id": "event2", "code": "CODE2", "startAt": "2024-06-16T17:00:00+00:00", "endAt": "2024-06-16T18:00:00+00:00", "rewardPerKwhInOctoPoints": 150},
        ],
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    # event1 is joined, so only event2 should be available
    if len(available) != 1 or len(joined) != 1:
        print("ERROR: Expected 1 available, 1 joined, got available={}, joined={}".format(len(available), len(joined)))
        failed = True
    elif available[0]["id"] != "event2":
        print("ERROR: Expected event2 in available, got: {}".format(available[0]))
        failed = True
    else:
        print("PASS: Correctly separated joined and available events")

    # Test 5: Expired available events (should be filtered out)
    print("\n*** Test 5: Expired available events (should be filtered out) ***")
    api.saving_sessions = {
        "account": {"hasJoinedCampaign": True, "joinedEvents": []},
        "events": [
            {"id": "event_past", "code": "PAST", "startAt": "2024-06-10T17:00:00+00:00", "endAt": "2024-06-10T18:00:00+00:00", "rewardPerKwhInOctoPoints": 100},  # Already ended (before fixed_time)
            {"id": "event_future", "code": "FUTURE", "startAt": "2024-06-16T17:00:00+00:00", "endAt": "2024-06-16T18:00:00+00:00", "rewardPerKwhInOctoPoints": 150},  # Future event
        ],
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    # Only future event should be available
    if len(available) != 1:
        print("ERROR: Expected 1 available event (future), got {}".format(len(available)))
        failed = True
    elif available[0]["id"] != "event_future":
        print("ERROR: Expected event_future, got: {}".format(available[0]))
        failed = True
    else:
        print("PASS: Correctly filtered out expired events")

    # Test 6: Active event (currently running)
    print("\n*** Test 6: Active event (currently running) ***")
    # Active event: starts before now, ends after now
    active_start_str = (fixed_time - timedelta(minutes=30)).strftime(DATE_TIME_STR_FORMAT)
    active_end_str = (fixed_time + timedelta(minutes=30)).strftime(DATE_TIME_STR_FORMAT)
    active_start_dt = fixed_time - timedelta(minutes=30)
    active_end_dt = fixed_time + timedelta(minutes=30)

    api.saving_sessions = {
        "account": {
            "hasJoinedCampaign": True,
            "joinedEvents": [
                {"eventId": "active_event", "startAt": active_start_str, "endAt": active_end_str, "start": active_start_dt, "end": active_end_dt, "rewardGivenInOctoPoints": 0}  # Datetime object for active check  # Datetime object for active check
            ],
        },
        "events": [{"id": "active_event", "code": "ACTIVE", "rewardPerKwhInOctoPoints": 200}],
    }

    # Mock dashboard_item to capture the state
    dashboard_calls = []

    def mock_dashboard_item(entity, state, attributes=None, app=None):
        dashboard_calls.append({"entity": entity, "state": state, "attributes": attributes})

    api.dashboard_item = mock_dashboard_item
    api.get_entity_name = lambda type, name: f"predbat.{name}"

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    # Check that binary_sensor.saving_session was set to "on" for active event
    sensor_call = [c for c in dashboard_calls if "saving_session" in c["entity"] and "join" not in c["entity"]]
    if len(sensor_call) != 1 or sensor_call[0]["state"] != "on":
        print("ERROR: Expected saving_session sensor to be 'on' for active event, got: {}".format(sensor_call))
        failed = True
    else:
        print("PASS: Active event correctly detected and sensor set to 'on'")

    # Test 7: Events with all data fields
    print("\n*** Test 7: Events with all data fields (rewards, codes, IDs) ***")
    api.saving_sessions = {
        "account": {"hasJoinedCampaign": True, "joinedEvents": [{"eventId": "joined1", "startAt": "2024-06-10T17:00:00+00:00", "endAt": "2024-06-10T18:00:00+00:00", "rewardGivenInOctoPoints": 750}]},
        "events": [
            {"id": "joined1", "code": "JOINED_CODE", "startAt": "2024-06-10T17:00:00+00:00", "endAt": "2024-06-10T18:00:00+00:00", "rewardPerKwhInOctoPoints": 150},
            {"id": "available1", "code": "AVAIL_CODE", "startAt": "2024-06-16T19:00:00+00:00", "endAt": "2024-06-16T20:00:00+00:00", "rewardPerKwhInOctoPoints": 200},
        ],
    }

    dashboard_calls = []
    api.dashboard_item = mock_dashboard_item

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        available, joined = api.get_saving_session_data()

    # Verify all fields present
    if len(joined) != 1:
        print("ERROR: Expected 1 joined event, got {}".format(len(joined)))
        failed = True
    else:
        j = joined[0]
        if j["id"] != "joined1" or j["code"] != "JOINED_CODE" or j["octopoints_per_kwh"] != 150 or j["rewarded_octopoints"] != 750:
            print("ERROR: Joined event missing fields: {}".format(j))
            failed = True
        elif len(available) != 1:
            print("ERROR: Expected 1 available event, got {}".format(len(available)))
            failed = True
        else:
            a = available[0]
            if a["id"] != "available1" or a["code"] != "AVAIL_CODE" or a["octopoints_per_kwh"] != 200:
                print("ERROR: Available event missing fields: {}".format(a))
                failed = True
            else:
                print("PASS: All event data fields correctly populated")

    if not failed:
        print("\n**** All get_saving_session_data tests PASSED ****")

    return failed


def _test_async_intelligent_update_sensor_wrapper(my_predbat):
    """
    Wrapper to run the async test function for intelligent update sensor
    """
    return asyncio.run(test_async_intelligent_update_sensor(my_predbat))


async def test_async_intelligent_update_sensor(my_predbat):
    """
    Test the async_intelligent_update_sensor function with various scenarios

    Tests:
    - Test 1: No intelligent device (should return early)
    - Test 2: Active dispatch (currently running)
    - Test 3: Planned dispatch only (future)
    - Test 4: Completed dispatch only (past)
    - Test 5: Weekday target time/SOC
    - Test 6: Weekend target time/SOC
    - Test 7: Multiple dispatches with overlapping times
    """
    print("**** Running async_intelligent_update_sensor tests ****")
    failed = False

    # Create API instance
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)

    # Set a fixed current time for predictable testing (Wednesday June 12, 2024)
    fixed_time = datetime(2024, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
    my_predbat.now_utc_exact = fixed_time

    # Test 1: No intelligent device (should return early)
    print("\n*** Test 1: No intelligent device (should return early) ***")
    api.intelligent_device = None

    dashboard_calls = []

    def mock_dashboard_item(entity, state, attributes=None, app=None):
        dashboard_calls.append({"entity": entity, "state": state, "attributes": attributes})

    api.dashboard_item = mock_dashboard_item
    api.get_entity_name = lambda type, name: f"predbat.{name}"

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        await api.async_intelligent_update_sensor("account123")

    if len(dashboard_calls) != 0:
        print("ERROR: Expected no dashboard calls when no intelligent device, got {}".format(len(dashboard_calls)))
        failed = True
    else:
        print("PASS: No dashboard calls when intelligent device is None")

    # Test 2: Active dispatch (currently running)
    print("\n*** Test 2: Active dispatch (currently running) ***")
    active_start_str = (fixed_time - timedelta(minutes=30)).strftime(DATE_TIME_STR_FORMAT)
    active_end_str = (fixed_time + timedelta(minutes=30)).strftime(DATE_TIME_STR_FORMAT)

    api.intelligent_device = {"planned_dispatches": [{"start": active_start_str, "end": active_end_str}], "completed_dispatches": [], "weekday_target_time": "07:30:00", "weekday_target_soc": 80, "weekend_target_time": "09:00:00", "weekend_target_soc": 100}

    dashboard_calls = []

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        await api.async_intelligent_update_sensor("account123")

    # Check that binary_sensor.intelligent_dispatch was set to "on"
    dispatch_call = [c for c in dashboard_calls if "intelligent_dispatch" in c["entity"]]
    if len(dispatch_call) != 1 or dispatch_call[0]["state"] != "on":
        print("ERROR: Expected intelligent_dispatch sensor to be 'on', got: {}".format(dispatch_call))
        failed = True
    else:
        print("PASS: Active dispatch correctly detected and sensor set to 'on'")

    # Test 3: Planned dispatch only (future)
    print("\n*** Test 3: Planned dispatch only (future) ***")
    future_start_str = (fixed_time + timedelta(hours=2)).strftime(DATE_TIME_STR_FORMAT)
    future_end_str = (fixed_time + timedelta(hours=3)).strftime(DATE_TIME_STR_FORMAT)

    api.intelligent_device = {"planned_dispatches": [{"start": future_start_str, "end": future_end_str}], "completed_dispatches": [], "weekday_target_time": "07:30:00", "weekday_target_soc": 80, "weekend_target_time": "09:00:00", "weekend_target_soc": 100}

    dashboard_calls = []

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        await api.async_intelligent_update_sensor("account123")

    # Check that binary_sensor.intelligent_dispatch was set to "off"
    dispatch_call = [c for c in dashboard_calls if "intelligent_dispatch" in c["entity"]]
    if len(dispatch_call) != 1 or dispatch_call[0]["state"] != "off":
        print("ERROR: Expected intelligent_dispatch sensor to be 'off' for future dispatch, got: {}".format(dispatch_call))
        failed = True
    else:
        print("PASS: Future dispatch correctly detected and sensor set to 'off'")

    # Test 4: Completed dispatch only (past)
    print("\n*** Test 4: Completed dispatch only (past) ***")
    past_start_str = (fixed_time - timedelta(hours=3)).strftime(DATE_TIME_STR_FORMAT)
    past_end_str = (fixed_time - timedelta(hours=2)).strftime(DATE_TIME_STR_FORMAT)

    api.intelligent_device = {"planned_dispatches": [], "completed_dispatches": [{"start": past_start_str, "end": past_end_str}], "weekday_target_time": "07:30:00", "weekday_target_soc": 80, "weekend_target_time": "09:00:00", "weekend_target_soc": 100}

    dashboard_calls = []

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        await api.async_intelligent_update_sensor("account123")

    # Check that binary_sensor.intelligent_dispatch was set to "off"
    dispatch_call = [c for c in dashboard_calls if "intelligent_dispatch" in c["entity"]]
    if len(dispatch_call) != 1 or dispatch_call[0]["state"] != "off":
        print("ERROR: Expected intelligent_dispatch sensor to be 'off' for past dispatch, got: {}".format(dispatch_call))
        failed = True
    else:
        print("PASS: Past dispatch correctly detected and sensor set to 'off'")

    # Test 5: Weekday target time/SOC
    print("\n*** Test 5: Weekday target time/SOC ***")
    weekday_time = datetime(2024, 6, 12, 12, 0, 0, tzinfo=timezone.utc)  # Wednesday

    api.intelligent_device = {"planned_dispatches": [], "completed_dispatches": [], "weekday_target_time": "07:30:00", "weekday_target_soc": 80, "weekend_target_time": "09:00:00", "weekend_target_soc": 100}

    dashboard_calls = []

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: weekday_time)):
        await api.async_intelligent_update_sensor("account123")

    # Check that select.intelligent_target_time uses weekday value
    time_call = [c for c in dashboard_calls if "intelligent_target_time" in c["entity"]]
    soc_call = [c for c in dashboard_calls if "intelligent_target_soc" in c["entity"]]

    if len(time_call) != 1 or time_call[0]["state"] != "07:30":
        print("ERROR: Expected target_time to be '07:30' on weekday, got: {}".format(time_call))
        failed = True
    elif len(soc_call) != 1 or soc_call[0]["state"] != 80:
        print("ERROR: Expected target_soc to be 80 on weekday, got: {}".format(soc_call))
        failed = True
    else:
        print("PASS: Weekday target time (07:30) and SOC (80) correctly used")

    # Test 6: Weekend target time/SOC
    print("\n*** Test 6: Weekend target time/SOC ***")
    weekend_time = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # Saturday

    api.intelligent_device = {"planned_dispatches": [], "completed_dispatches": [], "weekday_target_time": "07:30:00", "weekday_target_soc": 80, "weekend_target_time": "09:00:00", "weekend_target_soc": 100}

    dashboard_calls = []

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: weekend_time)):
        await api.async_intelligent_update_sensor("account123")

    # Check that select.intelligent_target_time uses weekend value
    time_call = [c for c in dashboard_calls if "intelligent_target_time" in c["entity"]]
    soc_call = [c for c in dashboard_calls if "intelligent_target_soc" in c["entity"]]

    if len(time_call) != 1 or time_call[0]["state"] != "09:00":
        print("ERROR: Expected target_time to be '09:00' on weekend, got: {}".format(time_call))
        failed = True
    elif len(soc_call) != 1 or soc_call[0]["state"] != 100:
        print("ERROR: Expected target_soc to be 100 on weekend, got: {}".format(soc_call))
        failed = True
    else:
        print("PASS: Weekend target time (09:00) and SOC (100) correctly used")

    # Test 7: Multiple dispatches with overlapping times
    print("\n*** Test 7: Multiple dispatches - check attributes contain all dispatches ***")
    dispatch1_start = (fixed_time - timedelta(hours=1)).strftime(DATE_TIME_STR_FORMAT)
    dispatch1_end = (fixed_time + timedelta(hours=1)).strftime(DATE_TIME_STR_FORMAT)
    dispatch2_start = (fixed_time + timedelta(hours=2)).strftime(DATE_TIME_STR_FORMAT)
    dispatch2_end = (fixed_time + timedelta(hours=3)).strftime(DATE_TIME_STR_FORMAT)

    api.intelligent_device = {
        "planned_dispatches": [{"start": dispatch1_start, "end": dispatch1_end, "charge_kwh": 10.5}, {"start": dispatch2_start, "end": dispatch2_end, "charge_kwh": 15.0}],
        "completed_dispatches": [{"start": past_start_str, "end": past_end_str, "charge_kwh": 8.0}],
        "weekday_target_time": "07:30:00",
        "weekday_target_soc": 80,
        "weekend_target_time": "09:00:00",
        "weekend_target_soc": 100,
    }

    dashboard_calls = []

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        await api.async_intelligent_update_sensor("account123")

    # Check that attributes contain all the intelligent_device data
    dispatch_call = [c for c in dashboard_calls if "intelligent_dispatch" in c["entity"]]
    if len(dispatch_call) != 1:
        print("ERROR: Expected 1 intelligent_dispatch call, got {}".format(len(dispatch_call)))
        failed = True
    else:
        attrs = dispatch_call[0]["attributes"]
        if "planned_dispatches" not in attrs or len(attrs["planned_dispatches"]) != 2:
            print("ERROR: Expected 2 planned_dispatches in attributes, got: {}".format(attrs.get("planned_dispatches")))
            failed = True
        elif "completed_dispatches" not in attrs or len(attrs["completed_dispatches"]) != 1:
            print("ERROR: Expected 1 completed_dispatch in attributes, got: {}".format(attrs.get("completed_dispatches")))
            failed = True
        elif dispatch_call[0]["state"] != "on":  # First dispatch is active
            print("ERROR: Expected state 'on' since first dispatch is active, got: {}".format(dispatch_call[0]["state"]))
            failed = True
        else:
            print("PASS: All dispatches included in attributes and active state correctly detected")

    if not failed:
        print("\n**** All async_intelligent_update_sensor tests PASSED ****")

    return failed


def _test_async_find_tariffs_wrapper(my_predbat):
    return asyncio.run(test_async_find_tariffs(my_predbat))


async def test_async_find_tariffs(my_predbat):
    """Test async_find_tariffs function."""
    failed = False

    print("\n=== Testing async_find_tariffs ===")

    # Fixed time for tests: 2024-03-15 14:30:00 UTC
    fixed_time = datetime(2024, 3, 15, 14, 30, 0, tzinfo=timezone.utc)

    # Test 1: No account data - should return empty tariffs
    print("\nTest 1: No account data - should return empty tariffs")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    api.tariffs = {}
    api.account_data = None

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if result != {}:
        print("ERROR: Expected empty dict for no account data, got: {}".format(result))
        failed = True
    else:
        print("PASS: Returned empty tariffs when no account data")

    # Test 2: Account with active import meter and agreement
    print("\nTest 2: Active import meter and agreement")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    api.tariffs = {}

    # Create account data with active import meter
    api.account_data = {
        "account": {
            "electricityAgreements": [
                {
                    "meterPoint": {
                        "meters": [{"activeFrom": "2024-01-01", "activeTo": None, "smartImportElectricityMeter": {"deviceId": "IMPORT-DEVICE-123"}}],
                        "agreements": [{"validFrom": "2024-01-01T00:00:00+00:00", "validTo": None, "tariff": {"tariffCode": "E-1R-AGILE-24-01-01-A", "productCode": "AGILE-24-01-01"}}],
                    }
                }
            ],
            "gasAgreements": [],
        }
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if "import" not in result:
        print("ERROR: Expected 'import' key in result, got: {}".format(result))
        failed = True
    elif result["import"]["tariffCode"] != "E-1R-AGILE-24-01-01-A":
        print("ERROR: Expected tariffCode 'E-1R-AGILE-24-01-01-A', got: {}".format(result["import"]["tariffCode"]))
        failed = True
    elif result["import"]["productCode"] != "AGILE-24-01-01":
        print("ERROR: Expected productCode 'AGILE-24-01-01', got: {}".format(result["import"]["productCode"]))
        failed = True
    elif result["import"]["deviceID"] != "IMPORT-DEVICE-123":
        print("ERROR: Expected deviceID 'IMPORT-DEVICE-123', got: {}".format(result["import"]["deviceID"]))
        failed = True
    else:
        print("PASS: Found active import tariff with correct details")

    # Test 3: Account with active export meter and agreement
    print("\nTest 3: Active export meter and agreement")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    api.tariffs = {}

    api.account_data = {
        "account": {
            "electricityAgreements": [
                {
                    "meterPoint": {
                        "meters": [{"activeFrom": "2024-01-01", "activeTo": None, "smartExportElectricityMeter": {"deviceId": "EXPORT-DEVICE-456"}}],
                        "agreements": [{"validFrom": "2024-01-01T00:00:00+00:00", "validTo": None, "tariff": {"tariffCode": "E-1R-OUTGOING-24-01-01-A", "productCode": "OUTGOING-24-01-01"}}],
                    }
                }
            ],
            "gasAgreements": [],
        }
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if "export" not in result:
        print("ERROR: Expected 'export' key in result, got: {}".format(result))
        failed = True
    elif result["export"]["tariffCode"] != "E-1R-OUTGOING-24-01-01-A":
        print("ERROR: Expected tariffCode 'E-1R-OUTGOING-24-01-01-A', got: {}".format(result["export"]["tariffCode"]))
        failed = True
    elif result["export"]["deviceID"] != "EXPORT-DEVICE-456":
        print("ERROR: Expected deviceID 'EXPORT-DEVICE-456', got: {}".format(result["export"]["deviceID"]))
        failed = True
    else:
        print("PASS: Found active export tariff with correct details")

    # Test 4: Account with active gas meter and agreement
    print("\nTest 4: Active gas meter and agreement")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    api.tariffs = {}

    api.account_data = {
        "account": {
            "electricityAgreements": [],
            "gasAgreements": [
                {
                    "meterPoint": {
                        "meters": [{"activeFrom": "2024-01-01", "activeTo": None, "smartGasMeter": {"deviceId": "GAS-DEVICE-789"}}],
                        "agreements": [{"validFrom": "2024-01-01T00:00:00+00:00", "validTo": None, "tariff": {"tariffCode": "G-1R-VAR-24-01-01-A", "productCode": "VAR-24-01-01"}}],
                    }
                }
            ],
        }
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if "gas" not in result:
        print("ERROR: Expected 'gas' key in result, got: {}".format(result))
        failed = True
    elif result["gas"]["tariffCode"] != "G-1R-VAR-24-01-01-A":
        print("ERROR: Expected tariffCode 'G-1R-VAR-24-01-01-A', got: {}".format(result["gas"]["tariffCode"]))
        failed = True
    elif result["gas"]["deviceID"] != "GAS-DEVICE-789":
        print("ERROR: Expected deviceID 'GAS-DEVICE-789', got: {}".format(result["gas"]["deviceID"]))
        failed = True
    else:
        print("PASS: Found active gas tariff with correct details")

    # Test 5: Inactive meter (expired activeTo) - should not find tariff
    print("\nTest 5: Inactive meter (expired) - should not find tariff")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    api.tariffs = {}

    api.account_data = {
        "account": {
            "electricityAgreements": [
                {
                    "meterPoint": {
                        "meters": [{"activeFrom": "2023-01-01", "activeTo": "2024-01-01", "smartImportElectricityMeter": {"deviceId": "EXPIRED-DEVICE"}}],  # Expired before fixed_time
                        "agreements": [{"validFrom": "2023-01-01T00:00:00+00:00", "validTo": None, "tariff": {"tariffCode": "EXPIRED-TARIFF", "productCode": "EXPIRED-PRODUCT"}}],
                    }
                }
            ],
            "gasAgreements": [],
        }
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if result != {}:
        print("ERROR: Expected empty dict for expired meter, got: {}".format(result))
        failed = True
    else:
        print("PASS: Correctly ignored expired meter")

    # Test 6: Inactive agreement (expired validTo) - should not find tariff
    print("\nTest 6: Inactive agreement (expired) - should not find tariff")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    api.tariffs = {}

    api.account_data = {
        "account": {
            "electricityAgreements": [
                {
                    "meterPoint": {
                        "meters": [{"activeFrom": "2024-01-01", "activeTo": None, "smartImportElectricityMeter": {"deviceId": "ACTIVE-DEVICE"}}],
                        "agreements": [{"validFrom": "2024-01-01T00:00:00+00:00", "validTo": "2024-02-01T00:00:00+00:00", "tariff": {"tariffCode": "EXPIRED-AGREEMENT", "productCode": "EXPIRED-PRODUCT"}}],  # Expired before fixed_time
                    }
                }
            ],
            "gasAgreements": [],
        }
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if result != {}:
        print("ERROR: Expected empty dict for expired agreement, got: {}".format(result))
        failed = True
    else:
        print("PASS: Correctly ignored expired agreement")

    # Test 7: Multiple meters and agreements, with existing tariff data preserved
    print("\nTest 7: Multiple meters/agreements with existing data preservation")
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)

    # Set up existing tariff data that should be preserved
    api.tariffs = {"import": {"data": [{"rate": 15.0}], "standing": 25.0}, "export": {"data": [{"rate": 5.0}], "standing": 0.0}}

    api.account_data = {
        "account": {
            "electricityAgreements": [
                {
                    "meterPoint": {
                        "meters": [{"activeFrom": "2024-01-01", "activeTo": None, "smartImportElectricityMeter": {"deviceId": "IMPORT-MULTI"}, "smartExportElectricityMeter": {"deviceId": "EXPORT-MULTI"}}],
                        "agreements": [{"validFrom": "2024-01-01T00:00:00+00:00", "validTo": None, "tariff": {"tariffCode": "E-1R-FLUX-24-01-01-A", "productCode": "FLUX-24-01-01"}}],
                    }
                }
            ],
            "gasAgreements": [],
        }
    }

    with patch.object(type(api), "now_utc_exact", new_callable=lambda: property(lambda self: fixed_time)):
        result = await api.async_find_tariffs()

    if "import" not in result or "export" not in result:
        print("ERROR: Expected both 'import' and 'export' keys in result, got: {}".format(result.keys()))
        failed = True
    elif result["import"]["deviceID"] != "IMPORT-MULTI":
        print("ERROR: Expected import deviceID 'IMPORT-MULTI', got: {}".format(result["import"]["deviceID"]))
        failed = True
    elif result["export"]["deviceID"] != "EXPORT-MULTI":
        print("ERROR: Expected export deviceID 'EXPORT-MULTI', got: {}".format(result["export"]["deviceID"]))
        failed = True
    elif result["import"]["data"] != [{"rate": 15.0}]:
        print("ERROR: Expected preserved import data [{'rate': 15.0}], got: {}".format(result["import"]["data"]))
        failed = True
    elif result["import"]["standing"] != 25.0:
        print("ERROR: Expected preserved import standing 25.0, got: {}".format(result["import"]["standing"]))
        failed = True
    elif result["export"]["data"] != [{"rate": 5.0}]:
        print("ERROR: Expected preserved export data [{'rate': 5.0}], got: {}".format(result["export"]["data"]))
        failed = True
    else:
        print("PASS: Found both import/export tariffs and preserved existing data")

    if not failed:
        print("\n**** All async_find_tariffs tests PASSED ****")

    return failed


def _test_edf_freephase_dynamic_url_wrapper(my_predbat):
    """
    Wrapper to run the async test function for EDF FreePhase Dynamic tariff URL
    """
    return asyncio.run(test_edf_freephase_dynamic_url(my_predbat))


async def test_edf_freephase_dynamic_url(my_predbat):
    """
    Test the async_download_octopus_url and download_octopus_rates_func with EDF FreePhase Dynamic tariff data

    Tests:
    - Test 1: Verify EDF API response structure decodes correctly (async_download_octopus_url)
    - Test 2: Verify pagination handling for EDF API (async_download_octopus_url)
    - Test 3: Verify rate data structure matches expected format (async_download_octopus_url)
    - Test 4: Verify download_octopus_rates_func with EDF data (single page)
    - Test 5: Verify download_octopus_rates_func with EDF data (pagination)
    """
    print("**** Running EDF FreePhase Dynamic URL tests ****")
    failed = False

    # Create API instance
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)

    # Test 1: Verify EDF API response structure decodes correctly
    print("\n*** Test 1: Verify EDF API response decodes correctly ***")
    json_data = {
        "count": 10126,
        "next": None,
        "previous": None,
        "results": [
            {"value_exc_vat": 15.775, "value_inc_vat": 16.56375, "valid_from": "2025-12-17T22:30:00Z", "valid_to": "2025-12-17T23:00:00Z", "payment_method": "DIRECT_DEBIT"},
            {"value_exc_vat": 15.775, "value_inc_vat": 16.56375, "valid_from": "2025-12-17T22:00:00Z", "valid_to": "2025-12-17T22:30:00Z", "payment_method": "DIRECT_DEBIT"},
            {"value_exc_vat": 16.025, "value_inc_vat": 16.82625, "valid_from": "2025-12-17T21:30:00Z", "valid_to": "2025-12-17T22:00:00Z", "payment_method": "DIRECT_DEBIT"},
        ],
    }
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates")

        if len(result) != 3:
            print("ERROR: Expected 3 results, got {}".format(len(result)))
            print(result)
            failed = True
        elif not all("value_inc_vat" in r for r in result):
            print("ERROR: Missing 'value_inc_vat' in results")
            failed = True
        elif not all("valid_from" in r for r in result):
            print("ERROR: Missing 'valid_from' in results")
            failed = True
        elif not all("payment_method" in r for r in result):
            print("ERROR: Missing 'payment_method' in results")
            failed = True
        else:
            print("PASS: EDF API response decoded correctly with 3 results")

    # Test 2: Verify pagination handling for EDF API
    print("\n*** Test 2: Verify pagination handling for EDF API ***")
    # First page
    json_data_page1 = {
        "count": 200,
        "next": "https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates/?page=2",
        "previous": None,
        "results": [
            {"value_exc_vat": 15.0, "value_inc_vat": 15.75, "valid_from": "2025-12-17T23:00:00Z", "valid_to": "2025-12-17T23:30:00Z", "payment_method": "DIRECT_DEBIT"},
            {"value_exc_vat": 15.5, "value_inc_vat": 16.275, "valid_from": "2025-12-17T22:30:00Z", "valid_to": "2025-12-17T23:00:00Z", "payment_method": "DIRECT_DEBIT"},
        ],
    }
    mock_response_page1 = create_aiohttp_mock_response(status=200, json_data=json_data_page1)

    # Second page
    json_data_page2 = {
        "count": 200,
        "next": None,
        "previous": "https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates/?page=1",
        "results": [
            {"value_exc_vat": 16.0, "value_inc_vat": 16.8, "valid_from": "2025-12-17T22:00:00Z", "valid_to": "2025-12-17T22:30:00Z", "payment_method": "DIRECT_DEBIT"},
        ],
    }
    mock_response_page2 = create_aiohttp_mock_response(status=200, json_data=json_data_page2)

    # Create sessions for each page
    mock_session_page1 = create_aiohttp_mock_session(mock_response=mock_response_page1)
    mock_session_page2 = create_aiohttp_mock_session(mock_response=mock_response_page2)

    # Configure mock to return different sessions
    with patch("aiohttp.ClientSession", side_effect=[mock_session_page1, mock_session_page2]):
        result = await api.async_download_octopus_url("https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates")

        if len(result) != 3:
            print("ERROR: Expected 3 results from 2 pages, got {}".format(len(result)))
            failed = True
        else:
            print("PASS: EDF API pagination handled correctly (3 results from 2 pages)")

    # Test 3: Verify rate data structure matches expected format
    print("\n*** Test 3: Verify rate data structure matches expected format ***")
    json_data = {
        "count": 1,
        "next": None,
        "previous": None,
        "results": [
            {
                "value_exc_vat": 15.775,
                "value_inc_vat": 16.56375,
                "valid_from": "2025-12-17T22:30:00Z",
                "valid_to": "2025-12-17T23:00:00Z",
                "payment_method": "DIRECT_DEBIT",
            }
        ],
    }
    mock_response = create_aiohttp_mock_response(status=200, json_data=json_data)
    mock_session = create_aiohttp_mock_session(mock_response=mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await api.async_download_octopus_url("https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates")

        if len(result) != 1:
            print("ERROR: Expected 1 result, got {}".format(len(result)))
            failed = True
        else:
            rate = result[0]
            # Verify all expected fields are present
            expected_fields = ["value_exc_vat", "value_inc_vat", "valid_from", "valid_to", "payment_method"]
            missing_fields = [f for f in expected_fields if f not in rate]
            if missing_fields:
                print("ERROR: Missing expected fields: {}".format(missing_fields))
                failed = True
            # Verify values match expected types/values
            elif rate["value_exc_vat"] != 15.775:
                print("ERROR: Expected value_exc_vat 15.775, got {}".format(rate["value_exc_vat"]))
                failed = True
            elif rate["value_inc_vat"] != 16.56375:
                print("ERROR: Expected value_inc_vat 16.56375, got {}".format(rate["value_inc_vat"]))
                failed = True
            elif rate["payment_method"] != "DIRECT_DEBIT":
                print("ERROR: Expected payment_method 'DIRECT_DEBIT', got {}".format(rate["payment_method"]))
                failed = True
            else:
                print("PASS: Rate data structure matches expected format with correct values")

    # Test 4: Verify download_octopus_rates_func with EDF data (single page)
    print("\n*** Test 4: Verify download_octopus_rates_func with EDF data (single page) ***")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0
    # Set midnight_utc to match the test data (2025-12-17)
    my_predbat.midnight_utc = datetime.strptime("2025-12-17T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    test_url = "https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates"

    with patch("requests.get") as mock_get:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "count": 3,
            "next": None,
            "previous": None,
            "results": [
                {"value_exc_vat": 15.775, "value_inc_vat": 16.56375, "valid_from": "2025-12-17T22:30:00Z", "valid_to": "2025-12-17T23:00:00Z", "payment_method": "DIRECT_DEBIT"},
                {"value_exc_vat": 15.775, "value_inc_vat": 16.56375, "valid_from": "2025-12-17T22:00:00Z", "valid_to": "2025-12-17T22:30:00Z", "payment_method": "DIRECT_DEBIT"},
                {"value_exc_vat": 16.025, "value_inc_vat": 16.82625, "valid_from": "2025-12-17T21:30:00Z", "valid_to": "2025-12-17T22:00:00Z", "payment_method": "DIRECT_DEBIT"},
            ],
        }
        mock_get.return_value = mock_response

        result = my_predbat.download_octopus_rates_func(test_url)

        # Result should be a dict mapping minutes to rates
        # Times: 21:30-22:00 (1290-1319 min) = 16.8263 (rounded to 4dp), 22:00-22:30 (1320-1349 min) = 16.5637, 22:30-23:00 (1350-1379 min) = 16.5637
        # Note: minute_data uses dp4() which rounds to 4 decimal places using Python's banker's rounding
        if not isinstance(result, dict):
            print("ERROR: Expected dict result from download_octopus_rates_func, got {}".format(type(result)))
            failed = True
        elif len(result) == 0:
            print("ERROR: Expected non-empty result from download_octopus_rates_func")
            failed = True
        # Check specific minute values to verify correct rate mapping
        elif result.get(1290) != 16.8263:  # 21:30 start (16.82625 rounded to 4dp)
            print("ERROR: Expected rate 16.8263 at minute 1290 (21:30), got {}".format(result.get(1290)))
            failed = True
        elif result.get(1319) != 16.8263:  # 21:30 slot last minute
            print("ERROR: Expected rate 16.8263 at minute 1319 (21:59), got {}".format(result.get(1319)))
            failed = True
        elif result.get(1320) != 16.5637:  # 22:00 start (16.56375 rounded to 4dp = 16.5637 banker's rounding)
            print("ERROR: Expected rate 16.5637 at minute 1320 (22:00), got {}".format(result.get(1320)))
            failed = True
        elif result.get(1350) != 16.5637:  # 22:30 start
            print("ERROR: Expected rate 16.5637 at minute 1350 (22:30), got {}".format(result.get(1350)))
            failed = True
        elif result.get(1379) != 16.5637:  # 22:30 slot last minute
            print("ERROR: Expected rate 16.5637 at minute 1379 (22:59), got {}".format(result.get(1379)))
            failed = True
        else:
            print("PASS: download_octopus_rates_func returned correct rate values at expected minutes")
            print("      21:30-22:00 = 16.8263, 22:00-22:30 = 16.5637, 22:30-23:00 = 16.5637")

    # Test 5: Verify download_octopus_rates_func with EDF data (pagination)
    print("\n*** Test 5: Verify download_octopus_rates_func with EDF data (pagination) ***")
    my_predbat.debug_enable = False
    my_predbat.failures_total = 0

    with patch("requests.get") as mock_get:
        # First page
        mock_response_page1 = Mock()
        mock_response_page1.status_code = 200
        mock_response_page1.json.return_value = {
            "count": 200,
            "next": "https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates/?page=2",
            "previous": None,
            "results": [
                {"value_exc_vat": 15.0, "value_inc_vat": 15.75, "valid_from": "2025-12-17T23:00:00Z", "valid_to": "2025-12-17T23:30:00Z", "payment_method": "DIRECT_DEBIT"},
                {"value_exc_vat": 15.5, "value_inc_vat": 16.275, "valid_from": "2025-12-17T22:30:00Z", "valid_to": "2025-12-17T23:00:00Z", "payment_method": "DIRECT_DEBIT"},
            ],
        }

        # Second page
        mock_response_page2 = Mock()
        mock_response_page2.status_code = 200
        mock_response_page2.json.return_value = {
            "count": 200,
            "next": None,
            "previous": "https://api.edfgb-kraken.energy/v1/products/EDF_FREEPHASE_DYNAMIC_12M_HH/electricity-tariffs/E-1R-EDF_FREEPHASE_DYNAMIC_12M_HH-J/standard-unit-rates/?page=1",
            "results": [
                {"value_exc_vat": 16.0, "value_inc_vat": 16.8, "valid_from": "2025-12-17T22:00:00Z", "valid_to": "2025-12-17T22:30:00Z", "payment_method": "DIRECT_DEBIT"},
            ],
        }

        # Configure mock to return different responses
        mock_get.side_effect = [mock_response_page1, mock_response_page2]

        result = my_predbat.download_octopus_rates_func(test_url)

        # Result should have combined data from both pages
        # Times: 22:00-22:30 (1320-1349 min) = 16.8, 22:30-23:00 (1350-1379 min) = 16.275, 23:00-23:30 (1380-1409 min) = 15.75
        if not isinstance(result, dict):
            print("ERROR: Expected dict result from download_octopus_rates_func with pagination, got {}".format(type(result)))
            failed = True
        elif len(result) == 0:
            print("ERROR: Expected non-empty result from download_octopus_rates_func with pagination")
            failed = True
        # Check specific minute values across both pages
        elif result.get(1320) != 16.8:  # 22:00 start (page 2)
            print("ERROR: Expected rate 16.8 at minute 1320 (22:00), got {}".format(result.get(1320)))
            failed = True
        elif result.get(1349) != 16.8:  # 22:00 slot last minute (page 2)
            print("ERROR: Expected rate 16.8 at minute 1349 (22:29), got {}".format(result.get(1349)))
            failed = True
        elif result.get(1350) != 16.275:  # 22:30 start (page 1)
            print("ERROR: Expected rate 16.275 at minute 1350 (22:30), got {}".format(result.get(1350)))
            failed = True
        elif result.get(1380) != 15.75:  # 23:00 start (page 1)
            print("ERROR: Expected rate 15.75 at minute 1380 (23:00), got {}".format(result.get(1380)))
            failed = True
        elif result.get(1409) != 15.75:  # 23:00 slot last minute (page 1)
            print("ERROR: Expected rate 15.75 at minute 1409 (23:29), got {}".format(result.get(1409)))
            failed = True
        else:
            print("PASS: download_octopus_rates_func handled pagination correctly with correct rate values")
            print("      22:00-22:30 = 16.8, 22:30-23:00 = 16.275, 23:00-23:30 = 15.75")

    if not failed:
        print("\n**** All EDF FreePhase Dynamic URL tests PASSED ****")

    return failed
