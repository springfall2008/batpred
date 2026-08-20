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

import copy
import datetime

MAX_RUNS = 20
STORAGE_MODULE = "annual"
INDEX_NAME = "runs_index"


def _run_key(run_id):
    """Return the storage filename holding one run's results document."""
    return "run_{}".format(run_id)


def _plan_key(run_id, month, index):
    """Return the storage filename holding one leg's captured plans."""
    return "run_{}_plans_{:02d}_{}".format(run_id, int(month), int(index))


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
        # What the system was reckoned to cost, so the compare table can show the price
        # beside the payback - a payback period means little without knowing the outlay
        # it is repaying. None rather than 0 when the run predates costs entirely.
        "total_gbp": costs.get("total_gbp"),
        "quoted": bool(costs.get("pv_quoted") or costs.get("battery_quoted")),
        # The tariff dicts themselves, not a rendered name. Naming one needs the merged
        # catalogue, which includes the user's own compare_list entries, and only the web
        # layer can read those - a name baked in here would be blind to them and would
        # also freeze at whatever the catalogue said on the day the run was saved. Both
        # are small: a URL, or a handful of {"rate": n} entries.
        #
        # Copied, so a later edit to the live configuration cannot reach back and rewrite
        # what a stored run says it used.
        # So the run selector and compare table can tell a fast run from a full one.
        # Comparing the two is legitimate - that is the accuracy claim - but it must be
        # visible, and a run that asked for fast mode but was given a full one reads False.
        "fast_mode": bool(((results or {}).get("annual") or {}).get("fast_mode")),
        "tariff": copy.deepcopy((config or {}).get("tariff") or {}),
        # What the no-PV/battery scenario was priced on. Carried separately because it is
        # a genuinely different tariff to the one the modelled system runs on, and a
        # saving quoted against an unnamed baseline cannot be checked.
        "baseline_tariff": copy.deepcopy((config or {}).get("baseline_tariff") or {}),
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


async def _discard_run(storage, run_id, plan_keys=None):
    """Remove an evicted run's stored document and plan keys so the ring does not leak them.

    The real Storage component has no ``delete`` method, so the primary path is
    to overwrite each document with ``None`` and set an expiry in the past, which
    lets ``storage.cleanup()`` reclaim it later. A ``delete`` method is still
    preferred when a backend (or the test's fake) provides one.
    """
    keys = [_run_key(run_id)] + list(plan_keys or [])
    if hasattr(storage, "delete"):
        for key in keys:
            await storage.delete(STORAGE_MODULE, key)
        return
    expired = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    for key in keys:
        await storage.save(STORAGE_MODULE, key, None, format="json", expiry=expired)


async def save_run(storage, results, config, run_id):
    """Save a completed run and prune the ring to MAX_RUNS. Returns the run id.

    Captured plans (present only on a debug run) are stripped out of the results
    document and written under their own key per leg, one leg per sampled day, so
    the results document - read to render totals that ignore the plans - stays
    small, and each plan can be fetched on its own rather than re-reading a
    several-megabyte document on every click of the plan viewer.

    A small ``plan_index`` (month, index, day, leg - no payload) is recorded on the
    index entry alongside ``plan_keys``, so the results page's plan viewer can build
    its day/scenario selector from the index entry rather than from the document,
    which - now that this same function has stripped the plans out of it - no
    longer carries them.

    ``results`` is deep-copied before anything is stripped from it, so the
    caller's own dict (the web layer still holds it after this returns) is left
    untouched.

    The evicted run's document and plan keys are discarded as well as its index
    entry, so the ring cannot leak documents that nothing references.
    """
    if not storage:
        return run_id

    stored = copy.deepcopy(results) if isinstance(results, dict) else results
    plan_keys = []
    plan_index = []
    if isinstance(stored, dict):
        for month in stored.get("months") or []:
            if not isinstance(month, dict):
                continue
            plans = month.pop("plans", None)
            if not isinstance(plans, list):
                continue
            for index, leg in enumerate(plans):
                key = _plan_key(run_id, month.get("month", 0), index)
                await storage.save(STORAGE_MODULE, key, leg, format="json")
                plan_keys.append(key)
                plan_index.append(
                    {
                        "month": month.get("month", 0),
                        "index": index,
                        "day": leg.get("day") if isinstance(leg, dict) else None,
                        "leg": leg.get("leg", "single") if isinstance(leg, dict) else "single",
                    }
                )

    await storage.save(STORAGE_MODULE, _run_key(run_id), stored, format="json")

    annual = results.get("annual", {}) if isinstance(results, dict) else {}
    entry = {
        "id": run_id,
        "timestamp": run_id,
        "label": build_label(config),
        "months_included": annual.get("months_included", 0),
        "status": "ok" if annual.get("months_included") else "empty",
        "summary": build_summary(results, config),
        "plan_keys": plan_keys,
        "plan_index": plan_index,
    }

    index = await list_runs(storage)
    index = [existing for existing in index if existing.get("id") != run_id]
    index.insert(0, entry)

    for dropped in index[MAX_RUNS:]:
        await _discard_run(storage, dropped["id"], dropped.get("plan_keys"))
    index = index[:MAX_RUNS]

    await storage.save(STORAGE_MODULE, INDEX_NAME, index, format="json")
    return run_id


async def load_plan(storage, run_id, month, index, scenario):
    """Return one captured plan, or None when it cannot be resolved.

    Reads the single leg's own key - about 177 KB - rather than the whole results
    document, which for a debug run is several megabytes and would be re-read on every
    click of the plan viewer's day and scenario selectors.

    Falls back to a document that still carries its plans inline, so a run stored before
    they were split out stays viewable instead of silently losing them.

    Never raises: month, index and scenario arrive off an attacker-controlled query
    string, and every failure to resolve is a None the caller turns into a 404.
    """
    if not storage or not run_id or not scenario:
        return None
    try:
        month = int(month)
        index = int(index)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None

    leg = await storage.load(STORAGE_MODULE, _plan_key(run_id, month, index))
    if isinstance(leg, dict):
        scenarios = leg.get("scenarios")
        return scenarios.get(scenario) if isinstance(scenarios, dict) else None

    results = await load_run(storage, run_id)
    if not isinstance(results, dict):
        return None
    for entry in results.get("months") or []:
        if not isinstance(entry, dict) or entry.get("month") != month:
            continue
        plans = entry.get("plans")
        if not isinstance(plans, list) or index >= len(plans):
            return None
        candidate = plans[index]
        if not isinstance(candidate, dict):
            return None
        scenarios = candidate.get("scenarios")
        return scenarios.get(scenario) if isinstance(scenarios, dict) else None
    return None


def _summary_is_current(summary):
    """Return True when a summary carries the fields the compare table reads today.

    Presence alone is not enough. A run stored before the compare table named three
    tariffs has a summary, but it holds a single pre-rendered import name and neither
    tariff dict - so those rows would show dashes forever despite the run's own document
    holding the answer. ``baseline_tariff`` is the marker because ``build_summary``
    always writes it, even as an empty dict, so it is present exactly when the summary
    was written by the current code.
    """
    return isinstance(summary, dict) and "baseline_tariff" in summary


async def backfill_summaries(storage, runs):
    """Return ``runs`` with any missing or outdated summary rebuilt, writing the index back once.

    A run stored before summaries existed has none; one stored before the tariff fields
    has a summary that predates them. Both are rebuilt from the run's own document.
    Rather than re-reading that document on every visit to the compare page - several
    megabytes for a debug run - the result is persisted, so every later visit is
    index-only.

    The index is written at most once per call, and only when something was actually
    rebuilt: a compare page that changes nothing must not write storage at all.
    """
    if not storage or not runs:
        return runs or []

    filled = False
    for entry in runs:
        if _summary_is_current(entry.get("summary")) or not entry.get("id"):
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


async def delete_run(storage, run_id):
    """Remove one run entirely - its document, its captured plans and its index entry.

    Reuses the same discard path eviction uses, so a run the user deletes leaves no more
    behind than one that aged out of the ring: the document and every plan key are
    expired, not merely unlinked from the index. Storage has no ``delete`` method on the
    real component, which is why discarding is overwrite-with-expiry rather than removal.

    Returns True when a run was found and removed, False when the id matched nothing -
    so the caller can tell "deleted" from "already gone" rather than reporting success
    for a run that was never there.
    """
    if not storage or not run_id:
        return False

    index = await list_runs(storage)
    entry = next((existing for existing in index if existing.get("id") == run_id), None)
    if entry is None:
        return False

    await _discard_run(storage, run_id, entry.get("plan_keys"))
    await storage.save(STORAGE_MODULE, INDEX_NAME, [existing for existing in index if existing.get("id") != run_id], format="json")
    return True
