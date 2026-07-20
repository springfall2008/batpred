# DEYE Cloud Inverter Integration — Design

Date: 2026-07-19
Status: Approved (design review complete)

## Goal

Add a DEYE Cloud component to Predbat, modelled on the existing Fox and Enphase
cloud integrations (`apps/predbat/fox.py`, `apps/predbat/enphase.py`), providing
full monitoring and battery control for DEYE (Sunsynk-family) hybrid inverters
via the DeyeCloud OpenAPI, so that a DEYE inverter can be used as a
Predbat-controlled inverter with no local hardware access, on both the
self-hosted Home Assistant add-on and the Predbat.com SaaS platform.

This supersedes and replaces PR #3917 (`feat/component-value-injection`), which
was a monitoring stub whose control model (standalone `charge()`/`discharge()`
methods that nothing in Predbat calls, and structured `batteryPower.soc`
parsing that does not match the real API) is architecturally incompatible with
how Predbat drives inverters. That PR will be closed. The unrelated
`components.py` "value injection" change it bundled is a separate concern and,
if wanted, lands as its own PR.

## Scope decisions (agreed)

- **Full control in one go** — monitoring and battery control ship together,
  to full Fox/Enphase parity.
- **Both deployment modes** — self-hosted HA add-on (Predbat self-manages the
  DEYE token from developer app credentials) *and* Predbat.com SaaS (token
  injected by the platform). A `deye_auth_method` arg switches between them,
  mirroring Fox's `api_key` / `oauth`.
- **Multi-inverter from the start** — discover all battery inverters in the
  account's station(s) and register each as a Predbat inverter
  (`num_inverters`), like Fox.
- **No Predbat-facing work-mode control** — Predbat only owns the charge window
  and the export window (plus reserve, rates, target SOCs, enables). The
  component derives the DEYE work mode internally from the desired behaviour
  (charge / hold / freeze charge / export / freeze export / self-use). This is
  the Enphase/Tesla mode-less pattern, not the Fox work-mode-select pattern.
- **Battery + solar only** — no EV charger, smart-load, grid peak-shaving,
  micro-storage modes, or station-management (create/alerts) control.
- **Write strategy: combined `strategy_dynamic_control`** — one atomic
  read + diff + write per cycle, with change detection; fall back to granular
  endpoints per-control only if hardware testing forces it.

## Background: the DeyeCloud OpenAPI

Two sources were used. Both were scraped during design:

1. Reference HA integration
   <https://github.com/heavenknows1978/hass-deyecloud> — confirms auth, station
   / device discovery, and the real-time data shape. It implements **only**
   monitoring plus a Solar-Sell on/off toggle; it does **not** implement any
   battery scheduling, so it does not cover the control surface Predbat needs.
2. Official developer docs <https://developer.deyecloud.com/api> and the
   bundled "Deye Open MCP" tool catalogue
   <https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html>,
   which enumerates all 39 OpenAPI capabilities including the control and TOU
   endpoints Predbat requires.

### Base URLs / data centres

- EU: `https://eu1-developer.deyecloud.com/v1.0`
- US/AM: `https://us1-developer.deyecloud.com/v1.0`
- India: `https://india-developer.deyecloud.com/v1.0`

Selected by a `deye_data_center` arg (`eu` / `am` / `india`).

### Auth

`POST /account/token?appId={appId}` with JSON body
`{appSecret, email|username, password: sha256(pw) [, companyId]}` →
`{success, accessToken, refreshToken, expiresIn, scope, tokenType, uid, msg}`.
Bearer token thereafter. `companyId` is only needed for installer/business
accounts. `password` is sent as a lower-case hex SHA-256 digest. Login key is
`email` when the value contains `@`, else `username`.

### Reads

- `POST /station/list` (paged) → `stationList[]`.
- `POST /station/device` (paged `{page, size, stationIds}`) → `deviceListItems[]`;
  filter `deviceType == "INVERTER"`, key `deviceSn`.
- `POST /device/latest` → `deviceDataList[]`, each
  `{deviceSn, deviceType, deviceState, collectionTime, dataList:[{key,value,unit}]}`.
  **Real-time telemetry is a flat key/value/unit list with dynamic keys**
  (units W / V / A / % / °C / Hz / kWh), not a nested object. `batterySOC`,
  `chargePower`, `dischargePower` appear as keys; the exact key spelling for
  each metric is confirmed at the spike from a live `/device/latest` response
  and/or `/device/measure_points`.
- `POST /device/measure_points` → measure-point metadata (key → name/unit).
- `POST /station/history`, `/device/history`, `/device/history/raw` → energy
  totals and history curves.
- `POST /config/battery` → `battCapacity`, `battLowCapacity`,
  `battShutDownCapacity`, `maxChargeCurrent`, `maxDischargeCurrent` (device
  capability limits).
- `POST /config/tou` → current TOU (`timeUseSettingItems[]`, `touAction`).

### Control (asynchronous — returns `orderId`, poll for completion)

- `POST /order/sys/tou/update` — writes **6 fixed sequential TOU intervals**
  (`{deviceSn, timeUseSettingItems:[TimeUseSettingItem × 6]}`). "Have to be
  passed in sequence."
- `POST /order/sys/tou/switch` — TOU on/off.
- `POST /order/sys/workMode/update` — work mode:
  `SELLING_FIRST` / `ZERO_EXPORT_TO_LOAD` / `ZERO_EXPORT_TO_CT`.
- `POST /order/battery/parameter/update` — `gridChargeAction` (on/off),
  `gridChargeAmpere`, `maxChargeCurrent`, `maxDischargeCurrent`.
- `POST /strategy/dynamicControl` (camelCase — confirmed in sample code) — **the
  combined control used by this design**: `{deviceSn, gridChargeAction,
  gridChargeAmpere, maxSellPower, maxSolarPower, solarSellAction,
  timeUseSettingItems[6], touAction, touDays[], workMode, zeroExportPower}`.
- `GET /order/{orderId}` — control command status; response carries
  `connectionStatus` (0 offline / 1 online), `success`.

Every control response is `{success, code, msg, orderId, collectionTime,
connectionStatus, requestId}`. **The write is not applied synchronously** — the
`orderId` must be polled via `get_order_result` until success/timeout.

> The exact per-slot `TimeUseSettingItem` field names (start time, SOC target,
> power, grid-charge flag, gen/PV-charge flag) were confirmed from the official
> sample code — see "Contract confirmed from official sample code" below —
> `{time, power, soc, enableGridCharge, enableGeneration}`.

## Architecture

### Component

`DeyeAPI(ComponentBase, OAuthMixin)` in `apps/predbat/deye.py`, structured like
`enphase.py` (leaner than `fox.py`). Registered in `components.py` under key
`deye` with `event_filter: "predbat_deye_"`, `phase: 1`.

`run(seconds, first)` loop (Fox-shaped, with age-based refresh tiers and a
midnight counter reset):

1. On `first`, restore cached device data from Storage.
2. Discover stations (`/station/list`) and battery inverters
   (`/station/device`), refreshed on a slow tier.
3. Poll `config_battery` (capabilities) on a slow tier; `device/latest`
   (telemetry) on a fast tier.
4. Read the schedule entities into local state; apply on write-button/diff
   (change detection is against the last-applied payload cache).
5. `publish_data()` (sensors) + `publish_schedule_settings_ha()` (control
   entities).
6. On `first and automatic`, `automatic_config()`.

### Files

| File | Change |
|---|---|
| `apps/predbat/deye.py` | **New** — the component |
| `apps/predbat/components.py` | Register `deye` (event filter `predbat_deye_`) |
| `apps/predbat/config.py` | `deye_*` CONFIG_ITEMS + APPS_SCHEMA; new `"DeyeCloud"` INVERTER_DEF |
| `apps/predbat/tests/test_deye_api.py`, `test_deye_oauth.py` | **New**; registered in `unit_test.py` `TEST_REGISTRY` |
| `.cspell/custom-dictionary-workspace.txt` | DEYE terms |
| `docs/inverter-setup.md`, `docs/components.md`, `docs/apps-yaml.md` | User docs |

No `inverter.py` change is expected: with the mode-less pattern Deye rides the
standard charge/export control path, exactly like `EnphaseCloud` / `TESLA`.

### Config items / args

`deye_app_id`, `deye_app_secret`, `deye_username`, `deye_password`,
`deye_data_center` (`eu`/`am`/`india`), `deye_company_id` (optional),
`deye_auth_method` (`app_credentials` self-managed vs `oauth` SaaS-injected),
`deye_token_expires_at`, `deye_token_hash` (SaaS refresh), `deye_inverter_sn`
(`string|string_list`), `deye_automatic`, `deye_automatic_ignore_pv`. APPS_SCHEMA
entries mirror the Fox block.

### `DeyeCloud` INVERTER_DEF (mode-less template, modelled on `EnphaseCloud`)

`has_ge_inverter_mode: False`, `has_fox_inverter_mode: False`,
`has_ge_eco_toggle: False` (no work-mode entity), `has_charge_enable_time: True`,
`has_discharge_enable_time: True`, `has_target_soc: True`, `has_reserve_soc: True`,
`support_charge_freeze: True`, `support_discharge_freeze: True`,
`target_soc_used_for_discharge: True`, `output_charge_control: "power"`,
`charge_time_entity_is_option: True`, `time_button_press: True`, `soc_units: "%"`.
`charge_time_format` and the freeze-support flags are confirmed against a real
inverter at the spike.

## Controls — HA entities published (per inverter)

Same entity set and naming scheme as Enphase/Fox so `inverter.py` drives them
generically. **No work-mode select.**

- **Sensors:** soc, battery power, grid power, pv power, load power, battery
  capacity, inverter capacity, battery rate max, reserve min, temperature,
  energy totals (from `device/latest` `dataList` + `config_battery`).
- **Schedule controls:** charge & export `start_time` / `end_time` (selects),
  `soc` / `power` / `reserve` (numbers), `enable` / `write` (switches).

`automatic_config()` discovers all battery inverters and sets
`inverter_type=["DeyeCloud", …]`, `num_inverters`, and the full arg → entity map
(`soc_percent`, `soc_max`, `battery_power`, `grid_power`, `pv_power`,
`load_power`, `reserve`, `charge_start_time`, `charge_end_time`, `charge_limit`,
`scheduled_charge_enable`, `charge_rate`, `discharge_start_time`,
`discharge_end_time`, `discharge_target_soc`, `scheduled_discharge_enable`,
`discharge_rate`, `battery_temperature`, `inverter_limit`, `battery_rate_max`,
`load_today`/`import_today`/`export_today`/`pv_today` where available). It does
**not** set `inverter_mode`.

## Behaviour: internal work-mode derivation

Predbat never sets a DEYE work mode. Each cycle the component reads the schedule
entities, decides the active behaviour for the imminent window, and picks the
work mode + flags + slot SOC itself. This matches the proven Enphase precedent
(reserve-based freeze charge; a 99% export target as the freeze-export
sentinel).

| Predbat intent | Signal from entities | DEYE realisation (component-internal) |
|---|---|---|
| **Charge** | charge enable, target SOC > current | `ZERO_EXPORT_TO_LOAD`, `gridChargeAction=on`, slot SOC = charge_limit, power = charge_rate |
| **Freeze charge** | a charge with target SOC = reserve | `gridChargeAction=on`, slot SOC = reserve → holds at reserve |
| **Hold charge** | charge amount ≤ current SOC, held with reserve | `gridChargeAction=off`, slot SOC = reserve → battery held, no grid charge |
| **Real export** | export enable, export SOC **< 99** | `SELLING_FIRST`, `solarSellAction=on`, `gridChargeAction=off`, slot SOC = export floor, power = discharge_rate |
| **Freeze export** | export enable, export SOC **= 99** | `SELLING_FIRST`, `solarSellAction=on`, hold battery at 99% → **solar-only export**, battery not drained |
| **Idle / self-use** | no active window | `ZERO_EXPORT_TO_LOAD`, `gridChargeAction=off`, slot SOC = reserve |

Two consequences:

1. **Reserve is written live every cycle** — the `number_event` for the reserve
   entity pushes it to DEYE immediately (its own path, independent of the
   schedule write button), because Predbat's freeze-charge relies on the reserve
   taking effect at once. This mirrors Enphase `set_reserve` and Fox.
2. **The 99% export sentinel is preserved end-to-end** so freeze-export maps to
   solar-only export and never a battery drain.

## Window → 6-slot mapping

DEYE has 6 fixed sequential TOU slots covering 24h; each slot applies from its
start time until the next slot's start. `compute_schedule()` (à la Fox's) takes
the charge window and export window from the entities, derives the boundary
times, and fills the 6 slots: a self-use baseline at reserve SOC, one charge
slot, one export slot, and self-use for the remainder. One charge + one export
window needs ≤ 6 slots comfortably; if boundaries exceed 6, the imminent windows
are kept (Predbat only programs near-term windows anyway). Slots are always
written as a full set of 6 in sequence, as the API requires.

## Write loop (Approach A)

Each cycle: build the desired combined state (work mode + grid-charge +
solar-sell + 6 TOU items + TOU on) → **diff against the last-applied cached
payload** (`schedules_are_equal`-style); if unchanged, no write. On a detected
diff or a schedule **write-button** press: one atomic `POST /strategy/dynamicControl`
call, cache the applied payload, capture `orderId`, poll `GET /order/{orderId}`
until success or timeout with exponential backoff. Pending/failed orders retry
on the next cycle. The reserve live-write path is separate and immediate.

## Auth (both modes, via OAuthMixin)

- **`app_credentials`** (HA add-on): `POST /account/token?appId=` with
  `appSecret` + login + `sha256(password)` (+ `companyId`); store `accessToken`
  / `refreshToken` / `expiresIn`; refresh before expiry. Whether refresh uses a
  dedicated refresh-token grant or a re-login with app credentials is confirmed
  at the spike (re-login always works as a fallback).
- **`oauth`** (SaaS): token injected by the platform; `handle_oauth_401()` on
  401/403 requests a refresh; this module never calls the token endpoint.

`_auth_headers()` sets `Authorization: Bearer {access_token}` and
`Content-Type: application/json`. The data-centre arg selects the base URL.

## Error handling / resilience

Fox-style throughout: 3-retry exponential backoff on transient errors;
`record_api_call()` telemetry; per-request `401/403 → refresh → retry`; Storage
caching of device list / detail / config with age-based refresh tiers; midnight
counter reset; cache-version self-heal after a code change to the cached shape.
Async control failures and API errors log and are retried on the next cycle;
they never crash the run loop. DEYE OpenAPI request quotas are respected by the
change-detection write suppression and the tiered read cadence.

## Testing

`test_deye_api.py`:

- `device/latest` `dataList` → sensor parsing (dynamic keys, unit inference).
- `publish_data()` / `publish_schedule_settings_ha()` entity creation.
- `automatic_config()` arg → entity map (multi-inverter).
- **The behaviour → work-mode derivation table** — one case per row (charge,
  freeze charge, hold charge, real export, freeze export at 99%, idle), asserting
  the chosen work mode, `gridChargeAction`, `solarSellAction`, and slot SOC.
- Reserve live-write path fires immediately on the reserve `number_event`.
- Window → 6-slot mapping (boundary derivation, full-6 output, overflow keeps
  imminent windows).
- Change-detection write suppression (no write when the last-applied payload matches).
- Async order polling (pending → success; failure → retry next cycle).

`test_deye_oauth.py`: both auth modes; `sha256` password digest and
`email` vs `username` selection; `401 → refresh → retry`; token expiry refresh.

Both registered in `unit_test.py` `TEST_REGISTRY`. Per project policy, unit
tests are added for all new code.

## Docs

Add DEYE to `docs/inverter-setup.md` (setup: developer app, App ID/Secret, data
centre, add-on vs SaaS), `docs/components.md` (component entry), and
`docs/apps-yaml.md` (the `deye_*` args).

## Contract confirmed from official sample code

The design's original spike items were resolved from the **official**
`DeyeCloudDevelopers/deye-openapi-client-sample-code` repo — **no live device is
needed to build**:

- **`TimeUseSettingItem` (6 slots):** `{time:"HH:MM", power:<W>, soc:<%>,
  enableGridCharge:bool, enableGeneration:bool}` (`commission/sys_tou_update.py`).
- **Combined control:** `POST /strategy/dynamicControl` (camelCase) with
  `{deviceSn, workMode, gridChargeAction, solarSellAction, maxSellPower,
  maxSolarPower, touAction, touDays[], timeUseSettingItems[6]}`
  (`strategy/dynamic_control_*.py`).
- **Mode realisation matches the behaviour table:** charge = `gridChargeAction:on`
  + `workMode:ZERO_EXPORT_TO_*` + high slot SOC (SELLING_FIRST stops charging);
  export = `solarSellAction:on` + `workMode:SELLING_FIRST` + low slot SOC;
  hold/idle = slot SOC set to the hold level ("battery ceases charging and
  discharging").
- **Reserve = slot SOC floor** — no dedicated reserve endpoint
  (`battery/parameter/update` only sets `MAX_CHARGE_CURRENT`/`MAX_DISCHARGE_CURRENT`).
- **`device/latest` request** `{deviceList:[sn]}` (≤10); **order poll**
  `GET /order/{orderId}`; **token** `POST /account/token?appId=` with
  `sha256(password)` (+ `companyId`).

Change detection diffs the freshly-computed payload against the **last-applied
cached payload** (no read endpoint dependency), rather than a read-back.

**Two items remain empirical** (safe defaults shipped, corrected on first live
connection):

- The exact `device/latest` `dataList[].key` strings for SOC / battery / grid
  / pv / load power, isolated in `deye_const.py:DEYE_TELEMETRY_KEYS`.
- The `config/battery` field names and units — esp. whether `battCapacity` is
  kWh (directly usable as `soc_max`) or Ah (needing a voltage scale), isolated
  in `deye_const.py:CONFIG_BATTERY_KEYS`.

## Out of scope (YAGNI)

EV charger, smart-load, grid peak-shaving, micro-storage work modes
(`GREEN_POWER_MODE` / `FULL_CHARGE_MODE` / `CUSTOMIZED_MODE`), station
create/alerts management, and the PR #3917 `components.py` value-injection
feature (separate PR if wanted).

## Rollout

New branch off `main`; `deye.py` written from the Enphase/Fox template; PR
#3917 closed with a pointer to this design. First milestone is an
implementation spike against a live DEYE inverter to resolve the open items,
then the full component, tests, and docs.
