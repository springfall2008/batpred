# Fold Balance Into Execute — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `execute_plan()` the only writer of inverter charge and discharge rates, with balancing expressed as a mutation of the executor's intent rather than a second subsystem overwriting it.

**Architecture:** `execute_plan()` builds a per-inverter intent dict instead of writing rates inline; a pure `balance_inverters()` in `utils.py` mutates that intent; a single `apply_inverter_rates()` pass writes it. The 60-second balance timer is deleted and the existing 120-second inverter poll becomes the second caller at 60 seconds. Per-inverter export rate allocation and a deadband reconciliation ride along.

**Tech Stack:** Python 3, no new dependencies. Tests via `coverage/run_all`, mock HA through `tests/test_infra.py`.

**Spec:** `docs/superpowers/specs/2026-09-18-balance-into-execute-design.md`

## Global Constraints

- Line length: 256 chars (Black), 250 chars (Flake8).
- Docstrings required on every function and class — `interrogate` enforces 100% coverage.
- British English spelling (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit — re-stage after running pre-commit.
- Variable naming: `lower_case_with_underscores`.
- Run `./run_pre_commit` from `coverage/` before every commit. **`git add` new files first** — pre-commit's `--all-files` covers git-tracked files only, so an unstaged new file is silently skipped.
- Tests are run from the `coverage/` directory. Always redirect test output to a file and grep the file afterwards; never pipe directly to grep.
- All work happens on branch `feat/balance-into-execute`, which already exists and contains the spec commit.
- Run `detect_changes()` before the final commit, per `CLAUDE.md`.

## Risk Note

`impact({target: "execute_plan", direction: "upstream"})` reports **HIGH** — 4 execution flows (`update_pred`, `run_time_loop`, `update_time_loop`, `initialize`), all `earliest_broken_step: 1`. Task 2 is the dangerous one. Its acceptance test is that the **entire existing execute suite passes with zero assertion edits**. Do not proceed past Task 2 until that holds.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/predbat/utils.py` | Pure helpers — already holds `find_charge_rate` | Add `balance_inverters()`, `allocate_export_rates()` |
| `apps/predbat/execute.py` | The executor | Build intent, apply once, call balance; delete old `balance_inverters()` |
| `apps/predbat/predbat.py` | Orchestration and timers | Delete `run_time_loop_balance` and its registration |
| `apps/predbat/const.py` | Constants | `INVERTER_QUICK_UPDATE_SECONDS` 120 → 60 |
| `apps/predbat/config.py` | `CONFIG_ITEMS`, `APPS_SCHEMA` | Defaults; remove `balance_inverters_seconds` |
| `apps/predbat/tests/test_execute.py` | Execute harness and scenarios | Per-inverter arrays; new scenarios |
| `apps/predbat/tests/test_balance_inverters.py` | Balance tests | Rework onto the pure function |
| `apps/predbat/tests/test_utils_allocate.py` | New — allocator unit tests | Create |
| `apps/predbat/unit_test.py` | Test registry | Register the new test module |

---

### Task 1: Per-inverter arrays in the execute test harness

Pure test-infrastructure work. No production code changes, so every existing test must still pass untouched. This is the F10 gap — `run_execute_test` already runs two inverters but takes capacity, rate ceiling and pause capability as scalars, so the execute path has never been tested on a mixed fleet.

**Files:**

- Modify: `apps/predbat/tests/test_execute.py:158-230` (signature), `:266-305` (setup), `:396-401` (assertions)
- Test: `apps/predbat/tests/test_execute.py` (new scenario at the end of `run_execute_tests`)

**Interfaces:**

- Produces: `run_execute_test(...)` gains keyword arguments `soc_max_array`, `battery_max_rate_array`, `battery_max_export_rate_array`, `has_timed_pause_array`, `assert_charge_rate_array`, `assert_discharge_rate_array`, each defaulting to `None` and each a list indexed by `inverter.id`. All later tasks use these.

- [ ] **Step 1: Add the six parameters to the signature**

In the `run_execute_test(` parameter list, after `assert_discharge_rate=None,` (line 198):

```python
    assert_charge_rate_array=None,
    assert_discharge_rate_array=None,
```

After `has_timed_pause=True,` (line 209):

```python
    has_timed_pause_array=None,
```

After `battery_max_export_rate=None,` (line 183):

```python
    battery_max_rate_array=None,
    battery_max_export_rate_array=None,
```

After `soc_max=10,` (line 167):

```python
    soc_max_array=None,
```

- [ ] **Step 2: Honour the arrays in the per-inverter setup loop**

Replace the body of the `for inverter in my_predbat.inverters:` loop (around line 278) so each scalar consults its array first. The fleet totals must be recomputed from the per-inverter values rather than assumed uniform, otherwise `my_predbat.soc_max` disagrees with the sum and the existing sanity checks at lines 320-328 fail.

Before the loop, replace the fleet-total block (lines 272-277):

```python
    total_inverters = len(my_predbat.inverters)
    if battery_max_rate_array:
        fleet_rate_w = sum(battery_max_rate_array)
    else:
        fleet_rate_w = battery_max_rate * total_inverters
    if battery_max_export_rate_array:
        fleet_export_w = sum(battery_max_export_rate_array)
    else:
        fleet_export_w = battery_max_export_rate * total_inverters
    my_predbat.battery_rate_max_charge = fleet_rate_w / 1000.0 / 60.0
    my_predbat.battery_rate_max_charge_dc = fleet_rate_w / 1000.0 / 60.0
    my_predbat.battery_rate_max_discharge = fleet_rate_w / 1000.0 / 60.0
    my_predbat.battery_rate_max_export = fleet_export_w / 1000.0 / 60.0
    my_predbat.set_reserve_enable = set_reserve_enable
```

Inside the loop, replace the five per-inverter assignments:

```python
        inverter.soc_max = soc_max_array[inverter.id] if soc_max_array else soc_max / total_inverters
        inverter.soc_percent = calc_percent_limit(inverter.soc_kw, inverter.soc_max)
        inverter.in_calibration = in_calibration_array[inverter.id] if in_calibration_array else in_calibration
        inv_rate_w = battery_max_rate_array[inverter.id] if battery_max_rate_array else battery_max_rate
        inv_export_w = battery_max_export_rate_array[inverter.id] if battery_max_export_rate_array else battery_max_export_rate
        inverter.battery_rate_max_charge = inv_rate_w / 1000.0 / 60.0
        inverter.battery_rate_max_charge_dc = inv_rate_w / 1000.0 / 60.0
        inverter.battery_rate_max_discharge = inv_rate_w / 1000.0 / 60.0
        inverter.battery_rate_max_export = inv_export_w / 1000.0 / 60.0
        inverter.inv_has_timed_pause = has_timed_pause_array[inverter.id] if has_timed_pause_array else has_timed_pause
```

The `reserve_kwh = reserve / total_inverters` line below is unchanged.

- [ ] **Step 3: Honour the assertion arrays**

Replace lines 396-401:

```python
        expect_charge_rate = assert_charge_rate_array[inverter.id] if assert_charge_rate_array else assert_charge_rate
        expect_discharge_rate = assert_discharge_rate_array[inverter.id] if assert_discharge_rate_array else assert_discharge_rate
        if expect_charge_rate != inverter.charge_rate:
            print("ERROR: Inverter {} Charge rate should be {} got {}".format(inverter.id, expect_charge_rate, inverter.charge_rate))
            failed = True
        if expect_discharge_rate != inverter.discharge_rate:
            print("ERROR: Inverter {} Discharge rate should be {} got {}".format(inverter.id, expect_discharge_rate, inverter.discharge_rate))
            failed = True
```

- [ ] **Step 4: Run the whole execute suite to confirm zero regressions**

```bash
cd coverage && ./run_all --test execute > /tmp/t1.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t1.log
```

Expected: `exit=0`, and `grep -c ERROR` prints `0`. The arrays all default to `None`, so behaviour is identical.

- [ ] **Step 5: Add a heterogeneous scenario proving the arrays work**

At the end of `run_execute_tests`, before its `return failed`:

```python
    # Mixed fleet: 9.5kWh/2600W and 5.2kWh/2600W, one with timed pause and one without.
    # Nothing planned, so both reset to max - this asserts the arrays are wired, not any new behaviour.
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_idle",
        soc_kw=7.35,
        soc_kw_array=[4.75, 2.6],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        battery_max_rate_array=[2600, 2600],
        has_timed_pause_array=[True, False],
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_rate_array=[2600, 2600],
        assert_discharge_rate_array=[2600, 2600],
    )
    if failed:
        return failed
```

- [ ] **Step 6: Run it**

```bash
cd coverage && ./run_all --test execute > /tmp/t1b.log 2>&1; echo "exit=$?"
grep -n "mixed_fleet_idle" /tmp/t1b.log
```

Expected: `exit=0`, the scenario name appears, no `ERROR` lines follow it.

- [ ] **Step 7: Commit**

```bash
cd coverage && ./run_pre_commit > /tmp/pc1.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/tests/test_execute.py
git commit -m "test(execute): per-inverter arrays in run_execute_test harness

Adds soc_max_array, battery_max_rate_array, battery_max_export_rate_array,
has_timed_pause_array and the two rate assertion arrays, so the execute path
can be tested on a heterogeneous fleet. The harness already ran two inverters
but took capacity, rate ceiling and pause capability as scalars, so every
multi-inverter execute scenario used identical units.

Refs #5141"
```

---

### Task 2: Intent and a single apply pass

The dangerous task. Strictly behaviour-preserving: **no assertion in the existing execute suite may change.**

**Files:**

- Modify: `apps/predbat/execute.py:125-640` (the `execute_plan` loop)

**Interfaces:**

- Produces: `Execute.apply_inverter_rates(self, inverter, intent)` — writes one inverter's rates. `intent` is a dict with keys `charge_rate`, `discharge_rate`, `pause_charge`, `pause_discharge`, `owner`. A `charge_rate` or `discharge_rate` of `None` means "reset to that inverter's maximum". Later tasks consume the intent dict built in `execute_plan`.

- [ ] **Step 1: Write the failing test**

Add to the end of `run_execute_tests` in `apps/predbat/tests/test_execute.py`:

```python
    # A freeze-charge hold on the inverter without timed pause must survive as rate 0 while the
    # inverter with timed pause holds via pause mode. This is the F5 / #829 shape: with two
    # writers the second one used to reset the zero back to max. Regression guard for the intent
    # refactor - it must hold before and after.
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 5.0}]
    charge_limit_best = [my_predbat.reserve]
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_freeze_hold",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        soc_kw=7.35,
        soc_kw_array=[4.75, 2.6],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        has_timed_pause_array=[True, False],
        set_charge_window=True,
        set_export_window=True,
        set_discharge_during_charge=False,
        assert_status="Freeze charging",
        assert_pause_discharge_array=[True, False],
        assert_discharge_rate_array=[2600, 0],
        assert_charge_rate_array=[2600, 2600],
    )
    if failed:
        return failed
```

- [ ] **Step 2: Run it against unmodified production code**

```bash
cd coverage && ./run_all --test execute > /tmp/t2a.log 2>&1; echo "exit=$?"
grep -A4 "mixed_fleet_freeze_hold" /tmp/t2a.log
```

Expected: PASS. This test characterises existing behaviour, so it must pass *before* the refactor — that is what makes it a regression guard. If it fails, the expected values above are wrong for this codebase: read the actual values from the log, correct the assertions, and re-run until it passes. Do not change production code to make it pass.

- [ ] **Step 3: Add `apply_inverter_rates`**

In `apps/predbat/execute.py`, as a new method on the same class as `execute_plan`, placed immediately before `def execute_plan(self):`:

```python
    def apply_inverter_rates(self, inverter, intent):
        """
        Write one inverter's charge and discharge rates from its intent.

        The single point at which rates reach the hardware. A rate of None means the executor
        claimed nothing, so the inverter returns to its maximum - which is what the
        resetCharge / resetDischarge flags used to express at the end of the per-inverter loop.

        Args:
            inverter: the Inverter to write to
            intent (dict): keys charge_rate, discharge_rate (W or None), and owner (str)
        """
        charge_rate = intent.get("charge_rate", None)
        discharge_rate = intent.get("discharge_rate", None)
        if charge_rate is None:
            charge_rate = inverter.battery_rate_max_charge * MINUTE_WATT
        if discharge_rate is None:
            discharge_rate = inverter.battery_rate_max_discharge * MINUTE_WATT
        inverter.adjust_charge_rate(int(charge_rate))
        inverter.adjust_discharge_rate(int(discharge_rate))
```

- [ ] **Step 4: Convert the loop to build intent**

In `execute_plan`, immediately before `for inverter in self.inverters:` (line 168), add:

```python
        intent = {}
```

Replace the four reset flags (lines 196-199) with:

```python
            charge_rate = None
            discharge_rate = None
            rate_owner = "demand"
            resetPause = self.set_charge_window or self.set_export_window
            resetReserve = self.set_charge_window or self.set_export_window
```

`resetPause` and `resetReserve` stay exactly as they are — only the two rate flags go.

Now convert each of the 15 rate call sites. Each is a two-line edit: replace the `inverter.adjust_*_rate(x)` call with an assignment, and delete the `resetCharge = False` / `resetDischarge = False` line that follows it.

| Line | Replace | With |
|---|---|---|
| 288 | `inverter.adjust_charge_rate(new_charge_rate)` inside the 10% conditional | keep the conditional; in the `if` branch `charge_rate = new_charge_rate`, and add an `else: charge_rate = current_charge_rate`. Set `rate_owner = "charge"` in both. |
| 292 | `inverter.adjust_discharge_rate(0)` | `discharge_rate = 0` |
| 315 | `inverter.adjust_discharge_rate(0)` | `discharge_rate = 0` |
| 354 | `inverter.adjust_discharge_rate(0)` | `discharge_rate = 0` |
| 373 | `inverter.adjust_discharge_rate(0)` | `discharge_rate = 0` |
| 489 | `inverter.adjust_discharge_rate(inverter.battery_rate_max_export * export_rate_adjust * MINUTE_WATT)` | `discharge_rate = inverter.battery_rate_max_export * export_rate_adjust * MINUTE_WATT`; `rate_owner = "export"` |
| 493 | `inverter.adjust_charge_rate(0)` | `charge_rate = 0` |
| 509 | `inverter.adjust_charge_rate(0)` | `charge_rate = 0` |
| 515 | `inverter.adjust_charge_rate(0)` | `charge_rate = 0` |
| 552 | `inverter.adjust_charge_rate(0)` | `charge_rate = 0`; `rate_owner = "demand_freeze_export"` |
| 558 | `inverter.adjust_charge_rate(0)` | `charge_rate = 0`; `rate_owner = "demand_freeze_export"` |
| 584 | `if resetDischarge: inverter.adjust_discharge_rate(0)` | `if discharge_rate is None: discharge_rate = 0`; `rate_owner = "car"` |
| 617 | `if resetDischarge: inverter.adjust_discharge_rate(0)` | `if discharge_rate is None: discharge_rate = 0`; `rate_owner = "iboost"` |

The specific shape for line 286-292:

```python
                        max_rate = inverter.battery_rate_max_charge * MINUTE_WATT
                        if abs(new_charge_rate - current_charge_rate) > (0.1 * max_rate) or (new_charge_rate == max_rate):
                            charge_rate = new_charge_rate
                        else:
                            # Inside the low-power deadband: intend the rate already set, so the
                            # apply pass is a no-op rather than a reset to max. Reconciled to the
                            # global 5% deadband in a later task.
                            charge_rate = current_charge_rate
                        rate_owner = "charge"
```

- [ ] **Step 5: Replace the end-of-loop reset with intent capture**

Replace lines 630-636 (`# Reset charge/discharge rate` through the `resetCharge` block) with:

```python
            # Reset pause mode; rates are resolved by the apply pass below
            if resetPause:
                inverter.adjust_pause_mode()

            intent[inverter.id] = {
                "charge_rate": charge_rate,
                "discharge_rate": discharge_rate,
                "pause_charge": not resetPause and pause_charge_requested,
                "pause_discharge": not resetPause and pause_discharge_requested,
                "owner": rate_owner,
            }
```

`pause_charge_requested` and `pause_discharge_requested` do not exist yet. Add both as `False` beside the other locals in Step 4, and set the matching one to `True` at each `adjust_pause_mode(pause_charge=True)` / `adjust_pause_mode(pause_discharge=True)` call site, leaving those calls themselves in place. They are read-only context for balance and are not acted on by `apply_inverter_rates`.

- [ ] **Step 6: Add the apply pass**

After the `for inverter in self.inverters:` loop closes and before the status resolution at line 762, add:

```python
        # Single point at which rates reach the hardware
        for inverter in self.inverters:
            if inverter.id in intent:
                self.apply_inverter_rates(inverter, intent[inverter.id])
```

The `id in intent` guard matters: the read-only branch `continue`s and the calibration branch `break`s, so those inverters never record intent and must not be written here.

- [ ] **Step 7: Run the full execute suite — the acceptance gate**

```bash
cd coverage && ./run_all --test execute > /tmp/t2b.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t2b.log
```

Expected: `exit=0` and `0` ERROR lines, **with no assertion edited**. If any scenario fails, the refactor changed behaviour — fix the production code, never the assertion. Common cause: a branch that used to write a rate now falls through to `None` because its `resetCharge = False` was deleted without the corresponding assignment being added.

- [ ] **Step 8: Run the neighbouring suites**

```bash
cd coverage && ./run_all --test balance_inverters --test multi_inverter_status --test inverter > /tmp/t2c.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t2c.log
```

Expected: `exit=0`, `0`.

- [ ] **Step 9: Commit**

```bash
cd coverage && ./run_pre_commit > /tmp/pc2.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/execute.py apps/predbat/tests/test_execute.py
git commit -m "refactor(execute): build per-inverter rate intent, apply once

The 15 rate call sites in execute_plan's loop become assignments to two
locals, and the resetCharge/resetDischarge flags become a None in the
intent. apply_inverter_rates() is now the only place a rate is written,
so 'restore to max' exists once instead of at the end of the loop and
again in the balancer's reset loop.

Behaviour-preserving: the whole execute suite passes unedited.

Refs #5141"
```

---

### Task 3: Pure `balance_inverters()` in utils, wired into execute

**Files:**

- Modify: `apps/predbat/utils.py` (add function at end), `apps/predbat/execute.py` (delete old method, call new one)
- Test: `apps/predbat/tests/test_balance_inverters.py` (rework)

**Interfaces:**

- Consumes: the `intent` dict from Task 2.
- Produces: `balance_inverters(intent, snapshot, balance_charge, balance_discharge, balance_crosscharge, threshold_charge, threshold_discharge, log_to=None)` in `utils.py`. Mutates `intent` in place, returns `None`. `snapshot` is a list of dicts indexed by inverter id with keys `soc_percent`, `reserve_percent`, `battery_power`, `pv_power`, `charge_rate_now`, `discharge_rate_now`, `battery_rate_max_charge`, `battery_rate_max_discharge`, `in_calibration` — all plain floats except the bool.

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_balance_pure.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

from utils import balance_inverters


def make_snapshot(soc_percent, battery_power, charge_rate_now=2600.0, discharge_rate_now=2600.0, reserve_percent=4.0, pv_power=0.0, rate_max=2600.0, in_calibration=False):
    """
    Build one inverter's snapshot entry with sensible defaults.
    """
    return {
        "soc_percent": soc_percent,
        "reserve_percent": reserve_percent,
        "battery_power": battery_power,
        "pv_power": pv_power,
        "charge_rate_now": charge_rate_now,
        "discharge_rate_now": discharge_rate_now,
        "battery_rate_max_charge": rate_max,
        "battery_rate_max_discharge": rate_max,
        "in_calibration": in_calibration,
    }


def test_freeze_hold_is_not_raised():
    """
    F5 / #829 regression: a deliberate rate 0 hold must survive balancing.

    The old reset loop raised any zero rate to max whenever another inverter was non-zero.
    Balance must leave an intent claimed by the executor alone.
    """
    intent = {
        0: {"charge_rate": None, "discharge_rate": 0, "pause_charge": False, "pause_discharge": False, "owner": "freeze"},
        1: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
    }
    snapshot = [make_snapshot(50.0, 1000.0, discharge_rate_now=0.0), make_snapshot(50.0, 1000.0)]
    balance_inverters(intent, snapshot, False, True, True, 1.0, 1.0)
    assert intent[0]["discharge_rate"] == 0, "a deliberate hold must not be raised to max"


def test_calibration_blocks_all_balancing():
    """
    Any inverter in calibration disables balancing entirely, matching the old return False.
    """
    intent = {
        0: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
        1: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
    }
    before = {k: dict(v) for k, v in intent.items()}
    snapshot = [make_snapshot(80.0, 1000.0), make_snapshot(20.0, -1000.0, in_calibration=True)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent == before, "calibration must leave intent untouched"


def test_crosscharge_during_discharge_stops_the_charging_inverter():
    """
    Inverter 1 is charging while the fleet is net discharging - stop it charging.
    """
    intent = {
        0: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
        1: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
    }
    snapshot = [make_snapshot(80.0, 2000.0), make_snapshot(20.0, -500.0)]
    balance_inverters(intent, snapshot, False, False, True, 1.0, 1.0)
    assert intent[1]["charge_rate"] == 0, "the cross-charging inverter must be stopped"


def test_balanced_fleet_is_left_alone():
    """
    Equal SoC means nothing to do, whatever the switches say.
    """
    intent = {
        0: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
        1: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"},
    }
    before = {k: dict(v) for k, v in intent.items()}
    snapshot = [make_snapshot(50.0, 1000.0), make_snapshot(50.0, 1000.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent == before, "a balanced fleet must not be touched"


def test_three_inverters_do_not_crash():
    """
    The (i + 1) % n partner ring is retained from the original, so 3+ must at least be safe.
    """
    intent = {i: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"} for i in range(3)}
    snapshot = [make_snapshot(90.0, 1000.0), make_snapshot(50.0, 1000.0), make_snapshot(10.0, 1000.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert len(intent) == 3


def run_balance_pure_tests(my_predbat):
    """
    Run the pure balance function tests. my_predbat is unused - these need no PredBat instance.
    """
    print("**** Running pure balance_inverters tests ****\n")
    failed = False
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("Test {}: PASSED".format(name))
            except AssertionError as e:
                print("ERROR: Test {} FAILED: {}".format(name, e))
                failed = True
    return failed
```

Register it in `apps/predbat/unit_test.py` — import beside the other test imports:

```python
from tests.test_balance_pure import run_balance_pure_tests
```

and add to the registry beside the `balance_inverters` entry at line 631:

```python
        ("balance_pure", run_balance_pure_tests, "Pure balance_inverters function tests", False),
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd coverage && ./run_all --test balance_pure > /tmp/t3a.log 2>&1; echo "exit=$?"
grep -E "ImportError|cannot import" /tmp/t3a.log
```

Expected: FAIL with `cannot import name 'balance_inverters' from 'utils'`.

- [ ] **Step 3: Port the algorithm into `utils.py`**

Append to `apps/predbat/utils.py`. This is the body of `Execute.balance_inverters` (currently `execute.py:1234-1398`) with `self.` removed, `inverters[i].<attr>` replaced by `snapshot[i]["<attr>"]`, and the two `adjust_*_rate` calls plus the reset loop replaced by intent mutation:

```python
def balance_inverters(intent, snapshot, balance_charge, balance_discharge, balance_crosscharge, threshold_charge, threshold_discharge, log_to=None):
    """
    Mutate the executor's per-inverter rate intent to correct fleet imbalance.

    Ported from the old Execute.balance_inverters() timer loop, with the actuator changed: instead
    of writing rate 0 and later resetting to max, this adjusts the intent that execute_plan built,
    which is then applied once. Convergence therefore returns to the executor's value rather than
    the register ceiling, so a deliberate hold can no longer be overwritten.

    Args:
        intent (dict): inverter id -> rate intent, mutated in place
        snapshot (list): per-inverter plain readings, indexed by inverter id
        balance_charge (bool): balance while charging
        balance_discharge (bool): balance while discharging
        balance_crosscharge (bool): correct cross charging
        threshold_charge (float): minimum SoC% divergence during charge
        threshold_discharge (float): minimum SoC% divergence during discharge
        log_to (callable): optional logger
    """
    num_inverters = len(snapshot)
    if num_inverters < 2:
        return
    for entry in snapshot:
        if entry["in_calibration"]:
            if log_to:
                log_to("BALANCE: an inverter is in calibration, not balancing")
            return

    socs = [entry["soc_percent"] for entry in snapshot]
    reserves = [entry["reserve_percent"] for entry in snapshot]
    battery_powers = [entry["battery_power"] for entry in snapshot]
    pv_powers = [entry["pv_power"] for entry in snapshot]
    charge_rates = [entry["charge_rate_now"] for entry in snapshot]
    discharge_rates = [entry["discharge_rate_now"] for entry in snapshot]

    total_battery_power = sum(battery_powers)
    total_pv_power = sum(pv_powers)
    total_charge_rates = sum(charge_rates)
    total_discharge_rates = sum(discharge_rates)

    out_of_balance = any(soc != socs[0] for soc in socs)
    during_discharge = total_battery_power >= 0.0
    during_charge = total_battery_power < 0.0
    soc_min = min(socs)
    soc_max = max(socs)

    soc_low = [(soc < soc_max) and (abs(soc - soc_max) >= threshold_discharge) for soc in socs]
    soc_high = [(soc > soc_min) and (abs(soc - soc_min) >= threshold_charge) for soc in socs]

    above_reserve = [(socs[i] - reserves[i]) >= 4.0 for i in range(num_inverters)]
    below_full = [socs[i] < 100.0 for i in range(num_inverters)]
    can_power_house = [(total_discharge_rates - discharge_rates[i] - 200) >= total_battery_power for i in range(num_inverters)]
    can_store_pv = [total_pv_power <= (total_charge_rates - charge_rates[i]) for i in range(num_inverters)]
    power_enough_discharge = [battery_powers[i] >= 50.0 for i in range(num_inverters)]
    power_enough_charge = [battery_powers[i] <= -50.0 for i in range(num_inverters)]

    if log_to:
        log_to("BALANCE: socs {} out_of_balance {} soc_low {} soc_high {}".format(socs, out_of_balance, soc_low, soc_high))

    for this_inverter in range(num_inverters):
        other_inverter = (this_inverter + 1) % num_inverters
        if this_inverter not in intent:
            continue
        if balance_discharge and total_discharge_rates > 0 and out_of_balance and during_discharge and soc_low[this_inverter] and above_reserve[other_inverter] and can_power_house[this_inverter] and (power_enough_discharge[this_inverter] or discharge_rates[this_inverter] == 0):
            intent[this_inverter]["discharge_rate"] = 0
        elif balance_charge and total_charge_rates > 0 and out_of_balance and during_charge and soc_high[this_inverter] and below_full[other_inverter] and can_store_pv[this_inverter] and (power_enough_charge[this_inverter] or charge_rates[this_inverter] == 0):
            intent[this_inverter]["charge_rate"] = 0
        elif balance_crosscharge and during_discharge and total_discharge_rates > 0 and power_enough_charge[this_inverter]:
            if soc_low[this_inverter] and can_power_house[other_inverter]:
                intent[this_inverter]["charge_rate"] = 0
            elif can_power_house[this_inverter] and other_inverter in intent:
                intent[other_inverter]["discharge_rate"] = 0
        elif balance_crosscharge and during_charge and total_charge_rates > 0 and power_enough_discharge[this_inverter]:
            intent[this_inverter]["discharge_rate"] = 0
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd coverage && ./run_all --test balance_pure > /tmp/t3b.log 2>&1; echo "exit=$?"
grep -E "PASSED|ERROR" /tmp/t3b.log
```

Expected: `exit=0`, five `PASSED` lines, no `ERROR`.

- [ ] **Step 5: Wire it into `execute_plan`**

In `apps/predbat/execute.py`, extend the import on line 20 with `balance_inverters`:

```python
from utils import dp0, dp2, dp3, calc_percent_limit, find_charge_rate, balance_inverters, export_mode_of, export_power_of, export_target_of
```

Add a snapshot builder as a new method beside `apply_inverter_rates`:

```python
    def build_inverter_snapshot(self):
        """
        Build the plain per-inverter readings that balance_inverters() consumes.

        Returns:
        - list: one dict per inverter, indexed by inverter id
        """
        snapshot = []
        for inverter in self.inverters:
            snapshot.append(
                {
                    "soc_percent": inverter.soc_percent,
                    "reserve_percent": inverter.reserve_current,
                    "battery_power": inverter.battery_power,
                    "pv_power": inverter.pv_power,
                    "charge_rate_now": inverter.charge_rate_now * MINUTE_WATT,
                    "discharge_rate_now": inverter.discharge_rate_now * MINUTE_WATT,
                    "battery_rate_max_charge": inverter.battery_rate_max_charge * MINUTE_WATT,
                    "battery_rate_max_discharge": inverter.battery_rate_max_discharge * MINUTE_WATT,
                    "in_calibration": inverter.in_calibration,
                }
            )
        return snapshot
```

In `execute_plan`, between the per-inverter loop and the apply pass added in Task 2 Step 6:

```python
        if self.balance_inverters_enable and not self.set_read_only:
            balance_inverters(
                intent,
                self.build_inverter_snapshot(),
                self.balance_inverters_charge,
                self.balance_inverters_discharge,
                self.balance_inverters_crosscharge,
                self.balance_inverters_threshold_charge,
                self.balance_inverters_threshold_discharge,
                log_to=self.log,
            )
```

- [ ] **Step 6: Delete the old method and its timer**

Delete `Execute.balance_inverters` entirely — `apps/predbat/execute.py:1234` through the closing `self.log("BALANCE: Completed this run")`.

In `apps/predbat/predbat.py`, delete `run_time_loop_balance` (lines 2122-2136) and its registration block (lines 1983-1989).

- [ ] **Step 7: Rework `test_balance_inverters.py`**

Its scenarios currently call `my_predbat.balance_inverters(test_mode=True)`, which no longer exists. Convert each to build an `intent` and `snapshot` and call the `utils` function directly, asserting on `intent` rather than on `inverter.charge_rate`. The `test_mode` parameter and the `dummy_sleep` helper both disappear. Keep the scenario names so the log stays comparable.

- [ ] **Step 8: Run everything touched**

```bash
cd coverage && ./run_all --test execute --test balance_inverters --test balance_pure --test multi_inverter_status > /tmp/t3c.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t3c.log
```

Expected: `exit=0`, `0`.

- [ ] **Step 9: Commit**

```bash
cd coverage && ./run_pre_commit > /tmp/pc3.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/utils.py apps/predbat/execute.py apps/predbat/predbat.py apps/predbat/unit_test.py apps/predbat/tests/test_balance_pure.py apps/predbat/tests/test_balance_inverters.py
git commit -m "refactor(balance): move balancing into execute as a pure function

balance_inverters() becomes a pure function in utils.py that mutates the
executor's rate intent, called from execute_plan before the single apply
pass. The 60-second timer, its own Inverter() construction and its reset
loop are all deleted.

Closes the F5 / #829 clobber by construction: max is no longer a value
balance can produce, so convergence returns to the executor's intent.
Also removes the unguarded Inverter(self, id) re-creation that turned an
apps.yaml fault into a timer_tick traceback every 60 seconds.

Refs #5141"
```

---

### Task 4: Quick poll becomes the second caller

**Files:**

- Modify: `apps/predbat/execute.py:1067-1083` (`quick_inverter_data_update`), `apps/predbat/const.py:64`, `apps/predbat/config.py:2573`

**Interfaces:**

- Consumes: `build_inverter_snapshot()`, `apply_inverter_rates()` and the `balance_inverters()` import from Task 3.
- Produces: `self.inverter_rate_intent` — the intent dict from the last `execute_plan`, stored on the instance so the poll can re-apply it.

- [ ] **Step 1: Store the intent on the instance**

At the end of `execute_plan`'s apply pass (Task 2 Step 6), add:

```python
        self.inverter_rate_intent = intent
```

Initialise it beside the other instance attributes in `apps/predbat/predbat.py` near line 342:

```python
        self.inverter_rate_intent = {}
```

- [ ] **Step 2: Write the failing test**

Add to `apps/predbat/tests/test_balance_pure.py`:

```python
def test_rebalance_returns_to_intent_not_max():
    """
    Once balanced, re-running balance against a fresh snapshot must leave the executor's
    intent in place - the old code restored to max here, which is what broke freeze charge.
    """
    intent = {
        0: {"charge_rate": 1200, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "charge"},
        1: {"charge_rate": 1200, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "charge"},
    }
    snapshot = [make_snapshot(50.0, -1000.0, charge_rate_now=1200.0), make_snapshot(50.0, -1000.0, charge_rate_now=1200.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent[0]["charge_rate"] == 1200, "a balanced fleet must keep the planned rate, not jump to max"
    assert intent[1]["charge_rate"] == 1200
```

- [ ] **Step 3: Run it**

```bash
cd coverage && ./run_all --test balance_pure > /tmp/t4a.log 2>&1; echo "exit=$?"
grep -E "rebalance_returns|ERROR" /tmp/t4a.log
```

Expected: PASS — the Task 3 implementation already has this property. If it fails, balance is writing max somewhere and must be fixed.

- [ ] **Step 4: Make the poll re-balance and re-apply**

Replace the body of `quick_inverter_data_update` (`execute.py:1080-1083`):

```python
        if self.fetch_inverter_data(create=False):
            self.publish_inverter_data()
            # Second caller of the single write path. execute_plan owns the intent; this only
            # re-derives the balance skew against fresh SoC and re-applies, so the poll can never
            # invent a rate the executor did not ask for.
            if self.inverter_rate_intent and self.balance_inverters_enable and not self.set_read_only:
                intent = {inverter_id: dict(value) for inverter_id, value in self.inverter_rate_intent.items()}
                balance_inverters(
                    intent,
                    self.build_inverter_snapshot(),
                    self.balance_inverters_charge,
                    self.balance_inverters_discharge,
                    self.balance_inverters_crosscharge,
                    self.balance_inverters_threshold_charge,
                    self.balance_inverters_threshold_discharge,
                    log_to=self.log,
                )
                for inverter in self.inverters:
                    if inverter.id in intent:
                        self.apply_inverter_rates(inverter, intent[inverter.id])
            return True
        return False
```

The copy matters: the poll must not mutate the stored intent, or successive polls would compound their own skew.

- [ ] **Step 5: Change the poll cadence**

In `apps/predbat/const.py:64`:

```python
INVERTER_QUICK_UPDATE_SECONDS = 60  # Minimum seconds between quick inverter data updates
```

- [ ] **Step 6: Remove the dead config item**

Delete the `"balance_inverters_seconds": {"type": "integer", "zero": True},` line from `APPS_SCHEMA` in `apps/predbat/config.py:2573`. `validate_config()` iterates `for name in APPS_SCHEMA`, so a stale entry left in a user's apps.yaml is silently ignored rather than erroring.

- [ ] **Step 7: Run the suites**

```bash
cd coverage && ./run_all --test execute --test balance_pure --test balance_inverters --test inverter > /tmp/t4b.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t4b.log
```

Expected: `exit=0`, `0`.

- [ ] **Step 8: Commit**

```bash
cd coverage && ./run_pre_commit > /tmp/pc4.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/execute.py apps/predbat/const.py apps/predbat/config.py apps/predbat/predbat.py apps/predbat/tests/test_balance_pure.py
git commit -m "feat(balance): re-balance on the inverter poll, drop the balance timer

The 120s quick inverter poll becomes the second caller of the single write
path at 60s, re-deriving the balance skew against fresh SoC and re-applying
the executor's intent. It works on a copy so successive polls cannot compound
their own skew. balance_inverters_seconds is removed - the poll interval now
governs.

Refs #5141"
```

---

### Task 5: Per-inverter export rate allocation

**Files:**

- Modify: `apps/predbat/utils.py` (add `allocate_export_rates`), `apps/predbat/execute.py` (pre-pass + use at line 489)
- Test: `apps/predbat/tests/test_utils_allocate.py` (create)

**Interfaces:**

- Produces: `allocate_export_rates(needs, max_rates, p_fleet)` in `utils.py` — `needs` is a list of kWh still to shed per inverter, `max_rates` a list of per-inverter export ceilings in W, `p_fleet` the planned fleet export power in W. Returns a list of W per inverter summing to `min(p_fleet, sum(max_rates))`.

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_utils_allocate.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

from utils import allocate_export_rates


def test_full_rate_is_a_no_op():
    """
    With low power off, p_fleet equals the sum of the ceilings, so every inverter runs at its
    own maximum and the allocation is identical to today's uniform scaling.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 5200.0)
    assert alloc == [2600.0, 2600.0]


def test_matched_fleet_equal_needs_is_uniform():
    """
    Equal needs and equal ceilings give the uniform split, matching today.
    """
    alloc = allocate_export_rates([2.0, 2.0], [2600.0, 2600.0], 3640.0)
    assert alloc == [1820.0, 1820.0]


def test_sum_is_preserved():
    """
    The planner costed p_fleet, so the allocation must deliver exactly that.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 3640.0)
    assert abs(sum(alloc) - 3640.0) < 0.01


def test_clamped_surplus_spills_to_the_other_inverter():
    """
    A 3:1 need split of 3640W would give 2730W to the first inverter, above its 2600W ceiling.
    The 130W surplus must spill rather than be lost.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 3640.0)
    assert alloc[0] == 2600.0
    assert abs(alloc[1] - 1040.0) < 0.01


def test_at_target_inverter_gets_nothing_and_spills_its_share():
    """
    An inverter already at its export target holds no budget; the fleet keeps the planned power.
    """
    alloc = allocate_export_rates([0.0, 2.0], [2600.0, 2600.0], 2000.0)
    assert alloc[0] == 0.0
    assert abs(alloc[1] - 2000.0) < 0.01


def test_no_need_falls_back_to_uniform():
    """
    Nothing to shed anywhere - fall back to the uniform split rather than dividing by zero.
    """
    alloc = allocate_export_rates([0.0, 0.0], [2600.0, 2600.0], 2600.0)
    assert abs(sum(alloc) - 2600.0) < 0.01
    assert abs(alloc[0] - alloc[1]) < 0.01


def test_single_inverter():
    """
    Degenerate fleet of one.
    """
    alloc = allocate_export_rates([1.0], [2600.0], 1300.0)
    assert alloc == [1300.0]


def test_demand_above_fleet_capacity_is_capped():
    """
    p_fleet can never exceed the sum of the ceilings.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 99999.0)
    assert alloc == [2600.0, 2600.0]


def run_allocate_export_tests(my_predbat):
    """
    Run the export allocator tests. my_predbat is unused - these need no PredBat instance.
    """
    print("**** Running allocate_export_rates tests ****\n")
    failed = False
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("Test {}: PASSED".format(name))
            except AssertionError as e:
                print("ERROR: Test {} FAILED: {}".format(name, e))
                failed = True
    return failed
```

Register in `apps/predbat/unit_test.py`:

```python
from tests.test_utils_allocate import run_allocate_export_tests
```

```python
        ("allocate_export", run_allocate_export_tests, "Export rate allocator tests", False),
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd coverage && ./run_all --test allocate_export > /tmp/t5a.log 2>&1; echo "exit=$?"
grep -E "ImportError|cannot import" /tmp/t5a.log
```

Expected: FAIL with `cannot import name 'allocate_export_rates'`.

- [ ] **Step 3: Implement it**

Append to `apps/predbat/utils.py`:

```python
def allocate_export_rates(needs, max_rates, p_fleet):
    """
    Split the planned fleet export power across inverters by how much each still has to shed.

    The planner costs a specific fleet export power (the low-power ladder), so the sum of the
    allocation is pinned to it and only the split varies. An inverter already at its target takes
    none of the budget and its share spills to inverters that can still deliver, which is what
    stops fleet export power sagging below plan as inverters finish one by one.

    At full rate p_fleet equals the sum of the ceilings, so every inverter clamps at its own
    maximum and this is exactly today's uniform scaling.

    Args:
        needs (list): kWh each inverter still has to shed before its own target
        max_rates (list): per-inverter export ceiling in W
        p_fleet (float): planned fleet export power in W

    Returns:
    - list: export rate in W per inverter, summing to min(p_fleet, sum(max_rates))
    """
    count = len(max_rates)
    if count == 0:
        return []

    remaining = min(p_fleet, sum(max_rates))
    total_need = sum(need for need in needs if need > 0)
    if total_need <= 0:
        # Nothing to shed anywhere - fall back to the uniform split
        needs = [1.0] * count
        total_need = float(count)

    alloc = [0.0] * count
    open_set = [i for i in range(count) if needs[i] > 0]

    while remaining > 0.01 and open_set:
        share_total = sum(needs[i] for i in open_set)
        if share_total <= 0:
            break
        clamped_any = False
        budget = remaining
        for i in list(open_set):
            want = alloc[i] + budget * (needs[i] / share_total)
            if want >= max_rates[i]:
                remaining -= max_rates[i] - alloc[i]
                alloc[i] = max_rates[i]
                open_set.remove(i)
                clamped_any = True
        if not clamped_any:
            for i in open_set:
                alloc[i] += remaining * (needs[i] / share_total)
            remaining = 0.0

    return alloc
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd coverage && ./run_all --test allocate_export > /tmp/t5b.log 2>&1; echo "exit=$?"
grep -E "PASSED|ERROR" /tmp/t5b.log
```

Expected: `exit=0`, eight `PASSED`, no `ERROR`.

- [ ] **Step 5: Wire it into `execute_plan`**

Add `allocate_export_rates` to the `utils` import on `execute.py:20`.

Before the per-inverter loop, after the `set_charge_freeze_only` clamp block (around line 150), add the fleet pre-pass:

```python
        # Fleet-level export allocation. Needs every inverter's target, so it cannot live inside
        # the per-inverter loop. adjust_battery_target_multi(check=True) probes without writing.
        export_rate_alloc = {}
        if self.export_limits_best and self.set_export_window:
            export_rate_adjust = 1.0
            if self.set_export_low_power:
                export_rate_adjust = 1 - (self.export_limits_best[0] - int(self.export_limits_best[0]))
            export_target_percent = self.export_target_soc_percent()
            needs = []
            max_rates = []
            for inverter in self.inverters:
                inv_target_percent = self.adjust_battery_target_multi(inverter, export_target_percent, False, True, check=True)
                target_kwh = inv_target_percent * inverter.soc_max / 100.0
                needs.append(max(0.0, inverter.soc_kw - target_kwh))
                max_rates.append(inverter.battery_rate_max_export * MINUTE_WATT)
            p_fleet = sum(max_rates) * export_rate_adjust
            allocation = allocate_export_rates(needs, max_rates, p_fleet)
            for index, inverter in enumerate(self.inverters):
                export_rate_alloc[inverter.id] = allocation[index]
```

Replace the export rate assignment made in Task 2 at line 489:

```python
                        discharge_rate = export_rate_alloc.get(inverter.id, inverter.battery_rate_max_export * export_rate_adjust * MINUTE_WATT)
                        rate_owner = "export"
```

- [ ] **Step 6: Add an execute-level scenario**

At the end of `run_execute_tests`, using the Task 1 arrays:

```python
    # Mixed fleet exporting at 70% of fleet max. Inverter 0 has 3x the need of inverter 1, so it
    # takes the larger share and clamps at its ceiling, spilling the surplus rather than both
    # running at a uniform 70%.
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 20.0}]
    export_limits_best = [20.3]
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_export_allocation",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        soc_kw=10.0,
        soc_kw_array=[7.5, 2.5],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        set_charge_window=True,
        set_export_window=True,
        set_export_low_power=True,
        assert_status="Exporting",
        assert_force_export=True,
    )
    if failed:
        return failed
```

Run it once, read the actual allocated rates from the log, then add `assert_discharge_rate_array=[...]` with those values and confirm they sum to `0.7 * 5200 = 3640`. If they do not sum to 3640, the wiring is wrong — fix the production code, not the assertion.

- [ ] **Step 7: Run the suites**

```bash
cd coverage && ./run_all --test execute --test allocate_export --test balance_pure > /tmp/t5c.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t5c.log
```

Expected: `exit=0`, `0`.

- [ ] **Step 8: Commit**

```bash
cd coverage && ./run_pre_commit > /tmp/pc5.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/utils.py apps/predbat/execute.py apps/predbat/unit_test.py apps/predbat/tests/test_utils_allocate.py apps/predbat/tests/test_execute.py
git commit -m "feat(execute): allocate export power per inverter instead of scaling uniformly

The planner costs a specific fleet export power, so the sum is pinned and
only the split varies - by how much each inverter still has to shed, clamped
at its own ceiling with the surplus spilled. At full rate every inverter
clamps at its maximum, making this a no-op outside low power mode.

Fixes fleet export power sagging below plan as inverters reach target one by
one, each still holding its 1/n slice of the budget while delivering nothing.

Refs #5141"
```

---

### Task 6: Reconcile the charge rate deadband to 5%

The first task that deliberately changes an assertion. Exactly one test asserts the 10% behaviour.

**Files:**

- Modify: `apps/predbat/execute.py:286-292`, `apps/predbat/tests/test_execute.py:1035`

- [ ] **Step 1: Delete the execute-side conditional**

Replace the block written in Task 2 Step 4 with:

```python
                        # One deadband, in the only writer. adjust_charge_rate and
                        # adjust_discharge_rate both suppress changes below 5% of max, which
                        # aligns with the GE power steps.
                        charge_rate = new_charge_rate
                        rate_owner = "charge"
```

`max_rate` and `current_charge_rate` are still used by the log line above, so leave both in place.

- [ ] **Step 2: Run to see the one expected failure**

```bash
cd coverage && ./run_all --test execute > /tmp/t6a.log 2>&1; echo "exit=$?"
grep -B2 -A2 "Charge rate should be" /tmp/t6a.log
```

Expected: exactly one failure, in `charge_low_power2b`: `Charge rate should be 600 got 480`. The scenario computes 480 W against a 2000 W per-inverter ceiling with a current rate of 600 W. At 10% (200 W) the 120 W delta was suppressed; at 5% (100 W) it is written.

If any *other* scenario fails, stop — something beyond the deadband changed.

- [ ] **Step 3: Update the one assertion**

In `apps/predbat/tests/test_execute.py`, scenario `charge_low_power2b` at line 1035:

```python
        assert_charge_rate=480,  # 120W delta clears the 5% (100W) deadband
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd coverage && ./run_all --test execute > /tmp/t6b.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t6b.log
```

Expected: `exit=0`, `0`.

- [ ] **Step 5: Commit**

```bash
cd coverage && ./run_pre_commit > /tmp/pc6.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/execute.py apps/predbat/tests/test_execute.py
git commit -m "refactor(execute): one charge rate deadband at 5%, aligned to GE power steps

The 10% check in execute.py and the 5% in adjust_charge_rate both came from
#1676 and were never reconciled; since 10% > 5% the execute-side check was
the only one that ever fired. Deleting it leaves one deadband, symmetric
across charge and discharge, inside the only writer - and intent now carries
the desired rate rather than a deadband-mangled one.

charge_low_power2b changes 600 -> 480: a 120W delta against a 2000W ceiling
clears 5% where it did not clear 10%.

Refs #5141"
```

---

### Task 7: Defaults and documentation

**Files:**

- Modify: `apps/predbat/config.py:1174-1186`, `docs/customisation.md:517-525`, `docs/apps-yaml.md:2271`, `docs/caution.md:31`

- [ ] **Step 1: Flip the two defaults**

In `apps/predbat/config.py`, change `"default": True` to `"default": False` for `balance_inverters_charge` (line 1171) and `balance_inverters_discharge` (line 1178). Leave `balance_inverters_crosscharge` at `True` and `balance_inverters_enable` at `False`.

Both switches remain fully functional when enabled — only the defaults change. `load_current_config()` restores saved values ahead of defaults, so this reaches new installs only.

- [ ] **Step 2: Verify no test depends on the old defaults**

```bash
cd coverage && ./run_all --test execute --test balance_inverters --test balance_pure --test allocate_export > /tmp/t7a.log 2>&1; echo "exit=$?"
grep -c ERROR /tmp/t7a.log
```

Expected: `exit=0`, `0`.

- [ ] **Step 3: Update the docs**

`docs/customisation.md` — delete the `balance_inverters_seconds` paragraph at line 517 and replace the bullet list at 519-525 so it states the new defaults: cross-charge on, charge and discharge off, and that balancing now runs as part of the normal execute cycle rather than on its own timer.

`docs/apps-yaml.md:2271` — delete the `balance_inverters_seconds: seconds` entry.

`docs/caution.md:31` — the warning says the feature "can make register changes once or twice a minute". Rewrite to explain that the reset-loop churn behind that warning is gone, that balancing now shares the executor's single write path with a 5% deadband, and that cross-charge prevention is the part worth enabling.

- [ ] **Step 4: Run the full suite**

```bash
cd coverage && ./run_all > /tmp/t7b.log 2>&1; echo "exit=$?"
grep -cE "^ERROR|FAILED" /tmp/t7b.log
tail -5 /tmp/t7b.log
```

Expected: `exit=0`, `0`, and a final line reporting all tests passed.

- [ ] **Step 5: Check the change scope**

Run `detect_changes({scope: "compare", base_ref: "main"})` and confirm the affected symbols are confined to `execute.py`, `utils.py`, `predbat.py`, `config.py`, `const.py` and the test modules. Anything outside that set needs explaining before the PR leaves draft.

- [ ] **Step 6: Commit and push**

```bash
cd coverage && ./run_pre_commit > /tmp/pc7.log 2>&1; echo "exit=$?"
cd .. && git add apps/predbat/config.py docs/customisation.md docs/apps-yaml.md docs/caution.md
git commit -m "feat(balance): default SoC balancing off, keep cross-charge prevention on

Balanced SoC is not important in itself; stopping one inverter charging from
another during eco/idle is. Both SoC balancing switches stay fully functional
when enabled. Saved config takes priority over defaults, so this reaches new
installs only.

Docs updated for the removed balance_inverters_seconds setting and the
caution.md warning about register churn, which the single write path and the
5% deadband address.

Refs #5141"
git push
```

---

## Self-Review

**Spec coverage** — every section maps to a task: architecture and intent → Task 2; the balance function → Task 3; cadence, poll wiring and `balance_inverters_seconds` → Task 4; export allocator → Task 5; deadband → Task 6; config and docs → Task 7; the testing section's harness arrays → Task 1.

**Deliberately not covered, matching the spec's non-goals:** F1, F2, AC limits in the allocator, pause as mutable intent, and a *per-inverter* PV-aware cross-charge rule.

**Amended while executing.** Two items this section originally listed as deferred were fixed instead, because moving the algorithm into a pure function made faults visible that could not responsibly be left. The spec's "Amended during implementation" section is the record; in short:

- **F7 was fixed**, along with two coupled faults it travelled with — a pass could hold a charger and a discharger at once, and the capacity guards were each evaluated as though its own hold were the only change.
- **Direction detection was changed** from the battery-sign proxy to the site energy balance, after it was shown holding every inverter that was correctly absorbing a PV surplus. The per-inverter PV case remains unsolved and stays a non-goal.

**The known gap is closed.** The spec called out that the quick poll becoming a write path starts conferring control-ledger ownership on a cadence that today only observes, and said it "needs its own test rather than being assumed benign". Reading `control_ledger.py` showed the rate writes were not the exposure — those controls were already tracked — but the 120s → 60s cadence change was: the tamper ladder counted cycles with no wall-clock floor, so a faster poll convicted a cached read sooner. Fixed by `MIN_DIVERGENCE_S`, measured from the confirming write so the threshold no longer moves with cadence, and covered by `test_divergence_needs_elapsed_time_not_just_cycles` and `test_divergence_floor_matches_the_previous_cadence_behaviour`. The poll's own write path is covered by `test_quick_poll_rebalance_guards`.

**Sequencing note.** Task 2 is the only task whose acceptance test is "nothing changes". Do not fold Task 6 into it — the deadband change moves a real assertion, and mixing the two would destroy the parity signal that makes the HIGH-risk refactor safe to review.
