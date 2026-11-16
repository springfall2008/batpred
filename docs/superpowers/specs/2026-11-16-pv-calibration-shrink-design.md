# PV calibration: a 28-day evidence store with self-limiting factors

Status: design, approved approach (16 Nov 2026). Implementation plan to follow.

## Why

`SolarAPI.pv_calibration()` (`apps/predbat/solcast.py`) scales the solar forecast by how the last 7 days of actual generation compared with the forecast. It has two faults.

1. **A few dull days drag down the next sunny one.**
   - The window is 7 days, with linear weights from 1.0 down to 0.4.
   - `days_use_scaling` then forces every future day's total to raw × the 7-day ratio, whatever that day is forecast to be.
   - After a dull spell, a sunny day is under-forecast. On one site with Forecast.Solar, that was −18% on sunny days that followed dull ones.
2. **It makes an already-calibrated forecast worse.**
   - Some providers calibrate per site themselves, such as the Predbat Cloud forecast.
   - Predbat's correction on top of theirs is mostly noise.
   - On a 100-site sample it raised day error from 19.3% to 20.6%, and made 50 of 92 sites worse than the raw forecast.

## Evidence

These are backtests against recorded history, with aggregate results only. Errors are mean absolute error as a percentage of actual energy, given as day / half-hour where both are shown.

| Method | Calibrated sites, 100-site sample, Nov | Biased sites, 12 sites, Jul–Nov | Raw Forecast.Solar site, Oct–Nov |
|---|---|---|---|
| Raw forecast | 19.3 / 44.0 | 18.6 / 35.4 | 27.9 / 55.0 |
| Today (7 days) | 20.6 / 45.0 | 14.5 / 31.9 | 34.9 / 52.7 |
| 28 days, half-life 3.5, no shrink | 19.5 / 43.2 | 13.9 / 30.9 | 32.6 / 52.3 |
| **This design** | **19.0 / 42.9** | **13.9 / 30.8** | **30.2 / 53.2** |

Further results:
- **Sunny days after 3 dull days** on the raw Forecast.Solar site: 7.9% day error, against 18.4% today.
- **Calibrated sites made worse than raw** by more than 1% of energy: 8 of 92, against 50 today.
- **The weak case is a large bias that keeps growing.** In November a rebuilt raw forecast drifted from about +10% to +30% high (low sun, likely shading). This design lags that drift a little more than today's 7-day window: 29.8% day error against 29.0%.
- **Tried and rejected:**
  - Learning only from clear or best slots. It is biased both ways: picking by actual pushes the factor up, picking by forecast pulls it down by regression to the mean.
  - A clipping ceiling inferred from power plateaus. It found real ceilings rarely, found false ones, and changed nothing.
  - Applying calibration only when a replay of the last 14 days said it would have helped. It added nothing over shrinking.
  - A signal-to-noise (James–Stein) form of shrinkage. It was no better than this one.

## Goals

- Stop a few dull days dragging down the next sunny day's forecast.
- Leave an already-calibrated forecast essentially alone, while still correcting real system error such as a misconfigured array, shading or inverter losses.
- Use one estimator for every provider. There is no per-provider switch.
- Make every tunable overridable in `apps.yaml`.
- Replace the **PVAccuracy** chart with a **PV Calibration** page in the style of the Curves page. It shows each half-hour slot's evidence points against the calibration.

## Non-goals

- **Unchanged:**
  - the P10/P90 band (worst and best day scaling);
  - clip imputation when `inverter_limit` is set (`impute_clipped_pv_history`);
  - the array ceiling cap;
  - `metric_pv_calibration_enable`;
  - the provider's own band handling (`calibrate_band`).
- **No ceiling inferred from plateaus.**
- **No change to how a day's total is applied.** `days_use_scaling` still sets each future day's total to raw × the day factor. What changes is how that factor and the slot shape are estimated.

## Anti-goals

- **A storage outage must not wipe the evidence.** This uses the same load-before-save guard as the battery rate grid.
- **A 28-day read must not stay in the history cache.** Long reads use `tracked=False`.
- **Calibration must never learn from its own output.** Evidence uses only the uncalibrated forecast (GH#5116).
- **A `pv_scaling` change must still take effect across the whole window at once.** The store holds the forecast before `pv_scaling`.

## Design

### 1. Evidence store (new module `apps/predbat/pv_calibration.py`, pure functions)

Like `battery_rate_grid.py`, the arithmetic lives in a module with no Predbat state, so it can be unit tested directly.

- **Record per local day:**
  - **Per plan slot:** `forecast_kwh` and `actual_kwh`, keyed by the minute of day of the slot start.
    - The forecast is the uncalibrated provider forecast before `pv_scaling`.
    - The actual is the clip-imputed actual, the same series today's calibration measures against.
  - **Per day:** `peak_kw` (the measured peak, not imputed) and `clipped_minutes`.
- **Location:**
  - Storage namespace `solcast`, key `pv_calibration_evidence`, saved as `{"days": {...}, "updated": iso, "last_scan": iso}`.
  - Expiry 60 days.
- **Which days are stored:**
  - Only complete past days; today is never stored.
  - Days older than `pv_calibration_history_days` are dropped.
  - A day returned by a scan replaces the stored copy of that day.
- **Scans:**
  - Until a scan succeeds, a scan reads `pv_calibration_history_days` (default 28), or as much history as exists. HA's recorder keeps 10 days unless `purge_keep_days` is raised, so the store fills up over time.
  - After that, it reads `pv_calibration_scan_days` (default 2) once a day, widened to cover any gap since the last success.
  - A failed read is retried after 60 minutes.
  - Reads use `tracked=False`.
  - `minute_data_import_export` and `pv_forecast_history` gain a `tracked` argument, defaulting to True.
- **Load and save guards:** saves wait until a load has succeeded, or until 3 failed loads spanning at least 10 minutes. This matches `battery_rate_grid_update`.
- **Where it runs:** a new async `SolarAPI.pv_calibration_update_evidence()` runs in `fetch_pv_forecast` before `pv_calibration()`. It does the load, scan, merge and save, and leaves the merged evidence on `self.pv_calibration_evidence`.

### 2. Fit: `fit_pv_calibration(evidence, today, pv_scaling, half_life, shrink_day, shrink_slot) -> model or None`

- **Down days** are excluded, by the same rule as today: no forecast, a forecast under 0.1 kWh, or an actual under 10% of the forecast.
- **Enough evidence:** with fewer than 3 valid days the fit returns None. Calibration is then off and the factors are 1.0, as today.
- **Weights:**
  - `w = 0.5 ** ((age - 1) / half_life)`, where age 1 is yesterday.
  - Forecasts are multiplied by the current `pv_scaling` before fitting.
- **Day factor:**
  1. Centre: `c = ln(Σ w·a / Σ w·f)`, the ratio of weighted sums, as today.
  2. For each day, `x_d = clamp(ln(a_d / f_d), ±1.5)`, weighted `ω_d = w·f_d`.
  3. Weighted mean `m`. Variance `v = Σ ω (x − m)² / Σ ω`. Effective count `n_eff = (Σ ω)² / Σ ω²`. Standard error `se² = v / max(n_eff − 1, 1)`.
  4. Shrink: `shrunk = c · τ² / (τ² + se²)`, with `τ = shrink_day`.
  5. `total = clamp(exp(shrunk), PV_CALIBRATION_LOWEST, PV_CALIBRATION_HIGHEST)`.
- **Slot shape** is computed relative to the day factor:
  - For each slot, the same formula, using only days whose slot forecast is at least 0.02 kWh.
  - `x = clamp(ln(a / f / total), ±1.5)`, with centre `ln(Σ w·a / Σ w·f / total)` and `τ = shrink_slot`.
  - `adj_slot = clamp(total · exp(shrunk), LOWEST, HIGHEST)`.
  - A slot with fewer than 3 points gets `adj_slot = total`.
- **Limit case:** as τ grows large the fit becomes the 28-day ratio of sums, which is today's estimator over a longer window. That is the property the tests pin.
- **The model** returns:
  - `total`, `adj`;
  - the unshrunk `raw_total` and `raw_adj`;
  - `se_day`, `days_used`, `oldest` and `newest`;
  - for the page, the per-day ratios and per-slot points with their weights.

### 3. Apply (`pv_calibration()`)

- **What it reads:** the in-function 7-day history pull and the slot and day sums are replaced by a read of `self.pv_calibration_evidence` and `fit_pv_calibration()`.
- **Factors:** `slot_adjustment` and `total_adjustment` come from the model.
- **Unchanged:** everything after the factors (`days_use_scaling`, the ceiling cap, the band, `calibrate_band`, metrics and logs).
- **Band:** `worst_day_scaling` and `best_day_scaling` are still the minimum and maximum day ratios over the last 7 valid days, divided by that 7-day weighted ratio, with today's linear weights and clamps. They are computed from the store, so the band behaves as it does today.
- **Peak:** the observed peak for the cap is the largest `peak_kw` in the last 7 stored days, as today. A 28-day peak would loosen the cap in winter.
- **State:** `self.pv_calibration_model` and the last scan's status are kept for the web page.

### 4. Settings (`APPS_SCHEMA`, documented in `docs/apps-yaml.md` and the PV calibration section of `docs/customisation.md`)

| Key | Default | Meaning |
|---|---|---|
| `pv_calibration_history_days` | 28 | Days of evidence kept, and how many days the first scan reads |
| `pv_calibration_scan_days` | 2 | Days a daily scan reads, widened after a gap |
| `pv_calibration_half_life` | 3.5 | Days for evidence weight to halve |
| `pv_calibration_shrink` | 0.20 | Prior strength for the day factor. Smaller leaves calibrated forecasts alone more; larger follows a real bias faster |
| `pv_calibration_shrink_slot` | 0.20 | The same, for the half-hour shape |

The constants live in `const.py`, like the `BATTERY_RATE_GRID_*` values. Values must be positive; an invalid value falls back to the default with a warning.

### 5. PV Calibration page (replaces PVAccuracy)

- **Menu:**
  - The `PVAccuracy` tab becomes `PVCalibration`.
  - `charts?chart=PVAccuracy` still resolves to the new page, so saved `default_page` links keep working.
  - The old PVAccuracy content (calibrated forecast so far against actual, today) is removed.
- **Built like `get_curves_chart`:**
  - It reads the solar component's `pv_calibration_model` and evidence, read-only.
  - It uses Plotly, loaded the way the Curves page loads it.
  - It is width-capped, and its text wraps (the Curves fix).
- **Status table:**
  - calibration on or off, and why it is off (switch off, or under 3 valid days);
  - days in the store, valid days used, and oldest and newest day;
  - last scan time and result, and the next scan;
  - the day factor applied against unshrunk, with ± se;
  - half-life and shrink strengths, marking any overridden in apps.yaml;
  - the worst and best band factors;
  - the clipping ceiling and imputed minutes, when `inverter_limit` is set.
- **Half-hour chart:**
  - x axis: slot time, 00:00–23:30.
  - y axis: actual ÷ forecast, clamped for display to 0–3.
  - Points: each valid day's slot ratio, sized by forecast energy and faded by recency weight.
  - Lines: the unshrunk slot factor, the applied slot factor, and a reference line at 1.0.
- **Day chart:**
  - x axis: date; y axis: day ratio.
  - Points: sized by forecast kWh.
  - Lines: the unshrunk and applied day factors.
- **Short table:** two-hour bands, giving mean forecast kWh, mean actual kWh, the unshrunk and applied factors, and the number of points.

### 6. Tests

- **Pure functions** in `tests/test_pv_calibration.py`:
  - Calibrated noise around 1 gives a factor near 1, with `|ln total| < 0.03`.
  - A steady 0.8 bias gives roughly 0.8 (within 0.03).
  - Large τ equals the ratio of sums.
  - Down days are excluded, and under 3 days returns None.
  - A slot with under 3 points gets the day factor, and clamps hold.
  - `pv_scaling` is applied at fit time.
  - Merge replaces scanned days and drops old ones; today is never stored.
  - The scan window is the first-scan length, then the daily length, widened after a gap.
  - Retry after a failure; the save guard waits for a load; `tracked=False` is passed.
- **`pv_calibration()` integration:**
  - The existing solcast calibration tests are updated where their expected numbers came from the 7-day estimator.
  - New cases: a dull spell then a sunny forecast is no longer cut by the dull ratio; the band and cap behave unchanged.
- **Web:** `test_web_charts` renders PVCalibration with and without evidence, and the PVAccuracy alias resolves.
- **Settings:** defaults and overrides are read, and invalid values fall back.
- **Full suite:** `./run_all --quick` passes. Any golden or baseline change from the new estimator is reviewed and regenerated with a written reason.

### 7. Behaviour change on upgrade

- **The store starts empty.** The first scan fills it from whatever history HA has, and calibration runs from the first cycle once there are 3 valid days, as today.
- **Factors become more conservative** on sites whose ratio is noisy.
- **Sites with a consistent bias** keep a correction close to today's.

## Left to the builder

Helper names, log wording, page styling, and the storage merge details, within the shapes above.
