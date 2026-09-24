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
| D5 | `capabilities` is a dictionary of `INVERTER_DEF` keys to `True`/`False`, shaped like an `INVERTER_DEF` row. |
| D6 | `ratings` are keyed by Predbat setting name, in Predbat's units. A sensor binding for a rating lives in `entities` under the same key. |
| D7 | A site-wide export limit is reported as each inverter's share of it. Predbat sums `export_limit` across inverters, so the shares add back up to the site figure. |
| D8 | `functions` reports what the component believes the device is, whether probed or assumed. There is no marker for an assumed value. |

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

Allowed keys are the fourteen behaviour fields of `INVERTER_DEF`:

`support_charge_freeze`, `support_discharge_freeze`, `support_feedin_first`, `has_timed_pause`,
`has_target_soc`, `has_reserve_soc`, `has_idle_time`, `has_time_window`, `has_charge_enable_time`,
`has_discharge_enable_time`, `can_span_midnight`, `charge_discharge_with_rate`,
`charge_control_immediate`, `target_soc_used_for_discharge`.

Values are `True` or `False`. An unknown key, or a non-bool value, is dropped by the validator. A reporter
may state as many or as few keys as it has established for the device; the rest come from the row.

The previous capability tokens are removed. Their meaning moves as follows:

| Old token | Now |
|---|---|
| `schedule` | `has_time_window`, `has_charge_enable_time`, `has_discharge_enable_time` |
| `target_soc` | `has_target_soc` |
| `discharge_target` | `target_soc_used_for_discharge` plus a `discharge_target_soc` entity |
| `pause_mode`, `pause_slots` | `has_timed_pause` plus the `pause_*` entities |
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

## 2. From record to inverter definition (proposal)

A pure function in the coordinator, `inverter_definition(record, component_defaults)`, returns a new
dict shaped like an `INVERTER_DEF` row. It never writes to `INVERTER_DEF`.

1. Start from a **copy** of `INVERTER_DEF[record["inverter_type"]]`.
2. Apply `record["capabilities"]` over it.
3. Derive the protocol fields from `record["entities"]`:

   | Field | Derived from |
   |---|---|
   | `charge_time_entity_is_option` | `charge_start_time`'s `domain` is `select` |
   | `charge_time_format` | `charge_start_time`'s `format` |
   | `clock_time_format` | `inverter_time`'s `format` |
   | `soc_units` | which state-of-charge setting is bound: `soc_percent` -> `%`, `soc_kw` -> `kWh` |
   | `output_charge_control` | `charge_rate`'s `unit`: W -> `power`, A -> `current`; no entity -> `none` |
   | `current_dp` | the decimal places of `charge_rate`'s `step`, when its unit is A |
   | `time_button_press` | whether a `schedule_write_button` entity is present |

   A field whose source entity is missing, or lacks the needed `format` or `unit`, keeps the row's
   value and is listed in the function's second return value, so a gap is visible rather than guessed.
4. Take `write_and_poll_sleep` from `component_defaults`, a constant on the component class. It is 2 for
   every component-driven type today.

The GE-only fields (`has_rest_api`, `has_mqtt_api`, `has_ge_eco_toggle`, `has_ge_inverter_mode`) keep the
row's value, which is `False` for every component-driven type.

`num_load_entities` is not derived: nothing reads it (`inverter.py` sets no attribute from it), so piece 3
deletes it rather than carrying it forward.

Building a new dict per inverter matters: `inverter.py:381-389` applies apps.yaml's `inverter:` override
by writing into the shared `INVERTER_DEF[type]` row, so with two inverters of one type the last
inverter's override applies to both. The coordinator must not repeat that.

**Proof of completeness.** For every converted reporter, a test builds a record from that component's
fixture and asserts `inverter_definition(record)` equals `INVERTER_DEF[type]` on every field, with no
gaps reported. That parity is what shows the record holds enough to retire the row in piece 3.

## 3. Validation and migration (proposal)

**Schema.** `SCHEMA_VERSION` goes from 1 to 2. A document's shape changes in three ways, all at once:
`capabilities` becomes a dict, ratings are renamed, and descriptors require `access`.

**Validator** (`coordinator.py`):

- `capabilities` leaves `VOCAB_CONTAINERS` and becomes a `CONTAINER_SPEC` entry with a bool cleaner that
  also rejects keys outside the fourteen.
- `_clean_descriptor` requires `access` in `{"rw", "r"}` and exactly one of `entity_id`/`value`, and
  gains `invert` (bool) and `value` (number, bool or short string) cleaners.
- `inverter_record()` accepts the dict-shaped `capabilities`.
- `capabilities` is a clear container; nothing in it is identifying.

**Reporters.** The seven existing reporters convert in one piece, because the old capability tokens and
rating names are removed. Each reporter builds its `entities` from the same table its
`automatic_config()` uses, so the two cannot disagree. Where the setting names come from a list in
`automatic_config()` (Sunsynk's, for instance), that list moves to a module constant read by both - a
change to a control path, made here with the parity test as its guard.

**Consumers.** No code reads `ratings` or `capabilities` by name today: the web discovery page renders
records generically. Its test fixtures (`test_web_discovery.py`) use the old tokens and rating names and
are updated, and a dict-valued `capabilities` must render. `docs/discovery-catalogue.md` and the catalogue design spec
are amended to match.

**Testing.**

- Validator tests for each new rule: bad capability key, non-bool value, descriptor without `access`,
  descriptor with both `entity_id` and `value`, `invert` on a non-bool.
- Per-reporter parity tests (section 2).
- Per-reporter tests that every setting `automatic_config()` binds appears in `entities` with the same
  entity id and the right `access`. The check runs `automatic_config()` against a fixture and compares
  the `set_arg` calls with the record.
- A round trip through `validate_report()` for each reporter.
