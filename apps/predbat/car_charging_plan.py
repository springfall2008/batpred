"""Native EV/battery scheduling with isolated preview and separately gated execution.

The solver makes coupled EV/battery decisions, then a fresh native Prediction
replays them. Shadow never changes executable controls. Explicit apply stages a
candidate for the native executor; staging alone cannot energise an EV or inverter.
"""

from car_charging_config import read_car_charging_config

from copy import copy, deepcopy
from datetime import datetime, timezone
import math

from const import PREDICT_STEP, PREDBAT_MAX_CARS
from prediction import Prediction


class UnsupportedCarPlanning(ValueError):
    """A native configuration cannot be represented by this single-EV prototype."""


def _number(value, name, minimum=0, maximum=None):
    """Reject coercion, booleans and non-finite configuration/forecast values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError("Invalid EV/battery numeric field: " + name)
    return float(value)


def _aware(value, name):
    """Require an offset-aware timestamp and reject anything naive; never guess a timezone."""
    if not isinstance(value, datetime):
        raise ValueError("EV-site timestamp required: " + name)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("EV-site timestamp needs an offset: " + name)
    return value.astimezone(timezone.utc)


def _parse_entity_timestamp(value, name):
    """Parse an offset-aware ISO 8601 timestamp from HA; never guess a timezone or next day."""
    if not isinstance(value, str):
        raise ValueError("EV-site entity timestamp required: " + name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid EV/battery entity timestamp: " + name) from None
    return _aware(parsed, name)


def _read_entity_state(base, entity_id):
    """Return the raw HA state payload via get_state_wrapper with no refresh.

    Uses the standard HA shape ``{state, attributes, last_changed, last_updated}``
    with ``refresh=False`` so no HA service is ever called. Returns ``None`` when
    the interface, the entity or the cache is unavailable. Never invokes a
    hardware poll or a cache writer.
    """
    if not entity_id or not isinstance(entity_id, str):
        return None
    try:
        payload = base.get_state_wrapper(entity_id=entity_id, raw=True, refresh=False)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("state")
    last_changed = payload.get("last_changed")
    last_updated = payload.get("last_updated")
    if state is None or last_changed is None or last_updated is None:
        return None
    try:
        last_changed_aware = _parse_entity_timestamp(last_changed, "last_changed") if isinstance(last_changed, str) else _aware(last_changed, "last_changed")
        last_updated_aware = _parse_entity_timestamp(last_updated, "last_updated") if isinstance(last_updated, str) else _aware(last_updated, "last_updated")
    except ValueError:
        return None
    transition = payload.get("value_last_changed")
    try:
        transition = _parse_entity_timestamp(transition, "value_last_changed") if isinstance(transition, str) else _aware(transition, "value_last_changed")
        if transition > last_changed_aware:
            transition = last_changed_aware
    except ValueError:
        transition = last_changed_aware
    return {
        "state": state,
        "last_changed": last_changed_aware,
        "last_updated": last_updated_aware,
        "value_last_changed": transition,
        "value_history": deepcopy(payload.get("value_history")),
        "attributes": deepcopy(payload.get("attributes", {})),
    }


def _parse_soc_raw(state, unit, capacity_kwh):
    """Parse an EV SoC raw value honouring the explicit unit and capacity.

    Returns ``None`` when the value is invalid (bool, non-finite, unknown string,
    NaN). When the unit is ``%``, the value is multiplied by the capacity.
    Numeric strings are parsed exactly once; anything else is rejected so an
    invalid reading is never clamped into an apparently valid kWh.
    """
    if unit not in ("kWh", "%"):
        raise ValueError("soc_entity_unit must be 'kWh' or '%'")
    if isinstance(state, bool):
        return None
    if isinstance(state, (int, float)):
        value = float(state)
    elif isinstance(state, str):
        try:
            value = float(state)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(value):
        return None
    if unit == "%":
        return value * capacity_kwh / 100.0
    return value


def _observation_clock():
    """Read wall time, never replace an entity's actual observation timestamp."""
    return datetime.now(timezone.utc)


def _ev_charging_priority(base):
    """Resolve a literal priority or a cached HA dropdown without refreshing devices."""
    settings = read_car_charging_config(base)
    priority = settings.get("ev_charging_priority", "cost") if isinstance(settings, dict) else None
    if isinstance(priority, str) and priority.startswith(("input_select.", "select.")):
        observed = _read_entity_state(base, priority)
        return observed["state"] if observed else None
    return priority


def _options(base):
    """Read a dated expected session and entity-driven evidence without treating them as permission to switch."""
    settings = read_car_charging_config(base)
    if settings is None or settings == {}:
        return None
    if not isinstance(settings, dict):
        raise ValueError("Car planning inputs must be a mapping")
    allowed = {
        "mode",
        "session",
        "load_port_limit_w",
        "ev_pause_percent",
        "ev_restart_percent",
        "min_off_minutes",
        "beam_width",
        "terminal_min_percent",
        "soc_max_age_minutes",
        "relay_entity",
        "connection_entity",
        "permission_entity",
        "soc_entity",
        "soc_entity_unit",
        "connection_entity_states",
        "connection_warning_states",
        "vehicle_state_validity",
        "import_limit_w",
        "control_model",
        "allow_stale_shadow_soc",
        "apply_verified",
        "ev_executor_lease_verified",
        "connection_observed_at_entity",
        "soc_observed_at_entity",
        "grid_charge_support",
        "confirmed_session",
        "execution_authorised",
        "connection_error_acknowledgement",
        "ev_charging_priority",
        "battery_min_window_minutes",
        "self_consumption_first",
        "household_fallback",
        "home_entity",
        "ev_partial_target_authorised",
        "recurring_session",
    }
    if set(settings) - allowed:
        raise ValueError("Unknown car planning input")
    mode = settings.get("mode", "off")
    if mode == "off":
        return None
    if mode not in ("shadow", "apply"):
        raise ValueError("car_charging_plan_mode must be off, shadow or apply")
    from car_charging_config import measurement_policy

    measurement_policy(base)
    control_model = settings.get("control_model", "native")
    if control_model not in ("native", "deye_tou", "deye_solarman"):
        raise ValueError("Unsupported EV/battery control model")
    ev_priority = _ev_charging_priority(base)
    if ev_priority not in ("cost", "asap", "now", "solar"):
        raise ValueError("EV charging priority must be cost, asap, now or solar")
    from battery_window import selected_window_minutes, planning_window

    battery_min_window = selected_window_minutes(base)
    if ev_priority in ("asap", "now", "solar") and (control_model != "deye_solarman" or not settings.get("grid_charge_support")):
        raise ValueError("ASAP, NOW and SOLAR charging require shared grid-support controls")
    self_consumption_first = settings.get("self_consumption_first", False)
    if type(self_consumption_first) is not bool:
        raise ValueError("self_consumption_first must be boolean")
    if self_consumption_first and (control_model != "deye_solarman" or not settings.get("grid_charge_support")):
        raise ValueError("Self-consumption preference requires shared grid-support controls")
    if type(settings.get("allow_stale_shadow_soc", False)) is not bool:
        raise ValueError("allow_stale_shadow_soc must be boolean")
    if type(settings.get("apply_verified", False)) is not bool:
        raise ValueError("apply_verified must be boolean")
    if type(settings.get("ev_executor_lease_verified", False)) is not bool:
        raise ValueError("ev_executor_lease_verified must be boolean")
    if type(settings.get("execution_authorised", False)) is not bool:
        raise ValueError("execution_authorised must be boolean")
    for name in ("household_fallback", "ev_partial_target_authorised"):
        if type(settings.get(name, False)) is not bool:
            raise ValueError("EV routing and partial authorisation must be boolean")
    if settings.get("home_entity") is not None and (not isinstance(settings["home_entity"], str) or not settings["home_entity"].startswith("device_tracker.")):
        raise ValueError("home_entity must be a vehicle device tracker")
    if mode == "apply" and (control_model not in ("deye_tou", "deye_solarman") or settings.get("allow_stale_shadow_soc", False)):
        raise ValueError("Apply requires supported real controls and cannot use stale SOC assumptions")
    for name in ("connection_observed_at_entity", "soc_observed_at_entity"):
        if settings.get(name) is not None and (not isinstance(settings[name], str) or not settings[name]):
            raise ValueError("Observation timestamp entities must be non-empty strings")
    _number(base.soc_max, "soc_max", 0.001)
    _number(base.reserve, "reserve", 0, base.soc_max)
    from car_charging_recurring import recurring_enabled, resolve_recurring_session

    recurring = None
    if recurring_enabled(settings):
        if "session" in settings or "confirmed_session" in settings:
            raise ValueError("Recurring and dated sessions are mutually exclusive")
        recurring = resolve_recurring_session(base, settings)
        if recurring["status"] != "ready":
            raise ValueError("Recurring session is unavailable: " + recurring["reason"])
        settings = {**settings, "session": recurring["session"]}
    session = settings.get("session")
    if not isinstance(session, dict) or set(session) != {"available_from", "departure", "soc_kwh", "soc_observed_at"}:
        raise ValueError("A complete dated car_charging_session is required")
    now = base.now_utc.astimezone(timezone.utc)
    relay_entity = settings.get("relay_entity")
    soc_entity = settings.get("soc_entity")
    soc_entity_unit = settings.get("soc_entity_unit", "kWh")
    connection_entity = settings.get("connection_entity")
    permission_entity = settings.get("permission_entity")
    from car_charging_observation import vehicle_policy

    connection_entity_states, _, _ = vehicle_policy(settings, base.args)
    if relay_entity is not None and (not isinstance(relay_entity, str) or not relay_entity):
        raise ValueError("relay_entity must be a non-empty string")
    if soc_entity is not None and (not isinstance(soc_entity, str) or not soc_entity):
        raise ValueError("soc_entity must be a non-empty string")
    if connection_entity is not None and (not isinstance(connection_entity, str) or not connection_entity):
        raise ValueError("connection_entity must be a non-empty string")
    if permission_entity is not None and (not isinstance(permission_entity, str) or not permission_entity):
        raise ValueError("permission_entity must be a non-empty string")
    if connection_entity_states is not None:
        if not isinstance(connection_entity_states, (list, tuple)) or not connection_entity_states or not all(isinstance(item, str) for item in connection_entity_states):
            raise ValueError("connection_entity_states must be a non-empty list of strings")
    if not isinstance(soc_entity_unit, str) or soc_entity_unit not in ("kWh", "%"):
        raise ValueError("soc_entity_unit must be 'kWh' or '%'")
    # Live EV SoC retrieval: a configured soc_entity MUST be honoured. A fresh
    # entity observation replaces the dated session value before the dated
    # observation's age check, so a live reading is never invalidated by a
    # stale dated session. A configured but unavailable/invalid entity is a
    # blocker, not a fallback. The explicit per-car manual SoC target
    # (car_charging_manual_soc_kwh) overrides the live entity so a deliberate
    # user setting is never silently replaced.
    relay_payload = _read_entity_state(base, relay_entity) if relay_entity else None
    soc_payload = _read_entity_state(base, soc_entity) if soc_entity else None
    connection_payload = _read_entity_state(base, connection_entity) if connection_entity else None
    permission_payload = _read_entity_state(base, permission_entity) if permission_entity else None
    # Predbat's forecast reference is floored to five minutes. A real observation
    # later in that block is NOT future-dated. Read the observation clock after
    # the cached states so a concurrent HA update is compared with current time.
    observed_now = _aware(_observation_clock(), "observation_clock")
    initial_window_mode, initial_window_remaining = planning_window(base, now, observed_now, battery_min_window)
    manual_target_active = bool(getattr(base, "car_charging_manual_soc", None) and base.car_charging_manual_soc and base.car_charging_manual_soc[0])
    manual_target_kwh = None
    if manual_target_active:
        try:
            manual_target_kwh = float(base.car_charging_soc[0])
        except (TypeError, ValueError, IndexError):
            manual_target_kwh = None
    ev_soc = None
    ev_soc_source = None
    stale_soc = None
    soc_observed_at = None
    soc_age_minutes = None
    entity_blockers = []
    support_w = 0.0
    support = settings.get("grid_charge_support")
    if support is not None:
        if control_model != "deye_solarman" or not isinstance(support, dict) or set(support) != {"enabled_entity", "limit_entity"}:
            raise ValueError("Grid charge support requires explicit cached entities and shared controls")
        if any(not isinstance(value, str) or not value for value in support.values()):
            raise ValueError("Grid charge support entities must be non-empty strings")
        enabled = _read_entity_state(base, support["enabled_entity"])
        limit = _read_entity_state(base, support["limit_entity"])
        try:
            if enabled is None or enabled["state"] != "on" or limit is None or isinstance(limit["state"], bool):
                raise ValueError("Grid support unavailable")
            support_w = _number(float(limit["state"]), "grid_charge_support_w", 1, _number(settings.get("import_limit_w", getattr(base, "import_limit", 0)), "import_limit_w", 1))
            if enabled["last_updated"] > observed_now or limit["last_updated"] > observed_now:
                raise ValueError("Future grid support observation")
        except (TypeError, ValueError):
            support_w = 0.0
            entity_blockers.append("grid_charge_support_unavailable")
    confirmed_observation = recurring["observation"] if recurring else None
    if settings.get("confirmed_session") is not None:
        from car_charging_observation import confirmed_session_observation

        if control_model != "deye_solarman" or manual_target_active:
            raise ValueError("Confirmed session cannot override a manual SOC target")
        confirmed_observation = confirmed_session_observation(base, settings, observed_now)
    if mode == "apply" and not confirmed_observation and (manual_target_active or soc_entity is None):
        # The native manual SOC can advance from prediction; it is not a fresh
        # vehicle observation, even when another vehicle timestamp is recent.
        entity_blockers.append("measured_ev_soc_required")
    if confirmed_observation:
        ev_soc = confirmed_observation["ev_soc"]
        ev_soc_source = confirmed_observation["ev_soc_source"]
        soc_observed_at = confirmed_observation["soc_observed_at"]
        soc_age_minutes = confirmed_observation["soc_observation_age_minutes"]
    elif manual_target_kwh is not None:
        ev_soc = _number(manual_target_kwh, "manual_ev_soc", 0, base.car_charging_battery_size[0])
        ev_soc_source = "manual_soc_target"
    elif soc_entity is None:
        # No live entity configured: dated session is the only source.
        observed = _aware(session["soc_observed_at"], "soc_observed_at") if isinstance(session["soc_observed_at"], datetime) else _parse_entity_timestamp(session["soc_observed_at"], "soc_observed_at")
        max_age = _number(settings.get("soc_max_age_minutes", 60), "soc_max_age_minutes", 1, 1440)
        age = (observed_now - observed).total_seconds() / 60
        if age < 0 or age > max_age:
            raise ValueError("EV SoC observation is stale or future-dated")
        ev_soc = _number(session["soc_kwh"], "soc_kwh", 0, base.car_charging_battery_size[0])
        ev_soc_source = "dated_session"
    else:
        # Live entity configured: a fresh observation is the only valid source.
        # The dated session is NOT a fallback; absence, invalid value or stale
        # timestamp all become structured blockers.
        if soc_payload is None:
            entity_blockers.append("soc_entity_unavailable")
        else:
            ev_soc_kwh = _parse_soc_raw(soc_payload["state"], soc_entity_unit, base.car_charging_battery_size[0])
            if ev_soc_kwh is None:
                entity_blockers.append("soc_entity_invalid_value")
            elif ev_soc_kwh < 0 or ev_soc_kwh > base.car_charging_battery_size[0]:
                entity_blockers.append("soc_entity_out_of_bounds")
            else:
                soc_max_age = _number(settings.get("soc_max_age_minutes", 60), "soc_max_age_minutes", 1, 1440)
                observed = soc_payload["last_updated"]
                if mode == "apply":
                    # Unchanged values need not change HA last_updated. Apply
                    # requires the independent field-specific observation time.
                    stamp_payload = _read_entity_state(base, settings.get("soc_observed_at_entity"))
                    if stamp_payload is None:
                        raise ValueError("Apply requires a physical SOC observation timestamp")
                    observed = _parse_entity_timestamp(stamp_payload["state"], "physical_soc_observation")
                    soc_max_age = min(soc_max_age, 3)
                    if soc_payload["last_updated"] > observed_now:
                        entity_blockers.append("soc_entity_timestamp_future")
                soc_age_minutes = (observed_now - observed).total_seconds() / 60
                soc_observed_at = observed.isoformat()
                if soc_age_minutes < 0 or soc_age_minutes > soc_max_age:
                    entity_blockers.append("soc_entity_stale_or_future")
                    if soc_age_minutes > soc_max_age:
                        stale_soc = ev_soc_kwh
                else:
                    ev_soc = ev_soc_kwh
                    ev_soc_source = "entity_cache"
    arrival = _aware(session["available_from"], "available_from") if isinstance(session["available_from"], datetime) else _parse_entity_timestamp(session["available_from"], "available_from")
    departure = _aware(session["departure"], "departure") if isinstance(session["departure"], datetime) else _parse_entity_timestamp(session["departure"], "departure")
    if arrival >= departure or (departure - now).total_seconds() > base.forecast_minutes * 60:
        raise ValueError("Expected EV session is outside the forecast/departure window")
    start_offset = max(math.ceil((arrival - now).total_seconds() / (60 * PREDICT_STEP)) * PREDICT_STEP, 0)
    end_offset = max(0, math.floor((departure - now).total_seconds() / (60 * PREDICT_STEP)) * PREDICT_STEP)
    pause = _number(settings.get("ev_pause_percent", 15), "ev_pause_percent", 0, 100)
    restart = _number(settings.get("ev_restart_percent", 25), "ev_restart_percent", pause, 100)
    off_minutes = _number(settings.get("min_off_minutes", 10), "min_off_minutes", PREDICT_STEP, 60)
    beam = _number(settings.get("beam_width", 128), "beam_width", 8, 512)
    if beam != int(beam) or off_minutes != int(off_minutes):
        raise ValueError("Beam width and cooldown must be integers")
    # Relay-driven initial state: only the relay entity defines the physical
    # switch state. The state value ('on'/'off') is read explicitly so an 'off'
    # can never masquerade as 'on'. The relay's last_changed defines the
    # elapsed off time; rounding DOWN means the cooldown remaining is never
    # shortened below the configured value.
    relay_state = relay_payload["state"] if relay_payload else None
    if relay_state not in (None, "on", "off"):
        raise ValueError("relay_entity state must be 'on' or 'off'")
    relay_last_changed = relay_payload["last_changed"] if relay_payload else None
    if relay_last_changed is not None:
        elapsed_seconds = (observed_now - relay_last_changed).total_seconds()
        if elapsed_seconds < 0:
            entity_blockers.append("relay_timestamp_future")
            elapsed_minutes_floor = 0
        else:
            elapsed_minutes_floor = int(elapsed_seconds // 60)
    else:
        elapsed_minutes_floor = None
    if relay_entity and relay_payload is None:
        entity_blockers.append("relay_entity_unavailable")
    initially_on = relay_state == "on"
    # The solver wants elapsed OFF time, NOT remaining cooldown. A currently ON
    # relay stays initially ON regardless of how recently it was energised.
    initial_off_minutes = 0 if initially_on else min(elapsed_minutes_floor, int(off_minutes)) if elapsed_minutes_floor is not None else int(off_minutes)
    # Connection entity is informational only. It carries the raw physical
    # plug-state string, interpreted through configuration, with freshness
    # uncertain; we surface the observation in the report but never gate
    # the relay-driven model on it.
    connection_state_observed = connection_payload["state"] if connection_payload else None
    connection_in_allowlist = None
    if connection_state_observed is not None and connection_entity_states is not None:
        connection_in_allowlist = connection_state_observed in connection_entity_states
    # Permission entity: exact 'on' means the user has explicitly permitted
    # the schedule. Missing observation is reported as not_permitted rather
    # than assumed ready.
    permission_state = permission_payload["state"] if permission_payload else None
    permission_granted = permission_state == "on"
    permission_observed = permission_state is not None
    return {
        "mode": mode,
        "settings": deepcopy(settings) if mode == "apply" else None,
        "arrival": start_offset,
        "control_model": control_model,
        "grid_charge_support_w": support_w,
        "ev_charging_priority": ev_priority,
        "battery_min_window_minutes": battery_min_window,
        "initial_window_mode": initial_window_mode,
        "initial_window_remaining_minutes": initial_window_remaining,
        "self_consumption_first": self_consumption_first,
        "deadline": end_offset,
        "departure_passed": departure <= observed_now,
        "house_only": mode == "apply" and departure <= observed_now,
        "planning_reference_at": now.isoformat(),
        "observations_checked_at": observed_now.isoformat(),
        "planning_alignment_seconds": (observed_now - now).total_seconds(),
        "ev_soc": ev_soc,
        "ev_soc_source": ev_soc_source,
        "stale_soc": stale_soc,
        "allow_stale_shadow_soc": settings.get("allow_stale_shadow_soc", False),
        "soc_observed_at": soc_observed_at,
        "soc_observation_age_minutes": soc_age_minutes,
        "confirmed_observation": confirmed_observation,
        "recurring_evidence": {key: recurring[key] for key in ("identity", "session", "latest_deadline")} if recurring else None,
        "load_cap": _number(settings.get("load_port_limit_w"), "load_port_limit_w", 1) / 1000,
        "import_limit_w": _number(settings.get("import_limit_w", getattr(base, "import_limit", 0)), "import_limit_w", 1),
        "pause": pause,
        "restart": restart,
        "off_minutes": int(off_minutes),
        "beam": int(beam),
        "terminal_min": _number(settings.get("terminal_min_percent", base.reserve / base.soc_max * 100), "terminal_min_percent", 0, 100) * base.soc_max / 100,
        "initially_on": initially_on,
        "initial_off_minutes": initial_off_minutes,
        "entity_config": {
            "relay_entity": relay_entity,
            "soc_entity": soc_entity,
            "soc_entity_unit": soc_entity_unit,
            "connection_entity": connection_entity,
            "connection_entity_states": connection_entity_states,
            "permission_entity": permission_entity,
        },
        "entity_observations": {
            "relay_state": relay_state,
            "connection_state_observed": connection_state_observed,
            "connection_in_allowlist": connection_in_allowlist,
            "permission_observed": permission_observed,
            "permission_granted": permission_granted,
        },
        "elapsed_minutes_since_relay_change": elapsed_minutes_floor,
        "entity_blockers": entity_blockers,
    }


def _collect_blockers(base, options):
    """Collect every independent reason the shadow planner cannot run, never first-reason stop."""
    blockers = []
    if base.num_cars != 1 or base.num_inverters != 1:
        blockers.append("multi_topology")
    if base.iboost_enable:
        blockers.append("iboost_unsupported")
    if not base.car_energy_reported_load:
        blockers.append("load_not_reported")
    template_model = options and options["control_model"] == "deye_solarman"
    if not base.set_charge_window or (not template_model and not base.set_charge_freeze) or base.set_charge_freeze_only or base.set_charge_low_power:
        blockers.append("freeze_required")
    if template_model:
        from inverter_service_control import validate_template

        inverters = getattr(base, "inverters", None)
        if not isinstance(inverters, list) or len(inverters) != 1:
            blockers.append("single_template_inverter_required")
        else:
            try:
                validate_template(inverters[0])
            except (ValueError, TypeError, KeyError, AttributeError):
                blockers.append("template_contract_invalid")
        if base.export_limit != 0 or not base.set_discharge_during_charge:
            blockers.append("template_dispatch_model_mismatch")
        if base.args.get("charge_freeze_service") not in (None, "", []):
            # The native target-equals-current branch can call this even without
            # an explicit freeze. The narrow template must not acquire that writer.
            blockers.append("template_freeze_service_unsupported")
    if not base.car_charging_from_battery:
        blockers.append("ev_from_battery_required")
    if getattr(base, "manual_charge_times", None):
        blockers.append("manual_charge_times")
    if getattr(base, "manual_demand_times", None):
        blockers.append("manual_demand_times")
    if getattr(base, "manual_export_times", None):
        blockers.append("manual_export_times")
    if getattr(base, "manual_freeze_charge_times", None):
        blockers.append("manual_freeze_charge_times")
    if getattr(base, "manual_freeze_export_times", None):
        blockers.append("manual_freeze_export_times")
    if getattr(base, "all_active_keep_max", None):
        blockers.append("active_keep_ceiling")
    if getattr(base, "io_adjusted", None):
        blockers.append("io_adjusted")
    if type(base.forecast_minutes) is not int or not 0 < base.forecast_minutes <= 2880 or base.forecast_minutes % PREDICT_STEP:
        blockers.append("forecast_window")
    # Entity evidence blockers from the live observation stage. Manual event
    # overrides, missing freeze gates, and stale or absent entity observations
    # all share the same first-class blocker treatment.
    blockers.extend(options.get("entity_blockers", []) if options else [])
    return blockers


def _forecast_inputs(base, options):
    """Require a complete series; never fill missing load/PV with zero."""
    _number(options["import_limit_w"], "import_limit_w", 1)
    _number(base.soc_max, "soc_max", 0.001)
    _number(base.soc_kw, "soc_kw", base.reserve, base.soc_max)
    _number(base.car_charging_limit[0], "car_charging_limit", 0, base.car_charging_battery_size[0])
    _number(base.car_charging_rate[0], "car_charging_rate", 0.001)
    for name in ("inverter_loss", "battery_loss", "battery_loss_discharge", "car_charging_loss"):
        _number(getattr(base, name), name, 0.001, 1)
    for name in ("inverter_limit", "battery_rate_max_charge", "battery_rate_max_charge_dc", "battery_rate_max_discharge", "export_limit", "battery_rate_max_scaling", "battery_rate_max_scaling_discharge"):
        _number(getattr(base, name), name)
    if options["pause"] < base.reserve / base.soc_max * 100:
        raise ValueError("EV pause threshold must not be below the domestic reserve")
    for offset in range(0, base.forecast_minutes, PREDICT_STEP):
        for name in ("load_minutes_step", "load_minutes_step10", "pv_forecast_minute_step", "pv_forecast_minute10_step"):
            _number(getattr(base, name).get(offset), name)
        for minute in range(base.minutes_now + offset, base.minutes_now + offset + PREDICT_STEP):
            for name in ("rate_import", "rate_export"):
                _number(getattr(base, name).get(minute), name, -1000000, 1000000)
            _number(base.all_active_keep.get(minute, 0), "domestic_manual_floor", 0, 100)


def build_candidate(base, options):
    """Search coupled fixed-rate EV and battery decisions using native forecast inputs."""
    from car_charging_solver import BatteryConfig, EVConfig, PlannerConfig, SiteConfig, Slot, plan

    slots = []
    conservative = options.get("forecast_basis") == "envelope"
    for index, minute in enumerate(range(0, base.forecast_minutes, PREDICT_STEP)):
        absolute = base.minutes_now + minute
        import_rate = sum(base.rate_import[m] for m in range(absolute, absolute + PREDICT_STEP)) / PREDICT_STEP
        export_rate = sum(base.rate_export[m] for m in range(absolute, absolute + PREDICT_STEP)) / PREDICT_STEP
        pv_step = min(base.pv_forecast_minute_step[minute], base.pv_forecast_minute10_step[minute]) if conservative else base.pv_forecast_minute_step[minute]
        load_step = max(base.load_minutes_step[minute], base.load_minutes_step10[minute]) if conservative else base.load_minutes_step[minute]
        pv_ac = pv_step * (base.inverter_loss if base.inverter_hybrid else 1)
        if not base.inverter_hybrid and base.pv_ac_limit > 0:
            pv_ac = min(pv_ac, base.pv_ac_limit * PREDICT_STEP)
        floor_percent = max(base.all_active_keep.get(m, 0) for m in range(absolute, absolute + PREDICT_STEP))
        slots.append(Slot(index, minute, PREDICT_STEP, load_step, pv_ac, import_rate, export_rate, floor_percent * base.soc_max / 100))
    battery = BatteryConfig(
        capacity_kwh=base.soc_max,
        initial_kwh=base.soc_kw,
        reserve_kwh=base.reserve,
        max_charge_kw=min(base.battery_rate_max_charge, base.inverter_limit) * 60 * base.battery_rate_max_scaling / base.inverter_loss * (min(base.charge_scaling10, 1) if conservative else 1),
        max_discharge_kw=min(base.battery_rate_max_discharge, base.inverter_limit) * 60 * base.battery_rate_max_scaling_discharge * base.inverter_loss,
        charge_efficiency=base.battery_loss * base.inverter_loss,
        discharge_efficiency=base.battery_loss_discharge * base.inverter_loss,
        end_min_kwh=max(options["terminal_min"], base.reserve),
        cycle_cost_per_kwh=_number(base.metric_battery_cycle, "metric_battery_cycle"),
        terminal_value_per_kwh=_number(base.battery_value_rate(base.minutes_now + base.forecast_minutes) * base.metric_battery_value_scaling, "terminal_value"),
    )
    ev = EVConfig(
        battery_kwh=base.car_charging_battery_size[0],
        start_soc_kwh=options["ev_soc"],
        stop_soc_kwh=options["ev_soc"] if options.get("house_only") else base.car_charging_limit[0],
        rate_kw=base.car_charging_rate[0],
        efficiency=base.car_charging_loss,
        deadline_min=options["deadline"],
        availability=tuple(
            s.index
            for s in slots
            if options["arrival"] <= s.start_min and s.start_min + s.duration_min <= options["deadline"] and (options.get("ev_charging_priority") != "solar" or _solar_surplus_kwh(base, s.start_min) >= base.car_charging_rate[0] * PREDICT_STEP / 60 - 1e-9)
        ),
        pause_battery_percent=options["pause"],
        restart_battery_percent=options["restart"],
        min_off_minutes=options["off_minutes"],
        initially_on=options["initially_on"],
        initial_off_minutes=options["initial_off_minutes"],
    )
    support_w = _number(options.get("grid_charge_support_w", 0), "grid_charge_support_w", 0, options["import_limit_w"])
    if support_w and (options.get("mode") not in ("shadow", "apply") or options["control_model"] != "deye_solarman"):
        raise ValueError("Grid charge support requires the shared control model")
    block = options.get("ev_continuous_block")
    if block is not None:
        from dataclasses import replace

        if not support_w or len(block) != 2 or any(type(value) is not int or value % PREDICT_STEP for value in block) or not options["arrival"] <= block[0] < block[1] <= options["deadline"]:
            raise ValueError("Invalid shadow continuous EV block")
        ev = replace(ev, availability=tuple(index for index in ev.availability if block[0] <= slots[index].start_min < block[1]))
    site = SiteConfig(options["import_limit_w"] / 1000, base.export_limit * 60, options["load_cap"], support_w / 1000 if support_w else None)
    modes = ("demand", "charge") if options["control_model"] == "deye_solarman" else ("demand", "hold", "charge")
    result = plan(
        slots,
        ev,
        battery,
        site,
        PlannerConfig(
            beam_width=options["beam"],
            allow_limited_discharge=options["control_model"] == "deye_tou",
            allowed_battery_modes=modes,
            economic_diversity=options.get("mode") == "shadow" and options["control_model"] == "deye_solarman" and options.get("economic_diversity", True),
            initial_battery_mode="demand" if options.get("initial_demand_only") else None,
            require_ev_start=options.get("ev_charging_priority") == "now" and not options.get("house_only") and options["ev_soc"] < base.car_charging_limit[0] - 0.002,
            solar_ev_only=options.get("ev_charging_priority") == "solar",
            battery_min_window_minutes=options.get("battery_min_window_minutes", 5),
            initial_window_mode=options.get("initial_window_mode"),
            initial_window_remaining_minutes=options.get("initial_window_remaining_minutes", 0),
        ),
    )
    return result, slots


def _validate_candidate_modes(options, result):
    """Reject unsupported solver actions before replay/staging, never reinterpret a hold as demand."""
    if options["control_model"] == "deye_solarman":
        if any(action.battery_mode not in ("demand", "charge") or getattr(action, "discharge_limit_kw", None) is not None for action in result.actions):
            raise UnsupportedCarPlanning("unsupported_template_candidate_controls")


def _native_windows(base, result, slots):
    """Translate modes to native charge/freeze windows without altering executable lists."""
    if len(result.actions) != len(slots) or any(action.slot_index != slot.index for action, slot in zip(result.actions, slots)):
        raise ValueError("EV candidate must contain one action for every native forecast step")
    ev_windows = []
    charge_windows = []
    limits = []
    for action, slot in zip(result.actions, slots):
        start = base.minutes_now + slot.start_min
        end = start + slot.duration_min
        if action.ev_action == "on":
            energy = base.car_charging_rate[0] * slot.duration_min / 60
            ev_windows.append({"start": start, "end": end, "kwh": energy, "average": slot.import_price, "cost": energy * slot.import_price, "octopus": False})
        if action.battery_mode in ("charge", "hold"):
            target = base.soc_max if action.battery_mode == "charge" else base.reserve
            if charge_windows and charge_windows[-1]["end"] == start and limits[-1] == target:
                charge_windows[-1]["end"] = end
            else:
                charge_windows.append({"start": start, "end": end, "average": slot.import_price})
                limits.append(target)
    return ev_windows, charge_windows, limits


def _replay(base, options, result, slots, windows, scenario):
    """Independently check raw native import, reserve and EV hysteresis using a new Prediction."""
    ev_windows, charge_windows, limits = windows
    model = copy(base)
    model.car_charging_soc = [options["ev_soc"]] * PREDBAT_MAX_CARS
    model.car_charging_soc_next = [None] * PREDBAT_MAX_CARS
    full_slot_target = options["ev_soc"] + sum(w["kwh"] for w in ev_windows) * base.car_charging_loss + 1
    model.car_charging_limit_model = [full_slot_target] * PREDBAT_MAX_CARS
    model.car_charging_slots = [ev_windows] + [[] for _ in range(PREDBAT_MAX_CARS - 1)]
    model.prediction_kernel_enable = False
    support_w = _number(options.get("grid_charge_support_w", 0), "grid_charge_support_w", 0, options["import_limit_w"])
    if support_w and (options.get("mode") not in ("shadow", "apply") or options["control_model"] != "deye_solarman"):
        raise ValueError("Grid charge support replay requires shared controls")
    model.plan_grid_charge_support_w = support_w
    model.debug_enable = False
    model.metric_standing_charge = 0
    model.import_limit = options["import_limit_w"]
    model.plan_preview_charge_limits = {minute: limit * 100 / base.soc_max for window, limit in zip(charge_windows, limits) for minute in range(window["start"], window["end"], PREDICT_STEP)}
    limited = any(getattr(action, "discharge_limit_kw", None) is not None for action in result.actions)
    if limited:
        from car_charging_replay import replay_discharge_limits

        pred = replay_discharge_limits(model, result, slots, scenario)
    else:
        pred = Prediction(model, base.pv_forecast_minute_step, base.pv_forecast_minute10_step, base.load_minutes_step, base.load_minutes_step10)
        pred.run_prediction(limits, charge_windows, [], [], scenario, end_record=base.forecast_minutes, save="test")
    diagnostic = getattr(pred, "import_limit_diagnostics", None)
    if support_w:
        evidence = getattr(pred, "plan_grid_support_import", None)
        if not isinstance(evidence, dict) or set(evidence) != set(range(0, base.forecast_minutes, PREDICT_STEP)):
            raise ValueError("Incomplete grid support replay evidence")
        peaks = [_number(row[1], "grid_support_peak") for row in evidence.values()]
        diagnostic = {"checked_minutes": len(peaks) * PREDICT_STEP, "peak_w": max(peaks, default=0), "minutes_over": sum(value - options["import_limit_w"] > 1e-6 for value in peaks) * PREDICT_STEP}
    elif not diagnostic or getattr(base, "import_limit", None) != options["import_limit_w"]:
        # Native saved grid
        # power is rounded to 0.001 kW: use its upper rounding bound, not a false
        # exact measurement. When available, the companion raw diagnostic wins.
        powers = list(pred.predict_grid_power.values())
        peak = max((max(0, value * 1000 + 0.5) for value in powers), default=0)
        diagnostic = {"checked_minutes": len(powers) * PREDICT_STEP, "peak_w": peak, "minutes_over": sum(value * 1000 + 0.5 > options["import_limit_w"] for value in powers) * PREDICT_STEP}
    reasons = []
    if not diagnostic or diagnostic["checked_minutes"] != base.forecast_minutes:
        reasons.append("incomplete_native_prediction")
    elif diagnostic["minutes_over"]:
        reasons.append("site_import_exceeded")
    trace = pred.predict_soc
    rounding = getattr(pred, "rounding_bound_kwh", 0)
    reserve_min = min(list(trace.values()) + [pred.final_soc])
    if reserve_min < base.reserve - 0.002:
        reasons.append("domestic_reserve")
    terminal_rounding = rounding if options["terminal_min"] > base.reserve + 0.002 else 0
    if pred.final_soc - terminal_rounding < max(options["terminal_min"], base.reserve) - 0.002:
        reasons.append("terminal_reserve")
    previous_on = options["initially_on"]
    previous_off_minutes = options["initial_off_minutes"]
    off_minutes = previous_off_minutes
    for slot_index, (action, slot) in enumerate(zip(result.actions, slots)):
        on = action.ev_action == "on"
        start_soc = trace[slot.start_min] - (slot_index * 0.00005 if limited else 0)
        end_soc = trace.get(slot.start_min + slot.duration_min, pred.final_soc) - ((slot_index + 1) * 0.00005 if limited else 0)
        if min(start_soc, end_soc) < getattr(slot, "battery_min_kwh", 0) - 0.002:
            reasons.append("domestic_manual_floor")
        if on and min(start_soc, end_soc) <= options["pause"] * base.soc_max / 100 + 0.002:
            reasons.append("ev_pause_threshold")
        if on and not previous_on and start_soc < options["restart"] * base.soc_max / 100 + 0.002:
            reasons.append("ev_restart_threshold")
        if on and (slot.start_min < options["arrival"] or slot.start_min + slot.duration_min > options["deadline"]):
            reasons.append("ev_availability_or_deadline")
        if on and not previous_on and off_minutes < options["off_minutes"]:
            reasons.append("ev_cooldown")
        scenario_load = base.load_minutes_step10 if scenario == 1 else base.load_minutes_step
        load_kw = scenario_load[slot.start_min] * 60 / slot.duration_min + (base.car_charging_rate[0] if on else 0)
        if load_kw > options["load_cap"] + 1e-9:
            reasons.append("load_port_exceeded")
        if on and options.get("ev_charging_priority") == "solar":
            # PV must cover the full house/EV reservation and the battery cannot
            # discharge. Native domestic PV-charging conversion losses remain in
            # the raw grid/cost trace; solar-only EV is not a whole-site zero-import claim.
            if action.battery_mode != "demand" or end_soc < start_soc - 0.002:
                reasons.append("solar_only_native_supply")
            if _solar_surplus_kwh(base, slot.start_min) < base.car_charging_rate[0] * slot.duration_min / 60 - 1e-9:
                reasons.append("solar_surplus_forecast_insufficient")
        off_minutes = 0 if on else (0 if previous_on else off_minutes) + slot.duration_min
        previous_on = on
    ev_target = options["ev_soc"] if options.get("house_only") else base.car_charging_limit[0]
    if pred.final_car_soc[0] < ev_target - 0.002:
        reasons.append("ev_target_not_met")
    check = {
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
        "checked_minutes": diagnostic["checked_minutes"] if diagnostic else 0,
        "peak_import_w": diagnostic["peak_w"] if diagnostic else None,
        "final_battery_kwh": pred.final_soc,
        "final_ev_kwh": pred.final_car_soc[0],
        "validated_ev_target_kwh": ev_target,
        "minimum_battery_kwh": reserve_min,
        "horizon_energy_cost": pred.final_metric - base.cost_today_sofar,
        "model": "native_python_grid_charge_support" if support_w else "native_python_segmented_rate_replay" if limited else "native_python_full_power_ev_envelope",
        "soc_rounding_bound_kwh": rounding,
        "_charge_peak_kwh": [
            max(_number(pred.final_soc if minute == base.forecast_minutes else trace[minute], "charge_window_soc") for minute in range(window["start"] - base.minutes_now, window["end"] - base.minutes_now + PREDICT_STEP, PREDICT_STEP))
            for window in charge_windows
        ],
    }
    # Presentation consumes this exact independent replay, not the native best
    # plan or the solver's simpler battery model. A preview failure must never
    # change acceptance or interrupt the control cycle.
    try:
        from car_charging_view import build_replay_preview

        check["_preview_rows"] = build_replay_preview(model, pred, result, slots, scenario)
    except Exception as exc:
        check["preview_unavailable"] = True
        _log_failure(base, "preview", exc)
    return check


def _replay_candidate(base, options, result, slots):
    """Reduce shadow and opted-in grid-support ceilings to paired peaks plus one percent.

    These are real candidate limits, not cosmetic row values. Replay both scenarios
    again with the reduced limits and accept only their independent native checks.
    A ceiling cannot override domestic minima, hardware rates or any execution gate.
    """
    windows = _native_windows(base, result, slots)
    checks = {name: _replay(base, options, result, slots, windows, scenario) for name, scenario in (("nominal", 0), ("pv10", 1))}
    peaks = {name: check.pop("_charge_peak_kwh", None) for name, check in checks.items()}
    if (options.get("mode") == "shadow" or options.get("grid_charge_support_w")) and all(check["passed"] for check in checks.values()) and all(isinstance(value, list) and len(value) == len(windows[1]) for value in peaks.values()):
        limits = []
        for index, original in enumerate(windows[2]):
            peak = max(peaks[name][index] for name in ("nominal", "pv10"))
            target = min(100, math.ceil(peak * 100 / base.soc_max + 1)) * base.soc_max / 100
            limits.append(target if original == base.soc_max else original)
        windows = (windows[0], windows[1], limits)
        checks = {name: _replay(base, options, result, slots, windows, scenario) for name, scenario in (("nominal", 0), ("pv10", 1))}
        for check in checks.values():
            check.pop("_charge_peak_kwh", None)
    return windows, checks


def _best_effort_shadow(base, options):
    """Find a new paired-safe partial EV schedule without changing the requested target.

    At most eight bounded searches prefer more EV delivery before cost. A search miss is
    not infeasibility: the result is the best validated alternative found, never
    a claim of maximum reachability. Apply requires separate explicit partial
    authorisation and shared support; closed sessions cannot use it.
    """
    attempts = []
    authorised = options.get("mode") == "apply" and read_car_charging_config(base).get("ev_partial_target_authorised") is True and options.get("grid_charge_support_w")
    if (options.get("mode") != "shadow" and not authorised) or options.get("control_model") != "deye_solarman" or options.get("house_only") or options.get("deadline", 0) <= 0:
        return None, attempts
    requested = base.car_charging_limit[0]
    low, high = options["ev_soc"], requested
    if requested <= low + 0.002:
        return None, attempts
    best = None
    for _ in range(8):
        # Native SOC values are rounded and EV reservations are full five-minute
        # steps. One-percent target probes avoid chasing meaningless fractions;
        # the actual delivered energy is still taken from both native replays.
        step = base.car_charging_battery_size[0] / 100
        target = step * math.floor((low + high) / (2 * step))
        if target <= low + 0.002:
            break
        model = copy(base)
        model.car_charging_limit = list(base.car_charging_limit)
        model.car_charging_limit[0] = target
        trial = {**options, "forecast_basis": "envelope"}
        result, slots = build_candidate(model, trial)
        _validate_candidate_modes(options, result)
        passed = False
        if result.actions:
            windows, checks = _replay_candidate(model, trial, result, slots)
            passed = all(check["passed"] and _number(check["final_ev_kwh"], "partial_ev_delivery") >= target - 0.002 for check in checks.values())
            if passed:
                delivered = min(check["final_ev_kwh"] for check in checks.values())
                if best is None or delivered > best[5] + 0.002 or (abs(delivered - best[5]) <= 0.002 and result.total_cost < best[0].total_cost):
                    best = (result, slots, windows, checks, target, delivered)
        attempts.append({"forecast_basis": "envelope", "beam_width": trial["beam"], "planning_ev_target_kwh": target, "solver_status": result.status, "native_passed": passed})
        if passed:
            low = target
        else:
            high = target
    return best, attempts


def _solar_surplus_kwh(base, minute):
    """Reserve conservative household demand before converting available PV to AC energy."""
    pv = min(base.pv_forecast_minute_step[minute], base.pv_forecast_minute10_step[minute])
    pv *= base.inverter_loss if base.inverter_hybrid else 1
    if not base.inverter_hybrid and base.pv_ac_limit > 0:
        pv = min(pv, base.pv_ac_limit * PREDICT_STEP)
    pv = min(pv, base.inverter_limit * PREDICT_STEP)
    return pv - max(base.load_minutes_step[minute], base.load_minutes_step10[minute])


def _priority_block_starts(base, options, first, last, duration):
    """Rank bounded opportunities, with explicit deadline fallback rather than a silent delay."""
    priority = options.get("ev_charging_priority", "cost")
    if options["initially_on"]:
        return [(0, "active_continuation")]
    if priority == "now":
        return [(0, "immediate")] if first == 0 else []
    starts = list(range(first, last + 1, PREDICT_STEP))
    ev_step = base.car_charging_rate[0] * PREDICT_STEP / 60
    if priority == "solar":
        return [(start, "solar_surplus_only") for start in starts if all(_solar_surplus_kwh(base, minute) >= ev_step - 1e-9 for minute in range(start, start + duration, PREDICT_STEP))][:12]
    if priority == "asap":
        solar = [start for start in starts if start <= 30 and all(_solar_surplus_kwh(base, minute) >= ev_step - 1e-9 for minute in range(start, start + min(15, duration), PREDICT_STEP))]
        current_rate = base.rate_import[base.minutes_now]
        previous = base.minutes_now - 1
        while previous >= 0 and base.rate_import.get(previous) == current_rate:
            previous -= 1
        current_discount = base.rate_import.get(previous, current_rate) > current_rate + 1e-9
        cheaper = [start for start in starts if start not in solar and (base.rate_import[base.minutes_now + start] < current_rate - 1e-9 or (current_discount and base.rate_import[base.minutes_now + start] == current_rate))]
        preferred = [(start, "near_term_solar") for start in solar] + [(start, "next_cheaper_band") for start in cheaper]
        # Failed preferred blocks never erase an earlier feasible deadline path.
        preferred = preferred[:12]
        tried = {start for start, _ in preferred}
        return preferred + [(start, "deadline_or_no_opportunity") for start in starts if start not in tried][:12]
    # Include PV opportunity cost when selecting the twelve coupled searches.
    # The final ranking still uses the whole-house solver objective and both replays.
    ranked = []
    for start in starts:
        estimate = 0
        for minute in range(start, start + duration, PREDICT_STEP):
            solar = min(ev_step, max(0, _solar_surplus_kwh(base, minute)))
            absolute = base.minutes_now + minute
            estimate += (ev_step - solar) * base.rate_import[absolute] + solar * base.rate_export[absolute]
        ranked.append((estimate, start))
    return [(start, "coupled_cost") for _, start in sorted(ranked)[:12]]


def _continuous_grid_support_shadow(base, options):
    """Prefer one paired-safe fixed-rate block within the finite departure horizon.

    This is an explicit continuity preference, not a calibrated restart penalty or
    global cost optimum. Rank valid blocks by the existing conservative solver
    objective (including cycle cost and terminal energy). COST also compares an
    unrestricted envelope schedule, so continuity cannot hide a cheaper valid plan.
    An active relay tries continuation without inventing a new OFF interval.
    ASAP tries nearby solar, then the next cheaper band, then an earlier safe
    deadline path. SOLAR reserves conservative surplus and never falls back to grid.
    NOW checks only the current interval; every other search also requires the
    initial EV action, so no retry or partial alternative can silently defer it.
    The search remains bounded; failure is not proof an earlier start is impossible.
    Closed sessions cannot use this preference.
    """
    if not options.get("grid_charge_support_w") or options.get("mode") not in ("shadow", "apply") or options.get("control_model") != "deye_solarman" or options.get("house_only"):
        return None, []
    required = max(base.car_charging_limit[0] - options["ev_soc"], 0)
    duration = math.ceil(required / (base.car_charging_rate[0] * base.car_charging_loss) * 60 / PREDICT_STEP - 1e-9) * PREDICT_STEP
    first = 0 if options["initially_on"] else max(options["arrival"], math.ceil(max(options["off_minutes"] - options["initial_off_minutes"], 0) / PREDICT_STEP) * PREDICT_STEP)
    last = options["deadline"] - duration
    if duration <= 0 or last < first:
        return None, []
    priority = options.get("ev_charging_priority", "cost")
    ranked = _priority_block_starts(base, options, first, last, duration)
    best, attempts = None, []
    selected = None
    for start, reason in ranked:
        for initial_demand in (True, False) if options.get("self_consumption_first") else (False,):
            trial = {**options, "forecast_basis": "envelope", "ev_continuous_block": (start, start + duration), "initial_demand_only": initial_demand}
            result, slots = build_candidate(base, trial)
            passed = False
            if result.actions:
                _validate_candidate_modes(options, result)
                windows, checks = _replay_candidate(base, trial, result, slots)
                passed = all(check["passed"] for check in checks.values())
                if passed and (best is None or result.total_cost < best[0].total_cost):
                    best = result, slots, windows, checks
                    selected = len(attempts)
            attempts.append({"start_offset": start, "end_offset": start + duration, "solver_status": result.status, "native_passed": passed, "initial_demand_only": initial_demand, "priority_reason": reason})
            if passed:
                break
        if priority != "cost" and passed:
            break
    if priority == "cost" and not options["initially_on"]:
        trial = {**options, "forecast_basis": "envelope"}
        result, slots = build_candidate(base, trial)
        passed = False
        if result.actions:
            _validate_candidate_modes(options, result)
            windows, checks = _replay_candidate(base, trial, result, slots)
            passed = all(check["passed"] for check in checks.values())
            if passed and (best is None or result.total_cost < best[0].total_cost - 1e-9):
                best = result, slots, windows, checks
                selected = len(attempts)
        attempts.append({"solver_status": result.status, "native_passed": passed, "priority_reason": "coupled_cost_flexible_schedule"})
    if selected is not None:
        attempts[selected]["selected"] = True
    return best, attempts


def _empty_report(status):
    """Build fresh, deliberately non-executable output; no stale schedule survives a failed run."""
    return {
        "status": status,
        "generated_at": _observation_clock().isoformat(),
        "ready_to_activate": False,
        "execution_enabled": False,
        "enforced": False,
        "actuator_signals_modified": False,
        "proposed_ev_windows": [],
        "proposed_charge_windows": [],
        "proposed_charge_limits": [],
        "proposed_discharge_power_limits": [],
        "native_checks": {},
    }


def _log_failure(base, stage, error):
    """Identify programmer/transport failures without echoing private exception values."""
    try:
        base.log("Warn: EV shadow planner failed during {} ({}); executable plan was not modified".format(stage, type(error).__name__))
    except Exception:
        pass


def _publish_report(base, report, publish):
    """A diagnostic publication or logger failure must not abort the native control cycle."""
    # The new per-run report owns its preview. Failed/disabled calculations
    # cannot inherit an earlier trajectory, and checks retain their original
    # validation schema rather than mixing presentation with acceptance.
    preview = {}
    for scenario, check in report.get("native_checks", {}).items():
        rows = check.pop("_preview_rows", None)
        if rows:
            preview[scenario] = rows
    if set(preview) == {"nominal", "pv10"}:
        report["preview"] = preview
    base.car_charging_report = report
    if publish:
        try:
            # Keep the full replay in memory for the read-only web page, not
            # duplicated into HA's recorder on every calculation.
            from car_charging_publish import publish_car_charging_report

            publish_car_charging_report(base, report)
        except Exception as exc:
            report["publication_failed"] = True
            _log_failure(base, "publication", exc)


def evaluate_car_charging(base, publish=True):
    """Evaluate a candidate; only explicit apply may stage it for later native execution."""
    settings = read_car_charging_config(base)
    if (isinstance(settings, dict) and settings.get("mode") == "apply") or getattr(base, "car_charging_pending", None) is not None:
        # A previous successful candidate cannot survive a failed calculation.
        base.car_charging_pending = None
    report = _empty_report("invalid_input")
    stage = "configuration"
    try:
        from car_charging_policy import household_reason

        reason = household_reason(base)
        if reason:
            report = _empty_report("household_fallback")
            report.update(execution_reason=reason, requested_mode="apply", ev_available=False, household_executor="native", ev_soc_not_required=True)
            from car_charging_recurring import recurring_enabled, resolve_recurring_session

            if recurring_enabled(settings):
                recurring = resolve_recurring_session(base)
                report["recurring_evidence"] = {"status": recurring["status"], "latest_deadline": recurring.get("latest_deadline")}
                from car_charging_arrival import arrival_evidence

                report["arrival_forecast"] = arrival_evidence(base)
            _publish_report(base, report, publish)
            return report
        options = _options(base)
        if options is None:
            if getattr(base, "car_charging_report", None):
                report = _empty_report("disabled")
                _publish_report(base, report, publish)
            return None
        stage = "evidence"
        model_base = base
        execution_requirements = []
        if options["control_model"] == "deye_tou" and options["mode"] == "shadow":
            # A counterfactual MODEL, never a capability override on the actual
            # inverter. The separate DEYE adapter defines these ToU actions;
            # its commissioning and native command equivalence remain gates.
            model_base = copy(base)
            model_base.set_charge_freeze = True
            model_base.set_discharge_during_charge = True
            model_base.set_charge_low_power = False
            model_base.export_limit = 0
            execution_requirements = ["deye_hardware_commissioning", "standing_hardware_current_limits", "native_executor_equivalence", "connection_and_session_permission", "tou_table_validation"]
        evidence = {
            "ev_initial_state": {
                "initially_on": options["initially_on"],
                "initial_off_minutes": options["initial_off_minutes"],
                "ev_soc_kwh": options["ev_soc"],
                "ev_soc_source": options["ev_soc_source"],
                **options["entity_observations"],
            },
            "ready_to_activate": False,
            "control_model": options["control_model"],
            "grid_charge_support_w": options["grid_charge_support_w"],
            "ev_charging_priority": options["ev_charging_priority"],
            "battery_min_window_minutes": options["battery_min_window_minutes"],
            "battery_window_initial_mode": options["initial_window_mode"],
            "battery_window_remaining_minutes": options["initial_window_remaining_minutes"],
            "battery_window_policy": "minimum_mode_duration_no_silent_relaxation_safety_gates_take_precedence",
            "self_consumption_first": options["self_consumption_first"],
            "grid_charge_support_convention": "charge_request_with_load_priority_and_bounded_battery_support" if options["grid_charge_support_w"] else "disabled",
            "confirmed_observation": options["confirmed_observation"],
            "recurring_evidence": options["recurring_evidence"],
            "counterfactual_controls": options["control_model"] == "deye_tou" and options["mode"] == "shadow",
            "requested_mode": options["mode"],
            "activation_requirements": execution_requirements,
            "manual_domestic_floor_slots": len(base.all_active_keep),
            "model_export_limit_w": model_base.export_limit * 60000,
            "planning_reference_at": options["planning_reference_at"],
            "observations_checked_at": options["observations_checked_at"],
            "soc_observed_at": options["soc_observed_at"],
            "soc_observation_age_minutes": options["soc_observation_age_minutes"],
            "planning_alignment_seconds": options["planning_alignment_seconds"],
            "session_closed": options["departure_passed"],
            "ev_session_renewed": False,
        }
        if options["departure_passed"]:
            # A house-only continuation is not evidence of an EV deadline met.
            # Keep the actual measured SOC; never invent a full/zero EV value.
            evidence["observed_ev_target_met"] = None if options["ev_soc"] is None or options["ev_soc_source"] == "closed_session_baseline" else options["ev_soc"] >= base.car_charging_limit[0]
            evidence["target_met_by_departure"] = None
        blockers = _collect_blockers(model_base, options)
        stale_hypothesis = options["mode"] == "shadow" and options["allow_stale_shadow_soc"] and options["stale_soc"] is not None and blockers == ["soc_entity_stale_or_future"] and not options["departure_passed"]
        if stale_hypothesis:
            # Preserve the freshness failure. This is a separately labelled
            # mathematical scenario, not a replacement live observation.
            options = {**options, "ev_soc": options["stale_soc"], "ev_soc_source": "stale_entity_assumption"}
            evidence["uses_stale_soc_assumption"] = True
            evidence["input_blockers"] = list(blockers)
            evidence["activation_requirements"] = [*evidence["activation_requirements"], "fresh_ev_soc_observation"]
            evidence["ev_initial_state"] = {**evidence["ev_initial_state"], "ev_soc_kwh": options["ev_soc"], "ev_soc_source": options["ev_soc_source"]}
        # Always surface the entity observations even when topology blockers
        # gate the solver. Missing optional entities are reported explicitly
        # instead of being silently assumed away.
        entity_observations = options.get("entity_observations", {})
        if options["departure_passed"] and not options["house_only"]:
            report = _empty_report("departure_passed")
            report.update(evidence)
            report["observed_ev_target_met"] = None if options["ev_soc"] is None else options["ev_soc"] >= base.car_charging_limit[0]
            report["target_met_by_departure"] = None
            report["reason"] = "Dated session closed; a later SOC reading cannot prove the departure target was met on time"
            _publish_report(base, report, publish)
            return report
        if blockers and not stale_hypothesis:
            report = _empty_report("unsupported")
            report["execution_blockers"] = list(blockers)
            report["reason"] = "Shadow planner is not configured for the current evidence set"
            report.update(evidence)
            report["entity_observations"] = entity_observations
            _publish_report(base, report, publish)
            return report
        stage = "forecast"
        report.update(evidence)
        _forecast_inputs(model_base, options)
        stage = "solver"
        result, slots = build_candidate(model_base, options)
        continuous = None
        if options["grid_charge_support_w"]:
            continuous, report["continuous_block_attempts"] = _continuous_grid_support_shadow(model_base, options)
            if continuous is not None:
                result, slots = continuous[:2]
            report["ev_run_strategy"] = "single_continuous_block_preferred_no_restart_calibration" if continuous is not None else "ordinary_bounded_search"
            if options["ev_charging_priority"] == "asap":
                report["ev_run_strategy"] = "next_solar_or_cheaper_opportunity" if continuous is not None else "asap_deadline_fallback_bounded_search"
            elif options["ev_charging_priority"] == "now":
                report["ev_run_strategy"] = "immediate_paired_continuous_block" if continuous is not None else "immediate_start_required_bounded_search"
            elif options["ev_charging_priority"] == "solar":
                report["ev_run_strategy"] = "solar_surplus_only"
            elif continuous is not None:
                report["ev_run_strategy"] = "lowest_validated_coupled_cost_found"
            report["priority_selection_reason"] = next((attempt["priority_reason"] for attempt in report["continuous_block_attempts"] if attempt.get("selected")), "bounded_search_fallback")
        if options["control_model"] == "deye_solarman":
            report["supported_battery_modes"] = ["demand", "charge"]
            report["search_beam_attempts"] = [{"beam_width": options["beam"], "solver_status": result.status}]
            if not result.actions and options["beam"] < 512:
                # A bounded search miss is not infeasibility. One explicit, reported
                # retry retains the exact capabilities/inputs and stays within the cap.
                retry_options = {**options, "beam": 512}
                result, slots = build_candidate(model_base, retry_options)
                report["search_beam_attempts"].append({"beam_width": 512, "solver_status": result.status})
            _validate_candidate_modes(options, result)
        if not result.actions:
            report["status"] = "conditional_no_plan_found" if stale_hypothesis else "no_plan_found"
            report["execution_blockers"] = list(blockers)
            report["entity_observations"] = entity_observations
            report["initial_execution_state_assumption"] = "relay_value_and_elapsed_off_time" if options["entity_config"]["relay_entity"] else "assumed_off_for_minimum_cooldown_no_relay"
        else:
            stage = "native_replay"
            windows, checks = continuous[2:] if continuous is not None else _replay_candidate(model_base, options, result, slots)
            selected_basis = "envelope" if continuous is not None else "nominal"
            attempts = [{"forecast_basis": selected_basis, "solver_status": result.status, "native_passed": all(check["passed"] for check in checks.values())}]
            if options["control_model"] in ("deye_tou", "deye_solarman") and not attempts[0]["native_passed"]:
                # One bounded retry uses the forecast envelope and PV10 charge
                # delivery assumption. Native nominal AND PV10 still validate
                # the same full-rate commands; this is not charge modulation.
                retry_options = {**options, "forecast_basis": "envelope"}
                retry_result, retry_slots = build_candidate(model_base, retry_options)
                _validate_candidate_modes(options, retry_result)
                retry_checks = {}
                if options["control_model"] == "deye_solarman" and not retry_result.actions and retry_options["beam"] < 512:
                    attempts.append({"forecast_basis": "envelope", "beam_width": retry_options["beam"], "solver_status": retry_result.status, "native_passed": False})
                    retry_options = {**retry_options, "beam": 512}
                    retry_result, retry_slots = build_candidate(model_base, retry_options)
                    _validate_candidate_modes(options, retry_result)
                if retry_result.actions:
                    retry_windows, retry_checks = _replay_candidate(model_base, retry_options, retry_result, retry_slots)
                    result, slots, windows, checks = retry_result, retry_slots, retry_windows, retry_checks
                    selected_basis = "envelope"
                attempts.append({"forecast_basis": "envelope", "beam_width": retry_options["beam"], "solver_status": retry_result.status, "native_passed": bool(retry_checks) and all(check["passed"] for check in retry_checks.values())})
                if options["mode"] == "shadow" and options["control_model"] == "deye_solarman" and not all(check["passed"] for check in checks.values()):
                    # Different finite pruning orders can lose different valid paths.
                    # Recover once with the established order; never relax replay gates.
                    fallback_options = {**options, "forecast_basis": "envelope", "economic_diversity": False}
                    fallback_result, fallback_slots = build_candidate(model_base, fallback_options)
                    fallback_checks = {}
                    if fallback_result.actions:
                        _validate_candidate_modes(options, fallback_result)
                        fallback_windows, fallback_checks = _replay_candidate(model_base, fallback_options, fallback_result, fallback_slots)
                        if all(check["passed"] for check in fallback_checks.values()):
                            result, slots, windows, checks = fallback_result, fallback_slots, fallback_windows, fallback_checks
                            selected_basis = "envelope"
                    attempts.append(
                        {
                            "forecast_basis": "envelope",
                            "beam_width": options["beam"],
                            "pruning_order": "established",
                            "solver_status": fallback_result.status,
                            "native_passed": bool(fallback_checks) and all(check["passed"] for check in fallback_checks.values()),
                        }
                    )
            from car_charging_replay import discharge_limit_w

            power_limits = []
            command_steps = []
            for action, slot in zip(result.actions, slots):
                power = discharge_limit_w(model_base, action)
                command_steps.append(
                    {
                        "start": model_base.minutes_now + slot.start_min,
                        "end": model_base.minutes_now + slot.start_min + slot.duration_min,
                        "target_soc": next(
                            (round(limit * 100 / model_base.soc_max) for window, limit in zip(windows[1], windows[2]) if window["start"] <= model_base.minutes_now + slot.start_min < window["end"]),
                            0,
                        )
                        if action.battery_mode == "charge"
                        else 0,
                        "freeze": action.battery_mode == "hold",
                        "power_w": power,
                        "ev_on": action.ev_action == "on",
                    }
                )
                if power is not None:
                    power_limits.append({"start": model_base.minutes_now + slot.start_min, "end": model_base.minutes_now + slot.start_min + slot.duration_min, "power_w": power})
            command_check = {"available": False, "passed": False, "reason": "Optional local DEYE adapter/mapping is not installed"}
            if options["control_model"] == "deye_tou" and base.args.get("deye_shadow"):
                try:
                    from deye_local_shadow import check_plan_deye_commands

                    command_check = check_plan_deye_commands(base, command_steps)
                except ImportError as exc:
                    command_check["reason"] = "Local DEYE command-check module is unavailable or incompatible"
                    command_check["error_type"] = type(exc).__name__
            report.update(
                status=("conditional_" if stale_hypothesis else "") + ("candidate" if all(check["passed"] for check in checks.values()) else "native_rejected"),
                proposed_ev_windows=windows[0],
                proposed_charge_windows=windows[1],
                proposed_charge_limits=windows[2],
                native_checks=checks,
                search_attempts=attempts,
                proposed_discharge_power_limits=power_limits,
                deye_command_check=command_check,
                solver_forecast_basis=selected_basis,
                solver_objective=result.total_cost,
                solver_grid_cost=sum(action.grid_import_kwh * action.import_price - action.grid_export_kwh * action.export_price for action in result.actions),
                cost_unit=base.currency_symbols[1],
                cost_convention="solver_objective_includes_cycle_cost_and_terminal_credit; native_horizon_energy_cost_excludes_both_and_standing_charge",
                ev_window_convention="full_step_power_reservation_not_partial_target_energy",
                charge_limit_convention="paired_native_replay_window_peak_plus_one_percentage_point_revalidated" if options["mode"] == "shadow" or options["grid_charge_support_w"] else "native_full_capacity_ceiling",
                initial_execution_state_assumption="relay_value_and_elapsed_off_time" if options["entity_config"]["relay_entity"] else "assumed_off_for_minimum_cooldown_no_relay",
                ev_initial_state={
                    "initially_on": options["initially_on"],
                    "initial_off_minutes": options["initial_off_minutes"],
                    "elapsed_minutes_since_relay_change": options["elapsed_minutes_since_relay_change"],
                    "ev_soc_source": options["ev_soc_source"],
                    "ev_soc_kwh": options["ev_soc"],
                },
                entity_observations=entity_observations,
                solver_ev_ac_kwh=result.diagnostics.ev_delivered_total_kwh,
                solver_pv_curtailed_kwh=result.diagnostics.pv_curtailed_total_kwh,
                bounded_search=True,
                execution_blockers=list(blockers),
            )
            if options["grid_charge_support_w"] and all(check["passed"] for check in checks.values()):
                delivered = min(check["final_ev_kwh"] for check in checks.values())
                requested = model_base.car_charging_limit[0]
                report.update(
                    requested_ev_target_kwh=requested,
                    requested_ev_target_percent=requested * 100 / model_base.car_charging_battery_size[0],
                    ev_departure_at=options["settings"]["session"]["departure"] if options["settings"] else read_car_charging_config(base)["session"]["departure"],
                    planning_ev_target_kwh=requested,
                    planned_ev_kwh_by_departure=min(delivered, model_base.car_charging_battery_size[0]),
                    planned_ev_percent_by_departure=min(100, delivered * 100 / model_base.car_charging_battery_size[0]),
                    requested_ev_target_met=delivered >= requested - 0.002,
                    ev_target_shortfall_kwh=max(0, requested - delivered),
                )
            if options["mode"] == "apply" and report["status"] == "candidate":
                from car_charging_execute import execution_fingerprint, command_fingerprint

                report["apply_candidate_staged"] = True
                base.car_charging_pending = {
                    "report": deepcopy(report),
                    "options": deepcopy(options),
                    "commands": deepcopy(command_steps),
                    "ev_windows": deepcopy(windows[0]),
                    "initial_battery_soc_kwh": base.soc_kw,
                    "fingerprint": execution_fingerprint(base),
                }
                if options["grid_charge_support_w"]:
                    base.car_charging_pending["command_fingerprint"] = command_fingerprint(command_steps)
                    base.car_charging_pending["replay_inputs"] = deepcopy((result, slots, windows))
        partial_authorised = options["mode"] == "apply" and settings.get("ev_partial_target_authorised") is True and bool(options["grid_charge_support_w"])
        if (options["mode"] == "shadow" or partial_authorised) and report["status"] in ("no_plan_found", "conditional_no_plan_found", "native_rejected", "conditional_native_rejected"):
            stage = "partial_shadow_search"
            partial, partial_attempts = _best_effort_shadow(model_base, options)
            report["partial_search_attempts"] = partial_attempts
            if partial is not None:
                result, slots, windows, checks, planning_target, delivered = partial
                if partial_authorised and options["ev_charging_priority"] in ("asap", "now", "solar"):
                    partial_base = copy(model_base)
                    partial_base.car_charging_limit = list(model_base.car_charging_limit)
                    partial_base.car_charging_limit[0] = planning_target
                    continuous, report["partial_continuous_block_attempts"] = _continuous_grid_support_shadow(partial_base, options)
                    if continuous is not None:
                        result, slots, windows, checks = continuous
                        delivered = min(check["final_ev_kwh"] for check in checks.values())
                        report["ev_run_strategy"] = {"now": "immediate_paired_partial_continuous_block", "asap": "next_opportunity_partial_continuous_block", "solar": "solar_surplus_only_partial_block"}[options["ev_charging_priority"]]
                        report["priority_selection_reason"] = next(attempt["priority_reason"] for attempt in report["partial_continuous_block_attempts"] if attempt.get("selected"))
                requested = model_base.car_charging_limit[0]
                target_met = delivered >= requested - 0.002
                report.update(
                    full_target_search_status=report["status"],
                    status=("conditional_" if stale_hypothesis else "") + ("candidate" if target_met else "partial_candidate"),
                    proposed_ev_windows=windows[0],
                    proposed_charge_windows=windows[1],
                    proposed_charge_limits=windows[2],
                    native_checks=checks,
                    proposed_discharge_power_limits=[],
                    solver_forecast_basis="envelope",
                    solver_objective=result.total_cost,
                    solver_grid_cost=sum(action.grid_import_kwh * action.import_price - action.grid_export_kwh * action.export_price for action in result.actions),
                    requested_ev_target_kwh=requested,
                    requested_ev_target_percent=requested * 100 / model_base.car_charging_battery_size[0],
                    ev_departure_at=options["settings"]["session"]["departure"] if options["settings"] else read_car_charging_config(base)["session"]["departure"],
                    planning_ev_target_kwh=planning_target,
                    planned_ev_kwh_by_departure=delivered,
                    planned_ev_percent_by_departure=delivered * 100 / model_base.car_charging_battery_size[0],
                    requested_ev_target_met=target_met,
                    ev_target_shortfall_kwh=max(0, requested - delivered),
                    partial_plan_convention="best_paired_validated_ev_delivery_found_not_maximum_reachability_proof",
                    solver_ev_ac_kwh=result.diagnostics.ev_delivered_total_kwh,
                    solver_pv_curtailed_kwh=result.diagnostics.pv_curtailed_total_kwh,
                    bounded_search=True,
                    execution_enabled=False,
                    enforced=False,
                    actuator_signals_modified=False,
                    execution_blockers=list(blockers),
                    ev_initial_state={**evidence["ev_initial_state"], "ev_soc_kwh": options["ev_soc"], "ev_soc_source": options["ev_soc_source"]},
                    charge_limit_convention="paired_native_replay_window_peak_plus_one_percentage_point_revalidated",
                    cost_unit=base.currency_symbols[1],
                    cost_convention="solver_objective_includes_cycle_cost_and_terminal_credit; native_horizon_energy_cost_excludes_both_and_standing_charge",
                    ev_window_convention="full_step_power_reservation_not_partial_target_energy",
                    entity_observations=entity_observations,
                )
                if partial_authorised:
                    from car_charging_policy import partial_fingerprint, partial_metadata_valid
                    from car_charging_execute import execution_fingerprint, command_fingerprint

                    report["partial_target_authorised"] = True
                    if not partial_metadata_valid(base, report):
                        raise ValueError("Inconsistent partial replay evidence")
                    commands = []
                    for action, slot in zip(result.actions, slots):
                        target = next((round(limit * 100 / base.soc_max) for window, limit in zip(windows[1], windows[2]) if window["start"] <= base.minutes_now + slot.start_min < window["end"]), 0)
                        commands.append(
                            {
                                "start": base.minutes_now + slot.start_min,
                                "end": base.minutes_now + slot.start_min + slot.duration_min,
                                "target_soc": target if action.battery_mode == "charge" else 0,
                                "freeze": False,
                                "power_w": None,
                                "ev_on": action.ev_action == "on",
                            }
                        )
                    report["apply_candidate_staged"] = True
                    base.car_charging_pending = {
                        "report": deepcopy(report),
                        "options": deepcopy(options),
                        "commands": commands,
                        "ev_windows": deepcopy(windows[0]),
                        "initial_battery_soc_kwh": base.soc_kw,
                        "fingerprint": execution_fingerprint(base),
                        "command_fingerprint": command_fingerprint(commands),
                        "replay_inputs": deepcopy((result, slots, windows)),
                        "partial_fingerprint": partial_fingerprint(report),
                    }
        if options["ev_charging_priority"] == "solar" and not options.get("house_only"):
            report["solar_policy"] = "conservative_household_surplus_only_with_fresh_meter_gate_no_grid_or_battery_ev_fallback"
            if report["status"] in ("no_plan_found", "conditional_no_plan_found", "native_rejected", "conditional_native_rejected"):
                requested = model_base.car_charging_limit[0]
                report.update(
                    reason="No paired-safe solar-only EV delivery was validated; waiting for surplus without grid fallback",
                    requested_ev_target_kwh=requested,
                    requested_ev_target_percent=requested * 100 / base.car_charging_battery_size[0],
                    planned_ev_kwh_by_departure=options["ev_soc"],
                    planned_ev_percent_by_departure=options["ev_soc"] * 100 / base.car_charging_battery_size[0],
                    requested_ev_target_met=options["ev_soc"] >= requested - 0.002,
                    ev_target_shortfall_kwh=max(0, requested - options["ev_soc"]),
                    priority_selection_reason="solar_surplus_not_validated",
                )
        if options["ev_charging_priority"] == "now" and not options.get("house_only") and options["ev_soc"] < model_base.car_charging_limit[0] - 0.002:
            if report["status"] in ("no_plan_found", "conditional_no_plan_found", "native_rejected", "conditional_native_rejected"):
                report["ev_run_strategy"] = "immediate_start_not_validated"
                report["execution_blockers"] = [*report.get("execution_blockers", []), "immediate_start_not_validated"]
                report["reason"] = "No paired-safe EV start was validated for the current control interval; NOW does not defer the start"
    except UnsupportedCarPlanning as exc:
        report = _empty_report("unsupported")
        report["reason"] = str(exc)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        input_stage = stage in ("configuration", "forecast", "evidence")
        report = _empty_report("invalid_input" if input_stage else "error")
        report["reason"] = "Check the dated session, supported settings and complete finite native forecasts" if input_stage else "Shadow calculation failed; executable native plan was not modified"
        if not input_stage:
            report["failure_stage"] = stage
            report["error_type"] = type(exc).__name__
            _log_failure(base, stage, exc)
    except Exception as exc:
        report = _empty_report("error")
        report["reason"] = "Unexpected shadow-planner failure; executable native plan was not modified"
        report["failure_stage"] = stage
        report["error_type"] = type(exc).__name__
        _log_failure(base, stage, exc)
    _publish_report(base, report, publish)
    return report
