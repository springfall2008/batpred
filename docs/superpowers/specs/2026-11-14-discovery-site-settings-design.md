# Discovery: hybrid, inverter serials, GE Cloud Data and the invert settings

Status: approved by Trefor, 14 Nov 2026. Addendum to
`2026-11-14-discovery-automatic-inverter-config-design.md`, on the same branch (PR #6188).

## Why

After the first slice, GE Cloud's `async_automatic_config()` still configures three things itself, outside
discovery: the `inverter_hybrid` switch (from the inverter model), the GE Cloud Data component (turned off behind
an EMS, restored when the EMS leaves) and `ge_cloud_serial`. Replay also ignores the `invert` flag records already
carry on their entity descriptors, which components such as Fox, Solis, SolaX and Teslemetry depend on. Trefor's
decisions, 14 Nov 2026:

- Every inverter component publishes to discovery whether its inverter is hybrid, when it knows. Discovery turns
  `inverter_hybrid` off when at least one inverter is confirmed not hybrid, and on when one or more are confirmed
  hybrid and none is confirmed not hybrid. PV-only inverters do not count, for now.
- GivTCP decides hybrid with the same model-name rule as GE Cloud.
- The GE Cloud Data change behind an EMS is a hack: take it out of GE Cloud. The GE Cloud Data component refuses to
  run behind an EMS itself; it is cloud data logic, not discovery.
- Each inverter's serial is posted to discovery (it already is); `ge_cloud_serial` should not need setting by GE Cloud.
- Discovery sets each `*_invert` setting true or false as the inverter requires.

## 1. Hybrid

### Record field

Inverter records gain one structural field, `hybrid`: `True` (the inverter is a hybrid - PV on its own DC inputs),
`False` (it is not - AC coupled, or a battery-only unit) or left out when the component cannot tell. It is a
keyword parameter of `inverter_record()`, validated as a bool (anything else is dropped and logged, as for other
structural fields), and documented in `docs/discovery-catalogue.md`.

### Who reports what

Each component states only what it has evidence for. The builder confirms each against the component's code and
tests, and records any component that turns out to have no evidence as "unknown":

| Component | Evidence | hybrid |
| --- | --- | --- |
| GE Cloud | device model | `False` when the model contains `ac`, `aio` or `all-in-one` (the existing rule), else `True`; unknown when no model |
| GivTCP | inverter model / type | the same rule as GE Cloud (Trefor) |
| Gateway | `inv.model` | the same rule (it already uses it); unknown on firmware that reports no model |
| AlphaESS | `detect_ac_coupled()` | `False` when AC coupled, `True` when hybrid, unknown when undecided |
| Sigenergy | `detect_ac_coupled()` | as AlphaESS |
| Teslemetry | `teslemetry_hybrid`, then Powerwall 3 / `battery_type` | the value its current code writes; unknown when the model is unknown |
| Hanchu | the AC-coupled field (`HANCHU_AC_COUPLED_FIELD`) | `False` when it says AC coupled, else `True` if the field is present |
| Enphase | AC batteries (`hasAcb` / IQ Battery) | `False` (AC coupled) when the site reports them |
| SolaX | model code table | `False` for AC models (`X1-AC`), `True` for the Hybrid models, unknown otherwise |
| Solis | model naming | `True` for the hybrid/storage ranges, unknown otherwise |
| Growatt | device type, if its API reports it | builder to confirm; unknown if not |
| Deye, Sunsynk, Fox | none (their code says they cannot tell) | unknown |

### The rule

The coordinator works out the site value from the inverter records of every reporting component whose report
carries `automatic: true` (my call: a component the user has not asked to configure automatically does not change
Predbat's settings, as today), counting only driven records (those with an `inverter_type`; PV-only records do not
count):

- any record `hybrid: False` -> `inverter_hybrid` off;
- otherwise any record `hybrid: True` -> on;
- otherwise (nothing known) -> left as it is.

It applies the rule whenever a report changes, whether or not the component has opted in to slot configuration,
and writes only when the value differs from the current setting, through the same path a component uses today
(`set_state_external` on Predbat's HA interface, which updates the setting the planner reads). That call is async
and `configure()` is synchronous and may run on a component's thread, so the write is scheduled, never awaited
inline; it never raises and never blocks a configure pass. Each change is logged once, naming the deciding
records, for example `"Coordinator: inverter_hybrid set off - gecloud:fd2445g166 reports it is not a hybrid inverter"`.

The five components' own `inverter_hybrid` writes (GE Cloud, AlphaESS, Sigenergy, Teslemetry, Gateway) are removed:
discovery is the one writer.

## 2. GE Cloud Data behind an EMS, and `ge_cloud_serial`

Today GE Cloud sets `ge_cloud_serial` (to its first battery, or to the EMS) and, behind an EMS, sets `ge_cloud_data`
to false, restoring the user's value when the EMS leaves. `ge_cloud_serial` has one reader: the GE Cloud Data
component (`GECloudData`), which takes it on its first run as the serial whose history it downloads.

- GE Cloud no longer sets `ge_cloud_serial` or `ge_cloud_data`; the `ems_disabled_ge_cloud_data` /
  `ge_cloud_data_before_ems` state goes.
- `GECloudData` takes its serial from the user's `ge_cloud_serial` when set, otherwise from discovery: the GE Cloud
  inverter record in the lowest slot - its first fronted battery (`serials[0]`) for a gateway record, else its own
  `hardware_ids.serial` - which matches today's first battery. It resolves the serial when it needs one rather than
  once at start-up, so a GE Cloud report that lands after `GECloudData` starts is still picked up.
- `GECloudData` refuses to run while GE Cloud's records say the site is behind an EMS (`composition: "ems"`),
  logging once, and resumes by itself when the EMS is no longer reported. The user's `ge_cloud_data` value is never
  changed.
- `GECloudData` reads discovery through a small read-only coordinator accessor (for example
  `Coordinator.inverter_records(source)`), not through args.
- Gateway's own `ge_cloud_data` / `ge_cloud_direct` writes are left for its conversion.

## 3. The invert settings

`inverter.py` reads `battery_power_invert`, `grid_power_invert` and `load_power_invert` per inverter, defaulting to
false. The coordinator now owns them for every discovered slot: each is `True` where that slot's record marks the
matching entity (`battery_power`, `grid_power`, `load_power`) `invert: true`, otherwise `False`. Manual slots are
never written. They become binding settings, written per slot like the others, kept compact with the slot count.

For GE Cloud this writes an explicit `[False]` (or one `False` per inverter) where the old configuration left the
key unset, which `inverter.py` reads the same. The acceptance comparison treats an unset invert setting as equal to
all-false; nothing else is normalised. Components not yet converted keep writing their own invert settings.

## Testing

- Each component: a test that its record reports `hybrid` as the table says (each evidence case, and unknown), and
  that it no longer writes `inverter_hybrid` itself.
- Coordinator: the rule (one false among trues -> off; all known true -> on; nothing known -> untouched; PV-only
  ignored; a non-automatic report ignored; write only on change; never raises), including the scheduled write
  against a stub HA interface.
- GE Cloud Data: serial from apps.yaml, from a direct record, from a gateway record; refuses behind an EMS and
  resumes after; picks up a report that lands after it started.
- Invert: per-slot replay of each flag; manual slots untouched; GE Cloud acceptance scenarios unchanged apart from
  the stated normalisation; a fixture record with `invert` true writes `True` at its slot.
- GE Cloud acceptance tests and the golden test keep passing; the checker treats the invert settings as matching
  when the legacy plan leaves them unset.

## Not in this piece

Gateway's `ge_cloud_data` / `ge_cloud_direct` writes; converting other components' slot configuration; PV-only
inverters counting towards hybrid; a read-only option for the GE Cloud command-line run.
