# Control failure detection — design

Date: 2026-10-16
Status: implemented

## Purpose

Predbat commands an inverter to charge, force-export, freeze-charge or freeze-export, but today it
has no record of whether the battery actually did it. A silently ignored command (a cloud write that
never landed, an inverter app overriding the mode, a BMS limit) shows up only as a plan that keeps
missing its targets.

This feature watches each controlled slot, decides when the battery is clearly not doing what was
asked, logs a single warning for that slot and counts it. It is the counterpart of the control
conflicts ledger (`control_ledger.py`): that one detects settings changed *outside* Predbat, this one
detects Predbat's own commands *not taking effect*.

It is observation only. Nothing here re-sends commands or changes behaviour.

## Scope

In scope:

- Per-inverter detection for four modes: Charging, Exporting (force export), Freeze charging,
  Freeze exporting.
- One `Warn:` log line and one count per failed slot per inverter.
- Two HA sensors (24h rolling count with events, lifetime total).
- Persistence of both across restarts via the sensors' own state/attributes.
- Prometheus gauges and a "Control failures" section on the metrics dashboard.
- Documentation in `docs/output-data.md`.

Out of scope:

- Any remedial action (retrying writes, alerts beyond the log line and sensors).
- User-configurable thresholds — constants until real data says they need tuning.

## Detection rules

### When no judgement is made

The monitor holds no expectation for an inverter — so nothing is observed or counted — when:

- Read-only mode is on (`set_read_only`). This is a hard requirement: read-only must never count.
- The inverter is calibrating. On the 60s poll this is read live from the `battery_calibration`
  sensor, not only the flag cached by the last plan run, so a calibration that starts between plan
  runs is seen at once.
- The inverter's status is anything other than the four judged modes (Demand, Hold charging, Hold
  exporting, Hold for car/iBoost, etc.).
- Predbat is not driving that window type: `set_charge_window` off for the charge modes,
  `set_export_window` off for the export modes.
- No plan has run since process start (no expectation exists yet).
- For Charging and Exporting, the requested rate (`P_req`, below) is below the floor (too little
  power to judge).
- The reading's `battery_power` could not be read — the sensor is unconfigured, `unavailable` or not
  a number. It still reads as 0 W elsewhere, as it always has, but `Inverter.update_status` records
  `battery_power_valid = False` and that reading is skipped entirely (not judged, no minutes, the
  consecutive count untouched): judging the stand-in 0 W would report an idle battery that may not be.
- The commanded end has passed (`now >= deadline`) — see Timing.

An expectation cleared mid-slot simply stops further observation; an event already recorded stays.

### Per-mode rules

Battery power sign convention: negative = charging, positive = discharging (as used by the charge
curve learner, `inverter.py`). `P_req` is the rate that inverter's battery can actually deliver:
the rate written to it, capped by the battery's charge (or discharge) power curve at the current SoC
and by its temperature cap — so a battery tapering near full, or a cold one, is not failing — and for
Exporting also by the headroom solar leaves: `AC limit − PV` on a hybrid inverter and
`export limit + load − PV` (a limit of 0 is treated as not set). It is computed on the engine side
(`Execute.expected_battery_rates`) on every reading and passed in, so the monitor stays engine-free.

| Mode | Judged only while | Failing reading when |
|---|---|---|
| Charging | `soc_percent < target_percent` | charge power (`-battery_power`) < `FAIL_FRACTION × P_req` |
| Exporting | `soc_percent > target_percent` | discharge power (`battery_power`) < `FAIL_FRACTION × P_req` |
| Freeze charging | always | `battery_power > FLOOR_W` (discharging) |
| Freeze exporting | always | `-battery_power > FLOOR_W` (charging) |

The SoC condition is evaluated on every reading using the fresh SoC, so reaching target between plan
runs stops judgement immediately instead of waiting for the status to flip to Hold. A reading for
which the SoC condition is false is not failing and resets the consecutive count.

For Exporting, `target_percent` is the fleet export target raised to at least that inverter's own
`reserve_percent`: `adjust_reserve()` clamps each inverter to its own minimum, so on a fleet with
20% and 4% reserves the first battery rightly stops at 20% while the fleet target is lower.

### Timing

- **Grace:** `GRACE_S = 300`, measured from `entered_at` — when this inverter entered this
  (mode, slot) — not from the slot's start. A mid-slot switch from Hold charging to Charging gets its
  own five minutes.
- **Confirmation:** a slot is declared failed on the `CONFIRM_READINGS = 3`rd consecutive failing
  reading after grace. Any non-failing reading resets the consecutive count to zero.
- **Failed minutes:** every failing reading after grace adds `min(now - last_observed_at,
  MAX_GAP_S)` with `MAX_GAP_S = 120`, so a skipped or throttled poll neither loses nor invents time.
  Minutes from the readings that led up to confirmation are included in the event.
- **Once per slot:** on confirmation the event is created, the `Warn:` line logged and both counters
  incremented. Later failing readings in the same slot only add to that event's `failed_minutes`.
- **Slot end:** a reading at or after the expectation's `deadline` — the end most recently
  commanded — is not judged and credits no minutes. The `deadline` is kept separately from
  `slot_end` (the span covered, only ever widened, which events are matched on), so a replan that
  shortens the active window stops judgement at the new end. The 60s poll keeps observing when a plan run is skipped (several `update_pred` early
  exits skip `execute_plan`), and by then the battery has rightly left the mode, so the stale
  expectation must not read the revert to Demand as a failure.
- **Reading spacing:** a reading less than `MIN_GAP_S = 30` seconds after the previous one for the
  same inverter is ignored entirely. A plan cycle's fetch and the 60s quick poll can land seconds
  apart, often returning the same sensor value; without this "3 in a row" could be met in about a
  minute.

Slot identity is `(inverter_id, mode, slot_start)`, where a slot is a **contiguous run** of the same
mode. The planner splits a long charge into back-to-back windows, so when the next window's start
falls inside the current slot's `[slot_start, slot_end]` the slot is extended rather than replaced:
the key, grace and counters carry on, and one sustained failure is counted once, not once per
half-hour window. For a charge slot the run stops where the charge/freeze instruction changes:
the inverter's schedule combines Charging, Freeze charging and Charging windows into one, but the
monitor treats each Charging run as its own slot, so a failure in the second is not absorbed into
the first one's event.

### Constants

| Name | Value | Meaning |
|---|---|---|
| `GRACE_S` | 300 | grace after entering a mode in a slot |
| `CONFIRM_READINGS` | 3 | consecutive failing readings to declare a failure |
| `FAIL_FRACTION` | 0.25 | Charging/Exporting fail below this fraction of `P_req` |
| `FLOOR_W` | 200 | minimum `P_req` to judge; wrong-way flow threshold for Freeze modes |
| `MAX_GAP_S` | 120 | cap on minutes credited between two readings |
| `MIN_GAP_S` | 30 | readings closer together than this are ignored |
| `DEFAULT_WINDOW_S` | 86400 | rolling window for the 24h sensor |

## Architecture

### New module: `apps/predbat/control_monitor.py`

No engine dependencies, so it is unit-testable with plain numbers, mirroring `control_ledger.py`.

```python
class ControlMonitor:
    def set_expectation(self, inverter_id, mode, slot_start, slot_end, target_percent, now)
    def clear_expectation(self, inverter_id=None)   # None clears every inverter
    def observe(self, inverter_id, battery_power_w, soc_percent, requested_charge_w, requested_discharge_w, now)
        # -> event dict on first failure of a slot, else None
    def recent_events(self, now, window_s=DEFAULT_WINDOW_S)
    def newest_events(self, limit)
    def prune(self, now, window_s=DEFAULT_WINDOW_S)
    def restore(self, events, total)
    total  # lifetime count (int)
```

Per-inverter state: the current expectation, `entered_at`, `consecutive_failing`,
`pending_failed_s` (failed time accumulated before confirmation), `last_observed_at`, and a
reference to the slot's event once fired.

`set_expectation` for the same mode with a `slot_start` inside the current slot updates
`target_percent`, extends `slot_end` and sets `deadline` to the new commanded end in place, keeping
`entered_at` and the counters; anything
else starts fresh tracking. Fresh tracking first looks for an existing event for that inverter and
mode whose slot contains the new start, and attaches to it (adopting its `slot_start`), so neither a
Hold-then-Charging re-entry nor a restart re-counts the slot. `slot_start`/`slot_end` are epoch
seconds.

The requested rate is passed to `observe` rather than fixed at `set_expectation`, because the 60s
balancer can change it between plan runs. `observe` picks the charge rate for Charging and the
discharge rate for Exporting; Freeze modes ignore both.

Event record (a plain dict, JSON-serialisable for the HA attribute):

```python
{"inverter": 0, "mode": "Charging", "slot_start": ..., "slot_end": ..., "at": ...,
 "requested_w": 3000, "measured_w": 120, "failed_minutes": 3.0}
```

### Wiring

1. **`PredBat.__init__`** (`predbat.py`, beside `control_ledger`): `self.control_monitor =
   ControlMonitor()`. Whether the restore has resolved is tracked on the monitor itself
   (`control_monitor.restored`).
2. **`apply_inverter_rates`** (`execute.py`): store the rate actually written on the inverter as
   `inverter.requested_charge_w` / `inverter.requested_discharge_w`, with `None` resolved to the
   inverter maximum when rates are reset. This is the per-inverter value after the 60s balancer, not
   the pre-balance stored intent.
3. **End of `execute_plan`** (`execute.py`), after per-inverter status is final: for each inverter,
   `set_expectation(...)` when its status is one of the four judged modes and the matching
   `set_*_window` is on, otherwise `clear_expectation(inverter.id)`. Inputs: mode from
   `status_per_inverter`; slot start from `charge_window_best[0]` / `export_window_best[0]`, slot
   end from the contiguous charge windows that share the first one's charge/freeze instruction /
   the export window end; target from `inv_target_soc_percent` (Charging) or the export target
   percent raised to the inverter's own reserve (Exporting).
4. **Read-only and calibration early exits** in `execute_plan` (where `clear_control_ledger` is
   called today): `clear_expectation()` for all inverters.
5. **End of `fetch_inverter_data`** (quick and full): `observe(...)` for each inverter with its
   fresh `battery_power`, `soc_percent` and the requested rates from step 2. If read-only mode or
   calibration has started since the last plan run, all expectations are cleared here instead, so
   nothing is counted in the minutes before the next `execute_plan`. On a plan cycle this runs before `execute_plan`, so the
   reading is judged against the command in force when it was taken. A returned event is logged as
   `Warn: Control failure: inverter {id} {mode} {HH:MM}-{HH:MM} - battery {charging|discharging}
   {measured}W vs {requested}W requested` (Freeze modes describe the wrong-way flow against the
   floor instead).

### Publishing and persistence

In `predbat.py`, directly after the `control_conflicts` block, with the same restore-once /
prune / recent / newest split:

| Entity | State | Attributes |
|---|---|---|
| `predbat.control_failures` | count of events in the last 24h; `state_class: measurement`, unit `failures` | `friendly_name: "Control failures (24h)"`, `icon: mdi:battery-alert`, `events` (newest 20 — the durable store restore reads), `by_mode` (24h count per mode) |
| `predbat.control_failures_total` | lifetime count; `state_class: total_increasing`, unit `failures` | `friendly_name: "Control failures (total)"`, `icon: mdi:battery-alert` |

### Restart behaviour

All durable data lives in the two entities; nothing goes through the Storage component (an async
component-side cache the core loop does not use). Restore reads them with
`load_previous_value_from_ha`, which tries the current state and falls back to the last history
entry. On Predbat.com the same calls are served by the DB component (`DatabaseManager`), so one
mechanism covers both deployments, as it does for control conflicts and the savings totals.

What survives a restart:

- **24h events** — restored from the `events` attribute. A restored event whose slot is still live is
  re-attached to that inverter's state (matched on inverter and mode, with the tracked slot start
  inside the event's slot), so a restart mid-slot neither re-warns nor re-counts, and its
  `failed_minutes` continue to accumulate. In production the first plan run after a restart sets
  the expectation *before* the publish cycle restores the events, so the attach is tried in three
  places: when an expectation is set fresh, at the end of every `restore()` for any tracking not yet
  attached, and when a contiguous window extends tracking that is still unattached.
- **Lifetime total** — restored numerically from the total entity's state.

What a restart deliberately drops: the in-flight per-inverter state (expectation, `entered_at`,
`consecutive_failing`, unconfirmed failed time). A slot that was failing but not yet confirmed starts
again with a fresh grace once the first plan run sets its expectation. Predbat re-sends its commands
on start-up, so the inverter is owed the same grace as on first entry. The cost is detection up to
`GRACE_S` + `CONFIRM_READINGS` minutes later than without the restart, and the pre-restart minutes of
an unconfirmed slot are not counted.

Guarding the total against a failed restore. If the restore reads nothing (HA unreachable or the
entity momentarily `unavailable` at start-up) and Predbat then publishes its in-memory total of 0,
the real value is overwritten and HA long-term statistics record a reset. Checking just before each
publish cannot recover from that once the 0 is written, so the guard is:

1. Until a restore read returns a value, the restore is retried on every publish cycle rather than
   once per process. The first attempt, and every 12th after it (`RESTORE_HISTORY_EVERY`, roughly
   hourly), uses the full `load_previous_value_from_ha` (state, then a history fallback); the
   attempts in between read current state only, filtering `unavailable`/`unknown` the same way, so
   an install that has never had a failure does not search 30 days of history every cycle. The first value read is merged as `total = restored + total`: before the
   restore resolves, the in-memory total holds only failures counted since this process started,
   so adding (not `max`) keeps them. The merge happens once; later restore calls merge events only.
2. While the restore is unresolved and the in-memory total is 0, the total entity is **not written**.
   On a genuinely fresh install this means `predbat.control_failures_total` first appears with the
   first counted failure; after that write, the restore is considered resolved.
3. Once resolved, the in-memory total starts from the restored value and only increments, so the
   published total never goes backwards.

Residual risk, accepted: if the entity exists but stays unreadable until a failure is counted, rule 2
lets that first write through and the old total is lost. That needs HA state and history both
unreadable for the whole period before the first failure, while the restore is retried every cycle
(with history consulted on the first attempt and about hourly after it).

The 24h `events` attribute gets only rule 1 (events read on a later attempt are merged in,
de-duplicated by slot key). If an empty list was already published over them they are lost, which is
acceptable: the 24h sensor is a rolling window and recovers within a day, and no statistics depend on
it.

### Metrics and dashboard

- `predbat_metrics.py`: gauges `predbat_control_failures_24h` and `predbat_control_failures_total`,
  plus a `control_failures_events` list, exported through the same dict as the
  `control_conflicts_*` fields.
- `predbat.py` metrics update: set them alongside the control conflicts metrics.
- `web_metrics_dashboard.py`: a "Control failures" section next to the control conflicts section —
  summary tiles (24h count, lifetime total) and a table of recent events (time, inverter, mode,
  slot, requested vs measured, failed minutes).

### Documentation

`docs/output-data.md`: both sensors, what a failure means, and the common causes (cloud polling lag,
an inverter app or schedule overriding the mode, a BMS charge/discharge limit, a failed write).

## Testing

New `apps/predbat/tests/test_control_monitor.py`, registered in `TEST_REGISTRY`, written test-first.

Monitor in isolation:

- nothing is judged during grace
- fires on the 3rd consecutive failing reading, not the 2nd
- a passing reading resets the consecutive count
- fires once per slot; `failed_minutes` keeps growing afterwards
- a new slot, or a new mode in the same slot, resets grace
- SoC reaching target stops judgement (Charging and Exporting)
- `requested_w` below `FLOOR_W` is not judged
- Freeze charging fails on discharge, Freeze exporting fails on charge, each only above `FLOOR_W`
- the `MAX_GAP_S` cap on credited time
- `clear_expectation` mid-slot stops observation but keeps the event
- prune and the 24h window
- restore of a mid-slot event does not double-count, and its `failed_minutes` continue
- restore merges by slot key without duplicating events
- total survives restore (`restored + counted`, merged once) and never decreases

Restart guard (wired into Predbat):

- restore read returns nothing: the total entity is not written while the total is 0, and the
  restore is retried next cycle
- restore succeeds on a later cycle after a failure was counted while unresolved: published total is
  `restored + counted`
- fresh install: the total entity first appears with the first counted failure

Wired into Predbat (`create_predbat`):

- `execute_plan` in read-only mode sets no expectation and nothing is counted
- calibration likewise
- a Charging plan with `battery_power` at 0 produces one `Warn:` line and both sensors at 1
- the per-inverter balanced rate, not the stored intent, is used as `P_req`

Metrics/dashboard: a round-trip test and a section render test, following
`tests/test_metrics_dashboard_control_conflicts.py`.
