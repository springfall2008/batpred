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
import inspect
import json
import os
import re
import tempfile
import threading
import time

from aiohttp import web as aiohttp_web
from aiohttp.test_utils import make_mocked_request
from ruamel.yaml import YAML

import web_chat
from chat import AgentNotReadyError, PROVIDER_DEFAULT_URLS
from components import Components
from tests.test_chat import _make_agent
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


class _StubSelectionStore:
    """The slice of ConversationStore the /chat/models route touches."""

    def __init__(self, selected=None):
        """Start with nothing selected unless a test says otherwise."""
        self.selected = selected

    def get_selected_model(self, provider=None):
        """Return the remembered model choice for a provider."""
        return self.selected

    def set_selected_model(self, model_id, provider=None):
        """Remember a model choice for a provider."""
        self.selected = model_id or None


def _make_web(my_predbat, agent=None):
    """Build a WebInterface bound to my_predbat without standing up the aiohttp app."""
    interface = WebInterface.__new__(WebInterface)
    interface.base = my_predbat
    interface.log = my_predbat.log
    interface.prefix = my_predbat.prefix
    interface.registered_endpoints = []
    # get_header() reaches for default_page. Needed since /chat renders the setup page rather
    # than 404ing when chat is unconfigured, so the header is built on that path too. arg_errors
    # is a read-only property proxying my_predbat's, so it needs nothing here.
    interface.default_page = "./dash"
    interface.chat_page = WebChat(interface)
    interface.chat_page.agent_override = agent
    return interface


def test_routes_always_registered_handlers_404_unconfigured(my_predbat):
    """The chat routes exist unconditionally; only the handlers gate on chat being configured.

    _register_chat_routes runs during WebInterface.start() in phase 0, before the chat component
    is constructed in phase 1 (see test_chat_routes_survive_the_real_phase_order for the real
    ordering). Gating registration itself on chat_enabled() would freeze the router with the
    routes permanently absent, since aiohttp's router cannot be added to once the site is serving.
    So registration must be unconditional, and each handler carries its own 404 for "not
    configured yet" instead.
    """
    failed = False
    print("**** Testing chat route registration and handler gating ****")

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
        paths = {str(route.resource.canonical) for route in app.router.routes()}
        expected_routes = [
            "/chat",
            "/chat/conversations",
            "/chat/history",
            "/chat/send",
            "/chat/stream",
            "/chat/confirm",
            "/chat/cancel",
            "/chat/delete",
            "/chat/rename",
            "/chat/models",
            "/chat/model",
            "/chat/status",
            "/chat/providers",
            "/chat/providers/models",
            "/chat/provider",
        ]
        for expected in expected_routes:
            if expected not in paths:
                print("ERROR: route {} was not registered with no chat component present, got {}".format(expected, sorted(paths)))
                failed = True

        # /chat is the exception to the 404 rule: a person typing it into a browser gets a page
        # explaining that the component is not running, rather than an error. Configuration is no
        # longer this page's job - a missing provider is handled by the Settings dialog on the
        # normal page - so what it has to explain is that there is nothing to talk to at all. The
        # JSON data routes below still 404, because their caller is the page's own script.
        response = asyncio.run(interface.chat_page.html_chat(FakeRequest()))
        if response.status != 200:
            print("ERROR: html_chat returned {} with no chat component, expected the setup page".format(response.status))
            failed = True
        if "Chat is not running" not in getattr(response, "text", "") or "./log" not in getattr(response, "text", ""):
            print("ERROR: the unconfigured chat page does not explain that the component is not running")
            failed = True

        my_predbat.components = WithChat()
        if not interface.chat_enabled():
            print("ERROR: chat reported as disabled with a component present")
            failed = True
        # Re-registering onto a fresh Application is still fine (a restart would rebuild one),
        # and the set of routes must not depend on whether chat happens to be enabled right now.
        app = aiohttp_web.Application()
        interface._register_chat_routes(app)
        paths = {str(route.resource.canonical) for route in app.router.routes()}
        for expected in expected_routes:
            if expected not in paths:
                print("ERROR: route {} was not registered with a chat component present, got {}".format(expected, sorted(paths)))
                failed = True
    finally:
        my_predbat.components = original_components

    return failed


def test_chat_routes_survive_the_real_phase_order(my_predbat):
    """Routes registered in phase 0 keep working once chat is constructed in phase 1.

    This drives the real boot sequence from predbat.py: Components.initialize(phase=0) then
    start(phase=0) constructs the real WebInterface and (via start()) registers the chat routes
    - _register_chat_routes is called here directly, the same split-out trick test_web_annual.py
    uses, so no real TCP listener is opened. Only afterwards does Components.initialize(phase=1)
    construct the real ChatAgent, mirroring predbat.py's own ordering (components.initialize(1)
    at predbat.py:1839, well after components.start(phase=0) at predbat.py:1812). If route
    registration were still gated on chat_enabled() (Critical 1), /chat would be entirely absent
    from the router built here and nothing done afterwards could add it back.
    """
    failed = False
    print("**** Testing chat routes across the real phase-0-then-phase-1 boot order ****")

    original_components = getattr(my_predbat, "components", None)
    original_args = dict(my_predbat.args)
    try:
        comps = Components(my_predbat)
        my_predbat.components = comps

        # Phase 0: construct the real WebInterface, exactly as predbat.py's startup does.
        comps.initialize(only="web", phase=0)
        interface = comps.get_component("web")
        if interface is None:
            print("ERROR: web component did not construct in phase 0")
            return True

        # The route-registration half of start(), without opening a socket.
        app = aiohttp_web.Application()
        interface._register_chat_routes(app)
        paths = {str(route.resource.canonical) for route in app.router.routes()}
        if "/chat" not in paths:
            print("ERROR: /chat was not registered during the phase-0 step, got {}".format(sorted(paths)))
            failed = True

        # Chat genuinely does not exist yet at this point in a real boot.
        if interface.chat_enabled():
            print("ERROR: chat reported enabled before the chat component exists")
            failed = True
        # /chat serves the setup page rather than 404ing, so a user who lands here before the
        # component exists is told what is happening instead of seeing an error.
        response = asyncio.run(interface.chat_page.html_chat(FakeRequest()))
        if response.status != 200 or "Chat is not running" not in getattr(response, "text", ""):
            print("ERROR: /chat did not serve the setup page before phase 1, got {}".format(response.status))
            failed = True

        # Phase 1: construct the real ChatAgent.
        my_predbat.args["openrouter_api_key"] = "sk-test"
        my_predbat.args["openrouter_default_model"] = "test/model"
        comps.initialize(only="chat", phase=1)
        if comps.get_component("chat") is None:
            print("ERROR: chat component did not construct in phase 1")
            return True

        if not interface.chat_enabled():
            print("ERROR: chat_enabled() is still False after the chat component was constructed")
            failed = True

        # Resolve /chat through the SAME router built during the phase-0 step - a freshly built
        # router would not prove anything survived the phase boundary.
        match = [route for route in app.router.routes() if str(route.resource.canonical) == "/chat" and route.method == "GET"]
        if not match:
            print("ERROR: /chat route is missing from the phase-0 router")
            return True
        response = asyncio.run(match[0].handler(FakeRequest()))
        if response.status != 200:
            body = response.text if hasattr(response, "text") else ""
            print("ERROR: /chat resolved to {} once chat was configured, expected 200: {}".format(response.status, body))
            failed = True
    finally:
        my_predbat.components = original_components
        my_predbat.args.clear()
        my_predbat.args.update(original_args)

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
            def get_approvals(cid):
                """No approvals recorded - the history route reports them beside messages."""
                return []

            @staticmethod
            def get_last_error(cid, message_count=None):
                """No failed turn recorded. The history route reports last_error beside messages."""
                return None

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
    else:
        # The client's Stop button needs this to wire itself up on the send-into-busy path -
        # without it, sending into an already-busy conversation shows a Stop button with no
        # turn_id to post, and can clobber a good one a prior SSE 'busy' event had already set.
        body_json = json.loads(response.text)
        if body_json.get("turn_id") != 3:
            print("ERROR: the 409 body does not carry the active turn's id, got {}".format(body_json))
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
            def get_approvals(cid):
                """No approvals recorded - the history route reports them beside messages."""
                return []

            @staticmethod
            def get_last_error(cid, message_count=None):
                """No failed turn recorded. The history route reports last_error beside messages."""
                return None

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
            def get_approvals(cid):
                """No approvals recorded - the history route reports them beside messages."""
                return []

            @staticmethod
            def get_last_error(cid, message_count=None):
                """No failed turn recorded. The history route reports last_error beside messages."""
                return None

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


def test_history_snapshot_and_cursor_do_not_lose_a_concurrent_message(my_predbat):
    """A message a concurrent turn appends between the snapshot and the cursor read is never lost.

    Reproduces the exact race the final review found: html_chat_history used to take the message
    snapshot on the agent loop via run_on_agent_loop(), then call events_since() separately, back
    on the calling thread. A turn that appended a message and emitted its event in the window
    between those two calls landed in neither: not in the snapshot (already taken) nor after the
    cursor (already past it by the time events_since ran) - a brand new conversation's own first
    message could silently vanish until the user switched away and back.

    agent.events_since is wrapped so that, the instant it is entered, it schedules a concurrent
    turn's append+emit onto the agent's own loop and blocks briefly waiting for it to land before
    computing the cursor - standing in for a second request's turn completing in that window. With
    the two calls still split (mutation-checked below), events_since runs on whichever thread
    called it - here, the same thread driving this test - so the scheduled task lands on the idle
    agent loop well within the wait and the race reproduces reliably. With them combined into one
    coroutine on the agent loop (the fix), events_since now runs FROM the agent loop's own thread,
    so blocking that thread waiting for another task on the very loop it is occupying can never be
    serviced - it times out harmlessly, proving there is no longer a gap to land anything in.
    """
    failed = False
    print("**** Testing chat history does not lose a message concurrent with the snapshot ****")
    agent = _make_agent(my_predbat, turn_timeout=30)
    cid = asyncio.run(agent.store.create())

    # A real background loop - the same pattern test_submit_turn_hands_off_to_the_component_loop
    # uses in test_chat.py - so run_on_agent_loop's cross-thread handoff is genuine, not simulated.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    agent.loop = loop

    async def concurrent_turn():
        """Stand in for a second request's turn landing its first message mid-flight."""
        await agent.store.append(cid, {"role": "user", "content": "concurrent message"})
        agent.emit(cid, "user", {"text": "concurrent message"})

    original_events_since = agent.events_since
    injected = {"tried": False, "landed": False}

    def spying_events_since(cursor, conversation_id):
        """Try to slip a concurrent append into the gap right before the cursor is computed."""
        injected["tried"] = True
        future = asyncio.run_coroutine_threadsafe(concurrent_turn(), loop)
        try:
            future.result(timeout=0.3)
            injected["landed"] = True
        except Exception:
            pass  # Could not land it in time - expected once there is no gap left to land it in.
        return original_events_since(cursor, conversation_id)

    agent.events_since = spying_events_since
    page = _make_web(my_predbat, agent=agent).chat_page

    try:
        response = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": cid})))
    finally:
        agent.events_since = original_events_since
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)

    if response.status != 200:
        print("ERROR: history returned {}, expected 200".format(response.status))
        return True
    if not injected["tried"]:
        print("ERROR: the test's injection point in events_since was never reached")
        return True

    body = json.loads(response.text)
    contents = [message.get("content") for message in body.get("messages", [])]
    message_in_snapshot = "concurrent message" in contents

    # The only unsafe outcome is the one the bug produced: the concurrent append landed before the
    # cursor was computed (so the SSE stream will never redeliver it, being already behind the
    # cursor) AND it is absent from the snapshot returned here - neither channel carries it.
    if injected["landed"] and not message_in_snapshot:
        print("ERROR: the concurrently appended message is in neither the snapshot nor reachable from the returned cursor - it would vanish client-side")
        failed = True

    return failed


def test_history_route_reports_last_prompt_tokens(my_predbat):
    """/chat/history surfaces last_prompt_tokens for the Chat tab's context-size footer, as a
    field distinct from usage_total - which is the cumulative total across every turn. The store
    stand-in reports two different numbers for the two fields (4000 vs usage_total's 5000) so a
    handler that read the wrong one - or fell back to usage_total.prompt_tokens - would fail this
    rather than pass by coincidence.
    """
    failed = False
    print("**** Testing /chat/history reports last_prompt_tokens ****")

    class RichAgent:
        """An agent stand-in whose store reports usage_total and last_prompt_tokens distinctly."""

        active = None

        class store:
            """A store stand-in with a conversation carrying two different usage figures."""

            @staticmethod
            def get_approvals(cid):
                """No approvals recorded - the history route reports them beside messages."""
                return []

            @staticmethod
            def get_last_error(cid, message_count=None):
                """No failed turn recorded. The history route reports last_error beside messages."""
                return None

            @staticmethod
            def get_meta(cid):
                """Resolve the one known conversation, with usage_total and last_prompt_tokens deliberately different."""
                return {"id": cid, "title": "known", "model": "test-model", "usage_total": {"cost": 0.5, "prompt_tokens": 5000}, "last_prompt_tokens": 4000} if cid == "aaaabbbbccccdddd" else None

            @staticmethod
            async def snapshot(cid):
                """Return an empty transcript - this test only cares about the usage fields."""
                return []

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
        print("ERROR: history returned {}, expected 200".format(response.status))
        return True
    body = json.loads(response.text)
    if body.get("last_prompt_tokens") != 4000:
        print("ERROR: /chat/history reported last_prompt_tokens={}, expected 4000 - distinct from usage_total.prompt_tokens=5000, which it must not fall back to".format(body.get("last_prompt_tokens")))
        failed = True
    if body.get("usage_total", {}).get("prompt_tokens") != 5000:
        print("ERROR: usage_total must still be reported as-is: {}".format(body.get("usage_total")))
        failed = True

    # A conversation predating this feature has no last_prompt_tokens key at all - the route must
    # default it to 0 rather than raising or reporting None.
    class LegacyAgent(RichAgent):
        """An agent stand-in whose conversation has no last_prompt_tokens key at all."""

        class store(RichAgent.store):
            """A store stand-in returning a conversation saved before last_prompt_tokens existed."""

            @staticmethod
            def get_approvals(cid):
                """No approvals recorded - the history route reports them beside messages."""
                return []

            @staticmethod
            def get_last_error(cid, message_count=None):
                """No failed turn recorded. The history route reports last_error beside messages."""
                return None

            @staticmethod
            def get_meta(cid):
                """Resolve the one known conversation, with no last_prompt_tokens key at all."""
                return {"id": cid, "title": "legacy", "model": "test-model", "usage_total": {"cost": 0.1}}

    legacy_page = _make_web(my_predbat, agent=LegacyAgent()).chat_page
    legacy_response = asyncio.run(legacy_page.html_chat_history(FakeRequest(query={"conversation": "aaaabbbbccccdddd"})))
    legacy_body = json.loads(legacy_response.text)
    if legacy_body.get("last_prompt_tokens") != 0:
        print("ERROR: a conversation with no stored last_prompt_tokens should default to 0, got {}".format(legacy_body.get("last_prompt_tokens")))
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


def test_markdown_tables_render_as_real_tables_with_scroll_wrapper(my_predbat):
    """A header row plus a valid GFM separator row becomes a real <table>, not raw pipe text.

    Anchored to the exact tag sequence buildTableHtml is required to emit - a real <thead>/<tbody>
    split, wrapped in a scroll container so a wide table cannot stretch the bubble - and to the
    separator validator's alignment syntax, rather than a loose "table" substring check.
    """
    failed = False
    print("**** Testing the client markdown renderer builds real tables ****")
    script = web_chat.get_chat_script()
    table_body = _extract_function_body(script, "buildTableHtml")
    if table_body is None:
        print("ERROR: no buildTableHtml function found - has table rendering been removed?")
        return True
    if "'<div class=\"chat-table-wrap\"><table><thead>'" not in table_body:
        print("ERROR: buildTableHtml does not open a <table> inside a .chat-table-wrap scroll container: {!r}".format(table_body))
        failed = True
    if "'</thead><tbody>'" not in table_body:
        print("ERROR: buildTableHtml does not separate <thead> from <tbody>: {!r}".format(table_body))
        failed = True
    if "'</tbody></table></div>'" not in table_body:
        print("ERROR: buildTableHtml does not close table/tbody/wrapper: {!r}".format(table_body))
        failed = True

    row_body = _extract_function_body(script, "buildTableRowHtml")
    if row_body is None or "'th'" not in table_body:
        print("ERROR: buildTableHtml never passes the 'th' tag through to build the header row")
        failed = True

    separator_body = _extract_function_body(script, "tableSeparatorAlignments")
    if separator_body is None:
        print("ERROR: no tableSeparatorAlignments function found")
        return True
    if ":?-+:?" not in separator_body:
        print("ERROR: tableSeparatorAlignments does not accept :--- / ---: / :---: alignment markers: {!r}".format(separator_body))
        failed = True

    # A pipe line is only ever promoted to a table when the guard below finds a valid separator
    # on the following line - so a bare "| a | b |" paragraph line can never become one.
    render_body = _extract_function_body(script, "renderMarkdown")
    if "tableSeparatorAlignments(lines[index + 1])" not in render_body:
        print("ERROR: renderMarkdown builds a table without checking the next line is a valid separator")
        failed = True
    return failed


def test_markdown_lists_group_consecutive_items_into_one_list(my_predbat):
    """Consecutive `- `/`* ` or `1. ` lines become one <ul>/<ol>, never one list per item.

    Anchored to the exact join the renderer is required to use (`.join('')`, no separator between
    <li> elements) and to <br> appearing nowhere in the list-building code - the bug this guards
    against was every <li> getting wrapped in its own <ul>, then the blanket newline pass adding a
    <br> on top, which is exactly what a per-item '<ul>...' template or a '<br>' join would
    reintroduce.
    """
    failed = False
    print("**** Testing the client markdown renderer groups list items into one list ****")
    script = web_chat.get_chat_script()
    render_body = _extract_function_body(script, "renderMarkdown")
    if render_body is None:
        print("ERROR: no renderMarkdown function found")
        return True
    if "'<ul>' + unorderedItems.join('') + '</ul>'" not in render_body:
        print("ERROR: renderMarkdown does not wrap a whole run of bullet items in one <ul>: {!r}".format(render_body))
        failed = True
    if "'<ol>' + orderedItems.join('') + '</ol>'" not in render_body:
        print("ERROR: numbered lists never produce <ol> - they would still render as bullets: {!r}".format(render_body))
        failed = True
    # A newline-to-<br> replace is legitimate exactly once in this function - inside the
    # paragraph block's own join - so counting that exact call pins it as not also being used to
    # separate list items (the list branches above join with '' instead, asserted above).
    replace_br_count = render_body.count(".replace(/\\n/g, '<br>')")
    if replace_br_count != 1:
        print("ERROR: expected exactly one newline-to-<br> replace in renderMarkdown (the paragraph block), found {}: {!r}".format(replace_br_count, render_body))
        failed = True
    return failed


def test_markdown_headings_render_h1_through_h6(my_predbat):
    """`#` through `######` become <h1>-<h6>, which the previous renderer never supported."""
    failed = False
    print("**** Testing the client markdown renderer builds headings ****")
    script = web_chat.get_chat_script()
    heading_match_body = _extract_function_body(script, "matchHeadingLine")
    if heading_match_body is None or "#{1,6}" not in heading_match_body:
        print("ERROR: matchHeadingLine does not recognise one to six leading # characters: {!r}".format(heading_match_body))
        failed = True
    render_body = _extract_function_body(script, "renderMarkdown")
    if render_body is None:
        print("ERROR: no renderMarkdown function found")
        return True
    if "'<h' + level + '>'" not in render_body or "'</h' + level + '>'" not in render_body:
        print("ERROR: renderMarkdown does not build a level-numbered heading tag: {!r}".format(render_body))
        failed = True
    if "heading[1].length" not in render_body:
        print("ERROR: renderMarkdown does not derive the heading level from the number of # characters matched")
        failed = True
    return failed


def test_markdown_fenced_code_skips_inline_and_line_rules(my_predbat):
    """Fenced code content is literal - no inline rule, and no list/table/heading line rule, runs
    on it, and its real newlines survive rather than being turned into <br>.
    """
    failed = False
    print("**** Testing the client markdown renderer treats fenced code as literal ****")
    script = web_chat.get_chat_script()
    render_body = _extract_function_body(script, "renderMarkdown")
    if render_body is None:
        print("ERROR: no renderMarkdown function found")
        return True
    fence_start = render_body.find("if (isFenceLine(line)) {")
    fence_end = render_body.find("var heading = matchHeadingLine(line);")
    if fence_start < 0 or fence_end < 0 or fence_end <= fence_start:
        print("ERROR: could not isolate the fenced-code branch inside renderMarkdown")
        return True
    fence_branch = render_body[fence_start:fence_end]
    if "renderInline(" in fence_branch:
        print("ERROR: the fenced-code branch calls renderInline - fenced content must stay literal: {!r}".format(fence_branch))
        failed = True
    if "codeLines.join('\\n')" not in fence_branch:
        print("ERROR: fenced-code lines are not rejoined with real newlines: {!r}".format(fence_branch))
        failed = True
    if "<br>" in fence_branch:
        print("ERROR: the fenced-code branch converts newlines to <br> - code content must stay literal: {!r}".format(fence_branch))
        failed = True
    return failed


def test_markdown_paragraphs_join_with_br_only_inside_the_paragraph_block(my_predbat):
    """Newlines become <br> only within a <p> block, not as a blanket pass over the whole string.

    The previous implementation's very last statement was `return safe.replace(/\\n/g, '<br>')`,
    a global pass that ran after every block tag had already been built - which is exactly what
    let a <br> land between <li> elements and inside already-built table markup. This pins the
    replacement to living inside the paragraph branch's own <p> template instead.
    """
    failed = False
    print("**** Testing the client markdown renderer confines <br> to paragraph blocks ****")
    script = web_chat.get_chat_script()
    render_body = _extract_function_body(script, "renderMarkdown")
    if render_body is None:
        print("ERROR: no renderMarkdown function found")
        return True
    if "'<p>' + renderInline(paragraphLines.join('\\n')).replace(/\\n/g, '<br>') + '</p>'" not in render_body:
        print("ERROR: paragraphs are not built by joining their own lines with <br> inside a <p> tag: {!r}".format(render_body))
        failed = True
    if re.search(r"return\s+safe\.replace\(/\\n/g,\s*'<br>'\)", script):
        print("ERROR: a blanket newline-to-<br> pass over the whole string is back")
        failed = True
    return failed


def test_markdown_table_and_heading_css_uses_theme_variables_and_scrolls(my_predbat):
    """Table/heading CSS takes its colours from --chat-* custom properties, never a hardcoded
    value - a hardcoded light-only colour was a previous review finding on this same page - and a
    wide table scrolls inside its own container rather than stretching #chat-transcript's layout.
    """
    failed = False
    print("**** Testing table/heading CSS is theme-aware and confines overflow to a scroll container ****")
    styles = web_chat.get_chat_styles()
    matches = list(re.finditer(r"([^{}]*\.chat-bubble\s+(?:h[1-6]|th|td|table)[^{}]*)\{([^}]*)\}", styles))
    if not matches:
        print("ERROR: no table/heading CSS rules found for .chat-bubble - has the CSS been removed?")
        return True
    for match in matches:
        selector, block = match.group(1), match.group(2)
        if re.search(r"#[0-9a-fA-F]{3,8}\b", block):
            print("ERROR: {!r} sets a hardcoded hex colour instead of a --chat-* variable: {!r}".format(selector.strip(), block.strip()))
            failed = True
    if ".chat-table-wrap" not in styles or "overflow-x: auto" not in styles:
        print("ERROR: no .chat-table-wrap rule with overflow-x: auto - a wide table would stretch the bubble")
        failed = True
    return failed


def test_nav_link_visibility(my_predbat):
    """The Chat nav link is emitted only when chat is enabled."""
    failed = False
    print("**** Testing the Chat nav link ****")
    from web_helper import get_header_html

    without = get_header_html("Test", False, "./dash", [], "v1.0", "", chat_enabled=False)
    if "./chat" in without:
        print("ERROR: the Chat link appears with chat disabled")
        failed = True

    with_chat = get_header_html("Test", False, "./dash", [], "v1.0", "", chat_enabled=True)
    if "./chat" not in with_chat:
        print("ERROR: the Chat link is missing with chat enabled")
        failed = True

    default = get_header_html("Test", False, "./dash", [], "v1.0", "")
    if "./chat" in default:
        print("ERROR: the Chat link appears by default, which would break every existing caller")
        failed = True

    return failed


def test_client_script_contract(my_predbat):
    """The client script wires the SSE events the server actually emits."""
    failed = False
    print("**** Testing the client script contract ****")
    script = web_chat.get_chat_script()
    for event in ["delta", "assistant", "tool_start", "tool_end", "confirm", "confirm_result", "usage", "title", "error", "done", "busy", "idle", "reload", "retry"]:
        if "'{}'".format(event) not in script and '"{}"'.format(event) not in script:
            print("ERROR: the client script does not handle the {!r} event".format(event))
            failed = True
    for endpoint in ["/chat/conversations", "/chat/history", "/chat/send", "/chat/stream", "/chat/confirm", "/chat/cancel", "/chat/delete", "/chat/rename"]:
        if endpoint not in script:
            print("ERROR: the client script never calls {}".format(endpoint))
            failed = True
    if "localStorage" not in script:
        print("ERROR: the client script does not persist the selected conversation")
        failed = True
    return failed


def test_chat_page_assembles_real_content(my_predbat):
    """The stubs are gone: styles, body and script all carry real, non-trivial content."""
    failed = False
    print("**** Testing the Chat tab page assembly ****")
    styles = web_chat.get_chat_styles()
    if "<style" not in styles or "chat-topbar" not in styles:
        print("ERROR: get_chat_styles() does not look like a real stylesheet: {!r}".format(styles[:200]))
        failed = True

    body = web_chat.get_chat_body()
    for element_id in ["chat-topbar", "chat-list", "chat-new", "chat-banner", "chat-transcript", "chat-composer", "chat-input", "chat-stop", "chat-footer", "chat-model", "chat-turn-usage", "chat-context-usage", "chat-total-cost", "chat-privacy"]:
        if element_id not in body:
            print("ERROR: get_chat_body() is missing #{}".format(element_id))
            failed = True
    # The banner no longer names OpenRouter: with several providers configurable there is no one
    # destination to name, so it has to describe where the results go rather than assert a brand.
    # Checked against the banner's own text, not the whole page - "OpenRouter" still appears in a
    # tooltip, so a substring test over the body would pass without the banner saying anything.
    banner = body[body.index('id="chat-privacy"') : body.index('id="chat-privacy-dismiss"')]
    if "provider" not in banner.lower() or "tool results" not in banner.lower():
        print("ERROR: the privacy banner does not say that tool results reach the configured provider: {!r}".format(banner))
        failed = True

    script = web_chat.get_chat_script()
    for helper in ["function selectConversation", "function openStream", "function refreshConversations"]:
        if helper not in script:
            print("ERROR: the client script is missing {}".format(helper))
            failed = True
    if "<details>" not in script and "createElement('details')" not in script and 'createElement("details")' not in script:
        print("ERROR: the client script never builds a collapsible <details> element for tool calls")
        failed = True
    if "called <code>" not in script and "called &lt;code&gt;" not in script:
        # The literal text is built in JS, so it may be split across a template/concatenation -
        # check loosely for the two words either side of the marker instead.
        if "called" not in script or "<code>" not in script:
            print("ERROR: the client script never renders a 'called <code>name</code>' summary for tool calls")
            failed = True
    return failed


def _extract_inner_html_assignments(script):
    """Return the right-hand side of every ``.innerHTML = ...;`` assignment in the script."""
    return re.findall(r"\.innerHTML\s*=\s*([^;]+);", script)


def _inner_html_rhs_is_safe(rhs):
    """Return whether an innerHTML right-hand side can only ever insert escaped/trusted content.

    Allow-listed: an empty-string clear, a bare ``renderMarkdown(...)`` call (renderMarkdown itself
    escapes first - covered separately by test_markdown_escapes_before_transforming), or a literal
    built from string constants and ``escapeHtml(...)`` calls. Everything else is checked for a
    dotted property read (``data.name``, ``message.content``, ``source.title``, ...) surviving
    outside both of those forms - that shape is how every piece of untrusted data in this client
    actually arrives (an SSE event payload, a history message, a tool result), so a bare local
    variable (no dot) is allowed through without needing to special-case it by name.
    """
    rhs = rhs.strip()
    if rhs in ("''", '""'):
        return True
    if re.fullmatch(r"renderMarkdown\(.*\)", rhs):
        return True
    working = re.sub(r"'(?:[^'\\]|\\.)*'", "SAFE", rhs)
    working = re.sub(r"escapeHtml\([^()]*\)", "SAFE", working)
    return re.search(r"[A-Za-z_$][\w$]*\.[A-Za-z_$]", working) is None


def test_inner_html_sinks_only_ever_receive_escaped_content(my_predbat):
    """Every innerHTML sink in the client is a clear, renderMarkdown output, or escaped literal.

    Unlike a proximity scan (which would still pass if a sink's own escaping were removed, as long
    as an unrelated escapeHtml/renderMarkdown/textContent happened to sit nearby in the file), this
    is anchored to each sink's own right-hand side - so it fails the moment one of them starts
    concatenating a raw property read into innerHTML.
    """
    failed = False
    print("**** Testing every innerHTML sink is escaped or renderMarkdown-derived ****")
    script = web_chat.get_chat_script()
    assignments = _extract_inner_html_assignments(script)
    if not assignments:
        print("ERROR: found no .innerHTML assignments to audit - has the sink pattern changed?")
        return True
    for rhs in assignments:
        if not _inner_html_rhs_is_safe(rhs):
            print("ERROR: an innerHTML assignment is not provably escaped: {!r}".format(rhs.strip()))
            failed = True
    return failed


def _extract_function_body(script, name):
    """Return a top-level JS function's body, found by counting braces from its signature.

    A regex alone cannot reliably find where a function ends without understanding nesting;
    counting braces from the first ``{`` after the signature until the count returns to zero
    handles that, which is all that is needed here - none of these functions embed a brace inside
    a string literal.
    """
    match = re.search(r"function\s+{}\s*\([^)]*\)\s*\{{".format(re.escape(name)), script)
    if not match:
        return None
    depth = 1
    index = match.end()
    start = index
    while index < len(script) and depth > 0:
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
        index += 1
    return script[start : index - 1]


def test_stream_cursor_advances_from_every_event(my_predbat):
    """The SSE dispatcher advances state.cursor from each event's lastEventId.

    This is what lets a client-driven reconnect resume from the position actually seen, instead of
    the browser's own automatic retry reopening the exact URL the stream was first constructed
    with - which would replay everything since the connection originally opened, not just what was
    missed. Anchored to the on(source, type, handler) dispatcher's own body, not a scan of the
    whole file, so it fails if that assignment is moved out of the dispatcher or removed.

    This is a static check on the source text, not an executed one - the repository's test
    infrastructure has no JavaScript runtime to run the client script against. The full
    cursor-advance-then-reconnect behaviour (including that a genuine `event: error` SSE frame is
    told apart from a dropped connection) was verified by hand with a stubbed EventSource under
    Node, on the machine that wrote this fix - see the task report; that verification does not run
    as part of this suite.
    """
    failed = False
    print("**** Testing the SSE cursor advances from every event's lastEventId ****")
    script = web_chat.get_chat_script()
    body = _extract_function_body(script, "on")
    if body is None:
        print("ERROR: could not find the on(source, type, handler) dispatcher to inspect")
        return True
    if "event.lastEventId" not in body or "state.cursor" not in body:
        print("ERROR: the SSE dispatcher no longer advances state.cursor from event.lastEventId: {!r}".format(body))
        failed = True
    return failed


def test_dropped_connection_is_told_apart_from_a_real_error_frame(my_predbat):
    """The reconnect handler only takes over for a dropped connection, never a real error frame.

    'error' is dispatched on the same EventSource for two unrelated things: a genuine
    `event: error` SSE frame from the server (a chat-turn failure, arriving with `data`) and a
    native browser event when the connection itself drops (no `data` at all). Rebuilding the
    stream is only correct for the second case - doing it for the first would tear down a healthy
    connection every time a chat turn merely failed. This checks the reconnect body is actually
    gated on the presence of `data`, not just present somewhere in the file.
    """
    failed = False
    print("**** Testing the reconnect handler is gated on a data-less (dropped-connection) event ****")
    script = web_chat.get_chat_script()
    body = _extract_function_body(script, "attachConnectionHandling")
    if body is None:
        print("ERROR: could not find attachConnectionHandling() to inspect")
        return True
    if "typeof event.data" not in body and "event.data ===" not in body and "event.data !==" not in body:
        print("ERROR: the reconnect handler does not appear to branch on whether the event carries data: {!r}".format(body))
        failed = True
    if "EventSource.CONNECTING" not in body:
        print("ERROR: the reconnect handler does not check readyState against EventSource.CONNECTING")
        failed = True
    reconnect_body = _extract_function_body(script, "scheduleReconnect")
    if reconnect_body is None or "openStream" not in reconnect_body:
        print("ERROR: scheduleReconnect() does not appear to call openStream()")
        failed = True
    return failed


def test_stream_reconnects_when_the_agent_is_replaced(my_predbat):
    """A restarted chat component's new instance is picked up mid-stream, not silently ignored.

    html_chat_stream used to capture self.agent once at handler entry and poll only that instance
    forever. Chat is a restartable component (Components tab, or an automatic restart after a
    health check failure): a restart builds a brand new ChatAgent and nothing ever writes to the
    old instance's event buffer again, so an open browser tab would sit there heartbeating
    normally while silently delivering nothing, forever, with no sign anything was wrong.
    Re-resolving self.agent every iteration and comparing identity is what turns that into the SSE
    stream's existing reload mechanism (already used for a buffer wraparound): on a mismatch it
    emits 'event: reload' and closes, which the client's existing handleReload() already turns
    into a fresh reconnect against the new instance - unchanged by this fix.

    Drives the real handler against a mocked aiohttp request (aiohttp.test_utils.make_mocked_
    request; StreamResponse.write is patched to record frames instead of touching a transport).
    The fake agent that starts the stream swaps the page's agent to a second instance from inside
    its own events_since() - simulating a restart landing between two poll iterations - and the
    second instance raises if the loop ever calls into it, since a correct implementation must
    stop polling the moment the identity check fails, not merely notice it eventually.
    """
    failed = False
    print("**** Testing the SSE stream reconnects when the agent instance is replaced ****")

    class FakeAgentV2:
        """The instance that replaces the original - the polling loop must never reach it."""

        def events_since(self, cursor, conversation_id):
            """Fail the test if a stale loop somehow kept polling after the swap."""
            raise AssertionError("the stream kept polling the conversation after the agent was replaced")

    class FakeAgentV1:
        """The original instance - a simulated restart replaces it mid-stream."""

        def events_since(self, cursor, conversation_id):
            """Return nothing, but swap the page's agent to simulate a concurrent restart."""
            page.agent_override = agent_v2
            return [], cursor, False

    agent_v2 = FakeAgentV2()
    agent_v1 = FakeAgentV1()
    page = _make_web(my_predbat, agent=agent_v1).chat_page

    writes = []

    async def fake_write(self, data):
        """Record what would have been written instead of touching a real transport."""
        writes.append(data)

    original_write = aiohttp_web.StreamResponse.write
    aiohttp_web.StreamResponse.write = fake_write
    try:
        request = make_mocked_request("GET", "/chat/stream?conversation=abc&cursor=0")
        try:
            response = asyncio.run(asyncio.wait_for(page.html_chat_stream(request), timeout=2))
        except asyncio.TimeoutError:
            print("ERROR: the stream never noticed the agent was replaced and kept polling past a 2s timeout")
            return True
    finally:
        aiohttp_web.StreamResponse.write = original_write

    if response.status != 200:
        print("ERROR: the stream handler returned {}, expected 200".format(response.status))
        failed = True
    if not any(b"event: reload" in chunk for chunk in writes):
        print("ERROR: no reload frame was written after the agent was replaced: {}".format(writes))
        failed = True

    return failed


def test_model_catalogue(my_predbat):
    """Only tool-capable models are offered, and the apps.yaml model always is."""
    failed = False
    print("**** Testing the model catalogue ****")
    import chat as chat_module

    agent = chat_module.ChatAgent.__new__(chat_module.ChatAgent)
    agent.default_model = "configured/model"
    agent.base_url = "https://openrouter.example/api/v1"
    agent.log = print
    # No agent.base is ever set on this bare instance, so ComponentBase's own read-only storage
    # property already resolves to None here (it guards on hasattr(self, "base")) - list_models()
    # therefore takes the storage-less path exercised below without anything set explicitly.
    if agent.storage is not None:
        print("ERROR: expected a bare ChatAgent's storage to already be None: {}".format(agent.storage))
        failed = True

    async def fake_catalogue():
        """Return a catalogue with one tool-capable model and one without."""
        return {"data": [{"id": "good/model", "name": "Good", "supported_parameters": ["tools", "temperature"], "context_length": 1000000}, {"id": "bad/model", "name": "Bad", "supported_parameters": ["temperature"]}]}

    agent._fetch_model_catalogue = fake_catalogue
    models = asyncio.run(agent.list_models())
    ids = [entry["id"] for entry in models]
    if "good/model" not in ids:
        print("ERROR: a tool-capable model was dropped: {}".format(ids))
        failed = True
    if "bad/model" in ids:
        print("ERROR: a model without tool support was offered: {}".format(ids))
        failed = True
    if "configured/model" not in ids:
        print("ERROR: the apps.yaml model must always be offered, even when absent from the catalogue")
        failed = True

    by_id = {entry["id"]: entry for entry in models}
    if by_id["good/model"].get("context_length") != 1000000:
        print("ERROR: context_length was not carried through list_models() for a catalogue entry that has one: {}".format(by_id["good/model"]))
        failed = True
    if by_id["configured/model"].get("context_length") is not None:
        print("ERROR: the apps.yaml fallback model (absent from the catalogue) should have context_length=None, not a guessed value: {}".format(by_id["configured/model"]))
        failed = True

    async def broken_catalogue():
        """Simulate an unreachable catalogue."""
        return None

    agent._fetch_model_catalogue = broken_catalogue
    fallback = asyncio.run(agent.list_models())
    if [entry["id"] for entry in fallback] != ["configured/model"]:
        print("ERROR: an unreachable catalogue should degrade to the configured model: {}".format(fallback))
        failed = True
    if fallback[0].get("context_length") is not None:
        print("ERROR: the configured model should have context_length=None when the catalogue is unreachable, not a wrong guess: {}".format(fallback[0]))
        failed = True

    return failed


def test_models_route_uses_agent_loop_and_reports_catalogue_availability(my_predbat):
    """The /chat/models route marshals list_models() onto the agent's own loop.

    The interface contract (see the task brief) is that the web layer never awaits
    ``agent.list_models()`` directly - it performs Storage I/O and an outbound HTTP request, both
    of which belong on the component's own loop, reached only via ``run_on_agent_loop``. A stand-in
    whose ``run_on_agent_loop`` refuses to run anything (exactly what the real component does
    before its own loop exists) proves that: if the handler instead awaited ``list_models()``
    directly, this would not raise AgentNotReadyError and the assertions below would fail.
    """
    failed = False
    print("**** Testing the /chat/models route ****")

    class RichAgent:
        """An agent stand-in whose list_models() is only reachable via run_on_agent_loop."""

        default_model = "configured/model"
        # The remembered model is per provider, so the route asks which one is active.
        active_provider = "openrouter"
        # /chat/models reports the remembered selection alongside the catalogue.
        store = _StubSelectionStore()

        @staticmethod
        async def list_models():
            """Return a catalogue with more than just the configured model."""
            return [{"id": "configured/model", "name": "configured/model"}, {"id": "good/model", "name": "Good"}]

        @staticmethod
        async def run_on_agent_loop(coro):
            """Await the coroutine inline, standing in for the real cross-loop marshalling."""
            return await coro

    page = _make_web(my_predbat, agent=RichAgent()).chat_page
    response = asyncio.run(page.html_chat_models(FakeRequest()))
    if response.status != 200:
        print("ERROR: /chat/models returned {}, expected 200".format(response.status))
        failed = True
    body = json.loads(response.text)
    if [entry["id"] for entry in body.get("models", [])] != ["configured/model", "good/model"]:
        print("ERROR: unexpected model list: {}".format(body.get("models")))
        failed = True
    if body.get("catalogue_available") is not True:
        print("ERROR: catalogue_available should be True when more than the configured model is on offer: {}".format(body))
        failed = True

    calls = []

    class NotReadyAgent:
        """An agent stand-in whose loop has not started yet."""

        default_model = "configured/model"

        @staticmethod
        async def list_models():
            """Should never run - reachable only via run_on_agent_loop, which is not ready."""
            calls.append("list_models")
            return []

        @staticmethod
        async def run_on_agent_loop(coro):
            """Refuse to run the coroutine, exactly as the real component does before its loop exists."""
            coro.close()
            raise AgentNotReadyError("not ready")

    page = _make_web(my_predbat, agent=NotReadyAgent()).chat_page
    response = asyncio.run(page.html_chat_models(FakeRequest()))
    if response.status != 503:
        print("ERROR: /chat/models with a not-yet-started agent returned {}, expected 503 - this proves list_models() is only reached via run_on_agent_loop".format(response.status))
        failed = True
    if calls:
        print("ERROR: list_models() ran directly, bypassing run_on_agent_loop: {}".format(calls))
        failed = True

    class SingleModelAgent:
        """An agent stand-in whose catalogue degraded to just the configured model."""

        default_model = "configured/model"
        active_provider = "openrouter"
        store = _StubSelectionStore()

        @staticmethod
        async def list_models():
            """Return only the configured model, as list_models() does when the catalogue is unreachable."""
            return [{"id": "configured/model", "name": "configured/model"}]

        @staticmethod
        async def run_on_agent_loop(coro):
            """Await the coroutine inline, standing in for the real cross-loop marshalling."""
            return await coro

    page = _make_web(my_predbat, agent=SingleModelAgent()).chat_page
    response = asyncio.run(page.html_chat_models(FakeRequest()))
    body = json.loads(response.text)
    if body.get("catalogue_available") is not False:
        print("ERROR: catalogue_available should be False when only the configured model is on offer: {}".format(body))
        failed = True

    return failed


def test_model_picker_script_wires_routes_and_persists_selection(my_predbat):
    """The model picker filters safely and persists per conversation.

    Static checks on the client script's source text, in the same spirit as
    test_stream_cursor_advances_from_every_event: there is no JavaScript runtime in this suite, so
    the wiring is verified by inspecting the actual function bodies rather than merely scanning the
    whole file for nearby-looking strings.
    """
    failed = False
    print("**** Testing the model picker's client wiring ****")
    script = web_chat.get_chat_script()

    if "/chat/models" not in script:
        print("ERROR: the client script never calls GET /chat/models to populate the picker")
        failed = True
    if "/chat/model" not in script.replace("/chat/models", ""):
        print("ERROR: the client script never calls POST /chat/model to persist a selection")
        failed = True

    # The picker is a filter box over a rendered list rather than a <select>, because the
    # tool-capable catalogue runs to several hundred entries. renderModelResults() is where
    # catalogue text reaches the DOM, so it carries the same createElement/textContent
    # requirement the <option> construction used to.
    render_body = _extract_function_body(script, "renderModelResults")
    if render_body is None:
        print("ERROR: could not find a renderModelResults() function to inspect")
        failed = True
    else:
        if "createElement" not in render_body:
            print("ERROR: renderModelResults() does not build rows with createElement")
            failed = True
        # Reuses the same sink audit as test_inner_html_sinks_only_ever_receive_escaped_content,
        # rather than a fresh regex, because a naive `\.innerHTML\s*=\s*(?!'')` check backtracks
        # around the whitespace before the quotes and silently stops catching anything - the
        # allow-listed helper already gets this right and is exercised elsewhere in this suite.
        for rhs in _extract_inner_html_assignments(render_body):
            if not _inner_html_rhs_is_safe(rhs):
                print("ERROR: renderModelResults() assigns catalogue content straight into innerHTML: {!r}".format(rhs.strip()))
                failed = True
        if ".textContent" not in render_body:
            print("ERROR: renderModelResults() does not set row labels via textContent")
            failed = True
        # A model id or name is attacker-influenced only in the weak sense that it comes from
        # OpenRouter, but it is third-party text rendered into Predbat's page either way.
        if "innerHTML" in render_body and "innerHTML = ''" not in render_body.replace('innerHTML = ""', "innerHTML = ''"):
            print("ERROR: renderModelResults() uses innerHTML for something other than clearing: {!r}".format(render_body))
            failed = True
        if "toLowerCase" not in render_body or "indexOf" not in render_body:
            print("ERROR: renderModelResults() does not filter the catalogue case-insensitively: {!r}".format(render_body))
            failed = True

    # Selecting a model must send both the conversation id and the chosen model id to
    # POST /chat/model. Anchored to the handler's own body (found by brace-counting, not a
    # proximity regex) so it fails if the post is moved somewhere that no longer carries the
    # conversation id, or dropped altogether.
    change_body = _extract_function_body(script, "selectModel")
    if change_body is None:
        print("ERROR: could not find a selectModel() handler for the picker")
        failed = True
    else:
        if "state.conversation" not in change_body or "/chat/model" not in change_body:
            print("ERROR: selectModel() does not post to /chat/model with the conversation id: {!r}".format(change_body))
            failed = True
        if "conversation:" not in change_body or ("id:" not in change_body):
            print("ERROR: selectModel() does not post both conversation and id: {!r}".format(change_body))
            failed = True
    # The list opens on focus and filters as you type - without both, it is a dropdown again.
    for wiring in ("addEventListener('focus', openModelList)", "addEventListener('input'"):
        if wiring not in script:
            print("ERROR: the picker is not wired for search: missing {}".format(wiring))
            failed = True
    # mousedown, not click: blur fires first on click and closes the list before the selection is
    # read, so a click-wired list silently never selects anything.
    if "mousedown" not in script:
        print("ERROR: the result rows are not wired on mousedown, so blur would close the list first")
        failed = True

    # Reloading a conversation must restore its own stored model, not silently keep showing
    # whatever was selected for the previous one.
    load_body = _extract_function_body(script, "loadConversationData")
    if load_body is None or "payload.model" not in load_body:
        print("ERROR: loadConversationData() does not read the conversation's stored model back from the history payload")
        failed = True

    return failed


def test_context_counter_uses_the_last_turns_prompt_tokens_not_cumulative(my_predbat):
    """The context-size footer reads data.prompt_tokens off the live 'usage' event and
    payload.last_prompt_tokens off /chat/history - never a cumulative field such as
    usage_total.prompt_tokens or conversation_cost.

    Checked on the actual extracted function bodies (brace-counted, like every other client-script
    test in this file), not a substring search anywhere in the script - a stray comment mentioning
    the right field name could otherwise make a wrongly-wired implementation pass.
    """
    failed = False
    print("**** Testing the context counter reads the last turn's prompt_tokens, not a cumulative field ****")
    script = web_chat.get_chat_script()

    if "chat-context-usage" not in script:
        print("ERROR: the client script never touches #chat-context-usage")
        return True

    usage_body = _extract_function_body(script, "renderUsageEvent")
    if usage_body is None or "renderContextUsage(data.prompt_tokens)" not in usage_body:
        print("ERROR: renderUsageEvent() does not feed this completion's own prompt_tokens to renderContextUsage(): {!r}".format(usage_body))
        failed = True

    total_body = _extract_function_body(script, "renderConversationTotal")
    if total_body is None or "renderContextUsage(lastPromptTokens)" not in total_body:
        print("ERROR: renderConversationTotal() does not feed the persisted last_prompt_tokens to renderContextUsage(): {!r}".format(total_body))
        failed = True

    context_body = _extract_function_body(script, "renderContextUsage")
    if context_body is None:
        print("ERROR: could not find a renderContextUsage() function to inspect")
        return True
    if "usage_total" in context_body or "conversation_cost" in context_body or "conversationCost" in context_body:
        print("ERROR: renderContextUsage() reads a cumulative field directly, rather than only the value it was passed: {!r}".format(context_body))
        failed = True
    if ".innerHTML" in context_body:
        print("ERROR: renderContextUsage() touches innerHTML - it must only ever use textContent: {!r}".format(context_body))
        failed = True
    if "textContent" not in context_body:
        print("ERROR: renderContextUsage() never sets textContent, so it cannot actually update the footer")
        failed = True

    history_body = _extract_function_body(script, "renderHistory")
    if history_body is None or "renderConversationTotal(payload.usage_total, payload.last_prompt_tokens)" not in history_body:
        print("ERROR: renderHistory() does not pass payload.last_prompt_tokens through to renderConversationTotal(): {!r}".format(history_body))
        failed = True

    return failed


def test_stop_button_wired_to_cancel_with_turn_id(my_predbat):
    """The Stop button posts the running turn's id to /chat/cancel, and its wording is honest.

    /chat/cancel existed and was tested, but nothing in the client called it - grepping the
    client script for 'chat/cancel' hit only the test. A user watching a runaway turn had no way
    to stop it short of waiting out chat_turn_timeout, since the composer locks globally for the
    whole turn. This checks the button exists, is wired to stopTurn(), that stopTurn() posts the
    turn_id currently tracked in state.busy (not a global "cancel whatever is running" with no id,
    which the server would then have to accept blindly), and that its wording does not overclaim
    an immediate abort - the handler only stops the turn at its next checkpoint, not mid-step.
    """
    failed = False
    print("**** Testing the Stop button is wired to /chat/cancel with a turn id ****")
    script = web_chat.get_chat_script()
    body = web_chat.get_chat_body()

    if 'id="chat-stop"' not in body and "id='chat-stop'" not in body:
        print("ERROR: get_chat_body() does not define #chat-stop")
        return True
    # The tooltip is the wording surface for a plain HTML button - it must not claim an immediate
    # abort when the handler only stops the turn at its next checkpoint (see html_chat_cancel's
    # own docstring in web_chat.py for the same claim on the server side).
    if "step" not in body.lower() or "chat-stop" not in body:
        print("ERROR: #chat-stop's markup does not explain that it stops after the current step: {!r}".format(body[body.find("chat-stop") : body.find("chat-stop") + 250]))
        failed = True

    if "addEventListener('click', stopTurn)" not in script and 'addEventListener("click", stopTurn)' not in script:
        print("ERROR: #chat-stop is never wired to a stopTurn() click handler")
        failed = True

    stop_body = _extract_function_body(script, "stopTurn")
    if stop_body is None:
        print("ERROR: could not find a stopTurn() function to inspect")
        return True
    if "/chat/cancel" not in stop_body:
        print("ERROR: stopTurn() does not call /chat/cancel")
        failed = True
    if "turn_id" not in stop_body or "state.busy" not in stop_body:
        print("ERROR: stopTurn() does not post the currently running turn's id from state.busy: {!r}".format(stop_body))
        failed = True

    # setBusy must be the one place turn_id enters state.busy, and EVERY caller must supply it - a
    # caller that dropped the argument would silently disable Stop on that path (the button would
    # show, but stopTurn() would have no turn_id to post), or worse, overwrite a good turn_id a
    # different caller had already set with 'undefined'. There are three callers: the SSE 'busy'
    # event, the active-turn restore on history reload, and doSend()'s handling of a 409 when
    # sending into an already-busy conversation - the last of these was missed in an earlier pass
    # (setBusy gained a third parameter and only two of its three call sites were updated).
    busy_body = _extract_function_body(script, "setBusy")
    if busy_body is None or "turn_id:turnId" not in busy_body.replace(" ", ""):
        print("ERROR: setBusy() does not record turnId onto state.busy: {!r}".format(busy_body))
        failed = True

    calls = re.findall(r"setBusy\(([^)]*)\)", script)
    if len(calls) < 3:
        print("ERROR: expected at least 3 setBusy() call sites (SSE busy, history restore, send-into-busy 409), found {}: {}".format(len(calls), calls))
        failed = True
    for args in calls:
        parts = [part.strip() for part in args.split(",")]
        if len(parts) != 3 or not parts[2]:
            print("ERROR: a setBusy() call site does not pass a third (turn id) argument: setBusy({})".format(args))
            failed = True

    if "setBusy(data.conversation_id,data.title,data.turn_id)" not in script.replace(" ", ""):
        print("ERROR: the SSE 'busy' handler does not pass the event's turn_id through to setBusy()")
        failed = True
    if "setBusy(payload.active.conversation_id,payload.active.title,payload.active.turn_id)" not in script.replace(" ", "").replace("\n", ""):
        print("ERROR: restoring an in-flight turn on history reload does not pass its turn_id through to setBusy()")
        failed = True

    # doSend()'s 409 handler: sending into an already-busy conversation must not leave the Stop
    # button wired to an undefined turn_id, and must not clobber a turn_id a genuine SSE 'busy'
    # event had already set with one that is missing.
    send_body = _extract_function_body(script, "doSend")
    if send_body is None:
        print("ERROR: could not find doSend() to inspect")
        failed = True
    else:
        if "setBusy(payload.conversation_id,payload.title,payload.turn_id)" not in send_body.replace(" ", ""):
            print("ERROR: doSend()'s 409 handler does not pass the busy response's turn_id through to setBusy(): {!r}".format(send_body))
            failed = True

    idle_body = _extract_function_body(script, "setIdle")
    if idle_body is None or "chat-stop" not in idle_body:
        print("ERROR: setIdle() does not hide the Stop button again")
        failed = True

    return failed


def test_html_chat_cancel_requires_the_running_turn_id(my_predbat):
    """/chat/cancel only stops the turn whose id was posted, and ignores everything else.

    Without this check, a stale cancel request - for a turn that has already finished, arriving
    late, or simply replayed - would zero the deadline of whatever DIFFERENT turn has since
    claimed the single turn slot, cutting it short for no reason the user watching it asked for.
    """
    failed = False
    print("**** Testing /chat/cancel requires the currently running turn's id ****")

    class FakeAgent:
        """An agent stand-in with one turn active, recording whether it was told to stop."""

        def __init__(self):
            """Start with turn 42 active and a non-zero deadline."""
            self.active = {"conversation_id": "abc", "turn_id": 42, "title": "t"}
            self.deadline = 12345

    agent = FakeAgent()
    page = _make_web(my_predbat, agent=agent).chat_page

    # A missing turn_id, a mismatched turn_id, and no active turn at all must all be refused, and
    # none of them may touch the deadline of whatever IS (or is not) running.
    for body in ({}, {"turn_id": 41}, {"turn_id": None}):
        response = asyncio.run(page.html_chat_cancel(FakeRequest(body=body)))
        if response.status != 409:
            print("ERROR: cancelling with body {} returned {}, expected 409".format(body, response.status))
            failed = True
        if agent.deadline != 12345:
            print("ERROR: cancelling with body {} touched the deadline of a turn it was not asked to stop: {}".format(body, agent.deadline))
            failed = True

    response = asyncio.run(page.html_chat_cancel(FakeRequest(body={"turn_id": 42})))
    if response.status != 200:
        print("ERROR: cancelling the actually-running turn's id returned {}, expected 200".format(response.status))
        failed = True
    if agent.deadline != 0:
        print("ERROR: cancelling the running turn did not zero its deadline: {}".format(agent.deadline))
        failed = True

    return failed


class _SwitchEventComponents:
    """Minimal component-registry stand-in so switch_event's real dispatch can run in a test,
    matching test_predheat.py's own FakeComponents for the same purpose."""

    async def switch_event(self, entity_id, service):
        """Swallow the component-level switch routing, which is not under test here."""
        return None


def test_chat_status_route(my_predbat):
    """/chat/status reads and writes the three footer permission switches: 404s like every other
    chat route when chat is not configured, reports each switch's live value on GET, and on POST
    writes through set_state_external() - the real mechanism the dashboard's own switches and the
    set_config tool both use - rather than a raw state overwrite that would leave get_ha_config()
    disagreeing with what the footer shows.

    The POST half also refuses a name outside CHAT_STATUS_SWITCHES without writing anything. That
    check is what stops the route being a toggle for any Predbat switch a caller can name, so it
    asserts on the absence of a set_state_external call, not merely on the 400.
    """
    failed = False
    print("**** Testing /chat/status route ****")

    switches = ["chat_confirm_writes", "chat_web_search", "ai_ha_state_enable"]
    original_values = {name: my_predbat.config_index[name].get("value") for name in switches}
    original_components = my_predbat.components
    try:
        for name in switches:
            my_predbat.config_index[name]["value"] = False

        # Not configured: 404, the same as every other chat route.
        interface = _make_web(my_predbat, agent=None)
        response = asyncio.run(interface.chat_page.html_chat_status(FakeRequest()))
        if response.status != 404:
            print("ERROR: expected 404 with no chat component, got {}".format(response.status))
            failed = True

        # Configured, all three off: every switch reported, every one False.
        interface = _make_web(my_predbat, agent=object())
        response = asyncio.run(interface.chat_page.html_chat_status(FakeRequest()))
        payload = json.loads(response.text)
        if payload.get("switches") != {name: False for name in switches}:
            print("ERROR: expected all three switches reported false, got {}".format(payload))
            failed = True

        # set_state_external() routes a CONFIG_ITEMS change through switch_event(), which always
        # notifies self.components first - a real Components registry outside the scope of this
        # route test, so it is stood in for the same way test_predheat.py stands it in.
        my_predbat.components = _SwitchEventComponents()

        calls = []
        original_set_state_external = my_predbat.ha_interface.set_state_external

        async def spy_set_state_external(entity_id, state, attributes=None):
            """Record this call's arguments, then behave exactly as normal."""
            calls.append((entity_id, state))
            return await original_set_state_external(entity_id, state, attributes or {})

        my_predbat.ha_interface.set_state_external = spy_set_state_external
        try:
            # Each switch toggles independently, through its own entity id.
            for name in switches:
                del calls[:]
                response = asyncio.run(interface.chat_page.html_chat_status_post(FakeRequest(body={"name": name, "enabled": True})))
                payload = json.loads(response.text)
                if not payload.get("ok") or payload.get("enabled") is not True or payload.get("name") != name:
                    print("ERROR: unexpected response from the status POST for {}: {}".format(name, payload))
                    failed = True
                expected_entity = "switch.{}_{}".format(my_predbat.prefix, name)
                if calls != [(expected_entity, True)]:
                    print("ERROR: expected exactly one set_state_external({}, True) call, got {}".format(expected_entity, calls))
                    failed = True
                value, _ = my_predbat.get_ha_config(name, False)
                if value is not True:
                    print("ERROR: expected {} to actually be on after the POST, got {}".format(name, value))
                    failed = True

            # An unlisted name is refused, and - the point of the allowlist - nothing is written.
            del calls[:]
            response = asyncio.run(interface.chat_page.html_chat_status_post(FakeRequest(body={"name": "expert_mode", "enabled": True})))
            if response.status != 400:
                print("ERROR: expected 400 for a switch outside the allowlist, got {}".format(response.status))
                failed = True
            if calls:
                print("ERROR: a rejected switch name still wrote to HA: {}".format(calls))
                failed = True
        finally:
            my_predbat.ha_interface.set_state_external = original_set_state_external
    finally:
        for name in switches:
            my_predbat.config_index[name]["value"] = original_values[name]
        my_predbat.components = original_components

    return failed


def test_chat_history_reports_elapsed_seconds_for_an_active_turn(my_predbat):
    """/chat/history's active payload carries elapsed_seconds, computed fresh at request time.

    A browser calling this route mid-turn - a fresh page load, or a reload - has no idea when the
    turn it is about to show as busy actually started: the SSE 'busy' event that announced it may
    have fired long before this request, or (a plain page load) never reached this browser's
    JavaScript at all, since the event buffer is only replayed to an already-open EventSource.
    elapsed_seconds is what lets the client offset its own "thinking..." counter to the turn's
    true total instead of restarting it at 0 - see the client-side half of this in
    test_thinking_bubble_shown_on_busy_and_hidden_on_first_delta.
    """
    failed = False
    print("**** Testing /chat/history reports elapsed_seconds for the active turn ****")
    agent = _make_agent(my_predbat, turn_timeout=30)
    cid = asyncio.run(agent.store.create())

    # A real background loop, the same pattern test_history_snapshot_and_cursor_do_not_lose_a_
    # concurrent_message uses just above, so run_on_agent_loop's cross-thread handoff is genuine.
    # Both requests below must run while this loop is still alive - a second run_on_agent_loop()
    # call made after it has been stopped would schedule its coroutine via call_soon_threadsafe()
    # onto a loop that is no longer turning, and the awaited future would then never resolve.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    agent.loop = loop
    agent.active = {"conversation_id": cid, "turn_id": 1, "title": "t", "started": time.monotonic() - 7.5}

    page = _make_web(my_predbat, agent=agent).chat_page
    try:
        response = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": cid})))
        agent.active = None
        response2 = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": cid})))
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)

    if response.status != 200:
        print("ERROR: history returned {}, expected 200".format(response.status))
        return True
    body = json.loads(response.text)
    active = body.get("active")
    if not active or "elapsed_seconds" not in active:
        print("ERROR: the active payload does not carry elapsed_seconds: {}".format(active))
        return True
    elapsed = active["elapsed_seconds"]
    if not (7.0 <= elapsed <= 30.0):
        print("ERROR: elapsed_seconds is {}, expected close to the 7.5s the turn has actually been running".format(elapsed))
        failed = True

    if response2.status != 200:
        print("ERROR: the second history request returned {}, expected 200".format(response2.status))
        return True
    body2 = json.loads(response2.text)
    if body2.get("active") is not None:
        print("ERROR: active should be None with no running turn, got {}".format(body2.get("active")))
        failed = True

    return failed


def _extract_on_handler_body(script, event_name):
    """Return the body of the inline handler passed to on(source, '<event_name>', function ...).

    Mirrors _extract_function_body's brace-counting approach, but anchored to the on() call site
    rather than a top-level named function declaration - openStream() wires several SSE events
    (busy, idle) with an inline anonymous function rather than a named one.
    """
    match = re.search(r"on\(source,\s*'{}'\s*,\s*function\s*\([^)]*\)\s*\{{".format(re.escape(event_name)), script)
    if not match:
        return None
    depth = 1
    index = match.end()
    start = index
    while index < len(script) and depth > 0:
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
        index += 1
    return script[start : index - 1]


def test_thinking_bubble_shown_on_busy_and_hidden_on_first_delta(my_predbat):
    """The ghost 'thinking...' bubble starts with the turn, hides on the first delta, and shows
    again after each tool_end while the model works on its next step; done/error/idle clear it.

    Static source checks, like the rest of this file's client-script tests - there is no
    JavaScript runtime in this test infrastructure (see test_stream_cursor_advances_from_every_
    event for the same constraint). Each assertion is anchored to the specific handler's own body,
    not a whole-file substring scan, so it fails the moment one handler stops wiring the call
    rather than merely if the call disappears from the file entirely.
    """
    failed = False
    print("**** Testing the thinking bubble's busy/delta/tool_end/done/error/idle wiring ****")
    script = web_chat.get_chat_script()

    busy_body = _extract_on_handler_body(script, "busy")
    if busy_body is None:
        print("ERROR: could not find the SSE 'busy' handler to inspect")
        return True
    if "startThinkingTimer(" not in busy_body or "showThinkingBubble()" not in busy_body:
        print("ERROR: the 'busy' handler does not start/show the thinking bubble: {!r}".format(busy_body))
        failed = True

    delta_body = _extract_function_body(script, "handleDelta")
    if delta_body is None or "hideThinkingBubble()" not in delta_body:
        print("ERROR: handleDelta() does not hide the thinking bubble on the first delta: {!r}".format(delta_body))
        failed = True

    if "on(source, 'tool_end', handleToolEnd)" not in script.replace("\n", " "):
        print("ERROR: the SSE 'tool_end' event is not wired to handleToolEnd()")
        failed = True
    tool_end_body = _extract_function_body(script, "handleToolEnd")
    if tool_end_body is None or "showThinkingBubble()" not in tool_end_body:
        print("ERROR: handleToolEnd() does not show the thinking bubble again for the model's next step: {!r}".format(tool_end_body))
        failed = True

    for name in ("handleDone", "handleError"):
        body = _extract_function_body(script, name)
        if body is None or "clearThinkingBubble()" not in body:
            print("ERROR: {}() does not clear the thinking bubble: {!r}".format(name, body))
            failed = True

    idle_body = _extract_on_handler_body(script, "idle")
    if idle_body is None or "clearThinkingBubble()" not in idle_body:
        print("ERROR: the 'idle' handler does not clear the thinking bubble: {!r}".format(idle_body))
        failed = True

    return failed


def test_thinking_bubble_element_is_reused_not_recreated(my_predbat):
    """ensureThinkingBubble() reuses the one #chat-thinking element rather than creating a new one.

    Creating a fresh element on every show() would jitter the transcript with insert/remove churn
    across a multi-tool-call turn instead of the intended single steady placeholder - the task's
    own requirement is "reuse one #chat-thinking element rather than creating one per wait".
    """
    failed = False
    print("**** Testing the thinking bubble element is reused, not recreated per wait ****")
    script = web_chat.get_chat_script()
    body = _extract_function_body(script, "ensureThinkingBubble")
    if body is None:
        print("ERROR: could not find ensureThinkingBubble() to inspect")
        return True
    guard_index = body.find("byId('chat-thinking')")
    create_index = body.find("createElement")
    if guard_index == -1 or create_index == -1 or guard_index > create_index:
        print("ERROR: ensureThinkingBubble() does not look up the existing element before creating a new one: {!r}".format(body))
        failed = True
    before_create = body.split("createElement", 1)[0] if "createElement" in body else body
    if "return bubble" not in before_create:
        print("ERROR: ensureThinkingBubble() does not return early when the element already exists - it would create and append a duplicate every time it is called: {!r}".format(body))
        failed = True

    show_body = _extract_function_body(script, "showThinkingBubble")
    if show_body is None or "ensureThinkingBubble()" not in show_body:
        print("ERROR: showThinkingBubble() does not go through ensureThinkingBubble(): {!r}".format(show_body))
        failed = True

    return failed


def test_thinking_timer_interval_is_genuinely_cleared_not_merely_clearable(my_predbat):
    """stopThinkingTimer() calls clearInterval() on the real handle, AND the conversation-switch
    path (loadConversationData) actually calls it before rebuilding the transcript.

    Two checks, not one, because a stopThinkingTimer() that genuinely clears its interval but that
    nothing switching conversations ever calls would still leak exactly the interval this task
    named: "switching away mid-turn leaks a setInterval ticking against a detached element". A
    test that only checked the clearing function's existence would pass even with that call site
    missing - which is precisely the trap the task warned this test must not fall into.
    """
    failed = False
    print("**** Testing the thinking timer interval is actually cleared, not just clearable ****")
    script = web_chat.get_chat_script()

    stop_body = _extract_function_body(script, "stopThinkingTimer")
    if stop_body is None:
        print("ERROR: could not find stopThinkingTimer() to inspect")
        return True
    if "clearInterval(thinkingTimer)" not in stop_body.replace(" ", ""):
        print("ERROR: stopThinkingTimer() does not call clearInterval() on the real timer handle: {!r}".format(stop_body))
        failed = True
    if "thinkingTimer=null" not in stop_body.replace(" ", ""):
        print("ERROR: stopThinkingTimer() does not null out the handle after clearing it: {!r}".format(stop_body))
        failed = True

    load_body = _extract_function_body(script, "loadConversationData")
    if load_body is None:
        print("ERROR: could not find loadConversationData() to inspect")
        return True
    if "clearThinkingBubble()" not in load_body:
        print("ERROR: loadConversationData() (the conversation-switch path) never clears the thinking timer - switching away mid-turn would leak the interval against a detached #chat-thinking element")
        failed = True
    elif "renderHistory(payload)" in load_body and load_body.find("clearThinkingBubble()") > load_body.find("renderHistory(payload)"):
        # renderHistory() wipes #chat-transcript's innerHTML - clearing after that point still
        # leaves the interval ticking against the (by then detached) previous element for however
        # long it takes this function to get around to clearing it.
        print("ERROR: loadConversationData() clears the thinking timer after rebuilding the transcript, not before - leaves a window where the old interval targets a detached element")
        failed = True

    return failed


def test_thinking_bubble_css_respects_theme_vars_and_reduced_motion(my_predbat):
    """The thinking bubble's styling uses --chat-* custom properties, not a hardcoded colour, and
    its caret sits steady under prefers-reduced-motion rather than always blinking.

    Both constraints were dark-mode/accessibility findings on an earlier banner in this same file
    (see get_chat_styles()'s docstring); this pins them against the same mistake recurring here.
    """
    failed = False
    print("**** Testing the thinking bubble's CSS uses theme variables and respects reduced motion ****")
    styles = web_chat.get_chat_styles()
    if ".chat-bubble-thinking" not in styles:
        print("ERROR: no .chat-bubble-thinking rule found")
        return True
    thinking_block_match = re.search(r"\.chat-bubble-thinking\s*\{([^}]*)\}", styles)
    if not thinking_block_match or "var(--chat-" not in thinking_block_match.group(1):
        print("ERROR: .chat-bubble-thinking does not take its colour from a --chat-* custom property: {!r}".format(thinking_block_match.group(1) if thinking_block_match else None))
        failed = True
    if "prefers-reduced-motion" not in styles:
        print("ERROR: no prefers-reduced-motion media query found")
        failed = True
    elif "chat-thinking-caret" not in styles.split("prefers-reduced-motion", 1)[-1]:
        print("ERROR: the caret animation is not disabled under prefers-reduced-motion")
        failed = True

    return failed


def test_retry_event_wired_to_handle_retry(my_predbat):
    """The SSE 'retry' event is wired to a handleRetry() function, not left unhandled.

    Server-side, chat.py's _wait_before_retrying() emits 'retry' before every backoff so the
    browser can discard a partial assistant bubble before the retried attempt's own deltas start
    arriving - see test_handle_retry_discards_pending_bubble_and_shows_the_reason for what the
    handler itself must do once it receives one. This just pins the wiring: without it, the event
    would arrive at the client and be silently dropped by the EventSource, same as any other
    unregistered event type.
    """
    failed = False
    print("**** Testing the SSE 'retry' event is wired to handleRetry() ****")
    script = web_chat.get_chat_script()
    if "on(source, 'retry', handleRetry)" not in script.replace("\n", " "):
        print("ERROR: the client script does not wire the 'retry' SSE event to handleRetry()")
        failed = True
    if "function handleRetry" not in script:
        print("ERROR: the client script has no handleRetry() function")
        failed = True
    return failed


def test_handle_retry_discards_pending_bubble_and_shows_the_reason(my_predbat):
    """handleRetry() discards the in-progress assistant bubble and shows the retry status.

    Discarding via discardPendingBubble() (not the ordinary clearPendingBubble(), which leaves a
    bubble with real content in place - correct for a turn-ending error, wrong here) is what stops
    a retried attempt's deltas from being appended onto a bubble the failed attempt had already
    partly filled in. The status text itself must be set with textContent, never innerHTML: a
    provider's own error wording (data.reason) is exactly as untrusted as any other server-relayed
    text in this client, and the sink audit (test_inner_html_sinks_only_ever_receive_escaped_
    content) never allow-lists a bare property read reaching innerHTML.
    """
    failed = False
    print("**** Testing handleRetry() discards the pending bubble and shows the reason via textContent ****")
    script = web_chat.get_chat_script()
    body = _extract_function_body(script, "handleRetry")
    if body is None:
        print("ERROR: could not find handleRetry() to inspect")
        return True
    if "discardPendingBubble()" not in body:
        print("ERROR: handleRetry() does not call discardPendingBubble() - a retried attempt would append onto the failed attempt's partial bubble: {!r}".format(body))
        failed = True
    if "clearPendingBubble()" in body:
        print("ERROR: handleRetry() calls clearPendingBubble(), which keeps a bubble that has real content - it must unconditionally discard instead: {!r}".format(body))
        failed = True
    if ".innerHTML" in body:
        print("ERROR: handleRetry() touches innerHTML directly - the reason text must go through textContent only: {!r}".format(body))
        failed = True

    # handleRetry() itself only needs to hand the event payload off - the actual reason/attempt/
    # countdown display lives in startRetryCountdown(), which it must call with the raw data.
    if "startRetryCountdown(data)" not in body:
        print("ERROR: handleRetry() does not call startRetryCountdown(data), so the retry status would never be shown: {!r}".format(body))
        failed = True
    countdown_body = _extract_function_body(script, "startRetryCountdown")
    if countdown_body is None:
        print("ERROR: could not find startRetryCountdown() to inspect")
        return True
    if ".innerHTML" in countdown_body:
        print("ERROR: startRetryCountdown() touches innerHTML directly - the reason text must go through textContent only: {!r}".format(countdown_body))
        failed = True
    if "textContent" not in countdown_body:
        print("ERROR: startRetryCountdown() never sets textContent, so the retry status would never actually be shown: {!r}".format(countdown_body))
        failed = True
    if "data.reason" not in countdown_body:
        print("ERROR: startRetryCountdown() does not read data.reason, so the retry's cause would never reach the user: {!r}".format(countdown_body))
        failed = True
    if "data.attempt" not in countdown_body or "data.of" not in countdown_body:
        print("ERROR: startRetryCountdown() does not read data.attempt/data.of, so the attempt count would never reach the user: {!r}".format(countdown_body))
        failed = True
    return failed


def test_discard_pending_bubble_unconditionally_removes_the_bubble(my_predbat):
    """discardPendingBubble() removes the bubble element regardless of whether it has text.

    This is what distinguishes it from clearPendingBubble(), whose whole point is the opposite:
    only remove an empty ghost bubble, leaving one with real streamed content in place. A retry
    must discard the failed attempt's content unconditionally - see the task this implements -
    so a discardPendingBubble() that only removed an empty bubble would silently degrade into
    clearPendingBubble() and let a retried attempt's deltas append onto old content again.
    """
    failed = False
    print("**** Testing discardPendingBubble() unconditionally removes the bubble ****")
    script = web_chat.get_chat_script()
    body = _extract_function_body(script, "discardPendingBubble")
    if body is None:
        print("ERROR: could not find discardPendingBubble() to inspect")
        return True
    if "pendingBubble.remove()" not in body.replace(" ", ""):
        print("ERROR: discardPendingBubble() does not call pendingBubble.remove(): {!r}".format(body))
        failed = True
    if "!pendingText" in body:
        print("ERROR: discardPendingBubble() is gated on pendingText - that makes it conditional, exactly like clearPendingBubble(), instead of an unconditional discard: {!r}".format(body))
        failed = True
    if "pendingBubble=null" not in body.replace(" ", "") or "pendingText=''" not in body.replace(" ", ""):
        print("ERROR: discardPendingBubble() does not reset pendingBubble/pendingText, so the next delta would append onto the discarded state: {!r}".format(body))
        failed = True
    return failed


def test_retry_countdown_interval_is_genuinely_cleared(my_predbat):
    """stopRetryCountdown() clears its own interval on the real handle, and clearThinkingBubble()
    (the function every turn-ending path already calls) calls it - otherwise a retry countdown
    left running past done/error/idle, or past a conversation switch, ticks forever against a
    detached element exactly like the pre-existing thinkingTimer leak this mirrors.
    """
    failed = False
    print("**** Testing the retry countdown interval is actually cleared, not just clearable ****")
    script = web_chat.get_chat_script()

    stop_body = _extract_function_body(script, "stopRetryCountdown")
    if stop_body is None:
        print("ERROR: could not find stopRetryCountdown() to inspect")
        return True
    if "clearInterval(retryCountdownTimer)" not in stop_body.replace(" ", ""):
        print("ERROR: stopRetryCountdown() does not call clearInterval() on the real timer handle: {!r}".format(stop_body))
        failed = True
    if "retryCountdownTimer=null" not in stop_body.replace(" ", ""):
        print("ERROR: stopRetryCountdown() does not null out the handle after clearing it: {!r}".format(stop_body))
        failed = True

    clear_body = _extract_function_body(script, "clearThinkingBubble")
    if clear_body is None or "stopRetryCountdown()" not in clear_body:
        print("ERROR: clearThinkingBubble() does not call stopRetryCountdown() - a retry countdown would outlive every turn-ending path that already relies on clearThinkingBubble(): {!r}".format(clear_body))
        failed = True

    return failed


def test_retry_status_element_takes_its_colour_from_theme_variables(my_predbat):
    """Any CSS rule styling the retry status text uses a --chat-* custom property, never a
    hardcoded colour - the same theme-awareness constraint the thinking bubble itself follows.
    """
    failed = False
    print("**** Testing the retry status element's CSS uses theme variables, not hardcoded colours ****")
    styles = web_chat.get_chat_styles()
    for match in re.finditer(r"([^{}]*chat-thinking-retr[^{}]*)\{([^}]*)\}", styles):
        selector, block = match.group(1), match.group(2)
        if "color" in block and "var(--chat-" not in block:
            print("ERROR: a rule for {!r} sets a colour without a --chat-* custom property: {!r}".format(selector.strip(), block.strip()))
            failed = True
    return failed


def test_chat_page_fills_the_window_without_an_outer_scrollbar(my_predbat):
    """The Chat tab fills the window exactly; only the transcript scrolls.

    Two earlier attempts got this wrong in opposite directions, both because they needed to know
    the header's height, which is not fixed - an apps.yaml error banner adds a line and the
    version string wraps on a narrow window. A hardcoded calc() guessed too small and the document
    scrolled, taking the nav off the top. A JS measurement then read the page's DOCUMENT-relative
    top and subtracted it from the viewport height, which is only correct while the document
    has not been scrolled; once it had scrolled at all the height came out short, leaving dead space under
    the footer.

    Flex needs neither. body is a column, #chat-page takes the remainder, and the browser
    recomputes it on every reflow.

    Mutation checks: removing min-height: 0, body's overflow: hidden, or the flex rule on
    #chat-page each fails an assertion below.
    """
    failed = False
    print("**** Testing the chat page fills the window without an outer scrollbar ****")
    styles = web_chat.get_chat_styles()
    script = web_chat.get_chat_script()

    # No JS sizing at all - that approach is what produced the dead space.
    if "sizeChatPage" in script:
        print("ERROR: the page is being sized from JavaScript again, which drifts once the document scrolls")
        failed = True

    body_rule = _extract_css_rule(styles, "body")
    if body_rule is None:
        print("ERROR: the chat stylesheet does not size body at all")
        return True
    if "flex" not in body_rule or "column" not in body_rule:
        print("ERROR: body is not a flex column, so #chat-page cannot take the remaining height: {!r}".format(body_rule))
        failed = True
    if "overflow: hidden" not in body_rule:
        print("ERROR: body still scrolls, so the outer scrollbar remains: {!r}".format(body_rule))
        failed = True

    # body's overflow does not govern the document. If anything is wider than the window, html
    # grows a horizontal scrollbar, and that bar consumes ~15px of HEIGHT - which pushed the
    # composer off the bottom even with the height arithmetic correct.
    html_rule = _extract_css_rule(styles, "html")
    if html_rule is None or "overflow: hidden" not in html_rule:
        print("ERROR: html can still scroll, so a horizontal bar there steals height from the page: {!r}".format(html_rule))
        failed = True
    # body carries a 5px margin from the global stylesheet, top and bottom. Without allowing for
    # it the page is exactly one margin too tall and the document scrolls by that much.
    if "100vh - 10px" not in body_rule:
        print("ERROR: body's height does not allow for its own margins: {!r}".format(body_rule))
        failed = True
    # The menu bar is position: fixed, so the global stylesheet gives body padding-top: 65px to
    # clear it. Under the default content-box that padding is ADDED to the height above, making
    # the page 65px taller than the window and pushing the composer off the bottom.
    if "box-sizing: border-box" not in body_rule:
        print("ERROR: body is not border-box, so the global 65px padding is added to its height: {!r}".format(body_rule))
        failed = True

    page_rule = _extract_css_rule(styles, "#chat-page")
    if page_rule is None:
        print("ERROR: there is no #chat-page rule")
        return True
    if "flex: 1" not in page_rule:
        print("ERROR: #chat-page does not take the remaining height: {!r}".format(page_rule))
        failed = True
    # A flex item defaults to min-height auto and refuses to shrink below its content, which would
    # push the page past the viewport and bring the outer scrollbar back.
    if "min-height: 0" not in page_rule:
        print("ERROR: #chat-page has no min-height: 0, so its content can push it past the viewport: {!r}".format(page_rule))
        failed = True
    if "height: calc(100vh" in page_rule:
        print("ERROR: #chat-page is back to guessing the header height: {!r}".format(page_rule))
        failed = True

    # The transcript must remain the one thing that scrolls, and every ancestor between it and the
    # page needs min-height: 0 or the chain breaks silently.
    transcript_rule = _extract_css_rule(styles, "#chat-transcript")
    if transcript_rule is None or "overflow-y: auto" not in transcript_rule:
        print("ERROR: the transcript is not the inner scroller: {!r}".format(transcript_rule))
        failed = True
    main_rule = _extract_css_rule(styles, "#chat-main")
    if main_rule is None or "min-height: 0" not in main_rule:
        print("ERROR: #chat-main has no min-height: 0, which breaks the scroll chain: {!r}".format(main_rule))
        failed = True

    return failed


def _extract_css_rule(styles, selector):
    """Return the declarations of the first rule for an exact selector, or None.

    Comments are stripped first: the rules under test carry long explanatory comments directly
    above them, which would otherwise be read as part of the selector and match nothing.
    """
    stripped = re.sub(r"/\*.*?\*/", "", styles, flags=re.S)
    for block in stripped.split("}"):
        if "{" not in block:
            continue
        head, _, body = block.partition("{")
        if head.strip() == selector:
            return body.strip()
    return None


def test_thinking_bubble_moves_to_the_end(my_predbat):
    """The waiting indicator is re-appended each time, so it sits below the newest message.

    hideThinkingBubble() only adds a CSS class - the element stays in the transcript where it was.
    So if showThinkingBubble() appends only when the bubble has no parent, it is placed correctly
    on the first turn and never again: everything appended afterwards, including the user's next
    message, lands below the stranded bubble, and "thinking" appears above the message it is
    waiting on.

    appendChild moves an element already in the DOM rather than duplicating it, so an
    unconditional append is both the fix and safe to repeat.

    Mutation check: restoring the "if (!bubble.parentNode)" guard fails this.
    """
    failed = False
    print("**** Testing the thinking bubble is moved to the end ****")
    script = web_chat.get_chat_script()

    body = _extract_function_body(script, "showThinkingBubble")
    if body is None:
        print("ERROR: there is no showThinkingBubble() to inspect")
        return True

    if "appendChild" not in body:
        print("ERROR: showThinkingBubble() never appends the bubble: {!r}".format(body))
        failed = True
    # The guard is the bug: it makes the append happen once and only once.
    if "parentNode" in body:
        print("ERROR: showThinkingBubble() still guards the append on parentNode, so the bubble is stranded above later messages: {!r}".format(body))
        failed = True

    # Showing it last is not enough on its own. The user's own message arrives as an SSE event
    # AFTER the indicator goes up, and tool blocks arrive after that, so each would land below
    # "thinking" and leave it hanging above the message it is waiting on - which is exactly what
    # was seen: the indicator sat above the prompt, then jumped below the first tool call. Every
    # transcript append therefore reasserts the indicator's position.
    append_body = _extract_function_body(script, "appendToTranscript")
    if append_body is None:
        print("ERROR: there is no appendToTranscript() to keep the waiting indicator last")
        failed = True
    elif "chat-thinking" not in append_body:
        print("ERROR: appendToTranscript() does not reassert the waiting indicator's position: {!r}".format(append_body))
        failed = True

    # And nothing may bypass it. The indicator's own append is the single exception - routing that
    # through the helper would have it try to reorder itself.
    direct = script.count("chat-transcript').appendChild")
    if direct > 1:
        print("ERROR: {} transcript appends bypass appendToTranscript(), so the indicator can be stranded again".format(direct))
        failed = True

    # And the reason the guard breaks it: hiding does not remove the element.
    hide_body = _extract_function_body(script, "hideThinkingBubble")
    if hide_body is not None and "removeChild" not in hide_body and "remove()" not in hide_body:
        if "classList" not in hide_body:
            print("ERROR: hideThinkingBubble() neither removes the element nor hides it by class: {!r}".format(hide_body))
            failed = True

    return failed


def test_chat_switch_defaults_and_mirror(my_predbat):
    """The three chat switches default on, and CHAT_STATUS_SWITCHES agrees with CONFIG_ITEMS.

    Two separate things, both previously unpinned.

    The defaults: confirm-writes on so a write always stops for approval, HA state access on so
    the agent is useful out of the box, and web search OFF because it is the only one that costs
    money per request. Predbat's own search_docs does not go through it - that reads the published
    documentation index directly - so leaving it off costs no capability the user is likely to
    miss. Nothing asserted these, so the gate tests, which set each switch explicitly, would pass
    whichever way round the defaults were.

    The mirror: web_chat.CHAT_STATUS_SWITCHES carries its own copy of the defaults, used by
    /chat/status when get_ha_config falls back. A comment says it must match config.py, but a
    comment cannot fail. If it drifts, the footer shows a state the gate itself disagrees with
    until the first write lands - a toggle that reads "off" while the tool it gates is running.

    Mutation checks: flipping any default in config.py, or any value in CHAT_STATUS_SWITCHES,
    fails this.
    """
    failed = False
    print("**** Testing chat switch defaults and the CHAT_STATUS_SWITCHES mirror ****")

    from config import CONFIG_ITEMS

    expected = {"chat_confirm_writes": True, "chat_web_search": False, "ai_ha_state_enable": True}
    by_name = {item.get("name"): item for item in CONFIG_ITEMS}

    for name, wanted in expected.items():
        item = by_name.get(name)
        if item is None:
            print("ERROR: {} is not in CONFIG_ITEMS at all".format(name))
            failed = True
            continue
        if item.get("type") != "switch":
            print("ERROR: {} is not a switch, it is a {}".format(name, item.get("type")))
            failed = True
        if item.get("default") is not wanted:
            print("ERROR: {} defaults to {}, expected {}".format(name, item.get("default"), wanted))
            failed = True

    mirror = web_chat.CHAT_STATUS_SWITCHES
    if set(mirror) != set(expected):
        print("ERROR: CHAT_STATUS_SWITCHES covers {}, expected {}".format(sorted(mirror), sorted(expected)))
        failed = True
    for name, value in mirror.items():
        item = by_name.get(name)
        if item is not None and item.get("default") is not value:
            print("ERROR: CHAT_STATUS_SWITCHES has {}={} but CONFIG_ITEMS defaults it to {} - the footer would show a state the gate disagrees with".format(name, value, item.get("default")))
            failed = True

    return failed


def test_model_picker_shows_prices(my_predbat):
    """The picker renders per-million-token prices, and handles free and routed models.

    The catalogue already carried prompt_price/completion_price; nothing displayed them. They
    arrive as strings of US dollars PER TOKEN ("0.000002"), which is unreadable at that scale -
    every model would show as $0.00 - so they are scaled to the per-million figure that pricing is
    normally quoted in.

    Two cases come from the live catalogue rather than being imagined: 21 of its 388 models are
    priced at zero, and 5 (openrouter/auto and the other routing models) are priced at -1, meaning
    the cost depends on which model the request is routed to. Formatting -1 arithmetically gives
    "$-1000000", which is why the negative case is handled rather than assumed impossible.

    Static checks on the script source - there is no JS runtime in this suite - so this pins the
    wiring and the branches, not the arithmetic, which was validated against all 388 live models.

    Mutation checks: dropping the per-million scaling, the free branch, or the negative branch
    each fails an assertion below.
    """
    failed = False
    print("**** Testing the model picker shows prices ****")
    script = web_chat.get_chat_script()

    body = _extract_function_body(script, "formatModelPrice")
    if body is None:
        print("ERROR: there is no formatModelPrice() to render catalogue prices")
        return True

    # Scaled to per-million, or every model reads as $0.00. Anchored to the multiplication rather
    # than the bare number: the comment below it mentions "$-1000000", so a substring check for
    # the digits alone passes even with the scaling deleted.
    if "* 1000000" not in body:
        print("ERROR: formatModelPrice() does not scale to a per-million-token price: {!r}".format(body))
        failed = True
    # Zero-priced models say so rather than showing "$0/$0".
    if "'free'" not in body and '"free"' not in body:
        print("ERROR: formatModelPrice() has no free branch: {!r}".format(body))
        failed = True
    # Routing models quote -1; the arithmetic would render "$-1000000".
    if "< 0" not in body:
        print("ERROR: formatModelPrice() does not handle the -1 price OpenRouter uses for routed models: {!r}".format(body))
        failed = True
    # A missing price must not render as "$NaN".
    if "isFinite" not in body:
        print("ERROR: formatModelPrice() does not guard against a non-numeric price: {!r}".format(body))
        failed = True

    # Trailing-zero trimming must not touch integers: "10" would become "1".
    trim = _extract_function_body(script, "trimTrailingZeros")
    if trim is None or "indexOf('.')" not in trim:
        print("ERROR: trimTrailingZeros() does not check for a decimal point first, so '10' becomes '1': {!r}".format(trim))
        failed = True

    # And the picker has to actually call it.
    render = _extract_function_body(script, "renderModelResults")
    if render is None or "formatModelPrice" not in render:
        print("ERROR: renderModelResults() never calls formatModelPrice, so no price is shown")
        failed = True

    return failed


def test_busy_banner_is_reconciled_against_the_server(my_predbat):
    """The "Replying in ..." banner is corrected from /chat/conversations, not just SSE events.

    /chat/conversations has always returned `active` - the server's own view of which turn is
    running - and the client threw it away, reading only the conversation list. state.busy was
    therefore cleared solely by the 'idle' SSE event. Miss that once, which a dropped connection
    or a sleeping tab will do, and the banner stays up forever offering to switch to a
    conversation that finished long ago, with the composer locked behind it.

    Reconciled on every list refresh and on stream open, the latter being exactly when events go
    missing: EventSource resumes by itself after a drop and an 'idle' that arrived meanwhile is
    simply gone.

    Mutation checks: dropping the reconcileBusy() call from refreshConversations, or the open
    listener, each fails below.
    """
    failed = False
    print("**** Testing the busy banner is reconciled against the server ****")
    script = web_chat.get_chat_script()

    body = _extract_function_body(script, "reconcileBusy")
    if body is None:
        print("ERROR: there is no reconcileBusy() to correct a stale banner")
        return True
    # Both directions: adopt a running turn, and clear one that has finished.
    if "setBusy" not in body:
        print("ERROR: reconcileBusy() never adopts a running turn: {!r}".format(body))
        failed = True
    if "setIdle" not in body:
        print("ERROR: reconcileBusy() never clears a finished turn, which is the stuck-banner case: {!r}".format(body))
        failed = True

    refresh = _extract_function_body(script, "refreshConversations")
    if refresh is None or "reconcileBusy" not in refresh:
        print("ERROR: refreshConversations() still discards payload.active: {!r}".format(refresh))
        failed = True

    # Reconnect is the case that matters most, since that is when an idle event is lost.
    if "addEventListener('open'" not in script:
        print("ERROR: the stream does not reconcile on open, so a missed idle is never corrected")
        failed = True

    # And the route has to keep sending it.
    if '"active": agent.active' not in web_chat.__file__ and "active" not in script:
        print("ERROR: nothing reads an active turn from the conversations payload")
        failed = True

    return failed


def test_bubble_content_stays_inside_its_bubble(my_predbat):
    """Long identifiers and wide blocks stay within the bubble instead of overflowing it.

    The model's answers are full of tokens like "my_predbat.car_charging_planned[car_n]" - no
    spaces to break at - and word-wrap: break-word does not break a token that is the sole content
    of its line. They ran straight out of the grey bubble and put a horizontal scrollbar across
    the whole transcript.

    Wide children were the other half: a pre or a table with overflow-x set still sizes to its
    content unless capped, so instead of scrolling internally it widened the bubble and pushed the
    scrollbar outward.

    Mutation checks: removing overflow-wrap: anywhere, either max-width: 100% cap, or the
    transcript's overflow-x: hidden, each fails an assertion below.
    """
    failed = False
    print("**** Testing bubble content stays inside the bubble ****")
    styles = web_chat.get_chat_styles()

    bubble = _extract_css_rule(styles, ".chat-bubble")
    if bubble is None:
        print("ERROR: there is no .chat-bubble rule")
        return True
    # Without border-box the 24px of horizontal padding lands outside the 92%, so the bubble is
    # wider than it claims and its content runs past the visible edge.
    if "box-sizing: border-box" not in bubble:
        print("ERROR: .chat-bubble padding is added outside its max-width: {!r}".format(bubble))
        failed = True
    if "overflow-wrap: anywhere" not in bubble:
        print("ERROR: .chat-bubble cannot break an unbroken identifier: {!r}".format(bubble))
        failed = True

    # Predbat's global stylesheet sets `p { white-space: nowrap }` for its data tables, which
    # applies to every paragraph the markdown renderer produces. nowrap forbids wrapping outright,
    # so overflow-wrap cannot act on it - this was the actual reason replies ran off the right of
    # their bubble, whatever the bubbles were sized at.
    nowrap_override = _extract_css_rule(styles, "#chat-page p,\n#chat-page li,\n#chat-page td,\n#chat-page th")
    if nowrap_override is None:
        nowrap_override = styles[styles.find("#chat-page p") : styles.find("#chat-page p") + 200]
    if "white-space: normal" not in (nowrap_override or ""):
        print("ERROR: the global p { white-space: nowrap } is not overridden, so chat text cannot wrap at all: {!r}".format(nowrap_override))
        failed = True

    code = _extract_css_rule(styles, ".chat-bubble code")
    if code is None or "overflow-wrap: anywhere" not in code:
        print("ERROR: inline code, where the unbreakable tokens live, still cannot wrap: {!r}".format(code))
        failed = True

    # A block that scrolls internally must not be able to widen its bubble instead.
    pre = _extract_css_rule(styles, ".chat-bubble pre,\n.chat-tool-row pre,\n.chat-confirm-card pre")
    if pre is None:
        # The grouped selector may be normalised differently; fall back to a targeted search.
        pre = styles[styles.find(".chat-bubble pre") : styles.find(".chat-bubble code")]
    if "max-width: 100%" not in (pre or ""):
        print("ERROR: a pre block is uncapped, so overflow-x never engages and it widens the bubble: {!r}".format(pre))
        failed = True

    table = _extract_css_rule(styles, ".chat-bubble table")
    if table is None or "max-width: 100%" not in table or "overflow-x: auto" not in table:
        print("ERROR: a wide table can still drag the bubble wider than the window: {!r}".format(table))
        failed = True

    transcript = _extract_css_rule(styles, "#chat-transcript")
    if transcript is None or "overflow-x: hidden" not in transcript:
        print("ERROR: the transcript still scrolls horizontally, which is the symptom this fixes: {!r}".format(transcript))
        failed = True
    if transcript and "overflow-y: auto" not in transcript:
        print("ERROR: the transcript no longer scrolls vertically: {!r}".format(transcript))
        failed = True

    return failed


def test_tool_rows_show_a_status_marker_and_wrap_their_output(my_predbat):
    """Tool calls carry a status marker, and their output wraps instead of one clipped line.

    A <pre> does not wrap by default, and every tool returns a single-line JSON preview, so an
    expanded tool showed one line with a scrollbar under it and the rest of the text unreachable.
    Tool and approval output now wraps; code blocks inside an assistant bubble keep horizontal
    scrolling, where the author's line breaks matter more.

    The marker is grey while the call is in flight, then a tick or a cross, so the outcome is
    readable without expanding anything.

    Mutation checks: dropping white-space: pre-wrap, or the setToolStatus call, each fails below.
    """
    failed = False
    print("**** Testing tool status markers and output wrapping ****")
    styles = web_chat.get_chat_styles()
    script = web_chat.get_chat_script()

    # Wrapping, on tool and approval output only.
    wrap_rule = _extract_css_rule(styles, ".chat-tool-result pre,\n.chat-confirm-card pre")
    if wrap_rule is None:
        wrap_rule = styles[styles.find(".chat-tool-result pre") : styles.find(".chat-bubble pre")]
    if "white-space: pre-wrap" not in (wrap_rule or ""):
        print("ERROR: tool output does not wrap, so it stays one clipped line: {!r}".format(wrap_rule))
        failed = True
    if "overflow-y: auto" not in (wrap_rule or ""):
        print("ERROR: wrapped tool output has no vertical scroll, so a long result has no bound: {!r}".format(wrap_rule))
        failed = True

    # Three distinct states, and all three must be reachable.
    for marker in ("TOOL_STATUS_PENDING", "TOOL_STATUS_OK", "TOOL_STATUS_ERROR"):
        if marker not in script:
            print("ERROR: {} is not defined, so a tool cannot show that state".format(marker))
            failed = True

    start = _extract_function_body(script, "appendToolStart")
    if start is None or "TOOL_STATUS_PENDING" not in start:
        print("ERROR: a tool call does not start in the pending state: {!r}".format(start))
        failed = True
    # textContent, not innerHTML: the marker must not go through markup.
    if start and "status.textContent" not in start:
        print("ERROR: the status marker is not set via textContent: {!r}".format(start))
        failed = True

    end = _extract_function_body(script, "appendToolEnd")
    if end is None or "setToolStatus" not in end:
        print("ERROR: a finished tool never updates its status marker: {!r}".format(end))
        failed = True

    resolve = _extract_function_body(script, "setToolStatus")
    if resolve is None or "TOOL_STATUS_OK" not in resolve or "TOOL_STATUS_ERROR" not in resolve:
        print("ERROR: setToolStatus() cannot show both outcomes: {!r}".format(resolve))
        failed = True

    for css_class in (".chat-tool-status-pending", ".chat-tool-status-ok", ".chat-tool-status-error"):
        if css_class not in styles:
            print("ERROR: {} has no styling, so the states are not visually distinct".format(css_class))
            failed = True

    return failed


def test_no_thinking_counter_while_an_approval_is_outstanding(my_predbat):
    """The waiting indicator is suppressed while a turn is parked on an approval.

    A turn awaiting approval is genuinely still active, so switching to it showed "thinking" with
    a counter climbing beside the Approve card. Nothing was thinking: the model was blocked on an
    answer only the user could give, and the elapsed time being counted was the user's, not the
    model's. The card itself states the situation far better than a counter contradicting it.

    The counter must also actually stop, not merely be hidden - a live setInterval ticking against
    a removed element is the leak stopThinkingTimer() exists to prevent.

    Mutation checks: dropping the awaitingApproval() guard, or the clearThinkingBubble() call when
    a card appears, each fails below.
    """
    failed = False
    print("**** Testing no thinking counter while an approval is outstanding ****")
    script = web_chat.get_chat_script()

    guard = _extract_function_body(script, "awaitingApproval")
    if guard is None:
        print("ERROR: there is no awaitingApproval() to suppress the indicator")
        return True
    if "confirmCards" not in guard:
        print("ERROR: awaitingApproval() does not consult the outstanding cards: {!r}".format(guard))
        failed = True

    show = _extract_function_body(script, "showThinkingBubble")
    if show is None or "awaitingApproval" not in show:
        print("ERROR: showThinkingBubble() still shows the indicator during an approval: {!r}".format(show))
        failed = True
    # Hiding is not enough - the interval has to stop, or it ticks against a removed element.
    if show and "clearThinkingBubble" not in show:
        print("ERROR: the indicator is hidden but its timer keeps running: {!r}".format(show))
        failed = True

    appended = _extract_function_body(script, "appendConfirmCard")
    if appended is None or "clearThinkingBubble" not in appended:
        print("ERROR: a turn asking for approval does not drop the indicator: {!r}".format(appended))
        failed = True

    # And it comes back once the user answers, or the turn looks stalled from then on.
    resolved = _extract_function_body(script, "resolveConfirmCard")
    if resolved is None or "showThinkingBubble" not in resolved:
        print("ERROR: the indicator never returns after an approval is answered: {!r}".format(resolved))
        failed = True

    return failed


def test_model_picker_free_only_filter(my_predbat):
    """The picker filters to free models by default, and the filter can be turned off.

    The live catalogue is 388 models of which 21 are free, so the default keeps the list
    browsable and keeps a user from picking something billable by accident. Unticking shows
    everything.

    Which models are free is decided by the server, not from the quoted price here: only that side
    knows what the endpoint is. Reading it from the price - which this did - meant a local endpoint
    publishing no pricing had every model treated as not-free, so the filter emptied the picker on
    an Ollama server where everything is free. With the box ticked by default, that was the first
    thing a new Ollama user saw. See is_free_model() in chat.py for the rule itself.

    Mutation checks: defaulting the filter off, dropping the current-model exemption, or letting
    a non-free model through, each fails below.
    """
    failed = False
    print("**** Testing the model picker's free-only filter ****")
    script = web_chat.get_chat_script()
    styles = web_chat.get_chat_styles()

    free_check = _extract_function_body(script, "isFreeModel")
    if free_check is None:
        print("ERROR: there is no isFreeModel() to filter on")
        return True
    # Anchored to the server's own answer. Deriving it here from the price is the bug: a local
    # endpoint quotes none, and "no price" is not "not free".
    if "model.free" not in free_check or "formatModelPrice" in free_check:
        print("ERROR: isFreeModel() does not take the server's answer: {!r}".format(free_check))
        failed = True

    default_read = _extract_function_body(script, "readFreeOnly")
    if default_read is None:
        print("ERROR: there is no readFreeOnly() to seed the filter")
        return True
    # Default ON: the stored value is only consulted to turn it off.
    if "!== '0'" not in default_read:
        print("ERROR: the filter does not default to on: {!r}".format(default_read))
        failed = True
    if "catch" not in default_read:
        print("ERROR: readFreeOnly() does not survive blocked site data: {!r}".format(default_read))
        failed = True

    # Anchored to the filter expression itself. Loose checks for "state.freeOnly" or
    # "effectiveModel()" pass on other uses in the same function - the empty-state message and the
    # current-row highlight - so deleting the filter left them green.
    render = _extract_function_body(script, "renderModelResults")
    if render is None or "!isFreeModel(model)" not in render:
        print("ERROR: the result list does not filter out paid models: {!r}".format(render))
        failed = True
    # The model in use must stay listed, or the picker looks as though it lost its own setting.
    if render and "model.id !== effectiveModel()" not in render:
        print("ERROR: the current model is not exempt from the filter: {!r}".format(render))
        failed = True

    row = _extract_function_body(script, "appendFreeOnlyRow")
    if row is None:
        print("ERROR: there is no checkbox to turn the filter off")
        return True
    if "checkbox" not in row:
        print("ERROR: the filter control is not a checkbox: {!r}".format(row))
        failed = True
    # mousedown, like the result rows: a click would blur the search box and close the list first.
    # Anchored to this listener specifically - "mousedown" alone appears elsewhere in the picker.
    if "label.addEventListener('mousedown'" not in script or "preventDefault" not in row:
        print("ERROR: the checkbox is wired on click, so the list closes before it registers: {!r}".format(row))
        failed = True
    if "localStorage" not in row:
        print("ERROR: the choice is not remembered: {!r}".format(row))
        failed = True

    if ".chat-model-free-only" not in styles:
        print("ERROR: the filter row has no styling")
        failed = True

    return failed


def test_busy_banner_only_points_at_another_conversation(my_predbat):
    """The "switch to it" banner appears only when the running turn is somewhere else.

    It exists to say a reply is happening in a conversation the user is not looking at, and to
    offer a way there. On the conversation already open it was describing the transcript directly
    below it and offering to switch to where the user already was.

    Mutation check: calling showBanner() unconditionally fails this.
    """
    failed = False
    print("**** Testing the busy banner only points elsewhere ****")
    script = web_chat.get_chat_script()

    body = _extract_function_body(script, "setBusy")
    if body is None:
        print("ERROR: there is no setBusy() to inspect")
        return True
    if "state.conversation" not in body:
        print("ERROR: setBusy() does not compare the busy conversation with the open one: {!r}".format(body))
        failed = True
    # Both outcomes must be reachable: shown for another conversation, hidden for this one.
    if "showBanner" not in body or "hideBanner" not in body:
        print("ERROR: setBusy() cannot both show and hide the banner: {!r}".format(body))
        failed = True

    return failed


# -------------------------------------------------------------------------------------------------
# Provider settings.
# -------------------------------------------------------------------------------------------------

APPS_YAML_WITH_PROVIDERS = """pred_bat:
  # A comment that must survive a provider edit
  battery_size: 9.5
  chat:
    max_tool_rounds: 8
    providers:
      openrouter:
        type: openrouter
        url: 'https://openrouter.ai/api/v1'
        api_key: 'sk-or-secret-value'
        model: 'a/model'
"""

# The shape documented briefly before `providers:` existed: entries written straight into the chat
# block. extract_providers() still reads it, so the dialog has to migrate it rather than orphan it.
APPS_YAML_LOOSE_PROVIDERS = """pred_bat:
  battery_size: 9.5
  chat:
    openrouter:
      url: 'https://openrouter.ai/api/v1'
      api_key: 'sk-or-loose-value'
"""


class _ProbeAgent:
    """An agent stand-in that records what probe_models() was asked to dial."""

    def __init__(self, providers=None):
        """Present a provider list and capture probe calls."""
        self.providers = providers or [{"name": "openrouter", "type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "sk-or-secret-value", "model": "a/model", "settings": {"needs_key": True}, "configured": True}]
        self.calls = []

    async def probe_models(self, api_type, url, api_key):
        """Record the arguments and return a one-model catalogue."""
        self.calls.append({"type": api_type, "url": url, "api_key": api_key})
        return [{"id": "probe/model", "name": "Probe"}], None

    @staticmethod
    async def run_on_agent_loop(coro):
        """Await inline, standing in for the real cross-loop marshalling."""
        return await coro


def _apps_yaml_fixture(contents):
    """Write an apps.yaml into a temporary directory and return its path."""
    directory = tempfile.mkdtemp(prefix="predbat_chat_providers_")
    path = os.path.join(directory, "apps.yaml")
    with open(path, "w") as handle:
        handle.write(contents)
    return path


def _run_inline_on_agent(agent):
    """Let an agent's run_on_agent_loop await inline, as if its own loop were already running.

    The real one marshals onto the component's loop and refuses until that loop exists, which is
    correct for the component and useless for a test - so the marshalling is replaced while the
    coroutines it would have carried (apply_provider_block, and everything under it) stay real.
    """

    async def inline(coro):
        """Await the coroutine on this loop."""
        return await coro

    agent.run_on_agent_loop = inline
    return agent


def _save_providers(page, providers, active=None):
    """POST a provider list to the save route and return (status, decoded body)."""
    response = asyncio.run(page.html_chat_providers_post(FakeRequest(body={"providers": providers, "active": active})))
    return response.status, json.loads(response.text)


def test_provider_list_route_never_hands_a_key_to_the_browser(my_predbat):
    """/chat/providers reports that a key is set without ever sending its value.

    The dialog needs to show 'key saved' and to let a key be replaced. Neither needs the key
    itself, and a route that returned it would put a live credential into a page, into the
    browser's memory, and into anything logging the response - for no gain at all.
    """
    failed = False
    print("**** Testing the /chat/providers list route ****")

    agent = _make_agent(my_predbat, providers={"openrouter": {"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "sk-or-secret-value", "model": "a/model"}})
    page = _make_web(my_predbat, agent=agent).chat_page
    response = asyncio.run(page.html_chat_providers(FakeRequest()))
    if response.status != 200:
        print("ERROR: /chat/providers returned {}, expected 200".format(response.status))
        failed = True
    if "sk-or-secret-value" in response.text:
        print("ERROR: the API key was sent to the browser: {!r}".format(response.text))
        failed = True
    body = json.loads(response.text)
    entries = body.get("providers") or []
    if len(entries) != 1 or entries[0].get("name") != "openrouter":
        print("ERROR: unexpected provider list: {}".format(entries))
        failed = True
    elif not entries[0].get("has_key") or not entries[0].get("configured") or not entries[0].get("active"):
        print("ERROR: the configured provider should report has_key/configured/active: {}".format(entries[0]))
        failed = True
    elif "api_key" in entries[0]:
        print("ERROR: the summary carries an api_key field at all: {}".format(entries[0]))
        failed = True

    # The type list is what populates the dialog's dropdown and prefills its URL and model.
    types = {entry["type"]: entry for entry in body.get("types") or []}
    if "openrouter" not in types or "ollama" not in types:
        print("ERROR: the provider types offered are missing openrouter/ollama: {}".format(sorted(types)))
        failed = True
    else:
        # The Ollama form is prefilled with Ollama's own cloud rather than localhost. localhost is
        # the right reading of an existing entry that names no url - which is why
        # PROVIDER_DEFAULT_URLS still says so - but it is almost never right for a new install,
        # because Predbat runs inside its Home Assistant container where localhost is the
        # container. The note is what makes that a suggestion rather than a trap.
        if types["ollama"]["url"] != "https://ollama.com/v1" or types["ollama"]["model"] != "gpt-oss:120b":
            print("ERROR: the ollama form is not prefilled with the cloud endpoint: {}".format(types["ollama"]))
            failed = True
        if "localhost" not in types["ollama"].get("note", ""):
            print("ERROR: nothing tells the user how to point Ollama at their own server: {}".format(types["ollama"].get("note")))
            failed = True
        # Resolution of an existing apps.yaml entry is deliberately unchanged, so an install that
        # relies on the localhost fallback today keeps working.
        if PROVIDER_DEFAULT_URLS["ollama"] != "http://localhost:11434/v1":
            print("ERROR: the apps.yaml fallback for ollama changed, which repoints working installs")
            failed = True
        if types["openrouter"].get("note"):
            print("ERROR: a type with nothing to explain carries a note anyway: {}".format(types["openrouter"]))
            failed = True
        if types["ollama"]["needs_key"] or not types["openrouter"]["needs_key"]:
            print("ERROR: needs_key is wrong for ollama/openrouter: {}".format(types))
            failed = True
        # A type with no real default offers none. The agent's own fallback is OpenRouter's
        # endpoint, which would prefill the form for a generic endpoint with a URL that has
        # nothing to do with it and is plausible enough to be saved by mistake.
        for name in ("local", "openai"):
            if types.get(name, {}).get("url") == "https://openrouter.ai/api/v1":
                print("ERROR: the {} type prefills OpenRouter's endpoint: {}".format(name, types.get(name)))
                failed = True
        if types["local"]["url"]:
            print("ERROR: 'local' has no sensible default URL and should offer none: {}".format(types["local"]))
            failed = True
    return failed


def test_provider_save_keeps_a_key_the_dialog_never_saw(my_predbat):
    """Editing a provider's URL leaves its API key in apps.yaml untouched.

    The browser is never given the key, so it cannot send it back; a save that took the empty key
    field at face value would therefore wipe the credential of every provider on every edit. An
    absent key means 'keep', and this is the test that says so.
    """
    failed = False
    print("**** Testing that a provider edit preserves its key ****")

    path = _apps_yaml_fixture(APPS_YAML_WITH_PROVIDERS)
    original = web_chat.APPS_YAML_PATH
    web_chat.APPS_YAML_PATH = path
    try:
        agent = _run_inline_on_agent(_make_agent(my_predbat, providers={"openrouter": {"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "sk-or-secret-value", "model": "a/model"}}))
        page = _make_web(my_predbat, agent=agent).chat_page

        status, body = _save_providers(page, [{"name": "openrouter", "type": "openrouter", "url": "https://openrouter.ai/api/v1/", "model": "b/model", "api_key": None, "original_name": "openrouter"}], active="openrouter")
        if status != 200 or not body.get("ok"):
            print("ERROR: saving a provider edit failed: {} {}".format(status, body))
            failed = True
        written = open(path).read()
        if "sk-or-secret-value" not in written:
            print("ERROR: the API key was lost when the URL was edited:\n{}".format(written))
            failed = True
        if "b/model" not in written:
            print("ERROR: the new model was not written:\n{}".format(written))
            failed = True
        if "A comment that must survive" not in written or "battery_size" not in written:
            print("ERROR: the rest of apps.yaml did not survive the write:\n{}".format(written))
            failed = True
        if "max_tool_rounds" not in written:
            print("ERROR: other chat settings were dropped from the block:\n{}".format(written))
            failed = True
        # The live agent adopts the change without waiting for a restart.
        if agent.providers[0]["model"] != "b/model":
            print("ERROR: the agent did not adopt the saved providers: {}".format(agent.providers))
            failed = True
        if (my_predbat.args.get("chat") or {}).get("providers", {}).get("openrouter", {}).get("model") != "b/model":
            print("ERROR: the live args were not updated: {}".format(my_predbat.args.get("chat")))
            failed = True

        # Renaming has nothing to preserve in place, so the key has to be carried across
        # explicitly - the case that silently emptied the credential before it was handled.
        status, body = _save_providers(page, [{"name": "my-router", "type": "openrouter", "url": "https://openrouter.ai/api/v1", "model": "b/model", "api_key": None, "original_name": "openrouter"}], active="my-router")
        if status != 200:
            print("ERROR: renaming a provider failed: {} {}".format(status, body))
            failed = True
        written = open(path).read()
        if "my-router" not in written or "sk-or-secret-value" not in written:
            print("ERROR: renaming a provider lost its key:\n{}".format(written))
            failed = True
        if agent.active_provider != "my-router":
            print("ERROR: the renamed provider did not become active: {}".format(agent.active_provider))
            failed = True

        # A typed key replaces the stored one.
        status, body = _save_providers(page, [{"name": "my-router", "type": "openrouter", "url": "https://openrouter.ai/api/v1", "model": "b/model", "api_key": "sk-or-replacement", "original_name": "my-router"}], active="my-router")
        written = open(path).read()
        if "sk-or-replacement" not in written or "sk-or-secret-value" in written:
            print("ERROR: a typed key did not replace the stored one:\n{}".format(written))
            failed = True

        # Adding a second provider, and making it the active one.
        status, body = _save_providers(
            page,
            [
                {"name": "my-router", "type": "openrouter", "url": "https://openrouter.ai/api/v1", "model": "b/model", "api_key": None, "original_name": "my-router"},
                {"name": "ollama", "type": "ollama", "url": "http://localhost:11434/v1", "model": "gpt-oss:20b", "api_key": None, "original_name": "ollama"},
            ],
            active="ollama",
        )
        if status != 200:
            print("ERROR: adding a second provider failed: {} {}".format(status, body))
            failed = True
        if agent.active_provider != "ollama" or agent.store.get_selected_provider() != "ollama":
            print("ERROR: the chosen provider was not made active and remembered: {} / {}".format(agent.active_provider, agent.store.get_selected_provider()))
            failed = True

        # Removing one: the list posted is the whole truth, so what is missing is deleted.
        status, body = _save_providers(page, [{"name": "ollama", "type": "ollama", "url": "http://localhost:11434/v1", "model": "gpt-oss:20b", "api_key": None, "original_name": "ollama"}], active="ollama")
        written = open(path).read()
        if "my-router" in written or "sk-or-replacement" in written:
            print("ERROR: a removed provider was left in apps.yaml:\n{}".format(written))
            failed = True
        if len(agent.providers) != 1:
            print("ERROR: the agent still holds the removed provider: {}".format(agent.providers))
            failed = True
    finally:
        web_chat.APPS_YAML_PATH = original
    return failed


def test_provider_save_does_not_rewrap_long_values(my_predbat):
    """A long API key stays on its own line rather than being folded onto the next one.

    ruamel defaults to a line width of 80. An OpenRouter key is 73 characters, so at any realistic
    indent `api_key: sk-or-v1-...` is over that and gets written as a bare `api_key:` with the
    value folded onto a following, more-indented line. It reads back as the same string, so
    nothing breaks - but every long value in the file is re-wrapped by a save that meant to change
    one setting, and a credential written that way looks mangled to anyone opening the file.
    """
    failed = False
    print("**** Testing that saving providers does not re-wrap long values ****")

    long_key = "sk-or-v1-" + "b" * 64
    # Written unquoted, which is the shape that actually folds: preserve_quotes keeps a quoted
    # scalar as it found it and ruamel never wraps those, so a quoted fixture would pass this test
    # with or without the width set - and a real apps.yaml holds the key plain.
    path = _apps_yaml_fixture(APPS_YAML_WITH_PROVIDERS.replace("'sk-or-secret-value'", long_key))
    original = web_chat.APPS_YAML_PATH
    web_chat.APPS_YAML_PATH = path
    try:
        agent = _run_inline_on_agent(_make_agent(my_predbat, providers={"openrouter": {"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": long_key, "model": "a/model"}}))
        page = _make_web(my_predbat, agent=agent).chat_page
        status, body = _save_providers(page, [{"name": "openrouter", "type": "openrouter", "url": "https://openrouter.ai/api/v1", "model": "b/model", "api_key": None, "original_name": "openrouter"}], active="openrouter")
        if status != 200:
            print("ERROR: the save failed: {} {}".format(status, body))
            failed = True
        written = open(path).read()
        # Asserted against the api_key line itself rather than the whole file: preserve_quotes
        # keeps the fixture's own quoting, so matching a bare "api_key: <key>" would be testing
        # how the value is quoted instead of whether it was folded onto the next line.
        key_lines = [line for line in written.splitlines() if "api_key" in line]
        if len(key_lines) != 1 or long_key not in key_lines[0]:
            print("ERROR: the key was folded off its own line:\n{}".format(written))
            failed = True
        # And it still means the same thing once read back.
        reloaded = YAML().load(written)
        stored = reloaded["pred_bat"]["chat"]["providers"]["openrouter"]["api_key"]
        if stored != long_key:
            print("ERROR: the key did not survive the round trip: {!r}".format(stored))
            failed = True
    finally:
        web_chat.APPS_YAML_PATH = original
    return failed


def test_provider_save_migrates_the_loose_block_without_losing_its_key(my_predbat):
    """Saving folds providers written directly in the chat block under `providers:`, key and all.

    Once `providers:` exists, extract_providers() stops reading the loose entries - so leaving
    them behind would present a user with settings that are visibly in their file and silently
    ignored. Migrating them means their key has to be re-written rather than left in place, which
    is the one case where the file cannot preserve it for us.
    """
    failed = False
    print("**** Testing migration of loosely-written providers ****")

    path = _apps_yaml_fixture(APPS_YAML_LOOSE_PROVIDERS)
    original = web_chat.APPS_YAML_PATH
    web_chat.APPS_YAML_PATH = path
    try:
        agent = _make_agent(my_predbat, providers=None)
        # Deliberately the loose shape: provider entries as direct members of the chat block.
        agent.initialize({"openrouter": {"url": "https://openrouter.ai/api/v1", "api_key": "sk-or-loose-value"}})
        _run_inline_on_agent(agent)
        if not agent.providers or agent.providers[0]["api_key"] != "sk-or-loose-value":
            print("ERROR: the fixture did not produce a loose provider: {}".format(agent.providers))
            return True
        page = _make_web(my_predbat, agent=agent).chat_page

        status, body = _save_providers(page, [{"name": "openrouter", "type": "openrouter", "url": "https://openrouter.ai/api/v1", "model": "", "api_key": None, "original_name": "openrouter"}], active="openrouter")
        if status != 200:
            print("ERROR: saving a migrated provider failed: {} {}".format(status, body))
            failed = True
        written = open(path).read()
        if "providers:" not in written:
            print("ERROR: the provider was not moved under providers:\n{}".format(written))
            failed = True
        if "sk-or-loose-value" not in written:
            print("ERROR: migrating the loose provider lost its key:\n{}".format(written))
            failed = True
        # One provider, not two: the loose copy must be gone rather than shadowed.
        if written.count("openrouter:") != 1:
            print("ERROR: the loose entry was left behind alongside the migrated one:\n{}".format(written))
            failed = True
    finally:
        web_chat.APPS_YAML_PATH = original
    return failed


def test_provider_save_refuses_bad_input_before_touching_the_file(my_predbat):
    """Every entry is validated before apps.yaml is opened, so one bad entry writes nothing.

    The name becomes a YAML mapping key and the type selects behaviour the agent relies on, so
    neither can be whatever a caller sends. Validating up front rather than per entry is what
    stops a two-provider save writing the first and refusing the second.
    """
    failed = False
    print("**** Testing provider save validation ****")

    path = _apps_yaml_fixture(APPS_YAML_WITH_PROVIDERS)
    original = web_chat.APPS_YAML_PATH
    web_chat.APPS_YAML_PATH = path
    before = open(path).read()
    try:
        agent = _make_agent(my_predbat, providers={"openrouter": {"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "sk-or-secret-value", "model": "a/model"}})
        page = _make_web(my_predbat, agent=agent).chat_page
        good = {"name": "ollama", "type": "ollama", "url": "http://localhost:11434/v1", "model": "", "api_key": None, "original_name": "ollama"}

        cases = [
            ("an unknown provider type", [dict(good, type="skynet")], None),
            ("a name that is not a safe YAML key", [dict(good, name="oh: no")], None),
            ("an empty name", [dict(good, name="  ")], None),
            ("a URL with no scheme", [dict(good, url="localhost:11434")], None),
            ("two providers with the same name", [good, dict(good, original_name="other")], None),
            ("an active provider that is not in the list", [good], "missing"),
            ("providers that are not a list", None, None),
        ]
        for label, providers, active in cases:
            body = {"providers": providers, "active": active}
            response = asyncio.run(page.html_chat_providers_post(FakeRequest(body=body)))
            if response.status != 400:
                print("ERROR: {} was accepted with status {}: {}".format(label, response.status, response.text))
                failed = True
            elif not json.loads(response.text).get("error"):
                print("ERROR: {} was refused without saying why: {}".format(label, response.text))
                failed = True

        if open(path).read() != before:
            print("ERROR: a refused save still rewrote apps.yaml:\n{}".format(open(path).read()))
            failed = True
        # And the live configuration is untouched too.
        if agent.providers[0]["url"] != "https://openrouter.ai/api/v1":
            print("ERROR: a refused save changed the live providers: {}".format(agent.providers))
            failed = True
    finally:
        web_chat.APPS_YAML_PATH = original
    return failed


def test_probe_route_uses_the_stored_key_rather_than_asking_the_browser(my_predbat):
    """Probing an existing provider reuses its saved key; a typed key wins over it.

    This is what lets the dialog list an existing provider's models without the key ever leaving
    the server. A probe with no key and no name gets no key at all, which is correct for a local
    endpoint being set up for the first time.
    """
    failed = False
    print("**** Testing the /chat/providers/models probe route ****")

    agent = _ProbeAgent()
    page = _make_web(my_predbat, agent=agent).chat_page

    response = asyncio.run(page.html_chat_provider_models(FakeRequest(body={"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": None, "name": "openrouter"})))
    if response.status != 200:
        print("ERROR: the probe route returned {}".format(response.status))
        failed = True
    if json.loads(response.text).get("models") != [{"id": "probe/model", "name": "Probe"}]:
        print("ERROR: the probe result was not returned: {}".format(response.text))
        failed = True
    if not agent.calls or agent.calls[-1]["api_key"] != "sk-or-secret-value":
        print("ERROR: the probe did not reuse the stored key: {}".format(agent.calls))
        failed = True

    asyncio.run(page.html_chat_provider_models(FakeRequest(body={"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "typed-key", "name": "openrouter"})))
    if agent.calls[-1]["api_key"] != "typed-key":
        print("ERROR: a typed key did not take priority: {}".format(agent.calls[-1]))
        failed = True

    asyncio.run(page.html_chat_provider_models(FakeRequest(body={"type": "ollama", "url": "http://localhost:11434/v1", "api_key": None, "name": ""})))
    if agent.calls[-1]["api_key"] is not None:
        print("ERROR: an unnamed provider was given somebody else's key: {}".format(agent.calls[-1]))
        failed = True

    response = asyncio.run(page.html_chat_provider_models(FakeRequest(body={"type": "skynet", "url": "https://example.com/v1"})))
    if response.status != 400:
        print("ERROR: an unknown provider type was probed anyway: {}".format(response.status))
        failed = True
    return failed


def test_active_provider_is_remembered_across_a_restart(my_predbat):
    """The chosen provider survives the restart that saving apps.yaml can cause.

    Held only in memory it would be undone by the very act of choosing it, since a provider change
    rewrites apps.yaml. A remembered name that no longer exists falls back to the first usable
    entry rather than leaving the agent pointing at nothing.
    """
    failed = False
    print("**** Testing that the active provider is remembered ****")

    block = {"providers": {"openrouter": {"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "k"}, "ollama": {"type": "ollama", "url": "http://localhost:11434/v1"}}}
    agent = _make_agent(my_predbat, providers=block["providers"])

    agent.store.set_selected_provider("ollama")
    if agent.reload_providers(block) != "ollama" or agent.active_provider != "ollama":
        print("ERROR: the remembered provider was not selected: {}".format(agent.active_provider))
        failed = True
    if agent.base_url != "http://localhost:11434/v1" or agent.default_model != "gpt-oss:20b":
        print("ERROR: selecting a provider did not re-point the endpoint: {} / {}".format(agent.base_url, agent.default_model))
        failed = True

    # A remembered name that is no longer configured must not strand the agent.
    agent.store.set_selected_provider("gone")
    agent.reload_providers(block)
    if agent.active_provider not in ("openrouter", "ollama"):
        print("ERROR: a stale remembered provider left nothing active: {}".format(agent.active_provider))
        failed = True

    # And it is written into the index that survives a restart, not just held in the object.
    payload = json.dumps({"selected_provider": agent.store.selected_provider})
    if "gone" not in payload:
        print("ERROR: the selected provider is not part of the saved index payload: {}".format(payload))
        failed = True
    return failed


def test_chat_page_uses_a_top_bar_and_a_settings_dialog(my_predbat):
    """The conversation list is a dropdown off the title, and the sidebar is gone.

    A 260px column is most of a phone's width for something looked at rarely. Asserting the
    sidebar is absent rather than only that the top bar is present is the point: leaving both in
    would give two conversation lists, each needing the pending badge and the rename and delete
    wiring kept in step.
    """
    failed = False
    print("**** Testing the Chat tab's top bar and settings dialog ****")

    body = web_chat.get_chat_body()
    for marker in ['id="chat-topbar"', 'id="chat-settings-open"', 'id="chat-new"', 'id="chat-title-button"', 'id="chat-rename"', 'id="chat-conv-panel"', 'id="chat-list"']:
        if marker not in body:
            print("ERROR: the top bar is missing {}".format(marker))
            failed = True
    if "chat-sidebar" in body or "chat-sidebar" in web_chat.get_chat_styles():
        print("ERROR: the sidebar is still present")
        failed = True

    for marker in ['id="chat-settings"', 'id="chat-provider-list"', 'id="chat-provider-add"', 'id="chat-provider-form"', 'id="chat-provider-fetch"', 'id="chat-settings-save"', 'id="chat-no-provider"']:
        if marker not in body:
            print("ERROR: the settings dialog is missing {}".format(marker))
            failed = True

    # The permission toggles moved out of the footer and into the dialog. Compared by position so
    # this cannot pass on a page that simply has both.
    if body.index('id="chat-toggles"') < body.index('id="chat-settings"'):
        print("ERROR: the permission toggles are still in the footer, not the settings dialog")
        failed = True
    if body.index('id="chat-model-wrap"') > body.index('id="chat-settings"'):
        print("ERROR: the model picker left the footer - it belongs there, not in Settings")
        failed = True
    for switch in web_chat.CHAT_STATUS_SWITCHES:
        if 'data-switch="{}"'.format(switch) not in body:
            print("ERROR: the {} toggle is missing from the dialog".format(switch))
            failed = True

    styles = web_chat.get_chat_styles()
    if "#chat-settings.open" not in styles or "#chat-conv-panel.open" not in styles:
        print("ERROR: the dialog and dropdown have no open state in the CSS")
        failed = True
    # The dialog and the dropdown must sit above the transcript, or they open behind it.
    if "z-index: 200" not in styles or "z-index: 60" not in styles:
        print("ERROR: the dialog/dropdown are not stacked above the page")
        failed = True
    return failed


def test_settings_script_wires_the_provider_routes(my_predbat):
    """The client posts the whole provider list, carries original_name, and never holds a key.

    original_name is what the server looks a saved key up under, so a rename without it silently
    empties the credential. api_key starting as null is what makes an untouched field mean 'keep'.
    """
    failed = False
    print("**** Testing the settings dialog's client wiring ****")

    script = web_chat.get_chat_script()
    for marker in ["fetch('./chat/providers')", "fetch('./chat/providers/models'", "fetch('./chat/providers', { method: 'POST'", "function openSettings", "function saveSettings", "function fetchProviderModels", "function applyProviderForm"]:
        if marker not in script:
            print("ERROR: the settings script is missing {!r}".format(marker))
            failed = True
    # The type's setup note has to reach the form, not just the payload: it is the only thing that
    # explains a prefilled URL at the moment somebody is deciding whether to keep it.
    if 'id="chat-provider-url-note"' not in web_chat.get_chat_body():
        print("ERROR: the URL field has nowhere to show its note")
        failed = True
    open_form = script[script.index("function openProviderForm") : script.index("function closeProviderForm")]
    if "chat-provider-url-note" not in open_form or "defaults.note" not in open_form:
        print("ERROR: opening the form does not show the type's note: {!r}".format(open_form))
        failed = True
    # And not over an existing provider, where it would explain a default the user did not choose
    # against the URL they did.
    if "entry ?" not in open_form:
        print("ERROR: the note is shown when editing an existing provider, not only when adding")
        failed = True

    if "original_name: entry.original_name" not in script:
        print("ERROR: the save payload does not carry original_name, so a rename would lose the key")
        failed = True
    if "api_key: null" not in script:
        print("ERROR: providers loaded from the server should start with a null api_key")
        failed = True
    # The dropdown, and what closes it.
    for marker in ["function setConversationPanel", "function updateChatTitle", "function renameCurrentConversation", "chat-title-wrap"]:
        if marker not in script:
            print("ERROR: the conversation dropdown is missing {!r}".format(marker))
            failed = True
    # A provider where nothing is free - Ollama Cloud, for one - hits the free filter with no
    # search term, and "No free model matches \"\"" reads as a broken picker rather than a ticked
    # box. Saying how many models are behind the filter is what turns it into an instruction.
    if "Nothing this provider offers is free" not in script:
        print("ERROR: a provider with no free models has no empty-state message of its own")
        failed = True

    if "'chat-no-provider'" not in script:
        print("ERROR: nothing shows or hides the no-provider banner")
        failed = True

    # Writing apps.yaml restarts Predbat - hass.py watches the file and stops the process within
    # about five seconds of its mtime changing. The dialog has to say so in both places a user
    # looks: before saving, and after, when the page goes quiet and they are left wondering why.
    body = web_chat.get_chat_body()
    dialog = body[body.index('id="chat-settings"') :]
    if "restart" not in dialog.lower():
        print("ERROR: the settings dialog does not warn that saving restarts Predbat")
        failed = True
    save_body = script[script.index("function saveSettings") : script.index("function saveSettings") + 2000]
    notice = re.search(r"showChatNotice\('([^']*)'\)", save_body)
    if not notice or "restart" not in notice.group(1).lower():
        print("ERROR: the post-save message does not mention the restart: {}".format(notice and notice.group(1)))
        failed = True

    # A successful save closes the dialog and puts the user back on the conversation; a failed one
    # must leave it open, since there is something to fix in it. Checked by position rather than
    # presence: both branches live in the same function, and a closeSettings() that ran before the
    # error branch's return would dismiss the dialog on a failure too.
    if "closeSettings()" not in save_body:
        print("ERROR: a successful save does not close the settings dialog")
        failed = True
    else:
        error_branch = save_body.index("showSettingsError(result.error)")
        if save_body.index("closeSettings()") < error_branch:
            print("ERROR: the settings dialog is closed before the failure branch, so a failed save would dismiss it too")
            failed = True
        if "return;" not in save_body[error_branch : save_body.index("closeSettings()")]:
            print("ERROR: the failure branch does not return before the dialog is closed")
            failed = True

    # The notice cannot live in #chat-banner: reconcileBusy() rewrites that element on every
    # conversation refresh, which would wipe the message within a second or two of showing it.
    if "byId('chat-notice')" not in script:
        print("ERROR: the post-save notice does not use its own element")
        failed = True
    if "function clearChatNotice" not in script or "clearTimeout(noticeTimer)" not in script:
        print("ERROR: the notice timer is not cleared through a single helper")
        failed = True
    return failed


def test_every_route_answers_503_while_the_component_is_starting(my_predbat):
    """A route that marshals onto the component's loop answers 503, never 500, before it exists.

    The chat component is one of the last to start, and saving a provider deliberately restarts
    Predbat - the dialog even says the page will reconnect - so "reload the Chat tab while it is
    coming back" is a normal thing for a user to do rather than a rare race. Four routes answered
    500 to that, including html_chat_history, which every page load calls.

    Enforced two ways. The structural check is the one that lasts: no route may call
    run_on_agent_loop() itself, because a route added next year will not remember this rule but
    will copy a neighbour, and every neighbour now goes through _marshal(). The behavioural check
    proves _marshal actually produces a 503 rather than merely existing.
    """
    failed = False
    print("**** Testing that marshalling routes answer 503 while starting ****")

    source = inspect.getsource(web_chat.WebChat)
    for block in re.split(r"\n    (?=async def |def )", source):
        name = re.match(r"(?:async )?def (\w+)", block)
        if not name or name.group(1) == "_marshal":
            continue
        if "run_on_agent_loop" in block and "self._marshal" not in block:
            print("ERROR: {} marshals without going through _marshal, so it answers 500 while starting".format(name.group(1)))
            failed = True

    class StartingAgent:
        """An agent whose loop does not exist yet - exactly what the real one does before start."""

        active = None
        active_provider = "openrouter"
        default_model = "a/model"
        providers = []

        class store:
            """The store slice these routes touch before they reach the loop."""

            @staticmethod
            def get_meta(cid):
                """Every conversation resolves, so the 404 gate is not what answers here."""
                return {"id": cid, "title": "known", "usage_total": {}, "message_count": 0}

            @staticmethod
            def rename(cid, title):
                """Renaming is synchronous; only the flush after it needs the loop."""
                return title

            @staticmethod
            def set_model(cid, model, provider=None):
                """Setting the model is synchronous; only the flush needs the loop."""
                return True

            @staticmethod
            def set_selected_model(model, provider=None):
                """Remembering the choice is synchronous too."""
                return True

            @staticmethod
            def flush(cid=None):
                """Return a coroutine the route will try to marshal."""
                return asyncio.sleep(0)

            @staticmethod
            def delete(cid):
                """Return a coroutine the route will try to marshal."""
                return asyncio.sleep(0)

            @staticmethod
            def create(protect_id=None):
                """Return a coroutine the route will try to marshal."""
                return asyncio.sleep(0)

        @staticmethod
        async def run_on_agent_loop(coro):
            """Refuse, as the real one does until the component's own loop exists."""
            coro.close()
            raise AgentNotReadyError("The chat component has not finished starting")

        @staticmethod
        def events_since(cursor, conversation_id):
            """No events yet."""
            return [], 0, False

    page = _make_web(my_predbat, agent=StartingAgent()).chat_page
    cid = "aaaabbbbccccdddd"
    calls = [
        ("history", page.html_chat_history(FakeRequest(query={"conversation": cid}))),
        ("rename", page.html_chat_rename(FakeRequest(body={"id": cid, "title": "new"}))),
        ("delete", page.html_chat_delete(FakeRequest(body={"id": cid}))),
        ("model", page.html_chat_model(FakeRequest(body={"conversation": cid, "id": "a/model"}))),
        ("create", page.html_chat_create(FakeRequest())),
    ]
    for label, coro in calls:
        response = asyncio.run(coro)
        if response.status != 503:
            print("ERROR: /chat/{} answered {} while the component was starting, expected 503".format(label, response.status))
            failed = True
        elif "starting" not in json.loads(response.text).get("error", ""):
            print("ERROR: /chat/{} did not say why it refused: {}".format(label, response.text))
            failed = True
    return failed


def test_history_reports_the_override_only_for_its_own_provider(my_predbat):
    """The page is not handed a model override belonging to a provider that is not answering.

    The browser sets its current model straight from this field, so a route that returned the raw
    override would put an OpenRouter model into the picker the moment an Ollama conversation was
    opened - naming a model every turn would fail on, while resolve_model() quietly used another.
    """
    failed = False
    print("**** Testing that history reports an override only for its own provider ****")

    class ProviderAgent:
        """An agent on one provider, holding a conversation whose model was chosen on another."""

        active = None
        active_provider = "ollama"

        def __init__(self, model_provider):
            """Hold the provider the stored override should claim to belong to."""
            self.model_provider = model_provider
            agent = self

            class Store:
                """The slice of ConversationStore the history route reads."""

                @staticmethod
                def get_meta(cid):
                    """Return the one conversation, with its override and that override's owner."""
                    return {"id": cid, "title": "known", "model": "vendor/hosted-only", "model_provider": agent.model_provider, "usage_total": {"cost": 0}}

                @staticmethod
                def get_last_error(cid, message_count=None):
                    """No failed turn recorded."""
                    return None

                @staticmethod
                def get_approvals(cid):
                    """No approvals recorded."""
                    return []

                @staticmethod
                async def snapshot(cid):
                    """An empty transcript is enough - this test is about the model field."""
                    return []

            self.store = Store()

        @staticmethod
        async def run_on_agent_loop(coro):
            """Await inline."""
            return await coro

        @staticmethod
        def events_since(cursor, conversation_id):
            """No events, as a freshly opened conversation would report."""
            return [], 0, False

    page = _make_web(my_predbat, agent=ProviderAgent("openrouter")).chat_page
    response = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": "aaaabbbbccccdddd"})))
    body = json.loads(response.text)
    if body.get("model") is not None:
        print("ERROR: an override from another provider was handed to the page: {}".format(body.get("model")))
        failed = True

    # And it is still reported for the provider it does belong to - the filter must not swallow
    # every override, only the ones that are not this endpoint's.
    page.agent_override = ProviderAgent("ollama")
    response = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": "aaaabbbbccccdddd"})))
    if json.loads(response.text).get("model") != "vendor/hosted-only":
        print("ERROR: the override was dropped on its own provider: {}".format(response.text))
        failed = True
    return failed


def test_picker_drops_a_model_the_provider_does_not_serve(my_predbat):
    """After a provider switch the picker forgets a model the new endpoint has no idea about.

    The server stops sending it - the conversation override is per provider now - but the browser
    is still holding what it had before the switch, and it also cannot see the case the server
    cannot: a model genuinely gone from an endpoint that still remembers it. Falling back to "Pick
    a model to start" beats naming something every turn would fail on.
    """
    failed = False
    print("**** Testing that the picker drops a model the provider does not serve ****")

    script = web_chat.get_chat_script()
    if "function dropModelsTheProviderDoesNotServe" not in script:
        print("ERROR: nothing drops a model the provider does not serve")
        return True
    body = script[script.index("function dropModelsTheProviderDoesNotServe") : script.index("function populateModelPicker")]

    # Guarded on the catalogue being readable: with no list to check against, "not in the list"
    # means nothing, and blanking a configured model that works would be the worse mistake.
    if "state.catalogueAvailable" not in body:
        print("ERROR: models are dropped even when the catalogue could not be read: {!r}".format(body))
        failed = True
    for held in ("state.currentModel", "state.selectedModel"):
        if held not in body:
            print("ERROR: {} is not checked against the catalogue".format(held))
            failed = True
    if "modelOffered" not in body:
        print("ERROR: the check does not consult the offered models")
        failed = True

    # It has to run before the picker is drawn, or the picker shows what was just invalidated.
    load_body = script[script.index("function loadModels") : script.index("function loadModels") + 900]
    if "dropModelsTheProviderDoesNotServe()" not in load_body:
        print("ERROR: loading a catalogue does not re-check the held model")
        failed = True
    elif load_body.index("dropModelsTheProviderDoesNotServe()") > load_body.index("populateModelPicker("):
        print("ERROR: the picker is drawn before the stale model is dropped")
        failed = True
    return failed


def test_text_inputs_are_hinted_against_password_autofill(my_predbat):
    """No text field on the Chat tab invites a password manager to fill it.

    The page has a type=password input for the provider key, and that alone makes every text input
    on it a candidate username as far as macOS and the password managers are concerned - which is
    why the model search box was being offered keychain entries. autocomplete=off does not settle
    it, because it is deliberately ignored on anything thought to be a login field.

    Two parts, and the password field is the load-bearing one: autocomplete="new-password" says
    this is not a login, which is what stops a nearby text box being treated as its username.
    """
    failed = False
    print("**** Testing that text inputs are hinted against password autofill ****")

    body = web_chat.get_chat_body()
    text_inputs = re.findall(r'<input type="text"[^>]*>', body)
    if len(text_inputs) < 4:
        print("ERROR: expected the chat page to have several text inputs, found {}".format(len(text_inputs)))
        failed = True
    for field in text_inputs:
        for hint in ('autocomplete="off"', "data-1p-ignore", 'data-lpignore="true"', "data-bwignore", 'data-form-type="other"'):
            if hint not in field:
                print("ERROR: a text input is missing {}: {}".format(hint, field))
                failed = True

    password_inputs = re.findall(r'<input type="password"[^>]*>', body)
    if not password_inputs:
        print("ERROR: the provider key field is no longer a password input")
        failed = True
    for field in password_inputs:
        # "off" is what it used to say, and is precisely what does not work here: the browser reads
        # a password field with autocomplete off as a login it should still help with.
        if 'autocomplete="new-password"' not in field:
            print("ERROR: the key field does not declare itself as new-password: {}".format(field))
            failed = True
    return failed


def test_unavailable_catalogue_says_why(my_predbat):
    """When the model list cannot be fetched, the picker reports the reason, not just the fact.

    An unreachable URL, a rejected key and an endpoint serving nothing all reduced to the same
    "(catalogue unavailable)" note, leaving the user to work out which of three quite different
    problems they had. The reason is recorded where the failure happens and carried through to the
    note beside the picker.
    """
    failed = False
    print("**** Testing that an unavailable catalogue says why ****")

    class FailingAgent:
        """An agent whose catalogue fetch has failed, with a reason to report."""

        default_model = "gpt-oss:20b"
        active_provider = "ollama"
        store = _StubSelectionStore()
        catalogue_error = "Could not reach http://192.168.0.33:11434/v1: Cannot connect to host"

        @staticmethod
        async def list_models():
            """Return only the configured model, as a failed catalogue fetch leaves it."""
            return [{"id": "gpt-oss:20b", "name": "gpt-oss:20b (from apps.yaml)"}]

        @staticmethod
        async def run_on_agent_loop(coro):
            """Await inline."""
            return await coro

    page = _make_web(my_predbat, agent=FailingAgent()).chat_page
    response = asyncio.run(page.html_chat_models(FakeRequest()))
    body = json.loads(response.text)
    if body.get("catalogue_available") is not False:
        print("ERROR: a one-model catalogue should not report as available: {}".format(body))
        failed = True
    if body.get("catalogue_error") != FailingAgent.catalogue_error:
        print("ERROR: the reason was not passed on: {}".format(body.get("catalogue_error")))
        failed = True

    # A healthy agent that never set the attribute must not break the route.
    class BareAgent:
        """An agent predating catalogue_error entirely."""

        default_model = "a/model"
        active_provider = "openrouter"
        store = _StubSelectionStore()

        @staticmethod
        async def list_models():
            """Return two models, so the catalogue counts as available."""
            return [{"id": "a/model"}, {"id": "b/model"}]

        @staticmethod
        async def run_on_agent_loop(coro):
            """Await inline."""
            return await coro

    response = asyncio.run(page.html_chat_models(FakeRequest()))
    page.agent_override = BareAgent()
    response = asyncio.run(page.html_chat_models(FakeRequest()))
    if response.status != 200 or json.loads(response.text).get("catalogue_error") is not None:
        print("ERROR: an agent with no catalogue_error should report none: {}".format(response.text))
        failed = True

    script = web_chat.get_chat_script()
    if "state.catalogueError" not in script:
        print("ERROR: the client never reads the reason")
        failed = True

    # A failed catalogue is never cached, so the endpoint coming back is one request away - but
    # only if something asks. Opening the picker is when the user wants the list and when they
    # have just fixed the endpoint, so it retries then rather than staying stale until a reload.
    open_body = script[script.index("function openModelList") : script.index("function closeModelList")]
    if "state.catalogueAvailable" not in open_body or "loadModels()" not in open_body:
        print("ERROR: opening the picker does not retry a failed catalogue: {!r}".format(open_body))
        failed = True

    # And a reconnect - which is what a restart looks like from the browser - refreshes both the
    # provider list and the models, since either can have changed while the stream was down.
    stream_body = script[script.index("function openStream") : script.index("function openStream") + 1800]
    if "state.streamConnected" not in stream_body:
        print("ERROR: a reconnect is not told apart from a first connection")
        failed = True
    if "loadModels()" not in stream_body or "loadProviders()" not in stream_body:
        print("ERROR: a reconnect does not refresh the provider or model state")
        failed = True
    # Reset per source, or switching conversation looks like a reconnect and refetches every time.
    if "state.streamConnected = false" not in stream_body:
        print("ERROR: the reconnect flag is not reset per EventSource")
        failed = True
    note = script[script.index("function updateModelNote") : script.index("function updateModelNote") + 700]
    if "state.catalogueError ?" not in note:
        print("ERROR: the note does not prefer the reason over the bare 'unavailable': {!r}".format(note))
        failed = True
    return failed


def test_switching_provider_writes_nothing_and_restarts_nothing(my_predbat):
    """Choosing between configured providers is not a configuration change, so it costs no restart.

    Every provider is already in apps.yaml; which one answers is a preference, and routing it
    through the save route would rewrite the file, trip the watcher in hass.py and restart Predbat
    to change nothing about the file's contents. The choice is remembered in the conversation
    index instead, beside the remembered model.
    """
    failed = False
    print("**** Testing the provider switch route ****")

    path = _apps_yaml_fixture(APPS_YAML_WITH_PROVIDERS)
    original = web_chat.APPS_YAML_PATH
    web_chat.APPS_YAML_PATH = path
    before = open(path).read()
    try:
        providers = {"openrouter": {"type": "openrouter", "url": "https://openrouter.ai/api/v1", "api_key": "sk-or-secret-value"}, "ollama": {"type": "ollama", "url": "http://localhost:11434/v1"}}
        agent = _run_inline_on_agent(_make_agent(my_predbat, providers=providers))
        page = _make_web(my_predbat, agent=agent).chat_page

        response = asyncio.run(page.html_chat_provider_select(FakeRequest(body={"name": "ollama"})))
        if response.status != 200:
            print("ERROR: switching provider returned {}: {}".format(response.status, response.text))
            failed = True
        if agent.active_provider != "ollama" or agent.base_url != "http://localhost:11434/v1":
            print("ERROR: the switch did not re-point the agent: {} / {}".format(agent.active_provider, agent.base_url))
            failed = True
        if agent.store.get_selected_provider() != "ollama":
            print("ERROR: the switch was not remembered: {}".format(agent.store.get_selected_provider()))
            failed = True
        if open(path).read() != before:
            print("ERROR: switching provider rewrote apps.yaml, which would restart Predbat:\n{}".format(open(path).read()))
            failed = True

        response = asyncio.run(page.html_chat_provider_select(FakeRequest(body={"name": "not-a-provider"})))
        if response.status != 400:
            print("ERROR: an unknown provider was accepted: {} {}".format(response.status, response.text))
            failed = True
        if agent.active_provider != "ollama":
            print("ERROR: a refused switch changed the active provider anyway: {}".format(agent.active_provider))
            failed = True
    finally:
        web_chat.APPS_YAML_PATH = original
    return failed


def test_save_button_is_ghosted_until_something_changes(my_predbat):
    """Save is disabled while the dialog matches what was loaded.

    A save that writes an identical file still bumps its mtime, and hass.py restarts Predbat on
    mtime alone - so an enabled Save button on an untouched dialog offers the user a pointless
    restart, and one they would have no reason to expect from a button they pressed to change
    nothing. The snapshot includes api_key so that typing a replacement key counts as a change
    even though it alters no visible field.
    """
    failed = False
    print("**** Testing that Save is ghosted with nothing to save ****")

    script = web_chat.get_chat_script()
    for marker in ["function settingsSnapshot", "function updateSaveButton", "settings.baseline = settingsSnapshot()"]:
        if marker not in script:
            print("ERROR: the dirty check is missing {!r}".format(marker))
            failed = True
    snapshot = script[script.index("function settingsSnapshot") : script.index("function updateSaveButton")]
    if "entry.api_key" not in snapshot:
        print("ERROR: a newly typed API key would not count as a change: {!r}".format(snapshot))
        failed = True
    if "active" not in snapshot:
        print("ERROR: the snapshot ignores which provider is active")
        failed = True
    if "disabled = unchanged" not in script:
        print("ERROR: nothing actually disables the Save button")
        failed = True
    # Every mutation has to re-run the check, or the button stays ghosted over a real change.
    for mutator in ("function applyProviderForm", "function removeProvider"):
        body = script[script.index(mutator) : script.index(mutator) + 1800]
        if "updateSaveButton()" not in body:
            print("ERROR: {} does not re-check whether there is anything to save".format(mutator))
            failed = True
    return failed


def test_nothing_is_revealed_by_clearing_an_inline_display(my_predbat):
    """No element hidden by a stylesheet rule is re-shown by blanking its inline display.

    `node.style.display = ''` removes the inline style and hands the element straight back to the
    rule that hid it, so the show branch shows nothing. It reads exactly like working code, and
    the provider selector shipped that way: right condition, right element, permanently invisible.

    Enforced over the whole script rather than that one function, because the trap is in the idiom
    and not in any one use of it. Setting a real value ('block', 'inline-block') is fine, as is
    toggling a class - it is the empty string that is the bug.
    """
    failed = False
    print("**** Testing that nothing is shown by clearing an inline display ****")

    script = web_chat.get_chat_script()
    styles = web_chat.get_chat_styles()
    blanked = re.findall(r"\.style\.display = ((?:''|\"\")|[^;\n]*\?[^;\n]*''[^;\n]*)", script)
    if blanked:
        print("ERROR: an element is shown by clearing its inline display, which a stylesheet rule then undoes: {}".format(blanked))
        failed = True

    # The other half of the same rule: an id the stylesheet hides must have somewhere to be shown
    # from - a .visible/.open rule - or nothing in the script can reveal it at all.
    hidden_ids = set(re.findall(r"#([a-z-]+) \{\n(?:[^}]*?)display: none;", styles))
    # Setting a real display value is the other legitimate way to reveal one of these, and is what
    # the model list does, so those are not missing a rule - they simply do not need one.
    shown_inline = set(re.findall(r"byId\('([^']+)'\)\.style\.display = '[a-z-]+'", script))
    for element_id in sorted(hidden_ids - shown_inline):
        if element_id not in script:
            continue
        if not re.search(r"#" + element_id + r"\.(visible|open|dismissed)", styles):
            print("ERROR: #{} is hidden by the stylesheet with no rule that can show it again".format(element_id))
            failed = True
    return failed


def test_provider_selector_sits_in_the_footer_beside_the_model_picker(my_predbat):
    """Switching provider is a footer control, not a dialog one, and hides with nothing to choose.

    It belongs next to the model picker because the two are the same kind of choice and the model
    catalogue depends on the provider. Asserting the Settings dialog no longer carries a radio is
    the other half: two controls for one setting, one of which needed a Save and a restart, is
    what this replaced.
    """
    failed = False
    print("**** Testing the footer provider selector ****")

    body = web_chat.get_chat_body()
    if 'id="chat-provider-select"' not in body:
        print("ERROR: there is no provider selector")
        failed = True
    elif body.index('id="chat-provider-select"') < body.index('id="chat-footer"'):
        print("ERROR: the provider selector is not in the footer")
        failed = True
    if 'name="chat-active-provider"' in web_chat.get_chat_script():
        print("ERROR: the Settings dialog still carries its own active-provider radio")
        failed = True

    script = web_chat.get_chat_script()
    if "fetch('./chat/provider', { method: 'POST'" not in script:
        print("ERROR: the selector does not post to the switch route")
        failed = True
    # The trailing "(" matters: without it this matches changeProviderType, which is declared
    # earlier, and the slice below comes out empty and passes on nothing.
    select_body = script[script.index("function renderProviderSelect") : script.index("function changeProvider(")]
    # Shown with one provider too: it names the endpoint that is answering, and the endpoint the
    # model list beside it belongs to, which is worth knowing before there is anything to switch
    # between. Only a configuration with no providers at all has nothing to say.
    if "settings.providers.length > 0" not in select_body:
        print("ERROR: the selector is not shown whenever a provider exists: {!r}".format(select_body))
        failed = True
    # Asserting the condition is not enough - the first version of this shipped with the right
    # condition and a show branch that showed nothing, because it cleared the inline display of an
    # element the stylesheet hides by default. So check the mechanism actually reveals it.
    if "classList.toggle('visible'" not in select_body:
        print("ERROR: the selector is not revealed by its .visible class: {!r}".format(select_body))
        failed = True
    if "#chat-provider-select.visible" not in web_chat.get_chat_styles():
        print("ERROR: there is no .visible rule to reveal the provider selector")
        failed = True
    change_body = script[script.index("function changeProvider(") : script.index("function saveSettings")]
    if "loadModels()" not in change_body:
        print("ERROR: switching provider leaves the previous endpoint's models in the picker")
        failed = True
    return failed


def test_own_message_is_shown_without_waiting_for_the_server_echo(my_predbat):
    """Hitting send must render your own message immediately, not only when the SSE echo lands.

    The bubble was previously drawn solely by handleUser(), from the server's 'user' event. Miss
    that one event - createAndSend() sends before openStream() has run, and the history snapshot
    and event cursor are taken under different locks on different threads - and the message was
    gone from the transcript until a conversation switch rebuilt it from history, which is exactly
    what users hit. Drawing it on send removes the dependency entirely.

    The echo must then not draw it a second time, so handleUser() adopts the pending bubble when
    there is one, and still appends when there is not - a second browser watching the same
    conversation never sent anything and must render the message normally.
    """
    failed = False
    print("**** Testing own message renders without the server echo ****")
    script = web_chat.get_chat_script()

    send = _extract_function_body(script, "sendMessage")
    if send is None:
        print("ERROR: no sendMessage function found")
        return True
    if "appendBubble('user'" not in send:
        print("ERROR: sendMessage does not draw the user bubble itself: {!r}".format(send))
        failed = True

    handle = _extract_function_body(script, "handleUser")
    if handle is None:
        print("ERROR: no handleUser function found")
        return True
    # Must still append when nothing is pending, or a second browser shows no message at all.
    if "appendBubble" not in handle:
        print("ERROR: handleUser must still append when no local bubble is pending: {!r}".format(handle))
        failed = True
    if "pendingUserBubble" not in handle:
        print("ERROR: handleUser does not adopt the locally drawn bubble, so the echo duplicates it: {!r}".format(handle))
        failed = True

    # renderHistory() rebuilds the transcript from scratch; a pending marker surviving that would
    # make the next echo adopt a bubble that is no longer in the document.
    render = _extract_function_body(script, "renderHistory")
    if render is None or "pendingUserBubble" not in render:
        print("ERROR: renderHistory does not clear the pending user bubble: {!r}".format(render))
        failed = True

    # The send must not race the stream being opened for a brand new conversation.
    create = _extract_function_body(script, "createAndSend")
    if create is None:
        print("ERROR: no createAndSend function found")
        return True
    if "selectConversation(payload.id).then(" not in create:
        print("ERROR: createAndSend does not chain the send off the conversation load, so it still races openStream(): {!r}".format(create))
        failed = True

    # A send that never landed must take its optimistic bubble back down, or the transcript shows
    # a message the server never stored - the mirror of the bug this fixes, visible until a reload.
    send_fn = _extract_function_body(script, "doSend")
    if send_fn is None or "pendingUserBubble" not in send_fn:
        print("ERROR: doSend leaves the optimistic bubble up when the send fails: {!r}".format(send_fn))
        failed = True

    # selectConversation() has to hand the promise back for that chaining to be possible at all.
    select = _extract_function_body(script, "selectConversation")
    if select is None or "return loadConversationData(" not in select:
        print("ERROR: selectConversation does not return its load promise: {!r}".format(select))
        failed = True

    if not failed:
        print("✓ Test passed: your own message renders on send and the echo does not duplicate it")
    return failed


def run_web_chat_tests(my_predbat):
    """Run every Chat tab web layer test, returning True if any of them failed."""
    failed = False
    failed |= test_routes_always_registered_handlers_404_unconfigured(my_predbat)
    failed |= test_own_message_is_shown_without_waiting_for_the_server_echo(my_predbat)
    failed |= test_chat_routes_survive_the_real_phase_order(my_predbat)
    failed |= test_send_is_busy_and_unknown_is_404(my_predbat)
    failed |= test_delete_refuses_the_active_conversation(my_predbat)
    failed |= test_history_reads_via_snapshot_not_get_messages(my_predbat)
    failed |= test_history_snapshot_and_cursor_do_not_lose_a_concurrent_message(my_predbat)
    failed |= test_history_route_reports_last_prompt_tokens(my_predbat)
    failed |= test_sse_framing(my_predbat)
    failed |= test_markdown_escapes_before_transforming(my_predbat)
    failed |= test_markdown_tables_render_as_real_tables_with_scroll_wrapper(my_predbat)
    failed |= test_markdown_lists_group_consecutive_items_into_one_list(my_predbat)
    failed |= test_markdown_headings_render_h1_through_h6(my_predbat)
    failed |= test_markdown_fenced_code_skips_inline_and_line_rules(my_predbat)
    failed |= test_markdown_paragraphs_join_with_br_only_inside_the_paragraph_block(my_predbat)
    failed |= test_markdown_table_and_heading_css_uses_theme_variables_and_scrolls(my_predbat)
    failed |= test_nav_link_visibility(my_predbat)
    failed |= test_client_script_contract(my_predbat)
    failed |= test_chat_page_assembles_real_content(my_predbat)
    failed |= test_inner_html_sinks_only_ever_receive_escaped_content(my_predbat)
    failed |= test_stream_cursor_advances_from_every_event(my_predbat)
    failed |= test_dropped_connection_is_told_apart_from_a_real_error_frame(my_predbat)
    failed |= test_stream_reconnects_when_the_agent_is_replaced(my_predbat)
    failed |= test_model_catalogue(my_predbat)
    failed |= test_models_route_uses_agent_loop_and_reports_catalogue_availability(my_predbat)
    failed |= test_model_picker_script_wires_routes_and_persists_selection(my_predbat)
    failed |= test_chat_page_fills_the_window_without_an_outer_scrollbar(my_predbat)
    failed |= test_bubble_content_stays_inside_its_bubble(my_predbat)
    failed |= test_tool_rows_show_a_status_marker_and_wrap_their_output(my_predbat)
    failed |= test_no_thinking_counter_while_an_approval_is_outstanding(my_predbat)
    failed |= test_thinking_bubble_moves_to_the_end(my_predbat)
    failed |= test_chat_switch_defaults_and_mirror(my_predbat)
    failed |= test_model_picker_shows_prices(my_predbat)
    failed |= test_model_picker_free_only_filter(my_predbat)
    failed |= test_busy_banner_is_reconciled_against_the_server(my_predbat)
    failed |= test_busy_banner_only_points_at_another_conversation(my_predbat)
    failed |= test_context_counter_uses_the_last_turns_prompt_tokens_not_cumulative(my_predbat)
    failed |= test_stop_button_wired_to_cancel_with_turn_id(my_predbat)
    failed |= test_html_chat_cancel_requires_the_running_turn_id(my_predbat)
    failed |= test_chat_status_route(my_predbat)
    failed |= test_chat_history_reports_elapsed_seconds_for_an_active_turn(my_predbat)
    failed |= test_thinking_bubble_shown_on_busy_and_hidden_on_first_delta(my_predbat)
    failed |= test_thinking_bubble_element_is_reused_not_recreated(my_predbat)
    failed |= test_thinking_timer_interval_is_genuinely_cleared_not_merely_clearable(my_predbat)
    failed |= test_thinking_bubble_css_respects_theme_vars_and_reduced_motion(my_predbat)
    failed |= test_retry_event_wired_to_handle_retry(my_predbat)
    failed |= test_handle_retry_discards_pending_bubble_and_shows_the_reason(my_predbat)
    failed |= test_discard_pending_bubble_unconditionally_removes_the_bubble(my_predbat)
    failed |= test_retry_countdown_interval_is_genuinely_cleared(my_predbat)
    failed |= test_retry_status_element_takes_its_colour_from_theme_variables(my_predbat)
    failed |= test_provider_list_route_never_hands_a_key_to_the_browser(my_predbat)
    failed |= test_provider_save_keeps_a_key_the_dialog_never_saw(my_predbat)
    failed |= test_provider_save_does_not_rewrap_long_values(my_predbat)
    failed |= test_provider_save_migrates_the_loose_block_without_losing_its_key(my_predbat)
    failed |= test_provider_save_refuses_bad_input_before_touching_the_file(my_predbat)
    failed |= test_probe_route_uses_the_stored_key_rather_than_asking_the_browser(my_predbat)
    failed |= test_active_provider_is_remembered_across_a_restart(my_predbat)
    failed |= test_chat_page_uses_a_top_bar_and_a_settings_dialog(my_predbat)
    failed |= test_settings_script_wires_the_provider_routes(my_predbat)
    failed |= test_every_route_answers_503_while_the_component_is_starting(my_predbat)
    failed |= test_history_reports_the_override_only_for_its_own_provider(my_predbat)
    failed |= test_picker_drops_a_model_the_provider_does_not_serve(my_predbat)
    failed |= test_text_inputs_are_hinted_against_password_autofill(my_predbat)
    failed |= test_unavailable_catalogue_says_why(my_predbat)
    failed |= test_switching_provider_writes_nothing_and_restarts_nothing(my_predbat)
    failed |= test_save_button_is_ghosted_until_something_changes(my_predbat)
    failed |= test_provider_selector_sits_in_the_footer_beside_the_model_picker(my_predbat)
    failed |= test_nothing_is_revealed_by_clearing_an_inline_display(my_predbat)
    return failed
