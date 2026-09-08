# Annual compare table: three tariffs

## Problem

The WhatIf compare table shows one `Tariff` column per run. Since import and export
tariffs became independent selections, and the no-PV/battery scenario gained its own
baseline tariff, one column cannot describe what a run actually used — two runs
differing only in their export deal look identical, and the baseline is invisible
everywhere.

The column is fed by `summary["tariff"]`, a string built at save time by
`annual_store._describe_tariff`, which searches the import URL for one of seven
hard-coded product substrings. Anything else renders as `fixed rates` or the generic
`tariff`. It has no notion of the export or baseline sides at all.

## Design

### Naming lives in the web layer, not the store

Names must come from the merged catalogue, which includes the user's own `compare_list`
entries — and only the web layer can read those. So the store stops pre-rendering a
name and instead carries the raw tariff dicts, which are small (a URL, or a handful of
`{"rate": n}` entries):

```python
# annual_store.build_summary
"tariff": copy.deepcopy(config.get("tariff") or {}),
"baseline_tariff": copy.deepcopy(config.get("baseline_tariff") or {}),
```

`_describe_tariff` and the old `"tariff"` string are removed. `build_label` keeps its
own short run label unchanged — that is a different job, and shortening matters there.

### One reverse-match rule, shared

`AnnualPage._selected_side_id` already maps a tariff dict back to a catalogue id so the
form's dropdowns can show the saved selection. That logic moves to the catalogue module
as the single rule both callers use:

```python
# tariff_catalogue.py
def match_entry(catalogue, tariff, url_key, rates_key):
    """Return the catalogue entry this tariff side was chosen from, or None."""
```

`_selected_side_id` becomes a thin wrapper returning `entry["id"]`, so the form's
behaviour is unchanged and the compare table cannot drift from it.

A cell then resolves as: `match_entry(...)` → that entry's `name`; otherwise the
existing `AnnualPage._describe_tariff_side` fallback, which already handles a
hand-entered custom URL (its Octopus product code), a flat rate (`flat 26.11p`), a
deliberate no-export run (`no export payment`) and an unset side (`not set`).

Result: `Price cap`, `Agile`, `Octopus Outgoing Fixed` rather than `fixed rates`,
`Agile`, `fixed rates`.

### The table

`Tariff` is replaced by `Baseline`, `Import` and `Export`, in that order — the baseline
first because it is what the other two are being judged against.

That takes the table to 13 columns, so the width is addressed at the same time.
`table.annual-compare { white-space: nowrap }` currently applies to headers as well as
data, which forces every column to be at least as wide as its header set on one line —
`PV + battery pays back in` alone is ~24 characters holding open a column whose data is
`4.2 years`. Restricting the rule to `td` and letting `th` wrap lets each column shrink
to its content, which is the actual fix for the columns that are too wide.

### Runs already stored

`annual_store.backfill_summaries` already exists for runs saved before summaries did:
it reads each such run's document once, rebuilds the summary, and writes the index back
a single time so later visits stay index-only. Its trigger widens from "summary is
missing" to "summary is missing **or** predates the tariff fields":

```python
def _summary_is_current(summary):
    """Return True when this summary carries the fields the compare table reads."""
    return isinstance(summary, dict) and "baseline_tariff" in summary
```

The stored results document holds the validated config, in which `validate_config`
(`annual.py:313`) has already substituted `DEFAULT_BASELINE_TARIFF` when the run did not
name one — so a backfilled row shows the baseline that run genuinely used, not a blank.

### Results page consistency

`_render_run_details` ("What this run used") gains a **Baseline tariff** row, and its
existing Import/Export rows switch to the same catalogue-name-first helper. Without
this the two pages would name the same run's tariff differently, and only one of them
would mention the baseline.

## Testing

`test_annual_store.py`

- `build_summary` carries both tariff dicts and no longer emits the `"tariff"` string
- a summary predating the change is detected as stale and backfilled from the document
- a current summary is left alone and the index is **not** rewritten
- a run whose document is missing or corrupt is skipped without abandoning the rest

`test_tariff_catalogue.py`

- `match_entry` matches a side on its URL and on its rates list, and returns `None` for
  a side that matches nothing in the catalogue
- the form's dropdown selection is unchanged by the extraction: `_selected_side_id`
  still returns the same ids it did before, including `custom` for an unmatched side

`test_web_annual.py`

- the compare table renders `Baseline`, `Import`, `Export` headers and the catalogue
  names in the right cells
- a custom URL falls back to its product code, `no_export` renders as its catalogue name
- a run with an old-style summary still renders rather than raising
- the run details table shows the baseline row
- `th` is not covered by the nowrap rule

## Out of scope

- Changing `build_label`, the short run label in the selector
- Sorting or filtering the compare table by tariff
