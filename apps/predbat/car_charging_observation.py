"""Explicit operator-confirmed EV sessions with measured AC delivery, not fabricated freshness."""


def valid_connection_state(value):
    """Reject missing HA observations without assigning meaning to vendor codes."""
    return isinstance(value, str) and bool(value.strip()) and value.lower() not in ("unknown", "unavailable", "none")


def vehicle_policy(settings, car_args=None):
    """Read configured vehicle policy through the native argument adapter."""
    car_args = car_args or {}

    def configured(name, car_name, default=None):
        """Never silently choose between contradictory old and native-style settings."""
        if car_name in car_args:
            value = car_args[car_name]
            if name in settings and settings[name] != value:
                raise ValueError("Conflicting vehicle settings: " + car_name)
            return value
        return settings.get(name, default)

    normal = settings.get("connection_entity_states")
    warnings = configured("connection_warning_states", "car_charging_warning_states", [])
    if normal is not None and (not isinstance(normal, (list, tuple)) or not normal or not all(valid_connection_state(item) for item in normal)):
        raise ValueError("connection_entity_states must contain explicit non-missing state strings")
    if not isinstance(warnings, (list, tuple)) or not all(valid_connection_state(item) for item in warnings):
        raise ValueError("car_charging_warning_states must be a list of explicit non-missing state strings")
    quality = configured("vehicle_state_validity", "car_charging_state_validity")
    if quality is not None:
        if not isinstance(quality, dict) or set(quality) != {"attribute", "valid_states"} or not isinstance(quality["attribute"], str) or not quality["attribute"].strip():
            raise ValueError("car_charging_state_validity requires an attribute and valid_states")
        if not isinstance(quality["valid_states"], (list, tuple)) or not quality["valid_states"] or not all(isinstance(item, str) and bool(item.strip()) for item in quality["valid_states"]):
            raise ValueError("car_charging_state_validity valid_states must be non-empty strings")
    allowed = list(dict.fromkeys([*(normal or []), *warnings])) if normal is not None or warnings else None
    return allowed, warnings, quality


def vehicle_state_valid(settings, observation, car_args=None):
    """Use generic HA availability and only the explicitly configured quality attribute."""
    _, _, quality = vehicle_policy(settings, car_args)
    if not isinstance(observation, dict) or not valid_connection_state(str(observation.get("state", ""))):
        return False
    return quality is None or observation.get("attributes", {}).get(quality["attribute"]) in quality["valid_states"]


def log_connection_warning(base, settings, connection):
    """Deduplicate configured warning observations in logs without publishing an error status."""
    _, warnings, _ = vehicle_policy(settings, base.args)
    marker = (connection["state"], connection.get("value_last_changed", connection["last_changed"])) if connection["state"] in warnings else None
    if marker is not None and marker != getattr(base, "car_charging_connection_warning", None):
        base.log("Warn: EV configured connection warning {!r} accepted; independent execution checks remain required.".format(connection["state"]))
    base.car_charging_connection_warning = marker


def confirmed_session_observation(base, settings, now):
    """Use a dated operator baseline plus the dedicated non-resetting EV energy counter.

    Vehicle push values may remain unchanged while parked. A retained valid
    value is not a fresh measurement, nor is its old last_updated proof of an
    invalid value. This opt-in records the operator's confirmation separately,
    estimates delivery from the local meter, and compares later valid SOC changes.
    It never rewrites entity timestamps, renews departure, or changes targets.
    """
    from car_charging_plan import _number, _parse_entity_timestamp, _parse_soc_raw, _read_entity_state
    from car_charging_config import measurement_policy
    from car_charging_measurements import normalise_car_meter

    config = settings["confirmed_session"]
    measurements = measurement_policy(base)
    power_factor = 1 + measurements["meter_power_tolerance_percent"] / 100
    if not isinstance(config, dict) or set(config) != {"confirmed_at", "departure", "soc_kwh", "energy_entity", "energy_kwh", "power_entity"}:
        raise ValueError("Invalid confirmed EV session")
    confirmed = _parse_entity_timestamp(config["confirmed_at"], "operator_confirmation")
    departure = _parse_entity_timestamp(config["departure"], "confirmed_departure")
    arrival = _parse_entity_timestamp(settings["session"]["available_from"], "session_arrival")
    if departure != _parse_entity_timestamp(settings["session"]["departure"], "session_departure") or not arrival <= confirmed <= now or not confirmed < departure or (departure - confirmed).total_seconds() > 86400:
        raise ValueError("Invalid or renewed operator session")
    capacity = _number(base.car_charging_battery_size[0], "ev_capacity", 0.001)
    initial = _number(config["soc_kwh"], "confirmed_soc", 0, capacity)
    baseline = _number(config["energy_kwh"], "confirmed_meter")
    efficiency = _number(base.car_charging_loss, "ev_efficiency", 0.001, 1)
    for name in ("energy_entity", "power_entity"):
        if not isinstance(config[name], str) or not config[name].startswith("sensor."):
            raise ValueError("Dedicated EV meter required")
    if now >= departure:
        # This numerical baseline is used only to build a zero-delivery EV model.
        # It is not a present SOC reading or evidence of departure success.
        return {
            "ev_soc": initial,
            "ev_soc_source": "closed_session_baseline",
            "soc_observed_at": confirmed.isoformat(),
            "soc_observation_age_minutes": (now - confirmed).total_seconds() / 60,
            "operator_confirmed_at": confirmed.isoformat(),
            "ev_metered_ac_kwh": None,
            "ev_soc_is_estimate": True,
            "session_closed": True,
        }
    meter = _read_entity_state(base, config["energy_entity"])
    power = _read_entity_state(base, config["power_entity"])
    soc = _read_entity_state(base, settings.get("soc_entity"))
    connection = _read_entity_state(base, settings.get("connection_entity"))
    relay = _read_entity_state(base, settings.get("relay_entity"))
    if any(value is None or not isinstance(value["attributes"], dict) or value["last_updated"] > now or value["last_changed"] > now for value in (meter, power, soc, connection, relay)):
        raise ValueError("Confirmed session observation unavailable or future")
    meter = normalise_car_meter(base, meter, "kWh")
    power = normalise_car_meter(base, power, "kW")
    if any(not vehicle_state_valid(settings, value, base.args) for value in (soc, connection)):
        raise ValueError("EV retained state not valid")
    measured_soc = _parse_soc_raw(soc["state"], settings.get("soc_entity_unit", "kWh"), capacity)
    if measured_soc is None or not 0 <= measured_soc <= capacity:
        raise ValueError("Invalid retained EV SOC")
    if isinstance(meter["state"], bool) or isinstance(power["state"], bool):
        raise ValueError("Invalid EV meter state")
    total = _number(float(meter["state"]), "ev_meter", baseline)
    watts_kw = _number(float(power["state"]), "ev_power")
    if relay["state"] not in ("off", "on"):
        raise ValueError("EV relay unavailable")
    energy = total - baseline
    elapsed_hours = (now - confirmed).total_seconds() / 3600
    rate = _number(base.car_charging_rate[0], "ev_rate", 0.001)
    if energy > min(capacity / efficiency + measurements["session_energy_tolerance_kwh"], rate * elapsed_hours * power_factor + measurements["meter_energy_tolerance_kwh"]) or watts_kw > rate * power_factor:
        raise ValueError("EV meter no longer belongs to the confirmed session")
    estimated = min(capacity, initial + energy * efficiency)
    changed = soc["last_changed"] > confirmed and abs(measured_soc - initial) > 0.001
    # A new HA value is corroboration, not a fabricated physical observation time.
    # Only use it during its recent change interval and when consistent with the
    # meter. Otherwise the cumulative meter keeps progressing across cycles/restarts.
    recent = changed and (now - soc["last_changed"]).total_seconds() <= measurements["soc_recent_seconds"]
    tolerance = max(capacity * measurements["soc_tolerance_percent"] / 100, measurements["soc_tolerance_kwh"])
    contradiction = changed and (measured_soc < initial - tolerance or measured_soc > estimated + tolerance or (recent and abs(measured_soc - estimated) > tolerance))
    from car_charging_policy import retain_session_validity

    retain_session_validity(base, settings, contradiction, soc["last_changed"], now)
    completed = changed and measured_soc >= base.car_charging_limit[0]
    value = measured_soc if recent or completed else estimated
    if value < base.car_charging_limit[0] and relay["state"] == "on" and min((now - relay["last_changed"]).total_seconds(), (now - meter["last_updated"]).total_seconds()) > measurements["meter_stall_seconds"]:
        raise ValueError("EV energy meter stopped advancing during charge")
    return {
        "ev_soc": value,
        "ev_soc_source": "changed_vehicle_value_after_confirmation" if recent or completed else "operator_confirmed_metered_session",
        "soc_observed_at": soc["last_updated"].isoformat(),
        "soc_observation_age_minutes": (now - soc["last_updated"]).total_seconds() / 60,
        "operator_confirmed_at": confirmed.isoformat(),
        "ev_metered_ac_kwh": energy,
        "ev_soc_is_estimate": not (recent or completed),
        "vehicle_value_kwh": measured_soc,
    }
