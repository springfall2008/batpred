"""Translate native flat car/inverter arguments into internal planning inputs.

The public configuration remains the ordinary Predbat argument dictionary. This
module is an internal adapter, not another YAML namespace or another controller.
"""

from copy import deepcopy
import math


CAR_SETTING_KEYS = {
    "mode": "car_charging_plan_mode",
    "session": "car_charging_session",
    "load_port_limit_w": "load_limit",
    "ev_pause_percent": "car_charging_battery_pause_soc",
    "ev_restart_percent": "car_charging_battery_restart_soc",
    "min_off_minutes": "car_charging_min_off_minutes",
    "beam_width": "car_charging_search_beam_width",
    "terminal_min_percent": "battery_terminal_min_soc",
    "soc_max_age_minutes": "car_charging_soc_max_age_minutes",
    "relay_entity": "car_charging_relay",
    "connection_entity": "car_charging_plugged",
    "permission_entity": "car_charging_permission",
    "soc_entity": "car_charging_soc",
    "soc_entity_unit": "car_charging_soc_unit",
    "connection_entity_states": "car_charging_plugged_response",
    "connection_warning_states": "car_charging_warning_states",
    "vehicle_state_validity": "car_charging_state_validity",
    "import_limit_w": "import_limit",
    "control_model": "car_charging_control_model",
    "allow_stale_shadow_soc": "car_charging_allow_stale_soc_preview",
    "apply_verified": "car_charging_control_verified",
    "ev_executor_lease_verified": "car_charging_executor_verified",
    "connection_observed_at_entity": "car_charging_plugged_observed_at",
    "soc_observed_at_entity": "car_charging_soc_observed_at",
    "grid_charge_support": "inverter_grid_support",
    "confirmed_session": "car_charging_confirmed_session",
    "execution_authorised": "car_charging_control_authorised",
    "connection_error_acknowledgement": "car_charging_error_acknowledgement",
    "ev_charging_priority": "car_charging_priority",
    "battery_min_window_minutes": "battery_min_window_minutes",
    "self_consumption_first": "car_charging_self_consumption_first",
    "household_fallback": "car_charging_household_fallback",
    "home_entity": "car_charging_home",
    "ev_partial_target_authorised": "car_charging_partial_target",
    "recurring_session": "car_charging_recurring_session",
}

# These existing argument shapes follow native car order. The currently supported
# execution topology still validates one car/one inverter independently.
INDEXED_CAR_SETTINGS = frozenset(("relay_entity", "connection_entity", "permission_entity", "soc_entity", "soc_entity_unit", "home_entity", "connection_observed_at_entity", "soc_observed_at_entity"))


def read_car_charging_config(base):
    """Read explicit native arguments without enabling control from unrelated car settings."""
    mode = base.args.get("car_charging_plan_mode", "off")
    if mode == "off":
        return None
    if not isinstance(mode, str) or mode not in ("shadow", "apply"):
        # The planning boundary reports malformed input; an early publication
        # hook must not throw before that boundary can revoke old authority.
        return {"mode": mode}
    result = {}
    for name, argument in CAR_SETTING_KEYS.items():
        if argument not in base.args:
            continue
        value = base.args[argument]
        if name in INDEXED_CAR_SETTINGS and isinstance(value, list):
            value = value[0] if value else None
        result[name] = value
    result["mode"] = mode
    result.setdefault("soc_entity_unit", "%")
    return result


MEASUREMENT_DEFAULTS = {
    "meter_stall_seconds": (600, 30, 7200),
    "soc_recent_seconds": (180, 1, 3600),
    "soc_tolerance_kwh": (0.5, 0, 5),
    "soc_tolerance_percent": (5, 0, 20),
    "meter_power_tolerance_percent": (10, 0, 50),
    "meter_energy_tolerance_kwh": (0.1, 0, 2),
    "session_energy_tolerance_kwh": (0.5, 0, 5),
    "idle_power_kw": (0.01, 0, 0.1),
}


def measurement_policy(base):
    """Validate device-dependent intervals and tolerances, retaining conservative defaults."""
    result = {}
    for name, (default, minimum, maximum) in MEASUREMENT_DEFAULTS.items():
        value = base.args.get("car_charging_" + name, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError("Invalid car charging measurement policy: " + name)
        result[name] = value
    return result


def native_car_arguments(settings, battery_window_minutes=None):
    """Convert an explicitly supplied previous settings mapping; never guess its YAML location."""
    if not isinstance(settings, dict) or set(settings) - set(CAR_SETTING_KEYS):
        raise ValueError("Unsupported car planning settings")
    result = {CAR_SETTING_KEYS[name]: deepcopy(value) for name, value in settings.items()}
    if "soc_entity" in settings:
        # Older dated inputs allowed cell-side energy. Keep that explicit when
        # converting; ordinary native car SOC continues to default to percent.
        result.setdefault("car_charging_soc_unit", "kWh")
    if battery_window_minutes is not None:
        if type(battery_window_minutes) is not int or battery_window_minutes not in (5, 15, 30, 60):
            raise ValueError("Invalid saved battery minimum window")
        previous = result.get("battery_min_window_minutes", battery_window_minutes)
        if str(previous) != str(battery_window_minutes):
            raise ValueError("Saved and configured battery minimum windows disagree")
        result["battery_min_window_minutes"] = battery_window_minutes
    return result
