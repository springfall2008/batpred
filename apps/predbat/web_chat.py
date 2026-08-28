# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""The Chat tab: its page, its routes and its server-sent event stream.

Which conversation a browser is looking at is client state, passed on every request, so two
browsers never fight over a shared cursor. The only global server state is the single active
turn, and busy/idle events reach every browser whatever conversation it is viewing - which is
what lets a user read another conversation while a turn runs.
"""

import asyncio
import json
import time

from aiohttp import web

from chat import AgentNotReadyError, ChatBusyError

SSE_POLL_SECONDS = 0.1
SSE_HEARTBEAT_SECONDS = 15


def format_sse_event(event):
    """Render one buffered event as an SSE frame."""
    return "id: {}\nevent: {}\ndata: {}\n\n".format(event["seq"], event["type"], json.dumps(event.get("data") or {}))


class WebChat:
    """Serves the Chat tab and streams turns to the browser."""

    def __init__(self, web_interface):
        """Attach to the running web interface so the chat component is reachable."""
        self.web = web_interface
        self.base = web_interface.base
        self.log = web_interface.log
        self.agent_override = None

    @property
    def agent(self):
        """Return the chat component, or None when it is not configured."""
        if self.agent_override is not None:
            return self.agent_override
        components = getattr(self.base, "components", None)
        return components.get_component("chat") if components else None

    def _conversation_or_404(self, agent, conversation_id):
        """Return a conversation's metadata, or a 404 response to return instead."""
        meta = agent.store.get_meta(conversation_id) if conversation_id else None
        if meta is None:
            return None, web.json_response({"error": "Unknown conversation"}, status=404)
        return meta, None

    async def html_chat(self, request):
        """Render the Chat tab."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        text = self.web.get_header("Predbat Chat")
        text += get_chat_styles()
        text += get_chat_body()
        text += "<script>{}</script>".format(get_chat_script())
        text += "</body></html>"
        return web.Response(content_type="text/html", text=text)

    async def html_chat_conversations(self, request):
        """List the conversations a user can see, plus which one is mid-turn."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        # agent.pending_confirm is mutated from the component thread under agent.lock; reading it
        # directly here, from the web thread, without that lock can raise "dictionary changed size
        # during iteration" if a confirmation lands mid-request. pending_conversations() takes the
        # snapshot under the lock so this request never sees a torn dict.
        pending = agent.pending_conversations()
        conversations = []
        for meta in agent.store.list_conversations():
            conversations.append(
                {"id": meta["id"], "title": meta.get("title"), "updated": meta.get("updated"), "message_count": meta.get("message_count", 0), "cost": (meta.get("usage_total") or {}).get("cost", 0), "pending_confirm": meta["id"] in pending}
            )
        return web.json_response({"conversations": conversations, "active": agent.active, "default_model": agent.default_model})

    async def html_chat_create(self, request):
        """Create a conversation and return its id."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        protect = (agent.active or {}).get("conversation_id")
        try:
            cid = await agent.run_on_agent_loop(agent.store.create(protect_id=protect))
        except AgentNotReadyError:
            return web.json_response({"error": "The chat component is still starting"}, status=503)
        return web.json_response({"id": cid})

    async def html_chat_rename(self, request):
        """Rename a conversation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        meta, error = self._conversation_or_404(agent, body.get("id"))
        if error:
            return error
        title = agent.store.rename(body.get("id"), body.get("title"))
        await agent.run_on_agent_loop(agent.store.flush(body.get("id")))
        return web.json_response({"id": body.get("id"), "title": title})

    async def html_chat_delete(self, request):
        """Hide a conversation, refusing the one with the running turn."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        cid = body.get("id")
        meta, error = self._conversation_or_404(agent, cid)
        if error:
            return error
        if (agent.active or {}).get("conversation_id") == cid:
            return web.json_response({"error": "busy", "message": "This conversation is mid-reply"}, status=409)
        await agent.run_on_agent_loop(agent.store.delete(cid))
        return web.json_response({"deleted": cid})

    async def html_chat_history(self, request):
        """Return one conversation's full transcript for first paint."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        cid = request.query.get("conversation")
        meta, error = self._conversation_or_404(agent, cid)
        if error:
            return error
        # snapshot(), not get_messages(): get_messages() hands back the live list, which the
        # component thread may still be appending to while this request serialises it to JSON on
        # the web thread - json.dumps can raise mid-serialisation on a list mutated underneath it.
        # snapshot() takes its copy under the store's lock so it cannot interleave with an append.
        #
        # The snapshot and the event cursor are taken together, in one coroutine on the agent's
        # own loop, with no await between the two calls. Scheduling them as two separate
        # run_on_agent_loop() calls left a window between them where a brand new turn could append
        # the user's own message and emit its "user" event: that message would then be in neither
        # the snapshot (taken before the append) nor the event stream (whose cursor is already
        # past the event by the time this request reaches events_since) - the user's first message
        # in a new conversation would silently vanish until they switched away and back. Nothing
        # can run on the agent's loop between two synchronous statements in the same coroutine, so
        # combining them closes that window.
        messages, cursor = await agent.run_on_agent_loop(self._snapshot_and_cursor(agent, cid))
        return web.json_response({"id": cid, "title": meta.get("title"), "model": meta.get("model"), "usage_total": meta.get("usage_total"), "messages": messages or [], "cursor": cursor, "active": self._active_with_elapsed(agent)})

    @staticmethod
    def _active_with_elapsed(agent):
        """Return agent.active with elapsed_seconds added, computed now from the turn's own clock.

        A browser calling this route mid-turn - a fresh page load, or a reload - has no idea when
        the turn actually started: the 'busy' SSE event that announced it may have fired long
        before this request, or (a plain page load) never reached this browser's JavaScript at
        all, since the event buffer is only replayed to an already-open EventSource. started is a
        time.monotonic() value recorded once in claim_turn, so elapsed wall-clock time since then
        is computed fresh on every call here rather than trusted from anything the client already
        believes - which is what lets the client offset its own "thinking..." timer correctly
        instead of reading it as having just started.
        """
        active = agent.active
        if not active:
            return None
        elapsed = max(0.0, time.monotonic() - active.get("started", time.monotonic()))
        return dict(active, elapsed_seconds=round(elapsed, 1))

    @staticmethod
    async def _snapshot_and_cursor(agent, conversation_id):
        """Return a message snapshot and the event cursor as of the same instant.

        events_since() is synchronous and lock-guarded, so calling it immediately after awaiting
        snapshot() - both inside this one coroutine - leaves no gap in which a concurrently
        running turn could append a message and emit its event between the two. See the comment
        at the call site in html_chat_history for why that gap mattered.
        """
        messages = await agent.store.snapshot(conversation_id)
        _, cursor, _ = agent.events_since(0, conversation_id)
        return messages, cursor

    async def html_chat_send(self, request):
        """Start a turn, or refuse with 409 when one is already running."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        cid = body.get("conversation")
        meta, error = self._conversation_or_404(agent, cid)
        if error:
            return error
        message = str(body.get("message") or "").strip()
        if not message:
            return web.json_response({"error": "Message is empty"}, status=400)
        # submit_turn claims the slot under the lock and schedules the turn on the component's
        # own loop, so this returns while the reply is still being produced. Everything the user
        # sees arrives over the SSE stream; there is nothing for this request to wait for.
        try:
            turn_id = agent.submit_turn(cid, message)
        except ChatBusyError:
            # turn_id must travel with this 409 the same way it does on the SSE 'busy' event and
            # the active-turn restore on history reload - the client's setBusy() needs it to wire
            # up the Stop button. Without it here, sending into an already-busy conversation shows
            # a Stop button that silently does nothing (state.busy.turn_id is undefined), and
            # worse, overwrites a turn_id a genuine 'busy' event had already set.
            active = agent.active or {}
            return web.json_response({"error": "busy", "conversation_id": active.get("conversation_id"), "title": active.get("title"), "turn_id": active.get("turn_id")}, status=409)
        except AgentNotReadyError:
            return web.json_response({"error": "The chat component is still starting"}, status=503)
        except KeyError:
            return web.json_response({"error": "Unknown conversation"}, status=404)
        return web.json_response({"turn_id": turn_id})

    async def html_chat_confirm(self, request):
        """Answer a pending write confirmation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        if not agent.confirm(body.get("call_id"), body.get("conversation"), bool(body.get("approve"))):
            return web.json_response({"error": "Unknown or expired confirmation"}, status=404)
        return web.json_response({"ok": True})

    async def html_chat_cancel(self, request):
        """Ask the running turn to stop at its next checkpoint.

        Zeroing agent.deadline does not abort mid-step - the model's current completion or tool
        call still runs to the end, and _turn_loop only notices the blown deadline the next time
        it checks (see chat.py's deadline check at the top of each iteration) - hence "at its next
        checkpoint", not immediately.

        Requires the caller's turn_id to match the turn actually running. Without that check, a
        stale cancel request - one sent for a turn that has already finished, arriving late, or
        replayed - would zero the deadline of whatever DIFFERENT turn has since claimed the single
        turn slot, cutting it short for no reason the user asked for.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        turn_id = body.get("turn_id")
        active = agent.active or {}
        if turn_id is None or active.get("turn_id") != turn_id:
            return web.json_response({"error": "No running turn with that id"}, status=409)
        agent.deadline = 0
        return web.json_response({"ok": True})

    async def html_chat_stream(self, request):
        """Stream events for one conversation, plus every global event, as SSE."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        cid = request.query.get("conversation")
        cursor = int(request.query.get("cursor") or 0)

        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        idle = 0.0
        try:
            while True:
                # Re-resolve the agent every iteration rather than trusting the one captured at
                # handler entry. Chat is a restartable component (Components tab, or an automatic
                # restart after a health check failure): a restart builds a brand new ChatAgent
                # instance, and nothing ever writes to the old one's event buffer again. Polling
                # the stale instance forever would keep this stream alive - it would go on
                # heartbeating normally - while silently delivering nothing. Identity comparison
                # also catches the component being stopped outright, since self.agent then becomes
                # None, which is never `is` the original instance either.
                current_agent = self.agent
                if current_agent is not agent:
                    await response.write(b"event: reload\ndata: {}\n\n")
                    break
                events, cursor, reload_needed = agent.events_since(cursor, cid)
                if reload_needed:
                    await response.write(b"event: reload\ndata: {}\n\n")
                    break
                for event in events:
                    await response.write(format_sse_event(event).encode("utf-8"))
                    idle = 0.0
                if not events:
                    idle += SSE_POLL_SECONDS
                    if idle >= SSE_HEARTBEAT_SECONDS:
                        await response.write(b": heartbeat\n\n")
                        idle = 0.0
                await asyncio.sleep(SSE_POLL_SECONDS)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    async def html_chat_models(self, request):
        """Return the model catalogue for the picker."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        try:
            models = await agent.run_on_agent_loop(agent.list_models())
        except AgentNotReadyError:
            return web.json_response({"error": "The chat component is still starting"}, status=503)
        return web.json_response({"models": models, "default_model": agent.default_model, "catalogue_available": len(models) > 1})

    async def html_chat_model(self, request):
        """Set the model for one conversation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        meta, error = self._conversation_or_404(agent, body.get("conversation"))
        if error:
            return error
        agent.store.set_model(body.get("conversation"), body.get("id") or None)
        await agent.run_on_agent_loop(agent.store.flush(body.get("conversation")))
        return web.json_response({"ok": True})

    async def html_chat_status(self, request):
        """Return AI-surface status flags the Chat tab footer shows live.

        Currently just ai_ha_state_enable, read the same way the tool gate itself reads it
        (base.get_ha_config, not cached) so the footer control can never show something the gate
        would disagree with.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        enabled, _ = self.base.get_ha_config("ai_ha_state_enable", False)
        return web.json_response({"ai_ha_state_enabled": bool(enabled)})

    async def html_chat_status_post(self, request):
        """Toggle switch.predbat_ai_ha_state_enable from the Chat tab footer control.

        Writes through ha_interface.set_state_external(), the same mechanism the dashboard's own
        switch toggles (html_dash_post) and the set_config tool both use: it is dispatched as a
        real turn_on/turn_off service call against the matching CONFIG_ITEMS entry, not a raw
        state overwrite. /api/state - the JSON API other switch-like external clients use - only
        writes the raw HA entity state and never updates the matching CONFIG_ITEMS value, so it
        would silently fail to change what get_ha_config('ai_ha_state_enable', ...) actually
        returns; set_state_external is what genuinely flips the switch.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        enabled = bool(body.get("ai_ha_state_enable"))
        entity_id = "switch.{}_ai_ha_state_enable".format(self.base.prefix)
        await self.base.ha_interface.set_state_external(entity_id, enabled)
        return web.json_response({"ok": True, "ai_ha_state_enabled": enabled})


def get_chat_styles():
    """Return the Chat tab's CSS.

    Colours are read from page-local CSS custom properties, redefined under `body.dark-mode`,
    following the same approach `get_plan_css()` uses - so `toggleDarkMode()` (which just toggles
    that class and reloads) works without any extra wiring here.
    """
    return """
<style>
:root {
    --chat-bg: #ffffff;
    --chat-panel-bg: #f7f7f7;
    --chat-border: #dddddd;
    --chat-text: #222222;
    --chat-text-muted: #666666;
    --chat-accent: #4CAF50;
    --chat-user-bubble: #e3f2ea;
    --chat-assistant-bubble: #f1f1f1;
    --chat-error-bubble: #ffe3e3;
    --chat-input-bg: #ffffff;
    --chat-code-bg: #ececec;
    --chat-badge-bg: #ff9800;
    --chat-banner-bg: #fff3cd;
    --chat-banner-border: #ffe08a;
    --chat-banner-text: #664d03;
    --chat-error-text: #c62828;
}

body.dark-mode {
    --chat-bg: #121212;
    --chat-panel-bg: #1e1e1e;
    --chat-border: #444444;
    --chat-text: #e0e0e0;
    --chat-text-muted: #aaaaaa;
    --chat-accent: #66bb6a;
    --chat-user-bubble: #16321f;
    --chat-assistant-bubble: #2a2a2a;
    --chat-error-bubble: #4a1f1f;
    --chat-input-bg: #2b2b2b;
    --chat-code-bg: #262626;
    --chat-badge-bg: #b96a00;
    --chat-banner-bg: #4d3c00;
    --chat-banner-border: #8a6d00;
    --chat-banner-text: #ffe08a;
    --chat-error-text: #ff6b6b;
}

#chat-page {
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    gap: 14px;
    height: calc(100vh - 130px);
    min-height: 420px;
    color: var(--chat-text);
    background: var(--chat-bg);
}

#chat-sidebar {
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--chat-border);
    padding-right: 10px;
    overflow: hidden;
}

#chat-new {
    flex: 0 0 auto;
    margin-bottom: 8px;
    padding: 8px 10px;
    border: 1px solid var(--chat-accent);
    border-radius: 4px;
    background: var(--chat-accent);
    color: #ffffff;
    font-weight: bold;
    cursor: pointer;
}

#chat-list {
    flex: 1 1 auto;
    overflow-y: auto;
}

.chat-conv-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    padding: 6px 8px;
    border-radius: 4px;
}

.chat-conv-row:hover {
    background: var(--chat-panel-bg);
}

.chat-conv-row.active {
    background: var(--chat-user-bubble);
}

.chat-conv-main {
    flex: 1 1 auto;
    min-width: 0;
    cursor: pointer;
}

.chat-conv-title {
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.chat-conv-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--chat-text-muted);
}

.chat-pending-badge {
    background: var(--chat-badge-bg);
    color: #ffffff;
    border-radius: 8px;
    padding: 0 6px;
    font-size: 10px;
    line-height: 16px;
}

.chat-conv-actions {
    flex: 0 0 auto;
    display: flex;
    gap: 2px;
}

.chat-conv-actions button {
    border: none;
    background: transparent;
    color: var(--chat-text-muted);
    cursor: pointer;
    font-size: 13px;
    padding: 2px 4px;
}

.chat-conv-actions button:hover {
    color: var(--chat-text);
}

#chat-main {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
}

#chat-banner {
    display: none;
    background: var(--chat-banner-bg);
    border: 1px solid var(--chat-banner-border);
    color: var(--chat-banner-text);
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 8px;
}

#chat-banner.visible {
    display: block;
}

#chat-privacy {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    background: var(--chat-panel-bg);
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    padding: 6px 10px;
    margin-bottom: 8px;
    font-size: 12px;
    color: var(--chat-text-muted);
}

#chat-privacy.dismissed {
    display: none;
}

#chat-privacy button {
    flex: 0 0 auto;
    border: 1px solid var(--chat-border);
    background: var(--chat-bg);
    color: var(--chat-text);
    border-radius: 4px;
    padding: 2px 8px;
    cursor: pointer;
}

#chat-transcript {
    flex: 1 1 auto;
    overflow-y: auto;
    border: 1px solid var(--chat-border);
    border-radius: 6px;
    padding: 12px;
    background: var(--chat-panel-bg);
}

.chat-bubble {
    max-width: 80%;
    margin: 6px 0;
    padding: 8px 12px;
    border-radius: 8px;
    line-height: 1.4;
    word-wrap: break-word;
}

.chat-bubble-user {
    margin-left: auto;
    background: var(--chat-user-bubble);
}

.chat-bubble-assistant {
    margin-right: auto;
    background: var(--chat-assistant-bubble);
}

.chat-bubble-error {
    margin-right: auto;
    background: var(--chat-error-bubble);
}

.chat-bubble-thinking {
    margin-right: auto;
    color: var(--chat-text-muted);
    font-style: italic;
}

.chat-thinking-hidden {
    display: none;
}

.chat-thinking-caret {
    display: inline-block;
    width: 0.5em;
    margin-left: 2px;
    border-right: 2px solid var(--chat-text-muted);
    animation: chat-thinking-caret-blink 1s steps(1) infinite;
}

@keyframes chat-thinking-caret-blink {
    50% {
        border-color: transparent;
    }
}

@media (prefers-reduced-motion: reduce) {
    .chat-thinking-caret {
        animation: none;
    }
}


.chat-bubble pre,
.chat-tool-row pre,
.chat-confirm-card pre {
    background: var(--chat-code-bg);
    padding: 8px;
    border-radius: 4px;
    overflow-x: auto;
    margin: 6px 0 0 0;
}

.chat-bubble code {
    background: var(--chat-code-bg);
    padding: 1px 4px;
    border-radius: 3px;
}

.chat-sources {
    margin-top: 6px;
    font-size: 12px;
    color: var(--chat-text-muted);
}

.chat-sources ul {
    margin: 4px 0 0 18px;
    padding: 0;
}

.chat-tool-row {
    margin: 6px 0;
}

.chat-tool-row details {
    border: 1px solid var(--chat-border);
    border-radius: 6px;
    padding: 6px 10px;
    background: var(--chat-bg);
}

.chat-tool-row summary {
    cursor: pointer;
    color: var(--chat-text-muted);
}

.chat-tool-row summary code {
    color: var(--chat-text);
}

.chat-tool-pending {
    font-style: italic;
    color: var(--chat-text-muted);
}

.chat-tool-ok {
    color: var(--chat-accent);
}

.chat-tool-error {
    color: var(--chat-error-text);
}

.chat-tool-elapsed {
    font-size: 11px;
    color: var(--chat-text-muted);
    margin-top: 4px;
}

.chat-confirm-card {
    margin: 8px 0;
    border: 2px solid var(--chat-accent);
    border-radius: 6px;
    padding: 10px 12px;
    background: var(--chat-bg);
}

.chat-confirm-heading {
    font-weight: bold;
    margin-bottom: 6px;
}

.chat-confirm-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}

.chat-confirm-actions button {
    padding: 6px 14px;
    border-radius: 4px;
    border: 1px solid var(--chat-border);
    cursor: pointer;
}

.chat-confirm-approve {
    background: var(--chat-accent);
    color: #ffffff;
    border-color: var(--chat-accent);
}

.chat-confirm-reject {
    background: var(--chat-bg);
    color: var(--chat-text);
}

.chat-confirm-outcome {
    font-style: italic;
    color: var(--chat-text-muted);
}

#chat-composer {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}

#chat-input {
    flex: 1 1 auto;
    resize: vertical;
    min-height: 48px;
    max-height: 200px;
    padding: 8px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-input-bg);
    color: var(--chat-text);
    font-family: inherit;
    font-size: 14px;
}

#chat-input:disabled,
#chat-send:disabled {
    opacity: 0.6;
    cursor: not-allowed;
}

#chat-send {
    flex: 0 0 auto;
    padding: 8px 18px;
    border: none;
    border-radius: 4px;
    background: var(--chat-accent);
    color: #ffffff;
    font-weight: bold;
    cursor: pointer;
}

#chat-stop {
    flex: 0 0 auto;
    padding: 8px 18px;
    border: 1px solid var(--chat-error-text);
    border-radius: 4px;
    background: transparent;
    color: var(--chat-error-text);
    font-weight: bold;
    cursor: pointer;
    display: none;
}

#chat-stop.visible {
    display: inline-block;
}

#chat-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-top: 6px;
    font-size: 12px;
    color: var(--chat-text-muted);
}

#chat-model {
    padding: 4px 6px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-input-bg);
    color: var(--chat-text);
}

#chat-model-note {
    margin-left: 6px;
    font-style: italic;
}

#chat-ha-state-wrap {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
    user-select: none;
}
</style>
"""


def get_chat_body():
    """Return the Chat tab's markup.

    `get_header_html()` has already opened `<body>` and written the nav bar, so this only adds
    the page's own content, following the same `<body>...` convention `web_annual.py` uses for its
    own pages.
    """
    return """
<body>
<div id="chat-page">
    <div id="chat-sidebar">
        <button id="chat-new" type="button">+ New chat</button>
        <div id="chat-list"></div>
    </div>
    <div id="chat-main">
        <div id="chat-banner"></div>
        <div id="chat-privacy">
            <span>Tool results - including log lines and configuration - are sent to <strong>OpenRouter</strong>, and on to the provider behind whichever model is selected.</span>
            <button id="chat-privacy-dismiss" type="button">Dismiss</button>
        </div>
        <div id="chat-transcript"></div>
        <div id="chat-composer">
            <textarea id="chat-input" rows="2" placeholder="Ask Predbat... (Enter to send, Shift+Enter for a new line)"></textarea>
            <button id="chat-send" type="button">Send</button>
            <button id="chat-stop" type="button" title="Stops after the current step (the tool call or reply in progress finishes first)">Stop</button>
        </div>
        <div id="chat-footer">
            <span id="chat-model-wrap">
                <select id="chat-model"><option value="">Default model</option></select>
                <span id="chat-model-note"></span>
            </span>
            <label id="chat-ha-state-wrap" for="chat-ha-state-toggle" title="Lets the model read arbitrary Home Assistant entities and history, not just Predbat's own. Off by default; also controls the MCP server.">
                <input type="checkbox" id="chat-ha-state-toggle">
                HA state access
            </label>
            <span id="chat-turn-usage"></span>
            <span id="chat-total-cost"></span>
        </div>
    </div>
</div>
"""


def get_chat_script():
    """Return the Chat tab's client script.

    A raw string throughout: the markdown renderer's regexes carry literal backslash sequences
    (`\\n`, `\\s`, `\\d`, ...) that must reach the browser unchanged, and a non-raw Python string
    would silently turn `\\n` into an actual newline byte, corrupting the regex it sits inside.
    """
    return r"""
function escapeHtml(text) {
    return String(text === null || text === undefined ? '' : text).replace(/[&<>"']/g, function (character) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
}

// Escape first, then transform. Model output and conversation titles are both untrusted text -
// a title is derived from whatever the user or the model wrote - so nothing reaches innerHTML
// before it has been through escapeHtml.
function renderMarkdown(text) {
    var safe = escapeHtml(text);
    safe = safe.replace(/```([\s\S]*?)```/g, function (match, code) { return '<pre><code>' + code + '</code></pre>'; });
    safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    safe = safe.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    safe = safe.replace(/^\s*[-*]\s+(.*)$/gm, '<li>$1</li>');
    safe = safe.replace(/^\s*\d+\.\s+(.*)$/gm, '<li>$1</li>');
    safe = safe.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
    return safe.replace(/\n/g, '<br>');
}

function setTitleText(node, title) {
    node.textContent = title;
}

// ---------------------------------------------------------------------------------------------
// State. Which conversation is being viewed is client state (localStorage), not server state -
// the server's only shared state is the single active turn, broadcast via the busy/idle events.
// ---------------------------------------------------------------------------------------------

var state = { conversation: localStorage.getItem('predbatChatConversation'), cursor: 0, source: null, busy: null, models: [], defaultModel: '', currentModel: null, catalogueAvailable: true };
var toolRows = {};
var confirmCards = {};
var pendingBubble = null;
var pendingText = '';
// The "thinking..." ghost bubble's own state, kept apart from pendingBubble: pendingBubble is the
// real (eventually-rendered) assistant reply, while this is a placeholder shown only while there is
// nothing to render yet - before the first delta, and again between a tool_end and the model's next
// step. thinkingTimer is a setInterval id and must always be cleared through stopThinkingTimer(),
// never left running - see that function for why a leaked interval against a detached element is
// exactly the failure mode this exists to avoid.
var thinkingTimer = null;
var thinkingStartedAtMs = 0;

function byId(id) {
    return document.getElementById(id);
}

function safeJsonPreview(value) {
    try {
        return JSON.stringify(value, null, 2);
    } catch (error) {
        return String(value);
    }
}

function scrollTranscriptToBottom() {
    var transcript = byId('chat-transcript');
    transcript.scrollTop = transcript.scrollHeight;
}

function formatRelativeTime(iso) {
    if (!iso) {
        return '';
    }
    var then = new Date(iso).getTime();
    if (isNaN(then)) {
        return '';
    }
    var deltaSeconds = Math.round((Date.now() - then) / 1000);
    if (deltaSeconds < 60) {
        return 'just now';
    }
    var deltaMinutes = Math.round(deltaSeconds / 60);
    if (deltaMinutes < 60) {
        return deltaMinutes + 'm ago';
    }
    var deltaHours = Math.round(deltaMinutes / 60);
    if (deltaHours < 24) {
        return deltaHours + 'h ago';
    }
    return Math.round(deltaHours / 24) + 'd ago';
}

function formatCost(cost) {
    return '$' + (Number(cost) || 0).toFixed(4);
}

// ---------------------------------------------------------------------------------------------
// Model picker. Model ids and names come from a third-party catalogue (OpenRouter) and are
// untrusted, so every option is built with createElement/textContent - never by concatenating a
// catalogue string into innerHTML. The model is stored per conversation: selecting 'Use default'
// posts an empty id, which the server stores as None so a later apps.yaml change takes effect
// rather than freezing the conversation on today's default.
// ---------------------------------------------------------------------------------------------

function populateModelPicker(models, selectedId) {
    var select = byId('chat-model');
    select.innerHTML = '';
    var defaultOption = document.createElement('option');
    defaultOption.value = '';
    defaultOption.textContent = 'Use default' + (state.defaultModel ? ' (' + state.defaultModel + ')' : '');
    select.appendChild(defaultOption);
    (models || []).forEach(function (model) {
        var option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.name || model.id;
        select.appendChild(option);
    });
    select.value = selectedId || '';
}

function updateModelNote() {
    byId('chat-model-note').textContent = state.catalogueAvailable ? '' : '(catalogue unavailable - only the configured model is offered)';
}

function loadModels() {
    return fetch('./chat/models')
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            state.models = payload.models || [];
            state.defaultModel = payload.default_model || '';
            state.catalogueAvailable = payload.catalogue_available !== false;
            populateModelPicker(state.models, state.currentModel);
            updateModelNote();
        })
        .catch(function (error) { console.error('Failed to load chat models', error); });
}

function changeModel() {
    var id = byId('chat-model').value || null;
    state.currentModel = id;
    if (!state.conversation) {
        return;
    }
    fetch('./chat/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation: state.conversation, id: id })
    }).catch(function (error) { console.error('Failed to set chat model', error); });
}

// ---------------------------------------------------------------------------------------------
// HA state access toggle. Reads and writes switch.predbat_ai_ha_state_enable, which gates
// search_entities/get_entity_state/get_entity_history for every AI surface (chat and MCP alike),
// not just this tab - the footer control is just the one place a user is looking at while
// asking the model something and wondering why it can't see a light switch. loadHaStateStatus()
// is the source of truth for the checkbox on every load/reconnect, so the control never drifts
// from what the gate itself is actually enforcing.
// ---------------------------------------------------------------------------------------------

function loadHaStateStatus() {
    return fetch('./chat/status')
        .then(function (response) { return response.json(); })
        .then(function (payload) { byId('chat-ha-state-toggle').checked = !!payload.ai_ha_state_enabled; })
        .catch(function (error) { console.error('Failed to load HA state access status', error); });
}

function changeHaStateAccess() {
    var toggle = byId('chat-ha-state-toggle');
    var desired = toggle.checked;
    fetch('./chat/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ai_ha_state_enable: desired })
    })
        .then(function (response) { return response.json(); })
        .then(function (payload) { toggle.checked = !!payload.ai_ha_state_enabled; })
        .catch(function (error) {
            console.error('Failed to set HA state access', error);
            // Revert the checkbox rather than leave it showing a state the write never reached.
            toggle.checked = !desired;
        });
}

// ---------------------------------------------------------------------------------------------
// Composer and busy banner. busy/idle are global events - they arrive whatever conversation this
// browser is looking at - so the composer is locked and unlocked here regardless of which
// conversation is currently selected.
// ---------------------------------------------------------------------------------------------

function setComposerDisabled(disabled) {
    byId('chat-input').disabled = disabled;
    byId('chat-send').disabled = disabled;
}

function showBanner(conversationId, title) {
    var banner = byId('chat-banner');
    banner.innerHTML = '';
    banner.appendChild(document.createTextNode("Replying in '"));
    var titleSpan = document.createElement('span');
    setTitleText(titleSpan, title || '');
    banner.appendChild(titleSpan);
    banner.appendChild(document.createTextNode("' - "));
    var link = document.createElement('a');
    link.href = '#';
    link.textContent = 'switch to it';
    link.addEventListener('click', function (event) {
        event.preventDefault();
        selectConversation(conversationId);
    });
    banner.appendChild(link);
    banner.classList.add('visible');
}

function hideBanner() {
    var banner = byId('chat-banner');
    banner.classList.remove('visible');
    banner.innerHTML = '';
}

function setBusy(conversationId, title, turnId) {
    state.busy = { conversation_id: conversationId, title: title, turn_id: turnId };
    setComposerDisabled(true);
    showBanner(conversationId, title);
    byId('chat-stop').classList.add('visible');
}

function setIdle() {
    state.busy = null;
    setComposerDisabled(false);
    hideBanner();
    byId('chat-stop').classList.remove('visible');
}

function stopTurn() {
    if (!state.busy || !state.busy.turn_id) {
        return;
    }
    var button = byId('chat-stop');
    button.disabled = true;
    fetch('./chat/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_id: state.busy.turn_id })
    })
        .catch(function (error) { console.error('Failed to stop the chat turn', error); })
        .then(function () { button.disabled = false; });
}

// ---------------------------------------------------------------------------------------------
// Transcript rendering. User/assistant text goes through renderMarkdown (which escapes first);
// tool arguments, tool results and source titles never touch innerHTML with untrusted text - they
// are built with createElement/textContent instead, which cannot be interpreted as markup.
// ---------------------------------------------------------------------------------------------

function appendBubble(role, text) {
    var bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-' + role;
    bubble.innerHTML = renderMarkdown(text || '');
    byId('chat-transcript').appendChild(bubble);
    scrollTranscriptToBottom();
    return bubble;
}

function appendSources(container, sources) {
    if (!sources || !sources.length) {
        return;
    }
    var wrap = document.createElement('div');
    wrap.className = 'chat-sources';
    wrap.appendChild(document.createTextNode('Sources:'));
    var list = document.createElement('ul');
    sources.forEach(function (source) {
        var item = document.createElement('li');
        var url = String((source && source.url) || '');
        var title = (source && source.title) || url;
        // Only ever build a link for http(s) - a citation url is exactly as untrusted as the page
        // text it was found in, and a javascript: url would run when the link was clicked.
        if (/^https?:\/\//i.test(url)) {
            var link = document.createElement('a');
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = title;
            item.appendChild(link);
        } else {
            item.textContent = title;
        }
        list.appendChild(item);
    });
    wrap.appendChild(list);
    container.appendChild(wrap);
}

function appendToolStart(data) {
    var container = document.createElement('div');
    container.className = 'chat-tool-row';
    var details = document.createElement('details');
    var summary = document.createElement('summary');
    // The tool name is developer-controlled (it is one of Predbat's own registered tools) but is
    // still escaped before going through innerHTML, on the same escape-first principle as
    // everything else that reaches the DOM as markup.
    summary.innerHTML = 'called <code>' + escapeHtml(data.name || '') + '</code>';
    details.appendChild(summary);

    var argsPre = document.createElement('pre');
    var argsCode = document.createElement('code');
    argsCode.textContent = safeJsonPreview(data.arguments);
    argsPre.appendChild(argsCode);
    details.appendChild(argsPre);

    var resultHolder = document.createElement('div');
    resultHolder.className = 'chat-tool-result chat-tool-pending';
    resultHolder.textContent = 'Running...';
    details.appendChild(resultHolder);

    container.appendChild(details);
    byId('chat-transcript').appendChild(container);
    toolRows[data.call_id] = resultHolder;
    scrollTranscriptToBottom();
}

function appendToolEnd(data) {
    var holder = toolRows[data.call_id];
    if (!holder) {
        appendToolStart({ call_id: data.call_id, name: data.name, arguments: {} });
        holder = toolRows[data.call_id];
    }
    holder.textContent = '';
    holder.classList.remove('chat-tool-pending');
    holder.classList.add(data.ok ? 'chat-tool-ok' : 'chat-tool-error');
    var pre = document.createElement('pre');
    var code = document.createElement('code');
    code.textContent = data.preview || '';
    pre.appendChild(code);
    holder.appendChild(pre);
    var elapsed = document.createElement('div');
    elapsed.className = 'chat-tool-elapsed';
    elapsed.textContent = (data.elapsed || 0) + 's';
    holder.appendChild(elapsed);
    scrollTranscriptToBottom();
}

function appendConfirmCard(data) {
    var card = document.createElement('div');
    card.className = 'chat-confirm-card';
    var heading = document.createElement('div');
    heading.className = 'chat-confirm-heading';
    heading.innerHTML = 'Approve <code>' + escapeHtml(data.name || '') + '</code>?';
    card.appendChild(heading);

    var pre = document.createElement('pre');
    var code = document.createElement('code');
    code.textContent = safeJsonPreview(data.arguments);
    pre.appendChild(code);
    card.appendChild(pre);

    var actions = document.createElement('div');
    actions.className = 'chat-confirm-actions';
    var approveButton = document.createElement('button');
    approveButton.type = 'button';
    approveButton.className = 'chat-confirm-approve';
    approveButton.textContent = 'Approve';
    approveButton.addEventListener('click', function () { answerConfirm(data.call_id, true); });
    var rejectButton = document.createElement('button');
    rejectButton.type = 'button';
    rejectButton.className = 'chat-confirm-reject';
    rejectButton.textContent = 'Reject';
    rejectButton.addEventListener('click', function () { answerConfirm(data.call_id, false); });
    actions.appendChild(approveButton);
    actions.appendChild(rejectButton);
    card.appendChild(actions);

    byId('chat-transcript').appendChild(card);
    confirmCards[data.call_id] = card;
    scrollTranscriptToBottom();
}

function answerConfirm(callId, approve) {
    fetch('./chat/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ call_id: callId, conversation: state.conversation, approve: approve })
    }).catch(function (error) { console.error('Failed to answer confirmation', error); });
}

function resolveConfirmCard(data) {
    var card = confirmCards[data.call_id];
    if (!card) {
        return;
    }
    card.innerHTML = '';
    var verb = data.approved ? 'Approved' : 'Rejected';
    var heading = document.createElement('div');
    heading.className = 'chat-confirm-heading';
    heading.innerHTML = verb + ' <code>' + escapeHtml(data.name || '') + '</code>';
    card.appendChild(heading);
    var outcome = document.createElement('div');
    outcome.className = 'chat-confirm-outcome';
    outcome.textContent = data.approved ? 'Approved.' : 'Rejected.';
    card.appendChild(outcome);
    delete confirmCards[data.call_id];
}

function clearPendingBubble() {
    if (pendingBubble && !pendingText) {
        pendingBubble.remove();
    }
    pendingBubble = null;
    pendingText = '';
}

// ---------------------------------------------------------------------------------------------
// "thinking..." ghost bubble. Reuses one #chat-thinking element for the whole page - never one
// per wait - so repeatedly showing and hiding it across a multi-tool-call turn does not jitter
// the transcript by inserting and removing nodes. The element itself is only ever built with
// createElement/textContent/appendChild, so it never needs auditing as an innerHTML sink.
// ---------------------------------------------------------------------------------------------

function ensureThinkingBubble() {
    var bubble = byId('chat-thinking');
    if (bubble) {
        return bubble;
    }
    bubble = document.createElement('div');
    bubble.id = 'chat-thinking';
    bubble.className = 'chat-bubble chat-bubble-assistant chat-bubble-thinking chat-thinking-hidden';
    bubble.appendChild(document.createTextNode('thinking'));
    var caret = document.createElement('span');
    caret.className = 'chat-thinking-caret';
    bubble.appendChild(caret);
    var counter = document.createElement('span');
    counter.className = 'chat-thinking-counter';
    bubble.appendChild(counter);
    return bubble;
}

function showThinkingBubble() {
    var bubble = ensureThinkingBubble();
    if (!bubble.parentNode) {
        byId('chat-transcript').appendChild(bubble);
    }
    bubble.classList.remove('chat-thinking-hidden');
    scrollTranscriptToBottom();
}

function hideThinkingBubble() {
    var bubble = byId('chat-thinking');
    if (bubble) {
        bubble.classList.add('chat-thinking-hidden');
    }
}

function formatThinkingElapsed(totalSeconds) {
    var seconds = Math.max(0, Math.floor(totalSeconds));
    if (seconds < 60) {
        return seconds + 's';
    }
    var minutes = Math.floor(seconds / 60);
    var remainderSeconds = seconds % 60;
    return minutes + 'm ' + remainderSeconds + 's';
}

function tickThinkingTimer() {
    var bubble = byId('chat-thinking');
    var counter = bubble ? bubble.querySelector('.chat-thinking-counter') : null;
    if (!counter) {
        return;
    }
    // Set with textContent, never innerHTML - this value is rebuilt every second, and the sink
    // audit (test_inner_html_sinks_only_ever_receive_escaped_content) allow-lists innerHTML
    // right-hand sides, not this element, so a plain text write is what keeps this out of scope
    // for that audit entirely.
    counter.textContent = ' ' + formatThinkingElapsed((Date.now() - thinkingStartedAtMs) / 1000);
}

// started_at_ms is the wall-clock instant the turn itself began - Date.now() for a turn that just
// started in this browser, or Date.now() minus a server-computed elapsed_seconds offset for one
// resumed from /chat/history after a reload. Either way the counter reads the turn's true total
// elapsed time, never resetting between tool calls - see stopThinkingTimer for why this is the one
// function allowed to start a fresh interval.
function startThinkingTimer(startedAtMs) {
    stopThinkingTimer();
    thinkingStartedAtMs = startedAtMs;
    tickThinkingTimer();
    thinkingTimer = setInterval(tickThinkingTimer, 1000);
}

// The only place that clears thinkingTimer. Must run on every path that stops the counter
// mattering - turn end (done/idle/error) and switching away from the conversation showing it -
// or the interval keeps firing forever against an element that conversation switching has since
// removed from the document (renderHistory() rebuilds #chat-transcript from scratch), ticking
// against nothing for as long as the tab stays open. clearThinkingBubble() below is the usual
// caller; this is exported separately because loadConversationData needs to stop a stale timer
// before it knows yet whether the freshly loaded conversation needs a new one.
function stopThinkingTimer() {
    if (thinkingTimer) {
        clearInterval(thinkingTimer);
        thinkingTimer = null;
    }
}

function clearThinkingBubble() {
    stopThinkingTimer();
    hideThinkingBubble();
}

function handleUser(data) {
    appendBubble('user', data.text || '');
}

function handleDelta(data) {
    hideThinkingBubble();
    if (!pendingBubble) {
        pendingBubble = appendBubble('assistant', '');
        pendingText = '';
    }
    pendingText += data.text || '';
    pendingBubble.innerHTML = renderMarkdown(pendingText);
    scrollTranscriptToBottom();
}

function handleAssistant(data) {
    hideThinkingBubble();
    var text = data.text || '';
    if (!pendingBubble) {
        if (!text && !(data.sources && data.sources.length)) {
            return;
        }
        pendingBubble = appendBubble('assistant', '');
    }
    pendingBubble.innerHTML = renderMarkdown(text);
    appendSources(pendingBubble, data.sources);
    pendingBubble = null;
    pendingText = '';
    scrollTranscriptToBottom();
}

function handleToolEnd(data) {
    appendToolEnd(data);
    // The model still has to look at this result and decide what to do next - show the ghost
    // bubble again so that wait is not silent, exactly like the wait before this tool call was
    // even chosen. If the next thing that arrives is a delta, handleDelta hides it again at once.
    showThinkingBubble();
}

function handleError(data) {
    clearPendingBubble();
    clearThinkingBubble();
    appendBubble('error', data.message || 'Something went wrong');
}

function handleDone() {
    clearPendingBubble();
    clearThinkingBubble();
}

function findConversationRow(id) {
    var rows = byId('chat-list').querySelectorAll('.chat-conv-row');
    for (var index = 0; index < rows.length; index++) {
        if (rows[index].getAttribute('data-id') === id) {
            return rows[index];
        }
    }
    return null;
}

function highlightActiveRow(id) {
    var rows = byId('chat-list').querySelectorAll('.chat-conv-row');
    for (var index = 0; index < rows.length; index++) {
        rows[index].classList.toggle('active', rows[index].getAttribute('data-id') === id);
    }
}

function handleTitle(data) {
    var row = findConversationRow(state.conversation);
    if (row) {
        var titleNode = row.querySelector('.chat-conv-title');
        if (titleNode) {
            setTitleText(titleNode, data.title || '');
        }
    }
    if (state.busy && state.busy.conversation_id === state.conversation) {
        showBanner(state.busy.conversation_id, data.title);
    }
}

function renderUsageEvent(data) {
    byId('chat-turn-usage').textContent = 'This turn: ' + (data.prompt_tokens || 0) + ' in / ' + (data.completion_tokens || 0) + ' out - ' + formatCost(data.cost);
    byId('chat-total-cost').textContent = 'Conversation total: ' + formatCost(data.conversation_cost);
}

function renderConversationTotal(usageTotal) {
    usageTotal = usageTotal || {};
    byId('chat-turn-usage').textContent = '';
    byId('chat-total-cost').textContent = 'Conversation total: ' + formatCost(usageTotal.cost);
}

// ---------------------------------------------------------------------------------------------
// History replay - reconstructs the transcript from the stored message list on first paint or
// after a reload event, in the same shape _run_one_tool() and _turn_loop() append to the store.
// ---------------------------------------------------------------------------------------------

function renderHistory(payload) {
    var transcript = byId('chat-transcript');
    transcript.innerHTML = '';
    toolRows = {};
    confirmCards = {};
    pendingBubble = null;
    pendingText = '';
    (payload.messages || []).forEach(function (message) {
        if (message.role === 'user') {
            appendBubble('user', message.content || '');
        } else if (message.role === 'assistant') {
            if (message.content) {
                appendBubble('assistant', message.content);
            }
            (message.tool_calls || []).forEach(function (call) {
                var name = (call.function || {}).name || '';
                var args = {};
                try {
                    args = JSON.parse((call.function || {}).arguments || '{}');
                } catch (error) {
                    args = {};
                }
                appendToolStart({ call_id: call.id, name: name, arguments: args });
            });
        } else if (message.role === 'tool') {
            var holder = toolRows[message.tool_call_id];
            if (holder) {
                var content = message.content || '';
                var ok = true;
                try {
                    ok = !!JSON.parse(content).success;
                } catch (error) {
                    ok = true;
                }
                appendToolEnd({ call_id: message.tool_call_id, name: message.name, ok: ok, elapsed: 0, preview: content.slice(0, 400) });
            }
        }
    });
    renderConversationTotal(payload.usage_total);
}

// ---------------------------------------------------------------------------------------------
// Streaming and conversation switching.
// ---------------------------------------------------------------------------------------------

function on(source, type, handler) {
    source.addEventListener(type, function (event) {
        // Every SSE frame carries its buffer position as `id:`, delivered here as lastEventId - so
        // state.cursor always reflects what has actually been seen, not just what the URL was
        // opened with. This is what lets a client-driven reconnect (below) resume from the live
        // position instead of replaying the whole buffer.
        if (event.lastEventId) {
            state.cursor = Number(event.lastEventId) || state.cursor;
        }
        handler(event.data ? JSON.parse(event.data) : {});
    });
}

function scheduleReconnect(conversationId) {
    // A short delay before reopening, so a server that is genuinely down does not produce a tight
    // reconnect loop. Re-checked against current state when it fires, in case the browser has since
    // switched conversations or another reconnect already succeeded in the meantime.
    setTimeout(function () {
        if (state.conversation === conversationId && !state.source) {
            openStream();
        }
    }, 1000);
}

function attachConnectionHandling(source) {
    // 'error' is dispatched for two unrelated things on the same EventSource: a genuine
    // `event: error` SSE frame from the server (a chat-turn failure, which arrives as a proper
    // MessageEvent with `data`), and, confusingly, a native browser-level event with no `data` at
    // all when the connection itself drops. Only the first is a chat error to show the user.
    source.addEventListener('error', function (event) {
        if (typeof event.data === 'string') {
            if (event.lastEventId) {
                state.cursor = Number(event.lastEventId) || state.cursor;
            }
            handleError(JSON.parse(event.data));
            return;
        }
        // No data: the connection dropped (a WiFi blip, laptop sleep/wake, a backgrounded tab).
        // readyState === CONNECTING means the browser is about to retry on its own, but it would
        // retry against the exact URL this EventSource was constructed with - a cursor frozen at
        // whatever it was when the stream first opened - and the server only ever reads that query
        // parameter, never the Last-Event-ID header the browser also sends. Left alone, that retry
        // replays every event since the connection first opened, not just the ones actually missed
        // (a replayed tool_start recreates an already-resolved tool row that never receives its
        // tool_end). Taking over here rebuilds the URL from the live, continuously-advanced cursor
        // instead.
        if (source.readyState === EventSource.CONNECTING) {
            source.close();
            if (state.source === source) {
                state.source = null;
            }
            scheduleReconnect(state.conversation);
        }
    });
}

function openStream() {
    if (state.source) {
        state.source.close();
        state.source = null;
    }
    if (!state.conversation) {
        return;
    }
    var source = new EventSource('./chat/stream?conversation=' + encodeURIComponent(state.conversation) + '&cursor=' + state.cursor);
    on(source, 'user', handleUser);
    on(source, 'delta', handleDelta);
    on(source, 'assistant', handleAssistant);
    on(source, 'tool_start', appendToolStart);
    on(source, 'tool_end', handleToolEnd);
    on(source, 'confirm', appendConfirmCard);
    on(source, 'confirm_result', resolveConfirmCard);
    on(source, 'usage', renderUsageEvent);
    on(source, 'title', handleTitle);
    on(source, 'done', handleDone);
    on(source, 'busy', function (data) {
        setBusy(data.conversation_id, data.title, data.turn_id);
        // busy is global - every browser gets it whatever conversation it is looking at - but the
        // ghost bubble belongs in a transcript, so only start/show it when this is the transcript
        // currently on screen. tool_end and delta need no such check: unlike busy/idle, the server
        // already scopes those to this EventSource's own ?conversation= (events_since filters on
        // conversation_id), so they can only ever arrive here for the conversation being viewed.
        if (state.conversation && data.conversation_id === state.conversation) {
            startThinkingTimer(Date.now());
            showThinkingBubble();
        }
    });
    on(source, 'idle', function () {
        setIdle();
        clearThinkingBubble();
    });
    on(source, 'reload', function () { handleReload(); });
    attachConnectionHandling(source);
    state.source = source;
}

function loadConversationData(id) {
    return fetch('./chat/history?conversation=' + encodeURIComponent(id))
        .then(function (response) {
            if (!response.ok) {
                throw new Error('history ' + response.status);
            }
            return response.json();
        })
        .then(function (payload) {
            // Stop whatever the previously viewed conversation's timer was doing before rebuilding
            // the transcript below. renderHistory() wipes #chat-transcript's innerHTML, which
            // detaches any #chat-thinking element still in it - an interval left running past that
            // point ticks forever against a node no longer in the document. This is the one place
            // a plain conversation switch (as opposed to done/idle/error ending the turn itself)
            // needs to stop it explicitly.
            clearThinkingBubble();
            renderHistory(payload);
            state.cursor = payload.cursor || 0;
            state.currentModel = payload.model || null;
            populateModelPicker(state.models, state.currentModel);
            if (payload.active) {
                setBusy(payload.active.conversation_id, payload.active.title, payload.active.turn_id);
                // Only the conversation actually mid-turn gets the ghost bubble in its own
                // transcript. elapsed_seconds is computed server-side at request time from the
                // turn's real start, which is what lets a reload mid-turn resume the counter at its
                // true total instead of restarting it at 0.
                if (payload.active.conversation_id === id) {
                    startThinkingTimer(Date.now() - (Number(payload.active.elapsed_seconds) || 0) * 1000);
                    showThinkingBubble();
                }
            } else {
                setIdle();
            }
            openStream();
        });
}

function selectConversation(id) {
    if (!id) {
        return;
    }
    state.conversation = id;
    try {
        localStorage.setItem('predbatChatConversation', id);
    } catch (error) {
        // Storage unavailable (private browsing, quota) - the conversation just will not survive a reload.
    }
    highlightActiveRow(id);
    loadConversationData(id).catch(function (error) { console.error('Failed to load chat history', error); });
}

function handleReload() {
    if (!state.conversation) {
        return;
    }
    loadConversationData(state.conversation).catch(function (error) { console.error('Failed to reload chat history', error); });
}

// ---------------------------------------------------------------------------------------------
// Conversation list.
// ---------------------------------------------------------------------------------------------

function renderConversationList(conversations) {
    var list = byId('chat-list');
    list.innerHTML = '';
    conversations.forEach(function (meta) {
        var row = document.createElement('div');
        row.className = 'chat-conv-row' + (meta.id === state.conversation ? ' active' : '');
        row.setAttribute('data-id', meta.id);

        var main = document.createElement('div');
        main.className = 'chat-conv-main';
        main.addEventListener('click', function () { selectConversation(meta.id); });

        var titleNode = document.createElement('div');
        titleNode.className = 'chat-conv-title';
        setTitleText(titleNode, meta.title || 'New chat');
        main.appendChild(titleNode);

        var metaNode = document.createElement('div');
        metaNode.className = 'chat-conv-meta';
        var metaText = document.createElement('span');
        metaText.textContent = [formatRelativeTime(meta.updated), formatCost(meta.cost)].join(' - ');
        metaNode.appendChild(metaText);
        if (meta.pending_confirm) {
            var badge = document.createElement('span');
            badge.className = 'chat-pending-badge';
            badge.textContent = 'pending';
            metaNode.appendChild(badge);
        }
        main.appendChild(metaNode);
        row.appendChild(main);

        var actions = document.createElement('div');
        actions.className = 'chat-conv-actions';
        var renameButton = document.createElement('button');
        renameButton.type = 'button';
        renameButton.title = 'Rename';
        renameButton.textContent = String.fromCharCode(9998);
        renameButton.addEventListener('click', function (event) {
            event.stopPropagation();
            renameConversation(meta.id, meta.title);
        });
        var deleteButton = document.createElement('button');
        deleteButton.type = 'button';
        deleteButton.title = 'Delete';
        deleteButton.textContent = String.fromCharCode(10005);
        deleteButton.addEventListener('click', function (event) {
            event.stopPropagation();
            deleteConversation(meta.id);
        });
        actions.appendChild(renameButton);
        actions.appendChild(deleteButton);
        row.appendChild(actions);

        list.appendChild(row);
    });
}

function refreshConversations() {
    fetch('./chat/conversations')
        .then(function (response) { return response.json(); })
        .then(function (payload) { renderConversationList(payload.conversations || []); })
        .catch(function (error) { console.error('Failed to load conversations', error); });
}

function createConversation() {
    fetch('./chat/conversations', { method: 'POST' })
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            refreshConversations();
            if (payload.id) {
                selectConversation(payload.id);
            }
        })
        .catch(function (error) { console.error('Failed to create conversation', error); });
}

function renameConversation(id, currentTitle) {
    var next = window.prompt('Rename conversation', currentTitle || '');
    if (next === null) {
        return;
    }
    fetch('./chat/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, title: next })
    })
        .then(function () { refreshConversations(); })
        .catch(function (error) { console.error('Failed to rename conversation', error); });
}

function deleteConversation(id) {
    if (!window.confirm('Delete this conversation?')) {
        return;
    }
    fetch('./chat/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id })
    })
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            if (payload && payload.error) {
                window.alert(payload.message || payload.error);
                return;
            }
            refreshConversations();
            if (id === state.conversation) {
                state.conversation = null;
                try {
                    localStorage.removeItem('predbatChatConversation');
                } catch (error) {
                    // Storage unavailable - nothing to clean up.
                }
                clearThinkingBubble();
                byId('chat-transcript').innerHTML = '';
                if (state.source) {
                    state.source.close();
                    state.source = null;
                }
            }
        })
        .catch(function (error) { console.error('Failed to delete conversation', error); });
}

// ---------------------------------------------------------------------------------------------
// Composer.
// ---------------------------------------------------------------------------------------------

function doSend(conversationId, text) {
    fetch('./chat/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation: conversationId, message: text })
    })
        .then(function (response) {
            if (response.status === 409) {
                return response.json().then(function (payload) {
                    setBusy(payload.conversation_id, payload.title, payload.turn_id);
                    throw new Error('busy');
                });
            }
            if (!response.ok) {
                throw new Error('send ' + response.status);
            }
            return response.json();
        })
        .then(function () { refreshConversations(); })
        .catch(function (error) {
            if (!error || error.message !== 'busy') {
                console.error('Failed to send message', error);
            }
        });
}

function createAndSend(text) {
    fetch('./chat/conversations', { method: 'POST' })
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            selectConversation(payload.id);
            doSend(payload.id, text);
            refreshConversations();
        })
        .catch(function (error) { console.error('Failed to start conversation', error); });
}

function sendMessage() {
    var input = byId('chat-input');
    var text = input.value.trim();
    if (!text) {
        return;
    }
    input.value = '';
    if (!state.conversation) {
        createAndSend(text);
        return;
    }
    doSend(state.conversation, text);
}

// ---------------------------------------------------------------------------------------------
// Startup.
// ---------------------------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', function () {
    byId('chat-new').addEventListener('click', createConversation);
    byId('chat-send').addEventListener('click', sendMessage);
    byId('chat-stop').addEventListener('click', stopTurn);
    byId('chat-model').addEventListener('change', changeModel);
    byId('chat-ha-state-toggle').addEventListener('change', changeHaStateAccess);
    byId('chat-input').addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            sendMessage();
        }
    });

    var dismiss = byId('chat-privacy-dismiss');
    dismiss.addEventListener('click', function () {
        byId('chat-privacy').classList.add('dismissed');
        try {
            localStorage.setItem('predbatChatPrivacyDismissed', '1');
        } catch (error) {
            // Storage unavailable - the banner will simply reappear next visit.
        }
    });
    try {
        if (localStorage.getItem('predbatChatPrivacyDismissed') === '1') {
            byId('chat-privacy').classList.add('dismissed');
        }
    } catch (error) {
        // Storage unavailable - leave the banner showing.
    }

    loadModels();
    loadHaStateStatus();
    refreshConversations();
    if (state.conversation) {
        selectConversation(state.conversation);
    }
});
"""
