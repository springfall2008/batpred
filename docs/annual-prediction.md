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
the Open-Meteo ERA5 archive) and the current year.

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
(scenario 2) and the smart plan's charging rate (scenario 3): a smaller number (say 3.0
for a granny charger) spreads the same annual `car_charging_kwh` over a longer window
each day, while a larger one (up to a three-phase charger's 22 kW) charges it faster. It
must be greater than zero.

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

## Running it

```bash
cd apps/predbat
python3 annual_cli.py --config annual.yaml --out results.json
```

Other options: `--work-dir` (default `./annual_work`) sets where the headless Predbat
instance and the download cache live, and `--quiet` suppresses the per-month progress
lines written to stderr. A human-readable table is always printed to stdout; `--out`
additionally writes the full results document as JSON.

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
`pv_generated_kwh`, `battery_throughput_kwh`, `export_credit_p_estimate` and
`self_consumed_kwh` (plus `self_consumed_kwh_meaningful`, see below).

`export_credit_p_estimate` is **not** extra money on top of `cost_p` — `cost_p` already
prices every export minute at its real per-minute rate, so the export credit is already
inside it. `export_credit_p_estimate` is a cruder second estimate of that same income
(the day's flat average export rate applied to total export), kept only as a
human-readable "how much of that came from export" figure. Adding it to `cost_p`
double-counts export income.

`self_consumed_kwh` is `pv_generated_kwh` minus `export_kwh` and is approximate: when the
battery exports grid-charged energy (rather than genuine excess solar) it is understated.
If export exceeds generation for a scenario, `self_consumed_kwh` is clamped to zero and
`self_consumed_kwh_meaningful` is `False` for that scenario/month, flagging that the
figure should not be trusted there.

The top-level `annual` block sums the included months' scenarios and reports two savings
figures: `pv_battery_vs_none_p` (PV and battery vs. no system) and
`predbat_vs_baseline_p` (Predbat vs. the dumb timer baseline). If no month produced a
usable result, `annual.scenarios`, `annual.standing_charge_p` and `annual.savings` are
empty rather than a fabricated zero-cost year.

The `caveats` list in the results document records anything that could affect how much
to trust the numbers — a P10 fallback, a missing month's rate data, and the
`export_credit_p_estimate`/`self_consumed_kwh` notes above among them — and is worth
reading before quoting the totals.

## Limitations

- The Open-Meteo forecast archive only reaches back to about 2021. For earlier years the
  tool plans against actuals instead and P10 uses the flat fallback derate, which it
  states in the results' caveats — savings are likely overstated for those years.
- `self_consumed_kwh` is approximate, as described above.
- The forecast-versus-ERA5 gap includes systematic model bias as well as genuine
  forecast error, so measured solar uncertainty is slightly overstated.
- A month with no rate data, no usable weather days, or where every sampled day failed to
  plan is reported as `unavailable` and excluded from the annual total, rather than
  counted as zero.
- Heat pump, iBoost and gas modelling are not included.
