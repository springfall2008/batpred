# Phase B: Batch Prediction Fan-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `launch_run_prediction_*` queue work into a lazily-flushed batch that reaches the C++ kernel through one `pk_run_batch` call per fan-out, so the per-call Python overhead is paid once per batch and the kernel's threading can finally be used.

**Architecture:** The four `launch_run_prediction_*` call sites in `plan.py` are already launch-all-then-collect-all, so they do not change shape. `launch_*` stops running anything: it prepares the trial inputs, appends a `BatchJob` to a pending list on the `Prediction`, and returns a `BatchHandle`. The first `BatchHandle.get()` flushes the whole pending batch — prediction-cache hits are resolved in Python, the misses are marshalled into one `PkBatchJob` array and run by a single `pk_run_batch` call (optionally multi-threaded inside C++), and each job's result is then shaped into exactly what its `thread_run_prediction_*` used to return. Anything the kernel cannot take falls back to the existing Python path, per job.

**Tech Stack:** Python 3 (ctypes), C++ prediction kernel (`prediction_kernel.cpp`, ABI 3, parity revision 5, already built and committed), predbat's own `unit_test.py` harness.

**Spec:** [PHASE_B_BATCH_REFACTOR.md](../../../PHASE_B_BATCH_REFACTOR.md) — the working brief this plan implements. Read it first; it carries the measurements, the traps and the dead ends that justify the design below.

## Global Constraints

- **Branch:** `perf/kernel-batch-abi` (4 commits, unpushed, based on `main`). All work continues on this branch.
- **No C++ changes.** The kernel side is done, verified and committed. If you find yourself editing `prediction_kernel.cpp`, stop — that means the design has drifted. (The one exception would be a genuine kernel bug, which then needs `PK_PARITY_REVISION` **and** `KERNEL_PARITY_REVISION` bumped and all 6 shipped binaries rebuilt.)
- **Byte-identical plans are the gate.** Every task that can change results must end with the 20-scenario comparison showing `20 unchanged, +0.0000` on metric, cost and all three PV futures.
- **Line length:** 256 chars (Black), 250 (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every function, method and class, including `__init__`.
- **Spelling:** British English (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit — re-stage after running pre-commit.
- **Naming:** `lower_case_with_underscores`.
- **Unit tests are mandatory for all new code** (CLAUDE.md), registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`.
- **Test output goes to a file, then you grep the file.** Never pipe a test run straight into grep — a wrong pattern means running it all again.
- **`cd` does not persist between Bash tool calls.** Every command below is written to be pasted whole.
- **Do not `git checkout` between branches with uncommitted work** — it carries the changes across. Commit first.

## Invariant this design depends on

**No caller may mutate a list or window dict it has passed to `launch_run_prediction_*` between that call and the matching `.get()`.**

Today every launch either runs immediately (serial path) or pickles its arguments immediately (pool path), so this invariant did not exist. Batching defers the read to flush time, so it does now. All four call sites were read and already satisfy it, *after* Task 1 removes the one violation:

| call site | fan-out | why it is safe |
|---|---|---|
| `plan.py:528-530` (`optimise_levels`) | `launch_run_prediction_single` ×3 | `try_charge_limit`/`try_export` are freshly materialised per scenario and never written again |
| `plan.py:1781-1799` (`optimise_charge_limit`) | `launch_run_prediction_charge_min_max` ×4-6 | `charge_limit`/`export_limits` are read-only across the block |
| `plan.py:1950-1955` (`optimise_charge_limit`) | `launch_run_prediction_charge` ×2-3 per SoC | `try_charge_limit` is a separate list, only written in the results loop *after* every `.get()` |
| `plan.py:2183-2186` (`optimise_export`) | `launch_run_prediction_export` ×2-3 per option | **violates it today** via `export_window[window_n]["start"] = start` — Task 1 fixes this |

The 20-scenario byte-identical gate is what enforces the invariant in practice: any violation shows up as a changed plan immediately.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/predbat/prediction_kernel.py` | ctypes/ABI layer only. Gains `BatchJob` (the queued-job record), `run_prediction_kernel_batch()` and `kernel_result_tuple()`; `pk_run_batch`'s argtypes get their missing `n_threads`. | Modify |
| `apps/predbat/prediction_batch.py` | **New.** `PredictionBatch` mixin (pending list, enqueue, flush, per-job Python fallback), `BatchHandle`, `prediction_cache_key()`. Imports `prediction_kernel` and `const` only — never `prediction`, so there is no import cycle. | Create |
| `apps/predbat/prediction.py` | `Prediction` gains the `PredictionBatch` mixin, the `_prepare_*` input builders shared by the direct and batch paths, `scan_soc_range()`, and the four `queue_run_prediction_*` entry points. | Modify |
| `apps/predbat/plan.py` | The four `launch_run_prediction_*` become one-line delegations to `queue_run_prediction_*`; the process pool goes away. | Modify |
| `apps/predbat/tests/test_kernel_parity.py` | Gains `run_batch_parity_tests()` — `pk_run_batch` against a loop of `pk_run`, at several thread counts. | Modify |
| `apps/predbat/tests/test_prediction_batch.py` | **New.** The batch runner's behaviour: equivalence with the direct path for all four kinds, laziness, dedup, cache hits, and both fallback routes. | Create |
| `apps/predbat/unit_test.py` | Register `prediction_batch` in `TEST_REGISTRY`. | Modify |
| `docs/apps-yaml.md` | `threads` changes meaning: kernel threads, not worker processes. | Modify |

---

### Task 1: Stop the export trial mutating the caller's window

**Files:**
- Modify: `apps/predbat/prediction.py:358-373` (`thread_run_prediction_export`)
- Test: `apps/predbat/tests/test_prediction_batch.py` (created here, one test; the rest of the file lands in Task 4)
- Modify: `apps/predbat/unit_test.py` (register the new test)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `thread_run_prediction_export` no longer writes to `export_window[window_n]["start"]`. Task 3 splits this same function, and Task 4 relies on the window list being safe to hold across a deferred flush.

This is the fix from `perf/threadpool-prototype` (`263f8178`, marked do-not-merge), ported across. It was only ever safe because each pool worker mutated its own unpickled copy.

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_prediction_batch.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the batched prediction fan-out.

launch_run_prediction_* no longer runs anything: it queues a job on the Prediction and returns a
handle whose first get() flushes the whole batch through one pk_run_batch call. These tests pin the
two things that makes conditional - that a queued job returns exactly what the direct
thread_run_prediction_* path returns, and that a job the kernel cannot take still runs.
"""

import random

from prediction import Prediction


def make_export_windows(minutes_now):
    """Build a small deterministic export window layout for the trial-input tests"""
    return [
        {"start": minutes_now + 60, "end": minutes_now + 120, "average": 15.0},
        {"start": minutes_now + 180, "end": minutes_now + 240, "average": 20.0},
    ]


def test_export_trial_does_not_mutate_caller_window(my_predbat):
    """The export trial must build its own window list, returns True on failure.

    It used to write the trial start straight into the caller's window dict, which was only safe
    because each pool worker mutated its own unpickled copy. A batched fan-out holds one shared list
    across every job in the batch, so an in-place write would corrupt every other trial of the same
    window.
    """
    print("**** Running export trial input tests ****")
    failed = False
    minutes_now = my_predbat.minutes_now
    export_window = make_export_windows(minutes_now)
    export_limits = [100.0, 100.0]
    original = [dict(window) for window in export_window]

    prediction = Prediction(my_predbat, {}, {}, {}, {})
    trial_window, trial_limits = prediction._prepare_export(5.0, minutes_now + 90, 0, export_window, export_limits, None)

    if export_window != original:
        print("ERROR: _prepare_export mutated the caller's export window: {} vs {}".format(export_window, original))
        failed = True
    if export_limits != [100.0, 100.0]:
        print("ERROR: _prepare_export mutated the caller's export limits: {}".format(export_limits))
        failed = True
    if trial_window[0]["start"] != minutes_now + 90:
        print("ERROR: trial window start not applied, got {}".format(trial_window[0]["start"]))
        failed = True
    if trial_window[1] is not export_window[1]:
        print("ERROR: untouched windows should be shared with the caller's list, not copied")
        failed = True
    if trial_limits[0] != 5.0:
        print("ERROR: trial export limit not applied, got {}".format(trial_limits[0]))
        failed = True

    # The trial start is clamped to at least 5 minutes before the window end
    trial_window, _ = prediction._prepare_export(5.0, minutes_now + 200, 0, export_window, export_limits, None)
    if trial_window[0]["start"] != minutes_now + 115:
        print("ERROR: trial window start not clamped to end-5, got {}".format(trial_window[0]["start"]))
        failed = True

    if not failed:
        print("Export trial input tests passed")
    return failed


def run_prediction_batch_tests(my_predbat):
    """Run every batched prediction test, returns True on failure"""
    failed = test_export_trial_does_not_mutate_caller_window(my_predbat)
    if failed:
        print("**** Prediction batch tests FAILED ****")
    else:
        print("**** Prediction batch tests passed ****")
    return failed
```

The test is written against `_prepare_export`, which does not exist yet — Step 4 of this task creates it, because extracting the helper *is* the fix: it is the only way to build the trial window without touching the caller's. Task 3 later joins the other three `thread_run_prediction_*` to the same pattern.

- [ ] **Step 2: Register the test**

In `apps/predbat/unit_test.py`, add the import next to the other test imports (they are alphabetical-ish, grouped by module):

```python
from tests.test_prediction_batch import run_prediction_batch_tests
```

and add the registry entry to `TEST_REGISTRY`, next to the other prediction entries:

```python
        ("prediction_batch", run_prediction_batch_tests, "Batched prediction fan-out tests", False),
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test prediction_batch > /tmp/predbat-task1.log 2>&1; grep -nE "ERROR|FAIL|PASS|Traceback|AttributeError" /tmp/predbat-task1.log | head -20
```

Expected: `AttributeError: 'Prediction' object has no attribute '_prepare_export'`.

- [ ] **Step 4: Add `_prepare_export` and route the direct path through it**

In `apps/predbat/prediction.py`, add the helper immediately above `thread_run_prediction_export`:

```python
    def _prepare_export(self, this_export_limit, start, window_n, export_window, export_limits, all_n):
        """Build the trial export limits and window list - shared by thread_run_prediction_export and the batch path.

        The trial start is applied to a private copy of the window rather than written into the
        caller's list: with a process pool each worker mutated its own unpickled copy, but a batched
        fan-out shares one list across every job in the batch, so an in-place write would corrupt the
        other trials of the same window. Only ["end"] is ever read back by the caller
        (optimise_export), so nothing depends on the write being visible.
        """
        export_limits = export_limits.copy()

        if all_n:
            for window_id in all_n:
                export_limits[window_id] = this_export_limit
        else:
            export_limits[window_n] = this_export_limit
            # Adjust start
            window = export_window[window_n]
            start = min(start, window["end"] - 5)
            export_window = list(export_window)
            export_window[window_n] = dict(window, start=start)

        return export_window, export_limits
```

and replace the head of `thread_run_prediction_export` (everything from `# Store try value into the window` down to the line before the `(` of the result unpack) with:

```python
        export_window, export_limits = self._prepare_export(this_export_limit, start, window_n, export_window, export_limits, all_n)
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test prediction_batch > /tmp/predbat-task1.log 2>&1; grep -nE "ERROR|FAIL|passed|Traceback" /tmp/predbat-task1.log | head -20
```

Expected: `Export trial input tests passed`, no ERROR lines.

- [ ] **Step 6: Verify plans are byte-identical**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-task1-run.log 2>&1; python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json > /tmp/predbat-task1-cmp.log 2>&1; tail -30 /tmp/predbat-task1-cmp.log
```

Expected: 20 scenarios unchanged, `+0.0000` on metric, cost, and all three PV futures. Anything else means the mutation *was* being depended on — stop and investigate before continuing.

- [ ] **Step 7: Run the full suite**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all > /tmp/predbat-task1-all.log 2>&1; grep -nE "FAILED|ERROR|Traceback|All tests passed|tests passed" /tmp/predbat-task1-all.log | tail -20
```

- [ ] **Step 8: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit && git add apps/predbat/prediction.py apps/predbat/tests/test_prediction_batch.py apps/predbat/unit_test.py && git commit -m "fix(prediction): build the export trial window privately instead of mutating the caller's

The trial start was written straight into the caller's window dict, which was only safe because each
pool worker mutated its own unpickled copy. A batched fan-out shares one window list across every job
in the batch, so this has to be local. Ported from perf/threadpool-prototype.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

(If pre-commit rewrites the cspell dictionary, `git add` it and re-run the commit.)

---

### Task 2: Batch entry point in the ctypes layer

**Files:**
- Modify: `apps/predbat/prediction_kernel.py:287-292` (argtypes), `:596-629` (result assembly)
- Test: `apps/predbat/tests/test_kernel_parity.py`

**Interfaces:**
- Consumes: `PkBatchJob`, `PkBatchResult`, `KERNEL_HAS_BATCH` (already present on this branch).
- Produces:
  - `class BatchJob` — the queued-job record, with `__slots__` `("charge_limit", "charge_window", "export_window", "export_limits", "pv_scenario", "end_record", "step", "soc_range_start_step", "soc_range_end_step", "sim_hash", "want_range", "range_window", "result")`.
  - `kernel_result_tuple(pred, result, predict_soc) -> tuple` — the 17-field `run_prediction()` result tuple built from a `PkResult`.
  - `run_prediction_kernel_batch(pred, jobs, n_threads=1) -> list | None` — returns one `(result_tuple_or_None, soc_range_min, soc_range_max)` per job in job order, or `None` if the whole batch could not run.

**The bug this task fixes first:** `pk_run_batch` is declared in C++ as
`int32_t pk_run_batch(int64_t handle, const PkBatchJob *jobs, int32_t n_jobs, PkBatchResult *results, int32_t n_threads)`
but `prediction_kernel.py:289` gives it only four `argtypes`. Nothing calls it yet so nothing has broken, but a call built against that binding hands the C++ side an undefined `n_threads`.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_kernel_parity.py`:

```python
def build_batch_jobs(my_predbat, rng, count):
    """Build a list of BatchJob scenarios plus the Prediction they run against"""
    apply_random_scenario(my_predbat, rng)
    pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, rng)
    my_predbat.prediction_kernel_enable = True
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
    prediction.kernel_handle = create_kernel_context(prediction)

    charge_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, rng.randint(2, 5))
    export_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, rng.randint(2, 5))
    end_record = my_predbat.forecast_minutes

    jobs = []
    for index in range(count):
        charge_limit = [round(rng.uniform(0, my_predbat.soc_max), 2) for _ in charge_window]
        export_limits = [rng.choice([100.0, 99.0, 0.0, round(rng.uniform(1, 99), 1)]) for _ in export_window]
        pv_scenario = rng.choice([PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90])
        # Every third job asks for the SoC range over a charge window, as the min/max fan-out does
        range_window = charge_window[index % len(charge_window)] if (index % 3) == 0 else None
        start_step = -1
        end_step = -1
        if range_window is not None:
            start_step = max(int((range_window["start"] - my_predbat.minutes_now) / 5) * 5, 0) // 5
            end_step = int((range_window["end"] - my_predbat.minutes_now) / 5) * 5 // 5
        jobs.append(
            prediction_kernel.BatchJob(
                charge_limit,
                charge_window,
                export_window,
                export_limits,
                pv_scenario,
                end_record,
                5,
                soc_range_start_step=start_step,
                soc_range_end_step=end_step,
                want_range=range_window is not None,
                range_window=range_window,
            )
        )
    return prediction, jobs


def run_batch_parity_tests(my_predbat, count=60):
    """Check pk_run_batch matches a loop of pk_run exactly, at every thread count, returns True on failure.

    The batch path is the only one Python will use after this refactor, and it is the only one that
    can run scenarios concurrently, so it is checked against the single-scenario path it replaces
    rather than against the Python engine (which run_random_sweep_tests already pins pk_run against).
    Results must be bit-identical, not merely close: the whole point of the fan-out is that plans do
    not change.
    """
    print("**** Running kernel batch parity tests ****")
    if not prediction_kernel.KERNEL_HAS_BATCH:
        print("SKIP: kernel does not expose pk_run_batch")
        return False

    failed = False
    prediction, jobs = build_batch_jobs(my_predbat, random.Random(4321), count)
    if not prediction.kernel_handle:
        print("ERROR: batch parity kernel context creation failed")
        return True

    # Reference: one pk_run per scenario, with the SoC series materialised so the range can be
    # scanned in Python exactly as thread_run_prediction_charge_min_max does
    reference = []
    for job in jobs:
        single = run_prediction_kernel(prediction, job.charge_limit, job.charge_window, job.export_window, job.export_limits, job.pv_scenario, job.end_record, 5, False)
        if single is None:
            print("ERROR: reference pk_run failed")
            return True
        if job.range_window is None:
            soc_range = (prediction.soc_max, 0)
        else:
            soc_range = prediction.scan_soc_range(single[11], job.range_window)
        reference.append((single, soc_range))

    for n_threads in (1, 2, 4, 8):
        batched = prediction_kernel.run_prediction_kernel_batch(prediction, jobs, n_threads)
        if batched is None:
            print("ERROR: pk_run_batch failed at {} threads".format(n_threads))
            return True
        if len(batched) != len(jobs):
            print("ERROR: pk_run_batch returned {} results for {} jobs".format(len(batched), len(jobs)))
            return True
        for index, (result_tuple, soc_range_min, soc_range_max) in enumerate(batched):
            single, (expect_min, expect_max) = reference[index]
            if result_tuple is None:
                print("ERROR: job {} reported a non-zero status at {} threads".format(index, n_threads))
                failed = True
                continue
            for field, name in enumerate(RESULT_NAMES):
                if result_tuple[field] != single[field]:
                    print("ERROR: job {} at {} threads differs on {}: batch {} single {}".format(index, n_threads, name, result_tuple[field], single[field]))
                    failed = True
            # The batch never materialises the SoC series - that is what makes it affordable
            if result_tuple[11] != {}:
                print("ERROR: job {} returned a SoC series from the batch path".format(index))
                failed = True
            for field, name in [(12, "car_charging_soc_next"), (13, "iboost_next"), (14, "iboost_running"), (15, "iboost_running_solar"), (16, "iboost_running_full")]:
                if result_tuple[field] != single[field]:
                    print("ERROR: job {} at {} threads differs on {}: batch {} single {}".format(index, n_threads, name, result_tuple[field], single[field]))
                    failed = True
            if soc_range_min != expect_min or soc_range_max != expect_max:
                print("ERROR: job {} at {} threads SoC range {} {} expected {} {}".format(index, n_threads, soc_range_min, soc_range_max, expect_min, expect_max))
                failed = True

    if not failed:
        print("Batch parity: {} scenarios bit-identical to pk_run at 1, 2, 4 and 8 threads".format(count))
    return failed
```

and call it from `run_kernel_parity_tests`, after the clipping sweep:

```python
        if not failed:
            failed |= run_batch_parity_tests(my_predbat)
```

The test uses `prediction.scan_soc_range`, which Task 3 creates. Add it in this task as part of Step 4 (Task 3 then routes `thread_run_prediction_charge_min_max` through it).

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test kernel_parity > /tmp/predbat-task2.log 2>&1; grep -nE "ERROR|FAIL|Traceback|AttributeError|passed" /tmp/predbat-task2.log | head -20
```

Expected: `AttributeError: module 'prediction_kernel' has no attribute 'BatchJob'`.

- [ ] **Step 3: Fix the `pk_run_batch` binding**

In `apps/predbat/prediction_kernel.py`, replace the argtypes line inside the `try:` at line 288-289:

```python
                lib.pk_run_batch.restype = ctypes.c_int32
                # The trailing c_int32 is n_threads - pk_run_batch takes five arguments, and ctypes
                # will happily pass a fourth-and-a-bit if the binding says otherwise
                lib.pk_run_batch.argtypes = [ctypes.c_int64, ctypes.POINTER(PkBatchJob), ctypes.c_int32, ctypes.POINTER(PkBatchResult), ctypes.c_int32]
```

- [ ] **Step 4: Add `BatchJob`, `kernel_result_tuple`, `run_prediction_kernel_batch` and `scan_soc_range`**

In `apps/predbat/prediction_kernel.py`, add `BatchJob` after the `PkBatchResult` class:

```python
class BatchJob:
    """One queued prediction: its trial inputs, how the result should be shaped, and the result itself.

    run_prediction_kernel_batch reads only the input fields and the SoC range steps. sim_hash,
    want_range, range_window and result belong to the batch runner in prediction_batch.py, which is
    the only thing that creates these; they live here so the ctypes layer can be tested on its own.
    """

    __slots__ = ("charge_limit", "charge_window", "export_window", "export_limits", "pv_scenario", "end_record", "step", "soc_range_start_step", "soc_range_end_step", "sim_hash", "want_range", "range_window", "result")

    def __init__(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, soc_range_start_step=-1, soc_range_end_step=-1, sim_hash=None, want_range=False, range_window=None):
        """Record one job's trial inputs; result stays None until the batch is flushed"""
        self.charge_limit = charge_limit
        self.charge_window = charge_window
        self.export_window = export_window
        self.export_limits = export_limits
        self.pv_scenario = pv_scenario
        self.end_record = end_record
        self.step = step
        self.soc_range_start_step = soc_range_start_step
        self.soc_range_end_step = soc_range_end_step
        self.sim_hash = sim_hash
        self.want_range = want_range
        self.range_window = range_window
        self.result = None
```

Then factor the result assembly out of `run_prediction_kernel` so the single and batch paths cannot drift. Add:

```python
def kernel_result_tuple(pred, result, predict_soc):
    """Assemble the 17-field Prediction.run_prediction() result tuple from a PkResult.

    Shared by the single-scenario and batch paths - mirrors prediction.py:626-628, 1266-1284.
    """
    car_charging_soc_next = pred.car_charging_soc_next[:]
    if result.car_soc_next_valid:
        for car_n in range(pred.num_cars):
            car_charging_soc_next[car_n] = result.car_soc_next[car_n]

    iboost_next = result.iboost_next if pred.iboost_enable else pred.iboost_next

    return (
        round(result.final_metric, 4),
        round(result.import_kwh_battery, 4),
        round(result.import_kwh_house, 4),
        round(result.export_kwh, 4),
        round(result.soc_min, 4),
        round(result.final_soc, 4),
        result.soc_min_minute,
        round(result.battery_cycle, 4),
        round(result.metric_keep, 4),
        round(result.final_iboost, 4),
        round(result.final_carbon_g, 4),
        predict_soc,
        car_charging_soc_next,
        iboost_next,
        bool(result.iboost_running),
        bool(result.iboost_running_solar),
        bool(result.iboost_running_full),
    )


def reset_kernel_run_state(pred):
    """Clear the per-run state attributes exactly as the Python engine does - prediction.py:414-422

    Non-save runs never populate these, so they simply stay empty/False; a kernel run has to leave
    the Prediction in the same state a Python run would.
    """
    pred.predict_soc_best = {}
    pred.predict_metric_best = {}
    pred.predict_iboost_best = {}
    pred.predict_carbon_best = {}
    pred.predict_clipped_best = {}
    pred.iboost_running = False
    pred.iboost_running_solar = False
    pred.iboost_running_full = False


def run_prediction_kernel_batch(pred, jobs, n_threads=1):
    """Run a whole fan-out of prediction scenarios through the kernel in one pk_run_batch call.

    Returns a list of (result_tuple, soc_range_min, soc_range_max) in job order, where result_tuple
    is the same 17-field tuple Prediction.run_prediction() returns, or None for a job the kernel
    refused - the caller re-runs those through the Python engine. Returns None when the batch could
    not be run at all (no kernel, no batch entry point, or a non-zero return code).

    Whether the kernel is allowed to answer at all is the caller's decision, not this function's:
    kernel_supported() also refuses debug and saving runs, and flush_batch checks it before getting
    here.

    Batched jobs never materialise the per-minute SoC series: their callers all discard it, and one
    buffer per job would be ~84MB on a large batch. The one thing a caller does want from it - the
    SoC range across a charge window - the kernel tracks inline over
    [soc_range_start_step, soc_range_end_step] instead.
    """
    lib = KERNEL_LIB
    if not lib or not KERNEL_HAS_BATCH or not getattr(pred, "kernel_handle", 0):
        return None

    n_jobs = len(jobs)
    job_array = (PkBatchJob * n_jobs)()
    result_array = (PkBatchResult * n_jobs)()
    # Every ctypes buffer behind a pointer field has to outlive the call. Structures reached through
    # an array index do not keep their own _objects, so the buffers are held here rather than relying
    # on ctypes' keepalive doing the right thing through the array.
    buffers = []
    # A fan-out reuses the same window lists across most of its jobs, so their start/end arrays are
    # marshalled once per distinct list instead of once per job - that is the bulk of the batching
    # win. Keyed on identity, with the list retained so an id() cannot be recycled mid-batch. This
    # relies on no caller mutating a window list between enqueue and flush (see prediction_batch.py).
    window_cache = {}

    def window_arrays(windows):
        """Marshal a window list's start/end arrays, reusing an earlier job's arrays where possible"""
        entry = window_cache.get(id(windows))
        if entry is None:
            entry = (int32_array([window["start"] for window in windows]), int32_array([window["end"] for window in windows]), windows)
            window_cache[id(windows)] = entry
        return entry

    for index, job in enumerate(jobs):
        charge_start, charge_end, _ = window_arrays(job.charge_window)
        export_start, export_end, _ = window_arrays(job.export_window)
        charge_limit = double_array(job.charge_limit)
        export_limits = double_array(job.export_limits)
        buffers.append((charge_limit, export_limits))

        pk_job = job_array[index]
        pk_job.charge_limit = charge_limit
        pk_job.charge_start = charge_start
        pk_job.charge_end = charge_end
        pk_job.export_limits = export_limits
        pk_job.export_start = export_start
        pk_job.export_end = export_end
        pk_job.soc_out = None
        pk_job.n_charge = len(job.charge_window)
        pk_job.n_export = len(job.export_window)
        pk_job.pv_scenario = int(job.pv_scenario)
        pk_job.end_record = job.end_record
        pk_job.step = PREDICT_STEP  # the caller's step is ignored - see run_prediction_kernel
        pk_job.soc_range_start_step = job.soc_range_start_step
        pk_job.soc_range_end_step = job.soc_range_end_step

    return_code = lib.pk_run_batch(pred.kernel_handle, job_array, n_jobs, result_array, max(int(n_threads), 1))
    if return_code != 0:
        return None

    reset_kernel_run_state(pred)

    results = []
    for index in range(n_jobs):
        batch_result = result_array[index]
        if batch_result.status != 0:
            results.append((None, 0.0, 0.0))
            continue
        results.append((kernel_result_tuple(pred, batch_result.result, {}), batch_result.soc_range_min, batch_result.soc_range_max))
    return results
```

Then rewrite the tail of `run_prediction_kernel` (from the `# Reset the per-run state attributes` comment to the end) to use the shared helpers:

```python
    reset_kernel_run_state(pred)

    # Assemble the same return value as the Python engine
    predict_soc = {}
    if not cache:
        # Indexed loop, not dict(zip(range(...), soc_out)): iterating a ctypes array boxes each double
        # through the sequence protocol and measured 34% slower than subscripting it.
        for k in range(n_steps):
            predict_soc[k * PREDICT_STEP] = soc_out[k]

    return kernel_result_tuple(pred, result, predict_soc)
```

Finally, add `scan_soc_range` to `Prediction` in `apps/predbat/prediction.py`, immediately above `thread_run_prediction_charge_min_max`:

```python
    def scan_soc_range(self, predict_soc, window):
        """Return the (min, max) SoC across a charge window - shared by the direct and batch min/max paths.

        The kernel computes the same range inline (see PkBatchJob.soc_range_start_step), so this is
        only reached when a job falls back to the Python engine; the two must agree exactly, including
        the clamping that collapses an empty range to a single value rather than leaving min above max.
        """
        min_soc = self.soc_max
        max_soc = 0
        predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
        predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
        for minute in range(predict_minute_start, predict_minute_end + 5, 5):
            if minute in predict_soc:
                min_soc = min(predict_soc[minute], min_soc)
                max_soc = max(predict_soc[minute], max_soc)
        max_soc = max(max_soc, min_soc)
        min_soc = min(min_soc, max_soc)
        return min_soc, max_soc
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test kernel_parity > /tmp/predbat-task2.log 2>&1; grep -nE "ERROR|FAIL|bit-identical|passed|Traceback" /tmp/predbat-task2.log | head -20
```

Expected: `Batch parity: 60 scenarios bit-identical to pk_run at 1, 2, 4 and 8 threads`, and the parity suite passing.

- [ ] **Step 6: Verify the single-scenario path still matches (the refactor above touched it)**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test model_kernel --test kernel_parity > /tmp/predbat-task2-model.log 2>&1; grep -nE "ERROR|FAILED|passed" /tmp/predbat-task2-model.log | tail -20
```

- [ ] **Step 7: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit && git add apps/predbat/prediction_kernel.py apps/predbat/prediction.py apps/predbat/tests/test_kernel_parity.py && git commit -m "feat(kernel): marshal a whole fan-out into one pk_run_batch call

Adds BatchJob, run_prediction_kernel_batch and the shared result assembly, and gives pk_run_batch's
ctypes binding the n_threads argument it was missing. Window start/end arrays are marshalled once per
distinct window list rather than once per job, which is where the batching win comes from.

Bit-identical to a loop of pk_run across 60 scenarios at 1, 2, 4 and 8 threads.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Split the trial-input building out of `thread_run_prediction_*`

**Files:**
- Modify: `apps/predbat/prediction.py:227-394` (all four `thread_run_prediction_*`)

**Interfaces:**
- Consumes: `_prepare_export` (Task 1), `scan_soc_range` (Task 2).
- Produces:
  - `_prepare_single(charge_limit, export_limits) -> (list, list)`
  - `_prepare_charge(try_soc, window_n, charge_limit, all_n) -> list`
  - `_prepare_export(...)` — already exists from Task 1.
  Task 4's `queue_run_prediction_*` call exactly these, so the direct and batch paths cannot drift.

This step must be a **no-op**. It exists so the batch path never gets a second copy of the input building.

- [ ] **Step 1: Add the two remaining prepare helpers**

In `apps/predbat/prediction.py`, above `thread_run_prediction_single`:

```python
    def _prepare_single(self, charge_limit, export_limits):
        """Copy the caller's limit lists for a single-scenario trial - shared by thread_run_prediction_single and the batch path.

        The copy used to live in Plan.launch_run_prediction_single, where it protected the caller's
        lists from the pool pickling them; the batch path defers the read to flush time, which needs
        the same protection, so it belongs with the other trial-input building.
        """
        return list(charge_limit), list(export_limits)

    def _prepare_charge(self, try_soc, window_n, charge_limit, all_n):
        """Build the trial charge limits - shared by thread_run_prediction_charge/_charge_min_max and the batch path"""
        try_charge_limit = charge_limit.copy()
        if all_n:
            for set_n in all_n:
                try_charge_limit[set_n] = try_soc
        else:
            try_charge_limit[window_n] = try_soc
        return try_charge_limit
```

- [ ] **Step 2: Route the four thread functions through them**

`thread_run_prediction_single` — insert as the first statement of the body:

```python
        charge_limit, export_limits = self._prepare_single(charge_limit, export_limits)
```

`thread_run_prediction_charge` and `thread_run_prediction_charge_min_max` — replace the five-line `try_charge_limit = charge_limit.copy()` block in each with:

```python
        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)
```

`thread_run_prediction_charge_min_max` — replace the scan block (`min_soc = self.soc_max` through `min_soc = min(min_soc, max_soc)`) with:

```python
        min_soc = self.soc_max
        max_soc = 0
        if not all_n:
            min_soc, max_soc = self.scan_soc_range(predict_soc, charge_window[window_n])
```

`thread_run_prediction_export` — already routed through `_prepare_export` in Task 1.

- [ ] **Step 3: Drop the now-duplicated copy in plan.py**

`Plan.launch_run_prediction_single` in `apps/predbat/plan.py:675-676` still does its own `list()` copies. Delete those two lines — `_prepare_single` now owns that.

- [ ] **Step 4: Verify the split changed nothing**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-task3-run.log 2>&1; python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json > /tmp/predbat-task3-cmp.log 2>&1; tail -30 /tmp/predbat-task3-cmp.log
```

Expected: 20 unchanged, `+0.0000` everywhere. This step alone should be a no-op — if it is not, the split is wrong.

- [ ] **Step 5: Run the full suite**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all > /tmp/predbat-task3-all.log 2>&1; grep -nE "FAILED|ERROR|Traceback|tests passed" /tmp/predbat-task3-all.log | tail -20
```

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit && git add apps/predbat/prediction.py apps/predbat/plan.py && git commit -m "refactor(prediction): split trial-input building out of thread_run_prediction_*

Each thread_run_prediction_* built the trial inputs, ran the prediction and shaped the result. The
batch path needs the first and third but not the second, so they are split rather than copied - one
definition of what a trial input is, for both paths.

No-op: 20 random scenarios byte-identical.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: The lazy batch runner, serial

**Files:**
- Create: `apps/predbat/prediction_batch.py`
- Modify: `apps/predbat/prediction.py` (mixin, `queue_run_prediction_*`, `prediction_cache_key` reuse, `__init__`)
- Modify: `apps/predbat/plan.py:671-714` (the four `launch_*`), `:27` (imports), `:1340-1365` (pool creation)
- Test: `apps/predbat/tests/test_prediction_batch.py`

**Interfaces:**
- Consumes: `BatchJob`, `run_prediction_kernel_batch` (Task 2); `_prepare_single`, `_prepare_charge`, `_prepare_export`, `scan_soc_range` (Tasks 1 and 3).
- Produces:
  - `prediction_batch.prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step) -> int`
  - `prediction_batch.BatchHandle` with `.get()` — the drop-in for `AsyncResult`/`DummyThread`.
  - `prediction_batch.PredictionBatch` mixin: `enqueue_prediction(...) -> BatchHandle`, `flush_batch()`, `run_batch_job_python(job) -> tuple`, attributes `pending_batch` (list) and `batch_threads` (int).
  - `Prediction.queue_run_prediction_single/_charge/_charge_min_max/_export` — same signatures as the matching `thread_run_prediction_*`, returning a `BatchHandle`.

`batch_threads` stays at 1 for this whole task. That isolates the marshalling win with no concurrency in play, so any divergence here is a Python bug and nothing else.

- [ ] **Step 1: Write the failing tests**

Extend `apps/predbat/tests/test_prediction_batch.py` (keep the Task 1 test, add these). Import at the top:

```python
import prediction_batch
from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10
from prediction_kernel import create_kernel_context
from tests.test_kernel_parity import apply_random_scenario, kernel_available, make_step_data, make_windows, restore_scenario_state, snapshot_scenario_state
```

```python
def make_batch_prediction(my_predbat, seed=7):
    """Build a kernel-enabled Prediction plus a charge/export window layout to fan out over"""
    rng = random.Random(seed)
    apply_random_scenario(my_predbat, rng)
    pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, rng)
    my_predbat.prediction_kernel_enable = True
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
    prediction.kernel_handle = create_kernel_context(prediction)
    charge_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, 4)
    export_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, 4)
    charge_limit = [round(my_predbat.soc_max / 2, 2)] * len(charge_window)
    export_limits = [100.0] * len(export_window)
    return prediction, charge_window, export_window, charge_limit, export_limits


def test_queued_matches_direct(my_predbat):
    """Every queued prediction must return exactly what the direct thread path returns, returns True on failure"""
    print("**** Running queued vs direct equivalence tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes
    try_soc = round(my_predbat.soc_max * 0.8, 2)

    cases = [
        ("single", lambda: prediction.thread_run_prediction_single(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, 5), lambda: prediction.queue_run_prediction_single(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, 5)),
        ("charge", lambda: prediction.thread_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, None, end_record), lambda: prediction.queue_run_prediction_charge(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, None, end_record)),
        ("charge_min_max", lambda: prediction.thread_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record), lambda: prediction.queue_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)),
        ("charge_min_max_all_n", lambda: prediction.thread_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, [0, 2], end_record), lambda: prediction.queue_run_prediction_charge_min_max(try_soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, [0, 2], end_record)),
        ("export", lambda: prediction.thread_run_prediction_export(5.0, export_window[1]["start"] + 15, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record), lambda: prediction.queue_run_prediction_export(5.0, export_window[1]["start"] + 15, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)),
    ]

    for name, direct_call, queue_call in cases:
        # The queued run goes first, so the direct run cannot be answered out of the cache the queued
        # one just populated - and vice versa, hence the clear between them
        prediction.prediction_cache = {}
        queued = queue_call().get()
        prediction.prediction_cache = {}
        direct = direct_call()
        if queued != direct:
            print("ERROR: {} queued {} != direct {}".format(name, queued, direct))
            failed = True

    if not failed:
        print("Queued predictions match the direct path for all five shapes")
    return failed


def test_batch_is_lazy(my_predbat):
    """Nothing runs until the first get(), and one get() flushes the whole batch, returns True on failure"""
    print("**** Running batch laziness tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes

    handles = [prediction.queue_run_prediction_charge(round(soc, 2), 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record) for soc in (1.0, 2.0, 3.0)]
    if len(prediction.pending_batch) != 3:
        print("ERROR: expected 3 pending jobs, got {}".format(len(prediction.pending_batch)))
        failed = True
    if any(handle.job.result is not None for handle in handles):
        print("ERROR: a job ran before its handle was read")
        failed = True

    handles[0].get()
    if prediction.pending_batch:
        print("ERROR: pending batch not drained by the first get(), {} left".format(len(prediction.pending_batch)))
        failed = True
    if any(handle.job.result is None for handle in handles):
        print("ERROR: one get() did not flush the whole batch")
        failed = True

    # A second read must not re-run anything
    before = handles[2].job.result
    if handles[2].get() is not before:
        print("ERROR: reading a handle twice recomputed the result")
        failed = True

    if not failed:
        print("Batch laziness tests passed")
    return failed


def test_batch_cache_and_dedup(my_predbat):
    """Cache hits and intra-batch duplicates must never reach the kernel, returns True on failure"""
    print("**** Running batch cache/dedup tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes
    prediction.prediction_cache_enable = True

    calls = []
    real_batch = prediction_batch.run_prediction_kernel_batch

    def spy(pred, jobs, n_threads):
        """Record how many jobs each batch call was handed, then run it for real"""
        calls.append(len(jobs))
        return real_batch(pred, jobs, n_threads)

    prediction_batch.run_prediction_kernel_batch = spy
    try:
        # Three identical jobs in one batch must collapse to one kernel job
        prediction.prediction_cache = {}
        handles = [prediction.queue_run_prediction_charge(4.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record) for _ in range(3)]
        results = [handle.get() for handle in handles]
        if calls != [1]:
            print("ERROR: expected one kernel job for three identical trials, got {}".format(calls))
            failed = True
        if results[0] != results[1] or results[1] != results[2]:
            print("ERROR: deduplicated jobs returned different results")
            failed = True

        # And a repeat in a later batch must come from the cache without a kernel call at all
        del calls[:]
        again = prediction.queue_run_prediction_charge(4.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
        if calls:
            print("ERROR: a cached scenario still reached the kernel: {}".format(calls))
            failed = True
        if again != results[0]:
            print("ERROR: cached result {} differs from the computed one {}".format(again, results[0]))
            failed = True
    finally:
        prediction_batch.run_prediction_kernel_batch = real_batch

    if not failed:
        print("Batch cache/dedup tests passed")
    return failed


def test_batch_fallbacks(my_predbat):
    """A job the kernel cannot take still runs, and gives the Python engine's answer, returns True on failure"""
    print("**** Running batch fallback tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes

    # Two references, because the fallbacks land on two different engines. A batch the kernel refused
    # still re-runs through run_prediction, which dispatches to the kernel; a Prediction the kernel
    # will not serve at all re-runs on the Python engine. Those two agree to 1e-6 (kernel_parity), not
    # bit-exactly, so each fallback is compared against the engine it actually used.
    prediction.prediction_cache = {}
    expected = prediction.thread_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    prediction.debug_enable = True
    prediction.prediction_cache = {}
    expected_python = prediction.thread_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record)
    prediction.debug_enable = False

    # 1. Whole batch refused (no kernel, stale binary, or a non-zero return code)
    real_batch = prediction_batch.run_prediction_kernel_batch

    def refuse_all(pred, jobs, n_threads):
        """Stand in for a kernel that cannot run the batch at all"""
        return None

    def refuse_each(pred, jobs, n_threads):
        """Stand in for a kernel that ran the batch but rejected every job"""
        return [(None, 0.0, 0.0)] * len(jobs)

    for name, stub in (("whole batch", refuse_all), ("per job", refuse_each)):
        prediction_batch.run_prediction_kernel_batch = stub
        try:
            prediction.prediction_cache = {}
            got = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
            if got != expected:
                print("ERROR: {} fallback returned {} expected {}".format(name, got, expected))
                failed = True
        finally:
            prediction_batch.run_prediction_kernel_batch = real_batch

    # 2. Debug runs must not reach the kernel from the batch when the direct path would not take it
    prediction.debug_enable = True
    try:
        prediction.prediction_cache = {}
        got = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
        if got != expected_python:
            print("ERROR: debug_enable fallback returned {} expected {}".format(got, expected_python))
            failed = True
    finally:
        prediction.debug_enable = False

    # 3. Kernel genuinely unavailable for this Prediction
    prediction.kernel_handle = 0
    prediction.prediction_cache = {}
    got = prediction.queue_run_prediction_charge_min_max(3.0, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record).get()
    if got != expected_python:
        print("ERROR: kernel-less fallback returned {} expected {}".format(got, expected_python))
        failed = True

    if not failed:
        print("Batch fallback tests passed")
    return failed
```

and rewrite the runner at the bottom of the file:

```python
def run_prediction_batch_tests(my_predbat):
    """Run every batched prediction test, returns True on failure"""
    failed = test_export_trial_does_not_mutate_caller_window(my_predbat)

    available, required_failure = kernel_available()
    if not available:
        print("WARNING: kernel not available - batch tests that need it are SKIPPED")
        return failed or required_failure

    state = snapshot_scenario_state(my_predbat)
    try:
        failed |= test_queued_matches_direct(my_predbat)
        failed |= test_batch_is_lazy(my_predbat)
        failed |= test_batch_cache_and_dedup(my_predbat)
        failed |= test_batch_fallbacks(my_predbat)
    finally:
        restore_scenario_state(my_predbat, state)
        my_predbat.prediction_kernel_enable = False

    if failed:
        print("**** Prediction batch tests FAILED ****")
    else:
        print("**** Prediction batch tests passed ****")
    return failed
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test prediction_batch > /tmp/predbat-task4.log 2>&1; grep -nE "ERROR|FAIL|Traceback|ModuleNotFound|AttributeError" /tmp/predbat-task4.log | head -20
```

Expected: `ModuleNotFoundError: No module named 'prediction_batch'`.

- [ ] **Step 3: Write `prediction_batch.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Lazy batching of the prediction fan-out.

Every optimiser fan-out in plan.py is launch-all-then-collect-all, so the launches can queue instead
of running: each one prepares its trial inputs, appends a BatchJob and returns a BatchHandle, and the
first get() runs the whole batch through a single pk_run_batch call. That pays the Python/C boundary
and the window marshalling once per fan-out instead of once per scenario, and it is the only shape
that lets the kernel spread the work across cores - a Python thread pool cannot (the GIL serialises
it) and a process pool cannot (the parent's pickling is a serial bottleneck); both were measured and
rejected.

INVARIANT: a caller must not mutate any list or window dict it has handed to a queue_* entry point
until it has read the matching handle. The four fan-outs in plan.py satisfy this; the 20-scenario
byte-identical plan comparison is what keeps it that way.
"""

from operator import itemgetter

from const import PREDICT_STEP
from prediction_kernel import BatchJob, kernel_supported, run_prediction_kernel_batch

# Pulls (start, end) from a window dict; used to build the prediction cache key without a Python
# level loop over every window
window_bounds = itemgetter("start", "end")


def prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step):
    """Hash a simulation's inputs into its prediction cache key.

    Shared by Prediction.run_prediction and the batch runner so a scenario cannot land under two
    different keys depending on which path reached it. Built as one tuple hash to keep the per-window
    hashing in C rather than looping in Python, which matters because it runs on every simulation
    with a few hundred windows.
    """
    return hash(
        (
            tuple(charge_limit),
            tuple(map(window_bounds, charge_window)),
            tuple(export_limits),
            tuple(map(window_bounds, export_window)),
            pv_scenario,
            end_record,
            step,
        )
    )


def shape_batch_result(job, result, soc_range_min, soc_range_max):
    """Shape a full run_prediction result into what this job's thread_run_prediction_* returned"""
    shaped = tuple(result[:11])
    if job.want_range:
        shaped = shaped + (soc_range_min, soc_range_max)
    return shaped


class BatchHandle:
    """Result handle for a queued prediction - the drop-in for the pool's AsyncResult"""

    __slots__ = ("pred", "job")

    def __init__(self, pred, job):
        """Bind the handle to the Prediction that owns the batch and to its own job"""
        self.pred = pred
        self.job = job

    def get(self):
        """Return this job's result, flushing the whole pending batch on the first call"""
        if self.job.result is None:
            self.pred.flush_batch()
        return self.job.result


class PredictionBatch:
    """Pending-batch state and flushing for Prediction.

    A mixin rather than a separate object so the pending list lives directly on the Prediction that
    owns the kernel context, with no reference cycle between the two.
    """

    def enqueue_prediction(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache, want_range=False, range_window=None):
        """Queue one prediction and return a handle to its eventual result"""
        sim_hash = prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step) if cache else None

        # The kernel tracks the SoC range itself over a step range, so a min/max job does not have to
        # ship an 8928-entry SoC buffer. A negative start step means "no range asked for", which the
        # kernel answers with (soc_max, 0) - exactly what the Python all_n path returns unclamped.
        soc_range_start_step = -1
        soc_range_end_step = -1
        if range_window is not None:
            predict_minute_start = max(int((range_window["start"] - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((range_window["end"] - self.minutes_now) / 5) * 5
            soc_range_start_step = predict_minute_start // PREDICT_STEP
            soc_range_end_step = predict_minute_end // PREDICT_STEP

        job = BatchJob(
            charge_limit,
            charge_window,
            export_window,
            export_limits,
            pv_scenario,
            end_record,
            step,
            soc_range_start_step=soc_range_start_step,
            soc_range_end_step=soc_range_end_step,
            sim_hash=sim_hash,
            want_range=want_range,
            range_window=range_window,
        )
        self.pending_batch.append(job)
        return BatchHandle(self, job)

    def run_batch_job_python(self, job):
        """Run one job through the normal prediction path, for when the kernel cannot take it"""
        result = self.run_prediction(job.charge_limit, job.charge_window, job.export_window, job.export_limits, job.pv_scenario, end_record=job.end_record, step=job.step, cache=job.sim_hash is not None)
        if not job.want_range:
            return tuple(result[:11])
        if job.range_window is None:
            return tuple(result[:11]) + (self.soc_max, 0)
        return tuple(result[:11]) + self.scan_soc_range(result[11], job.range_window)

    def flush_batch(self):
        """Run every pending job: cache hits first, then one kernel call, then any fallbacks"""
        pending = self.pending_batch
        if not pending:
            return
        # Cleared before anything runs, so a fallback that re-enters run_prediction cannot see a
        # half-flushed batch or flush it a second time
        self.pending_batch = []

        cache = self.prediction_cache
        kernel_jobs = []
        duplicates = []
        first_by_hash = {}
        for job in pending:
            if job.sim_hash is not None:
                cached = cache.get(job.sim_hash)
                if cached is not None:
                    job.result = shape_batch_result(job, cached, 0.0, 0.0)
                    continue
                # Two identical trials in one fan-out used to be a cache hit for the second one; the
                # batch has to collapse them itself or that hit is lost (~44% of calls are hits)
                first = first_by_hash.get(job.sim_hash)
                if first is not None:
                    duplicates.append((job, first))
                    continue
                first_by_hash[job.sim_hash] = job
            kernel_jobs.append(job)

        if kernel_jobs:
            # kernel_supported() is a property of the Prediction, not of the individual job - it also
            # refuses debug runs, which would otherwise reach the kernel here when the direct path
            # would have used the Python engine. save is always None for a queued job: a saving run
            # calls run_prediction directly and never fans out.
            batched = run_prediction_kernel_batch(self, kernel_jobs, self.batch_threads) if kernel_supported(self, None, PREDICT_STEP) else None
            for index, job in enumerate(kernel_jobs):
                entry = batched[index] if batched is not None else None
                if entry is None or entry[0] is None:
                    job.result = self.run_batch_job_python(job)
                    continue
                result, soc_range_min, soc_range_max = entry
                if job.sim_hash is not None:
                    # Stored without the SoC/car data to save memory, mirroring run_prediction
                    cache[job.sim_hash] = result[:11] + ([], []) + result[13:]
                job.result = shape_batch_result(job, result, soc_range_min, soc_range_max)

        for job, first in duplicates:
            job.result = first.result
```

- [ ] **Step 4: Wire the mixin and the queue entry points into `Prediction`**

In `apps/predbat/prediction.py`, replace the local `window_bounds` definition and the inline hash in `run_prediction` with the shared ones, and add the mixin. Imports:

```python
from prediction_batch import PredictionBatch, prediction_cache_key
```

Delete the module-level `window_bounds = itemgetter("start", "end")` and its comment (and the now-unused `from operator import itemgetter` if nothing else uses it — check first).

Class declaration:

```python
class Prediction(PredictionBatch):
```

In `__init__`, after the `PRED_GLOBAL["dict"] = self.__dict__.copy()` line:

```python
            # Deliberately after the PRED_GLOBAL snapshot: a pool worker reconstructs its Prediction
            # from that dict and never queues anything, so it has no business carrying a batch.
            self.pending_batch = []
            self.batch_threads = 1
```

In `run_prediction`, replace the inline `sim_hash = hash((...))` block with:

```python
        sim_hash = None
        if cache and not save:
            sim_hash = prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step)
            cached_result = self.prediction_cache.get(sim_hash)
            if cached_result is not None:
                # Return cached result
                return cached_result
```

Then add the four queue entry points, next to their `thread_run_prediction_*` twins:

```python
    def queue_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step):
        """Queue a single-scenario prediction, returning a handle - the batch runs on the first get()"""
        charge_limit, export_limits = self._prepare_single(charge_limit, export_limits)
        return self.enqueue_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, self.prediction_cache_enable)

    def queue_run_prediction_charge(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a charge-window trial prediction, returning a handle"""
        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)
        return self.enqueue_prediction(try_charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, PREDICT_STEP, self.prediction_cache_enable)

    def queue_run_prediction_charge_min_max(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a charge-window trial prediction that also reports the SoC range across that window.

        Uncached, exactly as the direct path is: the SoC range is not part of the cached result, so a
        hit would answer with the wrong shape.
        """
        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)
        range_window = None if all_n else charge_window[window_n]
        return self.enqueue_prediction(try_charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, PREDICT_STEP, False, want_range=True, range_window=range_window)

    def queue_run_prediction_export(self, this_export_limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue an export-window trial prediction, returning a handle"""
        export_window, export_limits = self._prepare_export(this_export_limit, start, window_n, export_window, export_limits, all_n)
        return self.enqueue_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, PREDICT_STEP, self.prediction_cache_enable)
```

- [ ] **Step 5: Switch the launch sites and stop creating the pool**

In `apps/predbat/plan.py`, replace all four `launch_run_prediction_*` bodies:

```python
    def launch_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step=PREDICT_STEP):
        """Queue a prediction and return a handle to its result"""
        return self.prediction.queue_run_prediction_single(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step)

    def launch_run_prediction_charge(self, loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a prediction and return a handle to its result"""
        return self.prediction.queue_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    def launch_run_prediction_charge_min_max(self, loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a prediction and return a handle to its result"""
        return self.prediction.queue_run_prediction_charge_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    def launch_run_prediction_export(self, this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record):
        """Queue a prediction and return a handle to its result"""
        return self.prediction.queue_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record)
```

Delete the `wrapped_run_prediction_*` import (line 27 becomes `from prediction import Prediction`) and the `from multiprocessing import Pool, cpu_count` import — both are unused now and Flake8 will fail otherwise.

Delete the pool creation block (`# Create pool` through the `self.log("Not using threading...")` line) and the LoadML pool-disable block above it, keeping the LoadML status log:

```python
        # Check if LoadML is active - it used to force the process pool off, which no longer exists;
        # the kernel's threads are C++ threads with no fork and no NumPy involvement
        load_ml_comp = self.components.get_component("load_ml") if self.components else None
        if load_ml_comp:
            self.log("LoadML is_calculating {}".format(load_ml_comp.is_calculating()))

        # Serial for now - Task 5 wires this to the threads config
        self.prediction.batch_threads = 1
```

Place that `batch_threads` assignment immediately after `self.prediction = Prediction(...)` at line 1336.

- [ ] **Step 6: Run the new tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test prediction_batch --test kernel_parity > /tmp/predbat-task4.log 2>&1; grep -nE "ERROR|FAIL|passed|Traceback" /tmp/predbat-task4.log | head -30
```

- [ ] **Step 7: Verify plans are byte-identical — this is the real gate**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-task4-run.log 2>&1; python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json > /tmp/predbat-task4-cmp.log 2>&1; tail -30 /tmp/predbat-task4-cmp.log
```

Expected: 20 unchanged, `+0.0000` on metric, cost and all three PV futures. If a scenario moves, the most likely causes in order are: a caller mutating an input between launch and get (the invariant above), the min/max range steps disagreeing with the Python scan, or a cache key built differently on the two paths.

- [ ] **Step 8: Run the full suite including the slow tests**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all > /tmp/predbat-task4-all.log 2>&1; grep -nE "FAILED|ERROR|Traceback|tests passed|seconds" /tmp/predbat-task4-all.log | tail -25
```

- [ ] **Step 9: Record the serial benchmark**

Sum `runtime_s` across the 20 scenarios, best of 3 runs. Run-to-run spread is 2-6%, so a single run proves nothing — an earlier single-run measurement in this work produced a bogus 18% "win" that was pure noise.

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && for i in 1 2 3; do python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-bench-serial-$i.log 2>&1; python3 -c "import json;print(round(sum(s['runtime_s'] for s in json.load(open('random_results.json'))['results']), 2))"; done
```

Baseline to beat: **31.6s**.

- [ ] **Step 10: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit && git add apps/predbat/prediction_batch.py apps/predbat/prediction.py apps/predbat/plan.py apps/predbat/tests/test_prediction_batch.py && git commit -m "perf(plan): batch the prediction fan-out into one kernel call

launch_run_prediction_* no longer runs anything: it prepares the trial inputs, queues a job and
returns a handle whose first get() flushes the whole batch through a single pk_run_batch call. Window
start/end arrays are marshalled once per fan-out rather than once per scenario, cache hits and
intra-batch duplicates are resolved in Python first, and anything the kernel refuses falls back to the
Python engine per job. Serial for now.

20 random scenarios byte-identical.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Turn on kernel threading

**Files:**
- Modify: `apps/predbat/plan.py` (threads config → `batch_threads`)
- Modify: `docs/apps-yaml.md:285-297` (`threads` now means kernel threads)
- Test: `apps/predbat/tests/test_prediction_batch.py`

**Interfaces:**
- Consumes: `Prediction.batch_threads` (Task 4), `run_prediction_kernel_batch`'s `n_threads` (Task 2).
- Produces: nothing new — `batch_threads` is now derived from `args["threads"]`.

`pk_run_batch` was measured bit-identical at every thread count in the C-level test (Task 2 pins that), so any divergence here is a Python bug, not a race in the kernel.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_prediction_batch.py`:

```python
def test_batch_threads_do_not_change_results(my_predbat):
    """A threaded flush must return exactly what a serial one does, returns True on failure.

    pk_run_batch is pinned bit-identical across thread counts at the C level (kernel_parity), so this
    is really checking the Python side: that results are matched back to their own job regardless of
    how the kernel split the work.
    """
    print("**** Running batch threading tests ****")
    failed = False
    prediction, charge_window, export_window, charge_limit, export_limits = make_batch_prediction(my_predbat)
    end_record = my_predbat.forecast_minutes
    socs = [round(my_predbat.soc_max * fraction, 2) for fraction in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)]

    reference = None
    for n_threads in (1, 2, 8):
        prediction.batch_threads = n_threads
        prediction.prediction_cache = {}
        handles = [prediction.queue_run_prediction_charge(soc, 1, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, None, end_record) for soc in socs]
        results = [handle.get() for handle in handles]
        if reference is None:
            reference = results
        elif results != reference:
            for index, (got, want) in enumerate(zip(results, reference)):
                if got != want:
                    print("ERROR: job {} at {} threads returned {} expected {}".format(index, n_threads, got, want))
            failed = True
    prediction.batch_threads = 1

    if not failed:
        print("Batch threading tests passed - identical results at 1, 2 and 8 threads")
    return failed
```

and call it from `run_prediction_batch_tests` alongside the others.

- [ ] **Step 2: Run it and watch it pass already**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test prediction_batch > /tmp/predbat-task5.log 2>&1; grep -nE "ERROR|FAIL|passed|Traceback" /tmp/predbat-task5.log | head -20
```

This one is expected to pass immediately — the plumbing landed in Task 4; the test exists to pin it before the config starts driving it. If it fails, stop: that is a real threading bug and the config must not be wired until it is understood.

- [ ] **Step 3: Wire the config**

In `apps/predbat/plan.py`, re-add the import:

```python
from multiprocessing import cpu_count
```

and replace the placeholder from Task 4 with:

```python
        # The kernel spreads one batched fan-out across threads with the GIL released for the whole
        # call, so these are real cores - unlike a Python ThreadPool, which peaked at 1.15x on two
        # threads and then degraded below serial (perf/threadpool-prototype).
        threads = self.get_arg("threads", "auto")
        if threads == "auto":
            self.prediction.batch_threads = cpu_count()
        else:
            self.prediction.batch_threads = max(int(threads), 1)
        self.log("Prediction batch using {} kernel thread(s)".format(self.prediction.batch_threads))
```

- [ ] **Step 4: Verify plans are byte-identical with threading on**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-task5-run.log 2>&1; python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json > /tmp/predbat-task5-cmp.log 2>&1; tail -30 /tmp/predbat-task5-cmp.log
```

Note the benchmark harness may set `threads: 0` (as `annual.py` does deliberately). Check which the run used — grep the run log for `Prediction batch using` — and if it is 1, re-run the benchmark with the template's `threads` raised to `auto` to actually exercise threading before claiming a number.

- [ ] **Step 5: Benchmark, best of 3**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && for i in 1 2 3; do python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-bench-threaded-$i.log 2>&1; python3 -c "import json;print(round(sum(s['runtime_s'] for s in json.load(open('random_results.json'))['results']), 2))"; done
```

Record serial vs threaded against the 31.6s baseline. Remember what the brief predicts: the batch call itself is not faster (0.99x vs a loop of `pk_run`) — the win is the Python-side marshalling plus, now, the threads. Kernel time was already down to ~21.6% of plan time, so Amdahl caps what threading alone can return.

- [ ] **Step 6: Update the docs**

`docs/apps-yaml.md`, the `threads` section:

```markdown
### threads

If defined sets the number of threads to use during plan calculation, the default is 'auto' which will use the same number of threads as you have CPUs in your system.

Predbat batches each group of simulations into a single call to the C++ prediction kernel, which then
spreads them across this many threads. Results do not depend on the thread count - the same plan is
produced at any setting - so this only trades CPU for planning time.

Valid values are:

- 'auto' - Use the same number of threads as your CPU count
```

Keep the rest of the section as it stands, and check whether the list below it still describes reality (e.g. any wording about "0 - no threading" is still correct: 0 means serial).

```bash
cd /Users/treforsouthwell/predbat/batpred && grep -rn "threads" docs/*.md apps/predbat/*.yaml 2>/dev/null | grep -vi "threshold" | head -20
```

Update anything else that describes threads as worker processes.

- [ ] **Step 7: Full suite and commit**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all > /tmp/predbat-task5-all.log 2>&1; grep -nE "FAILED|ERROR|Traceback|tests passed" /tmp/predbat-task5-all.log | tail -20
```

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit && git add apps/predbat/plan.py apps/predbat/tests/test_prediction_batch.py docs/apps-yaml.md && git commit -m "perf(plan): run each batched fan-out across kernel threads

args[threads] now sizes the kernel's thread pool rather than a pool of worker processes. The GIL is
released for the whole pk_run_batch call, so unlike a Python ThreadPool these are real cores.

Identical results at 1, 2 and 8 threads; 20 random scenarios byte-identical.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Delete the process pool

**Files:**
- Modify: `apps/predbat/plan.py` (`DummyThread`, pool close/join, unused imports)
- Modify: `apps/predbat/prediction.py` (`wrapped_run_prediction_*`, `PRED_GLOBAL`, `reset_prediction_globals`)
- Modify: `apps/predbat/predbat.py` (`self.pool` lifecycle)
- Modify: `apps/predbat/annual.py:1004`, `apps/predbat/marginal.py:89` (comments referencing `PRED_GLOBAL`)
- Delete: `PHASE_B_BATCH_REFACTOR.md`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: no `self.pool`, no `PRED_GLOBAL`, no `DummyThread`, no `wrapped_run_prediction_*`.

**Stated consequence:** with the pool gone, a Predbat install where the kernel is unavailable (no binary for the architecture, a stale binary, or `debug_enable` on) plans serially in Python where it used to fan out across processes. The pool was measured at 1.11x on 16 cores with the kernel active; it will be worth more than that without one. This is the brief's call, made explicitly — if that regression is not acceptable, the alternative is to keep the pool solely for the `not kernel_supported()` case, which means keeping `wrapped_run_prediction_*` and `PRED_GLOBAL` alive with it.

- [ ] **Step 1: Find every reference before deleting anything**

```bash
cd /Users/treforsouthwell/predbat/batpred && grep -rn "PRED_GLOBAL\|wrapped_run_prediction\|DummyThread\|reset_prediction_globals\|\.pool\b\|apply_async" apps/predbat/ --include="*.py" > /tmp/predbat-pool-refs.txt; cat /tmp/predbat-pool-refs.txt
```

Work the list top to bottom. Note `apps/predbat/tests/` may reference these too.

- [ ] **Step 2: Delete the dead code**

- `apps/predbat/prediction.py`: remove `PRED_GLOBAL`, `reset_prediction_globals`, all four `wrapped_run_prediction_*`, the `global PRED_GLOBAL` lines in `__init__`, and the `PRED_GLOBAL["dict"] = self.__dict__.copy()` line. `self.pending_batch = []` / `self.batch_threads = 1` stay where they are (the comment above them about the PRED_GLOBAL snapshot no longer applies — delete it).
- `apps/predbat/plan.py`: remove `DummyThread` and its preamble comment, and the pool `close()`/`join()` blocks at ~1621-1628. Check whether `import time` is still used elsewhere in the file before removing it.
- `apps/predbat/predbat.py`: remove the `self.pool` attribute and its terminate/close/join blocks (lines ~283-288, 320, 1180-1188, 1612, 1735-1743).
- `apps/predbat/annual.py`: the `predbat.args["threads"] = 0` line stays (it now means serial batching, which is still what a per-day annual run wants), but rewrite the comment — the PRED_GLOBAL/spawn-safety reasoning it gives is gone. Something like: "The annual tool plans hundreds of small, fast days per run, where splitting each fan-out across kernel threads costs more in thread setup than it returns."
- `apps/predbat/marginal.py:89`: drop the "this updates PRED_GLOBAL which is safe since we run synchronously" comment; the remaining sentence about not needing 10% extra load stays.

- [ ] **Step 3: Verify nothing references the removed names**

```bash
cd /Users/treforsouthwell/predbat/batpred && grep -rn "PRED_GLOBAL\|wrapped_run_prediction\|DummyThread\|reset_prediction_globals\|apply_async\|self\.pool" apps/predbat/ --include="*.py"
```

Expected: no output.

- [ ] **Step 4: Full suite plus the parity gate**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-task6-run.log 2>&1; python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json > /tmp/predbat-task6-cmp.log 2>&1; tail -30 /tmp/predbat-task6-cmp.log
```

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all > /tmp/predbat-task6-all.log 2>&1; grep -nE "FAILED|ERROR|Traceback|tests passed|seconds" /tmp/predbat-task6-all.log | tail -25
```

`annual_integration` is the one most likely to notice this task — it explicitly sets `threads: 0` and its comment about the pool is what changed. Check its timing in the log against the ~34s the brief records.

- [ ] **Step 5: Delete the brief**

```bash
cd /Users/treforsouthwell/predbat/batpred && git rm PHASE_B_BATCH_REFACTOR.md
```

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit && git add -A apps/predbat docs && git commit -m "refactor(plan): remove the prediction process pool

Nothing has launched work into it since the fan-out started batching, so PRED_GLOBAL, the four
wrapped_run_prediction_* trampolines, DummyThread and the pool lifecycle all go with it. The pool was
measured at 1.11x on 16 cores, with the parent's per-call pickling as the serial bottleneck.

20 random scenarios byte-identical.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Final verification before the branch is offered for merge

Run all four gates from the brief, in this order, and paste the actual output rather than summarising it:

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml --random-scenarios cases/random_scenarios.yaml > /tmp/predbat-final-run.log 2>&1; python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json > /tmp/predbat-final-cmp.log 2>&1; tail -30 /tmp/predbat-final-cmp.log
```

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all --test kernel_parity --test prediction_batch > /tmp/predbat-final-parity.log 2>&1; grep -nE "ERROR|FAILED|passed" /tmp/predbat-final-parity.log | tail -20
```

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage && source setup.csh && ./run_all > /tmp/predbat-final-all.log 2>&1; grep -nE "FAILED|ERROR|Traceback|tests passed|seconds" /tmp/predbat-final-all.log | tail -25
```

```bash
cd /Users/treforsouthwell/predbat/batpred && ./run_pre_commit
```

Required: 20 scenarios unchanged with `+0.0000` on metric, cost and all three PV futures; kernel parity green; full suite green including the slow tests; pre-commit clean (re-run and re-stage after the cspell dictionary auto-sorts).

Then report, best of 3 each: serial batch vs threaded batch vs the 31.6s pre-Phase-B baseline.

## What it actually measured, once built

The plan was executed and these are the results, kept here because the hand-off notes this plan was
written from are deleted by the last task.

**The win is the batching itself, and it is large.** Measured against `a131f12c`, the fork point,
with the kernel built from each commit's own source and confirmed active on both sides, runs
interleaved, best of 3, on a 16-CPU machine. All 20 scenarios byte-identical in every configuration.

| 20-scenario benchmark | fork point | this branch | delta |
|---|---|---|---|
| parallelism off (`threads: 0`) | 41.74s | 26.22s | **37.2% faster** |
| each side's default (`threads: auto`) | 40.02s | 25.81s | **35.5% faster** |

The 31.6s figure in the hand-off notes was measured in an earlier session under unknown conditions
and did not survive re-measurement — do not carry it forward.

**Neither side's parallelism is worth much.** The fork point's 16-process pool bought 41.74s → 40.02s
(4%); this branch's 16 kernel threads buy 26.22s → 25.81s (1.6%), inside the 2-6% noise band. The
entire improvement is the batching and the marshalling it removes, not concurrency.

**The fork point's process pool was also broken outside the `__main__` entry point.** Its workers
rebuild their `Prediction` from a module global populated only in the parent, which requires `fork`
start semantics; `hass.py` sets those only under `if __name__ == "__main__"`. On macOS (spawn by
default since 3.8) every worker therefore started empty and every scenario died with
`KeyError: 'dict'` — the benchmark above needed `fork` forced to get comparable numbers out of the
baseline at all. This branch removes that failure mode by removing the pool.

**Why**, from a `n_jobs` census over one benchmark run:

```text
25,091 pk_run_batch calls carrying 253,899 jobs
mean 10.12 jobs/call, median 6, p95 24, max 363
97.4% of calls carry fewer than 32 jobs
```

This plan's own profile predicted fan-outs of ~1176, ~900 and ~628 jobs. The gap is mostly not a
batching failure: the large fan-out is scoped to one `(loop_price, coarse)` iteration and its ~1176
launches become ~363 kernel jobs once the ~44% prediction-cache hit rate and the intra-batch dedup
have taken their share. The median of 6 is `optimise_charge_limit`'s min/max pre-pass, which is
genuinely that small. So the batch is collecting whole fan-outs — the fan-outs are just small.

At ~6 jobs of ~30us each, `pk_run_batch` builds and joins six `std::thread`s to cover ~180us of work.
Thread setup dominates by construction.

**Where the next change goes**, in order:

1. **`pk_run_batch` in `prediction_kernel.cpp`** — replace the per-call `std::vector<std::thread>`
   with a pool created once at `pk_context_create` and woken per batch. This is the smallest change
   with the largest effect, and it makes threading pay at today's batch sizes without touching Python.
2. **`plan.py`, not `prediction_batch.py`**, if larger batches are wanted. The ceiling is
   `optimise_charge_limit`'s internal serialisation: the min/max pre-pass computes the SoC-pruning
   envelope that decides which `try_socs` are launched at all. Breaking that read-then-decide
   dependency means either launching the unpruned SoC set speculatively or running the pre-pass for
   every window before any trial — both change optimiser semantics and risk moving plans.
   `enqueue_prediction`/`flush_batch` already accept arbitrarily large batches; they need no change.
3. **Not** a batch-size threshold before threading in `run_prediction_kernel_batch` — the C++ already
   clamps threads to job count, and a Python-side knob would only hide (1).

## Notes for whoever picks this up

- **`PR #4536` (`perf/window-bounds-cache`) is in flight and independent**, worth -21% on its own. Batching partly subsumes it — a batch ships window bounds once per fan-out rather than per call — but the `sim_hash` half still matters, and `prediction_cache_key()` in `prediction_batch.py` is exactly where that half would land. If both branches merge, re-profile before assuming what is left.
- **`round_py` is poison in any hot loop** — `snprintf`+`strtod`, ~96ns, serialising on libc's global locale, measured 17x degradation at 8 threads. Two calls remain in the kernel (`iboost_next`, `car_soc_next`), both once per run. Do not add a third.
- **Any new test that calls `calculate_plan` must not use the shared `my_predbat` fixture** — it arrives carrying whatever earlier tests left on it and `calculate_plan` raises. Follow `run_annual_integration_isolated` in `unit_test.py`: take a fresh instance from `create_predbat()` and load a debug YAML.
- **Do not re-litigate the dead ends**: Python `ThreadPool` (1.15x peak, then below serial), the process pool itself (1.11x on 16 cores), and expecting `pk_run_batch` to beat a loop of `pk_run` on its own (0.99x). All three were measured on this work.
