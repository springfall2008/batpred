# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Discovery bindings: which inverter slot each discovered record drives, and what it binds there.

Pure functions and constants only - nothing here reads Home Assistant, writes args, logs or holds
state. The coordinator calls them with the apps.yaml snapshot (args exactly as the user wrote them),
the live args and the args it scans for serials (its first-pass baseline), all plain dicts. See
docs/superpowers/specs/2026-11-14-discovery-automatic-inverter-config-design.md.
"""

import copy
from functools import lru_cache

from utils import is_entity_id

# Slots the coordinator will allocate; a record with no slot left below this is not configured
MAX_INVERTERS = 4

# A serial shorter than this matches too many unrelated strings to identify a manual slot
MIN_SERIAL_LENGTH = 6

# Bound once for the whole site, from the lowest-numbered discovered slot that carries it
SITE_SETTINGS = frozenset({"battery_temperature_history"})

# Summed whatever their length, written compact: each slot's entry in slot order, then the user's extras
COMPACT_SETTINGS = ("load_today", "import_today", "export_today", "pv_today")

# Lists whose length is not tied to num_inverters; pv_power stays indexed by slot, its extras counting as extra PV
SUMMED_SETTINGS = COMPACT_SETTINGS + ("pv_power",)

# The service API's control keys: not indexed by inverter, so read when detecting a manual slot and never written
CONTROL_SERVICE_SETTINGS = ("charge_start_service", "charge_stop_service", "discharge_start_service", "discharge_stop_service", "charge_freeze_service", "discharge_freeze_service")

# The settings the coordinator owns. Membership rule: a per-inverter setting that a reporter's record binds,
# that is one the component's automatic configuration sets today, plus givtcp_rest. APPS_SCHEMA membership
# is not the test - several settings inverter.py reads by name (schedule_write_button, battery_min_soc) have
# no schema entry. Nor is it "every APPS_SCHEMA key with entries: num_inverters": user tuning such as
# inverter_limit_charge or soc_max_fallback is never cleared here (tests/discovery_contract.py REPORTED_TUNING).
# battery_charge_today and battery_discharge_today have not been checked for whether they are summed lists
# like load_today; that is settled when a component that binds them is converted (GE Cloud does not).
BINDING_SETTINGS = frozenset(
    {
        # GE Cloud's _device_entities()
        "battery_power",
        "battery_rate_max",
        "battery_scaling",
        "battery_temperature",
        "charge_end_time",
        "charge_limit",
        "charge_limit_enable",
        "charge_rate",
        "charge_rate_percent",
        "charge_start_time",
        "discharge_end_time",
        "discharge_rate",
        "discharge_rate_percent",
        "discharge_start_time",
        "discharge_target_soc",
        "export_limit",
        "export_today",
        "grid_power",
        "idle_end_time",
        "idle_start_time",
        "import_today",
        "inverter_limit",
        "inverter_mode",
        "inverter_time",
        "load_power",
        "load_today",
        "pause_end_time",
        "pause_mode",
        "pause_start_time",
        "pv_power",
        "pv_today",
        "reserve",
        "scheduled_charge_enable",
        "scheduled_discharge_enable",
        "soc_max",
        "soc_percent",
        # The other reporters' records
        "battery_calibration",
        "battery_charge_today",
        "battery_discharge_today",
        "battery_min_soc",
        "battery_size_power",
        "battery_voltage",
        "schedule_write_button",
        "soc_kw",
        # GE's REST control, bound per slot although no record's entities carry it
        "givtcp_rest",
    }
)


@lru_cache(maxsize=None)
def modify_settings() -> tuple[str, ...]:
    """The APPS_SCHEMA settings Predbat writes to, sorted: a bound entity on one of these gives a slot a control method.

    config is imported here, not at module level: config imports predbat, which imports config, so a module-level
    import meant this module - and coordinator and every reporter importing it - could not be imported first in a
    fresh process, which is how the standalone component CLIs start. By the time a pass calls this, Predbat is loaded.
    """
    from config import APPS_SCHEMA

    return tuple(sorted(name for name, schema in APPS_SCHEMA.items() if schema.get("modify")))


def slot_value(value, index):
    """The entry of an args value for one inverter: a list's entry at index (None past its end), a scalar for index 0 only."""
    if isinstance(value, (list, tuple)):
        return value[index] if index < len(value) else None
    return value if index == 0 else None


def _num_inverters(apps_yaml):
    """The num_inverters the snapshot gives, as an int; 1 when it is missing or not a number."""
    try:
        return int(apps_yaml.get("num_inverters", 1))
    except (TypeError, ValueError):
        return 1


def _has_control_method(apps_yaml, index):
    """Whether the snapshot gives inverter `index` a way to be controlled: a writable entity, a givtcp_rest URL or a control service."""
    for setting in modify_settings():
        value = slot_value(apps_yaml.get(setting), index)
        if isinstance(value, str) and (is_entity_id(value) or value.startswith("re:")):
            return True
    rest = slot_value(apps_yaml.get("givtcp_rest"), index)
    if isinstance(rest, str) and rest.strip():
        return True
    return any(apps_yaml.get(setting) for setting in CONTROL_SERVICE_SETTINGS)


def manual_slots(apps_yaml: dict) -> list[dict]:
    """One entry per index below apps_yaml's num_inverters (default 1): {"slot", "origin": "manual"|"free", "inverter_type"}.

    A slot is manual when the snapshot gives it both an inverter_type and a control method. A slot with
    controls but no inverter_type is a dead slot: free, with inverter_type None, for a discovered
    inverter to take.
    """
    slots = []
    for index in range(_num_inverters(apps_yaml)):
        inverter_type = slot_value(apps_yaml.get("inverter_type"), index)
        if not isinstance(inverter_type, str) or not inverter_type.strip():
            inverter_type = None
        origin = "manual" if inverter_type is not None and _has_control_method(apps_yaml, index) else "free"
        slots.append({"slot": index, "origin": origin, "inverter_type": inverter_type})
    return slots


def slot_serials(slot: int, apps_yaml: dict, args: dict, serials: list[str]) -> list[str]:
    """Which of `serials` (already casefolded) appear in the strings bound at this slot.

    The strings are every per-inverter setting's entry at this slot in `args` - build_bindings passes
    the args it scans, which the coordinator gives as its first-pass baseline (re: patterns resolved,
    nothing yet replayed) - plus ge_cloud_serial for slot 0 as the apps.yaml snapshot has it, never the
    live value, which GE Cloud sets to its own first battery. Serials shorter than MIN_SERIAL_LENGTH
    are ignored.

    SUMMED_SETTINGS are not scanned: those lists are not indexed by slot (the compact ones leave unbound
    slots out, and all of them can carry the user's extras), so their entry at this index may belong to
    another slot or to no inverter at all. Every control and per-inverter sensor is still scanned, so no
    identifying string is lost.
    """
    bound = []
    for setting in sorted(BINDING_SETTINGS - set(SUMMED_SETTINGS)):
        value = slot_value(args.get(setting), slot)
        if isinstance(value, str):
            bound.append(value.casefold())
    if slot == 0 and isinstance(apps_yaml.get("ge_cloud_serial"), str):
        bound.append(apps_yaml["ge_cloud_serial"].casefold())
    return [serial for serial in serials if isinstance(serial, str) and len(serial) >= MIN_SERIAL_LENGTH and any(serial.casefold() in text for text in bound)]


def slot_map(bindings: dict) -> dict[str, int]:
    """device_id -> slot for every discovered slot in a bindings dict (empty for None or {})."""
    if not bindings:
        return {}
    return {entry["device_id"]: entry["slot"] for entry in bindings.get("inverters", []) if entry.get("origin") == "discovered" and entry.get("device_id")}


def _manual_descriptor(value):
    """A manual slot's binding for one args value: entity_id for an entity, value for a literal, never access (args do not say)."""
    return {"entity_id": value} if is_entity_id(value) else {"value": value}


def _free_entry(slot, inverter_type=None):
    """A free slot's bindings entry: nothing bound, no source."""
    return {"slot": slot, "origin": "free", "source": None, "device_id": None, "inverter_type": inverter_type, "serials": [], "settings": {}}


def _is_driven(record):
    """Whether a record is an inverter Predbat drives (it has an inverter_type), as opposed to a PV-only device."""
    return isinstance(record.get("inverter_type"), str) and bool(record["inverter_type"].strip())


def _record_entities(record):
    """A record's entities map, {} when it has none."""
    return record.get("entities") if isinstance(record.get("entities"), dict) else {}


def _record_serials(record):
    """A record's serials, sorted and casefolded: hardware_ids.serial plus its structural serials."""
    hardware = record.get("hardware_ids") if isinstance(record.get("hardware_ids"), dict) else {}
    found = [hardware.get("serial")] + list(record.get("serials") or [])
    return sorted({serial.casefold() for serial in found if isinstance(serial, str) and serial})


def _discovered_entry(source, record):
    """A discovered slot's entry for a fresh record (slot filled in on placement); site settings are left out of its settings."""
    settings = {name: copy.deepcopy(descriptor) for name, descriptor in _record_entities(record).items() if name not in SITE_SETTINGS}
    return {
        "slot": None,
        "origin": "discovered",
        "source": source["name"],
        "device_id": record["device_id"],
        "inverter_type": record["inverter_type"],
        "serials": _record_serials(record),
        "settings": settings,
        "skip_settings": sorted(source.get("skip_settings") or ()),
        "keep_user_settings": sorted(source.get("keep_user_settings") or ()),
    }


def _manual_entries(apps_yaml, args, scan_args, candidate_serials):
    """Step 1 and 2: one entry per index below the manual num_inverters, each manual slot carrying its settings and scanned serials.

    Summed settings come from the apps.yaml snapshot, since replay rewrites those lists in live args and a
    live index stops meaning a slot after the first pass; every other binding setting comes from live args
    at the slot index, where re: patterns are resolved. Serials are scanned in scan_args, never live args:
    replay rewrites the entries at every slot it takes over or clears, so a scan of live args stops finding
    the serial that matched on the first pass and the slot comes back as manual on the next.
    """
    entries = []
    for slot in manual_slots(apps_yaml):
        index = slot["slot"]
        if slot["origin"] != "manual":
            entries.append(_free_entry(index, slot["inverter_type"]))
            continue
        settings = {}
        for setting in sorted(BINDING_SETTINGS - set(SUMMED_SETTINGS)):
            value = slot_value(args.get(setting), index)
            if value is not None:
                settings[setting] = _manual_descriptor(value)
        for setting in SUMMED_SETTINGS:
            value = slot_value(apps_yaml.get(setting), index)
            if value is not None:
                settings[setting] = _manual_descriptor(value)
        serials = sorted(slot_serials(index, apps_yaml, scan_args, candidate_serials))
        entries.append({"slot": index, "origin": "manual", "source": None, "device_id": None, "inverter_type": slot["inverter_type"], "serials": serials, "settings": settings})
    return entries


def _place(slots, target, entry, matched, warnings):
    """Put entry in slot target, warning on a manual slot taken over and clearing every other manual slot that names the record.

    target is only ever a manual slot that matches the entry by serial, so the takeover warning always has a shared serial to name.
    """
    entry["slot"] = target
    current = slots[target]
    if current["origin"] == "manual":
        serial = sorted(set(current["serials"]) & set(entry["serials"]))[0]
        warnings.append("Inverter slot {}: {} inverter {} is also configured manually in apps.yaml - discovery is taking over this slot".format(target, entry["source"], serial))
    slots[target] = entry
    for other in matched:
        if other["slot"] != target and slots[other["slot"]] is other:
            serial = sorted(set(other["serials"]) & set(entry["serials"]))[0]
            warnings.append("Inverter slot {}: also names {}, already configured in slot {} - clearing this slot".format(other["slot"], serial, target))
            slots[other["slot"]] = _free_entry(other["slot"])


def _allocate(slots, placements, warnings):
    """Step 3: place each (entry, sticky slot) in order - a sticky slot first, then the lowest matching manual slot, then the lowest free slot.

    A sticky slot is honoured only when it is free, or manual and matching the entry by serial: a manual inverter
    that shares no serial with a discovered one is never changed. Otherwise the entry - fresh or retained - takes
    the normal path, and with no slot left it is dropped with the no-room warning.

    Two passes: every sticky entry that can keep its slot is placed first, so a displaced one cannot take a free
    slot a later sticky entry still owns. The displaced entries then take the normal path, ahead of new records.
    """
    deferred = []
    for entry, sticky in placements:
        matched = [slot for slot in slots if slot["origin"] == "manual" and set(slot["serials"]) & set(entry["serials"])]
        if sticky is not None and sticky < len(slots) and (slots[sticky]["origin"] == "free" or any(slot is slots[sticky] for slot in matched)):
            _place(slots, sticky, entry, matched, warnings)
        else:
            deferred.append(entry)
    for entry in deferred:
        matched = [slot for slot in slots if slot["origin"] == "manual" and set(slot["serials"]) & set(entry["serials"])]
        if matched:
            target = matched[0]["slot"]
        else:
            target = next((slot["slot"] for slot in slots[:MAX_INVERTERS] if slot["origin"] == "free"), None)
            if target is None:
                warnings.append("{} inverter {} cannot be configured - all {} inverter slots are in use".format(entry["source"], entry["device_id"], MAX_INVERTERS))
                continue
        _place(slots, target, entry, matched, warnings)


def _site_extras(apps_yaml, sources):
    """The summed-setting entries that belong to no slot: the user's apps.yaml extras in order, then PV-only devices' entity ids where included."""
    num_inverters = max(_num_inverters(apps_yaml), 0)
    extras = {}
    for setting in SUMMED_SETTINGS:
        value = apps_yaml.get(setting)
        values = list(value) if isinstance(value, (list, tuple)) else ([] if value is None else [value])
        entries = [entry for entry in values[num_inverters:] if entry is not None]
        if setting in ("pv_power", "pv_today"):
            for source in sources:
                if not source.get("include_pv_only"):
                    continue
                for record in source.get("records") or []:
                    descriptor = _record_entities(record).get(setting)
                    if not _is_driven(record) and isinstance(descriptor, dict) and descriptor.get("entity_id"):
                        entries.append(descriptor["entity_id"])
        if entries:
            extras[setting] = entries
    return extras


def _site_settings(slots, fresh_records, previous):
    """SITE_SETTINGS from the lowest-numbered discovered slot whose fresh record carries each; a retained slot's come from previous."""
    settings = {}
    for entry in slots:
        record = fresh_records.get(entry["device_id"]) if entry["origin"] == "discovered" else None
        for name in sorted(SITE_SETTINGS):
            if record is not None and name not in settings and name in _record_entities(record):
                settings[name] = copy.deepcopy(_record_entities(record)[name])
    if any(entry["origin"] == "discovered" and entry["device_id"] not in fresh_records for entry in slots):
        for name, descriptor in ((previous or {}).get("site") or {}).get("settings", {}).items():
            settings.setdefault(name, copy.deepcopy(descriptor))
    return settings


def build_bindings(apps_yaml: dict, args: dict, sources: list[dict], previous: dict | None, scan_args: dict | None = None) -> tuple[dict, list[str]]:
    """Return (bindings, warnings) for the apps.yaml snapshot, live args, the opted-in sources and the previous bindings. Writes nothing.

    sources: one dict per opted-in component, {"name", "records", "skip_settings", "keep_user_settings",
    "include_pv_only"}. previous: the bindings the last call returned, or None. A record previous placed
    keeps its slot. A source that reports no driven record, or is absent from sources, keeps every slot
    previous gave it, copied verbatim; only a source reporting at least one driven record can lose a slot.
    scan_args: the args a manual slot's serials are scanned in, args when None. The coordinator passes the
    args as they were before its first replay, so a slot taken over or cleared on one pass matches again on
    the next, however replay has rewritten its live entries.
    """
    warnings = []
    previous_entries = [entry for entry in (previous or {}).get("inverters", []) if entry.get("origin") == "discovered"]
    previous_slots = slot_map(previous)

    fresh = []
    seen_device_ids = set()
    driven_sources = set()
    for source in sorted(sources, key=lambda item: item["name"]):
        driven = [record for record in source.get("records") or [] if _is_driven(record)]
        if not driven:
            warnings.append("{} reported no inverter to configure - keeping its existing slots".format(source["name"]))
            continue
        driven_sources.add(source["name"])
        for record in driven:
            # Nothing upstream stops a report repeating a device_id: the first record is placed, any later one ignored
            if record["device_id"] not in seen_device_ids:
                seen_device_ids.add(record["device_id"])
                fresh.append((source, record))
    retained = [copy.deepcopy(entry) for entry in previous_entries if entry.get("source") not in driven_sources]

    candidate_serials = sorted({serial for _, record in fresh for serial in _record_serials(record)} | {serial for entry in retained for serial in entry.get("serials", [])})
    slots = _manual_entries(apps_yaml, args, args if scan_args is None else scan_args, candidate_serials)
    slots.extend(_free_entry(index) for index in range(len(slots), MAX_INVERTERS))

    sticky = [(entry, entry["slot"]) for entry in retained]
    placements = []
    for source, record in fresh:
        entry = _discovered_entry(source, record)
        if record["device_id"] in previous_slots:
            sticky.append((entry, previous_slots[record["device_id"]]))
        else:
            placements.append((entry, None))
    _allocate(slots, sorted(sticky, key=lambda item: item[1]) + placements, warnings)

    while slots and slots[-1]["origin"] == "free":
        slots.pop()
    for entry in slots:
        if entry["origin"] == "free" and entry["inverter_type"] is None:
            warnings.append("Inverter slot {} is empty below a configured inverter - it has no inverter_type and Predbat will stop on it".format(entry["slot"]))

    fresh_records = {record["device_id"]: record for _, record in fresh}
    site = {"settings": _site_settings(slots, fresh_records, previous), "extras": _site_extras(apps_yaml, sources)}
    return {"inverters": slots, "site": site}, warnings


def _descriptor_value(descriptor):
    """What a binding writes: its entity_id, else its value; None when there is no binding."""
    if not isinstance(descriptor, dict):
        return None
    return descriptor["entity_id"] if "entity_id" in descriptor else descriptor.get("value")


def _discovered_value(entry, setting, apps_yaml, args):
    """A discovered slot's entry for one binding setting.

    A skipped setting, or a kept one the snapshot gives a value at this slot (a re: pattern counts), is not
    touched (a kept one the snapshot gives as a single value is not decided at all - see desired_args()): a slot-indexed one keeps the live args entry, which may be a resolved pattern or another writer's
    value. A summed one takes the snapshot's entry, since replay rewrites those lists and a live index stops
    meaning a slot. Otherwise it is the bound entity id or value, None when unbound - except pv_power, which is a fixed 0.
    """
    snapshot = slot_value(apps_yaml.get(setting), entry["slot"])
    if setting in entry.get("skip_settings", ()) or (setting in entry.get("keep_user_settings", ()) and snapshot is not None):
        return snapshot if setting in SUMMED_SETTINGS else slot_value(args.get(setting), entry["slot"])
    value = _descriptor_value(entry["settings"].get(setting))
    return 0 if value is None and setting == "pv_power" else value


def _slot_entry(entry, setting, apps_yaml, args):
    """One slot's entry for inverter_type or a binding setting, before compacting and extras.

    A manual slot keeps the live args entry, except for the summed settings, which come from its own
    settings (taken from the snapshot by build_bindings). A free slot has its inverter_type and nothing else.
    """
    if entry["origin"] == "discovered":
        return entry["inverter_type"] if setting == "inverter_type" else _discovered_value(entry, setting, apps_yaml, args)
    if entry["origin"] == "manual":
        return _descriptor_value(entry["settings"].get(setting)) if setting in SUMMED_SETTINGS else slot_value(args.get(setting), entry["slot"])
    return entry["inverter_type"] if setting == "inverter_type" else None


def desired_args(bindings: dict, apps_yaml: dict, args: dict) -> dict:
    """The full value of every arg the bindings decide: {setting: list-or-scalar}, None for a key that should be absent. Pure.

    Decided: num_inverters (the slot count), inverter_type and every BINDING_SETTINGS member, and the site
    settings the bindings carry. Slot-indexed settings are lists as long as the slot count; COMPACT_SETTINGS
    are the slots' entries in slot order with None left out, then site extras; pv_power is slot-indexed, then
    its extras. A list whose every entry is None is None. With no discovered slot nothing is decided: {}.

    A kept setting (a discovered slot's keep_user_settings) that the snapshot gives as a single value - not a
    list, and not None or absent; 0 and False count - is the user's for the whole key: nothing is decided for
    it, so args keep it exactly as they hold it, for every slot, manual or discovered. That is what
    set_arg_auto(..., overwrite=False) did: it kept the user's whole value, never only its first entry. A list
    value is per slot, as _discovered_value() describes.
    """
    entries = (bindings or {}).get("inverters", [])
    if not any(entry.get("origin") == "discovered" for entry in entries):
        return {}
    site = bindings.get("site") or {}
    extras = site.get("extras") or {}
    kept = {setting for entry in entries if entry.get("origin") == "discovered" for setting in entry.get("keep_user_settings", ())}
    user_owned = {setting for setting in kept if apps_yaml.get(setting) is not None and not isinstance(apps_yaml.get(setting), (list, tuple))}
    desired = {"num_inverters": len(entries)}
    for setting in sorted((BINDING_SETTINGS | {"inverter_type"}) - user_owned):
        values = [_slot_entry(entry, setting, apps_yaml, args) for entry in entries]
        if setting in COMPACT_SETTINGS:
            values = [value for value in values if value is not None]
        if setting in SUMMED_SETTINGS:
            values += list(extras.get(setting) or [])
        desired[setting] = values if any(value is not None for value in values) else None
    for name, descriptor in (site.get("settings") or {}).items():
        desired[name] = _descriptor_value(descriptor)
    return desired


def _entry_writes(desired, live):
    """The indices to write one at a time to turn a live list into desired, or None when it takes one whole-value write.

    A whole-value write is needed when live is not a list, is longer than desired, or an entry must become
    None: set_arg(setting, None, index) would delete the whole key.
    """
    if not isinstance(live, list) or len(live) > len(desired):
        return None
    changed = [index for index, value in enumerate(desired) if slot_value(live, index) != value]
    return None if any(desired[index] is None for index in changed) else changed


def replay_bindings(bindings: dict, apps_yaml: dict, args: dict, set_arg) -> list[tuple[str, int | None]]:
    """Write only what differs from args, through set_arg(arg, value, index=None). Return the (setting, index) pairs written; index is None for a whole-value write.

    set_arg has PredBat.set_arg's semantics: a None value deletes the key, a value with an index pads the
    list with None and sets that entry, a value without one replaces the whole value.
    """
    written = []
    for setting, value in sorted(desired_args(bindings, apps_yaml, args).items()):
        present = setting in args
        live = args[setting] if present else None
        if value is None:
            if present:
                set_arg(setting, None)
                written.append((setting, None))
            continue
        if not isinstance(value, list):
            if not present or live != value:
                set_arg(setting, value)
                written.append((setting, None))
            continue
        indices = _entry_writes(value, live if present else [])
        if indices is None:
            set_arg(setting, list(value))
            written.append((setting, None))
            continue
        for index in indices:
            set_arg(setting, value[index], index=index)
            written.append((setting, index))
    return written
