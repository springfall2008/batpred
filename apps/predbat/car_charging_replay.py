"""Segmented native replay for explicit per-period battery discharge ceilings.

The existing Prediction engines and kernel ABI are unchanged. Each five-minute
step is a new native Python Prediction with the current-rate input set explicitly
for limited demand. This is an independent check, not an actuator implementation.
"""

from copy import copy
from datetime import timedelta
import math
from types import SimpleNamespace

from const import MINUTE_WATT, PREDICT_STEP
from prediction import Prediction


def discharge_limit_w(base, action):
    """Convert a proposed AC ceiling to whole native DC watts without raising a cap."""
    limit = getattr(action, "discharge_limit_kw", None)
    if limit is None:
        return None
    return min(int(base.battery_rate_max_discharge * MINUTE_WATT), math.ceil(limit * 1000 / base.inverter_loss))


def replay_discharge_limits(model, result, slots, scenario):
    """Replay a complete trajectory through unmodified native step calculations.

    Native final SOC is rounded to four decimals. Track the worst accumulated
    rounding uncertainty so acceptance checks can retain a conservative margin.
    The original base, forecasts, active Prediction and kernel remain untouched.
    """
    soc = model.soc_kw
    car_soc = list(model.car_charging_soc)
    trace = {}
    grid = {}
    cost = 0
    peak = 0
    minutes_over = 0
    checked = 0
    for action, slot in zip(result.actions, slots):
        if slot.duration_min != PREDICT_STEP:
            raise ValueError("Native discharge replay requires five-minute steps")
        step_base = copy(model)
        step_base.minutes_now = model.minutes_now + slot.start_min
        step_base.now_utc = model.midnight_utc + timedelta(minutes=step_base.minutes_now)
        step_base.forecast_minutes = PREDICT_STEP
        step_base.soc_kw = soc
        step_base.car_charging_soc = list(car_soc)
        step_base.cost_today_sofar = 0
        step_base.battery_temperature_prediction = {0: model.battery_temperature_prediction.get(slot.start_min, model.battery_temperature)}
        trace[slot.start_min] = soc
        power = discharge_limit_w(model, action)
        if power is not None:
            # No charge/export window applies in limited demand. Suppress only
            # the native per-step rate reset, retaining original curve ceilings.
            step_base.set_charge_window = False
            step_base.set_export_window = False
            step_base.discharge_rate_now = power / MINUTE_WATT
            step_base.charge_rate_now = model.battery_rate_max_charge
        windows = []
        limits = []
        if action.battery_mode in ("charge", "hold"):
            windows = [{"start": step_base.minutes_now, "end": step_base.minutes_now + PREDICT_STEP, "average": slot.import_price}]
            limits = [model.soc_max if action.battery_mode == "charge" else model.reserve]
        pred = Prediction(
            step_base,
            {0: model.pv_forecast_minute_step[slot.start_min]},
            {0: model.pv_forecast_minute10_step[slot.start_min]},
            {0: model.load_minutes_step[slot.start_min]},
            {0: model.load_minutes_step10[slot.start_min]},
        )
        pred.run_prediction(limits, windows, [], [], scenario, end_record=PREDICT_STEP, save="test")
        soc = pred.final_soc
        car_soc = list(pred.final_car_soc)
        cost += pred.final_metric
        grid.update(pred.predict_grid_power)
        diagnostic = getattr(pred, "import_limit_diagnostics", None)
        if diagnostic and diagnostic["checked_minutes"] == PREDICT_STEP:
            peak = max(peak, diagnostic["peak_w"])
            minutes_over += diagnostic["minutes_over"]
            checked += diagnostic["checked_minutes"]
        else:
            values = list(pred.predict_grid_power.values())
            peak = max(peak, max((max(0, value * 1000 + 0.5) for value in values), default=0))
            minutes_over += sum(value * 1000 + 0.5 > model.import_limit for value in values) * PREDICT_STEP
            checked += len(values) * PREDICT_STEP
    return SimpleNamespace(
        predict_soc=trace,
        predict_grid_power=grid,
        final_soc=soc,
        final_car_soc=car_soc,
        final_metric=cost + model.cost_today_sofar,
        import_limit_diagnostics={"peak_w": peak, "minutes_over": minutes_over, "checked_minutes": checked},
        rounding_bound_kwh=len(slots) * 0.00005,
    )
