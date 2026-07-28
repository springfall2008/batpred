# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Storage-backed history of annual prediction runs.

Keeps the most recent runs so a user can flip between "with a 5 kWh battery" and
"with a 10 kWh battery" without re-running either. Everything goes through the
Storage abstraction rather than the filesystem, because there may not be one.
"""

import datetime

MAX_RUNS = 20
STORAGE_MODULE = "annual"
INDEX_NAME = "runs_index"


def _run_key(run_id):
    """Return the storage filename holding one run's results document."""
    return "run_{}".format(run_id)


def _describe_tariff(tariff):
    """Return a short human name for the tariff a run used."""
    if not isinstance(tariff, dict):
        return "tariff"
    url = tariff.get("import_octopus_url") or ""
    for name in ["AGILE", "INTELLI-FLUX", "INTELLI", "FLUX", "COSY", "SNUG", "GO"]:
        if name in url.upper():
            return name.title().replace("Intelli-Flux", "Intelligent Flux").replace("Intelli", "Intelligent Go")
    if tariff.get("rates_import"):
        return "fixed rates"
    return "tariff"


def build_label(config):
    """Return a short human label describing the configuration a run used.

    A selector listing five bare timestamps tells the user nothing about which
    run was which, which defeats the point of keeping more than one.
    """
    config = config if isinstance(config, dict) else {}
    parts = []
    battery = config.get("battery")
    if isinstance(battery, dict) and battery.get("size_kwh"):
        parts.append("{}kWh battery".format(battery["size_kwh"]))
    else:
        parts.append("no battery")

    solar = config.get("solar")
    if solar:
        total_kwp = sum(array.get("kwp", 0) for array in solar if isinstance(array, dict))
        if total_kwp:
            parts.append("{}kWp".format(round(total_kwp, 2)))
    else:
        parts.append("no solar")

    parts.append(_describe_tariff(config.get("tariff")))
    return " · ".join(parts)


def build_summary(results, config):
    """Return the headline figures the compare table shows for one run.

    Read once at save time and cached on the index entry, so comparing twenty runs
    reads one small index rather than twenty results documents - a debug run's
    document is several megabytes.

    Every figure is None when it could not be computed, never zero: the compare table
    renders None as "-" and a zero as a real cost, and confusing "we do not know" with
    "it costs nothing" is exactly the failure this tool avoids elsewhere.
    """
    annual = (results or {}).get("annual") or {}
    scenarios = annual.get("scenarios") or {}
    costs = annual.get("costs") or {}
    payback = annual.get("payback") or {}

    # Matches the isinstance guard in annual_costs.py's payback calculation: a missing
    # or malformed cost_p is a different situation to a zero saving, and must not be
    # silently treated as one, whether the field is absent or simply not a number
    # (as a partially-written or otherwise corrupt document might contain).
    raw_baseline = (scenarios.get("no_pvbat") or {}).get("cost_p")
    raw_predbat = (scenarios.get("with_predbat") or {}).get("cost_p")
    baseline = raw_baseline if isinstance(raw_baseline, (int, float)) else None
    predbat = raw_predbat if isinstance(raw_predbat, (int, float)) else None

    payback_years = {}
    if payback.get("available"):
        for key in ["pv_only", "pv_battery", "pv_battery_predbat"]:
            row = payback.get(key) or {}
            # None for a row that does not pay back, so the table can say so rather
            # than print a number that does not exist.
            payback_years[key] = row.get("years") if row.get("pays_back") else None

    return {
        "total_kwp": costs.get("total_kwp"),
        "battery_kwh": costs.get("battery_kwh"),
        "tariff": _describe_tariff((config or {}).get("tariff") or {}),
        "cost_with_predbat_p": predbat,
        "saving_vs_none_p": (baseline - predbat) if (baseline is not None and predbat is not None) else None,
        "payback_years": payback_years,
        "payback_reason": None if payback.get("available") else payback.get("reason"),
        "months_included": annual.get("months_included", 0),
    }


async def list_runs(storage):
    """Return the stored runs newest-first, or an empty list when there are none.

    A corrupt or unexpected index reads as empty rather than raising: the tab
    must still render so the user can start a fresh run.
    """
    if not storage:
        return []
    index = await storage.load(STORAGE_MODULE, INDEX_NAME)
    if not isinstance(index, list):
        return []
    return [entry for entry in index if isinstance(entry, dict) and entry.get("id")]


async def load_run(storage, run_id):
    """Return one run's results document, or None when it is unknown or missing."""
    if not storage or not run_id:
        return None
    return await storage.load(STORAGE_MODULE, _run_key(run_id))


async def _discard_run(storage, run_id):
    """Remove an evicted run's stored document so the ring does not leak documents.

    The real Storage component has no ``delete`` method, so the primary path is
    to overwrite the document with ``None`` and set an expiry in the past, which
    lets ``storage.cleanup()`` reclaim it later. A ``delete`` method is still
    preferred when a backend (or the test's fake) provides one.
    """
    if hasattr(storage, "delete"):
        await storage.delete(STORAGE_MODULE, _run_key(run_id))
        return
    expired = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    await storage.save(STORAGE_MODULE, _run_key(run_id), None, format="json", expiry=expired)


async def save_run(storage, results, config, run_id):
    """Save a completed run and prune the ring to MAX_RUNS. Returns the run id.

    The evicted run's document is discarded as well as its index entry, so the
    ring cannot leak documents that nothing references.
    """
    if not storage:
        return run_id

    await storage.save(STORAGE_MODULE, _run_key(run_id), results, format="json")

    annual = results.get("annual", {}) if isinstance(results, dict) else {}
    entry = {
        "id": run_id,
        "timestamp": run_id,
        "label": build_label(config),
        "months_included": annual.get("months_included", 0),
        "status": "ok" if annual.get("months_included") else "empty",
        "summary": build_summary(results, config),
    }

    index = await list_runs(storage)
    index = [existing for existing in index if existing.get("id") != run_id]
    index.insert(0, entry)

    for dropped in index[MAX_RUNS:]:
        await _discard_run(storage, dropped["id"])
    index = index[:MAX_RUNS]

    await storage.save(STORAGE_MODULE, INDEX_NAME, index, format="json")
    return run_id


async def backfill_summaries(storage, runs):
    """Return ``runs`` with any missing summary filled in, writing the index back once.

    A run stored before summaries existed has none. Rather than re-reading its document
    on every visit to the compare page - several megabytes for a debug run - its summary
    is computed once and persisted, so every later visit is index-only.

    The index is written at most once per call, and only when something was actually
    filled in: a compare page that changes nothing must not write storage at all.
    """
    if not storage or not runs:
        return runs or []

    filled = False
    for entry in runs:
        if entry.get("summary") or not entry.get("id"):
            continue
        results = await load_run(storage, entry["id"])
        if not isinstance(results, dict):
            continue
        # build_summary() already guards against a missing or non-numeric cost_p, but this
        # loop reads arbitrary previously-stored documents, so one entry that is corrupt in
        # some other way must not abandon the runs already filled - and, critically, must
        # not skip the index write below that persists them.
        try:
            entry["summary"] = build_summary(results, results.get("config") or {})
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
        filled = True

    if filled:
        await storage.save(STORAGE_MODULE, INDEX_NAME, runs, format="json")
    return runs
