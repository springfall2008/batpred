# WhatIf fast mode: four seasonal months, interpolate the rest

## Problem

A WhatIf annual run plans every one of the twelve months. At the default
`samples_per_month: 2` that is 24 planned days, or 48 with a car configured (each
sampled day is planned twice, with and without a charging session). It takes one to
three minutes without a car and two to six with one.

That cost is paid every time someone changes a single number. The tool's audience is
someone trying battery sizes against each other — "what about 10 kWh instead of 5?" —
and a three minute wait per answer discourages exactly the exploration the tool exists
for.

Most of that work is redundant. Household electricity cost over a year is dominated by
one smooth seasonal driver, solar yield, which the run already downloads in full for
every day of the year before it plans anything. Planning all twelve months re-derives a
curve whose shape is already known.

`fast_mode` plans four months and reconstructs the other eight from that known curve.

## Measured basis

The design is not a guess. Three complete twelve month runs were recovered from the
addon cache and used as ground truth: hold eight months out, rebuild them from four
seasonal anchors, and score the reconstruction against what actually ran. Three anchor
sets were tried against each of five candidate curves, over all four scenarios.

| Scheme | Annual cost error | Savings error | Per-month error |
|---|---|---|---|
| Cyclic linear on raw monthly totals | 0.91% | 1.78% | 18.3% |
| Cyclic linear on per-day values | 1.71% | 1.36% | 18.7% |
| Fourier (mean + 1st harmonic + cos 2θ) | 1.80% | 1.37% | 19.0% |
| Proportional to solar (value ∝ PV) | 20.48% | 5.16% | 54.2% |
| **Affine in solar (per-day = a + b·PV)** | 1.82% | 1.45% | **12.6%** |

Errors are means; the savings column drives payback and is the figure that matters most.

Three findings shape the design:

**Solar-shaped, but affine rather than proportional.** Making a month's cost
proportional to its solar yield is the worst scheme tested, at 20% annual error,
because cost has a large load-driven floor that does not scale with sun. Fitting
`a + b·PV` recovers that floor in `a` and the solar response in `b`, and becomes the
best scheme.

**Annual totals barely care which curve is used.** Every sane scheme lands within about
2% on the annual total and 1.8% on savings. The curve choice is not what makes fast mode
trustworthy for payback; four well-sampled anchors are. Where the curve earns its keep is
the month-by-month table and chart.

**Affine-in-solar is insensitive to anchor choice.** Its per-month error holds at
12.4-12.9% across every anchor set tried, while cyclic linear swings between 15.9% and
26.4% depending on whether the chosen months happened to be seasonally representative.
It is robust because it reads the real solar curve for the missing months instead of
assuming the anchors implied it.

### What this evidence does not cover

Three runs, two distinct system configurations, one location, one year, and one tariff:
Octopus Intelligent Go. That tariff is nearly rate-flat across the year — monthly mean
import 16.3p to 18.1p, export fixed at 15p — so **no rate seasonality was exercised at
all**. Agile, whose winter price spikes are precisely the seasonality four anchors might
miss, is untested. Closing that gap is the first implementation step, and the design
keeps the door open for a rate term if the gap turns out to matter.

## Design

### Surface

Off by default, exposed three ways:

- `annual.fast_mode: true` in the config file
- `--fast` on `annual_cli`, which sets the same key before validation
- a checkbox in the web form's **Advanced** section, beside `samples_per_month`

`validate_config` gains one entry beside `"debug"`, using the same coercion so that an
explicit `fast_mode: "false"` in YAML cannot be read as truthy:

```python
"fast_mode": _coerce_bool(raw.get("fast_mode", False)),
```

Because `validate_config` already stores `"raw": scrub_secrets(raw)` into the results
document, the flag reaches the web layer's "what this run used" panel with no extra
plumbing.

### Anchor months

`ANCHOR_MONTHS = (1, 4, 7, 10)` — January, April, July, October. One per season, and a
spread that spans the solar range from midwinter to midsummer, which is what the fit
needs to resolve `b`. The measured data does not meaningfully separate this from
`(2, 5, 8, 11)` or `(3, 6, 9, 12)`; it is chosen for spread and for reading naturally as
one per season.

### The interpolation module

A new `apps/predbat/annual_interpolate.py`. Pure functions, no I/O, no Predbat import, so
the curve can be unit tested against known inputs and re-scored against reference runs
without standing up an engine.

```python
ANCHOR_MONTHS = (1, 4, 7, 10)
BASIS_SOLAR_AFFINE = "solar_affine"
BASIS_LINEAR = "linear"
DEFAULT_BASIS = BASIS_SOLAR_AFFINE  # provisional; fixed by the reference study below

def monthly_pv_kwh(weather, year):
    """Return {month: total actual PV kWh}, or None when the run has no solar."""

def fit_solar_affine(pv_per_day, values_per_day):
    """Least-squares (a, b) for value_per_day = a + b * pv_per_day, or None if degenerate."""

def build_interpolated_rows(anchor_rows, year, monthly_pv, basis=DEFAULT_BASIS):
    """Return month rows for every month of the year absent from anchor_rows."""
```

`monthly_pv_kwh` reads `WeatherYear._daily_actual`, which already holds a total for every
day of the year — the complete twelve month solar curve costs **nothing extra to obtain**,
no additional fetch. (A small accessor is added to `WeatherYear` rather than reaching into
the private attribute from another module.)

Each scenario key is fitted independently for each field, on per-day values:

```
per_day(m)  = a + b · pv_per_day(m)        # a, b fitted over the surviving anchors
value(m)    = per_day(m) · days_in_month(m)
```

Fitting per field matters. `no_pvbat` cost is load-driven and lands near `b ≈ 0`, while
`export_kwh` is steeply solar-driven; one shared shape would serve neither. Working in
per-day space keeps February's 28 days from reading as a seasonal dip.

Three guards, each reachable with real configs:

- **No solar** (`self.weather is None`, a battery-only run) leaves no basis to fit. Falls
  back to `BASIS_LINEAR`, cyclic linear interpolation on per-day values, wrapping December
  to January.
- **Degenerate fit** — anchor PV variance at or near zero — takes the same fallback rather
  than dividing by it.
- **Clamping.** `cost_p` is legitimately negative when export credit exceeds import spend,
  so it is left alone. Every other field in `SCENARIO_FIELDS` (`import_kwh`, `export_kwh`,
  `pv_generated_kwh`, `battery_throughput_kwh`, `battery_cycles`) is clamped at zero: an
  affine fit extrapolated below the anchors' solar range will otherwise produce negative
  December export.

The basis is a named strategy, not a hardcoded formula, so the reference study selects the
default rather than this document asserting it.

### Rate data is still fetched for all twelve months

Fast mode skips *planning*, not *downloading*. Rate fetches are network-bound and cheap
next to the search that planning runs; keeping all twelve buys three things:

1. A month with genuinely no rate data stays `"unavailable"`, as it does today, instead of
   being fabricated by interpolation over a gap the user should be told about.
2. `export_credit_p_estimate` is recomputed from the month's real average export rate
   rather than interpolated. This keeps the field's meaning identical in fast and full
   runs. Because it needs tariff data, it is attached in `run()` rather than inside the
   pure module, using the 15th of the month as the representative day — the planned path
   uses its first sampled day for the same purpose.
3. If the Agile reference shows solar-affine cannot absorb rate seasonality, the
   `+ c · mean_rate` term is already fetchable without redesigning the mode.

### What `run()` does

The month loop is restructured around a `months_to_plan` list — all twelve today,
`ANCHOR_MONTHS` under `fast_mode` — leaving the per-month planning body unchanged. Rate
fetching, availability checks and the next-month spill fetch still run for every month, so
an unavailable month is still detected and reported as one.

After the loop, when `fast_mode` is on:

- Anchor rows that reached `"ok"` or `"degraded"` are the fit's input.
- **Fewer than two survive** — one point cannot define a line — so fast mode is abandoned:
  the remaining months are planned normally, a caveat records why, and the run completes
  as a full one rather than failing or guessing.
- Otherwise `build_interpolated_rows` fills every month that is not an anchor and was not
  already marked unavailable.

Progress reports `len(months_to_plan)` units plus a final interpolation step, so the web
progress bar stays truthful rather than stalling at 4/12.

### Results document

Interpolated rows carry `"status": "interpolated"` and a provenance block:

```python
{
    "month": 5,
    "status": "interpolated",
    "days": 31,
    "standing_charge_p": ...,
    "scenarios": {...},
    "interpolated_from": {"anchors": [1, 4, 7, 10], "basis": "solar_affine"},
}
```

They carry no `sampled_days` — nothing was sampled — which is itself the honest signal
that no day of that month was ever planned.

`_build_results` treats `"interpolated"` as included alongside `"ok"` and `"degraded"`, so
these months count toward annual totals, savings and payback. That is the entire point of
the mode; excluding them would report a four month year. The `annual` block gains
`"fast_mode": True` and `"months_interpolated"`, so a stored run can never be mistaken for
a full one after the fact.

A caveat states the measured accuracy rather than implying none: annual savings land
within roughly 1.5%, individual months within roughly 12% typically and worse in the tails.

### Web UI

- **Configure**: a `fast_mode` checkbox in the Advanced block, following the existing
  `debug` checkbox pattern for rendering and for `config_from_post` parsing, with a note
  giving the trade — about 2.5× faster, monthly figures approximate, annual totals close.
- **Results**: interpolated months are marked in the month table and chart, following the
  precedent already set by `rates_synthesised`; "what this run used" gains a Fast mode row.
- **Run history**: `annual_store.build_summary` records the flag so the run selector and
  the Compare table distinguish a fast run from a full one. Comparing a fast run against a
  full one is legitimate — that is the accuracy claim being made — but it must be visible.

### Speed

24 planned days become 8; 48 become 16 with a car. Weather fetch, twelve months of rate
downloads and headless Predbat construction are fixed overhead that does not shrink, so
the realistic figure is **about 2.5× faster**, not a clean 3×. The docs should say 2.5×.

## Selecting the curve

The first implementation step, before the default is fixed:

1. Run fresh twelve month CLI references on **Agile import** and on **one banded tariff**
   (Cosy or Flux), with the same system config, so the two tariff families are directly
   comparable. Both APIs are reachable from the dev environment.
2. Score every candidate — `solar_affine`, `linear`, `fourier`, and `solar_affine` plus a
   mean-rate term — against those two references and the three Intelligent Go runs, on
   annual cost, savings, and per-month error.
3. If a rate term materially improves Agile, note that four anchors give four equations, so
   a three-parameter fit has one degree of freedom spare and overfitting is a real risk that
   the scoring must measure rather than assume away.
4. Record the resulting table in this document and set `DEFAULT_BASIS` from it.

The scoring harness is committed under `apps/predbat/tests/` so the curve can be
re-validated when new reference runs appear, rather than living as a throwaway script. The
reference runs themselves are large and site-specific, so they are not committed wholesale:
one reduced fixture — months, scenarios and monthly PV only, no plans, no location — is
committed per tariff family and asserts the chosen basis still beats the alternatives on it.
The harness additionally accepts a directory of full reference runs via an environment
variable and scores those when present, which is how the selection in step 2 is performed.
Absent both, the test skips rather than failing, so a checkout without fixtures stays green.

## Testing

Per `CLAUDE.md`, all new code gets unit tests. New `tests/test_annual_interpolate.py`:

- exact recovery — anchors drawn from a known `a + b·PV` line reconstruct it exactly
- per-day handling — a constant per-day value reconstructs as varying monthly totals that
  track month length
- clamping — an anchor set extrapolating a field negative yields zero, and `cost_p` stays
  negative when it should
- degenerate fit — flat anchor PV falls back to linear rather than dividing by zero
- no-solar runs take the linear basis
- cyclic linear wraps December to January rather than extrapolating off the end

Extended elsewhere:

- `test_annual_config.py` — `fast_mode` defaults false, coerces `"false"` correctly
- `test_annual_cli.py` — `--fast` sets the flag; absent leaves it false
- `test_web_annual.py` — checkbox renders from config, round-trips through
  `config_from_post`, and interpolated rows render marked in the table
- a fast-mode engine test asserting four months planned, eight marked `"interpolated"`,
  annual totals covering all twelve, and the fallback when fewer than two anchors survive

## Documentation

`docs/annual-prediction.md` gains a Fast mode section under Advanced covering what it
does, the measured accuracy, when not to use it (reading a specific month's figure), and
the `--fast` flag; the run-duration text notes the fast figure alongside the full one.

## Out of scope

- Changing `samples_per_month` under fast mode. Fast mode reduces months, not sampling
  within a month; cutting both would let a single unlucky day shift the fitted slope for
  the whole year, which is what the existing percentile sampler exists to prevent.
- A user-configurable month count. A boolean keeps the tested surface to two paths.
- Interpolating a stored full run after the fact.
- Re-pricing interpolated months by re-running the costing engine against their real rates
  — that is a different and much larger feature than interpolation.
