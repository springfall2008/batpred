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
import re

from aiohttp import web as aiohttp_web

import web_chat
from chat import AgentNotReadyError
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
    for event in ["delta", "assistant", "tool_start", "tool_end", "confirm", "confirm_result", "usage", "title", "error", "done", "busy", "idle", "reload"]:
        if "'{}'".format(event) not in script and '"{}"'.format(event) not in script:
            print("ERROR: the client script does not handle the {!r} event".format(event))
            failed = True
    for endpoint in ["/chat/conversations", "/chat/history", "/chat/send", "/chat/stream", "/chat/confirm", "/chat/delete", "/chat/rename"]:
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
    if "<style" not in styles or "chat-sidebar" not in styles:
        print("ERROR: get_chat_styles() does not look like a real stylesheet: {!r}".format(styles[:200]))
        failed = True

    body = web_chat.get_chat_body()
    for element_id in ["chat-sidebar", "chat-list", "chat-new", "chat-banner", "chat-transcript", "chat-composer", "chat-input", "chat-footer", "chat-model", "chat-turn-usage", "chat-total-cost", "chat-privacy"]:
        if element_id not in body:
            print("ERROR: get_chat_body() is missing #{}".format(element_id))
            failed = True
    if "openrouter" not in body.lower():
        print("ERROR: the privacy banner does not name OpenRouter as the destination for tool results")
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
        return {"data": [{"id": "good/model", "name": "Good", "supported_parameters": ["tools", "temperature"]}, {"id": "bad/model", "name": "Bad", "supported_parameters": ["temperature"]}]}

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

    async def broken_catalogue():
        """Simulate an unreachable catalogue."""
        return None

    agent._fetch_model_catalogue = broken_catalogue
    fallback = asyncio.run(agent.list_models())
    if [entry["id"] for entry in fallback] != ["configured/model"]:
        print("ERROR: an unreachable catalogue should degrade to the configured model: {}".format(fallback))
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
    """The model picker is built with createElement/textContent and persists per conversation.

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

    populate_body = _extract_function_body(script, "populateModelPicker")
    if populate_body is None:
        print("ERROR: could not find a populateModelPicker() function to inspect")
        failed = True
    else:
        if "createElement" not in populate_body:
            print("ERROR: populateModelPicker() does not build options with createElement")
            failed = True
        # Reuses the same sink audit as test_inner_html_sinks_only_ever_receive_escaped_content,
        # rather than a fresh regex, because a naive `\.innerHTML\s*=\s*(?!'')` check backtracks
        # around the whitespace before the quotes and silently stops catching anything - the
        # allow-listed helper already gets this right and is exercised elsewhere in this suite.
        for rhs in _extract_inner_html_assignments(populate_body):
            if not _inner_html_rhs_is_safe(rhs):
                print("ERROR: populateModelPicker() assigns catalogue content straight into innerHTML: {!r}".format(rhs.strip()))
                failed = True
        if ".textContent" not in populate_body:
            print("ERROR: populateModelPicker() does not set option labels via textContent")
            failed = True
        # Anchored to the actual option construction (an empty .value immediately followed by a
        # 'Use default' .textContent), not a bare "''" substring check - select.innerHTML = '';
        # a few lines above already contains that same substring, so a loose check would still
        # pass even with the option itself deleted.
        if re.search(r"\.value\s*=\s*(?:''|\"\")\s*;[\s\S]{0,200}?\.textContent\s*=\s*['\"]Use default", populate_body) is None:
            print("ERROR: populateModelPicker() does not build an empty-value 'Use default' option: {!r}".format(populate_body))
            failed = True

    # The picker's change handler must send both the conversation id and the chosen model id to
    # POST /chat/model - persistence is per-conversation, not global. Anchored to the handler's own
    # body (found by brace-counting, not a proximity regex) so it fails if the post is moved
    # somewhere that no longer carries the conversation id, or dropped altogether.
    change_body = _extract_function_body(script, "changeModel")
    if change_body is None:
        print("ERROR: could not find a changeModel() handler for the picker's change event")
        failed = True
    else:
        if "state.conversation" not in change_body or "/chat/model" not in change_body:
            print("ERROR: changeModel() does not post to /chat/model with the conversation id: {!r}".format(change_body))
            failed = True
        if "conversation:" not in change_body or ("id:" not in change_body):
            print("ERROR: changeModel() does not post both conversation and id: {!r}".format(change_body))
            failed = True
    if "addEventListener('change', changeModel)" not in script and 'addEventListener("change", changeModel)' not in script:
        print("ERROR: changeModel() is never wired to #chat-model's change event")
        failed = True

    # Reloading a conversation must restore its own stored model, not silently keep showing
    # whatever was selected for the previous one.
    load_body = _extract_function_body(script, "loadConversationData")
    if load_body is None or "payload.model" not in load_body:
        print("ERROR: loadConversationData() does not read the conversation's stored model back from the history payload")
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
    failed |= test_nav_link_visibility(my_predbat)
    failed |= test_client_script_contract(my_predbat)
    failed |= test_chat_page_assembles_real_content(my_predbat)
    failed |= test_inner_html_sinks_only_ever_receive_escaped_content(my_predbat)
    failed |= test_stream_cursor_advances_from_every_event(my_predbat)
    failed |= test_dropped_connection_is_told_apart_from_a_real_error_frame(my_predbat)
    failed |= test_model_catalogue(my_predbat)
    failed |= test_models_route_uses_agent_loop_and_reports_catalogue_availability(my_predbat)
    failed |= test_model_picker_script_wires_routes_and_persists_selection(my_predbat)
    return failed
