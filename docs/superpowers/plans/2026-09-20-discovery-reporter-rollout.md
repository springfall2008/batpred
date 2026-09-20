# Discovery Reporter Rollout — Plan 1: Shared Inverter Record + Fox

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a shared inverter-record builder from the two existing inverter reporters, then use it to add the first new one (Fox) — taking the discovery catalogue from GivEnergy-only to covering a third vendor.

**Architecture:** `coordinator.py` gains a pure `inverter_record()` function that assembles one `inverters`-section record and drops every empty container, so the "only include this key if non-empty" ladder is written once rather than per reporter. GivTCP and GE Cloud are migrated onto it (no behaviour change, proven by their existing tests), and Fox becomes its first new consumer. Fox's reporter reads the data `automatic_config()` already gathers — `self.device_list`, `self.device_detail`, `self.device_settings` — so no new API calls are added.

**Tech Stack:** Python 3.14, no new dependencies. Tests via `coverage/run_all`, quality via `coverage/run_pre_commit`.

**Spec:** `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md` (the v1 observe-only catalogue design), plus the reporter contract in `docs/discovery-catalogue.md` under "Writing a reporter".

## Global Constraints

- **Observe-only.** Nothing in this plan may change any component's behaviour. No task writes `self.args`, calls `set_arg_auto()`, publishes an entity, or alters an existing control path. A reporter reads state that already exists.
- **A reporter never degrades its component.** Never call `non_fatal_error_occurred()` from discovery code: it sets `base.had_errors`, which makes `update_pred()` skip `record_status()` and suppress the run notification. `ComponentBase.refresh_discovery()` already catches and logs; do not add a second try/except around it.
- **A reporter is `build_discovery()` plus one unconditional `self.refresh_discovery()` call in `run()`.** Do not add a marker attribute, a state-key snapshot, or a try/except — `ComponentBase.refresh_discovery()` owns all of that. Return `None` from `build_discovery()` when there is nothing to describe yet.
- **Report only entities that exist.** Build descriptors and pass them through `self.discovery_entities(descriptors)`, which keeps only those Home Assistant has actually seen.
- **Never invent a record to satisfy a cross-link.** A dangling `measures_meter` is correct; a fabricated `meters` record is not.
- **Typed containers.** `ratings` accepts numbers and booleans ONLY. `info` accepts bounded vendor strings. `hardware_ids` accepts bounded strings. `account_ids` is pseudonymised on output. `functions`/`capabilities`/`flags`/`effects` are lists of lowercase tokens matching `^[a-z0-9_]{1,32}$`. A value that does not fit its container is silently dropped by `validate_report()` — so putting a model name in `ratings` means losing it.
- **Line length:** 256 (Black), 250 (Flake8).
- **Docstrings:** every function and class needs one (`interrogate`, 100%).
- **Spelling:** British English via cspell. Add genuinely new words to `.cspell/custom-dictionary-workspace.txt` (auto-sorted on commit, so re-stage after).
- **`git add` new files BEFORE running pre-commit.** `--all-files` means git-tracked only, so an untracked new test file passes unchecked.
- **Tests run from `coverage/`:** `./run_all --test <name>`. Save output to a file and grep it; do not pipe to grep directly.
- **Scope `git add` to the files you changed.** A GitNexus re-index can regenerate banners into `CLAUDE.md`/`AGENTS.md`/`.claude/skills/**`; never commit those incidentally.

---

## File Structure

| File | Responsibility | Change |
|------|---------------|--------|
| `apps/predbat/coordinator.py` | Catalogue schema, validation, redaction | **Modify** — add `inverter_record()` |
| `apps/predbat/givtcp.py` | GivTCP reporter | **Modify** — build records via `inverter_record()` |
| `apps/predbat/gecloud.py` | GE Cloud reporter | **Modify** — build records via `inverter_record()` |
| `apps/predbat/fox.py` | Fox Cloud component | **Modify** — add `build_discovery()` + `refresh_discovery()` call |
| `apps/predbat/tests/test_coordinator.py` | Coordinator unit tests | **Modify** — tests for `inverter_record()` |
| `apps/predbat/tests/test_fox_api.py` | Fox unit tests | **Modify** — tests for the Fox reporter |
| `docs/discovery-catalogue.md` | User + developer docs | **Modify** — document the builder and Fox |

---

## Task 1: `inverter_record()` in the coordinator

**Files:**
- Modify: `apps/predbat/coordinator.py`
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `inverter_record(device_id, inverter_type=None, control=None, composition=None, measures_meter=None, serials=None, functions=None, capabilities=None, flags=None, effects=None, hardware_ids=None, account_ids=None, info=None, ratings=None, coverage=None, entities=None) -> dict`. Every parameter except `device_id` is optional; any that is `None` or empty is omitted from the returned dict. Tasks 2, 3 and 4 import it as `from coordinator import inverter_record`.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_coordinator.py`, immediately before `def test_assemble_merges_sections_and_tags_source():`

```python
def test_inverter_record_omits_empty_containers():
    """An empty or unset container is left out of the record rather than written as {} or [].

    Every reporter previously wrote its own `if info: record["info"] = info` ladder. Centralising
    it means a reporter can pass whatever it gathered and let the builder decide, and the shape of
    an inverters record lives in exactly one place.
    """
    record = inverter_record("fox:ABC123", inverter_type="FOX", composition="direct", functions=["solar", "battery"], info={}, ratings={}, entities={}, serials=[])
    assert record == {"device_id": "fox:ABC123", "inverter_type": "FOX", "composition": "direct", "functions": ["solar", "battery"]}, record
    print("PASS: inverter_record omits empty containers")
    return 0


def test_inverter_record_keeps_everything_populated():
    """Every populated field survives, including a falsy-but-real rating like 0."""
    record = inverter_record(
        "fox:ABC123",
        inverter_type="FOX",
        composition="gateway",
        serials=["S1", "S2"],
        measures_meter="fox:meter:M1",
        functions=["solar"],
        capabilities=["export_limit"],
        hardware_ids={"serial": "ABC123"},
        info={"model": "H3-10.0"},
        ratings={"battery_kwh": 10.4, "max_charge_w": 0},
        entities={"charge_rate": {"entity_id": "number.fox_abc123_charge_rate", "domain": "number", "access": "rw"}},
    )
    assert record["serials"] == ["S1", "S2"]
    assert record["measures_meter"] == "fox:meter:M1"
    assert record["capabilities"] == ["export_limit"]
    assert record["hardware_ids"] == {"serial": "ABC123"}
    assert record["ratings"]["max_charge_w"] == 0, "a real zero rating is data, not an empty container"
    assert record["entities"]["charge_rate"]["domain"] == "number"
    print("PASS: inverter_record keeps every populated field")
    return 0


def test_inverter_record_round_trips_through_validation():
    """A record the builder produced survives validate_report() unchanged.

    The builder's whole job is producing something the coordinator will accept. If a field name
    here ever drifts from SECTION_SPEC's structural list, validation silently drops it - so pin
    that the two agree rather than trusting they do.
    """
    record = inverter_record("fox:ABC123", inverter_type="FOX", composition="direct", serials=["S1"], measures_meter="fox:meter:M1", functions=["solar"], hardware_ids={"serial": "ABC123"}, info={"model": "H3"}, ratings={"battery_kwh": 10.4})
    cleaned = validate_report({"inverters": [record]}, "fox", print)["inverters"][0]
    for field in ("device_id", "inverter_type", "composition", "serials", "measures_meter", "functions", "hardware_ids", "info", "ratings"):
        assert field in cleaned, "{} was dropped by validate_report - builder and SECTION_SPEC disagree: {}".format(field, cleaned)
    print("PASS: a built record survives validation with every field intact")
    return 0
```

Register all three in `run_coordinator_tests()`, immediately after the line `failures += test_assemble_component_status()`:

```python
    failures += test_inverter_record_omits_empty_containers()
    failures += test_inverter_record_keeps_everything_populated()
    failures += test_inverter_record_round_trips_through_validation()
```

Extend the existing import at `apps/predbat/tests/test_coordinator.py:8` — it currently reads:

```python
from coordinator import Coordinator, Redactor, SCHEMA_VERSION, SECTION_SPEC
```

and must become:

```python
from coordinator import Coordinator, Redactor, SCHEMA_VERSION, SECTION_SPEC, inverter_record, validate_report
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd coverage
source /Users/treforsouthwell/predbat/batpred/coverage/venv/bin/activate
python3 ../apps/predbat/unit_test.py --test coordinator > /tmp/t1.log 2>&1; grep -E "ImportError|cannot import|ERROR" /tmp/t1.log | head
```

Expected: FAIL with `cannot import name 'inverter_record' from 'coordinator'`.

- [ ] **Step 3: Write the implementation**

In `apps/predbat/coordinator.py`, add immediately before `def validate_report(report, component_name, log):`

```python
def inverter_record(device_id, inverter_type=None, control=None, composition=None, measures_meter=None, serials=None, functions=None, capabilities=None, flags=None, effects=None, hardware_ids=None, account_ids=None, info=None, ratings=None, coverage=None, entities=None):
    """Assemble one inverters-section record, omitting every field that is unset or empty.

    The parameter list IS the inverters section's schema, spelled out rather than taken as
    **kwargs: a mistyped field name is then a TypeError a test catches at the call site, instead
    of a key that reaches validate_report() and is silently dropped from a user's dump.

    Empty containers are omitted rather than written as {} or []. Every reporter previously
    carried its own `if info: record["info"] = info` ladder, which is how a record ends up
    carrying `"ratings": {}` in one component and omitting it in another. A falsy value that is
    real data - a rating of 0 - is kept: only None and empty containers are dropped.

    Tuples are normalised to lists so a caller can pass a module-level constant without it
    reaching the catalogue as a tuple, which neither JSON nor YAML serialises as a list.
    """
    fields = {
        "inverter_type": inverter_type,
        "control": control,
        "composition": composition,
        "measures_meter": measures_meter,
        "serials": serials,
        "functions": functions,
        "capabilities": capabilities,
        "flags": flags,
        "effects": effects,
        "hardware_ids": hardware_ids,
        "account_ids": account_ids,
        "info": info,
        "ratings": ratings,
        "coverage": coverage,
        "entities": entities,
    }
    record = {"device_id": device_id}
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple, set)) and not value:
            continue
        record[name] = list(value) if isinstance(value, (tuple, set)) else value
    return record


```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test coordinator > /tmp/t1.log 2>&1; grep -E "PASS: inverter_record|PASS: a built record|failures" /tmp/t1.log
```

Expected: all three PASS lines.

- [ ] **Step 5: Verify the tests are load-bearing**

Temporarily change `if isinstance(value, (dict, list, tuple, set)) and not value:` to `if False:`, re-run, and confirm `test_inverter_record_omits_empty_containers` fails. Restore the line afterwards and re-run to confirm green.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/coordinator.py apps/predbat/tests/test_coordinator.py
git commit -m "feat(discovery): a shared builder for inverters-section records

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Migrate GivTCP onto the builder

**Files:**
- Modify: `apps/predbat/givtcp.py` (the record assembly inside `build_discovery()`)
- Test: `apps/predbat/tests/test_givtcp_component.py` (existing tests must pass unchanged)

**Interfaces:**
- Consumes: `inverter_record(...)` from Task 1.
- Produces: no new interface. GivTCP's reported records must be byte-identical to before.

This task is a refactor with no behaviour change. GivTCP's existing discovery tests are the proof — do not modify them. If one fails, the refactor is wrong, not the test.

- [ ] **Step 1: Confirm the existing tests pass before touching anything**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test givtcp_component > /tmp/t2-before.log 2>&1; grep -E "RESULTS|ERROR" /tmp/t2-before.log
```

Expected: all pass. Record the count; it must not change.

- [ ] **Step 2: Add the import**

In `apps/predbat/givtcp.py`, add to the existing imports near the top:

```python
from coordinator import inverter_record
```

- [ ] **Step 3: Replace the record assembly**

In `build_discovery()`, replace this block:

```python
            record = {
                "device_id": device_id,
                "inverter_type": "GE",
                "composition": "direct",
                "functions": ["solar", "battery"],
                "capabilities": capabilities,
                "entities": entities,
            }
            if known_serial:
                record["hardware_ids"] = {"serial": known_serial}
            if info:
                record["info"] = info
            if ratings:
                record["ratings"] = ratings
            inverters.append(record)
```

with:

```python
            inverters.append(
                inverter_record(
                    device_id,
                    inverter_type="GE",
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=capabilities,
                    hardware_ids={"serial": known_serial} if known_serial else None,
                    info=info,
                    ratings=ratings,
                    entities=entities,
                )
            )
```

- [ ] **Step 4: Run the GivTCP and end-to-end discovery tests**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test givtcp_component --test discovery_catalogue > /tmp/t2.log 2>&1; grep -E "RESULTS|ERROR" /tmp/t2.log
```

Expected: same pass count as Step 1, zero failures.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/givtcp.py
git commit -m "refactor(discovery): build GivTCP inverter records with the shared builder

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Migrate GE Cloud onto the builder

**Files:**
- Modify: `apps/predbat/gecloud.py` (both record loops inside `build_discovery()`)
- Test: `apps/predbat/tests/test_ge_cloud.py` (existing tests must pass unchanged)

**Interfaces:**
- Consumes: `inverter_record(...)` from Task 1.
- Produces: no new interface. GE Cloud's reported records must be byte-identical to before.

GE Cloud is the harder migration because it builds two kinds of record and applies a cross-link via a helper that mutates the record in place. The `_apply_meter_cross_link` call sets `record["measures_meter"]`, so it must still run against the finished dict.

- [ ] **Step 1: Confirm the existing tests pass before touching anything**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test ge_cloud > /tmp/t3-before.log 2>&1; grep -E "RESULTS|ERROR" /tmp/t3-before.log
```

- [ ] **Step 2: Add the import**

In `apps/predbat/gecloud.py`:

```python
from coordinator import inverter_record
```

- [ ] **Step 3: Replace the controlled-device loop**

Replace:

```python
        for device in controlled:
            info, ratings = self._device_info_and_ratings(device)
            record = {
                "device_id": "gecloud:{}".format(device),
                "inverter_type": inverter_type,
                "composition": composition,
                "functions": ["solar", "battery"],
                "hardware_ids": {"serial": device},
            }
            if fronted_serials:
                record["serials"] = fronted_serials
            capabilities = self._device_capabilities(device)
            if capabilities:
                record["capabilities"] = capabilities
            self._apply_meter_cross_link(devices, device, record)
            if info:
                record["info"] = info
            if ratings:
                record["ratings"] = ratings
            inverters.append(record)
```

with:

```python
        for device in controlled:
            info, ratings = self._device_info_and_ratings(device)
            record = inverter_record(
                "gecloud:{}".format(device),
                inverter_type=inverter_type,
                composition=composition,
                functions=["solar", "battery"],
                capabilities=self._device_capabilities(device),
                hardware_ids={"serial": device},
                serials=fronted_serials,
                info=info,
                ratings=ratings,
            )
            # Sets record["measures_meter"] in place when GE Cloud reports a CT/meter serial for
            # this device - after the build, since it is a cross-link derived from other devices
            # rather than a property of this one.
            self._apply_meter_cross_link(devices, device, record)
            inverters.append(record)
```

- [ ] **Step 4: Replace the PV-only loop**

Replace:

```python
        for device in devices.get("pv") or []:
            info, ratings = self._device_info_and_ratings(device)
            record = {
                "device_id": "gecloud:{}".format(device),
                "composition": "direct",
                "functions": ["solar"],
                "hardware_ids": {"serial": device},
            }
            if info:
                record["info"] = info
            if ratings:
                record["ratings"] = ratings
            inverters.append(record)
```

with:

```python
        for device in devices.get("pv") or []:
            info, ratings = self._device_info_and_ratings(device)
            inverters.append(
                inverter_record(
                    "gecloud:{}".format(device),
                    composition="direct",
                    functions=["solar"],
                    hardware_ids={"serial": device},
                    info=info,
                    ratings=ratings,
                )
            )
```

Note the PV-only record deliberately has no `inverter_type` — a sensor-only PV device is not an inverter Predbat controls. `inverter_record()` omits it because it is `None`.

- [ ] **Step 5: Run the GE Cloud and end-to-end tests**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test ge_cloud --test discovery_catalogue > /tmp/t3.log 2>&1; grep -E "RESULTS|ERROR" /tmp/t3.log
```

Expected: same pass count as Step 1, zero failures.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/gecloud.py
git commit -m "refactor(discovery): build GE Cloud inverter records with the shared builder

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: The Fox reporter

**Files:**
- Modify: `apps/predbat/fox.py` (add `build_discovery()`, add the `refresh_discovery()` call in `run()`)
- Test: `apps/predbat/tests/test_fox_api.py`

**Interfaces:**
- Consumes: `inverter_record(...)` from Task 1; `self.refresh_discovery()` and `self.discovery_entities(descriptors)` from `ComponentBase`.
- Produces: `FoxAPI.build_discovery() -> dict | None`, returning `{"automatic": bool, "inverters": [...]}` or `None` when no devices have been discovered yet.

**Background the implementer needs:**

Fox already gathers everything the reporter needs, in the same data `automatic_config()` reads:

- `self.device_list` — list of dicts, each with a `deviceSN` key.
- `self.device_detail[sn]` — dict with `hasPV` (bool), `hasBattery` (bool), `thirdPartyGen` (bool), `capacity` (inverter kW, so `* 1000` for watts), `deviceType` (model string), `productType`, `stationName`, `function` (dict, `function["scheduler"]` is a bool), and `batteryList` (list of dicts each with `capacity`).
- `self.device_settings[sn]` — settings the device has reported, keyed by name; `FOX_SETTINGS = ["ExportLimit", "MaxSoc", "GridCode", "WorkMode", "MinSoc", "MinSocOnGrid"]`.

**Do not sum `batteryList` capacities.** `publish_data()` does (`fox.py:1852`) and it is a known live bug (GH#4919): an AIO ESS returns one physical pack as four `bmu` entries all carrying the inverter's own serial, so the sum comes out 4× the real capacity. Report `batteryList` entry **count** as a `ratings` number instead, and let the catalogue show maintainers how often a fleet reports more battery entries than physical packs. That count is the evidence the bug needs, and reporting a knowingly-wrong capacity would put a 4×-too-large `battery_kwh` into users' dumps.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_fox_api.py`, at the end of the file but before any `run_*_tests` aggregator:

```python
def _fox_discovery_api(my_predbat):
    """A FoxAPI carrying one battery inverter and one PV-only device, as the cloud would report them."""
    fox = FoxAPI(my_predbat, key="test_key", automatic=False)
    fox.component_name = "fox"
    fox.device_list = [{"deviceSN": "BATT001"}, {"deviceSN": "PVONLY1"}]
    fox.device_detail = {
        "BATT001": {
            "hasPV": True,
            "hasBattery": True,
            "thirdPartyGen": False,
            "capacity": 10.0,
            "deviceType": "H3-10.0",
            "function": {"scheduler": True},
            "batteryList": [{"capacity": 2.6}, {"capacity": 2.6}, {"capacity": 2.6}, {"capacity": 2.6}],
        },
        "PVONLY1": {"hasPV": True, "hasBattery": False, "capacity": 5.0, "deviceType": "S1-5.0", "function": {}},
    }
    # {deviceSN: {SettingName: {"value": ...}}} - the shape fox.py reads at line 1251
    fox.device_settings = {"BATT001": {"ExportLimit": {"value": 5000}, "WorkMode": {"value": "SelfUse"}}, "PVONLY1": {}}
    return fox


def test_fox_build_discovery_describes_each_device(my_predbat):
    """One record per discovered device, with the battery inverter and the PV-only device distinguished."""
    print("**** test_fox_build_discovery_describes_each_device ****")
    fox = _fox_discovery_api(my_predbat)

    report = fox.build_discovery()

    assert report["automatic"] is False, "the report carries the component's automatic flag, whatever it is"
    by_id = {record["device_id"]: record for record in report["inverters"]}
    assert set(by_id) == {"fox:BATT001", "fox:PVONLY1"}, by_id

    battery = by_id["fox:BATT001"]
    assert battery["inverter_type"] == "FOX"
    assert sorted(battery["functions"]) == ["battery", "solar"]
    assert battery["hardware_ids"] == {"serial": "BATT001"}
    assert battery["info"]["model"] == "H3-10.0"
    assert battery["ratings"]["inverter_w"] == 10000.0
    assert "scheduler" in battery["capabilities"]
    assert "export_limit" in battery["capabilities"]

    pv = by_id["fox:PVONLY1"]
    assert pv["functions"] == ["solar"], "a device with no battery is solar only"
    assert "inverter_type" not in pv, "a PV-only device is not an inverter Predbat controls"
    print("PASS: Fox build_discovery describes each discovered device")
    return 0


def test_fox_build_discovery_counts_battery_entries_rather_than_summing_them(my_predbat):
    """batteryList entries are counted, never summed into a capacity.

    GH#4919: an AIO ESS returns one physical pack as four bmu entries all carrying the inverter's
    own serial, so publish_data()'s sum comes out 4x the real capacity. Reporting that sum would
    put a knowingly-wrong battery_kwh into every affected user's dump. The COUNT is the evidence
    the bug needs - a fleet reporting four entries against one pack is exactly what a maintainer
    wants to see - so report that and no capacity at all.
    """
    print("**** test_fox_build_discovery_counts_battery_entries_rather_than_summing_them ****")
    fox = _fox_discovery_api(my_predbat)

    battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]

    assert battery["ratings"]["battery_entries"] == 4
    assert "battery_kwh" not in battery["ratings"], "the summed capacity is known to be wrong - do not report it"
    assert 10.4 not in battery["ratings"].values(), "4 x 2.6 is the bug, not a rating"
    print("PASS: Fox reports the battery entry count, not the known-wrong sum")
    return 0


def test_fox_build_discovery_returns_none_before_discovery(my_predbat):
    """With no devices found yet there is nothing to describe, so nothing is reported."""
    print("**** test_fox_build_discovery_returns_none_before_discovery ****")
    fox = FoxAPI(my_predbat, key="test_key", automatic=False)
    fox.device_list = []

    assert fox.build_discovery() is None, "an empty device list means 'ask me again later', not an empty report"
    print("PASS: Fox reports nothing before it has discovered anything")
    return 0


def test_fox_run_reports_discovery_and_survives_a_failure(my_predbat):
    """run() files a report, and a broken build_discovery() cannot degrade the component.

    refresh_discovery() owns the guard, so this pins the wiring rather than re-testing the loop:
    a report reaches the coordinator on a normal cycle, and a raising build_discovery() neither
    propagates out of run() nor sets had_errors.
    """
    print("**** test_fox_run_reports_discovery_and_survives_a_failure ****")
    fox = _fox_discovery_api(my_predbat)
    reports = []
    fox.report_discovery = lambda report: reports.append(report)

    fox.refresh_discovery()
    assert len(reports) == 1, f"expected the report to be filed, got {len(reports)}"

    my_predbat.had_errors = False
    fox.build_discovery = MagicMock(side_effect=Exception("boom"))
    fox.refresh_discovery()  # must not raise
    assert not my_predbat.had_errors, "a discovery failure must never set had_errors - that suppresses record_status()"
    print("PASS: Fox reports on a normal cycle and contains a reporter failure")
    return 0
```

Register them in `run_fox_api_tests(my_predbat)` (`apps/predbat/tests/test_fox_api.py:7438`, the function `unit_test.py:593` calls for the `fox_api` test), adding one line per test:

```python
    failed |= test_fox_build_discovery_describes_each_device(my_predbat)
    failed |= test_fox_build_discovery_counts_battery_entries_rather_than_summing_them(my_predbat)
    failed |= test_fox_build_discovery_returns_none_before_discovery(my_predbat)
    failed |= test_fox_run_reports_discovery_and_survives_a_failure(my_predbat)
```

`MagicMock` is already imported at `test_fox_api.py:15` — no import change is needed.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test fox_api > /tmp/t4.log 2>&1; grep -E "AttributeError|ERROR|RESULTS" /tmp/t4.log | head
```

Expected: FAIL with `'FoxAPI' object has no attribute 'build_discovery'`.

- [ ] **Step 3: Write `build_discovery()`**

In `apps/predbat/fox.py`, add the import near the other imports:

```python
from coordinator import inverter_record
```

Add this method to `FoxAPI`, immediately after `automatic_config()`:

```python
    def build_discovery(self):
        """
        Describe the discovered Fox devices for the discovery catalogue.

        Reads only what automatic_config() already gathers - self.device_list, self.device_detail
        and self.device_settings - so this adds no API calls and cannot change what Fox does.
        Reporting is independent of self.automatic: the catalogue records what hardware is there,
        not whether this component wired apps.yaml to it, which is what the report's own
        "automatic" flag is for.

        A device with no battery is reported with functions ["solar"] and no inverter_type: it is
        a generation source Predbat reads, not an inverter it controls. A battery device gets
        inverter_type "FOX" and both functions.

        batteryList entries are COUNTED, never summed. publish_data() sums them (fox.py:1852) and
        that is a known live bug (GH#4919): an AIO ESS returns one physical pack as four bmu
        entries all carrying the inverter's own serial, so the sum is 4x the real capacity.
        Publishing that figure would put a knowingly-wrong battery_kwh in every affected user's
        dump, whereas the entry count is the evidence the bug needs - a fleet reporting four
        entries against one pack is exactly what a maintainer wants to see.

        Returns None when nothing has been discovered yet, which refresh_discovery() treats as
        "nothing to report, ask again next cycle".
        """
        if not self.device_list:
            return None

        inverters = []
        for device in self.device_list:
            serial = device.get("deviceSN")
            if not serial:
                continue
            detail = self.device_detail.get(serial, {}) or {}
            has_battery = bool(detail.get("hasBattery", False))
            has_pv = bool(detail.get("hasPV", False))

            functions = []
            if has_pv:
                functions.append("solar")
            if has_battery:
                functions.append("battery")

            capabilities = []
            if detail.get("function", {}).get("scheduler", False):
                capabilities.append("scheduler")
            if detail.get("thirdPartyGen", False):
                capabilities.append("third_party_gen")
            settings = self.device_settings.get(serial, {}) or {}
            if "ExportLimit" in settings:
                capabilities.append("export_limit")

            info = {}
            device_type = detail.get("deviceType")
            if device_type:
                info["model"] = str(device_type)
            product_type = detail.get("productType")
            if product_type:
                info["product_type"] = str(product_type)

            ratings = {}
            capacity_kw = detail.get("capacity")
            if capacity_kw:
                ratings["inverter_w"] = capacity_kw * 1000.0
            battery_list = detail.get("batteryList") or []
            if battery_list:
                ratings["battery_entries"] = len(battery_list)

            inverters.append(
                inverter_record(
                    "fox:{}".format(serial),
                    inverter_type="FOX" if has_battery else None,
                    composition="direct",
                    functions=functions,
                    capabilities=capabilities,
                    hardware_ids={"serial": serial},
                    info=info,
                    ratings=ratings,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test fox_api > /tmp/t4.log 2>&1; grep -E "PASS: Fox|RESULTS|ERROR" /tmp/t4.log
```

Expected: four `PASS: Fox ...` lines, zero failures.

- [ ] **Step 5: Wire the reporter into `run()`**

`FoxAPI.run()` is at `apps/predbat/fox.py:449`. Add the following at the end of the method's main body, **before** its final `return True` and outside any `if first:` block:

```python
        # Unconditional, once per cycle and outside any one-shot gate, so a transient failure is
        # retried rather than lost - see ComponentBase.refresh_discovery(), which owns the
        # compare/guard loop. Placed after the device poll above so it describes what this cycle
        # actually read.
        self.refresh_discovery()
```

- [ ] **Step 6: Run the full suite**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py > /tmp/t4-full.log 2>&1; echo "exit=$?"; tail -2 /tmp/t4-full.log
```

Expected: `All tests passed`, exit 0. If an unrelated suite fails, check it fails on `origin/main` too before assuming this task caused it.

- [ ] **Step 7: Verify the tests are load-bearing**

Temporarily change `ratings["battery_entries"] = len(battery_list)` to `ratings["battery_kwh"] = sum(b.get("capacity", 0) for b in battery_list)`, re-run `--test fox_api`, and confirm `test_fox_build_discovery_counts_battery_entries_rather_than_summing_them` fails. Restore and re-run green.

- [ ] **Step 8: Commit**

```bash
git add apps/predbat/fox.py apps/predbat/tests/test_fox_api.py
git commit -m "feat(discovery): report Fox devices to the discovery catalogue

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Documentation and quality gate

**Files:**
- Modify: `docs/discovery-catalogue.md`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing code-facing.

- [ ] **Step 1: Document the shared builder**

In `docs/discovery-catalogue.md`, in the "Writing a reporter" section, after rule 1 (the `discovery_entities` rule, which ends at line 238 with "worse than omitting it."), add a new paragraph:

```markdown
For the `inverters` section, build each record with `inverter_record()` from `coordinator.py`
rather than assembling the dict by hand. It takes the section's fields as named parameters and
omits whatever is unset or empty, so a reporter can pass everything it gathered without writing
its own `if info: record["info"] = info` ladder. A mistyped field name is a `TypeError` at the
call site rather than a key silently dropped from a user's dump.
```

- [ ] **Step 2: Add Fox to the reporter list**

`docs/discovery-catalogue.md:68` currently reads:

```markdown
No v1 reporter (GivTCP, GE Cloud, Octopus, Ohme, Solcast) populates `programmes` yet - it is part of
```

Change the parenthesised list to `(GivTCP, GE Cloud, Octopus, Ohme, Solcast, Fox)`.

- [ ] **Step 3: Run the quality gate**

```bash
git add docs/discovery-catalogue.md
cd coverage
./run_pre_commit > /tmp/t5-pc.log 2>&1; echo "exit=$?"; grep -E "Failed$" -A5 /tmp/t5-pc.log || echo "all hooks passed"
```

If cspell flags a new word that is genuinely correct, add it to `.cspell/custom-dictionary-workspace.txt` and re-stage that file (it is auto-sorted on commit).

- [ ] **Step 4: Commit**

```bash
git add docs/discovery-catalogue.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(discovery): the shared inverter record builder, and Fox as a reporter

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Subsequent plans

This plan deliberately stops after Fox. The remaining components are **not** tasks to bolt onto the end of it: each wraps a different vendor API, and writing non-placeholder tasks for one means reading a 1,400–3,800 line module to learn what device data it actually holds. A plan that guessed at those would be worse than no plan.

The order below is from the review, ranked by maintainer cost (debug-journal mentions, a proxy for install base × trouble) and weighted up where a component would make a single-vendor section less narrow. Each gets its own plan, written when it is next up:

| Plan | Component(s) | Section | Why here |
|------|-------------|---------|----------|
| 2 | AlphaESS | `inverters` | 20 journal mentions, second-highest unreported |
| 3 | Solis | `inverters` | 15 mentions, very common inverter |
| 4 | Deye + Sunsynk | `inverters` | Sibling modules sharing a shape — one plan, not two |
| 5 | myenergi | `chargers`, `cars` | Makes `chargers` more than a single vendor, and chargers are the allocator's second axis |
| 6 | Teslemetry | `inverters` | Powerwall is AC-coupled — the first real test of the `composition` model |
| 7 | Sigenergy, SolaX, Enphase, Gateway | `inverters` | Remaining inverter coverage |
| 8 | Kraken | `meters` | The only way to learn whether the meter/tariff model is Octopus-shaped |
| 9 | Axle | `programmes` | The first reporter for a section no component populates — that schema is unvalidated design today |
| 10 | Carbon | `forecasts` | Carbon intensity alongside solar |

Two things that should be revisited once Plan 2 or 3 lands, rather than designed now:

- **Whether `inverter_record()` wants a sibling for `chargers`/`cars`/`meters`.** Those sections have one or two reporters each today; the same ladder will start repeating there once myenergi and Kraken arrive. Extract it when there is a third caller, not before.
- **Whether `capabilities` needs a shared vocabulary.** Fox contributes `scheduler`, `export_limit`, `third_party_gen`; GivTCP contributes `rest_v3`, `pause_mode`, `soh`. If two vendors coin different tokens for the same capability the section stops being comparable across a fleet, which is most of its value. Worth a pass once four or five reporters have contributed tokens.
