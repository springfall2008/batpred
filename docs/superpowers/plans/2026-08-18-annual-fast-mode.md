# WhatIf Fast Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `fast_mode` option to the WhatIf annual prediction that plans four seasonal months and reconstructs the other eight from the solar curve the run already downloads, making a run about 2.5× faster.

**Architecture:** A new pure module `annual_interpolate.py` fits each scenario field's per-day value against that month's actual PV yield (`per_day = a + b · pv_per_day`, least squares over the anchors) and evaluates it for the unplanned months. `AnnualPredictor.run()` plans only `ANCHOR_MONTHS` when the flag is set, then calls the module; rate downloads and availability checks still cover all twelve months. Interpolated months carry `status: "interpolated"` and count toward annual totals.

**Tech Stack:** Python 3, no new dependencies. Tests are hand-rolled functions registered in `TEST_REGISTRY` (this repo does **not** use pytest).

**Spec:** `docs/superpowers/specs/2026-08-18-annual-fast-mode-design.md`

## Global Constraints

- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every function and class, including tests and nested helpers.
- **Spelling:** British English (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit — re-stage after running pre-commit.
- **Naming:** `lower_case_with_underscores`.
- **Tests:** Every test function takes `my_predbat` (unused for pure tests) and returns a truthy `failed` value. Register in `TEST_REGISTRY` in `apps/predbat/unit_test.py` as `("name", func, "description", slow_bool)` and add the import.
- **Anchor months:** `ANCHOR_MONTHS = (3, 6, 9, 12)` — exactly these, in this order.
  (Corrected during implementation: this plan originally specified `(1, 4, 7, 10)`. The
  per-month cost error does not separate the candidates, but the savings figures do —
  see the design doc's Anchor months section for the measurements.)
- **Chosen basis:** `solar_affine`. The mean-rate regressor was tested and **rejected** (overfits: 362% held-out error). Do not add it.
- **Verification:** `cd coverage && ./run_pre_commit` must exit 0 before any task is considered done. Save output to a file and grep it; never pipe straight to grep.

---

### Task 1: `fast_mode` config flag and `--fast` CLI flag

**Files:**
- Modify: `apps/predbat/annual.py` (the returned dict in `validate_config`, beside `"debug"` at line 321)
- Modify: `apps/predbat/annual_cli.py` (argparse block at lines 155-160; config load at lines 163-168)
- Test: `apps/predbat/tests/test_annual_config.py`, `apps/predbat/tests/test_annual_cli.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `validate_config(config)["fast_mode"] -> bool`; `annual_cli.apply_fast_override(config: dict, fast: bool) -> dict`.

- [ ] **Step 1: Write the failing config test**

Append to the end of `test_annual_config()` in `apps/predbat/tests/test_annual_config.py`, immediately before its final `return failed`:

```python
    print("Test: fast_mode defaults to False")
    if validate_config(base_config())["fast_mode"] is not False:
        print("  ERROR: fast_mode should default to False, got {!r}".format(validate_config(base_config())["fast_mode"]))
        failed = True

    print("Test: fast_mode accepts a real True")
    config = base_config()
    config["annual"]["fast_mode"] = True
    if validate_config(config)["fast_mode"] is not True:
        print("  ERROR: fast_mode True should survive validation")
        failed = True

    print("Test: an explicit 'false' string does not become truthy")
    # Same trap as "debug"/"hybrid": bool("false") is True, so a YAML value quoted by
    # hand would silently enable fast mode for someone who explicitly turned it off.
    config = base_config()
    config["annual"]["fast_mode"] = "false"
    if validate_config(config)["fast_mode"] is not False:
        print("  ERROR: fast_mode 'false' must coerce to False, not a truthy string")
        failed = True
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd coverage && ./run_all --test annual_config > /tmp/t1.txt 2>&1; grep -E "ERROR|KeyError|FAILED|PASSED" /tmp/t1.txt`
Expected: FAIL — `KeyError: 'fast_mode'`.

- [ ] **Step 3: Add the config field**

In `apps/predbat/annual.py`, in the dict returned by `validate_config`, immediately after the `"debug"` line:

```python
        # Plans four seasonal months and interpolates the rest - see annual_interpolate.py.
        # _coerce_bool for the same reason as "debug": an explicit fast_mode: "false" in a
        # hand-written YAML must not read as truthy.
        "fast_mode": _coerce_bool(raw.get("fast_mode", False)),
```

- [ ] **Step 4: Run the config test and make sure it passes**

Run: `cd coverage && ./run_all --test annual_config > /tmp/t1.txt 2>&1; grep -E "ERROR|FAILED|PASSED" /tmp/t1.txt`
Expected: PASS.

- [ ] **Step 5: Write the failing CLI test**

In `apps/predbat/tests/test_annual_cli.py`, add this function after `test_annual_cli`:

```python
def test_annual_cli_fast_flag(my_predbat):
    """--fast sets annual.fast_mode on either config form, and is absent by default."""
    failed = False

    print("Test: --fast sets fast_mode on the wrapped config form")
    config = {"annual": {"location": {"postcode": "SW1A 1AA"}}}
    result = annual_cli.apply_fast_override(config, True)
    if result["annual"].get("fast_mode") is not True:
        print("  ERROR: --fast should set annual.fast_mode True, got {!r}".format(result["annual"].get("fast_mode")))
        failed = True

    print("Test: --fast sets fast_mode on the bare inner form")
    # validate_config accepts either shape (raw = config.get("annual", config)), so the
    # override has to reach the same mapping validate_config will read.
    config = {"location": {"postcode": "SW1A 1AA"}}
    result = annual_cli.apply_fast_override(config, True)
    if result.get("fast_mode") is not True:
        print("  ERROR: --fast should set fast_mode on an unwrapped config, got {!r}".format(result.get("fast_mode")))
        failed = True

    print("Test: without --fast nothing is added")
    config = {"annual": {"location": {"postcode": "SW1A 1AA"}}}
    result = annual_cli.apply_fast_override(config, False)
    if "fast_mode" in result["annual"]:
        print("  ERROR: fast_mode must not be injected when --fast was not given")
        failed = True

    return failed
```

- [ ] **Step 6: Run it to make sure it fails**

Run: `cd coverage && ./run_all --test annual_cli > /tmp/t1.txt 2>&1; grep -E "ERROR|AttributeError|FAILED|PASSED" /tmp/t1.txt`
Expected: FAIL — the test is not registered yet, and `apply_fast_override` does not exist.

- [ ] **Step 7: Implement the CLI flag**

In `apps/predbat/annual_cli.py`, add this function immediately above `def main(argv=None):`

```python
def apply_fast_override(config, fast):
    """Set ``annual.fast_mode`` when --fast was given, on whichever config shape was loaded.

    ``validate_config`` accepts both the wrapped ({"annual": {...}}) and the bare inner
    mapping, so the override has to land on the same mapping it will read - writing to the
    outer dict of a wrapped config would be silently ignored.
    """
    if not fast:
        return config
    inner = config.get("annual") if isinstance(config, dict) and isinstance(config.get("annual"), dict) else config
    inner["fast_mode"] = True
    return config
```

In the argparse block, after the `--machine` argument:

```python
    parser.add_argument("--fast", action="store_true", help="Plan only four seasonal months and interpolate the rest (about 2.5x faster, monthly figures approximate)")
```

Immediately after the `yaml.safe_load` try/except block that sets `config` (after the `return 2` for a bad config), add:

```python
    config = apply_fast_override(config, args.fast)
```

- [ ] **Step 8: Register the new test**

In `apps/predbat/unit_test.py`, extend the existing import at line 228:

```python
from tests.test_annual_cli import test_annual_cli, test_annual_cli_fast_flag, test_annual_cli_machine, test_annual_cli_machine_end_to_end
```

And add to `TEST_REGISTRY` beside the other annual CLI entries:

```python
        ("annual_cli_fast_flag", test_annual_cli_fast_flag, "Annual CLI --fast flag tests", False),
```

- [ ] **Step 9: Run both tests and make sure they pass**

Run: `cd coverage && ./run_all --test annual_config --test annual_cli --test annual_cli_fast_flag > /tmp/t1.txt 2>&1; grep -E "ERROR|FAILED|PASSED" /tmp/t1.txt`
Expected: three PASSED lines, no ERROR lines.

- [ ] **Step 10: Commit**

```bash
git add apps/predbat/annual.py apps/predbat/annual_cli.py apps/predbat/tests/test_annual_config.py apps/predbat/tests/test_annual_cli.py apps/predbat/unit_test.py
git commit -m "feat(whatif): add fast_mode config flag and --fast CLI flag"
```

---

### Task 2: The interpolation module

**Files:**
- Create: `apps/predbat/annual_interpolate.py`
- Modify: `apps/predbat/annual_weather.py` (add a public accessor to `WeatherYear`, after `daily_actual_kwh` at line 75)
- Create: `apps/predbat/tests/test_annual_interpolate.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `ANCHOR_MONTHS = (3, 6, 9, 12)`
  - `BASIS_SOLAR_AFFINE = "solar_affine"`, `BASIS_LINEAR = "linear"`, `DEFAULT_BASIS = BASIS_SOLAR_AFFINE`
  - `choose_basis(anchor_months: list, monthly_pv: dict | None, year: int) -> str`
  - `build_interpolated_rows(anchor_rows: dict, year: int, monthly_pv: dict | None, months=None, basis=None) -> dict` returning `{month: row}`. Each row has `month`, `status="interpolated"`, `days`, `scenarios`, `interpolated_from`. It does **not** set `standing_charge_p` or `export_credit_p_estimate` — `run()` adds both (Task 4).
  - `WeatherYear.monthly_actual_kwh(year) -> dict` mapping month number to total actual PV kWh.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_annual_interpolate.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for fast-mode month interpolation."""

import calendar

from annual_interpolate import ANCHOR_MONTHS, BASIS_LINEAR, BASIS_SOLAR_AFFINE, build_interpolated_rows, choose_basis

YEAR = 2025
FIELDS = ["cost_p", "import_kwh", "export_kwh", "pv_generated_kwh", "battery_throughput_kwh", "battery_cycles"]


def days_in(month):
    """Return the number of days in the given month of the test year."""
    return calendar.monthrange(YEAR, month)[1]


def linear_pv():
    """Return a monthly PV curve that rises to midsummer and falls back, in kWh."""
    return {month: 100.0 + 500.0 * (1 - abs(month - 7) / 6.0) for month in range(1, 13)}


def anchor_rows_from(value_for):
    """Build anchor month rows whose every scenario field is value_for(month)."""
    rows = {}
    for month in ANCHOR_MONTHS:
        scenarios = {key: {field: value_for(month) for field in FIELDS} for key in ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]}
        rows[month] = {"month": month, "status": "ok", "days": days_in(month), "scenarios": scenarios}
    return rows


def test_annual_interpolate(my_predbat):
    """Interpolation reproduces a known affine curve, clamps, and falls back safely."""
    failed = False
    pv = linear_pv()

    print("Test: an exact affine relationship is reproduced exactly")
    # per-day value = 2 + 0.5 * pv_per_day, so a correct least-squares fit through the four
    # anchors must recover every other month to floating point precision.
    def affine(month):
        """Monthly total whose per-day value is exactly 2 + 0.5 * pv per day."""
        return (2.0 + 0.5 * (pv[month] / days_in(month))) * days_in(month)

    rows = build_interpolated_rows(anchor_rows_from(affine), YEAR, pv)
    if sorted(rows) != [m for m in range(1, 13) if m not in ANCHOR_MONTHS]:
        print("  ERROR: expected the eight non-anchor months, got {}".format(sorted(rows)))
        failed = True
    for month, row in rows.items():
        got = row["scenarios"]["with_predbat"]["cost_p"]
        if abs(got - affine(month)) > 1e-6:
            print("  ERROR: month {} should reconstruct to {:.4f}, got {:.4f}".format(month, affine(month), got))
            failed = True

    print("Test: interpolated rows are marked and carry their provenance")
    row = rows[5]
    if row["status"] != "interpolated":
        print("  ERROR: status should be 'interpolated', got {!r}".format(row["status"]))
        failed = True
    if row["interpolated_from"] != {"anchors": list(ANCHOR_MONTHS), "basis": BASIS_SOLAR_AFFINE}:
        print("  ERROR: unexpected provenance {!r}".format(row["interpolated_from"]))
        failed = True
    if "sampled_days" in row:
        print("  ERROR: an interpolated month must not claim sampled days")
        failed = True
    if row["days"] != days_in(5):
        print("  ERROR: days should be the real month length, got {}".format(row["days"]))
        failed = True

    print("Test: a constant per-day value scales with month length, not flat totals")
    # Guards the per-day working space: February must come out smaller than March purely
    # because it is shorter, which a fit on raw monthly totals would smear away.
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 10.0 * days_in(month)), YEAR, pv)
    for month, row in rows.items():
        expected = 10.0 * days_in(month)
        if abs(row["scenarios"]["no_pvbat"]["cost_p"] - expected) > 1e-6:
            print("  ERROR: month {} constant per-day should total {:.2f}, got {:.2f}".format(month, expected, row["scenarios"]["no_pvbat"]["cost_p"]))
            failed = True

    print("Test: physical fields clamp at zero but cost may stay negative")
    # A steep slope through the anchors extrapolates below zero in the dark months; export
    # kWh cannot be negative, but an export-credit-dominated cost legitimately can be.
    def steep(month):
        """Monthly total that extrapolates well below zero in midwinter."""
        return (-40.0 + 0.9 * (pv[month] / days_in(month))) * days_in(month)

    rows = build_interpolated_rows(anchor_rows_from(steep), YEAR, pv)
    if any(row["scenarios"]["with_predbat"]["export_kwh"] < 0 for row in rows.values()):
        print("  ERROR: export_kwh must be clamped at zero")
        failed = True
    if not any(row["scenarios"]["with_predbat"]["cost_p"] < 0 for row in rows.values()):
        print("  ERROR: cost_p must be allowed to go negative")
        failed = True

    print("Test: no solar falls back to the linear basis")
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0), YEAR, None)
    if rows[5]["interpolated_from"]["basis"] != BASIS_LINEAR:
        print("  ERROR: a run with no solar should use the linear basis, got {!r}".format(rows[5]["interpolated_from"]["basis"]))
        failed = True

    print("Test: a degenerate (flat) PV curve falls back rather than dividing by zero")
    flat = {month: 300.0 for month in range(1, 13)}
    if choose_basis(list(ANCHOR_MONTHS), flat, YEAR) != BASIS_LINEAR:
        print("  ERROR: flat PV across the anchors should select the linear basis")
        failed = True
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0 * days_in(month)), YEAR, flat)
    if len(rows) != 8:
        print("  ERROR: the fallback must still produce all eight months, got {}".format(len(rows)))
        failed = True

    print("Test: the linear basis wraps December round to January")
    # December sits between the October and January anchors going forward round the circle;
    # treating the anchor list as a straight line would extrapolate off the end instead.
    values = {1: 10.0 * days_in(1), 4: 20.0 * days_in(4), 7: 30.0 * days_in(7), 10: 40.0 * days_in(10)}
    rows = build_interpolated_rows(anchor_rows_from(lambda month: values[month]), YEAR, None)
    december = rows[12]["scenarios"]["no_pvbat"]["cost_p"] / days_in(12)
    if not 10.0 < december < 40.0:
        print("  ERROR: December should interpolate between the Oct (40/day) and Jan (10/day) anchors, got {:.2f}/day".format(december))
        failed = True

    print("Test: only the requested months are produced")
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0), YEAR, pv, months=[2, 3])
    if sorted(rows) != [2, 3]:
        print("  ERROR: expected only months 2 and 3, got {}".format(sorted(rows)))
        failed = True

    print("Test: derived fields are not interpolated")
    # export_credit_p_estimate is recomputed by run() from the month's real export rate;
    # interpolating it here would be overwritten at best and inconsistent at worst.
    base = anchor_rows_from(lambda month: 100.0)
    for month in ANCHOR_MONTHS:
        for key in base[month]["scenarios"]:
            base[month]["scenarios"][key]["export_credit_p_estimate"] = 55.0
    rows = build_interpolated_rows(base, YEAR, pv)
    if "export_credit_p_estimate" in rows[5]["scenarios"]["with_predbat"]:
        print("  ERROR: export_credit_p_estimate must be left for run() to attach")
        failed = True

    return failed
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd coverage && ./run_all --test annual_interpolate > /tmp/t2.txt 2>&1; grep -E "ERROR|not found|PASSED" /tmp/t2.txt`
Expected: FAIL — `Test 'annual_interpolate' not found` (not registered) and the module does not exist.

- [ ] **Step 3: Write the module**

Create `apps/predbat/annual_interpolate.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Reconstruct the months a fast-mode run never planned, from the ones it did.

Pure functions only - no I/O, no Predbat import - so the curve can be unit tested against
known inputs and re-scored against stored reference runs without standing up an engine.

The curve is affine in solar: a month's per-day figure is modelled as ``a + b * pv_per_day``
and fitted by least squares over the anchor months, independently for every scenario and
every field. Fitting per field is the point - ``no_pvbat`` cost is load-driven and lands
near ``b = 0`` while ``export_kwh`` is steeply solar-driven, and one shared shape would
serve neither. Working in per-day space stops February's 28 days reading as a seasonal dip.

Measured against five twelve-month reference runs, this contributes under 1% error to the
annual savings figure; see docs/superpowers/specs/2026-08-18-annual-fast-mode-design.md.
"""

import calendar

# One per season, spanning midwinter to midsummer so the fit can resolve the solar slope.
ANCHOR_MONTHS = (3, 6, 9, 12)

BASIS_SOLAR_AFFINE = "solar_affine"
BASIS_LINEAR = "linear"
DEFAULT_BASIS = BASIS_SOLAR_AFFINE

# Cost is the one field that may legitimately be negative (export credit exceeding import
# spend). Everything else is a physical quantity that an extrapolated fit could otherwise
# drive below zero - negative December export being the obvious way to get it wrong.
SIGNED_FIELDS = ("cost_p",)

# Recomputed by run() from the month's real average export rate, so interpolating it here
# would be overwritten at best and silently inconsistent at worst.
DERIVED_FIELDS = ("export_credit_p_estimate",)

# Below this variance in per-day anchor PV there is no solar signal to fit against, and the
# least-squares denominator is not safely invertible.
_MIN_PV_VARIANCE = 1e-9


def _fit_affine(xs, ys):
    """Return least-squares ``(intercept, slope)`` for ``y = a + b * x``, or None if degenerate."""
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator < _MIN_PV_VARIANCE:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    return mean_y - slope * mean_x, slope


def _cyclic_linear(anchors, values, month):
    """Interpolate linearly around the month circle, wrapping December to January.

    The year is a cycle, so December sits between the October and January anchors going
    forward rather than off the end of a straight list.
    """
    count = len(anchors)
    for index in range(count):
        start = anchors[index]
        end = anchors[(index + 1) % count]
        span = (end - start) % 12 or 12
        offset = (month - start) % 12
        if offset <= span:
            fraction = offset / float(span)
            return values[index] * (1 - fraction) + values[(index + 1) % count] * fraction
    return values[0]


def choose_basis(anchor_months, monthly_pv, year):
    """Return the basis to use for this run: solar-affine when there is a solar signal to fit.

    Chosen once for the whole run rather than per field, so every interpolated row reports
    one honest basis in its provenance block.
    """
    if not monthly_pv:
        return BASIS_LINEAR
    per_day = [monthly_pv.get(month, 0.0) / calendar.monthrange(year, month)[1] for month in anchor_months]
    mean_pv = sum(per_day) / len(per_day)
    if sum((value - mean_pv) ** 2 for value in per_day) < _MIN_PV_VARIANCE:
        return BASIS_LINEAR
    return BASIS_SOLAR_AFFINE


def build_interpolated_rows(anchor_rows, year, monthly_pv, months=None, basis=None):
    """Return ``{month: row}`` for every wanted month absent from ``anchor_rows``.

    ``anchor_rows`` maps month number to a planned month row (as ``run()`` builds it, with a
    ``scenarios`` dict). ``monthly_pv`` maps month number to that month's total actual PV
    kWh, or is None for a battery-only run. ``months`` defaults to every month of the year
    that is not an anchor; pass a list to skip months already known to be unavailable.

    The returned rows carry no ``standing_charge_p`` and no ``export_credit_p_estimate``:
    both need tariff data, so ``run()`` attaches them.
    """
    anchors = sorted(anchor_rows)
    if len(anchors) < 2:
        # One point cannot define a line. The caller is expected to have abandoned fast mode
        # before reaching here; returning nothing keeps that contract enforceable rather
        # than inventing a flat year from a single month.
        return {}

    if basis is None:
        basis = choose_basis(anchors, monthly_pv, year)
    if months is None:
        months = [month for month in range(1, 13) if month not in anchors]

    days = {month: calendar.monthrange(year, month)[1] for month in range(1, 13)}
    pv_per_day = {month: (monthly_pv.get(month, 0.0) / days[month] if monthly_pv else 0.0) for month in range(1, 13)}
    anchor_pv = [pv_per_day[month] for month in anchors]

    scenario_keys = list(anchor_rows[anchors[0]]["scenarios"].keys())
    provenance = {"anchors": anchors, "basis": basis}

    rows = {}
    for month in months:
        rows[month] = {
            "month": month,
            "status": "interpolated",
            "days": days[month],
            "scenarios": {key: {} for key in scenario_keys},
            "interpolated_from": dict(provenance),
        }

    for key in scenario_keys:
        fields = [field for field in anchor_rows[anchors[0]]["scenarios"][key] if field not in DERIVED_FIELDS]
        for field in fields:
            per_day = [anchor_rows[month]["scenarios"][key][field] / days[month] for month in anchors]
            fit = _fit_affine(anchor_pv, per_day) if basis == BASIS_SOLAR_AFFINE else None
            for month in months:
                if fit is not None:
                    value = (fit[0] + fit[1] * pv_per_day[month]) * days[month]
                else:
                    value = _cyclic_linear(anchors, per_day, month) * days[month]
                if field not in SIGNED_FIELDS:
                    value = max(0.0, value)
                rows[month]["scenarios"][key][field] = round(value, 3)

    return rows
```

- [ ] **Step 4: Add the weather accessor**

In `apps/predbat/annual_weather.py`, add to `WeatherYear` immediately after `daily_actual_kwh` (line 75):

```python
    def monthly_actual_kwh(self, year):
        """Return {month: total actual PV kWh} for the given year.

        The whole twelve month solar curve, already in hand from the archive fetch - this is
        what lets fast mode reconstruct months it never planned without another download.
        """
        totals = {month: 0.0 for month in range(1, 13)}
        for day, kwh in self._daily_actual.items():
            if day.year == year:
                totals[day.month] += kwh
        return totals
```

- [ ] **Step 5: Register the test**

In `apps/predbat/unit_test.py`, add the import beside the other annual test imports:

```python
from tests.test_annual_interpolate import test_annual_interpolate
```

And add to `TEST_REGISTRY`:

```python
        ("annual_interpolate", test_annual_interpolate, "Annual fast-mode interpolation curve tests", False),
```

- [ ] **Step 6: Run the tests and make sure they pass**

Run: `cd coverage && ./run_all --test annual_interpolate --test annual_weather > /tmp/t2.txt 2>&1; grep -E "ERROR|FAILED|PASSED" /tmp/t2.txt`
Expected: two PASSED lines, no ERROR lines.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/annual_interpolate.py apps/predbat/annual_weather.py apps/predbat/tests/test_annual_interpolate.py apps/predbat/unit_test.py
git commit -m "feat(whatif): add solar-affine month interpolation module"
```

---

### Task 3: Shared included-status constant

An interpolated month is currently invisible to four separate `("ok", "degraded")` checks, each of which would render it as **unavailable**. This task makes them agree via one constant, before Task 4 starts producing such rows.

**Files:**
- Modify: `apps/predbat/annual.py` (add constant near `SCENARIO_FIELDS` at line 1259; use it at lines 1621-1622)
- Modify: `apps/predbat/annual_cli.py` (import at line 26; check at line 68)
- Modify: `apps/predbat/web_annual.py` (import at line 28; checks at lines 1428 and 1477)
- Test: `apps/predbat/tests/test_annual_results.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `annual.INCLUDED_STATUSES = ("ok", "degraded", "interpolated")`.

- [ ] **Step 1: Write the failing test**

Append to `test_annual_results()` in `apps/predbat/tests/test_annual_results.py`, before its final `return failed`. It reuses that file's existing `make_month_row(month, costs, status="ok")` and `make_predictor()` helpers. Extend the file's existing `from annual import ...` line to also import `INCLUDED_STATUSES`.

```python
    print("Test: INCLUDED_STATUSES covers planned, degraded and interpolated months")
    if sorted(INCLUDED_STATUSES) != ["degraded", "interpolated", "ok"]:
        print("  ERROR: INCLUDED_STATUSES should be ok/degraded/interpolated, got {!r}".format(INCLUDED_STATUSES))
        failed = True

    print("Test: an interpolated month counts toward the annual totals")
    # The whole point of fast mode: these months were never planned, but excluding them
    # would report a four month year rather than a twelve month one.
    predictor = make_predictor()
    months = [
        make_month_row(1, {"no_pvbat": 100.0, "pv_only": 75.0, "without_predbat": 50.0, "with_predbat": 20.0}),
        make_month_row(2, {"no_pvbat": 200.0, "pv_only": 140.0, "without_predbat": 80.0, "with_predbat": 30.0}, status="interpolated"),
    ]
    result = predictor._build_results(months)
    if result["annual"]["months_included"] != 2:
        print("  ERROR: an interpolated month should be included, got months_included {}".format(result["annual"]["months_included"]))
        failed = True
    if result["annual"]["months_excluded"] != []:
        print("  ERROR: an interpolated month must not be excluded, got {}".format(result["annual"]["months_excluded"]))
        failed = True
    if result["annual"]["scenarios"]["no_pvbat"]["cost_p"] != 300.0:
        print("  ERROR: the interpolated month's cost should be in the total, expected 300.0, got {}".format(result["annual"]["scenarios"]["no_pvbat"]["cost_p"]))
        failed = True
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd coverage && ./run_all --test annual_results > /tmp/t3.txt 2>&1; grep -E "ERROR|ImportError|cannot import|PASSED" /tmp/t3.txt`
Expected: FAIL — `cannot import name 'INCLUDED_STATUSES'`.

- [ ] **Step 3: Add the constant and use it everywhere**

In `apps/predbat/annual.py`, immediately after the `SCENARIO_FIELDS` definition (line 1259):

```python
# Month statuses that carry real figures and count toward the annual totals. "interpolated"
# is one of them: a fast-mode month was never planned, but it is a modelled estimate of a
# real month, and dropping it would report a four month year. Shared with annual_cli and
# web_annual so the CLI table, the chart and the month table cannot drift from the totals.
INCLUDED_STATUSES = ("ok", "degraded", "interpolated")
```

In `apps/predbat/annual.py` `_build_results` (lines 1621-1622):

```python
        included = [entry for entry in months if entry["status"] in INCLUDED_STATUSES]
        excluded = [entry["month"] for entry in months if entry["status"] not in INCLUDED_STATUSES]
```

In `apps/predbat/annual_cli.py`, extend the import at line 26 and change line 68:

```python
from annual import INCLUDED_STATUSES, SCENARIO_KEYS, AnnualConfigError, AnnualPredictor  # noqa: E402
```

```python
        if entry["status"] not in INCLUDED_STATUSES:
```

In `apps/predbat/web_annual.py`, extend the import at line 28 and change both lines 1428 and 1477:

```python
from annual import INCLUDED_STATUSES, AnnualConfigError, validate_config
```

```python
            if entry.get("status") not in INCLUDED_STATUSES:
```

- [ ] **Step 4: Run the affected suites and make sure they pass**

Run: `cd coverage && ./run_all -k annual > /tmp/t3.txt 2>&1; grep -E "ERROR|FAILED" /tmp/t3.txt; grep -c PASSED /tmp/t3.txt`
Expected: no ERROR or FAILED lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/annual.py apps/predbat/annual_cli.py apps/predbat/web_annual.py apps/predbat/tests/test_annual_results.py
git commit -m "refactor(whatif): share one included-status constant across annual consumers"
```

---

### Task 4: Wire fast mode into the engine

**Files:**
- Modify: `apps/predbat/annual.py` — `AnnualPredictor.run()` (lines 1450-1609) and `_build_results` (lines 1611-1664)
- Test: `apps/predbat/tests/test_annual_interpolate.py` (add an engine-level test)

**Interfaces:**
- Consumes: `annual_interpolate.ANCHOR_MONTHS`, `build_interpolated_rows`; `WeatherYear.monthly_actual_kwh`; `INCLUDED_STATUSES`.
- Produces: results document gains `annual["fast_mode"]: bool` and `annual["months_interpolated"]: int`; interpolated rows gain `standing_charge_p` and `scenarios[*]["export_credit_p_estimate"]`.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_annual_interpolate.py`:

```python
def test_annual_fast_mode_assembly(my_predbat):
    """The pieces run() assembles: month selection, provenance, totals and the anchor fallback."""
    failed = False
    pv = linear_pv()

    print("Test: fast mode plans only the anchor months")
    from annual_interpolate import ANCHOR_MONTHS as anchors

    if sorted(anchors) != [1, 4, 7, 10]:
        print("  ERROR: anchors should be Jan/Apr/Jul/Oct, got {}".format(sorted(anchors)))
        failed = True

    print("Test: an unavailable month is not interpolated over")
    # A month with no rate data must stay unavailable, not be quietly fabricated - which is
    # why run() passes an explicit month list rather than letting the module fill everything.
    wanted = [month for month in range(1, 13) if month not in anchors and month != 3]
    rows = build_interpolated_rows(anchor_rows_from(lambda month: 100.0 * days_in(month)), YEAR, pv, months=wanted)
    if 3 in rows:
        print("  ERROR: month 3 was excluded but got interpolated anyway")
        failed = True
    if len(rows) != 7:
        print("  ERROR: expected 7 interpolated months, got {}".format(len(rows)))
        failed = True

    print("Test: fewer than two surviving anchors produces nothing")
    single = {1: anchor_rows_from(lambda month: 100.0)[1]}
    if build_interpolated_rows(single, YEAR, pv) != {}:
        print("  ERROR: a single anchor must not be fitted - run() falls back to a full run instead")
        failed = True

    print("Test: two surviving anchors still work")
    two = {month: anchor_rows_from(lambda m: 100.0 * days_in(m))[month] for month in (1, 7)}
    rows = build_interpolated_rows(two, YEAR, pv)
    if len(rows) != 10:
        print("  ERROR: two anchors should fill the other ten months, got {}".format(len(rows)))
        failed = True
    if rows[5]["interpolated_from"]["anchors"] != [1, 7]:
        print("  ERROR: provenance should record the surviving anchors, got {!r}".format(rows[5]["interpolated_from"]["anchors"]))
        failed = True

    return failed
```

Register it: add `test_annual_fast_mode_assembly` to the `from tests.test_annual_interpolate import ...` line in `unit_test.py`, and add to `TEST_REGISTRY`:

```python
        ("annual_fast_mode_assembly", test_annual_fast_mode_assembly, "Annual fast-mode assembly tests", False),
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd coverage && ./run_all --test annual_fast_mode_assembly > /tmp/t4.txt 2>&1; grep -E "ERROR|not found|PASSED" /tmp/t4.txt`
Expected: FAIL — test not registered yet.

- [ ] **Step 3: Restructure the month loop**

In `apps/predbat/annual.py`, add to the imports at the top of the file:

```python
from annual_interpolate import ANCHOR_MONTHS, build_interpolated_rows
```

In `run()`, replace the line `for month in range(1, 13):` (line 1504) and the `total_units`/`completed` setup above it (lines 1501-1502) with:

```python
        fast_mode = self.config["fast_mode"]
        # Rate downloads and availability checks still cover all twelve months even in fast
        # mode - they are network-bound and cheap next to planning, and skipping them would
        # let interpolation paper over a month that genuinely had no rates.
        months_to_plan = list(ANCHOR_MONTHS) if fast_mode else list(range(1, 13))
        total_units = len(months_to_plan) + (1 if fast_mode else 0)
        completed = 0

        for month in range(1, 13):
```

Inside the loop, immediately after `standing_charge_p` is computed (line 1509), insert the skip for months this run will not plan. Place it **after** both `fetch_month` calls and their unavailable checks, so an unavailable month is still detected — that is, immediately after the spill-fetch block that ends at line 1526:

```python
            if month not in months_to_plan:
                # Planned nothing here; the row is built after the loop by interpolation.
                # Recorded so the interpolation step knows this month had usable rates.
                interpolatable.append((month, days_in_month, standing_charge_p))
                continue
```

Initialise `interpolatable = []` beside `months = []` (line 1500).

Change the progress call and the `completed` bookkeeping so `completed` only advances for planned months — otherwise the unavailable branches, which run for all twelve months, push `completed` past `total_units` and the progress bar overshoots. Replace the `if progress:` block at lines 1505-1506 with:

```python
            if progress and month in months_to_plan:
                progress(completed, total_units, "Month {:02d}/{}".format(month, year))
```

Then replace **every** bare `completed += 1` inside the loop (there are four: the two unavailable-rate branches, the no-usable-weather branch, the all-days-failed branch, and the success path) with:

```python
            if month in months_to_plan:
                completed += 1
```

An unavailable month is still appended to `months` as it is today, and because the skip added above sits *after* the availability checks, it is never added to `interpolatable` — so a month with no rate data stays unavailable rather than being quietly interpolated over.

- [ ] **Step 4: Add the interpolation step after the loop**

In `run()`, immediately after the month loop and **before** `self.caveats.extend(self._tariff_fallback_caveats(...))` (line 1597), insert:

```python
        interpolated_count = 0
        if fast_mode:
            anchor_rows = {entry["month"]: entry for entry in months if entry["status"] in INCLUDED_STATUSES}
            if len(anchor_rows) < 2 and interpolatable:
                # One anchor cannot define a line. Rather than fail or guess, plan the rest
                # normally and finish as an ordinary full run - the user gets a slower but
                # correct answer instead of a fast wrong one.
                self.log("Warn: Annual: only {} anchor month(s) produced a result, so fast mode was abandoned and the remaining months planned in full".format(len(anchor_rows)))
                self.caveats.append("Fast mode was abandoned because too few of the four sampled months produced a usable result, so all twelve months were planned instead. This run is a full run.")
                for month, days_in_month, standing_charge_p in interpolatable:
                    baseline_ready = await self.baseline_tariff.fetch_month(year, month)
                    if not baseline_ready:
                        baseline_fallback_months.append(month)
                    months.append(await self._plan_one_month(month, year, zone, days_in_month, standing_charge_p, baseline_ready))
                    completed += 1
                months.sort(key=lambda entry: entry["month"])
                fast_mode = False
            elif anchor_rows:
                monthly_pv = self.weather.monthly_actual_kwh(year) if self.weather else None
                if progress:
                    progress(completed, total_units, "Interpolating {} month(s)".format(len(interpolatable)))
                wanted = [month for month, _, _ in interpolatable]
                rows = build_interpolated_rows(anchor_rows, year, monthly_pv, months=wanted)
                for month, days_in_month, standing_charge_p in interpolatable:
                    row = rows.get(month)
                    if not row:
                        continue
                    row["standing_charge_p"] = round(standing_charge_p, 3)
                    # Recomputed from this month's own export rate rather than interpolated,
                    # so the field means exactly what it means in a planned month. The 15th
                    # stands in for the month the way a planned month uses its first sample.
                    midnight_utc = zone.localize(datetime(year, month, 15)).astimezone(pytz.utc)
                    _, rate_export = self.tariff.rates_for(midnight_utc, DAY_MINUTES)
                    export_rate = average_rate(rate_export, DAY_MINUTES)
                    for key in row["scenarios"]:
                        row["scenarios"][key]["export_credit_p_estimate"] = round(row["scenarios"][key].get("export_kwh", 0.0) * export_rate, 3)
                    months.append(row)
                    interpolated_count += 1
                months.sort(key=lambda entry: entry["month"])
                self.caveats.append(
                    "Fast mode: only {} were planned; the other {} month(s) were interpolated from them against this year's solar curve. Interpolation adds under 1% error to the annual savings figure, and individual months land within roughly 10-13% typically. On a tariff whose daily prices swing widely (Agile especially) the sampled months carry roughly 8-18% error of their own, which a full run has too - read individual months as indicative, not exact.".format(
                        ", ".join(calendar.month_abbr[month] for month in sorted(anchor_rows)), len(interpolatable)
                    )
                )
```

- [ ] **Step 5: Extract the per-month planning body**

The fallback in Step 4 calls `self._plan_one_month(...)`, which does not exist yet. Extract it from the loop body so the fallback can plan a skipped month without duplicating ~65 lines.

Move the code from `samples = select_samples(...)` (line 1528) down to and including `months.append(row)` (line 1594) into this new method, placed immediately above `run()`:

```python
    async def _plan_one_month(self, month, year, zone, days_in_month, standing_charge_p, baseline_ready):
        """Plan and cost one month, returning its results row.

        Extracted from run()'s loop so the fast-mode fallback can plan the months it
        originally skipped without duplicating the body. Always returns a row - an
        "unavailable" one when the month produced nothing usable - so the caller appends
        unconditionally rather than reproducing the loop's branching.
        """
```

Three mechanical substitutions inside the moved body, and nothing else changes:

1. Each `months.append({...}); completed += 1; continue` for a failure case becomes `return {...}` with the same dict.
2. The final `months.append(row)` / `completed += 1` becomes `return row`.
3. `self.config` and `self.tariff` references are already on `self` and need no change.

At the original site in the loop, the body becomes:

```python
            row = await self._plan_one_month(month, year, zone, days_in_month, standing_charge_p, baseline_ready)
            months.append(row)
            if month in months_to_plan:
                completed += 1
```

Note `baseline_ready` is a parameter rather than recomputed inside: the loop already awaited `self.baseline_tariff.fetch_month(year, month)` for every month, and re-awaiting it would double the downloads.

- [ ] **Step 6: Record fast mode in the results document**

In `_build_results`, change the signature to accept the two new values and add them to the returned `annual` block:

```python
    def _build_results(self, months, fast_mode=False, months_interpolated=0):
```

```python
            "annual": {
                "scenarios": annual_scenarios,
                "standing_charge_p": standing_total,
                "savings": savings,
                "months_included": len(included),
                "months_excluded": excluded,
                # Recorded so a stored run can never be mistaken for a full one after the
                # fact - the compare table and the results page both key off this.
                "fast_mode": fast_mode,
                "months_interpolated": months_interpolated,
                "costs": costs,
                "payback": payback,
            },
```

And change the final call in `run()`:

```python
        return self._build_results(months, fast_mode=fast_mode, months_interpolated=interpolated_count)
```

- [ ] **Step 7: Run the tests and make sure they pass**

Run: `cd coverage && ./run_all -k annual > /tmp/t4.txt 2>&1; grep -E "ERROR|FAILED" /tmp/t4.txt; grep -c PASSED /tmp/t4.txt`
Expected: no ERROR or FAILED lines.

- [ ] **Step 8: Commit**

```bash
git add apps/predbat/annual.py apps/predbat/tests/test_annual_interpolate.py apps/predbat/unit_test.py
git commit -m "feat(whatif): plan four anchor months and interpolate the rest in fast mode"
```

---

### Task 5: Web UI

**Files:**
- Modify: `apps/predbat/web_annual.py` — Advanced fieldset (line 599), `config_from_post` (line 772), "what this run used" (line 1243), month table (line 1482), chart note (line 1454)
- Modify: `apps/predbat/annual_store.py` — `build_summary` (line 71)
- Test: `apps/predbat/tests/test_web_annual.py`

**Interfaces:**
- Consumes: `annual["fast_mode"]`, `annual["months_interpolated"]`, row `status == "interpolated"`, row `interpolated_from`.
- Produces: summary key `"fast_mode"`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_web_annual.py`. It reuses that file's existing `make_page(my_predbat)` helper, its `sample_run_results()` fixture, its `valid_postdata()` helper and the `DEFAULT_CONFIG` already imported at line 25. Before writing, read `make_page` (line 65) and `sample_run_results` (line 1421) and confirm the form-rendering method's exact name — use whatever `test_web_annual_form` calls, not the name guessed below.

```python
def test_web_annual_fast_mode(my_predbat):
    """The fast mode checkbox renders, round-trips, and interpolated months are marked."""
    failed = False
    page = make_page(my_predbat)

    print("Test: the Advanced block offers a fast mode checkbox")
    form = page.html_annual_form(dict(DEFAULT_CONFIG))
    if 'name="fast_mode"' not in form:
        print("  ERROR: the form should contain a fast_mode checkbox")
        failed = True

    print("Test: a ticked box round-trips into the config")
    postdata = valid_postdata()
    postdata["fast_mode"] = "on"
    config = page.config_from_post(postdata)
    if config.get("fast_mode") is not True:
        print("  ERROR: a ticked fast_mode box should set fast_mode True, got {!r}".format(config.get("fast_mode")))
        failed = True

    print("Test: an absent box means off")
    # A checkbox absent from postdata means unchecked - there is no "off" value to read.
    config = page.config_from_post(valid_postdata())
    if config.get("fast_mode") is not False:
        print("  ERROR: an absent fast_mode box should set fast_mode False, got {!r}".format(config.get("fast_mode")))
        failed = True

    print("Test: an interpolated month renders as interpolated, not unavailable")
    results = sample_run_results()
    results["months"][4] = {
        "month": 5,
        "status": "interpolated",
        "days": 31,
        "standing_charge_p": 1860.0,
        "scenarios": results["months"][4]["scenarios"],
        "interpolated_from": {"anchors": [1, 4, 7, 10], "basis": "solar_affine"},
    }
    table = page._render_month_table(results)
    if "unavailable" in table.split("May")[1][:200]:
        print("  ERROR: an interpolated month must not render as unavailable")
        failed = True
    if "interpolated" not in table.lower():
        print("  ERROR: an interpolated month should be labelled as such")
        failed = True

    return failed
```

Register it in `unit_test.py`: add `test_web_annual_fast_mode` to the `from tests.test_web_annual import (...)` block and add:

```python
        ("web_annual_fast_mode", test_web_annual_fast_mode, "Annual web tab fast mode tests", False),
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd coverage && ./run_all --test web_annual_fast_mode > /tmp/t5.txt 2>&1; grep -E "ERROR|not found|PASSED" /tmp/t5.txt`
Expected: FAIL.

- [ ] **Step 3: Add the form control**

In `apps/predbat/web_annual.py`, in the Advanced `<details>` block immediately after the `samples_per_month` field (line 599):

```python
        text += '<div class="annual-field"><label for="fast_mode">Fast mode</label><input type="checkbox" id="fast_mode" name="fast_mode" {}></div>\n'.format("checked" if config.get("fast_mode") else "")
        text += '<p class="annual-note">Plans January, April, July and October and estimates the other eight months from this year\'s solar curve. About 2.5&times; faster. Annual totals and payback stay close; individual months are approximate, so turn this off if you want to read a specific month\'s figure.</p>\n'
```

Add `"fast_mode": False` to `DEFAULT_CONFIG` (line 60) beside `"samples_per_month": 2`.

- [ ] **Step 4: Parse the checkbox**

In `config_from_post`, beside the existing `debug` line (line 777):

```python
        config["fast_mode"] = postdata.get("fast_mode") is not None
```

- [ ] **Step 5: Show it in "what this run used"**

After the `samples_per_month` row (line 1244):

```python
        if config.get("fast_mode"):
            rows.append(("Fast mode", "on — 4 months planned, 8 interpolated"))
```

- [ ] **Step 6: Mark interpolated months in the table and chart**

In `_render_month_table`, beside the `synthesised` handling (after line 1482):

```python
            if entry.get("status") == "interpolated":
                # Included in the totals and rendered with real figures, so without this it
                # is indistinguishable from a month that was actually planned.
                basis = (entry.get("interpolated_from") or {}).get("anchors") or []
                suffix += " <span class='annual-synthesised-tag' title='This month was not planned; its figures were estimated from the planned months against this year&#39;s solar curve'>interpolated from {}</span>".format(html.escape(", ".join(calendar.month_abbr[month] for month in basis), quote=True))
```

In the chart method, beside the `synthesised_months` note (after line 1458):

```python
        interpolated_months = [calendar.month_abbr[entry["month"]] for entry in results.get("months", []) if entry.get("status") == "interpolated"]
        if interpolated_months:
            text += "<p class='annual-note'>{} were not planned — their bars are estimated from the planned months against this year's solar curve. The annual total stays close; individual months are approximate.</p>\n".format(", ".join(interpolated_months))
```

- [ ] **Step 7: Record it in the run summary**

In `apps/predbat/annual_store.py` `build_summary`, add to the returned dict:

```python
        # So the run selector and compare table can distinguish a fast run from a full one.
        # Comparing the two is legitimate - that is the accuracy claim - but it must be visible.
        "fast_mode": bool(((results or {}).get("annual") or {}).get("fast_mode")),
```

- [ ] **Step 8: Run the tests and make sure they pass**

Run: `cd coverage && ./run_all -k annual > /tmp/t5.txt 2>&1; grep -E "ERROR|FAILED" /tmp/t5.txt; grep -c PASSED /tmp/t5.txt`
Expected: no ERROR or FAILED lines.

- [ ] **Step 9: Commit**

```bash
git add apps/predbat/web_annual.py apps/predbat/annual_store.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
git commit -m "feat(whatif): expose fast mode in the web UI and mark interpolated months"
```

---

### Task 6: Committed curve-scoring harness

Locks in the curve choice so a future change cannot silently regress it.

**Files:**
- Create: `apps/predbat/tests/test_annual_curve_reference.py`
- Create: `coverage/cases/annual_reference_agile.json`, `coverage/cases/annual_reference_cosy.json`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual_interpolate.build_interpolated_rows`, `BASIS_SOLAR_AFFINE`, `BASIS_LINEAR`, `ANCHOR_MONTHS`.
- Produces: nothing other tasks use.

- [ ] **Step 1: Confirm the fixtures are present**

**The two fixtures are already committed** — `coverage/cases/annual_reference_agile.json` (from the six-samples-per-month Agile reference) and `coverage/cases/annual_reference_cosy.json`. Each holds twelve months of scenarios plus that month's PV yield, and nothing else: no plans, no location, no tariff URLs, no keys. Nothing to build.

Run: `ls -la coverage/cases/annual_reference_*.json`
Expected: two files, roughly 13 KB each.

Only if you later produce a **new** reference run is regeneration needed, in which case reduce it the same way — keep `year`, `months` (each with `month`, `days`, `scenarios`) and `monthly_pv` keyed by month-number-as-string, and drop everything else.

- [ ] **Step 2: Write the test**

Create `apps/predbat/tests/test_annual_curve_reference.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Guards the fast-mode curve choice against stored reference runs.

Reconstructs eight months from four anchors and checks the shipped basis still beats the
alternatives. Skips when no fixture is present, so a checkout without them stays green.
"""

import json
import os

from annual_interpolate import ANCHOR_MONTHS, BASIS_LINEAR, BASIS_SOLAR_AFFINE, build_interpolated_rows

FIXTURES = ["annual_reference_agile.json", "annual_reference_cosy.json"]


def fixture_paths():
    """Return the reference fixtures that exist, newest search path first."""
    found = []
    for name in FIXTURES:
        for base in ("cases", os.path.join("coverage", "cases")):
            path = os.path.join(base, name)
            if os.path.exists(path):
                found.append(path)
                break
    return found


def per_month_error(doc, basis):
    """Mean per-month cost error, as a percentage of that scenario's mean monthly cost."""
    year = doc["year"]
    months = {entry["month"]: entry for entry in doc["months"]}
    monthly_pv = {int(month): value for month, value in doc["monthly_pv"].items()}
    anchors = {month: months[month] for month in ANCHOR_MONTHS if month in months}
    rebuilt = build_interpolated_rows(anchors, year, monthly_pv, basis=basis)

    errors = []
    for key in months[ANCHOR_MONTHS[0]]["scenarios"]:
        truth = {month: entry["scenarios"][key]["cost_p"] for month, entry in months.items()}
        scale = sum(abs(value) for value in truth.values()) / len(truth)
        if scale < 1e-6:
            continue
        for month, row in rebuilt.items():
            if month in truth:
                errors.append(abs(row["scenarios"][key]["cost_p"] - truth[month]) / scale * 100)
    return sum(errors) / len(errors) if errors else None


def test_annual_curve_reference(my_predbat):
    """The shipped solar-affine basis still beats plain linear across the reference runs.

    Measured when the basis was chosen: Agile 18.39% vs linear 18.84%, Cosy 9.56% vs 14.47%.
    Agile's margin is genuinely narrow, so a per-fixture strict comparison would flake on
    any harmless rounding change. The guard that matters is therefore the combined mean,
    with a loose per-fixture ceiling to catch a basis that has broken outright.
    """
    failed = False
    paths = fixture_paths()
    if not paths:
        print("  SKIP: no annual reference fixtures found")
        return failed

    affine_all = []
    linear_all = []
    for path in paths:
        doc = json.load(open(path))
        affine = per_month_error(doc, BASIS_SOLAR_AFFINE)
        linear = per_month_error(doc, BASIS_LINEAR)
        if affine is None or linear is None:
            print("  ERROR: {} produced no comparable months".format(path))
            failed = True
            continue
        print("  {}: solar_affine {:.2f}%, linear {:.2f}%".format(os.path.basename(path), affine, linear))
        affine_all.append(affine)
        linear_all.append(linear)
        # 1.10 is slack for rounding and anchor reshuffles, not for a real regression: at
        # selection time the worst fixture had solar_affine at 0.98x linear.
        if affine > linear * 1.10:
            print("  ERROR: {} - solar_affine ({:.2f}%) is far worse than linear ({:.2f}%); the basis has regressed".format(path, affine, linear))
            failed = True

    if affine_all:
        affine_mean = sum(affine_all) / len(affine_all)
        linear_mean = sum(linear_all) / len(linear_all)
        print("  combined: solar_affine {:.2f}%, linear {:.2f}%".format(affine_mean, linear_mean))
        if affine_mean >= linear_mean:
            print("  ERROR: solar_affine ({:.2f}%) must beat linear ({:.2f}%) overall, or it is not the right default".format(affine_mean, linear_mean))
            failed = True

    return failed
```

- [ ] **Step 3: Register and run it**

Add the import and registry entry in `unit_test.py`:

```python
from tests.test_annual_curve_reference import test_annual_curve_reference
```

```python
        ("annual_curve_reference", test_annual_curve_reference, "Annual fast-mode curve reference scoring", False),
```

Run: `cd coverage && ./run_all --test annual_curve_reference > /tmp/t6.txt 2>&1; grep -E "ERROR|SKIP|solar_affine|PASSED" /tmp/t6.txt`
Expected: PASS, printing roughly `agile: solar_affine 18.39%, linear 18.84%` and `cosy: solar_affine 9.56%, linear 14.47%`, then a combined line where solar_affine is lower. Figures within a few tenths of these are fine; a large divergence means `build_interpolated_rows` is not doing what the selection study measured.

- [ ] **Step 4: Commit**

```bash
git add apps/predbat/tests/test_annual_curve_reference.py apps/predbat/unit_test.py
git commit -m "test(whatif): guard the fast-mode curve choice against reference runs"
```

(The fixtures themselves were committed with this plan; they are not part of this commit.)

---

### Task 7: Documentation

**Files:**
- Modify: `docs/annual-prediction.md`

**Interfaces:**
- Consumes: everything above. Produces: nothing.

- [ ] **Step 1: Add the Fast mode section**

In `docs/annual-prediction.md`, add after the paragraph describing what the Advanced fieldset holds (search for `samples_per_month` and the "Advanced" mention around line 118):

```markdown
### Fast mode

**Fast mode** under **Advanced** plans January, April, July and October only, and estimates
the other eight months from those four against the year's actual solar curve. A run takes
about 2.5 times less time — the saving is smaller than four-months-in-twelve suggests
because the weather download, the rate downloads and starting the engine all still happen.

It is built for comparing systems: try 5 kWh against 10 kWh, or Agile against Cosy, without
waiting three minutes for each answer. The annual totals, savings and payback it reports are
close to a full run — interpolation itself contributes under 1% to the annual saving.

Individual months are approximate, typically within 10-13% and worse in the tails, and the
month table and chart mark every estimated month so you can see which figures were modelled
rather than planned. If you want to read one specific month's number, turn fast mode off.

One caveat worth knowing, which is not about fast mode: on a tariff whose daily prices swing
widely — Agile especially — sampling two days to stand for a month carries roughly 8-18%
error of its own. Fast mode inherits that, and so does a full twelve month run. Raising
**Days sampled per month** reduces it in both modes.

Rates are still downloaded for all twelve months in fast mode, so a month with no rate data
is still reported as unavailable rather than quietly estimated over.
```

- [ ] **Step 2: Update the run-duration text**

Find the "### Running" paragraph beginning "A run takes roughly one to three minutes" and append to it:

```markdown
With **Fast mode** on (see below) those figures fall to roughly 30 to 90 seconds, or one to
two and a half minutes with a car.
```

- [ ] **Step 3: Add the CLI flag to the command line section**

In the "Running it from the command line" section, alongside the other flags:

```markdown
`--fast` enables fast mode for that run, overriding the config file: four months are planned
and the rest interpolated. Equivalent to `fast_mode: true` under `annual:`.
```

Also add `fast_mode: false` to the annotated example config block (near `samples_per_month: 2`, around line 462) with a one-line comment.

- [ ] **Step 4: Verify docs build and spelling**

Run: `cd coverage && ./run_pre_commit > /tmp/t7.txt 2>&1; echo "exit=$?"; grep -iE "cspell|Unknown word|Failed" /tmp/t7.txt | head`
Expected: exit 0, cspell Passed. If cspell flags a word, add it to `.cspell/custom-dictionary-workspace.txt` and re-stage (the file is auto-sorted on commit).

- [ ] **Step 5: Commit**

```bash
git add docs/annual-prediction.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(whatif): document fast mode"
```

---

## Final verification

- [ ] **Full suite green**

Run: `cd coverage && ./run_pre_commit > /tmp/final.txt 2>&1; echo "exit=$?"; tail -3 /tmp/final.txt`
Expected: exit 0 and `**** All tests passed`.

- [ ] **End-to-end sanity check against a real run**

Fast mode is worth confirming against the network once, not only against fixtures:

```bash
cd apps/predbat && python3 annual_cli.py --config <your-annual.yaml> --fast --out /tmp/fast.json
python3 -c "
import json; d=json.load(open('/tmp/fast.json'))
print('fast_mode', d['annual']['fast_mode'], 'interpolated', d['annual']['months_interpolated'])
print('included', d['annual']['months_included'], 'statuses', [m['status'] for m in d['months']])
print('savings', d['annual']['savings'])
"
```

Expected: `fast_mode True`, `interpolated 8`, `included 12`, statuses showing `ok` for months 1/4/7/10 and `interpolated` for the other eight, and a savings figure within a few percent of a full run of the same config.
