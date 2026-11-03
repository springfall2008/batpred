# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the Hypervolt EV charger integration
"""

import asyncio
import datetime
import json
import os
import random
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytz

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async
from mock_base import MockBase

from hypervolt import (
    HYPERVOLT_API_URL,
    HYPERVOLT_TOKEN_URL,
    HypervoltApiError,
    CHARGER_LIST_SECONDS,
    Backoff,
    HypervoltAPI,
    HypervoltAuthError,
    HypervoltCharger,
    HypervoltRefusedError,
    HypervoltTimeoutError,
    HypervoltTransport,
    STALE_SECONDS,
    apply_pilot,
    apply_plugncharge,
    apply_session,
    apply_snapshot,
    charge_mode_command,
    charge_mode_options,
    charger_api_version,
    charger_major_version,
    connected_state,
    is_stale,
    max_current_command,
    plugged_state,
    release_command,
    status_text,
)

# Charger ids are decimal; their hex form is 12 digits for a V2 and 16 for a V3
V2_ID = str(int("a" * 12, 16))
V3_ID = str(int("a" * 16, 16))


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
    records the method, URL, headers and form body of every request in order.
    """
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        """Record the request, then return or raise the next queued item."""
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}, "data": kwargs.get("data")})
        item = queue.pop(0) if queue else _response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def _token_payload(access="jwt-1", refresh="refresh-1", life=300):
    """A Keycloak token response."""
    return {"access_token": access, "refresh_token": refresh, "expires_in": life, "token_type": "Bearer"}


# Message bodies quoted in the Home Assistant integration's source (hypervolt_api_client.py).
# They stand in for messages captured from a real charger with `python3 hypervolt.py --raw`,
# and should be replaced by such captures (Rik's charger). Fields marked "assumed" are not in
# those examples.
MOCK_SNAPSHOT = [{"brightness": 0.25}, {"lock_state": "unlocked"}, {"release_state": "default"}, {"max_current": 32000}, {"ct_flags": 1}, {"solar_mode": "boost"}, {"features": ["super_eco"]}, {"random_start": True}]
MOCK_SESSION_IDLE = {"charging": False, "session": 240, "milli_amps": 32000, "true_milli_amps": 0, "watt_hours": 2371, "ccy_spent": 34, "carbon_saved_grams": 1036, "ct_current": 0, "ct_power": 0, "voltage": 0}
# assumed: a charging session in the same shape
MOCK_SESSION_CHARGING = {"charging": True, "session": 241, "milli_amps": 32000, "true_milli_amps": 31000, "watt_hours": 5120, "ccy_spent": 70, "carbon_saved_grams": 2100, "ct_current": 31000, "ct_power": 7440, "voltage": 240}
MOCK_PILOT_A = {"pilot_status": "A"}
MOCK_PILOT_B = {"pilot_status": "B"}
MOCK_PILOT_C = {"pilot_status": "C"}
MOCK_PLUGNCHARGE_GET = {"applied": "boost"}
MOCK_PLUGNCHARGE_SET = {"mode": "boost", "applied": True}
MOCK_FIRMWARE = "2483.2"

# Captured from a Home 3 Pro on firmware 4009 (V3 API), 26 Oct 2026 - the stand-ins above are V2-shaped:
# a V3 session carries no session id, milli_amps, ccy_spent or ct_power
MOCK_V3_SNAPSHOT = [{"brightness": 0.25}, {"lock_state": "unlocked"}, {"release_state": "default"}, {"max_current": 32000}, {"ct_flags": 0}, {"solar_mode": "boost"}, {"features": ["mms", "home-battery-drain-prevention", "enhanced-energy"]}, {"random_start": False}, {"effect_name": "party_mode"}]
MOCK_V3_FIRMWARE = "4009"
MOCK_V3_SESSION_UNPLUGGED = {"carbon_saved_grams": 0, "charging": False, "ct_current": 20600, "energy_flow": [], "ev_power": 0, "generation_power": 5065, "grid_power": -5065, "house_power": 0, "true_milli_amps": 0, "voltage": 0, "watt_hours": 0}
# The first messages of a charge: current is flowing but ev_power has not caught up
MOCK_V3_SESSION_STARTING = {"carbon_saved_grams": 2, "charging": True, "ct_current": 32700, "energy_flow": ["grid_to_ev", "grid_to_house"], "ev_power": 3, "generation_power": 0, "grid_power": 7716, "house_power": 7713, "true_milli_amps": 30400, "voltage": 231, "watt_hours": 7}
MOCK_V3_SESSION_CHARGING = {"carbon_saved_grams": 19, "charging": True, "ct_current": 33000, "energy_flow": ["grid_to_ev", "grid_to_house"], "ev_power": 7087, "generation_power": 0, "grid_power": 7692, "house_power": 605, "true_milli_amps": 30800, "voltage": 231, "watt_hours": 44}
# The shape of a V3 error reply, as the charger sent it when refusing get.pilot_status
MOCK_V3_ERROR = {"code": -32601, "data": None, "message": "get.pilot_status not allowed"}

MOCK_CHARGERS = {"chargers": [{"id": V3_ID, "model": "Home 3 Pro", "adopted_at": "2024-01-01T00:00:00.000Z"}]}


def test_api_version_from_id():
    """The charger generation comes from the id's length in bytes; updated V2 firmware uses the V3 API."""
    assert charger_major_version(V2_ID) == 2
    assert charger_major_version(V3_ID) == 3
    assert charger_major_version("junk") == 3
    assert charger_api_version(V2_ID, None) == 2
    assert charger_api_version(V2_ID, "1.0") == 3
    assert charger_api_version(V2_ID, 1) == 3
    assert charger_api_version(V2_ID, "202111231810.abcdef") == 2
    assert charger_api_version(V3_ID, None) == 3
    print("  ✓ API version from charger id and firmware")


def test_sign_in():
    """The first call is a password grant to Keycloak with the integration's client id."""
    session, calls = _session([_response(_token_payload())])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        token = run_async(transport.authenticate())

    assert token == "jwt-1"
    assert calls[0]["method"] == "POST" and calls[0]["url"] == HYPERVOLT_TOKEN_URL
    assert calls[0]["data"] == {"client_id": "home-assistant", "grant_type": "password", "scope": "openid profile email offline_access", "username": "user@example.com", "password": "secret"}
    assert transport.access_token == "jwt-1" and transport.refresh_token == "refresh-1"
    assert abs(transport.token_expiry - (time.time() + 300)) < 5
    print("  ✓ Signs in with a password grant")


def test_token_reused_then_refreshed():
    """A token with time to spare is reused; one inside the margin is refreshed, not signed in again."""
    session, calls = _session([_response(_token_payload(life=3600)), _response(_token_payload(access="jwt-2", refresh="refresh-2"))])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(transport.authenticate())
        run_async(transport.authenticate())
        assert len(calls) == 1
        assert transport.token_due() is False
        transport.token_expiry = time.time() + 60
        assert transport.token_due() is True
        token = run_async(transport.authenticate())

    assert token == "jwt-2"
    assert calls[1]["data"] == {"client_id": "home-assistant", "grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert transport.refresh_token == "refresh-2"
    print("  ✓ Token reused, then refreshed")


def test_refresh_rejected_then_sign_in():
    """A rejected refresh token falls back to a password sign in."""
    session, calls = _session([_response({"error": "invalid_grant"}, status=400), _response(_token_payload(access="jwt-3"))])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    transport.access_token = "jwt-old"
    transport.refresh_token = "refresh-old"
    transport.token_expiry = time.time() - 1
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        token = run_async(transport.authenticate())

    assert token == "jwt-3"
    assert [call["data"]["grant_type"] for call in calls] == ["refresh_token", "password"]
    print("  ✓ A rejected refresh falls back to sign in")


def test_bad_credentials():
    """A rejected sign in raises HypervoltAuthError without the password in its text."""
    for status in (400, 401):
        session, _ = _session([_response({"error": "invalid_grant"}, status=status)])
        transport = HypervoltTransport(print, "user@example.com", "s3cr3t-pw")
        with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
            try:
                run_async(transport.authenticate())
                raise AssertionError("Expected HypervoltAuthError")
            except HypervoltAuthError as exc:
                assert "s3cr3t-pw" not in str(exc)
        assert transport.access_token is None
    print("  ✓ Bad credentials raise HypervoltAuthError")


def test_list_chargers():
    """The charger list is read with the bearer token; a 401 signs in again once; a 500 is an API error."""
    session, calls = _session([_response(_token_payload()), _response(MOCK_CHARGERS)])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        chargers = run_async(transport.list_chargers())
    assert chargers == MOCK_CHARGERS["chargers"]
    assert calls[1]["method"] == "GET" and calls[1]["url"] == HYPERVOLT_API_URL + "users/me?includes=chargers"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-1"

    session, calls = _session([_response({}, status=401), _response(_token_payload(access="jwt-9")), _response(MOCK_CHARGERS)])
    transport.token_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        chargers = run_async(transport.list_chargers())
    assert chargers == MOCK_CHARGERS["chargers"]
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-9"

    session, _ = _session([_response({}, status=500)])
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        try:
            run_async(transport.list_chargers())
            raise AssertionError("Expected HypervoltApiError")
        except HypervoltApiError:
            pass
    print("  ✓ Lists chargers")


class FakeSocket:
    """An aiohttp websocket stand-in: receive() serves queued frames, send_str() records what was sent."""

    def __init__(self):
        """Start with nothing queued."""
        self.frames = []
        self.sent = []
        self.closed = False
        self.event = asyncio.Event()

    def queue(self, frame):
        """Queue a frame: a dict is sent as JSON, a str as it is, None as a close."""
        self.frames.append(frame)
        self.event.set()

    async def receive(self):
        """Return the next queued frame, waiting for one."""
        while not self.frames:
            self.event.clear()
            await self.event.wait()
        frame = self.frames.pop(0)
        if frame is None:
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, frame if isinstance(frame, str) else json.dumps(frame), None)

    async def send_str(self, text):
        """Record a sent message, decoded."""
        self.sent.append(json.loads(text))

    async def close(self):
        """Close: the reader sees a close frame."""
        self.closed = True
        self.queue(None)

    def methods(self):
        """The method of every message sent, in order."""
        return [message.get("method") for message in self.sent]

    def last(self, method):
        """The last message sent with this method."""
        return [message for message in self.sent if message.get("method") == method][-1]


def _ws_session(sockets):
    """A mock aiohttp session whose ws_connect() yields the next FakeSocket. Returns (session, connects).

    sockets is a list, or a dict from URL ending ("sync", "session/in-progress") to a list.
    """
    connects = []
    queues = {key: list(value) for key, value in sockets.items()} if isinstance(sockets, dict) else {"": list(sockets)}

    def _ws_connect(url, **kwargs):
        """Record the connect and hand out the next socket, from the queue whose key ends the URL when given a dict."""
        connects.append({"url": url, "headers": kwargs.get("headers") or {}})
        key = next((key for key in queues if key and url.endswith(key)), "")
        queue = queues.get(key, [])
        socket = queue.pop(0) if queue else FakeSocket()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=socket)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    session = MagicMock()
    session.ws_connect = _ws_connect
    session.close = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, connects


async def _settle(rounds=20):
    """Let the reader tasks run."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def _signed_in_transport():
    """A transport holding a valid token, with every message hook recorded in .seen."""
    transport = HypervoltTransport(print, "user@example.com", "secret")
    transport.access_token = "jwt-1"
    transport.refresh_token = "refresh-1"
    transport.token_expiry = time.time() + 3600
    transport.seen = []
    transport.sleeps = []
    original = transport.on_message

    def _on_message(charger_id, method, body):
        """Record the message, then apply it as the transport would."""
        transport.seen.append((charger_id, method, body))
        original(charger_id, method, body)

    async def _sleep(seconds):
        """Record a back-off instead of waiting it out."""
        transport.sleeps.append(seconds)
        await asyncio.sleep(0)

    transport.on_message = _on_message
    transport.sleep = _sleep
    return transport


def _reply_to(socket, method, result):
    """Queue the reply to the last message sent with this method."""
    socket.queue({"jsonrpc": "2.0", "id": socket.last(method)["id"], "result": result})


async def _logged_in(transport, socket, charger_id=V3_ID):
    """Connect a charger and answer its login."""
    await transport.connect(charger_id)
    await _settle()
    _reply_to(socket, "login", {"authenticated": True})
    await _settle()


def test_sync_login_and_state_requests():
    """The sync socket logs in with the token, then asks for state; plugncharge only on the V3 API,
    and get.pilot_status only on the V2 API - a V3 charger refuses it and pushes the status after login."""
    for charger_id, expect_plugncharge in ((V3_ID, True), (V2_ID, False)):
        socket = FakeSocket()
        session, connects = _ws_session([socket])

        async def scenario():
            """Connect and answer the login."""
            transport = _signed_in_transport()
            await _logged_in(transport, socket, charger_id)
            await transport.close()

        with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
            run_async(scenario())
        assert connects[0]["url"] == "wss://api.hypervolt.co.uk/ws/charger/{}/sync".format(charger_id)
        assert connects[0]["headers"]["Origin"] == "https://hypervolt.co.uk"
        assert connects[0]["headers"]["User-Agent"] == "Predbat"
        assert socket.sent[0]["method"] == "login" and socket.sent[0]["params"] == {"token": "jwt-1", "version": 3}
        assert all(message["jsonrpc"] == "2.0" and message["id"] for message in socket.sent)
        assert len({message["id"] for message in socket.sent}) == len(socket.sent)
        methods = socket.methods()
        for method in ("sync.snapshot", "firmware.version", "get.name"):
            assert method in methods, methods
        assert ("plugncharge.get" in methods) == expect_plugncharge, methods
        assert ("get.pilot_status" in methods) == (not expect_plugncharge), methods
    print("  ✓ Sync socket logs in then asks for state")


def test_login_refused():
    """A refused login closes the socket, and commands then fail at once."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Refuse the login, then try a command."""
        transport = _signed_in_transport()
        await transport.connect(V3_ID)
        await _settle()
        _reply_to(socket, "login", {"authenticated": False})
        await _settle()
        try:
            await transport.request(V3_ID, "sync.apply", {"release": True})
            raise AssertionError("Expected HypervoltApiError")
        except HypervoltApiError:
            pass
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    assert socket.closed
    print("  ✓ Refused login closes the socket")


def test_request_reply():
    """A command returns its own reply's result; a push arriving first goes to on_message, not the caller."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Send a command and answer it after an unrelated push."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        task = asyncio.ensure_future(transport.request(V3_ID, "sync.apply", {"release": True}))
        await _settle()
        sent = socket.last("sync.apply")
        assert sent["params"] == {"release": True}
        socket.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "B"}})
        socket.queue({"jsonrpc": "2.0", "id": sent["id"], "result": [{"release_state": "released"}]})
        result = await task
        await transport.close()
        return transport, result

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport, result = run_async(scenario())
    assert result == [{"release_state": "released"}]
    assert (V3_ID, "get.pilot_status", {"pilot_status": "B"}) in transport.seen
    print("  ✓ Command returns its own reply")


def test_request_error_reply():
    """An error reply raises HypervoltRefusedError carrying Hypervolt's code and reason."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Answer a command with an error."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        task = asyncio.ensure_future(transport.request(V3_ID, "sync.apply", {"max_current": 16000}))
        await _settle()
        socket.queue({"jsonrpc": "2.0", "id": socket.last("sync.apply")["id"], "error": {"code": 409, "error": "Concurrent modifications invalidated this request", "data": None}})
        try:
            await task
            raise AssertionError("Expected HypervoltRefusedError")
        except HypervoltRefusedError as exc:
            assert "409" in str(exc) and "Concurrent modifications" in str(exc), str(exc)
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ Error reply raises HypervoltRefusedError")


def test_request_error_reply_v3_shape():
    """A V3 error reply carries its reason in "message", and the raised error keeps it."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Answer a command with a captured V3 error."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        task = asyncio.ensure_future(transport.request(V3_ID, "sync.apply", {"max_current": 16000}))
        await _settle()
        socket.queue({"jsonrpc": "2.0", "id": socket.last("sync.apply")["id"], "error": MOCK_V3_ERROR})
        try:
            await task
            raise AssertionError("Expected HypervoltRefusedError")
        except HypervoltRefusedError as exc:
            assert "-32601" in str(exc) and "not allowed" in str(exc), str(exc)
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ V3 error reply keeps its reason")


def test_request_timeout():
    """A command with no reply raises HypervoltTimeoutError and is forgotten."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Send a command nobody answers."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        try:
            await transport.request(V3_ID, "sync.apply", {"release": False})
            raise AssertionError("Expected HypervoltTimeoutError")
        except HypervoltTimeoutError:
            pass
        assert transport.links[V3_ID].pending == {}
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"), patch("hypervolt.COMMAND_TIMEOUT_SECONDS", 0.05):
        run_async(scenario())
    print("  ✓ Unanswered command times out")


def test_late_reply_is_ignored():
    """A reply nobody is waiting for is dropped and the reader carries on."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Send an unknown reply, then a push."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        socket.queue({"jsonrpc": "2.0", "id": "999", "result": [{"release_state": "released"}]})
        socket.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "A"}})
        await _settle()
        await transport.close()
        return transport

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport = run_async(scenario())
    assert (V3_ID, "get.pilot_status", {"pilot_status": "A"}) in transport.seen
    assert not [seen for seen in transport.seen if seen[2] == [{"release_state": "released"}]]
    print("  ✓ Late reply is ignored")


def test_reader_skips_bad_json():
    """A frame that is not JSON is skipped; the next one is processed."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Send garbage, then a push."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        socket.queue("not json {")
        socket.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "C"}})
        await _settle()
        await transport.close()
        return transport

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport = run_async(scenario())
    assert (V3_ID, "get.pilot_status", {"pilot_status": "C"}) in transport.seen
    print("  ✓ Bad JSON is skipped")


def test_replies_and_pushes_reach_on_message():
    """Replies to the state requests reach on_message under the method they answer."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Answer each state request."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        _reply_to(socket, "sync.snapshot", [{"release_state": "default"}])
        _reply_to(socket, "plugncharge.get", {"applied": "boost"})
        _reply_to(socket, "firmware.version", "2483.2")
        _reply_to(socket, "get.name", "Driveway")
        socket.queue({"jsonrpc": "2.0", "method": "get.session", "params": {"charging": False}})
        await _settle()
        await transport.close()
        return transport

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport = run_async(scenario())
    for expected in ((V3_ID, "sync.snapshot", [{"release_state": "default"}]), (V3_ID, "plugncharge.get", {"applied": "boost"}), (V3_ID, "firmware.version", "2483.2"), (V3_ID, "get.name", "Driveway"), (V3_ID, "get.session", {"charging": False})):
        assert expected in transport.seen, transport.seen
    print("  ✓ Replies and pushes reach on_message")


def test_raw_log_redacts_token():
    """The raw message log never shows the access token."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])
    logged = []

    async def scenario():
        """Connect with the raw log on."""
        transport = _signed_in_transport()
        transport.raw_log = lambda label, message: logged.append((label, message))
        await _logged_in(transport, socket)
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    login = [message for _, message in logged if message.get("method") == "login"][0]
    assert login["params"]["token"] == "********"
    assert "jwt-1" not in json.dumps(logged)
    assert socket.sent[0]["params"]["token"] == "jwt-1"
    print("  ✓ Raw log redacts the token")


def test_close_cancels_readers():
    """close() ends every reader task and closes every socket."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Connect, then close."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        tasks = list(transport.links[V3_ID].tasks)
        await transport.close()
        return tasks

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        tasks = run_async(scenario())
    assert tasks and all(task.done() for task in tasks)
    assert socket.closed
    print("  ✓ close() ends the readers")


def test_apply_snapshot():
    """A snapshot sets release state, current in amps, charge mode, features and lock state."""
    charger = HypervoltCharger(V3_ID)
    apply_snapshot(charger, MOCK_SNAPSHOT)
    assert charger.release_state == "default"
    assert charger.max_current_a == 32 and isinstance(charger.max_current_a, int)
    assert charger.charge_mode == "boost"
    assert charger.features == ["super_eco"]
    assert charger.lock_state == "unlocked"
    apply_snapshot(charger, [{"release_state": "released"}])
    assert charger.release_state == "released" and charger.max_current_a == 32
    print("  ✓ Snapshot applied")


def test_apply_snapshot_ignores_bad_items():
    """An unknown enum value or a non-number is skipped and the rest still applied."""
    charger = HypervoltCharger(V3_ID, release_state="default")
    apply_snapshot(charger, [{"release_state": "paused"}, {"max_current": "x"}, {"max_current": 16000}, "junk", {"solar_mode": "turbo"}, {"features": "nope"}])
    assert charger.release_state == "default"
    assert charger.max_current_a == 16
    assert charger.charge_mode is None and charger.features == []
    apply_snapshot(charger, None)
    apply_snapshot(charger, {"release_state": "released"})
    assert charger.release_state == "released"
    print("  ✓ Bad snapshot items are skipped")


def test_apply_session_power():
    """Power is ev_power when sent (unless it is stale-low against amps times volts), else amps times volts, and zero when not charging."""
    charger = HypervoltCharger(V3_ID)
    apply_session(charger, {"charging": True, "ev_power": 7100, "true_milli_amps": 16000, "voltage": 240})
    assert charger.charging is True and charger.power_w == 7100.0
    apply_session(charger, {"charging": True, "true_milli_amps": 16000, "voltage": 240})
    assert charger.power_w == 3840.0
    apply_session(charger, {"charging": False, "true_milli_amps": 16000, "voltage": 240})
    assert charger.charging is False and charger.power_w == 0.0
    apply_session(charger, {"charging": True, "ev_power": "bad"})
    assert charger.charging is True and charger.power_w == 0.0
    print("  ✓ Session power")


def test_session_energy_holds_and_resets():
    """Session energy is the session's maximum, held between sessions, restarting with a new session id."""
    charger = HypervoltCharger(V3_ID)
    apply_session(charger, {"charging": True, "session": 7, "watt_hours": 2371})
    assert charger.session_energy_kwh == 2.371
    apply_session(charger, {"charging": True, "session": 7, "watt_hours": 1000})
    assert charger.session_energy_kwh == 2.371
    apply_session(charger, {"charging": False})
    assert charger.session_energy_kwh == 2.371
    apply_session(charger, {"charging": True, "session": 8, "watt_hours": 150})
    assert charger.session_energy_kwh == 0.15 and charger.session_id == 8
    print("  ✓ Session energy holds and resets")


def test_session_energy_v3_without_session_id():
    """V3 sends no session id and watt_hours drops to 0 at a stop, so a lower value starts a new session."""
    charger = HypervoltCharger(V3_ID, api_version=3)
    for watt_hours, expected in ((0, 0.0), (3343, 3.343), (0, 0.0), (1000, 1.0), (17836, 17.836), (0, 0.0), (1664, 1.664)):
        apply_session(charger, {"charging": watt_hours > 0, "watt_hours": watt_hours})
        assert charger.session_energy_kwh == expected
    apply_session(charger, {"charging": False, "ct_current": 30100})
    assert charger.session_energy_kwh == 1.664
    # A lower reading other than 0 also starts a new session, so a lost 0 at a stop does not freeze the sensor
    apply_session(charger, {"charging": True, "watt_hours": 500})
    assert charger.session_energy_kwh == 0.5
    # With a session id a lower value in the same session is still ignored
    apply_session(charger, {"charging": True, "session": 9, "watt_hours": 5000})
    apply_session(charger, {"charging": True, "session": 9, "watt_hours": 0})
    assert charger.session_energy_kwh == 5.0
    # A V2 charger keeps its session maximum even for a message without a session id
    v2 = HypervoltCharger(V2_ID)
    apply_session(v2, {"charging": True, "session": 9, "watt_hours": 5000})
    apply_session(v2, {"charging": True, "watt_hours": 4000})
    assert v2.session_energy_kwh == 5.0
    print("  ✓ Session energy on V3 without a session id")


def test_apply_pilot():
    """Pilot A is unplugged, B and C plugged; anything else is ignored."""
    charger = HypervoltCharger(V3_ID)
    apply_pilot(charger, MOCK_PILOT_A)
    assert connected_state(charger) == "off" and plugged_state(charger) == "off"
    for frame in (MOCK_PILOT_B, MOCK_PILOT_C):
        apply_pilot(charger, frame)
        assert connected_state(charger) == "on" and plugged_state(charger) == "on"
    apply_pilot(charger, {"pilot_status": "E"})
    apply_pilot(charger, "junk")
    assert charger.pilot_status == "C"
    print("  ✓ Pilot status")


def test_connected_state_unknown_until_pilot():
    """With no pilot status, connected is on only while charging and plugged is never guessed."""
    charger = HypervoltCharger(V3_ID)
    assert connected_state(charger) == "unavailable" and plugged_state(charger) == "unavailable"
    charger.charging = True
    assert connected_state(charger) == "on" and plugged_state(charger) == "unavailable"
    print("  ✓ Connection unknown until the pilot status arrives")


def test_apply_plugncharge():
    """Both reply forms set the mode; a refused set or unknown mode does not."""
    charger = HypervoltCharger(V3_ID)
    apply_plugncharge(charger, MOCK_PLUGNCHARGE_GET)
    assert charger.charge_mode == "boost"
    apply_plugncharge(charger, {"mode": "eco", "applied": True})
    assert charger.charge_mode == "eco"
    apply_plugncharge(charger, {"mode": "super_eco", "applied": False})
    assert charger.charge_mode == "eco"
    apply_plugncharge(charger, {"applied": "warp"})
    apply_plugncharge(charger, ["junk"])
    assert charger.charge_mode == "eco"
    print("  ✓ Plug and charge mode")


def test_status_text():
    """Status reads charging, stopped, connected, disconnected or unknown."""
    assert status_text(HypervoltCharger(V3_ID)) == "unknown"
    assert status_text(HypervoltCharger(V3_ID, charging=True, release_state="released")) == "charging"
    assert status_text(HypervoltCharger(V3_ID, release_state="released", pilot_status="B")) == "stopped"
    assert status_text(HypervoltCharger(V3_ID, release_state="default", pilot_status="B")) == "connected"
    assert status_text(HypervoltCharger(V3_ID, release_state="default", pilot_status="A")) == "disconnected"
    print("  ✓ Status text")


def test_charge_mode_options():
    """Battery Safe is offered only when the charger lists the feature."""
    assert charge_mode_options(HypervoltCharger(V3_ID)) == ["boost", "eco", "super_eco"]
    assert charge_mode_options(HypervoltCharger(V3_ID, features=["home-battery-drain-prevention"])) == ["boost", "eco", "super_eco", "super_eco_battery_safe"]
    print("  ✓ Charge mode options")


def test_captured_payloads():
    """The stand-in messages, applied in order, give the state they describe."""
    charger = HypervoltCharger(V3_ID)
    apply_snapshot(charger, MOCK_SNAPSHOT)
    apply_plugncharge(charger, MOCK_PLUGNCHARGE_SET)
    apply_session(charger, MOCK_SESSION_IDLE)
    apply_pilot(charger, MOCK_PILOT_B)
    assert status_text(charger) == "connected" and charger.power_w == 0.0 and charger.session_energy_kwh == 2.371
    apply_session(charger, MOCK_SESSION_CHARGING)
    apply_pilot(charger, MOCK_PILOT_C)
    assert status_text(charger) == "charging" and charger.power_w == 7440.0 and charger.session_energy_kwh == 5.12
    print("  ✓ Stand-in payloads give their states")


def test_captured_v3_payloads():
    """Messages captured from a real V3 charger give the state they were captured in."""
    charger = HypervoltCharger(V3_ID, api_version=charger_api_version(V3_ID, MOCK_V3_FIRMWARE))
    apply_snapshot(charger, MOCK_V3_SNAPSHOT)
    assert charger.release_state == "default" and charger.max_current_a == 32 and charger.charge_mode == "boost"
    apply_pilot(charger, MOCK_PILOT_A)
    apply_session(charger, MOCK_V3_SESSION_UNPLUGGED)
    assert status_text(charger) == "disconnected" and charger.power_w == 0.0 and charger.session_energy_kwh == 0.0
    apply_pilot(charger, MOCK_PILOT_C)
    apply_session(charger, MOCK_V3_SESSION_STARTING)
    # ev_power reads 3 W while 30.4 A flows at 231 V, so the power comes from current x voltage
    assert status_text(charger) == "charging" and abs(charger.power_w - 30.4 * 231) < 0.5, charger.power_w
    apply_session(charger, MOCK_V3_SESSION_CHARGING)
    assert charger.power_w == 7087.0 and charger.session_energy_kwh == 0.044
    # A message with no power fields keeps the last power rather than reading 0 mid-charge
    apply_session(charger, {"charging": True, "watt_hours": 45})
    assert charger.power_w == 7087.0
    # A noisy trickle of current with ev_power 0 is not invented into power
    apply_session(charger, dict(MOCK_V3_SESSION_CHARGING, ev_power=0, true_milli_amps=300))
    assert charger.power_w == 0.0
    # Stopping (or unplugging) ends the session: watt_hours starts again from 0 with no session id, so the energy resets
    apply_session(charger, dict(MOCK_V3_SESSION_CHARGING, watt_hours=0))
    assert charger.session_energy_kwh == 0.0
    print("  ✓ Captured V3 payloads give their states")


def test_on_message_updates_charger():
    """on_message keeps transport.chargers current and stamps last_message; firmware can promote a V2."""
    transport = HypervoltTransport(print, "user@example.com", "secret")
    transport.on_message(V3_ID, "get.pilot_status", MOCK_PILOT_B)
    transport.on_message(V3_ID, "sync.apply", [{"release_state": "released"}])
    transport.on_message(V3_ID, "get.session", MOCK_SESSION_CHARGING)
    transport.on_message(V3_ID, "plugncharge.set", {"mode": "eco", "applied": True})
    transport.on_message(V3_ID, "get.name", "Driveway")
    charger = transport.chargers[V3_ID]
    assert charger.pilot_status == "B" and charger.release_state == "released" and charger.charging is True
    assert charger.charge_mode == "eco" and charger.name == "Driveway"
    assert abs(charger.last_message - time.time()) < 5
    assert not is_stale(charger, time.time()) and is_stale(charger, time.time() + STALE_SECONDS + 1)

    transport.on_message(V2_ID, "firmware.version", "202111231810.abcdef")
    assert transport.chargers[V2_ID].api_version == 2 and transport.chargers[V2_ID].firmware_version == "202111231810.abcdef"
    transport.on_message(V2_ID, "firmware.version", "1.0")
    assert transport.chargers[V2_ID].api_version == 3
    print("  ✓ on_message updates the charger")


def test_backoff_sequence():
    """Back-off starts at 3-12 seconds, grows by 1.7 to a 300 second cap, and resets."""
    random.seed(1)
    backoff = Backoff()
    first = backoff.next()
    assert 3 <= first <= 12
    delays = [first] + [backoff.next() for _ in range(12)]
    for previous, current in zip(delays, delays[1:]):
        assert current == min(300, int(previous * 1.7)), delays
    assert delays[-1] == 300
    backoff.reset()
    assert 3 <= backoff.next() <= 12
    print("  ✓ Back-off sequence")


def test_reconnects_with_backoff():
    """A closed socket is reopened after a back-off and logs in again; four messages reset the back-off."""
    first, second = FakeSocket(), FakeSocket()
    session, connects = _ws_session([first, second])

    async def scenario():
        """Close the first socket from the server side."""
        transport = _signed_in_transport()
        await _logged_in(transport, first)
        first.queue(None)
        await _settle()
        assert len(connects) == 2 and second.methods()[0] == "login"
        _reply_to(second, "login", {"authenticated": True})
        for _ in range(4):
            second.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "B"}})
        await _settle()
        assert transport.links[V3_ID].backoff.delay is None
        await transport.close()
        return transport

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport = run_async(scenario())
    assert len(transport.sleeps) == 1 and 3 <= transport.sleeps[0] <= 12
    print("  ✓ Reconnects with back-off")


def test_drop_fails_pending_commands():
    """A command in flight when the socket drops fails at once rather than timing out."""
    first = FakeSocket()
    session, _ = _ws_session([first])

    async def scenario():
        """Drop the socket under a command."""
        transport = _signed_in_transport()
        await _logged_in(transport, first)
        task = asyncio.ensure_future(transport.request(V3_ID, "sync.apply", {"release": True}))
        await _settle()
        first.queue(None)
        started = time.time()
        try:
            await task
            raise AssertionError("Expected HypervoltApiError")
        except HypervoltTimeoutError:
            raise AssertionError("Timed out instead of failing at once")
        except HypervoltApiError:
            pass
        assert time.time() - started < 1
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ A drop fails pending commands")


def test_reconnect_stops_on_auth_failure():
    """A sign in refused during a reconnect stops the reader; a later good sign in and reconnect_all start it again."""
    first, second = FakeSocket(), FakeSocket()
    session, connects = _ws_session([first, second])

    async def scenario():
        """Expire the token and refuse the sign in, then accept it."""
        transport = _signed_in_transport()
        await _logged_in(transport, first)
        transport.token_expiry = 0
        transport.authenticate = AsyncMock(side_effect=HypervoltAuthError("refused"))
        first.queue(None)
        await _settle()
        assert transport.auth_failed is True and len(connects) == 1
        assert all(task.done() for task in transport.links[V3_ID].tasks)
        transport.authenticate = AsyncMock(return_value="jwt-2")
        transport.auth_failed = False
        await transport.reconnect_all()
        await _settle()
        assert len(connects) == 2 and second.sent[0]["params"]["token"] == "jwt-2"
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ Reconnect stops on an auth failure")


def test_successful_sign_in_clears_auth_failed():
    """A token obtained again, by an explicit retry, clears the auth failure flag."""
    session, _ = _session([_response(_token_payload())])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    transport.auth_failed = True
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(transport.authenticate(retry_refused=True))
    assert transport.auth_failed is False
    print("  ✓ A good sign in clears the auth failure")


def test_session_socket_only_for_api_2():
    """An API 2 charger also gets the session socket, whose frames arrive as get.session; an API 3 charger does not."""
    sync, sess = FakeSocket(), FakeSocket()
    session, connects = _ws_session({"sync": [sync], "session/in-progress": [sess]})

    async def scenario():
        """Connect a V2 charger and send a session frame."""
        transport = _signed_in_transport()
        await _logged_in(transport, sync, V2_ID)
        sess.queue(MOCK_SESSION_CHARGING)
        await _settle()
        await transport.close()
        return transport

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport = run_async(scenario())
    urls = sorted(connect["url"] for connect in connects)
    assert urls == ["wss://api.hypervolt.co.uk/ws/charger/{}/session/in-progress".format(V2_ID), "wss://api.hypervolt.co.uk/ws/charger/{}/sync".format(V2_ID)]
    assert (V2_ID, "get.session", MOCK_SESSION_CHARGING) in transport.seen
    assert sess.sent == []

    sync3 = FakeSocket()
    session, connects = _ws_session({"sync": [sync3]})

    async def scenario3():
        """Connect a V3 charger."""
        transport = _signed_in_transport()
        await _logged_in(transport, sync3, V3_ID)
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario3())
    assert [connect["url"] for connect in connects] == ["wss://api.hypervolt.co.uk/ws/charger/{}/sync".format(V3_ID)]
    print("  ✓ Session socket only for the V2 API")


def test_promotion_to_api_3():
    """Short firmware on a V2 charger moves it to the V3 API: plugncharge.get is sent and the session socket closed."""
    sync, sess = FakeSocket(), FakeSocket()
    session, _ = _ws_session({"sync": [sync], "session/in-progress": [sess]})

    async def scenario():
        """Answer the firmware request with an updated version."""
        transport = _signed_in_transport()
        await _logged_in(transport, sync, V2_ID)
        assert "plugncharge.get" not in sync.methods()
        _reply_to(sync, "firmware.version", "1.0")
        await _settle()
        link = transport.links[V2_ID]
        assert link.api_version == 3 and "plugncharge.get" in sync.methods()
        assert sess.closed and link.session is None
        await _settle()
        await transport.close()
        return transport

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        transport = run_async(scenario())
    assert transport.sleeps == []
    print("  ✓ Promotion to the V3 API")


def test_reconnect_all_uses_new_token():
    """reconnect_all closes every socket and logs in again with the current token."""
    first, second = FakeSocket(), FakeSocket()
    session, connects = _ws_session([first, second])

    async def scenario():
        """Change the token, then reconnect."""
        transport = _signed_in_transport()
        await _logged_in(transport, first)
        transport.access_token = "jwt-new"
        await transport.reconnect_all()
        await _settle()
        assert first.closed and len(connects) == 2
        assert second.sent[0]["params"]["token"] == "jwt-new"
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ reconnect_all uses the new token")


def test_probe_and_reconnect_one():
    """probe() asks a charger for a snapshot; reconnect() reopens just that charger."""
    first, second = FakeSocket(), FakeSocket()
    session, connects = _ws_session([first, second])

    async def scenario():
        """Probe, then reconnect."""
        transport = _signed_in_transport()
        await _logged_in(transport, first)
        before = first.methods().count("sync.snapshot")
        await transport.probe(V3_ID)
        assert first.methods().count("sync.snapshot") == before + 1
        await transport.probe("unknown")
        await transport.reconnect(V3_ID)
        await _settle()
        assert first.closed and len(connects) == 2
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ Probe and single reconnect")


def test_command_builders():
    """Each control maps to the method and params the integration sends."""
    assert release_command(True) == ("sync.apply", {"release": False})
    assert release_command(False) == ("sync.apply", {"release": True})
    assert max_current_command(16) == ("sync.apply", {"max_current": 16000})
    assert max_current_command(40) == ("sync.apply", {"max_current": 32000})
    assert max_current_command(3) == ("sync.apply", {"max_current": 6000})
    assert charge_mode_command(3, "eco") == ("plugncharge.set", {"mode": "eco"})
    assert charge_mode_command(2, "eco") == ("sync.apply", {"solar_mode": "eco"})
    print("  ✓ Command builders")


class _StubTransport:
    """In-memory transport: holds HypervoltCharger records by id and records every call."""

    def __init__(self, chargers=None):
        """Hold the chargers; errors maps a call name to an exception raised once."""
        now = time.time()
        if chargers is None:
            chargers = [HypervoltCharger(V3_ID, name="Driveway", model="Home 3 Pro", api_version=3, release_state="default", charging=True, pilot_status="C", power_w=7200.0, session_id=5, session_energy_kwh=4.5, max_current_a=32, charge_mode="boost", lock_state="unlocked", last_message=now)]
        self.chargers = {charger.charger_id: charger for charger in chargers}
        self.listed = list(self.chargers)
        self.calls = []
        self.errors = {}
        self.due = False
        self.auth_failed = False
        self.dead = set()

    def link_alive(self, charger_id):
        """Whether the charger's reader is running, as the test set it."""
        return charger_id not in self.dead

    def _record(self, *call):
        """Record a call, raising the queued error for it if there is one."""
        self.calls.append(call)
        error = self.errors.pop(call[0], None)
        if error:
            raise error

    def token_due(self):
        """Whether the token needs renewing, as the test set it."""
        return self.due

    async def authenticate(self, retry_refused=False):
        """Record a sign in."""
        self._record("authenticate")
        self.auth_failed = False
        self.due = False
        return "jwt-1"

    async def list_chargers(self):
        """Return the listed chargers."""
        self._record("list_chargers")
        return [{"id": charger_id, "model": "Home 3 Pro"} for charger_id in self.listed]

    async def connect(self, charger_id):
        """Record a connect."""
        self._record("connect", charger_id)

    async def reconnect_all(self):
        """Record a reconnect of every charger."""
        self._record("reconnect_all")

    async def reconnect(self, charger_id):
        """Record a reconnect of one charger."""
        self._record("reconnect", charger_id)
        self.dead.discard(charger_id)

    async def probe(self, charger_id):
        """Record a probe."""
        self._record("probe", charger_id)

    async def request(self, charger_id, method, params=None):
        """Record a command."""
        self._record("request", charger_id, method, params)
        return {}

    async def close(self):
        """Record a close."""
        self._record("close")

    def count(self, name):
        """How many calls of this name were made."""
        return len([call for call in self.calls if call[0] == name])


def _make_component(chargers=None, **overrides):
    """Build a HypervoltAPI against MockBase with a stub transport and a captured log."""
    args = {"username": "user@example.com", "password": "secret", "automatic": True, "hypervolt_control": False}
    args.update(overrides)
    component = HypervoltAPI(MockBase(), **args)
    component.transport = _StubTransport(chargers)
    component.first_wait_seconds = 0
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

    entry = COMPONENT_LIST["hypervolt"]
    assert load_component_class(entry) is HypervoltAPI
    assert entry["name"] == "Hypervolt Charger" and entry["event_filter"] == "predbat_hypervolt_"
    assert entry["phase"] == 1 and entry["can_restart"] is True
    assert entry["args"]["username"] == {"required": True, "secret": True, "config": "hypervolt_username"}
    assert entry["args"]["password"] == {"required": True, "secret": True, "config": "hypervolt_password"}
    assert entry["args"]["automatic"] == {"required": False, "config": "hypervolt_automatic", "default": True}
    assert entry["args"]["hypervolt_control"] == {"required": False, "config": "hypervolt_control", "default": False}
    assert len(entry["args"]) == 4
    parameters = inspect.signature(HypervoltAPI.initialize).parameters
    for arg_name in entry["args"]:
        assert arg_name in parameters, "initialize() has no parameter '{}'".format(arg_name)
    assert APPS_SCHEMA["hypervolt_username"] == {"type": "string", "empty": False}
    assert APPS_SCHEMA["hypervolt_password"] == {"type": "string", "empty": False}
    assert APPS_SCHEMA["hypervolt_automatic"] == {"type": "boolean"}
    assert APPS_SCHEMA["hypervolt_control"] == {"type": "boolean"}
    print("  ✓ Component is registered with matching config keys")


def test_component_missing_credentials():
    """Without both credentials there is no transport and run() fails."""
    component = HypervoltAPI(MockBase(), username=None, password="secret")
    assert component.transport is None
    assert run_async(component.run(0, True)) is False
    print("  ✓ Missing credentials leave the component failed")


def test_run_first_signs_in_lists_and_connects():
    """The first run signs in, lists and connects each charger in id order, and succeeds only with fresh data."""
    other = str(int("b" * 16, 16))
    component = _make_component([HypervoltCharger(other, last_message=time.time()), HypervoltCharger(V3_ID, last_message=time.time())])
    assert run_async(component.run(0, True)) is True
    calls = component.transport.calls
    assert calls[0] == ("authenticate",) and calls[1] == ("list_chargers",)
    assert [call[1] for call in calls if call[0] == "connect"] == sorted([V3_ID, other], key=int)
    assert component.car_order == sorted([V3_ID, other], key=int)

    stale = _make_component([HypervoltCharger(V3_ID, last_message=0)])
    assert run_async(stale.run(0, True)) is False
    assert stale.last_success_timestamp is None
    print("  ✓ First run signs in, lists and connects")


def test_run_refreshes_token():
    """A token near expiry is renewed without tearing down the sockets, so commands in the same run still work."""
    component = _make_component()
    run_async(component.run(0, True))
    component.transport.due = True
    run_async(component.run(60, False))
    assert component.transport.count("authenticate") == 2
    assert component.transport.count("reconnect_all") == 0
    print("  ✓ Token renewed without reconnecting")


def test_run_restarts_readers_after_auth_recovery():
    """Once a refused sign in succeeds again, the stopped readers are restarted."""
    component = _make_component()
    run_async(component.run(0, True))
    component.transport.auth_failed = True
    run_async(component.run(60, False))
    assert component.transport.count("reconnect_all") == 1
    print("  ✓ Readers restarted after the sign in recovers")


def test_short_lived_token_not_due():
    """A 300 second token is not due straight after sign in; it falls due in its second half."""
    session, _ = _session([_response(_token_payload(life=300))])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(transport.authenticate())
    assert transport.token_due() is False
    transport.token_expiry = time.time() + 100
    assert transport.token_due() is True
    session, _ = _session([_response({"access_token": "jwt-x"})])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(transport.authenticate())
    assert transport.token_due() is False, "A reply without expires_in is not taken as already expired"
    print("  ✓ A short-lived token is not due at once")


def test_concurrent_authenticate_makes_one_request():
    """Readers signing in together share one token request."""
    transport = HypervoltTransport(print, "user@example.com", "secret")
    calls = []

    async def _slow_call(method, url, headers=None, data=None):
        """A token request that takes a moment, as a real one does, so the callers overlap."""
        calls.append(data)
        await asyncio.sleep(0.01)
        return 200, _token_payload(access="jwt-{}".format(len(calls)), life=3600)

    transport._call = _slow_call

    async def scenario():
        """Sign in from three places at once."""
        return await asyncio.gather(transport.authenticate(), transport.authenticate(), transport.authenticate())

    with patch("hypervolt.record_api_call"):
        tokens = run_async(scenario())
    assert len(calls) == 1 and tokens == ["jwt-1", "jwt-1", "jwt-1"], (calls, tokens)
    print("  ✓ Concurrent sign ins share one request")


def test_run_probes_then_reconnects_stale():
    """A quiet charger is probed, a silent one reconnected, a fresh one left alone."""
    quiet, silent = str(int("b" * 16, 16)), str(int("c" * 16, 16))
    now = time.time()
    component = _make_component([HypervoltCharger(V3_ID, last_message=now), HypervoltCharger(quiet, last_message=now - 300), HypervoltCharger(silent, last_message=now - 600)])
    assert run_async(component.run(0, True)) is True
    calls = component.transport.calls
    assert ("probe", quiet) in calls and ("reconnect", silent) in calls
    assert ("probe", V3_ID) not in calls and ("reconnect", V3_ID) not in calls
    run_async(component.run(60, False))
    assert component.transport.count("reconnect") == 1, "A charger just reconnected is given time to answer"
    print("  ✓ Stale chargers probed then reconnected")


def test_run_lists_chargers_every_30_minutes():
    """The charger list is re-read every 30 minutes and a new charger connected and appended."""
    component = _make_component()
    run_async(component.run(0, True))
    added = str(int("1" * 16, 16))
    component.transport.chargers[added] = HypervoltCharger(added, last_message=time.time())
    component.transport.listed.append(added)
    run_async(component.run(60, False))
    assert component.transport.count("list_chargers") == 1
    run_async(component.run(CHARGER_LIST_SECONDS, False))
    assert component.transport.count("list_chargers") == 2
    assert ("connect", added) in component.transport.calls
    assert component.car_order == [V3_ID, added], "A charger added later becomes the next car even with a lower id"
    print("  ✓ Charger list refreshed every 30 minutes")


def test_run_auth_failure_after_start():
    """A refused sign in after start-up logs an error every run and fails the run."""
    component = _make_component()
    run_async(component.run(0, True))
    for seconds in (60, 120):
        component.transport.due = True
        component.transport.errors["authenticate"] = HypervoltAuthError("refused")
        component.log_messages.clear()
        assert run_async(component.run(seconds, False)) is False
        assert len([message for message in component.log_messages if message.startswith("Error: hypervolt:")]) == 1
    print("  ✓ Auth failure after start-up fails the run")


def test_publishes_one_charger():
    """One charger publishes exactly the spec's entities, with the charger name in each friendly name."""
    component = _make_component()
    assert run_async(component.run(0, True)) is True
    entities = component.base.entities
    prefix = "predbat_hypervolt_{}".format(V3_ID)
    names = {name for name in entities if "_hypervolt_" in name}
    assert names == {
        "sensor.{}_status".format(prefix), "sensor.{}_power".format(prefix), "sensor.{}_session_energy".format(prefix),
        "binary_sensor.{}_connected".format(prefix), "binary_sensor.{}_plugged".format(prefix), "binary_sensor.{}_charging".format(prefix),
        "switch.{}_charging".format(prefix), "number.{}_max_charging_current".format(prefix), "select.{}_charge_mode".format(prefix),
    }, names
    status = entities["sensor.{}_status".format(prefix)]
    assert status["state"] == "charging"
    for key, value in (("model", "Home 3 Pro"), ("name", "Driveway"), ("api_version", 3), ("release_state", "default"), ("pilot_status", "C"), ("lock_state", "unlocked"), ("stale", False)):
        assert status["attributes"][key] == value, (key, status["attributes"])
    assert entities["sensor.{}_power".format(prefix)]["state"] == 7200.0
    assert entities["sensor.{}_power".format(prefix)]["attributes"]["unit_of_measurement"] == "W"
    assert entities["sensor.{}_session_energy".format(prefix)]["state"] == 4.5
    assert entities["sensor.{}_session_energy".format(prefix)]["attributes"]["state_class"] == "total_increasing"
    assert entities["binary_sensor.{}_connected".format(prefix)]["state"] == "on"
    assert entities["binary_sensor.{}_plugged".format(prefix)]["state"] == "on"
    assert entities["binary_sensor.{}_charging".format(prefix)]["state"] == "on"
    assert entities["switch.{}_charging".format(prefix)]["state"] == "on"
    number = entities["number.{}_max_charging_current".format(prefix)]
    assert number["state"] == 32 and number["attributes"]["min"] == 6 and number["attributes"]["max"] == 32 and number["attributes"]["step"] == 1
    select = entities["select.{}_charge_mode".format(prefix)]
    assert select["state"] == "boost" and select["attributes"]["options"] == ["boost", "eco", "super_eco"]
    assert "Driveway" in status["attributes"]["friendly_name"] and "Driveway" in number["attributes"]["friendly_name"]
    assert "switch.predbat_hypervolt_control" not in entities
    print("  ✓ One charger publishes the expected entities")


def test_publishes_two_chargers():
    """Each charger gets its own entities, in car order; a released charger's switch is off."""
    other = str(int("b" * 16, 16))
    component = _make_component([HypervoltCharger(other, release_state="released", pilot_status="B", last_message=time.time()), HypervoltCharger(V3_ID, release_state="default", pilot_status="A", last_message=time.time())])
    assert run_async(component.run(0, True)) is True
    entities = component.base.entities
    assert entities["switch.predbat_hypervolt_{}_charging".format(other)]["state"] == "off"
    assert entities["sensor.predbat_hypervolt_{}_status".format(other)]["state"] == "stopped"
    assert entities["binary_sensor.predbat_hypervolt_{}_connected".format(V3_ID)]["state"] == "off"
    assert [charger.charger_id for charger in component.ordered_chargers()] == sorted([V3_ID, other], key=int)
    print("  ✓ Two chargers publish separately")


def test_update_success_only_when_fresh():
    """Success is stamped only while at least one charger is fresh."""
    component = _make_component()
    run_async(component.run(0, True))
    stamped = component.last_success_timestamp
    assert stamped is not None
    component.transport.chargers[V3_ID].last_message = time.time() - 10 * 60
    component.last_success_timestamp = None
    assert run_async(component.run(60, False)) is False
    assert component.last_success_timestamp is None
    assert component.base.entities["sensor.predbat_hypervolt_{}_status".format(V3_ID)]["attributes"]["stale"] is True
    print("  ✓ Success only with fresh data")


def test_final_closes_transport():
    """final() closes the transport."""
    component = _make_component()
    run_async(component.run(0, True))
    run_async(component.final())
    assert component.transport.count("close") == 1
    print("  ✓ final() closes the transport")


def _started_component(chargers=None, **overrides):
    """A component that has completed its first run, with the call log cleared."""
    component = _make_component(chargers, **overrides)
    assert run_async(component.run(0, True)) is True
    component.transport.calls.clear()
    return component


def _requests(component):
    """The commands sent, as (charger id, method, params)."""
    return [call[1:] for call in component.transport.calls if call[0] == "request"]


SWITCH = "switch.predbat_hypervolt_{}_charging".format(V3_ID)
NUMBER = "number.predbat_hypervolt_{}_max_charging_current".format(V3_ID)
SELECT = "select.predbat_hypervolt_{}_charge_mode".format(V3_ID)


def test_controls_queue_rather_than_call():
    """An event is queued and sent on the next run, not from the event handler."""
    component = _started_component()
    run_async(component.switch_event(SWITCH, "turn_off"))
    assert _requests(component) == []
    run_async(component.run(60, False))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})]
    print("  ✓ Controls are queued for the run loop")


def test_switch_charging():
    """The charge switch sends release false to charge and release true to stop."""
    component = _started_component()
    run_async(component.switch_event(SWITCH, "turn_on"))
    run_async(component.switch_event(SWITCH, "turn_off"))
    run_async(component.switch_event(SWITCH, "toggle"))
    run_async(component.run(60, False))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False}), (V3_ID, "sync.apply", {"release": True})]
    print("  ✓ Charge switch")


def test_number_current_clamps_and_ignores_junk():
    """The current is sent in milliamps, clamped to 6-32 A; junk is ignored."""
    component = _started_component()
    for value in ("16", 40, 3, "abc", None):
        run_async(component.number_event(NUMBER, value))
    run_async(component.run(60, False))
    assert _requests(component) == [(V3_ID, "sync.apply", {"max_current": 16000}), (V3_ID, "sync.apply", {"max_current": 32000}), (V3_ID, "sync.apply", {"max_current": 6000})]
    assert _logged(component, "ignoring charging current 'abc'")
    print("  ✓ Current is clamped and junk ignored")


def test_select_charge_mode_by_api_version():
    """The mode goes by plugncharge.set on the V3 API and sync.apply on the V2 API; unknown or unsupported modes are ignored."""
    component = _started_component()
    for value in ("eco", "turbo", "super_eco_battery_safe"):
        run_async(component.select_event(SELECT, value))
    run_async(component.run(60, False))
    assert _requests(component) == [(V3_ID, "plugncharge.set", {"mode": "eco"})]

    component = _started_component([HypervoltCharger(V2_ID, api_version=2, features=["home-battery-drain-prevention"], last_message=time.time())])
    run_async(component.select_event("select.predbat_hypervolt_{}_charge_mode".format(V2_ID), "super_eco_battery_safe"))
    run_async(component.run(60, False))
    assert _requests(component) == [(V2_ID, "sync.apply", {"solar_mode": "super_eco_battery_safe"})]
    print("  ✓ Charge mode by API version")


def test_control_refused_logged_with_reason():
    """A refused command is logged with Hypervolt's reason and the run still succeeds."""
    component = _started_component()
    component.transport.errors["request"] = HypervoltRefusedError("charger refused sync.apply: 409 Concurrent modifications invalidated this request")
    run_async(component.switch_event(SWITCH, "turn_off"))
    assert run_async(component.run(60, False)) is True
    assert any(message.startswith("Warn: hypervolt:") and "409 Concurrent" in message for message in component.log_messages)
    assert component.queued_events == []
    print("  ✓ Refused command logged with the reason")


def test_charger_for_entity_requires_whole_id():
    """A charger id that is a prefix of another does not claim its entities."""
    component = _started_component([HypervoltCharger("12", last_message=time.time()), HypervoltCharger("123", last_message=time.time())])
    assert component.charger_for_entity("switch.predbat_hypervolt_123_charging").charger_id == "123"
    assert component.charger_for_entity("switch.predbat_hypervolt_12_charging").charger_id == "12"
    assert component.charger_for_entity("switch.predbat_hypervolt_1_charging") is None
    print("  ✓ Entities match whole charger ids")


OTHER_ID = str(int("b" * 16, 16))


def _two_chargers():
    """Two fresh V3 chargers, listed in reverse id order."""
    return [HypervoltCharger(OTHER_ID, api_version=3, last_message=time.time()), HypervoltCharger(V3_ID, api_version=3, last_message=time.time())]


def test_automatic_config_two_chargers():
    """Each argument is a per-car list in charger id order, and num_cars is raised to match."""
    component = _make_component(_two_chargers())
    run_async(component.run(0, True))
    args = component.base.args
    first, second = sorted([V3_ID, OTHER_ID], key=int)
    for key, kind, suffix in (("car_charging_energy", "sensor", "session_energy"), ("car_charging_planned", "binary_sensor", "connected"), ("car_charging_plugged", "binary_sensor", "plugged"), ("car_charging_power", "sensor", "power"), ("car_charging_now", "sensor", "power")):
        assert args[key] == ["{}.predbat_hypervolt_{}_{}".format(kind, first, suffix), "{}.predbat_hypervolt_{}_{}".format(kind, second, suffix)], (key, args[key])
    assert args["num_cars"] == 2
    assert "car_charging_soc" not in args
    print("  ✓ Automatic configuration wires per-car lists")


def test_automatic_config_keeps_user_values():
    """A user's car_charging_now is kept, and a larger num_cars is not reduced."""
    component = _make_component()
    component.base.args_from_apps_yaml = {"car_charging_now": ["binary_sensor.my_car_charging"]}
    component.base.apps_yaml_override_warned = set()
    component.base.args["car_charging_now"] = ["binary_sensor.my_car_charging"]
    component.base.args["num_cars"] = 3
    run_async(component.run(0, True))
    assert component.base.args["car_charging_now"] == ["binary_sensor.my_car_charging"]
    assert component.base.args["num_cars"] == 3
    print("  ✓ User values kept")


def test_automatic_config_disabled():
    """With hypervolt_automatic off nothing is wired."""
    component = _make_component(automatic=False)
    run_async(component.run(0, True))
    assert "car_charging_energy" not in component.base.args and "num_cars" not in component.base.args
    print("  ✓ Automatic configuration off wires nothing")


def test_automatic_config_runs_once_per_order():
    """A later run does not put a changed value back; a charger added later wires once more."""
    component = _make_component()
    run_async(component.run(0, True))
    component.base.args["car_charging_energy"] = ["sensor.something_else"]
    run_async(component.run(60, False))
    assert component.base.args["car_charging_energy"] == ["sensor.something_else"]
    component.transport.chargers[OTHER_ID] = HypervoltCharger(OTHER_ID, last_message=time.time())
    component.transport.listed.append(OTHER_ID)
    run_async(component.run(CHARGER_LIST_SECONDS, False))
    assert len(component.base.args["car_charging_energy"]) == 2 and component.base.args["num_cars"] == 2
    print("  ✓ Automatic configuration runs once per car order")


def test_automatic_config_api_2_info():
    """A V2 API charger is wired, with one Info line pointing at the plug-in guidance."""
    component = _make_component([HypervoltCharger(V2_ID, api_version=2, last_message=time.time())])
    run_async(component.run(0, True))
    run_async(component.run(60, False))
    infos = [message for message in component.log_messages if message.startswith("Info: hypervolt:") and "docs/car-charging.md" in message]
    assert len(infos) == 1, component.log_messages
    assert component.base.args["car_charging_planned"] == ["binary_sensor.predbat_hypervolt_{}_connected".format(V2_ID)]
    print("  ✓ V2 API charger wired with a pointer to the guidance")


CONTROL_TZ = pytz.timezone("Europe/London")
CONTROL_NOW = CONTROL_TZ.localize(datetime.datetime(2026, 10, 5, 23, 30))


def _plan_window(start, end):
    """Build one planned-window dict in the shape output.py publishes."""
    return {"start": start.strftime("%m-%d %H:%M:%S"), "end": end.strftime("%m-%d %H:%M:%S")}


# CONTROL_NOW is inside this window...
PLAN_INSIDE = [_plan_window(datetime.datetime(2026, 10, 5, 23, 0), datetime.datetime(2026, 10, 6, 1, 0))]
# ...and before this one
PLAN_OUTSIDE = [_plan_window(datetime.datetime(2026, 10, 6, 2, 0), datetime.datetime(2026, 10, 6, 4, 0))]


class _Storage:
    """Minimal in-memory stand-in for the Storage component; saves go into a shared log when one is given."""

    def __init__(self, log=None):
        """Start empty."""
        self.data = {}
        self.log = log
        self.fail_loads = 0

    async def save(self, module, filename, data, **kwargs):
        """Store a document."""
        if self.log is not None:
            self.log.append(("save", data))
        self.data[(module, filename)] = data

    async def load(self, module, filename):
        """Return a stored document, or None; raise while fail_loads is above zero."""
        if self.fail_loads > 0:
            self.fail_loads -= 1
            raise OSError("storage not ready")
        return self.data.get((module, filename))


class _Components:
    """Stand-in for the component registry, serving only the storage component."""

    def __init__(self, storage):
        """Hold the storage stand-in to serve."""
        self.storage = storage

    def get_component(self, name):
        """Return the storage stand-in, and nothing else."""
        return self.storage if name == "storage" else None


def _charger(charger_id=V3_ID, **fields):
    """A fresh V3 charger record with the given fields."""
    fields.setdefault("api_version", 3)
    fields.setdefault("last_message", time.time())
    return HypervoltCharger(charger_id, **fields)


def _control_component(plans, chargers=None, storage=None, **overrides):
    """A component with control on, chargers loaded and car plans published; tests call control_tick() with a fixed clock."""
    overrides.setdefault("hypervolt_control", True)
    component = _make_component(chargers if chargers is not None else [_charger(release_state="default", pilot_status="B")], **overrides)
    component.local_tz = CONTROL_TZ
    component.base.local_tz = CONTROL_TZ
    if storage is not None:
        component.base.components = _Components(storage)
    for car_n, windows in plans.items():
        postfix = "" if car_n == 0 else "_{}".format(car_n)
        component.base.set_state_wrapper("binary_sensor.predbat_car_charging_slot" + postfix, "off", {"planned": windows})
    run_async(component.load_control_state())
    component.enable_control()
    component.update_car_order(component.transport.chargers)
    component.transport.calls = []
    return component


def test_control_needs_automatic():
    """Control stays off, with a warning, unless automatic configuration is on."""
    assert _control_component({}).charger_control_active is True
    without_auto = _control_component({}, automatic=False)
    assert without_auto.charger_control_active is False and _logged(without_auto, "needs hypervolt_automatic")
    assert _control_component({}, hypervolt_control=False).charger_control_active is False
    print("  ✓ Control needs hypervolt_control and hypervolt_automatic")


def test_control_starts_inside_window():
    """A released charger inside its window is let charge, even with no pilot status; not once unplugged."""
    for pilot in ("B", None):
        component = _control_component({0: PLAN_INSIDE}, [_charger(release_state="released", pilot_status=pilot)])
        component.released_by_predbat.add(V3_ID)
        run_async(component.control_tick(CONTROL_NOW))
        assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})], pilot
        assert V3_ID not in component.released_by_predbat
    component = _control_component({0: PLAN_INSIDE}, [_charger(release_state="released", pilot_status="A")])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == []
    print("  ✓ Released charger started inside a window")


def test_control_stops_outside_window():
    """A charging charger outside its window is recorded and saved before it is stopped."""
    log = []
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)], storage=_Storage(log))
    component.transport.calls = log
    run_async(component.control_tick(CONTROL_NOW))
    kinds = [entry[0] for entry in log]
    assert "save" in kinds and kinds.index("save") < kinds.index("request"), log
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})]
    saved = [entry[1] for entry in log if entry[0] == "save"][0]
    assert saved["released"] == [V3_ID]
    print("  ✓ Charger stopped outside a window, recorded first")


def test_control_stops_waiting_car():
    """A car plugged in but not yet charging is stopped outside a window too."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="B", charging=False)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})]
    print("  ✓ Waiting car stopped outside a window")


def test_control_waits_for_pilot_status():
    """With no pilot status and no charge, nothing is stopped and a recorded charger is not forgotten."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status=None)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == []
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status=None)])
    component.released_by_predbat.add(V3_ID)
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [] and V3_ID in component.released_by_predbat
    print("  ✓ Waits for the pilot status")


def test_control_forgets_unplugged():
    """A recorded charger that reports unplugged, and is no longer stopped, is forgotten and sent nothing."""
    component = _control_component({0: PLAN_INSIDE}, [_charger(release_state="default", pilot_status="A")])
    component.released_by_predbat.add(V3_ID)
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [] and component.released_by_predbat == set()
    print("  ✓ Unplugged charger forgotten")


def test_control_skips_stale_and_unplanned():
    """A stale charger, and a car with no slot sensor, are left alone."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True, last_message=time.time() - 600)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == []
    component = _control_component({}, [_charger(release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == []
    print("  ✓ Stale and unplanned chargers left alone")


def test_control_is_per_car():
    """The second charger follows car 1's slot sensor."""
    first, second = sorted([V3_ID, OTHER_ID], key=int)
    component = _control_component({0: PLAN_INSIDE, 1: PLAN_OUTSIDE}, [_charger(first, release_state="default", pilot_status="C", charging=True), _charger(second, release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(second, "sync.apply", {"release": True})]
    print("  ✓ Control is per car")


def test_control_one_command_per_run():
    """One control cycle sends a charger at most one command."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    assert len(_requests(component)) == 1
    print("  ✓ One command per run")


def test_control_releases_on_read_only_and_switch_off():
    """Read-only mode and the switch turned off each release only the chargers Predbat stopped."""
    for setup in ("read_only", "switch_off"):
        storage = _Storage()
        other = _charger(OTHER_ID, release_state="released", pilot_status="B")
        component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B"), other], storage=storage)
        component.released_by_predbat.add(V3_ID)
        if setup == "read_only":
            component.base.args["set_read_only"] = True
        else:
            component.charger_control_enabled = False
        run_async(component.control_tick(CONTROL_NOW))
        assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})], setup
        assert component.released_by_predbat == set()
        assert storage.data[("hypervolt", "control_state")]["released"] == []
    print("  ✓ Release on read-only and switch off")


def test_control_state_survives_restart():
    """A new component with the same storage releases a charger an earlier one stopped, with control now off."""
    storage = _Storage()
    first = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)], storage=storage)
    run_async(first.control_tick(CONTROL_NOW))
    assert storage.data[("hypervolt", "control_state")]["released"] == [V3_ID]
    second = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B")], storage=storage, hypervolt_control=False)
    run_async(second.control_tick(CONTROL_NOW))
    assert _requests(second) == [(V3_ID, "sync.apply", {"release": False})]
    print("  ✓ Control state survives a restart")


def test_control_switch_published_only_when_available():
    """The control switch is published only while control can act."""
    component = _control_component({})
    run_async(component.publish_data())
    assert component.base.entities["switch.predbat_hypervolt_control"]["state"] == "on"
    component = _control_component({}, hypervolt_control=False)
    run_async(component.publish_data())
    assert "switch.predbat_hypervolt_control" not in component.base.entities
    print("  ✓ Control switch published only when available")


def test_control_switch_state_survives_restart():
    """Turning the control switch off is saved and loaded by the next component."""
    storage = _Storage()
    component = _control_component({}, storage=storage)
    run_async(component.switch_event_handler("switch.predbat_hypervolt_control", "turn_off"))
    assert component.charger_control_enabled is False
    again = _control_component({}, storage=storage)
    assert again.charger_control_enabled is False
    print("  ✓ Control switch state survives a restart")


def test_control_without_storage_still_works():
    """Without a Storage component, control still runs."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)])
    component.base.components = None
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})]
    print("  ✓ Control works without storage")


def test_control_state_load_is_retried():
    """A failed read of the saved state is tried again, and merged rather than replacing what is known."""
    storage = _Storage()
    storage.data[("hypervolt", "control_state")] = {"control_enabled": False, "released": [OTHER_ID]}
    storage.fail_loads = 1
    component = _make_component(hypervolt_control=True)
    component.base.components = _Components(storage)
    component.released_by_predbat.add(V3_ID)
    run_async(component.load_control_state())
    assert component.charger_control_enabled is True and component.control_state_loaded is False
    run_async(component.load_control_state())
    assert component.charger_control_enabled is False and component.released_by_predbat == {V3_ID, OTHER_ID}
    print("  ✓ Control state load is retried and merged")


def test_control_failure_does_not_fail_run():
    """A refused control command warns once and the run still succeeds."""
    component = _make_component([_charger(release_state="default", pilot_status="C", charging=True)], hypervolt_control=True)
    component.local_tz = CONTROL_TZ
    component.base.local_tz = CONTROL_TZ
    component.base.set_state_wrapper("binary_sensor.predbat_car_charging_slot", "off", {"planned": PLAN_OUTSIDE})
    with patch.object(HypervoltAPI, "now_utc_exact", new=CONTROL_NOW, create=True):
        component.transport.errors["request"] = HypervoltRefusedError("409 busy")
        assert run_async(component.run(0, True)) is True
        component.transport.errors["request"] = HypervoltRefusedError("409 busy")
        assert run_async(component.run(60, False)) is True
    assert len([message for message in component.log_messages if "409 busy" in message]) == 1
    print("  ✓ A control failure does not fail the run")


def test_released_charger_reads_connected_without_pilot():
    """A charger Predbat stopped, with no pilot status, still reads connected, so its car keeps a plan and can be started."""
    component = _make_component([_charger(release_state="released", pilot_status=None, charging=False)])
    component.released_by_predbat.add(V3_ID)
    run_async(component.run(0, True))
    entities = component.base.entities
    assert entities["binary_sensor.predbat_hypervolt_{}_connected".format(V3_ID)]["state"] == "on"
    assert entities["binary_sensor.predbat_hypervolt_{}_plugged".format(V3_ID)]["state"] == "unavailable"
    other = _make_component([_charger(release_state="released", pilot_status=None, charging=False)])
    run_async(other.run(0, True))
    assert other.base.entities["binary_sensor.predbat_hypervolt_{}_connected".format(V3_ID)]["state"] == "unavailable"
    print("  ✓ A charger Predbat stopped reads connected without a pilot status")


DISPATCH = "binary_sensor.octopus_energy_intelligent_dispatching"
# CONTROL_NOW (23:30 BST) is inside this dispatch
DISPATCH_NOW = [{"start": "2026-10-05T22:00:00+0000", "end": "2026-10-05T23:00:00+0000", "charge_in_kwh": 7.0}]


def _octopus_control_component(is_charger, plans, chargers, dispatches=None, follows_car=False):
    """A control component whose car 0 is on Octopus Intelligent, with a dispatch sensor saying whether Octopus drives the charger."""
    component = _control_component(plans, chargers)
    component.base.args["octopus_intelligent_slot"] = [DISPATCH]
    component.base.args["octopus_intelligent_charger_follows_car"] = follows_car
    attributes = {"planned_dispatches": dispatches or []}
    if is_charger is not None:
        attributes["is_charger"] = is_charger
    component.base.set_state_wrapper(DISPATCH, "on" if dispatches else "off", attributes)
    return component


def test_octopus_drives_car_leaves_charger_alone():
    """Octopus drives the car: by default the charger is left alone outside a window, with one Info line naming the switch."""
    component = _octopus_control_component(False, {0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == []
    assert len([message for message in component.log_messages if "drives car 0 itself" in message and "charger_follows_car" in message]) == 1, component.log_messages
    print("  ✓ Octopus drives the car: charger left alone")


def test_octopus_drives_car_follows_dispatches():
    """With octopus_intelligent_charger_follows_car on, the charger is stopped outside a dispatch and let charge inside one."""
    component = _octopus_control_component(False, {0: []}, [_charger(release_state="default", pilot_status="B")], follows_car=True)
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})]
    assert component.released_by_predbat == {V3_ID}
    component.transport.chargers[V3_ID].release_state = "released"
    component.transport.calls = []
    component.base.set_state_wrapper(DISPATCH, "on", {"is_charger": False, "planned_dispatches": DISPATCH_NOW})
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    assert component.released_by_predbat == set()
    print("  ✓ Octopus drives the car: charger follows the dispatches when asked")


def test_octopus_drives_charger_hands_it_back():
    """Octopus drives the charger: Predbat sends nothing, and a charger it had stopped is let charge and forgotten."""
    component = _octopus_control_component(True, {0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == []
    component = _octopus_control_component(True, {0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B")])
    component.released_by_predbat.add(V3_ID)
    run_async(component.control_tick(CONTROL_NOW))
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    assert component.released_by_predbat == set() and V3_ID not in component.charger_control_state
    print("  ✓ Octopus drives the charger: handed back")


def test_octopus_cannot_tell_drives_charger_with_control_true():
    """A dispatch sensor that does not say which device Octopus drives: hypervolt_control true means the charger is not it."""
    component = _octopus_control_component(None, {0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})]
    print("  ✓ Cannot tell, with hypervolt_control true: driven")


def test_guest_charging_releases_and_publishes():
    """The guest charging switch is published with control, and turning it on releases a charger Predbat stopped."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B")])
    component.released_by_predbat.add(V3_ID)
    run_async(component.publish_data())
    assert component.base.entities["switch.predbat_hypervolt_guest_charging"]["state"] == "off"
    run_async(component.switch_event_handler("switch.predbat_hypervolt_guest_charging", "turn_on"))
    assert component.charger_control_guest is True
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    assert component.released_by_predbat == set()
    print("  ✓ Guest charging releases the charger")


def test_drift_is_reapplied():
    """A charger Predbat stopped that is let charge from the app outside a window is stopped again."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="C", charging=True)])
    run_async(component.control_tick(CONTROL_NOW))
    component.transport.chargers[V3_ID].release_state = "released"
    run_async(component.control_tick(CONTROL_NOW))
    assert len(_requests(component)) == 1
    component.transport.chargers[V3_ID].release_state = "default"
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": True})] * 2
    print("  ✓ A charger changed from the app outside a window is stopped again")


def test_release_retried_after_stale_charger():
    """A charger that is stale when guest charging releases is not dropped: once it is heard from, it is released."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B", last_message=time.time() - 600)])
    component.released_by_predbat.add(V3_ID)
    run_async(component.switch_event_handler("switch.predbat_hypervolt_guest_charging", "turn_on"))
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [] and component.released_by_predbat == {V3_ID}
    component.transport.chargers[V3_ID].last_message = time.time()
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    assert component.released_by_predbat == set()
    print("  ✓ A release missed while a charger was stale is retried")


def test_release_with_unknown_state_is_sent():
    """After a restart, a charger owed a release whose state is not reported yet is sent the release, plugged in or not."""
    for pilot in ("B", "A", None):
        component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state=None, pilot_status=pilot)])
        component.released_by_predbat.add(V3_ID)
        component.base.args["set_read_only"] = True
        run_async(component.control_tick(CONTROL_NOW))
        assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})], pilot
        assert component.released_by_predbat == set(), pilot
    print("  ✓ A release is sent when the charger's state is not known yet")


def test_stale_release_is_quiet_and_kept_owed():
    """A charger that stays offline is retried without a line every cycle, and stays owed even if it last read default -
    a stop whose reply was lost may still have landed."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B", last_message=time.time() - 600)])
    component.released_by_predbat.add(V3_ID)
    component.base.args["set_read_only"] = True
    for _ in range(3):
        run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [] and component.released_by_predbat == {V3_ID}
    assert len([message for message in component.log_messages if "releasing the Hypervolt because" in message or "control failed" in message]) <= 1, component.log_messages
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="default", pilot_status="B", last_message=time.time() - 600)])
    component.released_by_predbat.add(V3_ID)
    component.base.args["set_read_only"] = True
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [] and component.released_by_predbat == {V3_ID}
    component.transport.chargers[V3_ID].last_message = time.time()
    component.transport.chargers[V3_ID].release_state = "released"
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})] and component.released_by_predbat == set()
    print("  ✓ An offline charger is retried quietly")


def test_axle_read_only_releases():
    """Axle's VPP read only, which sets only the base flag, releases the chargers too."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B")])
    component.released_by_predbat.add(V3_ID)
    component.base.set_read_only_axle = True
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    print("  ✓ Axle read only releases")


def test_string_error_reply_fails_caller_and_reader_survives():
    """An error that is a bare string fails its caller at once and the reader carries on."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Answer a command with a string error, then push."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        task = asyncio.ensure_future(transport.request(V3_ID, "plugncharge.set", {"mode": "eco"}))
        await _settle()
        socket.queue({"jsonrpc": "2.0", "id": socket.last("plugncharge.set")["id"], "error": "plugncharge.set not allowed"})
        started = time.time()
        try:
            await task
            raise AssertionError("Expected HypervoltRefusedError")
        except HypervoltRefusedError as exc:
            assert "not allowed" in str(exc)
        assert time.time() - started < 1
        socket.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "B"}})
        await _settle()
        assert transport.chargers[V3_ID].pilot_status == "B"
        assert not any(task.done() for task in transport.links[V3_ID].tasks)
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"), patch("hypervolt.COMMAND_TIMEOUT_SECONDS", 5):
        run_async(scenario())
    print("  ✓ String error fails the caller and the reader survives")


def test_reader_survives_a_handler_exception():
    """An unexpected exception handling one frame is logged and the next frame is still read."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Make on_message raise once."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        original = transport.on_message
        state = {"raised": False}

        def _flaky(charger_id, method, body):
            """Raise on the first push only."""
            if not state["raised"]:
                state["raised"] = True
                raise KeyError("boom")
            original(charger_id, method, body)

        transport.on_message = _flaky
        socket.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "A"}})
        socket.queue({"jsonrpc": "2.0", "method": "get.pilot_status", "params": {"pilot_status": "C"}})
        await _settle()
        assert transport.chargers[V3_ID].pilot_status == "C"
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ Reader survives a handler exception")


def test_link_alive_and_dead_link_restarted():
    """link_alive() reports a finished reader, and run() restarts a dead link unless the sign in has failed."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Kill the reader and check link_alive."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        assert transport.link_alive(V3_ID) is True
        for task in transport.links[V3_ID].tasks:
            task.cancel()
        await _settle()
        assert transport.link_alive(V3_ID) is False
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())

    component = _make_component()
    run_async(component.run(0, True))
    component.transport.dead.add(V3_ID)
    run_async(component.run(60, False))
    assert ("reconnect", V3_ID) in component.transport.calls
    component.transport.calls.clear()
    component.transport.auth_failed = True
    component.transport.errors["authenticate"] = HypervoltAuthError("refused")
    run_async(component.run(120, False))
    assert ("reconnect", V3_ID) not in component.transport.calls
    print("  ✓ Dead link detected and restarted")


def test_login_error_reply_is_a_refusal():
    """An error reply to the login is treated as a refused login: the socket is closed and the token renewed."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Answer the login with an error."""
        transport = _signed_in_transport()
        await transport.connect(V3_ID)
        await _settle()
        socket.queue({"jsonrpc": "2.0", "id": socket.last("login")["id"], "error": {"code": 401, "error": "invalid token"}})
        await _settle()
        assert socket.closed and transport.token_expiry == 0
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ Login error reply is a refusal")


def test_request_send_failure_is_api_error():
    """A send on a closing socket raises HypervoltApiError, not a raw connection error."""
    socket = FakeSocket()
    session, _ = _ws_session([socket])

    async def scenario():
        """Make the send fail."""
        transport = _signed_in_transport()
        await _logged_in(transport, socket)
        socket.send_str = AsyncMock(side_effect=ConnectionResetError("closing"))
        try:
            await transport.request(V3_ID, "sync.apply", {"release": True})
            raise AssertionError("Expected HypervoltApiError")
        except HypervoltApiError:
            pass
        assert transport.links[V3_ID].pending == {}
        await transport.close()

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(scenario())
    print("  ✓ Send failure is an API error")


def test_sign_in_backs_off_after_auth_failure():
    """After a refused sign in, attempts back off (logged and failed every run), so the account is not locked out."""
    component = _make_component()
    run_async(component.run(0, True))
    component.transport.due = True
    component.transport.errors["authenticate"] = HypervoltAuthError("refused")
    assert run_async(component.run(60, False)) is False
    assert component.transport.count("authenticate") == 2
    component.log_messages.clear()
    assert run_async(component.run(120, False)) is False
    assert component.transport.count("authenticate") == 2, "No sign in attempt during the back-off"
    assert len([message for message in component.log_messages if message.startswith("Error: hypervolt:")]) == 1
    component.auth_retry_at = 0
    component.transport.errors["authenticate"] = HypervoltAuthError("refused")
    run_async(component.run(180, False))
    assert component.transport.count("authenticate") == 3
    assert component.auth_backoff_seconds == 240, component.auth_backoff_seconds
    component.auth_retry_at = 0
    assert run_async(component.run(240, False)) is True
    assert component.auth_backoff_seconds == 0
    print("  ✓ Sign in backs off after a refusal")


def test_quiet_charger_probed_before_stale():
    """A charger quiet for two and a half minutes is probed while still fresh, so the run still succeeds."""
    component = _make_component([_charger(last_message=time.time() - 150)])
    assert run_async(component.run(0, True)) is True
    assert ("probe", V3_ID) in component.transport.calls
    print("  ✓ Quiet charger probed before it goes stale")


def test_unplugged_released_charger_is_released():
    """A charger Predbat stopped that is unplugged while still stopped is released before it is forgotten."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="A")])
    component.released_by_predbat.add(V3_ID)
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    assert component.released_by_predbat == set()
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="A")], hypervolt_control=False)
    component.released_by_predbat.add(V3_ID)
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    assert component.released_by_predbat == set()
    print("  ✓ Unplugged but still stopped charger is released")


def test_read_only_comes_from_get_arg():
    """Read only is read with get_arg, not from the base attribute that reset() leaves True until the first update_pred()."""
    component = _control_component({0: PLAN_OUTSIDE}, [_charger(release_state="released", pilot_status="B")])
    component.released_by_predbat.add(V3_ID)
    component.base.set_read_only = True
    component.base.args["set_read_only"] = False
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [], "A restart must not release the chargers Predbat stopped"
    assert component.released_by_predbat == {V3_ID}
    component.base.args["set_read_only"] = True
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [(V3_ID, "sync.apply", {"release": False})]
    print("  ✓ Read only comes from get_arg")


def test_refused_stop_does_not_hold_connected():
    """A charger whose stop never took effect does not read connected, and is forgotten inside a window."""
    component = _make_component([_charger(release_state="default", pilot_status=None, charging=False)])
    component.released_by_predbat.add(V3_ID)
    run_async(component.run(0, True))
    assert component.base.entities["binary_sensor.predbat_hypervolt_{}_connected".format(V3_ID)]["state"] == "unavailable"
    component = _control_component({0: PLAN_INSIDE}, [_charger(release_state="default", pilot_status=None)])
    component.released_by_predbat.add(V3_ID)
    run_async(component.control_tick(CONTROL_NOW))
    assert _requests(component) == [] and component.released_by_predbat == set()
    print("  ✓ A stop that never took effect does not hold the charger connected")


def test_refused_sign_in_not_retried_by_readers():
    """After a refused password, readers get the refusal without a request; only an explicit retry signs in again."""
    session, calls = _session([_response({"error": "invalid_grant"}, status=401), _response(_token_payload())])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        for _ in range(2):
            try:
                run_async(transport.authenticate())
                raise AssertionError("Expected HypervoltAuthError")
            except HypervoltAuthError:
                pass
        assert transport.auth_failed is True and len(calls) == 1
        assert run_async(transport.authenticate(retry_refused=True)) == "jwt-1"
    assert len(calls) == 2 and transport.auth_failed is False
    print("  ✓ Readers do not retry a refused sign in")


def test_session_reader_does_not_sign_in():
    """The V2 session socket carries no login, so its reader does not sign in."""
    sync, sess = FakeSocket(), FakeSocket()
    session, connects = _ws_session({"sync": [sync], "session/in-progress": [sess]})

    async def scenario():
        """Connect a V2 charger and count the sign ins."""
        transport = _signed_in_transport()
        transport.authenticate = AsyncMock(return_value="jwt-1")
        await transport.connect(V2_ID)
        await _settle()
        assert len(connects) == 2
        count = transport.authenticate.await_count
        await transport.close()
        return count

    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        count = run_async(scenario())
    assert count == 1, count
    print("  ✓ Session reader does not sign in")


def test_empty_account_reports_started():
    """An account with no chargers yet reports started, as Wallbox does, without stamping a success or failing later runs."""
    component = _make_component([])
    assert run_async(component.run(0, True)) is True
    assert _logged(component, "no chargers were found")
    assert component.last_success_timestamp is None
    assert run_async(component.run(60, False)) is True
    assert not [name for name in component.base.entities if "_hypervolt_" in name]
    print("  ✓ An empty account reports started")


# Discovery catalogue reporter


def _capture_reports(component):
    """Capture every report this component files, in order."""
    component.filed_reports = []
    component.report_discovery = component.filed_reports.append
    return component.filed_reports


def test_discovery_record():
    """A charger keyed by its hardware id, with its model, a stub car and the four bound entities."""
    component = _started_component()
    report = component.build_discovery()
    assert report["automatic"] is True
    charger, car = report["chargers"][0], report["cars"][0]
    assert charger["device_id"] == "hypervolt:{}".format(V3_ID), charger
    assert charger["hardware_ids"] == {"serial": V3_ID}, charger
    assert charger["info"] == {"vendor": "Hypervolt", "model": "Home 3 Pro"}, charger
    assert set(charger["entities"]) == {"car_charging_planned", "car_charging_plugged", "car_charging_energy", "car_charging_power"}, charger["entities"]
    assert charger["entities"]["car_charging_planned"]["entity_id"] == "binary_sensor.predbat_hypervolt_{}_connected".format(V3_ID)
    assert car == {"device_id": "hypervolt:{}:car".format(V3_ID), "charged_by": "hypervolt:{}".format(V3_ID), "flags": ["stub"]}, car
    print("  ✓ Discovery reports a Hypervolt charger with a stub car")


def test_discovery_firmware_reported_when_known():
    """The firmware version is reported once the charger has sent it."""
    component = _started_component()
    component.transport.chargers[V3_ID].firmware_version = "1.2.3"
    assert component.build_discovery()["chargers"][0]["info"]["firmware"] == "1.2.3"
    print("  ✓ Discovery reports the firmware version")


def test_discovery_name_never_reported():
    """The user-chosen charger name never reaches the report."""
    component = _started_component()
    assert component.transport.chargers[V3_ID].name == "Driveway"
    assert "Driveway" not in str(component.build_discovery())
    print("  ✓ Discovery never reports the charger name")


def test_discovery_empty_account_files_nothing():
    """Signed in with no charger: nothing to describe, and the cycle still succeeds."""
    component = _make_component([])
    reports = _capture_reports(component)
    assert run_async(component.run(0, True)) is True
    assert component.build_discovery() is None
    assert reports == []
    print("  ✓ Discovery files nothing for an empty account")


def test_discovery_second_charger_refiles():
    """A charger added to the account later is reported after the first, in car order."""
    component = _make_component()
    reports = _capture_reports(component)
    assert run_async(component.run(0, True)) is True
    added = str(int("1" * 16, 16))
    component.transport.chargers[added] = HypervoltCharger(added, model="Home 3 Pro", last_message=time.time())
    component.transport.listed.append(added)
    assert run_async(component.run(CHARGER_LIST_SECONDS, False)) is True
    assert len(reports) == 2, reports
    assert [charger["device_id"] for charger in reports[-1]["chargers"]] == ["hypervolt:{}".format(V3_ID), "hypervolt:{}".format(added)], reports[-1]
    print("  ✓ Discovery re-files when a second charger appears")


def test_discovery_filed_when_chargers_stale():
    """The report is still filed on a cycle that fails because every charger has gone quiet."""
    component = _make_component()
    reports = _capture_reports(component)
    component.transport.chargers[V3_ID].last_message = time.time() - 10 * STALE_SECONDS
    run_async(component.run(0, True))
    assert len(reports) == 1, "a quiet charger is still hardware worth describing"
    print("  ✓ Discovery is filed even when the chargers are stale")


def test_discovery_round_trip():
    """Through the real Coordinator and Redactor: entities and the stub flag survive, the hardware id stays readable."""
    from coordinator import Coordinator

    component = _started_component()
    logs = []
    coordinator = Coordinator(MockBase())
    coordinator.log = logs.append
    coordinator.report("hypervolt", component.build_discovery())
    cleaned = coordinator.reports["hypervolt"]
    assert len(cleaned["chargers"][0]["entities"]) == 4, cleaned
    assert cleaned["cars"][0]["flags"] == ["stub"], cleaned
    assert not [line for line in logs if line.startswith("Warn")], logs
    coordinator.assemble()
    assert V3_ID in str(coordinator.catalogue()), "the charger id is hardware, and stays readable"
    print("  ✓ Discovery report round-trips through the Coordinator and Redactor")


def test_hypervolt(my_predbat=None):
    """Run every Hypervolt test."""
    print("=" * 70)
    print("Hypervolt tests")
    print("=" * 70)
    test_api_version_from_id()
    test_sign_in()
    test_token_reused_then_refreshed()
    test_refresh_rejected_then_sign_in()
    test_bad_credentials()
    test_list_chargers()
    test_sync_login_and_state_requests()
    test_login_refused()
    test_request_reply()
    test_request_error_reply()
    test_request_error_reply_v3_shape()
    test_request_timeout()
    test_late_reply_is_ignored()
    test_reader_skips_bad_json()
    test_replies_and_pushes_reach_on_message()
    test_raw_log_redacts_token()
    test_close_cancels_readers()
    test_apply_snapshot()
    test_apply_snapshot_ignores_bad_items()
    test_apply_session_power()
    test_session_energy_holds_and_resets()
    test_session_energy_v3_without_session_id()
    test_apply_pilot()
    test_connected_state_unknown_until_pilot()
    test_apply_plugncharge()
    test_status_text()
    test_charge_mode_options()
    test_captured_payloads()
    test_captured_v3_payloads()
    test_on_message_updates_charger()
    test_backoff_sequence()
    test_reconnects_with_backoff()
    test_drop_fails_pending_commands()
    test_reconnect_stops_on_auth_failure()
    test_successful_sign_in_clears_auth_failed()
    test_session_socket_only_for_api_2()
    test_promotion_to_api_3()
    test_reconnect_all_uses_new_token()
    test_probe_and_reconnect_one()
    test_command_builders()
    test_component_registration()
    test_component_missing_credentials()
    test_run_first_signs_in_lists_and_connects()
    test_run_refreshes_token()
    test_run_restarts_readers_after_auth_recovery()
    test_short_lived_token_not_due()
    test_concurrent_authenticate_makes_one_request()
    test_run_probes_then_reconnects_stale()
    test_run_lists_chargers_every_30_minutes()
    test_run_auth_failure_after_start()
    test_publishes_one_charger()
    test_publishes_two_chargers()
    test_update_success_only_when_fresh()
    test_final_closes_transport()
    test_controls_queue_rather_than_call()
    test_switch_charging()
    test_number_current_clamps_and_ignores_junk()
    test_select_charge_mode_by_api_version()
    test_control_refused_logged_with_reason()
    test_charger_for_entity_requires_whole_id()
    test_automatic_config_two_chargers()
    test_automatic_config_keeps_user_values()
    test_automatic_config_disabled()
    test_automatic_config_runs_once_per_order()
    test_automatic_config_api_2_info()
    test_control_needs_automatic()
    test_control_starts_inside_window()
    test_control_stops_outside_window()
    test_control_stops_waiting_car()
    test_control_waits_for_pilot_status()
    test_control_forgets_unplugged()
    test_control_skips_stale_and_unplanned()
    test_control_is_per_car()
    test_control_one_command_per_run()
    test_control_releases_on_read_only_and_switch_off()
    test_control_state_survives_restart()
    test_control_switch_published_only_when_available()
    test_control_switch_state_survives_restart()
    test_control_without_storage_still_works()
    test_control_state_load_is_retried()
    test_control_failure_does_not_fail_run()
    test_released_charger_reads_connected_without_pilot()
    test_octopus_drives_car_leaves_charger_alone()
    test_octopus_drives_car_follows_dispatches()
    test_octopus_drives_charger_hands_it_back()
    test_octopus_cannot_tell_drives_charger_with_control_true()
    test_guest_charging_releases_and_publishes()
    test_drift_is_reapplied()
    test_release_retried_after_stale_charger()
    test_release_with_unknown_state_is_sent()
    test_stale_release_is_quiet_and_kept_owed()
    test_axle_read_only_releases()
    test_string_error_reply_fails_caller_and_reader_survives()
    test_reader_survives_a_handler_exception()
    test_link_alive_and_dead_link_restarted()
    test_login_error_reply_is_a_refusal()
    test_request_send_failure_is_api_error()
    test_sign_in_backs_off_after_auth_failure()
    test_quiet_charger_probed_before_stale()
    test_unplugged_released_charger_is_released()
    test_read_only_comes_from_get_arg()
    test_refused_stop_does_not_hold_connected()
    test_refused_sign_in_not_retried_by_readers()
    test_session_reader_does_not_sign_in()
    test_empty_account_reports_started()
    test_discovery_record()
    test_discovery_firmware_reported_when_known()
    test_discovery_name_never_reported()
    test_discovery_empty_account_files_nothing()
    test_discovery_second_charger_refiles()
    test_discovery_filed_when_chargers_stale()
    test_discovery_round_trip()
    print("=" * 70)
    return False
