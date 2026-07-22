# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk API skeleton + Bearer request layer
# -----------------------------------------------------------------------------

"""Tests for the SunsynkAPI component skeleton (Task 8).

Covers the injected-token Bearer request layer only - device discovery, publishing
and control land in later tasks and are not tested here.
"""

from unittest.mock import patch

from sunsynk import SunsynkAPI, MockBase
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session


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
