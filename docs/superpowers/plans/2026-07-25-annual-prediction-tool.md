# Annual Prediction Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone tool that projects a year of household electricity costs using the real Predbat planning engine, reporting each month under three scenarios: no PV/battery, PV+battery without Predbat, and with Predbat.

**Architecture:** Six new flat modules in `apps/predbat/` plus one extracted shared solar helper. `annual.py` drives a headless `PredBat` object per sampled day — it does not re-implement the optimiser. Weather and tariff modules perform all HTTP and never touch `PredBat`; `annual.py` performs no HTTP. Predbat plans against the archived Open-Meteo forecast and is costed against ERA5 actuals.

**Tech Stack:** Python 3, `aiohttp` (async HTTP, already a dependency), `requests` (sync HTTP, already used by `octopus.py`), `pytz`, PyYAML, Predbat's own `Prediction` / `Plan` / `Fetch` mixins, `StorageLocalFiles` for caching.

**Spec:** `docs/superpowers/specs/2026-07-25-annual-prediction-tool-design.md`

## Global Constraints

- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every function *and* class needs one, including test functions.
- **Spelling:** British English (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit — re-stage after running pre-commit. `docs/superpowers/` is excluded from cspell; `docs/*.md` is **not**.
- **Variable naming:** `lower_case_with_underscores`.
- **String formatting:** this codebase uses `"...".format(...)`, not f-strings, in `apps/predbat/*.py`. Match it.
- **File header:** every new `apps/predbat/*.py` file starts with the five-line copyright block used by all existing modules.
- **Storage:** all caching goes through the Storage abstraction, never direct file access.
- **Tests:** every new module needs unit tests, registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`. Test signature is `def test_name(my_predbat):` returning a truthy value on failure. Tests must not perform network I/O.
- **Run tests from `coverage/`:** `cd coverage && source setup.csh` once, then `./run_all --test <name> > /tmp/out.txt 2>&1` and grep the file. Never pipe test output straight to grep.
- **Pre-commit:** the script lives at `coverage/run_pre_commit`, NOT the repo root. Run it (or `coverage/venv/bin/pre-commit run --files <files>`) and confirm every hook reports Passed with **no files modified** — the `black` and `file-contents-sorter` hooks rewrite files in place, so a first run that "passes" while rewriting has not passed. Re-stage and re-run until clean.
- **`git add` your new files BEFORE running pre-commit.** `pre-commit --all-files` enumerates via `git ls-files`, which skips untracked files, so a brand-new module is silently never checked and the run reports a false pass. Two tasks on this branch shipped `black` and `cspell` violations this way. Either `git add` first, or pass the paths explicitly with `--files`.
- **Report only what you ran.** Never state that a check passed unless you executed the command and read its output. These reports are the review's evidence base.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/predbat/solar_model.py` | **New.** Shared GTI→kW conversion extracted from `solcast.py`: SAPM cell temperature, azimuth conversion, trapezoidal hourly integration. |
| `apps/predbat/solcast.py` | **Modify.** `download_open_meteo_data()` delegates to `solar_model.py`. |
| `apps/predbat/annual_profiles.py` | **New.** Half-hourly domestic shape table, monthly weights, tilt constant. Data only, no logic. |
| `apps/predbat/annual_load.py` | **New.** `LoadProfileSource` interface; synthetic and Octopus-consumption implementations; cumulative `load_forecast` builder. |
| `apps/predbat/annual_weather.py` | **New.** Open-Meteo actuals + forecast archive clients, per-day PV series, monthly P10 ratios. |
| `apps/predbat/annual_tariff.py` | **New.** Per-date import/export rate resolution from an Octopus URL or basic rates. |
| `apps/predbat/annual.py` | **New.** `AnnualPredictor`: config validation, headless `PredBat` bootstrap, sample selection, three-scenario execution, aggregation. |
| `apps/predbat/annual_cli.py` | **New.** Argument parsing, progress output, JSON and table writing. |
| `apps/predbat/tests/test_annual_*.py` | **New.** One test module per new module above. |
| `docs/annual-prediction.md` | **New.** User documentation. |
| `mkdocs.yml` | **Modify.** Nav entry for the new doc page. |

---

## Task 1: Extract the shared GTI→kW solar model

`solcast.py` converts Open-Meteo GTI into PV kWh using a cell-temperature model and trapezoidal hourly integration. `annual_weather.py` needs the identical conversion. Copying it would guarantee drift, and the azimuth convention (Open-Meteo uses 0 = south, Predbat uses 180 = south) is easy to get silently wrong while still producing plausible output.

**Files:**
- Create: `apps/predbat/solar_model.py`
- Modify: `apps/predbat/solcast.py` (lines ~20-40 for the constants, ~243-255 for `convert_azimuth`, ~356-408 for the two-pass conversion)
- Create: `apps/predbat/tests/test_solar_model.py`
- Modify: `apps/predbat/unit_test.py`
- Do **not** modify: `apps/predbat/tests/test_open_meteo.py` — it is the parity guard for this refactor

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `solar_model.pvwatts_cell_temperature(poa_global, temp_air, wind_speed) -> float`
  - `solar_model.convert_azimuth(az) -> float`
  - `solar_model.gti_hourly_to_period_kwh(times, gti_values, temp_values, wind_values, kwp, system_loss, shading_factors=None, p10_instant=None, p10_fallback=0.7) -> dict[datetime, dict]` where each value is `{"pv_estimate": float, "pv_estimate10": float}` keyed by a tz-aware UTC hour-start stamp, giving kWh generated during that hour for **one** array.

- [ ] **Step 1: Record the current behaviour baseline**

This refactor touches live forecasting code. Parity is proven by the *existing* suite in `apps/predbat/tests/test_open_meteo.py`, which calls the real `download_open_meteo_data()` against mocked responses and asserts on its output, including temperature derating and multi-array summing. Capture its result now, before any edit:

Run: `cd coverage && ./run_all -k open_meteo > /tmp/t1_before.txt 2>&1; tail -20 /tmp/t1_before.txt`

Expected: the suite passes. Record which tests ran — Step 8 re-runs exactly these and they must still pass, unchanged. Do **not** modify `test_open_meteo.py` at any point in this task; a refactor that requires editing the test that guards it is not a refactor.

- [ ] **Step 2: Write the failing test**

Create `apps/predbat/tests/test_solar_model.py`. The expected values are hand-derived from the documented model rather than snapshotted, so a wrong constant fails the test instead of being baked into a fixture:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the shared solar GTI to kW conversion model."""

from datetime import datetime

import pytz

from solar_model import convert_azimuth, gti_hourly_to_period_kwh, pvwatts_cell_temperature

FLAT_TIMES = ["2025-06-01T{:02d}:00".format(hour) for hour in range(4)]


def stamp_for(text):
    """Return the tz-aware UTC datetime for an Open-Meteo timestamp string."""
    return pytz.utc.localize(datetime.strptime(text, "%Y-%m-%dT%H:%M"))


def test_solar_model(my_predbat):
    """Verify the shared solar model against hand-derived values."""
    failed = False
    print("**** Testing solar_model ****")

    print("Test: convert_azimuth maps the Predbat convention onto the Open-Meteo one")
    for predbat_az, expected in [(180, 0), (90, 90), (270, -90), (0, 180)]:
        result = convert_azimuth(predbat_az)
        if result != expected:
            print("  ERROR: convert_azimuth({}) expected {}, got {}".format(predbat_az, expected, result))
            failed = True

    print("Test: pvwatts_cell_temperature matches the SAPM formula")
    # T_cell = 25 + 1000*exp(-3.47 + -0.0594*0) + (1000/1000)*3.0
    #        = 25 + 1000*0.031117 + 3 = 59.117
    hot = pvwatts_cell_temperature(1000.0, 25.0, 0.0)
    if abs(hot - 59.117) > 0.001:
        print("  ERROR: cell temperature expected 59.117, got {}".format(hot))
        failed = True
    if pvwatts_cell_temperature(0.0, 20.0, 1.5) != 20.0:
        print("  ERROR: zero irradiance should give ambient temperature")
        failed = True

    print("Test: a constant-irradiance hour converts to the hand-derived energy")
    # eta = 1 - 0.004*(59.117 - 25) = 0.863532; pv = (1000/1000) * 1 kWp * eta * 1.0
    # Both endpoints are equal so the trapezoid returns the same value.
    flat_gti = [1000.0] * 4
    flat_temp = [25.0] * 4
    flat_wind = [0.0] * 4
    result = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0)
    if len(result) != 3:
        print("  ERROR: 4 samples should yield 3 integrated periods, got {}".format(len(result)))
        failed = True
    first = result.get(stamp_for(FLAT_TIMES[0]))
    if first is None:
        print("  ERROR: missing the first period")
        failed = True
    elif abs(first["pv_estimate"] - 0.8635) > 0.0001:
        print("  ERROR: expected 0.8635 kWh, got {}".format(first["pv_estimate"]))
        failed = True

    print("Test: cold panels are allowed to exceed their STC rating")
    # T_cell = 0 + 200*exp(-3.47 - 0.0594) + 0.6 = 6.4645; eta = 1.074142 (above 1.0)
    cold = gti_hourly_to_period_kwh(FLAT_TIMES, [200.0] * 4, [0.0] * 4, [1.0] * 4, kwp=1.0, system_loss=0.0)
    cold_first = cold[stamp_for(FLAT_TIMES[0])]
    if abs(cold_first["pv_estimate"] - 0.2148) > 0.0001:
        print("  ERROR: expected 0.2148 kWh for cold panels, got {}".format(cold_first["pv_estimate"]))
        failed = True

    print("Test: the trapezoid integrates a rising ramp to the mean of its endpoints")
    ramp = gti_hourly_to_period_kwh(FLAT_TIMES, [0.0, 1000.0, 1000.0, 0.0], [25.0] * 4, [0.0] * 4, kwp=1.0, system_loss=0.0)
    # Endpoints 0.0 and 0.8635 average to 0.43175, rounded to 4 places
    if abs(ramp[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - 0.4318) > 0.0001:
        print("  ERROR: expected 0.4318 kWh across the sunrise hour, got {}".format(ramp[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    print("Test: zero irradiance produces zero energy")
    dark = gti_hourly_to_period_kwh(FLAT_TIMES, [0.0] * 4, [15.0] * 4, [1.0] * 4, kwp=5.0, system_loss=0.05)
    if any(entry["pv_estimate"] != 0.0 for entry in dark.values()):
        print("  ERROR: zero irradiance should give zero energy, got {}".format(dark))
        failed = True

    print("Test: system_loss and kwp scale the output linearly")
    scaled = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=2.0, system_loss=0.5)
    if abs(scaled[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - first["pv_estimate"]) > 0.0001:
        print("  ERROR: doubling kwp and halving efficiency should cancel out, got {}".format(scaled[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    print("Test: p10_fallback scales the P10 series and defaults to 0.7")
    if abs(first["pv_estimate10"] - round(first["pv_estimate"] * 0.7, 4)) > 0.0001:
        print("  ERROR: the default P10 fallback should be 0.7, got {}".format(first["pv_estimate10"]))
        failed = True
    half = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_fallback=0.5)
    half_first = half[stamp_for(FLAT_TIMES[0])]
    if abs(half_first["pv_estimate10"] - round(half_first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: p10_fallback 0.5 not applied, got {}".format(half_first["pv_estimate10"]))
        failed = True

    print("Test: p10_instant overrides the fallback and is capped at P50")
    ensemble = {FLAT_TIMES[index]: 0.1 for index in range(4)}
    with_ensemble = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_instant=ensemble)
    ensemble_first = with_ensemble[stamp_for(FLAT_TIMES[0])]
    if ensemble_first["pv_estimate10"] >= ensemble_first["pv_estimate"]:
        print("  ERROR: an ensemble P10 below P50 should stay below it, got {}".format(ensemble_first["pv_estimate10"]))
        failed = True
    huge = {FLAT_TIMES[index]: 99.0 for index in range(4)}
    capped = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_instant=huge)
    capped_first = capped[stamp_for(FLAT_TIMES[0])]
    if abs(capped_first["pv_estimate10"] - capped_first["pv_estimate"]) > 0.0001:
        print("  ERROR: an ensemble P10 above P50 should be capped at P50, got {}".format(capped_first["pv_estimate10"]))
        failed = True

    print("Test: shading_factors apply the correct month")
    shaded = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, shading_factors=[0.5] * 12)
    if abs(shaded[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - round(first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: a 0.5 shading factor was not applied, got {}".format(shaded[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    print("Test: a gap in the timestamps is not integrated across")
    gapped_times = ["2025-06-01T00:00", "2025-06-01T01:00", "2025-06-01T05:00"]
    gapped = gti_hourly_to_period_kwh(gapped_times, [1000.0] * 3, [25.0] * 3, [0.0] * 3, kwp=1.0, system_loss=0.0)
    if len(gapped) != 1:
        print("  ERROR: only the contiguous hour pair should integrate, got {} periods".format(len(gapped)))
        failed = True

    print("Test: a None irradiance sample is treated as zero rather than raising")
    with_none = gti_hourly_to_period_kwh(FLAT_TIMES, [None, 1000.0, 1000.0, None], [25.0] * 4, [0.0] * 4, kwp=1.0, system_loss=0.0)
    if abs(with_none[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - 0.4318) > 0.0001:
        print("  ERROR: a None sample should behave as zero, got {}".format(with_none[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    return failed
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test solar_model > /tmp/t1.txt 2>&1; grep -E "ERROR|FAILED|ModuleNotFound" /tmp/t1.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'solar_model'`. The test is not yet registered, so first add it (Step 5) if `--test solar_model` reports an unknown test name.

- [ ] **Step 4: Create `apps/predbat/solar_model.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Shared photovoltaic conversion model.

Converts Open-Meteo global tilted irradiance (GTI) into PV energy, applying a
SAPM/PVWatts cell-temperature derate and integrating each hourly sample pair.
Shared by the live Solcast/Open-Meteo forecast path and the annual prediction
tool so the two cannot drift apart.
"""

import math
from datetime import datetime, timedelta

import pytz

from utils import dp4

# PVWatts / SAPM cell temperature model constants (glass/glass, open rack)
# Equivalent to pvlib.temperature.sapm_cell with open_rack_glass_glass parameters
_SAPM_A = -3.47
_SAPM_B = -0.0594
_SAPM_DELTA_T = 3.0

# c-Si temperature coefficient: -0.4%/degC relative to STC (25degC)
_TEMP_COEFF = 0.004
_STC_TEMP_C = 25.0

# Defaults used when a sample has no measured value
_DEFAULT_TEMP_C = 25.0
_DEFAULT_WIND_MS = 1.0

# Applied when no ensemble P10 data is available
_DEFAULT_P10_FALLBACK = 0.7


def pvwatts_cell_temperature(poa_global, temp_air, wind_speed):
    """Compute PV cell temperature using the SAPM (PVWatts) model.

    Parameters correspond to a glass/glass module on an open rack (the most
    common residential case). Formula: T_cell = T_air + GTI*exp(a + b*wind) + (GTI/1000)*deltaT
    """
    return temp_air + poa_global * math.exp(_SAPM_A + _SAPM_B * wind_speed) + (poa_global / 1000.0) * _SAPM_DELTA_T


def convert_azimuth(az):
    """
    Convert azimuth from Predbat/Solcast convention to Forecast.solar/Open-Meteo convention.
    Predbat/Solcast convention:         0 = North, -90 = East, 90 = West, 180 = South
    Forecast.solar/Open-Meteo convention: 0 = South, -90 = East, 90 = West, +/-180 = North
    """
    if az >= 0:
        az = 180 - az
    else:
        az = -180 - az

    return az


def _temperature_efficiency(gti, temp, wind):
    """Return the cell-temperature efficiency multiplier for one irradiance sample."""
    t_cell = pvwatts_cell_temperature(gti, temp, wind)
    # No lower clamp on (t_cell - 25): cool cells genuinely produce more power.
    # Cap at 1.1 (10% above STC) to prevent unrealistic gains at very cold temperatures.
    return max(0.5, min(1.1, 1.0 - _TEMP_COEFF * (t_cell - _STC_TEMP_C)))


def gti_hourly_to_period_kwh(times, gti_values, temp_values, wind_values, kwp, system_loss, shading_factors=None, p10_instant=None, p10_fallback=_DEFAULT_P10_FALLBACK):
    """Convert hourly GTI samples into per-hour PV energy for a single array.

    Open-Meteo returns point-in-time irradiance (W/m2) at the start of each hour, so the
    samples are integrated trapezoidally across each adjacent pair rather than treated as
    period energy.

    Args:
        times: list of ISO timestamp strings, "%Y-%m-%dT%H:%M", assumed UTC
        gti_values: list of global tilted irradiance values in W/m2, aligned to times
        temp_values: list of air temperatures in degC, aligned to times
        wind_values: list of wind speeds in m/s, aligned to times
        kwp: array peak power in kW
        system_loss: fractional system loss, e.g. 0.05 for 95% efficiency
        shading_factors: optional list of 12 per-month multipliers
        p10_instant: optional dict of timestamp string to raw P10 kW, before temperature derate
        p10_fallback: multiplier applied to P50 when p10_instant has no entry

    Returns:
        dict of tz-aware UTC hour-start datetime to {"pv_estimate": kWh, "pv_estimate10": kWh}
    """
    instant_kw = {}
    instant_stamps = []

    for idx, ts in enumerate(times):
        if idx >= len(gti_values):
            break
        gti = gti_values[idx]
        if gti is None:
            gti = 0.0
        temp = temp_values[idx] if idx < len(temp_values) and temp_values[idx] is not None else _DEFAULT_TEMP_C
        wind = wind_values[idx] if idx < len(wind_values) and wind_values[idx] is not None else _DEFAULT_WIND_MS
        eta_temp = _temperature_efficiency(gti, temp, wind)
        pv50_inst = dp4((gti / 1000.0) * kwp * eta_temp * (1.0 - system_loss))
        raw_p10 = p10_instant.get(ts) if p10_instant else None
        # p10_instant was computed without temperature derating; apply eta_temp now
        pv10_inst = dp4(min(raw_p10 * eta_temp, pv50_inst) if raw_p10 is not None else pv50_inst * p10_fallback)
        try:
            stamp = datetime.strptime(ts, "%Y-%m-%dT%H:%M")
            stamp = stamp.replace(tzinfo=pytz.utc)
        except (ValueError, TypeError):
            continue
        instant_kw[stamp] = (pv50_inst, pv10_inst)
        instant_stamps.append(stamp)

    period_data = {}
    for i in range(len(instant_stamps) - 1):
        stamp = instant_stamps[i]
        next_stamp = instant_stamps[i + 1]
        if (next_stamp - stamp) != timedelta(hours=1):
            continue
        pv50_start, pv10_start = instant_kw[stamp]
        pv50_end, pv10_end = instant_kw[next_stamp]
        pv50 = dp4(0.5 * (pv50_start + pv50_end))
        pv10 = dp4(0.5 * (pv10_start + pv10_end))

        if shading_factors and len(shading_factors) == 12:
            shading_month = shading_factors[stamp.month - 1]
            pv50 = dp4(pv50 * shading_month)
            pv10 = dp4(pv10 * shading_month)

        period_data[stamp] = {"pv_estimate": pv50, "pv_estimate10": pv10}

    return period_data
```

- [ ] **Step 5: Register the test**

In `apps/predbat/unit_test.py`, add the import alongside the other `from tests.test_* import ...` lines:

```python
from tests.test_solar_model import test_solar_model
```

and add to `TEST_REGISTRY` (near the other model entries):

```python
        ("solar_model", test_solar_model, "Shared solar GTI conversion model tests", False),
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test solar_model > /tmp/t1.txt 2>&1; grep -E "ERROR|FAILED|PASSED|Traceback" /tmp/t1.txt`

Expected: no `ERROR` lines, test reports success.

- [ ] **Step 7: Make `solcast.py` delegate to the new module**

In `apps/predbat/solcast.py`:

1. Delete the `_SAPM_A` / `_SAPM_B` / `_SAPM_DELTA_T` constants and the `pvwatts_cell_temperature` function (lines ~26-39).
2. Add to the import block: `from solar_model import convert_azimuth, gti_hourly_to_period_kwh`.
3. Delete the `convert_azimuth` method (lines ~243-255). Its call site becomes the module function: `az = convert_azimuth(az)`.
4. Replace the two-pass conversion block in `download_open_meteo_data()` (from `# Pass 1: compute instantaneous kW` through the `period_data[stamp] = data_item` else-branch) with:

```python
            array_periods = gti_hourly_to_period_kwh(
                times,
                gti_values,
                temp_values,
                wind_values,
                kwp=kwp,
                system_loss=system_loss,
                shading_factors=shading_factors,
                p10_instant=ensemble_p10,
            )
            for stamp, values in array_periods.items():
                pv50 = values["pv_estimate"]
                pv10 = values["pv_estimate10"]
                if stamp in period_data:
                    period_data[stamp]["pv_estimate"] = dp4(period_data[stamp]["pv_estimate"] + pv50)
                    period_data[stamp]["pv_estimate10"] = dp4(period_data[stamp]["pv_estimate10"] + pv10)
                else:
                    period_data[stamp] = {"period_start": stamp.strftime(TIME_FORMAT), "pv_estimate": pv50, "pv_estimate10": pv10}
```

Note `math` may now be unused in `solcast.py` — check before removing the import, as `download_open_meteo_ensemble_data()` still uses `math.ceil`.

- [ ] **Step 8: Run the full existing solar test suites to confirm no regression**

Run: `cd coverage && ./run_all -k open_meteo > /tmp/t1b.txt 2>&1; ./run_all -k solcast >> /tmp/t1b.txt 2>&1; ./run_all -k pv_forecast >> /tmp/t1b.txt 2>&1; grep -E "ERROR|FAILED|Traceback" /tmp/t1b.txt`

Expected: no output from the grep, and the `open_meteo` tests must pass exactly as they did in Step 1 with `test_open_meteo.py` unedited. That suite calls the real `download_open_meteo_data()` end to end and is the parity guarantee for this whole refactor — if it fails, the extraction changed behaviour and the extraction is wrong, not the test.

- [ ] **Step 9: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/solar_model.py apps/predbat/solcast.py apps/predbat/unit_test.py apps/predbat/tests/test_solar_model.py
git commit -m "refactor(solar): extract the shared GTI to kW conversion model"
```

---

## Task 2: Load profile data tables

Pure data, no logic, so it can be revised against real measurements without touching behaviour.

**Files:**
- Create: `apps/predbat/annual_profiles.py`
- Create: `apps/predbat/tests/test_annual_profiles.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `annual_profiles.HOURLY_SHAPE` — list of 24 unnormalised relative weights
  - `annual_profiles.MONTH_WEIGHTS` — list of 12 relative daily-consumption multipliers, January first
  - `annual_profiles.SHAPE_TILT_FRACTION` — float, proportion of the *source band's* energy moved to the destination band
  - `annual_profiles.NIGHT_BAND_SLOTS` — list of half-hour indices for 00:00-07:00
  - `annual_profiles.DAY_BAND_SLOTS` — list of half-hour indices for 07:00-20:00
  - `annual_profiles.half_hour_shape() -> list[float]` — 48 values summing to exactly 1.0

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_profiles.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction load profile data tables."""

from annual_profiles import DAY_BAND_SLOTS, HOURLY_SHAPE, MONTH_WEIGHTS, NIGHT_BAND_SLOTS, SHAPE_TILT_FRACTION, half_hour_shape


def test_annual_profiles(my_predbat):
    """Verify the profile tables are well formed and normalise correctly."""
    failed = False
    print("**** Testing annual_profiles ****")

    print("Test: HOURLY_SHAPE has 24 positive entries")
    if len(HOURLY_SHAPE) != 24:
        print("  ERROR: expected 24 hourly weights, got {}".format(len(HOURLY_SHAPE)))
        failed = True
    if any(value <= 0 for value in HOURLY_SHAPE):
        print("  ERROR: all hourly weights must be positive")
        failed = True

    print("Test: half_hour_shape returns 48 values summing to 1.0")
    shape = half_hour_shape()
    if len(shape) != 48:
        print("  ERROR: expected 48 half-hourly values, got {}".format(len(shape)))
        failed = True
    total = sum(shape)
    if abs(total - 1.0) > 1e-9:
        print("  ERROR: half_hour_shape must sum to 1.0, got {}".format(total))
        failed = True

    print("Test: the evening peak exceeds the overnight trough")
    evening = sum(shape[36:42])
    overnight = sum(shape[4:10])
    if evening <= overnight:
        print("  ERROR: evening 18:00-21:00 share {} should exceed overnight 02:00-05:00 share {}".format(evening, overnight))
        failed = True

    print("Test: MONTH_WEIGHTS has 12 positive entries with winter above summer")
    if len(MONTH_WEIGHTS) != 12:
        print("  ERROR: expected 12 month weights, got {}".format(len(MONTH_WEIGHTS)))
        failed = True
    if any(value <= 0 for value in MONTH_WEIGHTS):
        print("  ERROR: all month weights must be positive")
        failed = True
    if MONTH_WEIGHTS[0] <= MONTH_WEIGHTS[6]:
        print("  ERROR: January weight {} should exceed July weight {}".format(MONTH_WEIGHTS[0], MONTH_WEIGHTS[6]))
        failed = True

    print("Test: the night and day bands are disjoint and correctly sized")
    if NIGHT_BAND_SLOTS != list(range(0, 14)):
        print("  ERROR: NIGHT_BAND_SLOTS should cover 00:00-07:00, got {}".format(NIGHT_BAND_SLOTS))
        failed = True
    if DAY_BAND_SLOTS != list(range(14, 40)):
        print("  ERROR: DAY_BAND_SLOTS should cover 07:00-20:00, got {}".format(DAY_BAND_SLOTS))
        failed = True
    if set(NIGHT_BAND_SLOTS) & set(DAY_BAND_SLOTS):
        print("  ERROR: night and day bands must be disjoint")
        failed = True

    print("Test: SHAPE_TILT_FRACTION is a sane proportion")
    if not 0.0 < SHAPE_TILT_FRACTION < 0.5:
        print("  ERROR: SHAPE_TILT_FRACTION should be between 0 and 0.5, got {}".format(SHAPE_TILT_FRACTION))
        failed = True

    return failed
```

- [ ] **Step 2: Run the test to verify it fails**

First register it — add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_profiles import test_annual_profiles
```

```python
        ("annual_profiles", test_annual_profiles, "Annual prediction load profile table tests", False),
```

Run: `cd coverage && ./run_all --test annual_profiles > /tmp/t2.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/t2.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_profiles'`.

- [ ] **Step 3: Create `apps/predbat/annual_profiles.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Domestic load profile data tables for the annual prediction tool.

Data only, no behaviour, so the shapes can be recalibrated against real
consumption data without touching the code that consumes them.
"""

# Relative electricity consumption by hour of day for a typical UK domestic
# property, index 0 = 00:00. Unnormalised; half_hour_shape() normalises to 1.0.
# Shape: overnight trough, a modest morning peak, a midday plateau, and a
# pronounced evening peak from about 17:00 to 21:00.
HOURLY_SHAPE = [
    2.6,  # 00:00
    2.4,  # 01:00
    2.3,  # 02:00
    2.2,  # 03:00
    2.2,  # 04:00
    2.4,  # 05:00
    3.0,  # 06:00
    3.8,  # 07:00
    4.2,  # 08:00
    4.1,  # 09:00
    3.9,  # 10:00
    3.8,  # 11:00
    3.9,  # 12:00
    3.8,  # 13:00
    3.7,  # 14:00
    3.9,  # 15:00
    4.6,  # 16:00
    5.8,  # 17:00
    6.5,  # 18:00
    6.3,  # 19:00
    5.6,  # 20:00
    5.0,  # 21:00
    4.3,  # 22:00
    3.4,  # 23:00
]

# Relative daily consumption by month, index 0 = January. Captures the UK
# winter/summer split, which drives much of the annual answer. These are daily
# rates, so consumers must normalise by days-in-month to preserve the annual total.
MONTH_WEIGHTS = [
    1.20,  # January
    1.15,  # February
    1.05,  # March
    0.95,  # April
    0.88,  # May
    0.83,  # June
    0.82,  # July
    0.83,  # August
    0.90,  # September
    1.00,  # October
    1.12,  # November
    1.22,  # December
]

# Proportion of the SOURCE band's own energy moved to the destination band when
# the user selects a "night" or "day" biased profile. Expressed relative to the
# source band rather than to the whole day so the transfer can never exceed the
# energy available to move. Tunable against real data.
SHAPE_TILT_FRACTION = 0.30

# Half-hour slot indices, 0 = 00:00-00:30, 47 = 23:30-00:00.
NIGHT_BAND_SLOTS = list(range(0, 14))  # 00:00 - 07:00
DAY_BAND_SLOTS = list(range(14, 40))  # 07:00 - 20:00


def half_hour_shape():
    """Return the 48-slot half-hourly domestic shape, normalised to sum to exactly 1.0.

    Each hourly weight is split evenly across its two half-hour slots. The final
    slot absorbs any floating-point residue so the total is exactly 1.0.
    """
    total = float(sum(HOURLY_SHAPE))
    shape = []
    for weight in HOURLY_SHAPE:
        half = (weight / total) / 2.0
        shape.append(half)
        shape.append(half)
    residue = 1.0 - sum(shape)
    shape[-1] += residue
    return shape
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_profiles > /tmp/t2.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t2.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual_profiles.py apps/predbat/tests/test_annual_profiles.py apps/predbat/unit_test.py
git commit -m "feat(annual): add domestic load profile data tables"
```

---

## Task 3: Synthetic load profile source

Turns an annual kWh figure plus a night/day/flat preference into the forward cumulative series Predbat consumes. The key insight from the spec: setting `load_forecast_only = True` makes `step_data_history()` ignore historical load entirely (`apps/predbat/fetch.py`, the `if type_load and not forward:` branch) and build the forward profile purely from `load_forecast`, read via `get_from_incrementing(load_forecast, minute, backwards=False)` which returns `data[m + 1] - data[m]`. So `load_forecast` must be a **cumulative** kWh series keyed by absolute minute.

**Files:**
- Create: `apps/predbat/annual_load.py`
- Create: `apps/predbat/tests/test_annual_load.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual_profiles.half_hour_shape()`, `MONTH_WEIGHTS`, `SHAPE_TILT_FRACTION`, `NIGHT_BAND_SLOTS`, `DAY_BAND_SLOTS`.
- Produces:
  - `annual_load.LoadProfileSource` — base class with `minute_profile(day)` and `daily_kwh(day)`
  - `annual_load.SyntheticLoadProfile(annual_kwh, shape, year)` where `shape` is one of `"night"`, `"day"`, `"flat"`
  - `annual_load.tilt_shape(shape_values, direction) -> list[float]`
  - `annual_load.build_load_forecast(source, start_day, days) -> dict[int, float]` — cumulative kWh keyed by absolute minute, entries `0 .. days * 1440` inclusive

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_load.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction load profile sources."""

import calendar
from datetime import date

from annual_load import SyntheticLoadProfile, build_load_forecast, tilt_shape
from annual_profiles import DAY_BAND_SLOTS, NIGHT_BAND_SLOTS, half_hour_shape


def test_annual_load(my_predbat):
    """Verify the synthetic load profile preserves totals and tilts correctly."""
    failed = False
    print("**** Testing annual_load ****")

    print("Test: tilt_shape preserves the daily total exactly")
    base = half_hour_shape()
    for direction in ["night", "day", "flat"]:
        tilted = tilt_shape(base, direction)
        total = sum(tilted)
        if abs(total - 1.0) > 1e-9:
            print("  ERROR: tilt '{}' changed the total to {}".format(direction, total))
            failed = True
        if any(value < 0 for value in tilted):
            print("  ERROR: tilt '{}' produced a negative slot".format(direction))
            failed = True

    print("Test: tilt 'night' moves energy into the night band")
    night_tilted = tilt_shape(base, "night")
    base_night = sum(base[slot] for slot in NIGHT_BAND_SLOTS)
    tilted_night = sum(night_tilted[slot] for slot in NIGHT_BAND_SLOTS)
    if tilted_night <= base_night:
        print("  ERROR: night tilt should raise the night band from {} to more, got {}".format(base_night, tilted_night))
        failed = True

    print("Test: tilt 'day' moves energy into the day band")
    day_tilted = tilt_shape(base, "day")
    base_day = sum(base[slot] for slot in DAY_BAND_SLOTS)
    tilted_day = sum(day_tilted[slot] for slot in DAY_BAND_SLOTS)
    if tilted_day <= base_day:
        print("  ERROR: day tilt should raise the day band from {} to more, got {}".format(base_day, tilted_day))
        failed = True

    print("Test: tilt 'flat' is a no-op")
    flat_tilted = tilt_shape(base, "flat")
    if flat_tilted != base:
        print("  ERROR: flat tilt should leave the shape unchanged")
        failed = True

    print("Test: the twelve monthly totals sum to annual_kwh")
    annual_kwh = 3800.0
    source = SyntheticLoadProfile(annual_kwh=annual_kwh, shape="flat", year=2025)
    year_total = 0.0
    for month in range(1, 13):
        days_in_month = calendar.monthrange(2025, month)[1]
        month_total = sum(source.daily_kwh(date(2025, month, day)) for day in range(1, days_in_month + 1))
        year_total += month_total
    if abs(year_total - annual_kwh) > 1e-6:
        print("  ERROR: twelve months summed to {}, expected {}".format(year_total, annual_kwh))
        failed = True

    print("Test: January daily consumption exceeds July")
    january = source.daily_kwh(date(2025, 1, 15))
    july = source.daily_kwh(date(2025, 7, 15))
    if january <= july:
        print("  ERROR: January daily {} should exceed July daily {}".format(january, july))
        failed = True

    print("Test: minute_profile has 1440 entries summing to the day's kWh")
    day = date(2025, 3, 10)
    profile = source.minute_profile(day)
    if len(profile) != 1440:
        print("  ERROR: expected 1440 minutes, got {}".format(len(profile)))
        failed = True
    if abs(sum(profile) - source.daily_kwh(day)) > 1e-9:
        print("  ERROR: minute profile sums to {}, expected {}".format(sum(profile), source.daily_kwh(day)))
        failed = True

    print("Test: build_load_forecast produces a cumulative series Predbat can difference")
    forecast = build_load_forecast(source, date(2025, 3, 10), 2)
    if forecast.get(0) != 0.0:
        print("  ERROR: cumulative series must start at 0, got {}".format(forecast.get(0)))
        failed = True
    if 2 * 1440 not in forecast:
        print("  ERROR: cumulative series must include the final boundary minute {}".format(2 * 1440))
        failed = True
    for minute in range(1, 2 * 1440 + 1):
        if forecast[minute] < forecast[minute - 1] - 1e-12:
            print("  ERROR: cumulative series decreased at minute {}".format(minute))
            failed = True
            break
    expected_two_days = source.daily_kwh(date(2025, 3, 10)) + source.daily_kwh(date(2025, 3, 11))
    if abs(forecast[2 * 1440] - expected_two_days) > 1e-9:
        print("  ERROR: two-day total {} expected {}".format(forecast[2 * 1440], expected_two_days))
        failed = True

    print("Test: differencing the cumulative series recovers the per-minute profile")
    first_minute = forecast[1] - forecast[0]
    if abs(first_minute - profile[0]) > 1e-12:
        print("  ERROR: differenced minute 0 gave {}, expected {}".format(first_minute, profile[0]))
        failed = True

    print("Test: a zero annual figure produces a zero profile rather than dividing by zero")
    zero_source = SyntheticLoadProfile(annual_kwh=0.0, shape="flat", year=2025)
    if sum(zero_source.minute_profile(day)) != 0.0:
        print("  ERROR: zero annual kWh should give a zero profile")
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_load import test_annual_load
```

```python
        ("annual_load", test_annual_load, "Annual prediction load profile tests", False),
```

Run: `cd coverage && ./run_all --test annual_load > /tmp/t3.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/t3.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_load'`.

- [ ] **Step 3: Create `apps/predbat/annual_load.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Load profile sources for the annual prediction tool.

Produces the forward cumulative kWh series that Predbat consumes as
``load_forecast`` when ``load_forecast_only`` is set, so no synthetic backwards
history has to be fabricated.
"""

import calendar
from datetime import timedelta

from annual_profiles import DAY_BAND_SLOTS, MONTH_WEIGHTS, NIGHT_BAND_SLOTS, SHAPE_TILT_FRACTION, half_hour_shape

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_SLOT = 30


def tilt_shape(shape_values, direction):
    """Move energy between the night and day bands, preserving the total exactly.

    ``direction`` is one of "night" (move day energy into the night band), "day"
    (the reverse), or "flat" (no change). The amount moved is
    ``SHAPE_TILT_FRACTION`` of the source band's own energy, so the transfer can
    never exceed what is available. Energy is taken from and added to individual
    slots in proportion to their existing share of their band, which keeps the
    within-band shape intact.
    """
    if direction == "flat":
        return list(shape_values)

    if direction == "night":
        source_slots, dest_slots = DAY_BAND_SLOTS, NIGHT_BAND_SLOTS
    elif direction == "day":
        source_slots, dest_slots = NIGHT_BAND_SLOTS, DAY_BAND_SLOTS
    else:
        raise ValueError("Unknown load shape '{}', expected night, day or flat".format(direction))

    tilted = list(shape_values)
    source_total = sum(tilted[slot] for slot in source_slots)
    dest_total = sum(tilted[slot] for slot in dest_slots)
    if source_total <= 0 or dest_total <= 0:
        return tilted

    moved = source_total * SHAPE_TILT_FRACTION
    for slot in source_slots:
        tilted[slot] -= moved * (tilted[slot] / source_total)
    for slot in dest_slots:
        tilted[slot] += moved * (shape_values[slot] / dest_total)

    # Push any floating-point residue into the largest slot so the total stays exact
    residue = sum(shape_values) - sum(tilted)
    largest = max(range(len(tilted)), key=lambda index: tilted[index])
    tilted[largest] += residue
    return tilted


class LoadProfileSource:
    """Base class for a source of daily household load profiles."""

    def daily_kwh(self, day):
        """Return the total household kWh for the given date."""
        raise NotImplementedError

    def minute_profile(self, day):
        """Return a list of 1440 per-minute kWh values for the given date, or None if unavailable."""
        raise NotImplementedError


class SyntheticLoadProfile(LoadProfileSource):
    """Load profile synthesised from an annual kWh total and a shape preference.

    Monthly weights are normalised across the specific year's day counts so the
    twelve monthly totals sum to exactly ``annual_kwh``.
    """

    def __init__(self, annual_kwh, shape, year):
        """Build the synthetic profile for one calendar year."""
        self.annual_kwh = float(annual_kwh)
        self.shape = shape
        self.year = year
        self.slot_shape = tilt_shape(half_hour_shape(), shape)

        weighted_days = 0.0
        for month in range(1, 13):
            days_in_month = calendar.monthrange(year, month)[1]
            weighted_days += MONTH_WEIGHTS[month - 1] * days_in_month
        self.base_daily_kwh = (self.annual_kwh / weighted_days) if weighted_days > 0 else 0.0

    def daily_kwh(self, day):
        """Return the total household kWh for the given date."""
        return self.base_daily_kwh * MONTH_WEIGHTS[day.month - 1]

    def minute_profile(self, day):
        """Return a list of 1440 per-minute kWh values for the given date."""
        total = self.daily_kwh(day)
        profile = []
        for slot_value in self.slot_shape:
            per_minute = (total * slot_value) / MINUTES_PER_SLOT
            profile.extend([per_minute] * MINUTES_PER_SLOT)
        return profile


def build_load_forecast(source, start_day, days):
    """Build the cumulative kWh series Predbat reads as ``load_forecast``.

    Keys are absolute minutes from midnight on ``start_day``. Predbat differences
    consecutive entries via ``get_from_incrementing(..., backwards=False)``, so
    the series must be cumulative and must include the final boundary minute
    ``days * 1440`` for the last minute to be readable.

    Days for which the source has no data contribute zero and are skipped by the
    caller, which is responsible for logging the gap.
    """
    forecast = {0: 0.0}
    running = 0.0
    for day_offset in range(days):
        day = start_day + timedelta(days=day_offset)
        profile = source.minute_profile(day)
        if profile is None:
            profile = [0.0] * MINUTES_PER_DAY
        base = day_offset * MINUTES_PER_DAY
        for index, value in enumerate(profile):
            running += value
            forecast[base + index + 1] = running
    return forecast
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_load > /tmp/t3.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t3.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual_load.py apps/predbat/tests/test_annual_load.py apps/predbat/unit_test.py
git commit -m "feat(annual): add synthetic load profile source"
```

---

## Task 4: Octopus consumption load profile source

The real-data alternative to the synthetic profile. Mutually exclusive with it — that validation lives in Task 7, not here.

**Files:**
- Modify: `apps/predbat/annual_load.py`
- Modify: `apps/predbat/tests/test_annual_load.py`

**Interfaces:**
- Consumes: `annual_load.LoadProfileSource` from Task 3.
- Produces:
  - `annual_load.OctopusConsumptionLoadProfile(api_key, account_id, log, storage=None, fallback=None)`
  - `OctopusConsumptionLoadProfile.fetch(year)` — async, populates the internal per-day cache
  - `OctopusConsumptionLoadProfile.missing_days` — set of `date` objects with no usable data
  - `annual_load.parse_consumption_results(results) -> dict[date, list[float]]` — pure, testable without network; maps each date to 48 half-hourly kWh values

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_annual_load.py`, and add `parse_consumption_results, OctopusConsumptionLoadProfile` to the `annual_load` import line:

```python
def test_annual_load_octopus(my_predbat):
    """Verify Octopus consumption parsing and its fallback behaviour."""
    failed = False
    print("**** Testing annual_load Octopus source ****")

    print("Test: parse_consumption_results maps half-hourly readings onto dates")
    results = []
    for slot in range(48):
        hour = slot // 2
        minute = 30 * (slot % 2)
        results.append(
            {
                "consumption": 0.25,
                "interval_start": "2025-03-10T{:02d}:{:02d}:00Z".format(hour, minute),
                "interval_end": "2025-03-10T{:02d}:{:02d}:00Z".format(hour, minute),
            }
        )
    parsed = parse_consumption_results(results)
    target = date(2025, 3, 10)
    if target not in parsed:
        print("  ERROR: expected {} in parsed output, got {}".format(target, list(parsed.keys())))
        failed = True
    elif len(parsed[target]) != 48:
        print("  ERROR: expected 48 slots, got {}".format(len(parsed[target])))
        failed = True
    elif abs(sum(parsed[target]) - 12.0) > 1e-9:
        print("  ERROR: expected 12.0 kWh for the day, got {}".format(sum(parsed[target])))
        failed = True

    print("Test: a partial day is reported as missing rather than silently understated")
    partial = parse_consumption_results(results[:20])
    if date(2025, 3, 10) in partial:
        print("  ERROR: a day with only 20 of 48 slots must not be returned as complete")
        failed = True

    print("Test: minute_profile falls back to the synthetic source for a missing day")
    fallback = SyntheticLoadProfile(annual_kwh=3800.0, shape="flat", year=2025)
    source = OctopusConsumptionLoadProfile(api_key="x", account_id="A-1", log=print, fallback=fallback)
    source.consumption = parse_consumption_results(results)

    present = source.minute_profile(date(2025, 3, 10))
    if abs(sum(present) - 12.0) > 1e-9:
        print("  ERROR: present day should use real data summing to 12.0, got {}".format(sum(present)))
        failed = True

    absent = source.minute_profile(date(2025, 3, 11))
    if abs(sum(absent) - fallback.daily_kwh(date(2025, 3, 11))) > 1e-9:
        print("  ERROR: missing day should fall back to synthetic, got {}".format(sum(absent)))
        failed = True
    if date(2025, 3, 11) not in source.missing_days:
        print("  ERROR: a fallback day must be recorded in missing_days")
        failed = True

    print("Test: no fallback and no data yields None so the caller can exclude the day")
    bare = OctopusConsumptionLoadProfile(api_key="x", account_id="A-1", log=print)
    if bare.minute_profile(date(2025, 3, 11)) is not None:
        print("  ERROR: with no data and no fallback minute_profile must return None")
        failed = True

    return failed
```

Register it in `apps/predbat/unit_test.py`:

```python
from tests.test_annual_load import test_annual_load, test_annual_load_octopus
```

```python
        ("annual_load_octopus", test_annual_load_octopus, "Annual prediction Octopus consumption tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test annual_load_octopus > /tmp/t4.txt 2>&1; grep -E "ERROR|ImportError|cannot import" /tmp/t4.txt`

Expected: FAIL with `cannot import name 'parse_consumption_results'`.

- [ ] **Step 3: Add the Octopus source to `apps/predbat/annual_load.py`**

Add these imports at the top of the file:

```python
import base64
from datetime import date, datetime, timedelta
```

(replacing the existing `from datetime import timedelta` line), plus:

```python
import aiohttp
```

Then append:

```python
OCTOPUS_API_BASE = "https://api.octopus.energy/v1"
SLOTS_PER_DAY = 48


def parse_consumption_results(results):
    """Turn raw Octopus consumption rows into complete per-day half-hourly kWh lists.

    Only days with all 48 slots present are returned. A partially reported day is
    omitted entirely rather than returned short, because a half-populated day
    looks like genuinely low consumption and would silently understate the bill.
    """
    by_day = {}
    for row in results or []:
        start = row.get("interval_start")
        consumption = row.get("consumption")
        if start is None or consumption is None:
            continue
        try:
            stamp = datetime.strptime(start[:16], "%Y-%m-%dT%H:%M")
        except (ValueError, TypeError):
            continue
        slot = stamp.hour * 2 + (1 if stamp.minute >= 30 else 0)
        day = stamp.date()
        if day not in by_day:
            by_day[day] = [None] * SLOTS_PER_DAY
        by_day[day][slot] = float(consumption)

    complete = {}
    for day, slots in by_day.items():
        if all(value is not None for value in slots):
            complete[day] = slots
    return complete


class OctopusConsumptionLoadProfile(LoadProfileSource):
    """Load profile taken from the account's real half-hourly Octopus consumption.

    The meter series already includes any EV charging, which is why the config
    layer rejects an Octopus key alongside a separate car charging figure.
    """

    def __init__(self, api_key, account_id, log, storage=None, fallback=None):
        """Set up the Octopus consumption source, optionally backed by a fallback profile."""
        self.api_key = api_key
        self.account_id = account_id
        self.log = log
        self.storage = storage
        self.fallback = fallback
        self.consumption = {}
        self.missing_days = set()
        self.mpan = None
        self.serial = None

    def _auth_header(self):
        """Return the HTTP Basic auth header Octopus expects, API key as username."""
        token = base64.b64encode("{}:".format(self.api_key).encode("utf-8")).decode("utf-8")
        return {"Authorization": "Basic {}".format(token), "accept": "application/json", "user-agent": "predbat/1.0"}

    async def _get_json(self, session, url):
        """Fetch and decode one JSON page, returning None on any failure."""
        try:
            async with session.get(url, headers=self._auth_header(), timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status not in [200, 201]:
                    self.log("Warn: Annual: Octopus consumption request to {} returned {}".format(url, response.status))
                    return None
                return await response.json()
        except (aiohttp.ClientError, ValueError, TimeoutError) as error:
            self.log("Warn: Annual: Octopus consumption request to {} failed: {}".format(url, error))
            return None

    async def resolve_meter(self, session):
        """Resolve the account's MPAN and meter serial. Returns True on success."""
        data = await self._get_json(session, "{}/accounts/{}/".format(OCTOPUS_API_BASE, self.account_id))
        if not data:
            return False
        for prop in data.get("properties", []) or []:
            for point in prop.get("electricity_meter_points", []) or []:
                if point.get("is_export"):
                    continue
                meters = point.get("meters", []) or []
                if point.get("mpan") and meters:
                    self.mpan = point["mpan"]
                    self.serial = meters[-1].get("serial_number")
                    if self.serial:
                        self.log("Annual: Octopus resolved MPAN {} meter {}".format(self.mpan, self.serial))
                        return True
        self.log("Warn: Annual: Octopus account {} has no usable electricity import meter".format(self.account_id))
        return False

    async def fetch(self, year):
        """Download a calendar year of half-hourly consumption. Returns True on success."""
        cache_key = "consumption_{}_{}".format(self.account_id, year)
        if self.storage:
            cached = await self.storage.load("annual", cache_key)
            if isinstance(cached, dict) and cached:
                self.consumption = {date.fromisoformat(key): value for key, value in cached.items()}
                self.log("Annual: Octopus consumption for {} loaded from cache, {} days".format(year, len(self.consumption)))
                return True

        async with aiohttp.ClientSession() as session:
            if not await self.resolve_meter(session):
                return False

            url = "{}/electricity-meter-points/{}/meters/{}/consumption/?period_from={}-01-01T00:00Z&period_to={}-01-01T00:00Z&page_size=25000&order_by=period".format(
                OCTOPUS_API_BASE, self.mpan, self.serial, year, year + 1
            )
            rows = []
            pages = 0
            while url and pages < 40:
                data = await self._get_json(session, url)
                if not data or "results" not in data:
                    break
                rows += data["results"]
                url = data.get("next", None)
                pages += 1

        self.consumption = parse_consumption_results(rows)
        if not self.consumption:
            self.log("Warn: Annual: Octopus returned no complete days of consumption for {}".format(year))
            return False

        self.log("Annual: Octopus consumption for {} downloaded, {} complete days".format(year, len(self.consumption)))
        if self.storage:
            await self.storage.save("annual", cache_key, {day.isoformat(): slots for day, slots in self.consumption.items()}, format="json")
        return True

    def daily_kwh(self, day):
        """Return the total household kWh for the given date."""
        slots = self.consumption.get(day)
        if slots is not None:
            return sum(slots)
        if self.fallback:
            return self.fallback.daily_kwh(day)
        return 0.0

    def minute_profile(self, day):
        """Return 1440 per-minute kWh values, falling back or returning None when the day is missing."""
        slots = self.consumption.get(day)
        if slots is None:
            self.missing_days.add(day)
            if self.fallback:
                return self.fallback.minute_profile(day)
            return None
        profile = []
        for slot_value in slots:
            profile.extend([slot_value / MINUTES_PER_SLOT] * MINUTES_PER_SLOT)
        return profile
```

- [ ] **Step 4: Run both load tests to verify they pass**

Run: `cd coverage && ./run_all -k annual_load > /tmp/t4.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t4.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual_load.py apps/predbat/tests/test_annual_load.py apps/predbat/unit_test.py
git commit -m "feat(annual): add Octopus consumption load profile source"
```

---

## Task 5: Open-Meteo weather module

Downloads two archives per array — ERA5 actuals and the archived short-range forecast — converts both through the Task 1 solar model, and derives each month's P10 ratio from the measured forecast error.

The module takes an injectable `fetch_json` coroutine so tests never touch the network.

**Files:**
- Create: `apps/predbat/annual_weather.py`
- Create: `apps/predbat/tests/test_annual_weather.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `solar_model.convert_azimuth`, `solar_model.gti_hourly_to_period_kwh`.
- Produces:
  - `annual_weather.ARCHIVE_URL`, `annual_weather.FORECAST_ARCHIVE_URL` — base URL constants
  - `annual_weather.percentile(values, fraction) -> float`
  - `annual_weather.WeatherYear` with:
    - `pv_minutes(series, midnight_utc, minutes) -> dict[int, float]` where `series` is `"actual"` or `"forecast"`
    - `pv_minutes_p10(midnight_utc, minutes, month) -> dict[int, float]`
    - `daily_actual_kwh(day) -> float`
    - `has_actual(day) -> bool`
    - `p10_ratio(month) -> float`
    - `forecast_available` (bool), `fallback_months` (set of ints)
  - `annual_weather.AnnualWeather(arrays, latitude, longitude, log, storage=None, p10_fallback=0.7, fetch_json=None)` with `async fetch(year) -> WeatherYear`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_weather.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction Open-Meteo weather module."""

import asyncio
from datetime import date, datetime, timedelta

import pytz

from annual_weather import AnnualWeather, percentile

ARRAYS = [{"kwp": 5.0, "declination": 35, "azimuth": 180, "efficiency": 0.95}]


def build_hourly(start_day, days, peak_gti):
    """Build a synthetic Open-Meteo hourly payload with a fixed daily irradiance curve."""
    times = []
    gti = []
    temp = []
    wind = []
    curve = [0, 0, 0, 0, 0, 0, 0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0, 0.95, 0.85, 0.7, 0.5, 0.3, 0.1, 0, 0, 0, 0, 0]
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for hour in range(24):
            times.append("{}T{:02d}:00".format(day.isoformat(), hour))
            gti.append(peak_gti * curve[hour])
            temp.append(15.0)
            wind.append(1.5)
    return {"hourly": {"time": times, "global_tilted_irradiance": gti, "temperature_2m": temp, "wind_speed_10m": wind}}


def test_annual_weather(my_predbat):
    """Verify weather fetching, P10 derivation from forecast error, and the fallback path."""
    failed = False
    print("**** Testing annual_weather ****")

    print("Test: percentile picks the expected order statistic")
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    got = percentile(values, 0.10)
    if got != 1.0:
        print("  ERROR: 10th percentile of 1..10 expected 1.0, got {}".format(got))
        failed = True
    if percentile([], 0.10) != 0.0:
        print("  ERROR: percentile of an empty list should be 0.0")
        failed = True

    # Actuals peak at 800, forecast peaks at 1000, so every day's actual/forecast ratio is 0.8
    start = date(2025, 1, 1)
    actual_payload = build_hourly(start, 40, 800.0)
    forecast_payload = build_hourly(start, 40, 1000.0)

    async def fake_fetch(url):
        """Return the archive or forecast payload depending on the host in the URL."""
        if "archive-api" in url:
            return actual_payload
        return forecast_payload

    weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=fake_fetch)
    year = asyncio.get_event_loop().run_until_complete(weather.fetch(2025))

    print("Test: the forecast archive is reported as available")
    if not year.forecast_available:
        print("  ERROR: forecast_available should be True when both payloads parse")
        failed = True

    print("Test: January's P10 ratio reflects the measured 0.8 forecast error")
    ratio = year.p10_ratio(1)
    if abs(ratio - 0.8) > 0.01:
        print("  ERROR: expected a P10 ratio near 0.8, got {}".format(ratio))
        failed = True

    print("Test: actual daily energy is below forecast daily energy")
    day = date(2025, 1, 10)
    if not year.has_actual(day):
        print("  ERROR: expected actual data for {}".format(day))
        failed = True
    midnight = pytz.utc.localize(datetime(2025, 1, 10, 0, 0))
    actual_minutes = year.pv_minutes("actual", midnight, 24 * 60)
    forecast_minutes = year.pv_minutes("forecast", midnight, 24 * 60)
    actual_total = sum(actual_minutes.values())
    forecast_total = sum(forecast_minutes.values())
    if actual_total <= 0:
        print("  ERROR: actual PV for {} should be positive, got {}".format(day, actual_total))
        failed = True
    if forecast_total <= actual_total:
        print("  ERROR: forecast total {} should exceed actual total {}".format(forecast_total, actual_total))
        failed = True

    print("Test: pv_minutes covers a 48 hour window and is keyed by absolute minute")
    two_day = year.pv_minutes("actual", midnight, 48 * 60)
    if max(two_day.keys()) >= 48 * 60:
        print("  ERROR: pv_minutes must not emit minutes at or beyond the window length")
        failed = True
    if abs(sum(two_day.values()) - (actual_total + year.daily_actual_kwh(date(2025, 1, 11)))) > 0.01:
        print("  ERROR: the 48 hour window should equal two days of actual energy")
        failed = True

    print("Test: pv_minutes_p10 scales the forecast series by the month ratio")
    p10_minutes = year.pv_minutes_p10(midnight, 24 * 60, 1)
    expected = forecast_total * year.p10_ratio(1)
    if abs(sum(p10_minutes.values()) - expected) > 0.01:
        print("  ERROR: P10 total {} expected {}".format(sum(p10_minutes.values()), expected))
        failed = True

    print("Test: a missing forecast archive falls back and records the degradation")
    async def actuals_only_fetch(url):
        """Serve actuals and fail every forecast request."""
        if "archive-api" in url:
            return actual_payload
        return None

    degraded_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=actuals_only_fetch, p10_fallback=0.7)
    degraded = asyncio.get_event_loop().run_until_complete(degraded_weather.fetch(2025))
    if degraded.forecast_available:
        print("  ERROR: forecast_available should be False when the forecast archive is empty")
        failed = True
    if abs(degraded.p10_ratio(1) - 0.7) > 1e-9:
        print("  ERROR: expected the 0.7 fallback ratio, got {}".format(degraded.p10_ratio(1)))
        failed = True
    if 1 not in degraded.fallback_months:
        print("  ERROR: January should be recorded in fallback_months")
        failed = True
    degraded_forecast = degraded.pv_minutes("forecast", midnight, 24 * 60)
    degraded_actual = degraded.pv_minutes("actual", midnight, 24 * 60)
    if abs(sum(degraded_forecast.values()) - sum(degraded_actual.values())) > 1e-9:
        print("  ERROR: with no forecast archive the forecast series must fall back to actuals")
        failed = True

    print("Test: a month with fewer than seven usable days falls back")
    sparse_actual = build_hourly(date(2025, 1, 1), 4, 800.0)
    sparse_forecast = build_hourly(date(2025, 1, 1), 4, 1000.0)

    async def sparse_fetch(url):
        """Serve only four days of data."""
        return sparse_actual if "archive-api" in url else sparse_forecast

    sparse_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=sparse_fetch, p10_fallback=0.7)
    sparse = asyncio.get_event_loop().run_until_complete(sparse_weather.fetch(2025))
    if abs(sparse.p10_ratio(1) - 0.7) > 1e-9:
        print("  ERROR: a four-day month should fall back to 0.7, got {}".format(sparse.p10_ratio(1)))
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_weather import test_annual_weather
```

```python
        ("annual_weather", test_annual_weather, "Annual prediction Open-Meteo weather tests", False),
```

Run: `cd coverage && ./run_all --test annual_weather > /tmp/t5.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/t5.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_weather'`.

- [ ] **Step 3: Create `apps/predbat/annual_weather.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Open-Meteo historical weather for the annual prediction tool.

Downloads two archives per PV array: ERA5 reanalysis actuals (what really
happened) and the archived short-range forecast for the same dates (what Predbat
would have been looking at). The gap between them is genuine day-ahead forecast
error, from which each month's P10 ratio is derived.
"""

import math
from datetime import timedelta

import aiohttp

from solar_model import convert_azimuth, gti_hourly_to_period_kwh

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

HOURLY_VARIABLES = "global_tilted_irradiance,temperature_2m,wind_speed_10m"

# A month needs at least this many usable forecast/actual day pairs before its
# measured P10 ratio is trusted over the flat fallback.
MIN_DAYS_FOR_P10 = 7

# Fraction used for the P10 order statistic
P10_FRACTION = 0.10


def percentile(values, fraction):
    """Return the order statistic at ``fraction`` through a list of values.

    Uses the same convention as the Solcast ensemble P10 in ``solcast.py``:
    sort ascending and take index ``ceil(n * fraction) - 1``, clamped to zero.
    Returns 0.0 for an empty list.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


class WeatherYear:
    """A year of per-array-summed PV energy, for both actuals and forecast."""

    def __init__(self, actual_periods, forecast_periods, p10_ratios, forecast_available, fallback_months):
        """Hold the converted period data and the derived monthly P10 ratios."""
        self.actual_periods = actual_periods
        self.forecast_periods = forecast_periods if forecast_available else actual_periods
        self.p10_ratios = p10_ratios
        self.forecast_available = forecast_available
        self.fallback_months = fallback_months
        self._daily_actual = self._daily_totals(self.actual_periods)

    @staticmethod
    def _daily_totals(periods):
        """Sum hourly period energy into per-date totals."""
        totals = {}
        for stamp, kwh in periods.items():
            day = stamp.date()
            totals[day] = totals.get(day, 0.0) + kwh
        return totals

    def has_actual(self, day):
        """Return True when actuals exist for the given date."""
        return day in self._daily_actual

    def daily_actual_kwh(self, day):
        """Return the total actual PV kWh generated on the given date."""
        return self._daily_actual.get(day, 0.0)

    def p10_ratio(self, month):
        """Return the P10 scaling ratio for the given month number, 1 = January."""
        return self.p10_ratios.get(month, 1.0)

    def pv_minutes(self, series, midnight_utc, minutes):
        """Spread hourly period energy across per-minute kWh, keyed by absolute minute.

        ``series`` is "actual" or "forecast". Minutes outside [0, minutes) are
        discarded, so the caller always receives a window it asked for.
        """
        periods = self.actual_periods if series == "actual" else self.forecast_periods
        result = {}
        end_utc = midnight_utc + timedelta(minutes=minutes)
        for stamp, kwh in periods.items():
            if stamp < midnight_utc or stamp >= end_utc:
                continue
            offset = int((stamp - midnight_utc).total_seconds() // 60)
            per_minute = kwh / 60.0
            for minute in range(offset, offset + 60):
                if 0 <= minute < minutes:
                    result[minute] = result.get(minute, 0.0) + per_minute
        return result

    def pv_minutes_p10(self, midnight_utc, minutes, month):
        """Return the P10 per-minute series: the forecast series scaled by the month's ratio."""
        ratio = self.p10_ratio(month)
        return {minute: value * ratio for minute, value in self.pv_minutes("forecast", midnight_utc, minutes).items()}


class AnnualWeather:
    """Fetches and converts a calendar year of Open-Meteo data for one site."""

    def __init__(self, arrays, latitude, longitude, log, storage=None, p10_fallback=0.7, fetch_json=None):
        """Configure the site's PV arrays and the JSON fetcher used for downloads."""
        self.arrays = arrays
        self.latitude = latitude
        self.longitude = longitude
        self.log = log
        self.storage = storage
        self.p10_fallback = p10_fallback
        self.fetch_json = fetch_json or self._default_fetch_json

    async def _default_fetch_json(self, url):
        """Download and decode one JSON document, returning None on any failure."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"accept": "application/json", "user-agent": "predbat/1.0"}, timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status not in [200, 201]:
                        self.log("Warn: Annual: Open-Meteo request to {} returned {}".format(url, response.status))
                        return None
                    return await response.json()
        except (aiohttp.ClientError, ValueError, TimeoutError) as error:
            self.log("Warn: Annual: Open-Meteo request to {} failed: {}".format(url, error))
            return None

    def _build_url(self, base, array, year):
        """Build one Open-Meteo request URL for a single array and calendar year.

        The window runs to 1 January of the following year so the final sampled
        day still has the following day its 48 hour plan needs.
        """
        azimuth = array.get("azimuth", 180.0)
        if not array.get("azimuth_zero_south", False):
            azimuth = convert_azimuth(azimuth)
        return "{}?latitude={}&longitude={}&start_date={}-01-01&end_date={}-01-01&hourly={}&tilt={}&azimuth={}&wind_speed_unit=ms&timezone=UTC".format(
            base, self.latitude, self.longitude, year, year + 1, HOURLY_VARIABLES, array.get("declination", 35.0), azimuth
        )

    async def _fetch_series(self, base, year, cache_tag):
        """Fetch one source for every array and return the summed hourly period energy."""
        totals = {}
        any_data = False
        for index, array in enumerate(self.arrays):
            url = self._build_url(base, array, year)
            data = None
            cache_key = "weather_{}_{}_{}_{}_{}".format(cache_tag, year, index, self.latitude, self.longitude)
            if self.storage:
                data = await self.storage.load("annual", cache_key)
            if not data:
                data = await self.fetch_json(url)
                # Only cache a response that actually carries the hourly block we need,
                # so a rate-limit or error page is never pinned as this array's weather
                if data and data.get("hourly", {}).get("global_tilted_irradiance") and self.storage:
                    await self.storage.save("annual", cache_key, data, format="json")
            if not data:
                self.log("Warn: Annual: no {} data for array {}".format(cache_tag, index))
                continue

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            gti_values = hourly.get("global_tilted_irradiance", [])
            if not times or not gti_values:
                self.log("Warn: Annual: {} data for array {} has no hourly values".format(cache_tag, index))
                continue
            if len(gti_values) < len(times):
                # A response truncated mid-year would otherwise be cached and read back
                # as a genuinely dark second half of the year
                self.log("Warn: Annual: {} data for array {} is truncated ({} stamps, {} values); discarding".format(cache_tag, index, len(times), len(gti_values)))
                continue

            periods = gti_hourly_to_period_kwh(
                times,
                gti_values,
                hourly.get("temperature_2m", []),
                hourly.get("wind_speed_10m", []),
                kwp=array.get("kwp", 3.0),
                system_loss=1.0 - array.get("efficiency", 0.95),
                shading_factors=array.get("shading_factors", None),
            )
            for stamp, values in periods.items():
                totals[stamp] = totals.get(stamp, 0.0) + values["pv_estimate"]
            any_data = True

        return totals if any_data else {}

    def _derive_p10_ratios(self, actual_periods, forecast_periods, forecast_available):
        """Derive each month's P10 ratio from the measured actual/forecast daily energy error."""
        ratios = {}
        fallback_months = set()

        actual_daily = WeatherYear._daily_totals(actual_periods)
        forecast_daily = WeatherYear._daily_totals(forecast_periods)

        by_month = {}
        if forecast_available:
            for day, forecast_kwh in forecast_daily.items():
                if forecast_kwh <= 0:
                    continue
                if day not in actual_daily:
                    continue
                by_month.setdefault(day.month, []).append(actual_daily[day] / forecast_kwh)

        for month in range(1, 13):
            samples = by_month.get(month, [])
            if len(samples) >= MIN_DAYS_FOR_P10:
                ratios[month] = min(1.0, percentile(samples, P10_FRACTION))
            else:
                ratios[month] = self.p10_fallback
                fallback_months.add(month)

        if fallback_months:
            self.log("Warn: Annual: P10 fell back to the flat {} derate for months {}".format(self.p10_fallback, sorted(fallback_months)))

        return ratios, fallback_months

    async def fetch(self, year):
        """Download and convert a calendar year, returning a populated WeatherYear."""
        actual_periods = await self._fetch_series(ARCHIVE_URL, year, "actual")
        forecast_periods = await self._fetch_series(FORECAST_ARCHIVE_URL, year, "forecast")
        forecast_available = bool(forecast_periods)

        if not forecast_available:
            self.log("Warn: Annual: the Open-Meteo forecast archive returned nothing for {}; planning on actuals with the flat P10 derate".format(year))

        ratios, fallback_months = self._derive_p10_ratios(actual_periods, forecast_periods, forecast_available)
        return WeatherYear(actual_periods, forecast_periods, ratios, forecast_available, fallback_months)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_weather > /tmp/t5.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t5.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual_weather.py apps/predbat/tests/test_annual_weather.py apps/predbat/unit_test.py
git commit -m "feat(annual): add Open-Meteo actuals and forecast archive weather module"
```

---

## Task 6: Tariff module

Resolves import and export rates for a specific historical date, either from an Octopus product URL with `period_from` / `period_to`, or from a basic rates structure. Fetches a month at a time and slices per day, which keeps the API call count low.

**Files:**
- Create: `apps/predbat/annual_tariff.py`
- Create: `apps/predbat/tests/test_annual_tariff.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `utils.minute_data` (already used by `octopus.py:2338` for exactly this parsing), `PredBat.basic_rates`.
- Produces:
  - `annual_tariff.build_period_url(base_url, start_utc, end_utc) -> str`
  - `annual_tariff.AnnualTariff(config, log, predbat, storage=None, fetch_json=None)` with:
    - `async fetch_month(year, month)` — populates the internal cache; returns True on success
    - `rates_for(midnight_utc, minutes) -> (dict, dict)` — import and export rate dicts keyed by absolute minute
    - `month_available(year, month) -> bool`
    - `standing_charge_p_per_day` (float)

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_tariff.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction tariff module."""

import asyncio
from datetime import datetime, timedelta

import pytz

from annual_tariff import AnnualTariff, build_period_url


def build_agile_results(start_day, days, base_rate):
    """Build a synthetic Octopus half-hourly rate payload with a repeating daily shape."""
    results = []
    stamp = pytz.utc.localize(datetime(start_day.year, start_day.month, start_day.day))
    for slot in range(days * 48):
        valid_from = stamp + timedelta(minutes=30 * slot)
        valid_to = valid_from + timedelta(minutes=30)
        # Cheap overnight, expensive in the evening peak
        hour = valid_from.hour
        rate = base_rate * (0.3 if hour < 5 else (2.0 if 16 <= hour < 19 else 1.0))
        results.append(
            {
                "value_inc_vat": round(rate, 4),
                "valid_from": valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to": valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return results


def test_annual_tariff(my_predbat):
    """Verify Octopus date-ranged rate fetching, pagination, slicing and basic rates."""
    failed = False
    print("**** Testing annual_tariff ****")
    from datetime import date

    print("Test: build_period_url appends the date range without losing existing query parameters")
    start = pytz.utc.localize(datetime(2025, 3, 1))
    end = pytz.utc.localize(datetime(2025, 4, 1))
    plain = build_period_url("https://example.com/rates/", start, end)
    if "?period_from=2025-03-01T00:00Z" not in plain or "period_to=2025-04-01T00:00Z" not in plain:
        print("  ERROR: expected a period range in {}".format(plain))
        failed = True
    existing = build_period_url("https://example.com/rates/?page_size=100", start, end)
    if "&period_from=" not in existing or "page_size=100" not in existing:
        print("  ERROR: existing query parameters must be preserved, got {}".format(existing))
        failed = True

    print("Test: an Octopus URL tariff resolves rates for a specific date, following pagination")
    page_two = {"results": build_agile_results(date(2025, 3, 16), 16, 20.0), "next": None}
    page_one = {"results": build_agile_results(date(2025, 3, 1), 15, 20.0), "next": "https://example.com/page2"}

    calls = []

    async def fake_fetch(url):
        """Serve two pages of rate data and record the URLs requested."""
        calls.append(url)
        return page_two if "page2" in url else page_one

    config = {"import_octopus_url": "https://example.com/import/", "export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 60.0}
    tariff = AnnualTariff(config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
    ok = asyncio.get_event_loop().run_until_complete(tariff.fetch_month(2025, 3))
    if not ok:
        print("  ERROR: fetch_month should succeed with valid payloads")
        failed = True
    if not tariff.month_available(2025, 3):
        print("  ERROR: March 2025 should be reported as available")
        failed = True
    if len(calls) != 4:
        print("  ERROR: expected 4 requests (2 pages x import and export), got {}".format(len(calls)))
        failed = True

    print("Test: rates_for returns a 48 hour window keyed by absolute minute")
    midnight = pytz.utc.localize(datetime(2025, 3, 10))
    rate_import, rate_export = tariff.rates_for(midnight, 48 * 60)
    if len(rate_import) < 48 * 60:
        print("  ERROR: expected at least {} import rate minutes, got {}".format(48 * 60, len(rate_import)))
        failed = True
    if rate_import.get(0) is None:
        print("  ERROR: minute 0 must have an import rate")
        failed = True
    # 02:00 is in the cheap overnight band, 17:00 is in the peak band
    if not rate_import[120] < rate_import[17 * 60]:
        print("  ERROR: overnight rate {} should be below peak rate {}".format(rate_import[120], rate_import[17 * 60]))
        failed = True
    if abs(rate_import[120] - 6.0) > 0.01:
        print("  ERROR: overnight rate expected 6.0, got {}".format(rate_import[120]))
        failed = True

    print("Test: the second day of the window carries the following day's rates")
    if rate_import.get(24 * 60 + 120) is None:
        print("  ERROR: the second day of the window must be populated")
        failed = True

    print("Test: a failed download reports the month as unavailable rather than returning zeros")
    async def failing_fetch(url):
        """Simulate a download failure."""
        return None

    broken = AnnualTariff(config, log=print, predbat=my_predbat, fetch_json=failing_fetch)
    if asyncio.get_event_loop().run_until_complete(broken.fetch_month(2025, 4)):
        print("  ERROR: fetch_month should report failure when the download fails")
        failed = True
    if broken.month_available(2025, 4):
        print("  ERROR: a failed month must not be reported as available")
        failed = True

    print("Test: basic rates repeat a fixed daily pattern across the window")
    basic_config = {
        "rates_import": [{"start": "00:00:00", "end": "05:00:00", "rate": 7.0}, {"start": "05:00:00", "end": "00:00:00", "rate": 30.0}],
        "rates_export": [{"rate": 15.0}],
        "standing_charge_p_per_day": 45.0,
    }
    basic = AnnualTariff(basic_config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
    if not asyncio.get_event_loop().run_until_complete(basic.fetch_month(2025, 3)):
        print("  ERROR: basic rates should always be available")
        failed = True
    basic_import, basic_export = basic.rates_for(midnight, 48 * 60)
    if abs(basic_import[120] - 7.0) > 0.001:
        print("  ERROR: basic overnight import expected 7.0, got {}".format(basic_import[120]))
        failed = True
    if abs(basic_import[10 * 60] - 30.0) > 0.001:
        print("  ERROR: basic daytime import expected 30.0, got {}".format(basic_import[10 * 60]))
        failed = True
    if abs(basic_import[24 * 60 + 120] - 7.0) > 0.001:
        print("  ERROR: basic rates must repeat on day two, got {}".format(basic_import[24 * 60 + 120]))
        failed = True
    if abs(basic_export[10 * 60] - 15.0) > 0.001:
        print("  ERROR: basic export expected 15.0, got {}".format(basic_export[10 * 60]))
        failed = True

    print("Test: standing charge is carried through from config")
    if abs(tariff.standing_charge_p_per_day - 60.0) > 0.001:
        print("  ERROR: standing charge expected 60.0, got {}".format(tariff.standing_charge_p_per_day))
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_tariff import test_annual_tariff
```

```python
        ("annual_tariff", test_annual_tariff, "Annual prediction tariff tests", False),
```

Run: `cd coverage && ./run_all --test annual_tariff > /tmp/t6.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/t6.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_tariff'`.

- [ ] **Step 3: Create `apps/predbat/annual_tariff.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Historical tariff resolution for the annual prediction tool.

Resolves import and export rates for a specific past date, either from an
Octopus product URL using period_from/period_to, or from a static basic rates
structure. Fetching is done a month at a time and sliced per sampled day.
"""

import calendar
from datetime import datetime, timedelta

import aiohttp
import pytz

from utils import minute_data

MINUTES_PER_DAY = 24 * 60


def build_period_url(base_url, start_utc, end_utc):
    """Append period_from/period_to to an Octopus rates URL, preserving existing query parameters."""
    separator = "&" if "?" in base_url else "?"
    return "{}{}period_from={}&period_to={}&page_size=1500".format(base_url, separator, start_utc.strftime("%Y-%m-%dT%H:%MZ"), end_utc.strftime("%Y-%m-%dT%H:%MZ"))


class AnnualTariff:
    """Import and export rates for arbitrary historical dates."""

    def __init__(self, config, log, predbat, storage=None, fetch_json=None):
        """Configure the tariff from the annual config's ``tariff`` block.

        Octopus product codes are region-suffixed. ``resolve_arg`` substitutes
        ``{dno_region}`` from ``predbat.args``, so the region is injected there
        first. Without it a URL silently 404s and the month is reported
        unavailable, which looks like an outage rather than a config mistake.
        """
        self.config = config
        self.log = log
        self.predbat = predbat
        self.storage = storage
        self.fetch_json = fetch_json or self._default_fetch_json
        if config.get("dno_region"):
            predbat.args["dno_region"] = config["dno_region"]
        self.import_url = self._resolve_url(config.get("import_octopus_url"), "import_octopus_url")
        self.export_url = self._resolve_url(config.get("export_octopus_url"), "export_octopus_url")
        self.basic_import = config.get("rates_import")
        self.basic_export = config.get("rates_export")
        self.standing_charge_p_per_day = float(config.get("standing_charge_p_per_day", 0.0))
        # Keyed by (year, month); each value is a dict of tz-aware UTC stamp to rate
        self.import_rates = {}
        self.export_rates = {}
        self.available = set()

    def _resolve_url(self, url, name):
        """Substitute templated arguments such as {dno_region} into a tariff URL."""
        if not url:
            return None
        return self.predbat.resolve_arg(name, url, indirect=False)

    async def _default_fetch_json(self, url):
        """Download and decode one JSON document, returning None on any failure."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"accept": "application/json", "user-agent": "predbat/1.0"}, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    if response.status not in [200, 201]:
                        self.log("Warn: Annual: Octopus rate request to {} returned {}".format(url, response.status))
                        return None
                    return await response.json()
        except (aiohttp.ClientError, ValueError, TimeoutError) as error:
            self.log("Warn: Annual: Octopus rate request to {} failed: {}".format(url, error))
            return None

    async def _download_octopus(self, base_url, start_utc, end_utc, cache_key):
        """Download every page of Octopus rates for a date range, returning raw result rows."""
        if self.storage:
            cached = await self.storage.load("annual", cache_key)
            if isinstance(cached, list) and cached:
                return cached

        url = build_period_url(base_url, start_utc, end_utc)
        rows = []
        pages = 0
        truncated = False
        while url and pages < 10:
            data = await self.fetch_json(url)
            if not data or "results" not in data:
                # A failed page means we do not know what we are missing. Caching a
                # partial month would permanently pin wrong rates for that month.
                self.log("Warn: Annual: rate download for {} stopped early at page {}; not caching a partial result".format(cache_key, pages))
                truncated = True
                break
            rows += data["results"]
            url = data.get("next", None)
            pages += 1

        if truncated:
            return []
        if rows and self.storage:
            await self.storage.save("annual", cache_key, rows, format="json")
        return rows

    @staticmethod
    def _rows_to_stamped_rates(rows, start_utc, days):
        """Convert Octopus rate rows into a dict of tz-aware UTC stamp to rate.

        Reuses ``minute_data`` exactly as ``octopus.py`` does, then re-keys the
        minute offsets back onto absolute timestamps so a single monthly download
        can be sliced for any day within it.
        """
        parsed, _ = minute_data(rows, days + 1, start_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return {start_utc + timedelta(minutes=minute): rate for minute, rate in parsed.items()}

    async def fetch_month(self, year, month):
        """Fetch (or synthesise) the rates covering one calendar month plus a one day buffer.

        Returns True when usable rates exist for the month. The buffer day lets the
        last sampled day of the month complete its 48 hour plan.
        """
        key = (year, month)
        if self.import_url or self.export_url:
            days_in_month = calendar.monthrange(year, month)[1]
            start_utc = pytz.utc.localize(datetime(year, month, 1))
            end_utc = start_utc + timedelta(days=days_in_month + 2)
            days = days_in_month + 2

            import_rates = {}
            export_rates = {}
            if self.import_url:
                rows = await self._download_octopus(self.import_url, start_utc, end_utc, "rates_import_{}_{:02d}".format(year, month))
                if not rows:
                    self.log("Warn: Annual: no import rates available for {}-{:02d}".format(year, month))
                    return False
                import_rates = self._rows_to_stamped_rates(rows, start_utc, days)
            if self.export_url:
                rows = await self._download_octopus(self.export_url, start_utc, end_utc, "rates_export_{}_{:02d}".format(year, month))
                if rows:
                    export_rates = self._rows_to_stamped_rates(rows, start_utc, days)
                else:
                    self.log("Warn: Annual: no export rates available for {}-{:02d}, treating export as unpaid".format(year, month))

            if not import_rates:
                return False
            self.import_rates[key] = import_rates
            self.export_rates[key] = export_rates
            self.available.add(key)
            return True

        # Basic rates repeat a fixed daily pattern, so nothing needs downloading
        self.available.add(key)
        return True

    def month_available(self, year, month):
        """Return True when usable rates exist for the given month."""
        return (year, month) in self.available

    def _basic_window(self, info, name, minutes):
        """Expand a basic rates structure across the requested window."""
        rates = self.predbat.basic_rates(info, name)
        return {minute: rates.get(minute % MINUTES_PER_DAY, 0.0) for minute in range(minutes)}

    def rates_for(self, midnight_utc, minutes):
        """Return (import, export) rate dicts keyed by absolute minute from ``midnight_utc``."""
        key = (midnight_utc.year, midnight_utc.month)

        if self.import_url or self.export_url:
            import_stamped = self.import_rates.get(key, {})
            export_stamped = self.export_rates.get(key, {})
            # A 48 hour window starting late in a month spills into the next month's download
            next_key = (midnight_utc.year, midnight_utc.month + 1) if midnight_utc.month < 12 else (midnight_utc.year + 1, 1)
            import_stamped = dict(import_stamped)
            import_stamped.update(self.import_rates.get(next_key, {}))
            export_stamped = dict(export_stamped)
            export_stamped.update(self.export_rates.get(next_key, {}))

            rate_import = {}
            rate_export = {}
            for minute in range(minutes):
                stamp = midnight_utc + timedelta(minutes=minute)
                if stamp in import_stamped:
                    rate_import[minute] = import_stamped[stamp]
                if stamp in export_stamped:
                    rate_export[minute] = export_stamped[stamp]
            return rate_import, rate_export

        rate_import = self._basic_window(self.basic_import or [], "rates_import", minutes)
        rate_export = self._basic_window(self.basic_export or [], "rates_export", minutes)
        return rate_import, rate_export
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_tariff > /tmp/t6.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t6.txt`

Expected: no output. If `minute_data` returns fewer minutes than expected, check its `to_key` handling against `octopus.py:2338` — the call signature must match exactly.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual_tariff.py apps/predbat/tests/test_annual_tariff.py apps/predbat/unit_test.py
git commit -m "feat(annual): add historical tariff resolution module"
```

---

## Task 7: Config validation

The first piece of `annual.py`. Rejects contradictory input at load time rather than producing a plausible but wrong answer — in particular an Octopus API key alongside a manual load figure, because the meter series already contains any EV charging and accepting both would double-count it.

**Files:**
- Create: `apps/predbat/annual.py`
- Create: `apps/predbat/tests/test_annual_config.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `annual.AnnualConfigError` — exception raised for invalid configuration
  - `annual.validate_config(config, today=None) -> dict` — returns a fully defaulted, normalised config
  - `annual.scrub_secrets(config) -> dict` — deep copy with secret-looking values replaced by `"xxx"`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_config.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for annual prediction config validation."""

from datetime import date

from annual import AnnualConfigError, scrub_secrets, validate_config


def base_config():
    """Return a minimal valid annual config."""
    return {
        "annual": {
            "location": {"latitude": 51.5, "longitude": -0.1},
            "solar": [{"kwp": 5.6}],
            "battery": {"size_kwh": 9.5, "inverter_kw": 5.0},
            "load": {"annual_kwh": 3800},
            "tariff": {"rates_import": [{"rate": 25.0}]},
        }
    }


def expect_error(label, config, fragment, failed):
    """Assert that validate_config rejects the config with a message containing fragment."""
    try:
        validate_config(config)
    except AnnualConfigError as error:
        if fragment.lower() not in str(error).lower():
            print("  ERROR: {} raised '{}', expected it to mention '{}'".format(label, error, fragment))
            return True
        return failed
    print("  ERROR: {} should have raised AnnualConfigError".format(label))
    return True


def test_annual_config(my_predbat):
    """Verify annual config defaulting, normalisation and rejection rules."""
    failed = False
    print("**** Testing annual config validation ****")

    print("Test: a minimal config validates and gains defaults")
    result = validate_config(base_config(), today=date(2026, 7, 25))
    if result["year"] != 2025:
        print("  ERROR: year should default to the most recent complete calendar year, got {}".format(result["year"]))
        failed = True
    if result["samples_per_month"] != 2:
        print("  ERROR: samples_per_month should default to 2, got {}".format(result["samples_per_month"]))
        failed = True
    if result["timezone"] != "Europe/London":
        print("  ERROR: timezone should default to Europe/London, got {}".format(result["timezone"]))
        failed = True
    if abs(result["pv10_derate_fallback"] - 0.7) > 1e-9:
        print("  ERROR: pv10_derate_fallback should default to 0.7, got {}".format(result["pv10_derate_fallback"]))
        failed = True
    if result["load"]["shape"] != "flat":
        print("  ERROR: load shape should default to flat, got {}".format(result["load"]["shape"]))
        failed = True
    if result["solar"][0]["declination"] != 35 or result["solar"][0]["azimuth"] != 180:
        print("  ERROR: solar defaults should be declination 35 azimuth 180, got {}".format(result["solar"][0]))
        failed = True
    if abs(result["solar"][0]["efficiency"] - 0.95) > 1e-9:
        print("  ERROR: solar efficiency should default to 0.95, got {}".format(result["solar"][0]["efficiency"]))
        failed = True
    if result["battery"]["charge_rate_kw"] != 5.0 or result["battery"]["discharge_rate_kw"] != 5.0:
        print("  ERROR: charge and discharge rates should default to inverter_kw, got {}".format(result["battery"]))
        failed = True
    if result["battery"]["export_limit_kw"] != 5.0:
        print("  ERROR: export_limit_kw should default to inverter_kw, got {}".format(result["battery"]))
        failed = True
    if result["battery"]["hybrid"] is not True:
        print("  ERROR: hybrid should default to True, got {}".format(result["battery"].get("hybrid")))
        failed = True

    print("Test: an unwrapped config without the 'annual' key is accepted")
    unwrapped = base_config()["annual"]
    result = validate_config(unwrapped, today=date(2026, 7, 25))
    if result["year"] != 2025:
        print("  ERROR: an unwrapped config should validate the same way")
        failed = True

    print("Test: Octopus load together with a manual figure is rejected")
    config = base_config()
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    failed = expect_error("octopus plus annual_kwh", config, "mutually exclusive", failed)

    config = base_config()
    del config["annual"]["load"]["annual_kwh"]
    config["annual"]["load"]["car_charging_kwh"] = 2500
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    failed = expect_error("octopus plus car_charging_kwh", config, "mutually exclusive", failed)

    print("Test: an Octopus-only load block validates")
    config = base_config()
    del config["annual"]["load"]["annual_kwh"]
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    result = validate_config(config, today=date(2026, 7, 25))
    if result["load"].get("octopus", {}).get("account_id") != "A-1":
        print("  ERROR: the Octopus load block should survive validation")
        failed = True

    print("Test: a missing battery block yields a two-scenario run")
    config = base_config()
    del config["annual"]["battery"]
    result = validate_config(config, today=date(2026, 7, 25))
    if result["battery"] is not None:
        print("  ERROR: an omitted battery should normalise to None, got {}".format(result["battery"]))
        failed = True

    print("Test: a missing solar block is allowed for a battery-only run")
    config = base_config()
    del config["annual"]["solar"]
    result = validate_config(config, today=date(2026, 7, 25))
    if result["solar"] != []:
        print("  ERROR: an omitted solar block should normalise to an empty list, got {}".format(result["solar"]))
        failed = True

    print("Test: omitting both solar and battery is rejected as pointless")
    config = base_config()
    del config["annual"]["solar"]
    del config["annual"]["battery"]
    failed = expect_error("neither solar nor battery", config, "at least one", failed)

    print("Test: missing location is rejected")
    config = base_config()
    del config["annual"]["location"]
    failed = expect_error("no location", config, "location", failed)

    print("Test: missing load is rejected")
    config = base_config()
    del config["annual"]["load"]
    failed = expect_error("no load", config, "load", failed)

    print("Test: missing tariff is rejected")
    config = base_config()
    del config["annual"]["tariff"]
    failed = expect_error("no tariff", config, "tariff", failed)

    print("Test: an unknown load shape is rejected")
    config = base_config()
    config["annual"]["load"]["shape"] = "sideways"
    failed = expect_error("bad shape", config, "shape", failed)

    print("Test: a solar array without kwp is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"declination": 30}]
    failed = expect_error("array without kwp", config, "kwp", failed)

    print("Test: samples_per_month below 1 is rejected")
    config = base_config()
    config["annual"]["samples_per_month"] = 0
    failed = expect_error("zero samples", config, "samples_per_month", failed)

    print("Test: a postcode-only location validates")
    config = base_config()
    config["annual"]["location"] = {"postcode": "SW1A 1AA"}
    result = validate_config(config, today=date(2026, 7, 25))
    if result["location"].get("postcode") != "SW1A 1AA":
        print("  ERROR: a postcode location should survive validation")
        failed = True

    print("Test: a templated tariff URL without dno_region is rejected up front")
    config = base_config()
    config["annual"]["tariff"] = {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE/electricity-tariffs/E-1R-AGILE-{dno_region}/standard-unit-rates/"}
    failed = expect_error("templated url without region", config, "dno_region", failed)

    print("Test: a templated tariff URL with dno_region validates and is carried through")
    config = base_config()
    config["annual"]["tariff"] = {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE/electricity-tariffs/E-1R-AGILE-{dno_region}/standard-unit-rates/", "dno_region": "A"}
    result = validate_config(config, today=date(2026, 7, 25))
    if result["tariff"].get("dno_region") != "A":
        print("  ERROR: dno_region should survive validation, got {}".format(result["tariff"].get("dno_region")))
        failed = True

    print("Test: scrub_secrets removes API keys without mutating the original")
    config = base_config()
    config["annual"]["load"]["octopus"] = {"api_key": "sk_live_secret", "account_id": "A-1"}
    scrubbed = scrub_secrets(config)
    if scrubbed["annual"]["load"]["octopus"]["api_key"] != "xxx":
        print("  ERROR: api_key should be scrubbed, got {}".format(scrubbed["annual"]["load"]["octopus"]["api_key"]))
        failed = True
    if config["annual"]["load"]["octopus"]["api_key"] != "sk_live_secret":
        print("  ERROR: scrub_secrets must not mutate its input")
        failed = True
    if scrubbed["annual"]["load"]["octopus"]["account_id"] != "A-1":
        print("  ERROR: non-secret values should survive scrubbing")
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_config import test_annual_config
```

```python
        ("annual_config", test_annual_config, "Annual prediction config validation tests", False),
```

Run: `cd coverage && ./run_all --test annual_config > /tmp/t7.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/t7.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual'`.

- [ ] **Step 3: Create `apps/predbat/annual.py` with the validation layer**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Annual prediction engine.

Projects a year of household electricity costs using the real Predbat planning
engine, reporting each month under three scenarios: no PV or battery, PV and
battery without Predbat, and with Predbat. Performs no HTTP itself; the weather
and tariff modules own all network access.
"""

import copy
from datetime import date

VALID_SHAPES = ["night", "day", "flat"]

DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_SAMPLES_PER_MONTH = 2
DEFAULT_PV10_DERATE_FALLBACK = 0.7
DEFAULT_DECLINATION = 35
DEFAULT_AZIMUTH = 180
DEFAULT_EFFICIENCY = 0.95
DEFAULT_HYBRID = True

# Substrings that mark a config value as secret and therefore scrubbable
SECRET_MARKERS = ["_key", "password", "token", "secret"]


class AnnualConfigError(ValueError):
    """Raised when the annual prediction config is invalid or self-contradictory."""


def scrub_secrets(config):
    """Return a deep copy of the config with secret-looking values replaced by "xxx".

    Mirrors the redaction ``create_debug_yaml()`` applies, so a results document or
    debug dump can never carry an API key.
    """
    if isinstance(config, dict):
        scrubbed = {}
        for key, value in config.items():
            if any(marker in str(key).lower() for marker in SECRET_MARKERS):
                scrubbed[key] = "xxx"
            else:
                scrubbed[key] = scrub_secrets(value)
        return scrubbed
    if isinstance(config, list):
        return [scrub_secrets(item) for item in config]
    return config


def _validate_solar(raw):
    """Normalise the solar array list, applying defaults and rejecting arrays without kwp."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise AnnualConfigError("annual.solar must be a list of arrays")

    arrays = []
    for index, array in enumerate(raw):
        if not isinstance(array, dict):
            raise AnnualConfigError("annual.solar[{}] must be a mapping".format(index))
        if "kwp" not in array:
            raise AnnualConfigError("annual.solar[{}] is missing kwp, the array's peak power in kW".format(index))
        normalised = dict(array)
        normalised["kwp"] = float(array["kwp"])
        normalised["declination"] = array.get("declination", DEFAULT_DECLINATION)
        normalised["azimuth"] = array.get("azimuth", DEFAULT_AZIMUTH)
        normalised["efficiency"] = float(array.get("efficiency", DEFAULT_EFFICIENCY))
        arrays.append(normalised)
    return arrays


def _validate_battery(raw):
    """Normalise the battery block, or return None for a run with no battery."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AnnualConfigError("annual.battery must be a mapping")
    if "size_kwh" not in raw:
        raise AnnualConfigError("annual.battery is missing size_kwh")
    if "inverter_kw" not in raw:
        raise AnnualConfigError("annual.battery is missing inverter_kw")

    inverter_kw = float(raw["inverter_kw"])
    return {
        "size_kwh": float(raw["size_kwh"]),
        "inverter_kw": inverter_kw,
        "export_limit_kw": float(raw.get("export_limit_kw", inverter_kw)),
        "hybrid": bool(raw.get("hybrid", DEFAULT_HYBRID)),
        "charge_rate_kw": float(raw.get("charge_rate_kw", inverter_kw)),
        "discharge_rate_kw": float(raw.get("discharge_rate_kw", inverter_kw)),
    }


def _validate_load(raw):
    """Normalise the load block and enforce the Octopus / manual exclusivity rule."""
    if not isinstance(raw, dict):
        raise AnnualConfigError("annual.load is required and must be a mapping")

    octopus = raw.get("octopus")
    has_manual = ("annual_kwh" in raw) or ("car_charging_kwh" in raw)

    if octopus and has_manual:
        raise AnnualConfigError("annual.load.octopus and annual.load.annual_kwh/car_charging_kwh are mutually exclusive: the Octopus consumption series already includes any car charging, so supplying both would double-count it")

    if not octopus and "annual_kwh" not in raw:
        raise AnnualConfigError("annual.load requires either annual_kwh or an octopus block")

    if octopus:
        if not isinstance(octopus, dict) or not octopus.get("api_key") or not octopus.get("account_id"):
            raise AnnualConfigError("annual.load.octopus requires both api_key and account_id")
        return {"octopus": dict(octopus), "shape": raw.get("shape", "flat"), "car_charging_kwh": 0.0}

    shape = raw.get("shape", "flat")
    if shape not in VALID_SHAPES:
        raise AnnualConfigError("annual.load.shape must be one of {}, got '{}'".format(VALID_SHAPES, shape))

    return {"annual_kwh": float(raw["annual_kwh"]), "shape": shape, "car_charging_kwh": float(raw.get("car_charging_kwh", 0.0))}


def _validate_tariff(raw):
    """Normalise the tariff block, requiring at least one import rate source.

    A URL containing {dno_region} with no dno_region supplied is rejected here
    rather than left to 404 at fetch time, where it would surface as an
    unavailable month and read like an Octopus outage.
    """
    if not isinstance(raw, dict):
        raise AnnualConfigError("annual.tariff is required and must be a mapping")
    if not raw.get("import_octopus_url") and not raw.get("rates_import"):
        raise AnnualConfigError("annual.tariff requires either import_octopus_url or rates_import")

    templated = [name for name in ["import_octopus_url", "export_octopus_url"] if raw.get(name) and "{dno_region}" in raw[name]]
    if templated and not raw.get("dno_region"):
        raise AnnualConfigError("annual.tariff.{} uses {{dno_region}} but annual.tariff.dno_region is not set; supply your Octopus region letter, for example 'A' for Eastern England".format(templated[0]))

    tariff = dict(raw)
    tariff["standing_charge_p_per_day"] = float(raw.get("standing_charge_p_per_day", 0.0))
    return tariff


def validate_config(config, today=None):
    """Validate and normalise an annual prediction config, returning a fully defaulted copy.

    Accepts either the wrapped form ({"annual": {...}}) or the inner mapping directly.
    Raises AnnualConfigError with an actionable message on any problem.
    """
    if not isinstance(config, dict):
        raise AnnualConfigError("The annual config must be a mapping")

    raw = config.get("annual", config)
    if not isinstance(raw, dict):
        raise AnnualConfigError("The annual config must be a mapping")

    location = raw.get("location")
    if not isinstance(location, dict):
        raise AnnualConfigError("annual.location is required, with either a postcode or latitude and longitude")
    if not location.get("postcode") and not ("latitude" in location and "longitude" in location):
        raise AnnualConfigError("annual.location needs either a postcode or both latitude and longitude")

    solar = _validate_solar(raw.get("solar"))
    battery = _validate_battery(raw.get("battery"))
    if not solar and battery is None:
        raise AnnualConfigError("annual needs at least one of solar or battery: with neither there is nothing to evaluate")

    samples_per_month = int(raw.get("samples_per_month", DEFAULT_SAMPLES_PER_MONTH))
    if samples_per_month < 1:
        raise AnnualConfigError("annual.samples_per_month must be at least 1, got {}".format(samples_per_month))

    if today is None:
        today = date.today()
    year = int(raw.get("year", today.year - 1))

    return {
        "location": dict(location),
        "year": year,
        "solar": solar,
        "battery": battery,
        "load": _validate_load(raw.get("load")),
        "tariff": _validate_tariff(raw.get("tariff")),
        "samples_per_month": samples_per_month,
        "timezone": raw.get("timezone", DEFAULT_TIMEZONE),
        "pv10_derate_fallback": float(raw.get("pv10_derate_fallback", DEFAULT_PV10_DERATE_FALLBACK)),
        "raw": scrub_secrets(copy.deepcopy(raw)),
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_config > /tmp/t7.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t7.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual.py apps/predbat/tests/test_annual_config.py apps/predbat/unit_test.py
git commit -m "feat(annual): add annual prediction config validation"
```

---

## Task 8: Headless PredBat bootstrap and per-sample state reset

`PredBat()` inherits `hass.Hass`, whose `__init__` reads `apps.yaml` from the current directory (or `$PREDBAT_APPS_FILE`) and opens `predbat.log` for append in the current directory. The tool therefore writes its own minimal `apps.yaml` into a working directory and points `PREDBAT_APPS_FILE` at it.

**State isolation is the principal risk of driving the real `PredBat` object.** Derived state leaks between runs — `apps/predbat/tests/test_single_debug.py` documents exactly this for `dynamic_load_baseline` and `battery_rate_max_export`. `reset_sample_state()` is the explicit allow-list that stops month N's result depending on month N-1 having run first.

**Files:**
- Modify: `apps/predbat/annual.py`
- Create: `apps/predbat/tests/test_annual_bootstrap.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual.validate_config` from Task 7.
- Produces:
  - `annual.AnnualNullHA` — a no-op Home Assistant interface
  - `annual.MINIMAL_APPS_YAML` — template string
  - `annual.write_minimal_apps_yaml(work_dir, timezone) -> str` — returns the written path
  - `annual.create_headless_predbat(work_dir, timezone, log) -> PredBat`
  - `annual.reset_sample_state(predbat)` — resets every field a previous sample could have left behind
  - `annual.apply_hardware(predbat, battery, solar)` — maps the config's battery block onto the PredBat instance

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_bootstrap.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction headless bootstrap and state reset."""

import os
import tempfile

import yaml

from annual import AnnualNullHA, apply_hardware, reset_sample_state, write_minimal_apps_yaml
from const import MINUTE_WATT


def test_annual_bootstrap(my_predbat):
    """Verify the minimal apps.yaml, the null HA interface, hardware mapping and state reset."""
    failed = False
    print("**** Testing annual bootstrap ****")

    print("Test: write_minimal_apps_yaml produces a parseable pred_bat config")
    with tempfile.TemporaryDirectory() as work_dir:
        path = write_minimal_apps_yaml(work_dir, "Europe/London")
        if not os.path.exists(path):
            print("  ERROR: apps.yaml was not written to {}".format(path))
            failed = True
        with open(path, "r") as handle:
            parsed = yaml.safe_load(handle)
        if "pred_bat" not in parsed:
            print("  ERROR: the written apps.yaml has no pred_bat key")
            failed = True
        else:
            section = parsed["pred_bat"]
            for key in ["module", "class", "prefix", "timezone", "currency_symbols", "threads"]:
                if key not in section:
                    print("  ERROR: the written apps.yaml is missing '{}'".format(key))
                    failed = True
            if section.get("threads") != 0:
                print("  ERROR: threads must be 0 so plan runs are deterministic, got {}".format(section.get("threads")))
                failed = True
            if section.get("timezone") != "Europe/London":
                print("  ERROR: the timezone should be written through, got {}".format(section.get("timezone")))
                failed = True

    print("Test: AnnualNullHA satisfies the interface PredBat calls without a Home Assistant")
    null_ha = AnnualNullHA()
    if null_ha.get_state("sensor.anything", default=7) != 7:
        print("  ERROR: get_state should return the supplied default")
        failed = True
    if null_ha.get_state(None) != {}:
        print("  ERROR: get_state with no entity should return an empty mapping of all states")
        failed = True
    if null_ha.get_history("sensor.anything") is not None:
        print("  ERROR: get_history should return None when no history exists")
        failed = True
    null_ha.set_state("sensor.written", "5", attributes={"unit": "kWh"})
    if null_ha.get_state("sensor.written") != "5":
        print("  ERROR: set_state then get_state should round trip")
        failed = True
    if null_ha.call_service("some/service", value=1) is not None:
        print("  ERROR: call_service should be a no-op returning None")
        failed = True

    print("Test: apply_hardware maps the battery block onto PredBat's internal units")
    battery = {"size_kwh": 9.5, "inverter_kw": 5.0, "export_limit_kw": 3.6, "hybrid": True, "charge_rate_kw": 3.7, "discharge_rate_kw": 4.2}
    apply_hardware(my_predbat, battery, [{"kwp": 5.6}])
    if abs(my_predbat.soc_max - 9.5) > 1e-9:
        print("  ERROR: soc_max expected 9.5, got {}".format(my_predbat.soc_max))
        failed = True
    if abs(my_predbat.inverter_limit - (5.0 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: inverter_limit should be in kW per minute, got {}".format(my_predbat.inverter_limit))
        failed = True
    if abs(my_predbat.export_limit - (3.6 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: export_limit should be in kW per minute, got {}".format(my_predbat.export_limit))
        failed = True
    if abs(my_predbat.battery_rate_max_charge - (3.7 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: battery_rate_max_charge should be in kW per minute, got {}".format(my_predbat.battery_rate_max_charge))
        failed = True
    if abs(my_predbat.battery_rate_max_discharge - (4.2 * 1000 / MINUTE_WATT)) > 1e-9:
        print("  ERROR: battery_rate_max_discharge should be in kW per minute, got {}".format(my_predbat.battery_rate_max_discharge))
        failed = True
    if my_predbat.inverter_hybrid is not True:
        print("  ERROR: inverter_hybrid should be True")
        failed = True

    print("Test: apply_hardware with no battery produces a zero-capacity system")
    apply_hardware(my_predbat, None, [{"kwp": 5.6}])
    if my_predbat.soc_max != 0.0 or my_predbat.soc_kw != 0.0:
        print("  ERROR: a battery-less run should have soc_max and soc_kw of 0, got {} / {}".format(my_predbat.soc_max, my_predbat.soc_kw))
        failed = True

    print("Test: reset_sample_state clears every field a previous sample could have left behind")
    my_predbat.dynamic_load_baseline = {5: 1.0}
    my_predbat.battery_rate_max_export = 99.0
    my_predbat.manual_charge_times = [1, 2, 3]
    my_predbat.manual_export_times = [4]
    my_predbat.manual_all_times = [5]
    my_predbat.cost_today_sofar = 123.0
    my_predbat.import_today_now = 4.0
    my_predbat.export_today_now = 5.0
    my_predbat.iboost_today = 6.0
    my_predbat.carbon_today_sofar = 7.0
    my_predbat.load_minutes_now = 8.0
    my_predbat.pv_today_now = 9.0
    my_predbat.charge_limit_best = [1.0]
    my_predbat.charge_window_best = [{"start": 0, "end": 30}]
    my_predbat.export_window_best = [{"start": 0, "end": 30}]
    my_predbat.export_limits_best = [50.0]
    my_predbat.plan_valid = True

    reset_sample_state(my_predbat)

    checks = [
        ("dynamic_load_baseline", {}),
        ("battery_rate_max_export", 0.0333),
        ("manual_charge_times", []),
        ("manual_export_times", []),
        ("manual_all_times", []),
        ("cost_today_sofar", 0),
        ("import_today_now", 0),
        ("export_today_now", 0),
        ("iboost_today", 0),
        ("carbon_today_sofar", 0),
        ("load_minutes_now", 0),
        ("pv_today_now", 0),
        ("charge_limit_best", []),
        ("charge_window_best", []),
        ("export_window_best", []),
        ("export_limits_best", []),
        ("plan_valid", False),
    ]
    for name, expected in checks:
        actual = getattr(my_predbat, name)
        if actual != expected:
            print("  ERROR: reset_sample_state left {} as {}, expected {}".format(name, actual, expected))
            failed = True

    print("Test: reset_sample_state disables the live-system behaviours that make no sense offline")
    if my_predbat.octopus_intelligent_charging is not False:
        print("  ERROR: octopus_intelligent_charging should be disabled")
        failed = True
    if my_predbat.load_forecast_only is not True:
        print("  ERROR: load_forecast_only must be True so the load profile is taken from load_forecast")
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_bootstrap import test_annual_bootstrap
```

```python
        ("annual_bootstrap", test_annual_bootstrap, "Annual prediction bootstrap and state reset tests", False),
```

Run: `cd coverage && ./run_all --test annual_bootstrap > /tmp/t8.txt 2>&1; grep -E "ERROR|ImportError|cannot import" /tmp/t8.txt`

Expected: FAIL with `cannot import name 'AnnualNullHA'`.

- [ ] **Step 3: Add the bootstrap to `apps/predbat/annual.py`**

Add to the imports at the top of the file:

```python
import os

from const import MINUTE_WATT
```

Then append:

```python
# Minimal apps.yaml for a headless run. PredBat's Hass base class reads this at
# construction time; nothing here talks to Home Assistant.
MINIMAL_APPS_YAML = """pred_bat:
  module: predbat
  class: PredBat
  prefix: predbat
  timezone: {timezone}
  currency_symbols:
  - '£'
  - 'p'
  threads: 0
  db_enable: false
  db_mirror_ha: false
  db_primary: false
  web_enable: false
  mcp_enable: false
  notify_devices: []
  days_previous:
  - 1
  days_previous_weight:
  - 1
  forecast_hours: 48
"""

# The default discharge power cap. A leaked full-precision value from a previous
# sample can flip a plan at a decision boundary, so it is reset explicitly.
DEFAULT_BATTERY_RATE_MAX_EXPORT = 0.0333


class AnnualNullHA:
    """A no-op Home Assistant interface for headless annual runs.

    Provides the subset of the interface PredBat touches during ``auto_config()``,
    ``load_user_config()`` and ``fetch_config_options()``. Nothing is published and
    no history exists, which is correct: every input the annual tool needs is
    injected directly.
    """

    def __init__(self):
        """Create an empty in-memory state store."""
        self.history_enable = False
        self.dummy_items = {}
        self.service_store_enable = False
        self.service_store = []
        self.db_primary = False

    def get_state(self, entity_id, default=None, attribute=None, refresh=False, raw=False):
        """Return a stored state, the supplied default, or all states when no entity is given."""
        if not entity_id:
            return {}
        if entity_id in self.dummy_items:
            result = self.dummy_items[entity_id]
            if raw:
                return result
            if isinstance(result, dict):
                return result.get(attribute, "") if attribute else result.get("state", default)
            return default if attribute else result
        return default

    def set_state(self, entity_id, state, attributes=None):
        """Store a state locally so subsequent reads round trip."""
        self.dummy_items[entity_id] = state
        return state

    def get_history(self, entity_id, now=None, days=30):
        """Return None: a headless annual run has no Home Assistant history."""
        return None

    def call_service(self, service, **kwargs):
        """Accept and discard a service call."""
        return None

    def get_service_store(self):
        """Return and clear the recorded service calls."""
        stored = self.service_store
        self.service_store = []
        return stored


def write_minimal_apps_yaml(work_dir, timezone):
    """Write the headless apps.yaml into ``work_dir`` and return its path."""
    os.makedirs(work_dir, exist_ok=True)
    path = os.path.join(work_dir, "apps.yaml")
    with open(path, "w") as handle:
        handle.write(MINIMAL_APPS_YAML.format(timezone=timezone))
    return path


def create_headless_predbat(work_dir, timezone, log):
    """Construct a PredBat instance with no Home Assistant connection.

    PredBat's Hass base class reads apps.yaml from ``$PREDBAT_APPS_FILE`` at
    construction time, so the environment variable is set before the import-time
    construction happens. The predbat import is deliberately local to this
    function so merely importing ``annual`` does not drag in the whole engine.
    """
    path = write_minimal_apps_yaml(work_dir, timezone)
    os.environ["PREDBAT_APPS_FILE"] = path

    import predbat

    instance = predbat.PredBat()
    instance.states = {}
    instance.reset()
    instance.update_time()
    instance.ha_interface = AnnualNullHA()
    instance.auto_config()
    instance.load_user_config()
    instance.fetch_config_options()
    instance.config_root = work_dir
    instance.save_restore_dir = work_dir
    instance.args["threads"] = 0
    instance.log = log
    return instance


def apply_hardware(predbat, battery, solar):
    """Map the config's battery block onto the PredBat instance.

    Rates are stored internally as kW per minute, matching
    ``Compare.apply_hardware_overrides()``. With no battery block the system is
    given zero capacity, which is how the no-battery scenario is expressed.
    """
    if battery is None:
        predbat.soc_max = 0.0
        predbat.soc_kw = 0.0
        predbat.battery_rate_max_charge = 0.0
        predbat.battery_rate_max_charge_dc = 0.0
        predbat.battery_rate_max_discharge = 0.0
        predbat.battery_rate_max_export = 0.0
        predbat.inverter_limit = (solar[0]["kwp"] if solar else 5.0) * 1000 / MINUTE_WATT
        predbat.export_limit = predbat.inverter_limit
        predbat.inverter_hybrid = False
        return

    predbat.soc_max = battery["size_kwh"]
    predbat.soc_kw = min(predbat.soc_kw, predbat.soc_max)
    predbat.inverter_limit = battery["inverter_kw"] * 1000 / MINUTE_WATT
    predbat.export_limit = battery["export_limit_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_charge = battery["charge_rate_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_charge_dc = predbat.battery_rate_max_charge
    predbat.battery_rate_max_discharge = battery["discharge_rate_kw"] * 1000 / MINUTE_WATT
    predbat.battery_rate_max_export = predbat.battery_rate_max_discharge
    predbat.inverter_hybrid = battery["hybrid"]


def reset_sample_state(predbat):
    """Reset every field a previous sample could have left behind.

    Without this, a month's result silently depends on what ran before it: the
    numbers stay plausible while becoming order-dependent. The list covers the
    accumulators, the previous plan, the manual overrides, and the two fields
    ``tests/test_single_debug.py`` documents as leaking between debug cases.
    """
    predbat.dynamic_load_baseline = {}
    predbat.battery_rate_max_export = DEFAULT_BATTERY_RATE_MAX_EXPORT

    predbat.cost_today_sofar = 0
    predbat.carbon_today_sofar = 0
    predbat.iboost_today = 0
    predbat.import_today_now = 0
    predbat.export_today_now = 0
    predbat.load_minutes_now = 0
    predbat.pv_today_now = 0

    predbat.manual_charge_times = []
    predbat.manual_export_times = []
    predbat.manual_freeze_charge_times = []
    predbat.manual_freeze_export_times = []
    predbat.manual_demand_times = []
    predbat.manual_all_times = []

    predbat.charge_limit_best = []
    predbat.charge_window_best = []
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.charge_limit = []
    predbat.charge_window = []
    predbat.export_window = []
    predbat.export_limits = []
    predbat.plan_valid = False

    predbat.octopus_intelligent_charging = False
    predbat.load_forecast_only = True
    predbat.load_scaling = 1.0
    predbat.load_scaling10 = 1.0
    predbat.load_inday_adjustment = 1.0
    predbat.load_scaling_dynamic = None
    predbat.manual_load_adjust = {}
    predbat.iboost_enable = False
    predbat.carbon_enable = False
    predbat.plan_debug = False
    predbat.debug_enable = False
    predbat.rate_import_replicated = {}
    predbat.rate_export_replicated = {}
    predbat.savings_last_updated = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_bootstrap > /tmp/t8.txt 2>&1; grep -E "ERROR|Traceback|AttributeError" /tmp/t8.txt`

Expected: no output. If an `AttributeError` names a field that does not exist on `PredBat`, check it against `predbat.py`'s `reset()` and remove or rename it — do not silently `getattr` around it, since a misspelled field is exactly the leak this function exists to prevent.

- [ ] **Step 5: Verify the headless bootstrap actually constructs**

`create_headless_predbat` is not covered by the unit test (it constructs a second engine). Smoke-test it once by hand:

```bash
cd coverage && python3 -c "
import sys
sys.path.insert(0, '../apps/predbat')
from annual import create_headless_predbat
pb = create_headless_predbat('/tmp/annual_work', 'Europe/London', print)
print('soc_max', pb.soc_max, 'forecast_minutes', pb.forecast_minutes)
" > /tmp/t8b.txt 2>&1; tail -20 /tmp/t8b.txt
```

Expected: prints a `soc_max` and `forecast_minutes` line with no traceback. If `apps.yaml` is missing a key that `load_user_config()` or `fetch_config_options()` requires, add that key to `MINIMAL_APPS_YAML` and re-run until it constructs cleanly.

- [ ] **Step 6: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual.py apps/predbat/tests/test_annual_bootstrap.py apps/predbat/unit_test.py
git commit -m "feat(annual): add headless PredBat bootstrap and per-sample state reset"
```

---

## Task 9: Sample selection

Picks the days to plan. Stratified by irradiance percentile so the answer does not swing on whether the sampled days happened to be sunny, and fully deterministic so the same config always yields the same days.

**Files:**
- Modify: `apps/predbat/annual.py`
- Create: `apps/predbat/tests/test_annual_sampling.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual_weather.WeatherYear.has_actual`, `daily_actual_kwh`.
- Produces:
  - `annual.select_samples(weather, year, month, samples_per_month, has_solar=True) -> list[tuple[date, float]]` — list of `(day, weight_in_days)` pairs, ordered by date

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_sampling.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for annual prediction sample day selection."""

from datetime import date, timedelta

from annual import select_samples


class FakeWeather:
    """A stub WeatherYear exposing only what select_samples needs."""

    def __init__(self, daily):
        """Hold a mapping of date to daily actual PV kWh."""
        self.daily = daily

    def has_actual(self, day):
        """Return True when the date has actual PV data."""
        return day in self.daily

    def daily_actual_kwh(self, day):
        """Return the actual PV kWh for the date."""
        return self.daily.get(day, 0.0)


def build_january(kwh_by_day, extra_days=1):
    """Build a FakeWeather covering January plus a buffer into February."""
    daily = {}
    for day_number, kwh in kwh_by_day.items():
        daily[date(2025, 1, day_number)] = kwh
    for offset in range(extra_days):
        daily[date(2025, 2, 1) + timedelta(days=offset)] = 1.0
    return FakeWeather(daily)


def test_annual_sampling(my_predbat):
    """Verify percentile sampling, weighting, determinism and degraded months."""
    failed = False
    print("**** Testing annual sample selection ****")

    # January: day N generates N kWh, so the sorted order is simply day order
    weather = build_january({day: float(day) for day in range(1, 32)})

    print("Test: two samples land on the 25th and 75th percentile days")
    samples = select_samples(weather, 2025, 1, 2)
    if len(samples) != 2:
        print("  ERROR: expected 2 samples, got {}".format(len(samples)))
        failed = True
    else:
        days = [day.day for day, _ in samples]
        # 31 candidates: indices int(31*0.25)=7 and int(31*0.75)=23 -> days 8 and 24
        if days != [8, 24]:
            print("  ERROR: expected days [8, 24], got {}".format(days))
            failed = True

    print("Test: weights sum to the number of days in the month")
    total_weight = sum(weight for _, weight in samples)
    if abs(total_weight - 31.0) > 1e-9:
        print("  ERROR: weights should sum to 31, got {}".format(total_weight))
        failed = True

    print("Test: selection is deterministic")
    if select_samples(weather, 2025, 1, 2) != samples:
        print("  ERROR: repeated selection returned different days")
        failed = True

    print("Test: samples are returned in date order")
    ordered = [day for day, _ in select_samples(weather, 2025, 1, 4)]
    if ordered != sorted(ordered):
        print("  ERROR: samples should be returned in date order, got {}".format(ordered))
        failed = True

    print("Test: samples are distinct")
    four = select_samples(weather, 2025, 1, 4)
    if len({day for day, _ in four}) != 4:
        print("  ERROR: expected 4 distinct days, got {}".format([day for day, _ in four]))
        failed = True

    print("Test: a day without a following day is excluded, since the 48 hour plan needs one")
    truncated = build_january({day: float(day) for day in range(1, 32)}, extra_days=0)
    truncated_days = [day for day, _ in select_samples(truncated, 2025, 1, 2)]
    if date(2025, 1, 31) in truncated_days:
        print("  ERROR: 31 January has no following day and must not be sampled")
        failed = True

    print("Test: a month with fewer candidates than samples uses every candidate")
    sparse = build_january({1: 5.0, 2: 9.0, 3: 1.0})
    sparse_samples = select_samples(sparse, 2025, 1, 4)
    # Day 3 has no following day (4 January is absent), so only days 1 and 2 are usable
    if len(sparse_samples) != 2:
        print("  ERROR: expected 2 usable samples, got {}".format(len(sparse_samples)))
        failed = True
    if abs(sum(weight for _, weight in sparse_samples) - 31.0) > 1e-9:
        print("  ERROR: weights must still sum to 31 when samples are scarce, got {}".format(sum(w for _, w in sparse_samples)))
        failed = True

    print("Test: a month with no usable candidates returns nothing")
    if select_samples(FakeWeather({}), 2025, 1, 2) != []:
        print("  ERROR: a month with no weather data should return no samples")
        failed = True

    print("Test: a battery-only run with no solar falls back to evenly spaced calendar days")
    no_solar = select_samples(FakeWeather({}), 2025, 1, 2, has_solar=False)
    if len(no_solar) != 2:
        print("  ERROR: with no solar, expected 2 calendar samples, got {}".format(len(no_solar)))
        failed = True
    elif [day.day for day in [entry[0] for entry in no_solar]] != [8, 24]:
        print("  ERROR: expected evenly spaced days [8, 24], got {}".format([entry[0].day for entry in no_solar]))
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_sampling import test_annual_sampling
```

```python
        ("annual_sampling", test_annual_sampling, "Annual prediction sample selection tests", False),
```

Run: `cd coverage && ./run_all --test annual_sampling > /tmp/t9.txt 2>&1; grep -E "ERROR|cannot import" /tmp/t9.txt`

Expected: FAIL with `cannot import name 'select_samples'`.

- [ ] **Step 3: Add sample selection to `apps/predbat/annual.py`**

Add `import calendar` and `from datetime import date, timedelta` to the imports (replacing the existing `from datetime import date` line), then append:

```python
def _percentile_indices(count, samples):
    """Return ``samples`` distinct indices spread evenly through ``count`` sorted items.

    Index i sits at percentile (i + 0.5) / samples, so two samples land at the 25th
    and 75th percentiles and each represents an equal share of the month. Collisions
    are resolved by walking to the nearest unused index, which only matters when the
    sample count approaches the number of candidate days.
    """
    chosen = []
    used = set()
    for index in range(samples):
        target = min(count - 1, int(count * ((index + 0.5) / samples)))
        while target in used and target < count - 1:
            target += 1
        while target in used and target > 0:
            target -= 1
        if target in used:
            continue
        used.add(target)
        chosen.append(target)
    return chosen


def select_samples(weather, year, month, samples_per_month, has_solar=True):
    """Choose the days to plan for one month, with the weight in days each represents.

    Days are ranked by their *actual* PV energy and sampled at even percentiles, so an
    unlucky sunny or dull draw cannot swing the month. Ranking uses actuals rather than
    the forecast: the aim is to represent what the month really contained, not what was
    predicted. Days without a following day are excluded because the 48 hour plan needs one.

    Weights always sum to the number of days in the month, so a month with fewer usable
    candidates than requested is scaled up rather than silently under-counted.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    all_days = [date(year, month, day) for day in range(1, days_in_month + 1)]

    if has_solar:
        candidates = [day for day in all_days if weather.has_actual(day) and weather.has_actual(day + timedelta(days=1))]
        candidates.sort(key=lambda day: (weather.daily_actual_kwh(day), day))
    else:
        # With no PV there is nothing to rank by, so fall back to evenly spaced calendar days
        candidates = all_days

    if not candidates:
        return []

    indices = _percentile_indices(len(candidates), samples_per_month)
    chosen = sorted({candidates[index] for index in indices})
    weight = days_in_month / float(len(chosen))
    return [(day, weight) for day in chosen]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_sampling > /tmp/t9.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t9.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual.py apps/predbat/tests/test_annual_sampling.py apps/predbat/unit_test.py
git commit -m "feat(annual): add irradiance-stratified sample day selection"
```

---

## Task 10: Three-scenario execution for one sampled day

The heart of the tool. Mirrors `calculate_yesterday()` in `apps/predbat/output.py`, which already runs these exact three scenarios against a past day.

Two things here are easy to get subtly wrong and both have dedicated tests:

1. **Predbat plans on the forecast series but is costed on the actuals series.** Without the Prediction swap before the final `run_prediction()`, Predbat gets perfect foresight and every result overstates its savings.
2. **Only scenario 3 gets a smart car.** Scenarios 1 and 2 charge on the same fixed off-peak timer, so Predbat is credited only for what it wins over a timer.

**Files:**
- Modify: `apps/predbat/annual.py`
- Create: `apps/predbat/tests/test_annual_scenarios.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual.reset_sample_state`, `annual.apply_hardware`, `annual_load.build_load_forecast`, `annual_weather.WeatherYear`, `annual_tariff.AnnualTariff`.
- Produces:
  - `annual.DAY_MINUTES` (1440), `annual.PLAN_MINUTES` (2880), `annual.START_SOC_KWH` (0.0)
  - `annual.build_step_data(predbat, pv_minute, pv_minute10) -> (load_step, pv_step, pv10_step)`
  - `annual.timer_charge_window(rate_import, car_kwh, car_rate_kw) -> list[dict]`
  - `annual.add_car_to_load(load_forecast, window, car_kwh) -> dict` — returns a new cumulative series with the car's energy inserted
  - `annual.prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc)` — injects all per-day state
  - `annual.run_day(predbat, config, weather, tariff, load_source, day, midnight_utc) -> dict` with keys `no_pvbat`, `without_predbat`, `with_predbat`, each a dict of `cost_p`, `import_kwh`, `export_kwh`, `pv_generated_kwh`, `battery_throughput_kwh`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_scenarios.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction three-scenario day runner."""

from annual import DAY_MINUTES, PLAN_MINUTES, add_car_to_load, timer_charge_window


def flat_rates(cheap_start, cheap_end, cheap_rate, peak_rate):
    """Build a 48 hour import rate dict with one cheap overnight band per day."""
    rates = {}
    for minute in range(PLAN_MINUTES):
        in_day = minute % DAY_MINUTES
        rates[minute] = cheap_rate if cheap_start <= in_day < cheap_end else peak_rate
    return rates


def test_annual_scenarios(my_predbat):
    """Verify the timer charge window and car load insertion helpers."""
    failed = False
    print("**** Testing annual scenario helpers ****")

    print("Test: timer_charge_window finds the cheapest band and sizes it to the car's energy")
    rates = flat_rates(cheap_start=30, cheap_end=330, cheap_rate=7.0, peak_rate=30.0)
    window = timer_charge_window(rates, car_kwh=14.8, car_rate_kw=7.4)
    if not window:
        print("  ERROR: expected a charge window")
        failed = True
    else:
        first = window[0]
        if first["start"] != 30:
            print("  ERROR: the window should start at the cheap band start 30, got {}".format(first["start"]))
            failed = True
        # 14.8 kWh at 7.4 kW is 2 hours
        if first["end"] - first["start"] != 120:
            print("  ERROR: expected a 120 minute window for 14.8 kWh at 7.4 kW, got {}".format(first["end"] - first["start"]))
            failed = True

    print("Test: a car needing more than the cheap band gets a window extended beyond it")
    long_window = timer_charge_window(rates, car_kwh=74.0, car_rate_kw=7.4)
    if long_window[0]["end"] - long_window[0]["start"] < 300:
        print("  ERROR: a 10 hour charge should extend past the 5 hour cheap band, got {} minutes".format(long_window[0]["end"] - long_window[0]["start"]))
        failed = True

    print("Test: zero car energy produces no window")
    if timer_charge_window(rates, car_kwh=0.0, car_rate_kw=7.4) != []:
        print("  ERROR: no car energy should produce no window")
        failed = True

    print("Test: add_car_to_load inserts exactly the car's energy and stays cumulative")
    base = {minute: 0.01 * minute for minute in range(PLAN_MINUTES + 1)}
    with_car = add_car_to_load(base, window, car_kwh=14.8)
    added = with_car[PLAN_MINUTES] - base[PLAN_MINUTES]
    if abs(added - 14.8) > 1e-6:
        print("  ERROR: expected 14.8 kWh added over the window, got {}".format(added))
        failed = True
    for minute in range(1, PLAN_MINUTES + 1):
        if with_car[minute] < with_car[minute - 1] - 1e-12:
            print("  ERROR: the series stopped being cumulative at minute {}".format(minute))
            failed = True
            break

    print("Test: add_car_to_load leaves minutes before the window untouched")
    if abs(with_car[10] - base[10]) > 1e-12:
        print("  ERROR: minutes before the window must be unchanged")
        failed = True

    print("Test: add_car_to_load does not mutate its input")
    if abs(base[PLAN_MINUTES] - 0.01 * PLAN_MINUTES) > 1e-9:
        print("  ERROR: add_car_to_load must not mutate the input series")
        failed = True

    print("Test: the car repeats on the second day of the window")
    second_day = [entry for entry in window if entry["start"] >= DAY_MINUTES]
    if not second_day:
        print("  ERROR: the timer should also charge on day two of the 48 hour window")
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_scenarios import test_annual_scenarios
```

```python
        ("annual_scenarios", test_annual_scenarios, "Annual prediction scenario helper tests", False),
```

Run: `cd coverage && ./run_all --test annual_scenarios > /tmp/t10.txt 2>&1; grep -E "ERROR|cannot import" /tmp/t10.txt`

Expected: FAIL with `cannot import name 'DAY_MINUTES'`.

- [ ] **Step 3: Add the scenario runner to `apps/predbat/annual.py`**

Add to the imports:

```python
from annual_load import build_load_forecast
from prediction import Prediction
```

Then append:

```python
DAY_MINUTES = 24 * 60
PLAN_MINUTES = 48 * 60

# Every sample starts from an empty battery. The compute_metric correction values
# whatever charge is left at the end, so the starting level does not bias the cost.
START_SOC_KWH = 0.0

# Cars are charged at this rate when the config gives no explicit figure
DEFAULT_CAR_RATE_KW = 7.4

# Maximum charge slots the dumb-battery baseline is allowed, matching the
# calculate_savings_max_charge_slots convention in calculate_yesterday()
BASELINE_MAX_CHARGE_SLOTS = 1


def build_step_data(predbat, pv_minute, pv_minute10):
    """Build the 5-minute step arrays the Prediction engine consumes.

    Mirrors the calls ``calculate_plan()`` makes in ``plan.py``. Because
    ``load_forecast_only`` is set, the historical branch of ``step_data_history``
    contributes nothing and the whole load profile comes from ``load_forecast``.
    """
    load_step = predbat.step_data_history(
        predbat.load_minutes,
        predbat.minutes_now,
        forward=False,
        scale_today=1.0,
        scale_fixed=1.0,
        type_load=True,
        load_forecast=predbat.load_forecast,
        load_scaling_dynamic=None,
        cloud_factor=None,
        load_adjust={},
        load_baseline={},
    )
    pv_step = predbat.step_data_history(pv_minute, predbat.minutes_now, forward=True, cloud_factor=None)
    pv10_step = predbat.step_data_history(pv_minute10, predbat.minutes_now, forward=True, cloud_factor=None, flip=True)
    return load_step, pv_step, pv10_step


def timer_charge_window(rate_import, car_kwh, car_rate_kw):
    """Return the fixed off-peak timer windows a non-Predbat household would use.

    Finds the cheapest contiguous band of each day and starts the charge there,
    extending past the band if the car needs longer than the cheap rate lasts.
    Returns one window per day of the 48 hour plan so the second day matches the first.
    """
    if car_kwh <= 0 or car_rate_kw <= 0:
        return []

    minutes_needed = int(round((car_kwh / car_rate_kw) * 60.0))
    if minutes_needed <= 0:
        return []

    windows = []
    for day_offset in range(2):
        base = day_offset * DAY_MINUTES
        day_rates = {minute: rate_import.get(base + minute, 0.0) for minute in range(DAY_MINUTES)}
        if not day_rates:
            continue
        cheapest = min(day_rates.values())
        # The first minute of the longest run at the cheapest rate
        start = None
        best_start = 0
        best_length = 0
        for minute in range(DAY_MINUTES + 1):
            at_cheapest = minute < DAY_MINUTES and day_rates[minute] <= cheapest + 1e-9
            if at_cheapest and start is None:
                start = minute
            elif not at_cheapest and start is not None:
                if minute - start > best_length:
                    best_length = minute - start
                    best_start = start
                start = None
        windows.append({"start": base + best_start, "end": base + best_start + minutes_needed})
    return windows


def add_car_to_load(load_forecast, windows, car_kwh):
    """Return a copy of the cumulative load series with the car's energy inserted.

    Used by the two baseline scenarios, where the car is simply extra load in a
    fixed timer window rather than something Predbat schedules.
    """
    if not windows or car_kwh <= 0:
        return dict(load_forecast)

    per_window = car_kwh / float(len(windows))
    additions = {}
    for window in windows:
        length = max(1, window["end"] - window["start"])
        per_minute = per_window / length
        for minute in range(window["start"], window["end"]):
            additions[minute] = additions.get(minute, 0.0) + per_minute

    result = {}
    running_extra = 0.0
    for minute in sorted(load_forecast.keys()):
        result[minute] = load_forecast[minute] + running_extra
        running_extra += additions.get(minute, 0.0)
    return result


def _apply_rates(predbat, rate_import, rate_export):
    """Install the day's rates and run the scans the planner depends on."""
    predbat.rate_import = rate_import
    predbat.rate_export = rate_export
    predbat.rate_low_threshold = 0
    predbat.rate_high_threshold = 0

    if predbat.rate_import:
        predbat.rate_scan(predbat.rate_import, print=False)
        predbat.rate_import, predbat.rate_import_replicated = predbat.rate_replicate(predbat.rate_import, is_import=True)
        predbat.rate_scan(predbat.rate_import, print=False)
    if predbat.rate_export:
        predbat.rate_scan_export(predbat.rate_export, print=False)
        predbat.rate_export, predbat.rate_export_replicated = predbat.rate_replicate(predbat.rate_export, is_import=False)
        predbat.rate_scan_export(predbat.rate_export, print=False)

    predbat.set_rate_thresholds()

    if predbat.rate_export:
        predbat.high_export_rates, export_lowest, _ = predbat.rate_scan_window(predbat.rate_export, 5, predbat.rate_export_cost_threshold, True)
        if predbat.rate_high_threshold == 0 and export_lowest <= predbat.rate_export_max:
            predbat.rate_export_cost_threshold = export_lowest
    else:
        predbat.high_export_rates = []

    if predbat.rate_import:
        predbat.low_rates, _, highest = predbat.rate_scan_window(predbat.rate_import, 5, predbat.rate_import_cost_threshold, False)
        if predbat.rate_low_threshold == 0 and highest >= predbat.rate_min:
            predbat.rate_import_cost_threshold = highest
    else:
        predbat.low_rates = []


def _baseline_charge_window(predbat):
    """Return the dumb battery's charge windows: the cheapest static band, charged to full.

    Mirrors the baseline in ``calculate_yesterday()`` — a household without Predbat
    that sets a timer for the cheapest rate and charges to 100%.
    """
    if not predbat.rate_import or predbat.soc_max <= 0:
        return [], []
    day_values = [value for minute, value in predbat.rate_import.items() if minute < DAY_MINUTES]
    if not day_values or min(day_values) == max(day_values):
        return [], []

    combine = predbat.combine_charge_slots
    predbat.combine_charge_slots = True
    windows, _, _ = predbat.rate_scan_window(predbat.rate_import, 5, min(day_values), False, return_raw=True)
    predbat.combine_charge_slots = combine

    windows = [window for window in windows if window["start"] < PLAN_MINUTES][:BASELINE_MAX_CHARGE_SLOTS]
    return windows, [predbat.soc_max for _ in windows]


def _billed_result(predbat, end_record, pv_step):
    """Run one scenario to completion and return its billed figures.

    The battery-value correction (metric_end minus metric_start) values whatever
    charge is left at the end, so a scenario cannot look cheap simply by finishing
    on an empty battery. This is the same correction ``Compare.run_scenario()`` applies.
    """
    cost, import_kwh_battery, import_kwh_house, export_kwh, _, final_soc, _, battery_cycle, metric_keep, final_iboost, final_carbon_g = predbat.run_prediction(
        predbat.charge_limit_best, predbat.charge_window_best, predbat.export_window_best, predbat.export_limits_best, False, end_record=end_record
    )
    metric_start, _ = predbat.compute_metric(end_record, predbat.soc_kw, predbat.soc_kw, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    metric_end, _ = predbat.compute_metric(end_record, final_soc, final_soc, cost, cost, final_iboost, final_iboost, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh)

    pv_generated = sum(value for minute, value in pv_step.items() if minute < end_record)
    return {
        "cost_p": metric_end - metric_start,
        "import_kwh": import_kwh_battery + import_kwh_house,
        "export_kwh": export_kwh,
        "pv_generated_kwh": pv_generated,
        "battery_throughput_kwh": battery_cycle,
    }


def prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc):
    """Inject every per-day input into the PredBat instance for one sampled day."""
    reset_sample_state(predbat)

    predbat.midnight_utc = midnight_utc
    predbat.now_utc = midnight_utc
    predbat.minutes_now = 0
    predbat.forecast_plan_hours = 48
    predbat.forecast_minutes = PLAN_MINUTES
    predbat.forecast_days = 2
    predbat.end_record = PLAN_MINUTES

    predbat.load_minutes = {}
    predbat.load_minutes_age = 0
    predbat.load_forecast = build_load_forecast(load_source, day, 2)

    rate_import, rate_export = tariff.rates_for(midnight_utc, PLAN_MINUTES)
    _apply_rates(predbat, rate_import, rate_export)

    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = START_SOC_KWH


def run_day(predbat, config, weather, tariff, load_source, day, midnight_utc):
    """Run all three scenarios against one sampled day and return their billed figures."""
    car_kwh = config["load"].get("car_charging_kwh", 0.0) / 365.0
    car_rate_kw = config["load"].get("car_rate_kw", DEFAULT_CAR_RATE_KW)

    prepare_sample(predbat, config, weather, tariff, load_source, day, midnight_utc)

    actual_pv = weather.pv_minutes("actual", midnight_utc, PLAN_MINUTES) if config["solar"] else {}
    forecast_pv = weather.pv_minutes("forecast", midnight_utc, PLAN_MINUTES) if config["solar"] else {}
    p10_pv = weather.pv_minutes_p10(midnight_utc, PLAN_MINUTES, day.month) if config["solar"] else {}

    timer_windows = timer_charge_window(predbat.rate_import, car_kwh, car_rate_kw)
    baseline_load = add_car_to_load(predbat.load_forecast, timer_windows, car_kwh)

    results = {}

    # Scenario 1: no PV, no battery. The car still charges on the same timer, so the
    # only difference between the scenarios is the system being evaluated.
    predbat.num_cars = 0
    predbat.load_forecast = baseline_load
    load_step, actual_step, _ = build_step_data(predbat, actual_pv, actual_pv)
    zero_step = {minute: 0.0 for minute in actual_step}
    predbat.charge_limit_best = []
    predbat.charge_window_best = []
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.prediction = Prediction(predbat, zero_step, zero_step, load_step, load_step, soc_kw=0, soc_max=0)
    results["no_pvbat"] = _billed_result(predbat, DAY_MINUTES, zero_step)

    # Scenario 2: PV and battery on a dumb cheapest-rate timer, no export optimisation
    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = START_SOC_KWH
    charge_window, charge_limit = _baseline_charge_window(predbat)
    predbat.charge_window_best = charge_window
    predbat.charge_limit_best = charge_limit
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.prediction = Prediction(predbat, actual_step, actual_step, load_step, load_step, soc_kw=START_SOC_KWH)
    results["without_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)

    # Scenario 3: Predbat plans on the FORECAST, then is costed against the ACTUALS.
    # Skipping the Prediction swap below would hand Predbat perfect foresight.
    predbat.load_forecast = build_load_forecast(load_source, day, 2)
    if car_kwh > 0:
        predbat.num_cars = 1
        predbat.car_charging_planned = [True]
        predbat.car_charging_plan_smart = [True]
        predbat.car_charging_battery_size = [max(car_kwh * 2, 50.0)]
        predbat.car_charging_limit = [car_kwh]
        predbat.car_charging_soc = [0.0]
        predbat.car_charging_rate = [car_rate_kw]
        predbat.car_charging_slots = [[] for _ in range(8)]
        predbat.car_charging_from_battery = False
    else:
        predbat.num_cars = 0

    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = START_SOC_KWH
    predbat.pv_forecast_minute = forecast_pv
    predbat.pv_forecast_minute10 = p10_pv
    predbat.calculate_plan(recompute=True, debug_mode=False, publish=False)

    # Swap in the actuals before costing
    forecast_load_step, _, _ = build_step_data(predbat, forecast_pv, p10_pv)
    predbat.prediction = Prediction(predbat, actual_step, actual_step, forecast_load_step, forecast_load_step, soc_kw=START_SOC_KWH)
    results["with_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)

    return results
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_scenarios > /tmp/t10.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t10.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual.py apps/predbat/tests/test_annual_scenarios.py apps/predbat/unit_test.py
git commit -m "feat(annual): add three-scenario day runner"
```

---

## Task 11: AnnualPredictor orchestration and results

Ties everything together and produces the results document the CLI prints and the future web UI will consume. Also carries the three integration tests the spec calls for — scenario ordering, plan-on-forecast/bill-on-actuals, and state isolation — because they can only be checked once a full day can be run.

**Files:**
- Modify: `apps/predbat/annual.py`
- Modify: `apps/predbat/annual_weather.py` (postcode resolution, so `annual.py` stays HTTP-free)
- Create: `apps/predbat/tests/test_annual_integration.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: everything from Tasks 3-10.
- Produces:
  - `annual_weather.resolve_postcode(postcode, fetch_json, log) -> (lat, lon) or None`
  - `annual.SCENARIO_KEYS` — `["no_pvbat", "without_predbat", "with_predbat"]`
  - `annual.average_rate(rates, minutes) -> float`
  - `annual.AnnualPredictor(config, log=None, storage=None, work_dir="./annual_work")` with `async run(progress=None) -> dict`

- [ ] **Step 1: Add postcode resolution to `apps/predbat/annual_weather.py`**

Append to that module:

```python
POSTCODE_URL = "https://api.postcodes.io/postcodes/{}"


async def resolve_postcode(postcode, fetch_json, log):
    """Resolve a UK postcode to (latitude, longitude), or None when it cannot be resolved."""
    data = await fetch_json(POSTCODE_URL.format(postcode))
    result = (data or {}).get("result", {}) if isinstance(data, dict) else {}
    if "latitude" in result and "longitude" in result:
        log("Annual: postcode {} resolved to latitude {} longitude {}".format(postcode, result["latitude"], result["longitude"]))
        return result["latitude"], result["longitude"]
    log("Warn: Annual: postcode {} could not be resolved".format(postcode))
    return None
```

- [ ] **Step 2: Write the failing integration test**

Create `apps/predbat/tests/test_annual_integration.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Integration tests for the annual prediction day runner.

These run real Predbat plans, so they are registered as slow.
"""

from datetime import date, datetime, timedelta

import pytz

from annual import DAY_MINUTES, PLAN_MINUTES, run_day, validate_config
from annual_load import SyntheticLoadProfile
from tests.test_infra import reset_inverter

SOLAR_CURVE = [0, 0, 0, 0, 0, 0, 0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0, 0.95, 0.85, 0.7, 0.5, 0.3, 0.1, 0, 0, 0, 0, 0]


class StubWeather:
    """A WeatherYear stand-in with a fixed daily solar curve and a forecast multiplier."""

    def __init__(self, peak_kw, forecast_multiplier=1.0, p10_ratio_value=0.8):
        """Configure the actual peak power and how much the forecast overstates it."""
        self.peak_kw = peak_kw
        self.forecast_multiplier = forecast_multiplier
        self.p10_ratio_value = p10_ratio_value

    def _series(self, midnight_utc, minutes, scale):
        """Build a per-minute kWh series from the fixed daily curve."""
        result = {}
        for minute in range(minutes):
            hour = (minute // 60) % 24
            result[minute] = (self.peak_kw * SOLAR_CURVE[hour] * scale) / 60.0
        return result

    def pv_minutes(self, series, midnight_utc, minutes):
        """Return the actual or forecast per-minute series."""
        return self._series(midnight_utc, minutes, 1.0 if series == "actual" else self.forecast_multiplier)

    def pv_minutes_p10(self, midnight_utc, minutes, month):
        """Return the P10 series, the forecast scaled by the month ratio."""
        return self._series(midnight_utc, minutes, self.forecast_multiplier * self.p10_ratio_value)

    def has_actual(self, day):
        """Every day has data in this stub."""
        return True

    def daily_actual_kwh(self, day):
        """Return the fixed daily total."""
        return sum(self._series(None, DAY_MINUTES, 1.0).values())

    def p10_ratio(self, month):
        """Return the fixed P10 ratio."""
        return self.p10_ratio_value


class StubTariff:
    """A tariff stand-in with a cheap overnight band and an expensive evening peak."""

    def __init__(self, cheap=7.0, normal=28.0, peak=45.0, export=15.0):
        """Configure the four rate levels."""
        self.cheap = cheap
        self.normal = normal
        self.peak = peak
        self.export = export
        self.standing_charge_p_per_day = 50.0

    def rates_for(self, midnight_utc, minutes):
        """Return (import, export) rate dicts keyed by absolute minute."""
        rate_import = {}
        rate_export = {}
        for minute in range(minutes):
            in_day = minute % DAY_MINUTES
            if 30 <= in_day < 330:
                rate_import[minute] = self.cheap
            elif 16 * 60 <= in_day < 19 * 60:
                rate_import[minute] = self.peak
            else:
                rate_import[minute] = self.normal
            rate_export[minute] = self.export
        return rate_import, rate_export

    def month_available(self, year, month):
        """Always available."""
        return True


def make_config(with_car=False):
    """Return a validated annual config for the integration tests."""
    raw = {
        "location": {"latitude": 51.5, "longitude": -0.1},
        "solar": [{"kwp": 5.0}],
        "battery": {"size_kwh": 10.0, "inverter_kw": 5.0},
        "load": {"annual_kwh": 3800, "shape": "flat"},
        "tariff": {"rates_import": [{"rate": 28.0}]},
        "year": 2025,
    }
    if with_car:
        raw["load"]["car_charging_kwh"] = 2500
    return validate_config(raw, today=date(2026, 7, 25))


def run_one(my_predbat, config, weather, day):
    """Run all three scenarios for one day and return the results dict."""
    reset_inverter(my_predbat)
    midnight = pytz.utc.localize(datetime(day.year, day.month, day.day))
    load_source = SyntheticLoadProfile(annual_kwh=config["load"]["annual_kwh"], shape=config["load"]["shape"], year=config["year"])
    return run_day(my_predbat, config, weather, StubTariff(), load_source, day, midnight)


def test_annual_integration(my_predbat):
    """Verify scenario ordering, the forecast/actuals split, and state isolation."""
    failed = False
    print("**** Testing annual integration ****")

    config = make_config()
    weather = StubWeather(peak_kw=4.0)
    day = date(2025, 5, 15)

    print("Test: the three scenarios run and produce the expected keys")
    results = run_one(my_predbat, config, weather, day)
    for key in ["no_pvbat", "without_predbat", "with_predbat"]:
        if key not in results:
            print("  ERROR: missing scenario '{}'".format(key))
            failed = True
            continue
        for field in ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh"]:
            if field not in results[key]:
                print("  ERROR: scenario '{}' is missing field '{}'".format(key, field))
                failed = True

    print("Test: predbat_cost <= without_predbat_cost <= no_pvbat_cost")
    if not failed:
        predbat_cost = results["with_predbat"]["cost_p"]
        baseline_cost = results["without_predbat"]["cost_p"]
        none_cost = results["no_pvbat"]["cost_p"]
        if not predbat_cost <= baseline_cost + 1e-6:
            print("  ERROR: Predbat cost {} should not exceed the dumb baseline {}".format(predbat_cost, baseline_cost))
            failed = True
        if not baseline_cost <= none_cost + 1e-6:
            print("  ERROR: the PV/battery baseline {} should not exceed the no-system cost {}".format(baseline_cost, none_cost))
            failed = True

    print("Test: the no-PV/no-battery scenario generates nothing and stores nothing")
    if results["no_pvbat"]["pv_generated_kwh"] != 0.0:
        print("  ERROR: the no-system scenario should generate no PV, got {}".format(results["no_pvbat"]["pv_generated_kwh"]))
        failed = True
    if results["no_pvbat"]["battery_throughput_kwh"] != 0.0:
        print("  ERROR: the no-system scenario should cycle no battery, got {}".format(results["no_pvbat"]["battery_throughput_kwh"]))
        failed = True

    print("Test: Predbat is billed on actuals, not on the forecast it planned against")
    # The forecast claims three times the real generation. If the Prediction swap were
    # skipped, pv_generated_kwh and the cost would follow the inflated forecast.
    inflated = StubWeather(peak_kw=4.0, forecast_multiplier=3.0)
    inflated_results = run_one(my_predbat, config, inflated, day)
    honest_pv = results["with_predbat"]["pv_generated_kwh"]
    inflated_pv = inflated_results["with_predbat"]["pv_generated_kwh"]
    if abs(inflated_pv - honest_pv) > 0.01:
        print("  ERROR: reported PV should track actuals ({}) regardless of the forecast, got {}".format(honest_pv, inflated_pv))
        failed = True
    if inflated_results["with_predbat"]["cost_p"] < results["with_predbat"]["cost_p"] - 1e-6:
        print("  ERROR: planning against an over-optimistic forecast must not make the billed cost cheaper")
        failed = True

    print("Test: state isolation - a day run in isolation matches the same day run after another")
    isolated = run_one(my_predbat, config, weather, day)
    _ = run_one(my_predbat, config, StubWeather(peak_kw=1.0), date(2025, 11, 20))
    after_other = run_one(my_predbat, config, weather, day)
    for scenario in ["no_pvbat", "without_predbat", "with_predbat"]:
        for field in ["cost_p", "import_kwh", "export_kwh", "battery_throughput_kwh"]:
            first = isolated[scenario][field]
            second = after_other[scenario][field]
            if abs(first - second) > 1e-6:
                print("  ERROR: {}.{} changed from {} to {} depending on what ran before it".format(scenario, field, first, second))
                failed = True

    print("Test: a car charging config still produces an ordered result")
    car_config = make_config(with_car=True)
    car_results = run_one(my_predbat, car_config, weather, day)
    if car_results["with_predbat"]["cost_p"] > car_results["without_predbat"]["cost_p"] + 1e-6:
        print("  ERROR: with a car, Predbat cost {} should not exceed the timer baseline {}".format(car_results["with_predbat"]["cost_p"], car_results["without_predbat"]["cost_p"]))
        failed = True
    if car_results["no_pvbat"]["import_kwh"] <= results["no_pvbat"]["import_kwh"]:
        print("  ERROR: adding a car should raise the no-system import, got {} vs {}".format(car_results["no_pvbat"]["import_kwh"], results["no_pvbat"]["import_kwh"]))
        failed = True

    return failed
```

- [ ] **Step 3: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_integration import test_annual_integration
```

```python
        ("annual_integration", test_annual_integration, "Annual prediction integration tests", True),
```

Note the trailing `True` — this runs real plans, so it is marked slow and skipped by `--quick`.

Run: `cd coverage && ./run_all --test annual_integration > /tmp/t11.txt 2>&1; grep -E "ERROR|Traceback|cannot import" /tmp/t11.txt`

Expected: failures. `run_day` exists from Task 10, so this is a genuine behavioural test — expect to iterate on it. If the scenario-ordering assertion fails, do **not** relax the assertion: it is the property the whole tool exists to demonstrate. Debug the scenario setup instead, comparing against `calculate_yesterday()` in `apps/predbat/output.py`.

- [ ] **Step 4: Fix whatever the integration test exposes**

Likely areas, in order of probability:

1. `_apply_rates()` — if `rate_scan_window` returns no low rates, the dumb baseline never charges and scenario 2 equals scenario 1. Check `predbat.rate_import_cost_threshold` after `set_rate_thresholds()`.
2. `build_step_data()` — if `load_step` is all zeros, confirm `predbat.load_forecast_only` is `True` and that `load_forecast` is cumulative and reaches minute `PLAN_MINUTES`.
3. `calculate_plan()` — if it raises, check `predbat.args["threads"] == 0` so no process pool is created, and that `predbat.end_record` is set.
4. State isolation — if results differ between runs, add the offending field to `reset_sample_state()`.

- [ ] **Step 5: Add the orchestrator to `apps/predbat/annual.py`**

Add to the imports. Note `datetime` joins the existing `from datetime import date, timedelta` line, and `pytz` moves to module scope rather than being imported inside `run()`:

```python
import pytz

from datetime import date, datetime, timedelta

from annual_load import OctopusConsumptionLoadProfile, SyntheticLoadProfile
from annual_tariff import AnnualTariff
from annual_weather import AnnualWeather, resolve_postcode
```

and delete the `import pytz` line from inside `run()` shown below.

Then append:

```python
SCENARIO_KEYS = ["no_pvbat", "without_predbat", "with_predbat"]

SCENARIO_FIELDS = ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh"]


def average_rate(rates, minutes):
    """Return the mean rate across the first ``minutes`` of a rate dict."""
    values = [rates[minute] for minute in range(minutes) if minute in rates]
    return (sum(values) / len(values)) if values else 0.0


class AnnualPredictor:
    """Projects a year of electricity costs under three scenarios using the Predbat engine."""

    def __init__(self, config, log=None, storage=None, work_dir="./annual_work"):
        """Validate the config and prepare the run."""
        self.log = log or print
        self.config = validate_config(config)
        self.storage = storage
        self.work_dir = work_dir
        self.predbat = None
        self.weather = None
        self.tariff = None
        self.load_source = None
        self.caveats = []

    async def _resolve_location(self, weather_fetch):
        """Return (latitude, longitude) from the config, resolving a postcode if needed."""
        location = self.config["location"]
        if "latitude" in location and "longitude" in location:
            return location["latitude"], location["longitude"]
        resolved = await resolve_postcode(location["postcode"], weather_fetch, self.log)
        if not resolved:
            raise AnnualConfigError("annual.location.postcode '{}' could not be resolved; supply latitude and longitude instead".format(location["postcode"]))
        return resolved

    async def _build_load_source(self):
        """Build the load profile source, falling back to synthetic if Octopus data fails."""
        load_config = self.config["load"]
        year = self.config["year"]

        if "octopus" not in load_config:
            return SyntheticLoadProfile(annual_kwh=load_config["annual_kwh"], shape=load_config["shape"], year=year)

        # A synthetic profile at the UK average backs the real data so an isolated
        # missing day does not silently become zero consumption
        fallback = SyntheticLoadProfile(annual_kwh=2700.0, shape="flat", year=year)
        source = OctopusConsumptionLoadProfile(
            api_key=load_config["octopus"]["api_key"],
            account_id=load_config["octopus"]["account_id"],
            log=self.log,
            storage=self.storage,
            fallback=fallback,
        )
        if not await source.fetch(year):
            raise AnnualConfigError("Octopus consumption data could not be downloaded for {}; check the API key and account id".format(year))
        return source

    def _month_scenarios(self, samples, day_results):
        """Weight each sample's daily figures into monthly totals per scenario."""
        totals = {key: {field: 0.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}
        for (_, weight), result in zip(samples, day_results):
            for key in SCENARIO_KEYS:
                for field in SCENARIO_FIELDS:
                    totals[key][field] += result[key][field] * weight
        return totals

    async def run(self, progress=None):
        """Run the full annual projection and return the results document."""
        year = self.config["year"]
        samples_per_month = self.config["samples_per_month"]
        has_solar = bool(self.config["solar"])

        weather_client = AnnualWeather(
            self.config["solar"],
            latitude=0.0,
            longitude=0.0,
            log=self.log,
            storage=self.storage,
            p10_fallback=self.config["pv10_derate_fallback"],
        )
        latitude, longitude = await self._resolve_location(weather_client.fetch_json)
        weather_client.latitude = latitude
        weather_client.longitude = longitude

        self.weather = await weather_client.fetch(year) if has_solar else None
        if has_solar and not self.weather.forecast_available:
            self.caveats.append("The Open-Meteo forecast archive did not cover {}, so Predbat planned against actuals and P10 used the flat {} derate. Savings are likely overstated.".format(year, self.config["pv10_derate_fallback"]))
        elif has_solar and self.weather.fallback_months:
            self.caveats.append("Months {} had too few forecast/actual day pairs, so their P10 used the flat {} derate.".format(sorted(self.weather.fallback_months), self.config["pv10_derate_fallback"]))
        if has_solar:
            self.caveats.append("The forecast-versus-ERA5 gap includes systematic model bias as well as forecast error, so measured solar uncertainty is slightly overstated.")
        self.caveats.append("self_consumed_kwh is approximate: when the battery exports grid-charged energy it is understated.")

        self.predbat = create_headless_predbat(self.work_dir, self.config["timezone"], self.log)
        self.load_source = await self._build_load_source()
        self.tariff = AnnualTariff(self.config["tariff"], log=self.log, predbat=self.predbat, storage=self.storage)

        zone = pytz.timezone(self.config["timezone"])
        months = []
        total_units = 12
        completed = 0

        for month in range(1, 13):
            if progress:
                progress(completed, total_units, "Month {:02d}/{}".format(month, year))

            days_in_month = calendar.monthrange(year, month)[1]
            standing_charge_p = self.tariff.standing_charge_p_per_day * days_in_month

            if not await self.tariff.fetch_month(year, month):
                months.append({"month": month, "status": "unavailable", "reason": "no rate data available", "days": days_in_month, "standing_charge_p": standing_charge_p})
                completed += 1
                continue
            # The 48 hour plan for the last sampled day can spill into the next month
            next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
            await self.tariff.fetch_month(next_year, next_month)

            samples = select_samples(self.weather, year, month, samples_per_month, has_solar=has_solar) if has_solar else select_samples(None, year, month, samples_per_month, has_solar=False)
            if not samples:
                months.append({"month": month, "status": "unavailable", "reason": "no usable weather days", "days": days_in_month, "standing_charge_p": standing_charge_p})
                completed += 1
                continue

            day_results = []
            for day, _ in samples:
                midnight_utc = zone.localize(datetime(day.year, day.month, day.day)).astimezone(pytz.utc)
                day_results.append(run_day(self.predbat, self.config, self.weather, self.tariff, self.load_source, day, midnight_utc))

            totals = self._month_scenarios(samples, day_results)
            first_midnight = zone.localize(datetime(samples[0][0].year, samples[0][0].month, samples[0][0].day)).astimezone(pytz.utc)
            _, rate_export = self.tariff.rates_for(first_midnight, DAY_MINUTES)
            export_rate = average_rate(rate_export, DAY_MINUTES)

            scenarios = {}
            for key in SCENARIO_KEYS:
                entry = {field: totals[key][field] for field in SCENARIO_FIELDS}
                entry["export_credit_p"] = entry["export_kwh"] * export_rate
                entry["self_consumed_kwh"] = max(0.0, entry["pv_generated_kwh"] - entry["export_kwh"])
                scenarios[key] = {name: round(value, 3) for name, value in entry.items()}

            months.append(
                {
                    "month": month,
                    "status": "ok",
                    "days": days_in_month,
                    "sampled_days": [day.isoformat() for day, _ in samples],
                    "standing_charge_p": round(standing_charge_p, 3),
                    "scenarios": scenarios,
                }
            )
            completed += 1

        if progress:
            progress(total_units, total_units, "Complete")

        return self._build_results(months)

    def _build_results(self, months):
        """Assemble the final results document from the per-month rows."""
        included = [entry for entry in months if entry["status"] == "ok"]
        excluded = [entry["month"] for entry in months if entry["status"] != "ok"]

        annual_scenarios = {}
        for key in SCENARIO_KEYS:
            annual_scenarios[key] = {field: round(sum(entry["scenarios"][key][field] for entry in included), 3) for field in SCENARIO_FIELDS + ["export_credit_p", "self_consumed_kwh"]}
        standing_total = round(sum(entry["standing_charge_p"] for entry in included), 3)

        savings = {}
        if annual_scenarios:
            savings["pv_battery_vs_none_p"] = round(annual_scenarios["no_pvbat"]["cost_p"] - annual_scenarios["without_predbat"]["cost_p"], 3)
            savings["predbat_vs_baseline_p"] = round(annual_scenarios["without_predbat"]["cost_p"] - annual_scenarios["with_predbat"]["cost_p"], 3)

        return {
            "year": self.config["year"],
            "config": self.config["raw"],
            "months": months,
            "annual": {
                "scenarios": annual_scenarios,
                "standing_charge_p": standing_total,
                "savings": savings,
                "months_included": len(included),
                "months_excluded": excluded,
            },
            "caveats": self.caveats,
        }
```

- [ ] **Step 6: Run the integration test and the whole annual suite**

Run: `cd coverage && ./run_all -k annual > /tmp/t11.txt 2>&1; grep -E "ERROR|Traceback|FAILED" /tmp/t11.txt`

Expected: no output.

- [ ] **Step 7: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual.py apps/predbat/annual_weather.py apps/predbat/tests/test_annual_integration.py apps/predbat/unit_test.py
git commit -m "feat(annual): add AnnualPredictor orchestration and results document"
```

---

## Task 12: Command line interface

**Files:**
- Create: `apps/predbat/annual_cli.py`
- Create: `apps/predbat/tests/test_annual_cli.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual.AnnualPredictor`.
- Produces:
  - `annual_cli.format_table(results, currency="p") -> str`
  - `annual_cli.main(argv=None) -> int`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_cli.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction command line output."""

from annual_cli import format_table


def sample_results():
    """Return a small results document covering an ok month and an unavailable one."""
    scenarios = {
        "no_pvbat": {"cost_p": 12000.0, "import_kwh": 400.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p": 0.0, "self_consumed_kwh": 0.0},
        "without_predbat": {"cost_p": 8000.0, "import_kwh": 300.0, "export_kwh": 20.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 90.0, "export_credit_p": 300.0, "self_consumed_kwh": 100.0},
        "with_predbat": {"cost_p": 6000.0, "import_kwh": 280.0, "export_kwh": 45.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 140.0, "export_credit_p": 675.0, "self_consumed_kwh": 75.0},
    }
    return {
        "year": 2025,
        "config": {},
        "months": [
            {"month": 1, "status": "ok", "days": 31, "sampled_days": ["2025-01-08", "2025-01-24"], "standing_charge_p": 1860.0, "scenarios": scenarios},
            {"month": 2, "status": "unavailable", "reason": "no rate data available", "days": 28, "standing_charge_p": 1680.0},
        ],
        "annual": {
            "scenarios": scenarios,
            "standing_charge_p": 1860.0,
            "savings": {"pv_battery_vs_none_p": 4000.0, "predbat_vs_baseline_p": 2000.0},
            "months_included": 1,
            "months_excluded": [2],
        },
        "caveats": ["An example caveat."],
    }


def test_annual_cli(my_predbat):
    """Verify the table output reports every month, including excluded ones."""
    failed = False
    print("**** Testing annual CLI output ****")

    table = format_table(sample_results())

    print("Test: the table names the year and every scenario")
    for fragment in ["2025", "No PV/Battery", "Without Predbat", "With Predbat"]:
        if fragment not in table:
            print("  ERROR: the table should mention '{}'".format(fragment))
            failed = True

    print("Test: an unavailable month is shown as excluded, never as zero")
    if "unavailable" not in table.lower():
        print("  ERROR: the table must state that February was unavailable")
        failed = True
    if "no rate data available" not in table:
        print("  ERROR: the table should state why the month was excluded")
        failed = True

    print("Test: annual savings appear")
    if "Savings" not in table:
        print("  ERROR: the table should include a savings section")
        failed = True

    print("Test: caveats are printed rather than buried in the JSON")
    if "An example caveat." not in table:
        print("  ERROR: caveats must be shown to the user")
        failed = True

    print("Test: the excluded-month count is stated alongside the annual totals")
    if "1 of 12" not in table:
        print("  ERROR: the table should state how many months are included, got:\n{}".format(table))
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`:

```python
from tests.test_annual_cli import test_annual_cli
```

```python
        ("annual_cli", test_annual_cli, "Annual prediction CLI output tests", False),
```

Run: `cd coverage && ./run_all --test annual_cli > /tmp/t12.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/t12.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_cli'`.

- [ ] **Step 3: Create `apps/predbat/annual_cli.py`**

```python
#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Command line entry point for the annual prediction tool.

Usage:
    python3 annual_cli.py --config annual.yaml --out results.json
"""

import argparse
import asyncio
import calendar
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from annual import SCENARIO_KEYS, AnnualConfigError, AnnualPredictor  # noqa: E402
from storage import StorageLocalFiles  # noqa: E402

SCENARIO_LABELS = {"no_pvbat": "No PV/Battery", "without_predbat": "Without Predbat", "with_predbat": "With Predbat"}


def format_table(results, currency="p"):
    """Render the results document as a human-readable table."""
    lines = []
    lines.append("Annual prediction for {}".format(results["year"]))
    lines.append("")
    header = "{:<6}".format("Month") + "".join("{:>20}".format(SCENARIO_LABELS[key]) for key in SCENARIO_KEYS)
    lines.append(header)
    lines.append("-" * len(header))

    for entry in results["months"]:
        name = calendar.month_abbr[entry["month"]]
        if entry["status"] != "ok":
            lines.append("{:<6}{:>60}".format(name, "unavailable - {}".format(entry.get("reason", "unknown"))))
            continue
        row = "{:<6}".format(name)
        for key in SCENARIO_KEYS:
            row += "{:>20}".format("{:.2f}{}".format(entry["scenarios"][key]["cost_p"] / 100.0, currency.upper() if currency == "p" else currency))
        lines.append(row)

    annual = results["annual"]
    lines.append("-" * len(header))
    total_row = "{:<6}".format("Year")
    for key in SCENARIO_KEYS:
        total_row += "{:>20}".format("{:.2f}".format(annual["scenarios"].get(key, {}).get("cost_p", 0.0) / 100.0))
    lines.append(total_row)
    lines.append("")
    lines.append("Based on {} of 12 months.".format(annual["months_included"]))
    if annual["months_excluded"]:
        lines.append("Excluded months: {}".format(", ".join(calendar.month_abbr[month] for month in annual["months_excluded"])))
    lines.append("")
    lines.append("Savings")
    lines.append("  PV and battery vs no system: {:.2f}".format(annual["savings"].get("pv_battery_vs_none_p", 0.0) / 100.0))
    lines.append("  Predbat vs without Predbat:  {:.2f}".format(annual["savings"].get("predbat_vs_baseline_p", 0.0) / 100.0))
    lines.append("  Standing charge (all scenarios): {:.2f}".format(annual["standing_charge_p"] / 100.0))

    if results.get("caveats"):
        lines.append("")
        lines.append("Caveats")
        for caveat in results["caveats"]:
            lines.append("  - {}".format(caveat))

    return "\n".join(lines)


def make_progress(quiet):
    """Return a progress callback that writes to stderr, or None when quiet."""
    if quiet:
        return None

    def progress(completed, total, message):
        """Report progress to stderr so stdout stays parseable."""
        sys.stderr.write("[{}/{}] {}\n".format(completed, total, message))
        sys.stderr.flush()

    return progress


def main(argv=None):
    """Parse arguments, run the projection, and write the results. Returns an exit code."""
    parser = argparse.ArgumentParser(description="Project a year of electricity costs using the Predbat engine")
    parser.add_argument("--config", required=True, help="Path to the annual prediction YAML config")
    parser.add_argument("--out", default=None, help="Write the results JSON to this path")
    parser.add_argument("--work-dir", default="./annual_work", help="Working directory for the headless Predbat instance and cache")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = parser.parse_args(argv)

    try:
        with open(args.config, "r") as handle:
            config = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        sys.stderr.write("Could not read config {}: {}\n".format(args.config, error))
        return 2

    storage = StorageLocalFiles(args.work_dir, print)

    try:
        predictor = AnnualPredictor(config, log=print if not args.quiet else lambda *a, **k: None, storage=storage, work_dir=args.work_dir)
        results = asyncio.get_event_loop().run_until_complete(predictor.run(progress=make_progress(args.quiet)))
    except AnnualConfigError as error:
        sys.stderr.write("Config error: {}\n".format(error))
        return 2

    if args.out:
        with open(args.out, "w") as handle:
            json.dump(results, handle, indent=2)
        sys.stderr.write("Results written to {}\n".format(args.out))

    print(format_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_cli > /tmp/t12.txt 2>&1; grep -E "ERROR|Traceback" /tmp/t12.txt`

Expected: no output.

- [ ] **Step 5: Verify the CLI rejects a bad config cleanly**

```bash
cd coverage && echo "annual: {}" > /tmp/bad_annual.yaml && python3 ../apps/predbat/annual_cli.py --config /tmp/bad_annual.yaml; echo "exit=$?"
```

Expected: `Config error: annual.location is required...` and `exit=2`, with no traceback.

- [ ] **Step 6: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add apps/predbat/annual_cli.py apps/predbat/tests/test_annual_cli.py apps/predbat/unit_test.py
git commit -m "feat(annual): add annual prediction command line interface"
```

---

## Task 13: Documentation

**Files:**
- Create: `docs/annual-prediction.md`
- Modify: `mkdocs.yml`
- Modify: `.cspell/custom-dictionary-workspace.txt`

- [ ] **Step 1: Write `docs/annual-prediction.md`**

```markdown
# Annual prediction

The annual prediction tool projects a year of household electricity costs using the
real Predbat planning engine. For each month it reports three scenarios:

1. **No PV, no battery** — the counterfactual bill.
2. **PV and battery, without Predbat** — a battery charging on a static cheap-rate timer.
3. **PV and battery, with Predbat** — the optimiser's plan.

It is a standalone command line tool; it does not need Home Assistant or any hardware.

## How it works

For each month the tool picks sample days by irradiance percentile, so the answer does
not swing on whether the sampled days happened to be sunny. Two samples per month is the
default; each represents half the month.

Each sampled day gets a 48-hour plan starting at midnight, but only the first 24 hours
are billed — the second day exists so the optimiser does not artificially drain the
battery at the horizon. Whatever charge is left at the end is valued, so a scenario
cannot look cheap by finishing empty.

Predbat plans against the **archived weather forecast** for that date and is costed
against **ERA5 actuals**. This matters: costing against the same series it planned from
would hand Predbat perfect foresight and overstate its savings.

Solar uncertainty (P10) is derived from measured forecast error — for each month, the
10th percentile of the actual-over-forecast daily energy ratio.

## Configuration

```yaml
annual:
  location:
    postcode: "SW1A 1AA"        # or latitude/longitude
  year: 2025                     # defaults to the most recent complete calendar year

  solar:                         # omit for a battery-only run
    - kwp: 5.6
      declination: 35            # pitch in degrees, default 35
      azimuth: 180               # 180 = south, default 180
      efficiency: 0.95           # default 0.95

  battery:                       # omit for a PV-only run
    size_kwh: 9.5
    inverter_kw: 5.0
    export_limit_kw: 5.0         # defaults to inverter_kw
    hybrid: true                 # false = AC coupled
    charge_rate_kw: 3.6          # defaults to inverter_kw
    discharge_rate_kw: 3.6       # defaults to inverter_kw

  load:
    annual_kwh: 3800
    shape: flat                  # night | day | flat
    car_charging_kwh: 2500       # annual, 0 to disable

  tariff:
    import_octopus_url: "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{dno_region}/standard-unit-rates/"
    export_octopus_url: "..."
    dno_region: "A"              # required when a URL contains {dno_region}
    standing_charge_p_per_day: 60.0

  samples_per_month: 2
```

Octopus product codes are region-suffixed. If your tariff URL contains `{dno_region}`
you must also set `dno_region` to your region letter (`A` for Eastern England, and so
on) — the tool rejects the config otherwise rather than letting the request 404 and
reporting the month as unavailable.

Instead of `annual_kwh`, `shape` and `car_charging_kwh` you may supply real consumption:

```yaml
  load:
    octopus:
      api_key: !secret octopus_key
      account_id: A-1234ABCD
```

These two forms are **mutually exclusive** and supplying both is rejected. The Octopus
consumption series already includes any EV charging, so accepting both would
double-count it. The trade-off is that a car baked into the meter data cannot be
smart-planned separately.

Instead of an Octopus URL you may give a fixed rate structure:

```yaml
  tariff:
    rates_import:
      - start: "00:30:00"
        end: "05:30:00"
        rate: 7.0
      - start: "05:30:00"
        end: "00:30:00"
        rate: 28.0
    rates_export:
      - rate: 15.0
```

## Running it

```bash
cd apps/predbat
python3 annual_cli.py --config annual.yaml --out results.json
```

A run takes roughly one to three minutes: 24 plan calculations plus the downloads,
which are cached between runs.

## Limitations

- The forecast archive only reaches back to about 2021. For earlier years the tool
  plans on actuals and falls back to a flat P10 derate, which it states in its output.
- `self_consumed_kwh` is approximate. When the battery exports grid-charged energy it
  is understated.
- The forecast-versus-ERA5 gap includes systematic model bias as well as genuine
  forecast error, so measured solar uncertainty is slightly overstated.
- A month with no rate data is reported as `unavailable` and excluded from the annual
  total, rather than counted as zero.
- Heat pump, iBoost and gas modelling are not included.
```

- [ ] **Step 2: Add the nav entry to `mkdocs.yml`**

Under the `Viewing Predbat data:` section, after `compare.md`:

```yaml
    - annual-prediction.md
```

- [ ] **Step 3: Add new words to the CSpell dictionary**

`docs/*.md` is spell-checked. Run pre-commit first to discover exactly which words it flags, then add only those:

```bash
./run_pre_commit 2>&1 | grep -i "unknown word" | sort -u
```

Likely candidates: `ERA`, `kwp`, `dno`. Append each flagged word to `.cspell/custom-dictionary-workspace.txt`, then run `./run_pre_commit` again — the file is auto-sorted, so re-stage it afterwards.

- [ ] **Step 4: Verify the docs build**

```bash
mkdocs build --strict > /tmp/t13.txt 2>&1; grep -iE "error|warning" /tmp/t13.txt
```

Expected: no output. If `mkdocs` is not installed, `pip install mkdocs` inside `coverage/venv` first.

- [ ] **Step 5: Run the full test suite**

```bash
cd coverage && ./run_all > /tmp/full.txt 2>&1; grep -E "ERROR|FAILED|Traceback" /tmp/full.txt; tail -5 /tmp/full.txt
```

Expected: no `ERROR`/`FAILED` lines. This is the last gate — the `solcast.py` refactor in Task 1 touches live forecasting code, so the whole suite must pass, not just the annual tests.

- [ ] **Step 6: Run pre-commit and commit**

```bash
coverage/run_pre_commit
git add docs/annual-prediction.md mkdocs.yml .cspell/custom-dictionary-workspace.txt
git commit -m "docs: add annual prediction tool documentation"
```

---

## Notes for the implementer

**The three assertions that must not be weakened.** If any of these fail, the bug is in
the implementation, not the test:

1. `predbat_cost <= without_predbat_cost <= no_pvbat_cost` — the property the tool exists
   to demonstrate.
2. Reported PV tracks actuals regardless of what the forecast claimed — proves the
   Prediction swap in scenario 3 is actually happening.
3. A month run in isolation matches that month within a longer run — proves
   `reset_sample_state()` is complete.

**Where to look when a scenario looks wrong.** `calculate_yesterday()` in
`apps/predbat/output.py` runs these same three scenarios against a real past day and is
the reference implementation. `Compare.run_scenario()` in `apps/predbat/compare.py` is
the reference for the battery-value correction.

**Do not add a P90 output.** The planner consumes only `pv_forecast_minute` and
`pv_forecast_minute10`; P90 reaches no decision. It was considered and deliberately
excluded.
