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

from annual_store import INDEX_NAME, MAX_RUNS, STORAGE_MODULE, _discard_run, backfill_summaries, build_label, build_summary, delete_run, list_runs, load_plan, load_run, save_run
from tests.test_infra import run_async


class FakeStorage:
    """An in-memory stand-in for the Storage component that supports ``delete``.

    Exercises the primary eviction path in ``annual_store``, where a genuine
    ``delete`` method is available and preferred over the fallback.
    """

    def __init__(self):
        """Start with nothing stored."""
        self.store = {}
        self.deleted = []
        self.save_calls = []

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a saved value and remember that a save happened."""
        self.store[(module, filename)] = data
        self.save_calls.append((module, filename))

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

    print("Test: build_label tolerates a config that is not a dict at all")
    for not_a_config in [None, "not-a-dict", 42, [], [1, 2, 3], {"tariff": "also-not-a-dict"}]:
        label = build_label(not_a_config)
        if not label:
            print("  ERROR: build_label should still return a non-empty string for {!r}, got {!r}".format(not_a_config, label))
            failed = True

    print("Test: the index survives a corrupt stored value")
    storage = FakeStorage()
    storage.store[(STORAGE_MODULE, INDEX_NAME)] = "not a list"
    if asyncio.run(list_runs(storage)) != []:
        print("  ERROR: a corrupt index should read as empty rather than raising")
        failed = True

    print("Test: build_summary lifts the headline figures off a results document")
    results = {
        "annual": {
            "scenarios": {"no_pvbat": {"cost_p": 180000.0}, "with_predbat": {"cost_p": 66000.0}},
            "months_included": 12,
            "costs": {"total_kwp": 5.6, "battery_kwh": 9.5},
            "payback": {
                "available": True,
                "pv_only": {"pays_back": True, "years": 17.8},
                "pv_battery": {"pays_back": True, "years": 13.61},
                "pv_battery_predbat": {"pays_back": False, "years": None},
            },
        }
    }
    config = {"tariff": {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE-24-10-01/x/"}}
    summary = build_summary(results, config)
    if summary["total_kwp"] != 5.6 or summary["battery_kwh"] != 9.5:
        print("  ERROR: the summary should carry the system size, got {}".format(summary))
        failed = True
    if summary["cost_with_predbat_p"] != 66000.0:
        print("  ERROR: expected the with_predbat cost, got {}".format(summary))
        failed = True
    if summary["saving_vs_none_p"] != 114000.0:
        print("  ERROR: saving should be no_pvbat minus with_predbat, got {}".format(summary))
        failed = True
    if summary["payback_years"]["pv_battery"] != 13.61:
        print("  ERROR: expected the pv_battery payback, got {}".format(summary))
        failed = True
    if summary["payback_years"]["pv_battery_predbat"] is not None:
        print("  ERROR: a row that does not pay back must carry None, not a number, got {}".format(summary))
        failed = True

    print("Test: a run with no usable month summarises as unknown rather than zero")
    empty = build_summary({"annual": {"scenarios": None, "months_included": 0}}, {})
    if empty["cost_with_predbat_p"] is not None or empty["saving_vs_none_p"] is not None:
        print("  ERROR: no usable month means unknown, not zero, got {}".format(empty))
        failed = True

    print("Test: a non-numeric or missing cost_p summarises as unknown rather than raising")
    malformed = build_summary({"annual": {"scenarios": {"no_pvbat": {"cost_p": 180000.0}, "with_predbat": {"cost_p": "66000"}}, "months_included": 12}}, {})
    if malformed["cost_with_predbat_p"] is not None or malformed["saving_vs_none_p"] is not None:
        print("  ERROR: a string cost_p should summarise as unknown, not a number, got {}".format(malformed))
        failed = True
    none_baseline = build_summary({"annual": {"scenarios": {"no_pvbat": {"cost_p": None}, "with_predbat": {"cost_p": 66000.0}}, "months_included": 12}}, {})
    if none_baseline["saving_vs_none_p"] is not None:
        print("  ERROR: a missing baseline cost_p should summarise as unknown, got {}".format(none_baseline))
        failed = True

    print("Test: an unavailable payback keeps its reason for the compare table to show")
    unavailable = build_summary(
        {"annual": {"scenarios": {"no_pvbat": {"cost_p": 10.0}, "with_predbat": {"cost_p": 5.0}}, "months_included": 11, "payback": {"available": False, "reason": "Payback needs a full year, but only 11 of 12 months could be modelled."}}}, {}
    )
    if unavailable["payback_years"] != {} or "11 of 12" not in (unavailable.get("payback_reason") or ""):
        print("  ERROR: an unavailable payback should carry its reason, got {}".format(unavailable))
        failed = True

    print("Test: the summary carries both tariff dicts so the compare table can name all three sides")
    # Deliberately the raw dicts rather than a rendered name: naming needs the merged
    # catalogue, which includes the user's own compare_list entries, and only the web
    # layer can read those. A name baked in here could not see them.
    tariffs = {
        "tariff": {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE-24-10-01/x/", "rates_export": [{"rate": 0.0}]},
        "baseline_tariff": {"rates_import": [{"rate": 26.11}]},
    }
    with_tariffs = build_summary(results, tariffs)
    if with_tariffs.get("tariff") != tariffs["tariff"]:
        print("  ERROR: the summary should carry the run's tariff dict, got {}".format(with_tariffs.get("tariff")))
        failed = True
    if with_tariffs.get("baseline_tariff") != tariffs["baseline_tariff"]:
        print("  ERROR: the summary should carry the baseline tariff dict, got {}".format(with_tariffs.get("baseline_tariff")))
        failed = True

    print("Test: the summary's tariffs are copies, so later edits to the config cannot rewrite a stored run")
    mutable = {"tariff": {"rates_import": [{"rate": 5.0}]}, "baseline_tariff": {"rates_import": [{"rate": 26.11}]}}
    copied = build_summary(results, mutable)
    mutable["tariff"]["rates_import"][0]["rate"] = 99.0
    if copied["tariff"]["rates_import"][0]["rate"] != 5.0:
        print("  ERROR: the summary should hold its own copy of the tariff, got {}".format(copied["tariff"]))
        failed = True

    print("Test: a config with no tariff at all summarises as empty dicts rather than raising")
    bare = build_summary(results, {})
    if bare.get("tariff") != {} or bare.get("baseline_tariff") != {}:
        print("  ERROR: a config with no tariff should summarise as empty, got {}".format(bare))
        failed = True

    print("Test: save_run records the summary on the index entry")
    storage = FakeStorage()
    asyncio.run(save_run(storage, results, config, "20260728-090000"))
    index = asyncio.run(list_runs(storage))
    if not index or not index[0].get("summary"):
        print("  ERROR: save_run should record a summary, got {}".format(index))
        failed = True

    print("Test: backfill fills every summary-less entry from its document and writes the index back exactly once")
    storage = FakeStorage()
    asyncio.run(save_run(storage, results, config, "20260728-090100"))
    asyncio.run(save_run(storage, results, config, "20260728-090101"))
    # Simulate two runs stored before summaries existed - with only one stale entry, "wrote
    # once" cannot be told apart from "wrote once per entry", so this uses two.
    stale = asyncio.run(list_runs(storage))
    for entry in stale:
        entry.pop("summary", None)
    asyncio.run(storage.save(STORAGE_MODULE, INDEX_NAME, stale, format="json"))
    writes_before = len(storage.save_calls)
    filled = asyncio.run(backfill_summaries(storage, asyncio.run(list_runs(storage))))
    if not all(entry.get("summary") for entry in filled):
        print("  ERROR: backfill should fill every missing summary, got {}".format(filled))
        failed = True
    if len(storage.save_calls) != writes_before + 1:
        print("  ERROR: backfill should write the index exactly once regardless of how many entries were filled, got {} writes".format(len(storage.save_calls) - writes_before))
        failed = True

    print("Test: backfill survives one corrupt document and still fills and saves the others")
    storage = FakeStorage()
    asyncio.run(save_run(storage, results, config, "20260728-090200"))
    asyncio.run(save_run(storage, results, config, "20260728-090201"))
    stale = asyncio.run(list_runs(storage))
    for entry in stale:
        entry.pop("summary", None)
    # One run's stored document is corrupt in a way build_summary's own guards cannot catch -
    # a scenario that is a string rather than a dict - so it must be skipped, not abort the loop.
    corrupt_id = stale[-1]["id"]
    asyncio.run(storage.save(STORAGE_MODULE, "run_{}".format(corrupt_id), {"annual": {"scenarios": {"no_pvbat": "not-a-dict"}, "months_included": 12}}, format="json"))
    asyncio.run(storage.save(STORAGE_MODULE, INDEX_NAME, stale, format="json"))
    writes_before = len(storage.save_calls)
    filled = asyncio.run(backfill_summaries(storage, asyncio.run(list_runs(storage))))
    good_entries = [entry for entry in filled if entry["id"] != corrupt_id]
    if not good_entries or not good_entries[0].get("summary"):
        print("  ERROR: backfill should still fill the good entry despite the corrupt one, got {}".format(filled))
        failed = True
    if len(storage.save_calls) != writes_before + 1:
        print("  ERROR: backfill should still write the index exactly once despite the corrupt entry, got {} writes".format(len(storage.save_calls) - writes_before))
        failed = True

    print("Test: backfill writes nothing when every entry already has a current summary")
    storage = FakeStorage()
    asyncio.run(save_run(storage, results, config, "20260728-090400"))
    asyncio.run(save_run(storage, results, config, "20260728-090401"))
    writes_before = len(storage.save_calls)
    asyncio.run(backfill_summaries(storage, asyncio.run(list_runs(storage))))
    if len(storage.save_calls) != writes_before:
        print("  ERROR: a compare page that changes nothing must not write storage, got {} writes".format(len(storage.save_calls) - writes_before))
        failed = True

    print("Test: a summary predating the tariff fields is refreshed, not left as it is")
    # A summary that merely EXISTS is no longer enough: runs stored before the compare
    # table showed three tariffs have one, but it describes none of them. Left alone,
    # those rows would show dashes forever despite their document holding the answer.
    storage = FakeStorage()
    # The config rides along inside the document, which is where backfill reads it from -
    # the index entry's own copy is exactly what is being rebuilt here.
    stale_config = {"tariff": {"rates_import": [{"rate": 7.5}]}, "baseline_tariff": {"rates_import": [{"rate": 26.11}]}}
    stale_results = dict(results, config=stale_config)
    asyncio.run(save_run(storage, stale_results, stale_config, "20260728-090500"))
    stale = asyncio.run(list_runs(storage))
    # Exactly the shape the old code wrote: a summary with a rendered name and no dicts.
    stale[0]["summary"] = {"total_kwp": 5.6, "battery_kwh": 9.5, "tariff": "Agile", "cost_with_predbat_p": 66000.0}
    asyncio.run(storage.save(STORAGE_MODULE, INDEX_NAME, stale, format="json"))
    writes_before = len(storage.save_calls)
    filled = asyncio.run(backfill_summaries(storage, asyncio.run(list_runs(storage))))
    if filled[0]["summary"].get("tariff") != {"rates_import": [{"rate": 7.5}]}:
        print("  ERROR: a stale summary should be rebuilt from the document's config, got {}".format(filled[0]["summary"].get("tariff")))
        failed = True
    if filled[0]["summary"].get("baseline_tariff") != {"rates_import": [{"rate": 26.11}]}:
        print("  ERROR: the rebuilt summary should carry the baseline the run actually used, got {}".format(filled[0]["summary"]))
        failed = True
    if len(storage.save_calls) != writes_before + 1:
        print("  ERROR: refreshing a stale summary should persist the index once, got {} writes".format(len(storage.save_calls) - writes_before))
        failed = True

    print("Test: save_run strips the plans out of the stored document and keys them per leg")
    storage = FakeStorage()
    debug_results = {
        "annual": {"scenarios": {"no_pvbat": {"cost_p": 10.0}, "with_predbat": {"cost_p": 5.0}}, "months_included": 12},
        "months": [
            {
                "month": 1,
                "status": "ok",
                "plans": [
                    {"day": "2025-01-08", "leg": "with_car", "scenarios": {"with_predbat": {"rows": [1]}}},
                    {"day": "2025-01-08", "leg": "without_car", "scenarios": {"with_predbat": {"rows": [2]}}},
                ],
            },
            {"month": 2, "status": "ok", "plans": [{"day": "2025-02-10", "leg": "single", "scenarios": {"pv_only": {"rows": [3]}}}]},
        ],
    }
    run_async(save_run(storage, debug_results, {}, "20260728-091000"))
    stored = run_async(load_run(storage, "20260728-091000"))
    if any("plans" in month for month in stored["months"]):
        print("  ERROR: the stored document must not carry the plans, got {}".format(stored["months"]))
        failed = True
    index = run_async(list_runs(storage))
    if len(index[0].get("plan_keys") or []) != 3:
        print("  ERROR: three legs should produce three plan keys, got {}".format(index[0].get("plan_keys")))
        failed = True

    print("Test: save_run records a plan_index entry per leg, carrying the labels the viewer needs")
    plan_index = index[0].get("plan_index") or []
    if len(plan_index) != 3:
        print("  ERROR: three legs should produce three plan_index entries, got {}".format(plan_index))
        failed = True
    expected = [
        {"month": 1, "index": 0, "day": "2025-01-08", "leg": "with_car"},
        {"month": 1, "index": 1, "day": "2025-01-08", "leg": "without_car"},
        {"month": 2, "index": 0, "day": "2025-02-10", "leg": "single"},
    ]
    if plan_index != expected:
        print("  ERROR: expected {}, got {}".format(expected, plan_index))
        failed = True

    print("Test: load_plan returns the right scenario from its own key")
    plan = run_async(load_plan(storage, "20260728-091000", 1, 1, "with_predbat"))
    if plan != {"rows": [2]}:
        print("  ERROR: expected the without_car leg's plan, got {}".format(plan))
        failed = True

    print("Test: a non-debug run writes no plan keys at all")
    storage = FakeStorage()
    run_async(save_run(storage, {"annual": {"months_included": 12}, "months": [{"month": 1, "status": "ok"}]}, {}, "20260728-091100"))
    if run_async(list_runs(storage))[0].get("plan_keys"):
        print("  ERROR: a run with no plans should record no plan keys")
        failed = True

    print("Test: load_plan falls back to a document that still has its plans embedded")
    # A run stored before this split. It must stay viewable rather than silently losing
    # its plans.
    legacy_storage = FakeStorage()
    run_async(legacy_storage.save(STORAGE_MODULE, "run_legacy", {"months": [{"month": 3, "plans": [{"leg": "single", "scenarios": {"pv_only": {"rows": [9]}}}]}]}, format="json"))
    if run_async(load_plan(legacy_storage, "legacy", 3, 0, "pv_only")) != {"rows": [9]}:
        print("  ERROR: a legacy embedded-plans run should still resolve")
        failed = True

    print("Test: the legacy fallback bounds-checks the embedded plans list rather than indexing past the end")
    # legacy_storage's "run_legacy" document has exactly one plan, at index 0, for month
    # 3. Deleting the bound check in load_plan's fallback (`index >= len(plans)`) would
    # let `plans[index]` raise IndexError here instead of resolving to None.
    for args in [("legacy", 3, 1, "pv_only"), ("legacy", 3, 99, "pv_only")]:
        try:
            if run_async(load_plan(legacy_storage, *args)) is not None:
                print("  ERROR: {} should resolve to None".format(args))
                failed = True
        except Exception as error:
            print("  ERROR: {} raised {} instead of returning None".format(args, type(error).__name__))
            failed = True

    print("Test: load_plan's legacy fallback resolves None, not an exception, when 'plans' itself is not a list")
    for corrupt_plans in [{"0": "not a list"}, "also not a list", 42, None]:
        corrupt_legacy_storage = FakeStorage()
        run_async(corrupt_legacy_storage.save(STORAGE_MODULE, "run_corruptplans", {"months": [{"month": 5, "plans": corrupt_plans}]}, format="json"))
        try:
            result = run_async(load_plan(corrupt_legacy_storage, "corruptplans", 5, 0, "with_predbat"))
        except Exception as error:
            print("  ERROR: a legacy document whose plans is {!r} raised {} instead of returning None".format(corrupt_plans, type(error).__name__))
            failed = True
        else:
            if result is not None:
                print("  ERROR: a legacy document whose plans is {!r} should resolve to None, got {!r}".format(corrupt_plans, result))
                failed = True

    print("Test: load_plan resolves None, not an exception, when the primary key itself holds a corrupt leg blob")
    corrupt_leg_storage = FakeStorage()
    run_async(corrupt_leg_storage.save(STORAGE_MODULE, "run_corruptleg_plans_01_0", "not a dict", format="json"))
    run_async(corrupt_leg_storage.save(STORAGE_MODULE, "run_corruptleg_plans_01_1", ["not", "a", "dict"], format="json"))
    run_async(corrupt_leg_storage.save(STORAGE_MODULE, "run_corruptleg_plans_01_2", {"leg": "single"}, format="json"))  # a dict with no "scenarios" key
    run_async(corrupt_leg_storage.save(STORAGE_MODULE, "run_corruptleg", {"months": [{"month": 1, "status": "ok"}]}, format="json"))
    for index in [0, 1, 2]:
        try:
            result = run_async(load_plan(corrupt_leg_storage, "corruptleg", 1, index, "with_predbat"))
        except Exception as error:
            print("  ERROR: a corrupt leg blob at index {} raised {} instead of returning None".format(index, type(error).__name__))
            failed = True
        else:
            if result is not None:
                print("  ERROR: a corrupt leg blob at index {} should resolve to None, got {!r}".format(index, result))
                failed = True

    print("Test: load_plan returns None rather than raising for anything it cannot resolve, against a run that actually exists")
    # Uses its own storage holding the real "20260728-091000" run, not the leftover
    # legacy_storage above (which only contains "run_legacy") - reusing that would let
    # every case here short-circuit on "run not found" before reaching the out-of-range
    # month/index or bad-scenario logic it claims to exercise.
    resolve_storage = FakeStorage()
    run_async(save_run(resolve_storage, debug_results, {}, "20260728-091000"))
    for args in [
        ("20260728-091000", 99, 0, "with_predbat"),  # month not present
        ("20260728-091000", 1, 99, "with_predbat"),  # index out of range for a real month
        ("20260728-091000", 1, 0, "nope"),  # scenario not recorded on that leg
        ("nosuchrun", 1, 0, "with_predbat"),  # run id not present
        ("20260728-091000", "not-a-number", 0, "with_predbat"),  # non-integer month
        ("20260728-091000", 1, "not-a-number", "with_predbat"),  # non-integer index
        ("20260728-091000", 1, -1, "with_predbat"),  # negative index
    ]:
        try:
            if run_async(load_plan(resolve_storage, *args)) is not None:
                print("  ERROR: {} should resolve to None".format(args))
                failed = True
        except Exception as error:
            print("  ERROR: {} raised {} instead of returning None".format(args, type(error).__name__))
            failed = True

    print("Test: discarding an evicted run expires its plan keys, not just its document")
    storage = FakeStorage()
    run_async(save_run(storage, debug_results, {}, "20260728-091200"))
    keys_before = [key for key in storage.store if "plans" in str(key)]
    run_async(_discard_run(storage, "20260728-091200", run_async(list_runs(storage))[0].get("plan_keys")))
    if any(storage.store.get(key) is not None for key in keys_before):
        print("  ERROR: an evicted run's plan keys should be discarded too")
        failed = True

    print("Test: deleting a run removes its document, its plans and its index entry")
    storage = FakeStorage()
    debug_doc = {
        "annual": {"scenarios": {"no_pvbat": {"cost_p": 10.0}, "with_predbat": {"cost_p": 5.0}}, "months_included": 12},
        "months": [{"month": 1, "status": "ok", "plans": [{"day": "2025-01-08", "leg": "single", "scenarios": {"with_predbat": {"rows": [1]}}}]}],
    }
    asyncio.run(save_run(storage, debug_doc, {}, "doomed"))
    asyncio.run(save_run(storage, sample_results(1), sample_config(), "keeper"))
    plan_keys = [key for key in storage.store if "doomed" in str(key) and "plans" in str(key)]
    if not plan_keys:
        print("  ERROR: the fixture should have produced plan keys to delete")
        failed = True

    if asyncio.run(delete_run(storage, "doomed")) is not True:
        print("  ERROR: deleting a stored run should report success")
        failed = True
    if [entry["id"] for entry in asyncio.run(list_runs(storage))] != ["keeper"]:
        print("  ERROR: only the deleted run should leave the index, got {}".format(asyncio.run(list_runs(storage))))
        failed = True
    if asyncio.run(load_run(storage, "doomed")) is not None:
        print("  ERROR: a deleted run's document should no longer load")
        failed = True
    # Dropping it from the index alone would leave the plans orphaned - unreachable, but
    # still occupying storage that nothing will ever clean up.
    if any(storage.store.get(key) is not None for key in plan_keys):
        print("  ERROR: a deleted run's plan keys should be discarded too, got {}".format(plan_keys))
        failed = True
    if asyncio.run(load_run(storage, "keeper")) is None:
        print("  ERROR: deleting one run must not touch another")
        failed = True

    print("Test: deleting an unknown run reports failure rather than pretending it worked")
    if asyncio.run(delete_run(storage, "never-existed")) is not False:
        print("  ERROR: deleting a run that was never there should return False")
        failed = True
    for bad in [None, ""]:
        if asyncio.run(delete_run(storage, bad)) is not False:
            print("  ERROR: delete_run({!r}) should return False".format(bad))
            failed = True

    return failed
