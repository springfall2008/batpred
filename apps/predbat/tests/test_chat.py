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

import time

from chat import EVENT_BUFFER_MAX, STALE_TURN_GRACE_SECONDS, ChatAgent, build_snapshot
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


def run_chat_tests(my_predbat):
    """Run every chat agent test, returning True if any of them failed."""
    failed = False
    failed |= test_component_gating(my_predbat)
    failed |= test_component_gating_end_to_end(my_predbat)
    failed |= test_build_snapshot(my_predbat)
    failed |= test_event_buffer(my_predbat)
    failed |= test_release_stale_turn(my_predbat)
    return failed
