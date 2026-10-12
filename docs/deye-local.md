---
type: integration-guide
title: Experimental local Deye control
---

`inverter_type: DEYE` is an opt-in adapter for the six-period Time of Use (ToU)
controls exposed by [Solarman](https://github.com/davidrapan/ha-solarman).
It is **not** `DeyeCloud`, and does not change `SK` or existing installations.
Predbat remains the planner and executor: there is no extra HA automation,
direct Modbus client, cloud controller or background control loop.

**Software tests do not establish compatibility with an installed inverter or
firmware. Control is disabled by default. Do not migrate a live installation
just by changing its inverter type.** This experimental adapter is not a grid
import limiter, an EV charging solution, or a certified backup controller.

## Control contract

The native executor selects the current action. Native schedule entities are
virtual bookkeeping; they are not aliases for physical programs 1 and 2.
All six physical periods carry the same current action, so crossing a physical
period boundary cannot accidentally restore a different action.

| Native action | Charging source (all six) | SOC (all six) | Discharge power (all six) |
| --- | --- | --- | --- |
| Charge to target | `Grid` | Native target, bounded below by the configured reserve | Configured ceiling, clamped to native discharge limit |
| Demand/self-consumption | `Disabled` | Configured reserve, not a hard-coded SOC | Same bounded ceiling |
| Charge freeze/hold | `Disabled` | 100% | 0 W |

Hold means **no grid charging and no battery discharge, with surplus PV charging
still allowed**. It does not set global battery charge current to zero. A ToU
target is not a solar charge ceiling. Normal charging may discharge surplus SOC
down to its target, matching `set_discharge_during_charge` on this profile.
The charge rate must already be set and correctly modelled by the installation;
this adapter does not modulate it or raise current limits.

The hold contract must be commissioned on the actual inverter before setting
`hold_verified: true`. Only then is charge freeze advertised to the planner for
this instance. This does not change the `SK` capability declaration. If a hold is
requested without commissioning, it fails without falling back to grid charging.

### Transition and acknowledgement rules

1. Validate all six rows, units, ranges and operating-mode observations.
2. Before changing any SOC or power, disable charging sources in **all six** rows.
3. Wait until their HA states match, then write the new SOC and power values.
4. Only after every numeric HA state matches may `Grid` be enabled.
5. Skip unchanged controls; reconcile drift rather than trusting the last command.

An accepted service with a mismatching state remains pending. Retries wait at
least 300 seconds, stop after three attempts per unchanged requested value, and
never switch to another transport or register. A batch uses the existing HA
cache with a five-second settling interval; no hardware refresh is requested.
Failed or incomplete batches are not atomic and do not imply rollback. In
particular a partial source-disable operation may leave some old rows enabled.
Investigate the diagnostic rather than repeatedly restarting Predbat.

`predbat.deye_local_control` reports `disabled`, `read_only`, `pending`, `failed`
or `observed`. **Observed is not physical confirmation.** Solarman v26.01.31
updates entity state after a register write acknowledgement; that state can be
optimistic. `hardware_confirmed` remains false even when every HA state matches.
The adapter requires an existing inverter clock with a recent value before
enabled control, but clock freshness alone does not prove every register is fresh.

## Configuration and limits

### Preview without changing the current inverter type

An optional `deye_shadow` mapping accepts the same entity table and limits as
`deye_local`, with `control: false`, `hold_verified: false` and `telemetry_time`.
Keep the existing inverter type, generic mappings and service templates in this
case: they remain the real controller. Each native immediate charge/stop request
also exercises the DEYE translator against a private, in-memory state copy.
Proposed services update only that copy; neither capabilities nor native commands
are replaced. `predbat.deye_shadow` publishes the trace or a blocking reason.

The preview simulates successful acknowledgements; it does not test real write
acceptance. `ready_to_activate` and `execution_enabled` are always false. An
unordered live table or a hold awaiting commissioning remains a blocker, not a passing
test. Local failure-injection tests cover rejected and delayed acknowledgements.

When the observed six starts are distinct but out of order, the real result stays
`blocked`. An additional `table_repair_hypothesis` tests the translator with those
same times sorted **only in the private copy**. This shows the proposed commands
after a possible migration; it neither repairs the physical table nor fulfils
the activation precondition. Duplicate or invalid times are not guessed.

### Selecting the experimental controller

Start from the [disabled template fragment](https://github.com/springfall2008/batpred/blob/main/templates/deye.yaml),
merging it into an already validated configuration rather than replacing its
tariffs, history, forecasts, telemetry, capacity or safety limits.

- One inverter per Predbat instance, a SOC-based battery setup, and existing
  `Zero Export To CT` or `Zero Export To Load` with `Load First` are required.
- ToU must already be `Week`. `Enabled` alone does not prove all days are selected.
- Map each of the six distinct time, SOC, power and charging-source entities
  explicitly. Controls require whole-unit `%` and `W` values; do not guess scales.
- Existing physical times must be strictly ascending and distinct within the day.
  The sixth period wraps to the first. The first need not be midnight. Duplicate
  or non-sequential tables are rejected, not silently rewritten.
- Remove legacy service templates and generic output mappings that might compete
  with the adapter. Do not leave an external controller writing the same rows.
- `max_discharge_power_w` is an explicit per-period ceiling. Set it no higher
  than the already approved site limit, and preserve all global/BMS limits.
- `control: false` sends no physical adapter commands. It is not an instruction
  to stop an already running inverter or restore a previous table.
- `hold_verified: false` keeps planned charge freeze unavailable. Both switches
  require actual YAML booleans, not strings.
- With `control: true`, `telemetry_time` must name the existing inverter clock
  sensor or `datetime` entity, containing an ISO date/time. Naive timestamps use Predbat's timezone;
  ambiguous local times are rejected. Age must be at most 180 seconds, with no
  more than 30 seconds of future skew. Never increase hardware polling to pass it.

Native domestic manual **minimum/target** planning remains native. No separate
fixed reserve substitutes for it. Manual SOC **maximum** maps are not supported:
control fails visibly instead of pretending a ToU target can cap solar charging.
The adapter does not edit those manual settings. Forced export, export freeze,
low-power grid charging and freeze-export-during-demand are disabled in the
effective native configuration, without writing their UI persistence.

No writes target ToU hours, global charge/discharge current, battery shutdown,
BMS parameters, work mode, energy pattern, peak shaving, export policy, or EV
controls. No inference about topology, phase count or sustained capacity follows
from a configured power rating.

## Commissioning and stopping

Keep the existing controller unchanged while reviewing offline traces. Before
any live transition, capture the original six-row table and generic mappings,
verify the exact model/firmware and existing protections, and arrange a scoped
restoration plan. Check ordinary charge, demand, hold, target changes and midnight
transitions with independent measurements. Do not use disconnection or outage
experiments, increased limits, BMS bypasses or forced EV starts for validation.

This is immediate control, not a preloaded future schedule: if Predbat or HA
stops, the inverter retains its last table until another authorised change.
There is no new watchdog that automatically restores demand. Read-only mode,
disabling `control`, switching profile, and removing the adapter **do not undo
physical settings**. Restore the captured table through the established local
integration only under a separate, reviewed operational procedure.

## Evidence and remaining uncertainty

- [Deye SUN-12/16K-SG01LP1-EU manual, 240203, section 5.7](https://www.deyeinverter.com/deyeinverter/2024/02/03/instructions_sun-12-16k-sg01lp1-eu_240203_en.pdf)
  ([manufacturer-manual mirror](https://elkom-express.bg/pdf/instructions_sun-16k_en.pdf)):
  ToU power is the maximum permitted battery discharge power; grid charge enables
  charging during a period. Its example charges to 80% and discharges down to
  per-period SOC targets. This is not a guarantee that every firmware handles
  zero power, midnight or transient multi-register updates identically.
- [Solarman v26.01.31 profile](https://github.com/davidrapan/ha-solarman/blob/v26.01.31/custom_components/solarman/inverter_definitions/deye_hybrid.yaml):
  six times, powers, SOC targets and masked charging-source controls. Register
  definitions establish encoding, not electrical behaviour.
- [Solarman writable entity](https://github.com/davidrapan/ha-solarman/blob/v26.01.31/custom_components/solarman/entity.py):
  explains why an HA observation is weaker than independent physical read-back.

Exact firmware behaviour, hold effectiveness, measurement freshness and
simulation-to-hardware equivalence still require commissioning. There is no
claim that this adapter alone enforces an import cap or an EV departure target.
