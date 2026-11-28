# LoadML training benchmark and the predbat-load-ml board: design

Status: draft for the maintainer's approval, 27 Nov 2026. Branch `feat/board-predbat-load-ml`. Builder check done;
its questions are answered below (the peak target by the maintainer, the rest as my calls).

## Intent

LoadML's initial training is the most expensive thing Predbat does. A spike on the live install's 90-day LoadML
history (native macOS, so indicative only) took **251 s** and 900 epochs, and raised RSS from about 45 MB to a peak
of about **171 MB**. Every install pays that on first enable, after a `MODEL_VERSION` bump, and whenever the saved
model is discarded. A Raspberry Pi or a small Predbat.com container pays it in minutes and in headroom.

The maintainer wants two things:

1. **A benchmark** (`coverage/load_ml_benchmark`), a sibling of `coverage/memory_benchmark`, in which LoadML does one
   training cycle and reports how long it took and how much memory it used.
2. **A board** (`boards/predbat-load-ml`), built like `boards/predbat-memory`, whose workers cut that runtime and memory
   with the trained model's forecast unchanged.

## Decisions taken with the maintainer (27 Nov 2026)

- **Approach A:** a new driver for the existing memory-benchmark harness (image, container script, frozen clock,
  offline container), not a predictor-only harness and not training inside plan cycles. See Approaches.
- **The measured cycle is the initial training** (no saved model), not the 2-hourly fine-tune.
- **"Unchanged" means the 48 h forecast matches main's at dp2**, the maintainer's rule.
- **One run per tree per comparison.** Training is deterministic, so a worker compares main and its branch with one
  run each, back to back.
- **The memory target is the absolute peak RSS during the cycle**, not only what training adds on top of Predbat's
  baseline. Peak memory and runtime are what matter. So a cut to the RSS Predbat holds before training counts too.
- **My calls, stated to the maintainer and not objected to:**
  - The BLAS library is pinned to 4 threads to match `--cpus 4`.
  - The goal is training wall time and peak RSS each at least 25% below main's.
  - `measure.py` confirms over 3 rounds.
  - Benchmark runs never overlap: a machine-wide lock queues them, because wall time is sensitive to other load in a
    way RSS is not.
  - The measured cycle covers both LoadML ticks, the npz load and the training tick, without the idle gap between them.
  - `epochs_trained` must equal main's and `val_mae` must match at 4 dp, as well as the forecast digest.
  - If the baseline shows the 25% time cut is out of reach under bit-identical maths, the lead reports that to the
    maintainer and stops.

### What the spike showed, and why it shaped those decisions

| Run (native macOS, live 90-day history) | Change | Effect on the 48 h forecast |
| --- | --- | --- |
| Initial training, twice | none | identical (training is seeded, `np.random.seed(42)`) |
| Initial training | learning rate × 1.000001 | 572 of 576 steps differ at dp2; 48 h total 30.43 → 32.20 kWh |
| Fine-tune from the live model | learning rate × 1.000001 | no change |
| Fine-tune versus no training | — | at most 0.018 kWh cumulative |

Initial training is chaotic: a one-in-a-million change in the arithmetic gives a visibly different model. So **the dp2
rule means the floating-point maths must be bit-identical in practice**. That is the deal the maintainer chose to
start with. Fine-tune hardly moves the forecast, so a forecast check on fine-tune would prove little. That is why the
initial training is the measured cycle. Spike scripts: the session scratchpad, not committed.

## Goals

1. A worker runs `coverage/load_ml_benchmark <tree> <out dir>` and gets training wall time, CPU time, peak RSS and a
   forecast digest from one run, in under about 10 minutes.
2. Two runs of one tree give the same `forecast_digest`. Timing and RSS are close enough that a 25% change is clear
   from a single run each.
3. No network during a run, enforced by `--network none`.
4. The cycle measured is the component's own work up to and including its first training, run the way a real install
   runs it. That is two ticks of `LoadMLComponent.run()`. The first loads the npz history and fetches; the second shifts,
   fetches and merges, does the curriculum training, then predicts, publishes and saves. The idle gap of about 60 s
   between them is not counted. Moving work from one tick to the other saves nothing, because both are measured.
5. The digest changes if and only if the trained model's forecast changes at dp2.

## Non-goals

- Fine-tune, plan cycles, and memory between cycles (the memory board covers that).
- Forecast accuracy against real load. That is the alternative rule the maintainer set aside for now (see Later).
- Changing how often or when LoadML trains.

## Anti-goals

- A board that wins by training less: fewer epochs, lower patience, fewer curriculum passes, a shorter history, a
  smaller network, or skipped validation or diagnostics. The digest catches most of these; the rules forbid them all.
- Household data in git or on the board. The npz is a real household's load, PV, temperature and tariff history. It
  stays in the local fixture, and facts quote figures and digests, never forecast values.
- Disturbing the memory benchmark or its fixture. The predbat-memory board's facts cite them.

## Constraints

- Docker on Apple Silicon (aarch64 Linux containers), as for the memory benchmark.
- `coverage/` and `boards/` are off-limits to board workers, so the benchmark cannot be changed by the board it serves.
- Unit tests for all new code; pre-commit, 100% docstrings, British English.
- The board runs in the public-safe `predbat-devs` scope.

## How the code works today (verified on main 2f584404)

- `LoadMLComponent.run()` is ticked by `ComponentBase.start()`: at seconds 0 with `first=True`, then every 60 s of
  monotonic time. The first tick loads `predbat_ml_history.npz` (`load_database_history`), fetches, and defers initial
  training ("Initial training is required, delaying until component has started"). The next tick, about 60 s later, has
  `should_train` true and runs `_do_training(is_initial=True)` **synchronously** inside `run()`. Then it re-fetches if
  the data went stale (it won't, since the clock is frozen), predicts, publishes, and saves the history npz and the
  model.
- The first tick runs inside `PredBat.initialize()`: `components.start(phase=2)` waits for each component's
  `api_started`. So it finishes before a driver could wrap the component instance. Covering it means patching
  `LoadMLComponent.run` on the class before `PredBat()` is built (reported by the design's builder check; confirm
  while building).
- Initial training is `train_curriculum(..., epochs=100, curriculum_step_days=5, max_intermediate_passes=8, patience=10)`
  over up to `load_ml_database_days` (90) of history.
- `load_database_history` shifts the saved arrays by `now - saved_utc`, so the fixture's T0 must be at or after the
  npz's `saved_utc`.
- `_fetch_load_data` reads `load_today`, `load_power`, `pv_today` and `car_charging_energy` history through
  `minute_data_load` (the database under `db_primary`), the PV forecast, `sensor.<prefix>_temperature`'s `results`, and
  the current state and history of `<prefix>.rates` and `<prefix>.rates_export`. None of it needs a plan cycle first:
  under `db_primary`, the latest states come from the fixture database.
- The memory benchmark's `container_run.sh` already takes `BENCH_DRIVER`, and the wrapper passes it through.
- `export_fixture.py` drops every live `load_ml_*` setting, and the template sets `load_ml_enable: false`.
- The live 90-day npz has all five channels for 90 days and is saved every few minutes. The 7-day memory fixture's T0
  (26 Nov) is before any npz that exists now, so it cannot be reused.

## Approaches

| | A. Training driver in the existing harness | B. Predictor only | C. Inside the memory benchmark's plan cycles |
| --- | --- | --- | --- |
| What runs | real Predbat startup, then the component's own training tick | `LoadPredictor.train_curriculum` on the npz | plan cycles with training in between |
| Covers the component's Python (npz to dicts, fetch, merge) | yes | no | yes |
| Peak RSS is | what an install sees during training | the predictor's alone | blurred by plan cycles |
| Run time | ~5-8 min | ~4-6 min | 10+ min |

**Chosen: A** (the maintainer). B misses component work the board should be free to optimise, and its RSS isn't an
install's. C mixes plan memory and time into the figures.

## Design

### Pieces (all committed except the fixture)

1. **`coverage/load_ml_benchmark [--tracemalloc|--profile] <tree> <out dir>`**: a shell wrapper modelled on
   `coverage/memory_benchmark`. It uses the same `Dockerfile` (and so the same image tag scheme), the same
   `container_run.sh` with `BENCH_DRIVER=/bench/load_ml_driver.py`, and the same copy-the-tree and fresh-scratch-fixture
   steps. Its fixture comes from `$LOAD_ML_BENCHMARK_DATA`, default `coverage/load_ml_benchmark_data`. It passes
   `OPENBLAS_NUM_THREADS=4`, `OMP_NUM_THREADS=4` and `MKL_NUM_THREADS=4`, along with `--cpus 4` and `--memory 3g`. The
   memory benchmark's behaviour must not change: if a shared file needs a change (for example `container_run.sh`
   passing extra driver arguments), it must be default-off, and the memory benchmark's tests keep passing.

   **One run at a time per machine.** The wrapper holds an exclusive lock for the whole container run, so every
   caller queues: workers, `measure.py`, and a person at the terminal. The lock lives at a fixed path, for example
   `/tmp/predbat-load-ml-benchmark.lock`. It is released however the run ends, and a holder that has died doesn't
   block the next run. macOS has no `flock(1)`, so this needs a small Python `fcntl.flock` helper or an equivalent;
   that's the builder's call. The wrapper prints when it is waiting. `result.json` records `queued_seconds`, so a fact
   can show it was never measured while another run was going.
2. **`coverage/memory_benchmark_tools/load_ml_driver.py`**: run in the container.
   - It starts `PredBat()` the way `driver.py` does, and waits for the dummy inverter, solar and Octopus components
     (`wait_ready`), as `driver.py` does. It runs **no plan cycles**.
   - Before building `PredBat()`, it patches `LoadMLComponent.run` on the class with a timing wrapper. The wrapper
     covers every tick up to and including the first one that completes training. It adds up each tick's wall time and
     CPU time, so the idle gap between ticks isn't counted.
   - Before the first LoadML tick starts, it resets the peak by writing `5` to `/proc/self/clear_refs`, which resets
     VmHWM. Startup's own transient peak, before LoadML runs, is left out. If the kernel refuses, it falls back to a
     50 ms VmRSS sampling thread from that point, and the result records which method was used.
   - Recorded:
     - `training_seconds`: the summed wall time of the LoadML ticks.
     - `training_cpu_seconds`: process CPU time over the same ticks, from `time.process_time()`.
     - `training_tick_seconds`: the training tick on its own, as a diagnostic.
     - `rss_before_mb`: VmRSS as the first tick starts.
     - `peak_rss_mb`: VmHWM after the training tick, as an absolute figure, which is what the goal judges.
     - `rss_after_mb`: VmRSS once the training tick has finished.
     - `val_mae` and `epochs_trained`, from the component's predictor.
   - `forecast_digest` is the sha256 of the component's `current_predictions` after the tick, with each value
     rounded to 2 dp (`dp2`) and keys sorted. The driver also writes `forecast.json` (the dp2 values) to the out dir,
     so a worker can see where two forecasts differ. Workers never quote it on the board.
   - `ok` is false if the log has problems (`driver.check_log`, extended with LoadML's own harmless warnings, each
     with its reason), if training did not complete, if the model status after the tick is not `active`, or if the
     forecast is empty. A timeout on waiting for the training tick, defaulting to 30 minutes, is a problem.
   - It writes `result.json` and `modules.txt` (reusing `write_module_report`). `--tracemalloc` writes
     `tracemalloc.txt` and marks the result as profiled, as `driver.py` does. **`--profile`** runs the training tick
     under `cProfile` and writes `profile.txt` (the top functions by cumulative and by total time), marking the result
     as profiled. Workers need that to find where the time goes. `measure.py` refuses profiled results.
   - It reuses `driver.py`'s helpers (`check_log`, `read_status_kb`, `write_module_report`, `wait_ready`,
     `finish_profile`) by importing them, not copying.
3. **The fixture, `coverage/load_ml_benchmark_data/`** (git-ignored, local, never committed). It is built by
   `export_fixture.py --load-ml --days 28 --live <add-on config dir> --out coverage/load_ml_benchmark_data`. With
   `--load-ml` the export:
   - copies the live `predbat_ml_history.npz` into the fixture, read-only from the live side, copied once at the start
     so the database snapshot and the npz are from the same moment;
   - sets T0 to the export time, and refuses if T0 is earlier than the npz's `saved_utc`;
   - uses the template's LoadML settings: `load_ml_enable: true`, `load_ml_source: true`, defaults for the rest. They
     come from a second template, `apps_template_load_ml.yaml`, or from an overlay on the existing template; that is
     the builder's call. The live `load_ml_*` settings stay dropped;
   - never copies `predbat_ml_model.npz`, so the run always does initial training;
   - writes `manifest.json` with an `npz_sha256` and the npz's `saved_utc` as well as the existing fields.

   28 days of database history is my call. It matches `load_ml_max_days_history`, so the fetch part of the cycle reads
   what a real install's does.
4. **`boards/predbat-load-ml/`**: `board.toml`, `measure.py`, `test_measure.py` and `README.md`, modelled on
   `boards/predbat-memory/`. There is also a pre-commit hook for its tests, like `board-predbat-memory-tests`.
5. **`.gitignore`** entries for `coverage/load_ml_benchmark_data/` and `coverage/load_ml_benchmark_runs/`.
6. **Tests:** `test_load_ml_driver.py` (digest, result assembly, `ok` rules, the run wrapper's timing and peak logic
   against fakes), the export's `--load-ml` path in `test_export_fixture.py`, a wrapper test like `test_wrapper.py`'s,
   and the board's `test_measure.py` (the verdict logic and the permission-list checks the memory board has).

### `result.json`

```json
{"ok": true, "training_seconds": 312.4, "training_cpu_seconds": 1180.2, "rss_before_mb": 88.1,
 "peak_rss_mb": 214.7, "rss_after_mb": 130.2, "forecast_digest": "<hex>", "val_mae": 0.0184,
 "epochs_trained": 900, "peak_method": "clear_refs", "problems": []}
```

The figures in the example are invented. Diagnostics (startup seconds, modules loaded) can be added freely.

### The board's goal (`board.toml`)

> Cut the time and memory LoadML's initial training takes. On the LoadML training benchmark
> (coverage/load_ml_benchmark: Predbat standalone in a Linux container with LoadML on, no saved model and a real
> 90-day training history, timing the component's work up to and including its first initial training), lower the
> training wall time by at least 25% and the process's peak RSS over that cycle by at least 25% against main, with
> the trained model's 48 h forecast identical to main's at 2 dp. Peak memory and runtime are what decide whether a
> small device copes with LoadML.

**Done when:** facts show a single branch that meets all of these, measured back to back with main on the same machine:

- `training_seconds` and the absolute `peak_rss_mb` are each at least 25% below main's;
- `forecast_digest` equals main's in every run, `epochs_trained` equals main's, and `val_mae` matches at 4 dp;
- `rss_after_mb` is no more than 10% above main's;
- `./run_all --quick` and `./run_pre_commit` pass.

Then `boards/predbat-load-ml/measure.py` confirms it over 3 rounds, interleaved, using medians.

**Stop rule (in the rules):** the baseline fact reports the share of `training_seconds` spent inside BLAS and NumPy
matrix kernels, from `--profile`. If that share, or later evidence, shows a 25% time cut can't be reached without
changing the floating-point maths, the lead stores a hint saying so, with the figures, and stops the board. It doesn't
spend the intent budget finding out. The maintainer then decides whether to lower the target or to move to the accuracy
rule (see Later).

### The board's rules (the differences from predbat-memory)

- **Baseline first.** The first intent records main's commit, runs the benchmark on main twice (to show the digest is
  stable and to see the timing spread), and maps where the time goes (`--profile`) and the memory goes
  (`--tracemalloc`).
- **Evidence:** one run of main and one of the branch, back to back, for each fact. A worker runs a second pair only if
  the change is under about 5% or the figures look odd. Each fact starts with `Commit:`, `Built on:` and
  `Tree contains:`, then `Time:` and `Peak:` against main, then the digest comparison.
- **Same work, faster.** Never change epochs, patience, the curriculum (window, step, passes), the learning rate,
  the network's size, the history length, the validation holdout, or the diagnostics the stats sensor publishes.
  Never skip work whose output is kept. The forecast digest must match. Any change to the floating-point maths that
  changes it is refuted, whatever the saving.
- **What is in bounds:** copies and temporaries, memory layout and dtypes where the result is bit-identical, dataset
  construction, pure-Python loops around the maths (npz to dicts, merging, the windowed features), redundant passes, and
  work done twice. Running work concurrently counts too, provided the results stay bit-identical. Because the target
  is the absolute peak, cutting the RSS Predbat already holds when LoadML starts also counts. Those changes follow the
  memory board's rule as well: no plan may change.
- **Not in bounds:** the BLAS thread count and other environment variables (the harness fixes those), and anything
  that changes when or how often training runs.
- Worktrees at `~/predbat/batpred/.worktrees/board-loadml-<8>`, branches `board/predbat-load-ml/<8>`, commit prefix
  `board(predbat-load-ml <8>): `, long output under `/tmp/predbat-board/load-ml/<8>/`. The shell rules, permission list
  and safety rules are the memory board's, with the benchmark entries pointing at `coverage/load_ml_benchmark`.
- Limits: 2 open intents, 20 intents in all, 120 claim minutes (a pair of runs takes about 15 minutes), and the lead
  every 20 minutes. Models as for the memory board (lead opus with ideas model fable; workers opus and sonnet).

### `measure.py`

The runner's `[measure]` command. It works like predbat-memory's, adapted:

- It runs `BOARD_CANDIDATE` against `BOARD_BASE` (default main) in temporary worktrees, for 3 interleaved rounds.
- It passes when all of these hold:
  - the median `training_seconds` and the median `peak_rss_mb` are each at least 25% below the base's;
  - the median `rss_after_mb` is no more than 10% above the base's;
  - every run is `ok`;
  - every candidate run has the base's `forecast_digest` and `epochs_trained`, and its `val_mae` matches at 4 dp.
- It is a harness error (exit 2) if any of these happens:
  - the base disagrees with itself on any of the digest, `epochs_trained` or `val_mae`;
  - a result is profiled;
  - a figure is missing or not finite.
- It runs the benchmark through the wrapper, so it takes the same lock and queues behind workers.
- Expect roughly 45 minutes for a confirmation, plus any queueing. The `[measure]` timeout is 180 minutes.
- Worker claims are 120 minutes and worker timeouts 150, which allows for one queued pair of runs.

## Left to the builder

- Whether to wrap `run` or `_do_training`. Wrapping `run` measures the whole tick (fetch, train, predict, publish,
  save), which is the cycle the maintainer asked for, so prefer it. Record `_do_training`'s share as a diagnostic if
  it's cheap.
- Check that the fixture's `load_today`, `pv_today` and `load_power` history is present under the dummy inverter's
  remapped names, so LoadML's fetch reads real data. If the remap does not carry them, fix it in the export.
- Which LoadML log warnings are benign in this offline run (for example "Training with only N days" must not appear,
  since 90 days are loaded), each listed with its reason, as `driver.py` does.
- How the wrapper and `container_run.sh` pass `--profile` and the training timeout. Keep the memory benchmark's
  invocation unchanged.
- Check that the digest is stable across two container runs of main before writing the board's goal. If it isn't
  stable (for example OpenBLAS thread scheduling), stop and report it; don't loosen the rule.

## Later (not now)

- The accuracy rule: train on history up to T0 − 48 h, score the forecast against the 48 h of real load that
  followed, and accept a candidate whose error is within main's spread across seeds. That would open up changes to the
  floating-point maths. The maintainer chose dp2 to start with.
- A fine-tune benchmark from a saved model, once someone works out why fine-tune barely moves the forecast. The spike's
  perturbation changed nothing at all, which may mean early stopping restores the loaded weights.
