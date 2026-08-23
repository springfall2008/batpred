# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the myenergi Zappi and Eddi integration
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async

from myenergi import (
    DEVICE_KIND_EDDI,
    DEVICE_KIND_ZAPPI,
    MyEnergiApiError,
    MyEnergiAuthError,
    MyEnergiCloudTransport,
    MyEnergiDirectTransport,
    MyEnergiTransport,
    normalise_cloud_device,
    normalise_direct_device,
)

# ============================================================================
# Mock data constants
# ============================================================================

# One entry from the "zappi" group of a direct /cgi-jstatus-* response
MOCK_DIRECT_ZAPPI = {
    "sno": 12345678,
    "sta": 3,
    "zmo": 2,
    "pst": "C2",
    "div": 7360,
    "che": 4.25,
    "grd": 120,
    "gen": 3400,
    "vol": 2405,
    "frq": 50.02,
}

# One entry from the "eddi" group of a direct /cgi-jstatus-* response
MOCK_DIRECT_EDDI = {
    "sno": 87654321,
    "sta": 3,
    "div": 1500,
    "che": 2.5,
    "grd": -40,
    "gen": 3400,
    "vol": 2401,
    "bsm": 0,
    "rbt": 0,
    "tp1": 54,
    "tp2": 127,
    "hno": 1,
}

# GET /devices/{id}/status for the same Zappi, plus its GET /devices metadata
MOCK_CLOUD_ZAPPI_STATUS = {
    "deviceClass": "ZAPPI",
    "status": "active",
    "state": "charging",
    "deviceStatus": "charging",
    "supplyMode": "eco",
    "pilotState": "C2",
    "boostCharge": False,
    "actualPower": 7.36,
    "gridPower": 0.12,
    "genPower": 3.4,
    "sessionEnergy": 4.25,
    "energyDelivered": 0.12,
}

MOCK_CLOUD_ZAPPI_META = {
    "deviceId": "ZA12345678",
    "model": "zappi",
    "alias": "Driveway",
    "serialNumber": 12345678,
    "online": True,
}

MOCK_CLOUD_EDDI_STATUS = {
    "deviceClass": "EDDI",
    "status": "active",
    "state": "waiting_for_surplus",
    "deviceStatus": "diverting",
    "boostActive": False,
    "actualPower": 1.5,
    "gridPower": -0.04,
    "genPower": 3.4,
    "sessionEnergy": 2.5,
}

MOCK_CLOUD_EDDI_META = {
    "deviceId": "ED87654321",
    "model": "eddi",
    "alias": "Hot water",
    "serialNumber": 87654321,
    "online": True,
}


def test_normalise_direct_zappi():
    """Direct Zappi payloads normalise into the shared device model."""
    device = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    assert device.device_id == "Z12345678"
    assert device.kind == DEVICE_KIND_ZAPPI
    assert device.serial == "12345678"
    assert device.status == "Charging"
    assert device.mode == "Eco"
    assert device.plug_status == "Charging"
    assert device.power_w == 7360
    assert device.grid_power_w == 120
    assert device.generation_w == 3400
    assert device.voltage == 240.5
    assert device.session_energy_kwh == 4.25
    assert device.boost_active is False
    assert device.temp_1 is None
    print("  ✓ Direct Zappi normalisation")


def test_normalise_direct_eddi():
    """Direct Eddi payloads normalise, including probe temperature handling."""
    device = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    assert device.device_id == "E87654321"
    assert device.kind == DEVICE_KIND_EDDI
    assert device.status == "Diverting"
    assert device.power_w == 1500
    assert device.session_energy_kwh == 2.5
    assert device.boost_active is False
    assert device.plug_status == ""
    assert device.temp_1 == 54
    # 127 is myenergi's "probe not connected" sentinel and must not be published
    assert device.temp_2 is None
    print("  ✓ Direct Eddi normalisation")


def test_normalise_direct_eddi_boosting():
    """An Eddi mid-boost reports boost_active and remaining minutes."""
    raw = dict(MOCK_DIRECT_EDDI, sta=4, bsm=1, rbt=1800)
    device = normalise_direct_device(raw, DEVICE_KIND_EDDI)
    assert device.status == "Boosting"
    assert device.boost_active is True
    assert device.boost_remaining_mins == 30
    print("  ✓ Direct Eddi boost state")


def test_normalise_direct_zappi_boosting():
    """A Zappi mid-boost reports status Boosting and boost_active True."""
    raw = dict(MOCK_DIRECT_ZAPPI, sta=4)
    device = normalise_direct_device(raw, DEVICE_KIND_ZAPPI)
    assert device.status == "Boosting"
    assert device.boost_active is True
    print("  ✓ Direct Zappi boost state")


def test_normalise_cloud_matches_direct():
    """Cloud and direct payloads for the same device produce equal values."""
    direct_zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    cloud_zappi = normalise_cloud_device(MOCK_CLOUD_ZAPPI_STATUS, MOCK_CLOUD_ZAPPI_META)
    assert cloud_zappi.kind == direct_zappi.kind
    assert cloud_zappi.serial == direct_zappi.serial
    assert cloud_zappi.status == direct_zappi.status
    assert cloud_zappi.mode == direct_zappi.mode
    assert cloud_zappi.plug_status == direct_zappi.plug_status
    # Cloud reports kW, direct reports W - both land in W
    assert cloud_zappi.power_w == direct_zappi.power_w
    assert cloud_zappi.generation_w == direct_zappi.generation_w
    assert cloud_zappi.session_energy_kwh == direct_zappi.session_energy_kwh
    # The cloud device id keeps its two letter prefix and the friendly alias is used
    assert cloud_zappi.device_id == "ZA12345678"
    assert cloud_zappi.name == "Driveway"

    direct_eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    cloud_eddi = normalise_cloud_device(MOCK_CLOUD_EDDI_STATUS, MOCK_CLOUD_EDDI_META)
    assert cloud_eddi.kind == direct_eddi.kind
    assert cloud_eddi.status == direct_eddi.status
    assert cloud_eddi.power_w == direct_eddi.power_w
    assert cloud_eddi.session_energy_kwh == direct_eddi.session_energy_kwh
    print("  ✓ Cloud and direct normalisation agree")


def test_normalise_handles_bad_values():
    """Out of range indices and missing fields fall back rather than raising."""
    device = normalise_direct_device({"sno": 1, "sta": 99, "zmo": "x"}, DEVICE_KIND_ZAPPI)
    assert device.status == "Unknown"
    assert device.mode == "Unknown"
    assert device.power_w == 0
    assert device.session_energy_kwh == 0

    device = normalise_direct_device({}, DEVICE_KIND_EDDI)
    assert device.serial == ""
    assert device.temp_1 is None

    device = normalise_cloud_device({"deviceClass": "EDDI"}, {})
    assert device.kind == DEVICE_KIND_EDDI
    assert device.power_w == 0
    print("  ✓ Malformed payloads degrade safely")


class _StubTransport(MyEnergiTransport):
    """Minimal concrete transport used to exercise the abstract base's stubs."""

    async def connect(self):
        """Pretend to connect."""
        return True

    async def fetch_devices(self):
        """Return no devices."""
        return []

    async def send_boost(self, device, amount, target_time=None):
        """Pretend to send a boost."""
        return True

    async def cancel_boost(self, device):
        """Pretend to cancel a boost."""
        return True


def test_transport_stubs():
    """Every unimplemented control returns False and warns exactly once."""
    messages = []
    transport = _StubTransport(messages.append)

    assert run_async(transport.set_mode(None, "Eco")) is False
    assert run_async(transport.set_priority(None, 1)) is False
    assert run_async(transport.set_min_green_level(None, 50)) is False
    assert run_async(transport.set_phase_setting(None, "1")) is False
    assert run_async(transport.get_schedule(None)) is False
    assert run_async(transport.set_schedule(None, [])) is False

    assert len(messages) == 6, "Each stub should warn once, got {}".format(messages)
    assert all("not implemented" in message for message in messages)

    # A second call must not warn again
    assert run_async(transport.set_mode(None, "Eco")) is False
    assert len(messages) == 6, "Repeat calls must not warn again"
    print("  ✓ Stubbed controls warn once and return False")


def _direct_response(json_data=None, asn="s18.myenergi.net", status=200, json_error=None):
    """Build a mock aiohttp response carrying an X_MYENERGI-asn header.

    Args:
        json_data: The value `.json()` resolves to. Ignored when `json_error` is set.
        asn: The X_MYENERGI-asn header value, or falsy to omit the header.
        status: The HTTP status code to report.
        json_error: When set, `.json()` raises this instead of returning `json_data`,
                    simulating an undecodable body such as a captive portal page.
    """
    response = MagicMock()
    response.status = status
    response.headers = {"X_MYENERGI-asn": asn} if asn else {}
    if json_error is not None:
        response.json = AsyncMock(side_effect=json_error)
    else:
        response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _direct_session(responses):
    """Build a mock aiohttp session whose get() returns the next queued response.

    Each queued item is either a mock response (from `_direct_response`) or an
    exception instance, which is raised instead - used to simulate a timeout or
    connection failure. Returns (session, calls), where calls records every
    requested URL in order.
    """
    calls = []
    queue = list(responses)

    def _get(url, **kwargs):
        """Record the requested URL, then return or raise the next queued item."""
        calls.append(url)
        item = queue.pop(0) if queue else _direct_response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.get = _get
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


MOCK_JSTATUS_ALL = [
    {"eddi": [MOCK_DIRECT_EDDI]},
    {"zappi": [MOCK_DIRECT_ZAPPI]},
    {"harvi": [{"sno": 11112222}]},
    {"asn": "s18.myenergi.net"},
    {"fwv": "3560S5.036"},
]


def test_direct_fetch_devices():
    """The direct transport resolves the ASN then parses the jstatus device groups."""
    session, calls = _direct_session([_direct_response([]), _direct_response(MOCK_JSTATUS_ALL)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        devices = run_async(transport.fetch_devices())

    assert calls[0].startswith("https://director.myenergi.net/cgi-jstatus-E"), calls
    assert calls[1] == "https://s18.myenergi.net/cgi-jstatus-*", calls
    assert transport.base_url == "https://s18.myenergi.net"
    # harvi is not a supported kind and must be skipped
    assert len(devices) == 2, [device.kind for device in devices]
    kinds = sorted(device.kind for device in devices)
    assert kinds == [DEVICE_KIND_EDDI, DEVICE_KIND_ZAPPI]
    print("  ✓ Direct transport resolves ASN and parses devices")


def test_direct_missing_asn_is_auth_error():
    """A response without the ASN header means bad credentials."""
    session, _calls = _direct_session([_direct_response([], asn=None)])
    transport = MyEnergiDirectTransport(print, "12345678", "wrong-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError:
            pass
    print("  ✓ Missing ASN header raises MyEnergiAuthError")


def test_direct_boost_urls():
    """Boost and cancel produce the exact documented URLs for both device kinds."""
    zappi = normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, zmo=2), DEVICE_KIND_ZAPPI)
    eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    # Pre-resolve the active server so the requests under test are the only ones made
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0}) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.send_boost(zappi, 10))
        run_async(transport.cancel_boost(zappi))
        run_async(transport.send_boost(eddi, 60))
        run_async(transport.cancel_boost(eddi))

    assert calls[0] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-0-10-10-0000", calls[0]
    assert calls[1] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-0-2-0-0000", calls[1]
    assert calls[2] == "https://s18.myenergi.net/cgi-eddi-boost-E87654321-10-1-60", calls[2]
    assert calls[3] == "https://s18.myenergi.net/cgi-eddi-boost-E87654321-1-1-0", calls[3]
    print("  ✓ Direct transport boost URLs")


def test_direct_smart_boost_url():
    """A Zappi boost with a target time uses the smart boost command."""
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0})])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.send_boost(zappi, 15, target_time="07:30"))

    assert calls[0] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-0-11-15-0730", calls[0]
    print("  ✓ Direct transport smart boost URL")


def test_direct_401_is_auth_error():
    """A 401 from the active server raises MyEnergiAuthError."""
    session, _calls = _direct_session([_direct_response([]), _direct_response({}, status=401)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError:
            pass
    print("  ✓ 401 from the active server raises MyEnergiAuthError")


def test_direct_401_missing_header_precedence():
    """A response that is both 401 and missing the ASN header raises via the header check, not the status check."""
    session, _calls = _direct_session([_direct_response([]), _direct_response({}, asn=None, status=401)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError as exc:
            # Proves the header check fired, not the 401 status check, which raises a different message
            assert "X_MYENERGI-asn" in str(exc), exc
    print("  ✓ Missing-header check runs ahead of the 401 status check")


def test_direct_non_200_sets_needs_asn_refresh():
    """A non-401 non-200 response from the active server raises MyEnergiApiError and forces ASN re-resolution."""
    session, _calls = _direct_session([_direct_response([]), _direct_response({}, status=500)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    assert transport.needs_asn_refresh is True
    print("  ✓ Non-200 from the active server raises MyEnergiApiError and sets needs_asn_refresh")


def test_direct_timeout_sets_needs_asn_refresh():
    """A timeout on the active-server request raises MyEnergiApiError and forces ASN re-resolution."""
    session, _calls = _direct_session([_direct_response([]), asyncio.TimeoutError()])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    assert transport.needs_asn_refresh is True
    print("  ✓ Timeout on the active-server request raises MyEnergiApiError and sets needs_asn_refresh")


def test_direct_asn_migration_follows_new_host():
    """A response naming a different active server updates base_url and routes the next call there."""
    session, calls = _direct_session(
        [
            _direct_response([]),  # director resolve -> s18
            _direct_response(MOCK_JSTATUS_ALL),  # first jstatus-* call, still on s18
            _direct_response(MOCK_JSTATUS_ALL, asn="s21.myenergi.net"),  # second call, server has migrated
            _direct_response(MOCK_JSTATUS_ALL),  # third call, now targets s21
        ]
    )
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.fetch_devices())
        assert transport.base_url == "https://s18.myenergi.net"
        run_async(transport.fetch_devices())
        assert transport.base_url == "https://s21.myenergi.net"
        run_async(transport.fetch_devices())

    assert calls[1] == "https://s18.myenergi.net/cgi-jstatus-*", calls
    assert calls[2] == "https://s18.myenergi.net/cgi-jstatus-*", calls
    assert calls[3] == "https://s21.myenergi.net/cgi-jstatus-*", calls
    print("  ✓ Active server migration is followed on the next request")


def test_direct_resolve_asn_non_200_is_api_error():
    """A non-200 response from the director during ASN resolution is a service outage, not bad credentials."""
    session, _calls = _direct_session([_direct_response({}, asn=None, status=503)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.connect())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ Non-200 while resolving the ASN raises MyEnergiApiError, not an auth error")


def test_direct_resolve_asn_timeout_is_api_error():
    """A timeout resolving the ASN raises MyEnergiApiError."""
    session, _calls = _direct_session([asyncio.TimeoutError()])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.connect())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ Timeout while resolving the ASN raises MyEnergiApiError")


MOCK_CLOUD_DEVICES = {
    "sites": [
        {
            "siteId": "site-1",
            "name": "Home",
            "gridLimit": 15,
            "devices": [
                MOCK_CLOUD_ZAPPI_META,
                MOCK_CLOUD_EDDI_META,
                {"deviceId": "HA11112222", "model": "harvi", "alias": "CT", "serialNumber": 11112222, "online": True},
            ],
        }
    ]
}


def _cloud_response(json_data=None, status=200, json_error=None):
    """Build a mock aiohttp response for the cloud API.

    Args:
        json_data: The value `.json()` resolves to. Ignored when `json_error` is set.
        status: The HTTP status code to report.
        json_error: When set, `.json()` raises this instead of returning `json_data`,
                    simulating an undecodable body such as an HTML error page.
    """
    response = MagicMock()
    response.status = status
    if json_error is not None:
        response.json = AsyncMock(side_effect=json_error)
    else:
        response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _cloud_session(responses):
    """Patch aiohttp.ClientSession recording (method, url, json) for each request.

    Each queued item is either a mock response (from `_cloud_response`) or an
    exception instance, which is raised instead - used to simulate a timeout or
    connection failure, mirroring `_direct_session` above.
    """
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        """Record the request, then return or raise the next queued item."""
        calls.append((method, url, kwargs.get("json")))
        item = queue.pop(0) if queue else _cloud_response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def test_cloud_fetch_devices():
    """The cloud transport lists devices then polls status for supported ones only."""
    session, calls = _cloud_session(
        [
            _cloud_response(MOCK_CLOUD_DEVICES),
            _cloud_response(MOCK_CLOUD_ZAPPI_STATUS),
            _cloud_response(MOCK_CLOUD_EDDI_STATUS),
        ]
    )
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        devices = run_async(transport.fetch_devices())

    assert calls[0] == ("GET", "https://api.s18.myenergi.net/devices", None), calls[0]
    assert calls[1][1] == "https://api.s18.myenergi.net/devices/ZA12345678/status", calls[1]
    assert calls[2][1] == "https://api.s18.myenergi.net/devices/ED87654321/status", calls[2]
    # harvi is unsupported and must never be polled
    assert len(calls) == 3, calls
    assert len(devices) == 2
    assert devices[0].name == "Driveway"
    print("  ✓ Cloud transport lists and polls supported devices")


def test_cloud_boost_bodies():
    """Boost bodies are shaped per device class, never mixing the two forms."""
    zappi = normalise_cloud_device(MOCK_CLOUD_ZAPPI_STATUS, MOCK_CLOUD_ZAPPI_META)
    eddi = normalise_cloud_device(MOCK_CLOUD_EDDI_STATUS, MOCK_CLOUD_EDDI_META)
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    session, calls = _cloud_session([_cloud_response({"commandId": "c1"}) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.send_boost(zappi, 10))
        run_async(transport.send_boost(eddi, 60))
        run_async(transport.cancel_boost(zappi))
        run_async(transport.cancel_boost(eddi))

    assert calls[0] == ("POST", "https://api.s18.myenergi.net/devices/ZA12345678/boost", {"mode": "normal", "parameters": {"energy": 10}}), calls[0]
    assert calls[1] == ("POST", "https://api.s18.myenergi.net/devices/ED87654321/boost", {"durationMinutes": 60}), calls[1]
    assert calls[2][0] == "DELETE" and calls[2][1].endswith("/devices/ZA12345678/boost"), calls[2]
    assert calls[3][0] == "DELETE" and calls[3][1].endswith("/devices/ED87654321/boost"), calls[3]

    # A Zappi body must never carry durationMinutes, an Eddi body never mode/parameters
    assert "durationMinutes" not in calls[0][2]
    assert "mode" not in calls[1][2] and "parameters" not in calls[1][2]
    print("  ✓ Cloud transport boost bodies")


def test_cloud_sets_bearer_header():
    """Requests carry the current bearer token from the supplied callable, re-read on every call.

    A single request would pass identically for an implementation that captured the
    token once in __init__, which is exactly the bug the callable design exists to
    avoid (OAuthMixin refreshing the token on the component must take effect on the
    very next request). Changing the token mid-test and issuing a second request
    proves the re-read, not just that a bearer header is sent at all.
    """
    tokens = ["first-token"]
    session, _calls = _cloud_session([_cloud_response(MOCK_CLOUD_DEVICES)])
    transport = MyEnergiCloudTransport(print, lambda: tokens[0])

    captured = {}

    def _client_session(**kwargs):
        """Capture the headers passed to aiohttp.ClientSession and return the mock session."""
        captured.update(kwargs.get("headers") or {})
        return session

    with patch("aiohttp.ClientSession", side_effect=_client_session):
        run_async(transport._request("GET", "/devices"))
        assert captured.get("Authorization") == "Bearer first-token", captured

        tokens[0] = "second-token"
        run_async(transport._request("GET", "/devices"))
        assert captured.get("Authorization") == "Bearer second-token", captured

    print("  ✓ Cloud transport re-reads the bearer token on every request")


def test_cloud_unauthorised_raises_auth_error():
    """An HTTP 401 from the cloud API surfaces as MyEnergiAuthError."""
    session, _calls = _cloud_session([_cloud_response({"message": "nope", "code": "UNAUTHORISED"}, status=401)])
    transport = MyEnergiCloudTransport(print, lambda: "stale-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError:
            pass
    print("  ✓ Cloud transport 401 raises MyEnergiAuthError")


def test_cloud_non_200_is_api_error():
    """A non-401, non-2xx response from the cloud API raises MyEnergiApiError."""
    session, _calls = _cloud_session([_cloud_response({"message": "boom"}, status=500)])
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ Cloud transport non-200 raises MyEnergiApiError")


def test_cloud_timeout_is_api_error():
    """A timeout calling the cloud API raises MyEnergiApiError."""
    session, _calls = _cloud_session([asyncio.TimeoutError()])
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ Cloud transport timeout raises MyEnergiApiError")


def test_cloud_non_json_response_is_api_error():
    """A 200 whose body cannot be decoded as JSON raises MyEnergiApiError, not a raw ValueError.

    aiohttp's json() is called with content_type=None, which disables the
    content-type guard - so a 200 carrying an HTML error page (CDN, proxy or
    maintenance interstitial) reaches the JSON decoder and must not escape as a
    bare json.JSONDecodeError (a ValueError subclass).
    """
    session, _calls = _cloud_session([_cloud_response(json_error=json.JSONDecodeError("Expecting value", "", 0))])
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ Cloud transport non-JSON body raises MyEnergiApiError")


def test_cloud_non_dict_payload_is_api_error():
    """A 200 whose decoded body is not a dict raises MyEnergiApiError, not a raw AttributeError.

    GET /devices is documented to return {"sites": [...]}; a body that decodes to a
    list or string instead must not reach payload.get() and crash with AttributeError.
    """
    session, _calls = _cloud_session([_cloud_response(["not", "a", "dict"])])
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ Cloud transport non-dict device list raises MyEnergiApiError")


def test_cloud_record_api_call_reasons():
    """record_api_call receives the documented reason vocabulary for every cloud failure branch."""
    scenarios = [
        (_cloud_response({}, status=401), "auth_error"),
        (_cloud_response({}, status=500), "server_error"),
        (_cloud_response({}, status=403), "client_error"),
        (asyncio.TimeoutError(), "connection_error"),
        (aiohttp.ClientConnectionError(), "connection_error"),
        (_cloud_response(json_error=ValueError("bad json")), "decode_error"),
    ]
    for queued, expected_reason in scenarios:
        session, _calls = _cloud_session([queued])
        transport = MyEnergiCloudTransport(print, lambda: "jwt-token")
        with patch("aiohttp.ClientSession", return_value=session), patch("myenergi.record_api_call") as mock_record:
            try:
                run_async(transport.fetch_devices())
                raise AssertionError("Expected a MyEnergiError for reason={}".format(expected_reason))
            except (MyEnergiAuthError, MyEnergiApiError):
                pass
        reasons = [call.kwargs.get("reason") for call in mock_record.call_args_list if call.kwargs.get("reason")]
        assert reasons == [expected_reason], (expected_reason, reasons)
    print("  ✓ Cloud transport records the documented reason for every failure branch")


def test_direct_client_error_reason_is_connection_error():
    """A generic aiohttp.ClientError from the active-server request records reason=connection_error.

    _resolve_asn and the cloud transport both use connection_error for this case;
    _request must be consistent with them rather than labelling it client_error,
    which is reserved for a non-401 4xx HTTP response, not a transport failure.
    """
    session, _calls = _direct_session([_direct_response([]), aiohttp.ClientConnectionError()])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session), patch("myenergi.record_api_call") as mock_record:
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass

    reasons = [call.kwargs.get("reason") for call in mock_record.call_args_list if call.kwargs.get("reason")]
    assert "connection_error" in reasons, reasons
    assert "client_error" not in reasons, reasons
    print("  ✓ Direct transport ClientError records reason=connection_error")


def test_direct_non_json_response_is_api_error():
    """A 200 body that cannot be decoded as JSON raises MyEnergiApiError with reason=decode_error.

    Mirrors test_cloud_non_json_response_is_api_error: MyEnergiDirectTransport._request
    also calls response.json(content_type=None), which disables aiohttp's content-type
    guard, so a captive portal or misconfigured proxy in front of the resolved ASN host
    can return a 200 whose body is not valid JSON. It must not escape as a raw
    json.JSONDecodeError (a ValueError subclass) - only MyEnergiError subclasses may
    leave a transport, since Task 5's component catches only that base class.
    """
    session, _calls = _direct_session([_direct_response([]), _direct_response(json_error=json.JSONDecodeError("Expecting value", "", 0))])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session), patch("myenergi.record_api_call") as mock_record:
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass

    reasons = [call.kwargs.get("reason") for call in mock_record.call_args_list if call.kwargs.get("reason")]
    assert reasons == ["decode_error"], reasons
    print("  ✓ Direct transport non-JSON body raises MyEnergiApiError with reason=decode_error")


def test_myenergi(my_predbat=None):
    """
    ======================================================================
    MYENERGI TEST SUITE
    ======================================================================
    Comprehensive test suite for the myenergi Zappi and Eddi integration including:
    - Payload normalisation for both transports
    """
    print("\n" + "=" * 70)
    print("MYENERGI TEST SUITE")
    print("=" * 70)

    test_normalise_direct_zappi()
    test_normalise_direct_eddi()
    test_normalise_direct_eddi_boosting()
    test_normalise_direct_zappi_boosting()
    test_normalise_cloud_matches_direct()
    test_normalise_handles_bad_values()
    test_transport_stubs()
    test_direct_fetch_devices()
    test_direct_missing_asn_is_auth_error()
    test_direct_boost_urls()
    test_direct_smart_boost_url()
    test_direct_401_is_auth_error()
    test_direct_401_missing_header_precedence()
    test_direct_non_200_sets_needs_asn_refresh()
    test_direct_timeout_sets_needs_asn_refresh()
    test_direct_asn_migration_follows_new_host()
    test_direct_resolve_asn_non_200_is_api_error()
    test_direct_resolve_asn_timeout_is_api_error()
    test_cloud_fetch_devices()
    test_cloud_boost_bodies()
    test_cloud_sets_bearer_header()
    test_cloud_unauthorised_raises_auth_error()
    test_cloud_non_200_is_api_error()
    test_cloud_timeout_is_api_error()
    test_cloud_non_json_response_is_api_error()
    test_cloud_non_dict_payload_is_api_error()
    test_cloud_record_api_call_reasons()
    test_direct_client_error_reason_is_connection_error()
    test_direct_non_json_response_is_api_error()

    print("=" * 70)
    return False
