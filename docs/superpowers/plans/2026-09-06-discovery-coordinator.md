# Discovery Coordinator (Minimal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Components report discovery records into a JSON-serialisable catalogue held by a central coordinator, which allocates inverter slots, car indices and sensor indices after phase-1 startup; automatic configuration then writes apps.yaml keys at the assigned indices — unblocking multi-inverter automatic mode across two components (GivTCP + GECloud), proven by a test.

**Architecture:** New `apps/predbat/coordinator.py` holds a thread-safe `Coordinator` whose discovery catalogue is plain dicts (JSON-safe end to end; components may attach vendor facts beyond the required keys). `ComponentBase` gains a pending-assignment hand-off applied on each component's own thread. Migrated components (GivTCP, GECloud, Octopus, Ohme) report discovery in their first `run()` instead of auto-configuring, and their `automatic_config` paths accept an `Assignment` (a class — the in-process API components consume) that turns whole-list writes into slot-indexed writes. The coordinator alone writes the fleet-shape keys (`num_inverters`, `inverter_type`, `num_cars`). Serial component startup is unchanged; unmigrated components are untouched.

**Tech Stack:** Python 3 (no new dependencies), existing test harness (`unit_test.py` + `MockBase`), Storage component for persistence.

**Spec:** `docs/superpowers/specs/2026-09-06-discovery-coordinator-design.md`

## Global Constraints

- Work on a new branch `feat/discovery-coordinator` created from `main` (current checkout is on an unrelated fix branch).
- CLAUDE.md mandates: run `impact({target: "<symbol>", direction: "upstream"})` before modifying any existing function/method listed in a task, and `detect_changes()` before every commit; warn on HIGH/CRITICAL.
- Tests: run from `coverage/` (`source setup.csh` first time). ALWAYS redirect output to a log file and grep it, e.g. `./run_all --test coordinator > test_coordinator.log 2>&1; grep -E "PASS|FAIL|Error|Traceback" test_coordinator.log`. Never commit `.log` files.
- 100% docstring coverage (`interrogate`) — every new function, class and method needs a docstring. British English spelling (CSpell, `en-gb`); add genuinely new words to `.cspell/custom-dictionary-workspace.txt` and re-stage after pre-commit sorts it.
- Line length: 256 (Black) / 250 (Flake8). Variable naming: `lower_case_with_underscores`.
- Run `./run_pre_commit` before each commit; `git add` new files first (pre-commit skips untracked files).
- Behaviour parity rule: with a single automatic component enabled, the resulting `self.args` contents must be identical to today's (same keys, same entity ids, same list lengths) — several tasks assert exactly this.
- Catalogue invariant: every report is stored as plain dicts/lists/scalars; `json.dumps(coordinator.catalogue())` must always succeed (Task 1 tests it and later tasks must not break it — no datetimes, sets or objects in reports).

## Discovery report schema (used by every task)

A component reports ONE dict. All sections optional; unknown extra keys in any record are preserved verbatim in the catalogue (that is the point — vendor facts like model/firmware ride along):

```python
{
    "automatic": True,                       # excluded from allocation when False (still catalogued)
    "inverters": [                           # slot candidates, post-topology
        {"device_key": "givtcp:SN1", "inverter_type": "GE", "serial": "SN1", ...},
    ],
    "sensors": [                             # entries for summed list keys
        {"device_key": "givtcp:SN1", "key": "load_today", "entity_id": "sensor.predbat_givtcp_0_load_today"},
    ],
    "cars": [                                # chargers and intelligent devices
        {"device_key": "ohme:CH1", "kind": "charger", "octopus_intelligent": None, ...},
    ],
    "tariff": {"import_code": "...", "export_code": "...", "is_intelligent_go": False, "has_six_hour_cap": False},
}
```

Required keys per record: inverters `device_key`+`inverter_type`; sensors `device_key`+`key`+`entity_id`; cars `device_key`+`kind`. `Coordinator.report()` warns and drops records missing them.

---

### Task 1: Coordinator core — catalogue, validation, inverter slot allocation

**Files:**
- Create: `apps/predbat/coordinator.py`
- Create: `apps/predbat/tests/test_coordinator.py`
- Modify: `apps/predbat/unit_test.py` (TEST_REGISTRY, around line 654 and the import block around line 256)

**Interfaces:**
- Produces: `Assignment` (attributes `inverter_slots: dict`, `num_inverters: int`, `sensor_indices: dict`, `skip_keys: set`, `car_slots: dict`, `num_cars: int`, `octopus_intelligent_owner: bool`, method `to_dict()`), `Coordinator(base, priority)` with `report(component_name, report)`, `allocate() -> dict[str, Assignment]`, `catalogue() -> dict`, `fleet_inverter_types() -> list`, `get_tariff() -> dict|None`, and attribute `persisted_slots: dict[str, int]` (device_key → slot; injected by tests, wired to storage in Task 4).
- Consumes: `MockBase` from `mock_base.py` (existing).

- [ ] **Step 1: Create the branch**

```bash
git checkout main && git pull && git checkout -b feat/discovery-coordinator
```

- [ ] **Step 2: Write the failing tests**

Create `apps/predbat/tests/test_coordinator.py`:

```python
# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery coordinator (coordinator.py) - catalogue collection and slot allocation."""

import json

from mock_base import MockBase
from coordinator import Coordinator


def _coordinator(priority=None, persisted=None):
    """A Coordinator on a MockBase with an explicit component priority order and optional persisted slot map."""
    base = MockBase()
    coordinator = Coordinator(base, priority=priority or ["gecloud", "givtcp"])
    if persisted:
        coordinator.persisted_slots = dict(persisted)
    return base, coordinator


def test_allocate_two_components():
    """Two automatic inverter components get disjoint slots in priority order and the fleet totals are right."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:SN100", "serial": "SN100", "inverter_type": "GE"}]})
    coordinator.report("gecloud", {"inverters": [{"device_key": "gecloud:sn200", "serial": "sn200", "inverter_type": "GEC"}]})
    assignments = coordinator.allocate()
    assert assignments["gecloud"].inverter_slots == {"gecloud:sn200": 0}, assignments["gecloud"].inverter_slots
    assert assignments["givtcp"].inverter_slots == {"givtcp:SN100": 1}, assignments["givtcp"].inverter_slots
    assert assignments["givtcp"].num_inverters == 2
    assert coordinator.fleet_inverter_types() == ["GEC", "GE"]
    print("PASS: two components allocated disjoint slots")
    return 0


def test_allocate_duplicate_serial():
    """The same physical inverter offered by two components is only allocated once - first component in priority order wins."""
    base, coordinator = _coordinator()
    coordinator.report("gecloud", {"inverters": [{"device_key": "gecloud:sn100", "serial": "SN100", "inverter_type": "GEC"}]})
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:SN100", "serial": "sn100", "inverter_type": "GE"}]})
    assignments = coordinator.allocate()
    assert assignments["gecloud"].inverter_slots == {"gecloud:sn100": 0}
    assert assignments["givtcp"].inverter_slots == {}, "duplicate serial must not get a second slot"
    assert assignments["givtcp"].num_inverters == 1
    print("PASS: duplicate serial deduplicated, priority component won")
    return 0


def test_allocate_non_automatic_excluded():
    """Offers from a component whose automatic flag is off do not take part in allocation but stay catalogued."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"automatic": False, "inverters": [{"device_key": "givtcp:SN1", "inverter_type": "GE"}]})
    coordinator.report("gecloud", {"inverters": [{"device_key": "gecloud:sn2", "inverter_type": "GEC"}]})
    assignments = coordinator.allocate()
    assert assignments["givtcp"].inverter_slots == {}
    assert assignments["gecloud"].inverter_slots == {"gecloud:sn2": 0}
    assert assignments["gecloud"].num_inverters == 1
    assert coordinator.catalogue()["components"]["givtcp"]["inverters"], "non-automatic offers still catalogued"
    print("PASS: non-automatic offers excluded from allocation")
    return 0


def test_allocate_sticky_ordering():
    """A persisted slot map keeps devices in their previous relative order; a departed device compacts, a new one appends."""
    base, coordinator = _coordinator(persisted={"givtcp:B": 0, "givtcp:C": 1})
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:A", "inverter_type": "GE"}, {"device_key": "givtcp:C", "inverter_type": "GE"}]})
    assignments = coordinator.allocate()
    # C held slot 1 previously; B is gone so C compacts down to 0 and the new A appends after
    assert assignments["givtcp"].inverter_slots == {"givtcp:C": 0, "givtcp:A": 1}, assignments["givtcp"].inverter_slots
    assert coordinator.persisted_slots == {"givtcp:C": 0, "givtcp:A": 1}
    print("PASS: sticky ordering compacts and appends deterministically")
    return 0


def test_report_is_idempotent():
    """Re-reporting replaces a component's previous report instead of accumulating it."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:A", "inverter_type": "GE"}]})
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:A", "inverter_type": "GE"}, {"device_key": "givtcp:B", "inverter_type": "GE"}]})
    assignments = coordinator.allocate()
    assert assignments["givtcp"].inverter_slots == {"givtcp:A": 0, "givtcp:B": 1}
    assert assignments["givtcp"].num_inverters == 2
    print("PASS: re-report replaces prior report")
    return 0


def test_invalid_records_dropped():
    """A record missing a required key is warned about and dropped without breaking the rest of the report."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"serial": "no-device-key", "inverter_type": "GE"}, {"device_key": "givtcp:OK", "inverter_type": "GE"}]})
    assignments = coordinator.allocate()
    assert assignments["givtcp"].inverter_slots == {"givtcp:OK": 0}
    print("PASS: invalid records dropped, valid ones kept")
    return 0


def test_catalogue_is_json_serialisable():
    """The whole catalogue, including extra vendor keys, round-trips through json.dumps."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:A", "inverter_type": "GE", "firmware": "D0.451", "givtcp_version": "3.0.4"}],
                                  "tariff": {"import_code": "E-1R-AGILE-24-10-01-A", "is_intelligent_go": False}})
    coordinator.allocate()
    catalogue = coordinator.catalogue()
    text = json.dumps(catalogue)
    reloaded = json.loads(text)
    assert reloaded["components"]["givtcp"]["inverters"][0]["firmware"] == "D0.451"
    assert reloaded["allocation"]["inverter_slots"] == {"givtcp:A": 0}
    assert reloaded["allocation"]["fleet_inverter_types"] == ["GE"]
    print("PASS: catalogue serialises to JSON with vendor extras intact")
    return 0


def test_coordinator_all(my_predbat=None):
    """Run every coordinator test, returning the number of failures."""
    failures = 0
    failures += test_allocate_two_components()
    failures += test_allocate_duplicate_serial()
    failures += test_allocate_non_automatic_excluded()
    failures += test_allocate_sticky_ordering()
    failures += test_report_is_idempotent()
    failures += test_invalid_records_dropped()
    failures += test_catalogue_is_json_serialisable()
    return failures
```

- [ ] **Step 3: Register the test and verify it fails**

In `apps/predbat/unit_test.py` add beside the existing imports (~line 263): `from tests.test_coordinator import test_coordinator_all` and in `TEST_REGISTRY` beside the `components` entry: `("coordinator", test_coordinator_all, "Discovery coordinator catalogue and allocation tests", False),`

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -E "PASS|FAIL|Error|ModuleNotFound" test_coordinator.log`
Expected: FAIL — `ModuleNotFoundError: No module named 'coordinator'`

- [ ] **Step 4: Implement coordinator.py**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Central discovery coordinator and catalogue.

Components report what they discovered (inverter slot candidates, summed-sensor entries,
car chargers, the energy tariff) as plain dicts during their first run instead of writing
shared apps.yaml keys directly. The reports form a JSON-serialisable discovery catalogue.
After phase-1 startup the coordinator allocates inverter slots, car indices and sensor
list indices deterministically, and each component's apply_assignment() then writes its
own keys at the assigned indices. Only the fleet-shape keys (num_inverters, inverter_type,
num_cars) are written by the coordinator itself. See
docs/superpowers/specs/2026-09-06-discovery-coordinator-design.md for the report schema.
"""

import copy
import threading

# Required keys per record section; report() warns about and drops records missing one.
REQUIRED_RECORD_KEYS = {
    "inverters": ("device_key", "inverter_type"),
    "sensors": ("device_key", "key", "entity_id"),
    "cars": ("device_key", "kind"),
}


class Assignment:
    """Slot and index allocations handed to one component after discovery.

    Deliberately a class rather than a dict: this is the in-process API migrated
    components consume, so named attributes with defaults beat dict.get chains.
    """

    def __init__(self):
        """Start with an empty allocation; the coordinator fills the fields in allocate()."""
        self.inverter_slots = {}
        self.num_inverters = 0
        self.sensor_indices = {}
        self.skip_keys = set()
        self.car_slots = {}
        self.num_cars = 0
        self.octopus_intelligent_owner = False

    def to_dict(self):
        """A JSON-safe dict of this assignment, for the catalogue and diagnostics."""
        return {
            "inverter_slots": dict(self.inverter_slots),
            "num_inverters": self.num_inverters,
            "sensor_indices": {key: dict(value) for key, value in self.sensor_indices.items()},
            "skip_keys": sorted(self.skip_keys),
            "car_slots": dict(self.car_slots),
            "num_cars": self.num_cars,
            "octopus_intelligent_owner": self.octopus_intelligent_owner,
        }


class Coordinator:
    """Collects component discovery reports into a catalogue and allocates slots deterministically.

    Thread-safe: components report from their own threads while allocate() runs on the
    main thread at startup (and from a reporting component's thread on a later re-report).
    """

    def __init__(self, base, priority):
        """Create an empty coordinator; priority is the component ordering (COMPONENT_LIST key order in production)."""
        self.base = base
        self.log = base.log
        self.priority = list(priority)
        self.lock = threading.Lock()
        self.reports = {}
        self.persisted_slots = {}
        self.started = False
        self._fleet_types = []
        self._last_assignments = {}

    def report(self, component_name, report):
        """Validate and store (or replace) one component's discovery report dict.

        Idempotent per component: a re-report (fleet growth, device-set change) replaces
        the previous report wholesale rather than accumulating. Records missing a required
        key are logged and dropped; the rest of the report still counts.
        """
        cleaned = {"automatic": bool(report.get("automatic", True))}
        for section, required in REQUIRED_RECORD_KEYS.items():
            records = []
            for record in report.get(section, []) or []:
                missing = [key for key in required if not isinstance(record, dict) or record.get(key) is None]
                if missing:
                    self.log("Warn: Coordinator: dropping {} {} record missing {}: {}".format(component_name, section, "/".join(missing), record))
                    continue
                records.append(dict(record))
            cleaned[section] = records
        if isinstance(report.get("tariff"), dict):
            cleaned["tariff"] = dict(report["tariff"])
        with self.lock:
            self.reports[component_name] = cleaned
        self.log("Coordinator: {} reported {} inverter(s), {} sensor(s), {} car(s) (automatic={})".format(component_name, len(cleaned["inverters"]), len(cleaned["sensors"]), len(cleaned["cars"]), cleaned["automatic"]))

    def get_tariff(self):
        """The reported tariff dict, or None when no tariff component has reported."""
        with self.lock:
            for name in self._ordered_reports():
                tariff = self.reports[name].get("tariff")
                if tariff:
                    return dict(tariff)
        return None

    def catalogue(self):
        """The whole discovery catalogue as a JSON-safe dict: every report plus the current allocation."""
        with self.lock:
            return copy.deepcopy(
                {
                    "components": self.reports,
                    "allocation": {
                        "inverter_slots": self.persisted_slots,
                        "fleet_inverter_types": self._fleet_types,
                        "assignments": {name: assignment.to_dict() for name, assignment in self._last_assignments.items()},
                    },
                }
            )

    def _ordered_reports(self):
        """Reporting components in priority order (unknown names last, in report order). Caller holds the lock."""
        known = [name for name in self.priority if name in self.reports]
        unknown = [name for name in self.reports if name not in self.priority]
        return known + unknown

    def fleet_inverter_types(self):
        """The inverter_type list for the whole allocated fleet, ordered by slot (valid after allocate())."""
        return list(self._fleet_types)

    def allocate(self):
        """Allocate inverter slots (and, in later tasks, sensor indices and car indices) and return per-component Assignments.

        Deterministic: components in priority order, records in reported order, duplicate
        serials deduplicated (first claim wins). Stickiness: persisted_slots orders devices
        seen before ahead of new ones, then slots are compacted to 0..N-1 - Inverter cannot
        cope with holes in the per-inverter lists.
        """
        with self.lock:
            assignments = {name: Assignment() for name in self.reports}
            candidates = []
            seen_serials = set()
            for name in self._ordered_reports():
                entry = self.reports[name]
                if not entry["automatic"]:
                    continue
                for record in entry["inverters"]:
                    serial = str(record.get("serial") or record["device_key"]).casefold()
                    if serial in seen_serials:
                        self.log("Coordinator: {} record {} duplicates an already-claimed serial, skipping".format(name, record["device_key"]))
                        continue
                    seen_serials.add(serial)
                    candidates.append((name, record))
            # Devices with a persisted slot keep their previous relative order; new devices follow in record order
            order = sorted(range(len(candidates)), key=lambda i: (self.persisted_slots.get(candidates[i][1]["device_key"], len(self.persisted_slots) + i), i))
            self._fleet_types = [None] * len(candidates)
            new_persisted = {}
            for slot, i in enumerate(order):
                name, record = candidates[i]
                assignments[name].inverter_slots[record["device_key"]] = slot
                self._fleet_types[slot] = record["inverter_type"]
                new_persisted[record["device_key"]] = slot
            if candidates:
                self.persisted_slots = new_persisted
            for assignment in assignments.values():
                assignment.num_inverters = len(candidates)
            self._last_assignments = assignments
            return assignments
```

(Note `get_tariff`/`catalogue` take the lock, so `allocate()` must not call them internally — it reads `self.reports` directly.)

- [ ] **Step 5: Run the tests, verify they pass**

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -cE "^PASS" test_coordinator.log && grep -E "FAIL|Error|Traceback" test_coordinator.log`
Expected: 7 PASS lines, no failures.

- [ ] **Step 6: Pre-commit and commit**

```bash
git add apps/predbat/coordinator.py apps/predbat/tests/test_coordinator.py apps/predbat/unit_test.py docs/superpowers/specs/2026-09-06-discovery-coordinator-design.md docs/superpowers/plans/2026-09-06-discovery-coordinator.md
./run_pre_commit
git commit -m "feat(coordinator): discovery catalogue and deterministic inverter slot allocation"
```

(Per CLAUDE.md run `detect_changes()` first — expected scope: new symbols only.)

---

### Task 2: Sensor index allocation and user-configured skip keys

**Files:**
- Modify: `apps/predbat/coordinator.py`
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Produces: `Assignment.sensor_indices` as `{key: {device_key: index}}`; `Assignment.skip_keys` (summed keys the user configured in apps.yaml — the component must not write them); module constant `SLOT_EXTENDED_KEYS = ("pv_power", "load_power")` whose sensor indices start **after** the inverter fleet (those keys are slot-indexed for control devices, with extra sensor-only entries appended).
- Consumes: `getattr(base, "args_from_apps_yaml", None)` — the user's raw apps.yaml snapshot (`predbat.py:1874`), same source `set_arg_auto` uses.

- [ ] **Step 1: Write the failing tests** (append to `test_coordinator.py`, and call them from `test_coordinator_all`)

```python
def test_sensor_allocation():
    """Summed-key sensor records get indices 0..M-1 across components; slot-extended keys start after the inverter fleet."""
    base, coordinator = _coordinator()
    coordinator.report("gecloud", {"inverters": [{"device_key": "gecloud:sn1", "inverter_type": "GEC"}],
                                   "sensors": [{"device_key": "gecloud:sn1", "key": "pv_today", "entity_id": "sensor.predbat_gecloud_sn1_solar_total"},
                                               {"device_key": "gecloud:pv9", "key": "pv_today", "entity_id": "sensor.predbat_gecloud_pv9_solar_total"},
                                               {"device_key": "gecloud:pv9", "key": "pv_power", "entity_id": "sensor.predbat_gecloud_pv9_solar_power"}]})
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:SN2", "inverter_type": "GE"}],
                                  "sensors": [{"device_key": "givtcp:SN2", "key": "pv_today", "entity_id": "sensor.predbat_givtcp_0_pv_today"}]})
    assignments = coordinator.allocate()
    assert assignments["gecloud"].sensor_indices["pv_today"] == {"gecloud:sn1": 0, "gecloud:pv9": 1}
    assert assignments["givtcp"].sensor_indices["pv_today"] == {"givtcp:SN2": 2}
    # pv_power is slot-indexed for control devices, so the sensor-only extra starts after the 2-inverter fleet
    assert assignments["gecloud"].sensor_indices["pv_power"] == {"gecloud:pv9": 2}
    print("PASS: sensor indices allocated across components")
    return 0


def test_sensor_skip_user_configured():
    """A summed key the user wrote in apps.yaml is skipped entirely - the #4959 user-history-wins rule, centralised."""
    base, coordinator = _coordinator()
    base.args_from_apps_yaml = {"load_today": ["sensor.my_own_meter"]}
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:SN1", "inverter_type": "GE"}],
                                  "sensors": [{"device_key": "givtcp:SN1", "key": "load_today", "entity_id": "sensor.predbat_givtcp_0_load_today"}]})
    assignments = coordinator.allocate()
    assert "load_today" in assignments["givtcp"].skip_keys
    assert "load_today" not in assignments["givtcp"].sensor_indices
    print("PASS: user-configured summed key skipped")
    return 0
```

- [ ] **Step 2: Run and verify the new tests fail**

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -E "FAIL|Error|AssertionError|KeyError" test_coordinator.log`
Expected: FAIL — `sensor_indices` empty / `KeyError: 'pv_today'`.

- [ ] **Step 3: Implement**

In `coordinator.py` add at module level `SLOT_EXTENDED_KEYS = ("pv_power", "load_power")` and extend `allocate()` after the inverter loop (inside the lock, before `self._last_assignments = assignments`):

```python
            # Summed-sensor keys: one shared list per key across all components, deduplicated by
            # owning device, with the user's own apps.yaml entry winning the whole key (#4959).
            raw_args = getattr(self.base, "args_from_apps_yaml", None) or {}
            key_counts = {}
            seen_sensor = set()
            for name in self._ordered_reports():
                entry = self.reports[name]
                if not entry["automatic"]:
                    continue
                for record in entry["sensors"]:
                    if raw_args.get(record["key"]) is not None:
                        assignments[name].skip_keys.add(record["key"])
                        continue
                    dedup = (record["key"], str(record["device_key"]).casefold())
                    if dedup in seen_sensor:
                        continue
                    seen_sensor.add(dedup)
                    start = len(candidates) if record["key"] in SLOT_EXTENDED_KEYS else 0
                    index = key_counts.get(record["key"], start)
                    key_counts[record["key"]] = index + 1
                    assignments[name].sensor_indices.setdefault(record["key"], {})[record["device_key"]] = index
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -cE "^PASS" test_coordinator.log && grep -E "FAIL|Error|Traceback" test_coordinator.log`
Expected: 9 PASS, no failures.

- [ ] **Step 5: Commit**

```bash
./run_pre_commit && git add -u && git commit -m "feat(coordinator): sensor index allocation with user-configured skip keys"
```

---

### Task 3: Car allocation and the Ohme/Octopus intelligent rule

**Files:**
- Modify: `apps/predbat/coordinator.py`
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Produces: `Assignment.car_slots {device_key: car_index}`, `Assignment.num_cars`, `Assignment.octopus_intelligent_owner`.
- Behaviour parity contract (consumed by Tasks 8–9): a charger record always gets car 0 (Ohme registers as a car regardless of tariff, `ohme.py:460-471`); intelligent devices get 0..N-1 sorted by device_key **unless** a charger wins the Octopus Intelligent claim, in which case they get no slots (today's `car_slot_owner` behaviour, `octopus.py:1316-1318`); `num_cars` is never lowered below `get_arg("num_cars", 0)`.
- Tri-state resolution: a charger record's `octopus_intelligent` — explicit `True`/`False` wins; `None` (or absent) means auto = the reported tariff has `is_intelligent_go` truthy.

- [ ] **Step 1: Write the failing tests** (append, and register in `test_coordinator_all`)

```python
def _car_reports():
    """An Ohme charger report plus a two-device Octopus report, for the ownership tests."""
    ohme = {"cars": [{"device_key": "ohme:CH1", "kind": "charger", "octopus_intelligent": None}]}
    octopus = {"cars": [{"device_key": "octopus:dev-b", "kind": "intelligent_device"}, {"device_key": "octopus:dev-a", "kind": "intelligent_device"}]}
    return ohme, octopus


def test_cars_ohme_wins_on_iog():
    """Tri-state unset + Intelligent Go tariff: the charger owns the intelligent slots and the devices get none."""
    base, coordinator = _coordinator(priority=["octopus", "ohme"])
    ohme, octopus = _car_reports()
    octopus["tariff"] = {"import_code": "E-1R-INTELLI-VAR-22-10-14-A", "is_intelligent_go": True}
    coordinator.report("octopus", octopus)
    coordinator.report("ohme", ohme)
    assignments = coordinator.allocate()
    assert assignments["ohme"].car_slots == {"ohme:CH1": 0}
    assert assignments["ohme"].octopus_intelligent_owner is True
    assert assignments["octopus"].car_slots == {}
    assert assignments["ohme"].num_cars == 1
    print("PASS: Ohme owns the intelligent slots on an IOG tariff")
    return 0


def test_cars_octopus_wins_off_iog():
    """Tri-state unset + non-IOG tariff: devices get sorted car indices; the charger still registers as car 0."""
    base, coordinator = _coordinator(priority=["octopus", "ohme"])
    ohme, octopus = _car_reports()
    octopus["tariff"] = {"import_code": "E-1R-AGILE-24-10-01-A", "is_intelligent_go": False}
    coordinator.report("octopus", octopus)
    coordinator.report("ohme", ohme)
    assignments = coordinator.allocate()
    assert assignments["ohme"].octopus_intelligent_owner is False
    assert assignments["ohme"].car_slots == {"ohme:CH1": 0}
    assert assignments["octopus"].car_slots == {"octopus:dev-a": 0, "octopus:dev-b": 1}
    assert assignments["octopus"].num_cars == 2
    print("PASS: Octopus devices allocated sorted off IOG")
    return 0


def test_cars_explicit_tri_state_false():
    """An explicit octopus_intelligent=False always leaves the slots with Octopus, even on IOG."""
    base, coordinator = _coordinator(priority=["octopus", "ohme"])
    coordinator.report("octopus", {"cars": [{"device_key": "octopus:dev-a", "kind": "intelligent_device"}], "tariff": {"is_intelligent_go": True}})
    coordinator.report("ohme", {"cars": [{"device_key": "ohme:CH1", "kind": "charger", "octopus_intelligent": False}]})
    assignments = coordinator.allocate()
    assert assignments["ohme"].octopus_intelligent_owner is False
    assert assignments["octopus"].car_slots == {"octopus:dev-a": 0}
    print("PASS: explicit tri-state False respected")
    return 0


def test_cars_num_cars_never_lowered():
    """num_cars respects a larger user-configured value."""
    base, coordinator = _coordinator()
    base.args["num_cars"] = 3
    coordinator.report("ohme", {"cars": [{"device_key": "ohme:CH1", "kind": "charger"}]})
    assignments = coordinator.allocate()
    assert assignments["ohme"].num_cars == 3
    print("PASS: num_cars never lowered")
    return 0
```

- [ ] **Step 2: Run, verify the four new tests fail**

Run: `cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -E "FAIL|AssertionError" test_coordinator.log`

- [ ] **Step 3: Implement** — extend `allocate()` (inside the lock, after the sensor block, before `self._last_assignments = assignments`). Gather the tariff without calling `get_tariff()` (which takes the lock):

```python
            # Car allocation. Parity with today's overlay semantics: a charger (Ohme) always
            # registers as car 0; intelligent devices take 0..N-1 sorted by device_key so a given
            # device always lands in the same slot - unless a charger wins the Octopus Intelligent
            # claim, in which case the devices get no slots at all (the old car_slot_owner rule).
            tariff = None
            for name in self._ordered_reports():
                if self.reports[name].get("tariff"):
                    tariff = self.reports[name]["tariff"]
                    break
            chargers = []
            intelligent = []
            for name in self._ordered_reports():
                entry = self.reports[name]
                if not entry["automatic"]:
                    continue
                for record in entry["cars"]:
                    (chargers if record["kind"] == "charger" else intelligent).append((name, record))
            charger_owns_intelligent = False
            if chargers:
                name, record = chargers[0]  # one charger component in the minimal design; first in priority order decides
                wish = record.get("octopus_intelligent")
                charger_owns_intelligent = bool(wish) if wish is not None else bool(tariff and tariff.get("is_intelligent_go"))
                if charger_owns_intelligent:
                    self.log("Coordinator: {} charger {} takes the Octopus Intelligent car slots".format(name, record["device_key"]))
            allocated_cars = 0
            if chargers:
                name, record = chargers[0]
                assignments[name].car_slots[record["device_key"]] = 0
                assignments[name].octopus_intelligent_owner = charger_owns_intelligent
                allocated_cars = 1
            if not charger_owns_intelligent and intelligent:
                for index, (name, record) in enumerate(sorted(intelligent, key=lambda pair: pair[1]["device_key"])):
                    assignments[name].car_slots[record["device_key"]] = index
                allocated_cars = max(allocated_cars, len(intelligent))
            num_cars = max(self.base.get_arg("num_cars", 0), allocated_cars)
            for assignment in assignments.values():
                assignment.num_cars = num_cars
```

- [ ] **Step 4: Run tests, verify all pass; commit**

```bash
cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -cE "^PASS" test_coordinator.log
cd .. && ./run_pre_commit && git add -u && git commit -m "feat(coordinator): car allocation with Ohme/Octopus intelligent ownership rule"
```

---

### Task 4: Persistence of the slot map through Storage

**Files:**
- Modify: `apps/predbat/coordinator.py`
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Produces: `Coordinator.load_persisted()` / `Coordinator.save_persisted()` using the Storage component (`storage.load("coordinator", "allocation")` / `storage.save(..., format="json")` via `run_async` from `utils`), called from `allocate_and_apply()` in Task 5. The saved payload is `{"inverter_slots": {...}, "catalogue": <coordinator.catalogue()>}` — the catalogue rides along purely for supportability (it is the JSON artefact a bug report can include); only `inverter_slots` is read back. When `base.components` is None (MockBase harnesses) both are silent no-ops.
- Consumes: `run_async` (same pattern as `PredBat.save_plan`, `predbat.py:730`), storage lookup: `self.base.components.get_component("storage") if self.base.components else None`.

- [ ] **Step 1: Write the failing test** (uses a stub storage, no real files)

```python
class _StubStorage:
    """In-memory stand-in for the Storage component's save/load used by the persistence tests."""

    def __init__(self):
        """Start with nothing saved."""
        self.saved = {}

    async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
        """Record the saved payload keyed by module/filename."""
        self.saved[(module, filename)] = data

    async def load(self, module, filename):
        """Return a previously saved payload, or None."""
        return self.saved.get((module, filename))


class _StubComponents:
    """Just enough of the Components registry for the coordinator to find storage."""

    def __init__(self, storage):
        """Hold the single stubbed component."""
        self._storage = storage

    def get_component(self, name):
        """Return the stub storage for "storage", else None."""
        return self._storage if name == "storage" else None


def test_persistence_round_trip():
    """Allocation saves the slot map (with the catalogue alongside), and a fresh coordinator loads the map back."""
    storage = _StubStorage()
    base, coordinator = _coordinator()
    base.components = _StubComponents(storage)
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:A", "inverter_type": "GE"}, {"device_key": "givtcp:B", "inverter_type": "GE"}]})
    coordinator.allocate()
    coordinator.save_persisted()
    saved = storage.saved[("coordinator", "allocation")]
    assert saved["inverter_slots"] == {"givtcp:A": 0, "givtcp:B": 1}
    assert saved["catalogue"]["components"]["givtcp"]["inverters"], "catalogue snapshot saved for supportability"
    base2, coordinator2 = _coordinator()
    base2.components = _StubComponents(storage)
    coordinator2.load_persisted()
    assert coordinator2.persisted_slots == {"givtcp:A": 0, "givtcp:B": 1}
    print("PASS: allocation map round-trips through storage")
    return 0
```

- [ ] **Step 2: Run, verify it fails** (`AttributeError: ... no attribute 'save_persisted'`)

- [ ] **Step 3: Implement** in `coordinator.py` (import `run_async` from `utils`):

```python
    def _storage(self):
        """The Storage component, or None outside a full Predbat (CLI harnesses, unit tests)."""
        components = getattr(self.base, "components", None)
        return components.get_component("storage") if components else None

    def load_persisted(self):
        """Load the persisted device-to-slot map so slots stay stable across restarts."""
        storage = self._storage()
        if not storage:
            return
        try:
            data = run_async(storage.load("coordinator", "allocation"))
        except Exception as e:
            self.log("Warn: Coordinator: failed to load persisted allocation: {}".format(e))
            return
        if isinstance(data, dict) and isinstance(data.get("inverter_slots"), dict):
            self.persisted_slots = {str(key): int(slot) for key, slot in data["inverter_slots"].items()}
            self.log("Coordinator: restored slot map for {} device(s)".format(len(self.persisted_slots)))

    def save_persisted(self):
        """Save the device-to-slot map, with a catalogue snapshot alongside for supportability."""
        storage = self._storage()
        if not storage:
            return
        try:
            run_async(storage.save("coordinator", "allocation", {"inverter_slots": dict(self.persisted_slots), "catalogue": self.catalogue()}, format="json"))
        except Exception as e:
            self.log("Warn: Coordinator: failed to save allocation: {}".format(e))
```

- [ ] **Step 4: Run tests, verify pass; commit**

```bash
cd coverage && ./run_all --test coordinator > test_coordinator.log 2>&1; grep -cE "^PASS" test_coordinator.log
cd .. && ./run_pre_commit && git add -u && git commit -m "feat(coordinator): persist the slot map and catalogue through the storage component"
```

---

### Task 5: Lifecycle plumbing — ComponentBase hand-off, Components ownership, startup hook

**Files:**
- Modify: `apps/predbat/component_base.py` (`__init__` ~line 56, `start()` loop ~line 207, `set_arg` ~line 97, `set_arg_auto` ~line 103)
- Modify: `apps/predbat/components.py` (`Components.__init__` ~line 748, `initialize()` ~line 803)
- Modify: `apps/predbat/coordinator.py` (`allocate_and_apply`, fleet-key writes, diagnostics entity)
- Modify: `apps/predbat/predbat.py` (`initialize()` between lines 1930 and 1934)
- Modify: `apps/predbat/mock_base.py` (`set_arg` index support, ~line 113)
- Test: `apps/predbat/tests/test_coordinator.py`, `apps/predbat/tests/test_component_base.py`

**Interfaces:**
- Produces:
  - module function `set_arg_auto_base(base, arg, value, overwrite=True)` in `component_base.py` — the existing `set_arg_auto` body with `self.base` → `base` and the final call being `base.set_arg(arg, value)`; `ComponentBase.set_arg_auto` delegates to it; the coordinator imports it for fleet-key writes.
  - `ComponentBase`: `self.component_name` (default `self.__class__.__name__`, overwritten by `Components.initialize()` with the registry key), `self.pending_assignment = None`, `self.assignment_applied = False`, `async def apply_assignment(self, assignment)` (default no-op), `@property coordinator` returning `self.base.components.coordinator` or None, and `set_arg(self, arg, value, index=None)` passing `index` through to `base.set_arg`.
  - `Coordinator.allocate_and_apply(wait=True, timeout=60)` — loads persistence (first call only), allocates, writes `num_inverters`/`inverter_type` (only when at least one automatic inverter record exists) and `num_cars` (only when it grows) via `set_arg_auto_base`, saves persistence, publishes `sensor.predbat_coordinator`, posts each reporting component's Assignment to `component.pending_assignment`, and (when `wait`) polls every 0.1 s until all `assignment_applied` or timeout. Sets `self.started = True`; a later `report()` with `self.started` re-runs `allocate_and_apply(wait=False)` so re-discovery re-applies without deadlocking the reporting thread.
- Consumes: `Assignment`, `Coordinator` from Tasks 1–4; `COMPONENT_LIST` ordering.

- [ ] **Step 1: Impact analysis** on `ComponentBase.start`, `ComponentBase.set_arg`, `ComponentBase.set_arg_auto`, `Components.initialize`, `PredBat.initialize`, and `set_arg` in `userinterface.py` (`impact({target: ..., direction: "upstream"})`); report blast radius before editing. These are wide-fan-in symbols — expect HIGH; the changes are strictly additive (new optional parameter, new attributes, new loop branch), state that when warning.

- [ ] **Step 2: Write the failing tests**

Append to `test_coordinator.py` (register in `test_coordinator_all`):

```python
def test_allocate_and_apply_hand_off():
    """allocate_and_apply posts each component's Assignment, writes the fleet keys, and publishes the catalogue."""
    from component_base import ComponentBase

    class _FakeComponent(ComponentBase):
        """A minimal discovery-aware component that records the assignment it was handed."""

        def initialize(self, **kwargs):
            """Nothing to set up."""
            self.seen = None

        async def run(self, seconds, first):
            """Never called in this test."""
            return True

        async def apply_assignment(self, assignment):
            """Record the assignment for the test to inspect."""
            self.seen = assignment

    base, coordinator = _coordinator(priority=["givtcp"])
    fake = _FakeComponent(base)
    fake.component_name = "givtcp"

    class _Registry:
        """Just enough of Components for allocate_and_apply to find the fake component."""

        def __init__(self):
            """Expose the coordinator the way Components does."""
            self.coordinator = coordinator

        def get_component(self, name):
            """Return the fake for givtcp, None otherwise."""
            return fake if name == "givtcp" else None

    base.components = _Registry()
    coordinator.report("givtcp", {"inverters": [{"device_key": "givtcp:A", "inverter_type": "GE"}]})
    coordinator.allocate_and_apply(wait=False)
    assert fake.pending_assignment is not None
    # Apply on the component's behalf, the way its start() loop would
    from tests.test_infra import run_async
    run_async(fake.apply_assignment(fake.pending_assignment))
    assert fake.seen.inverter_slots == {"givtcp:A": 0}
    assert base.args["num_inverters"] == 1
    assert base.args["inverter_type"] == ["GE"]
    assert base.entities.get("sensor.predbat_coordinator"), "diagnostics entity must be published"
    print("PASS: allocate_and_apply hands off assignments and writes fleet keys")
    return 0
```

Append to `apps/predbat/tests/test_component_base.py` (register in its module runner the same way its existing tests are) a test that the `start()` loop applies a pending assignment: build the module's existing minimal `ComponentBase` subclass whose `run()` returns True, set `component.pending_assignment = Assignment()` before driving `start()` via `tests.test_infra.run_async` with `api_stop` set after ~2 loop iterations (mirror how the module's newest `start()` test bounds the loop), then assert `component.assignment_applied is True` and `component.pending_assignment is None`.

Also extend `MockBase.set_arg` (`mock_base.py:113`) to accept `index=None`, mirroring `userinterface.py:187`: with an index, pad `self.args[key]` with `None` up to the index and set the element (a `None` value with an index sets the element to `None` rather than deleting the key).

- [ ] **Step 3: Run both test modules, verify the new tests fail**

Run: `cd coverage && ./run_all --test coordinator --test component_base > test_task5.log 2>&1; grep -E "FAIL|Error|AttributeError" test_task5.log`

- [ ] **Step 4: Implement**

`component_base.py`:
- Extract `set_arg_auto` body into module function `set_arg_auto_base(base, arg, value, overwrite=True)` (docstring: shared by ComponentBase and the Coordinator); the method delegates: `return set_arg_auto_base(self.base, arg, value, overwrite=overwrite)`.
- `__init__` additions before `self.initialize(**kwargs)`: `self.component_name = self.__class__.__name__`, `self.pending_assignment = None`, `self.assignment_applied = False`.
- `set_arg(self, arg, value, index=None)` → `return self.base.set_arg(arg, value, index=index)`. In `userinterface.py`'s `set_arg`, the `value is None` delete branch runs before the index handling — change it so a `None` value with an index sets the list element to `None` (pad as usual) and only a `None` value **without** an index deletes the key. Run `impact` on it first; grep for callers passing both `None` and `index` (expected: none today).
- New default method:

```python
    async def apply_assignment(self, assignment):
        """Apply a coordinator Assignment. Discovery-aware components override this; the default does nothing."""
        pass
```

- In the `start()` loop, after the `if should_run:` block and before `seconds += 5`:

```python
                # A coordinator assignment is applied here, on the component's own event loop and
                # thread, so vendor code never runs cross-thread. Posted by allocate_and_apply().
                if self.pending_assignment is not None:
                    assignment = self.pending_assignment
                    self.pending_assignment = None
                    try:
                        await self.apply_assignment(assignment)
                    except Exception as e:
                        self.log("Error: {}: apply_assignment failed: {}".format(self.__class__.__name__, e))
                        self.log("Error: " + traceback.format_exc())
                        self.non_fatal_error_occurred()
                    self.assignment_applied = True
```

- Property:

```python
    @property
    def coordinator(self):
        """The discovery coordinator, or None outside a full Predbat (CLI harnesses, unit tests)."""
        components = getattr(self.base, "components", None)
        return getattr(components, "coordinator", None)
```

`components.py`:
- `from coordinator import Coordinator` at the top; in `Components.__init__`: `self.coordinator = Coordinator(base, priority=list(COMPONENT_LIST.keys()))`.
- In `initialize()`, right after successful construction (`self.components[component_name] = component_class(...)`): `self.components[component_name].component_name = component_name`.

`coordinator.py` — `allocate_and_apply` and `publish` (plus `import time` at the top; import `set_arg_auto_base` lazily inside the method — `component_base` must NOT import `coordinator`, and the lazy import avoids the cycle in the other direction):

```python
    def allocate_and_apply(self, wait=True, timeout=60):
        """Allocate slots and hand each reporting component its Assignment.

        Called from PredBat.initialize() after phase-1 startup (wait=True), and again from a
        component's own thread on a post-startup re-report (wait=False - waiting there would
        deadlock, since the reporting component's loop is busy inside run()).
        """
        from component_base import set_arg_auto_base

        if not self.started:
            self.load_persisted()
        assignments = self.allocate()
        fleet_types = self.fleet_inverter_types()
        if fleet_types:
            set_arg_auto_base(self.base, "inverter_type", fleet_types)
            set_arg_auto_base(self.base, "num_inverters", len(fleet_types))
            self.save_persisted()
        num_cars = max((assignment.num_cars for assignment in assignments.values()), default=0)
        if num_cars > self.base.get_arg("num_cars", 0):
            set_arg_auto_base(self.base, "num_cars", num_cars)
        self.publish()
        waiting = []
        for name, assignment in assignments.items():
            components = getattr(self.base, "components", None)
            component = components.get_component(name) if components else None
            if component:
                component.assignment_applied = False
                component.pending_assignment = assignment
                waiting.append(component)
        self.started = True
        if wait:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not all(component.assignment_applied for component in waiting):
                time.sleep(0.1)
            stragglers = [component.component_name for component in waiting if not component.assignment_applied]
            if stragglers:
                self.log("Warn: Coordinator: timed out waiting for {} to apply their assignment".format(", ".join(stragglers)))

    def publish(self):
        """Publish the discovery catalogue as sensor.predbat_coordinator for supportability."""
        catalogue = self.catalogue()
        attributes = {"friendly_name": "Predbat discovery coordinator", "icon": "mdi:sitemap"}
        attributes.update(catalogue["allocation"])
        attributes["components"] = sorted(catalogue["components"].keys())
        self.base.dashboard_item("sensor.{}_coordinator".format(self.base.prefix), state=len(self.persisted_slots), attributes=attributes)
```

And at the end of `report()` (outside the lock): `if self.started: self.allocate_and_apply(wait=False)`.

`predbat.py` — in `initialize()` directly after the phase-1 `start` block (line 1932) and before `initialize(phase=2)`:

```python
            # Discovery barrier: every phase-1 component has now started (or timed out), so
            # allocate inverter/car slots and let discovery-aware components apply them before
            # phase 2 (solar, load_ml) reads the resulting configuration.
            self.components.coordinator.allocate_and_apply()
```

- [ ] **Step 5: Run the three affected suites, verify pass**

Run: `cd coverage && ./run_all --test coordinator --test component_base --test components > test_task5.log 2>&1; grep -cE "^PASS" test_task5.log && grep -E "FAIL|Error|Traceback" test_task5.log`
Expected: all pass (the `components` suite guards the registry import of `coordinator`).

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(coordinator): component assignment hand-off and startup barrier"
```

---

### Task 6: Migrate GivTCP to assigned slots

**Files:**
- Modify: `apps/predbat/givtcp.py` (`initialize` ~line 380, `run()` ~lines 421–489, `automatic_config()` ~line 740)
- Test: `apps/predbat/tests/test_givtcp_component.py`

**Interfaces:**
- Produces: `GivTCPComponent.build_discovery()` returning the report dict (`{"automatic": ..., "inverters": [...], "sensors": [...]}`); `automatic_config(assignment=None)` — `None` keeps today's whole-list behaviour byte-for-byte (CLI harness / coordinator absent); with an assignment, every per-inverter key is written with `self.set_arg(key, entity_id, index=slot)` at `assignment.inverter_slots[device_key]`, the energy-totals keys (`GIVTCP_ENERGY_TODAY_FIELDS`) are written at `assignment.sensor_indices[key][device_key]` and skipped when in `assignment.skip_keys`, `givtcp_rest` is rewritten slot-aligned (URL at the device's slot, `None` padding), and the fleet keys (`num_inverters`, `inverter_type`) are NOT written (coordinator owns them).
- Consumes: `self.coordinator` property, `apply_assignment` hand-off (Task 5).
- Device key: `_device_key(self, n)` = `"givtcp:{}".format(getattr(self.rest[n], "serial_number", None) or self.rest[n].inverter.rest_api)` — serial when GivTCP reports one, REST URL as the stable fallback. The inverter records also carry `serial`, `firmware` and `givtcp_version` (from the values `run()` logs at line 440) as catalogue extras.

- [ ] **Step 1: Impact analysis** on `GivTCPComponent.run` and `GivTCPComponent.automatic_config` (`impact` upstream); report.

- [ ] **Step 2: Write the failing tests** (append to `test_givtcp_component.py`, registered via the module's existing runner `test_givtcp_component`). The fixtures give no serial, so device keys use the URL fallback — assert that rather than guessing how `serial_number` is derived from the blob:

```python
def test_automatic_config_with_assignment_offsets():
    """With a coordinator assignment, GivTCP writes its keys at the assigned slots rather than replacing whole lists."""
    from coordinator import Assignment
    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob()
    _mark_discovered(component)
    component.published_discovery = {0: set(GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS) | set(GIVTCP_AUTO_CONFIG_SCHEDULE_KEYS)}
    device_key = component._device_key(0)
    assert device_key == "givtcp:http://a:6345", device_key
    assignment = Assignment()
    assignment.inverter_slots = {device_key: 1}
    assignment.num_inverters = 2
    assignment.sensor_indices = {key: {device_key: 1} for key in ("load_today", "import_today", "export_today", "pv_today")}
    run_async(component.apply_assignment(assignment))
    assert base.args["soc_kw"][1] == "sensor.predbat_givtcp_0_soc_kw", base.args["soc_kw"]
    assert base.args["soc_kw"][0] is None
    assert base.args["givtcp_rest"] == [None, "http://a:6345"]
    assert base.args["load_today"][1] == "sensor.predbat_givtcp_0_load_today"
    assert "num_inverters" not in base.args, "fleet keys belong to the coordinator"
    print("PASS: GivTCP writes at assigned slots")
    return 0


def test_automatic_config_skip_keys():
    """A summed key in skip_keys is left alone entirely."""
    from coordinator import Assignment
    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob()
    _mark_discovered(component)
    assignment = Assignment()
    assignment.inverter_slots = {component._device_key(0): 0}
    assignment.num_inverters = 1
    assignment.skip_keys = {"load_today"}
    base.args["load_today"] = ["sensor.my_meter"]
    run_async(component.apply_assignment(assignment))
    assert base.args["load_today"] == ["sensor.my_meter"]
    print("PASS: skip keys respected")
    return 0


def test_none_rest_url_tolerated():
    """A None entry in rest_urls (a slot-aligned rewrite read back on restart) is skipped, preserving indices."""
    base, component = _make_component(rest_urls=[None, "http://b:6345"])
    assert component.rest[0] is None
    assert component.rest[1].inverter.rest_api == "http://b:6345"
    print("PASS: None rest_urls entries tolerated")
    return 0


def test_legacy_automatic_config_unchanged():
    """Without an assignment (no coordinator - CLI harness) automatic_config behaves exactly as before."""
    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob()
    _mark_discovered(component)
    run_async(component.automatic_config())
    assert base.args["num_inverters"] == 1
    assert base.args["inverter_type"] == ["GE"]
    assert base.args["soc_kw"] == ["sensor.predbat_givtcp_0_soc_kw"]
    print("PASS: legacy path unchanged")
    return 0
```

(Adjust the exact expected entity ids to what the existing tests in this module assert — copy the strings from the module's current `automatic_config` tests rather than inventing them. `_make_component` builds `None`-URL rest entries only after the Step 4 change; until then `test_none_rest_url_tolerated` fails, which is the point.)

- [ ] **Step 3: Run, verify the new tests fail**

Run: `cd coverage && ./run_all --test givtcp_component > test_givtcp.log 2>&1; grep -E "FAIL|Error|TypeError" test_givtcp.log`

- [ ] **Step 4: Implement**

In `givtcp.py`:
- `initialize` (~line 386): build `self.rest` tolerating `None` URLs — `self.rest.append(GivTCPRest(self.base, InverterRestState(id=n, rest_api=url)) if url else None)`; guard every `self.rest[n]`/iteration in `run()`, `rediscover()`, `publish_data()` and `_parse_entity` handlers with `if rest is None: continue` (grep the file for `self.rest[` and `for ... self.rest` to catch all sites).
- Add `_device_key(self, n)` per the Interfaces block (docstring: stable identity for the coordinator catalogue).
- Add `build_discovery(self)` returning the report dict:

```python
    def build_discovery(self):
        """The discovery report for the coordinator: one inverter record per discovered endpoint plus its energy-total sensors."""
        inverters = []
        sensors = []
        for n in self.discovered:
            rest = self.rest[n]
            inverters.append({"device_key": self._device_key(n), "inverter_type": "GE", "serial": getattr(rest, "serial_number", None), "firmware": getattr(rest, "firmware_version", None), "givtcp_version": getattr(rest, "givtcp_version", None), "rest_api": rest.inverter.rest_api})
            for key in GIVTCP_ENERGY_TODAY_FIELDS:
                sensors.append({"device_key": self._device_key(n), "key": key, "entity_id": self._entity_id("sensor", n, key)})
        return {"automatic": self.automatic, "inverters": inverters, "sensors": sensors}
```

- In `run()` replace the trigger block at lines 479–482: when `self.coordinator` is not None, call `self.coordinator.report(self.component_name, self.build_discovery()); self.configured_for = list(self.discovered)` (the report replaces the direct `automatic_config()` call; `automatic_config_done` is set in `apply_assignment`). When `self.coordinator` is None keep the existing direct call — that is the CLI-harness/legacy path.
- Add:

```python
    async def apply_assignment(self, assignment):
        """Apply the coordinator's slot allocation by running automatic_config against it."""
        await self.automatic_config(assignment=assignment)
        self.automatic_config_done = True
```

- Change `automatic_config(self, assignment=None)`: the capability-gating body (lines 749–813) is unchanged. At the write stage (lines 815–827): when `assignment` is None keep the existing whole-list `set_arg_auto` calls including `inverter_type`/`num_inverters`; when an assignment is present, skip the fleet keys and write per device:

```python
        if assignment is not None:
            for n in discovered:
                slot = assignment.inverter_slots.get(self._device_key(n))
                if slot is None:
                    continue
                self.set_arg("givtcp_rest", self.rest[n].inverter.rest_api, index=slot)
                for key in keys:
                    if key in GIVTCP_ENERGY_TODAY_FIELDS:
                        index = assignment.sensor_indices.get(key, {}).get(self._device_key(n))
                        if key in assignment.skip_keys or index is None:
                            continue
                        self.set_arg(key, self._entity_id("sensor", n, key), index=index)
                        continue
                    domain, _, _ = GIVTCP_CONTROLS.get(key, (None, None, None))
                    domain = domain or "sensor"
                    entity = self._entity_id("sensor", n, "battery_dod_soh") if key == "battery_scaling" else self._entity_id(domain, n, key)
                    self.set_arg(key, entity, index=slot)
            return
```

- [ ] **Step 5: Run the module's full suite (old + new tests), verify pass**

Run: `cd coverage && ./run_all --test givtcp_component --test givtcp_rest > test_givtcp.log 2>&1; grep -cE "^PASS" test_givtcp.log && grep -E "FAIL|Error|Traceback" test_givtcp.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(givtcp): report discovery and auto-configure at coordinator-assigned slots"
```

---

### Task 7: Migrate GECloud to assigned slots

**Files:**
- Modify: `apps/predbat/gecloud.py` (`async_automatic_config` ~line 1131, the `run()` first-pass call ~lines 1690–1694, the `givtcp_rest` deletion ~line 1276)
- Test: `apps/predbat/tests/test_ge_cloud.py`

**Interfaces:**
- Produces: `GECloudDirect.build_discovery(devices)` returning the report dict whose inverter records are the **post-topology** candidates: the same `batteries` list `async_automatic_config` computes today (gateway collapse at lines 1148–1151 included), each `{"device_key": "gecloud:{serial}", "serial": serial, "inverter_type": "GEC"}` (`"GEE"` on the EMS path — reuse the existing detection), with any device facts already held (model, firmware from the devices dict) as catalogue extras; plus sensor records for `load_today`/`import_today`/`export_today`/`pv_today` per battery and, when `ge_cloud_automatic_split_pv` is set, the extra PV devices' `pv_today`/`pv_power`.
- Produces: `async_automatic_config(devices, assignment=None)` — `None` byte-for-byte today; with an assignment, per-device index writes (same transformation as Task 6: `slot = assignment.inverter_slots.get(...)`, `build_entities`-derived entity at `index=slot`, summed keys via `assignment.sensor_indices`/`skip_keys`), fleet keys not written, and the `set_arg("givtcp_rest", None)` deletion **not executed** (under the coordinator GivTCP legitimately owns other slots; keep the deletion on the legacy path).
- Consumes: `apply_assignment` hand-off; `self.devices_dict` (already stored by `run()`).
- Extraction refactor: pull the topology decision (lines 1143–1151) into `_control_candidates(self, devices)` returning `(batteries, num_inverters)` with a docstring, used by both `build_discovery` and `async_automatic_config`, so the record list and the write list cannot diverge.

- [ ] **Step 1: Impact analysis** on `async_automatic_config` and `GECloudDirect.run`; report.

- [ ] **Step 2: Write the failing tests** — follow the arrangement of the existing `_test_async_automatic_config` (`test_ge_cloud.py:283`, using `MockGECloudDirect`): build the same devices/settings fixture it uses, then (a) `run_async(ge_cloud.apply_assignment(assignment))` with `assignment.inverter_slots = {"gecloud:<serial>": 1}`, `num_inverters=2` and assert the wired entity for `soc_percent` lands at index 1 with `None` at index 0, `num_inverters` untouched, and `givtcp_rest` NOT deleted when previously present in `base.args`; (b) assert the legacy call (`run_async(ge_cloud.async_automatic_config(devices))`) still produces exactly the args the existing test asserts (extend, do not weaken, that test); (c) a gateway-collapse case: two batteries plus a gateway and no EMS must produce exactly one inverter record in `build_discovery(devices)["inverters"]` whose serial is the gateway's.

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all --test ge_cloud > test_gecloud.log 2>&1; grep -E "FAIL|Error|TypeError" test_gecloud.log`

- [ ] **Step 4: Implement** per the Interfaces block: extract `_control_candidates`, add `build_discovery`, add `apply_assignment` calling `await self.async_automatic_config(self.devices_dict, assignment=assignment)`, thread `assignment` through the write block (every `self.set_arg(key, [ ... for device in batteries])` becomes, on the assignment path, a per-device `self.set_arg(key, entity, index=slot)` loop; `build_entities` per-device values keep their `None`-means-unsupported meaning — write `None` at the slot via the index-aware `set_arg` so an unsupported control stays unset for that slot), report from `run()`'s first pass (lines 1690–1694) when `self.coordinator` is not None instead of calling `async_automatic_config` directly, and gate the `givtcp_rest` deletion on `assignment is None`.

- [ ] **Step 5: Run the full GECloud suite, verify pass**: `cd coverage && ./run_all --test ge_cloud > test_gecloud.log 2>&1; grep -cE "^PASS" test_gecloud.log && grep -E "FAIL|Error|Traceback" test_gecloud.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(gecloud): report discovery and auto-configure at coordinator-assigned slots"
```

---

### Task 8: Migrate Octopus — car records and the tariff record

**Files:**
- Modify: `apps/predbat/octopus.py` (`automatic_config` ~line 1294, the device-set-change re-run ~lines 675–685, first-run wiring)
- Test: `apps/predbat/tests/test_octopus_intelligent_devices.py` (extend; it already covers `automatic_config` car wiring)

**Interfaces:**
- Produces: `OctopusAPI.build_discovery()` returning the report dict: one `{"device_key": "octopus:{device_id}", "kind": "intelligent_device"}` car record per active (non-suspended) intelligent device (vehicle battery size and charge-point power from the device data as catalogue extras), and `"tariff": {"import_code": <tariffs["import"]["tariffCode"]>, "export_code": ..., "is_intelligent_go": self.is_intelligent_go_tariff(code), "has_six_hour_cap": self.has_six_hour_cap(code)}`.
- Produces: `automatic_config(tariffs, assignment=None)` — the tariff/saving-session/rate-metric writes (lines 1298–1305) are unchanged on both paths. Car block: `None` ⇒ today's behaviour including the `car_slot_owner` check; with an assignment ⇒ for each device with a car slot write `octopus_intelligent_slot`/`octopus_ready_time`/`octopus_charge_limit` at `index=car_n` (`set_arg(key, entity, index=car_n)`), do NOT write `num_cars` (coordinator owns it), and when `assignment.car_slots` is empty log that another component owns the intelligent slots and leave them alone. Track `self.wired_car_indices` (set of indices written last apply) and blank entries that disappeared with `set_arg(key, None, index=stale_index)` so a removed device does not leave stale wiring (the index-aware `None` write from Task 5).
- Produces: on the device-set change detected in `run()` (lines 675–685): when `self.coordinator` is not None, re-`report(...)` (which triggers `allocate_and_apply(wait=False)`) instead of calling `automatic_config` directly.
- Consumes: `apply_assignment` hand-off (`async def apply_assignment(self, assignment)` calls `self.automatic_config(self.automatic_tariffs, assignment=assignment)` — capture the tariffs list used at first wiring on `self`, following how `run()` currently passes it).

- [ ] **Step 1: Impact analysis** on `OctopusAPI.automatic_config` and its `run()` call sites; report.

- [ ] **Step 2: Write failing tests** in `test_octopus_intelligent_devices.py`, mirroring its existing fixtures: (a) assignment with `car_slots = {"octopus:dev-a": 0, "octopus:dev-b": 1}` wires `octopus_intelligent_slot[0]`/`[1]` to the per-device entity names the existing tests assert, and does not touch `num_cars`; (b) empty `car_slots` leaves user-set `octopus_intelligent_slot` untouched; (c) `build_discovery()` on the module's standard two-device fixture returns two car records (suspended devices excluded) and a tariff dict with `is_intelligent_go` matching the fixture tariff; (d) a second apply with one device removed blanks the stale index.

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all -k octopus_ > test_octopus.log 2>&1; grep -E "FAIL|Error|TypeError" test_octopus.log`

- [ ] **Step 4: Implement** per the Interfaces block.

- [ ] **Step 5: Run the whole octopus keyword suite, verify pass**: `cd coverage && ./run_all -k octopus_ > test_octopus.log 2>&1; grep -cE "^PASS" test_octopus.log && grep -E "FAIL|Error|Traceback" test_octopus.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(octopus): report car and tariff discovery, wire cars at assigned indices"
```

---

### Task 9: Migrate Ohme — assigned car index instead of hard-wired car 0

**Files:**
- Modify: `apps/predbat/ohme.py` (`octopus_intelligent_wanted` ~line 434, `automatic_config` ~line 460, `automatic_config_octopus_intelligent` ~line 495, the first-run call site ~lines 255–262)
- Test: `apps/predbat/tests/test_ohme.py`

**Interfaces:**
- Produces: `OhmeAPI.build_discovery()` returning `{"automatic": self.ohme_automatic, "cars": [{"device_key": "ohme:{serial}", "kind": "charger", "octopus_intelligent": self.ohme_automatic_octopus_intelligent}]}` (the raw tri-state, `None` meaning auto — the coordinator resolves it against the tariff record, replacing the `get_component("octopus")` peek for the coordinator path; `octopus_intelligent_wanted()` stays for the legacy path). Charger facts Ohme already holds (serial, model, current vehicle) go in as catalogue extras.
- Produces: `automatic_config(assignment=None)` — `None` ⇒ today's behaviour. With an assignment: `car_n = assignment.car_slots.get(self._device_key())`; when present write `car_charging_planned` and `car_charging_soc` at `index=car_n` (not as one-element lists), keep the existing `car_charging_energy`/`car_charging_power` defensive logic verbatim (global keys, unchanged), do not write `num_cars`; when `assignment.octopus_intelligent_owner` also write the three `octopus_intelligent_slot`/`octopus_ready_time`/`octopus_charge_limit` entities at `index=car_n` and set `self.base.car_slot_owner = "ohme"` (Kraken still reads it — parity).
- Consumes: `apply_assignment` hand-off; first-run call site reports via `self.coordinator.report(self.component_name, self.build_discovery())` when the coordinator exists, else the legacy direct calls.

- [ ] **Step 1: Impact analysis** on `OhmeAPI.automatic_config`, `automatic_config_octopus_intelligent`, `octopus_intelligent_wanted`; report.

- [ ] **Step 2: Write failing tests** in `test_ohme.py` following its existing component fixtures: (a) assignment `car_slots={"ohme:CH1": 1}, octopus_intelligent_owner=True` ⇒ `car_charging_planned[1] == "binary_sensor.predbat_ohme_connected"`, index 0 untouched, `octopus_intelligent_slot[1] == "binary_sensor.predbat_ohme_slot_active"`, `base.car_slot_owner == "ohme"`; (b) `octopus_intelligent_owner=False` ⇒ the three intelligent keys untouched; (c) legacy call with no assignment matches today's asserts (extend the existing automatic-config test rather than replacing it).

- [ ] **Step 3: Run, verify failures**: `cd coverage && ./run_all --test ohme > test_ohme.log 2>&1; grep -E "FAIL|Error|TypeError" test_ohme.log`

- [ ] **Step 4: Implement** per the Interfaces block.

- [ ] **Step 5: Run, verify pass**: `cd coverage && ./run_all --test ohme > test_ohme.log 2>&1; grep -cE "^PASS" test_ohme.log && grep -E "FAIL|Error|Traceback" test_ohme.log`

- [ ] **Step 6: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add -u apps/predbat && git commit -m "feat(ohme): register the charger at its coordinator-assigned car index"
```

---

### Task 10: Headline test — multi-inverter automatic mode across two components

**Files:**
- Create: `apps/predbat/tests/test_discovery_multi_inverter.py`
- Modify: `apps/predbat/unit_test.py` (import + registry entry)

**Interfaces:**
- Consumes: everything above — real `GivTCPComponent` (mocked REST, fixtures from `test_givtcp_component.py`) and `MockGECloudDirect` (from `test_ge_cloud.py`) on one shared `MockBase`, with a real `Coordinator`.

- [ ] **Step 1: Write the test**

```python
# fmt: off
# pylint: disable=line-too-long
"""End-to-end discovery test: GivTCP and GECloud both in automatic mode on one base, allocated disjoint inverter slots by the coordinator - the multi-vendor case that positional auto-config could never do."""

from tests.test_infra import run_async
from mock_base import MockBase
from coordinator import Coordinator


def test_two_vendor_automatic_fleet(my_predbat=None):
    """GivTCP (one REST inverter) + GECloud (one battery) both automatic produce one coherent two-inverter fleet."""
    base = MockBase()
    coordinator = Coordinator(base, priority=["gecloud", "givtcp"])

    class _Registry:
        """Minimal Components stand-in exposing the coordinator and the two components."""

        def __init__(self):
            """Hold the coordinator; components registered after construction."""
            self.coordinator = coordinator
            self.members = {}

        def get_component(self, name):
            """Return a registered component by name."""
            return self.members.get(name)

    registry = _Registry()
    base.components = registry

    # --- GivTCP with one answering REST endpoint (fixtures per test_givtcp_component.py) ---
    from givtcp import GivTCPComponent
    from tests.test_givtcp_component import _rest_data_blob
    givtcp = GivTCPComponent(base, rest_urls=["http://a:6345"], automatic=True)
    givtcp.component_name = "givtcp"
    givtcp.rest[0].inverter.rest_data = _rest_data_blob()
    givtcp.discovered = [0]
    givtcp.discovery_done = True
    registry.members["givtcp"] = givtcp

    # --- GECloud with one battery device (fixtures per test_ge_cloud.py's automatic_config test) ---
    from tests.test_ge_cloud import MockGECloudDirect
    gecloud = MockGECloudDirect()
    gecloud.component_name = "gecloud"
    # ... arrange gecloud.base = base / devices+settings exactly as _test_async_automatic_config does ...
    registry.members["gecloud"] = gecloud

    # Report -> allocate -> apply, the way run()/allocate_and_apply drive it in production
    coordinator.report("givtcp", givtcp.build_discovery())
    coordinator.report("gecloud", gecloud.build_discovery(gecloud.devices_dict))
    coordinator.allocate_and_apply(wait=False)
    run_async(gecloud.apply_assignment(gecloud.pending_assignment))
    run_async(givtcp.apply_assignment(givtcp.pending_assignment))

    assert base.args["num_inverters"] == 2, base.args.get("num_inverters")
    assert base.args["inverter_type"] == ["GEC", "GE"], base.args.get("inverter_type")
    assert base.args["soc_kw"][1] == "sensor.predbat_givtcp_0_soc_kw"
    assert base.args["soc_percent"][0].startswith("sensor.predbat_gecloud_")
    assert base.args["givtcp_rest"] == [None, "http://a:6345"]
    load_today = base.args["load_today"]
    assert any("gecloud" in entity for entity in load_today if entity) and any("givtcp" in entity for entity in load_today if entity), load_today

    # The catalogue for this fleet is one JSON document - the discovery artefact a bug report can carry
    import json
    catalogue = json.loads(json.dumps(coordinator.catalogue()))
    assert set(catalogue["components"].keys()) == {"gecloud", "givtcp"}
    print("PASS: two-vendor automatic fleet allocated coherently")
    return 0
```

(The GECloud arrangement lines are copied from `test_ge_cloud.py`'s `_test_async_automatic_config` — the executor lifts the exact fixture, keeping the serial used in the `soc_percent` assert consistent with it. If `MockGECloudDirect`'s constructor builds its own MockBase, re-point `gecloud.base`, `gecloud.args`, `gecloud.log` at the shared `base` immediately after construction.)

- [ ] **Step 2: Register** in `unit_test.py`: `from tests.test_discovery_multi_inverter import test_two_vendor_automatic_fleet` and `("discovery_multi_inverter", test_two_vendor_automatic_fleet, "Two-vendor automatic multi-inverter allocation (discovery coordinator)", False),`

- [ ] **Step 3: Run, iterate until green**

Run: `cd coverage && ./run_all --test discovery_multi_inverter > test_headline.log 2>&1; grep -E "PASS|FAIL|Error|Traceback" test_headline.log`
Expected: PASS. Any failure here is an integration bug in Tasks 5–7 — fix there, not by weakening the asserts.

- [ ] **Step 4: Full quick suite regression**

Run: `cd coverage && ./run_all --quick > test_full.log 2>&1; grep -E "FAIL|Error:" test_full.log | head -50`
Expected: no new failures versus main (compare against a fresh `main` run if anything looks pre-existing).

- [ ] **Step 5: detect_changes, pre-commit, commit**

```bash
./run_pre_commit && git add apps/predbat/tests/test_discovery_multi_inverter.py && git add -u && git commit -m "test(coordinator): two-vendor automatic multi-inverter headline test"
```

---

### Task 11: Documentation

**Files:**
- Create: `docs/discovery-coordinator.md`
- Modify: `mkdocs.yml` (add the page to the nav, near the components/apps-yaml pages)

- [ ] **Step 1: Write the page** — user-facing first (what changes for a user: two automatic inverter components now coexist; slots are stable across restarts; where to see the allocation — `sensor.predbat_coordinator` and its JSON catalogue; the `*_automatic` keys and manual apps.yaml configuration are unchanged), then a developer section: the report dict schema (copy the schema block from this plan), the offer/report/allocate/apply lifecycle, and how to migrate another component (`build_discovery()` + `apply_assignment()` + report from `run()`, keep the legacy path when `self.coordinator` is None, attach vendor facts as extra record keys). Link the spec for rationale.

- [ ] **Step 2: Verify the docs build**: `mkdocs build > docs_build.log 2>&1; grep -iE "error|warning" docs_build.log`

- [ ] **Step 3: Commit**

```bash
./run_pre_commit && git add docs/discovery-coordinator.md mkdocs.yml && git commit -m "docs: discovery coordinator page"
```

---

## Self-review notes

- Spec §Decisions 1–6 map to: 1 → Tasks 1, 5–9; 2 → Task 5 (barrier location only); 3 → legacy paths kept in Tasks 6–9; 4 → Task 2 skip keys + `set_arg_auto_base` fleet writes; 5 → Tasks 2, 6, 7; 6 → Tasks 6–9 with unmigrated components untouched (verified by the Task 10 full-suite run). The catalogue/JSON requirement → Task 1 (`catalogue()`, JSON test), Task 4 (saved snapshot), Task 5 (`publish()`), Task 10 (end-to-end JSON assert).
- Type consistency: report dicts use the schema block verbatim in Tasks 1–3 and 6–10; `Assignment` field names (`inverter_slots`, `sensor_indices`, `skip_keys`, `car_slots`, `num_cars`, `num_inverters`, `octopus_intelligent_owner`) are used identically throughout; `build_discovery()` returns the full report dict for every migrated component (GECloud's takes `devices`).
- Known deliberate parity gaps (documented in the spec): mixed manual+automatic multi-vendor, 4-inverter ceilings, unmigrated components' whole-list writes, `set_arg_auto(overwrite=False)`'s value-equality nuance replaced by the coarser skip-keys check.
