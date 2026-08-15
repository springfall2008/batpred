# Phase B: batch the prediction fan-out from Python

Working notes for picking this up in a fresh session. Delete this file before the branch merges.

Branch: `perf/kernel-batch-abi` (4 commits, unpushed, based on `main`).

## Where things stand

The C++ side is done, verified and committed. The Python side has not been touched yet — it still
issues one `pk_run` call per scenario.

| commit | what |
|---|---|
| `a3254924` | `pk_run_batch` ABI added; stop filling the SoC series when the caller discards it |
| `2e71022e` | precomputed SoC percent buckets replace `round_py` in the hot loop |
| `be2510d0` | bucket table verification test; all 6 shipped binaries rebuilt |
| `f813d219` | fix: don't clamp the batch SoC range when none was requested |

Everything is byte-identical to before: all 20 random scenarios unchanged on metric, cost and all
three PV futures, `kernel_parity` green, full suite green including slow tests (101.22s).

### What the kernel work bought

Measured on `cases/random_scenarios.yaml` scenario 0 (the benchmark scenario), `threads=0`:

```
calculate_plan   2418.4 ms  ->  1758.5 ms     -27%
  C++ pk_run     1056.5 ms  ->   380.5 ms
  Python         1361.9 ms  ->  1378.0 ms     unchanged, as expected
C++ share            43.7%  ->     21.6%
```

20-scenario benchmark: **42.5s -> 31.6s**.

## Why Phase B is now the whole game

**Python is 78.4% of plan time.** The kernel is no longer the bottleneck by a wide margin — per
prediction it is roughly 20us of C++ against ~70us of Python. Every further optimisation has to come
off the Python side, and batching is the mechanism.

There is a second, independent reason: `pk_run_batch` already takes an `n_threads` argument and
scales properly now (**6.7x at 16 threads, bit-identical at every thread count**), but nothing can
use it until Python submits work in batches. One `pk_run` call per scenario cannot be parallelised —
that was measured and rejected twice, see "Dead ends" below.

## The design

### The call sites are already batch-shaped

This is the key enabler and the reason this refactor is tractable. Every fan-out in `plan.py` was
written for the process pool, so it already reads launch-all-then-collect-all:

```python
for try_soc in try_socs:
    results.append(self.launch_run_prediction_charge(...))   # fan out
for try_soc in try_socs:
    resultmid[try_soc] = results.pop(0).get()                # then collect
```

`DummyThread` exists purely so the non-pooled path mimics that shape. So **the call sites do not need
to change at all** — only what `launch_*` returns and when the work actually happens.

### Lazy batch

- `launch_run_prediction_*` stops running anything. It appends a job to a pending list on the
  `Prediction` and returns a `BatchHandle(batch, index)`.
- `BatchHandle.get()` flushes the whole pending batch on first call, then returns its own slot.
  Subsequent `get()`s read already-computed results.
- Flush: resolve prediction-cache hits in Python first, marshal only the misses into a
  `PkBatchJob` array, one `pk_run_batch` call, then shape each job's result.

Batch sizes this produces (scenario 0, from the profile):

```
launch_run_prediction_charge          10,698
launch_run_prediction_export           5,436
launch_run_prediction_single           4,752
launch_run_prediction_charge_min_max   3,294
                                      ------
                                      24,180 launches, ~19,209 reaching the kernel
```
`optimise_charge_limit_price_threads` fans out ~1176 at a time (392 candidates x 3 PV scenarios),
`optimise_charge_limit` ~900, `optimise_export` ~628. Those are the batches.

### Do not duplicate the input preparation

Each `thread_run_prediction_*` does three things: build the trial inputs, call `run_prediction`,
shape the result. The batch path needs the first and third but not the second.

**Split them rather than copying them**, so the direct and batch paths cannot drift:

```python
def _prepare_charge(self, try_soc, window_n, charge_limit, all_n):
    """Build the trial charge limits - shared by thread_run_prediction_charge and the batch path."""
    try_charge_limit = charge_limit.copy()
    if all_n:
        for set_n in all_n:
            try_charge_limit[set_n] = try_soc
    else:
        try_charge_limit[window_n] = try_soc
    return try_charge_limit
```

Same for export (`export_limits.copy()` plus the private window copy — see traps) and single
(pass-through). Then `thread_run_prediction_*` becomes prepare + `run_prediction` + shape, and the
batch enqueue is prepare + record.

### min/max jobs

`thread_run_prediction_charge_min_max` scans `predict_soc` across the charge window to get
`min_soc`/`max_soc`. The kernel now does this itself so the batch does not have to ship an
8928-entry SoC buffer per job (that would be ~84MB on a large batch). Set on the job:

```python
predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
job.soc_range_start_step = predict_minute_start // 5
job.soc_range_end_step = predict_minute_end // 5      # inclusive
```

and read `result.soc_range_min` / `result.soc_range_max` back.

**When `all_n` is set, Python skips the scan and returns `(soc_max, 0)` unclamped.** Pass
`soc_range_start_step = -1` for those; the kernel returns exactly `(soc_max, 0)` in that case (this
is what `f813d219` fixed).

### Fallbacks that must keep working

The batch path only applies when the kernel can run the job. Fall back to running the job through
the existing `thread_run_prediction_*` path, serially, when any of:

- `KERNEL_HAS_BATCH` is False (binary predates `pk_run_batch` — the loader probes for the symbol)
- `kernel_supported()` is False for the job (no kernel context, `debug_enable`, or a `save` run)
- `pk_run_batch` returns non-zero, or a job's `status` field is non-zero

A per-job `status` is returned precisely so one malformed scenario cannot discard the fan-out.

## Traps found the hard way

1. **`thread_run_prediction_export` used to mutate the caller's window dict** (`export_window[n]["start"] = start`).
   That was only safe because each pool worker mutated its own unpickled copy. Any shared-memory
   parallelism needs it local. The fix is on branch `perf/threadpool-prototype`:
   ```python
   export_window = list(export_window)
   export_window[window_n] = dict(window, start=start)
   ```
   Only `["end"]` is ever read back by `optimise_export`, so nothing depends on the write being
   visible. **Port this across.**

2. **`round_py` is poison in any hot loop.** It is `snprintf`+`strtod`, ~96ns, and serialises on
   libc's global locale — with constant work per thread it degrades **17x at 8 threads**. Two calls
   remain (`iboost_next`, `car_soc_next`), both once per run, both harmless. Do not add more.

3. **The prediction cache is worth keeping.** ~6,518 of 25,727 calls are hits (44%), saving far more
   than the hashing costs. Resolve hits in Python before building the batch.

4. **Test isolation.** Any new test that calls `calculate_plan` must not use the shared `my_predbat`
   fixture — it arrives carrying whatever earlier tests left on it and `calculate_plan` raises. Follow
   `run_annual_integration_isolated` / `run_window_cache_tests_isolated`: take a fresh instance from
   `create_predbat()` and load a debug YAML.

5. **`cd` in a Bash tool call does not persist**, and `git checkout` carries uncommitted changes
   across branches. Both bit during this session.

## Verification protocol

Non-negotiable, in this order:

```bash
cd coverage && source setup.csh

# 1. byte-identical plans - the real gate
python3 ../apps/predbat/unit_test.py --random-run \
    --random-template cases/predbat_debug_agile1.yaml \
    --random-scenarios cases/random_scenarios.yaml
python3 ../apps/predbat/unit_test.py --random-compare cases/random_results.json random_results.json
# require: 20 unchanged, +0.0000 on metric, cost and all three PV futures

# 2. kernel parity (includes the SoC bucket table check)
./run_all --test kernel_parity

# 3. full suite including slow tests
./run_all

# 4. quality gates
./run_pre_commit        # re-run after the dictionary auto-sorts, then re-stage
```

Benchmark by summing `runtime_s` across the 20 scenarios, best of 3. Run-to-run spread is ~2-6%, so
do not trust a single run — that mistake was made earlier in this work and produced a bogus 18%
"win" that turned out to be noise.

## Suggested order

1. Port the `thread_run_prediction_export` mutation fix from `perf/threadpool-prototype`.
2. Split `thread_run_prediction_*` into `_prepare_*` + shaping. **Verify parity here** — this step
   alone should be a no-op.
3. Add `BatchRunner` / `BatchHandle` and switch `launch_*` to enqueue. Keep `n_threads=1`.
   **Verify parity.** This isolates the marshalling win with no concurrency risk.
4. Turn on threading (`n_threads` from the `threads` config). **Verify parity again** — results were
   bit-identical at every thread count in the C-level test, so any divergence here is a Python bug.
5. Only then remove the process pool, `wrapped_run_prediction_*`, `PRED_GLOBAL` and `DummyThread`.

## Dead ends - do not repeat these

- **Python `ThreadPool` instead of the process pool.** `pk_run` does release the GIL (`ctypes.CDLL`)
  and the kernel is reentrant, but per-call Python overhead makes the parallel fraction too small.
  Peaks at **1.15x on two threads** then degrades below serial. Recorded with measurements on branch
  `perf/threadpool-prototype` (commit message, marked do-not-merge).
- **The process pool itself.** 1.11x on 16 cores; the parent's per-call pickling is a serial
  bottleneck. Removing it costs almost nothing.
- **Expecting the batch call itself to be faster.** `pk_run_batch` vs a loop of `pk_run` over 120
  scenarios measured **0.99x**. The C boundary crossing was never the cost. The batch is worth doing
  for the Python-side savings and because it is the only way to use threads — not for the call
  itself.

## Related work in flight

`PR #4536` (`perf/window-bounds-cache`) caches window start/end arrays and the prediction-cache key
tuple, worth **-21%** on its own and independent of this branch. It should compose: it removes Python
per-call work, this removes C++ per-call work. If both land, re-profile before assuming what is left.

Note that batching **partly subsumes** the window-bounds cache, since a batch ships window bounds once
rather than per call. The `sim_hash` half still matters.
