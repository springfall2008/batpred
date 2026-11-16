"""
Tests for Octopus async_read_response_retry function
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from octopus import OctopusAPI


def test_octopus_read_response_retry_wrapper(my_predbat):
    return asyncio.run(test_octopus_read_response_retry(my_predbat))


async def test_octopus_read_response_retry(my_predbat):
    """
    Test OctopusAPI async_read_response_retry, which reads a response once.

    Tests:
    - Test 1: Successful response on first attempt (no retry)
    - Test 2: A failed read is read once, without sleeping
    - Test 3: HTTP 429 sets last_read_rate_limited
    - Test 6: ignore_errors parameter is passed through correctly
    - Test 7: A rate limit is not re-read or slept on
    - Test 8: Auth errors are not re-read
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

    # Test 2: A failed read is not re-read. aiohttp caches the body, so a second read of the same
    # response returns the same failure - the old loop only stalled ~15s and repeated the warning,
    # even when (as mocked here) a later read could have been made to succeed.
    print("\n*** Test 2: A failed read is read once, without sleeping ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0
    response = create_mock_response(500, "Internal Server Error")
    api.async_read_response = AsyncMock(side_effect=[None, {"data": {"result": "success"}}])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, url)

        if result is not None:
            print(f"ERROR: Expected None for a failed read, got {result}")
            failed = True
        elif api.async_read_response.call_count != 1:
            print(f"ERROR: Expected the response to be read once, got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != 0:
            print(f"ERROR: Expected no sleep, got {mock_sleep.call_count}")
            failed = True
        elif api.failures_total != 1:
            print(f"ERROR: failures_total should be 1, got {api.failures_total}")
            failed = True
        else:
            print("PASS: Failed read returned at once and counted one failure")

    # Test 3: An HTTP 429 is reported as a rate limit, so the caller backs off
    print("\n*** Test 3: HTTP 429 sets last_read_rate_limited ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    logged = []
    api.log = lambda message, *args, **kwargs: logged.append(message)
    response = create_mock_response(429, "Too Many Requests")
    result = await api.async_read_response_retry(response, "https://api.octopus.energy/v1/graphql/")

    if result is not None or not api.last_read_rate_limited:
        print(f"ERROR: Expected None with last_read_rate_limited set, got {result} / {api.last_read_rate_limited}")
        failed = True
    elif not any("Rate limited: 429" in message for message in logged):
        print(f"ERROR: Expected the 429 to be logged as a rate limit, got {logged}")
        failed = True
    else:
        print("PASS: HTTP 429 reported as a rate limit")

    # A read that is not rate limited clears a flag left over from an earlier one
    api.async_read_response = AsyncMock(return_value={"data": {}})
    await api.async_read_response_retry(create_mock_response(200, "{}"), url)
    if api.last_read_rate_limited:
        print("ERROR: last_read_rate_limited was not cleared by the next read")
        failed = True
    else:
        print("PASS: last_read_rate_limited cleared by the next read")

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
        if "ignore_errors" not in call_kwargs or call_kwargs["ignore_errors"] is not True:
            print(f"ERROR: ignore_errors=True not passed through correctly")
            failed = True
        else:
            print("PASS: ignore_errors parameter passed through correctly")

    # Test 7: A rate limit (KT-CT-1199) is definitive for the response and is not re-read. aiohttp
    # caches the body, so every re-read returned the same rate limit: a single rate-limited request
    # logged the warning five times and stalled for ~35s (5s + 1/2/4/8s) before giving up anyway.
    print("\n*** Test 7: Rate limit is not re-read or slept on ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0
    logged = []
    api.log = lambda message, *args, **kwargs: logged.append(message)

    rate_limit_error = {
        "errors": [
            {
                "message": "Too many requests.",
                "path": ["flexPlannedDispatches"],
                "extensions": {"errorCode": "KT-CT-1199"},
            }
        ]
    }
    response = create_mock_response(200, json.dumps(rate_limit_error))
    graphql_url = "https://api.octopus.energy/v1/graphql/"

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(response, graphql_url, ignore_errors=True)

        rate_limit_logs = [message for message in logged if "Rate limit error" in message]
        if result is not None:
            print(f"ERROR: Expected None for a rate-limited response, got {result}")
            failed = True
        elif response.text.await_count != 1:
            print(f"ERROR: Expected the rate-limited response to be read once, got {response.text.await_count}")
            failed = True
        elif mock_sleep.call_count != 0:
            print(f"ERROR: Expected no sleep on a rate limit, got {mock_sleep.call_count} sleep calls")
            failed = True
        elif len(rate_limit_logs) != 1:
            print(f"ERROR: Expected one rate limit warning, got {len(rate_limit_logs)}: {rate_limit_logs}")
            failed = True
        elif api.failures_total != 1:
            print(f"ERROR: failures_total should be 1, got {api.failures_total}")
            failed = True
        else:
            print("PASS: Rate limit returns at once, read once, no sleep, one warning")

    # Test 8: Auth/blocked responses are definitive and are not re-read.
    # aiohttp caches the response body, so re-reading the same 401/403 response can only
    # ever yield the same result. Retrying it just duplicates log lines and sleeps through
    # the backoff - which is what made a CloudFront 403 emit five identical warnings and
    # stall for 15s on every query.
    print("\n*** Test 8: Auth/blocked responses are not re-read ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.failures_total = 0

    auth_error_response = create_mock_response(401, "Unauthorized")

    # A real 401 body cannot change between reads, so async_read_response always returns None
    api.async_read_response = AsyncMock(return_value=None)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await api.async_read_response_retry(auth_error_response, url, ignore_errors=False)

        if result is not None:
            print(f"ERROR: Expected None for an auth error, got {result}")
            failed = True
        elif api.async_read_response.call_count != 1:
            print(f"ERROR: Expected 1 call (no retry on a definitive auth error), got {api.async_read_response.call_count}")
            failed = True
        elif mock_sleep.call_count != 0:
            print(f"ERROR: Expected no sleep for a definitive auth error, got {mock_sleep.call_count}")
            failed = True
        elif api.failures_total != 1:
            print(f"ERROR: failures_total should be 1 after an auth error, got {api.failures_total}")
            failed = True
        else:
            print("PASS: Auth error returns immediately without pointless retries")

    if failed:
        print("\n**** ❌ Octopus async_read_response_retry tests FAILED ****")
        return 1
    else:
        print("\n**** ✅ All Octopus async_read_response_retry tests PASSED ****")
        return 0
