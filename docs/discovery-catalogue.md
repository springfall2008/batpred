# Discovery Catalogue

## What it is

The discovery catalogue is a single document describing the hardware and accounts Predbat's
components have actually found on your installation - inverters, EV chargers, cars, electricity
meters and solar forecast providers - built once at startup from what each component reports about
itself.

It exists because a real installation is rarely as simple as one inverter and one tariff: several
components can each see part of the same physical setup (a GivEnergy inverter visible through both
GivTCP and GE Cloud, say), or claim the same car slot, and today those situations are resolved
silently by whichever component happens to run first. The catalogue does not change that behaviour

- **nothing about it writes to your configuration, or changes anything Predbat does**. It only
makes what was already happening visible, so a maintainer reading a bug report can see the whole
picture at a glance instead of reconstructing it from log lines, and so a genuine ambiguity (two
components both claiming the same inverter) is recorded as an observation rather than staying an
invisible coincidence of startup order.

## Where to find it

**The Discovery page in the web UI.** The quickest way to look: open **Discovery** in the Predbat
web interface. It shows the per-section record counts, any conflicts, a status for every component,
and a card per record, with the entity map collapsed behind a toggle and the whole document
available at the bottom in the same YAML form a debug dump carries.

The page shows **what a bug report would contain** by default: everything the components reported,
with email addresses removed. "Show raw values" (`?raw=1`) switches to the document as reported, email addresses included. The
raw view is labelled on screen and is **not safe to share**: Predbat's web interface has no
authentication of its own, so treat raw mode the way you already treat the Apps page.

**A debug dump.** Every `predbat_debug.yaml` (written to `debug/` when
`switch.predbat_debug_enable` is on, and what you normally attach to a bug report) carries the full
catalogue under a top-level `discovery:` key. This is the complete document, in the form described
below: no passwords, keys or email addresses, but every identifier readable.

**`sensor.predbat_discovery`.** This entity carries a summary rather than the full document: its
state is the total number of records found, and its attributes carry `schema_version`, `generated`,
a per-section count, a status for every component Predbat knows about, and any conflicts observed
(see below). The full per-section data - which inverter has which entities, and so on - is
deliberately **not** in the entity's attributes; Home Assistant's recorder writes every attribute
change to disk, and the full catalogue is tens of kilobytes even on a modest install. Read the debug
dump for the complete picture.

The latest report from each component is kept - a new one replaces that component's previous
report rather than adding to it - and the catalogue is re-assembled fresh each time
it is actually read. A debug dump always reflects the latest state, including anything reported
*after* Predbat started - a rediscovered inverter, a changed tariff, a newly-discovered forecast
site, a retry that succeeded on a later cycle - not only what was known at the "discovery barrier"
(the point straight after every component has started or timed out). `sensor.predbat_discovery` is
different: its summary is *published* only once, at that same discovery barrier, and does not
refresh itself afterwards even though the underlying document it was built from keeps moving -
restart Predbat, or read a fresh debug dump, to see anything reported later than startup.

## What each section describes

| Section | What it holds |
| ------- | -------------- |
| `inverters` | Battery inverters and PV-only devices - type, composition (direct/gateway/EMS), which functions it serves (`solar`, `battery`), its behaviour (`capabilities`), its fixed ratings under Predbat's own setting names, and every setting Predbat's automatic configuration binds for it (`entities`) |
| `chargers` | EV chargers, cross-linked to the cars they serve |
| `cars` | Electric vehicles, cross-linked to the charger that charges them |
| `meters` | Electricity (and gas) supply points, each with a direction (`import`/`export`) and, where known, a nested tariff record |
| `forecasts` | Solar forecast providers (Solcast, forecast.solar, Open-Meteo, or your own HA sensors) and what each one covers |
| `programmes` | Flexibility enrolments (a VPP, a saving session, a free-electricity event) that emit events and may constrain Predbat, cross-linked to the meter they apply to |

Deye and Sunsynk cannot tell a PV-only inverter from a hybrid: their APIs give no signal, and
their automatic configuration treats every discovered inverter as a battery inverter. Their
records follow that configuration - `solar` and `battery` on every inverter - rather than
evidence about the hardware, and Solis's `solar` works the same way, since every Solis inverter
is configured as a PV source. A PV-only unit that has been configured as a battery inverter will
usually show it in its record: `battery` among its functions, but no `battery_capacity_ah` rating
and no `soc_max` entity.

Three reporters populate `programmes`. Octopus reports a joined Saving Sessions campaign as a
`saving_session` programme, and a `free_electricity` programme once the account has had a Free
Electricity session (that campaign has no joined flag, so a session is the only evidence). Kraken
reports an EDF account enrolled in Flextras as a `free_electricity` programme. Each of these links
its `meter` to the account's import meter. Axle reports its VPP enrolment
as a `vpp` programme with the effects it has on Predbat: `rate_override` always, plus
`forces_read_only` when `axle_control` is on. Axle cannot tell which supply point it is enrolled on,
so its record names no `meter`. On other installs the section is present as an empty list rather
than missing from the document.

A charger integration that knows nothing about the vehicle it charges still reports a car, marked
`flags: ["stub"]`, because Predbat indexes cars rather than chargers. A car without that flag is a
vehicle the integration actually described (Ohme can, when the account names one).

Every record carries a `source` field naming the component that reported it, a `device_id` unique
within that component, and whichever typed containers below the component chose to populate. Two
extra top-level sections describe the fleet as a whole rather than any one device:

- **`components`** - a status (`ok`, `no_report`, `not_started` or `load_error`) for each component
  that either reported, or was expected to and did not. A component that should be describing your
  hardware but is not shows up as clearly as one that is. `load_error` says only *that* the
  component failed to start; the reason is an arbitrary exception message, which is free text this
  document deliberately does not carry - look in the Predbat log, which records it in full
  alongside a traceback.

    Two kinds of component are deliberately left out, because their silence carries no
    information: one you never configured (its absence is its status - the full registry is in
    `components.py`), and one that is running fine but has no reporter at all. Most components are
    in that second group - `web`, `ha`, `chat`, `carbon`, `temperature` and the rest are not
    hardware sources and will never report. Listing them all made the map thirty-odd entries of
    which a handful said anything, and buried the case that matters: a component that *can* report
    and did not
- **`observations`** - things noticed about the assembled picture rather than about any one
  component: `conflicts` (see below) and `resulting_config`, the handful of apps.yaml keys
  (`num_inverters`, `num_cars`, `inverter_type`) discovery can be compared against

### Conflicts

`observations.conflicts` records situations that today resolve silently by whichever component
happened to run first - the catalogue observes them, it does not resolve them:

| Kind | Fires when |
| ---- | ---------- |
| `duplicate_serial` | Two components claim the same hardware serial - including a serial a gateway record fronts on behalf of the batteries behind it, not only the serial identifying the record itself |
| `multiple_inverter_sources` | More than one component reports an inverter record at all (e.g. both GivTCP and GE Cloud) |
| `multiple_import_meters` | More than one component reports an import meter |
| `contested_car_slots` | A car is reported by a component that reports no charger, alongside a component that reports both a charger and a car - two components with a different idea of what should occupy a car slot |

## What is, and is not, redacted

Only **credentials and email addresses** are kept out of the catalogue. Everything else a component
reports is published exactly as reported (maintainer decision, 2 Nov 2026):

- **Never included:** a password, an API key, a token or any other field whose name reads as a
  credential (`api_key`, `password`, `token`, ...). Validation refuses such a field outright,
  wherever it is nested, so it never reaches the assembled catalogue.
- **Removed:** an email address, wherever a component put it - a value, an entity id, a dict key -
  is replaced with `<email>` in the debug dump, the web page and `sensor.predbat_discovery`. Only
  the raw web view (`?raw=1`) shows it.
- **Readable:** every identifier - hardware serials, MPANs, account numbers, site, plant and system
  ids, charger ids, Intelligent device ids - along with location fields, firmware, models, tariff
  and product codes, and entity ids. Identifiers are what make a record useful: they let you match
  it to your own hardware and dashboard, and they are what anything configuring Predbat from the
  catalogue needs to re-bind an entity. Earlier versions replaced account identifiers with `#`
  tokens; they no longer do.

Bear that in mind before attaching a dump to a public issue: it carries your serial numbers, MPAN
and account ids.

## For developers: the report schema

A component describes what it found by implementing `build_discovery()`, returning a plain dict:

```python
def build_discovery(self):
    return {
        "automatic": self.automatic,   # whether THIS component wired apps.yaml to what it found
        "inverters": [...],            # any section keys you have records for - omit the rest
    }
```

and reporting it - typically once per `run()` cycle - via the base class helper:

```python
self.report_discovery(report)
```

which routes to `Coordinator.report(component_name, report)` if a coordinator is running, and is a
safe no-op otherwise (the standalone CLI harnesses have no registry at all). The coordinator
validates the report - see the container model below - keyed on `component_name`
(`ComponentBase` sets this to your registry key automatically), and a later `report()` call for the
same component fully replaces its previous one; there is no need to compute a diff yourself.

A record needs a `device_id` (a string, unique within your component - anything else is dropped
with a warning) plus whichever *structural* fields that section defines (`inverter_type`,
`direction`, `serves_cars`, ...) and *containers* you have facts for.

### The container model, and why it is typed

Rather than trying to name every field a future reporter might need, the schema instead defines a
small set of **containers**, each declaring the *type* of value it accepts:

| Container | Accepts |
| --------- | ------- |
| `hardware_ids` | Short strings |
| `info` | Short strings, no `@` |
| `ratings` | Numbers and booleans, keyed by Predbat's setting name where one exists (`inverter_limit`, `export_limit`, `import_limit`, `battery_rate_max`, `soc_max`, `battery_min_soc`) |
| `coverage` | A number/boolean, or a list of lowercase tokens |
| `entities` | An entity descriptor: `access` (`rw` or `r`, required), exactly one of `entity_id` or a fixed `value`, and typed fields such as `domain`, `unit`, `format`, `min`/`max`, `invert` |
| `capabilities` | `true`/`false` for the seven behaviour keys of an inverter definition (`support_charge_freeze`, `support_discharge_freeze`, `support_feedin_first`, `can_span_midnight`, `charge_discharge_with_rate`, `charge_control_immediate`, `target_soc_used_for_discharge`) |
| `account_ids` | Any scalar: an identifier that names your account or site rather than hardware (an MPAN, an account number, a site id) |
| `functions` / `flags` / `effects` | Lists of short lowercase tokens |

A value that does not fit its container's declared type is silently **dropped**, not coerced and
not raised as an error - a string offered to `ratings`, or free text over 64 characters offered to
`info`, simply never reaches the assembled catalogue. This is the actual safety property the design
rests on: it does not depend on enumerating every dangerous field name a component might one day
introduce, because a container that only accepts numbers structurally cannot carry a name, an
address or a pasted credential, however the schema grows. Choose whichever container matches the
*kind* of fact you are reporting, not the one that happens to accept the value you have.

### Inverter records and inverter definitions

An inverter record holds enough to rebuild that inverter's definition - the per-type table
(`INVERTER_DEF`) that tells Predbat how to drive it. `capabilities` carries its behaviour; whether it
has a reserve, a target SoC, charge/discharge enable switches, idle times or a timed pause follows
from which settings `entities` binds to a writable (`rw`) entity; and protocol detail such as the time
format or whether the charge rate is set in watts or amps is read from those entities' descriptors.
A setting Predbat replaces with a placeholder for this inverter type - the Solis Modbus (`GS`) type's
`reserve`, say - is left out of `entities`. See the design in
`docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md`.

### Writing a reporter

A component reports by defining **`build_discovery()`**, returning its report dict - or `None` when
it has not discovered enough to describe yet (no serial, no account) - and calling
**`self.refresh_discovery()`** once per `run()` cycle, unconditionally. That is the whole contract.
`ComponentBase.refresh_discovery()` owns the reporting loop itself, and with it three rules the
first five reporters each had to remember, and between them got wrong more than once:

- **A report is rebuilt and compared in full on every call**, never keyed on a hand-maintained
  snapshot of whatever `build_discovery()` reads. Such a key has to be kept in step with the build
  by hand, and when it drifts the report freezes in its first, incomplete state for the life of the
  process. Comparing the built report cannot drift, and needs no completeness check either: a
  report that fills in later simply differs from the stored one and replaces it. Builds are dict
  work over data already in hand, so rebuilding to compare costs nothing measurable, and nothing is
  filed unless something actually moved.
- **The marker only advances after a report is filed**, never on the failure path, so a transient
  failure is retried next cycle. This is why the call must sit *outside* any one-shot `if first:`
  gate: `first` flips to False forever the instant `run()` returns True, so a single failed cycle
  inside it could never be retried.
- **A failure is caught and logged, never raised.** An observer must not be able to degrade the
  health of the component it observes - an exception escaping `run()` withholds the success
  timestamp and eventually pushes a healthy component toward unhealthy. It is logged only, never
  through `non_fatal_error_occurred()`, which would set `had_errors` and suppress `record_status()`.

Two rules remain yours to follow:

1. **Report only entities that actually exist in the state store, never every entry in a static
   table.** An entity spec describes what a component *can* publish, not what it *has* published on
   this particular install with this particular firmware version. Pass your descriptors through
   `self.discovery_entities(descriptors)`, which keeps only those Home Assistant has actually seen -
   claiming an entity exists that Home Assistant has never seen is worse than omitting it.

2. **Never invent a record to resolve a cross-link - a dangling one is fine.** A device can point at
   another with a cross-link field (`measures_meter`, `charged_by`, ...) without the thing on the
   other end existing as a record in its own right. GE Cloud's CT-clamp cross-link is the clearest
   example: a battery can report which meter serial its own clamp measures without a `meters`
   section record ever being fabricated for that clamp, because a CT clamp is not itself a billing
   supply point. Resolving a dangling cross-link against a genuine record reported by another
   component is exactly what `observations` is for - it is not this reporter's job to guess one into
   existence.

For the `inverters` section, build each record with `inverter_record()` from `coordinator.py`
rather than assembling the dict by hand. It takes the section's fields as keyword-only parameters and
omits whatever is unset or empty, so a reporter can pass everything it gathered without writing
its own `if info: record["info"] = info` ladder. A mistyped field name is a `TypeError` at the
call site rather than a key silently dropped from a user's dump.

For `chargers` and `cars`, use `charger_record()` and `car_record()` the same way. A charger with
no vehicle data at all can use `charger_with_stub_car()`, which returns the charger and its stub
car already cross-linked (`serves_cars` / `charged_by`).
