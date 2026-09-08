# Annual Prediction Web UI — Design

Date: 2026-07-26
Status: Approved (design review complete)

## Goal

Add an **Annual** tab to the Predbat web interface that lets a user fill in
their home's details, run the annual prediction engine, watch its progress, and
read the result as annual totals plus a month-by-month chart.

This is the UI half of the annual prediction tool. The engine — `annual.py`,
`annual_weather.py`, `annual_load.py`, `annual_profiles.py`, `annual_tariff.py`,
`annual_cli.py` — already exists and is specified in
`docs/superpowers/specs/2026-07-25-annual-prediction-tool-design.md`. This spec
adds no modelling; it exposes what is there.

## Scope decisions (agreed)

- **The run happens in a subprocess**, not in the web server's event loop.
- **Form values prefill from the live Predbat instance where possible**, and are
  saved to a config file the CLI consumes directly.
- **Common fields up front, advanced collapsed.**
- **Monthly results as grouped bars**, three per month.
- **Self-hosted only for now** — no job queue, no tenancy, no quotas.
- **It must work with Predbat entirely unconfigured**, and defaults to a typical
  UK system so a visitor can run it immediately.
- **A tariff dropdown**, built from a curated catalogue merged with the user's
  own `compare_list` if they have one.
- **The last five runs are kept**, in Storage, with a selector to switch between
  them.
- **Same branch, one PR** — this builds on `feat/annual-prediction-tool`, since
  the UI cannot work without the engine.

## Why a subprocess

`AnnualPredictor.run()` is `async`, but the planning inside it is one to three
minutes of *synchronous* CPU work — two to six with a car, since a car config
plans each sampled day twice. Awaiting that from aiohttp would freeze the whole
Predbat web interface, and the five-minute optimiser loop shares that event
loop.

A subprocess also sidesteps a concrete hazard: `create_headless_predbat()`
mutates `os.environ["PREDBAT_APPS_FILE"]` and writes an `apps.yaml` into its
work directory. Both are process-global, so an in-process run would race with
the live instance and with any second run.

The boundary pays a second dividend. Because the child builds its own headless
`PredBat` from a minimal `apps.yaml`, the **engine needs nothing from the user's
configuration**. Only the form's prefill touches live state, and that is
best-effort — which is what makes the unconfigured case work at all.

## Architecture

| File | Responsibility |
|---|---|
| `apps/predbat/tariff_catalogue.py` | **New.** The curated tariff list as data, plus the mapping from Compare's key names to the annual engine's. No logic beyond lookup and merge. |
| `apps/predbat/web_annual.py` | **New.** The tab: form rendering, config load/save, results rendering. |
| `apps/predbat/annual_job.py` | **New.** Subprocess lifecycle only — spawn, parse progress, track state, cancel, reap. No HTML. |
| `apps/predbat/annual_store.py` | **New.** The run ring: save, list, load, evict, and label generation. Takes a Storage object; used by the web tab. |
| `apps/predbat/web.py` | **Modify.** Five route registrations delegating to `web_annual.py`. |
| `apps/predbat/web_helper.py` | **Modify.** One nav link, alongside the others at the `<a href='./compare'>Compare</a>` block. |
| `apps/predbat/annual_cli.py` | **Modify.** Add a `--machine` mode: results JSON on stdout, progress JSON on stderr, no human table. |

`web.py` is already 5,730 lines and `web_helper.py` 8,903, so the tab gets its
own module rather than growing either — following the `web_metrics_dashboard.py`
precedent.

**The split that matters:** `annual_job.py` knows nothing about HTML and
`web_annual.py` knows nothing about process handling. The job control can then
be tested by driving a stub process with no aiohttp request in sight, which is
where the bugs will be.

### Routes

| Route | Purpose |
|---|---|
| `GET /annual` | The form, plus the last run's results if any |
| `POST /annual` | Save the config without running |
| `POST /annual_run` | Validate, save, spawn |
| `GET /annual_status` | JSON, polled once a second |
| `POST /annual_cancel` | Terminate a running job |

### Persistence

**The config** is `annual.yaml` in `config_root`, alongside `comparisons.yaml` —
handed to the subprocess as `--config`, hand-editable, and surviving restarts.

**The results go to Storage**, not a bare file, per CLAUDE.md. The Storage
component is reached the way the rest of the codebase reaches it:
`self.base.components.get_component("storage")`.

The **last five runs** are kept:

| Storage key | Contents |
|---|---|
| `annual` / `runs_index` | Newest-first list of `{id, timestamp, label, months_included, status}` |
| `annual` / `run_<id>` | That run's full results document |

Saving appends to the index and deletes the blob of anything falling off the
end, so the ring never leaks entries.

`id` is timestamp-derived. `label` is generated from the config that produced the
run — for example *"9.5 kWh battery · 5.6 kWp · Agile"* — because a selector
listing five bare timestamps tells the user nothing about which run was which.

**The simulation returns its results; persisting them is the wrapper's job.**
`AnnualPredictor.run()` keeps returning the results document, as it does today.
It does not write them anywhere. Its `storage=` argument stays, but only for
what it was always for — caching weather and rate downloads, which is an input
concern.

The subprocess hands the results back over the pipe:

| Stream | Carries |
|---|---|
| stdout | The results document, one JSON object, on completion |
| stderr | Progress, one JSON object per line, as it runs |

Each wrapper then decides where results go. The web tab parses stdout and saves
through `annual_store.py` into the live Storage component. A human running
`annual_cli.py` gets the table on stdout as before, or `--out` to write a file.

**This is why the child never needs reach into the parent's Storage** — the
process boundary carries everything, so no filesystem or shared backend is
assumed on either side. A results document without `--debug` is tens of
kilobytes, which is unremarkable over a pipe; `--debug` retains per-sample HTML
plans and is deliberately not used by the web path.

**Forward compatibility.** A stable `id` and a human label in the index are
exactly what a later *compare runs* feature needs, so it can be added without
migrating stored results. That feature is out of scope here.

### Progress protocol

`annual_cli.py` currently writes progress to stderr as `[3/12] Month 03/2025`
and a human-readable table to stdout. Parsing prose would couple the parent to
wording that will get reworded, and the results cannot simply join the table on
stdout.

A single `--machine` flag switches both streams at once, rather than two flags
that interact:

| Stream | Default | Under `--machine` |
|---|---|---|
| stdout | Human-readable table | The results document as one JSON object |
| stderr | `[3/12] Month 03/2025` | `{"completed": 3, "total": 12, "message": "..."}` per line |

The human-readable behaviour stays the default so the CLI remains pleasant by
hand, and the web tab always passes `--machine`.

## The form

Six groups: **Location**, **Solar**, **Battery**, **Load**, **Tariff**, and a
collapsed **Advanced** holding `year`, `samples_per_month`,
`pv10_derate_fallback`, per-array `efficiency`, and the fixed-rate-band editor.

### Prefill is best-effort, field by field

Each value is read from the live instance where available and falls back to a
typical-UK default otherwise, **independently**. A half-configured Predbat gets
its real inverter limits alongside example solar, rather than all-or-nothing.

**Read from the in-memory args dictionary, never from `apps.yaml` on disk.**
`self.base.args` is already the parsed configuration; the file itself may not
exist at all in some deployments, so re-reading it would fail exactly where the
unconfigured case matters most. Use `get_arg()` so indirection and defaults
behave as they do everywhere else in Predbat.

| Form field | Source |
|---|---|
| Solar arrays | `open_meteo_forecast`, else `forecast_solar` — both are already lists of `{kwp, declination, azimuth, efficiency}`, a direct shape match |
| Location | `latitude`/`longitude` or `postcode` from the same solar entries |
| Inverter and export limits | `inverter_limit`, `export_limit` |
| Hybrid or AC coupled | `inverter_type` |
| Tariff URLs and region | `rates_import_octopus_url`, `rates_export_octopus_url`, `dno_region` |
| Tariff dropdown extras | `compare_list` |
| Battery capacity | `soc_max` — a zero or absent value means it is not set, so fall back to the default and let the user adjust |

Anything absent falls back to the typical-UK value. No field is required to be
present for the form to render.

When the live instance supplied neither a battery nor a solar array — the two
that signal a configured system — a banner says so:

> Predbat isn't configured yet — these are example values, edit them to match
> your home.

Without that line the defaults could be mistaken for a reading of the visitor's
actual system, which would make the result look authoritative when it is
illustrative.

### The unconfigured case is a first-class requirement

The tab must render, validate and run against a Predbat with nothing set up —
no inverter, no tariff, no meaningful `apps.yaml`. This is not a graceful
degradation afterthought: it is the path a prospective buyer takes, and
eventually the path an unregistered Predbat.com visitor takes.

Defaults for that visitor describe a plausible system they might buy — around
5 kWp of solar, a 9.5 kWh battery on a 5 kW hybrid inverter, 3,800 kWh/year of
flat load, no car, a price-cap tariff — so Run works immediately and tweaking
follows.

### Load

A radio pair mirroring the engine's own exclusivity rule:

- *Enter my usage* — annual kWh, day/night/flat shape, car kWh, charger kW
- *Import from Octopus* — API key, account ID

Choosing one greys the other. The engine already rejects both being set, because
the Octopus consumption series contains any car charging; the UI expresses a
constraint that exists rather than inventing one.

### Tariff

A dropdown built from `tariff_catalogue.py` merged with the user's `compare_list`
if configured. Selecting an entry **fills in** the import and export URL fields,
which stay visible and editable — so it is obvious what was chosen and the
custom path still works. A "Custom…" entry leaves them blank.

`dno_region` sits beside it and is required whenever the chosen URL contains
`{dno_region}`. The engine rejects that combination up front; the form should
catch it before spending minutes to fail.

### The catalogue

The ~14 tariffs currently living as a commented-out `compare_list` template in
`apps/predbat/config/apps.yaml` — Agile, Go, Intelligent Go, Flux, Cosy, Snug,
Intelligent Flux, Eon Next Drive, price cap with SEG, and their export pairings.

Those entries use Compare's key names, so the catalogue owns the mapping:

| Compare | Annual engine |
|---|---|
| `rates_import_octopus_url` | `import_octopus_url` |
| `rates_export_octopus_url` | `export_octopus_url` |
| `rates_import` / `rates_export` | unchanged |

Extracting this as its own module means Compare could later read the same source
instead of every user hand-copying a commented block into `apps.yaml`. **Compare
is not changed by this work** — the extraction just stops a second copy being
created.

## Running

`POST /annual_run` saves `annual.yaml`, validates it through the engine's own
`validate_config`, and only then spawns:

```
python3 annual_cli.py --config <config_root>/annual.yaml \
                      --out <config_root>/annual_results.json \
                      --progress-json
```

`annual_job.py` holds a single `AnnualJob`: state (`idle` / `running` /
`complete` / `failed` / `cancelled`), progress counters, start time, last error,
and the process handle. One run at a time — a second Run is refused rather than
spawning a competitor for the same CPU.

The page polls `GET /annual_status` once a second for
`{state, completed, total, message, elapsed}`. Because the job is server-side,
navigating away and back shows a run still in progress. Cancel sends `SIGTERM`,
then `SIGKILL` if the child does not exit.

### Two limitations, stated rather than hidden

- **A Predbat restart orphans a running child.** Job state resets to idle and
  the tab reports that the last run did not complete, rather than showing a
  progress bar stuck forever.
- **Validation happens twice.** The browser checks for immediate feedback;
  `validate_config` inside the subprocess is the authority. The form's copy is a
  convenience and never the gate.

## Results

Three things, top to bottom.

**Annual totals** — the three scenario costs, then the two figures people came
for: what PV+battery saves over nothing, and what Predbat adds over a dumb
battery. Standing charge is shown separately, since it is identical across
scenarios and folding it in would dilute the comparison.

**The monthly chart** — twelve months, three grouped bars each, in ApexCharts to
match the existing charts and their dark-mode styling.

**A monthly table** — per scenario: import, export, PV generated, self-consumed,
battery throughput, plus which days were sampled.

Three properties carry through from the engine and must survive into the UI:

- **Caveats are displayed, not buried.** The results document carries them: P10
  fallback, `export_credit_p_estimate` not being additive with `cost_p`, the
  Agile-versus-banded baseline asymmetry.
- **Unavailable and degraded months are marked**, never drawn as a zero-height
  bar. A zero bar reads as free electricity.
- **`self_consumed_kwh` is greyed with its reason** when
  `self_consumed_kwh_meaningful` is false, rather than showing a bare 0.

Above all three sits a **run selector** listing the stored runs newest-first by
their generated label and timestamp, so a user can flip between "with a 5 kWh
battery" and "with a 10 kWh battery" without re-running either. Selecting a run
re-renders the totals, chart and table from that run's stored document.

Plus a "last run" timestamp and a link to download the raw JSON of the selected
run.

## Failure handling

Failures stay visible, matching the engine's own contract:

| Condition | Behaviour |
|---|---|
| Config invalid | Rendered inline, form still populated with what was entered |
| Subprocess exits non-zero | Exit code and last stderr lines shown, not a blank page |
| Results file missing or unreadable | Says so, rather than rendering an empty chart |
| Run already in progress | Second Run refused with a clear message |
| Predbat restarted mid-run | Reported as "did not complete", state reset to idle |

## Testing

All offline. No network, and **no test spawns the real engine** — `annual_job`
is driven with a stub script.

- **`tariff_catalogue`** — the Compare-to-annual key mapping is right; user
  `compare_list` entries merge without clobbering built-ins; a malformed entry is
  skipped rather than breaking the dropdown.
- **`annual_job`** — progress lines parse; malformed lines do not crash the
  parser; cancel terminates; a non-zero exit is reported as failed; a second Run
  while running is refused.
- **Run history** — saving a sixth run evicts the oldest and deletes its blob;
  the index stays newest-first; a corrupt or missing run blob is reported rather
  than rendering an empty chart; labels are generated from the config.
- **`web_annual`** — **the form renders and validates against a completely
  unconfigured PredBat.** That is the acceptance criterion for the unconfigured
  requirement, and the one most likely to regress silently. Plus: prefill falls
  back per field; the load radio pair enforces exclusivity; a `{dno_region}` URL
  with no region is rejected before spawning.

## Out of scope

- Predbat.com multi-user support — no queue, tenancy or quotas. Revisit when
  that becomes the target.
- Changes to the Compare tab, including making it read `tariff_catalogue.py`.
- Comparing two stored runs side by side. The selector switches between them;
  it does not overlay them. The index is designed so this can be added later
  without migrating stored results.
- Any change to the prediction model itself.
