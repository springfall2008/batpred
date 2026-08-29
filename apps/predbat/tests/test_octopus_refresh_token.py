"""
Tests for Octopus async_refresh_token function
"""

import asyncio
import base64
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from octopus import OctopusAPI, TOKEN_MINT_BACKOFF_MAX_SECONDS


def test_octopus_refresh_token_wrapper(my_predbat):
    return asyncio.run(test_octopus_refresh_token(my_predbat))


async def test_octopus_refresh_token(my_predbat):
    """
    Test OctopusAPI async_refresh_token method.

    Tests:
    - Test 1: Token refresh when no existing token
    - Test 2: Token refresh when existing token expired
    - Test 3: Token reuse when existing token still valid
    - Test 4: Token refresh saves to cache
    - Test 5: Token refresh handles API failure gracefully
    - Test 6: Token refresh handles timeout gracefully
    - Test 7: Token expiry decoding from JWT
    - Test 9: A CDN/WAF 403 on the mint backs off instead of re-minting every poll
    - Test 10: No HTTP request is made at all while the mint backoff is active
    - Test 11: Backoff grows per block, is capped, and a success clears it
    - Test 12: A non-CDN 403 does not start a backoff
    """
    print("**** Running Octopus async_refresh_token tests ****")
    failed = False

    # Helper function to create a mock JWT token
    def create_mock_jwt_token(expiry_time):
        """Create a mock JWT token with specified expiry time"""
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
        payload_data = {"exp": int(expiry_time.timestamp()), "user_id": "test-user"}
        payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
        signature = "mock_signature"
        return f"{header}.{payload}.{signature}"

    # Test 1: Token refresh when no existing token
    print("\n*** Test 1: Token refresh when no existing token ***")
    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)
    api.graphql_token = None

    # Create mock response
    new_token_expiry = datetime.now() + timedelta(hours=1)
    new_token = create_mock_jwt_token(new_token_expiry)

    mock_response_body = {"data": {"obtainKrakenToken": {"token": new_token}}}

    # Mock the async methods
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_session.post = MagicMock(return_value=mock_response)

    api.api.async_create_client_session = AsyncMock(return_value=mock_session)
    api.async_read_response = AsyncMock(return_value=mock_response_body)
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result != new_token:
        print(f"ERROR: Expected token {new_token}, got {result}")
        failed = True
    elif api.graphql_token != new_token:
        print(f"ERROR: Token not stored in api.graphql_token")
        failed = True
    elif api.save_octopus_cache.call_count != 1:
        print(f"ERROR: save_octopus_cache should be called once, was called {api.save_octopus_cache.call_count} times")
        failed = True
    else:
        print("PASS: Token refreshed successfully when no existing token")

    # Test 2: Token refresh when existing token expired
    print("\n*** Test 2: Token refresh when existing token expired ***")
    api = OctopusAPI(my_predbat, key="test-api-key-2", account_id="test-account-2", automatic=False)

    # Create expired token (expired 10 minutes ago)
    old_token_expiry = datetime.now() - timedelta(minutes=10)
    old_token = create_mock_jwt_token(old_token_expiry)
    api.graphql_token = old_token

    # Create new token
    new_token_expiry = datetime.now() + timedelta(hours=2)
    new_token = create_mock_jwt_token(new_token_expiry)

    mock_response_body = {"data": {"obtainKrakenToken": {"token": new_token}}}

    api.api.async_create_client_session = AsyncMock(return_value=mock_session)
    api.async_read_response = AsyncMock(return_value=mock_response_body)
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result != new_token:
        print(f"ERROR: Expected new token, got {result}")
        failed = True
    elif api.graphql_token != new_token:
        print(f"ERROR: Token not updated in api.graphql_token")
        failed = True
    else:
        print("PASS: Expired token refreshed successfully")

    # Test 3: Token reuse when existing token still valid
    print("\n*** Test 3: Token reuse when existing token still valid ***")
    api = OctopusAPI(my_predbat, key="test-api-key-3", account_id="test-account-3", automatic=False)

    # Create token that expires in 10 minutes (should be reused, as threshold is 5 minutes)
    valid_token_expiry = datetime.now() + timedelta(minutes=10)
    valid_token = create_mock_jwt_token(valid_token_expiry)
    api.graphql_token = valid_token

    # Mock methods (should not be called)
    api.api.async_create_client_session = AsyncMock()
    api.async_read_response_retry = AsyncMock()
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result != valid_token:
        print(f"ERROR: Expected valid token to be reused, got different token")
        failed = True
    elif api.api.async_create_client_session.call_count != 0:
        print(f"ERROR: API should not be called when token is still valid")
        failed = True
    elif api.save_octopus_cache.call_count != 0:
        print(f"ERROR: Cache should not be saved when token is reused")
        failed = True
    else:
        print("PASS: Valid token reused without API call")

    # Test 4: Token near expiry triggers refresh
    print("\n*** Test 4: Token near expiry triggers refresh ***")
    api = OctopusAPI(my_predbat, key="test-api-key-4", account_id="test-account-4", automatic=False)

    # Create token that expires in 3 minutes (below 5 minute threshold)
    near_expiry_token_time = datetime.now() + timedelta(minutes=3)
    near_expiry_token = create_mock_jwt_token(near_expiry_token_time)
    api.graphql_token = near_expiry_token

    # Create new token
    new_token_expiry = datetime.now() + timedelta(hours=1)
    new_token = create_mock_jwt_token(new_token_expiry)

    mock_response_body = {"data": {"obtainKrakenToken": {"token": new_token}}}

    api.api.async_create_client_session = AsyncMock(return_value=mock_session)
    api.async_read_response_retry = AsyncMock(return_value=mock_response_body)
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result != new_token:
        print(f"ERROR: Expected new token for near-expiry case, got {result}")
        failed = True
    elif api.api.async_create_client_session.call_count != 1:
        print(f"ERROR: API should be called when token near expiry")
        failed = True
    else:
        print("PASS: Token refreshed when near expiry (< 5 minutes)")

    # Test 5: Token refresh handles API failure gracefully
    print("\n*** Test 5: Token refresh handles API failure gracefully ***")
    api = OctopusAPI(my_predbat, key="test-api-key-5", account_id="test-account-5", automatic=False)
    api.graphql_token = None

    # Mock API returning invalid response
    mock_response_body = {"data": {"obtainKrakenToken": None}}  # Invalid response

    api.api.async_create_client_session = AsyncMock(return_value=mock_session)
    api.async_read_response_retry = AsyncMock(return_value=mock_response_body)
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result is not None:
        print(f"ERROR: Expected None for failed API call, got {result}")
        failed = True
    elif api.graphql_token is not None:
        print(f"ERROR: Token should remain None after failed refresh")
        failed = True
    elif api.save_octopus_cache.call_count != 0:
        print(f"ERROR: Cache should not be saved on failed refresh")
        failed = True
    else:
        print("PASS: API failure handled gracefully, returns None")

    # Test 6: Token refresh handles timeout gracefully
    print("\n*** Test 6: Token refresh handles timeout gracefully ***")
    api = OctopusAPI(my_predbat, key="test-api-key-6", account_id="test-account-6", automatic=False)
    api.graphql_token = None
    api.api.timeout = 30

    # Mock timeout error from within the context manager
    mock_timeout_session = MagicMock()
    mock_timeout_response = MagicMock()

    async def raise_timeout(*args, **kwargs):
        raise TimeoutError("Connection timeout")

    mock_timeout_response.__aenter__ = raise_timeout
    mock_timeout_response.__aexit__ = AsyncMock(return_value=None)
    mock_timeout_session.post = MagicMock(return_value=mock_timeout_response)

    api.api.async_create_client_session = AsyncMock(return_value=mock_timeout_session)
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result is not None:
        print(f"ERROR: Expected None for timeout, got {result}")
        failed = True
    elif api.save_octopus_cache.call_count != 0:
        print(f"ERROR: Cache should not be saved on timeout")
        failed = True
    else:
        print("PASS: Timeout handled gracefully, returns None")

    # Test 7: Token expiry decoding from JWT
    print("\n*** Test 7: Token expiry decoding from JWT ***")
    api = OctopusAPI(my_predbat, key="test-api-key-7", account_id="test-account-7", automatic=False)

    # Create token with known expiry
    test_expiry = datetime.now() + timedelta(hours=5)
    test_token = create_mock_jwt_token(test_expiry)

    decoded_expiry = api.decode_kraken_token_expiry(test_token)

    if decoded_expiry is None:
        print(f"ERROR: Failed to decode token expiry")
        failed = True
    elif abs((decoded_expiry - test_expiry).total_seconds()) > 1:
        print(f"ERROR: Decoded expiry doesn't match expected. Got {decoded_expiry}, expected {test_expiry}")
        failed = True
    else:
        print("PASS: Token expiry decoded correctly from JWT")

    # Test 8: Invalid token format handled gracefully
    print("\n*** Test 8: Invalid token format handled gracefully ***")
    api = OctopusAPI(my_predbat, key="test-api-key-8", account_id="test-account-8", automatic=False)

    invalid_tokens = [None, "", "invalid.token", "not.a.valid.jwt.token", "invalid_format"]

    for invalid_token in invalid_tokens:
        decoded_expiry = api.decode_kraken_token_expiry(invalid_token)
        if decoded_expiry is not None:
            print(f"ERROR: Expected None for invalid token '{invalid_token}', got {decoded_expiry}")
            failed = True
            break
    else:
        print("PASS: Invalid token formats handled gracefully")

    # Helper to build a mock session whose response carries a given status/body
    def create_mock_session(status, body_text):
        """Build a mock client session returning one response with this status and body"""
        session = MagicMock()
        response = MagicMock()
        response.status = status
        response.text = AsyncMock(return_value=body_text)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(return_value=response)
        return session

    cloudfront_body = (
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">\n'
        "<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE></HEAD><BODY>\n"
        "<H1>403 ERROR</H1><H2>The request could not be satisfied.</H2>\n"
        "Request blocked.\n"
        "Generated by cloudfront (CloudFront)\n"
        "</BODY></HTML>"
    )

    # Test 9: A CDN/WAF 403 on the mint backs off instead of re-minting every poll
    print("\n*** Test 9: CDN/WAF 403 on the token mint starts a backoff ***")
    api = OctopusAPI(my_predbat, key="test-api-key-9", account_id="test-account-9", automatic=False)
    api.graphql_token = None

    api.api.async_create_client_session = AsyncMock(return_value=create_mock_session(403, cloudfront_body))
    api.async_read_response_retry = AsyncMock(return_value=None)
    api.save_octopus_cache = AsyncMock()

    result = await api.async_refresh_token()

    if result is not None:
        print(f"ERROR: Expected None for an edge-blocked mint, got {result}")
        failed = True
    elif api.token_mint_blocked_until is None or api.token_mint_blocked_until <= datetime.now():
        print(f"ERROR: Expected a future mint backoff deadline, got {api.token_mint_blocked_until}")
        failed = True
    elif api.token_mint_block_count != 1:
        print(f"ERROR: Expected block count 1, got {api.token_mint_block_count}")
        failed = True
    elif api.async_read_response_retry.call_count != 0:
        print("ERROR: An edge block should short-circuit before the generic response reader")
        failed = True
    else:
        print("PASS: CDN/WAF 403 on the mint starts a backoff")

    # Test 10: No HTTP request is made at all while the mint backoff is active
    print("\n*** Test 10: Mint backoff suppresses the request entirely ***")
    api.api.async_create_client_session = AsyncMock(return_value=create_mock_session(403, cloudfront_body))

    result = await api.async_refresh_token()

    if result is not None:
        print(f"ERROR: Expected None while backing off, got {result}")
        failed = True
    elif api.api.async_create_client_session.call_count != 0:
        print(f"ERROR: Expected no mint attempt while backing off, got {api.api.async_create_client_session.call_count}")
        failed = True
    elif api.token_mint_block_count != 1:
        print(f"ERROR: A suppressed attempt must not grow the backoff, got {api.token_mint_block_count}")
        failed = True
    else:
        print("PASS: No mint request is made while the backoff is active")

    # Test 11: Backoff grows per block, is capped, and a success clears it
    print("\n*** Test 11: Backoff grows, caps, and clears on success ***")
    api = OctopusAPI(my_predbat, key="test-api-key-11", account_id="test-account-11", automatic=False)
    api.graphql_token = None
    api.save_octopus_cache = AsyncMock()
    api.async_read_response_retry = AsyncMock(return_value=None)

    delays = []
    for _ in range(8):
        api.token_mint_blocked_until = None  # let each attempt through to measure its own delay
        api.api.async_create_client_session = AsyncMock(return_value=create_mock_session(403, cloudfront_body))
        before = datetime.now()
        await api.async_refresh_token()
        delays.append((api.token_mint_blocked_until - before).total_seconds())

    # Measured off datetime.now(), so allow a second of jitter when comparing neighbours
    if any(delays[i] > delays[i + 1] + 1 for i in range(len(delays) - 1)):
        print(f"ERROR: Expected non-decreasing backoff delays, got {delays}")
        failed = True
    elif delays[0] >= delays[1]:
        print(f"ERROR: Expected the backoff to grow between blocks, got {delays}")
        failed = True
    elif delays[-1] > TOKEN_MINT_BACKOFF_MAX_SECONDS + 1:
        print(f"ERROR: Backoff exceeded the cap of {TOKEN_MINT_BACKOFF_MAX_SECONDS}s, got {delays[-1]}")
        failed = True
    else:
        # A successful mint must clear the backoff so the next expiry refreshes normally
        good_token = create_mock_jwt_token(datetime.now() + timedelta(hours=1))
        api.token_mint_blocked_until = None
        api.api.async_create_client_session = AsyncMock(return_value=create_mock_session(200, "{}"))
        api.async_read_response_retry = AsyncMock(return_value={"data": {"obtainKrakenToken": {"token": good_token}}})

        result = await api.async_refresh_token()

        if result != good_token:
            print(f"ERROR: Expected the mint to succeed after the block cleared, got {result}")
            failed = True
        elif api.token_mint_blocked_until is not None or api.token_mint_block_count != 0:
            print(f"ERROR: A successful mint must clear the backoff, got {api.token_mint_blocked_until} / {api.token_mint_block_count}")
            failed = True
        else:
            print(f"PASS: Backoff grows {delays[:3]}... capped at {TOKEN_MINT_BACKOFF_MAX_SECONDS}s and clears on success")

    # Test 12: A non-CDN 403 does not start a backoff
    print("\n*** Test 12: A non-CDN 403 does not start a backoff ***")
    api = OctopusAPI(my_predbat, key="test-api-key-12", account_id="test-account-12", automatic=False)
    api.graphql_token = None
    api.save_octopus_cache = AsyncMock()

    api.api.async_create_client_session = AsyncMock(return_value=create_mock_session(403, json.dumps({"errors": [{"message": "Forbidden"}]})))
    api.async_read_response_retry = AsyncMock(return_value=None)

    result = await api.async_refresh_token()

    if result is not None:
        print(f"ERROR: Expected None for a forbidden mint, got {result}")
        failed = True
    elif api.token_mint_blocked_until is not None:
        print("ERROR: A JSON 403 is a credential problem, not an edge block - it must not start a backoff")
        failed = True
    elif api.async_read_response_retry.call_count != 1:
        print("ERROR: A non-CDN 403 should still go through the normal response reader")
        failed = True
    else:
        print("PASS: A non-CDN 403 keeps the existing refresh behaviour")

    # Summary
    if failed:
        print("\n**** Octopus async_refresh_token tests FAILED ****")
        raise Exception("Octopus async_refresh_token tests failed")
    else:
        print("\n**** All Octopus async_refresh_token tests PASSED ****")

    return failed
