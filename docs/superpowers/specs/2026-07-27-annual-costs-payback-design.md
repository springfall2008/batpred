# Annual Install Costs and Payback — Design

**Status:** approved
**Date:** 2026-07-27

## Purpose

The annual prediction tool answers "what would this system save me?" It does not answer
"is it worth buying?", which needs the other half: what the system costs, and how long the
saving takes to repay it. This adds an install-cost estimate and a simple payback period
for each of three purchase decisions — PV alone, PV with a battery, and PV with a battery
run by Predbat.

It also lets a user describe their array the way they actually think about it: as a number
of panels, not only as a peak-power figure.

## 1. PV input: kWp or panel count

Each solar array accepts **either** `kwp` **or** `panels`, with an optional `panel_watts`
(default 400). When `kwp` is absent it is derived:

```
kwp = panels * panel_watts / 1000
```

This is done in `_validate_solar`, not in the web form, so a hand-written `annual.yaml`
can use panel counts too. `kwp` remains the single canonical value the engine consumes —
nothing downstream needs to know which way it was entered.

Supplying **both** `kwp` and `panels` is rejected with a message naming the array, rather
than silently preferring one. Two figures that disagree are a mistake worth surfacing, and
guessing which the user meant is exactly the kind of quiet assumption this tool avoids.

`panels` must be a positive whole number; `panel_watts` a positive number. The normalised
array retains `panels` and `panel_watts` alongside the derived `kwp` so the form can
round-trip what the user typed instead of replacing it with a computed decimal.

**Form:** each array offers a mode toggle — enter peak power, or enter panel count. Below
the roof-aspect fields a live summary reads:

> Total: 5.2 kWp across 13 panels

updating as the user types. The panel count is shown only when every array was entered as
panels; a mixed or kWp-only set shows the kWp total alone, because a partial panel count
would read as the whole system's.

## 2. The fourth scenario: `pv_only`

Payback for PV alone needs a PV-alone number, which the engine does not currently produce.
A fourth scenario is added between `no_pvbat` and `without_predbat`:

| Scenario | Solar | Battery | Optimiser |
|---|---|---|---|
| `no_pvbat` | no | no | — |
| `pv_only` | **yes** | **no** | — |
| `without_predbat` | yes | yes | cheapest-band timer |
| `with_predbat` | yes | yes | Predbat |

`pv_only` uses the same inverter and export limits as the battery scenarios but zero
battery capacity, so surplus generation is exported rather than stored. Like `no_pvbat`
and `without_predbat` it is a single prediction with empty charge and export windows — it
does **not** call `calculate_plan()`, which is what makes a run expensive. The added
runtime is therefore a few percent, not a third.

**This figure is the point of the exercise.** Without a battery, far more generation is
exported instead of used, so the PV-only saving is genuinely different from — not a fixed
fraction of — the PV-plus-battery saving. Deriving it arithmetically was considered and
rejected for the same reason `self_consumed_kwh` was removed: it would look like a
measurement and be an assumption.

**Chart colour: `#9439ef`.** Chosen by running the dataviz validator over a 216-colour
sweep against the existing three in both light and dark mode, not by eye. The existing
three (`#0072B2`, `#D55E00`, `#009E73`) are unchanged.

```
#0072B2, #9439ef, #D55E00, #009E73   (scenario order)

light  CVD separation   worst all-pairs #9439ef↔#0072B2  ΔE 9.2 (deutan)   PASS
       Normal-vision    worst all-pairs #009E73↔#0072B2  ΔE 18.7           PASS
       Contrast         all 4 >= 3:1                                       PASS
dark   Lightness band   all 4 inside L 0.48–0.67                           PASS
       Contrast         all 4 >= 3:1                                       PASS
```

Worth recording because the obvious candidates all fail. The Okabe-Ito colours that look
right — `#E69F00`, `#56B4E9` — sit at L 0.75 and 0.74, outside the dark band (0.48–0.67),
and both drop below 3:1 contrast in light mode. `#CC79A7` reaches only ΔE 7.6 against the
green under deuteranopia, inside the 6–8 band that is legal only with secondary encoding.
`#AA4499` passes both modes but only at ΔE 6.4 against the blue under protanopia — and the
blue is its immediate neighbour in scenario order, which is the worst place for a marginal
pair. `#9439ef` clears every check outright, so the chart needs no secondary-encoding
relief to be legible.

**Do not substitute a colour here without re-running the validator in both modes.**

## 3. Cost model

A new module `annual_costs.py`, pure functions with no I/O, so it is directly testable.

### Battery

```
battery_cost = 0                                    if size_kwh <= 0
             = install + per_kwh * size_kwh          otherwise
```

Defaults: `install = £500`, `per_kwh = £300`.

### PV

```
pv_cost = 0                                         if total_kwp <= 0
        = max(minimum, total_kwp * rate(total_kwp))  otherwise
```

Default `minimum = £2,500`. `total_kwp` is the sum across all arrays.

`rate(kwp)` linearly interpolates between the published band medians, each anchored at its
band's midpoint, clamped flat outside that span:

| Anchor | £/kWp |
|---|---|
| 2 kWp (midpoint of 0–4) | 1,780 |
| 7 kWp (midpoint of 4–10) | 1,697 |
| 30 kWp (midpoint of 10–50) | 1,262 |

Midpoints because a band median describes the typical system *within* that band, so the
band's centre is where it applies most accurately. Interpolating rather than stepping
avoids the discontinuity a step function produces, where a 4.1 kWp system would cost less
in total than a 4.0 kWp one. Interpolation is monotonic in total cost across the whole
0.1–50 kWp range (verified numerically).

Computed values:

| System | £/kWp | PV cost |
|---|---|---|
| 1.0 kWp | 1,780.00 | £2,500 (minimum applies) |
| 2.0 kWp | 1,780.00 | £3,560 |
| 3.0 kWp | 1,763.40 | £5,290 |
| 4.0 kWp | 1,746.80 | £6,987 |
| 4.1 kWp | 1,745.14 | £7,155 |
| 5.0 kWp | 1,730.20 | £8,651 |
| 8.0 kWp | 1,678.09 | £13,425 |
| 12.0 kWp | 1,602.43 | £19,229 |
| 30.0 kWp | 1,262.00 | £37,860 |

### Configurable

All six parameters are editable under **Advanced**, and travel in the config as a `costs`
block:

```yaml
costs:
  battery_install_gbp: 500
  battery_per_kwh_gbp: 300
  pv_minimum_gbp: 2500
  pv_rate_small_gbp_per_kwp: 1780     # anchored at 2 kWp
  pv_rate_medium_gbp_per_kwp: 1697    # anchored at 7 kWp
  pv_rate_large_gbp_per_kwp: 1262     # anchored at 30 kWp
  predbat_annual_gbp: 0               # recurring, not capital - see below
```

Every value is validated as a non-negative number. An absent `costs` block uses the
defaults, so existing configs keep working untouched.

### Predbat's own cost

`predbat_annual_gbp` is a **recurring yearly** cost, defaulting to `0` — Predbat is free
when self-hosted. The hosted web version is expected to charge around £100/year, so the
field exists to model that; it stays at zero unless set.

Because it recurs, it is **not** added to capital. It reduces the net annual saving of the
Predbat row only:

```
predbat_net_annual_saving = (no_pvbat - with_predbat) - predbat_annual_gbp
```

That is the correct treatment for a subscription, and it makes the comparison honest in
both directions: if the fee exceeds what Predbat adds over a plain timer, the Predbat row's
payback becomes *worse* than the PV-plus-battery row's, or fails to pay back at all. That
is a real and useful signal, not something to smooth over.

It does not touch any scenario's `cost_p`, which stays a pure electricity figure. The fee
is applied at the payback layer, where it belongs.

## 4. Payback

Simple payback in years, each measured against the no-system baseline:

| Payback for | Capital | Annual saving |
|---|---|---|
| PV only | PV | `no_pvbat.cost_p − pv_only.cost_p` |
| PV + battery | PV + battery | `no_pvbat.cost_p − without_predbat.cost_p` |
| PV + battery + Predbat | PV + battery | `no_pvbat.cost_p − with_predbat.cost_p` **− `predbat_annual_gbp`** |

```
years = capital_gbp / annual_saving_gbp
```

The three rows share only two capital figures: Predbat is software, so it adds no capital.
Where it has a recurring fee, that is subtracted from its annual saving instead (see
Predbat's own cost, above) — which is what makes its row a genuine comparison rather than a
free upgrade.

### Two honesty guards

**A saving of zero or less does not pay back.** The result records
`{"pays_back": false}` rather than a negative or infinite year count. A negative payback
period is meaningless, and a large positive one produced by dividing by a near-zero saving
is worse — it looks like an answer.

**Payback requires a complete year.** It is emitted only when `months_included == 12`.
If any month is `unavailable`, the annual total covers less than a year, so the saving is
understated and the payback correspondingly overstated. Rather than extrapolate, the
results say payback could not be computed and name the missing months. This matches how
the tool already refuses to count an unavailable month as free.

### Stated limitations

The results carry a caveat, and the docs say the same: simple payback ignores panel
degradation, electricity price inflation, battery replacement, finance costs, and any
export-tariff change over the period. It is a comparison aid, not a financial projection.

## 5. Results document

The engine writes both blocks so the CLI gets them without any web-layer involvement:

```json
"annual": {
  "costs": {
    "pv_gbp": 8651.0,
    "battery_gbp": 3350.0,
    "total_gbp": 12001.0,
    "pv_rate_gbp_per_kwp": 1730.2,
    "total_kwp": 5.0,
    "battery_kwh": 9.5
  },
  "payback": {
    "pv_only": {"pays_back": true, "years": 9.4, "capital_gbp": 8651.0, "annual_saving_gbp": 920.3},
    "pv_battery": {"pays_back": true, "years": 11.2, "capital_gbp": 12001.0, "annual_saving_gbp": 1071.5},
    "pv_battery_predbat": {"pays_back": true, "years": 8.7, "capital_gbp": 12001.0, "annual_saving_gbp": 1381.6, "gross_annual_saving_gbp": 1481.6, "predbat_annual_gbp": 100.0}
  }
}
```

When payback cannot be computed the `payback` block is `{"available": false, "reason": "..."}`
rather than absent, so a consumer can tell "not computed, and here is why" from "old
results document".

## 6. Web presentation

A costs and payback table above the monthly chart, showing for each of the three
purchase options: capital cost, annual saving, and payback period (or "does not pay back").
The PV and battery cost breakdown is shown alongside, so the capital figure is not a
number from nowhere.

The `pv_only` scenario also joins the existing monthly chart and month table as a fourth
series and row.

## 7. Testing

`annual_costs.py` is pure, so it is tested directly:

- the band rate at each anchor, between anchors, and clamped beyond both ends
- total cost monotonic across the range — the property that motivated interpolation
- the £2,500 minimum applying to a small system and not to a large one
- zero PV and zero battery each costing nothing, rather than the minimum
- payback arithmetic, including a zero and a negative saving producing `pays_back: false`
- payback suppressed when `months_included < 12`
- custom `costs` values overriding every default
- `predbat_annual_gbp` reducing only the Predbat row's saving, leaving capital and the
  other two rows untouched
- a `predbat_annual_gbp` large enough to exceed what Predbat adds, producing a *worse*
  payback than the PV-plus-battery row — and, when it exceeds the whole saving,
  `pays_back: false`. This is the case a naive "subscription as capital" implementation
  would get wrong, so it is asserted explicitly.

Engine and web tests cover: `panels`/`panel_watts` deriving `kwp`; both-supplied being
rejected; the `pv_only` scenario appearing in month rows and annual totals; the form's
kWp/panel toggle round-tripping; and the fourth chart colour being present and distinct.
