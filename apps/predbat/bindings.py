# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Discovery bindings: which inverter slot each discovered record drives, and what it binds there.

Pure functions and constants only - nothing here reads Home Assistant, writes args, logs or holds
state. The coordinator calls them with the apps.yaml snapshot (args exactly as the user wrote them)
and the live args, both plain dicts. See
docs/superpowers/specs/2026-11-14-discovery-automatic-inverter-config-design.md.
"""

import copy

from config import APPS_SCHEMA
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

# APPS_SCHEMA settings Predbat writes to: a bound entity on one of these gives a slot a control method
MODIFY_SETTINGS = tuple(sorted(name for name, schema in APPS_SCHEMA.items() if schema.get("modify")))


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
    for setting in MODIFY_SETTINGS:
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

    The strings are every per-inverter setting's entry at this slot in the live args (so re: patterns
    are already resolved), plus ge_cloud_serial for slot 0 as the apps.yaml snapshot has it - never the
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


def _manual_entries(apps_yaml, args, candidate_serials):
    """Step 1 and 2: one entry per index below the manual num_inverters, each manual slot carrying its settings and scanned serials.

    Summed settings come from the apps.yaml snapshot, since replay rewrites those lists in live args and a
    live index stops meaning a slot after the first pass; every other binding setting comes from live args
    at the slot index, where re: patterns are resolved.
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
        serials = sorted(slot_serials(index, apps_yaml, args, candidate_serials))
        entries.append({"slot": index, "origin": "manual", "source": None, "device_id": None, "inverter_type": slot["inverter_type"], "serials": serials, "settings": settings})
    return entries


def _place(slots, target, entry, matched, warnings):
    """Put entry in slot target, warning on a manual slot taken over and clearing every other manual slot that names the record."""
    entry["slot"] = target
    current = slots[target]
    if current["origin"] == "manual":
        common = sorted(set(current["serials"]) & set(entry["serials"]))
        serial = (common or entry["serials"] or [entry["device_id"]])[0]
        warnings.append("Inverter slot {}: {} inverter {} is also configured manually in apps.yaml - discovery is taking over this slot".format(target, entry["source"], serial))
    slots[target] = entry
    for other in matched:
        if other["slot"] != target and slots[other["slot"]] is other:
            serial = sorted(set(other["serials"]) & set(entry["serials"]))[0]
            warnings.append("Inverter slot {}: also names {}, already configured in slot {} - clearing this slot".format(other["slot"], serial, target))
            slots[other["slot"]] = _free_entry(other["slot"])


def _allocate(slots, placements, warnings):
    """Step 3: place each (entry, sticky slot) in order - a sticky slot first, then the lowest matching manual slot, then the lowest free slot."""
    for entry, sticky in placements:
        matched = [slot for slot in slots if slot["origin"] == "manual" and set(slot["serials"]) & set(entry["serials"])]
        if sticky is not None and sticky < len(slots) and slots[sticky]["origin"] != "discovered":
            target = sticky
        elif matched:
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


def build_bindings(apps_yaml: dict, args: dict, sources: list[dict], previous: dict | None) -> tuple[dict, list[str]]:
    """Return (bindings, warnings) for the apps.yaml snapshot, live args, the opted-in sources and the previous bindings. Writes nothing.

    sources: one dict per opted-in component, {"name", "records", "skip_settings", "keep_user_settings",
    "include_pv_only"}. previous: the bindings the last call returned, or None. A record previous placed
    keeps its slot. A source that reports no driven record, or is absent from sources, keeps every slot
    previous gave it, copied verbatim; only a source reporting at least one driven record can lose a slot.
    """
    warnings = []
    previous_entries = [entry for entry in (previous or {}).get("inverters", []) if entry.get("origin") == "discovered"]
    previous_slots = slot_map(previous)

    fresh = []
    driven_sources = set()
    for source in sorted(sources, key=lambda item: item["name"]):
        driven = [record for record in source.get("records") or [] if _is_driven(record)]
        if not driven:
            warnings.append("{} reported no inverter to configure - keeping its existing slots".format(source["name"]))
            continue
        driven_sources.add(source["name"])
        fresh.extend((source, record) for record in driven)
    retained = [copy.deepcopy(entry) for entry in previous_entries if entry.get("source") not in driven_sources]

    candidate_serials = sorted({serial for _, record in fresh for serial in _record_serials(record)} | {serial for entry in retained for serial in entry.get("serials", [])})
    slots = _manual_entries(apps_yaml, args, candidate_serials)
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
