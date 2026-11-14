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
    """
    bound = []
    for setting in sorted(BINDING_SETTINGS):
        value = slot_value(args.get(setting), slot)
        if isinstance(value, str):
            bound.append(value.casefold())
    if slot == 0 and isinstance(apps_yaml.get("ge_cloud_serial"), str):
        bound.append(apps_yaml["ge_cloud_serial"].casefold())
    return [serial for serial in serials if isinstance(serial, str) and len(serial) >= MIN_SERIAL_LENGTH and any(serial.casefold() in text for text in bound)]
