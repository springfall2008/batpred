# Discovery performs automatic inverter configuration: proof of concept with GE Cloud

Status: approved by Trefor, 14 Nov 2026.

This is the first slice of "piece 3" of the discovery work (see
`2026-09-24-discovery-inverter-record-vocabulary-design.md`, Scope). Pieces 1 and 2 made every inverter
component report a record that describes its devices. Nothing reads those records to configure Predbat
yet: each component still writes `apps.yaml` args itself in its own `automatic_config()`.

## Why

Each component's `automatic_config()` writes whole per-inverter lists from index 0 and sets
`num_inverters` to its own count. Two sources of inverters therefore cannot share an install, and a
manually configured inverter is overwritten. The records were built so one place - the discovery
coordinator - can own inverter configuration for every source.

The longer direction (Trefor, 14 Nov 2026): manually configured inverters will later get their own
component that reports discovery too, and the reference for "what is each inverter bound to" will be a
bindings structure held by discovery rather than `apps.yaml`. `inverter.py` will later read those
bindings directly. This piece builds the bindings structure and replays it into args, so the later step
only changes the reader.

## Goals

1. The coordinator builds a **bindings** structure: one entry per inverter slot, manual and discovered.
2. The coordinator **replays** the bindings into `self.args`, so `inverter.py` and everything else keep
   reading args unchanged.
3. A component **opts in** from its `automatic_config()`. Only opted-in components are configured.
4. GE Cloud is the first and only component converted.
5. GE Cloud's `async_automatic_config()` stops writing inverter bindings and becomes a **checker**: it
   works out what it would have set and compares that with what the coordinator set.
6. Manual inverters keep their slots. A discovered inverter takes a free slot, or takes over the manual
   slot that names the same serial.
7. The assumed inverter type is retired. An inverter slot with no `inverter_type` is dead, and Predbat
   errors out rather than assuming GivEnergy.

## Non-goals

- Converting any other component. Their `automatic_config()` is untouched.
- Deleting GE Cloud's `async_automatic_config()` or its `INVERTER_DEF` rows. That follows once the checker
  has been quiet on real installs.
- A component for manual inverters, and `inverter.py` reading bindings directly.
- The liveness check that will later decide which of two claimants for one serial wins.
- Chargers, cars, meters. GE Cloud's `async_automatic_config_evc()` is untouched.
- Updating the GE Cloud templates. The serial rule below makes the current templates safe.
- Persisting slot numbers across restarts.

## Anti-goals

- An install with the switch off must behave exactly as it does today, with one deliberate exception:
  goal 7, which is a rule about `apps.yaml` and applies whatever the switch says.
- A manual inverter that does not share a serial with a discovered one must never be changed.
- The checker must never change configuration or component health; it only logs.

## What exists today (verified against the tree, 14 Nov 2026)

- `coordinator.py` `Coordinator` collects reports (`report()`), assembles and publishes a catalogue. It
  writes nothing to args. It lives at `base.components.coordinator`.
- `ComponentBase.refresh_discovery()` rebuilds a component's report each cycle and files it when it
  differs from the last one filed.
- `gecloud.py` `async_automatic_config(devices)` runs on the component's first cycle and whenever the
  device set changes, when `self.automatic and self.inverter_control`. `refresh_discovery()` runs later in
  the same cycle. `build_discovery()` reports one record per driven device with an `entities` map
  (`_device_entities()`), built to match what `async_automatic_config()` binds.
- `Fetch.set_arg(arg, value, index=None)` (`userinterface.py`) can already write one list entry, padding
  with `None`. `value=None` without an index deletes the key.
- `args_from_apps_yaml` is a snapshot of args before any defaulting or automatic configuration.
- `inverter.py` supports at most four inverters and treats a scalar per-inverter arg as inverter 0's.
- GE Cloud starts in component phase 1. Solcast starts in phase 2 specifically so that `pv_today` set by
  GE Cloud is already in args (`components.py`, comment on the solar entry).
- `tests/discovery_contract.py` has `capture_automatic_config()` (runs `automatic_config()` with the
  setters recorded) and `assert_record_agrees()`.

## Approaches

Three were considered.

- **A. The coordinator writes from the records** (chosen). The coordinator turns each opted-in record's
  `entities` map into per-slot bindings and replays them. GE Cloud's old code is kept only to check the
  result. This is the only option that proves the records are sufficient, which is what the proof of
  concept is for. Its cost is that every place the old code and the records differ needs a coordinator
  rule or a named exception.
- **B. The coordinator only hands out slots**, and GE Cloud's existing code writes into the slots it is
  given. Small and low risk, but it proves nothing about the records and every component would need the
  same rework again later.
- **C. Shadow mode**: GE Cloud keeps writing; the coordinator builds its version on the side and the two
  are diffed. Safest, but slots and manual coexistence never run for real.

Trefor picked A.

## Design

### The switch (my call, not yet confirmed by Trefor)

> 14 Nov 2026: the switch and GE Cloud's old write path were removed by the maintainer's decision; GE Cloud configures only through discovery, and its old automatic configuration survives only as check_automatic_config().

A new `apps.yaml` boolean `discovery_automatic`, default `False`, added to `APPS_SCHEMA`.

- Off: `allow_automatic()` returns `False`, GE Cloud runs its existing code unchanged, the coordinator
  stays observe-only.
- On: the behaviour in this document.

Reason: GE Cloud automatic is the most common predbat.com configuration and this piece changes
`num_inverters` and slot layout. The switch lets it be turned on per instance and the checker's output
read before it becomes the default. Removing the switch later is a one-line default change.

### Opting in

`Coordinator.allow_automatic(component_name, skip_settings=(), keep_user_settings=(), include_pv_only=False)`
returns whether the coordinator will configure this component (`False` when the switch is off).

- `skip_settings`: settings the user has opted out of, which the record still carries (spec D11). GE
  Cloud passes `load_today` when `ge_cloud_load_today_ignore` is set. A skipped setting is left exactly
  as it is in args.
- `keep_user_settings`: settings where an `apps.yaml` value for that slot wins over discovery, matching
  today's `set_arg_auto(..., overwrite=False)`. GE Cloud passes `inverter_mode` and `export_limit`.
- `include_pv_only`: whether records with no `inverter_type` (PV-only devices) contribute their
  `pv_power` and `pv_today`. GE Cloud passes the value of `ge_cloud_automatic_split_pv`.

`ComponentBase` gains a thin `allow_automatic_config(**options)` wrapper, like `report_discovery()`,
that is a no-op returning `False` when there is no coordinator.

A component stays in the allowed set when it stops, and keeps its slots (see Releasing a slot).

### The bindings structure

Held by the coordinator. Each configure pass derives it again from the current reports and the existing
slot assignments (cheap dict work), and replay then writes only what differs:

```python
{
    "inverters": [                      # index in this list IS the slot number
        {
            "slot": 0,
            "origin": "manual",         # "manual" | "discovered" | "free"
            "source": None,             # component name for a discovered slot
            "device_id": None,          # the record's device_id for a discovered slot
            "inverter_type": "GE",
            "serials": ["ce1234g567"],  # casefolded; for a manual slot, the serials found by the scan
            "settings": {               # Predbat setting name -> binding
                "charge_rate": {"entity_id": "number.x", "access": "rw"},
                "grid_power": {"value": 0, "access": "r"},
            },
        },
    ],
    "site": {
        "settings": {                   # bound once, not per slot
            "battery_temperature_history": {"entity_id": "sensor.x", "access": "r"},
        },
        "extras": {                     # summed-setting entries that belong to no slot: the user's
            "pv_power": ["sensor.y"],   # own extras first, then PV-only devices when included
        },
    },
}
```

For a discovered slot `settings` is the record's validated `entities` map (descriptors kept whole, so the
later `inverter.py` reader has formats, units and access). For a manual slot it is built from args: each
per-inverter setting's value at that index, as `{"entity_id": ...}` when it is an entity id and
`{"value": ...}` otherwise, with `access` left out because args do not say.

**Binding settings** (my call). The settings the coordinator owns are an explicit constant
`BINDING_SETTINGS`: every setting name an inverter record's `entities` map may bind, plus `givtcp_rest`.
The control service keys are not indexed by inverter, so they are read when detecting a manual slot and
never written. It is not "every `APPS_SCHEMA` key with
`entries: num_inverters`": that wider set includes user tuning the old code never touches
(`inverter_battery_rate_min`, `inverter_limit_charge`/`_discharge`/`_export`/`_override`,
`battery_export_permitted`, `soc_max_fallback`) and `inverter_reserve_max`, which is read as a global.
Those are never read, written or cleared here, in a takeover included. The contract test asserts every
reporter's `entities` keys are in `BINDING_SETTINGS`, so a new setting cannot be reported without being
owned. Wherever this document says "per-inverter setting" it means a member of `BINDING_SETTINGS`.

**Summed settings** (my call). `load_today`, `import_today`, `export_today`, `pv_today` and `pv_power`
are lists whose length is not tied to `num_inverters`: the first four are summed whatever their length,
and `pv_power` entries past `num_inverters` count as extra PV (`execute.py`). For these, an `apps.yaml`
entry at an index at or above the manual `num_inverters` is the user's extra and is preserved, in its
original order, after the slots' own entries and followed by the PV-only entries when included.

- `load_today`, `import_today`, `export_today` and `pv_today` are written **compact**: each slot's
  entry in slot order with unbound slots left out, then the extras. This is the shape the old code
  writes (one entry on a shared CT or behind an EMS), so the lists never hold a `None`.
- `pv_power` stays indexed by slot, because `inverter.py` reads it by inverter index: a slot that binds
  nothing gets a fixed `0`, and the extras follow the last slot so they still count as extra PV.

Site settings: the record field set `SITE_SETTINGS` in `tests/discovery_contract.py`
(`battery_temperature_history`) moves into `coordinator.py` as the shared definition. A site setting is
taken from the lowest-numbered discovered slot whose record carries it.

The bindings are added to the catalogue as `observations.bindings`, replacing nothing, and each
inverter record in the assembled catalogue gains a `slot` field when it has one. This is the "slot
number saved in the discovery data". The summary entity (`publish()`) is unchanged.

### Building the bindings

A pure function of: the `apps.yaml` snapshot, live args, the validated reports of allowed components and
the previous pass's `device_id -> slot` map. It writes nothing. Steps, in order:

1. **Manual slots.** For each index below the configured `num_inverters` (read from the `apps.yaml`
   snapshot, default 1), the slot is manual when the snapshot gives it both:
   - an `inverter_type` (list entry at that index, or a scalar for index 0), and
   - a control method: at least one of a bound entity on a setting whose `APPS_SCHEMA` entry has
     `"modify": True`, a `givtcp_rest` entry, or a configured control service (the `*_service` keys the
     service API uses).

   Anything else is a free slot. The snapshot is used, not live args, so a previous pass's own writes
   never count as manual. A slot with control entities but no `inverter_type` is not manual: it is a
   dead slot (Trefor, 14 Nov 2026 - see Retiring the assumed inverter type), free for a discovered
   inverter to take.
2. **Serial scan of manual slots.** For each manual slot, collect every string bound at that index in a
   per-inverter setting (from live args, so `re:` patterns are resolved), plus `ge_cloud_serial` for
   slot 0 as the `apps.yaml` snapshot has it - never the live value, which GE Cloud itself sets to its
   first battery and which would make every slot 0 match. A discovered record's serials are its `hardware_ids.serial` plus its structural `serials`.
   The slot matches a record when any of the record's serials, casefolded and at least six characters
   long, appears in any of those strings casefolded. Matched serials are stored on the manual slot.
3. **Allocation**, over the allowed components' records that have an `inverter_type`, in report order
   (components sorted by name, records in report order):
   - A record already in the previous map keeps its slot.
   - Otherwise a record matching a manual slot takes over the lowest matching slot. A warning is logged
     naming the serial, the slot and both sources. Any other manual slot matching the same record is
     cleared and becomes free, with its own warning (a gateway record claims the batteries behind it, so
     a two-serial template collapses to one inverter).
   - Otherwise it takes the lowest free slot.
   - With no slot left below four, the record is not configured and a warning is logged.
4. **Result.** `num_inverters` is the highest non-free slot plus one. A free slot left below that is
   not compacted in this piece. It has no `inverter_type`, so it is a dead slot and Predbat errors out
   on it by the rule below; the coordinator logs a warning naming the slot so the cause is clear.

**Releasing a slot** (my call, to match today, where args survive a component restart). A slot is
released, and its binding settings cleared, only when its component files a new report that still has at
least one driven inverter but no longer has that record. A component stopping does not release anything:
the web restart button stops a component for ten seconds or more while the main loop keeps running. A
report with no driven inverter at all releases nothing either and logs a warning, as the old code's "No
battery devices found" does. `num_inverters` is never written as 0.

**Unconverted automatic components** (my call). Every other inverter component still writes whole lists
from index 0 and sets `num_inverters` itself, and its writes are not in the `apps.yaml` snapshot. Running
one alongside an opted-in component is not supported in this piece, as running two automatic inverter
components is not supported today (last writer wins). When a component that has not opted in reports
`automatic: true` with a driven inverter while another has opted in, the coordinator logs a warning
naming both on each pass that writes, and carries on. The catalogue already records this as
`multiple_inverter_sources`.

### Retiring the assumed inverter type

Decided by Trefor, 14 Nov 2026. Today `inverter.py` reads `inverter_type` with a default of `"GE"` and
logs a warning that it is assuming GivEnergy. That default goes.

- An inverter index below `num_inverters` with no `inverter_type` is a dead slot. Predbat errors out on
  it: `record_status(..., had_errors=True)` and a `ValueError`, the way `inverter.py` already fails on
  an inverter it cannot read, so the cycle stops and no inverter is driven on a guessed type.
- The two cases that set no type in `apps.yaml` keep working, because the type is set for them before
  any `Inverter` is built:
  - every component's automatic configuration sets `inverter_type` (all of them do today, and with the
    switch on the coordinator sets it per slot);
  - GivTCP REST sets it too (`givtcp.py`, its own automatic configuration).
- While an inverter component is active but has not configured yet (`inverter_source_active()`), the
  message says Predbat is waiting for that source to configure inverter N. With no such component it
  says `inverter_type` must be set in `apps.yaml` for inverter N. Both stop the cycle.
- This applies with the switch on or off (my reading of "retire"; it is a breaking change for a manual
  install that relies on the default, which has been warned on every start since #4990).
- Every template without an `inverter_type` line is an automatic one (checked 14 Nov 2026), so no
  template changes are needed.

What changes in code: `inverter.py` (the read at the top of the type handling, and
`inverter_type_is_assumed()` / `inverter_type_assumed_name()` with their callers, which exist only to
word messages around the assumption), and the same `"GE"` default in `plan.py`'s load power read.

Two things the builder must check, because the default is relied on quietly:

- The shared test fixture (`tests/test_infra.py`) and any test that builds args without an
  `inverter_type`. Set the type once in the fixture rather than in each test.
- Debug-file replay (`./run_all --debug_file`, and the golden `coverage/cases/*.yaml`): a dump from a
  manual install with no type would now fail. Decided by Trefor, 14 Nov 2026: the debug-file harness
  assumes `GE` when a dump has no `inverter_type`, for legacy dumps, and logs that it did. This default
  lives in the replay harness only, never in `inverter.py`, and the committed cases stay as they are.

This part does not depend on the rest of the piece and can be built and merged first.

### Replaying into args

`Coordinator.replay(bindings)` compares each of the following with the live arg and writes it only when
it differs. For each **discovered** slot only:

- `inverter_type` at the slot index.
- Every per-inverter setting at the slot index: the binding's `entity_id` or `value` when the record
  binds it, otherwise `None` (the coordinator owns the whole slot and clears what no record binds,
  including `givtcp_rest`). Exceptions: `skip_settings` are not touched; a `keep_user_settings` setting is
  not touched when the `apps.yaml` snapshot has a value at that index.
- A scalar per-inverter arg is first turned into a list with the scalar at index 0.

Then once: the site settings, `pv_power`/`pv_today` extended with the PV-only entries when included, and
`num_inverters`.

Manual slots are not written. Cleared slots have every per-inverter setting set to `None` at that index.

A per-inverter key whose every entry is `None` is deleted, which is what `set_arg(key, None)` did before.

A skipped setting, or a kept one with a snapshot value, contributes the `apps.yaml` snapshot's own
entry for that slot, which for a slot-indexed setting means no write and for a compact one means the
user's entry stays in the list.

### When configuration runs

Configuration is incremental and re-runs whenever something changes (Trefor, 14 Nov 2026). There is no
single moment at which it must be complete: a pass applies what is known now, and a later pass applies
only what has changed since.

- **On change**: `configure()` runs whenever an allowed component's cleaned report differs from the one
  it replaces, and when a component opts in (`allow_automatic()`). This
  holds from the moment the component opts in, including while components are still starting - nothing
  is deferred to a barrier. GE Cloud opts in and files its report during its first cycle in phase 1, so
  its inverters are in args before phase 2 starts, which is what Solcast (phase 2) relies on for
  `pv_today`.
- **At startup**: `predbat.py` `initialize()` also calls `configure()` after phase 1 has started and
  again at the existing catalogue barrier after phase 2. These are ordinary passes. They exist so the
  manual slots are in the bindings on an install where no component has opted in, and they write nothing
  when nothing has changed.
- **Incremental means three things**:
  - *Slots are sticky.* Once a device has a slot it keeps it for the life of the process, whatever else
    appears or disappears. A new device only ever takes a free slot or takes over a matching manual one.
  - *Only differences are written.* Replay compares each value with the live arg and writes only where
    they differ, so a pass with nothing new writes nothing and logs nothing. Comparing with live args,
    not with the previous bindings, also repairs an entry something else has overwritten.
  - *Only what changed is checked and logged.* The checker runs for a component only when the pass
    changed one of its slots, and each pass logs one line per slot it changed.
- `configure()` is build, then replay, then check, under the coordinator's lock. It runs on whichever
  thread triggered it (the main thread at startup, the component's thread later), which is the threading
  args writes already have today. It never raises: a failure is logged as a warning and args are left as
  they were.

Headless hosts that call `initialize_config()`/`finalize_config()` themselves call `configure()` between
the two, where they complete discovery today.

### GE Cloud

`async_automatic_config(devices)` is split:

- `_legacy_bindings(devices)`: the existing body from the feature sniffing down to the EMS section, made
  synchronous and pure. Every `set_arg`/`set_arg_auto` call becomes an entry in a returned dict
  `{setting: value}` (the last write wins, as today). It reads nothing it does not read today.
- The rest stays live in `async_automatic_config()`, with the switch on or off, because none of it is an
  inverter binding: `ge_cloud_serial`, the `ge_cloud_data` EMS toggle and its restore, the
  `inverter_hybrid` write, and the log lines.

With the switch off it applies `_legacy_bindings()` through `set_arg`/`set_arg_auto` exactly as now
(same values, same `overwrite=False` keys, same order).

With the switch on it calls `allow_automatic_config(...)` with the three options above and writes no
binding.

`check_automatic_config(slots)` is the checker. After a replay that changed one of a component's slots,
the coordinator calls it on that component if it defines it, passing the slot of each of its driven
records in record order. It:

- recomputes `_legacy_bindings(self.devices_dict)`,
- for each setting, compares the legacy value for logical inverter `i` with `args[setting][slots[i]]`
  (scalars and site settings compared directly; `num_inverters` and `inverter_type` compared per slot
  only, since manual slots change the totals),
- logs one `Warn: GECloud: discovery set ...` line per difference, at most once per setting and value
  pair for the life of the process,
- logs one `Info` line when there are none.

Known differences are not warnings. They are listed in one named table in `gecloud.py`, each with its
reason, and logged at `Info`:

- `pause_mode`, `pause_start_time`, `pause_end_time`, `discharge_target_soc`, `charge_rate_percent`,
  `discharge_rate_percent`: the legacy code binds them when any device has the register, the record only
  when this device does (spec D10). The record is right.
- `givtcp_rest`: legacy deletes the key; replay clears only its own slots.
- Settings in `skip_settings`.

Anything else that differs is a real finding and stays a warning until fixed in the record builder or
the coordinator.

### Error handling

- Switch off, no coordinator, or a component that never opts in: nothing changes.
- A record with no `entities` or no `inverter_type`: not allocated (PV-only records are handled by
  `include_pv_only` only).
- More inverters than slots, a free slot left in the middle: warning, as above.
- An exception anywhere in `configure()`: warning, args untouched for that pass.

## Testing

New `tests/test_discovery_bindings.py`, registered in `TEST_REGISTRY`, quick suite:

- Manual slot detection: type and control entity; type only; control only (a dead slot, not manual);
  scalar type; `givtcp_rest`; a service API slot.
- Dead slot: a discovered inverter takes a control-only slot; a hole left below `num_inverters` is
  reported.

In `tests/test_inverter.py`, replacing `test_inverter_type_default_warning`: an inverter with no
`inverter_type` and no inverter component errors out with the "must be set" message; with an inverter
component active it errors with the "waiting" message; a GivTCP REST install and a GE Cloud automatic
install with no `inverter_type` in `apps.yaml` start normally.
- Serial scan: case differences; a serial shorter than six characters ignored; no match.
- Allocation: empty install; one manual plus one discovered; takeover of a matching manual slot with the
  warning; a gateway record matching two manual slots; a fifth inverter; a stable slot across two passes
  when report order changes; a slot freed when a record disappears.
- Replay: only discovered slots written; unbound settings cleared at the slot; `skip_settings` and
  `keep_user_settings`; scalar turned into a list; site settings; PV-only entries.
- Triggers: applied as soon as an opted-in component reports, before the startup passes; applied again
  on a changed report; not applied for a component that has not opted in; nothing applied with the
  switch off.
- Incremental: a second pass with the same reports makes no `set_arg` call and logs nothing; a second
  device appearing leaves the first device's slot and args untouched; a changed entity on one slot writes
  that one entry only; an arg overwritten from outside is put back on the next pass; the checker is not
  called for a component whose slots did not change.

### Acceptance tests (Trefor, 14 Nov 2026)

These are the tests that decide whether the piece is done. Each one drives the real `GECloudDirect`
with dummy cloud data, twice from the same starting args, and compares the two resulting configurations:

1. **Old**: switch off. The component's own automatic configuration writes args, as today.
2. **New**: switch on. The component opts in and reports; the coordinator builds the bindings and
   replays them.

The dummy data goes in at the HTTP boundary where the existing fake cloud can serve it
(`tests/gecloud_fake_cloud.py` `FakeGECloud`, extended to more than one device, PV-only devices and
meter serials as needed), so device discovery, register reads, entity publishing, the report and the
configuration all run for real. Where the fake cloud cannot yet express a topology, the test may set
`devices_dict`, `settings` and `info` directly, as the current `test_ge_cloud.py` automatic-config tests
do, but the automatic configuration and discovery code under test must not be stubbed.

The comparison is the whole inverter configuration, not a sample: `num_inverters`, `inverter_type`,
every per-inverter setting and the site settings. The two must be equal, except for:

- the named known differences listed under GE Cloud above, each asserted to be exactly the documented
  difference rather than skipped, so an exception that no longer applies fails the test;
- trailing `None` entries in a slot-indexed list, and a key whose every entry is `None` against the
  same key absent.

Each scenario also asserts the checker logged no warning in the new run.

Scenarios, all required:

| Scenario | Dummy data | What it proves |
| --- | --- | --- |
| Single inverter | one battery inverter | the base case |
| Multi inverter | two battery inverters, a meter each | per-slot lists line up, `num_inverters` 2 |
| Multi inverter, mixed registers | one with a power-rate register, one with only the percentage rate; one with pause, one without | the per-device rule and its named exceptions |
| PV-only plus battery, split PV off | one battery inverter, one PV-only inverter | the PV-only device adds nothing |
| PV-only plus battery, split PV on | the same, `ge_cloud_automatic_split_pv: true` | `pv_power`/`pv_today` gain the PV-only entries, `num_inverters` stays 1 |
| CT clamp: detected shared | two inverters reporting the same meter serial | grid and load from the first inverter, zeros after |
| CT clamp: detected separate | two inverters, different meter serials | every inverter contributes |
| CT clamp: `ge_cloud_automatic_shared_ct` | different serials, override on | forced shared |
| CT clamp: `ge_cloud_automatic_split_ct` | same serial, override on | forced split, and split wins when both are set |
| Gateway | a gateway in front of two batteries | one logical inverter |
| EMS | an EMS with two batteries | type `GEE`, EMS entities, first-slot-only power sensors |
| Opt-outs | `ge_cloud_load_today_ignore`; an `apps.yaml` `inverter_mode` and `export_limit` | skipped and user-kept settings survive |

Two further acceptance tests use the same harness without an old run to compare against, because the old
code cannot do them:

- **Manual plus discovered**: `apps.yaml` holds a manual inverter of another type in slot 0; the GE
  Cloud inverter lands in slot 1, slot 0 is byte-for-byte unchanged, `num_inverters` is 2.
- **Template takeover**: `apps.yaml` holds the `givenergy_cloud.yaml` template bindings naming the same
  serial; the result equals the single-inverter scenario's new run, with the takeover warning logged.

The switch-off path is itself protected by the existing `test_ge_cloud.py` automatic-config tests, which
must pass unchanged after `async_automatic_config()` is split.

The existing `discovery_contract` agreement tests keep passing: they call `automatic_config()` through
`capture_automatic_config()`, so that helper is pointed at the switch-off path.

## Documentation

- `docs/discovery-catalogue.md`: the bindings, slots, the switch, and the takeover rule.
- `docs/apps-yaml.md`: `discovery_automatic`; the `inverter_type` section no longer says it defaults to
  `GE`, and says when it may be left out.
- `docs/inverter-setup.md` where it mentions the default, and a breaking-change line for the release
  notes.
- `docs/components.md`: the GE Cloud section notes the switch.

## Left to the builder

- Exact names of private helpers, and how the lock is arranged around `report()` calling `configure()`.
- The list of `*_service` keys that count as a control method, read from `inverter.py`.
- Whether `_legacy_bindings()` needs any state beyond `devices` to stay pure.
- Task order. Suggested: retiring the assumed inverter type; bindings builder and replay with tests; the
  switch and opt-in; the startup and change triggers; the GE Cloud split and checker; the acceptance
  tests; docs.

## Open follow-ups, not in this piece

- Liveness check to choose between a manual and a discovered claimant for one serial.
- Compacting a free slot left below a used one.
- Persisting the slot map across restarts.
- Template updates once the switch is on by default.
- The three follow-ups carried from piece 1 (partial-poll policy for energy bindings, `soc_units`
  preference, Fox limit ratings as strings).
