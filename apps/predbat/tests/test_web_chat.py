# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the Chat tab's routes and its server-sent event stream.

Routes are asserted against a bare aiohttp Application rather than a listening server - building
one performs no network I/O, which is the same trick test_web_annual.py uses.
"""

import asyncio
import json

from aiohttp import web as aiohttp_web

import web_chat
from web import WebInterface
from web_chat import WebChat, format_sse_event


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing the query and JSON body a handler reads."""

    def __init__(self, query=None, body=None):
        """Hold the query string and JSON body this request should present."""
        self.query = query or {}
        self._body = body or {}

    async def json(self):
        """Return the decoded JSON body."""
        return self._body


def _make_web(my_predbat, agent=None):
    """Build a WebInterface bound to my_predbat without standing up the aiohttp app."""
    interface = WebInterface.__new__(WebInterface)
    interface.base = my_predbat
    interface.log = my_predbat.log
    interface.prefix = my_predbat.prefix
    interface.registered_endpoints = []
    interface.chat_page = WebChat(interface)
    interface.chat_page.agent_override = agent
    return interface


def test_routes_registered_only_when_enabled(my_predbat):
    """The chat routes and nav link appear only when the component is configured."""
    failed = False
    print("**** Testing chat route registration ****")

    class NoChat:
        """A components registry with no chat component."""

        def get_component(self, name):
            """Return nothing for every component."""
            return None

    class WithChat:
        """A components registry that has a chat component."""

        def get_component(self, name):
            """Return a stand-in component for 'chat' only."""
            return object() if name == "chat" else None

    original_components = getattr(my_predbat, "components", None)
    try:
        interface = _make_web(my_predbat)
        my_predbat.components = NoChat()
        if interface.chat_enabled():
            print("ERROR: chat reported as enabled with no component")
            failed = True
        app = aiohttp_web.Application()
        interface._register_chat_routes(app)
        if [route for route in app.router.routes() if "/chat" in str(route.resource)]:
            print("ERROR: chat routes were registered with no component")
            failed = True

        my_predbat.components = WithChat()
        if not interface.chat_enabled():
            print("ERROR: chat reported as disabled with a component present")
            failed = True
        app = aiohttp_web.Application()
        interface._register_chat_routes(app)
        paths = {str(route.resource.canonical) for route in app.router.routes()}
        for expected in ["/chat", "/chat/conversations", "/chat/history", "/chat/send", "/chat/stream", "/chat/confirm", "/chat/cancel", "/chat/delete", "/chat/rename", "/chat/models", "/chat/model"]:
            if expected not in paths:
                print("ERROR: route {} was not registered, got {}".format(expected, sorted(paths)))
                failed = True
    finally:
        my_predbat.components = original_components

    return failed


def test_send_is_busy_and_unknown_is_404(my_predbat):
    """Sending during a turn returns 409 naming the busy conversation; unknown ids 404."""
    failed = False
    print("**** Testing chat send busy and unknown conversation handling ****")

    from chat import ChatBusyError

    class BusyAgent:
        """An agent stand-in that is always mid-turn."""

        active = {"conversation_id": "aaaabbbbccccdddd", "turn_id": 3, "title": "why is it charging at 3am"}

        def submit_turn(self, conversation, message):
            """Refuse the handoff the way a busy component would."""
            raise ChatBusyError("A reply is already in progress in 'why is it charging at 3am'")

        class store:
            """A conversation store stand-in that knows one conversation."""

            @staticmethod
            def get_meta(cid):
                """Resolve only the known conversation id."""
                return {"id": cid, "title": "known"} if cid == "aaaabbbbccccdddd" else None

    interface = _make_web(my_predbat, agent=BusyAgent())
    page = interface.chat_page

    response = asyncio.run(page.html_chat_send(FakeRequest(body={"conversation": "aaaabbbbccccdddd", "message": "hello"})))
    if response.status != 409:
        print("ERROR: sending during a turn returned {}, expected 409".format(response.status))
        failed = True
    elif "why is it charging at 3am" not in response.text:
        print("ERROR: the 409 body does not name the busy conversation: {}".format(response.text))
        failed = True

    for handler, payload in [(page.html_chat_send, {"conversation": "ffffffffffffffff", "message": "x"}), (page.html_chat_delete, {"id": "ffffffffffffffff"}), (page.html_chat_rename, {"id": "ffffffffffffffff", "title": "x"})]:
        result = asyncio.run(handler(FakeRequest(body=payload)))
        if result.status != 404:
            print("ERROR: {} returned {} for an unknown id, expected 404".format(handler.__name__, result.status))
            failed = True

    history = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": "ffffffffffffffff"})))
    if history.status != 404:
        print("ERROR: history for an unknown conversation returned {}, expected 404".format(history.status))
        failed = True

    return failed


def test_delete_refuses_the_active_conversation(my_predbat):
    """The conversation with the running turn cannot be deleted out from under it."""
    failed = False
    print("**** Testing delete of the active conversation ****")

    class BusyAgent:
        """An agent stand-in mid-turn in a known conversation."""

        active = {"conversation_id": "aaaabbbbccccdddd", "turn_id": 1, "title": "busy one"}

        class store:
            """A store stand-in that knows the busy conversation."""

            @staticmethod
            def get_meta(cid):
                """Resolve the busy conversation."""
                return {"id": cid, "title": "busy one"} if cid == "aaaabbbbccccdddd" else None

    page = _make_web(my_predbat, agent=BusyAgent()).chat_page
    response = asyncio.run(page.html_chat_delete(FakeRequest(body={"id": "aaaabbbbccccdddd"})))
    if response.status != 409:
        print("ERROR: deleting the active conversation returned {}, expected 409".format(response.status))
        failed = True
    return failed


def test_history_reads_via_snapshot_not_get_messages(my_predbat):
    """The history route serves a lock-guarded snapshot, never the live messages list.

    get_messages() hands back the store's live list; serialising that to JSON on the web thread
    while the component thread is still appending to it is exactly the race snapshot() exists to
    avoid. Both methods return the same messages here, so asserting only on the returned payload
    would not catch a regression back to get_messages() - the test also asserts on which store
    method was actually called.
    """
    failed = False
    print("**** Testing chat history reads via snapshot(), not get_messages() ****")

    calls = []
    expected_messages = [{"role": "user", "content": "hi"}]

    class RichAgent:
        """An agent stand-in with a store rich enough to exercise the history success path."""

        active = None

        class store:
            """A store stand-in recording which read method the handler actually calls."""

            @staticmethod
            def get_meta(cid):
                """Resolve the one known conversation."""
                return {"id": cid, "title": "known", "model": "test-model", "usage_total": {"cost": 0}} if cid == "aaaabbbbccccdddd" else None

            @staticmethod
            async def snapshot(cid):
                """Record the call and return the expected transcript - the correct code path."""
                calls.append("snapshot")
                return expected_messages

            @staticmethod
            async def get_messages(cid):
                """Record the call - taking this path would be the regression under test."""
                calls.append("get_messages")
                return expected_messages

        @staticmethod
        async def run_on_agent_loop(coro):
            """Await the coroutine inline, standing in for the real cross-loop marshalling."""
            return await coro

        @staticmethod
        def events_since(cursor, conversation_id):
            """Return no events and cursor 0, as a freshly created conversation would."""
            return [], 0, False

    page = _make_web(my_predbat, agent=RichAgent()).chat_page
    response = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": "aaaabbbbccccdddd"})))
    if response.status != 200:
        print("ERROR: history for a known conversation returned {}, expected 200".format(response.status))
        failed = True
    body = json.loads(response.text)
    if body.get("messages") != expected_messages:
        print("ERROR: history did not return the expected messages: {}".format(body.get("messages")))
        failed = True
    if calls != ["snapshot"]:
        print(
            "ERROR: history called {} instead of exactly ['snapshot'] - it must read via snapshot(), never get_messages(), because get_messages() hands back the live list the component thread may still be appending to while this request serialises it to JSON".format(
                calls
            )
        )
        failed = True

    return failed


def test_sse_framing(my_predbat):
    """Events are framed with id/event/data lines and JSON-encoded payloads."""
    failed = False
    print("**** Testing SSE framing ****")
    frame = format_sse_event({"seq": 7, "conversation_id": "abcd", "type": "delta", "data": {"text": "hi\nthere"}})
    if not frame.startswith("id: 7\n"):
        print("ERROR: frame does not start with its id: {!r}".format(frame))
        failed = True
    if "event: delta\n" not in frame:
        print("ERROR: frame is missing its event line: {!r}".format(frame))
        failed = True
    if not frame.endswith("\n\n"):
        print("ERROR: frame is not terminated by a blank line: {!r}".format(frame))
        failed = True
    # A JSON-encoded payload is exactly one physical line, so a correctly framed event has
    # precisely four newlines: after "id:", after "event:", after "data:", and the blank
    # terminator line. Splitting on "data: " and taking the first "\n"-delimited segment (as an
    # earlier version of this check did) can never observe a raw newline in the payload - that
    # split necessarily discards it before the membership test runs - so it could never fail
    # regardless of whether the payload was JSON-encoded. Counting newlines catches the real bug.
    if frame.count("\n") != 4:
        print("ERROR: a newline in the payload broke the data line - it must be JSON encoded: {!r}".format(frame))
        failed = True
    return failed


def test_markdown_escapes_before_transforming(my_predbat):
    """The client renderer escapes HTML before applying markdown, for text and for titles."""
    failed = False
    print("**** Testing the client markdown renderer ****")
    script = web_chat.get_chat_script()
    if "function renderMarkdown" not in script:
        print("ERROR: the client script has no renderMarkdown function")
        return True

    escape_index = script.find("function escapeHtml")
    render_index = script.find("function renderMarkdown")
    if escape_index < 0:
        print("ERROR: the client script has no escapeHtml function")
        failed = True
    body = script[render_index : render_index + 800]
    if "escapeHtml(" not in body:
        print("ERROR: renderMarkdown does not escape before transforming - this is the XSS path")
        failed = True
    if "innerHTML" in script and "escapeHtml" not in script:
        print("ERROR: the client script assigns innerHTML without an escape helper")
        failed = True
    for sink in ["setTitleText", "renderMarkdown"]:
        if sink not in script:
            print("ERROR: the client script is missing {}, so titles may be injected raw".format(sink))
            failed = True
    return failed


def run_web_chat_tests(my_predbat):
    """Run every Chat tab web layer test, returning True if any of them failed."""
    failed = False
    failed |= test_routes_registered_only_when_enabled(my_predbat)
    failed |= test_send_is_busy_and_unknown_is_404(my_predbat)
    failed |= test_delete_refuses_the_active_conversation(my_predbat)
    failed |= test_history_reads_via_snapshot_not_get_messages(my_predbat)
    failed |= test_sse_framing(my_predbat)
    failed |= test_markdown_escapes_before_transforming(my_predbat)
    return failed
