"""Pure Gateway-to-PredBat auto-configuration mapping."""

from dataclasses import dataclass
from types import MappingProxyType


class GatewayAutoConfigError(ValueError):
    """Gateway discovery cannot produce a safe configuration plan."""


def _freeze_value(value):
    """Detach mutable legacy list values from the compiled plan."""
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    return value


@dataclass(frozen=True)
class GatewayAutoConfigPlan:
    """Detached configuration values and selected Gateway identities."""

    arguments: object
    selected_serials: tuple
    discovered_serials: tuple
    hybrid: object = None
    model_name: str = ""

    def __post_init__(self):
        """Freeze the top-level mapping and normalized serial tuples."""
        arguments = {name: _freeze_value(value) for name, value in self.arguments.items()}
        object.__setattr__(self, "arguments", MappingProxyType(arguments))
        object.__setattr__(self, "selected_serials", tuple(self.selected_serials))
        object.__setattr__(self, "discovered_serials", tuple(self.discovered_serials))

    def legacy_arguments(self):
        """Return fresh list values suitable for ``ComponentBase.set_arg``."""
        return {name: list(value) if isinstance(value, tuple) else value for name, value in self.arguments.items()}


def _field(item, name, default=None):
    """Read one normalized field from a mapping or immutable snapshot object."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _serial_suffix(serial):
    """Match the existing Gateway entity suffix convention."""
    return serial[-6:].lower() if len(serial) > 6 else serial.lower()


def compile_gateway_auto_config(inverters, prefix="predbat", serial_filter=()):
    """Return the exact non-EV arguments currently written by GatewayMQTT."""
    inverters = tuple(inverters)
    if not inverters:
        raise GatewayAutoConfigError("Gateway discovery contains no inverters")
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("Gateway entity prefix must be non-empty")
    prefix = prefix.strip()

    def serial(item):
        value = _field(item, "serial", "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Gateway inverter serial must be non-empty")
        return value.strip()

    def kind(item):
        return str(_field(item, "kind", "inverter")).strip().lower()

    ems_units = [item for item in inverters if kind(item) == "ems"]
    gateway_units = [item for item in inverters if kind(item) == "gateway"]
    candidate_aios = [item for item in inverters if kind(item) not in ("ems", "gateway")]
    any_primary = any(bool(_field(item, "primary", False)) for item in candidate_aios)
    if any_primary:
        aios = [item for item in candidate_aios if bool(_field(item, "primary", False)) and bool(_field(item, "battery_present", False))]
        if not aios:
            aios = [item for item in candidate_aios if int(_field(item, "battery_capacity_wh", 0) or 0) > 0]
    else:
        aios = [item for item in candidate_aios if bool(_field(item, "battery_present", False))]

    if ems_units:
        selected = ems_units[:1]
    elif gateway_units and len(aios) > 1:
        selected = gateway_units[:1]
    else:
        selected = aios
    if not selected:
        selected = candidate_aios or list(inverters)

    if isinstance(serial_filter, str):
        serial_filter = (serial_filter,)
    wanted = frozenset(str(value).strip().upper() for value in serial_filter if value is not None and str(value).strip())
    if wanted:
        filtered = [item for item in selected if serial(item).upper() in wanted]
        if not filtered:
            available = ", ".join(serial(item) for item in selected)
            raise GatewayAutoConfigError("Gateway serial filter matched no selected inverter; available: {}".format(available))
        selected = filtered
    selected = tuple(sorted(selected, key=serial))

    bases = tuple("{}_gateway_{}".format(prefix, _serial_suffix(serial(item))) for item in selected)
    first_base = bases[0]
    count = len(selected)
    arguments = {
        "inverter_type": ["GWMQTT"] * count,
        "num_inverters": count,
        "soc_percent": ["sensor.{}_soc".format(base) for base in bases],
        "soc_max": ["sensor.{}_battery_capacity".format(base) if int(_field(item, "battery_capacity_wh", 0) or 0) > 0 else None for base, item in zip(bases, selected)],
        "battery_power": ["sensor.{}_battery_power".format(base) for base in bases],
        "pv_power": ["sensor.{}_pv_power".format(base) for base in bases],
        "grid_power": ["sensor.{}_grid_power".format(base) for base in bases],
        "load_power": ["sensor.{}_load_power".format(base) for base in bases],
        "charge_rate": ["number.{}_charge_rate".format(base) for base in bases],
        "discharge_rate": ["number.{}_discharge_rate".format(base) for base in bases],
        "reserve": ["number.{}_reserve_soc".format(base) for base in bases],
        "charge_limit": ["number.{}_target_soc".format(base) for base in bases],
        "battery_temperature": ["sensor.{}_battery_temperature".format(base) for base in bases],
        "charge_start_time": ["select.{}_charge_slot1_start".format(base) for base in bases],
        "charge_end_time": ["select.{}_charge_slot1_end".format(base) for base in bases],
        "discharge_start_time": ["select.{}_discharge_slot1_start".format(base) for base in bases],
        "discharge_end_time": ["select.{}_discharge_slot1_end".format(base) for base in bases],
        "scheduled_charge_enable": ["switch.{}_charge_enabled".format(base) for base in bases],
        "scheduled_discharge_enable": ["switch.{}_discharge_enabled".format(base) for base in bases],
        "export_limit": ["sensor.{}_export_limit_w".format(base) for base in bases],
        "inverter_limit": ["sensor.{}_inverter_rate_max".format(base) for base in bases],
        "pv_today": ["sensor.{}_pv_today".format(first_base)],
        "import_today": ["sensor.{}_import_today".format(first_base)],
        "export_today": ["sensor.{}_export_today".format(first_base)],
        "load_today": ["sensor.{}_load_today".format(first_base)],
        "battery_temperature_history": "sensor.{}_battery_temperature".format(first_base),
        "battery_scaling": ["sensor.{}_battery_dod".format(first_base)],
        "battery_rate_max": ["sensor.{}_battery_rate_max".format(first_base)],
        "inverter_time": ["sensor.{}_inverter_time".format(first_base)],
        "givtcp_rest": None,
        "charge_rate_percent": None,
        "discharge_rate_percent": None,
        "pause_mode": None,
        "pause_start_time": None,
        "pause_end_time": None,
        "discharge_target_soc": None,
        "ge_cloud_data": False,
        "ge_cloud_direct": False,
    }
    first = selected[0]
    if kind(first) == "ems" and int(_field(first, "ems_num_inverters", 0) or 0) > 0:
        aggregate = "{}_gateway".format(prefix)
        arguments.update(
            {
                "ems_total_soc": "sensor.{}_ems_total_soc".format(aggregate),
                "ems_total_charge": "sensor.{}_ems_total_charge".format(aggregate),
                "ems_total_discharge": "sensor.{}_ems_total_discharge".format(aggregate),
                "ems_total_grid": "sensor.{}_ems_total_grid".format(aggregate),
                "ems_total_pv": "sensor.{}_ems_total_pv".format(aggregate),
                "ems_total_load": "sensor.{}_ems_total_load".format(aggregate),
                "idle_start_time": ["select.{}_discharge_slot1_start".format(first_base) for _index in range(count)],
                "idle_end_time": ["select.{}_discharge_slot1_end".format(first_base) for _index in range(count)],
            }
        )

    hybrid = None
    model_name = ""
    for item in candidate_aios:
        model = str(_field(item, "model", "") or "")
        if model:
            model_name = model
            hybrid = not any(token in model.lower() for token in ("ac", "aio", "all-in-one"))
            if not hybrid:
                break
    return GatewayAutoConfigPlan(
        arguments,
        tuple(serial(item) for item in selected),
        tuple(serial(item) for item in inverters),
        hybrid,
        model_name,
    )
