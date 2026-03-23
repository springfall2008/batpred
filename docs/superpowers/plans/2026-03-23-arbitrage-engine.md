# Arbitrage Engine Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a target-driven arbitrage engine to Predbat that optimises grid import/export on Octopus Agile to hit a user-defined daily profit target.

**Architecture:** A new standalone `ArbitrageEngine` class in `arbitrage.py` consumes existing rate and forecast data already present in Predbat, scores 30-minute slot pairs by net spread (after round-trip battery efficiency), selects the minimum set to hit the profit target, and injects pre-committed charge/export constraints into the existing `plan.py` optimiser via the `manual_all_times` mechanism. Four new HA entities expose the results.

**Tech Stack:** Python 3.11+, existing Predbat test runner (`unit_test.py` + functions in `tests/` package — **not** pytest), `config.py` CONFIG_ITEMS pattern, `output.py` entity publishing pattern, `db_manager.py` for daily gain persistence.

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `apps/predbat/arbitrage.py` | `ArbitrageEngine` class: slot scoring, scheduling, constraint output |
| Create | `apps/predbat/tests/test_arbitrage.py` | Unit tests — no HA/AppDaemon dependency, follows project test conventions |
| Modify | `apps/predbat/unit_test.py` | Register `test_arbitrage` so it runs in CI |
| Modify | `apps/predbat/config.py` | Add 4 new CONFIG_ITEMS for arbitrage settings |
| Modify | `apps/predbat/plan.py` | Instantiate ArbitrageEngine, inject slot constraints, store metrics |
| Modify | `apps/predbat/output.py` | Publish 4 new HA sensor/binary_sensor entities |

---

## Test Runner Conventions

This project does **not** use pytest. Tests are plain Python functions registered in `unit_test.py`. Follow this pattern throughout:

```python
# In tests/test_arbitrage.py

def _test_<specific_thing>(my_predbat=None):
    """Return True on failure, False on success."""
    failed = False
    # ... assertions ...
    if some_condition_fails:
        print(f"ERROR: ...")
        failed = True
    return failed

def test_arbitrage(my_predbat=None):
    """Main test runner — returns True if any test failed."""
    sub_tests = [
        ("descriptive name", _test_<specific_thing>),
        # ...
    ]
    failed = 0
    for name, fn in sub_tests:
        print(f"*** Running: {name}")
        try:
            result = fn(my_predbat)
            if result:
                print(f"FAILED: {name}")
                failed += 1
            else:
                print(f"PASSED: {name}")
        except Exception as e:
            print(f"EXCEPTION in {name}: {e}")
            failed += 1
    print(f"RESULTS: {len(sub_tests) - failed} passed, {failed} failed")
    return failed > 0
```

Run tests with:
```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick
```

---

## Chunk 1: Config Items & Module Skeleton

### Task 1: Add Arbitrage Config Items to config.py

**Files:**
- Modify: `apps/predbat/config.py`

- [ ] **Step 1: Read config.py to locate the insertion point**

Open `apps/predbat/config.py`. Find the `expert_mode` switch entry in `CONFIG_ITEMS`. Add the 4 arbitrage entries immediately after it.

- [ ] **Step 2: Add 4 CONFIG_ITEMS entries**

```python
{
    "name": "arbitrage_enable",
    "friendly_name": "Arbitrage Enable",
    "type": "switch",
    "default": False,
},
{
    "name": "arbitrage_profit_target_daily",
    "friendly_name": "Arbitrage Daily Profit Target",
    "type": "input_number",
    "min": 0,
    "max": 50.0,
    "step": 0.10,
    "unit": "£",
    "icon": "mdi:currency-gbp",
    "default": 1.0,
},
{
    "name": "arbitrage_profit_target_weekly",
    "friendly_name": "Arbitrage Weekly Profit Target",
    "type": "input_number",
    "min": 0,
    "max": 350.0,
    "step": 1.0,
    "unit": "£",
    "icon": "mdi:currency-gbp",
    "default": 7.0,
},
{
    "name": "arbitrage_reserve",
    "friendly_name": "Arbitrage Battery Reserve",
    "type": "input_number",
    "min": 0,
    "max": 100,
    "step": 5,
    "unit": "%",
    "icon": "mdi:battery-arrow-up",
    "default": 20.0,
},
```

- [ ] **Step 3: Verify no syntax errors**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 -c "from config import CONFIG_ITEMS; print(len(CONFIG_ITEMS), 'items OK')"
```
Expected: prints count followed by `items OK`

- [ ] **Step 4: Commit**

```bash
git add apps/predbat/config.py
git commit -m "feat(arbitrage): add CONFIG_ITEMS for arbitrage settings"
```

---

### Task 2: Create ArbitrageEngine Skeleton

**Files:**
- Create: `apps/predbat/arbitrage.py`

- [ ] **Step 1: Create arbitrage.py**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

"""Arbitrage engine for target-driven grid import/export optimisation.

Analyses Agile import/export rates alongside solar and load forecasts to select
the minimum set of 30-minute charge/export slot pairs needed to hit a
user-defined daily profit target. Outputs pre-committed slot constraints
for injection into plan.py.
"""

from __future__ import annotations

SLOT_MINUTES = 30  # Agile slot size in minutes


class ArbitrageEngine:
    """Compute arbitrage schedules for dynamic tariffs."""

    def __init__(
        self,
        rate_import: dict,       # {minute: pence_per_kwh}
        rate_export: dict,       # {minute: pence_per_kwh}
        solar_forecast: dict,    # {minute: kW}
        load_forecast: dict,     # {minute: kW}
        battery_soc_percent: float,        # current SoC 0-100
        battery_capacity_kwh: float,       # usable capacity in kWh
        charge_rate_kw: float,             # max charge rate in kW
        discharge_rate_kw: float,          # max discharge rate in kW
        battery_efficiency: float,         # round-trip efficiency 0.0-1.0
        profit_target_daily: float,        # target daily profit in £
        arbitrage_reserve_percent: float,  # % of battery ring-fenced for arbitrage
        minutes_now: int,                  # current minute of day (0-1439)
    ):
        self.rate_import = rate_import
        self.rate_export = rate_export
        self.solar_forecast = solar_forecast
        self.load_forecast = load_forecast
        self.battery_soc_percent = battery_soc_percent
        self.battery_capacity_kwh = battery_capacity_kwh
        self.charge_rate_kw = charge_rate_kw
        self.discharge_rate_kw = discharge_rate_kw
        self.battery_efficiency = battery_efficiency
        self.profit_target_daily = profit_target_daily
        self.arbitrage_reserve_percent = arbitrage_reserve_percent
        self.minutes_now = minutes_now

    def score_slots(self) -> list[dict]:
        """Return scored list of charge/export slot pairs sorted by net profit.

        Each entry: {"charge_minute": int, "export_minute": int,
                     "net_profit_gbp": float, "charge_kwh": float,
                     "discharge_kwh": float}
        Only positive-spread pairs after efficiency losses are included.
        Confidence discount applied: slots further ahead score proportionally lower.
        Sorted descending by discounted net profit.
        """
        raise NotImplementedError

    def schedule_to_target(self) -> list[dict]:
        """Select minimum non-overlapping slot pairs to hit profit_target_daily.

        Returns chronological list of slot dicts:
        {"start": int, "end": int, "type": "charge"|"export", "target_soc": float}.
        If target is unachievable, returns the best possible schedule without error.
        """
        raise NotImplementedError

    def plan_constraints(self) -> list[dict]:
        """Return slot constraints ready for injection into plan.py.

        Format matches charge_window/export_window entries used by plan.py:
        {"start": minute, "end": minute, "average": rate_p_per_kwh,
         "min": 0, "max": target_soc, "constraint_type": "charge"|"export"}
        """
        raise NotImplementedError

    def projected_gain(self) -> float:
        """Return projected arbitrage profit for today in £."""
        raise NotImplementedError

    def opportunity_score(self) -> int:
        """Return 0-100 score representing current arbitrage opportunity quality."""
        raise NotImplementedError
```

- [ ] **Step 2: Verify import works**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 -c "from arbitrage import ArbitrageEngine; print('ArbitrageEngine import OK')"
```
Expected: `ArbitrageEngine import OK`

- [ ] **Step 3: Commit**

```bash
git add apps/predbat/arbitrage.py
git commit -m "feat(arbitrage): add ArbitrageEngine class skeleton"
```

---

## Chunk 2: Slot Scoring

### Task 3: Test & Implement score_slots()

**Files:**
- Create: `apps/predbat/tests/test_arbitrage.py`
- Modify: `apps/predbat/unit_test.py`
- Modify: `apps/predbat/arbitrage.py`

**Background:** In Predbat, `rate_import` and `rate_export` are dicts keyed by minute (0–1439), values in pence per kWh. For Agile, meaningful keys are at 0, 30, 60, 90, etc. The raw net profit for a charge-at-A, export-at-B pair is:

```
charge_kwh     = charge_rate_kw * 0.5
discharge_kwh  = charge_kwh * battery_efficiency
raw_profit_gbp = (rate_export[B]/100 * discharge_kwh) - (rate_import[A]/100 * charge_kwh)
```

Confidence discount (applied only to positive raw profits, to prevent inflating near-zero losses):
```
hours_ahead = (charge_minute - minutes_now) / 60.0
confidence  = max(0.5, 1.0 - hours_ahead / 96.0)   # 1.0 now → 0.5 at 48h
net_profit_gbp = raw_profit_gbp * confidence
```

- [ ] **Step 1: Create test_arbitrage.py**

```python
# apps/predbat/tests/test_arbitrage.py
# fmt: off
# pylint: disable=line-too-long
"""Unit tests for ArbitrageEngine — no HA or AppDaemon dependency."""

from arbitrage import ArbitrageEngine


def _make_engine(**kwargs) -> ArbitrageEngine:
    """Build an ArbitrageEngine with sensible defaults, overridable per-test."""
    defaults = {
        "rate_import": {i: 15.0 for i in range(0, 1440)},  # 15p flat — no spread
        "rate_export": {i: 5.0 for i in range(0, 1440)},   # 5p flat
        "solar_forecast": {i: 0.0 for i in range(0, 1440)},
        "load_forecast": {i: 0.3 for i in range(0, 1440)},
        "battery_soc_percent": 50.0,
        "battery_capacity_kwh": 10.0,
        "charge_rate_kw": 3.6,
        "discharge_rate_kw": 3.6,
        "battery_efficiency": 0.9,
        "profit_target_daily": 1.0,
        "arbitrage_reserve_percent": 20.0,
        "minutes_now": 0,
    }
    defaults.update(kwargs)
    return ArbitrageEngine(**defaults)


# ---------------------------------------------------------------------------
# score_slots() tests
# ---------------------------------------------------------------------------

def _test_score_no_spread(my_predbat=None):
    """With import >= export after efficiency losses, no profitable slots."""
    slots = _make_engine().score_slots()
    if slots != []:
        print(f"ERROR: Expected [], got {slots[:3]}")
        return True
    return False


def _test_score_single_profitable_pair(my_predbat=None):
    """One cheap import slot + one expensive export slot produces a scored pair."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[0] = 3.0
    rate_export[60] = 40.0
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export).score_slots()
    if len(slots) < 1:
        print("ERROR: Expected at least one profitable slot pair")
        return True
    best = slots[0]
    if best["charge_minute"] != 0 or best["export_minute"] != 60:
        print(f"ERROR: Expected charge=0, export=60, got charge={best['charge_minute']}, export={best['export_minute']}")
        return True
    if best["net_profit_gbp"] <= 0:
        print(f"ERROR: Expected positive net_profit_gbp, got {best['net_profit_gbp']}")
        return True
    return False


def _test_score_sorted_descending(my_predbat=None):
    """Slots must be sorted descending by net_profit_gbp."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[0] = 3.0
    rate_export[60] = 40.0
    rate_import[30] = 8.0
    rate_export[90] = 20.0
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export).score_slots()
    profits = [s["net_profit_gbp"] for s in slots]
    if profits != sorted(profits, reverse=True):
        print(f"ERROR: Slots not sorted descending: {profits[:5]}")
        return True
    return False


def _test_score_export_after_charge(my_predbat=None):
    """Export slot must be strictly after charge slot — time travel not allowed."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[120] = 3.0
    rate_export[60] = 50.0  # export BEFORE the cheap import — invalid
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export).score_slots()
    for s in slots:
        if s["charge_minute"] == 120 and s["export_minute"] == 60:
            print("ERROR: Found export-before-charge pair in results")
            return True
    return False


def _test_score_future_slots_only(my_predbat=None):
    """Both charge and export slots must be >= minutes_now."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export, minutes_now=120).score_slots()
    for s in slots:
        if s["charge_minute"] < 120:
            print(f"ERROR: charge_minute {s['charge_minute']} < minutes_now 120")
            return True
        if s["export_minute"] < 120:
            print(f"ERROR: export_minute {s['export_minute']} < minutes_now 120")
            return True
    return False


def _test_score_confidence_near_beats_far(my_predbat=None):
    """Identical spreads at different horizons: nearer slot scores >= farther slot."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[30] = 3.0
    rate_export[60] = 40.0   # near pair (1h ahead)
    rate_import[660] = 3.0
    rate_export[690] = 40.0  # far pair (11.5h ahead)
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export, minutes_now=0).score_slots()
    near = next((s for s in slots if s["charge_minute"] == 30 and s["export_minute"] == 60), None)
    far = next((s for s in slots if s["charge_minute"] == 660 and s["export_minute"] == 690), None)
    if near is None or far is None:
        print("ERROR: Could not find near or far slot pair in results")
        return True
    if near["net_profit_gbp"] < far["net_profit_gbp"]:
        print(f"ERROR: Near ({near['net_profit_gbp']:.4f}) scored lower than far ({far['net_profit_gbp']:.4f})")
        return True
    return False


def test_arbitrage(my_predbat=None):
    """Main arbitrage test runner. Returns True if any test failed."""
    sub_tests = [
        ("score_slots: no spread returns empty", _test_score_no_spread),
        ("score_slots: single profitable pair", _test_score_single_profitable_pair),
        ("score_slots: sorted descending", _test_score_sorted_descending),
        ("score_slots: export after charge only", _test_score_export_after_charge),
        ("score_slots: future slots only", _test_score_future_slots_only),
        ("score_slots: confidence discount near > far", _test_score_confidence_near_beats_far),
    ]
    failed = 0
    for name, fn in sub_tests:
        print(f"*** Running: {name}")
        try:
            result = fn(my_predbat)
            if result:
                print(f"FAILED: {name}")
                failed += 1
            else:
                print(f"PASSED: {name}")
        except Exception as e:
            print(f"EXCEPTION in {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"RESULTS: {len(sub_tests) - failed} passed, {failed} failed out of {len(sub_tests)} tests")
    return failed > 0
```

- [ ] **Step 2: Register test_arbitrage in unit_test.py**

Add the import near the top of `unit_test.py` with the other test imports:

```python
from tests.test_arbitrage import test_arbitrage
```

Then add an entry to the `TEST_REGISTRY` list following the same 4-tuple pattern as `test_axle` and `test_db_manager`:

```python
("arbitrage", test_arbitrage, "Arbitrage engine tests (slot scoring, confidence discounting, future-only slots)", False),
```

Do **not** add a `failed |=` call — `unit_test.py` uses `TEST_REGISTRY` exclusively, not imperative `failed |=` calls.

- [ ] **Step 3: Run the test runner to verify test_arbitrage fails**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -A2 "arbitrage\|RESULTS.*arbitrage"
```
Expected: Tests run and fail with `EXCEPTION` (NotImplementedError from score_slots)

- [ ] **Step 4: Implement score_slots() in arbitrage.py**

Replace `raise NotImplementedError` in `score_slots()`:

```python
def score_slots(self) -> list[dict]:
    scored = []
    slot_hours = SLOT_MINUTES / 60.0
    future_slots = [
        m for m in range(self.minutes_now, 1440, SLOT_MINUTES)
        if m in self.rate_import
    ]

    for charge_minute in future_slots:
        charge_kwh = self.charge_rate_kw * slot_hours
        import_cost_gbp = (self.rate_import[charge_minute] / 100.0) * charge_kwh

        for export_minute in future_slots:
            if export_minute <= charge_minute:
                continue
            if export_minute not in self.rate_export:
                continue

            discharge_kwh = charge_kwh * self.battery_efficiency
            export_revenue_gbp = (self.rate_export[export_minute] / 100.0) * discharge_kwh
            raw_profit_gbp = export_revenue_gbp - import_cost_gbp

            # Only score profitable pairs; apply confidence discount after sign check
            if raw_profit_gbp <= 0:
                continue

            # Confidence discount: linear from 1.0 at 0h ahead to 0.5 at 48h
            hours_ahead = (charge_minute - self.minutes_now) / 60.0
            confidence = max(0.5, 1.0 - (hours_ahead / 96.0))
            net_profit_gbp = raw_profit_gbp * confidence

            scored.append({
                "charge_minute": charge_minute,
                "export_minute": export_minute,
                "net_profit_gbp": round(net_profit_gbp, 4),
                "charge_kwh": round(charge_kwh, 3),
                "discharge_kwh": round(discharge_kwh, 3),
            })

    scored.sort(key=lambda x: x["net_profit_gbp"], reverse=True)
    return scored
```

- [ ] **Step 5: Run tests to verify score_slots tests pass**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "Running.*score|PASSED|FAILED|RESULTS"
```
Expected: All 6 score_slots tests show `PASSED`

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/arbitrage.py apps/predbat/tests/test_arbitrage.py apps/predbat/unit_test.py
git commit -m "feat(arbitrage): implement score_slots() with tests wired into unit_test.py"
```

---

## Chunk 3: Target Scheduling

### Task 4: Test & Implement schedule_to_target()

**Files:**
- Modify: `apps/predbat/tests/test_arbitrage.py`
- Modify: `apps/predbat/arbitrage.py`

- [ ] **Step 1: Add schedule_to_target tests to test_arbitrage.py**

Add these functions and register them in `test_arbitrage()`:

```python
# ---------------------------------------------------------------------------
# schedule_to_target() tests
# ---------------------------------------------------------------------------

def _test_schedule_no_profitable_slots(my_predbat=None):
    """When there are no profitable slots, return empty schedule."""
    result = _make_engine().schedule_to_target()
    if result != []:
        print(f"ERROR: Expected [], got {result}")
        return True
    return False


def _test_schedule_contains_charge_and_export(my_predbat=None):
    """Each scheduled pair must produce one charge and one export slot."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[0] = 3.0
    rate_export[60] = 40.0
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    schedule = eng.schedule_to_target()
    types = {s["type"] for s in schedule}
    if "charge" not in types:
        print("ERROR: Schedule missing 'charge' slot")
        return True
    if "export" not in types:
        print("ERROR: Schedule missing 'export' slot")
        return True
    return False


def _test_schedule_slot_structure(my_predbat=None):
    """Each slot dict must have all required keys with correct types."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    failed = False
    for slot in eng.schedule_to_target():
        if not isinstance(slot.get("start"), int):
            print(f"ERROR: start missing or wrong type in {slot}")
            failed = True
        if not isinstance(slot.get("end"), int):
            print(f"ERROR: end missing or wrong type in {slot}")
            failed = True
        if slot.get("type") not in ("charge", "export"):
            print(f"ERROR: type invalid in {slot}")
            failed = True
        if not isinstance(slot.get("target_soc"), float):
            print(f"ERROR: target_soc missing or wrong type in {slot}")
            failed = True
        if slot.get("end", 0) <= slot.get("start", 0):
            print(f"ERROR: end <= start in {slot}")
            failed = True
    return failed


def _test_schedule_no_overlaps(my_predbat=None):
    """Slots in the schedule must not overlap each other."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=2.0)
    schedule = eng.schedule_to_target()
    for i, a in enumerate(schedule):
        for j, b in enumerate(schedule):
            if i >= j:
                continue
            overlap = a["start"] < b["end"] and b["start"] < a["end"]
            if overlap:
                print(f"ERROR: Slots overlap: {a} and {b}")
                return True
    return False


def _test_schedule_sorted_chronologically(my_predbat=None):
    """Schedule must be ordered by start time."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.50)
    schedule = eng.schedule_to_target()
    starts = [s["start"] for s in schedule]
    if starts != sorted(starts):
        print(f"ERROR: Schedule not sorted: {starts}")
        return True
    return False


def _test_schedule_meets_target(my_predbat=None):
    """With very profitable rates, projected_gain should approximately meet the target."""
    rate_import = {i: 1.0 for i in range(0, 1440)}
    rate_export = {i: 50.0 for i in range(0, 1440)}
    target = 0.50
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=target)
    # Use projected_gain() which uses raw (undiscounted) profit for monetary reporting
    gain = eng.projected_gain()
    if gain < target * 0.90:
        print(f"ERROR: projected_gain {gain:.3f} < 90% of target {target}")
        return True
    return False
```

Update the `sub_tests` list in `test_arbitrage()` to include these new tests.

- [ ] **Step 2: Run to verify the new tests fail (NotImplementedError)**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "schedule|RESULTS"
```
Expected: schedule tests show `EXCEPTION`

- [ ] **Step 3: Implement schedule_to_target() in arbitrage.py**

```python
def schedule_to_target(self) -> list[dict]:
    scored = self.score_slots()
    if not scored:
        return []

    slot_hours = SLOT_MINUTES / 60.0
    arb_capacity_kwh = self.battery_capacity_kwh * (self.arbitrage_reserve_percent / 100.0)
    soc_kwh = self.battery_capacity_kwh * (self.battery_soc_percent / 100.0)
    # Headroom within the arbitrage-reserved portion of the battery
    soc_in_arb_portion = min(soc_kwh, arb_capacity_kwh)
    available_charge_kwh = max(0.0, arb_capacity_kwh - soc_in_arb_portion)

    committed_minutes: set[int] = set()
    schedule: list[dict] = []
    accumulated_profit = 0.0

    for pair in scored:
        if accumulated_profit >= self.profit_target_daily:
            break

        charge_min = pair["charge_minute"]
        export_min = pair["export_minute"]
        charge_kwh = min(pair["charge_kwh"], available_charge_kwh)
        if charge_kwh <= 0:
            continue

        charge_range = set(range(charge_min, charge_min + SLOT_MINUTES))
        export_range = set(range(export_min, export_min + SLOT_MINUTES))
        if charge_range & committed_minutes or export_range & committed_minutes:
            continue

        discharge_kwh = charge_kwh * self.battery_efficiency
        import_cost = (self.rate_import[charge_min] / 100.0) * charge_kwh
        export_revenue = (self.rate_export[export_min] / 100.0) * discharge_kwh
        net_profit = export_revenue - import_cost

        soc_after_charge = min(
            100.0,
            ((soc_kwh + charge_kwh) / self.battery_capacity_kwh) * 100.0,
        )
        soc_after_discharge = max(
            0.0,
            soc_after_charge - (discharge_kwh / self.battery_capacity_kwh) * 100.0,
        )

        schedule.append({
            "start": charge_min,
            "end": charge_min + SLOT_MINUTES,
            "type": "charge",
            "target_soc": round(soc_after_charge, 1),
            "paired_export_minute": export_min,  # track pairing for projected_gain()
        })
        schedule.append({
            "start": export_min,
            "end": export_min + SLOT_MINUTES,
            "type": "export",
            "target_soc": round(soc_after_discharge, 1),
            "paired_charge_minute": charge_min,
        })

        committed_minutes |= charge_range
        committed_minutes |= export_range
        accumulated_profit += net_profit
        available_charge_kwh -= charge_kwh

    schedule.sort(key=lambda x: x["start"])
    return schedule
```

- [ ] **Step 4: Run all tests**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "PASSED|FAILED|RESULTS"
```
Expected: All score_slots and schedule_to_target tests PASSED

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/arbitrage.py apps/predbat/tests/test_arbitrage.py
git commit -m "feat(arbitrage): implement schedule_to_target() with tests"
```

---

## Chunk 4: Error Handling, Metrics & plan_constraints

### Task 5: Test & Implement Error Handling and Metrics

**Files:**
- Modify: `apps/predbat/tests/test_arbitrage.py`
- Modify: `apps/predbat/arbitrage.py`

- [ ] **Step 1: Add error handling and metrics tests**

```python
# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

def _test_error_empty_rate_import(my_predbat=None):
    result = _make_engine(rate_import={}).schedule_to_target()
    if result != []:
        print(f"ERROR: Expected [] with empty rate_import, got {result}")
        return True
    return False


def _test_error_empty_rate_export(my_predbat=None):
    result = _make_engine(rate_export={}).schedule_to_target()
    if result != []:
        print(f"ERROR: Expected [] with empty rate_export, got {result}")
        return True
    return False


def _test_error_partial_rates_no_exception(my_predbat=None):
    """Missing slots in rate dicts are skipped without error."""
    try:
        eng = _make_engine(rate_import={0: 3.0}, rate_export={60: 40.0}, profit_target_daily=0.01)
        result = eng.schedule_to_target()
        if not isinstance(result, list):
            print("ERROR: Expected list result")
            return True
    except Exception as e:
        print(f"ERROR: Unexpected exception with partial rates: {e}")
        return True
    return False


def _test_error_unachievable_target_no_exception(my_predbat=None):
    """Unachievable target returns best-effort schedule without raising."""
    rate_import = {i: 14.9 for i in range(0, 1440)}
    rate_export = {i: 15.1 for i in range(0, 1440)}
    try:
        result = _make_engine(rate_import=rate_import, rate_export=rate_export,
                              profit_target_daily=1000.0).schedule_to_target()
        if not isinstance(result, list):
            print("ERROR: Expected list")
            return True
    except Exception as e:
        print(f"ERROR: Unexpected exception: {e}")
        return True
    return False


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------

def _test_metrics_projected_gain_zero_no_spread(my_predbat=None):
    gain = _make_engine().projected_gain()
    if gain != 0.0:
        print(f"ERROR: Expected 0.0, got {gain}")
        return True
    return False


def _test_metrics_projected_gain_positive_with_spread(my_predbat=None):
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    gain = _make_engine(rate_import=rate_import, rate_export=rate_export).projected_gain()
    if gain <= 0:
        print(f"ERROR: Expected positive gain, got {gain}")
        return True
    return False


def _test_metrics_opportunity_score_zero_no_spread(my_predbat=None):
    score = _make_engine().opportunity_score()
    if score != 0:
        print(f"ERROR: Expected 0, got {score}")
        return True
    return False


def _test_metrics_opportunity_score_bounded(my_predbat=None):
    rate_import = {i: 0.1 for i in range(0, 1440)}
    rate_export = {i: 100.0 for i in range(0, 1440)}
    score = _make_engine(rate_import=rate_import, rate_export=rate_export).opportunity_score()
    if not (0 <= score <= 100):
        print(f"ERROR: Score {score} out of range 0-100")
        return True
    return False
```

Register all new tests in `test_arbitrage()`.

- [ ] **Step 2: Run to verify failures**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "error|metrics|RESULTS"
```

- [ ] **Step 3: Implement projected_gain() and opportunity_score()**

Note: `projected_gain()` reports the **true expected monetary gain** (not confidence-discounted). Confidence discounting affects which slot pairs are selected (by their relative ranking) but the gain reported to the user should reflect the actual expected £ value, not a planning-weight.

Also add a private helper `_score_slots_raw()` that returns raw (undiscounted) profit for monetary reporting, keeping `score_slots()` (used for scheduling) unchanged:

```python
def _slot_pairs_raw(self) -> list[dict]:
    """Raw slot pairs with true monetary profit (no confidence discount).

    Used for monetary reporting only — not for scheduling decisions.
    """
    pairs = []
    slot_hours = SLOT_MINUTES / 60.0
    future_slots = [
        m for m in range(self.minutes_now, 1440, SLOT_MINUTES)
        if m in self.rate_import
    ]
    for charge_minute in future_slots:
        charge_kwh = self.charge_rate_kw * slot_hours
        import_cost = (self.rate_import[charge_minute] / 100.0) * charge_kwh
        for export_minute in future_slots:
            if export_minute <= charge_minute:
                continue
            if export_minute not in self.rate_export:
                continue
            discharge_kwh = charge_kwh * self.battery_efficiency
            export_revenue = (self.rate_export[export_minute] / 100.0) * discharge_kwh
            raw_profit = export_revenue - import_cost
            if raw_profit > 0:
                pairs.append({
                    "charge_minute": charge_minute,
                    "export_minute": export_minute,
                    "raw_profit_gbp": round(raw_profit, 4),
                })
    return pairs

def projected_gain(self) -> float:
    """Return projected arbitrage profit for today in £ (true monetary value).

    Uses the 'paired_export_minute' field written by schedule_to_target() to
    correctly identify which export slot was paired with each charge slot.
    """
    schedule = self.schedule_to_target()
    raw_pairs = self._slot_pairs_raw()
    raw_lookup = {(p["charge_minute"], p["export_minute"]): p["raw_profit_gbp"] for p in raw_pairs}

    total = 0.0
    for slot in schedule:
        if slot["type"] == "charge":
            key = (slot["start"], slot["paired_export_minute"])
            total += raw_lookup.get(key, 0.0)
    return round(total, 2)

def opportunity_score(self) -> int:
    """Return 0-100 score representing current arbitrage opportunity quality.

    Returns 0 if profit_target_daily is 0 (avoid division by zero).
    """
    if self.profit_target_daily <= 0:
        return 0
    gain = self.projected_gain()
    return min(100, int((gain / self.profit_target_daily) * 100))
```

- [ ] **Step 4: Run all tests**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "PASSED|FAILED|RESULTS"
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/arbitrage.py apps/predbat/tests/test_arbitrage.py
git commit -m "feat(arbitrage): implement projected_gain, opportunity_score, error handling"
```

---

### Task 6: Test & Implement plan_constraints()

**Files:**
- Modify: `apps/predbat/tests/test_arbitrage.py`
- Modify: `apps/predbat/arbitrage.py`

- [ ] **Step 1: Add plan_constraints tests**

```python
# ---------------------------------------------------------------------------
# plan_constraints() tests
# ---------------------------------------------------------------------------

def _test_constraints_returns_list(my_predbat=None):
    result = _make_engine().plan_constraints()
    if not isinstance(result, list):
        print(f"ERROR: Expected list, got {type(result)}")
        return True
    return False


def _test_constraints_empty_no_spread(my_predbat=None):
    if _make_engine().plan_constraints() != []:
        print("ERROR: Expected [] with no spread")
        return True
    return False


def _test_constraints_structure(my_predbat=None):
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    constraints = eng.plan_constraints()
    if not constraints:
        print("ERROR: Expected at least one constraint")
        return True
    failed = False
    for c in constraints:
        for key in ("start", "end", "average", "min", "max", "constraint_type"):
            if key not in c:
                print(f"ERROR: Missing key '{key}' in constraint {c}")
                failed = True
        if c.get("constraint_type") not in ("charge", "export"):
            print(f"ERROR: Invalid constraint_type: {c.get('constraint_type')}")
            failed = True
        if c.get("end", 0) <= c.get("start", 0):
            print(f"ERROR: end <= start in {c}")
            failed = True
    return failed


def _test_constraints_charge_uses_import_rate(my_predbat=None):
    rate_import = {i: 7.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    charge_cs = [c for c in eng.plan_constraints() if c["constraint_type"] == "charge"]
    if not charge_cs:
        print("ERROR: No charge constraints found")
        return True
    if charge_cs[0]["average"] != 7.0:
        print(f"ERROR: Charge constraint average should be 7.0, got {charge_cs[0]['average']}")
        return True
    return False


def _test_constraints_export_uses_export_rate(my_predbat=None):
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    export_cs = [c for c in eng.plan_constraints() if c["constraint_type"] == "export"]
    if not export_cs:
        print("ERROR: No export constraints found")
        return True
    if export_cs[0]["average"] != 40.0:
        print(f"ERROR: Export constraint average should be 40.0, got {export_cs[0]['average']}")
        return True
    return False
```

Register these in `test_arbitrage()`.

- [ ] **Step 2: Run to verify failures**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "constraint|RESULTS"
```

- [ ] **Step 3: Implement plan_constraints() in arbitrage.py**

```python
def plan_constraints(self) -> list[dict]:
    schedule = self.schedule_to_target()
    constraints = []
    for slot in schedule:
        rate_dict = self.rate_import if slot["type"] == "charge" else self.rate_export
        average_rate = float(rate_dict.get(slot["start"], 0.0))
        constraints.append({
            "start": slot["start"],
            "end": slot["end"],
            "average": average_rate,
            "min": 0,
            "max": slot["target_soc"],
            "constraint_type": slot["type"],
        })
    return constraints
```

- [ ] **Step 4: Run all tests**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | grep -E "PASSED|FAILED|RESULTS"
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/arbitrage.py apps/predbat/tests/test_arbitrage.py
git commit -m "feat(arbitrage): implement plan_constraints() with tests"
```

---

## Chunk 5: Wiring into Predbat Core

### Task 7: Publish Arbitrage HA Entities in output.py

**Files:**
- Modify: `apps/predbat/output.py`

- [ ] **Step 1: Find the entity publication pattern**

```bash
grep -n "binary_sensor.predbat_charging\|binary_sensor.predbat_exporting" \
  /Users/ian/projects/batpred/apps/predbat/output.py | head -5
```

Read the lines around the match to understand the exact method signature used.

- [ ] **Step 2: Find where main sensors are published**

```bash
grep -n "predbat_status\|predbat_cost_today" \
  /Users/ian/projects/batpred/apps/predbat/output.py | head -5
```

Add the arbitrage entities in the same section.

- [ ] **Step 3: Add arbitrage entity publication**

The method in `output.py` is `self.dashboard_item(entity, state, attributes)` where `state` is always a string. Follow the exact pattern found in steps 1-2. Do not copy the block below verbatim — verify the attribute dict keys (e.g. `unit_of_measurement`, `icon`, `state_class`) from a nearby example first.

```python
# Arbitrage entities — only when arbitrage_enable is True
if self.get_arg("arbitrage_enable", False):
    self.dashboard_item(
        "sensor.predbat_arbitrage_projected_gain",
        state=str(round(getattr(self, "arbitrage_projected_gain", 0.0), 2)),
        attributes={"unit_of_measurement": "£", "icon": "mdi:currency-gbp", "state_class": "measurement"},
    )
    self.dashboard_item(
        "sensor.predbat_arbitrage_opportunity_score",
        state=str(getattr(self, "arbitrage_opportunity_score", 0)),
        attributes={"icon": "mdi:chart-line", "state_class": "measurement"},
    )
    self.dashboard_item(
        "sensor.predbat_arbitrage_weekly_gain",
        state=str(round(getattr(self, "arbitrage_weekly_gain", 0.0), 2)),
        attributes={"unit_of_measurement": "£", "icon": "mdi:currency-gbp", "state_class": "total"},
    )
    self.dashboard_item(
        "binary_sensor.predbat_arbitrage_active",
        state="on" if getattr(self, "arbitrage_active", False) else "off",
        attributes={},
    )
```

**Adapt** the method name and attribute keys to exactly match the pattern in steps 1-2.

- [ ] **Step 4: Verify syntax**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 -c "import output; print('output.py OK')"
```

- [ ] **Step 5: Run full test suite**

```bash
python3 unit_test.py --quick 2>&1 | tail -10
```
Expected: no new failures

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/output.py
git commit -m "feat(arbitrage): publish arbitrage HA entities in output.py"
```

---

### Task 8: Wire ArbitrageEngine into plan.py

**Files:**
- Modify: `apps/predbat/plan.py`

- [ ] **Step 1: Find the correct attribute names for battery/rate state**

```bash
grep -n "self\.soc_percent\|self\.soc_max\|self\.charge_rate_now\|self\.discharge_rate_now\|self\.battery_loss\b\|self\.manual_all_times\|self\.pv_forecast_minute\|self\.load_minutes_step" \
  /Users/ian/projects/batpred/apps/predbat/plan.py | head -20
```

Note the exact names. If any differ from what's shown below, use the correct names.

- [ ] **Step 2: Understand manual_all_times usage**

```bash
grep -n "manual_all_times" /Users/ian/projects/batpred/apps/predbat/plan.py | head -10
```

Confirm how entries are added to `manual_all_times` (dict or set). Check `test_manual_times.py` if unclear.

- [ ] **Step 3: Find the main plan calculation entry point**

```bash
grep -n "^    def optimise\|^    def calculate\|^    def run_" \
  /Users/ian/projects/batpred/apps/predbat/plan.py | head -10
```

Find the function that starts the main plan calculation. Add the arbitrage injection near its beginning, before the main optimisation loop.

- [ ] **Step 4: Add arbitrage injection block**

Use the exact attribute names found in steps 1 and 2. The values below are correct intent but names **must** be verified before use:

```python
# Arbitrage: inject pre-committed charge/export constraints when enabled
if self.get_arg("arbitrage_enable", False):
    from arbitrage import ArbitrageEngine

    # charge_rate_now is stored in kWh/min — multiply by 60 to get kW
    # Verify attribute names with grep in step 1 before using
    _soc_max = self.soc_max  # capacity in kWh — verify name
    _soc_kw = self.soc_kw   # current SoC in kWh — verify name (NOT soc_percent)
    _soc_percent = (_soc_kw / _soc_max * 100.0) if _soc_max > 0 else 0.0

    # Round-trip efficiency: combine charge loss, discharge loss, and inverter loss
    # Verify attribute names for battery_loss, battery_loss_discharge, inverter_loss
    _round_trip_eff = (1.0 - self.battery_loss) * (1.0 - self.battery_loss_discharge) * (1.0 - getattr(self, "inverter_loss", 0.0))

    _arb = ArbitrageEngine(
        rate_import=self.rate_import,
        rate_export=self.rate_export,
        solar_forecast=getattr(self, "pv_forecast_minute_step", {}),
        load_forecast=getattr(self, "load_minutes_step", {}),
        battery_soc_percent=_soc_percent,
        battery_capacity_kwh=_soc_max,
        charge_rate_kw=self.charge_rate_now * 60,       # convert kWh/min → kW
        discharge_rate_kw=self.discharge_rate_now * 60, # convert kWh/min → kW
        battery_efficiency=_round_trip_eff,
        profit_target_daily=self.get_arg("arbitrage_profit_target_daily", 1.0),
        arbitrage_reserve_percent=self.get_arg("arbitrage_reserve", 20.0),
        minutes_now=self.minutes_now,
    )
    _arb_constraints = _arb.plan_constraints()

    self.arbitrage_projected_gain = _arb.projected_gain()
    self.arbitrage_opportunity_score = _arb.opportunity_score()
    self.arbitrage_active = any(
        c["constraint_type"] == "export"
        and c["start"] <= self.minutes_now < c["end"]
        for c in _arb_constraints
    )

    # manual_all_times is a list of integer minutes — use append(), NOT dict assignment
    # This prevents the optimiser from removing these windows.
    # The constraints also need to be added to charge_window_best/export_window_best
    # so the optimiser knows to create the actual charge/export windows at those times.
    # Step 5 below covers verifying the correct window-insertion pattern.
    for c in _arb_constraints:
        self.manual_all_times.append(c["start"])
else:
    self.arbitrage_projected_gain = 0.0
    self.arbitrage_opportunity_score = 0
    self.arbitrage_active = False
```

- [ ] **Step 5: Verify and implement window creation**

`manual_all_times` prevents the optimiser from *removing* those windows, but the charge/export windows themselves must also exist in `charge_window_best`/`export_window_best`. Find how existing manual overrides from the web UI create windows in `plan.py` (grep for `charge_window_best.append` or `manual_charge_times`), then add equivalent logic to insert each `_arb_constraints` entry as a properly-formed window dict. A charge constraint becomes a `charge_window_best` entry; an export constraint becomes an `export_window_best` entry.

- [ ] **Step 6: Run the full test suite**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | tail -20
```
Expected: All pre-existing tests and all arbitrage tests PASS. Zero new failures.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/plan.py
git commit -m "feat(arbitrage): wire ArbitrageEngine into plan.py optimisation loop"
```

---

### Task 9: Wire Weekly Gain Persistence

**Files:**
- Modify: `apps/predbat/plan.py` (or whichever file handles end-of-run accounting)

- [ ] **Step 1: Find the db_manager pattern**

```bash
grep -n "db_manager\|set_state_db\|get_history_db\|carbon_history\|soc_kwh_history" \
  /Users/ian/projects/batpred/apps/predbat/plan.py \
  /Users/ian/projects/batpred/apps/predbat/output.py | head -20
```

Understand how Predbat stores rolling daily totals. Follow the exact pattern used for similar metrics (e.g. `carbon_history`).

- [ ] **Step 2: Store daily gain and compute rolling 7-day total**

After computing `self.arbitrage_projected_gain`, persist today's gain and sum the last 7 days. Use `db_manager.py`'s `set_state_db`/`get_history_db` methods following the pattern found in step 1. Store under key `predbat_arbitrage_daily_gain`. Assign the 7-day sum to `self.arbitrage_weekly_gain`.

- [ ] **Step 3: Run full test suite**

```bash
cd /Users/ian/projects/batpred/apps/predbat
python3 unit_test.py --quick 2>&1 | tail -10
```
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add apps/predbat/plan.py
git commit -m "feat(arbitrage): persist weekly arbitrage gain via db_manager"
```

---

## Post-Implementation Checklist

- [ ] Run full test suite: `python3 unit_test.py --quick 2>&1 | tail -5`
- [ ] Set `arbitrage_enable: true` and `arbitrage_profit_target_daily: 1.0` in `apps.yaml`, restart Predbat, confirm all 4 entities appear in the web UI entities view
- [ ] Verify `predbat_arbitrage_opportunity_score` is non-zero with live Agile rates loaded

---

## Notes for Plan 2 (Companion Integration)

Plan 2 (`custom_components/predbat_config`) lives in a separate GitHub repository. Start it after this plan is complete. The following `apps.yaml` keys established in this plan must be included in Plan 2's Options Flow Arbitrage section:

- `arbitrage_enable` (switch)
- `arbitrage_profit_target_daily` (input_number, £, step 0.10)
- `arbitrage_profit_target_weekly` (input_number, £, step 1.0)
- `arbitrage_reserve` (input_number, %, step 5)
