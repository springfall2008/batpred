"""
Tests for Octopus API rate limiting error handling
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from octopus import OctopusAPI


def test_octopus_rate_limit_wrapper(my_predbat):
    return asyncio.run(test_octopus_rate_limit(my_predbat))


async def test_octopus_rate_limit(my_predbat):
    """
    Test OctopusAPI rate limiting error handling.

    Tests:
    - Test 1: Rate limit error on auth token refresh (KT-CT-1199)
    - Test 2: Rate limit error during intelligent device fetch - data preserved
    - Test 3: Rate limit error with obtainKrakenToken in path
    - Test 4: Multiple rate limit errors - existing data not overridden
    - Test 5: Rate limit followed by successful request
    - Test 6: Rate limit error on async_get_account - account_data preserved
    - Test 7: async_graphql_query fails immediately when token refresh fails
    - Test 8: Token refresh fails during retry after auth error
    - Test 9: Expired token is automatically refreshed and query retried successfully
    """
    # Mock asyncio.sleep to prevent real delays during rate limit testing
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        print("**** Running Octopus API rate limit tests ****")
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

        # Test 1: Rate limit error on auth token refresh (KT-CT-1199)
        print("\n*** Test 1: Rate limit error on auth token refresh (KT-CT-1199) ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Simulate the exact error from the logs
        rate_limit_error = {
            "errors": [
                {
                    "message": "Too many requests.",
                    "locations": [{"line": 2, "column": 2}],
                    "path": ["obtainKrakenToken"],
                    "extensions": {
                        "errorType": "AUTHORIZATION",
                        "errorCode": "KT-CT-1199",
                        "errorDescription": "There were too many requests. Please try again later.",
                    },
                }
            ]
        }
        response = create_mock_response(200, json.dumps(rate_limit_error))

        result = await api.async_read_response(response, "https://api.octopus.energy/v1/graphql/", ignore_errors=False)

        if result is not None:
            print(f"ERROR: Expected None for rate limit error, got {result}")
            failed = True
        else:
            print("PASS: Rate limit error (KT-CT-1199) returns None as expected")

        # Test 2: Rate limit error during intelligent device fetch - data preserved
        print("\n*** Test 2: Rate limit error during intelligent device fetch - data preserved ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Set up existing intelligent device data
        existing_device = {
            "device_id": "existing-device-123",
            "deviceType": "ELECTRIC_VEHICLES",
            "status": "LIVE",
            "provider": "Tesla",
            "model": "Model 3",
            "completed_dispatches": [
                {
                    "start": "2025-12-22T01:00:00Z",
                    "end": "2025-12-22T05:00:00Z",
                    "charge_in_kwh": 15.5,
                }
            ],
        }
        api.intelligent_device = existing_device.copy()

        # Mock the graphql query to return None (simulating rate limit)
        api.async_graphql_query = AsyncMock(return_value=None)

        # Mock tariffs to trigger intelligent device update
        api.tariffs = {
            "import": {
                "tariffCode": "INTELLI-VAR-22-10-14",
                "deviceID": "test-device-456",
            }
        }

        # Call update - should not override existing data when API fails
        result = await api.async_update_intelligent_device("test-account")

        # Verify existing data is preserved
        if api.intelligent_device != existing_device:
            print(f"ERROR: Existing device data was modified. Expected {existing_device}, got {api.intelligent_device}")
            failed = True
        else:
            print("PASS: Existing intelligent device data preserved when API returns None due to rate limit")

        # Test 3: Rate limit error with obtainKrakenToken in path
        print("\n*** Test 3: Rate limit error with obtainKrakenToken in path ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Test with different error code but same pattern
        rate_limit_error_v2 = {
            "errors": [
                {
                    "message": "Too many requests.",
                    "path": ["obtainKrakenToken"],
                    "extensions": {
                        "errorCode": "KT-CT-1199",
                    },
                }
            ]
        }
        response = create_mock_response(200, json.dumps(rate_limit_error_v2))

        result = await api.async_read_response(response, "https://api.octopus.energy/v1/graphql/")

        if result is not None:
            print(f"ERROR: Expected None for rate limit error, got {result}")
            failed = True
        else:
            print("PASS: Rate limit error with obtainKrakenToken path handled correctly")

        # Test 4: Multiple rate limit errors - existing data not overridden
        print("\n*** Test 4: Multiple rate limit errors - existing data not overridden ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Set up existing data
        existing_device = {
            "device_id": "persistent-device",
            "completed_dispatches": [{"start": "2025-12-22T00:00:00Z", "end": "2025-12-22T04:00:00Z", "charge_in_kwh": 20.0}],
        }
        api.intelligent_device = existing_device.copy()
        api.tariffs = {
            "import": {
                "tariffCode": "INTELLI-VAR-22-10-14",
                "deviceID": "device-123",
            }
        }

        # Mock multiple failed calls
        api.async_graphql_query = AsyncMock(return_value=None)

        # First call - should not override
        await api.async_update_intelligent_device("test-account")
        if api.intelligent_device != existing_device:
            print(f"ERROR: First failed update modified data")
            failed = True

        # Second call - should still not override
        await api.async_update_intelligent_device("test-account")
        if api.intelligent_device != existing_device:
            print(f"ERROR: Second failed update modified data")
            failed = True

        # Third call - should still not override
        await api.async_update_intelligent_device("test-account")
        if api.intelligent_device != existing_device:
            print(f"ERROR: Third failed update modified data")
            failed = True
        else:
            print("PASS: Multiple rate limit errors do not override existing data")

        # Test 5: Rate limit followed by successful request
        print("\n*** Test 5: Rate limit followed by successful request ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Set up existing data
        existing_device = {
            "device_id": "old-device",
            "completed_dispatches": [{"start": "2025-12-21T00:00:00Z", "end": "2025-12-21T04:00:00Z", "charge_in_kwh": 10.0}],
        }
        api.intelligent_device = existing_device.copy()
        api.tariffs = {
            "import": {
                "tariffCode": "INTELLI-VAR-22-10-14",
                "deviceID": "device-123",
            }
        }

        # First call fails (rate limit)
        api.async_graphql_query = AsyncMock(return_value=None)
        await api.async_update_intelligent_device("test-account")

        if api.intelligent_device != existing_device:
            print(f"ERROR: Failed update modified data")
            failed = True

        # Second call succeeds
        new_device_data = {
            "devices": [
                {
                    "deviceType": "ELECTRIC_VEHICLES",
                    "status": {"current": "LIVE"},
                    "__typename": "SmartFlexChargePoint",
                    "make": "Tesla",
                    "model": "Model Y",
                    "id": "new-device-789",
                }
            ],
            "chargePointVariants": [
                {
                    "make": "Tesla",
                    "models": [{"model": "Model Y", "powerInKw": 7.4}],
                }
            ],
            "electricVehicles": [],
        }

        dispatch_data = {
            "plannedDispatches": [],
            "completedDispatches": [
                {
                    "start": "2025-12-22T01:00:00Z",
                    "end": "2025-12-22T05:00:00Z",
                    "delta": "25.5",
                    "meta": {},
                }
            ],
        }

        settings_data = {
            "devices": [
                {
                    "id": "new-device-789",
                    "status": {"isSuspended": False},
                    "chargingPreferences": {
                        "weekdayTargetTime": "07:00",
                        "weekdayTargetSoc": 80,
                        "weekendTargetTime": "09:00",
                        "weekendTargetSoc": 90,
                        "minimumSoc": 20,
                        "maximumSoc": 100,
                    },
                }
            ]
        }

        # Mock successful responses
        async def mock_successful_query(query, context, ignore_errors=False, returns_data=True):
            if "get-intelligent-devices" in context:
                return new_device_data
            elif "get-intelligent-dispatches" in context:
                return dispatch_data
            elif "get-intelligent-settings" in context:
                return settings_data
            return None

        api.async_graphql_query = AsyncMock(side_effect=mock_successful_query)
        api.get_intelligent_completed_dispatches = MagicMock(return_value=[])

        # Mock get_state_wrapper for fetch_previous_dispatch - save original
        original_get_state_wrapper = my_predbat.get_state_wrapper
        my_predbat.get_state_wrapper = MagicMock(return_value=[])

        result = await api.async_update_intelligent_device("test-account")

        # Restore original get_state_wrapper
        my_predbat.get_state_wrapper = original_get_state_wrapper

        # Verify data was updated after successful call
        if result is None or api.intelligent_device.get("device_id") != "new-device-789":
            print(f"ERROR: Successful update after rate limit did not update data. Got: {api.intelligent_device}")
            failed = True
        else:
            print("PASS: Data successfully updated after rate limit error resolved")

        # Test 6: Rate limit error on async_get_account - account_data preserved
        print("\n*** Test 6: Rate limit error on async_get_account - account_data preserved ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Set up existing account data
        existing_account_data = {
            "account": {
                "accountNumber": "A-12345678",
                "electricityAgreements": [
                    {
                        "meterPoint": {
                            "mpan": "1234567890123",
                            "meters": [
                                {
                                    "activeFrom": "2023-01-01T00:00:00Z",
                                    "activeTo": None,
                                    "smartImportElectricityMeter": {"deviceId": "device-123"},
                                }
                            ],
                            "agreements": [
                                {
                                    "validFrom": "2023-01-01T00:00:00Z",
                                    "validTo": None,
                                    "tariff": {
                                        "tariffCode": "E-1R-AGILE-FLEX-22-11-25-C",
                                        "productCode": "AGILE-FLEX-22-11-25",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "gasAgreements": [],
            }
        }
        api.account_data = existing_account_data.copy()

        # Mock async_graphql_query to return None (simulating rate limit)
        api.async_graphql_query = AsyncMock(return_value=None)

        # Call async_get_account - should not override existing data
        result = await api.async_get_account("test-account")

        # Verify existing data is preserved
        if api.account_data != existing_account_data:
            print(f"ERROR: Account data was modified. Expected {existing_account_data}, got {api.account_data}")
            failed = True
        elif result != existing_account_data:
            print(f"ERROR: Return value incorrect. Expected {existing_account_data}, got {result}")
            failed = True
        else:
            print("PASS: Existing account_data preserved when async_get_account rate limited")

        # Test 6b: Verify successful async_get_account updates data
        print("\n*** Test 6b: Successful async_get_account updates data ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Set up existing old account data
        old_account_data = {
            "account": {
                "accountNumber": "A-OLD-DATA",
            }
        }
        api.account_data = old_account_data.copy()

        # Mock successful response with new data
        new_account_data = {
            "account": {
                "accountNumber": "A-NEW-DATA",
                "electricityAgreements": [
                    {
                        "meterPoint": {
                            "mpan": "9999999999999",
                        }
                    }
                ],
            }
        }
        api.async_graphql_query = AsyncMock(return_value=new_account_data)

        # Call async_get_account - should update data
        result = await api.async_get_account("test-account")

        # Verify data was updated
        if api.account_data != new_account_data:
            print(f"ERROR: Account data not updated. Expected {new_account_data}, got {api.account_data}")
            failed = True
        elif result != new_account_data:
            print(f"ERROR: Return value incorrect. Expected {new_account_data}, got {result}")
            failed = True
        else:
            print("PASS: Account data successfully updated when async_get_account succeeds")

        # Test 7: async_graphql_query fails immediately when token refresh fails
        print("\n*** Test 7: async_graphql_query fails immediately when token refresh fails ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Mock async_refresh_token to return None (simulating rate limit during token refresh)
        api.async_refresh_token = AsyncMock(return_value=None)

        # Attempt a GraphQL query - should fail immediately without making HTTP request
        result = await api.async_graphql_query("query { test }", "test-query", returns_data=True, ignore_errors=False)

        if result is not None:
            print(f"ERROR: Expected None when token refresh fails, got {result}")
            failed = True
        else:
            print("PASS: async_graphql_query returns None immediately when token refresh fails")

        # Test 7b: Verify failures_total is incremented when token refresh fails
        print("\n*** Test 7b: Verify failures_total incremented when token refresh fails ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
        initial_failures = api.failures_total

        # Mock async_refresh_token to return None
        api.async_refresh_token = AsyncMock(return_value=None)

        # Attempt a GraphQL query
        result = await api.async_graphql_query("query { test }", "test-query", returns_data=True, ignore_errors=False)

        if api.failures_total != initial_failures + 1:
            print(f"ERROR: failures_total not incremented. Expected {initial_failures + 1}, got {api.failures_total}")
            failed = True
        else:
            print("PASS: failures_total incremented when token refresh fails")

        # Test 7c: Verify ignore_errors=True suppresses failure counting
        print("\n*** Test 7c: Verify ignore_errors=True suppresses failure counting ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
        initial_failures = api.failures_total

        # Mock async_refresh_token to return None
        api.async_refresh_token = AsyncMock(return_value=None)

        # Attempt a GraphQL query with ignore_errors=True
        result = await api.async_graphql_query("query { test }", "test-query", returns_data=True, ignore_errors=True)

        if api.failures_total != initial_failures + 1:
            print(f"ERROR: failures_total should increment even with ignore errors (due to API key) Expected {initial_failures + 1}, got {api.failures_total}")
            failed = True
        else:
            print("PASS: ignore_errors=True suppresses failure counting when token refresh fails")

        # Test 9: Expired token is automatically refreshed and query retried successfully
        print("\n*** Test 9: Expired token is automatically refreshed and query retried successfully ***")
        api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

        # Set up initial expired token
        api.graphql_token = "expired-token-123"

        # Mock async_create_client_session
        mock_client = AsyncMock()
        api.api.async_create_client_session = AsyncMock(return_value=mock_client)

        # Track calls
        call_count = [0]
        async_read_response_calls = []

        # First call returns auth error (KT-CT-1139 - expired token)
        # Second call (after refresh) returns success
        auth_error_response = {"errors": [{"message": "Token expired", "extensions": {"errorCode": "KT-CT-1139"}}]}

        success_response = {"data": {"account": {"accountNumber": "A-SUCCESS-12345"}}}

        async def mock_read_response(response, url, ignore_errors=False):
            call_count[0] += 1
            async_read_response_calls.append({"call": call_count[0], "url": url, "ignore_errors": ignore_errors})
            if call_count[0] == 1:
                # First call - return auth error
                return auth_error_response
            else:
                # Second call after token refresh - return success
                return success_response

        api.async_read_response = AsyncMock(side_effect=mock_read_response)

        # Mock the POST request
        mock_response = AsyncMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = MagicMock(return_value=mock_response)

        # Mock async_refresh_token to track calls and return new token
        refresh_call_count = [0]

        async def mock_refresh():
            refresh_call_count[0] += 1
            if refresh_call_count[0] == 1:
                # Initial refresh returns existing token
                return "expired-token-123"
            else:
                # Retry refresh returns new token
                api.graphql_token = "refreshed-token-456"
                return "refreshed-token-456"

        api.async_refresh_token = AsyncMock(side_effect=mock_refresh)

        # Attempt query - should automatically retry after token refresh
        result = await api.async_graphql_query("query { account }", "test-auto-retry", returns_data=True, ignore_errors=False)

        # Verify the results
        if result != success_response["data"]:
            print(f"ERROR: Expected success data, got {result}")
            failed = True
        elif call_count[0] != 2:
            print(f"ERROR: Expected 2 API calls (initial + retry), got {call_count[0]}")
            failed = True
        elif refresh_call_count[0] != 3:
            print(f"ERROR: Expected 3 token refresh calls (initial + retry + recursive), got {refresh_call_count[0]}")
            failed = True
        elif api.graphql_token != "refreshed-token-456":
            print(f"ERROR: Expected refreshed token, got {api.graphql_token}")
            failed = True
        else:
            print("PASS: Expired token automatically refreshed and query retried successfully")

        # Test 9b: Verify multiple auth error codes trigger auto-refresh
        print("\n*** Test 9b: Verify all auth error codes trigger auto-refresh ***")
        for error_code in ["KT-CT-1139", "KT-CT-1111", "KT-CT-1143"]:
            api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
            api.graphql_token = "expired-token"

            # Mock client
            mock_client = AsyncMock()
            api.api.async_create_client_session = AsyncMock(return_value=mock_client)

            # Track calls
            call_count = [0]

            # First call returns specific auth error, second call succeeds
            auth_error = {"errors": [{"message": "Auth error", "extensions": {"errorCode": error_code}}]}
            success = {"data": {"test": "success"}}

            async def mock_read(response, url, ignore_errors=False):
                call_count[0] += 1
                return auth_error if call_count[0] == 1 else success

            api.async_read_response = AsyncMock(side_effect=mock_read)

            # Mock POST
            mock_response = AsyncMock()
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = MagicMock(return_value=mock_response)

            # Mock refresh
            refresh_calls = [0]

            async def mock_refresh():
                refresh_calls[0] += 1
                if refresh_calls[0] == 1:
                    return "expired-token"
                api.graphql_token = "new-token"
                return "new-token"

            api.async_refresh_token = AsyncMock(side_effect=mock_refresh)

            # Test the query
            result = await api.async_graphql_query("query { test }", f"test-{error_code}", returns_data=True, ignore_errors=False)

            if result != success["data"]:
                print(f"ERROR: Error code {error_code} did not trigger auto-refresh correctly. Got {result}")
                failed = True
                break
            elif call_count[0] != 2:
                print(f"ERROR: Error code {error_code} - expected 2 calls, got {call_count[0]}")
                failed = True
                break
        else:
            print("PASS: All auth error codes (KT-CT-1139, KT-CT-1111, KT-CT-1143) trigger auto-refresh")

            # Summary
            if failed:
                print("\n**** Octopus API rate limit tests FAILED ****")
                raise Exception("Octopus API rate limit tests failed")
            else:
                print("\n**** All Octopus API rate limit tests PASSED ****")

        return failed
