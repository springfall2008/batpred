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

import aiohttp

import chat
from chat import (
    COMPLETION_MAX_ATTEMPTS,
    COMPLETION_RATE_LIMIT_RETRY_DELAY_SECONDS,
    COMPLETION_RETRY_DELAYS_SECONDS,
    EVENT_BUFFER_MAX,
    STALE_TURN_GRACE_SECONDS,
    ChatAgent,
    ChatBusyError,
    ChatRequestError,
    build_snapshot,
    classify_completion_failure,
    is_empty_completion,
)
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


def test_pending_conversations_is_lock_guarded(my_predbat):
    """pending_conversations() reads pending_confirm through agent.lock, not by iterating it raw.

    The component thread inserts, pops and wholesale rebuilds pending_confirm under agent.lock
    (_run_one_tool's claim/release around the confirmation, confirm(), and _execute_turn's
    cleanup). A caller on a different thread that iterates pending_confirm.values() directly -
    which is exactly what html_chat_conversations used to do - can race one of those mutations and
    raise "dictionary changed size during iteration", 500ing the /chat/conversations poll. This
    checks the accessor genuinely takes the shared lock rather than reading the dict raw: since
    the mutators already lock consistently, going through the same lock is what closes the race (a
    real thread-timing reproduction would be flaky in CI; see
    test_pending_conversations_survives_concurrent_mutation for a live stress version of the same
    guarantee).
    """
    failed = False
    print("**** Testing pending_conversations() takes agent.lock ****")
    agent = _make_agent(my_predbat)
    agent.pending_confirm = {"call_1": {"conversation_id": "abc", "turn_id": 1, "approved": None}, "call_2": {"conversation_id": "def", "turn_id": 2, "approved": None}}

    acquired = []
    real_lock = agent.lock

    class RecordingLock:
        """A lock stand-in that records every acquire, then delegates to the real lock."""

        def __enter__(self):
            """Record the acquire and hand off to the real lock."""
            acquired.append(True)
            return real_lock.__enter__()

        def __exit__(self, *args):
            """Hand off release to the real lock."""
            return real_lock.__exit__(*args)

    agent.lock = RecordingLock()
    try:
        result = agent.pending_conversations()
    finally:
        agent.lock = real_lock

    if not acquired:
        print("ERROR: pending_conversations() did not take agent.lock")
        failed = True
    if result != {"abc", "def"}:
        print("ERROR: pending_conversations() returned {}, expected {{'abc', 'def'}}".format(result))
        failed = True

    return failed


def test_pending_conversations_survives_concurrent_mutation(my_predbat):
    """A background thread churning pending_confirm cannot crash a concurrent read.

    Live reproduction of the race Important finding 3 in the final review reported: the web
    thread's /chat/conversations poll iterating pending_confirm.values() directly against the
    component thread's insert/pop of a confirmation raises RuntimeError mid-iteration. Both
    threads hammer the same agent for a bounded time; without the lock this reliably raises within
    a few hundred iterations, since a dict's iterator checks the size on every step.
    """
    failed = False
    print("**** Testing pending_conversations() survives concurrent mutation ****")
    agent = _make_agent(my_predbat)
    stop = threading.Event()
    errors = []

    def churn():
        """Insert and remove confirmations in a tight loop, the way turns really do."""
        counter = 0
        try:
            while not stop.is_set():
                counter += 1
                call_id = "call_{}".format(counter)
                with agent.lock:
                    agent.pending_confirm[call_id] = {"conversation_id": "conv-{}".format(counter % 5), "turn_id": counter, "approved": None}
                with agent.lock:
                    agent.pending_confirm.pop(call_id, None)
        except Exception as error:
            errors.append(error)

    writer = threading.Thread(target=churn, daemon=True)
    writer.start()
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                agent.pending_conversations()
            except Exception as error:
                errors.append(error)
                break
    finally:
        stop.set()
        writer.join(timeout=5)

    if errors:
        print("ERROR: concurrent read/mutation raised: {}".format(errors))
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


def test_release_stale_turn_keeps_a_slot_parked_in_confirmation(my_predbat):
    """A turn waiting on await_confirmation keeps its slot past turn_timeout + grace.

    CONFIRM_TIMEOUT_SECONDS (300s) is deliberately longer than turn_timeout + STALE_TURN_GRACE_
    SECONDS (240s with the defaults _make_agent uses), because a user reading an Approve/Reject
    prompt should get its own generous window rather than the turn's own budget for talking to
    the model. Without this guard, a user taking four minutes to answer has their live slot
    freed while await_confirmation is still polling for the answer.

    The final case drives the real await_confirmation() end to end and checks the slot survives
    even after the answer lands and pending_confirm is popped - not just while it is pending. See
    the comment at that case for why the earlier cases alone do not cover that.
    """
    failed = False
    print("**** Testing that a turn parked in confirmation keeps its slot past the stale threshold ****")
    agent = _make_agent(my_predbat, turn_timeout=30)
    stale_started = time.monotonic() - (agent.turn_timeout + STALE_TURN_GRACE_SECONDS + 5)

    # With a pending confirmation for this turn, release must be skipped however long it has run.
    agent.active = {"turn_id": 7, "started": stale_started}
    agent.pending_confirm = {"call_1": {"conversation_id": "c1", "turn_id": 7, "approved": None}}
    agent._release_stale_turn()
    if agent.active is None:
        print("ERROR: a turn genuinely waiting on a write confirmation was released as stale")
        failed = True

    # The same elapsed time, with no pending confirmation for this turn, must still release -
    # otherwise the guard would have silently swallowed the whole staleness check.
    agent.active = {"turn_id": 7, "started": stale_started}
    agent.pending_confirm = {}
    agent._release_stale_turn()
    if agent.active is not None:
        print("ERROR: a turn with no pending confirmation was not released once genuinely stale - the confirmation guard is too broad")
        failed = True

    # A pending confirmation that belongs to a DIFFERENT turn must not protect this one.
    agent.active = {"turn_id": 7, "started": stale_started}
    agent.pending_confirm = {"call_1": {"conversation_id": "c1", "turn_id": 8, "approved": None}}
    agent._release_stale_turn()
    if agent.active is not None:
        print("ERROR: a confirmation belonging to a different turn protected this one from release")
        failed = True

    # A confirmation that gets ANSWERED - not merely pending - must also extend the stale clock,
    # not only the turn's own deadline. The guard above only protects a turn WHILE its entry is
    # still in pending_confirm; _run_one_tool pops that entry immediately after
    # await_confirmation() returns. active["started"] is set once in claim_turn and never
    # otherwise advances, so a wait long enough to push self.deadline (which IS extended, at both
    # "self.deadline += elapsed" sites in await_confirmation) past started + turn_timeout +
    # STALE_TURN_GRACE_SECONDS would - without also advancing started - have its live slot
    # released on the very next housekeeping tick right after the user finally answers: the same
    # hazard as the original bug, displaced from during the wait to just after the answer.
    #
    # STALE_TURN_GRACE_SECONDS is patched down to keep this fast and deterministic without a real
    # multi-minute wait - the same trick test_write_confirmation_timeout uses on
    # CONFIRM_TIMEOUT_SECONDS below. This never touches time.monotonic() itself, so asyncio's own
    # scheduling (which reads it via loop.time()) is unaffected.
    confirm_agent = _make_agent(my_predbat, turn_timeout=0.05)
    original_grace = chat.STALE_TURN_GRACE_SECONDS
    chat.STALE_TURN_GRACE_SECONDS = 0.05
    try:
        answer_started = time.monotonic()
        confirm_agent.active = {"turn_id": 9, "started": answer_started}
        confirm_agent.deadline = answer_started + confirm_agent.turn_timeout
        confirm_agent.pending_confirm = {"call_9": {"conversation_id": "c1", "turn_id": 9, "approved": None}}
        old_threshold = confirm_agent.turn_timeout + chat.STALE_TURN_GRACE_SECONDS

        def answer_after_the_old_threshold():
            """Answer the confirmation only once the pre-fix release threshold has passed."""
            time.sleep(old_threshold + 0.3)
            confirm_agent.confirm("call_9", "c1", True)

        answer_thread = threading.Thread(target=answer_after_the_old_threshold, daemon=True)
        answer_thread.start()
        approved = asyncio.run(confirm_agent.await_confirmation("call_9"))
        answer_thread.join(timeout=5)

        if not approved:
            print("ERROR: the confirmation was not reported approved")
            failed = True
        elif confirm_agent.active["started"] <= answer_started + old_threshold - 0.01:
            print("ERROR: active['started'] was not advanced by the real wait - started at {}, still {}".format(answer_started, confirm_agent.active["started"]))
            failed = True
        else:
            # _run_one_tool pops the entry immediately after await_confirmation() returns -
            # simulate that here, so the release check below sees exactly what a real turn would.
            confirm_agent.pending_confirm.pop("call_9", None)
            confirm_agent._release_stale_turn()
            if confirm_agent.active is None:
                print("ERROR: the slot was released just after the user answered a long-pending confirmation - the stale clock was not extended along with the turn deadline")
                failed = True
    finally:
        chat.STALE_TURN_GRACE_SECONDS = original_grace

    return failed


class FakeOpenRouter:
    """Replays canned chat-completion chunk sequences in place of the real endpoint."""

    def __init__(self, *responses):
        """Hold one chunk list per expected round trip."""
        self.responses = list(responses)
        self.payloads = []

    async def stream(self, payload):
        """Record the request payload and yield the next canned chunk list.

        A canned response may be an exception instance instead of a chunk list, in which case it
        is raised before anything is yielded - mirroring how the real _stream_chunks fails a
        non-200 response before any chunk ever reaches the caller. This is what lets a retry test
        drive a specific failure (a given HTTP status, or a generic connection error) without
        going through the real network stack.
        """
        self.payloads.append(payload)
        chunks = self.responses.pop(0) if self.responses else [{"choices": [{"delta": {"content": "no more canned responses"}}]}]
        if isinstance(chunks, BaseException):
            raise chunks
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


def _two_tool_calls_response_without_ids(specs):
    """Build a chunk list for two tool calls streamed in one message, neither carrying an id.

    Real providers always send an id, but this is exactly the shape the id-normalisation code in
    _run_completion exists for: an id-less call must not end up unanswerable, and two id-less
    calls in the same message must not collide onto the same synthetic id.
    """
    chunks = []
    for index, (name, arguments) in enumerate(specs):
        chunks.append({"choices": [{"delta": {"tool_calls": [{"index": index, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]})
    chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 8, "cost": 0.0004}})
    return chunks


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
    """Build an agent whose only network call is replaced by a canned chunk replayer.

    agent._retry_sleep is also replaced by a fast recorder rather than the real asyncio.sleep, so
    that a test whose fixture happens to trigger a retry never actually pauses the suite for the
    real backoff - every requested delay is appended to agent.retry_sleeps instead, which is what
    a backoff test asserts against (per the project's own warning that a retry test which really
    sleeps makes the suite slow and flaky).
    """
    agent = _make_agent(my_predbat, **overrides)
    fake = FakeOpenRouter(*responses)
    agent._stream_chunks = fake.stream
    agent.fake = fake
    agent.retry_sleeps = []

    async def _fast_retry_sleep(seconds):
        """Record the requested delay instead of actually waiting for it."""
        agent.retry_sleeps.append(seconds)

    agent._retry_sleep = _fast_retry_sleep
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


def test_dispatch_strips_chat_omit_properties(my_predbat):
    """_dispatch enforces chat_omit_properties on the real arguments, not just the offered schema.

    openai_tool_list() removes 'masked' from the tool schema shown to the model - a presentation
    detail. Nothing stops the model, or content it read via fetch_url/search_docs, from naming the
    argument anyway: _dispatch() receives whatever arguments dict the model's own JSON decoded to,
    independent of what schema it was offered. This drives _dispatch() directly, the same call
    _run_one_tool() makes, and proves get_apps still redacts credentials even when the caller
    explicitly asks for 'masked: False' - reproducing the exact shape the review found reachable
    (chat schema hides 'masked', but a model that names it anyway got raw ha_key/octopus_api_key
    values back). Pinned at this layer because this is the layer that actually enforces it; the
    schema-only check lives in test_agent_tools.py's test_openai_tool_list_shape.
    """
    failed = False
    print("**** Testing _dispatch strips chat_omit_properties before executing a tool ****")
    agent = _make_agent(my_predbat)
    original_ha_key = my_predbat.args.get("ha_key")
    original_octopus_key = my_predbat.args.get("octopus_api_key")
    my_predbat.args["ha_key"] = "SUPER-SECRET-TOKEN"
    my_predbat.args["octopus_api_key"] = "sk-oct-123"
    try:
        result = asyncio.run(agent._dispatch(None, "get_apps", {"masked": False}))
    finally:
        if original_ha_key is None:
            my_predbat.args.pop("ha_key", None)
        else:
            my_predbat.args["ha_key"] = original_ha_key
        if original_octopus_key is None:
            my_predbat.args.pop("octopus_api_key", None)
        else:
            my_predbat.args["octopus_api_key"] = original_octopus_key

    if not result.get("success"):
        print("ERROR: get_apps failed: {}".format(result))
        return True
    data = result.get("data") or {}
    if data.get("ha_key") == "SUPER-SECRET-TOKEN" or data.get("octopus_api_key") == "sk-oct-123":
        print("ERROR: raw credentials reached the chat dispatch path despite 'masked: False': {}".format(data))
        failed = True
    if result.get("masked") is not True:
        print("ERROR: get_apps reported masked={} despite the model asking for masked: False through chat".format(result.get("masked")))
        failed = True

    return failed


def test_tool_call_ids_are_normalised_when_the_provider_omits_them(my_predbat):
    """Two id-less tool calls in one message get distinct synthetic ids, and both still run.

    Real OpenAI-compatible providers always send an id, but the invariant that every stored
    tool_call_id matches a stored call id must hold by construction, not by the provider's
    goodwill. Without normalisation an id-less call stores as {"id": None, ...}; its reply then
    falls back to a fallback keyed on the turn rather than the call, which never matches the
    stored None and - with two id-less calls in one message - collapses both replies onto the
    same id. Either way the call ends up effectively unanswered, which is the same
    API-rejection shape the tool-call cap fix closed, on the ordinary round-trip path instead of
    at the cap.
    """
    failed = False
    print("**** Testing that id-less tool calls get distinct synthetic ids ****")
    calls = _two_tool_calls_response_without_ids([("get_status", {}), ("get_plan", {})])
    agent = _agent_with_fake(my_predbat, calls, _text_response("both checked"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "check status and plan"))

    messages = asyncio.run(agent.store.get_messages(cid))
    with_calls = [message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")]
    if not with_calls:
        print("ERROR: no assistant message with tool_calls was stored: {}".format(messages))
        return True
    ids = [call.get("id") for call in with_calls[0]["tool_calls"]]
    if len(ids) != 2 or any(not call_id for call_id in ids) or len(set(ids)) != 2:
        print("ERROR: id-less tool calls did not get distinct, non-empty synthetic ids: {}".format(ids))
        failed = True

    problems = _dangling_tool_calls(messages)
    if problems:
        print("ERROR: id-less tool calls left the stored history API-invalid: {}".format(problems))
        failed = True

    tool_starts = [event for event in agent.events_since(0, cid)[0] if event["type"] == "tool_start"]
    if len(tool_starts) != 2:
        print("ERROR: expected both id-less calls to run separately, got {} tool_start event(s) - normalisation may have merged them".format(len(tool_starts)))
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
    """A turn's cleanup only releases the slot it owns - and only announces done/idle - when it
    still owns it, never for a later turn that has since claimed it.

    Simulates the race _release_stale_turn's docstring warns about: a stranded turn's slot gets
    freed and a new turn claims it while the old one is still running its own finally block. An
    unconditional clear there would silently unlock the composer while that new turn is still in
    flight - and so would an unconditional done/idle emit, even with the clear itself correctly
    guarded: idle is a GLOBAL event delivered to every browser regardless of which conversation it
    is looking at, so the finishing (superseded) turn broadcasting it would tell every browser the
    composer is free while turn 2 is still running. store.flush is the injection point because it
    is the last await before the lock-guarded clear, so patching it lets the race be reproduced
    deterministically instead of relying on real thread timing.
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

    events_before = len(agent.events)
    asyncio.run(agent.run_turn(cid, "hello"))

    if agent.active != other_slot:
        print("ERROR: the finishing turn clobbered a slot it did not own: {}".format(agent.active))
        failed = True

    new_events = agent.events[events_before:]
    if any(event["type"] in ("done", "idle") for event in new_events):
        print("ERROR: the superseded turn broadcast done/idle and would unlock the composer while turn 999 is still running: {}".format([event["type"] for event in new_events]))
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


def _write_call_response(entity_id="input_number.predbat_best_soc_keep", value="2.0"):
    """Build a chunk list for a streamed set_config call."""
    return _tool_call_response("set_config", {"entity_id": entity_id, "value": value}, call_id="call_write")


def _confirm_soon(agent, approved):
    """Answer the next pending confirmation from a background thread, as a browser would."""

    def answer():
        """Poll for the pending confirmation and resolve it."""
        for _ in range(200):
            with agent.lock:
                pending = dict(agent.pending_confirm)
            if pending:
                call_id = sorted(pending)[0]
                agent.confirm(call_id, pending[call_id]["conversation_id"], approved)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def test_write_confirmation_approved(my_predbat):
    """With the switch on, a write waits for approval and then executes."""
    failed = False
    print("**** Testing write confirmation - approved ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("done"))
    agent.confirm_writes_enabled = lambda: True
    cid = asyncio.run(agent.store.create())

    _confirm_soon(agent, True)
    asyncio.run(agent.run_turn(cid, "raise best soc keep"))

    kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
    for required in ("confirm", "confirm_result", "tool_start", "tool_end"):
        if required not in kinds:
            print("ERROR: approval path did not emit {!r}: {}".format(required, kinds))
            failed = True

    results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
    if not results or "declined" in str(results[0].get("content")).lower():
        print("ERROR: an approved write was not executed: {}".format(results))
        failed = True

    return failed


def test_write_confirmation_rejected(my_predbat):
    """A rejected write becomes an ordinary tool result so the model can respond to it."""
    failed = False
    print("**** Testing write confirmation - rejected ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("understood"))
    agent.confirm_writes_enabled = lambda: True
    cid = asyncio.run(agent.store.create())

    _confirm_soon(agent, False)
    asyncio.run(agent.run_turn(cid, "raise best soc keep"))

    results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
    if not results or "declined" not in str(results[0].get("content")).lower():
        print("ERROR: a rejected write did not come back as a declined tool result: {}".format(results))
        failed = True
    if "tool_start" in [event["type"] for event in agent.events_since(0, cid)[0]]:
        print("ERROR: a rejected write still ran the tool")
        failed = True
    if agent.pending_confirm:
        print("ERROR: the pending confirmation outlived its turn")
        failed = True

    return failed


def test_write_confirmation_timeout(my_predbat):
    """An unanswered confirmation times out into a decline rather than hanging."""
    failed = False
    print("**** Testing write confirmation - timeout ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("no answer"))
    agent.confirm_writes_enabled = lambda: True
    chat.CONFIRM_TIMEOUT_SECONDS_ORIGINAL = chat.CONFIRM_TIMEOUT_SECONDS
    chat.CONFIRM_TIMEOUT_SECONDS = 0.5
    try:
        cid = asyncio.run(agent.store.create())
        started = time.monotonic()
        asyncio.run(agent.run_turn(cid, "raise best soc keep"))
        elapsed = time.monotonic() - started
    finally:
        chat.CONFIRM_TIMEOUT_SECONDS = chat.CONFIRM_TIMEOUT_SECONDS_ORIGINAL

    if elapsed > 10:
        print("ERROR: the timeout path took {:.1f}s, so it is not honouring CONFIRM_TIMEOUT_SECONDS".format(elapsed))
        failed = True
    results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
    if not results or "declined" not in str(results[0].get("content")).lower():
        print("ERROR: a timed-out confirmation did not decline: {}".format(results))
        failed = True

    return failed


def test_write_without_confirmation(my_predbat):
    """With the switch off, a write executes directly but is still recorded in the transcript."""
    failed = False
    print("**** Testing writes with confirmation off ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("changed"))
    agent.confirm_writes_enabled = lambda: False
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "just do it"))

    kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
    if "confirm" in kinds:
        print("ERROR: a confirmation was requested with the switch off")
        failed = True
    for required in ("tool_start", "tool_end"):
        if required not in kinds:
            print("ERROR: the write was not recorded in the transcript: {}".format(kinds))
            failed = True

    return failed


def test_web_search_switch(my_predbat):
    """The plugin is added only when the switch is on, and a foreign base URL warns once."""
    failed = False
    print("**** Testing the web search switch ****")
    off = _agent_with_fake(my_predbat, _text_response("no plugin"))
    off.web_search_enabled = lambda: False
    cid = asyncio.run(off.store.create())
    asyncio.run(off.run_turn(cid, "hello"))
    if "plugins" in off.fake.payloads[0]:
        print("ERROR: the web plugin was sent with the switch off")
        failed = True

    on = _agent_with_fake(my_predbat, _text_response("with plugin"))
    on.web_search_enabled = lambda: True
    cid2 = asyncio.run(on.store.create())
    asyncio.run(on.run_turn(cid2, "hello"))
    if on.fake.payloads[0].get("plugins") != [{"id": "web"}]:
        print("ERROR: the web plugin was not sent with the switch on: {}".format(on.fake.payloads[0].get("plugins")))
        failed = True

    foreign = _make_agent(my_predbat, base_url="http://localhost:11434/v1")
    warnings = []
    foreign.log = lambda message, **kwargs: warnings.append(str(message))
    foreign.get_ha_config = lambda name, default: (True, False)
    foreign.web_search_enabled()
    foreign.web_search_enabled()
    matched = [line for line in warnings if "web search" in line.lower()]
    if len(matched) != 1:
        print("ERROR: expected exactly one warning for a non-OpenRouter base URL, got {}".format(matched))
        failed = True

    return failed


def _mid_stream_error_response():
    """Build a chunk list reproducing OpenRouter's documented mid-stream error shape exactly.

    content is "" (falsy), not omitted or truthy - the live bug depended on the original code's
    `if delta.get("content")` treating an empty string as nothing to append, so a fixture using a
    truthy content string would not reproduce the failure this guards against.
    """
    return [
        {
            "id": "cmpl-abc123",
            "object": "chat.completion.chunk",
            "model": "openai/gpt-4o",
            "error": {"code": "server_error", "message": "Provider disconnected unexpectedly"},
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
        }
    ]


def test_mid_stream_error_chunk_fails_the_turn_loudly_after_exhausting_every_retry(my_predbat):
    """A mid-stream OpenRouter error chunk that never clears surfaces the provider's own message,
    names how many attempts were made, and stores nothing.

    Reproduces the live failure this task started from: a turn that produced no response at all,
    stored as an assistant message with content: null and no tool_calls. The documented OpenRouter
    shape carries delta.content: "" (falsy) alongside a top-level "error" object - the original
    code's `if delta.get("content")` silently skipped the empty string, never looked at
    chunk["error"] at all, and finished the turn with nothing to show, which got stored and
    replayed to the model as junk history on every subsequent turn.

    error.code "server_error" is one of the markers classify_completion_failure() treats as
    retryable provider trouble, so the fixture is supplied COMPLETION_MAX_ATTEMPTS times - one per
    attempt the retry wrapper is expected to make - rather than once. Asserting the fake was
    actually called that many times (not just that the turn eventually failed) is what tells a
    version that retries from one that gives up on the first failure: a wrapper that never retries
    at all would still produce a failed turn with only one canned response, so the message and
    active-slot checks alone cannot tell the two apart - only counting attempts can.
    """
    failed = False
    print("**** Testing a mid-stream OpenRouter error chunk that never clears exhausts every retry and fails loudly ****")
    agent = _agent_with_fake(my_predbat, *[_mid_stream_error_response() for _ in range(COMPLETION_MAX_ATTEMPTS)])
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "what mode am I in?"))

    if len(agent.fake.payloads) != COMPLETION_MAX_ATTEMPTS:
        print("ERROR: expected {} completion attempts, the fake was called {} times".format(COMPLETION_MAX_ATTEMPTS, len(agent.fake.payloads)))
        failed = True

    events = agent.events_since(0, cid)[0]
    errors = [event for event in events if event["type"] == "error"]
    if not errors:
        print("ERROR: no error event was emitted for the mid-stream error chunk")
        return True
    message = errors[0]["data"].get("message", "")
    if "Provider disconnected unexpectedly" not in message:
        print("ERROR: the error event did not carry the provider's own message: {}".format(errors[0]["data"]))
        failed = True
    if "Gave up after {} attempts".format(COMPLETION_MAX_ATTEMPTS) not in message:
        print("ERROR: the error event did not say how many attempts were made: {!r}".format(message))
        failed = True

    retries = [event for event in events if event["type"] == "retry"]
    if len(retries) != COMPLETION_MAX_ATTEMPTS - 1:
        print("ERROR: expected {} 'retry' events (one per retry, not per attempt), got {}: {}".format(COMPLETION_MAX_ATTEMPTS - 1, len(retries), retries))
        failed = True

    # Assert on the stored conversation itself, not just the emitted events - the original bug's
    # damage was the persisted junk message, which would be invisible to an events-only check.
    messages = asyncio.run(agent.store.get_messages(cid))
    if any(message.get("role") == "assistant" for message in messages):
        print("ERROR: an assistant message was stored despite the mid-stream error: {}".format(messages))
        failed = True
    if agent.active is not None:
        print("ERROR: the turn slot was not released after the mid-stream error")
        failed = True

    return failed


def _length_truncated_response(text="partial answer that got cut off"):
    """Build a chunk list ending with finish_reason 'length' - an openrouter_max_tokens cutoff."""
    chunks = [{"choices": [{"delta": {"content": piece + " "}}]} for piece in text.split(" ")]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002}})
    return chunks


def test_finish_reason_length_keeps_partial_content_with_a_truncation_note(my_predbat):
    """A completion cut off by the token limit keeps its partial text plus a visible note.

    finish_reason 'length' means openrouter_max_tokens cut the reply short mid-thought. Discarding
    the partial content would be worse than a truncated answer, but showing it with no indication
    it was cut short would let a user mistake a half-answer for a whole one.
    """
    failed = False
    print("**** Testing a length-truncated completion keeps its content and says so ****")
    agent = _agent_with_fake(my_predbat, _length_truncated_response())
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "explain everything"))

    messages = asyncio.run(agent.store.get_messages(cid))
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant:
        print("ERROR: no assistant message was stored for a truncated completion")
        return True
    content = assistant[0].get("content") or ""
    if "partial answer that got cut off" not in content:
        print("ERROR: the truncated completion lost its partial content: {!r}".format(content))
        failed = True
    if "cut short" not in content.lower() and "truncated" not in content.lower():
        print("ERROR: the truncated completion carries no visible note that it was cut short: {!r}".format(content))
        failed = True

    errors = [event for event in agent.events_since(0, cid)[0] if event["type"] == "error"]
    if errors:
        print("ERROR: a length-truncated completion should not be treated as an error: {}".format(errors))
        failed = True

    return failed


def _reasoning_only_response():
    """Build a chunk list matching a real captured OpenRouter reasoning-model run: content empty
    on every chunk, reasoning_details fragments streamed across it, no tool_calls at all - the
    exact shape that produces a stored {"content": null, no tool_calls} message if nothing catches
    it, per the live captured example that showed a reasoning model can finish a completion having
    put everything into reasoning and nothing into a visible answer or a tool call.
    """
    return [
        {"choices": [{"delta": {"content": "", "role": "assistant", "reasoning": "The", "reasoning_details": [{"type": "reasoning.text", "text": "The", "format": "unknown", "index": 0}]}}]},
        {"choices": [{"delta": {"content": "", "role": "assistant", "reasoning": " user asks", "reasoning_details": [{"type": "reasoning.text", "text": " user asks", "format": "unknown", "index": 0}]}}]},
        {"choices": [{"delta": {"content": "", "role": "assistant", "reasoning": " a question.", "reasoning_details": [{"type": "reasoning.text", "text": " a question.", "format": "unknown", "index": 0}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 30, "completion_tokens": 12, "cost": 0.0006}},
    ]


def test_reasoning_only_completion_is_retried_then_reported_as_an_anomaly_if_it_never_clears(my_predbat):
    """A completion with no content and no tool calls is retried like any other provider trouble,
    and only reported as an anomaly - never stored - once every attempt still comes back empty.

    This is the live bug this task started from, reproduced with the shape a real reasoning-model
    run actually showed: reasoning fragments on nearly every chunk, content "" throughout, no
    tool_calls, and nothing else to fall back on. Storing the resulting content-less message
    anyway is how it silently replayed as junk history forever after; this checks the message is
    never stored and that the error names reasoning as the likely cause, not a generic failure.

    Supplied COMPLETION_MAX_ATTEMPTS times, and the fake's call count is asserted against that -
    see the docstring on the mid-stream-error equivalent of this test for why counting attempts,
    not just checking the eventual failure, is what actually proves a retry was attempted.
    """
    failed = False
    print("**** Testing a reasoning-only completion is retried, and only reported once every attempt is empty ****")
    agent = _agent_with_fake(my_predbat, *[_reasoning_only_response() for _ in range(COMPLETION_MAX_ATTEMPTS)])
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why is it charging?"))

    if len(agent.fake.payloads) != COMPLETION_MAX_ATTEMPTS:
        print("ERROR: expected {} completion attempts, the fake was called {} times".format(COMPLETION_MAX_ATTEMPTS, len(agent.fake.payloads)))
        failed = True

    events = agent.events_since(0, cid)[0]
    errors = [event for event in events if event["type"] == "error"]
    if not errors:
        print("ERROR: no error event was emitted for a reasoning-only completion")
        return True
    message = errors[0]["data"].get("message", "")
    if "reasoning" not in message.lower():
        print("ERROR: the error did not name reasoning as the likely cause: {}".format(errors[0]["data"]))
        failed = True
    if "Gave up after {} attempts".format(COMPLETION_MAX_ATTEMPTS) not in message:
        print("ERROR: the error event did not say how many attempts were made: {!r}".format(message))
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    if any(message.get("role") == "assistant" for message in messages):
        print("ERROR: a content-less, tool-call-less assistant message was stored: {}".format(messages))
        failed = True
    if any(event["type"] == "assistant" for event in events):
        print("ERROR: an 'assistant' event was emitted for a completion with nothing to show")
        failed = True
    if agent.active is not None:
        print("ERROR: the turn slot was not released after a reasoning-only completion")
        failed = True

    return failed


def _reasoning_tool_call_response(call_name="get_status", call_args=None):
    """Build a chunk list carrying one reasoning block streamed across three fragments - all
    sharing index 0, mirroring a real captured OpenRouter run word-for-word - followed by a tool
    call.
    """
    encoded = json.dumps(call_args if call_args is not None else {})
    return [
        {"choices": [{"delta": {"content": "", "reasoning": "The", "reasoning_details": [{"type": "reasoning.text", "text": "The", "format": "unknown", "index": 0}]}}]},
        {"choices": [{"delta": {"content": "", "reasoning": " user asks", "reasoning_details": [{"type": "reasoning.text", "text": " user asks", "format": "unknown", "index": 0}]}}]},
        {"choices": [{"delta": {"content": "", "reasoning": ': "What is', "reasoning_details": [{"type": "reasoning.text", "text": ': "What is', "format": "unknown", "index": 0}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_r1", "type": "function", "function": {"name": call_name, "arguments": encoded}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 8, "cost": 0.0004}},
    ]


def test_reasoning_details_fragments_merge_by_index_not_appended(my_predbat):
    """Three reasoning_details fragments sharing an index become one merged block, not three.

    Uses the exact fragment shape a real OpenRouter reasoning-model stream produced: three chunks,
    each carrying {"type": "reasoning.text", "text": <piece>, "format": "unknown", "index": 0}.
    OpenRouter's own replay contract requires "the entire sequence of consecutive reasoning blocks
    must match the outputs generated by the model during the original request" - three separately
    appended entries where the model emitted one block would violate that the moment it round-
    trips. Mutation-checked by hand: switching the merge to a naive per-fragment append makes this
    fail with 3 entries instead of 1 (see the task report for that check, done and reverted).
    """
    failed = False
    print("**** Testing reasoning_details fragments merge onto one block by index ****")
    agent = _agent_with_fake(my_predbat, _reasoning_tool_call_response(), _text_response("done"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why charging?"))

    messages = asyncio.run(agent.store.get_messages(cid))
    with_reasoning = [message for message in messages if message.get("role") == "assistant" and message.get("reasoning_details")]
    if not with_reasoning:
        print("ERROR: no assistant message carries reasoning_details: {}".format(messages))
        return True
    details = with_reasoning[0]["reasoning_details"]
    if len(details) != 1:
        print("ERROR: expected one merged reasoning block, got {}: {}".format(len(details), details))
        failed = True
    elif details[0].get("text") != 'The user asks: "What is':
        print("ERROR: the merged block's text is not the in-order concatenation of its fragments: {!r}".format(details[0].get("text")))
        failed = True
    if details and details[0].get("type") != "reasoning.text":
        print("ERROR: the merged block lost its type field: {}".format(details[0]))
        failed = True

    return failed


def test_reasoning_details_round_trip_to_the_next_request_in_order(my_predbat):
    """reasoning_details assembled from turn 1 reach turn 2's request messages, unchanged and in order.

    OpenRouter's documentation requires the whole reasoning_details array to be sent back verbatim
    on the next request ("preserve the complete reasoning_details when passing back"), so this
    checks the actual bytes sent on the SECOND round trip, not merely that the field survived
    storage. Asserting only "non-empty" would prove nothing about ordering; this compares the full
    assembled list against what the second request's payload actually carries.
    """
    failed = False
    print("**** Testing reasoning_details round-trips into the next request in order ****")
    agent = _agent_with_fake(my_predbat, _reasoning_tool_call_response(), _text_response("done"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why charging?"))

    if len(agent.fake.payloads) != 2:
        print("ERROR: expected two round trips, got {}".format(len(agent.fake.payloads)))
        return True
    first_stored = [message for message in asyncio.run(agent.store.get_messages(cid)) if message.get("role") == "assistant" and message.get("reasoning_details")]
    if not first_stored:
        print("ERROR: nothing stored a reasoning_details block to round-trip")
        return True
    expected = first_stored[0]["reasoning_details"]

    replayed = [message for message in agent.fake.payloads[1]["messages"] if message.get("role") == "assistant" and message.get("reasoning_details")]
    if not replayed:
        print("ERROR: the second request never replayed reasoning_details back to the model")
        failed = True
    elif replayed[0]["reasoning_details"] != expected:
        print("ERROR: the replayed reasoning_details do not match what was stored, in order: {} != {}".format(replayed[0]["reasoning_details"], expected))
        failed = True

    return failed


def _reasoning_no_index_response(call_name="get_status"):
    """Build a chunk list with two reasoning_details fragments that carry neither index nor id -
    the task's explicit fallback case: "if they do not [carry an index or id], append in order"
    rather than merge.
    """
    return [
        {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "First block."}]}}]},
        {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "Second block."}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": call_name, "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.0001}},
    ]


def test_reasoning_details_fragments_without_index_or_id_are_appended_not_merged(my_predbat):
    """Fragments carrying neither an index nor an id are kept as separate blocks, in order.

    The merge-by-index/id rule (see test_reasoning_details_fragments_merge_by_index_not_appended)
    only applies when a fragment actually carries something to merge on. Real OpenRouter fragments
    always carry an index, but the task's own fallback rule for the case where they do not is
    explicit: "if they do not, append in order" - collapsing two unrelated blocks onto the same
    entry just because both happened to lack an index would be exactly as wrong as merging blocks
    that do carry one.
    """
    failed = False
    print("**** Testing reasoning_details fragments without index or id are appended, not merged ****")
    agent = _agent_with_fake(my_predbat, _reasoning_no_index_response(), _text_response("done"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why?"))

    messages = asyncio.run(agent.store.get_messages(cid))
    with_reasoning = [message for message in messages if message.get("role") == "assistant" and message.get("reasoning_details")]
    if not with_reasoning:
        print("ERROR: no assistant message carries reasoning_details: {}".format(messages))
        return True
    details = with_reasoning[0]["reasoning_details"]
    texts = [entry.get("text") for entry in details]
    if texts != ["First block.", "Second block."]:
        print("ERROR: expected two separate, in-order blocks, got {}".format(texts))
        failed = True

    return failed


def _plain_reasoning_fallback_response(text="thinking without structured details"):
    """Build a chunk list carrying only delta.reasoning strings, no reasoning_details at all."""
    words = text.split(" ")
    chunks = [{"choices": [{"delta": {"reasoning": word + " "}}]} for word in words]
    chunks[-1]["choices"][0]["delta"]["content"] = "answer"
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cost": 0.0001}})
    return chunks


def test_plain_reasoning_string_is_captured_as_a_fallback(my_predbat):
    """delta.reasoning is captured as message['reasoning'] when reasoning_details is absent."""
    failed = False
    print("**** Testing the plain delta.reasoning fallback is captured ****")
    agent = _agent_with_fake(my_predbat, _plain_reasoning_fallback_response())
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why?"))

    messages = asyncio.run(agent.store.get_messages(cid))
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant:
        print("ERROR: no assistant message was stored")
        return True
    if "thinking without structured details" not in (assistant[0].get("reasoning") or ""):
        print("ERROR: the plain reasoning fallback was not captured: {!r}".format(assistant[0].get("reasoning")))
        failed = True
    if assistant[0].get("reasoning_details"):
        print("ERROR: reasoning_details was populated despite no structured fragments being sent: {}".format(assistant[0].get("reasoning_details")))
        failed = True
    if assistant[0].get("content") != "answer":
        print("ERROR: ordinary content alongside the reasoning fallback was lost: {!r}".format(assistant[0].get("content")))
        failed = True

    return failed


def test_classify_completion_failure_matches_the_documented_retry_policy(my_predbat):
    """classify_completion_failure() sorts every documented failure kind into retry-or-not.

    A direct unit test of the classifier, independent of the agentic loop, exactly because the
    task that added it asked for the rule to be "a small named function... testable on its own,
    rather than a condition buried in the loop". Covers every case named in that task: 502/503 and
    a mid-stream marker/message match retry as "provider trouble", 429 retries with rate_limited
    True, 401/402/400 fail immediately, finish_reason "error" with no chunk-level error object
    still retries, and both aiohttp.ClientError and asyncio.TimeoutError retry unconditionally. An
    unused Predbat instance is accepted only because every test in this file is called with one by
    the driver.
    """
    failed = False
    print("**** Testing classify_completion_failure against the documented retry policy ****")

    cases = [
        (ChatRequestError(502, "bad gateway"), True, False),
        (ChatRequestError(503, "unavailable"), True, False),
        (ChatRequestError(429, "slow down"), True, True),
        (ChatRequestError(401, "bad key"), False, False),
        (ChatRequestError(402, "no credit"), False, False),
        (ChatRequestError(400, "malformed"), False, False),
        (ChatRequestError("server_error", '{"code": "server_error"}', provider_message="Upstream error from Nvidia: Service temporarily overloaded"), True, False),
        (ChatRequestError(None, "{}", provider_message="Something else", error_type="provider_unavailable"), True, False),
        (ChatRequestError(None, "{}", provider_message="The upstream host is Overloaded right now"), True, False),
        (ChatRequestError(None, "{}", provider_message="The provider ended the response with an error"), True, False),
        (ChatRequestError(418, "teapot"), False, False),
        (aiohttp.ClientConnectionError("connection reset"), True, False),
        (asyncio.TimeoutError(), True, False),
        (ValueError("not a completion failure at all"), False, False),
    ]
    for error, expect_retryable, expect_rate_limited in cases:
        retryable, reason, rate_limited = classify_completion_failure(error)
        if retryable != expect_retryable:
            print("ERROR: {!r} classified as retryable={}, expected {}".format(error, retryable, expect_retryable))
            failed = True
        if rate_limited != expect_rate_limited:
            print("ERROR: {!r} classified as rate_limited={}, expected {}".format(error, rate_limited, expect_rate_limited))
            failed = True
        if expect_retryable and not reason:
            print("ERROR: {!r} was classified retryable with no reason to show the user".format(error))
            failed = True
        if not expect_retryable and reason:
            print("ERROR: {!r} was classified not-retryable but still carries a reason {!r}".format(error, reason))
            failed = True

    return failed


def test_is_empty_completion(my_predbat):
    """is_empty_completion() is true only for a message with neither content nor a tool call."""
    failed = False
    print("**** Testing is_empty_completion ****")
    cases = [
        ({"role": "assistant", "content": "an answer"}, False),
        ({"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]}, False),
        ({"role": "assistant", "content": None}, True),
        ({"role": "assistant", "content": ""}, True),
        ({"role": "assistant", "content": None, "reasoning": "thinking..."}, True),
    ]
    for message, expected in cases:
        actual = is_empty_completion(message)
        if actual != expected:
            print("ERROR: is_empty_completion({}) = {}, expected {}".format(message, actual, expected))
            failed = True
    return failed


def test_non_retryable_statuses_fail_on_the_first_attempt(my_predbat):
    """A 401, 402 or 400 fails immediately, unchanged, with exactly one attempt made.

    Asserting the attempt count, not merely that the turn failed, is the whole point: a wrapper
    that retried a 401 three times before giving up would still leave the turn failed and would
    still show the 401's own message, so a test that checked only those two things could not tell
    a broken "retries everything" implementation from a correct one. See the task's own warning
    about exactly this trap.
    """
    failed = False
    print("**** Testing 401/402/400 fail on the first attempt, not retried ****")
    for status, expected_phrase in ((401, "rejected the API key"), (402, "insufficient credit"), (400, "malformed request")):
        agent = _agent_with_fake(my_predbat, ChatRequestError(status, "malformed request" if status == 400 else "denied"))
        cid = asyncio.run(agent.store.create())
        asyncio.run(agent.run_turn(cid, "hello"))

        if len(agent.fake.payloads) != 1:
            print("ERROR: status {} was retried - expected exactly 1 attempt, the fake was called {} times".format(status, len(agent.fake.payloads)))
            failed = True
        events = agent.events_since(0, cid)[0]
        if any(event["type"] == "retry" for event in events):
            print("ERROR: status {} emitted a 'retry' event despite being non-retryable".format(status))
            failed = True
        errors = [event for event in events if event["type"] == "error"]
        if not errors:
            print("ERROR: status {} produced no error event".format(status))
            failed = True
            continue
        message = errors[0]["data"].get("message", "")
        if "Gave up after" in message:
            print("ERROR: status {} message names a retry count despite never retrying: {!r}".format(status, message))
            failed = True
        if status in (401, 402) and expected_phrase.lower() not in message.lower():
            print("ERROR: status {} lost its documented wording: {!r}".format(status, message))
            failed = True
        if agent.retry_sleeps:
            print("ERROR: status {} requested a backoff sleep despite never retrying: {}".format(status, agent.retry_sleeps))
            failed = True

    return failed


def test_mid_stream_provider_overload_retries_and_recovers(my_predbat):
    """A retryable mid-stream error on the first attempt is retried, and a clean second attempt
    succeeds - the turn ends normally with the second attempt's answer, having made two attempts.
    """
    failed = False
    print("**** Testing a retryable mid-stream error is retried and recovers ****")
    agent = _agent_with_fake(my_predbat, _mid_stream_error_response(), _text_response("confirmed"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why is it charging?"))

    if len(agent.fake.payloads) != 2:
        print("ERROR: expected exactly 2 completion attempts (one failure, one recovery), got {}".format(len(agent.fake.payloads)))
        failed = True

    events = agent.events_since(0, cid)[0]
    retries = [event for event in events if event["type"] == "retry"]
    if len(retries) != 1:
        print("ERROR: expected exactly one 'retry' event, got {}: {}".format(len(retries), retries))
        return True
    retry = retries[0]["data"]
    if retry.get("attempt") != 2 or retry.get("of") != COMPLETION_MAX_ATTEMPTS:
        print("ERROR: the retry event does not name attempt 2 of {}: {}".format(COMPLETION_MAX_ATTEMPTS, retry))
        failed = True
    if not retry.get("reason"):
        print("ERROR: the retry event carries no reason: {}".format(retry))
        failed = True
    if retry.get("delay") != COMPLETION_RETRY_DELAYS_SECONDS[0]:
        print("ERROR: the retry event's delay is {}, expected the first backoff {}".format(retry.get("delay"), COMPLETION_RETRY_DELAYS_SECONDS[0]))
        failed = True

    if [event["type"] for event in events].count("error") != 0:
        print("ERROR: a recovered turn should not emit an 'error' event")
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant or assistant[0].get("content") != "confirmed":
        print("ERROR: the recovered turn's stored answer is wrong: {}".format(assistant))
        failed = True

    if agent.retry_sleeps != [COMPLETION_RETRY_DELAYS_SECONDS[0]]:
        print("ERROR: expected exactly one backoff sleep of {}, agent requested {}".format(COMPLETION_RETRY_DELAYS_SECONDS[0], agent.retry_sleeps))
        failed = True

    return failed


def test_rate_limited_retry_uses_the_longer_first_delay(my_predbat):
    """A 429 is retried, but with COMPLETION_RATE_LIMIT_RETRY_DELAY_SECONDS rather than the plain
    first backoff - a provider's rate-limit window is unlikely to have cleared inside one second.
    """
    failed = False
    print("**** Testing a 429 retries with the longer rate-limit backoff ****")
    agent = _agent_with_fake(my_predbat, ChatRequestError(429, "slow down"), _text_response("ok now"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "hello"))

    if agent.retry_sleeps != [COMPLETION_RATE_LIMIT_RETRY_DELAY_SECONDS]:
        print("ERROR: expected the rate-limit backoff {}, agent requested {}".format(COMPLETION_RATE_LIMIT_RETRY_DELAY_SECONDS, agent.retry_sleeps))
        failed = True

    events = agent.events_since(0, cid)[0]
    retries = [event for event in events if event["type"] == "retry"]
    if not retries or retries[0]["data"].get("delay") != COMPLETION_RATE_LIMIT_RETRY_DELAY_SECONDS:
        print("ERROR: the retry event does not carry the rate-limit delay: {}".format(retries))
        failed = True

    return failed


def test_retry_backoff_sequence_is_one_then_three_seconds(my_predbat):
    """Three attempts that all fail with a plain retryable error back off 1s then 3s - the module
    constants named in the task, not hand-rolled numbers - and the suite never actually sleeps for
    either, since agent._retry_sleep only records what was requested.
    """
    failed = False
    print("**** Testing the plain retry backoff sequence is 1s then 3s ****")
    agent = _agent_with_fake(my_predbat, *[_mid_stream_error_response() for _ in range(COMPLETION_MAX_ATTEMPTS)])
    cid = asyncio.run(agent.store.create())

    started = time.monotonic()
    asyncio.run(agent.run_turn(cid, "hello"))
    elapsed = time.monotonic() - started

    if agent.retry_sleeps != list(COMPLETION_RETRY_DELAYS_SECONDS):
        print("ERROR: expected the backoff sequence {}, agent requested {}".format(list(COMPLETION_RETRY_DELAYS_SECONDS), agent.retry_sleeps))
        failed = True
    if elapsed > 2:
        print("ERROR: the turn took {:.2f}s - the injected sleep is not actually replacing the real backoff".format(elapsed))
        failed = True

    return failed


def test_retry_never_sleeps_past_the_turn_deadline(my_predbat):
    """A retry is not attempted when the remaining turn budget is smaller than the next backoff -
    the wrapper fails with the last error immediately, having requested no sleep at all, rather
    than sleeping into a self-inflicted timeout. Drives _run_completion_with_retry directly, with
    agent.deadline set to leave less time than COMPLETION_RETRY_DELAYS_SECONDS[0] (1s), so this
    does not depend on winning a real-clock race against the suite's own execution time.
    """
    failed = False
    print("**** Testing a retry backoff that would exceed the turn deadline gives up instead of sleeping ****")
    agent = _agent_with_fake(my_predbat)
    calls = []

    async def _always_overloaded(conversation_id, messages, model):
        """Fail every attempt with a retryable mid-stream-shaped error, counting how many ran."""
        calls.append(1)
        raise ChatRequestError(502, "bad gateway", provider_message="Service temporarily overloaded")

    agent._run_completion = _always_overloaded
    agent.deadline = time.monotonic() + (COMPLETION_RETRY_DELAYS_SECONDS[0] / 2)

    try:
        asyncio.run(agent._run_completion_with_retry("cid", [], "test/model"))
        print("ERROR: expected _run_completion_with_retry to raise once the deadline could not fit the backoff")
        return True
    except ChatRequestError as error:
        if "Gave up after 1 attempt" not in error.friendly():
            print("ERROR: the give-up message does not name the single attempt made: {!r}".format(error.friendly()))
            failed = True

    if len(calls) != 1:
        print("ERROR: expected exactly 1 completion attempt before giving up on a deadline too small for the backoff, got {}".format(len(calls)))
        failed = True
    if agent.retry_sleeps:
        print("ERROR: a sleep was requested even though the remaining deadline budget was smaller than the backoff: {}".format(agent.retry_sleeps))
        failed = True

    return failed


def _partial_then_mid_stream_error_response():
    """Build a chunk list that streams some real content before the mid-stream error chunk.

    Reproduces OpenRouter's own documented shape: "errors can occur after streaming has started".
    A retry that resends this failed attempt's partial text alongside the second attempt's answer
    is exactly the bug the task's 'retry' event exists to prevent on the client side; on the
    server side, the guard against it is simpler still - _run_completion starts a fresh local
    accumulator on every call, so nothing from this attempt's "Batt" + "ery is " can survive into
    the next one's message. The test below asserts that directly.
    """
    return [
        {"choices": [{"delta": {"content": "Batt"}}]},
        {"choices": [{"delta": {"content": "ery is "}}]},
        {"id": "cmpl-partial", "error": {"code": "server_error", "message": "Provider disconnected unexpectedly"}, "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]},
    ]


def test_retried_attempt_does_not_duplicate_the_failed_attempts_partial_content(my_predbat):
    """A retry after partial content has already streamed stores only the second attempt's answer
    - never the first attempt's partial text concatenated with it.

    This is the actual bug the task's partial-content handling exists to prevent: a naive retry
    that kept accumulating onto the same buffer across attempts would store "Battery is charging
    because rates are low" (the discarded "Batt" + "ery is " glued onto the real answer). Asserting
    an exact match, not a substring ("in"), is what catches that - a substring check would pass
    just as happily on the duplicated text as on the correct one.
    """
    failed = False
    print("**** Testing a retried attempt's stored message excludes the failed attempt's partial content ****")
    agent = _agent_with_fake(my_predbat, _partial_then_mid_stream_error_response(), _text_response("resolved"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why is it charging?"))

    messages = asyncio.run(agent.store.get_messages(cid))
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant:
        print("ERROR: no assistant message was stored")
        return True
    content = assistant[0].get("content")
    if content != "resolved":
        print("ERROR: the stored answer is {!r}, expected only the second attempt's text ('resolved') with none of the first attempt's partial content ('Batt'/'ery is ') concatenated onto it".format(content))
        failed = True

    # The first attempt's partial text was genuinely streamed to the browser in real time (delta
    # events), which is correct and expected - it is the client's job, on the 'retry' event, to
    # discard whatever bubble those deltas had built. So this checks the deltas are present...
    deltas = "".join(event["data"].get("text", "") for event in agent.events_since(0, cid)[0] if event["type"] == "delta")
    if "Batt" not in deltas:
        print("ERROR: the first attempt's partial content was not streamed as deltas at all: {!r}".format(deltas))
        failed = True
    # ...but that the final 'assistant' event (what a client uses once a bubble is not mid-stream,
    # e.g. after a reload) carries only the recovered answer, not the discarded partial glued on.
    finals = [event["data"].get("text") for event in agent.events_since(0, cid)[0] if event["type"] == "assistant"]
    if finals != ["resolved"]:
        print("ERROR: the final 'assistant' event text is {}, expected only the second attempt's answer".format(finals))
        failed = True

    return failed


def test_empty_completion_is_retried_and_recovers(my_predbat):
    """A reasoning-only (empty) completion on the first attempt is retried, and a clean second
    attempt with a real answer succeeds - the turn ends normally, having made two attempts.
    """
    failed = False
    print("**** Testing an empty completion is retried and recovers ****")
    agent = _agent_with_fake(my_predbat, _reasoning_only_response(), _text_response("recovered"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why is it charging?"))

    if len(agent.fake.payloads) != 2:
        print("ERROR: expected exactly 2 completion attempts (one empty, one recovery), got {}".format(len(agent.fake.payloads)))
        failed = True

    events = agent.events_since(0, cid)[0]
    if [event["type"] for event in events].count("error") != 0:
        print("ERROR: a recovered turn should not emit an 'error' event")
        failed = True
    retries = [event for event in events if event["type"] == "retry"]
    if len(retries) != 1:
        print("ERROR: expected exactly one 'retry' event for the empty completion, got {}: {}".format(len(retries), retries))
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    assistant = [message for message in messages if message.get("role") == "assistant"]
    if not assistant or assistant[0].get("content") != "recovered":
        print("ERROR: the recovered turn's stored answer is wrong: {}".format(assistant))
        failed = True

    return failed


def run_chat_tests(my_predbat):
    """Run every chat agent test, returning True if any of them failed."""
    failed = False
    failed |= test_component_gating(my_predbat)
    failed |= test_component_gating_end_to_end(my_predbat)
    failed |= test_build_snapshot(my_predbat)
    failed |= test_event_buffer(my_predbat)
    failed |= test_pending_conversations_is_lock_guarded(my_predbat)
    failed |= test_pending_conversations_survives_concurrent_mutation(my_predbat)
    failed |= test_release_stale_turn(my_predbat)
    failed |= test_release_stale_turn_keeps_a_slot_parked_in_confirmation(my_predbat)
    failed |= test_plain_answer(my_predbat)
    failed |= test_tool_call_round_trip(my_predbat)
    failed |= test_dispatch_strips_chat_omit_properties(my_predbat)
    failed |= test_tool_call_ids_are_normalised_when_the_provider_omits_them(my_predbat)
    failed |= test_tool_call_cap(my_predbat)
    failed |= test_tool_failures_are_results(my_predbat)
    failed |= test_titles(my_predbat)
    failed |= test_execute_turn_preserves_a_slot_claimed_by_a_later_turn(my_predbat)
    failed |= test_busy_rejects_a_second_turn(my_predbat)
    failed |= test_search_source_runs_off_the_loop(my_predbat)
    failed |= test_submit_turn_hands_off_to_the_component_loop(my_predbat)
    failed |= test_submit_turn_needs_a_running_component(my_predbat)
    failed |= test_write_confirmation_approved(my_predbat)
    failed |= test_write_confirmation_rejected(my_predbat)
    failed |= test_write_confirmation_timeout(my_predbat)
    failed |= test_write_without_confirmation(my_predbat)
    failed |= test_web_search_switch(my_predbat)
    failed |= test_mid_stream_error_chunk_fails_the_turn_loudly_after_exhausting_every_retry(my_predbat)
    failed |= test_finish_reason_length_keeps_partial_content_with_a_truncation_note(my_predbat)
    failed |= test_reasoning_only_completion_is_retried_then_reported_as_an_anomaly_if_it_never_clears(my_predbat)
    failed |= test_reasoning_details_fragments_merge_by_index_not_appended(my_predbat)
    failed |= test_reasoning_details_round_trip_to_the_next_request_in_order(my_predbat)
    failed |= test_reasoning_details_fragments_without_index_or_id_are_appended_not_merged(my_predbat)
    failed |= test_plain_reasoning_string_is_captured_as_a_fallback(my_predbat)
    failed |= test_classify_completion_failure_matches_the_documented_retry_policy(my_predbat)
    failed |= test_is_empty_completion(my_predbat)
    failed |= test_non_retryable_statuses_fail_on_the_first_attempt(my_predbat)
    failed |= test_mid_stream_provider_overload_retries_and_recovers(my_predbat)
    failed |= test_rate_limited_retry_uses_the_longer_first_delay(my_predbat)
    failed |= test_retry_backoff_sequence_is_one_then_three_seconds(my_predbat)
    failed |= test_retry_never_sleeps_past_the_turn_deadline(my_predbat)
    failed |= test_retried_attempt_does_not_duplicate_the_failed_attempts_partial_content(my_predbat)
    failed |= test_empty_completion_is_retried_and_recovers(my_predbat)
    return failed
