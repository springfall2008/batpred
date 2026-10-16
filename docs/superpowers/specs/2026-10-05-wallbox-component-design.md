# Wallbox Component — Design

Date: 2026-10-05
Status: Awaiting review

## 1. Purpose

Add Wallbox EV charger support to Predbat as a pluggable cloud component, so a Wallbox
works without Home Assistant (Predbat.com and Docker) and without the regex wiring that
[docs/devices.md](../../devices.md) describes today.

Scope:

- Monitoring of every charger on one Wallbox account, published as Predbat entities.
- Automatic configuration wiring those entities into the `car_charging_*` arguments.
- Manual controls: pause/resume, lock, maximum charging current and Eco-Smart mode.
- Opt-in Predbat-led charging, where the car plan pauses and resumes the charger.
- A command line test mode that doubles as the tool for capturing real payloads.

The reference for the API is the Home Assistant `wallbox` integration and the `wallbox`
PyPI library (v0.9.0) it wraps. That library is synchronous `requests`, so its calls are
reimplemented in `aiohttp` and no dependency is added.

## 2. The Wallbox API

Two hosts: `https://user-api.wall-box.com/` for authentication and
`https://api.wall-box.com/` for everything else.

### 2.1 Authentication

| Call | Request | Notes |
|---|---|---|
| Sign in | `GET user-api…/users/signin`, HTTP Basic (username, password), header `Partner: wallbox` | 403 means bad credentials |
| Refresh | `GET user-api…/users/refresh-token`, `Authorization: Bearer <refresh token>`, header `Partner: wallbox` | 401 means the refresh token was revoked: fall back to sign in |

Both return `data.attributes` with `token`, `refresh_token`, `ttl` and
`refresh_token_ttl`. The two TTLs are absolute expiry times in epoch milliseconds. Every
other call sends `Authorization: Bearer <token>`, `Accept: application/json` and
`Content-Type: application/json;charset=UTF-8`.

### 2.2 Endpoints used

| Purpose | Request | Body |
|---|---|---|
| List chargers | `GET v3/chargers/groups` | ids at `result.groups[].chargers[].id` |
| Charger status | `GET chargers/status/{id}` | see 2.3 |
| Pause / resume | `POST v3/chargers/{id}/remote-action` | `{"action": 2}` / `{"action": 1}` |
| Resume schedule | `POST v3/chargers/{id}/remote-action` | `{"action": 9}` |
| Lock / unlock | `PUT v2/charger/{id}` | `{"locked": 1}` / `{"locked": 0}` |
| Max charging current | `PUT v2/charger/{id}` | `{"maxChargingCurrent": <amps>}` |
| Eco-Smart | `PUT v4/chargers/{id}/eco-smart` | `{"data": {"attributes": {"enabled": 0\|1, "mode": 0\|1}, "type": "eco_smart"}}` |

Any call can return 429 (too many requests). The writes can return 403: seen live for a pause sent
to a locked charger, and treated by the Home Assistant integration as missing admin rights.
They return 409 when the action does not apply in the charger's state, such as a resume
with nothing paused.

### 2.3 Status fields used

| Field | Meaning |
|---|---|
| `status_id` | Numeric state, see 2.4 |
| `charging_power` | kW |
| `added_energy` | kWh delivered this session; resets for each session |
| `max_available_power` | Upper bound for the charging current, in amps despite the name |
| `name` | Charger name |
| `config_data.max_charging_current` | Amps |
| `config_data.locked` | Lock state |
| `config_data.serial_number`, `.part_number`, `.software.currentVersion` | Identity |
| `config_data.operation_mode` | `ocpp` when an OCPP backend runs the charger; published as an attribute of the status sensor. Seen live: such a charger stays Locked and an API unlock has no effect. Plan-led control skips a charger in this mode, with one warning |
| `config_data.timezone`, `.country.iso2`, `.zipcode` | Location, published as attributes of the status sensor |
| `config_data.ecosmart.enabled`, `.mode` | Absent when the charger has no Eco-Smart; mode 0 is eco, 1 is full solar |

The payload has a `state_of_charge` field, but a Type 2 connector cannot report the car's
state of charge, so it is not read.

### 2.4 Status codes

| Codes | State | Connected | Charging |
|---|---|---|---|
| 193, 194, 195 | Charging | yes | yes |
| 196 | Discharging | yes | no |
| 178, 182 | Paused | yes | no |
| 177, 179 | Scheduled | yes | no |
| 164, 180, 181, 183–189 | Waiting: the car is connected and the charger is waiting for it to draw energy | yes | no |
| 210 | Locked, car connected | yes | no |
| 165, 209 | Locked | no | no |
| 161, 162 | Ready | no | no |
| 0, 163 | Disconnected | no | no |
| 166 | Updating | no | no |
| 14, 15 | Error | no | no |
| anything else | Unknown | no | no |

The published status text is the finer Home Assistant wording (for example "Waiting in
queue by Power Boost"), so entity states match what existing users already see.

## 3. Architecture

One new file, `apps/predbat/wallbox.py`, in the shape of `myenergi.py`:

- `WallboxCharger` — a dataclass holding one charger's normalised state: id, name,
  serial, status id, status text, connected, charging, paused, locked, power in watts,
  session energy, max charging current, max available current and Eco-Smart mode
  (`None` when unsupported).
- `normalise_charger(charger_id, payload)` — a pure function from a status payload to a
  `WallboxCharger`. Missing or malformed fields give safe defaults, never an exception.
- `WallboxTransport` — owns the tokens and every HTTP call. A new `aiohttp.ClientSession`
  per request with a 30 second total timeout, as myenergi does. Raises `WallboxAuthError`
  (bad credentials), `WallboxRateLimitError` (429), `WallboxRefusedError` (403 on a
  write) or `WallboxApiError`, all under `WallboxError`. Every call is recorded with
  `record_api_call("wallbox", …)`.
- `WallboxAPI(ComponentBase)` — polling, publishing, event handling, automatic
  configuration and plan-led control. It never touches `aiohttp`.

The transport checks the token before each call: reuse it while it has more than
`poll_seconds` left, otherwise refresh, otherwise sign in. Tokens are held in memory
only, so a restart costs one sign in.

## 4. Configuration

### 4.1 `COMPONENT_LIST` entry (`components.py`)

```python
"wallbox": {
    "class": "wallbox.WallboxAPI",
    "name": "Wallbox Charger",
    "event_filter": "predbat_wallbox_",
    "args": {
        "username": {"required": True, "secret": True, "config": "wallbox_username"},
        "password": {"required": True, "secret": True, "config": "wallbox_password"},
        "automatic": {"required": False, "config": "wallbox_automatic", "default": True},
        "wallbox_control": {"required": False, "config": "wallbox_control", "default": False},
        "poll_seconds": {"required": False, "config": "wallbox_poll_seconds", "default": 120},
    },
    "phase": 1,
    "can_restart": True,
},
```

There is no switch to hide the manual controls: they are always published.

### 4.2 `APPS_SCHEMA` additions (`config.py`)

```python
"wallbox_username": {"type": "string", "empty": False},
"wallbox_password": {"type": "string", "empty": False},
"wallbox_automatic": {"type": "boolean"},
"wallbox_control": {"type": "boolean"},
"wallbox_poll_seconds": {"type": "integer", "zero": False},
```

`wallbox_poll_seconds` is rounded to a multiple of 60 and clamped to 60–1800, as
myenergi's is, so the 60 minute component health window can never be tripped by config.

## 5. Published entities

Per charger, with `<p>` = `{prefix}_wallbox_{charger_id}`, all through `dashboard_item`
with `app="wallbox"`:

| Entity | State | Notes |
|---|---|---|
| `sensor.<p>_status` | Status text | attributes: `status_id`, `name`, `serial_number`, `part_number`, `software_version` |
| `sensor.<p>_power` | Watts | `charging_power` × 1000 |
| `sensor.<p>_session_energy` | kWh | `added_energy` |
| `binary_sensor.<p>_connected` | on/off | from 2.4 |
| `binary_sensor.<p>_charging` | on/off | from 2.4 |
| `switch.<p>_charging` | on/off | on resumes, off pauses |
| `switch.<p>_locked` | on/off | |
| `number.<p>_max_charging_current` | Amps | min 6, max `max_available_power`, step 1 |
| `select.<p>_eco_smart` | `off` / `eco_mode` / `full_solar` | only when the charger reports Eco-Smart |

Global: `switch.{prefix}_wallbox_control`, published only while plan-led control is
available (section 7).

## 6. Automatic configuration

Runs after the first successful poll when `wallbox_automatic` is on, and again only when a
charger is added to the account. Car order comes from the account's charger list in numeric
id order and is append-only while Predbat runs, so charger N is car N even when a status
read fails, and a charger added later becomes the next car. All through `set_arg_auto`:

| Argument | Value |
|---|---|
| `car_charging_energy` | list of `sensor.<p>_session_energy` |
| `car_charging_planned` | list of `binary_sensor.<p>_connected` |
| `car_charging_power` | list of `sensor.<p>_power` |
| `car_charging_now` | the same power list, with `overwrite=False` |
| `num_cars` | raised to the number of chargers if lower, via `set_arg` |

`car_charging_soc` is not set: the charger cannot know it. Users who want Predbat to plan
to a target must supply it from their car's own integration, and the docs say so.
`car_charging_now` relies on the existing rule that a power sensor counts as charging
from `CAR_CHARGING_NOW_POWER_W` (200 W).

## 7. Controls

### 7.1 Manual

Event stubs queue `(handler, args)` and the queue is drained at the top of the next
`run()`, as both existing chargers do. Each successful write is followed by an immediate
re-poll of that charger so the entity reflects the result.

| Entity | Action |
|---|---|
| `switch.<p>_charging` | on → resume, off → pause |
| `switch.<p>_locked` | lock / unlock |
| `number.<p>_max_charging_current` | set, clamped to 6..`max_available_power` |
| `select.<p>_eco_smart` | `off` disables; `eco_mode` and `full_solar` enable with mode 0 and 1 |

A failed write logs a warning and does not fail the component.

### 7.2 Predbat-led charging (`wallbox_control`)

Available when `wallbox_control` and `wallbox_automatic` are both on. The user can then
turn it on and off at runtime with `switch.{prefix}_wallbox_control`.

Each poll, for each charger N, read the `planned` attribute of
`binary_sensor.{prefix}_car_charging_slot` (suffix `_N` for N > 0) and parse it with the
shared `utils.parse_car_plan_windows` / `in_car_plan_window`:

- Inside a window and the charger is Paused → resume.
- Outside a window and the charger is Charging → pause, and record that Predbat paused it.
- Any other state → no action. Resume applies only to a Paused charger: one in Scheduled
  is following its own schedule and one in Waiting is waiting on the car, so neither is
  sent anything.
- A locked charger is sent neither pause nor resume, by plan-led control, by release or by
  the manual switch: Wallbox refuses both while it is locked (seen live: 403 and 409).
  Plan-led control warns once per charger; the manual switch warns each time. Predbat never
  unlocks a charger. A charger Predbat paused stays on its list until it is unlocked.

At most one command is sent per charger per poll, and only on freshly polled state, so a
slow charger is not sent the same command repeatedly.

**Release.** When Predbat is in read-only mode, or the control switch is off, or control
is not available at all, every charger Predbat paused is resumed and then sent "resume
schedule", and the record is cleared.

**Persistence.** The control switch state and the list of charger ids Predbat paused are
saved through the Storage component (`storage.save("wallbox", "control_state", …)`) and
loaded on the first run. Release runs from the stored list even when control is
unavailable. This is the lesson from the myenergi entry in `tools/debug-journal.md`,
where control state latched at the first tick left a charger stopped after a restart with
control turned off.

**Known limits.** Wallbox can only be paused once it is charging. A car plugged in outside
a planned window therefore draws power until the next poll, up to `wallbox_poll_seconds`.
A schedule set in the Wallbox app competes with Predbat: a charger held in Scheduled does
not charge inside a Predbat window. The docs tell users to clear the Wallbox schedule
when they turn `wallbox_control` on.

## 8. Polling and error handling

- `run()` is called every 60 seconds; a poll happens on the first run, after a control
  event, and every `wallbox_poll_seconds`.
- The charger list is fetched on the first run and every 30 minutes. Each poll makes one
  status call per charger.
- `WallboxAuthError` on sign in logs an error and returns False, so the base class's
  start-up back-off applies.
- `WallboxRateLimitError` skips polls on a doubling back-off (2, 4, 8 minutes) capped at
  15 minutes, and returns True so one 429 does not count as a failure. The cap keeps
  recovery inside the 60 minute health window.
- `WallboxRefusedError` (403) on a control the user sent is logged each time with Wallbox's
  reason. In the plan-led loop it is logged once. Monitoring carries on.
- Any other `WallboxError` on a poll logs a warning and returns False.
- `update_success_timestamp()` is called only after a poll that returned at least one
  charger.

## 9. Command line test interface

`python3 wallbox.py --username … --password …`, in the shape of myenergi's `main()`:

- Default: sign in, list chargers, print the normalised record of each.
- `--raw` prints the raw status payload as JSON, for building fixtures.
- `--charger <id>` selects a charger for the actions; default is the first.
- `--pause`, `--resume`, `--resume-schedule`, `--lock`, `--unlock`,
  `--max-current <amps>`, `--eco-smart {off,eco_mode,full_solar}`.

## 10. Testing

`apps/predbat/tests/test_wallbox.py`, in the myenergi style: plain assert-style functions
called from one `test_wallbox(my_predbat=None)`, a real `WallboxAPI(MockBase(), …)`, the
transport tested under `patch("aiohttp.ClientSession")` and replaced by a stub for
component tests. Registered in `TEST_REGISTRY` in `unit_test.py` as `wallbox`.

Fixtures are real payloads captured with `--raw` from the maintainer's charger, with the
serial number, charger id and name replaced. Capture is the first implementation task, so
the normaliser is written against real data.

Coverage:

- Normalisation: every row of the status table, a missing `ecosmart` block, a missing
  `config_data`, kW to W.
- Transport: sign in, token reuse, refresh, refresh rejected with 401 then sign in, 403
  on sign in, 429, 403 on a write, timeout, and the exact method, URL and body of each
  write.
- Publishing: the entity set for one and for two chargers; the Eco-Smart select absent
  when unsupported.
- Events: each control's API call, clamping of the current, and the re-poll after a write.
- Automatic configuration: the argument lists, charger order, `car_charging_now` not
  overwritten, nothing set when `wallbox_automatic` is off.
- Control: resume inside a window, pause outside, no action when disconnected, locked, Scheduled or Waiting,
  one command per poll, second car reads the `_1` slot sensor, release on read-only and
  on switch off, release after a restart from stored state, control state saved and
  loaded.
- Polling: cadence, 429 back-off and recovery, `update_success_timestamp` only on success.

The last task is a live check on real hardware: pause, resume and release.

## 11. Documentation

- `docs/components.md`: a `Wallbox Charger (wallbox)` section and table of contents entry.
- `docs/car-charging.md`: a "Wallbox direct integration" section including Predbat-led
  charging and the missing state of charge, plus the lists of supported charger
  integrations.
- `docs/apps-yaml.md`: the five keys and the two secrets.
- `docs/devices.md`: the existing "Wallbox Pulsar" section becomes the Home Assistant
  route, with a pointer to the direct component as the alternative.
- `apps/predbat/config/apps.yaml`: a commented example block next to Ohme's.
- `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`: the component count and
  name lists.

## 12. Out of scope

- Energy cost, ICP maximum current, restart, firmware update and schedule editing.
- Quasar bidirectional discharge control; "Discharging" is reported as a status only.
- Octopus Intelligent wiring: the Wallbox API exposes no dispatch slots.
- Reporting to the discovery coordinator (myenergi does not either).
- The Predbat.com integration definition and credential UI, which live in another repo.

## 13. Risks, to settle on hardware

- Whether a 120 second poll stays clear of the rate limit. Home Assistant uses 90.
- Whether the API accepts a `User-Agent` of `Predbat`. The library sends
  `HomeAssistantWallboxPlugin/1.0.0`.
- Some cars go to sleep when paused and will not resume. This is a property of the car,
  and is documented as a caveat of Predbat-led charging.
