# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""The Predbat AI chat agent component.

Presents Predbat's tools directly to an OpenRouter-served model as function-calling tools and
runs the agentic loop. Deliberately holds no loop-bound state: the turn itself runs on whichever
event loop invoked it, which in practice is the web component's, while this component's own
thread only flushes and prunes. See spec section 3.
"""

import asyncio
import threading
import time
from datetime import datetime

from component_base import ComponentBase
from chat_store import ConversationStore
from chat_tools import DEFAULT_FETCH_ALLOWLIST

EVENT_BUFFER_MAX = 2000

# How long past its own deadline a turn must go before its slot is assumed abandoned. Only a
# component restart can strand a slot, and that is rare - so the grace period is generous.
STALE_TURN_GRACE_SECONDS = 60

PRIMER = """You are an assistant built into Predbat, a home battery optimisation system that plans when to charge and discharge a household battery based on electricity rates, solar forecasts and historical load. The person you are talking to owns this system and is looking at its web interface.

Answer concisely and quote the user's real values rather than generalities. Call a tool rather than guessing: the tools read this specific installation. Use search_docs for questions about how to configure Predbat, and search_source then read_source for questions about what the code actually does - the source you can read is the exact version running here. Never invent an entity name; look it up with get_entities or get_config."""


class ChatBusyError(RuntimeError):
    """Raised when a turn is requested while another is already running."""


class AgentNotReadyError(RuntimeError):
    """Raised when work is handed to the component before its event loop exists."""


def build_snapshot(base):
    """Render a compact description of the live system for the system prompt.

    Rebuilt every turn from base state rather than stored with the conversation, so a restored
    conversation is never anchored to a stale picture. Everything deeper than this stays behind a
    tool call, which is what keeps the per-turn cost bounded.
    """

    def arg(name, default=None):
        """Read one Predbat argument, tolerating a base that has not finished starting."""
        try:
            return base.get_arg(name, default)
        except Exception:
            return default

    def state(name, default=None):
        """Read one attribute off the base instance."""
        return getattr(base, name, default)

    lines = ["Current Predbat state:"]
    lines.append("- Time now: {}".format(state("now_utc", datetime.now())))
    lines.append("- Predbat version: {}".format(state("this_version", "unknown")))
    lines.append("- Status: {}".format(state("current_status", "unknown")))
    lines.append("- Mode: {}".format(arg("mode", "unknown")))
    lines.append("- SOC: {} kWh of {} kWh, reserve {}".format(state("soc_kw", "unknown"), state("soc_max", "unknown"), state("reserve", "unknown")))
    lines.append("- Inverters: {} of type {}".format(state("num_inverters", "unknown"), arg("inverter_type", "unknown")))
    lines.append("- Cars configured: {}".format(state("num_cars", 0)))
    lines.append("- Currency: {}".format(state("currency_symbols", ["p", "£"])))
    lines.append("- Errors seen this run: {}".format(bool(state("had_errors", False))))

    rate_import = state("rate_import", {}) or {}
    rate_export = state("rate_export", {}) or {}
    minutes_now = state("minutes_now", 0) or 0
    if rate_import:
        lines.append("- Import rate now: {}".format(rate_import.get(minutes_now, "unknown")))
    if rate_export:
        lines.append("- Export rate now: {}".format(rate_export.get(minutes_now, "unknown")))

    windows = state("charge_window_best", []) or []
    if windows:
        lines.append("- Next planned charge window: {}".format(windows[0]))
    exports = state("export_window_best", []) or []
    if exports:
        lines.append("- Next planned export window: {}".format(exports[0]))

    return "\n".join(lines)


class ChatAgent(ComponentBase):
    """Runs the OpenRouter-backed chat agent for the Predbat web interface."""

    def initialize(self, api_key, model, base_url="https://openrouter.ai/api/v1", max_tokens=0, max_tool_calls=8, max_history=40, max_conversations=20, expiry_days=30, turn_timeout=180, fetch_allowlist=None):
        """Store configuration and build the conversation store and event buffer."""
        self.api_key = api_key
        self.default_model = model
        self.base_url = str(base_url or "https://openrouter.ai/api/v1").rstrip("/")
        self.max_tokens = max_tokens or 0
        self.max_tool_calls = max_tool_calls or 8
        self.max_history = max_history or 40
        self.turn_timeout = turn_timeout or 180
        self.fetch_allowlist = list(fetch_allowlist) if fetch_allowlist else list(DEFAULT_FETCH_ALLOWLIST)
        self.lock = threading.Lock()
        # Set on the first run() tick, from inside this component's own thread. Everything the
        # web layer hands over is scheduled onto it; until it exists, the component is still
        # starting and handlers answer 503.
        self.loop = None
        self.events = []
        self.event_seq = 0
        self.event_base = 0
        self.active = None
        self.pending_confirm = {}
        self.warned_web_search_base_url = False
        self.store = ConversationStore(self.storage, self.log, max_history=self.max_history, max_conversations=max_conversations or 20, expiry_days=expiry_days or 30)
        self.index_loaded = False
        self.turn_counter = 0

    def emit(self, conversation_id, event_type, data=None):
        """Append an event to the buffer and return its sequence number.

        A conversation_id of None marks a global event - busy, idle and reload - which every
        browser receives whatever conversation it is currently looking at.
        """
        with self.lock:
            self.event_seq += 1
            event = {"seq": self.event_seq, "conversation_id": conversation_id, "type": event_type, "data": data or {}}
            self.events.append(event)
            while len(self.events) > EVENT_BUFFER_MAX:
                self.events.pop(0)
                self.event_base += 1
            return self.event_seq

    def events_since(self, cursor, conversation_id):
        """Return events after cursor for one conversation, plus the new cursor and reload flag.

        Cursor replay rather than a live queue is what lets two browsers follow the same turn and
        a mid-turn reload resume: the buffer is the single source and each reader keeps a position.
        """
        with self.lock:
            cursor = int(cursor or 0)
            reload_needed = bool(self.events) and cursor < self.event_base
            selected = [event for event in self.events if event["seq"] > cursor and event["conversation_id"] in (None, conversation_id)]
            return selected, self.event_seq, reload_needed

    async def run_on_agent_loop(self, coro):
        """Await a coroutine on this component's own loop, from another thread's loop.

        run_coroutine_threadsafe schedules the work on the component loop and hands back a
        concurrent Future; wrap_future turns that into something the *calling* loop can await.
        The web loop therefore yields rather than blocking, and the work runs where it belongs.
        """
        loop = self.loop
        if loop is None:
            coro.close()
            raise AgentNotReadyError("The chat component has not finished starting")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def run(self, seconds, first):
        """Record this component's loop, load the index, then flush and stay healthy.

        No network call happens here. Validating credentials at startup would let a slow or
        unreachable OpenRouter block Predbat's boot inside wait_api_started() for ten minutes.
        """
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        if not self.index_loaded:
            try:
                await self.store.load_index()
            except Exception as error:
                self.log("Warn: chat agent could not load its conversation index: {}".format(error))
            self.index_loaded = True
        else:
            try:
                await self.store.flush()
            except Exception as error:
                self.log("Warn: chat agent could not flush conversations: {}".format(error))
                self.count_errors += 1
        self._release_stale_turn()
        self.update_success_timestamp()
        return True

    def _release_stale_turn(self):
        """Clear a turn slot whose coroutine died without running its own cleanup.

        A turn scheduled on this loop is killed outright if the component is stopped or restarted
        mid-turn, because asyncio.run() closes the loop and the finally in _execute_turn never
        runs, leaving the composer locked in every browser until Predbat restarts.

        The test is elapsed wall-clock against the turn's own deadline, NOT a count of quiet
        ticks. A turn emits one busy event and can then legitimately produce nothing for a minute
        or more while the model thinks, and the housekeeping tick only fires every 60 seconds - so
        a two-tick rule frees the slot of a turn that is merely slow. Waiting until the turn has
        outlived its own deadline plus a grace period means a live turn is never touched.
        """
        with self.lock:
            active = self.active
            if active is None:
                return
            started = active.get("started")
            if started is None or time.monotonic() - started < self.turn_timeout + STALE_TURN_GRACE_SECONDS:
                return
            turn_id = active.get("turn_id")
            self.active = None
        self.log("Warn: chat turn {} outlived its {}s timeout with no cleanup - releasing the turn slot".format(turn_id, self.turn_timeout))
        self.emit(None, "idle", {})
