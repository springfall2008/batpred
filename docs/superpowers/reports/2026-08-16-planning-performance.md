# Planning performance: batching the fan-out and pooling the kernel's threads

Report on the work in PR #4540 and PR #4546, both merged on 2026-08-16.

**Result: the 20-scenario planning benchmark goes from 39.5s to 24.8s, 37% faster, with every
scenario producing an identical plan.**

## The measurement

Current `main` against the two branches together. Each side runs its own Python and its own compiled
kernel, interleaved on one 16-core machine, best of three rounds.

| 20-scenario suite | `main` | both PRs | change |
|---|---|---|---|
| Serial (`threads: 0`) | 41.70s | 26.26s | 37.0% faster |
| Default (`threads: auto`) | 39.53s | 24.82s | 37.2% faster |

All 20 scenarios identical on metric, cost, both PV futures, final SoC, battery cycles and carbon —
zero mismatches across 140 compared fields. The win holds with parallelism switched off, so it comes
from the batching rather than a threading effect that might not transfer to other hardware.

## What changed

### Batching the fan-out (#4540)

The optimiser runs thousands of trial simulations per plan. Each `launch_run_prediction_*` used to
run one immediately, or hand it to a pool of worker processes. It now prepares the trial inputs,
queues a job on the `Prediction`, and returns a handle. The first `.get()` flushes the whole fan-out:
prediction-cache hits resolved in Python, duplicate trials collapsed, and the remainder marshalled
once and run in a single call across the C boundary.

The call sites did not have to change — every fan-out in `plan.py` was already written
launch-all-then-collect-all for the old pool. The process pool, `PRED_GLOBAL` and the wrapper
trampolines are all deleted.

### A thread pool inside the kernel (#4546)

With batching in place, threading still bought only 1.6%. Instrumenting the kernel over one full plan
showed why:

| Per plan, inside `pk_run_batch` | serial | threaded |
|---|---|---|
| Calls / jobs | 25,091 / 253,899 | same |
| Threads created | 0 | 201,545 |
| Wall time in the function | 3131 ms | 2409 ms |
| — of which thread creation | — | 1507 ms |
| — of which work | 3131 ms | 902 ms |

The parallel work compresses well: 3131 ms of simulation becomes 902 ms across eight lanes. Thread
creation then ate almost all of it. The median batch is six jobs, so each call was paying eight
thread creations at 7.5 microseconds to cover roughly 180 microseconds of work. Workers now park on a
condition variable for the life of the process.

## What measurement overturned

Four things that seemed sound in design did not survive a benchmark. They are recorded here and in
the code rather than quietly dropped.

### Per-thread scratch buffers: no effect

The kernel allocated roughly 70KB of scratch per call, 25,000 times per plan. Removing that was
expected to pay on the serial path. It measured 26.291s before and 26.344s after — nothing, against
1.8% run-to-run spread.

The premise was a misreading. The kernel's own comment about the allocator being a bottleneck was
recorded under four threads contending, which is a different problem from one thread repeatedly
allocating and freeing the same-sized block and never leaving its thread cache. The change survives
only because the parked workers need per-thread scratch anyway.

### Broadcasting to wake workers: cost 2.4%

Built as designed, with one condition variable broadcast to all workers, the pool was worth 1.3%. A
six-job batch woke all fifteen workers and eleven went straight back to sleep — roughly 250,000
wasted wakeups per plan. Giving each worker its own flag and condition variable, so only the needed
lanes wake, took it to 3.7% on that change alone.

### Capping the default thread count: reversed

On a fast 16-core machine the curve peaks below the core count — 24.71s at six threads against 25.04s
at sixteen — which argued for capping `auto`.

Re-running with each job made eight times dearer, which is how a machine where the kernel dominates
behaves, the curve stops turning over at all:

| `threads` | normal | kernel 8x dearer |
|---|---|---|
| 0 (serial) | 26.33s | 48.92s |
| 4 | 24.89s | 32.03s |
| 6 | 24.71s | 29.98s |
| 8 | 24.91s | 29.85s |
| 16 | 25.04s | 28.94s |

Capping at four would cost 0.7% on fast hardware but 10.7% on the weak hardware the cap was meant to
protect. The cap was removed; `resolve_batch_threads` in `plan.py` carries the numbers.

### The inherited baseline: stale

A 31.6s baseline had been carried forward from an earlier session's notes. Re-measured on the same
machine it was 41.7s. Every figure in this report was measured fresh, interleaved, best of three.

## Defects found along the way

### A stale kernel binary was loaded instead of rejected

An earlier commit made the SoC output buffer optional — Python passes null for every cached run,
which is the common path — but left `PK_ABI_VERSION` at 3. An ABI 3 binary writes to that pointer
unconditionally, so the loader accepted a stale binary and Predbat segfaulted on its first prediction
rather than falling back to the Python engine.

Reproduced by building the kernel from `main` and pointing `PREDBAT_KERNEL_SO` at it: `model_kernel`
exits 139. Now bumped to ABI 4, so the loader reports a stale binary and uses the Python engine, which
is what the check exists to do. Anyone with a locally built library from an older checkout, or an
install whose per-architecture binary was not replaced, would have hit this.

### The old process pool was broken off the main entry point

Pool workers rebuilt their `Prediction` from a module global that only exists in the parent, which
needs `fork` start semantics that `hass.py` sets only under `if __name__ == "__main__"`. On any
spawn-default platform, macOS since Python 3.8, every worker started empty and every scenario died
with `KeyError: 'dict'`. Removing the pool removes the failure mode.

### A trial mutating the caller's data

The export trial wrote its start time straight into the caller's window dictionary, safe only because
each pool worker mutated its own unpickled copy. A batched fan-out shares one list across every job,
so this had to become local before anything else could proceed.

### A header that only libc++ provides transitively

The pool catches `std::system_error` when the system refuses a thread. libc++ declares it through
`<thread>`; libstdc++ does not, so the file compiled on macOS and failed to parse on GCC.

Worth knowing for the next kernel change: neither local build catches this. The native macOS build is
clang/libc++, and `build_kernel_cross.sh` produces the shipped Linux binaries through zig, which
bundles libc++ as well. So the cross-build targets glibc but not libstdc++, and it will happily
produce six loadable Linux binaries from source that a maintainer building with `g++` cannot compile.
Only CI, which builds natively with g++, closes that gap.

## How the tests were checked

Every new test was verified by deliberately breaking the thing it guards and confirming it fails.
That found five tests, three of them written during this work, that could not have failed:

- A gate keeping debug runs on the Python engine was pinned by comparing values the two engines agree
  on to within 1e-6. Deleting the gate entirely left the suite green.
- Nothing constrained how batch results were matched back to their jobs. A deliberately reversed
  mapping — every trial receiving another trial's cost — passed three separate suites.
- A replacement routing test had nine of ten trials producing identical costs, so a misrouted result
  would have coincidentally matched. The trial shape and seed were changed until all ten differ, and
  the distinctness is now asserted rather than assumed.
- The concurrency test passed with the dispatch mutex deleted: at 25 rounds the interpreter lock kept
  the two callers from overlapping. At 400 rounds over 8 lanes it deadlocks within seconds. That round
  count is load-bearing and the test says so.
- The fork test does earn its place. Remove the `pthread_atfork` handler and the forked child
  deadlocks on workers that no longer exist, which the test kills on a deadline rather than hanging
  the suite.

Alongside that: the full suite of 211 tests, the 20-scenario byte-identical gate re-run at every step,
and kernel parity pinned bit-identical across one, two, four and eight threads.

## Notes for whoever picks this up

`PK_PARITY_REVISION` stays at 5 while the ABI moved to 4. The bucket-table change altered how the hot
loop computes SoC percent, and it is pinned equivalent by `pk_verify_soc_percent_table`, so results do
not diverge. That is the same class of judgement that was got wrong for the ABI version, so it is
worth a second opinion.

The kernel is now about 12% of plan time, which caps what any further C++ work can return. The
remaining 88% is Python, and the batch-size census says those fan-outs are small and frequent — a
median of six jobs against an expected thousand, because a large fan-out's ~1176 launches become ~363
kernel jobs once the 44% prediction-cache hit rate and the intra-batch deduplication have taken their
share. Widening them means changing optimiser search semantics in `optimise_charge_limit`, where a
min/max pre-pass computes the SoC-pruning envelope that decides which trials are launched at all. That
is a design decision with real plan-difference risk, not an optimisation.

## Method

Benchmarks were run on a 16-core machine, interleaved rather than grouped, best of three, with the
kernel confirmed active on both sides of every comparison. Run-to-run spread on this benchmark is
2-6%, so single runs were not trusted. Plan equality was checked field by field across all 20
scenarios rather than on the headline metric alone.

Two measurement mistakes are worth recording. An early run reported plausible numbers that were
actually a stale `random_results.json` being re-read after every run had crashed; the runner now
deletes that file before each run so the failure mode is impossible rather than merely unlikely. And
`main`'s threaded column required forcing `fork` start semantics — left alone its process pool dies
with `KeyError: 'dict'`, which would have flattered these branches for an unrelated reason.
