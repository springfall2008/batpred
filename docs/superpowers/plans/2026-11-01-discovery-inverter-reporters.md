# Discovery Inverter Reporters (piece 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The five remaining inverter integrations publish discovery records that rebuild their `INVERTER_DEF` rows and bind exactly what their `automatic_config()` binds: Enphase, Teslemetry, SolaX, Sigenergy and the Predbat Gateway (MQTT). The Gateway also reports the EV charger it drives.

**Architecture:** First, extend the record vocabulary for three row fields added after piece 1 (`reserve_above_soc_charges_from_grid`, `write_max_retry`, `write_backoff`). In the same task, correct two `soc_units` row values that nothing reads. Then give each component a `build_discovery()` built with `inverter_record()`, plus one `self.refresh_discovery()` call per `run()` cycle. Each reporter is proven by `tests/discovery_contract.py`: completeness against its row with no base, and agreement both ways with captured `automatic_config()` output. The work is observe-only: no `automatic_config()`, control path or published entity changes.

**Tech Stack:** Python 3, Predbat's in-tree harness (`coverage/run_all`), pre-commit (ruff, black 256, cspell en-gb, interrogate 100%).

**Spec:** [docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md](../specs/2026-09-24-discovery-inverter-record-vocabulary-design.md) (sections 1-2, decisions D1-D15). Background: [docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md](../specs/2026-09-10-discovery-catalogue-design.md). Reference reporters: `SunsynkAPI` (`sunsynk.py` `_discovery_entities`, `build_discovery`; tests `tests/test_sunsynk_config.py:386-664`) and `GECloudDirect` (gateway and EMS composition, PV-only records, shared CT; tests `tests/test_ge_cloud.py` `_test_discovery_record_*`).

## Global Constraints

- Observe-only. Leave `automatic_config()`, every control path and every published entity as they are. The two Task 1 row edits are to a field nothing reads (`inv_soc_units` is assigned at `inverter.py:592` and never read).
- **Entity ids:** build each descriptor's `entity_id` from the same helper or f-string `automatic_config()` uses, never by re-typing the pattern.
- **No HA filtering:** inverter records are not filtered through `discovery_entities()` (spec 1.3). Teslemetry is the exception, because its `automatic_config()` itself binds four settings only when their entity exists (Task 3).
- **Descriptors:** every descriptor has `access` (`rw` for what Predbat writes, `r` otherwise), plus `domain` for controls, `unit` where the entity has one, and `format: "HH:MM:SS"` on time selects.
- **Capabilities:** every record states all seven `CAPABILITY_KEYS` as literals, equal to these rows (including the row defaults the contract uses):

  | Type | charge_freeze | discharge_freeze | feedin_first | span_midnight | with_rate | control_immediate | target_soc_for_discharge |
  |---|---|---|---|---|---|---|---|
  | SIGCLOUD | T | T | F | T | F | F | F |
  | SolaxCloud | T | T | T | T | F | F | T |
  | EnphaseCloud | T | T | F | F | F | F | T |
  | TESLA | T | F | F | F | F | F | T |
  | GWMQTT | T | T | F | T (EMS unit: F) | F | F | F |

  EnphaseCloud and TESLA also state `reserve_above_soc_charges_from_grid: True` (Task 1).
- **Ratings:** only figures the device reports (D14). A derived, estimated, capped or user-set figure is bound as an entity, never given as a rating. A rating that also has a sensor appears in both places (D6).
- **Ids:**
  - `device_id` is `"<component>:<hardware serial>"` when a hardware serial is known.
  - Cloud site and plant ids (Enphase `site_id`, Teslemetry `energy_site_id`, SolaX `plantId`) go in `account_ids` (pseudonymised; the Redactor also substitutes them inside entity ids when they are at least 6 characters long). The Sigenergy system id is the exception: it goes in `hardware_ids` and stays readable (maintainer decision, 2026-11-01).
  - If a record has no hardware serial, its `device_id` falls back to `"<component>:site:<account id>"`. That id is then identity-derived and redacted.
- **Never report:** emails, user names, site/plant/system names, addresses, lat/long, timezone, country, OAuth/MQTT credentials or tokens, the Gateway's `gateway_device_id` (its MQTT username), IP addresses or SSIDs.
- `"automatic"` is the flag that gates this component's `automatic_config()`.
- **Imports:** each component imports `inverter_record` (and the Gateway also imports `charger_with_stub_car`) from `coordinator` at module top. Test modules add `import predbat` before importing `tests.discovery_contract` (the circular-import guard `test_deye_api.py:11` uses).
- **Running tests:** from `coverage/`, write the output to a file and grep it afterwards. Never pipe tests straight to grep.

## Review Focus

1. **A cloud id leaking in clear.** Enphase `site_id` and SolaX `plantId` both appear inside entity ids. Each round-trip test feeds a realistic id of 7 or more characters through `Coordinator.catalogue()` and asserts the raw id is absent. The Sigenergy system id is deliberately readable (maintainer decision, 2026-11-01), so its test asserts it survives instead.
2. **A second unit later.** A plant, site or gateway that gains a unit re-files with one record per driven unit, in `automatic_config()` order. Tasks 4 and 6 each test a two-unit fixture.
3. **Before discovery completes.** `build_discovery()` returns `None` (not a half-built record) until the component holds the data `automatic_config()` needs. Each task tests the empty case.
4. **A failing `automatic_config()` must not block the report.** Enphase and SolaX raise `ValueError` from `automatic_config()`, and the Gateway defers. Each task places `refresh_discovery()` before that point, and its run-level test proves the report is filed on a cycle where `automatic_config()` raises or defers.
5. **Shared CT / site counters on the Gateway** (index ≥ 1 reads grid/load as 0 and carries no import/export/load totals). Task 6 copies GE Cloud's `_test_discovery_record_shared_ct` and its `_agree_at` helper.

## Out of scope (tell the maintainer, do not build)

- Teslemetry's `inverter_hybrid` and the Gateway's `inverter_hybrid` are written by `set_state_wrapper()` / `set_state_external()` rather than `set_arg`. The contract never sees them, and `functions` carries the same fact.
- Sigenergy `pv_today` binds every native PV lifetime and then every third-party one, as one 2N list. A record carries its own native entity. Piece 3 has to keep the third-party half, and `flags: ["third_party_gen"]` marks a system whose third-party entity is bound (Task 5).
- The Gateway's EMS `ems_total_*` settings are site aggregates outside any unit's record.

---

### Task 1: Vocabulary for the newer row fields, and `soc_units` row corrections

**Files:**
- Modify: `apps/predbat/coordinator.py`: `CAPABILITY_KEYS` (~39) and its validator key set; `inverter_definition()` (~1280)
- Modify: `apps/predbat/component_base.py` (beside `WRITE_AND_POLL_SLEEP`, ~64)
- Modify: `apps/predbat/tests/discovery_contract.py` (`ROW_DEFAULTS`, `assert_definition_complete`)
- Modify: `apps/predbat/config.py`: `SolaxCloud` row `soc_units` (~2505), `SIGCLOUD` row `soc_units` (~2556-2583)
- Modify: the spec's sections 1.1 and 2, plus a D16 row in its Decisions table
- Test: `apps/predbat/tests/test_coordinator.py` (register in `test_coordinator_all()`)

**Interfaces:**
- Produces:
  - `OPTIONAL_CAPABILITY_DEFAULTS = {"reserve_above_soc_charges_from_grid": False}` in `coordinator.py`. `capabilities` validation accepts `CAPABILITY_KEYS` plus these keys.
  - `inverter_definition(record, write_and_poll_sleep, base=None, write_max_retry=None, write_backoff=False)`:
    - For each optional capability: a bool in the record is used; if absent, the field is *not* a gap and takes `base`'s value, else the default (the `rate_step_percent_of_capacity` pattern).
    - `definition["write_max_retry"] = write_max_retry` only when not `None`.
    - `definition["write_backoff"] = True` only when `write_backoff` is true.
  - `ComponentBase.WRITE_MAX_RETRY = None` and `ComponentBase.WRITE_BACKOFF = False`.
  - `assert_definition_complete(record, write_and_poll_sleep, except_fields=(), write_max_retry=None, write_backoff=False)`, passing the two new arguments through. `ROW_DEFAULTS` gains `"reserve_above_soc_charges_from_grid": False` (`inverter.py:588`'s default).

- [ ] **Step 1: Write failing tests:**
  - `test_optional_capability_defaults_false_without_a_gap`: a record with the seven keys and no `reserve_above_soc_charges_from_grid` gives `definition[...] is False` and the key is not in `gaps`. With `capabilities[...] = True`, the definition is `True`.
  - `test_optional_capability_survives_validation`: `validate_report` keeps `{"reserve_above_soc_charges_from_grid": True}` in capabilities with no warning.
  - `test_definition_write_retry_from_component`: `inverter_definition(record, 10, write_max_retry=3, write_backoff=True)` contains `write_max_retry == 3` and `write_backoff is True`. Called with neither, it contains neither key.
  - `test_solax_and_sigenergy_rows_give_soc_units_kwh`: `INVERTER_DEF["SolaxCloud"]["soc_units"] == "kWh"` and the same for `"SIGCLOUD"`.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test coordinator > <ws>/t1-red.txt 2>&1`. Expected: assertion failures, and a `TypeError` on the unknown keyword.
- [ ] **Step 3: Implement** the interfaces above, and set both rows' `soc_units` to `"kWh"`. In the spec, add D16: "`reserve_above_soc_charges_from_grid` is an optional capability (absent = False); `write_max_retry` and `write_backoff` are component constants like `write_and_poll_sleep`; SolaxCloud and SIGCLOUD `soc_units` become kWh (never read, as D13)". Update sections 1.1 and 2 to match.
- [ ] **Step 4: Run to verify they pass, and that nothing regressed.** Run `./run_all --test coordinator --test discovery_catalogue --test sunsynk_config --test deye_api --test alphaess_api --test fox_api --test solis --test ge_cloud --test givtcp_component --test hanchu_api > <ws>/t1.txt 2>&1`. Expected: all pass, so every existing completeness test still holds with the new `ROW_DEFAULTS` entry. (Check exact names with `./run_all --list`.)
- [ ] **Step 5: Commit.** Message: `feat(discovery): vocabulary for reserve_above_soc_charges_from_grid and write retry/backoff`.

---

### Task 2: Enphase reporter

**Files:**
- Modify: `apps/predbat/enphase.py` (new `_discovery_entities(site_id)`, `_discovery_ratings(site_id)`, `build_discovery()`; `run()` ~470)
- Test: `apps/predbat/tests/test_enphase_api.py` (register in `run_enphase_api_tests`)

**Interfaces:**
- Consumes: `inverter_record` (existing) and Task 1's optional capability.
- Produces: `EnphaseAPI.build_discovery() -> dict | None`.

Decisions:
- **Shape:** one record for `self.sites[0]`, the site `automatic_config()` configures.
- **Readiness:** return `None` until that site has `battery_status[site]["max_capacity"]` and `today[site]["serial"]`.
- **Identity and composition:**
  - `device_id` is `"enphase:{envoy serial}"` (`gateway_serial()`).
  - `composition` is `"gateway"`, with `serials` set to the `serial_num` of each `battery_status[site]["batteries"]` entry (omitted when empty).
  - `hardware_ids` is `{"serial": envoy}`, and `account_ids` is `{"site_id": site_id}`.
  - `inverter_type` is `"EnphaseCloud"`.
  - `functions` is `["solar", "battery"]`.
  - `capabilities` are the table's values plus `reserve_above_soc_charges_from_grid: True`.
  - `info` is `{"vendor": "Enphase"}`.
- **Entities:** exactly the settings at `enphase.py:1504-1527`.
  - Every sensor is `r`, with unit kWh/W/% as published.
  - `reserve`, `charge_limit` and `discharge_target_soc` are `number` `rw` %.
  - The four times are `select` `rw` with `format "HH:MM:SS"`.
  - Both enables and `schedule_write_button` are `switch` `rw`.
  - `inverter_time` is `r` with `format "%Y-%m-%d %H:%M:%S"` (`ENPHASE_CLOCK_FORMAT`).
  - `battery_min_soc` is a sensor `r` %.
  - `pv_power` and `pv_today` are included whatever `automatic_ignore_pv` says (D11).
- **Ratings:**
  - `soc_max` = `max_capacity` (kWh).
  - `battery_rate_max` = `max_power_kw * 1000` (W), when reported.
  - `battery_min_soc` = `veryLowSocMin`, when reported (not the default 5).
- **`refresh_discovery()`:** immediately after the publish block ends (~470), before the `automatic_config()` call that can raise.

- [ ] **Step 1: Write failing tests** (site id `"4815162"`, Envoy `"122530006866"`, batteries from `BATTERY_STATUS_PAYLOAD`):
  - `test_enphase_catalogue_none_before_discovery`.
  - `test_enphase_record_shape`: the device_id, composition, serials, account_ids and ratings above.
  - `test_enphase_record_rebuilds_its_inverter_def_row`: `assert_definition_complete(record, EnphaseAPI.WRITE_AND_POLL_SLEEP)` with no `except_fields`.
  - `test_enphase_record_agrees_with_automatic_config`: `assert_record_agrees` plus `assert_record_binds_nothing_extra`.
  - `test_enphase_record_keeps_pv_despite_automatic_ignore_pv`: agreement with `allowed_extra=("pv_power", "pv_today")`.
  - `test_enphase_round_trip_hides_site_id`: through `Coordinator.report` then `catalogue()`, `"4815162"` is absent, the Envoy serial is present, and no `Warn` is logged.
  - `test_enphase_catalogue_filed_when_automatic_config_raises`: `run(0, True)` with `schedules[site]["cfg"]["supported"] = False` still files one report.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test enphase_api > <ws>/t2-red.txt 2>&1`. Expected: `AttributeError: build_discovery`.
- [ ] **Step 3: Implement** as decided.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit.** Message: `feat(enphase): report the battery site to the discovery catalogue`.

---

### Task 3: Teslemetry reporter

**Files:**
- Modify: `apps/predbat/teslemetry.py`:
  - keep the site_info `components` in `fetch_site_info()` (~459-535) as `self.site_components`
  - new `_discovery_entities()`, `_discovery_ratings()`, `build_discovery()`
  - `run()`, after ~649
- Test: `apps/predbat/tests/test_teslemetry.py` (register in `test_teslemetry()`)

**Interfaces:**
- Consumes: `inverter_record` and Task 1's optional capability.
- Produces: `TeslemetryAPI.build_discovery() -> dict | None`. `self.site_components` is a dict, `{}` until site_info has been read.

Decisions:
- **Readiness:** return `None` until `site_info_done`.
- **Identity:**
  - `device_id` is `"teslemetry:{gateway serial}"`, from `components.gateways[0].serial_number`. Fall back to `"teslemetry:site:{site_id}"` if there is no gateway serial.
  - `hardware_ids` is `{"serial": ...}` when known.
  - `account_ids` is `{"energy_site_id": str(self.site_id)}`.
  - Gateway `din` and `device_id` UUIDs are not reported.
- **Composition:** `"gateway"` with `serials` set to the `serial_number` of each `components.batteries[]` entry when there are any, otherwise `"direct"`.
- **Functions:** `["solar", "battery"]` when `self.powerwall_3`, else `["battery"]`. A Powerwall 2 is AC-coupled; its solar is a separate inverter the gateway only meters.
- **Type and capabilities:** `inverter_type` is `"TESLA"`. `capabilities` are the table's values plus `reserve_above_soc_charges_from_grid: True`.
- **Info:** `{"vendor": "Tesla", "model": gateways[0].part_name, "firmware": gateways[0].firmware_version}`, keeping only the values present.
- **Entities:** exactly `teslemetry.py:937-964`.
  - `grid_power` carries `invert: True`, and `battery_power` carries no invert (it is False).
  - `soc_max`, `battery_rate_max`, `inverter_limit`, `inverter_limit_charge` and `export_limit` are included only when `get_state_wrapper(entity) is not None`, the same test `automatic_config()` applies at 949-951.
  - `battery_export_permitted` is a `binary_sensor` `r`.
  - The schedule controls follow the reference shape (numbers %, selects HH:MM:SS, switches `rw`, `schedule_write_button` a `switch` `rw`).
- **Ratings:**
  - `soc_max` = nameplate energy kWh, when site_info reports one. The `battery_count` estimate is not a rating.
  - `inverter_limit` = `nameplate_power` W.
  - `export_limit` = `min_site_meter_power_ac` W, when not None.
  - Not ratings: `inverter_limit_charge` (derived 5000 W per unit) and the backup reserve (a setting).
- **`refresh_discovery()`:** right after the last publish (~649), before the auto-config gate.

- [ ] **Step 1: Write failing tests**, using the fixtures `SITE_INFO_AC_POWERWALL`, `SITE_INFO_PW3_EXPANSION` and `LIVE_STATUS`:
  - `test_teslemetry_catalogue_none_before_site_info`.
  - `test_teslemetry_record_ac_powerwall`: functions `["battery"]`, gateway composition, battery serials, and the gateway serial as hardware.
  - `test_teslemetry_record_pw3`: functions `["solar", "battery"]`.
  - `test_teslemetry_record_rebuilds_its_inverter_def_row`: no `except_fields`.
  - `test_teslemetry_record_agrees_with_automatic_config`: both directions.
  - `test_teslemetry_conditional_ratings_follow_entity_presence`: with `export_limit`'s entity absent, the record has no `export_limit` entity, and agreement still holds.
  - `test_teslemetry_round_trip_hides_site_id`: a 7-digit `site_id` is absent from the catalogue, and the site name from site_info is absent from the raw report.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test teslemetry > <ws>/t3-red.txt 2>&1`.
- [ ] **Step 3: Implement** as decided.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit.** Message: `feat(teslemetry): report the Powerwall site to the discovery catalogue`.

---

### Task 4: SolaX reporter

**Files:**
- Modify: `apps/predbat/solax.py` (new `_discovery_entities(plant_id)`, `_discovery_ratings(plant_id)`, `build_discovery()`; `run()` between ~3089 and ~3091)
- Test: `apps/predbat/tests/test_solax.py` (async tests inside `run_solax_tests`)

**Interfaces:**
- Consumes: `inverter_record`.
- Produces: `SolaxAPI.build_discovery() -> dict | None`.

Decisions:
- **Shape:** one record per plant that `automatic_config()` would configure (`solax.py:452-465`: it has inverters, and a battery device with that `plantId`), in the same order. Return `None` when there are none.
- **Identity:**
  - `device_id` is `"solax:{plant_inverters[p][0]}"`.
  - `serials` is the plant's inverter SNs when there is more than one.
  - `hardware_ids` is `{"serial": first inverter SN}`.
  - `account_ids` is `{"plant_id": p}`.
  - `composition` is `"direct"`.
- **Type and capabilities:** `inverter_type` is `"SolaxCloud"`, `functions` is `["solar", "battery"]`, and `capabilities` are the table's values.
- **Info:** `{"vendor": "SolaX", "model": the SOLAX_DEVICE_MODEL_RESIDENTIAL name of the first inverter's deviceModel, "firmware": "ARM {armVersion} DSP {dspVersion}"}`, keeping only what is present.
- **Entities:** exactly `solax.py:479-515`.
  - Plant entities use the raw `plantId` (as `automatic_config()` does); `load_power` and `battery_scaling` use `{p}_{inv}`.
  - `soc_kw` is a sensor `r` kWh.
  - `battery_power` carries `invert: True` (`battery_power_invert = "True"`).
  - `charge_rate` and `discharge_rate` are `number` `rw` W.
  - `reserve` is `number` `rw` %.
  - `battery_min_soc` is `{"value": SOLAX_MIN_RESERVE_PERCENT, "access": "r"}` (the spec's own example of a `value` stand-in).
  - `battery_temperature_history` goes on the first plant's record only (D15).
- **Ratings:**
  - `inverter_limit` = the sum of the plant's inverter `ratedPower` × 1000 (W).
  - `soc_max` = `plant_info` `batteryCapacity`, when reported.
  - `battery_rate_max` = the battery device's own `ratedPower` × 1000, when present (not the inverter-power fallback).
- **`refresh_discovery()`:** after the publish block (~3089), before the `if first and self.automatic:` `automatic_config()` call, which can raise.

- [ ] **Step 1: Write failing tests** (plant `"1618699116555534337"`, inverter `"H1231231932123"`, battery `"TP123456123123"`, seeding `realtime_device_data` or `dashboard_items` so `load_power`/`battery_soh` exist):
  - `test_solax_catalogue_none_without_a_battery_plant`.
  - `test_solax_record_shape`.
  - `test_solax_record_rebuilds_its_inverter_def_row`: no `except_fields`; this needs Task 1's `soc_units` fix.
  - `test_solax_record_agrees_with_automatic_config`: both directions.
  - `test_solax_two_plants_two_records`: two plants, each record agreeing at its own index; `battery_temperature_history` is only on the first.
  - `test_solax_round_trip_hides_plant_id`: the plant id is absent from the catalogue, and `plantName` is absent from the raw report.
  - `test_solax_catalogue_filed_when_automatic_config_raises`.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test solax > <ws>/t4-red.txt 2>&1`.
- [ ] **Step 3: Implement** as decided.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit.** Message: `feat(solax): report plants to the discovery catalogue`.

---

### Task 5: Sigenergy reporter

**Files:**
- Modify: `apps/predbat/sigenergy.py` (new `_discovery_entities(sid)`, `_discovery_ratings(sid)`, `build_discovery()`; `run()` after ~3522)
- Test: `apps/predbat/tests/test_sigenergy.py` (add to the `tests` list in `run_sigenergy_tests`)

**Interfaces:**
- Consumes: `inverter_record`.
- Produces: `SigenergyAPI.build_discovery() -> dict | None`.

Decisions:
- **Shape:** one record per system in `self.systems`, in `automatic_config()` order. Return `None` when there are none.
- **Identity:**
  - `device_id` is `"sigenergy:{_get_inverter_serial(sid)}"`, or `"sigenergy:system:{sid}"` if there is none.
  - `serials` is the serials of every Inverter/AIO device when there is more than one.
  - `hardware_ids` is `{"serial": ...}` when known, plus `"system_id": sid`. The system id is not secret (maintainer decision, 2026-11-01), so it stays readable, as do the entity ids that embed its slug. No `account_ids`.
  - `composition` is `"direct"`.
- **Functions:** `["solar", "battery"]` unless `apply_hybrid_verdict()` has classed the system as PV-only (read its result; do not re-derive it), in which case `["solar"]`.
- **Type, capabilities, info:** `inverter_type` is `"SIGCLOUD"` and `capabilities` are the table's values. `info` is `{"vendor": "Sigenergy"}`; model and firmware are not held.
- **Entities:** exactly `sigenergy.py:2540-2578`, using `_system_slug(sid)` exactly as `automatic_config()` does.
  - `soc_kw` is a sensor `r` kWh.
  - `inverter_time` is `r` with `format "%Y-%m-%dT%H:%M:%S%z"` (what the sensor publishes).
  - `reserve` is `number` with **`access: "r"`**: SIGCLOUD has no reserve control, and the component overwrites it from the discharge cut-off.
  - Rates are `number` `rw` W.
  - `pv_today` is this system's native `pv_lifetime` entity.
  - Add `flags: ["third_party_gen"]`, since `automatic_config()` always binds the third-party PV lifetime too.
- **Ratings:**
  - `inverter_limit` = the sum of Inverter `attrMap.ratedActivePower` kW × 1000.
  - `soc_max` = `systems[sid]["batteryCapacity"]`, when reported (not the Ah-derived fallback).
  - `export_limit` = the configured backfeed W when anti-reflux is on (never the 99999 "unknown").
  - `battery_min_soc` = the backup cut-off %, when known.
- **`refresh_discovery()`:** after the `automatic_config()` block (~3522) and outside it, so it runs every cycle.

- [ ] **Step 1: Write failing tests** (the survey skeleton: `SIG001`-style id replaced by a realistic 19-character id, one inverter, one battery, MQTT cut-off and backfeed, published controls):
  - `test_sigenergy_catalogue_none_before_discovery`.
  - `test_sigenergy_record_shape`: reserve `access == "r"`, `third_party_gen` flag, ratings.
  - `test_sigenergy_record_rebuilds_its_inverter_def_row`: `except_fields=("clock_time_format",)` (the sensor publishes ISO; D13's GE precedent). `soc_units` needs Task 1's row fix.
  - `test_sigenergy_record_agrees_with_automatic_config`: both directions.
  - `test_sigenergy_two_systems_two_records`.
  - `test_sigenergy_round_trip_keeps_system_id`: through `Coordinator.catalogue()`, the system id and the entity ids stay readable with no `Warn` logged (if a guard pseudonymises a long all-digit id, that is a finding to report, not to work around), and `systemName` is absent from the raw report.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test sigenergy > <ws>/t5-red.txt 2>&1`.
- [ ] **Step 3: Implement** as decided.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit.** Message: `feat(sigenergy): report systems to the discovery catalogue`.

---

### Task 6: Gateway (MQTT) inverter reporter

**Files:**
- Modify: `apps/predbat/gateway.py`:
  - `WRITE_AND_POLL_SLEEP = 10`, `WRITE_MAX_RETRY = 3`, `WRITE_BACKOFF = True` class constants (check `WRITE_AND_POLL_SLEEP` first; the row says 10)
  - new `_discovery_units()`, `_discovery_entities(unit, index, n)`, `build_discovery()`
  - `run()` → see the placement below
- Test: `apps/predbat/tests/test_gateway.py` (a new `TestDiscovery` class listed in `run_gateway_tests`)

**Interfaces:**
- Consumes: `inverter_record`; Task 1's `write_max_retry` / `write_backoff`.
- Produces:
  - `GatewayMQTT.build_discovery() -> dict | None`, returning `{"automatic": not self.gateway_display_only, "inverters": [...], "chargers": [...], "cars": [...]}`. Task 7 fills `chargers` and `cars`; Task 6 returns them empty.
  - `_discovery_units() -> (driven, pv_only, composition, fronted)`, the exact topology selection `automatic_config()` makes at `gateway.py:2200-2283`. Extract that selection into this helper and call it from `automatic_config()` too, so the two cannot drift. That keeps `automatic_config()`'s behaviour byte-identical; the existing `TestAutomaticConfig` suite proves it.

Decisions:
- **Readiness:** return `None` when display-only discovery has nothing, when there is no `_last_status`, or when no battery inverter has been found (the same cases where `automatic_config()` returns early).
- **One record per driven unit**, sorted by serial:
  - `device_id` is `"gateway:{inv.serial}"` and `hardware_ids` is `{"serial": inv.serial}`. These are the same serials GivTCP and GE Cloud report, so `duplicate_serial` fires when two of them describe one box.
  - `inverter_type` is `"GWMQTT"` and `functions` is `["solar", "battery"]`.
  - Composition: an EMS-driven record is `"ems"` and a Gateway-driven record is `"gateway"`, each with `serials` set to the units it fronts; otherwise `"direct"`.
- **Info:** `{"vendor": <inv.type name>, "model": inv.model, "firmware": status firmware}`, keeping only what is present. `inv.type` can be non-GivEnergy.
- **Capabilities:** the table's values, with `can_span_midnight: False` on an EMS unit (its selects carry that attribute, and `inverter.py:614` applies it).
- **PV-only units:** each gets a record with `functions: ["solar"]`, no `inverter_type`, no `capabilities`, and `pv_power` / `pv_today` as `r` entities (D12, as GE Cloud does).
- **Entities** follow `gateway.py:2323-2412`, keyed by `{prefix}_gateway_{serial[-6:].lower()}`:
  - `charge_rate` and `discharge_rate` are `number` `rw` W, `step` 10, with `step_percent_of_capacity: 1` where the published entity carries it (GivEnergy types, `gateway.py:1594-1600`).
  - `inverter_time` is `r` with `format "%Y-%m-%dT%H:%M:%S%z"`.
  - `soc_max` is included only when a capacity is known.
  - Shared CT: units at index ≥ 1 get `grid_power` / `load_power` `{"value": 0, "access": "r"}`, and no `import_today` / `export_today` / `load_today`.
  - `battery_temperature_history` is on the first record only.
  - Settings `automatic_config()` sets to `None` are left out.
- **Ratings:** `inverter_limit` = `inv.inverter.rate_max_w`, `battery_rate_max` = `inv.battery.rate_max_w`, `soc_max` = `capacity_wh / 1000`, and `export_limit` = `inv.control.export_limit_w` (only when the device has reported it: not 0, and not 99999). The 6000 W default is not a rating.
- **`refresh_discovery()` placement:** rename the existing `run()` body to `_run_cycle(seconds, first)`, unchanged. The new `run()` does `result = await self._run_cycle(seconds, first)`, then `self.refresh_discovery()`, then `return result`. That covers the `first` path, the display-only returns and the steady state with one call.

- [ ] **Step 1: Write failing tests** (reuse `TestAutomaticConfig._make_gateway`, `_make_inverter` and `_basic_status`; copy GE Cloud's `_agree_at` helper):
  - `test_none_before_status`.
  - `test_single_aio_record`: shape, hardware serial, and `step_percent_of_capacity` 1.
  - `test_single_aio_rebuilds_row`: `assert_definition_complete(record, GatewayMQTT.WRITE_AND_POLL_SLEEP, except_fields=("clock_time_format", "rate_step_percent_of_capacity"), write_max_retry=GatewayMQTT.WRITE_MAX_RETRY, write_backoff=GatewayMQTT.WRITE_BACKOFF)`. Both are runtime entity overrides of the row (`inverter.py:614-629`).
  - `test_single_aio_agrees`: with `gateway_evc_automatic` False.
  - `test_shared_ct_two_aios`: per-index agreement via `_agree_at`, plus the value stand-ins.
  - `test_gateway_composition_fronts_aios`: two or more battery AIOs behind a Gateway give one `"gateway"` record listing both serials.
  - `test_ems_record`: `"ems"` composition, `can_span_midnight` False, and completeness with `can_span_midnight` added to `except_fields`.
  - `test_pv_only_unit_record`.
  - `test_automatic_config_unchanged`: the existing `TestAutomaticConfig` suite passes untouched.
  - `test_run_files_report_on_first_cycle`: `run(0, True)` on a display-only gateway with status still files a report, with `automatic` False.
  - `test_no_credentials_in_report`: the gateway device id, MQTT token and IP are absent from `str(report)`.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test gateway > <ws>/t6-red.txt 2>&1`.
- [ ] **Step 3: Implement** as decided. Extract `_discovery_units()` first, with `TestAutomaticConfig` green before any reporter code is added.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit.** Message: `feat(gateway): report driven inverters to the discovery catalogue`.

---

### Task 7: Gateway EV charger records

**Files:**
- Modify: `apps/predbat/gateway.py` (`build_discovery()` fills `chargers` / `cars`)
- Test: `apps/predbat/tests/test_gateway.py` (`TestDiscovery`)

**Interfaces:**
- Consumes: `charger_with_stub_car` (from `coordinator.py`, merged in #5853); Task 6's `build_discovery()`.

Decisions:
- **Which chargers:** one charger plus a stub car per EV charger the status reports, whatever `gateway_evc_automatic` says.
- **Identity:**
  - `device_id` is `"gateway:evc:{charge_point_id}"`.
  - `hardware_ids` is `{"charge_point_id": charge_point_id}` (not secret: maintainer decision of 2026-10-29 for charger ids).
  - `info` is `{"vendor": ...}` from the charger's reported vendor, when present.
- **Entities:** the charger's entities are the ones `gateway.py:2478-2531` binds, keyed by Predbat name, minus `car_charging_now` (which only re-binds the power entity, matching the other chargers) and minus `car_charging_rate`, which goes through `expose_config` and is not a binding. Filter them through `self.discovery_entities()` (the charger convention).

- [ ] **Step 1: Write failing tests:**
  - `test_ev_charger_record`: one charger and a stub car, with the four entities.
  - `test_ev_charger_reported_without_evc_automatic`.
  - `test_no_ev_charger_no_records`.
  - `test_ev_round_trip`: through `Coordinator`, the stub flag and entities survive.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify they pass, then do the final verification:**
  - Run `./run_all --quick > <ws>/quick.txt 2>&1`. Expected: green.
  - From the repo root, `git add` the changes and run `coverage/venv/bin/python3 -m pre_commit run --files` with the branch's changed files (via `xargs`, so zsh splits them). Expected: every applicable hook passes.
  - Run `detect_changes({scope: "compare", base_ref: "origin/main"})`. Expected: only the five components, `coordinator.py`, `component_base.py`, `config.py` (two `soc_units` values), the tests, the contract helper and the docs.
- [ ] **Step 5: Commit.** Message: `feat(gateway): report the EV charger to the discovery catalogue`.
