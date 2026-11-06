"""Opt-in daily EV intent with sensor-derived sessions and durable metering evidence.

Standing user intent is not a daily operator confirmation or an EV execution lease.
Only the existing combined executor can consume the resulting bounded planning session.
"""

from car_charging_config import measurement_policy, read_car_charging_config
from car_charging_measurements import normalise_car_meter

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import RLock
from zoneinfo import ZoneInfo


_LOCK = RLock()


def recurring_enabled(settings):
    """Recognise explicit opt-in without reading entities or changing legacy behaviour."""
    return isinstance(settings, dict) and settings.get("mode") == "apply" and isinstance(settings.get("recurring_session"), dict) and settings["recurring_session"].get("enabled") is True


def next_departure(base, now):
    """Use the native ready time on the next local calendar day, including DST changes."""
    ready = datetime.strptime(base.car_charging_plan_time[0], "%H:%M:%S").time()
    zone = ZoneInfo(base.args["timezone"])
    local = now.astimezone(zone)
    date = local.date()
    departure = datetime.combine(date, ready, tzinfo=zone)
    if departure <= local:
        departure = datetime.combine(date + timedelta(days=1), ready, tzinfo=zone)
    return departure.astimezone(timezone.utc)


def _save(storage, before, after):
    """Require the real Storage acknowledgement; a memory mutation is not persistence."""
    from ha import run_async

    if before != after and run_async(storage.save("car_charging", "recurring_session", after, format="json", expiry=None)) is not True:
        raise ValueError("recurring_storage_write_failed")


def _record_valid(record, now):
    """Validate persisted evidence before it can grant or retain any session authority."""
    from car_charging_plan import _number, _parse_entity_timestamp

    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1 or type(record.get("active")) is not bool or not isinstance(record.get("policy"), str) or not isinstance(record.get("invalid"), str):
        raise ValueError("recurring_storage_corrupt")
    if record.get("session") is None:
        return
    session = record["session"]
    required = {
        "identity",
        "started_at",
        "home_transition",
        "connection_checked_at",
        "initial_kwh",
        "meter_baseline",
        "soc_observed_at",
        "last_energy",
        "last_checked",
        "last_energy_progress_at",
        "departure",
        "assessment",
        "anchor_kwh",
        "anchor_energy",
        "anchor_soc_at",
        "last_vehicle_value",
    }
    if not isinstance(session, dict) or set(session) != required or not isinstance(session["identity"], str):
        raise ValueError("recurring_storage_corrupt")
    for name in ("started_at", "home_transition", "connection_checked_at", "soc_observed_at", "last_checked", "last_energy_progress_at", "anchor_soc_at"):
        if _parse_entity_timestamp(session[name], name) > now:
            raise ValueError("recurring_storage_future")
    _parse_entity_timestamp(session["departure"], "departure")
    for name in ("initial_kwh", "meter_baseline", "last_energy", "anchor_kwh", "anchor_energy", "last_vehicle_value"):
        _number(session[name], name)
    if not isinstance(session["assessment"], dict):
        raise ValueError("recurring_storage_corrupt")


def _close_deadline(record, now, unavailable=False):
    """Preserve the previous deadline's own evidence, never later energy or an absent SOC."""
    from car_charging_plan import _parse_entity_timestamp

    session = record.get("session")
    if not session or now < _parse_entity_timestamp(session["departure"], "previous_departure"):
        return
    if (record.get("latest_deadline") or {}).get("departure") == session["departure"]:
        return
    previous = session["assessment"]
    met = True if not unavailable and previous.get("observed_target_met") is True else None
    record["latest_deadline"] = {"departure": session["departure"], "last_evidence": deepcopy(previous), "met": met, "unavailable": unavailable}


def _home_history_continuous(base, entity, since, transition, now):
    """Reconcile a cold-cache heartbeat against Recorder, never against refreshed hardware.

    The exact retained error and session are checked by the caller. History must
    cover the anchor and the current publication, with only home states between.
    Cache failed reads briefly so a missing recorder is not queried every tick.
    """
    from car_charging_plan import _parse_entity_timestamp

    key = (entity, since.isoformat(), transition.isoformat())
    cached = getattr(base, "car_charging_home_history_check", None)
    if cached and cached["key"] == key and 0 <= (now - cached["at"]).total_seconds() < 60:
        return cached["continuous"]
    continuous = False
    try:
        history = base.ha_interface.get_history_window(entity, since, now)
        if not isinstance(history, list) or len(history) != 1 or not isinstance(history[0], list) or not history[0]:
            return False
        stamps = []
        for item in history[0]:
            if item.get("entity_id") != entity or item.get("state") != "home":
                return False
            changed = _parse_entity_timestamp(item["last_changed"], "home_history_changed")
            updated = _parse_entity_timestamp(item["last_updated"], "home_history_updated")
            if changed > updated or updated > now or (stamps and updated < stamps[-1]):
                return False
            stamps.append(updated)
        continuous = stamps[0] <= since and stamps[-1] >= transition
        return continuous
    except Exception:
        return False
    finally:
        base.car_charging_home_history_check = {"key": key, "at": now, "continuous": continuous}


def _acknowledged_recurring_stop(base, settings, record, home, connection, now):
    """Recognise an operator-reconciled interruption of one existing physical session.

    This is not standing permission to ignore charging errors. A new publication,
    session, departure, genuine home transition or contrary cached history
    loses the exception. Reconciliation never supplies a new SOC or meter value.
    """
    from car_charging_plan import _parse_entity_timestamp
    from car_charging_observation import valid_connection_state, vehicle_policy

    ack = settings.get("connection_error_acknowledgement")
    session = record.get("session")
    if not isinstance(ack, dict) or set(ack) != {"state", "observed_at", "acknowledged_at", "departure", "session_identity"} or not session:
        return False
    observed = _parse_entity_timestamp(ack["observed_at"], "stop_observed_at")
    accepted = _parse_entity_timestamp(ack["acknowledged_at"], "stop_acknowledged_at")
    departure = _parse_entity_timestamp(ack["departure"], "stop_departure")
    if not valid_connection_state(ack["state"]) or ack["session_identity"] != session["identity"] or connection["state"] != ack["state"] or connection["last_updated"] != observed:
        return False
    if not _parse_entity_timestamp(session["started_at"], "session_start") <= observed <= accepted <= now < departure:
        return False
    if departure != next_departure(base, now) or departure != _parse_entity_timestamp(session["departure"], "session_departure"):
        return False
    if home is not None:
        transition = home.get("value_last_changed", home["last_changed"])
        home_anchor = max(accepted, _parse_entity_timestamp(session["home_transition"], "home_transition"))
        if transition > home_anchor:
            if any(item["state"] != "home" and _parse_entity_timestamp(item["at"], "home_history") > home_anchor for item in home.get("value_history") or []):
                return False
            if not _home_history_continuous(base, settings["home_entity"], home_anchor, transition, now):
                return False
    checked = _parse_entity_timestamp(session["connection_checked_at"], "connection_checked_at")
    allowed, _, _ = vehicle_policy(settings, base.args)
    for item in connection.get("value_history") or []:
        stamp = _parse_entity_timestamp(item["at"], "connection_history")
        if stamp > checked and item["state"] not in (allowed or []) and not (item["state"] == ack["state"] and stamp == observed):
            return False
    return True


def _resolve(base, settings, now):
    """Resolve cached observations, reconciling cold home continuity against Recorder if needed."""
    from ha import run_async
    from car_charging_plan import _number, _parse_entity_timestamp, _parse_soc_raw, _read_entity_state
    from car_charging_observation import vehicle_policy, vehicle_state_valid, log_connection_warning

    config = settings["recurring_session"]
    measurements = measurement_policy(base)
    power_factor = 1 + measurements["meter_power_tolerance_percent"] / 100
    if set(config) != {"enabled", "policy_authorised_at", "energy_entity", "power_entity"}:
        raise ValueError("recurring_config_invalid")
    authorised = _parse_entity_timestamp(config["policy_authorised_at"], "policy_authorised_at")
    if authorised > now or settings.get("mode") != "apply" or settings.get("control_model") != "deye_solarman" or not settings.get("grid_charge_support"):
        raise ValueError("recurring_policy_invalid")
    if settings.get("execution_authorised") is not True or settings.get("household_fallback") is not True:
        raise ValueError("recurring_not_authorised")
    if any(not isinstance(config[name], str) or not config[name].startswith("sensor.") for name in ("energy_entity", "power_entity")):
        raise ValueError("recurring_meter_mapping_invalid")
    components = getattr(base, "components", None)
    storage = components.get_component("storage") if components else None
    if storage is None:
        raise ValueError("recurring_storage_required")
    before = run_async(storage.load_checked("car_charging", "recurring_session"))
    if before is not None:
        _record_valid(before, now)
    else:
        legacy = run_async(storage.load_checked("car_charging", "confirmed_session_guard"))
        if legacy is not None and (not isinstance(legacy, dict) or legacy.get("invalid") is not False):
            raise ValueError("recurring_legacy_session_requires_reconciliation")
    policy = hashlib.sha256(json.dumps({"config": config, "home": settings.get("home_entity"), "connection": settings.get("connection_entity"), "soc": settings.get("soc_entity")}, sort_keys=True).encode()).hexdigest()
    if before is not None and before["policy"] != policy:
        raise ValueError("recurring_policy_changed_requires_reconciliation")
    record = deepcopy(before) if before is not None else {"version": 1, "policy": policy, "active": False, "invalid": "", "session": None, "latest_deadline": None}

    def read(entity):
        """Validate actual cached timestamps, never substitute the planning clock."""
        value = _read_entity_state(base, entity)
        if value is None or value["last_changed"] > now or value["last_updated"] > now:
            raise ValueError("recurring_entity_unavailable_or_future")
        return value

    def unavailable(reason, end_session=True):
        """Retain invalidation and close availability without reading any vehicle energy."""
        if end_session:
            record["active"] = False
        _close_deadline(record, now, unavailable=True)
        _save(storage, before, record)
        return {"status": "blocked", "reason": reason, "latest_deadline": record["latest_deadline"]}

    # Availability always precedes SOC and the EV meter, including resumed sessions.
    # A configured tracker remains a strict gate. Omitting the optional mapping
    # selects connection-only availability; it never manufactures a home reading.
    home = None
    if settings.get("home_entity") is not None:
        try:
            home = read(settings["home_entity"])
        except ValueError:
            return unavailable("ev_not_home")
        if home["state"] != "home":
            return unavailable("ev_not_home")
    try:
        connection = read(settings.get("connection_entity"))
    except ValueError:
        return unavailable("connection_not_confirmed", end_session=False)
    allowed, _, _ = vehicle_policy(settings, base.args)
    # Missing telemetry withdraws execution permission, but is not evidence of a
    # physical unplug. Retain the original meter anchor for delayed energy batches.
    if not vehicle_state_valid(settings, connection, base.args):
        return unavailable("connection_not_confirmed", end_session=False)
    if not isinstance(allowed, (list, tuple)) or not allowed:
        return unavailable("connection_not_confirmed")
    acknowledged = _acknowledged_recurring_stop(base, settings, record, home, connection, now) if connection["state"] not in allowed else False
    if connection["state"] not in allowed and not acknowledged:
        return unavailable("connection_not_confirmed")
    log_connection_warning(base, settings, connection)
    try:
        permission = read(settings.get("permission_entity"))
    except ValueError:
        return unavailable("permission_not_granted", end_session=False)
    if permission["state"] != "on":
        return unavailable("permission_not_granted", end_session=False)
    if record["invalid"]:
        return unavailable(record["invalid"], end_session=False)
    if getattr(base, "car_charging_manual_soc", None) and base.car_charging_manual_soc[0]:
        return unavailable("recurring_manual_soc_override", end_session=False)
    capacity = _number(base.car_charging_battery_size[0], "capacity", 0.001)
    rate = _number(base.car_charging_rate[0], "rate", 0.001)
    efficiency = _number(base.car_charging_loss, "efficiency", 0.001, 1)
    target = _number(base.car_charging_limit[0], "target", 0, capacity)
    departure = next_departure(base, now)
    relay = read(settings.get("relay_entity"))
    if relay["state"] not in ("off", "on"):
        return unavailable("recurring_relay_unavailable", end_session=False)
    soc = read(settings.get("soc_entity"))
    meter = normalise_car_meter(base, read(config["energy_entity"]), "kWh")
    power = normalise_car_meter(base, read(config["power_entity"]), "kW")
    if not vehicle_state_valid(settings, soc, base.args):
        return unavailable("recurring_observation_invalid", end_session=False)
    measured = _parse_soc_raw(soc["state"], settings.get("soc_entity_unit", "kWh"), capacity)
    if measured is None or not 0 <= measured <= capacity or isinstance(meter["state"], bool) or isinstance(power["state"], bool):
        return unavailable("recurring_observation_invalid", end_session=False)
    total = _number(float(meter["state"]), "meter")
    power_kw = _number(float(power["state"]), "power", 0, rate * power_factor)
    session = record["session"]
    # The legacy persisted anchor uses the observed plug transition when GPS is
    # not configured. Charging-state changes are handled independently below.
    anchor = home if home is not None else connection
    transition = anchor.get("value_last_changed", anchor["last_changed"])
    new_home = home is not None and not acknowledged and session is not None and transition > _parse_entity_timestamp(session["home_transition"], "home_transition")
    new_connection = False
    if session is not None:
        if total < session["last_energy"]:
            record["invalid"] = "recurring_meter_contradiction"
            return unavailable(record["invalid"], end_session=False)
        checked = _parse_entity_timestamp(session["connection_checked_at"], "connection_checked_at")
        if connection["last_updated"] < checked:
            return unavailable("recurring_connection_clock_reversed")
        if not acknowledged and connection["value_last_changed"] > checked:
            history = connection.get("value_history")
            # Eligible charging-state changes and unavailable telemetry are not
            # physical unplug evidence. A fresh eligible connection is still
            # required above; no permission exists during the telemetry gap.
            # A real ineligible state, or an unobserved cache gap, remains distinct.
            if not history or _parse_entity_timestamp(history[0]["at"], "connection_history") > checked:
                new_connection = True
            else:
                new_connection = any(_parse_entity_timestamp(item["at"], "connection_history") > checked and item["state"] not in allowed and item["state"] not in ("unknown", "unavailable") for item in history)
    new_session = (not record["active"] and not acknowledged) or session is None or new_home or new_connection
    _close_deadline(record, now, unavailable=new_session)
    if new_session:
        if relay["state"] != "off" or power_kw > measurements["idle_power_kw"]:
            return unavailable("recurring_new_baseline_requires_off", end_session=False)
        if measured >= target and (now - soc["last_updated"]).total_seconds() > measurements["soc_recent_seconds"]:
            return unavailable("recurring_stale_full_soc", end_session=False)
        stamp = now.isoformat()
        session = {
            "identity": hashlib.sha256((policy + stamp).encode()).hexdigest(),
            "started_at": stamp,
            "home_transition": transition.isoformat(),
            "connection_checked_at": connection["last_updated"].isoformat(),
            "initial_kwh": measured,
            "meter_baseline": total,
            "soc_observed_at": soc["last_updated"].isoformat(),
            "last_energy": total,
            "last_checked": stamp,
            "last_energy_progress_at": stamp,
            "departure": departure.isoformat(),
            "assessment": {},
            "anchor_kwh": measured,
            "anchor_energy": total,
            "anchor_soc_at": soc["last_changed"].isoformat(),
            "last_vehicle_value": measured,
        }
        record.update(active=True, session=session)
    initial = _number(session["initial_kwh"], "initial", 0, capacity)
    started = _parse_entity_timestamp(session["started_at"], "session_start")
    elapsed = (now - started).total_seconds() / 3600
    delta = total - session["meter_baseline"]
    anchor_delta = total - session["anchor_energy"]
    progress_at = _parse_entity_timestamp(session["last_energy_progress_at"], "last_energy_progress_at")
    recent_hours = (meter["last_changed"] - progress_at).total_seconds() / 3600
    progressed = total > session["last_energy"]
    invalid = ""
    increment_tolerance = measurements["meter_energy_tolerance_kwh"]
    if (
        delta < 0
        or anchor_delta < 0
        or anchor_delta > capacity / efficiency + measurements["session_energy_tolerance_kwh"]
        or delta > rate * elapsed * power_factor + increment_tolerance
        or (progressed and (recent_hours <= 0 or total - session["last_energy"] > rate * recent_hours * power_factor + increment_tolerance))
    ):
        invalid = "recurring_meter_contradiction"
    estimate = min(capacity, session["anchor_kwh"] + max(0, anchor_delta) * efficiency)
    changed = soc["last_changed"] > started and abs(measured - initial) > 0.001
    recent = changed and (now - soc["last_changed"]).total_seconds() <= measurements["soc_recent_seconds"]
    tolerance = max(measurements["soc_tolerance_kwh"], capacity * measurements["soc_tolerance_percent"] / 100)
    if changed and (measured < initial - tolerance or measured > estimate + tolerance or (recent and abs(measured - estimate) > tolerance)):
        invalid = "recurring_soc_contradiction"
    if not invalid and recent and measured != session["last_vehicle_value"] and soc["last_changed"] > _parse_entity_timestamp(session["anchor_soc_at"], "anchor_soc_at"):
        session.update(anchor_kwh=measured, anchor_energy=total, anchor_soc_at=soc["last_changed"].isoformat())
    completed_observation = changed and measured >= target
    value = measured if recent or completed_observation else estimate
    if not invalid and progressed:
        progress_at = meter["last_changed"]
        session["last_energy_progress_at"] = progress_at.isoformat()
    if value < target and relay["state"] == "on" and min((now - relay["last_changed"]).total_seconds(), (now - progress_at).total_seconds()) > measurements["meter_stall_seconds"]:
        invalid = "recurring_meter_stopped"
    if invalid:
        record["invalid"] = invalid
        return unavailable(invalid, end_session=False)
    session["departure"] = departure.isoformat()
    observation = {
        "ev_soc": value,
        "ev_soc_source": "changed_vehicle_value_under_standing_policy" if recent or completed_observation else "standing_policy_meter_estimate",
        "soc_observed_at": soc["last_updated"].isoformat(),
        "soc_observation_age_minutes": (now - soc["last_updated"]).total_seconds() / 60,
        "ev_soc_is_estimate": not (recent or completed_observation),
        "ev_metered_ac_kwh": delta,
        "vehicle_value_kwh": measured,
        "policy_authorised_at": config["policy_authorised_at"],
        "sensor_session_started_at": session["started_at"],
        "observed_target_met": bool(completed_observation),
        "connection_error_acknowledged": acknowledged,
        "connection_error_state": connection["state"] if acknowledged else None,
        "connection_error_observed_at": connection["last_updated"].isoformat() if acknowledged else None,
    }
    session.update(
        last_energy=total,
        last_checked=now.isoformat(),
        connection_checked_at=connection["last_updated"].isoformat(),
        last_vehicle_value=measured,
        assessment={"at": now.isoformat(), "soc_kwh": value, "shortfall_kwh": max(0, target - value), "is_estimate": observation["ev_soc_is_estimate"], "observed_target_met": observation["observed_target_met"]},
    )
    if acknowledged:
        record["active"] = True
        session["home_transition"] = transition.isoformat()
    _save(storage, before, record)
    return {
        "status": "ready",
        "reason": None,
        "identity": session["identity"],
        "session": {"available_from": session["started_at"], "departure": session["departure"], "soc_kwh": value, "soc_observed_at": soc["last_updated"].isoformat()},
        "observation": observation,
        "latest_deadline": record["latest_deadline"],
    }


def resolve_recurring_session(base, settings=None, now=None):
    """Resolve standing intent without mutating user configuration or writing to actuators."""
    from car_charging_plan import _observation_clock

    settings = read_car_charging_config(base) if settings is None else settings
    if not recurring_enabled(settings):
        return {"status": "disabled", "reason": None}
    now = _observation_clock() if now is None else now
    try:
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("recurring_clock_invalid")
        with _LOCK:
            return _resolve(base, settings, now.astimezone(timezone.utc))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("recurring_") else "recurring_evidence_invalid"
        return {"status": "blocked", "reason": reason}
