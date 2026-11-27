# Discovery Automatic Inverter Configuration (GE Cloud proof of concept) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The discovery coordinator builds a per-slot bindings structure from component reports and replays it into args, with GE Cloud as the first opted-in component and its old automatic configuration kept as a checker.

**Architecture:** A new pure module `bindings.py` holds the three steps that need no state: detect manual slots, build the bindings (allocation), and replay them into args incrementally. `Coordinator` owns the state (who opted in, the device-to-slot map, the last bindings) and the triggers. GE Cloud's `async_automatic_config()` is split into a pure `_legacy_bindings()` plus live side effects; with the switch on it opts in and `check_automatic_config()` compares the legacy result with what was replayed. Separately, `inverter.py` stops assuming `GE` for a missing `inverter_type`.

**Tech Stack:** Python 3, the repo's own test runner (`coverage/run_all`, tests registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`), no new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-14-discovery-automatic-inverter-config-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Work on a new branch `feat/discovery-automatic-config` cut from `main`. Commit the spec and this plan as the first commit.
- Run tests from `coverage/`, always to a file, then grep the file: `./run_all --test <name> > out_<name>.txt 2>&1; echo exit=$?` then `grep -n "ERROR\|Traceback\|FAIL" out_<name>.txt`. Never pipe test output straight into grep. A pass is `exit=0` and no matching lines.
- Before editing any existing function, class or method run GitNexus `impact({target, direction: "upstream"})` and report the blast radius; stop and tell the user on HIGH or CRITICAL. Run `detect_changes()` before each commit.
- Every function and class needs a docstring (`interrogate` is at 100%). British English in comments, docs and log text. Line length 250. `lower_case_with_underscores`.
- New test modules follow the repo pattern: `tests/test_<feature>.py` exporting one entry function taking `my_predbat` and returning 0 on success, registered in `TEST_REGISTRY` as `("name", func, "description", False)`.
- A test that changes shared state on `my_predbat` (args, module-level dicts such as `INVERTER_DEF`) restores it before returning.
- Components read configuration through `get_arg()`, never `self.base.<attr>`.
- With `discovery_automatic` unset or `False`, no code path added here writes to args. The only behaviour change with the switch off is Task 1.
- The checker and the coordinator's `configure()` never raise and never call `non_fatal_error_occurred()` or `record_status(..., had_errors=True)`; they only log.
- `./run_pre_commit` passes before the final commit; add new words to `.cspell/custom-dictionary-workspace.txt` and re-stage it.
- End each commit message with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Review Focus

The inputs the spec implies that are most likely to bite, each pinned by a test in the task named:

1. **An `apps.yaml` setting written as a scalar, not a list, on an install that gains a second slot.** A reasonable person expects inverter 0 to keep its value. Task 4, `test_replay_scalar_becomes_list`.
2. **GE Cloud's first report arrives before its registers are read, so the record binds little, and the next report fills in.** Expected: no error, the slot is kept, the second pass writes only the new entries, and the checker does not repeat a warning it already gave. Task 5 `test_configure_fills_in_on_second_report`, Task 6 `test_checker_warns_once`.
3. **A report filed from a component thread while the main thread runs a startup pass.** Expected: passes never interleave their writes. Task 5, `test_configure_is_serialised`.
4. **`num_inverters: 2` in `apps.yaml` with only one `inverter_type` entry and nothing discovered.** Expected: a clear error naming inverter 1, not a silent GivEnergy guess. Task 1, `test_inverter_type_missing_second_slot`.
5. **A manual slot bound through an unresolved `re:` pattern.** Expected: it still counts as a control method, so the slot is manual and is not handed to a discovered inverter. Task 2, `test_manual_slot_regex_binding`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `apps/predbat/bindings.py` (new) | Pure functions and constants: binding setting names, manual slot detection, serial scan, allocation into a bindings dict, incremental replay. No coordinator, no component, no logging of its own. |
| `apps/predbat/coordinator.py` | State and triggers: `allow_automatic()`, `configure()`, the change trigger in `report()`, slots and bindings in the catalogue. |
| `apps/predbat/component_base.py` | `allow_automatic_config()` wrapper. |
| `apps/predbat/config.py` | `discovery_automatic` in `APPS_SCHEMA`. |
| `apps/predbat/predbat.py` | The two startup passes in `initialize()`. |
| `apps/predbat/gecloud.py` | `_legacy_bindings()`, the opt-in, `check_automatic_config()`, `GE_CLOUD_KNOWN_DIFFERENCES`. |
| `apps/predbat/inverter.py`, `apps/predbat/plan.py` | The assumed `GE` type removed. |
| `apps/predbat/tests/test_bindings.py` (new) | Unit tests for `bindings.py`. |
| `apps/predbat/tests/test_discovery_automatic.py` (new) | Coordinator trigger tests and the acceptance tests. |
| `apps/predbat/tests/test_ge_cloud.py`, `test_inverter.py`, `test_single_debug.py`, `test_infra.py`, `discovery_contract.py` | Existing tests and helpers adjusted. |

---

### Task 1: Retire the assumed inverter type

**Files:**
- Modify: `apps/predbat/inverter.py` (the `inverter_type` read near line 543; `inverter_source_name()`, `inverter_type_is_assumed()`, `inverter_type_assumed_name()`, `inverter_source_hint()` near lines 160-215)
- Modify: `apps/predbat/plan.py` (the `get_arg("inverter_type", "GE", ...)` near line 459)
- Modify: `apps/predbat/tests/test_single_debug.py` (`load_debug_dump`)
- Modify: `apps/predbat/tests/test_infra.py` (where `create_predbat()` builds the test args)
- Test: `apps/predbat/tests/test_inverter.py` (replace `test_inverter_type_default_warning`, registered near line 5924)
- Docs: `docs/apps-yaml.md` (the `### inverter_type` section, which says it defaults to `GE`), `docs/inverter-setup.md` where it mentions the default

**Interfaces:**
- Produces: `Inverter` raises `ValueError` when `get_arg("inverter_type", None, indirect=False, index=self.id)` is empty. No other task depends on the names here.

- [ ] **Step 1: Write the failing tests** in `test_inverter.py`, replacing `test_inverter_type_default_warning` with `test_inverter_type_required(test_name, my_predbat)`, which saves and restores `my_predbat.args["inverter_type"]`, `givtcp_rest`, `num_inverters` and `my_predbat.components`, and checks:

```python
# 1. no type, no inverter component: the "must be set" error
my_predbat.args.pop("inverter_type", None)
try:
    Inverter(my_predbat, 0)
    failed = True  # must raise
except ValueError as e:
    assert "Inverter 0: inverter_type is not set in apps.yaml" in str(e)

# 2. no type, an inverter component active (stub components.inverter_source_active() -> True,
#    inverter_source_names() -> ["GivEnergy Cloud Direct"]): the "waiting" error
#    assert "Inverter 0: inverter_type is not set yet - waiting for GivEnergy Cloud Direct to configure this inverter" in str(e)

# 3. Review Focus 4 - test_inverter_type_missing_second_slot:
#    args["num_inverters"] = 2, args["inverter_type"] = ["GE"]; Inverter(my_predbat, 0) builds,
#    Inverter(my_predbat, 1) raises with "Inverter 1: inverter_type is not set in apps.yaml"

# 4. args["inverter_type"] = ["GE"]: builds, and no log line contains "assuming"
```

- [ ] **Step 2: Run to verify it fails.** `./run_all --test inverter > out_inverter.txt 2>&1; echo exit=$?`. Expected: the new test fails because `Inverter()` builds with an assumed type.

- [ ] **Step 3: Implement in `inverter.py`.** Read the type with a `None` default. When it is empty, build the message, call `self.base.log("Error: " + message)` and `self.base.record_status("Error: " + message, had_errors=True)`, then `raise ValueError(message)`. Exact messages:
  - With `self.inverter_source_active()`: `"Inverter {id}: inverter_type is not set yet - waiting for {names} to configure this inverter"`, where `names` is `", ".join(components.inverter_source_names())`.
  - Otherwise: `"Inverter {id}: inverter_type is not set in apps.yaml - set inverter_type to match your inverter, see the documentation"`.

  Delete `inverter_type_is_assumed()` and `inverter_type_assumed_name()`. `inverter_source_name()` loses its assumed-type branch; `inverter_source_hint()` always returns its final "check the {} credentials" sentence. In `plan.py` replace the `"GE"` default with `None` (the following `INVERTER_DEF.get(inverter_type, {})` already copes).

- [ ] **Step 4: Fix what relied on the default.**
  - `tests/test_infra.py`: set `inverter_type` to `["GE"]` once where the fixture's args are built, so tests that never named a type keep their behaviour.
  - `tests/test_single_debug.py` `load_debug_dump()`: after the dump is restored, if `"inverter_type"` is not in `my_predbat.args`, set it to `["GE"] * num_inverters` and print `"Note: debug file has no inverter_type, assuming GE for a legacy dump"`. This is the only place a default survives (decided by Trefor, 14 Nov 2026).
  - Grep `apps/predbat/tests` for `inverter_type_is_assumed`, `inverter_type_assumed_name` and `"assuming"`; update or remove each test of the retired wording.

- [ ] **Step 5: Run the suites this touches.** `./run_all --test inverter`, `./run_all --test debug_cases`, then `./run_all --quick`, each to its own file. Expected: all pass. A test outside `test_inverter.py` that fails for lack of a type gets the type in its own setup; list any such test in the commit message.

- [ ] **Step 6: Update the docs.** `docs/apps-yaml.md`: `inverter_type` no longer defaults to `GE`; it may be left out only when a component configures the inverter automatically (any `*_automatic` component, or `givtcp_rest`); otherwise Predbat stops with an error. Same correction in `docs/inverter-setup.md`. Add a breaking-change line where release notes are drafted if the repo has such a file; otherwise put it in the commit body.

- [ ] **Step 7: Commit.** `git commit -m "feat(inverter): error on a missing inverter_type instead of assuming GE"`

---

### Task 2: `bindings.py` - settings, manual slots and the serial scan

**Files:**
- Create: `apps/predbat/bindings.py`
- Create: `apps/predbat/tests/test_bindings.py`
- Modify: `apps/predbat/unit_test.py` (import and register `("bindings", test_bindings_all, "Discovery bindings: manual slots, allocation and replay", False)`)
- Modify: `apps/predbat/tests/discovery_contract.py` (import `SITE_SETTINGS` from `bindings` instead of defining it)
- Modify: `apps/predbat/tests/test_discovery_contract.py` (one new check, Step 5)

**Interfaces:**
- Produces, all in `bindings.py`:

```python
MAX_INVERTERS = 4
MIN_SERIAL_LENGTH = 6
SITE_SETTINGS = frozenset({"battery_temperature_history"})
COMPACT_SETTINGS = ("load_today", "import_today", "export_today", "pv_today")
SUMMED_SETTINGS = COMPACT_SETTINGS + ("pv_power",)
CONTROL_SERVICE_SETTINGS = ("charge_start_service", "charge_stop_service", "discharge_start_service", "discharge_stop_service", "charge_freeze_service", "discharge_freeze_service")
BINDING_SETTINGS: frozenset[str]   # see Step 5

def slot_value(value, index):
    """The entry of an args value for one inverter: a list's entry at index (None past its end), a scalar for index 0 only."""

def manual_slots(apps_yaml: dict) -> list[dict]:
    """One entry per index below apps_yaml's num_inverters (default 1): {"slot", "origin": "manual"|"free", "inverter_type"}."""

def slot_serials(slot: int, apps_yaml: dict, args: dict, serials: list[str]) -> list[str]:
    """Which of `serials` (already casefolded) appear in the strings bound at this slot."""
```

- [ ] **Step 1: Write the failing tests** in `test_bindings.py` (entry point `test_bindings_all(my_predbat)`, `my_predbat` unused):

```python
def test_manual_slot_type_and_control():
    assert manual_slots({"num_inverters": 1, "inverter_type": ["GE"], "charge_rate": ["number.a_charge_rate"]}) == [{"slot": 0, "origin": "manual", "inverter_type": "GE"}]

def test_manual_slot_type_only():      # no control method -> free
    assert manual_slots({"num_inverters": 1, "inverter_type": ["GE"]})[0]["origin"] == "free"

def test_manual_slot_control_only():   # no type -> a dead slot, reported as free with inverter_type None
    assert manual_slots({"num_inverters": 1, "charge_rate": ["number.a_charge_rate"]}) == [{"slot": 0, "origin": "free", "inverter_type": None}]

def test_manual_slot_scalar_type():    # a scalar is inverter 0's only
    slots = manual_slots({"num_inverters": 2, "inverter_type": "GE", "charge_rate": ["number.a", "number.b"]})
    assert [s["origin"] for s in slots] == ["manual", "free"]

def test_manual_slot_givtcp_rest():
    assert manual_slots({"inverter_type": ["GE"], "givtcp_rest": ["http://h:6345"]})[0]["origin"] == "manual"

def test_manual_slot_service_api():    # a control service is not per inverter: it counts for every typed slot
    slots = manual_slots({"num_inverters": 2, "inverter_type": ["MINE", "MINE"], "charge_start_service": {"service": "x.y"}})
    assert [s["origin"] for s in slots] == ["manual", "manual"]

def test_manual_slot_regex_binding():  # Review Focus 5
    assert manual_slots({"inverter_type": ["GE"], "charge_rate": ["re:(number.givtcp_.*_charge_rate)"]})[0]["origin"] == "manual"

def test_slot_serials():
    apps = {"ge_cloud_serial": "CE1234G567"}
    args = {"charge_rate": ["number.ge_inverter_ce1234g567_battery_charge_power", "number.other"]}
    assert slot_serials(0, {}, args, ["ce1234g567"]) == ["ce1234g567"]          # case-insensitive entity match
    assert slot_serials(1, {}, args, ["ce1234g567"]) == []
    assert slot_serials(0, apps, {}, ["ce1234g567"]) == ["ce1234g567"]          # ge_cloud_serial from the snapshot, slot 0 only
    assert slot_serials(1, apps, {}, ["ce1234g567"]) == []
    assert slot_serials(0, {}, {"charge_rate": ["number.x_ab12_rate"]}, ["ab12"]) == []   # shorter than MIN_SERIAL_LENGTH
```

- [ ] **Step 2: Run to verify it fails.** `./run_all --test bindings > out_bindings.txt 2>&1`. Expected: import error for `bindings`.

- [ ] **Step 3: Implement `slot_value`, `manual_slots`, `slot_serials`.** A control method for a slot is any of: a string at that index, in a setting whose `APPS_SCHEMA` entry has `"modify": True`, that `utils.is_entity_id()` accepts or that starts with `re:`; a non-empty `givtcp_rest` entry at that index; any `CONTROL_SERVICE_SETTINGS` key present and non-empty. `slot_serials` looks at every string `slot_value(args[setting], slot)` for `setting` in `BINDING_SETTINGS`, plus `apps_yaml["ge_cloud_serial"]` for slot 0, all casefolded, and ignores serials shorter than `MIN_SERIAL_LENGTH`.

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Define `BINDING_SETTINGS` from the reporters, test first.** Start it as the settings GE Cloud's `_device_entities()` binds plus `givtcp_rest` and `inverter_mode`. In `test_discovery_contract.py` add a check, run for every reporter fixture the module already builds, that `set(record["entities"]) - BINDING_SETTINGS - SITE_SETTINGS` is empty for every inverter record. Run `./run_all --test discovery_contract`; add each name it reports that is an `APPS_SCHEMA` key. A reported name that is not an `APPS_SCHEMA` key is not added: list it in the hand-back for the maintainer instead, and exclude it in the check with a named constant and a one-line reason. No tuning key from the spec's list (`inverter_battery_rate_min`, `inverter_limit_charge`, `inverter_limit_discharge`, `inverter_limit_export`, `inverter_limit_override`, `battery_export_permitted`, `soc_max_fallback`, `inverter_reserve_max`) may be in the set; assert that in `test_bindings.py`.

- [ ] **Step 6: Run `bindings` and `discovery_contract`, then commit.** `git commit -m "feat(discovery): binding settings, manual slot detection and serial scan"`

---

### Task 3: `bindings.py` - building the bindings

**Files:**
- Modify: `apps/predbat/bindings.py`
- Test: `apps/predbat/tests/test_bindings.py`

**Interfaces:**
- Consumes: Task 2's functions and constants.
- Produces:

```python
def build_bindings(apps_yaml: dict, args: dict, sources: list[dict], previous: dict[str, int]) -> tuple[dict, list[str]]:
    """Return (bindings, warnings). Writes nothing."""

def slot_map(bindings: dict) -> dict[str, int]:
    """device_id -> slot for every discovered slot."""
```

`sources` is one dict per opted-in component, sorted by `name`:
`{"name": str, "records": list[dict], "skip_settings": frozenset, "keep_user_settings": frozenset, "include_pv_only": bool}`, where `records` are that component's validated `inverters` records in report order.

`bindings` is the spec's structure. Each `inverters[]` entry: `slot`, `origin` (`"manual"`, `"discovered"` or `"free"`), `source`, `device_id`, `inverter_type`, `serials` (sorted, casefolded), `settings`, plus for a discovered slot `skip_settings` and `keep_user_settings` as sorted lists. `site` is `{"settings": {name: descriptor}, "extras": {summed_setting: [values]}}`. The list is trimmed so its last entry is not free.

Warning strings, exact:
- takeover: `"Inverter slot {slot}: {source} inverter {serial} is also configured manually in apps.yaml - discovery is taking over this slot"`
- extra match cleared: `"Inverter slot {slot}: also names {serial}, already configured in slot {taken} - clearing this slot"`
- no room: `"{source} inverter {device_id} cannot be configured - all {MAX_INVERTERS} inverter slots are in use"`
- hole: `"Inverter slot {slot} is empty below a configured inverter - it has no inverter_type and Predbat will stop on it"`
- no driven inverter in an opted-in report: `"{source} reported no inverter to configure - keeping its existing slots"`

- [ ] **Step 1: Write the failing tests.** A helper `_rec(serial, entities=None, inverter_type="GEC", serials=None)` builds a validated-shape record with `device_id` `"gecloud:" + serial` and `hardware_ids.serial`; `_src(records, **options)` builds a source named `"gecloud"`.

```python
def test_build_empty_install():
    b, w = build_bindings({}, {}, [_src([_rec("ce1111g111", {"charge_rate": {"entity_id": "number.p_gecloud_ce1111g111_battery_charge_power", "access": "rw"}})])], {})
    assert [s["origin"] for s in b["inverters"]] == ["discovered"] and b["inverters"][0]["inverter_type"] == "GEC" and w == []

def test_build_manual_plus_discovered():     # manual slot 0 untouched, discovered in slot 1
def test_build_takeover():                   # manual slot naming the same serial: discovered takes slot 0, one takeover warning
def test_build_gateway_two_manual_slots():   # record serials=["ce1","ce2"...] matching slots 0 and 1: takes 0, slot 1 freed and trimmed, two warnings
def test_build_dead_slot_taken():            # control-only slot 0 (no type) is free: discovered takes it, no warning
def test_build_fifth_inverter():             # four manual slots: record not allocated, the "no room" warning
def test_build_sticky():                     # previous={"gecloud:b": 1}; reported order [b, a] -> b stays 1, a takes 0
def test_build_release():                    # previous has a and b; report has only a -> b's slot free/trimmed
def test_build_no_release_on_empty_report(): # previous has a; report has no driven record -> a keeps slot 0 as "discovered", settings read from live args, the "reported no inverter" warning
def test_build_hole_warns():                 # manual 0 and 2, slot 1 free, nothing discovered to fill it -> the hole warning
def test_build_site_setting():               # battery_temperature_history from the lowest discovered slot goes to site.settings, not the slot
def test_build_pv_only():                    # a record with no inverter_type: ignored unless include_pv_only, then its pv_power/pv_today entity ids go to site.extras
def test_build_user_extras():                # apps_yaml pv_power with 2 entries and num_inverters 1: entry [1] is in site.extras["pv_power"], ahead of PV-only entries
def test_build_manual_settings_shape():      # manual slot settings: {"charge_rate": {"entity_id": "number.a"}} for an entity, {"value": 0} for a literal, no "access"
```

Each named test asserts the `origin`, `device_id` and `slot` of every entry and the full warning list.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement `build_bindings` and `slot_map`** following the spec's four steps (manual slots, serial scan, allocation, result). Points the spec fixes that the signature does not:
  - Allocation order: components by name, records in report order; a record in `previous` keeps its slot before anything else is placed.
  - A record matching several manual slots takes the lowest; the others become free.
  - Releasing: a source whose report has no record with an `inverter_type` contributes nothing new, and every slot `previous` gave it stays `discovered` with `settings` taken from live `args` in the manual-slot shape, plus the "reported no inverter" warning. A source absent from `sources` altogether (stopped, or never opted in) is treated the same way without the warning. Only a source that reports at least one driven record can lose a slot. `previous` entries are matched to their source by the `device_id` prefix before the first `:`.
  - Site settings come out of the slot's `settings` and into `site["settings"]`.

- [ ] **Step 4: Run to verify it passes, then commit.** `git commit -m "feat(discovery): build per-slot inverter bindings from reports"`

---

### Task 4: `bindings.py` - incremental replay

**Files:**
- Modify: `apps/predbat/bindings.py`
- Test: `apps/predbat/tests/test_bindings.py`

**Interfaces:**
- Consumes: `build_bindings`, the constants.
- Produces:

```python
def desired_args(bindings: dict, apps_yaml: dict, args: dict) -> dict:
    """The full value of every arg the bindings decide: {setting: list-or-scalar}. Pure."""

def replay_bindings(bindings: dict, apps_yaml: dict, args: dict, set_arg) -> list[tuple[str, int | None]]:
    """Write only what differs from args, through set_arg(arg, value, index=None). Return the (setting, index) pairs written; index is None for a whole-value write."""
```

- [ ] **Step 1: Write the failing tests.** A helper `_apply(bindings, apps_yaml, args)` calls `replay_bindings` with a recording `set_arg` that also applies `Fetch.set_arg` semantics to `args` (`None` without an index deletes the key; with an index pads with `None`), and returns the recorded calls.

```python
def test_replay_discovered_only():        # manual slot 0's entries are never written; slot 1 gets inverter_type and each bound setting at index 1
def test_replay_clears_unbound():         # a BINDING_SETTINGS entry the record does not bind, set in args at the discovered slot, is written None; givtcp_rest at that index too
def test_replay_tuning_untouched():       # inverter_battery_rate_min at the discovered slot is neither read nor written
def test_replay_skip_setting():           # skip_settings={"load_today"}: args["load_today"] is left exactly as apps_yaml had it
def test_replay_keep_user_setting():      # keep_user_settings={"inverter_mode"} with an apps_yaml value at that index: not written; without one: written
def test_replay_scalar_becomes_list():    # Review Focus 1: args {"charge_rate": "number.a"} with a discovered slot 1 -> ["number.a", "number.b"]
def test_replay_value_binding():          # {"value": 0, "access": "r"} writes the literal 0
def test_replay_compact_settings():       # shared CT: slot 0 binds import_today, slot 1 does not -> ["sensor.a"], no None
def test_replay_pv_power_indexed():       # slot 0 binds nothing for pv_power, slot 1 binds -> [0, "sensor.b"]; site.extras["pv_power"] follow at index 2
def test_replay_site_and_count():         # battery_temperature_history written as a scalar; num_inverters is the slot count
def test_replay_all_none_key_deleted():   # a key whose every entry would be None is deleted from args
def test_replay_idempotent():             # a second _apply with the same inputs returns []
def test_replay_single_entry():           # change one bound entity: the calls are exactly [("charge_rate", 1)]
def test_replay_repairs_overwrite():      # args["soc_percent"] overwritten from outside -> the next replay writes it back
def test_replay_never_zero_inverters():   # bindings with no non-free slot: nothing is written at all
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement.** `desired_args` computes, for every discovered slot, `inverter_type` and each `BINDING_SETTINGS` member (the descriptor's `entity_id` or `value`, else `None`), taking the `apps_yaml` snapshot's own entry instead for a skipped setting or a kept one with a snapshot value; manual slots contribute their live `args` entries unchanged. Slot-indexed settings become lists as long as the slot count. `COMPACT_SETTINGS` are the slots' entries in slot order with `None` left out, then `site["extras"]`. `pv_power` is slot-indexed with `0` for an empty slot, then its extras. `replay_bindings` compares `desired_args` with `args`: for a slot-indexed setting it calls `set_arg(setting, value, index=i)` for each differing entry, first replacing a scalar with a one-entry list; for a compact setting, a scalar or a key to delete it makes one whole-value call. With no non-free slot it returns `[]` without writing.

- [ ] **Step 4: Run to verify it passes, then commit.** `git commit -m "feat(discovery): replay inverter bindings into args incrementally"`

---

### Task 5: Coordinator - the switch, opting in, `configure()` and its triggers

**Files:**
- Modify: `apps/predbat/coordinator.py` (`Coordinator.__init__`, `report`, `assemble`)
- Modify: `apps/predbat/component_base.py` (new method beside `report_discovery`)
- Modify: `apps/predbat/config.py` (`APPS_SCHEMA`: `"discovery_automatic": {"type": "boolean"}` beside the other discovery-free booleans near `ge_cloud_automatic`)
- Modify: `apps/predbat/predbat.py` (`initialize()`)
- Create: `apps/predbat/tests/test_discovery_automatic.py`; register `("discovery_automatic", test_discovery_automatic_all, "Discovery automatic inverter configuration: triggers and GE Cloud acceptance", False)`
- Docs: `docs/apps-yaml.md` (`discovery_automatic`)

**Interfaces:**
- Consumes: `build_bindings`, `slot_map`, `replay_bindings` from Tasks 3-4.
- Produces:

```python
# Coordinator
def allow_automatic(self, component_name, skip_settings=(), keep_user_settings=(), include_pv_only=False) -> bool
def configure(self) -> None
self.bindings      # the last bindings built, or None
self.slot_map      # {device_id: slot}

# ComponentBase
def allow_automatic_config(self, skip_settings=(), keep_user_settings=(), include_pv_only=False) -> bool
```

After a replay, `configure()` calls `component.check_automatic_config(slots)` on each opted-in component that defines it and had a write attributed to it, where `slots` is the slot of each of its records that has an `inverter_type`, in report order (`None` for one that got no slot). The component is found with `self.base.components.get_component(name)`.

- [ ] **Step 1: Write the failing tests** in `test_discovery_automatic.py`. Build the fixture once: `_Base` (attributes `args`, `args_from_apps_yaml`, `apps_yaml_override_warned`, `prefix = "predbat"`, `log_messages`; `log()`, `get_arg(name, default=None, **kwargs)` reading `args`, `set_arg(arg, value, index=None)` with `Fetch.set_arg` semantics and a `set_calls` list) and `_Components` (holds the real `Coordinator`, `get_component(name)`, and the `get_all`/`is_active`/`is_alive`/`load_error` methods `assemble()` reads - copy the shape of `_StubComponents` in `test_discovery_catalogue.py`). `_report(serials)` returns a GE-Cloud-shaped report with one driven record per serial.

```python
def test_switch_off_is_inert():
    # discovery_automatic unset: allow_automatic() returns False; report() and configure() leave base.set_calls == []

def test_configures_on_report():
    # switch on, allow_automatic("gecloud") -> True, then report("gecloud", _report(["ce1111g111"])):
    # args["inverter_type"] == ["GEC"], args["num_inverters"] == 1, before any explicit configure() call

def test_not_opted_in_is_not_configured():
    # switch on, report without allow_automatic: set_calls == []

def test_unchanged_report_writes_nothing():
    # report the same report twice: set_calls after the second equals set_calls after the first, and no new log line

def test_second_device_leaves_first_alone():
    # report [a], then [a, b]: every set_call in the second pass has index 1 or is num_inverters; slot_map == {"gecloud:a": 0, "gecloud:b": 1}

def test_configure_fills_in_on_second_report():     # Review Focus 2
    # first report: record with only soc_percent bound; second: charge_rate added -> second pass writes exactly [("charge_rate", 0)]

def test_checker_called_only_on_change():
    # a stub component with check_automatic_config recording its calls: called with [0] after the first report, not called after an identical one

def test_unconverted_component_warns():
    # report("fox", {"automatic": True, "inverters": [driven record]}) without opting in, gecloud opted in:
    # a log line "Warn: Coordinator: fox configures inverters itself and is not yet handled by discovery - running it alongside gecloud is not supported"

def test_stop_keeps_slots():
    # after configuring, components.get_component("gecloud") returns None: configure() writes nothing and slot_map is unchanged

def test_configure_never_raises():
    # base.set_arg raising RuntimeError: configure() returns, logs "Warn: Coordinator: automatic configuration failed: ...", args unchanged

def test_configure_is_serialised():                 # Review Focus 3
    # set_arg blocks on an Event in one thread while a second thread calls configure(); the second does not enter replay until the first returns

def test_catalogue_carries_slots():
    # assemble()["inverters"][0]["slot"] == 0 and assemble()["observations"]["bindings"]["inverters"][0]["device_id"] == "gecloud:ce1111g111"

def test_takeover_and_hole_warnings_logged():
    # build_bindings warnings reach base.log prefixed "Warn: Coordinator: "
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement.**
  - `allow_automatic()` returns `False` and records nothing unless `self.base.get_arg("discovery_automatic", False)` is true. Otherwise it stores the options under the component name and calls `configure()`.
  - `report()`: after storing, call `configure()` when the component is opted in and the cleaned report differs from the one it replaced. Keep the existing lock for the stored reports; `configure()` runs outside it under its own `threading.RLock`, held for the whole build, replay and check.
  - `configure()`: no-op when the switch is off. Builds `sources` from the opted-in components' stored reports, calls `build_bindings(self.base.args_from_apps_yaml, self.base.args, sources, self.slot_map)`, logs each warning once per distinct text as `"Warn: Coordinator: " + text`, calls `replay_bindings(..., self.base.set_arg)`, stores `self.bindings` and `self.slot_map`, logs `"Coordinator: inverter slot {slot} configured from {source} ({device_id})"` once per slot that had a write, then calls the checkers. A write with index `i` is attributed to the source of slot `i`; a whole-value write to every opted-in source. The whole body is in `try/except Exception`, logging `"Warn: Coordinator: automatic configuration failed: {e}"`.
  - `assemble()`: add `slot` to each inverter entry whose `device_id` is in `self.slot_map`, and `observations["bindings"] = self.bindings` when it is not `None`. `publish()` is unchanged.
  - `ComponentBase.allow_automatic_config()` mirrors `report_discovery()`: finds the coordinator, returns `False` without one. It must also return `False` on an instance with no `base` attribute at all (`getattr(self, "base", None)`), because the test mocks skip `ComponentBase.__init__`.
  - Headless hosts drive `initialize_config()` and `finalize_config()` themselves. Add one sentence to the `initialize_state()` docstring in `predbat.py`, where the host sequence is described, saying a host calls `components.coordinator.configure()` once its components have started and before `finalize_config()`.
  - `predbat.py` `initialize()`: call `self.components.coordinator.configure()` after the phase 1 `components.start()` block and again inside the existing discovery barrier `try`, before `assemble()`. Update the barrier comment, which says nothing here changes configuration.

- [ ] **Step 4: Run `discovery_automatic`, `coordinator`, `discovery_catalogue` and `web_discovery`.** Expected: all pass; the catalogue's observe-only test still holds because the switch is off there.

- [ ] **Step 5: Document the switch** in `docs/apps-yaml.md`: what it does, that it is off by default, that only GE Cloud uses it so far.

- [ ] **Step 6: Commit.** `git commit -m "feat(discovery): coordinator configures opted-in inverters behind discovery_automatic"`

---

### Task 6: GE Cloud - split the automatic configuration, opt in, and check

**Files:**
- Modify: `apps/predbat/gecloud.py` (`async_automatic_config` near line 1691; new constants beside `GE_CLOUD_DUMMIED_SETTINGS` near line 335)
- Modify: `apps/predbat/tests/discovery_contract.py` (`capture_automatic_config` stays valid: it must still capture through the switch-off path)
- Test: `apps/predbat/tests/test_ge_cloud.py` (new tests registered in the module's own test list, where `discovery_record_shared_ct` is registered near line 368)

**Interfaces:**
- Consumes: `ComponentBase.allow_automatic_config()`; `SUMMED_SETTINGS`, `SITE_SETTINGS` from `bindings`.
- Produces, on `GECloudDirect`:

```python
GE_CLOUD_KEEP_USER_SETTINGS = ("inverter_mode", "export_limit")
GE_CLOUD_KNOWN_DIFFERENCES = {setting: reason}   # the six "any device has it" settings, and givtcp_rest

def _legacy_bindings(self, devices) -> dict:
    """What the old automatic configuration binds: {setting: value}, last write wins, None meaning "delete the key". Pure - writes nothing."""

def check_automatic_config(self, slots: list) -> list[str]:
    """Compare _legacy_bindings() with live args for these slots; log and return one line per unexpected difference."""
```

- [ ] **Step 1: Pin today's behaviour before touching it.** Add `_test_automatic_config_golden` to `test_ge_cloud.py`: for each of the device layouts in Task 7's scenario table (build them with the existing `_record_component(devices, settings, info, config)` and `GEC_FULL_REGISTERS`), run `capture_automatic_config(ge)` and compare with a literal expected dict written into the test from this first run. Run it on the unmodified code and confirm it passes. This is the guard for the refactor.

- [ ] **Step 2: Write the failing tests.**

```python
def _test_legacy_bindings_is_pure(my_predbat):
    # ge._legacy_bindings(devices) == capture_automatic_config(ge) minus ge_cloud_serial and ge_cloud_data, and ge.config_args is unchanged by the call

def _test_switch_on_opts_in_and_writes_no_binding(my_predbat):
    # allow_automatic_config stubbed to return True and record its kwargs: after run_async(ge.async_automatic_config(devices)),
    # no BINDING_SETTINGS key, inverter_type or num_inverters was set; ge_cloud_serial was; the kwargs are
    # skip_settings=() , keep_user_settings=("inverter_mode", "export_limit"), include_pv_only=False

def _test_opt_in_options_follow_config(my_predbat):
    # ge_cloud_load_today_ignore -> skip_settings == ("load_today",); ge_cloud_automatic_split_pv -> include_pv_only is True

def _test_checker_clean(my_predbat):
    # args set to exactly what _legacy_bindings() gives: check_automatic_config([0]) == [] and one "Info: GECloud: discovery configuration matches" line

def _test_checker_reports_difference(my_predbat):
    # args["soc_percent"][0] changed: one line
    # "Warn: GECloud: discovery set soc_percent[0] to sensor.wrong, the previous automatic configuration would set sensor.predbat_gecloud_battery001_battery_percent"

def _test_checker_slot_offset(my_predbat):
    # the same legacy values placed at slot 1 behind a manual slot 0: check_automatic_config([1]) == []

def _test_checker_known_difference_is_info(my_predbat):
    # two devices, only the first with pause registers; args hold pause_mode for slot 0 only:
    # returns [] and logs an "Info: GECloud: discovery differs on pause_mode (" line carrying the table's reason

def _test_checker_warns_once(my_predbat):        # Review Focus 2
    # the same difference checked twice logs its Warn line once

def _test_checker_never_raises(my_predbat):
    # devices_dict = None: returns [] and logs nothing at Warn level
```

- [ ] **Step 3: Run to verify they fail and the golden test passes.**

- [ ] **Step 4: Implement.**
  - `_legacy_bindings(devices)`: the present body from the feature sniffing to the end of the EMS section, with each `self.set_arg(k, v)` / `self.set_arg_auto(k, v, ...)` turned into `bound[k] = v`. It keeps its guards (`ge_cloud_load_today_ignore`, `site_export_limit`, the CT and split-PV options). It does not include `ge_cloud_serial`, `ge_cloud_data`, the `inverter_hybrid` write or any log line.
  - `async_automatic_config(devices)`: the early "No battery devices" return stays. Then `bound = self._legacy_bindings(devices)`. If `self.allow_automatic_config(skip_settings=..., keep_user_settings=GE_CLOUD_KEEP_USER_SETTINGS, include_pv_only=...)` is true, write no binding. Otherwise apply `bound` in dict order: `set_arg_auto(k, v, overwrite=False)` for `GE_CLOUD_KEEP_USER_SETTINGS`, `set_arg(k, v)` for the rest. Then, in both cases and in their present order, the live remainder: `ge_cloud_serial`, the EMS `ge_cloud_data` toggle and its restore, the model detection and `inverter_hybrid` write, and the existing log lines.
  - `check_automatic_config(slots)`: wrapped in `try/except` returning `[]`. For each setting in `_legacy_bindings(self.devices_dict)`:
    - `num_inverters`: not compared.
    - a member of `SITE_SETTINGS` or any other scalar: equal to `get_arg`'s raw arg.
    - a member of `SUMMED_SETTINGS`: the legacy entries, `None` removed, appear in the arg's list in the same order (a subsequence).
    - any other list (including `inverter_type`): legacy entry `i` equals the arg's entry at `slots[i]`, a missing entry counting as `None`; a legacy `None` value means every one of this component's slots is `None` or absent.
    - A difference on a `GE_CLOUD_KNOWN_DIFFERENCES` key or a skipped setting is logged at `Info` with its reason and not returned. Any other is logged as the `Warn` line in the test above and returned. Each distinct line is logged once per process (keep a set on the instance).

- [ ] **Step 5: Run `ge_cloud`, `discovery_contract` and `discovery_catalogue`.** Expected: all pass, the golden test and every pre-existing automatic-config test unchanged. If a pre-existing test fails only because it counted `set_arg` calls or relied on write order, fix `_legacy_bindings` to preserve what it checks rather than editing the test; if that is impossible, stop and report which test and why.

- [ ] **Step 6: Commit.** `git commit -m "feat(gecloud): opt in to discovery configuration and check it against the old automatic config"`

---

### Task 7: Acceptance tests - discovery against the old automatic configuration

**Files:**
- Modify: `apps/predbat/tests/test_discovery_automatic.py`
- Docs: `docs/discovery-catalogue.md` (bindings, slots, the switch, the takeover rule), `docs/components.md` (GE Cloud section notes the switch)

**Interfaces:**
- Consumes: everything above. Reuses `MockGECloudDirect`, `GEC_FULL_REGISTERS` and `_gec_devices()` from `tests/test_ge_cloud.py`, and `_Base`/`_Components` from Task 5.
- Produces: a harness other components' conversions will reuse:

```python
def run_old_and_new(devices, settings, info=None, config=None, apps_yaml=None) -> tuple[dict, dict, list[str]]:
    """Run GE Cloud's automatic configuration twice from the same starting args - switch off, then on with a real Coordinator - and return (old_args, new_args, new_run_log)."""

def compare_configuration(old_args, new_args, known=()) -> list[str]:
    """Every difference in num_inverters, inverter_type, BINDING_SETTINGS and SITE_SETTINGS, except the settings named in `known`. Trailing None entries and an all-None key against an absent key are equal."""
```

The dummy data is set on the component (`devices_dict`, `settings`, `info`, config args), as the existing GE Cloud automatic-config tests do. Nothing in `gecloud.py`, `coordinator.py` or `bindings.py` is stubbed: each run calls the real `async_automatic_config()`, then the real `refresh_discovery()`, on an `_AutoGECloud(MockGECloudDirect)` whose `base` is the `_Base`, whose `args` is `base.args`, whose `set_arg`/`get_arg` forward to it, and whose `component_name` is `"gecloud"`. `apps_yaml` seeds both `base.args` and `base.args_from_apps_yaml`.

- [ ] **Step 1: Write the scenarios.** One test function per row. Each asserts `compare_configuration(old, new, known) == []`, that every setting in `known` differs in exactly the stated way, and that the new run's log has no line starting `"Warn: GECloud: discovery set"`.

| Test | Dummy data | Extra assertions |
| --- | --- | --- |
| `test_accept_single` | one battery `battery001`, full registers | `new["num_inverters"] == 1`, `new["inverter_type"] == ["GEC"]` |
| `test_accept_two_inverters` | two batteries, meters `{battery001: [1001], battery002: [1002]}` | `num_inverters == 2`; every slot-indexed list has two entries |
| `test_accept_mixed_registers` | battery001 full registers; battery002 without `battery_charge_power`/`battery_discharge_power` (percentage rate only) and without the pause registers | `known = ("pause_mode", "pause_start_time", "pause_end_time", "charge_rate_percent", "discharge_rate_percent")`; `new["pause_mode"]` has an entity at 0 and `None` at 1 where `old` has entities at both |
| `test_accept_pv_only_split_off` | one battery, `pv=["pv001"]` | `pv_power` and `pv_today` each one entry; `num_inverters == 1` |
| `test_accept_pv_only_split_on` | the same, `ge_cloud_automatic_split_pv: True` | `pv_power == [battery001 solar_power, pv001 solar_power]`, same for `pv_today`; `num_inverters == 1` |
| `test_accept_ct_shared_detected` | two batteries, meters both `[9999]` | `grid_power == [battery001 grid_power, 0]`, `load_power` likewise; `import_today`, `export_today`, `load_today` one entry each |
| `test_accept_ct_separate_detected` | two batteries, meters `[1001]` and `[1002]` | `grid_power` two entities; `import_today` two entries |
| `test_accept_ct_shared_override` | meters `[1001]`/`[1002]`, `ge_cloud_automatic_shared_ct: True` | as the shared row |
| `test_accept_ct_split_override` | meters both `[9999]`, `ge_cloud_automatic_split_ct: True`; then both overrides set | as the separate row, both times |
| `test_accept_gateway` | `gateway="gw001"`, two batteries | `num_inverters == 1`; entity ids name `gw001` |
| `test_accept_ems` | `ems="ems001"`, two batteries | `inverter_type == ["GEE", "GEE"]`; `battery_power == [ems001 battery_power, 0]`; `ge_cloud_data` is `False` in both runs |
| `test_accept_opt_outs` | one battery; `ge_cloud_load_today_ignore: True`; `apps_yaml` with `load_today: ["sensor.mine"]`, `inverter_mode: ["select.mine"]`, `export_limit: [3600]` | all three keep the user's value in both runs |

- [ ] **Step 2: Write the two tests with no old run.**

```python
def test_accept_manual_plus_discovered():
    # apps_yaml: num_inverters 1, inverter_type ["SolisCloud"], charge_rate ["number.solis_charge"], soc_percent ["sensor.solis_soc"]
    # new run with one GE battery: num_inverters == 2, inverter_type == ["SolisCloud", "GEC"],
    # every arg entry at index 0 equals apps_yaml's, the GE entities are at index 1, no "taking over" line in the log

def test_accept_template_takeover():
    # apps_yaml: the inverter bindings of templates/givenergy_cloud.yaml with geserial = "battery001" resolved
    # (inverter_type ["GEC"], num_inverters 1, charge_rate ["number.ge_inverter_battery001_battery_charge_power"], ...)
    # new run == the new args of test_accept_single on num_inverters, inverter_type, BINDING_SETTINGS and SITE_SETTINGS,
    # and the log has "Warn: Coordinator: Inverter slot 0: gecloud inverter battery001 is also configured manually in apps.yaml - discovery is taking over this slot"
```

- [ ] **Step 3: Write the run-loop test,** using `_make_rediscovery_ge_cloud()` from `test_ge_cloud.py` to drive the real `run()`:

```python
def test_accept_run_loop_incremental():
    # first run(first=True) with one battery: slot_map == {"gecloud:battery001": 0}
    # api["devices"] gains battery002, the next device check runs: slot_map gains "gecloud:battery002": 1,
    # and no set_arg call in that pass has index 0
```

- [ ] **Step 4: Run and fix forward.** `./run_all --test discovery_automatic`. A failing scenario is a finding about the record builder, the bindings or the checker, not about the test: fix the code, or, if the old and new configurations differ for a reason that is right, add the setting to `GE_CLOUD_KNOWN_DIFFERENCES` with its reason and to that scenario's `known`, and say so in the hand-back. Do not widen `compare_configuration`.

- [ ] **Step 5: Write the docs.** `docs/discovery-catalogue.md`: a section on automatic configuration covering the switch, slots (manual first, sticky, four at most), the bindings under `observations.bindings`, the serial takeover and its warning, what happens to a slot with no `inverter_type`, and that only GE Cloud is converted. `docs/components.md`: one paragraph in the GE Cloud section.

- [ ] **Step 6: Full verification.** `./run_all --quick > out_quick.txt 2>&1; echo exit=$?`, then `./run_pre_commit`. Expected: both clean. Run GitNexus `detect_changes({scope: "compare", base_ref: "main"})` and confirm the changed symbols are the ones this plan names.

- [ ] **Step 7: Commit.** `git commit -m "test(discovery): acceptance tests comparing discovery configuration with GE Cloud's old automatic config"`

---

## After the last task

- Append to `tools/debug-journal.md` anything that cost time and would cost the next person time (for example a reporter binding a name that is not an `APPS_SCHEMA` key, found in Task 2 Step 5).
- Hand back: the list of `GE_CLOUD_KNOWN_DIFFERENCES` as finally written, any record entity name left out of `BINDING_SETTINGS`, and any existing test that needed a type added in Task 1.
- Not done here, by design: turning the switch on anywhere, converting another component, deleting `async_automatic_config()`.
