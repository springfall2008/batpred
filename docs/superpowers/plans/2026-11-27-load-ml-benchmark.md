# LoadML Training Benchmark and predbat-load-ml Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `coverage/load_ml_benchmark <tree> <out dir>`, which times LoadML's first initial training in the memory benchmark's offline frozen-clock container and digests the resulting forecast, plus the `boards/predbat-load-ml` board that cuts that time and peak RSS by 25% each with the forecast unchanged at dp2.

**Architecture:** `export_fixture.py --load-ml` writes a second git-ignored fixture (28 days of database plus the live `predbat_ml_history.npz`, with LoadML switched on). A new shell wrapper takes a machine-wide lock, then runs the memory benchmark's own image and `container_run.sh` with `BENCH_DRIVER=/bench/load_ml_driver.py`. The driver patches `LoadMLComponent.run` on the class before Predbat starts. It meters every tick up to and including the first one that trains, and writes `result.json`. The board files mirror `boards/predbat-memory/`.

**Tech Stack:** Python 3.13 stdlib (`unittest`, `fcntl`, `cProfile`, `hashlib`), bash, Docker (the existing pinned `python:3.13-slim-bookworm` image), NumPy/OpenBLAS inside the container.

**Spec:** `docs/superpowers/specs/2026-11-27-load-ml-benchmark-design.md`. Read it first; this plan records only the decisions it leaves open.

## Global Constraints

- Contract: `coverage/load_ml_benchmark [--tracemalloc|--profile] <tree> <out dir>`, run from the main checkout. It exits 0 when `result.json` was written. `result.json` has `ok` (bool), `training_seconds`, `training_cpu_seconds`, `training_tick_seconds`, `rss_before_mb`, `peak_rss_mb`, `rss_after_mb` (floats), `forecast_digest` (str), `val_mae` (float or null), `epochs_trained` (int), `peak_method` (`"clear_refs"` or `"sampler"`), `queued_seconds` (float) and `problems` (list).
- The memory benchmark's behaviour, invocation and tests are unchanged. Any shared-file change is default-off.
- Container: `--network none --cpus 4 --memory 3g`, with `OPENBLAS_NUM_THREADS=4`, `OMP_NUM_THREADS=4` and `MKL_NUM_THREADS=4`. The wall clock is frozen at the fixture's T0.
- One benchmark run per machine at a time. The lock is `${LOAD_ML_BENCHMARK_LOCK:-/tmp/predbat-load-ml-benchmark.lock}`.
- The fixture lives at `${LOAD_ML_BENCHMARK_DATA:-coverage/load_ml_benchmark_data}`. It is git-ignored and never committed. It has no `predbat_ml_model.npz`.
- `forecast_digest` = sha256 of `json.dumps({str(minute): dp2(value)}, sort_keys=True)` over the component's `current_predictions`, where `dp2` is `round(value, 2)`.
- Goal figures, copied verbatim from the spec. Pass when all of these hold:
  - `training_seconds` and the absolute `peak_rss_mb` are each at least 25% below main's;
  - `rss_after_mb` is no more than 10% above main's;
  - the digest and `epochs_trained` are equal to main's, and `val_mae` is equal at 4 dp.

  `measure.py` uses 3 interleaved rounds and medians, with a `[measure]` timeout of 180 min. Claims are 120 min, the worker timeout is 150 min, there are 2 open intents and 20 intents in all.
- Nothing from the live system goes into git, tests or board memories. Tests use synthetic data only.
- Repo rules:
  - 100% docstrings (`interrogate`), British English (cspell), line length 250 and `lower_case_with_underscores`.
  - The copyright header comes first in every new file.
  - Run GitNexus `impact` before editing an existing symbol, and `detect_changes()` before each commit.
  - Pre-commit runs from the repo root, after `git add` (it skips untracked files).
- Run unit tests as `python3 -m unittest discover -s <dir> -v > <scratch>/<name>.log 2>&1` and read the log. Never pipe test output straight into grep.

## Review Focus

- **A shared-file change alters the memory benchmark.** `container_run.sh` gains an optional driver-arguments variable, and the memory benchmark's `docker run` line must be byte-for-byte the same when it is unset. This is pinned by a test in Task 1, which greps the memory wrapper for no new `-e` flags, and by the memory benchmark's existing `test_wrapper.py` passing.
- **The compressed npz trips the export's byte scans.** `find_leaks` and `find_personal_data` search raw file bytes, and deflated float arrays can match a postcode or token regex by chance. The npz must be left out of both byte scans and its `metadata_json` text scanned instead. This is pinned in Task 2.
- **Training fails or is refused.** If val_mae is over the threshold, the status is not `active`, or `initial_training_done` never becomes true, the run must end with `ok: false` and a named problem, never a hang. The training-wait timeout (default 1800 s) is pinned in Task 3.
- **A dead lock holder.** Killing a run (or the board runner killing a timed-out worker) must not block the next run. `fcntl.flock` is released by the kernel when the process dies. This is pinned in Task 1 by a test that kills a holder.
- **The digest varies between two runs of main.** Thread scheduling, dict order or the clock could cause it. The driver sorts keys and rounds the values. Task 4 runs main twice and stops if the digests differ.

---

### Task 1: Benchmark lock and wrapper

**Files:**
- Create: `coverage/memory_benchmark_tools/bench_lock.py`, `coverage/memory_benchmark_tools/test_bench_lock.py`, `coverage/load_ml_benchmark`, `coverage/memory_benchmark_tools/test_load_ml_wrapper.py`
- Modify: `coverage/memory_benchmark_tools/container_run.sh` (add `${BENCH_DRIVER_ARGS:-}` after the existing driver arguments; nothing else), `.gitignore` (add `coverage/load_ml_benchmark_data/` and `coverage/load_ml_benchmark_runs/` under the memory benchmark's block)

**Interfaces:**
- Produces:
  - `bench_lock.held(path: str, notify=print) -> ContextManager[float]`. It takes an exclusive `fcntl.flock` on `path`, creating the file if needed. If the lock is busy it calls `notify("Waiting for the benchmark lock at <path> ...")` once, then blocks. It yields the seconds it waited.
  - `bench_lock.main(argv: list[str]) -> int`. Usage: `bench_lock.py <lock path> -- <command...>`. It runs the command under `held()` with `BENCH_QUEUED_SECONDS=<waited, 1 dp>` added to its environment, and returns the command's exit code. It returns 2 on a usage error.
  - `coverage/load_ml_benchmark [--tracemalloc|--profile] <tree> <out dir>`. It mirrors `coverage/memory_benchmark`'s checks, messages, output clean-up (which also removes `profile.txt` and `forecast.json`), image naming, tree copy and scratch fixture.
    - `docker run` runs under `python3 "$TOOLS/bench_lock.py" "$LOCK" -- docker run ...`.
    - It adds `-e BENCH_DRIVER=/bench/load_ml_driver.py -e BENCH_QUEUED_SECONDS -e OPENBLAS_NUM_THREADS=4 -e OMP_NUM_THREADS=4 -e MKL_NUM_THREADS=4`.
    - `--profile` sets `-e BENCH_DRIVER_ARGS=--profile`.
    - `BENCH_TIMEOUT` defaults to 3600 here.
    - With no fixture it prints the `export_fixture.py --load-ml --days 28` command.

- [ ] **Step 1: Write failing tests.**
  - `test_bench_lock.py`:
    - `test_held_yields_zero_when_free`;
    - `test_second_holder_waits_and_is_told` (a child process holds the lock for 1 s; `held` notifies once and yields at least 0.9);
    - `test_a_killed_holder_does_not_block` (start a child that takes the lock and sleeps 60 s, SIGKILL it, then `held` yields under 1 s);
    - `test_main_passes_queued_seconds_and_exit_code` (`main([lock, "--", sys.executable, "-c", "import os,sys; sys.exit(0 if os.environ['BENCH_QUEUED_SECONDS']=='0.0' else 5)"])` returns 0, and a command exiting 3 returns 3);
    - `test_main_usage_error_returns_2`.
  - `test_load_ml_wrapper.py`, modelled on `test_wrapper.py` with `LOAD_ML_BENCHMARK_DATA`:
    - `test_without_a_fixture_exits_2_and_names_the_load_ml_export`, whose output contains `--load-ml`;
    - `test_rejects_a_missing_tree`;
    - `test_needs_two_arguments`;
    - `test_takes_profile_or_tracemalloc_before_the_tree`;
    - `test_clears_every_output_of_an_earlier_run`, with a fake `docker` on PATH as `test_wrapper.py` does; this includes `profile.txt` and `forecast.json`;
    - `test_runs_docker_under_the_lock_with_blas_pinned`: the fake `docker` records its argv and env. Assert that `BENCH_DRIVER=/bench/load_ml_driver.py` and `OPENBLAS_NUM_THREADS=4` are in the argv, and that `BENCH_QUEUED_SECONDS` is set in the fake's environment;
    - `test_memory_benchmark_is_unchanged`: `coverage/memory_benchmark` contains neither `bench_lock` nor `BENCH_DRIVER_ARGS`.
- [ ] **Step 2: Run both test files and confirm they fail.**
- [ ] **Step 3: Implement** `bench_lock.py`, the `container_run.sh` passthrough, the wrapper (`chmod +x`) and the `.gitignore` lines.
- [ ] **Step 4: Run `coverage/memory_benchmark_tools` in full** (including the memory benchmark's existing `test_wrapper.py`) and confirm everything passes.
- [ ] **Step 5: `git add`, run pre-commit from the repo root, then commit** `feat(boards): a locked wrapper for the LoadML training benchmark`.

---

### Task 2: `export_fixture.py --load-ml`

**Files:**
- Modify: `coverage/memory_benchmark_tools/export_fixture.py` (`main`, `export`, `_export_from`, `build_apps_yaml`), `coverage/memory_benchmark_tools/scrub.py` (`find_leaks`, `find_personal_data`, `_files`)
- Test: `coverage/memory_benchmark_tools/test_export_fixture.py`, `coverage/memory_benchmark_tools/test_scrub.py`

**Interfaces:**
- Produces:
  - `ML_HISTORY = "predbat_ml_history.npz"`.
  - `LOAD_ML_SETTINGS = {"load_ml_enable": True, "load_ml_source": True}`.
  - `build_apps_yaml(template, live_args, forecast_config, totals, sizing, load_ml=False)` applies `LOAD_ML_SETTINGS` after the template when `load_ml` is set.
  - `copy_ml_history(live: Path, out: Path, t0: datetime) -> dict` copies `live/ML_HISTORY` to `out/ML_HISTORY` and returns `{"npz_sha256": str, "npz_saved_utc": str}`. It raises `ValueError` naming both times when `saved_utc > t0`, and `FileNotFoundError` when the file is missing.
  - `export(..., load_ml=False)` copies the npz first, before the DB snapshot, so the two are from the same moment, and merges its dict into the manifest. It never writes `predbat_ml_model.npz`.
  - The CLI gets `--load-ml`. The CLI default `--days` stays 7; the docs and the wrapper message say to pass `--days 28`.
  - The scrub byte scans skip `*.npz` files. `export` checks the npz's `metadata_json` string with `find_leaks`/`personal_data_kinds` logic, through a new `scrub.scan_text(text, secrets, tokens) -> list[str]` (the kinds and leaks found, by kind only).

- [ ] **Step 1: Run GitNexus `impact` upstream on `build_apps_yaml`, `export`, `_export_from`, `find_leaks` and `find_personal_data`.** Report the blast radius.
- [ ] **Step 2: Write failing tests** (synthetic npz built with `np.savez_compressed` and a metadata dict like the component's).
  - `test_build_apps_yaml_switches_load_ml_on_only_when_asked`: the default keeps `load_ml_enable: False`; `load_ml=True` gives `True`/`True`.
  - `test_copy_ml_history_records_sha_and_saved_time`.
  - `test_copy_ml_history_refuses_a_history_saved_after_t0`.
  - `test_export_fixture_cli_with_load_ml_end_to_end`: extend the existing synthetic end-to-end. The fixture has the npz and no model file, the manifest has `npz_sha256` and `npz_saved_utc`, and `apps.yaml` has LoadML on.
  - `test_byte_scans_skip_npz_files`: an `.npz` whose bytes contain a secret is not reported by `find_leaks`, while a `.json` holding the same secret is.
  - `test_export_refuses_a_secret_in_the_npz_metadata`.
- [ ] **Step 3: Run them and confirm they fail.**
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run the whole tools suite and confirm it passes**, so the memory export's behaviour is unchanged.
- [ ] **Step 6: Run `detect_changes()`, `git add`, run pre-commit, then commit** `feat(boards): export a LoadML training fixture with the live npz history`.

---

### Task 3: The training driver

**Files:**
- Create: `coverage/memory_benchmark_tools/load_ml_driver.py`, `coverage/memory_benchmark_tools/test_load_ml_driver.py`

**Interfaces:**
- Consumes, from `driver.py` by import (do not copy): `check_log`, `ALLOWED_WARNINGS`, `BENIGN_WARNINGS`, `read_status_kb`, `write_module_report`, `wait_ready`, `finish_profile`, `READY_COMPONENTS`.
- Produces, in `load_ml_driver.py`:
  - `forecast_digest(predictions: dict) -> str` and `dp2_forecast(predictions: dict) -> dict[str, float]`.
  - `LOAD_ML_BENIGN_WARNINGS: dict[str, str]`, the substring and the reason. It starts empty; Task 4 fills it from a real run.
  - `check_load_ml_log(text) -> list[str]`, which is `check_log(text, ALLOWED_WARNINGS, {**BENIGN_WARNINGS, **LOAD_ML_BENIGN_WARNINGS})`.
  - `reset_peak(clear_refs="/proc/self/clear_refs") -> bool`, which writes `"5"` and returns False on `OSError`.
  - `class PeakSampler(read_rss, interval=0.05)`, with `start()`, `stop() -> int` (peak kB) and a daemon thread.
  - `class TickMeter(wall=time.monotonic, cpu=time.process_time, read_rss=..., reset=reset_peak, read_hwm=..., sampler_factory=PeakSampler, profiler=None)`:
    - `begin()`: on the first call it records `rss_before_kb`, resets the peak (or starts the sampler, and sets `peak_method`), then starts the tick's wall and CPU clocks and enables `profiler` if one is set.
    - `end(trained: bool)`: adds to `wall_seconds` and `cpu_seconds`. When `trained` it sets `training_tick_seconds`, `peak_kb`, `rss_after_kb` and `done = True`, and sets the `finished` `threading.Event`.
    - `begin`/`end` are no-ops once `done`.
  - `instrument(component_class, meter) -> Callable[[], None]`. It replaces `component_class.run` with `async def run(self, seconds, first)`, which calls `meter.begin()`, awaits the original, then calls `meter.end(trained=bool(self.initial_training_done))` in a `finally`. It returns a function that restores the original.
  - `build_result(meter, component, problems: list[str], queued_seconds: float) -> dict`. This gives the contract fields, with MB to 1 dp, seconds to 2 dp, and `val_mae` from `component.predictor.validation_mae`. `ok` is false, with a problem added, when any of these is true: problems exist; the meter is not `done` ("training never completed"); `component.model_status != "active"`; or the forecast is empty.
  - `run(out: Path, timeout: float, profile: bool) -> dict` instruments `load_ml_component.LoadMLComponent` before `predbat.PredBat()` is built.
    - It calls `initialize()`, then `wait_ready`, then waits on `meter.finished` up to `timeout`; a timeout adds "training did not finish within <n> s".
    - It writes `forecast.json` (the dp2 forecast) and `modules.txt`.
    - With profile, it writes `profile.txt`: the top 40 by `cumulative` and by `tottime`, from `pstats`. It sets `result["profiled"] = True`.
    - Then it calls `finish_profile`.
  - `main(argv)` accepts `--out`, `--cycles` (accepted and ignored: `container_run.sh` always passes it), `--timeout` (default 1800), `--tracemalloc` and `--profile`. `--tracemalloc` calls `tracemalloc.start(1)` before Predbat is imported, as `driver.main` does. It writes `result.json` (on an exception, `ok: false` with zero figures and an empty digest, as `driver.main` does) and exits with `os._exit(0)`. `queued_seconds` comes from `float(os.environ.get("BENCH_QUEUED_SECONDS", 0))`.

- [ ] **Step 1: Write failing tests** (no Predbat import; fake clocks and a fake component class).
  - `test_forecast_digest_rounds_to_dp2_and_ignores_key_order`: two dicts differing by 0.001 in one value (both rounding to the same 2 dp), in different insertion orders, give the same digest; a 0.01 change gives a different one.
  - `test_tick_meter_sums_ticks_and_skips_the_gap`: fake wall 0→10 (tick 1), 70→370 (tick 2, trained). The result is `wall_seconds == 310` and `training_tick_seconds == 300`.
  - `test_tick_meter_resets_the_peak_once_before_the_first_tick`.
  - `test_tick_meter_falls_back_to_the_sampler_when_reset_fails`: `peak_method == "sampler"`.
  - `test_tick_meter_ignores_ticks_after_training`.
  - `test_instrument_wraps_and_restores_run`: an `asyncio.run` of a fake component's `run` passes through its return value and records the tick. `restore()` puts back the original, and `end` still runs when `run` raises.
  - `test_build_result_ok_only_when_trained_active_and_forecast_present`: four cases, each giving a named problem.
  - `test_check_load_ml_log_reuses_the_memory_rules`: an `Error:` line is flagged, and a listed Octopus warning is not.
  - `test_main_accepts_cycles_profile_tracemalloc_and_timeout`: `parse_args`.
  - `test_reset_peak_returns_false_when_clear_refs_is_missing`: point it at a non-existent path.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement** `load_ml_driver.py`.
- [ ] **Step 4: Run the tools suite and confirm it passes.**
- [ ] **Step 5: `git add`, run pre-commit, then commit** `feat(boards): the LoadML training benchmark driver`.

---

### Task 4: Fixture, first real runs and baseline check

Tasks 1-3 must be merged into the branch first. This task changes only benign-warning entries and docs.

**Files:**
- Modify: `coverage/memory_benchmark_tools/load_ml_driver.py` (`LOAD_ML_BENIGN_WARNINGS`, only as the real log requires, each entry with its reason) and `coverage/memory_benchmark_tools/test_load_ml_driver.py`
- Record (not committed): the figures from Steps 3-4 under `coverage/load_ml_benchmark_runs/`, for Task 5's README

- [ ] **Step 1: Build the fixture** from the main checkout (read-only on the live side):
  `python3 coverage/memory_benchmark_tools/export_fixture.py --load-ml --days 28 --live <add-on config dir> --out coverage/load_ml_benchmark_data`
  Expected: `Fixture written to ...`, with no leak or personal-data refusal. Never write the real live path into a committed file.
- [ ] **Step 2: Check the training inputs.** Confirm the fixture's `apps.yaml` points `load_today`, `pv_today` and `load_power` at entities with 28 days of rows in the fixture DB. Query the DB with `sqlite3`: count the rows per entity. If any is missing, fix `fixture_db.build_remap` under Task 2's TDD steps, and say so in the report.
- [ ] **Step 3: Run main twice:** `coverage/load_ml_benchmark <main checkout> coverage/load_ml_benchmark_runs/main-1`, then `main-2`. Expected: both have `ok: true`, the same `forecast_digest`, `epochs_trained` and `val_mae`, and `training_seconds` within about 5% of each other. Read `predbat.log` for warnings. For each harmless one, add a `LOAD_ML_BENIGN_WARNINGS` entry with its reason under TDD (a test line per entry, as `test_benign_warnings_each_carry_a_reason` does), and re-run. **If the digests differ, stop and report the two runs: don't loosen the rule.**
- [ ] **Step 4: Run once with `--profile`.** Record the share of `training_seconds` in NumPy/BLAS kernels (`numpy` and `_multiarray_umath` frames with high `tottime`) for the README. If that share is over 75%, flag it to the maintainer before Task 5's board file is finalised (spec stop rule).
- [ ] **Step 5: Commit** any benign-warning entries: `fix(boards): name LoadML's harmless offline warnings in the training benchmark`.

---

### Task 5: The predbat-load-ml board

**Files:**
- Create: `boards/predbat-load-ml/board.toml`, `boards/predbat-load-ml/measure.py`, `boards/predbat-load-ml/test_measure.py`, `boards/predbat-load-ml/README.md`
- Modify: `.pre-commit-config.yaml` (hook `board-predbat-load-ml-tests` after `board-predbat-memory-tests`, entry `python -m unittest discover -s boards/predbat-load-ml`, `files: ^boards/predbat-load-ml/`), `.cspell/custom-dictionary-workspace.txt` (as needed)

**Interfaces:**
- Consumes: the Task 1 wrapper contract and the `result.json` fields from the Global Constraints.
- Produces, in `measure.py`:
  - `TIME_CUT = 0.25`, `PEAK_CUT = 0.25`, `AFTER_RISE = 0.10`, `ROUNDS = 3` and `BENCHMARK = Path("coverage/load_ml_benchmark")`.
  - `load_result(path) -> dict`. It checks the types of every contract field. A non-finite or negative figure is a `HarnessError`, and so is `profiled` or `tracemalloc`.
  - `verdict(base_runs, candidate_runs) -> (bool, list[str])`. A base that disagrees with itself on the digest, `epochs_trained` or 4 dp `val_mae` raises `HarnessError`.
  - `main() -> int` (0 pass, 1 fail, 2 harness error), with the same worktree handling as predbat-memory's `measure.py`.

  `measure.py` is self-contained: copy predbat-memory's `_git`, `_run_benchmark` and `main` shape, don't import across boards.

- [ ] **Step 1: Write failing tests** in `test_measure.py`.
  - `TestVerdict`:
    - `test_passes_with_both_cuts_and_same_training`;
    - `test_fails_when_only_time_is_cut`;
    - `test_fails_when_only_peak_is_cut`;
    - `test_fails_when_the_digest_differs_whatever_the_saving`;
    - `test_fails_when_epochs_differ`;
    - `test_val_mae_compares_at_four_dp` (0.01844 against 0.01843 fails; 0.018432 against 0.018434 passes);
    - `test_retained_rss_up_to_ten_percent_higher_passes`;
    - `test_retained_rss_more_than_ten_percent_higher_fails`;
    - `test_a_run_not_ok_fails`;
    - `test_uses_medians`;
    - `test_a_base_disagreeing_with_itself_is_a_harness_error`.
  - `TestLoadResult`: the predbat-memory set, adapted to these fields, plus `test_rejects_a_cprofile_run`.
  - `TestMain`: missing candidate, and missing benchmark.
  - `TestBoardFile`: copy predbat-memory's matcher helpers and checks, with paths changed to `board-loadml-*`, `coverage/load_ml_benchmark` and `/tmp/predbat-board/load-ml/`. Add:
    - `test_the_benchmark_and_its_profile_mode_are_allowed`;
    - `test_the_rules_forbid_training_less` (the rules text names epochs, patience, curriculum, learning rate, network size, history length, holdout and diagnostics);
    - `test_the_rules_carry_the_stop_rule`;
    - `test_limits_match_the_spec` (`max_open_intents == 2`, `max_intents == 20`, `claim_minutes == 120`, `[measure] timeout_minutes == 180`, `worker_timeout_minutes == 150`).
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Write `board.toml`.**
  - The goal and done-when are copied from the spec's "The board's goal" section.
  - The rules are predbat-memory's rules, rewritten with the spec's "The board's rules" differences: the baseline intent, one-pair evidence, same work faster, in and out of bounds, the stop rule, and the paths and prefixes. The Evidence header lines are `Commit:`, `Built on:`, `Tree contains:`, `Time: <s> against main <s> (<%>)`, `Peak: <MB> against main <MB> (<%>)`, `After (limit +10%): ...`, and `Training: digest/epochs/val_mae equal|differ`. The rules quote the fixture path only as `coverage/load_ml_benchmark_data`.
  - `[lead]` is opus, every 20 min, with ideas_model `fable`. The workers are opus and sonnet.
  - `[permissions]` is predbat-memory's list with the benchmark and worktree patterns swapped.
- [ ] **Step 4: Write `measure.py` and `README.md`.** The README is modelled on predbat-memory's. It covers how the board runs, the fixture command (with the `--days 28 --load-ml` flags and a placeholder `<add-on config dir>`), Task 4's baseline figures (machine, two runs of main, profile share) and the lock.
- [ ] **Step 5: Run `boards/predbat-load-ml` tests and the tools suite, and confirm they pass.** Then run `~/agent-memory-supabase/runner/board-runner status boards/predbat-load-ml/board.toml`; expect it to load the file without error and report no state yet.
- [ ] **Step 6: Run a dry measurement:** `BOARD_CANDIDATE=main python3 boards/predbat-load-ml/measure.py > <scratch>/measure-main.log 2>&1` from the main checkout. Expect exit 1 (a clean fail with no cut), all digests equal, and no HARNESS ERROR.
- [ ] **Step 7: `git add`, run `./run_pre_commit` from `coverage/`, then commit** `feat(boards): the predbat-load-ml board for LoadML training time and peak memory`.

---

### Task 6: Wrap-up

- [ ] **Step 1: Run `detect_changes({scope: "compare", base_ref: "main"})`.** Confirm that only the files above are changed, and no `apps/predbat` symbol.
- [ ] **Step 2: Update the shared-memory status memory `ba87936c`** with the branch head, the baseline figures and the next action: "PR, then the maintainer starts the board". Keep `status:open` until a PR exists.
- [ ] **Step 3: Report to the maintainer.** Include the baseline (two runs of main), the profile share, the dry-measure output, and whether the stop rule looks likely to fire. Don't push or open a PR unless asked.
