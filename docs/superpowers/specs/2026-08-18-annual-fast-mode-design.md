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

### Confirmed against Agile and Cosy

The three runs above are all Octopus Intelligent Go, which is nearly rate-flat across the
year, so they exercised no rate seasonality. Two further twelve month references were run
to close that gap — Octopus Agile (monthly mean import 17.7p to 29.3p, a 66% spread) and
Octopus Cosy — on an identical system, varying only the import tariff. A third Agile
reference at `samples_per_month: 6` provides a lower-noise ground truth. See
[Curve selection results](#curve-selection-results).

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

`ANCHOR_MONTHS = (3, 6, 9, 12)` — March, June, September, December. One per season, and a
spread that spans the solar range from midwinter to midsummer, which is what the fit needs
to resolve `b`.

**Corrected after implementation.** This originally said `(1, 4, 7, 10)`, on the grounds
that the measured data did not separate the candidates. That was wrong, and wrong in a way
worth recording: the per-month cost error genuinely does not separate them, but the
*savings* figures — the numbers a reader acts on — separate them sharply, because a saving
is a difference between two reconstructed scenarios and small relative errors in each are
amplified in the difference. Measured on the Cosy reference, reconstructing the year from
each candidate:

| Anchors | System saving | Predbat saving |
|---|---|---|
| (1, 4, 7, 10) | +1.2% | −10.6% |
| (2, 5, 8, 11) | −0.8% | +11.7% |
| **(3, 6, 9, 12)** | **−0.5%** | **+0.2%** |

Anchor count does not rescue a bad set: six, seven and eight anchors were also tried and
land no better (Cosy with seven anchors is +11.8% on the Predbat saving, worse than four
well-chosen ones).

### The interpolation module

A new `apps/predbat/annual_interpolate.py`. Pure functions, no I/O, no Predbat import, so
the curve can be unit tested against known inputs and re-scored against reference runs
without standing up an engine.

```python
ANCHOR_MONTHS = (3, 6, 9, 12)
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
3. It leaves a mean-rate regressor available without redesigning the mode. That term was
   tested and rejected — see [Curve selection results](#curve-selection-results) — so this
   is now the weakest of the three reasons, but the first two stand on their own.

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

A caveat states the measured accuracy rather than implying none. On a tariff that passes
the volatility guard below, the annual savings land within about 3% and payback within
about 1% (measured: 0.5% and 0.2% on the Cosy reference); individual months are rougher,
typically within about 10%. Tariffs where that would not hold never reach interpolation.

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

## Curve selection results

Five twelve month references were scored: three Intelligent Go, one Cosy, one Agile, plus
an Agile run at six samples per month as a lower-noise ground truth. **`solar_affine` is
confirmed as the default and the mean-rate term is rejected.**

### The rate term overfits and is rejected

Scored against the noisy references, `solar_affine + mean_rate` looked like the winner: it
halved Agile's annual error, from 16.4% to 8.9%. Leave-one-anchor-out cross-validation —
fit on three anchors, predict the withheld fourth — shows why that was an illusion:

| Reference | `linear` | `solar_affine` | `solar_affine + rate` |
|---|---|---|---|
| Agile (6 sample) | 54.7% | **37.6%** | 361.9% |
| Cosy | 39.5% | **18.3%** | 50.9% |

Four anchors against three parameters leaves one degree of freedom, and the model spends it
fitting noise. Against the *clean* Agile reference it is also worse than plain
`solar_affine` on the figure that matters: +3.62% savings error versus +0.09%. Its gain on
noisy references was absorbing that noise, not modelling signal. The same cross-validation
confirms `solar_affine` beats `linear` decisively on generalisation, matching its per-month
advantage on all five references.

Rates are still fetched for all twelve months, for the unavailable-month and export-credit
reasons above — those stand on their own and do not depend on a rate regressor.

### The volatility guard

Fast mode's accuracy holds only while a month's economics follow the solar curve. On Agile
they do not, and **no anchor set rescues it** — every choice is 8-32% out on one savings
figure or the other, and adding anchors does not help. That is a property of the tariff,
not a fixable modelling defect: adding a rate term to the basis overfits (above), and
because least squares is linear, fitting the savings difference directly is *identically*
equal to differencing two fits, so that reformulation is a no-op.

So rather than report a fast wrong number, the run declines fast mode and plans all twelve
months. The signal is the coefficient of variation of daily average import price, which
separates the two cases by roughly fortyfold:

| Tariff | Rate variability | Verdict |
|---|---|---|
| Cosy | 0.002 | fast mode runs |
| Intelligent Go and other banded tariffs | ~0 by construction | fast mode runs |
| Agile | 0.222 | full run |

`FAST_MODE_MAX_RATE_CV = 0.10` sits between them with a wide margin either side. It is
measured over the four anchor months rather than one: a single month is too tight (Agile's
quietest month is 0.104 against the 0.10 limit) while the four-month mean sits at twice it.

The check runs **before any month is planned**, so the progress total is honest from the
first month rather than growing mid-run, and no work is wasted — the anchors a fast run
would have planned are months a full run needs anyway. Verified end to end: `--fast` on
Agile produces a bit-identical full run (+0.0% on both savings), and on Cosy reconstructs
payback to 5.84 years against 5.81.

### Fast mode's own error is very small

Decomposing Agile's error against the six-sample reference separates what fast mode causes
from what it inherits:

| | System saving error | Predbat saving error |
|---|---|---|
| A. Full 12 months at 2 samples (**today's tool, no fast mode**) | −8.47% | +17.83% |
| B. Fast mode from those same 2-sample anchors | −8.08% | +16.83% |
| C. Fast mode from clean anchors (**curve error alone**) | **+0.09%** | **−0.73%** |

Row C is the curve's actual contribution: **well under 1%**. Row B is indistinguishable
from row A — fast mode inherits the sampling noise already present in the anchors rather
than adding error of its own. The earlier reading, that "Agile breaks fast mode", was an
artefact of scoring against a reference that was itself noisy.

### Pre-existing finding: sampling noise on volatile tariffs

Row A is not about fast mode at all and deserves separate attention: **today's twelve month
WhatIf run is already 8–18% off on Agile** at the default two samples per month. Raising to
six samples does not converge it — individual months still move by up to 218 p/day between
the two runs, and June remains a large negative outlier in both. Agile's day-to-day price
variance is simply too high for a handful of sampled days to characterise a month.

This is a limitation of the existing sampling design, not of interpolation, and fixing it is
out of scope here. It is recorded because it bounds what fast mode can be held to: on a
volatile tariff, neither mode is accurate to better than roughly 10%, and the caveat text
should not imply otherwise.

The scoring harness is committed under `apps/predbat/tests/` so the curve can be
re-validated when new reference runs appear, rather than living as a throwaway script. The
reference runs themselves are large and site-specific, so they are not committed wholesale:
one reduced fixture — months, scenarios and monthly PV only, no plans, no location — is
committed per tariff family and asserts the chosen basis still beats the alternatives on it.
The harness additionally accepts a directory of full reference runs via an environment
variable and scores those when present, which is how the selection above was performed.
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
