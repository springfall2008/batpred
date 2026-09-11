# What If Annual Prediction

"What would solar and a battery actually save me?" — this answers that, by projecting a
year of household electricity costs through the real Predbat planning engine rather than
a rule of thumb. For each month it reports four scenarios:

1. **No PV, no battery** — the counterfactual bill.
2. **PV only** — solar with no battery to store or arbitrage it.
3. **PV and battery, without Predbat** — a battery charging on a static cheap-rate timer.
4. **PV and battery, with Predbat** — the optimiser's plan.

Most people will use it through the **WhatIf** tab in the Predbat web interface, which is
what the next section covers. It also runs as a standalone command line tool for anyone
who wants to script it — see [Running it from the command
line](#advanced-running-it-from-the-command-line) at the end.

Either way it needs neither Home Assistant nor a configured Predbat instance, and it
changes nothing about a running installation: it is the tool a prospective buyer reaches
for before they have installed anything, as much as one for an existing user.

## Using the WhatIf tab

The Predbat web UI has a **WhatIf** tab alongside Dash, Plan, Entities, Charts and
Compare. Its pages are titled **What If Annual Prediction**.

WhatIf is three pages, not one — Configure (`/annual`), Results
(`/annual_view`) and Compare (`/annual_compare`) — sharing a tab strip with
previous/next arrows across the top of all three:

```text
◀   [ Configure ]  [ Results ]  [ Compare ]   ▶
```

The current page is marked in the strip. The arrows step through that order and stop
at the ends rather than wrapping — pressing "next" on Compare does not loop back round
to Configure. Configure holds the form and the **Run simulations** button; Results
holds the run selector, "what this run used", the annual totals, chart, month table,
cost and payback, the plan viewer and the caveats; Compare holds the run-comparison
table described below. Each is a real URL you can bookmark, so the browser's back button
and a saved link both behave.

### The form

The form prefills from your live Predbat setup wherever it can: location, solar arrays,
battery capacity and inverter/export limits, and any Octopus import/export tariff URLs
and DNO region already configured. If your `octopus_api_key` and `octopus_api_account`
are both set, those two boxes are filled in too, so choosing **Import from Octopus** is a
click rather than a paste. Only a complete pair counts — a key with no account cannot
download anything, so an incomplete one is left blank rather than offered as a run that
would fail partway through. The load source itself always starts on **Enter my usage**:
the Octopus option reads your import meter, which is only sound for a home with no solar
or battery fitted, so it is never selected for you.

Anything it cannot determine — most commonly the
whole form, on an instance with no battery and no solar array configured — falls back to
example values for a plausible UK home, and a banner above the form says so explicitly
("Predbat isn't configured yet — these are example values, edit them to match your
home"). When your own setup supplies a battery or a solar array, the banner is absent and
the fields it can read are the real ones; any remaining gaps still use the same example
values, so the form is always complete rather than partially blank.

**Add another array** and **Remove array** under **Solar** change how many roof aspects
are modelled, for a house with panels facing more than one way. Both re-render the form
with everything you have already typed still in place, and the arrays renumber
themselves. Removing every array is allowed and gives a battery-only run — the form says
so rather than leaving you wondering whether it took. Neither button saves: like any
other edit, the change is yours until you press **Save settings** or **Run simulations**.

**Import tariff** and **Export tariff** are chosen separately, so any combination can be
modelled — Agile import with a fixed export deal, the price cap with Octopus Outgoing
Prime, or anything else you want to try. Each dropdown lists a curated set of built-in
products (Agile, Cosy, Flux, Intelligent Go, Outgoing Fixed, Outgoing Prime, Agile
Outgoing and so on) plus, if your `apps.yaml` has a `compare_list`, your own entries from
it — a user entry with the same id as a built-in replaces it rather than appearing twice.
A `compare_list` entry defining both sides is offered in both dropdowns; one defining
only an import is offered only as an import.

**No export payment** is the export option for a home with no export agreement, and
prices export at 0p rather than leaving it unpriced. If your tariff has no export source
at all this is what the dropdown shows, because it is the accurate description of that
situation rather than a missing setting. It is also the right choice for a physical
zero-export (G99) limitation: unpaid export and curtailed export both earn nothing, so
the costs come out the same, though the reported export kWh will still show the surplus
leaving the house.

**The dropdowns are what run.** Picking an entry takes that tariff's rates straight from
the catalogue, whether it is defined by an Octopus URL or by a fixed rate structure. Each
URL field appears only when you choose **Custom** for that side, because that is the only
case where what you type in it is used — and the two sides are independent, so a custom
import leaves the export box alone. If a chosen URL contains `{dno_region}` (as the
Octopus product codes do), the **Octopus region letter** field is required; leaving it
blank is rejected up front, with the field named in the error, rather than left to fail
as a 404 partway through the run. Choosing **Custom** reveals that side's URL field,
pre-filled with whichever tariff you were looking at, so hand-entering one starts from
something rather than from nothing.

**Import tariff without PV or a battery** sets what the no-PV/battery comparison is priced on,
and defaults to the price cap. This matters more than it looks: a household with no system
would not be on a battery tariff, because the cheap overnight rates those offer are only
worth having once you have somewhere to put the energy. Pricing the counterfactual on your
own smart tariff therefore credits it with a saving it could never have had, and
understates what the system is worth. It applies to the no-PV/battery scenario only —
every other scenario uses your main tariff. Only the import side is offered, because a
home with no PV and no battery has nothing to export.

One simplification to know about: both are charged the **main tariff's standing charge**,
so if the two tariffs differ there, that difference is not included in the savings or
payback.

Every field the [configuration file](#advanced-the-configuration-file) accepts has a form equivalent, including the
manual-usage/Octopus-consumption choice under **Load** and the year, sample count and P10
fallback derate under **Advanced**. **Save settings** stores the configuration without
running it, so you can park a half-adjusted setup and come back to it; **Run simulations**
validates, stores it and starts a run.

### Running

A run takes roughly one to three minutes with the default two samples per month, or two
to six minutes with a car configured (each sampled day is planned twice — with and
without a charging session — to work out how often the car overflows the cheap window). With
**Fast mode** on (see below) those fall to roughly 30 to 90 seconds, or one to two and a
half minutes with a car.
Once started it shows a progress bar with the current step and elapsed time, and it keeps
running on the server if you navigate away or close the tab — come back to the WhatIf tab
later and the same run is still there, or already finished. The progress area appears on
all three pages, not just Configure, so switching to Results or Compare mid-run does not
lose sight of it. Only one run is active at a time: submitting **Run** again while one is
already in progress does not queue a second run or interrupt the first, though any form
edits you made are still saved.

**Cancel** stops the running job. When the run finishes, the tab you pressed **Run
simulations** in navigates straight to the Results page.

Any other tab you have open only gets a **view results** link rather than being
navigated. The progress poll runs in every open tab, so reloading them all would
silently discard whatever had been typed into a form somewhere else — an earlier
version of the page did exactly that. Only the tab that actually started the run
follows the completion.

### Fast mode

**Fast mode**, under **Advanced** on the Configure page, plans March, June, September and
December only and estimates the other eight months from those four against the year's
actual solar yield. It takes about 2.5 times less time — less than the four-months-in-twelve
might suggest, because the weather download, the rate downloads and starting the engine
all still happen.

It exists for comparing systems: try 5 kWh against 10 kWh, or one tariff against another,
without waiting three minutes for each answer. Measured against a full run of the same
system, the annual savings came out within 0.5% and the payback within 0.2%.

Individual months are rougher — typically within about 10%, worse in the tails — so the
month table and the chart both mark every estimated month. If you want to read one
particular month's figure, turn fast mode off.

**Some tariffs get a full run anyway.** Estimating one month from another only works while
a month's economics follow the solar curve. On a tariff whose prices swing from day to day
rather than following a fixed daily pattern — Agile most obviously — they do not, and the
estimated savings can be tens of percent out. Predbat measures this before planning starts
and, when it finds it, plans all twelve months instead. You get the slower, correct answer
rather than a fast wrong one, the run says so in its caveats, and "what this run used"
reports that fast mode was requested but a full run was needed.

Rate data is still downloaded for all twelve months in fast mode, so a month with no rates
available is still reported as unavailable rather than quietly estimated over.

### The results view

The Results page shows what this run used, an annual totals table, the chart below it, a
month-by-month breakdown and the run's caveats. "What this run used" is read from the
run's own stored settings rather than from the form, so switching the selector to a run
made on a different system relabels every figure with that system's settings. It names
all three tariffs — baseline, import and export — because a saving quoted without the
baseline it is measured from cannot be checked. One thing carries over unchanged from how
the engine reports it, and matters for reading the numbers correctly: a month with
`status: unavailable` is left out of the chart and the totals — it is never drawn as a
zero-cost bar or counted as a free month.

Every completed run is saved automatically — there is nothing to press — and the last
twenty are kept, each labelled with a short summary of its configuration (battery size,
solar size, tariff) rather than just a timestamp. A selector above the results lets you
switch between them — instantly, with no re-run — so you can compare a 5 kWh battery
against a 10 kWh one, or two tariffs, side by side. Each stored run can also be
downloaded as its raw JSON results document; a debug run's captured plans are not part
of that download (see [Debugging a run](#debugging-a-run)).

### Comparing runs

The Compare page lists every stored run in one table, newest first, so you do not have
to hold numbers in your head while flipping the Results selector back and forth. Each
row is: run (label, linking to that run in the Results page), solar size, battery size,
system cost, the run's three tariffs, cost with Predbat, saving versus no system, and
three payback columns — PV only, PV + battery, and + Predbat. A system cost that came
from a quote you entered rather than from the cost model is marked "quoted", so the two
are never confused. The row for the run the Results page is currently showing is
highlighted. Thirteen columns is wide, so the table scrolls sideways inside its own
container rather than widening the whole page.

The three tariff columns are **Baseline**, **Import** and **Export**, in that order —
the baseline first because it is what the other two are being judged against, so the row
reads "instead of this, on these, it costs this". Each names the tariff as the dropdown
that chose it does, including your own `compare_list` tariffs; a hand-entered URL that
matches no entry shows its Octopus product code instead. A run stored before a tariff
was recorded shows a dash for it.

**A dash in this table means the figure could not be computed — it is not a zero.**
This is the distinction the table turns on, so it is worth being explicit about it:

- A run covering less than a full year has no payback figures at all — the totals
  themselves refuse to extrapolate a partial year (see [Cost and
  payback](#cost-and-payback)) — and all three payback cells show a dash. Hovering
  one explains why, in the same words as the run's own caveats (for example, "only
  11 of 12 months could be modelled").
- A payback option that *was* costed but genuinely never earns back its capital says
  **does not pay back** in words, never a number — that is a different fact from
  "unavailable" and the table keeps the two apart rather than collapsing them onto
  the same dash.

Each row has a **Delete** button, which asks for confirmation first — a deleted run
cannot be recovered, only re-run. Deleting removes the run's stored results and any
captured plans as well as its row, so it leaves nothing behind.

Each row reads only that run's own stored summary, never the live form and never
another run's figures, so the columns are guaranteed to describe the system named at
the start of the row. Treat this table as a comparison aid for the same reason the
payback figures it shows are one: it is a quick way to see which of your stored runs
looks better, not a substitute for reading a run's own caveats before quoting it.

## What the numbers mean

Everything below applies whether you are reading the Results page or the raw JSON — the
web pages are a view onto the same results document, and the field names are given so the
two line up.

Each month carries a `status`:

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

### Cost and payback

The `annual.costs` block estimates what the modelled system would cost to install, and
`annual.payback` turns that into how long it takes to earn back — both computed from
published 2025/26 median install prices, not anything specific to your quotes.

PV cost is size in kWp times a £/kWp rate, floored at a minimum install price
(`pv_minimum_gbp`, default £2,500) so a token array is never costed at pennies. The rate
itself interpolates between three published band medians — `pv_rate_small_gbp_per_kwp`
(£1,780, anchored at 2 kWp), `pv_rate_medium_gbp_per_kwp` (£1,697, anchored at 7 kWp) and
`pv_rate_large_gbp_per_kwp` (£1,262, anchored at 30 kWp) — rather than stepping between
them, so cost stays monotonic across the size range instead of a 4.1 kWp system coming out
cheaper than a 4.0 kWp one purely from crossing a band boundary. Battery cost is a flat
`battery_install_gbp` (default £500) plus `battery_per_kwh_gbp` (default £300) times
usable capacity. A system with no PV, or no battery, costs nothing for the part it does
not have.

All seven of these — the three PV band rates, `pv_minimum_gbp`, `battery_install_gbp`,
`battery_per_kwh_gbp` and `predbat_annual_gbp` below — are editable under **Advanced** on
the web form, or `annual.costs` in the config file, if your own quotes differ from the
published medians.

### If you have a real quote

A quote beats any model of one. `quoted_pv_gbp` (**solar only**) and `quoted_total_gbp`
(**solar and battery together**), both £0 by default meaning "no quote", replace the
estimate. They sit on the configuration page rather than under **Advanced**, because a
real figure is the most useful thing you can tell the tool.

They are deliberately *solar-only* and *whole-system* rather than solar and battery
separately, because that is the shape real quotes come in — an installer prices the
installation, and nobody is handed a battery-only figure to copy out. **The battery cost
is taken as the difference between the two**, so a single whole-system quote can be
entered as it stands with no arithmetic.

Either can be used on its own. Give only the whole-system price and the solar stays
modelled, which is what the PV-only payback is worked out from; give only the solar price
and the battery stays modelled. If a whole-system quote comes in below the solar figure
beside it the battery is held at zero rather than going negative — a contradiction only
you can resolve, but not one that should produce a total disagreeing with its own parts.

Anything priced from a quote is labelled as such in the results and on the comparison
page, so an estimate is never passed off as a real price.

The configuration page shows the estimated install cost as you type, updating from the
same cost model the run itself uses.

`predbat_annual_gbp` (default £0) is different from the other six: it is a **recurring**
yearly cost, not a one-off capital cost, and it is not added to the install price. Predbat
itself is free when self-hosted, which is what the default reflects; the hosted
Predbat.com product is expected to charge around £100 a year, and setting this field
subtracts it from the "with Predbat" scenario's annual saving before payback is worked
out, so a subscription that ate the whole saving would correctly never pay back.

`annual.payback` reports three simple payback periods, one per purchase you could
actually make: `pv_only` (PV cost against the PV-only scenario's saving), `pv_battery`
(the full system cost against the un-optimised battery's saving) and
`pv_battery_predbat` (the full system cost against Predbat's saving, net of
`predbat_annual_gbp`). Each row's `years` is capital divided by net annual saving; when
the saving is zero or negative — `predbat_annual_gbp` outweighing the benefit, most
plausibly — `pays_back` is `false` and `years` is `null` rather than a negative or
enormous number, since neither would mean "pays back", they would mean "never does".

Payback needs a full twelve months of modelled data: with even one month unavailable,
every included month's saving is for a different twelve months than the one being priced,
so `annual.payback.available` is `false` and `annual.payback.reason` says how many months
were actually covered instead. This mirrors how the annual totals themselves refuse to
extrapolate a partial year (see `months_included` above).

Simple payback is a comparison aid, not a financial projection: capital divided by one
year's saving, held constant. It ignores panel degradation, electricity price inflation,
battery replacement, and finance costs (a loan or the opportunity cost of paying cash) —
all of which would move the real number, in either direction, over a payback period that
can run to a decade or more.

The `caveats` list in the results document records anything that could affect how much
to trust the numbers — a P10 fallback, a missing month's rate data, the
`export_credit_p_estimate` note above, and a summary of the payback caveat too — and is
worth reading before quoting the totals.

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

## Advanced: running it from the command line

Everything above is available from the web tab. The command line exists for scripting a
run, or for running one somewhere without a browser; it produces the same results
document.

```bash
cd apps/predbat
python3 annual_cli.py --config annual.yaml --out results.json
```

Other options: `--fast` enables fast mode for that run, overriding the config file —
four months are planned and the rest estimated, subject to the same tariff check described
under [Fast mode](#fast-mode). `--work-dir` (default `./annual_work`) sets where the
headless Predbat instance and the download cache live, and `--quiet` suppresses only the
per-month progress lines written to stderr. Warnings - a P10 fallback, missing rate data, a failed
sample day, a car-charging shortfall - are never suppressed, `--quiet` or not: failures
stay visible. A human-readable table is always printed to stdout; `--out` additionally
writes the full results document as JSON.

`--months` restricts the run to a comma-separated list of month numbers (e.g. `--months 7`
or `--months 6,7`), the same as the config file's `annual.months` below — useful for a quick
single-month check without waiting for a full year. `--year` plans a specific year instead
of the config's default (the most recent complete calendar year); combine it with `--months`
to model a recent, still-partially-in-progress year, since a bounded window may reach into
the current year once each month it names is far enough past the archive lag to be complete
(see [Planning a month subset](#planning-a-month-subset) below). Both flags override
whatever the config file has.

`--export-compare` evaluates the three built-in Octopus export products (Outgoing Fixed,
Outgoing Prime and Agile Outgoing) against otherwise identical inputs in one run — the same
sweep `annual.export_tariffs` configures below, with `sampling: weekday_spread` and
`samples_per_month: 5` set automatically (see [How car charging is spread across the
year](#how-car-charging-is-spread-across-the-year) for why sampling strategy matters on a
tariff that varies by day of week) and fast mode forced off. The results document then
carries a `by_export` block — one card per tariff, plus a comparison table — instead of a
single set of scenarios; `annual_cli.py`'s table renders both shapes. See
[`docs/annual/export-compare.yaml`](annual/export-compare.yaml) for a complete, runnable
example config, including the equivalent flag-only invocation in its header comment.

A run takes roughly one to three minutes with the default two samples per month: 24 plan
calculations (12 months × 2 samples) plus the weather, tariff and (if configured)
Octopus consumption downloads, which are cached between runs in `--work-dir`. With
`--fast` it is 8 plan calculations instead of 24, so roughly 30 to 90 seconds. `--months`
scales this down roughly in proportion to how many months it names, and `--export-compare`
multiplies it by three tariffs (at 5 samples a month rather than the default two).

## Advanced: the configuration file

The web form writes this file for you, and every field on it has an equivalent here. You
only need to read this section if you are running from the command line, or want to hand-
edit something the form does not expose.

```yaml
annual:
  location:
    postcode: "SW1A 1AA"        # or latitude/longitude
  year: 2025                     # defaults to the most recent complete calendar year
  months: [7]                    # omit to plan all twelve; see "Planning a month subset" below

  solar:                         # omit for a battery-only run
    - kwp: 5.6
      declination: 35            # pitch in degrees, default 35
      azimuth: 180               # 180 = south, default 180
      efficiency: 0.95           # default 0.95, must be greater than 0 and at most 1

  export_limit_kw: 10.0          # grid connection export cap, default 10.0; applies with
                                  # or without a battery (previously battery.export_limit_kw,
                                  # which is still read for backward compatibility)

  battery:                       # omit for a PV-only run
    size_kwh: 9.5
    inverter_kw: 5.0
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

  baseline_tariff:               # priced for the no-PV/battery scenario only
    rates_import:                # defaults to the Ofgem price cap
      - rate: 26.11
    rates_export:
      - rate: 4.1

  samples_per_month: 2
  sampling: percentile           # percentile (default) | weekday_spread, see below
  fast_mode: false               # plan 4 months and estimate the rest, see Fast mode
  debug: false                   # keep each sampled day's plan, see Debugging a run

  # Optional: sweep several export products against the same import side, standing
  # charge, weather and sampled days — see "Comparing export tariffs" below. Omit for a
  # normal single-tariff run, which is what every config written before this existed means.
  export_tariffs:
    - id: outgoing_fixed
      name: Octopus Outgoing Fixed
      export_octopus_url: "https://api.octopus.energy/v1/products/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{dno_region}/standard-unit-rates/"
    - id: seg
      name: SEG fixed rate
      rates_export:
        - rate: 4.1
```

At least one of `solar` or `battery` must be given — with neither there is nothing to
evaluate. `annual.location` needs either `postcode` or both `latitude` and `longitude`;
if both are given, latitude/longitude wins.

### Planning a month subset

`annual.months` (equivalently, `--months` on the command line) restricts the run to an
explicit list of month numbers rather than the whole year — useful for a quick check of one
month, or for automating a run just after that month's data becomes available. Each month
named must have ended long enough ago for Open-Meteo's archive to be complete for it — at
least eight days, between the archive's own ~5 day lag and the two extra days the weather
window fetches past a month's end (see `year`'s rejection rule further below, which this
uses the same reasoning as); with no explicit subset the cap instead stays at the last
complete *calendar year*, since a whole-year weather download cannot tell a truncated
in-progress year from a finished one and would cache it that way permanently. This is also
why `--year` on the command line matters once you use `--months`: with no explicit
`--year`/`year`, the run defaults to *last* year, which is wrong for anything modelling a
tariff that only exists this year (Outgoing Prime, for example, launched 23 June 2026).

A month subset forces `fast_mode` off — there is nothing to interpolate in a handful of
months, and no full-year solar curve to fit them against — and the results document carries
an extra `months_requested` field so the table and the web UI can say "based on 1 of 1
requested month(s)" rather than misreport a deliberate subset as ten failed months.

### Comparing export tariffs

`annual.export_tariffs` (equivalently, `--export-compare` on the command line, which fills
this in from the built-in catalogue automatically) evaluates several export products in one
run, each priced against the same import side, standing charge, weather and sampled days as
everything else — the only thing that varies between cards is the export tariff, so the
comparison isolates that one variable. Each entry needs a unique `id`, a `name` for display,
and either an `export_octopus_url` or a fixed `rates_export` list, exactly like the export
side of `annual.tariff` itself.

The results document gains a `by_export` block — one card per entry, keyed by `id`, each
shaped like a normal single-tariff document plus a `rates_synthesised` flag — instead of a
single set of scenarios, and `annual_cli.py`'s table renders a per-tariff table for each
plus a side-by-side comparison summary. A sweep also forces `fast_mode` off, for the same
reason a month subset does. `annual.sampling` defaults to `percentile` (see [How it
works](#how-it-works)) but is worth setting to `weekday_spread` for a sweep against a
day-of-week-varying export tariff (Agile Outgoing, most obviously): it spreads the sampled
days across distinct weekdays instead of ranking purely by irradiance, which buys rate
variation that a purely PV-ranked sample would not reliably capture. `--export-compare`
sets this automatically, along with `samples_per_month: 5`.

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

**Only use the Octopus option if you do not already have solar or a battery.** An import
meter records what you *bought from the grid*, not what your home used. If you already
generate or store your own energy, that self-consumption and battery discharge have
already been subtracted from every reading — so the series is your residual grid demand,
not your household load. Feeding it in and then modelling a solar and battery system on
top applies the same saving twice: the tool would credit you for displacing import that
your existing system had already displaced, and overstate what a new system is worth.

For a home that already has a system, use `annual_kwh` instead, and give your **total
household consumption** — generation included — rather than the figure on your bill. The
web form says the same, and says it more loudly when it can see that your Predbat
instance already has a battery or an array configured.

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

## Debugging a run

When a figure looks wrong, a normal run gives you no way to see why: you get the monthly
totals and nothing about the plans that produced them. Ticking **Save plans for
debugging** on the Annual tab — or setting `debug: true` in the config file — keeps the
plan for every sampled day and makes it viewable.

What is kept is the plan as it was actually billed: the same charge and export windows,
against the same PV and load series the cost came from. That is what makes it usable for
cross-checking. A plan drawn against a different series would look plausible and prove
nothing.

Every scenario is kept, not just Predbat's — `no_pvbat`, `pv_only` and `without_predbat`
too, so you can see what all three baselines did rather than inferring it from their
totals. When a car
is configured each sampled day is planned twice, once with a charging session and once
without, and the month's figures blend the two (see [How it works](#how-it-works)); both
legs are kept and labelled, since a suspicious monthly figure may come from either.

The results page then offers a plan viewer below the monthly table, with a day and
scenario selector. It renders with exactly the same code as the live
[plan page](predbat-plan-card.md) — same columns, same colours, same debug-column toggle —
so anything you already know about reading a Predbat plan applies unchanged.

Captured plans are stored separately from the run's results document, one storage key
per sampled day's leg, rather than embedded inside it. That keeps the results document
itself small — the totals, chart and month table you look at on every visit do not carry
the plans — and a plan is only fetched off storage the moment you actually pick a day and
scenario in the viewer, not every time the results page loads. A run downloaded as JSON
is that results document as stored, which means it is the results **without** the
captured plans; the plan viewer is the only place to get at them.

Two things to know before turning it on:

- **The saved run gets much larger.** A year at the default two samples per month keeps
  72 plans (144 with a car configured). Runs are stored through the same twenty-run
  rotation as any other, so a debug run displaces older runs at the same rate.
- **It does not change the numbers.** The flag only retains plan data that the engine
  already computes; it does not enable Predbat's own `debug_enable`, which would disable
  the prediction kernel and slow every plan down substantially.
