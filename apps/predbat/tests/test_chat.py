# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the chat agent component.

Drives ChatAgent against a fake OpenRouter that replays canned SSE byte streams, so the agentic
loop, the tool dispatch and the confirmation gate are all exercised without a network or a model.
"""

import asyncio
import json
import threading
import time

import chat
from chat import EVENT_BUFFER_MAX, STALE_TURN_GRACE_SECONDS, ChatAgent, ChatBusyError, build_snapshot
from components import COMPONENT_LIST, Components


def _make_agent(my_predbat, **overrides):
    """Build a ChatAgent bound to my_predbat without going through ComponentBase.__init__."""
    agent = ChatAgent.__new__(ChatAgent)
    agent.base = my_predbat
    agent.log = my_predbat.log
    agent.prefix = my_predbat.prefix
    agent.args = my_predbat.args
    agent.count_errors = 0
    agent.api_started = False
    agent.api_stop = False
    settings = {"api_key": "test-key", "model": "test/model", "base_url": "https://openrouter.example/api/v1", "max_tokens": 0, "max_tool_calls": 4, "max_history": 40, "max_conversations": 20, "expiry_days": 30, "turn_timeout": 30, "fetch_allowlist": None}
    settings.update(overrides)
    agent.initialize(**settings)
    return agent


def test_component_gating(my_predbat):
    """The chat component is gated on the API key and the model being present."""
    failed = False
    print("**** Testing chat component gating ****")
    entry = COMPONENT_LIST.get("chat")
    if entry is None:
        print("ERROR: 'chat' is not registered in COMPONENT_LIST")
        return True

    for name in ("api_key", "model"):
        if not entry["args"].get(name, {}).get("required"):
            print("ERROR: '{}' is not required, so the component would start unconfigured".format(name))
            failed = True
    for name in ("base_url", "max_tool_calls", "max_history", "max_conversations", "expiry_days", "turn_timeout", "fetch_allowlist", "max_tokens"):
        if entry["args"].get(name, {}).get("required"):
            print("ERROR: '{}' is required, which would stop the component starting on a default install".format(name))
            failed = True
    if entry["args"]["api_key"]["config"] != "openrouter_api_key" or entry["args"]["model"]["config"] != "openrouter_model":
        print("ERROR: the gating args are not bound to the documented apps.yaml keys")
        failed = True
    if not entry.get("can_restart"):
        print("ERROR: the chat component should be restartable from the Components tab")
        failed = True

    return failed


class _FakeBase:
    """A minimal stand-in base object for driving Components.initialize() on the chat entry alone."""

    def __init__(self, args):
        """Store the apps.yaml-style args this fake base should report, and prepare log capture."""
        self.args = args
        self.args_from_apps_yaml = args
        self.local_tz = None
        self.prefix = "predbat"
        self.plan_interval_minutes = 30
        self.logged = []

    def log(self, message):
        """Capture a log line instead of printing it, so a test can inspect what was said."""
        self.logged.append(message)

    def get_arg(self, name, default=None, indirect=True, **kwargs):
        """Return the configured value for name, or default, ignoring the indirection kwargs."""
        return self.args.get(name, default)


def test_component_gating_end_to_end(my_predbat):
    """Components.initialize() genuinely withholds construction and logs precisely for chat.

    test_component_gating only inspects the static COMPONENT_LIST entry; it cannot fail if the
    gating mechanism itself is broken. This drives the real Components.initialize() against a
    stub base for all three configuration states it can be in.
    """
    failed = False
    print("**** Testing chat gating via Components.initialize() ****")

    base = _FakeBase({})
    comps = Components(base)
    comps.initialize(only="chat", phase=1)
    if comps.components.get("chat") is not None:
        print("ERROR: chat component was constructed with neither key configured")
        failed = True
    if base.logged:
        print("ERROR: an unconfigured install should stay quiet, got: {}".format(base.logged))
        failed = True

    base = _FakeBase({"openrouter_api_key": "sk-test"})
    comps = Components(base)
    comps.initialize(only="chat", phase=1)
    if comps.components.get("chat") is not None:
        print("ERROR: chat component was constructed with only the API key configured")
        failed = True
    if not any("openrouter_model" in line for line in base.logged):
        print("ERROR: the partial-configuration warning did not name the missing openrouter_model key, got: {}".format(base.logged))
        failed = True

    base = _FakeBase({"openrouter_api_key": "sk-test", "openrouter_model": "test/model"})
    comps = Components(base)
    comps.initialize(only="chat", phase=1)
    if comps.components.get("chat") is None:
        print("ERROR: chat component was not constructed with both keys configured")
        failed = True

    return failed


def test_build_snapshot(my_predbat):
    """The snapshot names the live figures and survives missing state."""
    failed = False
    print("**** Testing the live snapshot ****")
    snapshot = build_snapshot(my_predbat)
    if not isinstance(snapshot, str) or not snapshot.strip():
        print("ERROR: build_snapshot returned nothing useful")
        return True

    for label in ["SOC", "Predbat"]:
        if label.lower() not in snapshot.lower():
            print("ERROR: snapshot is missing {!r}:\n{}".format(label, snapshot))
            failed = True
    if len(snapshot) > 6000:
        print("ERROR: snapshot is {} characters, which would dominate every turn".format(len(snapshot)))
        failed = True

    class Sparse:
        """A base with almost nothing set, standing in for a half-started Predbat."""

        prefix = "predbat"
        args = {}

        def get_arg(self, name, default=None, **kwargs):
            """Return the default for every argument."""
            return default

        def get_ha_config(self, name, default):
            """Return the default for every config item."""
            return default, False

    try:
        sparse = build_snapshot(Sparse())
    except Exception as error:
        print("ERROR: build_snapshot raised on sparse state: {}".format(error))
        return True
    if not sparse.strip():
        print("ERROR: build_snapshot returned nothing for sparse state")
        failed = True

    return failed


def test_event_buffer(my_predbat):
    """Events are sequenced, filtered by conversation, and signal a reload when outrun."""
    failed = False
    print("**** Testing the chat event buffer ****")
    agent = _make_agent(my_predbat)

    agent.emit("aaaa", "delta", {"text": "one"})
    agent.emit("bbbb", "delta", {"text": "two"})
    agent.emit(None, "busy", {"conversation_id": "aaaa", "title": "t", "turn_id": 1})

    events, cursor, reload_needed = agent.events_since(0, "aaaa")
    kinds = [event["type"] for event in events]
    if kinds != ["delta", "busy"]:
        print("ERROR: conversation filtering returned {}, expected the conversation event plus the global one".format(kinds))
        failed = True
    if reload_needed:
        print("ERROR: a fresh cursor should not ask for a reload")
        failed = True
    if cursor != agent.event_seq:
        print("ERROR: returned cursor {} does not match the buffer head {}".format(cursor, agent.event_seq))
        failed = True

    later, _, _ = agent.events_since(cursor, "aaaa")
    if later:
        print("ERROR: replaying from the head returned {} stale events".format(len(later)))
        failed = True

    for index in range(EVENT_BUFFER_MAX + 50):
        agent.emit("aaaa", "delta", {"text": str(index)})
    if len(agent.events) > EVENT_BUFFER_MAX:
        print("ERROR: the event buffer grew to {}, cap is {}".format(len(agent.events), EVENT_BUFFER_MAX))
        failed = True
    _, _, reload_needed = agent.events_since(1, "aaaa")
    if not reload_needed:
        print("ERROR: a cursor older than the buffer did not ask for a reload")
        failed = True

    return failed


def test_release_stale_turn(my_predbat):
    """_release_stale_turn only frees a slot once its own deadline plus grace period has passed."""
    failed = False
    print("**** Testing stale turn release ****")
    agent = _make_agent(my_predbat, turn_timeout=30)

    agent.active = None
    agent._release_stale_turn()
    if agent.active is not None:
        print("ERROR: releasing with no active turn should leave it at None")
        failed = True

    agent.active = {"turn_id": 1, "started": time.monotonic()}
    agent._release_stale_turn()
    if agent.active is None:
        print("ERROR: a turn that just started was released")
        failed = True

    agent.active = {"turn_id": 2}
    agent._release_stale_turn()
    if agent.active is None:
        print("ERROR: a turn with no 'started' timestamp was released - this must be the safe (untouched) direction")
        failed = True

    # Strictly between turn_timeout (30) and turn_timeout + STALE_TURN_GRACE_SECONDS (90): past the
    # bare timeout but still within its grace period. A dropped "+ STALE_TURN_GRACE_SECONDS" term
    # would release this turn; the correct comparison must not. This is a different boundary from
    # the "just started" case above, not a duplicate of it - it is the one that actually exercises
    # the grace period rather than merely the timeout.
    agent.active = {"turn_id": 4, "started": time.monotonic() - (agent.turn_timeout + STALE_TURN_GRACE_SECONDS / 2)}
    events_before = len(agent.events)
    agent._release_stale_turn()
    if agent.active is None:
        print("ERROR: a turn within its grace period (past timeout, not past timeout+grace) was released")
        failed = True
    new_events = agent.events[events_before:]
    if any(event["type"] == "idle" and event["conversation_id"] is None for event in new_events):
        print("ERROR: a turn within its grace period emitted an idle event, got {}".format(new_events))
        failed = True

    events_before = len(agent.events)
    agent.active = {"turn_id": 3, "started": time.monotonic() - (agent.turn_timeout + STALE_TURN_GRACE_SECONDS + 5)}
    agent._release_stale_turn()
    if agent.active is not None:
        print("ERROR: a turn past its timeout plus grace period was not released")
        failed = True
    new_events = agent.events[events_before:]
    if not any(event["type"] == "idle" and event["conversation_id"] is None for event in new_events):
        print("ERROR: releasing a stale turn did not emit a global idle event, got {}".format(new_events))
        failed = True

    return failed


class FakeOpenRouter:
    """Replays canned chat-completion chunk sequences in place of the real endpoint."""

    def __init__(self, *responses):
        """Hold one chunk list per expected round trip."""
        self.responses = list(responses)
        self.payloads = []

    async def stream(self, payload):
        """Record the request payload and yield the next canned chunk list."""
        self.payloads.append(payload)
        chunks = self.responses.pop(0) if self.responses else [{"choices": [{"delta": {"content": "no more canned responses"}}]}]
        for chunk in chunks:
            yield chunk


def _text_response(text, usage=None):
    """Build a chunk list for a plain streamed answer."""
    chunks = [{"choices": [{"delta": {"content": piece}}]} for piece in text.split(" ")]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002}})
    return chunks


def _tool_call_response(name, arguments, call_id="call_1"):
    """Build a chunk list for a streamed tool call, fragmented the way providers send it."""
    encoded = json.dumps(arguments)
    half = len(encoded) // 2
    return [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": encoded[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": encoded[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 8, "cost": 0.0004}},
    ]


def _dangling_tool_calls(messages):
    """Return a description of every assistant tool_calls left without a matching tool reply.

    General over the whole conversation rather than tied to one turn's shape, so it guards both
    the normal round-trip path and any place - like the tool-call cap - that might append an
    assistant message carrying tool_calls without the tool replies the real API requires
    immediately afterwards. An unanswered tool_call_id here is exactly what gets a 400 back from
    OpenRouter on the next turn once the pair is sent again.
    """
    problems = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            index += 1
            continue
        expected = {call.get("id") for call in calls}
        following = index + 1
        seen = set()
        while following < len(messages) and messages[following].get("role") == "tool":
            seen.add(messages[following].get("tool_call_id"))
            following += 1
        missing = expected - seen
        if missing:
            problems.append("assistant message at index {} has tool_calls {} with no matching tool reply among {}".format(index, sorted(missing), sorted(seen)))
        index = following
    return problems


def _agent_with_fake(my_predbat, *responses, **overrides):
    """Build an agent whose only network call is replaced by a canned chunk replayer."""
    agent = _make_agent(my_predbat, **overrides)
    fake = FakeOpenRouter(*responses)
    agent._stream_chunks = fake.stream
    agent.fake = fake
    return agent


def test_plain_answer(my_predbat):
    """A turn with no tool call streams deltas, stores the answer, and reports usage."""
    failed = False
    print("**** Testing a plain chat turn ****")
    agent = _agent_with_fake(my_predbat, _text_response("your battery is charging because rates are low"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why is it charging?"))

    events, _, _ = agent.events_since(0, cid)
    kinds = [event["type"] for event in events]
    for required in ("user", "delta", "assistant", "usage", "done"):
        if required not in kinds:
            print("ERROR: turn did not emit {!r}, emitted {}".format(required, kinds))
            failed = True
    if "busy" not in [event["type"] for event in events] or "idle" not in [event["type"] for event in events]:
        print("ERROR: busy/idle were not both emitted: {}".format(kinds))
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    if not messages or messages[0]["role"] != "user" or messages[-1]["role"] != "assistant":
        print("ERROR: conversation did not record the exchange: {}".format(messages))
        failed = True
    if "charging" not in str(messages[-1].get("content")):
        print("ERROR: the assistant message lost its content: {}".format(messages[-1]))
        failed = True

    meta = agent.store.get_meta(cid)
    if not meta["usage_total"]["completion_tokens"]:
        print("ERROR: usage was not accumulated onto the conversation")
        failed = True
    if agent.active is not None:
        print("ERROR: the active turn was not released")
        failed = True

    return failed


def test_tool_call_round_trip(my_predbat):
    """A streamed tool call is reassembled, executed, and answered in a second round trip."""
    failed = False
    print("**** Testing a tool call turn ****")
    agent = _agent_with_fake(my_predbat, _tool_call_response("get_status", {}), _text_response("you are in Automatic mode"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "what mode am I in?"))

    kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
    for required in ("tool_start", "tool_end"):
        if required not in kinds:
            print("ERROR: the tool call was not reported: {}".format(kinds))
            failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    roles = [message["role"] for message in messages]
    if roles != ["user", "assistant", "tool", "assistant"]:
        print("ERROR: message roles were {}, expected a tool round trip".format(roles))
        failed = True
    tool_message = [message for message in messages if message["role"] == "tool"]
    if not tool_message or not tool_message[0].get("tool_call_id"):
        print("ERROR: the tool result is missing its tool_call_id, which the API rejects")
        failed = True

    if len(agent.fake.payloads) != 2:
        print("ERROR: expected two round trips, made {}".format(len(agent.fake.payloads)))
        failed = True
    else:
        tool_names = {tool["function"]["name"] for tool in agent.fake.payloads[0]["tools"]}
        for expected in ("get_plan", "search_docs", "read_source", "set_chat_title"):
            if expected not in tool_names:
                print("ERROR: {} was not offered to the model".format(expected))
                failed = True

    return failed


def test_tool_call_cap(my_predbat):
    """The loop stops at max_tool_calls and says so rather than spinning."""
    failed = False
    print("**** Testing the tool call cap ****")
    responses = [_tool_call_response("get_status", {}, call_id="call_{}".format(index)) for index in range(10)]
    agent = _agent_with_fake(my_predbat, *responses, max_tool_calls=2)
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "loop please"))

    if len(agent.fake.payloads) > 3:
        print("ERROR: the loop made {} round trips with a cap of 2".format(len(agent.fake.payloads)))
        failed = True
    tool_starts = [event for event in agent.events_since(0, cid)[0] if event["type"] == "tool_start"]
    if len(tool_starts) > 2:
        print("ERROR: {} tools were dispatched with a cap of 2 - the cap must stop tool execution too, not just round trips".format(len(tool_starts)))
        failed = True
    text = " ".join(str(event["data"].get("text", "")) for event in agent.events_since(0, cid)[0] if event["type"] == "assistant")
    if "limit" not in text.lower():
        print("ERROR: hitting the cap was not reported to the user: {!r}".format(text))
        failed = True
    if agent.active is not None:
        print("ERROR: the active turn was not released after hitting the cap")
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    problems = _dangling_tool_calls(messages)
    if problems:
        print("ERROR: hitting the cap left the stored history API-invalid: {}".format(problems))
        failed = True

    return failed


def test_tool_failures_are_results(my_predbat):
    """An unknown tool and malformed arguments come back to the model as tool results."""
    failed = False
    print("**** Testing tool failure handling ****")
    agent = _agent_with_fake(my_predbat, _tool_call_response("no_such_tool", {}), _text_response("sorry about that"))
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "call something silly"))
    messages = asyncio.run(agent.store.get_messages(cid))
    tool_results = [message for message in messages if message["role"] == "tool"]
    if not tool_results or "Unknown tool" not in str(tool_results[0].get("content")):
        print("ERROR: an unknown tool did not come back as a tool result: {}".format(tool_results))
        failed = True

    broken = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_b", "type": "function", "function": {"name": "get_status", "arguments": "{not json"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    agent2 = _agent_with_fake(my_predbat, broken, _text_response("retrying"))
    cid2 = asyncio.run(agent2.store.create())
    asyncio.run(agent2.run_turn(cid2, "send bad json"))
    results2 = [message for message in asyncio.run(agent2.store.get_messages(cid2)) if message["role"] == "tool"]
    if not results2 or "argument" not in str(results2[0].get("content")).lower():
        print("ERROR: malformed tool arguments were not reported back: {}".format(results2))
        failed = True
    if agent2.active is not None:
        print("ERROR: a malformed tool call wedged the active turn")
        failed = True

    return failed


def test_titles(my_predbat):
    """The model titles the conversation, and the first message titles it when the model does not."""
    failed = False
    print("**** Testing conversation titles ****")
    agent = _agent_with_fake(my_predbat, _tool_call_response("set_chat_title", {"title": "Overnight charging"}), _text_response("here you go"))
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "why charge overnight?"))
    if agent.store.get_meta(cid)["title"] != "Overnight charging":
        print("ERROR: set_chat_title did not take: {}".format(agent.store.get_meta(cid)["title"]))
        failed = True
    if "title" not in [event["type"] for event in agent.events_since(0, cid)[0]]:
        print("ERROR: the title change was not broadcast")
        failed = True

    quiet = _agent_with_fake(my_predbat, _text_response("no title for you"))
    cid2 = asyncio.run(quiet.store.create())
    asyncio.run(quiet.run_turn(cid2, "  what   is my export rate? "))
    title = quiet.store.get_meta(cid2)["title"]
    if title != "what is my export rate?":
        print("ERROR: the fallback title is {!r}, expected the collapsed first message".format(title))
        failed = True

    prompts = quiet.fake.payloads[0]["messages"][0]["content"]
    if "set_chat_title" not in prompts:
        print("ERROR: the title instruction was missing while the conversation was untitled")
        failed = True

    titled = _agent_with_fake(my_predbat, _text_response("second turn"))
    titled.store = quiet.store
    asyncio.run(titled.run_turn(cid2, "and my import rate?"))
    if "set_chat_title" in titled.fake.payloads[0]["messages"][0]["content"]:
        print("ERROR: the title instruction was still present after the conversation was titled")
        failed = True

    return failed


def test_execute_turn_preserves_a_slot_claimed_by_a_later_turn(my_predbat):
    """A turn's cleanup only releases the slot it owns, never one a later turn has since claimed.

    Simulates the race _release_stale_turn's docstring warns about: a stranded turn's slot gets
    freed and a new turn claims it while the old one is still running its own finally block. An
    unconditional clear there would silently unlock the composer while that new turn is still in
    flight. store.flush is the injection point because it is the last await before the
    lock-guarded clear, so patching it lets the race be reproduced deterministically instead of
    relying on real thread timing.
    """
    failed = False
    print("**** Testing that a finishing turn does not clobber a slot it no longer owns ****")
    agent = _agent_with_fake(my_predbat, _text_response("done"))
    cid = asyncio.run(agent.store.create())

    other_slot = {"conversation_id": "somewhere-else", "turn_id": 999, "title": "a later turn", "started": time.monotonic()}
    original_flush = agent.store.flush

    async def flush_and_steal(*args, **kwargs):
        """Flush as normal, then simulate a later turn claiming the slot before cleanup finishes."""
        result = await original_flush(*args, **kwargs)
        agent.active = other_slot
        return result

    agent.store.flush = flush_and_steal

    asyncio.run(agent.run_turn(cid, "hello"))

    if agent.active != other_slot:
        print("ERROR: the finishing turn clobbered a slot it did not own: {}".format(agent.active))
        failed = True

    return failed


def test_busy_rejects_a_second_turn(my_predbat):
    """Only one turn runs at a time, and an unknown conversation is a KeyError not a crash."""
    failed = False
    print("**** Testing busy and unknown conversation handling ****")
    agent = _agent_with_fake(my_predbat, _text_response("hello"))
    cid = asyncio.run(agent.store.create())
    other = asyncio.run(agent.store.create())

    agent.active = {"conversation_id": cid, "turn_id": 1, "title": "busy"}
    try:
        asyncio.run(agent.run_turn(other, "me too"))
        print("ERROR: a second concurrent turn was accepted")
        failed = True
    except ChatBusyError:
        pass
    agent.active = None

    try:
        asyncio.run(agent.run_turn("ffffffffffffffff", "who?"))
        print("ERROR: an unknown conversation id was accepted")
        failed = True
    except KeyError:
        pass

    return failed


def test_search_source_runs_off_the_loop(my_predbat):
    """search_source is dispatched to a worker thread, so a slow scan cannot stall the loop.

    read_source stays inline because it is bounded work, but search_source compiles and runs a
    model-supplied regular expression, and Python's re engine backtracks with no timeout - a
    pathological pattern must not be able to freeze the component's only event loop. This is
    proved, not assumed: a fast heartbeat coroutine races a slow synchronous replacement for
    search_source on the same loop. If the dispatch ever ran the search inline instead of via
    run_in_executor, the heartbeat would stall for the whole 0.3s the search takes.
    """
    failed = False
    print("**** Testing that search_source cannot block the component loop ****")

    def slow_search(*args, **kwargs):
        """Stand in for a pathological regex scan that blocks synchronously."""
        time.sleep(0.3)
        return {"success": True, "error": None, "data": [], "total_matches": 0}

    agent = _agent_with_fake(my_predbat, _tool_call_response("search_source", {"pattern": "def "}), _text_response("done"))
    original = chat.search_source
    chat.search_source = slow_search
    try:

        async def main():
            """Run one turn concurrently with a fast heartbeat on the same loop."""
            cid = await agent.store.create()
            heartbeat = {"count": 0}

            async def beat():
                """Tick a counter every 10ms - a blocked loop cannot advance this."""
                while True:
                    heartbeat["count"] += 1
                    await asyncio.sleep(0.01)

            beater = asyncio.ensure_future(beat())
            await agent.run_turn(cid, "search the source please")
            beater.cancel()
            return heartbeat["count"]

        ticks = asyncio.run(main())
    finally:
        chat.search_source = original

    if ticks < 10:
        print("ERROR: the loop's heartbeat only advanced {} times during a 0.3s synchronous search - search_source is blocking the component loop instead of a worker thread".format(ticks))
        failed = True

    return failed


def test_submit_turn_hands_off_to_the_component_loop(my_predbat):
    """submit_turn returns at once and the turn runs to completion on the component's own loop.

    This is the regression guard for the whole architecture: the web layer is a UI, so the call
    that starts a turn must not wait for it. A slow synchronous tool is used deliberately - if
    the work ever moves back onto the caller's loop, this test's timing assertion fails.
    """
    failed = False
    print("**** Testing the cross-thread turn handoff ****")
    import chat as chat_module

    def slow_search(*args, **kwargs):
        """Stand in for a slow synchronous full-tree scan."""
        time.sleep(0.4)
        return {"success": True, "error": None, "data": [], "total_matches": 0}

    agent = _agent_with_fake(my_predbat, _tool_call_response("search_source", {"pattern": "def "}), _text_response("found it"))

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    agent.loop = loop
    original = chat_module.search_source
    chat_module.search_source = slow_search
    try:
        cid = asyncio.run_coroutine_threadsafe(agent.store.create(), loop).result(10)

        started = time.monotonic()
        turn_id = agent.submit_turn(cid, "search the source")
        elapsed = time.monotonic() - started
        if elapsed > 0.2:
            print("ERROR: submit_turn took {:.2f}s - it is waiting for the turn rather than handing it off".format(elapsed))
            failed = True
        if not turn_id:
            print("ERROR: submit_turn did not return a turn id")
            failed = True

        deadline = time.monotonic() + 15
        while agent.active is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        if agent.active is not None:
            print("ERROR: the handed-off turn never completed or never released the turn slot")
            failed = True

        kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
        for required in ("tool_start", "tool_end", "done"):
            if required not in kinds:
                print("ERROR: the turn did not run through on the component loop: {}".format(kinds))
                failed = True
                break
    finally:
        chat_module.search_source = original
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    return failed


def test_submit_turn_needs_a_running_component(my_predbat):
    """Handing work over before the component loop exists is a clean error, not a hang."""
    failed = False
    print("**** Testing submit_turn before the component has started ****")
    agent = _agent_with_fake(my_predbat, _text_response("hello"))
    agent.loop = None
    cid = asyncio.run(agent.store.create())
    try:
        agent.submit_turn(cid, "too early")
        print("ERROR: submit_turn was accepted with no component loop")
        failed = True
    except chat.AgentNotReadyError:
        pass
    if agent.active is not None:
        print("ERROR: a refused submit_turn left the turn slot claimed")
        failed = True
    return failed


def run_chat_tests(my_predbat):
    """Run every chat agent test, returning True if any of them failed."""
    failed = False
    failed |= test_component_gating(my_predbat)
    failed |= test_component_gating_end_to_end(my_predbat)
    failed |= test_build_snapshot(my_predbat)
    failed |= test_event_buffer(my_predbat)
    failed |= test_release_stale_turn(my_predbat)
    failed |= test_plain_answer(my_predbat)
    failed |= test_tool_call_round_trip(my_predbat)
    failed |= test_tool_call_cap(my_predbat)
    failed |= test_tool_failures_are_results(my_predbat)
    failed |= test_titles(my_predbat)
    failed |= test_execute_turn_preserves_a_slot_claimed_by_a_later_turn(my_predbat)
    failed |= test_busy_rejects_a_second_turn(my_predbat)
    failed |= test_search_source_runs_off_the_loop(my_predbat)
    failed |= test_submit_turn_hands_off_to_the_component_loop(my_predbat)
    failed |= test_submit_turn_needs_a_running_component(my_predbat)
    return failed
