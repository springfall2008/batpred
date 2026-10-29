# src/batpred/apps/predbat/tests/test_kraken.py
import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_mock_base():
    base = MagicMock()
    base.log = MagicMock()
    base.prefix = "predbat"
    base.local_tz = "Europe/London"
    base.args = {"user_id": "test-instance-123"}
    # A real clock, as update_time() sets before any component runs - the component's midnight_utc
    # is derived from it, and the published rate window is measured from that midnight.
    base.now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Unconfigured settings fall back to their defaults, as the real get_arg() does
    base.get_arg = MagicMock(side_effect=lambda arg, default=None, **kwargs: default)
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
    # Not on Flextras by default, so run() tests never reach edfenergy.com; Flextras tests
    # delete this to get the real method back.
    api.async_fetch_flextras_hours = AsyncMock(return_value=None)
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
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == urlsplit("https://api.edfgb-kraken.energy").netloc
    assert parts.path == "/v1/products/VAR-22-11-01/electricity-tariffs/E-1R-VAR-22-11-01-J/standard-unit-rates/"
    params = parse_qs(parts.query)
    assert params == {"period_from": [(api.midnight_utc - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")], "period_to": [(api.midnight_utc + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")], "page_size": ["1500"]}


def test_build_rates_url_eon():
    api = make_kraken_api(provider="eon")
    url = api.build_rates_url("PROD-01", "E-1R-PROD-01-A")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == urlsplit("https://api.eonnext-kraken.energy").netloc
    assert parts.path == "/v1/products/PROD-01/electricity-tariffs/E-1R-PROD-01-A/standard-unit-rates/"
    assert parse_qs(parts.query)["page_size"] == ["1500"]


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


def test_fetch_rates_keeps_direct_debit_rows_only():
    """GH#5502: REST unit rates carry overlapping DIRECT_DEBIT / NON_DIRECT_DEBIT rows; keep Direct Debit.

    Downstream minute_data() merges overlapping rows last-write-wins, so leaving both in makes the
    rate depend on response order. Here NON_DIRECT_DEBIT comes last and would win.
    """
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-EDF_STANDARD_VARIABLE-J", "product_code": "EDF_STANDARD_VARIABLE"}
    rows = [
        {"value_inc_vat": 26.879, "valid_from": "2026-10-01T00:00:00Z", "valid_to": None, "payment_method": "DIRECT_DEBIT"},
        {"value_inc_vat": 28.2, "valid_from": "2026-10-01T00:00:00Z", "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
    ]
    api._fetch_rates_rest = AsyncMock(return_value=(rows, None))

    result = asyncio.run(api.async_fetch_rates())

    assert [rate["payment_method"] for rate in result] == ["DIRECT_DEBIT"]
    assert result[0]["value_inc_vat"] == 26.879


def test_fetch_rates_single_payment_method_unchanged():
    """REST unit rates offering only one payment method (or none) are all kept."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    rows = [
        {"value_inc_vat": 28.2, "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-02T00:00:00Z", "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 27.0, "valid_from": "2026-10-02T00:00:00Z", "valid_to": "2026-10-03T00:00:00Z", "payment_method": None},
    ]
    api._fetch_rates_rest = AsyncMock(return_value=(rows, None))

    result = asyncio.run(api.async_fetch_rates())

    assert [rate["value_inc_vat"] for rate in result] == [28.2, 27.0]


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


def _fetch_standing_charge_with_rows(rows):
    """Run async_fetch_standing_charges() against a REST response carrying the given rows."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-EDF_STANDARD_VARIABLE-J", "product_code": "EDF_STANDARD_VARIABLE"}

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=_mk_get_ctx(200, {"results": rows}))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        return asyncio.run(api.async_fetch_standing_charges())


def test_standing_charge_prefers_direct_debit_when_listed_second():
    """GH#5502: EDF lists the NON_DIRECT_DEBIT row first; the Direct Debit charge must still be chosen."""
    rows = [
        {"value_inc_vat": 61.186, "valid_from": "2026-10-01T00:00:00Z", "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 52.317, "valid_from": "2026-10-01T00:00:00Z", "valid_to": None, "payment_method": "DIRECT_DEBIT"},
        {"value_inc_vat": 60.0, "valid_from": "2026-07-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 51.0, "valid_from": "2026-07-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "payment_method": "DIRECT_DEBIT"},
    ]
    assert _fetch_standing_charge_with_rows(rows) == 52.317 / 100.0


def test_standing_charge_single_payment_method_unchanged():
    """A response offering only NON_DIRECT_DEBIT rows is used as-is rather than discarded."""
    rows = [
        {"value_inc_vat": 61.186, "valid_from": "2026-10-01T00:00:00Z", "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
    ]
    assert _fetch_standing_charge_with_rows(rows) == 61.186 / 100.0


def test_standing_charge_null_payment_method_unchanged():
    """Rows with a null payment_method keep the existing first-row behaviour."""
    rows = [
        {"value_inc_vat": 48.0, "valid_from": "2026-10-01T00:00:00Z", "valid_to": None, "payment_method": None},
        {"value_inc_vat": 47.0, "valid_from": "2026-07-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z", "payment_method": None},
    ]
    assert _fetch_standing_charge_with_rows(rows) == 48.0 / 100.0


def _days_from_now(days):
    """ISO timestamp the given number of days from the real clock (negative for the past)."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _both_orders(rows):
    """The rows as listed and reversed: neither Kraken endpoint promises an order (GH#5573)."""
    return [rows, list(reversed(rows))]


def test_standing_charge_skips_row_that_has_not_started():
    """GH#5573: a price change published ahead of time must not be used before it starts."""
    rows = [
        {"value_inc_vat": 55.0, "valid_from": _days_from_now(10), "valid_to": None, "payment_method": "DIRECT_DEBIT"},
        {"value_inc_vat": 52.0, "valid_from": _days_from_now(-30), "valid_to": _days_from_now(10), "payment_method": "DIRECT_DEBIT"},
        {"value_inc_vat": 50.0, "valid_from": _days_from_now(-90), "valid_to": _days_from_now(-30), "payment_method": "DIRECT_DEBIT"},
    ]
    for ordered in _both_orders(rows):
        assert _fetch_standing_charge_with_rows(ordered) == 52.0 / 100.0


def test_standing_charge_skips_row_that_has_ended():
    """GH#5573: a row whose valid_to has passed is history, even when it is listed first."""
    rows = [
        {"value_inc_vat": 50.0, "valid_from": _days_from_now(-90), "valid_to": _days_from_now(-30), "payment_method": None},
        {"value_inc_vat": 52.0, "valid_from": _days_from_now(-30), "valid_to": None, "payment_method": None},
    ]
    for ordered in _both_orders(rows):
        assert _fetch_standing_charge_with_rows(ordered) == 52.0 / 100.0


def test_standing_charge_latest_start_wins_when_rows_overlap():
    """When more than one row covers now the most recent valid_from wins, and an open start is the oldest."""
    rows = [
        {"value_inc_vat": 40.0, "valid_from": None, "valid_to": None},
        {"value_inc_vat": 48.0, "valid_from": _days_from_now(-60), "valid_to": None},
        {"value_inc_vat": 52.0, "valid_from": _days_from_now(-5), "valid_to": _days_from_now(5)},
    ]
    for ordered in _both_orders(rows):
        assert _fetch_standing_charge_with_rows(ordered) == 52.0 / 100.0


def test_standing_charge_keeps_direct_debit_row_among_rows_covering_now():
    """Choosing the row in force still leaves the GH#5502 Direct Debit preference in charge of a tie."""
    rows = [
        {"value_inc_vat": 61.0, "valid_from": _days_from_now(-30), "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 52.0, "valid_from": _days_from_now(-30), "valid_to": None, "payment_method": "DIRECT_DEBIT"},
        {"value_inc_vat": 65.0, "valid_from": _days_from_now(10), "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
        {"value_inc_vat": 56.0, "valid_from": _days_from_now(10), "valid_to": None, "payment_method": "DIRECT_DEBIT"},
    ]
    for ordered in _both_orders(rows):
        assert _fetch_standing_charge_with_rows(ordered) == 52.0 / 100.0


def test_standing_charge_none_when_no_row_covers_now():
    """With nothing in force the charge is None, so run() keeps the value it already had."""
    only_ended = [{"value_inc_vat": 50.0, "valid_from": _days_from_now(-90), "valid_to": _days_from_now(-1)}]
    only_future = [{"value_inc_vat": 55.0, "valid_from": _days_from_now(1), "valid_to": None}]
    assert _fetch_standing_charge_with_rows(only_ended) is None
    assert _fetch_standing_charge_with_rows(only_future) is None


def test_standing_charge_ignores_rows_it_cannot_read():
    """A row that is not an object is skipped, and an unparseable bound counts as open."""
    rows = [
        None,
        "not a row",
        {"value_inc_vat": 50.0, "valid_from": "not a timestamp", "valid_to": None},
    ]
    assert _fetch_standing_charge_with_rows(rows) == 50.0 / 100.0


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
        assert api.failures_total == 1, f"HTTP {status_code} must count one failure, got {api.failures_total}"


def _fetch_standing_charges_404_with_fallback(fallback):
    """Run async_fetch_standing_charges() against a REST 404 with the given GraphQL fallback side effect."""
    api = make_kraken_api(provider="eon", account_id="A-AA8A473C")
    api.current_tariff = {"tariff_code": "E-TOU-NEXT_SMART_SAVER_FIXED_12M_V8-M", "product_code": "NEXT_SMART_SAVER_FIXED_12M_V8"}
    api.import_mpan = "1900000000456"
    api.failures_total = 0
    api.async_fetch_standing_charges_graphql = AsyncMock(side_effect=lambda mpan: fallback(api))

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(404))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_standing_charges())
    return api, result


def test_fetch_standing_charges_404_recovered_by_graphql_is_not_a_failure():
    """GH#5584: the expected E.ON Next TOU 404 must not count a failure when the GraphQL fallback succeeds."""
    api, result = _fetch_standing_charges_404_with_fallback(lambda api: 0.6195)
    assert result == 0.6195
    assert api.failures_total == 0, f"a recovered 404 must not count as a failure, got {api.failures_total}"


def test_fetch_standing_charges_404_graphql_empty_counts_one_failure():
    """A 404 whose GraphQL fallback returns nothing (without counting) is one genuine failure."""
    api, result = _fetch_standing_charges_404_with_fallback(lambda api: None)
    assert result is None
    assert api.failures_total == 1


def test_fetch_standing_charges_404_graphql_failure_counts_once_not_twice():
    """A fallback that already counted its own failure inside async_graphql_query is not counted again."""

    def graphql_failure(api):
        """Stand in for a fallback that failed inside async_graphql_query, which counts its own."""
        api.failures_total += 1
        return None

    api, result = _fetch_standing_charges_404_with_fallback(graphql_failure)
    assert result is None
    assert api.failures_total == 1, f"one failed cycle must count once, got {api.failures_total}"


def _mk_undecodable_get_ctx():
    """Build a mock GET context yielding a 200 whose body claims JSON but does not parse."""
    ctx = _mk_get_ctx(200)
    resp = ctx.__aenter__.return_value
    resp.json = AsyncMock(side_effect=json.JSONDecodeError("Expecting value", "<html>Bad gateway</html>", 0))
    return ctx


def test_fetch_standing_charges_undecodable_200_is_counted_not_raised():
    """GH#5584: a 200 with a malformed JSON body is logged and counted rather than escaping the fetch."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
    api.failures_total = 0

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_undecodable_get_ctx())
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_standing_charges())

    assert result is None
    assert api.failures_total == 1


def test_fetch_rates_undecodable_200_is_counted_not_raised():
    """GH#5584: a 200 with a malformed JSON body on the REST rates endpoint is a counted failure, not an exception."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
    api.failures_total = 0
    api.async_fetch_rates_graphql = AsyncMock()

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_undecodable_get_ctx())
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates())

    assert result is None
    assert api.failures_total == 1
    api.async_fetch_rates_graphql.assert_not_called()


# JSON bodies that parse but are not an object, so data.get() on them would raise AttributeError.
NON_OBJECT_JSON_BODIES = [[], None, "Bad gateway", 42]


def _mk_non_object_get_ctx(body):
    """Build a mock GET context yielding a 200 whose body parses as JSON but is not an object."""
    ctx = _mk_get_ctx(200)
    resp = ctx.__aenter__.return_value
    resp.json = AsyncMock(return_value=body)
    return ctx


def test_fetch_standing_charges_non_object_200_is_counted_not_raised():
    """A 200 whose JSON is not an object is logged and counted rather than escaping as AttributeError."""
    for body in NON_OBJECT_JSON_BODIES:
        api = make_kraken_api()
        api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
        api.failures_total = 0

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_non_object_get_ctx(body))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(api.async_fetch_standing_charges())

        assert result is None, f"body {body!r}: expected None, got {result!r}"
        assert api.failures_total == 1, f"body {body!r}: expected one failure, got {api.failures_total}"


def test_fetch_rates_non_object_200_is_counted_not_raised():
    """A 200 whose JSON is not an object on the REST rates endpoint is a counted failure, not an exception."""
    for body in NON_OBJECT_JSON_BODIES:
        api = make_kraken_api()
        api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
        api.failures_total = 0
        api.async_fetch_rates_graphql = AsyncMock()

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_non_object_get_ctx(body))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = asyncio.run(api.async_fetch_rates())

        assert result is None, f"body {body!r}: expected None, got {result!r}"
        assert api.failures_total == 1, f"body {body!r}: expected one failure, got {api.failures_total}"
        api.async_fetch_rates_graphql.assert_not_called()


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
    # applicableRates publishes exc-VAT prices, so the raw value is value_exc_vat; with no
    # VAT multiplier supplied (the default, and what export uses) inc-VAT is the same figure.
    assert result[0]["value_exc_vat"] == 24.57
    assert result[0]["value_inc_vat"] == 24.57
    # Timestamps must be passed through unchanged
    assert result[0]["valid_from"] == "2026-04-10T00:00:00Z"
    assert result[0]["valid_to"] == "2026-04-11T00:00:00Z"


def test_fetch_rates_graphql_scales_exc_vat_to_inc_vat():
    """applicableRates values are exc-VAT; the supplied multiplier converts them to inc-VAT."""
    api = make_kraken_api()
    api.async_graphql_query = AsyncMock(return_value={"applicableRates": [{"value": 20.0, "validFrom": "2026-04-10T00:00:00Z", "validTo": "2026-04-11T00:00:00Z"}]})

    result = asyncio.run(api.async_fetch_rates_graphql("1900000000456", vat_multiplier=1.05))

    assert result[0]["value_exc_vat"] == 20.0
    assert result[0]["value_inc_vat"] == 21.0


def test_vat_multiplier_from_agreement_standing_charge():
    """The VAT multiplier is the tariff's standingCharge / preVatStandingCharge, 1.0 when unusable."""
    api = make_kraken_api()
    # 5% VAT
    assert round(api._vat_multiplier({"standingCharge": 63.0, "preVatStandingCharge": 60.0}), 4) == 1.05
    # Zero-rated (GB domestic electricity, Oct 2026 - Mar 2027): both values equal
    assert api._vat_multiplier({"standingCharge": 60.0, "preVatStandingCharge": 60.0}) == 1.0
    # Missing / null / zero / non-numeric values leave prices unscaled
    assert api._vat_multiplier({}) == 1.0
    assert api._vat_multiplier(None) == 1.0
    assert api._vat_multiplier({"standingCharge": 63.0, "preVatStandingCharge": None}) == 1.0
    assert api._vat_multiplier({"standingCharge": 63.0, "preVatStandingCharge": 0}) == 1.0
    assert api._vat_multiplier({"standingCharge": "bad", "preVatStandingCharge": 60.0}) == 1.0
    # Nonsense ratios are refused: VAT never subtracts and never adds more than a quarter
    assert api._vat_multiplier({"standingCharge": 50.0, "preVatStandingCharge": 60.0}) == 1.0
    assert api._vat_multiplier({"standingCharge": 120.0, "preVatStandingCharge": 60.0}) == 1.0


def _vat_account_data(standing_charge, pre_vat_standing_charge):
    """Account query response with one import meter point carrying the given standing charges."""
    tariff = {"productCode": "SMART-V16", "tariffCode": "E-1R-SMART-V16-J", "displayName": "Smart Saver", "standingCharge": standing_charge, "preVatStandingCharge": pre_vat_standing_charge}
    meter_point = {"mpan": "1900000000456", "direction": "IMPORT", "agreements": [{"validFrom": "2026-01-01T00:00:00+00:00", "validTo": None, "tariff": tariff}]}
    return {"account": {"number": "A-TEST123", "properties": [{"address": "1 Test Street", "electricityMeterPoints": [meter_point]}]}}


def test_find_tariffs_stores_import_vat_multiplier():
    """async_find_tariffs() records the import VAT multiplier, and refreshes it on an unchanged tariff."""
    api = make_kraken_api()
    assert api.import_vat_multiplier == 1.0
    api._discover_export_tariff = AsyncMock()

    api.async_graphql_query = AsyncMock(return_value=_vat_account_data(63.0, 60.0))
    assert asyncio.run(api.async_find_tariffs()) is not None
    assert round(api.import_vat_multiplier, 4) == 1.05
    # The multiplier must not leak into the tariff identity used for change detection
    assert api.current_tariff == {"tariff_code": "E-1R-SMART-V16-J", "product_code": "SMART-V16"}

    # Same tariff, VAT rate changed (e.g. the zero rate starting or ending): still picked up
    api.async_graphql_query = AsyncMock(return_value=_vat_account_data(60.0, 60.0))
    assert asyncio.run(api.async_find_tariffs()) is None
    assert api.import_vat_multiplier == 1.0


def test_fetch_rates_graphql_fallback_applies_vat_to_import_only():
    """The applicableRates fallback scales import by the agreement's VAT multiplier but never export."""
    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01-J", "product_code": "VAR-01"}
    api.export_tariff = {"tariff_code": "E-1R-EDF_EXPORT_SEG_12M_HH-B", "product_code": "EDF_EXPORT_SEG_12M"}
    api.import_mpan = "1900000000456"
    api.export_mpan = "2000000000789"
    api.import_vat_multiplier = 1.05
    api._fetch_rates_rest = AsyncMock(return_value=([], 400))
    api.async_fetch_rates_graphql = AsyncMock(return_value=[{"value_inc_vat": 21.0}])

    asyncio.run(api.async_fetch_rates())
    api.async_fetch_rates_graphql.assert_called_once_with("1900000000456", account_id="A-TEST123", vat_multiplier=1.05)

    api.async_fetch_rates_graphql.reset_mock()
    asyncio.run(api.async_fetch_rates(api.export_tariff))
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-TEST123", vat_multiplier=1.0)


def test_kraken_cache_round_trips_import_vat_multiplier():
    """The VAT multiplier survives a restart, and a cache written before it existed defaults to 1.0."""
    api = make_kraken_api()
    storage = attach_fake_storage(api)
    api.current_tariff = {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"}
    api.import_vat_multiplier = 1.05
    asyncio.run(api.save_kraken_cache())

    api2 = make_kraken_api(account_id="A-TEST123")
    api2.base.components = api.base.components
    api2.update_success_timestamp = MagicMock()
    asyncio.run(api2.load_kraken_cache())
    assert api2.import_vat_multiplier == 1.05

    del storage.data[("kraken", "account_A-TEST123")]["import_vat_multiplier"]
    api3 = make_kraken_api(account_id="A-TEST123")
    api3.base.components = api.base.components
    api3.update_success_timestamp = MagicMock()
    asyncio.run(api3.load_kraken_cache())
    assert api3.import_vat_multiplier == 1.0


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
    api.async_fetch_rates_graphql.assert_called_once_with("1900000000456", account_id="A-AA8A473C", vat_multiplier=1.0)


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
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-TEST123", vat_multiplier=1.0)


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

    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-EXPORT456", vat_multiplier=1.0)


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
    api.async_fetch_rates_graphql.assert_called_once_with("1900000000456", account_id="A-AA8A473C", vat_multiplier=1.0)


def test_fetch_rates_rest_400_day_night_tariff_falls_back_to_graphql():
    """async_fetch_rates() falls back to GraphQL when REST returns 400 on a day/night tariff.

    GH#5166: EDF day/night-structured tariffs (e.g. E-FLAT2R-EDF_STANDARD_VARIABLE-D) have no
    /standard-unit-rates/ resource — the API answers HTTP 400 "This tariff has day and night
    rates, not standard." That is as permanent as a 404, so it must reach the GraphQL fallback
    instead of being logged as a failure on every cycle. Auth cannot change a 400, so unlike the
    404 path there must be no authenticated retry of the same URL.
    """
    api = make_kraken_api(provider="edf", account_id="A-9C006563")
    api.current_tariff = {"tariff_code": "E-FLAT2R-EDF_STANDARD_VARIABLE-D", "product_code": "EDF_STANDARD_VARIABLE"}
    api.import_mpan = "1900000000456"
    api.failures_total = 0

    graphql_rates = [{"value_inc_vat": 27.5, "value_exc_vat": round(27.5 / 1.05, 4), "valid_from": "2026-09-19T00:00:00Z", "valid_to": "2026-09-21T00:00:00Z"}]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(400))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates())

    assert result is graphql_rates
    api.async_fetch_rates_graphql.assert_called_once_with("1900000000456", account_id="A-9C006563", vat_multiplier=1.0)
    assert mock_session.get.call_count == 1, f"400 must not trigger an authenticated retry, got {mock_session.get.call_count} REST requests"
    assert api.failures_total == 0, "A recovered 400 must not count as a failure"


def test_fetch_rates_rest_400_falls_back_to_graphql_for_export():
    """A day/night-structured EXPORT tariff's 400 falls back on the export MPAN and account.

    The 400 gate shares the fallback wiring with the 404 path, which selects export_mpan and
    export_account_id for an export tariff — but only the 404 side of that wiring was covered,
    so a 400 mis-routed to the import MPAN/account would have passed the suite.
    """
    api = make_kraken_api(provider="edf", account_id="A-9C006563", export_account_id="A-EXPORT456")
    api.current_tariff = {"tariff_code": "E-1R-IMP-01-J", "product_code": "IMP-01"}
    api.import_mpan = "1900000000456"
    export_tariff = {"tariff_code": "E-FLAT2R-EDF_EXPORT_VARIABLE-D", "product_code": "EDF_EXPORT_VARIABLE"}
    api.export_tariff = export_tariff
    api.export_mpan = "2000000000789"
    api.failures_total = 0

    graphql_rates = [{"value_inc_vat": 15.0, "value_exc_vat": round(15.0 / 1.05, 4), "valid_from": "2026-09-19T00:00:00Z", "valid_to": "2026-09-21T00:00:00Z"}]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(400))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    assert result is graphql_rates
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-EXPORT456", vat_multiplier=1.0)
    assert mock_session.get.call_count == 1, f"400 must not trigger an authenticated retry, got {mock_session.get.call_count} REST requests"
    assert api.failures_total == 0, "A recovered 400 must not count as a failure"


def test_fetch_rates_rest_400_no_fallback_without_import_mpan():
    """async_fetch_rates() returns None and counts a failure when REST 400s with no MPAN to fall back on."""
    api = make_kraken_api(provider="edf")
    api.current_tariff = {"tariff_code": "E-FLAT2R-EDF_STANDARD_VARIABLE-D", "product_code": "EDF_STANDARD_VARIABLE"}
    api.import_mpan = None  # Not yet discovered
    api.failures_total = 0
    api.async_fetch_rates_graphql = AsyncMock()

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(400))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates())

    assert result is None
    api.async_fetch_rates_graphql.assert_not_called()
    assert api.failures_total == 1


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
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-TEST123", vat_multiplier=1.0)
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


def test_fetch_rates_404_then_network_error_on_retry_still_falls_back_to_graphql():
    """A network error during the authenticated retry must not lose the GraphQL fallback.

    _fetch_rates_rest returns (None, None) on a network error, which would otherwise erase the
    404 the public attempt just proved and drop the cycle into the generic-failure path — losing
    a private product's rates to a transient blip even though GraphQL would have served them.
    """
    import aiohttp

    api = make_kraken_api()
    export_tariff = {"tariff_code": "E-1R-EDF_EXPORT_SEG_12M_HH-B", "product_code": "EDF_EXPORT_SEG_12M"}
    api.export_tariff = export_tariff
    api.export_mpan = "2000000000789"
    api.failures_total = 0

    graphql_rates = [{"value_inc_vat": 15.0, "value_exc_vat": round(15.0 / 1.05, 4), "valid_from": "2026-09-19T00:00:00Z", "valid_to": "2026-09-21T00:00:00Z"}]
    api.async_fetch_rates_graphql = AsyncMock(return_value=graphql_rates)

    call_count = [0]

    def get_side_effect(url, **kwargs):
        """Public attempt 404s; the authenticated retry fails with a connection error."""
        call_count[0] += 1
        if call_count[0] == 1:
            return _mk_get_ctx(404)
        raise aiohttp.ClientConnectionError("connection reset")

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=get_side_effect)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates(tariff=export_tariff))

    assert result is graphql_rates
    assert call_count[0] == 2, "expected an unauthenticated attempt then an authenticated retry"
    api.async_fetch_rates_graphql.assert_called_once_with("2000000000789", account_id="A-TEST123", vat_multiplier=1.0)
    assert api.failures_total == 0, "rates recovered via GraphQL must not count as a failure"


def test_fetch_rates_graphql_fallback_failure_counts_one_failure_not_two():
    """A GraphQL fallback that fails internally is counted once, not once per counting layer.

    async_graphql_query increments failures_total on every one of its own failure paths (auth
    unavailable, no token, non-200, GraphQL errors, network), so async_fetch_rates must not add
    a second count for the same cycle: for a day/night tariff whose GraphQL is down that would
    score 2 every 5 minutes and mislead anything watching the metric for component health.
    """
    api = make_kraken_api(provider="edf", account_id="A-9C006563")
    api.current_tariff = {"tariff_code": "E-FLAT2R-EDF_STANDARD_VARIABLE-D", "product_code": "EDF_STANDARD_VARIABLE"}
    api.import_mpan = "1900000000456"
    api.failures_total = 0

    def graphql_failure(mpan, account_id=None, vat_multiplier=1.0):
        """Stand in for a fallback that failed inside async_graphql_query, which counts its own."""
        api.failures_total += 1
        return None

    api.async_fetch_rates_graphql = AsyncMock(side_effect=graphql_failure)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=lambda url, **kwargs: _mk_get_ctx(400))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_fetch_rates())

    assert result is None
    assert api.failures_total == 1, f"one failed cycle must count once, got {api.failures_total}"


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


def _fetch_standing_charge_graphql_with_nodes(nodes):
    """Run async_fetch_standing_charges_graphql() against an applicableStandingCharges window holding the given nodes."""
    api = make_kraken_api()
    api.account_id = "A-AA8A473C"
    api.async_graphql_query = AsyncMock(return_value={"applicableStandingCharges": {"edges": [{"node": node} for node in nodes]}})
    return asyncio.run(api.async_fetch_standing_charges_graphql("1170001829927"))


def test_fetch_standing_charges_graphql_picks_row_covering_now():
    """GH#5573: the 3-day window can span a price change, so the charge in force now is used, not the first listed."""
    nodes = [
        {"value": 63.0, "validFrom": _days_from_now(1), "validTo": None},
        {"value": 61.0, "validFrom": _days_from_now(-30), "validTo": _days_from_now(1)},
    ]
    for ordered in _both_orders(nodes):
        result = _fetch_standing_charge_graphql_with_nodes(ordered)
        assert result is not None
        assert abs(result - 0.61) < 1e-6


def test_fetch_standing_charges_graphql_latest_start_wins_when_rows_overlap():
    """GH#5573: of two rows covering now the one that started most recently is the current charge."""
    nodes = [
        {"value": 60.0, "validFrom": _days_from_now(-60), "validTo": None},
        {"value": 62.0, "validFrom": _days_from_now(-2), "validTo": None},
    ]
    for ordered in _both_orders(nodes):
        result = _fetch_standing_charge_graphql_with_nodes(ordered)
        assert result is not None
        assert abs(result - 0.62) < 1e-6


def test_fetch_standing_charges_graphql_none_when_no_row_covers_now():
    """GH#5573: a window holding only a row that has not started (or has ended) gives no standing charge."""
    assert _fetch_standing_charge_graphql_with_nodes([{"value": 63.0, "validFrom": _days_from_now(1), "validTo": None}]) is None
    assert _fetch_standing_charge_graphql_with_nodes([{"value": 61.0, "validFrom": _days_from_now(-30), "validTo": _days_from_now(-1)}]) is None


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
                {"id": "dev-ev-1", "deviceType": "ELECTRIC_VEHICLES", "status": {"current": "LIVE"}, "__typename": "SmartFlexVehicle", "make": "Tesla", "model": "M3"},
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
    # provider is filled from make, as the Octopus component does (#5754)
    assert dev["provider"] == "Tesla"


def test_devices_query_does_not_request_deprecated_provider():
    """Kraken deprecated the SmartFlex device provider field (removal on or after 2027-04-15); asking for it would fail every devices query (#5754)."""
    from kraken import KRAKEN_DEVICES_QUERY

    fields = KRAKEN_DEVICES_QUERY.format(account_number="A-1234ABCD").split()
    assert "provider" not in fields, KRAKEN_DEVICES_QUERY
    assert "make" in fields and "deviceType" in fields


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
    from datetime import datetime, timezone, timedelta

    api = make_kraken_api()
    api.intelligent_devices = {"dev-1": {"device_id": "dev-1", "planned_dispatches": [], "completed_dispatches": []}}
    # Use dates relative to now so the completed dispatch stays inside the history prune window
    planned_start = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    planned_end = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed_start = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed_end = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    api.async_graphql_query = AsyncMock(
        return_value={
            "flexPlannedDispatches": [{"start": planned_start, "end": planned_end, "type": "SMART", "energyAddedKwh": 10.0}],
            "completedDispatches": [{"start": completed_start, "end": completed_end, "delta": 3.0, "meta": {"source": "SMART", "location": "AT_HOME"}}],
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


def _fixed_contract_rates(first_day, last_day):
    """Rates shaped like the fixed EDF day/night contract in #5534: three periods a day, newest first.

    Each day D has 05:00-22:00Z day, 22:00-23:00Z cheap and 23:00Z-05:00Z (D+1) night periods.
    """
    rates = []
    day = first_day
    while day <= last_day:
        d = day.strftime("%Y-%m-%d")
        n = (day + timedelta(days=1)).strftime("%Y-%m-%d")
        rates.append({"value_exc_vat": 29.634, "value_inc_vat": 29.634, "valid_from": f"{d}T05:00:00Z", "valid_to": f"{d}T22:00:00Z", "payment_method": None})
        rates.append({"value_exc_vat": 6.66, "value_inc_vat": 6.66, "valid_from": f"{d}T22:00:00Z", "valid_to": f"{d}T23:00:00Z", "payment_method": None})
        rates.append({"value_exc_vat": 6.66, "value_inc_vat": 6.66, "valid_from": f"{d}T23:00:00Z", "valid_to": f"{n}T05:00:00Z", "payment_method": None})
        day += timedelta(days=1)
    rates.reverse()
    return rates


def _make_publishing_api(now_utc, forecast_hours=48):
    """A KrakenAPI with a known tariff, clock and forecast_hours, recording dashboard_item calls."""
    api = make_kraken_api()
    api.base.now_utc = now_utc
    api.get_arg = MagicMock(side_effect=lambda arg, default=None, **kwargs: forecast_hours if arg == "forecast_hours" else default)
    api.current_tariff = {"tariff_code": "E-1R-EDF_EV_FIX_GOELEC_12M_V2_HH-A", "product_code": "EDF_EV_FIX_GOELEC_12M_V2_HH"}
    api.import_standing_charge = None
    api.dashboard_item = MagicMock()
    return api


def _published(api, suffix):
    """Return (state, attributes) of the last dashboard_item call for the given sensor suffix."""
    entity = api.get_entity_name("sensor", suffix)
    calls = [c for c in api.dashboard_item.call_args_list if c.args[0] == entity]
    assert calls, f"{entity} was not published"
    return calls[-1].kwargs["state"], calls[-1].kwargs["attributes"]


def _utc(text):
    """Parse a Kraken ...Z timestamp."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_publish_rate_sensors_bounds_long_contract_to_window():
    """#5534: a months-long fixed contract publishes only the (forecast_days + 1) days either side of midnight that Predbat reads, plus a spare day.

    The issue's sensor carried 255 rows (~38 KB), past Home Assistant's 16,384-byte attribute limit.
    """
    import json
    import pytz

    now = pytz.timezone("Europe/London").localize(datetime(2026, 10, 13, 10, 15))
    api = _make_publishing_api(now)
    # Contract started 20 July; rates known up to the end of tomorrow (night period runs into 15 Oct)
    api.import_rates = _fixed_contract_rates(datetime(2026, 7, 20), datetime(2026, 10, 14))
    full = list(api.import_rates)
    assert len(json.dumps(full)) > 16384, "fixture must reproduce the oversized payload"

    api._publish_rate_sensors()
    state, attributes = _published(api, "import_rates")
    rates = attributes["rates"]

    # Local midnight 13 Oct BST is 12 Oct 23:00Z; 48h is forecast_days=2, so the window is 9 Oct 23:00Z (- 3 days) to 16 Oct 23:00Z (+ 3 days + 1 spare)
    window_start = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 10, 16, 23, 0, tzinfo=timezone.utc)
    expected = [r for r in full if _utc(r["valid_to"]) > window_start and _utc(r["valid_from"]) < window_end]
    assert rates == expected, f"published {[(r['valid_from'], r['valid_to']) for r in rates]}"
    # 9 Oct 23:00Z night, then three periods a day for 10-14 Oct, newest first as fetched
    assert len(rates) == 1 + 5 * 3
    assert rates[0]["valid_from"] == "2026-10-14T23:00:00Z"
    assert rates[-1]["valid_from"] == "2026-10-09T23:00:00Z"
    assert state == len(rates)
    assert len(json.dumps(attributes)) < 16384
    # The in-memory list (and so the cache) keeps the full contract
    assert api.import_rates == full


def test_publish_rate_sensors_keeps_periods_straddling_window_edges():
    """Periods overlapping either edge are kept whole. One ending exactly at the start is dropped, but one
    starting exactly at the end is kept, because minute_data() keeps its last minute inclusively."""
    import pytz

    now = pytz.timezone("Europe/London").localize(datetime(2026, 1, 13, 10, 15))
    api = _make_publishing_api(now)
    # GMT: midnight is 13 Jan 00:00Z, so the window is 10 Jan 00:00Z to 17 Jan 00:00Z
    straddle_start = {"value_inc_vat": 1.0, "valid_from": "2026-01-09T23:00:00Z", "valid_to": "2026-01-10T01:00:00Z"}
    ends_at_start = {"value_inc_vat": 2.0, "valid_from": "2026-01-09T22:00:00Z", "valid_to": "2026-01-10T00:00:00Z"}
    inside = {"value_inc_vat": 3.0, "valid_from": "2026-01-13T05:00:00Z", "valid_to": "2026-01-13T22:00:00Z"}
    straddle_end = {"value_inc_vat": 4.0, "valid_from": "2026-01-16T23:00:00Z", "valid_to": "2026-01-17T05:00:00Z"}
    starts_at_end = {"value_inc_vat": 5.0, "valid_from": "2026-01-17T00:00:00Z", "valid_to": "2026-01-17T05:00:00Z"}
    starts_after_end = {"value_inc_vat": 9.0, "valid_from": "2026-01-17T00:30:00Z", "valid_to": "2026-01-17T05:00:00Z"}
    open_ended = {"value_inc_vat": 6.0, "valid_from": "2025-06-01T00:00:00Z", "valid_to": None}
    ancient = {"value_inc_vat": 7.0, "valid_from": "2025-06-01T00:00:00Z", "valid_to": "2025-06-02T00:00:00Z"}
    unparseable = {"value_inc_vat": 8.0, "valid_from": "not-a-time", "valid_to": "2025-06-02T00:00:00Z"}
    api.import_rates = [straddle_start, ends_at_start, inside, straddle_end, starts_at_end, starts_after_end, open_ended, ancient, unparseable]

    api._publish_rate_sensors()
    state, attributes = _published(api, "import_rates")

    assert attributes["rates"] == [straddle_start, inside, straddle_end, starts_at_end, open_ended, unparseable]
    assert state == 6

    # minute_data() agrees at both edges. Read from 13 Jan midnight its first minute is the window start (10 Jan);
    # read from 14 Jan midnight, before a republish, its last minute is the window end (17 Jan).
    from utils import minute_data

    parseable = [r for r in api.import_rates if r is not unparseable]
    published = [r for r in attributes["rates"] if r is not unparseable]
    for read_midnight in (datetime(2026, 1, 13, tzinfo=timezone.utc), datetime(2026, 1, 14, tzinfo=timezone.utc)):
        loaded_full, _ = minute_data(parseable, 3, read_midnight, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        loaded_trimmed, _ = minute_data(published, 3, read_midnight, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        assert loaded_trimmed == loaded_full, f"loaded rates differ when read from midnight {read_midnight}"


def test_publish_rate_sensors_bounds_export_rates():
    """The export rates sensor gets the same window as import."""
    import pytz

    now = pytz.timezone("Europe/London").localize(datetime(2026, 10, 13, 10, 15))
    api = _make_publishing_api(now)
    api.import_rates = _fixed_contract_rates(datetime(2026, 10, 12), datetime(2026, 10, 14))
    api.export_tariff = {"tariff_code": "E-1R-EDF_SEG-A", "product_code": "EDF_SEG"}
    api.export_rates = _fixed_contract_rates(datetime(2026, 7, 20), datetime(2026, 10, 14))

    api._publish_rate_sensors()
    state, attributes = _published(api, "export_rates")

    assert len(attributes["rates"]) == 1 + 5 * 3
    assert state == 1 + 5 * 3
    assert len(api.export_rates) == 87 * 3


def test_publish_rate_sensors_window_follows_forecast_hours():
    """forecast_hours=72 is forecast_days=3: minute_data() keeps midnight + 4 days, published with a day to spare."""
    import pytz

    now = pytz.timezone("Europe/London").localize(datetime(2026, 1, 13, 10, 15))
    api = _make_publishing_api(now, forecast_hours=72)
    api.import_rates = _fixed_contract_rates(datetime(2026, 1, 1), datetime(2026, 1, 20))

    api._publish_rate_sensors()
    _, attributes = _published(api, "import_rates")
    rates = attributes["rates"]

    # GMT window 9 Jan 00:00Z (- 4 days) to 18 Jan 00:00Z (+ 4 days + 1 spare): from the 8 Jan night period to the 17 Jan night period
    assert rates[-1]["valid_from"] == "2026-01-08T23:00:00Z"
    assert rates[0]["valid_from"] == "2026-01-17T23:00:00Z"
    assert len(rates) == 1 + 9 * 3


def test_publish_rate_sensors_window_clamps_forecast_hours_like_fetch():
    """fetch.py raises forecast_hours to at least 24, so 0 still means forecast_days=1 here, not 0."""
    import pytz

    now = pytz.timezone("Europe/London").localize(datetime(2026, 1, 13, 10, 15))
    api = _make_publishing_api(now, forecast_hours=0)
    api.import_rates = _fixed_contract_rates(datetime(2026, 1, 1), datetime(2026, 1, 20))

    api._publish_rate_sensors()
    _, attributes = _published(api, "import_rates")

    # forecast_days=1: the window ends 16 Jan 00:00Z (+ 2 days + 1 spare), so up to the 15 Jan night period
    assert attributes["rates"][0]["valid_from"] == "2026-01-15T23:00:00Z"


def test_publish_rate_sensors_trimmed_rates_load_identically():
    """What fetch_octopus_rates() loads through minute_data() is the same from the trimmed list as from the full one.

    Checked as read at the publish time and just after the next midnight, before any republish.
    """
    import pytz
    from utils import minute_data

    london = pytz.timezone("Europe/London")
    publish_time = london.localize(datetime(2026, 10, 12, 23, 50))
    forecast_days = 2
    for full in (_fixed_contract_rates(datetime(2026, 7, 20), datetime(2026, 10, 13)), _fixed_contract_rates(datetime(2026, 7, 20), datetime(2026, 10, 8))):
        api = _make_publishing_api(publish_time)
        api.import_rates = full
        api._publish_rate_sensors()
        _, attributes = _published(api, "import_rates")
        assert len(attributes["rates"]) < len(full)
        for read_midnight in (london.localize(datetime(2026, 10, 12)), london.localize(datetime(2026, 10, 13))):
            loaded_full, _ = minute_data(full, forecast_days + 1, read_midnight, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
            loaded_trimmed, _ = minute_data(attributes["rates"], forecast_days + 1, read_midnight, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
            assert loaded_trimmed == loaded_full, f"loaded rates differ when read from midnight {read_midnight}"


def test_publish_rate_sensors_keeps_stale_rates_rate_replicate_can_use():
    """When fetches keep failing, rates from two days back are still published for rate_replicate() to copy forward."""
    import pytz

    now = pytz.timezone("Europe/London").localize(datetime(2026, 1, 13, 10, 15))
    api = _make_publishing_api(now)
    # Last successful fetch was days ago: nothing newer than the 10 Jan periods
    api.import_rates = _fixed_contract_rates(datetime(2025, 12, 1), datetime(2026, 1, 10))

    api._publish_rate_sensors()
    state, attributes = _published(api, "import_rates")

    # Window starts 10 Jan 00:00Z (- 3 days): the 9 Jan night period and all of 10 Jan survive
    assert [r["valid_from"] for r in attributes["rates"]] == ["2026-01-10T23:00:00Z", "2026-01-10T22:00:00Z", "2026-01-10T05:00:00Z", "2026-01-09T23:00:00Z"]
    assert state == 4


def test_publish_rate_sensors_before_midnight_still_covers_next_day():
    """A sensor published at 23:50 is still what Predbat reads at 00:05, so it must cover the window from the new midnight."""
    import pytz

    london = pytz.timezone("Europe/London")
    api = _make_publishing_api(london.localize(datetime(2026, 1, 12, 23, 50)))
    api.import_rates = _fixed_contract_rates(datetime(2026, 1, 1), datetime(2026, 1, 20))

    api._publish_rate_sensors()
    _, attributes = _published(api, "import_rates")

    # At 00:05 on 13 Jan minute_data() keeps up to 13 Jan + 3 days = 16 Jan 00:00Z; the 15 Jan night period runs into it
    assert max(_utc(r["valid_to"]) for r in attributes["rates"]) >= datetime(2026, 1, 16, 0, 0, tzinfo=timezone.utc)
    # and from 00:05 its - 3 days start is 10 Jan 00:00Z, which the window from the old midnight (9 Jan 00:00Z) covers
    assert min(_utc(r["valid_from"]) for r in attributes["rates"]) <= datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)


def run_kraken_tests(my_predbat=None):
    """Run all KrakenAPI tests. Returns True on failure, False on success."""
    # Discovered, not listed: a hardcoded list silently skips any test added below it, and
    # unit_test.py invokes this runner rather than pytest discovery.
    module = sys.modules[__name__]
    # dir() gives a stable alphabetical order; these tests are independent of each other.
    tests = [obj for name in dir(module) if name.startswith("test_") and callable(obj := getattr(module, name))]
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


def test_kraken_api_edge_block_uses_all_bound_backoff_helpers():
    """Regression: KrakenAPI binds KrakenAuthMixin methods onto the instance one by one, so a
    helper reached from a bound method must be bound alongside it or it is AttributeError at
    runtime. Drive a real KrakenAPI through block -> suppression -> recovery so all three
    (start_token_mint_backoff, log_token_mint_backoff, clear_token_mint_backoff) are exercised.
    """
    import kraken as kraken_module

    assert kraken_module._KrakenAuthMixin is not None, "KrakenAuthMixin must be importable for this test"

    api = make_kraken_api(auth_method="email", email="user@eon.com", password="secret123", key=None)

    async def _blocked(_input_vars):
        api.token_mint_edge_blocked = True
        return None

    # start_token_mint_backoff
    api._kraken_token_request = AsyncMock(side_effect=_blocked)
    assert asyncio.run(api.check_and_refresh_oauth_token()) is False
    assert api.oauth_failed is False, "a CDN block must not latch oauth_failed"
    assert api.token_mint_backoff.count == 1

    # log_token_mint_backoff - suppressed call inside the window
    api._kraken_token_request = AsyncMock()
    assert asyncio.run(api.check_and_refresh_oauth_token()) is False
    api._kraken_token_request.assert_not_called()

    # clear_token_mint_backoff - deadline elapsed, mint succeeds
    api.token_mint_backoff.until = datetime.now(timezone.utc) - timedelta(seconds=1)
    api._kraken_token_request = AsyncMock(return_value={"token": "recovered-jwt", "refreshToken": "recovered-refresh", "exp": int(time.time()) + 3600})
    assert asyncio.run(api.check_and_refresh_oauth_token()) is True
    assert api.access_token == "recovered-jwt"
    assert api.token_mint_backoff.until is None
    assert api.token_mint_backoff.count == 0


def test_graphql_query_expired_jwt_refreshes_and_retries():
    """KT-CT-1124 (expired JWT) is an auth error, so it refreshes and retries in the same call (#5582)."""
    api = make_kraken_api()
    api.access_token = "expired-token"
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    api.handle_oauth_401 = AsyncMock(return_value=True)

    expired_resp = AsyncMock()
    expired_resp.status = 200
    expired_resp.json = AsyncMock(return_value={"errors": [{"extensions": {"errorCode": "KT-CT-1124"}, "message": "Signature of the JWT has expired."}]})
    success_resp = AsyncMock()
    success_resp.status = 200
    success_resp.json = AsyncMock(return_value={"data": {"ok": True}})
    responses = [expired_resp, success_resp]

    def post_side_effect(*args, **kwargs):
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=responses.pop(0))
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    mock_session = AsyncMock()
    mock_session.post = MagicMock(side_effect=post_side_effect)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(api.async_graphql_query("query { test }", "test-context"))

    assert result == {"ok": True}
    api.handle_oauth_401.assert_called_once()


def test_is_alive_false_once_login_rejected():
    """A rejected login makes the component unhealthy even though cached tariff and rates remain (#5582)."""
    api = make_kraken_api()
    api.api_started = True
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    assert api.is_alive() is True
    assert api.health_message() is None

    api.oauth_failed = True
    assert api.is_alive() is False


def test_health_message_names_the_login_failure():
    """health_message() explains the rejected login, steering E.ON Next email users to an API key (#5582)."""
    eon_email = make_kraken_api(provider="eon", auth_method="email", key=None, email="me@example.com", password="pw")
    eon_email.oauth_failed = True
    message = eon_email.health_message()
    assert "E.ON Next" in message and "api_key" in message, message

    edf_key = make_kraken_api(provider="edf", auth_method="api_key")
    edf_key.oauth_failed = True
    message = edf_key.health_message()
    assert message.startswith("EDF login failed"), message
    assert "api_key" not in message, message


def test_fetch_dispatches_reports_whether_any_device_fetched():
    """async_fetch_dispatches returns False when every device's query failed, True when one succeeded (#5582)."""
    api = make_kraken_api()
    cached = [{"start": "2026-07-08T02:00:00Z", "end": "2026-07-08T05:00:00Z", "charge_in_kwh": 10.0}]
    api.intelligent_devices = {"dev-1": {"device_id": "dev-1", "planned_dispatches": list(cached), "completed_dispatches": []}}

    api.async_graphql_query = AsyncMock(return_value=None)
    assert asyncio.run(api.async_fetch_dispatches()) is False
    assert api.intelligent_devices["dev-1"]["planned_dispatches"] == cached, "a failed query must keep the cached dispatches"

    api.async_graphql_query = AsyncMock(return_value={"flexPlannedDispatches": [], "completedDispatches": []})
    assert asyncio.run(api.async_fetch_dispatches()) is True


def test_run_failed_dispatch_fetch_is_not_a_refresh():
    """When every dispatch query fails, run() must not stamp the cached dispatches as freshly fetched (#5582)."""
    from datetime import datetime, timedelta
    from kraken import KRAKEN_DISPATCH_REFRESH_MINUTES

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()
    stale = datetime.now() - timedelta(minutes=KRAKEN_DISPATCH_REFRESH_MINUTES + 30)
    api.dispatch_fetched_at = stale
    api.intelligent_devices = {"dev-1": {"device_id": "dev-1", "planned_dispatches": [], "completed_dispatches": [], "dispatches_fetched_at": "2026-10-16T09:00:00+00:00"}}
    api.async_graphql_query = AsyncMock(return_value=None)  # e.g. "Auth not available for dispatches"
    api.save_kraken_cache = AsyncMock()
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.get_arg = MagicMock(return_value=1)
    api.update_success_timestamp = MagicMock()

    result = asyncio.run(api.run(120, False))

    assert result is True
    assert api.dispatch_fetched_at == stale, "a fetch where every query failed must keep the old timestamp"
    api.save_kraken_cache.assert_not_called()
    # The cached dispatches are still published, carrying their real age
    entity = api.get_entity_name("binary_sensor", "intelligent_dispatch_1")
    call = [c for c in api.dashboard_item.call_args_list if c.args[0] == entity][0]
    assert call.kwargs["attributes"]["dispatches_fetched_at"] == "2026-10-16T09:00:00+00:00"


def test_run_account_status_publishes_failure_counts():
    """The account_status sensor carries requests_total / failures_total so failures are visible outside the log (#5582)."""
    from datetime import datetime

    api = make_kraken_api()
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()
    api.requests_total = 7
    api.failures_total = 3
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    asyncio.run(api.run(120, False))

    entity = api.get_entity_name("sensor", "account_status")
    call = [c for c in api.dashboard_item.call_args_list if c.args[0] == entity][0]
    assert call.kwargs["attributes"]["requests_total"] == 7
    assert call.kwargs["attributes"]["failures_total"] == 3


def test_fetch_dispatches_stamps_only_the_devices_that_refreshed():
    """With one device's query failing, only the device that refreshed gets a new dispatches_fetched_at (#5583 review)."""
    api = make_kraken_api()
    old_stamp = "2026-10-16T09:00:00+00:00"
    api.intelligent_devices = {
        "dev-ok": {"device_id": "dev-ok", "planned_dispatches": [], "completed_dispatches": [], "dispatches_fetched_at": old_stamp},
        "dev-bad": {"device_id": "dev-bad", "planned_dispatches": [{"start": "s", "end": "e"}], "completed_dispatches": [], "dispatches_fetched_at": old_stamp},
    }

    async def query(query_text, request_context):
        if "dev-bad" in query_text:
            return None
        return {"flexPlannedDispatches": [], "completedDispatches": []}

    api.async_graphql_query = AsyncMock(side_effect=query)

    assert asyncio.run(api.async_fetch_dispatches()) is True
    assert api.intelligent_devices["dev-ok"]["dispatches_fetched_at"] != old_stamp
    assert api.intelligent_devices["dev-bad"]["dispatches_fetched_at"] == old_stamp, "a failed device must keep its own old timestamp"
    assert api.intelligent_devices["dev-bad"]["planned_dispatches"] == [{"start": "s", "end": "e"}]


def test_discover_smart_devices_keeps_dispatches_fetched_at():
    """Rediscovery rebuilds each device dict but must carry its dispatches_fetched_at across (#5583 review)."""
    api = make_kraken_api()
    api.intelligent_devices = {"dev-1": {"device_id": "dev-1", "planned_dispatches": [], "completed_dispatches": [], "dispatches_fetched_at": "2026-10-16T09:00:00+00:00"}}
    api.async_graphql_query = AsyncMock(return_value={"devices": [{"id": "dev-1", "deviceType": "ELECTRIC_VEHICLES", "status": {"current": "LIVE"}, "__typename": "SmartFlexVehicle"}]})

    asyncio.run(api.async_discover_smart_devices())

    assert api.intelligent_devices["dev-1"]["dispatches_fetched_at"] == "2026-10-16T09:00:00+00:00"


# ── EDF Flextras booked free hours ───────────────────────────────────────────


def _flextras_screen(items):
    """A Flextras hours screen in the shape EDF serve it, with the given BOOKED_HOURS_LIST items."""
    return {
        "screen": "REWARD_HOURS",
        "version": "1.0",
        "components": [
            {"id": "reward_hours_banner_1", "type": "REWARD_HOURS_BANNER", "props": {"title": "x"}},
            {"id": "booked_hours_list_1", "type": "BOOKED_HOURS_LIST", "props": {"bonusHours": 4, "totalRemainingHours": 0, "items": items}},
        ],
    }


def _flextras_slot(date, start, end):
    """One booked hourly slot as it appears in rawTimeSlots."""
    return {"slotId": f"SLOT-{date}-{start[:2]}-{end[:2]}", "date": date, "startTime": start, "endTime": end, "status": "SELECTED"}


def _flextras_api():
    """An EDF api with the real Flextras fetch restored, a known tariff and mocked publishing."""
    api = make_kraken_api(provider="edf")
    del api.async_fetch_flextras_hours
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()
    return api


def test_flextras_parse_joins_adjacent_hours_and_keeps_gaps():
    """Back-to-back hours become one window; hours with a gap between them stay separate."""
    from kraken import KrakenAPI

    body = _flextras_screen(
        [
            {"rawTimeSlots": [_flextras_slot("2026-10-10", "09:00", "10:00"), _flextras_slot("2026-10-10", "10:00", "11:00")]},
            {"rawTimeSlots": [_flextras_slot("2026-11-01", "00:00", "01:00"), _flextras_slot("2026-11-01", "02:00", "03:00")]},
        ]
    )
    assert KrakenAPI.parse_flextras_booked_hours(body) == [
        {"start": "2026-10-10T08:00:00+0000", "end": "2026-10-10T10:00:00+0000"},
        {"start": "2026-11-01T00:00:00+0000", "end": "2026-11-01T01:00:00+0000"},
        {"start": "2026-11-01T02:00:00+0000", "end": "2026-11-01T03:00:00+0000"},
    ]


def test_flextras_parse_reads_uk_local_time_across_the_clock_change():
    """The same wall-clock hour maps to 08:00 UTC in BST and 09:00 UTC in GMT."""
    from kraken import KrakenAPI

    bst = KrakenAPI.parse_flextras_booked_hours(_flextras_screen([{"rawTimeSlots": [_flextras_slot("2026-10-24", "09:00", "10:00")]}]))
    gmt = KrakenAPI.parse_flextras_booked_hours(_flextras_screen([{"rawTimeSlots": [_flextras_slot("2026-10-26", "09:00", "10:00")]}]))
    assert bst[0]["start"] == "2026-10-24T08:00:00+0000"
    assert gmt[0]["start"] == "2026-10-26T09:00:00+0000"


def test_flextras_parse_hour_over_midnight_ends_next_day():
    """An 11pm-midnight hour is dated by the day it starts."""
    from kraken import KrakenAPI

    parsed = KrakenAPI.parse_flextras_booked_hours(_flextras_screen([{"rawTimeSlots": [_flextras_slot("2026-11-07", "23:00", "00:00")]}]))
    assert parsed == [{"start": "2026-11-07T23:00:00+0000", "end": "2026-11-08T00:00:00+0000"}]


def test_flextras_parse_distinguishes_nothing_booked_from_unrecognised():
    """A recognised screen with no bookings is [], anything else is None so the caller keeps what it had."""
    from kraken import KrakenAPI

    assert KrakenAPI.parse_flextras_booked_hours(_flextras_screen([])) == []
    assert KrakenAPI.parse_flextras_booked_hours({"components": [{"type": "FAQ_LIST", "props": {}}]}) is None
    assert KrakenAPI.parse_flextras_booked_hours({"error": "nope"}) is None
    assert KrakenAPI.parse_flextras_booked_hours(None) is None


def test_flextras_parse_skips_malformed_slots():
    """A slot with a missing or unparseable time is dropped, not raised on."""
    from kraken import KrakenAPI

    body = _flextras_screen([{"rawTimeSlots": [{"date": "2026-10-10", "startTime": None}, {"date": "bad", "startTime": "09:00", "endTime": "10:00"}, "junk", _flextras_slot("2026-10-10", "12:00", "13:00")]}, "junk"])
    assert KrakenAPI.parse_flextras_booked_hours(body) == [{"start": "2026-10-10T11:00:00+0000", "end": "2026-10-10T12:00:00+0000"}]


def test_flextras_parse_reads_the_live_screen_shape():
    """The BOOKED_HOURS_LIST component as EDF served it live on 25 Oct 2026 (identifiers replaced): one booked weekend morning."""
    from kraken import KrakenAPI

    slots = [{"slotId": f"SLOT-2026-10-31-0{hour}-0{hour + 1}", "date": "2026-10-31", "startTime": f"0{hour}:00", "endTime": f"0{hour + 1}:00", "status": "SELECTED"} for hour in range(4)]
    body = {
        "screen": "REWARD_HOURS",
        "version": "1.0",
        "components": [
            {"id": "reward_hours_banner_1", "type": "REWARD_HOURS_BANNER", "order": 1, "status": "SUCCESS", "props": {"title": "No electricity hours"}},
            {
                "id": "booked_hours_list_1",
                "type": "BOOKED_HOURS_LIST",
                "order": 2,
                "status": "SUCCESS",
                "props": {
                    "title": "Booked hours",
                    "bonusHours": 4,
                    "bonusHoursRemaining": 0,
                    "totalRemainingHours": 0,
                    "items": [{"id": "booked-1-20261031", "date": "2026-10-31T00:00:00.000Z", "dateInText": "Saturday, 31 October", "rawTimeSlots": slots, "isAfterCutOff": False}],
                },
            },
            # Unbooked entitlement is a count in its own component, never a slot in the booked list
            {"id": "hours_to_use_1", "type": "HOURS_TO_USE_LIST", "order": 3, "status": "SUCCESS", "props": {"hoursLeft": 0, "items": [{"id": "x", "type": "bonus"}]}},
        ],
    }
    assert KrakenAPI.parse_flextras_booked_hours(body) == [{"start": "2026-10-31T00:00:00+0000", "end": "2026-10-31T04:00:00+0000"}]


def test_flextras_parse_prices_only_selected_slots():
    """Only a SELECTED slot (or one with no status) is a booking; any other status is skipped and logged once per parse."""
    from kraken import KrakenAPI

    available = dict(_flextras_slot("2026-10-10", "09:00", "10:00"), status="AVAILABLE")
    released = dict(_flextras_slot("2026-10-10", "10:00", "11:00"), status="RELEASED")
    released_again = dict(_flextras_slot("2026-10-10", "11:00", "12:00"), status="RELEASED")
    no_status = {key: value for key, value in _flextras_slot("2026-10-10", "14:00", "15:00").items() if key != "status"}
    body = _flextras_screen([{"rawTimeSlots": [available, released, released_again, _flextras_slot("2026-10-10", "12:00", "13:00"), no_status]}])

    messages = []
    assert KrakenAPI.parse_flextras_booked_hours(body, log=messages.append) == [
        {"start": "2026-10-10T11:00:00+0000", "end": "2026-10-10T12:00:00+0000"},
        {"start": "2026-10-10T13:00:00+0000", "end": "2026-10-10T14:00:00+0000"},
    ]
    assert len(messages) == 2, messages
    assert any("AVAILABLE" in message for message in messages) and any("RELEASED" in message for message in messages), messages
    # The log is optional, so existing callers are unchanged
    assert KrakenAPI.parse_flextras_booked_hours(body) == [
        {"start": "2026-10-10T11:00:00+0000", "end": "2026-10-10T12:00:00+0000"},
        {"start": "2026-10-10T13:00:00+0000", "end": "2026-10-10T14:00:00+0000"},
    ]


def test_flextras_parse_wrong_shape_is_unrecognised_not_empty():
    """A booked list of the wrong type, or one EDF mark as failed, is None (keep what is held), never [] and never raised on."""
    from kraken import KrakenAPI

    def screen_with_props(props, status=None):
        """A screen whose BOOKED_HOURS_LIST carries the given props and component status."""
        component = {"type": "BOOKED_HOURS_LIST", "props": props}
        if status is not None:
            component["status"] = status
        return {"components": [component]}

    slot = _flextras_slot("2026-10-10", "09:00", "10:00")
    # items that are not a list: a dict would iterate its keys and read as "nothing booked"; a scalar would raise
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": {"booked-1": {"rawTimeSlots": [slot]}}})) is None
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": 5})) is None
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": True})) is None
    # rawTimeSlots that are not a list
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": [{"rawTimeSlots": 3}]})) is None
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": [{"rawTimeSlots": {"0": slot}}]})) is None
    # A component EDF report as failed says nothing about what is booked
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": []}, status="ERROR")) is None
    # A successful component, or one with no status, is still read; absent items mean nothing booked
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": [{"rawTimeSlots": [slot]}]}, status="SUCCESS")) == [{"start": "2026-10-10T08:00:00+0000", "end": "2026-10-10T09:00:00+0000"}]
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({})) == []
    assert KrakenAPI.parse_flextras_booked_hours(screen_with_props({"items": [{"rawTimeSlots": None}]})) == []


def test_flextras_run_publishes_booked_hours_and_wires_free_electricity():
    """An EDF account with booked hours gets a free electricity sensor wired into octopus_free_electricity."""
    api = _flextras_api()
    api.async_find_property_id = AsyncMock(return_value="12345")
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    api.access_token = "jwt-token"

    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    body = _flextras_screen([{"rawTimeSlots": [_flextras_slot(future, "09:00", "10:00"), _flextras_slot(future, "10:00", "11:00")]}])
    response = AsyncMock(status=200)
    response.json = AsyncMock(return_value=body)
    session = AsyncMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(api.run(120, False))

    url = session.get.call_args.args[0]
    assert url == "https://www.edfenergy.com/support/energyhub/api/weekend-saver/v1/A-TEST123/12345/ui/hours"
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "jwt-token"

    entity = api.get_entity_name("sensor", "free_electricity")
    api.set_arg.assert_any_call("octopus_free_electricity", entity)
    call = [c for c in api.dashboard_item.call_args_list if c.args[0] == entity][0]
    events = call.kwargs["attributes"]["events"]
    assert len(events) == 1, "two back-to-back hours are one session"
    assert events[0]["rate"] == 0
    # fetch_octopus_sessions() parses these with str2time
    from utils import str2time

    assert (str2time(events[0]["end"]) - str2time(events[0]["start"])) == timedelta(hours=2)
    assert api.flextras_property_id == "12345"


def test_flextras_not_fetched_for_eon():
    """E.ON Next has no Flextras, so it is never asked."""
    api = make_kraken_api(provider="eon")
    api.current_tariff = {"tariff_code": "E-1R-VAR-01", "product_code": "VAR-01"}
    api.import_rates = [{"value_inc_vat": 24.5}]
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()

    asyncio.run(api.run(120, False))

    api.async_fetch_flextras_hours.assert_not_called()
    assert not [c for c in api.set_arg.call_args_list if c.args[0] == "octopus_free_electricity"]


def test_flextras_unavailable_is_asked_once_per_refresh_and_not_wired():
    """A non-200 (not on Flextras) is not retried every cycle, not a Kraken failure, and wires nothing."""
    api = _flextras_api()
    api.async_find_property_id = AsyncMock(return_value="12345")
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    api.access_token = "jwt-token"

    response = AsyncMock(status=403)
    session = AsyncMock()
    session.get = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=None)))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(api.run(120, False))
        asyncio.run(api.run(180, False))

    assert session.get.call_count == 1, "a second cycle inside the refresh period must not ask again"
    assert api.failures_total == 0
    assert not [c for c in api.set_arg.call_args_list if c.args[0] == "octopus_free_electricity"]
    assert len([c for c in api.log.call_args_list if "Flextras hours not available" in str(c)]) == 1


def test_flextras_failed_fetch_keeps_held_sessions():
    """A fetch that fails leaves the booked hours already held untouched."""
    api = _flextras_api()
    held = [{"start": "2030-01-05T09:00:00+0000", "end": "2030-01-05T10:00:00+0000"}]
    api.flextras_sessions = list(held)
    api.async_fetch_flextras_hours = AsyncMock(return_value=None)

    asyncio.run(api.run(120, False))

    assert api.flextras_sessions == held


def test_flextras_merge_drops_withdrawn_future_hours_but_keeps_started_ones():
    """A future hour handed back disappears; an hour already under way stays even if the screen drops it."""
    api = _flextras_api()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    started = {"start": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "end": (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    withdrawn = {"start": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "end": (now + timedelta(days=1, hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    old = {"start": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "end": (now - timedelta(days=3) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    fresh = {"start": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "end": (now + timedelta(days=2, hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
    api.flextras_sessions = [old, started, withdrawn]

    assert api._merge_flextras_sessions([fresh]) == [started, fresh]


def test_flextras_cache_round_trip():
    """Property id, sessions and the attempt stamp survive a restart, and cached sessions publish on the first run."""
    api = _flextras_api()
    storage = attach_fake_storage(api)
    api.flextras_property_id = "12345"
    api.flextras_sessions = [{"start": "2030-01-05T09:00:00+0000", "end": "2030-01-05T10:00:00+0000"}]
    api.flextras_fetched_at = datetime.now()
    asyncio.run(api.save_kraken_cache())

    restored = _flextras_api()
    restored.base.components = api.base.components
    assert storage.data
    restored.async_fetch_flextras_hours = AsyncMock(return_value=None)
    asyncio.run(restored.run(0, True))

    assert restored.flextras_property_id == "12345"
    assert restored.flextras_sessions == api.flextras_sessions
    restored.async_fetch_flextras_hours.assert_not_called()
    restored.set_arg.assert_any_call("octopus_free_electricity", restored.get_entity_name("sensor", "free_electricity"))


def test_find_property_id_reads_first_property():
    """The properties query's first id is the one the Flextras URL needs; a failed query is None."""
    api = make_kraken_api()
    api.async_graphql_query = AsyncMock(return_value={"properties": [{"id": 987}, {"id": 654}]})
    assert asyncio.run(api.async_find_property_id()) == "987"
    api.async_graphql_query = AsyncMock(return_value=None)
    assert asyncio.run(api.async_find_property_id()) is None


# ── Command line --config (apps.yaml) ────────────────────────────────────────


def _cli_settings(**given):
    """The CLI's settings dict as main() builds it, with every field unset unless given."""
    from kraken import KRAKEN_CLI_CONFIG_KEYS, KRAKEN_CLI_OAUTH_KEYS

    settings = {name: None for name in list(KRAKEN_CLI_CONFIG_KEYS) + list(KRAKEN_CLI_OAUTH_KEYS)}
    settings.update(given)
    return settings


def test_kraken_cli_config_keys_match_component():
    """Every apps.yaml key the CLI reads is one the Kraken component actually declares."""
    from components import COMPONENT_LIST
    from kraken import KRAKEN_CLI_CONFIG_KEYS

    declared = {spec.get("config") for spec in COMPONENT_LIST["kraken"]["args"].values() if spec.get("config")}
    for cli_name, config_key in KRAKEN_CLI_CONFIG_KEYS.items():
        assert config_key in declared, f"{cli_name} maps to {config_key}, which the kraken component does not declare: {sorted(declared)}"


def test_kraken_cli_config_oauth_takes_token_and_refresh_settings():
    """A SaaS-style OAuth config supplies the access token as key, plus the token and refresh settings."""
    from kraken import merge_kraken_cli_settings

    config = {
        "kraken_provider": "edf",
        "kraken_account_id": "A-1234ABCD",
        "kraken_auth_method": "oauth",
        "kraken_key": "access-token",
        "kraken_token_hash": "hash",
        "kraken_token_expires_at": datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc),
        "kraken_email": "ignored@example.com",
        "user_id": "inst-1",
        "supabase_url": "https://x.supabase.co",
        "supabase_key": "anon",
    }
    merged, used = merge_kraken_cli_settings(_cli_settings(), config)

    assert merged["auth_method"] == "oauth"
    assert merged["api_key"] == "access-token"
    assert merged["account_id"] == "A-1234ABCD"
    assert merged["token_hash"] == "hash"
    assert isinstance(merged["token_expires"], str), "a YAML timestamp arrives as a datetime; the CLI passes a string"
    assert (merged["user_id"], merged["supabase_url"], merged["supabase_key"]) == ("inst-1", "https://x.supabase.co", "anon")
    assert merged["email"] is None, "email is not an OAuth credential"
    assert "kraken_email" not in used and "kraken_key" in used


def test_kraken_cli_config_command_line_wins():
    """An explicit command line value beats the file, and a command line key overrides the file's OAuth method."""
    from kraken import merge_kraken_cli_settings

    config = {"kraken_account_id": "A-FROMFILE", "kraken_auth_method": "oauth", "kraken_key": "access-token", "kraken_token_hash": "hash"}
    merged, used = merge_kraken_cli_settings(_cli_settings(account_id="A-CLI", api_key="sk_live_cli"), config)

    assert merged["account_id"] == "A-CLI"
    assert merged["api_key"] == "sk_live_cli"
    assert merged["auth_method"] == "api_key"
    assert merged["token_hash"] is None, "an OAuth token hash does not apply to an API key run"
    assert used == []


def test_kraken_cli_config_email_method_skips_key():
    """With kraken_auth_method: email, the email and password are used and a stray kraken_key is not."""
    from kraken import merge_kraken_cli_settings

    config = {"kraken_account_id": "A-1", "kraken_auth_method": "email", "kraken_email": "me@example.com", "kraken_password": "pw", "kraken_key": "stale"}
    merged, _ = merge_kraken_cli_settings(_cli_settings(), config)

    assert (merged["email"], merged["password"], merged["api_key"]) == ("me@example.com", "pw", None)


def test_kraken_cli_config_reads_apps_yaml_with_secrets():
    """load_apps_yaml resolves !secret from a secrets.yaml beside the file, as --config sets it up."""
    import tempfile
    from utils import load_apps_yaml
    from kraken import merge_kraken_cli_settings

    with tempfile.TemporaryDirectory() as folder:
        apps = os.path.join(folder, "apps.yaml")
        with open(apps, "w") as handle:
            handle.write("pred_bat:\n  module: predbat\n  kraken_provider: edf\n  kraken_account_id: A-1234ABCD\n  kraken_auth_method: api_key\n  kraken_key: !secret kraken_key\n")
        with open(os.path.join(folder, "secrets.yaml"), "w") as handle:
            handle.write("kraken_key: sk_live_secret\n")
        saved = os.environ.get("PREDBAT_SECRETS_FILE")
        os.environ["PREDBAT_SECRETS_FILE"] = os.path.join(folder, "secrets.yaml")
        try:
            config, _ = load_apps_yaml(apps, log=lambda *a, **k: None)
        finally:
            if saved is None:
                os.environ.pop("PREDBAT_SECRETS_FILE", None)
            else:
                os.environ["PREDBAT_SECRETS_FILE"] = saved

    merged, used = merge_kraken_cli_settings(_cli_settings(), config)
    assert merged["api_key"] == "sk_live_secret"
    assert merged["provider"] == "edf" and merged["account_id"] == "A-1234ABCD"
    assert set(used) == {"kraken_provider", "kraken_account_id", "kraken_auth_method", "kraken_key"}


KRAKEN_CLOUDFRONT_403_BODY = "<!DOCTYPE HTML><HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE></HEAD><BODY>Request blocked.\nGenerated by cloudfront (CloudFront)</BODY></HTML>"
KRAKEN_RATE_LIMIT_RESPONSE = (200, {"errors": [{"message": "Too many requests.", "extensions": {"errorCode": "KT-CT-1199"}}]}, "", {})
KRAKEN_SUCCESS_RESPONSE = (200, {"data": {"ok": True}}, "", {})


def make_graphql_session(*responses):
    """Session whose posts hand out the given (status, json, text, headers) responses in turn, the last repeating."""
    queue = list(responses)

    def post(*args, **kwargs):
        """Wrap the next queued response in an async context manager."""
        status, json_body, text, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_body)
        resp.text = AsyncMock(return_value=text)
        resp.headers = headers
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    session = AsyncMock()
    session.post = MagicMock(side_effect=post)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def make_rate_limit_api():
    """KrakenAPI with a valid token, so queries reach the network mock."""
    api = make_kraken_api()
    api.access_token = "jwt-token-123"
    api.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    return api


def test_graphql_query_rate_limit_pauses_queries():
    """A KT-CT-1199 pauses the queries after it, rather than sending the rest of the poll into the same limit."""
    from kraken import GRAPHQL_RATE_LIMIT_BACKOFF_BASE_SECONDS

    api = make_rate_limit_api()
    session = make_graphql_session(KRAKEN_RATE_LIMIT_RESPONSE)
    before = datetime.now(timezone.utc)
    with patch("aiohttp.ClientSession", return_value=session):
        assert asyncio.run(api.async_graphql_query("query { a }", "first")) is None
        assert api.graphql_backoff.until >= before + timedelta(seconds=GRAPHQL_RATE_LIMIT_BACKOFF_BASE_SECONDS - 1)
        # Timed in UTC, the same clock as the mint backoff, so a clock change cannot stretch or shorten it
        assert api.graphql_backoff.until.utcoffset() == timedelta(0)
        assert asyncio.run(api.async_graphql_query("query { b }", "second")) is None

    assert session.post.call_count == 1, "the query after a rate limit was still sent"
    assert api.check_and_refresh_oauth_token.call_count == 1, "a held query should not touch the token"


def test_graphql_query_429_honours_retry_after():
    """An HTTP 429 pauses queries for at least its Retry-After."""
    api = make_rate_limit_api()
    session = make_graphql_session((429, None, "Too Many Requests", {"Retry-After": "600"}))
    before = datetime.now(timezone.utc)
    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(api.async_graphql_query("query { a }", "q"))

    assert api.graphql_backoff.until >= before + timedelta(seconds=599)


def test_graphql_query_edge_block_pauses_queries():
    """A CDN block page on a query pauses queries too."""
    api = make_rate_limit_api()
    session = make_graphql_session((403, None, KRAKEN_CLOUDFRONT_403_BODY, {}))
    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(api.async_graphql_query("query { a }", "q"))

    assert api.graphql_backoff.until is not None
    logged = " ".join(str(c.args[0]) for c in api.log.call_args_list)
    assert "edge/WAF blocked" in logged, logged


def test_graphql_query_plain_error_does_not_pause():
    """An ordinary HTTP error is not a rate limit."""
    api = make_rate_limit_api()
    session = make_graphql_session((400, None, '{"errors": [{"message": "bad field"}]}', {}))
    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(api.async_graphql_query("query { a }", "q"))

    assert api.graphql_backoff.until is None


def test_graphql_query_rate_limit_doubles_then_clears():
    """Consecutive rate limits double the pause; the first success clears it."""
    from kraken import GRAPHQL_RATE_LIMIT_BACKOFF_BASE_SECONDS

    api = make_rate_limit_api()
    session = make_graphql_session(KRAKEN_RATE_LIMIT_RESPONSE, KRAKEN_RATE_LIMIT_RESPONSE, KRAKEN_SUCCESS_RESPONSE)
    with patch("aiohttp.ClientSession", return_value=session):
        asyncio.run(api.async_graphql_query("query { a }", "q"))
        api.graphql_backoff.until = datetime.now(timezone.utc) - timedelta(seconds=1)
        before = datetime.now(timezone.utc)
        asyncio.run(api.async_graphql_query("query { a }", "q"))
        assert api.graphql_backoff.count == 2
        assert api.graphql_backoff.until >= before + timedelta(seconds=2 * GRAPHQL_RATE_LIMIT_BACKOFF_BASE_SECONDS - 1)
        api.graphql_backoff.until = datetime.now(timezone.utc) - timedelta(seconds=1)
        result = asyncio.run(api.async_graphql_query("query { a }", "q"))

    assert result == {"ok": True}
    assert api.graphql_backoff.count == 0
    assert api.graphql_backoff.until is None
    logged = " ".join(str(c.args[0]) for c in api.log.call_args_list)
    assert "rate limit cleared" in logged, logged
