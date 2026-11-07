"""Isolated battery re-optimisation for generic, explicit non-heating load scenarios."""

import copy
import math
from dataclasses import dataclass, field
from types import MethodType
from typing import Any

from const import EXPORT_MODE_IDLE, PREDICT_STEP
from prediction import Prediction
from utils import clone_windows, pack_export_limit, remove_intersecting_windows


def _copy_value(value, memo):
    """Copy nested planning containers without cloning live service objects or kernel handles."""
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if isinstance(value, dict):
        result = {}
        memo[identity] = result
        result.update((key, _copy_value(item, memo)) for key, item in value.items())
        return result
    if isinstance(value, list):
        result = []
        memo[identity] = result
        result.extend(_copy_value(item, memo) for item in value)
        return result
    if isinstance(value, tuple):
        result = tuple(_copy_value(item, memo) for item in value)
        memo[identity] = result
        return result
    if isinstance(value, set):
        result = {_copy_value(item, memo) for item in value}
        memo[identity] = result
        return result
    return value


def _no_publish(*args, **kwargs):
    """Fail closed if a supposedly internal battery trial reaches an output boundary."""
    raise RuntimeError("Battery load scenario attempted external output")


def _planning_shell(base):
    """Own every mutable planning container and rebind instance-bound methods to the shell."""
    shell = copy.copy(base)
    memo = {}
    for name, value in vars(base).items():
        if isinstance(value, MethodType) and value.__self__ is base:
            setattr(shell, name, MethodType(value.__func__, shell))
        else:
            setattr(shell, name, _copy_value(value, memo))
    shell.log = lambda *args, **kwargs: None
    shell.debug_enable = False
    shell.prediction = None
    for name in dir(shell):
        if name.startswith(("publish_", "set_state", "call_service", "save_", "write_")) or name in ("dashboard_item", "record_status", "execute_plan", "expose_config", "async_expose_config", "api_select", "api_select_update", "manual_select"):
            setattr(shell, name, _no_publish)
    return shell


@dataclass(frozen=True)
class BatteryHeatEvaluation:
    """A scored heating candidate and its isolated battery planning state."""

    metric: float
    cost: float
    battery_value: float
    final_soc: float
    import_kwh: float
    export_kwh: float
    charge_window_best: tuple
    charge_limit_best: tuple
    export_window_best: tuple
    export_limits_best: tuple
    load_minutes_step: dict
    load_minutes_step10: dict
    load_minutes_step90: dict
    planner: Any = field(repr=False, compare=False)


class BatteryHeatEvaluator:
    """Re-run the actual battery optimiser against an explicit non-heating baseline.

    Inputs are relative-minute dictionaries of kWh per PREDICT_STEP, already stepped
    and scaled. Callers must remove existing heating before supplying these baselines.
    Candidate heating is added afterwards, once, without household load scaling.
    Construction snapshots the live inputs; each evaluate starts from that same snapshot.
    """

    def __init__(self, base, non_heating_load_steps, non_heating_load_steps10=None, non_heating_load_steps90=None):
        """Capture immutable-in-practice planning inputs and validate the baseline contract."""
        self._base = _planning_shell(base)
        self._loads = tuple(
            dict(values) for values in (non_heating_load_steps, non_heating_load_steps10 if non_heating_load_steps10 is not None else non_heating_load_steps, non_heating_load_steps90 if non_heating_load_steps90 is not None else non_heating_load_steps)
        )
        self._steps = math.ceil(base.forecast_minutes / PREDICT_STEP)
        self._batch_threads = getattr(getattr(base, "prediction", None), "batch_threads", 1)
        for values in self._loads:
            for minute in range(0, self._steps * PREDICT_STEP, PREDICT_STEP):
                value = values.get(minute)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("A complete finite non-negative non-heating baseline is required")

    def evaluate(self, heating_kwh):
        """Optimise and score one full-horizon heating profile, without publishing or live mutation."""
        if len(heating_kwh) != self._steps or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in heating_kwh):
            raise ValueError("Heating must contain one finite non-negative kWh value per prediction step")
        planner = _planning_shell(self._base)
        loads = [dict(values) for values in self._loads]
        for index, value in enumerate(heating_kwh):
            for values in loads:
                values[index * PREDICT_STEP] += value
        planner.load_minutes_step, planner.load_minutes_step10, planner.load_minutes_step90 = loads
        planner.plan_valid = False
        planner.plan_preclip = None
        planner.rate_best_cost_threshold_charge = None
        planner.rate_best_cost_threshold_export = None
        planner.end_record = planner.forecast_minutes
        planner.charge_window_best = clone_windows(planner.low_rates if planner.low_rates and planner.calculate_best_charge and planner.set_charge_window else planner.charge_window)
        planner.export_window_best = clone_windows(planner.high_export_rates if planner.calculate_best_export and planner.set_export_window else planner.export_window)
        planner.prefill_charge_limit_best()
        planner.export_limits_best = [pack_export_limit(EXPORT_MODE_IDLE) for _ in planner.export_window_best]
        planner.prediction = Prediction(planner, planner.pv_forecast_minute_step, planner.pv_forecast_minute10_step, loads[0], loads[1], planner.pv_forecast_minute90_step, loads[2], car_charging_slots=planner.car_charging_slots_model())
        planner.prediction.batch_threads = self._batch_threads
        initial = planner.run_prediction_metric(planner.charge_limit, planner.charge_window, planner.export_window, planner.export_limits, end_record=planner.end_record)
        if planner.calculate_best:
            planner.optimise_all_windows(initial[0], initial[3], debug_mode=False)
            planner.update_target_values()
        planner.charge_limit_best, planner.charge_window_best = remove_intersecting_windows(planner.charge_limit_best, planner.charge_window_best, planner.export_limits_best, planner.export_window_best)
        # The optimiser can shorten end_record according to its chosen windows. Compare all
        # heating candidates over one common horizon, including residual battery value.
        planner.end_record = planner.forecast_minutes
        metric, battery_value, cost, _, _, _, import_kwh, export_kwh = planner.run_prediction_metric(planner.charge_limit_best, planner.charge_window_best, planner.export_window_best, planner.export_limits_best, end_record=planner.end_record)
        final_nominal = planner.run_prediction(planner.charge_limit_best, planner.charge_window_best, planner.export_window_best, planner.export_limits_best, False, end_record=planner.end_record)
        final_soc = final_nominal[5]
        if final_soc is None or not all(math.isfinite(value) for value in (metric, battery_value, cost, final_soc, import_kwh, export_kwh)):
            raise ValueError("Battery load scenario returned an invalid result")
        return BatteryHeatEvaluation(
            metric,
            cost,
            battery_value,
            final_soc,
            import_kwh,
            export_kwh,
            tuple(clone_windows(planner.charge_window_best)),
            tuple(planner.charge_limit_best),
            tuple(clone_windows(planner.export_window_best)),
            tuple(planner.export_limits_best),
            dict(loads[0]),
            dict(loads[1]),
            dict(loads[2]),
            planner,
        )
