# Discovery Charger Reporters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every EV charger integration publishes `chargers` and `cars` records to the discovery catalogue: myenergi (Zappi), Wallbox, Hypervolt and GE Cloud's EV charger, alongside Ohme which already does.

**Architecture:** Add `charger_record()`, `car_record()` and `charger_with_stub_car()` builders to `coordinator.py`, siblings of `inverter_record()`. Fix the stub-car marker, which validation drops today. Migrate Ohme onto the builders. Then give each charger component a `build_discovery()` and one `self.refresh_discovery()` call per `run()` cycle. The rollout stays observe-only: no `automatic_config()` or control path changes.

**Tech Stack:** Python 3, Predbat's in-tree test harness (`coverage/run_all`), pre-commit (Black 256, Flake8 250, interrogate 100%, cspell en-gb).

**Spec:** [docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md](../specs/2026-09-10-discovery-catalogue-design.md) (sections `chargers` / `cars`, redaction "Four classes"), and [docs/discovery-catalogue.md](../../discovery-catalogue.md) ("Writing a reporter"). The reference reporter is `OhmeAPI.build_discovery()` in `apps/predbat/ohme.py`.

## Global Constraints

- Observe-only. Do not change `automatic_config()`, `async_automatic_config_evc()`, `charger_control_*` or any entity a component publishes.
- A charger record has `device_id` `"<component>:<hardware serial>"`. Its car is a stub, `"<charger device_id>:car"`, with `flags: ["stub"]` and a `charged_by` link back. The charger links forward with `serves_cars: [<car device_id>]`.
- `info.vendor` is always set. `info.model` and `info.firmware` are set only when the vendor API supplies them.
- **No `ratings` on any of the four.** None of them gets a device-reported power or phase rating. A live max-current *setting* is not a rating, and the catalogue design forbids invented ratings.
- **Never report:** charger nicknames (myenergi cloud `alias`, Wallbox `name`, Hypervolt `name`, GE `alias`), location (Wallbox `zipcode`/`country`/`timezone`), vehicle registration (Wallbox `car_plate`), user/login ids (myenergi `hub_serial` is the digest-auth username, Wallbox `user_id`/`user_name`).
- **Entities are the ones the component's own automatic config binds**, keyed by Predbat standard name and filtered through `self.discovery_entities()`. All four bind the same four names: `car_charging_planned`, `car_charging_plugged`, `car_charging_energy`, `car_charging_power`. Leave out `car_charging_now`, which only re-binds the power entity (Ohme's precedent). A stub car carries no entities: none of the four binds a car-facing entity.
- Records are listed in the component's own car order, the same order `charger_control_chargers()` uses, so two dumps of unchanged hardware do not differ.
- `build_discovery()` returns `None` when there is no charger to describe. `refresh_discovery()` is called once per `run()` cycle, outside any `if first:` gate.
- Every new function needs a docstring. Unknown words go in `.cspell/custom-dictionary-workspace.txt`.
- Tests: from `coverage/`, save output to a file and grep the file afterwards, e.g. `./run_all --test wallbox > /tmp/out.txt 2>&1; grep -E "FAIL|Error|passed|failed" /tmp/out.txt`.

## Review Focus

1. **A nickname reaching the dump.** Every vendor holds a user-chosen charger name. Each vendor task has a round-trip test that sets a nickname and asserts it is absent from both the raw report and the redacted catalogue.
2. **GE Cloud serial case.** GE entity ids lowercase the serial (`sensor.predbat_gecloud_ev2244g123_...`) while `hardware_ids.serial` keeps it uppercase. The redactor's serial strip is case-insensitive (`Redactor._whole_token`), so all four entities should survive. Task 5's round-trip test pins this with an uppercase serial.
3. **A second charger appearing later.** `refresh_discovery()` compares whole reports, so a grown charger list re-files. Tasks 3 and 4 each add a two-cycle test: one charger, then two, then the report holds both.
4. **A Wallbox payload with no `serial_number`.** The charger must still be reported, not silently skipped. Task 3 pins the fallback identity.
5. **A charger-only GE Cloud install** (`ge_cloud_direct` off, `evc_direct` on). Its report has to carry the charger. The existing `evc_only_run` test asserts the report is otherwise empty, so Task 5 updates it.

## Out of scope (flag to the maintainer, do not build)

- `contested_car_slots` (`Coordinator._conflicts`) only fires when one component reports a car with no charger. Two charger components that both claim car slots (Ohme plus Hypervolt) raise nothing. Whether that should be a conflict is a design decision.
- GE Cloud's `"automatic"` flag is `self.automatic and self.inverter_control`, so a charger-only install reports `automatic: false` even with `ge_cloud_automatic_evc` on. A single per-component flag cannot describe two independently automated device types.
- Wallbox `max_available_current` (amps, device-reported) could become a charger rating once the allocator needs one. There is no consumer today.
- myenergi Eddi (a hot-water diverter) has no catalogue section.

---

### Task 1: Charger/car record builders, stub marker fix, Ohme migration

**Files:**
- Modify: `apps/predbat/coordinator.py` (beside `inverter_record()`, ~line 1134)
- Modify: `apps/predbat/ohme.py` (`build_discovery()`, ~line 884-963)
- Modify: `docs/discovery-catalogue.md` ("Writing a reporter" last paragraph; line 76 reporter list)
- Modify: `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md:177` (`info.stub` → `flags: ["stub"]`)
- Test: `apps/predbat/tests/test_coordinator.py`, `apps/predbat/tests/test_ohme.py`

**Interfaces:**
- Produces, all in `coordinator.py`, with the same omit-empty, copy-containers and sort-sets semantics as `inverter_record()`:
  - `charger_record(device_id, *, serves_cars=None, hardware_ids=None, account_ids=None, info=None, ratings=None, entities=None) -> dict`
  - `car_record(device_id, *, charged_by=None, flags=None, hardware_ids=None, info=None, ratings=None, entities=None) -> dict`
  - `charger_with_stub_car(device_id, *, hardware_ids=None, account_ids=None, info=None, entities=None) -> tuple[dict, dict]`. Returns `(charger, car)`, where the car is `car_record(device_id + ":car", charged_by=device_id, flags=["stub"])` and the charger has `serves_cars=[device_id + ":car"]`.
- Extract the omit/copy/sort loop from `inverter_record()` into a private `_record(device_id, fields: dict) -> dict` and have all three use it. `inverter_record()`'s behaviour must not change; its existing tests prove that.

- [ ] **Step 1: Write the failing tests** in `test_coordinator.py` and add each one to `test_coordinator_all()`:

```python
def test_charger_with_stub_car_cross_links():
    charger, car = charger_with_stub_car("wallbox:900001", hardware_ids={"serial": "900001"}, info={"vendor": "Wallbox"}, entities={})
    assert charger == {"device_id": "wallbox:900001", "serves_cars": ["wallbox:900001:car"], "hardware_ids": {"serial": "900001"}, "info": {"vendor": "Wallbox"}}, charger
    assert car == {"device_id": "wallbox:900001:car", "charged_by": "wallbox:900001", "flags": ["stub"]}, car
    return 0

def test_stub_car_survives_validation():
    _, car = charger_with_stub_car("x:1")
    logs = []
    cleaned = validate_report({"cars": [car]}, "x", logs.append)
    assert cleaned["cars"][0]["flags"] == ["stub"], cleaned
    assert not logs, logs
    return 0
```

Builders drop `None`, `""` and empty containers at the top level only, exactly like `inverter_record()`. They do not recurse into `info`, so each reporter leaves out empty `info` values itself. Add `test_charger_record_omits_empty_containers` mirroring `test_inverter_record_omits_empty_containers`.

In `test_ohme.py`, change `_test_ohme_build_discovery_stub_car_when_no_vehicle` (line ~4467) to assert `car["flags"] == ["stub"]` and `"info" not in car`. Add `_test_ohme_stub_car_round_trips_validation`: build with no vehicle, `Coordinator(MockBase()).report("ohme", report)`, then assert the cleaned car has `flags == ["stub"]` and nothing was logged. This is the test that would have caught the bug.

- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test coordinator --test ohme > /tmp/t1.txt 2>&1`. Expected: `charger_with_stub_car` is not defined, and the Ohme stub assertion fails on `flags`.

- [ ] **Step 3: Implement** `_record`, `charger_record`, `car_record` and `charger_with_stub_car` in `coordinator.py`. Then rewrite `OhmeAPI.build_discovery()` on `charger_record()` and `car_record()`: a known vehicle gives `car_record("ohme:{vehicle_id}", charged_by=..., info={make, model})`, and no vehicle gives `flags=["stub"]` and no `info`. Keep its docstring true. In `docs/discovery-catalogue.md`, extend the last paragraph of "Writing a reporter" to name the three new builders. Replace the stale parenthesised reporter list at line 76 with "No reporter populates `programmes` yet". Change `info.stub` to `flags: ["stub"]` in the design spec.

- [ ] **Step 4: Run to verify they pass.** Run `./run_all --test coordinator --test ohme --test discovery_catalogue > /tmp/t1.txt 2>&1`. Expected: every listed test passes, and Ohme's existing round-trip test is unchanged and green.

- [ ] **Step 5: Commit.** Message: `fix(discovery): stub cars kept through validation; add charger/car record builders`, with `Fixes #5849` in the body. The PR body must carry it too.

---

### Task 2: myenergi Zappi reporter

**Files:**
- Modify: `apps/predbat/myenergi.py` (new `build_discovery()` and `DISCOVERY_ENTITY_SUFFIXES`; `run()` ~line 1113)
- Test: `apps/predbat/tests/test_myenergi.py` (register each test in `test_myenergi()`)

**Interfaces:**
- Consumes: `charger_with_stub_car` (Task 1).
- Produces: `MyEnergiAPI.build_discovery() -> dict | None`.

Decisions:
- One record per Zappi from `self.controlled_zappis()` (myenergi.py:915), in that order. Skip Eddi.
- `device_id` is `"myenergi:{device.serial}"`. It is built from the hardware serial, so the direct and cloud transports give the same identity, even though their `device_id`s differ (`Z…` versus the cloud id).
- `hardware_ids` is `{"serial": str(device.serial)}`. `info` is `{"vendor": "myenergi", "model": "zappi"}`. No `account_ids`: the hub serial is a login identifier and is not needed.
- Entities with prefix `self.entity_prefix(device)` (myenergi.py:819). The bindings are `car_charging_planned` = `sensor.{p}_plug_status` (domain `sensor`), `car_charging_plugged` = `binary_sensor.{p}_plugged`, `car_charging_energy` = `sensor.{p}_session_energy` and `car_charging_power` = `sensor.{p}_power`. All have `access: "r"`.
- Return `{"automatic": self.automatic, "chargers": [...], "cars": [...]}`, or `None` if there are no Zappis.
- Call `self.refresh_discovery()` immediately before the final `return True` of `run()` (~line 1113), at the outermost level of the function. It then runs on non-poll cycles too, against the cached `self.devices`.

- [ ] **Step 1: Write failing tests:**
  - `test_discovery_none_without_zappi`: only an Eddi gives `build_discovery() is None`.
  - `test_discovery_zappi_record`: with `_zappi(12345678)` polled through `run(0, True)` and entities published, `report["chargers"][0]["device_id"] == "myenergi:12345678"`, the four entity keys are exactly the set above, and `report["cars"][0]["flags"] == ["stub"]`.
  - `test_discovery_cloud_alias_never_reported`: a cloud-normalised Zappi with `alias="Dad's car"` gives the same `device_id`, `"Dad's car"` is not in `str(report)`, and `"12345678"` (the hub serial used by `_make_component`) is not in `str(report)`.
  - `test_discovery_round_trip`: feed the report through `Coordinator(MockBase()).report("myenergi", report)`. All four entities survive, `flags` survives, nothing is logged, and after `assemble()` the serial is readable in `str(coordinator.catalogue())`.
  - `test_discovery_refreshed_on_non_poll_cycle`: stub `report_discovery` to record calls, run `run(0, True)` and then `run(30, False)` with `poll_seconds=60`. The report is filed once, and a failure raised from `build_discovery` on the first cycle is retried on the second.

  Use a hub serial that differs from the Zappi serial in these fixtures. Otherwise the leak assertion proves nothing.

- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test myenergi > /tmp/t2.txt 2>&1`. Expected: an `AttributeError` for `build_discovery`, or a `None` report.
- [ ] **Step 3: Implement** `build_discovery()` and the `run()` call as decided above.
- [ ] **Step 4: Run to verify they pass.** Run the same command. Expected: `test_myenergi` passes in full.
- [ ] **Step 5: Commit.** Message: `feat(myenergi): report Zappi chargers to the discovery catalogue`.

---

### Task 3: Wallbox reporter

**Files:**
- Modify: `apps/predbat/wallbox.py` (new `build_discovery()`; `run()` lines ~599-602)
- Test: `apps/predbat/tests/test_wallbox.py` (register each test in `test_wallbox()`)

**Interfaces:**
- Consumes: `charger_with_stub_car` (Task 1).
- Produces: `WallboxAPI.build_discovery() -> dict | None`.

Decisions:
- One record per charger from `self.ordered_chargers()`.
- `device_id` is `"wallbox:{charger.serial}"`. When `serial` is empty, fall back to `"wallbox:charger:{charger_id}"`; never skip the charger.
- `hardware_ids` is `{"serial": charger.serial, "charger_id": str(charger.charger_id)}`, with `serial` only when known. The charger id is not secret (maintainer decision, 2026-10-29), so it stays readable, as do the entity ids that embed it.
- `info` is `{"vendor": "Wallbox", "model": charger.part_number, "firmware": charger.software_version}`, keeping only the non-empty values.
- Entities with prefix `"{prefix}_wallbox_{charger_id}"`: `car_charging_planned` = `binary_sensor.{p}_connected`, `car_charging_plugged` = `binary_sensor.{p}_plugged`, `car_charging_energy` = `sensor.{p}_session_energy` and `car_charging_power` = `sensor.{p}_power`.
- `automatic` comes from `self.automatic`.
- In `run()`, replace `if …: return await self.poll(seconds, first)` / `return True` with: assign `ok` from `poll()` or `True`, then call `self.refresh_discovery()`, then `return ok`. The rate-limit `skip_cycles` early return, and exceptions raised out of `poll()`, still skip the call. Both are failure paths and are retried next cycle.

- [ ] **Step 1: Write failing tests:**
  - `test_discovery_record`: `_started_component` gives a charger `device_id` of `"wallbox:900001"`, `info.model` equal to the fixture's `part_number`, `hardware_ids == {"serial": "900001", "charger_id": "101"}`, the four entity keys, and a stub car.
  - `test_discovery_private_fields_never_reported`: the fixture's charger `name` ("Garage"), `zipcode`, `car_plate` and `user_name` values are all absent from `str(report)`.
  - `test_discovery_no_serial_fallback`: a payload with `serial_number` removed gives `device_id == "wallbox:charger:101"` and `hardware_ids == {"charger_id": "101"}`.
  - `test_discovery_second_charger_refiles`: run one cycle with a single charger, then add a second to the stub transport and run a list cycle (`seconds = CHARGER_LIST_SECONDS`). The recorded `report_discovery` calls are 2, and the last one has 2 chargers in car order.
  - `test_discovery_round_trip`: a ≥6-digit `charger_id` (e.g. `"4417302"`), through `Coordinator.report` then `assemble()` then `catalogue()`. All four entities and the stub flag survive validation and redaction, and `"4417302"` stays readable in the redacted catalogue.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test wallbox > /tmp/t3.txt 2>&1`.
- [ ] **Step 3: Implement** as decided above.
- [ ] **Step 4: Run to verify they pass.** Expected: `test_wallbox` passes in full.
- [ ] **Step 5: Commit.** Message: `feat(wallbox): report chargers to the discovery catalogue`.

---

### Task 4: Hypervolt reporter

**Files:**
- Modify: `apps/predbat/hypervolt.py` (new `build_discovery()`; `run()` between the `except HypervoltError` block ~942 and `if not self.car_order:` ~943)
- Test: `apps/predbat/tests/test_hypervolt.py` (register each test in `test_hypervolt()`)

**Interfaces:**
- Consumes: `charger_with_stub_car` (Task 1).
- Produces: `HypervoltAPI.build_discovery() -> dict | None`.

Decisions:
- One record per charger from `self.ordered_chargers()`. `device_id` is `"hypervolt:{charger_id}"`, and `hardware_ids` is `{"serial": charger_id}`. The Hypervolt charger id is the device's hardware id, with the generation encoded in its length (`charger_major_version`, hypervolt.py:303).
- `info` is `{"vendor": "Hypervolt", "model": charger.model, "firmware": charger.firmware_version}`, keeping only the non-empty values.
- Entities with prefix `"{prefix}_hypervolt_{charger_id}"`: the same four bindings as Wallbox (`_connected`, `_plugged`, `_session_energy`, `_power`).
- `automatic` comes from `self.automatic`.
- Call `self.refresh_discovery()` on its own line after the `try/except` and before `if not self.car_order:`. That covers both the empty-account path and the stale-charger early returns.

- [ ] **Step 1: Write failing tests:**
  - `test_discovery_record`: `_started_component()` gives a charger `device_id` of `f"hypervolt:{V3_ID}"`, `info.model == "Home 3 Pro"`, the four entity keys, and a stub car.
  - `test_discovery_name_never_reported`: `"Driveway"` is not in `str(report)`.
  - `test_discovery_empty_account_files_nothing`: a component signed in with no chargers gives `build_discovery() is None`, and `run()` still returns True.
  - `test_discovery_second_charger_refiles`: use `_two_chargers` (line ~1428) with the second charger added after the first cycle. The last filed report holds both chargers in `car_order`.
  - `test_discovery_round_trip`: through `Coordinator`, all four entities and the stub flag survive, and the serial is readable in the redacted catalogue.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test hypervolt > /tmp/t4.txt 2>&1`.
- [ ] **Step 3: Implement** as decided above.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit.** Message: `feat(hypervolt): report chargers to the discovery catalogue`.

---

### Task 5: GE Cloud EV charger records

**Files:**
- Modify: `apps/predbat/gecloud.py` (`build_discovery()` ~2069-2169; the call site at ~2581 is unchanged)
- Test: `apps/predbat/tests/test_ge_cloud.py` (add tuples to `sub_tests` in `test_ge_cloud()`; update `evc_only_run` ~8808)

**Interfaces:**
- Consumes: `charger_with_stub_car` (Task 1).
- Produces: `GECloudDirect.build_discovery()` now also returns `"chargers"` and `"cars"` keys.

Decisions:
- One record per uuid in `self.controlled_evc_devices()` (gecloud.py:2217), which already keeps only chargers with a `serial_number` and sorts them by serial. The serial comes from `self.evc_device[uuid]["serial_number"]`.
- `device_id` is `"gecloud:{serial}"`, `hardware_ids` is `{"serial": serial}` (original case), and `info` is `{"vendor": "GivEnergy"}`. Do not report the uuid, `alias` or `type`.
- Entities with `en = "{prefix}_gecloud_{serial}".lower()`, matching `async_automatic_config_evc()`: `car_charging_planned` = `binary_sensor.{en}_evc_car_connected`, `car_charging_plugged` = `binary_sensor.{en}_evc_car_plugged`, `car_charging_energy` = `sensor.{en}_evc_energy_active_import_register` and `car_charging_power` = `sensor.{en}_evc_power_active_import`. Do not report `_evc_soc`, which is published but never bound.
- Add the keys to the existing return value. Leave `"automatic"` and `"inverters"` untouched (see Out of scope).

- [ ] **Step 1: Write failing tests:**
  - `discovery_evc`: using `_make_rediscovery_ge_cloud(..., evc_devices=[{"uuid": "evc-1", "alias": "Garage"}])` and one run, the report has one charger with `device_id` `"gecloud:evc-1-sn"`, the stub car, and `"Garage"` absent.
  - `discovery_evc_round_trip`: an uppercase serial `"EV2244G123"` with all four lowercase-serial entities staged, through `Coordinator.report` then `assemble()`. All four entities survive validation *and* redaction, and `"EV2244G123"` is readable in the catalogue.
  - `discovery_evc_skips_charger_without_serial`: a uuid whose `evc_device` entry has no `serial_number` is not reported.
  - Update `evc_only_run`: it currently asserts the report is empty. Assert `report["inverters"] == []`, `report["automatic"] is False`, and `len(report["chargers"]) == 1`.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test ge_cloud > /tmp/t5.txt 2>&1`.
- [ ] **Step 3: Implement** as decided above.
- [ ] **Step 4: Run to verify they pass.** Expected: every `ge_cloud` sub-test passes, including the existing `discovery_*` set.
- [ ] **Step 5: Final verification, then commit.**
  - Run `./run_all --quick > /tmp/quick.txt 2>&1` and grep for failures. Expected: the suite is green.
  - From the repo root, `git add` everything, then run `./run_pre_commit`. Expected: every hook passes or is auto-fixed. Re-stage after the cspell dictionary sort.
  - Run `detect_changes({scope: "compare", base_ref: "main"})`. Expected: only the five components, `coordinator.py`, the tests and the docs.
  - Commit: `feat(gecloud): report GivEnergy EV chargers to the discovery catalogue`.
