# Enphase Cloud Integration — Design

Date: 2026-07-11
Status: Approved (design review complete)

## Goal

Add an Enphase cloud component to Predbat, modelled on the existing Fox cloud
integration (`apps/predbat/fox.py`), providing full monitoring and battery
control for Enphase IQ Battery (Encharge) systems via the Enphase Enlighten
cloud, so that an Enphase site can be used as a Predbat-controlled inverter
with no local hardware access.

Scope decisions (agreed):

- **Full control in one go** — monitoring and battery control ship together.
- **Password authentication only** in v1. The unofficial Enlighten API has no
  OAuth; the official OAuth developer API (api.enphaseenergy.com v4) is
  read-only, metered and unusable for control. Email-OTP MFA accounts are
  detected and rejected with a clear error; MFA support may be added later.
- **Battery + solar only** — no EV charger, heat pump, tariff, storm guard or
  grid on/off control.
- **Fox-style schedule entities** — Predbat's Inverter class drives published
  HA entities; all Enphase API knowledge stays inside `enphase.py`.

## Background: the Enphase Enlighten API

Reference implementation: <https://github.com/barneyonline/ha-enphase-energy>
(HA custom integration `enphase_ev`, MIT-compatible reference; bespoke aiohttp
client, no pypi dependency). Key facts verified from its source:

- Base URL `https://enlighten.enphaseenergy.com` (unofficial web-app API).
  Browser-mimicry headers (User-Agent, Referer, Origin,
  `X-Requested-With: XMLHttpRequest`) are mandatory; wrong headers yield
  HTTP 406 or an HTML login wall.
- **Auth chain**: `POST /login/login.json` (form body `user[email]`,
  `user[password]`) → Rails session cookie (`_enlighten_4_session`) and JWT
  cookie `enlighten_manager_token_production` → `GET /users/self/token` →
  e-auth JWT (sent as both `Authorization: Bearer` and `e-auth-token`).
  No refresh token: on 401, re-run the password login. MFA is signalled by
  `requires_mfa` / `login_otp_nonce`.
- **Reads**:
    - `/pv/settings/<site>/battery_status.json` — per-battery and site SOC
    (`current_charge` %), `available_energy` kWh, `max_capacity` kWh,
    `available_power`/`max_power` kW, status, live profile label.
    - `/pv/systems/<site>/lifetime_energy` — daily kWh arrays since
    `start_date`: `production`, `consumption`, `import`, `export`, `charge`,
    `discharge` plus flow decomposition (`solar_home`, `solar_grid`,
    `grid_home`, `battery_home`, `battery_grid`, `grid_battery`,
    `solar_battery`). Today's (last) entry updates intraday (~5 min cadence).
    - `/app-api/<site>/get_latest_power` — latest real consumption power sample.
    - `/app-api/search_sites.json` — site discovery;
    `/app-api/<site>/devices.json` — device inventory.
- **Battery control** ("BatteryConfig" microservice,
  `/service/batteryConfig/api/v1/...`; needs Bearer JWT +
  `Origin/Referer: https://battery-profile-ui.enphaseenergy.com`, `username`
  and `requestid` headers; multiple regional header variants exist):
    - `PUT /profile/<site>` — set profile: `self-consumption`, `cost_savings`,
    `backup_only`, `ai_optimisation`; body includes `batteryBackupPercentage`
    (reserve %).
    - `GET/PUT /batterySettings/<site>` — `chargeFromGrid` toggle, shutdown SOC
    (`veryLowSoc`); `POST /batterySettings/acceptDisclaimer/<site>` — one-time
    ITC disclaimer required before charge-from-grid.
    - `GET/POST /battery/sites/<site>/schedules`, `PUT /schedules/<id>`,
    `POST /schedules/<id>/delete` — persistent schedule objects with families:
        - `cfg` (charge from grid): `startTime`/`endTime` (HH:MM), `days`,
      `timezone`, `enabled`, `limit` = **charge target SOC %** (5–100).
        - `dtg` (discharge to grid): same shape; `limit` = **SOC floor** the
      battery discharges down to. **Feature-gated per site** — availability
      flags (`scheduleSupported`, `forceScheduleSupported`,
      `forceScheduleOpted`, `batteryLimitSupport`, country/region) come from
      site settings and the schedule control payload.
        - `rbd` (restrict battery discharge): a window in which the battery will
      not discharge — used for Predbat freeze modes.
- **Quirks to design for**: writes settle asynchronously (re-read to confirm;
  profile changes can stay "pending" for minutes); Enlighten rejects logins
  with "too many active sessions"; OTP/login endpoints 429 aggressively;
  ms-vs-s timestamps and payload aliases vary by endpoint family.

## Architecture

### 1. Component: `apps/predbat/enphase.py`

`class EnphaseAPI(ComponentBase)` — no `OAuthMixin` (cookie/JWT auth, not
OAuth). Same shape as `FoxAPI`:

- `initialize(username, password, site_id=None, automatic=False,
  automatic_ignore_pv=False)` — no `__init__` override (ComponentBase calls
  `initialize(**kwargs)`).
- `async run(seconds, first)` — polled every 60 s by `ComponentBase.start()`,
  with age-based refresh tiers (constants mirroring `FOX_REFRESH_*`):
    - static (site discovery, devices, site settings/capability flags): 1440 min
    - battery settings + profile + schedules: 5 min
    - `battery_status.json` (SOC/power limits): 5 min
    - `lifetime_energy` (+ `today` snapshot): 15 min
    - `get_latest_power`: 1 min
- Persistent cache through Storage (`ENPHASE_CACHE_KEYS` +
  `ENPHASE_CACHE_VERSION`), restored on first run (`load_cached_data()`
  pattern), so restarts do not hammer the API and data survives outages.
- `is_alive()` = `api_started` and a discovered site with battery data.
- Daily API-call counters and `record_api_call("enphase", ...)` telemetry,
  as fox does.

### 2. Authentication module (inside enphase.py, isolated)

- Login flow as above; session cookie jar + both JWTs held in memory and
  cached (encrypted-at-rest not required — matches existing components that
  store tokens via Storage).
- One `get_headers(family)` helper building per-endpoint-family headers
  (`site` reads vs `batteryConfig` control), including browser mimicry.
- BatteryConfig header-variant probing: primary variant first, fall back
  through the known variants on auth-shaped failures, cache the working
  variant per site via Storage.
- 401 handling: single silent re-login then one retry — behind guard rails
  copied from the reference implementation: a fresh login is reused for 30 s
  across concurrent failures; a rejected login triggers a 5-minute cooldown;
  3 consecutive rejections or a "too many active sessions" response suspends
  login attempts for 24 h (component reports unhealthy via
  `fatal_error_occurred`).
- MFA (`requires_mfa`) → fatal error with a log message instructing the user
  to disable MFA on the Enphase account. Documented limitation.
- Login-wall detection: HTML body on a JSON endpoint is treated as an auth
  failure, never a JSON parse crash.
- Request helper `request_get/post` with bounded retries, `Retry-After`
  honouring, and jittered backoff (fox `request_get` pattern).

### 3. Published entities

Per site, under `{prefix}_enphase_{site_id}_`:

**Sensors (monitoring)**

| Entity | Source |
|---|---|
| `soc_percent` | site `current_charge` (capacity-weighted per-battery fallback) |
| `soc_kw` (available energy) | site `available_energy` |
| `battery_capacity` | site `max_capacity` |
| `battery_rate_max` | site `max_power` (assumed symmetric) |
| `battery_reserve` | profile `batteryBackupPercentage` |
| `battery_status`, `battery_profile` | battery_status / profile reads |
| `pv_today`, `load_today`, `import_today`, `export_today`, `battery_charge_today`, `battery_discharge_today` | today's entries of `lifetime_energy` arrays (cumulative kWh, intraday-updating) |
| `load_power` | `get_latest_power` (real sample) |
| `pv_power`, `grid_power`, `battery_power` | derived by differentiating the `lifetime_energy` flow channels over the polling window (the reference integration's method); documented as estimates |

**Controls (consumed by Predbat's Inverter class)** — fox naming pattern
`..._battery_schedule_{direction}_{attribute}`:

- `select` charge/export window start & end times (`OPTIONS_TIME_FULL`)
- `number` charge target SOC, export target SOC, reserve
- `switch` charge enable, export enable, and a `..._write` apply button
- Export controls are only published when the site's capability flags report
  `dtg` support; otherwise Predbat is configured without forced-export.

### 4. Control mapping (write path)

Events (`select_event`/`number_event`/`switch_event`, routed by
`event_filter="predbat_enphase_"`) mutate a local schedule model; pressing the
write switch calls `apply_battery_schedule()` which diffs desired vs actual
and issues only the changed calls:

| Predbat intent | Enphase calls |
|---|---|
| Self-Use (no active window) | `PUT profile` → `self-consumption` + reserve %; cfg/dtg schedules disabled |
| Forced Charge window | ensure `chargeFromGrid` enabled (accept ITC disclaimer once); create/update `cfg` schedule: window times, `limit` = charge target SOC |
| Forced Export window | create/update `dtg` schedule: window times, `limit` = export target SOC (only when site supports dtg) |
| Charge/discharge freeze | `rbd` schedule window (battery discharge blocked, SOC held) |
| Reserve | `batteryBackupPercentage` via profile PUT |

Write-settle handling: after a write, bounded re-reads confirm the change
(writes land asynchronously, sometimes minutes later); a pending flag
suppresses duplicate PUTs; all writes are change-gated (no-op if the value
already matches), following fox `write_setting_from_event`.

### 5. Predbat wiring (config only — no core-code changes)

- `components.py`: `COMPONENT_LIST["enphase"]` — `class: EnphaseAPI`,
  `name: "Enphase API"`, `event_filter: "predbat_enphase_"`, `phase: 1`,
  args: `username` → `enphase_username` (required), `password` →
  `enphase_password` (required), `site_id` → `enphase_site_id`,
  `automatic` → `enphase_automatic`, `automatic_ignore_pv` →
  `enphase_automatic_ignore_pv`.
- `config.py`: `APPS_SCHEMA` entries for those keys, and
  `INVERTER_DEF["EnphaseCloud"]` modelled on `FoxCloud`: HA-entity control
  (`has_rest_api: False`), `time_button_press: True`,
  `charge_time_entity_is_option: True`, charge target SOC supported (via cfg
  `limit`), export target SOC supported where dtg is available, and
  `can_span_midnight: False` — Enphase windows are HH:MM within one day, and
  this flag makes Predbat split midnight-crossing windows automatically
  (same as FoxCloud).
- `automatic_config()`: sets `inverter_type=["EnphaseCloud"]` and points
  `soc_percent`, `soc_max`, `battery_rate_max`, `load_today`, `pv_today`,
  `import_today`, `export_today`, charge/export window entities, target SOC
  numbers, reserve and `schedule_write_button` at the published entities
  (fox.py:2204 pattern).
- Template `templates/enphase_cloud.yaml`.

### 6. Testing

`apps/predbat/tests/test_enphase_api.py` using the fox mock pattern:
`MockEnphaseAPI(EnphaseAPI)` overriding `request_get`/`request_post`,
`dashboard_item`, state wrappers and `log`, with
`set_http_response(path, status, json_data, ...)` to simulate HTTP statuses,
HTML login walls and Enphase payloads. Coverage targets:

- login happy path (cookie + token extraction), MFA rejection, login-wall
  detection, "too many sessions" 24 h suspension, 401 → re-login → retry,
  cooldown behaviour
- header construction per endpoint family; BatteryConfig variant fallback +
  caching
- parsing: battery_status (per-battery weighting), lifetime_energy (today
  extraction, aliases), latest power (ms vs s timestamps), derived power
- schedule model: compute/validate/diff, cfg/dtg/rbd payload construction,
  limit bounds, dtg capability gating, write-settle confirm + pending state
- event handlers (select/number/switch) and `apply_battery_schedule`
- `automatic_config()` arg wiring
- cache save/load and cache-version migration

Registered in `unit_test.py` as `("enphase_api", run_enphase_api_tests, ...)`.
100% docstring coverage (interrogate) as enforced repo-wide.

### 7. Documentation

- `docs/components.md`: "Enphase API (enphase)" section — what it does,
  config table, MFA limitation, unofficial-API risk statement.
- `docs/inverter-setup.md`: "Enphase Cloud" section.
- `docs/apps-yaml.md`: `enphase_*` keys.
- `.cspell/custom-dictionary-workspace.txt`: Enphase, Enlighten, Encharge,
  Enpower, entrez, etc.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Unofficial API changes without notice | Isolate all endpoint/paths/headers in constants; cache-version gate; health monitoring surfaces breakage quickly |
| Account lockout / too-many-sessions | Login guard rails (30 s reuse, 5 min cooldown, 24 h suspension); never burst password logins |
| Writes settle slowly (minutes) | Predbat already writes schedules ahead of window start (fox `time_button_press` model); pending-state + confirm re-reads |
| dtg unavailable in many regions | Capability-gated: component publishes export controls only when supported; Predbat plans without forced export otherwise |
| Regional header variants | Variant probing with per-site caching of the working variant |
| MFA accounts | Detected, clear error, documented; future enhancement (OTP entry entity) |

## Out of scope (future enhancements)

- Email-OTP MFA support via an OTP input entity
- AWS IoT MQTT livestream for true real-time power
- EV charger (IQ EVSE), heat pump, tariff, storm guard, grid on/off
- Legacy AC Battery (HTML-scraped endpoints)
