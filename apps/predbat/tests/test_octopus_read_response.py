"""
Tests for Octopus async_read_response function
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from octopus import OctopusAPI, integration_context_header


def test_octopus_read_response_wrapper(my_predbat):
    return asyncio.run(test_octopus_read_response(my_predbat))


async def test_octopus_read_response(my_predbat):
    """
    Test OctopusAPI async_read_response method.

    Tests:
    - Test 1: Successful response with valid JSON (status 200)
    - Test 2: Server error response (status 500+)
    - Test 3: Unauthenticated response (status 401)
    - Test 4: Forbidden response (status 403)
    - Test 5: Not found response (status 404)
    - Test 6: Other client error (status 400)
    - Test 7: Invalid JSON in response body
    - Test 8: GraphQL errors with ignore_errors=False
    - Test 9: GraphQL errors with ignore_errors=True
    - Test 10: GraphQL auth token errors (KT-CT-1139, KT-CT-1111, KT-CT-1143)
    - Test 11: Response with integration context header
    - Test 12: Response without integration context header
    """
    print("**** Running Octopus async_read_response tests ****")
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

    # Test 1: Successful response with valid JSON (status 200)
    print("\n*** Test 1: Successful response with valid JSON ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"data": {"key": "value"}, "status": "success"}
    response = create_mock_response(200, json.dumps(response_data))

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result != response_data:
        print(f"ERROR: Expected {response_data}, got {result}")
        failed = True
    else:
        print("PASS: Valid JSON response parsed correctly")

    # Test 2: Server error response (status 500+)
    print("\n*** Test 2: Server error response (status 500+) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(500, "Internal Server Error")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for server error, got {result}")
        failed = True
    else:
        print("PASS: Server error (500) handled correctly, returns None")

    # Test 3: Unauthenticated response (status 401)
    print("\n*** Test 3: Unauthenticated response (status 401) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(401, "Unauthorized")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for 401 error, got {result}")
        failed = True
    else:
        print("PASS: Unauthenticated error (401) handled correctly, returns None")

    # Test 4: Forbidden response (status 403)
    print("\n*** Test 4: Forbidden response (status 403) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(403, "Forbidden")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for 403 error, got {result}")
        failed = True
    else:
        print("PASS: Forbidden error (403) handled correctly, returns None")

    # Test 5: Not found response (status 404)
    print("\n*** Test 5: Not found response (status 404) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(404, "Not Found")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for 404 error, got {result}")
        failed = True
    else:
        print("PASS: Not found error (404) handled correctly, returns None")

    # Test 6: Other client error (status 400)
    print("\n*** Test 6: Other client error (status 400) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(400, "Bad Request")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for 400 error, got {result}")
        failed = True
    else:
        print("PASS: Bad request error (400) handled correctly, returns None")

    # Test 7: Invalid JSON in response body
    print("\n*** Test 7: Invalid JSON in response body ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(200, "This is not valid JSON {]}")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for invalid JSON, got {result}")
        failed = True
    else:
        print("PASS: Invalid JSON handled gracefully, returns None")

    # Test 8: GraphQL errors with ignore_errors=False
    print("\n*** Test 8: GraphQL errors with ignore_errors=False ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"errors": [{"message": "Field error", "extensions": {"errorCode": "SOME-ERROR"}}]}
    response = create_mock_response(200, json.dumps(response_data))

    result = await api.async_read_response(response, "https://api.octopus.energy/v1/graphql/", ignore_errors=False)

    if result is not None:
        print(f"ERROR: Expected None for GraphQL errors, got {result}")
        failed = True
    else:
        print("PASS: GraphQL errors with ignore_errors=False returns None")

    # Test 9: GraphQL errors with ignore_errors=True
    print("\n*** Test 9: GraphQL errors with ignore_errors=True ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"errors": [{"message": "Field error", "extensions": {"errorCode": "SOME-ERROR"}}], "data": {"someField": "value"}}
    response = create_mock_response(200, json.dumps(response_data))

    result = await api.async_read_response(response, "https://api.octopus.energy/v1/graphql/", ignore_errors=True)

    if result != response_data:
        print(f"ERROR: Expected data to be returned when ignore_errors=True, got {result}")
        failed = True
    else:
        print("PASS: GraphQL errors with ignore_errors=True returns data")

    # Test 10: GraphQL auth token errors
    print("\n*** Test 10: GraphQL auth token errors ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    for error_code in ["KT-CT-1139", "KT-CT-1111", "KT-CT-1143"]:
        response_data = {"errors": [{"message": "Token expired", "extensions": {"errorCode": error_code}}]}
        response = create_mock_response(200, json.dumps(response_data))

        result = await api.async_read_response(response, "https://api.octopus.energy/v1/graphql/", ignore_errors=False)

        if result is not None:
            print(f"ERROR: Expected None for auth token error {error_code}, got {result}")
            failed = True
            break
    else:
        print("PASS: GraphQL auth token errors (KT-CT-*) logged and return None")

    # Test 11: Response with integration context header
    print("\n*** Test 11: Response with integration context header ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"data": "test"}
    headers = {integration_context_header: "test-context"}
    response = create_mock_response(200, json.dumps(response_data), headers)

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result != response_data:
        print(f"ERROR: Expected {response_data}, got {result}")
        failed = True
    else:
        print("PASS: Response with integration context header processed correctly")

    # Test 12: Response without integration context header
    print("\n*** Test 12: Response without integration context header ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"data": "test"}
    response = create_mock_response(200, json.dumps(response_data), {})

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result != response_data:
        print(f"ERROR: Expected {response_data}, got {result}")
        failed = True
    else:
        print("PASS: Response without integration context header processed correctly")

    # Test 13: Non-GraphQL URL with errors field
    print("\n*** Test 13: Non-GraphQL URL with errors field ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"errors": ["Some error"], "data": {"key": "value"}}
    response = create_mock_response(200, json.dumps(response_data))

    # Should not treat as GraphQL error since URL doesn't contain "graphql"
    result = await api.async_read_response(response, "https://api.octopus.energy/v1/products/", ignore_errors=False)

    if result != response_data:
        print(f"ERROR: Expected non-GraphQL errors to be ignored, got {result}")
        failed = True
    else:
        print("PASS: Non-GraphQL URL with errors field returns data normally")

    # Test 14: Status 502 (bad gateway)
    print("\n*** Test 14: Status 502 (bad gateway) ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response = create_mock_response(502, "Bad Gateway")

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result is not None:
        print(f"ERROR: Expected None for 502 error, got {result}")
        failed = True
    else:
        print("PASS: Bad gateway error (502) handled correctly, returns None")

    # Test 15: Status 201 (created) with valid JSON
    print("\n*** Test 15: Status 201 (created) with valid JSON ***")
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    response_data = {"created": True, "id": 123}
    response = create_mock_response(201, json.dumps(response_data))

    result = await api.async_read_response(response, "https://api.octopus.energy/test")

    if result != response_data:
        print(f"ERROR: Expected {response_data} for 201 status, got {result}")
        failed = True
    else:
        print("PASS: Status 201 (created) with valid JSON parsed correctly")

    # Summary
    if failed:
        print("\n**** Octopus async_read_response tests FAILED ****")
        raise Exception("Octopus async_read_response tests failed")
    else:
        print("\n**** All Octopus async_read_response tests PASSED ****")

    return failed
