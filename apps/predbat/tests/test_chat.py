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
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import aiohttp

import chat
from chat import (
    COMPLETION_MAX_ATTEMPTS,
    COMPLETION_RATE_LIMIT_DELAYS_SECONDS,
    COMPLETION_RATE_LIMIT_MAX_ATTEMPTS,
    max_attempts_for,
    OPENROUTER_BASE_URL,
    resolve_provider,
    parse_retry_after,
    retry_delay_for,
    RETRY_AFTER_MAX_SECONDS,
    COMPLETION_RETRY_DELAYS_SECONDS,
    EVENT_BUFFER_MAX,
    PRIMER,
    STALE_TURN_GRACE_SECONDS,
    SYSTEM_PROMPT_CACHE_CONTROL,
    TITLE_INSTRUCTION,
    ChatAgent,
    ChatBusyError,
    ChatRequestError,
    build_snapshot,
    build_system_prompt,
    classify_completion_failure,
    is_empty_completion,
)
from chat_tools import APPS_YAML_RESTART_WARNING
from chat_store import ConversationStore
from tests.test_chat_store import FakeStorage
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
    settings = {
        "api_key": "test-key",
        "model": "test/model",
        # The real host, not a stand-in: chat_api_type auto-detects the provider from the URL,
        # so a fake hostname is classified as a generic OpenAI-compatible endpoint and the
        # OpenRouter-specific behaviour under test here never runs. Nothing is dialled - the
        # stream is stubbed - so the real name costs nothing.
        "base_url": "https://openrouter.ai/api/v1",
        "max_tokens": 0,
        "max_tool_rounds": 4,
        "max_history": 40,
        "max_conversations": 20,
        "expiry_days": 30,
        "turn_timeout": 30,
        "request_timeout": 30,
        "fetch_allowlist": None,
    }
    settings.update(overrides)
    agent.initialize(**settings)
    return agent


def test_component_gating(my_predbat):
    """The chat component is gated on the API key alone; the model is optional.

    openrouter_default_model being optional is the point: a user who has only pasted an API key
    gets a working Chat tab and picks a model from the search box, which is then remembered. If
    'model' were required again, that install would silently have no Chat tab at all and nothing
    would say why.
    """
    failed = False
    print("**** Testing chat component gating ****")
    entry = COMPONENT_LIST.get("chat")
    if entry is None:
        print("ERROR: 'chat' is not registered in COMPONENT_LIST")
        return True

    # The component takes no required arguments at all and always starts. The Chat tab configures
    # its own providers - adding one writes apps.yaml - so the component has to be running before
    # any provider exists, or there is nothing to configure it from.
    for name, spec in entry["args"].items():
        if spec.get("required"):
            print("ERROR: {!r} is required, which would stop the component starting unconfigured".format(name))
            failed = True
    if entry.get("required_or"):
        print("ERROR: chat still has a required_or gate, so it cannot start unconfigured: {}".format(entry["required_or"]))
        failed = True
    for name in ("model", "base_url", "max_tool_rounds", "max_history", "max_conversations", "expiry_days", "turn_timeout", "request_timeout", "fetch_allowlist", "max_tokens"):
        if entry["args"].get(name, {}).get("required"):
            print("ERROR: '{}' is required, which would stop the component starting on a default install".format(name))
            failed = True
    bindings = {"api_key": "chat_api_key", "base_url": "chat_api_url", "api_type": "chat_api_type", "model": "chat_model", "legacy_api_key": "openrouter_api_key", "legacy_base_url": "openrouter_base_url", "legacy_model": "openrouter_default_model"}
    for arg, config_key in bindings.items():
        if entry["args"].get(arg, {}).get("config") != config_key:
            print("ERROR: {!r} is bound to {!r}, expected {!r}".format(arg, entry["args"].get(arg, {}).get("config"), config_key))
            failed = True
    if not entry.get("can_restart"):
        print("ERROR: the chat component should be restartable from the Components tab")
        failed = True
    if entry["args"]["max_tool_rounds"]["config"] != "chat_max_tool_rounds":
        print("ERROR: max_tool_rounds is not bound to chat_max_tool_rounds: {}".format(entry["args"]["max_tool_rounds"]))
        failed = True
    if entry["args"]["max_history"].get("default") != 0:
        print("ERROR: chat_max_history should default to 0 (unlimited), got {}".format(entry["args"]["max_history"].get("default")))
        failed = True
    if entry["args"]["turn_timeout"].get("default") != 1800:
        print("ERROR: chat_turn_timeout should default to 1800s (the whole-turn budget), got {}".format(entry["args"]["turn_timeout"].get("default")))
        failed = True
    if entry["args"]["request_timeout"].get("default") != 300 or entry["args"]["request_timeout"]["config"] != "chat_request_timeout":
        print("ERROR: request_timeout should default to 300s and bind to chat_request_timeout: {}".format(entry["args"]["request_timeout"]))
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
    stub base for each configuration state it can be in.
    """
    failed = False
    print("**** Testing chat gating via Components.initialize() ****")

    # Always constructed, whatever is or is not configured: the Chat tab configures its own
    # providers by writing apps.yaml, so the component must exist before any provider does.
    for description, args in (
        ("nothing configured", {}),
        ("only the legacy key", {"openrouter_api_key": "sk-test"}),
        ("only a local url", {"chat_api_url": "http://localhost:11434/v1"}),
        ("a named provider block", {"chat": {"ollama": {"url": "http://localhost:11434/v1"}}}),
        ("a model but nothing to run it on", {"chat_model": "test/model"}),
    ):
        base = _FakeBase(args)
        comps = Components(base)
        comps.initialize(only="chat", phase=1)
        if comps.components.get("chat") is None:
            print("ERROR: chat component was not constructed with {}".format(description))
            failed = True

    # An install with nothing configured must not COMPLAIN about it. One ordinary init line is
    # expected now the component always starts, the same as every other component logs - what
    # would be wrong is a warning or error nagging a user who simply does not use chat. The Chat
    # tab says what is missing, in the one place someone is looking when they care.
    base = _FakeBase({})
    comps = Components(base)
    comps.initialize(only="chat", phase=1)
    complaints = [line for line in base.logged if "Warn" in line or "Error" in line]
    if complaints:
        print("ERROR: an unconfigured install complained: {}".format(complaints))
        failed = True

    # But it knows it cannot answer a turn, which is what the setup page keys off.
    agent = comps.components.get("chat")
    if agent is not None and agent.provider_ready():
        print("ERROR: an unconfigured agent reports itself ready to answer")
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
    # "Time now" reads as current inside a block headed "...as it was when this conversation
    # started" - a model can read it as live rather than frozen, which fights the staleness caveat
    # build_system_prompt() adds right after this snapshot. "Conversation started at" says the same
    # thing the heading already does, so nothing in the snapshot contradicts it.
    if "Time now" in snapshot:
        print("ERROR: the snapshot still says 'Time now', which reads as current inside a frozen snapshot: {}".format(snapshot))
        failed = True
    if "Conversation started at" not in snapshot:
        print("ERROR: the snapshot does not label its captured timestamp 'Conversation started at':\n{}".format(snapshot))
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


def test_build_system_prompt(my_predbat):
    """build_system_prompt() wraps the snapshot with the primer and a staleness caveat, and
    returns the captured_at it used to write it.

    The capture time is stated exactly once - by the snapshot's own first line, which carries a
    weekday. The caveat restated it in a second format immediately below, which is repetition the
    model pays for on every cached turn.
    """
    failed = False
    print("**** Testing build_system_prompt ****")
    prompt, captured_at = build_system_prompt(my_predbat)

    if not isinstance(captured_at, datetime):
        print("ERROR: build_system_prompt did not return a datetime as captured_at: {!r}".format(captured_at))
        return True
    # The capture time is stated once, by the snapshot's own first line. The caveat used to
    # restate it in a different format three lines later, which spent tokens saying the same thing
    # twice - so this asserts it appears exactly once, not that it is present at all.
    stamped = captured_at.strftime("%a %Y-%m-%d")
    if prompt.count(stamped) != 1:
        print("ERROR: the capture time {!r} appears {} times, expected exactly once:\n{}".format(stamped, prompt.count(stamped), prompt))
        failed = True
    if captured_at.strftime("%H:%M on %d %B %Y") in prompt:
        print("ERROR: the caveat restates the capture time the snapshot already gave:\n{}".format(prompt))
        failed = True
    for needle in ("get_status", "get_plan", "captured", "frozen"):
        if needle not in prompt:
            print("ERROR: the prompt is missing {!r}, which the staleness caveat is supposed to say:\n{}".format(needle, prompt))
            failed = True
    if PRIMER not in prompt:
        print("ERROR: the primer is missing from the built system prompt")
        failed = True

    # The two kinds of setting are changed by different tools, and using the wrong one used to
    # fail silently. The prompt has to say which is which, and name an example of the apps.yaml
    # side - "it is a setting" is not enough to tell them apart.
    for needle in ("set_config", "set_apps_config", "car_charging_exclusive"):
        if needle not in prompt:
            print("ERROR: the prompt does not explain the live/apps.yaml split: missing {!r}".format(needle))
            failed = True

    # A model's training data contains some older Predbat, whose settings have since been renamed
    # or removed, and it will state them with complete confidence. The prompt has to say not to
    # answer configuration questions from recollection, and name what to check instead.
    lowered = prompt.lower()
    if "memory" not in lowered and "recall" not in lowered:
        print("ERROR: the prompt does not tell the model to stop answering configuration questions from memory:\n{}".format(prompt))
        failed = True
    for needle in ("search_docs", "search_source"):
        if needle not in prompt:
            print("ERROR: the prompt does not name {!r} as what to check instead of recalling".format(needle))
            failed = True
    if build_snapshot(my_predbat) not in prompt:
        print("ERROR: the live snapshot is missing from the built system prompt")
        failed = True

    return failed


def test_primer_says_when_not_to_call_a_tool(my_predbat):
    """The primer tells the model to prefer the snapshot for a simple fact, not only how to call
    tools. With the round cap now generous (32, see max_tool_rounds), nothing else in the prompt
    discourages calling get_plan/get_state/search_source for a question the snapshot already
    answers - this line is what is meant to.
    """
    failed = False
    print("**** Testing the primer explains when NOT to call a tool ****")
    lowered = PRIMER.lower()
    for needle in ("snapshot", "current value", "about to act on"):
        if needle not in lowered:
            print("ERROR: the primer is missing {!r}, which is meant to steer the model away from reflexively calling a tool:\n{}".format(needle, PRIMER))
            failed = True
    return failed


class _TickingBase:
    """A minimal base whose now_utc reads a fresh, later timestamp on every access.

    Stands in for a real Predbat base in the cache-stability test below, deliberately instead of
    my_predbat: my_predbat is a single instance every test in this suite shares, so mutating its
    now_utc to prove a rebuild would happen would leak into whichever test runs next. This is
    self-contained and also counts its own reads, which is what lets the test assert not just that
    two prompts matched (which a coincidence could also produce) but that the second build never
    consulted the clock at all.
    """

    prefix = "predbat"
    args = {}

    def __init__(self):
        """Start the simulated clock at a fixed moment with no reads yet."""
        self.reads = 0
        self._start = datetime(2026, 8, 28, 9, 15, tzinfo=timezone.utc)

    @property
    def now_utc(self):
        """Return a timestamp one hour later than the previous read, counting the read."""
        value = self._start + timedelta(hours=self.reads)
        self.reads += 1
        return value

    def get_arg(self, name, default=None, **kwargs):
        """Return the default for every argument - this fake only cares about the clock."""
        return default

    def get_ha_config(self, name, default):
        """Return the default for every config item."""
        return default, False


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


def _many_tool_calls_response(name, arguments, count):
    """Build a chunk list for `count` calls of the same tool streamed in one round trip, each with
    its own real id - the shape used to prove the turn deadline is checked between individual
    tool calls within a round, not only once per round trip."""
    chunks = []
    for index in range(count):
        chunks.append({"choices": [{"delta": {"tool_calls": [{"index": index, "id": "call_{}".format(index), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]})
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


def _system_text(message):
    """Return the text inside a system message's single content block.

    build_messages() sends the system message's content as a one-element array carrying a
    cache_control breakpoint rather than a plain string (see chat.py's module docstring for why) -
    this pulls the prompt text back out so callers can keep making the same substring/equality
    assertions against it that they made when content was a plain string. Deliberately does not
    fall back to a bare string: a test wanting the raw content list itself, cache_control included,
    should just read message["content"] directly, as test_system_message_carries_cache_control()
    does.
    """
    return message["content"][0]["text"]


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


def test_tool_round_cap(my_predbat):
    """The loop stops at max_tool_rounds and says so rather than spinning.

    max_tool_rounds bounds model round trips (completions), not individual tool calls - see
    _turn_loop's docstring - so this drives a fake that returns one tool call per round trip,
    which is exactly the shape that makes "round" and "call" indistinguishable and is why the cap
    used to be named after the wrong one of the two.
    """
    failed = False
    print("**** Testing the tool round cap ****")
    responses = [_tool_call_response("get_status", {}, call_id="call_{}".format(index)) for index in range(10)]
    agent = _agent_with_fake(my_predbat, *responses, max_tool_rounds=2)
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


def test_deadline_checked_between_tool_calls_within_a_round(my_predbat):
    """The turn deadline is checked between individual tool calls, not only once per round.

    A round is one completion - the per-round check at the top of _turn_loop's for-loop only runs
    before that completion is requested. A model can put an unbounded number of tool calls in the
    single message that completion returns, and each one dispatched inside _run_one_tool can itself
    take real time; with no check between them, one round emitting twenty tool calls at up to five
    seconds each could run for roughly a hundred seconds with no deadline check at all. This drives
    a single round of 20 tool calls and expires the deadline immediately after the third one runs,
    then asserts execution actually stopped there - not merely that an error was eventually
    emitted, which a check running only after all 20 had executed would also produce.
    """
    failed = False
    print("**** Testing the turn deadline is checked between tool calls within one round, not just between rounds ****")
    round_response = _many_tool_calls_response("get_status", {}, 20)
    agent = _agent_with_fake(my_predbat, round_response, _text_response("done"))
    cid = asyncio.run(agent.store.create())

    executed = []
    real_run_one_tool = agent._run_one_tool

    async def spy_run_one_tool(conversation_id, turn_id, call):
        """Run the real tool, then expire the turn deadline right after the third call runs."""
        executed.append(call.get("id"))
        result = await real_run_one_tool(conversation_id, turn_id, call)
        if len(executed) == 3:
            agent.deadline = time.monotonic() - 1
        return result

    agent._run_one_tool = spy_run_one_tool
    asyncio.run(agent.run_turn(cid, "run lots of tools please"))

    if len(executed) != 3:
        print("ERROR: expected exactly 3 of the 20 tool calls to run before the mid-round deadline check stopped the rest, got {}: {}".format(len(executed), executed))
        failed = True

    events = agent.events_since(0, cid)[0]
    tool_starts = [event for event in events if event["type"] == "tool_start"]
    if len(tool_starts) != 3:
        print("ERROR: expected exactly 3 tool_start events, got {}: {}".format(len(tool_starts), tool_starts))
        failed = True
    error_events = [event for event in events if event["type"] == "error"]
    if len(error_events) != 1 or "seconds" not in str(error_events[0]["data"].get("message", "")):
        print("ERROR: expected exactly one deadline error event naming the timeout, got {}".format(error_events))
        failed = True
    if agent.active is not None:
        print("ERROR: the turn slot was not released after the mid-round deadline stop")
        failed = True

    # The 17 calls that never ran must still have been answered. An assistant message carrying
    # tool_calls with no matching tool reply is rejected by the API with a 400 on the NEXT turn,
    # and with max_history now defaulting to 0 the broken pair is replayed forever rather than
    # ageing out - so this assertion, not the event counts above, is what stops a stopped turn
    # bricking the conversation.
    messages = asyncio.run(agent.store.get_messages(cid))
    problems = _dangling_tool_calls(messages)
    if problems:
        print("ERROR: the mid-round deadline stop left tool_calls with no tool reply: {}".format(problems))
        failed = True

    return failed


def test_stop_button_leaves_history_well_formed(my_predbat):
    """Pressing Stop during a single in-flight tool call leaves a conversation that can still be replayed.

    The Stop button (POST /chat/cancel) zeroes agent.deadline, so it lands on the same mid-round
    check as a genuine timeout - but it is the path a user takes deliberately, and with one tool
    call in the round rather than twenty. That is the shape most likely to occur in practice and
    the one that used to store an assistant tool_calls message with no reply at all.
    """
    failed = False
    print("**** Testing Stop during a tool call leaves history well-formed ****")

    # Two calls, stopped after the first: one call alone would run to completion before the loop
    # ended, never reaching the mid-round check this is about.
    agent = _agent_with_fake(my_predbat, _many_tool_calls_response("get_status", {}, 2), _text_response("stopped"))
    cid = asyncio.run(agent.store.create())

    executed = []
    real_run_one_tool = agent._run_one_tool

    async def spy_run_one_tool(conversation_id, turn_id, call):
        """Run the first call, then zero the deadline the way POST /chat/cancel does."""
        executed.append(call.get("id"))
        result = await real_run_one_tool(conversation_id, turn_id, call)
        agent.deadline = 0
        return result

    agent._run_one_tool = spy_run_one_tool
    asyncio.run(agent.run_turn(cid, "do something then get stopped"))

    if len(executed) != 1:
        print("ERROR: expected Stop to halt after the first of the two calls, got {}: {}".format(len(executed), executed))
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    problems = _dangling_tool_calls(messages)
    if problems:
        print("ERROR: Stop left tool_calls with no tool reply: {}".format(problems))
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

    system_content = _system_text(quiet.fake.payloads[0]["messages"][0])
    if "set_chat_title" in system_content:
        print("ERROR: the title instruction leaked into the system prompt, which must stay frozen: {!r}".format(system_content))
        failed = True
    user_content = quiet.fake.payloads[0]["messages"][-1]["content"]
    if "set_chat_title" not in user_content:
        print("ERROR: the title instruction was missing from the outgoing user message while untitled: {!r}".format(user_content))
        failed = True

    titled = _agent_with_fake(my_predbat, _text_response("second turn"))
    titled.store = quiet.store
    asyncio.run(titled.run_turn(cid2, "and my import rate?"))
    second_request = titled.fake.payloads[0]["messages"]
    if any("set_chat_title" in str(message.get("content")) for message in second_request):
        print("ERROR: the title instruction was still present anywhere in the request after the conversation was titled: {}".format(second_request))
        failed = True

    return failed


def test_title_instruction_reaches_the_model_without_touching_the_stored_message(my_predbat):
    """The title reminder lands on the copy of the user message sent to the model, and nowhere
    else - not the system message, and not the message actually stored in the conversation.

    Drives build_messages() directly rather than through a full turn, so this pins the placement
    at the layer that decides it rather than depending on a particular model response. The
    stored-message half is the transcript-corruption risk the task called out explicitly: storing
    the instruction inside the user's own message would replay it back to the model as if the user
    had typed it, on every later turn, forever.
    """
    failed = False
    print("**** Testing the title instruction reaches only the outgoing user message ****")
    agent = _make_agent(my_predbat)
    cid = asyncio.run(agent.store.create())
    original_text = "why is the battery discharging right now?"
    asyncio.run(agent.store.append(cid, {"role": "user", "content": original_text}))

    history = asyncio.run(agent.store.get_messages(cid))
    messages = asyncio.run(agent.build_messages(cid, history))

    if TITLE_INSTRUCTION in _system_text(messages[0]):
        print("ERROR: the title instruction appeared in the system message: {!r}".format(messages[0]["content"]))
        failed = True
    if TITLE_INSTRUCTION not in messages[-1]["content"]:
        print("ERROR: the title instruction did not reach the outgoing user message: {!r}".format(messages[-1]["content"]))
        failed = True
    if not messages[-1]["content"].startswith(original_text):
        print("ERROR: the outgoing user message lost or reordered the user's own words: {!r}".format(messages[-1]["content"]))
        failed = True

    stored = asyncio.run(agent.store.get_messages(cid))
    if stored[-1]["content"] != original_text:
        print("ERROR: the stored user message was corrupted with the title instruction: {!r}".format(stored[-1]["content"]))
        failed = True
    if stored[-1] is messages[-1]:
        print("ERROR: build_messages() handed back the exact stored message object rather than a copy - mutating the outgoing payload would corrupt history")
        failed = True

    return failed


def test_system_prompt_is_frozen_byte_identical_across_turns_including_a_titling_turn(my_predbat):
    """The system prompt is captured once per conversation and replayed verbatim afterwards -
    character-for-character identical, not merely 'the same snapshot' - across a turn that titles
    the conversation, which is the second, easy-to-miss source of prefix instability alongside the
    live snapshot itself (see the module docstring). Asserting only that both prompts mention the
    same figures would pass even if the prompt were silently rebuilt every turn and the underlying
    state simply had not changed between two calls in the same test process; this closes that gap
    two ways at once - _TickingBase's now_utc genuinely differs on every read, and the number of
    reads is asserted to stay the same after the second turn as it was after the first, so a
    rebuild cannot hide behind a lucky coincidence.

    build_messages() sends the system message's content as a one-element array carrying a
    cache_control breakpoint, not a plain string (see chat.py's module docstring) - system_1 and
    system_2 are pulled out with _system_text() specifically so this still compares the prompt
    *text* byte-for-byte, rather than leaning on Python's structural list equality to do that
    inspection implicitly. test_system_message_carries_cache_control() covers the wrapper shape
    itself.

    Mutation-checked by hand: temporarily changing ChatAgent._frozen_system_prompt() to call
    build_system_prompt() unconditionally, rather than returning the stored value once one exists,
    turns this test red immediately (system_1 != system_2, and the read count climbs again on the
    second turn) - see the task report for the captured output.
    """
    failed = False
    print("**** Testing the system prompt is byte-identical across turns, including a titling turn ****")
    agent = _make_agent(my_predbat)
    agent.base = _TickingBase()
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.store.append(cid, {"role": "user", "content": "why is it charging?"}))
    messages_1 = asyncio.run(agent.build_messages(cid, asyncio.run(agent.store.get_messages(cid))))
    system_1 = _system_text(messages_1[0])
    reads_after_turn_1 = agent.base.reads
    if not reads_after_turn_1:
        print("ERROR: test setup did not consult the ticking clock at all while building the first prompt")
        return True

    agent.store.set_title(cid, "Why the battery is charging")
    asyncio.run(agent.store.append(cid, {"role": "assistant", "content": "because rates are low right now"}))
    asyncio.run(agent.store.append(cid, {"role": "user", "content": "and when does it stop?"}))
    messages_2 = asyncio.run(agent.build_messages(cid, asyncio.run(agent.store.get_messages(cid))))
    system_2 = _system_text(messages_2[0])

    if system_1 != system_2:
        print("ERROR: the system prompt is not byte-identical across turns - the request prefix is not cacheable")
        print("turn 1: {!r}".format(system_1))
        print("turn 2: {!r}".format(system_2))
        failed = True
    # Not merely "the two prompts matched" (which a rebuild could produce by coincidence if
    # nothing else in the fake base had changed) - the clock behind the snapshot must genuinely
    # not have been touched again on the second turn, which only holds if the stored prompt was
    # reused rather than rebuilt.
    if agent.base.reads != reads_after_turn_1:
        print("ERROR: now_utc was read again on the second turn ({} reads, was {} after turn 1) - the prompt was rebuilt instead of reused".format(agent.base.reads, reads_after_turn_1))
        failed = True

    return failed


def test_system_message_carries_cache_control(my_predbat):
    """The system message's content is array-form, one text block, carrying the ephemeral
    cache_control breakpoint - the shape a caching-capable provider (Anthropic, Qwen) needs in
    order to actually cache the frozen prefix; see the module docstring for why this is sent to
    every provider unconditionally rather than gated on model family.

    Deliberately pins every field rather than just checking content is *some* list: a dict shaped
    differently, or a second content block, would still let the substring/equality assertions
    elsewhere in this file (which go through _system_text()) keep passing while missing
    OpenRouter's documented breakpoint shape entirely.
    """
    failed = False
    print("**** Testing the system message carries a cache_control breakpoint ****")
    agent = _make_agent(my_predbat)
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.store.append(cid, {"role": "user", "content": "why is it charging?"}))
    messages = asyncio.run(agent.build_messages(cid, asyncio.run(agent.store.get_messages(cid))))

    system_message = messages[0]
    if system_message.get("role") != "system":
        print("ERROR: the first message is not the system message: {!r}".format(system_message))
        return True
    content = system_message.get("content")
    if not isinstance(content, list) or len(content) != 1:
        print("ERROR: the system message's content is not a one-element array: {!r}".format(content))
        return True

    block = content[0]
    if block.get("type") != "text":
        print("ERROR: the system message's content block has type {!r}, expected 'text'".format(block.get("type")))
        failed = True
    if block.get("cache_control") != SYSTEM_PROMPT_CACHE_CONTROL:
        print("ERROR: the system message's content block cache_control is {!r}, expected {!r}".format(block.get("cache_control"), SYSTEM_PROMPT_CACHE_CONTROL))
        failed = True
    if SYSTEM_PROMPT_CACHE_CONTROL != {"type": "ephemeral"}:
        print("ERROR: SYSTEM_PROMPT_CACHE_CONTROL is {!r}, expected the 'ephemeral' breakpoint OpenRouter and Anthropic define".format(SYSTEM_PROMPT_CACHE_CONTROL))
        failed = True
    if not isinstance(block.get("text"), str) or not block["text"]:
        print("ERROR: the system message's content block has no usable text: {!r}".format(block.get("text")))
        failed = True

    return failed


def test_frozen_system_prompt_is_built_once_and_reused_not_rebuilt(my_predbat):
    """A conversation with no stored system prompt - every conversation created before this
    feature existed, and any brand new one before its first turn runs - gets one built and stored
    on the next call, and every call after that reuses the stored value rather than rebuilding it.

    There is deliberately no separate migration code path (see _frozen_system_prompt()'s
    docstring): 'no prompt yet' and 'first turn of a new conversation' are the same case, and a
    freshly created conversation already exercises it. Reuse is proven the same way the byte-
    identical test proves it - by swapping in a base whose clock would visibly change the output
    if it were ever consulted again, and asserting it never is.
    """
    failed = False
    print("**** Testing a conversation with no stored system prompt builds one, then reuses it ****")
    agent = _make_agent(my_predbat)
    cid = asyncio.run(agent.store.create())

    before = asyncio.run(agent.store.get_system_prompt(cid))
    if before != (None, None):
        print("ERROR: a freshly created conversation already has a stored system prompt: {}".format(before))
        failed = True

    first = asyncio.run(agent._frozen_system_prompt(cid))
    stored_prompt, stored_at = asyncio.run(agent.store.get_system_prompt(cid))
    if stored_prompt != first or not stored_at:
        print("ERROR: the built prompt was not stored: prompt matches stored={}, stored_at={!r}".format(stored_prompt == first, stored_at))
        failed = True

    agent.base = _TickingBase()
    second = asyncio.run(agent._frozen_system_prompt(cid))
    if second != first:
        print("ERROR: the second call rebuilt the prompt instead of reusing the stored one: {!r} != {!r}".format(second, first))
        failed = True
    if agent.base.reads:
        print("ERROR: the ticking base's clock was read even though the prompt should have come from storage, not been rebuilt: {} reads".format(agent.base.reads))
        failed = True

    return failed


def test_usage_event_and_totals_report_cached_tokens(my_predbat):
    """OpenRouter's usage.prompt_tokens_details.cached_tokens is surfaced on the 'usage' event for
    the turn that produced it, and accumulated onto the conversation's usage_total across turns -
    so a caching hit is something a user can actually observe, not something only assumed to be
    working once the prefix is stable.
    """
    failed = False
    print("**** Testing cached_tokens is captured on the usage event and the running total ****")
    usage_1 = {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.001, "prompt_tokens_details": {"cached_tokens": 80}}
    usage_2 = {"prompt_tokens": 105, "completion_tokens": 12, "cost": 0.0011, "prompt_tokens_details": {"cached_tokens": 90}}
    agent = _agent_with_fake(my_predbat, _text_response("first", usage=usage_1), _text_response("second", usage=usage_2))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "one"))
    usage_events = [event for event in agent.events_since(0, cid)[0] if event["type"] == "usage"]
    if not usage_events or usage_events[0]["data"].get("cached_tokens") != 80:
        print("ERROR: the usage event did not report cached_tokens=80: {}".format(usage_events))
        return True
    if usage_events[0]["data"].get("conversation_cached_tokens") != 80:
        print("ERROR: the usage event's running total did not include this turn's cached tokens: {}".format(usage_events[0]["data"]))
        failed = True
    meta = agent.store.get_meta(cid)
    if meta["usage_total"].get("cached_tokens") != 80:
        print("ERROR: the conversation's usage_total did not accumulate cached_tokens: {}".format(meta["usage_total"]))
        failed = True

    asyncio.run(agent.run_turn(cid, "two"))
    meta_after = agent.store.get_meta(cid)
    if meta_after["usage_total"].get("cached_tokens") != 170:
        print("ERROR: cached_tokens did not accumulate across turns: expected 170, got {}".format(meta_after["usage_total"].get("cached_tokens")))
        failed = True

    return failed


def test_last_prompt_tokens_reflects_the_most_recent_turn_not_the_cumulative_total(my_predbat):
    """The Chat tab's context-size footer shows the LAST turn's prompt_tokens, not the cumulative
    usage_total.prompt_tokens - see ConversationStore.add_usage(). Two turns are driven with
    deliberately different prompt sizes (1000, then 4000) so a test - or an implementation - that
    read the cumulative total (5000) instead of the most recent value (4000) fails rather than
    passing by coincidence, exactly the trap the task brief warns about.
    """
    failed = False
    print("**** Testing last_prompt_tokens tracks the most recent turn, not the cumulative total ****")
    usage_1 = {"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.01}
    usage_2 = {"prompt_tokens": 4000, "completion_tokens": 80, "cost": 0.02}
    agent = _agent_with_fake(my_predbat, _text_response("first", usage=usage_1), _text_response("second", usage=usage_2))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "one"))
    meta_after_first = agent.store.get_meta(cid)
    if meta_after_first.get("last_prompt_tokens") != 1000:
        print("ERROR: last_prompt_tokens is {} after the first turn, expected 1000".format(meta_after_first.get("last_prompt_tokens")))
        failed = True

    asyncio.run(agent.run_turn(cid, "two"))
    meta_after_second = agent.store.get_meta(cid)
    if meta_after_second.get("last_prompt_tokens") != 4000:
        print("ERROR: last_prompt_tokens is {} after the second turn, expected 4000 (the most recent turn's prompt size, not the 5000 cumulative total)".format(meta_after_second.get("last_prompt_tokens")))
        failed = True
    if meta_after_second["usage_total"]["prompt_tokens"] != 5000:
        print("ERROR: usage_total.prompt_tokens is {}, expected the cumulative 5000 - the cumulative total must still work alongside last_prompt_tokens".format(meta_after_second["usage_total"]["prompt_tokens"]))
        failed = True

    return failed


def test_request_timeout_and_turn_timeout_are_not_confused(my_predbat):
    """chat_request_timeout bounds one completion's aiohttp.ClientTimeout; chat_turn_timeout bounds
    self.deadline, the whole turn's budget across every round trip - see initialize()'s docstring
    for why sharing one value between the two, the previous behaviour, capped a multi-round turn on
    a budget meant for a single request. The fixture uses two very different numbers (45 vs 999)
    so a test - or an implementation - that read one where the other belongs would fail rather than
    passing by coincidence.
    """
    failed = False
    print("**** Testing request_timeout and turn_timeout are not confused ****")
    agent = _make_agent(my_predbat, request_timeout=45, turn_timeout=999)

    class _StoppedAfterCapture(Exception):
        """Raised the moment the ClientTimeout's `total` is captured, to skip the real network I/O."""

    captured = {}

    def fake_client_timeout(total=None, **kwargs):
        """Record the total passed to aiohttp.ClientTimeout, then abort before any real request."""
        captured["total"] = total
        raise _StoppedAfterCapture()

    original_client_timeout = chat.aiohttp.ClientTimeout
    chat.aiohttp.ClientTimeout = fake_client_timeout
    try:
        chunk_generator = agent._stream_chunks({"model": "test/model"})
        try:
            asyncio.run(chunk_generator.__anext__())
            print("ERROR: _stream_chunks did not reach aiohttp.ClientTimeout at all")
            failed = True
        except _StoppedAfterCapture:
            pass
    finally:
        chat.aiohttp.ClientTimeout = original_client_timeout

    if captured.get("total") != 45:
        print("ERROR: _stream_chunks built its ClientTimeout with total={}, expected chat_request_timeout's 45 - it must not read turn_timeout here".format(captured.get("total")))
        failed = True

    before = time.monotonic()
    agent2 = _agent_with_fake(my_predbat, _text_response("hello"), request_timeout=45, turn_timeout=999)
    cid = asyncio.run(agent2.store.create())
    asyncio.run(agent2.run_turn(cid, "hi"))
    expected_deadline = before + 999
    if not (expected_deadline - 2 <= agent2.deadline <= expected_deadline + 2):
        print("ERROR: agent.deadline is {:.1f}, expected close to {:.1f} (started + turn_timeout=999) - self.deadline must be built from turn_timeout, not request_timeout=45".format(agent2.deadline, expected_deadline))
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


def test_set_apps_config_confirmation_gate_and_card(my_predbat):
    """set_apps_config hits the same write-confirmation gate set_config does, with a card that
    names the key, its current value, the proposed value and the restart warning - and declining
    stops the tool from running at all.

    tool_defs_by_name is built from TOOL_DEFS + CHAT_TOOL_DEFS (see ChatAgent.__init__), so
    set_apps_config's 'writes': True (chat_tools.py) should reach _run_one_tool's confirmation
    gate the same way set_config's does - this drives that path directly rather than assuming the
    merge works. The card content matters because the model's raw {'key', 'value'} arguments say
    nothing about what the key is currently set to; _confirmation_card_arguments() (chat.py) looks
    the current value up fresh from self.base.args rather than trusting anything the model said,
    so this also proves that lookup is wired in. "Did not execute" is checked the same way
    test_write_confirmation_rejected checks it for set_config: by the absence of any tool_start
    event, not merely by the declined error text.
    """
    failed = False
    print("**** Testing set_apps_config hits the write-confirmation gate with an informative card ****")
    original_value = my_predbat.args.get("ha_url")
    my_predbat.args["ha_url"] = "http://old-value.local:8123"
    try:
        agent = _agent_with_fake(my_predbat, _tool_call_response("set_apps_config", {"key": "ha_url", "value": "http://new-value.local:8123"}, call_id="call_apps"), _text_response("understood"))
        agent.confirm_writes_enabled = lambda: True
        cid = asyncio.run(agent.store.create())

        _confirm_soon(agent, False)
        asyncio.run(agent.run_turn(cid, "change the HA url"))

        events, _, _ = agent.events_since(0, cid)
        kinds = [event["type"] for event in events]
        if "confirm" not in kinds:
            print("ERROR: set_apps_config did not trigger a confirmation - the writes gate did not fire for it")
            return True
        if "tool_start" in kinds:
            print("ERROR: a declined set_apps_config call still ran the tool")
            failed = True

        card = next(event["data"]["arguments"] for event in events if event["type"] == "confirm")
        if card.get("key") != "ha_url":
            print("ERROR: the confirmation card did not name the key: {}".format(card))
            failed = True
        if card.get("current_value") != "http://old-value.local:8123":
            print("ERROR: the confirmation card did not show the current value: {}".format(card))
            failed = True
        if card.get("proposed_value") != "http://new-value.local:8123":
            print("ERROR: the confirmation card did not show the proposed value: {}".format(card))
            failed = True
        if not card.get("warning") or card["warning"] != APPS_YAML_RESTART_WARNING:
            print("ERROR: the confirmation card did not carry the restart warning: {}".format(card))
            failed = True

        results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
        if not results or "declined" not in str(results[0].get("content")).lower():
            print("ERROR: the declined set_apps_config call did not come back as a declined tool result: {}".format(results))
            failed = True
    finally:
        my_predbat.args["ha_url"] = original_value

    return failed


def test_set_apps_config_approved_writes_apps_yaml(my_predbat):
    """An approved set_apps_config call actually writes apps.yaml and mirrors into base.args - the
    end-to-end proof that chat.py's _dispatch wiring for 'set_apps_config' calls the real
    chat_tools.set_apps_config rather than something that only looks right.

    Runs inside a temporary directory, the same way test_web_if.py does for the same reason:
    set_apps_config's path defaults are relative to the working directory, matching every other
    apps.yaml access in web.py, so this must never run against the real repository's apps.yaml.
    """
    failed = False
    print("**** Testing an approved set_apps_config call writes apps.yaml ****")
    original_dir = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix="predbat_test_chat_apps_")
    original_args_value = my_predbat.args.get("num_inverters")
    try:
        with open(os.path.join(temp_dir, "apps.yaml"), "w", encoding="utf-8") as handle:
            handle.write("pred_bat:\n  num_inverters: 1\n")
        os.chdir(temp_dir)
        my_predbat.args["num_inverters"] = 1

        agent = _agent_with_fake(my_predbat, _tool_call_response("set_apps_config", {"key": "num_inverters", "value": 2}, call_id="call_apps_ok"), _text_response("done"))
        agent.confirm_writes_enabled = lambda: True
        cid = asyncio.run(agent.store.create())

        _confirm_soon(agent, True)
        asyncio.run(agent.run_turn(cid, "change num_inverters"))

        with open(os.path.join(temp_dir, "apps.yaml"), "r", encoding="utf-8") as handle:
            written = handle.read()
        if "num_inverters: 2" not in written:
            print("ERROR: apps.yaml was not updated by the approved call: {!r}".format(written))
            failed = True
        if not os.path.exists(os.path.join(temp_dir, "apps.yaml.backup")):
            print("ERROR: no backup was created by the approved call")
            failed = True
        if my_predbat.args.get("num_inverters") != 2:
            print("ERROR: base.args was not updated by the approved call")
            failed = True

        results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
        if not results or not json.loads(results[0]["content"]).get("success"):
            print("ERROR: the tool result did not report success: {}".format(results))
            failed = True
    finally:
        os.chdir(original_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)
        my_predbat.args["num_inverters"] = original_args_value
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


def test_rate_limited_retry_is_patient(my_predbat):
    """A 429 gets its own, much longer budget than a provider error.

    A 429 is not a fault - the request was refused because a quota window is full, and only time
    fixes it. Sharing the three-attempt provider-error budget meant a user saw "Rate limited by
    OpenRouter, gave up after 3 attempts" within about six seconds and had to retype the question,
    which is exactly what free-tier models do constantly.

    The schedule starts short in case the window has already rolled, then settles: a window that
    has not cleared in ten seconds will not clear in eleven, and polling harder only spends the
    quota being waited for. The final delay repeats for every attempt past the list, which is what
    lets 20 attempts run on a four-entry schedule.

    Mutation checks: sharing the provider-error cap, or indexing the schedule without the repeat,
    each fails below.
    """
    failed = False
    print("**** Testing a 429 retries patiently ****")

    if COMPLETION_RATE_LIMIT_MAX_ATTEMPTS <= COMPLETION_MAX_ATTEMPTS:
        print("ERROR: a rate limit gets no more attempts than a provider error ({} vs {})".format(COMPLETION_RATE_LIMIT_MAX_ATTEMPTS, COMPLETION_MAX_ATTEMPTS))
        failed = True

    # The schedule itself, including the repeat past its end.
    expected = [1, 5, 10, 30, 30, 30]
    actual = [retry_delay_for(attempt, True) for attempt in range(1, 7)]
    if actual != expected:
        print("ERROR: the rate-limit backoff schedule is {}, expected {}".format(actual, expected))
        failed = True
    # A provider error keeps its own, shorter schedule.
    if retry_delay_for(1, False) != COMPLETION_RETRY_DELAYS_SECONDS[0]:
        print("ERROR: a provider error no longer uses its own backoff")
        failed = True
    if max_attempts_for(True) != COMPLETION_RATE_LIMIT_MAX_ATTEMPTS or max_attempts_for(False) != COMPLETION_MAX_ATTEMPTS:
        print("ERROR: max_attempts_for does not distinguish the two kinds of failure")
        failed = True

    # And end to end: a 429 that clears on the second attempt waits the first scheduled delay.
    agent = _agent_with_fake(my_predbat, ChatRequestError(429, "slow down"), _text_response("ok now"))
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "hello"))

    if agent.retry_sleeps != [COMPLETION_RATE_LIMIT_DELAYS_SECONDS[0]]:
        print("ERROR: expected the first rate-limit backoff {}, agent requested {}".format(COMPLETION_RATE_LIMIT_DELAYS_SECONDS[0], agent.retry_sleeps))
        failed = True

    events = agent.events_since(0, cid)[0]
    retries = [event for event in events if event["type"] == "retry"]
    if not retries or retries[0]["data"].get("delay") != COMPLETION_RATE_LIMIT_DELAYS_SECONDS[0]:
        print("ERROR: the retry event does not carry the rate-limit delay: {}".format(retries))
        failed = True
    # The count shown to the user must be the rate-limit budget, not the provider-error one.
    if retries and retries[0]["data"].get("of") != COMPLETION_RATE_LIMIT_MAX_ATTEMPTS:
        print("ERROR: the retry event reports {} attempts, expected the rate-limit budget {}".format(retries[0]["data"].get("of"), COMPLETION_RATE_LIMIT_MAX_ATTEMPTS))
        failed = True

    # A run of 429s must exhaust the longer budget, not the short one.
    failures = [ChatRequestError(429, "slow down") for _ in range(COMPLETION_RATE_LIMIT_MAX_ATTEMPTS)]
    # A realistic turn budget: the deadline guard correctly refuses a 30s backoff that would
    # not fit, so a short-budget fixture would stop after three and prove nothing about the cap.
    agent = _agent_with_fake(my_predbat, *failures, turn_timeout=3600)
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "hello"))
    if len(agent.retry_sleeps) != COMPLETION_RATE_LIMIT_MAX_ATTEMPTS - 1:
        print("ERROR: a run of 429s backed off {} times, expected {}".format(len(agent.retry_sleeps), COMPLETION_RATE_LIMIT_MAX_ATTEMPTS - 1))
        failed = True

    return failed


def test_retry_after_header_is_honoured(my_predbat):
    """A Retry-After from the provider overrides the schedule, within a ceiling.

    OpenRouter sends Retry-After with a 429 saying how long the quota window has left. That is the
    one number that actually knows when the limit clears, so it wins over any fixed schedule - but
    it is bounded, because a header asking for ten minutes would park the turn until its own
    deadline killed it with nothing to show for the wait.

    Only the numeric form is honoured. The header may also carry an HTTP date, which needs the
    server's clock to agree with ours; guessing wrong there means either hammering a limit that
    has not cleared or idling long past one that has.

    Mutation checks: ignoring retry_after, or dropping the ceiling, each fails below.
    """
    failed = False
    print("**** Testing the Retry-After header is honoured ****")

    for header, expected in (("7", 7), ("0", 0), (None, None), ("", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None), ("-3", None), ("2.5", None)):
        if parse_retry_after(header) != expected:
            print("ERROR: parse_retry_after({!r}) returned {!r}, expected {!r}".format(header, parse_retry_after(header), expected))
            failed = True

    # A header longer than the first scheduled delay is used instead of it.
    error = ChatRequestError(429, "slow down", retry_after=12)
    agent = _agent_with_fake(my_predbat, error, _text_response("ok now"), turn_timeout=3600)
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "hello"))
    if agent.retry_sleeps != [12]:
        print("ERROR: the Retry-After header was not honoured, agent slept {}".format(agent.retry_sleeps))
        failed = True

    # And one beyond the ceiling is capped rather than obeyed.
    error = ChatRequestError(429, "slow down", retry_after=RETRY_AFTER_MAX_SECONDS + 600)
    agent = _agent_with_fake(my_predbat, error, _text_response("ok now"), turn_timeout=3600)
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "hello"))
    if agent.retry_sleeps != [RETRY_AFTER_MAX_SECONDS]:
        print("ERROR: an over-long Retry-After was not capped, agent slept {}".format(agent.retry_sleeps))
        failed = True

    # A header shorter than the schedule does not make the backoff more aggressive than intended.
    error = ChatRequestError(429, "slow down", retry_after=0)
    agent = _agent_with_fake(my_predbat, error, _text_response("ok now"), turn_timeout=3600)
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "hello"))
    if agent.retry_sleeps != [COMPLETION_RATE_LIMIT_DELAYS_SECONDS[0]]:
        print("ERROR: a zero Retry-After undercut the schedule, agent slept {}".format(agent.retry_sleeps))
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


def test_model_resolution_order(my_predbat):
    """A turn picks the conversation's model, then the remembered pick, then the apps.yaml default.

    With openrouter_default_model optional, all three can be empty - a fresh install where the
    user has pasted only an API key. That is not a misconfiguration, so the turn must say what to
    do rather than posting a request with model=None and surfacing whatever the API says about it.

    Mutation check: deleting the "if not model" guard in _turn_loop makes the no-model case send
    a request instead of emitting the message, failing the last block below.
    """
    failed = False
    print("**** Testing model resolution order ****")

    agent = _agent_with_fake(my_predbat, _text_response("hello"), model=None)
    cid = asyncio.run(agent.store.create())

    # Nothing anywhere: refuse, and name the picker.
    asyncio.run(agent.run_turn(cid, "hello"))
    events = agent.events_since(0, cid)[0]
    errors = [event for event in events if event["type"] == "error"]
    if not errors or "model" not in str(errors[0]["data"].get("message", "")).lower():
        print("ERROR: a turn with no model available did not report it: {}".format(errors))
        failed = True
    if [event for event in events if event["type"] == "assistant"]:
        print("ERROR: a turn with no model still produced an assistant message")
        failed = True

    # The remembered pick is used when the conversation has none of its own.
    agent.store.set_selected_model("remembered/model")
    if agent.resolve_model(cid) != "remembered/model":
        print("ERROR: the remembered model was not used, got {}".format(agent.resolve_model(cid)))
        failed = True

    # The apps.yaml default is the last resort, below the remembered pick.
    agent.default_model = "apps/default"
    if agent.resolve_model(cid) != "remembered/model":
        print("ERROR: the apps.yaml default outranked the user's remembered pick")
        failed = True
    agent.store.set_selected_model(None)
    if agent.resolve_model(cid) != "apps/default":
        print("ERROR: the apps.yaml default was not used once nothing was remembered")
        failed = True

    # The conversation's own model outranks everything.
    agent.store.set_model(cid, "conversation/model")
    agent.store.set_selected_model("remembered/model")
    if agent.resolve_model(cid) != "conversation/model":
        print("ERROR: the conversation's own model did not win, got {}".format(agent.resolve_model(cid)))
        failed = True

    return failed


def test_snapshot_formats_times_and_percentages(my_predbat):
    """The snapshot renders window minutes as weekday + clock, and kWh figures with a percentage.

    Raw minutes-since-midnight are the worst of the lot: a charge window at "start: 1590" is
    26.5 hours after midnight - half past two TOMORROW - and neither a reader nor a model reliably
    works that out. A model that does not is liable to tell a user their battery charges at 15:90.
    The weekday is what disambiguates it, which is also why the captured timestamp carries one.

    Mutation checks: reverting any of format_window, format_clock or format_percent_of to the raw
    value fails one of the assertions below.
    """
    failed = False
    print("**** Testing snapshot time and percentage formatting ****")

    tz = timezone(timedelta(hours=1))

    class Base:
        """A base carrying exactly the state the snapshot formats."""

        now_utc = datetime(2026, 8, 28, 17, 0, 0, tzinfo=tz)
        midnight_utc = datetime(2026, 8, 28, 0, 0, 0, tzinfo=tz)
        soc_kw = 6.76
        soc_max = 9.52
        reserve = 0.381
        minutes_now = 1020
        rate_import = {1020: 30.26}
        rate_export = {1020: 12.0}
        charge_window_best = [{"start": 1590, "end": 1860, "average": 6.9, "target": 9.52}]
        export_window_best = [{"start": 1255, "end": 1320, "average": 12.0, "set": 11.0, "start_orig": 1230, "target": 4}]

        def get_arg(self, name, default=None, **kwargs):
            """Return the default for every argument."""
            return default

    snapshot = build_snapshot(Base())

    # The captured timestamp carries a short weekday.
    if "Fri 2026-08-28" not in snapshot:
        print("ERROR: the captured timestamp has no short weekday: {}".format(snapshot))
        failed = True

    # 1590 minutes is 02:30 the NEXT day - the weekday is the whole point.
    if "Sat 02:30 to Sat 07:00" not in snapshot:
        print("ERROR: the charge window was not rendered as weekday + clock: {}".format(snapshot))
        failed = True
    if "1590" in snapshot or "1860" in snapshot:
        print("ERROR: raw minute counts are still in the snapshot: {}".format(snapshot))
        failed = True

    # An export window inside today, with its extra minute-valued key also converted.
    if "Fri 20:55 to Fri 22:00" not in snapshot:
        print("ERROR: the export window was not rendered as weekday + clock: {}".format(snapshot))
        failed = True
    if "start_orig Fri 20:30" not in snapshot:
        print("ERROR: start_orig was left as raw minutes: {}".format(snapshot))
        failed = True

    # Non-time keys must survive untouched - they are what the model actually reasons about.
    for kept in ("average 6.9", "target 9.52", "set 11.0"):
        if kept not in snapshot:
            print("ERROR: window field {!r} was dropped: {}".format(kept, snapshot))
            failed = True

    # kWh alone does not say whether the battery is nearly full.
    if "6.76 kWh of 9.52 kWh (71%)" not in snapshot:
        print("ERROR: SOC has no percentage: {}".format(snapshot))
        failed = True
    if "reserve 0.381 kWh (4%)" not in snapshot:
        print("ERROR: reserve has no unit and percentage: {}".format(snapshot))
        failed = True

    # Version and inverter type both used to read attributes that do not exist, so every real
    # install reported "unknown" for both. The version is a module constant in predbat.py, and the
    # type lives on each built Inverter (inverter_type is a per-inverter string_list in
    # APPS_SCHEMA, so an unindexed get_arg never resolves it).
    class WithInverters(Base):
        """A base with inverters built, as a running Predbat has."""

        inverters = [type("FakeInverter", (), {"inverter_type": "GE"})(), type("FakeInverter", (), {"inverter_type": "SOLIS"})()]
        num_inverters = 2

    detailed = build_snapshot(WithInverters())
    if "Predbat version: unknown" in detailed:
        print("ERROR: the snapshot still reports an unknown version: {}".format(detailed))
        failed = True
    # A mixed install must name both, not pick one and imply the other does not exist.
    if "GE, SOLIS" not in detailed:
        print("ERROR: the snapshot did not name both inverter types: {}".format(detailed))
        failed = True

    # With no inverters built yet, fall back to what apps.yaml asked for rather than "unknown".
    class Configured(Base):
        """A base before the inverters are constructed."""

        def get_arg(self, name, default=None, **kwargs):
            """Report the apps.yaml inverter_type list."""
            return ["GE"] if name == "inverter_type" else default

    if "of type GE" not in build_snapshot(Configured()):
        print("ERROR: the snapshot did not fall back to the configured inverter type: {}".format(build_snapshot(Configured())))
        failed = True

    # A base with no midnight (half-started) must degrade rather than raise.
    class NoMidnight(Base):
        """Predbat before midnight_utc has been worked out."""

        midnight_utc = None

    try:
        degraded = build_snapshot(NoMidnight())
    except Exception as error:
        print("ERROR: build_snapshot raised with no midnight: {}".format(error))
        return True
    if "1590" not in degraded:
        print("ERROR: with no midnight the window should fall back to raw minutes: {}".format(degraded))
        failed = True

    # A zero capacity must not produce a division error or a nonsense percentage.
    class ZeroMax(Base):
        """An inverter reporting no capacity at all."""

        soc_max = 0

    try:
        zeroed = build_snapshot(ZeroMax())
    except Exception as error:
        print("ERROR: build_snapshot raised with soc_max of 0: {}".format(error))
        return True
    if "%" in zeroed.split("Inverters")[0].split("SOC")[-1]:
        print("ERROR: a zero capacity still produced a percentage: {}".format(zeroed))
        failed = True

    return failed


def test_turn_error_is_detailed_and_stored_but_never_replayed(my_predbat):
    """A failed turn shows its provider detail, is saved, and never goes back to the model.

    OpenRouter's generic wrapper - "Provider returned error" - says nothing a user can act on.
    The cause is in the error object's metadata: which provider failed and its raw response. That
    is surfaced in the transcript and recorded on the conversation.

    Recorded BESIDE the messages, never among them. A transport failure is not something the
    model said; replaying it would waste context and invite the model to treat a provider outage
    as part of the conversation. This asserts the negative directly rather than trusting the
    design, because that is the half that would fail silently.

    Mutation checks: dropping detail() from the emit, or storing the error as a message, each
    fails an assertion below.
    """
    failed = False
    print("**** Testing turn error detail, storage and non-replay ****")

    # An error chunk shaped the way OpenRouter sends one, with the real cause in metadata.
    error_chunk = [
        {
            "error": {
                "code": 502,
                "message": "Provider returned error",
                "metadata": {"provider_name": "Nvidia", "raw": "upstream connect error: overloaded"},
            }
        }
    ]
    # A 502 is retryable, so the fake needs a response for every attempt - otherwise the turn
    # fails for running out of canned replies rather than for the error under test.
    agent = _agent_with_fake(my_predbat, error_chunk, error_chunk, error_chunk)
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "is it sunny"))

    events = agent.events_since(0, cid)[0]
    errors = [event for event in events if event["type"] == "error"]
    if not errors:
        print("ERROR: a provider error produced no error event")
        return True

    data = errors[0]["data"]
    detail = str(data.get("detail") or "")
    # The generic message alone is what the user complained about - the detail must carry more.
    if "Nvidia" not in detail or "overloaded" not in detail:
        print("ERROR: the error detail does not name the provider or its raw response: {}".format(data))
        failed = True
    if "502" not in detail:
        print("ERROR: the error detail does not carry the status code: {}".format(data))
        failed = True

    # Stored against the conversation.
    stored = agent.store.get_last_error(cid)
    if not stored or "overloaded" not in str(stored.get("detail")):
        print("ERROR: the failure was not recorded on the conversation: {}".format(stored))
        failed = True
    if not stored.get("at"):
        print("ERROR: the recorded failure has no timestamp: {}".format(stored))
        failed = True

    # And - the point - it is not in the conversation the model would be sent.
    messages = asyncio.run(agent.store.get_messages(cid))
    for message in messages:
        blob = json.dumps(message)
        if "overloaded" in blob or "Provider returned error" in blob:
            print("ERROR: the failure was stored as a message and would be replayed to the model: {}".format(message))
            failed = True
    roles = [message.get("role") for message in messages]
    if roles != ["user"]:
        print("ERROR: expected only the user's message to be stored, got {}".format(roles))
        failed = True

    return failed


def test_confirmation_card_resolves_nested_paths(my_predbat):
    """The approval card shows the real current value for a nested path, and masks credentials.

    The card looked the current value up with a flat args.get(), which only understands top-level
    names. Every nested path set_apps_config accepts - "car_charging_exclusive[0]",
    "forecast_solar[0].azimuth" - therefore showed "current_value": null, telling the user the
    setting did not exist when it did, on the one screen whose whole purpose is to show what is
    about to change.

    The card is also built before set_apps_config runs, so a credential key's value would appear
    in the transcript on its way to being refused.

    Mutation checks: reverting to args.get(key), or dropping the mask, each fails below.
    """
    failed = False
    print("**** Testing the set_apps_config confirmation card ****")

    original_args = my_predbat.args
    try:
        my_predbat.args = {
            "car_charging_exclusive": [True, False],
            "forecast_solar": [{"azimuth": 180, "api_key": "REAL-KEY"}],
            "num_inverters": 1,
            "ha_key": "REAL-HA-TOKEN",
        }
        agent = _agent_with_fake(my_predbat, _text_response("ok"))

        def card(key, value):
            """Build the confirmation card for one proposed change."""
            return agent._confirmation_card_arguments("set_apps_config", {"key": key, "value": value})

        if card("car_charging_exclusive[0]", False).get("current_value") is not True:
            print("ERROR: an indexed path showed the wrong current value: {}".format(card("car_charging_exclusive[0]", False)))
            failed = True
        if card("forecast_solar[0].azimuth", 90).get("current_value") != 180:
            print("ERROR: a dotted path showed the wrong current value: {}".format(card("forecast_solar[0].azimuth", 90)))
            failed = True
        if card("num_inverters", 2).get("current_value") != 1:
            print("ERROR: a plain top-level key regressed: {}".format(card("num_inverters", 2)))
            failed = True

        # A path that genuinely does not exist still shows null rather than raising. Named
        # without a credential substring on purpose - "no_such_key" would match is_secret_key and
        # come back masked, which is correct behaviour but not what this line is testing.
        if card("absent_setting[4].nope", 1).get("current_value") is not None:
            print("ERROR: a nonexistent path did not resolve to null: {}".format(card("absent_setting[4].nope", 1)))
            failed = True

        # Credentials never reach the transcript, at either level.
        for key in ("ha_key", "forecast_solar[0].api_key"):
            shown = json.dumps(card(key, "new"))
            if "REAL-KEY" in shown or "REAL-HA-TOKEN" in shown:
                print("ERROR: the card exposed a credential for {}: {}".format(key, shown))
                failed = True

        # The restart warning must survive all of this - it is why the card exists.
        if not card("num_inverters", 2).get("warning"):
            print("ERROR: the card lost its restart warning")
            failed = True
    finally:
        my_predbat.args = original_args
    return failed


def test_stop_reaches_a_turn_parked_on_a_confirmation(my_predbat):
    """Stop ends a turn waiting for an approval, instead of hanging until the confirm timeout.

    await_confirmation() deliberately ignores self.deadline - the parked time is added back so a
    slow human cannot time their own approval out - but that also made Stop inert for up to
    CONFIRM_TIMEOUT_SECONDS (300s). The observed symptom was a turn stuck on "thinking" with the
    counter climbing, a Stop button that did nothing, and no way to end it but to wait.

    An explicit stop is not a deadline, so it is tracked separately and checked here.

    Mutation checks: removing the stop_requested check in await_confirmation, or the assignment
    in the cancel route, leaves this hanging for the full timeout.
    """
    failed = False
    print("**** Testing Stop reaches a turn parked on a confirmation ****")

    agent = _agent_with_fake(my_predbat, _text_response("done"))
    with agent.lock:
        agent.pending_confirm["call_x"] = {"conversation_id": "c1", "turn_id": "t1", "approved": None, "card": {}}

    async def drive():
        """Park on the confirmation, then press Stop from another task."""

        async def press_stop():
            """Stand in for POST /chat/cancel, which sets both of these."""
            await asyncio.sleep(0)
            agent.deadline = 0
            agent.stop_requested = "t1"

        asyncio.ensure_future(press_stop())
        started = time.monotonic()
        approved = await agent.await_confirmation("call_x")
        return approved, time.monotonic() - started

    approved, elapsed = asyncio.run(drive())

    if approved:
        print("ERROR: a stopped confirmation was treated as approved")
        failed = True
    # The point: it returns promptly rather than sitting out the confirm timeout.
    if elapsed > 5:
        print("ERROR: Stop did not reach the parked turn - it waited {:.1f}s".format(elapsed))
        failed = True

    # A stop aimed at a finished turn must not kill the next one.
    agent.stop_requested = "t1"
    cid = asyncio.run(agent.store.create())
    agent.claim_turn(cid)
    if agent.stop_requested is not None:
        print("ERROR: a stale stop survived into the next turn: {}".format(agent.stop_requested))
        failed = True

    return failed


def test_approvals_are_recorded_but_never_replayed(my_predbat):
    """An approval is persisted as a decision record, and never reaches the model.

    Previously the card existed only as an SSE event, so a reconnect or a restart lost it while
    the turn went on waiting for an answer to a question no longer on screen. It is now stored
    beside the messages: a pending record is what a reconnecting client needs to get its card
    back, and a settled one is the audit trail for anything the agent changed.

    Beside the messages, never among them. The model learns the outcome from the tool result it
    gets back; putting the approval in the replayed history would both duplicate that and let a
    record of a human decision be re-read as conversation. This asserts the negative, because that
    is the half that fails silently.

    Mutation checks: dropping add_approval, resolve_approval, or the relabelling of a pending
    record on load, each fails an assertion below.
    """
    failed = False
    print("**** Testing approvals are recorded and never replayed ****")

    storage = FakeStorage()
    store = ConversationStore(storage, my_predbat.log)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "change a setting"}))

    card = {"call_id": "call_1", "name": "set_apps_config", "arguments": {"key": "num_inverters", "current_value": 1, "proposed_value": 2}}
    store.add_approval(cid, card)

    pending = store.get_approvals(cid)
    if len(pending) != 1 or pending[0].get("status") != "pending":
        print("ERROR: the approval was not recorded as pending: {}".format(pending))
        failed = True
    if not pending[0].get("asked_at"):
        print("ERROR: the approval record has no timestamp: {}".format(pending))
        failed = True

    store.resolve_approval(cid, "call_1", "approved")
    resolved = store.get_approvals(cid)
    if resolved[0].get("status") != "approved" or not resolved[0].get("answered_at"):
        print("ERROR: the answer was not recorded: {}".format(resolved))
        failed = True

    # Never in the conversation the model is sent.
    messages = asyncio.run(store.get_messages(cid))
    for message in messages:
        if "proposed_value" in json.dumps(message):
            print("ERROR: the approval leaked into the replayed messages: {}".format(message))
            failed = True

    # It survives a restart, and a still-pending one is relabelled rather than offered as a live
    # button - the turn that was waiting for it did not survive.
    store.add_approval(cid, {"call_id": "call_2", "name": "set_config", "arguments": {}})
    asyncio.run(store.flush(cid))

    reloaded = ConversationStore(storage, my_predbat.log)
    asyncio.run(reloaded.load_index())
    asyncio.run(reloaded.get_messages(cid))
    after = reloaded.get_approvals(cid)
    if len(after) != 2:
        print("ERROR: approvals did not survive the reload: {}".format(after))
        failed = True
    by_id = {entry.get("call_id"): entry for entry in after}
    if by_id.get("call_1", {}).get("status") != "approved":
        print("ERROR: a settled approval changed on reload: {}".format(after))
        failed = True
    if by_id.get("call_2", {}).get("status") != "unanswered":
        print("ERROR: a pending approval was not relabelled after a restart: {}".format(after))
        failed = True

    # A copy, so a caller serialising it cannot mutate the stored record.
    after[0]["status"] = "mutated"
    if reloaded.get_approvals(cid)[0].get("status") == "mutated":
        print("ERROR: get_approvals handed out the live record rather than a copy")
        failed = True

    return failed


def test_provider_detection_and_payload(my_predbat):
    """chat_api_type: auto works out the provider from the URL, and the payload follows it.

    Two things are being decided, and they are not the same question: whether a key is required
    (a local endpoint needs none) and whether this is OpenRouter (which decides pricing, the web
    plugin and how token usage is requested). A LAN Ollama is remote but still keyless, so URL
    locality and provider identity are resolved separately.

    Decided from the URL rather than by probing: this runs during component start-up, and a
    start-up that makes a network call is one that can hang. An explicit chat_api_type overrides
    the guess, which is what it is for.

    Mutation checks: making every provider need a key, or sending OpenRouter's extensions
    everywhere, each fails below.
    """
    failed = False
    print("**** Testing provider detection and per-provider payload ****")

    cases = {
        "https://openrouter.ai/api/v1": ("openrouter", True),
        "http://localhost:11434/v1": ("ollama", False),
        "http://192.168.1.50:11434/v1": ("ollama", False),
        "http://127.0.0.1:8080/v1": ("local", False),
        "http://nas.local:8080/v1": ("local", False),
        "https://api.openai.com/v1": ("openai", True),
    }
    for url, (expected_name, expected_key) in cases.items():
        name, settings = resolve_provider("auto", url)
        if name != expected_name:
            print("ERROR: {} detected as {!r}, expected {!r}".format(url, name, expected_name))
            failed = True
        if settings["needs_key"] is not expected_key:
            print("ERROR: {} needs_key is {}, expected {}".format(url, settings["needs_key"], expected_key))
            failed = True

    # An explicit type wins over the URL - the escape hatch for a setup the guess gets wrong.
    if resolve_provider("ollama", "https://somewhere.example/v1")[0] != "ollama":
        print("ERROR: an explicit chat_api_type did not override detection")
        failed = True
    # An unknown type falls back to generic OpenAI-compatible rather than raising.
    if resolve_provider("something-new", "https://x.example/v1")[1]["needs_key"] is not True:
        print("ERROR: an unrecognised chat_api_type did not fall back safely")
        failed = True

    # The payload carries only the extensions the provider understands.
    for url, expect_openrouter in (("https://openrouter.ai/api/v1", True), ("http://localhost:11434/v1", False)):
        agent = _agent_with_fake(my_predbat, _text_response("ok"), base_url=url, api_type="auto")
        payload = {}
        original = agent._stream_chunks

        async def capture(sent, _payload=payload, _original=original):
            """Record the payload, then replay the canned response."""
            _payload.update(sent)
            async for chunk in _original(sent):
                yield chunk

        agent._stream_chunks = capture
        cid = asyncio.run(agent.store.create())
        asyncio.run(agent.run_turn(cid, "hello"))

        has_usage_include = "usage" in payload
        has_stream_options = "stream_options" in payload
        if has_usage_include is not expect_openrouter:
            print("ERROR: {} usage.include present={}, expected {}".format(url, has_usage_include, expect_openrouter))
            failed = True
        # Everyone else reports usage through the standard option, so the token counter keeps
        # working off OpenRouter rather than silently reading zero.
        if has_stream_options is expect_openrouter:
            print("ERROR: {} stream_options present={}, expected {}".format(url, has_stream_options, not expect_openrouter))
            failed = True

    return failed


def test_legacy_openrouter_keys_still_work(my_predbat):
    """An apps.yaml written before the rename keeps working untouched.

    The openrouter_* names were live on real installations before chat_api_* existed. Dropping
    them would have broken those installs on update, with the Chat tab simply vanishing and
    nothing saying why.

    Mutation check: removing the legacy fallbacks leaves the agent with no key, url or model.
    """
    failed = False
    print("**** Testing the pre-rename apps.yaml keys still work ****")

    agent = _agent_with_fake(my_predbat, _text_response("ok"), api_key=None, base_url=None, model=None, legacy_api_key="sk-old", legacy_base_url="https://openrouter.ai/api/v1", legacy_model="old/model")
    if agent.api_key != "sk-old":
        print("ERROR: openrouter_api_key was not read as a fallback: {}".format(agent.api_key))
        failed = True
    if agent.default_model != "old/model":
        print("ERROR: openrouter_default_model was not read as a fallback: {}".format(agent.default_model))
        failed = True
    if agent.provider_name != "openrouter":
        print("ERROR: the legacy base url did not resolve to OpenRouter: {}".format(agent.provider_name))
        failed = True

    # The new names win when both are set.
    agent = _agent_with_fake(my_predbat, _text_response("ok"), api_key="sk-new", legacy_api_key="sk-old")
    if agent.api_key != "sk-new":
        print("ERROR: the legacy key overrode the new one: {}".format(agent.api_key))
        failed = True

    # With neither url set, OpenRouter is still the default endpoint.
    agent = _agent_with_fake(my_predbat, _text_response("ok"), api_key="sk-new", base_url=None, legacy_base_url=None)
    if agent.base_url != OPENROUTER_BASE_URL:
        print("ERROR: no url configured did not fall back to OpenRouter: {}".format(agent.base_url))
        failed = True

    return failed


def run_chat_tests(my_predbat):
    """Run every chat agent test, returning True if any of them failed."""
    failed = False
    failed |= test_component_gating(my_predbat)
    failed |= test_provider_detection_and_payload(my_predbat)
    failed |= test_legacy_openrouter_keys_still_work(my_predbat)
    failed |= test_model_resolution_order(my_predbat)
    failed |= test_turn_error_is_detailed_and_stored_but_never_replayed(my_predbat)
    failed |= test_confirmation_card_resolves_nested_paths(my_predbat)
    failed |= test_stop_reaches_a_turn_parked_on_a_confirmation(my_predbat)
    failed |= test_approvals_are_recorded_but_never_replayed(my_predbat)
    failed |= test_component_gating_end_to_end(my_predbat)
    failed |= test_build_snapshot(my_predbat)
    failed |= test_snapshot_formats_times_and_percentages(my_predbat)
    failed |= test_build_system_prompt(my_predbat)
    failed |= test_primer_says_when_not_to_call_a_tool(my_predbat)
    failed |= test_event_buffer(my_predbat)
    failed |= test_pending_conversations_is_lock_guarded(my_predbat)
    failed |= test_pending_conversations_survives_concurrent_mutation(my_predbat)
    failed |= test_release_stale_turn(my_predbat)
    failed |= test_release_stale_turn_keeps_a_slot_parked_in_confirmation(my_predbat)
    failed |= test_plain_answer(my_predbat)
    failed |= test_tool_call_round_trip(my_predbat)
    failed |= test_dispatch_strips_chat_omit_properties(my_predbat)
    failed |= test_tool_call_ids_are_normalised_when_the_provider_omits_them(my_predbat)
    failed |= test_tool_round_cap(my_predbat)
    failed |= test_deadline_checked_between_tool_calls_within_a_round(my_predbat)
    failed |= test_tool_failures_are_results(my_predbat)
    failed |= test_titles(my_predbat)
    failed |= test_title_instruction_reaches_the_model_without_touching_the_stored_message(my_predbat)
    failed |= test_system_prompt_is_frozen_byte_identical_across_turns_including_a_titling_turn(my_predbat)
    failed |= test_system_message_carries_cache_control(my_predbat)
    failed |= test_frozen_system_prompt_is_built_once_and_reused_not_rebuilt(my_predbat)
    failed |= test_usage_event_and_totals_report_cached_tokens(my_predbat)
    failed |= test_last_prompt_tokens_reflects_the_most_recent_turn_not_the_cumulative_total(my_predbat)
    failed |= test_request_timeout_and_turn_timeout_are_not_confused(my_predbat)
    failed |= test_execute_turn_preserves_a_slot_claimed_by_a_later_turn(my_predbat)
    failed |= test_busy_rejects_a_second_turn(my_predbat)
    failed |= test_search_source_runs_off_the_loop(my_predbat)
    failed |= test_submit_turn_hands_off_to_the_component_loop(my_predbat)
    failed |= test_submit_turn_needs_a_running_component(my_predbat)
    failed |= test_write_confirmation_approved(my_predbat)
    failed |= test_write_confirmation_rejected(my_predbat)
    failed |= test_write_confirmation_timeout(my_predbat)
    failed |= test_write_without_confirmation(my_predbat)
    failed |= test_set_apps_config_confirmation_gate_and_card(my_predbat)
    failed |= test_set_apps_config_approved_writes_apps_yaml(my_predbat)
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
    failed |= test_rate_limited_retry_is_patient(my_predbat)
    failed |= test_retry_after_header_is_honoured(my_predbat)
    failed |= test_retry_backoff_sequence_is_one_then_three_seconds(my_predbat)
    failed |= test_retry_never_sleeps_past_the_turn_deadline(my_predbat)
    failed |= test_retried_attempt_does_not_duplicate_the_failed_attempts_partial_content(my_predbat)
    failed |= test_empty_completion_is_retried_and_recovers(my_predbat)
    return failed
