"""EV availability routing and explicit partial-target evidence, never actuator control."""

from car_charging_config import read_car_charging_config

import hashlib
import json
import math


def household_reason(base):
    """Check availability BEFORE reading vehicle SOC or the confirmed-session meter."""
    settings = read_car_charging_config(base)
    if not isinstance(settings, dict) or settings.get("mode") != "apply" or settings.get("household_fallback") is not True:
        return None
    from car_charging_recurring import recurring_enabled, resolve_recurring_session

    if recurring_enabled(settings):
        return resolve_recurring_session(base)["reason"]
    from car_charging_plan import _read_entity_state, _observation_clock, _parse_entity_timestamp

    now = _observation_clock()

    def observed(name):
        """Require a cached, non-future observation; never refresh a device."""
        value = _read_entity_state(base, settings.get(name))
        if value is None or value["last_changed"] > now or value["last_updated"] > now:
            return None
        return value

    if settings.get("home_entity"):
        home = observed("home_entity")
        if home is None or home["state"] != "home":
            return "ev_not_home"
    connection = observed("connection_entity")
    from car_charging_observation import vehicle_policy, vehicle_state_valid, valid_connection_state, log_connection_warning

    allowed, _, _ = vehicle_policy(settings, base.args)
    if connection is None or not valid_connection_state(connection["state"]):
        return "connection_not_confirmed"
    if not isinstance(allowed, (list, tuple)) or connection["state"] not in allowed:
        from car_charging_execute import _acknowledged_stop_state

        try:
            acknowledged = _acknowledged_stop_state(settings, connection, now)
        except (ValueError, TypeError, KeyError):
            acknowledged = False
        if not acknowledged:
            return "connection_not_confirmed"
    permission = observed("permission_entity")
    if permission is None or permission["state"] != "on":
        return "permission_not_granted"
    try:
        session = settings["session"]
        departure = _parse_entity_timestamp(session["departure"], "departure")
        arrival = _parse_entity_timestamp(session["available_from"], "arrival")
        if now >= departure:
            return "departure_passed"
        if arrival > now or arrival >= departure:
            return "ev_outside_session"
    except (ValueError, TypeError, KeyError):
        return "ev_session_required"
    authorised = settings.get("execution_authorised") is True and settings.get("confirmed_session") and settings.get("grid_charge_support") and settings.get("control_model") == "deye_solarman"
    if settings.get("apply_verified") is not True and not authorised:
        return "apply_commissioning_required"
    confirmation = settings.get("confirmed_session")
    if confirmation:
        try:
            confirmed = _parse_entity_timestamp(confirmation["confirmed_at"], "confirmed_at")
            if settings.get("home_entity") and home["value_last_changed"] > confirmed:
                return "new_vehicle_session_required"
            relay = observed("relay_entity")
            if relay and relay["state"] == "off" and connection["value_last_changed"] > confirmed and connection["state"] in (allowed or []):
                return "new_connection_confirmation_required"
        except (ValueError, TypeError, KeyError):
            return "ev_session_required"
    if not vehicle_state_valid(settings, connection, base.args):
        return "connection_not_confirmed"
    log_connection_warning(base, settings, connection)
    return None


def partial_metadata_valid(base, report):
    """Require finite, consistent requested/planning/delivery evidence from both native replays."""
    settings = read_car_charging_config(base) or {}
    if settings.get("ev_partial_target_authorised") is not True or settings.get("control_model") != "deye_solarman" or not settings.get("grid_charge_support"):
        return False
    fields = ("requested_ev_target_kwh", "planning_ev_target_kwh", "planned_ev_kwh_by_departure", "ev_target_shortfall_kwh")
    if any(type(report.get(key)) not in (int, float) or not math.isfinite(report[key]) for key in fields):
        return False
    requested, planning, delivered, shortfall = (report[key] for key in fields)
    initial = report.get("ev_initial_state", {}).get("ev_soc_kwh")
    if type(initial) not in (int, float) or not math.isfinite(initial):
        return False
    if requested != base.car_charging_limit[0] or not 0 <= initial < planning < requested or not planning - 0.002 <= delivered < requested - 0.002:
        return False
    if report.get("requested_ev_target_met") is not False or report.get("partial_target_authorised") is not True or abs(shortfall - (requested - delivered)) > 0.002:
        return False
    checks = report.get("native_checks", {})
    if set(checks) != {"nominal", "pv10"}:
        return False
    for check in checks.values():
        target, final = check.get("validated_ev_target_kwh"), check.get("final_ev_kwh")
        if type(target) not in (int, float) or type(final) not in (int, float) or not math.isfinite(target) or not math.isfinite(final):
            return False
        if target != planning or final < planning - 0.002 or check.get("passed") is not True or check.get("checked_minutes") != base.forecast_minutes:
            return False
    return abs(delivered - min(check["final_ev_kwh"] for check in checks.values())) <= 0.002


def partial_fingerprint(report):
    """Bind targets and paired evidence to the staged candidate, separately from user configuration."""
    keys = ("requested_ev_target_kwh", "planning_ev_target_kwh", "planned_ev_kwh_by_departure", "ev_target_shortfall_kwh", "requested_ev_target_met", "partial_target_authorised", "ev_initial_state")
    payload = {key: report.get(key) for key in keys}
    payload["checks"] = {name: {key: check.get(key) for key in ("passed", "checked_minutes", "validated_ev_target_kwh", "final_ev_kwh")} for name, check in report.get("native_checks", {}).items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def retain_session_validity(base, settings, contradiction, soc_changed_at, now):
    """Persist invalidation through Storage; only a strictly newer explicit confirmation can replace it."""
    if settings.get("mode") != "apply" or settings.get("household_fallback") is not True:
        if contradiction:
            raise ValueError("EV SOC contradicts confirmed metered delivery")
        return
    from ha import run_async
    from car_charging_plan import _parse_entity_timestamp

    components = getattr(base, "components", None)
    storage = components.get_component("storage") if components else None
    if storage is None:
        raise ValueError("Confirmed session requires durable validation storage")
    config = settings["confirmed_session"]
    identity = hashlib.sha256(json.dumps({"confirmation": config, "soc_entity": settings.get("soc_entity"), "connection_entity": settings.get("connection_entity")}, sort_keys=True).encode()).hexdigest()
    confirmed = _parse_entity_timestamp(config["confirmed_at"], "confirmed_at")
    if confirmed > now:
        raise ValueError("Confirmed session is future-dated")
    record = run_async(storage.load("car_charging", "confirmed_session_guard"))
    if record is not None:
        if not isinstance(record, dict) or set(record) != {"identity", "confirmed_at", "invalid", "soc_changed_at"} or type(record["invalid"]) is not bool:
            raise ValueError("Confirmed session validity record is invalid")
        prior = _parse_entity_timestamp(record["confirmed_at"], "previous_confirmation")
        if record["identity"] == identity:
            if record["invalid"]:
                raise ValueError("Confirmed EV session requires explicit reconciliation")
        elif confirmed <= prior:
            raise ValueError("A new explicit confirmation is required")
    updated = {"identity": identity, "confirmed_at": config["confirmed_at"], "invalid": bool(contradiction), "soc_changed_at": soc_changed_at.isoformat()}
    if record != updated:
        if run_async(storage.save("car_charging", "confirmed_session_guard", updated, format="json", expiry=None)) is not True:
            raise ValueError("Confirmed session validation could not be persisted")
    if contradiction:
        raise ValueError("EV SOC contradicts confirmed metered delivery")
