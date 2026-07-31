# Monotonic `tweak_plan` and `optimise_full_second_pass`

**Date:** 2026-07-31
**Status:** Approved

## Problem

`tweak_plan` and `optimise_full_second_pass` can hand back a plan that is worse than the one
they were given.

Both iterate the windows in the record period, call `optimise_charge_limit` or `optimise_export`
on one window at a time, and write the result straight back with no check that the whole plan
improved. Neither pass has a notion of "this made things worse, put it back".

The symptom reported from the field (PR #4398): during a force export at the highest export price
of the horizon, a recompute cancelled the export and switched the inverter to Demand. The plan
handed to `tweak_plan` had metric -16218.30; the plan it returned had metric -15846.94 — 371.36
worse — with the running export moved from "start now" to "start 35 minutes from now". The export
then flapped `Exporting -> Demand -> Exporting -> Demand` over the next 16 minutes, idling the
battery through the best-priced hour of the horizon.

The whole-plan guard in `should_replace_plan` did not catch it, because that compares the new plan
against the *previous cycle's* plan. The value `tweak_plan` had just destroyed was never visible
to it.

Two days of production logs from the reporting system (Kostal, 44.79 kWh, `calculate_second_pass`
off) showed 25 of 104 `tweak_plan` calls regressed the plan — 24%.

## Root cause

Nothing is blocking the optimisers, and neither is blind to the status quo. `optimise_charge_limit`
explicitly forces the current setting into its candidate list (plan.py, "Keep the current setting if
different from the selected ones"), and `optimise_export` regenerates the full start grid so the
current start is re-enumerated. The setting the plan already holds is always scored. It is simply
not *privileged*, and it is not scored on the yardstick the plan is finally judged by.

Three distinct causes, in rough order of how much damage they do at default settings:

### A. The ranking score is not the plan metric

Inside `optimise_export` the metric carries adjustments the whole-plan metric does not: the
in-progress export commitment bonus, and small tie-break weightings (-0.002 for off, -0.001 for
0%). `optimise_charge_limit` has its own (-0.003 / -0.002 / -0.001, plus
`-max(0.1, metric_min_improvement)` when the inverter's live target matches the candidate).

Maximising that adjusted score can move the plan metric the wrong way. This is the direct cause of
the reported symptom: the commitment bonus added by #4118 was applied to *every* candidate in the
window, including ones starting after `minutes_now`. Giving "stop now, restart later" the same bonus
as "keep exporting" cancels it out, so a delayed start scored equal-or-better internally while
costing the plan 371 metric.

### B. The reference point is a fixed default, not the status quo

Both functions latch their comparison basis on their *first* candidate:

| Function | First candidate | Latched into | Fallback if nothing wins |
|----------|-----------------|--------------|--------------------------|
| `optimise_charge_limit` | highest SoC (`try_socs = [loop_soc]`) | `best_metric_first` | `best_soc = soc_max` |
| `optimise_export` | export off, at the window start | `off_metric` / `off_cost` | `best_export = 100.0` |

So every window is re-decided as though fresh, against "charge to max" or "do not export".
Whatever an earlier pass chose has to re-win from scratch against that default, in a context where
other windows have since moved (`optimise_swap_export` runs immediately before, and mutates the
plan).

### C. The export gate is a cost test, but the plan is judged on metric

`(cost + min_improvement_scaled) <= off_cost` is deliberate — it is the #2984 fix that stops exports
which only game `metric_keep` without real cash savings. But the metric also carries battery value,
cycle cost and carbon, so an export that improves the metric can still fail the cash test and be
dropped.

### Why hysteresis is not the main culprit

`metric_min_improvement` defaults to **0.0**, so charge windows have no margin to clear at all.
`metric_min_improvement_export` defaults to **0.1**, which scales to roughly 0.4p on a two-hour
window at a 30-minute plan interval. The min_improvement bar is a minor contributor at defaults.
Cause A is doing the damage.

## Approach

Fixing A or B properly means seeding the reference with the current setting's metric, or stripping
the adjustments out of the ranking. Both change fresh-plan behaviour, because `optimise_levels_pass`
and `optimise_detailed_pass` call the same two functions — and the levels pass genuinely wants
fresh-decision semantics. That is a much larger blast radius than this change warrants.

Instead: leave every selection rule untouched and refuse to write back a net regression. The sibling
passes already work this way — `optimise_swap_export` measures, mutates, re-measures and restores;
`optimise_solar` snapshots, mutates, re-measures and reverts on a threshold. This brings tweak and
the second pass in line rather than introducing a new principle.

This treats the symptom, not the cause. The passes will keep *proposing* regressions; we stop
accepting them. See Follow-ups.

## Design

### 1. Window snapshot helpers

Every plan mutation in both passes is at index `window_n`, and neither optimiser touches
`self.*_best` at all — both work on copies (`try_charge_limit = list(charge_limit)`,
`try_export_window = copy.deepcopy(export_window)`). So a single-window restore is sufficient; the
whole-plan deepcopy that `optimise_solar` uses is not needed here.

Two helpers on `PredBat` in `plan.py`, placed next to `should_replace_plan`:

```python
def plan_window_snapshot(self, typ, window_n):
    """Copy the single window a pass can modify, so the change can be undone."""
    if typ == "c":
        return self.charge_limit_best[window_n]
    return self.export_limits_best[window_n], copy.deepcopy(self.export_window_best[window_n])

def plan_window_restore(self, typ, window_n, snapshot):
    """Put the window back as it was when plan_window_snapshot() captured it."""
    if typ == "c":
        self.charge_limit_best[window_n] = snapshot
    else:
        self.export_limits_best[window_n], self.export_window_best[window_n] = snapshot
```

The export branch copies the **whole window dict** rather than naming `start` and `start_orig`
individually. Same size, but it restores `start_orig` correctly whether or not it existed
beforehand, and it will not quietly go wrong if a field is added to the window dict later.

Restoring by element reassignment is safe: `window_links` holds only a type and an integer index,
and both loops re-read `self.export_window_best[window_n]` each iteration, so no stale reference to
the replaced dict survives.

### 2. The guard in both passes

Each pass measures its baseline once on entry, then wraps each existing window change:

```python
snapshot = self.plan_window_snapshot(typ, window_n)
<existing window change, untouched>
candidate = self.run_prediction_metric(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, end_record=end_record)
if candidate[0] < selected[0]:   # element 0 is the metric, lower is better
    selected = candidate
else:
    self.plan_window_restore(typ, window_n, snapshot)
```

`end_record` is the `tweak_plan` parameter of that name in the first pass, and `self.end_record` in
the second — matching what each pass already passes to its optimiser calls.

`selected` is returned at the end of the pass in place of the values previously carried out of the
last optimise call.

**The baseline cannot come from the caller.** `optimise_swap_export` runs immediately before both
passes, mutates the plan, and its return value is discarded at the call site. The `best_metric` the
caller holds is therefore already stale by the time either pass is entered.

**Comparison is strict (`<`).** A change that merely ties is reverted, which reduces plan churn
between cycles. This differs from `optimise_swap_export`, which accepts ties with `<=`.

Any log line reads from `selected` by unpacking it first rather than indexing it positionally.
Reverts are logged under `debug_enable`.

### 3. Scope the in-progress export commitment bonus

In `optimise_export`, move

```python
metric -= max(0.5, self.metric_min_improvement_export)
```

inside the existing `if start <= self.minutes_now:` so a candidate that starts *after* the current
minute no longer receives the in-progress bonus. This matches the `keep_export` condition that
already sits directly below it, and is the direct fix for cause A.

### 4. Signature

`tweak_plan(self, end_record)` — the `best_metric` and `metric_keep` parameters become unused once
the baseline is measured internally, so they go, along with the one caller in `optimise_all_windows`.
`optimise_full_second_pass` keeps its signature; its `best_soc_min` and `best_battery_value`
parameters are still passed through unchanged.

## Testing

In `apps/predbat/tests/test_export_commitment.py`:

1. **Prerequisite fix to `setup_single_export_window`.** `Prediction.__init__` snapshots `soc_kw`,
   `soc_max` and the rate tables at construction time, but the helper builds it *before* those are
   assigned. The prediction object therefore simulates an empty battery (`soc_kw = 0.0`), every plan
   in the module costs 0.0, and any assertion on a plan metric is silently a no-op. Move the
   `Prediction(...)` construction to after `reset_rates` / `update_rates_export`. Verified: the three
   existing tests still pass, and a metric assertion that previously compared `0.0` to `0.0` then
   reports a real `-215.0` to `-200.0` regression.

2. **`tweak_plan` reverts a window change that worsens the plan.** Stub `optimise_export` to return
   a worse option than the plan holds (export off, start delayed) and assert the limit, the start and
   the absence of `start_orig` are all restored, and that the plan metric did not worsen.

3. **`optimise_full_second_pass` reverts likewise.** New coverage — the existing
   `optimise_windows_kernel` test exercises `second_pass=True` but only proves the pass still runs.

4. **An in-progress export is not restarted later inside its own window.** Covers §3.

Each new test must be verified to fail with the guard removed, not merely to pass with it.

Full suite (`./run_all`), not just `--quick`, plus `./run_pre_commit`.

## Risks and non-goals

- **The guard's objective excludes the commitment bonus.** An in-progress export that is
  raw-metric-worse than what the plan already holds will be reverted, because `run_prediction_metric`
  carries no bonus. This follows from the strict comparison and is stated in the helper docstring so
  it is a documented property rather than a surprise. In practice it rarely bites: in tweak mode the
  incoming plan already holds the export, so re-selecting it is a no-change tie.
- **`tweak_plan`'s 8-window budget is spent on reverted windows too**, so a cycle can burn its budget
  achieving nothing. Existing behaviour of the cap; not changed here.
- **Performance.** One extra `run_prediction_metric` (two simulations) per window. `tweak_plan` is
  capped at 8 windows. `optimise_full_second_pass` is uncapped but `calculate_second_pass` defaults
  to `False`. Measured on `optimise_windows_kernel` in #4398: 46.95s / 47.65s without, 42.69s /
  43.04s with — no regression, plausibly because later windows are no longer optimised on top of a
  degraded intermediate plan.
- **Not doing:** skipping the re-measure when the window is unchanged. It would save roughly a third
  of the added cost but adds a branch that is not needed at these window counts.
- **Not touching:** `optimise_detailed_pass`, `optimise_levels_pass`, `optimise_solar`,
  `should_replace_plan`.

## Follow-ups

Open an issue for root causes A and B — the ranking score diverging from the plan metric, and the
reference point being a fixed default rather than the status quo. Fixing those would make the passes
monotonic by construction and let the guard become a cheap assertion rather than a correction.

## Provenance

The diagnosis, the field logs and the performance measurements are from PR #4398 by @mbuhansen.
This design keeps that analysis and reduces the implementation from roughly 50 lines of helpers to
about 10, after establishing that only one window changes per iteration.
