# Octopus/Kraken Billing in the History View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read half-hourly billing from the Kraken `measurements` API for Octopus and Kraken (EDF/E.ON) users, publish it as `predbat.billing`, and show it alongside Predbat's own cost on the Plan page's History view, with a 7-day picker.

**Architecture:** A new pure module `billing.py` parses `measurements` responses, keeps a 7-day slot cache, builds the sensor, and attaches billing to History-view plan JSON. `OctopusAPI` and `KrakenAPI` each call it on a 3-hour cadence and publish the same `predbat.billing` entity. `calculate_yesterday()`'s replay is extracted into a reusable per-day function, so the six older days are rebuilt once a day and published as `predbat.plan_history`. The Plan page renders the extra columns, the summary strip and the day picker from JSON.

**Tech Stack:** Python 3 (aiohttp, asyncio), the Predbat component framework (`ComponentBase`, Storage, `dashboard_item`), embedded JS in `web_helper.py`, the custom `unit_test.py` harness.

**Spec:** `docs/superpowers/specs/2026-11-01-octopus-billing-history-design.md`. Read it first; this plan argues from it.

## Global Constraints

- Line length 256 (Black) / 250 (Flake8); 100% docstrings (`interrogate`); British English (CSpell, add new words to `.cspell/custom-dictionary-workspace.txt`); `lower_case_with_underscores` names.
- Components read config via `get_arg`, never `self.base.<attr>` / `getattr(self.base, ...)`. File access goes through the Storage component (`self.storage.load/save`).
- **Run GitNexus `impact` (upstream) on every existing symbol before editing it, and report the blast radius.** Warn the user on HIGH/CRITICAL. Run `detect_changes()` before every commit.
- Tests: new tests go in `apps/predbat/tests/test_billing.py` (and the modules named per task). Each exported runner returns `failed` (bool) and is registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`. Run from `coverage/`: `./run_all --test <name> > /tmp/... 2>&1`, then grep the saved file. Never pipe straight to grep.
- Bucket labels are never matched by name. Store whatever labels arrive.
- **Bad data is worse than no data.** If a figure cannot be trusted, hide it.
- Billing amounts are stored in **pence**; plan JSON cost fields stay in the currency's major unit (as `cost_change` is today).
- Enabled automatically for every running `OctopusAPI` / `KrakenAPI`. No new config switch.
- Only the MPANs Predbat models: Octopus `self.tariffs["import"]["mpan"]` / `self.tariffs["export"]["mpan"]`; Kraken `self.import_mpan` / `self.export_mpan` (export queried on `self.export_account_id or self.account_id`).

## Review Focus

1. **Clock-change days (46/50 half-hours).** A day is complete at its *actual* slot count in local time; it is never shown as incomplete or with a shifted row mapping. Test in Task 2 (`complete` on 25 Oct 2026, 50 slots) and Task 5 (rows map by UTC instant, not by local hour).
2. **A Kraken/Octopus response that returns `data` with partial `errors`** (e.g. `KT-CT-4718` on one MPAN). It must count as a failed read for that leg, never as an empty day. Test in Task 3.
3. **`plan_interval_minutes` other than 30** (e.g. 60). A row sums the billing slots inside it, and a row only partly covered by published billing shows "—". Test in Task 5.
4. **A row whose Predbat cost includes the midnight standing-charge step.** Diff must not show the standing charge as a mismatch. Test in Task 5.
5. **Restart mid-cycle.** The billing cache is restored from Storage and not refetched until it is due, and complete days are never refetched. Test in Task 3.

---

## File Structure

| File | Responsibility |
| ---- | -------------- |
| `apps/predbat/billing.py` (create) | Pure functions: parse `measurements`, merge/prune the slot cache, day completeness, sensor state/attributes, attach billing + summary to plan JSON, the shared async `BillingReader` used by both components |
| `apps/predbat/octopus.py` (modify) | `OctopusAPI.run()` calls the billing reader every 180 min; billing cache in Storage |
| `apps/predbat/kraken.py` (modify) | `KrakenAPI.run()` does the same |
| `apps/predbat/output.py` (modify) | Extract the History replay into `replay_history_day()`; add `calculate_history_days()`; attach billing to yesterday JSON and the older days |
| `apps/predbat/predbat.py` or wherever `calculate_yesterday()` is called (modify) | Call `calculate_history_days()` next to it |
| `apps/predbat/web.py` (modify) | `/api/plan_data` adds `history_days`; new `/api/plan_history?date=` |
| `apps/predbat/web_helper.py` (modify) | Day picker, Billed/Diff columns, summary strip, bucket hover, event highlight |
| `apps/predbat/tests/test_billing.py` (create) | Tests for `billing.py` and the component integrations |
| `apps/predbat/tests/test_history_days.py` (create) | Tests for the replay extraction and older days |
| `apps/predbat/tests/test_billing_client_js.py` (create) | Structural JS tests (same pattern as `tests/test_debug_history_client_js.py`) |
| `docs/` (modify) | The new sensor and the History-view additions |

---

### Task 1: Verify the live data shape (spike, needs the user)

Nothing else is built until this is done. It settles the facts the parser depends on.

**Files:**
- Create: `apps/predbat/tests/billing_fixtures.py`: anonymised responses as Python dicts (MPANs, account numbers and addresses replaced)
- Modify: `docs/superpowers/specs/2026-11-01-octopus-billing-history-design.md`: append a "Verified data shape" section

**Interfaces:**
- Produces: `OCTOPUS_IOG_IMPORT_DAY`, `OCTOPUS_EXPORT_DAY` (or `None` if export returns nothing), `EON_IMPORT_DAY` (if an E.ON account is available) in `tests/billing_fixtures.py`. Each is the `data` part of a `MEASUREMENTS_QUERY` response for one complete local day.

- [ ] **Step 1: Write a scratch script** (in the session scratchpad, not the repo) that obtains a Kraken token the way `OctopusAPI` does and runs `octopus.MEASUREMENTS_QUERY` for the import MPAN, then the export MPAN, for one complete local day, printing the raw JSON. Do the same against `https://api.eonnext-kraken.energy/v1/graphql/` if an E.ON account is available.
- [ ] **Step 2: Ask the user to run it** (it needs their API key; never store the key) and paste or save the output.
- [ ] **Step 3: Record the answers** in the spec's new section, each with the evidence:
  - `estimatedAmount` units (pence or pounds) for `costInclTax`;
  - whether `statistics` holds only per-bucket rows, or also a total row (so bucket kWh would double the node `value`);
  - the standing-charge statistic's shape (`value` 0/None and non-zero cost?);
  - export: does the export MPAN return edges, and is its cost positive (earnings)?
  - whether `costInclTax ÷ kWh` matches Predbat's inc-VAT `value_inc_vat` rate for the same slots on the user's tariff.
- [ ] **Step 4: Save the anonymised fixtures** to `tests/billing_fixtures.py` with a module docstring saying where and when each came from.
- [ ] **Step 5: Gate.** If any answer contradicts Task 2's rules (bucket = statistic with kWh > 0; standing = kWh 0 and cost ≠ 0; `costInclTax`; pence), stop and update this plan and the spec before Task 2.
- [ ] **Step 6: Commit** `tests/billing_fixtures.py` and the spec update: `git commit -m "test(billing): record live measurements fixtures and verified data shape"`

---

### Task 2: `billing.py`: parse, cache, completeness, sensor

**Files:**
- Create: `apps/predbat/billing.py`
- Create: `apps/predbat/tests/test_billing.py` (runner `run_billing_tests(my_predbat) -> bool`)
- Modify: `apps/predbat/unit_test.py`: import and register `("billing", run_billing_tests, "Octopus/Kraken billing parser, cache, sensor and History attach", False)`

**Interfaces:**
- Consumes: fixtures from Task 1.
- Produces (all in `billing.py`):
  - `BILLING_DAYS = 7`, `BILLING_REFRESH_MINUTES = 180`, `BILLING_ENTITY_SUFFIX = "billing"`
  - `parse_measurements(response_data: dict | None) -> dict[str, dict] | None`: keyed by slot start as `"%Y-%m-%dT%H:%M:%SZ"` UTC. Each value is `{"kwh": float, "cost": float, "standing": float, "buckets": {label: {"kwh": float, "cost": float}}}` in pence. Returns `None` if `response_data` is `None` (a failed read), and `{}` for a read with zero edges.
  - `day_slot_starts(day: date, tz) -> list[datetime]`: the UTC starts of every half-hour in that local day (46, 48 or 50).
  - `merge_slots(cache: dict, leg: str, new_slots: dict, now_utc: datetime, tz) -> dict`: returns a new cache with `new_slots` written under `cache[leg]` and anything before local midnight `BILLING_DAYS` days ago dropped.
  - `days_to_fetch(cache: dict, leg: str, now_utc: datetime, tz) -> list[date]`: local dates in the window (the `BILLING_DAYS` days before today, plus today) that are not complete for that leg.
  - `build_billing_sensor(cache: dict, legs: dict, supplier: str, now_utc: datetime, tz) -> tuple[float | None, dict]`: returns `(state, attributes)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_billing.py`:
  - `test_parse_iog_buckets`: parse `OCTOPUS_IOG_IMPORT_DAY`. Assert 48 slots; for every slot, `abs(sum(b["kwh"]) - kwh) < 0.01` and `cost == sum(b["cost"])`; the standing charge appears in exactly one slot's `standing` and in no `buckets`; labels are exactly the set found in the fixture (no hard-coded names in `billing.py`).
  - `test_parse_unknown_labels`: a synthetic node whose statistics are labelled `"Zebra"` and `"Yak"` gives buckets `{"Zebra": …, "Yak": …}`.
  - `test_parse_bucket_mismatch_drops_slot`: a node with `value` 1.0 whose buckets sum to 2.0 is left out of the result (an untrusted slot).
  - `test_parse_none_vs_empty`: `parse_measurements(None) is None`; `parse_measurements({"account": {"properties": [{"measurements": {"edges": []}}]}}) == {}`.
  - `test_day_slot_starts_clock_change`: `len(day_slot_starts(date(2026, 10, 25), ZoneInfo("Europe/London"))) == 50`, `… date(2026, 3, 29) … == 46`, `… date(2026, 11, 2) … == 48`.
  - `test_days_to_fetch_skips_complete`: with 48 import slots cached for 31 Oct and none for 1 Nov, `now` = 2 Nov 10:00 local. Assert 31 Oct is not in the result, 1 Nov and 2 Nov are, and nothing is before 26 Oct.
  - `test_merge_prunes_old`: a slot dated 20 Oct is gone after a merge at 2 Nov.
  - `test_sensor_shape`: from a cache holding a complete 1 Nov (import and export) and a partial 2 Nov, `build_billing_sensor` returns `state` equal to 1 Nov's net energy cost in pence. Attributes include `supplier`, `legs` (`{"import": True, "export": True}`), `complete_until` (ISO UTC end of the last contiguous published import slot), `slots` (`{"import": {...}, "export": {...}}`), and `days` (`{"2026-11-01": {"complete": True, "import_cost", "export_cost", "standing", "import_kwh", "export_kwh", "buckets": {label: {"kwh", "cost"}}}}`), plus `friendly_name`, `unit_of_measurement` `"p"` and `icon` `"mdi:receipt-text"`.
  - `test_sensor_no_data`: an empty cache gives `state is None`.
- [ ] **Step 2: Run** `cd coverage && ./run_all --test billing > /tmp/billing.txt 2>&1; grep -n "ERROR\|FAIL\|passed" /tmp/billing.txt`. Expected: import error (module missing).
- [ ] **Step 3: Implement the functions** in `billing.py` with the signatures above. A statistic with kWh > 0 is a bucket, keyed by `label` (falling back to `type` when there's no label). A statistic with kWh 0/None and cost ≠ 0 is standing charge. Ignore statistics with zero kWh and zero cost. Cost is `costInclTax.estimatedAmount`, scaled to pence per Task 1. A slot is complete for a day only when every start from `day_slot_starts` is present.
- [ ] **Step 4: Run the tests.** Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "feat(billing): parse Kraken measurements into a 7-day half-hour billing cache"`

---

### Task 3: `BillingReader` + Octopus integration

**Files:**
- Modify: `apps/predbat/billing.py`: add `BillingReader`
- Modify: `apps/predbat/octopus.py`: `OctopusAPI.__init__`/`initialize`, `run()` (around lines 824-930), plus `load_octopus_cache` and `save_octopus_cache` are **not** reused; billing gets its own Storage key
- Test: `apps/predbat/tests/test_billing.py`

**Interfaces:**
- Consumes: Task 2 functions; `octopus.MEASUREMENTS_QUERY`; `OctopusAPI.async_graphql_query(query, request_context, ignore_errors=False)`.
- Produces:
  - `class BillingReader` in `billing.py`: `__init__(self, log, storage, storage_app: str, storage_name: str, tz)`; `async load(self) -> None`; `async refresh(self, query_fn, account_id: str, mpans: dict[str, tuple[str, str] | None], now_utc: datetime) -> bool` (where `mpans` = `{"import": (account_id, mpan), "export": (account_id, mpan) or None}` and `query_fn(query: str, context: str) -> awaitable dict | None`; returns True if anything new was stored); `due(self, now_utc) -> bool` (180 min since `fetched_at`, or never fetched); `sensor(self, supplier, now_utc) -> tuple[float | None, dict]`.
  - Publishes entity `self.base.prefix + ".billing"` (i.e. `predbat.billing`) via `dashboard_item(..., app="octopus")`.

- [ ] **Step 1: Write the failing tests:**
  - `test_reader_fetches_only_incomplete_days`: a mocked `query_fn` records each formatted query. With 31 Oct complete in the cache, `refresh` at 2 Nov 10:00 issues no query whose `startAt` is 31 Oct, and does issue queries for 1 Nov and 2 Nov.
  - `test_reader_failed_read_marks_leg`: `query_fn` returns `None` for the export MPAN. After `refresh`, `sensor()` attributes have `legs["export"] is False`, and the export cache is unchanged (not wiped).
  - `test_reader_partial_errors_is_failure`: (Review Focus 2) an `OctopusAPI` whose transport returns `{"data": {...}, "errors": [{"extensions": {"errorCode": "KT-CT-4718"}}]}` makes the Octopus `query_fn` adapter return `None`, so the day is not stored as empty.
  - `test_reader_restart_restores_cache`: (Review Focus 5) save via a fake Storage, construct a new reader, `load()`. `due()` is False when `fetched_at` was 10 minutes ago, and the slots are restored.
  - `test_octopus_run_publishes_billing`: drive `OctopusAPI.run()` with the test infra's mocked GraphQL (as the existing Octopus tests do) returning the Task 1 fixture. Assert `predbat.billing` was published, and that the import query used `tariffs["import"]["mpan"]` and no other MPAN.
- [ ] **Step 2: Run** `./run_all --test billing > /tmp/billing.txt 2>&1` and grep. Expected: the new tests FAIL.
- [ ] **Step 3: Implement `BillingReader`.** Query one local day per request (`first` = 50), using `MEASUREMENTS_QUERY` with `frequency="THIRTY_MIN_INTERVAL"`. Store under Storage `(storage_app, storage_name)` with a 14-day expiry, saving `{"slots": ..., "legs": ..., "fetched_at": ISO}`.
- [ ] **Step 4: Wire it into `OctopusAPI`.** Run `impact` on `OctopusAPI.run` first. Construct the reader in `initialize` with `storage_app="octopus_user"` and `storage_name="billing"`, and `await load()` inside the `if first:` block. After the tariff block, `if self.billing.due(now)`: call `refresh` with a `query_fn` that wraps `self.async_graphql_query(query, context)` and returns `None` whenever the raw response carried `errors`. Then publish the sensor whenever it refreshed, and on `first`. Skip the whole block when `"import"` is not in `self.tariffs` or has no `mpan`.
- [ ] **Step 5: Run the tests**, plus `./run_all -k octopus_ > /tmp/oct.txt 2>&1`. Expected: all pass, and no existing Octopus test changes.
- [ ] **Step 6: Commit** `git commit -m "feat(octopus): publish half-hourly billing as predbat.billing"`

---

### Task 4: Kraken integration

**Files:**
- Modify: `apps/predbat/kraken.py`: `KrakenAPI.initialize`, `run()` (lines ~1800-1957)
- Test: `apps/predbat/tests/test_billing.py` (reuse `make_kraken_api` / `attach_fake_storage` from `tests/test_kraken.py`)

**Interfaces:**
- Consumes: `BillingReader` (Task 3); `KrakenAPI.async_graphql_query(query, request_context)`, which already returns `None` on any GraphQL `errors`.
- Produces: the same `predbat.billing` entity, `app="kraken"`, with `supplier = self.provider`.

- [ ] **Step 1: Write the failing tests:**
  - `test_kraken_run_publishes_billing`: with `current_tariff` and `import_mpan` set, mocked GraphQL returns `EON_IMPORT_DAY` (or a synthetic single-bucket day if Task 1 had no E.ON account). Assert `predbat.billing` is published with one bucket label per slot and `supplier == "eon"`.
  - `test_kraken_export_uses_export_account`: with `export_account_id="A-EXP"` and `export_mpan="X"`, the export query contains `accountNumber: "A-EXP"` and `marketSupplyPointId: "X"`.
  - `test_kraken_no_mpan_skips`: with `import_mpan=None`, no measurements query is sent and nothing is published.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Run `impact` on `KrakenAPI.run`. Storage is `("kraken", f"billing_{self.account_id}")`. `query_fn` is `self.async_graphql_query` directly. The billing block goes after the rates block and does not touch the existing `save_kraken_cache` condition.
- [ ] **Step 4: Run** `./run_all --test billing --test kraken > /tmp/kr.txt 2>&1`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "feat(kraken): publish half-hourly billing as predbat.billing"`

---

### Task 5: Attach billing to History-view plan JSON

**Files:**
- Modify: `apps/predbat/billing.py`
- Test: `apps/predbat/tests/test_billing.py`

**Interfaces:**
- Consumes: the `predbat.billing` attribute shape (Task 2).
- Produces: `attach_billing(plan_json: dict, billing: dict, view_midnight_utc: datetime, plan_interval_minutes: int, predbat_slot_cost: dict[int, float], standing_pence: float, events: dict[int, str], import_only: bool, predbat_import_kwh: float | None, predbat_export_kwh: float | None) -> dict`. It mutates and returns `plan_json`. `predbat_slot_cost` maps each row's `slot_minute` to Predbat's cost for that row in pence: net, or import-only when `import_only`. `events` maps a minute (relative to `view_midnight_utc`) to an event tag. For each row it adds:
  - `billed` (major units, dp2) or `None`;
  - `billed_buckets` (`{label: {"kwh", "cost"}}`, cost in major units) or `None`;
  - `billing_diff` (major units, dp2) or `None`;
  - `billing_event` (a display string) or `None`.

  It also adds `plan_json["billing_summary"]` = `{"predbat_cost", "billed_cost", "diff", "standing_predbat", "standing_billed", "import_kwh": [predbat, meter], "export_kwh": [predbat, meter], "complete_until", "import_only": bool, "supplier"}`. Every cost is in major units, and the key is left out entirely when the view has no billed rows.

- [ ] **Step 1: Write the failing tests:**
  - `test_attach_basic`: rows at 30-minute slots where billing equals Predbat's cost. Assert `billing_diff == 0.0` on every row, and that the summary `diff == 0.0`.
  - `test_attach_unpublished_row`: a row with no billing slot has `billed is None` and `billing_diff is None`, and the summary's `predbat_cost` leaves that row out.
  - `test_attach_hour_rows`: (Review Focus 3) with `plan_interval_minutes=60`, a row's `billed` is the sum of two slots. A row with only one of its two slots published gets `billed is None`.
  - `test_attach_standing_stripped`: (Review Focus 4) the row at `slot_minute` 0 has `predbat_slot_cost[0] = standing_pence + 10`, and its billing is 10p energy. Assert `billing_diff == 0.0` and `standing_predbat == standing_pence / 100`.
  - `test_attach_clock_change`: (Review Focus 1) for 25 Oct 2026 with `view_midnight_utc` = 24 Oct 23:00Z, the rows map by UTC instant, and the row at `slot_minute` 120 picks up the billing slot starting `2026-10-25T01:00:00Z`.
  - `test_attach_event_label`: `events={600: "saving"}` puts `billing_event == "Saving session"` on the row covering minute 600. Tags `"manual"`/`"user"` map to `"Manual rate"`, `"increment"` to `"Rate adjustment"`, and any other tag gives `None`.
  - `test_attach_import_only`: `import_only=True` uses only the import billing, and the summary has `import_only is True`.
  - `test_attach_no_billing`: an empty `billing` leaves `plan_json` without `billing_summary` and its rows without the new keys.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement `attach_billing`.** A row covers `[view_midnight_utc + slot_minute, + plan_interval_minutes)`. It is billed only if every 30-minute billing slot inside it is present. Billed net is the import cost minus the export cost (or import only, when `import_only`). Standing is removed from Predbat's side for any row whose `slot_minute % 1440 == 0`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(billing): attach billed cost, diff and day summary to History plan JSON"`

---

### Task 6: Extract the History replay from `calculate_yesterday()` (no behaviour change)

**Files:**
- Modify: `apps/predbat/output.py`: `calculate_yesterday()` (lines ~3423-3990); the "Now try to show what really happened yesterday" block (~3683-3832) moves into a new method
- Test: `apps/predbat/tests/test_history_days.py` (runner `run_history_days_tests(my_predbat) -> bool`, registered as `("history_days", …, "History view replay and 7-day history", False)`)

**Interfaces:**
- Produces: `Output.replay_history_day(self, end_record: int, pv_step: dict, load_step: dict, soc_array: dict, cost_array: dict, status_data: list | None, rates_import: dict, rates_export: dict) -> dict`. It returns the plan JSON for the replay, and saves and restores every attribute it fakes, exactly as the current code does.

- [ ] **Step 1: Run `impact` upstream** on `calculate_yesterday` and `publish_html_plan`, and report the blast radius to the user. Expect HIGH: it feeds the savings sensors. Warn and get the go-ahead before editing.
- [ ] **Step 2: Write the guard test** `test_replay_matches_calculate_yesterday`. Using `test_calculate_yesterday.py`'s helpers (`_make_constant_history`, `_make_mock_step_data`), run `calculate_yesterday()` and capture the `json` attribute of `predbat.cost_yesterday`. Then call `replay_history_day` with the same inputs, and assert the `rows` are equal apart from `timestamp`.
- [ ] **Step 3: Run** it. Expected: FAIL (method missing).
- [ ] **Step 4: Extract** the block into `replay_history_day`, and have `calculate_yesterday()` call it with its existing arrays.
- [ ] **Step 5: Run** `./run_all --test history_days --test calculate_yesterday --test savings_stability > /tmp/hd.txt 2>&1`. Expected: all pass, and `calculate_yesterday` / `savings_stability` behave as before.
- [ ] **Step 6: Commit** `git commit -m "refactor(output): extract the History view replay into replay_history_day()"`

---

### Task 7: Older days and billing on the History view

**Files:**
- Modify: `apps/predbat/output.py`: add `calculate_history_days()`; call `attach_billing` on yesterday's JSON inside `calculate_yesterday()`
- Modify: the caller of `calculate_yesterday()` (find it with `context({name: "calculate_yesterday"})`), to call `calculate_history_days()` straight after it
- Test: `apps/predbat/tests/test_history_days.py`

**Interfaces:**
- Consumes: `replay_history_day` (Task 6), `attach_billing` (Task 5), and the `predbat.billing` attributes (read with `get_state_wrapper(self.prefix + ".billing", attribute=...)`).
- Produces:
  - `Output.calculate_history_days(self) -> None`. It publishes `self.prefix + ".plan_history"` with state = the number of days and attribute `days` = `{"YYYY-MM-DD": plan_json}` for days 2..7 back (local dates).
  - `self.history_days_built_for: date | None`, which gates the rebuild to once per local date. It also rebuilds when the billing sensor's `complete_until` has moved on since the last build, so late-arriving billing appears the same day.
  - The yesterday JSON (`predbat.cost_yesterday` `json`) now carries the Task 5 billing keys.

- [ ] **Step 1: Write the failing tests:**
  - `test_history_days_built_once_per_day`: two calls on the same date with unchanged billing build once (count via a spy on `replay_history_day`); a third call after `now` moves past midnight builds again.
  - `test_history_days_rebuild_on_new_billing`: a changed `complete_until` triggers a rebuild on the same date.
  - `test_history_days_limited_by_history`: with `max_days_previous = 3`, only days 2 and 3 back are published, and days HA history can't reach are left out.
  - `test_history_days_rates_from_history`: older-day rows take `import_rate` from the mocked `predbat.rates` history. Where that history is missing for a minute, the row's `import_rate` is `None`, and the JS shows "—" (Task 8).
  - `test_yesterday_json_has_billing`: with a `predbat.billing` state set, `predbat.cost_yesterday`'s `json` has `billing_summary`. A saving-session tag at yesterday minute 600 (in `self.rate_import_replicated` at key `600 - 1440`) appears as `billing_event` on that row.
  - `test_import_only_when_export_leg_missing`: billing `legs={"import": True, "export": False}` with an export tariff (`rate_export` non-zero) makes `billing_summary.import_only` True, with Predbat's side taken from `cost_today_import` history.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement `calculate_history_days`.** Read `cost_today`, `cost_today_import`, `soc_kw_h0`, `status`, `rates` and `rates_export` history for `min(8, max_days_previous + 1)` days in one `get_history_wrapper` call each. Build each day's arrays with the same `minute_data(..., backwards=True)` indexing `calculate_yesterday()` uses, offset by whole days. Use `step_data_history` with `base_offset = days_back * 24 * 60 + self.minutes_now` for load and PV. Pass event tags only where `rate_import_replicated` reaches; older days have none. Take `predbat_import_kwh` / `predbat_export_kwh` for each day from the day's change in `self.import_today` / `self.export_today` (incrementing minute data, which reaches back `max_days_previous` days), over the billed slots only.
- [ ] **Step 4: Run** `./run_all --test history_days --test calculate_yesterday --test billing > /tmp/hd.txt 2>&1`. Expected: all pass.
- [ ] **Step 5: Commit** `git commit -m "feat(output): 7-day History replay with billing attached"`

---

### Task 8: Web UI: day picker, columns, summary strip

**Files:**
- Modify: `apps/predbat/web.py`: `html_api_plan_data` (~2577-2665) adds `"history_days": sorted(list of dates)`; new route `app.router.add_get("/api/plan_history", self.html_api_plan_history)`
- Modify: `apps/predbat/web_helper.py`: `renderPlanTable` (~7033), `switchView`/`refreshPlan` (~7810-7890), the History button area (`web.py` ~2686)
- Test: `apps/predbat/tests/test_billing_client_js.py` (runner `test_billing_client_js(my_predbat) -> bool`, registered `("billing_client_js", …, "History billing columns/picker JS structure", False)`); a web handler test in `tests/test_history_days.py`

**Interfaces:**
- Consumes: the plan JSON keys from Task 5, and `predbat.plan_history` from Task 7.
- Produces: `async html_api_plan_history(self, request)`. `GET /api/plan_history?date=YYYY-MM-DD` returns that day's plan JSON, or 404 `{"error": "no history for date"}`.

- [ ] **Step 1: Write the failing tests:**
  - Handler: `test_api_plan_history_returns_day` (200 with `rows`) and `test_api_plan_history_unknown_date` (404).
  - JS structure (assert on the `get_plan_renderer_js()` text, as `test_debug_history_client_js.py` does):
    - `renderPlanTable` adds `th('billed', 'Billed')` and `th('billing_diff', 'Diff')` only when `jsonData.rows.some(r => r.billed !== undefined)`;
    - the Billed cell sets a `title` built from `billed_buckets`;
    - the Diff cell's colour logic uses red for `> 0` and green for `< 0`;
    - `null` renders as `—`;
    - a `renderBillingSummary(` function exists, reads `billing_summary`, and writes "billing complete to";
    - `billing_event` is rendered in the row;
    - the History picker fetches `./api/plan_history?date=` only when an older day is chosen (no fetch on the default yesterday+today view);
    - `COLUMN_HEADER_HELP` has `billed` and `billing_diff` entries;
    - the merged-blocks view sums `billed` / `billing_diff` across the merged rows, and shows `—` if any merged row is `null`.
- [ ] **Step 2: Run** `./run_all --test billing_client_js --test history_days > /tmp/web.txt 2>&1`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The picker is a row of buttons under the History button, shown only when `currentView === 'yesterday'`: one per `history_days` date (oldest first), then "Yesterday + today", the default.
  - The summary strip shows `predbat_cost`, `billed_cost`, `diff` (coloured), import and export kWh pairs, the standing charge (Predbat vs billed), "billing complete to HH:MM" in local time, and "import only" when set.
  - Hide every billing element when `billing_summary` is absent.
  - Honour the existing dark-mode colour handling (`updateTableColors`).
- [ ] **Step 4: Run** the tests, plus `./run_all --test debug_history_client_js > /tmp/web2.txt 2>&1` (no regression).
- [ ] **Step 5: Check it in the browser.** Run Predbat with the debug-file replay or the test harness, open `/plan`, click History, and confirm the columns, strip and picker against mockup C (`.superpowers/brainstorm/*/content/history-billing.html`).
- [ ] **Step 6: Commit** `git commit -m "feat(web): billing columns, summary strip and 7-day picker on the History view"`

---

### Task 9: Docs, full checks, PR

**Files:**
- Modify: the docs page that covers the Plan page/History view, and the Octopus and Kraken component pages in `docs/`: describe `predbat.billing`, the History additions, the 1–2 day lag, the import-only label, and that Octopus users who only use the HA Octopus integration (no `octopus_api_key` in Predbat) don't get billing
- Modify: `.cspell/custom-dictionary-workspace.txt` as needed

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run** `cd coverage && ./run_all --quick > /tmp/quick.txt 2>&1; grep -n "FAIL\|ERROR\|passed\|failed" /tmp/quick.txt`. Expected: no failures.
- [ ] **Step 3: Run** `./run_pre_commit > /tmp/pc.txt 2>&1` and grep for `Failed`. Re-stage the auto-sorted dictionary, then re-run until clean.
- [ ] **Step 4: Run** `detect_changes({scope: "compare", base_ref: "main"})`. Expected: changes limited to `billing.py`, `OctopusAPI.run`/`initialize`, `KrakenAPI.run`/`initialize`, `calculate_yesterday`/`replay_history_day`/`calculate_history_days`, the web handlers and the plan renderer JS.
- [ ] **Step 5: Commit** the docs, push the branch, and open the PR (ask the user before pushing).
