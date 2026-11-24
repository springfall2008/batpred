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

The battery is treated as one thermal mass that cools or warms towards the outdoor temperature and is warmed by its own
charging and discharging:

    dT/dt = (ambient - T) / tau + heat_rate * |battery power|

tau (hours) and heat_rate (degrees C per hour per kW) are fitted per install by least squares on one step ahead changes in
the battery temperature history (GH#5919). tau comes from the idle periods and heat_rate from the charging and discharging,
so the two are fitted separately. There is deliberately no offset or partial coupling to the outdoor temperature: over the
narrow range of outdoor temperatures seen in a week, those trade against each other and against heat_rate and extrapolate
badly into colder weather. A battery that the outdoor temperature does not fully reach (a garage or a heated room) fits a
long tau instead; it is forecast too cold, which errs towards a lower charge rate, and only matters if it gets cold.

Series follow Predbat's minute conventions: history is keyed by minutes ago (0 is now) and forecasts by minutes ahead.
"""

import math
from datetime import timedelta

from utils import str2time

# Bounds for a fitted model to be believed: outside them the fit is taken to be noise or a sensor fault
THERMAL_TAU_HOURS_MIN = 0.5
THERMAL_TAU_HOURS_MAX = 200.0
THERMAL_HEAT_RATE_MAX = 5.0
# The number of fitted parameters: dt / tau and heat_rate * dt
THERMAL_PARAMETERS = 2
# The fit needs at least a day of complete steps
THERMAL_MIN_SAMPLES_MINUTES = 24 * 60
# The forecast is held within the range a battery temperature sensor can sensibly report
# A step whose temperature moves faster than this (°C per minute, 2 °C in 5 minutes; charging warmed a battery 7.3 °C in an hour)
# is a sensor glitch, not heat, and is left out of the fit
THERMAL_MAX_CHANGE_PER_MINUTE = 0.4
THERMAL_TEMPERATURE_MIN = -30.0
THERMAL_TEMPERATURE_MAX = 60.0


def solve_linear(matrix, vector):
    """
    Solve a square linear system by Gaussian elimination with partial pivoting.

    Args:
        matrix: n rows of n floats
        vector: n floats

    Returns:
        The solution as a list of n floats, or None when the system is singular
    """
    size = len(vector)
    rows = [list(matrix[i]) + [vector[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, size):
            factor = rows[r][col] / rows[col][col]
            for c in range(col, size + 1):
                rows[r][c] -= factor * rows[col][c]
    solution = [0.0] * size
    for r in range(size - 1, -1, -1):
        total = rows[r][size] - sum(rows[r][c] * solution[c] for c in range(r + 1, size))
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


def thermal_statistics(temperature, power, ambient, history_minutes, step, newest_minute=None, max_power=None):
    """
    Least squares statistics of the thermal model over history: what thermal_fit_statistics() solves.

    The model, stepped by dt hours, is T[n+1] - T[n] = k (A[n] - T[n]) + h |P[n]|, with k = dt / tau and h = heat_rate * dt. Keeping the sums rather than the samples lets evidence build up over months
    in a few numbers (thermal_statistics_merge) without reading months of history.

    Args:
        temperature: battery temperature in degrees C keyed by minutes ago
        power: magnitude of the battery power in kW over the step starting at each minute ago (battery_power_from_soc)
        ambient: outdoor temperature in degrees C keyed by minutes ago
        history_minutes: the oldest step to use, in minutes ago
        step: step length in minutes
        newest_minute: the newest step to use, in minutes ago (default one step ago, the newest complete step)
        max_power: the most power in kW the battery can move; a step showing more is a glitch and left out (None: no limit)

    Returns:
        dict of xtx (2x2), xty (2), yty, weight (the number of steps, decayed once merged) and samples (steps added)
    """
    size = THERMAL_PARAMETERS
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    yty = 0.0
    samples = 0
    for minute in range(newest_minute or step, history_minutes + 1, step):
        temp_now = temperature.get(minute)
        temp_next = temperature.get(minute - step)
        ambient_now = ambient.get(minute)
        power_now = power.get(minute)
        if temp_now is None or temp_next is None or ambient_now is None or power_now is None:
            continue
        if not all(math.isfinite(value) for value in (temp_now, temp_next, ambient_now, power_now)):
            continue
        # One glitch would dominate the sums and stay in the stored evidence for months, so implausible steps are left out:
        # a jump no heat explains (which also catches a missing reading reported as 0 by a warmer battery, while a real
        # reading at freezing is kept) or more power than the battery has
        if abs(temp_next - temp_now) > THERMAL_MAX_CHANGE_PER_MINUTE * step:
            continue
        if max_power is not None and power_now > max_power:
            continue
        x = (ambient_now - temp_now, power_now)
        y = temp_next - temp_now
        samples += 1
        yty += y * y
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    return {"xtx": xtx, "xty": xty, "yty": yty, "weight": float(samples), "samples": samples}


def thermal_statistics_merge(stored, new, decay):
    """
    Add new statistics to stored ones, the stored evidence first scaled by decay (0 to 1) for its age.

    Args:
        stored: thermal_statistics() result (or a merge of them), or None
        new: thermal_statistics() result
        decay: the factor the stored evidence is scaled by

    Returns:
        The merged statistics; stored and new are left unchanged
    """
    if not valid_thermal_statistics(stored):
        return {"xtx": [list(row) for row in new["xtx"]], "xty": list(new["xty"]), "yty": new["yty"], "weight": new["weight"], "samples": new["samples"]}
    size = THERMAL_PARAMETERS
    return {
        "xtx": [[stored["xtx"][i][j] * decay + new["xtx"][i][j] for j in range(size)] for i in range(size)],
        "xty": [stored["xty"][i] * decay + new["xty"][i] for i in range(size)],
        "yty": stored["yty"] * decay + new["yty"],
        "weight": stored["weight"] * decay + new["weight"],
        "samples": stored.get("samples", 0) + new["samples"],
    }


def valid_thermal_statistics(stats):
    """True when stats has the shape thermal_statistics() returns (as read back from storage; an older shape is not valid)."""
    size = THERMAL_PARAMETERS
    try:
        if not (len(stats["xtx"]) == size and all(len(row) == size for row in stats["xtx"]) and len(stats["xty"]) == size and float(stats["yty"]) >= 0 and float(stats["weight"]) >= 0):
            return False
        # A NaN would stay in the merged sums for good and stop every fit
        values = [float(value) for row in stats["xtx"] for value in row] + [float(value) for value in stats["xty"]] + [float(stats["yty"]), float(stats["weight"])]
        return all(math.isfinite(value) for value in values)
    except (KeyError, TypeError, ValueError):
        return False


def thermal_fit_statistics(stats, step):
    """
    Solve thermal statistics for the model.

    Args:
        stats: thermal_statistics() result (or a merge of them)
        step: step length in minutes the statistics were gathered at

    Returns:
        dict with tau_hours, heat_rate, samples (decayed step count, rounded) and rmse (degrees C per step), or None
        when there is less than THERMAL_MIN_SAMPLES_MINUTES of evidence or the fit lies outside the plausible bounds
    """
    if not valid_thermal_statistics(stats) or stats["weight"] * step < THERMAL_MIN_SAMPLES_MINUTES:
        return None
    xtx, xty = stats["xtx"], stats["xty"]
    solution = solve_linear(xtx, xty)
    if solution is None:
        return None
    k, h = solution
    if h < 0 and xtx[0][0] > 0:
        # Warming can't be negative: a slightly negative heat term (charging that happened to coincide with cooling) means
        # none was seen, so fit the time constant alone rather than dropping the model
        k, h = xty[0] / xtx[0][0], 0.0
        solution = [k, h]
    dt_hours = step / 60.0
    if k <= 0:
        return None
    tau_hours = dt_hours / k
    heat_rate = h / dt_hours
    if not (THERMAL_TAU_HOURS_MIN <= tau_hours <= THERMAL_TAU_HOURS_MAX) or not (0 <= heat_rate <= THERMAL_HEAT_RATE_MAX):
        return None
    # Residual sum of squares from the sums: y'y - 2 s'X'y + s'X'X s
    size = THERMAL_PARAMETERS
    sum_squares = stats["yty"] - 2 * sum(solution[i] * xty[i] for i in range(size)) + sum(solution[i] * xtx[i][j] * solution[j] for i in range(size) for j in range(size))
    return {
        "tau_hours": round(tau_hours, 3),
        "heat_rate": round(heat_rate, 4),
        "samples": int(round(stats["weight"])),
        "rmse": round((max(sum_squares, 0.0) / stats["weight"]) ** 0.5, 4),
    }


def thermal_fit(temperature, power, ambient, history_minutes, step):
    """
    Fit the thermal model to one window of history (thermal_statistics then thermal_fit_statistics).

    Args:
        temperature: battery temperature in degrees C keyed by minutes ago
        power: magnitude of the battery power in kW over the step starting at each minute ago (battery_power_from_soc)
        ambient: outdoor temperature in degrees C keyed by minutes ago
        history_minutes: how far back to use
        step: step length in minutes

    Returns:
        thermal_fit_statistics() result, or None
    """
    return thermal_fit_statistics(thermal_statistics(temperature, power, ambient, history_minutes, step), step)


def thermal_forecast(current_temp, model, ambient, power, forecast_minutes, step):
    """
    Forecast the battery temperature by stepping the fitted thermal model forward.

    Args:
        current_temp: the battery temperature now, degrees C
        model: a thermal_fit result
        ambient: outdoor temperature forecast keyed by minutes ahead; a missing minute holds the last value, and before the
            first value the battery is held at its target
        power: magnitude of the planned battery power in kW keyed by minutes ahead; a missing minute counts as idle
        forecast_minutes: how far ahead to forecast
        step: step length in minutes

    Returns:
        The forecast temperature in degrees C keyed by minutes ahead, from 0 (now) in steps
    """
    dt_hours = step / 60.0
    tau_hours = model["tau_hours"]
    heat_rate = model["heat_rate"]
    temp = float(current_temp)
    ambient_last = ambient.get(0)
    forecast = {}
    for minute in range(0, forecast_minutes, step):
        # An exact 0 is looked up as a missing reading (utils.battery_temperature_index), which a forecast never is
        value = round(temp, 2)
        forecast[minute] = value if value != 0 else (0.01 if temp >= 0 else -0.01)
        ambient_last = ambient.get(minute, ambient_last)
        target = temp if ambient_last is None else ambient_last
        temp += dt_hours * ((target - temp) / tau_hours + heat_rate * power.get(minute, 0.0))
        temp = min(max(temp, THERMAL_TEMPERATURE_MIN), THERMAL_TEMPERATURE_MAX)
    return forecast
