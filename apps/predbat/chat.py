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

import aiohttp
import asyncio
import functools
import json
import threading
import time
from datetime import datetime

from agent_tools import TOOL_DEFS, PredbatTools, openai_tool_list
from component_base import ComponentBase
from chat_store import ConversationStore, NEW_CONVERSATION_TITLE, derive_title, trim_history
from chat_tools import CHAT_TOOL_DEFS, DEFAULT_FETCH_ALLOWLIST, fetch_url, read_source, search_docs, search_source

EVENT_BUFFER_MAX = 2000

# How long a fetched model catalogue is trusted before list_models() refreshes it. list_models()
# passes this plus a further 60 minutes as the stale ceiling - the window during which a stale copy
# is still served while one caller refreshes in the background - so the effective outer limit is 25
# hours, not 24. OpenRouter's catalogue changes rarely enough that once a day is plenty either way.
MODEL_CACHE_MINUTES = 1440

# How long past its own deadline a turn must go before its slot is assumed abandoned. Only a
# component restart can strand a slot, and that is rare - so the grace period is generous.
STALE_TURN_GRACE_SECONDS = 60

# How long a write tool waits for a user's confirm/reject answer before it is treated as declined,
# and how often await_confirmation polls for it. Polling rather than an asyncio.Event because the
# answer arrives from the web thread's loop while the turn runs on the component's own loop, and
# an Event is bound to whichever loop created it.
CONFIRM_TIMEOUT_SECONDS = 300
CONFIRM_POLL_SECONDS = 0.2

PRIMER = """You are an assistant built into Predbat, a home battery optimisation system that plans when to charge and discharge a household battery based on electricity rates, solar forecasts and historical load. The person you are talking to owns this system and is looking at its web interface.

Answer concisely and quote the user's real values rather than generalities. Call a tool rather than guessing: the tools read this specific installation. Use search_docs for questions about how to configure Predbat, and search_source then read_source for questions about what the code actually does - the source you can read is the exact version running here. Never invent an entity name; look it up with get_entities or get_config."""


class ChatBusyError(RuntimeError):
    """Raised when a turn is requested while another is already running."""


class AgentNotReadyError(RuntimeError):
    """Raised when work is handed to the component before its event loop exists."""


class ChatRequestError(RuntimeError):
    """Raised when the chat-completions endpoint returns a non-200 response."""

    def __init__(self, status, body):
        """Keep the status and body so the message can name what actually went wrong."""
        super().__init__("HTTP {}: {}".format(status, body[:500]))
        self.status = status
        self.body = body

    def friendly(self):
        """Return a message worth showing a user rather than a raw HTTP error."""
        if self.status == 401:
            return "OpenRouter rejected the API key - check openrouter_api_key in apps.yaml"
        if self.status == 402:
            return "OpenRouter reports insufficient credit: {}".format(self.body[:200])
        if self.status == 429:
            return "Rate limited by OpenRouter, try again shortly"
        return "OpenRouter returned HTTP {}: {}".format(self.status, self.body[:200])


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
        self.tools = PredbatTools(self.base, log_func=self.log)
        self.tool_defs_by_name = {entry["name"]: entry for entry in list(TOOL_DEFS) + list(CHAT_TOOL_DEFS)}

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

    def pending_conversations(self):
        """Return the set of conversation ids with a write awaiting confirmation.

        pending_confirm is mutated (inserted, popped and wholesale rebuilt) from the component
        thread under self.lock - see claim/confirm handling in _run_one_tool, confirm() and
        _execute_turn's cleanup. Iterating it directly from the web thread without the lock races
        those mutations: a confirmation landing mid-iteration can raise "dictionary changed size
        during iteration" and 500 the request that called this. Snapshotting the values under the
        lock, then building the id set outside it, keeps the lock held for as short as possible.
        """
        with self.lock:
            entries = list(self.pending_confirm.values())
        return {entry["conversation_id"] for entry in entries}

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

        A turn parked in await_confirmation is a further exception, and it does not depend on the
        defaults lining up. At the shipped values CONFIRM_TIMEOUT_SECONDS (300s) sits inside
        turn_timeout + STALE_TURN_GRACE_SECONDS (300 + 60 = 360s), so the arithmetic is
        comfortable - but chat_turn_timeout is user-configurable, and anything below 240s puts the
        confirmation window back outside the stale threshold. The exception is therefore written to
        hold whatever those numbers are: a user reading an Approve/Reject prompt gets a generous
        window rather than the turn's own budget for talking to the model. So a turn whose active call is still
        in self.pending_confirm is left alone regardless of elapsed time - its own timeout is
        CONFIRM_TIMEOUT_SECONDS, enforced by await_confirmation itself, not this one.
        """
        with self.lock:
            active = self.active
            if active is None:
                return
            started = active.get("started")
            if started is None or time.monotonic() - started < self.turn_timeout + STALE_TURN_GRACE_SECONDS:
                return
            turn_id = active.get("turn_id")
            if any(entry.get("turn_id") == turn_id for entry in self.pending_confirm.values()):
                return
            self.active = None
        self.log("Warn: chat turn {} outlived its {}s timeout with no cleanup - releasing the turn slot".format(turn_id, self.turn_timeout))
        self.emit(None, "idle", {})

    def build_messages(self, conversation_id, history):
        """Assemble the request messages: a freshly built system prompt plus trimmed history."""
        meta = self.store.get_meta(conversation_id) or {}
        parts = [PRIMER, "", build_snapshot(self.base)]
        if meta.get("title", NEW_CONVERSATION_TITLE) == NEW_CONVERSATION_TITLE:
            parts.append("")
            parts.append("This conversation has no title yet. Call set_chat_title once, early in your reply, with a short descriptive title of at most 60 characters summarising what the user is asking about.")
        return [{"role": "system", "content": "\n".join(parts)}] + trim_history(history, self.max_history, log=self.log)

    def tool_payload(self):
        """Return the tool list offered to the model: the shared tools plus the chat-only ones."""
        return openai_tool_list() + openai_tool_list(CHAT_TOOL_DEFS)

    async def _fetch_model_catalogue(self):
        """Download the model catalogue from the configured endpoint."""
        headers = {"Authorization": "Bearer {}".format(self.api_key)}
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("{}/models".format(self.base_url), headers=headers) as response:
                if response.status != 200:
                    return None
                return await response.json()

    async def list_models(self):
        """Return the tool-capable models on offer, always including the configured one.

        A model with no tool support cannot drive this agent at all - it would answer from the
        snapshot alone and never call get_plan - so the picker hides them rather than letting a
        user select one and wonder why the answers got worse. The catalogue itself is cached once
        a day via Storage's stale-while-revalidate helper, because it is only ever consulted to
        populate a dropdown and does not need to be fetched on every page load; a custom
        openrouter_base_url with no /models endpoint at all still works because the configured
        model is added whether or not the catalogue could be read.
        """
        catalogue = None
        storage = self.storage
        try:
            if storage:
                catalogue = await storage.fetch_cached("chat", "models", self._fetch_model_catalogue, fresh_minutes=MODEL_CACHE_MINUTES, stale_minutes=MODEL_CACHE_MINUTES + 60, format="json")
            else:
                catalogue = await self._fetch_model_catalogue()
        except Exception as error:
            self.log("Warn: could not fetch the model catalogue from {}: {}".format(self.base_url, error))

        models = []
        for entry in (catalogue or {}).get("data") or []:
            if "tools" not in (entry.get("supported_parameters") or []):
                continue
            pricing = entry.get("pricing") or {}
            models.append({"id": entry.get("id"), "name": entry.get("name") or entry.get("id"), "prompt_price": pricing.get("prompt"), "completion_price": pricing.get("completion")})
        models.sort(key=lambda entry: str(entry.get("id")))
        if self.default_model not in [entry["id"] for entry in models]:
            models.insert(0, {"id": self.default_model, "name": "{} (from apps.yaml)".format(self.default_model), "prompt_price": None, "completion_price": None})
        return models

    async def _stream_chunks(self, payload):
        """Yield decoded chunk dicts from the chat-completions endpoint.

        The only network call in the component, and the seam the tests replace. The session is
        created per request because this coroutine runs on whichever event loop invoked the turn.
        """
        headers = {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": "application/json", "HTTP-Referer": "https://springfall2008.github.io/batpred/", "X-Title": "Predbat"}
        timeout = aiohttp.ClientTimeout(total=self.turn_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("{}/chat/completions".format(self.base_url), headers=headers, json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    raise ChatRequestError(response.status, body)
                async for raw in response.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue

    async def _run_completion(self, conversation_id, messages, model):
        """Stream one completion, returning the assistant message, its usage and any sources."""
        payload = {"model": model, "messages": messages, "tools": self.tool_payload(), "stream": True, "usage": {"include": True}}
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.web_search_enabled():
            payload["plugins"] = [{"id": "web"}]

        content = ""
        usage = {}
        sources = []
        accumulator = {}
        async for chunk in self._stream_chunks(payload):
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            if delta.get("content"):
                content += delta["content"]
                self.emit(conversation_id, "delta", {"text": delta["content"]})
            for annotation in delta.get("annotations") or []:
                citation = annotation.get("url_citation") or {}
                if citation.get("url"):
                    sources.append({"url": citation["url"], "title": citation.get("title") or citation["url"]})
            # Tool call fragments are keyed by index, not id: the id and name arrive only in the
            # first fragment and the arguments are split across the rest.
            for fragment in delta.get("tool_calls") or []:
                slot = accumulator.setdefault(fragment.get("index", 0), {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]

        message = {"role": "assistant", "content": content or None}
        if accumulator:
            # Real OpenAI-compatible providers always send an id, but the invariant that every
            # stored tool_call_id matches a stored call id must hold by construction, not by the
            # provider's goodwill - an id-less call would otherwise store as {"id": None, ...},
            # and downstream code would have to invent a per-call synthetic id anyway. Doing it
            # once here, keyed on the accumulator's own index, keeps two id-less calls in the same
            # message distinct instead of colliding on a single turn-wide fallback.
            for index in accumulator:
                if not accumulator[index].get("id"):
                    accumulator[index]["id"] = "call_auto_{}".format(index)
            message["tool_calls"] = [accumulator[index] for index in sorted(accumulator)]
        return message, usage, sources

    async def _dispatch(self, conversation_id, name, arguments):
        """Run one tool, trying the chat-only tools before the shared Predbat ones.

        Every property named in the tool's chat_omit_properties is stripped from arguments right
        here, before any branch below sees them - the single choke point every tool call passes
        through regardless of which one handles it. openai_tool_list() only removes the property
        from the schema offered to the model; nothing stops the model - or content it read via
        fetch_url/search_docs - from naming the property anyway (this is exactly how 'masked':
        false reaches get_apps: the tool description still says credentials are redacted "by
        default", and fetch_url's github.com allowlist can serve a page that names the argument).
        So the guarantee has to be enforced on the arguments dict actually executed, not merely
        omitted from what the model was invited to ask for. See spec section 14.1. Builds a new
        dict rather than popping in place, so this never mutates the arguments already captured
        by the tool_start/confirm event emitted just before this call.
        """
        omit = (self.tool_defs_by_name.get(name) or {}).get("chat_omit_properties") or []
        if omit and arguments:
            arguments = {key: value for key, value in arguments.items() if key not in omit}
        if name == "set_chat_title":
            title = self.store.set_title(conversation_id, arguments.get("title"))
            if title is None:
                return {"success": False, "error": "This conversation no longer exists", "data": None}
            self.titled_this_turn = True
            self.emit(conversation_id, "title", {"title": title})
            return {"success": True, "error": None, "data": {"title": title}}
        if name == "search_docs":
            return await search_docs(self.storage, arguments.get("query"), max_results=arguments.get("max_results", 5))
        # read_source is bounded work on this loop, which is fine: the component loop's only
        # other job is a five-second tick, and the web server is a different loop in a different
        # thread. search_source is different - it runs a MODEL-SUPPLIED regular expression, and
        # Python's re engine backtracks with no timeout, so a pattern like (.*)*x can hang inside
        # a single search() call that no elapsed-time check can interrupt. Run it on a worker
        # thread so a pathological pattern burns that thread rather than killing the component's
        # only event loop: the component stays alive, other turns still run, and this turn dies on
        # its own deadline. Containment, not latency.
        #
        # Never pass `root` to either function. It is deliberately absent from both tool schemas -
        # a model that could set it would point the search at /config and walk straight past the
        # extension allowlist that keeps apps.yaml and the token cache out.
        if name == "search_source":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, functools.partial(search_source, arguments.get("pattern"), file=arguments.get("file"), max_results=arguments.get("max_results", 20)))
        if name == "read_source":
            return read_source(arguments.get("file"), start_line=arguments.get("start_line", 1), max_lines=arguments.get("max_lines", 200))
        if name == "fetch_url":
            return await fetch_url(arguments.get("url"), allowlist=self.fetch_allowlist)
        return await self.tools.execute(name, arguments)

    def claim_turn(self, conversation_id):
        """Reserve the single turn slot and return the new turn id.

        Synchronous and lock-guarded so the web thread can decide busy-or-not without waiting on
        either loop: by the time submit_turn returns, a concurrent send is already a clean 409.
        """
        meta = self.store.get_meta(conversation_id)
        if meta is None:
            raise KeyError("Unknown conversation {}".format(conversation_id))
        with self.lock:
            if self.active is not None:
                raise ChatBusyError("A reply is already in progress in '{}'".format(self.active.get("title")))
            self.turn_counter += 1
            turn_id = self.turn_counter
            # started is what _release_stale_turn measures against; without it a stranded slot is
            # never freed, and with a tick count instead it would free live ones.
            self.active = {"conversation_id": conversation_id, "turn_id": turn_id, "title": meta.get("title"), "started": time.monotonic()}
        return turn_id

    def submit_turn(self, conversation_id, text):
        """Start a turn on this component's loop and return its id without waiting for it.

        This is the web layer's entry point. The reply is delivered through the event buffer, so
        the HTTP request that started it has nothing left to wait for.
        """
        loop = self.loop
        if loop is None:
            raise AgentNotReadyError("The chat component has not finished starting")
        turn_id = self.claim_turn(conversation_id)
        asyncio.run_coroutine_threadsafe(self._execute_turn(conversation_id, turn_id, text), loop)
        return turn_id

    async def run_turn(self, conversation_id, text):
        """Claim and run a turn inline on the current loop, returning its id when it finishes."""
        turn_id = self.claim_turn(conversation_id)
        await self._execute_turn(conversation_id, turn_id, text)
        return turn_id

    async def _execute_turn(self, conversation_id, turn_id, text):
        """Run one full agentic turn, releasing the turn slot however it ends."""
        meta = self.store.get_meta(conversation_id) or {}
        self.titled_this_turn = False
        self.deadline = time.monotonic() + self.turn_timeout
        self.emit(None, "busy", {"conversation_id": conversation_id, "title": meta.get("title"), "turn_id": turn_id})
        try:
            await self.store.append(conversation_id, {"role": "user", "content": text})
            self.emit(conversation_id, "user", {"text": text})
            await self._turn_loop(conversation_id, turn_id, text)
        except ChatRequestError as error:
            self.count_errors += 1
            self.emit(conversation_id, "error", {"message": error.friendly()})
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self.count_errors += 1
            self.emit(conversation_id, "error", {"message": "Could not reach {}: {}".format(self.base_url, error)})
        except Exception as error:
            self.count_errors += 1
            self.log("Error: chat turn failed: {}".format(error))
            self.emit(conversation_id, "error", {"message": "The chat turn failed: {}".format(error)})
        finally:
            if not self.titled_this_turn and (self.store.get_meta(conversation_id) or {}).get("title") == NEW_CONVERSATION_TITLE:
                title = self.store.set_title(conversation_id, derive_title(text))
                if title:
                    self.emit(conversation_id, "title", {"title": title})
            try:
                await self.store.flush(conversation_id)
            except Exception as error:
                self.count_errors += 1
                self.log("Warn: could not persist chat conversation {}: {}".format(conversation_id, error))
            with self.lock:
                self.pending_confirm = {key: value for key, value in self.pending_confirm.items() if value.get("turn_id") != turn_id}
                # Only clear the slot - and only announce done/idle - if this turn still owns it.
                # If _release_stale_turn already freed it and another turn has since claimed it,
                # an unconditional clear (or an unconditional emit) here would silently unlock the
                # composer everywhere while that later turn is still running.
                owns_slot = (self.active or {}).get("turn_id") == turn_id
                if owns_slot:
                    self.active = None
            if owns_slot:
                self.emit(conversation_id, "done", {"turn_id": turn_id})
                self.emit(None, "idle", {})

    async def _turn_loop(self, conversation_id, turn_id, text):
        """Alternate completions and tool calls until the model answers or the cap is reached."""
        model = (self.store.get_meta(conversation_id) or {}).get("model") or self.default_model
        for iteration in range(self.max_tool_calls + 1):
            if time.monotonic() > self.deadline:
                self.emit(conversation_id, "error", {"message": "This turn took longer than {} seconds and was stopped".format(self.turn_timeout)})
                return
            history = await self.store.get_messages(conversation_id)
            message, usage, sources = await self._run_completion(conversation_id, self.build_messages(conversation_id, history), model)
            await self.store.append(conversation_id, message)
            if usage:
                self.store.add_usage(conversation_id, usage)
                total = (self.store.get_meta(conversation_id) or {}).get("usage_total", {})
                self.emit(conversation_id, "usage", {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0), "cost": usage.get("cost", 0), "conversation_cost": total.get("cost", 0)})
            self.emit(conversation_id, "assistant", {"text": message.get("content") or "", "sources": sources})

            calls = message.get("tool_calls") or []
            if not calls:
                return
            if iteration >= self.max_tool_calls:
                # Answer the calls we are refusing to run. Leaving an assistant message that
                # carries tool_calls with no matching tool replies is exactly the shape
                # trim_history exists to avoid: the next turn sends the pair back, the API
                # rejects the whole request with a 400, and the conversation stays broken until
                # the pair ages out of the trim window. Deliberately no tool_start/tool_end
                # events - nothing ran, and the transcript should not suggest otherwise.
                for call in calls:
                    refused = {"success": False, "error": "Not run: the {} tool call limit for one turn was reached".format(self.max_tool_calls), "data": None}
                    # call["id"] is guaranteed by _run_completion's normalisation - every stored
                    # tool_calls entry carries one, real or synthetic - so no turn-wide fallback
                    # is needed (and one would collide two id-less calls in the same message).
                    await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call.get("id"), "name": (call.get("function") or {}).get("name") or "", "content": json.dumps(refused)})
                break
            for call in calls:
                await self._run_one_tool(conversation_id, turn_id, call)

        note = "I stopped after {} tool calls, which is the configured limit for one turn. Ask me to continue if you want me to keep going.".format(self.max_tool_calls)
        await self.store.append(conversation_id, {"role": "assistant", "content": note})
        self.emit(conversation_id, "assistant", {"text": note, "sources": []})

    async def _run_one_tool(self, conversation_id, turn_id, call):
        """Execute one tool call and append its result as a tool message."""
        name = (call.get("function") or {}).get("name") or ""
        # call["id"] is guaranteed by _run_completion's normalisation, so no turn-wide fallback is
        # needed here either - see the note beside the refusal loop in _turn_loop.
        call_id = call.get("id")
        raw = (call.get("function") or {}).get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if raw.strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as error:
            result = {"success": False, "error": "Could not read the tool argument JSON: {}".format(error), "data": None}
            self.emit(conversation_id, "tool_end", {"call_id": call_id, "name": name, "ok": False, "elapsed": 0, "preview": result["error"]})
            await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
            return

        definition = self.tool_defs_by_name.get(name) or {}
        if definition.get("writes") and self.confirm_writes_enabled():
            with self.lock:
                self.pending_confirm[call_id] = {"conversation_id": conversation_id, "turn_id": turn_id, "approved": None}
            self.emit(conversation_id, "confirm", {"call_id": call_id, "name": name, "arguments": arguments})
            approved = await self.await_confirmation(call_id)
            with self.lock:
                self.pending_confirm.pop(call_id, None)
            if not approved:
                # An ordinary tool result rather than aborting the turn: the model acknowledges the
                # decline and can offer an alternative, which is what a user expects from a refusal.
                result = {"success": False, "error": "User declined this change", "data": None}
                await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
                return

        started = time.monotonic()
        self.emit(conversation_id, "tool_start", {"call_id": call_id, "name": name, "arguments": arguments})
        try:
            result = await self._dispatch(conversation_id, name, arguments)
        except Exception as error:
            result = {"success": False, "error": "Tool '{}' failed: {}".format(name, error), "data": None}
        elapsed = round(time.monotonic() - started, 2)
        encoded = json.dumps(result)
        self.emit(conversation_id, "tool_end", {"call_id": call_id, "name": name, "ok": bool(result.get("success")), "elapsed": elapsed, "preview": encoded[:400]})
        await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": encoded})

    def confirm_writes_enabled(self):
        """Return whether a write tool must be confirmed before it runs.

        Read at the moment the tool is called rather than cached at turn start, so toggling the
        switch mid-turn takes effect on the next tool call.
        """
        value, _ = self.get_ha_config("chat_confirm_writes", True)
        return True if value is None else bool(value)

    def web_search_enabled(self):
        """Return whether OpenRouter's web search plugin should be added to the request.

        The plugin is an OpenRouter feature. If the user has pointed openrouter_base_url at
        something else it is silently ignored by that endpoint, so say so once rather than
        leaving them wondering why nothing changed.
        """
        value, _ = self.get_ha_config("chat_web_search", False)
        if not value:
            return False
        if "openrouter.ai" not in self.base_url:
            if not self.warned_web_search_base_url:
                self.warned_web_search_base_url = True
                self.log("Warn: chat web search is enabled but openrouter_base_url is '{}', which is not OpenRouter - the web plugin will be ignored by that endpoint".format(self.base_url))
            return False
        return True

    def confirm(self, call_id, conversation_id, approved):
        """Record a user's answer to a pending write confirmation."""
        with self.lock:
            pending = self.pending_confirm.get(call_id)
            if pending is None or pending.get("conversation_id") != conversation_id:
                return False
            pending["approved"] = bool(approved)
        self.emit(conversation_id, "confirm_result", {"call_id": call_id, "approved": bool(approved)})
        return True

    async def await_confirmation(self, call_id):
        """Wait for a confirmation answer, polling rather than blocking on a primitive.

        Polling keeps this free of loop-bound objects, so the turn runs correctly on whichever
        event loop invoked it. The time spent parked is added back to two separate clocks, not
        just one: self.deadline (the turn's own timeout, checked in _turn_loop) - a user who steps
        away should not turn their own approval into a timeout - and active["started"] (the
        stale-turn clock _release_stale_turn measures against). _release_stale_turn's own guard
        only protects this turn while its entry is still in pending_confirm; that entry is popped
        by _run_one_tool right after this returns, so a wait long enough to push the extended
        deadline past started + turn_timeout + STALE_TURN_GRACE_SECONDS would otherwise have its
        live slot released on the very next housekeeping tick after the user finally answers -
        displacing the original during-the-wait hazard to just-after-the-answer instead of closing
        it. Advancing both clocks together closes it in both places.
        """
        started = time.monotonic()
        while time.monotonic() - started < CONFIRM_TIMEOUT_SECONDS:
            with self.lock:
                pending = self.pending_confirm.get(call_id)
                if pending is None:
                    break
                if pending.get("approved") is not None:
                    elapsed = time.monotonic() - started
                    self.deadline += elapsed
                    if self.active is not None:
                        self.active["started"] += elapsed
                    return bool(pending["approved"])
            await asyncio.sleep(CONFIRM_POLL_SECONDS)
        elapsed = time.monotonic() - started
        self.deadline += elapsed
        with self.lock:
            if self.active is not None:
                self.active["started"] += elapsed
        return False
