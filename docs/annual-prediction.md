# Annual prediction

The annual prediction tool projects a year of household electricity costs using the
real Predbat planning engine. For each month it reports three scenarios:

1. **No PV, no battery** — the counterfactual bill.
2. **PV and battery, without Predbat** — a battery charging on a static cheap-rate timer.
3. **PV and battery, with Predbat** — the optimiser's plan.

It is a standalone command line tool; it does not need Home Assistant or any hardware,
and it makes no changes to a running Predbat installation.

## How it works

For each month the tool picks sample days by irradiance percentile (ranked by actual PV
energy, not what was forecast), so the answer does not swing on whether the sampled days
happened to be sunny. Two samples per month is the default; each represents half the
month. On a battery-only run (no solar arrays configured) there is nothing to rank by, so
days are instead spread evenly across the calendar.

Each sampled day gets a 48-hour plan starting at midnight, but only the first 24 hours
are billed — the second day exists so the optimiser does not artificially drain the
battery at the horizon. Whatever charge is left at the end is valued, so a scenario
cannot look cheap by finishing empty.

Predbat plans against the **archived weather forecast** for that date and is costed
against **ERA5 actuals**. This matters: costing against the same series it planned from
would hand Predbat perfect foresight and overstate its savings.

Solar uncertainty (P10) is derived from measured forecast error — for each month, the
10th percentile of the actual-over-forecast daily energy ratio, taken across every day in
that month with both a usable actual and forecast reading. A month needs at least seven
such day pairs before its measured P10 ratio is trusted; below that it falls back to a
flat derate (0.7 by default, `pv10_derate_fallback`), and the run's caveats say so.

## Configuration

```yaml
annual:
  location:
    postcode: "SW1A 1AA"        # or latitude/longitude
  year: 2025                     # defaults to the most recent complete calendar year

  solar:                         # omit for a battery-only run
    - kwp: 5.6
      declination: 35            # pitch in degrees, default 35
      azimuth: 180               # 180 = south, default 180
      efficiency: 0.95           # default 0.95, must be greater than 0 and at most 1

  battery:                       # omit for a PV-only run
    size_kwh: 9.5
    inverter_kw: 5.0
    export_limit_kw: 5.0         # defaults to inverter_kw
    hybrid: true                 # false = AC coupled
    charge_rate_kw: 3.6          # defaults to inverter_kw
    discharge_rate_kw: 3.6       # defaults to inverter_kw

  load:
    annual_kwh: 3800
    shape: flat                  # night | day | flat
    car_charging_kwh: 2500       # annual, 0 to disable
    car_rate_kw: 7.4             # charger power, default 7.4, must be greater than 0

  tariff:
    import_octopus_url: "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{dno_region}/standard-unit-rates/"
    export_octopus_url: "..."
    dno_region: "A"              # required when a URL contains {dno_region}
    standing_charge_p_per_day: 60.0

  samples_per_month: 2
  debug: false                   # keep each sampled day's plan, see Debugging a run
```

At least one of `solar` or `battery` must be given — with neither there is nothing to
evaluate. `annual.location` needs either `postcode` or both `latitude` and `longitude`;
if both are given, latitude/longitude wins.

Octopus product codes are region-suffixed. If your tariff URL contains `{dno_region}`
you must also set `dno_region` to your region letter (`A` for Eastern England, and so
on) — the config is rejected up front with an error naming the offending field, rather
than left to 404 at fetch time and reported as an unavailable month, which would look
like an Octopus outage rather than a config mistake.

Numeric fields are range-checked and a bad value is rejected with an explanatory message
rather than silently producing a nonsense result: `kwp`, `size_kwh`, `inverter_kw`,
`charge_rate_kw` and `discharge_rate_kw` must all be greater than zero; `efficiency` and
`pv10_derate_fallback` must be greater than zero and at most one; `samples_per_month`
must be a whole number of at least one; and `year` must be between 1940 (the start of
the Open-Meteo ERA5 archive) and the most recently completed calendar year - the current,
still-in-progress year is rejected, since Open-Meteo answers a mid-year request with
short but internally-consistent data that looks complete, and it would then be cached
permanently as if it were.

Instead of `annual_kwh`, `shape` and `car_charging_kwh` you may supply real consumption:

```yaml
  load:
    octopus:
      api_key: !secret octopus_key
      account_id: A-1234ABCD
```

These two forms are **mutually exclusive** and supplying both is rejected. The Octopus
consumption series already includes any EV charging, so accepting both would
double-count it. The trade-off is that a car baked into the meter data cannot be
smart-planned separately. A day missing from the downloaded consumption falls back to a
synthetic UK-average profile rather than being billed as zero. `car_rate_kw` has no
effect on an Octopus load and is ignored there — there is no separately-tracked car
energy to apply a charging rate to.

`car_rate_kw` is the charger's power, used to size both the dumb timer's charge window
(scenarios 1 and 2, which share the same fixed timer) and the smart plan's charging rate
(scenario 3): a smaller number (say 3.0 for a granny charger) spreads the same energy over
a longer window, while a larger one (up to a three-phase charger's 22 kW) charges it
faster. It must be greater than zero.

Instead of an Octopus URL you may give a fixed rate structure:

```yaml
  tariff:
    rates_import:
      - start: "00:30:00"
        end: "05:30:00"
        rate: 7.0
      - start: "05:30:00"
        end: "00:30:00"
        rate: 28.0
    rates_export:
      - rate: 15.0
```

### How car charging is spread across the year

The car does **not** charge a little every day. Spreading an annual figure evenly across
365 days would make every top-up short enough to fit inside the cheapest overnight
window, so a dumb timer would price just as well as Predbat and the car would contribute
almost nothing to the measured saving. Real sessions are large enough to overflow a short
cheap band, and that overflow is where smart charging earns its money.

The schedule is derived from `car_charging_kwh` and `car_rate_kw` rather than configured
directly:

- One session a week, carrying the whole week's energy.
- If that session would run longer than **six hours** at the configured rate, the week's
  energy is split across as many sessions as needed to bring each under six hours, up to
  a maximum of one a day.

Each sampled day is then planned **twice** — once carrying a full session, once with no
car — and the two results are blended by how often charging actually happens that week.
Blending each sampled day, rather than setting aside separate "car" and "no car" sample
days, keeps the irradiance stratification intact instead of tying charging state to how
sunny the sampled day happened to be.

Two consequences worth knowing. A configuration with a car doubles the number of plan
runs, so it takes roughly twice as long. And if even one session a day cannot get under
six hours (a very low charge rate against very high mileage), the tool still uses seven
sessions but logs a warning: the sessions run long, so the overflow effect — and
therefore Predbat's advantage — is understated.

## Using the web interface

The Predbat web UI has an **Annual** tab alongside Dash, Plan, Entities, Charts and
Compare. Unlike the rest of the UI it needs neither Home Assistant nor a configured
Predbat instance — it is the tool a prospective buyer reaches for before they have
installed anything, as well as the quick option for an existing user.

### The form

The form prefills from your live Predbat setup wherever it can: location, solar arrays,
battery capacity and inverter/export limits, and any Octopus import/export tariff URLs
and DNO region already configured. If your `octopus_api_key` and `octopus_api_account`
are both set, they are filled in too and **Import from Octopus** is selected, since your
real metered consumption models the year far better than the synthetic profile. Only a
complete pair counts — a key with no account cannot download anything, so an incomplete
one is ignored rather than offered as a run that would fail partway through.

Anything it cannot determine — most commonly the
whole form, on an instance with no battery and no solar array configured — falls back to
example values for a plausible UK home, and a banner above the form says so explicitly
("Predbat isn't configured yet — these are example values, edit them to match your
home"). When your own setup supplies a battery or a solar array, the banner is absent and
the fields it can read are the real ones; any remaining gaps still use the same example
values, so the form is always complete rather than partially blank.

The **Tariff** dropdown lists a curated set of built-in Octopus products (Agile, Cosy,
Flux, Intelligent Go and so on) plus, if your `apps.yaml` has a `compare_list`, your own
entries from it — a user entry with the same id as a built-in replaces it rather than
appearing twice. Picking an entry fills in the import/export URL fields beneath it, and
those fields stay editable afterwards, so a dropdown choice is a starting point rather
than a lock. If the chosen URL contains `{dno_region}` (as the Octopus product codes do),
the **Octopus region letter** field is required; leaving it blank is rejected up front,
with the field named in the error, rather than left to fail as a 404 partway through the
run. Choosing **Custom** clears the URL fields for hand-entered rates.

Every field the CLI's `annual.yaml` accepts has a form equivalent, including the
manual-usage/Octopus-consumption choice under **Load** and the year, sample count and P10
fallback derate under **Advanced**. **Save settings** stores the configuration without
running it, so you can park a half-adjusted setup and come back to it; **Run simulations**
validates, stores it and starts a run.

### Running

A run takes roughly one to three minutes with the default two samples per month, or two
to six minutes with a car configured (each sampled day is planned twice — with and
without a charging session — to work out how often the car overflows the cheap window).
Once started it shows a progress bar with the current step and elapsed time, and it keeps
running on the server if you navigate away or close the tab — come back to the Annual tab
later and the same run is still there, or already finished. Only one run is active at a
time: submitting **Run** again while one is already in progress does not queue a second
run or interrupt the first, though any form edits you made are still saved.

**Cancel** stops the running job. When the run finishes, the tab you pressed **Run
simulations** in goes straight to the results.

Any other tab you have open only gets a **view results** link rather than being
navigated. The progress poll runs in every open tab, so reloading them all would
silently discard whatever had been typed into a form somewhere else — an earlier
version of the page did exactly that. Only the tab that actually started the run
follows the completion.

### Comparing runs

Every completed run is saved automatically — there is nothing to press — and the last
twenty are kept, each labelled with a short summary of its
configuration (battery size, solar size, tariff) rather than just a timestamp. A selector
above the results lets you switch between them — instantly, with no re-run — so you can
compare a 5 kWh battery against a 10 kWh one, or two tariffs, side by side. Each stored
run can also be downloaded as its raw JSON results document.

### The results view

The results view mirrors the CLI's output: an annual totals table, the chart below, a
month-by-month breakdown and the run's caveats. One thing carries over unchanged from how
the engine reports it, and matters for reading the numbers correctly: a month with
`status: unavailable` is left out of the chart and the totals — it is never drawn as a
zero-cost bar or counted as a free month.

## Running it

```bash
cd apps/predbat
python3 annual_cli.py --config annual.yaml --out results.json
```

Other options: `--work-dir` (default `./annual_work`) sets where the headless Predbat
instance and the download cache live, and `--quiet` suppresses only the per-month
progress lines written to stderr. Warnings - a P10 fallback, missing rate data, a failed
sample day, a car-charging shortfall - are never suppressed, `--quiet` or not: failures
stay visible. A human-readable table is always printed to stdout; `--out` additionally
writes the full results document as JSON.

A run takes roughly one to three minutes with the default two samples per month: 24 plan
calculations (12 months × 2 samples) plus the weather, tariff and (if configured)
Octopus consumption downloads, which are cached between runs in `--work-dir`.

## Reading the results

Each month in the results JSON has a `status`:

- `ok` — every sampled day planned successfully.
- `degraded` — some sampled days failed to plan or cost; the survivors are reweighted
  so the month still represents a full month, and the failed dates are listed under
  `failed_days`. It is still included in the annual totals.
- `unavailable` — no rate data, no usable weather days, or every sampled day failed.
  Excluded from the annual totals entirely, rather than counted as zero.

Within each included month, every scenario reports `cost_p`, `import_kwh`, `export_kwh`,
`pv_generated_kwh`, `battery_throughput_kwh` and `export_credit_p_estimate`.

`export_credit_p_estimate` is **not** extra money on top of `cost_p` — `cost_p` already
prices every export minute at its real per-minute rate, so the export credit is already
inside it. `export_credit_p_estimate` is a cruder second estimate of that same income
(the day's flat average export rate applied to total export), kept only as a
human-readable "how much of that came from export" figure. Adding it to `cost_p`
double-counts export income.

There is deliberately no self-consumption figure. It cannot be derived from these
totals: `pv_generated_kwh − export_kwh` assumes all export comes from PV, which is false
whenever the battery exports grid-charged energy — on an arbitrage tariff such as
Intelligent Octopus Go, export routinely exceeds generation and the subtraction goes
negative. Reporting a clamped zero there would look like a real measurement rather than
a broken one, so the field was removed instead. Measuring it properly needs a
per-minute PV-to-load accumulator inside the prediction engine.

The top-level `annual` block sums the included months' scenarios and reports two savings
figures: `pv_battery_vs_none_p` (PV and battery vs. no system) and
`predbat_vs_baseline_p` (Predbat vs. the dumb timer baseline). If no month produced a
usable result, `annual.scenarios` and `annual.standing_charge_p` are `null` and
`annual.savings` is an empty object (`{}`), rather than a fabricated zero-cost year.

**`predbat_vs_baseline_p` is not equally hard to beat on every tariff.** The "without
Predbat" baseline charges in the cheapest contiguous band of the day (mirroring
Predbat's own `calculate_yesterday()` savings baseline, deliberately kept consistent with
it rather than made tariff-aware here). On a banded tariff such as Economy 7, Cosy or
Flux, where the cheap rate holds for several contiguous hours, that band is almost the
whole cheap period, so the baseline is a strong comparator and Predbat's measured saving
mostly reflects genuine optimisation. On a half-hourly tariff such as Agile, where the
day's single cheapest rate is often just one 30 minute slot, the same rule yields a much
narrower baseline window - a comparator that is easier to beat - so `predbat_vs_baseline_p`
reads more flattering on Agile than the equivalent system would on a banded tariff. This
is a property of the comparator, not of Predbat performing differently; treat cross-tariff
comparisons of this figure with that in mind.

The `caveats` list in the results document records anything that could affect how much
to trust the numbers — a P10 fallback, a missing month's rate data, and the
`export_credit_p_estimate` note above among them — and is worth
reading before quoting the totals.

## Debugging a run

When a figure looks wrong, a normal run gives you no way to see why: you get the monthly
totals and nothing about the plans that produced them. Ticking **Save plans for
debugging** on the Annual tab — or setting `debug: true` in the config file — keeps the
plan for every sampled day and makes it viewable.

What is kept is the plan as it was actually billed: the same charge and export windows,
against the same PV and load series the cost came from. That is what makes it usable for
cross-checking. A plan drawn against a different series would look plausible and prove
nothing.

Every scenario is kept, not just Predbat's — `no_pvbat` and `without_predbat` too, so you
can see what the two baselines did rather than inferring it from their totals. When a car
is configured each sampled day is planned twice, once with a charging session and once
without, and the month's figures blend the two (see [How it works](#how-it-works)); both
legs are kept and labelled, since a suspicious monthly figure may come from either.

The results page then offers a plan viewer below the monthly table, with a day and
scenario selector. It renders with exactly the same code as the live
[plan page](predbat-plan-card.md) — same columns, same colours, same debug-column toggle —
so anything you already know about reading a Predbat plan applies unchanged.

Two things to know before turning it on:

- **The saved run gets much larger.** A year at the default two samples per month keeps
  72 plans (144 with a car configured). Runs are stored through the same twenty-run
  rotation as any other, so a debug run displaces older runs at the same rate.
- **It does not change the numbers.** The flag only retains plan data that the engine
  already computes; it does not enable Predbat's own `debug_enable`, which would disable
  the prediction kernel and slow every plan down substantially.

## Limitations

- The Open-Meteo forecast archive only reaches back to about 2021. For earlier years the
  tool plans against actuals instead and P10 uses the flat fallback derate, which it
  states in the results' caveats — savings are likely overstated for those years.
- Self-consumption is not reported, for the reason described above.
- The forecast-versus-ERA5 gap includes systematic model bias as well as genuine
  forecast error, so measured solar uncertainty is slightly overstated.
- A month with no rate data, no usable weather days, or where every sampled day failed to
  plan is reported as `unavailable` and excluded from the annual total, rather than
  counted as zero.
- Heat pump, iBoost and gas modelling are not included.
