# Discovery Coordinator — Minimal Implementation Design

Date: 2026-09-06. Status: agreed with maintainer (this session).

## Problem

Predbat components auto-configure by racing to overwrite shared apps.yaml keys
(`num_inverters`, `inverter_type`, the ~40 per-inverter entity lists, `num_cars`, the
per-car lists). There is no arbitration: within phase 1 the winner is whichever
component's `automatic_config()` runs last (dict order in `COMPONENT_LIST`, serialized by
`wait_api_started()`). Consequences:

- Two inverter components cannot both be in automatic mode: each writes `num_inverters`
  and replaces whole entity lists (e.g. `gecloud.py` even deletes `givtcp_rest` to win).
- Slot identity is positional. Only the Gateway sorts devices by serial (2026-06-04
  incident: NVS wipe re-ordered discovery and bound control to the wrong slot).
- `num_cars` is a max-of-claims; Ohme hard-wires car 0; `base.car_slot_owner` is the only
  (two-party) arbitration.
- Sensor-only devices (e.g. extra PV inverters) have no home distinct from control slots.

## Decisions (agreed)

1. **Coordinator assigns, components write.** Components report *discovery offers* to a
   central coordinator during their first run instead of auto-configuring. After phase-1
   startup completes, the coordinator allocates inverter slots, car indices and sensor
   indices, then each component's `apply_assignment()` writes its own apps.yaml keys at
   its assigned indices. The alternative (coordinator performs the whole automatic config
   from submitted sensor lists) centralises every vendor quirk and is rejected as too big;
   the coordinator writes only the *fleet-shape* keys (`num_inverters`, `inverter_type`,
   `num_cars`).
2. **Serial start stays.** No parallel component startup. The "all components started or
   timed out" barrier is simply the return of `Components.start(phase=1)`.
3. **The `*_automatic` apps.yaml keys stay.** Discovery-driven allocation only powers what
   `automatic` powers today; with it off, entities are still published and the user
   configures apps.yaml manually, exactly as now.
4. **User apps.yaml still wins per the existing rules.** The `args_from_apps_yaml`
   snapshot / `set_arg_auto()` semantics are preserved, including the history-bearing
   energy keys rule from #4959 (`overwrite=False`).
5. **Sensors get their own assignment.** A component can offer more energy/PV sensors than
   control slots (`pv_today` etc. are *summed* lists, unlike the slot-indexed control
   keys). The coordinator assigns list indices per summed key; components write entries
   with `set_arg(key, value, index=i)`.
6. **Minimal migration set:** GivTCP, GECloud, Octopus (cars + tariff record), Ohme.
   All other components keep today's behaviour untouched. Kraken keeps reading
   `base.car_slot_owner`, so the coordinator still sets it for parity.

## Architecture

New module `apps/predbat/coordinator.py`, built around a **discovery catalogue**: a plain,
JSON-serialisable dict. Discovery records are dictionaries, not typed classes — components
can attach any vendor facts they discover (model, firmware, battery ratings — the facts
found being discarded today) beyond the required keys, and the whole catalogue can be
persisted, published on the diagnostics sensor, included in debug dumps, and shipped to
predbat.com without a conversion layer. A validation helper warns on missing required keys
and drops malformed entries (the typo-safety typed classes would have given), never
crashing allocation on a bad vendor report.

One report per component, a dict with optional sections:

- `"automatic"`: bool (default True) — offers from a non-automatic component are catalogued
  but excluded from allocation.
- `"inverters"`: list of dicts, one per **slot candidate** (after the component's own
  topology rules — GECloud's gateway collapse and EMS fan-out stay inside GECloud).
  Required keys: `device_key`, `inverter_type`; well-known: `serial`; anything else is
  carried verbatim into the catalogue.
- `"sensors"`: list of dicts, one per entry the component wants in a summed list key
  (`load_today`, `import_today`, `export_today`, `pv_today`, and GECloud's split-PV
  extras). Required keys: `device_key`, `key`, `entity_id`.
- `"cars"`: list of dicts. Required keys: `device_key`, `kind` (`"charger"` for Ohme,
  `"intelligent_device"` for Octopus IOG); well-known: `octopus_intelligent` (the
  charger's tri-state wish).
- `"tariff"`: dict with well-known keys `import_code`, `export_code`, `is_intelligent_go`,
  `has_six_hour_cap` — reported by Octopus, exposed via `Coordinator.get_tariff()`.
  (Ohme's IOG auto-detection moves from peeking at the Octopus component into the
  coordinator's car allocation.)

- `Assignment` — what a component receives; deliberately a class, not a dict: it is the
  in-process API the components consume. Fields: `inverter_slots {device_key: slot}`,
  `sensor_indices {key: {device_key: index}}`, `skip_keys` (user-configured summed keys),
  `car_slots {device_key: car_index}`, `octopus_intelligent_owner` (bool, for Ohme),
  totals; `to_dict()` for the catalogue.
- `Coordinator(base)` — thread-safe (`threading.Lock`; components report from their own
  threads):
  - `report(component_name, report)` — validates and stores one component's report dict;
    idempotent (a re-report replaces); a re-report after startup (GivTCP re-probe
    adoption, Octopus device-set change) triggers reallocation.
  - `catalogue()` — the JSON-safe dict of every report plus the current allocation;
    `json.dumps(coordinator.catalogue())` must always succeed.
  - `allocate()` — deterministic: offers ordered by `COMPONENT_LIST` position then
    component-reported order; duplicate serials deduped (first claim wins, logged);
    slot **stickiness** via a map persisted through the Storage component
    (`storage.save("coordinator", "allocation", ...)`, called with `run_async()`):
    devices keep their relative order across restarts, new devices append, slots are
    compacted 0..N-1 (no holes — `Inverter` cannot skip an index). Offers from
    components with `automatic=False` are excluded from allocation.
  - Writes fleet-shape keys via the `set_arg_auto` logic (moved to a module-level
    function so the coordinator and `ComponentBase` share it).
  - `allocate_and_apply()` — called from `PredBat.initialize()` between
    `start(phase=1)` and `initialize(phase=2)`: allocate, post each reporting
    component's `Assignment`, wait for all applies (poll, 60 s timeout).
  - Publishes `sensor.predbat_coordinator` with the allocation as attributes for
    supportability.

`ComponentBase` additions: `pending_assignment` checked every 5 s tick of the existing
`start()` loop, dispatched to an overridable `async def apply_assignment(assignment)` on
the component's **own** thread/event loop (no cross-thread calls into component state);
`assignment_applied` flag; `report_discovery(...)` convenience. `set_arg` gains an
`index=` pass-through.

### Car allocation rule (parity with today)

Resolve Ohme's tri-state (`ohme_automatic_octopus_intelligent`: explicit value wins;
unset ⇒ auto = `ohme_automatic` and tariff record says Intelligent Go). If Ohme wins it
gets a car slot and `octopus_intelligent_owner=True`; Octopus's intelligent devices get
**no** car slots (today's `car_slot_owner` behaviour — improving this to allocate the
remaining devices as extra cars is a noted future enhancement, not in scope). Otherwise
Octopus devices are allocated sorted by device id. `num_cars` is never lowered below the
user's configured value.

### Slot-aligned `givtcp_rest`

`inverter_type[n] == "GE"` makes `Inverter(n)` read `givtcp_rest[n]` directly for REST
control, so GivTCP's apply must write its REST URLs at its **assigned** slots (padding
with `None`), and `GivTCPComponent` must tolerate `None` entries in `rest_urls` (skip,
preserving positional ids) so a component restart re-reading the rewritten arg works.

### Known limitations carried forward (unchanged from today)

- Mixed manual + automatic multi-vendor fleets: fleet-shape keys are written with
  `set_arg_auto(overwrite=True)`, so automatic wins with the standard logged note.
- 4-inverter ceilings in `set_reserve_min_N` config items and `create_missing_arg`.
- Unmigrated components (Kraken, DEYE, Sunsynk, …) still write whole lists; enabling one
  of those alongside a migrated one behaves as today.

## Testing

Headline requirement: **multi-inverter automatic mode across two components** — GivTCP
(mocked REST, one discovered inverter) + GECloud (`MockGECloudDirect`, one battery) both
automatic, driven through report → allocate → apply, asserting the merged args:
`num_inverters == 2`, `inverter_type == ["GE", "GEC"]`, each per-inverter list holds each
vendor's entity at its assigned index, and summed keys contain both vendors' sensors.
Plus: coordinator unit tests (determinism, dedup, stickiness/compaction, sensor-only
offers, user-configured skip keys, car tri-state × tariff cases), per-component
offset tests (assigned slots not starting at 0), and regression that non-automatic and
unmigrated paths are untouched. All registered in `TEST_REGISTRY`.
