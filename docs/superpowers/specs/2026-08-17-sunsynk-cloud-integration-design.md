# Sunsynk Cloud Inverter Integration — Design

Date: 2026-08-17
Status: Draft (awaiting review)

## Goal

Add a Sunsynk Connect cloud component to Predbat, modelled on the existing Fox and DEYE
cloud integrations (`apps/predbat/fox.py`, `apps/predbat/deye.py`), providing monitoring
and battery control for Sunsynk hybrid inverters via the Sunsynk Connect API, so a
Sunsynk inverter can be driven by Predbat with no local hardware or Modbus dongle access,
on both the self-hosted Home Assistant add-on and the Predbat.com SaaS platform.

## Scope decisions (agreed)

- **Standalone module.** New `sunsynk.py` and `sunsynk_const.py` written in `deye.py`'s
  image, with their own derivation logic. `deye.py` is not modified. This matches how
  every cloud component in Predbat stands alone today — `fox`, `deye`, `solax`, `enphase`
  and `solis` share only `ComponentBase`, `OAuthMixin` and `MockBase`. The duplicated
  window-to-slot logic is accepted in exchange for zero risk to shipped DEYE behaviour.
- **Pure-Python RSA.** The Sunsynk login RSA-encrypts the account password. This is
  implemented in ~50 lines in `sunsynk_const.py` rather than by adding `cryptography` to
  `requirements.txt`, so no Rust-built binary wheel is introduced for the armv7/armhf
  add-on targets. Only public-key encryption is performed — no private keys, no
  timing-sensitive operations.
- **Both deployment modes, three auth methods.** A `sunsynk_auth_method` arg selects
  `password` (self-hosted, RSA-encrypted login, the default), `password_legacy`
  (self-hosted, the pre-2025 plaintext login, opt-in) or `oauth` (Predbat.com injects the
  access token), mirroring and extending `deye_auth_method`. `password` never
  auto-downgrades to `password_legacy` — see "No automatic downgrade".
- **Multi-inverter from the start.** Discover every inverter on the account and register
  each as a Predbat inverter, with an optional `sunsynk_inverter_sn` filter.
- **No Predbat-facing work-mode control.** Predbat owns the charge window, the export
  window, reserve, target SOCs and enables. The component derives the Sunsynk work mode
  internally. This is the DEYE/Enphase mode-less pattern, not the Fox work-mode-select
  pattern.
- **Battery and solar only.** No generator control, smart-load, EV charger, or
  plant-management surface.
- **Control is opt-in until field-verified.** Nobody on the project has a Sunsynk account.
  Monitoring is always active; writes to the inverter happen only when
  `sunsynk_control_enable: true` (default `false`). The default flips once a tester
  confirms the wire format.
- **Default to DEYE semantics where the API is silent.** See below — this is the
  tie-breaking rule for every unknown, and it has a deliberate boundary.

## Defaulting to DEYE semantics

Sunsynk inverters are rebadged DEYE hardware. The control registers behind both cloud
APIs are the same registers, so where this design has to guess, it guesses at whatever
`deye.py` already does. That rule has a boundary, and stating it precisely matters more
than stating it:

**Assume DEYE for *semantics*. Never assume DEYE for *encoding*.**

Semantics are register-level and transfer: the six sequential time-of-use slots and their
distinct-ascending-start-time constraint; the existence of exactly three work modes and
what each one does; which mode-and-flag combination produces charge, hold, freeze-charge,
export and freeze-export; a battery capacity reported in amp-hours needing a pack voltage;
an installer-set SOC floor the inverter will not go below; the battery-power sign
convention. All of it is lifted directly from `deye.py`, and the intent-mapping table in
"Control model" below *is* DEYE's `derive_control_state()` table.

Encoding is wrapper-level and demonstrably differs: DeyeCloud is a developer API with
camelCase fields and string enums (`"SELLING_FIRST"`) and asynchronous `orderId` writes;
Sunsynk Connect is the `csp-web` application backend with a flat object of numeric
strings and a synchronous whole-object write. Field names, value types, enum
representations and request shape are established from the Sunsynk sources only, never
carried across.

The clearest trap this rule catches is `sysWorkMode`. That Sunsynk has the same three
modes as DEYE is a semantic claim and safe to assume. That they are numbered `0`, `1`,
`2` *in DEYE's enum order* is an encoding claim, and it is exactly the kind of assumption
that is plausible, unverifiable from the sources, and wrong in a way that silently swaps
export for charge. It stays flagged as an unknown regardless of how well the semantics
line up.

Two supports for the rule:

- Where a Sunsynk field name is ambiguous, the **published Modbus register map** for these
  inverters (as used by <https://github.com/kellerza/sunsynk>) disambiguates the meaning,
  because it describes the same registers both clouds are writing. It is the tiebreaker
  when the two cloud clients disagree.
- The rule sets *defaults*, never *safety*. Guessing DEYE never relaxes a guard: the SOC
  floor clamp stays unconditional, and any response that does not parse as expected fails
  closed — the component skips the write and leaves the inverter in self-use — rather than
  falling through into assumed-DEYE behaviour. A wrong assumption must cost a missed
  optimisation, not a battery driven the wrong way.

## Background: the Sunsynk Connect API

There is no public Sunsynk API documentation. Three sources were used, all reviewed
during design:

1. <https://github.com/martinville/solarsynkv3> — the Home Assistant add-on that
   supersedes the abandoned `solarsynkv2` the request pointed at (that repository now
   404s). Confirms the auth flow, the telemetry endpoints and field names, and — despite
   its README describing it as sensor-only — it *does* write settings, via
   `settingsmanager.py`. Its control architecture is not reusable (settings are driven by
   a user typing JSON fragments into an `input_text` helper, and state is passed between
   functions through temporary files on disk), but its endpoint and field vocabulary is
   directly applicable.
2. <https://github.com/hammingweight/synkctl> — a Go CLI. The most authoritative source
   for the endpoint list, the current RSA authentication workflow (`rest/authentication.go`)
   and the full settings object shape (`rest/inverter_struct.go`).
3. <https://github.com/jamesridgway/sunsynk-api-client> and community write-ups, used to
   corroborate the read endpoints and the absence of a documented rate limit.

Sunsynk inverters are rebadged DEYE hardware, so the underlying control registers are the
same ones `deye.py` already drives. The cloud wrapper differs: DEYE exposes a
`strategy/dynamicControl` endpoint taking structured `timeUseSettingItems`; Sunsynk
exposes one flat settings object with indexed fields.

### Regions

Selected by a `sunsynk_region` arg:

| `sunsynk_region` | Host | `source` parameter |
|---|---|---|
| `sunsynk` (default) | `api.sunsynk.net` | `sunsynk` |
| `inteless` | `pv.inteless.com` | `elinter` |

### Auth

`sunsynk_auth_method` selects one of three paths. All of them end with a bearer token
used identically thereafter.

#### `password` (default) — RSA-encrypted login

Three steps, all against the region host:

1. `nonce = int(time.time() * 1000)`;
   `sign = md5(f"nonce={nonce}&source={source}POWER_VIEW")`
2. `GET /anonymous/publicKey?source={source}&nonce={nonce}&sign={sign}` →
   `data` is a base64 DER `SubjectPublicKeyInfo` RSA public key (no PEM armour).
3. `POST /oauth/token/new` with
   `{client_id: "csp-web", grant_type: "password", source, username,
   password: <base64 RSA-PKCS1v15 ciphertext>, nonce, sign}`, where this second
   `sign = md5(f"nonce={nonce}&source={source}" + public_key_string[:10])` →
   `data.access_token`, `data.refresh_token`, `data.expires_in`.

#### `password_legacy` — plaintext login

The pre-2025 flow, a single call with no public-key step, no nonce and no signature:

`POST /oauth/token` with
`{areaCode: "sunsynk", client_id: "csp-web", grant_type: "password", source, username,
password: <plaintext>}` → the same `data.access_token` / `refresh_token` / `expires_in`
envelope.

Retained because the two flows are served by different Sunsynk server generations and it
is not established which regions have migrated. `pv.inteless.com` in particular is a
separate deployment from `api.sunsynk.net` and may still be on the older path, and the
legacy endpoint is a one-line request that costs almost nothing to keep working. It is
also the diagnostic that separates "my credentials are wrong" from "the RSA handshake is
wrong" when a tester reports a login failure — the single most likely place for this
integration to fail on first contact, given nobody on the project can try it first.

#### `oauth` — injected token

Skips login entirely. `OAuthMixin` assigns the injected `sunsynk_key` straight to the
access token, exactly as the DEYE component does. This is the Predbat.com path.

#### No automatic downgrade

`password` never silently falls back to `password_legacy`. The fallback is superficially
attractive — it would make the component self-configuring across both server generations
— but it converts any failure of the `publicKey` step into a plaintext credential
transmission, and that failure is externally triggerable. TLS-intercepting middleboxes
are common on home and corporate networks, and against one of those the RSA layer is the
only thing protecting the password: an interceptor can read the TLS stream but cannot
decrypt an RSA ciphertext without Sunsynk's private key. Auto-downgrade hands it the
plaintext instead, and Sunsynk very likely added the RSA step for exactly this reason.

So the legacy method is opt-in: the user sets `sunsynk_auth_method: password_legacy`
deliberately, and the component logs a warning naming the trade-off each time it starts
with that setting. A failing RSA login logs the diagnostic pointing at the legacy option
rather than taking it unasked.

### Reads

All `GET` with `Accept: application/json` and `Authorization: Bearer <token>`. Every
response is `{code, msg, success, data}`.

| Purpose | Path | Key fields consumed |
|---|---|---|
| Inverter discovery | `/api/v1/inverters?page={n}&limit=10&type=-2&status=-1` | `data.infos[].sn`, `data.total` |
| Inverter detail | `/api/v1/inverter/{sn}` | `ratePower`, `model`, `plant.id`, `etoday`, `etotal` |
| Battery | `/api/v1/inverter/battery/{sn}/realtime?sn={sn}&lan=en` | `soc`, `power`, `capacity`, `temp`, `voltage`, `chargeVolt`, `maxChargeCurrentLimit`, `maxDischargeCurrentLimit`, `etodayChg`, `etodayDischg` |
| Grid | `/api/v1/inverter/grid/{sn}/realtime?sn={sn}` | `pac`, `etodayFrom`, `etodayTo` |
| Load | `/api/v1/inverter/load/{sn}/realtime?sn={sn}` | `totalPower`, `dailyUsed` |
| PV input | `/api/v1/inverter/{sn}/realtime/input` | `pac`, `etoday` |
| Settings | `/api/v1/common/setting/{sn}/read` | the full settings object |

### Write

One endpoint: `POST /api/v1/common/setting/{sn}/set`, body is the settings object,
response `{code, msg: "Success", success: true}`.

Unlike DEYE this is **synchronous at the API layer** — there is no `orderId` to poll — so
the DEYE component's `poll_order()` and `_reconcile_control()` machinery collapses into a
straight response check. It is *not* synchronous at the hardware layer; see "Cloud to
dongle latency" under Error handling.

**Strategy: read-modify-write the full object.** `GET .../read`, mutate only the fields
this design owns, `POST .../set` with everything else preserved verbatim. Nothing
documents whether Sunsynk treats an absent field as "leave unchanged" or "reset to
default", and the second interpretation would silently wipe installer settings such as
`batteryShutDownCap`. The extra read costs one call per *changed* plan, not per cycle.

## Architecture

### Component

`SunsynkAPI(ComponentBase, OAuthMixin)` in `apps/predbat/sunsynk.py`, registered in
`components.py` under key `sunsynk` with `event_filter: "predbat_sunsynk_"` and
`phase: 1`. `required_or: ["username", "key"]` gates activation on having at least one
usable auth path, matching the DEYE registration.

`run(seconds, first)` loop:

1. On `first`, restore cached device data from Storage.
2. Discover inverters (`/inverters`, paged), refreshed on the static tier.
3. Poll the settings object on the config tier; telemetry on the live tier.
4. Read the control entities into local state; apply on write-button or payload diff.
5. `publish_data()` (sensors) and `publish_schedule_settings_ha()` (control entities).
6. On `first and automatic`, `automatic_config()`.

### Files

| File | Change |
|---|---|
| `apps/predbat/sunsynk.py` | **New** — the component |
| `apps/predbat/sunsynk_const.py` | **New** — endpoints, field maps, TTLs, RSA helper |
| `apps/predbat/components.py` | Register `sunsynk` |
| `apps/predbat/config.py` | `sunsynk_*` keys in `APPS_SCHEMA`; `INVERTER_DEF["SunsynkCloud"]` |
| `apps/predbat/tests/test_sunsynk_auth.py` | **New** |
| `apps/predbat/tests/test_sunsynk_api.py` | **New** |
| `apps/predbat/tests/test_sunsynk_control.py` | **New** |
| `apps/predbat/tests/test_sunsynk_publish.py` | **New** |
| `apps/predbat/tests/test_sunsynk_config.py` | **New** |
| `apps/predbat/tests/test_sunsynk_const.py` | **New** |
| `apps/predbat/tests/test_sunsynk_storage.py` | **New** |
| `apps/predbat/unit_test.py` | Register the seven suites in `TEST_REGISTRY` |
| `docs/components.md` | New "Sunsynk Cloud API (sunsynk)" section |
| `docs/inverter-setup.md` | Sunsynk Cloud setup walkthrough |
| `docs/apps-yaml.md` | Config reference |
| `.cspell/custom-dictionary-workspace.txt` | `inteless`, `elinter`, `solarsynk`, `synkctl`, `Vallery` |

### Inverter definition

`INVERTER_DEF["SunsynkCloud"]` is a copy of `INVERTER_DEF["DeyeCloud"]` — the capability
flags are identical because the underlying registers are:

```text
output_charge_control:    "power"
charge_control_immediate: False
has_charge_enable_time:   True
has_discharge_enable_time: True
has_target_soc:           True
has_reserve_soc:          True
has_timed_pause:          False
charge_time_format:       "HH:MM:SS"
charge_time_entity_is_option: True
soc_units:                "%"
num_load_entities:        1
time_button_press:        True
has_time_window:          False
support_charge_freeze:    True
support_discharge_freeze: True
has_idle_time:            False
can_span_midnight:        False
charge_discharge_with_rate: False
target_soc_used_for_discharge: True
```

## Control model

### Slot derivation

Sunsynk exposes six time-of-use slots as flat indexed fields. Slots are sequential
intervals — each runs from its own start time until the next slot's start — so all six
start times must be distinct and ascending, the same constraint DEYE has.

Since confirmed by Sunsynk's own documentation, "Avoiding conflicts in the System Mode
timer": the six slots are sequential chronological intervals, each running until the next
begins, and "Timers MUST be set chronologically from Timer 1 to Timer 6". Timer 6 is the
only one permitted to roll over midnight and continue until Timer 1 restarts.

The same article settles a question the API alone could not: a slot is an interval
regardless of its grid-charge flag — the documented factory default runs all six timers as
ranges with Grid Charge ticked on only two. That is what makes the filler padding work, since
a filler slot with grid charge off still terminates the charge window before it. Had the
opposite been true (only grid-charge-enabled slots being active), a Predbat charge window
would never have handed over and would have grid-charged for 24 hours.

An inverter can nonetheless be found sitting with all six slots at 00:00, which the API
stores happily. That is an unconfigured default rather than a valid programme, so no
conclusion about legal schedules follows from it.

The component derives six ordered slots from Predbat's charge and export windows using
the same segment-boundary approach as `deye.py`'s `build_tou_slots()`: start from a
baseline self-use segment at `00:00`, add a segment at each enabled window's start and
another at its end returning to self-use, sort, then pad with self-use slots at filler
times not already used until exactly six remain, trimming to the earliest six.

| Per-slot concept | Sunsynk field (`N` = 1..6) | Wire type |
|---|---|---|
| Slot start time | `sellTime{N}` | `"HH:MM"` string |
| Slot power limit | `sellTime{N}Pac` | watts, string |
| Slot target SOC | `cap{N}` | percent, string |
| Grid charge enabled in slot | `time{N}on` | JSON boolean |
| Generator charge in slot | `genTime{N}on` | preserved from read |
| Voltage-mode target | `sellTime{N}Volt` | preserved from read |

### Intent mapping

| Predbat behaviour | `sysWorkMode` | `solarSell` | `cap{N}` | `time{N}on` |
|---|---|---|---|---|
| Charge | zero-export-to-load | off | target SOC | true |
| Freeze charge | zero-export-to-load | off | reserve | true |
| Hold charge | zero-export-to-load | off | reserve | false |
| Export | selling-first | on | target SOC | false |
| Freeze export | selling-first | on | 99 | false |
| Idle / self-use | zero-export-to-load | off | reserve | false |

Plus, on every write: `peakAndVallery: "1"` (the time-of-use master enable),
`mondayOn` through `sundayOn` all true, and `sn` echoed back.

Sunsynk has a single global work mode, so — exactly as in `deye.py`'s `_active_state()` —
the top-level mode and flags follow the window active **right now**, not a static
export-first precedence. Without that rule an export window enabled elsewhere in the day
would pin the mode to selling-first and block the charge window's grid charging. The six
slots still encode every window.

A final guard clamps any slot SOC below the inverter's own floor (`batteryLowCap` from
the settings object) up to that floor, logged once per serial — the same protection
`deye.py` applies, and for the same reason: Predbat's control entities start at 0 and
only reach their real values once it has written them.

### Sensor mapping (`sunsynk_automatic: true`)

| Predbat arg | Source |
|---|---|
| `soc_percent` | battery `soc` |
| `battery_power` | battery `power` |
| `grid_power` | grid `pac` |
| `load_power` | load `totalPower` |
| `pv_power` | input `pac` |
| `battery_temperature` | battery `temp` |
| `pv_today` | input `etoday` |
| `import_today` | grid `etodayFrom` |
| `export_today` | grid `etodayTo` |
| `load_today` | load `dailyUsed` |
| `battery_charge_today` | battery `etodayChg` |
| `battery_discharge_today` | battery `etodayDischg` |
| `inverter_limit` | detail `ratePower` |
| `battery_min_soc` | settings `batteryLowCap` |
| `soc_max` | derived, see below |
| `battery_rate_max` | derived, see below |

Battery `capacity` is reported in amp-hours, so `soc_max` needs a pack voltage to become
kilowatt-hours, and `battery_rate_max` needs one to turn `maxChargeCurrentLimit` (amps)
into watts. The component uses the same derivation `deye.py` already does — infer the
nominal pack voltage from the BMS charge target (`chargeVolt`) over LiFePO4 volts per
cell — with `sunsynk_battery_nominal_voltage` as an explicit override for packs that do
not report one.

Following the DEYE precedent, an arg is only mapped when *every* discovered inverter
reports the underlying value; otherwise the component logs a warning naming the arg and
leaves it for `apps.yaml`. An arg pointing at a sensor that is never published is worse
than an absent arg.

## Refresh tiers

`ComponentBase` ticks `run()` every 60 seconds. Sunsynk data only updates at the dongle's
upload interval (typically 60 seconds) and the API has no documented rate limit but is
known to throttle, so each tier is the maximum age its cache may reach before re-polling:

| Tier | Interval | Contents |
|---|---|---|
| Static | 8 hours | inverter list, detail and `ratePower` |
| Config | 15 minutes | the settings object |
| Live | 5 minutes | battery, grid, load and input telemetry |

The live tier is deliberately slower than DEYE's 1-minute equivalent: Sunsynk needs four
separate endpoint calls per inverter per poll where DEYE needs one, so a 1-minute tier on
a three-inverter account would be 720 calls an hour against an undocumented limit. Five
minutes matches Predbat's own replan cadence, so no plan ever acts on data more than one
cycle stale.

Tier clocks are seeded from `storage.age()` at startup so the cadence survives a process
restart rather than restarting with it — the DEYE storage pattern.

### Caching

One Storage file per tier under the `sunsynk` module, so each gets an independent
`storage.age()` clock:

| File | Contents |
|---|---|
| `static` | inverter serials, detail |
| `config` | the last-read settings object |
| `ratings` | derived capacity, pack voltage, rated power |
| `control` | last-applied payload for change detection |

Telemetry is not cached: the live tier polls every five minutes, Home Assistant already
retains the last published value of every entity, and `publish_data()` only writes a
sensor when it has a value — so a failed poll leaves the previous reading in place rather
than overwriting it.

The `control` cache is restored only within a bounded window
(`SUNSYNK_RESTORE_MAX_CONTROL`, 15 minutes). It is a change-detection cache with no
read-back, so restoring it asserts the inverter still holds what Predbat last wrote. If
something changed it externally while Predbat was down, that assertion is false, the next
write is wrongly skipped, and the battery silently diverges from the plan. A redundant
write is cheap; a skipped one is not.

## Error handling

### Cloud to dongle latency

A `POST .../set` returning `Success` means the *cloud* accepted the settings, not that the
inverter applied them. The dongle picks changes up on its next poll, typically one to five
minutes later. Predbat replans every five minutes, so a write may still be in flight when
the next cycle starts.

Mitigations: writes are diff-gated against the `control` cache so an unchanged plan never
rewrites; and the read-back comparison tolerates a configurable number of cycles of
divergence (`SUNSYNK_SETTLE_POLLS`) before warning, so normal latency is not reported as
a failure.

### Read-modify-write races

Someone using the Sunsynk phone app while Predbat is running can have their change
overwritten, and vice versa. This is unavoidable with a single whole-object write
endpoint. The component re-reads immediately before each write to keep the window as
small as possible, and logs at info level whenever the freshly-read object differs from
the cached one in a field Predbat does not own.

### Rate limiting and transport failures

Retry with exponential backoff on transport errors, non-200 status, and `success: false`
bodies. Auth failures are detected from the body as well as the status code — the DEYE
component learned that these APIs answer an expired token with HTTP 200 carrying a
body-level failure, and Sunsynk's uniform `{code, msg, success}` envelope makes the same
handling necessary here. A body-level auth marker triggers one token refresh and one
retry, then gives up for that cycle.

### Field serialisation

solarsynkv3 carries a `ReplaceTRUE()` helper that rewrites the string `"true"` to a bare
`true` in its payload before posting. That is strong evidence the API requires real JSON
booleans for the `time{N}on` and day-of-week flags while numeric fields stay strings.
Serialisation is therefore declared per field in `sunsynk_const.py` rather than guessed at
each call site, so a correction is a one-line change in one file.

## Testing

Seven test modules under `apps/predbat/tests/`, all running against `MockBase` with no
network access, mirroring the coverage DEYE has:

| Module | Covers |
|---|---|
| `test_sunsynk_auth` | RSA PKCS#1 v1.5 against a fixed key with a known ciphertext; DER `SubjectPublicKeyInfo` parsing; nonce and sign derivation for both steps; all three methods (`password`, `password_legacy`, `oauth`) producing an equivalent authenticated client; that a failing `password` login never emits the plaintext password on any path; token refresh; expiry handling |
| `test_sunsynk_api` | Endpoint construction per region; discovery pagination; `success: false` handling; body-level auth-error detection; retry and backoff |
| `test_sunsynk_control` | Every row of the intent-mapping table; six-slot derivation including midnight-wrapping windows; distinct ascending start times; filler padding and trimming; SOC floor clamping; read-modify-write preserving untouched keys; diff gating |
| `test_sunsynk_publish` | Sensor names and values; amp-hour to kilowatt-hour derivation; pack-voltage inference and the explicit override; missing-field tolerance |
| `test_sunsynk_config` | `automatic_config()` arg mapping; multi-inverter; the "only map when every inverter reports it" rule; `sunsynk_inverter_sn` filtering |
| `test_sunsynk_const` | Field-map integrity — all six slots present in every map, no typos, serialisation declared for every owned field |
| `test_sunsynk_storage` | Cache save, restore and age per tier; the bounded control restore; restart behaviour |

The RSA test is the one with a hand-rolled cryptographic primitive behind it, so it
verifies against a fixed key pair with a pre-computed expected ciphertext, and separately
round-trips through a decryption implemented in the test itself — padding structure
included, since PKCS#1 v1.5 encryption is randomised and a naive equality check would be
either flaky or vacuous.

## Field verification

Nobody on the project has a Sunsynk account, so the wire-format details in this design are
inferred from two third-party clients rather than read from documentation. The build is
therefore explicitly defensive:

- Every inferred constant is marked `# VERIFY@SPIKE` in `sunsynk_const.py`.
- `api_debug` defaults to `True`, tracing every request and response body with credentials
  and tokens redacted, so a tester can paste raw traffic into an issue.
- `sunsynk.py` carries a standalone `argparse` CLI entry point (the `fox.py` pattern) that
  logs in, dumps the settings object and telemetry, and optionally performs one round-trip
  write — usable without running Predbat at all. `--auth-method` selects the login flow so
  a tester can establish which one their region serves in a single command, before any
  `apps.yaml` is written.
- `sunsynk_control_enable` defaults to `false`. Monitoring works immediately; writes
  require an explicit opt-in until the format is confirmed.

Per "Defaulting to DEYE semantics", every unknown below is an **encoding** question. The
semantic questions — how many slots, which modes exist, what each mode does — are
answered by DEYE and are not on this list.

| # | Unknown | Current assumption | Cost if wrong |
|---|---|---|---|
| 1 | `sysWorkMode` enum values | `0` selling-first, `1` zero-export-to-load, `2` zero-export-to-CT — DEYE's mode *order*, which is an encoding claim | High: silently swaps export for charge |
| 2 | String versus boolean per field, especially `time{N}on`, `solarSell`, `peakAndVallery`, day flags | Booleans bare, numerics quoted, per solarsynkv3's `ReplaceTRUE()` | Medium: write rejected, or flag ignored |
| 3 | Whether `/set` accepts a partial body | Not assumed — full read-modify-write | None; a confirmed yes is a later simplification |
| 4 | `battery power` sign convention | DEYE's convention | High: charge and discharge readings inverted |
| 5 | Whether `capacity` is per-battery or pack total | Pack total | Medium: `soc_max` wrong by an integer factor |
| 6 | Which auth flow each region serves | `api.sunsynk.net` on RSA, `pv.inteless.com` unknown | Low: both are implemented, so this is a documentation fix, not a code one |

Unknowns 1 and 4 are the two that can drive the battery the wrong way, so both are
verified first, before `sunsynk_control_enable` is documented as safe to turn on. The
fail-closed rule means a parse failure on either leaves the inverter in self-use rather
than acting on a guess.

Unknown 6 is why `password_legacy` earns its place. Login is where this integration is
most likely to fail on first contact, and having both flows available turns an
unresolvable "it will not connect" report into a one-line config change that isolates
which half is broken.

## Out of scope

- Generator, smart-load, EV-charger and plant-management control surfaces.
- Historical or daily-curve endpoints (`/output/day`, `/flow`) — Predbat sources history
  from Home Assistant.
- Automatic fallback from the RSA login to the plaintext one. Both are implemented; the
  choice between them is the user's.
- Any change to `deye.py`. Should a shared time-of-use derivation module prove worthwhile
  once both components are in production, extracting one is a separate piece of work with
  both test suites as its safety net.
