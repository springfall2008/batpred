# -----------------------------------------------------------------------------
# Predbat Home Battery System - pure GivEnergy Cloud automatic configuration
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Side-effect-free GivEnergy Cloud automatic-configuration mapping."""

from dataclasses import dataclass
from typing import Optional


class GECloudAutoConfigError(ValueError):
    """Raised when an automatic-configuration snapshot is incomplete."""


def normalize_register_name(name):
    """Convert one GE register name to its legacy Home Assistant suffix."""
    return str(name).lower().replace(" ", "_").replace("%", "percent").replace("-", "_")


@dataclass(frozen=True)
class GECloudAutoConfigInput:
    """Complete detached input used by the legacy GE auto-config policy."""

    batteries: tuple
    ems: Optional[str]
    gateway: Optional[str]
    pv: tuple
    battery_meters: tuple
    register_names: tuple
    models: tuple
    prefix: str
    load_today_ignore: bool = False
    split_pv: bool = False
    split_ct: bool = False
    shared_ct: bool = False


@dataclass(frozen=True)
class GECloudAutoConfigPlan:
    """Immutable mapper result with exact legacy values and decisions."""

    arguments: tuple
    primary_targets: tuple
    inverter_targets: tuple
    pv_sources: tuple
    grid_energy_sources: tuple
    ac_coupled: bool
    model_name: str
    shared_ct: Optional[bool]
    shared_ct_reason: Optional[str]
    ems_serial: Optional[str]

    def legacy_arguments(self):
        """Return fresh list values suitable for ``ComponentBase.set_arg``."""
        result = {}
        for name, value in self.arguments:
            result[name] = list(value) if isinstance(value, tuple) else value
        return result


def _pairs(values):
    """Return a detached lookup from an iterable of two-item pairs."""
    return {key: value for key, value in values}


def compile_gecloud_auto_config(config):
    """Compile the current GE Cloud discovery policy without side effects."""
    if not isinstance(config, GECloudAutoConfigInput):
        raise TypeError("config must be GECloudAutoConfigInput")
    batteries_real = tuple(config.batteries)
    if not batteries_real:
        raise GECloudAutoConfigError("GE Cloud discovery contains no batteries")
    batteries = batteries_real
    if not config.ems and config.gateway and len(batteries) > 1:
        batteries = (config.gateway,)
    count = len(batteries)
    register_names = {device: frozenset(names) for device, names in config.register_names}
    models = _pairs(config.models)
    meters = _pairs(config.battery_meters)

    def entity(domain, device, suffix):
        return "{}.{}_gecloud_{}_{}".format(
            domain,
            config.prefix,
            device,
            suffix,
        ).lower()

    def first_existing(domain, device, candidates):
        available = register_names.get(device, frozenset())
        for candidate in candidates:
            if candidate in available:
                return entity(domain, device, candidate)
        return None

    def build(domain, candidates):
        values = tuple(first_existing(domain, device, candidates) for device in batteries)
        return None if all(value is None for value in values) else values

    all_registers = frozenset(name for device in batteries for name in register_names.get(device, frozenset()))
    has_charge_rate = "battery_charge_power" in all_registers
    has_discharge_rate = "battery_discharge_power" in all_registers
    has_charge_percent = bool(all_registers & {"inverter_charge_power_percentage", "charge_power_rate"})
    has_discharge_percent = bool(all_registers & {"inverter_discharge_power_percentage", "discharge_power_rate"})
    has_pause = "pause_battery" in all_registers
    has_pause_time = "pause_battery_start_time" in all_registers
    has_discharge_target = "dc_discharge_1_lower_soc_percent_limit" in all_registers

    arguments = {
        "inverter_type": tuple("GEC" for _device in batteries),
        "num_inverters": count,
        "inverter_mode": build("switch", ("enable_eco_mode",)),
        "import_today": tuple(entity("sensor", device, "grid_import_total") for device in batteries),
        "export_today": tuple(entity("sensor", device, "grid_export_total") for device in batteries),
        "charge_rate": build("number", ("battery_charge_power",)),
        "battery_rate_max": tuple(entity("sensor", device, "max_charge_rate") for device in batteries),
        "discharge_rate": build("number", ("battery_discharge_power",)),
        "battery_power": tuple(entity("sensor", device, "battery_power") for device in batteries),
        "load_power": tuple(entity("sensor", device, "consumption_power") for device in batteries),
        "grid_power": tuple(entity("sensor", device, "grid_power") for device in batteries),
        "soc_percent": tuple(entity("sensor", device, "battery_percent") for device in batteries),
        "soc_max": tuple(entity("sensor", device, "battery_size") for device in batteries),
        "reserve": build(
            "number",
            ("battery_reserve_percent_limit", "battery_reserve_percent"),
        ),
        "inverter_time": tuple(entity("sensor", device, "time") for device in batteries),
        "charge_start_time": tuple(entity("select", device, "ac_charge_1_start_time") for device in batteries),
        "charge_end_time": tuple(entity("select", device, "ac_charge_1_end_time") for device in batteries),
        "charge_limit": build(
            "number",
            ("ac_charge_upper_percent_limit", "ac_charge_1_upper_soc_percent_limit"),
        ),
        "charge_limit_enable": build(
            "switch",
            ("enable_ac_charge_upper_percent_limit", "enable_ac_charge_1_upper_soc_percent_limit"),
        ),
        "discharge_start_time": tuple(entity("select", device, "dc_discharge_1_start_time") for device in batteries),
        "discharge_end_time": tuple(entity("select", device, "dc_discharge_1_end_time") for device in batteries),
        "scheduled_charge_enable": build(
            "switch",
            ("ac_charge_enable", "enable_ac_charge"),
        ),
        "scheduled_discharge_enable": build(
            "switch",
            ("enable_dc_discharge", "enable_force_discharge"),
        ),
        "battery_temperature": tuple(entity("sensor", device, "battery_temperature") for device in batteries),
        "battery_scaling": tuple(entity("sensor", device, "battery_dod_soh") for device in batteries),
        "inverter_limit": tuple(entity("sensor", device, "max_inverter_rate") for device in batteries),
    }
    if not config.load_today_ignore:
        arguments["load_today"] = tuple(entity("sensor", device, "consumption_total") for device in batteries)

    pv_sources = batteries + tuple(config.pv) if config.split_pv else batteries
    arguments["pv_today"] = tuple(entity("sensor", device, "solar_total") for device in pv_sources)
    arguments["pv_power"] = tuple(entity("sensor", device, "solar_power") for device in pv_sources)
    arguments["battery_temperature_history"] = entity(
        "sensor",
        batteries[0],
        "battery_temperature",
    )

    if has_pause:
        arguments["pause_mode"] = tuple(entity("select", device, "pause_battery") for device in batteries)
        if has_pause_time:
            arguments["pause_start_time"] = tuple(entity("select", device, "pause_battery_start_time") for device in batteries)
            arguments["pause_end_time"] = tuple(entity("select", device, "pause_battery_end_time") for device in batteries)
    else:
        arguments["pause_mode"] = None
        arguments["pause_start_time"] = None
        arguments["pause_end_time"] = None

    arguments["discharge_target_soc"] = (
        tuple(
            entity(
                "number",
                device,
                "dc_discharge_1_lower_soc_percent_limit",
            )
            for device in batteries
        )
        if has_discharge_target
        else None
    )
    arguments["charge_rate_percent"] = (
        build(
            "number",
            ("inverter_charge_power_percentage", "charge_power_rate"),
        )
        if has_charge_percent and not has_charge_rate
        else None
    )
    arguments["discharge_rate_percent"] = (
        build(
            "number",
            ("inverter_discharge_power_percentage", "discharge_power_rate"),
        )
        if has_discharge_percent and not has_discharge_rate
        else None
    )
    arguments["givtcp_rest"] = None
    arguments["ge_cloud_serial"] = batteries_real[0]

    shared = None
    shared_reason = None
    grid_energy_sources = batteries
    if count > 1 and not config.ems:
        meter_serials = tuple(meter for battery in batteries for meter in meters.get(battery, ()))
        duplicate_meters = len(meter_serials) != len(set(meter_serials))
        shared = duplicate_meters
        shared_reason = "duplicate meter serials" if duplicate_meters else "meters are not duplicated"
        if config.split_ct:
            shared = False
            shared_reason = "split CT override"
        elif config.shared_ct:
            shared = True
            shared_reason = "shared CT override"
        if shared:
            first = batteries[0]
            arguments["grid_power"] = (entity("sensor", first, "grid_power"),) + tuple(0 for _index in range(count - 1))
            arguments["load_power"] = (entity("sensor", first, "consumption_power"),) + tuple(0 for _index in range(count - 1))
            arguments["import_today"] = (entity("sensor", first, "grid_import_total"),)
            arguments["export_today"] = (entity("sensor", first, "grid_export_total"),)
            if not config.load_today_ignore:
                arguments["load_today"] = (entity("sensor", first, "consumption_total"),)
            grid_energy_sources = (first,)

    inverter_targets = batteries
    primary_targets = batteries
    if config.ems:
        ems = config.ems
        arguments["inverter_type"] = tuple("GEE" for _index in range(count))
        arguments["ge_cloud_serial"] = ems
        arguments["ge_cloud_data"] = False
        if not config.load_today_ignore:
            arguments["load_today"] = (entity("sensor", ems, "consumption_total"),)
        arguments["import_today"] = (entity("sensor", ems, "grid_import_total"),)
        arguments["export_today"] = (entity("sensor", ems, "grid_export_total"),)
        arguments["pv_today"] = (entity("sensor", ems, "solar_total"),)
        arguments["charge_start_time"] = tuple(entity("select", ems, "charge_start_time_slot_1") for _index in range(count))
        arguments["charge_end_time"] = tuple(entity("select", ems, "charge_end_time_slot_1") for _index in range(count))
        arguments["idle_start_time"] = tuple(entity("select", ems, "discharge_start_time_slot_1") for _index in range(count))
        arguments["idle_end_time"] = tuple(entity("select", ems, "discharge_end_time_slot_1") for _index in range(count))
        arguments["charge_limit"] = tuple(entity("number", ems, "charge_soc_percent_limit_1") for _index in range(count))
        arguments["discharge_start_time"] = tuple(entity("select", ems, "export_start_time_slot_1") for _index in range(count))
        arguments["discharge_end_time"] = tuple(entity("select", ems, "export_end_time_slot_1") for _index in range(count))
        for name, suffix in (
            ("battery_power", "battery_power"),
            ("pv_power", "solar_power"),
            ("load_power", "consumption_power"),
            ("grid_power", "grid_power"),
        ):
            arguments[name] = (entity("sensor", ems, suffix),) + tuple(0 for _index in range(count - 1))
        inverter_targets = tuple(ems for _index in range(count))
        pv_sources = (ems,)
        grid_energy_sources = (ems,)

    ac_coupled = False
    model_name = "Unknown"
    for device in batteries_real:
        model = str(models.get(device, "") or "")
        if model:
            model_name = model
            if any(token in model.lower() for token in ("ac", "aio", "all-in-one")):
                ac_coupled = True
                break
    return GECloudAutoConfigPlan(
        tuple(arguments.items()),
        tuple(primary_targets),
        tuple(inverter_targets),
        tuple(pv_sources),
        tuple(grid_energy_sources),
        ac_coupled,
        model_name,
        shared,
        shared_reason,
        config.ems,
    )
