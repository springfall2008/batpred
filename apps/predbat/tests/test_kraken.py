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


def make_kraken_api(provider="edf", account_id="A-TEST123", auth_method="api_key", key="test-key", **kwargs):
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
    api.initialize(provider=provider, account_id=account_id, key=key, auth_method=auth_method, **kwargs)
    return api


def test_initialize_sets_base_url_from_provider():
    api = make_kraken_api(provider="edf")
    assert api.base_url == "https://api.edfgb-kraken.energy"
    api2 = make_kraken_api(provider="eon")
    assert api2.base_url == "https://api.eonnext-kraken.energy"


def test_initialize_sets_current_tariff_none():
    api = make_kraken_api()
    assert api.current_tariff is None


def test_initialize_with_export_config():
    api = make_kraken_api(
        export_account_id="A-EXPORT456",
        export_mpan="2000000000123",
        mpan="1900000000456",
    )
    assert api.export_account_id == "A-EXPORT456"
    assert api.export_mpan == "2000000000123"
    assert api.configured_mpan == "1900000000456"


def test_graphql_query_success():
    api = make_kraken_api()
    api.access_token = "jwt-token-123"
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"data": {"account": {"electricityMeterPoints": []}}})

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

    assert result == {"account": {"electricityMeterPoints": []}}
    # Auth header should be bare token (not "JWT ..." or "Bearer ...")
    call_args = mock_session.post.call_args
    assert call_args[1]["headers"]["Authorization"] == "jwt-token-123"


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
    success_resp.json = AsyncMock(return_value={"data": {"account": {"electricityMeterPoints": []}}})

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

    assert result == {"account": {"electricityMeterPoints": []}}
    api.handle_oauth_401.assert_called_once()


def test_graphql_query_oauth_failed_returns_none():
    api = make_kraken_api()
    api.oauth_failed = True
    api.check_and_refresh_oauth_token = AsyncMock(return_value=False)
    result = asyncio.run(api.async_graphql_query("query { test }", "test-context"))
    assert result is None


def test_find_tariffs_detects_change():
    api = make_kraken_api()
    # Response shape matches validated EDF/E.ON Kraken schema (electricityMeterPoints)
    account_data = {
        "account": {
            "number": "A-TEST123",
            "properties": [
                {
                    "address": "1 Test Street, London, SW1A 1AA",
                    "electricityMeterPoints": [
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
    api._discover_export_tariff = AsyncMock()
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
                    "address": "1 Test Street",
                    "electricityMeterPoints": [
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
    api._discover_export_tariff = AsyncMock()
    result = asyncio.run(api.async_find_tariffs())
    assert result is None


def test_find_tariffs_graphql_failure():
    api = make_kraken_api()
    api.async_graphql_query = AsyncMock(return_value=None)
    result = asyncio.run(api.async_find_tariffs())
    assert result is None
    assert api.current_tariff is None


def test_find_tariffs_skips_export_on_import_discovery():
    """Import discovery should NOT match export tariff codes."""
    api = make_kraken_api()
    account_data = {
        "account": {
            "number": "A-TEST123",
            "properties": [
                {
                    "address": "1 Test Street",
                    "electricityMeterPoints": [
                        {
                            "mpan": "2000000000123",
                            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "EXPORT-VAR-01", "tariffCode": "E-1R-EXPORT-VAR-01-J", "displayName": "EDF Export"}}],
                        }
                    ],
                }
            ],
        },
    }
    api.async_graphql_query = AsyncMock(return_value=account_data)
    api._discover_export_tariff = AsyncMock()
    result = asyncio.run(api.async_find_tariffs())
    assert result is None  # No import tariff found


def test_find_tariffs_discovers_export_on_same_account():
    """Export tariff discovered from same account's meter points (strategy 2)."""
    api = make_kraken_api()
    meter_points = [
        {
            "mpan": "1900000000456",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "VAR-01", "tariffCode": "E-1R-VAR-01-J", "displayName": "Import"}}],
        },
        {
            "mpan": "2000000000789",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "EXPORT-FIX-01", "tariffCode": "E-1R-EXPORT-FIX-01-J", "displayName": "Export"}}],
        },
    ]
    result = api._find_active_tariff(meter_points, is_export=True)
    assert result is not None
    assert result["tariff_code"] == "E-1R-EXPORT-FIX-01-J"
    assert result["mpan"] == "2000000000789"


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
    api.async_fetch_rates.assert_called()
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

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.async_fetch_rates = AsyncMock(return_value=[{"value_inc_vat": 24.5}])
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.update_success_timestamp = MagicMock()

    # seconds=600 → count_minutes=10, 10%10==0, 10%30!=0 (so no tariff discovery, but rates fetch)
    result = asyncio.run(api.run(600, False))

    assert result is True
    api.async_fetch_rates.assert_called()
    api.async_fetch_standing_charges.assert_called_once()


def test_run_wires_export_when_discovered():
    """run() wires export rates into fetch.py when export tariff is discovered."""
    api = make_kraken_api()
    discovered = {"tariff_code": "E-1R-IMP-01", "product_code": "IMP-01"}

    async def find_tariffs_side_effect():
        api.current_tariff = discovered
        api.export_tariff = {"tariff_code": "E-1R-EXPORT-01", "product_code": "EXPORT-01"}
        return discovered

    api.async_find_tariffs = AsyncMock(side_effect=find_tariffs_side_effect)
    api.async_fetch_rates = AsyncMock(return_value=[{"value_inc_vat": 24.5}])
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is True
    api.set_arg.assert_any_call("metric_octopus_export", api.get_entity_name("sensor", "export_rates"))


def test_find_active_tariff_prefers_configured_mpan():
    """_find_active_tariff should prefer the configured MPAN."""
    api = make_kraken_api()
    meter_points = [
        {
            "mpan": "1900000000001",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "VAR-01", "tariffCode": "E-1R-VAR-01-A", "displayName": "Tariff A"}}],
        },
        {
            "mpan": "1900000000002",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "VAR-01", "tariffCode": "E-1R-VAR-01-B", "displayName": "Tariff B"}}],
        },
    ]
    result = api._find_active_tariff(meter_points, preferred_mpan="1900000000002", is_export=False)
    assert result is not None
    assert result["mpan"] == "1900000000002"
    assert result["tariff_code"] == "E-1R-VAR-01-B"


def test_standing_charge_converts_pence_to_pounds():
    """async_fetch_standing_charges divides by 100 (API returns pence, fetch.py expects pounds)."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}

    standing_response = {
        "results": [{"value_inc_vat": 53.0, "valid_from": "2026-01-01T00:00:00Z"}],
    }

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=standing_response)

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
        result = asyncio.run(api.async_fetch_standing_charges())

    # 53.0 pence → 0.53 pounds
    assert result == 0.53


def test_export_discovery_clears_stale_when_not_found():
    """Export tariff is cleared if all strategies fail (prevents stale rates)."""
    api = make_kraken_api()
    api.export_tariff = {"tariff_code": "E-1R-OLD-EXPORT", "product_code": "OLD-EXPORT"}

    # No export meter points on import account, no configured export account
    meter_points = [
        {
            "mpan": "1900000000456",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "VAR-01", "tariffCode": "E-1R-VAR-01-J", "displayName": "Import Only"}}],
        },
    ]
    asyncio.run(api._discover_export_tariff(meter_points, "1 test street"))
    assert api.export_tariff is None


def test_export_discovery_strategy1_no_fallthrough_on_network_failure():
    """When export_account_id is configured, Strategy 1 network failure does NOT fall through to Strategy 2."""
    api = make_kraken_api(export_account_id="A-EXPORT456")
    api.export_tariff = {"tariff_code": "E-1R-OLD-EXPORT", "product_code": "OLD-EXPORT"}

    # Strategy 1 will fail (graphql returns None)
    api.async_graphql_query = AsyncMock(return_value=None)

    # Import account has an export tariff — Strategy 2 SHOULD NOT pick it up
    import_meter_points = [
        {
            "mpan": "2000000000789",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "EXPORT-IMP", "tariffCode": "E-1R-EXPORT-IMP-01-J", "displayName": "Wrong Export"}}],
        },
    ]
    asyncio.run(api._discover_export_tariff(import_meter_points, "1 test street"))
    # Should NOT have picked up the import account's export tariff
    assert api.export_tariff == {"tariff_code": "E-1R-OLD-EXPORT", "product_code": "OLD-EXPORT"}


def test_normalize_rate_timestamps_flat_rate_both_null():
    """Flat-rate tariff with valid_from=null and valid_to=null (observed from Kraken export API).

    Without normalization, minute_data() skips these entries because it can't
    parse null as a timestamp, resulting in 'metric_octopus_export not set correctly'.
    """
    from kraken import KrakenAPI

    results = [{"value_inc_vat": 16.5, "valid_from": None, "valid_to": None, "payment_method": None}]
    normalized = KrakenAPI._normalize_rate_timestamps(results)

    assert len(normalized) == 1
    assert normalized[0]["value_inc_vat"] == 16.5
    assert normalized[0]["valid_from"] is not None  # Must be a real timestamp
    assert normalized[0]["valid_to"] is None  # Left as-is (minute_data handles this)
    # Should be parsable as ISO datetime
    from datetime import datetime

    datetime.fromisoformat(normalized[0]["valid_from"].replace("Z", "+00:00"))


def test_normalize_rate_timestamps_normal_rates_unchanged():
    """Rates with real timestamps should pass through unmodified."""
    from kraken import KrakenAPI

    results = [
        {"value_inc_vat": 24.5, "valid_from": "2026-03-23T00:00:00Z", "valid_to": "2026-03-24T00:00:00Z"},
        {"value_inc_vat": 28.3, "valid_from": "2026-03-24T00:00:00Z", "valid_to": None},
    ]
    normalized = KrakenAPI._normalize_rate_timestamps(results)

    assert normalized[0]["valid_from"] == "2026-03-23T00:00:00Z"
    assert normalized[1]["valid_from"] == "2026-03-24T00:00:00Z"


def test_normalize_rate_timestamps_empty_list():
    """Empty results should return empty."""
    from kraken import KrakenAPI

    assert KrakenAPI._normalize_rate_timestamps([]) == []
    assert KrakenAPI._normalize_rate_timestamps(None) is None


def test_normalize_rate_timestamps_mixed_null_and_real():
    """Mixed results where some have null valid_from (hypothetical future Kraken response).

    We have NOT observed this from Kraken's API - only the single-entry flat-rate case
    has been seen. This test documents expected behaviour if Kraken later returns
    multiple rate entries where the latest has valid_from=null (open-ended) alongside
    historical entries with real timestamps, similar to Octopus's pattern.
    """
    from kraken import KrakenAPI

    results = [
        {"value_inc_vat": 15.0, "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-04-01T00:00:00Z"},
        {"value_inc_vat": 16.5, "valid_from": None, "valid_to": None},
    ]
    normalized = KrakenAPI._normalize_rate_timestamps(results)

    # The null valid_from should get the earliest valid_to as its start
    assert normalized[0]["valid_from"] == "2026-01-01T00:00:00Z"  # Unchanged
    assert normalized[1]["valid_from"] == "2026-04-01T00:00:00Z"  # Set from earliest valid_to


def run_kraken_tests(my_predbat=None):
    """Run all KrakenAPI tests. Returns True on failure, False on success."""
    tests = [
        test_initialize_sets_base_url_from_provider,
        test_initialize_sets_current_tariff_none,
        test_initialize_with_export_config,
        test_graphql_query_success,
        test_graphql_query_auth_error_retries,
        test_graphql_query_oauth_failed_returns_none,
        test_find_tariffs_detects_change,
        test_find_tariffs_no_change,
        test_find_tariffs_graphql_failure,
        test_find_tariffs_skips_export_on_import_discovery,
        test_find_tariffs_discovers_export_on_same_account,
        test_build_rates_url,
        test_build_rates_url_eon,
        test_fetch_rates_single_page,
        test_fetch_rates_no_tariff,
        test_get_entity_name,
        test_run_first_discovers_tariff_and_fetches_rates,
        test_run_returns_false_on_auth_failure,
        test_run_fetches_rates_on_10min_cycle,
        test_run_wires_export_when_discovered,
        test_find_active_tariff_prefers_configured_mpan,
        test_standing_charge_converts_pence_to_pounds,
        test_export_discovery_clears_stale_when_not_found,
        test_export_discovery_strategy1_no_fallthrough_on_network_failure,
        test_normalize_rate_timestamps_flat_rate_both_null,
        test_normalize_rate_timestamps_normal_rates_unchanged,
        test_normalize_rate_timestamps_empty_list,
        test_normalize_rate_timestamps_mixed_null_and_real,
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
