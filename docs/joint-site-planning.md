---
type: design
---

# Joint site energy planning (experimental shadow)

This development branch belongs to the [personal fork](https://github.com/arturonaredo/batpred).
It develops the behaviour requested in [issue #5490](https://github.com/springfall2008/batpred/issues/5490).
It provides no executable joint controller, regardless of where its shadow runs.

The earlier import diagnostics remain a separate feature:
[PR #5486](https://github.com/springfall2008/batpred/pull/5486) neither implements
joint scheduling nor closes the new issue.

## Implemented boundary

This branch contains a bounded joint scheduler, synthetic regression cases, and
an opt-in **shadow** integration with native forecast data. It does not just report
an existing plan's excessive import: it searches different EV start/stop times
and home-battery demand/hold/charge sequences together.

However, **it does not replace the executable native plan yet**. A fresh native
`Prediction` replays the candidate in both the central and PV10 scenarios. The
candidate is published only on the separate `predbat.joint_site_plan` entity;
`execution_enabled` and `enforced` are always `false`. The existing EV-slot signal,
inverter windows, active prediction object and kernel/cache are left untouched.

This is intentional, not a claim of completed control integration. In particular,
the native executor joins adjacent charge windows even when their targets differ.
Prediction of a hold followed by charge is not enough to prove that the executor
will preserve that transition. Window resolution, dwell time and actual inverter
behaviour still need an independently tested executable mapping.

### Shadow inputs

`joint_site` is an optional `apps.yaml` mapping. Omitted or `mode: off` preserves
ordinary Predbat behaviour. Only `mode: shadow` is accepted; `mode: apply` is
rejected rather than silently treated as an execution setting.

The mapping accepts:

| Field | Meaning |
| --- | --- |
| `mode` | `off` or `shadow` |
| `import_limit_w` | Positive whole-site import ceiling; may use the companion diagnostic's configured limit when installed |
| `control_model` | `native` (default) or the explicitly hypothetical `deye_tou` shadow model |
| `load_port_limit_w` | Explicit aggregate household + EV AC demand limit, distinct from `import_limit` |
| `session.available_from` | Offset-aware expected arrival timestamp |
| `session.departure` | Offset-aware departure inside the forecast horizon |
| `session.soc_kwh` | Observed EV stored energy; not a percentage or a connection signal |
| `session.soc_observed_at` | Offset-aware time of that actual observation |
| `soc_max_age_minutes` | Maximum age of the EV observation; default 60, maximum 1440 |
| `ev_pause_percent` / `ev_restart_percent` | Domestic-battery EV thresholds; defaults 15 / 25 |
| `min_off_minutes` | Minimum EV off interval; default 10 |
| `terminal_min_percent` | End-of-horizon battery floor; defaults to the configured domestic reserve |
| `beam_width` | Bounded search size; default 128, maximum 512 |
| `soc_entity` / `soc_entity_unit` | Optional current HA EV SOC entity and explicit `%` or `kWh` units |
| `relay_entity` | Actual HA relay state; `last_changed` determines elapsed OFF time |
| `connection_entity` / `connection_entity_states` | Optional connection observation and exact state allowlist, separate from energisation permission |
| `permission_entity` | Optional schedule permission observation; only exact `on` is reported as granted |

Capacity, present domestic battery energy, charge/discharge limits, losses, EV
rate/capacity/target, tariffs and load/PV forecasts come from the native model.
The site import ceiling must be positive for this search, but neither shadow nor
the companion diagnostic enforces anything on hardware. This PR can run without
the diagnostic PR: native replay then uses the upper rounding bound of saved
grid-power readings (0.5 W conservatism), not a claimed exact raw peak. No rate band is excluded
merely for being more expensive. A dated session expires: it is not a recurring
instruction to charge tomorrow regardless of the vehicle's whereabouts.

Unknown options, non-finite inputs, missing forecasts and stale/future EV
observations are rejected. Arrival and departure are rounded inward to five-minute
steps, never granting energy before arrival or after departure.

An arrival in the past means the expected availability interval has already begun;
the EV observation must still be fresh and the departure must still be future.
That remains an expectation, not confirmation of a connection.

With `relay_entity`, the adapter reads the cached `on`/`off` value and its real
transition time. ON remains ON, even if it started recently; OFF time is elapsed
time rounded down, not cooldown remaining. A missing configured relay blocks the
calculation. Without this optional mapping the explicitly labelled legacy
assumption is OFF with minimum cooldown elapsed; it is never permission to energise.

With `soc_entity`, a fresh cached observation replaces the dated SOC before checking
age. An unavailable configured entity does not fall back to stale dated data.
HA ISO timestamps and numeric strings are validated; unknown, non-finite, naive,
future and out-of-range data are rejected. The existing explicit EV manual SOC
setting retains precedence; it is not the domestic manual SOC target. Retaining
HA's `last_updated` separately from `last_changed` avoids equating an unchanged
value with an old observation. Cached HA freshness still does not prove physical
device freshness or connection, and no hardware refresh or service is requested.

The initial integration supports one metered EV and one inverter, full-rate charge
and charge-freeze controls, no iBoost, and no manual mode/ceiling overrides. Domestic
manual minimum targets are retained as time-local SOC constraints and checked in
both the pure search and native replay; they are not converted to a permanent
reserve. Unsupported
cases have an explicit result rather than silently dropping the extra demand.
The solver uses an AC-equivalent model; hybrid PV conversion, power curves and
other native details can therefore reject a candidate during the independent
native replay. Such a rejection is **not** proof that no feasible plan exists.

`control_model: deye_tou` is an explicit counterfactual scenario for commissioning:
it models full-rate charge, battery-first demand and hold with PV charging allowed
on an isolated copy. It does **not** change SK's capability flags or claim a hold
has been demonstrated on the installed firmware. Reports mark
`counterfactual_controls: true`, always keep `ready_to_activate: false`, and list
physical commissioning, coherent ToU configuration, executor equivalence and real
connection/permission as activation requirements. A successful hypothetical plan
is not evidence those requirements have been satisfied.

The report separates `solver_grid_cost` from `solver_objective` (grid cost plus
the configured cycle-cost surrogate, less terminal stored-energy credit).
The terminal credit uses Predbat's existing battery value rate and scaling.
Native replay's `horizon_energy_cost` excludes wear, terminal credit and standing
charges, and reserves full EV power for every proposed on step, including the
final partly-filled step. These quantities are labelled in the configured minor
currency unit; subtracting one from another is **not** a measured saving.

### Report states

- `candidate`: a schedule was found and passed both native forecast checks; still
  **not authorised or implemented for execution**.
- `native_rejected`: the candidate failed a native import, reserve or EV-threshold
  check. Proposed windows are retained for analysis, never for control.
- `no_plan_found`: bounded search did not find a complete plan; not a mathematical
  proof of infeasibility.
- `unsupported`, `invalid_input`, `error`: no candidate is published; any earlier
  candidate is cleared. Raw configuration or exception values are not exposed.
- `disabled`: previously enabled shadow output is cleared on disabling the feature.
- `departure_passed`: the dated session is closed, with no automatic next-day
  renewal. A later SOC observation is not proof the target was reached on time;
  departure-time evidence must come from the recorded history.

No trustworthy connection sensor is invented by this feature. A future execution
path will need a separate real connection/session permission gate, including
staleness and disconnection handling. Expected arrival alone must never enable
the existing EV-slot signal.

## Objective

Choose EV charging and home-battery charging/holding/demand operation together,
using real household history, heating demand, solar forecasts and all eligible
tariff bands. The aim is a physically feasible, low-cost whole-site plan, not the
lowest import-price label for the car in isolation.

Household demand is not silently shifted or shed. A fixed-rate EV remains an
on/off load at its actual rated power. The site import ceiling is distinct from
the inverter conversion, load-port and battery power limits.

## Behaviour contract

### Solar now, battery recovery later

Given usable PV of 5 kW, household demand of 1 kW and a fixed 6.77 kW EV,
the planner may use approximately 2.77 kW of battery support when physically
available. It must account for subsequent PV recovery rather than insisting the
home battery be full before the car is allowed to charge. It must not pretend
the EV has modulated down to the PV surplus.

### Night-time preparation and higher-price fallback

Given insufficient PV, the planner may buy cheap electricity for the home battery
before the EV needs its support. Preparation must fit the real charging rate,
losses, available time, domestic reserve and import ceiling.

When cheap hours alone cannot satisfy a departure target, the search includes
more expensive eligible hours before departure. Running out of cheap time is not
itself an unmet request. A genuine physical failure or a bounded search that finds
no plan must remain explicit; neither is repaired by clipping the import graph.

### Household priority and battery hysteresis

Given rising heating or household demand, the flexible EV/battery plan must adapt.
No household consumption disappears from the energy balance. Domestic reserve,
EV pause/restart thresholds and minimum off time are separate constraints, not
one arbitrary fixed reserve percentage.

### Expected availability versus actual connection

An expected arrival may establish a future EV energy need while the car is away.
It must not permit EV energy before arrival or authorise actual switching while
disconnected. Connection/session permission and expected availability are distinct
inputs. Unknown or stale EV state must not be silently treated as a confirmed
connection or a full battery.

### Full-horizon accounting

PV after a departure cannot meet an earlier EV deadline. It can affect the later
household battery value. Plans must preserve all energy, explicitly account for
losses and curtailment, and value terminal stored energy so a depleted battery
does not masquerade as a saving.

## Physical and execution boundaries

- Planning is not an instantaneous electrical protection mechanism.
- The inverter's actual demand/hold/charge behaviour matters. Do not assume a
  battery-first inverter only supplies the portion above an import ceiling.
- A nominal inverter rating does not establish verified load-port, grid-passthrough,
  battery/BMS or wiring capability. Those constraints need explicit configuration.
- No changes to hardware current limits, inverter mappings, polling frequency,
  unrelated Home Assistant automations or protection settings are part of this fork.
- Native Predbat remains the planner, with the existing single EV executor. A
  competing controller or an external solar-following service is not introduced.

## Related upstream work

- [#2449](https://github.com/springfall2008/batpred/issues/2449): import-limit modelling.
- [#5384](https://github.com/springfall2008/batpred/issues/5384): EV solar opportunity cost.
- [#5195](https://github.com/springfall2008/batpred/pull/5195): solar-led EV scheduling,
  but its solar mode currently assumes charger modulation; it is not a drop-in
  solution for a fixed-rate charger.
- [#3293](https://github.com/springfall2008/batpred/issues/3293): EV/battery concurrency.
- [#1199](https://github.com/springfall2008/batpred/issues/1199): rolling capacity
  tariffs, a separate billing feature from a per-step site import ceiling.

## Validation and release boundary

Implementation and acceptance evidence must distinguish the isolated solver,
native prediction validation, HA publication and actual hardware execution.
Synthetic tests must include every case above plus reserve exhaustion, negative
prices, malformed inputs and disabled-feature regressions. A bounded search is
not a proof of global optimality or physical impossibility.

The pure solver uses constant-power sub-intervals when a battery reaches its
reserve or capacity, or the EV reaches its requested energy or departure.
The EV is either off or at its configured fixed power in each sub-interval;
unused energy in a five-minute step is not fictitious power modulation.
The native replay deliberately reserves a whole on step, a conservative envelope
rather than a claim that its EV energy equals the solver's exact partial target.

Run the focused tests from `coverage/` with the project's isolated Python:

```bash
python ../apps/predbat/unit_test.py --test joint_site_core --test joint_site_native > joint-tests.log 2>&1
```

The core tests check conservation independently of the solver's physics helpers
and compare tiny exhaustive cases against a separately written reference.
Native tests exercise a real `calculate_plan` call through the real solver and
replay, including positive candidates, native rejection, disabled operation,
error/publication isolation and untouched actuator signals. The 48-hour test
asserts a deterministic search-work bound and reports elapsed time; it does not
impose a machine-dependent wall-clock CI threshold. Neither prediction engine's
dispatch semantics are changed by this feature; the existing Python/kernel parity
suite must still pass.

No installation recommendation follows merely from publishing this branch.
Hardware behaviour, real connection evidence and forecast quality still require
separate validation before any explicitly authorised live rollout.
