# Discovery Catalogue v1 (Observe Only) — Design

Date: 2026-09-10. Status: agreed with maintainer.
Supersedes the v1 scope of `2026-09-06-discovery-coordinator-design.md`, which remains the
design record for the slot **allocator** deferred out of this version.

## Purpose

Components describe what they discovered — inverters, cars, car chargers, meters, forecast
providers and flexibility programmes — into a single assembled catalogue. The catalogue is
published into the debug YAML dump (and later a web viewer) so real user topologies can be
read before any allocation rules are designed.

**v1 takes no action on the catalogue.** Nothing is allocated, nothing is written to
`self.args`, no existing behaviour changes. Every component keeps auto-configuring exactly
as it does today.

## Why observe first

The prior design allocated inverter slots, car indices and sensor indices from these same
reports. That design stands, but it makes assumptions about real installations that nobody
can currently check: how often two inverter components are enabled together, how often a
serial is claimed twice, how often Octopus and Kraken both wire the import rate, whether
sensor-only PV devices are common enough to justify a separate index pool. The catalogue
answers those questions from user bug reports at a fraction of the risk, because a purely
additive observer cannot break a working installation.

## Non-goals for v1

- No slot, index or car-number allocation.
- No `Assignment`, no `apply_assignment()`, no changes to any `automatic_config()`.
- No writes to `self.args`, no `set_arg` changes, no `givtcp_rest` slot alignment.
- No persistence or slot stickiness.
- No conflict *resolution* — conflicts are recorded as observations only.

## Architecture

New module `apps/predbat/coordinator.py` holding a thread-safe `Coordinator`:

- `report(component_name, report)` — validates and stores one component's report dict,
  replacing any previous one. Called by a component from its own thread during its first
  successful `run()`.
- `assemble()` — merges every report into one site-level catalogue organised by category,
  tagging each entry with the component that reported it, and adds the observation layer.
- `catalogue()` — the assembled catalogue, **redacted** (see below). This is what every
  consumer gets.
- `catalogue_raw()` — the unredacted form. In-process diagnostics only; never written to a
  file, an entity, an API response or a log line.

`ComponentBase` gains `report_discovery()` (a thin wrapper that finds the coordinator and
calls `report`) and the `build_discovery()` convention. `Components` owns the coordinator
instance. `PredBat.initialize()` calls `assemble()` after `start(phase=1)` returns — the
point at which every phase-1 component has started or timed out — and again after phase 2.

A component that never reports is not absent from the catalogue: `Components` already
distinguishes configured-and-loaded from failed-to-load from not-configured via
`get_active()`, `is_alive()` and `load_error()`, so `assemble()` derives a status for every
component in `COMPONENT_LIST` rather than only for those that spoke.

## Report schema

One dict per component. All sections optional.

```python
{
    "schema_version": 1,
    "automatic": True,          # mirrors the component's own *_automatic flag, recorded not acted on
    "inverters":  [ ... ],
    "chargers":   [ ... ],
    "cars":       [ ... ],
    "meters":     [ ... ],
    "forecasts":  [ ... ],
    "programmes": [ ... ],
}
```

### Record containers

Every record is built from a small fixed set of **typed containers**. A container declares
what kind of value it holds and what happens to those values when the catalogue is shared —
so a component adding a new fact chooses a container, not a schema entry, and needs no
change here. The container's type constraint is what makes that safe: a container that
accepts only numbers cannot leak a name, an email or a credential however it is extended.

| Container | Accepts | On share | Extend freely |
|---|---|---|---|
| `hardware_ids` | Strings ≤64 chars identifying a device: serial, model number | clear | yes |
| `account_ids` | Any scalar identifying a person, supply point or account | pseudonymised | yes |
| `info` | Vendor-supplied descriptive strings ≤64 chars: model, firmware, API version | clear | yes |
| `ratings` | Numbers and booleans only — physical quantities | clear | yes |
| `coverage` | Numbers, booleans, vocabulary tokens | clear | yes |
| `functions`, `capabilities`, `flags`, `effects` | Lists of vocabulary tokens matching `^[a-z0-9_]{1,32}$` | clear | yes |
| `entities` | Entity descriptors, keyed by Predbat standard name | clear | yes |
| Top level | `source`, `device_id`, `kind`, `direction`, and the cross-links | structural | needs a schema change |

The split between `hardware_ids` and `account_ids` is the one classification a developer has
to get right, and it follows the line the codebase already draws: a serial addresses
hardware, so it stays readable and is what makes a bug report diagnosable, while an MPAN or
account number identifies a person and does not. Putting a new identifier in `account_ids`
is always the safe choice — it is pseudonymised whatever it is called.

Only the top-level structural fields are enumerated by this spec, and there are few of them.
Everything descriptive lives in a container and extends without touching the schema.

### The entity descriptor

Every record's `entities` is a dict keyed by **Predbat's standard name** for that sensor or
control — the key is what gives the entity its meaning. Sensors and controls live in the
same map, separated by `access`.

```python
"charge_rate": {
    "entity_id": "number.predbat_givtcp_0_charge_rate",
    "domain": "number",          # number | select | switch | sensor | binary_sensor
    "access": "rw",              # r = read only (sensor), rw = control
    "unit": "W",
    "device_class": "power",
    "min": 0, "max": 3600, "step": 100,
    "options": [...],            # select domain only
    "format": "HH:MM:SS",        # time-valued selects
    "precision": 0
}
```

This vocabulary already exists: `GIVTCP_CONTROLS` in `givtcp.py` is keyed by Predbat
standard name and carries domain, unit, device class, min, max, step and options, and
`Inverter` already reads per-device bounds back off entity attributes
(`get_arg("charge_rate", attribute="max", ...)` for the real battery rate;
`reserve_device_bounds()` for the reserve floor and ceiling). Today that metadata travels
out of band as HA attributes and only three values are ever read back. The catalogue records
it in band.

Boundary: the descriptor states **what values are legal**. `INVERTER_DEF` continues to state
**how a write is performed** (button press, poll-and-verify, time format, percent versus
power control). No write protocol moves into the catalogue.

### Sections

Every record carries `source` (the reporting component name) and `device_id`, a stable
identity of the form `"{component}:{identifier}"`.

The field name is `device_id`, not `device_key`: `is_secret_key()` matches the substring
`_key`, so any field named `*_key` is redacted to `xxx` by the generic masker in
`mask_secret_args()`. Naming the catalogue's primary identity field `device_key` would mean
that one careless call to the generic masker silently destroys the whole document.

**`inverters`** — a device that generates, stores or both.

| Field | Container | Notes |
|---|---|---|
| `serial` | `hardware_ids` | |
| `functions` | vocabulary | `["solar"]`, `["battery"]` or both. A PV-only inverter is how the extra-PV-sensor case is expressed |
| `inverter_type` | top level | `INVERTER_DEF` key, where the component has one |
| `control` | top level | Whether Predbat is permitted to drive it (`control_enable`, monitor-only modes) |
| `composition` | top level | `direct`, `gateway` (fronting `serials`), or `ems` |
| `measures_meter` | cross-link | A meter `device_id`, so double counting is detectable |
| `model`, `firmware` | `info` | |
| `battery_kwh`, `max_charge_w`, `modules`, `dod_percent`, `nominal_voltage` | `ratings` | |
| `capabilities` | vocabulary | Recorded not acted on: `schedule`, `pause_mode`, `pause_slots`, `target_soc`, `discharge_target`, `charge_rate_power`, `charge_rate_percent`, `soh` |
| `entities` | `entities` | Keyed by Predbat standard name |

**`chargers`** — an EVSE. `serial` in `hardware_ids`; `vendor` and `model` in `info`;
`max_power_kw` and `phases` in `ratings`; `serves_cars` cross-links to `cars`. Its
`entities` carry the charger facts: `car_charging_planned`, `car_charging_energy`,
`car_charging_power`, `car_charging_now`.

**`cars`** — a vehicle. Predbat indexes cars, not chargers, so a charger integration that
knows nothing about the vehicle still emits a stub car record with `info.stub`. Ohme can do
better: it holds a vehicle list today that is discarded. `make` and `model` in `info`;
`battery_kwh` and `max_charge_kw` in `ratings`; `charged_by` cross-links to `chargers`. Its
`entities` carry the car facts: `car_charging_soc`, `car_charging_limit`,
`octopus_intelligent_slot`, `octopus_ready_time`, `octopus_charge_limit`.

`car_charging_rate` is the one genuinely negotiated value: the charger reports its limit and
the car reports its own, rather than either pre-flattening the minimum as today.

**`meters`** — a supply point, which may carry a tariff.

| Field | Container | Notes |
|---|---|---|
| `device_id` | derived from `account_ids` | Pseudonymised, since it is built from the MPAN |
| `direction` | top level | `import`, `export` or `gas`. One or more of each is legal |
| `mpan`, `account`, `meter_serial` | `account_ids` | |
| `tariff.tariff_code`, `tariff.product_code` | `info` | Public product identifiers and highly diagnostic |
| `tariff.flags` | vocabulary | `intelligent_go`, `agile`, `six_hour_cap`, `tracker`, `fixed` |
| `tariff.standing_charge_p` | `ratings` | |
| `entities` | `entities` | `metric_octopus_import`, `metric_octopus_export`, `metric_standing_charge` |

Modelling tariffs as a property of a meter rather than as a single site-wide record is what
makes Kraken's separate import and export accounts representable, makes gas a direction
rather than a special case, and gives the Octopus-versus-Kraken contention over
`metric_octopus_import` a place to be observed per meter.

**`forecasts`** — a service producing a time series. No slot, no index. `provider_id` from
`account_ids` (Solcast site ids and plant ids are registry-flagged); `kind` at top level
(`solar`, `carbon`, `temperature`, `load`); `vendor` in `info`; `capacity_kw` in `ratings`;
`horizon_hours`, `resolution_minutes` and `variants` (`pv10`/`pv50`/`pv90`) in `coverage`.

**`programmes`** — a flexibility enrolment that emits events and may constrain Predbat.
`programme_id` from `account_ids`; `kind` at top level (`vpp`, `saving_session`,
`free_electricity`); `meter` cross-linking to the enrolled supply point; `effects` as
vocabulary (`rate_override`, `forces_read_only`).

A seventh section, `loads` (myenergi Eddi, iBoost, Predheat), is named here as the intended
home for controllable loads. Nothing reports it in v1.

## Redaction

The catalogue is designed to be posted to public GitHub issues, so it redacts itself at
source: `catalogue()` returns the redacted form and every consumer uses it. There is no
"remember to mask this" step for a caller to forget.

The existing generic masker is not usable for this and must not be relied on. It matches on
key **name**: `mask_secret_args()` would redact `device_key`, `meter_key` and `provider_key`
because they contain `_key`, while passing `mpan`, `account`, `site_id` and `postcode`
through in the clear, because the registry flags the apps.yaml names (`kraken_mpan`,
`octopus_api_account`, `enphase_site_id`) rather than these field names. It is wrong in both
directions here.

### Four classes

**Never included.** These must not enter the catalogue at all, not even to be masked later.
Credentials of every kind (API keys, tokens, passwords, secrets, OAuth material); any
user-authored free text (Solcast site names, charger nicknames, account labels), which can
contain anything including names and addresses; precise location (latitude, longitude, full
postcode, address); and vehicle registration. No container accepts these, which is what
makes the rule enforceable rather than advisory.

**Pseudonymised.** Everything in `account_ids`, and any value cross-linking to one: MPAN and
MPRN, account identifiers, site, plant and system identifiers, hub serials (myenergi's is
the digest-auth username), and login identifiers such as the Ohme email. Each is replaced by
`"{prefix}:#{token}"` where the token is a short digest of the value with a per-installation
random salt — so `meters[0].device_id` still matches `programmes[0].meter` within a dump and
across successive dumps from the same installation, while the same MPAN from two different
users does not collide and the original cannot be recovered. A plain unsalted digest would
be pointless for a 13-digit MPAN, which is brute-forceable in seconds. The salt is generated
once, stored via the Storage component, and is itself excluded from every dump.

Pseudonymising a value also rewrites it wherever else it appears, entity ids included.
Vendors embed identifiers in entity names, so `sensor.predbat_x_{site_id}_forecast` would
otherwise republish in the clear exactly what the `site_id` field just hid.

**Coarsened.** Location that retains diagnostic value in reduced form: a postcode is
truncated to its outward code (`SW1A`), latitude and longitude are dropped rather than
rounded.

**Clear.** Everything in `hardware_ids`, `info`, `ratings`, `coverage`, the vocabulary lists
and `entities`: device serials, model, firmware, `inverter_type`, `functions`,
`capabilities`, ratings, tariff and product codes, entity ids, units, bounds, options,
counts, statuses and timestamps. This follows the line the codebase already draws —
`secret_config_names()` deliberately does not flag device serials, because they address
hardware rather than authenticate it and they are what makes a bug report diagnosable.

### Enforcement

Safety comes from the containers being typed, not from enumerating every field. A component
adds facts freely; what it cannot do is put a value somewhere its type does not fit. Values
failing a container's constraint are dropped and logged rather than emitted, so the failure
mode of misfiling is missing data, never a leak.

Three guards sit on top, in order of how much work they do:

1. **Type constraints per container**, as tabled above. This is the primary mechanism: a
   numbers-only `ratings`, a `^[a-z0-9_]{1,32}$` vocabulary and a length-capped `info` that
   rejects `@` between them exclude names, emails, addresses, JWTs and free text
   structurally, no matter how the catalogue grows.
2. **A field-name credential guard.** Any field whose name trips `is_secret_key()`'s
   substrings (`_key`, `password`, `secret`, `token`), in any container, is rejected and the
   reporting component logged. A credential reported by mistake never reaches the catalogue.
3. **Value-shape guards** for the one gap the type system cannot close: an MPAN is a
   number, so it satisfies `ratings`. Any clear-container value that looks like an
   identifier rather than a measurement — a long digit run, something containing `@`, a
   coordinate-shaped float pair — is pseudonymised defensively and logged loudly as
   misfiled. This is a safety net for a mistake, not the classification mechanism.

A test asserts every container has a declared class and constraint, and a corpus test walks
a synthetic catalogue seeded with credential-, MPAN-, email- and postcode-shaped values and
asserts none survives into the redacted form.

## Assembled catalogue

```yaml
discovery:
  schema_version: 1
  generated: 2026-09-10T08:30:00Z
  components:
    givtcp:  {status: ok, reported_at: 2026-09-10T08:29:12Z, automatic: true, counts: {inverters: 2}}
    gecloud: {status: timed_out, reported_at: null}
    solis:   {status: not_configured}
    fox:     {status: load_error, error: "No module named 'protobuf'"}
  inverters:  [...]
  chargers:   [...]
  cars:       [...]
  meters:     [...]
  forecasts:  [...]
  programmes: [...]
  observations:
    conflicts:
      - {kind: duplicate_serial, serial: SA2242G123, claimed_by: [givtcp, gecloud]}
      - {kind: contested_key, key: metric_octopus_import, claimed_by: [octopus, kraken]}
      - {kind: contested_car_slots, claimed_by: [ohme, octopus]}
    resulting_config: {num_inverters: 2, num_cars: 1, inverter_type: [GE, GEC]}
```

`observations` is the part that earns v1 its keep. `conflicts` records collisions that today
resolve silently by dict ordering, without changing the outcome — so the frequency of the
two-vendor case in the field becomes measurable. `resulting_config` records what
`self.args` actually ended up as, making every dump a free comparison between what was
discovered and what was configured, which is the evidence the allocator should be designed
from.

## Consumers

`create_debug_yaml()` gains one explicit line, `debug["discovery"] = catalogue`, placed
beside the existing `debug["inverters"]`. It must be explicit: `components` is in
`DEBUG_EXCLUDE_LIST`, so nothing under it is dumped automatically, and an explicit plain
dict also avoids the object-graph walk that the surrounding code documents as dangerous.

The catalogue is also published as attributes on `sensor.predbat_discovery` for the web
viewer to read later. A web page and any predbat.com upload are out of scope for v1.

## v1 reporters

Five components, chosen to exercise every section once:

| Component | Exercises |
|---|---|
| GivTCP | Multiple inverters from one component; entity descriptors with real per-device min/max |
| GE Cloud | Gateway composition, EMS, split PV (`functions: ["solar"]` devices), shared-CT `measures_meter` |
| Octopus | Meters in both directions with tariffs; cars via intelligent devices |
| Ohme | The charger-and-car split, including a non-stub car record |
| Solcast | The `forecasts` section and multi-site providers |

Each is a `build_discovery()` returning a dict plus one `report_discovery()` call in its
existing first-run path. No other line of those components changes.

## Testing

Coordinator unit tests: container type constraints and value dropping, the credential guard,
the value-shape guards, assembly and merging across components, status derivation for
reported, silent, failed and unconfigured components, conflict detection, and JSON/YAML
round-tripping of the assembled catalogue. Redaction tests: each of the four classes,
pseudonym stability within a document and instability across salts, substitution of
pseudonymised values inside entity ids, and the seeded-corpus scan asserting no credential,
MPAN, account id, email or postcode survives into the redacted catalogue. Per-component
tests: each reporter's `build_discovery()` against that module's existing fixtures. Plus a
regression assertion that enabling discovery changes no `self.args` key.

## Deferred

Slot and index allocation, `Assignment` hand-off, persistence and stickiness, and the
migration of `automatic_config()` to assigned indices are all designed in
`2026-09-06-discovery-coordinator-design.md` and unchanged by this document. They become
implementable — and their rules become evidence-based — once catalogues from real
installations can be read.
