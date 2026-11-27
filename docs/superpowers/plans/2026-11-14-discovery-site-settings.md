# Discovery: hybrid, GE Cloud Data serial and EMS, invert settings - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move GE Cloud's remaining non-slot configuration out of the component: every inverter component reports whether it is hybrid and discovery sets `inverter_hybrid`; GE Cloud Data finds its serial from discovery and declines to run behind an EMS; discovery writes the `*_invert` settings per slot.

**Architecture:** One new structural record field (`hybrid`) and a site-wide rule in `Coordinator` that runs on every changed report from an automatic component, writing `inverter_hybrid` through the existing `set_state_external` path, scheduled rather than awaited. `GECloudData` reads GE Cloud's records through a read-only coordinator accessor. The invert settings become binding settings whose per-slot value `bindings.py` derives from the record's entity descriptors.

**Tech Stack:** Python 3, the repo's own test runner (`coverage/run_all`, `TEST_REGISTRY` in `apps/predbat/unit_test.py`), no new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-14-discovery-site-settings-design.md` (addendum to `2026-11-14-discovery-automatic-inverter-config-design.md`). Read both; the addendum is binding.

## Global Constraints

- Work in `/Users/treforsouthwell/batpred3` on branch `feat/discovery-automatic-config` (PR #6188). The checkout holds the maintainer's own uncommitted changes to `CLAUDE.md`, `AGENTS.md` and four `.claude/skills/gitnexus/*/SKILL.md` files: never stage, commit, revert or edit them. Stage files by name only. Do not push.
- The first commit adds this plan and the addendum spec.
- Tests from `coverage/`, always to a file: `./run_all --test <name> > out_<name>.txt 2>&1; echo exit=$?`. A pass is exit=0 and a final `**** All tests passed` line; passing runs print `Traceback`/`Error:` lines from failure-path tests.
- GitNexus `impact` before editing an existing function (report HIGH/CRITICAL; carry on only with the change the task names); `detect_changes` before each commit.
- Every function and class needs a docstring. British English in comments, docs and log text. Line length up to 250. Tests restore any shared state they change.
- `bindings.py` stays pure and importable on its own; `test_modules_import_standalone` keeps passing.
- `configure()`, the hybrid rule and the checker never raise and never call `non_fatal_error_occurred()` or `record_status(..., had_errors=True)`.
- Components read configuration through `self.get_arg()` and `ComponentBase` helpers, never `self.base.<attr>` from a component module.
- The GE Cloud acceptance tests and the golden test keep passing; the API-call budget test stays byte-identical.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **The write has not landed before the next pass.** `set_state_external` is scheduled, so a second report can arrive while the switch still reads the old value; expect one scheduled write, not two. Task 1, `test_hybrid_write_not_repeated_while_pending`.
2. **A component the user configures by hand.** A report with `automatic: false` that has `hybrid: False` must not turn the switch off. Task 1, `test_hybrid_ignores_non_automatic_report`.
3. **GE Cloud Data starts before GE Cloud has reported.** It must pick the serial up later, not give up for the run. Task 5, `test_cloud_data_serial_arrives_late`.
4. **An EMS appears or leaves mid-run.** GE Cloud Data stops and resumes by itself, and the user's `ge_cloud_data` is never written. Task 5, `test_cloud_data_ems_comes_and_goes`.
5. **A mixed manual and discovered fleet.** Invert settings at a manual slot are never written, including when the manual slot sits between two discovered ones. Task 6, `test_replay_invert_leaves_manual_slot`.

---

### Task 1: The `hybrid` record field and the coordinator's site rule

**Files:**
- Modify: `apps/predbat/coordinator.py` (`SECTION_SPEC["inverters"]["structural"]` about line 118; `inverter_record()` about line 845; `Coordinator.report()`, `configure()`; new methods)
- Modify: `apps/predbat/component_base.py` (new helper beside `report_discovery`)
- Test: `apps/predbat/tests/test_coordinator.py` (record field) and `apps/predbat/tests/test_discovery_automatic.py` (the rule)
- Docs: `docs/discovery-catalogue.md` (the inverter record section: the new field; the automatic configuration section: the hybrid rule)

**Interfaces:**
- Produces:
  - `inverter_record(..., hybrid=None, ...)`: keyword-only; kept when a bool (`False` is kept), omitted when None.
  - `Coordinator.apply_hybrid()`: works out the site value and schedules the write; called from `report()` on every changed cleaned report (any component, opted in or not) and from `configure()`.
  - `Coordinator.hybrid_decision() -> tuple[bool | None, list[str]]`: pure over the stored reports; the value (True / False / None for "leave alone") and the deciding `device_id`s.
  - `Coordinator.inverter_records(source) -> list[dict]`: a deep copy of that source's validated inverter records in report order (used by Task 5).
  - `ComponentBase.discovery_inverter_records(source) -> list[dict]`: wrapper returning `[]` without a coordinator (or with no `base`), like `report_discovery()`.

- [ ] **Step 1: Write the failing tests.**

```python
# test_coordinator.py
def test_inverter_record_hybrid():
    assert inverter_record("x:1", hybrid=False)["hybrid"] is False
    assert inverter_record("x:1", hybrid=True)["hybrid"] is True
    assert "hybrid" not in inverter_record("x:1")
    # validate_report keeps a bool and drops anything else, logging once
    # validate_report({"inverters": [{"device_id": "x:1", "hybrid": "no"}]}, ...) -> no "hybrid" key, a "dropped" log line

# test_discovery_automatic.py, with the existing _Base/_Components stubs; base gets ha_interface = a stub recording set_state_external calls,
# and a get_arg returning args.get(...) with "inverter_hybrid" starting True
def test_hybrid_one_false_turns_off():          # gecloud a hybrid True, alphaess b hybrid False, both automatic -> one write ("switch.predbat_inverter_hybrid", False)
def test_hybrid_all_true_turns_on():            # current False, records True and True -> one write True
def test_hybrid_nothing_known_untouched():      # records without hybrid -> no write
def test_hybrid_ignores_pv_only():              # a PV-only record (no inverter_type) with hybrid False -> ignored
def test_hybrid_ignores_non_automatic_report(): # Review Focus 2: report automatic False with hybrid False -> no write
def test_hybrid_no_write_when_unchanged():      # current value already equals the decision -> no write
def test_hybrid_write_not_repeated_while_pending():  # Review Focus 1: two changed reports before the stub applies the first write -> one write
def test_hybrid_runs_without_opt_in():          # a component that never called allow_automatic() still drives the rule
def test_hybrid_never_raises():                 # ha_interface.set_state_external raising -> no exception, one Warn line
def test_hybrid_log_line():                     # exactly "Coordinator: inverter_hybrid set off - gecloud:a reports it is not a hybrid inverter"
                                                # and for on: "Coordinator: inverter_hybrid set on - gecloud:a, gecloud:b report they are hybrid inverters"
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement.**
  - Add `"hybrid"` to the inverters' structural fields and `hybrid=None` to `inverter_record()`. Validation keeps a bool structural value already; make a non-bool `hybrid` drop with a log line (it is the only structural field that must be a bool).
  - `hybrid_decision()`: over every stored report whose cleaned `automatic` is true, every inverter record with an `inverter_type`: any `hybrid is False` -> `(False, those device_ids)`; else any `hybrid is True` -> `(True, those device_ids)`; else `(None, [])`.
  - `apply_hybrid()`: compare the decision with the current value (`self.base.get_arg("inverter_hybrid", True)`) and with a pending value it keeps itself; when it differs from both, record it as pending and schedule `self.base.ha_interface.set_state_external("switch.{prefix}_inverter_hybrid", value)` without awaiting. Scheduling: run the coroutine on Predbat's event loop when `self.base` exposes one (look at how `ha.py` / `hass.py` run coroutines from other threads, for example `asyncio.run_coroutine_threadsafe` against the main loop, or `base.create_task`), falling back to running it to completion only when there is no running loop at all (the command-line harness). Clear the pending value once the current value equals it. Log the exact line on scheduling. Wrapped so it never raises: an exception logs `"Warn: Coordinator: could not set inverter_hybrid: {e}"`.
  - On a thin base (no `ha_interface`, no `get_arg`, no `prefix`) it does nothing.
  - `report()`: after storing, call `apply_hybrid()` when the cleaned report changed, outside the reports lock (the same place it calls `configure()`), regardless of opt-in.

- [ ] **Step 4: Run `coordinator`, `discovery_automatic`, `discovery_catalogue` (its observe-only test must still pass: it checks args, and nothing there reports `hybrid`), then commit.** `git commit -m "feat(discovery): inverter records report hybrid and discovery sets inverter_hybrid"`

---

### Task 2: GE Cloud, GivTCP and Gateway report hybrid from the model

**Files:**
- Modify: `apps/predbat/utils.py` (one shared function), `apps/predbat/gecloud.py` (`build_discovery`, `async_automatic_config` hybrid block about lines 1898-1913), `apps/predbat/givtcp.py` (its `build_discovery`), `apps/predbat/gateway.py` (its `build_discovery`, and the hybrid block about lines 2708-2723)
- Test: `apps/predbat/tests/test_ge_cloud.py`, `tests/test_givtcp_component.py`, `tests/test_gateway.py`

**Interfaces:**
- Consumes: `inverter_record(..., hybrid=...)` from Task 1.
- Produces: `utils.givenergy_model_is_hybrid(model) -> bool | None`: `None` for an empty or non-string model; `False` when the lower-cased model contains `ac`, `aio` or `all-in-one` (exactly the existing GE Cloud and Gateway test); else `True`.

- [ ] **Step 1: Failing tests.** `givenergy_model_is_hybrid("GIV-HY5.0") is True`, `("GIV-AC3.0") is False`, `("GIV-AIO") is False`, `("All-in-One") is False`, `("") is None`, `(None) is None`. GE Cloud: a record per driven device carries `hybrid` from that device's own model (`self.info[device]["info"]["model"]`; for a gateway record, False if any fronted battery's model is not hybrid, True if all known are hybrid, else omitted); a PV-only record carries no `hybrid`; `async_automatic_config()` no longer calls `set_state_external` (assert the mock's call list) and no longer logs "Detected inverter model". GivTCP: the record carries `hybrid` from the inverter's model or type with the same function (find where `givtcp.py` holds the model/type - `raw.invertor.model` can be a numeric code, so use the readable type/model string; if only a numeric code is available for a given inverter, omit `hybrid`). Gateway: records carry `hybrid` from `inv.model`; the `set_state_wrapper(hybrid_entity, ...)` write and its log line are gone.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**, replacing the two inline model tests with the shared function.
- [ ] **Step 4: Update the golden test for GE Cloud**: its `hybrid` expectation is now the record's `hybrid` field (same values as the captured switch value), not a `set_state_external` call. Expected values do not change.
- [ ] **Step 5: Run `ge_cloud`, `givtcp_component`, `gateway`, `discovery_contract`, `discovery_automatic`; commit.** `git commit -m "feat(discovery): GE Cloud, GivTCP and Gateway report hybrid from the inverter model"`

---

### Task 3: AlphaESS, Sigenergy, Teslemetry and Hanchu report hybrid

**Files:**
- Modify: `apps/predbat/alphaess.py` (`apply_hybrid_verdict` about line 770 and its caller; `build_discovery`), `apps/predbat/sigenergy.py` (`apply_hybrid_verdict` about line 1134; `build_discovery`), `apps/predbat/teslemetry.py` (the hybrid block about lines 1228-1247; `build_discovery`), `apps/predbat/hanchu.py` (`build_discovery`)
- Test: each component's existing test module (`test_alphaess_api.py`, `test_sigenergy.py`, `test_teslemetry.py`, `test_hanchu_publish.py` or whichever holds its discovery tests)

**Interfaces:** consumes `inverter_record(..., hybrid=...)`.

- [ ] **Step 1: Failing tests.**
  - AlphaESS and Sigenergy: each record's `hybrid` is `not detect_ac_coupled(serial)` when that returns True/False, omitted when None; `apply_hybrid_verdict` no longer writes `inverter_hybrid` (remove it and its call if nothing else remains in it).
  - Teslemetry: the record's `hybrid` is exactly the value the current code writes, including `teslemetry_hybrid` overriding either way and `False` when the Powerwall model is unknown (ruling, below); the `set_state_external` write and its log line are gone (keep the reason in a docstring).
  - Hanchu: `hybrid` False when `HANCHU_AC_COUPLED_FIELD` says AC coupled (value 1), True when the field is present with another value, omitted when absent. Read where Hanchu already reads that field (about line 636) and keep the value it needs for the record.
- [ ] **Step 2-4: Run to fail, implement, run the four modules plus `discovery_contract`; commit.** `git commit -m "feat(discovery): AlphaESS, Sigenergy, Teslemetry and Hanchu report hybrid"`

Ruling (plan author, 14 Nov 2026): the design table says Teslemetry reports "unknown when the model is unknown", but its current code deliberately turns hybrid off then (an AC-coupled Powerwall is the common case, and hybrid on would invent clipping, GH#5275). Teslemetry reports `False` in that case so the behaviour does not change. Cost if wrong: a Powerwall 3 whose model is not reported is treated as AC coupled, as today.

---

### Task 4: Enphase, SolaX, Solis and Growatt report hybrid where they can; confirm the rest

**Files:**
- Modify: `apps/predbat/enphase.py`, `apps/predbat/solax.py`, `apps/predbat/solis.py`, `apps/predbat/growatt.py` (their `build_discovery`)
- Test: each component's test module
- Read only: `deye.py`, `sunsynk.py`, `fox.py`

- [ ] **Step 1: For each of the four, find the evidence the design names, and write a failing test per case.**
  - Enphase: `False` when the site reports AC batteries (`hasAcb`, or IQ Battery / Encharge storage - read how `enphase.py` already reads the site flags near line 1442); omitted otherwise.
  - SolaX: from its model code table (about lines 253-262): `False` for `X1-AC`; `True` for the codes whose name contains `Hybrid` (and `A1-Hybrid`); omitted for the rest.
  - Solis: from its model naming (about line 275): `True` for the hybrid and storage ranges the code already recognises (EH/EA/RHI/RAI); omitted otherwise.
  - Growatt: only if its API data carries a device type that distinguishes hybrid (SPH) from AC-coupled (SPA); if it does not, omit and say so in the report.
- [ ] **Step 2: Confirm Deye, Sunsynk and Fox report no `hybrid`** (their code states they cannot tell); add one assertion each to their existing record tests that `"hybrid" not in record`.
- [ ] **Step 3-4: Implement, run the seven modules plus `discovery_contract`; commit.** `git commit -m "feat(discovery): Enphase, SolaX, Solis and Growatt report hybrid where their data says"`

---

### Task 5: GE Cloud Data takes its serial from discovery and declines behind an EMS

**Files:**
- Modify: `apps/predbat/gecloud.py` (`GECloudData.run()` about line 4170 and `download_ge_data()` about line 4288; `GECloudDirect.async_automatic_config()` about lines 1873-1896 and `initialize()` about lines 862-863)
- Test: `apps/predbat/tests/test_ge_cloud.py`

**Interfaces:**
- Consumes: `ComponentBase.discovery_inverter_records("gecloud")` from Task 1.
- Produces: `GECloudData.resolve_serial() -> str | None`; `GECloudData.behind_ems() -> bool`.

- [ ] **Step 1: Failing tests.**

```python
def test_cloud_data_serial_from_apps_yaml():    # ge_cloud_serial set by the user wins over discovery
def test_cloud_data_serial_from_record():       # no ge_cloud_serial; gecloud direct record "gecloud:ce1111g111" -> "ce1111g111"
def test_cloud_data_serial_from_gateway():      # gateway record with serials ["b1", "b2"] -> "b1"
def test_cloud_data_serial_lowest_slot():       # two records, coordinator slot_map puts gecloud:b at 0 -> b's serial
def test_cloud_data_serial_arrives_late():      # Review Focus 3: first run with no record -> no download, no error status, the existing "ge_cloud_serial is not set" error not logged every cycle; a record arrives -> the next cycle downloads
def test_cloud_data_ems_comes_and_goes():       # Review Focus 4: records with composition "ems" -> run() skips the download, logs once "GECloud: GE Cloud Data is not used behind a GivEnergy EMS - skipping the history download"; EMS no longer reported -> downloads resume; ge_cloud_data never written
def test_gecloud_sets_neither():                # async_automatic_config() with and without an EMS writes neither ge_cloud_serial nor ge_cloud_data
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
  - `GECloudData.resolve_serial()`: the user's value (`self.get_arg(self.ge_cloud_serial_config_item, default="")`, as now) when non-empty; else the GE Cloud record in the lowest slot (use `self.base.components.coordinator.slot_map` through a small `ComponentBase` helper if you need the slot order, else report order), `serials[0]` for a record that has structural `serials`, else `hardware_ids["serial"]`; else None. Called before each download, not only on the first run.
  - `GECloudData.behind_ems()`: any GE Cloud record with `composition == "ems"`.
  - `run()`: keep the `ge_cloud_data` check; then skip (logging once per EMS episode) while `behind_ems()`; then download with `resolve_serial()`. With no serial yet, skip quietly until one appears; log the existing error only when there is neither a user value nor any GE Cloud report after the first 10 minutes.
  - `GECloudDirect`: delete the `ge_cloud_serial` writes, the EMS `ge_cloud_data` block and the `ems_disabled_ge_cloud_data` / `ge_cloud_data_before_ems` state (constructor and `MockGECloudDirect`). Keep the "EMS detected, using EMS for control" log line.
  - Rework the existing tests that asserted the old writes (for example `test_ge_cloud.py` about line 7977 and the "restores ge_cloud_data" test) onto the new behaviour; do not weaken them.
- [ ] **Step 4: Run `ge_cloud`, `discovery_automatic`, `gecloud_budget_harness` and `gecloud_api_budget` (the slow one, by name; it must stay byte-identical - if the call log changes because the download starts later or earlier, report it with the diff rather than editing the expectation); commit.** `git commit -m "feat(gecloud): GE Cloud Data finds its serial from discovery and declines behind an EMS"`

---

### Task 6: Discovery writes the invert settings per slot

**Files:**
- Modify: `apps/predbat/bindings.py` (`BINDING_SETTINGS`, `desired_args` / `_discovered_value`), `apps/predbat/tests/test_discovery_automatic.py` (`compare_configuration`)
- Test: `apps/predbat/tests/test_bindings.py`, `apps/predbat/tests/test_discovery_automatic.py`

**Interfaces:**
- Produces: `bindings.INVERT_SETTINGS = {"battery_power_invert": "battery_power", "grid_power_invert": "grid_power", "load_power_invert": "load_power"}`; the three names join `BINDING_SETTINGS`.

- [ ] **Step 1: Failing tests.**

```python
def test_replay_invert_from_descriptor():       # discovered slot whose grid_power descriptor has invert True -> grid_power_invert[slot] is True; battery/load False
def test_replay_invert_defaults_false():        # no invert on any descriptor -> all three written False at the slot
def test_replay_invert_leaves_manual_slot():    # Review Focus 5: [discovered, manual, discovered]; manual's live invert entries untouched, no write at index 1
def test_replay_invert_idempotent():            # a second replay writes nothing
def test_invert_not_scanned_for_serials():      # bool values never reach slot_serials
```
  In the acceptance harness, `compare_configuration` treats an unset invert setting as equal to all-False and nothing else; add one acceptance assertion that a GE Cloud run writes `[False]` for each of the three on one battery.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**: for a discovered slot, `INVERT_SETTINGS` member value = `bool(record entity descriptor for its partner setting .get("invert", False))`; manual slots keep live args as for every other non-summed setting; the three are slot-indexed lists. Check `test_discovery_contract`'s ownership check still passes (the invert names are not entity keys, so nothing should change).
- [ ] **Step 4: Run `bindings`, `discovery_automatic`, `ge_cloud`, `discovery_contract`; commit.** `git commit -m "feat(discovery): discovery writes the invert settings from each slot's record"`

---

### Task 7: Review output, docs and full verification

**Files:**
- Modify: `apps/predbat/gecloud.py` (`discovery_review_lines()`), `docs/discovery-catalogue.md`, `docs/components.md`, `tools/debug-journal.md`
- Test: `apps/predbat/tests/test_ge_cloud.py` (`_test_discovery_review_lines`)

- [ ] **Step 1:** `discovery_review_lines()` gains a "Site settings" section after "Inverter configuration": `inverter_hybrid: on|off|unchanged (<deciding device ids>)` from `hybrid_decision()`. Extend its unit test.
- [ ] **Step 2:** Docs: `docs/discovery-catalogue.md` - the `hybrid` field and who reports it (the design's table as built), the site rule, that GE Cloud Data takes its serial from discovery and declines behind an EMS, and the invert settings. `docs/components.md` - GE Cloud no longer sets `ge_cloud_serial` or changes `ge_cloud_data`; each component that reports hybrid no longer sets the switch itself. Debug journal: one entry that `inverter_hybrid` is written by the coordinator, scheduled, so a test reading the switch straight after a report must let the scheduled write run.
- [ ] **Step 3:** From `coverage/`: `./run_pre_commit` (all hooks plus the quick suite), then the full suite once (`./run_all > out_full.txt 2>&1; echo exit=$?`, in the background; about 21 minutes). Expected: both pass.
- [ ] **Step 4:** `detect_changes({scope: "compare", base_ref: "origin/feat/discovery-automatic-config"})`; list the changed symbols in the report. Commit. `git commit -m "docs(discovery): hybrid, GE Cloud Data serial and invert settings"`
