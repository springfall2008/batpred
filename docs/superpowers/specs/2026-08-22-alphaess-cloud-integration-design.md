# AlphaESS Cloud Inverter Integration — Design

Date: 2026-08-22
Status: Draft (awaiting review)

## Goal

Add an AlphaESS Open API cloud component to Predbat, modelled on the existing Sunsynk and
DEYE cloud integrations (`apps/predbat/sunsynk.py`, `apps/predbat/deye.py`), providing
monitoring and battery control for AlphaESS SMILE/Storion hybrid inverters via the
AlphaESS Open API, so an AlphaESS inverter can be driven by Predbat with no local
hardware or Modbus access, on both the self-hosted Home Assistant add-on and the
Predbat.com SaaS platform.

The user supplies an AlphaESS developer **AppID** and **AppSecret**, obtained from
<https://open.alphaess.com/>.

## Scope decisions (agreed)

- **Standalone module.** New `alphaess.py` and `alphaess_const.py` written in
  `sunsynk.py`'s image, with their own derivation logic. No existing component is
  modified. This matches how every cloud component in Predbat stands alone today — `fox`,
  `deye`, `sunsynk`, `solax`, `enphase` and `solis` share only `ComponentBase` and
  `MockBase`.
- **Native `aiohttp` client, no new dependency.** The `alphaessopenapi` PyPI package is
  *not* added to `requirements.txt`. The auth is a single SHA-512 hash and the endpoints
  are plain REST, so the client is short; and the package discards the response `code`,
  which this design depends on to tell a successful write from a rejected one (see
  "Writes are ambiguous without the code"). This also keeps the component testable
  against the existing mock infrastructure with no network stubbing library.
- **No `OAuthMixin`.** AlphaESS auth is stateless — every request is signed from the
  AppID and AppSecret. There is no token, no refresh, no expiry. The Predbat.com SaaS
  path injects `alphaess_app_id`/`alphaess_app_secret` directly, so both deployment modes
  use one code path.
- **Multi-inverter from the start.** Discover every system bound to the AppID and
  register each as a Predbat inverter, with an optional `alphaess_inverter_sn` filter.
- **Predbat's controls pass straight through.** Predbat owns the charge window, the
  export window, reserve, target SOCs and enables. The component maps them onto the
  AlphaESS schedule fields directly and lets the inverter do the timing. It does *not*
  re-derive a per-instant work mode the way `sunsynk.py` must, because the AlphaESS API
  is genuinely a schedule API.
- **Probe the periodic API, fall back to legacy.** `setTimeChargeBySn` offers six windows
  per day and a per-window power setpoint but is not entitled on many systems. Probe once
  per serial, cache the verdict, and use the universally available
  `updateChargeConfigInfo`/`updateDisChargeConfigInfo` pair otherwise.
- **Bind and unbind supported.** Bind is CLI-only (the API requires an emailed code that
  cannot be entered from a toggle). Unbind gets both a CLI flag and a per-serial toggle
  switch, following the Sigenergy `offboard` pattern.
- **Battery and solar only.** The EV charger endpoints are read for an informational
  power sensor only. No EV charger control, no system binding management beyond
  bind/unbind, no meter offset.
- **Control on by default, EXPERIMENTAL wire format.** `alphaess_control_enable` defaults
  to true, as `sunsynk_control_enable` does — an inverter component that does not drive
  the inverter is not what a user configuring it expects. Nobody on the project has an
  AlphaESS account, so every request/response is traced to the log by default and a
  tester's log is usable evidence. `switch.predbat_set_read_only` gates every write,
  including the component's own reconcile loop.
- **No history backfill.** `getOneDayPowerBySn` returns a ~288-sample daily series that
  could seed load history on a fresh install, and `GECloudData` is the existing pattern
  for that. Explicitly out of scope: the 5-minute energy tier supplies data on a regular
  basis from the moment the component starts, which is what Predbat's learning needs.

## Background: the AlphaESS Open API

Base URL: `https://openapi.alphaess.com/api`

### Auth

Stateless per-request signing. Every call carries four headers:

| Header | Value |
|:--|:--|
| `appId` | The developer AppID |
| `timeStamp` | Unix seconds |
| `timestamp` | Unix seconds (the API accepts and the reference client sends both spellings) |
| `sign` | `sha512(appId + appSecret + timeStamp)`, lower-case hex |

The timestamp must be within **300 seconds** of server time, so a host with a badly
skewed clock fails every call with `6006`. The component detects this specific case and
logs it as a clock problem rather than a credentials problem.

### Response envelope

```json
{"code": 200, "msg": "Success", "expMsg": null, "extra": null, "data": {...}}
```

Success is `code == 200`. The periodic endpoints report their status in `info` rather
than `msg`, so the success test checks `code`, `msg` and `info`.

`expMsg` is null most of the time, but on parameter errors it is the only field that
names what was wrong — `msg` says "Parameter error" while `expMsg` says "time list is
null". It is always logged.

### Return codes

| Code | Meaning | Handling |
|:--|:--|:--|
| `200` | Success | — |
| `6001` | Parameter error | Fatal for that call; log `expMsg` |
| `6002` | SN not bound to the user | Actionable — bind first |
| `6003` | You have bound this SN | **Success** on bind (idempotent) |
| `6004` | CheckCode error | Actionable — wrong CheckCode on verify |
| `6005` | AppID not bound to the SN | **Success** on unbind (already gone) |
| `6006` | Timestamp error | Host clock skew — logged as such |
| `6007` / `6010` / `6012` | Sign verification error / sign empty / AppId empty | Credentials problem |
| `6008` | Set failed | Write rejected — check the 15-minute grid and window overlap |
| `6009` | Whitelist verification failed | The developer account has an IP allow-list |
| `6017` | No operation permissions | Periodic API not entitled — cached, never retried |
| `6038` | System SN does not exist | Actionable |
| `6042` | System offline | Routine and transient; not a component failure |
| `6046` | Verification code error | Actionable — re-run `--verify` |
| `6053` | Request too fast | Back the tier off; not a failure |

### Reads

| Endpoint | Returns |
|:--|:--|
| `getEssList` | Every system bound to the AppID: `sysSn`, `cobat` kWh, `mbat`, `minv`, `poinv` kW, `popv` kW, `surplusCobat` kWh, `usCapacity` %, `emsStatus` |
| `getLastPowerData` | Live watts: `ppv`, `pload`, `soc` %, `pgrid`, `pbat`, `pev`, per-phase and per-string detail |
| `getOneDateEnergyBySn` | Daily kWh: `epv`, `eCharge`, `eDischarge`, `eGridCharge`, `eInput`, `eOutput`, `eChargingPile` |
| `getSumDataForCustomer` | Daily and lifetime totals including `eload` |
| `getChargeConfigInfo` | `gridCharge`, `timeChaf1`, `timeChae1`, `timeChaf2`, `timeChae2`, `batHighCap` |
| `getDisChargeConfigInfo` | `ctrDis`, `timeDisf1`, `timeDise1`, `timeDisf2`, `timeDise2`, `batUseCap` |
| `getTimeChargeBySn` | Periodic schedule — `chargeTimeList`, `dischargeTimeList`, `executeCycleType`, `gridChargeCycle`, `ctrDisCycle` |

Field naming: `f` = *from* (start), `e` = *end*. `timeChaf1` is the start of charge period
1, `timeChae1` is its end.

### Writes

| Endpoint | Fields |
|:--|:--|
| `updateChargeConfigInfo` | `sysSn`, `batHighCap`, `gridCharge`, `timeChaf1`, `timeChae1`, `timeChaf2`, `timeChae2` |
| `updateDisChargeConfigInfo` | `sysSn`, `batUseCap`, `ctrDis`, `timeDisf1`, `timeDise1`, `timeDisf2`, `timeDise2` |
| `setTimeChargeBySn` | `sysSn`, `executeCycleType`, `chargeTimeList`, `dischargeTimeList`, optional `gridChargeCycle`, `ctrDisCycle` |
| `bindSn` | `sysSn`, `code` |
| `unBindSn` | `sysSn` |
| `getVerificationCode` | `sysSn`, `checkCode` — **GET**, despite the portal describing a JSON body |

Both `update*ConfigInfo` endpoints are **full replacements, not patches**. All seven
fields must be sent or the omitted ones are silently reset. The component therefore
always builds a complete payload from its cached config read.

**Times are on a 15-minute grid.** `HH:mm`, minimum `00:00`, maximum `23:45`, steps of
`:00`/`:15`/`:30`/`:45`. Values off the grid are *accepted by the API and ignored by the
device* — a silent no-op, which is the worst possible failure mode, so snapping is done
in the component and asserted in tests.

**Disabling a period** is start == end, conventionally `00:00`–`00:00`.

### Writes are ambiguous without the code

Every write endpoint returns `data: null` on success *and* on failure. Only `code`
distinguishes them. The `alphaessopenapi` package returns `None` in both cases, which is
why this design uses a native client: `_request` returns the parsed envelope, and the
caller decides based on `code`.

### Rate limits

| Scope | Limit |
|:--|:--|
| General polling | AlphaESS advise a minimum **10-second** interval; exceeding it returns `6053` |
| `updateChargeConfigInfo` | Documented as **once per 24 hours** |
| `updateDisChargeConfigInfo` | Documented as **once per 24 hours** |
| Signature validity | `timeStamp` within **300 seconds** of server time |

The 24-hour write limit is documented but not enforced in practice — the Home Assistant
AlphaESS integration writes far more often. It is nevertheless treated as a real budget
by this design: see "Write minimisation".

## Architecture

### Component

`AlphaESSAPI(ComponentBase)` in `apps/predbat/alphaess.py`, registered in
`components.py`:

```python
"alphaess": {
    "class": AlphaESSAPI,
    "name": "AlphaESS Cloud API",
    "event_filter": "predbat_alphaess_",
    "args": {
        "app_id": {"required": False, "config": "alphaess_app_id"},
        "app_secret": {"required": False, "config": "alphaess_app_secret"},
        "inverter_sn": {"required": False, "config": "alphaess_inverter_sn"},
        "automatic": {"required": False, "default": False, "config": "alphaess_automatic"},
        "automatic_ignore_pv": {"required": False, "default": False, "config": "alphaess_automatic_ignore_pv"},
        "control_enable": {"required": False, "default": True, "config": "alphaess_control_enable"},
        "battery_rate_max": {"required": False, "config": "alphaess_battery_rate_max"},
        "api_delay": {"required": False, "default": 2, "config": "alphaess_api_delay"},
        "min_write_interval": {"required": False, "default": 300, "config": "alphaess_min_write_interval"},
    },
    "required_or": ["app_id"],
    "phase": 1,
    "can_restart": True,
},
```

`control_enable` defaults to **true**, matching `sunsynk_control_enable`: a user who has
configured an inverter component expects it to drive the inverter. Set it false for
monitoring only. The documented 24-hour write limit is handled by write minimisation
rather than by refusing to write at all, and `switch.predbat_set_read_only` remains the
per-run way to hold every write back.

### Files

| File | Change |
|:--|:--|
| `apps/predbat/alphaess_const.py` | **New** — base URL, endpoint map, return codes, field maps, TTLs, cache names, timeouts, time-grid helpers |
| `apps/predbat/alphaess.py` | **New** — `AlphaESSAPI`, the native client, control derivation, publishing, bind/unbind, and the standalone CLI |
| `apps/predbat/components.py` | Register the `alphaess` component |
| `apps/predbat/config.py` | `INVERTER_DEF["AlphaESSCloud"]` and the `alphaess_*` `APPS_SCHEMA` keys |
| `templates/alphaess_cloud.yaml` | **New** example configuration |
| `docs/apps-yaml.md` | New "AlphaESS Cloud API" section |
| `docs/inverter-setup.md` | AlphaESS entry |
| `apps/predbat/unit_test.py` | Register the new test modules |
| `apps/predbat/tests/test_alphaess_*.py` | **New** — six test modules |
| `.cspell/custom-dictionary-workspace.txt` | `alphaess`, `sysSn`, `batHighCap`, `batUseCap`, `ctrDis`, `timeChaf`, `timeChae`, `timeDisf`, `timeDise`, `cobat`, `poinv`, `popv`, `minv`, `mbat`, `usCapacity`, `epv`, `openapi` and friends |

### Inverter definition

```python
"AlphaESSCloud": {
    "name": "AlphaESSCloud",
    "has_rest_api": False,
    "has_mqtt_api": False,
    "output_charge_control": "power",
    "charge_control_immediate": False,
    "has_charge_enable_time": True,
    "has_discharge_enable_time": True,
    "has_target_soc": True,
    "has_reserve_soc": True,
    "has_timed_pause": False,
    "charge_time_format": "HH:MM:SS",
    "charge_time_entity_is_option": True,
    "soc_units": "%",
    "num_load_entities": 1,
    "has_ge_inverter_mode": False,
    "has_ge_eco_toggle": False,
    "has_fox_inverter_mode": False,
    "time_button_press": True,
    "clock_time_format": "%Y-%m-%d %H:%M:%S",
    "write_and_poll_sleep": 2,
    "has_time_window": False,
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "has_idle_time": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "target_soc_used_for_discharge": True,
},
```

Notes on the non-obvious choices:

- **`charge_time_format: "HH:MM:SS"`** — anything else makes `inverter.py` replace the
  published select entities with its own dummies and the window never reaches the
  component. The AlphaESS API wants `HH:mm`; the conversion happens at the payload
  boundary, not in the entity.
- **`can_span_midnight: False`** — wrap-around behaviour is undocumented for
  `timeChaf1`/`timeChae1`. Predbat splits the window instead, and period 2 takes the
  second half.
- **`output_charge_control: "power"`** — the periodic path has a real `chargePower`
  setpoint. On the legacy path a non-zero rate simply means "unrestricted"; a rate of
  **zero** is meaningful on both paths (see "Rate zero is freeze").
- **`has_timed_pause: False`** — there is no pause endpoint, so Predbat expresses freeze
  through the charge/discharge rate entities.

## Control model

### Predbat's controls map straight through

The component publishes Predbat's control surface as HA entities under
`predbat_alphaess_<sn>_*`, reads them back every tick, and builds two payloads. There is
no per-instant state machine: AlphaESS is a schedule API, so the inverter does the timing.

| Predbat control entity | AlphaESS field |
|:--|:--|
| `scheduled_charge_enable` | `gridCharge` (1/0) |
| `charge_start_time` | `timeChaf1` (15-min snapped) |
| `charge_end_time` | `timeChae1` (15-min snapped) |
| `charge_limit` % | `batHighCap` |
| `scheduled_discharge_enable` | `ctrDis` (1/0) |
| `discharge_start_time` | `timeDisf1` (15-min snapped) |
| `discharge_end_time` | `timeDise1` (15-min snapped) |
| `discharge_target_soc` % while exporting, else `reserve` % | `batUseCap` |

`execute.py` already gates how far ahead a window is programmed
(`(minutes_start - self.minutes_now) <= self.set_window_minutes`, `execute.py:514`), so
Predbat never hands the component a window hours in advance. That is why the naive
pass-through is safe and no window-blanking state machine is needed.

**`batUseCap` serves two Predbat concepts** because the API has only one field for the
discharge floor. It is the export target while an export window is programmed and the
reserve otherwise. A one-line conditional in the payload builder, asserted in tests.

**Period 2 is the midnight split, not a state.** `timeChaf2`/`timeChae2` and
`timeDisf2`/`timeDise2` are `00:00`–`00:00` (disabled) unless `can_span_midnight: False`
has caused Predbat to cut a window at midnight, in which case period 2 carries the
remainder.

### Rate zero is freeze

AlphaESS has no pause endpoint, so Predbat expresses freeze by driving the rate entities
to zero (`execute.py:491-495`). The component treats zero as a distinct instruction:

| Predbat writes | AlphaESS write | Effect |
|:--|:--|:--|
| `charge_rate == 0` | `gridCharge = 0`, overriding the charge window | No grid charging — freeze charge, no cross-charging |
| `discharge_rate == 0` | `ctrDis = 1` with both discharge periods disabled | Battery holds SOC — freeze export |

A non-zero rate becomes a real `chargePower` value on the periodic path. On the legacy
path it means "unrestricted" and is not written; the docs state this so a user is not
surprised that a planned slow charge runs at full power.

### Time snapping

Window times are snapped **inward** to the 15-minute grid — start rounded up, end rounded
down — so Predbat never claims time the inverter will not honour. If snapping collapses a
window to zero length or inverts it, the window is written as disabled (`00:00`–`00:00`)
rather than as a wrap-around, and the decision is logged. Predbat's plan slots are
normally half-hourly, so in practice snapping is a no-op; the code exists for the
five-minute and manually-overridden cases.

`23:45` is the maximum. An end time of `24:00` (midnight) snaps to `23:45`.

### Periodic path

On the first config-tier refresh for each serial, `getTimeChargeBySn` is called once.

- `code == 200` → the system is entitled. `_periodic_ok[sn] = True`. Writes use
  `setTimeChargeBySn` with `executeCycleType: 0` (daily), giving up to six windows and a
  per-window `chargePower`.
- `code == 6017` → not entitled. `_periodic_ok[sn] = False`, cached to disk, **never
  retried**. The API docs are explicit that `6017` is an entitlement verdict, not a
  transient error.
- Any other failure → verdict left unknown and re-probed next config tier.

The periodic write must satisfy two constraints the legacy path does not: both lists need
at least one element (an empty list is rejected with `6001 "time list is null"`, and an
omitted key with `10001`), and charge and discharge periods must not overlap. When Predbat
has no window for one direction, that list gets a single `00:00`–`00:00` element and the
corresponding cycle flag is set to `0`, so the disabled state is expressed by the flag
rather than by an ambiguous zero-length window.

### Write minimisation

The 24-hour documented write limit is treated as a real budget, three ways:

1. **Change detection.** `_applied_payload[sn][direction]` caches the last payload
   actually sent. A byte-identical rebuild is not re-sent. Charge and discharge payloads
   are gated **independently**, so a charge-only change does not consume a discharge
   write. This is the bug DEYE hit in PR #4371 (commit `3e1de759`): 40 button presses
   produced 36 byte-identical control orders over two hours on a live site because the
   write button forced the write.
2. **Minimum interval.** `alphaess_min_write_interval` (default 300 s) is the floor
   between two writes for the same serial and direction. A change arriving inside the
   window is held and applied on the next eligible tick, not dropped.
3. **`6053` backs off** rather than counting as a failure, and the tier that triggered it
   has its interval doubled up to a cap.

The write button (`time_button_press: True`) is deliberately **not** forced. Predbat
presses it every cycle as its normal "apply" action, so `force=True` there would bypass
the change-detection gate on every single cycle — exactly the DEYE bug above.

### Read-only and control gating

Writes are suppressed when either:

- `alphaess_control_enable` is false (monitoring only — it defaults to **true**, matching
  `sunsynk_control_enable`), or
- `switch.predbat_set_read_only` is on.

**The read-only gate belongs on the component's own writes, and this is easy to get
wrong.** Predbat's upstream read-only handling (`execute.py:145`) stops Predbat driving
the control entities, which covers every write that originates from a plan. It does *not*
cover a write the component initiates by itself — and this component has one:
`_reconcile_control` re-applies the schedule on every tick.

That re-apply is not idle. The payload depends on whether an export window is currently
programmed, because `batUseCap` switches between the export target and the reserve, so a
window transition changes the payload with no plan change at all. Without an explicit
gate, that transition would write to the inverter while Predbat was in read-only mode.
This is precisely GH#4436, fixed for DEYE and Sunsynk by adding `_is_read_only()` to their
reconcile loops (`deye.py:1661`, `sunsynk.py:1346`); the same gate is required here from
the start rather than added after someone reports it.

Concretely, `_reconcile_control(sn)` returns early when **any** of these hold:

- `switch.predbat_set_read_only` is on,
- `alphaess_control_enable` is false, or
- the serial is not in `control_active` — Predbat has not yet been asked to drive it, so a
  startup cycle can never clobber an inverter before there is a plan to apply.

The bind/unbind path is deliberately outside this gate: read-only guards writes to the
inverter, and unbinding is account management, not an inverter write.

## Monitoring and sensor mapping

Sensors are published as `sensor.predbat_alphaess_<sn>_<leaf>` via `dashboard_item`.

### Power — `getLastPowerData`, 60 s tier, watts

| Predbat arg | Leaf | Source | Sign |
|:--|:--|:--|:--|
| `load_power` | `load_power` | `pload` | pass through |
| `pv_power` | `pv_power` | `ppv` | pass through |
| `battery_power` | `battery_power` | `pbat` | pass through — positive is already discharge |
| `grid_power` | `grid_power` | `pgrid` | **negated** — AlphaESS is positive-on-import, Predbat wants negative-on-import |
| `soc_percent` | `soc` | `soc` | % |
| — | `ev_power` | `pev` | informational |

The `pbat` sign is established by arithmetic, not assumption: in the API documentation's
live evening sample, `pgrid 11 + pbat 1264 = pload 1275` with `ppv 0`, so a positive
`pbat` is discharge, matching Predbat's convention directly. The **charge** direction
being negative is the one part still to be confirmed by a tester, and is on the field
verification list.

`automatic_config()` sets `grid_power_invert`, `battery_power_invert` and
`load_power_invert` explicitly to `False` for every index rather than leaving them to
default. `base.args` is shared and not namespaced per inverter type, so a Teslemetry or
Fox install that legitimately inverts its own grid sensor leaves the flag set for every
index; an AlphaESS inverter that does not claim it inherits the flip, the sensor is
negated a second time, and an export reads as an import. This is the Sunsynk lesson at
`sunsynk.py:1543-1553`.

### Energy — 5 min tier, kWh daily counters

| Predbat arg | Leaf | Source |
|:--|:--|:--|
| `load_today` | `load_today` | `getSumDataForCustomer` → `eload` |
| `import_today` | `import_today` | `getOneDateEnergyBySn` → `eInput` |
| `export_today` | `export_today` | `getOneDateEnergyBySn` → `eOutput` |
| `pv_today` | `pv_today` | `getOneDateEnergyBySn` → `epv` |
| — | `battery_charge_today` | `eCharge` |
| — | `battery_discharge_today` | `eDischarge` |
| — | `grid_charge_today` | `eGridCharge` |

`eload` is the reason `getSumDataForCustomer` is called at all —
`getOneDateEnergyBySn` has no load field. When `eload` is null the component falls back to
the energy balance `epv + eInput − eOutput − eCharge + eDischarge` rather than leaving
`load_today` unmapped. The API docs warn that most `SumData` fields are null without a
configured tariff, though the `e*` daily totals were populated on the live test account.

These counters reset at midnight; `minute_data`/`clean_incrementing_reverse` absorbs that.
They are published with `device_class: energy` and `state_class: measurement` so the
recorder keeps them.

### Static and ratings — `getEssList`, 8 h tier

| Predbat arg | Source | Notes |
|:--|:--|:--|
| `soc_max` | `cobat` (kWh) | Direct |
| `inverter_limit` | `poinv` (kW × 1000) | Direct — the AC/grid-side limit |
| `battery_rate_max` | `poinv` (kW × 1000), overridable by `alphaess_battery_rate_max` | Derived, not reported — see "Battery rate max" below |
| `export_limit` | **Not in the API** | Unmapped, with a warning — otherwise Predbat falls back to the 99999 W default and plans exports the grid connection clips |
| `battery_min_soc` | Deliberately **not** mapped | `batUseCap` is a field Predbat *writes*; reading it back as the floor would be circular |
| `battery_temperature` | Not available | Unmapped |

`minv`, `mbat`, `emsStatus` and `popv` are published as informational sensors only.

`usCapacity` and `surplusCobat` are published but **not** mapped to `soc_percent`/`soc_kw`.
The arithmetic in the API docs' two live samples (`13.34/13.34 = 100.0` and
`9.09/10.1 = 90.0`) fits both "current SOC" and "configured usable depth" equally well,
and this is an 8-hour tier regardless, so live SOC comes from `LastPower.soc` where there
is no ambiguity.

Throughout, the Sunsynk rule applies: an arg is mapped only when **every** discovered
inverter reports the underlying value. An arg pointing at a sensor that never appears is
worse than an absent arg the user can fill in.

### Battery rate max

The API reports no battery charge/discharge power limit and no pack current or voltage
from which to derive one, so unlike `sunsynk.py` there is nothing to compute. It is
nevertheless **derived from `poinv` rather than left unmapped**, because leaving it
unmapped is not neutral:

- `inverter.py:410` falls back to a hard-coded **2600 W** when `battery_rate_max` is
  absent from `base.args`. On a SMILE5 (`poinv` 5.0 kW) that is roughly half the real
  rate, applied silently to every plan, with nothing in the log to indicate it.
- `inverter.py:423` computes
  `battery_rate_max_charge = min(inverter_limit_charge, battery_rate_max_raw)`, and
  `inverter_limit_charge` itself defaults to `battery_rate_max_raw` (`inverter.py:415`).
  So `battery_rate_max` is the governing value; mapping `inverter_limit` from `poinv`
  does **not** rescue it.

`poinv` is the inverter's nominal AC power, which on a matched AlphaESS package (a SMILE5
paired with SMILE-BAT modules) is close to the battery rate. Where it is not — a small
battery on a large inverter, where the BMS C-rate binds first — the estimate is high, and
that error is the safer one to make: `inverter.py:1295-1318` measures the achieved rate,
derives a charge/discharge power curve, and logs
`Consider setting in HA: input_number.battery_rate_max_scaling: X`, with
`battery_charge_power_curve_auto` able to apply the curve automatically. An over-estimate
is therefore self-reporting and correctable; the 2600 W default is invisible.

`alphaess_battery_rate_max` overrides the derived value outright, for a user who knows
their pack's real limit. The startup log states which of the two is in use and where the
derived figure came from.

`export_limit` is treated differently and deliberately left unmapped, because there is no
equivalent proxy: `poinv` is the inverter rating, not the site's grid-connection limit, and
a G98/G99-capped site can sit far below it. Guessing there would produce a plan that
over-exports with no feedback path, whereas the warning prompts the user to enter the one
number only they know.

### Hybrid versus AC-coupled

`inverter_hybrid` is one of Predbat's own `CONFIG_ITEMS` switches, not an `apps.yaml` arg,
so it is written with `set_state_external` rather than `set_arg_auto` — writing the entity
state alone would move the displayed switch without changing the value the planner reads
(`component_base.py:316-324`). GECloud infers it from the model string
(`gecloud.py:1188-1197`) and Teslemetry hard-sets it off for every Powerwall
(`teslemetry.py:689-698`); this component follows GECloud's shape but is deliberately more
conservative about acting on the verdict.

**The two errors are not symmetric.** `inverter_hybrid` controls whether PV output counts
against the inverter's capacity and incurs its conversion loss (`prediction.py:38-52`):

- **True on an actually AC-coupled system** — PV wrongly consumes inverter headroom, so
  Predbat under-uses the battery. Conservative, and it self-limits.
- **False on an actually hybrid system** — PV stops counting against `inverter_limit`, so
  Predbat plans charge-plus-PV beyond what the inverter can pass, and the surplus is
  clipped. Targets are silently missed.

Predbat's default is `True`, and every mainstream AlphaESS unit (SMILE5, SMILE-T10,
SMILE-G3, Storion-S5) is a hybrid with DC PV inputs, so the default is right for the
common case. The component therefore only ever moves the switch on positive evidence of
AC coupling, and never the other way.

**Signals, in confidence order:**

1. **`ppvDetail` all-null.** `getLastPowerData` returns `ppv1`–`ppv4` inside `ppvDetail`.
   AlphaESS uses null-for-absent in these detail objects — the API docs state `pevDetail`
   values are "`null` when no charger is fitted" — so a unit with no DC strings should
   report nulls, while a hybrid at night reports zeros (the docs' live sample shows
   `"ppv1":0.0` on a hybrid after dark). Null versus zero is the discriminator, and unlike
   a PV-power threshold it works at any time of day. Applying the `pevDetail` convention
   to `ppvDetail` is inference, not documented — hence `VERIFY@FIELD`.
2. **`popv == 0` while `epv > 0`.** A system generating solar energy but declaring no PV
   nameplate on the AlphaESS unit has its PV somewhere else. Needs daylight to evaluate,
   so it is assessed on the static tier rather than at startup.
3. **Model string**, via `ALPHAESS_AC_COUPLED_MODELS` in `alphaess_const.py`. Seeded
   **empty**. AlphaESS model naming does not encode coupling the way GivEnergy's does —
   there is no `"ac"` substring to match — and the HA integration's `KNOWN_INVERTERS`
   list (`Storion-S5`, `SMILE5-INV`, `VT1000`, `SMILE-T10-HV-INV`, `SMILE-G3-B5-INV`,
   `SMILE-G3-T10-INV`, `SMILE-S6-HV-INV`) contains no confirmed AC-coupled entry.
   Inventing a table here would be guessing; entries are added only as testers confirm
   them.

The switch is flipped to AC-coupled only when signal 1 and signal 2 **agree**, or when the
model appears in `ALPHAESS_AC_COUPLED_MODELS`. Any other combination leaves Predbat's
default alone and logs what was observed, naming the model and pointing the user at
`switch.predbat_inverter_hybrid`. A guess that lands on the damaging side of an asymmetric
error is worse than asking.

## Discovery filtering and model capability

### Systems with no battery are skipped

`getEssList` returns every product bound to the AppID, and not all of them are battery
systems — AlphaESS also sells plug-in solar (the VT1000 family), which has nothing for
Predbat to drive. Any discovered system reporting `cobat` of zero, null or missing is
skipped at discovery: not registered as a Predbat inverter, no control entities, no
control writes. It is logged once by serial and model so the user can see it was
recognised and deliberately passed over, and monitoring sensors are still published for it
if it reports power data.

This is a capability filter, not a model filter, which is why it is preferred to a
blacklist: it catches every non-battery product AlphaESS ships now or later without anyone
having to maintain a table. A serial the user has explicitly named in
`alphaess_inverter_sn` is still skipped when it has no battery — there is no plan to apply
to it — but the log says so explicitly rather than silently returning an empty device
list, since a filter matching nothing otherwise looks identical to an empty account.

### Live telemetry falls back to history

Not every system serves `getLastPowerData`. `Storion-S5` is the known example — the Home
Assistant integration skips the call outright for it (`LOWER_INVERTER_API_CALL_LIST`,
homeassistant-alphaESS `coordinator.py:2528`) — but the component does **not** keep a
model list for this. It decides on behaviour instead: if live data is not present, use the
history.

That matters because `getLastPowerData` is the whole 60-second power tier — live `soc`,
`pbat`, `pgrid`, `ppv` and `pload` — and Predbat cannot plan without a live SOC. A model
list would only cover the models someone had already written down, while the behavioural
rule covers `Storion-S5`, any unlisted model with the same gap, and a system that simply
stops answering.

**The rule.** On the power tier, call `getLastPowerData`. If it fails, or comes back
without a usable `soc`, fall back to `getOneDayPowerBySn` for today and take the most
recent sample. Those samples carry everything needed:

| History field | Supplies |
|:--|:--|
| `cbat` | Battery SOC % |
| `ppv` | PV power W |
| `load` | Load power W |
| `feedIn`, `gridCharge` | Grid power, reconstructed as `gridCharge - feedIn` |

Two details make the fallback usable rather than merely available:

- **`cbat`, not `cobat`.** The portal documents this field as `cobat`, the live API returns
  `cbat`, and reading the portal name silently yields `None` — which would look exactly
  like "this system has no SOC either". Both spellings are read, `cbat` first.
- **Grid sign.** `feedIn` and `gridCharge` are separate positive-only fields rather than a
  signed `pgrid`, so grid power is reconstructed as `gridCharge - feedIn` before Predbat's
  negate-on-import convention is applied.

**Demotion is latched, and reversible.** `getOneDayPowerBySn` returns ~288 records for a
full day, so it is not something to poll every minute. After a small number of consecutive
`getLastPowerData` failures a serial is demoted to the history path and its power tier
drops to 5 minutes, which is the resolution the history has anyway. The demotion is
re-probed on the config tier (every 30 minutes) with a single `getLastPowerData` call, so
a system that was merely offline or briefly failing returns to 60-second live data by
itself and a genuinely incapable one costs two extra calls an hour. The verdict is cached
so a restart does not re-learn it from scratch.

If neither path yields a SOC — no `getLastPowerData` and no `cbat` in the history — that
serial cannot be driven. It is logged plainly, saying which of the two calls failed and
how, and skipped rather than registered with a fabricated SOC.

`Storion-S5` also appears in the HA integration's `LIMITED_INVERTER_SENSOR_LIST`, which
needs no special handling here either: the standing rule that an arg is mapped only when
every inverter actually reports the underlying value already covers it.

## Control entities

Published per serial, matching `sunsynk.py`'s naming and domains so `inverter.py` drives
them unmodified:

| Entity | Domain | Purpose |
|:--|:--|:--|
| `predbat_alphaess_<sn>_battery_schedule_reserve` | `number` | Reserve % |
| `predbat_alphaess_<sn>_battery_schedule_charge_start_time` | `select` | `HH:MM:SS` |
| `predbat_alphaess_<sn>_battery_schedule_charge_end_time` | `select` | `HH:MM:SS` |
| `predbat_alphaess_<sn>_battery_schedule_charge_soc` | `number` | Target SOC % |
| `predbat_alphaess_<sn>_battery_schedule_charge_power` | `number` | W |
| `predbat_alphaess_<sn>_battery_schedule_charge_enable` | `switch` | — |
| `predbat_alphaess_<sn>_battery_schedule_export_*` | as above | Export equivalents |
| `predbat_alphaess_<sn>_battery_schedule_charge_write` | `switch` | Apply button |
| `predbat_alphaess_<sn>_unbind` | `switch` | Unbind this system (see below) |

The reserve and SOC entities are published with the value Predbat wrote, **not** clamped.
Predbat writes then reads back to confirm (`write_and_poll_value`), so publishing anything
other than what was written guarantees a mismatch and a retry storm. Clamping happens at
the API boundary in the payload builder.

Control entities are read **every tick, including the first**. Home Assistant retains them
across a Predbat restart, so on restart they already hold the live plan; seeding from an
empty schedule and publishing that back would cancel an in-flight charge until Predbat
next replanned.

## Bind and unbind

### The flow

Binding a system to an AppID is two steps, and the second needs a code that is emailed to
the **system owner**:

```
getVerificationCode(sysSn, checkCode)  →  emails a code to the owner's registered address
bindSn(sysSn, code)                    →  binds the system to the AppID
```

`checkCode` comes from the device label or the installer. Because the code arrives by
email and must be typed in, bind cannot be driven from a toggle — hence CLI-only.

### CLI

Added to `main()` in `alphaess.py`, alongside the diagnostic run:

```
--verify --serial SN --check-code CC   → getVerificationCode  (triggers the email)
--bind   --serial SN --code 123456     → bindSn
--unbind --serial SN                   → unBindSn
```

All three change account state, so each prompts `[y/N]` before sending. The confirmation
uses the `EOFError`/`KeyboardInterrupt` handling from `sunsynk.py`'s `--write-test`: a
remote tester on SSH or in a container with no TTY gets a clean "nothing sent", not a
traceback.

Result reporting uses the response `code`, which is the whole reason for the native
client — `data` is `null` whichever way the call went:

| Call | Code | Reported as |
|:--|:--|:--|
| bind | `200` | Bound |
| bind | `6003` | Bound (already bound — idempotent success) |
| bind | `6046` | Failed: code wrong or expired, re-run `--verify` |
| verify | `6004` | Failed: CheckCode incorrect |
| verify | `6002` | Failed: SN not bound to any user |
| verify / bind | `6038` | Failed: SN unknown to the platform |
| unbind | `200` | Unbound |
| unbind | `6005` | Unbound (was not bound — idempotent success) |

### Unbind switch

`switch.predbat_alphaess_<sn>_unbind`, published for every discovered serial, default
`off`. Follows the Sigenergy `offboard` pattern (`sigenergy.py:2008-2016`):

- **Turn on** → call `unBindSn`. On success the serial is latched in `_unbind_done`,
  persisted to the control cache, so neither the 60-second tick nor a Predbat restart
  re-fires it.
- **Failure** leaves the latch clear so the next tick retries, matching
  `_offboard_system_if_needed`.
- **Turn off** → clears the latch so discovery picks the system up again if it was re-bound
  via the CLI or the AlphaESS portal. It does **not** re-bind; that is impossible without
  the emailed code. The switch is one-way from Home Assistant, and its `friendly_name` and
  the documentation both say so.
- **After a successful unbind** the serial is dropped from `device_list`, its control
  entities stop being republished, and a warning is logged that `num_inverters` and the
  auto-config args in `apps.yaml` now reference a system Predbat can no longer read.

The switch is deliberately **not** gated on `switch.predbat_set_read_only`: read-only
guards writes to the inverter, and unbinding is account management, not an inverter write.

## Refresh tiers

| Tier | TTL | Calls per serial | Purpose |
|:--|:--|:--|:--|
| `static` | 8 h | `getEssList` (account-wide, once) | Discovery, capacity, ratings, models |
| `config` | 30 min | `getChargeConfigInfo`, `getDisChargeConfigInfo` (+ periodic probe once) | The read-modify-write baseline |
| `power` | 60 s, or 5 min when demoted | `getLastPowerData`, or `getOneDayPowerBySn` when demoted | Live telemetry — see "Live telemetry falls back to history" |
| `energy` | 5 min | `getOneDateEnergyBySn`, `getSumDataForCustomer` | Daily counters for load/import/export/PV learning |

Steady state is roughly 88 calls per hour per serial, plus one account-wide `getEssList`
per 8 hours — comfortably inside the 10-second guidance for a single system.
`alphaess_api_delay` (default 2 s) spaces consecutive calls when several serials are on
one account.

Splitting live telemetry into separate `power` and `energy` tiers exists specifically to
cut call volume: the daily energy counters do not need 60-second granularity, and polling
them at 5 minutes removes two thirds of what a single combined tier would cost.

`refresh_static` never overwrites `device_list` with an empty discovery result. Absence of
a result is not a result: one transient failure must not take a working component down
until the next success, and writing `{'device_list': []}` to the cache would additionally
make a restart skip re-discovery for a full TTL.

Tier clocks are marked fresh **only on success**, so a deferred startup genuinely retries
rather than finding the tier "fresh" and skipping the poll.

### Caching

Persisted through the Storage component (never direct file access), following
`sunsynk.py`'s four-file split:

| Cache | Contents |
|:--|:--|
| `static` | `device_list`, per-serial detail from `getEssList` |
| `config` | Last read `ChargeConfig`/`DisChargeConfig` per serial, `_periodic_ok` verdicts |
| `ratings` | Derived capacity, inverter limit |
| `control` | `local_schedule`, `_applied_payload`, `control_active`, `_unbind_done`, last write timestamps |

`load_cache` returns `{}` when `self.storage` is None — the normal state for a standalone
CLI run — silently and without flagging a restore error.

## Error handling

### Cloud latency

Settings reach the inverter on its next cloud poll, typically one to five minutes after
Predbat writes them. A read-back immediately after a write shows the old values; that is
not a failure and the component does not retry on it. The settle counter from
`sunsynk.py` (`note_settle`) is reused: a written payload is considered pending for a
fixed number of polls before an unexpected read-back counts as external interference.

### External interference

Using the AlphaESS phone app while Predbat is running overwrites Predbat's settings, and
vice versa — the write endpoints are whole-object replacements, so the last writer wins.
Detected changes to Predbat-owned fields are logged (`note_external_change`), which is
what the control ledger consumes.

### Transport versus API failure

Two distinct paths, as in the reference client:

- **API-level** (`code != 200`) — the request reached the service and was rejected.
  Resending it unchanged will not help. Logged with `code`, `msg` and `expMsg`, and
  returned to the caller as a failure with the code intact.
- **Transport** (connection reset, DNS, timeout, non-2xx HTTP) — affects every endpoint.
  Retried with backoff by `ComponentBase`.

`6042` (system offline) is routine on `getLastPowerData` for a system that has dropped off
and does not count as a component failure.

### Clock skew

`6006` means the host clock is more than 300 seconds from AlphaESS server time. It is
logged as a clock problem with the measured skew, not as a credentials problem, because
the symptom otherwise looks identical to a bad AppSecret.

## Testing

Six modules, registered in `TEST_REGISTRY` in `unit_test.py`:

| Module | Covers |
|:--|:--|
| `test_alphaess_const.py` | Endpoint map, return-code table, time-grid snapping helpers, field maps |
| `test_alphaess_api.py` | Signature construction, header set, envelope parsing, `msg`/`info` success forms, every return code in the table, bind/unbind code mapping including `6003`/`6005` as success, clock-skew detection |
| `test_alphaess_control.py` | The full control mapping table, `batUseCap` dual role, rate-zero-is-freeze, midnight split into period 2, snapping edge cases (collapse to zero, `24:00` → `23:45`), write minimisation (change detection, independent charge/discharge gating, minimum interval), periodic probe and `6017` caching, read-only gating of `_reconcile_control` specifically (a window transition that changes `batUseCap` must not write while `switch.predbat_set_read_only` is on - GH#4436), `control_enable` gating, unbind staying outside both gates, unbind switch latch idempotency and latch survival across restart, `device_list` removal after unbind |
| `test_alphaess_publish.py` | Every sensor in the monitoring tables, `pgrid` negation, the three `*_invert` flags forced to `False`, `eload` null fallback to the energy balance, `battery_rate_max` derived from `poinv` and overridden by `alphaess_battery_rate_max`, `export_limit` left unmapped with a warning, "only map when every inverter reports it" |
| `test_alphaess_config.py` | `INVERTER_DEF["AlphaESSCloud"]` completeness against the other cloud types, `APPS_SCHEMA` keys, `automatic_config` arg mapping, hybrid inference (all-null `ppvDetail` plus `popv`/`epv` disagreement leaves the switch alone; both agreeing flips it via `set_state_external`; a hybrid at night with zero-valued `ppvDetail` is never misread), history fallback when `getLastPowerData` fails or returns no `soc` (grid reconstructed as `gridCharge - feedIn`, `cbat` preferred over `cobat`, demotion latched after N failures, re-probe restoring 60 s live data, serial skipped when neither path yields a SOC), systems with zero/null/missing `cobat` skipped at discovery (including when named in `alphaess_inverter_sn`) |
| `test_alphaess_storage.py` | Cache round-trip for all four files, `storage is None` path, empty-discovery refusal, tier freshness only on success |

Tests use `TestHAInterface` from `tests/test_infra.py` with a stubbed HTTP layer — no
network, no new test dependency. Per `CLAUDE.md`, unit tests are added for all new code,
and `./run_all` output is written to a file and grepped rather than piped.

## Field verification

Nobody on the project has an AlphaESS account, so these are inferred from the API
documentation and the Home Assistant integration, and need a tester's log to confirm.
Each is marked `VERIFY@FIELD` in `alphaess_const.py`:

1. **`pbat` sign on charge.** Discharge-positive is confirmed by arithmetic from a live
   sample; charge-negative is inferred.
2. **`ctrDis = 1` with both periods disabled means "never discharge".** This is the
   natural reading of the documented "disabling a period is start == end", and it is what
   freeze export depends on.
3. **Whether surplus above house load reaches the grid during a discharge window.**
   `ctrDis` is documented as "Battery Discharge Time Control", so how much of an export
   window actually exports may depend on the unit's working mode. The writes are identical
   either way — this affects what a user should expect, not what the component sends.
4. **Whether the 24-hour write limit is enforced.** If a live tester sees `6008` or `6053`
   on a second same-day write, `alphaess_min_write_interval` becomes the primary defence
   and its default should rise.
5. **`usCapacity` semantics** — current SOC or configured usable depth. Not relied on
   either way; confirmation would let it be published with an accurate friendly name.
6. **How closely `poinv` tracks the real battery rate.** `battery_rate_max` is derived
   from the inverter's nominal AC power for want of anything better. A tester's
   `battery_rate_max_scaling` suggestion from `inverter.py:1295-1318` after a few full
   charge cycles is exactly the evidence needed; if it lands consistently below 1.0
   across systems, the derivation should apply that factor rather than `poinv` raw.
7. **`ppvDetail` null-versus-zero on an AC-coupled unit.** The hybrid inference rests on
   AlphaESS reporting null (not zero) for absent DC strings, which is documented for
   `pevDetail` and inferred for `ppvDetail`. A single log from an AC-coupled system
   settles it; until then the switch is only moved when two signals agree.
8. **Which models are actually AC-coupled.** `ALPHAESS_AC_COUPLED_MODELS` ships empty
   because AlphaESS model names do not encode coupling. Confirmed entries go in as
   testers report them.
   Separately, whether any battery-bearing system reports a zero `cobat` — if one does,
   the no-battery discovery filter would wrongly skip it and would need a second signal.
9. **Whether a `Storion-S5` really cannot serve `getLastPowerData`, and whether its
   history carries `cbat`.** The first is taken from the HA integration rather than
   measured; the behavioural fallback makes it moot either way, but the second is not —
   if the history has no SOC for such a system, it cannot be driven at all and that needs
   to be known before a user is told the model is supported.
10. **Periodic entitlement in the wild.** How many real systems answer `200` rather than
   `6017` for `getTimeChargeBySn` determines whether the periodic path is the common case
   or a rarity.

`api_debug = True` by default so every request and response is traced, with the AppSecret
and `sign` redacted. Flipped to `False` once the format is confirmed.

## Documentation

- `docs/apps-yaml.md` — an "AlphaESS Cloud API" section covering obtaining the AppID and
  AppSecret from <https://open.alphaess.com/>, every `alphaess_*` arg, the EXPERIMENTAL
  status, that `alphaess_control_enable` defaults to true and how to set it false for
  monitoring only, the cloud-to-inverter latency,
  the last-writer-wins interaction with the phone app, that a non-zero charge rate is not
  honoured on the legacy path, that `battery_rate_max` is estimated from the inverter
  rating and how to correct it with `battery_rate_max_scaling` or
  `alphaess_battery_rate_max`, that `export_limit` must be set by hand for a
  G98/G99-capped site, that `switch.predbat_inverter_hybrid` is only moved on positive
  evidence of AC coupling and should be checked by hand on a retrofit system, and that
  the unbind switch is one-way.
- `docs/inverter-setup.md` — an AlphaESS entry.
- `templates/alphaess_cloud.yaml` — a complete example.

## Out of scope

- **History backfill** from `getOneDayPowerBySn`. The 5-minute energy tier supplies data
  regularly from startup, which is what Predbat's learning needs. `GECloudData` remains
  the pattern if this is ever wanted.
- **EV charger control.** `getEvChargerConfigList` and friends are read only far enough to
  publish `pev`; `setEvChargerCurrentsBySn` and `remoteControlEvCharger` are not wired up.
- **Meter offset** (`getMeterOffsetConfigInfo` / `updateMeterOffsetConfigInfo`) — not
  available to a standard developer account.
- **Weekly scheduling.** The periodic path is used with `executeCycleType: 0` (daily)
  only. Predbat replans continuously, so a weekday-aware schedule has nothing to express.
- **Local IP polling.** The reference client can poll a device directly on the LAN; this
  component is cloud-only, matching every other Predbat cloud integration.
