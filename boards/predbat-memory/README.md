# Board predbat-memory

A shared-goal board that cuts Predbat's memory use, with plans unchanged. It runs on the board runner and the
blackboard skill from agent-memory-supabase (`runner/` and `skills/blackboard/` there). The board's memories live in the
`predbat-devs` scope, tagged `board:predbat-memory`.

| File               | What it is                                                                          |
| ------------------ | ----------------------------------------------------------------------------------- |
| `board.toml`       | The board file: goal, done-when, rules, intent limit, models and permissions        |
| `measure.py`       | The runner's `[measure]` command, which confirms a candidate before the board ends  |
| `test_measure.py`  | Tests for `measure.py` and the board file's permissions, run in pre-commit          |

## How the board runs

1. **Baseline.** The lead's first round writes one intent only. Its worker records main's commit, measures main three
   times with the benchmark, and maps the largest memory holders. No saving work starts until that fact is confirmed.
2. **Savings.** The lead plans intents from the map, two open at a time. Each worker makes its own worktree and branch
   (`board/predbat-memory/<intent short id>`) and makes one commit per change. It starts from main, from the commit the
   intent names, or from any earlier board commit with a confirmed fact. To combine savings it cherry-picks, merges or
   rebases them onto its own branch. All
   the worktrees share one repository, so every board commit is visible to every worker.
3. **Knowing what is in a tree.** Every board commit message starts `board(predbat-memory <intent short id>): <what it
   saves>`, so `git log --oneline main..<commit>` lists the savings in a tree. Every fact about a commit starts with
   `Commit:`, `Built on:`, `Tree contains:` (each saving with the fact that measured it) and the steady and peak
   medians against main.
4. **Finishing.** When a commit's facts meet the done-when, the lead proposes it, and the runner runs `measure.py`
   against main. A pass ends the board. A fail goes back to the lead to plan from. The board also stops at 20 intents,
   and can be resumed with a higher limit.

## The benchmark

`coverage/memory_benchmark <tree> <out dir>`, run from the main checkout, is what workers use to find and check savings
and what `measure.py` uses to judge the lead's candidate. Its design is in
`docs/superpowers/specs/2026-11-26-memory-benchmark-design.md`.

- **The run:** Predbat from `<tree>`'s `apps/predbat/`, started as `hass.py` starts it, with `db_primary: true`, the
  dummy inverter in place of the real one, and no Home Assistant. The forecast (forecast.solar) and Octopus components
  run, served from their seeded caches. MCP, Load ML and GivTCP are off. After every component has published once,
  the driver runs 6 plan cycles, each forced to recompute.
- **Where:** a Linux container on a pinned image (`coverage/memory_benchmark_tools/Dockerfile`), with `--network none`,
  4 CPUs and 3 GB. The container gets a copy of `<tree>`'s `apps/predbat` made for that run, because Docker Desktop
  can serve a file edited after an earlier container read it at its old length. The kernel is built inside from that
  copy, so a branch never runs a stale binary. The wall clock is
  frozen at the fixture's T0 with libfaketime (monotonic time keeps running), so every run plans at the same instant
  whenever it is made.
- **Output:** `<out dir>/result.json`, `<out dir>/modules.txt` and the run's `predbat.log`:
    - `steady_rss_mb`: the median `VmRSS` (MiB) of cycles 4 to 6.
    - `peak_rss_mb`: `VmHWM` (MiB) for the whole run. The board's goal is steady-state memory, so `measure.py` only
      checks that the candidate's median peak is no more than 10% above main's (`PEAK_RISE`).
    - `plan_digest`: a hash of each cycle's charge and export windows and limits, and its predicted SoC and cost every
      half-hour.
    - `ok`: false on any `Error:` line, a stale kernel binary, or a forecast URL fetch. Also false on any Octopus failure
      other than the token mint and the three Intelligent polls (`ALLOWED_WARNINGS`), any other unlisted warning
      (`BENIGN_WARNINGS` gives a reason for each), or a log line that looks like personal data.
    - Diagnostics: the per-cycle RSS, cycle times, `modules_loaded` and the problems found.
    - `modules.txt`: the modules loaded after the last cycle, one line per top-level package, largest first. Each line
      gives the compiled size on disk (a proxy for the code the import holds, not its RSS), the number of modules, and
      whether the package is third-party, stdlib, local (Predbat and the benchmark) or built-in. The tracemalloc report
      puts import costs under importlib, so this list is how a worker sees which libraries are worth cutting.
- **Profiling:** `coverage/memory_benchmark --tracemalloc <tree> <out dir>` also writes `<out dir>/tracemalloc.txt`, the
  largest live allocations after the last cycle by line and by file. This is how workers map the memory; they may
  also run Python for deeper analysis (import times, referrers). Tracing slows the run and adds to its RSS, so the result says
  `"tracemalloc": true` and `measure.py` refuses it as evidence.

`measure.py` treats a measurement that could not be made as a harness error (exit 2), never as a failed candidate. That
covers a benchmark that will not start or exits non-zero, a `result.json` with a missing, non-finite or negative
figure, a profiled run, and a base whose `plan_digest` differs between its own runs.

### The fixture

The benchmark's input is a local fixture in `coverage/memory_benchmark_data/`. It is gitignored and never committed,
because it is built from a real install. Build or rebuild it from the live Predbat add-on config directory with:

```sh
python3 coverage/memory_benchmark_tools/export_fixture.py --live <add-on config dir> --out coverage/memory_benchmark_data
```

The export reads the live `apps.yaml`, `predbat.db` (through a SQLite snapshot) and `cache/` read-only, and never reads
`secrets.yaml`. It writes:

- the last 7 days of history up to T0, with the real inverter's history re-keyed onto the dummy inverter's sensors and
  their starting totals;
- the Octopus and forecast cache entries, made fresh at T0;
- a generated `apps.yaml`, from `coverage/memory_benchmark_tools/apps_template.yaml`;
- `manifest.json`, holding T0 (the time of the export).

Identifiers are scrubbed in three ways:

- **Known values are replaced with fakes:** Octopus account numbers, MPANs and MPRNs, meter serials and device IDs,
  inverter serials, and the forecast key and location. Each is also replaced lower-cased and with dashes as underscores.
- **Device ids inside entity names are found by shape and renamed:** Solax sites and serials, chargers, Octopus devices
  (`benchidN`).
- **Personal attributes are replaced by category, at any depth:** coordinates, postcodes, names, addresses, emails,
  phone numbers and company. This covers zones and GE Cloud's account record.

The export then fails, and deletes its output, if any known real value survives in any case, or if anything still looks
like an email, a phone number, a postcode other than the fake, or real coordinates. The public Octopus vehicle catalogue
is not scanned. Every run's log gets the same personal-data check, so a log that looks like it holds one makes `ok`
false.

Predbat's own entities are kept, because it reads some of their history back; `--strip-outputs` leaves them out.

The benchmark only runs where the fixture is. With no fixture it exits 2, which `measure.py` reports as a harness error.

### Measured (26 Nov 2026, Apple M4 Max, Docker aarch64)

| What | Result |
| --- | --- |
| Baseline (main plus #6369), 12 runs | steady 101.2 to 105.3 MB, median 104.6; peak 136.3 to 137.9 MB; about 25 s a run |
| Determinism | the same `plan_digest` in all 12 runs, made over 16 minutes. The wall clock is frozen, so the time of day cannot reach the plan; a run hours later has not been made yet |
| After the review fixes (re-scrubbed fixture, predictions in the digest), 3 runs | steady 104.3 to 105.5 MB, peak 136.0 to 136.9 MB, one digest; the same two controls give +20 MB with the same digest, and a new digest |
| 20 MiB kept alive (control branch) | steady 124.5 MB (+19.9), peak 156.8 MB, same digest |
| Import rates 1% higher (control branch) | a different digest |
| `measure.py` with the same tree as base and candidate | a clean fail on the 25% target, not a harness error |

The 25% target in `board.toml` and `measure.py` (`STEADY_CUT`) means a candidate's steady RSS must come down to about
78 MB. Startup alone is about 74 MB, so the saving has to come both from what the plan cycles hold and from what
startup loads, the Python libraries included. On main at 7b86c544 the run loads 507 modules; the largest third-party
packages by compiled size are aiohttp (7.3 MiB with its multidict, propcache, frozenlist, yarl and attrs helpers taking
another 3.8 MiB), PyYAML (2.9 MiB) and ruamel.yaml (0.6 MiB), so two YAML libraries are loaded.

Noise is about 4 MB on steady between identical trees. The peak's own noise is 1 to 2 MB, so a strict "no higher than
main's" would fail unchanged peaks. The 10% limit (about 14 MB on main's 137 MB) is well clear of that noise, and still
stops a steady saving being bought with a much higher peak, which a memory-limited add-on runs out of.

The Octopus component needs #6369 (its token mint and GraphQL query catching connection errors) to run offline. Until
that is on main, a base of main fails every cycle.

## Running it

```sh
cd ~/predbat/batpred
board-runner run boards/predbat-memory/board.toml
```

The runner runs `measure.py` from the main checkout, so this directory and the benchmark must be on the checked-out
branch there. `board-runner status boards/predbat-memory/board.toml` shows where it got to. To continue after the
board stops at its intent limit, use the skill's `resume predbat-memory max_intents=<n>`.
