# pv90 Upside Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third simulated forecast scenario (`pv90`: high PV, low load) so Predbat prices the cost of an unnecessary hedge, not just the risk of a bad solar day.

**Architecture:** The boolean `pv10` flag threaded through the prediction stack becomes a three-valued `pv_scenario` integer. A new p90 PV series and a `load_scaling90` load series feed the third scenario. `compute_metric` replaces its downside-only clamp with a signed weighted average across nominal, pv10 and pv90.

**Tech Stack:** Python 3, `multiprocessing.Pool`, ctypes-bound C++ prediction kernel (`prediction_kernel.cpp` + 6 prebuilt `.so` per platform), custom test harness in `apps/predbat/unit_test.py`.

**Spec:** `docs/superpowers/specs/2026-08-08-pv90-upside-scenario-design.md`

## Global Constraints

- Line length: 256 chars (Black), 250 (Flake8). Run `./run_pre_commit` before finishing.
- Docstrings: 100% coverage required (`interrogate`) — every new function AND class needs one.
- Spell checking: British English (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit — re-stage after running pre-commit.
- Variable naming: `lower_case_with_underscores`.
- Tests run from `coverage/`: `cd coverage && ./run_all --test <name>`. Test output is large — **always redirect to a file and grep it afterwards**, never pipe straight to grep.
- Unit tests are required for all new code.
- The C++ kernel mirrors `Prediction.run_prediction`. Any behavioural change to that hot loop MUST be mirrored in `prediction_kernel.cpp`, with `KERNEL_PARITY_REVISION` (prediction_kernel.py) and `PK_PARITY_REVISION` (.cpp) both bumped, and `./run_all --test kernel_parity` passing.
- Work on branch `feat/pv90-upside-scenario`. Do not commit to `main`.
- `pv_metric90_weight` ships with default **0** for the whole of this plan. It is only raised to 0.1 in Task 9, and only if Task 8 justifies it.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/predbat/const.py` | Shared constants | Add three `PV_SCENARIO_*` constants |
| `apps/predbat/config.py` | `CONFIG_ITEMS` definitions | Add `pv_metric90_weight`, `load_scaling90` |
| `apps/predbat/fetch.py` | Data acquisition | `fetch_pv_forecast()` returns a third dict; read the two new config options |
| `apps/predbat/solcast.py` | Solcast source | Build/return/publish the real p90 minute series |
| `apps/predbat/plan.py` | Optimiser | Build p90 step arrays; thread `pv_scenario`; blend in `compute_metric`; launch pv90 sims |
| `apps/predbat/prediction.py` | Simulation engine | Accept p90 arrays; select series by `pv_scenario` |
| `apps/predbat/prediction_kernel.py` | ctypes binding | Two new context arrays; ABI 2→3; scenario passthrough |
| `apps/predbat/prediction_kernel.cpp` | C++ hot loop | Struct fields; three-way series select |
| `apps/predbat/tests/test_pv90.py` | **New** — all pv90 unit tests | Create |
| `apps/predbat/tests/test_kernel_parity.py` | Kernel parity | Add pv90 coverage |
| `apps/predbat/unit_test.py` | Test registry | Register `pv90` |

---

### Task 1: Scenario constants and configuration options

Foundation only — no behaviour change. After this task Predbat runs exactly as before.

**Files:**
- Modify: `apps/predbat/const.py`
- Modify: `apps/predbat/config.py:94-116`
- Modify: `apps/predbat/fetch.py:2349-2356`
- Create: `apps/predbat/tests/test_pv90.py`
- Modify: `apps/predbat/unit_test.py:259+` (TEST_REGISTRY) and imports

**Interfaces:**
- Consumes: nothing
- Produces:
  - `const.PV_SCENARIO_NOMINAL = 0`, `const.PV_SCENARIO_PV10 = 1`, `const.PV_SCENARIO_PV90 = 2`
  - `PredBat.pv_metric90_weight: float` (default 0.0)
  - `PredBat.load_scaling90: float` (default 0.9)
  - `tests/test_pv90.py::run_pv90_tests(my_predbat) -> bool` (True == failed, matching the suite convention)

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_pv90.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the pv90 upside forecast scenario."""

from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90


def test_pv90_scenario_constants():
    """The three scenario ids must be distinct, and PV10 must stay == 1 for bool compatibility."""
    failed = False
    if PV_SCENARIO_NOMINAL != 0:
        print("ERROR: PV_SCENARIO_NOMINAL is {}, expected 0".format(PV_SCENARIO_NOMINAL))
        failed = True
    if PV_SCENARIO_PV10 != 1:
        print("ERROR: PV_SCENARIO_PV10 is {}, expected 1 (must stay truthy-compatible with the old bool)".format(PV_SCENARIO_PV10))
        failed = True
    if PV_SCENARIO_PV90 != 2:
        print("ERROR: PV_SCENARIO_PV90 is {}, expected 2".format(PV_SCENARIO_PV90))
        failed = True
    return failed


def test_pv90_config_items(my_predbat):
    """pv_metric90_weight and load_scaling90 must exist, be expert-gated and carry the documented defaults."""
    failed = False
    expected = {
        "pv_metric90_weight": {"default": 0.0, "min": 0, "max": 1.0, "step": 0.01},
        "load_scaling90": {"default": 0.9, "min": 0, "max": 2.0, "step": 0.01},
    }
    by_name = {item["name"]: item for item in my_predbat.CONFIG_ITEMS}
    for name, want in expected.items():
        item = by_name.get(name)
        if not item:
            print("ERROR: config item {} is missing from CONFIG_ITEMS".format(name))
            failed = True
            continue
        if item.get("type") != "input_number":
            print("ERROR: config item {} type is {}, expected input_number".format(name, item.get("type")))
            failed = True
        if item.get("enable") != "expert_mode":
            print("ERROR: config item {} enable is {}, expected expert_mode".format(name, item.get("enable")))
            failed = True
        for key, value in want.items():
            if item.get(key) != value:
                print("ERROR: config item {} {} is {}, expected {}".format(name, key, item.get(key), value))
                failed = True
    return failed


def test_pv90_config_read(my_predbat):
    """fetch_config_options must populate the two new attributes."""
    failed = False
    for name, default in (("pv_metric90_weight", 0.0), ("load_scaling90", 0.9)):
        if not hasattr(my_predbat, name):
            print("ERROR: my_predbat has no attribute {} - fetch_config_options did not read it".format(name))
            failed = True
            continue
        value = getattr(my_predbat, name)
        if value != default:
            print("ERROR: {} is {}, expected the default {}".format(name, value, default))
            failed = True
    return failed


def run_pv90_tests(my_predbat):
    """Run all pv90 tests, returning True if any failed."""
    failed = False
    print("**** Running pv90 tests ****")
    failed |= test_pv90_scenario_constants()
    failed |= test_pv90_config_items(my_predbat)
    failed |= test_pv90_config_read(my_predbat)
    return failed
```

Register it in `apps/predbat/unit_test.py` — add the import next to the other test imports:

```python
from tests.test_pv90 import run_pv90_tests
```

and add to `TEST_REGISTRY` (near the `compute_metric` entry):

```python
        ("pv90", run_pv90_tests, "pv90 upside scenario tests", False),
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|ImportError|Traceback|FAIL|PASS" /tmp/pv90.txt
```

Expected: FAIL — `ImportError: cannot import name 'PV_SCENARIO_NOMINAL' from 'const'`.

- [ ] **Step 3: Add the constants**

In `apps/predbat/const.py`, next to `PREDICT_STEP`:

```python
# Forecast scenarios simulated by the planner.
# PV_SCENARIO_PV10 must remain 1 so it stays interchangeable with the legacy pv10 boolean.
PV_SCENARIO_NOMINAL = 0
PV_SCENARIO_PV10 = 1
PV_SCENARIO_PV90 = 2
```

- [ ] **Step 4: Add the config items**

In `apps/predbat/config.py`, immediately after the `charge_scaling10` entry (which ends around line 116):

```python
    {
        "name": "pv_metric90_weight",
        "friendly_name": "Metric 90 Weight",
        "type": "input_number",
        "min": 0,
        "max": 1.0,
        "step": 0.01,
        "unit": "*",
        "icon": "mdi:multiplication",
        "default": 0.0,
        "enable": "expert_mode",
    },
    {
        "name": "load_scaling90",
        "friendly_name": "Load Scaling PV90%",
        "type": "input_number",
        "min": 0,
        "max": 2.0,
        "step": 0.01,
        "unit": "*",
        "icon": "mdi:multiplication",
        "default": 0.9,
        "enable": "expert_mode",
    },
```

- [ ] **Step 5: Read them in fetch_config_options**

In `apps/predbat/fetch.py`, alongside the existing pv10 reads at ~line 2352:

```python
        self.pv_metric90_weight = self.get_arg("pv_metric90_weight")
        self.load_scaling90 = self.get_arg("load_scaling90")
```

Also add safe defaults in `apps/predbat/predbat.py` next to `self.metric_battery_value_scaling = 1.0` (~line 376), so code paths that never call `fetch_config_options` still work:

```python
        self.pv_metric90_weight = 0.0
        self.load_scaling90 = 0.9
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS, no ERROR lines.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/const.py apps/predbat/config.py apps/predbat/fetch.py apps/predbat/predbat.py apps/predbat/tests/test_pv90.py apps/predbat/unit_test.py
git commit -m "feat(pv90): add scenario constants and pv_metric90_weight/load_scaling90 config"
```

---

### Task 2: p90 PV data pipeline

Produce a `pv_forecast_minute90` series everywhere `pv_forecast_minute10` exists. Real p90 from Solcast, p50 copy as fallback.

**Files:**
- Modify: `apps/predbat/fetch.py:1292-1345` (`fetch_pv_forecast`), `fetch.py:1063`
- Modify: `apps/predbat/solcast.py:850-1226` (`pv_calibration`), `1228-1262` (`pack_and_store_forecast`), `1398-1413` (caller)
- Modify: `apps/predbat/tests/test_pv90.py`

**Interfaces:**
- Consumes: Task 1 constants (not used here, but the test file exists)
- Produces:
  - `Fetch.fetch_pv_forecast() -> (dict, dict, dict)` — `(pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90)`
  - `PredBat.pv_forecast_minute90: dict` — minute (int) → kWh (float), always populated
  - `Solcast.pv_calibration(...) -> (dict, dict, dict, list)` — now returns `pv_forecast_minute90` third
  - `Solcast.pack_and_store_forecast(pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90)`

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_pv90.py`:

```python
def test_pv90_forecast_fallback_to_p50(my_predbat):
    """With no forecast90 attribute published, pv_forecast_minute90 must be a copy of the p50 series."""
    failed = False
    my_predbat.dashboard_item(
        "sensor." + my_predbat.prefix + "_pv_forecast_raw",
        state=0,
        attributes={
            "relative_time": my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "forecast": {"0": 0.01, "60": 0.02},
            "forecast10": {"0": 0.005, "60": 0.01},
        },
    )
    pv50, pv10, pv90 = my_predbat.fetch_pv_forecast()
    if not pv50:
        print("ERROR: p50 series is empty, test setup failed")
        return True
    for minute, value in pv50.items():
        if pv90.get(minute) != value:
            print("ERROR: pv90[{}] is {}, expected the p50 value {} (fallback must copy p50)".format(minute, pv90.get(minute), value))
            failed = True
            break
    if pv90 is pv50:
        print("ERROR: pv90 is the same object as pv50 - must be a copy so later scaling cannot alias")
        failed = True
    return failed


def test_pv90_forecast_uses_published_p90(my_predbat):
    """When forecast90 is published it must be used verbatim, not the p50 fallback."""
    failed = False
    my_predbat.dashboard_item(
        "sensor." + my_predbat.prefix + "_pv_forecast_raw",
        state=0,
        attributes={
            "relative_time": my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "forecast": {"0": 0.01, "60": 0.02},
            "forecast10": {"0": 0.005, "60": 0.01},
            "forecast90": {"0": 0.03, "60": 0.04},
        },
    )
    pv50, pv10, pv90 = my_predbat.fetch_pv_forecast()
    if pv90.get(0) != 0.03:
        print("ERROR: pv90[0] is {}, expected the published 0.03".format(pv90.get(0)))
        failed = True
    if pv90.get(60) != 0.04:
        print("ERROR: pv90[60] is {}, expected the published 0.04".format(pv90.get(60)))
        failed = True
    if pv90.get(0) == pv50.get(0):
        print("ERROR: pv90 fell back to p50 despite forecast90 being published")
        failed = True
    return failed
```

Add both to `run_pv90_tests`:

```python
    failed |= test_pv90_forecast_fallback_to_p50(my_predbat)
    failed |= test_pv90_forecast_uses_published_p90(my_predbat)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|ValueError" /tmp/pv90.txt
```

Expected: FAIL — `ValueError: not enough values to unpack (expected 3, got 2)`.

- [ ] **Step 3: Extend fetch_pv_forecast**

In `apps/predbat/fetch.py`, replace the body of `fetch_pv_forecast()` (lines 1292-1345). The three changed regions:

```python
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        pv_forecast_minute90 = {}
```

```python
        pv_forecast90_packed_ld = self.get_state_wrapper(entity_id=entity_id, attribute="forecast90")
```

```python
        pv_forecast90_packed = {}

        if pv_forecast90_packed_ld:
            for key, value in pv_forecast90_packed_ld.items():
                try:
                    minute = int(key)
                    pv_forecast90_packed[minute] = float(value)
                except (ValueError, TypeError):
                    pass
```

In the unpack loop, add a third running value and populate the third dict:

```python
        last_value90 = 0
        ...
        for minute in range(0, max_minute + 1):
            target_minute = minute - minute_offset
            last_value = pv_forecast_packed.get(minute, last_value)
            last_value10 = pv_forecast10_packed.get(minute, last_value10)
            last_value90 = pv_forecast90_packed.get(minute, last_value90)
            pv_forecast_minute[target_minute] = last_value
            pv_forecast_minute10[target_minute] = last_value10
            pv_forecast_minute90[target_minute] = last_value90
```

Then before returning, apply the fallback:

```python
        # No p90 published (older sensor data, or a source that does not produce one) - fall back to
        # the central forecast. No upside is synthesised; see the design spec for why mirroring the
        # p10 spread was rejected.
        if not pv_forecast90_packed:
            pv_forecast_minute90 = dict(pv_forecast_minute)

        return pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90
```

Update the docstring to mention the third return value.

- [ ] **Step 4: Update the caller**

In `apps/predbat/fetch.py:1063`:

```python
        self.pv_forecast_minute, self.pv_forecast_minute10, self.pv_forecast_minute90 = self.fetch_pv_forecast()
```

Add the initialiser next to `self.pv_forecast_minute10 = {}` at `fetch.py:732`:

```python
        self.pv_forecast_minute90 = {}
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS.

- [ ] **Step 6: Publish the real p90 from Solcast**

In `apps/predbat/solcast.py`, in the fetch path around line 1398, build the p90 minute series after `pv_forecast_minute10`:

```python
            # Solcast publishes a real p90; only build the series when at least one entry carries it,
            # otherwise leave it empty so the p50 fallback below applies.
            has_p90 = any("pv_estimate90" in entry for entry in pv_forecast_data)
            if has_p90:
                pv_forecast_minute90, _ = minute_data(
                    pv_forecast_data,
                    self.forecast_days,
                    self.midnight_utc,
                    "pv_estimate90",
                    "period_start",
                    backwards=False,
                    divide_by=divide_by,
                    scale=self.pv_scaling,
                    spreading=period,
                )
            else:
                pv_forecast_minute90 = dict(pv_forecast_minute)
```

Change `pv_calibration`'s signature to take and return the p90 series:

```python
    def pv_calibration(self, pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90, pv_forecast_data, create_pv10, divide_by, max_kwh, forecast_days, period=None):
```

In the calibrated branch (which currently builds `pv_forecast_minute10` from `worst_day_scaling` around line 1216-1219), build the p90 counterpart from `best_day_scaling`:

```python
            for minute in range(0, max(pv_forecast_minute_adjusted.keys()) + 1):
                pv_value = pv_forecast_minute_adjusted.get(minute, 0)
                pv_forecast_minute10[minute] = dp4(pv_value * worst_day_scaling)
                pv_forecast_minute90[minute] = dp4(pv_value * best_day_scaling)
```

Both return statements gain the third value:

```python
            return pv_forecast_minute_adjusted, pv_forecast_minute10, pv_forecast_minute90, pv_forecast_data
        else:
            return pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90, pv_forecast_data
```

Update `pack_and_store_forecast` to accept and publish it:

```python
    def pack_and_store_forecast(self, pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90):
        pv_forecast_pack = {}
        pv_forecast_pack10 = {}
        pv_forecast_pack90 = {}

        prev_value = -1
        prev_value10 = -1
        prev_value90 = -1
```

In its packing loop add:

```python
            current_value90 = dp4(pv_forecast_minute90.get(minute, 0))
            if current_value90 != prev_value90:
                pv_forecast_pack90[minute] = current_value90
                prev_value90 = current_value90
```

and in the `attributes` dict, after `"forecast10": pv_forecast_pack10,`:

```python
                "forecast90": pv_forecast_pack90,
```

Update the two call sites at solcast.py:1411-1413:

```python
            pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90, pv_forecast_data = self.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90, pv_forecast_data, create_pv10, divide_by / period, max_kwh, self.forecast_days, period)
            self.publish_pv_stats(pv_forecast_data, divide_by / period, period)
            self.pack_and_store_forecast(pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90)
```

- [ ] **Step 7: Run the solcast and fetch suites**

```bash
cd coverage && ./run_all --test pv90 --test solcast --test fetch_pv_forecast > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS. If `test_solcast.py` asserts on the exact `pack_and_store_forecast` argument count, update those assertions.

- [ ] **Step 8: Commit**

```bash
git add apps/predbat/fetch.py apps/predbat/solcast.py apps/predbat/tests/test_pv90.py
git commit -m "feat(pv90): build and publish a p90 PV forecast series with p50 fallback"
```

---

### Task 3: p90 step arrays

**Files:**
- Modify: `apps/predbat/plan.py:1135-1142`
- Modify: `apps/predbat/tests/test_pv90.py`

**Interfaces:**
- Consumes: `PredBat.pv_forecast_minute90` (Task 2), `PredBat.load_scaling90` (Task 1)
- Produces: `PredBat.pv_forecast_minute90_step: dict`, `PredBat.load_minutes_step90: dict` — both keyed by minute-from-now in `PREDICT_STEP` increments, matching the existing `*_step` arrays

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_pv90.py`:

```python
def test_pv90_step_arrays_built(my_predbat):
    """calculate_plan must build p90 step arrays with the same keys as the nominal ones."""
    failed = False
    if not hasattr(my_predbat, "pv_forecast_minute90_step"):
        print("ERROR: pv_forecast_minute90_step was not built")
        return True
    if not hasattr(my_predbat, "load_minutes_step90"):
        print("ERROR: load_minutes_step90 was not built")
        return True
    if set(my_predbat.pv_forecast_minute90_step.keys()) != set(my_predbat.pv_forecast_minute_step.keys()):
        print("ERROR: pv_forecast_minute90_step keys differ from pv_forecast_minute_step keys")
        failed = True
    if set(my_predbat.load_minutes_step90.keys()) != set(my_predbat.load_minutes_step.keys()):
        print("ERROR: load_minutes_step90 keys differ from load_minutes_step keys")
        failed = True
    total = sum(my_predbat.load_minutes_step.values())
    total90 = sum(my_predbat.load_minutes_step90.values())
    if total > 0 and total90 >= total:
        print("ERROR: load_minutes_step90 total {} is not below the nominal {} - load_scaling90 was not applied".format(total90, total))
        failed = True
    return failed
```

```python
def test_pv90_missing_series_does_not_crash(my_predbat):
    """A replayed debug dump has no pv_forecast_minute90; the plan must still build its step array."""
    failed = False
    saved = my_predbat.pv_forecast_minute90
    my_predbat.pv_forecast_minute90 = {}
    try:
        my_predbat.calculate_plan(recompute=True)
    except KeyError as error:
        print("ERROR: calculate_plan raised KeyError {} with an empty pv_forecast_minute90 - the p50 guard is missing".format(error))
        failed = True
    finally:
        my_predbat.pv_forecast_minute90 = saved
    return failed
```

Add both to `run_pv90_tests`, noting they need a plan to have run first:

```python
    my_predbat.calculate_plan(recompute=True)
    failed |= test_pv90_step_arrays_built(my_predbat)
    failed |= test_pv90_missing_series_does_not_crash(my_predbat)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback" /tmp/pv90.txt
```

Expected: FAIL — `ERROR: pv_forecast_minute90_step was not built`.

- [ ] **Step 3: Build the arrays**

In `apps/predbat/plan.py`, after the existing `load_minutes_step10` block (~line 1134) add:

```python
        load_minutes_step90 = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=self.load_scaling90,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=self.metric_load_divergence,
            load_adjust=self.manual_load_adjust,
            load_baseline=self.dynamic_load_baseline,
        )
```

After the `pv_forecast_minute10_step` line (~1136):

```python
        pv_forecast_minute90_step = self.step_data_history(self.pv_forecast_minute90, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)
```

Note both use the **nominal** `cloud_factor` — `cloud_factor` shuffles energy between adjacent 5-minute slots and preserves the total, so a mirrored variant would add a knob without changing the scenario's level.

Store them alongside the others:

```python
        self.load_minutes_step90 = load_minutes_step90
        self.pv_forecast_minute90_step = pv_forecast_minute90_step
```

Guard against a missing p90 series (older debug dumps replayed without Task 2's fetch path):

```python
        if not self.pv_forecast_minute90:
            self.pv_forecast_minute90 = dict(self.pv_forecast_minute)
```

Place that guard immediately before the `pv_forecast_minute90_step` line.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/plan.py apps/predbat/tests/test_pv90.py
git commit -m "feat(pv90): build p90 PV and load step arrays"
```

---

### Task 4: Scenario selector in the Python engine

Replace the `pv10` boolean with `pv_scenario` throughout the Python prediction path. The kernel still receives a bool-compatible value, so it keeps working unchanged until Task 5.

**Files:**
- Modify: `apps/predbat/prediction.py:36-61`, `96`, `186-207`, `209-390`, `392-430`, `569-572`, `608`
- Modify: `apps/predbat/plan.py:629-671`, `1149`
- Modify: `apps/predbat/tests/test_pv90.py`

**Interfaces:**
- Consumes: `PV_SCENARIO_*` (Task 1), `pv_forecast_minute90_step` / `load_minutes_step90` (Task 3)
- Produces:
  - `Prediction.__init__(base=None, pv_forecast_minute_step=None, pv_forecast_minute10_step=None, load_minutes_step=None, load_minutes_step10=None, pv_forecast_minute90_step=None, load_minutes_step90=None, soc_kw=None, soc_max=None)` — the two new parameters are inserted **before** `soc_kw`; every existing call site passes `soc_kw`/`soc_max` by keyword so none break. When `None`, they fall back to the nominal arrays.
  - All `launch_run_prediction_*`, `wrapped_run_prediction_*`, `thread_run_prediction_*` and `Prediction.run_prediction` take `pv_scenario: int` in the position previously occupied by `pv10`.

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_pv90.py`:

```python
def test_pv90_scenario_selects_arrays(my_predbat):
    """Each scenario must simulate against its own PV and load series."""
    from prediction import Prediction

    failed = False
    n = my_predbat.forecast_minutes + my_predbat.minutes_now
    pv50 = {minute: 0.02 for minute in range(0, n, 5)}
    pv10 = {minute: 0.01 for minute in range(0, n, 5)}
    pv90 = {minute: 0.03 for minute in range(0, n, 5)}
    load = {minute: 0.02 for minute in range(0, n, 5)}
    load10 = {minute: 0.03 for minute in range(0, n, 5)}
    load90 = {minute: 0.01 for minute in range(0, n, 5)}

    pred = Prediction(my_predbat, pv50, pv10, load, load10, pv90, load90)
    pred.prediction_kernel_enable = False

    costs = {}
    for name, scenario in (("nominal", PV_SCENARIO_NOMINAL), ("pv10", PV_SCENARIO_PV10), ("pv90", PV_SCENARIO_PV90)):
        result = pred.run_prediction([], [], [], [], scenario, my_predbat.forecast_minutes)
        costs[name] = result[0]

    # pv10 is the worst case (least PV, most load), pv90 the best - so cost must order strictly
    if not (costs["pv10"] > costs["nominal"] > costs["pv90"]):
        print("ERROR: scenario costs are not ordered pv10 > nominal > pv90: {}".format(costs))
        failed = True
    return failed


def test_pv90_no_charge_derate_and_no_io_penalty(my_predbat):
    """pv90 must use the full charge rate and must not apply the pv10 io_adjusted worst-case rate."""
    from prediction import Prediction

    failed = False
    n = my_predbat.forecast_minutes + my_predbat.minutes_now
    flat_pv = {minute: 0.0 for minute in range(0, n, 5)}
    flat_load = {minute: 0.01 for minute in range(0, n, 5)}

    # Identical series for every scenario: any remaining difference is the de-rate / io penalty
    pred = Prediction(my_predbat, flat_pv, flat_pv, flat_load, flat_load, flat_pv, flat_load)
    pred.prediction_kernel_enable = False
    pred.charge_scaling10 = 0.5
    pred.io_adjusted = {minute: 1 for minute in range(0, n)}

    nominal = pred.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes)
    pv90 = pred.run_prediction([], [], [], [], PV_SCENARIO_PV90, my_predbat.forecast_minutes)
    pv10 = pred.run_prediction([], [], [], [], PV_SCENARIO_PV10, my_predbat.forecast_minutes)

    if abs(pv90[0] - nominal[0]) > 1e-6:
        print("ERROR: pv90 cost {} differs from nominal {} on identical series - a pv10-only penalty leaked into pv90".format(pv90[0], nominal[0]))
        failed = True
    if abs(pv10[0] - nominal[0]) < 1e-6:
        print("ERROR: pv10 cost matches nominal on identical series - the io_adjusted penalty is not being applied at all, so the pv90 check above is vacuous")
        failed = True
    return failed
```

Add both to `run_pv90_tests`.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|TypeError" /tmp/pv90.txt
```

Expected: FAIL — `TypeError: __init__() takes at most 7 arguments`.

- [ ] **Step 3: Extend the Prediction constructor**

In `apps/predbat/prediction.py:96`:

```python
    def __init__(self, base=None, pv_forecast_minute_step=None, pv_forecast_minute10_step=None, load_minutes_step=None, load_minutes_step10=None, pv_forecast_minute90_step=None, load_minutes_step90=None, soc_kw=None, soc_max=None):
```

In the `if base:` block, next to the existing step assignments (~line 186):

```python
            self.pv_forecast_minute90_step = pv_forecast_minute90_step if pv_forecast_minute90_step is not None else pv_forecast_minute_step
            self.load_minutes_step90 = load_minutes_step90 if load_minutes_step90 is not None else load_minutes_step
```

The fallback keeps the 15 existing `Prediction(...)` call sites in `annual.py`, `output.py`, `marginal.py` and the tests working unchanged — none of them ever request pv90.

- [ ] **Step 4: Thread pv_scenario through the engine**

In `apps/predbat/prediction.py`, rename the parameter `pv10` to `pv_scenario` in all four `wrapped_run_prediction_*` functions, all four `thread_run_prediction_*` methods, and `run_prediction`. The bodies just pass it along; only `run_prediction` interprets it.

Add the import at the top:

```python
from const import PREDICT_STEP, PV_SCENARIO_PV10, PV_SCENARIO_PV90, RUN_EVERY, TIME_FORMAT
```

Replace the series selection (prediction.py:422-428):

```python
        # Fetch data from globals, optimised away from class to avoid passing it between threads
        if pv_scenario == PV_SCENARIO_PV10:
            pv_forecast_minute_step = self.pv_forecast_minute10_step
            load_minutes_step = self.load_minutes_step10
        elif pv_scenario == PV_SCENARIO_PV90:
            pv_forecast_minute_step = self.pv_forecast_minute90_step
            load_minutes_step = self.load_minutes_step90
        else:
            pv_forecast_minute_step = self.pv_forecast_minute_step
            load_minutes_step = self.load_minutes_step
```

Replace the charge de-rate (prediction.py:568-572):

```python
        # For the PV10 case we apply some de-rating to the battery charge rate to be more pessimistic.
        # PV90 is the upside case and gets no de-rate.
        if pv_scenario == PV_SCENARIO_PV10:
            battery_rate_max_scaling = self.battery_rate_max_scaling * self.charge_scaling10
        else:
            battery_rate_max_scaling = self.battery_rate_max_scaling
```

Replace the io_adjusted penalty (prediction.py:608):

```python
            if io_adjusted.get(minute_absolute, 0) and pv_scenario == PV_SCENARIO_PV10 and minute > 30:
```

The `sim_hash` line needs no change — `hash(pv_scenario)` distinguishes all three values.

- [ ] **Step 5: Thread pv_scenario through plan.py**

In `apps/predbat/plan.py`, rename the `pv10` parameter to `pv_scenario` in all four `launch_run_prediction_*` methods (lines 629, 641, 651, 661) and in the `apply_async` / `DummyThread` argument tuples.

Update every existing caller that passes `False` or `True` positionally to use the constants. Add the import:

```python
from const import PREDICT_STEP, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90, TIME_FORMAT, MINUTE_WATT
```

Then replace: `, False, all_n, end_record)` → `, PV_SCENARIO_NOMINAL, all_n, end_record)` and `, True, all_n, end_record)` → `, PV_SCENARIO_PV10, all_n, end_record)` at the `launch_run_prediction_*` call sites, and the same for the `launch_run_prediction_single` calls at plan.py:495-496.

Find them all with:

```bash
grep -n "launch_run_prediction_" apps/predbat/plan.py
```

Also update the `Prediction(...)` construction at plan.py:1149:

```python
        self.prediction = Prediction(self, pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, pv_forecast_minute90_step, load_minutes_step90)
```

And `Plan.run_prediction` at plan.py:3862 — rename its `pv10` parameter to `pv_scenario` and pass it through.

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test pv90 --test model --test optimise_levels > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS. The kernel still sees `pv_scenario` where it expected a bool; `PV_SCENARIO_PV90 = 2` is truthy so it would wrongly select the pv10 arrays — that is exactly what Task 5 fixes. Guard against it in the meantime by forcing `pred.prediction_kernel_enable = False` in the two new tests (already done in Step 1).

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/prediction.py apps/predbat/plan.py apps/predbat/tests/test_pv90.py
git commit -m "feat(pv90): replace the pv10 boolean with a three-valued pv_scenario"
```

---

### Task 5: Kernel support for pv90

**Files:**
- Modify: `apps/predbat/prediction_kernel.cpp:29-30`, `60-75`, `155-165`, `185-200`, `275-312`, `348`, `405-430`
- Modify: `apps/predbat/prediction_kernel.py:33-34`, `45-160`, `290-350`, `447-490`
- Modify: `apps/predbat/tests/test_kernel_parity.py:355-380`

**Interfaces:**
- Consumes: `PV_SCENARIO_*` (Task 1), `Prediction.pv_forecast_minute90_step` / `.load_minutes_step90` (Task 4)
- Produces: `run_prediction_kernel(pred, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache)` handling all three scenarios; `KERNEL_ABI_VERSION == 3`

- [ ] **Step 1: Write the failing test**

In `apps/predbat/tests/test_kernel_parity.py`, change `dual_run` to take a scenario and construct the Prediction with p90 arrays:

```python
def dual_run(name, my_predbat, pv_step, pv10_step, load_step, load10_step, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, pv90_step=None, load90_step=None):
    """Run one scenario through both engines and compare - returns True if they diverge."""
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step, pv90_step, load90_step)
```

and pass `pv_scenario` through to both `prediction.run_prediction(...)` and `run_prediction_kernel(...)`.

Add a dedicated pv90 edge case inside `run_edge_case_tests`:

```python
    # pv90: the kernel must select the p90 arrays, skip the pv10 charge de-rate, and skip the
    # io_adjusted worst-case import rate. Distinct series per scenario so a wrong selection shows up.
    pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, pv_kw=2.0, load_kw=0.5)
    pv90_step = {minute: value * 2.0 for minute, value in pv_step.items()}
    load90_step = {minute: value * 0.5 for minute, value in load_step.items()}
    my_predbat.charge_scaling10 = 0.5
    my_predbat.io_adjusted = {minute: 1 for minute in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now)}
    for scenario in (PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90):
        failed |= dual_run(
            "pv90_scenario_{}".format(scenario),
            my_predbat,
            pv_step,
            pv10_step,
            load_step,
            load10_step,
            [my_predbat.soc_max],
            [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 120, "average": 5.0}],
            [],
            [],
            scenario,
            my_predbat.forecast_minutes,
            pv90_step=pv90_step,
            load90_step=load90_step,
        )
    my_predbat.io_adjusted = {}
```

Add the import at the top of the file:

```python
from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && bash ../apps/predbat/build_kernel.sh && ./run_all --test kernel_parity > /tmp/parity.txt 2>&1; grep -E "ERROR|MISMATCH|Traceback|Passed|Failed" /tmp/parity.txt
```

Expected: FAIL — the pv90 run diverges, because the kernel treats `pv_scenario == 2` as truthy and selects the pv10 arrays plus the de-rate.

- [ ] **Step 3: Update the C++ kernel**

In `apps/predbat/prediction_kernel.cpp`, bump both version macros (lines 29-30):

```cpp
#define PK_ABI_VERSION 3
#define PK_PARITY_REVISION 3
```

Add the two array fields to `PkContext`, immediately after `load10`:

```cpp
    const double *pv90;               // PV forecast kWh per step (PV90)
    const double *load90;             // load kWh per step (PV90)
```

Rename the `PkScenario` field (line 159):

```cpp
    int32_t pv_scenario;          // 0 = nominal, 1 = pv10, 2 = pv90
```

Add the vectors to `ContextStore` (line 188):

```cpp
    std::vector<double> pv, load, pv10, load10, pv90, load90;
```

In `pk_create_context`, deep-copy them (after the `load10` assign, ~line 284):

```cpp
    store->pv90.assign(in->pv90, in->pv90 + n);
    store->load90.assign(in->load90, in->load90 + n);
```

and re-point them (after the `load10` data() line, ~line 305):

```cpp
    store->ctx.pv90 = store->pv90.data();
    store->ctx.load90 = store->load90.data();
```

Replace the scenario decode (line 348):

```cpp
    const int32_t pv_scenario = s->pv_scenario;
    const bool is_pv10 = pv_scenario == 1;
    const bool is_pv90 = pv_scenario == 2;
```

Replace the series and scaling selection (lines 412-415):

```cpp
    // PV10 de-rating of the charge rate - prediction.py:568-572. PV90 is the upside case, no de-rate.
    const double battery_rate_max_scaling = is_pv10 ? c->battery_rate_max_scaling10 : c->battery_rate_max_scaling;
    const double battery_rate_max_scaling_discharge = c->battery_rate_max_scaling_discharge;
    const double *pv_step = is_pv10 ? c->pv10 : (is_pv90 ? c->pv90 : c->pv);
    const double *load_step = is_pv10 ? c->load10 : (is_pv90 ? c->load90 : c->load);
```

Replace the io_flag gate (line 425):

```cpp
        if (c->io_flag[k] && is_pv10 && minute > 30) {
```

- [ ] **Step 4: Update the ctypes binding**

In `apps/predbat/prediction_kernel.py`, bump both constants (lines 33-34):

```python
KERNEL_ABI_VERSION = 3
KERNEL_PARITY_REVISION = 3
```

In `PkContext._fields_`, after `("load10", ...)`:

```python
        ("pv90", ctypes.POINTER(ctypes.c_double)),
        ("load90", ctypes.POINTER(ctypes.c_double)),
```

In `PkScenario._fields_`, rename:

```python
        ("pv_scenario", ctypes.c_int32),
```

In `create_kernel_context`, add the two lists alongside `pv10`/`load10` (~line 301):

```python
        pv90 = []
        load90 = []
```

populate them in the step loop (~line 319):

```python
            pv90.append(pred.pv_forecast_minute90_step[minute])
            load90.append(pred.load_minutes_step90[minute])
```

and assign them (~line 346):

```python
        ctx.pv90 = double_array(pv90)
        ctx.load90 = double_array(load90)
```

In `run_prediction_kernel`, rename the parameter to `pv_scenario` and replace line 476:

```python
    scenario.pv_scenario = int(pv_scenario)
```

Update the call in `prediction.py:415`:

```python
            kernel_result = run_prediction_kernel(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache)
```

- [ ] **Step 5: Rebuild and run the parity tests**

```bash
cd coverage && bash ../apps/predbat/build_kernel.sh && ./run_all --test kernel_parity --test model_kernel --test pv90 > /tmp/parity.txt 2>&1; grep -E "ERROR|MISMATCH|Traceback|Passed|Failed" /tmp/parity.txt
```

Expected: PASS. If the kernel reports "stale binary", `build_kernel.sh` did not run — re-run it, otherwise the parity tests pass vacuously against the Python engine.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/prediction_kernel.cpp apps/predbat/prediction_kernel.py apps/predbat/prediction.py apps/predbat/tests/test_kernel_parity.py
git commit -m "feat(pv90): add pv90 arrays to the prediction kernel, ABI 2->3"
```

Note: the six platform `.so` binaries are cross-built and auto-committed by the `kernel-binaries` CI job on push. Do not build them by hand.

---

### Task 6: Weighted metric blend

**Files:**
- Modify: `apps/predbat/plan.py:1447-1483` (`compute_metric`)
- Modify: `apps/predbat/tests/test_pv90.py`

**Interfaces:**
- Consumes: `PredBat.pv_metric90_weight` (Task 1)
- Produces: `compute_metric(self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=None, cost90=None, final_iboost90=0.0) -> (float, float)` — return shape unchanged

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_pv90.py`:

These three tests mutate weighting state on the shared `my_predbat`, so they must restore it or
they will corrupt every test that runs after them in the suite. Add the helpers first:

```python
METRIC_STATE_ITEMS = [
    "pv_metric10_weight",
    "pv_metric90_weight",
    "metric_battery_value_scaling",
    "carbon_enable",
    "metric_self_sufficiency",
    "metric_battery_cycle",
]


def save_metric_state(my_predbat):
    """Snapshot the weighting attributes the blend tests overwrite."""
    return {name: getattr(my_predbat, name) for name in METRIC_STATE_ITEMS}


def restore_metric_state(my_predbat, state):
    """Restore the weighting attributes saved by save_metric_state."""
    for name, value in state.items():
        setattr(my_predbat, name, value)
```

```python
def test_pv90_metric_blend(my_predbat):
    """The blend must be a signed weighted average that sums to 1.0 across the three scenarios."""
    failed = False
    my_predbat.pv_metric10_weight = 0.1
    my_predbat.pv_metric90_weight = 0.2
    my_predbat.metric_battery_value_scaling = 0.0  # remove the residual credit so cost == metric
    my_predbat.carbon_enable = False
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_cycle = 0.0

    metric, _ = my_predbat.compute_metric(0, 0, 0, 100.0, 200.0, 0, 0, 0, 0, 0, 0, 0, 0, soc90=0, cost90=50.0)
    expected = 0.7 * 100.0 + 0.1 * 200.0 + 0.2 * 50.0
    if abs(metric - expected) > 1e-4:
        print("ERROR: blended metric is {}, expected {}".format(metric, expected))
        failed = True

    # cost90 omitted -> the pv90 term must drop out and the nominal weight absorb it
    metric, _ = my_predbat.compute_metric(0, 0, 0, 100.0, 200.0, 0, 0, 0, 0, 0, 0, 0, 0)
    expected = 0.9 * 100.0 + 0.1 * 200.0
    if abs(metric - expected) > 1e-4:
        print("ERROR: metric without cost90 is {}, expected {}".format(metric, expected))
        failed = True
    return failed


def test_pv90_metric_weight_renormalisation(my_predbat):
    """Weights summing above 1.0 must renormalise so the nominal weight never goes negative."""
    failed = False
    my_predbat.pv_metric10_weight = 0.8
    my_predbat.pv_metric90_weight = 0.8
    my_predbat.metric_battery_value_scaling = 0.0
    my_predbat.carbon_enable = False
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_cycle = 0.0

    metric, _ = my_predbat.compute_metric(0, 0, 0, 100.0, 200.0, 0, 0, 0, 0, 0, 0, 0, 0, soc90=0, cost90=50.0)
    expected = 0.5 * 200.0 + 0.5 * 50.0
    if abs(metric - expected) > 1e-4:
        print("ERROR: renormalised metric is {}, expected {} (nominal weight must clamp to 0)".format(metric, expected))
        failed = True
    return failed


def test_pv90_identical_scenarios_are_identity(my_predbat):
    """When every scenario has the same cost the blend must be a no-op at any weights."""
    failed = False
    my_predbat.pv_metric10_weight = 0.35
    my_predbat.pv_metric90_weight = 0.35
    my_predbat.metric_battery_value_scaling = 0.0
    my_predbat.carbon_enable = False
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_cycle = 0.0

    metric, _ = my_predbat.compute_metric(0, 0, 0, 123.0, 123.0, 0, 0, 0, 0, 0, 0, 0, 0, soc90=0, cost90=123.0)
    if abs(metric - 123.0) > 1e-4:
        print("ERROR: identity blend gave {}, expected 123.0".format(metric))
        failed = True
    return failed
```

Add all three to `run_pv90_tests`, bracketed by the save/restore so the mutated weights do not leak
into later tests in the suite:

```python
    metric_state = save_metric_state(my_predbat)
    try:
        failed |= test_pv90_metric_blend(my_predbat)
        failed |= test_pv90_metric_weight_renormalisation(my_predbat)
        failed |= test_pv90_identical_scenarios_are_identity(my_predbat)
    finally:
        restore_metric_state(my_predbat, metric_state)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|TypeError" /tmp/pv90.txt
```

Expected: FAIL — `TypeError: compute_metric() got an unexpected keyword argument 'soc90'`.

- [ ] **Step 3: Implement the blend**

Replace `compute_metric` in `apps/predbat/plan.py:1447-1483`:

```python
    def compute_metric(self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=None, cost90=None, final_iboost90=0.0):
        """
        Compute the metric by blending the nominal, PV10 and PV90 scenarios

        cost90 is the switch for the PV90 term - when it is None the scenario was not simulated and
        its weight collapses into the nominal weight.
        """
        # Store simulated mid value
        metric = cost
        metric10 = cost10
        metric90 = cost90

        # Balancing payment to account for battery left over
        # ie. how much extra battery is worth to us in future, assume it's the same as low rate
        rate_min = (self.rate_min_forward.get(self.minutes_now + end_record, self.rate_min)) / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
        rate_min = max(min(rate_min, self.rate_max * self.inverter_loss * self.battery_loss - self.metric_battery_cycle), 0)
        rate_export_min = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min
        battery_value = (soc * self.metric_battery_value_scaling + final_iboost * self.iboost_value_scaling) * max(rate_min, 1.0, rate_export_min)
        battery_value10 = (soc10 * self.metric_battery_value_scaling + final_iboost10 * self.iboost_value_scaling) * max(rate_min, 1.0, rate_export_min)
        metric -= battery_value
        metric10 -= battery_value10
        if metric90 is not None:
            battery_value90 = ((soc90 or 0) * self.metric_battery_value_scaling + final_iboost90 * self.iboost_value_scaling) * max(rate_min, 1.0, rate_export_min)
            metric90 -= battery_value90

        # Signed weighted average across the simulated scenarios. Unlike the previous downside-only
        # clamp this lets a better-than-nominal scenario pull the metric down, which is what gives
        # PV90 a gradient at all - PV90 is nearly always cheaper than nominal.
        weight10 = self.pv_metric10_weight
        weight90 = self.pv_metric90_weight if metric90 is not None else 0.0
        weight_total = weight10 + weight90
        if weight_total > 1.0:
            weight10 = weight10 / weight_total
            weight90 = weight90 / weight_total
        metric = (1.0 - weight10 - weight90) * metric + weight10 * metric10 + weight90 * (metric90 if metric90 is not None else 0.0)

        # Carbon metric
        if self.carbon_enable:
            metric += (final_carbon_g / 1000) * self.carbon_metric

        # Self sufficiency metric
        metric += (import_kwh_house + import_kwh_battery) * self.metric_self_sufficiency

        # Adjustment for battery cycles metric
        metric += battery_cycle * self.metric_battery_cycle + metric_keep

        return dp4(metric), dp4(battery_value)
```

- [ ] **Step 4: Update the compute_metric test helper and pin the changed behaviour**

None of the existing cases in `test_compute_metric.py` break: `compute_metric_test` defaults `pv_metric10_weight=0.0` (so the blend is an identity), and the one case that sets it — `"cost10"` with `cost=10, cost10=20, weight=0.5` — has `metric10 > metric`, where the old clamp and the new average both give 15.

But the helper does not yet control `pv_metric90_weight`, so it would inherit whatever the surrounding suite left behind. Add it to the signature after `pv_metric10_weight=0.0`:

```python
    pv_metric90_weight=0.0,
```

set it in the body next to the `pv_metric10_weight` assignment:

```python
    my_predbat.pv_metric90_weight = pv_metric90_weight
```

and add it to the `save_items` list in `save_state` so it is restored:

```python
        "pv_metric90_weight",
```

Then add two cases to `run_compute_metric_tests` that pin the new behaviour, immediately after the existing `"cost10"` line:

```python
    # The blend is a signed weighted average, so a cheaper-than-nominal PV10 now pulls the metric
    # DOWN. The previous downside-only clamp left the metric at `cost` in this case.
    failed |= compute_metric_test(my_predbat, "cost10_cheaper", cost=20.0, cost10=10.0, pv_metric10_weight=0.5, assert_metric=0.5 * 20 + 0.5 * 10)
    failed |= compute_metric_test(my_predbat, "cost90", cost=10.0, cost10=10.0, cost90=4.0, pv_metric10_weight=0.1, pv_metric90_weight=0.2, assert_metric=0.7 * 10 + 0.1 * 10 + 0.2 * 4)
```

The second case needs `cost90` and `soc90` plumbed through the helper — add `cost90=None` and `soc90=0` to its signature and pass them to `compute_metric`:

```python
        soc90=soc90,
        cost90=cost90,
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test pv90 --test compute_metric > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/plan.py apps/predbat/tests/test_pv90.py apps/predbat/tests/test_compute_metric.py
git commit -m "feat(pv90): blend scenarios with a signed weighted average"
```

---

### Task 7: Launch pv90 simulations from the optimiser

**Files:**
- Modify: `apps/predbat/plan.py:415-525` (`optimise_levels`), `1485-1700` (`optimise_charge_limit`), `1870-1915` (`optimise_export`)
- Modify: `apps/predbat/tests/test_pv90.py`

**Interfaces:**
- Consumes: everything from Tasks 1-6
- Produces: no new public names — the pv90 results flow into the existing `compute_metric` calls via its new keyword arguments

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_pv90.py`:

```python
def test_pv90_weight_zero_skips_simulation(my_predbat):
    """With the weight at 0 no pv90 prediction may be run, so plan time is unaffected."""
    failed = False
    my_predbat.pv_metric90_weight = 0.0
    calls = {"count": 0}
    original = my_predbat.launch_run_prediction_charge

    def counting(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Count pv90 launches so the skip can be asserted."""
        if pv_scenario == PV_SCENARIO_PV90:
            calls["count"] += 1
        return original(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    my_predbat.launch_run_prediction_charge = counting
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_charge = original
    if calls["count"] != 0:
        print("ERROR: {} pv90 predictions were launched with pv_metric90_weight=0".format(calls["count"]))
        failed = True
    return failed


def test_pv90_weight_nonzero_runs_simulation(my_predbat):
    """With a non-zero weight pv90 predictions must actually run."""
    failed = False
    my_predbat.pv_metric90_weight = 0.1
    calls = {"count": 0}
    original = my_predbat.launch_run_prediction_charge

    def counting(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Count pv90 launches so the wiring can be asserted."""
        if pv_scenario == PV_SCENARIO_PV90:
            calls["count"] += 1
        return original(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    my_predbat.launch_run_prediction_charge = counting
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_charge = original
        my_predbat.pv_metric90_weight = 0.0
    if calls["count"] == 0:
        print("ERROR: no pv90 predictions were launched with pv_metric90_weight=0.1")
        failed = True
    return failed
```

Add both to `run_pv90_tests`.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test pv90 > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback" /tmp/pv90.txt
```

Expected: FAIL — `ERROR: no pv90 predictions were launched with pv_metric90_weight=0.1`.

- [ ] **Step 3: Wire optimise_charge_limit**

In `apps/predbat/plan.py:1505`, add the third result dict:

```python
        resultmid = {}
        result10 = {}
        result90 = {}
        run_pv90 = self.pv_metric90_weight > 0
```

In the parallel launch block (~line 1657), add the pv90 launch:

```python
        results = []
        results10 = []
        results90 = []
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, all_n, end_record)
                results.append(hanres)
                hanres10 = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV10, all_n, end_record)
                results10.append(hanres10)
                if run_pv90:
                    results90.append(self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_PV90, all_n, end_record))
```

In the collection block (~line 1665):

```python
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = results.pop(0)
                hanres10 = results10.pop(0)
                resultmid[try_soc] = hanres.get()
                result10[try_soc] = hanres10.get()
                if run_pv90:
                    result90[try_soc] = results90.pop(0).get()
```

At the `compute_metric` call (~line 1690):

```python
            (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g) = resultmid[try_soc]
            (cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10) = result10[try_soc]
            soc90 = None
            cost90 = None
            final_iboost90 = 0.0
            if try_soc in result90:
                (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = result90[try_soc]

            # Compute the metric from simulation results
            metric, battery_value = self.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90)
```

The `launch_run_prediction_charge_min_max` calls near line 1533 stay on nominal and pv10 only — they compute the SoC envelope used to prune candidates, not a metric.

- [ ] **Step 4: Wire optimise_export**

Declare the third list next to the existing `results = []` / `results10 = []` near the top of the candidate loop:

```python
        results90 = []
        run_pv90 = self.pv_metric90_weight > 0
```

Replace the launch pair at plan.py:1878-1879:

```python
                results.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, PV_SCENARIO_NOMINAL, all_n, end_record))
                results10.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, PV_SCENARIO_PV10, all_n, end_record))
                if run_pv90:
                    results90.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, PV_SCENARIO_PV90, all_n, end_record))
```

Replace the collection loop at plan.py:1881-1888:

```python
        # Get results from sims
        try_results = []
        for try_option in try_options:
            hanres = results.pop(0)
            hanres10 = results10.pop(0)
            result = hanres.get()
            result10 = hanres10.get()
            result90 = results90.pop(0).get() if run_pv90 else None
            try_results.append(try_option + [result, result10, result90])
```

Replace the unpack at plan.py:1890-1892:

```python
        window_results = {}
        for try_option in try_results:
            start, this_export_limit, hanres, hanres10, hanres90 = try_option
```

Replace the `compute_metric` call at plan.py:1911:

```python
            soc90 = None
            cost90 = None
            final_iboost90 = 0.0
            if hanres90 is not None:
                (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = hanres90

            # Compute the metric from simulation results
            metric, battery_value = self.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90)
```

- [ ] **Step 5: Wire optimise_levels**

At plan.py:495-496:

```python
                                pred_item["handle"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, PV_SCENARIO_NOMINAL, end_record=end_record, step=step)
                                pred_item["handle10"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, PV_SCENARIO_PV10, end_record=end_record, step=step)
                                pred_item["handle90"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, PV_SCENARIO_PV90, end_record=end_record, step=step) if self.pv_metric90_weight > 0 else None
```

At plan.py:506-520:

```python
                    handle90 = pred.get("handle90")
                    ...
                    soc90 = None
                    cost90 = None
                    final_iboost90 = 0.0
                    if handle90 is not None:
                        (cost90, _, _, _, _, soc90, _, _, _, final_iboost90, _) = handle90.get()

                    metric, battery_value = self.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh, soc90=soc90, cost90=cost90, final_iboost90=final_iboost90)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test pv90 --test optimise_levels --test optimise_all_windows > /tmp/pv90.txt 2>&1; grep -E "ERROR|Traceback|Passed|Failed" /tmp/pv90.txt
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/plan.py apps/predbat/tests/test_pv90.py
git commit -m "feat(pv90): launch pv90 simulations from the charge, export and levels optimisers"
```

---

### Task 8: Stage A regression review, then the Stage B experiment

This task produces a decision, not code. Do not change the default weight here.

**Files:**
- Modify: none expected. If `debug_cases` expectations shift, investigate before changing anything.
- Create: `docs/superpowers/plans/2026-08-08-pv90-results.md` (findings)

**Interfaces:**
- Consumes: everything from Tasks 1-7
- Produces: a written result that decides whether Task 9 goes ahead

- [ ] **Step 1: Run the full suite (Stage A)**

```bash
cd coverage && bash ../apps/predbat/build_kernel.sh && ./run_all > /tmp/stage_a.txt 2>&1; echo "EXIT=$?"; grep -E "ERROR|FAILED|Traceback|Passed|Failed" /tmp/stage_a.txt | tail -40
```

Expected: all tests pass. `pv_metric90_weight` is 0, so the only behavioural change reaching the planner is the removal of the pv10 downside-only clamp.

- [ ] **Step 2: Attribute any diff**

If any `debug_cases` case now differs, that diff is caused *only* by the clamp removal — the pv90 scenario is not being simulated at weight 0. Confirm by checking whether the affected case has any candidate where `metric10 < metric`. Record the affected cases and the size of the metric change. Do not regenerate expectations yet.

- [ ] **Step 3: Run the Stage B experiment**

Write `/tmp/pv90_sweep.py`:

```python
"""Sweep pv_metric90_weight on the no-export debug case."""
import sys, os
sys.path.insert(0, os.path.abspath("../apps/predbat"))
import time
from unit_test import create_predbat
from tests.test_single_debug import run_single_debug

for weight in (0.0, 0.05, 0.1, 0.2):
    predbat = create_predbat()
    predbat.pv_metric90_weight = weight
    start = time.time()
    run_single_debug("pv90_w{}".format(weight), predbat, "predbat_no_export_100.txt", redo=True)
    limits = [round(limit / predbat.soc_max * 100) for limit in predbat.charge_limit_best]
    print("RESULT weight={} limits={}% plan_seconds={:.2f}".format(weight, limits, time.time() - start))
```

```bash
cd coverage && ./venv/bin/python /tmp/pv90_sweep.py > /tmp/stage_b.txt 2>&1; grep -E "RESULT|ERROR|Traceback" /tmp/stage_b.txt
```

- [ ] **Step 4: Record the findings**

Write `docs/superpowers/plans/2026-08-08-pv90-results.md` capturing, for each weight: the chosen charge limits, the plan duration, and **where in the 66-100% plateau the plan landed**.

Interpretation, from the spec:

- The dump predates `forecast90`, so pv90 falls back to p50 and this measures the **load-only** upside (10% lower load, identical PV). Stage B is therefore a lower bound.
- Landing at **66%** means pv90 merely overpowered the pv10 hedge and let the plateau's lower tiebreak win — it has not priced spill risk, and this is a weak result that may not generalise.
- Landing at an **interior value** (~80%) means the pv90 term is producing a real gradient across the plateau — the strong result.
- **No movement** is inconclusive, not negative. The next step would be re-running against a dump carrying real Solcast p90 before concluding.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-08-08-pv90-results.md
git commit -m "docs(pv90): record stage A regression review and stage B experiment results"
```

---

### Task 9: Enable by default (conditional on Task 8)

**Only run this task if Task 8's result justifies keeping the feature.** Confirm with the user before starting.

**Files:**
- Modify: `apps/predbat/config.py` (the `pv_metric90_weight` default)
- Modify: `apps/predbat/tests/test_pv90.py` (the expected default)
- Modify: `coverage/cases/*.expected.json` (regenerated)
- Modify: `docs/` (user-facing documentation of the two new settings)

**Interfaces:**
- Consumes: Task 8's decision
- Produces: `pv_metric90_weight` default 0.1

- [ ] **Step 1: Flip the default**

In `apps/predbat/config.py`, change the `pv_metric90_weight` entry:

```python
        "default": 0.1,
```

In `apps/predbat/predbat.py`:

```python
        self.pv_metric90_weight = 0.1
```

In `apps/predbat/tests/test_pv90.py`, update both expectations from `0.0` to `0.1`:

```python
        "pv_metric90_weight": {"default": 0.1, "min": 0, "max": 1.0, "step": 0.01},
```

```python
    for name, default in (("pv_metric90_weight", 0.1), ("load_scaling90", 0.9)):
```

- [ ] **Step 2: Run the full suite and inspect every diff**

```bash
cd coverage && ./run_all > /tmp/stage_c.txt 2>&1; echo "EXIT=$?"; grep -E "ERROR|FAILED|Traceback|Passed|Failed" /tmp/stage_c.txt | tail -40
```

Expected: `debug_cases` failures, because every plan now weighs a third scenario.

- [ ] **Step 3: Manually review each changed case**

For each failing case, replay it and compare the old and new plans:

```bash
cd coverage && ./venv/bin/python ../apps/predbat/unit_test.py --debug_file cases/<case>.yaml --redo > /tmp/case.txt 2>&1; grep -E "Wrote plan|metric" /tmp/case.txt
```

Confirm each change is a plan you would defend — typically a lower charge target where the battery had no headroom. A case that moves the *other* way (charging more) needs explaining before you accept it.

- [ ] **Step 4: Regenerate expectations**

Regenerate the `.expected.json` references only after reviewing each one. The `debug_cases` harness writes a `<case>.actual.json` on failure; promote them individually, not in bulk:

```bash
cd coverage && cp cases/<case>.actual.json cases/<case>.expected.json
```

- [ ] **Step 5: Document the settings**

Add `pv_metric90_weight` and `load_scaling90` to the customisation documentation in `docs/`, next to the existing `pv_metric10_weight` and `load_scaling10` entries. Explain that pv90 is the upside scenario and that raising its weight makes Predbat less willing to fill the battery when there is no export value.

- [ ] **Step 6: Run pre-commit and the full suite**

```bash
./run_pre_commit
cd coverage && ./run_all > /tmp/final.txt 2>&1; echo "EXIT=$?"; grep -E "ERROR|FAILED|Passed|Failed" /tmp/final.txt | tail -20
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(pv90): enable the upside scenario by default at 10% weight"
```

---

## Notes for the implementer

- **The kernel will silently pass tests if the binary is stale.** `build_kernel.sh` before any parity run. `PREDBAT_KERNEL_REQUIRED=1` makes a stale binary fail rather than fall back — use it if unsure.
- **Do not build the six platform `.so` files by hand.** CI's `kernel-binaries` job cross-builds and commits them.
- **Test output is large.** Always redirect to a file and grep afterwards, per the repo convention. Grepping a pipe means re-running the whole suite when you grep for the wrong string.
- **`PV_SCENARIO_PV10 == 1` is deliberate** so a missed `pv10=True` call site still behaves. That also means a missed `False` → `PV_SCENARIO_NOMINAL` conversion is silently correct, but a missed conversion in the *other* direction is not. After Task 4, `grep -n "prediction_charge\|prediction_single\|prediction_export" apps/predbat/plan.py` and confirm no bare `True`/`False` remains.
