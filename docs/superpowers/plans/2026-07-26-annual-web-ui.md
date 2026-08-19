# Annual Prediction Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an **Annual** tab to the Predbat web interface — a form that prefills from the live instance, a subprocess run with a real progress bar, and results as annual totals plus a grouped monthly bar chart, with the last five runs kept in Storage.

**Architecture:** The tab spawns `annual_cli.py --machine` as a child process; results come back as JSON on stdout and progress as JSON-per-line on stderr, so the child needs no filesystem and no reach into the parent's Storage. `annual_job.py` owns the process lifecycle and knows no HTML; `web_annual.py` owns the HTML and knows no process handling; `annual_store.py` owns the five-run ring.

**Tech Stack:** Python 3, aiohttp (already the web server), `asyncio.create_subprocess_exec`, ApexCharts (already loaded from CDN by the existing chart pages), Predbat's Storage component.

**Spec:** `docs/superpowers/specs/2026-07-26-annual-web-ui-design.md`
**Engine spec (already built):** `docs/superpowers/specs/2026-07-25-annual-prediction-tool-design.md`

## Global Constraints

- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage — every function *and* class, including test functions and nested helpers.
- **Spelling:** British English (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which a pre-commit hook auto-sorts — **re-stage it** after running pre-commit. `docs/*.md` is spell-checked; `docs/superpowers/` is not.
- **String formatting:** `"...".format(...)`, **not** f-strings, in `apps/predbat/*.py`.
- **Indent:** 4 spaces, never tabs.
- **File header:** every new `apps/predbat/*.py` starts with the five-line copyright block used by all existing modules.
- **Storage:** all persistence goes through the Storage component, never direct file access. Reached via `self.base.components.get_component("storage")`.
- **Never re-read `apps/predbat/apps.yaml` from disk.** Read configuration from the in-memory args dictionary via `get_arg()`. The file may not exist in some deployments.
- **Tests:** registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`; signature `def test_name(my_predbat):` returning truthy on failure. No network I/O. **No test may spawn the real engine** — `annual_job` is driven by a stub script.
- **`git add` new files BEFORE running pre-commit.** `pre-commit --all-files` enumerates via `git ls-files` and silently skips untracked files, producing a false pass. This has bitten this branch repeatedly.
- **Pre-commit:** the script is `coverage/run_pre_commit`, NOT the repo root. Prefer `coverage/venv/bin/pre-commit run --files <paths>`. A run reporting "files were modified by this hook" has **not** passed — re-stage and re-run until clean.
- **Report only what you ran.** Never state a check passed unless you executed it and read the output.
- **Tests run from `coverage/`:** `cd coverage && ./run_all --test <name> > /tmp/out.txt 2>&1`, then grep the FILE. Never pipe test output straight to grep.
- **The full annual suite takes ~95 seconds.** If it runs for many minutes, stop — that symptom previously meant a test leaked `debug_enable` onto the shared fixture, disabling the C++ prediction kernel.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/predbat/tariff_catalogue.py` | **New.** Curated tariff list as data, the Compare→annual key mapping, and the merge with a user's `compare_list`. |
| `apps/predbat/annual_job.py` | **New.** Subprocess lifecycle only: spawn, parse progress, track state, cancel, reap. No HTML, no Storage. |
| `apps/predbat/annual_store.py` | **New.** The five-run ring over a Storage object: save, list, load, evict, label generation. |
| `apps/predbat/web_annual.py` | **New.** The tab: prefill, config load/save, form HTML, results HTML, and the five route handlers. |
| `apps/predbat/annual_cli.py` | **Modify.** Add `--machine`: results JSON on stdout, progress JSON on stderr, no human table. |
| `apps/predbat/web.py` | **Modify.** Import, instantiate `AnnualPage`, register six routes. |
| `apps/predbat/web_helper.py` | **Modify.** One nav link beside `<a href='./compare'>Compare</a>`. |
| `apps/predbat/tests/test_annual_*.py` | **New.** One test module per new module. |
| `docs/annual-prediction.md` | **Modify.** Document the tab. |

### The validated chart palette

The three scenario colours are **not** free choice. Predbat's house chart trio
(`#2196F3`, `#FF9800`, `#4CAF50`) was measured with the `dataviz` skill's
validator and **fails**: green↔orange is ΔE 3.6 under protanopia, far below the
floor of 8 — meaning roughly 1 in 12 men cannot distinguish "Without Predbat"
from "With Predbat", the exact comparison this tool exists to make.

Use the Okabe-Ito trio below. It passes all five checks — lightness band, chroma
floor, CVD separation across all pairs, normal-vision floor, and contrast — in
**both** light and dark mode:

| Scenario | Colour |
|---|---|
| No PV/Battery | `#0072B2` (blue) |
| Without Predbat | `#D55E00` (vermillion) |
| With Predbat | `#009E73` (bluish green) |

Worst all-pairs CVD separation is ΔE 11.0 (deutan). Do not substitute
"Predbat-looking" colours without re-running
`node scripts/validate_palette.js "<hex,hex,hex>" --mode light --pairs all` and
the same for `--mode dark`.

---

## Task 1: Tariff catalogue

Pure data plus two small functions. No dependencies, so it goes first and later tasks can rely on it.

**Files:**
- Create: `apps/predbat/tariff_catalogue.py`
- Create: `apps/predbat/tests/test_tariff_catalogue.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tariff_catalogue.BUILTIN_TARIFFS` — list of `{"id", "name", "import_octopus_url", "export_octopus_url"}`
  - `tariff_catalogue.CUSTOM_ID` — the string `"custom"`
  - `tariff_catalogue.convert_compare_entry(entry) -> dict | None`
  - `tariff_catalogue.merged_catalogue(compare_list=None) -> list[dict]`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_tariff_catalogue.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the tariff catalogue used by the Annual tab's dropdown."""

from tariff_catalogue import BUILTIN_TARIFFS, CUSTOM_ID, convert_compare_entry, merged_catalogue


def test_tariff_catalogue(my_predbat):
    """Verify the built-in catalogue, the Compare key mapping, and the merge."""
    failed = False
    print("**** Testing tariff_catalogue ****")

    print("Test: every built-in entry has an id, a name and at least an import URL")
    if not BUILTIN_TARIFFS:
        print("  ERROR: the built-in catalogue is empty")
        failed = True
    seen_ids = set()
    for entry in BUILTIN_TARIFFS:
        for key in ["id", "name"]:
            if not entry.get(key):
                print("  ERROR: entry {} is missing '{}'".format(entry, key))
                failed = True
        if not entry.get("import_octopus_url") and not entry.get("rates_import"):
            print("  ERROR: entry {} has neither an import URL nor fixed rates".format(entry.get("id")))
            failed = True
        if entry.get("id") in seen_ids:
            print("  ERROR: duplicate id {}".format(entry.get("id")))
            failed = True
        seen_ids.add(entry.get("id"))

    print("Test: no built-in entry uses Compare's key names")
    for entry in BUILTIN_TARIFFS:
        for stale in ["rates_import_octopus_url", "rates_export_octopus_url"]:
            if stale in entry:
                print("  ERROR: entry {} still uses Compare's key '{}'".format(entry.get("id"), stale))
                failed = True

    print("Test: convert_compare_entry maps Compare's URL keys onto the engine's")
    converted = convert_compare_entry(
        {
            "id": "agile_agile",
            "name": "Agile import/Agile export",
            "rates_import_octopus_url": "https://example.com/import/",
            "rates_export_octopus_url": "https://example.com/export/",
        }
    )
    if converted is None:
        print("  ERROR: a valid Compare entry should convert")
        failed = True
    else:
        if converted.get("import_octopus_url") != "https://example.com/import/":
            print("  ERROR: import URL not mapped, got {}".format(converted))
            failed = True
        if converted.get("export_octopus_url") != "https://example.com/export/":
            print("  ERROR: export URL not mapped, got {}".format(converted))
            failed = True
        if "rates_import_octopus_url" in converted:
            print("  ERROR: the Compare key should not survive conversion")
            failed = True

    print("Test: fixed rate structures pass through unchanged")
    converted = convert_compare_entry({"id": "cap", "name": "Price cap", "rates_import": [{"rate": 24.86}], "rates_export": [{"rate": 4.1}]})
    if converted is None or converted.get("rates_import") != [{"rate": 24.86}]:
        print("  ERROR: fixed rates should pass through, got {}".format(converted))
        failed = True

    print("Test: an entry with no usable rate source is rejected rather than shown")
    if convert_compare_entry({"id": "current", "name": "Current Tariff"}) is not None:
        print("  ERROR: an entry with no rates should be rejected")
        failed = True
    if convert_compare_entry({"name": "No id"}) is not None:
        print("  ERROR: an entry with no id should be rejected")
        failed = True
    if convert_compare_entry("not a dict") is not None:
        print("  ERROR: a non-dict should be rejected rather than raising")
        failed = True

    print("Test: merged_catalogue with no user list returns the built-ins plus Custom")
    merged = merged_catalogue(None)
    if len(merged) != len(BUILTIN_TARIFFS) + 1:
        print("  ERROR: expected {} entries, got {}".format(len(BUILTIN_TARIFFS) + 1, len(merged)))
        failed = True
    if merged[-1]["id"] != CUSTOM_ID:
        print("  ERROR: Custom should be the last entry, got {}".format(merged[-1]))
        failed = True

    print("Test: a user's compare_list is merged in and does not clobber a built-in id")
    builtin_id = BUILTIN_TARIFFS[0]["id"]
    merged = merged_catalogue(
        [
            {"id": builtin_id, "name": "My override", "rates_import_octopus_url": "https://example.com/mine/"},
            {"id": "my_tariff", "name": "My tariff", "rates_import": [{"rate": 20.0}]},
        ]
    )
    ids = [entry["id"] for entry in merged]
    if ids.count(builtin_id) != 1:
        print("  ERROR: a user entry sharing a built-in id should not duplicate it, ids were {}".format(ids))
        failed = True
    if "my_tariff" not in ids:
        print("  ERROR: a new user entry should appear, ids were {}".format(ids))
        failed = True

    print("Test: a malformed user entry is skipped rather than breaking the dropdown")
    merged = merged_catalogue([{"junk": True}, None, "string", {"id": "ok", "name": "Ok", "rates_import": [{"rate": 5.0}]}])
    ids = [entry["id"] for entry in merged]
    if "ok" not in ids:
        print("  ERROR: the valid entry should survive alongside malformed ones, ids were {}".format(ids))
        failed = True

    return failed
```

- [ ] **Step 2: Register the test and run it to verify it fails**

Add to `apps/predbat/unit_test.py`, alongside the other `from tests.test_* import ...` lines:

```python
from tests.test_tariff_catalogue import test_tariff_catalogue
```

and to the `TEST_REGISTRY` list inside `main()`:

```python
        ("tariff_catalogue", test_tariff_catalogue, "Tariff catalogue tests", False),
```

Run: `cd coverage && ./run_all --test tariff_catalogue > /tmp/w1.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/w1.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'tariff_catalogue'`.

- [ ] **Step 3: Create `apps/predbat/tariff_catalogue.py`**

The entries are transcribed from the commented-out `compare_list` template in `apps/predbat/config/apps.yaml` (around lines 531-592), with Compare's key names mapped to the engine's.

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Curated tariff catalogue for the Annual prediction tab's dropdown.

The entries mirror the commented-out ``compare_list`` template in
``config/apps.yaml`` so a user does not have to hand-copy a YAML block to get a
realistic tariff. Compare's key names differ from the annual engine's, so the
mapping lives here and nowhere else.
"""

# Compare writes rates_import_octopus_url; the annual engine reads import_octopus_url
COMPARE_KEY_MAP = {
    "rates_import_octopus_url": "import_octopus_url",
    "rates_export_octopus_url": "export_octopus_url",
}

# Keys that mean the same thing in both and need no translation
PASSTHROUGH_KEYS = ["rates_import", "rates_export"]

# The dropdown's escape hatch: leaves the URL fields blank for a hand-entered tariff
CUSTOM_ID = "custom"

_OCTOPUS = "https://api.octopus.energy/v1/products"

BUILTIN_TARIFFS = [
    {"id": "cap_seg", "name": "Price cap import / SEG export", "rates_import": [{"rate": 24.86}], "rates_export": [{"rate": 4.1}]},
    {
        "id": "eon_next_drive",
        "name": "Eon Next Drive import / Fixed export",
        "rates_import": [{"rate": 6.7, "start": "00:00:00", "end": "07:00:00"}, {"rate": 24.86, "start": "07:00:00", "end": "00:00:00"}],
        "rates_export": [{"rate": 16.5}],
    },
    {
        "id": "igo_fixed",
        "name": "Intelligent GO import / Fixed export",
        "import_octopus_url": "{}/INTELLI-VAR-24-10-29/electricity-tariffs/E-1R-INTELLI-VAR-24-10-29-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "igo_agile",
        "name": "Intelligent GO import / Agile export",
        "import_octopus_url": "{}/INTELLI-VAR-24-10-29/electricity-tariffs/E-1R-INTELLI-VAR-24-10-29-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "go_fixed",
        "name": "GO import / Fixed export",
        "import_octopus_url": "{}/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "go_agile",
        "name": "GO import / Agile export",
        "import_octopus_url": "{}/GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "agile_fixed",
        "name": "Agile import / Fixed export",
        "import_octopus_url": "{}/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "agile_agile",
        "name": "Agile import / Agile export",
        "import_octopus_url": "{}/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "flux",
        "name": "Flux import / Flux export",
        "import_octopus_url": "{}/FLUX-IMPORT-23-02-14/electricity-tariffs/E-1R-FLUX-IMPORT-23-02-14-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": "{}/FLUX-EXPORT-23-02-14/electricity-tariffs/E-1R-FLUX-EXPORT-23-02-14-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
    },
    {
        "id": "cosy_fixed",
        "name": "Cosy import / Fixed export",
        "import_octopus_url": "{}/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": "{}/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "cosy_agile",
        "name": "Cosy import / Agile export",
        "import_octopus_url": "{}/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-{{dno_region}}/standard-unit-rates".format(_OCTOPUS),
        "export_octopus_url": "{}/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "snug_fixed",
        "name": "Snug import / Fixed export",
        "import_octopus_url": "{}/SNUG-24-11-07/electricity-tariffs/E-1R-SNUG-24-11-07-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        "export_octopus_url": "{}/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
    {
        "id": "iflux",
        "name": "Intelligent Flux import / export",
        "import_octopus_url": "{}/INTELLI-FLUX-IMPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-IMPORT-23-07-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
        # Not a copy-paste slip: the Octopus products API lists only
        # INTELLI-FLUX-IMPORT-23-07-14, and the export rates live under that same
        # product code. There is no INTELLI-FLUX-EXPORT product to point at.
        "export_octopus_url": "{}/INTELLI-FLUX-IMPORT-23-07-14/electricity-tariffs/E-1R-INTELLI-FLUX-IMPORT-23-07-14-{{dno_region}}/standard-unit-rates/".format(_OCTOPUS),
    },
]


def convert_compare_entry(entry):
    """Convert one Compare ``compare_list`` entry into the annual engine's shape.

    Returns None when the entry cannot be used - it is not a mapping, it has no
    id, or it carries no rate source at all. Compare allows an entry with neither
    (the 'current' pseudo-tariff, which means "whatever is configured"), and that
    has no meaning here, so it is dropped rather than offered as a broken choice.
    """
    if not isinstance(entry, dict):
        return None
    if not entry.get("id") or not entry.get("name"):
        return None

    converted = {"id": entry["id"], "name": entry["name"]}
    for source_key, target_key in COMPARE_KEY_MAP.items():
        if entry.get(source_key):
            converted[target_key] = entry[source_key]
    for key in PASSTHROUGH_KEYS:
        if entry.get(key):
            converted[key] = entry[key]

    if not converted.get("import_octopus_url") and not converted.get("rates_import"):
        return None
    return converted


def merged_catalogue(compare_list=None):
    """Return the dropdown's entries: built-ins, then the user's own, then Custom.

    A user entry sharing a built-in id replaces it rather than appearing twice -
    the user's own definition is the more specific one. Malformed entries are
    skipped so one bad line in apps.yaml cannot empty the dropdown.
    """
    catalogue = [dict(entry) for entry in BUILTIN_TARIFFS]
    by_id = {entry["id"]: index for index, entry in enumerate(catalogue)}

    for entry in compare_list or []:
        converted = convert_compare_entry(entry)
        if converted is None:
            continue
        if converted["id"] in by_id:
            catalogue[by_id[converted["id"]]] = converted
        else:
            by_id[converted["id"]] = len(catalogue)
            catalogue.append(converted)

    catalogue.append({"id": CUSTOM_ID, "name": "Custom - enter URLs below"})
    return catalogue
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test tariff_catalogue > /tmp/w1.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w1.txt`

Expected: no output.

- [ ] **Step 5: Run pre-commit and commit**

```bash
git add apps/predbat/tariff_catalogue.py apps/predbat/tests/test_tariff_catalogue.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/tariff_catalogue.py apps/predbat/tests/test_tariff_catalogue.py apps/predbat/unit_test.py
git commit -m "feat(annual): add the tariff catalogue for the Annual tab dropdown"
```

---

## Task 2: Machine mode for the CLI

The child process must hand results back over the pipe. `annual_cli.py` currently prints a human table to stdout and prose progress to stderr, so results cannot simply join stdout. One flag switches both streams together.

**Files:**
- Modify: `apps/predbat/annual_cli.py`
- Modify: `apps/predbat/tests/test_annual_cli.py`

**Interfaces:**
- Consumes: `annual.AnnualPredictor` (existing).
- Produces:
  - `annual_cli.make_progress(quiet, machine=False)` — returns a callback writing either `[3/12] msg` or `{"completed": 3, "total": 12, "message": "msg"}` to stderr
  - `--machine` flag: stdout carries the results document as one JSON object; the human table is suppressed

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_annual_cli.py` (and add `make_progress` to its `annual_cli` import line):

```python
def test_annual_cli_machine(my_predbat):
    """Verify machine mode emits JSON progress on stderr and nothing human on stdout."""
    import io
    import json
    import sys

    failed = False
    print("**** Testing annual CLI machine mode ****")

    print("Test: machine progress writes one JSON object per line to stderr")
    captured = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = captured
    try:
        progress = make_progress(quiet=False, machine=True)
        progress(3, 12, "Month 03/2025")
    finally:
        sys.stderr = original_stderr

    line = captured.getvalue().strip()
    try:
        parsed = json.loads(line)
    except ValueError:
        print("  ERROR: machine progress should be JSON, got {!r}".format(line))
        parsed = {}
        failed = True
    if parsed.get("completed") != 3 or parsed.get("total") != 12 or parsed.get("message") != "Month 03/2025":
        print("  ERROR: unexpected progress payload {}".format(parsed))
        failed = True

    print("Test: human progress is unchanged when machine mode is off")
    captured = io.StringIO()
    sys.stderr = captured
    try:
        progress = make_progress(quiet=False, machine=False)
        progress(3, 12, "Month 03/2025")
    finally:
        sys.stderr = original_stderr
    if "[3/12]" not in captured.getvalue():
        print("  ERROR: expected the human form, got {!r}".format(captured.getvalue()))
        failed = True

    print("Test: quiet still suppresses progress in both modes")
    if make_progress(quiet=True, machine=False) is not None:
        print("  ERROR: quiet should give no progress callback")
        failed = True
    if make_progress(quiet=True, machine=True) is not None:
        print("  ERROR: quiet should give no progress callback in machine mode either")
        failed = True

    return failed
```

Register it in `apps/predbat/unit_test.py`:

```python
from tests.test_annual_cli import test_annual_cli, test_annual_cli_machine
```

```python
        ("annual_cli_machine", test_annual_cli_machine, "Annual CLI machine mode tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test annual_cli_machine > /tmp/w2.txt 2>&1; grep -E "ERROR|TypeError|cannot import" /tmp/w2.txt`

Expected: FAIL — `make_progress()` does not yet accept `machine`.

- [ ] **Step 3: Add machine mode to `apps/predbat/annual_cli.py`**

Replace `make_progress` with:

```python
def make_progress(quiet, machine=False):
    """Return a progress callback writing to stderr, or None when quiet.

    Machine mode emits one JSON object per line so the parent process never has
    to parse prose - the human wording is free to change without breaking a
    caller. Progress always goes to stderr so stdout carries only the result,
    whichever mode is in use.
    """
    if quiet:
        return None

    if machine:

        def progress(completed, total, message):
            """Emit one JSON progress record to stderr."""
            sys.stderr.write(json.dumps({"completed": completed, "total": total, "message": message}) + "\n")
            sys.stderr.flush()

        return progress

    def progress(completed, total, message):
        """Report progress to stderr so stdout stays parseable."""
        sys.stderr.write("[{}/{}] {}\n".format(completed, total, message))
        sys.stderr.flush()

    return progress
```

Add the flag in `main()`, beside the existing arguments:

```python
    parser.add_argument("--machine", action="store_true", help="Emit results as JSON on stdout and progress as JSON on stderr, for a calling process")
```

Pass it through where the progress callback is built:

```python
        results = asyncio.run(predictor.run(progress=make_progress(args.quiet, machine=args.machine)))
```

Then replace the output section at the end of `main()` so machine mode emits JSON and suppresses the table:

```python
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(results, handle, indent=2)
        except (OSError, TypeError, ValueError) as error:
            sys.stderr.write("Could not write results to {}: {}\n".format(args.out, error))
            exit_code = 1

    if args.machine:
        # The parent reads exactly one JSON object from stdout; the human table would
        # corrupt it, so it is suppressed rather than merely reordered.
        json.dump(results, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(format_table(results))

    return exit_code
```

Ensure `import json` and `import sys` are present at the top of the file.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd coverage && ./run_all -k annual_cli > /tmp/w2.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w2.txt`

Expected: no output; both `annual_cli` and `annual_cli_machine` pass.

- [ ] **Step 5: Verify machine mode by hand**

```bash
cd coverage && echo "annual: {}" > /tmp/bad.yaml && ./venv/bin/python3 ../apps/predbat/annual_cli.py --config /tmp/bad.yaml --machine; echo "exit=$?"
```

Expected: a readable config error on stderr and `exit=2`, with **nothing** on stdout — a parent parsing stdout must not receive half a document on failure. Put the actual output in your report.

- [ ] **Step 6: Run pre-commit and commit**

```bash
git add apps/predbat/annual_cli.py apps/predbat/tests/test_annual_cli.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/annual_cli.py apps/predbat/tests/test_annual_cli.py apps/predbat/unit_test.py
git commit -m "feat(annual): add machine mode so a parent process can drive the CLI"
```

---

## Task 3: Subprocess job control

Owns the child process and nothing else — no HTML, no Storage, no knowledge of what the results mean. That isolation is what lets it be tested against a stub script rather than the real three-minute engine.

**Files:**
- Create: `apps/predbat/annual_job.py`
- Create: `apps/predbat/tests/test_annual_job.py`
- Create: `apps/predbat/tests/annual_stub.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `annual_job.AnnualJob(log)` with:
    - `async start(command) -> bool` — False if a run is already in progress
    - `async cancel() -> bool`
    - `status() -> dict` of `{state, completed, total, message, elapsed, error}`
    - `results` — the parsed results document once state is `complete`, else None
    - `state` — one of `idle`, `running`, `complete`, `failed`, `cancelled`

- [ ] **Step 1: Write the stub child process**

Create `apps/predbat/tests/annual_stub.py`. This stands in for `annual_cli.py` so no test ever spawns the real engine:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""A stand-in for annual_cli.py --machine, used to drive AnnualJob in tests.

Behaviour is chosen by argv[1] so one script covers every case the job control
has to survive, without ever running the real three-minute engine.
"""

import json
import sys
import time


def main():
    """Emit the behaviour named by argv[1] and exit with a matching code."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

    if mode == "ok":
        for step in range(1, 4):
            sys.stderr.write(json.dumps({"completed": step, "total": 3, "message": "step {}".format(step)}) + "\n")
            sys.stderr.flush()
        json.dump({"year": 2025, "months": [], "annual": {"months_included": 0}}, sys.stdout)
        return 0

    if mode == "garbage_progress":
        sys.stderr.write("not json at all\n")
        sys.stderr.write(json.dumps({"completed": 1, "total": 1, "message": "recovered"}) + "\n")
        sys.stderr.flush()
        json.dump({"year": 2025, "months": [], "annual": {"months_included": 0}}, sys.stdout)
        return 0

    if mode == "fail":
        sys.stderr.write("something went wrong\n")
        return 3

    if mode == "bad_output":
        sys.stdout.write("this is not json")
        return 0

    if mode == "hang":
        while True:
            time.sleep(0.1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write the failing test**

Create `apps/predbat/tests/test_annual_job.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's subprocess job control."""

import asyncio
import os
import sys

from annual_job import AnnualJob

STUB = os.path.join(os.path.dirname(__file__), "annual_stub.py")


def stub_command(mode):
    """Return the argv for the stub child in the given mode."""
    return [sys.executable, STUB, mode]


async def run_to_completion(job, mode, timeout=20):
    """Start the stub in the given mode and wait for the job to leave 'running'."""
    started = await job.start(stub_command(mode))
    waited = 0.0
    while job.state == "running" and waited < timeout:
        await asyncio.sleep(0.1)
        waited += 0.1
    return started


def test_annual_job(my_predbat):
    """Verify progress parsing, completion, failure, cancellation and refusal to double-run."""
    failed = False
    print("**** Testing annual_job ****")
    messages = []

    print("Test: a successful run parses progress and returns the results document")
    job = AnnualJob(log=messages.append)
    started = asyncio.run(run_to_completion(job, "ok"))
    if not started:
        print("  ERROR: start() should return True for a fresh job")
        failed = True
    if job.state != "complete":
        print("  ERROR: expected state 'complete', got {} ({})".format(job.state, job.status().get("error")))
        failed = True
    if job.status().get("completed") != 3 or job.status().get("total") != 3:
        print("  ERROR: final progress should be 3/3, got {}".format(job.status()))
        failed = True
    if (job.results or {}).get("year") != 2025:
        print("  ERROR: the results document should be parsed from stdout, got {}".format(job.results))
        failed = True

    print("Test: a malformed progress line does not crash the parser")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "garbage_progress"))
    if job.state != "complete":
        print("  ERROR: a garbage progress line should not fail the run, got {}".format(job.state))
        failed = True
    if job.status().get("message") != "recovered":
        print("  ERROR: parsing should recover after a bad line, got {}".format(job.status()))
        failed = True

    print("Test: a non-zero exit is reported as failed, with the child's stderr kept")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "fail"))
    if job.state != "failed":
        print("  ERROR: expected state 'failed', got {}".format(job.state))
        failed = True
    error_text = job.status().get("error") or ""
    if "something went wrong" not in error_text:
        print("  ERROR: the child's stderr should be reported, got {!r}".format(error_text))
        failed = True
    if "3" not in error_text:
        print("  ERROR: the exit code should be reported, got {!r}".format(error_text))
        failed = True

    print("Test: unparseable stdout is reported as failed rather than a silent empty result")
    job = AnnualJob(log=messages.append)
    asyncio.run(run_to_completion(job, "bad_output"))
    if job.state != "failed":
        print("  ERROR: unparseable output should fail the run, got {}".format(job.state))
        failed = True
    if job.results is not None:
        print("  ERROR: no results should be exposed after a parse failure, got {}".format(job.results))
        failed = True

    print("Test: a second start while running is refused, and cancel stops the child")

    async def double_start_then_cancel():
        """Start a hanging child, try to start another, then cancel."""
        job = AnnualJob(log=messages.append)
        first = await job.start(stub_command("hang"))
        await asyncio.sleep(0.5)
        second = await job.start(stub_command("hang"))
        cancelled = await job.cancel()
        waited = 0.0
        while job.state == "running" and waited < 10:
            await asyncio.sleep(0.1)
            waited += 0.1
        return first, second, cancelled, job

    first, second, cancelled, job = asyncio.run(double_start_then_cancel())
    if not first:
        print("  ERROR: the first start should succeed")
        failed = True
    if second:
        print("  ERROR: a second start while running must be refused")
        failed = True
    if not cancelled:
        print("  ERROR: cancel should report that it acted")
        failed = True
    if job.state != "cancelled":
        print("  ERROR: expected state 'cancelled', got {}".format(job.state))
        failed = True

    print("Test: a fresh job reports idle with no results")
    job = AnnualJob(log=messages.append)
    if job.state != "idle" or job.results is not None:
        print("  ERROR: a fresh job should be idle with no results, got {} / {}".format(job.state, job.results))
        failed = True
    if job.status().get("elapsed") != 0:
        print("  ERROR: an idle job should report zero elapsed, got {}".format(job.status()))
        failed = True

    return failed
```

Register it in `apps/predbat/unit_test.py`:

```python
from tests.test_annual_job import test_annual_job
```

```python
        ("annual_job", test_annual_job, "Annual subprocess job control tests", False),
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test annual_job > /tmp/w3.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/w3.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_job'`.

- [ ] **Step 4: Create `apps/predbat/annual_job.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Subprocess control for the Annual prediction run.

The annual engine is one to three minutes of synchronous CPU work - two to six
with a car - so running it inside the web server's event loop would freeze the
whole Predbat interface and the five minute optimiser loop that shares it. It
runs as a child process instead, handing progress back on stderr and the results
document on stdout.

This module owns the process and nothing else: no HTML, no Storage, no opinion
about what the results mean. That is what lets it be tested against a stub child
rather than the real engine.
"""

import asyncio
import json
import time

# How much of the child's stderr to keep for the failure message. Enough to carry
# a traceback, bounded so a chatty failure cannot grow without limit.
MAX_ERROR_LINES = 20

# Grace period between asking the child to stop and killing it outright
CANCEL_GRACE_SECONDS = 5.0


class AnnualJob:
    """Runs one annual prediction child process at a time and tracks its progress."""

    def __init__(self, log):
        """Create an idle job that logs through the supplied callable."""
        self.log = log
        self.state = "idle"
        self.completed = 0
        self.total = 0
        self.message = ""
        self.error = None
        self.results = None
        self.started_at = None
        self._process = None
        self._stderr_tail = []

    def status(self):
        """Return a JSON-serialisable snapshot for the polling endpoint."""
        elapsed = 0
        if self.started_at is not None:
            end = self.started_at if self.state == "idle" else time.time()
            elapsed = int(end - self.started_at)
        return {
            "state": self.state,
            "completed": self.completed,
            "total": self.total,
            "message": self.message,
            "elapsed": elapsed,
            "error": self.error,
        }

    async def start(self, command):
        """Spawn the child. Returns False when a run is already in progress.

        Refusing rather than queueing is deliberate: two annual runs on the same
        machine would compete for the same CPU and both would take twice as long.
        """
        if self.state == "running":
            self.log("Warn: Annual: a run is already in progress, refusing to start another")
            return False

        self.state = "running"
        self.completed = 0
        self.total = 0
        self.message = "Starting"
        self.error = None
        self.results = None
        self.started_at = time.time()
        self._stderr_tail = []

        try:
            self._process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as exception:
            self.state = "failed"
            self.error = "Could not start the annual run: {}".format(exception)
            self.log("Warn: Annual: {}".format(self.error))
            return False

        asyncio.ensure_future(self._supervise())
        return True

    async def cancel(self):
        """Ask the child to stop, killing it if it does not. Returns False if nothing was running."""
        if self.state != "running" or self._process is None:
            return False
        self.state = "cancelled"
        self.message = "Cancelled"
        try:
            self._process.terminate()
        except ProcessLookupError:
            return True
        try:
            await asyncio.wait_for(self._process.wait(), timeout=CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            self.log("Warn: Annual: the run did not stop when asked, killing it")
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
        return True

    async def _read_progress(self, stream):
        """Consume the child's stderr, updating progress and keeping a tail for errors."""
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            self._stderr_tail.append(text)
            if len(self._stderr_tail) > MAX_ERROR_LINES:
                self._stderr_tail.pop(0)
            try:
                record = json.loads(text)
            except ValueError:
                # Not a progress record - the child is allowed to write plain
                # warnings to stderr, and one bad line must not stop the parse.
                continue
            if isinstance(record, dict) and "completed" in record:
                self.completed = record.get("completed", self.completed)
                self.total = record.get("total", self.total)
                self.message = record.get("message", self.message)

    async def _supervise(self):
        """Read both streams to completion, then settle the final state."""
        process = self._process
        try:
            stdout_data, _ = await asyncio.gather(process.stdout.read(), self._read_progress(process.stderr))
            await process.wait()
        except (OSError, ValueError) as exception:
            self.state = "failed"
            self.error = "The annual run could not be read: {}".format(exception)
            self.log("Warn: Annual: {}".format(self.error))
            return

        if self.state == "cancelled":
            return

        tail = "\n".join(self._stderr_tail)

        if process.returncode != 0:
            self.state = "failed"
            self.error = "The annual run exited with code {}.\n{}".format(process.returncode, tail)
            self.log("Warn: Annual: {}".format(self.error))
            return

        try:
            self.results = json.loads(stdout_data.decode("utf-8", errors="replace"))
        except ValueError as exception:
            # A zero exit with unreadable output is worse than a crash: it would
            # otherwise render as an empty result that looks like a real answer.
            self.state = "failed"
            self.results = None
            self.error = "The annual run finished but its output could not be read: {}\n{}".format(exception, tail)
            self.log("Warn: Annual: {}".format(self.error))
            return

        self.state = "complete"
        self.message = "Complete"
        if self.total:
            self.completed = self.total
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_job > /tmp/w3.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w3.txt`

Expected: no output. If the cancel case hangs, check that `terminate()` is reached before `wait()` and that the stub's `hang` mode is actually being spawned.

- [ ] **Step 6: Run pre-commit and commit**

```bash
git add apps/predbat/annual_job.py apps/predbat/tests/test_annual_job.py apps/predbat/tests/annual_stub.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/annual_job.py apps/predbat/tests/test_annual_job.py apps/predbat/tests/annual_stub.py apps/predbat/unit_test.py
git commit -m "feat(annual): add subprocess job control for the Annual tab"
```

---

## Task 4: The run store

A five-run ring over a Storage object. Keeping the ring here rather than in the web layer means the eviction rule has one implementation and one test.

**Files:**
- Create: `apps/predbat/annual_store.py`
- Create: `apps/predbat/tests/test_annual_store.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: a Storage object exposing `async save(module, filename, data, format=...)`, `async load(module, filename)`.
- Produces:
  - `annual_store.MAX_RUNS` (5), `annual_store.STORAGE_MODULE` (`"annual"`), `annual_store.INDEX_NAME` (`"runs_index"`)
  - `annual_store.build_label(config) -> str`
  - `annual_store.save_run(storage, results, config, run_id) -> str` (async)
  - `annual_store.list_runs(storage) -> list[dict]` (async)
  - `annual_store.load_run(storage, run_id) -> dict | None` (async)

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_annual_store.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's five-run store."""

import asyncio

from annual_store import INDEX_NAME, MAX_RUNS, STORAGE_MODULE, build_label, list_runs, load_run, save_run


class FakeStorage:
    """An in-memory stand-in for the Storage component."""

    def __init__(self):
        """Start with nothing stored."""
        self.store = {}
        self.deleted = []

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a saved value."""
        self.store[(module, filename)] = data

    async def load(self, module, filename):
        """Return a stored value, or None."""
        return self.store.get((module, filename))

    async def delete(self, module, filename):
        """Remove a stored value and record that it happened."""
        self.deleted.append(filename)
        self.store.pop((module, filename), None)


def sample_results(cost):
    """Return a minimal results document with a distinguishing cost."""
    return {"year": 2025, "annual": {"scenarios": {"with_predbat": {"cost_p": cost}}, "months_included": 12}, "months": []}


def sample_config(size_kwh=9.5):
    """Return a minimal validated-shape config."""
    return {"battery": {"size_kwh": size_kwh}, "solar": [{"kwp": 5.6}], "tariff": {"import_octopus_url": "https://example.com/AGILE-24-10-01/x"}}


def test_annual_store(my_predbat):
    """Verify saving, listing, loading, eviction and label generation."""
    failed = False
    print("**** Testing annual_store ****")

    print("Test: a saved run appears in the index and can be loaded back")
    storage = FakeStorage()
    run_id = asyncio.run(save_run(storage, sample_results(100), sample_config(), "run-1"))
    if run_id != "run-1":
        print("  ERROR: save_run should return the id it was given, got {}".format(run_id))
        failed = True
    index = asyncio.run(list_runs(storage))
    if len(index) != 1 or index[0]["id"] != "run-1":
        print("  ERROR: expected one indexed run, got {}".format(index))
        failed = True
    loaded = asyncio.run(load_run(storage, "run-1"))
    if (loaded or {}).get("annual", {}).get("scenarios", {}).get("with_predbat", {}).get("cost_p") != 100:
        print("  ERROR: the loaded run should match what was saved, got {}".format(loaded))
        failed = True

    print("Test: the index is newest-first")
    asyncio.run(save_run(storage, sample_results(200), sample_config(), "run-2"))
    index = asyncio.run(list_runs(storage))
    if [entry["id"] for entry in index] != ["run-2", "run-1"]:
        print("  ERROR: expected newest first, got {}".format([entry["id"] for entry in index]))
        failed = True

    print("Test: a sixth run evicts the oldest AND deletes its stored document")
    storage = FakeStorage()
    for number in range(1, MAX_RUNS + 2):
        asyncio.run(save_run(storage, sample_results(number), sample_config(), "run-{}".format(number)))
    index = asyncio.run(list_runs(storage))
    if len(index) != MAX_RUNS:
        print("  ERROR: the ring should hold {} runs, got {}".format(MAX_RUNS, len(index)))
        failed = True
    if "run-1" in [entry["id"] for entry in index]:
        print("  ERROR: the oldest run should have been evicted from the index")
        failed = True
    if "run_run-1" not in storage.deleted:
        print("  ERROR: the evicted run's document should be deleted, deletions were {}".format(storage.deleted))
        failed = True
    if asyncio.run(load_run(storage, "run-1")) is not None:
        print("  ERROR: an evicted run should no longer load")
        failed = True

    print("Test: loading an unknown or missing run returns None rather than raising")
    if asyncio.run(load_run(storage, "does-not-exist")) is not None:
        print("  ERROR: an unknown run id should give None")
        failed = True

    print("Test: an index entry whose document is missing is reported, not rendered empty")
    storage = FakeStorage()
    asyncio.run(save_run(storage, sample_results(1), sample_config(), "orphan"))
    storage.store.pop((STORAGE_MODULE, "run_orphan"))
    if asyncio.run(load_run(storage, "orphan")) is not None:
        print("  ERROR: a missing document should give None so the caller can say so")
        failed = True

    print("Test: an empty store lists nothing rather than raising")
    if asyncio.run(list_runs(FakeStorage())) != []:
        print("  ERROR: an empty store should list no runs")
        failed = True

    print("Test: the label describes the configuration, not just a timestamp")
    label = build_label(sample_config(size_kwh=9.5))
    if "9.5" not in label or "5.6" not in label:
        print("  ERROR: the label should name the battery and array size, got {!r}".format(label))
        failed = True
    if "Agile" not in label:
        print("  ERROR: the label should name the tariff, got {!r}".format(label))
        failed = True

    print("Test: a label is still produced for a config with no battery or solar")
    label = build_label({"tariff": {"rates_import": [{"rate": 25.0}]}})
    if not label:
        print("  ERROR: a sparse config should still produce a label")
        failed = True

    print("Test: the index survives a corrupt stored value")
    storage = FakeStorage()
    storage.store[(STORAGE_MODULE, INDEX_NAME)] = "not a list"
    if asyncio.run(list_runs(storage)) != []:
        print("  ERROR: a corrupt index should read as empty rather than raising")
        failed = True

    return failed
```

Register it in `apps/predbat/unit_test.py`:

```python
from tests.test_annual_store import test_annual_store
```

```python
        ("annual_store", test_annual_store, "Annual run store tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test annual_store > /tmp/w4.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/w4.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'annual_store'`.

- [ ] **Step 3: Create `apps/predbat/annual_store.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Storage-backed history of annual prediction runs.

Keeps the most recent runs so a user can flip between "with a 5 kWh battery" and
"with a 10 kWh battery" without re-running either. Everything goes through the
Storage abstraction rather than the filesystem, because there may not be one.
"""

MAX_RUNS = 5
STORAGE_MODULE = "annual"
INDEX_NAME = "runs_index"


def _run_key(run_id):
    """Return the storage filename holding one run's results document."""
    return "run_{}".format(run_id)


def _describe_tariff(tariff):
    """Return a short human name for the tariff a run used."""
    if not isinstance(tariff, dict):
        return "tariff"
    url = tariff.get("import_octopus_url") or ""
    for name in ["AGILE", "INTELLI-FLUX", "INTELLI", "FLUX", "COSY", "SNUG", "GO"]:
        if name in url.upper():
            return name.title().replace("Intelli-Flux", "Intelligent Flux").replace("Intelli", "Intelligent Go")
    if tariff.get("rates_import"):
        return "fixed rates"
    return "tariff"


def build_label(config):
    """Return a short human label describing the configuration a run used.

    A selector listing five bare timestamps tells the user nothing about which
    run was which, which defeats the point of keeping more than one.
    """
    # Normalise once: `config or {}` is NOT enough, because any truthy non-dict
    # (a string, an int, a non-empty list - i.e. a corrupted stored document)
    # short-circuits past it and then raises on .get(). A label failure must never
    # take out the page the user needs in order to start a fresh run.
    config = config if isinstance(config, dict) else {}

    parts = []
    battery = config.get("battery")
    if isinstance(battery, dict) and battery.get("size_kwh"):
        parts.append("{}kWh battery".format(battery["size_kwh"]))
    else:
        parts.append("no battery")

    solar = config.get("solar")
    if solar:
        total_kwp = sum(array.get("kwp", 0) for array in solar if isinstance(array, dict))
        if total_kwp:
            parts.append("{}kWp".format(round(total_kwp, 2)))
    else:
        parts.append("no solar")

    parts.append(_describe_tariff(config.get("tariff")))
    return " · ".join(parts)


async def list_runs(storage):
    """Return the stored runs newest-first, or an empty list when there are none.

    A corrupt or unexpected index reads as empty rather than raising: the tab
    must still render so the user can start a fresh run.
    """
    if not storage:
        return []
    index = await storage.load(STORAGE_MODULE, INDEX_NAME)
    if not isinstance(index, list):
        return []
    return [entry for entry in index if isinstance(entry, dict) and entry.get("id")]


async def load_run(storage, run_id):
    """Return one run's results document, or None when it is unknown or missing."""
    if not storage or not run_id:
        return None
    return await storage.load(STORAGE_MODULE, _run_key(run_id))


async def save_run(storage, results, config, run_id):
    """Save a completed run and prune the ring to MAX_RUNS. Returns the run id.

    The evicted run's document is deleted as well as its index entry, so the ring
    cannot leak documents that nothing references.
    """
    if not storage:
        return run_id

    await storage.save(STORAGE_MODULE, _run_key(run_id), results, format="json")

    annual = results.get("annual", {}) if isinstance(results, dict) else {}
    entry = {
        "id": run_id,
        "timestamp": run_id,
        "label": build_label(config),
        "months_included": annual.get("months_included", 0),
        "status": "ok" if annual.get("months_included") else "empty",
    }

    index = await list_runs(storage)
    index = [existing for existing in index if existing.get("id") != run_id]
    index.insert(0, entry)

    for dropped in index[MAX_RUNS:]:
        if hasattr(storage, "delete"):
            await storage.delete(STORAGE_MODULE, _run_key(dropped["id"]))
    index = index[:MAX_RUNS]

    await storage.save(STORAGE_MODULE, INDEX_NAME, index, format="json")
    return run_id
```

- [ ] **Step 4: Check the Storage component actually has `delete`**

The fake in the test provides `delete`, and `save_run` guards with `hasattr`. Confirm what the real component offers:

Run: `grep -n "async def delete\|def delete" apps/predbat/storage.py`

If there is no `delete`, say so in your report and instead overwrite the evicted key with `None` (still through `storage.save`) so the ring does not leave a live document behind. Do not invent a filesystem call.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test annual_store > /tmp/w4.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w4.txt`

Expected: no output.

- [ ] **Step 6: Run pre-commit and commit**

```bash
git add apps/predbat/annual_store.py apps/predbat/tests/test_annual_store.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/annual_store.py apps/predbat/tests/test_annual_store.py apps/predbat/unit_test.py
git commit -m "feat(annual): add the five-run store for the Annual tab"
```

---

## Task 5: Prefill and config persistence

Reads what the live instance knows and fills the rest from a typical UK system. This is the task that makes the unconfigured case work, so its test is the acceptance criterion for that requirement.

**Files:**
- Create: `apps/predbat/web_annual.py`
- Create: `apps/predbat/tests/test_web_annual.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `tariff_catalogue.merged_catalogue`.
- Produces:
  - `web_annual.DEFAULT_CONFIG` — the typical-UK example as a dict
  - `web_annual.AnnualPage(web_interface)` with:
    - `prefill_config() -> dict`
    - `is_configured() -> bool`
    - `load_config() -> dict`
    - `save_config(config)`
    - `catalogue() -> list[dict]`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_web_annual.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's prefill and configuration handling."""

from annual import validate_config
from web import WebInterface
from web_annual import DEFAULT_CONFIG, AnnualPage


def make_page(my_predbat):
    """Return an AnnualPage backed by a WebInterface over the test fixture."""
    return AnnualPage(WebInterface(my_predbat, web_port=5054))


def test_web_annual(my_predbat):
    """Verify prefill against a configured and an unconfigured instance."""
    failed = False
    print("**** Testing web_annual prefill ****")

    saved_args = dict(my_predbat.args)
    try:
        print("Test: an unconfigured instance still produces a complete, valid config")
        # This is the acceptance criterion for "must work with Predbat unconfigured":
        # a prospective buyer, and eventually an unregistered Predbat.com visitor,
        # arrives with none of this set.
        for key in ["soc_max", "inverter_limit", "export_limit", "open_meteo_forecast", "forecast_solar", "compare_list", "dno_region"]:
            my_predbat.args.pop(key, None)
        page = make_page(my_predbat)
        config = page.prefill_config()
        try:
            validate_config(config)
        except Exception as error:
            print("  ERROR: an unconfigured prefill must still validate, got {}".format(error))
            failed = True
        if page.is_configured():
            print("  ERROR: with no battery and no solar the page should report unconfigured")
            failed = True
        if config["battery"]["size_kwh"] != DEFAULT_CONFIG["battery"]["size_kwh"]:
            print("  ERROR: battery should fall back to the default, got {}".format(config["battery"]))
            failed = True
        if not config["solar"]:
            print("  ERROR: solar should fall back to the default array")
            failed = True

        print("Test: a zero soc_max counts as unset and falls back to the default")
        my_predbat.args["soc_max"] = 0
        config = make_page(my_predbat).prefill_config()
        if config["battery"]["size_kwh"] != DEFAULT_CONFIG["battery"]["size_kwh"]:
            print("  ERROR: a zero soc_max should fall back, got {}".format(config["battery"]["size_kwh"]))
            failed = True

        print("Test: configured values are read from args and used")
        my_predbat.args["soc_max"] = 12.5
        my_predbat.args["open_meteo_forecast"] = [{"kwp": 7.2, "declination": 30, "azimuth": 170, "efficiency": 0.9}]
        config = make_page(my_predbat).prefill_config()
        if config["battery"]["size_kwh"] != 12.5:
            print("  ERROR: soc_max from args should be used, got {}".format(config["battery"]["size_kwh"]))
            failed = True
        if config["solar"][0]["kwp"] != 7.2 or config["solar"][0]["azimuth"] != 170:
            print("  ERROR: the solar array should come from args, got {}".format(config["solar"]))
            failed = True

        print("Test: prefill is per-field, not all-or-nothing")
        # Solar configured but no battery: the real array must survive alongside the
        # default battery rather than the whole prefill collapsing to defaults.
        my_predbat.args.pop("soc_max", None)
        config = make_page(my_predbat).prefill_config()
        if config["solar"][0]["kwp"] != 7.2:
            print("  ERROR: configured solar should survive an absent battery, got {}".format(config["solar"]))
            failed = True
        if config["battery"]["size_kwh"] != DEFAULT_CONFIG["battery"]["size_kwh"]:
            print("  ERROR: the battery should still fall back, got {}".format(config["battery"]))
            failed = True
        if not make_page(my_predbat).is_configured():
            print("  ERROR: a configured solar array alone should count as configured")
            failed = True

        print("Test: the catalogue merges the user's compare_list")
        my_predbat.args["compare_list"] = [{"id": "mine", "name": "My tariff", "rates_import_octopus_url": "https://example.com/x"}]
        ids = [entry["id"] for entry in make_page(my_predbat).catalogue()]
        if "mine" not in ids:
            print("  ERROR: a user compare_list entry should appear in the catalogue, got {}".format(ids))
            failed = True
        if "agile_agile" not in ids:
            print("  ERROR: built-in entries should still be present, got {}".format(ids))
            failed = True

        print("Test: the default config validates on its own")
        try:
            validate_config(DEFAULT_CONFIG)
        except Exception as error:
            print("  ERROR: DEFAULT_CONFIG must be valid, got {}".format(error))
            failed = True

    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)

    return failed
```

Register it in `apps/predbat/unit_test.py`:

```python
from tests.test_web_annual import test_web_annual
```

```python
        ("web_annual", test_web_annual, "Annual web tab prefill tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test web_annual > /tmp/w5.txt 2>&1; grep -E "ERROR|ModuleNotFound" /tmp/w5.txt`

Expected: FAIL with `ModuleNotFoundError: No module named 'web_annual'`.

- [ ] **Step 3: Create `apps/predbat/web_annual.py` with prefill and config handling**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""The Annual prediction tab.

Renders the configuration form, drives the subprocess that runs the annual
prediction engine, and presents the results. Prefills from whatever the live
Predbat instance knows and falls back to a typical UK system for the rest, so
the tab is usable by someone who has not configured Predbat at all - which is
the prospective-buyer path the tool exists to serve.
"""

import copy
import os

import yaml

from tariff_catalogue import merged_catalogue

# A plausible UK home, used for any field the live instance cannot supply. These
# are an EXAMPLE, not a recommendation - the form says so, because a visitor
# could otherwise mistake them for a reading of their own system.
DEFAULT_CONFIG = {
    "location": {"postcode": "SW1A 1AA"},
    "solar": [{"kwp": 5.0, "declination": 35, "azimuth": 180, "efficiency": 0.95}],
    "battery": {"size_kwh": 9.5, "inverter_kw": 5.0, "export_limit_kw": 5.0, "hybrid": True},
    "load": {"annual_kwh": 3800, "shape": "flat", "car_charging_kwh": 0, "car_rate_kw": 7.4},
    "tariff": {"rates_import": [{"rate": 24.86}], "rates_export": [{"rate": 4.1}], "standing_charge_p_per_day": 60.0},
    "samples_per_month": 2,
}

CONFIG_FILENAME = "annual.yaml"


class AnnualPage:
    """Renders and drives the Annual prediction tab."""

    def __init__(self, web_interface):
        """Attach to the running web interface so args and Storage are reachable."""
        self.web = web_interface
        self.base = web_interface.base
        self.log = web_interface.log

    def _arg(self, name, default=None, indirect=True):
        """Read one configuration value from the in-memory args dictionary.

        Pass indirect=False for any value that may contain a dot but is NOT an entity
        id - a URL, most obviously. resolve_arg would otherwise try to resolve it as a
        Home Assistant entity and hand back None.

        Pass a FLOAT default for any numeric field. get_arg() type-directs its
        coercion off the default's type, so an int default silently truncates a
        real float - soc_max 12.5 would come back as 12.

        Never reads apps.yaml from disk: the file may not exist at all in some
        deployments, which is exactly where the unconfigured case matters most.
        """
        try:
            return self.base.get_arg(name, default, indirect=indirect)
        except Exception:
            return default

    def _solar_from_args(self):
        """Return the configured solar arrays, or an empty list.

        open_meteo_forecast and forecast_solar are already lists of
        {kwp, declination, azimuth, efficiency}, which is the annual engine's own
        shape, so no translation is needed.
        """
        for name in ["open_meteo_forecast", "forecast_solar"]:
            configured = self._arg(name, None)
            if isinstance(configured, dict):
                configured = [configured]
            if isinstance(configured, list) and configured:
                arrays = []
                for entry in configured:
                    if not isinstance(entry, dict) or not entry.get("kwp"):
                        continue
                    arrays.append(
                        {
                            "kwp": entry.get("kwp"),
                            "declination": entry.get("declination", DEFAULT_CONFIG["solar"][0]["declination"]),
                            "azimuth": entry.get("azimuth", DEFAULT_CONFIG["solar"][0]["azimuth"]),
                            "efficiency": entry.get("efficiency", DEFAULT_CONFIG["solar"][0]["efficiency"]),
                        }
                    )
                if arrays:
                    return arrays
        return []

    def _location_from_args(self):
        """Return the configured location, taken from the solar entries if present."""
        for name in ["open_meteo_forecast", "forecast_solar"]:
            configured = self._arg(name, None)
            if isinstance(configured, dict):
                configured = [configured]
            for entry in configured or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("postcode"):
                    return {"postcode": entry["postcode"]}
                if entry.get("latitude") is not None and entry.get("longitude") is not None:
                    return {"latitude": entry["latitude"], "longitude": entry["longitude"]}
        return None

    def is_configured(self):
        """Return True when the live instance has a battery or a solar array.

        Those two are what signal a configured system; with neither, the form
        shows a banner saying the values on screen are examples.
        """
        battery_kwh = self._arg("soc_max", 0.0) or 0.0
        try:
            battery_kwh = float(battery_kwh)
        except (TypeError, ValueError):
            battery_kwh = 0
        return battery_kwh > 0 or bool(self._solar_from_args())

    def prefill_config(self):
        """Build a complete config from the live instance, filling gaps with the example.

        Every field falls back independently, so a half-configured Predbat gets its
        real values alongside example ones rather than all-or-nothing.
        """
        config = copy.deepcopy(DEFAULT_CONFIG)

        location = self._location_from_args()
        if location:
            config["location"] = location

        arrays = self._solar_from_args()
        if arrays:
            config["solar"] = arrays

        battery_kwh = self._arg("soc_max", 0.0) or 0.0
        try:
            battery_kwh = float(battery_kwh)
        except (TypeError, ValueError):
            battery_kwh = 0
        # A zero or absent soc_max means it is not set - fall back so the user can adjust
        if battery_kwh > 0:
            config["battery"]["size_kwh"] = battery_kwh

        for arg_name, field, divisor in [("inverter_limit", "inverter_kw", 1000.0), ("export_limit", "export_limit_kw", 1000.0)]:
            watts = self._arg(arg_name, 0.0) or 0.0
            try:
                watts = float(watts)
            except (TypeError, ValueError):
                watts = 0
            if watts > 0:
                config["battery"][field] = round(watts / divisor, 2)

        inverter_type = self._arg("inverter_type", None)
        if inverter_type:
            config["battery"]["hybrid"] = True

        # indirect=False is REQUIRED here. resolve_arg (userinterface.py:149) treats any
        # dotted string as a Home Assistant entity id and looks it up, and a URL is full
        # of dots - so with the default indirect=True the URL resolves to None and the
        # prefill silently does nothing. Every other call site in the codebase passes
        # indirect=False for these two args; see compare.py:75 and fetch.py:848.
        import_url = self._arg("rates_import_octopus_url", None, indirect=False)
        export_url = self._arg("rates_export_octopus_url", None, indirect=False)
        if import_url:
            config["tariff"] = {"import_octopus_url": import_url, "standing_charge_p_per_day": DEFAULT_CONFIG["tariff"]["standing_charge_p_per_day"]}
            if export_url:
                config["tariff"]["export_octopus_url"] = export_url

        dno_region = self._arg("dno_region", None)
        if dno_region:
            config["tariff"]["dno_region"] = dno_region

        return config

    def catalogue(self):
        """Return the tariff dropdown entries: built-ins merged with the user's own."""
        return merged_catalogue(self._arg("compare_list", None))

    def _config_path(self):
        """Return the path of the saved annual configuration."""
        return os.path.join(self.base.config_root, CONFIG_FILENAME)

    def load_config(self):
        """Return the saved configuration, or a fresh prefill when none exists."""
        path = self._config_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    saved = yaml.safe_load(handle)
                if isinstance(saved, dict) and saved:
                    return saved.get("annual", saved)
        except (OSError, yaml.YAMLError) as error:
            self.log("Warn: Annual: could not read {}: {}".format(path, error))
        return self.prefill_config()

    def save_config(self, config):
        """Write the configuration so the CLI subprocess can consume it directly."""
        path = self._config_path()
        try:
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"annual": config}, handle, default_flow_style=False, allow_unicode=True)
        except OSError as error:
            self.log("Warn: Annual: could not write {}: {}".format(path, error))
            raise
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test web_annual > /tmp/w5.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w5.txt`

Expected: no output. If `WebInterface(my_predbat, web_port=5054)` raises, check how `tests/test_web_functions.py` constructs it and match that.

- [ ] **Step 5: Run pre-commit and commit**

```bash
git add apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
git commit -m "feat(annual): add Annual tab prefill and config persistence"
```

---

## Task 6: The form

**Files:**
- Modify: `apps/predbat/web_annual.py`
- Modify: `apps/predbat/tests/test_web_annual.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `AnnualPage.prefill_config`, `load_config`, `catalogue`, `is_configured` from Task 5; `tariff_catalogue.CUSTOM_ID`.
- Produces:
  - `AnnualPage.render_form(config, errors=None) -> str`
  - `AnnualPage.config_from_post(postdata) -> dict`

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_web_annual.py` (add `CUSTOM_ID` to the imports from `tariff_catalogue`):

```python
def test_web_annual_form(my_predbat):
    """Verify the form renders every group, reflects config, and round-trips a post."""
    failed = False
    print("**** Testing web_annual form ****")

    saved_args = dict(my_predbat.args)
    try:
        for key in ["soc_max", "open_meteo_forecast", "forecast_solar"]:
            my_predbat.args.pop(key, None)
        page = make_page(my_predbat)
        config = page.prefill_config()
        html = page.render_form(config)

        print("Test: every configuration group is present")
        for heading in ["Location", "Solar", "Battery", "Load", "Tariff", "Advanced"]:
            if heading not in html:
                print("  ERROR: the form is missing the '{}' group".format(heading))
                failed = True

        print("Test: an unconfigured instance gets the example-values banner")
        if "example values" not in html.lower():
            print("  ERROR: an unconfigured instance should be told these are examples")
            failed = True

        print("Test: a configured instance does NOT get the banner")
        my_predbat.args["soc_max"] = 10.0
        configured_html = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        if "example values" in configured_html.lower():
            print("  ERROR: a configured instance should not be told its values are examples")
            failed = True
        my_predbat.args.pop("soc_max", None)

        print("Test: the tariff dropdown lists the catalogue and a Custom entry")
        if CUSTOM_ID not in html:
            print("  ERROR: the dropdown should offer a Custom entry")
            failed = True
        if "Agile import / Agile export" not in html:
            print("  ERROR: the dropdown should list the built-in tariffs")
            failed = True

        print("Test: the load source is a radio pair, not two independent sections")
        if html.count('type="radio"') < 2:
            print("  ERROR: expected a radio pair for the load source")
            failed = True
        if "octopus" not in html.lower():
            print("  ERROR: the Octopus load option should be offered")
            failed = True

        print("Test: current values are rendered into the inputs")
        if 'value="3800"' not in html.replace("'", '"'):
            print("  ERROR: the annual kWh value should appear in the form")
            failed = True

        print("Test: validation errors are shown with the form still populated")
        html_with_error = page.render_form(config, errors="annual.solar[0] is missing kwp")
        if "annual.solar[0] is missing kwp" not in html_with_error:
            print("  ERROR: the error message should be displayed")
            failed = True
        if 'value="3800"' not in html_with_error.replace("'", '"'):
            print("  ERROR: the form should stay populated when an error is shown")
            failed = True

        print("Test: config_from_post rebuilds a config the engine accepts")
        postdata = {
            "postcode": "SW1A 1AA",
            "solar_kwp_0": "5.6",
            "solar_declination_0": "35",
            "solar_azimuth_0": "180",
            "solar_efficiency_0": "0.95",
            "battery_size_kwh": "9.5",
            "battery_inverter_kw": "5.0",
            "battery_export_limit_kw": "5.0",
            "battery_hybrid": "on",
            "load_source": "manual",
            "load_annual_kwh": "3800",
            "load_shape": "flat",
            "load_car_charging_kwh": "2500",
            "load_car_rate_kw": "7.4",
            "tariff_id": CUSTOM_ID,
            "tariff_import_url": "https://example.com/import/",
            "tariff_export_url": "https://example.com/export/",
            "tariff_standing_charge": "60.0",
            "samples_per_month": "2",
        }
        rebuilt = page.config_from_post(postdata)
        try:
            validate_config(rebuilt)
        except Exception as error:
            print("  ERROR: a posted form should rebuild into a valid config, got {}".format(error))
            failed = True
        if rebuilt["load"]["car_charging_kwh"] != 2500:
            print("  ERROR: car charging should survive the round trip, got {}".format(rebuilt["load"]))
            failed = True

        print("Test: choosing the Octopus load source drops the manual figures")
        postdata["load_source"] = "octopus"
        postdata["load_octopus_api_key"] = "sk_test"
        postdata["load_octopus_account_id"] = "A-1234ABCD"
        rebuilt = page.config_from_post(postdata)
        if "annual_kwh" in rebuilt["load"] or "car_charging_kwh" in rebuilt["load"]:
            print("  ERROR: the manual figures must not be sent alongside Octopus, got {}".format(rebuilt["load"]))
            failed = True
        try:
            validate_config(rebuilt)
        except Exception as error:
            print("  ERROR: the Octopus form should rebuild into a valid config, got {}".format(error))
            failed = True

    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)

    return failed
```

Register it:

```python
from tests.test_web_annual import test_web_annual, test_web_annual_form
```

```python
        ("web_annual_form", test_web_annual_form, "Annual web tab form tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test web_annual_form > /tmp/w6.txt 2>&1; grep -E "ERROR|AttributeError" /tmp/w6.txt`

Expected: FAIL — `AnnualPage` has no `render_form`.

- [ ] **Step 3: Add the form to `apps/predbat/web_annual.py`**

Add `from tariff_catalogue import CUSTOM_ID, merged_catalogue` to the imports, then append these methods to `AnnualPage`:

```python
    def _number_field(self, name, label, value, step="any", suffix=""):
        """Return one labelled numeric input row."""
        return '<div class="annual-field"><label for="{name}">{label}</label><input type="number" step="{step}" id="{name}" name="{name}" value="{value}">{suffix}</div>\n'.format(
            name=name, label=label, step=step, value=value if value is not None else "", suffix=" {}".format(suffix) if suffix else ""
        )

    def _text_field(self, name, label, value):
        """Return one labelled text input row."""
        return '<div class="annual-field"><label for="{name}">{label}</label><input type="text" id="{name}" name="{name}" value="{value}"></div>\n'.format(
            name=name, label=label, value=value if value is not None else ""
        )

    def render_form(self, config, errors=None):
        """Return the configuration form as HTML, populated from ``config``.

        ``errors`` is displayed above the form with every field left as the user
        entered it - losing their input on a validation failure would be worse
        than the failure.
        """
        solar = config.get("solar") or [{}]
        battery = config.get("battery") or {}
        load = config.get("load") or {}
        tariff = config.get("tariff") or {}
        location = config.get("location") or {}

        text = '<div class="annual-form-wrap">\n'

        if errors:
            text += '<div class="annual-error"><strong>Could not run:</strong> {}</div>\n'.format(errors)

        if not self.is_configured():
            text += '<div class="annual-banner">Predbat isn\'t configured yet — these are <strong>example values</strong>, edit them to match your home.</div>\n'

        text += '<form action="./annual_run" method="post" id="annualform">\n'

        text += '<fieldset><legend>Location</legend>\n'
        text += self._text_field("postcode", "Postcode", location.get("postcode", ""))
        text += self._number_field("latitude", "Latitude (instead of postcode)", location.get("latitude"))
        text += self._number_field("longitude", "Longitude", location.get("longitude"))
        text += "</fieldset>\n"

        text += '<fieldset><legend>Solar</legend>\n'
        for index, array in enumerate(solar):
            text += '<div class="annual-array"><strong>Array {}</strong>\n'.format(index + 1)
            text += self._number_field("solar_kwp_{}".format(index), "Peak power", array.get("kwp"), suffix="kWp")
            text += self._number_field("solar_declination_{}".format(index), "Pitch", array.get("declination", 35), suffix="degrees")
            text += self._number_field("solar_azimuth_{}".format(index), "Azimuth (180 = south)", array.get("azimuth", 180), suffix="degrees")
            text += "</div>\n"
        text += "</fieldset>\n"

        text += '<fieldset><legend>Battery</legend>\n'
        text += self._number_field("battery_size_kwh", "Usable capacity", battery.get("size_kwh"), suffix="kWh")
        text += self._number_field("battery_inverter_kw", "Inverter size", battery.get("inverter_kw"), suffix="kW")
        text += self._number_field("battery_export_limit_kw", "Export limit", battery.get("export_limit_kw"), suffix="kW")
        text += '<div class="annual-field"><label for="battery_hybrid">Hybrid inverter</label><input type="checkbox" id="battery_hybrid" name="battery_hybrid" {}></div>\n'.format("checked" if battery.get("hybrid", True) else "")
        text += "</fieldset>\n"

        using_octopus = "octopus" in load
        text += '<fieldset><legend>Load</legend>\n'
        text += '<div class="annual-field"><label><input type="radio" name="load_source" value="manual" {}> Enter my usage</label></div>\n'.format("" if using_octopus else "checked")
        text += '<div class="annual-subgroup" id="load-manual">\n'
        text += self._number_field("load_annual_kwh", "Annual consumption", load.get("annual_kwh", DEFAULT_CONFIG["load"]["annual_kwh"]), suffix="kWh")
        shape = load.get("shape", "flat")
        text += '<div class="annual-field"><label for="load_shape">Usage pattern</label><select id="load_shape" name="load_shape">\n'
        for value, caption in [("flat", "About the same through the day"), ("night", "More at night"), ("day", "More during the day")]:
            text += '<option value="{}" {}>{}</option>\n'.format(value, "selected" if shape == value else "", caption)
        text += "</select></div>\n"
        text += self._number_field("load_car_charging_kwh", "Car charging per year (0 for none)", load.get("car_charging_kwh", 0), suffix="kWh")
        text += self._number_field("load_car_rate_kw", "Charger power", load.get("car_rate_kw", 7.4), suffix="kW")
        text += "</div>\n"
        text += '<div class="annual-field"><label><input type="radio" name="load_source" value="octopus" {}> Import from Octopus</label></div>\n'.format("checked" if using_octopus else "")
        text += '<div class="annual-subgroup" id="load-octopus">\n'
        text += self._text_field("load_octopus_api_key", "Octopus API key", (load.get("octopus") or {}).get("api_key", ""))
        text += self._text_field("load_octopus_account_id", "Account ID", (load.get("octopus") or {}).get("account_id", ""))
        text += '<p class="annual-note">Your meter readings already include any car charging, so the figures above are not used with this option.</p>\n'
        text += "</div>\n"
        text += "</fieldset>\n"

        text += '<fieldset><legend>Tariff</legend>\n'
        text += '<div class="annual-field"><label for="tariff_id">Tariff</label><select id="tariff_id" name="tariff_id" onchange="annualTariffChanged()">\n'
        for entry in self.catalogue():
            text += '<option value="{}" data-import="{}" data-export="{}">{}</option>\n'.format(
                entry["id"], entry.get("import_octopus_url", ""), entry.get("export_octopus_url", ""), entry["name"]
            )
        text += "</select></div>\n"
        text += self._text_field("tariff_import_url", "Import rates URL", tariff.get("import_octopus_url", ""))
        text += self._text_field("tariff_export_url", "Export rates URL", tariff.get("export_octopus_url", ""))
        text += self._text_field("tariff_dno_region", "Octopus region letter", tariff.get("dno_region", ""))
        text += self._number_field("tariff_standing_charge", "Standing charge", tariff.get("standing_charge_p_per_day", 60.0), suffix="p/day")
        text += "</fieldset>\n"

        text += '<details><summary>Advanced</summary>\n'
        text += self._number_field("year", "Year to model (blank for the most recent complete year)", config.get("year"))
        text += self._number_field("samples_per_month", "Days sampled per month", config.get("samples_per_month", 2), step="1")
        text += self._number_field("pv10_derate_fallback", "P10 fallback derate", config.get("pv10_derate_fallback", 0.7))
        for index, array in enumerate(solar):
            text += self._number_field("solar_efficiency_{}".format(index), "Array {} efficiency".format(index + 1), array.get("efficiency", 0.95))
        text += "</details>\n"

        text += '<button type="submit" id="annual-run-button">Run</button>\n'
        text += "</form>\n</div>\n"
        return text

    def config_from_post(self, postdata):
        """Rebuild a config dict from submitted form fields.

        Values are left as the strings the browser sent; validate_config() in the
        engine does the coercion and range checking, so there is exactly one place
        that decides what a valid number is.
        """

        def value(name, default=None):
            """Return one posted field, or the default when absent or blank."""
            raw = postdata.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip()

        config = {}

        location = {}
        if value("postcode"):
            location["postcode"] = value("postcode")
        if value("latitude") is not None and value("longitude") is not None:
            location["latitude"] = value("latitude")
            location["longitude"] = value("longitude")
        config["location"] = location

        arrays = []
        index = 0
        while value("solar_kwp_{}".format(index)) is not None:
            arrays.append(
                {
                    "kwp": value("solar_kwp_{}".format(index)),
                    "declination": value("solar_declination_{}".format(index), 35),
                    "azimuth": value("solar_azimuth_{}".format(index), 180),
                    "efficiency": value("solar_efficiency_{}".format(index), 0.95),
                }
            )
            index += 1
        if arrays:
            config["solar"] = arrays

        if value("battery_size_kwh") is not None:
            config["battery"] = {
                "size_kwh": value("battery_size_kwh"),
                "inverter_kw": value("battery_inverter_kw", 5.0),
                "export_limit_kw": value("battery_export_limit_kw", 5.0),
                "hybrid": bool(postdata.get("battery_hybrid")),
            }

        # The engine rejects an Octopus block alongside manual figures, because the
        # meter series already contains any car charging. Send one or the other.
        if value("load_source", "manual") == "octopus":
            config["load"] = {"octopus": {"api_key": value("load_octopus_api_key", ""), "account_id": value("load_octopus_account_id", "")}}
        else:
            config["load"] = {
                "annual_kwh": value("load_annual_kwh", 3800),
                "shape": value("load_shape", "flat"),
                "car_charging_kwh": value("load_car_charging_kwh", 0),
                "car_rate_kw": value("load_car_rate_kw", 7.4),
            }

        tariff = {"standing_charge_p_per_day": value("tariff_standing_charge", 0)}
        if value("tariff_import_url"):
            tariff["import_octopus_url"] = value("tariff_import_url")
        if value("tariff_export_url"):
            tariff["export_octopus_url"] = value("tariff_export_url")
        if value("tariff_dno_region"):
            tariff["dno_region"] = value("tariff_dno_region")
        if not tariff.get("import_octopus_url"):
            tariff["rates_import"] = DEFAULT_CONFIG["tariff"]["rates_import"]
            tariff["rates_export"] = DEFAULT_CONFIG["tariff"]["rates_export"]
        config["tariff"] = tariff

        if value("year"):
            config["year"] = value("year")
        config["samples_per_month"] = value("samples_per_month", 2)
        if value("pv10_derate_fallback"):
            config["pv10_derate_fallback"] = value("pv10_derate_fallback")

        return config
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd coverage && ./run_all -k web_annual > /tmp/w6.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w6.txt`

Expected: no output; both `web_annual` and `web_annual_form` pass.

- [ ] **Step 5: Run pre-commit and commit**

```bash
git add apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
git commit -m "feat(annual): render the Annual tab configuration form"
```

---

## Task 7: Routes and wiring

**Files:**
- Modify: `apps/predbat/web_annual.py`
- Modify: `apps/predbat/web.py`
- Modify: `apps/predbat/web_helper.py`
- Modify: `apps/predbat/tests/test_web_annual.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual_job.AnnualJob`, `annual_store.save_run/list_runs/load_run`, `annual.validate_config`, `AnnualPage.render_form/config_from_post/save_config/load_config`.
- Produces:
  - `AnnualPage.html_annual(request)`, `html_annual_post`, `html_annual_run`, `html_annual_status`, `html_annual_cancel` — all async aiohttp handlers
  - `AnnualPage.cli_command(config_path) -> list[str]`

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_web_annual.py`:

```python
def test_web_annual_routes(my_predbat):
    """Verify the run command, validation gating and the status payload."""
    import asyncio

    failed = False
    print("**** Testing web_annual routes ****")

    page = make_page(my_predbat)

    print("Test: the CLI command targets annual_cli.py in machine mode")
    command = page.cli_command("/tmp/annual.yaml")
    if "--machine" not in command:
        print("  ERROR: the child must be run in machine mode, got {}".format(command))
        failed = True
    if not any("annual_cli.py" in part for part in command):
        print("  ERROR: the command should invoke annual_cli.py, got {}".format(command))
        failed = True
    if "--config" not in command or "/tmp/annual.yaml" not in command:
        print("  ERROR: the config path should be passed, got {}".format(command))
        failed = True

    print("Test: the status payload is JSON-serialisable and names its state")
    status = asyncio.run(page.status_payload())
    for key in ["state", "completed", "total", "message", "elapsed"]:
        if key not in status:
            print("  ERROR: status is missing '{}', got {}".format(key, status))
            failed = True
    if status.get("state") != "idle":
        print("  ERROR: a fresh page should report idle, got {}".format(status.get("state")))
        failed = True

    print("Test: an invalid config is rejected before anything is spawned")
    bad = {"location": {}, "load": {"annual_kwh": 1}, "tariff": {"rates_import": [{"rate": 5}]}}
    error = page.validation_error(bad)
    if not error:
        print("  ERROR: an invalid config should produce an error message")
        failed = True
    if page.job.state != "idle":
        print("  ERROR: validation must not start a job, state was {}".format(page.job.state))
        failed = True

    print("Test: a valid config produces no error")
    if page.validation_error(page.prefill_config()):
        print("  ERROR: the prefill config should validate, got {}".format(page.validation_error(page.prefill_config())))
        failed = True

    return failed
```

Register it:

```python
from tests.test_web_annual import test_web_annual, test_web_annual_form, test_web_annual_routes
```

```python
        ("web_annual_routes", test_web_annual_routes, "Annual web tab route tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test web_annual_routes > /tmp/w7.txt 2>&1; grep -E "ERROR|AttributeError" /tmp/w7.txt`

Expected: FAIL — `AnnualPage` has no `cli_command`.

- [ ] **Step 3: Add the handlers to `apps/predbat/web_annual.py`**

Add to the imports:

```python
import datetime
import sys

from aiohttp import web

from annual import AnnualConfigError, validate_config
from annual_job import AnnualJob
from annual_store import list_runs, load_run, save_run
```

Add to `AnnualPage.__init__`, after `self.log`:

```python
        self.job = AnnualJob(log=self.log)
        self.last_error = None
```

Then append:

```python
    def cli_command(self, config_path):
        """Return the argv for the child process that performs the run."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "annual_cli.py")
        work_dir = os.path.join(self.base.config_root, "annual_work")
        return [sys.executable, script, "--config", config_path, "--work-dir", work_dir, "--machine"]

    def validation_error(self, config):
        """Return a message when the config is invalid, or None when it is fine.

        Validation runs here for immediate feedback, but the same validate_config
        runs again inside the child and remains the authority.
        """
        try:
            validate_config(config)
        except AnnualConfigError as error:
            return str(error)
        return None

    def _storage(self):
        """Return the Storage component, or None when it is unavailable."""
        components = getattr(self.base, "components", None)
        return components.get_component("storage") if components else None

    async def status_payload(self):
        """Return the polling payload: job state plus the stored run list."""
        payload = self.job.status()
        payload["runs"] = await list_runs(self._storage())
        return payload

    async def _store_completed_run(self, config):
        """Save a finished run into the ring, once."""
        if self.job.state != "complete" or self.job.results is None:
            return
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        await save_run(self._storage(), self.job.results, config, run_id)
        self.job.results = None

    async def html_annual(self, request):
        """Render the Annual tab: the form, then the selected run's results."""
        self.web.default_page = "./annual"
        config = self.load_config()

        text = self.web.get_header("Predbat Annual")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_form(config, errors=self.last_error)
        text += self.render_progress()

        storage = self._storage()
        runs = await list_runs(storage)
        selected = request.query.get("run") or (runs[0]["id"] if runs else None)
        results = await load_run(storage, selected) if selected else None
        text += self.render_results(results, runs, selected)
        text += self.render_script()
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_annual_post(self, request):
        """Save the configuration without running."""
        postdata = await request.post()
        config = self.config_from_post(postdata)
        self.last_error = self.validation_error(config)
        if not self.last_error:
            self.save_config(config)
        return await self.html_annual(request)

    async def html_annual_run(self, request):
        """Validate, save, and spawn the run."""
        postdata = await request.post()
        config = self.config_from_post(postdata)
        self.last_error = self.validation_error(config)
        if self.last_error:
            return await self.html_annual(request)

        self.save_config(config)
        self._running_config = config
        started = await self.job.start(self.cli_command(self._config_path()))
        if not started and self.job.state != "running":
            self.last_error = self.job.status().get("error") or "The run could not be started"
        return await self.html_annual(request)

    async def html_annual_status(self, request):
        """Return the job status as JSON for the page to poll."""
        if self.job.state == "complete" and self.job.results is not None:
            await self._store_completed_run(getattr(self, "_running_config", self.load_config()))
        return web.json_response(await self.status_payload())

    async def html_annual_cancel(self, request):
        """Cancel a running job."""
        await self.job.cancel()
        return web.json_response(self.job.status())

    async def html_annual_download(self, request):
        """Return one stored run's raw results document as a JSON download."""
        run_id = request.query.get("run")
        results = await load_run(self._storage(), run_id)
        if results is None:
            return web.json_response({"error": "No stored run with id {}".format(run_id)}, status=404)
        return web.json_response(results, headers={"Content-Disposition": 'attachment; filename="annual-{}.json"'.format(run_id)})
```

- [ ] **Step 4: Add the progress, CSS and script fragments**

Note the `render_results` placeholder below: `html_annual` calls it, so the module
would not render without one. The results task replaces it wholesale — replace it,
do not add a second definition.

Append to `AnnualPage`:

```python
    def render_results(self, results, runs, selected_id):
        """Placeholder replaced in full by the results task; keeps the page renderable."""
        return "<p>No results yet — fill in the form above and press Run.</p>\n"

    def render_css(self):
        """Return the scoped styles for the tab."""
        return """<style>
.annual-form-wrap fieldset { border: 1px solid var(--md-border, #cbd5e1); margin-bottom: 1rem; padding: 0.75rem; }
.annual-form-wrap legend { font-weight: 600; }
.annual-field { margin: 0.35rem 0; }
.annual-field label { display: inline-block; min-width: 20rem; }
.annual-subgroup { margin-left: 1.5rem; }
.annual-note { font-size: 0.85rem; opacity: 0.8; }
.annual-banner { border-left: 4px solid #D55E00; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-error { border-left: 4px solid #b00020; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-progress { margin: 1rem 0; }
.annual-bar { height: 1.25rem; border: 1px solid var(--md-border, #cbd5e1); }
.annual-bar-fill { height: 100%; background: #0072B2; width: 0%; }
.annual-caveats li { margin-bottom: 0.35rem; }
.annual-unavailable { opacity: 0.6; font-style: italic; }
</style>
"""

    def render_progress(self):
        """Return the progress area, hidden until a run starts."""
        return """<div class="annual-progress" id="annual-progress" style="display:none">
  <div class="annual-bar"><div class="annual-bar-fill" id="annual-bar-fill"></div></div>
  <p id="annual-progress-text"></p>
  <button type="button" onclick="annualCancel()">Cancel</button>
</div>
"""

    def render_script(self):
        """Return the polling and tariff-picker script."""
        return """<script>
function annualTariffChanged() {
  var select = document.getElementById('tariff_id');
  var option = select.options[select.selectedIndex];
  document.getElementById('tariff_import_url').value = option.getAttribute('data-import') || '';
  document.getElementById('tariff_export_url').value = option.getAttribute('data-export') || '';
}
function annualCancel() { fetch('./annual_cancel', {method: 'POST'}); }
function annualPoll() {
  fetch('./annual_status').then(function (r) { return r.json(); }).then(function (s) {
    var box = document.getElementById('annual-progress');
    var button = document.getElementById('annual-run-button');
    if (s.state === 'running') {
      box.style.display = 'block';
      if (button) { button.disabled = true; }
      var pct = s.total ? Math.round((s.completed / s.total) * 100) : 0;
      document.getElementById('annual-bar-fill').style.width = pct + '%';
      document.getElementById('annual-progress-text').textContent = s.message + ' — ' + pct + '% (' + s.elapsed + 's)';
    } else {
      if (button) { button.disabled = false; }
      if (s.state === 'complete') { window.location = './annual'; return; }
      if (s.state === 'failed' || s.state === 'cancelled') {
        box.style.display = 'block';
        document.getElementById('annual-progress-text').textContent = s.state + (s.error ? ': ' + s.error : '');
        return;
      }
      box.style.display = 'none';
    }
    setTimeout(annualPoll, 1000);
  }).catch(function () { setTimeout(annualPoll, 5000); });
}
annualPoll();
</script>
"""
```

- [ ] **Step 5: Wire it into `apps/predbat/web.py`**

Add the import beside the other web module imports (near `from web_metrics_dashboard import ...`):

```python
from web_annual import AnnualPage
```

In `WebInterface.__init__`, after the other attribute setup, create the page:

```python
        self.annual_page = AnnualPage(self)
```

Register the routes beside the existing `app.router.add_get("/compare", ...)` lines:

```python
        app.router.add_get("/annual", self.annual_page.html_annual)
        app.router.add_post("/annual", self.annual_page.html_annual_post)
        app.router.add_post("/annual_run", self.annual_page.html_annual_run)
        app.router.add_get("/annual_status", self.annual_page.html_annual_status)
        app.router.add_post("/annual_cancel", self.annual_page.html_annual_cancel)
        app.router.add_get("/annual_download", self.annual_page.html_annual_download)
```

- [ ] **Step 6: Add the nav link in `apps/predbat/web_helper.py`**

Find `<a href='./compare'>Compare</a>` and add immediately after it:

```html
<a href='./annual'>Annual</a>
```

- [ ] **Step 7: Run the tests**

Run: `cd coverage && ./run_all -k web_annual > /tmp/w7.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w7.txt`

Then the existing web tests, since `web.py` changed:

Run: `./run_all -k web_ > /tmp/w7b.txt 2>&1; grep -E "ERROR|FAILED|Traceback" /tmp/w7b.txt`

Expected: no output from either.

- [ ] **Step 8: Run pre-commit and commit**

```bash
git add apps/predbat/web_annual.py apps/predbat/web.py apps/predbat/web_helper.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/web.py apps/predbat/web_helper.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
git commit -m "feat(annual): wire the Annual tab routes and navigation"
```

---

## Task 8: Results, chart and run selector

**Files:**
- Modify: `apps/predbat/web_annual.py`
- Modify: `apps/predbat/tests/test_web_annual.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `annual_store.list_runs`, `load_run`.
- Produces: `AnnualPage.render_results(results, runs, selected_id) -> str`

**The palette is fixed and validated. Do not substitute.** Predbat's house chart trio was measured and fails: green↔orange is ΔE 3.6 under protanopia, so roughly 1 in 12 men could not tell "Without Predbat" from "With Predbat". These three pass every check in both light and dark mode:

| Series | Colour |
|---|---|
| No PV/Battery | `#0072B2` |
| Without Predbat | `#D55E00` |
| With Predbat | `#009E73` |

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_web_annual.py`:

```python
def sample_run_results():
    """Return a results document covering an ok, a degraded and an unavailable month."""
    scenarios = {
        "no_pvbat": {"cost_p": 18000.0, "import_kwh": 400.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 0.0, "self_consumed_kwh": 0.0, "self_consumed_kwh_meaningful": True},
        "without_predbat": {"cost_p": 9000.0, "import_kwh": 300.0, "export_kwh": 20.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 90.0, "export_credit_p_estimate": 300.0, "self_consumed_kwh": 100.0, "self_consumed_kwh_meaningful": True},
        "with_predbat": {"cost_p": 6600.0, "import_kwh": 280.0, "export_kwh": 145.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 140.0, "export_credit_p_estimate": 675.0, "self_consumed_kwh": 0.0, "self_consumed_kwh_meaningful": False},
    }
    return {
        "year": 2025,
        "months": [
            {"month": 1, "status": "ok", "days": 31, "sampled_days": ["2025-01-08", "2025-01-24"], "standing_charge_p": 1860.0, "scenarios": scenarios},
            {"month": 2, "status": "degraded", "days": 28, "failed_days": ["2025-02-14"], "standing_charge_p": 1680.0, "scenarios": scenarios},
            {"month": 3, "status": "unavailable", "reason": "no rate data available", "days": 31, "standing_charge_p": 1860.0},
        ],
        "annual": {"scenarios": scenarios, "standing_charge_p": 3540.0, "savings": {"pv_battery_vs_none_p": 9000.0, "predbat_vs_baseline_p": 2400.0}, "months_included": 2, "months_excluded": [3]},
        "caveats": ["An example caveat about the P10 fallback."],
    }


def test_web_annual_results(my_predbat):
    """Verify the results view: totals, chart series, month statuses, caveats, selector."""
    failed = False
    print("**** Testing web_annual results ****")

    page = make_page(my_predbat)
    runs = [{"id": "20260726-101500", "label": "9.5kWh battery · 5.6kWp · Agile", "months_included": 12}, {"id": "20260725-090000", "label": "no battery · 5.6kWp · Agile", "months_included": 12}]
    html = page.render_results(sample_run_results(), runs, "20260726-101500")

    print("Test: the annual savings figures are shown")
    if "90.00" not in html:
        print("  ERROR: the PV/battery saving (9000p = £90.00) should be shown")
        failed = True
    if "24.00" not in html:
        print("  ERROR: the Predbat saving (2400p = £24.00) should be shown")
        failed = True

    print("Test: the validated colourblind-safe palette is used, not the house trio")
    for colour in ["#0072B2", "#D55E00", "#009E73"]:
        if colour not in html:
            print("  ERROR: expected the validated colour {} in the chart".format(colour))
            failed = True
    for banned in ["#4CAF50", "#FF9800", "#2196F3"]:
        if banned in html:
            print("  ERROR: {} fails CVD separation for this chart and must not be used".format(banned))
            failed = True

    print("Test: an unavailable month is marked, never drawn as zero")
    if "unavailable" not in html.lower():
        print("  ERROR: the unavailable month should be marked as such")
        failed = True
    if "no rate data available" not in html:
        print("  ERROR: the reason for exclusion should be shown")
        failed = True

    print("Test: a degraded month is shown with its cost and flagged as partial")
    if "degraded" not in html.lower():
        print("  ERROR: the degraded month should be flagged")
        failed = True

    print("Test: months_included is stated so the annual figure's coverage is clear")
    if "2 of 12" not in html:
        print("  ERROR: the annual figure should say how many months it covers")
        failed = True

    print("Test: caveats are displayed, not buried in the JSON")
    if "An example caveat about the P10 fallback." not in html:
        print("  ERROR: caveats must be shown to the user")
        failed = True

    print("Test: self_consumed_kwh is qualified when it is not meaningful")
    if "not meaningful" not in html.lower():
        print("  ERROR: a non-meaningful self-consumption figure should be qualified, not shown bare")
        failed = True

    print("Test: the run selector lists every stored run and marks the selected one")
    for run in runs:
        if run["label"] not in html:
            print("  ERROR: run {} should appear in the selector".format(run["id"]))
            failed = True
    if "selected" not in html:
        print("  ERROR: the selected run should be marked in the dropdown")
        failed = True

    print("Test: a download link is offered for the selected run")
    if "annual_download?run=20260726-101500" not in html:
        print("  ERROR: the selected run should be downloadable as JSON")
        failed = True

    print("Test: with no runs at all the view says so rather than rendering an empty chart")
    empty = page.render_results(None, [], None)
    if "apexcharts" in empty.lower() and "series" in empty.lower():
        print("  ERROR: no chart should be drawn when there are no results")
        failed = True
    if "no results" not in empty.lower():
        print("  ERROR: the empty state should say there are no results yet")
        failed = True

    return failed
```

Register it:

```python
from tests.test_web_annual import test_web_annual, test_web_annual_form, test_web_annual_results, test_web_annual_routes
```

```python
        ("web_annual_results", test_web_annual_results, "Annual web tab results tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd coverage && ./run_all --test web_annual_results > /tmp/w8.txt 2>&1; grep -E "ERROR|AttributeError" /tmp/w8.txt`

Expected: FAIL — `AnnualPage` has no `render_results`.

- [ ] **Step 3: Add the results view to `apps/predbat/web_annual.py`**

Add near the top of the module:

```python
import calendar
import json

# Validated with the dataviz palette checker in BOTH light and dark mode, all pairs.
# Predbat's house chart trio (#2196F3/#FF9800/#4CAF50) FAILS here: green vs orange is
# only deltaE 3.6 under protanopia, so roughly one man in twelve could not tell
# "Without Predbat" from "With Predbat" - the exact comparison this chart exists to
# make. Do not substitute without re-running the validator.
SCENARIO_COLOURS = {"no_pvbat": "#0072B2", "without_predbat": "#D55E00", "with_predbat": "#009E73"}
SCENARIO_LABELS = {"no_pvbat": "No PV/Battery", "without_predbat": "Without Predbat", "with_predbat": "With Predbat"}
SCENARIO_ORDER = ["no_pvbat", "without_predbat", "with_predbat"]
```

Then append to `AnnualPage`:

```python
    @staticmethod
    def _pounds(pence):
        """Return a pence value formatted as pounds with an explicit unit."""
        try:
            return "£{:.2f}".format(float(pence) / 100.0)
        except (TypeError, ValueError):
            return "n/a"

    def render_results(self, results, runs, selected_id):
        """Return the results view: selector, totals, chart, monthly table, caveats."""
        text = '<div class="annual-results">\n'
        text += self._render_selector(runs, selected_id)

        if not results:
            text += "<p>No results yet — fill in the form above and press Run.</p>\n</div>\n"
            return text

        annual = results.get("annual", {}) or {}
        scenarios = annual.get("scenarios")
        included = annual.get("months_included", 0)

        text += "<h2>Annual totals for {}</h2>\n".format(results.get("year", ""))
        if not scenarios:
            text += "<p>No month produced a usable result, so there is no annual figure.</p>\n"
        else:
            text += "<table class='comparison-table'><tr><th>Scenario</th><th>Cost</th><th>Import</th><th>Export</th></tr>\n"
            for key in SCENARIO_ORDER:
                entry = scenarios.get(key, {})
                text += "<tr><td>{}</td><td>{}</td><td>{} kWh</td><td>{} kWh</td></tr>\n".format(
                    SCENARIO_LABELS[key], self._pounds(entry.get("cost_p")), round(entry.get("import_kwh", 0), 1), round(entry.get("export_kwh", 0), 1)
                )
            text += "</table>\n"
            savings = annual.get("savings", {}) or {}
            text += "<p><strong>PV and battery save {}</strong> against no system.</p>\n".format(self._pounds(savings.get("pv_battery_vs_none_p", 0)))
            text += "<p><strong>Predbat saves a further {}</strong> against a timer-controlled battery.</p>\n".format(self._pounds(savings.get("predbat_vs_baseline_p", 0)))
            text += "<p>Standing charge (identical in every scenario): {}</p>\n".format(self._pounds(annual.get("standing_charge_p", 0)))

        text += "<p>Based on {} of 12 months.".format(included)
        excluded = annual.get("months_excluded") or []
        if excluded:
            text += " Excluded: {}.".format(", ".join(calendar.month_abbr[month] for month in excluded))
        text += "</p>\n"

        text += self._render_chart(results)
        text += self._render_month_table(results)
        text += self._render_caveats(results)
        if selected_id:
            text += '<p><a href="./annual_download?run={}">Download this run as JSON</a></p>\n'.format(selected_id)
        text += "</div>\n"
        return text

    def _render_selector(self, runs, selected_id):
        """Return the run selector, or nothing when there are no stored runs."""
        if not runs:
            return ""
        text = '<form action="./annual" method="get" class="annual-selector"><label for="run">Run</label><select id="run" name="run" onchange="this.form.submit()">\n'
        for run in runs:
            text += '<option value="{}" {}>{} — {}</option>\n'.format(run["id"], "selected" if run["id"] == selected_id else "", run.get("label", run["id"]), run["id"])
        text += "</select></form>\n"
        return text

    def _render_chart(self, results):
        """Return the grouped monthly bar chart.

        Only months with a usable result contribute a bar. An unavailable month is
        left out of the series entirely rather than plotted as zero - a zero-height
        bar reads as free electricity, which is the opposite of what happened.
        """
        categories = []
        series = {key: [] for key in SCENARIO_ORDER}
        for entry in results.get("months", []):
            if entry.get("status") not in ("ok", "degraded"):
                continue
            categories.append(calendar.month_abbr[entry["month"]])
            for key in SCENARIO_ORDER:
                series[key].append(round(entry.get("scenarios", {}).get(key, {}).get("cost_p", 0) / 100.0, 2))

        if not categories:
            return "<p>No month produced a usable result, so there is nothing to chart.</p>\n"

        payload = {
            "chart": {"type": "bar", "height": 400, "toolbar": {"show": False}},
            "series": [{"name": SCENARIO_LABELS[key], "data": series[key]} for key in SCENARIO_ORDER],
            "colors": [SCENARIO_COLOURS[key] for key in SCENARIO_ORDER],
            "xaxis": {"categories": categories},
            "yaxis": {"title": {"text": "Cost (£)"}},
            "plotOptions": {"bar": {"columnWidth": "70%", "borderRadius": 4, "borderRadiusApplication": "end"}},
            "stroke": {"show": True, "width": 2, "colors": ["transparent"]},
            "dataLabels": {"enabled": False},
            "legend": {"position": "top"},
            "tooltip": {"y": {"formatter": None}},
        }
        text = '<div id="annual-chart"></div>\n'
        text += '<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>\n'
        text += "<script>\nvar annualOptions = {};\n".format(json.dumps(payload))
        text += "annualOptions.tooltip.y = {formatter: function (v) { return '£' + v.toFixed(2); }};\n"
        text += "new ApexCharts(document.querySelector('#annual-chart'), annualOptions).render();\n</script>\n"
        return text

    def _render_month_table(self, results):
        """Return the per-month energy breakdown, marking degraded and unavailable months."""
        text = "<h2>By month</h2>\n<table class='comparison-table'>\n"
        text += "<tr><th>Month</th><th>Scenario</th><th>Cost</th><th>Import</th><th>Export</th><th>PV</th><th>Self-consumed</th><th>Battery</th></tr>\n"
        for entry in results.get("months", []):
            name = calendar.month_abbr[entry["month"]]
            if entry.get("status") not in ("ok", "degraded"):
                text += "<tr class='annual-unavailable'><td>{}</td><td colspan='7'>unavailable — {}</td></tr>\n".format(name, entry.get("reason", "no result"))
                continue
            suffix = " (degraded — {} sampled day(s) failed)".format(len(entry.get("failed_days", []))) if entry.get("status") == "degraded" else ""
            for key in SCENARIO_ORDER:
                scenario = entry.get("scenarios", {}).get(key, {})
                if scenario.get("self_consumed_kwh_meaningful", True):
                    self_consumed = "{} kWh".format(round(scenario.get("self_consumed_kwh", 0), 1))
                else:
                    self_consumed = "<span class='annual-unavailable' title='The battery exported more than the PV generated, so this figure is not meaningful'>not meaningful</span>"
                text += "<tr><td>{}{}</td><td>{}</td><td>{}</td><td>{} kWh</td><td>{} kWh</td><td>{} kWh</td><td>{}</td><td>{} kWh</td></tr>\n".format(
                    name if key == SCENARIO_ORDER[0] else "",
                    suffix if key == SCENARIO_ORDER[0] else "",
                    SCENARIO_LABELS[key],
                    self._pounds(scenario.get("cost_p")),
                    round(scenario.get("import_kwh", 0), 1),
                    round(scenario.get("export_kwh", 0), 1),
                    round(scenario.get("pv_generated_kwh", 0), 1),
                    self_consumed,
                    round(scenario.get("battery_throughput_kwh", 0), 1),
                )
        text += "</table>\n"
        return text

    def _render_caveats(self, results):
        """Return the caveats the engine attached to this run."""
        caveats = results.get("caveats") or []
        if not caveats:
            return ""
        text = "<h2>Caveats</h2>\n<ul class='annual-caveats'>\n"
        for caveat in caveats:
            text += "<li>{}</li>\n".format(caveat)
        text += "</ul>\n"
        return text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd coverage && ./run_all -k web_annual > /tmp/w8.txt 2>&1; grep -E "ERROR|Traceback" /tmp/w8.txt`

Expected: no output.

- [ ] **Step 5: Look at the rendered page**

The validator checks colour, not layout. Start Predbat's web interface (or render `render_results(sample_run_results(), runs, id)` to a file and open it) and check the chart for label collisions, bar overflow and legend placement in **both** light and dark mode. Report what you saw — "it rendered" is not an observation.

- [ ] **Step 6: Run pre-commit and commit**

```bash
git add apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
coverage/venv/bin/pre-commit run --files apps/predbat/web_annual.py apps/predbat/tests/test_web_annual.py apps/predbat/unit_test.py
git commit -m "feat(annual): add the Annual tab results view and monthly chart"
```

---

## Task 9: Documentation

**Files:**
- Modify: `docs/annual-prediction.md`
- Modify: `.cspell/custom-dictionary-workspace.txt` (only if pre-commit flags a word)

- [ ] **Step 1: Add a "Using the web interface" section to `docs/annual-prediction.md`**

Insert before the existing "Running it" section, which covers the CLI. Cover:

- Where the tab is (the **Annual** entry in the navigation) and that it needs no Home Assistant and no configured Predbat.
- That the form prefills from your existing setup where it can, and shows example values otherwise — and that the banner tells you which you are looking at.
- The tariff dropdown, including that your own `compare_list` entries appear in it if you have any, and that picking one fills in the URL fields which stay editable.
- That a run takes one to three minutes, or two to six with a car configured, and shows a progress bar; that it keeps running if you navigate away; and that Cancel stops it.
- That the last five runs are kept and can be switched between with the selector, so you can compare a 5 kWh battery against a 10 kWh one without re-running either.
- That the chart omits unavailable months rather than drawing them as zero.

Keep the existing CLI documentation — both interfaces are supported.

- [ ] **Step 2: Verify the docs build**

```bash
coverage/venv/bin/pre-commit run --files docs/annual-prediction.md
cd coverage && ./venv/bin/mkdocs build --strict -f ../mkdocs.yml > /tmp/w9.txt 2>&1; grep -iE "error|warning" /tmp/w9.txt
```

Expected: no errors for `annual-prediction.md`. If cspell flags a word, add it to `.cspell/custom-dictionary-workspace.txt`, re-run pre-commit, and **re-stage** that file — a hook sorts it.

- [ ] **Step 3: Run the full test suite**

The tab touches `web.py`, which the whole web test suite depends on.

```bash
cd coverage && ./run_all > /tmp/w9full.txt 2>&1; grep -E "FAILED|All tests passed|tests failed" /tmp/w9full.txt | tail -5
```

Expected: "All tests passed", around 250-300 seconds. Read the file.

- [ ] **Step 4: Commit**

```bash
git add docs/annual-prediction.md .cspell/custom-dictionary-workspace.txt
coverage/venv/bin/pre-commit run --files docs/annual-prediction.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs: document the Annual prediction web tab"
```

---

## Notes for the implementer

**The chart palette is not a style preference.** It was measured. If you find yourself wanting Predbat's usual green/orange/blue because it looks more consistent, re-read Task 8's opening: that trio is ΔE 3.6 apart under protanopia for the two series the whole tool exists to compare. Consistency that hides the answer from one reader in twelve is not consistency worth having.

**Three properties must survive from the engine into the UI.** They were hard-won during the engine work and are easy to undo here:
1. An unavailable month is never a zero-height bar or a `£0.00` cell.
2. `self_consumed_kwh` is qualified when `self_consumed_kwh_meaningful` is false.
3. Caveats are on screen, not only in the JSON.

**The unconfigured case is the acceptance criterion**, not a nicety. `test_web_annual`'s first assertion is the one that proves it. If it starts failing, something has made the tab depend on a configured Predbat, which breaks the prospective-buyer path the tool exists for.

**Do not read `apps.yaml` from disk anywhere.** Everything comes from `get_arg()`.
