# Persistent scratch and thread pool in the prediction kernel

**Status:** implemented. Three things below were changed by measurement during implementation and are
recorded in place: stage 1 is a no-op on its own, broadcasting to wake workers was the wrong choice,
and the `threads: auto` cap this design would have added was measured and rejected. See "What
measurement changed" at the end.

**Branch:** `perf/kernel-thread-pool`, stacked on `perf/kernel-batch-abi` (PR #4540). Neither
`pk_run_batch` nor `PkScratch` exists on `main` — both arrive with that PR — so this work cannot be
based on `main`. The follow-up PR takes `perf/kernel-batch-abi` as its base so its diff shows only
this change, and is retargeted to `main` once #4540 merges.

## Why

PR #4540 replaced the per-scenario prediction fan-out with a batch that reaches the C++ kernel in one
`pk_run_batch` call. It made planning 35-37% faster, but enabling kernel threading on top of it
bought only 1.6% — inside the run-to-run noise band.

A census over one 20-scenario benchmark run explains why:

```text
25,091 pk_run_batch calls carrying 253,899 jobs
mean 10.12 jobs/call, median 6, p95 24, max 363
97.4% of calls carry fewer than 32 jobs
```

The median batch is six jobs of roughly 30us each — about 180us of simulation. Against that,
`pk_run_batch` builds and joins a fresh `std::vector<std::thread>` on every call, and the serial path
constructs a fresh `PkScratch` whose first use heap-allocates roughly 70KB. Both costs are paid per
call, and at these batch sizes both are the same order as the work they wrap.

The kernel's own comment already records that this class of cost dominates: scratch reuse was
introduced because "each scenario would otherwise heap-allocate ~70KB, and 1200 of those per batch
put every worker thread in contention on the allocator. Measured 4 threads at 0.6x of serial before
this, because the simulation is cheap enough that malloc, not arithmetic, was the bottleneck."

This design removes both per-call costs. It is deliberately staged, because the two halves carry very
different risk and the first may be enough on its own.

## Scope

**In scope:** `apps/predbat/prediction_kernel.cpp`, its tests, and the six shipped binaries.

**Out of scope:** any Python change. `n_threads` stays a per-call argument, the struct layouts do not
move, and the function signatures are unchanged, so `prediction_kernel.py` is untouched.

**Not an ABI or parity change.** Results are bit-identical before and after, so neither
`PK_ABI_VERSION` nor `PK_PARITY_REVISION` is bumped. A binary built before this change still loads
and runs correctly, just without the speedup. The six architecture binaries are still rebuilt and
shipped.

## Stage 1 — one scratch per thread

`PkScratch` is constructed at three sites: `pk_run` (prediction_kernel.cpp:1279), each worker lambda
in `pk_run_batch` (:1341), and the serial batch loop (:1352). All three become one accessor:

```cpp
// One scratch per thread, reused for that thread's lifetime. Every field is fully rewritten before
// it is read on each run - build_window_membership assigns exactly n_steps entries and the clip
// helpers clear-then-push - which is the same invariant that already lets one scratch serve a whole
// stride. Extending it across calls means a thread allocates these buffers once, not once per call.
// Thread-local rather than per-context because nothing here survives a simulation, so it never needs
// to belong to a particular context.
static PkScratch &thread_scratch()
{
    static thread_local PkScratch scratch;
    return scratch;
}
```

Each of the three sites drops its local and passes `thread_scratch()` instead.

### The risk this introduces

Reuse within a call spans one context, so `n_steps` is constant. Reuse across calls spans contexts,
and Predbat builds a new context every plan cycle — with a different `n_steps` whenever
`forecast_minutes` changes. `build_window_membership` assigns exactly `n_steps` entries and the clip
vectors clear before pushing, so a shrink cannot expose stale tail data through any read path.

That is an argument, not a proof, so it is pinned by a test (see Testing).

### Expected effect

Removes roughly 25,000 allocations of ~70KB per plan from the serial path — which is the default for
`threads: 0`, for `annual.py`, and for the whole test harness. Independently measurable and
independently shippable.

## Stage 2 — one pool for the process

A single pool, allocated on first threaded use and never destroyed, holding workers parked on a
condition variable.

```cpp
class PkThreadPool {
public:
    void reserve_workers(int32_t want);   // grows, never shrinks
    void run(const ContextStore *c, const PkBatchJob *jobs, int32_t n_jobs, PkBatchResult *results, int32_t use);
private:
    std::mutex mutex;
    std::condition_variable work_ready;   // workers park here
    std::condition_variable work_done;    // the caller waits here
    const ContextStore *cur_ctx = nullptr;
    const PkBatchJob *cur_jobs = nullptr;
    PkBatchResult *cur_results = nullptr;
    int32_t cur_n_jobs = 0;
    int32_t cur_use = 1;
    uint64_t generation = 0;              // bumped per batch; workers wake when it changes
    int32_t outstanding = 0;              // lanes still running
    std::vector<std::thread> workers;
};
```

### Dispatch

The lane count is `use = min(n_threads, n_jobs, workers.size() + 1)` — bounded by what the caller
asked for, by how much work there is, and by how many workers actually exist (see Thread creation
failure). The `+ 1` is the calling thread, which takes a lane itself.

The caller, holding `mutex`:

1. publishes the batch — context, jobs, results, `n_jobs` and `use`
2. sets `outstanding = use - 1`
3. bumps `generation`
4. broadcasts on `work_ready`

then releases the mutex and **runs lane 0 itself**, striding `i = 0; i < n_jobs; i += use`. This
matters at these batch sizes: six jobs wakes five workers rather than six, and the caller does useful
work instead of blocking. Finally it waits on `work_done` until `outstanding` reaches zero.

Each worker parks on `work_ready` until `generation` changes, records the new generation
unconditionally so it cannot spin, and participates only if its lane index is `< cur_use`. Workers
outside that range park again without touching `outstanding`. A participating worker copies the batch
pointers locally, releases the mutex, strides `i = lane; i < n_jobs; i += use`, then re-acquires the
mutex and notifies `work_done` if it was the last one out.

Workers simulate through `thread_scratch()` from stage 1, so a persistent worker allocates its
buffers on its first batch and reuses them for the life of the process. Stage 1 replaced the scratch
local in the per-call worker lambda; stage 2 replaces that lambda with a parked worker, and the
accessor carries over unchanged.

Nobody holds the mutex while simulating. The partition is the same stride the current code uses, and
`run_batch_job` already only reads the const context and writes its own result slot — the property
that makes today's threading safe is unchanged.

### Bypass

`n_threads <= 1 || n_jobs <= 1` runs inline exactly as today and never touches the pool, so
`threads: 0`, `annual.py` and the test harness never allocate a worker.

### Sizing

`reserve_workers` grows to whatever `n_threads` asks for and never shrinks. A later, smaller request
simply wakes fewer lanes.

## Error handling

**Fork.** Threads do not survive `fork()`, so a child would inherit a pool whose workers do not exist
and hang waiting on them. Nothing in Predbat forks today — PR #4540 deleted the last thing that did —
but `hass.py` still calls `set_start_method("fork")`, so a future feature that spawns a process would
trip it. A `pthread_atfork` child handler sets the pool pointer to null: no allocation, no locking,
async-signal-safe. The child lazily builds a fresh pool on its next batch, and the dead object leaks
in the child's address space, which is standard and costs a few hundred bytes. Registered once via
`std::call_once` before the pool is first created, guarded by `#if defined(__unix__) || defined(__APPLE__)`.

**Concurrent callers.** Two Python threads can enter `pk_run_batch` at once, because ctypes releases
the GIL. With one shared pool their published state would race, so a dispatch mutex is held for the
whole batch and concurrent callers serialise. Today they would each spawn private threads and run in
parallel, so this is a regression in a case nothing exercises; a data race is the worse option.

**Thread creation failure.** `std::thread` throws `std::system_error` under resource exhaustion.
`reserve_workers` catches it, keeps whatever workers were created, and `run` clamps `use` to
`workers.size() + 1` — falling all the way back to inline if none exist. A home-automation daemon
should degrade, not crash.

**Shutdown.** The pool is never destroyed, deliberately. A static destructor running while CPython
tears down risks a deadlock for no benefit; the OS reclaims the threads at exit.

**Per-job failures** are unchanged: each job's `status` field still carries its own result, so one
malformed scenario cannot discard the fan-out.

## Testing

Every new test is mutation-checked — break the thing deliberately, confirm the test fails, restore.
That method found three real defects on PR #4540, two of them tests that could not fail.

| Pins | Test |
|---|---|
| Stage 1's risk | Alternate two contexts with different `forecast_minutes` on one thread; each must match its single-context result. Stale tail data or a mis-sized buffer appears here and nowhere else. |
| Worker reuse | Run the 1/2/4/8-thread parity sweep twice. The first batch creates workers, the second wakes parked ones — different paths, and "works once, breaks on reuse" is the classic pool bug. |
| Lane arithmetic | `n_jobs < n_threads`: a 3-job batch at 16 threads, so `use = 3` and thirteen workers must park without touching `outstanding`. This is the median-6 production shape. |
| Grow then wake fewer | Call at 8 threads, then 2, then 16. |
| The dispatch mutex | Two Python threads calling `pk_run_batch` at once; results must match serial. |
| The `pthread_atfork` handler | `os.fork()`, run a batch in the child, compare against the parent's result, check the child's exit status. Skipped where fork is unavailable. Without this the handler is unverified code whose whole job is preventing a hang. |

Existing gates that must continue to hold: the `kernel_parity` sweeps, the 20-scenario byte-identical
run (`+0.0000` on metric, cost and all three PV futures), and the full suite.

## Measurement

Interleaved, best of 3 per side, on one machine in one sitting — the method used for PR #4540's
numbers.

- **Stage 1** on the serial benchmark (`threads: 0`), which is where the 25,000 allocations are.
- **Stage 2** on the threaded benchmark, with serial re-checked to confirm it has not regressed.

Each stage is measured against the tree it modifies, so the two effects are attributable separately.
If stage 1 recovers most of the available time, stage 2 can be dropped rather than accepting its
concurrency risk for a small remainder.

**Honest ceiling.** This turns a thread creation (tens of microseconds) into a wake (a few
microseconds), so threading can pay where it currently cannot. But the upside is still bounded by the
kernel's share of plan time, so single-digit to low-teens percent is the realistic expectation, not
anything resembling a 16x.

## Deliverables

- `apps/predbat/prediction_kernel.cpp`: `thread_scratch()`, `PkThreadPool`, the atfork handler, and
  `pk_run_batch` rewired onto them.
- `apps/predbat/tests/test_kernel_parity.py`: the six tests above.
- All six shipped binaries rebuilt with `build_kernel_cross.sh` and confirmed to load.
- No change to any Python module.

## What measurement changed

Three parts of the design above did not survive contact with a benchmark. They are left in place so
the reasoning can be compared against what actually happened.

**Stage 1 is a no-op on its own.** Removing ~25,000 allocations of ~70KB per plan changed nothing:
26.291s before, 26.344s after, interleaved best of 3 against ~1.8% run-to-run spread. The premise was
a misreading of the kernel's own comment, which recorded the allocator being the bottleneck under
four threads contending — a different problem from one thread repeatedly allocating and freeing the
same-sized block, which never leaves the allocator's thread cache. Stage 1 is kept only because the
pool's parked workers need per-thread scratch, and because its two tests are worth having.

**Broadcasting to wake workers cost most of the win.** The design says "broadcasts on `work_ready`".
Built that way the pool was worth 1.3% (25.833s → 25.489s), because a six-job batch woke all fifteen
workers and eleven of them went straight back to sleep — roughly 250,000 wasted wakeups per plan.
Giving each worker its own flag and condition variable, so exactly the needed lanes wake, took it to
3.7% (26.008s → 25.056s). The shipped code does that; the single `work_ready` variable in the sketch
above does not exist.

**The `threads: auto` cap was measured and rejected.** With the pool in place the fast-machine curve
peaks below the core count — serial 26.33s, 4 threads 24.89s, 6 threads 24.71s, 8 threads 24.91s,
16 threads 25.04s — which argues for capping `auto`. Re-running with each job made eight times dearer,
which is how a machine where the kernel dominates behaves, the curve stops turning over: 48.92s
serial, 32.03s at 4, 29.98s at 6, 29.85s at 8, 28.94s at 16. Capping at 4 therefore costs 0.7% on a
fast machine but 10.7% on a kernel-heavy one, while not capping costs 1.3% at worst. `auto` is left
as the core count; `resolve_batch_threads` in `plan.py` carries the numbers.

**Final result:** 26.33s serial → 24.71s at 6 threads on the 20-scenario benchmark, **6.1%**, on top
of the 35% the batching work delivered. All 20 scenarios byte-identical at every thread count, in
both the normal and the kernel-heavy regime.

## What the tests found

Every new test was mutation-checked, and two were not good enough on the first attempt:

- The **fork** test earns its place. With the `pthread_atfork` handler removed the forked child
  deadlocks on workers that no longer exist; the test kills it on a deadline and reports the timeout
  rather than hanging the suite.
- The **concurrency** test initially did not bite. With the dispatch mutex deleted, 25 rounds over 4
  lanes passed happily — the GIL keeps two callers from overlapping often enough for the race to
  land. At 400 rounds over 8 lanes it deadlocks within seconds. The round count is therefore
  load-bearing and the test says so.
- The **scratch cross-context** test cannot fail before its change, since without reuse there is
  nothing to leak; it was verified by relaxing `build_window_membership`'s `assign` to skip shrinking,
  which makes it fail immediately.
