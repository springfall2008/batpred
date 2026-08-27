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
        pending = {entry["conversation_id"] for entry in agent.pending_confirm.values()}
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
        messages = await agent.run_on_agent_loop(agent.store.snapshot(cid))
        _, cursor, _ = agent.events_since(0, cid)
        return web.json_response({"id": cid, "title": meta.get("title"), "model": meta.get("model"), "usage_total": meta.get("usage_total"), "messages": messages or [], "cursor": cursor, "active": agent.active})

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
            active = agent.active or {}
            return web.json_response({"error": "busy", "conversation_id": active.get("conversation_id"), "title": active.get("title")}, status=409)
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
        """Ask the running turn to stop at its next checkpoint."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
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
        return web.json_response({"models": [{"id": agent.default_model, "name": agent.default_model}], "default_model": agent.default_model})

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


def get_chat_styles():
    """Return the Chat tab's CSS."""
    return "<style></style>"


def get_chat_body():
    """Return the Chat tab's markup."""
    return "<body><div id='chat-root'></div>"


def get_chat_script():
    """Return the Chat tab's client script."""
    return "function escapeHtml(text){return String(text).replace(/[&<>\"']/g, function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c];});}\nfunction renderMarkdown(text){var safe = escapeHtml(text); return safe;}\nfunction setTitleText(node, title){node.textContent = title;}\n"
