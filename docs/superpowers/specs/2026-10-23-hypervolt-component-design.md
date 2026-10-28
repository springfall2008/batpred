# Hypervolt Component — Design

Date: 2026-10-23
Status: Awaiting review

## 1. Purpose

Add Hypervolt EV charger support to Predbat as a pluggable cloud component, in the shape of
the Wallbox component (`apps/predbat/wallbox.py`,
[its design](2026-10-05-wallbox-component-design.md)), so a Hypervolt works without Home
Assistant (Predbat.com and Docker) and without the regex wiring and start/stop automation that
[docs/devices.md](../../devices.md) describes today.

Scope:

- Monitoring of every charger on one Hypervolt account, published as Predbat entities.
- Automatic configuration wiring those entities into the `car_charging_*` arguments.
- Manual controls: charge on/off, maximum charging current and charge mode.
- Opt-in Predbat-led charging, where the car plan stops and allows charging on the charger.
- A command line test mode that doubles as the tool for capturing real payloads.

The reference is the Home Assistant custom integration
[gndean/home-assistant-hypervolt-charger](https://github.com/gndean/home-assistant-hypervolt-charger)
(v2.10.0, MIT). It uses the `websockets` library; its calls are reimplemented over `aiohttp`
(`ClientSession.ws_connect`), so no dependency is added. Hypervolt publishes no API: what follows
is what that integration does, and section 13 lists what must be confirmed on a real charger.

## 2. The Hypervolt API

### 2.1 Authentication

Keycloak, OpenID Connect token endpoint
`https://kc.prod.hypervolt.co.uk/realms/retail-customers/protocol/openid-connect/token`,
form-encoded:

| Call | Body | Notes |
|---|---|---|
| Sign in | `client_id=home-assistant`, `grant_type=password`, `scope=openid profile email offline_access`, `username`, `password` | 401 or 400 `invalid_grant` means bad credentials |
| Refresh | `client_id=home-assistant`, `grant_type=refresh_token`, `refresh_token` | 400 or 401 means the refresh token is no longer valid: fall back to sign in |

Both return `access_token`, `refresh_token` and `expires_in` (seconds). REST calls send
`Authorization: Bearer <access_token>`. The `client_id` is the one the Home Assistant
integration uses; see section 13.

### 2.2 REST

| Purpose | Request | Result |
|---|---|---|
| List chargers | `GET https://api.hypervolt.co.uk/users/me?includes=chargers` | `chargers: [{"id": "<decimal string>", "model": "Home 3 Pro", "adopted_at": "…"}]` |

The V2 schedule endpoint is not used (schedule editing is out of scope).

### 2.3 The sync websocket

One per charger: `wss://api.hypervolt.co.uk/ws/charger/{id}/sync`, opened with headers
`Origin: https://hypervolt.co.uk` and `User-Agent: Predbat`. Messages are JSON-RPC 2.0. Every
request carries a unique string `id`; the reply carries the same `id` with `result` or `error`.
The server also pushes unsolicited messages with `method` and `params`.

After connecting, the client sends `login` and, on `{"authenticated": true}`, asks for state:

| Request | Params | Reply / push |
|---|---|---|
| `login` | `{"token": <access_token>, "version": 3}` | `{"authenticated": true}` |
| `sync.snapshot` | — | list of single-key objects, see 2.5 |
| `plugncharge.get` | — (API version 3 only) | `{"applied": "boost"}` |
| `firmware.version` | — | a string or number, e.g. `"2483.2"` |
| `get.name` | — | the charger's name |
| `get.pilot_status` | — | `{"pilot_status": …}`; the integration only sees it pushed, so whether the request is answered is to be confirmed (section 13) |
| `sync.apply` | one or more of `{"release": bool}`, `{"max_current": <mA>}`, `{"solar_mode": "<mode>"}` | the applied keys, as a snapshot |
| `plugncharge.set` | `{"mode": "<mode>"}` (API version 3 only) | `{"mode": "boost", "applied": true}` |

Pushes seen on the sync socket: `sync.apply` (same shape as a snapshot), `get.session` (2.6,
API version 3) and `get.pilot_status` (`{"pilot_status": "A"|"B"|"C"}`). An `error` reply looks
like `{"code": 409, "error": "Concurrent modifications invalidated this request"}`.

### 2.4 The session websocket (API version 2 only)

`wss://api.hypervolt.co.uk/ws/charger/{id}/session/in-progress`, same headers, no login message.
It pushes session objects (2.6) only. An API version 3 charger delivers the same data as
`get.session` on the sync socket, so this socket is opened only for API version 2.

### 2.5 Snapshot fields used

| Key | Meaning |
|---|---|
| `release_state` | `default`, or `released` when charging has been stopped by the user or by Predbat |
| `max_current` | milliamps |
| `solar_mode` | charge mode on API version 2: `boost`, `eco`, `super_eco` |
| `lock_state` | `unlocked`, `pending_lock`, `locked`; published as an attribute only |
| `features` | list; `home-battery-drain-prevention` adds the `super_eco_battery_safe` mode |

### 2.6 Session fields used

`charging` (bool), `session` (id; a new id is a new session), `watt_hours`, `true_milli_amps`,
`voltage`, `ct_current` (mA), and on API version 3 `ev_power`, `house_power`, `grid_power`,
`generation_power` (W).

### 2.7 Charger and API versions

Pure function `charger_api_version(charger_id, firmware_version)`, ported from the integration:

- Major version from the id: the number of hex digits in the decimal id, rounded up to an even
  number; 12 → 2, 16 → 3, anything else (or unparsable) → 3.
- Major 3 → API 3. Major 2 with a firmware version string of 7 characters or fewer (updated V2
  firmware reports `"1"` or `"1.0"`) → API 3. Otherwise → API 2, including while firmware is not
  yet known.

So a V2 charger first logs in as API 2 and is promoted to API 3 once `firmware.version` arrives.
On promotion the client sends `plugncharge.get` and closes the session socket.

## 3. Architecture

One new file, `apps/predbat/hypervolt.py`:

- `HypervoltCharger` — dataclass of one charger's state, built up from messages: id, model,
  name, firmware, `api_version`, `release_state`, `charging`, `pilot_status`, power in watts,
  session id, session energy in kWh, max current in amps, charge mode, features, lock state,
  `last_message` (epoch seconds of the last message from either socket).
- `apply_snapshot(charger, items)`, `apply_session(charger, result)`, `apply_pilot(charger,
  result)`, `apply_plugncharge(charger, result)` — pure functions that update a
  `HypervoltCharger` in place from a message body. A missing key leaves the field unchanged; a
  malformed value is ignored, never raised.
- `HypervoltTransport` — owns tokens, the REST call, and per charger a `ChargerLink`: the sync
  socket, the session socket where needed, a reader task each, and a map of pending request ids
  to futures. Request/response correlation makes every command awaitable:
  `await transport.set_release(charger_id, True)` returns the result or raises. Raises
  `HypervoltAuthError` (bad credentials), `HypervoltRefusedError` (an `error` reply),
  `HypervoltTimeoutError` (no reply within `COMMAND_TIMEOUT_SECONDS`), `HypervoltApiError`
  (anything else), all under `HypervoltError`. REST calls and websocket connects are recorded
  with `record_api_call("hypervolt", …)`.
- `HypervoltAPI(ComponentBase)` — publishing, event handling, automatic configuration and
  plan-led control, driven from `run()` every 60 seconds. It reads charger state from the
  transport and never touches `aiohttp`.

### 3.1 Connection model

The sockets are long-lived, unlike Wallbox's per-request REST. `HypervoltTransport.connect(id)`
starts a reader task per socket that loops: connect, (sync) login and request state, read until
closed, back off, reconnect. Back-off starts at a random 3–12 seconds and grows by 1.7× to a
300 second cap, reset after the fourth message on a connection, as the integration does. The
reader updates the `HypervoltCharger` through the pure `apply_*` functions and resolves pending
futures; it never calls Predbat.

`run()` checks link health each cycle:

- The access token is refreshed when it has less than `TOKEN_MARGIN_SECONDS` (600), or half its
  lifetime if that is shorter, left (a 300 second Keycloak token would otherwise always be due).
  A refresh does not reconnect the sockets: the token is only sent at login, and a reader that is
  dropped reconnects with the current token. `authenticate()` holds a lock, so readers
  reconnecting together share one refresh.
- Only the sync reader signs in; the session socket carries no token. Once a password sign in
  is refused, readers get the refusal without a request, and only `run()` retries.
- After start-up, a refused sign in backs off (120 seconds doubling to an hour) so old
  credentials cannot lock the Hypervolt account; each `run()` still logs and returns False.
  When it succeeds again the stopped readers are restarted.
- A charger with no message for `PROBE_SECONDS` (120) is sent `sync.snapshot`; at
  `STALE_SECONDS` (240) it is stale, published but skipped by control; at 480 seconds it is
  reconnected. A charger whose reader task has ended is reconnected on the next `run()`.
- One message that cannot be handled is logged and skipped; anything else unexpected reconnects
  with back-off. An `error` reply may be a dict or a bare string.
- `final()` cancels every reader task and closes every socket, so a component restart leaves no
  orphaned task.

## 4. Configuration

### 4.1 `COMPONENT_LIST` entry (`components.py`)

```python
"hypervolt": {
    "class": "hypervolt.HypervoltAPI",
    "name": "Hypervolt Charger",
    "event_filter": "predbat_hypervolt_",
    "args": {
        "username": {"required": True, "secret": True, "config": "hypervolt_username"},
        "password": {"required": True, "secret": True, "config": "hypervolt_password"},
        "automatic": {"required": False, "config": "hypervolt_automatic", "default": True},
        "hypervolt_control": {"required": False, "config": "hypervolt_control", "default": False},
    },
    "phase": 1,
    "can_restart": True,
},
```

There is no poll interval: state is pushed, and `run()` publishes every 60 seconds.

### 4.2 `APPS_SCHEMA` additions (`config.py`)

```python
"hypervolt_username": {"type": "string", "empty": False},
"hypervolt_password": {"type": "string", "empty": False},
"hypervolt_automatic": {"type": "boolean"},
"hypervolt_control": {"type": "boolean"},
```

## 5. Published entities

Per charger, with `<p>` = `{prefix}_hypervolt_{charger_id}`, all through `dashboard_item` with
`app="hypervolt"`. Friendly names carry the charger's name, as Wallbox's do.

| Entity | State | Notes |
|---|---|---|
| `sensor.<p>_status` | `charging`, `stopped` (released), `connected`, `disconnected`, `unknown` | attributes: `model`, `name`, `firmware_version`, `api_version`, `release_state`, `pilot_status`, `lock_state`, `features`, `stale` |
| `sensor.<p>_power` | W | `ev_power` when present, unless it is under half of `true_milli_amps` × `voltage` / 1000 (and that is at least 500 W) - a V3 charger's `ev_power` lags at the start of a charge - else `true_milli_amps` × `voltage` / 1000; a message with neither keeps the last value; 0 when not charging |
| `sensor.<p>_session_energy` | kWh | `watt_hours` / 1000, the session maximum; holds the last value between sessions and restarts at the new value when the session id changes, so it is never `unknown`. On the V3 API a message with no session id (it sends none; seen on a Home 3 Pro, firmware 4009) starts a new session when its reading is below the held value, so the sensor drops to 0 at a stop |
| `binary_sensor.<p>_connected` | on/off/unavailable | pilot B or C → on, A → off; with no pilot status yet: on while charging, else unavailable |
| `binary_sensor.<p>_plugged` | on/off/unavailable | as `_connected`, but never inferred from charging (see `plugged_state`) |
| `binary_sensor.<p>_charging` | on/off | session `charging` |
| `switch.<p>_charging` | on/off | on when `release_state` is `default`; on sends `release: false`, off sends `release: true` |
| `number.<p>_max_charging_current` | A | min 6, max 32, step 1 |
| `select.<p>_charge_mode` | `boost` / `eco` / `super_eco` (+ `super_eco_battery_safe` when the feature is listed) | |

Global: `switch.{prefix}_hypervolt_control`, published only while plan-led control is available.

The state of charge is not published: the charger cannot know it.

## 6. Automatic configuration

As Wallbox section 6, with `hypervolt` in place of `wallbox`. Car order is the account's charger
list in id order, append-only while Predbat runs. Through `set_arg_auto`:

| Argument | Value |
|---|---|
| `car_charging_energy` | list of `sensor.<p>_session_energy` |
| `car_charging_planned` | list of `binary_sensor.<p>_connected` |
| `car_charging_plugged` | list of `binary_sensor.<p>_plugged` |
| `car_charging_power` | list of `sensor.<p>_power` |
| `car_charging_now` | the same power list, `overwrite=False` |
| `num_cars` | raised to the number of chargers if lower, via `set_arg` |

An API version 2 charger has no pilot status, so its `_connected` sensor is on while it charges
and unavailable otherwise. The component logs one `Info` per such charger pointing at the V2 guidance in
`docs/car-charging.md` (a user-maintained helper for `car_charging_planned`), but still wires it,
so the list stays one entry per car.

## 7. Controls

### 7.1 Manual

Event stubs queue `(handler, args)`; the queue is drained at the top of the next `run()`, as in
Wallbox. Each command is awaited for its reply. A refused or timed-out command logs a warning
with Hypervolt's reason and does not fail the component.

| Entity | Action |
|---|---|
| `switch.<p>_charging` | on → `sync.apply {"release": false}`, off → `{"release": true}` |
| `number.<p>_max_charging_current` | `sync.apply {"max_current": amps × 1000}`, amps clamped to 6–32; non-numeric ignored |
| `select.<p>_charge_mode` | API 3: `plugncharge.set {"mode": value}`; API 2: `sync.apply {"solar_mode": value}`; a value not in the options ignored |

### 7.2 Predbat-led charging (`hypervolt_control`)

Available when `hypervolt_control` and `hypervolt_automatic` are both on; the user can then turn
it on and off with `switch.{prefix}_hypervolt_control`. Each `run()`, for car N, read the
`planned` attribute of `binary_sensor.{prefix}_car_charging_slot` (suffix `_N` for N > 0) and
parse it with `utils.parse_car_plan_windows` / `in_car_plan_window`:

- Inside a window, not disconnected, and `release_state` is `released` → send `release: false`,
  and drop the charger from `released_by_predbat`. "Not disconnected" rather than "connected",
  so an API 2 charger, which never reports a car that is not drawing power, can be started.
- Outside a window, connected, and `release_state` is `default` → add the charger to
  `released_by_predbat`, save, then send `release: true`. Recorded before it is sent, as Wallbox
  records a pause.
- Disconnected (pilot A) → if it is still `released` and Predbat stopped it, send `release: false`
  (whether a stop outlives the unplug is not known); then forget it.
- Stale, or no plan for the car → nothing.

Unlike Wallbox, which can only pause a charger that is already charging, a Hypervolt can be
released while it waits for the car, so a car plugged in outside a window is stopped at the next
`run()` that sees pilot B, within 60 seconds. `run()` is the one place commands are sent; the
reader tasks only record state.

At most one command per charger per `run()`, only on fresh state (no message older than
`STALE_SECONDS`).

**Fail open.** A charger in `released_by_predbat` that still reads `released` and whose
connection is unknown publishes `_connected` as on. One whose stop never took effect (it reads
`default`) does not, and is forgotten inside a window. Otherwise a V2 API charger, or a V3 charger after a restart with no pilot
status, would drop out of `car_charging_planned` once stopped, get no plan, and never be started.

**Read only** is read with `get_arg("set_read_only")`: the base's `set_read_only` attribute is
`True` from `reset()` until the first `update_pred()`, which comes after this component's first
run.

**Release.** When Predbat is read-only, or the control switch is off, or control is unavailable,
every charger in `released_by_predbat` is sent `release: false` and forgotten. This hands the
charger back to its own charge mode and schedule.

**Persistence.** The switch state and `released_by_predbat` are saved with
`storage.save("hypervolt", "control_state", …)` and loaded on the first run, merging rather than
replacing, retried until the read succeeds. This is the myenergi lesson in
`tools/debug-journal.md`: release must run from stored state even when control has since been
turned off.

**Known limits.** A schedule enabled in the Hypervolt app competes with Predbat: inside a Predbat
window the charger still waits for its own schedule. Eco and Super Eco modes only charge from
surplus solar. The docs tell users to use Boost with the schedule off when they turn
`hypervolt_control` on, and the status sensor carries the charge mode so the conflict is visible.

## 8. Error handling

- `HypervoltAuthError` at sign in logs an error and returns False, so the base class's start-up
  back-off applies. After start-up, a failed refresh falls back to sign in; a failed sign in
  logs an error each `run()` and returns False.
- A socket that will not connect is the reader's business (its back-off); `run()` returns True
  while the account signed in and at least one charger has a fresh message, and False when every
  charger is stale. An account with no chargers yet returns True without stamping a success, as
  Wallbox does, so start-up is not held up waiting for it.
- `update_success_timestamp()` is called only when at least one charger has a fresh message.
- The charger list is fetched on the first run and every 30 minutes; a charger added later gets
  a link and becomes the next car.

## 9. Command line test interface

`python3 hypervolt.py --username … --password …`, in the shape of Wallbox's `main()`:

- Default: sign in, list chargers, connect, wait `--wait` seconds (default 20), print each
  charger's record.
- `--raw` prints every message received (with the token redacted) as JSON lines, for fixtures.
- `--charger <id>` selects a charger for the actions; default the first.
- `--start`, `--stop` (release false/true), `--max-current <amps>`,
  `--mode {boost,eco,super_eco,super_eco_battery_safe}`.

## 10. Testing

`apps/predbat/tests/test_hypervolt.py`, in the Wallbox style: assert-style functions called from
one `test_hypervolt(my_predbat=None)`, registered in `TEST_REGISTRY` as `hypervolt`.

- Transport: REST under `patch("aiohttp.ClientSession")`; sockets through a `FakeSocket` whose
  `receive()` returns queued messages and records `send_str()` calls. No real network.
- Component tests replace the transport with a stub holding `HypervoltCharger` records.
- Fixtures are real messages captured with `--raw` from a maintainer's charger, with ids, names
  and tokens replaced. Capture is the second task, so the `apply_*` functions are written
  against real data.

Coverage: every `apply_*` function, including missing and malformed keys; `charger_api_version`
for 12- and 16-byte ids, short and long firmware, unparsable ids; sign in, refresh, refresh
rejected then sign in, bad credentials; login sent with the token, state requested after
`authenticated`, `plugncharge.get` only for API 3; reply correlation, error reply, timeout;
reconnect with back-off and reconnect after token refresh; promotion from API 2 to 3; the
session socket only for API 2; entity set for one and two chargers; session energy across a new
session; every manual control and its clamping; automatic configuration; every control rule in
7.2, release, persistence across a restart; staleness skips control and fails the cycle only
when every charger is stale; `final()` cancels the tasks.

The last task is a live check on real hardware: stop, start, plug in outside a window, release.

## 11. Documentation

- `docs/components.md`: a `Hypervolt Charger (hypervolt)` section and table of contents entry.
- `docs/car-charging.md`: a "Hypervolt direct integration" section with Predbat-led charging,
  the schedule and Eco caveats, the V2 plug detection limit, and the missing state of charge;
  add Hypervolt to the lists of supported charger integrations.
- `docs/apps-yaml.md`: the four keys and the two secrets.
- `docs/devices.md`: the existing Hypervolt section becomes the Home Assistant route, with a
  pointer to the direct component.
- `apps/predbat/config/apps.yaml`: a commented example block after Wallbox's.
- `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`: the component count and names.

## 12. Out of scope

- Schedule editing, activation mode (plug and charge / schedule / Octopus), LED controls, lock
  control and charger renaming.
- Octopus Intelligent wiring: the API exposes no dispatch slots.
- Reporting to the discovery coordinator.
- The Predbat.com integration definition and credential UI, which live in another repo.

## 13. Risks, to settle on hardware

- **The `client_id`.** `home-assistant` is the client Hypervolt issued for the community
  integration. Reusing it from Predbat may or may not be welcome; ask Hypervolt (or gndean)
  before release. It is one constant, `HYPERVOLT_CLIENT_ID`.
- **Pilot status at connect.** The integration never requests `get.pilot_status`; it may be
  pushed only on a change, leaving `_connected` unavailable after a restart until the cable
  moves. The capture task must establish this. If it is not pushed at connect, try sending
  `get.pilot_status` as a request.
- **Release across unplug.** Whether `release_state` returns to `default` when the car is
  unplugged, and whether a `release: false` sent to a disconnected charger is refused.
- **`ev_power` units and presence**, and whether `get.session` is pushed while not charging.
- **Lock.** Whether a locked charger refuses `release`.
