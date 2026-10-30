# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Bounded adapter for the shared Deye/Solarman verified-service template.

Three pure helpers - no HA service writes of the adapter's own:

* :func:`validate_template` resolves the canonical six-row charge and
  demand sequences through :meth:`Inverter.prepare_service_sequence`
  and compares the resulting entities, services and per-phase values
  against the canonical mapping. Returns ``True`` on valid; raises
  :class:`ServiceSequenceError` (a :class:`ValueError` subclass) on
  any mismatch so a malicious mapping that targets another inverter
  cannot pass preflight only because the cached terminal state already
  matches.
* :func:`snapshot_template` reads every canonical row from the HA
  cache via :meth:`Inverter.get_state_wrapper`, applies
  :func:`math.ceil` to the Predbat reserve and returns the resolved
  configuration mapping plus the current control states.
* :func:`dispatch_template` calls
  :meth:`Inverter.adjust_charge_immediate` exactly once with
  ``(target_soc, freeze=False)`` and re-observes the final six-row
  state through :meth:`Inverter.service_sequence_state_matches`.
  Returns the literal ``bool`` verdict: ``True`` only when every
  canonical row reaches the expected terminal state and
  ``base.had_errors`` stays ``False`` across the dispatch.

Two default intents - ``demand(0, False)`` and ``charge(100, False)``.
Explicit ``charge_ceiling=True`` additionally accepts replay-validated integer
targets strictly above reserve. Freeze and discharge-power changes stay unsupported.

The parent (``car_charging_execute``) owns commissioning, SOC/connection
freshness (≤180 s), the 300 s lease TTL and the EV external executor.
Verified dedup is sequence-based, not hash-based; do not migrate.
"""

import math
import re

from const import MINUTE_WATT
import inverter as native_inverter

# The verified-sequence core is optional until the service adapter is installed.
# A missing core must not break imports of the independent combined planner.
ServiceSequenceError = getattr(native_inverter, "ServiceSequenceError", ValueError)


PROFILE_ID = "deye_solarman_shared"
EXPECTED_ROW_COUNT = 6
DISABLED_OPTION = "Disabled"
ENABLE_ON_CHARGE_OPTION = "Grid"


def _expected_entities(prefix):
    """Return only the canonical six source and SOC entities for one inverter prefix."""
    sources = ["select.{}_program_{}_charging".format(prefix, row) for row in range(1, EXPECTED_ROW_COUNT + 1)]
    targets = ["number.{}_program_{}_soc".format(prefix, row) for row in range(1, EXPECTED_ROW_COUNT + 1)]
    return sources, targets


def _is_real_inverter(inverter):
    """``True`` only for an actual :class:`Inverter` instance - never a label string."""
    from inverter import Inverter as _Inverter

    return isinstance(inverter, _Inverter)


def _resolve_canonical_steps(inverter):
    """Run the native preflight at the canonical dispatch values and return the resolved steps.

    ``charge_start_service`` is resolved with ``target_soc=100`` so any
    out-of-bounds placeholder substitution fails closed BEFORE the
    adapter ever authorises a write. ``charge_stop_service`` is resolved
    with the Predbat ``reserve_percent`` rounded UP through
    :func:`math.ceil` exactly the way the canonical ``{reserve_soc}``
    substitution is computed at dispatch time.
    """
    base = inverter.base
    return (
        inverter.prepare_service_sequence(base.args["charge_start_service"], {"target_soc": 100}, {}),
        inverter.prepare_service_sequence(base.args["charge_stop_service"], {"target_soc": 100}, {}),
    )


def validate_template(inverter):
    """Return ``True`` iff the inverter matches the canonical shared template."""
    if not _is_real_inverter(inverter):
        raise ServiceSequenceError("not_an_inverter_instance")
    if not all(callable(getattr(inverter, name, None)) for name in ("prepare_service_sequence", "call_service_sequence", "service_sequence_state_matches")):
        raise ServiceSequenceError("verified_template_core_required")
    base = getattr(inverter, "base", None)
    args = getattr(base, "args", None) if base is not None else None
    if not isinstance(args, dict):
        raise ServiceSequenceError("base_args_missing")
    if not isinstance(inverter.inverter_type, str) or inverter.inverter_type != "DeyeSolarman":
        raise ServiceSequenceError("inverter_type_must_be_DeyeSolarman")
    prefix = args.get("deye_entity_prefix")
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9_]+", prefix):
        raise ServiceSequenceError("deye_entity_prefix_invalid")
    if args.get("discharge_stop_service") != []:
        raise ServiceSequenceError("discharge_stop_service_must_be_empty_list")
    if args.get("charge_freeze_service") not in (None, "", []):
        raise ServiceSequenceError("charge_freeze_service_unsupported")

    capability_invariants = (
        ("inv_support_charge_freeze", False),
        ("inv_support_discharge_freeze", False),
        ("inv_has_timed_pause", False),
        ("inv_charge_control_immediate", False),
        ("inv_has_target_soc", False),
        ("inv_has_reserve_soc", False),
        ("inv_has_solis_energy_control", False),
        ("inv_has_charge_enable_time", False),
        ("inv_has_discharge_enable_time", False),
        ("inv_charge_discharge_with_rate", False),
        ("inv_charge_time_format", "S"),
        ("inv_output_charge_control", "none"),
    )
    for attr, expected in capability_invariants:
        if not hasattr(inverter, attr) or getattr(inverter, attr) != expected:
            raise ServiceSequenceError(attr + "_mismatch")

    for name in ("battery_rate_max_charge", "battery_rate_max_charge_dc", "battery_rate_max_discharge", "inverter_limit", "export_limit"):
        value = getattr(base, name, None)
        if type(value) not in (int, float) or not math.isfinite(value) or (value < 0 if name == "export_limit" else value <= 0):
            raise ServiceSequenceError(name + "_must_be_positive")
        native_value = getattr(inverter, name, None)
        if type(native_value) not in (int, float) or not math.isfinite(native_value) or not math.isclose(native_value, value, rel_tol=0, abs_tol=1e-10):
            raise ServiceSequenceError(name + "_native_model_mismatch")
    for name in ("battery_rate_max_scaling", "battery_rate_max_scaling_discharge"):
        if type(getattr(base, name, None)) not in (int, float) or getattr(base, name) != 1:
            raise ServiceSequenceError("template_rate_scaling_unsupported")
    if type(inverter.reserve_percent) not in (int, float) or not math.isfinite(inverter.reserve_percent) or not 0 <= inverter.reserve_percent <= 100:
        raise ServiceSequenceError("reserve_percent_invalid")
    capacity = getattr(base, "soc_max", None)
    reserve = getattr(base, "reserve", None)
    if type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0 or not math.isclose(capacity, inverter.soc_max, rel_tol=0, abs_tol=0.001):
        raise ServiceSequenceError("template_capacity_model_mismatch")
    if type(reserve) not in (int, float) or not math.isfinite(reserve) or not math.isclose(reserve, inverter.soc_max * inverter.reserve_percent / 100, rel_tol=0, abs_tol=0.001):
        raise ServiceSequenceError("template_reserve_model_mismatch")

    charge_start = args.get("charge_start_service")
    charge_stop = args.get("charge_stop_service")
    if not (isinstance(charge_start, list) and len(charge_start) == 3):
        raise ServiceSequenceError("charge_start_service_length_must_be_three")
    if not (isinstance(charge_stop, list) and len(charge_stop) == 2):
        raise ServiceSequenceError("charge_stop_service_length_must_be_two")
    for step in (*charge_start, *charge_stop):
        if not (isinstance(step, dict) and step.get("verify") is True):
            raise ServiceSequenceError("every_step_must_have_verify_true")
        if "device_id" in step or step.get("data") or step.get("payload"):
            raise ServiceSequenceError("steps_must_not_contain_device_id_or_data_payload")
    if not (isinstance(charge_start[1].get("value"), str) and charge_start[1]["value"].strip() == "{target_soc}"):
        raise ServiceSequenceError("charge_start_service_target_step_must_carry_target_soc_placeholder")
    if charge_start[0].get("option") != DISABLED_OPTION or charge_start[2].get("option") != ENABLE_ON_CHARGE_OPTION:
        raise ServiceSequenceError("charge_start_service_source_options_must_be_Disabled_then_Grid")
    if charge_stop[0].get("option") != DISABLED_OPTION:
        raise ServiceSequenceError("charge_stop_service_disable_option_must_be_Disabled")
    if not (isinstance(charge_stop[1].get("value"), str) and charge_stop[1]["value"].strip() == "{reserve_soc}"):
        raise ServiceSequenceError("charge_stop_service_target_step_must_carry_reserve_soc_placeholder")

    charge_steps, demand_steps = _resolve_canonical_steps(inverter)
    sources, targets = _expected_entities(prefix)

    if len(charge_steps) != 3:
        raise ServiceSequenceError("charge_steps_resolved_count_mismatch")
    if len(demand_steps) != 2:
        raise ServiceSequenceError("demand_steps_resolved_count_mismatch")
    if charge_steps[0][0] != "select/select_option" or charge_steps[2][0] != "select/select_option":
        raise ServiceSequenceError("charge_phase_service_must_be_select_option")
    if charge_steps[1][0] != "number/set_value":
        raise ServiceSequenceError("charge_target_phase_service_must_be_number_set_value")
    disable_sources = list(charge_steps[0][2])
    target_phase_entities = list(charge_steps[1][2])
    enable_sources = list(charge_steps[2][2])
    if disable_sources != sources or enable_sources != sources:
        raise ServiceSequenceError("charge_phase_source_rows_mismatch")
    if target_phase_entities != targets:
        raise ServiceSequenceError("charge_target_phase_rows_mismatch")
    if charge_steps[1][3] != 100.0:
        raise ServiceSequenceError("charge_target_phase_resolved_value_mismatch:" + str(charge_steps[1][3]))
    if list(demand_steps[0][2]) != sources or demand_steps[0][0] != "select/select_option":
        raise ServiceSequenceError("demand_disable_phase_rows_or_service_mismatch")
    if list(demand_steps[1][2]) != targets or demand_steps[1][0] != "number/set_value":
        raise ServiceSequenceError("demand_target_phase_rows_or_service_mismatch")
    expected_demand_target = float(math.ceil(inverter.reserve_percent))
    if demand_steps[1][3] != expected_demand_target:
        raise ServiceSequenceError("demand_target_phase_resolved_value_mismatch:" + str(demand_steps[1][3]))

    return True


def snapshot_template(inverter):
    """Cache-only mirror of the verified six rows plus the resolved configuration mapping."""
    validate_template(inverter)
    base = inverter.base
    prefix = base.args.get("deye_entity_prefix")
    sources, targets = _expected_entities(prefix)
    sources_state, targets_state = [], []
    for entity in sources:
        payload = base.get_state_wrapper(entity_id=entity, raw=True, refresh=False)
        if not isinstance(payload, dict) or payload.get("state") in (None, "unknown", "unavailable", "none", ""):
            raise ServiceSequenceError("cache_observation_unavailable:" + entity)
        sources_state.append(payload.get("state"))
    for entity in targets:
        payload = base.get_state_wrapper(entity_id=entity, raw=True, refresh=False)
        if not isinstance(payload, dict) or payload.get("state") in (None, "unknown", "unavailable", "none", ""):
            raise ServiceSequenceError("cache_observation_unavailable:" + entity)
        state = payload.get("state")
        if not isinstance(state, (int, float)):
            try:
                float(str(state))
            except (TypeError, ValueError):
                raise ServiceSequenceError("cache_target_state_not_numeric:" + entity)
        targets_state.append(state)
    reserve_percent = inverter.reserve_percent
    if not (isinstance(reserve_percent, (int, float)) and math.isfinite(reserve_percent)):
        raise ServiceSequenceError("reserve_percent_invalid")
    return {
        "profile_id": PROFILE_ID,
        "prefix": prefix,
        "source_entities": sources,
        "target_entities": targets,
        "current_sources": sources_state,
        "current_targets": targets_state,
        "demand_reserve_soc": int(math.ceil(float(reserve_percent))),
        "charge_target_soc": 100.0,
        "configuration": {
            "inverter_type": inverter.inverter_type,
            "charge_start_service": base.args["charge_start_service"],
            "charge_stop_service": base.args["charge_stop_service"],
            "discharge_stop_service": base.args["discharge_stop_service"],
            "battery_rate_max_charge_w": base.battery_rate_max_charge * MINUTE_WATT,
            "battery_rate_max_discharge_w": base.battery_rate_max_discharge * MINUTE_WATT,
            "reserve_percent": float(reserve_percent),
        },
    }


def dispatch_template(inverter, target_soc, freeze=False, discharge_power_w=None, charge_ceiling=False):
    """Drive the verified shared template through :meth:`Inverter.adjust_charge_immediate`.

    Bounded intermediate charge targets require explicit ``charge_ceiling=True``;
    defaults keep the demand/full-charge contract. Invalid intents fail before
    the inverter is called. Native dispatch calls
    ``adjust_charge_immediate(target_soc, freeze=False)`` exactly once
    with no signature fallback.

    Returns the literal ``bool`` verdict: ``True`` iff the canonical
    preflight passes, ``base.had_errors`` stays ``False`` across the
    dispatch and every canonical row reaches the expected terminal
    state; ``False`` on any partial observation, had_errors divergence
    or native exception.
    """
    if freeze is not False:
        raise ValueError("freeze_unsupported")
    if discharge_power_w is not None:
        raise ValueError("discharge_power_w_unsupported")
    bounded = charge_ceiling is True and type(target_soc) is int and math.ceil(inverter.reserve_percent) < target_soc <= 100
    if type(target_soc) is not int or (target_soc not in (0, 100) and not bounded):
        raise ValueError("unsupported_target_soc")
    try:
        validate_template(inverter)
    except ServiceSequenceError:
        return False

    base = inverter.base
    if getattr(base, "had_errors", False):
        return False

    try:
        inverter.adjust_charge_immediate(target_soc, freeze=False)
    except Exception:
        return False
    if getattr(base, "had_errors", False):
        return False

    sources, targets = _expected_entities(base.args.get("deye_entity_prefix"))
    if target_soc > 0:
        expected_source, expected_target = ENABLE_ON_CHARGE_OPTION, float(target_soc)
    else:
        reserve_percent = inverter.reserve_percent
        if not (isinstance(reserve_percent, (int, float)) and math.isfinite(reserve_percent)):
            return False
        expected_source = DISABLED_OPTION
        expected_target = float(math.ceil(float(reserve_percent)))
    for entity in sources:
        observed = base.get_state_wrapper(entity_id=entity, raw=False, refresh=False)
        if not inverter.service_sequence_state_matches(observed, expected_source):
            return False
    for entity in targets:
        observed = base.get_state_wrapper(entity_id=entity, raw=False, refresh=False)
        if not inverter.service_sequence_state_matches(observed, expected_target):
            return False
    return True
