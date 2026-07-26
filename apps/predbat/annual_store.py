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

MAX_RUNS = 5
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
    parts = []
    battery = config.get("battery") if isinstance(config, dict) else None
    if isinstance(battery, dict) and battery.get("size_kwh"):
        parts.append("{}kWh battery".format(battery["size_kwh"]))
    else:
        parts.append("no battery")

    solar = config.get("solar") if isinstance(config, dict) else None
    if solar:
        total_kwp = sum(array.get("kwp", 0) for array in solar if isinstance(array, dict))
        if total_kwp:
            parts.append("{}kWp".format(round(total_kwp, 2)))
    else:
        parts.append("no solar")

    parts.append(_describe_tariff((config or {}).get("tariff")))
    return " · ".join(parts)


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
    }

    index = await list_runs(storage)
    index = [existing for existing in index if existing.get("id") != run_id]
    index.insert(0, entry)

    for dropped in index[MAX_RUNS:]:
        await _discard_run(storage, dropped["id"])
    index = index[:MAX_RUNS]

    await storage.save(STORAGE_MODULE, INDEX_NAME, index, format="json")
    return run_id
