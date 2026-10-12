---
type: design
---

# Joint site energy planning (experimental)

This development branch belongs to the [personal fork](https://github.com/arturonaredo/batpred).
It develops the behaviour requested in [issue #5490](https://github.com/springfall2008/batpred/issues/5490).
Shadow is isolated. Experimental execution is a separate, explicit opt-in for
commissioned local DEYE controls; it is not enabled by installing this branch.

The earlier import diagnostics remain a separate feature:
[PR #5486](https://github.com/springfall2008/batpred/pull/5486) neither implements
joint scheduling nor closes the new issue.

## Implemented boundary

This branch contains a bounded joint scheduler, synthetic regression cases, and
an opt-in **shadow** integration with native forecast data. It does not just report
an existing plan's excessive import: it searches different EV start/stop times
and home-battery demand/hold/charge sequences together.

In **shadow**, a fresh native `Prediction` replays the candidate in both the central
and PV10 scenarios. The candidate is published only on the separate
`predbat.joint_site_plan` entity; `execution_enabled` and `enforced` remain `false`.
The existing EV-slot signal, inverter windows, active prediction object and
kernel/cache are left untouched.

Experimental **apply** stages a successful candidate and revalidates it inside
the real `execute_plan` entry point. It dispatches the current command directly
through `Inverter.adjust_charge_immediate`, not through legacy window merging
which can join adjacent hold/charge targets. The legacy executor cannot run on top
of that command. See [Execution contract](#execution-contract) for the independent
hardware, freshness and permission gates; a successful replay does not satisfy them.

### Shadow inputs

`joint_site` is an optional `apps.yaml` mapping. Omitted or `mode: off` preserves
ordinary Predbat behaviour. `mode: shadow` cannot actuate. `mode: apply` is an
explicit execution request, not permission to skip commissioning or evidence gates.

The mapping accepts:

| Field | Meaning |
| --- | --- |
| `mode` | `off` (default), `shadow`, or explicitly commissioned `apply` |
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
| `allow_stale_shadow_soc` | Optional, default false: analyse the last valid but aged SOC as an explicitly labelled scenario; freshness remains an activation blocker |
| `apply_verified` | Boolean, default false; operator attestation that the particular hardware, limits, topology and sole EV executor have been commissioned |
| `ev_executor_lease_verified` | Boolean, default false; separate attestation that the sole external EV executor rejects expired or invalid version-1 leases even when Predbat stops |
| `connection_observed_at_entity` | Apply requires an offset-aware timestamp of the physical connection observation, not an unrelated vehicle message |
| `soc_observed_at_entity` | Apply requires an offset-aware timestamp of the actual EV SOC observation, not a cache access time |

Capacity, present domestic battery energy, charge/discharge limits, losses, EV
rate/capacity/target, tariffs and load/PV forecasts come from the native model.
The site import ceiling must be positive for this search, but neither shadow nor
the companion diagnostic enforces anything on hardware. Even apply remains a
planner, not instantaneous electrical protection. This PR can run without
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

Freshness and elapsed relay-OFF time use a wall clock read after the cached
observations, not Predbat's rounded five-minute forecast reference. Otherwise a
valid observation inside the current block would appear future-dated at startup.
Reports expose both references and their alignment difference. Entity timestamps
are never replaced with the time at which the cache was read.

For a parked vehicle that stops publishing SOC, `allow_stale_shadow_soc: true`
permits a **last-known-value assumption only in shadow**. The original observation
time and age remain visible, `soc_entity_stale_or_future` remains in the blocker
lists, and results are named `conditional_candidate`, `conditional_native_rejected`
or `conditional_no_plan_found`. An unavailable, malformed or genuinely future
observation is never substituted. This neither refreshes the vehicle nor proves
its present SOC/connection; it cannot authorise activation.

The initial integration supports one metered EV and one inverter, full-rate charge
and charge-freeze controls, no iBoost, and no manual mode/ceiling overrides. Domestic
manual minimum targets are retained as time-local SOC constraints and checked in
both the pure search and native replay; they are not converted to a permanent
reserve. Unsupported
cases have an explicit result rather than silently dropping the extra demand.
The solver uses an AC-equivalent model; hybrid PV conversion, power curves and
other native details can therefore reject a candidate during the independent
native replay. Such a rejection is **not** proof that no feasible plan exists.

In shadow, `control_model: deye_tou` is an explicit counterfactual scenario for commissioning:
it models full-rate charge, battery-first demand and hold with PV charging allowed
on an isolated copy. It does **not** change SK's capability flags or claim a hold
has been demonstrated on the installed firmware. Reports mark
`counterfactual_controls: true`, always keep `ready_to_activate: false`, and list
physical commissioning, coherent ToU configuration, executor equivalence and real
connection/permission as activation requirements. A successful hypothetical plan
is not evidence those requirements have been satisfied.

The DEYE scenario also considers **limited battery discharge**: a fixed battery
power ceiling for each interval, with the remaining load supplied by the grid.
The EV is still either off or at its configured full rate. The ceiling remains
fixed even if the EV completes partway through an interval; this is not an
invented continuously modulating grid-first mode. Whole-watt conversion includes
a small numerical rounding allowance, not a physical protection margin.

Time-local domestic SOC floors have a backwards reachability bound. A beam search
must retain states that can prepare for a future manual target rather than keep
only cheap low-SOC states and discover the target too late. The bound assumes
optimistic future charging and therefore only prunes states that cannot catch up;
it is not a replacement standing reserve.

If a nominal candidate fails the two native checks, one additional bounded search
uses maximum nominal/PV10 load, minimum PV and PV10 charge-delivery assumptions.
Both native scenarios still check the resulting full-rate commands. Reports label
the chosen `solver_forecast_basis` and `search_attempts`; a conservative search
objective must not be presented as measured savings or a proof of global minimum.
This DEYE scenario assumes zero export and reports curtailment explicitly, without
changing the installation's export configuration.

`proposed_discharge_power_limits` contains native DC-watt ceilings for limited
intervals; ordinary demand uses the existing full ceiling outside those intervals.
For these candidates, replay uses one fresh native Python `Prediction` per step,
setting its current-rate input while preserving the original SOC/temperature
curve maximum. The hot loop and kernel ABI are unchanged. The accumulated SOC
rounding bound is reported and considered at EV/manual thresholds. Existing
non-limited candidates still use whole-horizon native replay.

With the separate local DEYE PR and a `deye_shadow` mapping installed,
`deye_command_check` also translates every candidate step through the real
inverter entry point against copied HA states. It reports table-repair and hold
commissioning hypotheses separately. Success proves only software translation
under those hypotheses: it does not confirm physical writes, current-limit
availability, firmware behaviour or automatic activation readiness.

The report separates `solver_grid_cost` from `solver_objective` (grid cost plus
the configured cycle-cost surrogate, less terminal stored-energy credit).
The terminal credit uses Predbat's existing battery value rate and scaling.
Native replay's `horizon_energy_cost` excludes wear, terminal credit and standing
charges, and reserves full EV power for every proposed on step, including the
final partly-filled step. These quantities are labelled in the configured minor
currency unit; subtracting one from another is **not** a measured saving.

### Report states

- `candidate`: a schedule was found and passed both native forecast checks; still
  **not dispatched**. In apply it may be staged for the separately gated executor.
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

No trustworthy connection sensor is invented by this feature. Apply requires
separate real connection/session permission evidence, including staleness and
disconnection handling. Expected arrival alone cannot enable the existing EV-slot
signal.

### Execution contract

Apply currently requires the **separate local DEYE adapter**. The joint PR remains
independent and can be tested without that adapter; missing support blocks execution.
SK, Deye Cloud and generic inverter capability overrides do not become DEYE merely
because the hypothetical model worked in shadow.

- A candidate must pass complete nominal and PV10 native replay. Conditional stale
  scenarios are forbidden in apply. Every calculation first discards old staging.
- The actual DEYE adapter must have `control: true` and `hold_verified: true` after
  explicit commissioning. Its real table, operating mode and telemetry must pass
  validation without sorting times or changing capabilities in memory.
- Both field-specific EV observation timestamps must be fresh (at most 180 seconds)
  and not future-dated. A dated expected session, cached connection value, predicted
  manual EV SOC, or unrelated vehicle update is not a substitute.
- Connection must match its configured allowlist, permission must be exactly `on`,
  and the actual relay state and transition time must be available. The executor
  rechecks domestic SOC, restart hysteresis and minimum OFF time before granting
  an EV slot. It does not send EV switch services itself.
- Commands are dated and short-lived. Changed limits, manual constraints, stale
  plans, expired departure, unavailable observations and failed/pending inverter
  commands withdraw the joint EV signal rather than reverting to independent EV
  scheduling. Read-only, calibration and unsupported manual modes remain gates.
- Fetch-time `publish_car_plan` cannot leak the legacy EV schedule before the
  joint command is checked. Only the current accepted command can permit EV
  publication; a future candidate window alone has no execution authority.
- When the next action requires EV OFF, publication withdraws permission first
  and waits for the observed relay to be OFF before changing battery support.
- The physical write route is the real native inverter entry point. Standing
  charge/discharge current ceilings, reserve, shutdown, ToU times and work-mode
  settings are not raised or rewritten by the joint executor.
- Octopus Intelligent EV dispatch and registered native-plan consumers such as
  a gateway are unsupported in apply, rather than allowed to compete with it.
  The legacy `on_plan_executed` broadcast is suppressed in apply, including
  blocked cycles. Off/shadow continue their normal broadcasts.

Adapter success means **HA observations match**, not hardware confirmation. Reports
retain `ready_to_activate: false`; neither a boolean attestation nor software tests
establish electrical safety. The existing single HA EV executor and independent
protections must remain in charge of physical EV switching. This version must be
commissioned and reviewed before any authorised live migration; it is not a
production recommendation or proof of reaching a departure target.

The ordinary native best-plan graphs remain the independent baseline; the joint
report identifies the candidate and execution receipt. They must not be represented
as measurements or as a fully unified dashboard of the applied joint trajectory.

#### External EV lease requirement

The existing EV executor must also understand `source: joint_site_apply` and
`lease_version: 1` on the EV-slot binary sensor. It must require offset-aware
`valid_from <= now < valid_until`, with a positive duration no greater than
300 seconds, and preserve every existing grid, SOC, cooldown, connection and
manual-ownership guard. It must re-evaluate expiry independently of Predbat
publishing another state, including when Predbat has stopped. A cached `on`
or a fresh generic native status alone is not authority. Missing or invalid
lease attributes must fail closed for joint apply; legacy off/shadow slot
publication is unchanged.

This branch **does not install or replace that external automation**. Keep
`ev_executor_lease_verified: false` until its expiry behaviour and maximum
timer delay have been tested in the particular installation. Publishing expiry
metadata is not a promise of instantaneous physical OFF. Native status-age
watchdogs and electrical protections remain independent backstops.

Blocked apply makes no speculative recovery writes to the inverter: its last
accepted ToU controls can remain in force. The observed six-row commands are
not hardware leases. Commissioning must verify the inverter's safe persistence
and recovery behaviour; software receipt expiry is **not** inverter rollback.

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
