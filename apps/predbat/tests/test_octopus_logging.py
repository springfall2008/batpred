"""
Tests for Octopus API GraphQL request/response logging.

Verifies that the JWT auth token is never written to the log while the
GraphQL response body is logged for diagnostics.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from const import REPEAT_FULL_LOG_SECONDS
from octopus import OctopusAPI
from utils import RepeatLogGate


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

    # A repeat of the same query and response is logged briefly, without the query or body
    log_lines.clear()
    await api.async_graphql_query("query { account }", "test-logging", returns_data=True)
    repeat = "\n".join(log_lines)
    brief_request = "Making GraphQL request test-logging to " in repeat and "(query as logged in full at" in repeat
    brief_response = "GraphQL response for test-logging (status 200) unchanged since logged in full" in repeat
    if "A-LOG-12345" in repeat or "query { account }" in repeat or not brief_request or not brief_response:
        print("ERROR: a repeated query and response should be logged briefly, got {}".format(log_lines))
        failed = True
    else:
        print("PASS: repeated query and response logged briefly")

    # A changed response is logged in full again
    log_lines.clear()
    api.async_read_response_retry = AsyncMock(return_value={"data": {"account": {"accountNumber": "A-LOG-67890"}}})
    await api.async_graphql_query("query { account }", "test-logging", returns_data=True)
    if "A-LOG-67890" not in "\n".join(log_lines):
        print("ERROR: a changed response should be logged in full, got {}".format(log_lines))
        failed = True

    # An unchanged response is logged in full again once REPEAT_FULL_LOG_SECONDS have passed
    for entry, (logged_at, wall) in list(api.log_gate.logged.items()):
        api.log_gate.logged[entry] = (logged_at - REPEAT_FULL_LOG_SECONDS - 1, wall)
    log_lines.clear()
    await api.async_graphql_query("query { account }", "test-logging", returns_data=True)
    logged_again = "\n".join(log_lines)
    if "A-LOG-67890" not in logged_again or "query { account }" not in logged_again:
        print("ERROR: an unchanged response should be logged in full again after an hour, got {}".format(log_lines))
        failed = True

    # Two queries sharing one request context (one per device) are each logged briefly when they repeat
    api.log_gate = RepeatLogGate(REPEAT_FULL_LOG_SECONDS)
    api.async_read_response_retry = AsyncMock(return_value=response_body)
    await api.async_graphql_query("query { device(id: 1) }", "test-device", returns_data=True)
    await api.async_graphql_query("query { device(id: 2) }", "test-device", returns_data=True)
    log_lines.clear()
    await api.async_graphql_query("query { device(id: 1) }", "test-device", returns_data=True)
    if not any("(query as logged in full at" in line for line in log_lines) or not any("unchanged since logged in full" in line for line in log_lines):
        print("ERROR: a repeated query should be logged briefly even when another query shares its context, got {}".format(log_lines))
        failed = True

    # An error response is always logged in full, even when it repeats
    api.async_read_response_retry = AsyncMock(return_value={"errors": [{"message": "Something failed", "extensions": {"errorCode": "KT-CT-9999"}}]})
    await api.async_graphql_query("query { broken }", "test-error", returns_data=True, ignore_errors=True)
    log_lines.clear()
    await api.async_graphql_query("query { broken }", "test-error", returns_data=True, ignore_errors=True)
    if not any("Something failed" in line for line in log_lines):
        print("ERROR: a repeated error response should still be logged in full, got {}".format(log_lines))
        failed = True
    if not any("query { broken }" in line for line in log_lines):
        print("ERROR: the request behind a repeated error should be logged in full beside it, got {}".format(log_lines))
        failed = True

    # The token must not leak through any of the later lines either
    if secret_token in logged_again or secret_token in repeat:
        print("ERROR: JWT token leaked on a repeat or re-log")
        failed = True

    failed |= test_repeat_log_gate()
    return failed


def test_repeat_log_gate():
    """RepeatLogGate logs a text in full when new, briefly while it repeats, alternating texts each keep their own
    entry, always_full forces a full log, and entries older than the interval are dropped."""
    failed = False
    gate = RepeatLogGate(3600)
    if gate.last_full("key", "a") is not None:
        print("ERROR: a new text should be logged in full")
        failed = True
    if gate.last_full("key", "a") is None:
        print("ERROR: a repeated text should be logged briefly")
        failed = True
    if gate.last_full("key", "b") is not None or gate.last_full("key", "a") is None:
        print("ERROR: a second text under the same key should not make the first one log in full again")
        failed = True
    if gate.last_full("other", "a") is not None:
        print("ERROR: the same text under another key is new")
        failed = True
    if gate.last_full("key", "a", always_full=True) is not None:
        print("ERROR: always_full should log in full")
        failed = True
    for entry, (logged_at, wall) in list(gate.logged.items()):
        gate.logged[entry] = (logged_at - 3601, wall)
    if gate.last_full("key", "a") is not None or len(gate.logged) != 1:
        print("ERROR: entries older than the interval should be dropped, leaving {}".format(gate.logged))
        failed = True
    if gate.last_full("key", None) is not None or gate.last_full("key", None) is None:
        print("ERROR: a None text should be gated like any other")
        failed = True
    lines = []
    if not gate.log(lines.append, "lines", "x", ["one", "two"], lambda when: "brief " + when) or gate.log(lines.append, "lines", "x", ["one"], lambda when: "brief " + when):
        print("ERROR: log() should report a full log, then a brief one")
        failed = True
    if lines[:2] != ["one", "two"] or not lines[2].startswith("brief "):
        print("ERROR: log() should write every full line, then the brief line, got {}".format(lines))
        failed = True
    return failed
