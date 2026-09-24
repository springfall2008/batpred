# Discovery Reporter Rollout — Plan 1: Shared Inverter Record + Fox

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a shared inverter-record builder from the two existing inverter reporters, then use it to add the first new one (Fox) — taking the discovery catalogue from GivEnergy-only to covering a third vendor.

**Architecture:** `coordinator.py` gains a pure `inverter_record()` function that assembles one `inverters`-section record and drops every empty container, so the "only include this key if non-empty" ladder is written once rather than per reporter. GivTCP and GE Cloud are migrated onto it (no change to what reaches the catalogue, proven by their existing tests - see Amendments, R1), and Fox becomes its first new consumer. Fox's reporter reads the data `automatic_config()` already gathers — `self.device_list`, `self.device_detail`, `self.device_settings` — so no new API calls are added.

**Tech Stack:** Python 3.14, no new dependencies. Tests via `coverage/run_all`, quality via `coverage/run_pre_commit`.

**Spec:** `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md` (the v1 observe-only catalogue design), plus the reporter contract in `docs/discovery-catalogue.md` under "Writing a reporter".

## Global Constraints

- **Observe-only.** Nothing in this plan may change any component's behaviour. No task writes `self.args`, calls `set_arg_auto()`, publishes an entity, or alters an existing control path. A reporter reads state that already exists.
- **A reporter never degrades its component.** Never call `non_fatal_error_occurred()` from discovery code: it sets `base.had_errors`, which makes `update_pred()` skip `record_status()` and suppress the run notification. `ComponentBase.refresh_discovery()` already catches and logs; do not add a second try/except around it.
- **A reporter is `build_discovery()` plus one unconditional `self.refresh_discovery()` call in `run()`.** Do not add a marker attribute, a state-key snapshot, or a try/except — `ComponentBase.refresh_discovery()` owns all of that. Return `None` from `build_discovery()` when there is nothing to describe yet.
- **Report only entities that exist.** Build descriptors and pass them through `self.discovery_entities(descriptors)`, which keeps only those Home Assistant has actually seen.
- **Never invent a record to satisfy a cross-link.** A dangling `measures_meter` is correct; a fabricated `meters` record is not.
- **Typed containers.** `ratings` accepts numbers and booleans ONLY. `info` accepts bounded vendor strings. `hardware_ids` accepts bounded strings. `account_ids` is pseudonymised on output. `functions`/`capabilities`/`flags`/`effects` are lists of lowercase tokens matching `^[a-z0-9_]{1,32}$`. A value that does not fit its container is silently dropped by `validate_report()` — so putting a model name in `ratings` means losing it.
- **Call `refresh_discovery()` as soon as the discovery data is in hand** - before the component's `automatic_config()` and before any later early return. `automatic_config()` can raise, and a report filed after it is never filed on the installs whose dump most needs one. Every wiring test includes a cycle where auto-config fails.
- **Set `inverter_type` using exactly the predicate the vendor's own `automatic_config()` uses to count a device as an inverter**, duplicated with a comment naming `automatic_config()` as the source of truth, and test a device that predicate excludes.
- **Build fixtures from the module's real API samples and existing fixtures, units included; never invent them.** Docstring samples, captured test fixtures, and real responses recorded in issues are the sources.
- **Any number reported as evidence must be able to distinguish the cases it claims to.** Where the vendor figure is known to be wrong, name the rating after what the API returned, not after the true quantity it fails to measure.
- **Every reporter gets a test that pushes its report through `validate_report()`** and requires every record back unchanged.
- **Station/plant/site IDs go in `account_ids`; site NAMES never go in `info`.** A site name is user-authored free text that can hold a street address, and `info`'s guards (a length cap, no `@`) would let one through.
- **Use the spec's capability tokens where one exists** (`schedule`, `pause_mode`, `pause_slots`, `target_soc`, `discharge_target`, `charge_rate_power`, `charge_rate_percent`, `soh`). A fact about topology rather than something the device can do is a flag, not a capability.
- **Firmware goes in `info["firmware"]`**, flattened to one string when the vendor reports a version per board.
- **Comments cite symbols, not line numbers.** A line number is wrong after the next edit above it.
- **Line length:** 256 (Black), 250 (Flake8).
- **Docstrings:** every function and class needs one (`interrogate`, 100%).
- **Spelling:** British English via cspell. Add genuinely new words to `.cspell/custom-dictionary-workspace.txt` (auto-sorted on commit, so re-stage after).
- **`git add` new files BEFORE running pre-commit.** `--all-files` means git-tracked only, so an untracked new test file passes unchecked.
- **Tests run from `coverage/`:** `./run_all --test <name>`. Save output to a file and grep it; do not pipe to grep directly.
- **Scope `git add` to the files you changed.** A GitNexus re-index can regenerate banners into `CLAUDE.md`/`AGENTS.md`/`.claude/skills/**`; never commit those incidentally.

## Amendments

_Recorded 2026-09-21, after Tasks 1-3 were executed and the whole branch was reviewed. These rulings were made during execution and are copied here because the working notes they were made in are not committed. The task text below has been updated to match them._

- **R1 - Tasks 2 and 3 are "identical in what reaches the catalogue", not byte-identical.** Records must be identical as dicts wherever a container is non-empty, and identical in what reaches the catalogue. Key order may change, and an empty container may be omitted from the raw `build_discovery()` return: GivTCP's hand-built record always wrote `"capabilities": capabilities` and `"entities": entities`, even when empty, whereas the builder omits an empty container. Why this is acceptable: the binding requirement is observe-only, meaning no change to what reaches the catalogue, and `validate_report()` already drops an empty container (`_validate_record()` writes a container only `if cleaned:`), so the assembled catalogue is unchanged. Keeping the empties would have meant special-casing the builder for one caller. The one visible difference - a GivTCP inverter with no capabilities now has no `capabilities` key in the raw return - is pinned by `test_build_discovery_omits_capabilities_when_no_probe_applies` in `test_givtcp_component.py`.
- **R2 - the only test edit R1 permitted.** An existing GivTCP or GE Cloud test that failed *solely* because it indexed `record["capabilities"]` or `record["entities"]` on a now-empty container could change that one access to `.get("capabilities", [])` / `.get("entities", {})`. Assertions on a non-empty container's contents could not change, and a failure of any other kind meant the refactor was wrong. In the event, no test needed it.
- **Task 4 corrections (Task 4 is not yet executed).** `inverter_type` is `"FoxCloud"`, the `INVERTER_DEF` key Fox's own `automatic_config()` writes (`fox.py`, `set_arg("inverter_type", ["FoxCloud" ...])`): `"FOX"` is not a key, and the catalogue's resulting-config comparison would show every Fox user a permanent false mismatch. `ratings["inverter_w"]` comes from `self.capacity_watts(detail)`, not `capacity * 1000`: Fox reports a half-kW model's capacity truncated (a KH10.5 says 10), and `capacity_watts()` restores the 500 W, so for a battery device the catalogue agrees with Fox's own `_inverter_capacity` sensor (`publish_data()` sets that sensor to 0 on a PV-only device, where the catalogue still reports the device's own rating) - the test now asserts 10500 W for a KH10.5. The wiring test now drives the real `FoxAPI.run()` rather than calling `refresh_discovery()` directly, which passed whether or not `run()` ever made the call.
- **The builder as it now stands (Task 1's code block is updated to match).** Every field after `device_id` is keyword-only; every container is copied (a shallow copy of the top level); a set or frozenset becomes a sorted list; tuples become lists; an empty string is dropped like `None`, while `control=False` is kept. The copy matters most: `refresh_discovery()` compares each build with the report it last filed, so a record holding a reporter's live list (`serials=self.serials`) would change along with that list, the rebuild would always compare equal, and the report would never be re-filed. `test_coordinator.py` gained a test for each (schema parity, keyword-only, copying, set/tuple normalisation, empty string, `control=False`, and an exact `validate_report()` round trip), and `test_component_base.py` a `refresh_discovery()`-level reproduction of the frozen report.
- **GE Cloud passes `measures_meter` into the builder.** `_apply_meter_cross_link()`, which patched the finished record, became `_meter_cross_link()`, which returns the value, so a record's whole shape is decided by `inverter_record()` with nothing mutated afterwards - the pattern later reporters should copy. Task 3's code block is updated to match.

_Recorded 2026-09-21, after Tasks 4 and 5 were executed and the resumed branch was reviewed as a whole. Most of that review's findings traced back to this plan's own text rather than to the implementation, which is why each also became a Global Constraint above._

- **The `refresh_discovery()` call now precedes `automatic_config()` in `FoxAPI.run()`.** Task 4 Step 5 put it before `run()`'s final `return True`, i.e. after `if first and self.automatic: await self.automatic_config()`. `automatic_config()` raises `ValueError` when no device qualifies as a battery inverter (a battery with no scheduler, say), and that exception leaves `run()` on every `ComponentBase.start()` retry - so with `fox_automatic: true` the report was never filed on exactly the installs whose dump most needs to say why. It now sits just above the auto-config step, still after the device poll and `publish_data()`, which is where GivTCP's `run()` already calls it. Nothing else about `run()` changes: `refresh_discovery()` never raises, and `automatic_config()` reads nothing it writes. `test_fox_run_reports_discovery_when_automatic_config_fails` drives the real `automatic_config()` through two failing retries.
- **`battery_entries` became `battery_capacity_entries` and `battery_capacity_serials`.** The Background claimed every `batteryList` entry carries a capacity, and the fixture built on it was invented (four entries of `capacity: 2.6`, no `type`, no `batterySN`). A real list mixes `bcu`/`ivu` control units that carry no capacity with `bmu` entries whose capacity is in Wh, so `len()` read 3 for one KH pack and 5 for both GH#4919's AIO and a healthy four-module stack - the bug and a healthy system gave the same number. The two new ratings are named for what the API returned; the fixture is rebuilt from `get_device_list()`'s and `get_device_detail()`'s own docstring samples, and the AIO case from GH#4919's reported response.
- **`inverter_type` uses `automatic_config()`'s own predicate.** It was gated on `hasBattery` alone, so a battery with no scheduler was reported as a `FoxCloud` inverter while `automatic_config()` raised for it. It now requires `hasBattery` AND `function.scheduler` AND `capacity > 0`, duplicated rather than shared so the control path is untouched, and a test runs the real `automatic_config()` on each case to keep the copy in step.
- **Vocabulary, firmware, a round trip and privacy.** The capability token `scheduler` became the spec's `schedule`; `third_party_gen` moved from `capabilities` to `flags`; `info["firmware"]` carries the per-board versions; a test pushes a realistic report through `validate_report()`; and `stationName`, `stationID` and `moduleSN` are deliberately not reported, with a test that none reaches the report.
- **Why Fox records carry no entity map - corrected.** The outcome stands: Fox, like GE Cloud, reports no `entities`. But the reason given during execution - that an entity map for a cloud integration would be new design work - was wrong. Each cloud integration's `automatic_config()` already maps Predbat's standard names to per-device entities, so a map is an extraction of that table, deferred because the extraction touches a control path. Task 4's Interfaces line, which said the task consumes `self.discovery_entities()`, is corrected, and the decision is recorded under Subsequent plans as needed before Plan 2.
- **Task 4's text, code and test blocks now match the code as it stands**, so the plan no longer describes the superseded placement, `battery_entries`, or `scheduler`. Task 5's doc text is noted where the executed wording differs.

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
- Produces: `inverter_record(device_id, *, inverter_type=None, control=None, composition=None, measures_meter=None, serials=None, functions=None, capabilities=None, flags=None, effects=None, hardware_ids=None, account_ids=None, info=None, ratings=None, coverage=None, entities=None) -> dict`. Every parameter except `device_id` is optional and keyword-only; any that is `None`, an empty string or an empty container is omitted from the returned dict, and every container is copied rather than stored as the caller's own object. Tasks 2, 3 and 4 import it as `from coordinator import inverter_record`.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_coordinator.py`, immediately before `def test_assemble_merges_sections_and_tags_source():`

```python
def test_inverter_record_omits_empty_containers():
    """An empty or unset container is left out of the record rather than written as {} or [].

    Every reporter previously wrote its own `if info: record["info"] = info` ladder. Centralising
    it means a reporter can pass whatever it gathered and let the builder decide, and the shape of
    an inverters record lives in exactly one place.
    """
    record = inverter_record("fox:ABC123", inverter_type="FoxCloud", composition="direct", functions=["solar", "battery"], info={}, ratings={}, entities={}, serials=[])
    assert record == {"device_id": "fox:ABC123", "inverter_type": "FoxCloud", "composition": "direct", "functions": ["solar", "battery"]}, record
    print("PASS: inverter_record omits empty containers")
    return 0


def test_inverter_record_keeps_everything_populated():
    """Every populated field survives, including a falsy-but-real rating like 0 and control=False.

    control is the one top-level field where a falsy value is itself the fact: False means a
    monitor-only inverter. A drop rule "simplified" to `if not value: continue` would silently
    turn every monitor-only inverter into one whose control is unknown, so it is pinned here.
    """
    record = inverter_record(
        "fox:ABC123",
        inverter_type="FoxCloud",
        control=False,
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
    assert record["control"] is False, "control=False is a fact (monitor-only), not an empty value: {}".format(record)
    print("PASS: inverter_record keeps every populated field")
    return 0


def test_inverter_record_round_trips_through_validation():
    """A record the builder produced survives validate_report() unchanged.

    The builder's whole job is producing something the coordinator will accept. If a field name
    here ever drifts from SECTION_SPEC's structural list, validation silently drops it - so pin
    that the two agree rather than trusting they do.
    """
    record = inverter_record("fox:ABC123", inverter_type="FoxCloud", composition="direct", serials=["S1"], measures_meter="fox:meter:M1", functions=["solar"], hardware_ids={"serial": "ABC123"}, info={"model": "H3"}, ratings={"battery_kwh": 10.4})
    cleaned = validate_report({"inverters": [record]}, "fox", print)["inverters"][0]
    for field in ("device_id", "inverter_type", "composition", "serials", "measures_meter", "functions", "hardware_ids", "info", "ratings"):
        assert field in cleaned, "{} was dropped by validate_report - builder and SECTION_SPEC disagree: {}".format(field, cleaned)
    assert cleaned == record, "validate_report() changed a built record:\n  built:   {}\n  cleaned: {}".format(record, cleaned)
    print("PASS: a built record survives validation with every field intact")
    return 0


def test_inverter_record_parameters_match_the_inverters_schema():
    """inverter_record()'s parameters are exactly the inverters section's fields - no more, no fewer.

    The builder spells the schema out as named parameters so a mistyped field is a TypeError at
    the call site. That only holds while its parameter list and the coordinator's schema agree:
    rename a container in CONTAINER_SPEC, or add a structural field to SECTION_SPEC, and the builder
    would go on emitting the old name, which validate_report() then silently drops from every
    reporter's records at once. This passes today; its job is to fail the day the two drift.
    """
    parameters = set(inspect.signature(inverter_record).parameters)
    assert "device_id" in parameters, "device_id is the record's identity and must stay a parameter: {}".format(sorted(parameters))
    schema = (set(SECTION_SPEC["inverters"]["structural"]) - {"device_id"}) | set(CONTAINER_SPEC) | set(VOCAB_CONTAINERS)
    fields = parameters - {"device_id"}
    assert fields == schema, "inverter_record() and the inverters schema disagree - missing from the builder: {}, not in the schema: {}".format(sorted(schema - fields), sorted(fields - schema))
    print("PASS: inverter_record's parameters are exactly the inverters schema")
    return 0


def test_inverter_record_fields_are_keyword_only():
    """Only device_id may be passed positionally; every other field must be named.

    Fifteen optional parameters in a row are an easy place to slip by one: accepted positionally,
    inverter_record("x:1", "direct", True) would quietly build inverter_type="direct",
    control=True - a wrong record with nothing to say so.
    """
    try:
        inverter_record("gecloud:x", "direct", True)
    except TypeError:
        pass
    else:
        raise AssertionError("a positional second argument must be a TypeError, not silently taken as inverter_type")
    parameters = inspect.signature(inverter_record).parameters
    positional = [name for name, parameter in parameters.items() if parameter.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert positional == ["device_id"], "only device_id may be positional, got {}".format(positional)
    print("PASS: inverter_record's schema fields are keyword-only")
    return 0


def test_inverter_record_copies_the_callers_containers():
    """The record holds its own copies of the containers, never the caller's live objects.

    refresh_discovery() stores the report it filed and compares the next build against it with ==.
    If the record held the caller's own list - a reporter passing serials=self.serials - then
    growing that list would grow the STORED report too, the rebuilt report would compare equal to
    it, and the change would never be filed: a report frozen at its first state, the exact bug
    refresh_discovery() exists to prevent.
    """
    live_serials = ["S1"]
    live_info = {"model": "GIV-GATEWAY"}
    first = inverter_record("gecloud:gateway001", composition="gateway", serials=live_serials, info=live_info)

    live_serials.append("S2")
    live_info["firmware"] = "ARM 1"
    second = inverter_record("gecloud:gateway001", composition="gateway", serials=live_serials, info=live_info)

    assert first != second, "a rebuilt record must differ once the caller's live state has moved on: {}".format(first)
    assert first["serials"] == ["S1"], "the first record's serials changed under it: {}".format(first["serials"])
    assert first["info"] == {"model": "GIV-GATEWAY"}, "the first record's info changed under it: {}".format(first["info"])
    print("PASS: inverter_record copies the caller's containers, so live state cannot freeze a report")
    return 0


def test_inverter_record_normalises_sets_and_tuples():
    """A set or frozenset becomes a sorted list, a tuple becomes a list, and an empty frozenset is omitted.

    validate_report() keeps only a list for a structural or vocabulary field, so anything else
    would be silently dropped from the catalogue. A set is sorted rather than listed as-is: string
    hashing is randomised per process, so list(some_set) comes out in a different order after a
    restart and two dumps of the same hardware would diff for no reason.
    """
    record = inverter_record(
        "gecloud:gateway001",
        serials={"S5", "S3", "S1", "S4", "S2"},
        functions=frozenset({"solar", "battery"}),
        capabilities=frozenset(),
        flags=("monitor_only",),
    )
    assert record["serials"] == ["S1", "S2", "S3", "S4", "S5"], "a set should become a sorted list: {}".format(record.get("serials"))
    assert record["functions"] == ["battery", "solar"], "a frozenset should become a sorted list: {}".format(record.get("functions"))
    assert "capabilities" not in record, "an empty frozenset is empty and must be omitted: {}".format(record)
    assert record["flags"] == ["monitor_only"], "a tuple should become a list: {}".format(record.get("flags"))
    print("PASS: inverter_record sorts sets, lists tuples and omits an empty frozenset")
    return 0


def test_inverter_record_drops_an_empty_string():
    """An empty string is unset, like None - but control=False is a real value and is kept."""
    record = inverter_record("fox:ABC123", inverter_type="", measures_meter="", composition="direct", control=False)
    assert "inverter_type" not in record, "an empty inverter_type is unset and must be omitted: {}".format(record)
    assert "measures_meter" not in record, "an empty measures_meter is unset and must be omitted: {}".format(record)
    assert record["composition"] == "direct"
    assert record["control"] is False, "control=False is a bool, not an empty string, and must be kept: {}".format(record)
    print("PASS: inverter_record drops an empty string but keeps control=False")
    return 0
```

Register all eight in `test_coordinator_all()`, immediately after the line `failures += test_assemble_component_status()`:

```python
    failures += test_inverter_record_omits_empty_containers()
    failures += test_inverter_record_keeps_everything_populated()
    failures += test_inverter_record_round_trips_through_validation()
    failures += test_inverter_record_parameters_match_the_inverters_schema()
    failures += test_inverter_record_fields_are_keyword_only()
    failures += test_inverter_record_copies_the_callers_containers()
    failures += test_inverter_record_normalises_sets_and_tuples()
    failures += test_inverter_record_drops_an_empty_string()
```

Extend the existing import at `apps/predbat/tests/test_coordinator.py:8` — it currently reads:

```python
from coordinator import Coordinator, Redactor, SCHEMA_VERSION, SECTION_SPEC
```

and must become (with `import inspect` added above it, for the schema-parity and keyword-only tests):

```python
from coordinator import CONTAINER_SPEC, Coordinator, Redactor, SCHEMA_VERSION, SECTION_SPEC, VOCAB_CONTAINERS, inverter_record, validate_report
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd coverage
source setup.csh
python3 ../apps/predbat/unit_test.py --test coordinator > /tmp/t1.log 2>&1; grep -E "ImportError|cannot import|ERROR" /tmp/t1.log | head
```

Expected: FAIL with `cannot import name 'inverter_record' from 'coordinator'`.

- [ ] **Step 3: Write the implementation**

In `apps/predbat/coordinator.py`, add immediately before `def validate_report(report, component_name, log):`

```python
def inverter_record(
    device_id,
    *,
    inverter_type=None,
    control=None,
    composition=None,
    measures_meter=None,
    serials=None,
    functions=None,
    capabilities=None,
    flags=None,
    effects=None,
    hardware_ids=None,
    account_ids=None,
    info=None,
    ratings=None,
    coverage=None,
    entities=None,
):
    """Assemble one inverters-section record, omitting every field that is unset or empty.

    The parameter list IS the inverters section's schema, spelled out rather than taken as
    **kwargs: a mistyped field name is then a TypeError a test catches at the call site, instead
    of a key that reaches validate_report() and is silently dropped from a user's dump. Every
    field after device_id is keyword-only for the same reason - fifteen optional parameters in a
    row are easy to slip by one, and inverter_record("x:1", "direct", True) would otherwise build
    inverter_type="direct", control=True without complaint.

    Unset fields are omitted rather than written as None, {}, [] or "". Every reporter previously
    carried its own `if info: record["info"] = info` ladder, which is how a record ends up
    carrying `"ratings": {}` in one component and omitting it in another. A falsy value that is
    real data is kept: a rating of 0 inside a container, and control=False, which is the fact
    "monitor-only" rather than an absence. Only None, an empty string and an empty container are
    dropped.

    Every container is copied, never stored as the caller's own object. refresh_discovery()
    compares each new build against the report it last filed, so a record holding a reporter's
    live list (serials=self.serials) would change whenever that list did, the rebuilt report
    would always compare equal to it, and the report would freeze at its first state. The copy is
    shallow - the top level only - which is enough because reporters build nested descriptor
    dicts fresh on every call rather than handing over long-lived ones.

    Tuples become lists because validate_report() keeps only a list for a structural or
    vocabulary field: a tuple `serials` or `functions` - a module-level constant, say - would be
    silently dropped from the catalogue. A set or frozenset becomes a sorted list for the same
    reason, sorted because string hashing is randomised per process, so list(some_set) comes out
    in a different order after a restart and two dumps of the same hardware would diff for nothing.
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
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        elif isinstance(value, (list, tuple)):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        # A bool is never dropped: it is not a str/list/dict, so control=False survives
        if value is None or (isinstance(value, (str, list, dict)) and not value):
            continue
        record[name] = value
    return record


```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py --test coordinator > /tmp/t1.log 2>&1; grep -E "PASS: inverter_record|PASS: a built record|failures" /tmp/t1.log
```

Expected: all eight PASS lines.

- [ ] **Step 5: Verify the tests are load-bearing**

Temporarily change `if value is None or (isinstance(value, (str, list, dict)) and not value):` to `if value is None:`, re-run, and confirm `test_inverter_record_omits_empty_containers` fails; then change it to `if not value:` and confirm `test_inverter_record_keeps_everything_populated` fails on `control`. Restore the line afterwards and re-run to confirm green.

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
- Test: `apps/predbat/tests/test_givtcp_component.py` (existing tests must pass; the only permitted edit is R2's, see Amendments)

**Interfaces:**
- Consumes: `inverter_record(...)` from Task 1.
- Produces: no new interface. GivTCP's reported records must be identical as dicts wherever a container is non-empty, and identical in what reaches the catalogue (Amendments, R1). Key order may change, and an empty `capabilities` or `entities` may be omitted from the raw `build_discovery()` return, since `validate_report()` drops an empty container anyway.

This task is a refactor with no change to what reaches the catalogue. GivTCP's existing discovery tests are the proof. The one test edit allowed is R2's: a test that fails *solely* because it indexes `record["capabilities"]` or `record["entities"]` on a now-empty container may change that one access to `.get(...)`. Any other failure means the refactor is wrong, not the test.

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
- Test: `apps/predbat/tests/test_ge_cloud.py` (existing tests must pass; the only permitted edit is R2's, see Amendments)

**Interfaces:**
- Consumes: `inverter_record(...)` from Task 1.
- Produces: no new interface. GE Cloud's reported records must be identical as dicts wherever a container is non-empty, and identical in what reaches the catalogue (Amendments, R1); key order may change.

GE Cloud is the harder migration because it builds two kinds of record plus a `measures_meter` cross-link. As first executed, a helper (`_apply_meter_cross_link`) patched `record["measures_meter"]` into the finished dict. The fix wave (see Amendments) replaced it with `_meter_cross_link(devices, device)`, which returns the value, so it is passed to `inverter_record()` like every other field and nothing is mutated after the build. The code below is the final form.

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
            inverters.append(
                inverter_record(
                    "gecloud:{}".format(device),
                    inverter_type=inverter_type,
                    composition=composition,
                    measures_meter=self._meter_cross_link(devices, device),
                    functions=["solar", "battery"],
                    capabilities=self._device_capabilities(device),
                    hardware_ids={"serial": device},
                    serials=fronted_serials,
                    info=info,
                    ratings=ratings,
                )
            )
```

and turn `_apply_meter_cross_link(self, devices, device, record)` into `_meter_cross_link(self, devices, device)`, returning `"gecloud:meter:{serial}"` from `self._device_meter_serial(devices, device)`, or `None` when there is no meter serial (the builder omits a `None`). Keep its docstring's explanation of why a CT clamp does not become a fabricated `meters` record, and update the two `see _apply_meter_cross_link` references in `build_discovery()`'s docstring and closing comment.

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

_Corrected 2026-09-21 after the whole-branch review (see Amendments): the Background, Interfaces, Step 5 placement and every code and test block below now match the code as it stands, not the first draft - which placed the `refresh_discovery()` call after `automatic_config()`, reported `battery_entries`, and used the token `scheduler`._

**Files:**
- Modify: `apps/predbat/fox.py` (add `build_discovery()`, add the `refresh_discovery()` call in `run()`)
- Test: `apps/predbat/tests/test_fox_api.py`

**Interfaces:**
- Consumes: `inverter_record(...)` from Task 1; `self.refresh_discovery()` from `ComponentBase`. It does **not** consume `self.discovery_entities(descriptors)`: the Fox reporter builds no entity map, so Fox records carry no `entities`, like GE Cloud's. Building one is a decision needed before Plan 2 - see Subsequent plans.
- Produces: `FoxAPI.build_discovery() -> dict | None`, returning `{"automatic": bool, "inverters": [...]}` or `None` when no devices have been discovered yet.

**Background the implementer needs:**

Fox already gathers everything the reporter needs, in the same data `automatic_config()` reads. `get_device_list()` and `get_device_detail()` each carry a real API sample in their docstrings - build fixtures from those, never from memory.

- `self.device_list` — `get_device_list()`'s result: dicts with a `deviceSN` key, plus `stationName`, `stationID` and `moduleSN`, which the reporter must never copy out (below).
- `self.device_detail[sn]` — `get_device_detail()`'s result: `hasPV`, `hasBattery`, `thirdPartyGen` (bools), `capacity` (inverter kW - convert it with `FoxAPI.capacity_watts(detail)`, never `* 1000`: Fox truncates a half-kW model's capacity, a KH10.5 reporting 10, and `capacity_watts()` restores the 500 W), `deviceType` (model string), `productType`, `masterVersion`/`slaveVersion`/`managerVersion` (firmware, one per board), `function` (`function["scheduler"]` is a bool), `stationName`, `stationID`, `moduleSN`, and `batteryList`.
- `batteryList` is **not** one capacity-carrying entry per pack. It mixes control units that carry no capacity with `bmu` entries whose `capacity` is in **Wh**: the single-pack KH in `get_device_detail()`'s sample is `[bcu (no capacity), bmu (capacity 10360), ivu (no capacity)]`, so `len()` is 3 for one pack. GH#4919's AIO ESS (read out of the reporter's own `Fox: Device detail` log line) reports its one 10.24 kWh pack as a `bcu` plus four `bmu` entries, each claiming 10240 Wh and all carrying the inverter's own serial as `batterySN`.
- `self.device_settings[sn]` — settings keyed by name, each what `get_device_setting()` stored; `FOX_SETTINGS = ["ExportLimit", "MaxSoc", "GridCode", "WorkMode", "MinSoc", "MinSocOnGrid"]`.

**Do not report a battery capacity.** `publish_data()` sums every `batteryList` entry carrying a `capacity`, and that is a known live bug (GH#4919): the AIO's sum is 4× the real pack. Report instead two ratings named for what the API returned - `battery_capacity_entries` (the entries `publish_data()` sums) and `battery_capacity_serials` (distinct `batterySN` among them) - rather than the spec's `modules`, since the vendor figure is known to be wrong. Four entries against one serial is the GH#4919 signature; a healthy four-module stack reads four and four. Neither `len(batteryList)` (5 for both) nor the entry count alone (4 for both) can tell those two apart.

**`inverter_type` follows `automatic_config()`'s own predicate.** `automatic_config()` counts a device as an inverter only when `hasBattery` AND `function.scheduler` AND `capacity > 0`, and raises when none qualifies. Set `inverter_type="FoxCloud"` (the `INVERTER_DEF` key it writes) on exactly those devices, duplicating the predicate with a comment naming `automatic_config()` - do not refactor the control path to share it. Any other device still reports its battery in `functions`.

**Vocabulary and privacy.** A device-side scheduler is the spec's capability token `schedule`; `export_limit` has no spec equivalent; a third-party generator the inverter meters is topology, so `third_party_gen` is a flag. Firmware goes in `info["firmware"]`, flattened to one string as GE Cloud's `_device_info_and_ratings()` does. Never report `stationName` (user-authored free text - `get_device_list()`'s sample holds a street address), `stationID` or `moduleSN`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_fox_api.py`, at the end of the file but before the `run_fox_api_tests()` aggregator:

```python
# The single-pack batteryList from get_device_detail()'s own docstring sample: a bcu and an ivu
# that carry no capacity, and one bmu carrying the pack's capacity in Wh. len() is 3 for ONE pack.
FOX_SINGLE_PACK_BATTERY_LIST = [
    {"batterySN": "PACK0001", "model": "EP11", "type": "bcu", "version": "1.005"},
    {"batterySN": "PACK0001", "model": "EP11", "type": "bmu", "version": "1.05", "capacity": 10360},
    {"batterySN": "PACK0001", "model": "EP11", "type": "ivu", "version": "0.00"},
]

# GH#4919, read out of the reporter's own "Fox: Device detail" log line: an AIO ESS with ONE
# 10.24 kWh pack reports a bcu plus four bmu entries, each claiming the whole pack's 10240 Wh and
# all carrying the inverter's own serial as batterySN (here BATT001, the fixture inverter's).
FOX_AIO_BATTERY_LIST = [{"batterySN": "BATT001", "type": "bcu"}] + [{"batterySN": "BATT001", "type": "bmu", "capacity": 10240} for _ in range(4)]


def _fox_discovery_devices():
    """One battery inverter and one PV-only device as the Fox cloud reports them: (device_list, device_detail, device_settings).

    BATT001 is Fox's own samples, field for field, with the identifiers replaced: its device_list
    entry is get_device_list()'s docstring sample, and its detail is get_device_detail()'s. Both
    carry stationName, stationID and moduleSN, which the reporter must never copy out - the
    stationName here is address-shaped because get_device_list()'s real sample holds an address.
    The repo holds no real sample of a PV-only device's detail, so PVONLY1 carries only fields
    automatic_config() and publish_data() already read.
    """
    station = {"stationName": "2 Example Street", "stationID": "STATION-0001", "moduleSN": "MODULE0001"}
    device_list = [
        dict(station, deviceType="KH8", hasBattery=True, hasPV=True, deviceSN="BATT001", productType="KH", status=1),
        dict(station, deviceType="S1-5.0", hasBattery=False, hasPV=True, deviceSN="PVONLY1", status=1),
    ]
    device_detail = {
        "BATT001": dict(
            station,
            deviceType="KH8",
            masterVersion="1.34",
            afciVersion="",
            hasPV=True,
            deviceSN="BATT001",
            slaveVersion="1.01",
            capacity=8,
            hasBattery=True,
            function={"scheduler": True},
            hardwareVersion="--",
            managerVersion="1.28",
            batteryList=[dict(entry) for entry in FOX_SINGLE_PACK_BATTERY_LIST],
            productType="KH",
            status=1,
        ),
        "PVONLY1": {"hasPV": True, "hasBattery": False, "capacity": 5.0, "deviceType": "S1-5.0", "function": {}},
    }
    # {deviceSN: {SettingName: <what get_device_setting() stored>}}: WorkMode is get_device_setting()'s
    # own docstring sample; ExportLimit is the value update_settings_from_schedule() derives
    device_settings = {"BATT001": {"ExportLimit": {"value": 12000.0}, "WorkMode": {"enumList": ["PeakShaving", "Feedin", "SelfUse"], "unit": "", "precision": 1.0, "value": "SelfUse"}}, "PVONLY1": {}}
    return device_list, device_detail, device_settings


def _fox_discovery_api(my_predbat):
    """A FoxAPI carrying the _fox_discovery_devices() fleet."""
    fox = FoxAPI(my_predbat, key="test_key", automatic=False)
    fox.component_name = "fox"
    fox.device_list, fox.device_detail, fox.device_settings = _fox_discovery_devices()
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
    assert battery["inverter_type"] == "FoxCloud", "inverter_type is an INVERTER_DEF key - the one Fox's own automatic_config() writes"
    assert sorted(battery["functions"]) == ["battery", "solar"]
    assert battery["hardware_ids"] == {"serial": "BATT001"}
    assert battery["info"]["model"] == "KH8"
    assert battery["info"]["product_type"] == "KH"
    # Fox's per-board versions flattened into one string, board names sorted, as GE Cloud does;
    # the empty afciVersion and the placeholder hardwareVersion "--" are not firmware
    assert battery["info"]["firmware"] == "manager 1.28 master 1.34 slave 1.01", battery["info"]
    assert battery["ratings"]["inverter_w"] == 8000.0
    assert sorted(battery["capabilities"]) == ["export_limit", "schedule"], "schedule is the spec's token for a device-side scheduler - not scheduler"
    assert "flags" not in battery, "thirdPartyGen is not set on this device"

    # The station name is user-authored free text - get_device_list()'s own sample holds a street
    # address - so neither it nor the station and module identifiers may reach a public dump.
    for private in ("2 Example Street", "STATION-0001", "MODULE0001"):
        assert private not in repr(report), f"{private} must never be reported"

    pv = by_id["fox:PVONLY1"]
    assert pv["functions"] == ["solar"], "a device with no battery is solar only"
    assert "inverter_type" not in pv, "a PV-only device is not an inverter Predbat controls"
    # The device's own rating, even though publish_data() publishes 0 on a PV-only device's
    # _inverter_capacity sensor - the catalogue and that sensor agree only for battery devices
    assert pv["ratings"]["inverter_w"] == 5000.0
    assert "firmware" not in pv.get("info", {}), "no version fields, no firmware"

    # A third-party generator the inverter meters is topology, not something it can do: a flag
    fox.device_detail["BATT001"]["thirdPartyGen"] = True
    battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
    assert battery["flags"] == ["third_party_gen"], battery
    assert "third_party_gen" not in battery["capabilities"], battery["capabilities"]

    # A half-kW model: Fox reports a KH10.5's capacity truncated to 10. capacity_watts() restores
    # the 500 W, so for a battery inverter the catalogue agrees with the _inverter_capacity sensor.
    fox.device_detail["BATT001"].update({"deviceType": "KH10.5", "capacity": 10})
    battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
    assert battery["ratings"]["inverter_w"] == 10500.0, "a half-kW model must go through capacity_watts(), not capacity * 1000"
    print("PASS: Fox build_discovery describes each discovered device")
    return 0


def test_fox_build_discovery_round_trips_through_validate_report(my_predbat):
    """A realistic Fox report survives validate_report() with every field intact.

    validate_report() silently drops any value that does not fit its container - a string in
    ratings, a capital letter in a token, an info string over 64 characters - so a reporter whose
    values are quietly discarded passes its own tests and shows a thinner catalogue in production.
    Built from the fixture taken from Fox's own samples, with thirdPartyGen set so flags is carried
    too, and the GH#4919 AIO alongside it; the container checks at the end keep the equality from
    passing vacuously on a record that populates nothing.
    """
    print("**** test_fox_build_discovery_round_trips_through_validate_report ****")
    from coordinator import validate_report

    fox = _fox_discovery_api(my_predbat)
    fox.device_detail["BATT001"]["thirdPartyGen"] = True
    fox.device_list.append({"deviceSN": "AIO0001"})
    fox.device_detail["AIO0001"] = dict(fox.device_detail["BATT001"], deviceSN="AIO0001", batteryList=[dict(entry, batterySN="AIO0001") for entry in FOX_AIO_BATTERY_LIST], batteryDesignCapacity=10.24)
    report = fox.build_discovery()

    warnings = []
    cleaned = validate_report(report, "fox", warnings.append)

    assert warnings == [], f"validation dropped something: {warnings}"
    assert cleaned["automatic"] is False
    assert len(cleaned["inverters"]) == len(report["inverters"]) == 3, cleaned
    for record, cleaned_record in zip(report["inverters"], cleaned["inverters"]):
        assert cleaned_record == record, f"{record['device_id']} changed in validation:\n  built   {record}\n  cleaned {cleaned_record}"

    by_id = {record["device_id"]: record for record in cleaned["inverters"]}
    battery = by_id["fox:BATT001"]
    assert set(battery) == {"device_id", "inverter_type", "composition", "functions", "capabilities", "flags", "hardware_ids", "info", "ratings"}, set(battery)
    assert set(battery["info"]) == {"model", "product_type", "firmware"}, battery["info"]
    assert set(battery["ratings"]) == {"inverter_w", "battery_capacity_entries", "battery_capacity_serials"}, battery["ratings"]
    assert by_id["fox:AIO0001"]["ratings"]["battery_capacity_entries"] == 4 and by_id["fox:AIO0001"]["ratings"]["battery_capacity_serials"] == 1
    print("PASS: Fox's report round-trips through validate_report() with nothing dropped")
    return 0


def _fox_battery_record(my_predbat, battery_list, **detail):
    """BATT001's discovery record with its batteryList (and any other detail fields) replaced."""
    fox = _fox_discovery_api(my_predbat)
    fox.device_detail["BATT001"].update(detail, batteryList=battery_list)
    return {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]


def _fox_battery_facts(record):
    """Every battery-derived rating in a record - whatever the reporter chose to name them."""
    return {name: value for name, value in record.get("ratings", {}).items() if name.startswith("battery")}


def test_fox_build_discovery_battery_ratings_tell_the_aio_bug_from_a_healthy_stack(my_predbat):
    """The battery ratings distinguish the GH#4919 AIO from a healthy system, and never report the known-wrong sum.

    publish_data() sums the capacity of every batteryList entry that carries one. GH#4919's AIO
    reports one 10.24 kWh pack as four bmu entries, each claiming 10240 Wh and all carrying the
    inverter's own serial, so that sum is 4x the truth. Counting entries is no better evidence: the
    real list is a bcu plus the four bmu entries, five in all - exactly what a genuine four-module
    stack reports, whose bmu entries each carry their own serial. Only the serials tell the two
    apart, so the report carries both how many entries publish_data() sums and how many distinct
    serials those entries hold: four against one is the GH#4919 signature.

    The healthy four-module stack is the AIO's own entries with a serial per module - the repo holds
    no captured multi-pack sample, so it changes only the one field the ratings claim to read.
    """
    print("**** test_fox_build_discovery_battery_ratings_tell_the_aio_bug_from_a_healthy_stack ****")
    four_module_list = [{"batterySN": "BATT001", "type": "bcu"}] + [{"batterySN": "MODULE{}".format(n), "type": "bmu", "capacity": 10240} for n in range(4)]

    single_pack = _fox_battery_facts(_fox_battery_record(my_predbat, [dict(entry) for entry in FOX_SINGLE_PACK_BATTERY_LIST]))
    aio = _fox_battery_facts(_fox_battery_record(my_predbat, [dict(entry) for entry in FOX_AIO_BATTERY_LIST], batteryDesignCapacity=10.24))
    four_module = _fox_battery_facts(_fox_battery_record(my_predbat, four_module_list))

    assert aio != four_module, f"the GH#4919 AIO must not look like a healthy four-module stack: both report {aio}"
    assert aio == {"battery_capacity_entries": 4, "battery_capacity_serials": 1}, aio
    assert four_module == {"battery_capacity_entries": 4, "battery_capacity_serials": 4}, four_module
    # The bcu and ivu carry no capacity, so one pack is one summed entry - not len(batteryList), 3
    assert single_pack == {"battery_capacity_entries": 1, "battery_capacity_serials": 1}, single_pack
    for facts in (single_pack, aio, four_module):
        assert "battery_kwh" not in facts, "the summed capacity is known to be wrong - do not report it"
        assert 40960 not in facts.values() and 40.96 not in facts.values(), "4 x 10240 Wh is the bug, not a rating"

    # Deliberately not gated on hasBattery: a battery list on a device that says it has no battery
    # is itself worth seeing
    stray = _fox_battery_record(my_predbat, [dict(entry) for entry in FOX_SINGLE_PACK_BATTERY_LIST], hasBattery=False)
    assert "battery" not in stray["functions"]
    assert _fox_battery_facts(stray) == {"battery_capacity_entries": 1, "battery_capacity_serials": 1}, stray
    print("PASS: Fox's battery ratings tell the GH#4919 AIO from a healthy stack")
    return 0


def test_fox_build_discovery_sets_inverter_type_only_where_automatic_config_would(my_predbat):
    """inverter_type is set exactly on the devices automatic_config() counts as inverters.

    automatic_config() configures a device only when hasBattery, function.scheduler and a positive
    capacity all hold, and raises when none does. build_discovery() duplicates that predicate
    rather than sharing it (sharing it would touch the control path), so this pins the copy to the
    source of truth by running the REAL automatic_config() on each case: a device it refuses must
    carry no inverter_type - while still showing its battery in functions, which is the evidence
    a user's dump needs.
    """
    print("**** test_fox_build_discovery_sets_inverter_type_only_where_automatic_config_would ****")
    _, device_detail, device_settings = _fox_discovery_devices()
    cases = {
        "healthy": {},
        "no scheduler": {"function": {"scheduler": False}},
        "no function block": {"function": None},
        "zero capacity": {"capacity": 0},
    }
    for name, change in cases.items():
        detail = dict(device_detail["BATT001"], **change)
        if detail["function"] is None:
            del detail["function"]
        fox = MockFoxAPIWithRequests()
        fox.device_list = [{"deviceSN": "BATT001"}]
        fox.device_detail = {"BATT001": detail}
        fox.device_settings = {"BATT001": device_settings["BATT001"]}

        record = fox.build_discovery()["inverters"][0]
        try:
            run_async(fox.automatic_config())
            configured = True
        except ValueError:
            configured = False

        assert ("inverter_type" in record) == configured, f"{name}: inverter_type {record.get('inverter_type')!r} but automatic_config() configured={configured}"
        assert "battery" in record["functions"], f"{name}: the battery is reported whether or not Predbat can drive it"
        if configured:
            assert record["inverter_type"] == "FoxCloud" and fox.args_set["inverter_type"] == ["FoxCloud"], name
    print("PASS: Fox sets inverter_type exactly where automatic_config() would configure the device")
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
    """The real run() files a report on every cycle, and a broken build_discovery() cannot degrade Fox.

    Drives FoxAPI.run() itself rather than calling refresh_discovery() directly - the same reason
    the GE Cloud discovery tests call the real run() (see _discovery_component() in
    test_ge_cloud.py). A direct call passes whether or not run() ever makes it: a call placed
    inside `if first and self.automatic:` (automatic is False here), inside an `if first:` block,
    or after an early return would all go unnoticed. MockFoxAPIWithRunTracking stubs only the
    network-facing calls run() makes, exactly as the test_run_* tests above use it, and its stubs
    leave device_detail/device_settings alone, so the fixture seeded here is what run() reports.

    The second cycle is first=False with a device added, so a call confined to the first cycle
    fails it. The third cycle's build raises: run() must still succeed, had_errors must stay
    unset (it would suppress record_status() - see ComponentBase.refresh_discovery()), and the
    last filed report must stand.
    """
    print("**** test_fox_run_reports_discovery_and_survives_a_failure ****")
    device_list, device_detail, device_settings = _fox_discovery_devices()
    fox = MockFoxAPIWithRunTracking()
    fox.automatic = False
    fox.device_list = [device_list[0]]
    fox.device_detail = {"BATT001": device_detail["BATT001"]}
    fox.device_settings = {"BATT001": device_settings["BATT001"]}
    reports = []
    fox.report_discovery = lambda report: reports.append(report)

    assert run_async(fox.run(0, first=True)) is True
    assert len(reports) == 1, f"the first run() cycle should file a report, got {len(reports)}"
    assert [record["device_id"] for record in reports[0]["inverters"]] == ["fox:BATT001"], reports[0]

    # A second device appears; the next ordinary cycle must re-file the grown report
    fox.device_list.append(device_list[1])
    fox.device_detail["PVONLY1"] = device_detail["PVONLY1"]
    assert run_async(fox.run(60, first=False)) is True
    assert len(reports) == 2, f"a first=False cycle must re-file a report that has moved on, got {len(reports)}"
    assert {record["device_id"] for record in reports[1]["inverters"]} == {"fox:BATT001", "fox:PVONLY1"}, reports[1]

    fox.build_discovery = MagicMock(side_effect=Exception("boom"))
    assert run_async(fox.run(120, first=False)) is True, "a discovery failure must not fail run()"
    assert not getattr(fox.base, "had_errors", False), "a discovery failure must never set had_errors - that suppresses record_status()"
    assert len(reports) == 2 and fox._discovery_report == reports[1], "a failed build must leave the last filed report standing"
    print("PASS: Fox's run() reports on every cycle and contains a reporter failure")
    return 0


def test_fox_run_reports_discovery_when_automatic_config_fails(my_predbat):
    """A cycle whose automatic_config() raises still files a report - and still raises exactly as before.

    With fox_automatic: true and no device automatic_config() can drive (here a battery inverter
    whose function.scheduler is False), automatic_config() raises ValueError out of run(). run()
    then never returns True, so ComponentBase.start() keeps retrying it with first=True, and it
    raises every time. Those are exactly the installs whose debug dump most needs the catalogue to
    say why - "battery present, no scheduler" - so the report has to be filed BEFORE
    automatic_config() runs, the placement GivTCP's run() uses for the same reason.

    Discovery is observe-only, so the failure itself must be untouched: the same ValueError, with
    the same message, still out of run() on every retry. The REAL automatic_config() is bound here
    (MockFoxAPIWithRunTracking stubs it out) so the failure is the production one, not a stand-in.
    """
    print("**** test_fox_run_reports_discovery_when_automatic_config_fails ****")
    device_list, device_detail, device_settings = _fox_discovery_devices()
    fox = MockFoxAPIWithRunTracking()
    fox.automatic = True
    fox.automatic_config = FoxAPI.automatic_config.__get__(fox)
    fox.device_list = [device_list[0]]
    fox.device_detail = {"BATT001": dict(device_detail["BATT001"], function={"scheduler": False})}
    fox.device_settings = {"BATT001": device_settings["BATT001"]}
    reports = []
    fox.report_discovery = lambda report: reports.append(report)

    # Two start()-style retries: first stays True because run() never returned True
    for seconds in (0, 120):
        try:
            run_async(fox.run(seconds, first=True))
        except ValueError as error:
            assert "No batteries with scheduler found" in str(error), error
        else:
            raise AssertionError("automatic_config() must still raise out of run() - discovery must not change that")

    assert len(reports) == 1, f"a cycle whose automatic_config() fails must still file a report (and an unchanged one only once), got {len(reports)}"
    record = reports[0]["inverters"][0]
    assert record["device_id"] == "fox:BATT001", record
    assert "battery" in record["functions"], "the report must show the battery automatic_config() could not configure"
    assert reports[0]["automatic"] is True
    print("PASS: Fox files its report even on a cycle where automatic_config() fails")
    return 0
```

Register them in `run_fox_api_tests(my_predbat)` (the function `unit_test.py` calls for the `fox_api` test), one line per test:

```python
    failed |= test_fox_build_discovery_describes_each_device(my_predbat)
    failed |= test_fox_build_discovery_round_trips_through_validate_report(my_predbat)
    failed |= test_fox_build_discovery_battery_ratings_tell_the_aio_bug_from_a_healthy_stack(my_predbat)
    failed |= test_fox_build_discovery_sets_inverter_type_only_where_automatic_config_would(my_predbat)
    failed |= test_fox_build_discovery_returns_none_before_discovery(my_predbat)
    failed |= test_fox_run_reports_discovery_and_survives_a_failure(my_predbat)
    failed |= test_fox_run_reports_discovery_when_automatic_config_fails(my_predbat)
```

`MagicMock` and `run_async` are already imported at the top of `test_fox_api.py`, so no import change is needed. The run() tests use `MockFoxAPIWithRunTracking` (the stub the `test_run_*` tests drive `run()` through) and the inverter_type test uses `MockFoxAPIWithRequests`, so the new tests must sit after both classes - the end of the file is.

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

        functions says what a device physically has ("solar", "battery"); inverter_type says
        whether Predbat would drive it. inverter_type is "FoxCloud" - the INVERTER_DEF key
        automatic_config() itself writes - on exactly the devices automatic_config() counts as
        inverters: hasBattery AND function.scheduler AND a positive capacity. Any other device,
        a PV-only one or a battery automatic_config() refuses (no scheduler, say), carries no
        inverter_type but still shows its battery in functions, which is precisely what a dump
        from an install whose auto-config fails needs to show. That predicate is duplicated here
        rather than shared, because sharing it means changing automatic_config(), a control path;
        automatic_config() is the source of truth, and a test runs it to pin this copy to it.

        The inverter's rating goes through capacity_watts(), never capacity * 1000: Fox reports a
        half-kW model's capacity truncated (a KH10.5 says 10), and capacity_watts() is what
        restores the 500 W. For a battery device that is also the value of the _inverter_capacity
        sensor publish_data() publishes; for a PV-only device publish_data() sets that sensor to
        0, while the catalogue still reports the device's own rating.

        stationName, stationID and moduleSN are deliberately not reported. stationName is
        user-authored free text that can hold a street address (get_device_list()'s own sample
        holds one), and the info container's guards - a length cap and no "@" - would let an
        address straight into a debug dump users post publicly. Neither identifier describes the
        hardware; a station ID, if one is ever wanted, belongs in account_ids, which is
        pseudonymised.

        No battery capacity is reported. A real batteryList mixes control units that carry no
        capacity (bcu, ivu) with bmu entries carrying one in Wh, and publish_data() sums every
        entry that carries one - a known live bug (GH#4919): an AIO ESS reports its one pack as
        four bmu entries, each claiming the whole pack and all carrying the inverter's own
        serial, so the sum is 4x the truth. Publishing it would put a knowingly-wrong battery_kwh
        in every affected dump. Two ratings are reported instead, named for what the API returned
        rather than as the spec's "modules", since the vendor figure is known to be wrong:
        battery_capacity_entries, how many entries publish_data() sums, and
        battery_capacity_serials, how many distinct batterySN those entries carry. A healthy
        four-module stack reports four and four; four entries against one serial is the GH#4919
        signature. Neither len(batteryList) nor the entry count alone can tell them apart - each
        reads the same for the bug as for that healthy stack. Deliberately not gated on hasBattery:
        a battery list on a device that says it has no battery is itself worth seeing.

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
            has_scheduler = bool((detail.get("function") or {}).get("scheduler", False))

            functions = []
            if has_pv:
                functions.append("solar")
            if has_battery:
                functions.append("battery")

            # "schedule" is the spec's token for a device-side scheduler; export_limit has no spec
            # equivalent. A third-party generator this inverter meters is topology, not something
            # the inverter can do, so it is a flag rather than a capability.
            capabilities = []
            if has_scheduler:
                capabilities.append("schedule")
            settings = self.device_settings.get(serial, {}) or {}
            if "ExportLimit" in settings:
                capabilities.append("export_limit")
            flags = []
            if detail.get("thirdPartyGen", False):
                flags.append("third_party_gen")

            info = {}
            device_type = detail.get("deviceType")
            if device_type:
                info["model"] = str(device_type)
            product_type = detail.get("productType")
            if product_type:
                info["product_type"] = str(product_type)
            # Fox reports a firmware version per board; info takes only strings, so they are
            # flattened into one, board names in sorted order - as GE Cloud's
            # _device_info_and_ratings() does with its firmware_version dict
            boards = [(board, detail.get(board + "Version")) for board in ("manager", "master", "slave")]
            firmware = " ".join("{} {}".format(board, version.strip()) for board, version in boards if isinstance(version, str) and version.strip())
            if firmware:
                info["firmware"] = firmware

            ratings = {}
            if detail.get("capacity"):
                ratings["inverter_w"] = self.capacity_watts(detail)
            battery_list = detail.get("batteryList") or []
            summed = [entry for entry in battery_list if isinstance(entry, dict) and "capacity" in entry] if isinstance(battery_list, list) else []
            if summed:
                ratings["battery_capacity_entries"] = len(summed)
                ratings["battery_capacity_serials"] = len({entry.get("batterySN") for entry in summed if entry.get("batterySN")})

            # automatic_config() is the source of truth for which devices are inverters Predbat
            # drives: it configures one only when hasBattery, function.scheduler and capacity > 0
            # all hold. Duplicated here, not shared, so this observer does not touch that control
            # path; test_fox_build_discovery_sets_inverter_type_only_where_automatic_config_would
            # runs the real automatic_config() to keep the two in step.
            capacity = detail.get("capacity", 0)
            drives_it = has_battery and has_scheduler and isinstance(capacity, (int, float)) and capacity > 0

            inverters.append(
                inverter_record(
                    "fox:{}".format(serial),
                    inverter_type="FoxCloud" if drives_it else None,
                    composition="direct",
                    functions=functions,
                    capabilities=capabilities,
                    flags=flags,
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

Expected: a `PASS: Fox ...` line for each `build_discovery()` test. The two run() tests, registered last, still fail until Step 5 wires the call into `run()`.

- [ ] **Step 5: Wire the reporter into `run()`**

In `FoxAPI.run()`, add the call just **above** `if first and self.automatic:` - after the device poll and `publish_data()`, so the report describes what this cycle read, and outside any `if first:` block. It must precede `automatic_config()`, which raises when no device qualifies; placed after it, the report is never filed on exactly the installs whose dump most needs it. The last two lines below already exist:

```python
        # Unconditional, once per cycle and outside any one-shot gate, so a transient failure is
        # retried rather than lost - see ComponentBase.refresh_discovery(), which owns the
        # compare/guard loop, and never raises. Placed after the device poll and publish_data()
        # above so it describes what this cycle actually read.
        #
        # Deliberately BEFORE automatic_config() below, as GivTCP's run() does: automatic_config()
        # raises when no device qualifies as a battery inverter (a battery with no scheduler, say),
        # and that exception leaves run() every retry. Called after it, the report would never be
        # filed on exactly the installs whose debug dump most needs to say why. automatic_config()
        # reads nothing this call writes, so it still runs, and still raises, exactly as before.
        self.refresh_discovery()

        # Automatic configuration on first run
        if first and self.automatic:
            await self.automatic_config()
```

- [ ] **Step 6: Run the full suite**

```bash
cd coverage
python3 ../apps/predbat/unit_test.py > /tmp/t4-full.log 2>&1; echo "exit=$?"; tail -2 /tmp/t4-full.log
```

Expected: `All tests passed`, exit 0, after about two minutes - a focused run reports ~0.0s, so a "full" run that finishes in seconds is the wrong command. If an unrelated suite fails, check it fails on `origin/main` too before assuming this task caused it.

- [ ] **Step 7: Verify the tests are load-bearing**

Make each change below in turn, re-run `--test fox_api`, confirm the named test fails, and restore:

- Report `ratings["battery_entries"] = len(battery_list)` instead of the two capacity ratings: `test_fox_build_discovery_battery_ratings_tell_the_aio_bug_from_a_healthy_stack` fails, both the AIO and the four-module stack reading 5.
- Gate `inverter_type` on `has_battery` alone: `test_fox_build_discovery_sets_inverter_type_only_where_automatic_config_would` fails on its "no scheduler" case; drop only the capacity term and it fails on "zero capacity".
- Move the `self.refresh_discovery()` call below the `automatic_config()` step: `test_fox_run_reports_discovery_when_automatic_config_fails` fails with 0 reports filed.
- Wrap the call in `if first:`: `test_fox_run_reports_discovery_and_survives_a_failure` fails on its second cycle.

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

_As executed, the builder paragraph went after rule 2, once the numbered list closes, so it no longer splits the two rules, and says "keyword-only" rather than "named" parameters; and the Fox reporter note now reads "No reporter (...)" rather than "No v1 reporter (...)", since Fox was not one of the spec's original v1 reporters. `docs/discovery-catalogue.md` is authoritative._

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

One decision is needed **before** Plan 2 is written:

- **Entity maps for the cloud inverter reporters.** Only GivTCP's records carry an `entities` map, because it has a table mapping Predbat's controls to its entities (`GIVTCP_CONTROLS`). GE Cloud and Fox carry none - and yet each cloud integration's `automatic_config()` already maps every Predbat standard name to a per-device entity (Fox's writes `soc_percent`, `charge_limit`, `reserve` and the rest through `set_arg()`). An entity map is therefore not new design work: it means extracting that table into a shared constant read by both `automatic_config()` and `build_discovery()`. It was deferred on this branch because the extraction touches a control path. Without it, every reporter ships with no entity map, and the future allocator has nothing to allocate for any vendor except GivTCP. Decide whether each vendor plan does that extraction as its own task, or one plan does it for GE Cloud and Fox first.

Two things that should be revisited once Plan 2 or 3 lands, rather than designed now:

- **Whether `inverter_record()` wants a sibling for `chargers`/`cars`/`meters`.** Those sections have one or two reporters each today; the same ladder will start repeating there once myenergi and Kraken arrive. Extract it when there is a third caller, not before.
- **Whether `capabilities` needs a shared vocabulary.** Fox contributes the spec's `schedule` and a token of its own, `export_limit` (with `third_party_gen` as a flag, since it describes topology); GivTCP contributes `rest_v3`, `pause_mode`, `soh`. If two vendors coin different tokens for the same capability the section stops being comparable across a fleet, which is most of its value. Worth a pass once four or five reporters have contributed tokens.
