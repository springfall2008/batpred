"""Explicit joint execution through the native inverter and the sole external EV executor.

Off and shadow are strict no-ops. HA observations are never hardware confirmation.
This module neither sends EV switch services nor changes standing inverter limits.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math

from const import PREDBAT_MAX_CARS, PREDICT_STEP, MINUTE_WATT

RECEIPT_TTL_SECONDS = 300
OBSERVATION_MAX_AGE_SECONDS = 180


class JointBlocked(ValueError):
    """Carry a public reason code, never raw configuration or transport details."""


def _require(condition, reason):
    """Fail closed on a violated execution contract."""
    if not condition:
        raise JointBlocked(reason)


def _clock():
    """Use the same real observation clock as the native input adapter."""
    from joint_site_native import _observation_clock, _aware

    return _aware(_observation_clock(), "execution_clock")


def _stamp(value):
    """Read a strictly offset-aware timestamp through the native parser."""
    from joint_site_native import _parse_entity_timestamp, _aware

    return _aware(value, "execution_timestamp") if isinstance(value, datetime) else _parse_entity_timestamp(value, "execution_timestamp")


def _finite(value, minimum=0, maximum=None):
    """Accept finite native numeric values without coercing booleans or strings."""
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)


def _requested(base):
    """Recognise only the explicit apply setting without touching runtime state."""
    settings = base.args.get("joint_site")
    return isinstance(settings, dict) and settings.get("mode") == "apply"


def native_plan_hook_allowed(base):
    """Do not broadcast independent legacy controls as an executed joint plan."""
    return not _requested(base)


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
    payload["settings"] = base.args.get("joint_site")
    payload["deye_local"] = base.args.get("deye_local")
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cached(base, entity):
    """Require an explicitly configured cached entity; never refresh hardware."""
    from joint_site_native import _read_entity_state

    _require(isinstance(entity, str) and bool(entity), "entity_mapping_required")
    result = _read_entity_state(base, entity)
    _require(result is not None, "entity_observation_unavailable")
    return result


def _control_fingerprint(inverter):
    """Detect observed control drift without claiming read-back proves hardware state."""
    snapshot = inverter.deye_local.snapshot()
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _evidence(base):
    """Revalidate actual capabilities, overrides and independent EV evidence before use."""
    from joint_site_native import _options, _collect_blockers

    settings = base.args["joint_site"]
    _require(settings.get("apply_verified") is True, "apply_commissioning_required")
    _require(settings.get("ev_executor_lease_verified") is True, "ev_executor_lease_required")
    _require(not getattr(base, "octopus_intelligent_charging", False), "external_ev_scheduler_unsupported")
    _require(not base.get_arg("octopus_intelligent_slot", None, indirect=False), "external_ev_scheduler_unsupported")
    hooks = getattr(getattr(base, "plugin_system", None), "hooks", {})
    _require(not hooks.get("on_plan_executed"), "external_plan_consumer_unsupported")
    stamps = [_stamp(_cached(base, settings.get(name))["state"]) for name in ("soc_observed_at_entity", "connection_observed_at_entity")]
    now = _clock()
    _require(all(0 <= (now - stamp).total_seconds() <= OBSERVATION_MAX_AGE_SECONDS for stamp in stamps), "physical_observation_stale_or_future")
    options = _options(base)
    _require(options is not None and options["mode"] == "apply" and options["control_model"] == "deye_tou", "real_deye_controls_required")
    _require(not _collect_blockers(base, options), "native_inputs_or_overrides_blocked")
    _require(not options["departure_passed"], "departure_passed")
    _require(not base.set_read_only and not getattr(base, "set_read_only_axle", False), "read_only")
    _require(not any(getattr(base, name, False) for name in ("inverter_needs_reset", "had_errors", "fatal_error")), "native_state_not_ready")
    _require(not base.get_arg("template", False), "template_mode")
    inverters = getattr(base, "inverters", None)
    _require(isinstance(inverters, list) and len(inverters) == 1, "single_inverter_required")
    inverter = inverters[0]
    _require(getattr(inverter, "inverter_type", None) == "DEYE", "local_deye_required")
    adapter = getattr(inverter, "deye_local", None)
    _require(adapter is not None, "local_deye_adapter_missing")
    _require(adapter.settings.get("control") is True and adapter.settings.get("hold_verified") is True, "deye_commissioning_required")
    _require(getattr(inverter, "inv_support_charge_freeze", False) is True and not getattr(inverter, "in_calibration", True), "inverter_capability_or_calibration")
    _require(not getattr(base, "manual_soc_max_keep", None), "manual_ceiling_unsupported")
    _require(base.export_limit == 0 and base.set_discharge_during_charge, "deye_dispatch_model_mismatch")
    _require(options["import_limit_w"] == getattr(base, "import_limit", options["import_limit_w"]), "import_limit_mismatch")
    _require(options["ev_soc_source"] == "entity_cache", "measured_ev_soc_required")
    entities = options["entity_config"]
    relay = _cached(base, entities["relay_entity"])
    permission = _cached(base, entities["permission_entity"])
    connection = _cached(base, entities["connection_entity"])
    allowed = entities["connection_entity_states"]
    _require(isinstance(allowed, (list, tuple)) and bool(allowed) and connection["state"] in allowed, "connection_not_confirmed")
    _require(permission["state"] == "on", "permission_not_granted")
    _require(relay["state"] in ("on", "off"), "relay_state_invalid")
    now = _clock()
    _require(all(payload["last_changed"] <= now and payload["last_updated"] <= now for payload in (relay, permission, connection)), "entity_timestamp_future")
    _require(_finite(base.soc_max, 0.001) and _finite(base.soc_kw, base.reserve, base.soc_max), "domestic_soc_invalid")
    minute = (now - base.midnight_utc).total_seconds() / 60
    floor = max(base.reserve, base.all_active_keep.get(int(minute), 0) * base.soc_max / 100)
    _require(base.soc_kw >= floor - 0.002, "domestic_manual_floor")
    adapter.snapshot()
    return options, inverter, relay, now, minute


def _ev_permitted(base, options, relay, now, on):
    """Check hysteresis and exact elapsed OFF seconds independently of battery mode."""
    if not on:
        return
    _require(base.soc_kw > options["pause"] * base.soc_max / 100, "ev_pause_threshold")
    _require(options["ev_soc"] < base.car_charging_limit[0], "ev_target_already_reached")
    session = options["settings"]["session"]
    _require(_stamp(session["available_from"]) <= now < _stamp(session["departure"]), "ev_outside_session")
    if relay["state"] == "off":
        _require(base.soc_kw >= options["restart"] * base.soc_max / 100, "ev_restart_threshold")
        _require((now - relay["last_changed"]).total_seconds() >= options["off_minutes"] * 60, "ev_cooldown")


def _intent(command, inverter):
    """Validate exact bounded commands; do not clamp a rejected plan into another plan."""
    _require(isinstance(command, dict), "invalid_command")
    _require(type(command.get("target_soc")) is int and command["target_soc"] in (0, 100), "invalid_target")
    _require(type(command.get("freeze")) is bool and type(command.get("ev_on")) is bool, "invalid_command_boolean")
    _require(not command["freeze"] or command["target_soc"] == 0, "invalid_hold_target")
    cap = min(inverter.deye_local.settings["max_discharge_power_w"], inverter.battery_rate_max_discharge * MINUTE_WATT)
    power = command.get("power_w")
    _require(_finite(cap, 1), "invalid_discharge_cap")
    _require(power is None or (type(power) is int and _finite(power, 0, cap)), "discharge_power_out_of_range")
    return command["target_soc"], command["freeze"], power


def _candidate(base, pending, inverter, now, minute):
    """Validate complete replay and select only the current first planned command."""
    _require(isinstance(pending, dict), "no_pending_candidate")
    report = pending["report"]
    _require(report.get("status") == "candidate" and not report.get("counterfactual_controls") and not report.get("execution_blockers"), "candidate_not_accepted")
    checks = report.get("native_checks", {})
    _require(set(checks) == {"nominal", "pv10"}, "native_checks_missing")
    _require(all(check.get("passed") is True and check.get("checked_minutes") == base.forecast_minutes for check in checks.values()), "native_checks_failed")
    _require(0 <= (now - _stamp(report["generated_at"])).total_seconds() <= RECEIPT_TTL_SECONDS, "candidate_stale_or_future")
    _require(pending.get("fingerprint") == execution_fingerprint(base), "configuration_changed")
    _require(_finite(pending.get("initial_battery_soc_kwh"), base.reserve, base.soc_max), "initial_domestic_soc_missing")
    _require(base.soc_kw >= pending["initial_battery_soc_kwh"] - 0.002, "domestic_soc_fell_since_planning")
    commands = pending["commands"]
    _require(isinstance(commands, list) and len(commands) == base.forecast_minutes // PREDICT_STEP, "incomplete_command_horizon")
    for index, command in enumerate(commands):
        _intent(command, inverter)
        _require(type(command.get("start")) is int and type(command.get("end")) is int, "invalid_command_time")
        _require(command.get("start") == base.minutes_now + index * PREDICT_STEP and command.get("end") == command["start"] + PREDICT_STEP, "command_timeline_mismatch")
    command = commands[0]
    _require(command["start"] <= minute < command["end"], "current_command_expired")
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
            "planned": windows,
            "source": "joint_site_apply",
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
        attributes={"timestamp": now.isoformat() if windows else None, "source": "joint_site_apply", "minutes_to": 0 if windows else base.forecast_minutes},
    )


def _report(base, status, enabled, reason=None, attempted=False):
    """Keep model output and observed execution distinct, never claim electrical enforcement."""
    report = deepcopy(getattr(base, "joint_site_report", None) or {})
    report.update(status=status, execution_enabled=enabled, ready_to_activate=False, hardware_confirmed=False, enforced=False, actuator_signals_modified=attempted, execution_reason=reason)
    receipt = getattr(base, "joint_site_execution", None)
    report["execution_observed_at"] = receipt.get("generated_at") if receipt else None
    report["execution_valid_until"] = receipt.get("expires_at") if receipt else None
    base.joint_site_report = report
    base.dashboard_item(base.prefix + ".joint_site_plan", state=status, attributes=report)


def _blocked(base, reason, attempted=False):
    """Revoke previous EV authority on every error, including publication failure."""
    base.joint_site_execution = None
    base.inverter_rate_intent = {}
    base.isCharging = base.isExporting = False
    base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    try:
        _publish_ev(base)
        _report(base, "apply_blocked", False, reason, attempted)
    except Exception:
        # If publication itself is down, the existing EV executor must use its
        # independent native-status freshness watchdog; do not invent success.
        base.joint_site_report = {"status": "apply_blocked", "execution_enabled": False, "ready_to_activate": False, "hardware_confirmed": False, "execution_reason": reason, "publication_failed": True}
    try:
        base.record_status("Error: Joint apply blocked ({})".format(reason), had_errors=True)
    except Exception:
        pass
    return "Joint blocked", reason


def execute_joint_site(base):
    """Own explicit apply cycles inside the real native executor; never fall through on failure."""
    if not _requested(base):
        return None
    attempted = False
    try:
        options, inverter, relay, now, minute = _evidence(base)
        pending = getattr(base, "joint_site_pending", None)
        command, run = _candidate(base, pending, inverter, now, minute)
        _ev_permitted(base, options, relay, now, command["ev_on"])
        if not command["ev_on"] and relay["state"] == "on":
            # Remove EV permission first, then wait for the sole EV executor's
            # actual OFF observation before withdrawing battery support.
            return _blocked(base, "waiting_for_ev_relay_off")
        target, freeze, power = _intent(command, inverter)
        base.inverter_rate_intent = {}
        # No call to the adapter directly: this is the same native entry point
        # whose optional DEYE implementation owns all physical service writes.
        attempted = True
        observed = inverter.adjust_charge_immediate(target, freeze=freeze, discharge_power_w=power)
        _require(observed is True, "inverter_command_not_observed")
        # Permission, clocks and protection inputs can change while writes settle.
        options, inverter, relay, now, minute = _evidence(base)
        _require(pending["fingerprint"] == execution_fingerprint(base), "configuration_changed_during_dispatch")
        _require(command["start"] <= minute < command["end"], "command_expired_during_dispatch")
        _ev_permitted(base, options, relay, now, command["ev_on"])
        expires = min(base.midnight_utc + timedelta(minutes=run["end"]), now + timedelta(seconds=RECEIPT_TTL_SECONDS), _stamp(options["settings"]["session"]["departure"]))
        base.joint_site_execution = {
            "generated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "command": run,
            "fingerprint": execution_fingerprint(base),
            "control_fingerprint": _control_fingerprint(inverter),
            "observations_match": True,
            "hardware_confirmed": False,
        }
        base.isCharging = target > 0 or freeze
        base.isExporting = False
        _publish_ev(base, run, now, expires)
        _report(base, "applied", True, "inverter_observed_not_hardware_confirmed", attempted=True)
        return "Freeze charging" if freeze else "Charging" if target > 0 else "Demand", "Joint site; HA observations only"
    except Exception as exc:
        return _blocked(base, str(exc) if isinstance(exc, JointBlocked) else "execution_validation_failed", attempted)


def publish_joint_ev(base):
    """Prevent early native EV publication; revalidate a bounded receipt without actuating."""
    if not _requested(base):
        return False
    try:
        receipt = getattr(base, "joint_site_execution", None)
        _require(isinstance(receipt, dict) and receipt.get("observations_match") is True, "no_execution_receipt")
        options, inverter, relay, now, minute = _evidence(base)
        _require(receipt["fingerprint"] == execution_fingerprint(base), "configuration_changed")
        _require(receipt["control_fingerprint"] == _control_fingerprint(inverter), "observed_controls_changed")
        _require(0 <= (now - _stamp(receipt["generated_at"])).total_seconds() < RECEIPT_TTL_SECONDS, "receipt_expired")
        expires = _stamp(receipt["expires_at"])
        _require(now < expires, "command_authority_expired")
        command = receipt["command"]
        _intent(command, inverter)
        _require(command["start"] <= minute < command["end"], "command_authority_expired")
        _ev_permitted(base, options, relay, now, command["ev_on"])
        _publish_ev(base, command, now, expires)
    except Exception as exc:
        # Fetch-time absence of a receipt is normal at startup, not proof the
        # native planner is broken. It still revokes any old EV-slot signal.
        base.joint_site_execution = None
        base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
        try:
            _publish_ev(base)
            if receipt:
                _report(base, "apply_blocked", False, str(exc) if isinstance(exc, JointBlocked) else "execution_validation_failed")
        except Exception:
            _blocked(base, str(exc) if isinstance(exc, JointBlocked) else "publication_failed")
    return True
