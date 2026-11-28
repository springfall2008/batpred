# Board predbat-load-ml

A shared-goal board that cuts the time and peak memory of LoadML's initial training, with the trained model unchanged.
It runs on the board runner and the blackboard skill from agent-memory-supabase (`runner/` and `skills/blackboard/`
there). The board's memories live in the `predbat-devs` scope, tagged `board:predbat-load-ml`. The design is
`docs/superpowers/specs/2026-11-27-load-ml-benchmark-design.md`.

| File              | What it is                                                                         |
| ----------------- | ---------------------------------------------------------------------------------- |
| `board.toml`      | The board file: goal, done-when, rules, intent limit, models and permissions       |
| `measure.py`      | The runner's `[measure]` command, which confirms a candidate before the board ends |
| `test_measure.py` | Tests for `measure.py` and the board file's permissions, run in pre-commit         |

## How the board runs

1. **Baseline.** The lead's first round writes one intent only. Its worker records main's commit, runs the benchmark
   on main twice, and profiles where the time goes (`--profile`) and the memory goes (`--tracemalloc`). The fact states
   how much of the training time is inside NumPy and BLAS kernels, which this board cannot reorder.
2. **Stop rule.** If that share shows a 25% time cut is out of reach while the arithmetic stays bit-identical, the lead
   says so in a hint, with the figures, and stops the board. The maintainer then decides whether to lower the target or
   move to an accuracy-based rule.
3. **Savings.** Otherwise the lead plans intents from the profile, two open at a time. Each worker makes its own
   worktree and branch (`board/predbat-load-ml/<intent short id>`), makes one commit per change, and compares one run of
   main with one run of its branch. Every commit message starts `board(predbat-load-ml <intent short id>): <what it
   saves>`, and every fact starts with `Commit:`, `Built on:`, `Tree contains:`, then the time, peak and after figures
   against main and whether the trained model matched.
4. **Finishing.** When a commit's facts meet the done-when, the lead proposes it, and the runner runs `measure.py`
   against main: 3 interleaved rounds, medians. A pass ends the board. A fail goes back to the lead. The board also stops
   at 20 intents.

## The rule: the same model, faster

The trained model must be the one main trains: the 48 h forecast identical at 2 dp (`forecast_digest`), the same
`epochs_trained` and the same `val_mae` at 4 dp. LoadML's initial training is chaotic, so in practice this means the
floating-point maths must stay bit-identical. Scaling the learning rate by 1.000001 changed 572 of 576 forecast steps at
2 dp and the 48 h total by 6%. Copies, layout, dataset construction, pure-Python loops, duplicated work and Predbat's own
baseline memory are in bounds. Fewer epochs, a smaller network, a shorter history and reordered maths are not.

## The benchmark

`coverage/load_ml_benchmark [--tracemalloc|--profile] <tree> <out dir>`, run from the main checkout.

- It runs Predbat from `<tree>`'s `apps/predbat` in the memory benchmark's container: aarch64 Linux, no network, 4
  CPUs, 3 GB, the wall clock frozen at the fixture's T0, and `OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS` and
  `MKL_NUM_THREADS` set to 4. LoadML is on with no saved model, so it does its initial training from the fixture's
  90-day history.
- The driver (`coverage/memory_benchmark_tools/load_ml_driver.py`) patches `LoadMLComponent.run` before Predbat starts.
  It meters both LoadML ticks up to the end of the first training: the npz load and fetch, then the training tick
  itself. The idle minute between them is not counted. The peak is reset with `/proc/self/clear_refs` as the first tick
  starts, so startup's own transient peak is left out.
- `result.json` holds:
    - `ok`;
    - `training_seconds` (the summed ticks) and `training_cpu_seconds`;
    - `training_tick_seconds` (the training tick alone);
    - `rss_before_mb`, `peak_rss_mb` (absolute) and `rss_after_mb`;
    - `forecast_digest`, `val_mae` and `epochs_trained`;
    - `peak_method` and `queued_seconds`.

  `forecast.json` is the forecast at 2 dp, for finding where two runs differ. It is household data: never quote it on
  the board.
- **One run at a time.** The wrapper holds an `fcntl` lock (`/tmp/predbat-load-ml-benchmark.lock`, or
  `$LOAD_ML_BENCHMARK_LOCK`) for the whole container run. A caller that has to wait is told so. The kernel drops the
  lock if the holder is killed.
- `--profile` writes `profile.txt`, cProfile's top functions over the LoadML ticks. `--tracemalloc` writes
  `tracemalloc.txt`. Both mark the result as profiled, and `measure.py` refuses it.

### The fixture

The benchmark's input is a local fixture in `coverage/load_ml_benchmark_data/`. It is git-ignored and never committed,
because it is built from a real install. Build it from the live Predbat add-on config directory with:

```sh
python3 coverage/memory_benchmark_tools/export_fixture.py --load-ml --days 28 --live <add-on config dir> --out coverage/load_ml_benchmark_data
```

It is the memory benchmark's export, with the same scrubbing and leak checks, plus three changes:

- it holds 28 days of database history, which is what LoadML fetches;
- it copies the live `predbat_ml_history.npz` just before the database snapshot, and refuses one saved after T0;
- it switches `load_ml_enable` and `load_ml_source` on.

The export never copies a trained model. The byte scans skip the compressed npz, whose deflated arrays can match a
pattern by chance, and scan its metadata text instead. `manifest.json` records the npz's sha256 and saved time.

### Measured (27 Nov 2026, Apple M4 Max, Docker aarch64, main 2f584404)

| Run | training_seconds | CPU seconds | RSS before | Peak | After | forecast_digest, epochs, val_mae |
| --- | --- | --- | --- | --- | --- | --- |
| main 1 | 411.8 | 1631 | 88.2 MB | 216.3 MB | 199.6 MB | `dc8f2e2b...`, 900, 0.018144 |
| main 2 | 428.4 | 1697 | 90.2 MB | 219.6 MB | 201.4 MB | the same |

Two runs of main trained the same model, and their times were 4% apart. LoadML keeps about 110 MB after training: a
saving there counts as well, though only the peak is part of the goal.

A `--profile` run (425.6 s, its figures not evidence) showed where the time goes:

| Function | Own time | Kind |
| --- | --- | --- |
| `LoadPredictor._adam_update` | 152 s (228 s with what it calls) | element-wise optimiser maths over every weight, every batch |
| `numpy.nan_to_num` with `isposinf`/`isneginf` | 75 s cumulative | gradient sanitising |
| `LoadPredictor._forward` | 83 s | matrix products, ReLU, dropout masks |
| `LoadPredictor._backward` | 76 s | matrix products |
| `numpy.clip` | 16 s | gradient clipping |

The BLAS matrix products the rule forbids reordering are inside `_forward` and `_backward`, at most about 37% of the
time. The element-wise optimiser step and gradient handling are about 54%, and they can be made cheaper without changing
a bit, so the stop rule did not fire at the baseline.

## Running it

```sh
cd ~/predbat/batpred
board-runner run boards/predbat-load-ml/board.toml
```

The runner runs `measure.py` from the main checkout, so this directory, the benchmark and the fixture must be on the
checked-out branch there. `board-runner status boards/predbat-load-ml/board.toml` shows where it got to.
