# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk API skeleton + Bearer request layer
# -----------------------------------------------------------------------------

"""Tests for the SunsynkAPI component (Tasks 8-9).

Task 8 covers the injected-token Bearer request layer. Task 9 adds device discovery
(single-plant scoped) and read-only telemetry (flow/battery/grid/input/output) - publishing
and control land in later tasks and are not tested here.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sunsynk import SunsynkAPI, MockBase, SUNSYNK_REFRESH_REALTIME
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session

# Real captured demo responses (Task 9 brief) - used verbatim as test fixtures so the parsing
# logic is checked against the actual Sunsynk Connect API contract, not an invented shape.
PLANTS_RESPONSE = {"code": 0, "success": True, "data": {"infos": [{"id": 505825, "name": "Virtual power station", "status": 1}]}}

INVERTERS_RESPONSE = {
    "code": 0,
    "success": True,
    "data": {
        "pageSize": 50,
        "total": 2,
        "infos": [
            {"id": 260047, "sn": "2504040106", "model": "SUN-50KW-SG01HP3-EU-BM4", "status": 1},
            {"id": 260046, "sn": "2504040164", "model": "SUN-50KW-SG01HP3-EU-BM4", "status": 1},
        ],
    },
}

FLOW_RESPONSE = {
    "code": 0,
    "success": True,
    "data": {
        "pvPower": 0,
        "battPower": 400,
        "gridOrMeterPower": 3179,
        "loadOrEpsPower": 3164,
        "soc": 100,
        "batTo": True,
        "toBat": False,
        "gridTo": True,
        "toGrid": False,
        "toLoad": True,
        "pvTo": False,
    },
}

BATTERY_RESPONSE = {"code": 0, "success": True, "data": {"power": 170, "capacity": "100.0", "soc": "100.0", "temp": "17.0", "voltage": "639.8"}}


def test_get_headers_uses_bearer_token():
    """get_headers() returns a plain Bearer + JSON content-type header, no signing."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    h = api.get_headers()
    assert h["Authorization"] == "Bearer tok123"
    assert h["Content-Type"] == "application/json"


def test_initialize_stores_key_as_access_token_regardless_of_auth_method():
    """'key' is always the Bearer access token, even when auth_method isn't 'oauth'.

    _init_oauth() only populates self.access_token when auth_method == "oauth" (None
    otherwise), but Sunsynk's injected-token model means 'key' IS the access token no
    matter what auth_method is configured - so initialize() must re-apply it afterwards.
    """
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, auth_method=None)
    assert api.access_token == "tok123"


def test_initialize_reapplies_token_hash_after_oauth_init():
    """A configured token_hash must survive _init_oauth(), which resets it to "".

    This is the same trap fox.py/deye.py document: _init_oauth() unconditionally sets
    self.token_hash = "" as part of its own bookkeeping, so the configured value has to be
    re-applied AFTER that call or the Predbat.com SaaS dedup keyed on the hash breaks.
    """
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, auth_method="oauth", token_hash="abc123hash")
    assert api.token_hash == "abc123hash"


def test_initialize_normalizes_inverter_sn():
    """inverter_sn is list-normalized the same way fox.py/deye.py do."""
    api_none = SunsynkAPI(MockBase(), key="tok123", automatic=True, inverter_sn=None)
    assert api_none.inverter_sn_filter == []

    api_str = SunsynkAPI(MockBase(), key="tok123", automatic=True, inverter_sn="SN1")
    assert api_str.inverter_sn_filter == ["SN1"]

    api_list = SunsynkAPI(MockBase(), key="tok123", automatic=True, inverter_sn=["SN1", "SN2"])
    assert api_list.inverter_sn_filter == ["SN1", "SN2"]


def test_is_alive_requires_started_and_token():
    """is_alive() is False until the component has started, even with a token set."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    assert api.is_alive() is False
    api.api_started = True
    assert api.is_alive() is True


def test_request_get_func_success_returns_parsed_json():
    """A 200 response returns the parsed JSON body and clears needs_reauth."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.needs_reauth = True  # Simulate a prior auth failure that has since cleared

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 0, "data": {"sn": "SN1"}})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        data = run_async(api.request_get("/api/v1/plants"))

    assert data == {"code": 0, "data": {"sn": "SN1"}}
    assert api.needs_reauth is False


def test_request_get_func_401_sets_needs_reauth_and_does_not_retry():
    """A 401 flags needs_reauth and does NOT attempt a re-login (token is injected)."""
    api = SunsynkAPI(MockBase(), key="stale-token", automatic=True)

    mock_response = create_aiohttp_mock_response(status=401, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        data, allow_retry = run_async(api.request_get_func("/api/v1/plants"))

    assert data is None
    assert allow_retry is False
    assert api.needs_reauth is True
    assert api.failures_total == 1


def test_request_get_func_403_sets_needs_reauth():
    """A 403 is treated the same as a 401 - injected token rejected, flag needs_reauth."""
    api = SunsynkAPI(MockBase(), key="stale-token", automatic=True)

    mock_response = create_aiohttp_mock_response(status=403, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        data, allow_retry = run_async(api.request_get_func("/api/v1/plants"))

    assert data is None
    assert allow_retry is False
    assert api.needs_reauth is True


def test_request_get_func_200_with_success_false_is_treated_as_failure():
    """Sunsynk's API family returns HTTP 200 with {"success": false} on failure (e.g. stale
    token, invalid request) rather than a non-2xx status. This must not be unwrapped into an
    empty/None 'data' payload as if the call succeeded - mirrors deye.py's
    `if not data.get("success", True)` discipline."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 1, "success": False, "msg": "token invalid", "data": None})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        data, allow_retry = run_async(api.request_get_func("/api/v1/plants"))

    assert data is None
    assert allow_retry is False
    assert api.failures_total == 1


def test_get_battery_treats_200_success_false_as_failure_and_does_not_cache():
    """End-to-end: a 200 response with {"success": false} must surface as None all the way up
    through request_get()/get_battery(), and must NOT populate the battery cache with an
    empty dict as if it were valid, fresh data."""
    api = SunsynkAPI(MockBase(), key="stale-token", automatic=True)

    mock_response = create_aiohttp_mock_response(status=200, json_data={"code": 1, "success": False, "msg": "token invalid", "data": None})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = run_async(api.get_battery("SN1"))

    assert result is None
    assert "SN1" not in api.device_values["battery"]
    assert "device_values:battery:SN1" not in api.data_age


# -----------------------------------------------------------------------------
# Task 9: device discovery + telemetry reads
# -----------------------------------------------------------------------------


def test_get_device_list_uses_configured_plant_id_and_returns_devices():
    """When plant_id is configured, get_device_list() skips discovery entirely."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    api.request_get = AsyncMock(return_value=INVERTERS_RESPONSE)

    devices = run_async(api.get_device_list())

    assert api.request_get.await_count == 1
    assert {d["sn"] for d in devices} == {"2504040106", "2504040164"}
    assert all(d["plant_id"] == 505825 for d in devices)
    assert all(d["model"] == "SUN-50KW-SG01HP3-EU-BM4" for d in devices)


def test_get_device_list_discovers_single_plant_when_plant_id_not_configured():
    """Without a configured plant_id, GET /api/v1/plants is polled first for the sole plant."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(side_effect=[PLANTS_RESPONSE, INVERTERS_RESPONSE])

    devices = run_async(api.get_device_list())

    assert api.request_get.await_count == 2
    assert api.request_get.await_args_list[0].args[0] == "/api/v1/plants"
    assert api.request_get.await_args_list[1].args[0] == "/api/v1/plant/505825/inverters"
    assert {d["sn"] for d in devices} == {"2504040106", "2504040164"}
    assert all(d["plant_id"] == 505825 for d in devices)


def test_get_device_list_uses_first_plant_when_multiple_found():
    """Multi-plant accounts are defensive-only (SaaS handler rejects them at onboarding) -
    the first plant returned is used."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    multi_plant_response = {"code": 0, "success": True, "data": {"infos": [{"id": 111, "name": "Plant A", "status": 1}, {"id": 222, "name": "Plant B", "status": 1}]}}
    empty_inverters = {"code": 0, "success": True, "data": {"infos": []}}
    api.request_get = AsyncMock(side_effect=[multi_plant_response, empty_inverters])

    run_async(api.get_device_list())

    assert api.request_get.await_args_list[1].args[0] == "/api/v1/plant/111/inverters"


def test_get_device_list_filters_by_inverter_sn_filter():
    """inverter_sn_filter, when set, restricts the discovered devices."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825, inverter_sn="2504040106")
    api.request_get = AsyncMock(return_value=INVERTERS_RESPONSE)

    devices = run_async(api.get_device_list())

    assert [d["sn"] for d in devices] == ["2504040106"]


def test_get_device_list_returns_none_on_api_failure():
    """A failed inverters poll returns None and leaves any cached device_list untouched."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    api.request_get = AsyncMock(return_value=None)

    result = run_async(api.get_device_list())

    assert result is None
    assert api.device_list == []


def test_get_device_list_returns_none_when_no_plants_found():
    """When plant discovery finds no plants, get_device_list() fails closed with None."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    no_plants = {"code": 0, "success": True, "data": {"infos": []}}
    api.request_get = AsyncMock(return_value=no_plants)

    result = run_async(api.get_device_list())

    assert result is None
    assert api.request_get.await_count == 1  # never got to the inverters call


def test_get_device_list_caches_within_static_refresh_window():
    """A second call within SUNSYNK_REFRESH_STATIC re-uses the cached list, no new request."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    api.request_get = AsyncMock(return_value=INVERTERS_RESPONSE)

    first = run_async(api.get_device_list())
    second = run_async(api.get_device_list())

    assert api.request_get.await_count == 1
    assert second == first


def test_get_plant_flow_parses_flow_shape():
    """get_plant_flow() returns the parsed 'data' dict with the power-flow fields."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value=FLOW_RESPONSE)

    data = run_async(api.get_plant_flow(505825))

    assert data["battPower"] == 400
    assert data["batTo"] is True
    assert data["gridOrMeterPower"] == 3179
    assert data["gridTo"] is True
    assert data["soc"] == 100

    call = api.request_get.await_args
    assert call.args[0] == "/api/v1/plant/energy/505825/flow"
    assert "date" in call.kwargs["datain"]


def test_get_battery_parses_battery_shape():
    """get_battery() returns the parsed 'data' dict and requests the documented path/params."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value=BATTERY_RESPONSE)

    data = run_async(api.get_battery("2504040106"))

    assert data["power"] == 170
    assert data["soc"] == "100.0"
    assert data["capacity"] == "100.0"

    call = api.request_get.await_args
    assert call.args[0] == "/api/v1/inverter/battery/2504040106/realtime"
    assert call.kwargs["datain"] == {"sn": "2504040106", "lan": "en"}


def test_get_grid_requests_expected_path():
    """get_grid() hits /inverter/grid/{sn}/realtime with sn+lan query params."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value={"code": 0, "success": True, "data": {"gridPower": 123}})

    data = run_async(api.get_grid("SN1"))

    assert data["gridPower"] == 123
    call = api.request_get.await_args
    assert call.args[0] == "/api/v1/inverter/grid/SN1/realtime"
    assert call.kwargs["datain"] == {"sn": "SN1", "lan": "en"}


def test_get_input_requests_expected_path():
    """get_input() hits /inverter/{sn}/realtime/input."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value={"code": 0, "success": True, "data": {"pvPower": 5}})

    data = run_async(api.get_input("SN1"))

    assert data["pvPower"] == 5
    assert api.request_get.await_args.args[0] == "/api/v1/inverter/SN1/realtime/input"


def test_get_output_requests_expected_path():
    """get_output() hits /inverter/{sn}/realtime/output."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value={"code": 0, "success": True, "data": {"outputPower": 7}})

    data = run_async(api.get_output("SN1"))

    assert data["outputPower"] == 7
    assert api.request_get.await_args.args[0] == "/api/v1/inverter/SN1/realtime/output"


def test_get_battery_returns_none_on_api_failure():
    """A failed realtime poll returns None rather than an empty/partial dict."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value=None)

    assert run_async(api.get_battery("SN1")) is None


def test_realtime_reads_cache_within_refresh_window():
    """A second call for the same sn within SUNSYNK_REFRESH_REALTIME re-uses the cache."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value=BATTERY_RESPONSE)

    run_async(api.get_battery("SN1"))
    run_async(api.get_battery("SN1"))

    assert api.request_get.await_count == 1


def test_realtime_reads_are_cached_independently_per_sn():
    """Caching a value for one sn must not short-circuit the fetch for a different sn."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value=BATTERY_RESPONSE)

    run_async(api.get_battery("SN1"))
    run_async(api.get_battery("SN2"))

    assert api.request_get.await_count == 2


def test_realtime_cache_freshness_is_per_bucket_and_sn_not_a_shared_clock():
    """Reviewer scenario: all 5 telemetry buckets x all SNs must NOT share one clock.

    Fetch battery(SN1) and grid(SN1) at t0 (2 calls). Advance every data_age entry past
    SUNSYNK_REFRESH_REALTIME. Refetch grid(SN1) (3rd call) - this must only refresh grid(SN1)'s
    own age, not reset a shared "device_values" timestamp. Then fetch battery(SN1) again: it
    must ALSO be recognised as stale and refetch (4th call), not be served stale from cache
    just because a different bucket/sn was refreshed in between.
    """
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.request_get = AsyncMock(return_value=BATTERY_RESPONSE)

    run_async(api.get_battery("SN1"))  # call 1
    run_async(api.get_grid("SN1"))  # call 2
    assert api.request_get.await_count == 2

    # Clock-injection: age every tracked cache entry past the refresh window, same pattern
    # used elsewhere in this suite (test_fox_api.py, test_sigenergy.py, test_enphase_api.py).
    stale = datetime.now(timezone.utc) - timedelta(minutes=SUNSYNK_REFRESH_REALTIME + 1)
    for key in list(api.data_age.keys()):
        api.data_age[key] = stale

    run_async(api.get_grid("SN1"))  # call 3 - refetches; must only touch grid(SN1)'s own age
    assert api.request_get.await_count == 3

    run_async(api.get_battery("SN1"))  # call 4 - must ALSO refetch, not served stale
    assert api.request_get.await_count == 4
