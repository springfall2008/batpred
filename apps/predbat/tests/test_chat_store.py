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

from chat_store import BODY_CACHE_SIZE, NEW_CONVERSATION_TITLE, TITLE_MAX_LENGTH, ConversationStore, derive_title, extract_cached_tokens, trim_history


class FakeStorage:
    """An in-memory stand-in for StorageComponent that records expiry and can fake expiry."""

    def __init__(self):
        """Start with an empty store and no recorded calls."""
        self.data = {}
        self.expiry = {}
        self.saves = []
        self.expired = set()

    async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
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


def test_trim_history_zero_means_unlimited(my_predbat):
    """max_history=0 - the shipped default - returns a long conversation in full, untrimmed.

    A naive `len(messages) <= max_history` comparison (the code before this change) trims
    everything down to nothing useful once max_history is 0: `len(messages) <= 0` is False for any
    non-empty conversation, so the function falls through to `index = len(messages) - 0 =
    len(messages)`, which is one past the last valid index and raises IndexError on the very next
    line. This drives a genuinely long history (200 messages, comfortably longer than the old
    default of 40) through trim_history(..., 0) and asserts it comes back complete, not merely that
    no exception was raised - a fix that silently trimmed to some other window would pass a
    no-exception check just as well as a correct one.
    """
    failed = False
    print("**** Testing chat_max_history=0 means unlimited, not a crash or a silent trim ****")
    messages = []
    for index in range(50):
        messages.append({"role": "user", "content": "question {}".format(index)})
        messages.append({"role": "assistant", "content": "answer {}".format(index)})
    if len(messages) != 100:
        print("ERROR: test setup did not build the expected 100-message history")
        return True

    trimmed = trim_history(messages, 0)
    if trimmed != messages:
        print("ERROR: trim_history(messages, 0) returned {} messages, expected all 100 back unchanged".format(len(trimmed)))
        failed = True
    if trimmed is messages:
        print("ERROR: trim_history should return a copy, not alias the input list, even when returning everything")
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


def test_metadata_only_flush_does_not_clobber_body(my_predbat):
    """rename() on a conversation whose body has been evicted must not overwrite it with []."""
    failed = False
    print("**** Testing metadata-only flush does not clobber an uncached body ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())

    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "do not lose me"}))
    asyncio.run(store.flush())

    # Push cid out of the body LRU by touching BODY_CACHE_SIZE other conversations, exactly as a
    # real session would once enough other chats are opened.
    for _ in range(BODY_CACHE_SIZE):
        other = asyncio.run(store.create())
        asyncio.run(store.get_messages(other))
    if cid in store.bodies:
        print("ERROR: test setup failed to evict {} from the body cache".format(cid))
        failed = True

    store.rename(cid, "renamed while body is uncached")
    asyncio.run(store.flush())

    reopened = _store(storage)
    asyncio.run(reopened.load_index())
    reloaded = asyncio.run(reopened.get_messages(cid))
    if not reloaded or reloaded[-1].get("content") != "do not lose me":
        print("ERROR: a metadata-only save clobbered the stored body: {}".format(reloaded))
        failed = True

    return failed


def test_system_prompt_migrates_from_a_body_saved_before_it_existed(my_predbat):
    """A body saved before system_prompt existed has no such key at all; get_system_prompt() reads
    that as (None, None) - exactly the same 'not frozen yet' signal a brand new conversation
    starts with - and once one is set it round-trips through a save and a fresh store reload.

    There is deliberately no special-cased migration branch anywhere in ConversationStore for
    this: a dict.get() on a missing key already returns None, which is the whole story. This test
    pins that the old-format shape (no key at all, not the key present with a None value someone
    might have mis-implemented) actually goes through get() cleanly.
    """
    failed = False
    print("**** Testing system_prompt migration from a body saved before it existed ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "hello from before caching existed"}))
    asyncio.run(store.flush())

    # Overwrite the just-saved body with the old, pre-system_prompt shape: no system_prompt or
    # system_prompt_at key at all, exactly what every conversation saved before this feature has.
    key = ("chat", "conv_{}".format(cid))
    old_shaped = dict(storage.data[key])
    old_shaped.pop("system_prompt", None)
    old_shaped.pop("system_prompt_at", None)
    storage.data[key] = old_shaped

    reopened = _store(storage)
    asyncio.run(reopened.load_index())
    before = asyncio.run(reopened.get_system_prompt(cid))
    if before != (None, None):
        print("ERROR: an old-format body should read as no stored prompt, got {}".format(before))
        failed = True

    if not reopened.set_system_prompt(cid, "a frozen prompt", "2026-08-28T09:15:00+00:00"):
        print("ERROR: set_system_prompt() refused a conversation whose body was just loaded")
        return True
    asyncio.run(reopened.flush(cid))

    reopened_again = _store(storage)
    asyncio.run(reopened_again.load_index())
    after = asyncio.run(reopened_again.get_system_prompt(cid))
    if after != ("a frozen prompt", "2026-08-28T09:15:00+00:00"):
        print("ERROR: the frozen prompt did not survive a save and reload: {}".format(after))
        failed = True

    return failed


def test_system_prompt_survives_body_eviction(my_predbat):
    """A dirty system prompt is flushed alongside its body when the LRU evicts it, not lost or
    silently reset - exercising _cache_body()'s eviction path, which has to read the prompt out of
    self.system_prompts before popping it, since _save_body can no longer look it up once it has.
    """
    failed = False
    print("**** Testing a system prompt survives being evicted from the body cache ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "keep my prompt too"}))
    store.set_system_prompt(cid, "frozen before eviction", "2026-08-28T09:15:00+00:00")

    for _ in range(BODY_CACHE_SIZE):
        other = asyncio.run(store.create())
        asyncio.run(store.get_messages(other))
    if cid in store.bodies:
        print("ERROR: test setup failed to evict {} from the body cache".format(cid))
        return True

    reloaded_prompt, reloaded_at = asyncio.run(store.get_system_prompt(cid))
    if reloaded_prompt != "frozen before eviction" or reloaded_at != "2026-08-28T09:15:00+00:00":
        print("ERROR: the system prompt did not survive eviction: {!r}, {!r}".format(reloaded_prompt, reloaded_at))
        failed = True

    return failed


def test_set_system_prompt_requires_a_cached_body(my_predbat):
    """set_system_prompt() refuses an id whose body is not currently cached, rather than caching a
    prompt with no message list alongside it to be saved - the same shape of guard
    _save_body()'s metadata-only path already relies on for rename/model/usage."""
    failed = False
    print("**** Testing set_system_prompt refuses an uncached conversation ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())

    if store.set_system_prompt("deadbeefdeadbeef", "should not stick", "2026-08-28T09:15:00+00:00"):
        print("ERROR: set_system_prompt() succeeded for an id with no cached body")
        failed = True

    return failed


def test_extract_cached_tokens(my_predbat):
    """extract_cached_tokens() reads the nested OpenRouter shape and tolerates every field being
    absent, rather than raising on a provider that does not report caching at all."""
    failed = False
    print("**** Testing extract_cached_tokens ****")
    cases = [
        ({"prompt_tokens_details": {"cached_tokens": 42}}, 42),
        ({"prompt_tokens_details": {}}, 0),
        ({}, 0),
        ({"prompt_tokens_details": None}, 0),
    ]
    for usage, expected in cases:
        got = extract_cached_tokens(usage)
        if got != expected:
            print("ERROR: extract_cached_tokens({!r}) returned {!r}, expected {!r}".format(usage, got, expected))
            failed = True

    return failed


def test_add_usage_on_deleted_conversation_clears_dirty(my_predbat):
    """add_usage() on a deleted conversation still dirties it, but flush() clears it, not retries forever."""
    failed = False
    print("**** Testing add_usage on a deleted conversation ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.delete(cid))

    store.add_usage(cid, {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.01})
    if cid not in store.dirty:
        print("ERROR: add_usage() did not mark the deleted conversation dirty")
        failed = True

    asyncio.run(store.flush())
    if cid in store.dirty:
        print("ERROR: {} stayed in store.dirty after flush(), so every future flush() would retry it forever".format(cid))
        failed = True

    return failed


def test_get_meta_deep_copies_usage_total(my_predbat):
    """get_meta()'s usage_total is a copy, so a caller mutating it cannot corrupt the store's state."""
    failed = False
    print("**** Testing get_meta usage_total isolation ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    store.add_usage(cid, {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.02})

    meta = store.get_meta(cid)
    if meta["usage_total"] is store.index[cid]["usage_total"]:
        print("ERROR: get_meta()'s usage_total is the same object as the store's live entry")
        failed = True

    meta["usage_total"]["cost"] = 999
    meta["usage_total"]["prompt_tokens"] = 999
    reread = store.get_meta(cid)
    if reread["usage_total"]["cost"] == 999 or reread["usage_total"]["prompt_tokens"] == 999:
        print("ERROR: mutating the dict returned by get_meta() changed the stored usage_total: {}".format(reread))
        failed = True

    return failed


def test_add_usage_accumulates_cached_tokens(my_predbat):
    """add_usage() accumulates cached_tokens out of the nested prompt_tokens_details shape, the
    same way it already accumulates prompt_tokens, completion_tokens and cost - across several
    calls, and tolerating a usage object that does not report caching at all."""
    failed = False
    print("**** Testing add_usage accumulates cached_tokens ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())

    store.add_usage(cid, {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.001, "prompt_tokens_details": {"cached_tokens": 80}})
    store.add_usage(cid, {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0005})

    total = store.get_meta(cid)["usage_total"]
    if total.get("cached_tokens") != 80:
        print("ERROR: expected cached_tokens=80 after one usage object reported caching and one did not, got {}".format(total))
        failed = True
    if total.get("prompt_tokens") != 150:
        print("ERROR: cached_tokens tracking broke the existing prompt_tokens accumulation: {}".format(total))
        failed = True

    return failed


def test_add_usage_tracks_last_prompt_tokens_not_cumulative(my_predbat):
    """entry['last_prompt_tokens'] holds the most recent completion's prompt_tokens, overwritten
    on every call, not the running sum add_usage() also accumulates onto usage_total.

    The Chat tab's context-size footer needs 'how big is the request I am about to send', not
    'what has this conversation cost in total' - usage_total.prompt_tokens answers the second
    question and would read ever larger the longer a conversation runs even though the request
    itself might have stayed the same size. Two calls with deliberately different prompt sizes
    (1000, then 4000) are used so a test - or an implementation - that read the cumulative total
    (5000) instead of the most recent value (4000) fails rather than passing by coincidence.
    """
    failed = False
    print("**** Testing add_usage tracks the last turn's prompt_tokens, not the cumulative total ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())

    store.add_usage(cid, {"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.01})
    store.add_usage(cid, {"prompt_tokens": 4000, "completion_tokens": 80, "cost": 0.02})

    meta = store.get_meta(cid)
    if meta.get("last_prompt_tokens") != 4000:
        print("ERROR: last_prompt_tokens is {}, expected 4000 (the most recent call's prompt size, not the 5000 cumulative total)".format(meta.get("last_prompt_tokens")))
        failed = True
    if meta["usage_total"]["prompt_tokens"] != 5000:
        print("ERROR: usage_total.prompt_tokens is {}, expected the cumulative 5000 - this must keep working alongside last_prompt_tokens".format(meta["usage_total"]["prompt_tokens"]))
        failed = True

    return failed


def test_selected_model_persists(my_predbat):
    """The picked model is written with the index and read back on the next load.

    openrouter_default_model is optional, so on an install that has never set it the user's pick
    is the only model there is. If it lived in memory only, every Predbat restart would drop them
    back to "no model chosen" with a conversation history that plainly used one.

    Mutation check: dropping "selected_model" from the _save_index payload fails the reload
    assertion below.
    """
    failed = False
    print("**** Testing the selected model survives a reload ****")

    storage = FakeStorage()
    store = ConversationStore(storage, my_predbat.log)
    asyncio.run(store.load_index())

    if store.get_selected_model("openrouter") is not None:
        print("ERROR: a fresh store already has a model selected: {}".format(store.get_selected_model("openrouter")))
        failed = True

    cid = asyncio.run(store.create())
    store.set_selected_model("anthropic/claude-sonnet-5", "openrouter")
    asyncio.run(store.flush(cid))

    # A second store over the same storage is what a restart looks like.
    reloaded = ConversationStore(storage, my_predbat.log)
    asyncio.run(reloaded.load_index())
    if reloaded.get_selected_model("openrouter") != "anthropic/claude-sonnet-5":
        print("ERROR: the selected model did not survive a reload, got {}".format(reloaded.get_selected_model("openrouter")))
        failed = True

    # Clearing it back to nothing must also persist, rather than leaving the old pin in place.
    reloaded.set_selected_model(None, "openrouter")
    asyncio.run(reloaded.flush(cid))
    again = ConversationStore(storage, my_predbat.log)
    asyncio.run(again.load_index())
    if again.get_selected_model("openrouter") is not None:
        print("ERROR: clearing the selected model did not persist, got {}".format(again.get_selected_model("openrouter")))
        failed = True

    # Two providers keep separate choices. A single remembered id would point at an OpenRouter
    # model the moment someone switched to Ollama, leaving the picker set to something not there.
    again.set_selected_model("openai/gpt-4o-mini", "openrouter")
    again.set_selected_model("qwen3:latest", "ollama")
    if again.get_selected_model("openrouter") != "openai/gpt-4o-mini" or again.get_selected_model("ollama") != "qwen3:latest":
        print("ERROR: providers do not keep separate remembered models: {}".format(again.selected_model))
        failed = True
    if again.get_selected_model("never-configured") is not None:
        print("ERROR: an unknown provider inherited another's remembered model")
        failed = True

    return failed


def test_conversations_are_written_indented(my_predbat):
    """Saved conversations are written as indented JSON, not one long line.

    They are read by people - attached to bug reports, or opened to check what the model was
    actually sent. A single 100KB line is unreadable in any editor. The machine-only caches
    elsewhere in Storage stay compact, which is why indent is opt-in rather than a Storage-wide
    default.

    Mutation check: dropping indent= from either save call fails this.
    """
    failed = False
    print("**** Testing conversations are saved as indented JSON ****")

    saved = {}

    class RecordingStorage(FakeStorage):
        """Records the indent each save was asked for, then behaves as normal."""

        async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
            """Record the indent and delegate."""
            saved[filename] = indent
            return await FakeStorage.save(self, module, filename, data, format=format, expiry=expiry, indent=indent)

    store = ConversationStore(RecordingStorage(), my_predbat.log)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "hello"}))
    asyncio.run(store.flush(cid))

    if not saved:
        print("ERROR: nothing was saved, so the test proves nothing")
        return True
    for filename, indent in saved.items():
        if not indent:
            print("ERROR: {} was saved without an indent, so it is one long line: {!r}".format(filename, indent))
            failed = True

    return failed


def test_superseded_error_is_not_replayed(my_predbat):
    """An error a later turn has superseded is not handed back for replay.

    An error is deliberately not a message - it is kept out of the transcript so it can never be
    replayed to the model - which leaves a client rebuilding the conversation with no position to
    put it back at, so it appended it to the end. A failure from several turns ago then reappeared
    below the successful reply that followed it, on every single reload.

    The message count recorded with the error is what distinguishes "this is the latest thing that
    happened", which belongs at the end because that is where it happened, from "a turn has since
    succeeded", which does not belong on screen at all.

    Mutation check: dropping the count comparison returns the stale error again.
    """
    failed = False
    print("**** Testing a superseded error is not replayed ****")

    store = ConversationStore(FakeStorage(), my_predbat.log)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.append(cid, {"role": "user", "content": "first"}))
    asyncio.run(store.append(cid, {"role": "assistant", "content": "reply"}))

    # A turn fails here, with two messages already stored.
    store.set_last_error(cid, "Rate limited", "code 429", message_count=store.message_count(cid))

    # Nothing has happened since, so it is the current state and belongs on screen.
    if store.get_last_error(cid, message_count=2) is None:
        print("ERROR: the latest failure was suppressed")
        failed = True

    # A later turn succeeds. The failure is now history, and appending it after everything would
    # put it below the reply that came after it.
    asyncio.run(store.append(cid, {"role": "user", "content": "second"}))
    asyncio.run(store.append(cid, {"role": "assistant", "content": "worked this time"}))
    if store.get_last_error(cid, message_count=4) is not None:
        print("ERROR: a superseded error is still replayed, and would land at the bottom out of order")
        failed = True

    # Without a count the caller gets whatever is stored - used where the raw record is wanted.
    if store.get_last_error(cid) is None:
        print("ERROR: the stored record is unreachable without a count")
        failed = True

    # A record written before the count existed cannot be placed, so it is treated as history
    # rather than resurrected at the end.
    store.last_errors[cid] = {"message": "old", "detail": None, "at": "2026-01-01T00:00:00+00:00"}
    if store.get_last_error(cid, message_count=4) is not None:
        print("ERROR: a countless legacy error was replayed anyway")
        failed = True

    return failed


def run_chat_store_tests(my_predbat):
    """Run every conversation store test, returning True if any of them failed."""
    failed = False
    failed |= test_selected_model_persists(my_predbat)
    failed |= test_conversations_are_written_indented(my_predbat)
    failed |= test_superseded_error_is_not_replayed(my_predbat)
    failed |= test_create_and_list(my_predbat)
    failed |= test_expiry_is_rolling(my_predbat)
    failed |= test_delete_is_a_flag(my_predbat)
    failed |= test_index_self_heals(my_predbat)
    failed |= test_unknown_version_discarded(my_predbat)
    failed |= test_lru_flushes_before_eviction(my_predbat)
    failed |= test_pruning(my_predbat)
    failed |= test_derive_title(my_predbat)
    failed |= test_trim_history_keeps_tool_groups_intact(my_predbat)
    failed |= test_trim_history_zero_means_unlimited(my_predbat)
    failed |= test_snapshot_is_a_safe_copy(my_predbat)
    failed |= test_metadata_only_flush_does_not_clobber_body(my_predbat)
    failed |= test_system_prompt_migrates_from_a_body_saved_before_it_existed(my_predbat)
    failed |= test_system_prompt_survives_body_eviction(my_predbat)
    failed |= test_set_system_prompt_requires_a_cached_body(my_predbat)
    failed |= test_extract_cached_tokens(my_predbat)
    failed |= test_add_usage_on_deleted_conversation_clears_dirty(my_predbat)
    failed |= test_add_usage_accumulates_cached_tokens(my_predbat)
    failed |= test_add_usage_tracks_last_prompt_tokens_not_cumulative(my_predbat)
    failed |= test_get_meta_deep_copies_usage_total(my_predbat)
    return failed
