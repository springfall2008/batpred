# PV Calibration Shrink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- Replace PV calibration's 7-day history read with a 28-day per-slot evidence store and a self-limiting (shrunk) estimator.
- Replace the PVAccuracy chart with a PV Calibration page.

**Architecture:**
- **A pure module, `apps/predbat/pv_calibration.py`**, does the arithmetic: fit, band, merge, clean, scan window.
- **`SolarAPI`** (`solcast.py`) gains:
  - a synchronous history scan that builds day evidence;
  - an async store update (load, scan, merge, save, with the battery rate grid's guards);
  - a `pv_calibration()` whose factor stage reads the evidence and the fit. Everything after the factors is unchanged.
- **`web.py`** gets a Curves-style page built from the solar component's state.

**Tech Stack:** Python 3 (Predbat); Plotly (already loaded by the Curves page); Predbat's `unit_test.py` registry.

**Spec:** `docs/superpowers/specs/2026-11-16-pv-calibration-shrink-design.md`

## Global Constraints

- **Pre-commit:** `cd coverage && ./run_pre_commit` must pass.
  - Line length 256 (Black) / 250 (Flake8).
  - Every new function and class has a one-line docstring (interrogate, 100%).
  - British English (CSpell). Add new words to `.cspell/custom-dictionary-workspace.txt`.
  - Names are `lower_case_with_underscores`.
- **Tests:** they run from `coverage/` (`./run_all --test <name>`); always save output to a file, then grep it. Each new test module exports `run_<name>_tests(my_predbat)` and is registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`.
- **GitNexus:** run `impact` before editing any existing symbol and report HIGH/CRITICAL; run `detect_changes` before each commit.
- **Git:** never `git add .` or `-A`; add explicit paths.
- **Defaults** (`const.py`), exactly:
  - `PV_CALIBRATION_HISTORY_DAYS = 28`
  - `PV_CALIBRATION_SCAN_DAYS = 2`
  - `PV_CALIBRATION_HALF_LIFE_DAYS = 3.5`
  - `PV_CALIBRATION_SHRINK = 0.20`
  - `PV_CALIBRATION_SHRINK_SLOT = 0.20`
- **Fixed behaviour** (`const.py`), exactly:
  - `PV_CALIBRATION_MIN_DAYS = 3`
  - `PV_CALIBRATION_BAND_DAYS = 7`
  - `PV_CALIBRATION_LOG_CLAMP = 1.5`
  - `PV_CALIBRATION_MIN_SLOT_KWH = 0.02`
  - `PV_CALIBRATION_MIN_SLOT_POINTS = 3`
  - `PV_CALIBRATION_STORE_EXPIRY_DAYS = 60`
  - `PV_CALIBRATION_SCAN_MINUTES = 24 * 60`
  - `PV_CALIBRATION_RETRY_MINUTES = 60`
  - `PV_CALIBRATION_RESAVE_MINUTES = 24 * 60`
  - `PV_CALIBRATION_LOAD_ATTEMPTS = 3`
  - `PV_CALIBRATION_LOAD_WAIT_MINUTES = 10`
  - `PV_CALIBRATION_LOWEST = 0.20` and `PV_CALIBRATION_HIGHEST = 4.0` move here from `solcast.py`. `solcast.py` imports them, so existing `from solcast import PV_CALIBRATION_*` keeps working.
- **Storage:** namespace `"solcast"`, key `"pv_calibration_evidence"`. Document `{"slot_minutes": int, "days": {...}, "updated": iso, "last_scan": iso}`.
- **Unchanged after the factor stage:** `days_use_scaling`, the array ceiling cap, the P10/P90 construction, `calibrate_band`, `impute_clipped_pv_history`, `metric_pv_calibration_enable`.
- **The store holds the forecast before `pv_scaling`;** the fit multiplies by the current `pv_scaling`.
- **Long history reads** pass `tracked=False`.

## Review Focus

1. **`pv_scaling` set to 0** (a user zeroing PV for unseen AC-coupled solar). The scan must not store anything, and stored days must survive. With no usable forecast, calibration turns off. Task 2 test `test_scan_pv_scaling_zero_stores_nothing`.
2. **A partial first day** (fresh install, or history that starts mid-day). That day must not be stored, or its low actual would read as forecast error. Task 2 test `test_scan_skips_partially_covered_day`.
3. **A corrupt or foreign storage document,** or one saved at a different `slot_minutes`. It is dropped without a crash, and the store is rebuilt by the next scan. Task 1 `test_clean_drops_malformed`, Task 3 `test_update_discards_other_slot_minutes`.
4. **The clock moving backwards, or a stored day dated today or later.** The merge drops it, so a future-dated day never counts as evidence. Task 1 `test_merge_drops_today_and_future`.
5. **`pv_calibration_history_days` smaller than `pv_calibration_scan_days`, or below 3.** The scan days are clamped to the history days, and a warning says calibration needs at least 3 days. Task 2 `test_settings_clamp_and_warn`.

---

### Task 1: Pure calibration module

**Files:**
- Create: `apps/predbat/pv_calibration.py`
- Modify: `apps/predbat/const.py` (add the constants in Global Constraints)
- Modify: `apps/predbat/solcast.py:35-36` (import `PV_CALIBRATION_LOWEST`/`HIGHEST` from const instead of defining them)
- Test: `apps/predbat/tests/test_pv_calibration.py`, registered as `("pv_calibration", run_pv_calibration_tests, "PV calibration: shrunk fit, band, evidence merge", False)`

**Interfaces:**
- Produces, all in `pv_calibration.py`:
  - **Evidence day shape:** `{"slots": {minute_of_day:int: [forecast_kwh, actual_kwh]}, "peak_kw": float, "clipped_minutes": int}`.
    - Keyed by local date `"YYYY-MM-DD"`.
    - `forecast_kwh` is before `pv_scaling`.
  - `clean_pv_calibration_evidence(data) -> dict`: a stored or JSON `days` map with string minute keys gives the shape above, with int keys. Malformed days are dropped.
  - `merge_pv_calibration_evidence(stored, scanned, today, max_age_days) -> dict`:
    - Scanned days replace stored ones.
    - Days `>= today`, or older than `today - max_age_days`, are dropped.
  - `pv_calibration_scan_length(last_success, now, history_days, scan_days) -> int`: `history_days` when `last_success` is None, else `min(max(scan_days, int(gap_days + 0.5) + 1), history_days)`. This is the same formula as `battery_rate_grid_update`.
  - `day_age(day, today) -> int`: whole days, where yesterday is 1.
  - `fit_pv_calibration(evidence, today, pv_scaling, half_life, shrink_day, shrink_slot, slot_minutes) -> dict | None`:
    - Returns None when there are fewer than `PV_CALIBRATION_MIN_DAYS` valid days.
    - Otherwise the dict has these keys:
      - `total`, `raw_total`, `se_day`;
      - `adj` and `raw_adj`, each `{minute_of_day: factor}` for every slot in `range(0, 1440, slot_minutes)`;
      - `days_used` (a sorted list of dates) and `down_days` (a list);
      - `day_points`, a list of `{"day", "forecast_kwh", "actual_kwh", "ratio", "weight", "down"}`;
      - `slot_points`, a list of `{"day", "minute", "forecast_kwh", "actual_kwh", "ratio", "weight"}`.
  - `band_scaling(evidence, today, pv_scaling, band_days) -> tuple[float | None, float | None, float]`:
    - Returns worst and best relative to the average, and the average. Both are None when no valid day is in the window.
    - The arithmetic is today's `solcast.py:1363-1397`: linear weights `max(1.0 - 0.1 * (age - 1), 0.3)`, ratio of weighted sums clamped to [0.1, 2.0], worst clamped to [0.3, 1.0], best capped at 2.0.
  - `evidence_peak_kw(evidence, today, days) -> float`: the largest `peak_kw` over days with `1 <= age <= days`.

- [ ] **Step 1: Write the failing tests** in `tests/test_pv_calibration.py`. Each is a `def test_*() -> bool` (True means failed), using a helper `make_day(ratio, peak=4.0, scale=1.0, noise=None)`. That helper builds 48 slots with a bell-shaped `forecast_kwh` (about 20 kWh a day × scale) and `actual = forecast × ratio`, multiplied per slot by `noise[slot]` if given.
  - `test_fit_steady_bias`: 28 days at ratio 0.8 → `abs(total - 0.8) < 0.03` and every daytime `adj` within 0.03 of 0.8.
  - `test_fit_calibrated_noise_stays_near_one`: 28 days whose day ratios alternate 0.6 / 1.4 (mean 1) → `abs(math.log(total)) < 0.05`.
  - `test_fit_large_tau_is_ratio_of_sums`: `shrink_day = shrink_slot = 1e6` → `total` equals `Σw·a/Σw·f` computed in the test, within 1e-6.
  - `test_fit_down_days_excluded`: 10 days at 0.8, plus 5 days with `actual < 0.1 × forecast` → those 5 are in `down_days`, and `total` is within 0.03 of 0.8.
  - `test_fit_needs_three_days`: 2 valid days → None.
  - `test_fit_pv_scaling_applied`: 28 days at ratio 0.8 with `pv_scaling=0.5` → `total` within 0.03 of 1.6.
  - `test_fit_sparse_slot_uses_day_factor`: a slot forecast ≥ 0.02 kWh on only 2 days → `adj[slot] == total`.
  - `test_fit_clamps`: ratio 10 → `total <= PV_CALIBRATION_HIGHEST`.
  - `test_band_matches_old_arithmetic`: 7 days with ratios 0.5–1.1 → worst, best and average equal the formula of `solcast.py:1363-1397`, computed in the test.
  - `test_merge_replaces_and_ages`: stored `{d-30, d-5}` plus scanned `{d-5 (new), d-1}` with max age 28 → keys `{d-5, d-1}`, and d-5 is the scanned copy.
  - `test_merge_drops_today_and_future`: scanned days `today` and `today + 1` → both absent.
  - `test_clean_drops_malformed`: a mix of good days, a day with non-list slots, a non-dict and a string → only the good days come back, with int minute keys.
  - `test_scan_length`: None → 28; 1 day after a success → 2; 5 days after → 6; 40 days after → 28.

- [ ] **Step 2: Run them to see them fail**
  - Run: `cd coverage && ./run_all --test pv_calibration > /tmp/pvc1.log 2>&1; grep -n "ERROR\|Error\|failed" /tmp/pvc1.log | head`
  - Expected: an import error for `pv_calibration`.

- [ ] **Step 3: Implement the module.** The fit's shrink step is pinned by the spec:

```python
def shrunk_log(values, prior_sd, center):
    """Log factor shrunk toward 0: centre x tau^2 / (tau^2 + se^2); se from the weighted spread of the per-day log ratios."""
    sw = sum(w for _, w in values)
    if sw <= 0 or len(values) < PV_CALIBRATION_MIN_SLOT_POINTS:
        return 0.0
    mean = sum(v * w for v, w in values) / sw
    var = sum(w * (v - mean) ** 2 for v, w in values) / sw
    n_eff = sw * sw / sum(w * w for _, w in values)
    se2 = var / max(n_eff - 1, 1)
    return center * prior_sd**2 / (prior_sd**2 + se2)
```

  - **Day values:** `(clamp(ln(max(a,0.01)/f), ±LOG_CLAMP), w·f)` with `center = ln(Σw·a / Σw·f)`.
  - **Slot values:** use only days with slot `f >= PV_CALIBRATION_MIN_SLOT_KWH`. Each value is `(clamp(ln(max(a,0.005)/f/total), ±LOG_CLAMP), w·f)`, with `center = ln(Σw·a / Σw·f / total)`.
  - **Weights:** `w = 0.5 ** ((age - 1) / half_life)`; `f` is already × `pv_scaling`.
  - **Down day:** missing, `f < 0.1`, or `a < 0.1·f` (as `solcast.py:1297-1301`).
  - The unshrunk `raw_total`/`raw_adj` are `exp(center)`, clamped the same way.

- [ ] **Step 4: Run them to see them pass**
  - Run: `cd coverage && ./run_all --test pv_calibration > /tmp/pvc1.log 2>&1; tail -5 /tmp/pvc1.log`
  - Expected: no ERROR lines; the suite reports pass.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/pv_calibration.py apps/predbat/const.py apps/predbat/solcast.py apps/predbat/tests/test_pv_calibration.py apps/predbat/unit_test.py
git commit -m "feat(solar): shrunk PV calibration fit and evidence helpers"
```

### Task 2: History scan, settings and the `tracked` passthrough

**Files:**
- Modify: `apps/predbat/fetch.py:1055` `minute_data_import_export(..., tracked=True)`: pass `tracked` to `get_history_wrapper` (line ~1082).
- Modify: `apps/predbat/solcast.py:1111` `pv_forecast_history(self, days, tracked=True)`: pass `tracked` to both `get_history_wrapper` calls.
- Modify: `apps/predbat/solcast.py`: add `pv_calibration_settings()` and `pv_calibration_scan()`.
- Modify: `apps/predbat/config.py` `APPS_SCHEMA`, next to `battery_rate_grid_scan_days`:
  - `"pv_calibration_history_days": {"type": "integer", "zero": False}`
  - `"pv_calibration_scan_days": {"type": "integer", "zero": False}`
  - `"pv_calibration_half_life": {"type": "float", "zero": False}`
  - `"pv_calibration_shrink": {"type": "float", "zero": False}`
  - `"pv_calibration_shrink_slot": {"type": "float", "zero": False}`
- Modify: `docs/apps-yaml.md` (a "PV calibration" block beside the rate grid settings: the five keys, defaults and meaning, as in the spec table) and the PV calibration section of `docs/customisation.md` (the new behaviour in plain words; the 7-day description is replaced).
- Modify: the test mocks of `minute_data_import_export` / `pv_forecast_history` in `tests/test_pv_calibration_clipping.py` and `tests/test_solcast.py` now accept `**kwargs`.
- Test: `apps/predbat/tests/test_pv_calibration_scan.py`, registered `("pv_calibration_scan", run_pv_calibration_scan_tests, "PV calibration: history scanned into per-slot day evidence", False)`.

**Interfaces:**
- Consumes: the Task 1 evidence day shape.
- Produces:
  - `SolarAPI.pv_calibration_settings() -> dict`:
    - Keys `history_days`, `scan_days`, `half_life`, `shrink`, `shrink_slot`, read by `self.get_arg(<apps.yaml key>, default)`.
    - Plus `overridden` (a set of the keys the user set).
    - A non-positive or non-numeric value falls back to the default with a `Warn:` log.
    - `scan_days` is clamped to `history_days`.
    - `history_days < 3` logs a `Warn:` that calibration needs at least 3 days.
  - `SolarAPI.pv_calibration_scan(days, tracked=True) -> dict | None`:
    - Returns complete past days as `{date: evidence_day}`, or None when the history read failed (no `pv_today` history).
    - Reads `minute_data_import_export(days + 1, self.now_utc, "pv_today", required_unit="kWh", pad=False, tracked=tracked)` and `pv_forecast_history(days + 1, tracked=tracked)`. It converts them exactly as `solcast.py:1245-1263` does today: power from increments, `history_attribute_to_minute_data`, then `impute_clipped_pv_history`.
    - The day of a minute-ago is `charge_day(self.now_utc, minute, self.base.local_tz)` (from `battery_curve`). The slot is `((self.minutes_now - minute) % 1440)` floored to `self.plan_interval_minutes`.
    - `forecast_kwh` is divided by `self.pv_scaling`.
    - `actual_kwh` comes from the imputed series. `peak_kw` is the day's measured maximum, and `clipped_minutes` counts the minutes where imputed and measured differ.
    - A day is stored only when the forecast covers ≥ 40 of its slots and the `pv_today` history starts before that day's local midnight.
    - With `self.pv_scaling <= 0` it returns `{}` and logs why.

- [ ] **Step 1: Write the failing tests.** Use `create_predbat()`'s solar component, as `test_pv_calibration_clipping.calibrate()` builds it, with mocked readers.
  - `test_scan_builds_days`: 5 days of a 4 kW bell forecast at 0.9 actual → 5 dates.
    - Each has 48 slots.
    - The summed `actual_kwh / forecast_kwh` is within 0.01 of 0.9.
    - `peak_kw` is within 0.05 of 3.6.
  - `test_scan_forecast_before_pv_scaling`: `pv_scaling = 2.0` and a mocked `pv_forecast_history` that returns kW already × 2 → the stored `forecast_kwh` equals the unscaled bell's kWh.
  - `test_scan_pv_scaling_zero_stores_nothing`: `pv_scaling = 0` → `{}`.
  - `test_scan_skips_partially_covered_day`: `pv_today` history starting at 12:00 three days ago → that date is absent, the two later dates are present.
  - `test_scan_tracked_false_passed`: the mocks record kwargs → `tracked is False` reaches both readers.
  - `test_scan_history_missing_returns_none`: `minute_data_import_export` returns `{}` → None.
  - `test_settings_defaults_and_overrides`: no args gives `{28, 2, 3.5, 0.20, 0.20}` and an empty `overridden`; with `pv_calibration_shrink: 0.3` set, `shrink == 0.3` and `"shrink" in overridden`.
  - `test_settings_clamp_and_warn`: `history_days=2, scan_days=5` → `scan_days == 2`, and both a `Warn:` about 3 days and a `Warn:` for an invalid `half_life=-1` (falling back to 3.5) are logged.

- [ ] **Step 2: Run them to see them fail**
  - Run: `cd coverage && ./run_all --test pv_calibration_scan > /tmp/pvc2.log 2>&1; grep -n "ERROR\|Error" /tmp/pvc2.log | head`
  - Expected: `AttributeError` for `pv_calibration_scan`.

- [ ] **Step 3: Implement** the two `tracked` passthroughs, the schema entries, `pv_calibration_settings()`, `pv_calibration_scan()` and the doc changes.

- [ ] **Step 4: Run them to see them pass.** Also run the suites whose mocks changed.
  - Run: `cd coverage && ./run_all --test pv_calibration_scan --test pv_calibration_clipping --test solcast > /tmp/pvc2.log 2>&1; grep -n "ERROR" /tmp/pvc2.log | head; tail -3 /tmp/pvc2.log`
  - Expected: no ERROR lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/fetch.py apps/predbat/solcast.py apps/predbat/config.py docs/apps-yaml.md docs/customisation.md apps/predbat/tests/test_pv_calibration_scan.py apps/predbat/tests/test_pv_calibration_clipping.py apps/predbat/tests/test_solcast.py apps/predbat/unit_test.py
git commit -m "feat(solar): scan PV history into per-slot calibration evidence; pv_calibration_* settings"
```

### Task 3: Evidence store update

**Files:**
- Modify: `apps/predbat/solcast.py`: add `async pv_calibration_update_evidence()`; initialise its state in the component's init; call it in `fetch_pv_forecast` immediately before `self.pv_calibration(...)` (~line 1965).
- Test: `apps/predbat/tests/test_pv_calibration_update.py`, registered `("pv_calibration_update", run_pv_calibration_update_tests, "PV calibration: evidence storage, scan schedule and save guards", False)`.

**Interfaces:**
- Consumes:
  - `pv_calibration_settings()` and `pv_calibration_scan()` (Task 2);
  - `clean_pv_calibration_evidence`, `merge_pv_calibration_evidence`, `pv_calibration_scan_length` (Task 1).
- Produces these `SolarAPI` attributes, read by Tasks 4 and 5:
  - `pv_calibration_evidence: dict | None`. None until the first update has run.
  - `pv_calibration_last_scan: datetime | None` and `pv_calibration_last_success: datetime | None`.
  - `pv_calibration_next_scan: datetime | None`.
  - `pv_calibration_scan_status: str`, one of `"not run"`, `"ok: N days"`, `"history unavailable, retry at HH:MM"`.
  - `pv_calibration_loaded: bool`.

- [ ] **Step 1: Write the failing tests.** Use a fake storage object with async `load`/`save` that records calls and can be set to return None or raise; mock `pv_calibration_scan`; drive `now_utc` forward between calls.
  - `test_update_first_scan_reads_history_days`: an empty store → the scan is called with `days == 28` and `tracked=False`, and a save happens with expiry `now + 60 days`, namespace `"solcast"`, key `"pv_calibration_evidence"`, `slot_minutes == 30`.
  - `test_update_daily_scan_reads_two`: a second call 24 h later → `days == 2`. A call 1 h later → no scan.
  - `test_update_failed_scan_retries_after_an_hour`:
    - The scan returns None → `next_scan == now + 60 min`, and the stored evidence is unchanged.
    - At +59 min there is no scan; at +60 min there is one.
  - `test_update_save_waits_for_load`:
    - `load` returns None twice, 5 minutes apart → no save.
    - A third failure ≥ 10 minutes after the first → a save is allowed.
    - This matches `battery_rate_grid_update`.
  - `test_update_merges_with_stored`: a stored document with days d-10..d-3 plus a scanned d-2, d-1 → `pv_calibration_evidence` holds all of them.
  - `test_update_discards_other_slot_minutes`: a stored document with `slot_minutes = 15` → it is ignored (logged), and the next save writes 30.
  - `test_update_resaves_daily`: unchanged evidence 24 h after the last save → it is saved again (the expiry moves, #6225).
  - `test_update_without_storage`: `storage = None` → the evidence is kept in memory, and the scan schedule still applies.

- [ ] **Step 2: Run them to see them fail**
  - Run: `cd coverage && ./run_all --test pv_calibration_update > /tmp/pvc3.log 2>&1; grep -n "ERROR\|Error" /tmp/pvc3.log | head`
  - Expected: `AttributeError` for `pv_calibration_update_evidence`.

- [ ] **Step 3: Implement it** by following the shape of `fetch.py` `battery_rate_grid_update` (lines 4199-4330): the load closure, failure timestamps, the `store_ready` rule, the resave interval and `pending_save`. Use the PV constants.

- [ ] **Step 4: Run them to see them pass**
  - Run: `cd coverage && ./run_all --test pv_calibration_update > /tmp/pvc3.log 2>&1; grep -n "ERROR" /tmp/pvc3.log; tail -3 /tmp/pvc3.log`
  - Expected: no ERROR lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/solcast.py apps/predbat/tests/test_pv_calibration_update.py apps/predbat/unit_test.py
git commit -m "feat(solar): keep 28 days of PV calibration evidence in storage"
```

### Task 4: `pv_calibration()` uses the evidence and the shrunk fit

**Files:**
- Modify: `apps/predbat/solcast.py:1219-1434` (the history read and factor computation inside `pv_calibration`)
- Modify: `apps/predbat/tests/test_solcast.py`, `tests/test_pv_calibration_clipping.py`, `tests/test_pv_calibration_day_scaling.py`, where an expectation came from the 7-day estimator
- Test: new cases in `apps/predbat/tests/test_pv_calibration_scan.py` (they share its harness)

**Interfaces:**
- Consumes:
  - `fit_pv_calibration`, `band_scaling`, `evidence_peak_kw` (Task 1);
  - `pv_calibration_scan`, `pv_calibration_settings` (Task 2);
  - `pv_calibration_evidence` (Task 3).
- Produces these `SolarAPI` attributes, read by Task 5:
  - `pv_calibration_model: dict | None` (the fit result);
  - `pv_calibration_settings_used: dict`;
  - the existing `pv_calibration_worst_scaling`, `pv_calibration_best_scaling`, `pv_calibration_average_scaling` and `pv_calibration_total_adjustment`, with unchanged meaning.

- [ ] **Step 1: Write the failing tests** in `test_pv_calibration_scan.py`.
  - `test_calibration_sunny_after_dull_not_cut`:
    - The evidence is 23 days at ratio 0.95 (sunny bell, 20 kWh forecast), then the last 5 days forecast 12 kWh with actual 6 kWh (ratio 0.5): sunny forecasts that turned out dull.
    - Assert `total_adjustment >= 0.72`. A hand calculation gives about 0.76; today's 7-day estimator gives about 0.62. Print it.
    - Tomorrow's calibrated total is still raw × `total_adjustment`.
  - `test_calibration_uses_store_not_history`: with `pv_calibration_evidence` set, the history readers are never called (the mocks raise).
  - `test_calibration_falls_back_to_scan`: with `pv_calibration_evidence = None`, `pv_calibration_scan` runs once with `days == settings["history_days"]`.
  - `test_calibration_band_from_last_seven_days`: the evidence has 20 days at 0.9, then the last 7 days at 0.6–1.0 → `pv_calibration_worst_scaling` and `best_scaling` equal `band_scaling(..., 7)`.
  - `test_calibration_off_under_three_days`: 2 valid days → the factors are all 1.0, and worst/best are 0.7/1.3, as today.
  - `test_calibration_peak_from_last_seven_days`: a 6 kW peak 20 days ago and 3 kW in the last 7 → the existing log line reads `max historical power 3.0`.

- [ ] **Step 2: Run them to see them fail**
  - Run: `cd coverage && ./run_all --test pv_calibration_scan > /tmp/pvc4.log 2>&1; grep -n "ERROR" /tmp/pvc4.log | head`
  - Expected: failures on the new tests.

- [ ] **Step 3: Implement it.**
  - Replace the history pull and the slot, day and band sums (current lines 1239-1434) with:
    1. `settings = self.pv_calibration_settings()`;
    2. `evidence = self.pv_calibration_evidence`, or `self.pv_calibration_scan(settings["history_days"])` when None;
    3. `today = charge_day(self.now_utc, 0, self.base.local_tz)`;
    4. `fit_pv_calibration(...)`, `band_scaling(..., PV_CALIBRATION_BAND_DAYS)`, `evidence_peak_kw(..., PV_CALIBRATION_BAND_DAYS)`.
  - Map the model onto the existing variable names: `slot_adjustment`, `total_adjustment`, `worst_day_scaling`, `best_day_scaling`, `average_day_scaling`, `max_pv_power_hist`. The code from `days_forecast_total` onwards then runs unchanged.
  - Keep the existing log lines and the `metrics()` sets. Add one log line: `"SolarAPI: PV Calibration: day factor {applied} (unshrunk {raw}, se {se}) from {n} days, half-life {h}, shrink {s}/{ss}"`.

- [ ] **Step 4: Run all the calibration suites.** For each failure in the pre-existing suites, decide:
  - **The expectation came from the 7-day estimator** (a mixed-ratio week): update the expected value with a comment naming this design, and record a `Ruling:` in the ledger.
  - **A genuine regression** (constant-ratio weeks must still give exact ratios; the cap and band tests must still hold): fix the code.
  - Run: `cd coverage && ./run_all --test pv_calibration_scan --test pv_calibration --test pv_calibration_clipping --test pv_calibration_day_scaling --test solcast > /tmp/pvc4.log 2>&1; grep -n "ERROR" /tmp/pvc4.log; tail -3 /tmp/pvc4.log`
  - Expected: no ERROR lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/solcast.py apps/predbat/tests/test_pv_calibration_scan.py apps/predbat/tests/test_solcast.py apps/predbat/tests/test_pv_calibration_clipping.py apps/predbat/tests/test_pv_calibration_day_scaling.py
git commit -m "feat(solar): PV calibration from the 28-day store with shrunk factors"
```

### Task 5: PV Calibration page replaces PVAccuracy

**Files:**
- Modify: `apps/predbat/web.py`:
  - **`get_chart` (~3497):** `chart in ("PVCalibration", "PVAccuracy")` returns `self.get_pv_calibration_chart()` before `now_str`, as Curves does.
  - **The old PVAccuracy branch (~3677-3695):** removed.
  - **The menu (~4008):** the PVAccuracy link becomes `PVCalibration`, active for either name.
  - **`html_charts`:** `if chart not in (...)` also excludes `"PVCalibration"` and `"PVAccuracy"`.
  - **New:** `get_pv_calibration_chart()`, beside `get_curves_chart`.
- Modify: `docs/web-interface.md:140` (the PVAccuracy line becomes a PVCalibration description).
- Test: `apps/predbat/tests/test_web_pv_calibration_chart.py`, registered `("web_pv_calibration_chart", run_web_pv_calibration_chart_tests, "PV Calibration page: status table, slot and day evidence charts", False)`.

**Interfaces:**
- Consumes the solar component, `self.base.components.get_component("solar")` (None-safe), and its attributes:
  - `pv_calibration_model`, `pv_calibration_evidence`;
  - `pv_calibration_last_scan`, `pv_calibration_next_scan`, `pv_calibration_scan_status`;
  - `pv_calibration_settings_used`;
  - `pv_calibration_worst_scaling`, `pv_calibration_best_scaling`;
  - `pv_clipping_ceiling_kw()`;
  - and `self.base.get_arg("metric_pv_calibration_enable", True)`.
- Produces, embedded in the page for tests and Plotly:
  - `var pv_calibration_slot_data = <json>;`: `{"points": [[minute, ratio, forecast_kwh, weight], ...], "raw_adj": {...}, "adj": {...}}`;
  - `var pv_calibration_day_data = <json>;`: `{"points": [[day, ratio, forecast_kwh, down], ...], "raw_total": x, "total": y}`.

- [ ] **Step 1: Write the failing tests.** Use the `_make_web` and `_check` pattern from `tests/test_web_curves_chart.py`, with a stub solar component whose attributes are set directly.
  - `test_page_with_model`: the status table contains:
    - "On";
    - the applied and unshrunk day factors formatted to 2 dp;
    - "3.5" and "0.20";
    - the last scan time.
  - `test_page_slot_data`: parse `pv_calibration_slot_data` → the points count equals the model's `slot_points`, and `adj["720"]` equals the model value.
  - `test_page_day_data`: parse `pv_calibration_day_data` → a down day carries `down: true`.
  - `test_page_overridden_marked`: `"shrink" in overridden` → the shrink cell shows "(apps.yaml)".
  - `test_page_without_evidence`: model None and evidence `{}` → it shows "not enough evidence" and the reason, with no exception and no Plotly data blocks.
  - `test_page_without_solar_component`: `components = None` → it shows "Solar forecast component is not running".
  - `test_pvaccuracy_alias`: `web.get_chart("PVAccuracy") == web.get_chart("PVCalibration")`.
  - `test_page_wraps`: the html contains `.pv-calibration-page p { white-space: normal; }` and `max-width:960px`.

- [ ] **Step 2: Run them to see them fail**
  - Run: `cd coverage && ./run_all --test web_pv_calibration_chart > /tmp/pvc5.log 2>&1; grep -n "ERROR" /tmp/pvc5.log | head`
  - Expected: failures, because `get_pv_calibration_chart` is missing.

- [ ] **Step 3: Implement the page.**
  - **Layout:** a scoped `<style>.pv-calibration-page p { white-space: normal; }</style>`, then a `div.pv-calibration-page` capped at 960 px.
  - **The status table rows** are as in spec §5.
  - **Plotly** loads via `PLOTLY_GL3D_URL`, which includes 2D scatter.
  - **Slot chart:**
    - x axis: `HH:MM`; y axis clamped 0–3 for display.
    - Points sized `6 + 10 × forecast_kwh / max`, with opacity `max(weight, 0.15)`.
    - Lines: unshrunk and applied, plus a dashed line at 1.0.
  - **Day chart:** the same styling. Down days are drawn hollow and grey.
  - **Two-hour band table:** columns `Time`, `Forecast kWh`, `Actual kWh`, `Unshrunk`, `Applied`, `Points`. The means are over valid days.

- [ ] **Step 4: Run it together with the existing web suites**
  - Run: `cd coverage && ./run_all --test web_pv_calibration_chart --test web_charts --test web_curves_chart > /tmp/pvc5.log 2>&1; grep -n "ERROR" /tmp/pvc5.log; tail -3 /tmp/pvc5.log`
  - Expected: no ERROR lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/web.py docs/web-interface.md apps/predbat/tests/test_web_pv_calibration_chart.py apps/predbat/unit_test.py
git commit -m "feat(web): PV Calibration page replaces PVAccuracy"
```

### Task 6: Whole-suite verification

**Files:** only those a failing check points to. Typical candidates are `coverage/cases/random_results.json` and `.cspell/custom-dictionary-workspace.txt`.

- [ ] **Step 1: Run the quick suite**
  - Run: `cd coverage && ./run_all --quick > /tmp/pvc6.log 2>&1; grep -n "ERROR\|FAILED\|failed" /tmp/pvc6.log | head -30; tail -5 /tmp/pvc6.log`
  - Expected: all pass. For any golden or baseline difference (`debug_cases`, `random_results`), confirm it comes from the new factors (calibration now differs on mixed weeks). Then regenerate it the way that suite's docs say, and record a `Ruling:` with the before and after day factor.
- [ ] **Step 2: Run pre-commit**
  - Run: `cd coverage && ./run_pre_commit > /tmp/pvc6pc.log 2>&1; tail -20 /tmp/pvc6pc.log`
  - Expected: all hooks pass. Re-stage whatever the hooks rewrite (the dictionary is auto-sorted).
- [ ] **Step 3: Check the blast radius.** Run `detect_changes({scope: "compare", base_ref: "main"})` and confirm the changed symbols are limited to:
  - `solcast.py` `SolarAPI.*calibration*`, `pv_forecast_history`, `fetch_pv_forecast`;
  - `fetch.py` `minute_data_import_export`;
  - `web.py` `get_chart`, `html_charts`, `get_pv_calibration_chart`;
  - `config.py` and `const.py`;
  - the new module and the tests.
- [ ] **Step 4: Commit any fixes**

```bash
git add <paths the checks touched>
git commit -m "test: regenerate baselines for the shrunk PV calibration"
```
