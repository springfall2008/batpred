# Memory Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `coverage/memory_benchmark <tree> <out dir>`, the frozen-clock, no-network, db_primary benchmark that the `predbat-memory` board needs, plus the export script that builds its local fixture from the maintainer's live Predbat.

**Architecture:** An export script (run by the maintainer, read-only against the live add-on config share) writes a gitignored fixture: a 7-day `predbat.db`, a scrubbed and re-keyed `cache/`, a generated `apps.yaml` and a manifest holding T0. A shell wrapper runs a pinned Linux image with `--network none`, builds the kernel from `<tree>` inside it, freezes the wall clock at T0 with libfaketime, and runs a Python driver that starts the real `PredBat`, waits for the components to publish once, runs N forced plan cycles, samples RSS and writes `result.json`.

**Tech Stack:** Python 3.13 (stdlib `unittest`, `sqlite3`, `yaml`), bash, Docker (`python:3.13-slim-bookworm`, libfaketime), the repo's own `apps/predbat` modules.

**Spec:** `docs/superpowers/specs/2026-11-26-memory-benchmark-design.md` (read it first; this plan only records the decisions it leaves open).

## Global Constraints

- Contract (do not change): `coverage/memory_benchmark <tree> <out dir>`, run from the main checkout; exit 0 when the run completed; writes `<out dir>/result.json` `{"ok": bool, "steady_rss_mb": float, "peak_rss_mb": float, "plan_digest": str}` and `<out dir>/predbat.log`. `boards/predbat-memory/measure.py` already consumes this and turns a non-zero exit into a harness error.
- Linux container, fixed pinned image; macOS RSS is not evidence. The run has `--network none`.
- Wall clock frozen at T0 (the fixture's extraction time) for the whole process; `time.monotonic()` and real sleeps keep running.
- MCP, Load ML and GivTCP off; the forecast (forecast.solar) and Octopus components on, served from seeded cache entries; web server at its default (on).
- Nothing from the live system is committed: no entity ids, serials, account numbers, tokens, coordinates or values in git, logs under git or board memories. Committed tests use synthetic names only. The fixture directory is gitignored.
- The live DB and live `apps.yaml` are opened read-only (SQLite `mode=ro`).
- `ok` is false on any `Error:` log line, a stale-binary Python fallback, a `SolarAPI: Fetching` line, a missing result, or a skipped cycle; a short named list of Octopus `Warn:` lines is allowed.
- Repo rules: unit tests for all new code; 100% docstrings (`interrogate`); British English (cspell); line length 250; `lower_case_with_underscores`; any product-code edit starts with a GitNexus `impact` run on the symbol, reported to the maintainer, and `detect_changes()` before each commit.
- Run tests as `python3 -m unittest discover -s coverage/memory_benchmark_tools -v > /tmp/claude-501/<name>.log 2>&1` and read the log file. Run pre-commit from the repo root, with the files `git add`ed first (it skips untracked files).

## Review Focus

- A leak: a real account number, meter serial, token, postcode or coordinate surviving anywhere in the fixture or in a log. Pinned by `find_leaks` over the whole fixture (Task 5) and by a log scan (Task 8).
- A counter that jumps at T0: the dummy's lifetime counters not matching the fixture's last value. Pinned in Tasks 2 and 3.
- A run that quietly reaches for the network and is served by a stale or missing cache entry: `SolarAPI: Fetching` or an unlisted Octopus request must flip `ok`. Pinned in Task 7.
- A cache entry that is not fresh under the frozen clock, so the component refetches. Pinned in Task 4 (`created` equals T0) and Task 8 (no refetch lines).
- A digest that varies between runs or days (clock, threads, unordered dict output), or that fails to change when a plan does. Pinned in Tasks 7 and 8.

---

### Task 1: Tools package, scrub helpers, test hook and ignores

**Files:**
- Create: `coverage/memory_benchmark_tools/scrub.py`, `coverage/memory_benchmark_tools/test_scrub.py`
- Modify: `.pre-commit-config.yaml` (new hook after `triage-replay-tests`), `.gitignore`, `.cspell/custom-dictionary-workspace.txt` (as the spell check demands)

**Interfaces:**
- Produces (in `scrub.py`):
  - `FAKE_ACCOUNT = "A-BENCH0001"`, `FAKE_OCTOPUS_KEY = "sk_live_benchmark"`, `FAKE_FORECAST_KEY = "benchmark"`, `FAKE_POSTCODE = "SW1A 1AA"`, `FAKE_LATITUDE = 51.501`, `FAKE_LONGITUDE = -0.1419`
  - `collect_sensitive(account_cache: dict) -> dict[str, str]`: real value to fake value for the account number (the key under `account_data.account` that is not `gasAgreements`), every `mpan`, `mprn` and meter `serialNumber`, numbered `BENCH-MPAN-1`, `BENCH-MPRN-1`, `BENCH-MSN-1` and so on, in order of appearance.
  - `replace_everywhere(obj, replacements: dict[str, str])`: returns a new structure with every dict key and string value having each real value substring-replaced; never mutates its input.
  - `find_leaks(root: pathlib.Path, secrets: Iterable[str]) -> list[str]`: relative paths of files under `root` (any type, searched as bytes) containing any secret.

- [ ] **Step 1: Write failing tests** in `test_scrub.py` (`unittest.TestCase`, synthetic data only): `test_collect_sensitive_finds_account_mpan_mprn_and_serials` (a dict shaped like `{"account_data": {"account": {"A-1234": [{"meterPoint": {"mpan": "999", "meters": [{"serialNumber": "S1"}]}}], "gasAgreements": [{"meterPoint": {"mprn": "888"}}]}}}` returns `{"A-1234": FAKE_ACCOUNT, "999": "BENCH-MPAN-1", "S1": "BENCH-MSN-1", "888": "BENCH-MPRN-1"}`), `test_replace_everywhere_covers_keys_and_values_and_leaves_input_untouched`, `test_find_leaks_finds_a_secret_in_a_binary_file_and_not_in_clean_ones`.
- [ ] **Step 2: Run to verify they fail** (`ModuleNotFoundError: scrub`).
- [ ] **Step 3: Implement** the four names above in `scrub.py` (recursion over dict/list/str; `find_leaks` walks with `os.walk`).
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Add the hook** `memory-benchmark-tools-tests` (language `python`, entry `python -m unittest discover -s coverage/memory_benchmark_tools`, `files: ^coverage/memory_benchmark_tools/`, `pass_filenames: false`), and ignore `coverage/memory_benchmark_data/` and `coverage/memory_benchmark_runs/` in `.gitignore`.
- [ ] **Step 6: `git add`, run pre-commit from the repo root on the new files, fix, then commit** `feat(boards): scrub helpers for the memory benchmark fixture`.

---

### Task 2: Dummy inverter starting totals

**Files:**
- Modify: `apps/predbat/dummy_inverter.py` (`DUMMY_DEFAULTS`, `DummyInverter.initialize`, the module docstring's apps.yaml block), `docs/components.md` (the dummy inverter settings table, if it lists them)
- Test: `apps/predbat/tests/test_dummy_inverter.py`

**Interfaces:**
- Produces: an optional `totals` mapping in the `dummy_inverter:` apps.yaml block, `{"pv": kWh, "load": kWh, "import": kWh, "export": kWh}`, each defaulting to 0.0, that seeds `self.totals` so the published `*_lifetime` sensors start there. Absent means zero: no behaviour change for existing users.

- [ ] **Step 1: Run GitNexus `impact({target: "DummyInverter.initialize", direction: "upstream"})` and report the blast radius to the maintainer; stop and say so if it is HIGH or CRITICAL.**
- [ ] **Step 2: Write failing tests**: `test_dummy_totals_default_to_zero` (existing behaviour, `dummy.totals["load"] == 0.0`), `test_dummy_totals_seed_the_lifetime_sensors` (config `{"totals": {"load": 12.5, "pv": 3.25}}` gives `totals["load"] == 12.5`, `totals["pv"] == 3.25`, `totals["import"] == 0.0`, and after `dummy.publish()` the `sensor.predbat_dummy_load_lifetime` state is `12.5`), `test_dummy_totals_ignore_bad_values` (a non-numeric or negative entry is ignored, zero used). Register in the file's existing runner the way its neighbours are.
- [ ] **Step 3: Run `./run_all --test dummy_inverter` from `coverage/` (log to a file) and see them fail.**
- [ ] **Step 4: Implement** in `initialize`: read `config.get("totals")` if a dict, take each of `pv`, `load`, `import`, `export` as a non-negative float, else 0.0. Document it in the module docstring block.
- [ ] **Step 5: Run the dummy inverter tests, then `./run_all --quick` (log to a file) and see them pass.**
- [ ] **Step 6: `detect_changes()`, `git add`, pre-commit from the repo root, commit** `feat(dummy_inverter): optional starting totals for the lifetime sensors`.

---

### Task 3: Fixture database export

**Files:**
- Create: `coverage/memory_benchmark_tools/fixture_db.py`, `coverage/memory_benchmark_tools/test_fixture_db.py`

**Interfaces:**
- Consumes: `scrub.replace_everywhere`; `FAKE_*` constants.
- Produces (in `fixture_db.py`):
  - `@dataclass(frozen=True) class Remap: real_entity: str; dummy_entity: str; kind: str` where `kind` is one of `"lifetime"` (a daily-reset or lifetime energy counter), `"power_w"` (W, sign as the dummy publishes), `"gauge"` (copied as is).
  - `build_remap(live_args: dict, prefix: str) -> list[Remap]`: for each of `soc_kw`, `soc_max`, `battery_power`, `pv_power`, `grid_power`, `load_power`, `pv_today`, `load_today`, `import_today`, `export_today` present in the live `apps.yaml` args (value a list of one entity id), the real entity mapped to `sensor.<prefix>_dummy_<name>` using `dummy_inverter.DummyInverter.entity`'s naming: `battery_soc`, `battery_capacity`, `battery_power`, `pv_power`, `grid_power`, `load_power`, `pv_lifetime`, `load_lifetime`, `grid_import_lifetime`, `grid_export_lifetime`.
  - `daily_to_lifetime(rows: list[tuple[datetime, float]]) -> tuple[list[tuple[datetime, float]], float]`: input a time-ordered series of a counter that resets to near zero at midnight (or is already monotonic); returns the series rebased to start at 0 and rising across resets (a drop adds the previous value as carry), and the final total.
  - `settings_entities(prefix: str) -> set[str]`: the entity id of every `config.CONFIG_ITEMS` entry whose `type` is not `"update"`, formed `"{type}.{prefix}_{name}"` as `userinterface.py` sets `item["entity"]`; `"switch"` `"expert_mode"` with prefix `"predbat"` gives `"switch.predbat_expert_mode"`.
  - `is_predbat_output(entity_id: str, prefix: str, settings: set[str]) -> bool`: true for `predbat.*` and for any `*.<prefix>_*` entity that is not in `settings`.
  - `export_db(live_db: Path, out_db: Path, t0: datetime, days: int, remap: list[Remap], prefix: str, dropped: Callable[[str], bool], replacements: dict[str, str]) -> dict`: writes a new DB with the same schema `db_engine.DatabaseEngine._cleanup_db` creates (`entities`, `states`, `latest`, `idx_states_entity_datetime`, same columns), containing: every `latest` row of an entity not dropped, not a Predbat output (settings kept) and not a remapped real entity, with `replacements` applied; the `states` rows of the last `days` days up to `t0` for those entities; the remapped entities' history and latest under their dummy names (`lifetime` through `daily_to_lifetime`, `power_w` and `gauge` converted by the `unit_of_measurement` attribute to the dummy's unit); and returns `{"entities": int, "rows": int, "totals": {"pv": float, "load": float, "import": float, "export": float}, "dropped": int}`. `totals` are the final lifetime values, the seed for Task 2's `totals`.

- [ ] **Step 1: Write failing tests** with a small synthetic live DB built in `setUp` (three entities: a daily load counter with a midnight reset, a power sensor in kW, a switch that is a Predbat setting, plus `predbat.plan_history` and `sensor.predbat_cost_today` outputs): `test_daily_to_lifetime_carries_across_a_reset` (input 0, 1.0, 2.0, 0, 0.5 returns 0, 1.0, 2.0, 2.0, 2.5, total 2.5), `test_settings_entities_names`, `test_is_predbat_output_keeps_settings_and_drops_outputs`, `test_export_db_schema_matches_database_engine` (compare `sqlite_master` rows with a DB opened through a stub-base `DatabaseEngine` from `apps/predbat/db_engine.py`; put `apps/predbat` on `sys.path` in the test), `test_export_db_window_remap_and_drop` (rows older than `days` absent, the counter present under `sensor.predbat_dummy_load_lifetime` and ending at the returned total, the kW sensor converted to W, outputs gone, the setting kept with its value), `test_export_db_opens_live_db_read_only` (the live file's bytes and mtime unchanged).
- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement** the names above. Open the live DB with `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. Read the sign convention of the dummy's `battery_power` and `grid_power` from `simulate_minute` in `apps/predbat/dummy_inverter.py` and from how `inverter.py` treats `DUMMY`, and pin it in a test (`test_remap_battery_power_sign_matches_the_dummy`) using the real inverter's `battery_power_invert` setting if the live args carry one.
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: `git add`, pre-commit, commit** `feat(boards): fixture database export for the memory benchmark`.

---

### Task 4: Fixture cache export (forecast.solar and Octopus)

**Files:**
- Create: `coverage/memory_benchmark_tools/fixture_cache.py`, `coverage/memory_benchmark_tools/test_fixture_cache.py`

**Interfaces:**
- Consumes: `scrub.*`; `apps/predbat/solcast.py` (`SolarAPI.solar_cache_key(url, params)`, `URL_PERSONAL`, the postcodes.io URL form) put on `sys.path`.
- Produces (in `fixture_cache.py`):
  - `write_entry(cache_dir: Path, module: str, key: str, data, fmt: str, t0: datetime) -> None`: writes `<module>_<key>.<ext>` and the `.meta` sidecar `{"format", "expiry": t0 + 7 days, "module", "created": t0.isoformat()}` exactly as `storage.py`'s `StorageLocalFiles.save` does, so `age()` is 0 at T0.
  - `export_forecast_cache(live_cache: Path, out_cache: Path, t0: datetime, arrays: int) -> list[dict]`: reads the live `solar_*.json` entries, takes the forecast.solar responses (those with `result.watts`) in order of `created`, scrubs `message.info` (`latitude`, `longitude`, `place`, `distance` set to `FAKE_LATITUDE`, `FAKE_LONGITUDE`, `"Benchmark"`, 0) and drops the postcodes.io entry, then writes each response under the key the component computes for the neutral URL of array *i* (`FAKE_POSTCODE`, neutral `kwp`/`azimuth`/`declination`, `FAKE_FORECAST_KEY`), plus a synthesised postcodes.io response for `FAKE_POSTCODE` carrying the fake coordinates. Returns the neutral `forecast_solar` config list (`postcode`, `kwp`, `azimuth`, `declination`, `api_key`, `efficiency`) for the generated `apps.yaml`.
  - `export_octopus_cache(live_cache: Path, out_cache: Path, t0: datetime) -> dict[str, str]`: copies `octopus_user_account`, `octopus_<hash>` URL entries, `octopus_free_url_cache`, `octopus_user_billing` and `octopus_vehicle_catalogue`; computes `collect_sensitive` from the account entry and applies `replace_everywhere` to every copied entry; sets `kraken_token` to `None`; sets `tariff_fetched_at` and `device_fetched_at` in the account entry to `t0`; rewrites every `.meta` `created` to `t0`; returns the replacement map (the caller feeds it to the leak check and to the DB export).

- [ ] **Step 1: Write failing tests** with a synthetic live cache dir: `test_write_entry_meta_is_fresh_at_t0` (load it back through `StorageLocalFiles` from `apps/predbat/storage.py` with the clock at T0 and assert `age() == 0.0` and `load()` equals the data), `test_forecast_export_scrubs_location_and_rekeys` (the written entry sits at `SolarAPI.solar_cache_key(<neutral url>, <neutral params>)`, `message.info.latitude` is `FAKE_LATITUDE`, and no real `place` string survives anywhere in the output dir via `find_leaks`), `test_forecast_export_synthesises_the_postcode_lookup`, `test_octopus_export_scrubs_identifiers_and_token` (account number, mpan, serial absent from every output file; `kraken_token` is `None`; tariff and product codes unchanged; fetched-at timestamps equal T0), `test_octopus_export_keeps_url_cache_entries_byte_for_byte_except_meta` (rate entries have no identifiers, so their data is unchanged).
- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement.** Build the neutral URLs with the component's own constants, not copies of them, so a change to `solcast.py` is caught by Task 8's no-fetch check.
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: `git add`, pre-commit, commit** `feat(boards): fixture cache export for the memory benchmark`.

---

### Task 5: Export CLI, template and generated config

**Files:**
- Create: `coverage/memory_benchmark_tools/export_fixture.py`, `coverage/memory_benchmark_tools/apps_template.yaml`, `coverage/memory_benchmark_tools/test_export_fixture.py`
- Modify: `coverage/memory_benchmark_tools/` hook regex if needed

**Interfaces:**
- Consumes: Tasks 1, 3, 4.
- Produces:
  - `export_fixture.py` CLI: `python3 coverage/memory_benchmark_tools/export_fixture.py --live <live add-on config dir> --out coverage/memory_benchmark_data [--days 7]`. It takes T0 as the current UTC time (rounded down to the second), writes `<out>/predbat.db`, `<out>/cache/`, `<out>/apps.yaml`, `<out>/manifest.json` (`t0`, `days`, `db_sha256`, `tool_version`), runs `find_leaks` over `<out>` with the real sensitive values and the live `octopus_api_key`/`octopus_api_account`/`forecast_solar` keys and postcodes, and exits non-zero (deleting `<out>`) if any leak is found.
  - `build_apps_yaml(template: dict, live_apps: dict, forecast_config: list[dict], totals: dict, t0: datetime) -> dict`: the template (committed: generic dummy values, `db_enable: true`, `db_primary: true`, `timezone`, `mcp_enable: false`, load ML off, GivTCP/Octopus-direct inverter settings absent, `octopus_api_key: FAKE_OCTOPUS_KEY`, `octopus_api_account: FAKE_ACCOUNT`, `forecast_solar` from the caller, `forecast_solar_max_age` very large) overlaid with: the live entity settings Predbat reads for rates and cars (every `metric_*`, `rates_*`, `car_*`, `octopus_*` entity setting and the user's tuning keys, minus anything pointing at a real inverter or Solcast entity, which are dropped), and the live inverter sizing for the `dummy_inverter:` block (`battery_size` from the real `soc_max`, `battery_rate_max`, `inverter_limit`, `export_limit`, `reserve`, `soc_initial` from the real SoC percentage at T0, plus `totals`). `timezone` is copied from the live config. The CLI passes `export_db` a `dropped` predicate that is true for every entity the live `apps.yaml` names in a `pv_forecast_*` setting (the Home Assistant Solcast integration's) and for any other entity the forecast and Octopus components republish from their seeded caches.

- [ ] **Step 1: Write failing tests**: `test_build_apps_yaml_has_no_real_identifiers` (a synthetic live apps dict with a fake key and entity ids; none survive into the output except the entity-id settings that point at kept entities, which are named in the test), `test_build_apps_yaml_disables_mcp_load_ml_and_givtcp`, `test_build_apps_yaml_dummy_block_uses_live_sizing_and_totals`, `test_export_fixture_cli_end_to_end_on_synthetic_inputs` (builds a tiny synthetic live dir, runs `main([...])`, asserts the five outputs exist, the manifest `t0` parses, and a planted real identifier makes the CLI exit non-zero and remove `<out>`).
- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement** the CLI and `build_apps_yaml`; `main(argv: list[str]) -> int`.
- [ ] **Step 4: Run and see them pass.**
- [ ] **Step 5: Real export, read-only.** Run the CLI against the live add-on config share into `coverage/memory_benchmark_data`. Expected: exit 0, no leak, entity count in the same range as the live mirror's 684 minus the dropped ones. Record the counts (not the names) in the commit message. If the live share is not mounted, stop and ask the maintainer.
- [ ] **Step 6: `git add`, pre-commit, commit** `feat(boards): export_fixture builds the memory benchmark fixture`.

---

### Task 6: Container image and wrapper

**Files:**
- Create: `coverage/memory_benchmark_tools/Dockerfile`, `coverage/memory_benchmark` (executable), `coverage/memory_benchmark_tools/test_wrapper.py`

**Interfaces:**
- Produces: `coverage/memory_benchmark` (executable bash): `memory_benchmark <tree> <out dir>`. Steps: check `coverage/memory_benchmark_data/manifest.json` exists, else print how to build it and exit 2 with no `result.json`; build the image if missing; copy `<tree>/apps/predbat` into the container's `/work` (tree mounted read-only at `/src`); build the kernel in `/work` with `bash apps/predbat/build_kernel.sh` **without** libfaketime; start the driver with `--network none`, `TZ=UTC`, `LD_PRELOAD=<libfaketime path>`, `FAKETIME="<T0 as YYYY-MM-DD HH:MM:SS, no @ prefix, so the clock is frozen>"`, `FAKETIME_DONT_FAKE_MONOTONIC=1`, a copy of the fixture as the working directory, fixed `--cpus` and `--memory`, and a hard `timeout` on the whole run; copy `result.json` and `predbat.log` to `<out dir>`; exit 0 only if a `result.json` was written.
- Image: `Dockerfile` `FROM python:3.13-slim-bookworm@sha256:<digest>` (resolve the digest with `docker pull` then `docker inspect --format '{{index .RepoDigests 0}}'` and commit it), installs `g++`, `libfaketime` and the repo's `requirements.txt` packages; tag `predbat-memory-bench:<first 12 chars of the sha256 of Dockerfile and requirements.txt>`. The image build may use the network; a benchmark run may not.

- [ ] **Step 1: Write failing tests** in `test_wrapper.py` for what runs without Docker: `test_wrapper_without_a_fixture_exits_2_and_says_how_to_build_it` (run the script with a temp `coverage/memory_benchmark_data` absent; assert exit code 2, the message names `export_fixture.py`, no `result.json`), `test_wrapper_rejects_a_missing_tree` (exit 2).
- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement** the wrapper and Dockerfile.
- [ ] **Step 4: Run the tests and see them pass.**
- [ ] **Step 5: Smoke test with Docker** (the driver is a stub that prints the date and `python3 -c "import time; print(time.time())"`): run twice a few seconds apart; the printed wall time is identical and equals T0, `time.monotonic()` advances, and a connection to `1.1.1.1:53` fails. Record the three results in the commit message.
- [ ] **Step 6: `git add`, pre-commit, commit** `feat(boards): memory_benchmark wrapper and pinned image`.

---

### Task 7: Driver and result rules

**Files:**
- Create: `coverage/memory_benchmark_tools/driver.py`, `coverage/memory_benchmark_tools/test_driver.py`

**Interfaces:**
- Produces (in `driver.py`):
  - `ALLOWED_WARNINGS: tuple[str, ...]`: the exact `Warn: OctopusAPI:` substrings the failed token mint and GraphQL poll produce (found in Task 8 step 1 and written here).
  - `check_log(text: str, allowed_warnings=ALLOWED_WARNINGS) -> list[str]`: the problems found, empty when clean: any line whose message starts `Error:`, any line containing `stale binary` or `using Python engine`, any line containing `SolarAPI: Fetching`, any `Warn:` line that is neither allowed nor a known benign start-up warning listed in the same module.
  - `summarise_rss(samples_kb: list[int], hwm_kb: int, settle: int) -> dict`: `{"steady_rss_mb": median of samples after the first settle cycles, in MB rounded to 0.1, "peak_rss_mb": hwm_kb in MB rounded to 0.1}`.
  - `plan_digest(plans: list[dict]) -> str`: sha256 hex of `json.dumps(plans, sort_keys=True, default=str)`; each `plans[i]` is the canonical plan dict of cycle *i*: the same dict `tests/test_single_debug.py` writes to `plan_final.json` (reuse its builder, do not copy it) with run-time stamps removed.
  - `main(argv: list[str]) -> int`: arguments `--out <dir> --cycles N`.
- Behaviour of `main`: set `PREDBAT_APPS_FILE`, build `predbat.PredBat()` and call `initialize()` as `hass.py`'s `main()` does; pump `timer_tick()` only until the DB manager, dummy inverter, forecast and Octopus components have each published once (poll their entities in `ha_interface.state_data` with a deadline, fail with `ok: false` on timeout); then stop pumping and run N cycles of `update_pred(scheduled=False)`; after each, record `VmRSS` from `/proc/self/status`; at the end record `VmHWM`; write `result.json` (`ok` is `not check_log(...)` and every cycle returned non-`None`) and exit 0 if the file was written.

- [ ] **Step 1: Write failing tests**: `test_check_log_flags_errors_stale_binary_and_solar_fetch`, `test_check_log_allows_only_the_named_octopus_warnings`, `test_summarise_rss_uses_median_after_settle_and_hwm_for_peak` (samples `[100000, 120000, 119000, 121000, 120000]`, settle 2 gives steady `120.0`, hwm `130000` gives peak `130.0`), `test_plan_digest_is_stable_and_order_insensitive_for_dicts_and_changes_with_a_plan`, `test_plan_digest_ignores_run_timestamps`.
- [ ] **Step 2: Run and see them fail.**
- [ ] **Step 3: Implement** the four pure functions, then `main`.
- [ ] **Step 4: Run and see the unit tests pass.**
- [ ] **Step 5: First real run** of `coverage/memory_benchmark <main worktree> /tmp/predbat-board/memory/bench1` (log to a file). Expected: a `result.json` with plausible numbers. Fix what the log shows; add every benign start-up warning you decide to allow to the module's list with a comment saying why. Iterate until `ok` is true with no `Error:` lines.
- [ ] **Step 6: `git add`, pre-commit, commit** `feat(boards): memory benchmark driver and result rules`.

---

### Task 8: Acceptance, docs and baseline

**Files:**
- Modify: `boards/predbat-memory/README.md` (replace the "benchmark does not exist yet" section with how to build the fixture and run it), `docs/developing.md` (a short section beside the standalone scripts), `boards/predbat-memory/board.toml` and `boards/predbat-memory/measure.py` (`STEADY_CUT`) only if the measured baseline says the 25% target is wrong

- [ ] **Step 1: Find the allowed Octopus warnings.** Run the benchmark on main; copy the exact `Warn:` strings the failed token mint and poll produce into `ALLOWED_WARNINGS` (Task 7) with a test pinning them.
- [ ] **Step 2: Determinism.** Run main five times, two of them at least an hour apart. Expected: one `plan_digest` in all five. Record the five `steady_rss_mb` and `peak_rss_mb` and their spread in the README. If the spread makes a 25% target unreliable, report it to the maintainer, do not hide it.
- [ ] **Step 3: No network and no refetch.** In one run's log assert zero `SolarAPI: Fetching` lines and that the only Octopus network lines are the allowed warnings; from inside a container started the same way, a connection attempt fails.
- [ ] **Step 4: Sensitivity controls.** On a throwaway branch (not pushed), (a) keep a 20 MB list alive for the whole run and check `steady_rss_mb` rises by about 20; (b) make a one-line change that must change a plan (for example scale the import rates by 1% right after they are loaded in `fetch.py`) and check `plan_digest` differs from main's. Record both results in the README.
- [ ] **Step 5: `measure.py` end to end.** Run `BOARD_CANDIDATE=<main commit> python3 boards/predbat-memory/measure.py`. Expected: a clean harness result reporting a 0% saving (a fail on the target, not a `HARNESS ERROR`).
- [ ] **Step 6: Leak scan of everything the runs wrote** (`<out dir>`s and `predbat.log`) with `find_leaks` and the real sensitive values; expected none.
- [ ] **Step 7: Docs.** Write the README and `docs/developing.md` changes; re-check `STEADY_CUT` against the measured baseline and change it with the reason if needed.
- [ ] **Step 8: Full checks.** `./run_pre_commit` and `./run_all --quick` from `coverage/` (logs to files); expected pass.
- [ ] **Step 9: Update the shared memory.** `update` the PR #6367 memory (tag `pr:6367`, scope `predbat-devs`): benchmark built, the baseline figures and spread, and the next action (mark the PR ready, merge, run the board). Public-safe content only.
- [ ] **Step 10: Commit** `docs(boards): memory benchmark acceptance results and baseline`.
