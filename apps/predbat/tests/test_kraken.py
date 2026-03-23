# src/batpred/apps/predbat/tests/test_kraken.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_mock_base():
    base = MagicMock()
    base.log = MagicMock()
    base.prefix = "predbat"
    base.local_tz = "Europe/London"
    base.args = {"user_id": "test-instance-123"}
    return base


def make_kraken_api(provider="edf", account_id="A-TEST123", auth_method="api_key", key="test-key"):
    from kraken import KrakenAPI

    api = KrakenAPI.__new__(KrakenAPI)
    base = make_mock_base()
    api.base = base
    api.log = base.log
    api.api_started = False
    api.api_stop = False
    api.last_success_timestamp = None
    api.local_tz = base.local_tz
    api.prefix = base.prefix
    api.args = base.args
    api.count_errors = 0
    api.run_timeout = 3600
    api.initialize(provider=provider, account_id=account_id, key=key, auth_method=auth_method)
    return api


def test_initialize_sets_base_url_from_provider():
    api = make_kraken_api(provider="edf")
    assert api.base_url == "https://api.edfgb-kraken.energy"
    api2 = make_kraken_api(provider="eon")
    assert api2.base_url == "https://api.eonnext-kraken.energy"


def test_initialize_sets_current_tariff_none():
    api = make_kraken_api()
    assert api.current_tariff is None


def test_graphql_query_success():
    api = make_kraken_api()
    api.access_token = "jwt-token-123"
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"data": {"account": {"electricityAgreements": []}}})

    mock_session = AsyncMock()
    mock_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_graphql_query("query { test }", "test-context"))

    assert result == {"account": {"electricityAgreements": []}}
    call_args = mock_session.post.call_args
    assert call_args[1]["headers"]["Authorization"] == "JWT jwt-token-123"


def test_graphql_query_auth_error_retries():
    api = make_kraken_api()
    api.access_token = "old-token"
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    api.handle_oauth_401 = AsyncMock(return_value=True)

    auth_error_resp = AsyncMock()
    auth_error_resp.status = 200
    auth_error_resp.json = AsyncMock(return_value={"errors": [{"extensions": {"errorCode": "KT-CT-1139"}, "message": "Invalid token"}]})
    success_resp = AsyncMock()
    success_resp.status = 200
    success_resp.json = AsyncMock(return_value={"data": {"account": {"electricityAgreements": []}}})

    call_count = [0]

    def make_context(resp):
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    def post_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return make_context(auth_error_resp)
        return make_context(success_resp)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(side_effect=post_side_effect)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_graphql_query("query { test }", "test-context"))

    assert result == {"account": {"electricityAgreements": []}}
    api.handle_oauth_401.assert_called_once()


def test_graphql_query_oauth_failed_returns_none():
    api = make_kraken_api()
    api.oauth_failed = True
    api.check_and_refresh_oauth_token = AsyncMock(return_value=False)
    result = asyncio.run(api.async_graphql_query("query { test }", "test-context"))
    assert result is None


def test_find_tariffs_detects_change():
    api = make_kraken_api()
    # Response shape matches validated EDF/E.ON Kraken schema (electricitySupplyPoints)
    account_data = {
        "account": {
            "number": "A-TEST123",
            "properties": [
                {
                    "electricitySupplyPoints": [
                        {
                            "mpan": "1234567890123",
                            "agreements": [
                                {
                                    "validFrom": "2026-01-01T00:00:00+00:00",
                                    "validTo": None,
                                    "tariff": {
                                        "productCode": "VAR-22-11-01",
                                        "tariffCode": "E-1R-VAR-22-11-01-J",
                                        "displayName": "EDF Variable",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }
    api.async_graphql_query = AsyncMock(return_value=account_data)
    result = asyncio.run(api.async_find_tariffs())
    assert result is not None
    assert result["tariff_code"] == "E-1R-VAR-22-11-01-J"
    assert result["product_code"] == "VAR-22-11-01"
    assert api.current_tariff == {"tariff_code": "E-1R-VAR-22-11-01-J", "product_code": "VAR-22-11-01"}


def test_find_tariffs_no_change():
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-22-11-01-J", "product_code": "VAR-22-11-01"}
    account_data = {
        "account": {
            "number": "A-TEST123",
            "properties": [
                {
                    "electricitySupplyPoints": [
                        {
                            "mpan": "1234567890123",
                            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "VAR-22-11-01", "tariffCode": "E-1R-VAR-22-11-01-J", "displayName": "EDF Variable"}}],
                        }
                    ],
                }
            ],
        },
    }
    api.async_graphql_query = AsyncMock(return_value=account_data)
    result = asyncio.run(api.async_find_tariffs())
    assert result is None


def test_find_tariffs_graphql_failure():
    api = make_kraken_api()
    api.async_graphql_query = AsyncMock(return_value=None)
    result = asyncio.run(api.async_find_tariffs())
    assert result is None
    assert api.current_tariff is None


def test_build_rates_url():
    api = make_kraken_api(provider="edf")
    url = api.build_rates_url("VAR-22-11-01", "E-1R-VAR-22-11-01-J")
    assert url == "https://api.edfgb-kraken.energy/v1/products/VAR-22-11-01/electricity-tariffs/E-1R-VAR-22-11-01-J/standard-unit-rates/"


def test_build_rates_url_eon():
    api = make_kraken_api(provider="eon")
    url = api.build_rates_url("PROD-01", "E-1R-PROD-01-A")
    assert url == "https://api.eonnext-kraken.energy/v1/products/PROD-01/electricity-tariffs/E-1R-PROD-01-A/standard-unit-rates/"


def test_fetch_rates_single_page():
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-22-11-01-J", "product_code": "VAR-22-11-01"}

    rates_response = {
        "count": 2,
        "next": None,
        "results": [
            {"value_inc_vat": 24.5, "valid_from": "2026-03-23T00:00:00Z", "valid_to": "2026-03-24T00:00:00Z"},
            {"value_inc_vat": 28.3, "valid_from": "2026-03-24T00:00:00Z", "valid_to": "2026-03-25T00:00:00Z"},
        ],
    }

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=rates_response)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates())

    assert result is not None
    assert len(result) == 2
    assert result[0]["value_inc_vat"] == 24.5


def test_fetch_rates_no_tariff():
    api = make_kraken_api()
    assert api.current_tariff is None
    result = asyncio.run(api.async_fetch_rates())
    assert result is None


def test_get_entity_name():
    api = make_kraken_api(account_id="A-1234ABCD")
    name = api.get_entity_name("sensor", "import_rates")
    assert name == "sensor.predbat_kraken_a_1234abcd_import_rates"


def test_run_first_discovers_tariff_and_fetches_rates():
    api = make_kraken_api()
    discovered = {"tariff_code": "E-1R-NEW-01", "product_code": "NEW-01"}

    async def find_tariffs_side_effect():
        api.current_tariff = discovered
        return discovered

    api.async_find_tariffs = AsyncMock(side_effect=find_tariffs_side_effect)
    api.async_fetch_rates = AsyncMock(
        return_value=[
            {"value_inc_vat": 24.5, "valid_from": "2026-03-23T00:00:00Z", "valid_to": "2026-03-24T00:00:00Z"},
        ]
    )
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is True
    api.async_find_tariffs.assert_called_once()
    api.async_fetch_rates.assert_called_once()
    api.async_fetch_standing_charges.assert_called_once()
    api.dashboard_item.assert_called()
    api.update_success_timestamp.assert_called_once()
    # On first run, should wire into fetch.py
    api.set_arg.assert_any_call("metric_octopus_import", api.get_entity_name("sensor", "import_rates"))
    api.set_arg.assert_any_call("metric_standing_charge", api.get_entity_name("sensor", "import_standing"))


def test_run_returns_false_on_auth_failure():
    """run() returns False on first run when auth has failed."""
    api = make_kraken_api()
    api.oauth_failed = True
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.dashboard_item = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is False
    api.update_success_timestamp.assert_not_called()


def test_run_fetches_rates_on_10min_cycle():
    """run() fetches rates on 10-minute boundaries, not every cycle."""
    from datetime import datetime as dt

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.async_fetch_rates = AsyncMock(return_value=[{"value_inc_vat": 24.5}])
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.update_success_timestamp = MagicMock()

    # Mock time to 10:00 (count_minutes=600, 600%10==0, 600%30==0)
    with patch("kraken.datetime") as mock_dt:
        mock_dt.now.return_value = dt(2026, 3, 23, 10, 0, 0)
        mock_dt.fromisoformat = dt.fromisoformat
        result = asyncio.run(api.run(0, False))

    assert result is True
    api.async_fetch_rates.assert_called_once()
    api.async_fetch_standing_charges.assert_called_once()


def run_kraken_tests(my_predbat=None):
    """Run all KrakenAPI tests. Returns True on failure, False on success."""
    tests = [
        test_initialize_sets_base_url_from_provider,
        test_initialize_sets_current_tariff_none,
        test_graphql_query_success,
        test_graphql_query_auth_error_retries,
        test_graphql_query_oauth_failed_returns_none,
        test_find_tariffs_detects_change,
        test_find_tariffs_no_change,
        test_find_tariffs_graphql_failure,
        test_build_rates_url,
        test_build_rates_url_eon,
        test_fetch_rates_single_page,
        test_fetch_rates_no_tariff,
        test_get_entity_name,
        test_run_first_discovers_tariff_and_fetches_rates,
        test_run_returns_false_on_auth_failure,
        test_run_fetches_rates_on_10min_cycle,
    ]
    for test_func in tests:
        try:
            test_func()
        except Exception as e:
            print(f"  FAIL: {test_func.__name__}: {e}")
            import traceback

            traceback.print_exc()
            return True
        print(f"  OK: {test_func.__name__}")
    return False
