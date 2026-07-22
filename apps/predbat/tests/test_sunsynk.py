# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk API skeleton + Bearer request layer
# -----------------------------------------------------------------------------

"""Tests for the SunsynkAPI component (Tasks 8-13).

Task 8 covers the injected-token Bearer request layer. Task 9 adds device discovery
(single-plant scoped) and read-only telemetry (flow/battery/grid/input/output). Task 10 adds
publish_data() - entity publishing with the VERIFIED sign mapping. Task 11 adds
automatic_config() - the set_arg map wiring Predbat to the entities publish_data() emits.
Task 12 adds run() plus COMPONENT_LIST/config.py registration. Task 13 adds the standalone
CLI harness's RSA login helpers (publicKey -> RSA-encrypt password -> token/new) - tested here
via the payload/sign construction and the guarded `cryptography` import, NOT a live call (the
harness itself is exercised manually against a real Sunsynk account). Control writes land in a
later task and are not tested here.
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import sunsynk
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


# -----------------------------------------------------------------------------
# Task 10: publish_data() - entity publishing with the VERIFIED sign mapping
# -----------------------------------------------------------------------------


def test_sign_mapping_single_inverter_discharge_and_import():
    """Single-inverter plant, battery DISCHARGING (batTo=true) and grid IMPORTING
    (toGrid=false, gridTo=true): battery_power stays positive (discharge +ve), grid_power
    flips negative (predbat convention: grid +ve = export, so import must be negative)."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [{"sn": "2504040106", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825}]
    api.get_plant_flow = AsyncMock(return_value=dict(FLOW_RESPONSE["data"]))
    api.get_battery = AsyncMock(return_value=dict(BATTERY_RESPONSE["data"]))

    run_async(api.publish_data())

    assert api.base.entities["sensor.predbat_sunsynk_2504040106_battery_power"]["state"] == 400
    assert api.base.entities["sensor.predbat_sunsynk_2504040106_grid_power"]["state"] == -3179


def test_sign_mapping_single_inverter_charge_and_export():
    """Single-inverter plant, battery CHARGING (batTo=false, toBat=true) and grid EXPORTING
    (toGrid=true): battery_power flips negative (charge -ve), grid_power stays positive
    (export +ve)."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [{"sn": "2504040106", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825}]
    flow = {
        "pvPower": 5000,
        "battPower": 500,
        "batTo": False,
        "toBat": True,
        "gridOrMeterPower": 1000,
        "toGrid": True,
        "gridTo": False,
        "soc": 80,
        "loadOrEpsPower": 2000,
        "toLoad": True,
        "pvTo": True,
    }
    api.get_plant_flow = AsyncMock(return_value=flow)
    api.get_battery = AsyncMock(return_value=dict(BATTERY_RESPONSE["data"]))

    run_async(api.publish_data())

    assert api.base.entities["sensor.predbat_sunsynk_2504040106_battery_power"]["state"] == -500
    assert api.base.entities["sensor.predbat_sunsynk_2504040106_grid_power"]["state"] == 1000


def test_publish_data_single_inverter_also_publishes_soc_pv_load_and_battery_extras():
    """Single-inverter plant also publishes battery_soc/pv_power/load_power straight from
    flow, plus battery_temperature/soc_max sourced from the per-SN battery endpoint (flow
    carries neither field)."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [{"sn": "2504040106", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825}]
    api.get_plant_flow = AsyncMock(return_value=dict(FLOW_RESPONSE["data"]))
    api.get_battery = AsyncMock(return_value=dict(BATTERY_RESPONSE["data"]))

    run_async(api.publish_data())

    entities = api.base.entities
    assert entities["sensor.predbat_sunsynk_2504040106_battery_soc"]["state"] == 100
    assert entities["sensor.predbat_sunsynk_2504040106_pv_power"]["state"] == 0
    assert entities["sensor.predbat_sunsynk_2504040106_load_power"]["state"] == 3164
    assert entities["sensor.predbat_sunsynk_2504040106_battery_temperature"]["state"] == 17.0
    assert entities["sensor.predbat_sunsynk_2504040106_soc_max"]["state"] == 100.0


def test_publish_data_multi_inverter_avoids_double_count_and_warns_on_per_sn_signs():
    """Multi-inverter plant: per-SN entities must come from the per-SN battery/grid/input
    endpoints, NOT from plant flow (flow is plant-aggregated and would double-count a
    multi-inverter site - Codex Critical 3). Plant flow is fetched exactly once, used only
    for the site-level summary. A Warn: log must flag that per-SN signs are unconfirmed."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [
        {"sn": "SNA", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
        {"sn": "SNB", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
    ]
    api.get_plant_flow = AsyncMock(return_value=dict(FLOW_RESPONSE["data"]))

    battery_by_sn = {
        "SNA": {"power": 170, "capacity": "100.0", "soc": "55.0", "temp": "17.0", "voltage": "639.8"},
        "SNB": {"power": -220, "capacity": "150.0", "soc": "60.0", "temp": "19.5", "voltage": "640.1"},
    }
    grid_by_sn = {
        "SNA": {"vip": [{"power": 100}, {"power": 200}, {"power": 300}]},
        "SNB": {"power": 50},
    }
    input_by_sn = {
        "SNA": {"pv": [{"power": 1000}, {"power": 500}]},
        "SNB": {"pvPower": 750},
    }
    api.get_battery = AsyncMock(side_effect=lambda sn: battery_by_sn[sn])
    api.get_grid = AsyncMock(side_effect=lambda sn: grid_by_sn[sn])
    api.get_input = AsyncMock(side_effect=lambda sn: input_by_sn[sn])
    # Not under test here - the multi-inverter path also fetches per-SN output for load_power
    # (Fix 1); mocked purely to avoid a real network call from the unmocked get_output().
    api.get_output = AsyncMock(return_value={"pInv": 0})

    logs = []
    api.log = logs.append

    run_async(api.publish_data())

    entities = api.base.entities

    # Per-SN entities come from the per-SN endpoints, not plant flow (no double-count).
    assert entities["sensor.predbat_sunsynk_sna_battery_power"]["state"] == 170
    assert entities["sensor.predbat_sunsynk_snb_battery_power"]["state"] == -220
    assert entities["sensor.predbat_sunsynk_sna_grid_power"]["state"] == 600  # summed vip phases
    assert entities["sensor.predbat_sunsynk_snb_grid_power"]["state"] == 50  # flat fallback
    assert entities["sensor.predbat_sunsynk_sna_pv_power"]["state"] == 1500  # summed pv strings
    assert entities["sensor.predbat_sunsynk_snb_pv_power"]["state"] == 750  # flat fallback
    assert entities["sensor.predbat_sunsynk_sna_soc_max"]["state"] == 100.0
    assert entities["sensor.predbat_sunsynk_snb_soc_max"]["state"] == 150.0

    # Plant flow is used exactly once, for the site-level summary only - never per-SN.
    assert api.get_plant_flow.await_count == 1
    assert entities["sensor.predbat_sunsynk_site_battery_power"]["state"] == 400  # batTo=true
    assert entities["sensor.predbat_sunsynk_site_grid_power"]["state"] == -3179  # toGrid=false

    # Per-SN sign-uncertainty must be surfaced, not silently assumed correct.
    assert any("UNCONFIRMED" in message for message in logs)


def test_publish_data_multi_inverter_publishes_load_power_per_sn():
    """Multi-inverter plant: per-SN load_power must ALSO be published (Fix 1) - solis.py
    wires load_power as a per-device entity (solis.py:1273), so without a per-SN
    sensor.{prefix}_sunsynk_{sn}_load_power, a future automatic_config wiring step would have
    nothing to map for multi-inverter customers. Sourced from the per-SN output endpoint
    (inverter AC output), NOT plant flow's loadOrEpsPower (which is plant-aggregate and would
    double-count here, same as battery/grid/pv)."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [
        {"sn": "SNA", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
        {"sn": "SNB", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
    ]
    api.get_plant_flow = AsyncMock(return_value=dict(FLOW_RESPONSE["data"]))
    api.get_battery = AsyncMock(return_value=dict(BATTERY_RESPONSE["data"]))
    api.get_grid = AsyncMock(return_value={"power": 100})
    api.get_input = AsyncMock(return_value={"pvPower": 200})

    output_by_sn = {
        "SNA": {"vip": [{"power": 300}, {"power": 400}]},  # summed per-phase breakdown
        "SNB": {"pac": 650},  # flat fallback
    }
    api.get_output = AsyncMock(side_effect=lambda sn: output_by_sn[sn])

    logs = []
    api.log = logs.append

    run_async(api.publish_data())

    entities = api.base.entities
    assert entities["sensor.predbat_sunsynk_sna_load_power"]["state"] == 700
    assert entities["sensor.predbat_sunsynk_snb_load_power"]["state"] == 650

    # Per-SN load source uncertainty must be surfaced too, same discipline as the sign caveat.
    assert any("load_power source" in message and "UNCONFIRMED" in message for message in logs)


def test_multi_inverter_sn_does_not_refetch_battery_that_already_failed():
    """Fix 3: _publish_battery_extras() must NOT re-fetch a battery that the multi-inverter
    path already fetched and failed (None) this cycle - a plain `None` default couldn't tell
    'not fetched yet' apart from 'already fetched and failed', so a failing battery endpoint
    was being hit twice per publish cycle."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.get_battery = AsyncMock(return_value=None)
    api.get_grid = AsyncMock(return_value=None)
    api.get_input = AsyncMock(return_value=None)
    api.get_output = AsyncMock(return_value=None)

    run_async(api._publish_multi_inverter_sn("sensor.predbat_sunsynk", "SNA"))

    assert api.get_battery.await_count == 1


def test_publish_data_multi_inverter_skips_device_with_missing_sn_and_warns():
    """Fix 4: a device with no SN must be skipped (not crash on sn.lower()) AND must log a
    Warn:, rather than silently disappearing from published entities."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [
        {"sn": None, "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
        {"sn": "SNB", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
    ]
    api.get_plant_flow = AsyncMock(return_value=None)
    api.get_battery = AsyncMock(return_value=None)
    api.get_grid = AsyncMock(return_value=None)
    api.get_input = AsyncMock(return_value=None)
    api.get_output = AsyncMock(return_value=None)
    logs = []
    api.log = logs.append

    run_async(api.publish_data())

    assert any("skipping device with missing SN" in message for message in logs)
    assert api.get_battery.await_count == 1  # only attempted for SNB, never for the missing-sn device


def test_publish_data_multi_inverter_warns_when_flow_fetch_fails():
    """Fix 5: parity with the single-inverter branch - when plant flow fails, the multi-
    inverter branch must also log a Warn: (not silently skip the site summary)."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = [
        {"sn": "SNA", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
        {"sn": "SNB", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
    ]
    api.get_plant_flow = AsyncMock(return_value=None)
    api.get_battery = AsyncMock(return_value=None)
    api.get_grid = AsyncMock(return_value=None)
    api.get_input = AsyncMock(return_value=None)
    api.get_output = AsyncMock(return_value=None)
    logs = []
    api.log = logs.append

    run_async(api.publish_data())

    assert any("no plant flow data available, skipping site summary" in message for message in logs)
    assert "sensor.predbat_sunsynk_site_battery_power" not in api.base.entities


def test_publish_data_with_no_devices_logs_warning_and_publishes_nothing():
    """publish_data() with an empty device_list must not raise, and must publish nothing."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.device_list = []
    logs = []
    api.log = logs.append

    run_async(api.publish_data())

    assert api.base.entities == {}
    assert any("no discovered devices" in message for message in logs)


# -----------------------------------------------------------------------------
# Task 11: automatic_config() - set_arg map wiring Predbat to the Task 10 entities
# -----------------------------------------------------------------------------


def test_automatic_config_maps_two_inverters():
    """automatic_config() discovers devices via get_device_list() and emits a set_arg map
    whose per-device lists are ordered per the discovered device order, pointing at the
    EXACT entity ids publish_data() (Task 10) publishes - sensor.{prefix}_sunsynk_{sn}_{leaf}
    with SN lower-cased."""
    api = SunsynkAPI(MockBase(), key="t", automatic=True, plant_id=505825)
    api.request_get = AsyncMock(return_value=INVERTERS_RESPONSE)

    run_async(api.automatic_config())

    assert api.base.args["inverter_type"] == ["SUNSYNK", "SUNSYNK"]
    assert api.base.args["num_inverters"] == 2
    assert api.base.args["soc_percent"] == [
        "sensor.predbat_sunsynk_2504040106_battery_soc",
        "sensor.predbat_sunsynk_2504040164_battery_soc",
    ]
    assert api.base.args["battery_power"] == [
        "sensor.predbat_sunsynk_2504040106_battery_power",
        "sensor.predbat_sunsynk_2504040164_battery_power",
    ]
    assert api.base.args["grid_power"] == [
        "sensor.predbat_sunsynk_2504040106_grid_power",
        "sensor.predbat_sunsynk_2504040164_grid_power",
    ]
    assert api.base.args["load_power"] == [
        "sensor.predbat_sunsynk_2504040106_load_power",
        "sensor.predbat_sunsynk_2504040164_load_power",
    ]
    assert api.base.args["pv_power"] == [
        "sensor.predbat_sunsynk_2504040106_pv_power",
        "sensor.predbat_sunsynk_2504040164_pv_power",
    ]
    assert api.base.args["battery_temperature"] == [
        "sensor.predbat_sunsynk_2504040106_battery_temperature",
        "sensor.predbat_sunsynk_2504040164_battery_temperature",
    ]
    assert api.base.args["soc_max"] == [
        "sensor.predbat_sunsynk_2504040106_soc_max",
        "sensor.predbat_sunsynk_2504040164_soc_max",
    ]
    # A charge control entity - not published yet (Task 15), but must be named consistently.
    assert api.base.args["charge_start_time"] == [
        "select.predbat_sunsynk_2504040106_charge_slot1_start_time",
        "select.predbat_sunsynk_2504040164_charge_slot1_start_time",
    ]
    assert api.base.args["scheduled_discharge_enable"] == [
        "switch.predbat_sunsynk_2504040106_discharge_slot1_enable",
        "switch.predbat_sunsynk_2504040164_discharge_slot1_enable",
    ]


def test_automatic_config_respects_automatic_ignore_pv():
    """When automatic_ignore_pv is set, pv_power must NOT be wired (mirrors fox.py/deye.py),
    leaving any manually configured PV sensor in apps.yaml untouched."""
    api = SunsynkAPI(MockBase(), key="t", automatic=True, plant_id=505825, automatic_ignore_pv=True)
    api.request_get = AsyncMock(return_value=INVERTERS_RESPONSE)

    run_async(api.automatic_config())

    assert "pv_power" not in api.base.args


def test_automatic_config_logs_one_structured_json_line_with_the_full_map():
    """The complete arg map must be logged as ONE structured, Loki-greppable line (spec
    section 3.1, non-optional) - not scattered across many individual set_arg log lines."""
    api = SunsynkAPI(MockBase(), key="t", automatic=True, plant_id=505825)
    api.request_get = AsyncMock(return_value=INVERTERS_RESPONSE)
    logs = []
    api.log = logs.append

    run_async(api.automatic_config())

    matches = [message for message in logs if message.startswith("Info: SunsynkAPI automatic_config: ")]
    assert len(matches) == 1

    logged_map = json.loads(matches[0][len("Info: SunsynkAPI automatic_config: ") :])
    assert logged_map["inverter_type"] == ["SUNSYNK", "SUNSYNK"]
    assert logged_map["num_inverters"] == 2
    assert logged_map["soc_percent"] == api.base.args["soc_percent"]
    assert logged_map["charge_start_time"] == api.base.args["charge_start_time"]


def test_automatic_config_no_inverters_warns_and_sets_nothing():
    """No discovered devices: log a Warn: and set no args at all, rather than emitting an
    empty-list config that would silently disable Predbat's inverter control."""
    api = SunsynkAPI(MockBase(), key="t", automatic=True, plant_id=505825)
    api.request_get = AsyncMock(return_value={"code": 0, "success": True, "data": {"infos": []}})
    logs = []
    api.log = logs.append

    run_async(api.automatic_config())

    assert any("SunsynkAPI automatic_config found no inverters" in message for message in logs)
    assert api.base.args == {}


def test_automatic_config_skips_device_with_missing_sn_and_warns():
    """A device with no SN must be skipped (not crash on sn.lower()) and must log a Warn:,
    mirroring publish_data()'s Fix 4 discipline (Task 10)."""
    api = SunsynkAPI(MockBase(), key="t", automatic=True)
    api.get_device_list = AsyncMock(
        return_value=[
            {"sn": None, "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
            {"sn": "2504040164", "model": "SUN-50KW-SG01HP3-EU-BM4", "plant_id": 505825},
        ]
    )
    logs = []
    api.log = logs.append

    run_async(api.automatic_config())

    assert any("skipping device with missing SN" in message for message in logs)
    assert api.base.args["num_inverters"] == 1
    assert api.base.args["soc_percent"] == ["sensor.predbat_sunsynk_2504040164_battery_soc"]


# -----------------------------------------------------------------------------
# Task 12: run() loop + COMPONENT_LIST/config.py registration
# -----------------------------------------------------------------------------


def test_component_registered():
    """sunsynk is registered in COMPONENT_LIST, wired to SunsynkAPI with the expected
    event_filter, matching how fox/deye/solis register themselves.

    Imports ``predbat`` before ``components`` - components.py -> web.py -> predbat.py ->
    components.py is a genuine circular import, and entering it via a bare
    `from components import ...` (before anything has imported predbat) fails because
    Components/COMPONENT_LIST aren't defined yet in the partially-initialised components
    module when predbat.py re-enters it. unit_test.py's real suite avoids this by importing
    predbat first (`from predbat import PredBat`) before any test module runs; mirroring
    that order here keeps this test robust when run standalone via pytest too."""
    import predbat  # noqa: F401 - import order matters, see docstring above

    from components import COMPONENT_LIST

    assert "sunsynk" in COMPONENT_LIST
    entry = COMPONENT_LIST["sunsynk"]
    assert entry["class"] is SunsynkAPI
    assert entry["event_filter"] == "predbat_sunsynk_"
    assert entry["phase"] == 1
    assert entry["args"]["key"]["config"] == "sunsynk_key"
    assert entry["args"]["plant_id"]["config"] == "sunsynk_plant_id"
    assert entry["args"]["automatic"]["config"] == "sunsynk_automatic"
    assert entry["args"]["automatic"]["default"] is False


def test_component_registered_key_required_so_it_does_not_start_fleet_wide():
    """The key arg must be required: True (mirroring teslemetry's single-token gate), not
    required: False with a required_or fallback (deye/solis's multi-auth-path gate). Sunsynk
    has only one auth path (an injected sunsynk_key), so without this every individual arg is
    optional and Components.initialize()'s have_all_args check would be unconditionally True,
    starting SunsynkAPI for every instance regardless of whether it's a Sunsynk customer."""
    import predbat  # noqa: F401 - import order matters, see docstring on test_component_registered

    from components import COMPONENT_LIST

    assert COMPONENT_LIST["sunsynk"]["args"]["key"]["required"] is True


def test_component_registry_config_schema_has_sunsynk_keys():
    """APPS_SCHEMA declares the sunsynk_* keys, mirroring fox_*/solis_*/deye_*."""
    from config import APPS_SCHEMA

    assert APPS_SCHEMA["sunsynk_key"] == {"type": "string", "empty": False}
    assert APPS_SCHEMA["sunsynk_automatic"] == {"type": "boolean"}
    assert APPS_SCHEMA["sunsynk_plant_id"] == {"type": "string", "empty": False}


def test_run_first_publishes_and_runs_automatic_config_when_automatic():
    """First run: get_device_list() populates devices, publish_data() runs, and
    automatic_config() fires because first=True and automatic=True."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    api.get_device_list = AsyncMock(return_value=[{"sn": "SN1", "model": "M", "plant_id": 505825}])
    api.device_list = [{"sn": "SN1", "model": "M", "plant_id": 505825}]
    api.publish_data = AsyncMock()
    api.automatic_config = AsyncMock()

    result = run_async(api.run(5, True))

    assert result is True
    api.publish_data.assert_awaited_once()
    api.automatic_config.assert_awaited_once()
    assert api._known_sns == {"SN1"}


def test_run_not_automatic_skips_automatic_config():
    """When self.automatic is False, automatic_config() must never be called, even on first."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=False, plant_id=505825)
    api.get_device_list = AsyncMock(return_value=[{"sn": "SN1", "model": "M", "plant_id": 505825}])
    api.device_list = [{"sn": "SN1", "model": "M", "plant_id": 505825}]
    api.publish_data = AsyncMock()
    api.automatic_config = AsyncMock()

    result = run_async(api.run(5, True))

    assert result is True
    api.publish_data.assert_awaited_once()
    api.automatic_config.assert_not_awaited()


def test_run_no_devices_returns_false_and_skips_publish():
    """No discovered devices (empty self.device_list): run() logs an Error: and returns False
    without calling publish_data() or automatic_config()."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True)
    api.get_device_list = AsyncMock(return_value=None)
    api.device_list = []
    api.publish_data = AsyncMock()
    api.automatic_config = AsyncMock()
    logs = []
    api.log = logs.append

    result = run_async(api.run(5, True))

    assert result is False
    api.publish_data.assert_not_awaited()
    api.automatic_config.assert_not_awaited()
    assert any("No devices found" in message for message in logs)


def test_run_second_cycle_skips_automatic_config_when_device_set_unchanged():
    """On a later cycle (first=False) with the same discovered SNs as last time,
    automatic_config() must NOT be re-invoked."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    devices = [{"sn": "SN1", "model": "M", "plant_id": 505825}]
    api.get_device_list = AsyncMock(return_value=devices)
    api.device_list = devices
    api.publish_data = AsyncMock()
    api.automatic_config = AsyncMock()

    run_async(api.run(5, True))  # first cycle - seeds _known_sns, fires automatic_config
    api.automatic_config.reset_mock()

    result = run_async(api.run(5, False))  # second cycle - same device set

    assert result is True
    api.automatic_config.assert_not_awaited()


def test_run_reinvokes_automatic_config_when_device_set_changes():
    """When the discovered device set changes between cycles (e.g. an inverter is added),
    automatic_config() fires again even though first=False."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    devices_before = [{"sn": "SN1", "model": "M", "plant_id": 505825}]
    devices_after = [{"sn": "SN1", "model": "M", "plant_id": 505825}, {"sn": "SN2", "model": "M", "plant_id": 505825}]
    api.get_device_list = AsyncMock(return_value=devices_before)
    api.device_list = devices_before
    api.publish_data = AsyncMock()
    api.automatic_config = AsyncMock()

    run_async(api.run(5, True))
    api.automatic_config.reset_mock()

    api.get_device_list = AsyncMock(return_value=devices_after)
    api.device_list = devices_after
    result = run_async(api.run(5, False))

    assert result is True
    api.automatic_config.assert_awaited_once()
    assert api._known_sns == {"SN1", "SN2"}


def test_run_uses_cached_device_list_on_transient_api_failure():
    """Mirrors fox.py's run(): if get_device_list() returns None (transient API failure) but a
    previously-cached self.device_list is still populated, run() must still publish from the
    cached list rather than reporting "no devices"."""
    api = SunsynkAPI(MockBase(), key="tok123", automatic=True, plant_id=505825)
    api.device_list = [{"sn": "SN1", "model": "M", "plant_id": 505825}]  # pre-populated cache
    api.get_device_list = AsyncMock(return_value=None)  # this cycle's poll failed
    api.publish_data = AsyncMock()
    api.automatic_config = AsyncMock()

    result = run_async(api.run(5, True))

    assert result is True
    api.publish_data.assert_awaited_once()


# --- Task 13: standalone CLI harness RSA login helpers ---------------------------------------
#
# These test the payload/sign CONSTRUCTION only (no live network call - the harness itself is
# exercised manually against a real Sunsynk account, per the task brief). They also prove the
# `cryptography` import is correctly guarded, so importing sunsynk.py as a SaaS component never
# requires it.


def test_sunsynk_rsa_encrypt_password_round_trips_with_matching_private_key():
    """_sunsynk_rsa_encrypt_password() must produce a base64 PKCS1v15 ciphertext that decrypts
    back to the original plaintext with the matching private key - proves the standalone login
    helper's RSA step is wired correctly (right padding scheme, right PEM wrapping of Sunsynk's
    raw publicKey body) without ever calling the live Sunsynk API."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    public_pem = private_key.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    # Sunsynk's publicKey response body is the raw base64 key WITHOUT PEM header/footer -
    # strip them here, mirroring what _sunsynk_rsa_encrypt_password() itself re-wraps.
    raw_key = "".join(public_pem.strip().splitlines()[1:-1])

    encrypted_b64 = sunsynk._sunsynk_rsa_encrypt_password(raw_key, "hunter2")
    decrypted = private_key.decrypt(base64.b64decode(encrypted_b64), padding.PKCS1v15())

    assert decrypted == b"hunter2"


def test_sunsynk_build_login_payload_shape_and_sign():
    """_sunsynk_build_login_payload() must build the exact /oauth/token/new payload shape and
    MD5 sign the verified auth spike (scratchpad/sunsynk_auth_spike.py) uses, for a fixed
    nonce/raw_key - this is the payload the standalone CLI harness's login step sends."""
    with patch("sunsynk._sunsynk_rsa_encrypt_password", return_value="ENCRYPTED") as mock_rsa:
        payload = sunsynk._sunsynk_build_login_payload("RAWKEY1234567890", "user@example.com", "pw", nonce=1700000000000)

    mock_rsa.assert_called_once_with("RAWKEY1234567890", "pw")
    assert payload == {
        "username": "user@example.com",
        "password": "ENCRYPTED",
        "grant_type": "password",
        "client_id": "csp-web",
        "source": "sunsynk",
        "nonce": 1700000000000,
        "sign": hashlib.md5("nonce=1700000000000&source=sunsynkRAWKEY1234".encode()).hexdigest(),
    }


def test_sunsynk_rsa_encrypt_password_missing_cryptography_raises_clear_error():
    """When `cryptography` isn't installed, the standalone login helper must raise a clear,
    actionable RuntimeError (not a bare ImportError/traceback) - the SaaS/component import
    path is unaffected either way, since it never calls this function."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("No module named 'cryptography'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(RuntimeError, match="pip install cryptography"):
            sunsynk._sunsynk_rsa_encrypt_password("RAWKEY", "pw")


def test_sunsynk_module_has_no_top_level_cryptography_import():
    """Static guard: `cryptography` must never be imported at module scope in sunsynk.py -
    only inside _sunsynk_rsa_encrypt_password()'s guarded try/except (Task 13 brief) - so
    importing sunsynk.py as a SaaS component never requires the dependency. Checked via AST
    rather than an in-process reload (which would mutate the shared sunsynk module for every
    other test in this pytest session) - a real "does the guard work end-to-end" run is done
    separately, outside pytest, against a clean subprocess."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sunsynk))
    for node in tree.body:  # tree.body is module-level statements only, not nested in defs
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        assert not any(name == "cryptography" or name.startswith("cryptography.") for name in names), "cryptography must not be imported at module level"
