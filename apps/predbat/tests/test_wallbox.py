# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the Wallbox EV charger integration
"""

import asyncio
import datetime
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytz

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async
from mock_base import MockBase

from wallbox import (
    CHARGING_STATUS_IDS,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    PAUSED_STATUS_IDS,
    WALLBOX_API_URL,
    WALLBOX_AUTH_URL,
    WALLBOX_STATUS,
    WallboxAPI,
    WallboxApiError,
    WallboxAuthError,
    WallboxCharger,
    WallboxPermissionError,
    WallboxRateLimitError,
    WallboxTransport,
    basic_auth_header,
    normalise_charger,
)


def _response(json_data=None, status=200, json_error=None):
    """Build a mock aiohttp response usable as an async context manager."""
    response = MagicMock()
    response.status = status
    if json_error is not None:
        response.json = AsyncMock(side_effect=json_error)
    else:
        response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _session(responses):
    """Build a mock aiohttp session whose request() returns the next queued response.

    A queued exception instance is raised instead. Returns (session, calls), where calls
    records the method, URL, headers and JSON body of every request in order.
    """
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        """Record the request, then return or raise the next queued item."""
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}, "json": kwargs.get("json")})
        item = queue.pop(0) if queue else _response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def _auth_payload(token="jwt-1", refresh="refresh-1", life=3600, refresh_life=86400):
    """Build a sign in response whose TTLs are absolute epoch milliseconds, as Wallbox sends them."""
    now = time.time()
    return {"data": {"attributes": {"token": token, "refresh_token": refresh, "ttl": int((now + life) * 1000), "refresh_token_ttl": int((now + refresh_life) * 1000)}}}


MOCK_GROUPS = {"result": {"groups": [{"chargers": [{"id": 202}, {"id": 101}]}, {"chargers": []}]}}

# Status payloads in the shape of the Home Assistant wallbox integration's own test
# fixture (tests/components/wallbox/const.py). These stand in for payloads captured from a
# real charger with `python3 wallbox.py --raw`, and should be replaced by such captures.
MOCK_STATUS_CHARGING = {
    "charging_power": 7.2,
    "status_id": 193,
    "max_available_power": 25.0,
    "charging_speed": 32,
    "added_range": 150,
    "added_energy": 44.697,
    "name": "Garage",
    "config_data": {
        "max_charging_current": 24,
        "energy_price": 0.4,
        "locked": False,
        "serial_number": "900001",
        "part_number": "PLP1-0-2-4-9-002-E",
        "software": {"currentVersion": "5.5.10"},
        "currency": {"code": "EUR/kWh"},
        "icp_max_current": 20,
        "plan": {"features": ["POWER_BOOST"]},
        "ecosmart": {"enabled": False, "mode": 0},
    },
}
MOCK_STATUS_READY = dict(MOCK_STATUS_CHARGING, charging_power=0, status_id=161, charging_speed=0, added_range=0, added_energy=0)
MOCK_STATUS_PAUSED = dict(MOCK_STATUS_CHARGING, charging_power=0, status_id=178, charging_speed=0)


def test_basic_auth_header():
    """The Basic header is UTF-8, so a colon or accented character in the password survives."""
    assert basic_auth_header("user@example.com", "secret") == "Basic dXNlckBleGFtcGxlLmNvbTpzZWNyZXQ="
    # base64 of "a:pä:ss" encoded as UTF-8
    assert basic_auth_header("a", "pä:ss") == "Basic YTpww6Q6c3M="
    print("  ✓ Basic auth header is built as UTF-8")


def test_transport_signs_in_then_lists_chargers():
    """The first call signs in with Basic auth and the Partner header, then uses the bearer token."""
    session, calls = _session([_response(_auth_payload()), _response(MOCK_GROUPS)])
    transport = WallboxTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        chargers = run_async(transport.list_chargers())

    assert chargers == [202, 101], chargers
    assert calls[0]["url"] == WALLBOX_AUTH_URL + "users/signin"
    assert calls[0]["headers"]["Authorization"] == basic_auth_header("user@example.com", "secret")
    assert calls[0]["headers"]["Partner"] == "wallbox"
    assert calls[1]["url"] == WALLBOX_API_URL + "v3/chargers/groups"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-1"
    print("  ✓ Transport signs in then lists chargers")


def test_transport_reuses_a_valid_token():
    """A token with life left is reused: two reads make one sign in."""
    session, calls = _session([_response(_auth_payload()), _response({"status_id": 193}), _response({"status_id": 161})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        first = run_async(transport.get_status(101))
        second = run_async(transport.get_status(101))

    assert first == {"status_id": 193} and second == {"status_id": 161}
    assert [call["url"] for call in calls] == [WALLBOX_AUTH_URL + "users/signin", WALLBOX_API_URL + "chargers/status/101", WALLBOX_API_URL + "chargers/status/101"]
    print("  ✓ A valid token is reused")


def test_transport_refreshes_an_expired_token():
    """An expired token with a live refresh token is refreshed with a bearer call, not a sign in."""
    session, calls = _session([_response(_auth_payload(token="jwt-2")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() - 10
    transport.refresh_token = "refresh-1"
    transport.refresh_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        run_async(transport.get_status(101))

    assert calls[0]["url"] == WALLBOX_AUTH_URL + "users/refresh-token"
    assert calls[0]["headers"]["Authorization"] == "Bearer refresh-1"
    assert calls[0]["headers"]["Partner"] == "wallbox"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-2"
    print("  ✓ An expired token is refreshed")


def test_transport_signs_in_when_the_refresh_is_rejected():
    """A 401 on refresh means the refresh token was revoked: fall back to a full sign in."""
    session, calls = _session([_response({}, status=401), _response(_auth_payload(token="jwt-3")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() - 10
    transport.refresh_token = "refresh-1"
    transport.refresh_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        run_async(transport.get_status(101))

    assert [call["url"] for call in calls] == [WALLBOX_AUTH_URL + "users/refresh-token", WALLBOX_AUTH_URL + "users/signin", WALLBOX_API_URL + "chargers/status/101"]
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-3"
    print("  ✓ A rejected refresh falls back to sign in")


def test_transport_bad_credentials():
    """A 403 on sign in is an auth error, recorded with reason auth_error."""
    session, _ = _session([_response({}, status=403)])
    transport = WallboxTransport(print, "user@example.com", "wrong")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call") as mock_record:
        try:
            run_async(transport.list_chargers())
            raise AssertionError("Expected WallboxAuthError")
        except WallboxAuthError:
            pass
    assert mock_record.call_args.kwargs.get("reason") == "auth_error"
    print("  ✓ Bad credentials raise WallboxAuthError")


def test_transport_retries_once_after_a_revoked_token():
    """A 401 on a data call drops the token, signs in again and retries the call once."""
    session, calls = _session([_response({}, status=401), _response(_auth_payload(token="jwt-9")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        status = run_async(transport.get_status(101))

    assert status == {"status_id": 193}
    assert calls[1]["url"] == WALLBOX_AUTH_URL + "users/signin"
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-9"
    print("  ✓ A revoked token is replaced and the call retried once")


def test_transport_rate_limit_and_failures():
    """429 is its own error; a 500, a timeout, a dropped connection and a non-JSON body are API errors."""
    cases = [
        (_response({}, status=429), WallboxRateLimitError, "rate_limit"),
        (_response({}, status=500), WallboxApiError, "server_error"),
        (_response({}, status=404), WallboxApiError, "client_error"),
        (asyncio.TimeoutError(), WallboxApiError, "connection_error"),
        (aiohttp.ClientError("boom"), WallboxApiError, "connection_error"),
        (_response(json_error=ValueError("not json")), WallboxApiError, "decode_error"),
        (_response(["not", "a", "dict"]), WallboxApiError, "decode_error"),
    ]
    for item, expected, reason in cases:
        session, _ = _session([item])
        transport = WallboxTransport(print, "user@example.com", "secret")
        transport.token = "jwt-1"
        transport.token_expiry = time.time() + 3600
        with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call") as mock_record:
            try:
                run_async(transport.get_status(101))
                raise AssertionError("Expected {}".format(expected.__name__))
            except expected:
                pass
        assert mock_record.call_args.kwargs.get("reason") == reason, (expected, mock_record.call_args)
    print("  ✓ Rate limit and failures map to the right errors and metric reasons")


def _status(status_id=193, power=7.2, energy=4.5, locked=False, max_current=32, eco=None, name="Garage"):
    """Build a status payload in the shape Wallbox returns, for the fields Predbat reads."""
    config = {"max_charging_current": max_current, "locked": locked, "serial_number": "900001", "part_number": "PLP1-0-2-4-9-002-E", "software": {"currentVersion": "5.5.10"}}
    if eco is not None:
        config["ecosmart"] = eco
    return {"status_id": status_id, "charging_power": power, "added_energy": energy, "max_available_power": 32, "name": name, "config_data": config}


def test_normalise_charging():
    """A charging payload gives watts, session energy and the identity fields."""
    charger = normalise_charger(101, _status())
    assert charger.charger_id == "101"
    assert charger.name == "Garage" and charger.serial == "900001"
    assert charger.part_number == "PLP1-0-2-4-9-002-E" and charger.software_version == "5.5.10"
    assert charger.status == "Charging" and charger.status_id == 193
    assert charger.connected is True and charger.charging is True and charger.paused is False
    assert charger.power_w == 7200.0, charger.power_w
    assert charger.session_energy_kwh == 4.5
    assert charger.max_charging_current == 32 and charger.max_available_current == 32
    assert charger.locked is False and charger.eco_smart is None
    print("  ✓ A charging payload is normalised")


def test_normalise_status_table():
    """Every status code maps to the documented text, connected, charging and paused flags."""
    expected = {
        193: ("Charging", True, True, False), 194: ("Charging", True, True, False), 195: ("Charging", True, True, False),
        196: ("Discharging", True, False, False),
        178: ("Paused", True, False, True), 182: ("Paused", True, False, True),
        177: ("Scheduled", True, False, False), 179: ("Scheduled", True, False, False),
        164: ("Waiting", True, False, False),
        180: ("Waiting for car demand", True, False, False), 181: ("Waiting for car demand", True, False, False),
        183: ("Waiting in queue by Power Sharing", True, False, False), 184: ("Waiting in queue by Power Sharing", True, False, False),
        185: ("Waiting in queue by Power Boost", True, False, False), 186: ("Waiting in queue by Power Boost", True, False, False),
        187: ("Waiting MID failed", True, False, False), 188: ("Waiting MID safety margin exceeded", True, False, False),
        189: ("Waiting in queue by Eco-Smart", True, False, False),
        210: ("Locked, car connected", True, False, False),
        165: ("Locked", False, False, False), 209: ("Locked", False, False, False),
        161: ("Ready", False, False, False), 162: ("Ready", False, False, False),
        0: ("Disconnected", False, False, False), 163: ("Disconnected", False, False, False),
        166: ("Updating", False, False, False),
        14: ("Error", False, False, False), 15: ("Error", False, False, False),
    }
    assert set(expected) == set(WALLBOX_STATUS), set(expected) ^ set(WALLBOX_STATUS)
    for status_id, (text, connected, charging, paused) in expected.items():
        charger = normalise_charger(101, _status(status_id=status_id))
        assert (charger.status, charger.connected, charger.charging, charger.paused) == (text, connected, charging, paused), status_id
    unknown = normalise_charger(101, _status(status_id=999))
    assert (unknown.status, unknown.connected, unknown.charging, unknown.paused) == ("Unknown", False, False, False)
    print("  ✓ Every status code maps to the right state")


def test_normalise_eco_smart():
    """Eco-Smart is None when unsupported, off when disabled, otherwise the mode."""
    assert normalise_charger(101, _status()).eco_smart is None
    assert normalise_charger(101, _status(eco={"enabled": False, "mode": 0})).eco_smart == "off"
    assert normalise_charger(101, _status(eco={"enabled": True, "mode": 0})).eco_smart == "eco_mode"
    assert normalise_charger(101, _status(eco={"enabled": True, "mode": 1})).eco_smart == "full_solar"
    assert normalise_charger(101, _status(eco={"enabled": True})).eco_smart is None
    print("  ✓ Eco-Smart mode is normalised")


def test_normalise_handles_bad_values():
    """Null numbers, a missing config block and a non-dict payload give safe defaults, never an exception."""
    charger = normalise_charger(101, {"status_id": None, "charging_power": None, "added_energy": "junk", "config_data": None})
    assert charger.status == "Disconnected" and charger.connected is False
    assert charger.power_w == 0.0 and charger.session_energy_kwh == 0.0
    assert charger.max_charging_current == 0 and charger.max_available_current == 0
    assert charger.name == "Wallbox 101" and charger.serial == "" and charger.locked is False

    empty = normalise_charger(101, "not a dict")
    assert empty.status == "Disconnected" and empty.power_w == 0.0

    truthy_lock = normalise_charger(101, _status(locked=1))
    assert truthy_lock.locked is True
    print("  ✓ Bad values normalise to safe defaults")


def test_normalise_captured_payloads():
    """The payloads captured from a real charger normalise to the states they were captured in."""
    charging = normalise_charger(101, MOCK_STATUS_CHARGING)
    assert charging.charging is True and charging.power_w > 0
    ready = normalise_charger(101, MOCK_STATUS_READY)
    assert ready.connected is False and ready.power_w == 0.0
    paused = normalise_charger(101, MOCK_STATUS_PAUSED)
    assert paused.paused is True and paused.connected is True
    print("  ✓ Captured payloads normalise to their real states")


def _signed_in_transport():
    """A transport that already holds a valid token, so each test sees only the call it makes."""
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() + 3600
    return transport


def test_transport_control_requests():
    """Each control sends exactly the method, URL and body the Wallbox API expects."""
    eco_body = lambda enabled, mode: {"data": {"attributes": {"enabled": enabled, "mode": mode}, "type": "eco_smart"}}
    cases = [
        (lambda t: t.pause(101), "POST", "v3/chargers/101/remote-action", {"action": 2}),
        (lambda t: t.resume(101), "POST", "v3/chargers/101/remote-action", {"action": 1}),
        (lambda t: t.resume_schedule(101), "POST", "v3/chargers/101/remote-action", {"action": 9}),
        (lambda t: t.set_locked(101, True), "PUT", "v2/charger/101", {"locked": 1}),
        (lambda t: t.set_locked(101, False), "PUT", "v2/charger/101", {"locked": 0}),
        (lambda t: t.set_max_charging_current(101, 16), "PUT", "v2/charger/101", {"maxChargingCurrent": 16}),
        (lambda t: t.set_eco_smart(101, "off"), "PUT", "v4/chargers/101/eco-smart", eco_body(0, 0)),
        (lambda t: t.set_eco_smart(101, "eco_mode"), "PUT", "v4/chargers/101/eco-smart", eco_body(1, 0)),
        (lambda t: t.set_eco_smart(101, "full_solar"), "PUT", "v4/chargers/101/eco-smart", eco_body(1, 1)),
    ]
    for action, method, path, body in cases:
        session, calls = _session([_response({})])
        with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
            run_async(action(_signed_in_transport()))
        assert len(calls) == 1, calls
        assert (calls[0]["method"], calls[0]["url"], calls[0]["json"]) == (method, WALLBOX_API_URL + path, body), calls[0]
        assert calls[0]["headers"]["Content-Type"] == "application/json;charset=UTF-8"
    print("  ✓ Control calls send the right method, URL and body")


def test_transport_control_refused_for_rights():
    """A 403 on a write is a rights problem, not bad credentials, and is not retried behind a sign in."""
    session, calls = _session([_response({}, status=403)])
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        try:
            run_async(_signed_in_transport().set_locked(101, True))
            raise AssertionError("Expected WallboxPermissionError")
        except WallboxPermissionError:
            pass
    assert len(calls) == 1, "A rights refusal must not trigger a sign in and retry"
    print("  ✓ A 403 on a write raises WallboxPermissionError")


def test_transport_control_accepts_an_empty_body():
    """Eco-Smart answers with no JSON body; that is a success, not a decode error."""
    session, _ = _session([_response(json_error=ValueError("empty"))])
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        assert run_async(_signed_in_transport().set_eco_smart(101, "off")) == {}
    print("  ✓ A control call with an empty body succeeds")


def test_transport_rejects_an_unknown_eco_smart_mode():
    """An Eco-Smart mode that is not one of the three options never reaches the API."""
    session, calls = _session([])
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        try:
            run_async(_signed_in_transport().set_eco_smart(101, "turbo"))
            raise AssertionError("Expected WallboxApiError")
        except WallboxApiError:
            pass
    assert calls == []
    print("  ✓ An unknown Eco-Smart mode is rejected locally")


class _StubTransport:
    """In-memory transport: serves statuses by charger id and records every call."""

    def __init__(self, statuses=None):
        """Hold the statuses to serve; errors maps a call tuple to an exception to raise once."""
        self.statuses = statuses if statuses is not None else {101: _status()}
        self.calls = []
        self.errors = {}

    def _record(self, *call):
        """Record a call, raising the queued error for it if there is one."""
        self.calls.append(call)
        error = self.errors.pop(call, None) or self.errors.pop((call[0],), None)
        if error:
            raise error
        return {}

    async def list_chargers(self):
        """Return the ids of the statuses held."""
        self._record("list_chargers")
        return list(self.statuses)

    async def get_status(self, charger_id):
        """Return one charger's status payload."""
        self._record("get_status", charger_id)
        return self.statuses[charger_id]

    async def pause(self, charger_id):
        """Record a pause."""
        return self._record("pause", charger_id)

    async def resume(self, charger_id):
        """Record a resume."""
        return self._record("resume", charger_id)

    async def resume_schedule(self, charger_id):
        """Record a resume schedule."""
        return self._record("resume_schedule", charger_id)

    async def set_locked(self, charger_id, locked):
        """Record a lock change."""
        return self._record("set_locked", charger_id, locked)

    async def set_max_charging_current(self, charger_id, amps):
        """Record a current change."""
        return self._record("set_max_charging_current", charger_id, amps)

    async def set_eco_smart(self, charger_id, mode):
        """Record an Eco-Smart change."""
        return self._record("set_eco_smart", charger_id, mode)

    def count(self, name):
        """How many times a call of this name was made."""
        return len([call for call in self.calls if call[0] == name])


def _make_component(statuses=None, **overrides):
    """Build a WallboxAPI against MockBase with a stub transport and a captured log."""
    args = {"username": "user@example.com", "password": "secret", "automatic": True, "wallbox_control": False, "poll_seconds": 120}
    args.update(overrides)
    component = WallboxAPI(MockBase(), **args)
    component.transport = _StubTransport(statuses)
    component.log_messages = []
    component.log = component.log_messages.append
    return component


def _logged(component, text):
    """Was a message containing this text logged."""
    return any(text in message for message in component.log_messages)


def test_component_registration():
    """The component is registered with matching config keys and event filter."""
    import inspect

    from components import COMPONENT_LIST, load_component_class
    from config import APPS_SCHEMA

    entry = COMPONENT_LIST["wallbox"]
    assert load_component_class(entry) is WallboxAPI
    assert entry["event_filter"] == "predbat_wallbox_"
    assert entry["phase"] == 1 and entry["can_restart"] is True
    assert entry["args"]["username"]["required"] is True and entry["args"]["username"]["secret"] is True
    assert entry["args"]["password"]["required"] is True and entry["args"]["password"]["secret"] is True
    assert {spec["config"] for spec in entry["args"].values()} == {"wallbox_username", "wallbox_password", "wallbox_automatic", "wallbox_control", "wallbox_poll_seconds"}
    parameters = inspect.signature(WallboxAPI.initialize).parameters
    for arg_name, spec in entry["args"].items():
        assert arg_name in parameters, "initialize() has no parameter '{}'".format(arg_name)
        assert spec["config"] in APPS_SCHEMA, "{} missing from APPS_SCHEMA".format(spec["config"])
    print("  ✓ Component is registered with matching config keys")


def test_component_poll_seconds_is_clamped():
    """The poll interval is a whole number of minutes between the limits."""
    assert _make_component(poll_seconds=120).poll_seconds == 120
    assert _make_component(poll_seconds=90).poll_seconds == 120
    assert _make_component(poll_seconds=5).poll_seconds == MIN_POLL_SECONDS
    assert _make_component(poll_seconds=99999).poll_seconds == MAX_POLL_SECONDS
    assert _make_component(poll_seconds="junk").poll_seconds == 120
    print("  ✓ Poll interval is rounded and clamped")


def test_component_missing_credentials():
    """Without both credentials there is no transport and run() fails."""
    component = WallboxAPI(MockBase(), username=None, password="secret")
    assert component.transport is None
    assert run_async(component.run(0, True)) is False
    print("  ✓ Missing credentials leave the component failed")


def test_component_publishes_one_charger():
    """One charger without Eco-Smart publishes exactly the expected entities and values."""
    component = _make_component()
    assert run_async(component.run(0, True)) is True

    entities = component.base.entities
    prefix = "predbat_wallbox_101"
    wallbox_entities = {name for name in entities if "_wallbox_" in name}
    assert wallbox_entities == {
        "sensor.{}_status".format(prefix), "sensor.{}_power".format(prefix), "sensor.{}_session_energy".format(prefix),
        "binary_sensor.{}_connected".format(prefix), "binary_sensor.{}_charging".format(prefix),
        "switch.{}_charging".format(prefix), "switch.{}_locked".format(prefix), "number.{}_max_charging_current".format(prefix),
    }, wallbox_entities
    assert entities["sensor.{}_status".format(prefix)]["state"] == "Charging"
    status_attributes = entities["sensor.{}_status".format(prefix)]["attributes"]
    assert status_attributes["status_id"] == 193 and status_attributes["serial_number"] == "900001" and status_attributes["name"] == "Garage"
    assert entities["sensor.{}_power".format(prefix)]["state"] == 7200.0
    assert entities["sensor.{}_power".format(prefix)]["attributes"]["unit_of_measurement"] == "W"
    assert entities["sensor.{}_session_energy".format(prefix)]["state"] == 4.5
    assert entities["binary_sensor.{}_connected".format(prefix)]["state"] == "on"
    assert entities["binary_sensor.{}_charging".format(prefix)]["state"] == "on"
    assert entities["switch.{}_charging".format(prefix)]["state"] == "on"
    assert entities["switch.{}_locked".format(prefix)]["state"] == "off"
    number = entities["number.{}_max_charging_current".format(prefix)]
    assert number["state"] == 32 and number["attributes"]["min"] == 6 and number["attributes"]["max"] == 32 and number["attributes"]["step"] == 1
    assert component.last_success_timestamp is not None
    print("  ✓ One charger publishes the expected entities")


def test_component_publishes_two_chargers_and_eco_smart():
    """Each charger gets its own entities, and the Eco-Smart select appears only where supported."""
    component = _make_component({202: _status(status_id=161, power=0, energy=0), 101: _status(eco={"enabled": True, "mode": 1})})
    assert run_async(component.run(0, True)) is True

    entities = component.base.entities
    assert entities["sensor.predbat_wallbox_202_status"]["state"] == "Ready"
    assert entities["binary_sensor.predbat_wallbox_202_connected"]["state"] == "off"
    assert entities["switch.predbat_wallbox_202_charging"]["state"] == "off"
    select = entities["select.predbat_wallbox_101_eco_smart"]
    assert select["state"] == "full_solar" and select["attributes"]["options"] == ["off", "eco_mode", "full_solar"]
    assert "select.predbat_wallbox_202_eco_smart" not in entities
    assert [charger.charger_id for charger in component.ordered_chargers()] == ["101", "202"]
    print("  ✓ Two chargers publish separately, Eco-Smart only where supported")


def test_component_poll_cadence():
    """Status is polled on the first run and every poll_seconds; the charger list every 30 minutes."""
    component = _make_component()
    run_async(component.run(0, True))
    assert component.transport.count("get_status") == 1 and component.transport.count("list_chargers") == 1
    run_async(component.run(60, False))
    assert component.transport.count("get_status") == 1, "60s is between polls at poll_seconds=120"
    run_async(component.run(120, False))
    assert component.transport.count("get_status") == 2
    assert component.transport.count("list_chargers") == 1, "The charger list is not refetched every poll"
    run_async(component.run(1800, False))
    assert component.transport.count("list_chargers") == 2
    print("  ✓ Poll cadence follows poll_seconds and the 30 minute list refresh")


def test_component_no_chargers():
    """An account with no chargers warns on the first run and does not stamp a success."""
    component = _make_component({})
    assert run_async(component.run(0, True)) is True
    assert _logged(component, "no chargers were found")
    assert component.last_success_timestamp is None
    assert not [name for name in component.base.entities if "_wallbox_" in name]
    print("  ✓ An account with no chargers warns and publishes nothing")


def test_component_one_charger_failing_does_not_blank_the_other():
    """A status failure on one charger keeps the other published and keeps the charger order."""
    component = _make_component({101: _status(), 202: _status(status_id=161, power=0)})
    run_async(component.run(0, True))
    component.transport.errors[("get_status", 202)] = WallboxApiError("HTTP 500")
    component.transport.statuses[101] = _status(status_id=178, power=0)

    assert run_async(component.run(120, False)) is True
    assert component.base.entities["sensor.predbat_wallbox_101_status"]["state"] == "Paused"
    assert component.stale_ids == {"202"}
    assert [charger.charger_id for charger in component.ordered_chargers()] == ["101", "202"], "Charger N must stay car N"
    assert _logged(component, "could not read charger 202")

    assert run_async(component.run(240, False)) is True
    assert component.stale_ids == set()
    print("  ✓ One failing charger does not blank the other")


def test_component_every_charger_failing_fails_the_cycle():
    """When no charger could be read the cycle fails and no success is stamped."""
    component = _make_component()
    component.transport.errors[("get_status",)] = WallboxApiError("HTTP 500")
    assert run_async(component.run(0, True)) is False
    assert component.last_success_timestamp is None
    assert _logged(component, "poll failed")
    print("  ✓ A poll that reads no charger fails the cycle")


def test_component_auth_failure():
    """Bad credentials log an error naming the config keys and fail the cycle."""
    component = _make_component()
    component.transport.errors[("list_chargers",)] = WallboxAuthError("rejected")
    assert run_async(component.run(0, True)) is False
    assert _logged(component, "Error: wallbox:") and _logged(component, "wallbox_username")
    print("  ✓ Bad credentials are reported as an error")


def test_component_rate_limit_backoff():
    """A 429 skips polls on a doubling back-off capped at 15 minutes, then recovers."""
    component = _make_component()
    run_async(component.run(0, True))

    component.transport.errors[("get_status",)] = WallboxRateLimitError("429")
    assert run_async(component.run(120, False)) is True, "A rate limit after start-up is not a failed cycle"
    assert component.skip_cycles == 2 and _logged(component, "rate limited")

    polls = component.transport.count("get_status")
    run_async(component.run(180, False))
    run_async(component.run(240, False))
    assert component.transport.count("get_status") == polls, "Both skipped cycles made no call"

    for expected in (4, 8, 15, 15):
        component.skip_cycles = 0
        component.transport.errors[("get_status",)] = WallboxRateLimitError("429")
        run_async(component.run(120, False))
        assert component.skip_cycles == expected, (expected, component.skip_cycles)

    component.skip_cycles = 0
    assert run_async(component.run(120, False)) is True
    assert component.backoff_cycles == 0, "A good poll resets the back-off"
    print("  ✓ Rate limit back-off doubles, caps at 15 minutes and resets")


def test_component_rate_limit_on_first_run_fails():
    """A 429 on the very first run fails it, so the component is not marked started with no data."""
    component = _make_component()
    component.transport.errors[("list_chargers",)] = WallboxRateLimitError("429")
    assert run_async(component.run(0, True)) is False
    # The retry of the first run must poll, not be swallowed by the skip counter
    assert run_async(component.run(0, True)) is True
    assert "sensor.predbat_wallbox_101_status" in component.base.entities
    print("  ✓ A rate limit on the first run fails it and the retry polls")


def test_wallbox(my_predbat=None):
    """Run every Wallbox test."""
    print("=" * 70)
    print("Wallbox tests")
    print("=" * 70)
    test_basic_auth_header()
    test_transport_signs_in_then_lists_chargers()
    test_transport_reuses_a_valid_token()
    test_transport_refreshes_an_expired_token()
    test_transport_signs_in_when_the_refresh_is_rejected()
    test_transport_bad_credentials()
    test_transport_retries_once_after_a_revoked_token()
    test_transport_rate_limit_and_failures()
    test_normalise_charging()
    test_normalise_status_table()
    test_normalise_eco_smart()
    test_normalise_handles_bad_values()
    test_normalise_captured_payloads()
    test_transport_control_requests()
    test_transport_control_refused_for_rights()
    test_transport_control_accepts_an_empty_body()
    test_transport_rejects_an_unknown_eco_smart_mode()
    test_component_registration()
    test_component_poll_seconds_is_clamped()
    test_component_missing_credentials()
    test_component_publishes_one_charger()
    test_component_publishes_two_chargers_and_eco_smart()
    test_component_poll_cadence()
    test_component_no_chargers()
    test_component_one_charger_failing_does_not_blank_the_other()
    test_component_every_charger_failing_fails_the_cycle()
    test_component_auth_failure()
    test_component_rate_limit_backoff()
    test_component_rate_limit_on_first_run_fails()
    print("=" * 70)
    return False
