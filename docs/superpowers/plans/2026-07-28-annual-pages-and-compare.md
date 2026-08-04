# Annual Pages, Run Comparison and Split Plan Storage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the Annual tab into three navigable pages (configure / view / compare), add a run-comparison table, and move captured debug plans out of the results document.

**Architecture:** The storage layer gains a per-run `summary` on the index (so the compare table never reads a run document) and splits captured plans into one key per leg (so `/annual_plan` reads ~177 KB rather than 4–8 MB). The engine, the CLI and the results-document schema are untouched — the split happens at `save_run`, the storage boundary. The web layer's single page handler becomes three, sharing a tab strip.

**Tech Stack:** Python 3, aiohttp, the Storage abstraction.

**Spec:** `docs/superpowers/specs/2026-07-28-annual-pages-and-compare-design.md`

## Global Constraints

- **Storage has no `delete` method.** `_discard_run` overwrites with `None` and a past expiry so `storage.cleanup()` can reclaim later, preferring `delete` when a backend provides one. Anything new that is stored must be discarded the same way.
- **Never read a run's figures from anywhere but that run.** The compare table and the run-details table both describe stored runs; taking a value from the live form or another run misattributes every figure on the row. This has already been a defect once on this branch.
- **A figure that cannot be computed is shown as "—" or "does not pay back", never as a number or a zero.** A zero reads as "free", which is the opposite of "unknown".
- **The engine, `annual_cli.py`, and the results-document schema do not change.** The engine emits plans embedded (that is the transport); only `save_run` splits them out.
- **Backward compatibility is required.** Runs stored before this change have plans embedded and no `summary`. They must stay viewable and comparable, not silently lose data.
- **The compare page must not write storage when nothing changed.** Backfill writes the index once per visit, and only if it actually filled something in.
- Line length 256 (Black) / 250 (Flake8); 100% docstrings (`interrogate`); British English (CSpell).
- Tests live in `apps/predbat/tests/`, registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`. **Always redirect test output to a file and grep the file** — never pipe straight to grep.
- **Registry names are not file names.** `./run_all --test web_annual` runs ONLY `test_web_annual`. Use `./run_all -k web_annual` / `-k annual`, and confirm new assertions ran by grepping the output file for their printed "Test: ..." lines. A green run does not prove your test executed.
- Run pre-commit as `coverage/venv/bin/pre-commit run --files <paths>`; `--all-files` silently skips untracked files.
- **Verify new tests discriminate.** Break the code the test covers, confirm the test fails, restore, and verify with `git diff` that nothing was left broken. Defects have shipped green on this branch three times because assertions matched broken code, and one implementer crashed leaving a deliberate mutation in production code.

---

### Task 1: Run summaries on the index

**Files:**
- Modify: `apps/predbat/annual_store.py`
- Test: `apps/predbat/tests/test_annual_store.py`

**Interfaces produced:**
- `build_summary(results, config) -> dict`
- `save_run` writes `summary` onto the index entry
- `backfill_summaries(storage, runs) -> list` — returns the runs with summaries filled, writing the index back at most once

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_annual_store.py`:

```python
    print("Test: build_summary lifts the headline figures off a results document")
    results = {
        "annual": {
            "scenarios": {"no_pvbat": {"cost_p": 180000.0}, "with_predbat": {"cost_p": 66000.0}},
            "months_included": 12,
            "costs": {"total_kwp": 5.6, "battery_kwh": 9.5},
            "payback": {
                "available": True,
                "pv_only": {"pays_back": True, "years": 17.8},
                "pv_battery": {"pays_back": True, "years": 13.61},
                "pv_battery_predbat": {"pays_back": False, "years": None},
            },
        }
    }
    config = {"tariff": {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE-24-10-01/x/"}}
    summary = build_summary(results, config)
    if summary["total_kwp"] != 5.6 or summary["battery_kwh"] != 9.5:
        print("  ERROR: the summary should carry the system size, got {}".format(summary))
        failed = True
    if summary["cost_with_predbat_p"] != 66000.0:
        print("  ERROR: expected the with_predbat cost, got {}".format(summary))
        failed = True
    if summary["saving_vs_none_p"] != 114000.0:
        print("  ERROR: saving should be no_pvbat minus with_predbat, got {}".format(summary))
        failed = True
    if summary["payback_years"]["pv_battery"] != 13.61:
        print("  ERROR: expected the pv_battery payback, got {}".format(summary))
        failed = True
    if summary["payback_years"]["pv_battery_predbat"] is not None:
        print("  ERROR: a row that does not pay back must carry None, not a number, got {}".format(summary))
        failed = True

    print("Test: a run with no usable month summarises as unknown rather than zero")
    empty = build_summary({"annual": {"scenarios": None, "months_included": 0}}, {})
    if empty["cost_with_predbat_p"] is not None or empty["saving_vs_none_p"] is not None:
        print("  ERROR: no usable month means unknown, not zero, got {}".format(empty))
        failed = True

    print("Test: an unavailable payback keeps its reason for the compare table to show")
    unavailable = build_summary({"annual": {"scenarios": {"no_pvbat": {"cost_p": 10.0}, "with_predbat": {"cost_p": 5.0}}, "months_included": 11, "payback": {"available": False, "reason": "Payback needs a full year, but only 11 of 12 months could be modelled."}}}, {})
    if unavailable["payback_years"] != {} or "11 of 12" not in (unavailable.get("payback_reason") or ""):
        print("  ERROR: an unavailable payback should carry its reason, got {}".format(unavailable))
        failed = True

    print("Test: save_run records the summary on the index entry")
    storage = FakeStorage()
    await_result = run_async(save_run(storage, results, config, "20260728-090000"))
    index = run_async(list_runs(storage))
    if not index or not index[0].get("summary"):
        print("  ERROR: save_run should record a summary, got {}".format(index))
        failed = True

    print("Test: backfill fills a summary-less entry from its document and writes the index back once")
    storage = FakeStorage()
    run_async(save_run(storage, results, config, "20260728-090100"))
    # Simulate a run stored before summaries existed.
    stale = run_async(list_runs(storage))
    stale[0].pop("summary", None)
    run_async(storage.save(STORAGE_MODULE, INDEX_NAME, stale, format="json"))
    writes_before = len(storage.save_calls)
    filled = run_async(backfill_summaries(storage, run_async(list_runs(storage))))
    if not filled[0].get("summary"):
        print("  ERROR: backfill should fill the missing summary, got {}".format(filled))
        failed = True
    if len(storage.save_calls) != writes_before + 1:
        print("  ERROR: backfill should write the index exactly once, got {} writes".format(len(storage.save_calls) - writes_before))
        failed = True

    print("Test: backfill writes nothing when every entry already has a summary")
    writes_before = len(storage.save_calls)
    run_async(backfill_summaries(storage, run_async(list_runs(storage))))
    if len(storage.save_calls) != writes_before:
        print("  ERROR: a compare page that changes nothing must not write storage, got {} writes".format(len(storage.save_calls) - writes_before))
        failed = True
```

Match the module's existing helpers for `FakeStorage` and `run_async` — read the file first and reuse what is there rather than adding new ones. `FakeStorage` must record `save_calls`; if the existing fake does not, add that to it.

The module currently imports `from annual_store import INDEX_NAME, MAX_RUNS, STORAGE_MODULE, build_label, list_runs, load_run, save_run` — extend that with `build_summary` and `backfill_summaries`.

- [ ] **Step 2: Run and watch it fail**

```bash
cd coverage && ./run_all -k annual_store > /tmp/t.txt 2>&1; grep -iE "  ERROR|ImportError|NameError" /tmp/t.txt
```

Expected: `ImportError` for `build_summary`.

- [ ] **Step 3: Implement `build_summary`**

In `apps/predbat/annual_store.py`:

```python
def build_summary(results, config):
    """Return the headline figures the compare table shows for one run.

    Read once at save time and cached on the index entry, so comparing twenty runs
    reads one small index rather than twenty results documents - a debug run's
    document is several megabytes.

    Every figure is None when it could not be computed, never zero: the compare table
    renders None as "-" and a zero as a real cost, and confusing "we do not know" with
    "it costs nothing" is exactly the failure this tool avoids elsewhere.
    """
    annual = (results or {}).get("annual") or {}
    scenarios = annual.get("scenarios") or {}
    costs = annual.get("costs") or {}
    payback = annual.get("payback") or {}

    baseline = (scenarios.get("no_pvbat") or {}).get("cost_p")
    predbat = (scenarios.get("with_predbat") or {}).get("cost_p")

    payback_years = {}
    if payback.get("available"):
        for key in ["pv_only", "pv_battery", "pv_battery_predbat"]:
            row = payback.get(key) or {}
            # None for a row that does not pay back, so the table can say so rather
            # than print a number that does not exist.
            payback_years[key] = row.get("years") if row.get("pays_back") else None

    return {
        "total_kwp": costs.get("total_kwp"),
        "battery_kwh": costs.get("battery_kwh"),
        "tariff": _describe_tariff((config or {}).get("tariff") or {}),
        "cost_with_predbat_p": predbat,
        "saving_vs_none_p": (baseline - predbat) if (baseline is not None and predbat is not None) else None,
        "payback_years": payback_years,
        "payback_reason": None if payback.get("available") else payback.get("reason"),
        "months_included": annual.get("months_included", 0),
    }
```

- [ ] **Step 4: Record it at save time**

In `save_run`'s `entry` dict, add:

```python
        "summary": build_summary(results, config),
```

- [ ] **Step 5: Implement the backfill**

```python
async def backfill_summaries(storage, runs):
    """Return ``runs`` with any missing summary filled in, writing the index back once.

    A run stored before summaries existed has none. Rather than re-reading its document
    on every visit to the compare page - several megabytes for a debug run - its summary
    is computed once and persisted, so every later visit is index-only.

    The index is written at most once per call, and only when something was actually
    filled in: a compare page that changes nothing must not write storage at all.
    """
    if not storage or not runs:
        return runs or []

    filled = False
    for entry in runs:
        if entry.get("summary") or not entry.get("id"):
            continue
        results = await load_run(storage, entry["id"])
        if not isinstance(results, dict):
            continue
        entry["summary"] = build_summary(results, results.get("config") or {})
        filled = True

    if filled:
        await storage.save(STORAGE_MODULE, INDEX_NAME, runs, format="json")
    return runs
```

- [ ] **Step 6: Run the tests, then verify they discriminate**

```bash
cd coverage && ./run_all -k annual_store > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL" /tmp/t.txt
```

Then make `backfill_summaries` write the index unconditionally (move the `await storage.save` outside the `if filled`), confirm the "writes nothing" test FAILS, and restore. Verify with `git diff` that the restore is exact.

- [ ] **Step 7: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/annual_store.py apps/predbat/tests/test_annual_store.py
git add apps/predbat/annual_store.py apps/predbat/tests/test_annual_store.py
git commit -m "feat(annual): cache each run's headline figures on the index"
```

---

### Task 2: Split captured plans into their own storage keys

**Files:**
- Modify: `apps/predbat/annual_store.py`
- Modify: `apps/predbat/web_annual.py` (`html_annual_plan`, `html_annual_download`)
- Test: `apps/predbat/tests/test_annual_store.py`, `apps/predbat/tests/test_web_annual.py`

**Interfaces produced:**
- `_plan_key(run_id, month, index) -> str`
- `save_run` strips `plans` from the stored document and writes one key per leg, recording the keys on the index entry as `plan_keys`
- `load_plan(storage, run_id, month, index, scenario) -> dict | None`

**Why this task exists — the measurement:**

```
one captured plan:  44,312 bytes
   no car:   96 plans ->  4.3 MB per run
 with car:  192 plans ->  8.5 MB per run
```

`/annual_plan` currently reads that whole document to return one 44 KB plan, on every day/scenario click, and the results page reads it to render totals that ignore it.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_annual_store.py`:

```python
    print("Test: save_run strips the plans out of the stored document and keys them per leg")
    storage = FakeStorage()
    debug_results = {
        "annual": {"scenarios": {"no_pvbat": {"cost_p": 10.0}, "with_predbat": {"cost_p": 5.0}}, "months_included": 12},
        "months": [
            {"month": 1, "status": "ok", "plans": [
                {"day": "2025-01-08", "leg": "with_car", "scenarios": {"with_predbat": {"rows": [1]}}},
                {"day": "2025-01-08", "leg": "without_car", "scenarios": {"with_predbat": {"rows": [2]}}},
            ]},
            {"month": 2, "status": "ok", "plans": [{"day": "2025-02-10", "leg": "single", "scenarios": {"pv_only": {"rows": [3]}}}]},
        ],
    }
    run_async(save_run(storage, debug_results, {}, "20260728-091000"))
    stored = run_async(load_run(storage, "20260728-091000"))
    if any("plans" in month for month in stored["months"]):
        print("  ERROR: the stored document must not carry the plans, got {}".format(stored["months"]))
        failed = True
    index = run_async(list_runs(storage))
    if len(index[0].get("plan_keys") or []) != 3:
        print("  ERROR: three legs should produce three plan keys, got {}".format(index[0].get("plan_keys")))
        failed = True

    print("Test: load_plan returns the right scenario from its own key")
    plan = run_async(load_plan(storage, "20260728-091000", 1, 1, "with_predbat"))
    if plan != {"rows": [2]}:
        print("  ERROR: expected the without_car leg's plan, got {}".format(plan))
        failed = True

    print("Test: a non-debug run writes no plan keys at all")
    storage = FakeStorage()
    run_async(save_run(storage, {"annual": {"months_included": 12}, "months": [{"month": 1, "status": "ok"}]}, {}, "20260728-091100"))
    if run_async(list_runs(storage))[0].get("plan_keys"):
        print("  ERROR: a run with no plans should record no plan keys")
        failed = True

    print("Test: load_plan falls back to a document that still has its plans embedded")
    # A run stored before this split. It must stay viewable rather than silently losing
    # its plans.
    storage = FakeStorage()
    run_async(storage.save(STORAGE_MODULE, "run_legacy", {"months": [{"month": 3, "plans": [{"leg": "single", "scenarios": {"pv_only": {"rows": [9]}}}]}]}, format="json"))
    if run_async(load_plan(storage, "legacy", 3, 0, "pv_only")) != {"rows": [9]}:
        print("  ERROR: a legacy embedded-plans run should still resolve")
        failed = True

    print("Test: load_plan returns None rather than raising for anything it cannot resolve")
    for args in [("20260728-091000", 99, 0, "with_predbat"), ("20260728-091000", 1, 99, "with_predbat"), ("20260728-091000", 1, 0, "nope"), ("nosuchrun", 1, 0, "with_predbat")]:
        try:
            if run_async(load_plan(storage, *args)) is not None:
                print("  ERROR: {} should resolve to None".format(args))
                failed = True
        except Exception as error:
            print("  ERROR: {} raised {} instead of returning None".format(args, type(error).__name__))
            failed = True

    print("Test: discarding an evicted run expires its plan keys, not just its document")
    storage = FakeStorage()
    run_async(save_run(storage, debug_results, {}, "20260728-091200"))
    keys_before = [key for key in storage.store if "plans" in str(key)]
    run_async(_discard_run(storage, "20260728-091200", run_async(list_runs(storage))[0].get("plan_keys")))
    if any(storage.store.get(key) is not None for key in keys_before):
        print("  ERROR: an evicted run's plan keys should be discarded too")
        failed = True
```

Extend the test module's `annual_store` import with `load_plan` and `_discard_run`.

- [ ] **Step 2: Run and watch it fail**

```bash
cd coverage && ./run_all -k annual_store > /tmp/t.txt 2>&1; grep -iE "  ERROR|ImportError" /tmp/t.txt
```

- [ ] **Step 3: Implement the key and the split**

```python
def _plan_key(run_id, month, index):
    """Return the storage filename holding one leg's captured plans."""
    return "run_{}_plans_{:02d}_{}".format(run_id, int(month), int(index))
```

In `save_run`, before storing the document, move the plans out. Use a deep copy so the
caller's dict is not mutated — the web layer still holds it:

```python
    stored = copy.deepcopy(results) if isinstance(results, dict) else results
    plan_keys = []
    if isinstance(stored, dict):
        for month in stored.get("months") or []:
            if not isinstance(month, dict):
                continue
            plans = month.pop("plans", None)
            if not isinstance(plans, list):
                continue
            for index, leg in enumerate(plans):
                key = _plan_key(run_id, month.get("month", 0), index)
                await storage.save(STORAGE_MODULE, key, leg, format="json")
                plan_keys.append(key)
```

then store `stored` rather than `results`, and add `"plan_keys": plan_keys` to the index
entry.

- [ ] **Step 4: Implement `load_plan`**

```python
async def load_plan(storage, run_id, month, index, scenario):
    """Return one captured plan, or None when it cannot be resolved.

    Reads the single leg's own key - about 177 KB - rather than the whole results
    document, which for a debug run is several megabytes and would be re-read on every
    click of the plan viewer's day and scenario selectors.

    Falls back to a document that still carries its plans inline, so a run stored before
    they were split out stays viewable instead of silently losing them.

    Never raises: month, index and scenario arrive off an attacker-controlled query
    string, and every failure to resolve is a None the caller turns into a 404.
    """
    if not storage or not run_id or not scenario:
        return None
    try:
        month = int(month)
        index = int(index)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None

    leg = await storage.load(STORAGE_MODULE, _plan_key(run_id, month, index))
    if isinstance(leg, dict):
        scenarios = leg.get("scenarios")
        return scenarios.get(scenario) if isinstance(scenarios, dict) else None

    results = await load_run(storage, run_id)
    if not isinstance(results, dict):
        return None
    for entry in results.get("months") or []:
        if not isinstance(entry, dict) or entry.get("month") != month:
            continue
        plans = entry.get("plans")
        if not isinstance(plans, list) or index >= len(plans):
            return None
        candidate = plans[index]
        if not isinstance(candidate, dict):
            return None
        scenarios = candidate.get("scenarios")
        return scenarios.get(scenario) if isinstance(scenarios, dict) else None
    return None
```

- [ ] **Step 5: Discard the plan keys on eviction**

Give `_discard_run` a `plan_keys=None` parameter and expire each key the same way it
expires the document (prefer `delete`, else overwrite with `None` and a past expiry).
Pass the evicted entry's `plan_keys` at the call site in `save_run`.

- [ ] **Step 6: Point the web layer at it**

In `web_annual.py`, replace `html_annual_plan`'s body with a `load_plan` call:

```python
        plan = await load_plan(self._storage(), request.query.get("run", ""), request.query.get("month"), request.query.get("index"), request.query.get("scenario"))
        if plan is None:
            return web.json_response({"error": "plan not found"}, status=404)
        return web.json_response(plan)
```

Import `load_plan` from `annual_store`. Delete `_find_plan` and its test, which this
replaces — leaving a now-unused private method behind is dead code.

In `html_annual_download`'s docstring, state that the download is the results document as
stored, with captured plans held separately.

- [ ] **Step 7: Run the tests and verify discrimination**

```bash
cd coverage && ./run_all -k annual > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL|Traceback" /tmp/t.txt
```

Then remove the legacy fallback from `load_plan` (return `None` instead of reading the
document), confirm the legacy test FAILS, and restore. Verify the restore with `git diff`.

- [ ] **Step 8: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/annual_store.py apps/predbat/web_annual.py apps/predbat/tests/test_annual_store.py apps/predbat/tests/test_web_annual.py
git add apps/predbat/annual_store.py apps/predbat/web_annual.py apps/predbat/tests/
git commit -m "feat(annual): store captured plans per leg instead of inside the results"
```

---

### Task 3: Three pages and the tab strip

**Files:**
- Modify: `apps/predbat/web_annual.py` (`html_annual`, new `html_annual_view`, `render_nav`)
- Modify: `apps/predbat/web.py` (two new routes)
- Test: `apps/predbat/tests/test_web_annual.py`

**Interfaces produced:**
- `render_nav(current) -> str` where `current` is `"config"`, `"view"` or `"compare"`
- `GET /annual_view`, `GET /annual_compare`

- [ ] **Step 1: Write the failing tests**

```python
    print("Test: the config page shows the form and no results")
    page = make_page(my_predbat)
    config_html = run_async(page.html_annual(FakeRequest()))
    body = config_html.text
    if 'name="solar_kwp_0"' not in body:
        print("  ERROR: the config page should show the form")
        failed = True
    if "Annual totals for" in body or "What this run used" in body:
        print("  ERROR: the config page must not also render the results")
        failed = True

    print("Test: the viewer shows results and no form")
    view_html = run_async(page.html_annual_view(FakeRequest())).text
    if 'name="solar_kwp_0"' in view_html:
        print("  ERROR: the viewer page must not render the configuration form")
        failed = True

    print("Test: the nav marks the current page and disables the end arrows")
    nav = page.render_nav("config")
    if "annual-nav-current" not in nav:
        print("  ERROR: the nav should mark the current page")
        failed = True
    if "annual-nav-disabled" not in nav:
        print("  ERROR: the arrow at the first page should be disabled, not wrap")
        failed = True
    middle = page.render_nav("view")
    if "annual-nav-disabled" in middle:
        print("  ERROR: neither arrow should be disabled on the middle page")
        failed = True
    for target in ["./annual", "./annual_view", "./annual_compare"]:
        if target not in nav:
            print("  ERROR: the nav should link to {}".format(target))
            failed = True

    print("Test: the progress area is on every page, so a run stays visible when you navigate")
    for name, rendered in [("config", body), ("view", view_html)]:
        if "annual-progress" not in rendered:
            print("  ERROR: the {} page should carry the progress area".format(name))
            failed = True

    print("Test: a finished run sends the tab that started it to the viewer, not the form")
    if "'./annual_view'" not in page.render_script():
        print("  ERROR: run completion should navigate to the viewer page")
        failed = True
```

Reuse the module's existing `run_async` and `FakeRequest` helpers rather than adding new ones.

- [ ] **Step 2: Run and watch it fail**

```bash
cd coverage && ./run_all -k web_annual > /tmp/t.txt 2>&1; grep -iE "  ERROR|AttributeError" /tmp/t.txt
```

- [ ] **Step 3: Implement the nav**

```python
    # Ordered, because the arrows step through it. Not wrapped: "next" from the last page
    # landing back on the first reads as a misclick rather than a choice.
    NAV_PAGES = [("config", "Configure", "./annual"), ("view", "Results", "./annual_view"), ("compare", "Compare", "./annual_compare")]

    def render_nav(self, current):
        """Return the tab strip, marking the current page and disabling the end arrows."""
        names = [name for name, _, _ in self.NAV_PAGES]
        position = names.index(current) if current in names else 0
        text = '<div class="annual-nav">\n'
        previous = self.NAV_PAGES[position - 1][2] if position > 0 else None
        text += '<a class="annual-nav-arrow{}" href="{}">&#9664;</a>\n'.format("" if previous else " annual-nav-disabled", previous or "#")
        for name, label, href in self.NAV_PAGES:
            text += '<a class="annual-nav-tab{}" href="{}">{}</a>\n'.format(" annual-nav-current" if name == current else "", href, label)
        following = self.NAV_PAGES[position + 1][2] if position < len(self.NAV_PAGES) - 1 else None
        text += '<a class="annual-nav-arrow{}" href="{}">&#9654;</a>\n'.format("" if following else " annual-nav-disabled", following or "#")
        text += "</div>\n"
        return text
```

Add matching CSS to `render_css`: the current tab visually distinct, a disabled arrow at
reduced opacity with `pointer-events: none` so it cannot be followed.

- [ ] **Step 4: Split the page handlers**

`html_annual` keeps the form and progress and drops the results:

```python
        text = self.web.get_header("Predbat Annual")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_nav("config")
        text += self.render_form(config, errors=error)
        text += self.render_progress()
        text += self.render_script()
        text += "</body></html>\n"
```

Add `html_annual_view` carrying the results, built from the same pieces the old combined
handler used:

```python
    async def html_annual_view(self, request):
        """Render the results viewer for one stored run."""
        self.web.default_page = "./annual_view"
        text = self.web.get_header("Predbat Annual")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_nav("view")
        text += self.render_progress()
        storage = self._storage()
        runs = await list_runs(storage)
        selected = request.query.get("run") or (runs[0]["id"] if runs else None)
        results = await load_run(storage, selected) if selected else None
        text += self.render_results(results, runs, selected)
        text += self.render_script()
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)
```

`render_results` no longer needs its own `<hr>`/`Results` heading now the pages are
separate — remove those two, keeping the run-details table and everything below it.

**This breaks an existing test, deliberately.** `test_web_annual_results` currently asserts
`"annual-divider" not in html` is false and that `">Results<"` appears — those were added
when the form and results shared one page, and the divider existed to separate them. With
separate pages the divider is meaningless. Update that assertion to check the nav marks
the viewer as current instead, rather than deleting it outright: the thing worth asserting
is still "the user can tell which page they are on".

- [ ] **Step 5: Send a finished run to the viewer**

In `render_script`, change the completion navigation from `'./annual'` to `'./annual_view'`,
and the "view results" link the other tabs get likewise.

- [ ] **Step 6: Register the routes**

In `web.py`, beside the existing Annual routes:

```python
        app.router.add_get("/annual_view", self.annual_page.html_annual_view)
        app.router.add_get("/annual_compare", self.annual_page.html_annual_compare)
```

`html_annual_compare` does not exist until Task 4; add a placeholder returning
`self.render_nav("compare")` inside a minimal page so the route works, and replace its body
in Task 4.

- [ ] **Step 7: Run the tests**

```bash
cd coverage && ./run_all -k web_annual > /tmp/t.txt 2>&1; echo "EXIT=$?"; grep -iE "  ERROR|FAIL|Traceback" /tmp/t.txt
```

Also confirm the existing route-registration test covers the two new routes; update its
expected count if it asserts one.

- [ ] **Step 8: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/web.py apps/predbat/tests/test_web_annual.py
git add apps/predbat/web_annual.py apps/predbat/web.py apps/predbat/tests/test_web_annual.py
git commit -m "feat(annual): split the tab into configure, results and compare pages"
```

---

### Task 4: The compare page

**Files:**
- Modify: `apps/predbat/web_annual.py`
- Test: `apps/predbat/tests/test_web_annual.py`

**Interfaces consumed:** `build_summary`, `backfill_summaries` (Task 1); `render_nav` (Task 3).

- [ ] **Step 1: Write the failing tests**

```python
    print("Test: the compare table lists every run with its own figures")
    runs = [
        {"id": "20260728-0900", "label": "9.5 kWh battery, 5.6 kWp, Agile", "summary": {"total_kwp": 5.6, "battery_kwh": 9.5, "tariff": "Agile", "cost_with_predbat_p": 66000.0, "saving_vs_none_p": 114000.0, "payback_years": {"pv_only": 17.8, "pv_battery": 13.61, "pv_battery_predbat": 11.78}, "payback_reason": None, "months_included": 12}},
        {"id": "20260728-0800", "label": "20 kWh battery, 12 kWp, Cosy", "summary": {"total_kwp": 12.0, "battery_kwh": 20.0, "tariff": "Cosy", "cost_with_predbat_p": 40000.0, "saving_vs_none_p": 140000.0, "payback_years": {"pv_only": 9.1, "pv_battery": 8.2, "pv_battery_predbat": 7.0}, "payback_reason": None, "months_included": 12}},
    ]
    table = page.render_compare(runs, "20260728-0900")
    for expected in ["5.6", "9.5", "Agile", "13.6", "12", "20", "Cosy", "8.2"]:
        if expected not in table:
            print("  ERROR: the compare table should show {}, got {}".format(expected, table))
            failed = True

    print("Test: a run whose payback was unavailable shows a dash and its reason, not a number")
    unavailable = [{"id": "x", "label": "partial", "summary": {"total_kwp": 5.0, "battery_kwh": 9.0, "tariff": "Agile", "cost_with_predbat_p": 100.0, "saving_vs_none_p": 50.0, "payback_years": {}, "payback_reason": "Payback needs a full year, but only 11 of 12 months could be modelled.", "months_included": 11}}]
    text = page.render_compare(unavailable, "x")
    if "11 of 12" not in text:
        print("  ERROR: the reason payback is unavailable should be available to the user")
        failed = True
    if "0.0 years" in text or "None" in text:
        print("  ERROR: an unavailable payback must not render as a number, got {}".format(text))
        failed = True

    print("Test: a run that does not pay back says so rather than showing a number")
    never = [{"id": "y", "label": "never", "summary": {"total_kwp": 5.0, "battery_kwh": 9.0, "tariff": "Agile", "cost_with_predbat_p": 100.0, "saving_vs_none_p": -50.0, "payback_years": {"pv_only": None, "pv_battery": None, "pv_battery_predbat": None}, "payback_reason": None, "months_included": 12}}]
    if "does not pay back" not in page.render_compare(never, "y"):
        print("  ERROR: a non-paying-back run should say so")
        failed = True

    print("Test: the compare table is horizontally scrollable rather than widening the page")
    if "overflow-x" not in page.render_css():
        print("  ERROR: a nine-column table needs its own scroll container")
        failed = True

    print("Test: with no stored runs the compare page says so rather than showing an empty table")
    if "No runs" not in page.render_compare([], None):
        print("  ERROR: an empty compare page should explain itself")
        failed = True
```

- [ ] **Step 2: Run and watch it fail**

- [ ] **Step 3: Implement `render_compare`**

Render a `<div class="annual-compare-scroll">` wrapping a table with the nine columns from
the spec. For each run, read ONLY `run["summary"]` — never the live config or another run.
Formatting rules, matching the viewer:

- `cost_with_predbat_p` and `saving_vs_none_p` are pence; render via `self._pounds`, or `—` when None.
- A payback of `None` inside a populated `payback_years` means the option does not pay back → `does not pay back`.
- An empty `payback_years` means payback was unavailable → `—` with `payback_reason` as the cell's `title`.
- The row matching `selected_id` gets a `annual-compare-current` class; the label links to `./annual_view?run=<id>`.
- Escape every interpolated value with `html.escape(..., quote=True)`.

- [ ] **Step 4: Implement `html_annual_compare`**

```python
    async def html_annual_compare(self, request):
        """Render the run comparison table."""
        self.web.default_page = "./annual_compare"
        storage = self._storage()
        runs = await backfill_summaries(storage, await list_runs(storage))
        text = self.web.get_header("Predbat Annual")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_nav("compare")
        text += self.render_progress()
        text += self.render_compare(runs, request.query.get("run"))
        text += self.render_script()
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)
```

- [ ] **Step 5: Run the tests and verify discrimination**

Change `render_compare` to read the first run's summary for every row, confirm the
"every run with its own figures" test FAILS (the second row's 12 kWp / Cosy vanish), and
restore. Verify with `git diff`.

- [ ] **Step 6: Commit**

```bash
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py
git add apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py
git commit -m "feat(annual): add a page comparing stored runs and their paybacks"
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/annual-prediction.md`

- [ ] **Step 1: Document it**

Update the web-interface section for the three pages: what each holds, that the tabs and
arrows move between them, and that the arrows stop at the ends. Describe the compare table
and its columns, and say plainly that a dash means the figure could not be computed rather
than that it is zero.

Under "Debugging a run", note that captured plans are stored separately from the results
document — the results stay small, and a plan is fetched only when viewed — and that a run
downloaded as JSON is the results without the plans.

Run `cd coverage && ./run_all` (about 4 minutes) and confirm the whole suite is green.

- [ ] **Step 2: Commit**

```bash
coverage/venv/bin/pre-commit run --files docs/annual-prediction.md
git add docs/annual-prediction.md
git commit -m "docs(annual): describe the three pages and run comparison"
```
