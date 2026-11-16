# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for battery_thermal - the battery temperature forecast from outdoor temperature and planned battery power (GH#5919)."""

import math
from datetime import datetime, timedelta, timezone

from battery_thermal import ambient_from_results, battery_power_from_soc, plan_power_minutes, solve_linear_3, thermal_fit, thermal_forecast
from const import TIME_FORMAT

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
STEP = 5
MODEL = {"tau_hours": 6.7, "heat_rate": 0.62, "offset": 2.6}


def check(failed, condition, message):
    """Print a failure and return the updated failed flag."""
    if not condition:
        print("ERROR: {}".format(message))
        return True
    return failed


def ambient_at(minute_absolute):
    """A daily outdoor temperature cycle, 2 to 10 degrees C, by minutes from an arbitrary origin."""
    return 6.0 + 4.0 * math.sin(2 * math.pi * minute_absolute / (24 * 60))


def power_at(minute_absolute):
    """A battery that charges at 5 kW from 23:30 for 3 hours and discharges at 3 kW from 17:00 for 2 hours each day."""
    minute_of_day = minute_absolute % (24 * 60)
    if minute_of_day >= 23 * 60 + 30 or minute_of_day < 2 * 60 + 30:
        return 5.0
    if 17 * 60 <= minute_of_day < 19 * 60:
        return 3.0
    return 0.0


def simulate_history(model, days):
    """
    Simulate a battery under the model and return the history series keyed by minutes ago.

    Returns:
        (temperature, soc_kwh, ambient) dicts keyed by minutes ago
    """
    total = days * 24 * 60
    dt_hours = STEP / 60.0
    temp = 12.0
    soc = 5.0
    temperature = {}
    soc_kwh = {}
    ambient = {}
    # Step from the oldest minute to now; minute_absolute counts up from the start of the history
    for minute_absolute in range(0, total + 1, STEP):
        minute_ago = total - minute_absolute
        temperature[minute_ago] = temp
        soc_kwh[minute_ago] = soc
        ambient[minute_ago] = ambient_at(minute_absolute)
        power = power_at(minute_absolute)
        temp += dt_hours * ((ambient_at(minute_absolute) + model["offset"] - temp) / model["tau_hours"] + model["heat_rate"] * power)
        # Alternate the direction of the battery level change so the level stays in range; the fit uses |power|
        soc += power * dt_hours * (1 if (minute_absolute // (24 * 60)) % 2 == 0 else -1)
    return temperature, soc_kwh, ambient


def test_solve_linear_3():
    """The 3x3 solver returns the solution and None for a singular system."""
    failed = False
    solution = solve_linear_3([[2, 1, 0], [1, 3, 1], [0, 1, 4]], [3, 5, 5])
    failed = check(failed, solution is not None and all(abs(v - 1.0) < 1e-9 for v in solution), "solve_linear_3 expected [1, 1, 1], got {}".format(solution))
    failed = check(failed, solve_linear_3([[1, 2, 3], [2, 4, 6], [1, 1, 1]], [1, 2, 3]) is None, "solve_linear_3 should return None for a singular system")
    return failed


def test_fit_recovers_model():
    """Fitting simulated history recovers the model that made it."""
    failed = False
    temperature, soc_kwh, ambient = simulate_history(MODEL, days=7)
    history_minutes = 7 * 24 * 60
    power = battery_power_from_soc(soc_kwh, history_minutes, STEP)
    fit = thermal_fit(temperature, power, ambient, history_minutes, STEP)
    failed = check(failed, fit is not None, "thermal_fit returned None on clean simulated history")
    if fit:
        failed = check(failed, abs(fit["tau_hours"] - MODEL["tau_hours"]) < 0.05, "tau_hours {} expected {}".format(fit["tau_hours"], MODEL["tau_hours"]))
        failed = check(failed, abs(fit["heat_rate"] - MODEL["heat_rate"]) < 0.01, "heat_rate {} expected {}".format(fit["heat_rate"], MODEL["heat_rate"]))
        failed = check(failed, abs(fit["offset"] - MODEL["offset"]) < 0.05, "offset {} expected {}".format(fit["offset"], MODEL["offset"]))
        failed = check(failed, fit["rmse"] < 0.001, "rmse {} expected near 0".format(fit["rmse"]))
    return failed


def test_fit_rejects_short_or_implausible():
    """Too little history, or history no thermal model explains, gives no fit."""
    failed = False
    temperature, soc_kwh, ambient = simulate_history(MODEL, days=7)
    power = battery_power_from_soc(soc_kwh, 7 * 24 * 60, STEP)
    failed = check(failed, thermal_fit(temperature, power, ambient, 12 * 60, STEP) is None, "a 12 hour history should not fit")

    # A battery that warms as it moves away from the outdoor temperature gives a negative a: rejected
    anti = {minute: 20.0 - 0.001 * minute for minute in range(0, 7 * 24 * 60 + 1, STEP)}
    flat_ambient = {minute: 5.0 + 0.0001 * minute for minute in range(0, 7 * 24 * 60 + 1, STEP)}
    no_power = {minute: 0.0 for minute in range(0, 7 * 24 * 60 + 1, STEP)}
    failed = check(failed, thermal_fit(anti, no_power, flat_ambient, 7 * 24 * 60, STEP) is None, "an implausible history should not fit")

    # Missing ambient means no samples
    failed = check(failed, thermal_fit(temperature, power, {}, 7 * 24 * 60, STEP) is None, "no ambient history should not fit")
    return failed


def test_forecast_matches_simulation():
    """Forecasting with the true ambient and power reproduces the simulation; ignoring the power runs colder."""
    failed = False
    history_minutes = 2 * 24 * 60
    temperature, _, _ = simulate_history(MODEL, days=4)
    # Use the first 2 days as "the past" and the last 2 days as "the future": minute ahead m is minute ago (history_minutes - m)
    start_absolute = 2 * 24 * 60
    ambient = {m: ambient_at(start_absolute + m) for m in range(0, history_minutes, STEP)}
    power = {m: power_at(start_absolute + m) for m in range(0, history_minutes, STEP)}
    start_temp = temperature[history_minutes]
    forecast = thermal_forecast(start_temp, MODEL, ambient, power, history_minutes, STEP)
    worst = max(abs(forecast[m] - temperature[history_minutes - m]) for m in range(0, history_minutes, STEP))
    failed = check(failed, worst < 0.02, "forecast differs from the simulation by {}".format(worst))
    failed = check(failed, forecast[0] == round(start_temp, 2), "forecast should start at the current temperature")

    idle = thermal_forecast(start_temp, MODEL, ambient, {}, history_minutes, STEP)
    # 23:30 + 3h of 5 kW charging: by 02:30 the charging battery is well above the idle one
    minute_0230 = (24 * 60 + 2 * 60 + 30) - (start_absolute % (24 * 60))
    failed = check(failed, forecast[minute_0230] - idle[minute_0230] > 5.0, "charging should warm the battery: {} vs idle {}".format(forecast[minute_0230], idle[minute_0230]))

    # A missing ambient minute holds the last value
    held = thermal_forecast(10.0, {"tau_hours": 1.0, "heat_rate": 0.0, "offset": 0.0}, {0: 0.0}, {}, 24 * 60, STEP)
    failed = check(failed, held[24 * 60 - STEP] < 0.1, "forecast should approach the held ambient, got {}".format(held[24 * 60 - STEP]))
    return failed


def test_ambient_from_results():
    """Hourly results are interpolated into past and future step series."""
    failed = False
    results = {}
    for hour in range(-3, 4):
        results[(NOW + timedelta(hours=hour)).strftime(TIME_FORMAT)] = 10.0 + hour
    results["bad"] = "x"
    past, future = ambient_from_results(results, NOW, 4 * 60, 4 * 60, STEP)
    failed = check(failed, abs(past[0] - 10.0) < 1e-9 and abs(future[0] - 10.0) < 1e-9, "minute 0 should be 10, got {} {}".format(past.get(0), future.get(0)))
    failed = check(failed, abs(future[30] - 10.5) < 1e-9, "30 minutes ahead should interpolate to 10.5, got {}".format(future.get(30)))
    failed = check(failed, abs(past[90] - 8.5) < 1e-9, "90 minutes ago should interpolate to 8.5, got {}".format(past.get(90)))
    failed = check(failed, 185 not in past and 180 in past and 185 not in future, "minutes outside the results should be left out")
    failed = check(failed, ambient_from_results({}, NOW, 60, 60, STEP) == ({}, {}), "empty results should give empty series")
    return failed


def test_plan_power_minutes():
    """A plan made a cycle ago is aligned onto minutes ahead of now, as a magnitude."""
    failed = False
    plan_start = NOW - timedelta(minutes=5)
    plan = {}
    for minute in range(0, 60, STEP):
        plan[(plan_start + timedelta(minutes=minute)).strftime(TIME_FORMAT)] = -5.0 if minute < 30 else 2.0
    power = plan_power_minutes(plan, NOW, 120, STEP)
    failed = check(failed, power.get(0) == 5.0 and power.get(20) == 5.0, "charging steps should give 5 kW, got {}".format(power))
    failed = check(failed, power.get(25) == 2.0 and power.get(50) == 2.0, "discharging steps should give 2 kW, got {}".format(power))
    failed = check(failed, 55 not in power, "minutes past the end of the plan should be left out")
    failed = check(failed, plan_power_minutes({}, NOW, 60, STEP) == {}, "no plan gives no power")
    return failed


def test_battery_power_from_soc():
    """Battery power is the magnitude of the level change over each step."""
    failed = False
    soc = {0: 10.0, 5: 9.5, 10: 9.5, 20: 8.0}
    power = battery_power_from_soc(soc, 20, STEP)
    failed = check(failed, abs(power[5] - 6.0) < 1e-9, "a 0.5 kWh rise in 5 minutes is 6 kW, got {}".format(power.get(5)))
    failed = check(failed, power[10] == 0.0, "no change is 0 kW")
    failed = check(failed, 15 not in power and 20 not in power, "steps with a missing end should be left out")
    return failed


def run_battery_thermal_tests(my_predbat=None):
    """Run the battery thermal model tests; returns True on failure."""
    failed = False
    failed |= test_solve_linear_3()
    failed |= test_fit_recovers_model()
    failed |= test_fit_rejects_short_or_implausible()
    failed |= test_forecast_matches_simulation()
    failed |= test_ambient_from_results()
    failed |= test_plan_power_minutes()
    failed |= test_battery_power_from_soc()
    return failed
