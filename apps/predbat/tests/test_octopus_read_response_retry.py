"""
Tests for Octopus async_read_response_retry function
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from octopus import OctopusAPI, OCTOPUS_MAX_RETRIES


def test_octopus_read_response_retry_wrapper(my_predbat):
    return asyncio.run(test_octopus_read_response_retry(my_predbat))


async def test_octopus_read_response_retry(my_predbat):
    """
    Test OctopusAPI async_read_response_retry method with exponential backoff.

    Tests:
    - Test 1: Successful response on first attempt (no retry)
    - Test 2: Successful response on second attempt (1 retry)
    - Test 3: Successful response on last attempt (max retries - 1)
    - Test 4: All retries fail, returns None and increments failures_total
    - Test 5: Exponential backoff timing is correct
    - Test 6: ignore_errors parameter is passed through correctly
    - Test 7: Rate limit errors trigger retry with backoff
    - Test 8: Auth errors trigger retry with backoff
    """
    print("**** Running Octopus async_read_response_retry tests ****")
    failed = False

    # Helper function to create mock response
    def create_mock_response(status, text_content, headers=None):
        """Create a mock HTTP response"""
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.text = AsyncMock(return_value=text_content)

        # Mock request_info with headers
        mock_resp.request_info = MagicMock()
        if headers is None:
            headers = {}
        mock_resp.request_info.headers = headers

        return mock_resp

    # Test 1: Successful response on first attempt (no retry)
    print("\n*** Test 1: Successful response on first attempt (no retry) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    successful_data = {"data": {"result": "success"}}
    response = create_mock_response(200, json.dumps(successful_data))
    url = "https://api.octopus.energy/v1/test"

    # Mock async_read_response to succeed on first call
    api.async_read_response = AsyncMock(return_value=successful_data)

    result = await api.async_read_response_retry(response, url, ignore_errors=False)

    if result != successful_data:
        print(f"ERROR: Expected successful data, got {result}")
        failed = True
    elif api.async_read_response.call_count != 1:
        print(f"ERROR: Expected 1 call to async_read_response, got {api.async_read_response.call_count}")
        failed = True
    elif api.failures_total != 0:
        print(f"ERROR: failures_total should be 0, got {api.failures_total}")
        failed = True
    else:
        print("PASS: Successful response on first attempt, no retry needed")

    # Test 2: Successful response on second attempt (1 retry)
    print("\n*** Test 2: Successful response on second attempt (1 retry) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    successful_data = {"data": {"result": "success"}}
    response = create_mock_response(200, json.dumps(successful_data))

    # Mock async_read_response to fail once, then succeed
    api.async_read_response = AsyncMock(side_effect=[None, successful_data])

    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, url, ignore_errors=False)

        if result != successful_data:
            print(f"ERROR: Expected successful data, got {result}")
            failed = True
        elif api.async_read_response.call_count != 2:
            print(f"ERROR: Expected 2 calls to async_read_response, got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != 1:
            print(f"ERROR: Expected 1 sleep call, got {mock_sleep.call_count}")
            failed = True
        elif mock_sleep.call_args[0][0] != 1:  # 2^0 = 1 second
            print(f"ERROR: Expected 1 second sleep, got {mock_sleep.call_args[0][0]}")
            failed = True
        elif api.failures_total != 0:
            print(f"ERROR: failures_total should be 0 after eventual success, got {api.failures_total}")
            failed = True
        else:
            print("PASS: Successful response on second attempt with exponential backoff")

    # Test 3: Successful response on last attempt (max retries - 1)
    print("\n*** Test 3: Successful response on last attempt (max retries - 1) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    successful_data = {"data": {"result": "success"}}
    response = create_mock_response(200, json.dumps(successful_data))

    # Mock async_read_response to fail (OCTOPUS_MAX_RETRIES - 1) times, then succeed
    side_effects = [None] * (OCTOPUS_MAX_RETRIES - 1) + [successful_data]
    api.async_read_response = AsyncMock(side_effect=side_effects)

    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, url, ignore_errors=False)

        if result != successful_data:
            print(f"ERROR: Expected successful data, got {result}")
            failed = True
        elif api.async_read_response.call_count != OCTOPUS_MAX_RETRIES:
            print(f"ERROR: Expected {OCTOPUS_MAX_RETRIES} calls, got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != (OCTOPUS_MAX_RETRIES - 1):
            print(f"ERROR: Expected {OCTOPUS_MAX_RETRIES - 1} sleep calls, got {mock_sleep.call_count}")
            failed = True
        elif api.failures_total != 0:
            print(f"ERROR: failures_total should be 0 after eventual success, got {api.failures_total}")
            failed = True
        else:
            print(f"PASS: Successful response on last attempt after {OCTOPUS_MAX_RETRIES - 1} retries")

    # Test 4: All retries fail, returns None and increments failures_total
    print("\n*** Test 4: All retries fail, returns None and increments failures_total ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    response = create_mock_response(500, "Internal Server Error")

    # Mock async_read_response to always fail
    api.async_read_response = AsyncMock(return_value=None)

    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, url, ignore_errors=False)

        if result is not None:
            print(f"ERROR: Expected None after all retries fail, got {result}")
            failed = True
        elif api.async_read_response.call_count != OCTOPUS_MAX_RETRIES:
            print(f"ERROR: Expected {OCTOPUS_MAX_RETRIES} calls, got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != (OCTOPUS_MAX_RETRIES - 1):
            print(f"ERROR: Expected {OCTOPUS_MAX_RETRIES - 1} sleep calls, got {mock_sleep.call_count}")
            failed = True
        elif api.failures_total != 1:
            print(f"ERROR: failures_total should be 1 after all retries fail, got {api.failures_total}")
            failed = True
        else:
            print(f"PASS: All {OCTOPUS_MAX_RETRIES} retries failed, returns None and increments failures_total")

    # Test 5: Exponential backoff timing is correct
    print("\n*** Test 5: Exponential backoff timing is correct ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    response = create_mock_response(500, "Internal Server Error")

    # Mock async_read_response to always fail
    api.async_read_response = AsyncMock(return_value=None)

    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, url, ignore_errors=False)

        # Expected backoff: 2^0=1, 2^1=2, 2^2=4, 2^3=8 seconds (for 5 attempts)
        expected_sleeps = [2 ** i for i in range(OCTOPUS_MAX_RETRIES - 1)]
        actual_sleeps = [call[0][0] for call in mock_sleep.call_args_list]

        if actual_sleeps != expected_sleeps:
            print(f"ERROR: Expected exponential backoff {expected_sleeps}, got {actual_sleeps}")
            failed = True
        else:
            print(f"PASS: Exponential backoff timing correct: {actual_sleeps} seconds")

    # Test 6: ignore_errors parameter is passed through correctly
    print("\n*** Test 6: ignore_errors parameter is passed through correctly ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    successful_data = {"data": {"result": "success"}}
    response = create_mock_response(200, json.dumps(successful_data))

    # Mock async_read_response to verify ignore_errors is passed
    api.async_read_response = AsyncMock(return_value=successful_data)

    result = await api.async_read_response_retry(response, url, ignore_errors=True)

    if api.async_read_response.call_count != 1:
        print(f"ERROR: Expected 1 call to async_read_response")
        failed = True
    else:
        call_kwargs = api.async_read_response.call_args[1]
        if 'ignore_errors' not in call_kwargs or call_kwargs['ignore_errors'] != True:
            print(f"ERROR: ignore_errors=True not passed through correctly")
            failed = True
        else:
            print("PASS: ignore_errors parameter passed through correctly")

    # Test 7: Rate limit errors trigger retry with backoff
    print("\n*** Test 7: Rate limit errors trigger retry with backoff ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    rate_limit_error = {
        "errors": [
            {
                "message": "Too many requests.",
                "extensions": {"errorCode": "KT-CT-1199"},
            }
        ]
    }
    successful_data = {"data": {"result": "success"}}
    response = create_mock_response(200, json.dumps(rate_limit_error))

    # Mock async_read_response to return None (rate limit), then succeed
    api.async_read_response = AsyncMock(side_effect=[None, successful_data])

    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, url, ignore_errors=False)

        if result != successful_data:
            print(f"ERROR: Expected successful data after rate limit retry, got {result}")
            failed = True
        elif api.async_read_response.call_count != 2:
            print(f"ERROR: Expected 2 calls (rate limit + retry), got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != 1:
            print(f"ERROR: Expected 1 sleep call after rate limit, got {mock_sleep.call_count}")
            failed = True
        else:
            print("PASS: Rate limit error triggers retry with exponential backoff")

    # Test 8: Auth errors trigger retry with backoff
    print("\n*** Test 8: Auth errors trigger retry with backoff ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    auth_error_response = create_mock_response(401, "Unauthorized")
    successful_data = {"data": {"result": "success"}}

    # Mock async_read_response to return None (auth error), then succeed
    api.async_read_response = AsyncMock(side_effect=[None, successful_data])

    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(auth_error_response, url, ignore_errors=False)

        if result != successful_data:
            print(f"ERROR: Expected successful data after auth error retry, got {result}")
            failed = True
        elif api.async_read_response.call_count != 2:
            print(f"ERROR: Expected 2 calls (auth error + retry), got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != 1:
            print(f"ERROR: Expected 1 sleep call after auth error, got {mock_sleep.call_count}")
            failed = True
        else:
            print("PASS: Auth error triggers retry with exponential backoff")

    if failed:
        print("\n**** ❌ Octopus async_read_response_retry tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ All Octopus async_read_response_retry tests PASSED ****")
        return 0
