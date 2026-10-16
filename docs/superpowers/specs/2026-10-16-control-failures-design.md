# Control failure detection — design

Date: 2026-10-16
Status: approved design, awaiting implementation plan

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
- The inverter is calibrating.
- The inverter's status is anything other than the four judged modes (Demand, Hold charging, Hold
  exporting, Hold for car/iBoost, etc.).
- Predbat is not driving that window type: `set_charge_window` off for the charge modes,
  `set_export_window` off for the export modes.
- No plan has run since process start (no expectation exists yet).
- For Charging and Exporting, the requested rate is below the floor (too little power to judge).

An expectation cleared mid-slot simply stops further observation; an event already recorded stays.

### Per-mode rules

Battery power sign convention: negative = charging, positive = discharging (as used by the charge
curve learner, `inverter.py`). `P_req` is the rate actually written to that inverter.

| Mode | Judged only while | Failing reading when |
|---|---|---|
| Charging | `soc_percent < target_percent` | charge power (`-battery_power`) < `FAIL_FRACTION × P_req` |
| Exporting | `soc_percent > target_percent` | discharge power (`battery_power`) < `FAIL_FRACTION × P_req` |
| Freeze charging | always | `battery_power > FLOOR_W` (discharging) |
| Freeze exporting | always | `-battery_power > FLOOR_W` (charging) |

The SoC condition is evaluated on every reading using the fresh SoC, so reaching target between plan
runs stops judgement immediately instead of waiting for the status to flip to Hold. A reading for
which the SoC condition is false is not failing and resets the consecutive count.

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

Slot identity is `(inverter_id, mode, slot_start)`.

### Constants

| Name | Value | Meaning |
|---|---|---|
| `GRACE_S` | 300 | grace after entering a mode in a slot |
| `CONFIRM_READINGS` | 3 | consecutive failing readings to declare a failure |
| `FAIL_FRACTION` | 0.25 | Charging/Exporting fail below this fraction of `P_req` |
| `FLOOR_W` | 200 | minimum `P_req` to judge; wrong-way flow threshold for Freeze modes |
| `MAX_GAP_S` | 120 | cap on minutes credited between two readings |
| `DEFAULT_WINDOW_S` | 86400 | rolling window for the 24h sensor |

## Architecture

### New module: `apps/predbat/control_monitor.py`

No engine dependencies, so it is unit-testable with plain numbers, mirroring `control_ledger.py`.

```python
class ControlMonitor:
    def set_expectation(self, inverter_id, mode, slot_start, slot_end, requested_w, target_percent, now)
    def clear_expectation(self, inverter_id=None)   # None clears every inverter
    def observe(self, inverter_id, battery_power_w, soc_percent, now)  # -> event dict on first failure of a slot, else None
    def recent_events(self, now, window_s=DEFAULT_WINDOW_S)
    def newest_events(self, limit)
    def prune(self, now, window_s=DEFAULT_WINDOW_S)
    def restore(self, events, total)
    total  # lifetime count (int)
```

Per-inverter state: the current expectation, `entered_at`, `consecutive_failing`,
`pending_failed_s` (failed time accumulated before confirmation), `last_observed_at`, and a
reference to the slot's event once fired.

`set_expectation` with the same `(mode, slot_start)` as the current expectation updates
`requested_w`, `target_percent` and `slot_end` in place and keeps `entered_at` and the counters; a
different key resets them. `slot_start`/`slot_end` are epoch seconds.

Event record (a plain dict, JSON-serialisable for the HA attribute):

```python
{"inverter": 0, "mode": "Charging", "slot_start": ..., "slot_end": ..., "at": ...,
 "requested_w": 3000, "measured_w": 120, "failed_minutes": 3.0}
```

### Wiring

1. **`PredBat.__init__`** (`predbat.py`, beside `control_ledger`): `self.control_monitor =
   ControlMonitor()` and `self.control_monitor_restored = False`.
2. **`apply_inverter_rates`** (`execute.py`): store the rate actually written on the inverter as
   `inverter.requested_charge_w` / `inverter.requested_discharge_w`, with `None` resolved to the
   inverter maximum when rates are reset. This is the per-inverter value after the 60s balancer, not
   the pre-balance stored intent.
3. **End of `execute_plan`** (`execute.py`), after per-inverter status is final: for each inverter,
   `set_expectation(...)` when its status is one of the four judged modes and the matching
   `set_*_window` is on, otherwise `clear_expectation(inverter.id)`. Inputs: mode from
   `status_per_inverter`; slot start/end from `charge_window_best[0]` / `export_window_best[0]`;
   `requested_w` from step 2 (charge rate for Charging, discharge rate for Exporting); target from
   `inv_target_soc_percent` (Charging) or the export target percent (Exporting).
4. **Read-only and calibration early exits** in `execute_plan` (where `clear_control_ledger` is
   called today): `clear_expectation()` for all inverters.
5. **End of `fetch_inverter_data`** (quick and full): `observe(...)` for each inverter with its
   fresh `battery_power` and `soc_percent`. On a plan cycle this runs before `execute_plan`, so the
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

Restore happens once per process from the `events` attribute and the total's state. A restored
event whose slot is still live is re-attached to that inverter's state when the matching expectation
is next set, so a restart mid-slot neither re-warns nor re-counts. The total is restored numerically
and never goes backwards, which would otherwise register as a reset in HA long-term statistics.

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
- restore of a mid-slot event does not double-count
- total survives restore and never decreases

Wired into Predbat (`create_predbat`):

- `execute_plan` in read-only mode sets no expectation and nothing is counted
- calibration likewise
- a Charging plan with `battery_power` at 0 produces one `Warn:` line and both sensors at 1
- the per-inverter balanced rate, not the stored intent, is used as `P_req`

Metrics/dashboard: a round-trip test and a section render test, following
`tests/test_metrics_dashboard_control_conflicts.py`.
