# Fold Balance Into Execute — Design

Date: 2026-09-18. Status: agreed with maintainer.
Prompted by GH#5141 (mgazza, "Multi-inverter SoC balancing: should PredBat own it, and when
should it act?") and the multi-inverter rework tracking issue GH#4856.

## Purpose

`balance_inverters()` runs on its own 60-second timer, builds its own `Inverter` objects, knows
nothing about the plan, and writes charge and discharge rates that `execute_plan()` has just
set. Two independent writers disagreeing over the same registers is the root of several
long-open faults.

This design makes **`execute_plan()` the only writer of inverter rates**. Balancing becomes a
function that mutates the executor's intent before it is applied, rather than a subsystem that
overwrites the executor's work afterwards.

Two behavioural changes ride along:

- **Per-inverter export rate allocation**, mirroring the per-inverter treatment charge already
  gets, so the fleet holds the export power the planner costed instead of sagging as inverters
  reach target.
- **Cross-charge prevention becomes the default balancing behaviour**; SoC balancing on charge
  and on export stay fully functional but default off.

## Why this shape

Balanced SoC is not, in itself, an important goal — which is why balance charge and balance
export default off. The critical behaviour is preventing one inverter charging from another
during eco/idle, where energy cycles A into B at roughly 13% round-trip loss for no benefit.

The merge is worth doing independently of any balancing policy, because the two-writer seam is
what makes the current behaviour unpredictable, and because a single write point is a
precondition for any future proportional balancing.

## Non-goals

Explicitly out of scope, each deferred to a follow-up:

- **F1** — capacity-proportional target split in `adjust_battery_target_multi()`. The rate-share
  split stays. Consequence: the export allocator tracks targets that do not converge SoC. This
  is accepted, not overlooked.
- **F2** — fleet undershoot on charge above ~90%.
- A *per-inverter* PV-aware cross-charge rule — see "Amended during implementation" below for what
  was done instead, and what remains undone.
- Proportional balancing actuators. The actuator stays rate 0, as today.
- `inverter_limit` / `export_limit` (AC-side caps) in the export allocation.
- Making pause mode part of the mutable intent.

## Amended during implementation

Two items listed above as deferred were fixed after all, because moving the algorithm into a pure
function made faults visible that could not responsibly be left in place. Recorded here so a later
reader does not treat them as outstanding work.

**F7 — the partner ring (was: "moves verbatim").** Fixed. Reviewing the relocated algorithm on a
3+ inverter fleet showed the same logical fleet giving different answers depending on the order
the inverters happened to be configured in. Two coupled faults came with it:

- A pass could hold a charger and a discharger at once — the `elif` chain limits each *inverter*
  to one action, but the loop runs over every inverter. The units share an AC bus, so the rest
  simply absorb or supply whatever the held pair stopped doing.
- `can_power_house` asks "if I stop this one, can the rest cover?", so two holds in one pass each
  passed a check computed for one.

A pass now commits to a single direction, holds within it are applied cumulatively, the energy
guards ask whether *any* other inverter qualifies, and the rate guards consult no partner at all.

**Cross-charge detection (was: "today's power-sign rule moves unchanged").** Changed. The
power-sign rule chose direction from `sign(total_battery_power)`, a battery-side proxy for a
whole-site question. On an export window with a large PV surplus it held every inverter that was
correctly absorbing that surplus, because one inverter discharging into it made the batteries net
out as discharging.

Direction now comes from the site energy balance. Per-inverter load and PV readings cannot settle
it — one inverter discharging looks like less load to another, PV wiring is not declared, and an
AC-coupled unit has no PV of its own — but the fleet totals can. With grid +ve export and battery
+ve discharging, `load = pv + battery - grid`, so `spare PV = total_grid - total_battery`, needing
no PV attribution.

**Still undone:** the per-inverter case. Today's rule still cannot tell a hybrid absorbing its own
DC-coupled PV from one pulling off the bus; only the fleet aggregate is handled.

## Mode audit

The refactor collapsed several distinct "should we write a rate at all?" conditions into one
`None` sentinel meaning "reset to maximum". Each mode that needed a different answer had to be
restored individually as review found it, so this is the systematic pass over all of them.

**The rule the original encoded**, and the one the code now implements:

> A rate that something claimed — executor branch or balancer — is **always** written.
> A rate nobody claimed is reset to maximum **only** where the executor is driving the windows.

`reset_rates` in the intent carries that second condition; it is what
`resetCharge` / `resetDischarge` used to hold, initialised from
`set_charge_window or set_export_window`.

| Mode | charge_window | export_window | soc_enable | unclaimed rates | covered by |
|---|---|---|---|---|---|
| Monitor | off | off | off | **not written** | `test_monitor_mode_writes_no_rates` |
| Control SoC only | off | off | on | **not written**, target still set | `test_control_soc_only_sets_targets_without_writing_rates` |
| Control charge | on | off | on | reset to max | existing scenarios |
| Control charge & discharge | on | on | on | reset to max | existing scenarios |
| Read-only (incl. Axle) | — | — | — | loop `continue`s, nothing written | `test_read_only_mode_writes_no_rates` |
| Calibration | — | — | — | fleet set to max, intent discarded | `test_calibration_discards_intent_collected_so_far` |
| Calibration, seen by the poll | — | — | — | stored intent discarded | `test_poll_does_not_apply_stale_intent_during_calibration` |
| Template | — | — | — | `execute_plan` is not reached | pre-existing |

**Branches that can claim a rate while `reset_rates` is false.** Everything inside the loop is
gated on a window except two: the `else` after the charge-window checks, which only logs, and
`set_freeze_export_during_demand`, which claims `charge_rate = 0`. The original wrote that claim
regardless of the reset flags, so it must still be written — that is the first half of the rule
above, covered by `test_a_claimed_rate_is_written_even_when_rates_are_not_reset`.

**Unchanged by the refactor:** `resetPause` and `resetReserve` keep their original semantics and
application sites exactly (7 and 4 assignments respectively, same as before).

**Balancing is not gated on mode**, only on `balance_inverters_enable` and `set_read_only`. That
matches the deleted timer, which also ran in Monitor mode. Preserved deliberately rather than
tightened, since changing it is a behaviour decision rather than a refactor.

**Known behaviour change, accepted:** entering an export window, force export is now enabled
before the rate is written, where it used to be the other way round. In steady state neither call
rewrites, so this only bites on the transition cycle. It cannot be observed in the test harness,
which applies both within one synchronous pass - noted rather than pinned.

## Architecture

### Today

```
update_pred (5 min)     ──► execute_plan() ──────────────────────► writes rates
update_time_loop (15 s) ──► quick_inverter_data_update() (120 s)   read-only
run_time_loop_balance   ──► balance_inverters() (60 s) ───────────► writes rates
                              └── constructs its own Inverter objects
```

### After

```
update_pred (5 min)     ──► execute_plan() ──► build intent ──┐
                                                              ├──► apply_inverter_rates()
update_time_loop (15 s) ──► quick_inverter_data_update() ─────┘        (only writer)
                             (60 s)  └──► balance_inverters(intent)

run_time_loop_balance   ──► deleted
```

`quick_inverter_data_update()` already reuses `self.inverters` via `fetch_inverter_data(create=False)`
and already opens its own control-ledger cycle, so it is the natural second caller.

### The intent

Per inverter id, rebuilt by `execute_plan()` each cycle:

```python
{charge_rate, discharge_rate, pause_charge, pause_discharge, owner}
```

`charge_rate = None` means "max" — it is exactly today's `resetCharge = True`. `owner` records
which branch claimed the rate (`charge` / `export` / `freeze` / `demand` / `car` / `iboost`),
which is the information today's balancer has no way to see.

This **replaces** the `resetCharge` / `resetDischarge` flags rather than adding a parallel
mechanism. `apply_inverter_rates()` resolves `None → max`, so "restore to max" exists in one
place instead of three (the end-of-loop reset, and the balancer's own reset loop).

### Structure of `execute_plan()`

```python
intent = {}
for inverter in self.inverters:
    charge_rate    = None
    discharge_rate = None
    owner          = "demand"

    # 15 existing branches assign instead of writing
    # non-rate writes (adjust_force_export / adjust_reserve /
    #                  adjust_battery_target / adjust_pause_mode) stay inline, in today's order

    intent[inverter.id] = {...}

balance_inverters(intent, snapshot, ...)      # single mutation point, free to raise or lower

for inverter in self.inverters:
    apply_inverter_rates(inverter, intent[inverter.id])   # single write point
```

### Call sites to convert

All 15 are inside the per-inverter loop, and every one is already paired with a `reset*` flag
clear. Net effect: 15 rate call sites + 2 reset call sites + 2 reset flags become 2 locals and
one apply pass. The diff is mostly deletion.

`execute.py` lines 288, 292, 315, 354, 373, 489, 493, 509, 515, 552, 558, 584, 617, 634, 636.

Three need care rather than mechanical translation:

| Site | Today | Becomes |
|---|---|---|
| 288 | writes only if >10% from current | conditional **deleted** — see "Deadband" below |
| 584, 617 | `if resetDischarge: adjust_discharge_rate(0)` — car / iBoost hold only if unclaimed | `if discharge_rate is None: discharge_rate = 0`, making the precedence explicit rather than implied by flag order |
| pause | `adjust_pause_mode` is the alternative to rate 0 on inverters with timed pause | stays inline and unchanged, but is **recorded** in the intent as read-only context so balance can see a hold it must not fight |

### Ordering change (accepted)

Rate writes move from interleaved to after every non-rate write for that inverter. The only
observable case is entering an export window: today the rate is set (line 489) then force export
is enabled (line 491); afterwards force export enables first, so with low-power export the
inverter may run at the previous cycle's rate for the duration of the remaining loop I/O.

In steady state neither call rewrites — both have deadbands — so this bites only on the cycle
that *enters* a window. Accepted in exchange for a single unconditional write point.

## Deadband reconciliation

Both existing deadbands came from the same PR, GH#1676 ("Reduce register writes during low power
mode"), whose purpose was cutting register writes in low-power charging, where `find_charge_rate`
returns a slightly different value each cycle as SoC creeps toward target.

Both halves shipped broken and were fixed separately:

- Inverter side: GH#1676 wrote `abs(current - new) < (max * MINUTE_WATT / 24)` — inverted, so it
  wrote small differences and skipped large ones. Fixed by GH#1680 to `> /20` (5%).
- Execute side: the 10% shipped as `0.1 * battery_rate_max_charge` with no `MINUTE_WATT`,
  comparing watts against kW-per-minute (≈0.0043 for a 2.6 kW inverter), so it never fired.
  Fixed by GH#1677.

GH#1677 also had to move `resetCharge = False` out of the `if`: until then, skipping a small
adjustment left `resetCharge` True and the end-of-loop reset slammed the rate to **max** — a
deadband that caused full-rate charging. That is structurally the same failure as F5, and the
intent refactor makes it unrepresentable.

**Decision: 5% for both**, aligning with GE power steps. The execute-side conditional is deleted;
`charge_rate = new_charge_rate` unconditionally. `adjust_charge_rate` (`inverter.py:1870`) and
`adjust_discharge_rate` (`inverter.py:1911`) already carry an identical `/20`, so one deadband
remains, symmetric across charge and discharge, inside the only writer. Intent then always
carries the desired rate rather than a deadband-mangled one, which matters once balance mutates it.

## The balance function

Module-level in `utils.py`, alongside `find_charge_rate`, taking plain values and returning plain
values — no `self`, no `Inverter` objects, no writes:

```python
def balance_inverters(
    intent,              # mutated in place
    snapshot,            # list of per-inverter plain dicts
    balance_charge, balance_discharge, balance_crosscharge,
    threshold_charge, threshold_discharge,
    log_to=None,
)
```

Snapshot carries exactly what the algorithm reads today, as scalars:

```
soc_percent, reserve_percent, battery_power, pv_power,
charge_rate_now, discharge_rate_now,
battery_rate_max_charge, battery_rate_max_discharge, in_calibration
```

`execute.py` keeps three mechanical jobs: build the snapshot, call the function, apply the intent.
Every derived quantity currently computed inline in `balance_inverters` — `out_of_balance`,
`soc_low` / `soc_high`, `above_reserve`, `below_full`, `can_power_house`, `can_store_pv`,
`power_enough_charge` / `power_enough_discharge` — moves inside the pure function, which is what
makes them individually assertable for the first time.

The function mutates intent values freely in both directions. Raising a rate is not used by this
version, but the shape admits it so that future proportional balancing does not need another
refactor.

Behaviour preserved from today:

- The cross-charge rule moves verbatim, including the `(i + 1) % n` ring and the branch that skews
  a *different* inverter than the one being examined.
- Any inverter in calibration yields an empty mutation, matching today's `return False`.
  `execute_plan()` already handles calibration separately at `execute.py:183-194`.

### Coexistence with `set_freeze_export_during_demand`

That switch (expert mode, default off) already prevents cross-charging during demand, but bluntly
— it disables charging on every inverter, which also stops PV self-charging. Under the intent
model it sets `charge_rate = 0, owner = "demand"` on every inverter, so the cross-charge branch
finds nothing charging and does nothing. The two compose instead of fighting, which is not true
today, where the balancer's reset loop would raise those same rates back to max.

## Export allocator

A fleet-level pre-pass before the per-inverter loop, since it needs every inverter's state.
Free function in `utils.py`:

```python
def allocate_export_rates(needs, max_rates, p_fleet) -> list   # W per inverter
```

Each inverter's export target comes from `adjust_battery_target_multi(..., check=True)` — the
existing non-writing probe already used at `execute.py:248` — so the pre-pass computes targets
without touching the inverter.

```
P_fleet  = Σ(battery_rate_max_export_i) × export_rate_adjust      # unchanged from today
need_i   = max(0, soc_kw_i − target_kwh_i)                        # kWh still to shed
share_i  = need_i / Σ need
alloc_i  = clamp(P_fleet × share_i, 0, battery_rate_max_export_i)
           then spill clamped-off surplus to unclamped inverters, iterate to fixpoint
```

`battery_rate_max_export` is the correct field, matching `execute.py:489` — not
`battery_rate_max_discharge`.

### Degenerate cases

- `export_rate_adjust == 1.0` (low power off): `P_fleet` equals fleet max, every inverter clamps
  at its own maximum, and the allocation collapses to today's behaviour **exactly**. A provable
  no-op outside low-power mode, which is what lets this land without a switch.
- Matched fleet with equal needs: `share_i` uniform, identical to today.
- `Σ need == 0`: fall back to uniform.

### What it actually buys

Not SoC convergence — with F1 out of scope the targets still come from the rate-share split.

The win is that today the fleet's export power **sags below plan** as inverters reach target one
by one: an at-target inverter holds its 1/n slice of the budget while delivering nothing.
Allocation spills that slice to inverters that can still export, so the fleet holds the power the
planner costed for the whole window. This is the export analogue of F2 and it is a revenue
effect, not a cosmetic one.

## Faults closed

- **F5** (`GH#829`) — the balancer's unconditional reset overwriting a deliberate hold. Closed by
  construction: "max" is no longer a value balance can produce, and convergence returns to the
  executor's intent rather than the register ceiling. The mixed-fleet mechanism is the sharp case:
  inverters with timed pause express a hold via pause mode while the rest express it as rate 0, so
  the balancer sees "some at zero, some not" and resets the latter to max, repeatedly, until the
  next plan cycle.
- **Unguarded inverter re-creation** — `Inverter(self, id, quiet=True)` at `execute.py:1260` with
  no try/except, which turns an apps.yaml fault (GH#5033 class) into a `timer_tick` traceback every
  60 seconds. Gone; the poll reuses `self.inverters`.
- **Two writers disagreeing** — the structural cause behind the balancer-vs-executor seam in GH#4842.

## Config

| Item | Now | After | Reaches |
|---|---|---|---|
| `balance_inverters_enable` | off, "(Beta)" | unchanged | — |
| `balance_inverters_crosscharge` | on | unchanged | — |
| `balance_inverters_charge` | on | **off** | new installs only |
| `balance_inverters_discharge` | on | **off** | new installs only |
| `balance_inverters_threshold_charge` / `_discharge` | 1% | unchanged | — |
| `balance_inverters_seconds` | apps.yaml, 60 | **removed** | ignored if left behind |
| `INVERTER_QUICK_UPDATE_SECONDS` | 120 | **60** | everyone |

Balance charge and balance export remain fully functional when enabled; only their defaults change.

`load_current_config()` restores saved values from `predbat_config.json` (or the DB) and those take
priority over `default` (`userinterface.py:1047`), so changing a default reaches **new installs
only**. Existing users keep whatever they set. The population with the master switch on is small
and self-selected, so leaving their settings alone is correct, but the release notes should say so.

`validate_config()` iterates `for name in APPS_SCHEMA` (`predbat.py:1440`) — it validates known keys
only, so dropping `balance_inverters_seconds` leaves a stale apps.yaml entry silently ignored
rather than erroring.

### Poll rate cost

Moving `INVERTER_QUICK_UPDATE_SECONDS` from 120 to 60 doubles reads, not writes:
`write_and_poll_switch` is explicitly "re-written to minimise writes" (`inverter.py:2019`) and only
writes on a difference. The cost is read volume plus doubled drift-correction opportunities.

## Testing

### Pure functions — no `PredBat` instance, no mock HA

`balance_inverters`: heterogeneous fleet (differing `soc_max`, rate maxima, `inv_has_timed_pause`),
3+ inverters, threshold boundaries, calibration bail-out, and the **F5 regression directly** — an
intent of `charge_rate = 0` with `owner = "freeze"` must survive untouched. That is a one-line
assertion where today it would need a full integration test.

`allocate_export_rates`: sums to `P_fleet`; provable no-op at `export_rate_adjust == 1.0`; matched
fleet identical to uniform; at-target inverter spills its share; clamp fixpoint terminates;
single-inverter degenerate case; uses `battery_rate_max_export`.

### `test_balance_inverters.py` (844 lines) updated

Currently drives `balance_inverters(test_mode=True)`. Reworked onto the pure function plus the
execute path. The `test_mode` parameter disappears with the `Inverter()` re-creation it existed to
bypass.

### `test_execute.py` extended

`run_execute_test` already runs 2-inverter scenarios (`num_inverters = 2`, `soc_kw_array`), but
takes `soc_max`, `battery_max_rate` and `has_timed_pause` as scalars — so the execute path is only
ever tested with *identical* inverters. This is the accurate, narrower form of F10:
`test_multi_inverter.py` does already cover `adjust_battery_target_multi` with unequal capacities.

Add per-inverter arrays to the shared harness:

| Add | For |
|---|---|
| `soc_max_array` | heterogeneous capacity |
| `battery_max_rate_array`, `battery_max_export_rate_array` | heterogeneous rate ceilings, export allocator |
| `has_timed_pause_array` | the mixed-fleet F5 case — pause on one, rate 0 on the other |
| `assert_charge_rate_array`, `assert_discharge_rate_array` | per-inverter rate assertions |

Doing this in the harness rather than per-test is the F10 fix done once.

### Parity guard — no new work

With balance disabled and matched inverters, every existing scenario in `run_execute_tests` must
pass unchanged. 3247 lines of assertions become the regression net for the refactor for free. This
is the first thing to run and the main safety argument for a HIGH-risk change to `execute_plan`.

`debug_cases` is **not** applicable — it replays to a recalculated plan and does not exercise
`execute_plan`.

Full `./run_all` before the PR, and `detect_changes()` before committing.

## Risk

`impact({target: "execute_plan", direction: "upstream"})` reports **HIGH**: 4 execution flows
affected (`update_pred`, `run_time_loop`, `update_time_loop`, `initialize`), all with
`earliest_broken_step: 1`. It is the single write path for every inverter.

The mitigation is that risk concentrates in the *refactor* being behaviour-preserving rather than
in new behaviour switching itself on: the only change reaching existing installs is the poll rate.
Everything else is new-install-only or opt-in, and the existing execute suite is the parity net.

A second risk is that the quick poll becomes a write path, so it starts conferring control-ledger
ownership on a cadence that today only observes. `quick_inverter_data_update()` already calls
`begin_cycle()` (`execute.py:1078`), so the mechanism exists, but writes from this path will now
compete with the plan run's confirmations. This needs its own test rather than being assumed benign.

## Docs

- `docs/customisation.md:517-525` — removed setting, new defaults.
- `docs/apps-yaml.md:2271` — `balance_inverters_seconds` removed.
- `docs/caution.md:31` — currently warns users off the feature because it "can make register
  changes once or twice a minute". The reset-loop churn that caused that is what the intent model
  removes, so the warning should be revisited.

## Staging

1. **This PR** — the merge, the export allocator, the deadband reconciliation, the defaults.
   Master switch stays off.
2. **Follow-up, after hardware testing** — flip `balance_inverters_enable` on by default.
3. **Then, in order of value**: F7 ring (now a four-line change inside a tested pure function —
   the increment mgazza offered in GH#5141), AC limits in the allocator, pause into the intent, F1.
