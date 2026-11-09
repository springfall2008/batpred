# Discovery Inverter Record Vocabulary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every inverter discovery record an `entities` map, a `capabilities` dict and Predbat-named `ratings` complete enough to rebuild its `INVERTER_DEF` row, and convert the seven existing reporters to it - without changing anything Predbat does.

**Architecture:** `coordinator.py` gains the new container rules and a pure `inverter_definition()` that rebuilds an `INVERTER_DEF`-shaped dict from a record. A shared test module (`tests/discovery_contract.py`) proves, per reporter, that the record rebuilds the row exactly (completeness) and that it matches what the component's untouched `automatic_config()` binds (agreement). Each reporter then converts in its own task.

**Tech Stack:** Python 3, Predbat's own test harness (`coverage/run_all`), pre-commit (black, flake8, interrogate, cspell en-gb).

**Spec:** `docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md`

## Global Constraints

- **Base branch:** PR #5206 (`feat/discovery-phase1-plan2`) must be merged to `main` first; four of the seven reporters come from it. Branch `feat/discovery-record-vocabulary` from `main` after that merge, and bring in the spec branch (`docs/discovery-inverter-record-vocabulary`) with `git merge`.
- **Observe-only:** no change to any `automatic_config()`, to `inverter.py`, or to anything that writes to an inverter. Only `build_discovery()` and the discovery helpers change. The one `INVERTER_DEF` change is spec D13: GEC and GEE `soc_units` become `"%"` (Task 8) - `inv_soc_units` is never read, so no behaviour changes.
- **Tests:** run from `coverage/` and always save output to a file, then grep it: `./run_all --test <name> > /tmp/<name>.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/<name>.log`. Never pipe `run_all` straight into grep.
- **Every new function and class needs a docstring** (interrogate is 100%). Line length 256 (black) / 250 (flake8). British English in prose and identifiers (cspell `en-gb`); add real new words to `.cspell/custom-dictionary-workspace.txt`.
- **GitNexus:** run `impact({target: "<symbol>", direction: "upstream"})` before editing any existing function, and report HIGH/CRITICAL risk before proceeding. Run `detect_changes()` before each commit.
- **Shared fixture:** tests share one `PredBat`/HA fixture. A test that sets args or attributes on a shared object must restore them (see issue #5079); prefer test-local component instances.
- **Capabilities are literal constants in each component module**, never read back from `INVERTER_DEF` - reading the row would make the completeness test prove nothing.
- **A setting `inverter.py` replaces with a dummy entity is left out of `entities`**: any setting whose presence flag is False in the reporter's row (`PRESENCE_FLAGS` in `coordinator.py`), and `inverter_mode` when both `has_ge_inverter_mode` and `has_ge_eco_toggle` are False.
- **Ratings use Predbat setting names and units:** `inverter_limit` (W), `export_limit` (W, the configured grid export cap - never the lower of it and the inverter rating), `import_limit` (W), `battery_rate_max` (W), `soc_max` (kWh), `battery_min_soc` (%). The old `inverter_w`, `max_charge_w` and `battery_kwh` keys are removed. Descriptive vendor ratings (`battery_capacity_ah`, `battery_pack_count`, `battery_capacity_entries`, `battery_capacity_serials`, `pv_w`) keep their names.
- **Flags:** `soh` and `rest_v3` move from `capabilities` to `flags` (`soh` becomes `reports_soh`).
- **A record describes its own device (spec D10, D11):** it lists what that device has, whatever `automatic_config()`'s fleet-wide gates or the user's opt-out settings (`automatic_ignore_pv`, `givtcp_rest_power_ignore`) would bind. Tests pass those settings as `allowed_extra` to `assert_record_binds_nothing_extra()`.
- **PV-only records (spec D12)** carry `pv_power`/`pv_today` as `access: r` entities, and no `inverter_type`, no `capabilities`.
- **Ratings are figures the device reports (spec D14);** a derived or user-overridden value is an entity only. **Site-wide settings (spec D15)** such as `battery_temperature_history` go on the first driven inverter's record only.
- **Commit messages** end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. **A multi-inverter install.** Every reporter's record is per device; `automatic_config()` binds per-device lists. The agreement test runs on one device, so each reporter task also checks a two-device fixture produces two records whose entity ids differ by device.
2. **A device whose details have not been read yet** (Solis, Fox): the record must not claim entities or capabilities for it. Each owning task pins that the not-yet-read record carries no `entities` and no `capabilities`.
3. **An `entities` descriptor carrying a user-visible string** (`value: "True"`, `format: "%Y-%m-%d %H:%M:%S"`): it must survive redaction unchanged. Task 1 pins that `format` and string `value` pass `Redactor.redact()` untouched.
4. **A legacy list-shaped `capabilities`** from a reporter not yet converted: validation must not crash or drop the whole record. Task 1 pins it; Task 10 removes the legacy path once nothing emits it.
5. **`inverter_definition()` handed the live `INVERTER_DEF` row as `base`**: it must never mutate it. Task 1 pins that the row is unchanged afterwards.

---

### Task 1: Coordinator vocabulary and `inverter_definition()`

**Files:**
- Modify: `apps/predbat/coordinator.py` (constants near the top, the cleaners, `_clean_descriptor`, `CONTAINER_SPEC`, `Redactor._walk`, `_validate_container`, new `inverter_definition`)
- Modify: `apps/predbat/component_base.py` (class constant `WRITE_AND_POLL_SLEEP`)
- Test: `apps/predbat/tests/test_coordinator.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (all in `coordinator.py`):
  - `SCHEMA_VERSION = 2`
  - `CAPABILITY_KEYS` - tuple of the seven behaviour keys
  - `PRESENCE_FLAGS` - dict `flag -> tuple of setting names`
  - `NOT_APPLICABLE_DEFAULTS` - dict of protocol field -> default
  - `inverter_definition(record, write_and_poll_sleep, base=None) -> (dict, list[str], list[str])` returning `(definition, gaps, not_applicable)`
  - `ComponentBase.WRITE_AND_POLL_SLEEP = 2` (class attribute; GivTCP and GE Cloud override to 10 in their tasks)

- [ ] **Step 1: Run impact analysis**

Run `impact` upstream on `_clean_descriptor`, `_validate_container`, `Redactor._walk` and `inverter_record`. Expected: callers are `validate_report`/`_validate_record`/`Coordinator.catalogue` and the reporters; report the risk level before editing.

- [ ] **Step 2: Write the failing tests**

Add to `apps/predbat/tests/test_coordinator.py`. Extend the import line to:

```python
from coordinator import CAPABILITY_KEYS, CONTAINER_SPEC, Coordinator, NOT_APPLICABLE_DEFAULTS, PRESENCE_FLAGS, Redactor, SCHEMA_VERSION, SECTION_SPEC, VOCAB_CONTAINERS, inverter_definition, inverter_record, validate_report
from config import INVERTER_DEF
```

Add these tests (before `test_coordinator_all`):

```python
def _one_inverter(**fields):
    """Validate a single inverter record carrying the given fields and return what survives."""
    record = {"device_id": "test:SN1", "inverter_type": "SunsynkCloud"}
    record.update(fields)
    logs = []
    cleaned = validate_report({"inverters": [record]}, "test", logs.append)
    return (cleaned.get("inverters") or [{}])[0], logs


def test_schema_version_is_two():
    """The document shape changed (dict capabilities, renamed ratings, required access), so the version moved."""
    assert SCHEMA_VERSION == 2
    print("PASS: schema version 2")
    return 0


def test_capabilities_dict_keeps_known_bool_keys():
    """A capabilities dict of the seven behaviour keys survives validation intact."""
    capabilities = {key: True for key in CAPABILITY_KEYS}
    capabilities["can_span_midnight"] = False
    record, _ = _one_inverter(capabilities=capabilities)
    assert record["capabilities"] == capabilities, record
    print("PASS: capabilities dict preserved")
    return 0


def test_capabilities_drops_unknown_key_and_non_bool():
    """An unknown key, or a value that is not a bool, is dropped and logged; the rest survives."""
    record, logs = _one_inverter(capabilities={"support_charge_freeze": True, "has_reserve_soc": True, "can_span_midnight": 1})
    assert record["capabilities"] == {"support_charge_freeze": True}, record
    assert any("has_reserve_soc" in line for line in logs), logs
    assert any("can_span_midnight" in line for line in logs), logs
    print("PASS: capabilities rejects unknown keys and non-bools")
    return 0


def test_capabilities_legacy_token_list_still_accepted():
    """TRANSITIONAL (removed in Task 10): a not-yet-converted reporter's token list is kept, not crashed on."""
    record, _ = _one_inverter(capabilities=["schedule", "target_soc"])
    assert record["capabilities"] == ["schedule", "target_soc"], record
    base, coordinator = _coordinator()
    coordinator.report("test", {"inverters": [{"device_id": "test:SN1", "capabilities": ["schedule"]}]})
    catalogue = coordinator.catalogue()
    assert catalogue["inverters"][0]["capabilities"] == ["schedule"], catalogue
    print("PASS: legacy capability list tolerated")
    return 0


def test_descriptor_requires_access():
    """An entities descriptor without access rw or r is dropped."""
    record, _ = _one_inverter(entities={
        "soc_percent": {"entity_id": "sensor.a", "access": "r"},
        "charge_limit": {"entity_id": "number.b"},
        "reserve": {"entity_id": "number.c", "access": "write"},
    })
    assert set(record["entities"]) == {"soc_percent"}, record
    print("PASS: descriptor needs access")
    return 0


def test_descriptor_needs_exactly_one_of_entity_id_and_value():
    """entity_id and value are alternatives: both, or neither, drops the descriptor."""
    record, _ = _one_inverter(entities={
        "pv_power": {"value": 0, "access": "r"},
        "grid_power": {"entity_id": "sensor.g", "value": 0, "access": "r"},
        "load_power": {"access": "r"},
    })
    assert record["entities"] == {"pv_power": {"value": 0, "access": "r"}}, record
    print("PASS: exactly one of entity_id/value")
    return 0


def test_descriptor_value_rejects_free_text():
    """A string value must pass the info-string guard: no "@", no over-long text."""
    record, _ = _one_inverter(entities={
        "battery_power_invert_note": {"value": "True", "access": "r"},
        "email": {"value": "someone@example.com", "access": "r"},
        "blob": {"value": "x" * 200, "access": "r"},
    })
    assert set(record["entities"]) == {"battery_power_invert_note"}, record
    print("PASS: descriptor value rejects free text")
    return 0


def test_descriptor_invert_must_be_bool():
    """invert is kept when it is a bool and dropped (descriptor kept) when it is not."""
    record, _ = _one_inverter(entities={
        "grid_power": {"entity_id": "sensor.g", "access": "r", "invert": True},
        "battery_power": {"entity_id": "sensor.b", "access": "r", "invert": "yes"},
    })
    assert record["entities"]["grid_power"]["invert"] is True, record
    assert "invert" not in record["entities"]["battery_power"], record
    print("PASS: invert must be bool")
    return 0


def test_new_containers_survive_redaction_unchanged():
    """Bool capabilities, a strftime format and a string value stand-in pass the redactor untouched."""
    base, coordinator = _coordinator()
    coordinator.report("test", {"inverters": [{
        "device_id": "test:SN1",
        "capabilities": {"support_charge_freeze": True},
        "entities": {
            "inverter_time": {"entity_id": "sensor.t", "access": "r", "format": "%Y-%m-%d %H:%M:%S"},
            "pv_power": {"value": "0", "access": "r"},
        },
    }]})
    record = coordinator.catalogue()["inverters"][0]
    assert record["capabilities"] == {"support_charge_freeze": True}, record
    assert record["entities"]["inverter_time"]["format"] == "%Y-%m-%d %H:%M:%S", record
    assert record["entities"]["pv_power"]["value"] == "0", record
    print("PASS: new containers survive redaction")
    return 0


def _full_record():
    """A record carrying every source inverter_definition() reads, shaped like a cloud inverter."""
    return {
        "device_id": "test:SN1",
        "inverter_type": "SunsynkCloud",
        "capabilities": {key: key != "can_span_midnight" for key in CAPABILITY_KEYS},
        "entities": {
            "soc_percent": {"entity_id": "sensor.soc", "access": "r", "unit": "%"},
            "charge_rate": {"entity_id": "number.rate", "access": "rw", "unit": "W"},
            "charge_start_time": {"entity_id": "select.start", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "charge_limit": {"entity_id": "number.limit", "access": "rw"},
            "reserve": {"entity_id": "number.reserve", "access": "rw"},
            "scheduled_charge_enable": {"entity_id": "switch.c", "access": "rw"},
            "scheduled_discharge_enable": {"entity_id": "switch.d", "access": "rw"},
            "schedule_write_button": {"entity_id": "switch.w", "access": "rw"},
        },
    }


def test_inverter_definition_builds_every_field_without_a_base():
    """A full record yields every derived field and no gaps."""
    definition, gaps, not_applicable = inverter_definition(_full_record(), 2)
    assert gaps == [], gaps
    assert not_applicable == ["clock_time_format", "current_dp"], not_applicable
    assert definition["support_charge_freeze"] is True and definition["can_span_midnight"] is False
    assert definition["has_charge_enable_time"] and definition["has_discharge_enable_time"]
    assert definition["has_target_soc"] and definition["has_reserve_soc"]
    assert not definition["has_idle_time"] and not definition["has_timed_pause"]
    assert not definition["has_ge_inverter_mode"] and not definition["has_ge_eco_toggle"] and not definition["has_mqtt_api"]
    assert definition["charge_time_entity_is_option"] is True and definition["charge_time_format"] == "HH:MM:SS"
    assert definition["clock_time_format"] == NOT_APPLICABLE_DEFAULTS["clock_time_format"]
    assert definition["soc_units"] == "%" and definition["output_charge_control"] == "power"
    assert definition["time_button_press"] is True and definition["num_load_entities"] == 1
    assert definition["write_and_poll_sleep"] == 2 and definition["name"] == "SunsynkCloud"
    print("PASS: definition built without a base")
    return 0


def test_inverter_definition_presence_needs_a_real_rw_entity():
    """An r entry, or a value stand-in, does not make a presence flag True."""
    record = _full_record()
    record["entities"]["reserve"] = {"entity_id": "number.reserve", "access": "r"}
    record["entities"]["charge_limit"] = {"value": 100, "access": "rw"}
    record["entities"]["pause_mode"] = {"entity_id": "select.pause", "access": "rw"}
    definition, _, _ = inverter_definition(record, 2)
    assert definition["has_reserve_soc"] is False and definition["has_target_soc"] is False
    assert definition["has_timed_pause"] is True
    print("PASS: presence needs rw entity")
    return 0


def test_inverter_definition_ge_mode_flags_follow_inverter_mode_domain():
    """inverter_mode as a select means a GE inverter-mode select; as a switch, GE Cloud's eco toggle."""
    record = _full_record()
    record["entities"]["inverter_mode"] = {"entity_id": "select.mode", "access": "rw", "domain": "select"}
    definition, _, _ = inverter_definition(record, 2)
    assert definition["has_ge_inverter_mode"] is True and definition["has_ge_eco_toggle"] is False
    record["entities"]["inverter_mode"] = {"entity_id": "switch.eco", "access": "rw", "domain": "switch"}
    definition, _, _ = inverter_definition(record, 2)
    assert definition["has_ge_inverter_mode"] is False and definition["has_ge_eco_toggle"] is True
    print("PASS: GE mode flags from inverter_mode domain")
    return 0


def test_inverter_definition_charge_rate_units():
    """Amps mean current control with decimal places from the step; no charge_rate means none, unless charge_rate_percent is bound."""
    record = _full_record()
    record["entities"]["charge_rate"] = {"entity_id": "number.amps", "access": "rw", "unit": "A", "step": 0.1}
    definition, gaps, not_applicable = inverter_definition(record, 2)
    assert definition["output_charge_control"] == "current" and definition["current_dp"] == 1, definition
    assert "current_dp" not in not_applicable and gaps == []
    record["entities"]["charge_rate"]["step"] = 1
    assert inverter_definition(record, 2)[0]["current_dp"] == 0
    del record["entities"]["charge_rate"]
    definition, _, _ = inverter_definition(record, 2)
    assert definition["output_charge_control"] == "none"
    record["entities"]["charge_rate_percent"] = {"entity_id": "number.pct", "access": "rw", "unit": "%"}
    definition, gaps, _ = inverter_definition(record, 2)
    assert definition["output_charge_control"] == "power" and "output_charge_control" not in gaps, "a percentage-rate model is still power control"
    del record["entities"]["charge_rate_percent"]
    record["entities"]["charge_rate"] = {"entity_id": "number.rate", "access": "rw"}
    _, gaps, _ = inverter_definition(record, 2)
    assert "output_charge_control" in gaps, gaps
    print("PASS: charge_rate unit handling")
    return 0


def test_inverter_definition_gaps_and_not_applicable():
    """A bound entity missing its format is a gap; an unbound source is not applicable; a missing capability is a gap."""
    record = _full_record()
    del record["entities"]["charge_start_time"]["format"]
    del record["capabilities"]["support_feedin_first"]
    record["entities"]["inverter_time"] = {"entity_id": "sensor.t", "access": "r", "format": "%H:%M:%S"}
    definition, gaps, not_applicable = inverter_definition(record, 2)
    assert "charge_time_format" in gaps and "support_feedin_first" in gaps, gaps
    assert "charge_time_format" not in definition and "support_feedin_first" not in definition
    assert definition["clock_time_format"] == "%H:%M:%S" and "clock_time_format" not in not_applicable
    del record["entities"]["charge_start_time"]
    _, gaps, not_applicable = inverter_definition(record, 2)
    assert "charge_time_format" in not_applicable and "charge_time_format" not in gaps
    print("PASS: gaps vs not applicable")
    return 0


def test_inverter_definition_counts_load_entities():
    """num_load_entities is 1 plus the consecutive load_power_N entities bound."""
    record = _full_record()
    record["entities"]["load_power"] = {"entity_id": "sensor.l0", "access": "r"}
    record["entities"]["load_power_1"] = {"entity_id": "sensor.l1", "access": "r"}
    record["entities"]["load_power_3"] = {"entity_id": "sensor.l3", "access": "r"}
    assert inverter_definition(record, 2)[0]["num_load_entities"] == 2
    print("PASS: load entity count")
    return 0


def test_inverter_definition_base_fills_gaps_and_is_never_mutated():
    """With the live INVERTER_DEF row as base, a gap keeps the base value and the row is untouched."""
    row = INVERTER_DEF["SunsynkCloud"]
    before = dict(row)
    record = _full_record()
    record["capabilities"] = {}
    definition, gaps, _ = inverter_definition(record, 2, base=row)
    assert set(CAPABILITY_KEYS) <= set(gaps)
    assert definition["support_charge_freeze"] == row["support_charge_freeze"]
    definition["support_charge_freeze"] = "changed"
    assert INVERTER_DEF["SunsynkCloud"] == before, "inverter_definition() must never mutate INVERTER_DEF"
    print("PASS: base fills gaps, never mutated")
    return 0
```

Register each new test in `test_coordinator_all()` (append after `test_publish_writes_sensor()`):

```python
    failures += test_schema_version_is_two()
    failures += test_capabilities_dict_keeps_known_bool_keys()
    failures += test_capabilities_drops_unknown_key_and_non_bool()
    failures += test_capabilities_legacy_token_list_still_accepted()
    failures += test_descriptor_requires_access()
    failures += test_descriptor_needs_exactly_one_of_entity_id_and_value()
    failures += test_descriptor_value_rejects_free_text()
    failures += test_descriptor_invert_must_be_bool()
    failures += test_new_containers_survive_redaction_unchanged()
    failures += test_inverter_definition_builds_every_field_without_a_base()
    failures += test_inverter_definition_presence_needs_a_real_rw_entity()
    failures += test_inverter_definition_ge_mode_flags_follow_inverter_mode_domain()
    failures += test_inverter_definition_charge_rate_units()
    failures += test_inverter_definition_gaps_and_not_applicable()
    failures += test_inverter_definition_counts_load_entities()
    failures += test_inverter_definition_base_fills_gaps_and_is_never_mutated()
```

Update the existing tests that use the old shapes:
- `test_report_keeps_valid_containers`: change `"ratings": {"battery_kwh": 9.5, "max_charge_w": 3600}` to `"ratings": {"soc_max": 9.5, "battery_rate_max": 3600}` and the assertion to `record["ratings"]["soc_max"] == 9.5`.
- `test_inverter_record_keeps_everything_populated`: change `capabilities=["export_limit"]` to `capabilities={"support_charge_freeze": True}` and its assertion to `record["capabilities"] == {"support_charge_freeze": True}`.
- The empty-container test that passes `capabilities=frozenset()`: keep it (a frozenset is still normalised and omitted) and add `capabilities={}` beside it, asserting the same omission.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test coordinator > /tmp/coordinator.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/coordinator.log | head -20`
Expected: `ImportError: cannot import name 'CAPABILITY_KEYS'`.

- [ ] **Step 4: Implement in `coordinator.py`**

Replace the schema constants at the top:

```python
SCHEMA_VERSION = 2

MAX_STRING = 64
VOCAB_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Containers whose value is a list of vocabulary tokens
VOCAB_CONTAINERS = ("functions", "flags", "effects")

# TRANSITIONAL: capabilities was a token list before schema 2. A reporter not yet converted still sends
# one; it is kept as tokens rather than dropped. Removed once every reporter emits the dict (plan Task 10).
LEGACY_TOKEN_CONTAINERS = ("capabilities",)

# The seven INVERTER_DEF fields that describe behaviour rather than whether an entity exists.
# A record's capabilities dict may hold only these.
CAPABILITY_KEYS = (
    "support_charge_freeze",
    "support_discharge_freeze",
    "support_feedin_first",
    "can_span_midnight",
    "charge_discharge_with_rate",
    "charge_control_immediate",
    "target_soc_used_for_discharge",
)

# INVERTER_DEF presence flags -> the settings that must be bound to a real rw entity for the flag to be
# True. inverter.py creates a dummy entity for exactly these settings when the flag is False
# (inverter.py:612-655), and turns has_timed_pause off at runtime when no pause_mode entity exists.
PRESENCE_FLAGS = {
    "has_charge_enable_time": ("scheduled_charge_enable",),
    "has_discharge_enable_time": ("scheduled_discharge_enable",),
    "has_reserve_soc": ("reserve",),
    "has_target_soc": ("charge_limit",),
    "has_idle_time": ("idle_start_time", "idle_end_time"),
    "has_timed_pause": ("pause_mode",),
}

# The value a protocol field takes when the setting it is read from is not bound at all, so the field
# does not apply to this device. current_dp matches inverter.py's own .get() default.
NOT_APPLICABLE_DEFAULTS = {
    "charge_time_entity_is_option": True,
    "charge_time_format": "HH:MM:SS",
    "clock_time_format": "%Y-%m-%dT%H:%M:%S",
    "current_dp": 1,
}

ACCESS_VALUES = ("rw", "r")
```

Add `from decimal import Decimal, InvalidOperation` to the imports.

Add two cleaners after `_clean_scalar`:

```python
def _clean_bool(value):
    """A True/False fact, or None. Numbers are refused: 1 is not a capability statement."""
    return value if isinstance(value, bool) else None


def _clean_descriptor_value(value):
    """A descriptor's fixed stand-in value: a number, a bool, or a string passing the info-string guard.

    entities is published unredacted in dumps users post to public issues, so a stand-in string is held
    to the same length cap and "@" ban as an info string - free text cannot ride in as a "value".
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return _clean_string(value)
```

Replace `DESCRIPTOR_FIELD_CLEANERS` and `_clean_descriptor`:

```python
# Descriptor field name -> cleaner. access, entity_id and value are required/alternative fields and are
# handled in _clean_descriptor; every field here is optional and dropped rather than disqualifying.
DESCRIPTOR_FIELD_CLEANERS = {
    "domain": _clean_token,
    "unit": _clean_string,
    "device_class": _clean_string,
    "min": _clean_number,
    "max": _clean_number,
    "step": _clean_number,
    "precision": _clean_number,
    "options": _clean_option_list,
    "format": _clean_string,
    "invert": _clean_bool,
}


def _clean_descriptor(value):
    """One entity descriptor: access required, exactly one of entity_id and value, every other field cleaned by its own declared type.

    entities is a "clear" container, republished unredacted into debug dumps users post to public
    GitHub issues, so a field is kept only if it fits its type - free text cannot ride in on unit,
    device_class or options just because the container's name sounds safe.

    access says whether Predbat writes the setting ("rw") or only reads it ("r"); the coordinator will
    configure from it, so a descriptor without one is dropped rather than guessed. entity_id binds the
    setting to an HA entity; value is a fixed stand-in where no entity exists (a PV-less inverter's
    pv_power of 0). Both, or neither, is ambiguous and dropped.
    """
    if not isinstance(value, dict) or value.get("access") not in ACCESS_VALUES:
        return None
    has_entity = isinstance(value.get("entity_id"), str)
    has_value = value.get("value") is not None
    if has_entity == has_value:
        return None
    if has_entity:
        kept = {"entity_id": value["entity_id"], "access": value["access"]}
    else:
        stand_in = _clean_descriptor_value(value["value"])
        if stand_in is None:
            return None
        kept = {"value": stand_in, "access": value["access"]}
    for field, cleaner in DESCRIPTOR_FIELD_CLEANERS.items():
        if field in value and value[field] is not None:
            cleaned = cleaner(value[field])
            if cleaned is not None:
                kept[field] = cleaned
    return kept
```

Add `capabilities` to `CONTAINER_SPEC` and a key restriction beside it:

```python
CONTAINER_SPEC = {
    "hardware_ids": ("clear", _clean_string),
    "account_ids": ("pseudonym", _clean_scalar),
    "info": ("clear", _clean_string),
    "ratings": ("clear", _clean_number),
    "coverage": ("clear", _clean_measure_or_tokens),
    "entities": ("clear", _clean_descriptor),
    "capabilities": ("clear", _clean_bool),
}

# Containers whose keys are a closed set - anything else is dropped and logged
CONTAINER_KEYS = {"capabilities": CAPABILITY_KEYS}
```

In `Redactor._walk`, change the two container branches so a legacy token list under a now-clear container name is still walked as tokens:

```python
                elif key in CLEAR_CONTAINERS and isinstance(value, dict):
                    out[key] = {self._guard_key(key, name): self._guard_value(key, name, entry) for name, entry in value.items()}
                elif key in VOCAB_CONTAINERS or (key in LEGACY_TOKEN_CONTAINERS and isinstance(value, list)):
                    # Vocabulary lists are clear too, and a token is free-form enough (digits
                    # are legal in the pattern) that a misfiled identifier can hide as one.
                    out[key] = [self._guard_scalar(key, "token", entry) for entry in value]
```

In `_validate_container`, handle the legacy list and the key restriction:

```python
    if container_name in VOCAB_CONTAINERS or (container_name in LEGACY_TOKEN_CONTAINERS and isinstance(value, list)):
        if not isinstance(value, list):
            return None
        out = []
        for entry in value:
            token = _clean_token(entry)
            if token is None:
                continue
            if is_secret_key(token):
                log("Warn: Coordinator: {} {} {} token '{}' looks like a credential - refused".format(component_name, section, container_name, token))
                continue
            out.append(token)
        return out
    if not isinstance(value, dict):
        return None
    _, cleaner = CONTAINER_SPEC[container_name]
    allowed = CONTAINER_KEYS.get(container_name)
    out = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            continue
        if is_secret_key(key):
            log("Warn: Coordinator: {} {} field '{}' looks like a credential - refused".format(component_name, section, key))
            continue
        if allowed is not None and key not in allowed:
            log("Warn: Coordinator: {} {}.{} is not a recognised key - dropped".format(component_name, container_name, key))
            continue
        cleaned = cleaner(entry)
        if cleaned is None:
            log("Warn: Coordinator: {} {}.{} value does not fit the container's type - dropped".format(component_name, container_name, key))
            continue
        out[key] = cleaned
    return out
```

`_validate_record` iterates `list(CONTAINER_SPEC) + list(VOCAB_CONTAINERS)`; `capabilities` is now in `CONTAINER_SPEC`, so no change is needed there.

Add `inverter_definition` after `inverter_record`:

```python
def _bound_entity(entities, name, access=None):
    """The descriptor binding `name` to a real HA entity (not a value stand-in), optionally of one access, or None."""
    descriptor = entities.get(name)
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("entity_id"), str):
        return None
    if access is not None and descriptor.get("access") != access:
        return None
    return descriptor


def _decimal_places(step):
    """How many decimal places a numeric step has: 0.1 -> 1, 1 -> 0, 0.05 -> 2. None if it is not a number."""
    try:
        exponent = Decimal(str(step)).normalize().as_tuple().exponent
    except (InvalidOperation, ValueError):
        return None
    return max(0, -exponent) if isinstance(exponent, int) else None


def inverter_definition(record, write_and_poll_sleep, base=None):
    """Rebuild an INVERTER_DEF-shaped definition for one inverter from its discovery record.

    Returns (definition, gaps, not_applicable). definition is always a new dict: base, when given, is
    copied and never written to - inverter.py:381-389 applies apps.yaml's `inverter:` override by
    writing into the shared INVERTER_DEF[type] row, so two inverters of one type end up sharing the last
    one's override, and this must not repeat that.

    Sources, per the vocabulary spec (docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md):
    the seven CAPABILITY_KEYS from record["capabilities"]; the PRESENCE_FLAGS and the two GE mode flags
    from which settings record["entities"] binds to a real rw entity; the protocol fields from those
    entities' descriptors; write_and_poll_sleep from the component. A field it cannot work out - a
    missing capability, or a bound entity lacking the format/unit/domain it needs - is listed in gaps
    and keeps base's value (or is left out without a base). A protocol field whose source setting is not
    bound at all does not apply to this device: it takes NOT_APPLICABLE_DEFAULTS' value (or base's) and
    is listed in not_applicable, not in gaps.
    """
    definition = dict(base) if base else {}
    gaps = []
    not_applicable = []
    capabilities = record.get("capabilities") if isinstance(record.get("capabilities"), dict) else {}
    entities = record.get("entities") if isinstance(record.get("entities"), dict) else {}

    if not definition.get("name"):
        definition["name"] = record.get("inverter_type")

    for key in CAPABILITY_KEYS:
        if isinstance(capabilities.get(key), bool):
            definition[key] = capabilities[key]
        else:
            gaps.append(key)

    for flag, settings in PRESENCE_FLAGS.items():
        definition[flag] = all(_bound_entity(entities, setting, access="rw") is not None for setting in settings)

    mode = _bound_entity(entities, "inverter_mode", access="rw")
    definition["has_ge_inverter_mode"] = mode is not None and mode.get("domain") == "select"
    definition["has_ge_eco_toggle"] = mode is not None and mode.get("domain") == "switch"
    definition.setdefault("has_mqtt_api", False)

    def not_applicable_field(field):
        """Mark a protocol field as not applying to this device and give it its default."""
        not_applicable.append(field)
        definition.setdefault(field, NOT_APPLICABLE_DEFAULTS[field])

    start = _bound_entity(entities, "charge_start_time")
    if start is None:
        not_applicable_field("charge_time_entity_is_option")
        not_applicable_field("charge_time_format")
    else:
        if start.get("domain"):
            definition["charge_time_entity_is_option"] = start["domain"] == "select"
        else:
            gaps.append("charge_time_entity_is_option")
        if start.get("format"):
            definition["charge_time_format"] = start["format"]
        else:
            gaps.append("charge_time_format")

    clock = _bound_entity(entities, "inverter_time")
    if clock is None:
        not_applicable_field("clock_time_format")
    elif clock.get("format"):
        definition["clock_time_format"] = clock["format"]
    else:
        gaps.append("clock_time_format")

    if _bound_entity(entities, "soc_kw") is not None:
        definition["soc_units"] = "kWh"
    elif _bound_entity(entities, "soc_percent") is not None:
        definition["soc_units"] = "%"
    else:
        gaps.append("soc_units")

    rate = _bound_entity(entities, "charge_rate")
    if rate is None and _bound_entity(entities, "charge_rate_percent") is not None:
        # GE Cloud's percentage-rate models: charge_rate is left unbound and charge_rate_percent is
        # written instead, which inverter.py still treats as power control
        definition["output_charge_control"] = "power"
        not_applicable_field("current_dp")
    elif rate is None:
        definition["output_charge_control"] = "none"
        not_applicable_field("current_dp")
    elif rate.get("unit") == "W":
        definition["output_charge_control"] = "power"
        not_applicable_field("current_dp")
    elif rate.get("unit") == "A":
        definition["output_charge_control"] = "current"
        places = _decimal_places(rate.get("step")) if rate.get("step") is not None else None
        if places is None:
            gaps.append("current_dp")
        else:
            definition["current_dp"] = places
    else:
        gaps.append("output_charge_control")
        not_applicable_field("current_dp")

    definition["time_button_press"] = _bound_entity(entities, "schedule_write_button", access="rw") is not None

    count = 1
    while _bound_entity(entities, "load_power_{}".format(count)) is not None:
        count += 1
    definition["num_load_entities"] = count

    definition["write_and_poll_sleep"] = write_and_poll_sleep
    return definition, gaps, not_applicable
```

In `apps/predbat/component_base.py`, add a class attribute to `ComponentBase` (just under the class docstring):

```python
    # Seconds inverter.py waits between writing a setting and reading it back. The component owns the
    # entities, so it owns this timing; INVERTER_DEF's write_and_poll_sleep is 2 for every cloud type and
    # 10 for the GivEnergy types (GivTCP and GE Cloud override it). See coordinator.inverter_definition().
    WRITE_AND_POLL_SLEEP = 2
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test coordinator > /tmp/coordinator.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/coordinator.log | head -20; tail -3 /tmp/coordinator.log`
Expected: no FAIL/Traceback lines.

Then run the reporters' suites, which must still pass unchanged (the legacy list keeps them green):
`./run_all --test web_discovery --test fox_api --test deye_publish --test deye_config --test sunsynk_publish --test sunsynk_config --test alphaess_publish --test alphaess_config --test ge_cloud --test givtcp_component --test solis > /tmp/reporters.log 2>&1; grep -n "FAILED\|Traceback" /tmp/reporters.log | head`. Expected: none (verified on the prototype: all eleven suites pass with Task 1 applied).

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/coordinator.py apps/predbat/component_base.py apps/predbat/tests/test_coordinator.py
git commit -m "feat(discovery): capabilities dict, descriptor access/value/invert and inverter_definition()

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Shared reporter contract checks

**Files:**
- Create: `apps/predbat/tests/discovery_contract.py`
- Create: `apps/predbat/tests/test_discovery_contract.py`
- Modify: `apps/predbat/unit_test.py` (import and `TEST_REGISTRY` entry)

**Interfaces:**
- Consumes: `inverter_definition`, `PRESENCE_FLAGS`, `validate_report` from Task 1; `INVERTER_DEF` from `config.py`.
- Produces (in `tests/discovery_contract.py`), used by every reporter task:
  - `validated_inverters(report) -> list[dict]` - runs `validate_report()`, asserts nothing was dropped, returns the cleaned inverter records.
  - `assert_definition_complete(record, write_and_poll_sleep, except_fields=()) -> None` - completeness: no gaps, and matches the record's `INVERTER_DEF` row on every applicable field except the named ones, each of which must really differ (spec D13's `clock_time_format` on the GE family).
  - `capture_automatic_config(component) -> dict` - runs `component.automatic_config()` (sync or async) with `set_arg`/`set_arg_auto` recorded instead of applied; returns `{setting: value}`.
  - `assert_record_agrees(record, captured, index=0) -> None` - agreement: every setting `automatic_config()` bound for device `index` is in the record.
  - `assert_record_binds_nothing_extra(record, captured, index=0, allowed_extra=()) -> None` - the reverse: the record binds nothing `automatic_config()` did not bind for device `index`, apart from `allowed_extra` (settings a D10/D11 device fact adds on purpose).
  - `dummied_settings(inverter_type) -> set[str]` - settings `inverter.py` replaces with a dummy for that type.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_discovery_contract.py`:

```python
# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the shared discovery contract checks (tests/discovery_contract.py)."""

from coordinator import CAPABILITY_KEYS
from config import INVERTER_DEF
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, dummied_settings, validated_inverters


def _sunsynk_like_record():
    """A record that exactly rebuilds the SunsynkCloud row."""
    row = INVERTER_DEF["SunsynkCloud"]
    return {
        "device_id": "test:SN1",
        "inverter_type": "SunsynkCloud",
        "capabilities": {key: row.get(key, key == "target_soc_used_for_discharge") for key in CAPABILITY_KEYS},
        "entities": {
            "soc_percent": {"entity_id": "sensor.soc", "access": "r", "unit": "%"},
            "charge_rate": {"entity_id": "number.rate", "access": "rw", "unit": "W"},
            "charge_start_time": {"entity_id": "select.start", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "charge_limit": {"entity_id": "number.limit", "access": "rw"},
            "reserve": {"entity_id": "number.reserve", "access": "rw"},
            "scheduled_charge_enable": {"entity_id": "switch.c", "access": "rw"},
            "scheduled_discharge_enable": {"entity_id": "switch.d", "access": "rw"},
            "schedule_write_button": {"entity_id": "switch.w", "access": "rw"},
            "grid_power": {"entity_id": "sensor.grid", "access": "r", "invert": True},
        },
        "ratings": {"inverter_limit": 8000},
    }


class _FakeComponent:
    """Stands in for a component: automatic_config() binds a few settings the way real ones do."""

    def __init__(self, bindings):
        """Remember the (setting, value) pairs automatic_config() will bind."""
        self.bindings = bindings

    def set_arg(self, arg, value):
        """Must never be reached - the capture replaces it."""
        raise AssertionError("set_arg was not captured")

    def set_arg_auto(self, arg, value, overwrite=True):
        """Must never be reached - the capture replaces it."""
        raise AssertionError("set_arg_auto was not captured")

    async def automatic_config(self):
        """Bind every remembered setting, alternating the two setters as real components do."""
        for n, (arg, value) in enumerate(self.bindings):
            (self.set_arg if n % 2 else self.set_arg_auto)(arg, value)


def test_complete_record_passes():
    """A record that rebuilds its row passes the completeness check."""
    assert_definition_complete(_sunsynk_like_record(), 2)
    print("PASS: complete record passes")
    return 0


def test_incomplete_record_fails_with_the_field_named():
    """Dropping a capability, or binding a setting the row dummies, fails and names the field."""
    record = _sunsynk_like_record()
    del record["capabilities"]["support_feedin_first"]
    try:
        assert_definition_complete(record, 2)
    except AssertionError as error:
        assert "support_feedin_first" in str(error), error
    else:
        raise AssertionError("a missing capability must fail completeness")
    record = _sunsynk_like_record()
    record["entities"]["pause_mode"] = {"entity_id": "select.pause", "access": "rw"}
    try:
        assert_definition_complete(record, 2)
    except AssertionError as error:
        assert "has_timed_pause" in str(error), error
    else:
        raise AssertionError("a setting the row dummies must fail completeness")
    print("PASS: incomplete record fails with the field named")
    return 0


def test_capture_records_both_setters_and_restores_them():
    """capture_automatic_config() sees set_arg and set_arg_auto calls and puts the real methods back."""
    component = _FakeComponent([("soc_percent", ["sensor.soc"]), ("num_inverters", 1)])
    captured = capture_automatic_config(component)
    assert captured == {"soc_percent": ["sensor.soc"], "num_inverters": 1}, captured
    assert "set_arg" not in vars(component) and "set_arg_auto" not in vars(component)
    print("PASS: capture sees both setters")
    return 0


def test_agreement_accepts_matching_record_and_skips_exclusions():
    """Entity ids, invert flags, ratings and the out-of-record settings all line up."""
    captured = {
        "inverter_type": ["SunsynkCloud"],
        "num_inverters": 1,
        "soc_percent": ["sensor.soc"],
        "grid_power_invert": [True],
        "inverter_limit": [8000],
        "pause_mode": ["select.never_used"],
        "givtcp_rest": None,
    }
    assert_record_agrees(_sunsynk_like_record(), captured)
    print("PASS: agreement accepts a matching record")
    return 0


def test_agreement_names_each_disagreement():
    """A wrong entity id, a missing invert and a missing rating are each reported."""
    captured = {"soc_percent": ["sensor.other"], "battery_power_invert": ["True"], "export_limit": [3680]}
    try:
        assert_record_agrees(_sunsynk_like_record(), captured)
    except AssertionError as error:
        text = str(error)
        assert "soc_percent" in text and "battery_power_invert" in text and "export_limit" in text, text
    else:
        raise AssertionError("disagreements must fail")
    print("PASS: agreement names each disagreement")
    return 0


def test_dummied_settings_follow_the_row():
    """SolisCloud has no reserve, and no cloud type has a GE mode entity."""
    assert "reserve" in dummied_settings("SolisCloud")
    assert "reserve" not in dummied_settings("SunsynkCloud")
    assert "inverter_mode" in dummied_settings("SunsynkCloud")
    assert "inverter_mode" not in dummied_settings("GE")
    print("PASS: dummied settings follow the row")
    return 0


def test_validated_inverters_refuses_a_dropped_record():
    """A report whose record the validator drops fails loudly instead of testing nothing."""
    assert len(validated_inverters({"inverters": [_sunsynk_like_record()]})) == 1
    try:
        validated_inverters({"inverters": [{"inverter_type": "SunsynkCloud"}]})
    except AssertionError:
        pass
    else:
        raise AssertionError("a dropped record must fail")
    print("PASS: validated_inverters refuses drops")
    return 0


def test_named_exception_is_skipped_and_must_still_differ():
    """except_fields skips a deliberately different field, and fails once that field matches the row again."""
    record = _sunsynk_like_record()
    record["entities"]["inverter_time"] = {"entity_id": "sensor.t", "access": "r", "format": "%Y-%m-%dT%H:%M:%S%z"}
    assert_definition_complete(record, 2, except_fields=("clock_time_format",))
    record["entities"]["inverter_time"]["format"] = INVERTER_DEF["SunsynkCloud"]["clock_time_format"]
    try:
        assert_definition_complete(record, 2, except_fields=("clock_time_format",))
    except AssertionError as error:
        assert "no longer needed" in str(error), error
    else:
        raise AssertionError("a stale exception must fail")
    print("PASS: named exception skipped and kept honest")
    return 0


def test_site_setting_expected_only_on_the_first_record():
    """battery_temperature_history is checked on index 0 and ignored for later devices."""
    record = _sunsynk_like_record()
    captured = {"battery_temperature_history": "sensor.temp_history"}
    try:
        assert_record_agrees(record, captured, index=0)
    except AssertionError as error:
        assert "battery_temperature_history" in str(error), error
    else:
        raise AssertionError("the first record must carry the site setting")
    assert_record_agrees(record, captured, index=1)
    print("PASS: site setting on the first record only")
    return 0


def test_upper_case_serial_entity_id_is_an_entity_id():
    """An entity id built from an upper-case serial is still compared as an entity id, not a literal."""
    record = _sunsynk_like_record()
    record["entities"]["soc_percent"]["entity_id"] = "sensor.predbat_gecloud_SA2243G277_soc"
    assert_record_agrees(record, {"soc_percent": ["sensor.predbat_gecloud_SA2243G277_soc"]})
    print("PASS: upper-case serial entity id")
    return 0


def test_reverse_check_names_extra_settings_and_honours_allowed_extra():
    """A record entity automatic_config() did not bind fails, unless it is named in allowed_extra."""
    record = _sunsynk_like_record()
    captured = {name: ["x.y"] for name in record["entities"] if name != "grid_power"}
    try:
        assert_record_binds_nothing_extra(record, captured)
    except AssertionError as error:
        assert "grid_power" in str(error), error
    else:
        raise AssertionError("an unbound record entity must fail")
    assert_record_binds_nothing_extra(record, captured, allowed_extra=("grid_power",))
    print("PASS: reverse check")
    return 0


def run_discovery_contract_tests(my_predbat=None):
    """Run every discovery contract test, returning the number of failures."""
    failures = 0
    failures += test_complete_record_passes()
    failures += test_incomplete_record_fails_with_the_field_named()
    failures += test_capture_records_both_setters_and_restores_them()
    failures += test_agreement_accepts_matching_record_and_skips_exclusions()
    failures += test_agreement_names_each_disagreement()
    failures += test_dummied_settings_follow_the_row()
    failures += test_validated_inverters_refuses_a_dropped_record()
    failures += test_named_exception_is_skipped_and_must_still_differ()
    failures += test_site_setting_expected_only_on_the_first_record()
    failures += test_upper_case_serial_entity_id_is_an_entity_id()
    failures += test_reverse_check_names_extra_settings_and_honours_allowed_extra()
    return failures
```

Register it in `apps/predbat/unit_test.py` next to the coordinator entry:

```python
from tests.test_discovery_contract import run_discovery_contract_tests
```

```python
        ("discovery_contract", run_discovery_contract_tests, "Discovery reporter contract checks", False),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test discovery_contract > /tmp/contract.log 2>&1; grep -n "Error\|Traceback" /tmp/contract.log | head`
Expected: `ModuleNotFoundError: No module named 'tests.discovery_contract'`.

- [ ] **Step 3: Implement `apps/predbat/tests/discovery_contract.py`**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Contract checks every inverter reporter's tests run against its discovery record.

Completeness: the record alone rebuilds the reporter's INVERTER_DEF row (no row as a base - with one, a
field the record left out would silently inherit the right answer and prove nothing). Agreement: every
setting the component's automatic_config() binds is in the record, so the coordinator can later take
automatic configuration over from it. See docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md.
"""

import asyncio
import inspect
import re

from config import INVERTER_DEF
from coordinator import PRESENCE_FLAGS, inverter_definition, validate_report

# inverter.py's own defaults for the fields some rows leave out (inverter.py:400-432)
ROW_DEFAULTS = {
    "current_dp": 1,
    "support_feedin_first": False,
    "has_ge_eco_toggle": False,
    "charge_discharge_with_rate": False,
    "target_soc_used_for_discharge": True,
}

# Row fields the record is not expected to rebuild: the display name, and two fields nothing reads
NOT_COMPARED = ("name", "has_time_window", "has_rest_api")

# Settings automatic_config() sets that are not facts about this inverter (spec section 1.5)
SETTINGS_OUTSIDE_THE_RECORD = frozenset(
    {
        "inverter_type",
        "num_inverters",
        "givtcp_rest",
        "ge_cloud_data",
        "ge_cloud_direct",
        "ge_cloud_serial",
        "num_cars",
        "car_charging_energy",
        "car_charging_planned",
        "car_charging_power",
    }
)

# A *_invert setting describes the sign of the entity it names
INVERT_SETTINGS = {"grid_power_invert": "grid_power", "battery_power_invert": "battery_power", "load_power_invert": "load_power"}

# Bound once for the whole site, from the first driven inverter (spec D15) - expected on that record only
SITE_SETTINGS = frozenset({"battery_temperature_history"})

# GE Cloud builds entity ids from the raw, upper-case serial, so the object id may hold capitals
ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[A-Za-z0-9_]+$")


def row_value(inverter_type, field):
    """The value inverter.py would use for one field of an INVERTER_DEF row."""
    row = INVERTER_DEF[inverter_type]
    return row[field] if field in row else ROW_DEFAULTS[field]


def dummied_settings(inverter_type):
    """The settings inverter.py replaces with a dummy entity for this inverter type, so a record leaves them out."""
    row = INVERTER_DEF[inverter_type]
    settings = {setting for flag, names in PRESENCE_FLAGS.items() if not row.get(flag, False) for setting in names}
    if not row.get("has_ge_inverter_mode", False) and not row.get("has_ge_eco_toggle", False):
        settings.add("inverter_mode")
    return settings


def validated_inverters(report):
    """Run a report through validate_report() and return its inverter records, failing if any record was dropped."""
    logs = []
    cleaned = validate_report(report, "contract", logs.append)
    raw = report.get("inverters") or []
    records = cleaned.get("inverters") or []
    assert len(records) == len(raw), "validate_report() dropped {} of {} inverter records: {}".format(len(raw) - len(records), len(raw), logs)
    return records


def assert_definition_complete(record, write_and_poll_sleep, except_fields=()):
    """Fail unless the record alone rebuilds its INVERTER_DEF row: no gaps, and every applicable field equal.

    except_fields names row fields the record deliberately describes differently (spec D13: the GE family's
    clock_time_format, which the record gives as the ISO format its sensor really publishes). Each must still
    differ - an exception that no longer applies fails, so it is removed once the row is fixed.
    """
    inverter_type = record["inverter_type"]
    definition, gaps, not_applicable = inverter_definition(record, write_and_poll_sleep)
    assert not gaps, "{} record cannot rebuild these INVERTER_DEF fields: {}".format(inverter_type, gaps)
    stale = [field for field in except_fields if definition.get(field) == row_value(inverter_type, field)]
    assert not stale, "{} exceptions no longer needed - the record now matches the row: {}".format(inverter_type, stale)
    fields = (set(INVERTER_DEF[inverter_type]) | set(ROW_DEFAULTS)) - set(NOT_COMPARED) - set(not_applicable) - set(except_fields)
    mismatches = {field: {"record": definition.get(field), "row": row_value(inverter_type, field)} for field in sorted(fields) if definition.get(field) != row_value(inverter_type, field)}
    assert not mismatches, "{} record disagrees with its INVERTER_DEF row: {}".format(inverter_type, mismatches)


def capture_automatic_config(component):
    """Run component.automatic_config() with set_arg/set_arg_auto recorded instead of applied; return {setting: value}.

    The two setters are replaced on the instance only and removed afterwards, so the class methods are
    back in place whatever automatic_config() raised.
    """
    captured = {}

    def record(arg, value, *args, **kwargs):
        """Remember the last value bound to each setting."""
        captured[arg] = value

    component.set_arg = record
    component.set_arg_auto = record
    try:
        result = component.automatic_config()
        if inspect.isawaitable(result):
            asyncio.run(result)
    finally:
        del component.set_arg
        del component.set_arg_auto
    return captured


def _is_true(value):
    """Whether an invert setting's value means True - components pass True or the string "True"."""
    return value is True or str(value).lower() == "true"


def assert_record_agrees(record, captured, index=0):
    """Fail unless every setting automatic_config() bound for device `index` appears in the record.

    Skips the settings that are not facts about the inverter (SETTINGS_OUTSIDE_THE_RECORD), resets to None,
    and settings inverter.py replaces with a dummy for this type. An entity-id value must match the
    descriptor's entity_id; a *_invert value must match the named entity's invert; any other literal must
    match the descriptor's value stand-in or the rating of the same name.
    """
    inverter_type = record["inverter_type"]
    entities = record.get("entities") or {}
    ratings = record.get("ratings") or {}
    skipped = SETTINGS_OUTSIDE_THE_RECORD | dummied_settings(inverter_type)
    if index != 0:
        skipped = skipped | SITE_SETTINGS
    problems = []
    for setting, value in sorted(captured.items()):
        if setting in skipped or value is None:
            continue
        if isinstance(value, list):
            if index >= len(value):
                problems.append("{}: automatic_config() bound no value for device {}".format(setting, index))
                continue
            value = value[index]
        if setting in INVERT_SETTINGS:
            actual = bool((entities.get(INVERT_SETTINGS[setting]) or {}).get("invert", False))
            if actual != _is_true(value):
                problems.append("{}: automatic_config() sets {}, record's {} invert is {}".format(setting, value, INVERT_SETTINGS[setting], actual))
            continue
        descriptor = entities.get(setting) or {}
        if isinstance(value, str) and ENTITY_ID_RE.match(value):
            if descriptor.get("entity_id") != value:
                problems.append("{}: automatic_config() binds {}, record has {}".format(setting, value, descriptor.get("entity_id")))
        elif "value" in descriptor:
            if descriptor["value"] != value:
                problems.append("{}: automatic_config() sets {}, record's value is {}".format(setting, value, descriptor["value"]))
        elif ratings.get(setting) != value:
            problems.append("{}: automatic_config() sets {}, record has neither a value stand-in nor a matching rating ({})".format(setting, value, ratings.get(setting)))
    assert not problems, "{} record disagrees with automatic_config(): {}".format(inverter_type, problems)


def assert_record_binds_nothing_extra(record, captured, index=0, allowed_extra=()):
    """Fail if the record's entities bind a setting automatic_config() did not bind for device `index`.

    The reverse of assert_record_agrees(): together they pin the record to exactly what automatic_config()
    binds. allowed_extra names settings the record carries on purpose although automatic_config() skipped
    them - a device fact the user told Predbat to ignore (spec D11), or one a fleet-wide gate withheld (D10).
    """
    bound = set()
    for setting, value in captured.items():
        if isinstance(value, list):
            value = value[index] if index < len(value) else None
        if value is not None:
            bound.add(setting)
    extra = sorted(set(record.get("entities") or {}) - bound - set(allowed_extra))
    assert not extra, "{} record binds settings automatic_config() did not bind for device {}: {}".format(record["inverter_type"], index, extra)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test discovery_contract > /tmp/contract.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/contract.log | head; tail -3 /tmp/contract.log`
Expected: no FAIL/Traceback lines.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/tests/discovery_contract.py apps/predbat/tests/test_discovery_contract.py apps/predbat/unit_test.py
git commit -m "test(discovery): shared completeness and agreement checks for inverter reporters

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Sunsynk reporter

**Files:**
- Modify: `apps/predbat/sunsynk.py` (import `SUNSYNK_IMPORT_LIMIT_FIELD`, module constant `SUNSYNK_CAPABILITIES`, `initialize()` gains `_discovery_sensors`, new `_discovery_ratings` / `_discovery_sensor_bindings` / `_discovery_entities`, `build_discovery` rewritten)
- Modify: `apps/predbat/tests/test_sunsynk_api.py` (`MockSunsynk.__init__` mirrors the new `_discovery_sensors` attribute)
- Test: `apps/predbat/tests/test_sunsynk_config.py`

**Interfaces:**
- Consumes: `validated_inverters`, `assert_definition_complete`, `capture_automatic_config`, `assert_record_agrees`, `assert_record_binds_nothing_extra` from Task 2 (`tests/discovery_contract.py`); `ComponentBase.WRITE_AND_POLL_SLEEP` (2, not overridden) from Task 1; `inverter_record(..., entities=...)` accepting a dict `capabilities` from Task 1.
- Produces:
  - `sunsynk.SUNSYNK_CAPABILITIES` - literal dict of the seven `CAPABILITY_KEYS`, equal to the SunsynkCloud row.
  - Each Sunsynk inverter record carries `capabilities` (that dict), `entities` (one descriptor per setting `automatic_config()` binds for that serial) and `ratings` keyed `inverter_limit` (ratePower), `export_limit` (raw `pvMaxLimit`), `import_limit` (raw `importPower`, new), `battery_min_soc` (batteryLowCap, new), plus the descriptive `battery_capacity_ah`. There is no `soc_max` rating (spec D14 - see below); `soc_max` is an entity only.
  - The old tokens (`schedule`, `target_soc`, `discharge_target`, `charge_rate_power`, `export_limit`) and rating names (`inverter_w`, `battery_kwh`) are gone from Sunsynk.

How the spec decisions land for Sunsynk (each is pinned by a test below):
- **D10 - per device.** `automatic_config()` binds a sensor-backed setting (`soc_max`, `battery_rate_max`, `inverter_limit`, `export_limit`, `battery_min_soc`, the six energy counters) only when EVERY inverter reports it. The record uses the per-device half of that test, calling the same accessors. `test_sunsynk_mixed_fleet_records_describe_each_device` shows the first inverter's record carrying `soc_max`/`battery_rate_max`/`pv_today` that the fleet gate withheld (allowed by `assert_record_binds_nothing_extra(..., allowed_extra=...)`) and the second's carrying none of them.
- **D11 - device, not opt-outs.** `automatic_ignore_pv` no longer removes `pv_power`/`pv_today` from the record.
- **D14 - device-reported ratings only.** Sunsynk reports amp-hours; `battery_capacity()` turns them into kWh with a pack voltage Predbat infers from chargeVolt or the user sets (`sunsynk_battery_nominal_voltage`). That is a derivation, so `soc_max` is bound as the `battery_capacity` sensor entity and is NOT a rating. The previous Ah/kWh pair logic reduces to keeping the last Ah seen (`_discovery_battery_ratings` now holds `{"battery_capacity_ah": ...}` only). No `battery_rate_max` rating either (also derived).
- **D12, D13, D15** do not apply: Sunsynk has no PV-only records, is not GE, and binds no site setting.
- **Bindings latch once reported** (`_discovery_sensors`, reporter-internal): `publish_data()` skips a sensor a poll did not bring, so Home Assistant keeps its last state; dropping the binding would make the report flap on a partial poll.
- **No `invert` on any descriptor**: `automatic_config()` sets all three `*_power_invert` False because `publish_data()` already emits Predbat's conventions, and a missing `invert` means False.
- **`export_limit` entity vs rating differ by design**: the entity is `automatic_config()`'s binding to the `export_limit` sensor, whose state is `export_limit()` = min(pvMaxLimit, ratePower); the rating is raw pvMaxLimit (spec D1). With no pvMaxLimit the entity is still bound (export_limit() falls back to the rating) and there is no rating.

- [ ] **Step 1: Run impact analysis**

Run `impact({target: "build_discovery", direction: "upstream", file_path: "apps/predbat/sunsynk.py"})`. Expected: callers are `ComponentBase.refresh_discovery` (via `run()`) and the Sunsynk catalogue tests - LOW risk, observe-only. (If the index predates PR #5206 the symbol is not found; confirm by `grep -rn "build_discovery()" apps/predbat/sunsynk.py apps/predbat/component_base.py apps/predbat/tests/test_sunsynk_config.py`.) `automatic_config`, `publish_data` and every write path are not edited.

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_sunsynk_config.py`, replace the `from sunsynk import ...` line and add the contract import after it:

```python
from sunsynk import SunsynkAPI, load_apps_yaml_credentials, _tou_test_window
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
```

Add this constant directly after `SUNSYNK_LIVE_SERIAL = "2405116013"`:

```python
# The seven behaviour keys as the SunsynkCloud INVERTER_DEF row states them (config.py). Spelled out, not
# read from the row, so a change to either side shows up here.
SUNSYNK_ROW_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": True,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}
```

Replace `test_sunsynk_catalogue_describes_each_inverter` and `test_sunsynk_catalogue_export_limit_only_on_evidence` with:

```python
def test_sunsynk_catalogue_describes_each_inverter():
    """Each inverter is a SunsynkCloud record with the confirmed-live ratings under Predbat's setting names."""
    failed = False
    s = _sunsynk_fleet()
    record = s.build_discovery()["inverters"][0]
    checks = [
        (record["device_id"], "sunsynk:" + SUNSYNK_LIVE_SERIAL),
        (record["inverter_type"], "SunsynkCloud"),
        (record["composition"], "direct"),
        (record["functions"], ["solar", "battery"]),
        (record["hardware_ids"], {"serial": SUNSYNK_LIVE_SERIAL}),
        (record["capabilities"], SUNSYNK_ROW_CAPABILITIES),
        (record["ratings"]["inverter_limit"], 8000.0),
        (record["ratings"]["export_limit"], 7000.0),
        (record["ratings"]["battery_capacity_ah"], 200.0),
        (record["entities"]["soc_max"]["entity_id"], s._sensor_name(SUNSYNK_LIVE_SERIAL, "battery_capacity")),
    ]
    for actual, expected in checks:
        if actual != expected:
            print("ERROR: expected {!r}, got {!r}".format(expected, actual))
            failed = True
    # Spec D14: the kWh is Predbat's derivation (Ah x an inferred or user-set pack voltage), so it is
    # bound as the battery_capacity sensor above but is not a rating
    for old in ("inverter_w", "battery_kwh", "max_charge_w", "soc_max"):
        if old in record["ratings"]:
            print("ERROR: rating {} must be absent (renamed, or a Predbat derivation): {}".format(old, record["ratings"]))
            failed = True
    for absent in ("info", "account_ids"):
        if absent in record:
            print("ERROR: Sunsynk holds no model, firmware or station ID, so {} must be absent: {}".format(absent, record[absent]))
            failed = True
    return failed


def test_sunsynk_catalogue_export_limit_only_on_evidence():
    """The export_limit rating is the raw pvMaxLimit only; the entity follows automatic_config()'s export_limit() test.

    export_limit() falls back to the inverter rating when pvMaxLimit is absent, so automatic_config()
    still binds the export_limit sensor, and so does the record - but the rating is the configured
    cap (spec D1), which this install has not stated, so there is no rating.
    """
    failed = False
    s = _sunsynk_fleet()
    s.device_settings = {}
    record = s.build_discovery()["inverters"][0]
    if "export_limit" in record.get("ratings", {}):
        print("ERROR: with no pvMaxLimit there is no configured export cap to report: {}".format(record["ratings"]))
        failed = True
    if record.get("entities", {}).get("export_limit", {}).get("entity_id") != s._sensor_name(SUNSYNK_LIVE_SERIAL, "export_limit"):
        print("ERROR: export_limit() falls back to the rating, so the export_limit sensor is bound: {}".format(record.get("entities", {}).get("export_limit")))
        failed = True
    nothing = _sunsynk_fleet()
    nothing.device_rated_power = {}
    nothing.device_settings = {}
    record = nothing.build_discovery()["inverters"][0]
    if "export_limit" in record.get("ratings", {}) or "export_limit" in record.get("entities", {}):
        print("ERROR: with no rating and no pvMaxLimit, export_limit() is 0 - no rating and no entity: {}".format(record))
        failed = True
    return failed
```

Replace `test_sunsynk_catalogue_keeps_battery_ratings_through_a_partial_poll` with (the kWh is no longer a rating, so there is no Ah/kWh pair to keep consistent):

```python
def test_sunsynk_catalogue_keeps_battery_ratings_through_a_partial_poll():
    """A poll that omits the battery fields keeps the last battery Ah rating, as the published sensors keep their state.

    fetch_device_data() rebuilds device_values from every poll and leaves out a field the battery
    endpoint did not return. publish_data() then skips the battery sensors, so Home Assistant - and
    Predbat's soc_max - keep the last value. The catalogue keeps its last Ah too, rather than filing a
    thinner report and then the full one again when the fields return. The kWh is never a rating
    (spec D14), so there is no Ah/kWh pair left to keep consistent.
    """
    failed = False
    s = _sunsynk_fleet()
    sn = SUNSYNK_LIVE_SERIAL
    full = s.build_discovery()
    for name, partial in (("no capacity", {"chargeVolt": 58.4}), ("no battery fields", {})):
        s.device_values = {sn: dict(partial)}
        if s.build_discovery() != full:
            print("ERROR: {}: the report changed on a partial poll: {}".format(name, s.build_discovery()))
            failed = True

    # A poll that carries a new Ah replaces the kept one, with or without a chargeVolt
    s.device_values = {sn: {"capacity": 280}}
    ratings = s.build_discovery()["inverters"][0]["ratings"]
    if ratings.get("battery_capacity_ah") != 280.0 or "soc_max" in ratings:
        print("ERROR: a fresh Ah must replace the kept one, and soc_max is never a rating: {}".format(ratings))
        failed = True

    # Nothing is invented for an inverter whose battery fields have never been seen
    s.device_list = [sn, "UNSEEN1"]
    unseen = {record["device_id"]: record for record in s.build_discovery()["inverters"]}["sunsynk:UNSEEN1"]
    if {"soc_max", "battery_capacity_ah"} & set(unseen.get("ratings", {})) or "soc_max" in unseen.get("entities", {}):
        print("ERROR: an inverter never polled must carry no battery rating or capacity binding: {}".format(unseen))
        failed = True
    return failed
```

Add these before `test_sunsynk_catalogue_filed_when_first_cycle_defers`:

```python
def _sunsynk_driven_fleet(serials=(SUNSYNK_LIVE_SERIAL,)):
    """A MockSunsynk whose inverters report everything automatic_config() binds.

    The confirmed-live figures of _sunsynk_fleet() (ratePower 8000, pvMaxLimit 7000, 200 Ah at
    chargeVolt 58.4) plus the confirmed-live importPower 10350, a batteryLowCap floor, a charge
    current limit and every daily energy counter, so every conditional binding in
    automatic_config() is taken.
    """
    s = MockSunsynk()
    s.device_list = list(serials)
    for sn in serials:
        s.device_rated_power[sn] = 8000.0
        s.device_values[sn] = {"soc": 50, "capacity": 200, "chargeVolt": 58.4, "chargeCurrentLimit": 100}
        s.device_energy[sn] = {"pv_today": 1.0, "import_today": 1.0, "export_today": 1.0, "load_today": 1.0, "battery_charge_today": 1.0, "battery_discharge_today": 1.0}
        s.device_settings[sn] = {"batteryLowCap": "20", "pvMaxLimit": "7000", "importPower": "10350"}
    return s


def test_sunsynk_record_rebuilds_its_inverter_def_row():
    """Completeness: the record alone, with no INVERTER_DEF row as a base, rebuilds the SunsynkCloud row."""
    s = _sunsynk_driven_fleet()
    for record in validated_inverters(s.build_discovery()):
        assert_definition_complete(record, SunsynkAPI.WRITE_AND_POLL_SLEEP)
    return False


def test_sunsynk_record_agrees_with_automatic_config():
    """Agreement both ways: every setting automatic_config() binds is in the record, and the record binds nothing more."""
    s = _sunsynk_driven_fleet()
    records = validated_inverters(s.build_discovery())
    captured = capture_automatic_config(s)
    assert_record_agrees(records[0], captured, index=0)
    assert_record_binds_nothing_extra(records[0], captured, index=0)
    return False


def test_sunsynk_two_inverters_give_two_records_with_their_own_entities():
    """Review Focus 1: two inverters give two records, each binding its own serial's entities and agreeing at its own index."""
    failed = False
    s = _sunsynk_driven_fleet(("2405116013", "2211093089"))
    records = validated_inverters(s.build_discovery())
    if [record["device_id"] for record in records] != ["sunsynk:2405116013", "sunsynk:2211093089"]:
        print("ERROR: expected one record per serial, got {}".format([record["device_id"] for record in records]))
        return True
    captured = capture_automatic_config(s)
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    first, second = (record["entities"] for record in records)
    if set(first) != set(second):
        print("ERROR: both inverters report the same data, so they bind the same settings: {} vs {}".format(sorted(first), sorted(second)))
        failed = True
    for setting in sorted(set(first) & set(second)):
        if first[setting]["entity_id"] == second[setting]["entity_id"]:
            print("ERROR: {} is bound to the same entity {} for both inverters".format(setting, first[setting]["entity_id"]))
            failed = True
    return failed


def test_sunsynk_record_ratings_are_the_configured_figures():
    """Spec D1: export_limit is the raw pvMaxLimit and import_limit the raw importPower, never capped by the rating.

    A cap set above the inverter's rating is reported as set. export_limit() - the state of the
    export_limit sensor automatic_config() binds - is the lower of the two, 8000; the rating is
    the configured 9000.
    """
    failed = False
    s = _sunsynk_driven_fleet()
    s.device_settings[SUNSYNK_LIVE_SERIAL]["pvMaxLimit"] = "9000"
    record = s.build_discovery()["inverters"][0]
    expected = {"inverter_limit": 8000.0, "export_limit": 9000.0, "import_limit": 10350.0, "battery_min_soc": 20, "battery_capacity_ah": 200.0}
    if record["ratings"] != expected:
        print("ERROR: ratings {}, expected {}".format(record["ratings"], expected))
        failed = True
    if s.export_limit(SUNSYNK_LIVE_SERIAL) != 8000.0:
        print("ERROR: export_limit() should still be the lower of cap and rating, got {}".format(s.export_limit(SUNSYNK_LIVE_SERIAL)))
        failed = True
    if record["entities"]["export_limit"] != {"entity_id": s._sensor_name(SUNSYNK_LIVE_SERIAL, "export_limit"), "access": "r", "unit": "W"}:
        print("ERROR: the export_limit binding must be automatic_config()'s sensor: {}".format(record["entities"]["export_limit"]))
        failed = True
    return failed


def test_sunsynk_record_keeps_pv_despite_automatic_ignore_pv():
    """Spec D11: automatic_ignore_pv stops automatic_config() binding PV, but the record still describes the device's PV."""
    failed = False
    s = _sunsynk_driven_fleet()
    s.automatic_ignore_pv = True
    record = validated_inverters(s.build_discovery())[0]
    for setting, leaf in (("pv_power", "pv_power"), ("pv_today", "pv_today")):
        if record["entities"].get(setting, {}).get("entity_id") != s._sensor_name(SUNSYNK_LIVE_SERIAL, leaf):
            print("ERROR: {} must stay in the record despite automatic_ignore_pv: {}".format(setting, record["entities"].get(setting)))
            failed = True
    captured = capture_automatic_config(s)
    for setting in ("pv_power", "pv_today"):
        if setting in captured:
            print("ERROR: automatic_config() should still skip {} under automatic_ignore_pv".format(setting))
            failed = True
    assert_record_agrees(record, captured)
    assert_record_binds_nothing_extra(record, captured, allowed_extra=("pv_power", "pv_today"))
    return failed


def test_sunsynk_mixed_fleet_records_describe_each_device():
    """Spec D10: each record lists what its own device has, not what automatic_config()'s all-inverters gate allows.

    The second inverter reports no chargeVolt, charge current or PV counter, so automatic_config()
    binds soc_max, battery_rate_max and pv_today for neither. The first inverter's record still
    carries all three; the second's carries none of them.
    """
    failed = False
    per_device = ("soc_max", "battery_rate_max", "pv_today")
    s = _sunsynk_driven_fleet(("2405116013", "2211093089"))
    s.device_values["2211093089"] = {"soc": 50, "capacity": 200}
    del s.device_energy["2211093089"]["pv_today"]
    first, second = validated_inverters(s.build_discovery())
    captured = capture_automatic_config(s)
    for setting in per_device:
        if setting in captured:
            print("ERROR: the fleet gate should have withheld {} from automatic_config(): {}".format(setting, captured[setting]))
            failed = True
        if setting not in first["entities"]:
            print("ERROR: the first inverter reports {}, so its record must bind it".format(setting))
            failed = True
        if setting in second["entities"]:
            print("ERROR: the second inverter does not report {}, so its record must not bind it: {}".format(setting, second["entities"][setting]))
            failed = True
    assert_record_agrees(first, captured, index=0)
    assert_record_agrees(second, captured, index=1)
    assert_record_binds_nothing_extra(first, captured, index=0, allowed_extra=per_device)
    assert_record_binds_nothing_extra(second, captured, index=1)
    return failed


def test_sunsynk_record_keeps_sensor_bindings_through_a_partial_poll():
    """A poll that omits a rating's inputs or an energy counter keeps the binding, as Home Assistant keeps the sensor.

    publish_data() skips a sensor whose value this poll did not bring, so the entity keeps its last
    state and automatic_config()'s binding stays good. The record keeps the binding too, rather than
    filing a thinner report and then the full one again.
    """
    failed = False
    s = _sunsynk_driven_fleet()
    full = s.build_discovery()
    s.device_values = {SUNSYNK_LIVE_SERIAL: {"soc": 50}}
    s.device_energy = {SUNSYNK_LIVE_SERIAL: {}}
    if s.build_discovery() != full:
        print("ERROR: a partial poll changed the report: {}".format(s.build_discovery()))
        failed = True
    fresh = _sunsynk_driven_fleet()
    fresh.device_values = {SUNSYNK_LIVE_SERIAL: {"soc": 50}}
    fresh.device_energy = {SUNSYNK_LIVE_SERIAL: {"load_today": 1.0}}
    entities = fresh.build_discovery()["inverters"][0]["entities"]
    for setting in ("soc_max", "battery_rate_max", "pv_today", "import_today"):
        if setting in entities:
            print("ERROR: {} was never reported, so no sensor exists to bind: {}".format(setting, entities[setting]))
            failed = True
    if "load_today" not in entities:
        print("ERROR: load_today is reported, so its sensor must be bound")
        failed = True
    return failed
```

Register them in `run_sunsynk_config_tests()`, directly after the `("catalogue_filed_when_first_cycle_defers", ...)` entry:

```python
        ("record_rebuilds_inverter_def_row", test_sunsynk_record_rebuilds_its_inverter_def_row),
        ("record_agrees_with_automatic_config", test_sunsynk_record_agrees_with_automatic_config),
        ("record_two_inverters", test_sunsynk_two_inverters_give_two_records_with_their_own_entities),
        ("record_ratings_are_configured_figures", test_sunsynk_record_ratings_are_the_configured_figures),
        ("record_keeps_pv_despite_ignore_pv", test_sunsynk_record_keeps_pv_despite_automatic_ignore_pv),
        ("record_mixed_fleet", test_sunsynk_mixed_fleet_records_describe_each_device),
        ("record_keeps_bindings_through_partial_poll", test_sunsynk_record_keeps_sensor_bindings_through_a_partial_poll),
```

In `apps/predbat/tests/test_sunsynk_api.py`, `MockSunsynk.__init__` (which bypasses `initialize()`), add after `self._discovery_battery_ratings = {}`:

```python
        self._discovery_sensors = {}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test sunsynk_config > /tmp/sunsynk_config.log 2>&1; grep -n "FAILED\|EXCEPTION" /tmp/sunsynk_config.log | head -20`
Expected: `EXCEPTION in sunsynk_config.catalogue_describes_each_inverter: 'inverter_limit'`, `FAILED: sunsynk_config.catalogue_export_limit_only_on_evidence`, `FAILED: sunsynk_config.catalogue_keeps_battery_ratings_through_a_partial_poll`, `EXCEPTION in sunsynk_config.record_rebuilds_inverter_def_row: SunsynkCloud record cannot rebuild these INVERTER_DEF fields: ['support_charge_freeze', ..., 'soc_units']`, `EXCEPTION in sunsynk_config.record_agrees_with_automatic_config` and `record_two_inverters: SunsynkCloud record disagrees with automatic_config(): [...]`, and `EXCEPTION ... 'entities'` for `record_ratings_are_configured_figures`, `record_keeps_pv_despite_ignore_pv`, `record_mixed_fleet` and `record_keeps_bindings_through_partial_poll`. (`catalogue_round_trips` still passes - the legacy token list is tolerated until Task 10.)

- [ ] **Step 4: Implement in `apps/predbat/sunsynk.py`**

Add `SUNSYNK_IMPORT_LIMIT_FIELD,` to the `from sunsynk_const import (...)` list, directly after `SUNSYNK_EXPORT_LIMIT_FIELD,`.

Add this module constant between the end of that import and `class SunsynkAPI(`:

```python
# The seven INVERTER_DEF behaviour keys (coordinator.CAPABILITY_KEYS) every SunsynkCloud inverter has,
# matching the SunsynkCloud row in config.py. A literal, never read back from INVERTER_DEF: the discovery
# completeness test rebuilds the row from the record, and a record copied from the row would prove nothing.
SUNSYNK_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    # Freeze Export selects Selling First, which runs PV -> load -> grid ahead of the battery
    "support_feedin_first": True,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}
```

In `initialize()`, replace `self._discovery_battery_ratings = {}` and its comment with:

```python
        # {sn: last battery Ah rating seen} - build_discovery() keeps it through a poll that omits it
        self._discovery_battery_ratings = {}
        # {sn: sensor-backed settings once reported} - build_discovery() keeps their bindings likewise
        self._discovery_sensors = {}
```

Replace `build_discovery()` (everything from `    def build_discovery(self):` up to `    async def refresh_static(self):`) with these four methods:

```python
    def _discovery_ratings(self, sn):
        """The figures one inverter reports about itself, keyed by Predbat setting name in Predbat's units.

        Only figures the device reports go here (spec D14). inverter_limit is the hardware rating
        (ratePower, W). export_limit and import_limit are the configured grid caps exactly as set -
        pvMaxLimit and importPower, W - never bounded by the rating: export_limit() is the lower of
        pvMaxLimit and the rating, which conflates two limits (spec D1), so it is not used here.
        battery_min_soc is the inverter's own floor (batteryLowCap, %).

        soc_max is deliberately NOT a rating: Sunsynk reports amp-hours, and battery_capacity() turns
        them into kWh with a pack voltage Predbat infers from chargeVolt or the user sets
        (sunsynk_battery_nominal_voltage). A derived or user-overridden figure is an entity only (D14),
        so soc_max appears in entities, bound to the battery_capacity sensor. The Ah the battery
        endpoint returned is reported as the descriptive battery_capacity_ah. That endpoint is re-read
        every poll and can omit the field, so the last Ah seen is kept (_discovery_battery_ratings)
        rather than thinning the report on a partial poll; nothing is reported for an inverter whose
        battery fields have never been seen.

        A figure that is not known (0) is left out rather than reported as 0.
        """
        ratings = {}
        rated_w = self.inverter_limit(sn)
        if rated_w > 0:
            ratings["inverter_limit"] = rated_w
        settings = self.device_settings.get(sn, {})
        export_cap = self._as_float(settings.get(SUNSYNK_EXPORT_LIMIT_FIELD))
        if export_cap > 0:
            ratings["export_limit"] = export_cap
        import_cap = self._as_float(settings.get(SUNSYNK_IMPORT_LIMIT_FIELD))
        if import_cap > 0:
            ratings["import_limit"] = import_cap
        floor = self.battery_reserve_min(sn)
        if floor > 0:
            ratings["battery_min_soc"] = floor
        capacity_ah = self._as_float(self.device_values.get(sn, {}).get(SUNSYNK_CAPACITY_AH_FIELD))
        if capacity_ah > 0:
            self._discovery_battery_ratings[sn] = {"battery_capacity_ah": capacity_ah}
        ratings.update(self._discovery_battery_ratings.get(sn, {}))
        return ratings

    def _discovery_sensor_bindings(self, sn):
        """The sensor-backed settings automatic_config() binds only once a value is reported: {setting: (sensor leaf, unit)}.

        automatic_config() binds each of these only when every inverter reports the value, using the
        accessor named beside it here. A record describes its own device, not the fleet (spec D10), so
        this is the per-device half of that test: an inverter's record carries its own sensors whatever
        the other inverters report, and the coordinator applies whatever fleet rule it keeps.
        automatic_ignore_pv does not remove pv_today either - that is the user's opt-out, which the
        coordinator applies (D11).

        Once reported, a binding is kept (_discovery_sensors). publish_data() skips a sensor whose value
        a poll did not bring, so Home Assistant keeps its last state and the binding stays good;
        dropping it would thin the report on a partial poll and restore it on the next.
        """
        seen = self._discovery_sensors.setdefault(sn, set())
        candidates = {
            "soc_max": ("battery_capacity", "kWh", self.battery_capacity(sn) > 0),
            "battery_rate_max": ("battery_rate_max", "W", self.battery_rate_max(sn) > 0),
            "inverter_limit": ("inverter_limit", "W", self.inverter_limit(sn) > 0),
            # This sensor's state is export_limit(), the lower of pvMaxLimit and the rating - that is the
            # binding automatic_config() makes, recorded as it is. The export_limit RATING is the raw
            # pvMaxLimit (_discovery_ratings, spec D1), so the two can legitimately differ.
            "export_limit": ("export_limit", "W", self.export_limit(sn) > 0),
            "battery_min_soc": ("battery_reserve_min", "%", self.battery_reserve_min(sn) > 0),
        }
        for leaf in SUNSYNK_ENERGY:
            candidates[leaf] = (leaf, "kWh", leaf in self.device_energy.get(sn, {}))
        bindings = {}
        for setting, (leaf, unit, reported) in candidates.items():
            if reported:
                seen.add(setting)
            if setting in seen:
                bindings[setting] = (leaf, unit)
        return bindings

    def _discovery_entities(self, sn):
        """Every setting automatic_config() binds for one inverter, as discovery entity descriptors.

        Entity ids come from the same _sensor_name()/_control_name() calls automatic_config() makes, so
        the two cannot drift; tests/test_sunsynk_config.py checks they agree both ways. Sensors are read
        (access "r"); the schedule controls, the reserve and the write button are written ("rw"). The
        schedule selects carry HH:MM:SS, the format publish_schedule_settings_ha() publishes and
        inverter.py expects of them. No descriptor carries invert: publish_data() already emits
        Predbat's sign conventions, which is why automatic_config() sets every *_power_invert False,
        and a missing invert means False.

        pv_power is bound whatever automatic_ignore_pv says: the record describes the device, and the
        user's opt-out is applied by whoever configures from it (spec D11).
        """

        def sensor(leaf, unit):
            """A read-only binding to one of this inverter's published sensors."""
            return {"entity_id": self._sensor_name(sn, leaf), "access": "r", "unit": unit}

        def control(domain, leaf, **fields):
            """A binding to one of the schedule control entities Predbat writes."""
            return {"entity_id": self._control_name(domain, sn, leaf), "access": "rw", "domain": domain, **fields}

        entities = {
            "soc_percent": sensor("soc", "%"),
            "battery_power": sensor("battery_power", "W"),
            "grid_power": sensor("grid_power", "W"),
            "load_power": sensor("load_power", "W"),
            "battery_temperature": sensor("temperature", "°C"),
            "reserve": control("number", "battery_schedule_reserve", unit="%"),
            "charge_start_time": control("select", "battery_schedule_charge_start_time", format="HH:MM:SS"),
            "charge_end_time": control("select", "battery_schedule_charge_end_time", format="HH:MM:SS"),
            "charge_limit": control("number", "battery_schedule_charge_soc", unit="%"),
            "charge_rate": control("number", "battery_schedule_charge_power", unit="W"),
            "scheduled_charge_enable": control("switch", "battery_schedule_charge_enable"),
            "discharge_start_time": control("select", "battery_schedule_export_start_time", format="HH:MM:SS"),
            "discharge_end_time": control("select", "battery_schedule_export_end_time", format="HH:MM:SS"),
            "discharge_target_soc": control("number", "battery_schedule_export_soc", unit="%"),
            "discharge_rate": control("number", "battery_schedule_export_power", unit="W"),
            "scheduled_discharge_enable": control("switch", "battery_schedule_export_enable"),
            "schedule_write_button": control("switch", "battery_schedule_charge_write"),
            "pv_power": sensor("pv_power", "W"),
        }
        for setting, (leaf, unit) in self._discovery_sensor_bindings(sn).items():
            entities[setting] = sensor(leaf, unit)
        return entities

    def build_discovery(self):
        """
        Describe the discovered Sunsynk inverters for the discovery catalogue.

        Reads only state the component already holds - device_list, the telemetry, settings and the
        rating, capacity and export-limit accessors - so this adds no API calls and cannot change what
        Sunsynk does. Reporting is independent of self.automatic.

        Discovery applies no device-type filter and automatic_config() registers every serial as
        "SunsynkCloud" with no further test, binding PV and battery entities for each. Nothing
        Sunsynk returns tells a PV-only unit from a hybrid, so neither can this: every record
        carries inverter_type "SunsynkCloud" and functions solar and battery, mirroring
        automatic_config() as the source of truth. No device is excluded here either: discovery
        and automatic_config() both register every serial, so there is no excluded-device case to
        mirror.

        Each record carries SUNSYNK_CAPABILITIES, the entities automatic_config() binds for that
        inverter (_discovery_entities) and its ratings under Predbat's setting names
        (_discovery_ratings). Together they rebuild the SunsynkCloud INVERTER_DEF row
        (coordinator.inverter_definition()), which tests/test_sunsynk_config.py proves.

        Deliberately not reported: model, firmware and any station ID - none is held, and Sunsynk
        has no station grouping at all.

        Returns None when no inverter has been discovered yet.
        """
        if not self.device_list:
            return None

        inverters = []
        for sn in self.device_list:
            inverters.append(
                inverter_record(
                    "sunsynk:{}".format(sn),
                    inverter_type="SunsynkCloud",
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=dict(SUNSYNK_CAPABILITIES),
                    hardware_ids={"serial": sn},
                    ratings=self._discovery_ratings(sn),
                    entities=self._discovery_entities(sn),
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test sunsynk_config > /tmp/sunsynk_config.log 2>&1; grep -n "FAILED\|EXCEPTION\|Traceback" /tmp/sunsynk_config.log | head; tail -2 /tmp/sunsynk_config.log`
Expected: no FAILED/EXCEPTION lines; `**** Test sunsynk_config PASSED`.

Then the neighbouring suites: `./run_all --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test sunsynk_const --test sunsynk_auth --test sunsynk_api --test sunsynk_control --test sunsynk_publish --test sunsynk_storage --test sunsynk_config --test components > /tmp/sunsynk_all.log 2>&1; grep -n "FAILED\|EXCEPTION\|Traceback" /tmp/sunsynk_all.log | head; tail -1 /tmp/sunsynk_all.log`
Expected: none; `**** All tests passed`.

Pre-commit on the changed files: `pre-commit run --files apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_config.py apps/predbat/tests/test_sunsynk_api.py`. Expected: all hooks pass with no rewrites.

- [ ] **Step 6: Commit**

Run `detect_changes()` first: expected changed symbols are `build_discovery` and the three new `_discovery_*` helpers, the two `_discovery_*` attributes in `initialize()` and `MockSunsynk.__init__`. `automatic_config` must NOT appear as modified (a stale index can flag it by line offset - confirm with `git diff -U0 apps/predbat/sunsynk.py | grep "^@@"` that no hunk falls inside it).

```bash
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_config.py apps/predbat/tests/test_sunsynk_api.py
git commit -m "feat(discovery): Sunsynk record carries capabilities, entities and Predbat-named ratings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Deye reporter

**Files:**
- Modify: `apps/predbat/deye.py` (new module constants `DEYE_CAPABILITIES` and `DEYE_SCHEDULE_TIME_FORMAT`; new private helpers `_discovery_entities` and `_discovery_ratings`; `build_discovery`)
- Test: `apps/predbat/tests/test_deye_api.py`

**Interfaces:**
- Consumes: `validated_inverters`, `assert_definition_complete`, `capture_automatic_config`, `assert_record_agrees` and `assert_record_binds_nothing_extra` (Task 2); `ComponentBase.WRITE_AND_POLL_SLEEP` (Task 1). Deye inherits it unchanged, because the DeyeCloud row says 2.
- Produces: each Deye inverter record now carries:
  - `capabilities`: a dict of all seven `CAPABILITY_KEYS`.
  - `entities`: every setting `automatic_config()` binds, as **this** device has it. Each conditional sensor is listed when this inverter publishes it (D10), and `pv_power`/`pv_today` are listed even under `automatic_ignore_pv` (D11).
  - `ratings`: only device-reported figures (D14), keyed by Predbat setting name. `inverter_limit` (W) is RatedPower and `battery_min_soc` (%) is config/battery's battLowCapacity. The descriptive `battery_capacity_ah` stays.

  `soc_max` and `battery_rate_max` are entities only: both are Predbat derivations, scaled by a pack voltage Predbat infers. Removed: `inverter_w`, `battery_kwh`, and the tokens `schedule`, `target_soc`, `discharge_target` and `charge_rate_power`. Deye holds no grid export or import cap anywhere in `deye.py` or `deye_const.py`, so there is no `export_limit` or `import_limit`. There are no flags.

  Unchanged: `functions` stays `["solar", "battery"]` and `inverter_type` stays `"DeyeCloud"` (D8). `account_ids` handling does not change. Deye produces no PV-only records (D12), because every serial is registered as a DeyeCloud battery inverter. It binds no site setting (D15).

- [ ] **Step 1: Run impact analysis**

Run `impact({target: "build_discovery", direction: "upstream", file_path: "apps/predbat/deye.py"})`. Expected: `ComponentBase.refresh_discovery()` (the generic reporting loop) and the Deye tests only. LOW risk. `automatic_config()` is not edited. If the index predates PR #5206, `build_discovery` is not found; fall back to `grep -rn "build_discovery()" apps/predbat --include='*.py'`.

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_deye_api.py`, extend the imports:

```python
from deye_const import DEYE_BASE_URLS, DEYE_TELEMETRY_KEYS, CONFIG_BATTERY_KEYS, DEYE_LATEST_BODY_KEY
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_infra import run_async as run_async_local
```

Replace `test_deye_catalogue_describes_each_inverter` (it pinned the old tokens and rating names) with:

```python
def test_deye_catalogue_describes_each_inverter():
    """Each inverter is a DeyeCloud record with the live sample's own ratings."""
    failed = False
    d = _deye_fleet()
    record = d.build_discovery()["inverters"][0]
    checks = [
        (record["device_id"], "deye:INV1"),
        (record["inverter_type"], "DeyeCloud"),
        (record["composition"], "direct"),
        (record["functions"], ["solar", "battery"]),
        (record["hardware_ids"], {"serial": "INV1"}),
        (record["account_ids"], {"station_id": 10}),
        (
            record["capabilities"],
            {
                "support_charge_freeze": True,
                "support_discharge_freeze": True,
                "support_feedin_first": True,
                "can_span_midnight": False,
                "charge_discharge_with_rate": False,
                "charge_control_immediate": False,
                "target_soc_used_for_discharge": True,
            },
        ),
        (
            record["ratings"],
            {"inverter_limit": 8000.0, "battery_min_soc": 14, "battery_capacity_ah": 1200.0},
        ),
    ]
    for actual, expected in checks:
        if actual != expected:
            print("ERROR: expected {!r}, got {!r}".format(expected, actual))
            failed = True
    # Deye holds no grid export or import cap anywhere (deye.py, deye_const.py), so neither rating may appear;
    # soc_max and battery_rate_max are Predbat derivations (scaled by an inferred pack voltage), so entities only (D14)
    for absent in ("export_limit", "import_limit", "inverter_w", "battery_kwh", "soc_max", "battery_rate_max"):
        if absent in record["ratings"]:
            print("ERROR: rating {} must not be reported: {}".format(absent, record["ratings"]))
            failed = True
    if "info" in record:
        print("ERROR: Deye holds no model or firmware, so info must be absent: {}".format(record["info"]))
        failed = True
    return failed
```

Add these before `run_deye_api_tests`:

```python
def _deye_driven_fleet(*serials, station_ids=(10,)):
    """A MockDeye whose inverters were each read from the live device/latest and config/battery samples.

    fetch_device_data() fills telemetry, the daily energy counters, RatedPower and the derived pack
    voltage exactly as a live cycle does, so automatic_config() binds every setting it can - gated
    ones included - and the record has to match all of them.
    """
    d = MockDeye()
    d.device_list = list(serials or ("INV1",))
    d.station_ids = list(station_ids)

    async def fake_post(endpoint_key, body):
        """Answer device/latest with the live sample for whichever serial was asked for."""
        return {"success": True, "deviceDataList": [{"deviceSn": body[DEYE_LATEST_BODY_KEY][0], "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        for sn in d.device_list:
            run_async_local(d.fetch_device_data(sn))
            d.device_battery_config[sn] = dict(DEYE_LIVE_BATTERY_CONFIG)
    return d


def test_deye_record_rebuilds_inverter_def():
    """Completeness: the record alone, with no INVERTER_DEF row as a base, rebuilds the DeyeCloud row."""
    for record in validated_inverters(_deye_driven_fleet("INV1").build_discovery()):
        assert_definition_complete(record, DeyeAPI.WRITE_AND_POLL_SLEEP)
    return False


def test_deye_record_agrees_with_automatic_config():
    """Agreement both ways: every setting automatic_config() binds is in the record, and the record binds nothing else."""
    d = _deye_driven_fleet("INV1")
    records = validated_inverters(d.build_discovery())
    captured = capture_automatic_config(d)
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    # The fixture drives every conditional binding, so this proves the full set rather than a subset
    for name in ("pv_power", "pv_today", "load_today", "import_today", "export_today", "soc_max", "battery_min_soc", "battery_rate_max", "inverter_limit", "schedule_write_button"):
        assert name in records[0]["entities"], (name, sorted(records[0]["entities"]))
    return False


def test_deye_two_inverters_give_two_distinct_records():
    """Two inverters give two records binding the same settings to different, per-serial entities."""
    d = _deye_driven_fleet("INV1", "INV2")
    records = validated_inverters(d.build_discovery())
    assert [record["device_id"] for record in records] == ["deye:INV1", "deye:INV2"], records
    captured = capture_automatic_config(d)
    for index, record in enumerate(records):
        assert_definition_complete(record, DeyeAPI.WRITE_AND_POLL_SLEEP)
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    first, second = records[0]["entities"], records[1]["entities"]
    assert set(first) == set(second), (sorted(first), sorted(second))
    same = [name for name in first if first[name]["entity_id"] == second[name]["entity_id"]]
    assert not same, "entity ids shared between inverters: {}".format(same)
    return False


def test_deye_record_keeps_pv_when_automatic_ignore_pv():
    """automatic_ignore_pv is the user's opt-out, not a device fact (D11): the record still carries pv_power and pv_today."""
    d = _deye_driven_fleet("INV1")
    d.automatic_ignore_pv = True
    record = validated_inverters(d.build_discovery())[0]
    captured = capture_automatic_config(d)
    assert "pv_power" not in captured and "pv_today" not in captured, sorted(captured)
    assert record["entities"]["pv_power"]["entity_id"] == d._sensor_name("INV1", "pv_power"), record["entities"].get("pv_power")
    assert record["entities"]["pv_today"]["entity_id"] == d._sensor_name("INV1", "pv_today"), record["entities"].get("pv_today")
    assert_record_agrees(record, captured)
    assert_record_binds_nothing_extra(record, captured, allowed_extra=("pv_power", "pv_today"))
    return False


def test_deye_record_is_per_device_in_a_mixed_fleet():
    """Each record lists what its own inverter publishes, not automatic_config()'s fleet-wide gate (D10).

    automatic_config() binds battery_min_soc, battery_rate_max and each energy counter as ONE list
    across every inverter, so when INV2 has no config/battery and no load_today it binds them for
    neither. INV1's record still carries them - its sensors are published - and INV2's does not.
    """
    d = _deye_driven_fleet("INV1", "INV2")
    d.device_battery_config.pop("INV2")
    d.device_energy["INV2"].pop("load_today")
    records = validated_inverters(d.build_discovery())
    captured = capture_automatic_config(d)
    withheld = ("battery_min_soc", "battery_rate_max", "load_today")
    for name in withheld:
        assert name not in captured, (name, sorted(captured))
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index, allowed_extra=withheld)
    inv1, inv2 = records[0]["entities"], records[1]["entities"]
    for name, leaf in (("battery_min_soc", "battery_reserve_min"), ("battery_rate_max", "battery_rate_max"), ("load_today", "load_today")):
        assert inv1[name]["entity_id"] == d._sensor_name("INV1", leaf), (name, inv1.get(name))
        assert name not in inv2, (name, sorted(inv2))
    assert "soc_max" in inv2 and "inverter_limit" in inv2, sorted(inv2)
    assert records[0]["ratings"]["battery_min_soc"] == 14, records[0]["ratings"]
    assert "battery_min_soc" not in records[1]["ratings"], records[1]["ratings"]
    return False
```

Register them in `run_deye_api_tests()`, after `("catalogue_filed_when_first_cycle_defers", ...)`:

```python
        ("record_rebuilds_inverter_def", test_deye_record_rebuilds_inverter_def),
        ("record_agrees_with_automatic_config", test_deye_record_agrees_with_automatic_config),
        ("two_inverters_two_records", test_deye_two_inverters_give_two_distinct_records),
        ("record_keeps_pv_when_ignored", test_deye_record_keeps_pv_when_automatic_ignore_pv),
        ("record_is_per_device_in_mixed_fleet", test_deye_record_is_per_device_in_a_mixed_fleet),
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test deye_api > /tmp/deye_api.log 2>&1; grep -n "FAILED\|EXCEPTION" /tmp/deye_api.log | head -20`
Expected, six failures:
- `FAILED: deye_api.catalogue_describes_each_inverter`: capabilities is the old token list, and ratings has `inverter_w`/`battery_kwh`.
- `EXCEPTION in deye_api.record_rebuilds_inverter_def: DeyeCloud record cannot rebuild these INVERTER_DEF fields: ['support_charge_freeze', ..., 'target_soc_used_for_discharge', 'soc_units']`
- `EXCEPTION in deye_api.record_agrees_with_automatic_config: DeyeCloud record disagrees with automatic_config(): [... record has None ...]`
- `EXCEPTION in deye_api.two_inverters_two_records: DeyeCloud record cannot rebuild these INVERTER_DEF fields: [...]`
- `EXCEPTION in deye_api.record_keeps_pv_when_ignored: 'entities'`
- `EXCEPTION in deye_api.record_is_per_device_in_mixed_fleet: DeyeCloud record disagrees with automatic_config(): [...]`

- [ ] **Step 4: Implement in `apps/predbat/deye.py`**

Add after the `from deye_const import (...)` block, before `class DeyeAPI`:

```python
# How every DeyeCloud inverter behaves, as the INVERTER_DEF keys a discovery record may override
# (coordinator.CAPABILITY_KEYS). A literal, never read back from INVERTER_DEF: the discovery
# completeness test proves the record rebuilds the row, which it could not if the record copied it.
DEYE_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    # Freeze Export selects SELLING_FIRST: PV goes to load, then grid, then the battery
    "support_feedin_first": True,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}

# The format publish_schedule_settings_ha() publishes the schedule time selects in, and so the one
# Predbat must write them in
DEYE_SCHEDULE_TIME_FORMAT = "HH:MM:SS"
```

Replace `build_discovery()` with the two helpers and the new `build_discovery()`, directly after `automatic_config()`. Leave `automatic_config()` itself unchanged:

```python
    def _discovery_entities(self, sn):
        """The entity map for one inverter: every setting automatic_config() binds, as this device has it.

        Entity ids come from the same _sensor_name()/_control_name() calls automatic_config() makes,
        so the two cannot drift. access is "rw" for the schedule controls Predbat writes and "r" for
        the telemetry and rating sensors it only reads. The *_power_invert settings are all False
        (publish_data() already emits Predbat's sign conventions), so no descriptor carries invert.

        Each conditional sensor is listed when THIS inverter publishes it - the same per-device test
        publish_data() uses - not behind automatic_config()'s fleet-wide "every inverter has it" gate
        (spec D10). pv_power and pv_today are listed whatever automatic_ignore_pv says: that is the
        user's opt-out, not a fact about the device (D11).
        """
        entities = {
            "soc_percent": {"entity_id": self._sensor_name(sn, "soc"), "access": "r", "unit": "%"},
            "battery_power": {"entity_id": self._sensor_name(sn, "battery_power"), "access": "r"},
            "grid_power": {"entity_id": self._sensor_name(sn, "grid_power"), "access": "r"},
            "load_power": {"entity_id": self._sensor_name(sn, "load_power"), "access": "r"},
            "pv_power": {"entity_id": self._sensor_name(sn, "pv_power"), "access": "r"},
            "battery_temperature": {"entity_id": self._sensor_name(sn, "temperature"), "access": "r"},
        }
        for leaf in DEYE_ENERGY_KEYS:
            if leaf in self.device_energy.get(sn, {}):
                entities[leaf] = {"entity_id": self._sensor_name(sn, leaf), "access": "r"}
        published = {
            "soc_max": ("battery_capacity", self.battery_capacity(sn) > 0),
            "battery_min_soc": ("battery_reserve_min", sn in self.device_battery_config),
            "battery_rate_max": ("battery_rate_max", self.battery_rate_max(sn) > 0),
            "inverter_limit": ("inverter_limit", self.device_rated_power.get(sn, 0.0) > 0),
        }
        for setting, (leaf, present) in published.items():
            if present:
                entities[setting] = {"entity_id": self._sensor_name(sn, leaf), "access": "r"}
        entities["reserve"] = {"entity_id": self._control_name("number", sn, "battery_schedule_reserve"), "access": "rw"}
        for prefix, direction in (("charge", "charge"), ("discharge", "export")):
            entities[prefix + "_start_time"] = {"entity_id": self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), "access": "rw", "domain": "select", "format": DEYE_SCHEDULE_TIME_FORMAT}
            entities[prefix + "_end_time"] = {"entity_id": self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), "access": "rw", "domain": "select", "format": DEYE_SCHEDULE_TIME_FORMAT}
            entities["scheduled_{}_enable".format(prefix)] = {"entity_id": self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), "access": "rw"}
        entities["charge_limit"] = {"entity_id": self._control_name("number", sn, "battery_schedule_charge_soc"), "access": "rw"}
        entities["charge_rate"] = {"entity_id": self._control_name("number", sn, "battery_schedule_charge_power"), "access": "rw", "unit": "W"}
        entities["discharge_target_soc"] = {"entity_id": self._control_name("number", sn, "battery_schedule_export_soc"), "access": "rw"}
        entities["discharge_rate"] = {"entity_id": self._control_name("number", sn, "battery_schedule_export_power"), "access": "rw", "unit": "W"}
        entities["schedule_write_button"] = {"entity_id": self._control_name("switch", sn, "battery_schedule_charge_write"), "access": "rw"}
        return entities

    def _discovery_ratings(self, sn):
        """One inverter's device-reported ratings, keyed by Predbat setting name in its units, plus the raw Ah capacity.

        A rating is a figure the device reports (spec D14). inverter_limit is device/latest's
        RatedPower (W); battery_min_soc is config/battery's battLowCapacity (%), the floor the
        installer set on the inverter. battery_capacity_ah is config/battery's raw battCapacity.
        soc_max and battery_rate_max are left to their entities: both are Predbat derivations (an Ah
        or amp figure scaled by a pack voltage Predbat infers from the BMS charge request), not
        figures Deye reports. Deye holds no grid export or import cap, so there is no export_limit
        or import_limit.
        """
        ratings = {}
        rated_w = self._as_float(self.device_rated_power.get(sn), 0.0)
        if rated_w > 0:
            ratings["inverter_limit"] = rated_w
        reserve_min = self.battery_reserve_min(sn)
        if reserve_min > 0:
            ratings["battery_min_soc"] = reserve_min
        configured_ah = self._battery_config_value(sn, "capacity")
        if configured_ah > 0:
            ratings["battery_capacity_ah"] = configured_ah
        return ratings

    def build_discovery(self):
        """
        Describe the discovered Deye inverters for the discovery catalogue.

        Reads only state the component already holds - device_list, station_ids,
        device_rated_power, device_energy and the battery accessors - so this adds no API calls and
        cannot change what Deye does. Reporting is independent of self.automatic.

        automatic_config() registers every serial in device_list (already filtered to deviceType
        "INVERTER") as "DeyeCloud" with no further test, and binds both PV and battery entities for
        each. Deye's API offers no way to tell a PV-only unit from a hybrid, so neither can this:
        every record carries inverter_type "DeyeCloud" and functions solar and battery, mirroring
        automatic_config() as the source of truth. That is exactly what Predbat believes about the
        device - and the fact a maintainer needs when a PV-only unit has been configured as a
        battery inverter.

        capabilities is the literal DEYE_CAPABILITIES; entities is every setting automatic_config()
        binds, as this device has it (_discovery_entities); ratings are the figures the device itself
        reports (_discovery_ratings). derive_battery_capacity() is never called here: it logs and
        writes device_pack_voltage/device_capacity, whereas battery_capacity() and battery_rate_max()
        only read them.

        station_ids goes in account_ids only when the account has exactly one station:
        get_device_list() queries every station at once and flattens the result, so which device
        belongs to which station is not held, and attributing one of several would be a guess.

        Deliberately not reported: model and firmware (not held - device/measurePoints and
        station/latest are fetched for debug logging and discarded, and reading them would be a
        new API dependency).

        Returns None when no inverter has been discovered yet.
        """
        if not self.device_list:
            return None

        account_ids = {"station_id": self.station_ids[0]} if len(self.station_ids) == 1 else None

        inverters = []
        for sn in self.device_list:
            inverters.append(
                inverter_record(
                    "deye:{}".format(sn),
                    inverter_type="DeyeCloud",
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=dict(DEYE_CAPABILITIES),
                    hardware_ids={"serial": sn},
                    account_ids=account_ids,
                    ratings=self._discovery_ratings(sn),
                    entities=self._discovery_entities(sn),
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

The helpers use `DEYE_ENERGY_KEYS`, which `deye.py` already imports from `deye_const`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test deye_api > /tmp/deye_api.log 2>&1; grep -n "FAILED\|EXCEPTION\|Traceback" /tmp/deye_api.log | head; tail -2 /tmp/deye_api.log`
Expected: no FAILED/EXCEPTION lines; `**** Test deye_api PASSED ...`.

Then: `./run_all --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test components --test deye_const --test deye_config --test deye_api --test deye_oauth --test deye_control --test deye_publish --test deye_storage > /tmp/deye_all.log 2>&1; grep -n "FAILED\|Traceback" /tmp/deye_all.log | head; tail -1 /tmp/deye_all.log`
Expected: `**** All tests passed ...`.

Then `./run_pre_commit` (or `pre-commit run --files apps/predbat/deye.py apps/predbat/tests/test_deye_api.py`). Expected: all hooks pass.

- [ ] **Step 6: Commit**

Run `detect_changes()` first. Expected: only `deye.py`'s discovery helpers and `build_discovery`, and the test file. `automatic_config` must not appear as modified. A stale index can mis-map shifted line numbers onto neighbouring methods, so confirm with `git diff -U0 apps/predbat/deye.py | grep "^@@"` that no hunk touches the `automatic_config()` body.

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_api.py
git commit -m "feat(discovery): Deye record carries capabilities dict, per-device entity map and device-reported ratings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: AlphaESS reporter

**Files:**
- Modify: `apps/predbat/alphaess.py` (new module constant `ALPHAESS_CAPABILITIES` after `_HOLD_NOT_EVALUATED`; new method `AlphaESSAPI._discovery_entities`; `AlphaESSAPI.build_discovery`)
- Test: `apps/predbat/tests/test_alphaess_api.py`

**Interfaces:**
- Consumes: `CAPABILITY_KEYS` (Task 1); `ComponentBase.WRITE_AND_POLL_SLEEP` (Task 1, default 2 - AlphaESS needs no override, its row says 2); `validated_inverters`, `assert_definition_complete`, `capture_automatic_config`, `assert_record_agrees`, `assert_record_binds_nothing_extra` (Task 2).
- Produces: `alphaess.ALPHAESS_CAPABILITIES` (dict of all seven `CAPABILITY_KEYS`); `AlphaESSAPI._discovery_entities(sn) -> dict`; AlphaESS inverter records carrying dict `capabilities`, per-device `entities` (spec D10, D11), and ratings `inverter_limit` / `soc_max` / `pv_w` - no `export_limit` (AlphaESS reports none) and no `battery_rate_max` rating (D14: it is poinv or the user's override, so it is an entity only). `automatic_config()` is untouched.

- [ ] **Step 1: Run impact analysis**

Run `impact({target: "build_discovery", direction: "upstream", file_path: "apps/predbat/alphaess.py"})`. Expected: its only caller is `ComponentBase.refresh_discovery()` (plus the AlphaESS tests); risk LOW. `_discovery_entities` and `ALPHAESS_CAPABILITIES` are new. (If the index predates PR #5206 the symbol is not found; confirm with `grep -rn "build_discovery()" apps/predbat --include=*.py` that nothing outside `component_base.py` and the tests calls it.)

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_alphaess_api.py`, change the imports:

```python
from coordinator import CAPABILITY_KEYS, validate_report
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
```

In `test_alphaess_catalogue_describes_each_system`, replace the three rating/capability checks with:

```python
        (first["ratings"], {"inverter_limit": 5000.0, "pv_w": 9000.0, "soc_max": 13.34}),
        (sorted(first["capabilities"]), sorted(CAPABILITY_KEYS)),
        (by_id["alphaess:AL70110230302xx"]["ratings"]["soc_max"], 10.1),
```

Add these after `test_alphaess_catalogue_round_trips_through_validate_report`:

```python
def _alphaess_ready(entries, ignore_pv=False):
    """A discovered MockAlphaESS with live readings and today's energy counters, as automatic_config() sees it after the first telemetry cycle."""
    client = _alphaess_discovered(entries)
    client.automatic_ignore_pv = ignore_pv
    for sn in client.device_list:
        client.device_values[sn] = {"soc": 56.0, "battery_power": 1264.0, "grid_power": -11.0, "pv_power": 0.0, "load_power": 1275.0, "ev_power": 0.0}
        client.device_energy[sn] = {"import_today": 14.41, "export_today": 0.42, "pv_today": 10.6, "load_today": 19.49, "ev_energy_today": 3.2}
    return client


def test_alphaess_record_rebuilds_the_inverter_def_row():
    """Completeness: the record alone rebuilds the AlphaESSCloud INVERTER_DEF row, with no gaps."""
    client = _alphaess_ready(ESS_LIST_SAMPLE[:1])
    records = validated_inverters(client.build_discovery())
    for record in records:
        if record.get("inverter_type"):
            assert_definition_complete(record, AlphaESSAPI.WRITE_AND_POLL_SLEEP)
    return False


def test_alphaess_record_agrees_with_automatic_config():
    """Agreement both ways: every setting automatic_config() binds for the device is in its record, and the record binds nothing else."""
    client = _alphaess_ready(ESS_LIST_SAMPLE[:1])
    client._ev_present = {"AL70110230306xx": True}
    records = validated_inverters(client.build_discovery())
    captured = capture_automatic_config(client)
    if "reserve" not in captured or "car_charging_energy" not in captured:
        print("ERROR: the fixture must drive automatic_config() through its control and EV branches, got {}".format(sorted(captured)))
        return True
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    return False


def test_alphaess_two_devices_give_two_records_with_their_own_entities():
    """Two systems give two records, each bound to its own serial's entities (plan Review Focus 1)."""
    failed = False
    client = _alphaess_ready(ESS_LIST_SAMPLE)
    records = validated_inverters(client.build_discovery())
    if len(records) != 2:
        print("ERROR: expected two records, got {}".format(len(records)))
        return True
    first, second = (records[0]["entities"], records[1]["entities"])
    if set(first) != set(second):
        print("ERROR: two like systems should bind the same settings: {} vs {}".format(sorted(first), sorted(second)))
        failed = True
    for setting in sorted(set(first) & set(second)):
        if first[setting]["entity_id"] == second[setting]["entity_id"]:
            print("ERROR: {} is bound to the same entity {} on both systems".format(setting, first[setting]["entity_id"]))
            failed = True
    captured = capture_automatic_config(client)
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    return failed


def test_alphaess_mixed_fleet_records_describe_each_device():
    """Spec D10: each record states what its own system reports, not automatic_config()'s every-inverter gate.

    The second system has not reported load_today and has no poinv, so automatic_config() binds
    neither load_today nor inverter_limit nor battery_rate_max for either system. The first system's
    record still carries all three; the second's carries none of them.
    """
    failed = False
    client = _alphaess_ready(ESS_LIST_SAMPLE)
    second = client.device_list[1]
    client.device_energy[second] = {key: value for key, value in client.device_energy[second].items() if key != "load_today"}
    client.device_detail[second] = dict(client.device_detail[second], poinv=0)
    records = validated_inverters(client.build_discovery())
    captured = capture_automatic_config(client)
    withheld = ("load_today", "inverter_limit", "battery_rate_max")
    for setting in withheld:
        if setting in captured:
            print("ERROR: the fixture must make automatic_config()'s fleet gate withhold {}, but it bound {}".format(setting, captured[setting]))
            failed = True
        if setting not in records[0]["entities"]:
            print("ERROR: the first system reports {} and its record must say so".format(setting))
            failed = True
        if setting in records[1]["entities"]:
            print("ERROR: the second system does not report {} but its record binds it".format(setting))
            failed = True
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
    assert_record_binds_nothing_extra(records[0], captured, index=0, allowed_extra=withheld)
    assert_record_binds_nothing_extra(records[1], captured, index=1)
    return failed


def test_alphaess_record_capabilities_and_ratings():
    """All seven capability keys are stated (support_feedin_first False, the row's default); no export limit is invented."""
    failed = False
    client = _alphaess_ready(ESS_LIST_SAMPLE[:1])
    record = client.build_discovery()["inverters"][0]
    capabilities = record["capabilities"]
    if set(capabilities) != set(CAPABILITY_KEYS):
        print("ERROR: capabilities must state all seven keys, got {}".format(sorted(capabilities)))
        failed = True
    if capabilities.get("support_feedin_first") is not False:
        print("ERROR: the AlphaESSCloud row has no support_feedin_first, which inverter.py reads as False; got {!r}".format(capabilities.get("support_feedin_first")))
        failed = True
    # AlphaESS reports no export power limit: poinv is the inverter rating, not the grid connection's cap
    if "export_limit" in record.get("ratings", {}) or "export_limit" in record["entities"]:
        print("ERROR: AlphaESS reports no export limit, so the record must not claim one: {}".format(record))
        failed = True
    # battery_rate_max is poinv or the user's override, not a figure the device reports - entity only
    if "battery_rate_max" in record.get("ratings", {}) or "battery_rate_max" not in record["entities"]:
        print("ERROR: battery_rate_max must be an entity binding and not a rating: {}".format(record))
        failed = True
    for old in ("inverter_w", "battery_kwh", "max_charge_w"):
        if old in record.get("ratings", {}):
            print("ERROR: rating {} was renamed to its Predbat setting name".format(old))
            failed = True
    for setting, access in (("reserve", "rw"), ("charge_rate", "rw"), ("schedule_write_button", "rw"), ("soc_percent", "r"), ("soc_max", "r")):
        if record["entities"].get(setting, {}).get("access") != access:
            print("ERROR: {} should have access {}, got {}".format(setting, access, record["entities"].get(setting)))
            failed = True
    return failed


def test_alphaess_record_keeps_pv_under_automatic_ignore_pv():
    """Spec D11: automatic_ignore_pv is the user's opt-out, not a device fact, so the record keeps pv_power and pv_today."""
    failed = False
    client = _alphaess_ready(ESS_LIST_SAMPLE[:1], ignore_pv=True)
    record = validated_inverters(client.build_discovery())[0]
    for setting in ("pv_power", "pv_today"):
        if setting not in record["entities"]:
            print("ERROR: {} must stay in the record although automatic_ignore_pv is set".format(setting))
            failed = True
    captured = capture_automatic_config(client)
    if "pv_power" in captured or "pv_today" in captured:
        print("ERROR: automatic_config() must still skip the PV settings under automatic_ignore_pv: {}".format(sorted(captured)))
        failed = True
    assert_record_agrees(record, captured)
    assert_record_binds_nothing_extra(record, captured, allowed_extra=("pv_power", "pv_today"))
    return failed


def test_alphaess_record_leaves_out_what_the_device_has_not_reported():
    """An energy counter not yet seen, or a zero capacity/poinv, is not claimed as an entity."""
    failed = False
    client = _alphaess_ready(ESS_LIST_SAMPLE[:1])
    sn = client.device_list[0]
    client.device_energy[sn] = {"import_today": 1.0}
    client.device_detail[sn] = dict(client.device_detail[sn], poinv=0)
    record = client.build_discovery()["inverters"][0]
    for setting in ("load_today", "export_today", "pv_today", "inverter_limit", "battery_rate_max"):
        if setting in record["entities"]:
            print("ERROR: {} is bound but the device has not reported it: {}".format(setting, record["entities"][setting]))
            failed = True
    if "import_today" not in record["entities"]:
        print("ERROR: import_today is reported and should be bound")
        failed = True
    return failed
```

Register them at the end of the list in `run_alphaess_api_tests()` (after the `catalogue_filed_when_first_cycle_defers` entry):

```python
        ("record_rebuilds_inverter_def_row", test_alphaess_record_rebuilds_the_inverter_def_row),
        ("record_agrees_with_automatic_config", test_alphaess_record_agrees_with_automatic_config),
        ("record_two_devices", test_alphaess_two_devices_give_two_records_with_their_own_entities),
        ("record_capabilities_and_ratings", test_alphaess_record_capabilities_and_ratings),
        ("record_mixed_fleet", test_alphaess_mixed_fleet_records_describe_each_device),
        ("record_keeps_pv_under_ignore_pv", test_alphaess_record_keeps_pv_under_automatic_ignore_pv),
        ("record_leaves_out_unreported", test_alphaess_record_leaves_out_what_the_device_has_not_reported),
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test alphaess_api > /tmp/alphaess_api.log 2>&1; grep -n "FAILED\|EXCEPTION\|AssertionError" /tmp/alphaess_api.log | head -20`
Expected (eight failures):
- `EXCEPTION in alphaess_api.catalogue_describes_each_system: 'soc_max'`
- `record_rebuilds_inverter_def_row`: `AlphaESSCloud record cannot rebuild these INVERTER_DEF fields: ['support_charge_freeze', ..., 'target_soc_used_for_discharge', 'soc_units']`
- `record_agrees_with_automatic_config`: `AlphaESSCloud record disagrees with automatic_config(): ['battery_power: automatic_config() binds sensor.predbat_alphaess_al70110230306xx_battery_power, record has None', ...]`
- `record_two_devices`, `record_mixed_fleet`, `record_keeps_pv_under_ignore_pv`, `record_leaves_out_unreported`: `'entities'`
- `record_capabilities_and_ratings`: `'list' object has no attribute 'get'`

- [ ] **Step 4: Implement in `apps/predbat/alphaess.py`**

Add the constant directly after `_HOLD_NOT_EVALUATED = object()`:

```python
# The behaviour an AlphaESSCloud inverter has, stated for the discovery record's capabilities. A
# literal, never read back from INVERTER_DEF: the record has to rebuild the row on its own, or the
# completeness test proves nothing. support_feedin_first is False because the row leaves it out and
# inverter.py defaults it to False; there is no feed-in-first mode in the Open API.
ALPHAESS_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}
```

Add `_discovery_entities` immediately before `build_discovery`, and replace `build_discovery` whole:

```python
    def _discovery_entities(self, sn):
        """The settings automatic_config() binds for one system, as discovery entity descriptors.

        Each entity id is formed with the same _sensor_name()/_control_name() call automatic_config()
        makes, so the two cannot drift apart. The record describes this system alone (spec D10): a
        setting automatic_config() binds only when a figure is known - an energy counter, the
        capacity, poinv - is described whenever this system reports it, although automatic_config()
        binds it only once every system does. pv_power and pv_today are described even under
        automatic_ignore_pv, which is the user's opt-out rather than a fact about the device (D11).
        The three *_power_invert settings are always False, which is the descriptor's default, so no
        descriptor carries invert. car_charging_* belong to the chargers and cars sections, and
        export_limit and battery_min_soc are never bound (see automatic_config()).
        """
        entities = {
            "soc_percent": {"entity_id": self._sensor_name(sn, "soc"), "access": "r", "unit": "%"},
            "battery_power": {"entity_id": self._sensor_name(sn, "battery_power"), "access": "r", "unit": "W"},
            "grid_power": {"entity_id": self._sensor_name(sn, "grid_power"), "access": "r", "unit": "W"},
            "load_power": {"entity_id": self._sensor_name(sn, "load_power"), "access": "r", "unit": "W"},
            "pv_power": {"entity_id": self._sensor_name(sn, "pv_power"), "access": "r", "unit": "W"},
        }
        energy = self.device_energy.get(sn, {})
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf in energy:
                entities[leaf] = {"entity_id": self._sensor_name(sn, leaf), "access": "r", "unit": "kWh"}
        if self.battery_capacity(sn) > 0:
            entities["soc_max"] = {"entity_id": self._sensor_name(sn, "battery_capacity"), "access": "r", "unit": "kWh"}
        if self.inverter_limit(sn) > 0:
            entities["inverter_limit"] = {"entity_id": self._sensor_name(sn, "inverter_limit"), "access": "r", "unit": "W"}
        if self.battery_rate_max(sn) > 0:
            entities["battery_rate_max"] = {"entity_id": self._sensor_name(sn, "battery_rate_max"), "access": "r", "unit": "W"}

        entities["reserve"] = {"entity_id": self._control_name("number", sn, "battery_schedule_reserve"), "access": "rw", "unit": "%"}
        for direction, prefix in (("charge", "charge"), ("export", "discharge")):
            entities["{}_start_time".format(prefix)] = {"entity_id": self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["{}_end_time".format(prefix)] = {"entity_id": self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), "access": "rw", "domain": "select", "format": "HH:MM:SS"}
            entities["scheduled_{}_enable".format(prefix)] = {"entity_id": self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), "access": "rw", "domain": "switch"}
        entities["charge_limit"] = {"entity_id": self._control_name("number", sn, "battery_schedule_charge_soc"), "access": "rw", "unit": "%"}
        entities["charge_rate"] = {"entity_id": self._control_name("number", sn, "battery_schedule_charge_power"), "access": "rw", "unit": "W", "step": 100}
        entities["discharge_target_soc"] = {"entity_id": self._control_name("number", sn, "battery_schedule_export_soc"), "access": "rw", "unit": "%"}
        entities["discharge_rate"] = {"entity_id": self._control_name("number", sn, "battery_schedule_export_power"), "access": "rw", "unit": "W", "step": 100}
        entities["schedule_write_button"] = {"entity_id": self._control_name("switch", sn, "battery_schedule_charge_write"), "access": "rw", "domain": "switch"}
        return entities

    def build_discovery(self):
        """
        Describe the discovered AlphaESS systems for the discovery catalogue.

        Reads only what get_device_list() already holds - self.device_list and self.device_detail -
        plus the EV-charger verdict _apply_live_payload() records and the energy counters already
        read, so this adds no API calls and cannot change what AlphaESS does. Reporting is
        independent of self.automatic: the catalogue records the hardware, and the report's own
        "automatic" flag says whether Predbat wired apps.yaml to it.

        self.device_list holds serial strings (sysSn), and only systems that passed has_battery()
        at discovery: a battery-less system (the VT1000 family, cobat 0 or missing) is dropped in
        get_device_list() and never reaches device_detail. automatic_config() applies no further
        test - it registers every serial in device_list as "AlphaESSCloud" - so inverter_type is set
        on every record, mirroring automatic_config() as the source of truth.

        capabilities is the ALPHAESS_CAPABILITIES constant and entities is _discovery_entities(sn):
        together they rebuild the AlphaESSCloud INVERTER_DEF row, and entities holds every setting
        automatic_config() binds for the system.

        Ratings are getEssList's own figures under their Predbat setting names: poinv (kW) as
        inverter_limit (W) via inverter_limit(), cobat (kWh) as soc_max via battery_capacity(), and
        popv (kW) as the descriptive pv_w. cobat is one scalar per system, so there is no list of
        entries to mis-sum. A system reports "solar" only when popv says PV is attached.

        Deliberately not reported:
        - export_limit: AlphaESS reports no export power limit, and poinv is the inverter rating,
          not the grid connection's cap (automatic_config() warns about this).
        - battery_rate_max as a rating: the API reports no battery power limit; battery_rate_max()
          is poinv or the user's override, so only its entity binding is described.
        - emsStatus: a status that changes. refresh_discovery() compares whole reports, so a
          changing value would re-file the report every time it moved.
        - usCapacity and surplusCobat: they fit "current SoC" and "configured usable depth" equally
          well, which is why publish_data() never maps them to SoC; reporting either as a rating
          would assert a meaning nobody knows.
        - firmware: the AlphaESS Open API exposes none.
        - account_ids: the API has no station, plant or site identifier; every endpoint is keyed by
          sysSn alone.
        - an AC-coupled verdict: detect_ac_coupled() infers it from live telemetry and
          ALPHAESS_AC_COUPLED_MODELS ships empty on purpose, so there is no held fact to report.

        An EV charger is reported as a flag only when _ev_present says True. That verdict comes from
        live telemetry alone, so a charger not yet seen reports nothing either way rather than a
        guess. No chargers record is invented for it: AlphaESS reports a charger's power, not its
        identity.

        Returns None when no system has been discovered yet, which refresh_discovery() treats as
        "nothing to report, ask again next cycle".
        """
        if not self.device_list:
            return None

        inverters = []
        for sn in self.device_list:
            detail = self.device_detail.get(sn, {}) or {}
            pv_kw = self._as_float(detail.get("popv"), 0.0)

            functions = ["solar", "battery"] if pv_kw > 0 else ["battery"]

            info = {}
            if detail.get("minv"):
                info["model"] = str(detail["minv"])
            if detail.get("mbat"):
                info["battery_model"] = str(detail["mbat"])

            ratings = {}
            inverter_w = self.inverter_limit(sn)
            if inverter_w > 0:
                ratings["inverter_limit"] = inverter_w
            if pv_kw > 0:
                ratings["pv_w"] = pv_kw * 1000.0
            battery_kwh = self.battery_capacity(sn)
            if battery_kwh > 0:
                ratings["soc_max"] = battery_kwh

            flags = ["ev_charger"] if self._ev_present.get(sn) is True else []

            inverters.append(
                inverter_record(
                    "alphaess:{}".format(sn),
                    inverter_type="AlphaESSCloud",
                    composition="direct",
                    functions=functions,
                    capabilities=dict(ALPHAESS_CAPABILITIES),
                    flags=flags,
                    hardware_ids={"serial": sn},
                    info=info,
                    ratings=ratings,
                    entities=self._discovery_entities(sn),
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test alphaess_api --test alphaess_publish --test alphaess_config --test alphaess_control --test alphaess_storage --test alphaess_const --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test components > /tmp/alphaess.log 2>&1; grep -n "FAILED\|EXCEPTION\|Traceback" /tmp/alphaess.log | head; grep -n "PASSED\|All tests" /tmp/alphaess.log`
Expected: no FAILED/EXCEPTION/Traceback lines; eleven `**** Test <name> PASSED` lines and `**** All tests passed`.

Then run `./run_pre_commit` (or `pre-commit run --files apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py`). Expected: all hooks pass.

- [ ] **Step 6: Commit**

Run `detect_changes()` first; expected: only `alphaess.py` and `tests/test_alphaess_api.py`, risk low.

```bash
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py
git commit -m "feat(discovery): AlphaESS record states capabilities, entities and Predbat-named ratings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Fox reporter

**Files:**
- Modify: `apps/predbat/fox.py` (new module constant `FOX_CAPABILITIES`; `FoxAPI.build_discovery`; new helpers `FoxAPI._device_setting`, `FoxAPI._setting_number`, `FoxAPI._fox_entity`, `FoxAPI._discovery_pv_entities`, `FoxAPI._discovery_entities`)
- Test: `apps/predbat/tests/test_fox_api.py`

**Interfaces:**
- Consumes: `inverter_record` (unchanged signature, `capabilities` now a dict, `entities` new here) and `ComponentBase.WRITE_AND_POLL_SLEEP` (Fox keeps the default 2) from Task 1; `validated_inverters`, `assert_definition_complete`, `capture_automatic_config`, `assert_record_agrees` (with `SITE_SETTINGS` checked at index 0 only) and `assert_record_binds_nothing_extra` from Task 2.
- Produces:
  - `fox.FOX_CAPABILITIES` - literal dict of the seven `CAPABILITY_KEYS`, values of `INVERTER_DEF["FoxCloud"]`.
  - Each driven Fox record (`inverter_type: "FoxCloud"`) carries `capabilities` and `entities`: 28 settings, plus `battery_temperature_history` on the first driven record only (D15). `grid_power_invert` is `invert: True` on `grid_power`. `fox_automatic_ignore_pv` no longer removes `pv_power`/`pv_today` (D11).
  - A PV-only device (hasPV, no battery - the devices `automatic_config()` takes as PV sources) carries only `pv_power` and `pv_today` (`access: r`, its `pvpower` / `pvenergytotal_today` sensors), with no `inverter_type` and no `capabilities` (D12). A battery device `automatic_config()` refuses, or one whose detail has not been read, carries no `entities` and no `capabilities`: `automatic_config()` binds nothing for either.
  - Ratings (D1, D14 - all device-reported): `inverter_limit` (was `inverter_w`, via `capacity_watts()`), `export_limit` / `import_limit` (the ExportLimit / ImportLimit setting's value in W when it is a number). `battery_capacity_entries` / `battery_capacity_serials` unchanged. Capability tokens `schedule` and `export_limit` removed. Flag `third_party_gen` unchanged.

Fox binds no setting `inverter.py` dummies for `FoxCloud` (`has_idle_time`, `has_timed_pause` and both GE mode flags are False, and `automatic_config()` binds none of `idle_*`, `pause_mode`, `inverter_mode`), so nothing is left out of `entities` on that account. `automatic_config()`'s PV lists are built over PV devices rather than driven ones (spec section 3, "Found while prototyping"); the record does not model that misalignment - it states each device's own PV.

- [ ] **Step 1: Run impact analysis**

Run `impact({target: "build_discovery", direction: "upstream", file_path: "apps/predbat/fox.py"})`. Expected: LOW - its only production caller is `ComponentBase.refresh_discovery()` (looked up by name), plus the discovery tests in `test_fox_api.py`. `automatic_config()` is not touched. (If the index predates `build_discovery`, the tool reports "not found"; confirm by `grep -n "build_discovery()" apps/predbat/*.py`.)

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_fox_api.py`, extend the `from fox import (...)` list with `FOX_CAPABILITIES,` (after `FOX_CLI_OAUTH_KEYS,`) and add below it:

```python
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
```

Update the existing discovery tests for the new vocabulary:

- `test_fox_build_discovery_describes_each_device`: replace
  ```python
    assert battery["ratings"]["inverter_w"] == 8000.0
    assert sorted(battery["capabilities"]) == ["export_limit", "schedule"], "schedule is the spec's token for a device-side scheduler - not scheduler"
  ```
  with
  ```python
    assert battery["ratings"]["inverter_limit"] == 8000.0
    assert battery["ratings"]["export_limit"] == 12000.0, "the configured ExportLimit, in W"
    assert battery["capabilities"] == FOX_CAPABILITIES, battery["capabilities"]
  ```
  and change `pv["ratings"]["inverter_w"] == 5000.0` to `pv["ratings"]["inverter_limit"] == 5000.0` and `battery["ratings"]["inverter_w"] == 10500.0` to `battery["ratings"]["inverter_limit"] == 10500.0` (the message is unchanged).
- `test_fox_build_discovery_round_trips_through_validate_report`: replace the `set(battery)` and `set(battery["ratings"])` assertions with
  ```python
    assert set(battery) == {"device_id", "inverter_type", "composition", "functions", "capabilities", "flags", "hardware_ids", "info", "ratings", "entities"}, set(battery)
    assert set(battery["info"]) == {"model", "product_type", "firmware"}, battery["info"]
    assert set(battery["ratings"]) == {"inverter_limit", "export_limit", "battery_capacity_entries", "battery_capacity_serials"}, battery["ratings"]
    assert battery["entities"]["pv_power"]["entity_id"] == "sensor.predbat_fox_batt001_pvpower" and battery["entities"]["export_limit"]["access"] == "r", battery["entities"]
    assert by_id["fox:AIO0001"]["entities"]["export_limit"] == {"value": 99999, "access": "r"}, "no ExportLimit setting: automatic_config()'s 99999 stand-in"
  ```
- `test_fox_build_discovery_survives_a_capacity_that_is_not_a_number`: change both `"inverter_w"` references to `"inverter_limit"`.
- Replace `test_fox_build_discovery_matches_export_limit_as_automatic_config_does` whole:

```python
def test_fox_build_discovery_matches_export_limit_as_automatic_config_does(my_predbat):
    """The export_limit binding and rating follow the same case-insensitive ExportLimit match automatic_config() uses."""
    print("**** test_fox_build_discovery_matches_export_limit_as_automatic_config_does ****")
    for name in ("ExportLimit", "exportlimit", "EXPORTLIMIT"):
        fox = _fox_discovery_api(my_predbat)
        fox.device_settings["BATT001"] = {name: {"value": 12000.0}}
        battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
        assert battery["entities"]["export_limit"] == {"entity_id": "number.predbat_fox_batt001_setting_exportlimit", "access": "r"}, f"setting {name!r}: {battery['entities'].get('export_limit')}"
        assert battery["ratings"]["export_limit"] == 12000.0, f"setting {name!r}: {battery['ratings']}"

    # The setting is present but carries no number (never read, or read as text): automatic_config()
    # still binds the entity, but there is no configured figure to report
    fox = _fox_discovery_api(my_predbat)
    fox.device_settings["BATT001"] = {"ExportLimit": {"unit": "W"}}
    battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
    assert "entity_id" in battery["entities"]["export_limit"] and "export_limit" not in battery["ratings"], battery

    fox = _fox_discovery_api(my_predbat)
    fox.device_settings["BATT001"] = {"WorkMode": {"value": "SelfUse"}}
    battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
    assert battery["entities"]["export_limit"] == {"value": 99999, "access": "r"}, "no ExportLimit setting: automatic_config()'s 99999 stand-in, not an entity"
    assert "export_limit" not in battery["ratings"], "no ExportLimit setting, no configured export cap"
    print("PASS: Fox matches ExportLimit as automatic_config() does")
    return 0
```

Add after it (before `test_fox_build_discovery_returns_none_before_discovery`):

```python
def test_fox_build_discovery_reports_the_configured_import_limit(my_predbat):
    """import_limit is the device's ImportLimit setting in W - a figure the device reports, so a rating only when it holds a number."""
    print("**** test_fox_build_discovery_reports_the_configured_import_limit ****")
    for name in ("ImportLimit", "importlimit"):
        fox = _fox_discovery_api(my_predbat)
        fox.device_settings["BATT001"][name] = {"value": 9000.0, "unit": "W"}
        battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
        assert battery["ratings"]["import_limit"] == 9000.0, f"setting {name!r}: {battery['ratings']}"
        assert "import_limit" not in battery["entities"], "automatic_config() binds no import_limit, so the record binds none"

    fox = _fox_discovery_api(my_predbat)
    fox.device_settings["BATT001"]["ImportLimit"] = {"value": "unlimited"}
    battery = {record["device_id"]: record for record in fox.build_discovery()["inverters"]}["fox:BATT001"]
    assert "import_limit" not in battery["ratings"], "a value that is not a number is not a rating"
    assert "import_limit" not in {record["device_id"]: record for record in _fox_discovery_api(my_predbat).build_discovery()["inverters"]}["fox:BATT001"]["ratings"], "no setting, no rating"
    print("PASS: Fox reports the configured import limit")
    return 0


def _fox_driven_api(my_predbat, settings=None, **detail):
    """A FoxAPI carrying only BATT001 from _fox_discovery_devices() - one driven inverter - with detail fields (and optionally its settings) replaced."""
    fox = _fox_discovery_api(my_predbat)
    fox.device_list = fox.device_list[:1]
    fox.device_detail = {"BATT001": dict(fox.device_detail["BATT001"], **detail)}
    fox.device_settings = {"BATT001": fox.device_settings["BATT001"] if settings is None else settings}
    return fox


def test_fox_build_discovery_record_rebuilds_the_foxcloud_row(my_predbat):
    """Completeness: the driven device's record alone rebuilds INVERTER_DEF["FoxCloud"] - no row as a base."""
    print("**** test_fox_build_discovery_record_rebuilds_the_foxcloud_row ****")
    records = validated_inverters(_fox_driven_api(my_predbat).build_discovery())
    driven = [record for record in records if record.get("inverter_type")]
    assert len(driven) == 1, records
    for record in driven:
        assert_definition_complete(record, FoxAPI.WRITE_AND_POLL_SLEEP)
    print("PASS: Fox's record rebuilds the FoxCloud row")
    return 0


def test_fox_build_discovery_record_agrees_with_automatic_config(my_predbat):
    """Agreement both ways: the record binds exactly what the real automatic_config() binds for the device.

    Run over each shape automatic_config() treats differently: an ExportLimit setting or the 99999
    stand-in, the device's own PV, no PV at all (the [0] stand-ins), a metered third-party generator
    with and without the device's own PV, and fox_automatic_ignore_pv. The last is the user's opt-out,
    not a fact about the device (spec D11): automatic_config() binds no PV, while the record still
    carries it, so pv_power and pv_today are the only settings allowed beyond what was bound.
    """
    print("**** test_fox_build_discovery_record_agrees_with_automatic_config ****")
    cases = {
        "own pv and an export limit": ({}, None, False),
        "no ExportLimit setting": ({}, {"WorkMode": {"value": "SelfUse"}}, False),
        "no pv": ({"hasPV": False}, None, False),
        "third-party generator, no own pv": ({"hasPV": False, "thirdPartyGen": True}, None, False),
        "third-party generator and own pv": ({"thirdPartyGen": True}, None, False),
        "pv ignored": ({}, None, True),
    }
    for name, (detail, settings, ignore_pv) in cases.items():
        fox = _fox_driven_api(my_predbat, settings=settings, **detail)
        fox.automatic_ignore_pv = ignore_pv
        record = validated_inverters(fox.build_discovery())[0]
        captured = capture_automatic_config(fox)
        try:
            assert_record_agrees(record, captured, index=0)
            assert_record_binds_nothing_extra(record, captured, index=0, allowed_extra=("pv_power", "pv_today") if ignore_pv else ())
        except AssertionError as error:
            raise AssertionError(f"{name}: {error}")
        entities = record["entities"]
        if name == "no pv":
            assert entities["pv_power"] == {"value": 0, "access": "r"} and entities["pv_today"] == {"value": 0, "access": "r"}, entities
        if name == "third-party generator, no own pv":
            assert entities["pv_power"]["entity_id"] == "sensor.predbat_fox_batt001_meterpower2", entities
        if name == "pv ignored":
            assert "pv_power" not in captured and "pv_today" not in captured, "automatic_config() binds no PV setting when told to ignore PV"
            assert entities["pv_power"]["entity_id"] == "sensor.predbat_fox_batt001_pvpower", "the record still describes the device's PV"
    print("PASS: Fox's record agrees with automatic_config()")
    return 0


def test_fox_build_discovery_two_inverters_get_their_own_entities(my_predbat):
    """Two driven inverters give two records whose entity ids differ, each at its own index in automatic_config()'s lists.

    automatic_config() builds its per-device lists over the devices it drives, not over device_list,
    so a PV-only device listed first must not shift the index. battery_temperature_history is the one
    site-wide setting: a single entity, the first driven device's sensor, so it sits on that record only
    (spec D15) - the shared agreement check expects it at index 0 only.
    """
    print("**** test_fox_build_discovery_two_inverters_get_their_own_entities ****")
    device_list, device_detail, device_settings = _fox_discovery_devices()
    fox = _fox_discovery_api(my_predbat)
    fox.device_list = [device_list[1], device_list[0], {"deviceSN": "BATT002"}]
    fox.device_detail["BATT002"] = dict(device_detail["BATT001"], deviceSN="BATT002")
    fox.device_settings["BATT002"] = {}

    records = validated_inverters(fox.build_discovery())
    driven = [record for record in records if record.get("inverter_type")]
    assert [record["device_id"] for record in driven] == ["fox:BATT001", "fox:BATT002"], records
    entity_ids = [{descriptor["entity_id"] for descriptor in record["entities"].values() if "entity_id" in descriptor} for record in driven]
    assert entity_ids[0] and entity_ids[1] and not entity_ids[0] & entity_ids[1], f"entity ids shared between inverters: {entity_ids[0] & entity_ids[1]}"

    captured = capture_automatic_config(fox)
    for index, record in enumerate(driven):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    assert "battery_temperature_history" in driven[0]["entities"] and "battery_temperature_history" not in driven[1]["entities"]
    print("PASS: two Fox inverters get their own entities")
    return 0


def test_fox_build_discovery_pv_only_device_carries_its_pv(my_predbat):
    """A PV-only device's record carries its pv_power and pv_today - the entities automatic_config() binds for it - and nothing else (spec D12).

    automatic_config() binds a PV-only device's sensors when the battery inverters do not see the PV
    themselves; here BATT001 has no PV of its own, so PVONLY1's sensors are the ones bound. The record
    is still not an inverter Predbat drives: no inverter_type, no capabilities.
    """
    print("**** test_fox_build_discovery_pv_only_device_carries_its_pv ****")
    fox = _fox_discovery_api(my_predbat)
    fox.device_detail["BATT001"]["hasPV"] = False
    pv = {record["device_id"]: record for record in validated_inverters(fox.build_discovery())}["fox:PVONLY1"]
    captured = capture_automatic_config(fox)

    assert "inverter_type" not in pv and "capabilities" not in pv, pv
    assert pv["entities"] == {
        "pv_power": {"entity_id": "sensor.predbat_fox_pvonly1_pvpower", "access": "r"},
        "pv_today": {"entity_id": "sensor.predbat_fox_pvonly1_pvenergytotal_today", "access": "r"},
    }, pv["entities"]
    assert pv["entities"]["pv_power"]["entity_id"] in captured["pv_power"] and pv["entities"]["pv_today"]["entity_id"] in captured["pv_today"], captured
    print("PASS: a Fox PV-only device carries its PV")
    return 0


def test_fox_build_discovery_claims_no_controls_for_a_device_it_does_not_drive(my_predbat):
    """A device Fox does not drive claims no controls: no capabilities, and entities only for what automatic_config() can bind for it.

    A PV-only device carries just its PV (spec D12). A device whose detail has not been read yet, and a
    battery inverter automatic_config() refuses (no scheduler), carry no entities at all:
    automatic_config() binds nothing for either - a refused battery device is never a PV source to it,
    whatever its hasPV says.
    """
    print("**** test_fox_build_discovery_claims_no_controls_for_a_device_it_does_not_drive ****")
    _, device_detail, device_settings = _fox_discovery_devices()
    fox = _fox_discovery_api(my_predbat)
    fox.device_list += [{"deviceSN": "UNREAD1"}, {"deviceSN": "REFUSED1"}]
    fox.device_detail["REFUSED1"] = dict(device_detail["BATT001"], deviceSN="REFUSED1", function={"scheduler": False})
    fox.device_settings["REFUSED1"] = dict(device_settings["BATT001"])

    by_id = {record["device_id"]: record for record in validated_inverters(fox.build_discovery())}
    for device_id in ("fox:PVONLY1", "fox:UNREAD1", "fox:REFUSED1"):
        record = by_id[device_id]
        assert "inverter_type" not in record and "capabilities" not in record, f"{device_id} is not driven, so it claims no controls: {record}"
    assert set(by_id["fox:PVONLY1"]["entities"]) == {"pv_power", "pv_today"}, by_id["fox:PVONLY1"]
    assert "entities" not in by_id["fox:UNREAD1"] and "entities" not in by_id["fox:REFUSED1"], (by_id["fox:UNREAD1"], by_id["fox:REFUSED1"])
    assert by_id["fox:BATT001"]["capabilities"] == FOX_CAPABILITIES and by_id["fox:BATT001"]["entities"], by_id["fox:BATT001"]
    print("PASS: Fox claims no controls for a device it does not drive")
    return 0
```

Register them in `run_fox_api_tests()` after `test_fox_build_discovery_matches_export_limit_as_automatic_config_does`:

```python
        failed |= test_fox_build_discovery_reports_the_configured_import_limit(my_predbat)
        failed |= test_fox_build_discovery_record_rebuilds_the_foxcloud_row(my_predbat)
        failed |= test_fox_build_discovery_record_agrees_with_automatic_config(my_predbat)
        failed |= test_fox_build_discovery_two_inverters_get_their_own_entities(my_predbat)
        failed |= test_fox_build_discovery_pv_only_device_carries_its_pv(my_predbat)
        failed |= test_fox_build_discovery_claims_no_controls_for_a_device_it_does_not_drive(my_predbat)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test fox_api > /tmp/fox_api.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/fox_api.log | head`
Expected: `ImportError: cannot import name 'FOX_CAPABILITIES' from 'fox'`.

- [ ] **Step 4: Implement in `apps/predbat/fox.py`**

Add the constant just above `V3_EXTRA_PARAM_KEYS` (its comment block starts `# Group fields that the v3 scheduler API nests inside 'extraParam'`):

```python
# The behaviour a driven Fox Cloud inverter has, as the discovery record's capabilities - the seven
# coordinator.CAPABILITY_KEYS, with the values INVERTER_DEF["FoxCloud"] (config.py) holds. A literal,
# never read back from the row: the record has to rebuild the row on its own, and reading the row here
# would make that test prove nothing.
FOX_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": True,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}
```

Replace `FoxAPI.build_discovery()` whole, and add the helpers directly above it (after `automatic_config()`, which is unchanged):

```python
    @staticmethod
    def _device_setting(settings, name):
        """
        The device's setting entry called name, or None. Matched case-insensitively, as
        automatic_config() matches ExportLimit when it sets hasExportLimit.
        """
        for key, entry in settings.items():
            if str(key).lower() == name.lower():
                return entry if isinstance(entry, dict) else {}
        return None

    @staticmethod
    def _setting_number(entry):
        """A setting entry's value when it is a number of watts Fox reports (not negative, not a bool), else None."""
        value = (entry or {}).get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def _fox_entity(self, serial, entity_domain, suffix, access="r", **extra):
        """
        One discovery descriptor for an entity Fox publishes for this device, its id formed exactly as
        automatic_config() forms its per-device list element (f"<domain>.{self.prefix}_fox_{sn.lower()}_<suffix>").
        extra holds descriptor fields (domain, format, unit, invert).
        """
        return dict({"entity_id": f"{entity_domain}.{self.prefix}_fox_{serial.lower()}_{suffix}", "access": access}, **extra)

    def _discovery_pv_entities(self, serial, has_pv, third_party):
        """
        pv_today and pv_power for one device, as automatic_config() binds them for it: the device's own
        PV sensors when it has PV, otherwise a metered third-party generator's, otherwise 0 - the [0]
        stand-in automatic_config() sets when nothing on the site has PV.

        fox_automatic_ignore_pv is the user's opt-out, not a fact about the device, so it does not
        remove them (spec D11); the coordinator applies it.
        """
        if has_pv:
            return {"pv_today": self._fox_entity(serial, "sensor", "pvenergytotal_today"), "pv_power": self._fox_entity(serial, "sensor", "pvpower")}
        if third_party:
            return {"pv_today": self._fox_entity(serial, "sensor", "feedin2"), "pv_power": self._fox_entity(serial, "sensor", "meterpower2")}
        return {"pv_today": {"value": 0, "access": "r"}, "pv_power": {"value": 0, "access": "r"}}

    def _discovery_entities(self, serial, has_pv, third_party, has_export_limit, first):
        """
        The discovery record's entities for one inverter Fox drives: one descriptor per setting
        automatic_config() binds for it, each entity id formed as automatic_config() forms it (see
        _fox_entity()), so the agreement tests that run the real automatic_config() pin the two together.

        access is "rw" for the settings Predbat writes (the schedule, reserve and the write
        button) and "r" for what it only reads. grid_power_invert becomes invert on grid_power.
        export_limit is automatic_config()'s 99999 ("no cap") stand-in when the device has no
        ExportLimit setting. PV is _discovery_pv_entities().

        battery_temperature_history is a single site-wide entity automatic_config() binds to the
        first driven device's sensor, so only that device's record (first) carries it (spec D15).
        """
        time_select = {"domain": "select", "format": "HH:MM:SS"}
        entities = {
            "load_today": self._fox_entity(serial, "sensor", "loads"),
            "import_today": self._fox_entity(serial, "sensor", "gridconsumption"),
            "export_today": self._fox_entity(serial, "sensor", "feedin"),
            "battery_rate_max": self._fox_entity(serial, "sensor", "battery_rate_max"),
            "battery_power": self._fox_entity(serial, "sensor", "invbatpower"),
            "grid_power": self._fox_entity(serial, "sensor", "meterpower", invert=True),
            "load_power": self._fox_entity(serial, "sensor", "loadspower"),
            "soc_percent": self._fox_entity(serial, "sensor", "soc", unit="%"),
            "soc_max": self._fox_entity(serial, "sensor", "battery_capacity"),
            "reserve": self._fox_entity(serial, "number", "battery_schedule_reserve", "rw"),
            "battery_min_soc": self._fox_entity(serial, "sensor", "battery_reserve_min"),
            "charge_start_time": self._fox_entity(serial, "select", "battery_schedule_charge_start_time", "rw", **time_select),
            "charge_end_time": self._fox_entity(serial, "select", "battery_schedule_charge_end_time", "rw", **time_select),
            "charge_limit": self._fox_entity(serial, "number", "battery_schedule_charge_soc", "rw"),
            "scheduled_charge_enable": self._fox_entity(serial, "switch", "battery_schedule_charge_enable", "rw"),
            "charge_rate": self._fox_entity(serial, "number", "battery_schedule_charge_power", "rw", unit="W"),
            "scheduled_discharge_enable": self._fox_entity(serial, "switch", "battery_schedule_discharge_enable", "rw"),
            "discharge_target_soc": self._fox_entity(serial, "number", "battery_schedule_discharge_soc", "rw"),
            "discharge_start_time": self._fox_entity(serial, "select", "battery_schedule_discharge_start_time", "rw", **time_select),
            "discharge_end_time": self._fox_entity(serial, "select", "battery_schedule_discharge_end_time", "rw", **time_select),
            "discharge_rate": self._fox_entity(serial, "number", "battery_schedule_discharge_power", "rw", unit="W"),
            "battery_temperature": self._fox_entity(serial, "sensor", "battemperature"),
            "inverter_limit": self._fox_entity(serial, "sensor", "inverter_capacity"),
            "battery_scaling": self._fox_entity(serial, "sensor", "battery_soh"),
            "schedule_write_button": self._fox_entity(serial, "switch", "battery_schedule_charge_write", "rw"),
            "export_limit": self._fox_entity(serial, "number", "setting_exportlimit") if has_export_limit else {"value": 99999, "access": "r"},
        }
        entities.update(self._discovery_pv_entities(serial, has_pv, third_party))
        if first:
            entities["battery_temperature_history"] = self._fox_entity(serial, "sensor", "battemperature")
        return entities

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

        Only a driven device carries capabilities (FOX_CAPABILITIES, the FoxCloud row's behaviour)
        and its full entities (what automatic_config() binds for it - see _discovery_entities()).
        automatic_config() builds its per-device lists over the driven devices in device_list
        order, so the Nth driven record here is index N of those lists. A PV-only device (hasPV,
        no battery - the devices automatic_config() takes as PV sources) carries just its
        pv_power and pv_today (spec D12), so its generation is not lost when the coordinator
        configures from records. Any other device - a battery automatic_config() refuses, or one
        whose detail has not been read yet - claims no entities.

        A third-party generator this inverter meters is topology, not something the inverter can
        do, so it is a flag.

        ratings are figures the device reports, keyed by Predbat setting name. inverter_limit goes
        through capacity_watts(), never capacity * 1000: Fox reports a half-kW model's capacity
        truncated (a KH10.5 says 10), and capacity_watts() is what restores the 500 W. For a
        battery device that is also the value of the _inverter_capacity sensor publish_data()
        publishes; for a PV-only device publish_data() sets that sensor to 0, while the catalogue
        still reports the device's own rating. export_limit and import_limit are the ExportLimit
        and ImportLimit settings' configured values in W, when the device has the setting and it
        holds a number.

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
        serial, so the sum is 4x the truth. Publishing it would put a knowingly-wrong soc_max
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
        driven_count = 0
        for device in self.device_list:
            serial = device.get("deviceSN")
            if not serial:
                continue
            detail = self.device_detail.get(serial, {}) or {}
            has_battery = bool(detail.get("hasBattery", False))
            has_pv = bool(detail.get("hasPV", False))
            has_scheduler = bool((detail.get("function") or {}).get("scheduler", False))
            third_party = bool(detail.get("thirdPartyGen", False))

            functions = []
            if has_pv:
                functions.append("solar")
            if has_battery:
                functions.append("battery")

            flags = []
            if third_party:
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

            # capacity_watts() multiplies the raw field, so only a positive number reaches it -
            # the same test drives_it applies below. Anything else drops the rating, not the report.
            capacity = detail.get("capacity", 0)
            capacity_is_rating = isinstance(capacity, (int, float)) and capacity > 0
            ratings = {}
            if capacity_is_rating:
                ratings["inverter_limit"] = self.capacity_watts(detail)
            settings = self.device_settings.get(serial, {}) or {}
            export_setting = self._device_setting(settings, "ExportLimit")
            for rating, entry in (("export_limit", export_setting), ("import_limit", self._device_setting(settings, "ImportLimit"))):
                value = self._setting_number(entry)
                if value is not None:
                    ratings[rating] = value
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
            drives_it = has_battery and has_scheduler and capacity_is_rating
            entities = None
            if drives_it:
                entities = self._discovery_entities(serial, has_pv, third_party, export_setting is not None, driven_count == 0)
                driven_count += 1
            elif has_pv and not has_battery:
                entities = self._discovery_pv_entities(serial, has_pv, third_party)

            inverters.append(
                inverter_record(
                    "fox:{}".format(serial),
                    inverter_type="FoxCloud" if drives_it else None,
                    composition="direct",
                    functions=functions,
                    capabilities=FOX_CAPABILITIES if drives_it else None,
                    flags=flags,
                    hardware_ids={"serial": serial},
                    info=info,
                    ratings=ratings,
                    entities=entities,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test fox_api --test fox_oauth --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test components > /tmp/fox.log 2>&1; grep -n "FAIL\|Traceback" /tmp/fox.log | head; tail -2 /tmp/fox.log`
Expected: no FAIL/Traceback lines; `**** All tests passed ****`. Then `pre-commit run --files apps/predbat/fox.py apps/predbat/tests/test_fox_api.py`: all hooks pass.

- [ ] **Step 6: Commit**

Run `detect_changes()` first: expected changes are `build_discovery`, the five new helpers and `FOX_CAPABILITIES` in `fox.py`, plus the Fox discovery tests - `automatic_config` must not appear.

```bash
git add apps/predbat/fox.py apps/predbat/tests/test_fox_api.py
git commit -m "feat(discovery): Fox record carries entities, capabilities dict and Predbat-named ratings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Solis Cloud reporter

**Files:**
- Modify: `apps/predbat/solis.py` (new module constant `SOLIS_CLOUD_CAPABILITIES`; new `SolisAPI._discovery_pv_entities()`, `SolisAPI._discovery_entities()` and `SolisAPI._discovery_export_limit()`; `SolisAPI.build_discovery()`)
- Test: `apps/predbat/tests/test_solis.py`

**Interfaces:**
- Consumes: `inverter_definition` (Task 1, `coordinator.py`); `ComponentBase.WRITE_AND_POLL_SLEEP` (Task 1, 2 - SolisCloud's row says 2, so no override); `validated_inverters`, `assert_definition_complete`, `capture_automatic_config`, `assert_record_agrees`, `assert_record_binds_nothing_extra` (Task 2, `tests/discovery_contract.py`).
- Produces (in `solis.py`):
  - `SOLIS_CLOUD_CAPABILITIES` - literal dict of the seven `CAPABILITY_KEYS`, matching `INVERTER_DEF["SolisCloud"]`.
  - `SolisAPI._discovery_pv_entities(sn) -> dict` - `pv_today` and `pv_power` (`access: r`), the ids `automatic_config()` binds over `pv_devices`.
  - `SolisAPI._discovery_entities(sn) -> dict` - one descriptor per setting `automatic_config()` binds for a battery inverter, minus `reserve` (spec D9) and `battery_power_invert` (folded into `battery_power.invert`).
  - `SolisAPI._discovery_export_limit(sn) -> float | None` - register 499 in W as `publish_entities()` presents it; `None` when unread or 0 ("no limit").
  - Solis records: `capabilities` dict, `entities`, flag `reports_soh`, and ratings `inverter_limit` (was `inverter_w`), `battery_min_soc` and `export_limit` (new). `battery_capacity_ah`/`battery_pack_count` keep their names. `battery_kwh` is removed with no `soc_max` replacement (D14). The `schedule`/`target_soc`/`discharge_target`/`charge_rate_power`/`soh` tokens are gone.

SolisCloud's specifics, settled by the spec or confirmed against the code:
- **`reserve` is absent (spec D9).** `automatic_config()` binds `reserve` and `battery_min_soc` to the same `number.<prefix>_solis_<sn>_over_discharge_soc` (`solis.py:1715-1716` on main), and the row's `has_reserve_soc: False` makes `inverter.py` dummy the `reserve` binding. The record carries `battery_min_soc` as an `access: r` entity plus a rating from register 158 when held, and derived `has_reserve_soc` is False.
- **`time_button_press: False`.** Solis binds no `schedule_write_button`, so none is emitted and the derived field is False.
- **`battery_power_invert`** is the string `"True"` in `automatic_config()`; it becomes `invert: True` on `battery_power`.
- **Two kinds of per-device list.** `automatic_config()` builds `pv_today`/`pv_power` over every discovered inverter (`pv_devices`, PV-only ones included - GH#4922) and every other list over the battery inverters it drives. One serial therefore sits at different indexes in the two: in `[PV001, BAT001]`, BAT001 is index 0 of `soc_percent` and index 1 of `pv_power`. The tests cut each list at the index that belongs to it (`_captured_for_device()`), so they do not depend on fixture order.
- **PV-only records (D12)** - any inverter whose detail has been read but which `automatic_config()` does not drive - carry `pv_today` and `pv_power` only, with no `inverter_type` or `capabilities`. **A not-yet-read inverter carries nothing** (Review Focus 2), although `automatic_config()` still lists its serial in `pv_devices`.
- **`solis_cloud_pv_load_ignore` (D11)** no longer changes the record: `load_today`/`pv_today`/`load_power`/`pv_power` stay, and the reverse check allows them as `allowed_extra`.
- **Only driven inverters** (`drives_it`, `automatic_config()`'s own predicate) carry `capabilities`, the full entity map, `reports_soh`, `battery_min_soc` and `export_limit`.
- **`export_limit`** is the configured cap in register 499 (`SOLIS_CID_MAX_EXPORT_POWER`, held in `cached_values`), converted as `publish_entities()` converts it for the `max_export_power` number (<200 means 100 W units). An unread or 0 register - which `publish_entities()` shows as 99999 W, "no limit" - gives no rating.
- **D14:** `battery_rate_max` is an entity only (the `max_charge_power` number is register current x `get_nominal_voltage()`). `soc_max` is not reported: a kWh capacity is register 172 x an inferred or user-configured voltage, and `automatic_config()` binds no `soc_max` entity either.

- [ ] **Step 1: Run impact analysis**

Run `impact({target: "build_discovery", direction: "upstream", file_path: "apps/predbat/solis.py"})`. Expected: the only caller is `ComponentBase.refresh_discovery()` (via `getattr`), plus the Solis tests - LOW risk. (If the index predates PR #5206 the symbol is not found; `grep -rn "build_discovery()" apps/predbat/*.py` shows the same.) The three `_discovery_*` helpers are new.

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_solis.py`, replace the import line `from coordinator import validate_report` with:

```python
from solis import SOLIS_CID_MAX_EXPORT_POWER, SOLIS_CLOUD_CAPABILITIES
from coordinator import inverter_definition, validate_report
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
```

Replace `test_solis_catalogue_describes_battery_and_pv_only` and `test_solis_catalogue_battery_ratings_carry_only_stated_facts` (old capability tokens, `soh`, `inverter_w`, `battery_kwh`) with:

```python
def test_solis_catalogue_describes_battery_and_pv_only():
    """A battery inverter is a SolisCloud inverter; a PV-only one reports solar with no inverter_type.

    A third serial carries no entry in inverter_details at all - not read yet, or a failed fetch -
    which automatic_config() retries rather than treats as settled; its record must claim neither
    "solar" nor any other function, rather than the misleading PV-only guess it would get if empty
    detail were treated the same as a confirmed no-battery detail (_DETAIL_NO_BATTERY, on PV001,
    which does have a read detail and must still get ["solar"]).
    """
    api = _solis_fleet()
    api.inverter_sn = api.inverter_sn + ["NODETAIL001"]
    report = api.build_discovery()
    by_id = {record["device_id"]: record for record in report["inverters"]}
    assert set(by_id) == {"solis:BAT001", "solis:PV001", "solis:NODETAIL001"}, sorted(by_id)
    battery = by_id["solis:BAT001"]
    assert battery["inverter_type"] == "SolisCloud"
    assert battery["composition"] == "direct"
    assert battery["functions"] == ["solar", "battery"]
    assert battery["hardware_ids"] == {"serial": "BAT001"}
    assert battery["info"] == {"model": "Solis-5G-Hybrid"}
    assert battery["capabilities"] == SOLIS_CLOUD_CAPABILITIES, battery["capabilities"]
    assert battery["flags"] == ["reports_soh", "tou_v2"], battery["flags"]
    pv = by_id["solis:PV001"]
    assert "inverter_type" not in pv, "a PV-only inverter is not one automatic_config() configures"
    assert pv["functions"] == ["solar"], "every Solis inverter feeds the PV totals (automatic_config's pv_devices)"
    assert "capabilities" not in pv, pv
    assert set(pv["entities"]) == {"pv_power", "pv_today"}, "a PV-only record carries only its PV sensors (spec D12)"
    no_detail = by_id["solis:NODETAIL001"]
    assert "functions" not in no_detail, "no detail read yet must not claim PV-only"
    assert "inverter_type" not in no_detail
    # Review Focus 2: a device whose detail has not been read claims no entities and no capabilities
    assert "capabilities" not in no_detail and "entities" not in no_detail, no_detail
    return False


def test_solis_catalogue_battery_ratings_carry_only_stated_facts():
    """battery_capacity_ah is the bank total (register 172 x pack count); no kWh rating, even with the voltage configured.

    The fixture is 100 Ah per pack (register 172) with 2 packs, so the bank total is 200.0 Ah -
    publish_entities() multiplies by parallel_battery_count the same way, so battery_capacity_ah
    means the same thing here as on every other reporter.

    Spec D14: a rating is a figure the device reports. A kWh capacity is Predbat's product of the
    register and a voltage - inferred, or the user's solis_nominal_voltage (GH#5090) - so it is not
    reported as soc_max whether or not the voltage is configured.
    """
    api = _solis_fleet()
    ratings = {r["device_id"]: r for r in api.build_discovery()["inverters"]}["solis:BAT001"]["ratings"]
    assert ratings == {"inverter_limit": 5000.0, "battery_capacity_ah": 200.0, "battery_pack_count": 2}, ratings
    api.nominal_pack_voltage = 51.2
    ratings = {r["device_id"]: r for r in api.build_discovery()["inverters"]}["solis:BAT001"]["ratings"]
    assert ratings == {"inverter_limit": 5000.0, "battery_capacity_ah": 200.0, "battery_pack_count": 2}, ratings
    return False
```

Add these after `test_solis_catalogue_round_trips_through_validate_report` (sync on purpose: `capture_automatic_config()` runs the async `automatic_config()` with `asyncio.run`, which cannot nest inside another `asyncio.run`):

```python
def _solis_battery_pair():
    """A MockSolisAPI holding two battery inverters, both with the captured _DETAIL_WITH_BATTERY (plan Review Focus 1)."""
    api = _solis_fleet()
    api.inverter_sn = ["BAT001", "BAT002"]
    api.inverter_details = {
        "BAT001": dict(_DETAIL_WITH_BATTERY, productModel="Solis-5G-Hybrid", power=5.0, powerStr="kW"),
        "BAT002": dict(_DETAIL_WITH_BATTERY, productModel="Solis-5G-Hybrid", power=3.6, powerStr="kW"),
    }
    api.cached_values = {sn: {SOLIS_CID_BATTERY_CAPACITY: "100", SOLIS_CID_TOU_V2_MODE: "43605"} for sn in api.inverter_sn}
    api.parallel_battery_count = {"BAT001": 2, "BAT002": 1}
    return api


# automatic_config() builds these lists over every inverter (pv_devices, PV-only ones included - GH#4922),
# and every other per-device list over the battery inverters it drives only.
SOLIS_PV_LIST_SETTINGS = ("pv_today", "pv_power")

# The settings solis_cloud_pv_load_ignore stops automatic_config() binding; the record keeps them (spec D11).
SOLIS_PV_LOAD_IGNORED = ("load_today", "pv_today", "load_power", "pv_power")


def _captured_for_device(captured, battery_index, pv_index):
    """automatic_config()'s bindings for one inverter, each list cut down to that inverter's own entry.

    One serial sits at different positions in automatic_config()'s two kinds of list: in a fleet of
    [PV001, BAT001], BAT001 is index 0 of soc_percent but index 1 of pv_power. Each list is picked at the
    index that belongs to it and returned as a one-entry list, so the shared contract checks run at index
    0 without depending on how the fixture orders its inverters. battery_index is None for an inverter
    automatic_config() does not drive, whose battery lists then come back empty (nothing bound).
    """
    device = {}
    for setting, value in captured.items():
        if not isinstance(value, list):
            device[setting] = value
            continue
        index = pv_index if setting in SOLIS_PV_LIST_SETTINGS else battery_index
        device[setting] = [value[index]] if index is not None and index < len(value) else []
    return device


def _assert_solis_fleet_agrees(api, allowed_extra=()):
    """Every record agrees with automatic_config() in both directions, each at its own index in each kind of list.

    A driven record runs both shared checks. A PV-only record has no inverter_type for them to key on,
    so it is compared directly: exactly pv_today and pv_power, read-only, with the ids automatic_config()
    binds at its pv_devices position (spec D12). A record whose detail has not been read carries no
    entities, although automatic_config() still lists its serial in pv_devices (Review Focus 2).
    Returns the records by serial.
    """
    records = validated_inverters(api.build_discovery())
    captured = capture_automatic_config(api)
    by_serial = {record["hardware_ids"]["serial"]: record for record in records}
    assert list(by_serial) == list(api.inverter_sn), (list(by_serial), api.inverter_sn)
    driven = [sn for sn in api.inverter_sn if by_serial[sn].get("inverter_type")]
    for sn in api.inverter_sn:
        record = by_serial[sn]
        pv_index = api.inverter_sn.index(sn)
        battery_index = driven.index(sn) if sn in driven else None
        device = _captured_for_device(captured, battery_index, pv_index)
        if battery_index is not None:
            assert_record_agrees(record, device, index=0)
            assert_record_binds_nothing_extra(record, device, index=0, allowed_extra=allowed_extra)
        elif "functions" not in record:
            assert "entities" not in record and "capabilities" not in record, record
        else:
            assert "inverter_type" not in record and "capabilities" not in record, record
            expected = {setting: {"entity_id": "sensor.{}_solis_{}_{}".format(api.prefix, sn.lower(), "pv_energy_total" if setting == "pv_today" else "pv_power"), "access": "r"} for setting in SOLIS_PV_LIST_SETTINGS}
            assert record["entities"] == expected, record["entities"]
            for setting in SOLIS_PV_LIST_SETTINGS:
                if setting not in allowed_extra:
                    assert device[setting] == [expected[setting]["entity_id"]], (setting, device[setting])
    return by_serial


def test_solis_catalogue_record_rebuilds_the_row():
    """The driven inverter's record alone rebuilds INVERTER_DEF["SolisCloud"], with no row as a base (spec section 2)."""
    api = _solis_fleet()
    checked = 0
    for record in validated_inverters(api.build_discovery()):
        if record.get("inverter_type"):
            assert_definition_complete(record, SolisAPI.WRITE_AND_POLL_SLEEP)
            checked += 1
    assert checked == 1, "expected exactly one driven SolisCloud record, checked {}".format(checked)
    return False


def test_solis_catalogue_record_agrees_with_automatic_config():
    """Every record matches exactly what the real automatic_config() binds for it, in both directions."""
    by_serial = _assert_solis_fleet_agrees(_solis_fleet())
    assert by_serial["BAT001"]["inverter_type"] == "SolisCloud" and "inverter_type" not in by_serial["PV001"]
    return False


def test_solis_catalogue_mixed_fleet_matches_each_list_by_position():
    """A PV-only inverter listed first puts the battery inverter at different indexes in the two kinds of list.

    [PV001, BAT001, NODETAIL001]: BAT001 is index 0 of automatic_config()'s battery lists but index 1 of
    pv_today/pv_power; PV001 is index 0 of those; NODETAIL001 (detail not read) is index 2 of them and
    its record still carries nothing.
    """
    api = _solis_fleet()
    api.inverter_sn = ["PV001", "BAT001", "NODETAIL001"]
    captured = capture_automatic_config(api)
    assert captured["soc_percent"] == ["sensor.predbat_solis_bat001_battery_soc"], captured["soc_percent"]
    assert captured["pv_power"] == ["sensor.predbat_solis_pv001_pv_power", "sensor.predbat_solis_bat001_pv_power", "sensor.predbat_solis_nodetail001_pv_power"], captured["pv_power"]
    by_serial = _assert_solis_fleet_agrees(api)
    assert by_serial["PV001"]["entities"]["pv_power"]["entity_id"] == "sensor.predbat_solis_pv001_pv_power"
    assert by_serial["BAT001"]["entities"]["pv_power"]["entity_id"] == "sensor.predbat_solis_bat001_pv_power"
    assert "entities" not in by_serial["NODETAIL001"], by_serial["NODETAIL001"]
    return False


def test_solis_catalogue_pv_load_ignore_keeps_the_entities():
    """Spec D11: solis_cloud_pv_load_ignore stops automatic_config() binding the PV/load sensors, not the record carrying them."""
    records = {}
    for ignore in (False, True):
        api = _solis_fleet()
        api.get_arg = lambda name, default=None, ignore=ignore: ignore if name == "solis_cloud_pv_load_ignore" else default
        captured = capture_automatic_config(api)
        for setting in SOLIS_PV_LOAD_IGNORED:
            assert (setting in captured) == (not ignore), (setting, ignore)
        records[ignore] = _assert_solis_fleet_agrees(api, allowed_extra=SOLIS_PV_LOAD_IGNORED if ignore else ())
    assert records[True] == records[False], "the record must not change with solis_cloud_pv_load_ignore"
    for setting in SOLIS_PV_LOAD_IGNORED:
        assert setting in records[True]["BAT001"]["entities"], setting
    return False


def test_solis_catalogue_two_inverters_get_their_own_entities():
    """Two battery inverters give two records, each matching its own index of automatic_config()'s lists."""
    api = _solis_battery_pair()
    by_serial = _assert_solis_fleet_agrees(api)
    for record in by_serial.values():
        assert_definition_complete(record, SolisAPI.WRITE_AND_POLL_SLEEP)
    first, second = by_serial["BAT001"]["entities"], by_serial["BAT002"]["entities"]
    assert set(first) == set(second), (sorted(first), sorted(second))
    for setting in first:
        assert "bat001" in first[setting]["entity_id"] and "bat002" in second[setting]["entity_id"], setting
    return False


def test_solis_catalogue_reports_battery_min_soc_not_reserve():
    """Spec D9: the record carries battery_min_soc (read-only entity plus rating) and no reserve.

    automatic_config() binds reserve and battery_min_soc to the same over_discharge_soc number, and
    inverter.py dummies the reserve binding because the SolisCloud row has has_reserve_soc False -
    so the record leaves reserve out and the derived has_reserve_soc is False. Also pins the row's
    time_button_press False (no schedule_write_button is bound) and battery_power_invert's string
    "True" becoming invert on battery_power.
    """
    api = _solis_fleet()
    api.cached_values["BAT001"][SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC] = "20"
    record = validated_inverters(api.build_discovery())[0]
    entities = record["entities"]
    assert "reserve" not in entities, entities.get("reserve")
    assert entities["battery_min_soc"] == {"entity_id": "number.predbat_solis_bat001_over_discharge_soc", "access": "r", "unit": "%"}, entities["battery_min_soc"]
    assert record["ratings"]["battery_min_soc"] == 20, record["ratings"]
    captured = capture_automatic_config(api)
    assert captured["reserve"] == captured["battery_min_soc"], "automatic_config() binds both to over_discharge_soc"
    definition, gaps, _ = inverter_definition(record, SolisAPI.WRITE_AND_POLL_SLEEP)
    assert not gaps, gaps
    assert definition["has_reserve_soc"] is False
    assert "schedule_write_button" not in entities and "schedule_write_button" not in captured
    assert definition["time_button_press"] is False
    assert captured["battery_power_invert"] == ["True"] and entities["battery_power"]["invert"] is True, entities["battery_power"]
    return False


def test_solis_catalogue_export_limit_is_the_configured_register():
    """export_limit is register 499 in watts, converted as publish_entities() presents it; 0 or unread gives no rating.

    publish_entities() shows an unset or 0 register as 99999 W ("no limit") - a placeholder, not a
    configured figure, so no rating is reported for it. A value under 200 is in 100 W units there.
    """
    api = _solis_fleet()

    def ratings():
        """The battery inverter's ratings as currently reported."""
        return {record["device_id"]: record for record in api.build_discovery()["inverters"]}["solis:BAT001"]["ratings"]

    assert "export_limit" not in ratings(), "register not read yet"
    api.cached_values["BAT001"][SOLIS_CID_MAX_EXPORT_POWER] = "0"
    assert "export_limit" not in ratings(), "0 means no limit"
    api.cached_values["BAT001"][SOLIS_CID_MAX_EXPORT_POWER] = "37"
    assert ratings()["export_limit"] == 3700.0, ratings()
    api.cached_values["BAT001"][SOLIS_CID_MAX_EXPORT_POWER] = "3680"
    assert ratings()["export_limit"] == 3680.0, ratings()
    return False
```

Register them in `run_solis_tests()`, straight after `failed |= test_solis_catalogue_round_trips_through_validate_report()`:

```python
        failed |= test_solis_catalogue_record_rebuilds_the_row()
        failed |= test_solis_catalogue_record_agrees_with_automatic_config()
        failed |= test_solis_catalogue_mixed_fleet_matches_each_list_by_position()
        failed |= test_solis_catalogue_pv_load_ignore_keeps_the_entities()
        failed |= test_solis_catalogue_two_inverters_get_their_own_entities()
        failed |= test_solis_catalogue_reports_battery_min_soc_not_reserve()
        failed |= test_solis_catalogue_export_limit_is_the_configured_register()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test solis > /tmp/solis.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/solis.log | head -20`
Expected: `ImportError: cannot import name 'SOLIS_CLOUD_CAPABILITIES' from 'solis'`.

- [ ] **Step 4: Implement in `solis.py`**

Add the capabilities constant just above `# Time options for selectors (HH:MM:SS format)`:

```python
# The seven INVERTER_DEF behaviour keys for a SolisCloud inverter, reported as the discovery record's
# capabilities. Stated here as literals, never read back from INVERTER_DEF["SolisCloud"]: the record
# has to rebuild that row on its own, and reading the row would make the completeness test prove
# nothing (docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md, 1.1).
SOLIS_CLOUD_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": True,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}
```

Replace `SolisAPI.build_discovery()` with the three new helpers followed by the new `build_discovery()` (`parse_cid_int`, `SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC` and `SOLIS_CID_MAX_EXPORT_POWER` are already defined in `solis.py`):

```python
    def _discovery_pv_entities(self, sn):
        """The PV sensors automatic_config() binds for every inverter it lists in pv_devices, battery or not.

        automatic_config() builds pv_today and pv_power over every discovered inverter so a PV-only
        inverter's generation still counts (GH#4922). A PV-only record carries just these (spec D12); a
        driven record carries them among the rest (_discovery_entities()). Each entity id is the same
        f-string automatic_config() builds, over the same lower-cased serial.
        """
        prefix = self.prefix
        device = sn.lower()
        return {
            "pv_today": {"entity_id": f"sensor.{prefix}_solis_{device}_pv_energy_total", "access": "r"},
            "pv_power": {"entity_id": f"sensor.{prefix}_solis_{device}_pv_power", "access": "r"},
        }

    def _discovery_entities(self, sn):
        """The settings automatic_config() binds for one battery inverter, as discovery entity descriptors.

        Each entity id is the same f-string automatic_config() builds for that setting, over the same
        lower-cased serial, so the two cannot drift without the agreement test failing. access is "rw"
        for a setting inverter.py writes (the slot 1 schedule, SoC and power controls) and "r" for one
        it only reads.

        Two automatic_config() bindings are deliberately absent. reserve: SolisCloud does not write
        the reserve, the row has has_reserve_soc False and inverter.py replaces the binding with a
        dummy, so the record reports battery_min_soc - the same over_discharge_soc number - instead
        (spec D9). battery_power_invert: it becomes invert on battery_power.

        load_today, pv_today, load_power and pv_power are carried even when solis_cloud_pv_load_ignore
        stops automatic_config() binding them: the record describes the device, and the user's opt-out
        is the coordinator's to apply (spec D11).
        """
        prefix = self.prefix
        device = sn.lower()
        entities = {
            "soc_percent": {"entity_id": f"sensor.{prefix}_solis_{device}_battery_soc", "access": "r", "unit": "%"},
            "battery_scaling": {"entity_id": f"sensor.{prefix}_solis_{device}_battery_soh", "access": "r"},
            "battery_power": {"entity_id": f"sensor.{prefix}_solis_{device}_battery_power", "access": "r", "invert": True},
            "grid_power": {"entity_id": f"sensor.{prefix}_solis_{device}_grid_power", "access": "r"},
            "battery_voltage": {"entity_id": f"sensor.{prefix}_solis_{device}_battery_voltage", "access": "r"},
            "load_today": {"entity_id": f"sensor.{prefix}_solis_{device}_total_load_energy", "access": "r"},
            "load_power": {"entity_id": f"sensor.{prefix}_solis_{device}_load_power", "access": "r"},
        }
        entities.update(self._discovery_pv_entities(sn))
        entities.update(
            {
                "import_today": {"entity_id": f"sensor.{prefix}_solis_{device}_today_import_energy", "access": "r"},
                "export_today": {"entity_id": f"sensor.{prefix}_solis_{device}_today_export_energy", "access": "r"},
                "battery_min_soc": {"entity_id": f"number.{prefix}_solis_{device}_over_discharge_soc", "access": "r", "unit": "%"},
                "charge_start_time": {"entity_id": f"select.{prefix}_solis_{device}_charge_slot1_start_time", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
                "charge_end_time": {"entity_id": f"select.{prefix}_solis_{device}_charge_slot1_end_time", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
                "charge_limit": {"entity_id": f"number.{prefix}_solis_{device}_charge_slot1_soc", "access": "rw", "unit": "%"},
                "charge_rate": {"entity_id": f"number.{prefix}_solis_{device}_charge_slot1_power", "access": "rw", "unit": "W"},
                "scheduled_charge_enable": {"entity_id": f"switch.{prefix}_solis_{device}_charge_slot1_enable", "access": "rw", "domain": "switch"},
                "discharge_start_time": {"entity_id": f"select.{prefix}_solis_{device}_discharge_slot1_start_time", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
                "discharge_end_time": {"entity_id": f"select.{prefix}_solis_{device}_discharge_slot1_end_time", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
                "discharge_target_soc": {"entity_id": f"number.{prefix}_solis_{device}_discharge_slot1_soc", "access": "rw", "unit": "%"},
                "discharge_rate": {"entity_id": f"number.{prefix}_solis_{device}_discharge_slot1_power", "access": "rw", "unit": "W"},
                "scheduled_discharge_enable": {"entity_id": f"switch.{prefix}_solis_{device}_discharge_slot1_enable", "access": "rw", "domain": "switch"},
                "battery_rate_max": {"entity_id": f"number.{prefix}_solis_{device}_max_charge_power", "access": "r", "unit": "W"},
                "inverter_limit": {"entity_id": f"sensor.{prefix}_solis_{device}_inverter_size", "access": "r"},
                "export_limit": {"entity_id": f"number.{prefix}_solis_{device}_max_export_power", "access": "r", "unit": "W"},
            }
        )
        return entities

    def _discovery_export_limit(self, sn):
        """The configured maximum export power (register 499) in watts, or None when there is no configured figure.

        Converted exactly as publish_entities() presents the max_export_power number that
        automatic_config() binds export_limit to: a value under 200 is in 100 W units. publish_entities()
        shows an unread or 0 register as 99999 W, a "no limit" placeholder rather than a configured
        figure, so neither is reported as a rating.
        """
        try:
            value = float(self.cached_values.get(sn, {}).get(SOLIS_CID_MAX_EXPORT_POWER))
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        if value < 200:
            value *= 100
        return value

    def build_discovery(self):
        """
        Describe the discovered Solis inverters for the discovery catalogue.

        Reads only what the component already holds - self.inverter_sn, self.inverter_details,
        self.cached_values and self.parallel_battery_count - so this adds no API calls and cannot
        change what Solis does. Reporting is independent of self.automatic.

        inverter_type "SolisCloud" is set with automatic_config()'s own test for a battery inverter
        it configures, duplicated here rather than shared so the control path is untouched:
        _reports_no_battery() must be false AND batteryHealthSoh must parse as a number (0 is a
        valid reading). "battery" in functions is the hardware fact alone - details read and
        Solis Cloud not saying "no battery" - so a battery inverter automatic_config() declines is
        still described. Every inverter reports "solar": automatic_config() puts every inverter in
        pv_devices, battery or not. An inverter whose detail has not been read yet (empty, or a
        failed fetch) reports no functions at all rather than a misleading solar-only guess - the
        same "not read yet" state automatic_config() retries rather than treats as PV-only.

        Only an inverter automatic_config() configures carries capabilities (SOLIS_CLOUD_CAPABILITIES),
        the full entity map (_discovery_entities()) and the reports_soh flag. Any other inverter whose
        detail has been read - PV-only, or a battery automatic_config() declines - carries just its
        pv_today and pv_power (_discovery_pv_entities(), spec D12), because automatic_config() puts it in
        pv_devices. One whose detail has not been read yet claims nothing at all.

        Ratings are keyed by Predbat setting name where one exists. inverter_limit is inverterDetail's
        power in powerStr's unit - defaulted to "kW" exactly as publish_entities() does - reported only
        for a unit this code knows how to convert, never guessed. For a driven inverter,
        battery_min_soc is the over-discharge SoC register (158) and export_limit the configured export
        cap (_discovery_export_limit()), each only once the register has been read.

        A rating is a figure the device reports (spec D14), so two Predbat derivations are left out.
        battery_rate_max is an entity only: the max_charge_power number is register current x
        get_nominal_voltage(). soc_max is not reported at all: a kWh capacity is register 172 x a
        voltage that is either inferred (for an HV pack still a live reading, GH#5090) or the user's
        solis_nominal_voltage, and automatic_config() binds no soc_max entity either.

        Battery ratings carry only stated facts. Register 172 (SOLIS_CID_BATTERY_CAPACITY) is the
        per-battery Ah; battery_capacity_ah reports the bank total - register 172 x
        parallel_battery_count, the same product publish_entities() uses - so it means the same
        thing here as on every other reporter. battery_pack_count carries the pack count alongside
        it, and both are always reported for a battery inverter.

        Deliberately not reported: inverterName (user-set free text that can hold an address);
        firmware (inverterDetail carries none); any station or account ID (none is held).
        The TOU V2 register layout is reported as the flag "tou_v2" - how the inverter is driven,
        not something it can do.

        Returns None when no inverter has been discovered yet.
        """
        if not self.inverter_sn:
            return None

        inverters = []
        for sn in self.inverter_sn:
            detail = self.inverter_details.get(sn, {}) or {}
            has_battery = bool(detail) and not self._reports_no_battery(detail)
            try:
                float(detail.get("batteryHealthSoh"))
                reports_soh = True
            except (TypeError, ValueError):
                reports_soh = False
            # automatic_config()'s own predicate for an inverter it configures - the source of truth.
            drives_it = not self._reports_no_battery(detail) and reports_soh

            info = {}
            if detail.get("productModel"):
                info["model"] = str(detail["productModel"])

            ratings = {}
            try:
                power = float(detail.get("power"))
            except (TypeError, ValueError):
                power = 0.0
            power_unit = str(detail.get("powerStr", "kW")).strip()
            if power > 0 and power_unit == "kW":
                ratings["inverter_limit"] = power * 1000.0
            elif power > 0 and power_unit == "W":
                ratings["inverter_limit"] = power
            if has_battery:
                try:
                    capacity_ah = float(self.cached_values.get(sn, {}).get(SOLIS_CID_BATTERY_CAPACITY))
                except (TypeError, ValueError):
                    capacity_ah = 0.0
                if capacity_ah > 0:
                    pack_count = self.parallel_battery_count.get(sn, 1)
                    ratings["battery_capacity_ah"] = capacity_ah * pack_count
                    ratings["battery_pack_count"] = pack_count
            if drives_it:
                min_soc = parse_cid_int(self.cached_values.get(sn, {}).get(SOLIS_CID_BATTERY_OVER_DISCHARGE_SOC))
                if min_soc is not None:
                    ratings["battery_min_soc"] = min_soc
                export_limit = self._discovery_export_limit(sn)
                if export_limit is not None:
                    ratings["export_limit"] = export_limit

            if drives_it:
                entities = self._discovery_entities(sn)
            elif detail:
                entities = self._discovery_pv_entities(sn)
            else:
                entities = None

            flags = []
            if drives_it:
                flags.append("reports_soh")
            if self.is_tou_v2_mode(sn):
                flags.append("tou_v2")

            inverters.append(
                inverter_record(
                    "solis:{}".format(sn),
                    inverter_type="SolisCloud" if drives_it else None,
                    composition="direct",
                    functions=(["solar", "battery"] if has_battery else ["solar"]) if detail else None,
                    capabilities=dict(SOLIS_CLOUD_CAPABILITIES) if drives_it else None,
                    flags=flags,
                    hardware_ids={"serial": sn},
                    info=info,
                    ratings=ratings,
                    entities=entities,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

`automatic_config()`, `publish_entities()` and `INVERTER_DEF["SolisCloud"]` are unchanged.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test solis --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test components > /tmp/solis.log 2>&1; grep -n "FAILED\|Traceback\|AssertionError" /tmp/solis.log | head -20; tail -3 /tmp/solis.log`
Expected: no FAILED/Traceback/AssertionError lines; `**** All tests passed ****`.

Then `pre-commit run --files apps/predbat/solis.py apps/predbat/tests/test_solis.py`. Expected: every hook Passed or Skipped.

- [ ] **Step 6: Commit**

Run `detect_changes()` first: the diff must touch only `SOLIS_CLOUD_CAPABILITIES`, the three `_discovery_*` helpers, `build_discovery` and the Solis tests.

```bash
git add apps/predbat/solis.py apps/predbat/tests/test_solis.py
git commit -m "feat(discovery): Solis Cloud record carries capabilities, entities and Predbat-named ratings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: GE Cloud reporter

**Files:**
- Modify: `apps/predbat/gecloud.py` (replace `GE_CLOUD_CAPABILITY_SUBSTRINGS` with four constants; `GECloudDirect.WRITE_AND_POLL_SLEEP`; replace `_device_capabilities` with five discovery helpers; `_device_info_and_ratings`; `build_discovery`)
- Modify: `apps/predbat/config.py` (`INVERTER_DEF["GEC"]` and `INVERTER_DEF["GEE"]`: `soc_units` `"kWh"` -> `"%"`, spec D13)
- Test: `apps/predbat/tests/test_ge_cloud.py`

**Interfaces:**
- Consumes: `inverter_record(..., entities=...)`, the dict-shaped `capabilities` and `inverter_definition()` (Task 1, including its `charge_rate_percent` -> `"power"` rule); `assert_definition_complete(..., except_fields=...)`, `assert_record_agrees`, `assert_record_binds_nothing_extra`, `capture_automatic_config`, `validated_inverters`, `SITE_SETTINGS` (Task 2).
- Produces (in `gecloud.py`):
  - `GE_CLOUD_CAPABILITIES` - `{"GEC": {...seven keys...}, "GEE": {...}}`, literal
  - `GE_CLOUD_DUMMIED_SETTINGS` - `{"GEC": frozenset(), "GEE": frozenset({...})}`
  - `GE_CLOUD_TIME_SELECT_FORMAT = "HH:MM:SS"`; `GE_CLOUD_CLOCK_FORMAT = "%Y-%m-%dT%H:%M:%S%z"` (the ISO timestamp the time sensor really publishes)
  - `GECloudDirect.WRITE_AND_POLL_SLEEP = 10`
  - Private discovery helpers `GECloudDirect._register_features(device)`, `_first_register(device, candidates)`, `_shared_ct(devices, batteries)`, `_export_limit_share(device)` and `_device_entities(devices, batteries, index, inverter_type)`.
  - Ratings renamed: `max_charge_w` -> `battery_rate_max`, `battery_kwh` -> `soc_max`. New `export_limit`: this inverter's equal share of the site limit (D7).
  - PV-only records carry `pv_power` and `pv_today` (`access: r`) and still no `inverter_type` or `capabilities` (D12).
  - `GE_CLOUD_CAPABILITY_SUBSTRINGS` and `_device_capabilities()` are removed.

`async_automatic_config()` is **not** touched. `_device_entities()` repeats its per-device choices rather than sharing a helper with it:
- `first_existing_entity()`'s candidate order
- the shared-CT and EMS reconfigurations, including the 0 stand-ins and the lists that shrink to index 0

It differs from `async_automatic_config()` in exactly two ways, both spec decisions:
- **D10:** the pause, discharge-target and percentage-rate controls follow each device's own registers, not the "any device has it" gate.
- **D11:** `load_today` is carried despite `ge_cloud_load_today_ignore`.

The agreement and reverse checks below pin the rest equal on seven installs:
- a single inverter with an upper-case serial
- a mixed two-inverter fleet
- a shared CT
- an EMS
- a gateway
- a percentage-rate model
- a site export limit

The completeness tests carry one named exception, `clock_time_format` (D13). The GEC/GEE rows keep `"%H:%M:%S"` for installs that do not use the component. The record describes the ISO format the sensor publishes, and a test parses a published timestamp with it.

- [ ] **Step 1: Run impact analysis**

Run `impact` upstream on `build_discovery`, `_device_info_and_ratings` and `_device_capabilities` in `apps/predbat/gecloud.py`. Expected: the only production caller is `ComponentBase.refresh_discovery()`, via `GECloudDirect.run()`. `_device_capabilities` and `_device_info_and_ratings` are called only from `build_discovery`. Risk LOW.

For `INVERTER_DEF`'s `soc_units`, run `git grep -n "soc_units" -- 'apps/predbat/*.py'`. Expected: the only reader is `inverter.py:416`, which sets `inv_soc_units`, and nothing reads that attribute. Changing GEC/GEE therefore changes no behaviour.

(If the index is stale and a symbol is not found, `git grep -n "build_discovery\|_device_capabilities\|_device_info_and_ratings" -- apps/predbat` gives the same answer.)

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_ge_cloud.py`, add after the `from tests.test_infra import ...` line:

```python
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from coordinator import inverter_definition
```

Add this block after `_test_discovery_report_retries_after_a_failure` (before `_test_publish_evc_device`):

```python
# ---------------------------------------------------------------------------------------------
# Discovery record vocabulary (schema 2): entities, capabilities and ratings
# ---------------------------------------------------------------------------------------------

# Every control register async_automatic_config() looks for on a GivEnergy inverter
GEC_FULL_REGISTERS = {
    "reg1": {"name": "Enable_Eco_Mode"},
    "reg2": {"name": "Battery_Charge_Power"},
    "reg3": {"name": "Battery_Discharge_Power"},
    "reg4": {"name": "Battery_Reserve_Percent_Limit"},
    "reg5": {"name": "AC_Charge_Upper_Percent_Limit"},
    "reg6": {"name": "Enable_AC_Charge_Upper_Percent_Limit"},
    "reg7": {"name": "Enable_Force_Charge"},
    "reg8": {"name": "AC_Charge_Enable"},
    "reg9": {"name": "Enable_DC_Discharge"},
    "reg10": {"name": "Pause_Battery"},
    "reg11": {"name": "Pause_Battery_Start_Time"},
    "reg12": {"name": "DC_Discharge_1_Lower_SOC_Percent_Limit"},
}

GEC_INFO = {"info": {"model": "GIV-HY5.0", "max_charge_rate": 3600, "battery": {"nominal_capacity": 186, "nominal_voltage": 51.2}}, "firmware_version": {"ARM": 616, "DSP": 616}}

# Spec D13: the record gives inverter_time the ISO format the sensor really publishes, while the GEC/GEE
# rows keep "%H:%M:%S" for installs that do not use this component
CLOCK_EXCEPTION = ("clock_time_format",)

# async_automatic_config() binds these for every device when ANY device reports the register; the
# record lists them only for a device that reports them itself (spec D10)
FLEET_GATED = ("pause_mode", "pause_start_time", "pause_end_time", "discharge_target_soc", "charge_rate_percent", "discharge_rate_percent")


def _record_component(devices, settings, info=None, config=None):
    """A test-local GE Cloud component holding a discovered install, ready for build_discovery() and a captured async_automatic_config()."""
    ge = MockGECloudDirect()
    ge.devices_dict = devices
    ge.settings = settings
    ge.info = info or {}
    ge.config_args = dict(config or {})
    ge.automatic_config = lambda: ge.async_automatic_config(devices)
    return ge


def _agree_at(record, captured, index, missing=(), allowed_extra=()):
    """
    assert_record_agrees() and assert_record_binds_nothing_extra() for logical inverter `index` of a multi-device install.

    Some lists async_automatic_config() binds are shorter than the fleet (import_today on a shared
    CT or behind an EMS), and a None in a list means nothing is bound for that device: the record
    for this device must not carry either. `missing` names settings automatic_config() binds for
    this device that the record deliberately leaves out, because the device does not report them
    (spec D10); each must be absent. `allowed_extra` is passed to the reverse check.
    """
    entities = record.get("entities") or {}
    per_device = {}
    for setting, value in captured.items():
        if isinstance(value, list) and index >= len(value):
            assert setting not in entities, "{} is bound for {} devices only, so device {} must not carry it: {}".format(setting, len(value), index, entities[setting])
        elif isinstance(value, list) and value[index] is None:
            assert setting not in entities, "{} is bound to nothing for device {}, so the record must not carry it: {}".format(setting, index, entities[setting])
        elif setting in missing:
            assert setting not in entities, "device {} does not report {}, so its record must not carry it: {}".format(index, setting, entities[setting])
        else:
            per_device[setting] = value
    assert_record_agrees(record, per_device, index=index)
    assert_record_binds_nothing_extra(record, captured, index=index, allowed_extra=allowed_extra)


def _entity_ids(record):
    """Every entity id a record binds."""
    return {descriptor["entity_id"] for descriptor in (record.get("entities") or {}).values() if "entity_id" in descriptor}


def _test_discovery_ge_rows_soc_units(my_predbat):
    """GE Cloud binds soc_percent, so the GEC and GEE rows say "%" (spec D13); the GivTCP row is unchanged."""
    from config import INVERTER_DEF

    assert INVERTER_DEF["GEC"]["soc_units"] == "%" and INVERTER_DEF["GEE"]["soc_units"] == "%"
    assert INVERTER_DEF["GE"]["soc_units"] == "kWh"
    assert INVERTER_DEF["GEC"]["clock_time_format"] == INVERTER_DEF["GEE"]["clock_time_format"] == "%H:%M:%S", "clock_time_format rows stay as they are"
    print("PASS: GEC and GEE rows give soc_units %")
    return 0


def _test_discovery_record_complete_gec(my_predbat):
    """A single fully-featured GivEnergy inverter's record rebuilds the GEC row with no row to lean on."""
    devices = {"ems": None, "gateway": None, "battery": ["battery001"], "pv": [], "battery_meters": {"battery001": [1001]}}
    ge = _record_component(devices, {"battery001": dict(GEC_FULL_REGISTERS)}, info={"battery001": GEC_INFO})
    records = validated_inverters(ge.build_discovery())
    assert len(records) == 1, records
    for record in records:
        if record.get("inverter_type"):
            assert_definition_complete(record, GECloudDirect.WRITE_AND_POLL_SLEEP, except_fields=CLOCK_EXCEPTION)
    record = records[0]
    assert record["capabilities"] == {
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "support_feedin_first": False,
        "can_span_midnight": True,
        "charge_discharge_with_rate": False,
        "charge_control_immediate": False,
        "target_soc_used_for_discharge": False,
    }, record["capabilities"]
    assert record["entities"]["inverter_mode"] == {"entity_id": "switch.predbat_gecloud_battery001_enable_eco_mode", "access": "rw", "domain": "switch"}
    assert record["ratings"]["battery_rate_max"] == 3600 and record["ratings"]["soc_max"] == 9.52, record["ratings"]
    assert "max_charge_w" not in record["ratings"] and "battery_kwh" not in record["ratings"]
    print("PASS: GEC record rebuilds its row")
    return 0


def _test_discovery_record_clock_format_is_true(my_predbat):
    """inverter_time's format parses the timestamp the GE Cloud time sensor actually publishes."""
    devices = {"ems": None, "gateway": None, "battery": ["battery001"], "pv": [], "battery_meters": {}}
    ge = _record_component(devices, {"battery001": {}})
    record = validated_inverters(ge.build_discovery())[0]
    run_async(ge.publish_status("battery001", {"time": "2026-08-22T18:21:41Z"}))
    published = ge.dashboard_items[record["entities"]["inverter_time"]["entity_id"]]["state"]
    parsed = datetime.strptime(published, record["entities"]["inverter_time"]["format"])
    assert parsed == datetime(2026, 8, 22, 18, 21, 41, tzinfo=timezone.utc), parsed
    print("PASS: inverter_time format parses the published sensor")
    return 0


def _test_discovery_record_agrees_gec(my_predbat):
    """Every setting async_automatic_config() binds for a single inverter is in its record, and nothing more - with a real upper-case serial."""
    serial = "SA2243G277"
    devices = {"ems": None, "gateway": None, "battery": [serial], "pv": [], "battery_meters": {serial: [1001]}}
    ge = _record_component(devices, {serial: dict(GEC_FULL_REGISTERS)}, info={serial: GEC_INFO})
    record = validated_inverters(ge.build_discovery())[0]
    captured = capture_automatic_config(ge)
    assert captured["scheduled_charge_enable"] == ["switch.predbat_gecloud_SA2243G277_enable_force_charge"], "fixture should exercise the multi-candidate choice"
    assert_record_agrees(record, captured, index=0)
    assert_record_binds_nothing_extra(record, captured, index=0)
    assert record["entities"]["scheduled_charge_enable"]["entity_id"] == "switch.predbat_gecloud_SA2243G277_enable_force_charge"
    assert_definition_complete(record, GECloudDirect.WRITE_AND_POLL_SLEEP, except_fields=CLOCK_EXCEPTION)
    print("PASS: GEC record agrees with async_automatic_config()")
    return 0


def _test_discovery_record_two_devices(my_predbat):
    """
    Two inverters give two records whose entity ids differ by device, each describing its own device
    (spec D10). automatic_config()'s "any device has it" gate binds the pause and discharge-target
    entities on the second device although it has no such registers, and withholds its percentage
    rate controls because the first device has a power register; the record does neither.
    """
    devices = {"ems": None, "gateway": None, "battery": ["battery001", "battery002"], "pv": [], "battery_meters": {"battery001": [1001], "battery002": [1002]}}
    settings = {
        "battery001": dict(GEC_FULL_REGISTERS),
        "battery002": {
            "reg1": {"name": "Inverter_Charge_Power_Percentage"},
            "reg2": {"name": "Inverter_Discharge_Power_Percentage"},
            "reg3": {"name": "Battery_Reserve_Percent"},
            "reg4": {"name": "AC_Charge_1_Upper_SOC_Percent_Limit"},
            "reg5": {"name": "Enable_AC_Charge"},
        },
    }
    ge = _record_component(devices, settings, info={"battery001": GEC_INFO, "battery002": GEC_INFO})
    records = validated_inverters(ge.build_discovery())
    assert [record["device_id"] for record in records] == ["gecloud:battery001", "gecloud:battery002"]
    assert not (_entity_ids(records[0]) & _entity_ids(records[1])), "the two records must not share an entity"
    captured = capture_automatic_config(ge)
    _agree_at(records[0], captured, 0)
    _agree_at(records[1], captured, 1, missing=("pause_mode", "pause_start_time", "pause_end_time", "discharge_target_soc"), allowed_extra=("charge_rate_percent", "discharge_rate_percent"))
    assert captured["pause_mode"][1] == "select.predbat_gecloud_battery002_pause_battery", "automatic_config's fleet gate binds pause on the second device"
    assert captured["charge_rate_percent"] is None, "automatic_config's fleet gate withholds the percentage rates"
    second = records[1]["entities"]
    assert second["reserve"]["entity_id"] == "number.predbat_gecloud_battery002_battery_reserve_percent", "the per-device candidate choice"
    assert second["scheduled_charge_enable"]["entity_id"] == "switch.predbat_gecloud_battery002_enable_ac_charge"
    assert second["charge_rate_percent"]["entity_id"] == "number.predbat_gecloud_battery002_inverter_charge_power_percentage"
    assert "inverter_mode" not in second and "pause_mode" not in second and "charge_rate" not in second
    assert "battery_temperature_history" in records[0]["entities"] and "battery_temperature_history" not in second
    definition, _, _ = inverter_definition(records[1], GECloudDirect.WRITE_AND_POLL_SLEEP)
    assert definition["has_timed_pause"] is False and definition["output_charge_control"] == "power", definition
    print("PASS: two devices give two per-device records with distinct entity ids")
    return 0


def _test_discovery_record_shared_ct(my_predbat):
    """On a shared CT the second inverter reads grid and load as a fixed 0 and carries no import/export totals."""
    devices = {"ems": None, "gateway": None, "battery": ["battery001", "battery002"], "pv": [], "battery_meters": {"battery001": [9999], "battery002": [9999]}}
    settings = {"battery001": dict(GEC_FULL_REGISTERS), "battery002": dict(GEC_FULL_REGISTERS)}
    ge = _record_component(devices, settings)
    records = validated_inverters(ge.build_discovery())
    captured = capture_automatic_config(ge)
    for index, record in enumerate(records):
        _agree_at(record, captured, index)
    second = records[1]["entities"]
    assert second["grid_power"] == {"value": 0, "access": "r"} and second["load_power"] == {"value": 0, "access": "r"}, second
    assert "import_today" not in second and "export_today" not in second and "load_today" not in second
    ge.config_args["ge_cloud_automatic_split_ct"] = True
    split = validated_inverters(ge.build_discovery())[1]["entities"]
    assert split["grid_power"]["entity_id"] == "sensor.predbat_gecloud_battery002_grid_power", "the split-CT override keeps per-device readings"
    print("PASS: shared CT records mirror automatic_config's single-source binding")
    return 0


def _test_discovery_record_ems(my_predbat):
    """Behind an EMS every record is GEE, rebuilds the GEE row, and binds the EMS's own schedule entities."""
    devices = {"ems": "ems001", "gateway": None, "battery": ["battery001", "battery002"], "pv": [], "battery_meters": {}}
    settings = {"battery001": dict(GEC_FULL_REGISTERS), "battery002": dict(GEC_FULL_REGISTERS)}
    ge = _record_component(devices, settings)
    records = validated_inverters(ge.build_discovery())
    for record in records:
        assert record["inverter_type"] == "GEE"
        assert_definition_complete(record, GECloudDirect.WRITE_AND_POLL_SLEEP, except_fields=CLOCK_EXCEPTION)
    captured = capture_automatic_config(ge)
    for index, record in enumerate(records):
        _agree_at(record, captured, index)
    first, second = records[0]["entities"], records[1]["entities"]
    assert first["idle_start_time"] == {"entity_id": "select.predbat_gecloud_ems001_discharge_start_time_slot_1", "access": "rw", "domain": "select", "format": "HH:MM:SS"}
    assert second["charge_limit"]["entity_id"] == "number.predbat_gecloud_ems001_charge_soc_percent_limit_1"
    assert first["battery_power"]["entity_id"] == "sensor.predbat_gecloud_ems001_battery_power"
    assert second["battery_power"] == {"value": 0, "access": "r"}
    for dummied in ("scheduled_charge_enable", "scheduled_discharge_enable", "pause_mode", "inverter_mode"):
        assert dummied not in first, "{} is dummied by the GEE row".format(dummied)
    assert records[0]["capabilities"]["charge_control_immediate"] is True and records[0]["capabilities"]["can_span_midnight"] is False
    print("PASS: EMS records rebuild GEE and agree with automatic_config")
    return 0


def _test_discovery_record_gateway(my_predbat):
    """A gateway fronting two batteries is one record, bound to the gateway's own registers."""
    devices = {"ems": None, "gateway": "gateway001", "battery": ["battery001", "battery002"], "pv": [], "battery_meters": {}}
    ge = _record_component(devices, {"gateway001": dict(GEC_FULL_REGISTERS)})
    records = validated_inverters(ge.build_discovery())
    assert len(records) == 1
    assert_definition_complete(records[0], GECloudDirect.WRITE_AND_POLL_SLEEP, except_fields=CLOCK_EXCEPTION)
    captured = capture_automatic_config(ge)
    assert_record_agrees(records[0], captured, index=0)
    assert_record_binds_nothing_extra(records[0], captured, index=0)
    assert records[0]["entities"]["charge_limit"]["entity_id"] == "number.predbat_gecloud_gateway001_ac_charge_upper_percent_limit"
    print("PASS: gateway record rebuilds GEC and agrees")
    return 0


def _test_discovery_record_percentage_rate(my_predbat):
    """A model with only percentage rate registers records charge_rate_percent, not a W charge_rate, and still rebuilds the GEC row."""
    devices = {"ems": None, "gateway": None, "battery": ["battery001"], "pv": [], "battery_meters": {}}
    registers = {key: register for key, register in GEC_FULL_REGISTERS.items() if register["name"] not in ("Battery_Charge_Power", "Battery_Discharge_Power")}
    registers["reg13"] = {"name": "Inverter_Charge_Power_Percentage"}
    registers["reg14"] = {"name": "Inverter_Discharge_Power_Percentage"}
    ge = _record_component(devices, {"battery001": registers})
    record = validated_inverters(ge.build_discovery())[0]
    captured = capture_automatic_config(ge)
    assert_record_agrees(record, captured, index=0)
    assert_record_binds_nothing_extra(record, captured, index=0)
    entities = record["entities"]
    assert "charge_rate" not in entities and "discharge_rate" not in entities
    assert entities["charge_rate_percent"] == {"entity_id": "number.predbat_gecloud_battery001_inverter_charge_power_percentage", "access": "rw", "unit": "%"}
    assert entities["discharge_rate_percent"]["entity_id"] == "number.predbat_gecloud_battery001_inverter_discharge_power_percentage"
    assert_definition_complete(record, GECloudDirect.WRITE_AND_POLL_SLEEP, except_fields=CLOCK_EXCEPTION)
    print("PASS: percentage-rate model records charge_rate_percent and rebuilds GEC")
    return 0


def _test_discovery_record_export_limit_share(my_predbat):
    """The site export limit is each logical inverter's equal share, the value publish_site_export_limit() publishes."""
    devices = {"ems": None, "gateway": None, "battery": ["battery001", "battery002"], "pv": [], "battery_meters": {}}
    ge = _record_component(devices, {"battery001": {}, "battery002": {}})
    records = validated_inverters(ge.build_discovery())
    assert all("export_limit" not in record.get("ratings", {}) and "export_limit" not in record["entities"] for record in records), "no site limit, no export_limit"
    ge.site_export_limit = 5000
    ge.site_inverters = ["battery001", "battery002"]
    records = validated_inverters(ge.build_discovery())
    run_async(ge.publish_site_export_limit("battery001"))
    published = ge.dashboard_items["sensor.predbat_gecloud_battery001_export_limit"]["state"]
    assert records[0]["ratings"]["export_limit"] == published == 2500, (records[0]["ratings"], published)
    assert records[1]["ratings"]["export_limit"] == 2500
    assert records[0]["entities"]["export_limit"] == {"entity_id": "sensor.predbat_gecloud_battery001_export_limit", "access": "r"}
    captured = capture_automatic_config(ge)
    assert_record_agrees(records[0], captured, index=0)
    assert_record_binds_nothing_extra(records[0], captured, index=0)
    print("PASS: export_limit rating is the published per-inverter share")
    return 0


def _test_discovery_record_load_today_ignored(my_predbat):
    """ge_cloud_load_today_ignore is a user opt-out: automatic_config binds no load_today, but the record still carries it (spec D11)."""
    devices = {"ems": None, "gateway": None, "battery": ["battery001"], "pv": [], "battery_meters": {}}
    ge = _record_component(devices, {"battery001": {}}, config={"ge_cloud_load_today_ignore": True})
    record = validated_inverters(ge.build_discovery())[0]
    assert record["entities"]["load_today"] == {"entity_id": "sensor.predbat_gecloud_battery001_consumption_total", "access": "r"}
    captured = capture_automatic_config(ge)
    assert "load_today" not in captured
    assert_record_agrees(record, captured, index=0)
    assert_record_binds_nothing_extra(record, captured, index=0, allowed_extra=("load_today",))
    print("PASS: load_today stays in the record despite ge_cloud_load_today_ignore")
    return 0


def _test_discovery_record_pv_only_carries_generation(my_predbat):
    """
    A sensor-only PV device carries its pv_power and pv_today as read-only entities - the ids
    automatic_config() binds for it under ge_cloud_automatic_split_pv - and no inverter_type or
    capabilities (spec D12). Without the split-PV setting the record still carries them (D11).
    """
    devices = {"ems": None, "gateway": None, "battery": ["battery001"], "pv": ["pv001"], "battery_meters": {}}
    for split_pv in (True, False):
        ge = _record_component(devices, {"battery001": {}}, info={"pv001": {"info": {"model": "GIV-PV"}}}, config={"ge_cloud_automatic_split_pv": split_pv})
        records = validated_inverters(ge.build_discovery())
        pv_record = next(record for record in records if record["device_id"] == "gecloud:pv001")
        assert "inverter_type" not in pv_record and "capabilities" not in pv_record, pv_record
        assert pv_record["entities"] == {
            "pv_power": {"entity_id": "sensor.predbat_gecloud_pv001_solar_power", "access": "r"},
            "pv_today": {"entity_id": "sensor.predbat_gecloud_pv001_solar_total", "access": "r"},
        }, pv_record["entities"]
        captured = capture_automatic_config(ge)
        if split_pv:
            assert captured["pv_power"][1] == pv_record["entities"]["pv_power"]["entity_id"]
            assert captured["pv_today"][1] == pv_record["entities"]["pv_today"]["entity_id"]
        else:
            assert len(captured["pv_power"]) == 1, "without split PV automatic_config binds the battery inverter only"
    print("PASS: PV-only record carries pv_power and pv_today, no inverter_type or capabilities")
    return 0


def _test_discovery_write_and_poll_sleep(my_predbat):
    """GE Cloud waits 10 seconds between a write and its read-back, as the GEC and GEE rows say."""
    assert GECloudDirect.WRITE_AND_POLL_SLEEP == 10
    print("PASS: GE Cloud write_and_poll_sleep is 10")
    return 0
```

Register them in `test_ge_cloud()`'s `sub_tests`, straight after the `("discovery_report_retries", ...)` entry:

```python
        ("discovery_ge_rows_soc_units", _test_discovery_ge_rows_soc_units, "GEC and GEE rows give soc_units %"),
        ("discovery_record_complete_gec", _test_discovery_record_complete_gec, "discovery record rebuilds the GEC row"),
        ("discovery_record_clock_format", _test_discovery_record_clock_format_is_true, "inverter_time format parses the published sensor"),
        ("discovery_record_agrees_gec", _test_discovery_record_agrees_gec, "discovery record agrees with automatic_config"),
        ("discovery_record_two_devices", _test_discovery_record_two_devices, "two devices give two agreeing records with distinct entities"),
        ("discovery_record_shared_ct", _test_discovery_record_shared_ct, "shared-CT records mirror the single-source binding"),
        ("discovery_record_ems", _test_discovery_record_ems, "EMS records rebuild the GEE row and agree"),
        ("discovery_record_gateway", _test_discovery_record_gateway, "gateway record rebuilds the GEC row and agrees"),
        ("discovery_record_percentage_rate", _test_discovery_record_percentage_rate, "percentage-rate model records charge_rate_percent"),
        ("discovery_record_export_limit", _test_discovery_record_export_limit_share, "export_limit rating is the published per-inverter share"),
        ("discovery_record_load_today_ignored", _test_discovery_record_load_today_ignored, "load_today stays in the record despite ge_cloud_load_today_ignore"),
        ("discovery_record_pv_only", _test_discovery_record_pv_only_carries_generation, "PV-only record carries pv_power and pv_today only"),
        ("discovery_write_and_poll_sleep", _test_discovery_write_and_poll_sleep, "GE Cloud write_and_poll_sleep is 10"),
```

Update the existing assertions that pinned the old tokens and rating names:

- `_test_build_discovery_battery_only_direct`: replace
  `assert "charge_rate_power" in record["capabilities"], "the direct power registers should be sniffed as a capability"` with
  ```python
    assert len(record["capabilities"]) == 7 and record["capabilities"]["support_discharge_freeze"] is True, record["capabilities"]
    assert record["entities"]["charge_rate"] == {"entity_id": "number.predbat_gecloud_battery001_battery_charge_power", "access": "rw", "unit": "W"}, "the direct power register should be the charge_rate control"
  ```
  and `assert record["ratings"]["max_charge_w"] == 3600` with `assert record["ratings"]["battery_rate_max"] == 3600`.
- `_test_build_discovery_gateway_composition`: replace
  `assert "pause_mode" in record["capabilities"], "capabilities should be sniffed from the gateway's own settings"` with
  ```python
    assert record["entities"]["pause_mode"]["entity_id"] == "select.predbat_gecloud_gateway001_pause_battery", "entities should be chosen from the gateway's own settings"
  ```
- `_test_build_discovery_round_trips_through_the_coordinator`: replace
  `assert set(record["capabilities"]) == set(original["capabilities"]) == {"charge_rate_power", "pause_mode", "pause_slots", "discharge_target"}` with
  ```python
    assert record["capabilities"] == original["capabilities"] and len(record["capabilities"]) == 7
    assert record["entities"] == original["entities"], "every entity descriptor should survive validation"
    assert {"charge_rate", "pause_mode", "pause_start_time", "discharge_target_soc"} <= set(record["entities"])
  ```
  and the two rating lines with
  ```python
    assert record["ratings"]["soc_max"] == original["ratings"]["soc_max"]
    assert record["ratings"]["battery_rate_max"] == 3600
  ```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test ge_cloud > /tmp/ge_cloud.log 2>&1; grep -n "✗\|RESULTS" /tmp/ge_cloud.log | cut -c1-200`

Expected: `RESULTS: 101 passed, 16 failed out of 117 tests`. The failures:
- `discovery_direct`: `['charge_rate_power']`
- `discovery_gateway`, `discovery_record_clock_format`, `discovery_record_export_limit`, `discovery_record_load_today_ignored` and `discovery_record_pv_only`: `'entities'`
- `discovery_round_trip`, `discovery_ge_rows_soc_units` and `discovery_write_and_poll_sleep`: bare `AssertionError`
- the completeness tests: `record cannot rebuild these INVERTER_DEF fields: ['support_charge_freeze', ...]`
- the agreement tests: `record disagrees with automatic_config(): [...]`

- [ ] **Step 4: Implement**

In `apps/predbat/config.py`, change `"soc_units": "kWh"` to `"soc_units": "%"` in the `"GEC"` row (`"name": "GivEnergy Cloud"`) and the `"GEE"` row (`"name": "GivEnergy EMC"`) only. The `"GE"` row keeps `"kWh"`, and `clock_time_format` is unchanged on all three.

In `apps/predbat/gecloud.py`, replace the `GE_CLOUD_CAPABILITY_SUBSTRINGS` constant and its comment with:

```python
# The behaviour of the two GE Cloud inverter types, for the discovery record's `capabilities`: the
# seven INVERTER_DEF behaviour keys, valued as the "GEC" and "GEE" rows state them today
# (support_feedin_first is absent from both rows and defaults to False in inverter.py). Literal on
# purpose - reading the rows back would make the discovery completeness test prove nothing.
GE_CLOUD_CAPABILITIES = {
    "GEC": {
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "support_feedin_first": False,
        "can_span_midnight": True,
        "charge_discharge_with_rate": False,
        "charge_control_immediate": False,
        "target_soc_used_for_discharge": False,
    },
    "GEE": {
        "support_charge_freeze": True,
        "support_discharge_freeze": False,
        "support_feedin_first": False,
        "can_span_midnight": False,
        "charge_discharge_with_rate": False,
        "charge_control_immediate": True,
        "target_soc_used_for_discharge": False,
    },
}

# Settings async_automatic_config() binds that inverter.py replaces with a dummy entity for the type
# (the row's presence flag is False), so the discovery record leaves them out. Behind an EMS the
# battery inverters' own charge/discharge enables, pause and eco switches are never driven.
GE_CLOUD_DUMMIED_SETTINGS = {
    "GEC": frozenset(),
    "GEE": frozenset({"scheduled_charge_enable", "scheduled_discharge_enable", "pause_mode", "inverter_mode"}),
}

# publish_registers() publishes every "date_format:H:i" register as a select whose options and state
# are HH:MM:SS
GE_CLOUD_TIME_SELECT_FORMAT = "HH:MM:SS"

# The format of the inverter_time sensor: publish_status() publishes the API's ISO timestamp
# ("2025-02-09T15:00:03Z") unchanged. The GEC/GEE rows keep "%H:%M:%S" for installs that do not use
# this component (spec D13), so the discovery record describes the sensor, not the row.
GE_CLOUD_CLOCK_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
```

Add a class attribute to `GECloudDirect`, directly under its docstring:

```python
    # GivEnergy's cloud applies a write some seconds after accepting it; the GEC and GEE rows wait 10
    WRITE_AND_POLL_SLEEP = 10
```

Replace `_device_capabilities()` with these five methods:

```python
    def _register_features(self, device):
        """
        The register features async_automatic_config() sniffs for, as this one device reports them.

        The same substrings as its sniffing loop, but scoped to this device's own settings rather
        than ORed across the fleet: automatic_config() gates pause_mode, pause_start_time/end_time,
        discharge_target_soc and the percentage rate controls for every device on what ANY device
        reports, while a discovery record describes only its own device (spec D10).
        """
        features = {"charge_rate": False, "discharge_rate": False, "charge_power_percent": False, "discharge_power_percent": False, "pause_start_time": False, "discharge_target_soc": False, "pause_battery": False}
        for register in self.settings.get(device, {}).values():
            ha_name = regname_to_ha(register.get("name", ""))
            if "battery_charge_power" in ha_name:
                features["charge_rate"] = True
            if "battery_discharge_power" in ha_name:
                features["discharge_rate"] = True
            if "inverter_charge_power_percentage" in ha_name or "charge_power_rate" in ha_name:
                features["charge_power_percent"] = True
            if "inverter_discharge_power_percentage" in ha_name or "discharge_power_rate" in ha_name:
                features["discharge_power_percent"] = True
            if "pause_battery_start_time" in ha_name:
                features["pause_start_time"] = True
            if "dc_discharge_1_lower_soc_percent_limit" in ha_name:
                features["discharge_target_soc"] = True
            if "pause_battery" in ha_name:
                features["pause_battery"] = True
        return features

    def _first_register(self, device, candidates):
        """The first candidate register name this device's own settings report, or None - the choice async_automatic_config()'s first_existing_entity() makes."""
        names = {regname_to_ha(register.get("name", "")) for register in self.settings.get(device, {}).values()}
        return next((candidate for candidate in candidates if candidate in names), None)

    def _shared_ct(self, devices, batteries):
        """
        Whether async_automatic_config() reads grid and load from the first inverter only, as it does
        when several inverters share one CT clamp. A copy of its detection (a duplicated meter serial)
        and of its two override settings, split winning over shared.
        """
        if len(batteries) <= 1 or devices.get("ems"):
            return False
        battery_meters = devices.get("battery_meters") or {}
        meter_serials = []
        for battery in batteries:
            meter_serials.extend(battery_meters.get(battery) or [])
        if self.get_arg("ge_cloud_automatic_split_ct", default=False):
            return False
        if self.get_arg("ge_cloud_automatic_shared_ct", default=False):
            return True
        return len(meter_serials) != len(set(meter_serials))

    def _export_limit_share(self, device):
        """This logical inverter's equal share of the site grid export limit - the value publish_site_export_limit() publishes - or None."""
        if self.site_export_limit is None or device not in self.site_inverters:
            return None
        return dp2(self.site_export_limit / len(self.site_inverters))

    def _device_entities(self, devices, batteries, index, inverter_type):
        """
        The `entities` discovery container for logical inverter `index`: every setting
        async_automatic_config() binds for it, keyed by Predbat setting name.

        Built with the entity-name patterns and the per-device register choice async_automatic_config()
        uses, applied in its order and with its later overrides (the shared-CT and EMS
        reconfigurations), so each descriptor records the entity actually chosen for this device. Where
        it binds a list shorter than the fleet (import_today on a shared CT), this device has no
        entry; where it binds 0 in place of a sensor, the descriptor is a value stand-in.

        Two deliberate differences, both spec decisions: the pause, discharge-target and percentage
        rate controls follow this device's own registers, not automatic_config()'s "any device has it"
        gate (D10), and load_today is carried even when the user set ge_cloud_load_today_ignore (D11).

        Left out: settings inverter.py replaces with a dummy for this type
        (GE_CLOUD_DUMMIED_SETTINGS), and those that are not facts about the inverter (inverter_type,
        num_inverters, ge_cloud_serial, ge_cloud_data, givtcp_rest and the None resets).
        """
        device = batteries[index]
        stem = "{}_gecloud_".format(self.prefix)
        features = self._register_features(device)
        time_select = {"domain": "select", "format": GE_CLOUD_TIME_SELECT_FORMAT}
        entities = {}

        def sensor(setting, serial, suffix, **fields):
            """Bind a setting Predbat only reads to one of this component's sensors."""
            entities[setting] = dict({"entity_id": "sensor.{}{}_{}".format(stem, serial, suffix), "access": "r"}, **fields)

        def control(setting, platform, serial, suffix, **fields):
            """Bind a setting Predbat writes to one of this component's register entities."""
            entities[setting] = dict({"entity_id": "{}.{}{}_{}".format(platform, stem, serial, suffix), "access": "rw"}, **fields)

        def register(setting, platform, candidates, **fields):
            """Bind the first candidate register this device reports, as build_entities() does; nothing when it reports none."""
            name = self._first_register(device, candidates)
            if name is not None:
                control(setting, platform, device, name, **fields)

        register("inverter_mode", "switch", ["enable_eco_mode"], domain="switch")
        sensor("load_today", device, "consumption_total")
        sensor("import_today", device, "grid_import_total")
        sensor("export_today", device, "grid_export_total")
        register("charge_rate", "number", ["battery_charge_power"], unit="W")
        sensor("battery_rate_max", device, "max_charge_rate")
        register("discharge_rate", "number", ["battery_discharge_power"], unit="W")
        sensor("battery_power", device, "battery_power")
        sensor("load_power", device, "consumption_power")
        sensor("grid_power", device, "grid_power")
        sensor("soc_percent", device, "battery_percent")
        sensor("soc_max", device, "battery_size")
        register("reserve", "number", ["battery_reserve_percent_limit", "battery_reserve_percent"])
        sensor("inverter_time", device, "time", format=GE_CLOUD_CLOCK_FORMAT)
        control("charge_start_time", "select", device, "ac_charge_1_start_time", **time_select)
        control("charge_end_time", "select", device, "ac_charge_1_end_time", **time_select)
        register("charge_limit", "number", ["ac_charge_upper_percent_limit", "ac_charge_1_upper_soc_percent_limit"])
        register("charge_limit_enable", "switch", ["enable_ac_charge_upper_percent_limit", "enable_ac_charge_1_upper_soc_percent_limit"])
        control("discharge_start_time", "select", device, "dc_discharge_1_start_time", **time_select)
        control("discharge_end_time", "select", device, "dc_discharge_1_end_time", **time_select)
        register("scheduled_charge_enable", "switch", ["enable_force_charge", "ac_charge_enable", "enable_ac_charge"])
        register("scheduled_discharge_enable", "switch", ["enable_dc_discharge", "enable_force_discharge"])
        sensor("battery_temperature", device, "battery_temperature")
        sensor("battery_scaling", device, "battery_dod_soh")
        sensor("inverter_limit", device, "max_inverter_rate")
        if self.site_export_limit is not None:
            sensor("export_limit", device, "export_limit")
        sensor("pv_today", device, "solar_total")
        sensor("pv_power", device, "solar_power")
        if index == 0:
            sensor("battery_temperature_history", device, "battery_temperature")
        if features["pause_battery"]:
            control("pause_mode", "select", device, "pause_battery", domain="select")
            if features["pause_start_time"]:
                control("pause_start_time", "select", device, "pause_battery_start_time", domain="select")
                control("pause_end_time", "select", device, "pause_battery_end_time", domain="select")
        if features["discharge_target_soc"]:
            control("discharge_target_soc", "number", device, "dc_discharge_1_lower_soc_percent_limit")
        if features["charge_power_percent"] and not features["charge_rate"]:
            register("charge_rate_percent", "number", ["inverter_charge_power_percentage", "charge_power_rate"], unit="%")
        if features["discharge_power_percent"] and not features["discharge_rate"]:
            register("discharge_rate_percent", "number", ["inverter_discharge_power_percentage", "discharge_power_rate"], unit="%")

        if self._shared_ct(devices, batteries) and index > 0:
            entities["grid_power"] = {"value": 0, "access": "r"}
            entities["load_power"] = {"value": 0, "access": "r"}
            for setting in ("import_today", "export_today", "load_today"):
                entities.pop(setting, None)

        ems = devices.get("ems")
        if ems:
            for setting, suffix in (("load_today", "consumption_total"), ("import_today", "grid_import_total"), ("export_today", "grid_export_total"), ("pv_today", "solar_total")):
                entities.pop(setting, None)
                if index == 0:
                    sensor(setting, ems, suffix)
            control("charge_start_time", "select", ems, "charge_start_time_slot_1", **time_select)
            control("charge_end_time", "select", ems, "charge_end_time_slot_1", **time_select)
            control("idle_start_time", "select", ems, "discharge_start_time_slot_1", **time_select)
            control("idle_end_time", "select", ems, "discharge_end_time_slot_1", **time_select)
            control("charge_limit", "number", ems, "charge_soc_percent_limit_1")
            control("discharge_start_time", "select", ems, "export_start_time_slot_1", **time_select)
            control("discharge_end_time", "select", ems, "export_end_time_slot_1", **time_select)
            for setting, suffix in (("battery_power", "battery_power"), ("pv_power", "solar_power"), ("load_power", "consumption_power"), ("grid_power", "grid_power")):
                if index == 0:
                    sensor(setting, ems, suffix)
                else:
                    entities[setting] = {"value": 0, "access": "r"}

        for setting in GE_CLOUD_DUMMIED_SETTINGS[inverter_type]:
            entities.pop(setting, None)
        return entities
```

Replace `_device_info_and_ratings()` with:

```python
    def _device_info_and_ratings(self, device):
        """
        The `info` and `ratings` discovery containers for one device, from its own device info blob.

        Drawn from the same self.info[device] blob publish_info() already turns into HA
        attributes, so a fact reported here can never disagree with what was published.
        Firmware is reported there as a per-board {"ARM": n, "DSP": n} dict; the `info`
        container only accepts strings, so it is flattened into one descriptive string here
        rather than silently dropped by the catalogue's validator. Ratings are keyed by the
        Predbat setting they rate: battery_rate_max (W) is the figure the max_charge_rate sensor
        publishes, soc_max (kWh) the figure the battery_size sensor publishes.
        """
        device_info = self.info.get(device, {}) or {}
        fields = device_info.get("info", {}) or {}
        info = {}
        model = fields.get("model")
        if isinstance(model, str) and model:
            info["model"] = model
        firmware = device_info.get("firmware_version")
        if isinstance(firmware, dict) and firmware:
            info["firmware"] = " ".join("{} {}".format(board, version) for board, version in sorted(firmware.items()))

        ratings = {}
        max_charge_rate = fields.get("max_charge_rate")
        if isinstance(max_charge_rate, (int, float)) and not isinstance(max_charge_rate, bool):
            ratings["battery_rate_max"] = max_charge_rate
        battery = fields.get("battery", {}) or {}
        capacity, voltage = battery.get("nominal_capacity"), battery.get("nominal_voltage")
        if isinstance(capacity, (int, float)) and isinstance(voltage, (int, float)) and not isinstance(capacity, bool) and not isinstance(voltage, bool):
            ratings["soc_max"] = dp2(capacity * voltage / 1000.0)
        return info, ratings
```

Replace `build_discovery()` with:

```python
    def build_discovery(self):
        """
        Describe the discovered GE Cloud devices for the discovery catalogue.

        One inverter record per controlled battery device plus one per sensor-only PV device in
        `devices["pv"]` (`functions: ["solar"]`, no `inverter_type` - these are the extra devices
        `ge_cloud_automatic_split_pv` optionally wires in, but the catalogue reports them
        regardless of that flag, since it describes what is physically there, not how Predbat's
        apps.yaml happens to be wired).

        `composition` mirrors the exact precedence async_automatic_config() applies (its own
        `devices["ems"]` / `devices["gateway"]` checks at the top of that method, not
        re-implemented differently here): "ems" when an EMS device is present, "gateway" when a
        gateway fronts more than one battery - collapsing those battery records into one for the
        gateway itself, with the fronted serials recorded in the structural `serials` field -
        else "direct". async_automatic_config() itself is not called or modified.

        `measures_meter` is set from the device's own CT/meter serial where GE Cloud's device
        connections data reports one (see _meter_cross_link) - the same data
        async_automatic_config()'s shared-CT detection reads - so two devices sharing a meter
        serial show up in the catalogue as two inverters measuring the same meter. It is
        deliberately a dangling cross-link: `meters` is always returned empty here, since a CT
        clamp serial is not a utility supply point and does not fit that section's identity model
        (see _meter_cross_link).

        Each controlled record carries the type's literal `capabilities` (GE_CLOUD_CAPABILITIES),
        an `entities` map of every setting async_automatic_config() binds for that device (see
        _device_entities), and `ratings` under Predbat's setting names - including export_limit,
        this inverter's equal share of the site grid export limit (spec decision D7). PV-only
        records carry no capabilities - Predbat drives nothing on them - but do carry their
        generation, pv_power and pv_today, as read-only entities: the ids async_automatic_config()
        binds for them under ge_cloud_automatic_split_pv, reported whether or not that is set
        (spec D11, D12).

        Reporting is independent of self.automatic: the catalogue records what hardware GE Cloud
        found, not whether this component wired Predbat's apps.yaml to it - that distinction is
        what the report's own "automatic" flag is for.
        """
        devices = self.devices_dict or {}
        battery_devices = list(devices.get("battery") or [])
        gateway = devices.get("gateway")
        ems = devices.get("ems")

        composition = "direct"
        fronted_serials = None
        controlled = battery_devices
        if ems:
            composition = "ems"
        elif gateway and len(battery_devices) > 1:
            composition = "gateway"
            fronted_serials = list(battery_devices)
            controlled = [gateway]

        inverter_type = "GEE" if composition == "ems" else "GEC"

        inverters = []

        for index, device in enumerate(controlled):
            info, ratings = self._device_info_and_ratings(device)
            share = self._export_limit_share(device)
            if share is not None:
                ratings["export_limit"] = share
            inverters.append(
                inverter_record(
                    "gecloud:{}".format(device),
                    inverter_type=inverter_type,
                    composition=composition,
                    measures_meter=self._meter_cross_link(devices, device),
                    functions=["solar", "battery"],
                    capabilities=dict(GE_CLOUD_CAPABILITIES[inverter_type]),
                    hardware_ids={"serial": device},
                    serials=fronted_serials,
                    info=info,
                    ratings=ratings,
                    entities=self._device_entities(devices, controlled, index, inverter_type),
                )
            )

        for device in devices.get("pv") or []:
            info, ratings = self._device_info_and_ratings(device)
            stem = "sensor.{}_gecloud_{}".format(self.prefix, device)
            inverters.append(
                inverter_record(
                    "gecloud:{}".format(device),
                    composition="direct",
                    functions=["solar"],
                    hardware_ids={"serial": device},
                    info=info,
                    ratings=ratings,
                    entities={"pv_power": {"entity_id": stem + "_solar_power", "access": "r"}, "pv_today": {"entity_id": stem + "_solar_total", "access": "r"}},
                )
            )

        # Always empty - see _meter_cross_link for why a CT clamp does not become a
        # fabricated meters record.
        return {"automatic": self.automatic, "inverters": inverters, "meters": []}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test ge_cloud > /tmp/ge_cloud.log 2>&1; grep -n "✗\|RESULTS" /tmp/ge_cloud.log | cut -c1-200`
Expected: `RESULTS: 117 passed, 0 failed out of 117 tests`.

Run: `cd coverage && ./run_all --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test components --test ge_cloud --test inverter --test inverter_config_sensor > /tmp/task8.log 2>&1; grep -n "FAILED\|Traceback\|All tests passed" /tmp/task8.log | head`
Expected: `**** All tests passed ****`, no FAILED/Traceback.

- [ ] **Step 6: Pre-commit, detect_changes, commit**

Run: `coverage/venv/bin/pre-commit run --files apps/predbat/gecloud.py apps/predbat/config.py apps/predbat/tests/test_ge_cloud.py`
Expected: all hooks pass. `coverage/venv/bin/interrogate -v apps/predbat/gecloud.py` reports 100%.

Run `detect_changes()`. Expected changes:
- `gecloud.py`: the constants, `GECloudDirect`'s class attribute, and the discovery helpers from `_register_features` to `build_discovery`
- `config.py`: the two `soc_units` lines
- `tests/test_ge_cloud.py`

`async_automatic_config` must not appear: `git diff -U0 apps/predbat/gecloud.py | grep "^@@"` shows no hunk inside it.

```bash
git add apps/predbat/gecloud.py apps/predbat/config.py apps/predbat/tests/test_ge_cloud.py
git commit -m "feat(discovery): GE Cloud records carry entities, capabilities dict and Predbat-named ratings; GEC/GEE soc_units %

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: GivTCP reporter

**Files:**
- Modify: `apps/predbat/givtcp.py` (new module constants after `GIVTCP_AUTO_CONFIG_VOLTAGE_KEYS`, class constant `GivTCPComponent.WRITE_AND_POLL_SLEEP`, new `_record_entities`, `build_discovery`)
- Test: `apps/predbat/tests/test_givtcp_component.py`

**Interfaces:**
- Consumes: `inverter_record` (Task 1); `validated_inverters`, `assert_definition_complete(..., except_fields=)`, `capture_automatic_config`, `assert_record_agrees`, `assert_record_binds_nothing_extra(..., allowed_extra=)` (Task 2).
- Produces (in `givtcp.py`):
  - `GIVTCP_CAPABILITIES` - literal dict of the seven `CAPABILITY_KEYS`, matching the GE row
  - `GIVTCP_RECORD_DUMMIED_KEYS = ("scheduled_discharge_enable",)` - bound by `automatic_config()`, dummied by the GE row
  - `GIVTCP_RECORD_KEYS` - every key `automatic_config()` can bind, from its own key lists, less the dummied ones
  - `GIVTCP_INVERTER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"` - the shape of the published `inverter_time` value
  - `GivTCPComponent.WRITE_AND_POLL_SLEEP = 10`
  - `GivTCPComponent._record_entities(n, max_battery_rate) -> dict`
  - GivTCP inverter records: `capabilities` = `GIVTCP_CAPABILITIES`; `flags` `rest_v3`/`reports_soh`; `ratings` `soc_max` (only from GivTCP's own `Battery_Capacity_kWh`)/`battery_rate_max`/`inverter_limit`; `entities` = every setting `automatic_config()` can bind for that inverter that was published for it. Fleet-wide gates and user opt-outs are not applied (D10, D11)

**Decisions this task applies:**
- **D13, `clock_time_format`:** GivTCP publishes `Invertor_Time` unchanged, and GivTCP reports it as ISO 8601 with an offset (`"2024-12-30T13:07:22+00:00"` in both `coverage/cases/rest_v2.json` and `rest_v3.json`). The `inverter_time` descriptor therefore states `%Y-%m-%dT%H:%M:%S%z`. The GE row keeps `"%H:%M:%S"`, so completeness names `clock_time_format` in `except_fields`.
- **D10, per device:** `automatic_config()` claims the pause, export-target, discovery and schedule keys only when every discovered inverter qualifies. The record states what each inverter has, which a mixed v3 + v2 fleet test pins with the reverse check's `allowed_extra`.
- **D11, user opt-outs:** `givtcp_rest_power_ignore` stops `automatic_config()` binding the power and voltage keys, but the record still carries those entities.
- **D14, ratings:** `battery_rate_max` (`Invertor_Max_Bat_Rate`) and `inverter_limit` (`Invertor_Max_Inv_Rate`) are figures the device reports. `soc_max` is a rating only when GivTCP reports `Battery_Capacity_kWh` itself. `publish_data()`'s fallback, the nominal Ah scaled by an assumed 51.2 V pack voltage, is Predbat's derivation, so in that case the record keeps the `soc_max` entity binding and gives no rating.
- **Left out of `entities`:** `scheduled_discharge_enable`, which the GE row dummies (`has_discharge_enable_time` False). Also left out are published sensors that `automatic_config()` never binds. In particular `soc_percent` must stay out: `inverter.py` prefers `soc_percent` over `soc_kw`, and GivTCP binds `soc_kw` for its precision.

- [ ] **Step 1: Run impact analysis**

Run `impact({target: "build_discovery", direction: "upstream", file_path: "apps/predbat/givtcp.py"})` and the same for `_discovery_descriptor`. Expected: LOW. `build_discovery` is reached only through `ComponentBase.refresh_discovery()` from `run()`, and its failures are already contained there (`test_report_discovery_failure_does_not_degrade_component_health`). `_discovery_descriptor` has no callers outside `givtcp.py` and is left unchanged. `automatic_config()`, `publish_data()` and the write path are not touched.

- [ ] **Step 2: Write the failing tests**

In `apps/predbat/tests/test_givtcp_component.py`, replace the import block with:

```python
import json

from unittest.mock import MagicMock

from tests.test_infra import run_async
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from mock_base import MockBase
from givtcp import GivTCPComponent, GIVTCP_CAPABILITIES, GIVTCP_INVERTER_TIME_FORMAT, GIVTCP_POLL_SECONDS, GIVTCP_REDISCOVER_SECONDS, DISCHARGE_TARGET_UNSUPPORTED_MODELS, GIVTCP_CONTROLS, GIVTCP_SENSORS, GIVTCP_FRIENDLY_NAMES
```

Delete `test_build_discovery_capabilities_follow_the_same_probes_as_automatic_config` and `test_build_discovery_omits_capabilities_when_no_probe_applies`, which pinned the old token list. Put these two tests in their place:

```python
def test_build_discovery_flags_follow_the_same_probes_as_automatic_config(my_predbat=None):
    """rest_v3 and reports_soh are flags now, set per device from the same probes automatic_config() gates on; capabilities is the constant either way."""
    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob(version="2.4.0")
    _mark_discovered(component)
    run_async(component.publish_data())
    v2_record = component.build_discovery()["inverters"][0]
    assert "rest_v3" not in v2_record.get("flags", [])
    assert v2_record["capabilities"] == GIVTCP_CAPABILITIES
    for name in ("pause_mode", "pause_start_time", "pause_end_time", "discharge_target_soc"):
        assert name not in v2_record["entities"], "{} is v3 only".format(name)

    component.rest[0].inverter.rest_data = _rest_data_blob(version="3.0.4")
    run_async(component.publish_data())
    v3_record = component.build_discovery()["inverters"][0]
    assert "rest_v3" in v3_record["flags"]
    assert v3_record["capabilities"] == GIVTCP_CAPABILITIES
    for name in ("pause_mode", "pause_start_time", "pause_end_time", "discharge_target_soc"):
        assert v3_record["entities"][name]["access"] == "rw", "{} should be an rw control on v3".format(name)
    print("PASS: rest_v3 is a flag, and the v3-only controls are entities")
    return 0


def test_build_discovery_capabilities_are_the_constant_even_without_probes(my_predbat=None):
    """
    A device none of the probes applies to still states all seven capabilities; only its empty flags container is omitted.

    capabilities describes the GE inverter's behaviour (its INVERTER_DEF row), not what GivTCP
    reported this poll, so it no longer shrinks to nothing on a v2 GivTCP with no battery module
    details and no Enable_Charge_Target register. The flags container is still omitted when empty,
    raw and in the catalogue.
    """
    from coordinator import validate_report

    base, component = _make_component(rest_urls=["http://a:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob(version="2.4.0", charge_target_enable=None)
    _mark_discovered(component)
    run_async(component.publish_data())
    rest = component.rest[0]
    assert not rest.rest_v3 and rest.battery_soh() is None and rest.charge_target_enabled is None, "the fixture must leave every probe false"

    report = component.build_discovery()
    record = report["inverters"][0]
    assert record["capabilities"] == GIVTCP_CAPABILITIES, record.get("capabilities")
    assert "flags" not in record, "an empty flags list is omitted from the raw record, got {}".format(record.get("flags"))
    assert "charge_limit_enable" not in record["entities"], "no Enable_Charge_Target register, no charge_limit_enable entity"

    cleaned = validate_report(report, "givtcp", print)["inverters"][0]
    assert cleaned["capabilities"] == GIVTCP_CAPABILITIES
    assert "flags" not in cleaned
    print("PASS: capabilities always carries the seven keys; empty flags omitted, raw and in the catalogue")
    return 0
```

In `test_build_discovery_round_trips_through_the_coordinator`, replace the capabilities assertion with:

```python
    assert record["capabilities"] == original["capabilities"] == GIVTCP_CAPABILITIES
    assert set(record["flags"]) == set(original["flags"]) == {"rest_v3", "reports_soh"}
```

and replace the two ratings assertions (`battery_kwh`, `max_charge_w`) with:

```python
    assert record["ratings"] == {"soc_max": 9.52, "battery_rate_max": 3600, "inverter_limit": 3600}
    assert "battery_kwh" not in record["ratings"] and "max_charge_w" not in record["ratings"]
```

Add these tests immediately before `test_report_discovery_failure_does_not_degrade_component_health`:

```python
def _v3_capture_record():
    """The real rest_v3.json capture published by a component, and the one validated inverter record build_discovery() makes of it."""
    base, component = _rest_from_fixture("cases/rest_v3.json")
    _mark_discovered(component)
    run_async(component.publish_data())
    records = validated_inverters(component.build_discovery())
    assert len(records) == 1, records
    return component, records[0]


def test_build_discovery_record_rebuilds_the_ge_row(my_predbat=None):
    """
    Completeness: the record alone rebuilds INVERTER_DEF["GE"], with clock_time_format the one named exception.

    Spec D13: GivTCP publishes Invertor_Time as ISO 8601 with an offset ("2024-12-30T13:07:22+00:00"
    in both captures), so the inverter_time descriptor states that format. The GE row keeps
    "%H:%M:%S" because it also serves installs that do not use this component, where it is the
    last-resort parse.
    """
    component, record = _v3_capture_record()
    assert record["entities"]["inverter_time"]["format"] == GIVTCP_INVERTER_TIME_FORMAT == "%Y-%m-%dT%H:%M:%S%z"
    assert_definition_complete(record, GivTCPComponent.WRITE_AND_POLL_SLEEP, except_fields=("clock_time_format",))
    print("PASS: GivTCP record rebuilds the GE row")
    return 0


def test_build_discovery_record_agrees_with_automatic_config(my_predbat=None):
    """Agreement: every setting automatic_config() binds for the device is in the record, bound to the same entity."""
    component, record = _v3_capture_record()
    captured = capture_automatic_config(component)
    # automatic_config() still claims it, but the GE row dummies it (has_discharge_enable_time False)
    assert "scheduled_discharge_enable" in captured and "scheduled_discharge_enable" not in record["entities"]
    assert_record_agrees(record, captured, index=0)
    assert_record_binds_nothing_extra(record, captured, index=0)
    print("PASS: GivTCP record agrees with automatic_config()")
    return 0


def test_build_discovery_two_inverters_get_their_own_entities(my_predbat=None):
    """Two discovered endpoints give two records whose entity ids differ, each agreeing with automatic_config() at its own index."""
    base, component = _make_component(rest_urls=["http://a:6345", "http://b:6345"])
    for rest in component.rest:
        rest.inverter.rest_data = _rest_data_blob(version="3.0.4")
    _mark_discovered(component)
    run_async(component.publish_data())
    records = validated_inverters(component.build_discovery())
    assert len(records) == 2, records
    first, second = records[0]["entities"], records[1]["entities"]
    assert first and set(first) == set(second), (sorted(first), sorted(second))
    for name in first:
        assert first[name]["entity_id"] != second[name]["entity_id"], name
    captured = capture_automatic_config(component)
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    print("PASS: two inverters, two records, distinct entity ids")
    return 0


def test_build_discovery_entities_are_only_what_automatic_config_binds(my_predbat=None):
    """
    Published sensors automatic_config() does not bind stay out of entities, keyed as they are by Predbat setting name.

    soc_percent matters most: Inverter prefers soc_percent over soc_kw when both are set, and GivTCP
    binds soc_kw deliberately for its precision (see GIVTCP_AUTO_CONFIG_KEYS), so a coordinator
    that bound every entity in the record would lose it. battery_scaling is bound to the combined
    depth-of-discharge x health sensor, as automatic_config() binds it.
    """
    component, record = _v3_capture_record()
    entities = record["entities"]
    for name in ("soc_percent", "battery_rate_max", "battery_soh", "battery_dod", "battery_dod_soh", "load_total", "battery_charge_today", "givtcp_version", "serial_number", "scheduled_discharge_enable"):
        assert name not in entities, "{} is published but not bound by automatic_config()".format(name)
    assert entities["battery_scaling"] == {"entity_id": "sensor.predbat_givtcp_0_battery_dod_soh", "domain": "sensor", "access": "r"}, entities["battery_scaling"]
    assert entities["soc_kw"]["access"] == "r" and entities["charge_limit_enable"]["access"] == "rw"
    assert entities["inverter_mode"]["domain"] == "select" and entities["charge_start_time"]["domain"] == "select"
    print("PASS: entities hold only what automatic_config() binds")
    return 0


def test_build_discovery_power_ignore_keeps_the_power_entities(my_predbat=None):
    """
    Spec D11: givtcp_rest_power_ignore is the user's opt-out, not a fact about the device.

    automatic_config() leaves the power and voltage keys to the user's apps.yaml, but the record
    still describes the device's power sensors; piece 3's coordinator applies the opt-out.
    """
    power_keys = ("battery_power", "pv_power", "grid_power", "load_power", "battery_voltage")
    base, component = _rest_from_fixture("cases/rest_v3.json")
    base.args["givtcp_rest_power_ignore"] = True
    _mark_discovered(component)
    run_async(component.publish_data())
    record = validated_inverters(component.build_discovery())[0]
    for name in power_keys:
        assert record["entities"][name]["entity_id"] == "sensor.predbat_givtcp_0_{}".format(name), name
    captured = capture_automatic_config(component)
    assert not any(name in captured for name in power_keys), sorted(captured)
    assert_record_agrees(record, captured, index=0)
    assert_record_binds_nothing_extra(record, captured, index=0, allowed_extra=power_keys)
    print("PASS: power_ignore leaves the power entities in the record")
    return 0


def test_build_discovery_mixed_fleet_is_described_per_device(my_predbat=None):
    """
    Spec D10: a v3 inverter's record carries its pause and export-target controls even when a v2 neighbour withholds them fleet-wide.

    automatic_config() claims those keys only when every discovered inverter is v3, so on a v3 + v2
    fleet it binds none of them. The v3 record still lists them (allowed as extra), and the v2
    record - which has none - binds nothing automatic_config() did not.
    """
    v3_only = ("pause_mode", "pause_start_time", "pause_end_time", "discharge_target_soc")
    base, component = _make_component(rest_urls=["http://a:6345", "http://b:6345"])
    component.rest[0].inverter.rest_data = _rest_data_blob(version="3.0.4")
    component.rest[1].inverter.rest_data = _rest_data_blob(version="2.4.0")
    _mark_discovered(component)
    run_async(component.publish_data())
    v3_record, v2_record = validated_inverters(component.build_discovery())
    captured = capture_automatic_config(component)
    assert not any(name in captured for name in v3_only), sorted(captured)
    for name in v3_only:
        assert v3_record["entities"][name]["entity_id"] == "{}.predbat_givtcp_0_{}".format(GIVTCP_CONTROLS[name][0], name), name
        assert name not in v2_record["entities"], name
    assert "rest_v3" in v3_record["flags"] and "rest_v3" not in v2_record.get("flags", [])
    assert_record_agrees(v3_record, captured, index=0)
    assert_record_agrees(v2_record, captured, index=1)
    assert_record_binds_nothing_extra(v3_record, captured, index=0, allowed_extra=v3_only)
    assert_record_binds_nothing_extra(v2_record, captured, index=1)
    print("PASS: mixed v3/v2 fleet described per device")
    return 0


def test_build_discovery_soc_max_rating_only_when_givtcp_reports_it(my_predbat=None):
    """
    Spec D14: soc_max is a rating only when GivTCP reports Battery_Capacity_kWh itself.

    Without it, publish_data() falls back to the nominal Ah scaled by an assumed 51.2V pack voltage
    (GivTCPRest.nominal_capacity()). That figure is Predbat's derivation, not the device's, so the
    record keeps the soc_max entity binding but gives no soc_max rating.
    """
    base, component = _make_component()
    blob = _rest_data_blob()
    blob["raw"] = {"invertor": {"battery_nominal_capacity": 186}}
    component.rest[0].inverter.rest_data = blob
    _mark_discovered(component)
    run_async(component.publish_data())
    assert component.rest[0].battery_capacity_kwh() is None
    assert base.entities["sensor.predbat_givtcp_0_soc_max"]["state"] == 186 / 19.53125, "the derived capacity is still published"
    record = validated_inverters(component.build_discovery())[0]
    assert record["entities"]["soc_max"]["entity_id"] == "sensor.predbat_givtcp_0_soc_max"
    assert "soc_max" not in record.get("ratings", {}), record.get("ratings")
    assert_record_agrees(record, capture_automatic_config(component), index=0)

    blob["Invertor_Details"] = {"Battery_Capacity_kWh": 9.52}
    run_async(component.publish_data())
    assert validated_inverters(component.build_discovery())[0]["ratings"]["soc_max"] == 9.52
    print("PASS: soc_max rating only from GivTCP's own Battery_Capacity_kWh")
    return 0


def test_givtcp_write_and_poll_sleep_matches_the_ge_row(my_predbat=None):
    """The component owns its write/poll timing: 10s, the GE row's figure, not ComponentBase's 2."""
    assert GivTCPComponent.WRITE_AND_POLL_SLEEP == 10
    print("PASS: GivTCP write_and_poll_sleep is 10")
    return 0
```

In `test_givtcp_component()`'s `sub_tests`, replace the `discovery_capabilities` and `discovery_no_capabilities` entries with:

```python
        ("discovery_flags", test_build_discovery_flags_follow_the_same_probes_as_automatic_config, "rest_v3/reports_soh flags follow automatic_config()'s own probes"),
        ("discovery_capabilities_constant", test_build_discovery_capabilities_are_the_constant_even_without_probes, "capabilities always carries the seven keys; empty flags omitted"),
```

and insert these entries just before `("discovery_report_failure_contained", ...)`:

```python
        ("discovery_completeness", test_build_discovery_record_rebuilds_the_ge_row, "record rebuilds the GE row"),
        ("discovery_agreement", test_build_discovery_record_agrees_with_automatic_config, "record agrees with automatic_config()"),
        ("discovery_two_inverters", test_build_discovery_two_inverters_get_their_own_entities, "two inverters, two records, distinct entity ids"),
        ("discovery_bound_only", test_build_discovery_entities_are_only_what_automatic_config_binds, "entities hold only what automatic_config() binds"),
        ("discovery_power_ignore", test_build_discovery_power_ignore_keeps_the_power_entities, "power_ignore leaves the power entities in the record"),
        ("discovery_mixed_fleet", test_build_discovery_mixed_fleet_is_described_per_device, "mixed v3/v2 fleet described per device"),
        ("discovery_soc_max_rating", test_build_discovery_soc_max_rating_only_when_givtcp_reports_it, "soc_max rating only from GivTCP's own Battery_Capacity_kWh"),
        ("discovery_write_and_poll_sleep", test_givtcp_write_and_poll_sleep_matches_the_ge_row, "write_and_poll_sleep is the GE row's 10"),
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd coverage && ./run_all --test givtcp_component > /tmp/givtcp_component.log 2>&1; grep -n "FAIL\|Error\|Traceback" /tmp/givtcp_component.log | head`
Expected: `ImportError: cannot import name 'GIVTCP_CAPABILITIES' from 'givtcp'`.

- [ ] **Step 4: Implement in `givtcp.py`**

Add these module constants directly after `GIVTCP_AUTO_CONFIG_VOLTAGE_KEYS` (they are built from the `GIVTCP_AUTO_CONFIG_*` lists above them):

```python
# The behaviour of a GivTCP-driven GivEnergy inverter, as the discovery record's capabilities: the
# seven behaviour keys of the GE INVERTER_DEF row, stated here rather than read back from it so the
# completeness test proves the record can stand in for the row (vocabulary spec section 1.1).
GIVTCP_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": False,
    "can_span_midnight": True,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": False,
}

# Settings automatic_config() binds that the discovery record leaves out, because inverter.py
# replaces the binding with a dummy entity for a GE inverter: its row has has_discharge_enable_time
# False, so inverter.py:620-622 never reads or writes the published scheduled_discharge_enable switch.
GIVTCP_RECORD_DUMMIED_KEYS = ("scheduled_discharge_enable",)

# Every setting automatic_config() can bind for one inverter, in its own key lists, less the ones
# the record leaves out. build_discovery() keeps the ones actually published for that inverter.
GIVTCP_RECORD_KEYS = [
    key
    for key in (
        GIVTCP_AUTO_CONFIG_KEYS
        + GIVTCP_AUTO_CONFIG_POWER_KEYS
        + GIVTCP_AUTO_CONFIG_VOLTAGE_KEYS
        + GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS
        + GIVTCP_AUTO_CONFIG_SCHEDULE_KEYS
        + GIVTCP_AUTO_CONFIG_SCALING_KEYS
        + GIVTCP_AUTO_CONFIG_CHARGE_ENABLE_KEYS
        + GIVTCP_AUTO_CONFIG_DISCHARGE_TARGET_KEYS
        + GIVTCP_AUTO_CONFIG_PAUSE_MODE_KEYS
        + GIVTCP_AUTO_CONFIG_PAUSE_SLOT_KEYS
    )
    if key not in GIVTCP_RECORD_DUMMIED_KEYS
]

# The shape of the inverter_time sensor's value: publish_data() passes GivTCP's Invertor_Time
# through unchanged, and GivTCP reports it as ISO 8601 with an offset ("2024-12-30T13:07:22+00:00"
# in both the v2 and v3 captures under coverage/cases). The GE row's clock_time_format stays
# "%H:%M:%S" - it also serves installs that do not use this component (spec D13).
GIVTCP_INVERTER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
```

Add the class constant to `GivTCPComponent`, directly under its class docstring and above `initialize`:

```python
    # The GE INVERTER_DEF row's write_and_poll_sleep: GivTCP applies a write on its own poll cycle,
    # so inverter.py waits longer before reading a setting back than it does for a cloud API.
    WRITE_AND_POLL_SLEEP = 10
```

Replace `build_discovery` with the following, which also adds `_record_entities` before it. `_discovery_descriptor` is unchanged.

```python
    def _record_entities(self, n, max_battery_rate):
        """
        The discovery record's entity map for inverter n: each setting automatic_config() binds, keyed by that setting.

        Walks the same key lists automatic_config() does (GIVTCP_RECORD_KEYS) and forms each entity id
        with the same _entity_id() call, so the two cannot drift. A key is kept only when
        publish_data() actually published its entity for this inverter (see _discovery_descriptor).
        battery_scaling is bound to the combined battery_dod_soh sensor, exactly as automatic_config()
        binds it, and inverter_time carries the format its value is published in.

        The record describes this device alone (spec D10 and D11). automatic_config()'s fleet-wide
        gates - a key claimed only when every discovered inverter published it or runs GivTCP v3 -
        and the user's givtcp_rest_power_ignore opt-out are not applied here; the coordinator that
        configures from records applies them.
        """
        entities = {}
        for key in GIVTCP_RECORD_KEYS:
            if key == "battery_scaling":
                descriptor = self._discovery_descriptor(n, "battery_dod_soh", "sensor", "r", GIVTCP_SENSORS["battery_dod_soh"], max_battery_rate)
            elif key in GIVTCP_CONTROLS:
                domain, _, attrs = GIVTCP_CONTROLS[key]
                descriptor = self._discovery_descriptor(n, key, domain, "rw", attrs, max_battery_rate)
            else:
                descriptor = self._discovery_descriptor(n, key, "sensor", "r", GIVTCP_SENSORS[key], max_battery_rate)
            if descriptor is None:
                continue
            if key == "inverter_time":
                descriptor["format"] = GIVTCP_INVERTER_TIME_FORMAT
            entities[key] = descriptor
        return entities

    def build_discovery(self):
        """
        Describe the discovered inverters for the discovery catalogue.

        One record per REST endpoint that actually answered discovery (self.discovered), never the
        full configured list - the shipped apps.yaml deliberately over-provisions givtcp_rest with
        placeholder URLs that were never adopted. device_id is "givtcp:{serial}", falling back to
        "givtcp:{rest_api}" when the inverter reports no serial - the same fallback identity
        publish_data()'s own identity entities would show as "Unknown".

        entities holds every setting automatic_config() binds for the inverter (_record_entities),
        only where publish_data() actually published the entity this run, so the catalogue never
        lists a control or sensor that does not exist in Home Assistant. capabilities is the GE
        inverter's behaviour (GIVTCP_CAPABILITIES). flags record the per-inverter probes: rest_v3
        (GivTCP v3, which the pause and export-target controls need) and reports_soh (battery state
        of health reported, which battery_scaling needs). ratings use Predbat's setting names:
        soc_max (the design capacity in kWh, only as GivTCP reports it), battery_rate_max and
        inverter_limit (W).

        Reporting is independent of self.automatic: the catalogue records what hardware is
        physically there, not whether this component wired Predbat's apps.yaml to it - that
        distinction is what the report's own "automatic" flag is for, not a gate on reporting here.
        """
        inverters = []
        for n in self.discovered:
            rest = self.rest[n]
            serial = rest.serial_number
            known_serial = serial if serial and serial != "Unknown" else None
            device_id = "givtcp:{}".format(known_serial or rest.inverter.rest_api)

            max_battery_rate = rest.max_battery_rate()
            entities = self._record_entities(n, max_battery_rate)

            flags = []
            if rest.rest_v3:
                flags.append("rest_v3")
            if rest.battery_soh() is not None:
                flags.append("reports_soh")

            info = {}
            model = rest.inverter_type()
            if model:
                info["model"] = model
            if rest.firmware_version and rest.firmware_version != "Unknown":
                info["firmware"] = rest.firmware_version
            if rest.givtcp_version and rest.givtcp_version != "Unknown":
                info["givtcp_version"] = rest.givtcp_version

            # Ratings are figures the device reports (spec D14). soc_max is one only when GivTCP
            # reports Battery_Capacity_kWh itself: publish_data()'s fallback, the nominal Ah scaled by
            # an assumed 51.2V pack voltage, is Predbat's derivation, so it stays an entity binding only.
            ratings = {}
            reported_capacity = rest.battery_capacity_kwh()
            if reported_capacity:
                ratings["soc_max"] = reported_capacity
            if max_battery_rate:
                ratings["battery_rate_max"] = max_battery_rate
            max_inverter_rate = rest.max_inverter_rate()
            if max_inverter_rate:
                ratings["inverter_limit"] = max_inverter_rate

            inverters.append(
                inverter_record(
                    device_id,
                    inverter_type="GE",
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=dict(GIVTCP_CAPABILITIES),
                    flags=flags,
                    hardware_ids={"serial": known_serial} if known_serial else None,
                    info=info,
                    ratings=ratings,
                    entities=entities,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd coverage && ./run_all --test givtcp_component --test givtcp_rest --test coordinator --test discovery_contract --test web_discovery --test discovery_catalogue --test components > /tmp/givtcp.log 2>&1; grep -n "FAIL\|Traceback" /tmp/givtcp.log | head; grep -n "RESULTS\|\*\*\*\* Test" /tmp/givtcp.log`
Expected: `RESULTS: 115 passed, 0 failed out of 115 tests` for `givtcp_component`, and every suite reported `PASSED`.

- [ ] **Step 6: Commit**

Run `detect_changes()` first. Expected: only `build_discovery`, the new `_record_entities`, the new module constants and the GivTCP tests. pre-commit's ruff removes any import the tests no longer use, so re-stage after it runs.

```bash
git add apps/predbat/givtcp.py apps/predbat/tests/test_givtcp_component.py
git commit -m "feat(discovery): GivTCP record states the GE row's capabilities, bound entities and Predbat-named ratings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Retire the legacy capability list; web page fixtures and docs

**Files:**
- Modify: `apps/predbat/coordinator.py` (remove `LEGACY_TOKEN_CONTAINERS` and its two uses)
- Modify: `apps/predbat/tests/test_coordinator.py` (replace the transitional test)
- Modify: `apps/predbat/tests/test_web_discovery.py` (`SAMPLE_REPORT` and one expected string)
- Modify: `docs/discovery-catalogue.md`
- Modify: `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md` (inverters table, two rows)

**Interfaces:**
- Consumes: Tasks 1-9 (every reporter now emits a dict `capabilities`).
- Produces: schema 2 with no transitional path - a list-shaped `capabilities` no longer validates.

- [ ] **Step 1: Confirm nothing still emits a capability list, and run impact analysis**

Run: `git grep -n "capabilities=\[\|capabilities = \[\|\"capabilities\": \[" -- 'apps/predbat/*.py' ':!apps/predbat/tests/*'`
Expected: no output. If any reporter still builds a list, stop - its task is unfinished.

Run `impact` upstream on `_validate_container` and `Redactor._walk`; report the risk level.

- [ ] **Step 2: Write the failing test**

In `apps/predbat/tests/test_coordinator.py`, replace `test_capabilities_legacy_token_list_still_accepted` with:

```python
def test_capabilities_list_is_dropped():
    """Schema 2: capabilities is a dict of the seven behaviour keys, so a token list no longer validates."""
    record, _ = _one_inverter(capabilities=["schedule", "target_soc"])
    assert "capabilities" not in record, record
    base, coordinator = _coordinator()
    coordinator.report("test", {"inverters": [{"device_id": "test:SN1", "capabilities": ["schedule"]}]})
    assert "capabilities" not in coordinator.catalogue()["inverters"][0]
    print("PASS: capability token list dropped")
    return 0
```

and in `test_coordinator_all()` replace `failures += test_capabilities_legacy_token_list_still_accepted()` with `failures += test_capabilities_list_is_dropped()`.

In `apps/predbat/tests/test_web_discovery.py`, change the inverter in `SAMPLE_REPORT` to the schema-2 shapes:

```python
            "capabilities": {"support_charge_freeze": True, "can_span_midnight": True},
            "flags": ["rest_v3"],
            "hardware_ids": {"serial": "CE2143G123"},
            "info": {"model": "Gen3", "firmware": "D0.450"},
            "ratings": {"soc_max": 9.5, "battery_rate_max": 3000},
```

and in `test_discovery_page_renders_the_catalogue` extend the expected strings:

```python
        for expected in ("Inverters", "Meters", "givtcp:CE2143G123", "Gen3", "D0.450", "rest_v3", "support_charge_freeze", "soc_max"):
```

- [ ] **Step 3: Run the tests to verify the coordinator test fails**

Run: `cd coverage && ./run_all --test coordinator --test web_discovery > /tmp/task10.log 2>&1; grep -n "FAILED\|AssertionError" /tmp/task10.log | head`
Expected: `test_capabilities_list_is_dropped` fails (the legacy path still keeps the list); `web_discovery` passes.

- [ ] **Step 4: Remove the transitional path from `coordinator.py`**

Delete the `LEGACY_TOKEN_CONTAINERS` constant and its comment. In `Redactor._walk` the vocabulary branch becomes:

```python
                elif key in CLEAR_CONTAINERS and isinstance(value, dict):
                    out[key] = {self._guard_key(key, name): self._guard_value(key, name, entry) for name, entry in value.items()}
                elif key in VOCAB_CONTAINERS:
                    # Vocabulary lists are clear too, and a token is free-form enough (digits
                    # are legal in the pattern) that a misfiled identifier can hide as one.
                    out[key] = [self._guard_scalar(key, "token", entry) for entry in value]
```

Keep the `isinstance(value, dict)` guard on the clear-container branch: a malformed non-dict under a clear container name must still fall through to the generic walk rather than raise.

In `_validate_container` the first line becomes:

```python
    if container_name in VOCAB_CONTAINERS:
```

- [ ] **Step 5: Run the tests to verify they pass, then the quick suite**

Run: `cd coverage && ./run_all --test coordinator --test web_discovery --test discovery_contract > /tmp/task10.log 2>&1; grep -n "FAILED\|Traceback" /tmp/task10.log | head`
Expected: none.

Run: `cd coverage && ./run_all --quick > /tmp/quick.log 2>&1; grep -n "FAILED\|Traceback" /tmp/quick.log | head; tail -3 /tmp/quick.log`
Expected: `All tests passed`. (Between 00:00 and 01:00 BST `test_teslemetry_local_weekday_follows_the_base_clock` fails for an unrelated clock reason - see the debug journal; run outside that hour.)

- [ ] **Step 6: Update the user docs (`docs/discovery-catalogue.md`)**

Replace the `inverters` row of "What each section describes" with:

```markdown
| `inverters` | Battery inverters and PV-only devices - type, composition (direct/gateway/EMS), which functions it serves (`solar`, `battery`), its behaviour (`capabilities`), its fixed ratings under Predbat's own setting names, and every setting Predbat's automatic configuration binds for it (`entities`) |
```

In the paragraph after that table, change "no `battery_kwh` or `battery_capacity_ah`" to "no `soc_max` or `battery_capacity_ah`".

Replace the container table's `ratings`, `entities` and token rows with:

```markdown
| `ratings` | Numbers and booleans, keyed by Predbat's setting name where one exists (`inverter_limit`, `export_limit`, `import_limit`, `battery_rate_max`, `soc_max`, `battery_min_soc`) | Clear |
| `entities` | An entity descriptor: `access` (`rw` or `r`, required), exactly one of `entity_id` or a fixed `value`, and typed fields such as `domain`, `unit`, `format`, `min`/`max`, `invert` | Clear |
| `capabilities` | `true`/`false` for the seven behaviour keys of an inverter definition (`support_charge_freeze`, `support_discharge_freeze`, `support_feedin_first`, `can_span_midnight`, `charge_discharge_with_rate`, `charge_control_immediate`, `target_soc_used_for_discharge`) | Clear |
| `account_ids` | Any scalar | Pseudonymised |
| `functions` / `flags` / `effects` | Lists of short lowercase tokens | Clear |
```

(keeping the existing `hardware_ids`, `info` and `coverage` rows as they are, and removing the old `account_ids` and token rows these replace).

Add this subsection immediately before `### Writing a reporter`:

```markdown
### Inverter records and inverter definitions

An inverter record holds enough to rebuild that inverter's definition - the per-type table
(`INVERTER_DEF`) that tells Predbat how to drive it. `capabilities` carries its behaviour; whether it
has a reserve, a target SoC, charge/discharge enable switches, idle times or a timed pause follows
from which settings `entities` binds to a writable (`rw`) entity; and protocol detail such as the time
format or whether the charge rate is set in watts or amps is read from those entities' descriptors.
A setting Predbat replaces with a placeholder for this inverter type - SolisCloud's `reserve`, say - is
left out of `entities`. See the design in
`docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md`.
```

- [ ] **Step 7: Amend the catalogue design spec**

In `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md`, replace the two `inverters` rows:

```markdown
| `inverter_limit`, `export_limit`, `import_limit`, `battery_rate_max`, `soc_max`, `battery_min_soc`, `modules`, `dod_percent`, `nominal_voltage` | `ratings` | Renamed to Predbat setting names on 24 Sep 2026 (was `battery_kwh`, `max_charge_w`) - see `2026-09-24-discovery-inverter-record-vocabulary-design.md` |
| `capabilities` | dict of bool | Superseded on 24 Sep 2026: the seven `INVERTER_DEF` behaviour keys as `True`/`False`, not tokens - see `2026-09-24-discovery-inverter-record-vocabulary-design.md` |
```

- [ ] **Step 8: Pre-commit, detect_changes, commit**

Run: `coverage/venv/bin/pre-commit run --files apps/predbat/coordinator.py apps/predbat/tests/test_coordinator.py apps/predbat/tests/test_web_discovery.py docs/discovery-catalogue.md docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md`
Expected: all hooks pass (re-stage `.cspell/custom-dictionary-workspace.txt` if the sort hook rewrote it).

Run `detect_changes()`; expected: only the files above.

```bash
git add apps/predbat/coordinator.py apps/predbat/tests/test_coordinator.py apps/predbat/tests/test_web_discovery.py docs/discovery-catalogue.md docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md
git commit -m "feat(discovery): drop the transitional capability list; document schema 2

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
