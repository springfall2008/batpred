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
    """
    if len(messages) <= max_history:
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
        if isinstance(payload, dict):
            if payload.get("version") != CONVERSATION_VERSION:
                self.log("Warn: chat index version {} is not {}, discarding it".format(payload.get("version"), CONVERSATION_VERSION))
            else:
                entries = payload.get("conversations") or []

        healed = False
        with self.lock:
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
        entry = {"id": cid, "title": NEW_CONVERSATION_TITLE, "created": now, "updated": now, "deleted": False, "model": model, "message_count": 0, "usage_total": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "cached_tokens": 0}}
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

    def set_model(self, cid, model):
        """Set a conversation's model override, or clear it back to the default with None."""
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return False
            entry["model"] = model
            self.dirty.add(cid)
        return True

    def add_usage(self, cid, usage):
        """Accumulate token, cost and cached-token usage onto a conversation.

        usage is the raw OpenRouter usage object for one completion - cached_tokens is read out
        of its nested prompt_tokens_details via extract_cached_tokens(), the same helper chat.py
        uses for the per-turn 'usage' event, so the running total and the event can never disagree
        about what counts as a cache hit.
        """
        with self.lock:
            entry = self.index.get(cid)
            if entry is None:
                return
            total = entry.setdefault("usage_total", {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "cached_tokens": 0})
            for key in ("prompt_tokens", "completion_tokens", "cost"):
                total[key] = total.get(key, 0) + (usage.get(key) or 0)
            total["cached_tokens"] = total.get("cached_tokens", 0) + extract_cached_tokens(usage)
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

    async def _save_index(self):
        """Write the conversation index with a renewed expiry."""
        if not self.storage:
            return False
        with self.lock:
            payload = {"version": CONVERSATION_VERSION, "conversations": [dict(entry) for entry in self.index.values()]}
        return await self.storage.save(STORAGE_MODULE, INDEX_FILENAME, payload, format="json", expiry=self._expiry())

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
            payload = {"version": CONVERSATION_VERSION, "id": cid, "messages": json.loads(json.dumps(messages)), "system_prompt": system_prompt, "system_prompt_at": system_prompt_at}
            self.dirty.discard(cid)
        result = await self.storage.save(STORAGE_MODULE, self._body_name(cid), payload, format="json", expiry=self._expiry())
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
