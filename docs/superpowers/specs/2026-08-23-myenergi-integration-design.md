# myenergi Integration — Design

Date: 2026-08-23
Status: Approved for implementation planning

## 1. Purpose

Add myenergi Zappi (EV charger) and Eddi (hot water diverter) support to Predbat as a
pluggable component. Scope for this first implementation:

- Monitoring of Zappi and Eddi devices, published as Predbat entities.
- Automatic configuration wiring the device energy sensors into `car_charging_energy`
  (Zappi) and `iboost_energy_today` (Eddi).
- Send-boost and cancel-boost controls, exposed as switches with a companion number
  entity for the boost amount.
- Documented stubs for every other control (mode, priority, minimum green level,
  schedules, Libbi) so the shape of the interface is fixed before the work lands.
- A command line test interface, matching `fox.py`, `axle.py` and the other components.

Libbi is explicitly out of scope. Webhooks are out of scope; this release polls.

## 2. The two myenergi APIs

myenergi exposes two unrelated APIs, and Predbat needs both.

| | 3rd-party API | Direct ("director") API |
|---|---|---|
| Host | `api.s18.myenergi.net`, auth at `auth.s18.myenergi.net` | `director.myenergi.net`, redirecting to the user's active server |
| Auth | OAuth2 authorization_code, bearer JWT | HTTP Digest: username = hub serial, password = API key |
| Credentials | `client_id`/`client_secret` issued by manual myenergi partner registration | Self-served by the user at myaccount.myenergi.com |
| Protocol | REST/JSON | CGI-style GET endpoints returning JSON |
| Eddi | Documented as "support currently in development" | Fully supported |
| Token life | Access token 1 day, refresh token 1 year | n/a |
| Reference | <https://api-docs.s18.myenergi.net/> | `pymyenergi`, as used by `cjne/ha-myenergi` |

The direct API is the only one a self-hosted Home Assistant user can set up today. The
3rd-party API is the only one usable at scale by Predbat.com, and it is the officially
supported route. Supporting only one of them would strand one of the two audiences.

### 2.1 Direct API endpoints used

The hub redirects clients to a per-account server. `director.myenergi.net` returns an
`X_MYENERGI-asn` response header naming the real host (e.g. `s18.myenergi.net`); all
subsequent requests go there, and the value is re-read on every response so a server
migration is followed automatically.

| Purpose | Endpoint |
|---|---|
| All device status | `GET /cgi-jstatus-*` |
| Single device status | `GET /cgi-jstatus-{P}{serial}` where `{P}` is `Z` or `E` |
| Day history (hourly) | `GET /cgi-jdayhour-{P}{serial}-{yyyy}-{m}-{d}-{hour}-{hours}` |
| Zappi manual boost | `GET /cgi-zappi-mode-Z{serial}-0-10-{kwh}-0000` |
| Zappi smart boost | `GET /cgi-zappi-mode-Z{serial}-0-11-{kwh}-{hhmm}` |
| Zappi cancel boost | `GET /cgi-zappi-mode-Z{serial}-0-2-0-0000` |
| Zappi set mode | `GET /cgi-zappi-mode-Z{serial}-{mode}-0-0-0000` (stub) |
| Eddi boost | `GET /cgi-eddi-boost-E{serial}-10-{target}-{minutes}` |
| Eddi cancel boost | `GET /cgi-eddi-boost-E{serial}-1-{target}-0` |
| Eddi set mode | `GET /cgi-eddi-mode-E{serial}-{0\|1}` (stub) |

Boost targets are `heater1: 1`, `heater2: 2`, `relay1: 11`, `relay2: 12`. Only `heater1`
is used in this release.

Zappi charge modes are indexed `["None", "Fast", "Eco", "Eco+", "Stopped"]`; Zappi states
are `["Unkn0", "Paused", "Unkn2", "Charging", "Boosting", "Completed"]`; Eddi states are
`["Unkn0", "Paused", "Unkn2", "Diverting", "Boosting", "Max temp reached", "Stopped"]`.

Relevant raw JSON fields: `sno` serial, `sta` state index, `zmo` Zappi charge mode index,
`pst` plug state, `che` session energy in kWh, `div` diverted power in W, `grd` grid power,
`gen` generated power, `vol` voltage in decivolts, `frq` frequency, `rbt` Eddi remaining
boost seconds, `bsm` Eddi boosting flag, `tp1`/`tp2` Eddi temperatures, `hno` Eddi active
heater.

### 2.2 3rd-party API endpoints used

| Purpose | Endpoint |
|---|---|
| Token exchange / refresh | `POST https://auth.s18.myenergi.net/oauth2/token` |
| Device list | `GET /devices` |
| Device status | `GET /devices/{id}/status` |
| Send boost | `POST /devices/{id}/boost` |
| Cancel boost | `DELETE /devices/{id}/boost` |
| Set mode | `POST /devices/{id}/mode` (stub) |
| History | `GET /devices/{id}/history` (stub) |

Device IDs are the device class prefix plus serial, e.g. `ZA12345678`, `ED12345678`.

Zappi boost body is `{"mode": "normal", "parameters": {"energy": <1-99 kWh>}}`, or
`{"mode": "smart", "parameters": {"energy": N, "targetTime": "<ISO-8601>"}}`. Eddi boost
body is `{"durationMinutes": <0-240>}`. Sending Zappi fields to an Eddi (or the reverse)
is rejected with a 400, so the transport selects the body by device class.

Status fields used: `deviceClass`, `status`, `state`, `deviceStatus`, `supplyMode`,
`pilotState`, `boostCharge` (Zappi) / `boostActive` (Eddi), `actualPower`, `gridPower`,
`genPower`, `sessionEnergy`, `energyDelivered`, `timestamp`. Power is in kW and energy in
kWh, both of which are scaled to Predbat's expected units on normalisation.

## 3. Architecture

A single new module `apps/predbat/myenergi.py`, registered as component `myenergi`, built
around a transport abstraction so that everything above the wire format is written once.

```
MyEnergiAPI(ComponentBase, OAuthMixin)     # lifecycle, polling, publishing, auto-config, controls
  └── transport: MyEnergiTransport         # abstract
        ├── MyEnergiDirectTransport        # digest auth, ASN redirect, /cgi-* endpoints
        └── MyEnergiCloudTransport         # bearer JWT via OAuthMixin, REST endpoints
```

`MyEnergiTransport` is the only place that knows about wire formats. It exposes:

```python
async def connect(self) -> bool
async def fetch_devices(self) -> list[MyEnergiDevice]
async def send_boost(self, device, amount, target_time=None) -> bool
async def cancel_boost(self, device) -> bool
async def set_mode(self, device, mode) -> bool          # stub
async def set_priority(self, device, priority) -> bool  # stub
async def set_min_green_level(self, device, level)      # stub
async def get_schedule(self, device)                    # stub
async def set_schedule(self, device, schedule)          # stub
```

Stub methods log a single "not implemented in this release" warning and return `False`.
They exist so the interface is settled and the follow-up work is additive.

### 3.1 Normalised device model

Both transports return the same dataclass, so the publishing and control layers never
branch on transport:

```python
@dataclass
class MyEnergiDevice:
    device_id: str              # "Z12345678" direct, "ZA12345678" cloud
    kind: str                   # "zappi" | "eddi"
    serial: str
    name: str
    online: bool
    status: str                 # normalised: charging / boosting / diverting / paused / ...
    mode: str                   # zappi charge mode; eddi operating mode
    plug_status: str            # zappi only, "" for eddi
    power_w: float              # charging (zappi) or diverted (eddi) power
    grid_power_w: float
    generation_w: float
    voltage: float
    session_energy_kwh: float
    boost_active: bool
    boost_remaining_mins: int
    temp_1: float | None        # eddi only
    temp_2: float | None        # eddi only
```

Normalisation is two pure functions, `normalise_direct_device(raw, kind)` and
`normalise_cloud_device(raw, meta)`, testable without any network or component fixture.

### 3.2 Transport selection

`myenergi_auth_method` selects the transport: `direct` (default) or `oauth`. The
component validates at initialise time that the credentials for the chosen method are
present, and logs an actionable error naming the missing keys otherwise.

The cloud transport reuses `oauth_mixin.py` exactly as `fox.py`, `deye.py` and `solis.py`
do: the access token arrives via `myenergi_key`, refresh is delegated to the
oauth-refresh edge function keyed by `myenergi_token_hash`, and Predbat never holds a
`client_secret`. `provider_name` is `"myenergi"`. Both refresh paths are wired -
`check_and_refresh_oauth_token()` proactively before each poll for a token that has
reached its stated expiry, and `handle_oauth_401()` reactively when a poll comes back
401, with the poll retried once behind it, for a token revoked before then.

## 4. Configuration

### 4.1 `COMPONENT_LIST` entry (`components.py`)

```python
"myenergi": {
    "class": MyEnergiAPI,
    "name": "myenergi",
    "event_filter": "predbat_myenergi_",
    "args": {
        "auth_method":      {"required": False, "config": "myenergi_auth_method", "default": "direct"},
        "hub_serial":       {"required": False, "config": "myenergi_hub_serial"},
        "api_key":          {"required": False, "config": "myenergi_api_key"},
        "key":              {"required": False, "config": "myenergi_key"},
        "token_expires_at": {"required": False, "config": "myenergi_token_expires_at"},
        "token_hash":       {"required": False, "config": "myenergi_token_hash"},
        "automatic":        {"required": False, "config": "myenergi_automatic", "default": True},
        "enable_controls":  {"required": False, "config": "myenergi_enable_controls", "default": True},
        "poll_seconds":     {"required": False, "config": "myenergi_poll_seconds", "default": 60},
    },
    "required_or": ["api_key", "key"],
    "phase": 1,
    "can_restart": True,
},
```

`required_or` means the component only starts when the user has supplied credentials for
one transport or the other, matching how `axle` gates itself.

### 4.2 `APPS_SCHEMA` additions (`config.py`)

```python
"myenergi_auth_method":      {"type": "string", "empty": False},
"myenergi_hub_serial":       {"type": "string", "empty": False},
"myenergi_api_key":          {"type": "string", "empty": False},
"myenergi_key":              {"type": "string", "empty": False},
"myenergi_token_expires_at": {"type": "string", "empty": False},
"myenergi_token_hash":       {"type": "string", "empty": False},
"myenergi_automatic":        {"type": "boolean"},
"myenergi_enable_controls":  {"type": "boolean"},
"myenergi_poll_seconds":     {"type": "integer", "zero": False},
```

### 4.3 apps.yaml examples

Direct, the default for self-hosted users:

```yaml
myenergi_hub_serial: '12345678'
myenergi_api_key: 'your-api-key-from-myaccount-myenergi-com'
```

Cloud OAuth:

```yaml
myenergi_auth_method: 'oauth'
myenergi_key: '<access token>'
myenergi_token_hash: '<token hash>'
myenergi_token_expires_at: '2026-09-01T00:00:00Z'
```

## 5. Published entities

Entity names carry the serial so multi-device sites work without collisions, following
the `gecloud` per-device naming convention.

Zappi, per device:

| Entity | Notes |
|---|---|
| `sensor.predbat_myenergi_zappi_{sn}_status` | normalised status string |
| `sensor.predbat_myenergi_zappi_{sn}_mode` | Fast / Eco / Eco+ / Stopped |
| `sensor.predbat_myenergi_zappi_{sn}_plug_status` | EV connection state |
| `sensor.predbat_myenergi_zappi_{sn}_power` | W, `device_class: power` |
| `sensor.predbat_myenergi_zappi_{sn}_session_energy` | kWh, `device_class: energy` |
| `binary_sensor.predbat_myenergi_zappi_{sn}_charging` | |
| `switch.predbat_myenergi_zappi_{sn}_boost` | on = send boost, off = cancel boost |
| `number.predbat_myenergi_zappi_{sn}_boost_energy` | kWh, 1–99, default 10 |

Eddi, per device:

| Entity | Notes |
|---|---|
| `sensor.predbat_myenergi_eddi_{sn}_status` | |
| `sensor.predbat_myenergi_eddi_{sn}_power` | W |
| `sensor.predbat_myenergi_eddi_{sn}_session_energy` | kWh |
| `sensor.predbat_myenergi_eddi_{sn}_temp_1` / `_temp_2` | °C, omitted when unavailable |
| `switch.predbat_myenergi_eddi_{sn}_boost` | on = send boost, off = cancel boost |
| `number.predbat_myenergi_eddi_{sn}_boost_minutes` | minutes, 0–240, default 60 |

There is deliberately no separate `_boosting` binary sensor: the boost switch's own
state is derived from the device's `boost_active`, so a second entity would only
duplicate it.

All are published through `dashboard_item(..., app="myenergi")` with an attribute table
in the style of `ohme_attribute_table`.

## 6. Automatic configuration

Gated on `myenergi_automatic` (default true), run once after the first successful poll.

- Zappi session energy sensors → `car_charging_energy`, as a list when more than one
  Zappi is present. `minute_data_import_export` accepts a list and sums the entities.
- Zappi plug status sensors → `car_charging_planned`, as a list, which is indexed per
  car so entry N is the Nth Zappi by serial. The regex the apps.yaml templates ship for
  this key targets the third-party `ha-myenergi` integration's entity names, which do
  not match the ones this component publishes, so without this the key fails to resolve
  and Predbat silently falls back to the `car_charging_threshold` heuristic. The Zappi
  pilot states `C1`/`D1` normalise to `EV ready to charge`, which the templates'
  `car_charging_planned_response` lists did not carry and now do.
- Eddi session energy sensor → `iboost_energy_today`, first Eddi only.

All use `set_arg_auto()` so that an explicit apps.yaml value is reported rather than
silently overwritten.

Predbat reads these back from Home Assistant history as incrementing counters. Session
energy resets to zero at the end of each session, which `get_from_incrementing` handles
by clamping negative deltas to zero (`fetch.py:574`).

### 6.1 Known limitation

Session resets themselves are handled correctly. `iboost_energy_today` is read at
`fetch.py:782` as `abs(value[0] - value[minutes_now])`, but the series it reads has
already been through `minute_data_load(..., clean_increment=True)` →
`clean_incrementing_reverse()` (`utils.py:716-744`), which rebases the counter whenever
it detects a reset. A day of several Eddi sessions therefore totals correctly, and the
same holds for `car_charging_energy`.

The residual limitation is narrower, and applies to both keys equally because the loss is
in the shared cumulative series. `minute_data()` only propagates a fall as a reset when it
is near midnight or at least 1.0 kWh (`utils.py:565`); anything smaller is interpolated
over as a dip in the data before `clean_incrementing_reverse()` (`utils.py:740`) ever sees
it. A session ending below roughly 1 kWh is therefore under-counted, and an intervening
zero reading does not rescue it — the dip is smoothed away first. Measured against
Predbat's own `minute_data`, two 0.6 kWh sessions
in a day total 0.600 kWh rather than 1.20 kWh, while two sessions of 2.0 and 1.5 kWh total
correctly. This is accepted for this release: it is a fraction of a kWh, and the planner
is driven by the larger sessions. The fix, if it is wanted later, is to derive the sensor
from the day-history endpoint (`/cgi-jdayhour-E{sn}-...` or `GET /devices/{id}/history`),
which both transports already reach. This is recorded in the documentation so the
behaviour is not mistaken for a bug.

## 7. Controls

The boost switch is a momentary-style control, following `ohme.py`'s `_approve_charge`
pattern: `turn_on` sends a boost, `turn_off` cancels it, and the published state is
re-derived from the device's own `boost_active` on the next poll rather than being held
locally. That way a boost started or stopped from the myenergi app is reflected correctly.

Events arrive via `switch_event` / `number_event` and are queued onto `self.queued_events`
for the run loop rather than being actioned inside the event callback, exactly as
`ohme.py` does, so that API calls never run on the event thread.

Boost amount comes from the companion number entity, so the switch itself carries no
parameters. Sending a Zappi boost while the charger is in Fast or Stopped mode is
rejected by the API; the component checks the mode first and logs a clear warning instead
of issuing a call it knows will fail.

All controls are gated on `myenergi_enable_controls` (default true), so a user can run the
component in monitor-only mode.

### 7.1 Stubbed controls

Zappi mode select, priority, minimum green level, phase setting, lock settings, schedules
and super-schedules; Eddi mode, priority and heater priority; all Libbi support. Each is a
transport method that logs once and returns `False`, plus a line in the documentation
saying it is not yet implemented.

## 8. Polling and error handling

`ComponentBase.start()` calls `run(seconds, first)` on a fixed 60 second cadence once
started, so `myenergi_poll_seconds` (default 60) is rounded up to the nearest multiple of
60 and enforced inside `run()` by a `seconds % interval == 0` guard. It exists to let a
user back off polling on a multi-device site, not to poll faster than the base loop.

The direct transport fetches all devices in a single `/cgi-jstatus-*` call. The cloud
transport caches `GET /devices` and refreshes it every 30 minutes, polling
`/devices/{id}/status` per device in between.

- Digest auth failures and HTTP 401 are reported as configuration errors, not retried
  tightly; `ComponentBase` already applies exponential startup backoff.
- A missing `X_MYENERGI-asn` header on the direct transport means bad credentials, and is
  reported as such rather than as a transport failure.
- `update_success_timestamp()` is called on each successful poll so component health
  monitoring works.
- API calls are wrapped with `record_api_call` from `predbat_metrics`, as the other
  components do.
- The last good reading is retained when a poll returns nothing, so a transient failure
  does not publish zeros into the energy sensors that feed `car_charging_energy`.

## 9. Command line test interface

`python3 myenergi.py` with `argparse`, following `fox.py`:

```
--hub-serial SERIAL --api-key KEY     direct transport
--token KEY [--token-hash H]          cloud transport
--boost {zappi,eddi} --amount N       send a boost to the first matching device
--cancel-boost {zappi,eddi}           cancel a boost
--raw                                 dump the raw API response
```

Default behaviour with credentials only is to connect, run one poll, and print the
normalised device table. Uses `MockBase` from `mock_base.py`, is marked
`# pragma: no cover`, and never requires a running Predbat.

## 10. Testing

New file `apps/predbat/tests/test_myenergi.py`, exporting `test_myenergi()` and registered
in `TEST_REGISTRY` in `unit_test.py`.

Coverage:

1. **Normalisation** — direct and cloud raw payloads for Zappi and Eddi map to identical
   `MyEnergiDevice` values, including unit conversion (kW→W, decivolts→volts) and the
   `sta`/`zmo` index lookups. Out-of-range indices fall back safely rather than raising.
2. **Direct transport** — ASN redirect is followed and re-read; a missing `X_MYENERGI-asn`
   header is treated as an auth failure; boost and cancel produce the exact expected URLs
   for both device kinds.
3. **Cloud transport** — correct boost body per device class; Eddi never receives `mode`
   or `parameters`, Zappi never receives `durationMinutes`; bearer header is set.
4. **Transport selection** — `auth_method` picks the right class; missing credentials
   produce a clear error and no start.
5. **Publishing** — entity names, units and device classes for a two-Zappi one-Eddi site;
   temperatures omitted when unavailable.
6. **Auto-config** — `car_charging_energy` becomes a list for multiple Zappis;
   `iboost_energy_today` is set for the Eddi; nothing is set when `myenergi_automatic` is
   false; `set_arg_auto` is used.
7. **Controls** — switch and number events queue rather than calling inline; boost uses
   the number entity's value; a Zappi boost in Fast mode is refused with a warning and no
   API call; controls do nothing when `myenergi_enable_controls` is false.
8. **Stubs** — every stubbed method returns `False` and logs, without raising.
9. **Error handling** — a failed poll retains the previous reading and does not publish
   zeros; HTTP errors increment the error count without killing the component.

Tests use `unittest.mock.AsyncMock` against the transport's HTTP layer, following
`test_ohme.py` and `test_fox_api.py`. No network access.

Per the repository's shared-fixture constraint, the tests must not leak state into the
shared `my_predbat` fixture; the component is constructed against `MockBase` wherever a
full Predbat instance is not required.

## 11. Documentation

- `docs/components.md` — a `### myenergi (myenergi)` section matching the existing
  layout: what it does, when to enable, configuration options, how to get an API key
  from myaccount.myenergi.com, the published entities, the reserved controls, and the
  small-session limitation from section 6.1.
- `docs/apps-yaml.md` — the new `myenergi_*` keys.
- `.cspell/custom-dictionary-workspace.txt` — `libbi`, `jstatus`, `jdayhour`, `harvi`
  and `asn`. `Eddi`, `myenergi` and `zappi` are already present.

## 12. Out of scope

Libbi battery support; webhooks; charge schedules and super-schedules; managed mode;
cloud configuration endpoints; charge-session history import; Zappi mode control; Eddi
heater 2 and relay targets; myenergi as a Predbat inverter or battery source.

## 13. Risks

- **Partner registration.** The cloud transport cannot be tested end to end until
  myenergi issues a `client_id`/`client_secret`. Mitigation: the transport is written
  against the published OpenAPI schema and unit-tested against recorded payloads; the
  direct transport is the default so the release is useful regardless.
- **Eddi on the 3rd-party API.** myenergi document Eddi support there as "in development",
  so cloud-transport Eddi behaviour may change. Mitigation: normalisation is centralised,
  and the direct transport covers Eddi fully today.
- **Undocumented direct API.** The `/cgi-*` endpoints are not officially supported and
  could change. Mitigation: they are stable in practice and widely used by
  `pymyenergi`/`ha-myenergi`; failures degrade to a logged error, never a crash.
- **Session-energy semantics.** Covered in section 6.1.
