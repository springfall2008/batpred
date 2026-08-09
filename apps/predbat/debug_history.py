# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Storage-backed rolling history of debug snapshots.

Captures create_debug_yaml()'s output on a coarse interval and keeps the most recent
ones, so there is always some recent history to replay a bug report against without
switch.predbat_debug_enable having already been on before the problem happened.
Everything goes through the Storage abstraction rather than the filesystem, because
there may not be one.
"""

import datetime
import io
import tarfile

STORAGE_MODULE = "debug_history"
INDEX_NAME = "snapshots_index"


def _snapshot_key(snapshot_id):
    """Return the storage filename holding one snapshot's debug-yaml text."""
    return "snapshot_{}".format(snapshot_id)


async def list_snapshots(storage):
    """Return the stored snapshots newest-first, or an empty list when there are none.

    A corrupt or unexpected index reads as empty rather than raising: the caller
    must still be able to render a listing UI.
    """
    if not storage:
        return []
    index = await storage.load(STORAGE_MODULE, INDEX_NAME)
    if not isinstance(index, list):
        return []
    return [entry for entry in index if isinstance(entry, dict) and entry.get("id")]


def annotate_steps_back(snapshots):
    """Return a copy of a newest-first snapshot list with 'steps_back' (0, 1, 2...) added.

    Pure function, no storage access, so the download route can reuse a list it
    already has rather than re-fetching, and it is trivially testable on its own.
    """
    return [dict(entry, steps_back=i) for i, entry in enumerate(snapshots)]


async def resolve_and_load_snapshot(storage, snapshot_id):
    """Resolve snapshot_id (including "latest"/falsy) and load its text in one call,
    returning (resolved_id, text).

    A caller that needs both the id (e.g. to build a filename) and the data must use
    this rather than resolving "latest" via load_snapshot() and then separately calling
    list_snapshots() again to find the id - a capture landing between those two calls
    could resolve to a different snapshot than the one whose bytes were actually loaded,
    serving one snapshot's data under another's filename.

    Returns (None, None) when nothing could be resolved or loaded.
    """
    if not storage:
        return None, None
    if not snapshot_id or snapshot_id == "latest":
        snapshots = await list_snapshots(storage)
        if not snapshots:
            return None, None
        snapshot_id = snapshots[0]["id"]
    data = await storage.load(STORAGE_MODULE, _snapshot_key(snapshot_id))
    return snapshot_id, data


async def load_snapshot(storage, snapshot_id):
    """Return one snapshot's raw debug-yaml text, or None when it cannot be resolved.

    A falsy snapshot_id or the literal string "latest" resolves to the newest stored
    snapshot, so a caller (an automation that just forced a capture, or the download
    route with no id given) does not need a separate round-trip to the list endpoint
    first just to discover what id its own capture got. A caller that also needs the
    resolved id itself (not just the text) should use resolve_and_load_snapshot()
    instead, to avoid resolving "latest" twice.
    """
    _, data = await resolve_and_load_snapshot(storage, snapshot_id)
    return data


async def _discard_snapshot(storage, snapshot_id):
    """Remove an evicted snapshot's stored text so the ring does not leak it.

    The real Storage component has no ``delete`` method, so the primary path is to
    overwrite it with ``None`` and set an expiry in the past, which lets
    ``storage.cleanup()`` reclaim it later. A ``delete`` method is still preferred
    when a backend (or a test fake) provides one.
    """
    key = _snapshot_key(snapshot_id)
    if hasattr(storage, "delete"):
        await storage.delete(STORAGE_MODULE, key)
        return
    expired = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    await storage.save(STORAGE_MODULE, key, None, format="text", expiry=expired)


async def capture_snapshot(storage, yaml_text, now_utc, max_count, max_age=None):
    """Save a new debug snapshot and prune the ring. Returns the snapshot id.

    ``yaml_text`` must be ``create_debug_yaml(write_file=False)``'s already-rendered
    YAML string - saved with ``format="text"`` (raw passthrough), NOT ``format="yaml"``,
    which would run ``yaml.safe_dump()`` on an already-serialised string a second time
    and turn it into an unreadable quoted block scalar that ``unit_test.py --debug_file``
    could not parse.

    ``max_count`` is passed in per call (the live config value) rather than a fixed
    module constant, so a change to the retention count takes effect on the very next
    capture with no migration step.

    ``max_age`` (an optional ``datetime.timedelta``) additionally prunes anything older
    than ``now_utc - max_age``, even if ``max_count`` hasn't been reached yet - a burst
    of close-together captures (several force-captures, or a shortened interval) should
    not be able to leave something far older than the buffer's intended window sitting
    there just because the count cap alone hasn't caught up to it.

    Two captures landing in the same calendar minute (a routine capture and a
    close-by force-capture, say) are pruned to just the newer one here too, rather
    than left for a display layer to cope with two near-identical, same-named entries.
    """
    if not storage:
        return None

    snapshot_id = now_utc.strftime("%Y%m%d-%H%M%S")
    await storage.save(STORAGE_MODULE, _snapshot_key(snapshot_id), yaml_text, format="text")

    index = await list_snapshots(storage)
    index = [existing for existing in index if existing.get("id") != snapshot_id]
    index.insert(0, {"id": snapshot_id, "timestamp": now_utc.isoformat()})

    # Newest-first, so keeping only the first occurrence of each calendar minute
    # keeps the newer of any same-minute pair and discards the older.
    seen_minutes = set()
    deduped = []
    for existing in index:
        minute_key = existing["id"][:-2]  # id is %Y%m%d-%H%M%S - drop the seconds
        if minute_key in seen_minutes:
            await _discard_snapshot(storage, existing["id"])
            continue
        seen_minutes.add(minute_key)
        deduped.append(existing)
    index = deduped

    if max_age is not None:
        cutoff = now_utc - max_age
        kept = []
        for existing in index:
            try:
                existing_time = datetime.datetime.fromisoformat(existing["timestamp"])
            except (KeyError, ValueError, TypeError):
                kept.append(existing)  # malformed/missing timestamp - leave it for max_count to catch instead of guessing
                continue
            if existing_time < cutoff:
                await _discard_snapshot(storage, existing["id"])
            else:
                kept.append(existing)
        index = kept

    for dropped in index[max_count:]:
        await _discard_snapshot(storage, dropped["id"])
    index = index[:max_count]

    await storage.save(STORAGE_MODULE, INDEX_NAME, index, format="json")
    return snapshot_id


def snapshot_filename(snapshot_id):
    """Return the filename used both for a single-snapshot download and inside the bulk archive.

    Deliberately keyed on snapshot_id alone (a capture timestamp, stable and unique) and
    not on steps_back (the snapshot's position in the current ring, which shifts as newer
    captures push it back) - two archives downloaded hours apart must name the same real
    capture identically, or they can't be merged/deduplicated by filename.
    """
    return "predbat_debug_{}.yaml".format(snapshot_id)


async def load_all_snapshots(storage):
    """Return a newest-first list of (filename, yaml_text) for every retained snapshot.

    Skips any snapshot whose text failed to load (evicted between listing and loading,
    or a corrupt entry) rather than failing the whole archive for one bad snapshot.
    """
    if not storage:
        return []
    snapshots = await list_snapshots(storage)
    result = []
    for entry in snapshots:
        text = await load_snapshot(storage, entry["id"])
        if text is not None:
            result.append((snapshot_filename(entry["id"]), text))
    return result


def build_archive(named_snapshots):
    """Build a gzip tar archive in memory from a list of (filename, yaml_text) pairs.

    Synchronous and storage-agnostic - the caller does the async storage fetching
    (load_all_snapshots) first and hands the already-loaded text here, so this half is
    trivially testable without an event loop or a fake storage backend.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for filename, yaml_text in named_snapshots:
            data = yaml_text.encode("utf-8")
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            info.mtime = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
