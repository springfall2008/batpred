# Discovery Kraken Reporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `KrakenAPI` (EDF / E.ON Next) publishes `meters`, `cars` and `programmes` records to the discovery catalogue. These are the catalogue's first `programmes` records.

**Architecture:** add a `build_discovery()` modelled on `OctopusAPI.build_discovery()` (octopus.py ~1701-1810), plus one `self.refresh_discovery()` call per `run()` cycle. Observe-only: no binding, publish or fetch changes. Stacked on #5924 (identifiers published readable; only credentials and emails withheld).

**Spec:** [docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md](../specs/2026-09-10-discovery-catalogue-design.md), the `meters`, `cars` and `programmes` sections. Readability rules: [docs/discovery-catalogue.md](../../discovery-catalogue.md) "What is, and is not, redacted".

## Global Constraints

- **Readable ids:** identifiers are readable. Never read credentials: `_api_key`, `_email`, `_password`, `access_token`, `refresh_token`, `token_hash`, `token_expires_at`. Never report the account address.
- **Entities:** the ones Kraken actually binds, by Predbat setting name, filtered through `self.discovery_entities()` (the Octopus convention). Each is included under the same condition that gates its `set_arg`, ignoring the one-shot `wired` / `export_wired` latches, since the record describes the device, not whether wiring already happened.
- **Automatic:** `"automatic": True`. Kraken has no automatic flag; every `set_arg` it makes is unconditional.
- **Tests:** run from `coverage/`, write output to a file, then grep it.

## Decisions

- **Meters:**
  - **Import:** an import record when `self.current_tariff` is set.
    - `device_id` is `"kraken:{mpan}"`, where mpan is `self.import_mpan`, falling back to `self.configured_mpan`, then to the direction name.
    - `account_ids` is `{"mpan": ..., "account": self.account_id}`.
    - `tariff.info` holds `tariff_code` and `product_code`. There are no flags: Kraken exposes nothing to derive them from.
    - `ratings.standing_charge_p` is `self.import_standing_charge` (£/day) × 100, when known.
    - Entities: `metric_octopus_import` and `metric_standing_charge`.
  - **Export:** an export record when `self.export_tariff` is set.
    - The mpan is `self.export_mpan`, and the account is `self.export_account_id or self.account_id`.
    - Entity `metric_octopus_export`, only when `self.export_rates_available`.
  - No gas: Kraken does not fetch gas.
- **Cars:** one record per `self.intelligent_devices` entry (all are LIVE EVs).
  - `device_id` is `"kraken:{device_id}"`, and `account_ids` is `{"intelligent_device_id": device_id}`.
  - `info` holds `make` and `model` when present.
  - No ratings: battery size and charge-point power are never populated.
  - Entity `octopus_intelligent_slot` = `binary_sensor` from `get_entity_name("binary_sensor", "intelligent_dispatch_" + suffix)`, using the same suffix rule `_publish_dispatch_sensors()` uses.
- **Programmes:** one `free_electricity` record when the account is enrolled in EDF Flextras: `provider == "edf"` and `self.flextras_last_status == 200`.
  - `device_id` is `"kraken:flextras:{account}"`, and `meter` is the import meter's `device_id`.
  - `account_ids` is `{"account": self.account_id}`, plus `"programme_id": self.flextras_property_id` when known.
  - `effects` is `["rate_override"]`.
  - Entity `octopus_free_electricity` = the published free-electricity sensor.
  - Not enrolled (any other status, or E.ON): no programme record.
- **`build_discovery()`** returns `None` until a tariff has been found.
- **Placement:** `refresh_discovery()` goes in `run()` right after the `account_status` `dashboard_item` and before the cache save. It must sit before the `if first and (oauth_failed or not current_tariff): return False`.

## Review Focus

1. **E.ON split export account:** the export meter carries `export_account_id`, not the import account.
2. **Flextras not enrolled:** a 401/404 last status, or E.ON, must give no programme record, not an empty one.
3. **Unpublished entity:** an entity Home Assistant has not seen yet is not claimed.
4. **First-cycle failure:** a first cycle that fails (no tariff) files nothing and retries next cycle.
5. **Credentials:** none of the API key, email, password or tokens reaches the report.

---

### Task 1: Kraken reporter

**Files:** modify `apps/predbat/kraken.py` (new `build_discovery()`, plus the `run()` call); test in `apps/predbat/tests/test_kraken.py` (collected automatically by `run_kraken_tests`).

- [ ] **Step 1: Write failing tests:**
  - `test_kraken_discovery_none_before_tariff`
  - `test_kraken_discovery_import_meter`: device_id, account_ids, tariff info, `standing_charge_p` in pence, both entities.
  - `test_kraken_discovery_export_meter_uses_export_account`: E.ON split account; `metric_octopus_export` only when rates are available.
  - `test_kraken_discovery_cars`: one record per device, the slot entity id matching `_publish_dispatch_sensors()`'s, make and model in `info`.
  - `test_kraken_discovery_flextras_programme`: enrolled EDF gives a `free_electricity` programme cross-linked to the import meter.
  - `test_kraken_discovery_no_programme_when_not_enrolled`: covers status 401 and E.ON.
  - `test_kraken_discovery_entities_only_when_published`
  - `test_kraken_discovery_never_reads_credentials`: `str(report)` holds none of the key, email, password or token.
  - `test_kraken_discovery_round_trip`: through the real `Coordinator` with no `Warn`. The MPAN and account stay readable; the programme's `meter` equals the import meter's `device_id`.
  - `test_kraken_run_files_report_every_cycle`: `run(0, True)` with a tariff found files one report.
- [ ] **Step 2: Run to verify they fail.** Run `./run_all --test kraken > <ws>/k-red.txt 2>&1`. Expected: `AttributeError: build_discovery`.
- [ ] **Step 3: Implement** as decided.
- [ ] **Step 4: Run to verify they pass**, then run `./run_all --quick` and pre-commit on the changed files.
- [ ] **Step 5: Commit.** Message: `feat(kraken): report meters, SmartFlex cars and Flextras to the discovery catalogue`.
