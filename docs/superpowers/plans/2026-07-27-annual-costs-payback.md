# Annual Install Costs and Payback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add install-cost estimates and a simple payback period for PV alone, PV with a battery, and PV with a battery run by Predbat — plus a PV-only scenario to measure the first of those, and panel-count as an alternative to kWp.

**Architecture:** A new pure module `annual_costs.py` holds every cost and payback calculation with no I/O, so it is directly testable. The engine gains a fourth scenario (`pv_only`) and writes `annual.costs` and `annual.payback` into the results document, so the CLI gets both without any web-layer involvement. The web tab adds a kWp/panels toggle, editable cost parameters under Advanced, and a costs-and-payback table.

**Tech Stack:** Python 3, aiohttp, ApexCharts.

**Spec:** `docs/superpowers/specs/2026-07-27-annual-costs-payback-design.md`

## Global Constraints

- **Costs are money figures a user makes purchasing decisions on.** A wrong number here is worse than a missing one. Where a figure cannot be computed, say so rather than emitting a plausible-looking value.
- **Payback is emitted only when `months_included == 12`.** Otherwise the annual saving covers less than a year and the payback would be overstated. Emit `{"available": false, "reason": ...}`, never an extrapolation.
- **A saving of zero or less means `{"pays_back": false}`** — never a negative or infinite year count.
- **`predbat_annual_gbp` is recurring, not capital.** It is subtracted from the Predbat row's annual saving. It must NOT be added to any capital figure and must NOT alter any scenario's `cost_p`.
- **The fourth chart colour is `#9439ef`.** Validated with the dataviz validator in both light and dark mode. The existing three (`#0072B2`, `#D55E00`, `#009E73`) do not change. Do not substitute a colour without re-running the validator in both modes.
- **`pv_only` must not call `calculate_plan()`.** It is a single prediction with empty charge/export windows, like `no_pvbat` and `without_predbat`. Calling the planner would multiply run time.
- **Never set `predbat.debug_enable`.** `kernel_supported()` requires it to be False; setting it disables the C++ kernel and makes every plan ~8x slower.
- **Line length 256 (Black) / 250 (Flake8); 100% docstrings (`interrogate`); British English (CSpell).**
- **Tests** live in `apps/predbat/tests/`, are registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`, and run from `coverage/` via `./run_all --test <name>`. **Always redirect test output to a file and grep the file** — never pipe straight to grep.
- **Registry names are not file names.** `--test web_annual` runs only `test_web_annual`, NOT `test_web_annual_form`. Use `./run_all -k web_annual` to run every web_annual test, and check your new test actually executed by grepping its printed line.
- **Run pre-commit as `coverage/venv/bin/pre-commit run --files <paths>`.** There is no `./run_pre_commit`, and `--all-files` silently skips untracked files.
- **Verify new tests discriminate.** After a test passes, mutate the code it covers to reintroduce the bug and confirm the test fails. Two defects have already shipped green in this feature because assertions matched broken code.

---

### Task 1: The cost and payback module

**Files:**
- Create: `apps/predbat/annual_costs.py`
- Create: `apps/predbat/tests/test_annual_costs.py`
- Modify: `apps/predbat/unit_test.py` (register the test)

**Interfaces:**
- Consumes: nothing — this module is pure and standalone.
- Produces:
  - `DEFAULT_COSTS` (dict), `PV_RATE_ANCHORS_KWP = (2.0, 7.0, 30.0)`
  - `resolve_costs(raw) -> dict`
  - `pv_rate_gbp_per_kwp(total_kwp, settings) -> float`
  - `pv_cost_gbp(total_kwp, settings) -> float`
  - `battery_cost_gbp(size_kwh, settings) -> float`
  - `build_costs(total_kwp, battery_kwh, settings) -> dict`
  - `payback_row(capital_gbp, annual_saving_gbp, recurring_gbp=0.0) -> dict`
  - `build_payback(annual_scenarios, costs, months_included, settings) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_annual_costs.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Unit tests for the annual install-cost and payback model.

Everything here is pure arithmetic over plain dicts - no network, no Predbat
instance, no plan run - so it is fast and registered as a non-slow test.
"""

from annual_costs import DEFAULT_COSTS, battery_cost_gbp, build_costs, build_payback, payback_row, pv_cost_gbp, pv_rate_gbp_per_kwp, resolve_costs


def close(actual, expected, tolerance=0.01):
    """Return True when two floats agree to within a tolerance."""
    return abs(actual - expected) <= tolerance


def test_annual_costs():
    """Verify the cost bands, the minimum, and the payback arithmetic."""
    failed = False
    print("**** Testing annual cost and payback model ****")
    settings = resolve_costs(None)

    print("Test: the band rate sits exactly on each published median at its anchor")
    for kwp, expected in [(2.0, 1780.0), (7.0, 1697.0), (30.0, 1262.0)]:
        rate = pv_rate_gbp_per_kwp(kwp, settings)
        if not close(rate, expected):
            print("  ERROR: {} kWp should be {} per kWp, got {}".format(kwp, expected, rate))
            failed = True

    print("Test: the rate interpolates between anchors rather than stepping")
    # A step function is what produces the 4.0/4.1 kWp discontinuity this design exists
    # to avoid, so assert an actual in-between value, not merely "different".
    if not close(pv_rate_gbp_per_kwp(4.5, settings), 1738.50):
        print("  ERROR: 4.5 kWp should interpolate to 1738.50, got {}".format(pv_rate_gbp_per_kwp(4.5, settings)))
        failed = True

    print("Test: the rate is clamped flat outside the anchor span")
    if not close(pv_rate_gbp_per_kwp(0.5, settings), 1780.0) or not close(pv_rate_gbp_per_kwp(45.0, settings), 1262.0):
        print("  ERROR: the rate should clamp to 1780 below 2 kWp and 1262 above 30 kWp")
        failed = True

    print("Test: total PV cost never decreases as the system grows")
    # This is the property that motivated interpolation over a step function.
    previous = -1.0
    size = 0.1
    while size <= 50.0:
        cost = pv_cost_gbp(size, settings)
        if cost < previous - 0.001:
            print("  ERROR: PV cost fell from {} to {} at {} kWp".format(previous, cost, size))
            failed = True
            break
        previous = cost
        size += 0.1

    print("Test: the minimum applies to a small system and not to a large one")
    if not close(pv_cost_gbp(1.0, settings), 2500.0):
        print("  ERROR: a 1 kWp system should cost the 2500 minimum, got {}".format(pv_cost_gbp(1.0, settings)))
        failed = True
    if not close(pv_cost_gbp(5.0, settings), 8651.0, tolerance=1.0):
        print("  ERROR: a 5 kWp system should cost about 8651, got {}".format(pv_cost_gbp(5.0, settings)))
        failed = True

    print("Test: no PV and no battery cost nothing, rather than the minimum")
    if pv_cost_gbp(0, settings) != 0.0:
        print("  ERROR: zero PV should cost nothing, got {}".format(pv_cost_gbp(0, settings)))
        failed = True
    if battery_cost_gbp(0, settings) != 0.0:
        print("  ERROR: zero battery should cost nothing, got {}".format(battery_cost_gbp(0, settings)))
        failed = True

    print("Test: battery cost is the install fee plus the per-kWh rate")
    if not close(battery_cost_gbp(9.5, settings), 500.0 + 300.0 * 9.5):
        print("  ERROR: a 9.5 kWh battery should cost 3350, got {}".format(battery_cost_gbp(9.5, settings)))
        failed = True

    print("Test: custom settings override every default")
    custom = resolve_costs({"battery_install_gbp": 0, "battery_per_kwh_gbp": 200, "pv_minimum_gbp": 0, "pv_rate_small_gbp_per_kwp": 1000, "pv_rate_medium_gbp_per_kwp": 900, "pv_rate_large_gbp_per_kwp": 800, "predbat_annual_gbp": 99})
    if not close(battery_cost_gbp(10, custom), 2000.0) or not close(pv_rate_gbp_per_kwp(2.0, custom), 1000.0) or custom["predbat_annual_gbp"] != 99:
        print("  ERROR: custom cost settings were not honoured: {}".format(custom))
        failed = True

    print("Test: payback divides capital by the annual saving")
    row = payback_row(10000.0, 1000.0)
    if not row["pays_back"] or not close(row["years"], 10.0):
        print("  ERROR: 10000 capital against 1000 a year should pay back in 10 years, got {}".format(row))
        failed = True

    print("Test: a zero or negative saving does not pay back")
    for saving in [0.0, -50.0]:
        row = payback_row(10000.0, saving)
        if row["pays_back"] or row.get("years") is not None:
            print("  ERROR: a saving of {} must not produce a payback period, got {}".format(saving, row))
            failed = True

    print("Test: a recurring cost reduces the saving and lengthens payback")
    row = payback_row(10000.0, 1000.0, recurring_gbp=100.0)
    if not close(row["years"], 11.111) or not close(row["annual_saving_gbp"], 900.0) or not close(row["gross_annual_saving_gbp"], 1000.0):
        print("  ERROR: a 100/year fee should give 900 net and 11.11 years, got {}".format(row))
        failed = True

    print("Test: a recurring cost exceeding the saving means it never pays back")
    # The case a "subscription as capital" implementation gets wrong: treating the fee as
    # a one-off would still show a payback period here.
    row = payback_row(10000.0, 80.0, recurring_gbp=100.0)
    if row["pays_back"]:
        print("  ERROR: a fee larger than the saving must not pay back, got {}".format(row))
        failed = True

    print("Test: payback is refused on a partial year rather than extrapolated")
    scenarios = {"no_pvbat": {"cost_p": 200000.0}, "pv_only": {"cost_p": 130000.0}, "without_predbat": {"cost_p": 110000.0}, "with_predbat": {"cost_p": 80000.0}}
    costs = build_costs(5.0, 9.5, settings)
    partial = build_payback(scenarios, costs, 11, settings)
    if partial.get("available") is not False or "11" not in str(partial.get("reason", "")):
        print("  ERROR: 11 months should refuse payback and say why, got {}".format(partial))
        failed = True

    print("Test: a full year produces all three payback rows")
    full = build_payback(scenarios, costs, 12, settings)
    for key, capital in [("pv_only", costs["pv_gbp"]), ("pv_battery", costs["total_gbp"]), ("pv_battery_predbat", costs["total_gbp"])]:
        if key not in full:
            print("  ERROR: {} row missing from payback, got {}".format(key, full))
            failed = True
        elif not close(full[key]["capital_gbp"], capital):
            print("  ERROR: {} should use capital {}, got {}".format(key, capital, full[key]["capital_gbp"]))
            failed = True
    # no_pvbat 200000p - pv_only 130000p = 70000p = 700 GBP a year against PV-only capital
    if not close(full["pv_only"]["annual_saving_gbp"], 700.0):
        print("  ERROR: the PV-only saving should be 700 a year, got {}".format(full["pv_only"]["annual_saving_gbp"]))
        failed = True

    print("Test: the Predbat fee changes only the Predbat row")
    paid = build_payback(scenarios, costs, 12, resolve_costs({"predbat_annual_gbp": 100}))
    if not close(paid["pv_only"]["annual_saving_gbp"], full["pv_only"]["annual_saving_gbp"]):
        print("  ERROR: the Predbat fee must not touch the PV-only row")
        failed = True
    if not close(paid["pv_battery"]["annual_saving_gbp"], full["pv_battery"]["annual_saving_gbp"]):
        print("  ERROR: the Predbat fee must not touch the PV+battery row")
        failed = True
    if not close(paid["pv_battery_predbat"]["annual_saving_gbp"], full["pv_battery_predbat"]["annual_saving_gbp"] - 100.0):
        print("  ERROR: the Predbat fee should reduce the Predbat row's saving by 100")
        failed = True
    if not close(paid["pv_battery_predbat"]["capital_gbp"], full["pv_battery_predbat"]["capital_gbp"]):
        print("  ERROR: the Predbat fee is recurring and must not change capital")
        failed = True

    if failed:
        print("**** ERROR: annual cost tests failed ****")
    return failed
```

- [ ] **Step 2: Register the test**

In `apps/predbat/unit_test.py`, add the import beside the other annual imports:

```python
from tests.test_annual_costs import test_annual_costs
```

and the registry entry beside the other annual entries:

```python
        ("annual_costs", test_annual_costs, "Annual install cost and payback model tests", False),
```

- [ ] **Step 3: Run it and watch it fail**

```bash
cd coverage && ./run_all --test annual_costs > /tmp/t.txt 2>&1; grep -iE "ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: `ModuleNotFoundError: No module named 'annual_costs'`.

- [ ] **Step 4: Write the module**

Create `apps/predbat/annual_costs.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Install-cost and payback model for the annual prediction tool.

Pure arithmetic over plain dicts: no I/O, no Predbat state, no network. The engine
calls this once per run to turn a system size into an estimated capital cost, and a
year of modelled savings into a payback period.

The PV rate interpolates between published band medians rather than stepping between
them. A step function makes a 4.1 kWp system cost less in total than a 4.0 kWp one,
because it drops onto the cheaper band's rate for its whole size - an artefact of the
bucketing, not a real price. Interpolating between each band's midpoint keeps total
cost monotonic across the whole range.
"""

# Published median install costs, GBP per kWp, for financial year 2025/26. Each is the
# median for systems within a size band, so it describes the typical system at that
# band's CENTRE - which is where it is anchored below.
DEFAULT_COSTS = {
    "battery_install_gbp": 500.0,
    "battery_per_kwh_gbp": 300.0,
    "pv_minimum_gbp": 2500.0,
    "pv_rate_small_gbp_per_kwp": 1780.0,  # band 0-4 kWp
    "pv_rate_medium_gbp_per_kwp": 1697.0,  # band 4-10 kWp
    "pv_rate_large_gbp_per_kwp": 1262.0,  # band 10-50 kWp
    # Predbat itself. Zero when self-hosted; the hosted version is expected to charge
    # around 100 a year. RECURRING, not capital - see payback_row().
    "predbat_annual_gbp": 0.0,
}

# Midpoints of the 0-4, 4-10 and 10-50 kWp bands the rates above are medians of.
PV_RATE_ANCHORS_KWP = (2.0, 7.0, 30.0)

_RATE_KEYS = ("pv_rate_small_gbp_per_kwp", "pv_rate_medium_gbp_per_kwp", "pv_rate_large_gbp_per_kwp")


def resolve_costs(raw):
    """Return the cost settings, merging any overrides over the defaults.

    Every value must be a non-negative number; anything else raises ValueError naming
    the field, rather than silently falling back to a default and quietly costing the
    user's system at a price they did not ask for.
    """
    settings = dict(DEFAULT_COSTS)
    if not raw:
        return settings
    if not isinstance(raw, dict):
        raise ValueError("annual.costs must be a mapping")
    for key, value in raw.items():
        if key not in DEFAULT_COSTS:
            raise ValueError("annual.costs.{} is not a recognised cost setting".format(key))
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("annual.costs.{} must be a number, got {!r}".format(key, value))
        if number < 0:
            raise ValueError("annual.costs.{} must not be negative, got {}".format(key, number))
        settings[key] = number
    return settings


def pv_rate_gbp_per_kwp(total_kwp, settings):
    """Return the GBP-per-kWp rate for a system of this size.

    Linear interpolation between the three band anchors, clamped flat beyond the first
    and last. Clamping rather than extrapolating keeps a very small or very large system
    on a published figure instead of a straight line run off the end of the data.
    """
    rates = [settings[key] for key in _RATE_KEYS]
    anchors = PV_RATE_ANCHORS_KWP
    size = float(total_kwp)
    if size <= anchors[0]:
        return rates[0]
    if size >= anchors[-1]:
        return rates[-1]
    for index in range(len(anchors) - 1):
        low, high = anchors[index], anchors[index + 1]
        if low <= size <= high:
            fraction = (size - low) / (high - low)
            return rates[index] + fraction * (rates[index + 1] - rates[index])
    return rates[-1]


def pv_cost_gbp(total_kwp, settings):
    """Return the estimated PV install cost, or zero when there is no PV.

    The minimum install price applies to a real system only. A system of no panels
    costs nothing - returning the minimum there would invent a cost for equipment the
    user does not have and make a no-PV scenario look expensive.
    """
    size = float(total_kwp or 0)
    if size <= 0:
        return 0.0
    return max(settings["pv_minimum_gbp"], size * pv_rate_gbp_per_kwp(size, settings))


def battery_cost_gbp(size_kwh, settings):
    """Return the estimated battery install cost, or zero when there is no battery."""
    size = float(size_kwh or 0)
    if size <= 0:
        return 0.0
    return settings["battery_install_gbp"] + settings["battery_per_kwh_gbp"] * size


def build_costs(total_kwp, battery_kwh, settings):
    """Return the capital cost breakdown for a system of this size."""
    pv = pv_cost_gbp(total_kwp, settings)
    battery = battery_cost_gbp(battery_kwh, settings)
    return {
        "pv_gbp": round(pv, 2),
        "battery_gbp": round(battery, 2),
        "total_gbp": round(pv + battery, 2),
        "pv_rate_gbp_per_kwp": round(pv_rate_gbp_per_kwp(total_kwp or 0, settings), 2) if (total_kwp or 0) > 0 else 0.0,
        "total_kwp": round(float(total_kwp or 0), 3),
        "battery_kwh": round(float(battery_kwh or 0), 3),
    }


def payback_row(capital_gbp, annual_saving_gbp, recurring_gbp=0.0):
    """Return one payback row: capital divided by the net annual saving.

    ``recurring_gbp`` is an ongoing yearly cost (Predbat's own fee), so it is subtracted
    from the saving rather than added to capital. Adding it to capital would understate
    it enormously - over a ten year payback a 100 a year fee is 1000, not 100.

    A net saving of zero or less reports ``pays_back: False`` with no year count. A
    negative payback period is meaningless, and dividing by a near-zero saving produces
    a huge number that reads like an answer rather than the absence of one.
    """
    gross = float(annual_saving_gbp)
    net = gross - float(recurring_gbp or 0.0)
    row = {
        "capital_gbp": round(float(capital_gbp), 2),
        "gross_annual_saving_gbp": round(gross, 2),
        "annual_saving_gbp": round(net, 2),
        "predbat_annual_gbp": round(float(recurring_gbp or 0.0), 2),
    }
    if net <= 0:
        row["pays_back"] = False
        row["years"] = None
        return row
    row["pays_back"] = True
    row["years"] = round(float(capital_gbp) / net, 2)
    return row


def build_payback(annual_scenarios, costs, months_included, settings):
    """Return the payback block for a completed run, or a reason it could not be built.

    Payback needs a full year. When a month is unavailable the annual totals cover less
    than twelve months, so every saving is understated and every payback period
    correspondingly overstated. Extrapolating a partial year to a full one would invent
    savings for months the tool could not price, so this refuses instead and says which
    it had - matching how the rest of the tool declines to count an unavailable month.
    """
    if not annual_scenarios:
        return {"available": False, "reason": "No month produced a usable result, so there is nothing to pay back."}
    if months_included != 12:
        return {"available": False, "reason": "Payback needs a full year, but only {} of 12 months could be modelled. The missing months are named in the caveats.".format(months_included)}

    baseline = annual_scenarios.get("no_pvbat", {}).get("cost_p")
    if baseline is None:
        return {"available": False, "reason": "The no-PV/battery baseline is missing, so there is nothing to compare against."}

    def saving_gbp(key):
        """Return the annual saving in GBP of one scenario against the no-system baseline."""
        return (baseline - annual_scenarios.get(key, {}).get("cost_p", baseline)) / 100.0

    return {
        "available": True,
        "pv_only": payback_row(costs["pv_gbp"], saving_gbp("pv_only")),
        "pv_battery": payback_row(costs["total_gbp"], saving_gbp("without_predbat")),
        "pv_battery_predbat": payback_row(costs["total_gbp"], saving_gbp("with_predbat"), recurring_gbp=settings["predbat_annual_gbp"]),
    }
```

- [ ] **Step 5: Run the tests**

```bash
cd coverage && ./run_all --test annual_costs > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: PASS, exit 0.

- [ ] **Step 6: Verify the monotonic test discriminates**

Temporarily replace `pv_rate_gbp_per_kwp`'s interpolation with a step function (return `rates[0]` below 4, `rates[1]` below 10, else `rates[2]`), re-run, and confirm the monotonic test FAILS. Restore afterwards. This is the property the whole interpolation design exists to protect; a test that passes either way is worthless.

- [ ] **Step 7: Commit**

```bash
cd /Users/treforsouthwell/source/batpred
coverage/venv/bin/pre-commit run --files apps/predbat/annual_costs.py apps/predbat/tests/test_annual_costs.py apps/predbat/unit_test.py
git add apps/predbat/annual_costs.py apps/predbat/tests/test_annual_costs.py apps/predbat/unit_test.py
git commit -m "feat(annual): add the install cost and payback model"
```

---

### Task 2: Panel count as an alternative to kWp

**Files:**
- Modify: `apps/predbat/annual.py` (`_validate_solar`, around line 105)
- Test: `apps/predbat/tests/test_annual_config.py` — this module already covers `validate_config`'s solar handling and already has a `base_config()` helper at line 17 returning a minimal valid config, which the tests below use directly.

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `_validate_solar` accepts `panels` and `panel_watts`, and every normalised array still carries a `kwp` float. `DEFAULT_PANEL_WATTS = 400.0` is exported from `annual.py`.

- [ ] **Step 1: Write the failing tests**

Add to the test module covering `validate_config`, following that file's existing print-and-flag style:

```python
    print("Test: a panel count derives kwp at 400 W a panel")
    config = base_config()
    config["solar"] = [{"panels": 13}]
    validated = validate_config(config)
    if abs(validated["solar"][0]["kwp"] - 5.2) > 0.001:
        print("  ERROR: 13 panels at 400 W should be 5.2 kWp, got {}".format(validated["solar"][0]["kwp"]))
        failed = True

    print("Test: a custom panel wattage is honoured")
    config = base_config()
    config["solar"] = [{"panels": 10, "panel_watts": 450}]
    validated = validate_config(config)
    if abs(validated["solar"][0]["kwp"] - 4.5) > 0.001:
        print("  ERROR: 10 panels at 450 W should be 4.5 kWp, got {}".format(validated["solar"][0]["kwp"]))
        failed = True

    print("Test: the panel count and wattage survive validation for form round-tripping")
    if validated["solar"][0].get("panels") != 10 or validated["solar"][0].get("panel_watts") != 450:
        print("  ERROR: panels/panel_watts should be retained, got {}".format(validated["solar"][0]))
        failed = True

    print("Test: supplying both kwp and panels is rejected rather than silently preferring one")
    config = base_config()
    config["solar"] = [{"kwp": 5.0, "panels": 13}]
    try:
        validate_config(config)
        print("  ERROR: supplying both kwp and panels should be rejected")
        failed = True
    except AnnualConfigError as error:
        if "panels" not in str(error) or "kwp" not in str(error):
            print("  ERROR: the error should name both fields, got {}".format(error))
            failed = True

    print("Test: an array with neither kwp nor panels is rejected")
    config = base_config()
    config["solar"] = [{"declination": 35}]
    try:
        validate_config(config)
        print("  ERROR: an array with neither kwp nor panels should be rejected")
        failed = True
    except AnnualConfigError:
        pass

    print("Test: a fractional or zero panel count is rejected")
    for bad in [0, 2.5, -3]:
        config = base_config()
        config["solar"] = [{"panels": bad}]
        try:
            validate_config(config)
            print("  ERROR: a panel count of {} should be rejected".format(bad))
            failed = True
        except AnnualConfigError:
            pass
```

`base_config()` and `AnnualConfigError` are already imported in that module; add nothing new beyond the tests themselves.

- [ ] **Step 2: Run it and watch it fail**

```bash
cd coverage && ./run_all -k annual > /tmp/t.txt 2>&1; grep -iE "  ERROR|FAIL" /tmp/t.txt
```

Expected: failures on the panel tests.

- [ ] **Step 3: Implement**

In `apps/predbat/annual.py`, add near the other defaults:

```python
# A typical domestic panel in 2026. Only used to turn a panel count into kWp.
DEFAULT_PANEL_WATTS = 400.0
```

Replace the `if "kwp" not in array:` block in `_validate_solar` with:

```python
        has_kwp = "kwp" in array
        has_panels = "panels" in array
        if has_kwp and has_panels:
            # Two figures that disagree are a mistake worth surfacing. Guessing which the
            # user meant would silently model a different system than they described.
            raise AnnualConfigError("annual.solar[{}] has both kwp and panels; give one or the other, not both".format(index))
        if not has_kwp and not has_panels:
            raise AnnualConfigError("annual.solar[{}] is missing kwp (the array's peak power in kW) or panels (how many panels it has)".format(index))
        normalised = dict(array)
        if has_panels:
            panels = _require_number(array["panels"], "annual.solar[{}].panels".format(index), minimum=0, exclusive_minimum=True, integer=True)
            panel_watts = _require_number(array.get("panel_watts", DEFAULT_PANEL_WATTS), "annual.solar[{}].panel_watts".format(index), minimum=0, exclusive_minimum=True)
            # Retained alongside the derived kwp so the web form can show back what the
            # user actually typed rather than replacing it with a computed decimal.
            normalised["panels"] = panels
            normalised["panel_watts"] = panel_watts
            normalised["kwp"] = panels * panel_watts / 1000.0
        else:
            normalised["kwp"] = _require_number(array["kwp"], "annual.solar[{}].kwp".format(index), minimum=0, exclusive_minimum=True)
```

Leave the `declination`/`azimuth`/`efficiency`/`azimuth_zero_south` lines that follow unchanged.

- [ ] **Step 4: Run the tests**

```bash
cd coverage && ./run_all -k annual > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: all pass, exit 0.

- [ ] **Step 5: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/annual.py apps/predbat/tests/test_annual_config.py
git add apps/predbat/annual.py apps/predbat/tests/
git commit -m "feat(annual): accept a panel count as an alternative to kWp"
```

---

### Task 3: The `pv_only` scenario

**Files:**
- Modify: `apps/predbat/annual.py` (`_run_scenarios` around line 1010, `SCENARIO_KEYS` at line 1149)
- Test: `apps/predbat/tests/test_annual_results.py`, `apps/predbat/tests/test_annual_integration.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces: `SCENARIO_KEYS = ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]`. Every month row's `scenarios` dict and the annual totals gain a `pv_only` entry with the same fields as the others.

- [ ] **Step 1: Add the scenario key**

In `apps/predbat/annual.py`:

```python
SCENARIO_KEYS = ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]
```

`_blend_results`, `_month_scenarios` and `_build_results` all iterate `SCENARIO_KEYS`, so they follow automatically. Do not hand-edit them.

- [ ] **Step 2: Run the tests to see what breaks**

```bash
cd coverage && ./run_all -k annual > /tmp/t.txt 2>&1; grep -iE "  ERROR|FAIL|KeyError" /tmp/t.txt
```

Expected: failures in tests that build scenario dicts from hand-written literals (they will be missing `pv_only`). Note which, and fix them in Step 5.

- [ ] **Step 3: Implement the scenario**

In `_run_scenarios`, immediately after the `results["no_pvbat"] = ...` line and its plan capture, insert:

```python
    # Scenario 1b: PV but no battery. apply_hardware gives the array its real inverter
    # and export limits - a PV-only system still has an inverter, and clipping matters -
    # while soc_max=0 leaves it with nowhere to store surplus, so everything the house
    # cannot use at the moment it is generated is exported. That difference is the whole
    # point of this scenario: it is what makes PV-only payback different from a fixed
    # fraction of the PV-plus-battery figure.
    #
    # Like scenarios 1 and 2 this is a single prediction with empty windows. It must NOT
    # call calculate_plan(): the planner is what makes a run expensive, and there is
    # nothing to plan without a battery.
    apply_hardware(predbat, config["battery"], config["solar"])
    predbat.soc_kw = 0
    predbat.charge_limit_best = []
    predbat.charge_window_best = []
    predbat.export_window_best = []
    predbat.export_limits_best = []
    predbat.prediction = Prediction(predbat, actual_step, actual_step, load_step, load_step, soc_kw=0, soc_max=0)
    results["pv_only"] = _billed_result(predbat, DAY_MINUTES, actual_step)
    if plans is not None:
        plans["pv_only"] = _capture_plan(predbat, actual_step, actual_step, load_step, load_step, DAY_MINUTES)
```

`predbat.load_forecast` is still `baseline_load` from scenario 1 at this point, which is what this scenario wants — the same household load, including the car on its timer, as the other baselines.

- [ ] **Step 4: Add an assertion that it is a real, distinct scenario**

Add to `apps/predbat/tests/test_annual_integration.py`, inside the existing integration test after a day has been run:

```python
    print("Test: pv_only generates PV but stores none of it")
    pv_only = result["pv_only"]
    if pv_only["pv_generated_kwh"] <= 0:
        print("  ERROR: the PV-only scenario should generate PV, got {}".format(pv_only["pv_generated_kwh"]))
        failed = True
    if pv_only["battery_throughput_kwh"] != 0:
        print("  ERROR: the PV-only scenario must have no battery throughput, got {}".format(pv_only["battery_throughput_kwh"]))
        failed = True

    print("Test: pv_only sits between the no-system baseline and PV-plus-battery on cost")
    # Not an arbitrary ordering check: PV alone must beat no system (it displaces import),
    # and must not beat PV plus a battery (the battery can only add value). A violation
    # means the scenario is not modelling what its name says.
    if not (result["no_pvbat"]["cost_p"] > pv_only["cost_p"] > result["without_predbat"]["cost_p"]):
        print("  ERROR: expected no_pvbat > pv_only > without_predbat on cost, got {} / {} / {}".format(result["no_pvbat"]["cost_p"], pv_only["cost_p"], result["without_predbat"]["cost_p"]))
        failed = True
```

- [ ] **Step 5: Fix the hand-written scenario literals**

Update every test dict that enumerates scenarios by hand to include `pv_only`, using a value between `no_pvbat` and `without_predbat` so the ordering assertion above holds. The three files with hand-written literals are:

- `test_annual_results.py` — every `make_month_row(...)` call site passes a `costs` dict keyed by scenario; each gains a `"pv_only"` key.
- `test_annual_cli.py` — the sample results documents around lines 66-68 and 122-124.
- `test_web_annual.py` — `sample_run_results()`'s month and annual scenario dicts.

`test_annual_scenarios.py` needs **no** change: it builds its scenario dicts with `{key: ... for key in SCENARIO_KEYS}` comprehensions, so it picks the new key up automatically. Do not add literals to it.

- [ ] **Step 6: Run the tests**

```bash
cd coverage && ./run_all -k annual > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL|Traceback" /tmp/t.txt
cd coverage && ./run_all --test annual_integration > /tmp/i.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL" /tmp/i.txt
```

Expected: all pass, exit 0. Note the integration test takes ~90 s.

- [ ] **Step 7: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/annual.py apps/predbat/tests/test_annual_results.py apps/predbat/tests/test_annual_integration.py apps/predbat/tests/test_annual_cli.py
git add apps/predbat/annual.py apps/predbat/tests/
git commit -m "feat(annual): add a PV-only scenario so PV alone can be costed"
```

---

### Task 4: Wire costs and payback into the results document

**Files:**
- Modify: `apps/predbat/annual.py` (`validate_config` return, `_build_results`)
- Test: `apps/predbat/tests/test_annual_results.py`

**Interfaces:**
- Consumes: `resolve_costs`, `build_costs`, `build_payback` from Task 1; `SCENARIO_KEYS` including `pv_only` from Task 3; `kwp` on every array from Task 2.
- Produces: `config["costs"]` (validated settings dict); `results["annual"]["costs"]` and `results["annual"]["payback"]`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_annual_results.py`:

```python
    print("Test: the annual block carries a cost breakdown")
    predictor = make_predictor()
    months = [make_month_row(month, {"no_pvbat": 1000.0, "pv_only": 700.0, "without_predbat": 600.0, "with_predbat": 400.0}) for month in range(1, 13)]
    result = predictor._build_results(months)
    costs = result["annual"]["costs"]
    # base_config()'s solar is a single 5.0 kWp array and its battery is 10.0 kWh.
    if abs(costs["total_kwp"] - 5.0) > 0.001 or abs(costs["battery_kwh"] - 10.0) > 0.001:
        print("  ERROR: the cost block should reflect the configured system, got {}".format(costs))
        failed = True
    if abs(costs["battery_gbp"] - 3500.0) > 0.01:
        print("  ERROR: a 10 kWh battery should cost 3500, got {}".format(costs["battery_gbp"]))
        failed = True
    if abs(costs["total_gbp"] - (costs["pv_gbp"] + costs["battery_gbp"])) > 0.01:
        print("  ERROR: total should be pv plus battery, got {}".format(costs))
        failed = True

    print("Test: a full twelve months produces payback for all three options")
    payback = result["annual"]["payback"]
    if not payback.get("available"):
        print("  ERROR: twelve months should produce payback, got {}".format(payback))
        failed = True
    for key in ["pv_only", "pv_battery", "pv_battery_predbat"]:
        if key not in payback:
            print("  ERROR: payback should include {}, got {}".format(key, list(payback)))
            failed = True

    print("Test: a partial year refuses payback rather than extrapolating")
    partial_months = [make_month_row(month, {"no_pvbat": 1000.0, "pv_only": 700.0, "without_predbat": 600.0, "with_predbat": 400.0}) for month in range(1, 12)]
    partial_months.append(make_unavailable_row(12))
    partial = make_predictor()._build_results(partial_months)
    if partial["annual"]["payback"].get("available") is not False:
        print("  ERROR: eleven usable months should refuse payback, got {}".format(partial["annual"]["payback"]))
        failed = True
    if partial["annual"]["costs"]["total_gbp"] <= 0:
        print("  ERROR: costs should still be reported even when payback cannot be")
        failed = True
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd coverage && ./run_all --test annual_results > /tmp/t.txt 2>&1; grep -iE "  ERROR|KeyError" /tmp/t.txt
```

Expected: `KeyError: 'costs'`.

- [ ] **Step 3: Validate the costs block**

In `apps/predbat/annual.py`, import at the top beside the other local imports:

```python
from annual_costs import build_costs, build_payback, resolve_costs
```

In `validate_config`'s returned dict, add beside `"samples_per_month"`:

```python
        "costs": _validated_costs(raw.get("costs")),
```

and add this helper beside the other `_validate_*` functions, so a bad cost value produces the same error type as every other config mistake:

```python
def _validated_costs(raw):
    """Return the validated install-cost settings, as an AnnualConfigError on failure.

    annual_costs.resolve_costs raises ValueError because it is a pure module with no
    dependency on this one; translating here keeps every config problem a single
    exception type for the CLI and web layer to catch.
    """
    try:
        return resolve_costs(raw)
    except ValueError as error:
        raise AnnualConfigError(str(error))
```

- [ ] **Step 4: Emit both blocks**

In `_build_results`, replace the `return {` block's `"annual"` value so it also carries costs and payback. Compute them just before the return:

```python
        total_kwp = sum(float(array.get("kwp", 0) or 0) for array in self.config.get("solar") or [])
        battery_kwh = float((self.config.get("battery") or {}).get("size_kwh", 0) or 0)
        costs = build_costs(total_kwp, battery_kwh, self.config["costs"])
        payback = build_payback(annual_scenarios, costs, len(included), self.config["costs"])
        if not payback.get("available"):
            self.caveats.append("Payback could not be calculated: {}".format(payback["reason"]))
        else:
            self.caveats.append("Payback is simple payback - capital divided by the modelled annual saving. It ignores panel degradation, electricity price inflation, battery replacement and finance costs, so treat it as a comparison aid rather than a financial projection.")
```

and add to the `"annual"` dict:

```python
                "costs": costs,
                "payback": payback,
```

- [ ] **Step 5: Run the tests**

```bash
cd coverage && ./run_all -k annual > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: all pass, exit 0.

- [ ] **Step 6: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/annual.py apps/predbat/tests/test_annual_results.py
git add apps/predbat/annual.py apps/predbat/tests/test_annual_results.py
git commit -m "feat(annual): report install costs and payback in the results document"
```

---

### Task 5: Web form — panel toggle, live total, and cost settings

**Files:**
- Modify: `apps/predbat/web_annual.py` (`render_form` solar fieldset ~line 339, `config_from_post` ~line 473, `render_css`, `render_script`, `DEFAULT_CONFIG`)
- Test: `apps/predbat/tests/test_web_annual.py`

**Interfaces:**
- Consumes: `panels`/`panel_watts` from Task 2; `DEFAULT_COSTS` from Task 1.
- Produces: form fields `solar_mode_{i}`, `solar_panels_{i}`, `solar_panel_watts_{i}`, and `cost_{key}` for each key in `DEFAULT_COSTS`; `config_from_post` emits a `costs` block.

- [ ] **Step 1: Write the failing tests**

Add to `test_web_annual_form` in `apps/predbat/tests/test_web_annual.py`:

```python
    print("Test: each array offers a kWp/panels mode toggle and a panel count")
    form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
    for field in ['name="solar_mode_0"', 'name="solar_panels_0"', 'name="solar_panel_watts_0"']:
        if field not in form:
            print("  ERROR: the solar fieldset should offer {}".format(field))
            failed = True

    print("Test: a running total is shown under the roof aspects")
    if 'id="annual-solar-total"' not in form:
        print("  ERROR: the solar fieldset should show a running kWp/panel total")
        failed = True

    print("Test: submitting panels produces a panels config, not a kwp one")
    postdata = valid_postdata()
    postdata["solar_mode_0"] = "panels"
    postdata["solar_panels_0"] = "13"
    postdata["solar_panel_watts_0"] = "400"
    config = make_page(my_predbat).config_from_post(postdata)
    array = config["solar"][0]
    if array.get("panels") != 13 or "kwp" in array:
        print("  ERROR: panel mode should send panels and NOT kwp - validate_config rejects both together. Got {}".format(array))
        failed = True
    validate_config(config)

    print("Test: submitting kWp produces a kwp config, not a panels one")
    postdata = valid_postdata()
    postdata["solar_mode_0"] = "kwp"
    config = make_page(my_predbat).config_from_post(postdata)
    array = config["solar"][0]
    if "panels" in array or not array.get("kwp"):
        print("  ERROR: kWp mode should send kwp and NOT panels. Got {}".format(array))
        failed = True
    validate_config(config)

    print("Test: the cost settings appear and round-trip")
    for key in ["cost_battery_install_gbp", "cost_battery_per_kwh_gbp", "cost_pv_minimum_gbp", "cost_predbat_annual_gbp"]:
        if 'name="{}"'.format(key) not in form:
            print("  ERROR: the form should offer {}".format(key))
            failed = True
    postdata = valid_postdata()
    postdata["cost_battery_per_kwh_gbp"] = "250"
    postdata["cost_predbat_annual_gbp"] = "100"
    config = make_page(my_predbat).config_from_post(postdata)
    if config.get("costs", {}).get("battery_per_kwh_gbp") != 250 or config.get("costs", {}).get("predbat_annual_gbp") != 100:
        print("  ERROR: submitted cost settings should reach the config, got {}".format(config.get("costs")))
        failed = True

    print("Test: the Predbat annual cost defaults to zero")
    if make_page(my_predbat).prefill_config().get("costs", {}).get("predbat_annual_gbp", 0) != 0:
        print("  ERROR: predbat_annual_gbp must default to 0 - Predbat is free when self-hosted")
        failed = True
```

- [ ] **Step 2: Run and watch it fail**

```bash
cd coverage && ./run_all --test web_annual_form > /tmp/t.txt 2>&1; grep -E "  ERROR" /tmp/t.txt
```

- [ ] **Step 3: Render the mode toggle and panel fields**

Replace the solar fieldset loop body in `render_form` with:

```python
        text += "<fieldset><legend>Solar</legend>\n"
        for index, array in enumerate(solar):
            mode = "panels" if array.get("panels") else "kwp"
            text += '<div class="annual-array"><strong>Array {}</strong>\n'.format(index + 1)
            text += '<div class="annual-field"><label for="solar_mode_{i}">Describe this array by</label><select id="solar_mode_{i}" name="solar_mode_{i}" onchange="annualSolarModeChanged({i})">\n'.format(i=index)
            for value, caption in [("kwp", "Peak power (kWp)"), ("panels", "Number of panels")]:
                text += '<option value="{}" {}>{}</option>\n'.format(value, "selected" if mode == value else "", caption)
            text += "</select></div>\n"
            text += '<div id="solar-kwp-row-{i}" style="display:{d}">\n'.format(i=index, d="none" if mode == "panels" else "block")
            text += self._number_field("solar_kwp_{}".format(index), "Peak power", array.get("kwp"), suffix="kWp")
            text += "</div>\n"
            text += '<div id="solar-panels-row-{i}" style="display:{d}">\n'.format(i=index, d="block" if mode == "panels" else "none")
            text += self._number_field("solar_panels_{}".format(index), "Number of panels", array.get("panels", 0), step="1")
            text += self._number_field("solar_panel_watts_{}".format(index), "Watts per panel", array.get("panel_watts", 400), suffix="W")
            text += "</div>\n"
            text += self._number_field("solar_declination_{}".format(index), "Pitch", array.get("declination", 35), suffix="degrees")
            text += self._number_field("solar_azimuth_{}".format(index), "Azimuth (180 = south)", array.get("azimuth", 180), suffix="degrees")
            text += "</div>\n"
        text += '<p class="annual-note" id="annual-solar-total"></p>\n'
        text += "</fieldset>\n"
```

- [ ] **Step 4: Add the cost settings to Advanced**

In the Advanced fieldset, beside `samples_per_month`, add one number field per cost key:

```python
        costs_config = config.get("costs") or {}
        for key, label in [
            ("battery_install_gbp", "Battery install cost"),
            ("battery_per_kwh_gbp", "Battery cost per kWh"),
            ("pv_minimum_gbp", "Minimum PV install cost"),
            ("pv_rate_small_gbp_per_kwp", "PV £/kWp for a small system (about 2 kWp)"),
            ("pv_rate_medium_gbp_per_kwp", "PV £/kWp for a medium system (about 7 kWp)"),
            ("pv_rate_large_gbp_per_kwp", "PV £/kWp for a large system (about 30 kWp)"),
            ("predbat_annual_gbp", "Predbat cost per year"),
        ]:
            text += self._number_field("cost_{}".format(key), label, costs_config.get(key, DEFAULT_COSTS[key]), suffix="£")
```

Import `DEFAULT_COSTS` at the top of `web_annual.py`:

```python
from annual_costs import DEFAULT_COSTS
```

- [ ] **Step 5: Read them back in `config_from_post`**

Replace the solar-array loop so each array sends **either** `kwp` **or** `panels`, never both — `validate_config` rejects an array carrying both:

```python
        index = 0
        arrays = []
        while value("solar_kwp_{}".format(index)) is not None or value("solar_panels_{}".format(index)) is not None:
            array = {
                "declination": numeric("solar_declination_{}".format(index), 35),
                "azimuth": numeric("solar_azimuth_{}".format(index), 180),
                "efficiency": numeric("solar_efficiency_{}".format(index), 0.95),
            }
            if value("solar_mode_{}".format(index)) == "panels":
                array["panels"] = int(numeric("solar_panels_{}".format(index), 0) or 0)
                array["panel_watts"] = numeric("solar_panel_watts_{}".format(index), 400)
            else:
                array["kwp"] = numeric("solar_kwp_{}".format(index))
            arrays.append(array)
            index += 1
```

and add the costs block after it:

```python
        costs = {}
        for key in DEFAULT_COSTS:
            submitted = numeric("cost_{}".format(key), None)
            if submitted is not None:
                costs[key] = submitted
        if costs:
            config["costs"] = costs
```

- [ ] **Step 6: Add the toggle and running total to the page script**

In `render_script`, add:

```javascript
function annualSolarModeChanged(index) {
  var mode = document.getElementById('solar_mode_' + index).value;
  document.getElementById('solar-kwp-row-' + index).style.display = (mode === 'panels') ? 'none' : 'block';
  document.getElementById('solar-panels-row-' + index).style.display = (mode === 'panels') ? 'block' : 'none';
  annualUpdateSolarTotal();
}
function annualUpdateSolarTotal() {
  var totalKwp = 0, totalPanels = 0, allPanels = true, index = 0;
  while (document.getElementById('solar_mode_' + index)) {
    var mode = document.getElementById('solar_mode_' + index).value;
    if (mode === 'panels') {
      var count = parseFloat(document.getElementById('solar_panels_' + index).value) || 0;
      var watts = parseFloat(document.getElementById('solar_panel_watts_' + index).value) || 0;
      totalPanels += count;
      totalKwp += count * watts / 1000;
    } else {
      allPanels = false;
      totalKwp += parseFloat(document.getElementById('solar_kwp_' + index).value) || 0;
    }
    index += 1;
  }
  var note = document.getElementById('annual-solar-total');
  if (!note) { return; }
  // The panel count is shown only when EVERY array was entered as panels. A partial
  // count would read as the whole system's, which is worse than not showing one.
  note.textContent = allPanels && totalPanels > 0
    ? 'Total: ' + totalKwp.toFixed(2) + ' kWp across ' + totalPanels + ' panels'
    : 'Total: ' + totalKwp.toFixed(2) + ' kWp';
}
document.addEventListener('input', function (event) {
  if (event.target && /^solar_(kwp|panels|panel_watts)_/.test(event.target.id)) { annualUpdateSolarTotal(); }
});
annualUpdateSolarTotal();
```

- [ ] **Step 7: Run the tests and verify they discriminate**

```bash
cd coverage && ./run_all -k web_annual > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -E "  ERROR|FAILED" /tmp/t.txt
```

Then mutate `config_from_post` to emit BOTH `kwp` and `panels` in panel mode, re-run, and confirm the "panel mode should send panels and NOT kwp" test fails. Restore afterwards.

- [ ] **Step 8: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py
git add apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py
git commit -m "feat(annual): offer panel counts and editable install costs in the form"
```

---

### Task 6: Web results — fourth series and the payback table

**Files:**
- Modify: `apps/predbat/web_annual.py` (`SCENARIO_COLOURS`, `SCENARIO_LABELS`, `SCENARIO_ORDER`, `render_results`)
- Modify: `docs/annual-prediction.md`
- Test: `apps/predbat/tests/test_web_annual.py`

**Interfaces:**
- Consumes: `annual.costs` and `annual.payback` from Task 4; the `pv_only` scenario from Task 3.
- Produces: nothing further.

- [ ] **Step 1: Write the failing tests**

Add to `test_web_annual_results` in `apps/predbat/tests/test_web_annual.py`, and extend that module's `sample_run_results()` so its month rows and annual block carry `pv_only` plus a `costs`/`payback` block:

```python
    print("Test: the PV-only scenario appears in the chart and the month table")
    if "#9439ef" not in html:
        print("  ERROR: the validated PV-only colour should be present")
        failed = True
    if "PV only" not in html:
        print("  ERROR: the PV-only scenario should be labelled in the results")
        failed = True

    print("Test: the payback table shows all three purchase options")
    for label in ["PV only", "PV + battery", "With Predbat"]:
        if label not in html:
            print("  ERROR: the payback table should include {}".format(label))
            failed = True

    print("Test: a non-paying-back option says so rather than showing a number")
    no_payback = copy.deepcopy(results)
    no_payback["annual"]["payback"]["pv_only"] = {"pays_back": False, "years": None, "capital_gbp": 8000.0, "annual_saving_gbp": -10.0, "gross_annual_saving_gbp": -10.0, "predbat_annual_gbp": 0.0}
    text = page.render_results(no_payback, runs, runs[0]["id"])
    if "does not pay back" not in text.lower():
        print("  ERROR: an option that never pays back should say so")
        failed = True

    print("Test: an unavailable payback shows its reason instead of a blank table")
    unavailable = copy.deepcopy(results)
    unavailable["annual"]["payback"] = {"available": False, "reason": "Payback needs a full year, but only 11 of 12 months could be modelled."}
    text = page.render_results(unavailable, runs, runs[0]["id"])
    if "11 of 12" not in text:
        print("  ERROR: the reason payback is unavailable should be shown")
        failed = True
```

- [ ] **Step 2: Run and watch it fail**

```bash
cd coverage && ./run_all --test web_annual_results > /tmp/t.txt 2>&1; grep -E "  ERROR" /tmp/t.txt
```

- [ ] **Step 3: Add the fourth scenario to the palette**

```python
# Validated with the dataviz palette checker in BOTH light and dark mode, all pairs.
# Predbat's house chart trio (#2196F3/#FF9800/#4CAF50) FAILS here: green vs orange is
# only deltaE 3.6 under protanopia, so roughly one man in twelve could not tell
# "Without Predbat" from "With Predbat" - the exact comparison this chart exists to
# make. #9439ef was chosen for pv_only by sweeping 216 candidates through the same
# validator: the obvious Okabe-Ito picks (#E69F00, #56B4E9) fall outside the dark-mode
# lightness band, and #CC79A7/#AA4499 only reach deltaE 7.6/6.4 against a neighbour.
# Do not substitute without re-running the validator in both modes.
SCENARIO_COLOURS = {"no_pvbat": "#0072B2", "pv_only": "#9439ef", "without_predbat": "#D55E00", "with_predbat": "#009E73"}
SCENARIO_LABELS = {"no_pvbat": "No PV/Battery", "pv_only": "PV only", "without_predbat": "Without Predbat", "with_predbat": "With Predbat"}
SCENARIO_ORDER = ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]
```

The chart and month table both iterate `SCENARIO_ORDER`, so they pick this up without further change. Check `_render_month_table`'s `colspan` on the unavailable-month row still matches the column count.

- [ ] **Step 4: Render the costs and payback table**

Add a `_render_payback` method and call it from `render_results` immediately before `self._render_chart(results)`:

```python
    def _render_payback(self, results):
        """Return the install cost and payback table, or the reason there is none."""
        annual = results.get("annual") or {}
        costs = annual.get("costs") or {}
        payback = annual.get("payback") or {}
        if not costs:
            return ""

        text = "<h2>Cost and payback</h2>\n"
        text += "<p class='annual-note'>Estimated install cost for a {} kWp array and a {} kWh battery: <strong>£{:,.0f}</strong> (PV £{:,.0f} at £{:,.0f}/kWp, battery £{:,.0f}).</p>\n".format(
            costs.get("total_kwp", 0), costs.get("battery_kwh", 0), costs.get("total_gbp", 0), costs.get("pv_gbp", 0), costs.get("pv_rate_gbp_per_kwp", 0), costs.get("battery_gbp", 0)
        )

        if not payback.get("available"):
            reason = html.escape(str(payback.get("reason", "Payback could not be calculated.")), quote=True)
            text += "<p class='annual-unavailable'>{}</p>\n".format(reason)
            return text

        text += "<table class='comparison-table'>\n<tr><th>Option</th><th>Capital</th><th>Saving a year</th><th>Pays back in</th></tr>\n"
        for key, label in [("pv_only", "PV only"), ("pv_battery", "PV + battery"), ("pv_battery_predbat", "With Predbat")]:
            row = payback.get(key) or {}
            if row.get("pays_back"):
                years = "{:.1f} years".format(row.get("years", 0))
            else:
                years = "<span class='annual-unavailable'>does not pay back</span>"
            saving = "£{:,.0f}".format(row.get("annual_saving_gbp", 0))
            if row.get("predbat_annual_gbp"):
                saving += " <span class='annual-note'>(after £{:,.0f}/year for Predbat)</span>".format(row["predbat_annual_gbp"])
            text += "<tr><td>{}</td><td>£{:,.0f}</td><td>{}</td><td>{}</td></tr>\n".format(label, row.get("capital_gbp", 0), saving, years)
        text += "</table>\n"
        text += "<p class='annual-note'>Simple payback: capital divided by the modelled annual saving. It ignores panel degradation, price inflation, battery replacement and finance costs.</p>\n"
        return text
```

- [ ] **Step 5: Document it**

In `docs/annual-prediction.md`, add a "Cost and payback" subsection under the results section covering: the cost formulae and their defaults; that all seven values are editable under Advanced; that `predbat_annual_gbp` is recurring and defaults to zero; that payback needs a full twelve months; and that simple payback ignores degradation, inflation, replacement and finance. Also update the scenario list wherever the docs enumerate the three scenarios, to include `pv_only`.

- [ ] **Step 6: Run everything**

```bash
cd coverage && ./run_all > /tmp/full.txt 2>&1; echo "EXIT=$?"; grep -iE "FAILED|Some tests failed|All tests passed \(total" /tmp/full.txt | tail -3
```

Expected: all pass, exit 0.

- [ ] **Step 7: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py docs/annual-prediction.md
git add apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py docs/annual-prediction.md
git commit -m "feat(annual): show the PV-only scenario and a cost/payback table"
```
