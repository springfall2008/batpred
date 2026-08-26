# Annual Prediction Tool — Design

Date: 2026-07-25
Status: Approved (design review complete)

## Goal

Add a standalone tool that projects a **year** of household electricity costs
using the real Predbat planning engine, so a prospective or existing user can
answer "what would a battery, or solar, or Predbat itself, actually save me
over a year?" without owning any of the hardware.

For each month the tool reports three scenarios:

1. **No PV, no battery** — the counterfactual bill.
2. **PV and battery, without Predbat** — a dumb battery charging on a static
   cheap-rate timer.
3. **PV and battery, with Predbat** — the optimiser's plan.

This spec covers the **prediction engine only**. The web UI that consumes its
output is a separate, later piece of work; the design's job here is to leave a
clean programmatic interface for it.

## Scope decisions (agreed)

- **Packaged as shipped modules, not a dev script** — new `annual_*.py` modules
  in `apps/predbat/` plus a thin CLI, following the `compare.py` precedent, so
  the web UI can import the engine directly later.
- **Drives the real `PredBat` object** — the tool injects state and calls
  `calculate_plan()` / `run_prediction()`. It does not re-implement the
  optimiser. Numbers are defensible because they come from the same code that
  runs live.
- **Two sampled days per month, configurable** — chosen by irradiance
  percentile, not by calendar position.
- **Real historical rates for the sampled date** — genuine Agile/Tracker/Flux
  shape and seasonal price movement.
- **Load is either synthetic or Octopus-derived, never both** — providing an
  Octopus API key together with `annual_kwh`/`car_charging_kwh` is a config
  validation error.
- **Car charging is smart under Predbat, timer-driven in the baselines** —
  Predbat is credited only for what it wins over an off-peak timer.
- **Predbat plans on the archived forecast and is billed on actuals** — it does
  not get perfect foresight, and P10 comes from measured forecast error rather
  than a constant.

## Architecture

Six new flat modules in `apps/predbat/`. Flat `annual_` prefixing matches repo
convention (no subpackages exist besides `tests/` and `config/`).

| Module | Responsibility | Depends on |
|---|---|---|
| `annual.py` | `AnnualPredictor` orchestrator, public API, config validation | all below, `PredBat` |
| `annual_weather.py` | Open-Meteo actuals + forecast archive clients → per-day PV kW series per array, plus monthly P10 ratios | shared GTI helper |
| `annual_load.py` | `LoadProfileSource`: synthetic and Octopus-consumption implementations | `annual_profiles` |
| `annual_profiles.py` | Embedded half-hourly domestic shape tables and monthly weights (data only) | nothing |
| `annual_tariff.py` | Per-date rate resolution: Octopus URL or basic rates | `basic_rates()` |
| `annual_cli.py` | Argument parsing, progress output, JSON and table writing | `annual` |

The boundary that matters: **`annual.py` performs no HTTP.** All network access
lives in `annual_weather.py`, `annual_tariff.py` and `annual_load.py`. That keeps
the web UI's future job to "build a config dict, call `AnnualPredictor.run()`,
render the returned JSON", and lets every module be tested against fixtures
rather than the network.

`annual_weather.py` never touches `PredBat`. `annual_tariff.py` does hold a
`PredBat` reference, legitimately: it calls `resolve_arg()` to expand a
templated Octopus URL and `basic_rates()` to expand a static rate structure -
both listed as its dependency above - rather than reimplementing either. That
reference is used only for those two calls, never to drive a plan or touch
live state, so it does not compromise the no-HTTP boundary above.

HTTP responses are cached through the Storage component's `fetch_cached()`
(per CLAUDE.md, never direct file access): one cached blob per array-year per
source for weather — actuals and forecast are separate entries — and one per
tariff-month for rates.

### Public interface

```python
class AnnualPredictor:
    def __init__(self, config: dict, log=None, storage=None): ...
    async def run(self, progress=None) -> dict: ...
```

`progress` is an optional callable receiving `(completed, total, message)` so
the future web UI can stream status. `run()` returns the results document
described under "Results" below.

## Reused existing code

The tool leans on four things that already exist, rather than reinventing them:

- **`calculate_yesterday()`** (`apps/predbat/output.py`) already runs exactly
  these three scenarios against a past day — zeroed-PV/`soc_max=0` for the
  no-system case, a cheapest-window dumb charge for the baseline, and the
  optimised plan. It is the template for the per-sample execution.
- **`Compare.apply_hardware_overrides()`** (`apps/predbat/compare.py`) already
  maps battery kWh / charge rate / inverter limit onto a `PredBat` instance.
- **The battery-value correction** `metric_end − metric_start` via
  `compute_metric()`, as `Compare.run_scenario()` applies it.
- **The GTI→kW model** in `apps/predbat/solcast.py` (`download_open_meteo_data`).

`Compare` itself is *not* reused as a class: it is coupled to
`fetch_sensor_data()` and live inverter state, and plans "now → +48h" rather
than an arbitrary historical date. Bending it to serve a second purpose would
damage it.

## Inputs

A single YAML file, or the equivalent dict from the future web UI. Every
optional field has a stated default so a minimal config is short.

```yaml
annual:
  location:
    postcode: "SW1A 1AA"        # or latitude/longitude; resolved via postcodes.io
  year: 2025                     # defaults to the most recent complete calendar year

  solar:                         # list — multiple arrays supported; omit for battery-only
    - kwp: 5.6
      declination: 35            # pitch in degrees. Default 35
      azimuth: 180               # 180 = south. Default 180
      efficiency: 0.95           # system loss. Default 0.95

  battery:                       # omit entirely for a PV-only run
    size_kwh: 9.5
    inverter_kw: 5.0
    export_limit_kw: 5.0
    hybrid: true                 # false = AC coupled
    charge_rate_kw: 3.6          # defaults to inverter_kw
    discharge_rate_kw: 3.6       # defaults to inverter_kw

  load:
    annual_kwh: 3800             # mutually exclusive with load.octopus
    shape: flat                  # night | day | flat
    car_charging_kwh: 2500       # annual; 0 to disable
    octopus:                     # mutually exclusive with annual_kwh/car_charging_kwh
      api_key: !secret octopus_key
      account_id: A-1234ABCD

  tariff:
    import_octopus_url: "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{dno_region}/standard-unit-rates/"
    export_octopus_url: "..."
    # or: rates_import: [{start: "00:30:00", end: "05:30:00", rate: 7.0}, ...]
    standing_charge_p_per_day: 60.0

  samples_per_month: 2
  pv10_derate_fallback: 0.7      # only used when the forecast archive lacks the year
```

### Input rules

- **Battery and solar are each optional.** Omitting `battery:` produces a
  two-scenario run (no-PV/no-battery versus PV-only); omitting `solar:`
  produces a battery-only run. The tool must not force the user to configure
  the very thing they are trying to evaluate.
- **`load.octopus` versus `load.annual_kwh` is exclusive**, and supplying both
  is rejected at config-load time with a clear message. The Octopus consumption
  series already contains any car charging, so accepting both would
  double-count it. The consequence — a real-data user gets an accurate baseline
  but no separately smart-planned EV — is documented in the CLI help and the
  results document.
- **`{dno_region}` is resolved** through Predbat's existing region mechanism, so
  a postcode selects the correct regional tariff. Silently defaulting to region
  A would quote the wrong prices with no visible symptom.
- **Secrets are scrubbed** from the results document and any debug output,
  matching the existing `_key` / `password` redaction in `create_debug_yaml()`.
- **`year` is accepted for any date the actuals archive covers**, but years
  before the forecast archive begins (around 2021–2022) lose the forecast
  grounding and fall back as described under "P10 estimate". The default — the
  most recent complete calendar year — is always within coverage.

## Data flow

### 1. Weather

**Two** Open-Meteo requests per array, each covering the whole year plus one
buffer day (the last sampled day needs a following day for its 48h plan). Both
endpoints serve `global_tilted_irradiance` with `tilt` / `azimuth` and both
accept `start_date` / `end_date`.

**Actuals** — ERA5 reanalysis, what genuinely happened. Data back to 1940.

```
https://archive-api.open-meteo.com/v1/archive?latitude=..&longitude=..
  &start_date=YYYY-01-01&end_date=YYYY+1-01-01
  &hourly=global_tilted_irradiance,temperature_2m,wind_speed_10m
  &tilt=..&azimuth=..&wind_speed_unit=ms&timezone=UTC
```

**Forecast** — the archived short-range forecast for those same past dates, i.e.
what Predbat would actually have been looking at. Coverage starts around
2021–2022 depending on model.

```
https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=..&longitude=..
  &start_date=YYYY-01-01&end_date=YYYY+1-01-01
  &hourly=global_tilted_irradiance,temperature_2m,wind_speed_10m
  &tilt=..&azimuth=..&wind_speed_unit=ms&timezone=UTC
```

The field names match the live forecast endpoint already used, so one conversion
serves all three.

**The GTI→kW conversion is extracted, not copied.** The cell-temperature model,
the −0.4%/°C derate, the trapezoidal hourly integration, and `convert_azimuth`
move out of `SolarAPI.download_open_meteo_data()` into a shared helper that both
`solcast.py` and `annual_weather.py` call. Duplicating it would guarantee the
two drift; the azimuth convention in particular (Open-Meteo uses 0 = south) is
easy to get silently wrong and produces plausible-looking but incorrect output.

Open-Meteo's Previous Runs API would give cleaner fixed-lead-time forecasts, but
it accepts only `past_days` rather than a date range, so it cannot reach an
arbitrary past year. It is not used.

#### Plan on forecast, bill on actuals

Predbat plans against the **forecast** series and is costed against the
**actuals** series. Without this the tool would grant Predbat perfect foresight
and overstate what it can really achieve.

Mechanically: `calculate_plan()` runs with `pv_forecast_minute` set to the
forecast series; then, before the costing `run_prediction()`, `self.prediction`
is rebuilt from the actuals step data and the plan's best windows are replayed
against it. This is the same Prediction-swap that `calculate_yesterday()` and
`Compare.run_scenario()` already perform.

This applies to scenario 3 only. Scenario 1 has no PV, and scenario 2's charge
window is derived from rates rather than from the PV forecast, so neither
depends on forecast quality — both run directly on actuals. `pv_generated_kwh`
in the results is always taken from the actuals series.

#### P10 estimate

The archive exposes no ensemble members, so P10 is derived from **measured
forecast error**. For each month, the daily energy ratio
`r = actual_kWh / forecast_kWh` is computed across every day of that month; the
10th percentile of `r` becomes that month's `p10_ratio`, and the planning P10
series is the forecast series scaled by it (clamped to ≤ 1). Location- and
season-specific, and grounded in data rather than a constant.

Why this matters more than it looks: because the plan is costed against a fixed
actuals series, hedging can only ever *add* cost — a pessimistic P10 makes
Predbat over-charge and understates its own savings, while `P10 = P50` yields a
perfect-foresight upper bound. P10 is therefore not a free parameter, and it
must not be derived from within-month climatological spread, which is far wider
than 24-hour-ahead forecast error and would bias the tool against itself.

Two honest caveats, recorded in the results document:

- The forecast-versus-ERA5 gap includes systematic model bias, not purely
  forecast error, so measured uncertainty is slightly overstated.
- P90 is not used. The planner consumes only `pv_forecast_minute` and
  `pv_forecast_minute10`; P90 reaches no decision, so computing it would be
  decoration.

**Fallback.** If the forecast archive does not cover the requested year, the
tool logs a clear warning, plans on actuals, and applies a flat
`pv10_derate_fallback` (default 0.7 — `solcast.py`'s existing no-ensemble
fallback). The results document records that the fallback was used, so a
degraded run is never mistaken for a grounded one.

### 2. Load

`annual_load.py` produces a **forward cumulative kWh series keyed by absolute
minute**, assigned to `load_forecast`, with `load_forecast_only = True`.

This is the clean injection point: with `load_forecast_only` set,
`step_data_history()` ignores historical load entirely
(`apps/predbat/fetch.py`, in the `type_load and not forward` branch) and builds
the forward profile purely from `load_forecast`, which it reads via
`get_from_incrementing(..., backwards=False)`. No synthetic backwards history
needs to be fabricated.

**Synthetic path.** `annual_kwh × month_weight[m] / days_in_month` gives the
day's kWh, multiplied by a 48-point half-hourly domestic shape. The night/day
shape setting tilts energy between the 00:00–07:00 band and the daytime band
while preserving the daily total exactly — an invariant with a dedicated test.
The tilt magnitude is a named constant in `annual_profiles.py` (a fixed
proportion of the day's energy moved between bands) so it can be tuned against
real data without touching the tilt logic. Monthly weights carry the
winter/summer split, which drives much of the annual answer.

`annual_profiles.py` holds these tables as data only, with no logic, so they can
be revised against real measurements without touching behaviour.

**Octopus path.** Account lookup resolves MPAN and meter serial, then the
half-hourly consumption endpoint supplies each sampled date directly. Gaps fall
back to the synthetic profile for that date and are logged.

### 3. Tariff

One fetch per tariff per month using `period_from` / `period_to`, sliced per
sampled day. Per-month rather than per-day fetching keeps the API call count
low; the existing pagination handling in `download_octopus_rates_func()` still
applies. Basic rates go through `basic_rates()` unchanged.

### 4. Sample selection

Within each month, every day's total PV kWh from the **actuals** series is
summed across all arrays and the days sorted ascending — ranking on the forecast
would select days by what was predicted rather than by what the month really
contained. For `N` samples, the day at percentile `(i + 0.5) / N`
is taken for `i` in `0..N-1` — so `N=2` picks the 25th and 75th percentile days,
each representing exactly half the month. Selection is fully deterministic; the
same config always yields the same days.

Days without a valid following day are excluded from the candidate set, since
the 48h plan requires one.

### 5. Per-sample execution

State is reset, then `midnight_utc = D`, `minutes_now = 0`,
`forecast_minutes = 48 * 60`, `end_record = 24 * 60`. Three scenarios run
against the same day:

1. **No PV, no battery** — `Prediction(..., pv_zero, pv_zero, load, load,
   soc_kw=0, soc_max=0)` then `run_prediction([], [], [], [])`.
2. **Without Predbat** — dumb battery: the cheapest static charge window from
   `compute_rate_low_for_yesterday()` + `rate_scan_window()`, capped at
   `calculate_savings_max_charge_slots`, charging to 100%, with no export
   optimisation.

The car, where configured, charges in scenarios 1 and 2 on a fixed timer: the
same cheapest static window scenario 2 derives for the battery, extended as
needed to fit the session's kWh at the configured charge rate. Scenario 1 uses
that identical window even though it has no battery, so the only difference
between the three scenarios is the system being evaluated. Only scenario 3
plans the car smartly.

#### Charging is episodic, not a daily trickle

Spreading the annual car figure evenly across 365 days is wrong in a way that
matters. A 2,500 kWh/year smear is 6.85 kWh/day — under an hour at 7.4 kW —
which fits trivially inside any cheap overnight window, so the dumb timer gets
the cheap rate too and the gap between it and Predbat collapses to nothing. Real
owners charge in sessions of 20-40 kWh that can overflow a short cheap band
(Flux, Cosy), forcing part onto expensive rates under a timer while Predbat
splits the load across the cheapest half-hours. That overflow is where smart
charging earns its money, and smearing deletes it.

The schedule is derived, not configured:

- One session per week by default, carrying the whole week's energy.
- If that session would exceed **six hours** at the configured charge rate, split
  into as many sessions per week as needed to bring each under six hours,
  capped at seven (daily).

Each sampled day is then planned **twice** — once carrying a full session, once
with no car — and the two results are blended by how often charging actually
happens: `cost = f × with_car + (1 − f) × without_car`, where
`f = sessions_per_week / 7`. Blending per sampled day rather than dedicating
separate sample days keeps the irradiance stratification intact; every cost and
energy field blends linearly, and `pv_generated_kwh` is identical in both legs.

This doubles the plan count for configs with a car (24 → 48 runs/year). Configs
without one are unaffected.
3. **With Predbat** — `calculate_plan(recompute=True, publish=False)` then
   `run_prediction(charge_limit_best, charge_window_best, export_window_best,
   export_limits_best)`. The car uses real smart slot planning.

Each is billed over the **first 24 hours only**; the second day exists purely as
lookahead so the optimiser does not artificially drain the battery at the
horizon. Leftover or consumed charge is valued via the `compute_metric`
correction (`metric_end − metric_start`), so ending on a full battery is not
free.

### 6. State isolation

This is the principal risk of driving the real `PredBat` object. `PredBat` is a
large mutable object and derived state leaks between runs — the debug-case
harness already documents this for `dynamic_load_baseline` and
`battery_rate_max_export` (`apps/predbat/tests/test_single_debug.py`).

Between samples the tool resets an explicit allow-list of mutable fields, and a
test asserts that a single month run in isolation produces output identical to
that same month within a full-year run. Without that test this class of bug is
invisible: results stay plausible while silently depending on run order.

## Results

Monthly cost for a scenario is `Σ over samples (sample_daily_cost ×
days_in_month / N)`. Stratified percentile sampling gives each sample an equal
share of the month, so the weights are uniform.

Per month, per scenario:

| Field | Source |
|---|---|
| `cost_p` | `run_prediction` metric, battery-value corrected, excluding standing charge |
| `import_kwh` | `import_kwh_battery + import_kwh_house` |
| `export_kwh` | `export_kwh` |
| `export_credit_p` | `export_kwh` valued at the export rate |
| `pv_generated_kwh` | summed `pv_forecast_minute_step` over the billed 24h |
| `self_consumed_kwh` | `pv_generated_kwh − export_kwh`, clamped at 0 |
| `battery_throughput_kwh` | `battery_cycle` |
| `standing_charge_p` | `standing_charge_p_per_day × days_in_month` |

`standing_charge_p` is identical across scenarios and reported separately, so
savings comparisons are not diluted by a fixed cost neither system affects.

`self_consumed_kwh` is an approximation: when the battery exports grid-charged
energy, `export_kwh` exceeds the PV contribution and self-consumption is
understated. This is documented in the field description rather than papered
over with a decomposition the prediction does not actually track.

Annual totals are the sum of the twelve months, plus two derived savings
figures: PV+battery versus no-PV/no-battery, and Predbat versus without-Predbat.

Output is a JSON document — the future web UI's input — and a human-readable
table on stdout. `--debug` additionally retains the per-sample HTML plans; off
by default, since 24 retained plans make the payload large.

## Failure handling

**Failures are visible, never silent.**

| Condition | Behaviour |
|---|---|
| Missing archive weather for a sampled day | Substitute the next-nearest day within the same percentile stratum; log it |
| Forecast archive does not cover the requested year | Plan on actuals with `pv10_derate_fallback`; warn, and record the degradation in the results document |
| Forecast archive has gaps within a covered year | Exclude those days from the month's `p10_ratio` sample; if fewer than seven days remain, fall back for that month and record it |
| Missing rate data for a month | Mark the month `"status": "unavailable"`, exclude it from annual totals, and state the exclusion in the printed output |
| Octopus consumption gap for a sampled date | Fall back to the synthetic profile for that date; log it |
| Octopus account lookup failure | Fail the run with a clear message rather than silently degrading to synthetic |

A month must never quietly become zero. A zero month reads as "free
electricity" in a chart, and the user has no way to tell it apart from a real
result.

## Testing

All tests run offline against fixtures and are registered in `TEST_REGISTRY` in
`unit_test.py`, following the `tests/test_<feature>.py` convention.

- **Load profile** — shape tilts preserve the daily total exactly; the twelve
  monthly totals sum to `annual_kwh`.
- **Weather** — fixture GTI converts to known kW; the extracted shared helper
  produces output identical to `solcast.py`'s current path for the same inputs.
  This is what prevents the extraction being a silent regression.
- **P10 derivation** — a fixture pair of forecast and actual series yields the
  expected monthly `p10_ratio`; a year outside forecast-archive coverage falls
  back to `pv10_derate_fallback` and flags the degradation.
- **Plan-on-forecast, bill-on-actuals** — with a deliberately inflated forecast
  series, scenario 3's reported cost and `pv_generated_kwh` track the actuals,
  not the forecast. Without this test the Prediction swap could silently be
  skipped and every result would quietly assume perfect foresight.
- **Sample selection** — deterministic percentile picks for a known irradiance
  series; correct handling of a month with missing days.
- **Config validation** — Octopus key plus manual load is rejected; a missing
  `battery:` block yields a two-scenario run; `{dno_region}` resolves.
- **Scenario ordering** — on a synthetic day,
  `predbat_cost ≤ without_predbat_cost ≤ no_pvbat_cost`.
- **State isolation** — a single month run in isolation matches that month
  within a full-year run.
- **Rate fetch** — `period_from` / `period_to` slicing and pagination against a
  fixture.

## Performance

Twelve months at two samples is 24 `calculate_plan()` runs, roughly one to three
minutes. `AnnualPredictor.run()` accepts a progress callback so the later web UI
can stream status rather than block on a silent request.

## Out of scope

- The web UI (separate, later work).
- Heat pump, iBoost, or gas modelling.
- Multi-year averaging or typical-meteorological-year construction.
- A P90-based confidence band on the monthly figures. The forecast-error
  distribution would support one, but nothing consumes it and no decision
  depends on it.
- Tariff recommendation — the tool evaluates the tariff it is given.
