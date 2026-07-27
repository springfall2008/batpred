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

**Chart colour:** a fourth series needs a fourth colour. It must be chosen from the
Okabe-Ito set and **validated with the dataviz validator against the other three, in both
light and dark mode, before use** — not eyeballed. The existing three
(`#0072B2`, `#D55E00`, `#009E73`) must not change, since users will compare against
earlier runs.

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
```

Every value is validated as a non-negative number. An absent `costs` block uses the
defaults, so existing configs keep working untouched.

## 4. Payback

Simple payback in years, each measured against the no-system baseline:

| Payback for | Capital | Annual saving |
|---|---|---|
| PV only | PV | `no_pvbat.cost_p − pv_only.cost_p` |
| PV + battery | PV + battery | `no_pvbat.cost_p − without_predbat.cost_p` |
| PV + battery + Predbat | PV + battery | `no_pvbat.cost_p − with_predbat.cost_p` |

```
years = capital_gbp / (annual_saving_p / 100)
```

Note the three rows share only two capital figures: Predbat is software, so it adds saving
without adding cost — which is precisely what makes its row worth showing.

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
    "pv_battery_predbat": {"pays_back": true, "years": 8.1, "capital_gbp": 12001.0, "annual_saving_gbp": 1481.6}
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

Engine and web tests cover: `panels`/`panel_watts` deriving `kwp`; both-supplied being
rejected; the `pv_only` scenario appearing in month rows and annual totals; the form's
kWp/panel toggle round-tripping; and the fourth chart colour being present and distinct.
