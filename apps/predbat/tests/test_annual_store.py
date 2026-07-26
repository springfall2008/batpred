# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's five-run store."""

import asyncio
import datetime

from annual_store import INDEX_NAME, MAX_RUNS, STORAGE_MODULE, build_label, list_runs, load_run, save_run


class FakeStorage:
    """An in-memory stand-in for the Storage component that supports ``delete``.

    Exercises the primary eviction path in ``annual_store``, where a genuine
    ``delete`` method is available and preferred over the fallback.
    """

    def __init__(self):
        """Start with nothing stored."""
        self.store = {}
        self.deleted = []

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a saved value."""
        self.store[(module, filename)] = data

    async def load(self, module, filename):
        """Return a stored value, or None."""
        return self.store.get((module, filename))

    async def delete(self, module, filename):
        """Remove a stored value and record that it happened."""
        self.deleted.append(filename)
        self.store.pop((module, filename), None)


class FakeStorageNoDelete:
    """An in-memory stand-in for the Storage component with no ``delete`` method.

    This matches the real Storage component's actual surface (``save``, ``load``,
    ``age``, ``cleanup``, ``fetch_cached`` — no ``delete``), so it exercises the
    fallback eviction path: overwriting the evicted document with ``None`` and a
    past expiry, via ``save``, rather than calling a method that does not exist.
    """

    def __init__(self):
        """Start with nothing stored."""
        self.store = {}
        self.expiries = {}

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a saved value and the expiry it was saved with, if any."""
        self.store[(module, filename)] = data
        self.expiries[(module, filename)] = expiry

    async def load(self, module, filename):
        """Return a stored value, or None."""
        return self.store.get((module, filename))


def sample_results(cost):
    """Return a minimal results document with a distinguishing cost."""
    return {"year": 2025, "annual": {"scenarios": {"with_predbat": {"cost_p": cost}}, "months_included": 12}, "months": []}


def sample_config(size_kwh=9.5):
    """Return a minimal validated-shape config."""
    return {"battery": {"size_kwh": size_kwh}, "solar": [{"kwp": 5.6}], "tariff": {"import_octopus_url": "https://example.com/AGILE-24-10-01/x"}}


def test_annual_store(my_predbat):
    """Verify saving, listing, loading, eviction (both code paths) and label generation."""
    failed = False
    print("**** Testing annual_store ****")

    print("Test: a saved run appears in the index and can be loaded back")
    storage = FakeStorage()
    run_id = asyncio.run(save_run(storage, sample_results(100), sample_config(), "run-1"))
    if run_id != "run-1":
        print("  ERROR: save_run should return the id it was given, got {}".format(run_id))
        failed = True
    index = asyncio.run(list_runs(storage))
    if len(index) != 1 or index[0]["id"] != "run-1":
        print("  ERROR: expected one indexed run, got {}".format(index))
        failed = True
    loaded = asyncio.run(load_run(storage, "run-1"))
    if (loaded or {}).get("annual", {}).get("scenarios", {}).get("with_predbat", {}).get("cost_p") != 100:
        print("  ERROR: the loaded run should match what was saved, got {}".format(loaded))
        failed = True

    print("Test: the index is newest-first")
    asyncio.run(save_run(storage, sample_results(200), sample_config(), "run-2"))
    index = asyncio.run(list_runs(storage))
    if [entry["id"] for entry in index] != ["run-2", "run-1"]:
        print("  ERROR: expected newest first, got {}".format([entry["id"] for entry in index]))
        failed = True

    print("Test: a sixth run evicts the oldest AND deletes its stored document (delete-capable backend)")
    storage = FakeStorage()
    for number in range(1, MAX_RUNS + 2):
        asyncio.run(save_run(storage, sample_results(number), sample_config(), "run-{}".format(number)))
    index = asyncio.run(list_runs(storage))
    if len(index) != MAX_RUNS:
        print("  ERROR: the ring should hold {} runs, got {}".format(MAX_RUNS, len(index)))
        failed = True
    if "run-1" in [entry["id"] for entry in index]:
        print("  ERROR: the oldest run should have been evicted from the index")
        failed = True
    if "run_run-1" not in storage.deleted:
        print("  ERROR: the evicted run's document should be deleted, deletions were {}".format(storage.deleted))
        failed = True
    if asyncio.run(load_run(storage, "run-1")) is not None:
        print("  ERROR: an evicted run should no longer load")
        failed = True

    print("Test: a sixth run evicts the oldest via the fallback path when delete is unavailable")
    no_delete_storage = FakeStorageNoDelete()
    for number in range(1, MAX_RUNS + 2):
        asyncio.run(save_run(no_delete_storage, sample_results(number), sample_config(), "run-{}".format(number)))
    index = asyncio.run(list_runs(no_delete_storage))
    if len(index) != MAX_RUNS:
        print("  ERROR: the ring should hold {} runs without delete, got {}".format(MAX_RUNS, len(index)))
        failed = True
    if "run-1" in [entry["id"] for entry in index]:
        print("  ERROR: the oldest run should have been evicted from the index without delete")
        failed = True
    if asyncio.run(load_run(no_delete_storage, "run-1")) is not None:
        print("  ERROR: an evicted run should read back as None when the fallback blanking is used")
        failed = True
    evicted_expiry = no_delete_storage.expiries.get((STORAGE_MODULE, "run_run-1"))
    if evicted_expiry is None:
        print("  ERROR: the fallback should set an expiry on the blanked document so cleanup() can reclaim it")
        failed = True
    elif evicted_expiry.tzinfo is None:
        print("  ERROR: the fallback expiry should be timezone-aware, got {!r}".format(evicted_expiry))
        failed = True
    elif evicted_expiry >= datetime.datetime.now(datetime.timezone.utc):
        print("  ERROR: the fallback expiry should be in the past, got {!r}".format(evicted_expiry))
        failed = True

    print("Test: loading an unknown or missing run returns None rather than raising")
    if asyncio.run(load_run(storage, "does-not-exist")) is not None:
        print("  ERROR: an unknown run id should give None")
        failed = True

    print("Test: an index entry whose document is missing is reported, not rendered empty")
    storage = FakeStorage()
    asyncio.run(save_run(storage, sample_results(1), sample_config(), "orphan"))
    storage.store.pop((STORAGE_MODULE, "run_orphan"))
    if asyncio.run(load_run(storage, "orphan")) is not None:
        print("  ERROR: a missing document should give None so the caller can say so")
        failed = True

    print("Test: an empty store lists nothing rather than raising")
    if asyncio.run(list_runs(FakeStorage())) != []:
        print("  ERROR: an empty store should list no runs")
        failed = True

    print("Test: the label describes the configuration, not just a timestamp")
    label = build_label(sample_config(size_kwh=9.5))
    if "9.5" not in label or "5.6" not in label:
        print("  ERROR: the label should name the battery and array size, got {!r}".format(label))
        failed = True
    if "Agile" not in label:
        print("  ERROR: the label should name the tariff, got {!r}".format(label))
        failed = True

    print("Test: a label is still produced for a config with no battery or solar")
    label = build_label({"tariff": {"rates_import": [{"rate": 25.0}]}})
    if not label:
        print("  ERROR: a sparse config should still produce a label")
        failed = True

    print("Test: the index survives a corrupt stored value")
    storage = FakeStorage()
    storage.store[(STORAGE_MODULE, INDEX_NAME)] = "not a list"
    if asyncio.run(list_runs(storage)) != []:
        print("  ERROR: a corrupt index should read as empty rather than raising")
        failed = True

    return failed
