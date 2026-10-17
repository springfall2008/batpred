"""Explicit operator-confirmed EV sessions with measured AC delivery, not fabricated freshness."""


def confirmed_session_observation(base, settings, now):
    """Use a dated operator baseline plus the dedicated non-resetting EV energy counter.

    Mercedes push values may remain unchanged while parked. A retained VALID
    value is not a fresh measurement, nor is its old last_updated proof of an
    invalid value. This opt-in records the operator's confirmation separately,
    estimates delivery from the local meter, and compares later valid SOC changes.
    It never rewrites entity timestamps, renews departure, or changes targets.
    """
    from joint_site_native import _number, _parse_entity_timestamp, _parse_soc_raw, _read_entity_state

    config = settings["confirmed_session"]
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
    if meter["attributes"].get("unit_of_measurement") != "kWh" or power["attributes"].get("unit_of_measurement") != "kW":
        raise ValueError("EV meter units do not match")
    if any(value["attributes"].get("retrievalstatus") != "VALID" for value in (soc, connection)):
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
    if energy > min(capacity / efficiency + 0.5, rate * elapsed_hours * 1.1 + 0.1) or watts_kw > rate * 1.1:
        raise ValueError("EV meter no longer belongs to the confirmed session")
    estimated = min(capacity, initial + energy * efficiency)
    changed = soc["last_changed"] > confirmed and abs(measured_soc - initial) > 0.001
    # A new HA value is corroboration, not a fabricated physical observation time.
    # Only use it during its recent change interval and when consistent with the
    # meter. Otherwise the cumulative meter keeps progressing across cycles/restarts.
    recent = changed and (now - soc["last_changed"]).total_seconds() <= 180
    tolerance = max(capacity * 0.05, 0.5)
    contradiction = changed and (measured_soc < initial - tolerance or measured_soc > estimated + tolerance or (recent and abs(measured_soc - estimated) > tolerance))
    from joint_site_session import retain_session_validity

    retain_session_validity(base, settings, contradiction, soc["last_changed"], now)
    completed = changed and measured_soc >= base.car_charging_limit[0]
    value = measured_soc if recent or completed else estimated
    if value < base.car_charging_limit[0] and relay["state"] == "on" and min((now - relay["last_changed"]).total_seconds(), (now - meter["last_updated"]).total_seconds()) > 600:
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
