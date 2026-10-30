"""EV/battery execution through the native inverter and the sole external EV executor.

Off and shadow are strict no-ops. HA observations are never hardware confirmation.
This module neither sends EV switch services nor changes standing inverter limits.
"""

from car_charging_config import read_car_charging_config

from copy import copy, deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math

from const import PREDBAT_MAX_CARS, PREDICT_STEP, MINUTE_WATT

RECEIPT_TTL_SECONDS = 300
PLAN_REVALIDATION_MAX_AGE_SECONDS = 600
OBSERVATION_MAX_AGE_SECONDS = 180
EV_HANDOFF_SECONDS = 30
SOLAR_RECEIPT_SECONDS = 60
SOLAR_OBSERVATION_SECONDS = 60


class CarChargingBlocked(ValueError):
    """Carry a public reason code, never raw configuration or transport details."""


def _require(condition, reason):
    """Fail closed on a violated execution contract."""
    if not condition:
        raise CarChargingBlocked(reason)


def _clock():
    """Use the same real observation clock as the native input adapter."""
    from car_charging_plan import _observation_clock, _aware

    return _aware(_observation_clock(), "execution_clock")


def _stamp(value):
    """Read a strictly offset-aware timestamp through the native parser."""
    from car_charging_plan import _parse_entity_timestamp, _aware

    return _aware(value, "execution_timestamp") if isinstance(value, datetime) else _parse_entity_timestamp(value, "execution_timestamp")


def _finite(value, minimum=0, maximum=None):
    """Accept finite native numeric values without coercing booleans or strings."""
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)


def _requested(base):
    """Recognise only the explicit apply setting without touching runtime state."""
    settings = read_car_charging_config(base)
    return isinstance(settings, dict) and settings.get("mode") == "apply"


def native_plan_hook_allowed(base):
    """Do not broadcast independent legacy controls as an executed combined plan."""
    return not _requested(base)


def native_charger_control_conflict(base):
    """Reject separately polling native writers, including retained component configuration.

    These controls do not depend on the plan-executed hook. Even a temporarily
    disabled control switch can be re-enabled independently, so apply requires
    their configured control options to be off. Never change or release either
    controller here: the commissioned external executor remains the sole owner.
    """
    components = getattr(base, "components", None)
    for name, setting, runtime_setting in (
        ("hypervolt", "hypervolt_control", "hypervolt_control"),
        ("gateway", "gateway_evc_control", "gateway_evc_control"),
        ("myenergi", "myenergi_zappi_control", "zappi_control"),
        ("gecloud", "ge_cloud_evc_control", "evc_control"),
        ("ohme", "ohme_control", "ohme_control"),
        ("wallbox", "wallbox_control", "wallbox_control"),
    ):
        if base.get_arg(setting, False):
            return True
        component = components.get_component(name) if components else None
        if component is not None:
            if name == "hypervolt" and (getattr(component, "released_by_predbat", None) or getattr(component, "control_state_dirty", False) or (getattr(component, "storage", None) is not None and not getattr(component, "control_state_loaded", False))):
                return True
            if name == "wallbox" and (getattr(component, "paused_by_predbat", None) or getattr(component, "schedule_pending", None)):
                return True
            if getattr(component, "charger_control_save_pending", False) or (getattr(component, "charger_control_storage", None) is not None and getattr(component, "storage", None) is not None and not getattr(component, "charger_control_loaded", False)):
                return True
            if getattr(component, runtime_setting, False) or getattr(component, "charger_control_active", False) or getattr(component, "charger_control_state", None):
                return True
            # The shared control's None means automatic ownership, not disabled.
            # A pending release can also start a previously stopped charger, so
            # retained control above must be settled before an external lease.
            if hasattr(component, "charger_control_config") and component.charger_control_config is not False:
                return True
    return False


def execution_fingerprint(base):
    """Bind staging to actual configuration, not to the pending object's own copy."""
    names = (
        "num_cars",
        "num_inverters",
        "soc_max",
        "reserve",
        "import_limit",
        "export_limit",
        "inverter_limit",
        "battery_rate_max_charge",
        "battery_rate_max_charge_dc",
        "battery_rate_max_discharge",
        "battery_rate_max_scaling",
        "battery_rate_max_scaling_discharge",
        "inverter_loss",
        "battery_loss",
        "battery_loss_discharge",
        "car_charging_rate",
        "car_charging_limit",
        "car_charging_battery_size",
        "car_charging_loss",
        "car_charging_manual_soc",
        "car_charging_from_battery",
        "car_energy_reported_load",
        "set_charge_window",
        "set_charge_freeze",
        "set_discharge_during_charge",
        "set_charge_freeze_only",
        "set_charge_low_power",
        "set_read_only",
        "set_read_only_axle",
        "iboost_enable",
        "manual_charge_times",
        "manual_demand_times",
        "manual_export_times",
        "manual_freeze_charge_times",
        "manual_freeze_export_times",
        "all_active_keep",
        "all_active_keep_max",
        "manual_soc_max_keep",
        "io_adjusted",
    )
    payload = {name: getattr(base, name, None) for name in names}
    inverters = getattr(base, "inverters", None)
    if isinstance(inverters, list) and len(inverters) == 1 and getattr(inverters[0], "inverter_type", None) == "DeyeSolarman" and getattr(inverters[0], "inv_support_charge_freeze", None) is False:
        # The native config fetch restores the user's preference before the
        # inverter fetch disables unsupported charge freeze again. Publication
        # runs between those stages: fingerprint the SAME effective capability
        # used by native Prediction, not that transient unsupported preference.
        # _evidence/validate_template independently reject changed capabilities.
        payload["set_charge_freeze"] = False
    payload["settings"] = read_car_charging_config(base)
    payload["native_charger_control_conflict"] = native_charger_control_conflict(base)
    from car_charging_config import measurement_policy

    payload["measurements"] = measurement_policy(base)
    payload["car_vehicle_policy"] = {name: base.args.get(name) for name in ("car_charging_warning_states", "car_charging_state_validity")}
    from car_charging_plan import _ev_charging_priority

    payload["ev_charging_priority"] = _ev_charging_priority(base)
    from battery_window import selected_window_minutes

    payload["battery_min_window_minutes"] = selected_window_minutes(base)
    if payload["ev_charging_priority"] == "solar":
        payload["solar_meter_mapping"] = {name: base.args.get(name) for name in ("pv_power", "load_power", "load_power_invert", "deye_entity_prefix")}
        payload["solar_conversion"] = {name: getattr(base, name, None) for name in ("inverter_hybrid", "pv_ac_limit")}
    from car_charging_recurring import recurring_enabled, resolve_recurring_session

    if recurring_enabled(payload["settings"]):
        resolved = resolve_recurring_session(base)
        payload["recurring_session"] = {"identity": resolved.get("identity"), "departure": resolved.get("session", {}).get("departure"), "reason": resolved.get("reason")}
        payload["recurring_ready_time"] = base.car_charging_plan_time
        payload["recurring_timezone"] = base.args.get("timezone")
    payload["deye_local"] = base.args.get("deye_local")
    if isinstance(payload["settings"], dict) and payload["settings"].get("control_model") == "deye_solarman":
        payload["template_controls"] = {name: base.args.get(name) for name in ("inverter", "inverter_type", "deye_entity_prefix", "charge_start_service", "charge_stop_service", "discharge_stop_service", "charge_freeze_service")}
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cached(base, entity):
    """Require an explicitly configured cached entity; never refresh hardware."""
    from car_charging_plan import _read_entity_state

    _require(isinstance(entity, str) and bool(entity), "entity_mapping_required")
    result = _read_entity_state(base, entity)
    _require(result is not None, "entity_observation_unavailable")
    return result


def command_fingerprint(commands):
    """Bind exact staged commands to the paired replay; a valid but different target is not accepted."""
    return hashlib.sha256(json.dumps(commands, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _control_fingerprint(inverter):
    """Detect observed control drift without claiming read-back proves hardware state."""
    if getattr(inverter, "inverter_type", None) == "DeyeSolarman":
        from inverter_service_control import snapshot_template

        snapshot = snapshot_template(inverter)
    else:
        snapshot = inverter.deye_local.snapshot()
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _acknowledged_stop_state(settings, connection, now):
    """Accept only the exact retained error explicitly attributed to the operator's manual stop.

    This is not an error-state allowlist. Any subsequent state/attribute publication,
    changed departure or absent confirmation loses this exception; BMS, metering and
    all other executor guards remain authoritative.
    """
    acknowledgement = settings.get("connection_error_acknowledgement")
    if acknowledgement is None:
        return False
    _require(settings.get("control_model") == "deye_solarman" and settings.get("confirmed_session") and settings.get("grid_charge_support"), "stop_acknowledgement_scope")
    _require(isinstance(acknowledgement, dict) and set(acknowledgement) == {"state", "observed_at", "acknowledged_at", "departure"}, "stop_acknowledgement_invalid")
    from car_charging_observation import valid_connection_state

    _require(valid_connection_state(acknowledgement["state"]), "stop_acknowledgement_invalid")
    observed = _stamp(acknowledgement["observed_at"])
    accepted = _stamp(acknowledgement["acknowledged_at"])
    departure = _stamp(acknowledgement["departure"])
    _require(_stamp(settings["session"]["available_from"]) <= observed <= accepted <= now, "stop_acknowledgement_invalid")
    _require(departure == _stamp(settings["confirmed_session"]["departure"]) == _stamp(settings["session"]["departure"]), "stop_acknowledgement_invalid")
    return now < departure and connection is not None and connection["state"] == acknowledgement["state"] and connection["last_updated"] == observed


def _evidence(base, allow_read_only_transition=False):
    """Revalidate actual capabilities, overrides and independent EV evidence before use."""
    from car_charging_plan import _options, _collect_blockers

    settings = read_car_charging_config(base)
    from car_charging_policy import household_reason
    from car_charging_recurring import recurring_enabled

    _require(household_reason(base) is None, "ev_unavailable_for_execution")
    # Explicit experimental session authority is not a claim that the hardware
    # commissioning attestation is true. All electrical/control gates below remain.
    metered_session = settings.get("confirmed_session") is not None or recurring_enabled(settings)
    authorised_session = settings.get("execution_authorised") is True and metered_session and settings.get("grid_charge_support") is not None and settings.get("control_model") == "deye_solarman"
    _require(settings.get("apply_verified") is True or authorised_session, "apply_commissioning_required")
    _require(settings.get("ev_executor_lease_verified") is True, "ev_executor_lease_required")
    _require(not getattr(base, "octopus_intelligent_charging", False), "external_ev_scheduler_unsupported")
    _require(not base.get_arg("octopus_intelligent_slot", None, indirect=False), "external_ev_scheduler_unsupported")
    _require(not native_charger_control_conflict(base), "native_charger_control_unsupported")
    hooks = getattr(getattr(base, "plugin_system", None), "hooks", {})
    _require(not hooks.get("on_plan_executed"), "external_plan_consumer_unsupported")
    if not metered_session:
        stamps = [_stamp(_cached(base, settings.get(name))["state"]) for name in ("soc_observed_at_entity", "connection_observed_at_entity")]
        now = _clock()
        _require(all(0 <= (now - stamp).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS for stamp in stamps), "physical_observation_stale_or_future")
    options = _options(base)
    _require(options is not None and options["mode"] == "apply" and options["control_model"] in ("deye_tou", "deye_solarman"), "real_deye_controls_required")
    _require(not _collect_blockers(base, options), "native_inputs_or_overrides_blocked")
    _require(not base.set_read_only and not getattr(base, "set_read_only_axle", False), "read_only")
    transition = allow_read_only_transition and getattr(base, "inverter_needs_reset_force", "") == "set_read_only" and options["control_model"] == "deye_solarman" and options.get("grid_charge_support_w") and options.get("confirmed_observation")
    _require(not getattr(base, "inverter_needs_reset", False) or transition, "native_state_not_ready")
    _require(not any(getattr(base, name, False) for name in ("had_errors", "fatal_error")), "native_state_not_ready")
    _require(not base.get_arg("template", False), "template_mode")
    inverters = getattr(base, "inverters", None)
    _require(isinstance(inverters, list) and len(inverters) == 1, "single_inverter_required")
    inverter = inverters[0]
    if options["control_model"] == "deye_solarman":
        from inverter_service_control import validate_template

        _require(getattr(inverter, "inverter_type", None) == "DeyeSolarman", "template_inverter_required")
        try:
            validate_template(inverter)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CarChargingBlocked("template_contract_invalid") from None
        _require(not getattr(inverter, "in_calibration", True), "inverter_capability_or_calibration")
    else:
        _require(getattr(inverter, "inverter_type", None) == "DEYE", "local_deye_required")
        adapter = getattr(inverter, "deye_local", None)
        _require(adapter is not None, "local_deye_adapter_missing")
        _require(adapter.settings.get("control") is True and adapter.settings.get("hold_verified") is True, "deye_commissioning_required")
        _require(getattr(inverter, "inv_support_charge_freeze", False) is True and not getattr(inverter, "in_calibration", True), "inverter_capability_or_calibration")
    _require(not getattr(base, "manual_soc_max_keep", None), "manual_ceiling_unsupported")
    _require(base.export_limit == 0 and base.set_discharge_during_charge, "deye_dispatch_model_mismatch")
    _require(options["import_limit_w"] == getattr(base, "import_limit", options["import_limit_w"]), "import_limit_mismatch")
    _require(options["ev_soc_source"] == "entity_cache" or options.get("confirmed_observation") is not None, "measured_ev_soc_required")
    entities = options["entity_config"]
    relay = _cached(base, entities["relay_entity"])
    permission = _cached(base, entities["permission_entity"])
    closed_confirmation = options["house_only"] and bool(options.get("confirmed_observation"))
    connection = None if closed_confirmation else _cached(base, entities["connection_entity"])
    allowed = entities["connection_entity_states"]
    from car_charging_observation import vehicle_state_valid, log_connection_warning

    _require(closed_confirmation or vehicle_state_valid(settings, connection, base.args), "connection_not_confirmed")
    if recurring_enabled(settings):
        observation = options.get("confirmed_observation") or {}
        acknowledged = (
            observation.get("connection_error_acknowledged") is True and connection is not None and connection["state"] == observation.get("connection_error_state") and connection["last_updated"] == _stamp(observation["connection_error_observed_at"])
        )
    else:
        acknowledged = _acknowledged_stop_state(settings, connection, _clock())
    _require(closed_confirmation or acknowledged or (isinstance(allowed, (list, tuple)) and bool(allowed) and connection["state"] in allowed), "connection_not_confirmed")
    if not closed_confirmation:
        log_connection_warning(base, settings, connection)
    options["connection_error_acknowledged"] = acknowledged
    _require(permission["state"] == "on", "permission_not_granted")
    _require(relay["state"] in ("on", "off"), "relay_state_invalid")
    now = _clock()
    _require(all(payload["last_changed"] <= now and payload["last_updated"] <= now for payload in (relay, permission, connection) if payload is not None), "entity_timestamp_future")
    _require(_finite(base.soc_max, 0.001) and _finite(base.soc_kw, base.reserve, base.soc_max), "domestic_soc_invalid")
    minute = (now - base.midnight_utc).total_seconds() / 60
    floor = max(base.reserve, base.all_active_keep.get(int(minute), 0) * base.soc_max / 100)
    _require(base.soc_kw >= floor - 0.002, "domestic_manual_floor")
    _control_fingerprint(inverter)
    return options, inverter, relay, now, minute


def _solar_observation(base, options, relay, now):
    """Require fresh local surplus at full EV power, reserving measured household demand first."""
    inverter = base.inverters[0]
    _require(getattr(inverter, "inv_num_load_entities", None) == 1 and not base.get_arg("load_power_invert", False, index=0), "solar_meter_topology_unsupported")
    observations = []

    def power(entity):
        """Read explicit watt units from cached entities without refreshing hardware."""
        observed = _cached(base, entity)
        age = (now - observed["last_updated"]).total_seconds()
        _require(0 <= age < SOLAR_OBSERVATION_SECONDS and observed["last_changed"] <= now, "solar_power_stale_or_future")
        unit = observed["attributes"].get("unit_of_measurement")
        _require(unit in ("W", "kW") and not isinstance(observed["state"], bool), "solar_power_units_invalid")
        try:
            value = float(observed["state"]) * (1000 if unit == "kW" else 1)
        except (TypeError, ValueError):
            raise CarChargingBlocked("solar_power_invalid") from None
        _require(_finite(value), "solar_power_invalid")
        observations.append(observed["last_updated"])
        return value

    pv = power(base.get_arg("pv_power", None, indirect=False, index=0))
    load = power(base.get_arg("load_power", None, indirect=False, index=0))
    ev_power = 0
    if relay["state"] == "on":
        metering = options["settings"].get("recurring_session") or options["settings"].get("confirmed_session") or {}
        ev_power = power(metering.get("power_entity"))
        _require(ev_power <= load + 200 and ev_power <= base.car_charging_rate[0] * 1100, "solar_power_inconsistent")
    _require((max(observations) - min(observations)).total_seconds() <= 30, "solar_power_samples_misaligned")
    pv_ac = pv * (base.inverter_loss if base.inverter_hybrid else 1)
    if not base.inverter_hybrid and base.pv_ac_limit > 0:
        pv_ac = min(pv_ac, base.pv_ac_limit * MINUTE_WATT)
    pv_ac = min(pv_ac, base.inverter_limit * MINUTE_WATT)
    house = max(0, load - ev_power)
    surplus = pv_ac - house
    required = base.car_charging_rate[0] * 1000 + (200 if relay["state"] == "off" else 0)
    _require(surplus >= required, "solar_surplus_insufficient")
    return {"observed_at": min(observations).isoformat(), "surplus_w": surplus, "required_w": required, "household_load_w": house, "pv_ac_w": pv_ac, "ev_metered_w": ev_power}


def _ev_permitted(base, options, relay, now, on):
    """Check hysteresis and exact elapsed OFF seconds independently of battery mode."""
    if not on:
        return
    _require(not options["departure_passed"], "departure_passed")
    _require(base.soc_kw > options["pause"] * base.soc_max / 100, "ev_pause_threshold")
    _require(options["ev_soc"] < base.car_charging_limit[0], "ev_target_already_reached")
    session = options["settings"]["session"]
    _require(_stamp(session["available_from"]) <= now < _stamp(session["departure"]), "ev_outside_session")
    if relay["state"] == "off":
        _require(base.soc_kw >= options["restart"] * base.soc_max / 100, "ev_restart_threshold")
        _require((now - relay["last_changed"]).total_seconds() >= options["off_minutes"] * 60, "ev_cooldown")
    if options["ev_charging_priority"] == "solar":
        return _solar_observation(base, options, relay, now)


def _intent(command, inverter, charge_ceiling=False):
    """Validate exact bounded commands; do not clamp a rejected plan into another plan."""
    _require(isinstance(command, dict), "invalid_command")
    target = command.get("target_soc")
    bounded = charge_ceiling and getattr(inverter, "inverter_type", None) == "DeyeSolarman" and type(target) is int and math.ceil(inverter.reserve_percent) < target <= 100
    _require(type(target) is int and (target in (0, 100) or bounded), "invalid_target")
    _require(type(command.get("freeze")) is bool and type(command.get("ev_on")) is bool, "invalid_command_boolean")
    _require(not command["freeze"] or command["target_soc"] == 0, "invalid_hold_target")
    if getattr(inverter, "inverter_type", None) == "DeyeSolarman":
        _require(command["freeze"] is False and command.get("power_w") is None, "unsupported_template_command")
        return command["target_soc"], False, None
    cap = min(inverter.deye_local.settings["max_discharge_power_w"], inverter.battery_rate_max_discharge * MINUTE_WATT)
    power = command.get("power_w")
    _require(_finite(cap, 1), "invalid_discharge_cap")
    _require(power is None or (type(power) is int and _finite(power, 0, cap)), "discharge_power_out_of_range")
    return command["target_soc"], command["freeze"], power


def _candidate(base, pending, inverter, now, minute):
    """Validate complete replay and select only the current first planned command."""
    from car_charging_plan import _ev_charging_priority

    _require(isinstance(pending, dict), "no_pending_candidate")
    report = pending["report"]
    partial = report.get("status") == "partial_candidate"
    _require(report.get("status") in ("candidate", "partial_candidate") and not report.get("counterfactual_controls") and not report.get("execution_blockers"), "candidate_not_accepted")
    if partial:
        from car_charging_policy import partial_metadata_valid, partial_fingerprint

        _require(partial_metadata_valid(base, report), "partial_target_not_authorised")
        _require(pending.get("partial_fingerprint") == partial_fingerprint(report), "partial_evidence_changed")
    else:
        _require(not report.get("partial_target_authorised") and (report.get("planning_ev_target_kwh") in (None, base.car_charging_limit[0])), "partial_target_not_authorised")
    checks = report.get("native_checks", {})
    _require(set(checks) == {"nominal", "pv10"}, "native_checks_missing")
    _require(all(check.get("passed") is True and check.get("checked_minutes") == base.forecast_minutes for check in checks.values()), "native_checks_failed")
    _require(0 <= (now - _stamp(report["generated_at"])).total_seconds() <= RECEIPT_TTL_SECONDS, "candidate_stale_or_future")
    _require(pending.get("fingerprint") == execution_fingerprint(base), "configuration_changed")
    _require(pending["options"]["ev_charging_priority"] == _ev_charging_priority(base), "charging_priority_changed_during_planning")
    from battery_window import selected_window_minutes, commands_respect_windows, observed_window, command_mode, planning_window

    minimum_window = selected_window_minutes(base)
    _require(pending["options"].get("battery_min_window_minutes", 5) == minimum_window, "battery_window_changed_during_planning")
    closed = now >= _stamp(pending["options"]["settings"]["session"]["departure"])
    _require(report.get("session_closed", False) is closed, "session_phase_changed")
    _require(_finite(pending.get("initial_battery_soc_kwh"), base.reserve, base.soc_max), "initial_domestic_soc_missing")
    soc_fell = base.soc_kw < pending["initial_battery_soc_kwh"] - 0.002
    shared_support = bool(report.get("grid_charge_support_w"))
    _require(not soc_fell or shared_support, "domestic_soc_fell_since_planning")
    commands = pending["commands"]
    if shared_support:
        _require(pending.get("command_fingerprint") == command_fingerprint(commands), "candidate_commands_changed")
    _require(isinstance(commands, list) and len(commands) == base.forecast_minutes // PREDICT_STEP, "incomplete_command_horizon")
    for index, command in enumerate(commands):
        _intent(command, inverter, bool(report.get("grid_charge_support_w")))
        _require(not closed or not command["ev_on"], "closed_session_ev_command")
        _require(type(command.get("start")) is int and type(command.get("end")) is int, "invalid_command_time")
        _require(command.get("start") == base.minutes_now + index * PREDICT_STEP and command.get("end") == command["start"] + PREDICT_STEP, "command_timeline_mismatch")
    command = commands[0]
    # Recheck live continuity, not the staging copy. Drift releases the old
    # commitment: the candidate must then satisfy a fresh full minimum window.
    current_mode, current_remaining = planning_window(base, base.now_utc, now, minimum_window)
    _require(
        commands_respect_windows(commands, minimum_window, current_mode, current_remaining),
        "battery_window_candidate_invalid",
    )
    previous_window = observed_window(base, now) if minimum_window > 5 else None
    _require(
        not previous_window or command_mode(command) == previous_window["mode"] or now >= _stamp(previous_window["started_at"]) + timedelta(minutes=minimum_window),
        "battery_window_still_active",
    )
    _require(command["start"] <= minute < command["end"], "current_command_expired")
    if pending["options"]["ev_charging_priority"] == "now" and not closed and pending["options"]["ev_soc"] < base.car_charging_limit[0] - 0.002:
        _require(command["ev_on"], "immediate_start_not_validated")
    if pending["options"]["ev_charging_priority"] == "solar" and command["ev_on"]:
        _require(command["target_soc"] == 0 and not command["freeze"], "solar_requires_demand")
    if soc_fell:
        # The ordinary native loop re-reads SOC after calculating. A lower value
        # cannot be waived with a tolerance: replay the SAME controls from that
        # value and require both complete forecasts again, without a new search.
        from car_charging_plan import _options, _replay, _publish_report

        _require("replay_inputs" in pending, "replay_inputs_missing")
        result, slots, windows = pending["replay_inputs"]
        options = _options(base)
        model = base
        if partial:
            model = copy(base)
            model.car_charging_limit = list(base.car_charging_limit)
            model.car_charging_limit[0] = report["planning_ev_target_kwh"]
        checks = {name: _replay(model, options, result, slots, windows, scenario) for name, scenario in (("nominal", 0), ("pv10", 1))}
        for check in checks.values():
            check.pop("_charge_peak_kwh", None)
        updated = {**deepcopy(base.car_charging_report), "native_checks": checks, "dispatch_soc_revalidated_kwh": base.soc_kw}
        _publish_report(base, updated, False)
        _require(all(check["passed"] and check["checked_minutes"] == base.forecast_minutes for check in checks.values()), "dispatch_replay_failed")
    # Only extend an accepted intent over consecutive identical actions; never
    # authorise a future start or a different power ceiling before dispatch.
    run = deepcopy(command)
    for later in commands[1:]:
        if any(later[key] != command[key] for key in ("target_soc", "freeze", "power_w", "ev_on")):
            break
        run["end"] = later["end"]
    return command, run


def _publish_ev(base, command=None, now=None, expires=None):
    """Publish only a currently authorised EV interval, without switching the relay."""
    from car_charging_publish import car_plan_attributes

    windows = []
    if command and command["ev_on"]:
        start = (now - base.midnight_utc).total_seconds() / 60
        end = (expires - base.midnight_utc).total_seconds() / 60
        if end > start:
            rate = base.rate_import[int(start)]
            energy = base.car_charging_rate[0] * (end - start) / 60
            windows = [{"start": start, "end": end, "kwh": energy, "average": rate, "cost": rate * energy, "octopus": False}]
    base.car_charging_slots = [windows] + [[] for _ in range(PREDBAT_MAX_CARS - 1)]
    base.dashboard_item(
        "binary_sensor." + base.prefix + "_car_charging_slot",
        state="on" if windows else "off",
        attributes={
            **car_plan_attributes(getattr(base, "car_charging_report", None)),
            "planned": windows,
            "source": "predbat",
            "ready_to_activate": False,
            "hardware_confirmed": False,
            "lease_version": 1,
            "valid_from": now.isoformat() if windows else None,
            "valid_until": expires.isoformat() if windows else None,
        },
    )
    base.dashboard_item(
        base.prefix + ".car_charging_start",
        state=now.strftime("%H:%M:%S") if windows else "",
        attributes={"timestamp": now.isoformat() if windows else None, "source": "predbat", "minutes_to": 0 if windows else base.forecast_minutes},
    )


def _report(base, status, enabled, reason=None, attempted=False):
    """Keep model output and observed execution distinct, never claim electrical enforcement."""
    report = deepcopy(getattr(base, "car_charging_report", None) or {})
    report.update(status=status, execution_enabled=enabled, ready_to_activate=False, hardware_confirmed=False, enforced=False, actuator_signals_modified=attempted, execution_reason=reason)
    receipt = getattr(base, "car_charging_execution", None)
    report["execution_observed_at"] = receipt.get("generated_at") if receipt else None
    report["execution_valid_until"] = receipt.get("expires_at") if receipt else None
    base.car_charging_report = report
    from car_charging_publish import publish_car_charging_report

    publish_car_charging_report(base, report)


def _blocked(base, reason, attempted=False):
    """Revoke previous EV authority on every error, including publication failure."""
    base.car_charging_execution = None
    base.inverter_rate_intent = {}
    base.isCharging = base.isExporting = False
    base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    try:
        _publish_ev(base)
        _report(base, "apply_blocked", False, reason, attempted)
    except Exception:
        # If publication itself is down, the existing EV executor must use its
        # independent native-status freshness watchdog; do not invent success.
        base.car_charging_report = {"status": "apply_blocked", "execution_enabled": False, "ready_to_activate": False, "hardware_confirmed": False, "execution_reason": reason, "publication_failed": True}
    try:
        base.record_status("Error: EV apply blocked ({})".format(reason), had_errors=True)
    except Exception:
        pass
    return "EV blocked", reason


def _withdraw_for_household(base, reason, attempted=False):
    """Withdraw EV authority, then require observed relay OFF before the sole native handback."""
    base.car_charging_execution = None
    base.battery_window = None
    base.car_charging_pending = None
    base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    try:
        _publish_ev(base)
        relay = _cached(base, read_car_charging_config(base).get("relay_entity"))
        now = _clock()
        off = relay["state"] == "off" and relay["last_changed"] <= now and relay["last_updated"] <= now
        _report(base, "household_fallback" if off else "household_waiting", False, reason, attempted)
        base.car_charging_report.update(household_executor="native" if off else "waiting_for_ev_off", ev_available=False)
        if off:
            base.inverter_rate_intent = {}
            return None
        return "EV waiting", "EV authority withdrawn; waiting for observed relay OFF"
    except Exception:
        # Missing relay or failed publication is not an OFF observation. Existing
        # battery support must remain until the external executor is observed OFF.
        return _blocked(base, "household_handback_unconfirmed", attempted)


def _ev_handoff_until(base, options, run, commands):
    """Bound overlap at a domestic-only boundary inside an already accepted EV-ON run.

    The ordinary five-minute calculation finishes just after the boundary. Do not
    turn that dispatch latency into an EV stop and ten-minute restart cooldown.
    This does not dispatch a future domestic intent or permit a future EV start.
    """
    if options["control_model"] != "deye_solarman" or not options.get("grid_charge_support_w") or not options.get("confirmed_observation") or not run["ev_on"]:
        return None
    following = next((item for item in commands if item["start"] == run["end"]), None)
    if following is None or not following["ev_on"] or any(following[key] != run[key] for key in ("freeze", "power_w")):
        return None
    return (base.midnight_utc + timedelta(minutes=run["end"], seconds=EV_HANDOFF_SECONDS)).isoformat()


def _receipt_authority_end(base, options, receipt):
    """Keep overlap non-rolling and independently capped, while retaining every live guard."""
    command = receipt["command"]
    end = base.midnight_utc + timedelta(minutes=command["end"])
    handoff = receipt.get("ev_handoff_until")
    if handoff is None:
        return end
    handoff = _stamp(handoff)
    _require(options["control_model"] == "deye_solarman" and options.get("grid_charge_support_w") and options.get("confirmed_observation") and command["ev_on"], "ev_handoff_not_supported")
    _require(0 < (handoff - end).total_seconds() <= EV_HANDOFF_SECONDS, "ev_handoff_out_of_bounds")
    return handoff


def execute_car_charging_plan(base):
    """Execute combined intents or hand back only after revocation and observed EV OFF."""
    if not _requested(base):
        return None
    attempted = False
    try:
        from car_charging_policy import household_reason

        reason = household_reason(base)
        if reason:
            return _withdraw_for_household(base, reason)
        options, inverter, relay, now, minute = _evidence(base, allow_read_only_transition=True)
        read_only_transition = bool(getattr(base, "inverter_needs_reset", False))
        pending = getattr(base, "car_charging_pending", None)
        command, run = _candidate(base, pending, inverter, now, minute)
        if options.get("grid_charge_support_w"):
            _require(abs(options["ev_soc"] - pending["options"]["ev_soc"]) <= 0.002, "dispatch_ev_soc_changed")
        _ev_permitted(base, options, relay, now, command["ev_on"])
        _require(not read_only_transition or relay["state"] == "off", "waiting_for_ev_relay_off")
        if not command["ev_on"] and relay["state"] == "on":
            # Remove EV permission first, then wait for the sole EV executor's
            # actual OFF observation before withdrawing battery support.
            return _blocked(base, "waiting_for_ev_relay_off")
        target, freeze, power = _intent(command, inverter, bool(options.get("grid_charge_support_w")))
        from battery_window import observed_window, record_observed_window

        previous_window = observed_window(base, now)
        base.inverter_rate_intent = {}
        # No call to the adapter directly: this is the same native entry point
        # whose optional DEYE implementation owns all physical service writes.
        attempted = True
        if options["control_model"] == "deye_solarman":
            from inverter_service_control import dispatch_template

            if options.get("grid_charge_support_w"):
                observed = dispatch_template(inverter, target, charge_ceiling=True)
            else:
                observed = dispatch_template(inverter, target)
        else:
            observed = inverter.adjust_charge_immediate(target, freeze=freeze, discharge_power_w=power)
        _require(observed is True, "inverter_command_not_observed")
        # Permission, clocks and protection inputs can change while writes settle.
        options, inverter, relay, now, minute = _evidence(base, allow_read_only_transition=True)
        _require(pending["fingerprint"] == execution_fingerprint(base), "configuration_changed_during_dispatch")
        _require(pending["report"].get("session_closed", False) is options["departure_passed"], "session_phase_changed")
        _require(command["start"] <= minute < command["end"], "command_expired_during_dispatch")
        solar = _ev_permitted(base, options, relay, now, command["ev_on"])
        if read_only_transition:
            # The native read-only switch requests reinitialisation. The shared
            # executor fulfils it by observing ALL canonical rows of this exact
            # paired-safe intent, rather than running the unrelated native reset
            # (which can rewrite standing power/reserve settings). Failures above
            # leave the request pending; other reset reasons remain unsupported.
            _require(base.inverter_needs_reset and base.inverter_needs_reset_force == "set_read_only", "reset_request_changed")
            base.inverter_needs_reset = False
            base.inverter_needs_reset_force = ""
        handoff = _ev_handoff_until(base, options, run, pending["commands"])
        authority_end = _stamp(handoff) if handoff else base.midnight_utc + timedelta(minutes=run["end"])
        expires = min(authority_end, now + timedelta(seconds=RECEIPT_TTL_SECONDS))
        if command["ev_on"]:
            expires = min(expires, _stamp(options["settings"]["session"]["departure"]))
        if solar:
            expires = min(expires, now + timedelta(seconds=SOLAR_RECEIPT_SECONDS), _stamp(solar["observed_at"]) + timedelta(seconds=SOLAR_OBSERVATION_SECONDS))
        base.car_charging_execution = {
            "generated_at": now.isoformat(),
            "plan_generated_at": pending["report"]["generated_at"],
            "expires_at": expires.isoformat(),
            "command": run,
            "ev_handoff_until": handoff,
            "fingerprint": execution_fingerprint(base),
            "control_fingerprint": _control_fingerprint(inverter),
            "observations_match": True,
            "hardware_confirmed": False,
            "connection_error_acknowledged": options.get("connection_error_acknowledged", False),
            "read_only_transition_observed": read_only_transition,
            "solar_observation": solar,
        }
        base.isCharging = target > 0 or freeze
        record_observed_window(base, command, now, previous_window)
        base.isExporting = False
        _publish_ev(base, run, now, expires)
        _report(base, "applied", True, "inverter_observed_not_hardware_confirmed", attempted=True)
        return "Freeze charging" if freeze else "Charging" if target > 0 else "Demand", "EV site; HA observations only"
    except Exception as exc:
        reason = str(exc) if isinstance(exc, CarChargingBlocked) else "execution_validation_failed"
        if read_car_charging_config(base).get("household_fallback") is True:
            return _withdraw_for_household(base, reason, attempted)
        return _blocked(base, reason, attempted)


def publish_car_charging_slot(base, renew=False):
    """Prevent early native EV publication; revalidate a bounded receipt without actuating."""
    if not _requested(base):
        return False
    receipt = getattr(base, "car_charging_execution", None)
    try:
        from car_charging_policy import household_reason

        reason = household_reason(base)
        if reason:
            _withdraw_for_household(base, reason)
            return True
        _require(isinstance(receipt, dict) and receipt.get("observations_match") is True, "no_execution_receipt")
        options, inverter, relay, now, minute = _evidence(base)
        _require(receipt["fingerprint"] == execution_fingerprint(base), "configuration_changed")
        _require(receipt["control_fingerprint"] == _control_fingerprint(inverter), "observed_controls_changed")
        _require(0 <= (now - _stamp(receipt["generated_at"])).total_seconds() < RECEIPT_TTL_SECONDS, "receipt_expired")
        expires = _stamp(receipt["expires_at"])
        _require(now < expires, "command_authority_expired")
        command = receipt["command"]
        _intent(command, inverter, bool(options.get("grid_charge_support_w")))
        authority_end = _receipt_authority_end(base, options, receipt)
        _require(command["start"] <= minute and now < authority_end, "command_authority_expired")
        solar = _ev_permitted(base, options, relay, now, command["ev_on"])
        if solar:
            _require(command["target_soc"] == 0 and not command["freeze"], "solar_requires_demand")
        if renew:
            # Revalidate only the already observed, identical current intent.
            # The immutable plan clock bounds renewal across one delayed calculation;
            # it must never become an indefinitely rolling lease on an old forecast.
            _require(options.get("grid_charge_support_w") and options.get("confirmed_observation"), "lease_renewal_not_supported")
            plan_time = _stamp(receipt["plan_generated_at"])
            _require(0 <= (now - plan_time).total_seconds() < PLAN_REVALIDATION_MAX_AGE_SECONDS, "plan_revalidation_expired")
            expires = min(authority_end, now + timedelta(seconds=RECEIPT_TTL_SECONDS), plan_time + timedelta(seconds=PLAN_REVALIDATION_MAX_AGE_SECONDS))
            if command["ev_on"]:
                expires = min(expires, _stamp(options["settings"]["session"]["departure"]))
            if solar:
                expires = min(expires, now + timedelta(seconds=SOLAR_RECEIPT_SECONDS), _stamp(solar["observed_at"]) + timedelta(seconds=SOLAR_OBSERVATION_SECONDS))
            receipt = {**receipt, "generated_at": now.isoformat(), "expires_at": expires.isoformat()}
            if solar:
                receipt["solar_observation"] = solar
            base.car_charging_execution = receipt
        _publish_ev(base, command, now, expires)
        if renew:
            _report(base, "applied", True, "inverter_observed_not_hardware_confirmed", attempted=True)
    except Exception as exc:
        # Fetch-time absence of a receipt is normal at startup, not proof the
        # native planner is broken. It still revokes any old EV-slot signal.
        base.car_charging_execution = None
        base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
        try:
            _publish_ev(base)
            if receipt:
                _report(base, "apply_blocked", False, str(exc) if isinstance(exc, CarChargingBlocked) else "execution_validation_failed")
        except Exception:
            _blocked(base, str(exc) if isinstance(exc, CarChargingBlocked) else "publication_failed")
    return True


def refresh_car_charging_lease(base):
    """Use the existing cache-only timer to sustain an accepted intent, never to start a new one."""
    settings = read_car_charging_config(base)
    from car_charging_recurring import recurring_enabled

    if _requested(base) and settings.get("grid_charge_support") and (settings.get("confirmed_session") or recurring_enabled(settings)) and getattr(base, "car_charging_execution", None):
        publish_car_charging_slot(base, renew=True)
