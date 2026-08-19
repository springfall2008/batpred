# pv90 upside scenario

**Date:** 2026-08-08
**Status:** Approved

## Problem

Predbat simulates two futures: nominal (p50) and a pessimistic bundle (`pv10`: low PV, load
scaled to 110%, charge rate de-rated to 80%, Octopus IO slots assumed to vanish). Risk is
therefore one-sided. The optimiser is rewarded for hedging against a bad solar day and is never
charged for a hedge that turns out to be unnecessary.

The symptom, from the debug dump `coverage/predbat_no_export_100.txt` (GivEnergy 9.52 kWh,
export rate 0p everywhere, import 6.9p off-peak / 28.85p peak): Predbat charges to 100% overnight
even though the plan carries ~40% battery at `end_record` and the battery reaches 99% at 05:10
with only 0.07 kWh of headroom against an 8.39 kWh forecast solar day. Any PV over-performance
spills to the grid at 0p — energy bought at 6.9p and given away.

### Why 100% wins today

Two effects compound.

**The central forecast is exactly indifferent.** `compute_metric` (plan.py) credits leftover
battery at `end_record` using

```python
rate_min = rate_min_forward.get(minutes_now + end_record) / inverter_loss / battery_loss
battery_value = soc * metric_battery_value_scaling * max(rate_min, 1.0, rate_export_min)
```

With `rate_min_forward` at 6.9p and losses of 0.96 x 0.97 this credits **7.4098 p per stored
kWh**. The marginal cost of putting a kWh into the battery in that same 6.9p window is
`6.9 / (0.96 x 0.97)` = **7.4098 p**. Identical. Measured across the candidate charge limits:

| target | cost | battery_value | cost - value |
|---|---|---|---|
| 9.52 (100%) | 402.42 | 27.95 | 374.47 |
| 9.02 (95%) | 398.72 | 24.25 | 374.47 |
| 8.52 (89%) | 395.01 | 20.54 | 374.47 |
| 8.27 (87%) | 393.16 | 18.69 | 374.47 |

The metric is flat from 6.27 kWh (66%) to 9.52 kWh (100%). Below 66% the battery hits reserve and
the metric climbs steeply.

**So the decision falls entirely to tiebreaks, and only a pessimistic one exists.** In the pv10
scenario the battery ends at reserve for every candidate, so `cost10` falls about 19.5p per extra
kWh of target; at weight 0.1 that is ~1.95 p/kWh pulling towards 100%. Failing that, an explicit
`metric -= 0.002` bonus for `try_soc == soc_max` (plan.py) settles it.

Confirmed by sweeping the dump:

| setting | chosen limits |
|---|---|
| baseline | 100%, 100% |
| `pv_metric10_weight` = 0 | 100%, 100% (flat plateau + the soc_max bonus) |
| `pv_metric10_weight` = 0.3 | 100%, 100% |
| `metric_battery_value_scaling` = 0.75 | 100%, 100% |
| `metric_battery_value_scaling` = 0.70 | 66%, 100% |

The flip point matches the arithmetic: a residual-value haircut must beat the pv10 pull,
`(1 - s) x 7.4098 > 1.95` → `s < 0.737`.

### Why a valuation fix alone is not the answer

The formula does over-value residual battery: it credits the *entry cost*
(`rate / (inv_loss x batt_loss)` = 7.4098p) where the exit value is
`rate x inv_loss x batt_loss_discharge` = 6.4253p — losses grossed up where a discharge-side
quantity should be scaled down, an over-valuation of ~15.3%. Correcting it was measured on this
case:

| formula | credit | w10 = 0.1 | w10 = 0 |
|---|---|---|---|
| current | 7.4098 p | 100% | 100% |
| exit-value | 6.4253 p | 100% | 66% |
| no loss gross-up | 6.9 p | 100% | 66% |

The correction creates a ~0.99 p/kWh preference for charging less — the physically real
round-trip loss — but the pv10 hedge outweighs it at 1.95 p/kWh.

More fundamentally, the residual is not the problem. The plan ends at 40% and the house genuinely
consumes all of it in the p50 model. What is risky is *mid-plan* fullness. No residual-valuation
formula can separate 80% from 100% on economics, because in the p50 model both are correct. The
difference between them is **risk**, and risk lives in the scenario set.

`metric_battery_value_scaling` = 0.5 "fixes" the symptom only by making carrying energy so lossy
that it swamps the pv10 hedge, which would also stop Predbat carrying energy in cases where doing
so is right.

## Approach

Add a third simulated scenario, `pv90` (high PV, low load), and replace the one-sided pv10 risk
clamp with a signed weighted average over all three.

The one-sided clamp is load-bearing here: today's blend is
`if metric10 > metric: metric += (metric10 - metric) * weight`. In the pv90 world there is more PV
and less load, so `metric90 < metric` essentially always, and a one-sided clamp would contribute
exactly zero at every candidate charge limit. pv90 only produces a gradient as a true signed
weighted average.

## Design

### 1. Scenario selector replaces the `pv10` boolean

`pv10` becomes an integer `pv_scenario` with constants `PV_SCENARIO_NOMINAL = 0`,
`PV_SCENARIO_PV10 = 1`, `PV_SCENARIO_PV90 = 2`.

Threaded through the four `launch_run_prediction_*` methods (plan.py:629-671), the four
`wrapped_*` module functions and four `thread_run_prediction_*` methods (prediction.py:36-61,
209-375), `Prediction.run_prediction`, and `run_prediction_kernel`.

Behaviour by scenario inside `run_prediction`:

| | nominal | pv10 | pv90 |
|---|---|---|---|
| PV series | `pv_forecast_minute_step` | `pv_forecast_minute10_step` | `pv_forecast_minute90_step` |
| load series | `load_minutes_step` | `load_minutes_step10` | `load_minutes_step90` |
| charge rate scaling | `battery_rate_max_scaling` | `x charge_scaling10` | `battery_rate_max_scaling` (no de-rate) |
| IO slot assumption | normal | assumed lost, `rate_max` applied | normal |

`PV_SCENARIO_PV10 = 1` keeps any stale `pv10=True` numerically valid, but every call site is
updated explicitly rather than relying on that.

There is deliberately no `charge_scaling90`. The pv10 de-rate models a hedge against slow
charging; it has no upside counterpart.

### 2. pv90 data

Source chain, first available wins:

1. `forecast90` attribute on `sensor.<prefix>_pv_forecast_raw` (new)
2. Fallback: `pv90[m] = pv50[m]` — the nominal series, unchanged

No upside is synthesised. Solcast generates a real p90 in practice, so the fallback only covers
sources that do not (`solar_model.py`, Open-Meteo) and historical debug dumps captured before
`forecast90` existed. Mirroring the p10 spread about p50 was considered and rejected: solar
upside is bounded by clear-sky while the downside is not, so a mirrored p90 would overstate the
upside and contaminate any result measured through it.

The consequence is that under the fallback the pv90 scenario differs from nominal **only** by
`load_scaling90`. That is deliberate. It makes the fallback strictly conservative — the scenario
can only ever be milder than a real p90 would be, never more aggressive — so a result obtained
through it is a lower bound on the effect, and real p90 data can strengthen the conclusion but
not overturn it.

If `load_scaling90` is also left at 1.0 the pv90 scenario becomes identical to nominal,
`metric90 == metric`, and the blend degenerates to an identity. Harmless, and worth a test.

`solcast.py` already computes `pv_estimate90` per period and a `best_day_scaling` in the
calibration path, but only ever builds a *minute* array for p10. Changes:

- build `pv_forecast_minute90` alongside `pv_forecast_minute10`
- `pv_calibration()` (solcast.py:850) returns it
- `pack_and_store_forecast()` (solcast.py:1228) packs it and publishes `forecast90`

`fetch.py`:

- `fetch_pv_forecast()` (fetch.py:1292) returns a third dict, reading `forecast90` when present
  and copying the p50 series when absent
- stored as `self.pv_forecast_minute90`

`solar_model.py` and Open-Meteo produce no p90 and fall through to the p50 fallback.

`plan.py` builds `pv_forecast_minute90_step` and `load_minutes_step90` alongside the existing
step arrays, using `load_scaling90` for the load series.

pv90 uses the **nominal** `metric_cloud_coverage` and `metric_load_divergence` rather than
mirrored variants. `cloud_factor` in `step_data_history` shuffles energy between adjacent
5-minute slots and preserves the total (fetch.py:161-171), so mirroring it would add knobs
without changing the scenario's level. The scenario is defined purely by the p90 PV series and
`load_scaling90`.

### 3. Configuration

Two new `CONFIG_ITEMS` entries, both `enable: expert_mode` (matching `charge_scaling10`):

| name | type | default | min | max | step |
|---|---|---|---|---|---|
| `pv_metric90_weight` | input_number | **0** initially, 0.1 only at stage C | 0 | 1.0 | 0.01 |
| `load_scaling90` | input_number | 0.9 | 0 | 2.0 | 0.01 |

Both read in `fetch_config_options()` (fetch.py:2352-2355) alongside the existing pv10 settings.

`pv_metric90_weight` ships at 0 so the feature is inert until deliberately enabled; the 0.1 the
target design calls for is applied only if stage C goes ahead. See Validation.

### 4. Metric blend

`compute_metric` gains three optional keyword arguments so the ten non-planner call sites in
`annual.py`, `output.py`, `compare.py` and the tests need no change:

```python
def compute_metric(self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10,
                   battery_cycle, metric_keep, final_carbon_g, import_kwh_battery,
                   import_kwh_house, export_kwh,
                   soc90=None, cost90=None, final_iboost90=0.0):
```

`cost90 is not None` is the single switch that activates the pv90 term; `soc90` and
`final_iboost90` are only read when it is set. `battery_value90` is computed from them exactly as
`battery_value10` is, and `metric90 = cost90 - battery_value90`, mirroring `metric10`. The blend:

```python
w10 = self.pv_metric10_weight
w90 = self.pv_metric90_weight if cost90 is not None else 0.0
if w10 + w90 > 1.0:                    # keep the nominal weight non-negative
    w10, w90 = w10 / (w10 + w90), w90 / (w10 + w90)
metric = (1.0 - w10 - w90) * metric + w10 * metric10 + w90 * metric90
```

The carbon, self-sufficiency, battery-cycle and `metric_keep` terms continue to be applied after
the blend using the nominal-case values only, unchanged.

This drops the `if metric10 > metric` clamp. At `w90 = 0` results match today wherever
`metric10 >= metric` — the overwhelmingly common case, since pv10 is both lower PV and higher
load — and differ only where pv10 was *better* than nominal.

The callers in `annual.py`, `compare.py` and `output.py` that pass the mid-case values for both
arguments (`cost10 = cost`, `soc10 = soc`) are unaffected in every case: `metric10 == metric`
makes the blend an identity regardless of the weights.

### 5. Optimiser wiring

Everywhere a pv10 simulation is launched, a pv90 simulation is launched too, but **only when
`pv_metric90_weight > 0`**:

- `optimise_charge_limit` — a `result90` dict alongside `resultmid` and `result10`
- `optimise_export` — the same
- the two remaining `compute_metric` sites at plan.py:520 and plan.py:882

When the weight is 0 the third simulation is skipped entirely, so users who leave the feature off
pay no simulation cost. They still pay for building the p90 step arrays and carrying them in the
kernel context (see section 6), which is a fixed per-plan cost rather than a per-candidate one.

### 6. Kernel: pv90 is a first-class scenario

pv90 gets its own arrays in the existing `PkContext`. It is explicitly **not** implemented by
building a second context with the p90 series smuggled into the `pv10` slots.

The reason is the pool model. `plan.py:1173` creates `multiprocessing.Pool(processes=cpu_count())`
— a *process* pool — and workers reconstruct their `Prediction` from
`PRED_GLOBAL["dict"] = self.__dict__.copy()` (prediction.py:207), inheriting the parent's kernel
allocation by fork. A second context would duplicate the whole array set into every forked
worker, multiplied by `cpu_count()`. Adding two arrays to the single existing context pays that
cost once.

`prediction_kernel.py`:

- `PkContext._fields_` gains `pv90` and `load90` (`POINTER(c_double)`), placed immediately after
  `load10` — field order must match the `.cpp` exactly
- `PkScenario.pv10` becomes `pv_scenario` (`c_int32`, values 0/1/2). Same width, so this is a
  semantic change rather than a layout change
- `create_kernel_context` builds the `pv90` and `load90` arrays alongside the existing ones
- `run_prediction_kernel` passes the scenario through to `scenario.pv_scenario`
- `KERNEL_ABI_VERSION` 2 → 3 (context layout changed) and `KERNEL_PARITY_REVISION` bumped

No `battery_rate_max_scaling90` scalar is needed: pv90 uses the plain `battery_rate_max_scaling`
the kernel already holds.

`prediction_kernel.cpp`:

- `PkContext` / `PkScenario` struct fields to match
- `PkStore` (line 188) gains `pv90`, `load90` vectors, assigned in the context-copy path
  alongside the existing `pv10`/`load10` copies (lines 283-305)
- line 348 `const bool pv10 = s->pv10 != 0;` becomes a three-way scenario integer
- line 412 scaling select and lines 414-415 array select become three-way
- line 425 `io_flag` worst-case rate stays gated to the pv10 scenario only
- `PK_PARITY_REVISION` bumped to match

The p90 step arrays are built unconditionally so the context is always valid, even when
`pv_metric90_weight` is 0. That is one extra `step_data_history` pass per plan, negligible against
the thousands of simulations. Only the *simulations* are skipped when the weight is 0.

Binaries: `bash apps/predbat/build_kernel.sh` locally before running tests, or the loader reports
a stale binary and silently falls back to the Python engine. CI covers the rest — the
`kernel-binaries` job in `.github/workflows/code-quality.yml` cross-builds all six platform `.so`
files with zig and auto-commits them back to the PR branch, and the test job runs with
`PREDBAT_KERNEL_REQUIRED=1` so a stale binary fails rather than falling back.

`sim_hash` already mixes `hash(pv10)` (prediction.py:407); with an integer the three scenarios
hash distinctly, so the prediction cache stays correct.

## Testing

New `tests/test_pv90.py`, registered in `TEST_REGISTRY` in `unit_test.py`:

- the p50 fallback returns the nominal series unchanged when no `forecast90` is present, and
  `pv_forecast_minute90` is always populated so downstream step-array building cannot KeyError
- `fetch_pv_forecast()` reads `forecast90` when present and derives it when absent
- both config items exist, are expert-gated, and carry the specified defaults
- `compute_metric` blend: weights sum correctly, renormalisation when `w10 + w90 > 1`,
  `cost90=None` back-compatibility, and that `w90 = 0` with `metric10 > metric` reproduces the
  pre-change value
- the scenario selector picks the right PV and load arrays, and applies the charge de-rate for
  pv10 only

`tests/test_kernel_parity.py` gains pv90 coverage. The kernel change is now an ABI change, so
parity matters more than for a pure-Python feature:

- all three scenarios (nominal, pv10, pv90) must match between the kernel and the Python engine
- pv90 must use the undelated charge rate and must *not* apply the `io_flag` worst-case import
  rate, distinguishing it from pv10 in a case where a copy-paste of the pv10 branch would pass a
  naive equality check
- the `KERNEL_ABI_VERSION` / `KERNEL_PARITY_REVISION` guard rejects a stale binary

Run `bash apps/predbat/build_kernel.sh` before the suite; otherwise the loader falls back to the
Python engine and the parity tests pass vacuously.

## Validation

Ordered so that the experiment happens before any reference files are touched. The experiment
sets the weight at runtime, so if the feature is dropped there is nothing to revert.

**Stage A** — land with `pv_metric90_weight` default **0**. Run `./run_all`. Any diff is
attributable purely to the clamp removal; review and accept or adjust.

**Stage B** — the experiment. Replay `coverage/predbat_no_export_100.txt` with the weight set at
runtime to 0, 0.05, 0.1 and 0.2, reporting the chosen charge limits and the plan duration at each.
Decide whether the feature is worth keeping.

The dump predates `forecast90`, so pv90 falls back to p50 and this measures the **load-only**
upside: 10% lower load, identical PV. That makes Stage B a lower bound. If it moves the plan off
100% on load alone, real p90 data can only push harder in the same direction. If it does *not*
move the plan, the result is inconclusive rather than negative, and the next step is to re-run
against a dump carrying real Solcast p90 before drawing a conclusion.

Also worth recording at each weight: whether the plan moves to the bottom of the 66-100% plateau
or somewhere in between. Landing at 66% would mean pv90 is merely overpowering the pv10 hedge and
letting the plateau's lower tiebreak win, rather than genuinely pricing the spill risk — a
different and weaker outcome than landing at an interior value.

**Stage C** — only if the feature is kept, and a separate decision: flip the
`pv_metric90_weight` default to 0.1 and regenerate the `debug_cases` `.expected.json` references
with manual review.

Because both config items are expert-gated, shipping permanently at default 0 is a viable end
state: opt-in, no reference regeneration, and no change for existing users.

## Out of scope

- Correcting the entry-cost / exit-value error in the residual battery valuation. It is a real
  defect (~15.3% over-valuation) but is independent of this change, does not fix this scenario on
  its own, and would shift every plan. Tracked separately.
- Changing the `metric -= 0.002` bias towards `soc_max` in `optimise_charge_limit`.
- Plumbing p90 into `solar_model.py` or Open-Meteo; both fall back to p50, so for those sources
  pv90 is a load-only scenario until real p90 data is added.
