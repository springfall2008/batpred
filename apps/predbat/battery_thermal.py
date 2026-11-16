# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Battery thermal model: a forecast of the battery temperature from the outdoor temperature forecast and the planned battery power.

The battery is treated as one thermal mass that drifts towards the outdoor temperature (plus an offset for its siting) and
is warmed by its own charging and discharging:

    dT/dt = (ambient + offset - T) / tau + heat_rate * |battery power|

tau (hours), heat_rate (degrees C per hour per kW) and offset (degrees C) are fitted per install by least squares on one
step ahead changes in the battery temperature history (GH#5919). The offset absorbs a battery sited indoors or against a
warm wall and a bias in the outdoor temperature source.

Series follow Predbat's minute conventions: history is keyed by minutes ago (0 is now) and forecasts by minutes ahead.
"""

from datetime import timedelta

from utils import str2time

# Bounds for a fitted model to be believed: outside them the fit is taken to be noise or a sensor fault
THERMAL_TAU_HOURS_MIN = 0.5
THERMAL_TAU_HOURS_MAX = 200.0
THERMAL_HEAT_RATE_MAX = 5.0
THERMAL_OFFSET_MAX = 25.0
# The fit needs at least a day of complete steps
THERMAL_MIN_SAMPLES_MINUTES = 24 * 60
# The forecast is held within the range a battery temperature sensor can sensibly report
THERMAL_TEMPERATURE_MIN = -30.0
THERMAL_TEMPERATURE_MAX = 60.0


def solve_linear_3(matrix, vector):
    """
    Solve a 3x3 linear system by Gaussian elimination with partial pivoting.

    Args:
        matrix: three rows of three floats
        vector: three floats

    Returns:
        The solution as a list of three floats, or None when the system is singular
    """
    rows = [list(matrix[i]) + [vector[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, 3):
            factor = rows[r][col] / rows[col][col]
            for c in range(col, 4):
                rows[r][c] -= factor * rows[col][c]
    solution = [0.0, 0.0, 0.0]
    for r in range(2, -1, -1):
        total = rows[r][3] - sum(rows[r][c] * solution[c] for c in range(r + 1, 3))
        solution[r] = total / rows[r][r]
    return solution


def battery_power_from_soc(soc_kwh, history_minutes, step):
    """
    Average battery power over each step, from the battery level history.

    Args:
        soc_kwh: battery level in kWh keyed by minutes ago
        history_minutes: how far back to go
        step: step length in minutes

    Returns:
        The magnitude of the battery power in kW over the step that starts at each minute ago, keyed by that minute.
        Steps where either end is missing are left out.
    """
    power = {}
    for minute in range(step, history_minutes + 1, step):
        start = soc_kwh.get(minute)
        end = soc_kwh.get(minute - step)
        if start is None or end is None:
            continue
        power[minute] = abs(end - start) * 60.0 / step
    return power


def ambient_from_results(results, now_utc, minutes_back, minutes_forward, step):
    """
    Turn a temperature sensor's results attribute (timestamp to degrees C, usually hourly) into step series.

    Values between two points are interpolated linearly; minutes outside the points are left out.

    Args:
        results: dict of timestamp string to temperature
        now_utc: the time minute 0 refers to
        minutes_back: how far into the past to fill
        minutes_forward: how far into the future to fill
        step: step length in minutes

    Returns:
        (past, future): past keyed by minutes ago, future keyed by minutes ahead
    """
    points = []
    for key, value in (results or {}).items():
        try:
            points.append((str2time(key), float(value)))
        except (ValueError, TypeError):
            continue
    points.sort(key=lambda point: point[0])
    past = {}
    future = {}
    if len(points) < 2:
        return past, future

    def value_at(when):
        """Linear interpolation at a datetime, None outside the points."""
        if when < points[0][0] or when > points[-1][0]:
            return None
        low, high = 0, len(points) - 1
        while high - low > 1:
            middle = (low + high) // 2
            if points[middle][0] <= when:
                low = middle
            else:
                high = middle
        (time_low, temp_low), (time_high, temp_high) = points[low], points[high]
        span = (time_high - time_low).total_seconds()
        if span <= 0:
            return temp_low
        fraction = (when - time_low).total_seconds() / span
        return temp_low + (temp_high - temp_low) * min(max(fraction, 0.0), 1.0)

    for minute in range(0, minutes_back + 1, step):
        value = value_at(now_utc - timedelta(minutes=minute))
        if value is not None:
            past[minute] = value
    for minute in range(0, minutes_forward + 1, step):
        value = value_at(now_utc + timedelta(minutes=minute))
        if value is not None:
            future[minute] = value
    return past, future


def plan_power_minutes(power_by_stamp, now_utc, forecast_minutes, step):
    """
    Turn a plan's battery power (timestamp string to kW) into its magnitude by minutes ahead of now.

    Each minute takes the latest plan step at or before it, so a plan made a cycle ago still lines up.

    Args:
        power_by_stamp: dict of timestamp string to battery power in kW (positive discharging, negative charging)
        now_utc: the time minute 0 refers to
        forecast_minutes: how far ahead to fill
        step: step length in minutes

    Returns:
        The magnitude of the planned battery power in kW keyed by minutes ahead; minutes the plan does not cover are left out
    """
    points = []
    for key, value in (power_by_stamp or {}).items():
        try:
            points.append((str2time(key), abs(float(value))))
        except (ValueError, TypeError):
            continue
    points.sort(key=lambda point: point[0])
    power = {}
    if not points:
        return power
    index = 0
    plan_step = timedelta(minutes=step)
    for minute in range(0, forecast_minutes, step):
        when = now_utc + timedelta(minutes=minute)
        if when < points[0][0] or when >= points[-1][0] + plan_step:
            continue
        while index + 1 < len(points) and points[index + 1][0] <= when:
            index += 1
        power[minute] = points[index][1]
    return power


def thermal_fit(temperature, power, ambient, history_minutes, step):
    """
    Fit the thermal model to history by least squares on one step ahead temperature changes.

    The model, stepped by dt hours, is T[n+1] - T[n] = a (A[n] - T[n]) + b + h |P[n]|, with a = dt / tau,
    b = a * offset and h = heat_rate * dt.

    Args:
        temperature: battery temperature in degrees C keyed by minutes ago
        power: magnitude of the battery power in kW over the step starting at each minute ago (battery_power_from_soc)
        ambient: outdoor temperature in degrees C keyed by minutes ago
        history_minutes: how far back to use
        step: step length in minutes

    Returns:
        dict with tau_hours, heat_rate, offset, samples and rmse (degrees C per step), or None when there is too
        little history or the fit lies outside the plausible bounds
    """
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0, 0.0, 0.0]
    rows = []
    for minute in range(step, history_minutes + 1, step):
        temp_now = temperature.get(minute)
        temp_next = temperature.get(minute - step)
        ambient_now = ambient.get(minute)
        power_now = power.get(minute)
        if temp_now is None or temp_next is None or ambient_now is None or power_now is None:
            continue
        x = (ambient_now - temp_now, 1.0, power_now)
        y = temp_next - temp_now
        rows.append((x, y))
        for i in range(3):
            xty[i] += x[i] * y
            for j in range(3):
                xtx[i][j] += x[i] * x[j]

    if len(rows) * step < THERMAL_MIN_SAMPLES_MINUTES:
        return None
    solution = solve_linear_3(xtx, xty)
    if solution is None:
        return None
    a, b, h = solution
    dt_hours = step / 60.0
    if a <= 0:
        return None
    tau_hours = dt_hours / a
    offset = b / a
    heat_rate = h / dt_hours
    if not (THERMAL_TAU_HOURS_MIN <= tau_hours <= THERMAL_TAU_HOURS_MAX) or not (0 <= heat_rate <= THERMAL_HEAT_RATE_MAX) or abs(offset) > THERMAL_OFFSET_MAX:
        return None
    sum_squares = sum((y - (a * x[0] + b + h * x[2])) ** 2 for x, y in rows)
    return {
        "tau_hours": round(tau_hours, 3),
        "heat_rate": round(heat_rate, 4),
        "offset": round(offset, 3),
        "samples": len(rows),
        "rmse": round((sum_squares / len(rows)) ** 0.5, 4),
    }


def thermal_forecast(current_temp, model, ambient, power, forecast_minutes, step):
    """
    Forecast the battery temperature by stepping the fitted thermal model forward.

    Args:
        current_temp: the battery temperature now, degrees C
        model: a thermal_fit result
        ambient: outdoor temperature forecast keyed by minutes ahead; a missing minute holds the last value
        power: magnitude of the planned battery power in kW keyed by minutes ahead; a missing minute counts as idle
        forecast_minutes: how far ahead to forecast
        step: step length in minutes

    Returns:
        The forecast temperature in degrees C keyed by minutes ahead, from 0 (now) in steps
    """
    dt_hours = step / 60.0
    tau_hours = model["tau_hours"]
    heat_rate = model["heat_rate"]
    offset = model["offset"]
    temp = float(current_temp)
    ambient_last = ambient.get(0, temp - offset)
    forecast = {}
    for minute in range(0, forecast_minutes, step):
        forecast[minute] = round(temp, 2)
        ambient_last = ambient.get(minute, ambient_last)
        temp += dt_hours * ((ambient_last + offset - temp) / tau_hours + heat_rate * power.get(minute, 0.0))
        temp = min(max(temp, THERMAL_TEMPERATURE_MIN), THERMAL_TEMPERATURE_MAX)
    return forecast
