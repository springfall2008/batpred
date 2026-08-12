"""
Tests for Octopus API GraphQL request/response logging.

Verifies that the JWT auth token is never written to the log while the
GraphQL response body is logged for diagnostics.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from octopus import OctopusAPI


def test_octopus_logging_wrapper(my_predbat):
    """Synchronous entry point that runs the async logging test."""
    return asyncio.run(test_octopus_logging(my_predbat))


async def test_octopus_logging(my_predbat):
    """
    Test that async_graphql_query redacts the JWT token but logs the response.

    Tests:
    - The secret JWT token never appears in any log line
    - The redaction marker "<redacted>" is logged in its place
    - The GraphQL response body is logged
    """
    print("**** Running Octopus API logging tests ****")
    failed = False

    secret_token = "super-secret-jwt-token-value-123"
    response_body = {"data": {"account": {"accountNumber": "A-LOG-12345"}}}

    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api.graphql_token = secret_token

    # Capture every log line emitted by the API
    log_lines = []
    api.log = lambda message: log_lines.append(str(message))

    # Token refresh returns the existing (valid) token without hitting the network
    api.async_refresh_token = AsyncMock(return_value=secret_token)

    # Mock the HTTP client and POST response
    mock_client = AsyncMock()
    api.api.async_create_client_session = AsyncMock(return_value=mock_client)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = MagicMock(return_value=mock_response)

    # Return a known response body so we can assert it was logged
    api.async_read_response_retry = AsyncMock(return_value=response_body)

    result = await api.async_graphql_query("query { account }", "test-logging", returns_data=True)

    if result != response_body["data"]:
        print(f"ERROR: Expected response data {response_body['data']}, got {result}")
        failed = True

    all_logs = "\n".join(log_lines)

    # The secret token must never appear in the logs
    if secret_token in all_logs:
        print("ERROR: JWT token was leaked into the log output")
        failed = True
    else:
        print("PASS: JWT token not present in log output")

    # The redaction marker should be logged in place of the token
    if "<redacted>" not in all_logs:
        print("ERROR: Expected '<redacted>' marker in request log")
        failed = True
    else:
        print("PASS: Authorization header redacted in request log")

    # The response body should be logged
    if "A-LOG-12345" not in all_logs:
        print("ERROR: GraphQL response body was not logged")
        failed = True
    else:
        print("PASS: GraphQL response body logged")

    return failed
