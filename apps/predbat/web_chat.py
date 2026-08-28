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

# The switches the Chat tab footer shows and may toggle, mapped to the default get_ha_config falls
# back to. This doubles as the allowlist for the POST half of /chat/status: an unlisted name is
# rejected rather than interpolated into an entity id, so the route cannot be used to flip
# arbitrary Predbat switches. Defaults must match the CONFIG_ITEMS entries in config.py - a
# mismatch would make the footer show a state the gate itself disagrees with before the first
# write lands.
CHAT_STATUS_SWITCHES = {
    "chat_confirm_writes": True,
    "chat_web_search": False,
    "ai_ha_state_enable": True,
}


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
        """Render the Chat tab, or the setup page when the component is not configured.

        The tab is always in the nav now, so this is the page a user lands on before they have an
        API key. A JSON 404 is the right answer for the data routes the script calls, but a
        person typing /chat into a browser should be told what to do about it, not shown a error.
        """
        agent = self.agent
        if agent is None:
            text = self.web.get_header("Predbat Chat")
            text += get_chat_styles()
            text += get_chat_setup_body()
            text += "</body></html>"
            return web.Response(content_type="text/html", text=text)
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
        return web.json_response(
            {
                "id": cid,
                "title": meta.get("title"),
                "model": meta.get("model"),
                "usage_total": meta.get("usage_total"),
                # The most recent completion's prompt_tokens, not the cumulative usage_total.prompt_tokens
                # - see ConversationStore.add_usage() and the Chat tab's context-size footer
                # (renderContextUsage() in get_chat_script()), which is what actually reads this.
                "last_prompt_tokens": meta.get("last_prompt_tokens", 0),
                "messages": messages or [],
                # Carried separately from messages on purpose: the last failed turn is shown in
                # the transcript but is not part of the conversation, and must never be replayed
                # to the model. See ChatAgent._report_turn_error().
                "last_error": agent.store.get_last_error(cid),
                # Every write approval this conversation has asked for, and what was answered.
                # Carried separately from messages: an approval is a record of a decision, not
                # something the model said, and it must never be replayed. The pending ones are
                # what a reconnecting client needs to get its card back.
                "approvals": agent.store.get_approvals(cid),
                "cursor": cursor,
                "active": self._active_with_elapsed(agent),
            }
        )

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
        # A turn parked on a confirmation is not in the loop that reads the deadline, so the
        # deadline alone cannot stop it - see ChatAgent.await_confirmation().
        agent.stop_requested = turn_id
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
        return web.json_response({"models": models, "default_model": agent.default_model, "selected_model": agent.store.get_selected_model(), "catalogue_available": len(models) > 1})

    async def html_chat_model(self, request):
        """Set the model for one conversation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        meta, error = self._conversation_or_404(agent, body.get("conversation"))
        if error:
            return error
        model_id = body.get("id") or None
        agent.store.set_model(body.get("conversation"), model_id)
        # Also remembered as the global choice, so the next new conversation starts on it and it
        # survives a restart. Only a real selection is remembered - clearing back to the default
        # should not pin whatever the default happened to be at that moment.
        if model_id:
            agent.store.set_selected_model(model_id)
        await agent.run_on_agent_loop(agent.store.flush(body.get("conversation")))
        return web.json_response({"ok": True})

    async def html_chat_status(self, request):
        """Return the AI-surface switch states the Chat tab footer shows live.

        Every switch in CHAT_STATUS_SWITCHES is read the same way its own gate reads it
        (base.get_ha_config, not cached) so a footer control can never show something the gate
        would disagree with. Keyed by the real switch name rather than a prettified label: the
        POST below takes the same names back, so one spelling covers both directions.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        switches = {}
        for name, default in CHAT_STATUS_SWITCHES.items():
            enabled, _ = self.base.get_ha_config(name, default)
            switches[name] = bool(enabled)
        return web.json_response({"switches": switches})

    async def html_chat_status_post(self, request):
        """Toggle one of the Chat tab's footer switches.

        Writes through ha_interface.set_state_external(), the same mechanism the dashboard's own
        switch toggles (html_dash_post) and the set_config tool both use: it is dispatched as a
        real turn_on/turn_off service call against the matching CONFIG_ITEMS entry, not a raw
        state overwrite. /api/state - the JSON API other switch-like external clients use - only
        writes the raw HA entity state and never updates the matching CONFIG_ITEMS value, so it
        would silently fail to change what get_ha_config(name, ...) actually returns;
        set_state_external is what genuinely flips the switch.

        `name` is checked against CHAT_STATUS_SWITCHES before it is interpolated into an entity
        id. Without that check this route would toggle ANY Predbat switch whose name a caller
        could guess - the footer needs three, so it is allowed exactly three.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        name = body.get("name")
        if name not in CHAT_STATUS_SWITCHES:
            return web.json_response({"error": "Unknown switch"}, status=400)
        enabled = bool(body.get("enabled"))
        entity_id = "switch.{}_{}".format(self.base.prefix, name)
        await self.base.ha_interface.set_state_external(entity_id, enabled)
        return web.json_response({"ok": True, "name": name, "enabled": enabled})


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

/* The Chat tab is an app-like view: it fills the window exactly, the document itself never
   scrolls, and the only thing that scrolls is the transcript. Scoped safely to this page because
   get_chat_styles() is only emitted here.

   Done with flex rather than a measured or calculated height. Both of those need to know how tall
   the header is, and it is not a fixed height - an apps.yaml error banner adds a line, and the
   version string wraps on a narrow window. Every attempt to guess or measure it got this wrong in
   one direction or the other: too small left dead space under the footer, too large pushed the
   page past the viewport so the document scrolled and the nav went off the top. Making body a
   flex column and letting #chat-page take the remainder means nothing has to know, and the
   browser recomputes it on every reflow for free.

   The 10px is body's own 5px margin, top and bottom, from the global stylesheet - without it the
   page is exactly one margin too tall and the document scrolls by that much. */
html {
    /* body's overflow does not govern the document: if anything is wider than the window, html
       grows a horizontal scrollbar, and that bar eats ~15px of HEIGHT - which is what pushed the
       composer and footer off the bottom even though the height arithmetic below was right.
       Nothing here should ever need to scroll horizontally, so nothing is allowed to. */
    overflow: hidden;
}

body {
    display: flex;
    flex-direction: column;
    /* border-box is what makes this height correct. The menu bar is position: fixed, so the
       global stylesheet gives body padding-top: 65px to clear it - and under the default
       content-box that padding is ADDED to the height here, making the page 65px taller than the
       window. That is what pushed the composer and Send button off the bottom, and no amount of
       adjusting the number would have fixed it while the padding sat outside. The 10px is body's
       own 5px margins, which stay outside the box either way. */
    box-sizing: border-box;
    height: calc(100vh - 10px);
    /* Belt and braces alongside html above: this stops a stray wide child scrolling the page
       body, html stops it scrolling the document. */
    overflow: hidden;
}

/* Predbat's global stylesheet sets `p { white-space: nowrap }` for its data tables. That applies
   to every paragraph in the app, including the ones the markdown renderer produces for chat
   replies - and white-space: nowrap forbids wrapping outright, so no amount of overflow-wrap can
   act on it. This is why the model's answers ran off the right of their bubble however the
   bubbles were sized: the text was never allowed to wrap in the first place. Restored to normal
   for chat content only, leaving the tables that rule exists for alone. */
#chat-page p,
#chat-page li,
#chat-page td,
#chat-page th {
    white-space: normal;
}

#chat-page {
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    gap: 14px;
    /* Takes whatever the header leaves. min-height: 0 is load-bearing, not tidiness: a flex item
       defaults to min-height auto, which refuses to shrink below its content, so without it the
       grid's own content would push the page past the viewport and reintroduce the outer
       scrollbar this whole rule exists to remove. */
    flex: 1 1 auto;
    min-height: 0;
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
    /* min-width: 0 lets it shrink below its content; max-width stops it growing past its grid
       column. Both are needed: the first permits shrinking, the second forbids growing, and a
       flex or grid child will happily do the latter on the strength of one long line. */
    min-width: 0;
    max-width: 100%;
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
    max-width: 100%;
    overflow-y: auto;
    /* The transcript scrolls vertically only. Anything genuinely too wide - a code block, a wide
       table - now scrolls within itself, so a horizontal bar out here means something escaped its
       box rather than that the user needs to pan the conversation. */
    overflow-x: hidden;
    border: 1px solid var(--chat-border);
    border-radius: 6px;
    padding: 12px;
    background: var(--chat-panel-bg);
}

.chat-bubble {
    /* 92%, not 80%: the model's answers are full of long identifiers and the narrower bubble left
       a third of the window empty while its own text needed the space. Safe only now that the
       rules below actually keep content inside the box. */
    max-width: 92%;
    /* border-box, or the 24px of horizontal padding lands OUTSIDE the 92% and the bubble is
       wider than it claims to be. */
    box-sizing: border-box;
    margin: 6px 0;
    padding: 8px 12px;
    border-radius: 8px;
    /* The bubbles were inheriting body's default 16px while the tool rows around them sit at
       11-13px, which made the conversation look oversized against its own tool output. */
    font-size: 14px;
    line-height: 1.45;
    /* anywhere, not break-word: break-word will not break a token that is the sole content of a
       line, which is exactly the case here - "my_predbat.car_charging_planned[car_n]" and the
       like have no spaces to break at, so they ran straight out of the bubble. */
    overflow-wrap: anywhere;
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

/* Retry status text - reason, attempt count and countdown - shown inside the thinking bubble
   while a completion is being retried. Hidden by default; .chat-thinking-retrying (toggled by
   startRetryCountdown()/stopRetryCountdown()) swaps it in and hides the ordinary caret/counter
   underneath, so the two never show at once. No colour of its own: it inherits
   .chat-bubble-thinking's var(--chat-text-muted) above rather than declaring one, which is what
   keeps it theme-aware for free. */
.chat-thinking-retry {
    display: none;
}

.chat-bubble-thinking.chat-thinking-retrying .chat-thinking-caret,
.chat-bubble-thinking.chat-thinking-retrying .chat-thinking-counter {
    display: none;
}

.chat-bubble-thinking.chat-thinking-retrying .chat-thinking-retry {
    display: inline;
}


/* Tool output and approval arguments wrap rather than scrolling sideways. A <pre> does not wrap
   by default, so a one-line JSON preview - which is what every tool returns - showed a single
   line with a scrollbar under it and the rest of the text simply unreachable. Code blocks inside
   an assistant bubble keep horizontal scrolling, where preserving the author's line breaks
   matters more. */
.chat-tool-result pre,
.chat-confirm-card pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 320px;
    overflow-y: auto;
}

.chat-bubble pre,
.chat-tool-row pre,
.chat-confirm-card pre {
    background: var(--chat-code-bg);
    padding: 8px;
    border-radius: 4px;
    overflow-x: auto;
    /* Without the cap, overflow-x: auto never engages: the pre simply grows to fit its longest
       line and pushes the bubble out with it, which is what put a horizontal scrollbar across
       the whole transcript instead of inside the code block where it belongs. */
    max-width: 100%;
    margin: 6px 0 0 0;
}

.chat-bubble code {
    background: var(--chat-code-bg);
    padding: 1px 4px;
    border-radius: 3px;
    overflow-wrap: anywhere;
}

.chat-bubble p {
    margin: 0 0 8px 0;
}

.chat-bubble p:last-child {
    margin-bottom: 0;
}

.chat-bubble h1,
.chat-bubble h2,
.chat-bubble h3,
.chat-bubble h4,
.chat-bubble h5,
.chat-bubble h6 {
    margin: 10px 0 6px 0;
    line-height: 1.25;
    color: var(--chat-text);
}

.chat-bubble h1 {
    font-size: 1.35em;
}

.chat-bubble h2 {
    font-size: 1.2em;
}

.chat-bubble h3 {
    font-size: 1.1em;
}

.chat-bubble h4,
.chat-bubble h5,
.chat-bubble h6 {
    font-size: 1em;
}

.chat-bubble ul,
.chat-bubble ol {
    margin: 4px 0 8px 20px;
    padding: 0;
}

.chat-bubble li {
    margin: 2px 0;
}

/* Tables need their own horizontal scroll container - a wide table (many columns, long cell
   text) must scroll within the bubble rather than stretching #chat-transcript's layout. */
.chat-table-wrap {
    overflow-x: auto;
    margin: 6px 0 8px 0;
}

.chat-bubble table {
    border-collapse: collapse;
    /* display: block is what lets a table scroll: as a table it sizes to its content and drags
       the bubble wider however narrow the window gets. */
    display: block;
    max-width: 100%;
    overflow-x: auto;
}

.chat-bubble th,
.chat-bubble td {
    border: 1px solid var(--chat-border);
    padding: 4px 8px;
    text-align: left;
}

.chat-bubble thead th {
    background: var(--chat-code-bg);
    font-weight: 600;
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

.chat-tool-status {
    display: inline-block;
    width: 1.1em;
    margin-right: 4px;
    font-weight: 700;
}

.chat-tool-status-pending {
    color: var(--chat-text-muted);
}

.chat-tool-status-ok {
    color: var(--chat-accent);
}

.chat-tool-status-error {
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

#chat-model-wrap {
    position: relative;
}

#chat-model-list {
    display: none;
    position: absolute;
    bottom: 100%;
    left: 0;
    z-index: 20;
    min-width: 320px;
    max-height: 320px;
    overflow-y: auto;
    background: var(--chat-input-bg);
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    margin-bottom: 4px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
}

.chat-model-free-only {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    cursor: pointer;
    user-select: none;
    border-bottom: 1px solid var(--chat-border);
    position: sticky;
    top: 0;
    background: var(--chat-input-bg);
    color: var(--chat-text-muted);
}

.chat-model-option {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 5px 10px;
    cursor: pointer;
    white-space: nowrap;
}

.chat-model-option:hover {
    background: var(--chat-border);
}

.chat-model-current .chat-model-name {
    font-weight: 600;
}

.chat-model-meta {
    color: var(--chat-text-muted);
}

.chat-model-empty {
    padding: 5px 10px;
    color: var(--chat-text-muted);
    font-style: italic;
}

#chat-model-note {
    margin-left: 6px;
    font-style: italic;
}

/* The three permission toggles share a wrap so they stay together as one group when the footer's
   space-between pushes the usage and cost readouts to the right. flex-wrap lets them fold onto a
   second line on a narrow window rather than squeezing the model picker. */
#chat-toggles {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px 12px;
}

#chat-setup {
    max-width: 680px;
    margin: 24px auto;
    padding: 0 16px;
    line-height: 1.5;
}

#chat-setup pre {
    background: var(--chat-input-bg);
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    padding: 10px 12px;
    overflow-x: auto;
}

#chat-setup .chat-setup-note {
    color: var(--chat-text-muted);
    font-size: 13px;
    border-top: 1px solid var(--chat-border);
    padding-top: 12px;
}

.chat-approval-badge {
    margin-left: 8px;
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--chat-border);
    color: var(--chat-text-muted);
}

.chat-approval-approved {
    color: #3aee85;
}

.chat-approval-rejected {
    color: #ff6b6b;
}

.chat-approval-record {
    margin: 6px 0;
    padding: 6px 10px;
    border-left: 3px solid var(--chat-border);
    font-size: 12px;
    color: var(--chat-text-muted);
}

.chat-approval-record pre {
    margin: 4px 0 0;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow-y: auto;
}

.chat-error-detail {
    margin-top: 6px;
    font-size: 12px;
}

.chat-error-detail summary {
    cursor: pointer;
    opacity: 0.85;
}

.chat-error-detail pre {
    margin: 6px 0 0;
    padding: 6px 8px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 240px;
    overflow-y: auto;
    background: rgba(0, 0, 0, 0.25);
    border-radius: 4px;
}

.chat-toggle-cost {
    color: var(--chat-text-muted);
}

.chat-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
}
</style>
"""


def get_chat_setup_body():
    """Return the Chat tab's setup page, shown when the component is not configured.

    Deliberately concrete: the exact key, an example value, and where the file lives, because a
    user who reaches this page has no other clue what is missing. The model is described as
    optional because it genuinely is - the picker can set it once the key is in place.
    """
    return """
<div id="chat-page">
    <div id="chat-setup">
        <h2>Chat is not set up yet</h2>
        <p>
            The Chat tab needs an <a href="https://openrouter.ai" target="_blank" rel="noopener">OpenRouter</a>
            API key. Add one to the <code>pred_bat:</code> section of your <code>apps.yaml</code>:
        </p>
        <pre><code>pred_bat:
  openrouter_api_key: 'sk-or-v1-...'</code></pre>
        <p>
            Predbat restarts automatically when <code>apps.yaml</code> is saved, and the Chat tab
            becomes usable once it has. You can edit the file from the
            <a href="./apps">apps.yaml</a> tab.
        </p>
        <p>
            You do not need to choose a model here. Once the key is set you can pick one from the
            search box below the message box, and Predbat remembers it. To pin a default instead,
            set <code>openrouter_default_model</code> (for example
            <code>openai/gpt-4o-mini</code>) alongside the key.
        </p>
        <p class="chat-setup-note">
            Chat sends tool results - including log lines and configuration - to OpenRouter and on
            to the provider behind the model you choose. Credentials in <code>apps.yaml</code> are
            redacted before they leave Predbat.
        </p>
    </div>
</div>
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
                <input type="text" id="chat-model" autocomplete="off" spellcheck="false" placeholder="Choose a model">
                <div id="chat-model-list"></div>
                <span id="chat-model-note"></span>
            </span>
            <span id="chat-toggles">
                <label class="chat-toggle" for="chat-confirm-writes-toggle" title="Ask before the model changes any Predbat setting or apps.yaml key. Turning this off lets it write without stopping to ask you.">
                    <input type="checkbox" id="chat-confirm-writes-toggle" data-switch="chat_confirm_writes">
                    Confirm writes
                </label>
                <label class="chat-toggle" for="chat-web-search-toggle" title="Costs extra: billed per request through OpenRouter, on top of the model's own cost. Off by default. Predbat's own documentation search does not need this and always works.">
                    <input type="checkbox" id="chat-web-search-toggle" data-switch="chat_web_search">
                    Web search <span class="chat-toggle-cost">(costs extra)</span>
                </label>
                <label class="chat-toggle" for="chat-ha-state-toggle" title="Lets the model read arbitrary Home Assistant entities and history, not just Predbat's own. On by default; also controls the MCP server, so turning it off closes those tools there too.">
                    <input type="checkbox" id="chat-ha-state-toggle" data-switch="ai_ha_state_enable">
                    HA state access
                </label>
            </span>
            <span id="chat-turn-usage"></span>
            <span id="chat-context-usage"></span>
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
//
// renderMarkdown itself is a line-based block parser: it walks the escaped text grouping
// consecutive lines into blocks (fenced code, headings, tables, lists, paragraphs) before any
// inline rule or newline-to-<br> conversion runs. That grouping is the point - a single blanket
// "\n -> <br>" pass over the whole string (the previous implementation) cannot tell a blank line
// between two list items from a blank line between two paragraphs, and cannot represent "these
// three lines are one list" at all, so it fought every block-level construct it tried to render.
// Each block below owns its own line-joining instead, and inline rules (renderInline) apply only
// within a block's own content, never across a block boundary.
function renderInline(text) {
    var safe = text;
    safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    safe = safe.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return safe;
}

function isFenceLine(line) {
    return /^\s*```/.test(line);
}

function matchHeadingLine(line) {
    return line.match(/^(#{1,6})\s+(.*)$/);
}

function matchUnorderedItem(line) {
    return line.match(/^\s*[-*]\s+(.*)$/);
}

function matchOrderedItem(line) {
    return line.match(/^\s*\d+\.\s+(.*)$/);
}

// Split one table row into its cell texts, stripping a single optional leading/trailing pipe -
// the model's own tables carry both (`| Time | SOC |`), but a bare `a | b` row is honoured too.
function splitTableRow(line) {
    var trimmed = line.trim();
    if (trimmed.charAt(0) === '|') {
        trimmed = trimmed.slice(1);
    }
    if (trimmed.length && trimmed.charAt(trimmed.length - 1) === '|') {
        trimmed = trimmed.slice(0, -1);
    }
    return trimmed.split('|');
}

// Whether `line` is a GFM table separator row (`|---|:---:|---:|`), returning one alignment per
// cell ('', 'left', 'right' or 'center') or null if it is not a valid separator - which is also
// how a pipe line that is *not* followed by one stays a paragraph instead of becoming a table.
function tableSeparatorAlignments(line) {
    if (line.indexOf('-') < 0) {
        return null;
    }
    var cells = splitTableRow(line);
    var alignments = [];
    for (var i = 0; i < cells.length; i++) {
        var cell = cells[i].trim();
        if (!/^:?-+:?$/.test(cell)) {
            return null;
        }
        var left = cell.charAt(0) === ':';
        var right = cell.charAt(cell.length - 1) === ':';
        alignments.push(left && right ? 'center' : right ? 'right' : left ? 'left' : '');
    }
    return alignments;
}

function tableCellStyle(alignment) {
    return alignment ? ' style="text-align:' + alignment + '"' : '';
}

function buildTableRowHtml(cells, alignments, tag) {
    var html = '<tr>';
    for (var i = 0; i < cells.length; i++) {
        html += '<' + tag + tableCellStyle(alignments[i]) + '>' + renderInline((cells[i] || '').trim()) + '</' + tag + '>';
    }
    return html + '</tr>';
}

function buildTableHtml(headerCells, alignments, bodyRows) {
    var html = '<div class="chat-table-wrap"><table><thead>' + buildTableRowHtml(headerCells, alignments, 'th') + '</thead><tbody>';
    for (var i = 0; i < bodyRows.length; i++) {
        html += buildTableRowHtml(bodyRows[i], alignments, 'td');
    }
    return html + '</tbody></table></div>';
}

function renderMarkdown(text) {
    var lines = escapeHtml(text).split('\n');
    var html = '';
    var index = 0;
    while (index < lines.length) {
        var line = lines[index];

        // Fenced code: literal until the closing fence (or end of input) - no inline rule and no
        // line-splitting runs inside it, so a table or **bold** marker inside a code block stays
        // exactly as written.
        if (isFenceLine(line)) {
            var codeLines = [];
            index += 1;
            while (index < lines.length && !isFenceLine(lines[index])) {
                codeLines.push(lines[index]);
                index += 1;
            }
            if (index < lines.length) {
                index += 1;
            }
            html += '<pre><code>' + codeLines.join('\n') + '</code></pre>';
            continue;
        }

        var heading = matchHeadingLine(line);
        if (heading) {
            var level = heading[1].length;
            html += '<h' + level + '>' + renderInline(heading[2]) + '</h' + level + '>';
            index += 1;
            continue;
        }

        // Table: a header row followed by a valid separator row. A pipe line with no valid
        // separator underneath it falls through to the paragraph branch below unchanged.
        if (line.indexOf('|') >= 0 && index + 1 < lines.length) {
            var alignments = tableSeparatorAlignments(lines[index + 1]);
            if (alignments) {
                var headerCells = splitTableRow(line);
                var bodyRows = [];
                index += 2;
                while (index < lines.length && lines[index].indexOf('|') >= 0) {
                    bodyRows.push(splitTableRow(lines[index]));
                    index += 1;
                }
                html += buildTableHtml(headerCells, alignments, bodyRows);
                continue;
            }
        }

        // Lists: every consecutive matching line joins the same <ul>/<ol> - never one list
        // element per item - so there is no <br> and no per-item margin gap between bullets.
        if (matchUnorderedItem(line)) {
            var unorderedItems = [];
            var unorderedMatch;
            while (index < lines.length && (unorderedMatch = matchUnorderedItem(lines[index]))) {
                unorderedItems.push('<li>' + renderInline(unorderedMatch[1]) + '</li>');
                index += 1;
            }
            html += '<ul>' + unorderedItems.join('') + '</ul>';
            continue;
        }

        if (matchOrderedItem(line)) {
            var orderedItems = [];
            var orderedMatch;
            while (index < lines.length && (orderedMatch = matchOrderedItem(lines[index]))) {
                orderedItems.push('<li>' + renderInline(orderedMatch[1]) + '</li>');
                index += 1;
            }
            html += '<ol>' + orderedItems.join('') + '</ol>';
            continue;
        }

        if (line.trim() === '') {
            index += 1;
            continue;
        }

        // Paragraph: everything else. Consecutive plain lines join with <br> - but only within
        // this paragraph; the loop stops the moment the next line starts a different block, so
        // <br> never leaks across a block boundary the way the old blanket newline pass did.
        var paragraphLines = [];
        while (index < lines.length) {
            var current = lines[index];
            if (current.trim() === '' || isFenceLine(current) || matchHeadingLine(current) || matchUnorderedItem(current) || matchOrderedItem(current)) {
                break;
            }
            if (current.indexOf('|') >= 0 && index + 1 < lines.length && tableSeparatorAlignments(lines[index + 1])) {
                break;
            }
            paragraphLines.push(current);
            index += 1;
        }
        html += '<p>' + renderInline(paragraphLines.join('\n')).replace(/\n/g, '<br>') + '</p>';
    }
    return html;
}

function setTitleText(node, title) {
    node.textContent = title;
}

// ---------------------------------------------------------------------------------------------
// State. Which conversation is being viewed is client state (localStorage), not server state -
// the server's only shared state is the single active turn, broadcast via the busy/idle events.
// ---------------------------------------------------------------------------------------------

var state = { freeOnly: readFreeOnly(), conversation: localStorage.getItem('predbatChatConversation'), cursor: 0, source: null, busy: null, models: [], defaultModel: '', selectedModel: '', currentModel: null, catalogueAvailable: true };
var toolRows = {};
// Per tool call, keyed by call id: the summary element (so an approval badge can be attached),
// and the status marker. Declared here beside toolRows rather than next to the functions that
// use them - var hoisting would make the uses work either way, but only because the whole script
// runs before any handler fires, which is not a property worth depending on.
var toolSummaries = {};
var toolStatuses = {};
var TOOL_STATUS_PENDING = '\u25cf';
var TOOL_STATUS_OK = '\u2713';
var TOOL_STATUS_ERROR = '\u2717';
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
// The retry countdown's own interval, deliberately separate from thinkingTimer above - the two
// tick different displays inside the same bubble (total elapsed time vs. time until the next
// attempt) and must be started/stopped independently, or clearing one would silently leak the
// other. See stopRetryCountdown() for why this must always be nulled out after clearing.
var retryCountdownTimer = null;

function readFreeOnly() {
    // Defaults to on: the free models are the ones a user can experiment with at no cost, and the
    // full catalogue runs to several hundred entries where the paid ones dominate. Remembered per
    // browser, and any storage failure just means the default applies again next time.
    try {
        return localStorage.getItem('predbatChatFreeOnly') !== '0';
    } catch (error) {
        return true;
    }
}

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

// The model picker is a filter box over a list, not a <select>. OpenRouter's tool-capable
// catalogue runs to several hundred entries, which a native dropdown renders as one enormous
// scrolling column with no way to search it beyond type-ahead on the first character.
//
// The input shows the model in force when idle and becomes a search box on focus. state.models is
// the catalogue; the list is rebuilt from it on every keystroke, capped at MODEL_RESULTS_MAX so a
// cleared box does not lay out hundreds of rows.
// ---------------------------------------------------------------------------------------------

var MODEL_RESULTS_MAX = 60;

function trimTrailingZeros(text) {
    // Only meaningful after a decimal point: stripping zeros from "10" would give "1".
    if (text.indexOf('.') === -1) {
        return text;
    }
    return text.replace(/0+$/, '').replace(/\.$/, '');
}

function formatModelPrice(model) {
    // OpenRouter quotes pricing as a string of US dollars PER TOKEN ("0.000002"), which is
    // unreadable at that scale - every model would show as $0.00. Scaled to the per-million-token
    // figure everyone actually quotes, so $2/$10 means $2 per million in, $10 per million out.
    // Returns null when the catalogue gave no price, which is the case for the apps.yaml default
    // entry when the catalogue could not be fetched.
    if (model.prompt_price === null || model.prompt_price === undefined) {
        return null;
    }
    var inPrice = Number(model.prompt_price) * 1000000;
    var outPrice = Number(model.completion_price) * 1000000;
    if (!isFinite(inPrice) || !isFinite(outPrice)) {
        return null;
    }
    if (inPrice === 0 && outPrice === 0) {
        return 'free';
    }
    // OpenRouter quotes -1 for its routing models (openrouter/auto, fusion, ...), where the price
    // depends on whichever model the request is actually routed to and so is not known up front.
    // Rendering the arithmetic gives "$-1000000", which is worse than saying nothing.
    if (inPrice < 0 || outPrice < 0) {
        return 'varies';
    }
    return '$' + formatPricePart(inPrice) + '/$' + formatPricePart(outPrice);
}

function formatPricePart(perMillion) {
    // Enough precision to tell cheap models apart without four decimals on expensive ones: the
    // catalogue spans $0.02 to $75 per million, so a fixed number of decimals reads badly at one
    // end or the other.
    if (perMillion >= 10) {
        return perMillion.toFixed(0);
    }
    if (perMillion >= 1) {
        return trimTrailingZeros(perMillion.toFixed(2));
    }
    return trimTrailingZeros(perMillion.toFixed(3));
}

function modelLabel(id) {
    if (!id) {
        return state.defaultModel ? 'Default (' + state.defaultModel + ')' : 'Choose a model';
    }
    var match = (state.models || []).filter(function (model) { return model.id === id; })[0];
    return match ? (match.name || match.id) : id;
}

function effectiveModel() {
    return state.currentModel || state.selectedModel || state.defaultModel || '';
}

function updateModelNote() {
    var note = byId('chat-model-note');
    if (!state.catalogueAvailable) {
        note.textContent = '(catalogue unavailable - only the configured model is offered)';
    } else if (!effectiveModel()) {
        note.textContent = 'Pick a model to start';
    } else {
        note.textContent = '';
    }
}

function isFreeModel(model) {
    // Free means the catalogue quotes zero for both directions, which is what formatModelPrice
    // already reduces to. The routing models, which quote -1 because their cost depends on where
    // they route, are not free and must not be offered as if they were.
    return formatModelPrice(model) === 'free';
}

function appendFreeOnlyRow(list) {
    var label = document.createElement('label');
    label.className = 'chat-model-free-only';
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = state.freeOnly;
    label.appendChild(box);
    label.appendChild(document.createTextNode('Show only free models'));
    // mousedown, and preventDefault, for the same reason the result rows use it: clicking here
    // would otherwise blur the search box, whose blur handler closes the list before the click
    // lands. preventDefault also stops the browser toggling the box itself, so the state is
    // flipped here and the list re-rendered from it.
    label.addEventListener('mousedown', function (event) {
        event.preventDefault();
        state.freeOnly = !state.freeOnly;
        try {
            localStorage.setItem('predbatChatFreeOnly', state.freeOnly ? '1' : '0');
        } catch (error) {
            // A browser with site data blocked still gets a working filter, just not a
            // remembered one.
        }
        renderModelResults(byId('chat-model').value);
    });
    list.appendChild(label);
}

function renderModelResults(filter) {
    var list = byId('chat-model-list');
    list.innerHTML = '';
    appendFreeOnlyRow(list);
    var needle = (filter || '').toLowerCase();
    var matches = (state.models || []).filter(function (model) {
        // The model in use stays listed whatever the filter says, so the picker never looks as
        // though it has lost the thing it is currently set to.
        if (state.freeOnly && !isFreeModel(model) && model.id !== effectiveModel()) {
            return false;
        }
        if (!needle) {
            return true;
        }
        return (model.id || '').toLowerCase().indexOf(needle) !== -1 || (model.name || '').toLowerCase().indexOf(needle) !== -1;
    });

    if (state.defaultModel && !needle) {
        // Always offered: it is the apps.yaml setting rather than a catalogue entry, so the price
        // filter has nothing to say about it.
        matches = [{ id: '', name: 'Default (' + state.defaultModel + ')' }].concat(matches);
    }

    var shown = matches.slice(0, MODEL_RESULTS_MAX);
    shown.forEach(function (model) {
        var row = document.createElement('div');
        row.className = 'chat-model-option';
        row.setAttribute('data-id', model.id);
        if (model.id === effectiveModel()) {
            row.className += ' chat-model-current';
        }
        var name = document.createElement('span');
        name.className = 'chat-model-name';
        name.textContent = model.name || model.id;
        row.appendChild(name);
        var meta = document.createElement('span');
        meta.className = 'chat-model-meta';
        var parts = [];
        var price = formatModelPrice(model);
        if (price) {
            parts.push(price);
        }
        if (model.context_length) {
            parts.push(Math.round(model.context_length / 1000) + 'k');
        }
        if (parts.length) {
            meta.textContent = parts.join('  ');
            meta.title = 'Input / output price in US dollars per million tokens, then the context window';
            row.appendChild(meta);
        }
        row.addEventListener('mousedown', function (event) {
            // mousedown, not click: blur fires first on click and would close the list before the
            // selection was read.
            event.preventDefault();
            selectModel(model.id);
        });
        list.appendChild(row);
    });

    if (!shown.length) {
        var empty = document.createElement('div');
        empty.className = 'chat-model-empty';
        // Naming the filter matters here: with it on, a search for a paid model returns nothing
        // and the reason is a checkbox the user may have forgotten is ticked.
        empty.textContent = state.freeOnly ? 'No free model matches "' + filter + '" - untick "Show only free models" to search them all' : 'No model matches "' + filter + '"';
        list.appendChild(empty);
    } else if (matches.length > shown.length) {
        var more = document.createElement('div');
        more.className = 'chat-model-empty';
        more.textContent = 'and ' + (matches.length - shown.length) + ' more - keep typing to narrow';
        list.appendChild(more);
    }
}

function openModelList() {
    var input = byId('chat-model');
    input.value = '';
    var offered = (state.models || []).filter(function (model) { return !state.freeOnly || isFreeModel(model); });
    input.placeholder = 'Search ' + offered.length + (state.freeOnly ? ' free' : '') + ' models...';
    byId('chat-model-list').style.display = 'block';
    renderModelResults('');
}

function closeModelList() {
    byId('chat-model-list').style.display = 'none';
    var input = byId('chat-model');
    input.value = modelLabel(effectiveModel());
    input.placeholder = '';
}

function selectModel(id) {
    state.currentModel = id || null;
    closeModelList();
    updateModelNote();
    if (!state.conversation) {
        return;
    }
    fetch('./chat/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation: state.conversation, id: id || null })
    })
        .then(function () {
            // The server remembers a real selection as the global default, so mirror that here
            // rather than re-fetching the catalogue just to learn what we already know.
            if (id) {
                state.selectedModel = id;
            }
        })
        .catch(function (error) { console.error('Failed to set chat model', error); });
}

function populateModelPicker(models, selectedId) {
    state.currentModel = selectedId || state.currentModel;
    closeModelList();
    updateModelNote();
}

function loadModels() {
    return fetch('./chat/models')
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            state.models = payload.models || [];
            state.defaultModel = payload.default_model || '';
            state.selectedModel = payload.selected_model || '';
            state.catalogueAvailable = payload.catalogue_available !== false;
            populateModelPicker(state.models, state.currentModel);
        })
        .catch(function (error) { console.error('Failed to load chat models', error); });
}

// ---------------------------------------------------------------------------------------------
// The three permission toggles. Each carries its switch name in data-switch and shares one
// handler, so adding a fourth is a markup change plus a CHAT_STATUS_SWITCHES entry.
//
// None of these gate only this tab: chat_confirm_writes decides whether a write pauses for
// approval, chat_web_search bills per request through OpenRouter, and ai_ha_state_enable gates
// search_entities/get_entity_state/get_entity_history for every AI surface, MCP included. The
// footer is simply where a user is looking while wondering why the model can't see a light
// switch, or why it just changed a setting without asking. loadHaStateStatus() is the source of
// truth on every load and reconnect, so a control never drifts from what its gate enforces.
// ---------------------------------------------------------------------------------------------

function chatToggles() {
    return Array.prototype.slice.call(document.querySelectorAll('#chat-toggles input[data-switch]'));
}

function loadHaStateStatus() {
    return fetch('./chat/status')
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            var switches = payload.switches || {};
            chatToggles().forEach(function (toggle) {
                toggle.checked = !!switches[toggle.getAttribute('data-switch')];
            });
        })
        .catch(function (error) { console.error('Failed to load chat switch status', error); });
}

function changeChatSwitch(event) {
    var toggle = event.target;
    var name = toggle.getAttribute('data-switch');
    var desired = toggle.checked;
    fetch('./chat/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, enabled: desired })
    })
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            // A rejected name still parses as JSON, so trust the echoed value rather than the
            // request: on an error response there is no 'enabled' field and the box reverts.
            if (payload && payload.ok) {
                toggle.checked = !!payload.enabled;
            } else {
                console.error('Failed to set ' + name, payload);
                toggle.checked = !desired;
            }
        })
        .catch(function (error) {
            console.error('Failed to set ' + name, error);
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
    // The banner exists to say a reply is happening SOMEWHERE ELSE, and to offer a way there.
    // On the conversation already open it was telling the user about the thing in front of them
    // and offering to switch to where they already are. The transcript is the status here.
    if (conversationId && conversationId === state.conversation) {
        hideBanner();
    } else {
        showBanner(conversationId, title);
    }
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

function appendToTranscript(node) {
    // Every transcript append goes through here so the waiting indicator stays last. Showing it
    // is not enough on its own: the user's own message arrives as an SSE event AFTER the
    // indicator goes up, and tool blocks arrive after that, so each of them would otherwise land
    // below "thinking" and leave it hanging above the message it is waiting on.
    var transcript = byId('chat-transcript');
    transcript.appendChild(node);
    var thinking = byId('chat-thinking');
    if (thinking && thinking.parentNode === transcript && !thinking.classList.contains('chat-thinking-hidden')) {
        // appendChild moves it rather than copying, so this is a reorder, not a duplicate.
        transcript.appendChild(thinking);
    }
}

function appendBubble(role, text) {
    var bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-' + role;
    bubble.innerHTML = renderMarkdown(text || '');
    appendToTranscript(bubble);
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
    // A status marker the user can read at a glance without expanding anything: grey while the
    // call is in flight, then a green tick or a red cross. Inserted rather than written into the
    // innerHTML above so its text never passes through markup.
    var status = document.createElement('span');
    status.className = 'chat-tool-status chat-tool-status-pending';
    status.textContent = TOOL_STATUS_PENDING;
    summary.insertBefore(status, summary.firstChild);
    toolStatuses[data.call_id] = status;
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
    appendToTranscript(container);
    toolRows[data.call_id] = resultHolder;
    toolSummaries[data.call_id] = summary;
    // A write may already have been approved before its tool block was drawn - on a history
    // replay the approvals arrive first - so claim any badge waiting for this call.
    if (pendingApprovalBadges[data.call_id]) {
        markToolApproval(data.call_id, pendingApprovalBadges[data.call_id]);
        delete pendingApprovalBadges[data.call_id];
    }
    scrollTranscriptToBottom();
}

function setToolStatus(callId, ok) {
    // A tool whose row was rebuilt from history has a marker too, so a replayed conversation
    // reads the same as a live one.
    var marker = toolStatuses[callId];
    if (!marker) {
        return;
    }
    marker.textContent = ok ? TOOL_STATUS_OK : TOOL_STATUS_ERROR;
    marker.className = 'chat-tool-status ' + (ok ? 'chat-tool-status-ok' : 'chat-tool-status-error');
}

function markToolApproval(callId, status) {
    // The approval belongs on the request it approved, not in a block of its own: a user scanning
    // the transcript wants to see, against the call that changed something, that they said yes.
    // Held until the tool block exists, since the two arrive in either order.
    var summary = toolSummaries[callId];
    if (!summary) {
        pendingApprovalBadges[callId] = status;
        return;
    }
    if (summary.querySelector('.chat-approval-badge')) {
        return;
    }
    var badge = document.createElement('span');
    badge.className = 'chat-approval-badge chat-approval-' + status;
    badge.textContent = APPROVAL_BADGES[status] || status;
    summary.appendChild(badge);
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
    setToolStatus(data.call_id, data.ok);
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

    appendToTranscript(card);
    confirmCards[data.call_id] = card;
    // The turn stops being "thinking" the instant it asks for approval.
    clearThinkingBubble();
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
    markToolApproval(data.call_id, data.approved ? 'approved' : 'rejected');
    delete confirmCards[data.call_id];
    // Answered, so the model is working again - unless another approval is still outstanding, in
    // which case showThinkingBubble() declines on its own.
    if (state.busy && state.busy.conversation_id === state.conversation) {
        startThinkingTimer(Date.now());
        showThinkingBubble();
    }
}

function clearPendingBubble() {
    if (pendingBubble && !pendingText) {
        pendingBubble.remove();
    }
    pendingBubble = null;
    pendingText = '';
}

// Unlike clearPendingBubble() above - which keeps a bubble that already has real streamed content,
// correct when a turn simply ends - a retry must throw away whatever the failed attempt streamed
// unconditionally, content or not, so the retried attempt starts onto a clean bubble rather than
// appending onto the discarded one. See handleRetry().
function discardPendingBubble() {
    if (pendingBubble) {
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
    // Holds the retry reason/attempt/countdown text while a completion is being retried - see
    // startRetryCountdown(). Empty and invisible (.chat-thinking-retry has no content, and
    // .chat-thinking-retrying is what makes it visible) until a 'retry' event fills it in.
    var retryStatus = document.createElement('span');
    retryStatus.id = 'chat-thinking-retry';
    retryStatus.className = 'chat-thinking-retry';
    bubble.appendChild(retryStatus);
    return bubble;
}

function awaitingApproval() {
    // An outstanding approval card means the turn is parked on the user, not working.
    for (var key in confirmCards) {
        if (Object.prototype.hasOwnProperty.call(confirmCards, key)) {
            return true;
        }
    }
    return false;
}

function showThinkingBubble() {
    // Nothing is thinking while an approval is outstanding - the model is blocked on an answer
    // only the user can give, and the Approve card directly above says so far better than a
    // counter climbing against a turn that is not running. Showing both was actively misleading:
    // the elapsed time was real, but it was time the user was taking, not the model.
    if (awaitingApproval()) {
        clearThinkingBubble();
        return;
    }
    var bubble = ensureThinkingBubble();
    // Appended every time, not only when it has no parent. hideThinkingBubble() just adds a
    // class - the element stays in the transcript where it was - so everything appended since
    // then, including the user's next message, lands after it. Re-appending moves it back to the
    // end, which is where "waiting for a response" belongs. appendChild moves an element that is
    // already in the DOM rather than duplicating it, so there is nothing to remove first.
    byId('chat-transcript').appendChild(bubble);
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
    stopRetryCountdown();
    hideThinkingBubble();
}

// ---------------------------------------------------------------------------------------------
// Retry countdown, shown inside the thinking bubble while a completion attempt is being retried
// after a transient provider failure. Deliberately independent of thinkingTimer above, which keeps
// counting the turn's total elapsed time underneath - the two must be started and stopped without
// touching one another, since a retry is a temporary state inside a turn that is still running.
// ---------------------------------------------------------------------------------------------

// The only place that clears retryCountdownTimer, mirroring stopThinkingTimer()'s own discipline:
// called from clearThinkingBubble() (every turn-ending path already goes through that) and again
// at the start of startRetryCountdown() itself, so a second 'retry' event before the first
// countdown finishes replaces it cleanly rather than stacking a second interval on top.
function stopRetryCountdown() {
    if (retryCountdownTimer) {
        clearInterval(retryCountdownTimer);
        retryCountdownTimer = null;
    }
    var bubble = byId('chat-thinking');
    if (bubble) {
        bubble.classList.remove('chat-thinking-retrying');
    }
    var status = byId('chat-thinking-retry');
    if (status) {
        status.textContent = '';
    }
}

// data carries {attempt, of, reason, delay} - the next attempt's number, the total attempts, the
// short reason classify_completion_failure() picked on the server, and the backoff in seconds
// about to be waited before that attempt starts. The countdown itself is cosmetic (nothing here
// drives the actual wait, which happens entirely server-side); it exists only so a user watching
// the transcript sees why nothing is happening rather than mistaking a retry for a hang.
function startRetryCountdown(data) {
    stopRetryCountdown();
    var status = byId('chat-thinking-retry');
    if (!status) {
        return;
    }
    var bubble = byId('chat-thinking');
    if (bubble) {
        bubble.classList.add('chat-thinking-retrying');
    }
    var reason = (data && data.reason) ? String(data.reason) : 'Provider error';
    var attempt = Number(data && data.attempt) || 0;
    var of = Number(data && data.of) || 0;
    var remaining = Math.max(0, Math.round(Number(data && data.delay) || 0));

    function render() {
        var suffix = remaining > 0 ? ' in ' + remaining + 's' : '';
        // textContent, never innerHTML: reason is the provider's own wording relayed by the
        // server, exactly as untrusted as any other server-relayed text in this client - see the
        // sink audit (test_inner_html_sinks_only_ever_receive_escaped_content).
        status.textContent = reason + ' — retrying (' + attempt + ' of ' + of + ')' + suffix + '…';
    }
    render();
    retryCountdownTimer = setInterval(function () {
        remaining -= 1;
        if (remaining <= 0) {
            stopRetryCountdown();
            return;
        }
        render();
    }, 1000);
}

function handleRetry(data) {
    // The failed attempt's partial bubble (if any) must not survive into the retried attempt -
    // discardPendingBubble() removes it unconditionally, content or not, unlike clearPendingBubble
    // (used when a turn ends outright, where real partial content is worth keeping on screen).
    discardPendingBubble();
    // Via showThinkingBubble() rather than appending here: this carried its own copy of the
    // parentNode guard, which is the bug that left the indicator stranded above later messages.
    // One path, one behaviour.
    showThinkingBubble();
    startRetryCountdown(data);
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
    var bubble = appendBubble('error', data.message || 'Something went wrong');
    appendErrorDetail(bubble, data.detail);
}

var APPROVAL_LABELS = { approved: 'Approved', rejected: 'Rejected', unanswered: 'Never answered - Predbat restarted while it was waiting' };
var APPROVAL_BADGES = { approved: '\u2713 approved', rejected: '\u2717 rejected', unanswered: '\u26a0 never answered' };
// Badges that arrived before their tool block existed.
var pendingApprovalBadges = {};

function appendApprovalRecord(entry) {
    // A settled approval, replayed from history. Deliberately not a live card: the turn that
    // asked is long gone, so a button here would resolve nothing.
    var wrap = document.createElement('div');
    wrap.className = 'chat-approval-record';
    var head = document.createElement('div');
    head.className = 'chat-approval-head';
    head.textContent = (APPROVAL_LABELS[entry.status] || entry.status) + ' ' + (entry.name || 'a change');
    wrap.appendChild(head);
    if (entry.arguments) {
        var pre = document.createElement('pre');
        // textContent, not the markdown renderer: these are tool arguments, which include text
        // the model chose.
        pre.textContent = JSON.stringify(entry.arguments, null, 2);
        wrap.appendChild(pre);
    }
    appendToTranscript(wrap);
}

function appendErrorDetail(bubble, detail) {
    // Collapsed, because most of the time the one-line message is all a user wants; expanded it
    // carries what OpenRouter actually said - which provider failed and its raw response - which
    // is the difference between "Provider returned error" and something reportable.
    //
    // textContent, never renderMarkdown: this is a third party's error text arriving over the
    // wire, and it is the one string on the page that a failing provider controls outright.
    if (!detail) {
        return;
    }
    var details = document.createElement('details');
    details.className = 'chat-error-detail';
    var summary = document.createElement('summary');
    summary.textContent = 'Details';
    details.appendChild(summary);
    var pre = document.createElement('pre');
    pre.textContent = detail;
    details.appendChild(pre);
    bubble.appendChild(details);
    scrollTranscriptToBottom();
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

// Cached-token counts are diagnostic (proof prompt caching is actually landing hits, not merely
// configured), so they are appended modestly in parentheses rather than given their own label -
// and omitted entirely when zero, since "0 cached" on every turn would just be noise.
function renderUsageEvent(data) {
    var turnText = 'This turn: ' + (data.prompt_tokens || 0) + ' in / ' + (data.completion_tokens || 0) + ' out - ' + formatCost(data.cost);
    if (data.cached_tokens) {
        turnText += ' (' + data.cached_tokens + ' cached)';
    }
    byId('chat-turn-usage').textContent = turnText;
    var totalText = 'Conversation total: ' + formatCost(data.conversation_cost);
    if (data.conversation_cached_tokens) {
        totalText += ' (' + data.conversation_cached_tokens + ' cached)';
    }
    byId('chat-total-cost').textContent = totalText;
    // data.prompt_tokens is this one completion's own prompt size, never the running
    // conversation_cost/conversation_cached_tokens totals above - see renderContextUsage().
    renderContextUsage(data.prompt_tokens);
}

function renderConversationTotal(usageTotal, lastPromptTokens) {
    usageTotal = usageTotal || {};
    byId('chat-turn-usage').textContent = '';
    var totalText = 'Conversation total: ' + formatCost(usageTotal.cost);
    if (usageTotal.cached_tokens) {
        totalText += ' (' + usageTotal.cached_tokens + ' cached)';
    }
    byId('chat-total-cost').textContent = totalText;
    renderContextUsage(lastPromptTokens);
}

// Locale-independent thousands separator for the context counter - avoids toLocaleString, whose
// grouping and separator both vary by browser locale for a figure that should read the same for
// every user.
function formatTokenCount(value) {
    var n = Math.round(Number(value) || 0);
    var digits = String(Math.abs(n));
    var withCommas = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (n < 0 ? '-' : '') + withCommas;
}

// Looks up the selected model's context_length from state.models, the same catalogue loadModels()
// already fetched for the picker. Falls back to state.defaultModel when the conversation has no
// per-conversation override, matching populateModelPicker()'s own 'Use default' behaviour.
function contextLengthForModel(modelId) {
    var id = modelId || state.defaultModel;
    var models = state.models || [];
    for (var index = 0; index < models.length; index++) {
        if (models[index].id === id) {
            return models[index].context_length || null;
        }
    }
    return null;
}

// Diagnostic context-size counter: the LAST turn's prompt_tokens against the selected model's
// context_length, never the conversation's cumulative usage_total.prompt_tokens - see
// ConversationStore.add_usage()'s docstring for why 'how big is this request right now' and 'what
// has this conversation cost in total' are two different numbers. Falls back to showing the token
// count alone, rather than a wrong or blank limit, when the model's context_length is unknown -
// the catalogue could not be read, or this model is missing from it.
function renderContextUsage(promptTokens) {
    var el = byId('chat-context-usage');
    if (!el) {
        return;
    }
    if (!promptTokens) {
        el.textContent = '';
        return;
    }
    var limit = contextLengthForModel(state.currentModel);
    var text = 'context ' + formatTokenCount(promptTokens);
    if (limit) {
        text += ' / ' + formatTokenCount(limit);
    }
    el.textContent = text;
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
    renderConversationTotal(payload.usage_total, payload.last_prompt_tokens);
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
    source.addEventListener('open', function () {
        // A reconnect is exactly when events go missing - EventSource resumes on its own after a
        // drop, and an 'idle' that arrived while disconnected is simply gone. Re-reading the
        // server's view here is what stops a missed idle leaving the banner up permanently. This
        // also fires on the first connection, where it costs one request and confirms the state
        // the page was rendered with.
        refreshConversations();
    });
    on(source, 'user', handleUser);
    on(source, 'delta', handleDelta);
    on(source, 'assistant', handleAssistant);
    on(source, 'tool_start', appendToolStart);
    on(source, 'tool_end', handleToolEnd);
    on(source, 'retry', handleRetry);
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
            // state.currentModel is set before renderHistory() runs, not after: renderHistory()
            // feeds the conversation's last_prompt_tokens through to renderContextUsage(), which
            // reads state.currentModel to find the right context_length - setting it afterwards
            // would render the new conversation's context counter against the PREVIOUS
            // conversation's model for one frame.
            state.cursor = payload.cursor || 0;
            state.currentModel = payload.model || null;
            renderHistory(payload);
            // The last failure is stored beside the messages rather than among them, so
            // renderHistory() cannot show it - it has to be replayed here. Without this the
            // transcript loses the error on every reload, which is exactly when a user goes
            // looking for it.
            // Approvals are stored beside the messages, not among them, so renderHistory()
            // cannot restore them. A pending one comes back as a live card - losing it strands
            // the turn, waiting for an answer the user can no longer give. A resolved one comes
            // back as a record of what was decided, which is the audit trail for anything the
            // agent changed.
            (payload.approvals || []).forEach(function (entry) {
                if (entry.status === 'pending') {
                    appendConfirmCard(entry);
                } else if (entry.status === 'unanswered') {
                    // Nothing ran for this one, so there is no tool block to badge - it needs a
                    // record of its own or the transcript simply loses the question.
                    appendApprovalRecord(entry);
                } else {
                    markToolApproval(entry.call_id, entry.status);
                }
            });
            if (payload.last_error) {
                var bubble = appendBubble('error', payload.last_error.message || 'The last turn failed');
                appendErrorDetail(bubble, payload.last_error.detail);
            }
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
        .then(function (payload) {
            renderConversationList(payload.conversations || []);
            reconcileBusy(payload.active);
        })
        .catch(function (error) { console.error('Failed to load conversations', error); });
}

function reconcileBusy(active) {
    // The server's own view of which turn is running, and the only thing that corrects a stale
    // banner. state.busy was otherwise cleared solely by the 'idle' SSE event: miss that once -
    // a dropped connection, a sleeping tab, a reconnect past the cursor - and the "Replying in
    // ..." banner stays up forever, offering to switch to a conversation that finished long ago,
    // with the composer locked behind it. This response is a live read, so it is authoritative.
    if (active && active.conversation_id) {
        setBusy(active.conversation_id, active.title, active.turn_id);
    } else if (state.busy) {
        setIdle();
    }
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
    byId('chat-model').addEventListener('focus', openModelList);
    byId('chat-model').addEventListener('input', function (event) { renderModelResults(event.target.value); });
    byId('chat-model').addEventListener('blur', function () {
        // Deferred: a mousedown on a result must be allowed to run before the list is hidden.
        setTimeout(closeModelList, 150);
    });
    byId('chat-model').addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
            event.target.blur();
        }
    });
    chatToggles().forEach(function (toggle) { toggle.addEventListener('change', changeChatSwitch); });

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
