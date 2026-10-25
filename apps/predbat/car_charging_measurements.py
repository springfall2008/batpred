"""Cache-only EV meter normalisation through Predbat's existing unit converter."""

import math


def normalise_car_meter(base, observation, required_unit):
    """Convert compatible cached units without renewing timestamps or polling a device."""
    families = {"kWh": ("Wh", "kWh", "MWh"), "kW": ("W", "kW", "MW")}
    if required_unit not in families or not isinstance(observation, dict):
        raise ValueError("Invalid car meter observation")
    units = observation.get("attributes", {}).get("unit_of_measurement")
    if not isinstance(units, str) or units.strip() not in families[required_unit] or isinstance(observation.get("state"), bool):
        raise ValueError("Incompatible car meter units")
    converted = base.unit_conversion(None, observation["state"], units.strip(), required_unit)
    if type(converted) not in (int, float) or not math.isfinite(converted):
        raise ValueError("Invalid car meter value")
    return {**observation, "state": converted, "attributes": {**observation.get("attributes", {}), "unit_of_measurement": required_unit}}
