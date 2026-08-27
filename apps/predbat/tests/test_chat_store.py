# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the chat conversation store.

Covers the lifecycle a user drives - create, title, rename, delete - and the three behaviours
that are easy to get quietly wrong: the rolling expiry, the deleted flag standing in for a
storage delete that does not exist, and history trimming never orphaning a tool message.
"""

import asyncio
from datetime import datetime, timezone

from chat_store import BODY_CACHE_SIZE, NEW_CONVERSATION_TITLE, TITLE_MAX_LENGTH, ConversationStore, derive_title, trim_history


class FakeStorage:
    """An in-memory stand-in for StorageComponent that records expiry and can fake expiry."""

    def __init__(self):
        """Start with an empty store and no recorded calls."""
        self.data = {}
        self.expiry = {}
        self.saves = []
        self.expired = set()

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a save, keeping the payload and the expiry it was given."""
        key = (module, filename)
        self.data[key] = data
        self.expiry[key] = expiry
        self.saves.append(key)
        return True

    async def load(self, module, filename):
        """Return stored data, or None when the entry is missing or marked expired."""
        key = (module, filename)
        if key in self.expired:
            return None
        return self.data.get(key)


def _store(storage, **kwargs):
    """Build a ConversationStore over a fake storage with test-friendly defaults."""
    return ConversationStore(storage, print, **kwargs)


def test_create_and_list(my_predbat):
    """A new conversation gets a hex id, the placeholder title, and appears in the listing."""
    failed = False
    print("**** Testing conversation create and list ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())

    cid = asyncio.run(store.create())
    if len(cid) != 16 or any(char not in "0123456789abcdef" for char in cid):
        print("ERROR: conversation id {!r} is not 16 hex characters".format(cid))
        failed = True

    listed = store.list_conversations()
    if len(listed) != 1 or listed[0]["id"] != cid:
        print("ERROR: created conversation missing from the listing: {}".format(listed))
        failed = True
    if listed[0]["title"] != NEW_CONVERSATION_TITLE:
        print("ERROR: new conversation title is {!r}, expected {!r}".format(listed[0]["title"], NEW_CONVERSATION_TITLE))
        failed = True

    second = asyncio.run(store.create())
    if second == cid:
        print("ERROR: two conversations were given the same id")
        failed = True

    return failed


def test_expiry_is_rolling(my_predbat):
    """Every save carries an expiry expiry_days ahead, and a later save renews it."""
    failed = False
    print("**** Testing rolling conversation expiry ****")
    storage = FakeStorage()
    store = _store(storage, expiry_days=30)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())

    key = ("chat", "conv_{}".format(cid))
    first = storage.expiry.get(key)
    if first is None:
        print("ERROR: conversation body was saved without an expiry")
        failed = True
    else:
        days = (first - datetime.now(timezone.utc)).total_seconds() / 86400.0
        if not 29.5 < days < 30.5:
            print("ERROR: expiry is {:.2f} days ahead, expected about 30".format(days))
            failed = True
        if first.tzinfo is None:
            print("ERROR: expiry is not timezone-aware, which storage requires")
            failed = True

    asyncio.run(store.append(cid, {"role": "user", "content": "hello"}))
    asyncio.run(store.flush(cid))
    renewed = storage.expiry.get(key)
    if renewed is None or renewed <= first:
        print("ERROR: a later save did not renew the expiry ({} -> {})".format(first, renewed))
        failed = True

    if storage.expiry.get(("chat", "index")) is None:
        print("ERROR: the index was saved without an expiry, so it would outlive the bodies")
        failed = True

    return failed


def test_delete_is_a_flag(my_predbat):
    """Deleting hides a conversation, refuses it by id, and stops it being re-saved."""
    failed = False
    print("**** Testing conversation deletion ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.flush())

    saves_before = len(storage.saves)
    if not asyncio.run(store.delete(cid)):
        print("ERROR: delete() reported failure for a live conversation")
        failed = True

    if store.list_conversations():
        print("ERROR: a deleted conversation is still listed")
        failed = True
    if store.get_meta(cid) is not None:
        print("ERROR: get_meta() still resolves a deleted conversation")
        failed = True

    asyncio.run(store.flush())
    body_saves = [key for key in storage.saves[saves_before:] if key == ("chat", "conv_{}".format(cid))]
    if body_saves:
        print("ERROR: a deleted conversation's body was re-saved, so it would never expire")
        failed = True

    if asyncio.run(store.delete("deadbeefdeadbeef")):
        print("ERROR: delete() reported success for an unknown id")
        failed = True

    return failed


def test_index_self_heals(my_predbat):
    """An index entry whose body has expired is dropped on load rather than left dangling."""
    failed = False
    print("**** Testing index self-heal ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    kept = asyncio.run(store.create())
    gone = asyncio.run(store.create())
    asyncio.run(store.flush())

    storage.expired.add(("chat", "conv_{}".format(gone)))

    reopened = _store(storage)
    asyncio.run(reopened.load_index())
    ids = [entry["id"] for entry in reopened.list_conversations()]
    if gone in ids:
        print("ERROR: expired conversation {} survived the index reload".format(gone))
        failed = True
    if kept not in ids:
        print("ERROR: live conversation {} was dropped by the self-heal".format(kept))
        failed = True

    return failed


def test_unknown_version_discarded(my_predbat):
    """A payload from a future version is discarded rather than half-parsed."""
    failed = False
    print("**** Testing unknown payload version handling ****")
    storage = FakeStorage()
    storage.data[("chat", "index")] = {"version": 99, "conversations": [{"id": "aaaabbbbccccdddd", "title": "from the future"}]}

    store = _store(storage)
    asyncio.run(store.load_index())
    if store.list_conversations():
        print("ERROR: an index with an unknown version was loaded anyway")
        failed = True

    return failed


def test_lru_flushes_before_eviction(my_predbat):
    """Loading past the cache size evicts the oldest body, flushing it first if dirty."""
    failed = False
    print("**** Testing body LRU eviction ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())

    ids = [asyncio.run(store.create()) for _ in range(BODY_CACHE_SIZE + 1)]
    asyncio.run(store.append(ids[0], {"role": "user", "content": "keep me"}))
    for cid in ids[1:]:
        asyncio.run(store.get_messages(cid))

    if ids[0] in store.bodies:
        print("ERROR: the oldest body was not evicted past the cache size")
        failed = True

    reloaded = asyncio.run(store.get_messages(ids[0]))
    if not reloaded or reloaded[-1].get("content") != "keep me":
        print("ERROR: the evicted body lost its unflushed message: {}".format(reloaded))
        failed = True

    return failed


def test_pruning(my_predbat):
    """Past the cap the least recently updated is marked deleted, never the protected one."""
    failed = False
    print("**** Testing conversation pruning ****")
    storage = FakeStorage()
    store = _store(storage, max_conversations=3)
    asyncio.run(store.load_index())

    ids = [asyncio.run(store.create()) for _ in range(3)]
    store.set_title(ids[0], "oldest")
    asyncio.run(store.create(protect_id=ids[0]))

    listed = [entry["id"] for entry in store.list_conversations()]
    if len(listed) > 3:
        print("ERROR: pruning left {} conversations, cap is 3".format(len(listed)))
        failed = True
    if ids[0] not in listed:
        print("ERROR: the protected conversation was pruned")
        failed = True
    if ids[1] in listed:
        print("ERROR: the least recently updated unprotected conversation survived pruning")
        failed = True

    return failed


def test_derive_title(my_predbat):
    """Titles collapse whitespace, truncate, and never come back empty."""
    failed = False
    print("**** Testing title derivation ****")
    cases = [
        ("  why   is it\ncharging at 3am? ", "why is it charging at 3am?"),
        ("x" * 200, "x" * TITLE_MAX_LENGTH),
        ("", NEW_CONVERSATION_TITLE),
        ("   ", NEW_CONVERSATION_TITLE),
    ]
    for text, expected in cases:
        got = derive_title(text)
        if got != expected:
            print("ERROR: derive_title({!r}) returned {!r}, expected {!r}".format(text, got, expected))
            failed = True
        if len(got) > TITLE_MAX_LENGTH:
            print("ERROR: derive_title({!r}) exceeded the length cap".format(text))
            failed = True
    return failed


def test_trim_history_keeps_tool_groups_intact(my_predbat):
    """Trimming never leaves a tool message without the assistant tool_calls that asked for it."""
    failed = False
    print("**** Testing history trimming ****")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "get_plan", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
        {"role": "assistant", "content": "second answer"},
    ]

    trimmed = trim_history(messages, 3)
    if trimmed and trimmed[0].get("role") != "user":
        print("ERROR: trimmed history starts with {!r}, not a user message".format(trimmed[0].get("role")))
        failed = True

    ids = {message["tool_calls"][0]["id"] for message in trimmed if message.get("tool_calls")}
    answered = {message["tool_call_id"] for message in trimmed if message.get("role") == "tool"}
    if answered - ids:
        print("ERROR: orphaned tool results in the trimmed window: {}".format(answered - ids))
        failed = True

    if trim_history(messages, 99) != messages:
        print("ERROR: trimming below the cap changed the conversation")
        failed = True

    no_boundary = [{"role": "assistant", "content": str(index)} for index in range(10)]
    if trim_history(no_boundary, 3) != no_boundary:
        print("ERROR: a window with no user boundary should keep the whole conversation")
        failed = True

    return failed


def test_snapshot_is_a_safe_copy(my_predbat):
    """snapshot() matches get_messages(), copies rather than aliases, and is None for a dead id."""
    failed = False
    print("**** Testing conversation snapshot ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "hello"}))

    live = asyncio.run(store.get_messages(cid))
    snapshot = asyncio.run(store.snapshot(cid))
    if snapshot != live:
        print("ERROR: snapshot() returned {!r}, expected it to match get_messages() {!r}".format(snapshot, live))
        failed = True

    snapshot.append({"role": "user", "content": "mutated after the fact"})
    live_again = asyncio.run(store.get_messages(cid))
    if live_again != live:
        print("ERROR: mutating the snapshot changed the stored conversation: {}".format(live_again))
        failed = True
    if len(live_again) != 1:
        print("ERROR: expected the stored conversation to still have 1 message, got {}".format(len(live_again)))
        failed = True

    if asyncio.run(store.snapshot("deadbeefdeadbeef")) is not None:
        print("ERROR: snapshot() of an unknown id should be None")
        failed = True

    asyncio.run(store.delete(cid))
    if asyncio.run(store.snapshot(cid)) is not None:
        print("ERROR: snapshot() of a deleted conversation should be None")
        failed = True

    return failed


def run_chat_store_tests(my_predbat):
    """Run every conversation store test, returning True if any of them failed."""
    failed = False
    failed |= test_create_and_list(my_predbat)
    failed |= test_expiry_is_rolling(my_predbat)
    failed |= test_delete_is_a_flag(my_predbat)
    failed |= test_index_self_heals(my_predbat)
    failed |= test_unknown_version_discarded(my_predbat)
    failed |= test_lru_flushes_before_eviction(my_predbat)
    failed |= test_pruning(my_predbat)
    failed |= test_derive_title(my_predbat)
    failed |= test_trim_history_keeps_tool_groups_intact(my_predbat)
    failed |= test_snapshot_is_a_safe_copy(my_predbat)
    return failed
