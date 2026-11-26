# Memory benchmark for the predbat-memory board: design

Status: approved by the maintainer 26 Nov 2026 (including the frozen clock, the saved-cache Octopus and forecast data, and
MCP, Load ML and GivTCP off). Lives on PR #6367 (branch `feat/board-predbat-memory`).

## Intent

The `predbat-memory` board (`boards/predbat-memory/`) cuts Predbat's memory use with plans unchanged. It cannot run
until `coverage/memory_benchmark` exists: workers use it to find and check savings, and `boards/predbat-memory/measure.py`
uses it to judge the lead's candidate. This document designs that benchmark.

It runs Predbat standalone, the way `coverage/standalone` does, but with `db_primary: true`, the dummy inverter
(`apps/predbat/dummy_inverter.py`) and no network. That makes a run fast, self-contained and repeatable. Its input is a
reference database of real history from the maintainer's Home Assistant, so the memory it measures looks like a real
install's, not a toy one's.

The contract the board already depends on (from `boards/predbat-memory/README.md`, not up for change here):

- `coverage/memory_benchmark <tree> <out dir>`, run from the main checkout. `<tree>` is any batpred checkout (main, or
  a board worktree). Exit code 0 when the run completed.
- Writes `<out dir>/result.json` `{ "ok", "steady_rss_mb", "peak_rss_mb", "plan_digest" }` and the run's `predbat.log`.
- Runs in a Linux container on a fixed image (macOS RSS swings 40-140 MB with page compression and is not evidence; see
  PR #6152). Builds the kernel from `<tree>` for the container's architecture, so a branch never runs a stale binary.
- `ok` is false when the log shows an error or the kernel fell back to Python ("stale binary").
- Two runs of the same tree give the same `plan_digest`. `measure.py` treats a base that disagrees with itself as a
  harness error.

## Goals

1. A board worker can run `coverage/memory_benchmark <worktree> <out>` and get steady and peak RSS in minutes, not hours.
2. Two runs of one tree agree on `plan_digest` exactly, and on `steady_rss_mb` closely enough that a 25% saving is
   unmistakable. The spread between runs of main is measured and reported when the benchmark is built.
3. Zero network traffic during a run, enforced by the container (`--network none`), not by hoping nothing is enabled.
4. The data resembles a real install: the real entity set Predbat reads, with 7 days of real history behind it.
5. The plan digest changes if and only if a plan changes, so a branch that wins memory by changing behaviour is caught.

## Non-goals

- Idle RSS between cycles on a long-running install, and Load ML (agreed lever in the idle-memory work: judge on Linux
  steady-state RSS with Load ML off).
- Speed. Run time only has to be short enough that the board can run it often.
- Real inverter control. The dummy inverter's model is not meant to match a real GivTCP inverter, only to give Predbat a
  coherent inverter to plan against.
- Making the existing `coverage/standalone` scripts deterministic.

## Anti-goals

- A fixture holding the maintainer's household data in git. The board scope is public-safe.
- A benchmark whose run-to-run noise lets the board chase noise.
- Anything that lets a branch win memory by dropping a feature, history or output. The digest and the board rules guard
  that; the benchmark must not make it easier.
- Changing Predbat's behaviour for everyone to make the benchmark work. Hooks the benchmark needs are small, default-off,
  and tested.

## Constraints

- Docker is the container runtime on the maintainer's machine (Docker Engine 28.5 on Apple Silicon, so the container is
  aarch64 Linux). Image build may use the network once; a benchmark run may not.
- `coverage/` and `boards/` are off-limits to board workers, so the benchmark cannot be changed by the board it serves.
- Unit tests for all new code (CLAUDE.md); pre-commit and 100% docstrings; British English for cspell.
- New sensors, if any, are named `sensor.predbat_x`.
- Facts and commits on the board are public-safe: no entity ids or values from the live system leak into docs, tests or
  committed fixtures.

## What the code does today (verified 26 Nov 2026)

- With `db_primary` and no `ha_key`, `HAInterface.update_states_db()` loads the **latest state of every entity in the
  DB** into `state_data`, whether Predbat reads it or not. So every entity in the reference DB costs RSS; the fixture's
  entity set is part of what is being measured.
- The live DB (`predbat.db` on the HA add-on config share) is a mirror, not all of HA: 684 entities, 545 MB, 29 days. A
  fresh standalone_ha run mirrors 453 of them. The 222 not read by the current config total about 0.46 MB of the 2 MB
  of latest-state text.
- `db_engine._cleanup_db()` deletes non-`D` rows older than `db_days` against `now_utc_real` at startup, which is the real
  clock. A fixture a few weeks old would lose its history unless `db_days` were large; freezing the clock at T0 removes
  the problem.
- `PredBat.update_pred(scheduled, plan_at=...)` can plan at a caller-chosen instant, but `plan_at` must be at or behind
  the wall clock and only moves the planning clock (`now_utc_real`, `now_utc`, `midnight_utc`, `minutes_now`). The Octopus
  and forecast components and the storage cache's age checks read the real `datetime.now()` instead, and the DB manager
  and every component run on their own async schedules. So `plan_at` cannot freeze everything; the wall clock itself has to
  be frozen (see "The clock").
- `plan_once()` calls `download_predbat_releases()` when no `user_id` is set. In a no-network container that must fail
  quietly, not as an `Error:` line that flips `ok`.
- `hass.py`'s loop runs on the wall clock every 5 minutes with real sleeps, so it cannot give a fixed clock.

## Approaches

### The reference database

| | Real run only | Export 7 days from the live DB | Real run for the read list, live DB for the data |
| --- | --- | --- | --- |
| Holds | what one `standalone_ha` run reads | everything the live mirror holds | what a run reads, with full history |
| History | only what the run could backfill; a week's wait if it doesn't backfill | 7 days, real | 7 days, real |
| Steps | one | one | two |
| Memory fidelity | exact | about 20-25% more state than the current config reads | exact |

**Chosen: export 7 days from the live DB.** The live DB is already a mirror of what Predbat reads and writes, so it
is close to the read set, and one step beats two. The maintainer decided this after the question of whether extra rows
inflate memory was checked (they do, through `update_states_db`, but by a small margin).

Two filters on the export, my calls: the real inverter entities that the re-keying replaces are dropped, because the
dummy inverter never reads them; and Predbat's own `predbat.*` output entities are stripped (`plan_history` alone is
376 KB), because a stale plan from the live system must not leak into the benchmark's inputs, and the run regenerates
them.

### The driver

- **Run the real `hass.py` loop for a while.** Closest to production, but it plans on the wall clock every 5 minutes with
  real sleeps: slow, and the plans depend on when it was started. Rejected.
- **A mock harness like the unit tests' `create_predbat()`.** Fast, but it skips the real startup, DB manager and component
  wiring, which is where much of the memory is. Rejected.
- **Real startup, with the wall clock frozen at T0, then a driver running N forced plan cycles.** Real construction, real
  components, and every part of the process sees the same instant. **Chosen** (the maintainer's decision, 26 Nov: "fix the
  clock to the extraction time so it's all frozen"). The risk is that the DB manager and components are asynchronous, so
  the driver has to wait for each one's first publish before the first cycle (see Left to the builder).
- **Frozen at T0 versus stepped forward 5 minutes a cycle.** Frozen is what the maintainer asked for, and it is simpler and
  fully deterministic, so it is the design. It does not exercise the paths that need time to pass (plan ageing, day
  rollover, history growing between cycles). A stepped clock could be added later without changing the rest.

### Where it runs

- **On the host.** macOS RSS is unusable. Rejected.
- **A Linux container with a pinned image.** **Chosen.** RSS comes from `/proc` inside the container, the network is
  cut with `--network none`, and the kernel is built inside for the container's architecture.

## Design

### Pieces (all committed)

1. **`coverage/memory_benchmark`**: the executable named in the contract. A shell wrapper that makes sure the pinned
   image exists, copies the fixture DB to a scratch directory, starts the container with `--network none`, a copy of
   `<tree>`'s `apps/predbat` made for that run mounted read-only (not the tree itself: Docker Desktop can serve a file
   edited after an earlier container read it at its old length), the scratch directory and the out directory mounted writable, runs the driver, and exits with the
   driver's status. It builds the kernel for the container from `<tree>`'s sources; a build can be cached only if the
   cache key is the kernel source hash, so no branch can reuse another's binary.
2. **The driver (Python, run in the container).** Builds `PredBat` from `<tree>` through the real startup path with the
   benchmark's `apps.yaml`, steps N planning cycles at fixed instants, samples RSS, and writes `result.json`.
3. **The benchmark's `apps.yaml` (the test case).** A committed template, completed by the export script into a local
   file (see "What is committed and what stays local"). It sets `db_enable: true`, `db_primary: true`, a large `db_days`,
   a `dummy_inverter:` block (round values in the template; the generated file sizes it like the maintainer's real
   inverter so the history is coherent), a fixed timezone, no `ha_url`/`ha_key`, MCP, Load ML and GivTCP off, and the forecast and Octopus components on, served from
   their seeded caches with the network cut. The
   one place that says what runs.
4. **The export script.** Reads the live DB read-only and writes the fixture (below). Run by the maintainer, not by the
   board or CI. It is committed; its output is not.
5. **Tests.** Unit tests for the export's re-keying, filtering and windowing on a small synthetic DB, and for the
   driver's pure parts (digest, steady/peak calculation, log checks). They run in the repo's normal test or pre-commit
   path, following `boards/predbat-memory/test_measure.py` as the precedent.

### The fixture

- Written to a gitignored local directory (for example `coverage/memory_benchmark_data/`), with a small manifest recording
  the instant T0 it was cut at and the checksum of the DB.
- **T0 is the time of export.** The export takes the live DB's `latest` rows as they are at T0 (rates, forecasts and
  every other current-state entity are right for T0 without rebasing) plus the last 7 days of `states` ending at T0. Nothing
  is shifted in time.
- **Re-keying.** History that Predbat reads for the inverter (load, PV, import, export, battery power and SoC, and what the
  charge-rate curve fit reads) is copied from the real inverter's entities to the dummy inverter's sensor names. Daily
  counters ("today" sensors that reset at midnight) become the dummy's monotonically increasing lifetime counters by
  summing the daily values, so the series is continuous. Sign conventions and units are matched to what the dummy
  publishes.
- **Dropped:** the real inverter entities replaced by re-keying; entities the forecast and Octopus components republish
  (they regenerate them from their seeded caches); `predbat.*` and other Predbat-output entities. Everything else
  from the live mirror stays as it is, so entity count and state sizes stay close to a real install.

### What is committed and what stays local

Real entity ids carry meter and device serials, and the dummy inverter's sizing is the maintainer's household data, so
none of it goes into git.

- **Committed:** the export script, the driver, the wrapper, a **template** `apps.yaml` with generic names and round
  dummy-inverter values, and tests that use synthetic names only.
- **Local and gitignored, written by the export script into the fixture directory:** the reference DB, the manifest, and
  a generated `apps.yaml`. The script builds that file from the template plus the entity-id settings and inverter sizing
  in the maintainer's live `apps.yaml` (the add-on config share), so the run reads the same entities a real install
  does. The driver runs against the generated file.
- The benchmark only runs where the fixture exists, which is the maintainer's machine (the board runner runs
  `measure.py` from the main checkout, and the containers are local). CI does not run it. With no fixture the wrapper
  exits non-zero with a message saying how to build one; `measure.py` already turns a non-zero exit into a harness error.
- The export script and the builder may read the live DB and live `apps.yaml` read-only (SQLite opened with `mode=ro`).
  Nothing read from them is written to a committed file, a log under git, or a board memory.

### What the benchmark's Predbat runs

- **Settings are kept.** Predbat's user-facing settings (the switches, selects and inputs that `CONFIG_ITEMS` in
  `config.py` defines) stay in the fixture with the maintainer's real values, because they drive the plan and a real
  install runs on them. Every other entity Predbat publishes (`predbat.*`, `sensor.predbat_*` outputs and the like) is
  stripped. The export derives the keep list from `CONFIG_ITEMS` rather than from a name pattern.
- **The shape mirrors the Predbat cloud configuration** (maintainer's instruction, 26 Nov): no Home Assistant, `db_primary`,
  the dummy inverter in place of a real one, MCP **off**, Load ML **off**. The cloud's own `apps.yaml` is not in this
  repo, so "mirror" is taken to mean this component set and nothing more; the maintainer can correct it.
- **GivTCP is off.** Its job is done by the dummy inverter.
- **The forecast and Octopus components run for real, from the maintainer's own saved data, with the network cut.** The
  export script copies the relevant entries from the live add-on's `cache/` directory at the same moment as the DB
  export, so they match T0, and the container has no network, so every request has to be answered from them.
  Whatever the component would have fetched is served by the same storage cache it already uses, so the real parsing,
  merging and publishing code runs and only the HTTP is skipped. No product code changes.
  - **Forecast (forecast.solar, the maintainer's live provider).** `cache_get_url` returns a cached answer with no HTTP
    when the entry (storage area `solar`, key `solar_cache_key(url, params)`) is younger than the poll age. The export
    re-keys the live entries under neutral URLs and a fake key, because the component logs the URL on every cached hit
    and the real ones carry the home's coordinates and a key. It scrubs the location fields forecast.solar echoes back
    in its responses, and the generated `apps.yaml` points the component at those neutral values with a poll age too
    large for the entries to expire.
  - **Octopus.** The export copies the `octopus_user/account` entry (account data, devices, saving sessions), the
    `octopus` URL-cache entries (rates and tariffs), and the billing and vehicle-catalogue entries. It scrubs the
    account number, meter serials and the Kraken token, and rewrites the fetched-at timestamps and entry ages at the
    start of every run so the data counts as fresh. Tariff and product codes are public and stay, because the component
    reads their names (for example to detect Intelligent Go).
  - **What still reaches for the network, and why it is tolerable.** With fresh data the component does not refetch the
    account or tariffs. On its first run and every two minutes it still polls the Intelligent dispatches, which needs a
    token and a GraphQL call, and those fail with no network. A failed poll keeps what the component has ("None means the
    poll failed, so keep what we have"), and logs `Warn:` lines for the failed token and connection, not `Error:`. The
    benchmark's `ok` rule therefore allows a short, named list of those warnings and still fails on any `Error:`, on any
    request that is not one of them, and on `SolarAPI: Fetching`.
  - **The real clock would leak in, so it is frozen** (see "The clock"). These components read the real `datetime.now()`,
    so what they publish (started dispatches trimmed, current-rate sensors) would otherwise depend on when the benchmark
    is run relative to T0, and a stored baseline digest would stop being valid from one day to the next.
  - The Octopus and forecast entities in the live DB are dropped from the fixture where the components
    republish them, so the run does not start with stale copies.
  - Considered and set aside: a fake HTTP server on the container's loopback, and a synthetic forecast generator.
    Both add moving parts, and the maintainer's own cache is more realistic and cheaper.
- **The web server stays at its default (on).** It only listens, so nothing leaves the container. I cannot see whether
  the cloud turns it off; the maintainer can say.
- The forecast and Octopus components run on their own real-time schedules like the other components, so the driver has to
  wait for each one's first publish before the first cycle, or the first plan would be made without a forecast or rates
  and the digest would vary.

### The clock

The wall clock is frozen at T0, the time the fixture was extracted, for the whole process: `time.time()`,
`datetime.now()` and `utcnow()` all return T0 from before the first Predbat import, in `<tree>` and in every component.
That makes what the Octopus and forecast components publish, the storage cache's age checks, `db_engine`'s clean-up and
the dummy inverter's time all agree with the data, and a run gives the same digest whenever it is made.

- **Only wall time is frozen.** `time.monotonic()` and real sleeps must keep running, or asyncio timers, thread waits and
  timeouts never fire and the run hangs.
- **The mechanism is the builder's choice** (a preloaded libfaketime in the container, which freezes the wall clock for
  C code and Python alike, or a Python-level patch installed before the first import). The requirement is the behaviour
  above, and that it is the same for main and every branch.
- **Cache ages come from the entry, not the file.** `storage.age()` reads the `created` field in each entry's `.meta`
  sidecar and compares it with `datetime.now(timezone.utc)`, so with the clock frozen at T0 the export writes `created` as
  T0 and every seeded entry is fresh. File times (which the kernel stamps with the real time) are not used.
- With the clock frozen at T0, `db_days` no longer has to be large: the fixture is never older than its own window.

### One run

1. Start `PredBat` from `<tree>` against the scratch copy of the fixture, with the wall clock frozen at T0 from before the
   first import (see "The clock").
2. Wait for the DB manager, the dummy inverter and the forecast and Octopus components to publish once, then run N cycles
   with `update_pred(scheduled=False)`, which forces a full recompute each time (at a frozen clock a plan never ages, so
   an unforced cycle would reuse the first plan and measure nothing). N is fixed by the benchmark.
3. After each cycle record RSS (`VmRSS` from `/proc/self/status`) and, at the end, `VmHWM`.
4. `steady_rss_mb` is a fixed summary of the later cycles' RSS (for example the median of the last half); `peak_rss_mb`
   is `VmHWM`; the benchmark fixes the rule and does not change it.
5. `plan_digest` is a hash over a canonical form of each cycle's plan (what Predbat decided: charge and export windows,
   limits, the plan's metric and cost), excluding timestamps of the run and anything that is not a plan.
6. `ok` is false on any `Error:` line in the log, on a stale-binary Python fallback, on a missing result, or when a cycle
   was skipped.

## Acceptance

The benchmark is done when:

- Two runs of main give the identical `plan_digest`, including two made hours apart (the frozen clock is what makes
  that hold), and the run-to-run spread of `steady_rss_mb` and `peak_rss_mb` is
  measured over at least five runs and recorded in the README. If it is too large for a 25% target, that is reported,
  not hidden.
- A control branch that deliberately keeps about 20 MB alive changes `steady_rss_mb` by that much, and one that changes a
  plan changes `plan_digest`. Both confirm the benchmark can see what the board needs it to see.
- The container has no network (a deliberate in-container connection attempt fails) and the log has no `Error:` line for a
  missing network.
- `measure.py` runs end to end with main as both base and candidate and reports a harness-clean result.
- Pre-commit and the new tests pass, and `boards/predbat-memory/README.md` and `docs/developing.md` describe how to build
  the fixture and run the benchmark.
- The first measurement of main is recorded, and the 25% target in `board.toml` and `measure.py` (`STEADY_CUT`) is
  re-checked against it.

## Left to the builder

- The container image (a pinned slim Python 3.13 on Debian, by digest, is the obvious start), how it is built and cached,
  and its CPU and memory limits. These are fixed so runs are comparable, and not changed per run.
- N, the settle rule for `steady_rss_mb`, whether to force a garbage collection before sampling (it must be the same for
  main and every branch), and the driver's internal structure.
- How the driver knows each component has published once before the first cycle, and how `download_predbat_releases()` is
  kept quiet with no network, with no change to default behaviour.
- The clock-freezing mechanism (see "The clock"), and whether `update_pred(scheduled=False)` or a direct `calculate_plan`
  call is the better forced cycle.
- Thread settings: pick what a real install runs and what is deterministic, and fix it. The optimiser board used
  `threads: 0`.
- Which inverter entities Predbat reads history from when the dummy inverter is the inverter. Find out by running with the
  dummy and listing every `Entity ... does not exist` warning and every history read, then map each.
- Continuity of the dummy's lifetime counters at T0. The dummy publishes its own simulated counters from its start; the
  fixture's last lifetime values must not jump against them. Choose between rebasing the fixture to the dummy's starting
  values and a small, default-off, tested way to seed the dummy's counters.
- The exact `plan_digest` canonical form, within the rule above.
- Names and file layout, within the contract.
