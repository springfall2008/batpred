# Control Failure Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when the battery does not follow Predbat's Charging / Exporting / Freeze charging / Freeze exporting command, log one warning and count one failure per slot, and publish the counts as HA sensors and on the metrics dashboard.

**Architecture:** A new engine-free module `control_monitor.py` holds all judgement and bookkeeping (`ControlMonitor`). `execute.py` feeds it: `execute_plan()` sets each inverter's expectation once status is final, `apply_inverter_rates()` records the rate actually written, and `fetch_inverter_data()` passes each fresh reading to `observe()`. `predbat.py` publishes two sensors (24h count with an `events` attribute that doubles as the restore store, and a lifetime total) and feeds the Prometheus metrics that the metrics dashboard renders.

**Tech Stack:** Python 3, Predbat's own test harness (`apps/predbat/unit_test.py` + `TEST_REGISTRY`), Prometheus client gauges (`predbat_metrics.py`), a vanilla-JS dashboard fragment (`web_metrics_dashboard.py`), MkDocs.

**Spec:** `docs/superpowers/specs/2026-10-16-control-failures-design.md`

## Global Constraints

- Never count anything in read-only mode (`set_read_only`) — a hard requirement from the user.
- Never count while any inverter is calibrating (`inverter.in_calibration`).
- Constants exactly: `GRACE_S = 300`, `CONFIRM_READINGS = 3`, `FAIL_FRACTION = 0.25`, `FLOOR_W = 200`, `MAX_GAP_S = 120`, `MIN_GAP_S = 30`, `DEFAULT_WINDOW_S = 86400`.
- Battery power sign: negative = charging, positive = discharging.
- Entities: `predbat.control_failures` (24h count, `state_class: measurement`, unit `failures`, icon `mdi:battery-alert`, attributes `events` = newest 20, `by_mode`) and `predbat.control_failures_total` (`state_class: total_increasing`, unit `failures`, icon `mdi:battery-alert`).
- Persistence only through those entities (`load_previous_value_from_ha` / `dashboard_item`), never the Storage component.
- Total restore: retried every publish cycle until the total reads as a number; merged once as `restored + counted`; while unresolved and total is 0 the total entity is not written; after the first write the restore is resolved.
- Docstrings on every function and class (`interrogate` 100%); British English (CSpell `en-gb`) — add genuine new words to `.cspell/custom-dictionary-workspace.txt`; Black line length 256.
- Tests: save output to a file, then grep the file. Run from `coverage/`: `./run_all --test <name> > /tmp/<name>.txt 2>&1; grep -n "ERROR\|FAIL\|Traceback\|passed\|failed" /tmp/<name>.txt`.
- Pre-commit: `coverage/venv/bin/pre-commit run --files <files>` (new files must be `git add`ed first or pre-commit skips them).
- The shared `my_predbat` fixture means every test that mutates PredBat state must restore it in a `finally` (GH#5079).

## Review Focus

1. **Read-only toggled on between plan runs** — the user switches read-only on and the 60s poll observes before the next `execute_plan`. Expect: nothing counted. Pinned in Task 3 (`test_control_monitor_read_only_between_plan_runs`).
2. **A long charge split into back-to-back windows** — a sustained failure across several contiguous half-hour windows. Expect: one failure, not one per window. Pinned in Task 1 (`test_contiguous_window_is_same_slot`).
3. **Plan-cycle fetch and the 60s poll landing seconds apart** with the same stale value. Expect: the near-duplicate is ignored. Pinned in Task 1 (`test_close_readings_are_ignored`).
4. **Balancer holds one inverter at 0 W** while the fleet is Charging. Expect: that inverter is not judged (requested below the floor). Pinned in Task 3 (`test_control_monitor_uses_written_rate`).
5. **Restart in the middle of a failed slot whose plan has moved on to the next contiguous window.** Expect: no second warning or count, minutes keep accruing. Pinned in Task 2 (`test_restart_mid_slot_does_not_recount`).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `apps/predbat/control_monitor.py` | Create | Constants, `judge_reading`, `format_failure`, `ControlMonitor` (tracking, events, total, restore) |
| `apps/predbat/tests/test_control_monitor.py` | Create | Pure monitor tests + publish/restore tests against `my_predbat` |
| `apps/predbat/execute.py` | Modify | Record written rates; collect per-inverter slots in `execute_plan`; `record_control_expectations`; `observe_control_failures` |
| `apps/predbat/tests/test_execute.py` | Modify | Execute-level wiring tests on the `ActiveTestInverter` fleet |
| `apps/predbat/predbat.py` | Modify | Create the monitor in `reset()`; `publish_control_failures`; call it from `update_pred`; metrics in `_emit_snapshot_metrics` |
| `apps/predbat/predbat_metrics.py` | Modify | Gauges + events list, exported in `to_dict()` |
| `apps/predbat/web_metrics_dashboard.py` | Modify | Section 7 "Control Failures" + `mdRenderControlFailures` |
| `apps/predbat/tests/test_metrics_dashboard_control_failures.py` | Create | Metrics round-trip + section render tests |
| `apps/predbat/unit_test.py` | Modify | Register the new tests |
| `docs/output-data.md`, `docs/web-interface.md` | Modify | User documentation |

**GitNexus impact (run 2026-10-16, index slightly stale):** `execute_plan`, `fetch_inverter_data`, `_emit_snapshot_metrics` are **HIGH** (on the `update_pred` / `run_time_loop` main loop, one direct caller each); `apply_inverter_rates` and `get_metrics_dashboard_body` are **LOW**. Every edit to the HIGH symbols is an additive call at the end of the function. Re-run `impact` on each symbol before editing it (CLAUDE.md requirement) and stop if the risk has changed.

---

### Task 1: ControlMonitor core — judgement, tracking, once-per-slot events

**Files:**
- Create: `apps/predbat/control_monitor.py`
- Create: `apps/predbat/tests/test_control_monitor.py`
- Modify: `apps/predbat/unit_test.py` (import near line 223, registry entry near line 633)

**Interfaces:**
- Consumes: nothing.
- Produces (used by Tasks 2–5):
  - constants `MODE_CHARGING = "Charging"`, `MODE_EXPORTING = "Exporting"`, `MODE_FREEZE_CHARGING = "Freeze charging"`, `MODE_FREEZE_EXPORTING = "Freeze exporting"`, `JUDGED_MODES`, `GRACE_S`, `CONFIRM_READINGS`, `FAIL_FRACTION`, `FLOOR_W`, `MAX_GAP_S`, `MIN_GAP_S`, `DEFAULT_WINDOW_S`
  - `event_key(event) -> tuple`, `judge_reading(mode, requested_w, target_percent, battery_power_w, soc_percent) -> True | False | None`
  - `ControlMonitor()` with attributes `tracking: dict[int, dict]`, `events: list[dict]`, `total: int`, `restored: bool`
  - `ControlMonitor.set_expectation(inverter_id, mode, slot_start, slot_end, target_percent, now) -> None` (times are epoch seconds)
  - `ControlMonitor.clear_expectation(inverter_id=None) -> None`
  - `ControlMonitor.observe(inverter_id, battery_power_w, soc_percent, requested_charge_w, requested_discharge_w, now) -> dict | None`
  - event dict keys: `inverter, mode, slot_start, slot_end, at, requested_w, measured_w, failed_minutes`

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_control_monitor.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test control failure monitor
# -----------------------------------------------------------------------------

"""Tests for the control failure monitor.

The expensive failure for this feature is a false alarm - telling a customer their inverter
ignored Predbat when it was ramping up, legitimately limited, or simply not yet reported. Most of
these tests therefore assert SILENCE, and the ones that expect a failure check it fires exactly once.
"""

from control_monitor import (
    ControlMonitor,
    judge_reading,
    MODE_CHARGING,
    MODE_EXPORTING,
    MODE_FREEZE_CHARGING,
    MODE_FREEZE_EXPORTING,
    GRACE_S,
    FLOOR_W,
)

# 2023-11-14 00:00:00 UTC, so slot times format as 00:00-01:00 in UTC
T0 = 1699920000.0
SLOT_START = T0
SLOT_END = T0 + 3600


def close(a, b):
    """True when two floats are equal to within rounding."""
    return abs(a - b) < 1e-6


def charging_monitor(target=80.0):
    """A monitor with inverter 0 entering Charging at T0 for the slot T0..T0+1h."""
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START, SLOT_END, target, T0)
    return monitor


def feed(monitor, offsets, power, soc=50.0, charge_w=3000, discharge_w=3000, inverter_id=0):
    """Observe one reading at T0+offset for each offset and return every event that came back."""
    events = []
    for offset in offsets:
        event = monitor.observe(inverter_id, power, soc, charge_w, discharge_w, T0 + offset)
        if event is not None:
            events.append(event)
    return events


def test_judge_reading():
    """Each mode's failing condition, and the readings that cannot be judged at all."""
    failed = False
    cases = [
        # mode, requested, target, power, soc, expected
        (MODE_CHARGING, 3000, 80, 0, 50, True),  # asked to charge, idle
        (MODE_CHARGING, 3000, 80, -749, 50, True),  # just under 25%
        (MODE_CHARGING, 3000, 80, -750, 50, False),  # exactly 25% is passing
        (MODE_CHARGING, 3000, 80, 0, 80, None),  # target reached - not judged
        (MODE_CHARGING, FLOOR_W - 1, 80, 0, 50, None),  # too little requested to judge
        (MODE_CHARGING, None, 80, 0, 50, None),  # nothing written
        (MODE_EXPORTING, 2000, 20, 499, 50, True),
        (MODE_EXPORTING, 2000, 20, 500, 50, False),
        (MODE_EXPORTING, 2000, 20, 0, 20, None),  # at export target - not judged
        (MODE_FREEZE_CHARGING, None, None, FLOOR_W, 50, False),  # at the floor is tolerated
        (MODE_FREEZE_CHARGING, None, None, FLOOR_W + 1, 50, True),  # discharging past the floor
        (MODE_FREEZE_CHARGING, None, None, -3000, 50, False),  # charging is fine during freeze charge
        (MODE_FREEZE_EXPORTING, None, None, -(FLOOR_W + 1), 50, True),  # charging past the floor
        (MODE_FREEZE_EXPORTING, None, None, 3000, 50, False),
        ("Demand", 3000, 80, 0, 50, None),
    ]
    for mode, requested, target, power, soc, expected in cases:
        got = judge_reading(mode, requested, target, power, soc)
        if got is not expected:
            print("ERROR: judge_reading({}, {}, {}, {}, {}) = {}, expected {}".format(mode, requested, target, power, soc, got, expected))
            failed = True
    return failed


def test_nothing_during_grace():
    """Readings inside the grace period are never judged, however bad."""
    monitor = charging_monitor()
    events = feed(monitor, [60, 120, 180, 240], power=0)
    if events or monitor.total != 0 or monitor.tracking[0]["consecutive"] != 0:
        print("ERROR: grace readings were judged: events {} total {} consecutive {}".format(events, monitor.total, monitor.tracking[0]["consecutive"]))
        return True
    return False


def test_fires_on_third_reading():
    """Two failing readings after grace are not enough; the third confirms, with its minutes."""
    monitor = charging_monitor()
    feed(monitor, [60, 120, 180, 240], power=0)
    if feed(monitor, [GRACE_S, GRACE_S + 60], power=0):
        print("ERROR: fired before the third failing reading")
        return True
    events = feed(monitor, [GRACE_S + 120], power=0)
    if len(events) != 1 or monitor.total != 1:
        print("ERROR: expected exactly one event on the third reading, got {} (total {})".format(events, monitor.total))
        return True
    event = events[0]
    expected = {"inverter": 0, "mode": MODE_CHARGING, "slot_start": SLOT_START, "slot_end": SLOT_END, "at": T0 + GRACE_S + 120, "requested_w": 3000, "measured_w": 0}
    for key, value in expected.items():
        if event.get(key) != value:
            print("ERROR: event {} is {}, expected {}".format(key, event.get(key), value))
            return True
    # Reading at 300 credits nothing (grace ended at 300), then 60s + 60s
    if not close(event["failed_minutes"], 2.0):
        print("ERROR: failed_minutes {} expected 2.0".format(event["failed_minutes"]))
        return True
    return False


def test_passing_reading_resets():
    """A passing reading resets the run, and the minutes before it are not carried over."""
    monitor = charging_monitor()
    feed(monitor, [240], power=0)
    feed(monitor, [GRACE_S, GRACE_S + 60], power=0)
    feed(monitor, [GRACE_S + 120], power=-3000)
    if feed(monitor, [GRACE_S + 180, GRACE_S + 240], power=0):
        print("ERROR: the run was not reset by a passing reading")
        return True
    events = feed(monitor, [GRACE_S + 300], power=0)
    if len(events) != 1 or not close(events[0]["failed_minutes"], 3.0):
        print("ERROR: expected one event with 3.0 minutes after the reset, got {}".format(events))
        return True
    return False


def test_fires_once_and_minutes_grow():
    """Later failing readings in the same slot add minutes to the event but never re-fire."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    more = feed(monitor, [GRACE_S + 180, GRACE_S + 240, GRACE_S + 300], power=0)
    if more or monitor.total != 1 or len(monitor.events) != 1:
        print("ERROR: slot fired more than once: {} total {}".format(more, monitor.total))
        return True
    if not close(monitor.events[0]["failed_minutes"], 5.0):
        print("ERROR: failed_minutes {} expected 5.0".format(monitor.events[0]["failed_minutes"]))
        return True
    return False


def test_new_slot_resets_grace():
    """A non-contiguous new slot starts its own grace."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    later = SLOT_END + 600
    monitor.set_expectation(0, MODE_CHARGING, later, later + 3600, 80.0, T0 + 4500)
    track = monitor.tracking[0]
    if track["entered_at"] != T0 + 4500 or track["event"] is not None or track["slot_start"] != later:
        print("ERROR: new slot did not start fresh tracking: {}".format(track))
        return True
    if feed(monitor, [4560, 4620, 4680, 4740], power=0):
        print("ERROR: new slot judged inside its own grace")
        return True
    return False


def test_contiguous_window_is_same_slot():
    """The next back-to-back window continues the slot: same key, same grace, no second count."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START + 1800, SLOT_END + 1800, 80.0, T0 + 1800)
    track = monitor.tracking[0]
    if track["slot_start"] != SLOT_START or track["entered_at"] != T0 or track["event"] is None:
        print("ERROR: contiguous window started a new slot: {}".format(track))
        return True
    if feed(monitor, [1860, 1920, 1980], power=0) or monitor.total != 1:
        print("ERROR: contiguous window counted again, total {}".format(monitor.total))
        return True
    if monitor.events[0]["slot_end"] != SLOT_END + 1800:
        print("ERROR: event slot_end not extended: {}".format(monitor.events[0]["slot_end"]))
        return True
    return False


def test_new_mode_resets_grace():
    """A different mode in the same slot is a new slot with its own grace."""
    monitor = charging_monitor()
    monitor.set_expectation(0, MODE_FREEZE_CHARGING, SLOT_START, SLOT_END, None, T0 + 900)
    track = monitor.tracking[0]
    if track["mode"] != MODE_FREEZE_CHARGING or track["entered_at"] != T0 + 900:
        print("ERROR: new mode did not start fresh tracking: {}".format(track))
        return True
    return False


def test_reentry_does_not_recount():
    """Charging -> Hold charging -> Charging in the same slot re-attaches to the existing event."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    monitor.clear_expectation(0)
    monitor.set_expectation(0, MODE_CHARGING, SLOT_START, SLOT_END, 80.0, T0 + 900)
    if monitor.tracking[0]["event"] is not monitor.events[0]:
        print("ERROR: re-entry did not re-attach to the slot's event")
        return True
    before = monitor.events[0]["failed_minutes"]
    if feed(monitor, [1200, 1260, 1320], power=0) or monitor.total != 1:
        print("ERROR: re-entry counted the slot again, total {}".format(monitor.total))
        return True
    if not monitor.events[0]["failed_minutes"] > before:
        print("ERROR: re-entered slot did not accrue minutes")
        return True
    return False


def test_target_reached_is_silent():
    """Charging at/above target and Exporting at/below target are never judged."""
    monitor = charging_monitor(target=80.0)
    if feed(monitor, [240, 300, 360, 420, 480], power=0, soc=80.0) or monitor.tracking[0]["consecutive"] != 0:
        print("ERROR: Charging at target was judged")
        return True
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_EXPORTING, SLOT_START, SLOT_END, 20.0, T0)
    if feed(monitor, [240, 300, 360, 420, 480], power=0, soc=20.0):
        print("ERROR: Exporting at target was judged")
        return True
    return False


def test_low_requested_rate_is_silent():
    """A requested rate below the floor (low power, balancer hold) is never judged."""
    monitor = charging_monitor()
    if feed(monitor, [240, 300, 360, 420, 480], power=0, charge_w=FLOOR_W - 1):
        print("ERROR: a below-floor requested rate was judged")
        return True
    if feed(monitor, [540, 600, 660, 720], power=0, charge_w=None):
        print("ERROR: an unwritten (None) requested rate was judged")
        return True
    return False


def test_exporting_fails_on_low_discharge():
    """Exporting fails below 25% of the requested discharge rate and records that rate."""
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_EXPORTING, SLOT_START, SLOT_END, 20.0, T0)
    if feed(monitor, [240, 300, 360, 420], power=600, discharge_w=2000):
        print("ERROR: Exporting at 30% of the requested rate was judged failing")
        return True
    events = feed(monitor, [480, 540, 600], power=400, discharge_w=2000)
    if len(events) != 1 or events[0]["requested_w"] != 2000:
        print("ERROR: Exporting at 20% did not fail once with requested_w 2000: {}".format(events))
        return True
    return False


def test_freeze_modes():
    """Freeze charging fails on discharge, Freeze exporting on charge, each only past the floor."""
    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_FREEZE_CHARGING, SLOT_START, SLOT_END, None, T0)
    if feed(monitor, [240, 300, 360, 420], power=FLOOR_W):
        print("ERROR: Freeze charging at the floor was judged failing")
        return True
    events = feed(monitor, [480, 540, 600], power=500)
    if len(events) != 1 or events[0]["requested_w"] is not None:
        print("ERROR: Freeze charging discharging at 500W did not fail once: {}".format(events))
        return True

    monitor = ControlMonitor()
    monitor.set_expectation(0, MODE_FREEZE_EXPORTING, SLOT_START, SLOT_END, None, T0)
    if feed(monitor, [240, 300, 360, 420], power=3000):
        print("ERROR: Freeze exporting while discharging was judged failing")
        return True
    if len(feed(monitor, [480, 540, 600], power=-500)) != 1:
        print("ERROR: Freeze exporting charging at 500W did not fail once")
        return True
    return False


def test_gap_is_capped():
    """A long gap between readings credits at most MAX_GAP_S."""
    monitor = charging_monitor()
    feed(monitor, [240], power=0)
    events = feed(monitor, [GRACE_S, GRACE_S + 60, GRACE_S + 400], power=0)
    # 0 + 60 + min(340, 120) seconds = 3.0 minutes
    if len(events) != 1 or not close(events[0]["failed_minutes"], 3.0):
        print("ERROR: expected 3.0 capped minutes, got {}".format(events))
        return True
    return False


def test_close_readings_are_ignored():
    """Readings closer than MIN_GAP_S to the previous one are ignored entirely."""
    monitor = charging_monitor()
    feed(monitor, [240], power=0)
    feed(monitor, [GRACE_S, GRACE_S + 10, GRACE_S + 20], power=0)
    if monitor.tracking[0]["consecutive"] != 1:
        print("ERROR: close readings counted, consecutive {}".format(monitor.tracking[0]["consecutive"]))
        return True
    return False


def test_clear_keeps_events():
    """Clearing stops observation but keeps recorded events; clearing None clears every inverter."""
    monitor = charging_monitor()
    feed(monitor, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    monitor.set_expectation(1, MODE_CHARGING, SLOT_START, SLOT_END, 80.0, T0)
    monitor.clear_expectation(0)
    if 0 in monitor.tracking or len(monitor.events) != 1:
        print("ERROR: clear_expectation(0) lost the event or kept tracking")
        return True
    if monitor.observe(0, 0, 50.0, 3000, 3000, T0 + 900) is not None:
        print("ERROR: a cleared inverter was observed")
        return True
    monitor.clear_expectation()
    if monitor.tracking:
        print("ERROR: clear_expectation() left tracking {}".format(monitor.tracking))
        return True
    return False


def run_control_monitor_tests(my_predbat):
    """Run all control failure monitor tests."""
    failed = False
    for name, fn in [
        ("judge_reading", test_judge_reading),
        ("nothing_during_grace", test_nothing_during_grace),
        ("fires_on_third_reading", test_fires_on_third_reading),
        ("passing_reading_resets", test_passing_reading_resets),
        ("fires_once_minutes_grow", test_fires_once_and_minutes_grow),
        ("new_slot_resets_grace", test_new_slot_resets_grace),
        ("contiguous_window_same_slot", test_contiguous_window_is_same_slot),
        ("new_mode_resets_grace", test_new_mode_resets_grace),
        ("reentry_does_not_recount", test_reentry_does_not_recount),
        ("target_reached_silent", test_target_reached_is_silent),
        ("low_requested_rate_silent", test_low_requested_rate_is_silent),
        ("exporting_low_discharge", test_exporting_fails_on_low_discharge),
        ("freeze_modes", test_freeze_modes),
        ("gap_is_capped", test_gap_is_capped),
        ("close_readings_ignored", test_close_readings_are_ignored),
        ("clear_keeps_events", test_clear_keeps_events),
    ]:
        print("**** control_monitor: {} ****".format(name))
        if fn():
            print("ERROR: control_monitor test {} failed".format(name))
            failed = True
    return failed
```

Register in `apps/predbat/unit_test.py` — import beside the ledger import (line ~223):

```python
from tests.test_control_monitor import run_control_monitor_tests
```

and add to `TEST_REGISTRY` directly after the `("control_ledger", ...)` entry (line ~633):

```python
        ("control_monitor", run_control_monitor_tests, "Control failure monitor tests", False),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test control_monitor > /tmp/control_monitor.txt 2>&1; grep -n "ERROR\|Traceback\|ModuleNotFoundError" /tmp/control_monitor.txt | head`
Expected: `ModuleNotFoundError: No module named 'control_monitor'`.

- [ ] **Step 3: Write the implementation**

Create `apps/predbat/control_monitor.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Control failure monitor - detect Predbat's own commands not taking effect
# -----------------------------------------------------------------------------

"""Detect when the battery is not doing what Predbat commanded.

The control ledger (control_ledger.py) catches settings changed OUTSIDE Predbat. This catches the
other half: Predbat asked for Charging, Exporting, Freeze charging or Freeze exporting, and the
battery's measured power says it is not happening.

The expensive failure here is a false alarm, so judgement is deliberately conservative: a grace
period after entering a mode, a shortfall measured against the rate actually written (so low-power
and temperature-limited charging are not failures), and several consecutive failing readings
before anything is reported. Each failed slot is reported, and counted, once.

This module has no engine dependencies so it can be tested standalone.
"""

from datetime import datetime

MODE_CHARGING = "Charging"
MODE_EXPORTING = "Exporting"
MODE_FREEZE_CHARGING = "Freeze charging"
MODE_FREEZE_EXPORTING = "Freeze exporting"
JUDGED_MODES = (MODE_CHARGING, MODE_EXPORTING, MODE_FREEZE_CHARGING, MODE_FREEZE_EXPORTING)

# Seconds after entering a mode in a slot before any reading is judged - the inverter needs time to
# act on the command, and cloud integrations can take minutes to report it.
GRACE_S = 300
# Consecutive failing readings after grace before a slot is declared failed.
CONFIRM_READINGS = 3
# Charging/Exporting fail when the measured power is below this fraction of the rate written.
FAIL_FRACTION = 0.25
# Below this requested rate there is too little power to judge; also the wrong-way flow a Freeze
# mode tolerates before it counts as failing.
FLOOR_W = 200
# Cap on the failed time credited between two readings, so a long gap in polling is not counted.
MAX_GAP_S = 120
# A reading closer than this to the previous one is ignored - a plan-cycle fetch and the 60s poll
# can land seconds apart and return the same sensor value.
MIN_GAP_S = 30
# The rolling window the 24h sensor reports over.
DEFAULT_WINDOW_S = 86400


def event_key(event):
    """Return the identity of a failure event: (inverter, mode, slot_start)."""
    return (event.get("inverter"), event.get("mode"), event.get("slot_start"))


def judge_reading(mode, requested_w, target_percent, battery_power_w, soc_percent):
    """
    Judge one reading against the mode Predbat commanded.

    Battery power follows Predbat's convention: negative is charging, positive is discharging.

    Returns:
        True if the reading is failing, False if it is passing, None if it cannot be judged (the
        target is already reached, or too little power was requested to tell).
    """
    if mode == MODE_CHARGING:
        if requested_w is None or requested_w < FLOOR_W or target_percent is None or soc_percent >= target_percent:
            return None
        return -battery_power_w < FAIL_FRACTION * requested_w
    if mode == MODE_EXPORTING:
        if requested_w is None or requested_w < FLOOR_W or target_percent is None or soc_percent <= target_percent:
            return None
        return battery_power_w < FAIL_FRACTION * requested_w
    if mode == MODE_FREEZE_CHARGING:
        return battery_power_w > FLOOR_W
    if mode == MODE_FREEZE_EXPORTING:
        return -battery_power_w > FLOOR_W
    return None


class ControlMonitor:
    """Track, per inverter, whether the battery is following the mode Predbat commanded."""

    def __init__(self):
        """Start with nothing tracked, no events and a total of zero."""
        self.tracking = {}  # inverter id -> per-slot tracking state
        self.events = []  # oldest first; also the durable store published in the sensor attributes
        self.total = 0  # lifetime count of failed slots
        self.restored = False  # True once the total has been read back from its entity, or first written

    def _find_event(self, inverter_id, mode, slot_start):
        """Return the newest event for this inverter and mode whose slot contains slot_start, or None."""
        for event in reversed(self.events):
            if event.get("inverter") == inverter_id and event.get("mode") == mode and event["slot_start"] <= slot_start <= event["slot_end"]:
                return event
        return None

    def set_expectation(self, inverter_id, mode, slot_start, slot_end, target_percent, now):
        """
        Record what this inverter should be doing until the next plan run.

        The same mode with a start inside the current slot is the same slot - the planner splits a
        long charge into back-to-back windows - so it is extended in place and keeps its grace and
        counters. Anything else starts fresh tracking, attached to any existing event for that slot
        so that a re-entry, or a restart, cannot count it twice.
        """
        current = self.tracking.get(inverter_id)
        if current and current["mode"] == mode and current["slot_start"] <= slot_start <= current["slot_end"]:
            current["slot_end"] = max(current["slot_end"], slot_end)
            current["target_percent"] = target_percent
            if current["event"] is not None:
                current["event"]["slot_end"] = max(current["event"]["slot_end"], current["slot_end"])
            return
        event = self._find_event(inverter_id, mode, slot_start)
        if event is not None:
            slot_start = event["slot_start"]
            slot_end = max(slot_end, event["slot_end"])
            event["slot_end"] = slot_end
        self.tracking[inverter_id] = {
            "mode": mode,
            "slot_start": slot_start,
            "slot_end": slot_end,
            "target_percent": target_percent,
            "entered_at": now,
            "consecutive": 0,
            "pending_s": 0.0,
            "last_observed_at": None,
            "event": event,
        }

    def clear_expectation(self, inverter_id=None):
        """Stop judging one inverter, or every inverter when inverter_id is None. Recorded events are kept."""
        if inverter_id is None:
            self.tracking.clear()
        else:
            self.tracking.pop(inverter_id, None)

    def observe(self, inverter_id, battery_power_w, soc_percent, requested_charge_w, requested_discharge_w, now):
        """
        Judge one reading for one inverter.

        The requested rates are passed per reading rather than fixed when the expectation is set,
        because the 60s balancer can change them between plan runs. Charging is judged against the
        charge rate, Exporting against the discharge rate; the Freeze modes ignore both.

        Returns:
            The new event dict when this reading confirms the slot as failed, otherwise None. A slot
            returns an event at most once.
        """
        track = self.tracking.get(inverter_id)
        if track is None:
            return None
        last = track["last_observed_at"]
        if last is not None and now - last < MIN_GAP_S:
            return None
        track["last_observed_at"] = now
        grace_end = track["entered_at"] + GRACE_S
        if now < grace_end:
            return None

        mode = track["mode"]
        if mode == MODE_CHARGING:
            requested_w = requested_charge_w
        elif mode == MODE_EXPORTING:
            requested_w = requested_discharge_w
        else:
            requested_w = None
        if not judge_reading(mode, requested_w, track["target_percent"], battery_power_w, soc_percent):
            track["consecutive"] = 0
            track["pending_s"] = 0.0
            return None

        credited = 0.0
        if last is not None:
            credited = max(0.0, min(now - max(last, grace_end), MAX_GAP_S))

        event = track["event"]
        if event is not None:
            event["failed_minutes"] += credited / 60.0
            return None

        track["consecutive"] += 1
        track["pending_s"] += credited
        if track["consecutive"] < CONFIRM_READINGS:
            return None

        event = {
            "inverter": inverter_id,
            "mode": mode,
            "slot_start": track["slot_start"],
            "slot_end": track["slot_end"],
            "at": now,
            "requested_w": requested_w,
            "measured_w": battery_power_w,
            "failed_minutes": track["pending_s"] / 60.0,
        }
        self.events.append(event)
        track["event"] = event
        self.total += 1
        return event
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test control_monitor > /tmp/control_monitor.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/control_monitor.txt | head -20`
Expected: no `ERROR`/`Traceback` lines; the run reports the test passed.

- [ ] **Step 5: Pre-commit and commit**

```bash
git add apps/predbat/control_monitor.py apps/predbat/tests/test_control_monitor.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/control_monitor.py apps/predbat/tests/test_control_monitor.py apps/predbat/unit_test.py > /tmp/pc1.txt 2>&1; grep -v "Passed\|Skipped" /tmp/pc1.txt
```

Fix anything reported (CSpell: add genuine words to `.cspell/custom-dictionary-workspace.txt` and `git add` it), re-run until clean, then:

```bash
git commit -m "feat(control-monitor): judge each reading against the commanded mode, once per slot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: ControlMonitor retention, restore and log formatting

**Files:**
- Modify: `apps/predbat/control_monitor.py`
- Modify: `apps/predbat/tests/test_control_monitor.py`

**Interfaces:**
- Consumes: Task 1's `ControlMonitor`, `event_key`, constants.
- Produces (used by Tasks 3–5):
  - `is_valid_event(event) -> bool`
  - `format_failure(event, tz=None) -> str` — the full `Warn: Control failure: ...` line
  - `ControlMonitor.recent_events(now, window_s=DEFAULT_WINDOW_S) -> list[dict]`
  - `ControlMonitor.newest_events(limit) -> list[dict]` (oldest first)
  - `ControlMonitor.prune(now, window_s=DEFAULT_WINDOW_S) -> None`
  - `ControlMonitor.by_mode(events) -> dict[str, int]` (static)
  - `ControlMonitor.restore(events, total) -> bool` (True once the total is resolved)

- [ ] **Step 1: Write the failing tests**

Add to the imports at the top of `apps/predbat/tests/test_control_monitor.py`:

```python
import json

import pytz

from control_monitor import format_failure, DEFAULT_WINDOW_S
```

Add these tests above `run_control_monitor_tests`:

```python
def make_event(at, mode=MODE_CHARGING, inverter_id=0, slot_start=SLOT_START, failed_minutes=2.0):
    """A well-formed event as it would be read back from the sensor attribute."""
    return {"inverter": inverter_id, "mode": mode, "slot_start": slot_start, "slot_end": slot_start + 3600, "at": at, "requested_w": 3000, "measured_w": 0, "failed_minutes": failed_minutes}


def test_recent_and_prune():
    """recent_events() and prune() use the 24h window; prune keeps an event a live slot still owns."""
    monitor = ControlMonitor()
    monitor.events = [make_event(T0, slot_start=T0), make_event(T0 + DEFAULT_WINDOW_S + 100, slot_start=T0 + DEFAULT_WINDOW_S)]
    now = T0 + DEFAULT_WINDOW_S + 200
    if [event["at"] for event in monitor.recent_events(now)] != [T0 + DEFAULT_WINDOW_S + 100]:
        print("ERROR: recent_events returned {}".format(monitor.recent_events(now)))
        return True
    monitor.tracking[0] = {"event": monitor.events[0]}
    monitor.prune(now)
    if len(monitor.events) != 2:
        print("ERROR: prune dropped an event still attached to a live slot")
        return True
    monitor.tracking.clear()
    monitor.prune(now)
    if len(monitor.events) != 1:
        print("ERROR: prune kept an expired event: {}".format(monitor.events))
        return True
    return False


def test_newest_events_and_by_mode():
    """newest_events keeps the newest N oldest-first; by_mode counts per mode."""
    monitor = ControlMonitor()
    monitor.events = [make_event(T0 + i, slot_start=T0 + i) for i in range(25)]
    newest = monitor.newest_events(20)
    if len(newest) != 20 or newest[0]["at"] != T0 + 5 or newest[-1]["at"] != T0 + 24:
        print("ERROR: newest_events(20) returned the wrong slice")
        return True
    counts = ControlMonitor.by_mode([make_event(T0), make_event(T0 + 1, mode=MODE_EXPORTING), make_event(T0 + 2)])
    if counts != {MODE_CHARGING: 2, MODE_EXPORTING: 1}:
        print("ERROR: by_mode returned {}".format(counts))
        return True
    return False


def test_restore_merges_and_dedupes():
    """Restore merges valid events by key (keeping the larger minutes), skips junk, sorts by time."""
    monitor = ControlMonitor()
    monitor.events = [make_event(T0 + 50, slot_start=T0, failed_minutes=2.0)]
    restored = [
        make_event(T0 + 50, slot_start=T0, failed_minutes=5.0),
        make_event(T0 + 10, slot_start=T0 + 7200),
        {"inverter": 0, "mode": "Demand", "slot_start": T0, "slot_end": T0, "at": T0, "failed_minutes": 0},
        {"inverter": True, "mode": MODE_CHARGING, "slot_start": T0, "slot_end": T0, "at": T0, "failed_minutes": 0},
        "not an event",
    ]
    if not monitor.restore(restored, "7"):
        print("ERROR: restore with a numeric total did not resolve")
        return True
    if len(monitor.events) != 2 or monitor.events[0]["at"] != T0 + 10:
        print("ERROR: merged events wrong or unsorted: {}".format(monitor.events))
        return True
    if not close(monitor.events[1]["failed_minutes"], 5.0):
        print("ERROR: duplicate did not keep the larger failed_minutes")
        return True
    if monitor.total != 7 or not monitor.restored:
        print("ERROR: total {} restored {}".format(monitor.total, monitor.restored))
        return True
    return False


def test_restore_total_adds_once():
    """The total merges as restored + counted, once; unreadable totals leave it unresolved."""
    monitor = ControlMonitor()
    for unreadable in (None, "unavailable", "nan", "inf", "-3"):
        if monitor.restore([], unreadable) or monitor.restored:
            print("ERROR: restore resolved on unreadable total {!r}".format(unreadable))
            return True
    monitor.total = 2  # failures counted by this process before the restore resolved
    if not monitor.restore([], "5") or monitor.total != 7:
        print("ERROR: expected restored + counted = 7, got {}".format(monitor.total))
        return True
    monitor.restore([], "5")
    if monitor.total != 7:
        print("ERROR: a second restore added the total again: {}".format(monitor.total))
        return True
    return False


def test_restart_mid_slot_does_not_recount():
    """After a restart mid-slot (plan now on the next contiguous window) the slot is not re-counted."""
    first = charging_monitor()
    feed(first, [240, GRACE_S, GRACE_S + 60, GRACE_S + 120], power=0)
    stored = json.loads(json.dumps(first.newest_events(20)))  # what HA hands back

    second = ControlMonitor()
    second.restore(stored, "1")
    second.set_expectation(0, MODE_CHARGING, SLOT_START + 1800, SLOT_END + 1800, 80.0, T0 + 2000)
    if feed(second, [2300, 2360, 2420, 2480], power=0) or second.total != 1:
        print("ERROR: restart re-counted the slot, total {}".format(second.total))
        return True
    if not second.events[0]["failed_minutes"] > stored[0]["failed_minutes"]:
        print("ERROR: restored event did not keep accruing minutes")
        return True
    return False


def test_format_failure():
    """The Warn: line names the inverter, mode, slot and the power shortfall."""
    failed = False
    cases = [
        (make_event(T0), "Warn: Control failure: inverter 0 Charging 00:00-01:00 - battery idle vs 3000W charge requested"),
        (dict(make_event(T0), measured_w=-120), "Warn: Control failure: inverter 0 Charging 00:00-01:00 - battery charging at 120W vs 3000W charge requested"),
        (dict(make_event(T0, mode=MODE_EXPORTING), requested_w=2000, measured_w=300), "Warn: Control failure: inverter 0 Exporting 00:00-01:00 - battery discharging at 300W vs 2000W discharge requested"),
        (dict(make_event(T0, mode=MODE_FREEZE_CHARGING), requested_w=None, measured_w=450), "Warn: Control failure: inverter 0 Freeze charging 00:00-01:00 - battery discharging at 450W, Freeze charging allows at most 200W discharge"),
        (dict(make_event(T0, mode=MODE_FREEZE_EXPORTING, inverter_id=1), requested_w=None, measured_w=-450), "Warn: Control failure: inverter 1 Freeze exporting 00:00-01:00 - battery charging at 450W, Freeze exporting allows at most 200W charge"),
    ]
    for event, expected in cases:
        got = format_failure(event, pytz.utc)
        if got != expected:
            print("ERROR: format_failure gave\n  {}\nexpected\n  {}".format(got, expected))
            failed = True
    return failed
```

Add to the list in `run_control_monitor_tests`:

```python
        ("recent_and_prune", test_recent_and_prune),
        ("newest_and_by_mode", test_newest_events_and_by_mode),
        ("restore_merges_dedupes", test_restore_merges_and_dedupes),
        ("restore_total_adds_once", test_restore_total_adds_once),
        ("restart_mid_slot", test_restart_mid_slot_does_not_recount),
        ("format_failure", test_format_failure),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test control_monitor > /tmp/control_monitor.txt 2>&1; grep -n "ERROR\|Traceback\|ImportError" /tmp/control_monitor.txt | head`
Expected: `ImportError: cannot import name 'format_failure'`.

- [ ] **Step 3: Write the implementation**

In `apps/predbat/control_monitor.py`, add below `event_key`:

```python
def _is_number(value):
    """True for an int or float that is not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_valid_event(event):
    """True when a restored event has every field the monitor relies on, with sane types."""
    if not isinstance(event, dict):
        return False
    inverter_id = event.get("inverter")
    if not isinstance(inverter_id, int) or isinstance(inverter_id, bool):
        return False
    if event.get("mode") not in JUDGED_MODES:
        return False
    return all(_is_number(event.get(field)) for field in ("slot_start", "slot_end", "at", "failed_minutes"))
```

Add below `judge_reading`:

```python
def format_failure(event, tz=None):
    """Return the one-line Warn: log message for a newly confirmed failure."""

    def clock(timestamp):
        """Format an epoch timestamp as HH:MM in tz."""
        return datetime.fromtimestamp(timestamp, tz).strftime("%H:%M")

    measured = int(event.get("measured_w") or 0)
    if measured < 0:
        power_text = "battery charging at {}W".format(-measured)
    elif measured > 0:
        power_text = "battery discharging at {}W".format(measured)
    else:
        power_text = "battery idle"
    mode = event["mode"]
    if mode == MODE_CHARGING:
        detail = "{} vs {}W charge requested".format(power_text, int(event["requested_w"]))
    elif mode == MODE_EXPORTING:
        detail = "{} vs {}W discharge requested".format(power_text, int(event["requested_w"]))
    elif mode == MODE_FREEZE_CHARGING:
        detail = "{}, {} allows at most {}W discharge".format(power_text, mode, FLOOR_W)
    else:
        detail = "{}, {} allows at most {}W charge".format(power_text, mode, FLOOR_W)
    return "Warn: Control failure: inverter {} {} {}-{} - {}".format(event["inverter"], mode, clock(event["slot_start"]), clock(event["slot_end"]), detail)
```

Add these methods to `ControlMonitor`, after `observe`:

```python
    def recent_events(self, now, window_s=DEFAULT_WINDOW_S):
        """Return the events confirmed within the last window_s seconds, oldest first."""
        return [event for event in self.events if event["at"] >= now - window_s]

    def newest_events(self, limit):
        """Return the newest limit events, oldest first - the list published as the durable store."""
        return self.events[-limit:] if limit > 0 else []

    def prune(self, now, window_s=DEFAULT_WINDOW_S):
        """Drop events older than the window, except one still attached to a live slot."""
        live = [track.get("event") for track in self.tracking.values() if track.get("event") is not None]
        self.events = [event for event in self.events if event["at"] >= now - window_s or any(event is owned for owned in live)]

    @staticmethod
    def by_mode(events):
        """Count events per mode."""
        counts = {}
        for event in events:
            counts[event["mode"]] = counts.get(event["mode"], 0) + 1
        return counts

    def restore(self, events, total):
        """
        Merge the events and lifetime total read back from the sensors after a restart.

        Events are merged on every call, de-duplicated by event_key and keeping the larger
        failed_minutes. The total is merged once, the first time it reads as a number, as
        restored + total: until then the in-memory total holds only failures counted by this
        process, and taking the max would drop them.

        Returns:
            True once the total has been restored, now or on an earlier call.
        """
        if isinstance(events, list):
            for item in events:
                if not is_valid_event(item):
                    continue
                existing = next((event for event in self.events if event_key(event) == event_key(item)), None)
                if existing is None:
                    self.events.append(dict(item))
                else:
                    existing["failed_minutes"] = max(existing["failed_minutes"], item["failed_minutes"])
            self.events.sort(key=lambda event: event["at"])
        if self.restored:
            return True
        try:
            restored_total = int(float(total))
        except (TypeError, ValueError, OverflowError):
            return False
        if restored_total < 0:
            return False
        self.total += restored_total
        self.restored = True
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test control_monitor > /tmp/control_monitor.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/control_monitor.txt | head -20`
Expected: no `ERROR`/`Traceback`; passed.

- [ ] **Step 5: Pre-commit and commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/control_monitor.py apps/predbat/tests/test_control_monitor.py > /tmp/pc2.txt 2>&1; grep -v "Passed\|Skipped" /tmp/pc2.txt
git add apps/predbat/control_monitor.py apps/predbat/tests/test_control_monitor.py
git commit -m "feat(control-monitor): 24h window, restore merge and the warning line

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Wire the monitor into execute

**Files:**
- Modify: `apps/predbat/predbat.py` (import near line 111; `reset()` near line 347)
- Modify: `apps/predbat/execute.py` (imports lines 18–23; `apply_inverter_rates` ~260–296; `execute_plan` ~297–1032; `fetch_inverter_data` end ~1356; two new methods)
- Modify: `apps/predbat/tests/test_execute.py` (new tests + calls at the end of `run_execute_tests`)

**Interfaces:**
- Consumes: Tasks 1–2 (`ControlMonitor`, mode constants, `format_failure`, `GRACE_S`).
- Produces:
  - `PredBat.control_monitor: ControlMonitor` (created in `reset()`)
  - `inverter.requested_charge_w` / `inverter.requested_discharge_w`: `int | None`, set by `apply_inverter_rates`
  - `Execute.record_control_expectations(status_per_inverter, monitor_slots, now=None) -> None`
  - `Execute.observe_control_failures(now=None) -> None`

- [ ] **Step 1: Run impact analysis**

Run GitNexus `impact` (upstream) on `execute_plan`, `fetch_inverter_data`, `apply_inverter_rates` and `reset` (file `apps/predbat/predbat.py`). Expected: `execute_plan` and `fetch_inverter_data` HIGH, `apply_inverter_rates` LOW. If anything has escalated to CRITICAL, stop and report.

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_execute.py`, add to the imports:

```python
from control_monitor import ControlMonitor, MODE_CHARGING, MODE_EXPORTING, GRACE_S
```

Add these functions above `run_execute_tests`:

```python
def control_monitor_charging_scenario(my_predbat, name):
    """
    Put the two-inverter test fleet into Charging through the real execute path.

    Same arguments as the charge_iboost_no_hold_text scenario, which is known to resolve to Charging
    on this fleet. soc_kw defaults to 0, so both inverters are below target.
    """
    return run_execute_test(
        my_predbat,
        name,
        charge_window_best=[{"start": 12 * 60, "end": 13 * 60, "average": 1}],
        charge_limit_best=[10, 10],
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=13 * 60,
    )


def test_control_monitor_charging_counts_once(my_predbat):
    """
    Charging with no battery power: each inverter fails once, with one Warn: line each, and a later
    reading in the same slot neither warns nor counts again.
    """
    saved_monitor = my_predbat.control_monitor
    saved_log = my_predbat.log
    my_predbat.control_monitor = ControlMonitor()
    lines = []
    failed = False
    try:
        if control_monitor_charging_scenario(my_predbat, "control_monitor_charging"):
            return True
        monitor = my_predbat.control_monitor
        for inverter in my_predbat.inverters:
            track = monitor.tracking.get(inverter.id)
            if track is None or track["mode"] != MODE_CHARGING:
                print("ERROR: inverter {} has no Charging expectation: {}".format(inverter.id, track))
                failed = True
            if inverter.requested_charge_w != 1000:
                print("ERROR: inverter {} requested_charge_w {} expected 1000".format(inverter.id, inverter.requested_charge_w))
                failed = True
        if failed:
            return failed
        entered = monitor.tracking[0]["entered_at"]
        for inverter in my_predbat.inverters:
            inverter.battery_power = 0
        my_predbat.log = lambda message, *args, **kwargs: lines.append(message)
        for offset in (GRACE_S, GRACE_S + 60, GRACE_S + 120, GRACE_S + 180):
            my_predbat.observe_control_failures(now=entered + offset)
        warns = [line for line in lines if line.startswith("Warn: Control failure")]
        if len(warns) != 2 or monitor.total != 2:
            print("ERROR: expected one warning and one count per inverter, got {} warnings, total {}".format(len(warns), monitor.total))
            failed = True
    finally:
        my_predbat.log = saved_log
        my_predbat.control_monitor = saved_monitor
        for inverter in my_predbat.inverters:
            inverter.battery_power = 0
    return failed


def test_control_monitor_exporting_expectation(my_predbat):
    """Exporting sets an Exporting expectation, with the discharge rate written recorded on the inverter."""
    saved_monitor = my_predbat.control_monitor
    my_predbat.control_monitor = ControlMonitor()
    failed = False
    try:
        if run_execute_test(
            my_predbat,
            "control_monitor_exporting",
            export_window_best=[{"start": 12 * 60, "end": 13 * 60, "average": 1}],
            export_limits_best=[pack_export_limit(EXPORT_MODE_TARGET, 0)],
            assert_force_export=True,
            set_charge_window=True,
            set_export_window=True,
            soc_kw=10,
            assert_status="Exporting",
            assert_immediate_soc_target=0,
            assert_discharge_start_time_minutes=12 * 60,
            assert_discharge_end_time_minutes=13 * 60 + 1,
        ):
            return True
        for inverter in my_predbat.inverters:
            track = my_predbat.control_monitor.tracking.get(inverter.id)
            if track is None or track["mode"] != MODE_EXPORTING:
                print("ERROR: inverter {} has no Exporting expectation: {}".format(inverter.id, track))
                failed = True
            if inverter.requested_discharge_w != 1000:
                print("ERROR: inverter {} requested_discharge_w {} expected 1000".format(inverter.id, inverter.requested_discharge_w))
                failed = True
    finally:
        my_predbat.control_monitor = saved_monitor
    return failed


def test_control_monitor_read_only_records_nothing(my_predbat):
    """A read-only plan run clears every expectation, so nothing can be counted."""
    saved_monitor = my_predbat.control_monitor
    saved_read_only = my_predbat.set_read_only
    my_predbat.control_monitor = ControlMonitor()
    failed = False
    try:
        my_predbat.control_monitor.set_expectation(0, MODE_CHARGING, 0, 3600, 80.0, 0)
        failed = run_execute_test(
            my_predbat,
            "control_monitor_read_only",
            charge_window_best=[{"start": 12 * 60, "end": 13 * 60, "average": 1}],
            charge_limit_best=[10, 10],
            set_charge_window=True,
            set_export_window=True,
            read_only=True,
            assert_status="Read-Only",
            reserve=0,
            assert_immediate_charge_soc_target_array=[-1, -1],
            assert_immediate_discharge_soc_target_array=[-1, -1],
        )
        if my_predbat.control_monitor.tracking:
            print("ERROR: read-only plan run left expectations {}".format(my_predbat.control_monitor.tracking))
            failed = True
    finally:
        my_predbat.control_monitor = saved_monitor
        my_predbat.set_read_only = saved_read_only
    return failed


def test_control_monitor_read_only_between_plan_runs(my_predbat):
    """Read-only switched on after the plan ran: the next poll clears the expectations and counts nothing."""
    saved_monitor = my_predbat.control_monitor
    saved_read_only = my_predbat.set_read_only
    my_predbat.control_monitor = ControlMonitor()
    failed = False
    try:
        if control_monitor_charging_scenario(my_predbat, "control_monitor_read_only_poll"):
            return True
        monitor = my_predbat.control_monitor
        entered = monitor.tracking[0]["entered_at"]
        for inverter in my_predbat.inverters:
            inverter.battery_power = 0
        my_predbat.set_read_only = True
        for offset in (GRACE_S, GRACE_S + 60, GRACE_S + 120):
            my_predbat.observe_control_failures(now=entered + offset)
        if monitor.total != 0 or monitor.tracking:
            print("ERROR: counted in read-only mode: total {} tracking {}".format(monitor.total, monitor.tracking))
            failed = True
    finally:
        my_predbat.set_read_only = saved_read_only
        my_predbat.control_monitor = saved_monitor
    return failed


def test_control_monitor_calibration_records_nothing(my_predbat):
    """Calibration on any inverter clears every expectation."""
    failed = balance_fixture(my_predbat, "control_monitor_calibration_setup")
    if failed:
        return failed
    saved_monitor = my_predbat.control_monitor
    saved_calibration = [inverter.in_calibration for inverter in my_predbat.inverters]
    my_predbat.control_monitor = ControlMonitor()
    try:
        my_predbat.control_monitor.set_expectation(0, MODE_CHARGING, 0, 3600, 80.0, 0)
        my_predbat.inverters[1].in_calibration = True
        my_predbat.execute_plan()
        if my_predbat.control_monitor.tracking:
            print("ERROR: calibration left expectations {}".format(my_predbat.control_monitor.tracking))
            failed = True
    finally:
        my_predbat.control_monitor = saved_monitor
        for inverter, calibration in zip(my_predbat.inverters, saved_calibration):
            inverter.in_calibration = calibration
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
    return failed


def test_control_monitor_uses_written_rate(my_predbat):
    """
    The rate judged is the one apply_inverter_rates actually wrote: a balancer hold at 0W is not
    judged, an unclaimed rate resolves to max when rates are reset, and to None when they are not.
    """
    failed = False
    inverter = my_predbat.inverters[0]
    try:
        my_predbat.apply_inverter_rates(inverter, {"charge_rate": 0, "discharge_rate": None, "reset_rates": True})
        if inverter.requested_charge_w != 0 or inverter.requested_discharge_w != int(inverter.battery_rate_max_discharge * MINUTE_WATT):
            print("ERROR: written rates not recorded: charge {} discharge {}".format(inverter.requested_charge_w, inverter.requested_discharge_w))
            failed = True
        monitor = ControlMonitor()
        monitor.set_expectation(0, MODE_CHARGING, 0, 3600, 80.0, 0)
        for offset in (GRACE_S, GRACE_S + 60, GRACE_S + 120):
            monitor.observe(0, 0, 10.0, inverter.requested_charge_w, inverter.requested_discharge_w, offset)
        if monitor.total != 0:
            print("ERROR: a balancer hold at 0W was judged as a failure")
            failed = True
        my_predbat.apply_inverter_rates(inverter, {"charge_rate": None, "discharge_rate": None, "reset_rates": False})
        if inverter.requested_charge_w is not None or inverter.requested_discharge_w is not None:
            print("ERROR: an unwritten rate should record None, got {} / {}".format(inverter.requested_charge_w, inverter.requested_discharge_w))
            failed = True
    finally:
        my_predbat.apply_inverter_rates(inverter, {"charge_rate": None, "discharge_rate": None, "reset_rates": True})
    return failed
```

At the end of `run_execute_tests`, immediately before the final `return failed`, add:

```python
    for control_monitor_test in (
        test_control_monitor_charging_counts_once,
        test_control_monitor_exporting_expectation,
        test_control_monitor_read_only_records_nothing,
        test_control_monitor_read_only_between_plan_runs,
        test_control_monitor_calibration_records_nothing,
        test_control_monitor_uses_written_rate,
    ):
        failed |= control_monitor_test(my_predbat)
        if failed:
            return failed
```

(`pack_export_limit`, `EXPORT_MODE_TARGET` and `MINUTE_WATT` are already imported in `test_execute.py`; confirm with `grep -n "^from\|^import" apps/predbat/tests/test_execute.py` and add any that are missing.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test execute > /tmp/execute.txt 2>&1; grep -n "ERROR\|Traceback\|AttributeError" /tmp/execute.txt | head`
Expected: `AttributeError` on `control_monitor` (not yet created in `reset()`).

- [ ] **Step 4: Create the monitor in PredBat**

In `apps/predbat/predbat.py`, add beside `from control_ledger import ControlLedger`:

```python
from control_monitor import ControlMonitor
```

In `reset()`, directly after `self.control_ledger_restored = False`:

```python
        self.control_monitor = ControlMonitor()
```

- [ ] **Step 5: Record the written rate in `apply_inverter_rates`**

In `apps/predbat/execute.py`, at the end of `apply_inverter_rates` (after the discharge-rate `if/else`), add:

```python
        # The rate this inverter was actually asked for, after balancing - what the control-failure
        # monitor judges measured power against. None when nothing was written (Monitor mode).
        if charge_rate is not None:
            inverter.requested_charge_w = int(charge_rate)
        else:
            inverter.requested_charge_w = int(inverter.battery_rate_max_charge * MINUTE_WATT) if reset_rates else None
        if discharge_rate is not None:
            inverter.requested_discharge_w = int(discharge_rate)
        else:
            inverter.requested_discharge_w = int(inverter.battery_rate_max_discharge * MINUTE_WATT) if reset_rates else None
```

- [ ] **Step 6: Collect each inverter's slot in `execute_plan`**

Add to the imports at the top of `execute.py`:

```python
from control_monitor import MODE_CHARGING, MODE_EXPORTING, MODE_FREEZE_CHARGING, MODE_FREEZE_EXPORTING, format_failure
```

Directly after `export_rate_alloc = self.allocate_fleet_export_rates()` (before the `for inverter in self.inverters:` loop):

```python
        # Inverter id -> (mode, slot start minutes, slot end minutes, target percent), for the
        # control-failure monitor. Only the four judged modes are recorded.
        monitor_slots = {}
```

In the charge branch, directly after the "Combine contiguous windows" `for windows in self.charge_window_best:` loop:

```python
                # The slot as planned, before the register adjustments below move the start/end
                charge_slot = (window["start"], minutes_end)
```

After `status_per_inverter[inverter.id] = status` in the **Freeze charging** block (currently line ~520):

```python
                            monitor_slots[inverter.id] = (MODE_FREEZE_CHARGING, charge_slot[0], charge_slot[1], None)
```

After `status_per_inverter[inverter.id] = status` in the **Charging** `else:` block (currently line ~563):

```python
                                monitor_slots[inverter.id] = (MODE_CHARGING, charge_slot[0], charge_slot[1], inv_target_soc_percent)
```

After `status_per_inverter[inverter.id] = status` in the **Exporting** block (currently line ~708):

```python
                        monitor_slots[inverter.id] = (MODE_EXPORTING, window["start"], window["end"], calc_percent_limit(discharge_soc, self.soc_max))
```

After `status_per_inverter[inverter.id] = status` in the **Freeze exporting** block (currently line ~727):

```python
                            monitor_slots[inverter.id] = (MODE_FREEZE_EXPORTING, window["start"], window["end"], None)
```

Directly after `self.apply_rate_intent(intent)` (the single apply pass after the loop):

```python
        self.record_control_expectations(status_per_inverter, monitor_slots)
```

- [ ] **Step 7: Add the two monitor methods and the observe call**

Add to the `Execute` class (after `apply_inverter_rates`):

```python
    def record_control_expectations(self, status_per_inverter, monitor_slots, now=None):
        """
        Tell the control-failure monitor what each inverter should now be doing.

        Called at the end of execute_plan() once every inverter's status is final. An inverter is
        judged only while its final status is the mode its slot was recorded for; in read-only mode
        or while any inverter is calibrating nothing is judged at all.

        Args:
            status_per_inverter (dict): inverter id -> final core status from execute_plan()
            monitor_slots (dict): inverter id -> (mode, slot start minutes, slot end minutes, target percent)
            now (float): epoch seconds, defaults to time.time()
        """
        # getattr: partial Execute stubs in the tests never create a monitor
        monitor = getattr(self, "control_monitor", None)
        if monitor is None:
            return
        if now is None:
            now = time.time()
        if self.set_read_only or any(inverter.in_calibration for inverter in self.inverters):
            monitor.clear_expectation()
            return
        for inverter in self.inverters:
            slot = monitor_slots.get(inverter.id)
            if slot is None or slot[0] != status_per_inverter.get(inverter.id):
                monitor.clear_expectation(inverter.id)
                continue
            mode, start_minutes, end_minutes, target_percent = slot
            slot_start = (self.midnight_utc + timedelta(minutes=start_minutes)).timestamp()
            slot_end = (self.midnight_utc + timedelta(minutes=end_minutes)).timestamp()
            monitor.set_expectation(inverter.id, mode, slot_start, slot_end, target_percent, now)

    def observe_control_failures(self, now=None):
        """
        Judge each inverter's fresh reading against what it was last told to do.

        Called at the end of fetch_inverter_data(), quick or full. Read-only mode or calibration that
        started since the last plan run clears every expectation here, so nothing is counted in the
        minutes before execute_plan() next runs.

        Args:
            now (float): epoch seconds, defaults to time.time()
        """
        monitor = getattr(self, "control_monitor", None)
        if monitor is None or not monitor.tracking or not self.inverters:
            return
        if now is None:
            now = time.time()
        if self.set_read_only or any(inverter.in_calibration for inverter in self.inverters):
            monitor.clear_expectation()
            return
        for inverter in self.inverters:
            event = monitor.observe(
                inverter.id,
                inverter.battery_power,
                inverter.soc_percent,
                getattr(inverter, "requested_charge_w", None),
                getattr(inverter, "requested_discharge_w", None),
                now,
            )
            if event is not None:
                self.log(format_failure(event, self.local_tz))
```

At the end of `fetch_inverter_data`, directly before the final `return True`:

```python
        self.observe_control_failures()
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test execute --test control_monitor > /tmp/execute.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/execute.txt | head -20`
Expected: no `ERROR`/`Traceback`; both pass. If a `control_monitor_*` scenario's *own* assertions fail (status or window times), compare its arguments against the `charge_iboost_no_hold_text` / `discharge` scenarios in the same file and match them — do not loosen the monitor assertions.

- [ ] **Step 9: Pre-commit and commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/execute.py apps/predbat/predbat.py apps/predbat/tests/test_execute.py > /tmp/pc3.txt 2>&1; grep -v "Passed\|Skipped" /tmp/pc3.txt
git add apps/predbat/execute.py apps/predbat/predbat.py apps/predbat/tests/test_execute.py
git commit -m "feat(control-monitor): set expectations from execute_plan, observe every inverter read

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Publish the sensors with the restart guard

**Files:**
- Modify: `apps/predbat/predbat.py` (new method `publish_control_failures`; call in `update_pred` after the control-conflicts block ~line 1279)
- Modify: `apps/predbat/tests/test_control_monitor.py`

**Interfaces:**
- Consumes: `PredBat.control_monitor`, `ControlMonitor.restore/prune/recent_events/newest_events/by_mode/total/restored`.
- Produces: `PredBat.publish_control_failures(now) -> None`; entities `predbat.control_failures`, `predbat.control_failures_total`.

- [ ] **Step 1: Run impact analysis**

GitNexus `impact` upstream on `update_pred` with `file_path: apps/predbat/predbat.py`. Expected LOW.

- [ ] **Step 2: Write the failing tests**

Add to `apps/predbat/tests/test_control_monitor.py` (above `run_control_monitor_tests`):

```python
class RestoreSource:
    """Stands in for load_previous_value_from_ha so a test decides exactly what the restore reads."""

    def __init__(self, my_predbat, events=None, total=None):
        """Wrap my_predbat's loader, answering for the two control-failure entities only."""
        self.my_predbat = my_predbat
        self.events = events
        self.total = total
        self.original = my_predbat.load_previous_value_from_ha
        my_predbat.load_previous_value_from_ha = self.load

    def load(self, entity, attribute=None):
        """Return the configured value for the control-failure entities, defer for anything else."""
        if entity == self.my_predbat.prefix + ".control_failures" and attribute == "events":
            return self.events
        if entity == self.my_predbat.prefix + ".control_failures_total" and attribute is None:
            return self.total
        return self.original(entity, attribute=attribute)

    def close(self):
        """Put the real loader back."""
        self.my_predbat.load_previous_value_from_ha = self.original


def publish_setup(my_predbat):
    """Give my_predbat a fresh monitor and forget any earlier publish of the two entities."""
    saved = my_predbat.control_monitor
    my_predbat.control_monitor = ControlMonitor()
    for suffix in (".control_failures", ".control_failures_total"):
        my_predbat.dashboard_values.pop(my_predbat.prefix + suffix, None)
    return saved


def published(my_predbat, suffix):
    """The state last published for an entity, or None if it was never written."""
    item = my_predbat.dashboard_values.get(my_predbat.prefix + suffix)
    return None if item is None else item["state"]


def test_publish_fresh_install(my_predbat):
    """Nothing to restore and nothing counted: the 24h sensor publishes 0, the total is not written."""
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat)
    failed = False
    try:
        my_predbat.publish_control_failures(T0)
        if published(my_predbat, ".control_failures") != 0:
            print("ERROR: 24h sensor not published as 0")
            failed = True
        if published(my_predbat, ".control_failures_total") is not None or my_predbat.control_monitor.restored:
            print("ERROR: a 0 total was written over a total that could not be read")
            failed = True
        # The first counted failure is written, and resolves the restore
        my_predbat.control_monitor.events.append(make_event(T0 + 60))
        my_predbat.control_monitor.total = 1
        my_predbat.publish_control_failures(T0 + 120)
        if published(my_predbat, ".control_failures_total") != 1 or not my_predbat.control_monitor.restored:
            print("ERROR: first failure on a fresh install did not publish the total")
            failed = True
        attributes = my_predbat.dashboard_values[my_predbat.prefix + ".control_failures"]["attributes"]
        if published(my_predbat, ".control_failures") != 1 or attributes["by_mode"] != {MODE_CHARGING: 1} or len(attributes["events"]) != 1:
            print("ERROR: 24h sensor attributes wrong: {}".format(attributes))
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed


def test_publish_restores_total_and_events(my_predbat):
    """A readable total and events are restored and re-published."""
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat, events=[make_event(T0 - 60)], total="7")
    failed = False
    try:
        my_predbat.publish_control_failures(T0)
        if published(my_predbat, ".control_failures_total") != 7 or published(my_predbat, ".control_failures") != 1:
            print("ERROR: restore not published: total {} 24h {}".format(published(my_predbat, ".control_failures_total"), published(my_predbat, ".control_failures")))
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed


def test_publish_late_restore_adds_counted(my_predbat):
    """Unreadable at first, readable later, with a failure counted in between: total = restored + counted."""
    saved = publish_setup(my_predbat)
    source = RestoreSource(my_predbat)
    failed = False
    try:
        my_predbat.publish_control_failures(T0)
        my_predbat.control_monitor.total = 1  # counted between the two publish cycles
        source.total = "5"
        my_predbat.publish_control_failures(T0 + 300)
        if published(my_predbat, ".control_failures_total") != 6:
            print("ERROR: late restore published {} expected 6".format(published(my_predbat, ".control_failures_total")))
            failed = True
    finally:
        source.close()
        my_predbat.control_monitor = saved
    return failed
```

Change `run_control_monitor_tests` so the publish tests, which need `my_predbat`, also run — append after the existing loop, before `return failed`:

```python
    for name, fn in [
        ("publish_fresh_install", test_publish_fresh_install),
        ("publish_restores", test_publish_restores_total_and_events),
        ("publish_late_restore", test_publish_late_restore_adds_counted),
    ]:
        print("**** control_monitor: {} ****".format(name))
        if fn(my_predbat):
            print("ERROR: control_monitor test {} failed".format(name))
            failed = True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test control_monitor > /tmp/control_monitor.txt 2>&1; grep -n "ERROR\|Traceback\|AttributeError" /tmp/control_monitor.txt | head`
Expected: `AttributeError: ... has no attribute 'publish_control_failures'`.

- [ ] **Step 4: Implement `publish_control_failures`**

In `apps/predbat/predbat.py`, add this method to `PredBat` directly after `_emit_snapshot_metrics`:

```python
    def publish_control_failures(self, now):
        """
        Publish the control-failure sensors, restoring their history first if not yet done.

        The two entities ARE the durable store: the events attribute and the total's state are what
        restore() reads back after a restart. The restore is retried every cycle until the total
        reads back as a number, and while it is unresolved a total of 0 is not written - publishing
        0 over a total that merely could not be read yet would register as a reset in HA long-term
        statistics.

        Args:
            now (float): epoch seconds
        """
        monitor = self.control_monitor
        if not monitor.restored:
            monitor.restore(
                self.load_previous_value_from_ha(self.prefix + ".control_failures", attribute="events"),
                self.load_previous_value_from_ha(self.prefix + ".control_failures_total"),
            )
        monitor.prune(now)
        recent = monitor.recent_events(now)
        by_mode = monitor.by_mode(recent)
        self.dashboard_item(
            self.prefix + ".control_failures",
            state=len(recent),
            attributes={
                "friendly_name": "Control failures (24h)",
                "state_class": "measurement",
                "unit_of_measurement": "failures",
                "icon": "mdi:battery-alert",
                "events": monitor.newest_events(20),
                "by_mode": by_mode,
            },
        )
        if monitor.restored or monitor.total > 0:
            self.dashboard_item(
                self.prefix + ".control_failures_total",
                state=monitor.total,
                attributes={
                    "friendly_name": "Control failures (total)",
                    "state_class": "total_increasing",
                    "unit_of_measurement": "failures",
                    "icon": "mdi:battery-alert",
                },
            )
            # Once written, the entity holds our own value, so there is nothing left to restore
            monitor.restored = True
        if recent:
            self.log("Control failures: {} slot(s) in the last 24h {}".format(len(recent), by_mode))
```

In `update_pred`, directly after the control-conflicts block's
`self.log("Control interference: ...")` `if` statement, add:

```python
        self.publish_control_failures(now_ts)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test control_monitor > /tmp/control_monitor.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/control_monitor.txt | head -20`
Expected: no `ERROR`/`Traceback`; passed.

- [ ] **Step 6: Pre-commit and commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/predbat.py apps/predbat/tests/test_control_monitor.py > /tmp/pc4.txt 2>&1; grep -v "Passed\|Skipped" /tmp/pc4.txt
git add apps/predbat/predbat.py apps/predbat/tests/test_control_monitor.py
git commit -m "feat(control-monitor): publish 24h and lifetime sensors, guard the total on restart

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Metrics, dashboard section and documentation

**Files:**
- Modify: `apps/predbat/predbat_metrics.py` (gauges after the control-ownership block ~line 142; `to_dict()` ~line 245)
- Modify: `apps/predbat/predbat.py` (`_emit_snapshot_metrics`, after the control-ledger lines ~796)
- Modify: `apps/predbat/web_metrics_dashboard.py` (section markup after Section 6 ~line 140; render function after `mdRenderControlConflicts`; call in `mdRenderAll`)
- Create: `apps/predbat/tests/test_metrics_dashboard_control_failures.py`
- Modify: `apps/predbat/unit_test.py` (import + 2 registry entries after the `control_conflicts_dashboard` entry ~line 690)
- Modify: `docs/output-data.md` (new section after `## Alert data`), `docs/web-interface.md` (Metrics View list)

**Interfaces:**
- Consumes: `PredBat.control_monitor`.
- Produces: `metrics().control_failures_24h`, `metrics().control_failures_total` (gauges), `metrics().control_failures_events` (list); `to_dict()` keys `control_failures_24h`, `control_failures_total`, `control_failures_events`; JS `mdRenderControlFailures(d)`, containers `mdFailureCards`, `mdFailureTable`.

- [ ] **Step 1: Run impact analysis**

GitNexus `impact` upstream on `_emit_snapshot_metrics`, `to_dict` (file `apps/predbat/predbat_metrics.py`) and `get_metrics_dashboard_body`. Expected: `_emit_snapshot_metrics` HIGH (main loop), the others LOW.

- [ ] **Step 2: Write the failing tests**

Create `apps/predbat/tests/test_metrics_dashboard_control_failures.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Test that the control failure monitor's data reaches the Metrics Dashboard: the counts and events
_emit_snapshot_metrics() writes must round-trip through to_dict(), and the dashboard body must
render them.
"""

import time

from control_monitor import ControlMonitor, MODE_CHARGING
from predbat_metrics import metrics
from web_metrics_dashboard import get_metrics_dashboard_body


def test_control_failures_metrics_round_trip(my_predbat):
    """
    _emit_snapshot_metrics() must publish the 24h count, the lifetime total and the event detail.

    Uses real wall-clock time: _emit_snapshot_metrics() calls time.time() internally.
    """
    print("**** test_control_failures_metrics_round_trip ****")
    saved = my_predbat.control_monitor
    failed = False
    try:
        monitor = ControlMonitor()
        now = time.time()
        monitor.events = [
            {"inverter": 0, "mode": MODE_CHARGING, "slot_start": now - 3600, "slot_end": now, "at": now - 3000, "requested_w": 3000, "measured_w": 0, "failed_minutes": 4.0},
            {"inverter": 1, "mode": MODE_CHARGING, "slot_start": now - 3600, "slot_end": now, "at": now - 2900, "requested_w": 3000, "measured_w": -100, "failed_minutes": 2.0},
        ]
        monitor.total = 9
        my_predbat.control_monitor = monitor

        my_predbat._emit_snapshot_metrics()
        data = metrics().to_dict()

        if data["control_failures_24h"] != 2:
            print("ERROR: control_failures_24h {} expected 2".format(data["control_failures_24h"]))
            failed = True
        if data["control_failures_total"] != 9:
            print("ERROR: control_failures_total {} expected 9".format(data["control_failures_total"]))
            failed = True
        if [event["inverter"] for event in data["control_failures_events"]] != [0, 1]:
            print("ERROR: events not published oldest-first: {}".format(data["control_failures_events"]))
            failed = True
    finally:
        my_predbat.control_monitor = saved
    return failed


def test_control_failures_dashboard_renders_section(my_predbat):
    """The dashboard body must contain a Control Failures section wired into the 30s refresh."""
    print("**** test_control_failures_dashboard_renders_section ****")
    body = get_metrics_dashboard_body("{}")
    failed = False
    for needle in ("Control Failures", "mdFailureCards", "mdFailureTable", "mdRenderControlFailures(d)", "d.control_failures_events", "d.control_failures_24h", "d.control_failures_total"):
        if needle not in body:
            print("ERROR: dashboard body is missing {}".format(needle))
            failed = True
    return failed
```

Register in `apps/predbat/unit_test.py` — import beside the control-conflicts dashboard import (~line 265):

```python
from tests.test_metrics_dashboard_control_failures import test_control_failures_metrics_round_trip, test_control_failures_dashboard_renders_section
```

and add after the `("control_conflicts_dashboard", ...)` registry entry (~line 690):

```python
        ("control_failures_metrics", test_control_failures_metrics_round_trip, "Metrics dashboard control_failures round-trip tests", False),
        ("control_failures_dashboard", test_control_failures_dashboard_renders_section, "Metrics dashboard control_failures section render tests", False),
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test control_failures_metrics --test control_failures_dashboard > /tmp/cf_metrics.txt 2>&1; grep -n "ERROR\|Traceback\|KeyError" /tmp/cf_metrics.txt | head`
Expected: `KeyError: 'control_failures_24h'` and `ERROR: dashboard body is missing ...` lines.

- [ ] **Step 4: Add the metrics**

In `apps/predbat/predbat_metrics.py`, after the control-ownership block (after `self.control_conflicts_sustained_controls = []`):

```python
        # -- Control failure monitor ----------------------------------------------
        self.control_failures_24h = _gauge("predbat_control_failures_24h", "Slots where the battery did not follow Predbat's command in the last 24h")
        self.control_failures_total = _gauge("predbat_control_failures_total", "Lifetime count of slots where the battery did not follow Predbat's command")
        # Plain data for the dashboard table, as for control_conflicts_events
        self.control_failures_events = []
```

In `to_dict()`, after the `"control_conflicts_sustained_controls"` entry:

```python
            # Control failure monitor
            "control_failures_24h": _val(self.control_failures_24h),
            "control_failures_total": _val(self.control_failures_total),
            "control_failures_events": self.control_failures_events,
```

In `apps/predbat/predbat.py` `_emit_snapshot_metrics`, after `m.control_conflicts_sustained_controls = sustained`:

```python
        # Control failure monitor
        m.control_failures_24h.set(len(self.control_monitor.recent_events(time.time())))
        m.control_failures_total.set(self.control_monitor.total)
        m.control_failures_events = self.control_monitor.newest_events(20)
```

- [ ] **Step 5: Add the dashboard section**

In `apps/predbat/web_metrics_dashboard.py`, after the Section 6 `</section>` (the Control Conflicts section) and before the closing `</div>`:

```html
<!-- Section 7: Control Failures -->
<section>
  <h2>Control Failures</h2>
  <div class="md-card-grid" id="mdFailureCards"></div>
  <div style="margin-top:1rem;" id="mdFailureTable"></div>
</section>
```

After the `mdRenderControlConflicts` function:

```javascript
function mdClock(ts) {
  if (!ts) return '—';
  var t = new Date(ts * 1000);
  return ('0' + t.getHours()).slice(-2) + ':' + ('0' + t.getMinutes()).slice(-2);
}

function mdRenderControlFailures(d) {
  var events = d.control_failures_events || [];
  var count = d.control_failures_24h || 0;
  var cards = [
    {label:'Failed Slots (24h)',  value: mdFmt(count, 0),                         cls: count > 0 ? 'md-status-warn' : 'md-status-ok'},
    {label:'Failed Slots (Total)', value: mdFmt(d.control_failures_total || 0, 0), cls: ''},
  ];
  var h = '';
  cards.forEach(function (c) {
    h += '<div class="md-card"><div class="md-label">' + c.label + '</div>'
      + '<div class="md-value ' + c.cls + '">' + c.value + '</div></div>';
  });
  document.getElementById('mdFailureCards').innerHTML = h;

  if (events.length === 0) {
    document.getElementById('mdFailureTable').innerHTML = '<div class="md-card" style="text-align:center;">No control failures in the last 24h</div>';
    return;
  }
  var t = '<table><thead><tr><th>When</th><th>Inverter</th><th>Mode</th><th>Slot</th><th>Requested</th><th>Measured</th><th>Failed Minutes</th></tr></thead><tbody>';
  /* Newest first for reading, even though the source list is oldest-first for storage. */
  events.slice().reverse().forEach(function (e) {
    t += '<tr><td>' + mdAgo(e.at) + '</td><td>' + e.inverter + '</td><td>' + (e.mode || '—') + '</td>'
      + '<td>' + mdClock(e.slot_start) + '–' + mdClock(e.slot_end) + '</td>'
      + '<td>' + (e.requested_w === null || e.requested_w === undefined ? '—' : mdFmt(e.requested_w, 0) + 'W') + '</td>'
      + '<td>' + mdFmt(e.measured_w, 0) + 'W</td><td>' + mdFmt(e.failed_minutes, 1) + '</td></tr>';
  });
  t += '</tbody></table>';
  document.getElementById('mdFailureTable').innerHTML = t;
}
```

In `mdRenderAll`, after `mdRenderControlConflicts(d);`:

```javascript
  mdRenderControlFailures(d);
```

- [ ] **Step 6: Document the feature**

In `docs/output-data.md`, add after the `## Alert data` section (before `## Predbat Logfile`):

```markdown
## Control failure data

Predbat checks that the battery actually does what it was commanded during each Charging, Exporting (force export), Freeze charging and Freeze exporting slot.
Five minutes after an inverter enters one of these modes, Predbat compares the measured battery power against the rate it asked that inverter for.
If the battery stays well short of it (below 25% of the requested rate) or, in a freeze mode, flows the wrong way by more than 200W, for three readings in a row, the slot is counted as a control failure.
Each failed slot logs one warning and is counted once, however long it lasts.
Nothing is checked in read-only mode or while an inverter is calibrating.

- **predbat.control_failures** - the number of failed slots in the last 24 hours. The `events` attribute lists recent failures (inverter, mode, slot times, requested and measured power, and how many minutes the battery was not following the command) and `by_mode` breaks the count down by mode.
- **predbat.control_failures_total** - the lifetime count of failed slots, suitable for Home Assistant long-term statistics.

Common causes are an inverter app or schedule overriding Predbat's mode, a battery management system limiting the charge or discharge rate, a command that never reached the inverter, or a cloud integration that reports battery power several minutes late.
The same data is shown in the Control Failures section of the [Metrics view](web-interface.md#metrics-view).
```

In `docs/web-interface.md`, change `contains the following 5 sections:` to `contains the following sections:` and add after the **API & Solar Status** bullet (after the "Solar status shows ..." line):

```markdown
- **Control Failures** - shows how many charge, export and freeze slots in the last 24 hours (and in total) the battery did not follow Predbat's command, with a table of recent failures - see [control failure data](output-data.md#control-failure-data)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test control_failures_metrics --test control_failures_dashboard --test control_conflicts_metrics --test control_conflicts_dashboard > /tmp/cf_metrics.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/cf_metrics.txt | head -20`
Expected: no `ERROR`/`Traceback`; all four pass.

- [ ] **Step 8: Pre-commit and commit**

```bash
git add apps/predbat/predbat_metrics.py apps/predbat/predbat.py apps/predbat/web_metrics_dashboard.py apps/predbat/tests/test_metrics_dashboard_control_failures.py apps/predbat/unit_test.py docs/output-data.md docs/web-interface.md
coverage/venv/bin/pre-commit run --files apps/predbat/predbat_metrics.py apps/predbat/predbat.py apps/predbat/web_metrics_dashboard.py apps/predbat/tests/test_metrics_dashboard_control_failures.py apps/predbat/unit_test.py docs/output-data.md docs/web-interface.md > /tmp/pc5.txt 2>&1; grep -v "Passed\|Skipped" /tmp/pc5.txt
git commit -m "feat(control-monitor): metrics, Control Failures dashboard section and docs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Whole-branch verification

**Files:** none new.

- [ ] **Step 1: Quick suite**

Run: `cd coverage && ./run_all --quick > /tmp/quick.txt 2>&1; grep -n "ERROR\|Traceback\|FAILED\|failed" /tmp/quick.txt | head -30`
Expected: no failures. Any failure in an unrelated test: re-run that test alone; if it passes alone, hunt the order dependency with `./run_shuffle --bisect` rather than ignoring it.

- [ ] **Step 2: Full pre-commit**

Run: `coverage/venv/bin/pre-commit run --all-files > /tmp/pc_all.txt 2>&1; grep -v "Passed\|Skipped" /tmp/pc_all.txt`
Expected: clean (re-stage `.cspell/custom-dictionary-workspace.txt` if it was re-sorted).

- [ ] **Step 3: Scope check**

Run GitNexus `detect_changes({scope: "compare", base_ref: "main"})`. Expected: changed symbols limited to `control_monitor.py`, `apply_inverter_rates`, `execute_plan`, `fetch_inverter_data`, the two new `Execute` methods, `reset`, `update_pred`, `_emit_snapshot_metrics`, `publish_control_failures`, `PrometheusMetrics.__init__`/`to_dict`, `get_metrics_dashboard_body`, and tests. Anything else is unintended — investigate before continuing.

- [ ] **Step 4: Debug-dump replay (no behaviour change to the plan)**

Run: `cd coverage && ./run_all --test debug_cases > /tmp/debug_cases.txt 2>&1; grep -n "ERROR\|Traceback\|failed" /tmp/debug_cases.txt | head`
Expected: passes — the monitor only observes, so golden plans are unchanged.
