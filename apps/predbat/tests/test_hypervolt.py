# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the Hypervolt EV charger integration
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async

from hypervolt import (
    HYPERVOLT_API_URL,
    HYPERVOLT_TOKEN_URL,
    HypervoltApiError,
    HypervoltAuthError,
    HypervoltRefusedError,
    HypervoltTimeoutError,
    HypervoltTransport,
    charger_api_version,
    charger_major_version,
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
    """A mock aiohttp session whose ws_connect() yields the next FakeSocket. Returns (session, connects)."""
    connects = []
    queue = list(sockets)

    def _ws_connect(url, **kwargs):
        """Record the connect and hand out the next socket."""
        connects.append({"url": url, "headers": kwargs.get("headers") or {}})
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
    transport.on_message = lambda charger_id, method, body: transport.seen.append((charger_id, method, body))
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
    """The sync socket logs in with the token, then asks for state; plugncharge only on the V3 API."""
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
    test_request_timeout()
    test_late_reply_is_ignored()
    test_reader_skips_bad_json()
    test_replies_and_pushes_reach_on_message()
    test_raw_log_redacts_token()
    test_close_cancels_readers()
    print("=" * 70)
    return False
