# Discovery Catalogue v1 (Observe Only) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Components describe what they discovered into a single assembled, redacted catalogue published in the debug YAML dump, so real user topologies can be read before any allocation rules are designed.

**Architecture:** New `apps/predbat/coordinator.py` holds a thread-safe `Coordinator`. Components report a dict built from typed containers during their first successful `run()`; the coordinator validates, assembles by category, derives a status for every component in the registry, records observed conflicts, and redacts at source. Five components gain a `build_discovery()` and one reporting call. **No existing behaviour changes** — nothing is allocated and nothing is written to `self.args`.

**Tech Stack:** Python 3, stdlib only (`hashlib`, `secrets`, `threading`). Existing test harness (`unit_test.py` + `MockBase`), Storage component for the pseudonym salt.

**Spec:** `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md`

## Global Constraints

- Branch `feat/discovery-coordinator` already exists and is checked out; both spec documents are committed on it. Do not create a new branch.
- CLAUDE.md mandates: run `impact({target: "<symbol>", direction: "upstream"})` before modifying any existing function/method named in a task, and `detect_changes()` before every commit; warn on HIGH/CRITICAL. The GitNexus index is stale — run `node .gitnexus/run.cjs analyze` once before Task 4, the first task that touches existing symbols.
- Tests run from `coverage/` (`source setup.csh` first time). **Always** redirect output to a log file and grep it: `./run_all --test coordinator > test_coordinator.log 2>&1; grep -E "PASS|FAIL|Error" test_coordinator.log`. Never commit `.log` files.
- `./run_pre_commit` lives in `coverage/`, not the repo root. It runs the hooks **and** the full test suite. `git add` new files before running it (pre-commit skips untracked files).
- 100% docstring coverage (`interrogate`): every function, class and method needs one. British English (`en-gb`) for CSpell; new words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted, so re-stage after.
- Line length 256 (Black) / 250 (Flake8). Naming `lower_case_with_underscores`.
- **The v1 invariant:** no task may write to `self.args`, change an `automatic_config()`, or alter any existing behaviour. Task 10 asserts this.
- `run_async` is imported from `ha` (`from ha import run_async`), as `predbat.py:94` does.

## Container model (used by every task)

A record is a small set of structural fields plus typed containers. Only structural fields are enumerated by the spec; containers extend freely because their type constraint provides the safety.

| Container | Accepts | On share |
|---|---|---|
| `hardware_ids` | Strings ≤64 identifying a device (serial, model number); rejects `@` | clear |
| `account_ids` | Any scalar identifying a person, supply point or account | pseudonymised |
| `info` | Vendor descriptive strings ≤64; rejects `@` | clear |
| `ratings` | int / float / bool only | clear |
| `coverage` | int / float / bool, or vocabulary tokens | clear |
| `functions`, `capabilities`, `flags`, `effects` | Lists of tokens matching `^[a-z0-9_]{1,32}$` | clear |
| `entities` | Entity descriptors keyed by Predbat standard name | clear |

---

### Task 1: Coordinator core — containers, validation, reports

**Files:**
- Create: `apps/predbat/coordinator.py`
- Create: `apps/predbat/tests/test_coordinator.py`
- Modify: `apps/predbat/unit_test.py` (import block ~line 263; `TEST_REGISTRY` ~line 654)

**Interfaces:**
- Produces: `Coordinator(base)` with `report(component_name, report)` and attribute `reports: dict`; module constants `SCHEMA_VERSION = 1`, `CONTAINER_SPEC`, `VOCAB_CONTAINERS`, `SECTION_SPEC`; helper `validate_report(report, component_name, log)` returning the cleaned dict.
- Consumes: `MockBase` from `mock_base.py`; `is_secret_key` from `utils`.
- Contract: validation **drops** offending values and logs; it never raises and never partially fails a report.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_coordinator.py`:

```python
# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery catalogue coordinator (coordinator.py) - container validation and report collection."""

from mock_base import MockBase
from coordinator import Coordinator, SCHEMA_VERSION


def _coordinator():
    """A Coordinator on a bare MockBase."""
    base = MockBase()
    return base, Coordinator(base)


def test_report_keeps_valid_containers():
    """A well-formed inverter record survives validation intact."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{
        "device_id": "givtcp:SN1",
        "inverter_type": "GE",
        "functions": ["solar", "battery"],
        "hardware_ids": {"serial": "SN1"},
        "info": {"firmware": "D0.451"},
        "ratings": {"battery_kwh": 9.5, "max_charge_w": 3600},
        "entities": {"soc_kw": {"entity_id": "sensor.predbat_givtcp_0_soc_kw", "domain": "sensor", "access": "r", "unit": "kWh"}},
    }]})
    record = coordinator.reports["givtcp"]["inverters"][0]
    assert record["hardware_ids"] == {"serial": "SN1"}
    assert record["ratings"]["battery_kwh"] == 9.5
    assert record["functions"] == ["solar", "battery"]
    assert record["entities"]["soc_kw"]["entity_id"] == "sensor.predbat_givtcp_0_soc_kw"
    print("PASS: valid containers preserved")
    return 0


def test_ratings_rejects_non_numeric():
    """A numbers-only container drops a string value rather than emitting it - the structural safety guarantee."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "ratings": {"battery_kwh": 9.5, "owner": "Bob Smith, 12 Acacia Ave"}}]})
    ratings = coordinator.reports["givtcp"]["inverters"][0]["ratings"]
    assert ratings == {"battery_kwh": 9.5}, ratings
    print("PASS: non-numeric dropped from ratings")
    return 0


def test_vocabulary_rejects_free_text():
    """Vocabulary lists take short lowercase tokens only, so free text cannot ride in on one."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "functions": ["battery", "My Home Battery!", "SOLAR"]}]})
    assert coordinator.reports["givtcp"]["inverters"][0]["functions"] == ["battery"]
    print("PASS: vocabulary constraint enforced")
    return 0


def test_info_rejects_email_shaped_and_long_strings():
    """info takes vendor descriptors, not addresses - an @ or an over-long string is dropped."""
    base, coordinator = _coordinator()
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1", "info": {"vendor": "Ohme", "login": "bob@example.com", "notes": "x" * 200}}]})
    assert coordinator.reports["ohme"]["chargers"][0]["info"] == {"vendor": "Ohme"}
    print("PASS: info rejects email-shaped and over-long values")
    return 0


def test_credential_named_field_rejected():
    """A field whose NAME trips is_secret_key is refused in any container, however it is nested."""
    base, coordinator = _coordinator()
    coordinator.report("fox", {"inverters": [{"device_id": "fox:SN1", "info": {"model": "H3", "api_key": "abcd1234", "auth_token": "zzz"}}]})
    assert coordinator.reports["fox"]["inverters"][0]["info"] == {"model": "H3"}
    print("PASS: credential-named fields refused")
    return 0


def test_unknown_container_dropped():
    """A container this schema does not know is dropped - there is no untyped path into the catalogue."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "scratchpad": {"anything": "at all"}}]})
    assert "scratchpad" not in coordinator.reports["givtcp"]["inverters"][0]
    print("PASS: unknown container dropped")
    return 0


def test_record_without_device_id_dropped():
    """device_id is the one genuinely required field; a record without it cannot be referred to, so it is dropped."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"hardware_ids": {"serial": "SN1"}}, {"device_id": "givtcp:SN2"}]})
    records = coordinator.reports["givtcp"]["inverters"]
    assert len(records) == 1 and records[0]["device_id"] == "givtcp:SN2"
    print("PASS: record without device_id dropped")
    return 0


def test_report_is_idempotent_and_versioned():
    """Re-reporting replaces the prior report, and the schema version is stamped."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}, {"device_id": "givtcp:B"}]})
    assert len(coordinator.reports["givtcp"]["inverters"]) == 2
    assert coordinator.reports["givtcp"]["schema_version"] == SCHEMA_VERSION
    print("PASS: report replaces and is versioned")
    return 0


def test_meter_sub_record_validated():
    """A meter's nested tariff record is validated with the same container rules."""
    base, coordinator = _coordinator()
    coordinator.report("octopus", {"meters": [{
        "device_id": "octopus:m1", "direction": "import",
        "account_ids": {"mpan": "1234567890123"},
        "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A"}, "flags": ["agile"], "ratings": {"standing_charge_p": 47.5}, "secret_token": "nope"},
    }]})
    tariff = coordinator.reports["octopus"]["meters"][0]["tariff"]
    assert tariff["info"]["tariff_code"] == "E-1R-AGILE-24-10-01-A"
    assert tariff["flags"] == ["agile"]
    assert "secret_token" not in tariff
    print("PASS: nested tariff record validated")
    return 0


def test_coordinator_all(my_predbat=None):
    """Run every coordinator test, returning the number of failures."""
    failures = 0
    failures += test_report_keeps_valid_containers()
    failures += test_ratings_rejects_non_numeric()
    failures += test_vocabulary_rejects_free_text()
    failures += test_info_rejects_email_shaped_and_long_strings()
    failures += test_credential_named_field_rejected()
    failures += test_unknown_container_dropped()
    failures += test_record_without_device_id_dropped()
    failures += test_report_is_idempotent_and_versioned()
    failures += test_meter_sub_record_validated()
    return failures
```

- [ ] **Step 2: Register the test and verify it fails**

In `apps/predbat/unit_test.py`, beside the other tests imports: `from tests.test_coordinator import test_coordinator_all`, and in `TEST_REGISTRY` beside the `components` entry: `("coordinator", test_coordinator_all, "Discovery catalogue coordinator tests", False),`

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -E "PASS|FAIL|ModuleNotFound" test_coordinator.log`
Expected: FAIL — `ModuleNotFoundError: No module named 'coordinator'`

- [ ] **Step 3: Implement coordinator.py**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Discovery catalogue: what each component found, assembled into one document.

Components report inverters, chargers, cars, meters, forecast providers and flexibility
programmes as plain dicts during their first successful run. The coordinator validates,
assembles and redacts them into a catalogue published in the debug YAML dump, so real user
topologies can be read. Nothing here allocates anything or writes to self.args - see
docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md.

Safety comes from typed containers rather than from enumerating every field: a record is a
few structural fields plus containers that each declare what they accept and what happens
to them when shared. A container taking only numbers cannot leak a name or a credential
however the catalogue grows, so components add facts freely by choosing a container.
"""

import re
import threading

from utils import is_secret_key

SCHEMA_VERSION = 1

MAX_STRING = 64
VOCAB_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Containers whose value is a list of vocabulary tokens
VOCAB_CONTAINERS = ("functions", "capabilities", "flags", "effects")

# Descriptor attributes carried through; anything else on a descriptor is dropped
DESCRIPTOR_FIELDS = ("entity_id", "domain", "access", "unit", "device_class", "min", "max", "step", "options", "format", "precision")

SECTION_SPEC = {
    "inverters": {"structural": ("device_id", "inverter_type", "control", "composition", "measures_meter", "serials"), "sub_records": ()},
    "chargers": {"structural": ("device_id", "serves_cars"), "sub_records": ()},
    "cars": {"structural": ("device_id", "charged_by"), "sub_records": ()},
    "meters": {"structural": ("device_id", "direction"), "sub_records": ("tariff",)},
    "forecasts": {"structural": ("device_id", "kind"), "sub_records": ()},
    "programmes": {"structural": ("device_id", "kind", "meter"), "sub_records": ()},
}


def _clean_string(value):
    """A vendor descriptor string, or None if it is not one.

    Length-capped and free of "@" so an address, an email or a pasted blob cannot ride in on
    a descriptive field. Vendor model and firmware strings are comfortably inside the cap.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_STRING or "@" in value:
        return None
    return value


def _clean_number(value):
    """A numeric or boolean fact, or None. Strings are refused however numeric they look."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return None


def _clean_token(value):
    """A vocabulary token (lower case, no spaces), or None."""
    return value if isinstance(value, str) and VOCAB_RE.match(value) else None


def _clean_scalar(value):
    """Any scalar, for account_ids - the container pseudonymises whatever it holds, so its type is open."""
    return value if isinstance(value, (str, int, float, bool)) else None


def _clean_token_list(value):
    """A list of vocabulary tokens, dropping any entry that is not one."""
    if not isinstance(value, list):
        return None
    return [token for token in (_clean_token(entry) for entry in value) if token is not None]


def _clean_descriptor_map(value):
    """An entities map: Predbat standard name -> descriptor, keeping only known descriptor fields."""
    if not isinstance(value, dict):
        return None
    out = {}
    for name, descriptor in value.items():
        if not isinstance(name, str) or not isinstance(descriptor, dict):
            continue
        if not isinstance(descriptor.get("entity_id"), str):
            continue
        kept = {}
        for field in DESCRIPTOR_FIELDS:
            if field in descriptor and descriptor[field] is not None:
                kept[field] = descriptor[field]
        out[name] = kept
    return out


# container name -> (redaction class, value cleaner applied per key, or None for whole-value cleaners)
CONTAINER_SPEC = {
    "hardware_ids": ("clear", _clean_string),
    "account_ids": ("pseudonym", _clean_scalar),
    "info": ("clear", _clean_string),
    "ratings": ("clear", _clean_number),
    "coverage": ("clear", _clean_number),
}


class Coordinator:
    """Collects component discovery reports and assembles them into one catalogue.

    Thread-safe: components report from their own threads while assembly runs on the main
    thread at startup.
    """

    def __init__(self, base):
        """Create an empty coordinator."""
        self.base = base
        self.log = base.log
        self.lock = threading.Lock()
        self.reports = {}

    def report(self, component_name, report):
        """Validate and store one component's discovery report, replacing any previous one."""
        cleaned = validate_report(report, component_name, self.log)
        with self.lock:
            self.reports[component_name] = cleaned
        counts = ", ".join("{} {}".format(len(cleaned.get(section, [])), section) for section in SECTION_SPEC if cleaned.get(section))
        self.log("Coordinator: {} reported {}".format(component_name, counts or "nothing"))


def _validate_container(container_name, value, component_name, section, log):
    """Clean one container's contents against its declared type, dropping and logging what does not fit."""
    if container_name in VOCAB_CONTAINERS:
        return _clean_token_list(value)
    if container_name == "entities":
        return _clean_descriptor_map(value)
    _, cleaner = CONTAINER_SPEC[container_name]
    if not isinstance(value, dict):
        return None
    out = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            continue
        if is_secret_key(key):
            log("Warn: Coordinator: {} {} field '{}' looks like a credential - refused".format(component_name, section, key))
            continue
        cleaned = cleaner(entry)
        if cleaned is None:
            log("Warn: Coordinator: {} {}.{} value does not fit the container's type - dropped".format(component_name, container_name, key))
            continue
        out[key] = cleaned
    return out


def _validate_record(record, section, component_name, log):
    """Clean one record: its structural fields, its containers and any nested sub-records."""
    if not isinstance(record, dict) or not isinstance(record.get("device_id"), str):
        log("Warn: Coordinator: {} {} record without a device_id - dropped".format(component_name, section))
        return None
    spec = SECTION_SPEC[section]
    out = {}
    for field in spec["structural"]:
        if field not in record or record[field] is None:
            continue
        value = record[field]
        if isinstance(value, list):
            out[field] = [entry for entry in value if isinstance(entry, str) and len(entry) <= MAX_STRING]
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            out[field] = value
        elif isinstance(value, str) and len(value) <= MAX_STRING:
            out[field] = value
    for container_name in list(CONTAINER_SPEC) + list(VOCAB_CONTAINERS) + ["entities"]:
        if container_name in record:
            cleaned = _validate_container(container_name, record[container_name], component_name, section, log)
            if cleaned:
                out[container_name] = cleaned
    for sub_name in spec["sub_records"]:
        sub = record.get(sub_name)
        if isinstance(sub, dict):
            sub_out = {}
            for container_name in list(CONTAINER_SPEC) + list(VOCAB_CONTAINERS) + ["entities"]:
                if container_name in sub:
                    cleaned = _validate_container(container_name, sub[container_name], component_name, section, log)
                    if cleaned:
                        sub_out[container_name] = cleaned
            if sub_out:
                out[sub_name] = sub_out
    return out


def validate_report(report, component_name, log):
    """Return a cleaned copy of one component's report - never raises, drops what does not fit."""
    cleaned = {"schema_version": SCHEMA_VERSION, "automatic": bool(report.get("automatic", True))}
    for section in SECTION_SPEC:
        records = []
        for record in report.get(section, []) or []:
            validated = _validate_record(record, section, component_name, log)
            if validated:
                records.append(validated)
        if records:
            cleaned[section] = records
    return cleaned
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -cE "^PASS" test_coordinator.log; grep -E "FAIL|Error|Traceback" test_coordinator.log`
Expected: 9 PASS, no failures.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/coordinator.py apps/predbat/tests/test_coordinator.py apps/predbat/unit_test.py
./run_pre_commit   # from coverage/
git commit -m "feat(coordinator): discovery report containers and validation"
```

---

### Task 2: Assembly — merge, component status, observations

**Files:**
- Modify: `apps/predbat/coordinator.py`
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Produces: `Coordinator.assemble()` returning the unredacted assembled dict with keys `schema_version`, `generated`, `components`, the six section lists, and `observations`; `Coordinator.assembled` caching the last result.
- Status values per component in `COMPONENT_LIST`: `ok` (reported), `no_report` (active and alive but does not report — the unmigrated majority, not an error), `not_started` (active but `is_alive()` false, i.e. started late or timed out), `load_error` (constructed and failed, with `error`), `not_configured` (never initialised).
- Conflict kinds: `duplicate_serial`, `multiple_inverter_sources`, `multiple_import_meters`, `contested_car_slots`.
- Consumes: `base.components` (`get_all()`, `is_active()`, `is_alive()`, `load_error()`) when present; degrades to reported-components-only when `base.components` is None.

- [ ] **Step 1: Write the failing tests** (append; register each in `test_coordinator_all`)

```python
class _StubRegistry:
    """Stands in for Components so assemble() can derive a status for every registry entry."""

    def __init__(self, active=(), alive=(), errors=None, all_names=()):
        """Record which component names are active, alive, failed to load, and known at all."""
        self._active = set(active)
        self._alive = set(alive)
        self._errors = errors or {}
        self._all = list(all_names)

    def get_all(self):
        """Every component name the registry knows."""
        return list(self._all)

    def is_active(self, name):
        """Whether the component was constructed."""
        return name in self._active

    def is_alive(self, name):
        """Whether the component is running and fresh."""
        return name in self._alive

    def load_error(self, name):
        """Why the component failed to construct, or None."""
        return self._errors.get(name)


def test_assemble_merges_sections_and_tags_source():
    """Records from several components merge into one list per section, each tagged with its source."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A", "inverter_type": "GE"}]})
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:b", "inverter_type": "GEC"}]})
    catalogue = coordinator.assemble()
    sources = sorted(record["source"] for record in catalogue["inverters"])
    assert sources == ["gecloud", "givtcp"], sources
    assert catalogue["schema_version"] == SCHEMA_VERSION
    assert catalogue["generated"]
    print("PASS: sections merged and tagged with source")
    return 0


def test_assemble_component_status():
    """Every registry entry gets a status, distinguishing silent from timed out from failed from absent."""
    base, coordinator = _coordinator()
    base.components = _StubRegistry(active=["givtcp", "octopus", "gecloud"], alive=["givtcp", "octopus"],
                                    errors={"fox": "No module named 'protobuf'"},
                                    all_names=["givtcp", "octopus", "gecloud", "fox", "solis"])
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    components = coordinator.assemble()["components"]
    assert components["givtcp"]["status"] == "ok"
    assert components["octopus"]["status"] == "no_report"      # alive, simply does not report yet
    assert components["gecloud"]["status"] == "not_started"    # active but not alive
    assert components["fox"]["status"] == "load_error"
    assert components["solis"]["status"] == "not_configured"
    print("PASS: component statuses derived")
    return 0


def test_observations_duplicate_serial():
    """The same serial claimed by two components is recorded, not resolved."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "hardware_ids": {"serial": "SA2242"}}]})
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:sn1", "hardware_ids": {"serial": "sa2242"}}]})
    conflicts = coordinator.assemble()["observations"]["conflicts"]
    duplicate = [entry for entry in conflicts if entry["kind"] == "duplicate_serial"]
    assert duplicate and sorted(duplicate[0]["claimed_by"]) == ["gecloud", "givtcp"]
    assert any(entry["kind"] == "multiple_inverter_sources" for entry in conflicts)
    print("PASS: duplicate serial and multiple sources observed")
    return 0


def test_observations_contested_cars_and_meters():
    """A charger component and an intelligent-device component both claiming cars is recorded, as are two import meters."""
    base, coordinator = _coordinator()
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1"}], "cars": [{"device_id": "ohme:v1"}]})
    coordinator.report("octopus", {"cars": [{"device_id": "octopus:d1"}], "meters": [{"device_id": "octopus:m1", "direction": "import"}]})
    coordinator.report("kraken", {"meters": [{"device_id": "kraken:m1", "direction": "import"}]})
    conflicts = coordinator.assemble()["observations"]["conflicts"]
    kinds = {entry["kind"] for entry in conflicts}
    assert "contested_car_slots" in kinds and "multiple_import_meters" in kinds, kinds
    print("PASS: contested cars and multiple import meters observed")
    return 0


def test_observations_resulting_config():
    """The config discovery did NOT set is recorded alongside it, for comparison."""
    base, coordinator = _coordinator()
    base.args.update({"num_inverters": 2, "num_cars": 1, "inverter_type": ["GE", "GEC"]})
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    resulting = coordinator.assemble()["observations"]["resulting_config"]
    assert resulting["num_inverters"] == 2 and resulting["inverter_type"] == ["GE", "GEC"]
    print("PASS: resulting config recorded")
    return 0
```

- [ ] **Step 2: Run, verify the new tests fail** (`AttributeError: ... no attribute 'assemble'`)

- [ ] **Step 3: Implement** — add to `coordinator.py` (`from datetime import datetime, timezone` at the top):

```python
    def assemble(self):
        """Merge every report into one catalogue, with a status per component and the observation layer.

        Unredacted: catalogue() is what consumers get. Called after phase-1 startup, which is
        the point at which every component has started or timed out.
        """
        with self.lock:
            reports = {name: report for name, report in self.reports.items()}
        catalogue = {"schema_version": SCHEMA_VERSION, "generated": datetime.now(timezone.utc).isoformat(), "components": self._component_status(reports)}
        for section in SECTION_SPEC:
            merged = []
            for name in sorted(reports):
                for record in reports[name].get(section, []):
                    entry = {"source": name}
                    entry.update(record)
                    merged.append(entry)
            catalogue[section] = merged
        catalogue["observations"] = {"conflicts": self._conflicts(catalogue), "resulting_config": self._resulting_config()}
        self.assembled = catalogue
        return catalogue

    def _component_status(self, reports):
        """A status for every component the registry knows, not only those that reported.

        A component that never reports is not an error: in this version only a handful report
        at all, so "no_report" has to read differently from "started but never answered".
        """
        components = getattr(self.base, "components", None)
        names = components.get_all() if components else sorted(reports)
        out = {}
        for name in names:
            entry = {"status": "not_configured", "reported_at": None}
            if name in reports:
                entry["status"] = "ok"
                entry["automatic"] = reports[name].get("automatic", True)
                entry["counts"] = {section: len(reports[name][section]) for section in SECTION_SPEC if reports[name].get(section)}
            elif components and components.load_error(name):
                entry["status"] = "load_error"
                entry["error"] = components.load_error(name)
            elif components and components.is_active(name):
                entry["status"] = "no_report" if components.is_alive(name) else "not_started"
            out[name] = entry
        return out

    def _conflicts(self, catalogue):
        """Collisions that today resolve silently by component ordering - recorded, never resolved."""
        conflicts = []
        serials = {}
        for record in catalogue["inverters"]:
            serial = record.get("hardware_ids", {}).get("serial")
            if serial:
                serials.setdefault(str(serial).casefold(), set()).add(record["source"])
        for serial, sources in sorted(serials.items()):
            if len(sources) > 1:
                conflicts.append({"kind": "duplicate_serial", "serial": serial, "claimed_by": sorted(sources)})
        inverter_sources = sorted({record["source"] for record in catalogue["inverters"]})
        if len(inverter_sources) > 1:
            conflicts.append({"kind": "multiple_inverter_sources", "claimed_by": inverter_sources})
        import_sources = sorted({record["source"] for record in catalogue["meters"] if record.get("direction") == "import"})
        if len(import_sources) > 1:
            conflicts.append({"kind": "multiple_import_meters", "claimed_by": import_sources})
        charger_sources = {record["source"] for record in catalogue["chargers"]}
        car_sources = {record["source"] for record in catalogue["cars"]}
        if charger_sources and (car_sources - charger_sources):
            conflicts.append({"kind": "contested_car_slots", "claimed_by": sorted(car_sources | charger_sources)})
        return conflicts

    def _resulting_config(self):
        """What apps.yaml actually ended up as, so every dump compares discovered against configured."""
        return {key: self.base.get_arg(key, None) for key in ("num_inverters", "num_cars", "inverter_type")}
```

Add `self.assembled = None` to `__init__`.

- [ ] **Step 4: Run tests, verify pass** (14 PASS)

- [ ] **Step 5: Commit**

```bash
./run_pre_commit && git add -u && git commit -m "feat(coordinator): assemble catalogue with component status and observed conflicts"
```

---

### Task 3: Redaction

**Files:**
- Modify: `apps/predbat/coordinator.py`
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Produces: `Coordinator.catalogue()` (redacted, what every consumer gets), `Coordinator.catalogue_raw()` (unredacted, in-process only), `Redactor(salt)` with `redact(catalogue)`, `Coordinator.load_salt()`.
- Salt: `secrets.token_hex(16)`, loaded from / saved to Storage as `("coordinator", "salt")` via `run_async`; when no Storage exists (MockBase, CLI harnesses) a per-process random salt is generated so nothing ever falls back to unsalted.
- Pseudonym form: `"#" + sha256(salt + str(value))[:8]`. Applied to every value in an `account_ids` container, to the `device_id` of any record that has an `account_ids` container, and to the cross-link fields `meter` and `measures_meter`.
- Substitution: every pseudonymised original of length ≥ 6 is also replaced wherever it appears inside any string in the catalogue, entity ids included.
- Value-shape guard: in a clear container, a value that is a digit run of ≥ 10 characters, contains `@`, or is a float pair shaped like coordinates is pseudonymised defensively and logged as misfiled.

- [ ] **Step 1: Write the failing tests** (append; register in `test_coordinator_all`)

```python
def _redacting_coordinator():
    """A coordinator with a fixed salt, so pseudonym tokens are reproducible inside one test."""
    base, coordinator = _coordinator()
    coordinator.salt = "test-salt-0001"
    return base, coordinator


def test_account_ids_pseudonymised_and_stable():
    """account_ids values never appear in the clear, and the same value maps to the same token throughout."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {
        "meters": [{"device_id": "octopus:1234567890123", "direction": "import", "account_ids": {"mpan": "1234567890123", "account": "A-1234ABCD"}}],
        "programmes": [{"device_id": "axle:site1", "kind": "vpp", "meter": "octopus:1234567890123"}],
    })
    catalogue = coordinator.catalogue()
    text = str(catalogue)
    assert "1234567890123" not in text, "raw MPAN must not survive redaction"
    assert "A-1234ABCD" not in text
    meter = catalogue["meters"][0]
    assert meter["account_ids"]["mpan"].startswith("#")
    # the cross-link still resolves to the same meter
    assert catalogue["programmes"][0]["meter"] == meter["device_id"]
    print("PASS: account ids pseudonymised, cross-link preserved")
    return 0


def test_pseudonym_differs_across_salts():
    """The same value under a different installation salt produces a different token."""
    base_a, coordinator_a = _redacting_coordinator()
    base_b, coordinator_b = _coordinator()
    coordinator_b.salt = "a-different-salt"
    report = {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"mpan": "1234567890123"}}]}
    coordinator_a.report("octopus", report)
    coordinator_b.report("octopus", report)
    token_a = coordinator_a.catalogue()["meters"][0]["account_ids"]["mpan"]
    token_b = coordinator_b.catalogue()["meters"][0]["account_ids"]["mpan"]
    assert token_a != token_b
    print("PASS: tokens differ across installations")
    return 0


def test_pseudonym_substituted_inside_entity_ids():
    """A vendor that embeds an identifier in an entity name does not republish what the field just hid."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("solar", {"forecasts": [{
        "device_id": "solcast:abcdef123456", "kind": "solar",
        "account_ids": {"site_id": "abcdef123456"},
        "entities": {"pv_forecast_today": {"entity_id": "sensor.predbat_solcast_abcdef123456_today", "domain": "sensor", "access": "r"}},
    }]})
    catalogue = coordinator.catalogue()
    assert "abcdef123456" not in str(catalogue), catalogue["forecasts"][0]["entities"]
    print("PASS: pseudonymised value substituted inside entity ids")
    return 0


def test_serials_and_tariff_codes_stay_clear():
    """Hardware identity and product codes stay readable - they are what makes a bug report diagnosable."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SA2242G123", "hardware_ids": {"serial": "SA2242G123"}, "info": {"firmware": "D0.451"}}]})
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A"}}}]})
    text = str(coordinator.catalogue())
    assert "SA2242G123" in text and "D0.451" in text and "E-1R-AGILE-24-10-01-A" in text
    print("PASS: serials, firmware and tariff codes kept clear")
    return 0


def test_misfiled_identifier_caught_by_shape_guard():
    """An MPAN is a number, so ratings accepts it - the shape guard pseudonymises it anyway and logs."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "ratings": {"standing_charge_p": 47.5, "supply_number": 1234567890123}}]})
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    assert catalogue["meters"][0]["ratings"]["standing_charge_p"] == 47.5, "a real measurement is untouched"
    print("PASS: misfiled identifier caught by the shape guard")
    return 0


def test_catalogue_raw_is_unredacted():
    """catalogue_raw is the in-process view and keeps originals, so the redacted path is demonstrably doing work."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"mpan": "1234567890123"}}]})
    assert "1234567890123" in str(coordinator.catalogue_raw())
    print("PASS: raw catalogue retains originals")
    return 0
```

- [ ] **Step 2: Run, verify failures**

- [ ] **Step 3: Implement** — add to `coordinator.py` (`import hashlib`, `import secrets`, `from ha import run_async`):

```python
# A clear-container value shaped like an identifier rather than a measurement. An MPAN is a
# number so it satisfies ratings; this is the safety net for that, not the classifier.
DIGIT_RUN_RE = re.compile(r"^\d{10,}$")


class Redactor:
    """Applies the catalogue's redaction classes to an assembled document.

    Pseudonymises everything in account_ids and anything cross-linking to it, substitutes
    those originals wherever else they appear (entity ids included), and defensively
    pseudonymises identifier-shaped values misfiled into a clear container.
    """

    # Cross-link fields holding another record's pseudonymised device_id
    CROSS_LINKS = ("meter", "measures_meter")

    # Minimum length of an original before it is substituted inside other strings; below this a
    # substring replacement would corrupt unrelated text more often than it would hide anything.
    MIN_SUBSTITUTE = 6

    def __init__(self, salt, log=None):
        """Hold the installation salt and an optional logger for misfiled values."""
        self.salt = salt
        self.log = log
        self.originals = {}

    def token(self, value):
        """The stable pseudonym for one value under this installation's salt."""
        digest = hashlib.sha256((self.salt + str(value)).encode("utf-8")).hexdigest()
        return "#" + digest[:8]

    def _note(self, value):
        """Record an original so it is also substituted out of every other string later."""
        text = str(value)
        token = self.token(text)
        self.originals[text] = token
        return token

    def _misfiled(self, value):
        """Whether a clear-container value looks like an identifier rather than a measurement."""
        text = str(value)
        if "@" in text:
            return True
        return bool(DIGIT_RUN_RE.match(text.replace(" ", "")))

    def _walk(self, node, container=None):
        """Recursively redact a node, pseudonymising by container and catching misfiled values."""
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "account_ids":
                    out[key] = {name: self._note(entry) for name, entry in value.items()}
                elif key in ("hardware_ids", "info", "ratings", "coverage"):
                    cleaned = {}
                    for name, entry in value.items():
                        if self._misfiled(entry):
                            if self.log:
                                self.log("Warn: Coordinator: {}.{} looks like an identifier in a clear container - pseudonymised".format(key, name))
                            cleaned[name] = self._note(entry)
                        else:
                            cleaned[name] = entry
                    out[key] = cleaned
                else:
                    out[key] = self._walk(value, container=key)
            return out
        if isinstance(node, list):
            return [self._walk(entry, container=container) for entry in node]
        return node

    def _substitute(self, node):
        """Replace every noted original wherever it appears inside a string."""
        if isinstance(node, dict):
            return {key: self._substitute(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self._substitute(entry) for entry in node]
        if isinstance(node, str):
            for original, token in self.originals.items():
                if len(original) >= self.MIN_SUBSTITUTE and original in node:
                    node = node.replace(original, token)
            return node
        return node

    def redact(self, catalogue):
        """Return a redacted copy of an assembled catalogue."""
        walked = self._walk(catalogue)
        # device_id and cross-links are derived from identifiers, so they are substituted rather
        # than classified: the substitution pass rewrites them wherever the original appears.
        return self._substitute(walked)
```

And on `Coordinator`:

```python
    def load_salt(self):
        """The per-installation pseudonym salt, generated and stored on first use.

        Without Storage (CLI harnesses, unit tests) a per-process salt is generated instead, so
        redaction never silently falls back to an unsalted digest - a 13-digit MPAN under one of
        those is brute-forceable in seconds.
        """
        if self.salt:
            return self.salt
        components = getattr(self.base, "components", None)
        storage = components.get_component("storage") if components else None
        if storage:
            try:
                stored = run_async(storage.load("coordinator", "salt"))
                if isinstance(stored, dict) and stored.get("salt"):
                    self.salt = str(stored["salt"])
                    return self.salt
            except Exception as e:
                self.log("Warn: Coordinator: could not load the pseudonym salt: {}".format(e))
        self.salt = secrets.token_hex(16)
        if storage:
            try:
                run_async(storage.save("coordinator", "salt", {"salt": self.salt}, format="json"))
            except Exception as e:
                self.log("Warn: Coordinator: could not save the pseudonym salt: {}".format(e))
        return self.salt

    def catalogue(self):
        """The assembled catalogue, redacted. This is what every consumer gets."""
        return Redactor(self.load_salt(), log=self.log).redact(self.assembled or self.assemble())

    def catalogue_raw(self):
        """The assembled catalogue, unredacted. In-process diagnostics only - never write this anywhere."""
        return self.assembled or self.assemble()
```

Add `self.salt = None` to `__init__`.

- [ ] **Step 4: Run tests, verify pass** (21 PASS)

- [ ] **Step 5: Commit**

```bash
./run_pre_commit && git add -u && git commit -m "feat(coordinator): redact the catalogue at source with salted pseudonyms"
```

---

### Task 4: Lifecycle — registry ownership, reporting helper, debug dump, sensor

**Files:**
- Modify: `apps/predbat/components.py` (`Components.__init__` ~line 748)
- Modify: `apps/predbat/component_base.py` (`__init__` ~line 56)
- Modify: `apps/predbat/predbat.py` (`initialize()` ~line 1932)
- Modify: `apps/predbat/userinterface.py` (`create_debug_yaml` ~line 848)
- Modify: `apps/predbat/mock_base.py` (add `components = None` already present; no change expected — verify)
- Test: `apps/predbat/tests/test_coordinator.py`, `apps/predbat/tests/test_debug_yaml_scope.py`

**Interfaces:**
- Produces: `Components.coordinator` (a `Coordinator`); `ComponentBase.component_name` (registry key, set by `Components.initialize()`); `ComponentBase.report_discovery(report)` which finds the coordinator and calls `report(self.component_name, report)`, doing nothing when there is no coordinator (CLI harnesses); `Coordinator.publish()` writing `sensor.predbat_discovery`.
- `PredBat.initialize()` calls `self.components.coordinator.assemble()` after the phase-1 start block and again after phase 2, then `publish()`.
- `create_debug_yaml()` gains `debug["discovery"] = self.components.coordinator.catalogue()` guarded for a missing coordinator.

- [ ] **Step 1: Refresh the index and run impact analysis**

```bash
node .gitnexus/run.cjs analyze
```

Then `impact` upstream on `Components.__init__`, `ComponentBase.__init__`, `PredBat.initialize` and `create_debug_yaml`. Report the blast radius. Expect HIGH on the first two — state that every change is strictly additive (new attributes and a new method; no signature or behaviour change).

- [ ] **Step 2: Write the failing tests**

In `test_coordinator.py` (register in `test_coordinator_all`):

```python
def test_report_discovery_helper():
    """A component reports through the base-class helper, and does nothing harmless when there is no coordinator."""
    from component_base import ComponentBase

    class _FakeComponent(ComponentBase):
        """Minimal component used to exercise report_discovery."""

        def initialize(self, **kwargs):
            """Nothing to set up."""
            pass

        async def run(self, seconds, first):
            """Never called here."""
            return True

    base, coordinator = _coordinator()
    component = _FakeComponent(base)
    component.component_name = "givtcp"
    component.report_discovery({"inverters": [{"device_id": "givtcp:A"}]})   # no coordinator yet - must not raise
    assert coordinator.reports == {}

    class _Registry:
        """Registry stub exposing the coordinator, as Components does."""

        def __init__(self):
            """Hold the coordinator."""
            self.coordinator = coordinator

        def get_all(self):
            """One known component."""
            return ["givtcp"]

        def is_active(self, name):
            """Active."""
            return True

        def is_alive(self, name):
            """Alive."""
            return True

        def load_error(self, name):
            """No error."""
            return None

    base.components = _Registry()
    component.report_discovery({"inverters": [{"device_id": "givtcp:A"}]})
    assert coordinator.reports["givtcp"]["inverters"][0]["device_id"] == "givtcp:A"
    print("PASS: report_discovery routes to the coordinator and no-ops without one")
    return 0


def test_publish_writes_sensor():
    """The catalogue is published as an entity for the web viewer to read later."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A", "inverter_type": "GE"}]})
    coordinator.assemble()
    coordinator.publish()
    assert base.entities.get("sensor.predbat_discovery"), base.entities
    print("PASS: discovery sensor published")
    return 0
```

In `test_debug_yaml_scope.py`, add a test asserting `create_debug_yaml(write_file=False)` output contains a `discovery:` key and that a seeded MPAN does not appear in it. Follow that module's existing arrangement for building a Predbat with a debug dump.

- [ ] **Step 3: Run, verify failures**

Run: `cd coverage && ./run_all --test coordinator --test debug_yaml_scope > test_task4.log 2>&1; grep -E "FAIL|AttributeError" test_task4.log`

- [ ] **Step 4: Implement**

`coordinator.py` — add:

```python
    def publish(self):
        """Publish the redacted catalogue as an entity, for the web viewer and HA users."""
        catalogue = self.catalogue()
        counts = {section: len(catalogue.get(section, [])) for section in SECTION_SPEC}
        attributes = {"friendly_name": "Predbat discovery", "icon": "mdi:sitemap"}
        attributes.update(catalogue)
        self.base.dashboard_item("sensor.{}_discovery".format(self.base.prefix), state=sum(counts.values()), attributes=attributes)
```

`components.py` — `from coordinator import Coordinator` at the top; in `Components.__init__`: `self.coordinator = Coordinator(base)`; in `initialize()`, immediately after `self.components[component_name] = component_class(self.base, **arg_dict)`, add `self.components[component_name].component_name = component_name`.

`component_base.py` — in `__init__`, before `self.initialize(**kwargs)`: `self.component_name = self.__class__.__name__`. Add:

```python
    def report_discovery(self, report):
        """Report what this component discovered to the discovery catalogue.

        Silently does nothing when there is no coordinator - the standalone CLI harnesses run a
        component against a MockBase with no registry at all.
        """
        components = getattr(self.base, "components", None)
        coordinator = getattr(components, "coordinator", None) if components else None
        if coordinator:
            coordinator.report(self.component_name, report)
```

`predbat.py` — after the phase-2 start block (line ~1937), before `load_user_config(register=True)`:

```python
            # Discovery barrier: every component has now started or timed out, so assemble what
            # they reported into the catalogue. Observe only - nothing here changes configuration.
            try:
                self.components.coordinator.assemble()
                self.components.coordinator.publish()
            except Exception as e:
                self.log("Warn: Failed to assemble the discovery catalogue: {}".format(e))
```

Wrapped because an observer must never be able to break startup.

`userinterface.py` — in `create_debug_yaml`, after `debug["CONFIG_ITEMS"] = ...`:

```python
        # Explicit: "components" is in DEBUG_EXCLUDE_LIST so nothing under it is dumped
        # automatically, and a plain redacted dict avoids the object-graph walk described above.
        coordinator = self.components.coordinator if self.components else None
        if coordinator:
            try:
                debug["discovery"] = coordinator.catalogue()
            except Exception as e:
                self.log("Warn: Failed to add the discovery catalogue to the debug dump: {}".format(e))
```

- [ ] **Step 5: Run the affected suites, verify pass**

Run: `cd coverage && ./run_all --test coordinator --test debug_yaml_scope --test components --test component_base > test_task4.log 2>&1; grep -cE "^PASS" test_task4.log; grep -E "FAIL|Error|Traceback" test_task4.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(coordinator): wire the catalogue into startup, the debug dump and an entity"
```

---

### Task 5: GivTCP reporter

**Files:**
- Modify: `apps/predbat/givtcp.py` (`run()` ~lines 479-482)
- Test: `apps/predbat/tests/test_givtcp_component.py`

**Interfaces:**
- Produces: `GivTCPComponent.build_discovery()` returning `{"automatic": self.automatic, "inverters": [...]}`, one record per discovered endpoint. `device_id` is `"givtcp:{serial}"` falling back to `"givtcp:{rest_api}"` when no serial is reported. Descriptors are built from `GIVTCP_CONTROLS` (domain, `access: "rw"`) and `GIVTCP_SENSORS` (`access: "r"`), each carrying the same min/max/step/options/unit the component already publishes as HA attributes, with the per-device rate maximum from `rest.max_battery_rate()` overriding the generic ceiling. `functions` is `["solar", "battery"]`. `capabilities` records `rest_v3`, `pause_mode`, `pause_slots`, `soh`, `charge_enable`, `discharge_target` from the same probes `automatic_config()` already gates on.
- Reports from `run()` alongside the existing `automatic_config()` trigger. **`automatic_config()` itself is not modified.**

- [ ] **Step 1: Impact analysis** on `GivTCPComponent.run`; report.

- [ ] **Step 2: Write the failing tests** (append to `test_givtcp_component.py`, registered via its existing `test_givtcp_component` runner)

```python
def test_build_discovery_shape():
    """One record per discovered endpoint, with descriptors carrying the real per-device rate maximum."""
    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob()
    _mark_discovered(component)
    report = component.build_discovery()
    assert report["automatic"] is True
    record = report["inverters"][0]
    assert record["device_id"].startswith("givtcp:")
    assert record["inverter_type"] == "GE"
    assert "battery" in record["functions"] and "solar" in record["functions"]
    charge_rate = record["entities"]["charge_rate"]
    assert charge_rate["domain"] == "number" and charge_rate["access"] == "rw"
    assert charge_rate["entity_id"] == "number.predbat_givtcp_0_charge_rate"
    assert charge_rate["step"] == 100
    soc_kw = record["entities"]["soc_kw"]
    assert soc_kw["access"] == "r" and soc_kw["unit"] == "kWh"
    print("PASS: GivTCP discovery record shape")
    return 0


def test_build_discovery_uses_device_rate_max():
    """The descriptor carries the inverter's own maximum rate, not the generic 20kW ceiling."""
    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob()
    _mark_discovered(component)
    component.rest[0].max_battery_rate = lambda: 3600
    assert component.build_discovery()["inverters"][0]["entities"]["charge_rate"]["max"] == 3600
    print("PASS: per-device rate maximum used")
    return 0


def test_build_discovery_only_discovered_endpoints():
    """A configured but unanswered endpoint produces no record - the shipped apps.yaml over-provisions givtcp_rest."""
    base, component = _make_component(rest_urls=["http://a:6345", "http://b:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob()
    _mark_discovered(component, indices=[0])
    assert len(component.build_discovery()["inverters"]) == 1
    print("PASS: only discovered endpoints reported")
    return 0
```

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all --test givtcp_component > test_givtcp.log 2>&1; grep -E "FAIL|AttributeError" test_givtcp.log`

- [ ] **Step 4: Implement** — add `build_discovery()` to `givtcp.py`, and in `run()` report beside the existing trigger:

```python
        if self.discovered != self.configured_for:
            await self.automatic_config()
            self.configured_for = list(self.discovered)
            self.automatic_config_done = True
        if self.discovered != self.reported_for:
            self.report_discovery(self.build_discovery())
            self.reported_for = list(self.discovered)
```

with `self.reported_for = []` added in `initialize`. Reporting is deliberately independent of `self.automatic`: the catalogue describes what is there whether or not this component wired it.

- [ ] **Step 5: Run the module's suites, verify pass**: `cd coverage && ./run_all --test givtcp_component --test givtcp_rest > test_givtcp.log 2>&1; grep -cE "^PASS" test_givtcp.log; grep -E "FAIL|Error" test_givtcp.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(givtcp): report discovered inverters to the catalogue"
```

---

### Task 6: GE Cloud reporter

**Files:**
- Modify: `apps/predbat/gecloud.py` (`run()` first-pass block ~lines 1690-1694)
- Test: `apps/predbat/tests/test_ge_cloud.py`

**Interfaces:**
- Produces: `GECloudDirect.build_discovery(devices)` returning `{"automatic": ..., "inverters": [...], "meters": [...]}`.
  - One inverter record per battery device with `functions: ["solar", "battery"]`, plus one per entry in `devices["pv"]` with `functions: ["solar"]` — the sensor-only devices that motivated the separate index pool.
  - `composition` is `gateway` when the gateway fronts multiple batteries (the collapse at `gecloud.py:1148-1151`), `ems` on the EMS path, else `direct`; the fronted serials go in the structural `serials` list.
  - `capabilities` from the existing register sniffing: `charge_rate_power`, `charge_rate_percent`, `pause_mode`, `pause_slots`, `discharge_target`.
  - `measures_meter` set from the device's meter serial where GE Cloud knows it, so the shared-CT case (`gecloud.py:1281-1312`) shows up as two devices measuring one meter.
- Reports from the existing `if first:` block. `async_automatic_config()` is **not** modified; reporting happens whether or not `self.automatic` is set.

- [ ] **Step 1: Impact analysis** on `GECloudDirect.run`; report.

- [ ] **Step 2: Write the failing tests** in `test_ge_cloud.py`, reusing the devices/settings fixture from the existing `_test_async_automatic_config` (`test_ge_cloud.py:283`): (a) a battery-only fixture yields one inverter record with `composition: "direct"` and `functions` containing `battery`; (b) two batteries plus a gateway and no EMS yields records whose `composition` is `gateway` with both battery serials in `serials`; (c) with `ge_cloud_automatic_split_pv` set, each PV device yields a record with `functions == ["solar"]` and no `inverter_type`; (d) two devices sharing a meter serial both carry the same `measures_meter`.

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all --test ge_cloud > test_gecloud.log 2>&1; grep -E "FAIL|AttributeError" test_gecloud.log`

- [ ] **Step 4: Implement** `build_discovery(devices)` plus `self.report_discovery(self.build_discovery(self.devices_dict))` inside the existing `if first:` block, outside the `if self.automatic:` guard.

- [ ] **Step 5: Run, verify pass**: `cd coverage && ./run_all --test ge_cloud > test_gecloud.log 2>&1; grep -cE "^PASS" test_gecloud.log; grep -E "FAIL|Error" test_gecloud.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(gecloud): report inverters, PV-only devices and meters to the catalogue"
```

---

### Task 7: Octopus reporter — meters, tariffs and cars

**Files:**
- Modify: `apps/predbat/octopus.py` (beside the `automatic_config(self.tariffs)` calls at lines 678, 685 and 881)
- Test: `apps/predbat/tests/test_octopus_intelligent_devices.py`

**Interfaces:**
- Produces: `OctopusAPI.build_discovery()` returning `{"meters": [...], "cars": [...]}`.
  - One meter per direction present in `self.tariffs` (`import`, `export`, `gas`). `account_ids` carries `mpan` (where known) and `account`; `device_id` is `"octopus:{mpan or direction}"`. The nested `tariff` sub-record carries `info.tariff_code` and `info.product_code`, and `flags` built from the existing classifiers — `intelligent_go` from `is_intelligent_go_tariff()`, `six_hour_cap` from `has_six_hour_cap()`, plus `agile` when the product code contains `AGILE`.
  - One car per **active** (non-suspended) intelligent device, matching the filter `automatic_config()` already applies, with `device_id` `"octopus:{device_id}"`, `ratings` from the device's vehicle battery size and charge-point power where reported, and `entities` for `octopus_intelligent_slot`, `octopus_ready_time` and `octopus_charge_limit` built with the existing `get_entity_name(..., index=device_id_to_index_suffix(device_id))`.
- Reports beside each existing `automatic_config()` call, so a device-set or tariff change refreshes the report. `automatic_config()` is not modified.

- [ ] **Step 1: Impact analysis** on `OctopusAPI.automatic_config` and its `run()` call sites; report.

- [ ] **Step 2: Write the failing tests** in `test_octopus_intelligent_devices.py` using its existing device fixtures: (a) two active devices and one suspended yields exactly two car records; (b) an Intelligent Go tariff fixture yields a meter whose `tariff.flags` contains `intelligent_go`; (c) the car entity map matches the entity names the existing `automatic_config` tests assert; (d) an import and an export tariff yield two meter records with distinct `direction`.

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all -k octopus_ > test_octopus.log 2>&1; grep -E "FAIL|AttributeError" test_octopus.log`

- [ ] **Step 4: Implement** `build_discovery()` and add `self.report_discovery(self.build_discovery())` immediately after each of the three `automatic_config(self.tariffs)` calls.

- [ ] **Step 5: Run, verify pass**: `cd coverage && ./run_all -k octopus_ > test_octopus.log 2>&1; grep -cE "^PASS" test_octopus.log; grep -E "FAIL|Error" test_octopus.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(octopus): report meters, tariffs and intelligent-device cars to the catalogue"
```

---

### Task 8: Ohme reporter — the charger and car split

**Files:**
- Modify: `apps/predbat/ohme.py` (the `if first and self.client.serial:` block ~lines 259-266)
- Test: `apps/predbat/tests/test_ohme.py`

**Interfaces:**
- Produces: `OhmeAPI.build_discovery()` returning `{"automatic": self.ohme_automatic, "chargers": [...], "cars": [...]}`.
  - One charger: `device_id` `"ohme:{serial}"`, `hardware_ids.serial`, `info.vendor` `"Ohme"` plus model where known, `ratings.max_power_kw`, `serves_cars` listing the car it feeds, and `entities` for the charger facts (`car_charging_planned` → `binary_sensor.predbat_ohme_connected`, `car_charging_energy` → `ENERGY_TODAY_ENTITY`, `car_charging_power` → `POWER_WATTS_ENTITY`).
  - One car. When the Ohme API reports a current vehicle (`self.client._cars` / `current_vehicle`, discarded today), `device_id` is `"ohme:{vehicle id}"` with `info.make`/`info.model`; otherwise a stub with `device_id` `"ohme:{serial}:car"` and `info.stub` true. Its `entities` carry the car facts (`car_charging_soc` → `sensor.predbat_ohme_battery_percent`, plus the three Octopus Intelligent entities Ohme publishes), and `charged_by` links back to the charger.
- Reports inside the existing `if first and self.client.serial:` block, outside the `if self.ohme_automatic:` guard. Neither `automatic_config()` nor `octopus_intelligent_wanted()` is modified.

- [ ] **Step 1: Impact analysis** on `OhmeAPI.run` and `OhmeAPI.automatic_config`; report.

- [ ] **Step 2: Write the failing tests** in `test_ohme.py` using its existing client fixtures: (a) a charger record and a car record are produced, cross-linked in both directions; (b) with no vehicle known the car record is a stub with `info.stub` true; (c) with a vehicle known the car carries its make and model and a non-stub `device_id`; (d) charger entities and car entities land in the right record (`car_charging_planned` on the charger, `car_charging_soc` on the car).

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all --test ohme > test_ohme.log 2>&1; grep -E "FAIL|AttributeError" test_ohme.log`

- [ ] **Step 4: Implement** `build_discovery()` and add `self.report_discovery(self.build_discovery())` in the first-run block.

- [ ] **Step 5: Run, verify pass**: `cd coverage && ./run_all --test ohme > test_ohme.log 2>&1; grep -cE "^PASS" test_ohme.log; grep -E "FAIL|Error" test_ohme.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(ohme): report the charger and its car separately to the catalogue"
```

---

### Task 9: Solcast reporter — the forecasts section

**Files:**
- Modify: `apps/predbat/solcast.py` (site loop ~lines 560-580; `run()` ~line 96)
- Test: `apps/predbat/tests/test_solcast.py`

**Interfaces:**
- Produces: `SolarAPI.build_discovery()` returning `{"forecasts": [...]}`, one record per solar forecast provider actually used this fetch.
  - Solcast: one record per resource id seen in the site loop, accumulated into `self.discovered_sites` (a list, append-only, preserving order) as that loop walks them. `device_id` `"solcast:{resource_id}"`, `account_ids.site_id` the resource id (so it is pseudonymised), `kind` `"solar"`, `info.vendor` `"Solcast"`, `coverage.horizon_hours` 168 and `coverage.resolution_minutes` 30.
  - forecast.solar and Open-Meteo: one record each when enabled, `device_id` `"forecast_solar"` / `"open_meteo"`, with their own `coverage`.
  - `entities` carries whichever of `pv_forecast_today`, `pv_forecast_tomorrow`, `pv_forecast_d3`, `pv_forecast_d4` are configured.
  - No site *name* is reported — user-authored free text is excluded by the spec, and Solcast site names are user-chosen.
- Reports from `run()` after `fetch_pv_forecast()` returns, only when the discovered set has changed since the last report.

- [ ] **Step 1: Impact analysis** on `SolarAPI.run` and the site-fetch method; report.

- [ ] **Step 2: Write the failing tests** in `test_solcast.py` using its existing mocked-fetch fixtures: (a) two Solcast resource ids yield two forecast records with `kind: "solar"`; (b) the resource id appears in `account_ids`, not in `info`, so redaction pseudonymises it; (c) no user-authored site name appears anywhere in the record; (d) with forecast.solar enabled its own record is produced alongside.

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all --test solcast > test_solcast.log 2>&1; grep -E "FAIL|AttributeError" test_solcast.log`

- [ ] **Step 4: Implement** the `self.discovered_sites` accumulation, `build_discovery()`, and the report call in `run()`.

- [ ] **Step 5: Run, verify pass**: `cd coverage && ./run_all --test solcast --test solar_model > test_solcast.log 2>&1; grep -cE "^PASS" test_solcast.log; grep -E "FAIL|Error" test_solcast.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(solcast): report solar forecast providers to the catalogue"
```

---

### Task 10: End-to-end test, the v1 invariant, and documentation

**Files:**
- Create: `apps/predbat/tests/test_discovery_catalogue.py`
- Create: `docs/discovery-catalogue.md`
- Modify: `apps/predbat/unit_test.py`, `mkdocs.yml`

**Interfaces:**
- Consumes: every reporter from Tasks 5-9 plus the coordinator from Tasks 1-4.

- [ ] **Step 1: Write the end-to-end test**

One `MockBase` shared by a real `GivTCPComponent` and `MockGECloudDirect`, both reporting into one `Coordinator`, then `assemble()` and `catalogue()`. Assert: both components' inverters appear in one `inverters` list tagged by `source`; `observations.conflicts` contains `multiple_inverter_sources`; the assembled catalogue survives `yaml.safe_dump` and `json.dumps`; GE Cloud's PV-only device has `functions == ["solar"]` and no `inverter_type`; and every component in the stub registry has a status.

- [ ] **Step 2: Write the v1 invariant test**

The heart of this release. Snapshot `dict(base.args)` before any reporting, run every reporter, assemble, redact and publish, then assert `base.args` is byte-for-byte unchanged:

```python
def test_discovery_writes_no_config(my_predbat=None):
    """Discovery is observe-only: nothing it does may change a single apps.yaml key."""
    base, components = _build_reporting_fleet()
    before = copy.deepcopy(base.args)
    for component in components:
        component.report_discovery(component.build_discovery())
    base.components.coordinator.assemble()
    base.components.coordinator.catalogue()
    base.components.coordinator.publish()
    assert base.args == before, "discovery must not write configuration in v1"
    print("PASS: discovery changed no configuration")
    return 0
```

- [ ] **Step 3: Write the redaction corpus test**

Seed a report with a credential-shaped value, a 13-digit MPAN, an email, a postcode and a coordinate pair spread across several containers, then assert none of the original strings appears anywhere in `str(catalogue())`, while a serial, a firmware string and a tariff code all do.

- [ ] **Step 4: Register and run**

Add to `unit_test.py`: `from tests.test_discovery_catalogue import test_discovery_catalogue_all` and `("discovery_catalogue", test_discovery_catalogue_all, "End-to-end discovery catalogue assembly and the observe-only invariant", False),`

Run: `cd coverage && ./run_all --test discovery_catalogue > test_e2e.log 2>&1; grep -E "PASS|FAIL|Error|Traceback" test_e2e.log`

- [ ] **Step 5: Full suite regression**

Run: `cd coverage && ./run_all > test_full.log 2>&1; grep -E "FAIL|Error:" test_full.log | head -40`
Expected: no new failures against `main`. Any failure in an untouched area means a reporter changed behaviour it should not have.

- [ ] **Step 6: Write the documentation**

`docs/discovery-catalogue.md`: what the catalogue is and why it exists; how to find it (the `discovery:` section of a debug dump, and `sensor.predbat_discovery`); what each section describes; what is redacted and what deliberately is not, so users know a dump is safe to attach and maintainers know serials and tariff codes are readable; and a developer section on adding a reporter (`build_discovery()`, `report_discovery()`, choose a container, never invent a field outside one). Add the page to `mkdocs.yml`.

Verify: `mkdocs build > docs_build.log 2>&1; grep -iE "error|warning" docs_build.log`

- [ ] **Step 7: detect_changes, pre-commit, commit**

```bash
git add apps/predbat/tests/test_discovery_catalogue.py docs/discovery-catalogue.md
./run_pre_commit && git add -u && git commit -m "test(coordinator): end-to-end catalogue assembly and the observe-only invariant"
```

---

## Self-review notes

- Spec coverage: containers and validation → Task 1; assembly, status and observations → Task 2; the four redaction classes, salt, substitution and guards → Task 3; lifecycle, debug dump and sensor → Task 4; the five named reporters → Tasks 5-9; the observe-only non-goal → Task 10's invariant test; documentation → Task 10.
- Type consistency: `report(component_name, report)`, `build_discovery()` returning the report dict, `report_discovery(report)`, `assemble()`, `catalogue()`, `catalogue_raw()` and the container names are used identically in every task. Only GE Cloud's `build_discovery(devices)` takes an argument, which its Interfaces block states.
- Deliberate scope limits carried from the spec: no allocation, no writes, no `automatic_config()` changes, no persistence beyond the salt, no web page. Components beyond the five named keep reporting nothing and show as `no_report`, which is a status rather than an error.
