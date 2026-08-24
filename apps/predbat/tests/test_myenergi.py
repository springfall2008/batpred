# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the myenergi Zappi and Eddi integration
"""

import asyncio
import datetime
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytz

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async
from mock_base import MockBase

from myenergi import (
    BOOST_ENERGY_MAX,
    BOOST_ENERGY_MIN,
    BOOST_MINUTES_MAX,
    BOOST_MINUTES_MIN,
    CLOUD_DEVICE_LIST_MAX_AGE,
    DEFAULT_EDDI_BOOST_MINUTES,
    DEVICE_KIND_EDDI,
    DEVICE_KIND_ZAPPI,
    MAX_POLL_SECONDS,
    ZAPPI_PLUG_STATES,
    MyEnergiAPI,
    MyEnergiApiError,
    MyEnergiAuthError,
    MyEnergiCloudTransport,
    MyEnergiDevice,
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

    async def set_mode(self, device, mode):
        """Pretend to set a supply mode."""
        return True


def test_transport_stubs():
    """Every unimplemented control returns False and warns exactly once."""
    messages = []
    transport = _StubTransport(messages.append)

    assert run_async(transport.set_priority(None, 1)) is False
    assert run_async(transport.set_min_green_level(None, 50)) is False
    assert run_async(transport.set_phase_setting(None, "1")) is False
    assert run_async(transport.get_schedule(None)) is False
    assert run_async(transport.set_schedule(None, [])) is False

    assert len(messages) == 5, "Each stub should warn once, got {}".format(messages)
    assert all("not implemented" in message for message in messages)

    # A second call must not warn again
    assert run_async(transport.set_priority(None, 1)) is False
    assert len(messages) == 5, "Repeat calls must not warn again"
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


def test_direct_set_mode():
    """Setting a Zappi supply mode issues the documented mode command.

    The mode index comes from ZAPPI_CHARGE_MODES, so Fast is 1 and Stopped is 4 - the
    two Predbat-led charge control drives the charger with.
    """
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0}) for _ in range(3)])
    with patch("aiohttp.ClientSession", return_value=session):
        assert run_async(transport.set_mode(zappi, "Fast")) is True
        assert run_async(transport.set_mode(zappi, "Stopped")) is True
        assert run_async(transport.set_mode(zappi, "Eco+")) is True

    assert calls[0] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-1-0-0-0000", calls[0]
    assert calls[1] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-4-0-0-0000", calls[1]
    assert calls[2] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-3-0-0-0000", calls[2]
    print("  ✓ Direct transport sets the Zappi supply mode")


def test_direct_set_mode_rejects_bad_input():
    """An unknown mode, or a non-Zappi device, is refused without issuing a request.

    The Eddi mode command is a different endpoint with a different vocabulary, so
    falling through to the Zappi one would silently address the wrong device.
    """
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0})])
    with patch("aiohttp.ClientSession", return_value=session):
        for device, mode in ((zappi, "Turbo"), (eddi, "Fast")):
            try:
                run_async(transport.set_mode(device, mode))
                raise AssertionError("Expected MyEnergiApiError for {} {}".format(device.kind, mode))
            except MyEnergiApiError:
                pass
    assert calls == [], "No request may be made for an unsupported mode or device kind"
    print("  ✓ Direct transport refuses an unknown mode or a non-Zappi device")


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


def test_direct_missing_header_on_200_is_auth_error():
    """A 200 from the active server that carries no ASN header still fails the header check.

    The header check is what proves the digest handshake actually succeeded, so it must
    survive being moved below the status checks - it now applies to exactly the case it
    was meant for, a request that the server answered normally.
    """
    session, _calls = _direct_session([_direct_response([]), _direct_response(MOCK_JSTATUS_ALL, asn=None)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError as exc:
            assert "X_MYENERGI-asn" in str(exc), exc
    print("  ✓ A 200 without the ASN header raises MyEnergiAuthError")


def test_direct_401_without_header_is_a_credential_error():
    """A 401 that also lacks the ASN header is reported by the status check, naming the credentials.

    Both checks would raise MyEnergiAuthError here, so the message is what distinguishes
    them: the status check must win, because an error response has no reason to carry the
    header and diagnosing it as a missing header hides the actual 401.
    """
    session, _calls = _direct_session([_direct_response([]), _direct_response({}, asn=None, status=401)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError as exc:
            assert "rejected the credentials" in str(exc), exc
            assert "X_MYENERGI-asn" not in str(exc), exc
    print("  ✓ A 401 without the ASN header is reported as a credential failure")


def test_direct_503_without_header_is_api_error():
    """A provider outage on the active server is an API error, never a credential error.

    myenergi's own status page going down used to surface as "check the hub serial and
    API key", sending a self-hosted user off to regenerate a perfectly good key. A 503
    carries no ASN header, so this only passes while the status checks run first.
    """
    session, _calls = _direct_session([_direct_response([]), _direct_response({}, asn=None, status=503)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiAuthError as exc:
            raise AssertionError("A 503 must not be reported as an auth error: {}".format(exc))
        except MyEnergiApiError as exc:
            assert "503" in str(exc), exc
    print("  ✓ A 503 without the ASN header raises MyEnergiApiError, not an auth error")


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
            # The third call has to keep naming s21 explicitly: the default would migrate
            # base_url back to s18 behind the assertion below, which is the opposite of
            # what this test claims to be checking.
            _direct_response(MOCK_JSTATUS_ALL, asn="s21.myenergi.net"),  # third call, now targets s21
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


def test_cloud_set_mode():
    """The cloud transport posts the mode in the API's own lowercase vocabulary."""
    zappi = normalise_cloud_device(MOCK_CLOUD_ZAPPI_STATUS, MOCK_CLOUD_ZAPPI_META)
    eddi = normalise_cloud_device(MOCK_CLOUD_EDDI_STATUS, MOCK_CLOUD_EDDI_META)
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    session, calls = _cloud_session([_cloud_response({"commandId": "c1"}) for _ in range(2)])
    with patch("aiohttp.ClientSession", return_value=session):
        assert run_async(transport.set_mode(zappi, "Fast")) is True
        assert run_async(transport.set_mode(zappi, "Stopped")) is True
        for device, mode in ((zappi, "Turbo"), (eddi, "Fast")):
            try:
                run_async(transport.set_mode(device, mode))
                raise AssertionError("Expected MyEnergiApiError for {} {}".format(device.kind, mode))
            except MyEnergiApiError:
                pass

    assert calls[0] == ("POST", "https://api.s18.myenergi.net/devices/ZA12345678/mode", {"supplyMode": "fast"}), calls[0]
    # "Stopped" is the reported state, "stop" is what the API accepts - the two differ
    assert calls[1] == ("POST", "https://api.s18.myenergi.net/devices/ZA12345678/mode", {"supplyMode": "stop"}), calls[1]
    assert len(calls) == 2, "A bad mode or device kind must not reach the API: {}".format(calls)
    print("  ✓ Cloud transport sets the Zappi supply mode")


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


def _make_component(**overrides):
    """Build a MyEnergiAPI against MockBase with a stub transport already attached."""
    base = MockBase()
    args = {
        "auth_method": "direct",
        "hub_serial": "12345678",
        "api_key": "secret-key",
        "key": None,
        "token_expires_at": None,
        "token_hash": None,
        "automatic": True,
        "enable_controls": True,
        "poll_seconds": 60,
    }
    args.update(overrides)
    return MyEnergiAPI(base, **args)


CONTROL_TZ = pytz.timezone("Europe/London")


def _plan_window(start, end):
    """Build one planned-window dict in the shape output.py publishes."""
    return {"start": start.strftime("%m-%d %H:%M:%S"), "end": end.strftime("%m-%d %H:%M:%S")}


def _control_component(plans=None, **overrides):
    """Build a component on a pytz clock, with car charging plans already published.

    plans maps car number to a list of _plan_window() dicts; car 0's slot sensor has no
    postfix and later cars carry _1, _2 ... exactly as output.py names them.
    """
    component = _make_component(**overrides)
    component.local_tz = CONTROL_TZ
    component.base.local_tz = CONTROL_TZ
    # A Zappi is controlled from its own car's plan, so there must be that many cars
    component.base.num_cars = max([car_n + 1 for car_n in (plans or {})] or [1])
    for car_n, windows in (plans or {}).items():
        postfix = "" if car_n == 0 else "_{}".format(car_n)
        component.base.set_state_wrapper("binary_sensor.predbat_car_charging_slot" + postfix, "off", {"planned": windows})
    return component


def test_control_window_parsing():
    """Planned windows are read per car and matched against the clock.

    The slot sensor's own on/off state only refreshes on Predbat's five minute cycle, so
    the planned attribute is evaluated against the live clock here instead - otherwise
    every window boundary would be acted on up to five minutes late.
    """
    inside = _plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    component = _control_component(plans={0: [inside]})

    assert component.refresh_car_windows(CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 23, 30))) is True
    assert component.should_charge_now(0, CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 23, 30))) is True
    assert component.should_charge_now(0, CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 22, 59))) is False
    # The window end is exclusive, so the boundary minute is already outside
    assert component.should_charge_now(0, CONTROL_TZ.localize(datetime.datetime(2026, 8, 23, 1, 0))) is False
    print("  ✓ Planned car charging windows are parsed and matched against the clock")


def test_control_windows_across_new_year():
    """A window spanning New Year is still matched, read from either side of midnight.

    The plan carries no year, so a window read just after midnight on 1 January parses
    its 31 December start as this year - eleven months in the future - unless the year is
    rebuilt around the clock. Getting this wrong stops a car mid-charge once a year.
    """
    crossing = _plan_window(datetime.datetime(2026, 12, 31, 23, 0), datetime.datetime(2027, 1, 1, 5, 0))
    component = _control_component(plans={0: [crossing]})

    before_midnight = CONTROL_TZ.localize(datetime.datetime(2026, 12, 31, 23, 30))
    assert component.refresh_car_windows(before_midnight) is True
    assert component.should_charge_now(0, before_midnight) is True, "The window is active before midnight"

    after_midnight = CONTROL_TZ.localize(datetime.datetime(2027, 1, 1, 0, 30))
    assert component.refresh_car_windows(after_midnight) is True
    assert component.should_charge_now(0, after_midnight) is True, "The same window is still active after midnight"

    ended = CONTROL_TZ.localize(datetime.datetime(2027, 1, 1, 6, 0))
    assert component.refresh_car_windows(ended) is True
    assert component.should_charge_now(0, ended) is False, "The window has ended by 06:00"

    # A window genuinely far ahead must not be dragged back a year by the rebuild - the
    # plan reaches 48 hours, well beyond the 23 hour margin the first version allowed
    ahead = _plan_window(datetime.datetime(2026, 8, 23, 20, 0), datetime.datetime(2026, 8, 24, 2, 0))
    component = _control_component(plans={0: [ahead]})
    now = CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 10, 0))
    assert component.refresh_car_windows(now) is True
    assert component.should_charge_now(0, now) is False, "A window 34 hours ahead has not started"
    assert component.should_charge_now(0, CONTROL_TZ.localize(datetime.datetime(2026, 8, 23, 21, 0))) is True, "...and is active once it arrives"
    print("  ✓ Windows spanning New Year are matched from both sides of midnight")


def test_control_windows_are_per_car():
    """Each car's own slot sensor drives its own Zappi, so car 1 does not follow car 0."""
    car0 = _plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    car1 = _plan_window(datetime.datetime(2026, 8, 23, 4, 0), datetime.datetime(2026, 8, 23, 5, 0))
    component = _control_component(plans={0: [car0], 1: [car1]})

    now = CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 23, 30))
    assert component.refresh_car_windows(now) is True
    assert component.should_charge_now(0, now) is True
    assert component.should_charge_now(1, now) is False, "Car 1's window has not started yet"

    later = CONTROL_TZ.localize(datetime.datetime(2026, 8, 23, 4, 30))
    assert component.should_charge_now(0, later) is False
    assert component.should_charge_now(1, later) is True
    print("  ✓ Each car's plan drives its own Zappi")


def test_control_windows_tolerate_a_bad_entry_and_a_missing_plan():
    """A malformed window is skipped, and an unpublished plan reports nothing to act on.

    Returning False for a plan that has never been published is what stops the loop
    stopping a car on startup, before Predbat has decided anything.
    """
    good = _plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))
    now = CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 23, 30))

    component = _control_component(plans={0: [{"start": "nonsense"}, good]})
    assert component.refresh_car_windows(now) is True
    assert component.should_charge_now(0, now) is True, "The good window must survive a bad neighbour"

    never_published = _control_component()
    assert never_published.refresh_car_windows(now) is False
    assert never_published.should_charge_now(0, now) is False
    print("  ✓ A malformed window is skipped and a missing plan is not acted on")


def test_control_windows_cross_the_year_boundary():
    """A plan carries no year, so a window rebuilt around now must not land in the past."""
    # Published on 31 December for a window running into 1 January
    window = _plan_window(datetime.datetime(2026, 12, 31, 23, 30), datetime.datetime(2027, 1, 1, 1, 30))
    component = _control_component(plans={0: [window]})

    now = CONTROL_TZ.localize(datetime.datetime(2026, 12, 31, 23, 45))
    assert component.refresh_car_windows(now) is True
    assert component.should_charge_now(0, now) is True, "A window straddling New Year must still match"
    print("  ✓ Windows crossing the year boundary are rebuilt around now")


def _zappi(serial, mode_index=3):
    """Build a direct-API Zappi with a chosen supply mode (3 is Eco+)."""
    return normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, sno=serial, zmo=mode_index), DEVICE_KIND_ZAPPI)


IN_WINDOW = CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 23, 30))
OUT_OF_WINDOW = CONTROL_TZ.localize(datetime.datetime(2026, 8, 22, 20, 0))
NIGHT_WINDOW = _plan_window(datetime.datetime(2026, 8, 22, 23, 0), datetime.datetime(2026, 8, 23, 1, 0))


def test_control_charge_sets_fast_inside_and_stopped_outside():
    """Predbat holds the Zappi in Fast for a planned window and Stopped the rest of the time.

    Fast is the only mode that draws what the plan assumed - the window was chosen because
    the rate is cheap, not because there is sun, so Eco+ would leave the car uncharged.
    """
    component = _control_component(plans={0: [NIGHT_WINDOW]})
    component.devices = {"Z12345678": _zappi(12345678)}
    component.transport.set_mode = AsyncMock(return_value=True)

    run_async(component.control_charge(IN_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Fast", component.transport.set_mode.await_args

    run_async(component.control_charge(OUT_OF_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Stopped", component.transport.set_mode.await_args
    print("  ✓ Fast inside a planned window, Stopped outside it")


def test_control_charge_maps_each_zappi_to_its_own_car():
    """Zappis are matched to cars in serial order, the same order auto-config wires them.

    Car 0 is charging and car 1 is not, so the two Zappis must be driven differently in
    the same pass - a single shared decision would charge or stop both.
    """
    component = _control_component(plans={0: [NIGHT_WINDOW], 1: []})
    component.devices = {"Z12345678": _zappi(12345678), "Z22223333": _zappi(22223333)}
    component.transport.set_mode = AsyncMock(return_value=True)

    run_async(component.control_charge(IN_WINDOW))
    by_serial = {call.args[0].serial: call.args[1] for call in component.transport.set_mode.await_args_list}
    assert by_serial == {"12345678": "Fast", "22223333": "Stopped"}, by_serial
    print("  ✓ Each Zappi follows its own car's plan")


def test_control_charge_is_edge_triggered_but_corrects_drift():
    """A settled Zappi is left alone, but one changed in the myenergi app is put back.

    Purely edge-triggered control diverges silently the moment anything else touches the
    charger, so the mode already polled is compared against what was asked for.
    """
    component = _control_component(plans={0: [NIGHT_WINDOW]})
    component.devices = {"Z12345678": _zappi(12345678)}
    component.transport.set_mode = AsyncMock(return_value=True)

    run_async(component.control_charge(IN_WINDOW))
    assert component.transport.set_mode.await_count == 1
    # The poll now reports Fast, matching what was set, so nothing more is sent
    component.devices["Z12345678"] = _zappi(12345678, mode_index=1)
    run_async(component.control_charge(IN_WINDOW))
    assert component.transport.set_mode.await_count == 1, "A settled charger must not be re-commanded"

    # Someone switches it to Eco+ in the myenergi app - Predbat puts it back
    component.devices["Z12345678"] = _zappi(12345678, mode_index=3)
    run_async(component.control_charge(IN_WINDOW))
    assert component.transport.set_mode.await_count == 2, "Drift away from the set mode must be corrected"
    assert component.transport.set_mode.await_args.args[1] == "Fast"
    print("  ✓ Control is edge triggered but corrects drift")


def test_control_charge_ignores_eddis():
    """Only Zappis are driven - an Eddi has no car plan and a different mode vocabulary."""
    component = _control_component(plans={0: [NIGHT_WINDOW]})
    component.devices = {"E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)}
    component.transport.set_mode = AsyncMock(return_value=True)

    run_async(component.control_charge(IN_WINDOW))
    component.transport.set_mode.assert_not_awaited()
    print("  ✓ Eddis are never driven by car charge control")


def test_control_charge_does_nothing_before_a_plan_exists():
    """With no slot sensor published yet, nothing is commanded.

    Otherwise the first cycle after a restart would stop a car that Predbat has not yet
    decided anything about.
    """
    component = _control_component()
    component.devices = {"Z12345678": _zappi(12345678)}
    component.transport.set_mode = AsyncMock(return_value=True)

    run_async(component.control_charge(IN_WINDOW))
    component.transport.set_mode.assert_not_awaited()
    print("  ✓ Nothing is commanded before Predbat has published a plan")


def _controlling_component(plans=None, **overrides):
    """Build a component with Zappi control fully enabled and one Zappi already held."""
    args = {"zappi_control": True}
    args.update(overrides)
    component = _control_component(plans=plans, **args)
    component.devices = {"Z12345678": _zappi(12345678)}
    component.transport.set_mode = AsyncMock(return_value=True)
    # Capture the component's own log so the gating reasons can be asserted on
    component.log_messages = []
    component.log = component.log_messages.append
    component.enable_control()
    return component


def test_control_gating_refuses_with_a_reason():
    """Control only runs when it is asked for and can work, and says why when it will not."""
    assert _controlling_component().control_active is True

    for overrides, expected in (
        ({"zappi_control": False}, None),
        ({"automatic": False}, "myenergi_automatic"),
        ({"enable_controls": False}, "myenergi_enable_controls"),
    ):
        component = _controlling_component(**overrides)
        assert component.control_active is False, overrides
        if expected:
            assert any(expected in message for message in component.log_messages), (overrides, component.log_messages)
    print("  ✓ Zappi control refuses to run without its prerequisites, and says which")


def test_control_releases_to_the_saved_mode():
    """Releasing puts the Zappi back where it was before Predbat first moved it."""
    component = _controlling_component(plans={0: [NIGHT_WINDOW]})
    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Fast"

    # The switch going off is a release, not just a pause in commanding
    component.control_enabled = False
    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Eco+", "The Zappi was in Eco+ before Predbat took over"
    assert component.control_modes == {}, "A released Zappi is no longer held"
    print("  ✓ Releasing restores the mode the Zappi had before Predbat took over")


def test_control_releases_to_eco_plus_when_nothing_was_saved():
    """With no saved mode - a restart, or a mode that cannot be set - release falls back to Eco+."""
    component = _controlling_component(plans={0: [NIGHT_WINDOW]})
    component.control_modes = {"Z12345678": "Fast"}
    component.control_saved_modes = {}
    component.control_enabled = False

    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Eco+", component.transport.set_mode.await_args
    print("  ✓ Release falls back to Eco+ when there is nothing saved")


def test_control_stops_and_resumes_on_read_only():
    """Read only mode releases the Zappis, and clearing it resumes control."""
    component = _controlling_component(plans={0: [NIGHT_WINDOW]})
    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_count == 1

    component.base.args["set_read_only"] = True
    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Eco+", "Read only must release, not just stop commanding"

    # Still read only - nothing more is sent, there is nothing left to release
    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_count == 2

    component.base.args["set_read_only"] = False
    run_async(component.control_tick(IN_WINDOW))
    assert component.transport.set_mode.await_args.args[1] == "Fast", "Clearing read only must resume control"
    print("  ✓ Read only releases the Zappis and clearing it resumes control")


def test_control_switch_is_published_and_toggles_control():
    """The control switch is published on, and turning it off hands the Zappis back."""
    component = _controlling_component(plans={0: [NIGHT_WINDOW]})
    run_async(component.publish_data())
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_control") == "on"

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_control", "turn_off"))
    assert component.control_enabled is False
    run_async(component.publish_data())
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_control") == "off"

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_control", "turn_on"))
    assert component.control_enabled is True
    print("  ✓ The zappi control switch is published and toggles control")


def test_control_switch_is_not_published_when_control_cannot_run():
    """No switch appears when control could never act on it, rather than one that lies.

    With myenergi_enable_controls off the component is monitor only and enable_control()
    refuses, so a published switch would sit there reading "on" for a feature that cannot
    run. Toggling it would only make it responsive, not honest - so it is not published.
    """
    component = _controlling_component(enable_controls=False)
    assert component.control_active is False
    run_async(component.publish_data())
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_control") is None

    # It reappears, with its remembered state, once controls are allowed again
    allowed = _controlling_component()
    assert allowed.control_active is True
    run_async(allowed.publish_data())
    assert allowed.base.get_state_wrapper("switch.predbat_myenergi_zappi_control") == "on"
    print("  ✓ No control switch appears when control could not act on it")


def test_control_switch_publishes_its_restored_state_on_the_first_cycle():
    """A restart with control switched off must not show the switch on, even briefly.

    The saved state has to be restored before the first publish, or the switch reads "on"
    for a cycle and then flips - which looks like Predbat taking control back.
    """
    component = _control_component(plans={0: [NIGHT_WINDOW]}, zappi_control=True)
    component.transport.fetch_devices = AsyncMock(return_value=[_zappi(12345678)])
    component.transport.set_mode = AsyncMock(return_value=True)

    async def _load_off():
        """Stand in for storage returning a switched-off control state."""
        component.control_enabled = False

    component.load_control_enabled = _load_off

    run_async(component.run(0, True))
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_control") == "off"
    component.transport.set_mode.assert_not_awaited()
    print("  ✓ The control switch publishes its restored state on the first cycle")


def test_control_switch_is_not_published_without_the_feature():
    """With myenergi_zappi_control unset there is no switch, so no dead control appears."""
    component = _control_component()
    component.devices = {"Z12345678": _zappi(12345678)}
    run_async(component.publish_data())
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_control") is None
    print("  ✓ No control switch appears when the feature is off")


def test_run_enables_control_and_drives_the_zappi():
    """The run loop turns control on once, restores the switch, and drives the plan."""
    component = _control_component(plans={0: [NIGHT_WINDOW]}, zappi_control=True)
    zappi = _zappi(12345678)
    component.transport.fetch_devices = AsyncMock(return_value=[zappi])
    component.transport.set_mode = AsyncMock(return_value=True)
    component.load_control_enabled = AsyncMock()

    assert run_async(component.run(0, True)) is True
    assert component.control_active is True
    component.load_control_enabled.assert_awaited_once()
    # A real poll drove the Zappi from the plan without waiting for a second cycle
    assert component.transport.set_mode.await_count == 1, component.transport.set_mode.await_args_list
    print("  ✓ The run loop enables control and drives the Zappi from the plan")


def test_run_does_not_control_when_the_feature_is_off():
    """With myenergi_zappi_control unset the run loop never touches a Zappi's mode."""
    component = _control_component(plans={0: [NIGHT_WINDOW]})
    component.transport.fetch_devices = AsyncMock(return_value=[_zappi(12345678)])
    component.transport.set_mode = AsyncMock(return_value=True)

    assert run_async(component.run(0, True)) is True
    assert component.control_active is False
    component.transport.set_mode.assert_not_awaited()
    print("  ✓ The run loop leaves the Zappi alone when control is off")


def test_control_switch_state_survives_a_restart():
    """Turning control off is persisted, so a restart does not silently resume control."""
    saved = {}

    class _Storage:
        """Minimal in-memory stand-in for the Storage component."""

        async def save(self, module, filename, data, format="yaml", expiry=None):
            """Record the saved payload."""
            saved[(module, filename)] = data

        async def load(self, module, filename):
            """Return a previously saved payload, or None."""
            return saved.get((module, filename))

    class _Components:
        """Stand-in for the component registry, serving only the storage component."""

        def __init__(self, storage):
            """Hold the storage stand-in to serve."""
            self.storage = storage

        def get_component(self, name):
            """Return the storage stand-in, and nothing else."""
            return self.storage if name == "storage" else None

    storage = _Storage()
    component = _controlling_component(plans={0: [NIGHT_WINDOW]})
    # ComponentBase.storage is a read-only property resolving through base.components.
    # Each test builds its own MockBase, so this cannot leak into any other test.
    component.base.components = _Components(storage)

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_control", "turn_off"))
    assert saved, "Turning the switch off must be persisted"

    restarted = _controlling_component(plans={0: [NIGHT_WINDOW]})
    restarted.base.components = _Components(storage)
    assert restarted.control_enabled is True, "A fresh component starts with control on"
    run_async(restarted.load_control_enabled())
    assert restarted.control_enabled is False, "The saved off state must survive a restart"
    print("  ✓ The control switch state survives a restart")


def test_control_failure_does_not_break_the_poll():
    """A refused mode command is a warning, not a failed cycle.

    myenergi can refuse a mode for reasons Predbat cannot see - nothing plugged in, a
    fault on the charger. Letting that escape would mark the whole component errored and
    skip the success timestamp, so repeated refusals would eventually report it unhealthy
    even though monitoring is working perfectly.
    """
    component = _control_component(plans={0: [NIGHT_WINDOW]}, zappi_control=True)
    zappi = _zappi(12345678)
    component.transport.fetch_devices = AsyncMock(return_value=[zappi])
    component.transport.set_mode = AsyncMock(side_effect=MyEnergiApiError("myenergi refused the mode"))
    component.load_control_enabled = AsyncMock()

    assert run_async(component.run(0, True)) is True, "A refused mode must not fail the cycle"
    assert component.last_success_timestamp is not None, "Monitoring still succeeded, so the poll counts"
    # Nothing was recorded as set, so the next cycle tries again rather than assuming it stuck
    assert component.control_modes == {}, component.control_modes
    print("  ✓ A refused mode command warns without failing the poll")


def test_component_selects_transport():
    """auth_method picks the transport, and missing credentials refuse to start."""
    component = _make_component()
    assert isinstance(component.transport, MyEnergiDirectTransport)
    # _init_oauth() overwrites self.auth_method with its own "oauth"/"api_key" vocabulary,
    # so the user-facing configured value must survive under its own attribute name.
    assert component.auth_method_config == "direct"

    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key="jwt-token")
    assert isinstance(component.transport, MyEnergiCloudTransport)
    assert component.auth_method_config == "oauth"

    # No credentials at all - no transport, and the reason is logged
    component = _make_component(hub_serial=None, api_key=None)
    assert component.transport is None

    # oauth selected but neither the access token nor a stored token hash is set
    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key=None, token_hash=None)
    assert component.transport is None
    print("  ✓ Transport selection and credential validation")


def test_component_publishes_entities():
    """A poll publishes the documented entity set for each device."""
    component = _make_component()
    # A second, boosting Zappi so the boost switch's "on" case is actually exercised -
    # MOCK_DIRECT_ZAPPI alone only ever proves the switch can report "off".
    boosting_zappi = normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, sno=99999999, sta=4), DEVICE_KIND_ZAPPI)
    component.devices = {
        "Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI),
        "E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI),
        "Z99999999": boosting_zappi,
    }
    run_async(component.publish_data())

    entities = component.base.entities
    assert "sensor.predbat_myenergi_zappi_12345678_status" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_mode" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_plug_status" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_power" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_session_energy" in entities
    assert "binary_sensor.predbat_myenergi_zappi_12345678_charging" in entities
    assert "switch.predbat_myenergi_zappi_12345678_boost" in entities
    assert "number.predbat_myenergi_zappi_12345678_boost_energy" in entities

    assert "sensor.predbat_myenergi_eddi_87654321_status" in entities
    assert "sensor.predbat_myenergi_eddi_87654321_power" in entities
    assert "sensor.predbat_myenergi_eddi_87654321_session_energy" in entities
    assert "sensor.predbat_myenergi_eddi_87654321_temp_1" in entities
    assert "switch.predbat_myenergi_eddi_87654321_boost" in entities
    assert "number.predbat_myenergi_eddi_87654321_boost_minutes" in entities

    # tp2 was the 127 sentinel, so no entity should exist for it
    assert "sensor.predbat_myenergi_eddi_87654321_temp_2" not in entities

    # The boost switch reflects the device, not a locally held value
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_12345678_boost") == "off"
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_99999999_boost") == "on"

    # The charging binary sensor reflects device.status, not just whether it exists
    assert component.base.get_state_wrapper("binary_sensor.predbat_myenergi_zappi_12345678_charging") == "on"
    # The boosting device is not "Charging" (it is "Boosting"), so its charging sensor is off
    assert component.base.get_state_wrapper("binary_sensor.predbat_myenergi_zappi_99999999_charging") == "off"

    # Units come from the attribute table
    power = component.base.entities["sensor.predbat_myenergi_zappi_12345678_power"]
    assert power["attributes"]["unit_of_measurement"] == "W"
    assert power["attributes"]["device_class"] == "power"
    energy = component.base.entities["sensor.predbat_myenergi_zappi_12345678_session_energy"]
    assert energy["attributes"]["unit_of_measurement"] == "kWh"
    print("  ✓ Component publishes the expected entities")


def test_component_retains_last_good_reading():
    """A failed poll leaves the previously published values alone."""
    component = _make_component()
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    assert run_async(component.run(0, True)) is True
    good = component.base.get_state_wrapper("sensor.predbat_myenergi_zappi_12345678_session_energy")
    assert good == 4.25

    component.transport.fetch_devices = AsyncMock(side_effect=MyEnergiApiError("boom"))
    assert run_async(component.run(60, False)) is False
    still_good = component.base.get_state_wrapper("sensor.predbat_myenergi_zappi_12345678_session_energy")
    assert still_good == 4.25, "A failed poll must not overwrite the last good reading"
    print("  ✓ Failed polls retain the last good reading")


def test_component_empty_device_list_does_not_wipe_devices():
    """A successful poll returning no devices warns on the first run and never clears devices already known."""
    component = _make_component()
    messages = []
    component.log = messages.append

    component.transport.fetch_devices = AsyncMock(return_value=[])
    assert run_async(component.run(0, True)) is True
    assert component.devices == {}
    assert any("no Zappi or Eddi devices were found" in message for message in messages), messages

    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.transport.fetch_devices = AsyncMock(return_value=[])
    assert run_async(component.run(60, False)) is True
    assert "Z12345678" in component.devices, "An empty poll result must not wipe previously known devices"
    print("  ✓ An empty poll result warns once and never wipes previously known devices")


def test_component_poll_seconds_rounding():
    """poll_seconds is clamped to a whole number of base loop intervals, within the health window.

    The ceiling matters because the success timestamp is only stamped by a cycle that
    actually polled: components.py marks a component failed once its last success is over
    60 minutes old, so an unbounded poll interval would report a perfectly healthy
    component as broken.
    """
    assert _make_component(poll_seconds=1).poll_seconds == 60
    assert _make_component(poll_seconds=90).poll_seconds == 120
    assert _make_component(poll_seconds=300).poll_seconds == 300
    assert _make_component(poll_seconds=7200).poll_seconds == MAX_POLL_SECONDS
    assert MAX_POLL_SECONDS < 60 * 60, "The poll interval must stay inside components.py's 60 minute health window"
    print("  ✓ poll_seconds rounds to a multiple of 60 and stays inside the health window")


def test_component_oauth_refresh_failure_stops_the_poll():
    """A hard OAuth refresh failure (e.g. needs_reauth) stops run() before it ever calls the API with a dead token."""
    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key="jwt-token")
    component.check_and_refresh_oauth_token = AsyncMock(return_value=False)
    component.transport.fetch_devices = AsyncMock(return_value=[])

    assert run_async(component.run(0, True)) is False
    component.transport.fetch_devices.assert_not_awaited()
    print("  ✓ A failed OAuth refresh stops the poll before fetch_devices is ever called")


def test_component_registration():
    """The component is registered with matching config keys and event filter."""
    from components import COMPONENT_LIST
    from config import APPS_SCHEMA

    entry = COMPONENT_LIST["myenergi"]
    assert entry["class"] is MyEnergiAPI
    assert entry["event_filter"] == "predbat_myenergi_"
    assert entry["phase"] == 1
    assert entry["can_restart"] is True
    # token_hash must be in the gate: a refresh-only OAuth setup has no key, and
    # initialize() accepts that, so gating on key alone would never construct the component
    assert entry["required_or"] == ["api_key", "key", "token_hash"]

    # Every declared arg must name a config key that exists in the schema, and every
    # arg must be accepted by initialize()
    import inspect

    parameters = inspect.signature(MyEnergiAPI.initialize).parameters
    for arg_name, spec in entry["args"].items():
        assert arg_name in parameters, "initialize() has no parameter '{}'".format(arg_name)
        assert spec["config"] in APPS_SCHEMA, "{} missing from APPS_SCHEMA".format(spec["config"])

    # The reverse direction: every initialize() parameter must also be declared in
    # args, or a new parameter silently never receives a value from apps.yaml.
    expected = {name for name in parameters if name != "self"}
    assert set(entry["args"]) == expected, "COMPONENT_LIST args and initialize() parameters have diverged"
    print("  ✓ Component registration and schema keys")


def test_automatic_config():
    """Zappis wire into car_charging_energy and the first-by-serial Eddi into iboost_energy_today.

    Devices are inserted in deliberately reversed/shuffled order and a second Eddi is
    added, so this only passes if automatic_config() actually sorts by serial rather
    than relying on dict insertion order - which happened to match serial order in an
    earlier version of this test and let a missing sort go undetected.
    """
    component = _make_component()
    second_zappi = dict(MOCK_DIRECT_ZAPPI, sno=22223333)
    second_eddi = dict(MOCK_DIRECT_EDDI, sno=11112222)
    component.devices = {
        "E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI),
        "Z22223333": normalise_direct_device(second_zappi, DEVICE_KIND_ZAPPI),
        "E11112222": normalise_direct_device(second_eddi, DEVICE_KIND_EDDI),
        "Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI),
    }
    component.automatic_config()

    assert component.base.args["car_charging_energy"] == [
        "sensor.predbat_myenergi_zappi_12345678_session_energy",
        "sensor.predbat_myenergi_zappi_22223333_session_energy",
    ], component.base.args["car_charging_energy"]
    # car_charging_planned is indexed per car, so the list has to stay in the same
    # serial order as car_charging_energy or car N would be paired with another Zappi
    assert component.base.args["car_charging_planned"] == [
        "sensor.predbat_myenergi_zappi_12345678_plug_status",
        "sensor.predbat_myenergi_zappi_22223333_plug_status",
    ], component.base.args["car_charging_planned"]
    # 11112222 sorts before 87654321, so it must be the one picked as "the first Eddi"
    assert component.base.args["iboost_energy_today"] == "sensor.predbat_myenergi_eddi_11112222_session_energy"
    print("  ✓ Automatic configuration wires both energy inputs, deterministically by serial")


def test_automatic_config_wires_car_charging_power():
    """Zappi live power sensors wire into car_charging_power in the same serial order.

    The power sensors are display-only (the plan runs off car_charging_energy), but they have to
    line up with the energy list so both describe the same chargers.
    """
    component = _make_component()
    second_zappi = dict(MOCK_DIRECT_ZAPPI, sno=22223333)
    component.devices = {
        "Z22223333": normalise_direct_device(second_zappi, DEVICE_KIND_ZAPPI),
        "E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI),
        "Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI),
    }
    component.automatic_config()

    assert component.base.args["car_charging_power"] == [
        "sensor.predbat_myenergi_zappi_12345678_power",
        "sensor.predbat_myenergi_zappi_22223333_power",
    ], component.base.args["car_charging_power"]
    print("  ✓ Automatic configuration wires the Zappi power sensors for the flow diagram")


def test_automatic_config_eddi_only_leaves_car_charging_power_alone():
    """An Eddi is not a car charger, so it must not appear as car charging power."""
    component = _make_component()
    component.devices = {"E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)}
    component.automatic_config()
    assert "car_charging_power" not in component.base.args
    print("  ✓ Eddi-only site leaves car_charging_power unset")


def test_automatic_config_single_zappi_is_still_a_list():
    """A single Zappi still produces a list, so adding a second changes nothing else."""
    component = _make_component()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.automatic_config()
    assert component.base.args["car_charging_energy"] == ["sensor.predbat_myenergi_zappi_12345678_session_energy"]
    assert component.base.args["car_charging_planned"] == ["sensor.predbat_myenergi_zappi_12345678_plug_status"]
    assert "iboost_energy_today" not in component.base.args
    print("  ✓ Single Zappi auto-config")


def test_automatic_config_eddi_only():
    """A site with only an Eddi wires iboost_energy_today and leaves car_charging_energy untouched."""
    component = _make_component()
    component.devices = {"E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)}
    component.automatic_config()
    assert "car_charging_energy" not in component.base.args
    assert "car_charging_planned" not in component.base.args
    assert component.base.args["iboost_energy_today"] == "sensor.predbat_myenergi_eddi_87654321_session_energy"
    print("  ✓ Eddi-only site wires iboost_energy_today and skips car_charging_energy")


def test_automatic_config_uses_set_arg_auto():
    """An explicit apps.yaml value is reported via apps_yaml_override_warned, proving set_arg_auto (not set_arg) is used.

    MockBase has neither args_from_apps_yaml nor apps_yaml_override_warned, so
    set_arg_auto() silently degrades to plain set_arg() unless the test supplies
    them - meaning swapping set_arg_auto() for set_arg() in automatic_config()
    would leave every other auto-config test green. This test pins the call to
    set_arg_auto specifically.
    """
    component = _make_component()
    component.base.args_from_apps_yaml = {"car_charging_energy": ["sensor.explicit"]}
    component.base.apps_yaml_override_warned = set()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.automatic_config()
    assert "car_charging_energy" in component.base.apps_yaml_override_warned, component.base.apps_yaml_override_warned
    print("  ✓ Automatic configuration uses set_arg_auto, not set_arg")


def test_automatic_config_disabled():
    """With automatic off, nothing is wired even after a successful poll that reached the publish block."""
    component = _make_component(automatic=False)
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    run_async(component.run(0, True))
    # Proves the poll actually reached the block that would have called automatic_config(),
    # rather than an early return making the "nothing wired" assertion trivially true.
    assert component.devices, "poll must have reached the publish block"
    assert component._auto_configured is False
    assert "car_charging_energy" not in component.base.args
    print("  ✓ Automatic configuration respects the off switch")


def test_automatic_config_runs_once():
    """Auto-config runs after the first poll and is not repeated."""
    component = _make_component()
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    run_async(component.run(0, True))
    assert component._auto_configured is True
    # Proves the first run actually wired the value, not just set the flag
    assert component.base.args["car_charging_energy"] == ["sensor.predbat_myenergi_zappi_12345678_session_energy"]
    component.base.args["car_charging_energy"] = ["sensor.user_override"]
    run_async(component.run(60, False))
    assert component.base.args["car_charging_energy"] == ["sensor.user_override"], "Auto-config must not run twice"
    print("  ✓ Automatic configuration runs exactly once")


def test_controls_queue_rather_than_call():
    """Switch and number events queue for the run loop instead of calling inline."""
    component = _make_component()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    assert len(component.queued_events) == 1
    component.transport.send_boost.assert_not_called()

    component.transport.fetch_devices = AsyncMock(return_value=list(component.devices.values()))
    run_async(component.run(60, False))
    component.transport.send_boost.assert_called_once()
    assert component.queued_events == []
    print("  ✓ Control events queue for the run loop")


def test_boost_uses_number_entity_value():
    """The boost amount comes from the companion number entity."""
    component = _make_component()
    device = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    component.devices = {"Z12345678": device}
    component.transport.send_boost = AsyncMock(return_value=True)

    # number_event only queues, so drain the queue the way run() would
    run_async(component.number_event("number.predbat_myenergi_zappi_12345678_boost_energy", 25))
    handler, *event_args = component.queued_events.pop(0)
    run_async(handler(*event_args))

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))

    component.transport.send_boost.assert_called_once_with(device, 25)
    print("  ✓ Boost uses the number entity value")


def test_boost_refused_in_fast_mode():
    """A Zappi outside Eco or Eco+ is not boosted, and no API call is made."""
    component = _make_component()
    fast = normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, zmo=1), DEVICE_KIND_ZAPPI)
    assert fast.mode == "Fast"
    component.devices = {"Z12345678": fast}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    component.transport.send_boost.assert_not_called()
    print("  ✓ Boost refused outside Eco and Eco+")


def test_cancel_boost():
    """Turning the switch off cancels the boost."""
    component = _make_component()
    device = normalise_direct_device(dict(MOCK_DIRECT_EDDI, bsm=1, sta=4), DEVICE_KIND_EDDI)
    component.devices = {"E87654321": device}
    component.transport.cancel_boost = AsyncMock(return_value=True)

    run_async(component.switch_event_handler("switch.predbat_myenergi_eddi_87654321_boost", "turn_off"))
    component.transport.cancel_boost.assert_called_once_with(device)
    print("  ✓ Cancel boost")


def test_controls_disabled():
    """With enable_controls off, events are ignored entirely."""
    component = _make_component(enable_controls=False)
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    assert component.queued_events == []
    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    component.transport.send_boost.assert_not_called()
    print("  ✓ Controls respect enable_controls")


def test_control_for_unknown_entity_is_ignored():
    """An event for a device that is not known does nothing and does not raise.

    A known device is loaded first so device_for_entity() actually runs its comparison:
    with devices empty the loop body never executes and this passes for any lookup
    implementation at all, including a broken one.
    """
    component = _make_component()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.transport.send_boost = AsyncMock(return_value=True)
    assert run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_99999999_boost", "turn_on")) is False
    component.transport.send_boost.assert_not_called()
    print("  ✓ Unknown entity events are ignored")


def test_boost_eddi_skips_mode_check():
    """An Eddi boost is not subject to the Zappi-only Eco/Eco+ mode check and uses its own boost amount."""
    component = _make_component()
    device = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    component.devices = {"E87654321": device}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event_handler("switch.predbat_myenergi_eddi_87654321_boost", "turn_on"))
    component.transport.send_boost.assert_called_once_with(device, DEFAULT_EDDI_BOOST_MINUTES)
    print("  ✓ Eddi boost skips the Eco/Eco+ check and uses the default minutes")


def test_number_event_handler_clamps_amount():
    """The stored boost amount is clamped to the documented range for each device kind."""
    component = _make_component()
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    component.devices = {"Z12345678": zappi, "E87654321": eddi}

    run_async(component.number_event_handler("number.predbat_myenergi_zappi_12345678_boost_energy", 500))
    assert component.boost_amounts[zappi.device_id] == BOOST_ENERGY_MAX

    run_async(component.number_event_handler("number.predbat_myenergi_zappi_12345678_boost_energy", -5))
    assert component.boost_amounts[zappi.device_id] == BOOST_ENERGY_MIN

    run_async(component.number_event_handler("number.predbat_myenergi_eddi_87654321_boost_minutes", 999))
    assert component.boost_amounts[eddi.device_id] == BOOST_MINUTES_MAX

    run_async(component.number_event_handler("number.predbat_myenergi_eddi_87654321_boost_minutes", -10))
    assert component.boost_amounts[eddi.device_id] == BOOST_MINUTES_MIN
    print("  ✓ number_event_handler clamps to the documented range for both device kinds")


def test_number_event_handler_unknown_entity_is_ignored():
    """A number event for an unknown device does nothing and does not raise.

    As with the switch case, a known device has to be present or the lookup loop never
    runs and the assertion holds regardless of how device_for_entity() is written.
    """
    component = _make_component()
    component.devices = {"E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)}
    run_async(component.number_event_handler("number.predbat_myenergi_zappi_99999999_boost_energy", 25))
    assert component.boost_amounts == {}
    print("  ✓ Unknown entity number events are ignored")


def test_number_event_disabled():
    """With enable_controls off, number_event does not queue."""
    component = _make_component(enable_controls=False)
    run_async(component.number_event("number.predbat_myenergi_zappi_12345678_boost_energy", 25))
    assert component.queued_events == []
    print("  ✓ number_event respects enable_controls")


def test_switch_event_handler_ignores_non_boost_and_unknown_service():
    """A non-boost switch entity, or an unrecognised service on the boost switch, makes no API call."""
    component = _make_component()
    device = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    component.devices = {"Z12345678": device}
    component.transport.send_boost = AsyncMock(return_value=True)
    component.transport.cancel_boost = AsyncMock(return_value=True)

    # Not a boost entity at all
    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_something_else", "turn_on"))
    component.transport.send_boost.assert_not_called()

    # The boost switch, but a service that is neither turn_on nor turn_off
    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "toggle"))
    component.transport.send_boost.assert_not_called()
    component.transport.cancel_boost.assert_not_called()
    print("  ✓ Non-boost entities and unrecognised services make no API call")


def test_direct_record_api_call_reasons():
    """record_api_call receives the documented reason vocabulary for every direct failure branch.

    The cloud transport has had this table since review; the direct transport - which is
    the default, and therefore the one nearly every user runs - only had one-off tests for
    two of its six branches, leaving auth_error, server_error and client_error unasserted.
    The first queued response resolves the ASN successfully and records no reason, so the
    reasons collected here belong solely to the active-server request under test.
    """
    scenarios = [
        (_direct_response({}, status=401), "auth_error"),
        (_direct_response({}, status=500), "server_error"),
        (_direct_response({}, status=403), "client_error"),
        (asyncio.TimeoutError(), "connection_error"),
        (aiohttp.ClientConnectionError(), "connection_error"),
        (_direct_response(json_error=ValueError("bad json")), "decode_error"),
    ]
    for queued, expected_reason in scenarios:
        session, _calls = _direct_session([_direct_response([]), queued])
        transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
        with patch("aiohttp.ClientSession", return_value=session), patch("myenergi.record_api_call") as mock_record:
            try:
                run_async(transport.fetch_devices())
                raise AssertionError("Expected a MyEnergiError for reason={}".format(expected_reason))
            except (MyEnergiAuthError, MyEnergiApiError):
                pass
        reasons = [call.kwargs.get("reason") for call in mock_record.call_args_list if call.kwargs.get("reason")]
        assert reasons == [expected_reason], (expected_reason, reasons)
    print("  ✓ Direct transport records the documented reason for every failure branch")


def test_direct_transport_requires_aiohttp_digest_support():
    """An aiohttp too old for digest auth is reported as an actionable message, not an AttributeError.

    requirements.txt floors aiohttp at 3.12 for DigestAuthMiddleware and
    ClientSession(middlewares=...), but a hand-managed install can still be older, in
    which case _new_session() used to die with a bare AttributeError escaping as a raw
    traceback plus a startup stall, saying nothing about what to do.
    """
    with patch("myenergi.digest_auth_available", return_value=False):
        transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
        try:
            run_async(transport.connect())
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError as exc:
            assert "aiohttp 3.12" in str(exc), exc

        # The component refuses to build the transport at all, so the message is logged
        # once at startup rather than once per poll
        component = _make_component()
        assert component.transport is None
    print("  ✓ An aiohttp without digest support is reported with an actionable message")


def test_direct_boost_rejection_is_an_error():
    """A /cgi-* command answering 200 with a non-zero status is a refusal, not a success.

    The command endpoints always answer 200; the outcome is in the body. Without this the
    component logged "boosting eddi-87654321 by 60" for a tank already at temperature, and
    the switch quietly flipped back on the next poll with nothing to explain it.
    """
    eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, _calls = _direct_session([_direct_response({"status": -14})])
    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.send_boost(eddi, 60))
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError as exc:
            assert "-14" in str(exc), exc

    session, _calls = _direct_session([_direct_response({"status": 1})])
    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.cancel_boost(eddi))
            raise AssertionError("Expected MyEnergiApiError")
        except MyEnergiApiError:
            pass
    print("  ✓ A non-zero command status raises rather than reporting success")


def test_direct_boost_without_a_status_body_is_success():
    """A command endpoint that answers with no status at all is treated as success.

    Not every /cgi-* endpoint returns a status field, so the check has to be conservative
    or a working boost would be reported as a failure.
    """
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    for body in (None, {}, {"status": 0}, {"status": "0"}, ["unexpected"]):
        session, _calls = _direct_session([_direct_response(body)])
        with patch("aiohttp.ClientSession", return_value=session):
            assert run_async(transport.send_boost(zappi, 10)) is True, body
    print("  ✓ A command response with no failure status is treated as success")


def test_direct_boost_amount_and_time_formatting():
    """Boost amounts round rather than truncate, and a target time is zero padded to HHMM."""
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0}) for _ in range(3)])
    with patch("aiohttp.ClientSession", return_value=session):
        # 9.8 kWh must not be sent as 9
        run_async(transport.send_boost(zappi, 9.8))
        # "7:30" must not be sent as "730", which myenergi reads as a different time
        run_async(transport.send_boost(zappi, 15, target_time="7:30"))
        run_async(transport.send_boost(eddi, 44.6))

    assert calls[0].endswith("/cgi-zappi-mode-Z12345678-0-10-10-0000"), calls[0]
    assert calls[1].endswith("/cgi-zappi-mode-Z12345678-0-11-15-0730"), calls[1]
    assert calls[2].endswith("/cgi-eddi-boost-E87654321-10-1-45"), calls[2]
    print("  ✓ Boost amounts round and target times are zero padded")


def test_direct_boost_rejects_unsupported_kinds():
    """A device that is neither a Zappi nor an Eddi is refused rather than sent an Eddi command.

    The kind check used to be an else, so any future kind reaching send_boost would have
    been issued a /cgi-eddi-boost against a device that is not an Eddi.
    """
    harvi = _make_device(kind="harvi", serial="11112222")
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0})])
    with patch("aiohttp.ClientSession", return_value=session):
        for call in (transport.send_boost(harvi, 10), transport.cancel_boost(harvi)):
            try:
                run_async(call)
                raise AssertionError("Expected MyEnergiApiError")
            except MyEnergiApiError as exc:
                assert "harvi" in str(exc), exc
    assert calls == [], "No request may be made for an unsupported device kind"
    print("  ✓ Boost and cancel refuse unsupported device kinds")


def test_cloud_boost_rejects_unsupported_kinds():
    """The cloud transport refuses an unknown kind rather than defaulting it to the Eddi body.

    Sending an Eddi body (durationMinutes) to a non-Eddi is a documented 400, so the
    unsupported case must be caught locally - matching the direct transport above.
    """
    harvi = _make_device(kind="harvi", serial="11112222", device_id="HA11112222")
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    session, calls = _cloud_session([_cloud_response({"commandId": "c1"})])
    with patch("aiohttp.ClientSession", return_value=session):
        for call in (transport.send_boost(harvi, 10), transport.cancel_boost(harvi)):
            try:
                run_async(call)
                raise AssertionError("Expected MyEnergiApiError")
            except MyEnergiApiError as exc:
                assert "harvi" in str(exc), exc
    assert calls == [], "No request may be made for an unsupported device kind"
    print("  ✓ Cloud boost and cancel refuse unsupported device kinds")


def test_cloud_device_list_cache_expires_on_the_clock():
    """The cached device list is refetched once it is older than CLOUD_DEVICE_LIST_MAX_AGE, and not before.

    The cache age was tracked with a counter that nothing ever incremented, so GET /devices
    ran exactly once per process and a device added, removed or renamed in the myenergi app
    stayed invisible until Predbat restarted. Time is patched rather than slept on.
    """
    session, calls = _cloud_session(
        [
            _cloud_response(MOCK_CLOUD_DEVICES),
            _cloud_response(MOCK_CLOUD_ZAPPI_STATUS),
            _cloud_response(MOCK_CLOUD_EDDI_STATUS),
            _cloud_response(MOCK_CLOUD_ZAPPI_STATUS),
            _cloud_response(MOCK_CLOUD_EDDI_STATUS),
            _cloud_response(MOCK_CLOUD_DEVICES),
            _cloud_response(MOCK_CLOUD_ZAPPI_STATUS),
            _cloud_response(MOCK_CLOUD_EDDI_STATUS),
        ]
    )
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")
    clock = [1000.0]

    with patch("aiohttp.ClientSession", return_value=session), patch("myenergi.time.time", side_effect=lambda: clock[0]):
        run_async(transport.fetch_devices())
        clock[0] += CLOUD_DEVICE_LIST_MAX_AGE - 1
        run_async(transport.fetch_devices())
        list_calls = [call for call in calls if call[1].endswith("/devices")]
        assert len(list_calls) == 1, "A fresh cache must not be refetched: {}".format(list_calls)

        clock[0] += 2
        run_async(transport.fetch_devices())

    list_calls = [call for call in calls if call[1].endswith("/devices")]
    assert len(list_calls) == 2, "A stale cache must be refetched: {}".format(list_calls)
    print("  ✓ The cloud device list cache expires on the wall clock")


def test_cloud_one_bad_device_does_not_lose_the_others():
    """A status call failing for one device costs that device only, not the whole poll.

    Aborting the poll would blank every published entity over a single device being
    briefly unreachable, and a device answering with an empty body would vanish silently.
    """
    session, _calls = _cloud_session(
        [
            _cloud_response(MOCK_CLOUD_DEVICES),
            _cloud_response({"message": "boom"}, status=500),
            _cloud_response(MOCK_CLOUD_EDDI_STATUS),
        ]
    )
    messages = []
    transport = MyEnergiCloudTransport(messages.append, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        devices = run_async(transport.fetch_devices())

    assert [device.kind for device in devices] == [DEVICE_KIND_EDDI], devices
    assert any("ZA12345678" in message for message in messages), messages

    # An empty status body is reported too, rather than dropping the device silently
    session, _calls = _cloud_session([_cloud_response(MOCK_CLOUD_ZAPPI_STATUS), _cloud_response({})])
    messages = []
    transport.log = messages.append
    with patch("aiohttp.ClientSession", return_value=session):
        devices = run_async(transport.fetch_devices())
    assert len(devices) == 1, devices
    assert any("no status returned" in message for message in messages), messages
    print("  ✓ One failing device does not cost the whole cloud poll")


def test_cloud_every_device_failing_is_a_failed_poll():
    """Reading none of the known devices raises, rather than passing as an empty site.

    Returning [] would let run() keep the previous readings and still stamp success, so a
    site whose every device was erroring would report healthy for as long as it kept
    failing. An account with genuinely no Zappi or Eddi is still not an error.
    """
    session, _calls = _cloud_session(
        [
            _cloud_response(MOCK_CLOUD_DEVICES),
            _cloud_response({"message": "boom"}, status=500),
            _cloud_response({"message": "boom"}, status=500),
        ]
    )
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")
    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiApiError when every device fails")
        except MyEnergiApiError as exc:
            assert "2 skipped" in str(exc), exc

    # A site with no supported devices at all reports no devices, not a failure
    session, _calls = _cloud_session([_cloud_response({"sites": [{"siteId": "s1", "devices": []}]})])
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")
    with patch("aiohttp.ClientSession", return_value=session):
        assert run_async(transport.fetch_devices()) == []
    print("  ✓ A poll that reads no device at all is a failed poll, an empty site is not")


def test_failed_poll_does_not_stamp_success():
    """A failed poll leaves last_success_timestamp alone so repeated failures go unhealthy.

    components.py fails a component after 60 minutes without a success, which is the
    mechanism that surfaces a persistently broken account to the user - stamping on a
    cycle that read nothing would keep it looking healthy indefinitely.
    """
    component = _make_component()
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    assert run_async(component.run(0, True)) is True
    stamped = component.last_success_timestamp
    assert stamped is not None

    component.transport.fetch_devices = AsyncMock(side_effect=MyEnergiApiError("no myenergi device could be read this poll, 2 skipped"))
    assert run_async(component.run(60, False)) is False
    assert component.last_success_timestamp == stamped, "A failed poll must not advance the success timestamp"
    print("  ✓ A failed poll leaves the success timestamp alone")


def test_cloud_auth_error_still_aborts_the_poll():
    """A 401 on a device status still propagates, so the reactive token refresh can see it.

    The per-device tolerance above must catch MyEnergiApiError only: swallowing
    MyEnergiAuthError would strand a revoked token, since run() would never be told.
    """
    session, _calls = _cloud_session([_cloud_response(MOCK_CLOUD_DEVICES), _cloud_response({}, status=401)])
    transport = MyEnergiCloudTransport(print, lambda: "stale-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError:
            pass
    print("  ✓ A 401 on a device status still aborts the cloud poll")


def _make_device(kind="zappi", serial="12345678", **overrides):
    """Build a MyEnergiDevice directly, for kinds no transport would ever normalise."""
    fields = {
        "device_id": "{}{}".format(kind[0].upper(), serial),
        "kind": kind,
        "serial": serial,
        "name": "{}-{}".format(kind, serial),
        "online": True,
        "status": "Unknown",
        "mode": "Normal",
        "plug_status": "",
        "power_w": 0.0,
        "grid_power_w": 0.0,
        "generation_w": 0.0,
        "voltage": 0.0,
        "session_energy_kwh": 0.0,
        "boost_active": False,
        "boost_remaining_mins": 0,
        "temp_1": None,
        "temp_2": None,
    }
    fields.update(overrides)
    return MyEnergiDevice(**fields)


def test_automatic_config_ignores_unsupported_kinds():
    """A device that is neither a Zappi nor an Eddi is never wired into any Predbat input.

    The Eddi branch is an explicit kind test rather than a bare else, so a Harvi (or any
    kind a future release adds) cannot end up published as the house's hot water sensor.
    Reverting that guard leaves every other auto-config test green, so it is pinned here.
    """
    component = _make_component()
    component.devices = {"H11112222": _make_device(kind="harvi", serial="11112222")}
    component.automatic_config()
    assert "iboost_energy_today" not in component.base.args, component.base.args
    assert "car_charging_energy" not in component.base.args, component.base.args
    assert "car_charging_planned" not in component.base.args, component.base.args
    print("  ✓ Unsupported device kinds are never auto-configured")


def test_device_for_entity_requires_a_whole_prefix_match():
    """The entity lookup anchors on a whole prefix, so one serial cannot claim another's entities.

    An unanchored substring match let a device with serial 1234 answer for every entity
    belonging to serial 12345678, silently boosting the wrong charger.
    """
    component = _make_component()
    short = _make_device(serial="1234")
    long_serial = _make_device(serial="12345678", device_id="Z12345678")
    component.devices = {"Z1234": short, "Z12345678": long_serial}

    assert component.device_for_entity("switch.predbat_myenergi_zappi_12345678_boost") is long_serial
    assert component.device_for_entity("switch.predbat_myenergi_zappi_1234_boost") is short
    assert component.device_for_entity("switch.predbat_myenergi_zappi_9999_boost") is None
    print("  ✓ Entity lookup anchors on the whole device prefix")


def test_number_event_handler_ignores_other_number_entities():
    """A number entity that is not a boost amount is left alone even though its device is known.

    number_event_handler branches on device.kind, so without a suffix guard any future
    number.{prefix}_* entity would be clamped into boost_amounts as a boost amount.
    """
    component = _make_component()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    run_async(component.number_event_handler("number.predbat_myenergi_zappi_12345678_charge_limit", 25))
    assert component.boost_amounts == {}, component.boost_amounts
    print("  ✓ Number events for non-boost entities are ignored")


def test_component_poll_seconds_gate_skips_cycles():
    """poll_seconds actually gates the poll, and a skipped cycle stamps no success timestamp.

    Deleting the modulo gate left the whole suite green, which made myenergi_poll_seconds
    a setting with no observable effect.
    """
    component = _make_component(poll_seconds=300)
    devices = [normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)]
    component.transport.fetch_devices = AsyncMock(return_value=devices)

    assert run_async(component.run(0, True)) is True
    component.transport.fetch_devices.assert_awaited_once()
    first_stamp = component.last_success_timestamp
    assert first_stamp is not None

    component.transport.fetch_devices.reset_mock()
    assert run_async(component.run(60, False)) is True
    component.transport.fetch_devices.assert_not_awaited()
    assert component.last_success_timestamp == first_stamp, "A skipped cycle must not stamp a poll it never made"

    assert run_async(component.run(300, False)) is True
    component.transport.fetch_devices.assert_awaited_once()
    assert component.last_success_timestamp != first_stamp
    print("  ✓ poll_seconds gates the poll and only a real poll stamps success")


def test_component_reactive_oauth_refresh_retries_the_poll():
    """A 401 mid-poll triggers one reactive token refresh and a single retry.

    The proactive check only covers a token that has reached its stated expiry; a token
    revoked before then wedged the component until Predbat restarted.
    """
    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key="jwt-token")
    component.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    component.handle_oauth_401 = AsyncMock(return_value=True)
    devices = [normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)]
    component.transport.fetch_devices = AsyncMock(side_effect=[MyEnergiAuthError("401"), devices])

    assert run_async(component.run(0, True)) is True
    component.handle_oauth_401.assert_awaited_once()
    assert component.transport.fetch_devices.await_count == 2
    assert "Z12345678" in component.devices
    print("  ✓ A 401 mid-poll refreshes the token and retries once")


def test_component_reactive_oauth_refresh_gives_up_after_one_retry():
    """A refresh that fails, or a retry that fails again, ends the cycle instead of looping."""
    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key="jwt-token")
    component.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    component.handle_oauth_401 = AsyncMock(return_value=False)
    component.transport.fetch_devices = AsyncMock(side_effect=MyEnergiAuthError("401"))
    assert run_async(component.run(0, True)) is False
    assert component.transport.fetch_devices.await_count == 1

    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key="jwt-token")
    component.check_and_refresh_oauth_token = AsyncMock(return_value=True)
    component.handle_oauth_401 = AsyncMock(return_value=True)
    component.transport.fetch_devices = AsyncMock(side_effect=[MyEnergiAuthError("401"), MyEnergiAuthError("401 again")])
    assert run_async(component.run(0, True)) is False
    assert component.transport.fetch_devices.await_count == 2
    print("  ✓ The reactive refresh retries exactly once and then gives up")


def test_component_direct_auth_error_never_refreshes():
    """A digest credential failure is not an OAuth problem and must not attempt a refresh."""
    component = _make_component()
    component.handle_oauth_401 = AsyncMock(return_value=True)
    component.transport.fetch_devices = AsyncMock(side_effect=MyEnergiAuthError("bad key"))

    assert run_async(component.run(0, True)) is False
    component.handle_oauth_401.assert_not_awaited()
    assert component.transport.fetch_devices.await_count == 1
    print("  ✓ A direct-transport auth failure never attempts an OAuth refresh")


def test_switch_event_handler_reports_the_transport_result():
    """The handler returns whether the boost was issued and accepted, rather than always claiming success."""
    component = _make_component()
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    fast = normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, sno=99999999, zmo=1), DEVICE_KIND_ZAPPI)
    component.devices = {"Z12345678": zappi, "Z99999999": fast}
    component.transport.send_boost = AsyncMock(return_value=True)
    component.transport.cancel_boost = AsyncMock(return_value=True)

    assert run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on")) is True
    assert run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_off")) is True
    # Refused before the call, because the Zappi is in Fast mode
    assert run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_99999999_boost", "turn_on")) is False
    assert run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "toggle")) is False
    assert run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_something", "turn_on")) is False
    print("  ✓ The boost switch handler reports what actually happened")


def test_queued_boost_rejection_is_logged_as_a_failure():
    """A boost myenergi refuses is logged as a control failure by the run loop, not as a success."""
    component = _make_component()
    messages = []
    component.log = messages.append
    device = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    component.devices = {"E87654321": device}
    component.transport.send_boost = AsyncMock(side_effect=MyEnergiApiError("myenergi refused /cgi-eddi-boost-E87654321-10-1-60 with status -14"))
    component.transport.fetch_devices = AsyncMock(return_value=[device])

    run_async(component.switch_event("switch.predbat_myenergi_eddi_87654321_boost", "turn_on"))
    assert run_async(component.run(60, False)) is True

    assert any("control failed" in message and "status -14" in message for message in messages), messages
    print("  ✓ A refused boost is logged as a control failure")


def test_templates_accept_the_connected_zappi_plug_states():
    """Every myenergi-aware apps.yaml template accepts the plug states that mean the car is connected.

    automatic_config() wires the Zappi plug status sensor into car_charging_planned, so a
    published state missing from car_charging_planned_response reads as "not planned to
    charge" - which is exactly how "EV ready to charge" (pilot states C1 and D1) silently
    disabled charge planning for a plugged-in car waiting to start.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    templates_dir = os.path.join(repo_root, "templates")
    connected_states = sorted({value.lower() for key, value in ZAPPI_PLUG_STATES.items() if key != "A" and key != "F"})
    assert "ev ready to charge" in connected_states, connected_states

    checked = 0
    for name in sorted(os.listdir(templates_dir)):
        if not name.endswith(".yaml"):
            continue
        with open(os.path.join(templates_dir, name), encoding="utf-8") as handle:
            text = handle.read()
        if "car_charging_planned_response" not in text or "'ev connected'" not in text:
            continue
        checked += 1
        for state in connected_states:
            assert "'{}'".format(state) in text, "{} does not accept the '{}' plug state".format(name, state)
    assert checked > 10, "Expected the templates to be found and checked, only saw {}".format(checked)
    print("  ✓ {} templates accept every connected Zappi plug state".format(checked))


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
    test_direct_missing_header_on_200_is_auth_error()
    test_direct_401_without_header_is_a_credential_error()
    test_direct_503_without_header_is_api_error()
    test_direct_boost_urls()
    test_direct_smart_boost_url()
    test_direct_set_mode()
    test_direct_set_mode_rejects_bad_input()
    test_direct_401_is_auth_error()
    test_direct_non_200_sets_needs_asn_refresh()
    test_direct_timeout_sets_needs_asn_refresh()
    test_direct_asn_migration_follows_new_host()
    test_direct_resolve_asn_non_200_is_api_error()
    test_direct_resolve_asn_timeout_is_api_error()
    test_cloud_fetch_devices()
    test_cloud_boost_bodies()
    test_cloud_set_mode()
    test_cloud_sets_bearer_header()
    test_cloud_unauthorised_raises_auth_error()
    test_cloud_non_200_is_api_error()
    test_cloud_timeout_is_api_error()
    test_cloud_non_json_response_is_api_error()
    test_cloud_non_dict_payload_is_api_error()
    test_cloud_record_api_call_reasons()
    test_direct_client_error_reason_is_connection_error()
    test_direct_non_json_response_is_api_error()
    test_direct_record_api_call_reasons()
    test_direct_transport_requires_aiohttp_digest_support()
    test_direct_boost_rejection_is_an_error()
    test_direct_boost_without_a_status_body_is_success()
    test_direct_boost_amount_and_time_formatting()
    test_direct_boost_rejects_unsupported_kinds()
    test_cloud_boost_rejects_unsupported_kinds()
    test_cloud_every_device_failing_is_a_failed_poll()
    test_failed_poll_does_not_stamp_success()
    test_cloud_device_list_cache_expires_on_the_clock()
    test_cloud_one_bad_device_does_not_lose_the_others()
    test_cloud_auth_error_still_aborts_the_poll()
    test_control_window_parsing()
    test_control_windows_across_new_year()
    test_control_windows_are_per_car()
    test_control_windows_tolerate_a_bad_entry_and_a_missing_plan()
    test_control_windows_cross_the_year_boundary()
    test_control_charge_sets_fast_inside_and_stopped_outside()
    test_control_charge_maps_each_zappi_to_its_own_car()
    test_control_charge_is_edge_triggered_but_corrects_drift()
    test_control_charge_ignores_eddis()
    test_control_charge_does_nothing_before_a_plan_exists()
    test_control_gating_refuses_with_a_reason()
    test_control_releases_to_the_saved_mode()
    test_control_releases_to_eco_plus_when_nothing_was_saved()
    test_control_stops_and_resumes_on_read_only()
    test_control_switch_is_published_and_toggles_control()
    test_control_switch_is_not_published_when_control_cannot_run()
    test_control_switch_publishes_its_restored_state_on_the_first_cycle()
    test_control_switch_is_not_published_without_the_feature()
    test_run_enables_control_and_drives_the_zappi()
    test_run_does_not_control_when_the_feature_is_off()
    test_control_switch_state_survives_a_restart()
    test_control_failure_does_not_break_the_poll()
    test_component_selects_transport()
    test_component_publishes_entities()
    test_component_retains_last_good_reading()
    test_component_empty_device_list_does_not_wipe_devices()
    test_component_poll_seconds_rounding()
    test_component_oauth_refresh_failure_stops_the_poll()
    test_component_poll_seconds_gate_skips_cycles()
    test_component_reactive_oauth_refresh_retries_the_poll()
    test_component_reactive_oauth_refresh_gives_up_after_one_retry()
    test_component_direct_auth_error_never_refreshes()
    test_component_registration()
    test_automatic_config()
    test_automatic_config_wires_car_charging_power()
    test_automatic_config_eddi_only_leaves_car_charging_power_alone()
    test_automatic_config_single_zappi_is_still_a_list()
    test_automatic_config_eddi_only()
    test_automatic_config_uses_set_arg_auto()
    test_automatic_config_disabled()
    test_automatic_config_runs_once()
    test_automatic_config_ignores_unsupported_kinds()
    test_templates_accept_the_connected_zappi_plug_states()
    test_controls_queue_rather_than_call()
    test_boost_uses_number_entity_value()
    test_boost_refused_in_fast_mode()
    test_cancel_boost()
    test_controls_disabled()
    test_control_for_unknown_entity_is_ignored()
    test_boost_eddi_skips_mode_check()
    test_number_event_handler_clamps_amount()
    test_number_event_handler_unknown_entity_is_ignored()
    test_number_event_disabled()
    test_switch_event_handler_ignores_non_boost_and_unknown_service()
    test_switch_event_handler_reports_the_transport_result()
    test_queued_boost_rejection_is_logged_as_a_failure()
    test_device_for_entity_requires_a_whole_prefix_match()
    test_number_event_handler_ignores_other_number_entities()

    print("=" * 70)
    return False
