# Shared MockBase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace eleven near-identical `MockBase` classes in production modules with a single shared `apps/predbat/mock_base.py`, keeping every existing call site working.

**Architecture:** One concrete (not abstract) `MockBase` class provides the full superset of the PredBat base surface that `ComponentBase` dereferences. Five modules import it directly, five subclass it to add module-specific state, and `kraken` aliases it. The spec is `docs/superpowers/specs/2026-07-29-shared-mock-base-design.md`.

**Tech Stack:** Python 3, Predbat's own `unit_test.py` harness (not pytest), Black/Flake8/interrogate/CSpell via pre-commit.

## Global Constraints

- Line length: 256 chars (Black), 250 chars (Flake8).
- **100% docstring coverage** (`interrogate`) — every class and every function needs a docstring, including test functions.
- Variable naming: `lower_case_with_underscores`.
- British English spelling (CSpell, `en-gb`).
- Tests live in `apps/predbat/tests/`, are registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`, and are run from the `coverage/` directory.
- **Test convention:** a test function takes a single `my_predbat` argument and returns `False` on success, `True` on failure. An aggregator `test_<feature>_all(my_predbat)` runs them and returns `True` if any failed.
- **Always save test output to a file, then grep the file.** Never pipe test output straight into grep.
- Every file starts with the standard 9-line Predbat copyright/pylint header (copy it verbatim from `apps/predbat/component_base.py`).

## Working Context

Work happens on branch `refactor/shared-mock-base`, which already exists and holds the committed spec.

First-time environment setup, if the venv is not already present:

```bash
cd coverage
source setup.csh
```

---

### Task 1: Create the shared `mock_base.py` and its tests

**Files:**
- Create: `apps/predbat/mock_base.py`
- Create: `apps/predbat/tests/test_mock_base.py`
- Modify: `apps/predbat/unit_test.py` (add import near line 149, add `TEST_REGISTRY` entry near line 374)

**Interfaces:**
- Consumes: nothing (this is the foundation task).
- Produces: `MockBase` in `apps/predbat/mock_base.py` with constructor
  `MockBase(config_root="./temp_predbat", local_tz=None, **kwargs)` and methods
  `log(message)`,
  `get_state_wrapper(entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False)`,
  `set_state_wrapper(entity_id, state, attributes=None, app=None, required_unit=None)`,
  `dashboard_item(entity_id, state=None, attributes=None, app=None)`,
  `get_arg(arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None)`,
  `set_arg(key, value)`,
  `get_ha_config(name, default)`,
  `get_history_wrapper(entity_id, days=30, required=True, tracked=True)`,
  `call_notify(message)`,
  `record_status(message, debug="", had_errors=False, notify=False, extra="")`.
  Instance attributes set by the constructor: `local_tz`, `now_utc`, `now_utc_exact`,
  `midnight_utc`, `minutes_now`, `prefix`, `entities`, `config_root`,
  `plan_interval_minutes`, `fatal_error`, `had_errors`, `components`, `num_cars`,
  `currency_symbols`, `arg_errors`, `args`.

Note: `mock_base.py` does **not** carry a `# pragma: no cover` marker. The old classes did, but this one is directly unit-tested, so the coverage should be real. The CLI harness functions that *call* it keep their own pragmas.

- [ ] **Step 1: Write the failing test file**

Create `apps/predbat/tests/test_mock_base.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for the shared MockBase used by the standalone command-line harnesses.
"""

from datetime import datetime, timezone

from mock_base import MockBase


def test_mock_base_attribute_superset(my_predbat):
    """Every attribute ComponentBase dereferences off self.base is present after construction."""
    base = MockBase()
    for name in (
        "local_tz",
        "now_utc",
        "now_utc_exact",
        "midnight_utc",
        "minutes_now",
        "prefix",
        "args",
        "entities",
        "config_root",
        "plan_interval_minutes",
        "fatal_error",
        "had_errors",
        "components",
        "num_cars",
        "currency_symbols",
        "arg_errors",
    ):
        assert hasattr(base, name), f"MockBase is missing attribute {name}"
    assert base.prefix == "predbat", "prefix should default to predbat"
    assert base.components is None, "components must be None so ComponentBase.storage resolves to None"
    assert base.fatal_error is False, "fatal_error should start False"
    assert base.had_errors is False, "had_errors should start False"
    assert base.config_root == "./temp_predbat", "config_root should use the documented default"
    print("PASS: MockBase exposes the full base attribute superset")
    return False


def test_mock_base_config_root_and_local_tz_overrides(my_predbat):
    """config_root and local_tz are constructor-overridable, as the axle/gecloud/octopus/solax subclasses need."""
    base = MockBase(config_root="./temp_example")
    assert base.config_root == "./temp_example", "config_root override was ignored"
    utc_base = MockBase(local_tz=timezone.utc)
    assert utc_base.local_tz == timezone.utc, "local_tz override was ignored"
    assert utc_base.now_utc.tzinfo == timezone.utc, "now_utc should use the supplied timezone"
    print("PASS: MockBase honours config_root and local_tz overrides")
    return False


def test_mock_base_midnight_utc_is_aware(my_predbat):
    """midnight_utc is timezone-aware so now_utc - midnight_utc does not raise (fox/kraken/solis had naive values)."""
    base = MockBase()
    assert base.midnight_utc.tzinfo is not None, "midnight_utc must be timezone-aware"
    delta = base.now_utc - base.midnight_utc
    assert delta.total_seconds() >= 0, "now_utc should not precede midnight"
    assert base.midnight_utc.hour == 0, "midnight_utc should be at hour zero"
    assert base.midnight_utc.minute == 0, "midnight_utc should be at minute zero"
    print("PASS: MockBase midnight_utc is timezone-aware")
    return False


def test_mock_base_kwargs_populate_args(my_predbat):
    """Surplus kwargs land in self.args, covering the KrakenMockBase(user_id=...) call site."""
    base = MockBase(user_id="user-123")
    assert base.args.get("user_id") == "user-123", "kwargs should be stored into args"
    print("PASS: MockBase stores surplus kwargs into args")
    return False


def test_mock_base_none_kwargs_are_skipped(my_predbat):
    """A None-valued kwarg is not stored, matching kraken's 'if user_id:' guard and oauth_mixin's args.get(key, '')."""
    base = MockBase(user_id=None)
    assert "user_id" not in base.args, "None-valued kwargs must not be stored"
    assert base.args.get("user_id", "") == "", "absent user_id must fall back to the empty-string default"
    falsy = MockBase(control_enable=False)
    assert falsy.args.get("control_enable") is False, "a legitimate False value must still be stored"
    print("PASS: MockBase skips None kwargs but keeps False")
    return False


def test_mock_base_arg_round_trip(my_predbat):
    """set_arg persists into args and get_arg reads it back; unset keys return the caller's default."""
    base = MockBase()
    assert base.get_arg("missing_key", "fallback") == "fallback", "unset keys should return the default"
    assert base.get_arg("set_read_only", False) is False, "unset boolean should return the supplied default"
    base.set_arg("set_read_only", True)
    assert base.get_arg("set_read_only", False) is True, "set_arg value should be readable via get_arg"
    print("PASS: MockBase get_arg/set_arg round-trip")
    return False


def test_mock_base_dashboard_item_does_not_mutate_attributes(my_predbat):
    """dashboard_item must not corrupt the caller's attributes dict when eliding the options list."""
    base = MockBase()
    attributes = {"options": ["a", "b", "c"], "friendly_name": "Test"}
    base.dashboard_item("select.predbat_test", state="a", attributes=attributes)
    assert attributes["options"] == ["a", "b", "c"], "dashboard_item mutated the caller's options list"
    stored = base.get_state_wrapper("select.predbat_test", raw=True)
    assert stored["attributes"]["options"] == ["a", "b", "c"], "the stored attributes were corrupted"
    assert stored["state"] == "a", "the stored state is wrong"
    print("PASS: MockBase dashboard_item leaves caller attributes intact")
    return False


def test_mock_base_dashboard_item_serialises_datetime(my_predbat):
    """dashboard_item serialises non-JSON-native attribute values instead of raising TypeError."""
    base = MockBase()
    base.dashboard_item("sensor.predbat_test", state="ok", attributes={"last_updated": datetime.now()})
    assert base.get_state_wrapper("sensor.predbat_test") == "ok", "state should still be stored"
    print("PASS: MockBase dashboard_item serialises datetime attributes")
    return False


def test_mock_base_state_wrapper_paths(my_predbat):
    """get_state_wrapper covers the raw, attribute and default-fallback paths."""
    base = MockBase()
    base.set_state_wrapper("sensor.predbat_test", "42", attributes={"unit_of_measurement": "kWh"})
    assert base.get_state_wrapper("sensor.predbat_test") == "42", "plain state lookup failed"
    assert base.get_state_wrapper("sensor.predbat_test", attribute="unit_of_measurement") == "kWh", "attribute lookup failed"
    assert base.get_state_wrapper("sensor.predbat_test", raw=True)["state"] == "42", "raw lookup failed"
    assert base.get_state_wrapper("sensor.predbat_missing", default="none") == "none", "missing entity should return the default"
    assert base.get_state_wrapper("sensor.predbat_test", attribute="absent", default="dflt") == "dflt", "missing attribute should return the default"
    print("PASS: MockBase get_state_wrapper handles raw/attribute/default paths")
    return False


def test_mock_base_set_state_wrapper_accepts_both_kwargs(my_predbat):
    """set_state_wrapper accepts both app= and required_unit=, since the modules disagree on which they pass."""
    base = MockBase()
    base.set_state_wrapper("sensor.predbat_one", "1", attributes={}, app="predbat")
    base.set_state_wrapper("sensor.predbat_two", "2", attributes={}, required_unit="kWh")
    assert base.get_state_wrapper("sensor.predbat_one") == "1", "app= form failed"
    assert base.get_state_wrapper("sensor.predbat_two") == "2", "required_unit= form failed"
    print("PASS: MockBase set_state_wrapper accepts app and required_unit")
    return False


def test_mock_base_record_status_tracks_errors(my_predbat):
    """record_status sets had_errors when told to, so gecloud's guarded call reflects failures."""
    base = MockBase()
    base.record_status("All good")
    assert base.had_errors is False, "a clean status must not set had_errors"
    base.record_status("Something broke", debug="url", had_errors=True)
    assert base.had_errors is True, "had_errors should be set when reported"
    print("PASS: MockBase record_status tracks the error flag")
    return False


def test_mock_base_no_ha_helpers(my_predbat):
    """get_ha_config returns the caller's default and get_history_wrapper returns None, matching a no-HA run."""
    base = MockBase()
    assert base.get_ha_config("anything", "dflt") == "dflt", "get_ha_config should return the default"
    assert base.get_history_wrapper("sensor.predbat_test") is None, "get_history_wrapper should return None with no HA interface"
    print("PASS: MockBase HA helpers degrade cleanly")
    return False


def test_mock_base_all(my_predbat):
    """Run all mock_base tests."""
    tests = [
        ("attribute_superset", test_mock_base_attribute_superset, "Full base attribute superset is present"),
        ("constructor_overrides", test_mock_base_config_root_and_local_tz_overrides, "config_root and local_tz are overridable"),
        ("midnight_aware", test_mock_base_midnight_utc_is_aware, "midnight_utc is timezone-aware"),
        ("kwargs_args", test_mock_base_kwargs_populate_args, "Surplus kwargs populate args"),
        ("none_kwargs", test_mock_base_none_kwargs_are_skipped, "None kwargs are skipped, False is kept"),
        ("arg_round_trip", test_mock_base_arg_round_trip, "get_arg/set_arg round-trip"),
        ("dashboard_no_mutate", test_mock_base_dashboard_item_does_not_mutate_attributes, "dashboard_item does not mutate caller attributes"),
        ("dashboard_datetime", test_mock_base_dashboard_item_serialises_datetime, "dashboard_item serialises datetime attributes"),
        ("state_wrapper", test_mock_base_state_wrapper_paths, "get_state_wrapper raw/attribute/default paths"),
        ("state_wrapper_kwargs", test_mock_base_set_state_wrapper_accepts_both_kwargs, "set_state_wrapper accepts app and required_unit"),
        ("record_status", test_mock_base_record_status_tracks_errors, "record_status tracks had_errors"),
        ("ha_helpers", test_mock_base_no_ha_helpers, "HA helpers degrade cleanly"),
    ]

    failed = []
    for name, test_func, description in tests:
        print(f"\n*** Running: {name} - {description} ***")
        try:
            result = test_func(my_predbat)
            if result:
                failed.append(name)
                print(f"FAILED: {name}")
        except Exception as e:
            failed.append(name)
            print(f"ERROR in {name}: {e}")

    if failed:
        print(f"\n*** {len(failed)} test(s) failed: {', '.join(failed)} ***")
        return True
    else:
        print(f"\n*** All {len(tests)} mock_base tests passed ***")
        return False
```

- [ ] **Step 2: Register the test in `unit_test.py`**

Add the import alongside the other test-module imports (near line 149, next to `from tests.test_component_base import test_component_base_all`):

```python
from tests.test_mock_base import test_mock_base_all
```

Add the registry entry in `TEST_REGISTRY` (near line 374, next to the `component_base` entry):

```python
        ("mock_base", test_mock_base_all, "Shared CLI-harness MockBase tests", False),
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd coverage && ./run_all --test mock_base > /tmp/mock_base_test.txt 2>&1; echo "exit=$?"
grep -iE "ModuleNotFoundError|No module named|Traceback" /tmp/mock_base_test.txt
```

Expected: FAIL with `ModuleNotFoundError: No module named 'mock_base'`.

- [ ] **Step 4: Write the implementation**

Create `apps/predbat/mock_base.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Shared mock base object for standalone command-line runs of components.

Most component modules (fox, solis, octopus, teslemetry, ...) can be executed directly
from the command line to exercise their API against a live vendor endpoint. Those
harnesses need an object to pass as the ``base`` argument of ComponentBase. MockBase
provides the minimal PredBat base surface that ComponentBase and the components read:
a clock, an in-memory entity store, argument accessors and a logger.

It is deliberately concrete rather than abstract - modules instantiate it directly, and
the few needing extra state subclass it. ``components`` is always None, so
``ComponentBase.storage`` resolves to None and the disk cache is skipped for a
standalone run.
"""

from datetime import datetime
import json


class MockBase:
    """Minimal stand-in for the PredBat base object, used by the standalone CLI harnesses."""

    def __init__(self, config_root="./temp_predbat", local_tz=None, **kwargs):
        """Initialise the mock with a clock, empty entity/arg stores and the full base attribute superset.

        Surplus keyword arguments are stored into self.args, with None values skipped so an
        unset optional argument stays absent rather than shadowing a caller's default.
        """
        self.local_tz = local_tz if local_tz is not None else datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.now_utc_exact = self.now_utc
        self.midnight_utc = self.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.prefix = "predbat"
        self.entities = {}
        self.config_root = config_root
        self.plan_interval_minutes = 30
        self.fatal_error = False
        self.had_errors = False
        self.components = None
        self.num_cars = 0
        self.currency_symbols = "£p"
        self.arg_errors = {}
        self.args = {key: value for key, value in kwargs.items() if value is not None}

    def log(self, message):
        """Print a timestamped log line."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Return a stored entity state, one of its attributes, or the whole record when raw is set."""
        entity = self.entities.get(entity_id, {})
        if raw:
            return entity
        if attribute is not None:
            return entity.get("attributes", {}).get(attribute, default)
        return entity.get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None, required_unit=None):
        """Store an entity's state and attributes in memory.

        Accepts both app and required_unit because the component modules disagree on which
        one they pass, and ComponentBase.set_state_wrapper forwards required_unit.
        """
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print a published entity and store it.

        The options list is elided in a copy of the attributes, so the caller's dict - which
        is then stored verbatim - is never mutated.
        """
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            print_attrs = dict(attributes)
            if "options" in print_attrs:
                print_attrs["options"] = "..."
            print(f"  Attributes: {json.dumps(print_attrs, indent=2, default=str)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return a configured argument, falling back to the caller's default."""
        return self.args.get(arg, default)

    def set_arg(self, key, value):
        """Record an argument set by automatic_config, printing it with any referenced entity's state."""
        self.args[key] = value
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for item in value:
                if isinstance(item, str) and "." in item:
                    state = self.get_state_wrapper(item, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")

    def get_ha_config(self, name, default):
        """Return the caller's default - a standalone run has no Home Assistant config."""
        return default

    def get_history_wrapper(self, entity_id, days=30, required=True, tracked=True):
        """Return None - a standalone run has no Home Assistant recorder, matching PredBat's no-interface path."""
        return None

    def call_notify(self, message):
        """Print a notification message."""
        print(f"NOTIFY: {message}")

    def record_status(self, message, debug="", had_errors=False, notify=False, extra=""):
        """Print a status record and track the error flag."""
        print(f"STATUS: {message}" + (f" ({debug})" if debug else ""))
        if had_errors:
            self.had_errors = True
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd coverage && ./run_all --test mock_base > /tmp/mock_base_test.txt 2>&1; echo "exit=$?"
grep -iE "All 12 mock_base tests passed|FAILED|ERROR in" /tmp/mock_base_test.txt
```

Expected: `*** All 12 mock_base tests passed ***`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/mock_base.py apps/predbat/tests/test_mock_base.py apps/predbat/unit_test.py
git commit -m "refactor(mock): add shared MockBase for standalone CLI harnesses

Adds apps/predbat/mock_base.py providing the superset of the PredBat base
surface that ComponentBase dereferences, with unit tests. No module uses it
yet; migrations follow."
```

---

### Task 2: Migrate the five plain re-export modules

**Files:**
- Modify: `apps/predbat/deye.py` (delete lines 1366-1416)
- Modify: `apps/predbat/enphase.py` (delete lines 1470-1534)
- Modify: `apps/predbat/fox.py` (delete lines 2249-2298)
- Modify: `apps/predbat/solis.py` (delete lines 3031-3081)
- Modify: `apps/predbat/teslemetry.py` (delete lines 1239-1291)

**Interfaces:**
- Consumes: `MockBase` from `mock_base` (Task 1).
- Produces: a module-level `MockBase` name in each of these five modules, so existing call sites (`mock_base = MockBase()`) and `from teslemetry import MockBase` in `tests/test_teslemetry.py:1458` keep resolving.

These five need no extra state, so each is a straight import.

**Line numbers drift as you edit.** Locate each class by searching rather than trusting the numbers above:

```bash
grep -n "^class MockBase" apps/predbat/deye.py
```

- [ ] **Step 1: Replace the class in each of the five modules**

In each file, delete the entire `class MockBase:` block (from the `class MockBase:  # pragma: no cover` line through to the line before the next top-level `def`/`async def`/`class`/`if __name__`), and add this import next to the existing `from component_base import ComponentBase` line at the top of the file:

```python
from mock_base import MockBase
```

Apply to: `deye.py`, `enphase.py`, `fox.py`, `solis.py`, `teslemetry.py`.

Do **not** remove any `import json` or `import datetime` from these five — all five use both outside the deleted class. (Only `kraken.py` needs an import removed, handled in Task 3.)

- [ ] **Step 2: Verify each module still imports cleanly**

```bash
cd apps/predbat && for m in deye enphase fox solis teslemetry; do
  python3 -c "import $m; print('$m OK', $m.MockBase)" || echo "$m FAILED"
done
```

Expected: five `OK` lines, each showing `<class 'mock_base.MockBase'>`.

- [ ] **Step 3: Run the affected test suites**

```bash
cd coverage && ./run_all --test mock_base --test teslemetry --test fox_api --test fox_oauth --test solis --test deye_api --test enphase_api > /tmp/task2_test.txt 2>&1; echo "exit=$?"
grep -iE "passed|FAILED|ERROR in|Traceback" /tmp/task2_test.txt | head -40
```

Expected: exit 0, all pass. In particular `test_teslemetry_mock_base_get_arg_consults_args` must still pass — it is the regression check that the `get_arg` persistence unification preserved teslemetry's behaviour.

(These suite names were confirmed against `./run_all --list`.)

- [ ] **Step 4: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/enphase.py apps/predbat/fox.py apps/predbat/solis.py apps/predbat/teslemetry.py
git commit -m "refactor(mock): use shared MockBase in deye, enphase, fox, solis, teslemetry

These five need no module-specific state, so each drops its local copy in
favour of a plain import."
```

---

### Task 3: Migrate the five subclass modules and the kraken alias

**Files:**
- Modify: `apps/predbat/axle.py` (replace class at 709-764)
- Modify: `apps/predbat/gecloud.py` (replace class at 1962-2014; keep `MockHAInterface` at 1952-1960)
- Modify: `apps/predbat/octopus.py` (replace class at 3004-3056; leave `QuietMockBase` untouched)
- Modify: `apps/predbat/sigenergy.py` (replace class at 2500-2547)
- Modify: `apps/predbat/solax.py` (replace class at 2845-2894)
- Modify: `apps/predbat/kraken.py` (replace class at 1404-1461; **delete `import json` at line 19**)
- Modify: `apps/predbat/tests/test_mock_base.py` (add the subclass tests below)

**Interfaces:**
- Consumes: `MockBase` from `mock_base` (Task 1).
- Produces: `MockBase` in `axle`, `gecloud`, `octopus`, `sigenergy`, `solax`; `KrakenMockBase` in `kraken`. `sigenergy.MockBase` keeps its `readonly=False` first positional parameter.

- [ ] **Step 1: Write the failing subclass tests**

Append to `apps/predbat/tests/test_mock_base.py`, immediately **before** `def test_mock_base_all`:

```python
def test_mock_base_module_subclasses(my_predbat):
    """The five subclassing modules keep their distinguishing state on top of the shared base."""
    from axle import MockBase as AxleMockBase
    from gecloud import MockBase as GECloudMockBase
    from octopus import MockBase as OctopusMockBase
    from sigenergy import MockBase as SigenergyMockBase
    from solax import MockBase as SolaxMockBase

    assert AxleMockBase().config_root == "./temp_axle", "axle config_root is wrong"
    assert OctopusMockBase().config_root == "./temp_octopus", "octopus config_root is wrong"

    gecloud_base = GECloudMockBase()
    assert gecloud_base.config_root == "./temp_gecloud", "gecloud config_root is wrong"
    assert gecloud_base.ha_interface is not None, "gecloud must supply a mock ha_interface"
    assert hasattr(gecloud_base.ha_interface, "set_state_external"), "gecloud ha_interface needs set_state_external"

    assert SolaxMockBase().local_tz == timezone.utc, "solax must keep using UTC"

    read_only = SigenergyMockBase(readonly=True)
    assert read_only.get_state_wrapper("switch.predbat_set_read_only") == "on", "sigenergy readonly=True should seed the switch on"
    writable = SigenergyMockBase(readonly=False)
    assert writable.get_state_wrapper("switch.predbat_set_read_only") == "off", "sigenergy readonly=False should seed the switch off"

    print("PASS: module MockBase subclasses keep their distinguishing state")
    return False


def test_mock_base_kraken_alias(my_predbat):
    """KrakenMockBase is the shared base and still accepts user_id, skipping it when None."""
    from kraken import KrakenMockBase

    assert KrakenMockBase(user_id="user-123").args.get("user_id") == "user-123", "user_id should be stored"
    assert "user_id" not in KrakenMockBase(user_id=None).args, "a None user_id must not be stored"
    print("PASS: KrakenMockBase preserves its user_id contract")
    return False
```

Register both in the `tests` list inside `test_mock_base_all`, after the `ha_helpers` entry:

```python
        ("module_subclasses", test_mock_base_module_subclasses, "Module subclasses keep their distinguishing state"),
        ("kraken_alias", test_mock_base_kraken_alias, "KrakenMockBase alias preserves the user_id contract"),
```

- [ ] **Step 2: Run to establish the baseline — these tests must PASS now**

```bash
cd coverage && ./run_all --test mock_base > /tmp/task3_baseline.txt 2>&1; echo "exit=$?"
grep -iE "All 14 mock_base tests passed|FAILED|ERROR in" /tmp/task3_baseline.txt
```

Expected: **PASS**, exit 0.

This is deliberate and is **not** a broken red step. These two are *characterization* tests: they pin down behaviour the five modules already have, so the refactor cannot silently change it. Unlike Task 1 — where the module genuinely did not exist yet — there is no new behaviour to drive out here. A test that passes before *and* after is exactly the safety net a pure refactor needs.

If either test **fails** at this point, stop: the assertions do not match today's behaviour, so they would lock in the wrong contract. Correct the test against the real current behaviour before touching any module.

- [ ] **Step 3: Replace each class with a subclass**

**All five modules need this import** added next to their existing `from component_base import ComponentBase` line. The alias avoids a name clash with the subclass, which must keep the plain name `MockBase` for the existing call sites:

```python
from mock_base import MockBase as SharedMockBase
```

Then replace each deleted class block with the subclass below, at the position the old class occupied.

In `axle.py`:

```python
class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the Axle command-line harness, with its own cache directory."""

    def __init__(self):
        """Initialise the shared mock with the Axle cache root."""
        super().__init__(config_root="./temp_axle")
```

In `octopus.py` (leave the `QuietMockBase(MockBase)` subclass further down the file untouched — it now inherits through the new subclass and keeps working):

```python
class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the Octopus command-line harness, with its own cache directory."""

    def __init__(self):
        """Initialise the shared mock with the Octopus cache root."""
        super().__init__(config_root="./temp_octopus")
```

In `gecloud.py` (keep the existing `MockHAInterface` class exactly as it is, directly above):

```python
class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the GE Cloud command-line harness, with its own cache root and HA interface."""

    def __init__(self):
        """Initialise the shared mock with the GE Cloud cache root and a mock HA interface."""
        super().__init__(config_root="./temp_gecloud")
        self.ha_interface = MockHAInterface()
```

In `solax.py`:

```python
class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the Solax command-line harness, which works in UTC rather than local time."""

    def __init__(self):
        """Initialise the shared mock pinned to UTC."""
        super().__init__(local_tz=timezone.utc)
```

In `sigenergy.py`:

```python
class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the Sigenergy command-line harness, which can pre-seed read-only mode."""

    def __init__(self, readonly=False):
        """Initialise the shared mock, seeding the read-only switch so control writes are gated."""
        super().__init__()
        self.entities["switch.predbat_set_read_only"] = {"state": "on" if readonly else "off", "attributes": {}}
```

In `kraken.py`, delete the `class KrakenMockBase:` block, **delete `import json` on line 19**, and add:

```python
from mock_base import MockBase as KrakenMockBase
```

Note on `sigenergy`: the original seeded `{"state": ...}` with no `attributes` key. The version above adds `"attributes": {}` so the record matches what `set_state_wrapper` produces and an `attribute=` lookup cannot raise `KeyError`. `get_state_wrapper` reads `.get("state")`, so the seeded value is found either way.

Note on `solax`: it already imports `timezone` from `datetime` (line 24), so no import change is needed.

- [ ] **Step 4: Verify `kraken.py` has no other `json` usage before committing the import removal**

```bash
grep -n "json" apps/predbat/kraken.py
```

Expected: only `response.json()` calls, the `json={"query": ...}` kwarg, and the `"application/json"` string — no bare `json.` attribute access, and no `import json`.

- [ ] **Step 5: Verify every module imports cleanly**

```bash
cd apps/predbat && for m in axle gecloud octopus sigenergy solax; do
  python3 -c "import $m; b = $m.MockBase(); print('$m OK', b.config_root, b.local_tz)" || echo "$m FAILED"
done
python3 -c "import kraken; print('kraken OK', kraken.KrakenMockBase)"
```

Expected: six `OK` lines. `sigenergy` prints `./temp_predbat` (it does not override `config_root`); the others print their own roots.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd coverage && ./run_all --test mock_base --test axle --test ge_cloud --test sigenergy --test solax --test kraken --test kraken_auth -k octopus_ > /tmp/task3_test.txt 2>&1; echo "exit=$?"
grep -iE "All 14 mock_base tests passed|FAILED|ERROR in|Traceback" /tmp/task3_test.txt
```

Expected: `*** All 14 mock_base tests passed ***` and exit 0 with no other failures — the same result as the Step 2 baseline, which is the point: the refactor changed the implementation, not the behaviour.

If `-k octopus_` cannot be combined with `--test` in one invocation, run the octopus suites as a second command:

```bash
cd coverage && ./run_all -k octopus_ > /tmp/task3_octopus.txt 2>&1; echo "exit=$?"
grep -iE "FAILED|ERROR in|Traceback" /tmp/task3_octopus.txt
```

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/axle.py apps/predbat/gecloud.py apps/predbat/octopus.py apps/predbat/sigenergy.py apps/predbat/solax.py apps/predbat/kraken.py apps/predbat/tests/test_mock_base.py
git commit -m "refactor(mock): use shared MockBase in the five subclassing modules

axle, gecloud and octopus keep their own cache roots, gecloud its mock HA
interface, solax its UTC clock and sigenergy its readonly seeding. kraken
aliases the shared class and drops the now-unused json import."
```

---

### Task 4: Full-suite and pre-commit verification

**Files:** none modified unless a check fails.

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: a green full test suite and a clean pre-commit run.

- [ ] **Step 1: Confirm no stray `MockBase` definitions remain**

```bash
grep -rn "^class MockBase\|^class KrakenMockBase" apps/predbat/*.py
```

Expected: exactly six lines — `mock_base.py` plus the five subclasses in `axle`, `gecloud`, `octopus`, `sigenergy`, `solax`. `kraken.py` must **not** appear (it is now an import alias).

Note that `web_mcp.py:1293` has a function-local `class MockBase:` — it is indented, so this `^`-anchored grep will not match it. That is correct: it is a different, unrelated mock and is deliberately out of scope.

- [ ] **Step 2: Run the full test suite**

```bash
cd coverage && ./run_all > /tmp/full_test.txt 2>&1; echo "exit=$?"
grep -icE "^FAILED|ERROR in|Traceback" /tmp/full_test.txt
tail -30 /tmp/full_test.txt
```

Expected: exit 0 and no failures. If anything fails, read the surrounding context in `/tmp/full_test.txt` before changing code — do not guess at a fix.

- [ ] **Step 3: Run pre-commit**

```bash
./run_pre_commit > /tmp/precommit.txt 2>&1; echo "exit=$?"
grep -iE "Failed|error|F401|would reformat" /tmp/precommit.txt | head -30
```

Expected: exit 0. Watch specifically for:
- **F401 unused import** — most likely `json` in `kraken.py` if Step 4 of Task 3 was missed.
- **interrogate** docstring coverage — every new class and function needs one.
- **CSpell** — if it flags a word, add it to `.cspell/custom-dictionary-workspace.txt`. That file is auto-sorted on commit, so re-stage it afterwards.
- **Black** reformatting — if it rewrites a file, re-stage and re-run.

- [ ] **Step 4: Commit any pre-commit fixups**

Only if Step 3 changed files:

```bash
git status --short
git add -u
git commit -m "chore: pre-commit fixups for shared MockBase"
```

- [ ] **Step 5: Confirm the diff is the expected shape**

```bash
git diff --stat main...HEAD
```

Expected: roughly 588 lines deleted across the eleven modules, offset by ~120 added in `mock_base.py` plus ~40 in subclasses and the new test file. A substantially different shape means something was missed — investigate before finishing.
