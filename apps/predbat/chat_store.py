# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Conversation storage for the Predbat chat agent.

Holds many conversations as an index plus one body per conversation, so a turn rewrites only what
it touched. Every save carries a rolling expiry, which is also how deletion works: Storage has no
delete operation, so a deleted conversation is flagged, stops being re-saved, and ages out.

Deliberately free of asyncio primitives - the agent turn runs on the web component's event loop
while the owning component's thread flushes on its own, so all shared state is guarded by a
threading.Lock instead. See spec section 3.
"""

import json
import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

# Saved conversations are read by people - attached to bug reports, or opened to check what the
# model was actually sent. Written indented for that reason; the machine-only caches elsewhere
# in Storage stay compact.
CONVERSATION_JSON_INDENT = 2

# Approval record states. "pending" is the only one that is actionable; the rest are history.
# "unanswered" exists because a pending approval cannot survive a restart - the turn waiting on it
# was in memory - so a pending record loaded from disk is known to be dead and is relabelled on
# load rather than shown as a button that can no longer do anything.
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_UNANSWERED = "unanswered"

CONVERSATION_VERSION = 1
STORAGE_MODULE = "chat"
INDEX_FILENAME = "index"
BODY_CACHE_SIZE = 5
TITLE_MAX_LENGTH = 60
NEW_CONVERSATION_TITLE = "New chat"

# Sentinel distinguishing "no explicit system prompt override was passed" from "the override is
# genuinely None" (a conversation whose prompt has never been frozen) - see _save_body(), whose
# normal callers want it read fresh from the system_prompts cache while _cache_body()'s eviction
# path must pass the value it already popped, even when that value is None.
_UNSET = object()


def extract_cached_tokens(usage):
    """Return OpenRouter's usage.prompt_tokens_details.cached_tokens, or 0 if absent.

    Shared between ConversationStore.add_usage() (which accumulates it onto a conversation's
    running total) and chat.py's 'usage' event (which reports it for the turn that just
    completed), so the two can never read the field two different ways.
    """
    return (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0


def derive_title(text):
    """Turn a message into a conversation title: whitespace collapsed, truncated, never empty."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return NEW_CONVERSATION_TITLE
    return collapsed[:TITLE_MAX_LENGTH]


def trim_history(messages, max_history, log=None):
    """Return the tail of a conversation, cut only at a user message boundary.

    Cutting anywhere else eventually splits an assistant message carrying tool_calls from the
    tool messages answering them, and an OpenAI-compatible API rejects that whole request with a
    400 - every tool_call_id must have its tool reply present. Walking back to a user message
    can keep slightly more than max_history, which is the right trade: the cap exists to bound
    cost, and a few extra messages is cheaper than a failed turn.

    max_history <= 0 (0 is the shipped default) means unlimited: the whole conversation is
    returned untrimmed, with no scheme that rewrites or blanks older messages - doing that would
    change history bytes that are supposed to stay stable and invalidate the prompt cache from
    that point on, costing more than the trim would ever save. A tool-using exchange is four
    messages (user, assistant with tool_calls, tool, assistant), so the old default of 40 was only
    ten to twelve exchanges - too short for a long diagnostic session, hence unlimited by default.
    """
    if max_history <= 0 or len(messages) <= max_history:
        return list(messages)
    index = len(messages) - max_history
    while index >= 0 and messages[index].get("role") != "user":
        index -= 1
    if index < 0:
        if log:
            log("Warn: chat history has no user boundary within the last {} messages, keeping the whole conversation".format(max_history))
        return list(messages)
    return messages[index:]


class ConversationStore:
    """Index, bodies and persistence for the chat agent's conversations."""

    def __init__(self, storage, log, max_history=40, max_conversations=20, expiry_days=30):
        """Bind the store to a Storage component and its limits."""
        self.storage = storage
        self.log = log
        self.max_history = max_history
        self.max_conversations = max_conversations
        self.expiry_days = expiry_days
        self.index = OrderedDict()
        self.bodies = OrderedDict()
        # Mirrors self.bodies key-for-key: an entry exists here exactly when the matching
        # conversation is cached in self.bodies, populated and evicted together with it (see
        # _cache_body()) so a lookup never has to ask "loaded, but is the prompt?" separately.
        self.system_prompts = OrderedDict()
        self.dirty = set()
        # The model the user last chose in the picker, remembered across restarts so an install
        # whose provider names no model does not ask again on every boot. Guarded by the same
        # lock as the index it is stored beside; None means nothing has been chosen yet.
        # Per provider: {provider name: model id}. See get_selected_model().
        self.selected_model = {}
        # The provider the user last made active in the Settings dialog, remembered across
        # restarts. It has to be remembered: saving a provider change writes apps.yaml, which
        # restarts Predbat, so a choice held only in memory would be undone by the very act of
        # making it. None means "no explicit choice", which select_provider() reads as
        # "first usable entry".
        self.selected_provider = None
        # The most recent failed turn per conversation, saved beside the messages rather than
        # among them: a transport failure is not something the model said, so it must never be
        # replayed back to it. Kept for the user and for a bug report, and overwritten by the next
        # failure rather than accumulating.
        self.last_errors = {}
        # Write approvals per conversation: what was asked, and what the user answered. Saved
        # beside the messages, never among them - an approval is a record of a decision, not
        # something the model said, and the model already learns the outcome from the tool result
        # it gets back. Keeping it out of `messages` is what guarantees it is never replayed.
        self.approvals = {}
        self.lock = threading.Lock()
        self.loaded = False

    def _expiry(self):
        """Return the timezone-aware expiry a save should carry."""
        return datetime.now(timezone.utc) + timedelta(days=self.expiry_days)

    def _body_name(self, cid):
        """Return the storage filename for a conversation body."""
        return "conv_{}".format(cid)

    async def load_index(self):
        """Load the conversation index, dropping entries whose body has expired or vanished."""
        payload = await self.storage.load(STORAGE_MODULE, INDEX_FILENAME) if self.storage else None
        entries = []
        selected_model = None
        selected_provider = None
        if isinstance(payload, dict):
            if payload.get("version") != CONVERSATION_VERSION:
                self.log("Warn: chat index version {} is not {}, discarding it".format(payload.get("version"), CONVERSATION_VERSION))
            else:
                entries = payload.get("conversations") or []
                selected_model = payload.get("selected_model") or None
                selected_provider = payload.get("selected_provider") or None

        healed = False
        with self.lock:
            self.selected_model = selected_model if isinstance(selected_model, dict) else ({} if selected_model is None else selected_model)
            self.selected_provider = selected_provider if isinstance(selected_provider, str) else None
            self.index = OrderedDict()
            for entry in entries:
                cid = entry.get("id")
                if not cid:
                    continue
                self.index[cid] = entry
            self.loaded = True

        for cid in list(self.index.keys()):
            if self.index[cid].get("deleted"):
                continue
            body = await self.storage.load(STORAGE_MODULE, self._body_name(cid)) if self.storage else None
            if body is None or body.get("version") != CONVERSATION_VERSION:
                self.log("Info: chat conversation {} has expired or is unreadable, dropping it from the index".format(cid))
                with self.lock:
                    self.index.pop(cid, None)
                healed = True

        if healed:
            await self._save_index()
        return True

    def list_conversations(self):
        """Return metadata for every conversation the user can see, newest first.

        Deep-copied (not just dict(entry)) because a nested field - usage_total - is a dict of
        its own; a shallow copy would still alias it to the store's live object, which add_usage()
        mutates on another thread. Same aliasing hazard snapshot() closes for messages.
        """
        with self.lock:
            live = [json.loads(json.dumps(entry)) for entry in self.index.values() if not entry.get("deleted")]
        return sorted(live, key=lambda entry: entry.get("updated") or "", reverse=True)

    def get_meta(self, cid):
        """Return one conversation's metadata, or None if it is unknown or deleted.

        Deep-copied for the same reason as list_conversations(): usage_total is a nested dict that
        a shallow copy would still alias to the store's live object.
        """
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return None
            return json.loads(json.dumps(entry))

    async def create(self, model=None, protect_id=None):
        """Create a conversation, prune past the cap, and return the new id."""
        now = datetime.now(timezone.utc).isoformat()
        cid = secrets.token_hex(8)
        entry = {
            "id": cid,
            "title": NEW_CONVERSATION_TITLE,
            "created": now,
            "updated": now,
            "deleted": False,
            "model": model,
            "message_count": 0,
            "usage_total": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "cached_tokens": 0},
            "last_prompt_tokens": 0,
        }
        with self.lock:
            self.index[cid] = entry
            self.dirty.add(cid)
        await self._cache_body(cid, [])
        await self._prune(protect_id=protect_id)
        await self.flush(cid)
        return cid

    async def get_messages(self, cid):
        """Return a conversation's messages, loading and caching the body if needed."""
        with self.lock:
            if cid in self.bodies:
                self.bodies.move_to_end(cid)
                return self.bodies[cid]
            if self.index.get(cid) is None or self.index[cid].get("deleted"):
                return None

        payload = await self.storage.load(STORAGE_MODULE, self._body_name(cid)) if self.storage else None
        valid = isinstance(payload, dict) and payload.get("version") == CONVERSATION_VERSION
        messages = payload.get("messages", []) if valid else []
        # A body saved before system_prompt existed has no such key at all - get() then returns
        # None, which is exactly "not frozen yet" and needs no special migration handling; see
        # get_system_prompt()'s docstring.
        system_prompt = payload.get("system_prompt") if valid else None
        system_prompt_at = payload.get("system_prompt_at") if valid else None
        # Read back so the UI can still show the last failure after a reload. Deliberately kept
        # out of `messages`: it is stored beside them, so nothing that replays the conversation
        # to the model can pick it up by accident.
        last_error = payload.get("last_error") if valid else None
        if last_error:
            with self.lock:
                self.last_errors.setdefault(cid, last_error)

        approvals = payload.get("approvals") if valid else None
        if approvals:
            # A pending approval on disk is always dead: the turn that was waiting for it lived in
            # memory and did not survive whatever ended this process. Relabelled so the transcript
            # records that it was asked and never answered, rather than offering a button that
            # would resolve nothing.
            for entry in approvals:
                if entry.get("status") == APPROVAL_PENDING:
                    entry["status"] = APPROVAL_UNANSWERED
            with self.lock:
                self.approvals.setdefault(cid, approvals)
        await self._cache_body(cid, messages, system_prompt=system_prompt, system_prompt_at=system_prompt_at)
        return messages

    async def snapshot(self, cid):
        """Return a copy of a conversation's messages, safe to serialise off-thread.

        get_messages() hands back the live list, which the component thread may append to while
        a web handler is serialising it. The copy is taken under the lock so it cannot interleave
        with an append.
        """
        messages = await self.get_messages(cid)
        if messages is None:
            return None
        with self.lock:
            return list(messages)

    async def _cache_body(self, cid, messages, system_prompt=None, system_prompt_at=None):
        """Put a body in the LRU, evicting the least recently used once the cache is full.

        system_prompt/system_prompt_at are cached alongside messages in self.system_prompts,
        keyed the same way and evicted together, so a cache hit never has to ask the two caches
        two different questions. Eviction candidates are collected under the lock but flushed
        outside it - _save_body is a coroutine that takes the same lock, and awaiting while
        holding it would deadlock. A victim's prompt fields are read out of self.system_prompts
        and passed to _save_body explicitly, rather than left for _save_body to look up itself,
        because by the time _save_body runs the victim has already been popped from that cache.
        """
        evicted = []
        with self.lock:
            self.bodies[cid] = messages
            self.bodies.move_to_end(cid)
            self.system_prompts[cid] = {"system_prompt": system_prompt, "system_prompt_at": system_prompt_at}
            while len(self.bodies) > BODY_CACHE_SIZE:
                victim = next(iter(self.bodies))
                if victim == cid:
                    break
                victim_prompt = self.system_prompts.pop(victim, None) or {}
                evicted.append((victim, self.bodies.pop(victim), victim in self.dirty, victim_prompt.get("system_prompt"), victim_prompt.get("system_prompt_at")))
        for victim, payload, was_dirty, victim_system_prompt, victim_system_prompt_at in evicted:
            if was_dirty:
                await self._save_body(victim, messages=payload, system_prompt=victim_system_prompt, system_prompt_at=victim_system_prompt_at, evicted=True)

    async def append(self, cid, message):
        """Append a message to a conversation and mark it dirty."""
        messages = await self.get_messages(cid)
        if messages is None:
            return False
        with self.lock:
            messages.append(message)
            entry = self.index.get(cid)
            if entry is not None:
                entry["message_count"] = len(messages)
                entry["updated"] = datetime.now(timezone.utc).isoformat()
            self.dirty.add(cid)
        return True

    async def get_system_prompt(self, cid):
        """Return (system_prompt, system_prompt_at) for a conversation, loading the body if needed.

        Both are None when a prompt has never been frozen for this conversation - every
        conversation stored before this existed, plus any conversation whose first turn has not
        yet run - which is exactly the signal ChatAgent._frozen_system_prompt() uses to decide
        whether to build and store one now. Also None for an unknown or deleted id.
        """
        await self.get_messages(cid)
        with self.lock:
            info = self.system_prompts.get(cid) or {}
            return info.get("system_prompt"), info.get("system_prompt_at")

    def set_system_prompt(self, cid, system_prompt, system_prompt_at):
        """Freeze a conversation's system prompt in the cache and mark it dirty for the next flush.

        Requires the body to already be cached - every real caller gets there via
        get_system_prompt() (or get_messages()/append() earlier in the same turn) first, which
        always populates self.system_prompts for a live conversation as a side effect. Returns
        False without recording anything if that has not happened, rather than caching a prompt
        with no messages list to be saved alongside.
        """
        with self.lock:
            if cid not in self.bodies:
                return False
            self.system_prompts[cid] = {"system_prompt": system_prompt, "system_prompt_at": system_prompt_at}
            self.dirty.add(cid)
        return True

    def set_title(self, cid, title):
        """Set a conversation's title, returning the stored value."""
        cleaned = derive_title(title)
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return None
            entry["title"] = cleaned
            entry["updated"] = datetime.now(timezone.utc).isoformat()
            self.dirty.add(cid)
        return cleaned

    def rename(self, cid, title):
        """Rename a conversation - the same operation a user drives from the list."""
        return self.set_title(cid, title)

    def set_model(self, cid, model, provider=None):
        """Set a conversation's model override, or clear it back to the default with None.

        The provider it was chosen for is recorded alongside it. A model id only means anything to
        the endpoint serving it, so without knowing whose choice this was there is no way to tell,
        after a provider switch, whether the override still refers to something that exists.
        """
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return False
            entry["model"] = model
            entry["model_provider"] = provider if model else None
            self.dirty.add(cid)
        return True

    def add_usage(self, cid, usage):
        """Accumulate token, cost and cached-token usage onto a conversation.

        usage is the raw OpenRouter usage object for one completion - cached_tokens is read out
        of its nested prompt_tokens_details via extract_cached_tokens(), the same helper chat.py
        uses for the per-turn 'usage' event, so the running total and the event can never disagree
        about what counts as a cache hit.

        entry["last_prompt_tokens"] is set, not accumulated: it always holds the prompt_tokens of
        the most recent completion, overwritten on every call rather than summed the way
        usage_total is. That is deliberate - usage_total answers "what has this conversation cost
        in total", while last_prompt_tokens answers "how large is the request I am sending right
        now", which the Chat tab's context-size footer shows against the model's context_length.
        Reading usage_total.prompt_tokens for that instead would grow forever across turns and
        answer the wrong question - see chat.py's module docstring and _turn_loop's 'usage' event.
        """
        with self.lock:
            entry = self.index.get(cid)
            if entry is None:
                return
            total = entry.setdefault("usage_total", {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "cached_tokens": 0})
            for key in ("prompt_tokens", "completion_tokens", "cost"):
                total[key] = total.get(key, 0) + (usage.get(key) or 0)
            total["cached_tokens"] = total.get("cached_tokens", 0) + extract_cached_tokens(usage)
            entry["last_prompt_tokens"] = usage.get("prompt_tokens") or 0
            self.dirty.add(cid)

    async def delete(self, cid):
        """Hide a conversation and stop renewing it, so its stored body expires on its own."""
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return False
            entry["deleted"] = True
            self.bodies.pop(cid, None)
            self.system_prompts.pop(cid, None)
            self.dirty.discard(cid)
        await self._save_index()
        return True

    async def _prune(self, protect_id=None):
        """Mark the least recently updated conversations deleted once past the cap."""
        with self.lock:
            live = [entry for entry in self.index.values() if not entry.get("deleted")]
            surplus = len(live) - self.max_conversations
            if surplus <= 0:
                return
            candidates = sorted((entry for entry in live if entry["id"] != protect_id), key=lambda entry: entry.get("updated") or "")
            doomed = candidates[:surplus]
            for entry in doomed:
                entry["deleted"] = True
                self.bodies.pop(entry["id"], None)
                self.system_prompts.pop(entry["id"], None)
                self.dirty.discard(entry["id"])
        for entry in doomed:
            self.log("Info: chat conversation '{}' ({}) pruned past the {} conversation limit; its stored copy expires in {} days".format(entry.get("title"), entry["id"], self.max_conversations, self.expiry_days))
        await self._save_index()

    def set_last_error(self, cid, message, detail=None, message_count=None):
        """Record the most recent failed turn for a conversation, to be written on the next flush.

        The message count at the time is recorded with it. An error is not a message - it is kept
        out of the transcript so it can never be replayed to the model - which means a client
        rebuilding the conversation has no position to put it back at, and appended it to the end.
        A failure from three turns ago then reappeared below the successful reply that followed
        it, on every reload. The count is what lets the client tell "this is the latest thing that
        happened" from "a turn has since succeeded"; see html_chat_history's stale check.
        """
        if not cid:
            return
        with self.lock:
            self.last_errors[cid] = {
                "message": message,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(),
                "message_count": message_count,
            }
            self.dirty.add(cid)

    def message_count(self, cid):
        """Return how many messages a conversation currently holds, or 0 if it is not cached."""
        with self.lock:
            return len(self.bodies.get(cid) or [])

    def get_last_error(self, cid, message_count=None):
        """Return the most recent failed turn for a conversation, or None.

        message_count is the number of messages the caller is about to render. Given one, an error
        that has been superseded is not returned: if the conversation has grown since the failure
        was recorded, a later turn ran and the failure is history rather than the current state.
        Returning it anyway is what made an old error reappear at the bottom of the transcript on
        every reload, out of order and long after it mattered.

        The count is passed in rather than read from self.bodies because that is an LRU cache - an
        evicted conversation reads as zero messages, which would resurrect exactly the stale error
        this exists to suppress.
        """
        with self.lock:
            error = self.last_errors.get(cid)
            if error is None or message_count is None:
                return error
            recorded_at = error.get("message_count")
            if recorded_at is None:
                # Written before the count existed. No way to place it, so treat it as historical
                # rather than resurrect it at the end.
                return None
            return error if message_count <= recorded_at else None

    def add_approval(self, cid, card):
        """Record a write awaiting the user's answer."""
        if not cid or not card:
            return
        entry = {"call_id": card.get("call_id"), "name": card.get("name"), "arguments": card.get("arguments"), "status": APPROVAL_PENDING, "asked_at": datetime.now(timezone.utc).isoformat()}
        with self.lock:
            self.approvals.setdefault(cid, []).append(entry)
            self.dirty.add(cid)

    def resolve_approval(self, cid, call_id, status):
        """Record what the user answered, or that the approval expired."""
        if not cid or not call_id:
            return
        with self.lock:
            for entry in self.approvals.get(cid, []):
                if entry.get("call_id") == call_id and entry.get("status") == APPROVAL_PENDING:
                    entry["status"] = status
                    entry["answered_at"] = datetime.now(timezone.utc).isoformat()
                    self.dirty.add(cid)
                    break

    def get_approvals(self, cid):
        """Return copies of this conversation's approval records, oldest first."""
        with self.lock:
            return [dict(entry) for entry in self.approvals.get(cid, [])]

    def get_selected_model(self, provider=None):
        """Return the model the user last chose for a provider, or None.

        Kept per provider because a model id only means anything to the provider that serves it:
        a single remembered id would point at an OpenRouter model the moment someone switched to
        Ollama, leaving the picker set to something that is not there.
        """
        with self.lock:
            if isinstance(self.selected_model, dict):
                return self.selected_model.get(provider or "")
            # A value stored before this was per-provider. Honoured for whichever provider is
            # asking, since there is only one it could have belonged to.
            return self.selected_model

    def set_selected_model(self, model_id, provider=None):
        """Remember the model chosen for a provider, to be written on the next index flush."""
        with self.lock:
            if not isinstance(self.selected_model, dict):
                self.selected_model = {}
            key = provider or ""
            if model_id:
                self.selected_model[key] = model_id
            else:
                self.selected_model.pop(key, None)

    def get_selected_provider(self):
        """Return the provider the user last made active, or None if they never chose one."""
        with self.lock:
            return self.selected_provider

    def set_selected_provider(self, name):
        """Remember which provider is active, to be written on the next index flush."""
        with self.lock:
            self.selected_provider = str(name) if name else None

    async def _save_index(self):
        """Write the conversation index with a renewed expiry."""
        if not self.storage:
            return False
        with self.lock:
            payload = {"version": CONVERSATION_VERSION, "conversations": [dict(entry) for entry in self.index.values()], "selected_model": self.selected_model, "selected_provider": self.selected_provider}
        return await self.storage.save(STORAGE_MODULE, INDEX_FILENAME, payload, format="json", expiry=self._expiry(), indent=CONVERSATION_JSON_INDENT)

    async def _save_body(self, cid, messages=None, system_prompt=_UNSET, system_prompt_at=_UNSET, evicted=False):
        """Write one conversation body with a renewed expiry.

        system_prompt/system_prompt_at default to the sentinel _UNSET, meaning "read them from
        self.system_prompts" - true for every caller except _cache_body()'s eviction path, which
        passes the values it already popped out of that cache because by the time this runs they
        are no longer there to look up.
        """
        if not self.storage:
            return False
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                self.dirty.discard(cid)
                return False
            if messages is None:
                if cid not in self.bodies:
                    # A metadata-only change (rename, model, usage) on a conversation whose body
                    # is not cached. Those fields live in the index, which flush() saves
                    # separately; writing a body we do not hold would replace the stored history
                    # with an empty list. Safe to skip, because _cache_body always flushes a
                    # dirty body before evicting it, so an uncached body is never unsaved.
                    self.dirty.discard(cid)
                    return False
                messages = self.bodies[cid]
            if system_prompt is _UNSET or system_prompt_at is _UNSET:
                info = self.system_prompts.get(cid) or {}
                system_prompt = info.get("system_prompt")
                system_prompt_at = info.get("system_prompt_at")
            payload = {
                "version": CONVERSATION_VERSION,
                "id": cid,
                "messages": json.loads(json.dumps(messages)),
                "system_prompt": system_prompt,
                "system_prompt_at": system_prompt_at,
                "last_error": self.last_errors.get(cid),
                "approvals": self.approvals.get(cid, []),
            }
            self.dirty.discard(cid)
        result = await self.storage.save(STORAGE_MODULE, self._body_name(cid), payload, format="json", expiry=self._expiry(), indent=CONVERSATION_JSON_INDENT)
        if evicted:
            self.log("Info: flushed chat conversation {} before evicting it from the body cache".format(cid))
        return result

    async def flush(self, cid=None):
        """Write any dirty conversations and the index."""
        with self.lock:
            targets = [cid] if cid is not None else sorted(self.dirty)
        wrote = False
        for target in targets:
            if await self._save_body(target):
                wrote = True
        await self._save_index()
        return wrote
