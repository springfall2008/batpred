"""Opt-in native PredHeat joint preview; normal battery execution keeps baseline heat."""

import copy
import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite

from const import PREDICT_STEP, TIME_FORMAT
from predheat_joint import JointHeatingRequest, plan_joint_heating
from predheat_model import simulate_heating


@dataclass(frozen=True)
class _HeatingSnapshot:
    """Qualified immutable input values belonging to one component and planning origin."""

    source: object
    updated_at: datetime | None
    inputs: object
    comfort_targets: tuple
    had_errors: bool


def _capture_heating_snapshot(predheat, plan_at=None):
    """Capture inputs while holding the same lock as every real component refresh."""
    with getattr(predheat, "prediction_lock", nullcontext()):
        if plan_at is not None:
            if not predheat.is_enabled():
                raise ValueError("PredHeat must be configured and enabled for joint planning")
            predheat.update_pred(scheduled=False, plan_at=plan_at)
        return _HeatingSnapshot(predheat, getattr(predheat, "model_input_updated_at", None), predheat.model_input, tuple(predheat.comfort_target_temperature_c), predheat.had_errors)


def _require_live_heating_cycle(base):
    """Reject historical refreshes using the unrounded cycle origin, without clock skew."""
    cycle_origin = getattr(base, "now_utc_real", base.now_utc)
    if cycle_origin.tzinfo is None or abs((datetime.now(timezone.utc) - cycle_origin).total_seconds()) > 1800:
        raise ValueError("Live PredHeat refresh requires a cycle started within 30 minutes; replay callers must supply explicit HeatingInput")


def _joint_heating_request(inputs, native_targets, config, baseline):
    """Keep native warm-up/setback feasible while applying the preview comfort policy."""
    margin = config.get("joint_comfort_margin", 0.5)
    overheat = config.get("joint_overheat_margin", 1.5)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0 for value in (margin, overheat)):
        raise ValueError("Joint comfort margins must be finite nonnegative Celsius values")
    # A setback does not require instantaneous cooling, and a raised setpoint
    # does not make the house instantly warm. During those transitions the
    # native interval's temperature range remains feasible. A common ceiling
    # also permits useful preheating before a setpoint rise.
    ceiling = max(native_targets) + overheat
    minimum, maximum = [], []
    before = inputs.initial_state.room_temperature_c
    for target, interval in zip(native_targets, baseline.intervals):
        after = interval.room_temperature_c
        minimum.append(min(target - margin, before, after))
        maximum.append(max(ceiling, before, after))
        before = after
    return JointHeatingRequest(
        inputs,
        tuple(minimum),
        tuple(maximum),
        config.get("joint_terminal_temperature", inputs.initial_state.room_temperature_c),
        True,
        max_evaluations=config.get("joint_max_evaluations", 32),
        max_seconds=config.get("joint_max_seconds", 30),
        terminal_water_minimum_c=config.get("joint_terminal_water_temperature", inputs.initial_state.water_temperature_c),
    )


def _preview_attributes(summary):
    """Bound chart attributes without changing the full trajectory or planner precision."""
    attributes = dict(summary)
    points = list(summary.get("results", {}).items())
    if not points:
        return attributes
    stride = 1
    while True:
        selected = points[::stride]
        if selected[-1][0] != points[-1][0]:
            selected.append(points[-1])
        attributes["results"] = {stamp: {key: round(value, 3) for key, value in values.items()} for stamp, values in selected}
        attributes["forecast_interval_minutes"] = PREDICT_STEP
        attributes["plot_interval_minutes"] = PREDICT_STEP * stride
        if len(json.dumps(attributes, allow_nan=False).encode()) <= 12000:
            return attributes
        if len(selected) <= 2:
            raise ValueError("PredHeat preview metadata exceeds its publication budget")
        stride *= 2


def clear_joint_heating_shadow(base, reason):
    """Invalidate a published preview without changing a live battery or heating plan."""
    summary = {"state": "disabled" if reason == "disabled" else "unavailable", "reason": reason, "shadow_only": True, "generated_at": base.now_utc.isoformat()}
    base.joint_heating_plan = summary
    prefix = getattr(getattr(base, "predheat", None), "prefix", "predheat")
    base.set_state_wrapper(prefix + ".joint_plan", state=summary["state"], attributes=summary)


def prepare_joint_heating_shadow(base, loads, pv_steps, evaluate=True, refresh_inputs=None):
    """Prepare a preview, invalidating published results if live input preparation fails.

    ``refresh_inputs`` defaults to ``evaluate``. Native live cycles explicitly
    refresh even when reusing battery windows; comparison calls do neither.
    """
    try:
        return _prepare_joint_heating_shadow(base, loads, pv_steps, evaluate, evaluate if refresh_inputs is None else refresh_inputs)
    except Exception:
        if refresh_inputs or (refresh_inputs is None and evaluate):
            base.joint_heating_snapshot = None
        if evaluate or refresh_inputs:
            clear_joint_heating_shadow(base, "input_or_evaluation_error")
        raise


def _prepare_joint_heating_shadow(base, loads, pv_steps, evaluate=True, refresh_inputs=True):
    """Evaluate a joint preview and return the native baseline household load scenarios.

    This boundary is deliberately explicit: the configured household forecast must
    exclude heating. Normal battery operation receives the current thermostat's
    PredHeat baseline, while the joint alternative is published but not executed.
    Failure raises before any incomplete baseline can be used for battery control.
    """
    config = base.args.get("predheat", {})
    if not config.get("joint_planning", False):
        return loads
    if config.get("joint_load_is_non_heating") is not True:
        raise ValueError("PredHeat joint_planning requires joint_load_is_non_heating: true and a qualified non-heating forecast")
    predheat = getattr(base, "predheat", None)
    if predheat is None:
        raise ValueError("PredHeat must be configured and enabled for joint planning")
    # Live cycles refresh even when reusing the battery plan. Never move an old
    # relative forecast onto a new planning instant. Refresh once, not per trial.
    # The pure API is independent of this adapter for headless and replay hosts.
    if refresh_inputs:
        # now_utc_real is the unrounded cycle origin, without configured clock
        # skew (or the explicit plan_once instant). Allow normal fetch/search
        # delays within a live cycle without treating them as historical replay.
        _require_live_heating_cycle(base)
        snapshot = _capture_heating_snapshot(predheat, base.now_utc)
    else:
        snapshot = getattr(base, "joint_heating_snapshot", None)
        if snapshot is None or snapshot.source is not predheat:
            snapshot = _capture_heating_snapshot(predheat)
    updated_at = snapshot.updated_at
    if updated_at is None or updated_at.tzinfo is None or abs((base.now_utc - updated_at).total_seconds()) > 60:
        raise ValueError("PredHeat snapshot must match the battery planning instant")
    if snapshot.had_errors:
        raise ValueError("PredHeat inputs are unavailable for joint planning")
    inputs = snapshot.inputs
    steps = base.forecast_minutes // PREDICT_STEP
    if base.forecast_minutes % PREDICT_STEP or len(inputs.target_temperature_c) < steps:
        raise ValueError("PredHeat forecast does not cover the battery horizon")
    inputs = replace(
        inputs, target_temperature_c=inputs.target_temperature_c[:steps], outdoor_temperature_c=inputs.outdoor_temperature_c[:steps], heat_output_fraction=inputs.heat_output_fraction[:steps] if inputs.heat_output_fraction is not None else None
    )
    native_targets = snapshot.comfort_targets[:steps]
    baseline = simulate_heating(inputs)
    baseline_kwh = tuple(row.electrical_kwh for row in baseline.intervals)
    # Scaling has already been applied to non-heating demand by calculate_plan;
    # space heating is added once afterwards, equally across the load scenarios.
    combined = tuple({minute: values[minute] + baseline_kwh[minute // PREDICT_STEP] for minute in range(0, base.forecast_minutes, PREDICT_STEP)} for values in loads)
    if refresh_inputs:
        base.joint_heating_snapshot = snapshot
    if not evaluate:
        return combined
    try:
        request = _joint_heating_request(inputs, native_targets, config, baseline)
        _publish_joint_heating_preview(base, loads, pv_steps, request)
    except Exception as error:
        # A qualified baseline is already available. Preview-only faults must
        # not prevent the normal battery planner from using that native load.
        clear_joint_heating_shadow(base, "joint_search_error")
        base.log("PredHeat joint preview unavailable: {}".format(error))
    return combined


def _publish_joint_heating_preview(base, loads, pv_steps, request):
    """Evaluate and publish a preview without affecting the prepared native baseline."""
    predheat = base.predheat
    shadow = copy.copy(base)
    shadow.load_minutes_step, shadow.load_minutes_step10, shadow.load_minutes_step90 = tuple(dict(values) for values in loads)
    shadow.pv_forecast_minute_step, shadow.pv_forecast_minute10_step, shadow.pv_forecast_minute90_step = pv_steps
    result = plan_joint_heating(shadow, request)
    summary = {
        "state": "planned" if result.best is not None else result.reason,
        "generated_at": base.now_utc.isoformat(),
        "termination": result.termination,
        "evaluations": result.evaluations,
        "battery_evaluations": result.battery_evaluations,
        "shadow_only": True,
        "search": "bounded_joint_search",
        "battery_windows_stage": "before_execution_clipping",
        "baseline_comparable": result.baseline is not None,
    }
    if result.best is not None:
        best = result.best
        summary.update(
            {
                "metric": best.battery.metric,
                "cost": best.battery.cost,
                "battery_value": best.battery.battery_value,
                "terminal_soc_kwh": best.battery.final_soc,
                "terminal_room_c": best.simulation.final_state.room_temperature_c,
                "terminal_water_c": best.simulation.final_state.water_temperature_c,
                "charge_windows": list(best.battery.charge_window_best),
                "charge_limits_kwh": list(best.battery.charge_limit_best),
                "export_windows": list(best.battery.export_window_best),
                "export_limits": list(best.battery.export_limits_best),
            }
        )
        if result.baseline is not None:
            summary["baseline_metric"] = result.baseline.battery.metric
            summary["baseline_cost"] = result.baseline.battery.cost
        summary["results"] = {
            (base.now_utc + timedelta(minutes=(index + 1) * PREDICT_STEP)).strftime(TIME_FORMAT): {
                "room_temperature": row.room_temperature_c,
                "water_temperature": row.water_temperature_c,
                "outdoor_temperature": row.outdoor_temperature_c,
                "target_temperature": row.target_temperature_c,
                "heating_kwh": row.electrical_kwh,
            }
            for index, row in enumerate(best.simulation.intervals)
        }
    base.joint_heating_plan = summary
    base.set_state_wrapper(predheat.prefix + ".joint_plan", state=summary["state"], attributes=_preview_attributes(summary))
    base.log("PredHeat joint shadow plan: {} ({} thermal trials, {} battery evaluations)".format(summary["state"], result.evaluations, result.battery_evaluations))
