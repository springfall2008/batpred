# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the rolling debug-history snapshot buffer (#4417)."""

import asyncio
import datetime
import io
import os
import tempfile
import shutil
import tarfile

from debug_history import INDEX_NAME, STORAGE_MODULE, _discard_snapshot, _legacy_snapshot_key, annotate_steps_back, build_archive, capture_snapshot, list_snapshots, load_all_snapshots, load_snapshot, snapshot_filename
from storage import StorageLocalFiles


class FakeStorage:
    """An in-memory stand-in for the Storage component.

    ``store`` backs the generic save()/load() surface, used only for the ring's own
    index. ``debug_copies`` backs save_debug_copy()/load_debug_copy()/delete_debug_copy(),
    used for a snapshot's actual text (see #4720) - the two are deliberately separate
    dicts, mirroring how the real backend keeps the index in config_root/cache/ and
    snapshot content in config_root/debug/.
    """

    def __init__(self):
        """Start with nothing stored."""
        self.store = {}
        self.save_calls = []
        self.debug_copies = {}
        self.debug_copies_deleted = []

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a saved value, the format it was saved with, and that a save happened."""
        self.store[(module, filename)] = (data, format)
        self.save_calls.append((module, filename))

    async def load(self, module, filename):
        """Return a stored value, or None."""
        entry = self.store.get((module, filename))
        return entry[0] if entry else None

    async def save_debug_copy(self, filename, text):
        """Record a debug/ write, matching StorageLocalFiles.save_debug_copy()."""
        self.debug_copies[filename] = text
        return True

    async def load_debug_copy(self, filename):
        """Return previously written debug/ text, or None, matching StorageLocalFiles.load_debug_copy()."""
        return self.debug_copies.get(filename)

    async def delete_debug_copy(self, filename):
        """Record a debug/ deletion, matching StorageLocalFiles.delete_debug_copy()."""
        self.debug_copies_deleted.append(filename)
        self.debug_copies.pop(filename, None)


def sample_yaml_text(marker):
    """Return debug-yaml-shaped text (colons, nesting, a newline) with a distinguishing marker."""
    return "CONFIG_ITEMS:\n- name: marker\n  value: {}\nnested:\n  a: 1\n".format(marker)


def test_debug_history(my_predbat):
    """Verify capture, listing, loading, pruning (both eviction code paths), and step-back annotation."""
    failed = False
    print("**** Testing debug_history ****")

    now = datetime.datetime(2026, 8, 4, 14, 0, 0, tzinfo=datetime.timezone.utc)

    print("Test: a captured snapshot appears in the index and can be loaded back verbatim")
    storage = FakeStorage()
    snapshot_id = asyncio.run(capture_snapshot(storage, sample_yaml_text(1), now, max_count=15))
    index = asyncio.run(list_snapshots(storage))
    if len(index) != 1 or index[0]["id"] != snapshot_id:
        print("  ERROR: expected one indexed snapshot, got {}".format(index))
        failed = True
    loaded = asyncio.run(load_snapshot(storage, snapshot_id))
    if loaded != sample_yaml_text(1):
        print("  ERROR: loaded snapshot should match what was saved verbatim, got {!r}".format(loaded))
        failed = True

    print("Test: snapshot text is written via save_debug_copy(), verbatim, not through the generic cache store (#4720)")
    if storage.debug_copies.get(snapshot_filename(snapshot_id)) != sample_yaml_text(1):
        print("  ERROR: snapshot text should be in debug_copies verbatim, got {!r}".format(storage.debug_copies.get(snapshot_filename(snapshot_id))))
        failed = True
    if (STORAGE_MODULE, "snapshot_{}".format(snapshot_id)) in storage.store:
        print("  ERROR: snapshot text should not also be duplicated into the generic cache store")
        failed = True

    print("Test: the index is newest-first")
    later = now + datetime.timedelta(hours=3)
    snapshot_id_2 = asyncio.run(capture_snapshot(storage, sample_yaml_text(2), later, max_count=15))
    index = asyncio.run(list_snapshots(storage))
    if [entry["id"] for entry in index] != [snapshot_id_2, snapshot_id]:
        print("  ERROR: expected newest first, got {}".format([entry["id"] for entry in index]))
        failed = True

    print("Test: two captures in the same calendar minute keep only the newer, deleting the older (snapshot ids are floored to the minute, so two captures in the same minute would otherwise collide on the same id/filename in the index and download route)")
    minute_storage = FakeStorage()
    moment = datetime.datetime(2026, 8, 6, 8, 20, 0, tzinfo=datetime.timezone.utc)
    older_id = asyncio.run(capture_snapshot(minute_storage, sample_yaml_text("older"), moment, max_count=15))
    newer_id = asyncio.run(capture_snapshot(minute_storage, sample_yaml_text("newer"), moment + datetime.timedelta(seconds=37), max_count=15))
    if older_id == newer_id:
        print("  ERROR: test setup bug - the two captures should have distinct ids, got the same {!r} twice".format(older_id))
        failed = True
    minute_index = asyncio.run(list_snapshots(minute_storage))
    if len(minute_index) != 1 or minute_index[0]["id"] != newer_id:
        print("  ERROR: expected only the newer same-minute capture to survive, got {}".format(minute_index))
        failed = True
    if asyncio.run(load_snapshot(minute_storage, older_id)) is not None:
        print("  ERROR: the older same-minute capture should have been discarded from storage, not just the index")
        failed = True
    if snapshot_filename(older_id) not in minute_storage.debug_copies_deleted:
        print("  ERROR: expected the older same-minute capture to go through the normal discard path, deletions were {}".format(minute_storage.debug_copies_deleted))
        failed = True

    print("Test: captures in different minutes are unaffected by the same-minute dedup")
    distinct_storage = FakeStorage()
    for offset_minutes in (0, 1, 2):
        asyncio.run(capture_snapshot(distinct_storage, sample_yaml_text(offset_minutes), moment + datetime.timedelta(minutes=offset_minutes), max_count=15))
    if len(asyncio.run(list_snapshots(distinct_storage))) != 3:
        print("  ERROR: three captures a minute apart should all survive, got {}".format(asyncio.run(list_snapshots(distinct_storage))))
        failed = True

    print("Test: max_age prunes a snapshot older than the window even though max_count has not been reached")
    age_storage = FakeStorage()
    old_id = asyncio.run(capture_snapshot(age_storage, sample_yaml_text("ancient"), now, max_count=15))
    new_id = asyncio.run(capture_snapshot(age_storage, sample_yaml_text("recent"), now + datetime.timedelta(hours=50), max_count=15, max_age=datetime.timedelta(hours=45)))
    age_index = asyncio.run(list_snapshots(age_storage))
    if len(age_index) != 1 or age_index[0]["id"] != new_id:
        print("  ERROR: expected the too-old snapshot pruned by max_age despite being well under max_count, got {}".format(age_index))
        failed = True
    if asyncio.run(load_snapshot(age_storage, old_id)) is not None:
        print("  ERROR: the too-old snapshot should have been discarded from storage, not just the index")
        failed = True

    print("Test: max_age does not prune anything still within the window")
    within_storage = FakeStorage()
    asyncio.run(capture_snapshot(within_storage, sample_yaml_text("still fresh"), now, max_count=15))
    asyncio.run(capture_snapshot(within_storage, sample_yaml_text("later"), now + datetime.timedelta(hours=10), max_count=15, max_age=datetime.timedelta(hours=45)))
    if len(asyncio.run(list_snapshots(within_storage))) != 2:
        print("  ERROR: neither capture is older than the 45h window yet, both should survive, got {}".format(asyncio.run(list_snapshots(within_storage))))
        failed = True

    print("Test: max_age=None (the default) does no age-based pruning at all")
    no_age_storage = FakeStorage()
    asyncio.run(capture_snapshot(no_age_storage, sample_yaml_text("very old"), now, max_count=15))
    asyncio.run(capture_snapshot(no_age_storage, sample_yaml_text("much later"), now + datetime.timedelta(days=30), max_count=15))
    if len(asyncio.run(list_snapshots(no_age_storage))) != 2:
        print("  ERROR: with no max_age given, only max_count should apply - both should survive, got {}".format(asyncio.run(list_snapshots(no_age_storage))))
        failed = True

    print("Test: pruning to a configured count (not a fixed constant) evicts the oldest and deletes its debug/ file (#4720)")
    storage = FakeStorage()
    max_count = 5
    ids = []
    for offset in range(max_count + 2):
        this_time = now + datetime.timedelta(hours=offset)
        ids.append(asyncio.run(capture_snapshot(storage, sample_yaml_text(offset), this_time, max_count=max_count)))
    index = asyncio.run(list_snapshots(storage))
    if len(index) != max_count:
        print("  ERROR: the ring should hold {} snapshots, got {}".format(max_count, len(index)))
        failed = True
    if ids[0] in [entry["id"] for entry in index]:
        print("  ERROR: the oldest snapshot should have been evicted from the index")
        failed = True
    if snapshot_filename(ids[0]) not in storage.debug_copies_deleted:
        print("  ERROR: the evicted snapshot's debug/ file should have been deleted too, deletions were {}".format(storage.debug_copies_deleted))
        failed = True
    if asyncio.run(load_snapshot(storage, ids[0])) is not None:
        print("  ERROR: an evicted snapshot should no longer load")
        failed = True
    surviving_id = ids[-1]
    if storage.debug_copies.get(snapshot_filename(surviving_id)) != sample_yaml_text(max_count + 1):
        print("  ERROR: a surviving snapshot's debug/ file should hold its yaml text verbatim, got {!r}".format(storage.debug_copies.get(snapshot_filename(surviving_id))))
        failed = True

    print("Test: annotate_steps_back adds 0,1,2... in order without mutating its input")
    storage = FakeStorage()
    for offset in range(3):
        asyncio.run(capture_snapshot(storage, sample_yaml_text(offset), now + datetime.timedelta(hours=offset), max_count=15))
    index = asyncio.run(list_snapshots(storage))
    index_copy = [dict(entry) for entry in index]
    annotated = annotate_steps_back(index)
    if [entry["steps_back"] for entry in annotated] != [0, 1, 2]:
        print("  ERROR: expected steps_back [0, 1, 2], got {}".format([entry.get("steps_back") for entry in annotated]))
        failed = True
    if index != index_copy:
        print("  ERROR: annotate_steps_back should not mutate its input, got {} expected {}".format(index, index_copy))
        failed = True

    print("Test: a malformed or corrupt index reads as an empty list rather than raising")
    corrupt_storage = FakeStorage()
    asyncio.run(corrupt_storage.save(STORAGE_MODULE, "snapshots_index", "not a list", format="json"))
    if asyncio.run(list_snapshots(corrupt_storage)) != []:
        print("  ERROR: a corrupt index should read as an empty list")
        failed = True

    print("Test: loading an unknown snapshot id returns None rather than raising")
    storage = FakeStorage()
    if asyncio.run(load_snapshot(storage, "does-not-exist")) is not None:
        print("  ERROR: an unknown snapshot id should give None")
        failed = True

    print("Test: 'latest' (and a falsy id) resolves to the newest snapshot")
    storage = FakeStorage()
    asyncio.run(capture_snapshot(storage, sample_yaml_text("first"), now, max_count=15))
    asyncio.run(capture_snapshot(storage, sample_yaml_text("newest"), now + datetime.timedelta(hours=1), max_count=15))
    if asyncio.run(load_snapshot(storage, "latest")) != sample_yaml_text("newest"):
        print("  ERROR: id='latest' should resolve to the newest snapshot")
        failed = True
    if asyncio.run(load_snapshot(storage, None)) != sample_yaml_text("newest"):
        print("  ERROR: a falsy id should also resolve to the newest snapshot")
        failed = True
    if asyncio.run(load_snapshot(storage, "")) != sample_yaml_text("newest"):
        print("  ERROR: an empty-string id should also resolve to the newest snapshot")
        failed = True

    print("Test: 'latest' with nothing stored returns None rather than raising")
    empty_storage = FakeStorage()
    if asyncio.run(load_snapshot(empty_storage, "latest")) is not None:
        print("  ERROR: 'latest' with no snapshots should give None")
        failed = True

    print("Test: no storage component available is handled gracefully everywhere")
    if asyncio.run(list_snapshots(None)) != []:
        print("  ERROR: list_snapshots(None) should give an empty list")
        failed = True
    if asyncio.run(load_snapshot(None, "anything")) is not None:
        print("  ERROR: load_snapshot(None, ...) should give None")
        failed = True
    if asyncio.run(capture_snapshot(None, sample_yaml_text(1), now, max_count=15)) is not None:
        print("  ERROR: capture_snapshot(None, ...) should give None rather than raising")
        failed = True

    print("Test: snapshot text round-trips genuine YAML-syntax content (colons, nesting, newlines) through a real Storage backend, unaltered")
    tmpdir = tempfile.mkdtemp()
    try:
        log_messages = []
        real_storage = StorageLocalFiles(tmpdir, lambda msg: log_messages.append(msg))
        yaml_like_text = sample_yaml_text("real-backend")
        real_id = asyncio.run(capture_snapshot(real_storage, yaml_like_text, now, max_count=15))
        real_loaded = asyncio.run(load_snapshot(real_storage, real_id))
        if real_loaded != yaml_like_text:
            print("  ERROR: real Storage round-trip should return the exact original text, got {!r} expected {!r}".format(real_loaded, yaml_like_text))
            failed = True
        # save_debug_copy() has no format/serialisation step to trip over (unlike a generic
        # storage.save(..., format="yaml") call, which would re-escape this as a quoted scalar) -
        # confirm the colons/indentation genuinely survived the round trip through real disk I/O.
        if ":" not in real_loaded or "\n" not in real_loaded:
            print("  ERROR: round-tripped text lost its YAML structure (colons/newlines), got {!r}".format(real_loaded))
            failed = True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("Test: an install upgrading straight from before #4720 still loads its old snapshots, and eviction reaps the leftover legacy cache/ file from disk (real Storage backend)")
    tmpdir = tempfile.mkdtemp()
    try:
        real_storage = StorageLocalFiles(tmpdir, lambda msg: None)
        pre_upgrade_text = sample_yaml_text("pre-upgrade-on-disk")
        pre_upgrade_id = now.strftime("%Y%m%d-%H%M%S")
        # Simulate exactly what the pre-#4720 code left behind: an index entry, and content
        # saved to the generic cache store under the legacy key - no debug/ file at all.
        asyncio.run(real_storage.save(STORAGE_MODULE, INDEX_NAME, [{"id": pre_upgrade_id, "timestamp": now.isoformat()}], format="json"))
        asyncio.run(real_storage.save(STORAGE_MODULE, _legacy_snapshot_key(pre_upgrade_id), pre_upgrade_text, format="text"))
        legacy_meta_path = os.path.join(tmpdir, "cache", "{}_{}.meta".format(STORAGE_MODULE, _legacy_snapshot_key(pre_upgrade_id)))
        if not os.path.exists(legacy_meta_path):
            print("  ERROR: test setup bug - the simulated legacy cache/ file should exist on disk before eviction")
            failed = True

        if asyncio.run(load_snapshot(real_storage, pre_upgrade_id)) != pre_upgrade_text:
            print("  ERROR: a pre-upgrade snapshot should still load from the real backend via the legacy-key fallback")
            failed = True

        asyncio.run(_discard_snapshot(real_storage, pre_upgrade_id))
        asyncio.run(real_storage.cleanup())
        if os.path.exists(legacy_meta_path):
            print("  ERROR: the legacy cache/ file should have been reaped from disk after eviction + cleanup()")
            failed = True
        if asyncio.run(load_snapshot(real_storage, pre_upgrade_id)) is not None:
            print("  ERROR: the pre-upgrade snapshot should no longer load once its legacy entry has been reaped")
            failed = True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("Test: _discard_snapshot removes the snapshot's debug/ file directly (called outside of pruning too)")
    direct_storage = FakeStorage()
    asyncio.run(capture_snapshot(direct_storage, sample_yaml_text("to-discard"), now, max_count=15))
    ids_before = [entry["id"] for entry in asyncio.run(list_snapshots(direct_storage))]
    asyncio.run(_discard_snapshot(direct_storage, ids_before[0]))
    if snapshot_filename(ids_before[0]) not in direct_storage.debug_copies_deleted:
        print("  ERROR: _discard_snapshot should delete the snapshot's debug/ file")
        failed = True

    print("Test: a snapshot captured under the pre-#4720 scheme (content in the legacy cache/ key, nothing in debug/) still loads (#4720 upgrade path)")
    legacy_storage = FakeStorage()
    legacy_id = asyncio.run(capture_snapshot(legacy_storage, sample_yaml_text("pre-upgrade"), now, max_count=15))
    legacy_storage.debug_copies.pop(snapshot_filename(legacy_id), None)  # simulate: never written under the old scheme
    legacy_storage.store[(STORAGE_MODULE, _legacy_snapshot_key(legacy_id))] = (sample_yaml_text("pre-upgrade"), "text")  # simulate: still sitting there from before the upgrade
    if asyncio.run(load_snapshot(legacy_storage, legacy_id)) != sample_yaml_text("pre-upgrade"):
        print("  ERROR: a pre-upgrade snapshot with only a legacy cache/ entry should still load via the fallback")
        failed = True

    print("Test: evicting a pre-#4720 snapshot also expires its legacy cache/ entry, so an upgrade does not leak it forever (#4720)")
    if asyncio.run(legacy_storage.load(STORAGE_MODULE, _legacy_snapshot_key(legacy_id))) is None:
        print("  ERROR: test setup bug - the legacy entry should still be there before eviction")
        failed = True
    asyncio.run(_discard_snapshot(legacy_storage, legacy_id))
    legacy_entry = legacy_storage.store.get((STORAGE_MODULE, _legacy_snapshot_key(legacy_id)))
    if legacy_entry is None:
        print("  ERROR: _discard_snapshot should overwrite the legacy entry (with an expiry), not remove it outright - it has no delete() to call")
        failed = True
    if asyncio.run(load_snapshot(legacy_storage, legacy_id)) is not None:
        print("  ERROR: the legacy entry should no longer be readable as snapshot content once evicted")
        failed = True
    # A snapshot that never had a legacy entry (the common, post-upgrade case) must not gain one -
    # _discard_snapshot should not write legacy junk for every future eviction, only reap real leftovers.
    fresh_storage = FakeStorage()
    fresh_id = asyncio.run(capture_snapshot(fresh_storage, sample_yaml_text("fresh"), now, max_count=15))
    asyncio.run(_discard_snapshot(fresh_storage, fresh_id))
    if (STORAGE_MODULE, _legacy_snapshot_key(fresh_id)) in fresh_storage.store:
        print("  ERROR: a snapshot with no legacy entry should not have one created on eviction")
        failed = True

    print("Test: snapshot_filename is keyed on the timestamp id alone, not on ring position")
    if snapshot_filename("20260804-140000") != "predbat_debug_20260804-140000.yaml":
        print("  ERROR: unexpected snapshot_filename output {!r}".format(snapshot_filename("20260804-140000")))
        failed = True

    print("Test: load_all_snapshots returns newest-first (filename, text) pairs for every retained snapshot")
    bulk_storage = FakeStorage()
    for offset in range(3):
        asyncio.run(capture_snapshot(bulk_storage, sample_yaml_text(offset), now + datetime.timedelta(hours=offset), max_count=15))
    named = asyncio.run(load_all_snapshots(bulk_storage))
    if len(named) != 3:
        print("  ERROR: expected 3 named snapshots, got {}".format(len(named)))
        failed = True
    if named[0][1] != sample_yaml_text(2):
        print("  ERROR: newest snapshot's text should be first, got {!r}".format(named[0][1]))
        failed = True

    print("Test: a snapshot's archive filename is stable across downloads even as its ring position shifts")
    stable_storage = FakeStorage()
    target_id = asyncio.run(capture_snapshot(stable_storage, sample_yaml_text("target"), now, max_count=15))
    named_before = {fname: text for fname, text in asyncio.run(load_all_snapshots(stable_storage))}
    filename_before = next(fname for fname, text in named_before.items() if text == sample_yaml_text("target"))
    # Push the target snapshot back in the ring (not out of it) with newer captures, simulating
    # time passing between two separate "download the archive" actions against the same buffer.
    for offset in range(1, 4):
        asyncio.run(capture_snapshot(stable_storage, sample_yaml_text("newer_{}".format(offset)), now + datetime.timedelta(hours=offset), max_count=15))
    named_after = {fname: text for fname, text in asyncio.run(load_all_snapshots(stable_storage))}
    filename_after = next(fname for fname, text in named_after.items() if text == sample_yaml_text("target"))
    if filename_before != filename_after:
        print("  ERROR: the same snapshot's filename changed after its ring position shifted: {!r} -> {!r}".format(filename_before, filename_after))
        failed = True
    if filename_before != snapshot_filename(target_id):
        print("  ERROR: expected filename to be exactly snapshot_filename(target_id), got {!r}".format(filename_before))
        failed = True

    print("Test: load_all_snapshots skips an indexed snapshot whose text failed to load, rather than failing the whole batch")
    partial_storage = FakeStorage()
    asyncio.run(capture_snapshot(partial_storage, sample_yaml_text("keep"), now, max_count=15))
    missing_id = asyncio.run(capture_snapshot(partial_storage, sample_yaml_text("gone"), now + datetime.timedelta(hours=1), max_count=15))
    partial_storage.debug_copies.pop(snapshot_filename(missing_id), None)  # simulate a corrupt/evicted entry still left in the index
    named_partial = asyncio.run(load_all_snapshots(partial_storage))
    if len(named_partial) != 1 or named_partial[0][1] != sample_yaml_text("keep"):
        print("  ERROR: expected only the loadable snapshot to survive, got {}".format(named_partial))
        failed = True

    print("Test: load_all_snapshots with no storage/snapshots returns an empty list")
    if asyncio.run(load_all_snapshots(None)) != []:
        print("  ERROR: load_all_snapshots(None) should give an empty list")
        failed = True
    if asyncio.run(load_all_snapshots(FakeStorage())) != []:
        print("  ERROR: load_all_snapshots with nothing captured should give an empty list")
        failed = True

    print("Test: build_archive produces a gzip tarball whose members match the input exactly")
    archive_bytes = build_archive([("a.yaml", sample_yaml_text("a")), ("b.yaml", sample_yaml_text("b"))])
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        if names != ["a.yaml", "b.yaml"]:
            print("  ERROR: expected archive members [a.yaml, b.yaml], got {}".format(names))
            failed = True
        for name, expected_marker in [("a.yaml", "a"), ("b.yaml", "b")]:
            member = tar.extractfile(name)
            content = member.read().decode("utf-8") if member else None
            if content != sample_yaml_text(expected_marker):
                print("  ERROR: archive member {} content mismatch, got {!r}".format(name, content))
                failed = True

    print("Test: build_archive with no snapshots produces a valid (empty) archive rather than raising")
    empty_archive = build_archive([])
    with tarfile.open(fileobj=io.BytesIO(empty_archive), mode="r:gz") as tar:
        if tar.getnames() != []:
            print("  ERROR: expected an empty archive, got members {}".format(tar.getnames()))
            failed = True

    return failed
