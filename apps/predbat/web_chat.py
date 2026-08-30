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
import copy
import json
import re
import time

from aiohttp import web
from ruamel.yaml import YAML

from chat import AgentNotReadyError, ChatBusyError, PROVIDER_DEFAULT_URLS, PROVIDER_SETUP_HINTS, PROVIDERS, conversation_model_for, default_model_for
from utils import ROOT_YAML_KEY, SECRET_MASK, YAML_DUMP_WIDTH

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


# What a provider may be called in apps.yaml. The name becomes a YAML mapping key and is shown
# back to the user, so it is kept to characters that need no quoting and cannot be confused for
# YAML structure - a name like "a: b" or "- x" would round-trip through the file as something
# other than what was typed.
PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

# Sentinel for "the user did not type a new API key", which means keep whatever apps.yaml already
# holds for this provider. Distinct from an empty string, which is a request to clear the key.
KEEP_EXISTING_KEY = object()

APPS_YAML_PATH = "apps.yaml"


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

    async def _marshal(self, agent, coro):
        """Run a coroutine on the component's own loop, returning (result, error response).

        Every route that touches the store has to go through the component's loop, and every one
        of them can be called while that loop does not exist yet - the component is one of the last
        to start, and saving a provider deliberately restarts Predbat, so "reload the Chat tab
        while it is coming back" is a normal thing for a user to do rather than a rare race.

        Four routes were calling run_on_agent_loop() directly and answering 500 to that, including
        the one every page load makes. Written as a helper rather than a fifth copy of the same
        try/except so there is one place to be right, and so the test can assert that no route
        marshals any other way. Shaped like _conversation_or_404 below: a result and a response to
        return instead of it.
        """
        try:
            return await agent.run_on_agent_loop(coro), None
        except AgentNotReadyError:
            return None, web.json_response({"error": "The chat component is still starting"}, status=503)

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
        cid, error = await self._marshal(agent, agent.store.create(protect_id=protect))
        if error:
            return error
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
        _, error = await self._marshal(agent, agent.store.flush(body.get("id")))
        if error:
            return error
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
        _, error = await self._marshal(agent, agent.store.delete(cid))
        if error:
            return error
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
        snapshot, error = await self._marshal(agent, self._snapshot_and_cursor(agent, cid))
        if error:
            return error
        messages, cursor = snapshot
        return web.json_response(
            {
                "id": cid,
                "title": meta.get("title"),
                # The override only when it belongs to the provider now active, decided by the
                # same helper resolve_model() uses rather than a second copy of the rule: a page
                # showing a model the next turn would not use is worse than showing none.
                "model": conversation_model_for(meta, getattr(agent, "active_provider", None)),
                "usage_total": meta.get("usage_total"),
                # The most recent completion's prompt_tokens, not the cumulative usage_total.prompt_tokens
                # - see ConversationStore.add_usage() and the Chat tab's context-size footer
                # (renderContextUsage() in get_chat_script()), which is what actually reads this.
                "last_prompt_tokens": meta.get("last_prompt_tokens", 0),
                "messages": messages or [],
                # Carried separately from messages on purpose: the last failed turn is shown in
                # the transcript but is not part of the conversation, and must never be replayed
                # to the model. See ChatAgent._report_turn_error().
                # Passed the count being rendered, so an error a later turn has superseded is not
                # replayed. Without it the failure was appended after the whole transcript on
                # every load, landing below the successful reply that came after it.
                "last_error": agent.store.get_last_error(cid, message_count=len(messages or [])),
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

        events_since() is synchronous and lock-guarded, and no await separates it from snapshot()
        returning, so nothing on this loop can run between the two. That is narrower than it
        sounds: the two halves take different locks - the store's and the agent's - and a turn
        appends its message from the component thread, so a genuine thread interleave between
        list(messages) and events_since() can still yield a snapshot without the message whose
        event is already below the returned cursor. Such a message is neither rendered from
        history nor replayed from the stream.

        The window is tiny and closing it properly means one lock over both, which is a wider
        change than it is worth here: the Chat tab no longer depends on this for the user's own
        message, which it now draws on send rather than waiting for the echoed event (see
        sendMessage() and handleUser() in get_chat_script()). See the comment at the call site in
        html_chat_history for the gap this does close.
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
        models, error = await self._marshal(agent, agent.list_models())
        if error:
            return error
        return web.json_response(
            {
                "models": models,
                "default_model": agent.default_model,
                "selected_model": agent.store.get_selected_model(agent.active_provider),
                "catalogue_available": len(models) > 1,
                # Why it is unavailable, when that is known. "catalogue unavailable" on its own
                # leaves a user to work out for themselves whether the endpoint is down, the URL
                # is wrong or the key is refused - three very different fixes.
                "catalogue_error": getattr(agent, "catalogue_error", None),
            }
        )

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
        agent.store.set_model(body.get("conversation"), model_id, agent.active_provider)
        # Also remembered as the global choice, so the next new conversation starts on it and it
        # survives a restart. Only a real selection is remembered - clearing back to the default
        # should not pin whatever the default happened to be at that moment.
        if model_id:
            agent.store.set_selected_model(model_id, agent.active_provider)
        _, error = await self._marshal(agent, agent.store.flush(body.get("conversation")))
        if error:
            return error
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

    # ------------------------------------------------------------------------------------------
    # Provider settings.
    # ------------------------------------------------------------------------------------------

    def _saved_api_key(self, agent, name):
        """Return the API key apps.yaml holds for a named provider, or None.

        Server-side only. It exists so the Settings dialog can probe an existing provider's model
        list, or save an edit to its URL, without the browser ever being sent the key and asked to
        hand it back - a round trip that would put a credential in a page, in a POST body and in
        anything logging either.
        """
        for entry in agent.providers:
            if entry["name"] == name:
                return entry["api_key"]
        return None

    def _clean_provider(self, agent, posted, seen):
        """Validate one posted provider entry, returning (entry, error message).

        Everything is checked before the file is opened, so a batch with one bad entry in it
        cannot half-write apps.yaml.
        """
        if not isinstance(posted, dict):
            return None, "Malformed provider entry"
        name = str(posted.get("name") or "").strip()
        api_type = str(posted.get("type") or "").strip().lower()
        url = str(posted.get("url") or "").strip()
        model = str(posted.get("model") or "").strip()
        original = str(posted.get("original_name") or "").strip() or name

        if not PROVIDER_NAME_PATTERN.match(name):
            return None, "'{}' is not a usable provider name - use letters, numbers, spaces, dots, dashes or underscores".format(name)
        if name.lower() == "providers":
            return None, "'providers' cannot be used as a provider name"
        if name in seen:
            return None, "There is already a provider called '{}'".format(name)
        if api_type not in PROVIDERS:
            return None, "'{}' is not a provider type Predbat knows about".format(api_type)
        # Same reasoning as provider_type_choices(): a missing URL falls back only where there
        # is a real default for the type. For the others it stays empty and is refused below,
        # rather than quietly becoming OpenRouter's endpoint under someone else's name.
        url = url or PROVIDER_DEFAULT_URLS.get(api_type, "")
        if not url.startswith(("http://", "https://")):
            return None, "The URL for '{}' must start with http:// or https://".format(name)

        # Three distinct cases, and conflating any two of them loses a key or writes a blank one
        # over a real one: absent (or masked) means the dialog did not touch the field, so keep
        # what apps.yaml has; an empty string means the user cleared it deliberately; anything
        # else is a new key. saved_api_key travels alongside as the fallback for the keep case,
        # for entries the file cannot preserve in place - a rename, or a provider written in the
        # older loose shape - and never leaves this process.
        saved = self._saved_api_key(agent, original)
        api_key = posted.get("api_key")
        if api_key is None or api_key == SECRET_MASK:
            api_key = KEEP_EXISTING_KEY
        else:
            api_key = str(api_key).strip() or None
        return {"name": name, "type": api_type, "url": url, "model": model, "api_key": api_key, "saved_api_key": saved, "original_name": original}, None

    def _write_provider_block(self, cleaned):
        """Write the validated providers into apps.yaml, returning (chat block, error message).

        Existing entries are edited in place rather than replaced so their comments survive, and
        provider entries written loosely in the chat: block - the shape documented briefly before
        `providers:` existed - are folded in rather than left behind as config that silently stops
        being read once `providers:` is present.
        """
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = YAML_DUMP_WIDTH
        try:
            with open(APPS_YAML_PATH, "r") as handle:
                data = yaml.load(handle)
        except Exception as error:
            return None, "Error reading apps.yaml: {}".format(error)
        if not isinstance(data, dict) or ROOT_YAML_KEY not in data:
            return None, "'{}' section not found in apps.yaml".format(ROOT_YAML_KEY)

        root = data[ROOT_YAML_KEY]
        chat_block = root.get("chat")
        if not isinstance(chat_block, dict):
            chat_block = {}
            root["chat"] = chat_block
        providers = chat_block.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            chat_block["providers"] = providers

        # Anything dict-valued sitting directly in the chat: block is a provider written in the
        # older shape. Once `providers:` exists it is never read again, so it goes rather than
        # lingering as settings the user can see but Predbat ignores.
        for key in [key for key, value in list(chat_block.items()) if key != "providers" and isinstance(value, dict)]:
            del chat_block[key]

        keys_by_name = {entry["original_name"]: providers.get(entry["original_name"]) for entry in cleaned}
        for name in [name for name in list(providers.keys()) if name not in [entry["original_name"] for entry in cleaned]]:
            del providers[name]

        for entry in cleaned:
            existing = keys_by_name.get(entry["original_name"])
            target = existing if isinstance(existing, dict) else {}
            target["type"] = entry["type"]
            target["url"] = entry["url"]
            if entry["model"]:
                target["model"] = entry["model"]
            else:
                target.pop("model", None)
            if entry["api_key"] is KEEP_EXISTING_KEY:
                # Left in place untouched wherever the file already carries it, so its quoting -
                # and any anchor or alias it is written as - survives an edit to another field.
                # The fallback covers the cases where there is nothing to leave alone: an entry
                # being renamed, and one migrated out of the older loose shape above. Without it
                # both would silently drop the key and leave the provider unable to answer.
                if not target.get("api_key") and entry["saved_api_key"]:
                    target["api_key"] = entry["saved_api_key"]
            elif entry["api_key"]:
                target["api_key"] = entry["api_key"]
            else:
                target.pop("api_key", None)
            if entry["original_name"] != entry["name"]:
                providers.pop(entry["original_name"], None)
            providers[entry["name"]] = target

        try:
            with open(APPS_YAML_PATH, "w") as handle:
                yaml.dump(data, handle)
        except Exception as error:
            return None, "Error writing to apps.yaml: {}".format(error)
        return chat_block, None

    async def html_chat_providers(self, request):
        """Return the configured providers - without their keys - and the types one can be."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        return web.json_response({"providers": agent.provider_summary(), "types": provider_type_choices(), "ready": agent.provider_ready()})

    async def html_chat_provider_models(self, request):
        """Probe one endpoint for its model list, so a provider can be set up before it is saved.

        A failed probe is a 200 carrying an `error`, not an HTTP error: the request itself
        succeeded, it is the endpoint being described that did not answer, and the dialog shows
        that message beside the field rather than treating it as a broken page.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        api_type = str(body.get("type") or "").strip().lower()
        if api_type not in PROVIDERS:
            return web.json_response({"error": "Unknown provider type"}, status=400)
        url = str(body.get("url") or "").strip() or default_url_for(api_type)
        api_key = body.get("api_key")
        if api_key is None or api_key == SECRET_MASK:
            api_key = self._saved_api_key(agent, str(body.get("name") or "").strip())
        else:
            api_key = str(api_key).strip() or None
        probed, error = await self._marshal(agent, agent.probe_models(api_type, url, api_key))
        if error:
            return error
        models, reason = probed
        return web.json_response({"models": models or [], "error": reason})

    async def html_chat_provider_select(self, request):
        """Make one already-configured provider the active one.

        Separate from the save route because it is a different kind of act: the providers in
        apps.yaml do not change, so nothing is written and nothing restarts. That is what lets
        this sit in the footer next to the model picker rather than behind a dialog and a restart.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        chosen, error = await self._marshal(agent, agent.select_active_provider(name))
        if error:
            return error
        if chosen is None:
            return web.json_response({"error": "'{}' is not a configured provider".format(name)}, status=400)
        return web.json_response({"ok": True, "active": chosen, "ready": agent.provider_ready(), "providers": agent.provider_summary()})

    async def html_chat_providers_post(self, request):
        """Save the whole provider list to apps.yaml and adopt it in memory too.

        Writing apps.yaml restarts Predbat: hass.py watches the file and stops the process within
        about five seconds of its mtime changing, and the supervisor brings it back. The in-memory
        adoption below is therefore not what makes the change take effect - the restart is - but
        it is what makes this response describe reality rather than the configuration that was
        live before the write, and it keeps the seconds before the restart consistent.
        """
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        posted = body.get("providers")
        if not isinstance(posted, list):
            return web.json_response({"error": "No providers supplied"}, status=400)

        cleaned = []
        seen = set()
        for entry in posted:
            clean, error = self._clean_provider(agent, entry, seen)
            if error:
                return web.json_response({"error": error}, status=400)
            seen.add(clean["name"])
            cleaned.append(clean)

        active = str(body.get("active") or "").strip() or None
        if active and active not in seen:
            return web.json_response({"error": "'{}' is not one of the providers being saved".format(active)}, status=400)

        chat_block, error = self._write_provider_block(cleaned)
        if error:
            return web.json_response({"error": error}, status=500)

        # Only once the file write has succeeded is the live configuration changed, so a failed
        # save never leaves Predbat running settings that are not in the file.
        block = plain_yaml_value(chat_block)
        self.base.args["chat"] = block
        selected, error = await self._marshal(agent, agent.apply_provider_block(copy.deepcopy(block), active))
        if error:
            return error
        self.log("Chat providers saved to apps.yaml: {} ({} active)".format(", ".join(sorted(seen)) or "none", selected or "none"))
        return web.json_response({"ok": True, "providers": agent.provider_summary(), "active": selected, "ready": agent.provider_ready()})


def provider_type_choices():
    """Return the provider types the Settings dialog offers, with their defaults.

    Built from PROVIDERS itself so a type added to the agent appears in the dialog without a
    second list here needing to be remembered.

    Read from PROVIDER_DEFAULT_URLS directly rather than through default_url_for(), whose fallback
    is OpenRouter's own endpoint. That fallback is right for the agent - a provider with nothing
    configured has to dial somewhere - and wrong here, where it would prefill a form for a local
    or generic OpenAI-compatible endpoint with openrouter.ai and invite the user to save it.
    A type with no genuine default offers none, and the URL field starts empty.

    PROVIDER_SETUP_HINTS overrides both where a fresh setup wants something different from what an
    existing entry resolves to - see its comment for why Ollama does. The note it carries is shown
    under the URL field, which is the only place a prefilled value can be explained at the moment
    somebody is deciding whether to keep it.
    """
    choices = []
    for name, settings in PROVIDERS.items():
        hint = PROVIDER_SETUP_HINTS.get(name, {})
        choices.append(
            {
                "type": name,
                "url": hint.get("url", PROVIDER_DEFAULT_URLS.get(name, "")),
                "model": hint.get("model", default_model_for(name) or ""),
                "needs_key": bool(settings["needs_key"]),
                "note": hint.get("note", ""),
            }
        )
    return choices


def plain_yaml_value(value):
    """Return a copy of a ruamel value as plain Python containers.

    The live `args` dict is read all over Predbat and written back out by the apps.yaml page; a
    CommentedMap works as a dict but carries the parsed file's comments and anchors with it, so
    plain copies are what go into memory and the ruamel objects stay with the file.
    """
    if isinstance(value, dict):
        return {str(key): plain_yaml_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain_yaml_value(item) for item in value]
    return value


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
    display: flex;
    flex-direction: column;
    /* Takes whatever the header leaves. min-height: 0 is load-bearing, not tidiness: a flex item
       defaults to min-height auto, which refuses to shrink below its content, so without it the
       page's own content would push past the viewport and reintroduce the outer scrollbar this
       whole rule exists to remove. */
    flex: 1 1 auto;
    min-height: 0;
    color: var(--chat-text);
    background: var(--chat-bg);
}

#chat-topbar {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 6px;
    padding-bottom: 8px;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--chat-border);
    /* Without this the title button refuses to shrink and a long conversation title pushes the
       Settings and New chat buttons off the left of a phone screen. */
    min-width: 0;
}

#chat-topbar button {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 6px 10px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-panel-bg);
    color: var(--chat-text);
    font-size: 13px;
    cursor: pointer;
}

#chat-topbar button:hover {
    border-color: var(--chat-accent);
}

#chat-new {
    border-color: var(--chat-accent) !important;
    background: var(--chat-accent) !important;
    color: #ffffff !important;
    font-weight: bold;
}

.chat-topbar-icon {
    font-size: 14px;
    line-height: 1;
}

#chat-title-wrap {
    position: relative;
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    gap: 2px;
    /* The whole point of the row: the title is the only thing allowed to give way. */
    min-width: 0;
}

#chat-title-button {
    flex: 1 1 auto !important;
    min-width: 0;
    justify-content: space-between;
    border-color: transparent !important;
    background: transparent !important;
    font-size: 15px !important;
    font-weight: 600;
    text-align: left;
}

#chat-title-button:hover {
    border-color: var(--chat-border) !important;
    background: var(--chat-panel-bg) !important;
}

#chat-title-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

#chat-title-caret {
    flex: 0 0 auto;
    font-size: 10px;
    color: var(--chat-text-muted);
}

#chat-rename {
    padding: 6px 8px !important;
    border-color: transparent !important;
    background: transparent !important;
    color: var(--chat-text-muted) !important;
}

#chat-rename:hover {
    color: var(--chat-text) !important;
}

/* The conversation list, which used to be a permanent 260px column. Anchored under the title it
   belongs to, and above the transcript - the nav bar's own fixed elements sit higher still, but
   nothing in this page does. */
#chat-conv-panel {
    display: none;
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    z-index: 60;
    width: min(380px, 85vw);
    max-height: 60vh;
    overflow-y: auto;
    padding: 6px;
    border: 1px solid var(--chat-border);
    border-radius: 6px;
    background: var(--chat-bg);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
}

#chat-conv-panel.open {
    display: block;
}

#chat-conv-empty {
    padding: 8px;
    font-size: 12px;
    color: var(--chat-text-muted);
}

@media (max-width: 620px) {
    /* Icons only: three labelled buttons plus a title do not fit a phone, and the title is the
       one that carries information the user cannot guess from an icon. */
    .chat-topbar-label {
        display: none;
    }
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

/* Hidden only while no provider exists at all - see renderProviderSelect().
   Shown by adding .visible, the same way #chat-no-provider and #chat-notice are, and NOT by
   clearing an inline display: an element hidden by a rule here has no inline style to clear, so
   `style.display = ''` just lets this rule apply again and the control never appears at all.

   Sized to sit beside the model box without competing with it: which provider is answering is
   context for the model list, not the primary control. */
#chat-provider-select {
    display: none;
    max-width: 160px;
    padding: 3px 6px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-input-bg);
    color: var(--chat-text);
    font-size: 12px;
}

#chat-provider-select.visible {
    display: inline-block;
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
/* In the Settings dialog now rather than the footer, so a column rather than a row: these are
   set-once permissions with explanations attached, not controls a user reaches for mid-answer. */
#chat-toggles {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    font-size: 13px;
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
    gap: 6px;
    cursor: pointer;
    user-select: none;
}

/* ---------------------------------------------------------------------------------------------
   The Settings dialog.
   --------------------------------------------------------------------------------------------- */

#chat-no-provider {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 8px;
    padding: 8px 12px;
    border: 1px solid var(--chat-banner-border);
    border-radius: 4px;
    background: var(--chat-banner-bg);
    color: var(--chat-banner-text);
}

#chat-no-provider.visible {
    display: flex;
}

/* Deliberately not #chat-banner, which belongs to the busy state: showBanner/hideBanner own that
   element and reconcileBusy() rewrites it on every conversation refresh, so a message left there
   would be wiped within a second or two - exactly when the user needs to still be reading it. */
#chat-notice {
    display: none;
    margin-bottom: 8px;
    padding: 8px 12px;
    border: 1px solid var(--chat-banner-border);
    border-radius: 4px;
    background: var(--chat-banner-bg);
    color: var(--chat-banner-text);
}

#chat-notice.visible {
    display: block;
}

#chat-no-provider button {
    padding: 5px 10px;
    border: 1px solid var(--chat-banner-text);
    border-radius: 4px;
    background: transparent;
    color: var(--chat-banner-text);
    cursor: pointer;
}

#chat-settings {
    display: none;
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    left: 0;
    z-index: 200;
    align-items: center;
    justify-content: center;
    padding: 16px;
    background: rgba(0, 0, 0, 0.45);
}

#chat-settings.open {
    display: flex;
}

.chat-modal {
    width: min(720px, 100%);
    max-height: 88vh;
    overflow-y: auto;
    padding: 16px 18px 18px 18px;
    border: 1px solid var(--chat-border);
    border-radius: 8px;
    background: var(--chat-bg);
    color: var(--chat-text);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
}

.chat-modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
}

.chat-modal h2 {
    margin: 0;
    font-size: 18px;
}

.chat-modal h3 {
    margin: 18px 0 4px 0;
    font-size: 14px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--chat-text-muted);
}

.chat-modal-note {
    margin: 0 0 8px 0;
    font-size: 12px;
    color: var(--chat-text-muted);
}

#chat-settings-close {
    border: none;
    background: transparent;
    color: var(--chat-text-muted);
    font-size: 16px;
    cursor: pointer;
}

#chat-settings-error {
    display: none;
    margin-top: 10px;
    padding: 8px 10px;
    border: 1px solid var(--chat-banner-border);
    border-radius: 4px;
    background: var(--chat-error-bubble);
    color: var(--chat-error-text);
    font-size: 13px;
}

#chat-settings-error.visible {
    display: block;
}

.chat-provider-row {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 8px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    margin-bottom: 6px;
}

.chat-provider-row.active {
    border-color: var(--chat-accent);
    background: var(--chat-user-bubble);
}

.chat-active-chip {
    margin-left: 6px;
    padding: 0 6px;
    border-radius: 8px;
    background: var(--chat-accent);
    color: #ffffff;
    font-size: 10px;
    font-weight: normal;
    line-height: 16px;
}

.chat-provider-detail {
    flex: 1 1 auto;
    min-width: 0;
}

.chat-provider-name {
    font-weight: 600;
}

.chat-provider-sub {
    font-size: 12px;
    color: var(--chat-text-muted);
    overflow-wrap: anywhere;
}

.chat-provider-warn {
    color: var(--chat-error-text);
}

.chat-provider-buttons {
    flex: 0 0 auto;
    display: flex;
    gap: 4px;
}

.chat-provider-buttons button,
.chat-secondary-button,
.chat-modal-actions button,
.chat-form-actions button {
    padding: 5px 10px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-panel-bg);
    color: var(--chat-text);
    font-size: 13px;
    cursor: pointer;
}

#chat-settings-save,
#chat-provider-apply {
    border-color: var(--chat-accent);
    background: var(--chat-accent);
    color: #ffffff;
    font-weight: bold;
}

#chat-settings-save:disabled {
    opacity: 0.6;
    cursor: default;
}

#chat-provider-form {
    display: none;
    margin-top: 10px;
    padding: 10px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-panel-bg);
}

#chat-provider-form.open {
    display: block;
}

.chat-field {
    display: flex;
    flex-direction: column;
    gap: 3px;
    margin-bottom: 10px;
}

.chat-field label {
    font-size: 12px;
    font-weight: 600;
    color: var(--chat-text-muted);
}

.chat-field input,
.chat-field select {
    padding: 6px 8px;
    border: 1px solid var(--chat-border);
    border-radius: 4px;
    background: var(--chat-input-bg);
    color: var(--chat-text);
    font-size: 13px;
    /* Without this a long model id or URL widens the dialog instead of fitting it. */
    min-width: 0;
    max-width: 100%;
}

.chat-field-row {
    display: flex;
    gap: 6px;
    align-items: center;
    min-width: 0;
}

.chat-field-row input {
    flex: 1 1 auto;
}

.chat-field-note {
    font-size: 11px;
    color: var(--chat-text-muted);
}

.chat-field-note.chat-provider-warn {
    color: var(--chat-error-text);
}

.chat-form-actions,
.chat-modal-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}

.chat-modal-actions {
    margin-top: 18px;
    padding-top: 12px;
    border-top: 1px solid var(--chat-border);
}

#chat-settings-status {
    flex: 1 1 auto;
    font-size: 12px;
    color: var(--chat-text-muted);
}
</style>
"""


def get_chat_setup_body():
    """Return the Chat tab's page for when the chat component itself is not running.

    Rare now that the component starts whatever apps.yaml says - a missing provider is handled on
    the normal page by the Settings dialog, not here - so this only covers the component failing
    to construct at all, which is a Predbat problem rather than a configuration one.
    """
    return """
<div id="chat-page">
    <div id="chat-setup">
        <h2>Chat is not running</h2>
        <p>
            The chat component did not start, so there is nothing to talk to yet. This is not
            something you can fix from this page - check the <a href="./log">log</a> for why it
            failed to start, and the <a href="./components">components</a> page for its health.
        </p>
        <p>
            If Predbat has only just started, give it a moment and reload: components start in
            order and the chat component is one of the last.
        </p>
    </div>
</div>
"""


# What every text input on this page carries to keep password managers off it. autocomplete=off
# alone is not enough: browsers and managers deliberately ignore it on anything they think is a
# login field, and a page containing a type=password input - which this one does, in the Settings
# dialog - makes every text input on it a candidate username. macOS was offering to fill the model
# search box from the keychain because of that pairing.
#
# The data- attributes are the opt-outs 1Password, LastPass, Bitwarden and Dashlane each publish.
# They are vendor-specific by necessity: there is no standard way to say "this is not a credential
# field", only four proprietary ones. A manager not on this list may still offer - these are hints,
# and none of them is binding.
AUTOFILL_OFF = 'autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other"'


def get_chat_body():
    """Return the Chat tab's markup.

    `get_header_html()` has already opened `<body>` and written the nav bar, so this only adds
    the page's own content, following the same `<body>...` convention `web_annual.py` uses for its
    own pages.

    The conversation list lives in a dropdown off the title rather than in a sidebar. A 260px
    column is most of a phone's width for something a user looks at rarely, and keeping one
    layout at every width means the pending badge, the active highlight and the rename and delete
    controls exist once rather than twice.
    """
    return """
<body>
<div id="chat-page">
    <div id="chat-topbar">
        <button id="chat-settings-open" type="button" title="Providers, models and what the model is allowed to do">
            <span class="chat-topbar-icon">&#9881;</span><span class="chat-topbar-label">Settings</span>
        </button>
        <button id="chat-new" type="button" title="Start a new chat">
            <span class="chat-topbar-icon">+</span><span class="chat-topbar-label">New chat</span>
        </button>
        <span id="chat-title-wrap">
            <button id="chat-title-button" type="button" aria-haspopup="true" aria-expanded="false" title="Switch to another chat">
                <span id="chat-title-text">New chat</span>
                <span id="chat-title-caret">&#9662;</span>
            </button>
            <button id="chat-rename" type="button" title="Rename this chat">&#9998;</button>
            <div id="chat-conv-panel">
                <div id="chat-list"></div>
            </div>
        </span>
    </div>
    <div id="chat-main">
        <div id="chat-banner"></div>
        <div id="chat-no-provider">
            <span>No AI provider is set up yet, so Chat cannot answer anything.</span>
            <button id="chat-no-provider-open" type="button">Open Settings</button>
        </div>
        <div id="chat-notice"></div>
        <div id="chat-privacy">
            <span>Tool results - including log lines and configuration - are sent to the provider you have configured, and on to whoever runs the model you choose.</span>
            <button id="chat-privacy-dismiss" type="button">Dismiss</button>
        </div>
        <div id="chat-transcript"></div>
        <div id="chat-composer">
            <textarea id="chat-input" rows="2" placeholder="Ask Predbat... (Enter to send, Shift+Enter for a new line)"></textarea>
            <button id="chat-send" type="button">Send</button>
            <button id="chat-stop" type="button" title="Stops after the current step (the tool call or reply in progress finishes first)">Stop</button>
        </div>
        <div id="chat-footer">
            <select id="chat-provider-select" title="Which provider answers. Switching takes effect immediately - nothing is written and Predbat does not restart."></select>
            <span id="chat-model-wrap">
                <input type="text" id="chat-model" {autofill_off} spellcheck="false" placeholder="Choose a model">
                <div id="chat-model-list"></div>
                <span id="chat-model-note"></span>
            </span>
            <span id="chat-turn-usage"></span>
            <span id="chat-context-usage"></span>
            <span id="chat-total-cost"></span>
        </div>
    </div>
</div>
<div id="chat-settings" role="dialog" aria-modal="true" aria-labelledby="chat-settings-title">
    <div class="chat-modal">
        <div class="chat-modal-head">
            <h2 id="chat-settings-title">Chat settings</h2>
            <button id="chat-settings-close" type="button" title="Close">&#10005;</button>
        </div>
        <div id="chat-settings-error"></div>
        <h3>Providers</h3>
        <p class="chat-modal-note">Where your questions are sent. Saving writes them to <code>apps.yaml</code>, which restarts Predbat a few seconds later - this page will reconnect on its own. To switch between providers you have already set up, use the selector next to the model box instead: that takes effect immediately.</p>
        <div id="chat-provider-list"></div>
        <button id="chat-provider-add" type="button" class="chat-secondary-button">+ Add provider</button>
        <div id="chat-provider-form">
            <div class="chat-field">
                <label for="chat-provider-type">Provider</label>
                <select id="chat-provider-type"></select>
            </div>
            <div class="chat-field">
                <label for="chat-provider-name">Name</label>
                <input type="text" id="chat-provider-name" {autofill_off} spellcheck="false">
                <span class="chat-field-note">What this endpoint is called in apps.yaml. Any name will do - it only has to be unique.</span>
            </div>
            <div class="chat-field">
                <label for="chat-provider-url">URL</label>
                <input type="text" id="chat-provider-url" {autofill_off} spellcheck="false">
                <span id="chat-provider-url-note" class="chat-field-note"></span>
            </div>
            <div class="chat-field">
                <label for="chat-provider-key">API key</label>
                <input type="password" id="chat-provider-key" autocomplete="new-password" data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other" spellcheck="false">
                <span id="chat-provider-key-note" class="chat-field-note"></span>
            </div>
            <div class="chat-field">
                <label for="chat-provider-model">Default model</label>
                <span class="chat-field-row">
                    <input type="text" id="chat-provider-model" list="chat-provider-model-options" {autofill_off} spellcheck="false" placeholder="Fetch the list, or type a model id">
                    <datalist id="chat-provider-model-options"></datalist>
                    <button id="chat-provider-fetch" type="button" class="chat-secondary-button">Fetch models</button>
                </span>
                <span id="chat-provider-model-note" class="chat-field-note"></span>
            </div>
            <div class="chat-form-actions">
                <button id="chat-provider-apply" type="button">Done</button>
                <button id="chat-provider-cancel" type="button" class="chat-secondary-button">Cancel</button>
            </div>
        </div>
        <h3>What the model is allowed to do</h3>
        <p class="chat-modal-note">These are Predbat switches and take effect the moment you change them - they are not part of the Save below.</p>
        <span id="chat-toggles">
            <label class="chat-toggle" for="chat-confirm-writes-toggle" title="Ask before the model changes any Predbat setting or apps.yaml key. Turning this off lets it write without stopping to ask you.">
                <input type="checkbox" id="chat-confirm-writes-toggle" data-switch="chat_confirm_writes">
                Confirm writes
            </label>
            <label class="chat-toggle" for="chat-web-search-toggle" title="Costs extra: billed per request through OpenRouter, on top of the model's own cost. Off by default. Predbat's own documentation search does not need this and always works.">
                <input type="checkbox" id="chat-web-search-toggle" data-switch="chat_web_search">
                Web search <span class="chat-toggle-cost">(costs extra, OpenRouter only)</span>
            </label>
            <label class="chat-toggle" for="chat-ha-state-toggle" title="Lets the model read arbitrary Home Assistant entities and history, not just Predbat's own. On by default; also controls the MCP server, so turning it off closes those tools there too.">
                <input type="checkbox" id="chat-ha-state-toggle" data-switch="ai_ha_state_enable">
                HA state access
            </label>
        </span>
        <div class="chat-modal-actions">
            <span id="chat-settings-status"></span>
            <button id="chat-settings-save" type="button">Save to apps.yaml</button>
            <button id="chat-settings-cancel" type="button" class="chat-secondary-button">Close</button>
        </div>
    </div>
</div>
""".format(
        autofill_off=AUTOFILL_OFF
    )


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

var state = { catalogueError: '', streamConnected: false, freeOnly: readFreeOnly(), conversation: localStorage.getItem('predbatChatConversation'), cursor: 0, source: null, busy: null, models: [], defaultModel: '', selectedModel: '', currentModel: null, catalogueAvailable: true };
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
        note.textContent = state.catalogueError ? '(' + state.catalogueError + ' - only the configured model is offered)' : '(catalogue unavailable - only the configured model is offered)';
    } else if (!effectiveModel()) {
        note.textContent = 'Pick a model to start';
    } else {
        note.textContent = '';
    }
}

function isFreeModel(model) {
    // Decided by the server, which is the only side that knows what the endpoint is - see
    // is_free_model() in chat.py. Reading it from the quoted price here, which is what this did,
    // meant a local endpoint publishing no pricing had every model treated as not-free, and
    // "show only free models" - ticked by default - emptied the picker on an Ollama server where
    // everything is free.
    return model.free === true;
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
        //
        // The no-search-term case is its own message rather than 'No free model matches ""',
        // because it is a real configuration - a provider where nothing is free, such as Ollama
        // Cloud - rather than a search that found nothing. Saying how many models are behind the
        // filter is what turns "this is broken" into "untick that".
        if (!state.freeOnly) {
            empty.textContent = filter ? 'No model matches "' + filter + '"' : 'No models offered';
        } else if (filter) {
            empty.textContent = 'No free model matches "' + filter + '" - untick "Show only free models" to search them all';
        } else {
            empty.textContent = 'Nothing this provider offers is free - untick "Show only free models" to see ' + (state.models || []).length + ' paid models';
        }
        list.appendChild(empty);
    } else if (matches.length > shown.length) {
        var more = document.createElement('div');
        more.className = 'chat-model-empty';
        more.textContent = 'and ' + (matches.length - shown.length) + ' more - keep typing to narrow';
        list.appendChild(more);
    }
}

function openModelList() {
    // A catalogue that could not be fetched is not cached, so the endpoint coming back is one
    // request away - but nothing was asking. Opening the picker is exactly when someone wants the
    // list, and exactly when they have just fixed the endpoint that failed, so retry then. Only
    // while it is unavailable: a working catalogue is cached for a day and needs no re-asking.
    if (!state.catalogueAvailable) {
        loadModels().then(function () { renderModelResults(byId('chat-model').value || ''); });
    }
    var input = byId('chat-model');
    input.value = '';
    var offered = (state.models || []).filter(function (model) { return !state.freeOnly || isFreeModel(model); });
    input.placeholder = 'Search ' + offered.length + (state.freeOnly ? ' free' : '') + ' models...';
    byId('chat-model-list').style.display = 'block';
    // The list opens upward from the footer at the very bottom of the page (see #chat-model-list's
    // `bottom: 100%`), so the CSS max-height of 320px assumes there is always that much room above
    // it. On a short window there is not, and the list renders above the safe area where it is
    // clipped by the viewport or painted over by .menu-bar - the page-wide fixed nav (web_helper.py,
    // z-index: 1000) every Predbat page reserves body's padding-top for. Reaching the true top of
    // the viewport is not enough: that bar's own height has to be subtracted too, or a short window
    // still hides the top of the list underneath it. Clamp to what is actually free below it - the
    // list is already scrollable, so less height just means scrolling to see the rest.
    var wrapTop = byId('chat-model-wrap').getBoundingClientRect().top;
    var menuBar = document.querySelector('.menu-bar');
    var safeTop = menuBar ? menuBar.getBoundingClientRect().bottom : 0;
    byId('chat-model-list').style.maxHeight = Math.max(0, Math.min(320, wrapTop - safeTop - 12)) + 'px';
    renderModelResults('');
}

function closeModelList() {
    byId('chat-model-list').style.display = 'none';
    byId('chat-model-list').style.maxHeight = '';
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

function modelOffered(id) {
    return (state.models || []).some(function (model) { return model.id === id; });
}

// Drop a remembered model the provider now in use does not serve, so the picker falls back to
// "Pick a model to start" rather than naming something the next turn would fail on. The server
// applies the same rule to the conversation override, which is per provider - this covers what
// the browser is still holding from before the switch, and the case that rule cannot see: a model
// genuinely gone from an endpoint that still has it remembered.
//
// Only when the catalogue could actually be read. With no list to check against, "not in the
// list" means nothing, and blanking a configured model that works would be the worse mistake.
function dropModelsTheProviderDoesNotServe() {
    if (!state.catalogueAvailable) {
        return;
    }
    if (state.currentModel && !modelOffered(state.currentModel)) {
        state.currentModel = null;
    }
    if (state.selectedModel && !modelOffered(state.selectedModel)) {
        state.selectedModel = '';
    }
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
            state.catalogueError = payload.catalogue_error || '';
            dropModelsTheProviderDoesNotServe();
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
    // Every caller of setIdle() - the 'idle' SSE event, reconcileBusy() correcting a stale banner
    // on reconnect, and the no-active-turn branch of loadConversationData() - means the server has
    // no turn running. The thinking bubble is separate UI state driven by its own set of SSE
    // handlers (see clearThinkingBubble()'s callers), so a client that missed the 'idle' event
    // itself (a dropped connection, a thrown handler) previously had no way to notice the mismatch
    // even once reconcileBusy() caught up on reconnect - the banner cleared but the bubble did not.
    clearThinkingBubble();
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

// The bubble sendMessage() drew for a message this browser has sent but not yet seen echoed
// back. Held so the echo can adopt it instead of drawing a second copy; null at every other time.
var pendingUserBubble = null;

function handleUser(data) {
    // This browser already drew it on send, so consume that bubble rather than appending a
    // duplicate. Another browser watching the same conversation has nothing pending and appends
    // normally, which is why the echo is still what renders it there.
    if (pendingUserBubble) {
        pendingUserBubble = null;
        return;
    }
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
    // The transcript this pointed into has just been thrown away, and the rebuilt history already
    // contains the message. Leaving it set would make the next echo adopt a detached node and
    // swallow a message this browser had not drawn.
    pendingUserBubble = null;
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
    // Reset per EventSource, so "have we connected before?" means "has THIS source reconnected",
    // not "has any stream ever opened". Without it, switching conversation - which builds a new
    // source and fires 'open' again - would look like a reconnect and refetch on every click.
    state.streamConnected = false;
    var source = new EventSource('./chat/stream?conversation=' + encodeURIComponent(state.conversation) + '&cursor=' + state.cursor);
    source.addEventListener('open', function () {
        // A reconnect is exactly when events go missing - EventSource resumes on its own after a
        // drop, and an 'idle' that arrived while disconnected is simply gone. Re-reading the
        // server's view here is what stops a missed idle leaving the banner up permanently. This
        // also fires on the first connection, where it costs one request and confirms the state
        // the page was rendered with.
        refreshConversations();
        // A reconnect usually means Predbat restarted, which is the other moment the page's idea
        // of the provider and its models goes stale - a provider edit, or an endpoint that was
        // down when the page loaded and is up now. Skipped on the first connection, where the
        // page's own start-up already fetched both a moment ago.
        if (state.streamConnected) {
            loadProviders();
            loadModels();
        }
        state.streamConnected = true;
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
    updateChatTitle();
    setConversationPanel(false);
    // Returned, not just fired: createAndSend() has to wait for this before sending, because
    // openStream() runs at the end of it and a send that beats it races the event cursor.
    return loadConversationData(id).catch(function (error) { console.error('Failed to load chat history', error); });
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
    // Titles are cached here because the top bar shows the current conversation's own title and
    // has nowhere else to read it from - the transcript carries messages, not metadata.
    state.titles = {};
    if (!conversations.length) {
        var empty = document.createElement('div');
        empty.id = 'chat-conv-empty';
        empty.textContent = 'No chats yet.';
        list.appendChild(empty);
    }
    conversations.forEach(function (meta) {
        state.titles[meta.id] = meta.title || 'New chat';
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
    updateChatTitle();
}

// ---------------------------------------------------------------------------------------------
// The conversation dropdown, which replaced the sidebar. The list itself is unchanged - the same
// rows, the same pending badge and the same rename and delete buttons - it is only where they
// are shown that moved.
// ---------------------------------------------------------------------------------------------

function updateChatTitle() {
    var title = state.conversation ? (state.titles || {})[state.conversation] : null;
    setTitleText(byId('chat-title-text'), title || 'New chat');
    // Renaming needs something to rename. Nothing is selected on a first visit, and after the
    // current conversation is deleted.
    byId('chat-rename').disabled = !state.conversation;
}

function conversationPanelOpen() {
    return byId('chat-conv-panel').classList.contains('open');
}

function setConversationPanel(open) {
    byId('chat-conv-panel').classList.toggle('open', !!open);
    byId('chat-title-button').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
        // Costs and relative times go stale while the panel is shut, and a turn started in
        // another browser may have added a pending badge, so the list is refetched on open
        // rather than shown as it was whenever it was last drawn.
        refreshConversations();
    }
}

function toggleConversationPanel() {
    setConversationPanel(!conversationPanelOpen());
}

function renameCurrentConversation() {
    if (!state.conversation) {
        return;
    }
    renameConversation(state.conversation, (state.titles || {})[state.conversation]);
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
                updateChatTitle();
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
            // The message never landed, so take the bubble sendMessage() drew back down rather
            // than leaving the transcript claiming something was said that the server never
            // stored - a 409 because a turn is already running is the common way here.
            if (pendingUserBubble) {
                pendingUserBubble.remove();
                pendingUserBubble = null;
            }
            if (!error || error.message !== 'busy') {
                console.error('Failed to send message', error);
            }
        });
}

function createAndSend(text) {
    fetch('./chat/conversations', { method: 'POST' })
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            // Chained rather than fired alongside: selectConversation() opens the event stream at
            // the end of its load, and sending before that leaves the turn's own events arriving
            // with nothing listening, recoverable only through the cursor.
            return selectConversation(payload.id).then(function () {
                pendingUserBubble = appendBubble('user', text);
                doSend(payload.id, text);
                refreshConversations();
            });
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
    // Drawn here rather than waiting for the server to echo it back as a 'user' event. That echo
    // is a single event, and a single missed event left the message invisible until a conversation
    // switch rebuilt the transcript from history - see handleUser().
    pendingUserBubble = appendBubble('user', text);
    doSend(state.conversation, text);
}


// ---------------------------------------------------------------------------------------------
// Settings: providers and permissions.
//
// The provider list is edited entirely client-side and posted as a whole on Save, so a half-made
// edit - a name typed but no URL yet, a provider being swapped for another - never reaches
// apps.yaml. The server takes the full list and makes the file match it, which is also what makes
// removal work without a delete route of its own.
//
// API keys are never sent to this page. An entry says whether a key is set, and an empty key
// field means "leave whatever is in apps.yaml alone" - so editing a provider's URL cannot
// accidentally blank its credentials, and the browser never holds one.
// ---------------------------------------------------------------------------------------------

var settings = { providers: [], types: [], active: null, editing: null, loaded: false, baseline: null };

// What the dialog would post right now, as a comparable string. Save is ghosted while this still
// equals the baseline taken when the list was loaded, so closing a dialog you only looked at, or
// opening the add form and cancelling it, cannot write apps.yaml - and cannot therefore trigger a
// restart for no reason at all. api_key is included because typing a new key is a real change
// even when it leaves every visible field alone.
function settingsSnapshot() {
    return JSON.stringify({
        active: settings.active || '',
        providers: settings.providers.map(function (entry) {
            return [entry.name, entry.type, entry.url, entry.model, entry.api_key || '', entry.original_name];
        })
    });
}

function updateSaveButton() {
    var unchanged = settings.baseline !== null && settingsSnapshot() === settings.baseline;
    byId('chat-settings-save').disabled = unchanged;
    if (unchanged) {
        setSettingsStatus('');
    }
}

function settingsOpen() {
    return byId('chat-settings').classList.contains('open');
}

function openSettings() {
    byId('chat-settings').classList.add('open');
    setConversationPanel(false);
    showSettingsError('');
    setSettingsStatus('');
    loadProviders();
    loadHaStateStatus();
}

function closeSettings() {
    byId('chat-settings').classList.remove('open');
    closeProviderForm();
}

function showSettingsError(message) {
    var node = byId('chat-settings-error');
    node.textContent = message || '';
    node.classList.toggle('visible', !!message);
}

function setSettingsStatus(message) {
    byId('chat-settings-status').textContent = message || '';
}

// How long the post-save notice stays up. The file watcher checks every five seconds and the
// restart follows, so this has to outlast that gap comfortably - the whole point is that the user
// is still reading it when the tab goes quiet.
var CHAT_NOTICE_SECONDS = 25;
// A setTimeout id, always cleared through clearChatNotice() so a second save cannot leave the
// first one's timer running to hide a message it did not write.
var noticeTimer = null;

function clearChatNotice() {
    if (noticeTimer !== null) {
        clearTimeout(noticeTimer);
        noticeTimer = null;
    }
    byId('chat-notice').classList.remove('visible');
}

function showChatNotice(message) {
    clearChatNotice();
    var notice = byId('chat-notice');
    notice.textContent = message;
    notice.classList.add('visible');
    noticeTimer = setTimeout(function () {
        noticeTimer = null;
        notice.classList.remove('visible');
    }, CHAT_NOTICE_SECONDS * 1000);
}

function loadProviders() {
    return fetch('./chat/providers')
        .then(function (response) { return response.json(); })
        .then(function (payload) {
            if (payload.error) {
                showSettingsError(payload.error);
                return;
            }
            settings.providers = (payload.providers || []).map(function (entry) {
                // original_name is what the server looks the saved API key up under, so it has to
                // survive a rename in this dialog - it is the name in apps.yaml, not the one on
                // screen. api_key stays null until somebody types one.
                return { name: entry.name, type: entry.type, url: entry.url, model: entry.model || '', has_key: !!entry.has_key, needs_key: !!entry.needs_key, configured: !!entry.configured, original_name: entry.name, api_key: null };
            });
            settings.types = payload.types || [];
            settings.active = (payload.providers || []).reduce(function (found, entry) { return entry.active ? entry.name : found; }, null);
            settings.loaded = true;
            settings.baseline = settingsSnapshot();
            renderProviderTypes();
            renderProviderList();
            renderProviderSelect();
            updateSaveButton();
            setProviderReady(!!payload.ready);
        })
        .catch(function (error) {
            console.error('Failed to load providers', error);
            showSettingsError('Could not load the provider list.');
        });
}

function setProviderReady(ready) {
    byId('chat-no-provider').classList.toggle('visible', !ready);
}

function providerType(name) {
    return settings.types.filter(function (entry) { return entry.type === name; })[0] || null;
}

function renderProviderTypes() {
    var select = byId('chat-provider-type');
    if (select.options.length === settings.types.length && select.options.length) {
        return;
    }
    select.innerHTML = '';
    settings.types.forEach(function (entry) {
        var option = document.createElement('option');
        option.value = entry.type;
        option.textContent = entry.type;
        select.appendChild(option);
    });
}

function renderProviderList() {
    var list = byId('chat-provider-list');
    list.innerHTML = '';
    if (!settings.providers.length) {
        var empty = document.createElement('p');
        empty.className = 'chat-modal-note';
        empty.textContent = 'No providers yet. Add one to start chatting.';
        list.appendChild(empty);
        return;
    }
    settings.providers.forEach(function (entry, index) {
        var row = document.createElement('div');
        row.className = 'chat-provider-row' + (entry.name === settings.active ? ' active' : '');

        var detail = document.createElement('div');
        detail.className = 'chat-provider-detail';
        var name = document.createElement('div');
        name.className = 'chat-provider-name';
        name.textContent = entry.name + ' (' + entry.type + ')';
        // Which one is answering is shown here but changed in the footer: switching provider
        // writes nothing and restarts nothing, so putting it behind this dialog's Save button
        // would charge a restart for something that does not need one.
        if (entry.name === settings.active) {
            var chip = document.createElement('span');
            chip.className = 'chat-active-chip';
            chip.textContent = 'active';
            name.appendChild(chip);
        }
        detail.appendChild(name);

        var sub = document.createElement('div');
        sub.className = 'chat-provider-sub';
        sub.textContent = entry.url;
        detail.appendChild(sub);

        var extra = document.createElement('div');
        extra.className = 'chat-provider-sub';
        var bits = [entry.model ? entry.model : 'no default model'];
        if (entry.api_key) {
            bits.push('new key entered');
        } else if (entry.has_key) {
            bits.push('key saved');
        } else if (entry.needs_key) {
            bits.push('no key');
        }
        extra.textContent = bits.join(' - ');
        detail.appendChild(extra);

        // A provider that cannot answer is worth saying so about here, where it can be fixed,
        // rather than leaving the user to find out when a question fails.
        if (entry.needs_key && !entry.has_key && !entry.api_key) {
            var warn = document.createElement('div');
            warn.className = 'chat-provider-sub chat-provider-warn';
            warn.textContent = 'Needs an API key before it can answer.';
            detail.appendChild(warn);
        }
        row.appendChild(detail);

        var buttons = document.createElement('div');
        buttons.className = 'chat-provider-buttons';
        var edit = document.createElement('button');
        edit.type = 'button';
        edit.textContent = 'Edit';
        edit.addEventListener('click', function () { openProviderForm(index); });
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = 'Remove';
        remove.addEventListener('click', function () { removeProvider(index); });
        buttons.appendChild(edit);
        buttons.appendChild(remove);
        row.appendChild(buttons);

        list.appendChild(row);
    });
}

function removeProvider(index) {
    var entry = settings.providers[index];
    if (!entry || !window.confirm('Remove the provider "' + entry.name + '"?')) {
        return;
    }
    settings.providers.splice(index, 1);
    if (settings.active === entry.name) {
        settings.active = settings.providers.length ? settings.providers[0].name : null;
    }
    closeProviderForm();
    renderProviderList();
    // Status first, then the button: updateSaveButton() clears the status when the snapshot turns
    // out to match the baseline, which is what happens when somebody opens Edit and clicks Done
    // without changing anything. Setting it afterwards would leave "Not saved yet." above a
    // ghosted Save button, saying there is something to save when there is not.
    setSettingsStatus('Not saved yet.');
    updateSaveButton();
}

// A name that is unique among the providers already listed, so adding two of the same type in a
// row does not produce a duplicate the server would refuse.
function uniqueProviderName(base) {
    var taken = settings.providers.map(function (entry) { return entry.name; });
    if (taken.indexOf(base) < 0) {
        return base;
    }
    var suffix = 2;
    while (taken.indexOf(base + '-' + suffix) >= 0) {
        suffix += 1;
    }
    return base + '-' + suffix;
}

function openProviderForm(index) {
    var entry = index === null || index === undefined ? null : settings.providers[index];
    var type = entry ? entry.type : (settings.types.length ? settings.types[0].type : 'openrouter');
    var defaults = providerType(type) || { url: '', model: '' };
    settings.editing = entry ? index : null;
    byId('chat-provider-type').value = type;
    byId('chat-provider-name').value = entry ? entry.name : uniqueProviderName(type);
    byId('chat-provider-url').value = entry ? entry.url : defaults.url;
    byId('chat-provider-model').value = entry ? entry.model : defaults.model;
    byId('chat-provider-key').value = '';
    byId('chat-provider-model-options').innerHTML = '';
    setProviderNote('chat-provider-model-note', '');
    // Only when adding. Editing an existing provider must not explain a prefilled default over
    // the URL the user actually chose and is looking at.
    setProviderNote('chat-provider-url-note', entry ? '' : (defaults.note || ''));
    updateKeyNote();
    byId('chat-provider-form').classList.add('open');
    showSettingsError('');
}

function closeProviderForm() {
    settings.editing = null;
    byId('chat-provider-form').classList.remove('open');
}

function setProviderNote(id, message, warn) {
    var node = byId(id);
    node.textContent = message || '';
    node.classList.toggle('chat-provider-warn', !!warn);
}

function updateKeyNote() {
    var type = byId('chat-provider-type').value;
    var entry = settings.editing === null ? null : settings.providers[settings.editing];
    var defaults = providerType(type);
    var input = byId('chat-provider-key');
    if (entry && (entry.has_key || entry.api_key)) {
        input.placeholder = 'A key is already saved - leave blank to keep it';
        setProviderNote('chat-provider-key-note', 'Type a new key to replace it. Clearing it is not possible from here - remove the provider instead.');
        return;
    }
    // A type whose form is prefilled with a hosted endpoint cannot claim a key is unnecessary -
    // it is, for the endpoint sitting in the URL box above - but it is genuinely not needed if the
    // user follows the note and points it at their own server. Said once, covering both, rather
    // than reimplementing the server's is-this-address-local rule in the browser to guess which.
    if (defaults && defaults.note) {
        input.placeholder = 'Needed for a hosted endpoint';
        setProviderNote('chat-provider-key-note', 'Needed for the hosted endpoint above. Leave it empty if you change the URL to a server of your own.');
        return;
    }
    if (defaults && !defaults.needs_key) {
        input.placeholder = 'Not usually needed';
        setProviderNote('chat-provider-key-note', 'Local endpoints do not normally need a key. Leave this blank unless yours is behind a proxy that wants one.');
        return;
    }
    input.placeholder = '';
    setProviderNote('chat-provider-key-note', 'Required before this provider can answer anything.');
}

// Changing the type re-points the URL and model, but only when they are still a default - a URL
// the user typed themselves must survive flipping the type to correct a wrong guess.
function changeProviderType() {
    var defaults = providerType(byId('chat-provider-type').value);
    if (!defaults) {
        return;
    }
    var urlField = byId('chat-provider-url');
    var modelField = byId('chat-provider-model');
    var knownUrls = settings.types.map(function (entry) { return entry.url; });
    var knownModels = settings.types.map(function (entry) { return entry.model; });
    if (!urlField.value || knownUrls.indexOf(urlField.value) >= 0) {
        urlField.value = defaults.url;
    }
    if (!modelField.value || knownModels.indexOf(modelField.value) >= 0) {
        modelField.value = defaults.model;
    }
    byId('chat-provider-model-options').innerHTML = '';
    setProviderNote('chat-provider-model-note', '');
    setProviderNote('chat-provider-url-note', defaults.note || '');
    updateKeyNote();
}

function fetchProviderModels() {
    var entry = settings.editing === null ? null : settings.providers[settings.editing];
    var button = byId('chat-provider-fetch');
    var typedKey = byId('chat-provider-key').value;
    var payload = {
        type: byId('chat-provider-type').value,
        url: byId('chat-provider-url').value.trim(),
        // null, not an empty string: an empty string means "no key", while null means "use the
        // one already saved for this provider", which is the only way to probe an existing
        // endpoint without the browser ever seeing its key.
        api_key: typedKey ? typedKey : null,
        name: entry ? entry.original_name : ''
    };
    button.disabled = true;
    setProviderNote('chat-provider-model-note', 'Asking ' + (payload.url || 'the endpoint') + '...');
    fetch('./chat/providers/models', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        .then(function (response) { return response.json(); })
        .then(function (result) {
            button.disabled = false;
            if (result.error) {
                setProviderNote('chat-provider-model-note', result.error + ' You can still type a model id by hand.', true);
                return;
            }
            var options = byId('chat-provider-model-options');
            options.innerHTML = '';
            (result.models || []).forEach(function (model) {
                var option = document.createElement('option');
                option.value = model.id;
                option.label = model.name || model.id;
                options.appendChild(option);
            });
            setProviderNote('chat-provider-model-note', 'Found ' + (result.models || []).length + ' tool-capable models - start typing to search them.');
        })
        .catch(function (error) {
            button.disabled = false;
            console.error('Failed to fetch models', error);
            setProviderNote('chat-provider-model-note', 'Could not ask the server for the model list.', true);
        });
}

function applyProviderForm() {
    var name = byId('chat-provider-name').value.trim();
    var type = byId('chat-provider-type').value;
    var url = byId('chat-provider-url').value.trim() || (providerType(type) || {}).url || '';
    var model = byId('chat-provider-model').value.trim();
    var key = byId('chat-provider-key').value;
    var editing = settings.editing;

    if (!name) {
        showSettingsError('Give the provider a name.');
        return;
    }
    var clash = settings.providers.filter(function (entry, index) { return entry.name === name && index !== editing; });
    if (clash.length) {
        showSettingsError('There is already a provider called "' + name + '".');
        return;
    }
    if (!/^https?:\/\//.test(url)) {
        showSettingsError('The URL must start with http:// or https://');
        return;
    }

    var defaults = providerType(type) || {};
    if (editing === null) {
        settings.providers.push({ name: name, type: type, url: url, model: model, has_key: false, needs_key: !!defaults.needs_key, configured: false, original_name: name, api_key: key || null });
        if (!settings.active) {
            settings.active = name;
        }
    } else {
        var entry = settings.providers[editing];
        if (settings.active === entry.name) {
            settings.active = name;
        }
        entry.name = name;
        entry.type = type;
        entry.url = url;
        entry.model = model;
        entry.needs_key = !!defaults.needs_key;
        if (key) {
            entry.api_key = key;
        }
    }
    showSettingsError('');
    closeProviderForm();
    renderProviderList();
    // Status first, then the button: updateSaveButton() clears the status when the snapshot turns
    // out to match the baseline, which is what happens when somebody opens Edit and clicks Done
    // without changing anything. Setting it afterwards would leave "Not saved yet." above a
    // ghosted Save button, saying there is something to save when there is not.
    setSettingsStatus('Not saved yet.');
    updateSaveButton();
}

// The footer selector: which of the already-configured providers is answering. Changing it
// posts to a route that writes nothing to apps.yaml, so there is no restart and no Save button
// involved - the model catalogue is the only thing that has to be refetched, because it belongs
// to the endpoint rather than to Predbat.
function renderProviderSelect() {
    var select = byId('chat-provider-select');
    select.innerHTML = '';
    settings.providers.forEach(function (entry) {
        var option = document.createElement('option');
        option.value = entry.name;
        option.textContent = entry.name;
        option.selected = entry.name === settings.active;
        select.appendChild(option);
    });
    // Shown even with a single provider. It is not only a control - it is the label saying which
    // endpoint is answering, and which endpoint the model list beside it came from. Hiding it
    // until a second provider exists means the one case where a user most needs to know what they
    // are talking to, a fresh install with one endpoint, is the case that tells them nothing.
    // A class, not an inline display: the stylesheet hides this element by default, so clearing
    // the inline style would simply hand it back to that rule and show nothing ever.
    select.classList.toggle('visible', settings.providers.length > 0);
}

function changeProvider() {
    var name = byId('chat-provider-select').value;
    fetch('./chat/provider', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) })
        .then(function (response) { return response.json(); })
        .then(function (result) {
            if (result.error) {
                showChatNotice(result.error);
                return;
            }
            settings.active = result.active;
            settings.baseline = settingsSnapshot();
            setProviderReady(!!result.ready);
            renderProviderList();
            renderProviderSelect();
            updateSaveButton();
            // The catalogue, and the remembered model, both belong to the provider - so the
            // picker has to be refetched rather than left showing another endpoint's models.
            loadModels();
        })
        .catch(function (error) {
            console.error('Failed to switch provider', error);
            showChatNotice('Could not switch provider.');
        });
}

function saveSettings() {
    var button = byId('chat-settings-save');
    var payload = {
        active: settings.active,
        providers: settings.providers.map(function (entry) {
            return { name: entry.name, type: entry.type, url: entry.url, model: entry.model, api_key: entry.api_key, original_name: entry.original_name };
        })
    };
    button.disabled = true;
    setSettingsStatus('Saving...');
    fetch('./chat/providers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        .then(function (response) { return response.json(); })
        .then(function (result) {
            button.disabled = false;
            if (result.error) {
                setSettingsStatus('');
                showSettingsError(result.error);
                return;
            }
            showSettingsError('');
            setSettingsStatus('');
            // Back to the conversation: the save is the end of what anyone came to this dialog to
            // do, and leaving it open over the chat means dismissing it by hand before the page
            // is usable again. The message moves out with the user, because writing apps.yaml is
            // what restarts Predbat and the tab going quiet a few seconds later needs explaining.
            closeSettings();
            showChatNotice('Saved to apps.yaml. Predbat is restarting to pick it up - this page will reconnect on its own.');
            // Reloaded rather than assumed: the server decides which provider ends up active, and
            // has_key now reflects what is really in the file rather than what was typed here.
            // Still worth doing with the dialog shut - it is what clears the no-provider banner.
            loadProviders();
            // The catalogue belongs to whichever provider is now active, so the footer picker has
            // to be refetched - its old contents are another endpoint's models.
            loadModels();
        })
        .catch(function (error) {
            button.disabled = false;
            console.error('Failed to save providers', error);
            setSettingsStatus('');
            showSettingsError('Could not save to apps.yaml.');
        });
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

    byId('chat-title-button').addEventListener('click', function (event) {
        event.stopPropagation();
        toggleConversationPanel();
    });
    byId('chat-rename').addEventListener('click', renameCurrentConversation);
    byId('chat-settings-open').addEventListener('click', openSettings);
    byId('chat-no-provider-open').addEventListener('click', openSettings);
    byId('chat-settings-close').addEventListener('click', closeSettings);
    byId('chat-settings-cancel').addEventListener('click', closeSettings);
    byId('chat-settings-save').addEventListener('click', saveSettings);
    byId('chat-provider-add').addEventListener('click', function () { openProviderForm(null); });
    byId('chat-provider-apply').addEventListener('click', applyProviderForm);
    byId('chat-provider-cancel').addEventListener('click', closeProviderForm);
    byId('chat-provider-fetch').addEventListener('click', fetchProviderModels);
    byId('chat-provider-type').addEventListener('change', changeProviderType);
    byId('chat-provider-select').addEventListener('change', changeProvider);
    // Clicking the backdrop closes; clicking the dialog itself must not, so the panel stops the
    // event before it reaches the overlay it sits inside.
    byId('chat-settings').addEventListener('click', function (event) {
        if (event.target === byId('chat-settings')) {
            closeSettings();
        }
    });

    document.addEventListener('click', function (event) {
        if (conversationPanelOpen() && !byId('chat-title-wrap').contains(event.target)) {
            setConversationPanel(false);
        }
    });
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape') {
            return;
        }
        if (settingsOpen()) {
            closeSettings();
        } else if (conversationPanelOpen()) {
            setConversationPanel(false);
        }
    });

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
    // Loaded on every page view, not only when the dialog opens: this is what decides whether the
    // "no provider" banner is shown, and a user with nothing configured needs to be told that
    // before they type a question into a box that cannot answer it.
    loadProviders();
    refreshConversations();
    updateChatTitle();
    if (state.conversation) {
        selectConversation(state.conversation);
    }
});
"""
