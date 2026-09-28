# Kraken: Free Sessions, Gas Rates and EV Controls Implementation Plan

**Goal:** Close the gaps between Predbat's native Kraken component (`apps/predbat/kraken.py`) and the community EDF Home Assistant integration ([stevekirtley/HomeAssistant-EDFEnergy](https://github.com/stevekirtley/HomeAssistant-EDFEnergy)) so an EDF (or E.ON Next) customer can run Predbat without the second integration. Three independent features, one PR each:

1. **Free electricity sessions** — EDF Sunday Saver (API) and Power Perks (no API) fed into the plan as 0p import slots.
2. **Gas rates** — gas tariff discovery, unit rates and standing charge, wired to `metric_octopus_gas`.
3. **EV smart-charging controls** — read and write the SmartFlex target time / target SoC, smart-charge suspend and bump charge, wired to `octopus_ready_time` / `octopus_charge_limit`.

**Approach:** Mirror what `OctopusAPI` (`apps/predbat/octopus.py`) already does for Octopus wherever possible — same sensor shapes, same `set_arg` wiring, same command queue — so `fetch.py` / `octopus.py` consumers need no changes.

**Reference:** Every API detail below was taken from the EDF integration's source (`custom_components/edf_energy/api_client/__init__.py` and `coordinators/`) and must be re-verified against a live account using the component's CLI (`python3 kraken.py ...`) before merging, because unit tests only exercise mocks.

## Global constraints

- Follow `CLAUDE.md`: run GitNexus `impact` on every symbol before editing it and report the blast radius; run `detect_changes` before committing.
- Line length 256 (Black) / 250 (Flake8); 100% docstrings (`interrogate`); British English (CSpell); `lower_case_with_underscores`.
- Unit tests for all new code go in `apps/predbat/tests/test_kraken.py` and must be called from `run_kraken_tests()`. Run from `coverage/`: `./run_all --test kraken > /tmp/kraken.log 2>&1` then grep the file.
- Persistence goes through the existing `load_kraken_cache` / `save_kraken_cache` (Storage component), never direct file access.
- Each new data source gets an age-based refresh constant alongside `KRAKEN_TARIFF_REFRESH_MINUTES` etc. and follows the existing `run()` pattern: fetch when due, publish on `first` or when refreshed, save cache only when something was refreshed.
- A failed fetch keeps the last known data (never blank a sensor on a transient error).
- Update `docs/components.md` (Kraken section: behaviour, config options, published entities) in each PR.

## Current state (baseline)

| Area | Kraken today | Octopus equivalent |
| ---- | ------------ | ------------------ |
| Import / export rates, standing charge | Yes (`async_fetch_rates`, GraphQL fallback) | Yes |
| SmartFlex dispatches | Yes — `binary_sensor..._intelligent_dispatch[_N]`, wires `octopus_intelligent_slot` | Yes |
| Free sessions | **No** | `sensor..._free_electricity` → `octopus_free_electricity` (`octopus.py` ~1380-1412) |
| Gas | **No** — `KRAKEN_ACCOUNT_QUERY` only asks for `electricityMeterPoints` | `tariffs["gas"]` → `metric_octopus_gas` via `automatic_config()` |
| EV target time / SoC | **No** — dispatches are read-only; `octopus_ready_time` / `octopus_charge_limit` unwired | `select..._intelligent_target_time[_N]`, `number..._intelligent_target_soc[_N]`, command queue (`select_event`, `number_event`, `process_commands`) |
| Respect `car_slot_owner` (e.g. Ohme) | **No** — `_publish_dispatch_sensors()` always sets `octopus_intelligent_slot` | Yes (`automatic_config()`) |

Events for `predbat_kraken_*` entities are already routed to the component by `event_filter` in `components.py`; `ComponentBase.select_event` / `number_event` / `switch_event` are currently no-ops for Kraken.

---

## Phase 1 — Free electricity sessions

### 1.1 Consumer side (no change needed)

`OctopusAPI`'s reader in `octopus.py` (`fetch_octopus_sessions`, the `octopus_free_electricity` branch ~line 3972) reads the `events` attribute of whatever entity `octopus_free_electricity` points at and turns each `{start, end}` into a 0p slot (`load_free_slot`). `input_number.predbat_load_scaling_free` already applies. Kraken only has to publish a sensor in that shape and `set_arg` it.

### 1.2 Sunday Saver (EDF only)

API (from `api_client.async_get_sunday_saver` and `coordinators/sunday_saver.py`):

- `POST https://www.edfenergy.com/support/sunday-saver/api/weekly`
- JSON body: `{"accountNumber": "<A-...>", "WEEK_START_DATE": "<YYYY-MM-DD>"}` where the date is **Monday of the previous week** (EDF's anchor to get the coming Sunday).
- Header `Authorization: <kraken JWT>` — the raw token, **without** the `JWT ` prefix used for GraphQL.
- Response `{"data": ...}` where `data` may be a dict, a JSON string, or a double-encoded JSON string; `"{}"` means no event this week.
- Fields: `FREE_HOURS` (float, 0 = none), `FREE_HOURS_START_DATETIME`, `FREE_HOURS_END_DATETIME`. Times are **UK local time mislabelled as UTC** — strip the tz and attach `Europe/London`. The end time is the start of the last half hour, so add 30 minutes.
- Enrolment: `GET https://www.edfenergy.com/support/energyhub/api/sunday-saver/v1/accounts...` (confirm exact path in `async_get_sunday_saver_enrollment_status`). Useful for a status attribute and a log hint ("not enrolled — no Sunday Saver sessions will be seen"); not required for planning.

Tasks:

- [ ] Add `KRAKEN_SUNDAY_SAVER_URL`, `KRAKEN_FREE_SESSION_REFRESH_MINUTES` (e.g. 60) constants; only active when `self.provider == "edf"`.
- [ ] Add a helper to get the current bearer token for non-GraphQL calls (both `KrakenAuthMixin` and SaaS `OAuthMixin` paths). **Check the SaaS OAuth token is accepted by this endpoint**; if not, skip Sunday Saver in OAuth mode with a single log line.
- [ ] `async_fetch_sunday_saver()` — POST, tolerant decode (dict / string / double-encoded), UK-local time fix, +30 min end. Returns a list of `{start, end, code, source: "sunday_saver"}` (code e.g. `sunday_saver_YYYYMMDDHHMM`) or `None` on failure.
- [ ] Keep sessions in `self.free_sessions` (list), merged by `code`, pruned once `end` is more than ~2 days old; cache in `save_kraken_cache` / `load_kraken_cache` with `free_sessions_fetched_at`.

### 1.3 Power Perks (EDF only — no API)

EDF announce Power Perks by SMS only; they are not in Kraken GraphQL, the EDF website APIs or the app. The EDF integration uses a relay run by its author (`https://apirelay.sitetest.org.uk/power_perks.php?action=sessions`, identified by the integration's User-Agent) that parses forwarded texts, plus a manual registration action.

Tasks:

- [ ] **Manual entry (always available):** accept a user-supplied list via `apps.yaml` key `kraken_free_sessions` (list of `{start, end}` in local time), validated in `APPS_SCHEMA` (`config.py`). Merge into `self.free_sessions` with `source: "manual"`.
- [ ] **Relay feed (opt-in, default off):** new component arg `power_perks_url` / config key `kraken_power_perks_url`. When set, `GET` it, parse `{"sessions": [{start, end, code?}]}` (bare timestamps are UK local), skip malformed entries, keep last known sessions if the feed fails. **Do not ship the relay URL as a default** without the relay owner's agreement (see Open questions).

### 1.4 Publish and wire

- [ ] `_publish_free_session_sensor()` — `sensor.predbat_kraken_<acct>_free_electricity`, state `on` if a session is active now else `off`, attributes `{"friendly_name": "Kraken Free Electricity Sessions", "icon": "mdi:flash", "events": [{start, end, code, rate: 0, source}], "sunday_saver_enrolled": ...}` — the same shape as Octopus's sensor.
- [ ] `set_arg("octopus_free_electricity", <that sensor>)` once on first publish (EDF only). Note: a user who also points `octopus_free_session` at the EDF integration's event entity will get duplicate 0p slots, which is harmless.

### 1.5 Tests

- [ ] Sunday Saver: dict / JSON-string / double-encoded / `"{}"` / `FREE_HOURS: 0` responses; BST and GMT dates give correct UTC; +30 min end; `None` keeps cached sessions; not called for `provider="eon"`.
- [ ] Power Perks feed: valid, malformed entry skipped, non-feed payload ignored, HTTP error keeps last sessions, disabled when no URL.
- [ ] Manual sessions parsed and merged; pruning of old sessions; merge by code.
- [ ] Sensor shape and `octopus_free_electricity` wiring; end-to-end: a published session makes `fetch_octopus_sessions()` return a 0p slot.
- [ ] Cache round-trip includes `free_sessions`.

### 1.6 Out of scope

Football "Extra Time" free electricity (relay-only, sporadic), Flextras / Power Perks / Sunday Saver sign-up, Weekend Saver challenges.

---

## Phase 2 — Gas rates

Predbat uses gas rates only for the iBoost / gas-vs-electric hot water decision and the rates chart (`fetch.py` ~1148 reads `metric_octopus_gas` via `fetch_octopus_rates`). There is no consumer for the gas standing charge, so publish it for information only. Applies to both EDF and E.ON Next.

Tasks:

- [ ] **Confirm the schema first:** run the CLI with `--introspect-type` on `PropertyType` / `GasMeterPointType` against a live EDF account. The EDF integration uses `account { gasAgreements(active: true) { meterPoint { mprn agreements { validFrom validTo tariff { tariffCode productCode } } } } }`; Kraken's own query uses `properties { electricityMeterPoints { ... } }`, so the likely addition is `properties { gasMeterPoints { mprn agreements { ... } } }`.
- [ ] Extend `KRAKEN_ACCOUNT_QUERY` with the gas meter points. Run `impact` on `_find_active_tariff`, `_discover_export_tariff` and `async_discover_all_accounts` first — all three consume this query.
- [ ] In `async_find_tariffs()` set `self.gas_tariff = {"product_code", "tariff_code", "mprn"}` (or `None`) using `_find_active_tariff`-style active-agreement selection.
- [ ] Generalise `build_rates_url` / `build_standing_charge_url` with a `fuel` argument (`electricity-tariffs` vs `gas-tariffs`); GraphQL fallback (`applicableRates` / `applicableStandingCharges`) passes `mpxn=<mprn>` — verify it works for gas.
- [ ] In `run()` fetch gas rates + standing charge on the existing rates cycle when `self.gas_tariff` is set; cache as `gas_rates`, `gas_standing_charge`.
- [ ] Publish `sensor.predbat_kraken_<acct>_gas_rates` (same shape as `import_rates`) and `sensor..._gas_standing`.
- [ ] `set_arg("metric_octopus_gas", ...)` only once gas rates are actually available (same guard as `export_wired` / `export_rates_available`), and never overwrite a user's own `metric_octopus_gas` / `rates_gas` — check `self.args` first.

Tests:

- [ ] Account with and without gas; gas tariff selection by date; gas REST URLs; GraphQL fallback for gas; sensor shape; wiring only when rates present; user's own `rates_gas` not overwritten; cache round-trip.

---

## Phase 3 — EV smart-charging controls

Applies to both providers (SmartFlex is Kraken-wide). The GraphQL below matches the EDF integration and Octopus's existing mutations in `octopus.py`; reuse the Octopus strings/helpers where they are byte-identical rather than duplicating them.

### 3.1 Read preferences

```graphql
devices(accountNumber: "<acct>", deviceId: "<id>") {
  id
  status { isSuspended }
  preferences { targetType unit mode schedules { dayOfWeek time min max } }
}
```

- [ ] Add `KRAKEN_DEVICE_SETTINGS_QUERY`; fetch per device on the dispatch cycle (or a slower settings cycle, e.g. 10 minutes).
- [ ] Store `suspended`, `weekday_target_time`, `weekday_target_soc`, `weekend_target_time`, `weekend_target_soc`, `minimum_soc`, `maximum_soc` on each `self.intelligent_devices[device_id]` (the same keys as Octopus's `INTELLIGENT_DEVICE_SETTING_KEYS`). A failed settings query carries the previous values forward.

### 3.2 Publish controls

Per device (index suffix from `_device_index_suffix`):

- [ ] `select.predbat_kraken_<acct>_intelligent_target_time[_N]` — options from the schedule range the API accepts (Octopus uses `OPTIONS_TIME` 04:00-10:30; confirm EDF's range).
- [ ] `number.predbat_kraken_<acct>_intelligent_target_soc[_N]` — min/max from `minimum_soc` / `maximum_soc`.
- [ ] `switch.predbat_kraken_<acct>_intelligent_smart_charge[_N]` — on = unsuspended.
- [ ] `switch.predbat_kraken_<acct>_intelligent_bump_charge[_N]` — on = boost requested.

### 3.3 Wire into Predbat

- [ ] Extend `_publish_dispatch_sensors()` (run `impact` first) to also `set_arg("octopus_ready_time", [...])` and `set_arg("octopus_charge_limit", [...])`, in the same sorted device order as `octopus_intelligent_slot`.
- [ ] Exclude suspended devices from the slot, ready-time and limit lists and from the `num_cars` bump, as `OctopusAPI.automatic_config()` does.
- [ ] Respect `self.base.car_slot_owner`: if another component (e.g. Ohme) owns the car slots, leave all three args alone. This also fixes the existing gap where Kraken always overwrites `octopus_intelligent_slot`.
- [ ] Sort devices by id so a car always lands in the same slot (per-car settings are keyed by index).

### 3.4 Write path

- [ ] Implement `select_event`, `number_event`, `switch_event` on `KrakenAPI`: map entity → suffix → device id, validate, append to `self.commands` (as `OctopusAPI` does).
- [ ] `process_commands()` called at the top of `run()`:
  - target time / SoC → `setDevicePreferences(input: {deviceId, mode: CHARGE, unit: PERCENTAGE, schedules: [7 × {dayOfWeek, time: "HH:MM", max: "<soc>"}]})`, filling the missing value from cached settings.
  - smart charge → `updateDeviceSmartControl(input: {deviceId, action: SUSPEND | UNSUSPEND})`.
  - bump charge → `updateBoostCharge(input: {deviceId, action: BOOST | CANCEL})`.
- [ ] On success update the cached settings immediately, re-publish the entity, and set `dispatch_fetched_at = None` so dispatches refresh on the next cycle (a bump or new target changes the plan).
- [ ] On failure log a warning and re-publish the previous value so the HA entity snaps back.
- [ ] Decide whether `set_read_only` should block these writes (Octopus does not; see Open questions).
- [ ] `async_graphql_query` currently expects a data payload; check mutations return `{"data": {...}}` and handle `errors` without retrying non-auth failures.

### 3.5 Tests

- [ ] Settings parse (weekday/weekend, missing preferences, failed query keeps old values).
- [ ] Entity publishing per device, suffixes for one and two devices.
- [ ] Wiring of all three args; suspended device excluded; `car_slot_owner` respected; `num_cars` bump.
- [ ] Each event → command → mutation payload (assert the GraphQL string), cache update, dispatch refresh forced; invalid SoC rejected; unknown suffix ignored; mutation failure reverts entity.

### 3.6 Live verification

- [ ] Extend the CLI (`test_kraken_api` / `main`) with `--preferences` (print device settings) and a guarded `--set-target HH:MM SOC` to confirm the mutation on a real EDF GoElectric / SmartFlex account.

---

## Documentation

- [ ] `docs/components.md` Kraken section: free sessions (Sunday Saver automatic for EDF, Power Perks manual/opt-in feed), gas, EV controls, new config keys, new entities table rows.
- [ ] `docs/energy-rates.md`: note under free sessions that EDF customers get them from the Kraken component; how to add a Power Perks session by hand.
- [ ] `docs/car-charging.md`: Kraken SmartFlex target time / SoC / bump / suspend controls.
- [ ] `tools/debug-journal.md`: Sunday Saver quirks (raw token header, UK time labelled UTC, double-encoded JSON, previous-Monday anchor).

## Suggested order

1. Phase 1 (free sessions) — highest planning value; Sunday Saver first, Power Perks manual entry second, relay last.
2. Phase 3 (EV controls) — fixes the `car_slot_owner` gap and gives Predbat the ready time / charge limit it plans against.
3. Phase 2 (gas) — lowest value, only matters for iBoost / gas hot water users.

Each phase is independent and ships as its own PR.

## Open questions

1. **Power Perks relay:** the feed is run by the EDF integration's author and is served to that integration by User-Agent. Ask before pointing Predbat at it; otherwise ship manual entry only and leave the URL as user-supplied config.
2. **SaaS OAuth tokens:** does the Sunday Saver website endpoint accept the OAuth access token Predbat.com uses, or only tokens minted by email/password or API key?
3. **E.ON Next:** does E.ON have an equivalent free-session scheme with an API? If so, add it to Phase 1 behind the same sensor.
4. **Read-only mode:** should `set_read_only` block EV control writes? Octopus does not today.
5. **Target time range:** which times does EDF's `setDevicePreferences` accept (Octopus limits 04:00-11:00)?
