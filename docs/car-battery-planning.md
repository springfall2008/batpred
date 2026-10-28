---
type: guide
---

# EV and battery planning

Predbat can plan a fixed-rate EV and the home battery together using native tariffs,
household forecasts, solar forecasts and battery constraints. The objective is a
low-cost whole-house outcome by departure, not just the cheapest rate for the car.
Household demand is never silently shifted or shed; the EV remains off or at its
configured full charging power.

This is an opt-in planning and execution path, currently limited to **one car and
one inverter**. Software validation does not establish physical commissioning,
electrical safety, sustained charging delivery or completion by departure. Those
remain unverified until separately demonstrated on the installation.

## Modes and native integration

Set the flat `apps.yaml` argument `car_charging_plan_mode`:

Quote the literal `"off"` in YAML so it is not parsed as a boolean.

| Value | Behaviour |
| --- | --- |
| `off` (default, including omission) | Preserve ordinary Predbat planning, execution and publication. |
| `shadow` | Search and replay on isolated inputs; no candidate actuator commands or executable EV authority. Ordinary native execution remains unchanged. |
| `apply` | Explicitly request execution through native plan, inverter execution and car-slot publication seams, subject to every execution guard. |

The planning report is carried by the **existing car charging slot attributes**,
with `source: predbat`; no separate planning sensor or second EV controller is
introduced. A report or a future charging window is not permission to energise.
Only the current accepted command and a valid expiring lease can grant authority
in apply mode. The sole external EV executor retains physical relay ownership.

Apply rejects configured native `hypervolt_control`, `gateway_evc_control`,
`myenergi_zappi_control`, `ge_cloud_evc_control`, `ohme_control` or `wallbox_control`,
including control retained by an already initialised component. These independent
polling loops do not rely on the plan-executed hook and cannot share ownership
with the external executor. A temporary native control switch being off is not
sufficient: configure those control options off before requesting apply. Enabling
them later invalidates the candidate or lease on its next validation. Automatic
shared control and outstanding held-charger state also block apply until ownership
has been released and settled outside this path. Wallbox's retained pause/schedule
releases, Hypervolt's outstanding releases (including absent chargers), and their
unread or unsaved ownership records must likewise settle first. This gate
does not disable, release or commission either controller, and cannot discover
every unrelated external writer.

Native forecasts supply battery capacity, reserve, energy, charge/discharge rates,
losses, car capacity/rate/target, tariffs and household/PV demand. Existing
`car_charging_soc`, `car_charging_plugged` and `import_limit` mappings are reused.
The car's existing `car_charging_rate` and `car_charging_loss` supply explicit power
and efficiency; no installation-specific charger rating is assumed.

## Flat configuration

All arguments below sit beside the existing car and inverter arguments in the
Predbat profile, **not in a new feature-specific YAML block**. Nested values such
as session evidence remain values of their documented flat argument.

| Argument | Meaning |
| --- | --- |
| `car_charging_plan_mode` | `off`, `shadow` or explicitly authorised `apply`. |
| `import_limit` | Positive whole-site import ceiling used by planning; not an electrical clamp. |
| `load_limit` | Explicit aggregate household plus EV AC load-port limit, distinct from import. |
| `car_charging_control_model` | `native`, hypothetical shadow-only `deye_tou`, or capability-constrained `deye_solarman`. Selecting a model does not enable hardware capabilities. |
| `car_charging_session` | Dated expected availability: offset-aware `available_from`, `departure`, observed `soc_kwh` and `soc_observed_at`. |
| `car_charging_soc_max_age_minutes` | Maximum SOC observation age; default 60 minutes, maximum 1440. |
| `car_charging_battery_pause_soc` / `car_charging_battery_restart_soc` | Domestic-battery EV pause/restart thresholds; defaults 15 / 25 percent. |
| `car_charging_min_off_minutes` | Minimum EV OFF interval; default 10 minutes. |
| `car_charging_priority` | `cost` (default), `asap`, `now`, `solar`, or a watched HA dropdown. |
| `car_charging_self_consumption_first` | Default false; with shared grid support, first try a complete continuous candidate using native Demand in the current step. Both native replays must pass; otherwise a separately validated ordinary candidate may be used. Not a ban on necessary import. |
| `battery_terminal_min_soc` | End-of-horizon domestic floor; defaults to the configured reserve. |
| `car_charging_search_beam_width` | Bounded search width; default 128, maximum 512. |
| `car_charging_soc` / `car_charging_soc_unit` | Current EV SOC mapping; `%` by default, or explicitly configured `kWh`. The native fetch path uses the same units. Existing explicit manual EV SOC retains precedence, without substituting for physical execution evidence. |
| `car_charging_relay` | Actual relay state and transition time; not a command target owned by this planner. |
| `car_charging_plugged` / `car_charging_plugged_response` | Connection observation and accepted states, separate from permission. |
| `car_charging_warning_states` | Additional exact connection states accepted with a deduplicated log-only warning; default empty. |
| `car_charging_state_validity` | Optional `{attribute, valid_states}` telemetry quality policy; no vendor-specific marker is assumed. |
| `car_charging_permission` | Schedule permission observation; only exact `on` grants permission. |
| `car_charging_allow_stale_soc_preview` | Default false; allow a labelled last-known SOC assumption in shadow only, retaining freshness blockers. |
| `car_charging_control_verified` | Default false; operator attestation of commissioning for the selected hardware, topology, limits and controller. Not physical proof. |
| `car_charging_executor_verified` | Default false; separate attestation that the sole external executor independently rejects invalid and expired version-1 leases, even if Predbat stops. |
| `car_charging_plugged_observed_at` / `car_charging_soc_observed_at` | Offset-aware timestamps of the actual field observations, not cache-read times or unrelated messages. |
| `inverter_grid_support` | Cached peak-shaving `enabled_entity` and `limit_entity` observations for the optional sharing model; not a hardware write. |
| `car_charging_confirmed_session` | Explicit operator baseline: `confirmed_at`, unchanged `departure`, `soc_kwh`, dedicated cumulative `energy_entity` / `energy_kwh`, and `power_entity`. |
| `car_charging_control_authorised` | Default false; explicit execution request for the metered shared-support path, distinct from commissioning verification. |
| `car_charging_error_acknowledgement` | Exact configured `state`, original `observed_at`, `acknowledged_at` and unchanged `departure` for an operator-attributed interruption event. Never a general error allowlist. |
| `car_charging_household_fallback` | Default false; withdraw EV authority and return to native household execution only after observed relay OFF. |
| `car_charging_home` | Optional vehicle tracker; only `home` counts as present. |
| `car_charging_partial_target` | Default false; separately authorise a validated partial shared-support apply target while retaining the original request and shortfall. |
| `car_charging_recurring_session` | Off-by-default standing policy, mutually exclusive with dated and confirmed sessions; see below. |
| `battery_min_window_minutes` | Optional override of the native saved battery minimum-window select; see below. |

Entity-valued car mappings follow native car order. This does not extend the
one-car/one-inverter execution boundary. Unknown or malformed planning inputs,
non-finite values and missing forecasts fail closed.

### Device cadence and measurement tolerances

These flat `car_charging_*` arguments describe device reporting cadence and
measurement uncertainty. Choose them from the actual meter and vehicle behaviour;
loosening a tolerance is not reconciliation of contradictory delivery evidence.
Invalid, non-finite or out-of-range values are rejected.

| Argument | Default | Allowed range |
| --- | --- | --- |
| `car_charging_meter_stall_seconds` | 600 | 30–7200 seconds |
| `car_charging_soc_recent_seconds` | 180 | 1–3600 seconds |
| `car_charging_soc_tolerance_kwh` | 0.5 | 0–5 kWh |
| `car_charging_soc_tolerance_percent` | 5 | 0–20 percent |
| `car_charging_meter_power_tolerance_percent` | 10 | 0–50 percent |
| `car_charging_meter_energy_tolerance_kwh` | 0.1 | 0–2 kWh |
| `car_charging_session_energy_tolerance_kwh` | 0.5 | 0–5 kWh |
| `car_charging_idle_power_kw` | 0.01 | 0–0.1 kW |

### Connection, observations and consent

Expected arrival is a forecast input, not a connection signal. Availability and
departure round inward to five-minute steps; no delivery is allocated before
arrival or after departure. Past arrival is allowed only with a future departure
and suitable evidence. Dated sessions expire rather than silently renewing tomorrow.

Connection codes have no built-in numeric meaning. Configure normal states and
optional warning states using the integration's real vocabulary. Warning states
do not waive permission, metering, telemetry quality or executor checks. Neither
list may accept HA `unknown` or `unavailable`. A configured quality attribute must
be present and valid. Unrecognised error states require exact-event acknowledgement
where that session path supports it; a later error publication is not covered.

Cached state is not proof of device freshness. Retain actual field timestamps and
HA publication times separately; never replace them with cache access time. The
clock used to assess freshness and elapsed OFF time is read after observations,
not rounded to the five-minute forecast start. Configured unavailable observations
do not silently fall back to older dated data. A stale-SOC shadow assumption stays
conditional and blocked from execution; malformed or future data is not substituted.

Relay ON stays ON even after a recent transition. OFF duration is elapsed time,
rounded down, not cooldown remaining. A missing configured relay blocks planning.
Any model-only assumption without a relay observation cannot authorise energising.

## Charging priorities

| Priority | Preference |
| --- | --- |
| `cost` | Compare whole-house economic outcomes through departure, including solar opportunity cost, cycling and terminal energy. A cheaper validated split schedule may beat continuity. |
| `asap` | Prefer nearby useful solar, then the next lower import-rate band, with an earlier deadline-preserving fallback. |
| `now` | Require a validated start in the current control interval, including any separately authorised partial alternative. Never silently substitute a later start. |
| `solar` | Use forecast household PV surplus only, with an independent fresh local-meter gate. No grid/battery fallback for EV charging. |

ASAP checks starts within 30 minutes with conservative surplus covering full EV
power for at least 15 minutes, or the complete shorter block. It then tries the
first lower tariff band, including a current band already below its predecessor.
Up to twelve preferred and twelve earlier fallback starts are considered at
five-minute resolution before the ordinary validated search. Full target and both
native replays still take precedence over waiting for a cheaper opportunity.
The cost path ranks up to twelve continuous candidates against an unrestricted
schedule. These are bounded searches, not proofs of global optimality.

An already active relay first tries continuation without inventing another OFF
interval. NOW still obeys availability, consent, reserve, hysteresis and cooldown;
`immediate_start_not_validated` is a bounded search result, not physical impossibility.
A satisfied target requests no extra charging. Priority changes take effect on the
next update-loop tick, not as direct relay commands.

`car_charging_priority` may reference an HA `input_select` or `select` whose values
are the supported policies. Add that entity to the existing `watch_list` for prompt
replanning. Missing or unavailable selections fail validation. Changes invalidate
the old receipt, including when a selection changes during calculation.
Existing `car_charging_plan_smart` and `car_charging_plan_max_price` do not select
these policies; more expensive eligible energy may still be needed for departure.

### Solar-only delivery

Eligible ON steps require lower-forecast PV, after conversion/caps, to cover the
higher household forecast plus full EV power. They use Demand rather than a
domestic grid-charge request, and independent replay must show no domestic battery
depletion. Whole-house losses and imports remain visible: solar-only EV does not
mean zero whole-house import.

Dispatch and lease renewal also check existing local `pv_power` and `load_power`
mappings with explicit W/kW units, readings under 60 seconds old and alignment
within 30 seconds. Starting requires 200 W extra headroom; continuation still
requires full EV power. When already charging, separately metered EV power is
subtracted from household load. The supported topology is one non-inverted
household power reading including the EV, with the shared single-inverter adapter.
Solar leases last at most 60 seconds and cannot outlive reading freshness. Missing
or lost surplus withdraws authority for the external executor, never switches the
relay directly, and is not an instantaneous electrical clamp.

Solar may miss departure. Any separately authorised partial plan retains the
original request and shortfall. Choosing solar does not renew consent or authorise
partial delivery; household handback still waits for observed EV OFF.

## Battery window stability

The native **Battery minimum window (minutes)** select uses the setting
`battery_min_window_minutes`, with choices **5, 15, 30 and 60**. New installations
default to **30 minutes**. **Explicitly preserve the previous saved choice during
migration, including 5 minutes; never replace it with the new-install default.**
The existing settings UI and save/restore mechanism own the selection.

The optional flat override accepts one of those integers, its exact string, or
an existing HA `input_select` / `select` entity. Referenced values come from cache
and fail closed when unavailable or malformed. Add external selectors to
`watch_list`; omit the override to use the persisted native select. A change during
calculation or dispatch rejects the previous candidate without an inverter reset.

This constrains planned battery control-mode runs, not actual instantaneous power.
EV decisions remain five-minute steps, independent of the calculation interval,
priority, cooldown and lease expiry. Runs need not align with clock half hours;
the final horizon-truncated run is allowed. The pure solver retains a five-minute
compatibility default; the native planner passes the resolved selection explicitly.

Observed successful dispatch remembers the mode's original start and carries only
its remaining commitment. Updating the same mode's SOC ceiling or EV request does
not restart it. SOC updates may still need a brief adapter source-disable phase;
uninterrupted electrical power is not promised. Manual changes, control drift,
household handback or restart discard continuity. Off/shadow do not start physical
commitments and ordinary household planning is not constrained by this setting.
Safety always wins: no obsolete command is held merely to finish a timer, and the
minimum is not silently shortened to meet departure. Longer windows can cost more
or prevent a validated full target; bounded failure is not proof of infeasibility.

## Optional grid-charge sharing model

With the Deye/Solarman adapter, `inverter_grid_support` can name the existing local
peak-shaving enable and limit observations. Missing, disabled, excessive or invalid
cached settings block the candidate. A grid-charge request can leave available
grid/PV headroom for domestic charging while household and EV demand take priority;
the battery may supply a deficit within its energy, rate and reserve limits.
Insufficient support remains a real import violation, never a clipped graph.

No hold, Program Power, current-limit or work-mode writes are implied. Opt-in
nominal and PV10 Python replay models this sharing; ordinary native plans and
unmodified kernel semantics remain unchanged. Cached settings are neither physical
commissioning nor a guaranteed instantaneous grid clamp.

## Sessions, household routing and partial targets

`car_charging_household_fallback` opts into availability-first routing in apply.
Away, disconnected, unpermitted, unauthorised and expired sessions are checked
before vehicle SOC or metered delivery. EV authority is revoked, but native
household execution resumes only after observed relay OFF. While ON or unknown,
existing battery support is retained and no second writer runs. Ordinary absence
does not turn into a global error or override read-only/manual controls.

An operator-confirmed session is an explicit baseline, **not fresh vehicle
telemetry**. Dedicated cumulative AC energy and power, with configured efficiency,
estimate delivery until coherent changed vehicle data arrives. Meter resets,
implausible delivery, contradictory SOC and an energised stalled meter fail closed.
Age, restart and deadline rollover cannot erase invalidation. Recovery requires
explicit reconciliation and, where required, a strictly newer confirmation, keeping
the original physical baseline, delivery evidence and failed record.

With `car_charging_control_authorised: true`, the narrowly scoped metered
shared-support path may be requested while `car_charging_control_verified` is
false. This distinguishes authorisation from physical verification, not a bypass
of lease verification, native-template validation, paired replay or safety guards.
It does not extend to hypothetical hold/discharge controls or other adapters.

`car_charging_partial_target` separately permits a lower validated planning target
for shared-support apply. The requested kWh/percentage remains unchanged. Paired
delivery evidence is bound to the exact staged commands and rechecked at dispatch,
including SOC changes. Shadow and authorised shared-support apply may try up to
eight partial searches. The result is the best validated alternative found, not
the maximum physically achievable delivery. No valid partial evidence means no
partial execution; deployment itself never renews consent.

### Optional standing daily policy

`car_charging_recurring_session` accepts `enabled: true`, an offset-aware
`policy_authorised_at`, and dedicated `energy_entity` and `power_entity` mappings.
Wh/kWh/MWh and W/kW/MW are normalised through the native unit converter, without
changing observation timestamps or resetting the physical energy baseline.
It is mutually exclusive with `car_charging_session` and
`car_charging_confirmed_session`. It requires apply, shared local grid support,
`car_charging_control_authorised: true` and `car_charging_household_fallback: true`.
It does not attest to commissioning or manufacture daily operator confirmations.

The first car's native `car_charging_plan_time` and configured `timezone` define
the next local deadline, including 23/25-hour days. Target, priority and partial
authorisation stay independent. Presence is checked only when `car_charging_home`
is configured; unknown or away then blocks. Omitting the tracker avoids reliance
on GPS but does not establish the charger location from connection alone. Changing
policy identity requires explicit retained-record reconciliation, never erasing
baseline, delivered energy or latched faults.

New arrival/connection baselines need observed relay OFF and zero EV power.
Unknown/unavailable telemetry withdraws the lease but is not proof of physical
unplug. A complete cached chain of eligible transitions can retain the baseline
across telemetry interruption and late energy batches. An ineligible state or
missing chain requires a new session. Storage retains physical evidence across
midnight, restart and deadline rollover; unreadable records fail closed, not as
first use. Metered SOC stays labelled as estimated. Old SOC cannot establish a
new completed session, and later energy is never assigned retroactively to an
earlier deadline. Missing durable storage blocks EV authority, not safe household
handback after OFF.

### Anticipated arrival is model-only demand

While a recurring-policy car is observed away with relay OFF, native household
prediction may include conditional future EV demand through the existing
**model-only car-slot seam**, never executable charger slots. It does not read
away SOC/energy or advance delivery counters. Without a tracker this forecast is
disabled. Real arrival, connection, consent and executor checks still govern charging.

Native cached Recorder history uses fourteen days of paired departures/returns,
excluding unknown gaps and repeated same-state reports. Similar trip timing and
weekday/weekend samples need at least three examples; sparse long trips can use
completed-day return times still ahead of the current time and before deadline,
also requiring three examples. The 75th percentile gives expected arrival, with
10th/90th percentiles describing uncertainty. History is cached for up to an hour;
a genuine new departure invalidates it without extra hardware polling.

Unknown away SOC is modelled conservatively as empty, explicitly **not measured
zero SOC**. Demand starts after expected arrival and ends by the ready deadline,
with shortfall shown. Show Debug retains confidence, samples and timestamps.
Household forecast totals include the scenario without imposing a new hard reserve.
Missing history, faults, changed intent, stale assumptions or actual return
withdraw it without granting EV authority.

## Native replay and plan display

Every accepted candidate passes fresh complete nominal and PV10 native replay,
including reserves, time-local manual domestic floors, import/rate constraints,
EV thresholds, cooldown and departure. The simplified AC-equivalent search can
miss native conversion details, curves and caps; native rejection is meaningful,
but not proof that another feasible plan cannot exist. A conservative retry uses
higher load, lower PV and PV10 delivery assumptions. Search attempts and forecast
basis remain visible; supported bounded retries do not prove global optimality.

The existing **Plan** table uses the candidate's own native replay, preserving
native columns, colours, precision and totals. Shadow is clearly marked
**simulation only — not executed**, with no executable native-plan replacement.
There is no second table. Five-minute peaks and partial first/last intervals must
not be averaged away. Detailed report metadata belongs in Show Debug, not extra
columns. Stale, failed or rejected reports never reuse an earlier trajectory;
simulation rows cannot edit native overrides. Turning planning off restores the
ordinary native view.

**Active plan** requires a valid observed current-command receipt and complete
paired traces. Expired or blocked execution clears candidate rows. Future rows
remain forecasts; read-back and permission do not establish delivered energy.
Independent native-plan diagnostics and ordinary best-plan graphs remain a
separate baseline, not measurements of the applied trajectory.

In shadow, charge ceilings derive from the highest nominal/PV10 native SOC within
each continuous charge run, plus one percentage point, rounded up and capped at
100%. Both scenarios replay again with those ceilings. Apply retains its model's
full-capacity contract unless explicit shared support allows the same revalidated
integer ceilings above reserve. A marked charge cell can contain several
five-minute modes; its SOC arrow describes the full displayed interval.

Cost reports separate grid cost, cycle-cost/terminal-energy objective and native
horizon energy cost. Native energy cost excludes wear, terminal credit and standing
charges, and reserves full EV power for every ON step, even a partly filled final
step. Solver sub-intervals conserve energy without inventing charger modulation.
Subtracting these different quantities does not establish measured savings.

Typical report states distinguish validated `candidate`, `partial_candidate`,
conditional stale-SOC previews, `applied`, `household_fallback`,
`household_waiting`, `native_rejected`, `no_plan_found`, `unsupported`,
`invalid_input`, `error`, `disabled` and `departure_passed`. A partial result retains
requested target, achievable forecast and shortfall. Rejected proposals are
diagnostic only; unavailable execution clears authority and obsolete output.

## Execution contract

The current supported execution topology is one metered car and one inverter.
Unsupported topology, iBoost, competing dispatch/controller consumers and unsupported
manual modes are rejected, not silently dropped. Read-only and calibration remain
guards. Apply suppresses the legacy plan-executed broadcast, including blocked
cycles; off/shadow preserve ordinary broadcasts.

Execution stages a candidate at native planning, revalidates inside `execute_plan`
and uses `Inverter.adjust_charge_immediate` with the actual adapter's observed
phase barriers. It does not merge incompatible adjacent legacy windows or allow
the legacy executor to run over the accepted command. Fetch-time
`publish_car_plan` cannot leak an unchecked independent EV schedule. Limits,
configuration, manual constraints, consent, freshness, controls and command age
are rechecked before publication and renewal. When EV OFF is required, withdraw
permission first and wait for observed OFF before changing battery support.

The direct-observation execution path requires both physical connection and SOC
timestamps to be no more than 180 seconds old and not future-dated. Expected
arrival, a cached value, an unrelated vehicle message or a manual model SOC is not
a substitute. Operator-confirmed metered sessions use their separately described
baseline and delivery evidence, not a claim that old vehicle fields became fresh.

### Adapter capabilities

The Deye/Solarman six-row source/SOC protocol is an **adapter capability**, not an
installation profile or evidence of physical behaviour. Its actual custom
`Inverter` profile, both verified service sequences and all twelve cached control
entities must validate. All six sources and six targets must match before granting
an EV lease. Model capacity, reserve and raw ceilings must match the real native
inputs; fixed-rate scaling must be 1.0. No speculative repair or silent capability
rewrite is permitted.

This path supports battery-first Demand and full-rate Charge, with optional
explicitly configured grid sharing. It does not support hold/freeze, limited
discharge, forced export, physical ToU-time changes or global-current writes.
Unavailable rows, partial services, changed mappings and observed drift revoke
authority without falling through to another writer. Standing current ceilings,
reserve, shutdown and work-mode settings are not raised or rewritten.

`deye_tou` is a separate counterfactual shadow model for battery-first demand,
hold and limited discharge hypotheses. It is not proof that those controls exist
on hardware and cannot be applied through the six-row adapter. Missing a separately
commissioned native adapter blocks execution; generic capability overrides and
cloud integrations do not acquire local hardware capabilities from a successful
simulation. Command translation on copied HA states proves software translation
under stated assumptions only.

Blocked apply makes no speculative inverter recovery writes. Existing accepted
controls may persist: **receipt expiry is not inverter rollback**. Safe persistence
and recovery require physical commissioning. Even observed matching HA controls
are not hardware confirmation; readiness must not be inferred from tests or a
boolean attestation.

### Sole external EV executor and expiring lease

The existing car-slot publication uses `source: predbat` and version-1 lease
metadata. The sole external EV executor must require offset-aware
`valid_from <= now < valid_until`, positive duration no greater than 300 seconds,
and all existing import, reserve, SOC, cooldown, connection, permission and manual
ownership guards. Missing/invalid metadata fails closed for apply. A cached ON,
future slot or generic fresh Predbat status is not sufficient authority.

The external executor must independently re-evaluate expiry even if Predbat stops.
This feature does not install or replace that automation. Keep
`car_charging_executor_verified: false` until expiry response and maximum timer
delay are tested. Expiry metadata cannot promise instantaneous physical OFF;
independent watchdogs and electrical protection remain essential.

The metered shared-support path may use the existing 15-second timer to revalidate
an unchanged observed command from cache without inverter services or extra polling.
Leases cannot renew beyond ten minutes from immutable plan generation. Normally
they cannot cross command boundaries or departure; the validated continuous-ON
shared path permits at most 30 seconds of non-rolling handoff overlap across a
domestic boundary only when its already validated next step also keeps EV ON.
It cannot start a future run, cross departure, waive checks or survive stalled
calculation. Solar uses the stricter freshness-bound 60-second maximum.

Partial writes withdraw permission. Restart requires new calculation and replay,
not revival of old authority. Leaving read-only can use the narrow confirmed
shared-support reinitialisation route only with EV observed OFF and the exact
paired-safe canonical sequence; failed writes retain the request and other reset
reasons remain blocked.

After a dated departure, shadow reports closure. Apply may calculate a **new**
house-only plan over the full horizon, retaining measured EV SOC and forbidding
all EV ON commands. It needs fresh complete paired replay and all applicable
gates; an old candidate cannot silently change identity at expiry. This does not
renew the session or establish that the departure target was delivered.

## Offline migration

Use `tools/migrate_car_planning.py` only with explicit input/output, profile,
previous settings key and **previous saved battery-window choice**. The following
paths and names are generic placeholders; replace them with your offline copies:

```bash
python tools/migrate_car_planning.py \
  --input <original-apps.yaml> --output <converted-apps.yaml> \
  --profile <profile-name> --settings-key <previous-settings-key> \
  --battery-window-minutes <saved-choice> --offline
```

`--offline` attests that the application is stopped; the utility does not stop it
or independently verify service state. Work on offline copies, not a running
application's evidence store.

When retained Storage evidence also needs copying, supply the optional pair
`--config-root <offline-config-root> --source-module <previous-storage-module>`.
This pair is required for retained confirmed or recurring sessions; missing expected
records block conversion rather than starting with empty evidence.
Do not omit one member of the pair or guess a settings key, source module or saved
choice. Accepted saved window choices are 5, 15, 30 and 60 minutes; preserve the
actual prior value, including 5, rather than selecting the new-install default.

Keep originals and `!secret` references intact; migration must not resolve secret
values or overwrite the original configuration/evidence. Review the converted
flat settings and retained records offline before any separately authorised use.
Copying records must preserve physical meter baseline, delivered energy, real
timestamps and latched invalidation. An unreadable or contradictory record is not
a new empty session. Configuration conversion, record copying and passing software
tests do **not** commission hardware, renew consent, reset faults, authorise apply
or prove that physical charging occurred. The tool writes a companion
`<converted-apps.yaml>.migration.json` receipt for offline review; this receipt is
configuration/evidence-copy verification, not physical execution verification.

## Validation boundary

Focused acceptance groups use the `car_planning_` prefix. From `coverage/`, save
the full log before inspecting results, for example:

```bash
PREDBAT_EV_TEMPLATE_REQUIRED=1 python ../apps/predbat/unit_test.py -k car_planning_ > car-planning-tests.log 2>&1
```

The required-template flag makes missing adapter dependencies fail rather than
counting skipped real-template scenarios as acceptance. Keep solver conservation,
native replay, publication isolation, execution guards and physical commissioning
as separate evidence. Synthetic fixtures use synthetic quantities; for example,
5 kW PV plus 1 kW household demand and a 7 kW fixed-rate EV needs about 3 kW
additional supply, not invented EV modulation. Existing Python/kernel parity and
ordinary disabled-path behaviour still matter. Sustained physical delivery,
independent expiry response and departure success need separate measured evidence.
