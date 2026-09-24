# Discovery inverter record: controls, capabilities and ratings

**Status:** design, awaiting review. **Builds on:** the discovery catalogue design
(`2026-09-10-discovery-catalogue-design.md`), the reporter rollout (`2026-09-20-discovery-reporter-rollout.md`,
PR #5184) and PR #5206 (AlphaESS, Solis, Deye, Sunsynk reporters). Implementation waits for #5206 to merge.

## Why

The long-term aim is to move automatic configuration out of the individual components and into the
coordinator, and then to retire the `INVERTER_DEF` rows for component-driven inverter types. Today each
component carries its own `automatic_config()` that binds 24-50 Predbat settings with `set_arg()`, and the
behaviour of each inverter type is hard-wired in `INVERTER_DEF` (`config.py`).

For that to work, each inverter's discovery record has to hold everything those two places hold. The
record's current vocabulary cannot: `capabilities` mixes control tokens (`schedule`, `target_soc`), a
measurement (`soh`), a protocol version (`rest_v3`) and a setting (`export_limit`); reporters disagree on
whether a token means "probed" or "assumed"; and only GivTCP carries an entity map. The table below is
the vocabulary as it stood when this was written.

| Field | Token | GivTCP | GE Cloud | Fox | AlphaESS | Solis | Deye | Sunsynk |
|---|---|---|---|---|---|---|---|---|
| functions | `solar` | assumed | assumed (probed for PV-only devices) | probed | probed | assumed | assumed | assumed |
| functions | `battery` | assumed | assumed | probed | probed | probed | assumed | assumed |
| capabilities | `schedule` | - | - | probed | assumed | assumed | assumed | assumed |
| capabilities | `target_soc` | - | - | - | assumed | assumed | assumed | assumed |
| capabilities | `discharge_target` | probed | probed | - | assumed | assumed | assumed | assumed |
| capabilities | `charge_rate_power` | - | probed | - | assumed | assumed | assumed | assumed |
| capabilities | `charge_rate_percent` | - | probed | - | - | - | - | - |
| capabilities | `pause_mode`, `pause_slots` | probed | probed | - | - | - | - | - |
| capabilities | `charge_enable` | probed | - | - | - | - | - | - |
| capabilities | `soh` | probed | - | - | - | probed | - | - |
| capabilities | `rest_v3` | probed | - | - | - | - | - | - |
| capabilities | `export_limit` | - | - | probed | - | - | - | always, once the rating is known |

Sunsynk's `export_limit` token is set from `export_limit()`, which returns the lower of the configured
export cap and the inverter rating, so it conflates two different limits.

## Scope

This work is split into three pieces, each with its own spec and plan:

1. **This spec - the record's vocabulary**, the validator, a pure function that turns a record into an
   inverter definition, and the conversion of the seven existing reporters (GivTCP, GE Cloud, Fox,
   AlphaESS, Solis, Deye, Sunsynk). Observe-only: nothing Predbat does changes.
2. **The remaining inverter reporters** (SolaX, Sigenergy, Enphase, Teslemetry, Gateway) in the new
   vocabulary - plans 6 and 7 of the rollout.
3. **The coordinator performs automatic configuration** from the reports, each component's
   `automatic_config()` is removed, and the `INVERTER_DEF` rows for component-driven types are retired.

This ends the rollout's "observe-only, entity maps deferred" rule (decision of 21 Sep 2026): entity maps
are now the core of the work rather than a later project. Pieces 1 and 2 are still observe-only; piece 3
is the first to change behaviour.

## Decisions (24 Sep 2026)

| # | Decision |
|---|---|
| D1 | `export_limit` is the configured maximum grid export power; `inverter_limit` is the maximum power the inverter can invert; `import_limit` is the configured maximum grid import power. All three are distinct ratings. |
| D2 | Controls and sensors share one map, `entities`, keyed by Predbat setting name. Every entry is marked `access: rw` (Predbat writes it) or `access: r` (Predbat only reads it). |
| D3 | `capabilities` holds per-device behaviour that overrides the device's `INVERTER_DEF` row. The goal is to retire `INVERTER_DEF` for component-driven types. |
| D4 | Protocol detail (time formats, units, option-vs-string time entities) is derived from the entity map, not stated separately. |
| D5 | `capabilities` is a dictionary of `INVERTER_DEF` keys to `True`/`False`, shaped like an `INVERTER_DEF` row. It holds only the seven keys that describe behaviour; the `has_*` keys that describe whether an entity exists are derived (section 2). |
| D6 | `ratings` are keyed by Predbat setting name, in Predbat's units. A sensor binding for a rating lives in `entities` under the same key. |
| D7 | A site-wide export limit is reported as each inverter's share of it. Predbat sums `export_limit` across inverters, so the shares add back up to the site figure. |
| D8 | `functions` reports what the component believes the device is, whether probed or assumed. There is no marker for an assumed value. |
| D9 | SolisCloud does not write the reserve (it often won't change); it presents the battery minimum SoC instead. Its record reports `battery_min_soc` and no `reserve` (section 3). |

## 1. The inverter record

| Field | Shape | Meaning |
|---|---|---|
| `functions` | list of tokens (unchanged) | What the box physically is: `solar`, `battery`. |
| `capabilities` | dict of `INVERTER_DEF` key -> bool | Behaviour this device has. A key left out keeps its `INVERTER_DEF` row value. |
| `ratings` | dict of name -> number | Fixed figures, keyed by Predbat setting name where one exists. |
| `entities` | dict of Predbat setting name -> descriptor | Every setting automatic configuration binds for this device. |
| `flags` | list of tokens | Other held facts: topology and protocol variants. |

`inverter_type`, `control`, `composition`, `serials`, `hardware_ids`, `account_ids`, `info` and
`coverage` are unchanged.

### 1.1 `capabilities`

Allowed keys are the seven `INVERTER_DEF` fields that describe behaviour rather than the presence of an
entity:

`support_charge_freeze`, `support_discharge_freeze`, `support_feedin_first`, `can_span_midnight`,
`charge_discharge_with_rate`, `charge_control_immediate`, `target_soc_used_for_discharge`.

Values are `True` or `False`. An unknown key, or a non-bool value, is dropped by the validator. While
reporters migrate, a key left out keeps the row's value; a converted reporter states all seven (section 2's
completeness test enforces it).

The other `has_*` fields are not capabilities: each says whether the inverter has a particular entity, and
is derived from `entities` (section 2).

The previous capability tokens are removed. Their meaning moves as follows:

| Old token | Now |
|---|---|
| `schedule` | the `scheduled_charge_enable` / `scheduled_discharge_enable` and `*_start_time` / `*_end_time` entities |
| `target_soc` | a `charge_limit` entity |
| `discharge_target` | `target_soc_used_for_discharge` plus a `discharge_target_soc` entity |
| `pause_mode`, `pause_slots` | the `pause_*` entities |
| `charge_rate_power`, `charge_rate_percent` | derived from the `charge_rate` entity (section 2) |
| `charge_enable` | a `charge_limit_enable` entity |
| `export_limit` | the `export_limit` rating (D1) |
| `soh` | flag `reports_soh` |
| `rest_v3` | flag `rest_v3` |

### 1.2 `ratings`

Keyed by Predbat setting name, in the units that setting takes:

| Key | Unit | Was |
|---|---|---|
| `inverter_limit` | W | `inverter_w` |
| `export_limit` | W | capability token `export_limit` |
| `import_limit` | W | (new) |
| `battery_rate_max` | W | `max_charge_w` |
| `soc_max` | kWh | `battery_kwh` |
| `battery_min_soc` | % | (new) |

Each value is the configured or rated figure as the device reports it. In particular Sunsynk reports
the raw `pvMaxLimit` as `export_limit` and `importPower` as `import_limit`, not the lower of either and the
inverter rating. The one exception is a limit that applies to the whole site: GE Cloud reports each
inverter's equal share of the site figure (D7), the same value `publish_site_export_limit()` already
publishes.

Descriptive vendor figures that are not a Predbat setting (`battery_capacity_ah`, `battery_pack_count`,
`battery_capacity_entries`, `battery_capacity_serials`, `pv_w`) stay in `ratings` under their current
names. The coordinator never binds them to a setting.

### 1.3 `entities`

One descriptor per Predbat setting that automatic configuration binds for this device. The fields:

| Field | Required | Notes |
|---|---|---|
| `access` | yes | `rw` or `r`. A descriptor without it is dropped. |
| `entity_id` | one of these two | The HA entity bound to the setting. |
| `value` | one of these two | A fixed stand-in where no entity exists (Fox's `pv_power: 0` for a device with no PV; SolaX's constant `battery_min_soc`). Number, bool or short string. |
| `domain`, `unit`, `device_class`, `min`, `max`, `step`, `precision`, `options`, `format` | no | As today. |
| `invert` | no | `true` when the entity's sign is the opposite of Predbat's convention. Replaces the `grid_power_invert`, `battery_power_invert` and `load_power_invert` settings. |

A descriptor with both `entity_id` and `value`, or neither, is dropped.

A rating with a sensor appears twice: the number in `ratings`, the binding in `entities` with
`access: r`. The coordinator binds the entity where one exists and the rating's number otherwise.

Where a component resolves one of several candidate entities at configuration time - GE Cloud's
`build_entities()` tries several register names per setting - the descriptor records the entity that
was actually chosen.

### 1.4 `flags`

Unchanged in shape. New tokens: `reports_soh` (the device reports battery state of health; Solis drives
only inverters that do) and `rest_v3` (moved from `capabilities`). Existing: `third_party_gen`,
`ev_charger`, `tou_v2`.

### 1.5 Values that stay out of the record

- `num_inverters` - the coordinator counts driven records.
- Settings that reset another integration's leftovers (`givtcp_rest`, `ge_cloud_data`, `ge_cloud_direct`,
  and GE Cloud and the gateway setting `pause_mode`/`discharge_target_soc`/`charge_rate_percent` to
  `None`). In piece 3 the coordinator owns the whole inverter configuration and clears whatever no
  report binds.
- `num_cars` and `car_charging_*` - these belong to the `chargers` and `cars` sections.

## 2. From record to inverter definition

A pure function in `coordinator.py`, `inverter_definition(record, write_and_poll_sleep, base=None)`,
returns `(definition, gaps)`: a new dict shaped like an `INVERTER_DEF` row, and a list of the fields it
could not work out. It never writes to `INVERTER_DEF`.

The definition is built from four sources:

1. **Behaviour** - the seven `capabilities` keys (section 1.1).
2. **Entity presence** - each flag is true when `entities` binds the setting to a real entity with
   `access: rw`. All six are settings Predbat writes, so an `access: r` entry does not count. A `value`
   stand-in counts as absent, because Predbat treats that setting as having no entity.

   | Field | True when `entities` has an `rw` entity for |
   |---|---|
   | `has_charge_enable_time` | `scheduled_charge_enable` |
   | `has_discharge_enable_time` | `scheduled_discharge_enable` |
   | `has_reserve_soc` | `reserve` |
   | `has_target_soc` | `charge_limit` |
   | `has_idle_time` | `idle_start_time` and `idle_end_time` |
   | `has_timed_pause` | `pause_mode` |

   This matches what `inverter.py` already does: lines 612-655 create a dummy entity for exactly these
   settings when the flag is False, and lines 438-449 turn `has_timed_pause` off at runtime when no
   `pause_mode` entity exists.
3. **Protocol detail** - read from the entity descriptors:

   | Field | Derived from |
   |---|---|
   | `charge_time_entity_is_option` | `charge_start_time`'s `domain` is `select` |
   | `charge_time_format` | `charge_start_time`'s `format` |
   | `clock_time_format` | `inverter_time`'s `format` |
   | `soc_units` | which state-of-charge setting is bound: `soc_percent` -> `%`, `soc_kw` -> `kWh` |
   | `output_charge_control` | `charge_rate`'s `unit`: W -> `power`, A -> `current`; no entity -> `none` |
   | `current_dp` | the decimal places of `charge_rate`'s `step`, when its unit is A |
   | `time_button_press` | whether a `schedule_write_button` entity is present |

4. **The component** - `write_and_poll_sleep`, a constant on the component class. It is 2 for every
   component-driven type today.

The GE-only fields (`has_rest_api`, `has_mqtt_api`, `has_ge_eco_toggle`, `has_ge_inverter_mode`) default to
`False`, their value for every component-driven type.

A protocol field whose source setting is not bound at all is **not applicable**, not a gap: for example
`clock_time_format` only matters when `inverter_time` is bound, and of the seven reporters only GE Cloud
binds it. A not-applicable field takes a fixed default, is not listed in `gaps`, and is skipped by the
completeness test.

A field the function cannot work out - a missing capability key, or a bound source entity lacking the
`format` or `unit` it needs - is listed in `gaps`. With `base` given (a copy of the `INVERTER_DEF` row, used while
reporters migrate) the field takes the base's value; with no `base` it is left out.

Two fields are neither derived nor carried: `has_time_window` and `num_load_entities`. Nothing reads either
(`inverter.py` sets no attribute from them), so piece 3 deletes them.

Building a new dict per inverter matters: `inverter.py:381-389` applies apps.yaml's `inverter:` override
by writing into the shared `INVERTER_DEF[type]` row, so with two inverters of one type the last
inverter's override applies to both. The coordinator must not repeat that.

**Proof of completeness.** For every converted reporter, a test builds a record from that component's
fixture and calls `inverter_definition(record, ...)` with **no** `base`. It asserts that `gaps` is empty and
that the definition equals `INVERTER_DEF[type]` on every applicable field except the two dead ones. Building without
the row matters: with the row as a base, any field the record left out would silently inherit the right
answer and the test would pass without proving anything. Passing is what shows the record holds enough to
retire the row in piece 3.

## 3. Validation and migration

**Schema.** `SCHEMA_VERSION` goes from 1 to 2: `capabilities` becomes a dict, ratings are renamed, and
descriptors require `access`. Reports are held in memory only (the pseudonymisation salt is the only
thing the coordinator persists), so the bump affects debug dumps and their readers, nothing stored.

**Validator** (`coordinator.py`):

- `capabilities` leaves `VOCAB_CONTAINERS` and becomes a `CONTAINER_SPEC` entry: a clear container whose
  cleaner keeps only `True`/`False` values for the seven keys of section 1.1. Only inverter reporters set
  `capabilities`, so no other section is affected.
- `_clean_descriptor` requires `access` in `{"rw", "r"}` and exactly one of `entity_id`/`value`. Every
  component that emits `entities` today (GivTCP, Octopus, Ohme, Solcast) already sets `access`.
- `value` must be a number, a bool, or a string passing the same guard as `info` strings. `entities` is
  published unredacted in dumps users post to public issues, so it must not admit free text.
- `invert` must be a bool.
- `inverter_record()` accepts the dict-shaped `capabilities`.

**Reporters.** Each component's `automatic_config()` is left untouched in this piece. Piece 3 deletes it,
so extracting its setting lists into shared constants now would change a control path for code about to
be removed. Agreement between the two is proven by a test instead (below).

The seven existing reporters convert in one PR, because the old capability tokens and rating names are
removed. The plan's tasks are the validator and `inverter_definition()` first, then one reporter per task,
simplest first: Sunsynk, Deye, AlphaESS, Fox, Solis, GE Cloud, GivTCP. Between those tasks an unconverted
reporter's list-shaped `capabilities` is dropped by the validator; that state exists only inside the
branch.

**Solis's `reserve`.** SolisCloud deliberately does not write the reserve - it often won't change - and
presents the battery minimum SoC instead, which is why its row has `has_reserve_soc: False`.
`solis.py:1715-1716` binds both `reserve` and `battery_min_soc` to the same `over_discharge_soc` entity,
and `inverter.py:622-624` replaces the `reserve` binding with a dummy. So the Solis record reports
`battery_min_soc` (an `access: r` entity plus a rating) and no `reserve`; derived `has_reserve_soc` is
then False, matching the row. Piece 3 removes the unused `reserve` binding.

**Consumers.** No code reads `ratings` or `capabilities` by name today: the web discovery page renders
records generically. Its test fixtures (`test_web_discovery.py`) use the old tokens and rating names and
are updated, and a dict-valued `capabilities` must render. `docs/discovery-catalogue.md` and the catalogue
design spec are amended to match.

**Testing.**

- **Validator** - one test per new rule: an unknown capability key, a non-bool capability value, a
  descriptor without `access`, one with both `entity_id` and `value`, a free-text `value`, a non-bool
  `invert`.
- **Completeness** (section 2) - per reporter, `inverter_definition()` with no `base` gives no gaps and
  matches the `INVERTER_DEF` row on every applicable field.
- **Agreement** - per reporter, run `automatic_config()` against a single-device fixture, capture every
  setting it sets, and check each appears in the record: an entity id matching the descriptor, a literal
  matching a `value` entry or a rating, and each `*_invert` setting matching the descriptor's `invert`.
  Two kinds of setting are skipped: the section 1.5 exclusions, and any setting whose presence flag is
  False in the type's row, since `inverter.py` replaces those bindings with a dummy (Solis's `reserve` is
  the only case today).
- **Round trip** - each reporter's report survives `validate_report()` unchanged.
