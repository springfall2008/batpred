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

**A debug dump.** Every `predbat_debug.yaml` (written to `debug/` when
`switch.predbat_debug_enable` is on, and what you normally attach to a bug report) carries the full
catalogue under a top-level `discovery:` key. This is the complete document, and the redacted form
described below - safe to attach to a public GitHub issue.

**`sensor.predbat_discovery`.** This entity carries a summary rather than the full document: its
state is the total number of records found, and its attributes carry `schema_version`, `generated`,
a per-section count, a status for every component Predbat knows about, and any conflicts observed
(see below). The full per-section data - which inverter has which entities, and so on - is
deliberately **not** in the entity's attributes; Home Assistant's recorder writes every attribute
change to disk, and the full catalogue is tens of kilobytes even on a modest install. Read the debug
dump for the complete picture.

Every report a component has ever filed is kept, and the catalogue is re-assembled fresh each time
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
| `inverters` | Battery inverters and PV-only devices - type, composition (direct/gateway/EMS), which functions it serves (`solar`, `battery`), and its entities |
| `chargers` | EV chargers, cross-linked to the cars they serve |
| `cars` | Electric vehicles, cross-linked to the charger that charges them |
| `meters` | Electricity (and gas) supply points, each with a direction (`import`/`export`) and, where known, a nested tariff record |
| `forecasts` | Solar forecast providers (Solcast, forecast.solar, Open-Meteo, or your own HA sensors) and what each one covers |
| `programmes` | Flexibility enrolments (a VPP, a saving session, a free-electricity event) that emit events and may constrain Predbat, cross-linked to the meter they apply to |

No v1 reporter (GivTCP, GE Cloud, Octopus, Ohme, Solcast) populates `programmes` yet - it is part of
the schema for a future Axle/VPP-style reporter - so today it is always present as an empty list
rather than missing from the document.

Every record carries a `source` field naming the component that reported it, a `device_id` unique
within that component, and whichever typed containers below the component chose to populate. Two
extra top-level sections describe the fleet as a whole rather than any one device:

- **`components`** - a status (`ok`, `no_report`, `not_started`, `load_error` or `not_configured`)
  for *every* component Predbat's registry knows about, not only the ones that reported something -
  so a component that should be describing your hardware but is not shows up as clearly as one that
  is
- **`observations`** - things noticed about the assembled picture rather than about any one
  component: `conflicts` (see below) and `resulting_config`, the handful of apps.yaml keys
  (`num_inverters`, `num_cars`, `inverter_type`) discovery can be compared against

### Conflicts

`observations.conflicts` records situations that today resolve silently by whichever component
happened to run first - the catalogue observes them, it does not resolve them:

| Kind | Fires when |
| ---- | ---------- |
| `duplicate_serial` | Two components report an inverter with the same hardware serial |
| `multiple_inverter_sources` | More than one component reports an inverter record at all (e.g. both GivTCP and GE Cloud) |
| `multiple_import_meters` | More than one component reports an import meter |
| `contested_car_slots` | A car is reported by a component that reports no charger, alongside a component that reports both a charger and a car - two components with a different idea of what should occupy a car slot |

## What is, and is not, redacted

The document you get from a debug dump or `sensor.predbat_discovery` has already been through
redaction - the catalogue in memory is never published in its raw form. Two classes of information
are treated very differently, deliberately:

**Pseudonymised** - never appears in the clear: account and site identifiers that trace back to
*you specifically* - an MPAN, an Octopus/utility account number, a Solcast site id. Each is replaced
with a short stable token (`#` followed by 8 hex characters) derived from the value and a random
salt generated once per installation, so the *same* identifier always maps to the *same* token
within one install's dumps (letting you correlate two records that share an account), but a
different installation's token for the identical MPAN is completely unrelated - there is nothing to
compare across users. Anywhere that identifier would otherwise be echoed elsewhere in the document -
inside an entity id, a free-text note - is caught and replaced too, including a case- or
`-`/`_`-folded form the same value can appear in (either direction: an upper-cased echo of a
lower-cased original is caught exactly as a lower-cased echo of an upper-cased one is). This
echo-substitution only fires for an original of six characters or more - anything shorter would
corrupt more ordinary text as a false-positive substring match than it would ever hide, so a very
short identifier is only ever replaced where it appears whole, not embedded inside a longer string.

**Kept readable, deliberately** - a hardware serial number, a firmware version string, a device
model name, and a tariff or product code. None of these identify *you*; they identify a public
product or a specific physical device, and stripping them would make a bug report undiagnosable -
"my GivEnergy inverter won't discharge" is a much harder bug to chase without knowing which
inverter, which firmware, or which tariff is in play. **Entity ids are published verbatim too** -
exactly the `sensor.`/`number.`/... id Home Assistant knows the entity by - since that is what lets
you match a catalogue record back to something you can see on your own dashboard; a component is
expected to build its entity ids from public naming, never from a value this catalogue treats as
sensitive (and where one is, the redactor still catches it - see the guards below).

A handful of further guards run regardless of which container a value landed in: anything that
looks like a credential by its field name (`api_key`, `password`, `token`, ...) is refused outright,
wherever it is nested; a value that looks like a misfiled identifier is pseudonymised even inside a
container that is not supposed to hold one - a 10-or-more-digit run is enough anywhere it turns up
in a clear container's value (embedded in a longer string too, e.g. `"MPAN 1234567890123"`), except
inside `hardware_ids`, where a value is only flagged when it is *nothing but* digits, so a
letter-prefixed vendor serial like `HV2160123456` stays readable; and a field literally named
`latitude`, `longitude` or `postcode` is pseudonymised regardless of what it contains, since a
location cannot otherwise be recognised from one value alone. A debug dump is safe to attach to a
public issue; **the equivalent in-process, unredacted view exists only for Predbat's own internal
diagnostics and must never be written anywhere.**

One visible side effect of pseudonymisation worth knowing about when reading a dump: a `device_id`
built from a sensitive identifier is replaced *wholesale* by its pseudonym token rather than having
just that part of it swapped out. Octopus's meter `device_id` is `"octopus:{mpan}"`, so a
pseudonymised one loses its `"octopus:"` prefix entirely and reads as a bare `#` token, unlike a
`device_id` in most other sections. Nothing is lost functionally - cross-links between records still
resolve to the same token, and the record is still tagged with its `source` - but a maintainer
comparing sections will notice the inconsistency and should not have to wonder whether it is a bug.

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
small set of **containers**, each declaring the *type* of value it accepts and the *redaction
class* that value gets:

| Container | Accepts | Redaction |
| --------- | ------- | --------- |
| `hardware_ids` | Short strings | Clear |
| `info` | Short strings, no `@` | Clear |
| `ratings` | Numbers and booleans | Clear |
| `coverage` | A number/boolean, or a list of lowercase tokens | Clear |
| `entities` | An entity descriptor (`entity_id` plus typed fields like `domain`, `unit`, `min`/`max`) | Clear |
| `account_ids` | Any scalar | Pseudonymised |
| `functions` / `capabilities` / `flags` / `effects` | Lists of short lowercase tokens | Clear |

A value that does not fit its container's declared type is silently **dropped**, not coerced and
not raised as an error - a string offered to `ratings`, or free text over 64 characters offered to
`info`, simply never reaches the assembled catalogue. This is the actual safety property the design
rests on: it does not depend on enumerating every dangerous field name a component might one day
introduce, because a container that only accepts numbers structurally cannot carry a name, an
address or a pasted credential, however the schema grows. Choose whichever container matches the
*kind* of fact you are reporting, not the one that happens to accept the value you have.

### Four rules the five existing reporters paid for

GivTCP, GE Cloud, Octopus, Ohme and Solcast each report today, and between them ran into the same
handful of mistakes more than once. None of these are enforced by the coordinator - they are
conventions a new reporter has to follow itself:

1. **Report only entities that actually exist in the state store, never every entry in a static
   table.** An entity spec describes what a component *can* publish, not what it *has* published on
   this particular install with this particular firmware version. Check `get_state_wrapper(entity_id)
   is not None` for each one before including it - claiming an entity exists that Home Assistant has
   never seen is worse than omitting it.
2. **Compare your reported-marker outside any one-shot `if first:` gate.** A component's startup
   path commonly runs certain setup exactly once, guarded by a `first` flag that never fires again
   for the life of the process. If the discovery report is built only inside that gate, a single
   transient failure on that one cycle (a bug, a slow API) loses the report forever, even once
   whatever caused it is fixed. Keep your own out-of-band marker (a snapshot of whatever
   `build_discovery()` depends on) and compare it on *every* `run()` cycle, unconditionally, so a
   failed or incomplete attempt is simply retried the next time round.
3. **Wrap the report call in try/except.** `build_discovery()`/`report_discovery()` is a side
   channel, not the component's actual job - a bug in it must never be allowed to propagate out of
   `run()` and withhold the success timestamp, which is what would otherwise happen. An observer
   must never be able to degrade the health of the thing it is observing.
4. **Never invent a record to resolve a cross-link - a dangling one is fine.** A device can point at
   another with a cross-link field (`measures_meter`, `charged_by`, ...) without the thing on the
   other end existing as a record in its own right. GE Cloud's CT-clamp cross-link is the clearest
   example: a battery can report which meter serial its own clamp measures without a `meters`
   section record ever being fabricated for that clamp, because a CT clamp is not itself a billing
   supply point. Resolving a dangling cross-link against a genuine record reported by another
   component is exactly what `observations` is for - it is not this reporter's job to guess one into
   existence.
