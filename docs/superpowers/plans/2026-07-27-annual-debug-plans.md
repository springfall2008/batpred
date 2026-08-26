# Annual Debug Plans Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under an opt-in debug flag, retain each sampled day's plan JSON inside the annual results document so the web UI can render any scenario with Predbat's existing client-side plan renderer.

**Architecture:** `publish_html_plan(..., publish=False)` already returns `(html, raw_plan)` and writes nothing to Home Assistant; `raw_plan` is byte-for-byte the structure the `/plan` page hands to `renderPlanTable()` as `window.planData`. The engine captures that structure per scenario per sampled day into the month rows, and the Annual tab gains a plan viewer that fetches one plan at a time from a new route and renders it with `get_plan_css()` + `get_plan_renderer_js()`. No new rendering code, and no new files on disk — the plans travel inside the results document that already goes through Storage.

**Tech Stack:** Python 3, aiohttp, the existing `renderPlanTable` JavaScript in `web_helper.py`.

## Global Constraints

- **Never re-read `apps.yaml`.** The Annual page reads live settings only via the predbat args dictionary; apps.yaml may not exist.
- **No direct file access.** Everything persists through the Storage abstraction (`components.get_component("storage")`). Storage has no `delete` method.
- **Debug is opt-in and off by default.** A non-debug run's results document must be byte-identical in shape to today's — no empty `plans` keys.
- **`pre-commit run --all-files` silently skips untracked files** (it uses `git ls-files`). Always `git add` new files first, or pass `--files` explicitly. Run it as `coverage/venv/bin/pre-commit`.
- **Line length 256 (Black) / 250 (Flake8); docstrings required on every function and class (`interrogate`, 100%); British English spelling (CSpell, `en-gb`).**
- Tests live in `apps/predbat/tests/`, are registered in `TEST_REGISTRY` in `coverage/unit_test.py`, and run from `coverage/` via `./run_all --test <name>`. **Always redirect test output to a file and grep the file afterwards.**
- Never leave `debug_enable = True` on the shared `my_predbat` fixture — `kernel_supported()` requires `not pred.debug_enable`, and leaking it makes every later test run pure Python (an 8× slowdown that has already caused one apparent hang). Restore it in a `finally`.

---

### Task 1: Capture plan JSON in the engine under a debug flag

**Files:**
- Modify: `apps/predbat/annual.py`
- Test: `apps/predbat/tests/test_annual_results.py`

**Interfaces:**
- Consumes: `publish_html_plan(pv_forecast_minute_step, pv_forecast_minute_step10, load_minutes_step, load_minutes_step10, end_record, publish=False, prediction=None) -> (html, raw_plan)` from `output.py:947`.
- Produces: `validate_config()` gains a `"debug"` boolean key. Month rows in the results document gain an optional `"plans"` list, described below.

**The results-document addition.** When and only when debug is on, each `ok`/`degraded` month row gains:

```python
"plans": [
    {
        "day": "2025-01-15",
        "leg": "single",              # or "with_car" / "without_car"
        "scenarios": {"no_pvbat": {...raw_plan...}, "without_predbat": {...}, "with_predbat": {...}},
    },
    ...
]
```

`leg` distinguishes the two legs `run_day()` blends when a car is configured: `"single"` when no car is configured (one `_run_scenarios()` call), otherwise `"with_car"` and `"without_car"`. The blended month figures come from both, so both must be inspectable.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_annual_results.py`:

```python
def test_annual_debug_flag():
    """Verify the debug flag defaults off and is coerced to a bool when set."""
    failed = False
    print("Test: debug defaults to False")
    config = validate_config(base_config())
    if config["debug"] is not False:
        print("  ERROR: debug should default to False, got {!r}".format(config["debug"]))
        failed = True

    print("Test: debug is coerced to a bool, so a form's 'on' string does not become a truthy string")
    raw = base_config()
    raw["debug"] = "on"
    config = validate_config(raw)
    if config["debug"] is not True:
        print("  ERROR: debug should coerce to True, got {!r}".format(config["debug"]))
        failed = True

    print("Test: debug is not echoed into the scrubbed raw config as a secret")
    if config["raw"].get("debug") != "on":
        print("  ERROR: raw config should retain the submitted value")
        failed = True
    return failed
```

Write `base_config()` as a module-level helper returning the same minimal valid config dict the existing `make_predictor()` uses (reuse its literal rather than inventing new values), and call `test_annual_debug_flag()` from the module's existing entry point, folding its result into the existing `failed` flag.

- [ ] **Step 2: Run it and watch it fail**

```bash
cd coverage && ./run_all --test annual_results > /tmp/t.txt 2>&1; grep -iE "ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: `KeyError: 'debug'`.

- [ ] **Step 3: Add the config flag**

In `validate_config()`'s returned dict in `apps/predbat/annual.py`, alongside `"samples_per_month"`:

```python
"debug": bool(raw.get("debug", False)),
```

- [ ] **Step 4: Add the capture helper**

Add above `_run_scenarios()` in `apps/predbat/annual.py`:

```python
def _capture_plan(predbat, pv_step, pv_step10, load_step, load_step10, end_record):
    """Return the current scenario's plan as the JSON structure the web plan renderer consumes.

    This is the same ``raw_plan`` the live ``/plan`` page renders from - ``publish_html_plan()``
    builds it from ``charge_limit_best``/``charge_window_best``/``export_*_best`` and the step
    data, exactly as they stand for the scenario just costed. ``publish=False`` keeps it from
    touching Home Assistant, so this is safe in a headless run: it only reads state and returns.
    """
    _, raw_plan = predbat.publish_html_plan(pv_step, pv_step10, load_step, load_step10, end_record, publish=False, prediction=predbat.prediction)
    return raw_plan
```

- [ ] **Step 5: Capture each scenario**

Give `_run_scenarios()` a trailing `plans=None` parameter (a dict the caller supplies when it wants plans; filled in place so the return type and `_blend_results()` are untouched). After each of the three `_billed_result(...)` calls, capture with the same series that scenario was costed against:

```python
    results["no_pvbat"] = _billed_result(predbat, DAY_MINUTES, zero_step)
    if plans is not None:
        plans["no_pvbat"] = _capture_plan(predbat, zero_step, zero_step, load_step, load_step, DAY_MINUTES)
```

```python
    results["without_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)
    if plans is not None:
        plans["without_predbat"] = _capture_plan(predbat, actual_step, actual_step, load_step, load_step, DAY_MINUTES)
```

```python
    results["with_predbat"] = _billed_result(predbat, DAY_MINUTES, actual_step)
    if plans is not None:
        plans["with_predbat"] = _capture_plan(predbat, actual_step, actual_step, predbat_load_step, predbat_load_step, DAY_MINUTES)
```

Each capture must use the same series passed to that scenario's `Prediction(...)` — a plan drawn against a different PV or load series than the one billed would defeat the entire point of the feature, which is cross-checking the billed numbers.

- [ ] **Step 6: Thread it through `run_day()`**

Give `run_day()` a trailing `plans=None` parameter. `plans`, when supplied, is a **list** the function appends leg entries to:

```python
    if car_kwh <= 0 or sessions_per_week <= 0:
        leg_plans = {} if plans is not None else None
        result = _run_scenarios(predbat, config, weather, tariff, load_source, day, midnight_utc, car_kwh=0.0, car_rate_kw=car_rate_kw, plans=leg_plans)
        if plans is not None:
            plans.append({"leg": "single", "scenarios": leg_plans})
        return result
```

Match the existing early-return's actual condition rather than copying the line above verbatim — read the current body of `run_day()` and preserve its logic exactly, adding only the capture. Do the same for the two-leg path, appending `{"leg": "with_car", ...}` and `{"leg": "without_car", ...}` in that order.

- [ ] **Step 7: Collect into the month rows**

In `AnnualPredictor.run()`'s sampled-day loop (around `annual.py:1293`), when `self.config["debug"]` is set, pass a fresh list per day and record the surviving days' plans. A day that raised is skipped by the existing `except` and must contribute no plans. Because `surviving_samples` and `day_results` are appended together after a successful `run_day()`, append the day's plan entries in the same place, tagging each with the day:

```python
                day_plans = [] if self.config["debug"] else None
                try:
                    result = run_day(self.predbat, self.config, self.weather, self.tariff, self.load_source, day, midnight_utc, plans=day_plans)
                except Exception as exc:  # noqa: BLE001 - one bad sample must not abort the whole year
                    ...unchanged...
                surviving_samples.append((day, weight))
                day_results.append(result)
                if day_plans is not None:
                    month_plans.extend(dict(entry, day=day.isoformat()) for entry in day_plans)
```

Initialise `month_plans = []` beside `day_results`, and add it to the month row only under debug:

```python
            row = {
                "month": month,
                ...unchanged...
            }
            if self.config["debug"]:
                row["plans"] = month_plans
            months.append(row)
```

A month that is dropped after `_reweight_survivors()` keeps its plans — the plans are diagnostics for what actually ran, not an input to the totals.

- [ ] **Step 8: Test the capture end to end**

Add to `apps/predbat/tests/test_annual_results.py` a test that runs `_run_scenarios()` through the real fixture with `plans={}` supplied and asserts all three scenario keys are populated with a dict carrying a non-empty `"rows"` list, and that passing `plans=None` leaves behaviour unchanged. If `_run_scenarios()` is too heavy to call directly in a unit test, assert instead against `_capture_plan()` driven by the `my_predbat` fixture with `charge_limit_best`/`charge_window_best`/`export_window_best`/`export_limits_best` set to empty lists and `predbat.prediction` left as the fixture leaves it — the assertion that matters is that the returned structure has `rows`, `soc_max` and `end_record` keys, which is what `renderPlanTable` requires.

- [ ] **Step 9: Run the annual tests**

```bash
cd coverage && ./run_all --test annual_results --test annual_cli --test annual_bootstrap --test annual_integration > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: all pass, exit 0.

- [ ] **Step 10: Commit**

```bash
git add apps/predbat/annual.py apps/predbat/tests/test_annual_results.py
git commit -m "feat(annual): retain each sampled day's plan JSON under a debug flag"
```

---

### Task 2: Render the captured plans in the Annual tab

**Files:**
- Modify: `apps/predbat/web_annual.py`, `apps/predbat/web.py`
- Test: `apps/predbat/tests/test_web_annual.py`

**Interfaces:**
- Consumes: the `"plans"` month-row key from Task 1; `get_plan_css()` and `get_plan_renderer_js()` from `web_helper.py`; `renderPlanTable(jsonData, overrides, showDebug, editable)` defined inside `get_plan_renderer_js()`.
- Produces: a new route `./annual_plan`.

**The renderer's contract, verified against `web_helper.py:6370`:**
- `jsonData` is the `raw_plan` structure; it returns an error paragraph if `jsonData.rows` is missing.
- `overrides` is dereferenced immediately as `overrides.manual_charge_times.concat(overrides.manual_export_times, overrides.manual_freeze_charge_times, overrides.manual_freeze_export_times, overrides.manual_demand_times)` — so it must be an object with **all five** of those arrays present. Passing `{}` throws.
- `editable` must be `false`: a historical plan has nothing to edit and the edit dropdowns post to the live plan's override routes.
- It **returns** an HTML string; it does not insert into the DOM itself.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_web_annual.py`, following the file's existing print-and-flag style:

```python
    print("Test: the form offers a debug checkbox, defaulting off")
    form = page.render_form(page.prefill_config())
    if 'name="debug"' not in form:
        print("  ERROR: the form should offer a debug checkbox")
        failed = True
    if re.search(r'name="debug"[^>]*checked', form):
        print("  ERROR: debug must default to off")
        failed = True

    print("Test: a submitted debug checkbox becomes a true config flag")
    postdata = valid_postdata()
    postdata["debug"] = "on"
    config = page.config_from_post(postdata)
    if config.get("debug") is not True:
        print("  ERROR: debug should be True when checked, got {!r}".format(config.get("debug")))
        failed = True

    print("Test: an unchecked debug box is False, not absent")
    config = page.config_from_post(valid_postdata())
    if config.get("debug") is not False:
        print("  ERROR: debug should be False when unchecked, got {!r}".format(config.get("debug")))
        failed = True
```

And a test for the viewer against a results document carrying one plan:

```python
    print("Test: a run with captured plans offers a plan viewer")
    debug_results = copy.deepcopy(results)
    debug_results["months"][0]["plans"] = [{"day": "2025-01-15", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}}]
    html_text = page.render_results(debug_results, runs, runs[0]["id"])
    if "annual-plan-viewer" not in html_text:
        print("  ERROR: a run with plans should render the plan viewer")
        failed = True
    if "renderPlanTable" not in html_text:
        print("  ERROR: the viewer must use the existing plan renderer")
        failed = True

    print("Test: a run with no captured plans renders no viewer")
    html_text = page.render_results(results, runs, runs[0]["id"])
    if "annual-plan-viewer" in html_text:
        print("  ERROR: a non-debug run must not show an empty plan viewer")
        failed = True
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd coverage && ./run_all --test web_annual > /tmp/t.txt 2>&1; grep -iE "ERROR|FAIL|Traceback" /tmp/t.txt
```

- [ ] **Step 3: Add the debug checkbox**

In `render_form()`, in the same fieldset as `samples_per_month`, add a checkbox named `debug`, checked from `config.get("debug")`, labelled "Save plans for debugging" with the help text "Keeps each sampled day's plan so you can inspect it below. Makes the saved run much larger." In `config_from_post()`, set `"debug": postdata.get("debug") is not None` — matching how the existing `battery_hybrid` checkbox is read (a checkbox absent from postdata means unchecked).

- [ ] **Step 4: Add the plan route**

In `web.py`, beside the other five Annual routes, register:

```python
        app.router.add_get("/annual_plan", self.annual_page.html_annual_plan)
```

In `web_annual.py`, add the handler. It takes `run`, `month`, `index` and `scenario` query parameters, loads the stored run, walks to `months[?]["plans"][index]["scenarios"][scenario]`, and returns it as JSON — 404 with a JSON error body if any step is missing:

```python
    async def html_annual_plan(self, request):
        """Return one captured plan as JSON, for the results page's plan viewer to render."""
        run_id = request.query.get("run", "")
        results = await load_run(self.base, run_id) if run_id else None
        if not results:
            return web.json_response({"error": "run not found"}, status=404)
        plan = self._find_plan(results, request.query.get("month"), request.query.get("index"), request.query.get("scenario"))
        if plan is None:
            return web.json_response({"error": "plan not found"}, status=404)
        return web.json_response(plan)
```

Write `_find_plan(results, month, index, scenario)` as a pure function that coerces `month` and `index` with a `try`/`except (TypeError, ValueError)` returning `None` — the query string is attacker-controlled and must never raise out of the handler. Match the surrounding file's import style for `web.json_response` (`from aiohttp import web`).

- [ ] **Step 5: Render the viewer**

In `render_results()`, after the month table, when any month row carries a non-empty `plans` list, emit a `<div class='annual-plan-viewer'>` containing:

- three `<select>` elements: day (label `"{day} ({leg})"`, value `"{month}:{index}"` built from the row's month and the plan's position in that row's list), scenario (the three `SCENARIO_LABELS`), and a "Show debug columns" checkbox;
- an empty `<div id='annual-plan-container'></div>`;
- `get_plan_css()` and `get_plan_renderer_js()`, and a script that fetches and renders:

```javascript
const ANNUAL_EMPTY_OVERRIDES = {
    manual_charge_times: [], manual_export_times: [], manual_freeze_charge_times: [],
    manual_freeze_export_times: [], manual_demand_times: [],
    manual_import_rates: [], manual_export_rates: [], manual_load_adjust: [], manual_soc: []
};
async function annualLoadPlan() {
    const [month, index] = document.getElementById('annual-plan-day').value.split(':');
    const scenario = document.getElementById('annual-plan-scenario').value;
    const showDebug = document.getElementById('annual-plan-debug').checked;
    const container = document.getElementById('annual-plan-container');
    const params = new URLSearchParams({run: ANNUAL_RUN_ID, month: month, index: index, scenario: scenario});
    try {
        const response = await fetch('./annual_plan?' + params.toString());
        if (!response.ok) { container.innerHTML = '<p>That plan is not available.</p>'; return; }
        container.innerHTML = renderPlanTable(await response.json(), ANNUAL_EMPTY_OVERRIDES, showDebug, false);
    } catch (error) {
        container.innerHTML = '<p>Could not load that plan.</p>';
    }
}
```

Emit `ANNUAL_RUN_ID` with `json.dumps(selected_id)` so the id is escaped rather than interpolated raw, and wire all three controls to `annualLoadPlan()` via `onchange`. Call it once on load so the first plan appears without interaction. Every value interpolated into an HTML attribute must go through `html.escape(..., quote=True)`, as the rest of this file already does.

- [ ] **Step 6: Run the tests**

```bash
cd coverage && ./run_all --test web_annual --test annual_results > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "ERROR|FAIL|Traceback" /tmp/t.txt
```

Expected: all pass, exit 0.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/web_annual.py apps/predbat/web.py apps/predbat/tests/test_web_annual.py
git commit -m "feat(annual): view a debug run's captured plans with the existing plan renderer"
```

---

### Task 3: Document the debug mode

**Files:**
- Modify: `docs/annual-prediction.md`

- [ ] **Step 1: Document it**

Add a "Debugging a run" section after "Reading the results" covering: what the checkbox (and the config's `debug: true`) does; that each sampled day's plan is kept for all three scenarios, and for both the with-car and without-car legs when a car is configured; that the viewer is the same renderer as the live `/plan` page; and that a debug run's saved results are substantially larger, so the flag is off by default. State plainly that the plans are the ones actually billed — the same charge/export windows and the same PV and load series — which is what makes them usable for cross-checking a suspicious figure.

- [ ] **Step 2: Commit**

```bash
git add docs/annual-prediction.md
git commit -m "docs(annual): document the debug plan capture and viewer"
```
