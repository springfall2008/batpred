# src/batpred/apps/predbat/tests/test_kraken.py
import asyncio
import time
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
    # No storage component by default — the `storage` property returns None, so cache
    # load/save are no-ops. Tests that exercise caching inject a fake storage explicitly.
    base.components = None
    return base


class FakeStorage:
    """In-memory stand-in for the storage component used by cache tests."""

    def __init__(self):
        self.data = {}

    async def load(self, module, filename):
        """Return the stored blob for (module, filename), or None."""
        return self.data.get((module, filename))

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Store a blob for (module, filename)."""
        self.data[(module, filename)] = data
        return True


def attach_fake_storage(api):
    """Give an api a working in-memory storage and return it (bypasses the storage property)."""
    storage = FakeStorage()
    # `storage` is a read-only property on ComponentBase, so route base.components at it.
    components = MagicMock()
    components.get_component = MagicMock(return_value=storage)
    api.base.components = components
    return storage


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
    api.async_discover_smart_devices = AsyncMock()  # no SmartFlex devices — avoid a real network call
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
    api.async_discover_smart_devices = AsyncMock()
    api.dashboard_item = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is False
    api.update_success_timestamp.assert_not_called()


def test_run_refetches_rates_when_stale():
    """run() re-fetches rates when the cached rates are older than the refresh threshold."""
    from datetime import datetime, timedelta
    from kraken import KRAKEN_RATES_REFRESH_MINUTES

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.tariff_fetched_at = datetime.now()  # fresh tariff → no re-discovery
    api.rates_fetched_at = datetime.now() - timedelta(minutes=KRAKEN_RATES_REFRESH_MINUTES + 5)  # stale → re-fetch
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.async_fetch_rates = AsyncMock(return_value=[{"value_inc_vat": 24.5}])
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(600, False))

    assert result is True
    api.async_find_tariffs.assert_not_called()  # tariff still fresh
    api.async_fetch_rates.assert_called()
    api.async_fetch_standing_charges.assert_called_once()


def test_run_skips_refetch_when_cache_fresh():
    """run() does NOT re-query the API when both tariff and rates are freshly cached."""
    from datetime import datetime

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.async_fetch_rates = AsyncMock(return_value=[{"value_inc_vat": 24.5}])
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(120, False))

    assert result is True
    api.async_find_tariffs.assert_not_called()
    api.async_fetch_rates.assert_not_called()
    api.async_fetch_standing_charges.assert_not_called()


def test_run_wires_export_when_discovered():
    """run() wires export rates into fetch.py when export tariff is discovered."""
    api = make_kraken_api()
    discovered = {"tariff_code": "E-1R-IMP-01", "product_code": "IMP-01"}

    async def find_tariffs_side_effect():
        api.current_tariff = discovered
        api.export_tariff = {"tariff_code": "E-1R-EXPORT-01", "product_code": "EXPORT-01"}
        return discovered

    api.async_find_tariffs = AsyncMock(side_effect=find_tariffs_side_effect)
    api.async_discover_smart_devices = AsyncMock()
    api.async_fetch_rates = AsyncMock(return_value=[{"value_inc_vat": 24.5}])
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is True
    api.set_arg.assert_any_call("metric_octopus_export", api.get_entity_name("sensor", "export_rates"))


def test_run_does_not_wire_export_when_rates_unavailable():
    """An export tariff can be discovered while its rate endpoint returns no data.

    Real case: EDF SEG export tariffs (e.g. EDF_EXPORT_SEG_12M) are registered on
    the meter point so the tariff is discovered, but Kraken's standard-unit-rates
    endpoint returns HTTP 404, so no export rates are ever fetched. Wiring
    metric_octopus_export to the (empty) export_rates sensor in that case makes
    fetch.py take the octopus-export branch and ignore the user's manual
    rates_export fallback, zeroing out all export in the plan. So when no export
    rates are available, metric_octopus_export must NOT be wired.
    """
    api = make_kraken_api()
    discovered = {"tariff_code": "E-1R-IMP-01", "product_code": "IMP-01"}

    async def find_tariffs_side_effect():
        api.current_tariff = discovered
        api.export_tariff = {"tariff_code": "E-1R-EDF_EXPORT_SEG_12M_HH-B", "product_code": "EDF_EXPORT_SEG_12M"}
        return discovered

    async def fetch_rates_side_effect(tariff=None):
        # Export fetch (tariff kwarg set) returns nothing — simulates the SEG 404.
        if tariff is not None:
            return []
        return [{"value_inc_vat": 24.5}]

    api.async_find_tariffs = AsyncMock(side_effect=find_tariffs_side_effect)
    api.async_discover_smart_devices = AsyncMock()
    api.async_fetch_rates = AsyncMock(side_effect=fetch_rates_side_effect)
    api.async_fetch_standing_charges = AsyncMock(return_value=53.0)
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is True
    # Import is still wired normally.
    api.set_arg.assert_any_call("metric_octopus_import", api.get_entity_name("sensor", "import_rates"))
    # Export must NOT be wired — no export rates were available.
    export_calls = [c for c in api.set_arg.call_args_list if c.args and c.args[0] == "metric_octopus_export"]
    assert export_calls == [], "metric_octopus_export should not be wired when export rates are unavailable, got {}".format(export_calls)
    assert api.export_wired is False


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


def test_fetch_standing_charges_rest_404_falls_back_to_graphql():
    """async_fetch_standing_charges() falls back to GraphQL when REST returns 404.

    Reproduces the E.ON Next TOU tariff scenario: NEXT_SMART_SAVER_FIXED_12M_V8 has no
    /standing-charges/ REST endpoint, returns HTTP 404, GraphQL fallback must be used.
    """
    api = make_kraken_api(provider="eon", account_id="A-AA8A473C")
    api.current_tariff = {"tariff_code": "E-TOU-NEXT_SMART_SAVER_FIXED_12M_V8-M", "product_code": "NEXT_SMART_SAVER_FIXED_12M_V8"}
    api.import_mpan = "1900000000456"
    api.async_fetch_standing_charges_graphql = AsyncMock(return_value=0.6195)

    mock_response = AsyncMock()
    mock_response.status = 404
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

    assert result == 0.6195
    api.async_fetch_standing_charges_graphql.assert_called_once_with("1900000000456")


def test_fetch_standing_charges_rest_404_no_fallback_without_import_mpan():
    """async_fetch_standing_charges() returns None (no fallback) when REST 404 and import_mpan not set."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-TOU-OLD-M", "product_code": "OLD"}
    api.import_mpan = None

    mock_response = AsyncMock()
    mock_response.status = 404
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

    assert result is None


def test_fetch_standing_charges_rest_410_falls_back_to_graphql():
    """async_fetch_standing_charges() also falls back on HTTP 410 Gone."""
    api = make_kraken_api(provider="eon", account_id="A-AA8A473C")
    api.current_tariff = {"tariff_code": "E-TOU-GONE-V1-M", "product_code": "GONE-V1"}
    api.import_mpan = "1900000000456"
    api.async_fetch_standing_charges_graphql = AsyncMock(return_value=0.53)

    mock_response = AsyncMock()
    mock_response.status = 410
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

    assert result == 0.53
    api.async_fetch_standing_charges_graphql.assert_called_once_with("1900000000456")


def test_fetch_standing_charges_graphql_returns_value():
    """async_fetch_standing_charges_graphql() converts GraphQL value to pounds/day."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(
        return_value={
            "applicableStandingCharges": [
                {"value": 61.95, "validFrom": "2026-04-10T00:00:00Z", "validTo": None},
            ]
        }
    )

    result = asyncio.run(api.async_fetch_standing_charges_graphql("1900000000456"))

    # 61.95 pence/day → 0.6195 pounds/day
    assert result is not None
    assert abs(result - 0.6195) < 1e-6


def test_fetch_standing_charges_graphql_returns_none_on_empty():
    """async_fetch_standing_charges_graphql() returns None when applicableStandingCharges is empty."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(return_value={"applicableStandingCharges": []})

    result = asyncio.run(api.async_fetch_standing_charges_graphql("1900000000456"))
    assert result is None


def test_fetch_standing_charges_graphql_returns_none_on_graphql_failure():
    """async_fetch_standing_charges_graphql() returns None when GraphQL query fails."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(return_value=None)

    result = asyncio.run(api.async_fetch_standing_charges_graphql("1900000000456"))
    assert result is None


def test_fetch_standing_charges_transient_error_does_not_fall_back_to_graphql():
    """async_fetch_standing_charges() returns None (no fallback) for transient errors like 500/429."""
    for status_code in (429, 500, 503):
        api = make_kraken_api()
        api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
        api.import_mpan = "1900000000456"
        api.async_fetch_standing_charges_graphql = AsyncMock()

        mock_response = AsyncMock()
        mock_response.status = status_code
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

        assert result is None, f"Expected None for HTTP {status_code}, got {result}"
        api.async_fetch_standing_charges_graphql.assert_not_called()


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


def test_email_auth_obtains_token_when_oauth_mixin_is_base():
    """Regression: email auth must obtain a token even when OAuthMixin is _AUTH_BASE.

    Bug: when both oauth_mixin and kraken_auth_mixin are present, OAuthMixin becomes
    _AUTH_BASE.  The old hasattr(self, '_init_kraken_auth') check returned False because
    KrakenAPI only inherited OAuthMixin.  OAuthMixin._init_oauth() sets access_token=None
    and check_and_refresh_oauth_token() returns True without obtaining a token, causing
    "Warn: Kraken: No access token for find-tariffs".

    Fix: use module-level _KrakenAuthMixin reference and bind its methods to the instance.
    """
    import kraken as kraken_module

    assert kraken_module._KrakenAuthMixin is not None, "KrakenAuthMixin must be importable for this test"

    api = make_kraken_api(auth_method="email", email="user@eon.com", password="secret123", key=None)

    # access_token must be None before first auth — KrakenAuthMixin lazy-obtains on first call
    assert api.access_token is None, "access_token should be None before first auth"

    # Mock _kraken_token_request so no real HTTP call is made
    api._kraken_token_request = AsyncMock(
        return_value={
            "token": "email-jwt-token",
            "refreshToken": "email-refresh-token",
            "exp": int(time.time()) + 3600,
        }
    )

    result = asyncio.run(api.check_and_refresh_oauth_token())
    assert result is True, "check_and_refresh_oauth_token must return True on success"
    assert api.access_token == "email-jwt-token", "access_token must be set after email auth (was None — OAuthMixin bug)"


def test_api_key_auth_obtains_token_when_oauth_mixin_is_base():
    """Regression: api_key auth must obtain a token even when OAuthMixin is _AUTH_BASE."""
    import kraken as kraken_module

    assert kraken_module._KrakenAuthMixin is not None, "KrakenAuthMixin must be importable for this test"

    api = make_kraken_api(auth_method="api_key", key="sk_live_test123")
    assert api.access_token is None

    api._kraken_token_request = AsyncMock(
        return_value={
            "token": "api-key-jwt-token",
            "refreshToken": "api-key-refresh-token",
            "exp": int(time.time()) + 3600,
        }
    )

    result = asyncio.run(api.check_and_refresh_oauth_token())
    assert result is True
    assert api.access_token == "api-key-jwt-token", "access_token must be set after api_key auth"


def test_oauth_mode_unaffected_by_kraken_auth_mixin():
    """Regression: OAuthMixin OAuth mode must still work (SaaS scenario).

    Ensure the fix for email/api_key auth does not break SaaS users who use
    auth_method='oauth' with a pre-issued access_token.
    """
    api = make_kraken_api(auth_method="oauth", key="saas-access-token", token_expires_at="2099-01-01T00:00:00Z")
    # For OAuth mode, OAuthMixin._init_oauth() is called and access_token = key
    assert api.access_token == "saas-access-token", "OAuth mode must preserve the pre-issued access_token"
    # Token is valid (far future expiry) — must return True without refresh
    result = asyncio.run(api.check_and_refresh_oauth_token())
    assert result is True


def test_find_tariffs_stores_import_mpan():
    """async_find_tariffs() must set self.import_mpan from the discovered import meter point."""
    api = make_kraken_api()
    account_data = {
        "account": {
            "number": "A-TEST123",
            "properties": [
                {
                    "address": "1 Test Street",
                    "electricityMeterPoints": [
                        {
                            "mpan": "1900000000456",
                            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "SMART-V16", "tariffCode": "E-1R-SMART-V16-J", "displayName": "Smart Saver"}}],
                        }
                    ],
                }
            ],
        },
    }
    api.async_graphql_query = AsyncMock(return_value=account_data)
    api._discover_export_tariff = AsyncMock()
    asyncio.run(api.async_find_tariffs())
    assert api.import_mpan == "1900000000456"


def test_fetch_rates_graphql_parses_applicable_rates():
    """async_fetch_rates_graphql() converts GraphQL value/validFrom/validTo to REST-compatible shape."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(
        return_value={
            "applicableRates": [
                {"value": 24.57, "validFrom": "2026-04-10T00:00:00Z", "validTo": "2026-04-11T00:00:00Z"},
                {"value": 24.57, "validFrom": "2026-04-11T00:00:00Z", "validTo": "2026-04-12T00:00:00Z"},
            ]
        }
    )

    result = asyncio.run(api.async_fetch_rates_graphql("1900000000456"))

    assert result is not None
    assert len(result) == 2
    # value_inc_vat must be the raw value from the API (pence/kWh inc VAT)
    assert result[0]["value_inc_vat"] == 24.57
    # value_exc_vat must be value / 1.05, rounded to 4dp
    assert result[0]["value_exc_vat"] == round(24.57 / 1.05, 4)
    # Timestamps must be passed through unchanged
    assert result[0]["valid_from"] == "2026-04-10T00:00:00Z"
    assert result[0]["valid_to"] == "2026-04-11T00:00:00Z"


def test_fetch_rates_graphql_normalizes_null_timestamps():
    """async_fetch_rates_graphql() applies _normalize_rate_timestamps to handle null valid_from."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(
        return_value={
            "applicableRates": [
                {"value": 28.0, "validFrom": None, "validTo": "2026-04-12T00:00:00Z"},
            ]
        }
    )

    result = asyncio.run(api.async_fetch_rates_graphql("1900000000456"))

    assert result is not None
    assert len(result) == 1
    # Normalization must replace null valid_from with a real timestamp
    assert result[0]["valid_from"] is not None
    from datetime import datetime

    datetime.fromisoformat(result[0]["valid_from"].replace("Z", "+00:00"))


def test_fetch_rates_graphql_returns_none_on_empty_response():
    """async_fetch_rates_graphql() returns None when applicableRates is empty."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(return_value={"applicableRates": []})

    result = asyncio.run(api.async_fetch_rates_graphql("1900000000456"))
    assert result is None


def test_fetch_rates_graphql_returns_none_on_graphql_failure():
    """async_fetch_rates_graphql() returns None when GraphQL query fails."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(return_value=None)

    result = asyncio.run(api.async_fetch_rates_graphql("1900000000456"))
    assert result is None


def test_fetch_rates_graphql_window_derived_from_forecast_hours():
    """async_fetch_rates_graphql() derives start/end from forecast_hours, not a hard-coded 2-day window.

    With forecast_hours=72 (3 days), forecast_days=3 and end_at should be midnight + 4 days.
    start_at should be midnight - 1 day (to capture rate periods that started earlier today).
    """
    from datetime import datetime, timedelta, timezone
    import re

    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.get_arg = MagicMock(side_effect=lambda arg, default=None, **kwargs: 72 if arg == "forecast_hours" else default)

    captured_query = {}

    async def capture_query(query, context):
        captured_query["query"] = query
        return {"applicableRates": [{"value": 24.0, "validFrom": "2026-04-10T00:00:00Z", "validTo": "2026-04-14T00:00:00Z"}]}

    api.async_graphql_query = capture_query

    asyncio.run(api.async_fetch_rates_graphql("1900000000456"))

    query = captured_query.get("query", "")
    # Extract startAt and endAt from the query string
    start_match = re.search(r'startAt:\s*"([^"]+)"', query)
    end_match = re.search(r'endAt:\s*"([^"]+)"', query)
    assert start_match and end_match, f"Could not find startAt/endAt in query: {query}"

    start_dt = datetime.fromisoformat(start_match.group(1).replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_match.group(1).replace("Z", "+00:00"))

    now = datetime.now(timezone.utc)
    midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # start_at must be midnight - 1 day (within a small tolerance for test timing)
    expected_start = midnight_utc - timedelta(days=1)
    assert abs((start_dt - expected_start).total_seconds()) < 60, f"start_at {start_dt} not close to expected {expected_start}"

    # forecast_hours=72 → forecast_days=3 → end_at must be midnight + 4 days
    expected_end = midnight_utc + timedelta(days=4)
    assert abs((end_dt - expected_end).total_seconds()) < 60, f"end_at {end_dt} not close to expected {expected_end}"


def test_fetch_rates_graphql_window_default_forecast_hours():
    """async_fetch_rates_graphql() uses forecast_hours default of 48 when not configured.

    forecast_hours=48 → forecast_days=2 → end_at = midnight + 3 days.
    """
    from datetime import datetime, timedelta, timezone
    import re

    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    # No forecast_hours in args — should default to 48
    api.get_arg = MagicMock(side_effect=lambda arg, default=None, **kwargs: default)

    captured_query = {}

    async def capture_query(query, context):
        captured_query["query"] = query
        return {"applicableRates": [{"value": 24.0, "validFrom": "2026-04-10T00:00:00Z", "validTo": "2026-04-13T00:00:00Z"}]}

    api.async_graphql_query = capture_query

    asyncio.run(api.async_fetch_rates_graphql("1900000000456"))

    query = captured_query.get("query", "")
    end_match = re.search(r'endAt:\s*"([^"]+)"', query)
    assert end_match, f"Could not find endAt in query: {query}"

    end_dt = datetime.fromisoformat(end_match.group(1).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # default forecast_hours=48 → forecast_days=2 → end_at must be midnight + 3 days
    expected_end = midnight_utc + timedelta(days=3)
    assert abs((end_dt - expected_end).total_seconds()) < 60, f"end_at {end_dt} not close to expected {expected_end}"


def test_fetch_rates_rest_404_falls_back_to_graphql_for_import():
    """async_fetch_rates() falls back to GraphQL applicableRates when REST returns 404 on import tariff.

    Reproduces the Derek scenario: NEXT_SMART_SAVER_FIXED_12M_V6 removed from the E.ON API
    but the customer is still on that tariff — REST 404, GraphQL fallback should succeed.
    """
    api = make_kraken_api(provider="eon", account_id="A-AA8A473C")
    api.current_tariff = {"tariff_code": "E-1R-NEXT_SMART_SAVER_FIXED_12M_V6-J", "product_code": "NEXT_SMART_SAVER_FIXED_12M_V6"}
    api.import_mpan = "1900000000456"

    graphql_rates = [
        {"value_inc_vat": 24.57, "value_exc_vat": round(24.57 / 1.05, 4), "valid_from": "2026-04-10T00:00:00Z", "valid_to": "2026-04-12T00:00:00Z"},
    ]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    mock_response = AsyncMock()
    mock_response.status = 404
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

    assert result is graphql_rates
    api.async_fetch_rates_graphql.assert_called_once_with("1900000000456", account_id="A-AA8A473C")


def test_fetch_rates_rest_404_no_fallback_without_import_mpan():
    """async_fetch_rates() returns None (no fallback) when REST 404 and import_mpan is not set."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-OLD-J", "product_code": "OLD"}
    api.import_mpan = None  # Not yet discovered

    mock_response = AsyncMock()
    mock_response.status = 404
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

    assert result is None


def test_fetch_rates_rest_404_falls_back_to_graphql_for_export():
    """async_fetch_rates() falls back to GraphQL applicableRates on export REST 404.

    EDF SEG / export tariffs are frequently registered on the meter point while their
    /standard-unit-rates/ REST endpoint returns 404. The fallback must recover them using
    the export MPAN so the customer's export rates are not silently lost.
    """
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"}
    api.import_mpan = "1900000000456"
    export_tariff = {"tariff_code": "E-1R-EDF_EXPORT_SEG_12M_HH-B", "product_code": "EDF_EXPORT_SEG_12M"}
    api.export_tariff = export_tariff
    api.export_mpan = "2000000000789"

    graphql_rates = [{"value_inc_vat": 15.0, "value_exc_vat": round(15.0 / 1.05, 4), "valid_from": "2026-04-10T00:00:00Z", "valid_to": "2026-04-12T00:00:00Z"}]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    mock_response = AsyncMock()
    mock_response.status = 404
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
        result = asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    assert result is graphql_rates
    # Export fallback must use the export MPAN, defaulting to the import account here.
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-TEST123")


def test_fetch_rates_export_404_uses_export_account_for_split_accounts():
    """For E.ON split import/export accounts, the export fallback queries the export account."""
    api = make_kraken_api(export_account_id="A-EXPORT456")
    api.current_tariff = {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"}
    api.import_mpan = "1900000000456"
    export_tariff = {"tariff_code": "E-1R-OUTGOING-FIX-12M-J", "product_code": "OUTGOING-FIX-12M"}
    api.export_tariff = export_tariff
    api.export_mpan = "2000000000789"

    api.async_fetch_rates_graphql = AsyncMock(return_value=[])

    mock_response = AsyncMock()
    mock_response.status = 404
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
        asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-EXPORT456")


def test_fetch_rates_rest_410_falls_back_to_graphql_for_import():
    """async_fetch_rates() also falls back on HTTP 410 Gone (product permanently removed)."""
    api = make_kraken_api(provider="eon", account_id="A-AA8A473C")
    api.current_tariff = {"tariff_code": "E-1R-GONE-V1-J", "product_code": "GONE-V1"}
    api.import_mpan = "1900000000456"

    graphql_rates = [{"value_inc_vat": 24.57, "value_exc_vat": round(24.57 / 1.05, 4), "valid_from": "2026-04-10T00:00:00Z", "valid_to": "2026-04-12T00:00:00Z"}]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    mock_response = AsyncMock()
    mock_response.status = 410
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

    assert result is graphql_rates
    api.async_fetch_rates_graphql.assert_called_once_with("1900000000456", account_id="A-AA8A473C")


def test_fetch_rates_transient_error_does_not_fall_back_to_graphql():
    """async_fetch_rates() returns None (no GraphQL fallback) for transient errors like 500/429."""
    for status_code in (429, 500, 503):
        api = make_kraken_api()
        api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
        api.import_mpan = "1900000000456"
        api.async_fetch_rates_graphql = AsyncMock()

        mock_response = AsyncMock()
        mock_response.status = status_code
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

        assert result is None, f"Expected None for HTTP {status_code}, got {result}"
        api.async_fetch_rates_graphql.assert_not_called()


def test_find_active_tariff_uses_direction_for_seg_export_code():
    """Export detection must use meterPoint.direction, not the 'EXPORT' substring.

    Regression for missing customer export rates: many EDF/E.ON export tariff codes
    (SEG, outgoing, agile export) do NOT contain the literal string 'EXPORT'. Keyed off
    the tariff code alone these were never matched; keyed off direction they are.
    """
    api = make_kraken_api()
    meter_points = [
        {
            "mpan": "1900000000456",
            "direction": "IMPORT",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "VAR-01", "tariffCode": "E-1R-VAR-01-J", "displayName": "Import"}}],
        },
        {
            "mpan": "2000000000789",
            "direction": "EXPORT",
            # Note: tariff code contains NO 'EXPORT' substring — only direction identifies it.
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "EDF_SEG_12M", "tariffCode": "E-1R-EDF_SEG_12M-B", "displayName": "SEG"}}],
        },
    ]

    export = api._find_active_tariff(meter_points, is_export=True)
    assert export is not None, "Export tariff should be found via direction=EXPORT"
    assert export["tariff_code"] == "E-1R-EDF_SEG_12M-B"
    assert export["mpan"] == "2000000000789"

    # And import discovery must NOT pick up the SEG export meter.
    imp = api._find_active_tariff(meter_points, is_export=False)
    assert imp["tariff_code"] == "E-1R-VAR-01-J"


def test_find_active_tariff_direction_overrides_export_substring():
    """direction=IMPORT wins even when the tariff code contains 'EXPORT'."""
    api = make_kraken_api()
    meter_points = [
        {
            "mpan": "1900000000456",
            "direction": "IMPORT",
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "EXPORTVALUE-01", "tariffCode": "E-1R-EXPORTVALUE-01-J", "displayName": "Import"}}],
        },
    ]
    assert api._find_active_tariff(meter_points, is_export=False) is not None
    assert api._find_active_tariff(meter_points, is_export=True) is None


def test_find_active_tariff_falls_back_to_substring_without_direction():
    """When direction is absent/null, detection falls back to the tariff-code substring."""
    api = make_kraken_api()
    meter_points = [
        {
            "mpan": "2000000000789",
            # No 'direction' key at all (older API response / cached shape).
            "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": {"productCode": "EXPORT-FIX-01", "tariffCode": "E-1R-EXPORT-FIX-01-J", "displayName": "Export"}}],
        },
    ]
    export = api._find_active_tariff(meter_points, is_export=True)
    assert export is not None
    assert export["tariff_code"] == "E-1R-EXPORT-FIX-01-J"


def _mk_get_ctx(status, json_body=None):
    """Build a mock aiohttp GET context manager yielding a response of the given status."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {})
    resp.text = AsyncMock(return_value="body")
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def test_build_rest_auth_uses_basic_auth_for_api_key():
    """_build_rest_auth returns HTTP Basic auth (API key as username) in api_key mode."""
    import aiohttp

    api = make_kraken_api()  # api_key mode → _api_key = "test-key"
    auth, headers = asyncio.run(api._build_rest_auth())
    assert isinstance(auth, aiohttp.BasicAuth)
    assert auth.login == "test-key"
    assert headers == {}


def test_build_rest_auth_uses_jwt_header_for_oauth():
    """_build_rest_auth refreshes and returns a JWT bearer header when no API key is set."""
    api = make_kraken_api()
    api._api_key = None  # OAuth has no API key
    api.access_token = "jwt-abc"
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)

    auth, headers = asyncio.run(api._build_rest_auth())
    assert auth is None
    assert headers.get("Authorization") == "JWT jwt-abc"
    api.check_and_refresh_oauth_token.assert_awaited_once()


def test_fetch_rates_404_retries_authenticated_and_succeeds():
    """A 404 on the public REST endpoint retries WITH auth; private SEG export rates then load.

    Reproduces the live EDF case: E-1R-EDF_EXPORT_SEG_12M_HH-B (a private product) 404s
    unauthenticated, so the authenticated retry is what actually recovers the export rates.
    """
    api = make_kraken_api()  # api_key mode → authenticated retry uses HTTP Basic auth
    export_tariff = {"tariff_code": "E-1R-EDF_EXPORT_SEG_12M_HH-B", "product_code": "EDF_EXPORT_SEG_12M"}
    api.export_tariff = export_tariff
    api.export_mpan = "1170001829927"

    rates_body = {"count": 1, "next": None, "results": [{"value_inc_vat": 15.0, "value_exc_vat": 14.29, "valid_from": "2026-07-08T00:00:00Z", "valid_to": "2026-07-08T00:30:00Z"}]}

    call_count = [0]

    def get_side_effect(url, **kwargs):
        call_count[0] += 1
        # First call is unauthenticated (404), retry is authenticated (200).
        return _mk_get_ctx(404) if call_count[0] == 1 else _mk_get_ctx(200, rates_body)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=get_side_effect)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    assert result is not None and len(result) == 1
    assert result[0]["value_inc_vat"] == 15.0
    assert call_count[0] == 2, "expected an unauthenticated attempt then an authenticated retry"
    # The first attempt carries no auth; the retry carries HTTP Basic auth.
    assert mock_session.get.call_args_list[0].kwargs.get("auth") is None
    assert mock_session.get.call_args_list[1].kwargs.get("auth") is not None


def test_fetch_rates_404_authenticated_retry_still_404_falls_back_to_graphql():
    """If the authenticated retry also 404s, we still fall back to GraphQL applicableRates."""
    api = make_kraken_api()
    export_tariff = {"tariff_code": "E-1R-EXPORT-01-J", "product_code": "EXPORT-01"}
    api.export_tariff = export_tariff
    api.export_mpan = "2000000000789"

    graphql_rates = [{"value_inc_vat": 12.0, "value_exc_vat": round(12.0 / 1.05, 4), "valid_from": "2026-07-08T00:00:00Z", "valid_to": None}]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(404))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    assert result is graphql_rates
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-TEST123")
    # Recovered via GraphQL — the expected private-product 404 must NOT count as a failure.
    assert api.failures_total == 0


def test_fetch_rates_404_graphql_fallback_empty_counts_one_failure():
    """When REST 404s AND the GraphQL fallback returns nothing, that is a genuine failure."""
    api = make_kraken_api()
    export_tariff = {"tariff_code": "E-1R-EXPORT-01-J", "product_code": "EXPORT-01"}
    api.export_tariff = export_tariff
    api.export_mpan = "2000000000789"
    api.async_fetch_rates_graphql = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(404))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    assert result is None
    assert api.failures_total == 1


def test_connection_nodes_extracts_edges():
    """_connection_nodes returns node dicts from a Relay connection and tolerates other shapes."""
    from kraken import KrakenAPI

    conn = {"edges": [{"node": {"value": 1}}, {"node": {"value": 2}}, {}, None]}
    assert KrakenAPI._connection_nodes(conn) == [{"value": 1}, {"value": 2}]
    # Backward-compat: a plain list is returned unchanged.
    assert KrakenAPI._connection_nodes([{"value": 3}]) == [{"value": 3}]
    # Empty / missing shapes → [].
    assert KrakenAPI._connection_nodes(None) == []
    assert KrakenAPI._connection_nodes({}) == []
    assert KrakenAPI._connection_nodes({"edges": []}) == []


def test_fetch_rates_graphql_parses_connection_edges():
    """async_fetch_rates_graphql parses the applicableRates Relay connection (edges/node)."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(
        return_value={
            "applicableRates": {
                "edges": [
                    {"node": {"value": 15.0, "validFrom": "2026-07-08T00:00:00Z", "validTo": "2026-07-08T00:30:00Z"}},
                    {"node": {"value": 16.0, "validFrom": "2026-07-08T00:30:00Z", "validTo": "2026-07-08T01:00:00Z"}},
                ]
            }
        }
    )

    result = asyncio.run(api.async_fetch_rates_graphql("1170001829927"))
    assert result is not None and len(result) == 2
    assert result[0]["value_inc_vat"] == 15.0
    assert result[1]["value_inc_vat"] == 16.0
    assert result[0]["valid_from"] == "2026-07-08T00:00:00Z"


def test_fetch_rates_graphql_paginates_via_cursor():
    """async_fetch_rates_graphql walks all connection pages via pageInfo.endCursor."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"

    page1 = {
        "applicableRates": {
            "edges": [{"node": {"value": 10.0, "validFrom": "2026-07-08T00:00:00Z", "validTo": "2026-07-08T00:30:00Z"}}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        }
    }
    page2 = {
        "applicableRates": {
            "edges": [{"node": {"value": 11.0, "validFrom": "2026-07-08T00:30:00Z", "validTo": "2026-07-08T01:00:00Z"}}],
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
        }
    }

    captured = []

    async def fake_query(query, context):
        captured.append(query)
        return page1 if len(captured) == 1 else page2

    api.async_graphql_query = fake_query

    result = asyncio.run(api.async_fetch_rates_graphql("1170001829927"))

    assert result is not None and len(result) == 2, "both pages should be accumulated"
    assert [r["value_inc_vat"] for r in result] == [10.0, 11.0]
    # Two requests: first with after: null, second carrying the page-1 cursor.
    assert len(captured) == 2
    assert "after: null" in captured[0]
    assert 'after: "cursor-1"' in captured[1]


def test_fetch_standing_charges_graphql_parses_connection_edges():
    """async_fetch_standing_charges_graphql parses the applicableStandingCharges connection."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(
        return_value={
            "applicableStandingCharges": {
                "edges": [
                    {"node": {"value": 61.95, "validFrom": "2026-07-08T00:00:00Z", "validTo": None}},
                ]
            }
        }
    )

    result = asyncio.run(api.async_fetch_standing_charges_graphql("1170001829927"))
    assert result is not None
    assert abs(result - 0.6195) < 1e-6


def test_save_and_load_kraken_cache_round_trip():
    """save_kraken_cache persists state that load_kraken_cache restores into a fresh instance."""
    from datetime import datetime

    api = make_kraken_api()
    attach_fake_storage(api)
    api.current_tariff = {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"}
    api.export_tariff = {"tariff_code": "E-1R-EDF_EXPORT_SEG_12M_HH-B", "product_code": "EDF_EXPORT_SEG_12M"}
    api.import_mpan = "1100000946808"
    api.export_mpan = "1170001829927"
    api.import_rates = [{"value_inc_vat": 28.6, "valid_from": "2026-07-08T00:00:00Z", "valid_to": None}]
    api.export_rates = [{"value_inc_vat": 15.0, "valid_from": "2026-07-08T00:00:00Z", "valid_to": None}]
    api.import_standing_charge = 0.5676
    api.export_rates_available = True
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()

    asyncio.run(api.save_kraken_cache())

    # Fresh instance sharing the same storage restores everything.
    api2 = make_kraken_api(account_id="A-TEST123")
    api2.base.components = api.base.components
    api2.update_success_timestamp = MagicMock()
    asyncio.run(api2.load_kraken_cache())

    assert api2.current_tariff == {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"}
    assert api2.export_tariff["tariff_code"] == "E-1R-EDF_EXPORT_SEG_12M_HH-B"
    assert api2.import_mpan == "1100000946808"
    assert api2.export_mpan == "1170001829927"
    assert api2.import_rates[0]["value_inc_vat"] == 28.6
    assert api2.export_rates[0]["value_inc_vat"] == 15.0
    assert api2.import_standing_charge == 0.5676
    assert api2.export_rates_available is True


def test_run_first_restores_cache_and_skips_fetch():
    """On first run a fresh cache is restored, sensors are published, and no API calls are made."""
    from datetime import datetime

    api = make_kraken_api()
    storage = attach_fake_storage(api)
    storage.data[("kraken", "account_A-TEST123")] = {
        "current_tariff": {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"},
        "export_tariff": {"tariff_code": "E-1R-EXPORT-01-J", "product_code": "EXPORT-01"},
        "import_mpan": "1100000946808",
        "export_mpan": "1170001829927",
        "export_account_id": None,
        "import_rates": [{"value_inc_vat": 28.6, "valid_from": "2026-07-08T00:00:00Z", "valid_to": None}],
        "export_rates": [{"value_inc_vat": 15.0, "valid_from": "2026-07-08T00:00:00Z", "valid_to": None}],
        "import_standing_charge": 0.5676,
        "export_rates_available": True,
        "tariff_fetched_at": datetime.now(),
        "rates_fetched_at": datetime.now(),
    }
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.async_fetch_rates = AsyncMock(return_value=None)
    api.async_fetch_standing_charges = AsyncMock(return_value=None)
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(0, True))

    assert result is True
    # Fresh cache → no API calls at all.
    api.async_find_tariffs.assert_not_called()
    api.async_fetch_rates.assert_not_called()
    api.async_fetch_standing_charges.assert_not_called()
    # Sensors published from cache.
    published = [c.args[0] for c in api.dashboard_item.call_args_list]
    assert api.get_entity_name("sensor", "import_rates") in published
    assert api.get_entity_name("sensor", "export_rates") in published
    # Both import and export wired from cached data.
    api.set_arg.assert_any_call("metric_octopus_import", api.get_entity_name("sensor", "import_rates"))
    api.set_arg.assert_any_call("metric_octopus_export", api.get_entity_name("sensor", "export_rates"))


def test_data_age_minutes():
    """_data_age_minutes handles None, datetimes, and ISO strings."""
    from datetime import datetime, timedelta
    from kraken import KrakenAPI

    assert KrakenAPI._data_age_minutes(None) >= 9999
    recent = datetime.now() - timedelta(minutes=5)
    assert 4 < KrakenAPI._data_age_minutes(recent) < 6
    # ISO string (as YAML/JSON might round-trip) is parsed.
    iso = (datetime.now() - timedelta(minutes=20)).isoformat()
    assert 19 < KrakenAPI._data_age_minutes(iso) < 21
    assert KrakenAPI._data_age_minutes("not-a-date") >= 9999


def test_discover_smart_devices_filters_live_ev():
    """async_discover_smart_devices keeps only LIVE ELECTRIC_VEHICLES devices."""
    api = make_kraken_api()
    api.async_graphql_query = AsyncMock(
        return_value={
            "devices": [
                {"id": "dev-ev-1", "deviceType": "ELECTRIC_VEHICLES", "status": {"current": "LIVE"}, "__typename": "SmartFlexVehicle", "make": "Tesla", "model": "M3", "provider": "JEDLIX"},
                {"id": "dev-meter", "deviceType": "ELECTRICITY_METERS", "status": {"current": "LIVE"}, "__typename": "SmartFlexDevice"},
                {"id": "dev-ev-suspended", "deviceType": "ELECTRIC_VEHICLES", "status": {"current": "SUSPENDED"}, "__typename": "SmartFlexVehicle"},
            ]
        }
    )
    asyncio.run(api.async_discover_smart_devices())
    assert set(api.intelligent_devices) == {"dev-ev-1"}
    dev = api.intelligent_devices["dev-ev-1"]
    assert dev["make"] == "Tesla" and dev["is_charger"] is False
    assert dev["planned_dispatches"] == [] and dev["completed_dispatches"] == []


def test_normalize_dispatches_field_mapping():
    """_normalize_dispatches maps planned (energyAddedKwh/type) and completed (delta/meta) shapes."""
    api = make_kraken_api()
    planned = api._normalize_dispatches(
        [
            {"start": "2026-07-08T00:00:00Z", "end": "2026-07-08T00:30:00Z", "type": "SMART", "energyAddedKwh": "2.5"},
            {"start": None, "end": "x"},  # missing start → dropped
        ],
        completed=False,
    )
    assert planned == [{"start": "2026-07-08T00:00:00Z", "end": "2026-07-08T00:30:00Z", "charge_in_kwh": 2.5, "source": "SMART", "location": None}]

    completed = api._normalize_dispatches(
        [{"start": "2026-07-07T00:00:00Z", "end": "2026-07-07T00:30:00Z", "delta": 1.25, "meta": {"source": "SMART", "location": "AT_HOME"}}],
        completed=True,
    )
    assert completed[0]["charge_in_kwh"] == 1.25 and completed[0]["source"] == "SMART" and completed[0]["location"] == "AT_HOME"


def test_normalize_dispatches_trims_in_progress_planned():
    """A planned dispatch already in progress is trimmed: start advances to now, energy scaled."""
    from datetime import datetime, timezone, timedelta

    api = make_kraken_api()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")  # started 30m ago
    end = (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")  # ends in 30m (half elapsed)

    planned = api._normalize_dispatches([{"start": start, "end": end, "type": "SMART", "energyAddedKwh": 10.0}], completed=False)
    assert len(planned) == 1
    # ~half the window remains → ~half the energy, and start advanced to ~now.
    assert 4.5 < planned[0]["charge_in_kwh"] < 5.5
    trimmed_start = datetime.fromisoformat(planned[0]["start"])
    assert abs((trimmed_start - now).total_seconds()) < 60

    # A future (not-yet-started) planned dispatch is left untouched.
    future_start = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_end = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = api._normalize_dispatches([{"start": future_start, "end": future_end, "energyAddedKwh": 8.0}], completed=False)
    assert future[0]["start"] == future_start and future[0]["charge_in_kwh"] == 8.0


def test_normalize_dispatches_does_not_trim_completed():
    """Completed dispatches are historical and never trimmed, even if end is in the future."""
    from datetime import datetime, timezone, timedelta

    api = make_kraken_api()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed = api._normalize_dispatches([{"start": start, "end": end, "delta": 10.0, "meta": {}}], completed=True)
    assert completed[0]["start"] == start and completed[0]["charge_in_kwh"] == 10.0


def test_merge_completed_dispatches_dedup_and_prune():
    """_merge_completed_dispatches dedup by start (newest wins) and prunes entries > history window."""
    from datetime import datetime, timezone, timedelta

    api = make_kraken_api()
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cached = [
        {"start": old, "end": old, "charge_in_kwh": 1.0},  # pruned (older than history window)
        {"start": recent, "end": recent, "charge_in_kwh": 1.0},  # overwritten by new
    ]
    new = [{"start": recent, "end": recent, "charge_in_kwh": 2.0}]
    merged = api._merge_completed_dispatches(cached, new)
    assert old not in [d["start"] for d in merged]
    assert len(merged) == 1 and merged[0]["charge_in_kwh"] == 2.0


def test_fetch_dispatches_populates_device():
    """async_fetch_dispatches fills planned + completed dispatches for each known device."""
    api = make_kraken_api()
    api.intelligent_devices = {"dev-1": {"device_id": "dev-1", "planned_dispatches": [], "completed_dispatches": []}}
    api.async_graphql_query = AsyncMock(
        return_value={
            "flexPlannedDispatches": [{"start": "2026-07-08T02:00:00Z", "end": "2026-07-08T05:00:00Z", "type": "SMART", "energyAddedKwh": 10.0}],
            "completedDispatches": [{"start": "2026-07-08T00:00:00Z", "end": "2026-07-08T01:00:00Z", "delta": 3.0, "meta": {"source": "SMART", "location": "AT_HOME"}}],
        }
    )
    asyncio.run(api.async_fetch_dispatches())
    dev = api.intelligent_devices["dev-1"]
    assert len(dev["planned_dispatches"]) == 1 and dev["planned_dispatches"][0]["charge_in_kwh"] == 10.0
    assert len(dev["completed_dispatches"]) == 1 and dev["completed_dispatches"][0]["charge_in_kwh"] == 3.0


def test_publish_dispatch_sensors_active_state_and_wiring():
    """_publish_dispatch_sensors publishes an on/off binary_sensor and wires slot + num_cars."""
    from datetime import datetime, timezone, timedelta

    api = make_kraken_api()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    api.intelligent_devices = {"aaa-bbb-12345": {"device_id": "aaa-bbb-12345", "planned_dispatches": [{"start": start, "end": end, "charge_in_kwh": 5.0}], "completed_dispatches": []}}
    api.dashboard_item = MagicMock()
    captured = {}
    api.set_arg = MagicMock(side_effect=lambda k, v: captured.__setitem__(k, v))
    api.get_arg = MagicMock(return_value=0)  # num_cars currently 0

    api._publish_dispatch_sensors()

    entity = api.get_entity_name("binary_sensor", "intelligent_dispatch_12345")
    call = [c for c in api.dashboard_item.call_args_list if c.args[0] == entity][0]
    assert call.args[1] == "on", "a dispatch spanning now should make the sensor active"
    # Wired to the sensor list, and num_cars bumped so fetch.py actually consumes it.
    assert captured["octopus_intelligent_slot"] == [entity]
    assert captured["num_cars"] == 1


def test_run_fetches_and_wires_dispatches():
    """On a dispatch-due cycle, run() fetches dispatches and wires octopus_intelligent_slot."""
    from datetime import datetime

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()  # fresh → no tariff work / device re-discovery
    api.rates_fetched_at = datetime.now()  # fresh → no rate work
    api.dispatch_fetched_at = None  # dispatch due
    api.intelligent_devices = {"dev-1": {"device_id": "dev-1", "planned_dispatches": [], "completed_dispatches": []}}
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.async_discover_smart_devices = AsyncMock()

    async def fetch_disp():
        api.intelligent_devices["dev-1"]["planned_dispatches"] = [{"start": "2026-07-08T02:00:00Z", "end": "2026-07-08T05:00:00Z", "charge_in_kwh": 10.0}]

    api.async_fetch_dispatches = AsyncMock(side_effect=fetch_disp)
    api.dashboard_item = MagicMock()
    captured = {}
    api.set_arg = MagicMock(side_effect=lambda k, v: captured.__setitem__(k, v))
    api.get_arg = MagicMock(return_value=0)
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(120, False))

    assert result is True
    api.async_discover_smart_devices.assert_not_called()  # tariff fresh
    api.async_fetch_dispatches.assert_called_once()
    assert captured.get("octopus_intelligent_slot") == [api.get_entity_name("binary_sensor", "intelligent_dispatch_1")]


def test_run_no_devices_does_not_save_cache_every_cycle():
    """With no SmartFlex devices and fresh tariff/rates, run() must not persist the cache each cycle.

    Regression: dispatch_due stays permanently true when there are no devices (dispatch_fetched_at
    is never set), so the save guard must key off an actual dispatch refresh, not dispatch_due.
    """
    from datetime import datetime

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()  # fresh
    api.rates_fetched_at = datetime.now()  # fresh
    api.dispatch_fetched_at = None  # no devices → never set → dispatch_due would be True
    api.intelligent_devices = {}  # common no-EV account
    api.async_find_tariffs = AsyncMock(return_value=None)
    api.save_kraken_cache = AsyncMock()
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(120, False))

    assert result is True
    api.save_kraken_cache.assert_not_called()


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
        test_run_refetches_rates_when_stale,
        test_run_skips_refetch_when_cache_fresh,
        test_run_first_restores_cache_and_skips_fetch,
        test_save_and_load_kraken_cache_round_trip,
        test_data_age_minutes,
        test_discover_smart_devices_filters_live_ev,
        test_normalize_dispatches_field_mapping,
        test_normalize_dispatches_trims_in_progress_planned,
        test_normalize_dispatches_does_not_trim_completed,
        test_merge_completed_dispatches_dedup_and_prune,
        test_fetch_dispatches_populates_device,
        test_publish_dispatch_sensors_active_state_and_wiring,
        test_run_fetches_and_wires_dispatches,
        test_run_no_devices_does_not_save_cache_every_cycle,
        test_run_wires_export_when_discovered,
        test_find_active_tariff_prefers_configured_mpan,
        test_standing_charge_converts_pence_to_pounds,
        test_export_discovery_clears_stale_when_not_found,
        test_export_discovery_strategy1_no_fallthrough_on_network_failure,
        test_normalize_rate_timestamps_flat_rate_both_null,
        test_normalize_rate_timestamps_normal_rates_unchanged,
        test_normalize_rate_timestamps_empty_list,
        test_normalize_rate_timestamps_mixed_null_and_real,
        test_email_auth_obtains_token_when_oauth_mixin_is_base,
        test_api_key_auth_obtains_token_when_oauth_mixin_is_base,
        test_oauth_mode_unaffected_by_kraken_auth_mixin,
        test_find_tariffs_stores_import_mpan,
        test_fetch_rates_graphql_parses_applicable_rates,
        test_fetch_rates_graphql_normalizes_null_timestamps,
        test_fetch_rates_graphql_returns_none_on_empty_response,
        test_fetch_rates_graphql_returns_none_on_graphql_failure,
        test_fetch_rates_graphql_window_derived_from_forecast_hours,
        test_fetch_rates_graphql_window_default_forecast_hours,
        test_fetch_rates_rest_404_falls_back_to_graphql_for_import,
        test_fetch_rates_rest_404_no_fallback_without_import_mpan,
        test_fetch_rates_rest_404_falls_back_to_graphql_for_export,
        test_fetch_rates_export_404_uses_export_account_for_split_accounts,
        test_fetch_rates_rest_410_falls_back_to_graphql_for_import,
        test_find_active_tariff_uses_direction_for_seg_export_code,
        test_find_active_tariff_direction_overrides_export_substring,
        test_find_active_tariff_falls_back_to_substring_without_direction,
        test_build_rest_auth_uses_basic_auth_for_api_key,
        test_build_rest_auth_uses_jwt_header_for_oauth,
        test_fetch_rates_404_retries_authenticated_and_succeeds,
        test_fetch_rates_404_authenticated_retry_still_404_falls_back_to_graphql,
        test_fetch_rates_404_graphql_fallback_empty_counts_one_failure,
        test_connection_nodes_extracts_edges,
        test_fetch_rates_graphql_parses_connection_edges,
        test_fetch_rates_graphql_paginates_via_cursor,
        test_fetch_standing_charges_graphql_parses_connection_edges,
        test_fetch_rates_transient_error_does_not_fall_back_to_graphql,
        test_fetch_standing_charges_rest_404_falls_back_to_graphql,
        test_fetch_standing_charges_rest_404_no_fallback_without_import_mpan,
        test_fetch_standing_charges_rest_410_falls_back_to_graphql,
        test_fetch_standing_charges_graphql_returns_value,
        test_fetch_standing_charges_graphql_returns_none_on_empty,
        test_fetch_standing_charges_graphql_returns_none_on_graphql_failure,
        test_fetch_standing_charges_transient_error_does_not_fall_back_to_graphql,
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
