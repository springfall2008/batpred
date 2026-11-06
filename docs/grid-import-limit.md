# Grid import diagnostics (draft)

> **Diagnostics only — not a hard limit, safety device or complete fix.**
> This draft reports when the existing best forecast exceeds a configurable site
> threshold. It does not change optimisation, EV scheduling or inverter commands.
> Related: [#2449](https://github.com/springfall2008/batpred/issues/2449) and
> [#1199](https://github.com/springfall2008/batpred/issues/1199). Neither is closed by this stage.

## Configuration

An experimental `apps.yaml` setting, in **watts**, applies once to the whole site:

```yaml
pred_bat:
  import_limit: 6000
```

The default is `0` (diagnostics disabled). This stage accepts a finite, non-negative
literal number, not a list or entity. Invalid values are rejected during configuration
fetch rather than silently treated as disabled. Reloading a changed or removed setting
replaces the previous value; inverter count/order does not aggregate or retain it.

This is separate from `inverter_limit`, battery charge/discharge power, hardware limits
and `export_limit`. It does not modify any of them. The name and eventual enforcement
contract remain open for upstream review.

## Reported diagnostic

`predbat.best_import_limit` (using your configured prefix) has these states:

| State | Meaning |
| --- | --- |
| `disabled` | The threshold is zero or absent. Not a successful check. |
| `unavailable` | No complete, matching saved forecast was checked. |
| `within_limit` | No simulated step average exceeds the threshold in this forecast. |
| `exceeded` | At least one simulated step average exceeds the threshold. |

Attributes include `limit_w`, `peak_w` (peak import, not peak excess), `excess_kwh`,
`minutes_over`, `first_exceeded`, `step_minutes`, `checked_minutes`, `start` and `end`.
`scope` is `whole_site` and `enforced` is always `false`. Disabled/unavailable reports
clear the previous diagnostic values. Only the final saved **best** plan publishes
this entity; baseline and alternative solar scenarios cannot overwrite it.

The calculation observes the raw AC energy balance **without altering it**:

```text
step import W = max(net site import kWh, 0) * 60000 / step minutes
excess kWh = sum(max(step import W - limit W, 0) * step minutes / 60000)
```

Comparisons precede display rounding, with a numerical tolerance of 0.000001 W.
The diagnostic covers the full forward forecast, not elapsed daily energy or only
the optimiser's shorter `end_record` interval. Modelled EV load outside the house
CT is included once, assuming it shares the grid connection; metered EV load is
already in the balance. The existing CT-based grid-power graph remains unchanged.

## Important limitations

- **No enforcement:** grid charging is not throttled, battery preparation is not
  optimised for the threshold, and EV slots are not moved or rejected. An over-limit
  plan can still be selected and executed. Do not rely on this for electrical protection.
- **No proof of feasibility:** `exceeded` describes this forecast, not whether every
  alternative is impossible. `within_limit` does not guarantee real-time safety,
  forecast accuracy, EV connection, charging deadlines or reserve-latch compatibility.
- **Step averages:** normally five minutes, not instantaneous peaks, per-phase/current
  limits or rolling demand-tariff averages. Loads absent from the model cannot be checked.
- **No cost penalty:** even a large finite penalty cannot guarantee that every feasible
  candidate outranks every violating one, especially across varying/negative prices.
- **No kernel slowdown:** optimisation trials continue to use the native C++ kernel.
  Saved runs already use Python; only those produce this diagnostic. No optimisation
  return tuple, cache key or kernel ABI changes are required for this stage.
- **Offline proposal:** this draft has not been installed on or validated against live
  hardware. Existing inverter mappings (including local `SK` setups) are untouched.

## Proposed follow-up: a coupled planning constraint

1. Carry explicit constraint results through candidate search, caches, batching and
   Python/C++ paths. Rank feasible candidates before monetary cost; when none are
   found, report the unmet constraint without claiming global impossibility.
2. Include household demand, heating, PV, battery grid charging and actual fixed-rate
   EV demand together. Preserve all energy, battery losses, reserve, temperature/rate
   curves and shared/per-inverter AC limits. Never debit an empty battery while clamping
   its SoC at the reserve, or crop the grid graph to hide unmet demand.
3. Limit only the grid-funded portion of battery charging, preserving available PV
   charging. Model only dispatch/rate changes the real inverter can execute, including
   ECO battery-first behaviour, rather than assuming ideal peak-shaving hardware.
4. Integrate native EV slot selection and battery preparation: household loads take
   priority; a non-modulating charger remains a fixed on/off load. Use trustworthy
   connection evidence, charging losses, departure targets and configured pause/restart
   hysteresis. Do not invent a connection signal or a universal fixed SoC reserve.
5. Keep a single executor and real hardware protection. Validate the executed rates,
   deadlines, failure reporting and battery/EV interaction before any live rollout.
6. Treat rolling capacity-tariff averaging as a separate extension, not as this
   pointwise diagnostic or as an instantaneous protective limit.

## Offline verification

Tests use synthetic forecasts and mocked HA output, not private configuration or
hardware. Key checks cover disabled/enabled simulation equivalence, a battery at
reserve with fixed EV demand, raw excess accounting, PV/grid charging, configuration
reloads, step units, diagnostic reset and publication. They do not establish coupled
optimisation, automatic connection-only starting or a charging deadline guarantee.

```bash
cd coverage
./run_all --test import_limit > import-limit-tests.log 2>&1
./run_all --quick > quick-tests.log 2>&1
```
