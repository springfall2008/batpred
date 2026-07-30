# PV Calibration Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the PV calibration cap do what it was designed to do — stop calibration scaling the forecast above what the array can physically produce — by computing it per slot from a trustworthy ceiling and applying it to the data the planner actually uses.

**Architecture:** Replace the single global `capped_data` in `pv_calibration()` with a per-slot cap `min(ceiling, max(observed_slot, raw_slot))`, where `ceiling = max(1.2 * max_kwh, max_pv_power_hist)`. Drop `max_pv_power_forecast` from the calculation entirely — it is read back from the published `pv_forecast_h0` sensor, which is itself the capped output, so it made the cap depend on its own previous result. Then apply the cap to `pv_forecast_minute_adjusted` (the planner's data) rather than only to the published sensor attributes.

**Tech Stack:** Python 3, asyncio. Tests use the project's `TestSolarAPI` harness in `apps/predbat/tests/test_solcast.py` with mocked history; no pytest.

## Background: why the current code is wrong

Three separate defects, all in `apps/predbat/solcast.py`:

1. **The cap never reaches the planner.** It is computed at `:1102-1107`, applied to `pv_estimateCL`/`pv_estimate10`/`pv_estimate90` at `:1113-1115`, and those three series are used only to annotate `pv_forecast_data` entries (published sensor attributes). `pv_calibration()` returns the *uncapped* `pv_forecast_minute_adjusted` at `:1167`. Since `slot_adjustment` is clamped to `[0.2, 4.0]` and `average_day_scaling` to `[0.1, 2.0]`, and the two compound, the planner can be handed a forecast several times the array's physical ceiling.

2. **The cap can clip below the raw forecast.** `capped_data = min(max_kwh_cap, observed_cap)` takes no account of what the forecast itself predicted for a slot. A system whose 7-day observed peak is 1 kW gets capped at 1 kW even when the raw forecast says 3 kW for a sunny day — so a dull week suppresses the first sunny day's plan. The existing `test_pv_calibration_capped_data_clamp` fixture demonstrates exactly this: raw forecast 3 kW, observed 1 kW, and the cap lands on 1 kW.

3. **The cap is circular.** `observed_cap` includes `max_pv_power_forecast`, which is read from `sensor.predbat_pv_forecast_h0` history at `:898` and `:975`. That sensor's state is `power_nowCL` when calibration is enabled (`:811`), which derives from the capped `pv_estimateCL`. So the cap's input is derived from its own output.

A fourth issue is cosmetic but caused real confusion: the comment at `:1097-1101` says the cap is "the inverter rating", but `max_kwh` is the declared array capacity (`kwp * efficiency`, summed across planes, at `:296` and `:398`). The inverter limit is never referenced in this file.

## The agreed formula

Per slot `s`, all quantities in kWh per plan interval:

```
observed_slot = max_pv_power_hist / 60 * plan_interval_minutes
ceiling       = max(1.2 * max_kwh, max_pv_power_hist) / 60 * plan_interval_minutes
raw_slot[s]   = sum of pv_forecast_minute over the slot        (pre-scaling forecast)

cap[s] = min(ceiling, max(observed_slot, raw_slot[s]))
```

Rationale for each term:

- **`1.2 * max_kwh`** — the declared array capacity plus 20% headroom. Cloud-edge enhancement pushes plane-of-array irradiance above 1000 W/m² and cool cells run above STC efficiency, so an array genuinely exceeds its nameplate briefly.
- **`max(..., max_pv_power_hist)` in the ceiling** — measured generation is direct physical evidence and beats any declared figure. Under-declared `kwp` is common (users enter the inverter size, or one string of two). Without this term, a user who declared 6.44 kWp on an 8.5 kWp array would have the cap clip below what their meter recorded.
- **`max(observed_slot, raw_slot[s])`** — the cap only ever limits calibration scaling *upwards*; it never clips the raw forecast itself. This is what makes a dull week harmless.

Because `ceiling >= observed_slot` and the inner `max >= observed_slot`, the invariant `cap[s] >= observed_slot` holds always: **the cap can never clip below observed generation.**

Note `max_kwh` is initialised to `9999` at `:1234` and only reassigned on the forecast.solar and open-meteo branches, so Solcast and HA-sensor users keep `9999`. For them the `ceiling` term is inert and the cap reduces to `max(observed_slot, raw_slot[s])`. This is a deliberate behaviour change: those users currently get no cap at all. It is bounded — the cap can never fall below that slot's own raw forecast — so it can only ever limit scaling above what Solcast already predicted.

## Global Constraints

- Line length: 256 chars (Black), 250 chars (Flake8).
- Docstrings required on every function and class (`interrogate`, 100% coverage), including nested functions inside tests.
- Variable naming: `lower_case_with_underscores`.
- Tests are run from the `coverage/` directory. Always redirect test output to a file and grep the file afterwards; never pipe test output directly to grep.
- `max_kwh` keeps its current meaning (`kwp * efficiency`, summed across planes). Do not change what the download functions accumulate, and do not rename it.
- Do not touch `fetch_pv_forecast()`, `log_source_change()`, or anything to do with `forecast_solar_open_meteo_first` — that work is already merged and is not part of this change.
- Do not change the `kwp * efficiency` value sent to Forecast.solar.

## Reference: how to run the tests

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/cap_solcast.txt 2>&1
grep -E "ERROR|FAIL|Traceback" /tmp/cap_solcast.txt
```

A passing run produces no `ERROR:` lines. Each test function returns a `failed` boolean; `run_solcast_tests` ORs them together.

---

### Task 1: Replace the global cap with the per-slot formula

**Files:**
- Modify: `apps/predbat/solcast.py:1094-1115` (the cap block)
- Test: `apps/predbat/tests/test_solcast.py` — update `test_pv_calibration_capped_data_clamp` (line 2975), add four new tests, register them in `run_solcast_tests` (line 4056)

**Interfaces:**
- Consumes: `max_pv_power_hist` (kW, computed at `:954`), `max_kwh` (kW, the `pv_calibration` parameter), `pv_forecast_minute` (the raw pre-scaling per-minute forecast, first parameter), `pv_forecast_minute_adjusted` (built at `:1086-1092`).
- Produces: `pv_estimateCL`, `pv_estimate10`, `pv_estimate90` dicts keyed by slot start minute, each capped per slot. Task 2 relies on the per-slot `capped_data` value being computed inside the same loop.

- [ ] **Step 1: Update the existing cap test to the new expected value**

`test_pv_calibration_capped_data_clamp` at `apps/predbat/tests/test_solcast.py:2975` will fail after this change, and it should — its fixture is the exact case the new formula fixes. Its setup is: observed peak 1 kW, raw forecast 3 kW, `max_kwh` 2 kW, and the h0 forecast history is empty (`get_history_wrapper` returns `[]`), so calibration is disabled and the adjusted values equal the raw 3 kW.

- Old behaviour: `observed_cap = max(1.0, 0) = 1.0`, `capped_data = min(2.0, 1.0) = 1.0` — the raw 3 kW forecast is clipped to 1 kW.
- New behaviour: `ceiling = max(1.2 * 2.0, 1.0) = 2.4`, `cap = min(2.4, max(1.0, 3.0)) = 2.4`.

Replace the docstring and the expectation. Find the docstring block that begins `Test that the capped_data clamp in pv_calibration correctly limits the` and replace the whole docstring with:

```python
    """
    Test the per-slot cap in pv_calibration.

    Setup: observed peak is 1 kW, the raw forecast is 3 kW, and max_kwh (declared array
    capacity) is 2 kW. The h0 forecast history is empty so calibration is disabled and the
    adjusted values equal the raw forecast.

    cap = min(ceiling, max(observed_slot, raw_slot))
        = min(max(1.2 * 2.0, 1.0), max(1.0, 3.0))
        = min(2.4, 3.0)
        = 2.4 kW  ->  2.4 * plan_interval / 60 per slot

    The 1.2 * max_kwh ceiling is what binds here. Note the cap is ABOVE the observed peak
    of 1 kW: a dull week must not suppress a sunny day's forecast.
    """
```

Then replace the expectation line. Find:

```python
        expected_cap = max_kwh / 60 * plan_interval  # max_kwh limits here
```

with:

```python
        expected_cap = 1.2 * max_kwh / 60 * plan_interval  # the 1.2 * max_kwh ceiling binds here
```

Also delete the three stale comment lines directly above it that describe the old formula (they begin `# capped_data = min(max(max_pv_power_hist, max_pv_power_forecast), max_kwh)`).

- [ ] **Step 2: Write the four new failing tests**

Add these after `test_pv_calibration_capped_data_clamp` ends (immediately before `def test_pv_calibration_no_history_not_zeroed` at line 3053).

They share a helper that builds a controlled history where **today's raw forecast is higher than the historical average forecast**. That is what makes the cap bite: `adjusted = raw_now * slot_adjustment * use_scaling_day`, and when `raw_now` exceeds the forecast level the slot adjustment was derived from, the product can exceed the observed peak.

```python
def _cap_scenario(max_kwh, raw_kw, hist_kw, hist_forecast_kw=1.0, days_back=5):
    """Run pv_calibration with a controlled history.

    Builds days_back past days that each generated hist_kw for one hour while the recorded
    h0 forecast said hist_forecast_kw, then offers a raw forecast of raw_kw for today's
    matching window. Today's raw forecast is deliberately allowed to differ from the
    historical forecast level - that is what lets the adjusted value exceed the observed
    peak and so exercise the cap.

    Returns (test_api, adj_m, adj_data, plan_interval, gen_start, gen_end). The caller owns
    the returned test_api and must call cleanup() on it.
    """
    gen_start = 600
    gen_end = 660
    test_api = create_test_solar_api()
    solar = test_api.solar
    base = test_api.mock_base
    plan_interval = base.plan_interval_minutes
    minutes_now = base.minutes_now

    # Cumulative pv_today kWh keyed by minutes-ago, hist_kw for one hour each past day
    hist = {}
    for day_idx in range(days_back):
        day = day_idx + 1
        midnight_ago = day * 1440 + minutes_now
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago < 0:
                continue
            if step < gen_start:
                cumulative = 0.0
            elif step < gen_end:
                cumulative = hist_kw * (step - gen_start) / 60.0
            else:
                cumulative = hist_kw
            hist[minute_ago] = cumulative

    # Recorded h0 forecast history for the same windows
    pv_forecast_hist = {}
    for day_num in range(1, days_back + 1):
        for m_of_day in range(gen_start, gen_end):
            pv_forecast_hist[day_num * 1440 + (minutes_now - m_of_day)] = float(hist_forecast_kw)

    def mock_minute_import_export(max_days_prev, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=hist):
        """Return the synthetic pv_today history."""
        return dict(_hist) if key == "pv_today" else {}

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = lambda entity_id, days, required=False: []

    total_minutes = 4 * 24 * 60
    pv_m = {m: (raw_kw / 60.0) if gen_start <= m < gen_end else 0.0 for m in range(total_minutes)}
    pv_m10 = dict(pv_m)

    midnight = datetime(2025, 6, 15, 0, 0, 0, tzinfo=pytz.utc)
    pv_data = []
    for slot in range(gen_start, gen_end, plan_interval):
        ts = midnight + timedelta(minutes=slot)
        pv_data.append({"period_start": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"), "pv_estimate": raw_kw * plan_interval / 60.0})

    with patch("solcast.history_attribute_to_minute_data", return_value=(pv_forecast_hist, days_back)):
        adj_m, adj_m10, adj_data = solar.pv_calibration(pv_m, pv_m10, pv_data, create_pv10=True, divide_by=1.0, max_kwh=max_kwh, forecast_days=solar.forecast_days)

    return test_api, adj_m, adj_data, plan_interval, gen_start, gen_end


def _max_slot_cl(adj_data):
    """Return the largest pv_estimateCL written back into the forecast entries."""
    values = [e.get("pv_estimateCL", 0) for e in adj_data if e.get("pv_estimateCL", None) is not None]
    return max(values) if values else 0


def test_pv_calibration_cap_allows_raw_forecast_above_observed(my_predbat):
    """
    The cap must never clip below a slot's own pre-scaling forecast.

    Observed peak 2 kW, raw forecast 3 kW, max_kwh 4 kW.
    ceiling = max(1.2 * 4.0, 2.0) = 4.8;  cap = min(4.8, max(2.0, 3.0)) = 3.0 kW.

    The old formula gave min(4.0, 2.0) = 2.0 kW, clipping the raw forecast to two thirds
    of what the forecast itself predicted - so a dull week suppressed the next sunny day.
    """
    print("  - test_pv_calibration_cap_allows_raw_forecast_above_observed")
    failed = False

    test_api, adj_m, adj_data, plan_interval, gen_start, gen_end = _cap_scenario(max_kwh=4.0, raw_kw=3.0, hist_kw=2.0)
    try:
        expected_cap = 3.0 / 60 * plan_interval
        got = _max_slot_cl(adj_data)
        if got > expected_cap * 1.01:
            print("ERROR: pv_estimateCL {} exceeds expected cap {}".format(got, expected_cap))
            failed = True
        # And it must not be clipped down to the old, lower observed-peak cap
        old_cap = 2.0 / 60 * plan_interval
        if got <= old_cap * 1.01:
            print("ERROR: pv_estimateCL {} was clipped to the old observed-peak cap {} - the raw forecast floor is not being applied".format(got, old_cap))
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_cap_ceiling_binds_at_headroom(my_predbat):
    """
    When the raw forecast exceeds the array's physical ceiling, the 1.2 * max_kwh headroom binds.

    Observed peak 2 kW, raw forecast 10 kW, max_kwh 4 kW.
    ceiling = max(1.2 * 4.0, 2.0) = 4.8;  cap = min(4.8, max(2.0, 10.0)) = 4.8 kW.
    """
    print("  - test_pv_calibration_cap_ceiling_binds_at_headroom")
    failed = False

    test_api, adj_m, adj_data, plan_interval, gen_start, gen_end = _cap_scenario(max_kwh=4.0, raw_kw=10.0, hist_kw=2.0)
    try:
        expected_cap = 1.2 * 4.0 / 60 * plan_interval
        got = _max_slot_cl(adj_data)
        if got > expected_cap * 1.01:
            print("ERROR: pv_estimateCL {} exceeds the 1.2 * max_kwh ceiling {}".format(got, expected_cap))
            failed = True
        if got < expected_cap * 0.99:
            print("ERROR: pv_estimateCL {} is below the ceiling {} - expected the ceiling to bind".format(got, expected_cap))
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_cap_never_clips_observed_generation(my_predbat):
    """
    An under-declared kwp must not cause the cap to clip below measured generation.

    A user declares 2 kW but the array demonstrably produced 6 kW.
    ceiling = max(1.2 * 2.0, 6.0) = 6.0;  cap = min(6.0, max(6.0, 1.0)) = 6.0 kW.

    The old formula gave min(2.0, 6.0) = 2.0 kW - a third of what the meter recorded.
    """
    print("  - test_pv_calibration_cap_never_clips_observed_generation")
    failed = False

    test_api, adj_m, adj_data, plan_interval, gen_start, gen_end = _cap_scenario(max_kwh=2.0, raw_kw=1.0, hist_kw=6.0)
    try:
        observed_slot = 6.0 / 60 * plan_interval
        got = _max_slot_cl(adj_data)
        # The calibrated value is scaled up towards the observed level and must not be
        # clipped below it by the under-declared max_kwh.
        old_cap = 2.0 / 60 * plan_interval
        if got <= old_cap * 1.01:
            print("ERROR: pv_estimateCL {} was clipped to the under-declared max_kwh cap {}".format(got, old_cap))
            failed = True
        if got > observed_slot * 1.01:
            print("ERROR: pv_estimateCL {} exceeds the observed peak {}".format(got, observed_slot))
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_pv_calibration_cap_applies_without_declared_capacity(my_predbat):
    """
    Solcast and HA-sensor users have max_kwh = 9999, so the ceiling term is inert and the
    cap reduces to max(observed_slot, raw_slot). It must still limit scaling above that.

    Observed peak 2 kW, raw forecast 3 kW, max_kwh 9999.
    ceiling is effectively unbounded;  cap = max(2.0, 3.0) = 3.0 kW.
    """
    print("  - test_pv_calibration_cap_applies_without_declared_capacity")
    failed = False

    test_api, adj_m, adj_data, plan_interval, gen_start, gen_end = _cap_scenario(max_kwh=9999, raw_kw=3.0, hist_kw=2.0)
    try:
        expected_cap = 3.0 / 60 * plan_interval
        got = _max_slot_cl(adj_data)
        if got > expected_cap * 1.01:
            print("ERROR: pv_estimateCL {} exceeds expected cap {} with max_kwh 9999".format(got, expected_cap))
            failed = True
    finally:
        test_api.cleanup()

    return failed
```

Register all four in `run_solcast_tests`, immediately after the existing `failed |= test_pv_calibration_capped_data_clamp(my_predbat)` line (4130):

```python
    failed |= test_pv_calibration_capped_data_clamp(my_predbat)
    failed |= test_pv_calibration_cap_allows_raw_forecast_above_observed(my_predbat)
    failed |= test_pv_calibration_cap_ceiling_binds_at_headroom(my_predbat)
    failed |= test_pv_calibration_cap_never_clips_observed_generation(my_predbat)
    failed |= test_pv_calibration_cap_applies_without_declared_capacity(my_predbat)
```

`datetime`, `timedelta`, `pytz` and `patch` are already module-level imports in `test_solcast.py` (lines 16-18), so the new tests need no import changes.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/cap_step3.txt 2>&1
grep -E "ERROR|Traceback" /tmp/cap_step3.txt
```

Expected: failures from the updated `test_pv_calibration_capped_data_clamp` (its value is capped to 1.0 kW by the old formula, below the new 2.4 expectation) and from `test_pv_calibration_cap_allows_raw_forecast_above_observed`, `test_pv_calibration_cap_never_clips_observed_generation` and `test_pv_calibration_cap_applies_without_declared_capacity`. `test_pv_calibration_cap_ceiling_binds_at_headroom` may pass or fail depending on where the old formula lands; either is fine at this stage.

- [ ] **Step 4: Replace the cap block**

In `apps/predbat/solcast.py`, replace lines 1094-1115 — from `pv_estimateCL = {}` through the `pv_estimate90[minute] = ...` line — with:

```python
        pv_estimateCL = {}
        pv_estimate10 = {}
        pv_estimate90 = {}
        # Cap the calibrated forecast so calibration cannot scale it above what the array can
        # physically produce. The ceiling is the declared array capacity (max_kwh, which is
        # kwp * efficiency - NOT the inverter rating) plus 20% headroom, since cloud-edge
        # enhancement and cool cells briefly push an array above its nameplate. The ceiling is
        # never below the observed peak: measured generation is direct evidence and beats a
        # declared figure, which is often understated (users enter the inverter size, or one
        # string of two).
        #
        # Within that ceiling each slot is allowed up to the larger of the observed peak and
        # that slot's own pre-scaling forecast, so the cap only ever limits scaling upwards and
        # never clips the raw forecast itself - otherwise a dull week would suppress the first
        # sunny day. Because the ceiling and the inner max are both >= observed_slot, the cap
        # can never fall below observed generation.
        #
        # max_pv_power_forecast is deliberately NOT used here: it is read back from the
        # published pv_forecast_h0 sensor, whose state is this same capped output, so including
        # it made the cap depend on its own previous result.
        observed_slot = max_pv_power_hist / 60 * self.plan_interval_minutes
        ceiling_slot = max(1.2 * max_kwh, max_pv_power_hist) / 60 * self.plan_interval_minutes
        for minute in range(0, max(pv_forecast_minute.keys()) + 1, self.plan_interval_minutes):
            pv_value = 0
            raw_value = 0
            for offset in range(0, self.plan_interval_minutes, 1):
                pv_value += pv_forecast_minute_adjusted.get(minute + offset, 0)
                raw_value += pv_forecast_minute.get(minute + offset, 0)
            capped_data = min(ceiling_slot, max(observed_slot, raw_value))
            pv_estimateCL[minute] = dp4(min(pv_value, capped_data))
            pv_estimate10[minute] = dp4(min(pv_value * worst_day_scaling, capped_data))
            pv_estimate90[minute] = dp4(min(pv_value * best_day_scaling, capped_data))
```

`max_pv_power_forecast` is still computed at `:975` and still printed in the log lines at `:1021` and `:1060` — leave both alone. It remains useful diagnostic output; it is only its use in the cap that was wrong.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/cap_step5.txt 2>&1
grep -E "ERROR|Traceback|FAIL" /tmp/cap_step5.txt
```

Expected: no output from grep. `test_pv_calibration_no_history_not_zeroed` must still pass — with no history `max_pv_power_hist` is 0, so `cap = min(1.2 * max_kwh, max(0, raw_slot)) = raw_slot`, and since calibration is disabled in that case the adjusted values equal the raw forecast, so nothing is zeroed.

- [ ] **Step 6: Run pre-commit and commit**

```bash
cd /Users/treforsouthwell/source/batpred
source coverage/venv/bin/activate
pre-commit run --files apps/predbat/solcast.py apps/predbat/tests/test_solcast.py
git add apps/predbat/solcast.py apps/predbat/tests/test_solcast.py
git commit -m "fix(solar): compute the PV calibration cap per slot from a trustworthy ceiling"
```

---

### Task 2: Apply the cap to the planner's data

**Files:**
- Modify: `apps/predbat/solcast.py` (the cap loop from Task 1, plus a summary log line)
- Test: `apps/predbat/tests/test_solcast.py` (one new test)

**Interfaces:**
- Consumes: the per-slot `capped_data` computed inside the cap loop in Task 1, and `pv_forecast_minute_adjusted`.
- Produces: `pv_forecast_minute_adjusted` capped in place. `pv_forecast_minute10` is built from it at `:1157-1161` and so inherits the cap; `worst_day_scaling` is always `<= 1.0` (it is the minimum day ratio divided by the average), so no separate cap is needed there.

- [ ] **Step 1: Write the failing test**

Add after `test_pv_calibration_cap_applies_without_declared_capacity`:

```python
def test_pv_calibration_cap_applied_to_planner_data(my_predbat):
    """
    The cap must reach the data the planner uses, not just the published sensor attributes.

    pv_calibration returns pv_forecast_minute_adjusted, which the planner consumes. Summed
    over a slot it must respect the same cap as pv_estimateCL, otherwise the optimiser plans
    against PV output the array cannot produce.

    Observed peak 2 kW, raw forecast 3 kW, max_kwh 4 kW -> cap = 3.0 kW per slot equivalent.
    """
    print("  - test_pv_calibration_cap_applied_to_planner_data")
    failed = False

    test_api, adj_m, adj_data, plan_interval, gen_start, gen_end = _cap_scenario(max_kwh=4.0, raw_kw=3.0, hist_kw=2.0)
    try:
        expected_cap = 3.0 / 60 * plan_interval
        worst_slot = 0
        for slot in range(gen_start, gen_end, plan_interval):
            slot_sum = sum(adj_m.get(slot + offset, 0) for offset in range(plan_interval))
            worst_slot = max(worst_slot, slot_sum)
        if worst_slot > expected_cap * 1.01:
            print("ERROR: planner slot total {} exceeds the cap {} - the cap is not reaching pv_forecast_minute_adjusted".format(worst_slot, expected_cap))
            failed = True
        if worst_slot <= 0:
            print("ERROR: planner slot total is {} - the scenario produced no forecast to cap".format(worst_slot))
            failed = True
    finally:
        test_api.cleanup()

    return failed
```

Register it in `run_solcast_tests` after the four from Task 1:

```python
    failed |= test_pv_calibration_cap_applied_to_planner_data(my_predbat)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/cap_t2_step2.txt 2>&1
grep -E "ERROR|Traceback" /tmp/cap_t2_step2.txt
```

Expected: `ERROR: planner slot total ... exceeds the cap ... - the cap is not reaching pv_forecast_minute_adjusted`. The adjusted value is roughly `raw * slot_adjustment * use_scaling_day`, well above the 3.0 kW cap, because `pv_calibration` currently returns the uncapped dict.

- [ ] **Step 3: Scale the per-minute data inside the cap loop**

The cap is in kWh per plan interval while `pv_forecast_minute_adjusted` is per-minute, so a per-minute `min()` against `capped_data` would be wrong by a factor of `plan_interval_minutes`. Scale the slot's minutes by the ratio instead, which preserves the shape within the slot.

Extend the loop body from Task 1 Step 4. After the three `pv_estimate*` assignments, add:

```python
            # Apply the same cap to the per-minute data the planner consumes. Scale rather than
            # clamp per minute: capped_data is kWh per plan interval, not per minute.
            if pv_value > capped_data and pv_value > 0:
                scale_down = capped_data / pv_value
                for offset in range(0, self.plan_interval_minutes, 1):
                    if (minute + offset) in pv_forecast_minute_adjusted:
                        pv_forecast_minute_adjusted[minute + offset] = dp4(pv_forecast_minute_adjusted[minute + offset] * scale_down)
                capped_slots += 1
```

Initialise the counter directly above the loop, next to `observed_slot` and `ceiling_slot`:

```python
        capped_slots = 0
```

- [ ] **Step 4: Log when the cap binds**

The cap now changes the plan, so it must be visible. Add directly after the cap loop ends, before the `for entry in pv_forecast_data:` loop:

```python
        if capped_slots:
            self.log("SolarAPI: PV Calibration: Capped {} slots to the array ceiling ({}kW observed peak, {}kW ceiling)".format(capped_slots, dp2(max_pv_power_hist), dp2(max(1.2 * max_kwh, max_pv_power_hist))))
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/cap_t2_step5.txt 2>&1
grep -E "ERROR|Traceback|FAIL" /tmp/cap_t2_step5.txt
```

Expected: no output from grep.

- [ ] **Step 6: Run the full quick suite**

`pv_calibration` feeds the planner, so a change here can move plan outputs across the whole suite.

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --quick > /tmp/cap_quick.txt 2>&1
grep -E "^\*\*\*\*.*FAILED|All tests passed" /tmp/cap_quick.txt
tail -5 /tmp/cap_quick.txt
```

Expected: `All tests passed`. If a plan-level test now fails, do not adjust the cap to make it pass — report it. A changed plan output may be the correct new behaviour, but it is the controller's call, not the implementer's. Note the suite prints `ERROR:`-shaped strings from negative-path tests even on a green run, which is why this step greps for the suite's own verdict rather than for `ERROR`.

- [ ] **Step 7: Run pre-commit and commit**

```bash
cd /Users/treforsouthwell/source/batpred
source coverage/venv/bin/activate
pre-commit run --files apps/predbat/solcast.py apps/predbat/tests/test_solcast.py
git add apps/predbat/solcast.py apps/predbat/tests/test_solcast.py
git commit -m "fix(solar): apply the PV calibration cap to the planner's forecast data"
```

---

## Verification

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all > /tmp/cap_full.txt 2>&1
grep -E "^\*\*\*\*.*FAILED|All tests passed" /tmp/cap_full.txt
tail -5 /tmp/cap_full.txt
```

Expected: the full suite passes.

## Out of scope

- `fetch_pv_forecast()`, `log_source_change()` and `forecast_solar_open_meteo_first` — merged separately in #4387.
- Renaming `max_kwh` (it is kW, not kWh — a genuine misnomer, but renaming it is a separate change).
- Changing what the download functions accumulate into `max_kwh`.
- The `kwp * efficiency` value sent to Forecast.solar, and any possible double-derate there.
- Making the 1.2 headroom factor configurable.
