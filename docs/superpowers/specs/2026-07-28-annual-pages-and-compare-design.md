# Annual Tab: Three Pages, Run Comparison, and Split Plan Storage — Design

**Status:** approved
**Date:** 2026-07-28

## Purpose

The Annual tab has grown into one long page: a configuration form, then a results view,
with nothing between them. Three problems follow from that.

1. **The form and the results read as one thing.** The settings on screen often do not
   describe the run below them — the selector can show a run from a different system
   entirely.
2. **There is no way to compare runs.** Switching the selector replaces one set of
   figures with another, so comparing two batteries means remembering numbers.
3. **Debug plans make every page expensive.** They are embedded in the results document,
   which is loaded to render totals that do not use them.

This splits the tab into three pages, adds a comparison table, and moves the captured
plans out of the results document.

## 1. Three pages

| Route | Contents |
|---|---|
| `/annual` | Configuration form, Run button |
| `/annual_view` | Selector, "what this run used", totals, chart, month table, cost/payback, plan viewer, caveats |
| `/annual_compare` | One row per stored run |

Separate URLs rather than a `?page=` parameter: each page is bookmarkable, the browser
back button behaves, and the run-completion navigation can land directly on the viewer.

### Navigation

A tab strip at the top of all three pages, with the current page marked:

```
◀   [ Configure ]  [ Results ]  [ Compare ]   ▶
```

The arrows step through that order and are **disabled at the ends rather than wrapping** —
wrapping makes "next" from Compare land on Configure, which reads as a misclick rather
than a choice.

The progress area renders on **all three** pages, hidden unless a run is in flight, so
navigating away mid-run does not lose sight of it. The existing poll already runs on every
page. On completion the tab that started the run now navigates to `/annual_view` rather
than back to the form; other tabs still only get a link, for the reason already documented
(a forced reload would discard whatever someone had typed elsewhere).

## 2. The compare page

One row per stored run, newest first:

| Column | Source |
|---|---|
| Run | index `label` + timestamp |
| Solar | summary `total_kwp` |
| Battery | summary `battery_kwh` |
| Import tariff | summary `tariff` |
| Cost with Predbat | summary `cost_with_predbat_p` |
| Saving vs no system | summary `saving_vs_none_p` |
| Payback: PV only | summary `payback_years.pv_only` |
| Payback: PV + battery | summary `payback_years.pv_battery` |
| Payback: + Predbat | summary `payback_years.pv_battery_predbat` |

Nine columns is wide, so the table scrolls inside its own `overflow-x: auto` container
rather than pushing the page sideways.

Cells are honest about what they cannot show, consistent with the viewer:

- A run whose payback was unavailable shows **—**, with the stored reason on hover.
- A row that does not pay back says **does not pay back**, never a number.
- A run with no usable month shows **—** across the figures rather than zeros.

The selected run (the one `/annual_view` is showing) is marked, and each row's label links
to `/annual_view?run=<id>`.

## 3. The run summary, and backfilling

The compare table must not read every run's document. The index entry gains a `summary`:

```json
{
  "id": "20260728-090412",
  "label": "9.5 kWh battery, 5.6 kWp, Agile",
  "months_included": 12,
  "status": "ok",
  "summary": {
    "total_kwp": 5.6,
    "battery_kwh": 9.5,
    "tariff": "Agile",
    "cost_with_predbat_p": 66000.0,
    "saving_vs_none_p": 114000.0,
    "payback_years": {"pv_only": 17.8, "pv_battery": 13.61, "pv_battery_predbat": 11.78},
    "payback_reason": null
  }
}
```

`build_summary(results, config)` is a pure function, so it is tested directly.

**Backfill.** A run saved before this change has no `summary`. On first visit to the
compare page, each such run's document is loaded once, its summary extracted, and the
index **written back**. Every later visit is index-only. Without this, twenty debug runs
would be re-read on every page load.

The backfill writes the index once per visit, not once per run, and only when something
actually changed — a compare page that changes nothing must not write storage at all.

## 4. Splitting the plans out of the results document

### Why

Measured, with the fourth scenario in place:

```
one captured plan:  44,312 bytes
   no car:   96 plans ->  4.3 MB per run
 with car:  192 plans ->  8.5 MB per run   (x20 runs = 170 MB)
```

Today that whole document is read to render totals that ignore it, and `/annual_plan`
reads all 4–8 MB to return one 44 KB plan — on every day/scenario click.

### Where the split happens

**Not in the engine.** The engine runs as a subprocess and returns its results on stdout;
its `storage` is the CLI's own work directory, not the web layer's Storage. The plans stay
embedded in the document the engine emits — that is the transport — and `save_run` splits
them out as it stores them.

This keeps the engine, the CLI and the results-document schema unchanged, and confines the
change to the storage boundary.

### Keys

Plans are addressed today as `month` + `index` + `scenario`, where `index` selects a *leg*
entry (a car-configured day has two: with-car and without-car). Keying per `(month, index)`
preserves that addressing exactly:

```
run_<id>                    ~30 KB   results, with "plans" stripped from every month row
run_<id>_plans_<MM>_<idx>  ~177 KB   that leg's four scenarios
```

24 plan keys for a car-less debug run, 48 with a car. A non-debug run writes none.

`save_run` records the list of plan keys it wrote on the index entry, so `_discard_run`
can expire them on eviction without having to re-derive which existed. `_discard_run`
already handles the absent-`delete` case by overwriting with `None` and a past expiry;
plan keys get the same treatment in a bounded loop.

### Reading

`load_plan(storage, run_id, month, index, scenario)` reads the one leg blob and returns
that scenario's plan. `/annual_plan` uses it instead of walking the results document.

**Backward compatibility:** a run stored before this change has its plans embedded and no
plan keys. `load_plan` falls back to reading them out of the results document, so existing
debug runs stay viewable rather than silently losing their plans.

### What `/annual_download` returns

The stored results document, with plans stripped — matching what is stored. The plans are
diagnostics for the viewer, not part of the results, and re-assembling 8 MB into a download
would reintroduce the cost this section exists to remove. The download link says so.

## 5. Testing

- `build_summary` against a full results document, a no-usable-month one, and one whose
  payback is unavailable.
- Backfill: an index entry lacking a summary is filled from its document and written back;
  a second visit reads no documents and writes nothing.
- `save_run` strips `plans` from the stored document and writes one key per leg; a
  non-debug run writes no plan keys; the index records the keys written.
- `_discard_run` expires an evicted run's plan keys, not just its document.
- `load_plan` returns the right scenario, falls back to an embedded-plans document, and
  returns `None` (never raises) for a month, index or scenario that does not exist.
- Each page renders only its own section: the config page has no results table, the viewer
  no form, the compare page neither.
- The nav marks the current page and disables the arrow at each end.
- The compare table reads each run's OWN summary — the same misattribution risk the run
  details table had — and renders "—"/"does not pay back" rather than a fabricated number.
