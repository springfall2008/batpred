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

import tempfile

from battery_thermal import ambient_from_results, battery_power_from_soc, plan_power_minutes, solve_linear, thermal_fit, thermal_fit_statistics, thermal_forecast, thermal_statistics, thermal_statistics_merge, valid_thermal_statistics
from const import PREDICT_STEP, TIME_FORMAT
from fetch import BATTERY_TEMPERATURE_HISTORY_DAYS, BATTERY_THERMAL_FIT_DAYS, BATTERY_THERMAL_HALF_LIFE_DAYS, BATTERY_THERMAL_RETRY_MINUTES, BATTERY_THERMAL_STORE_KEY
from utils import classify_log_line
from tests.test_infra import MockStorageComponents, make_test_storage, run_async

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
STEP = 5
MODEL = {"tau_hours": 6.7, "heat_rate": 0.62}
# A battery in a heated room, simulated: barely follows the outdoor temperature (alpha) and is held near the room temperature (offset)
INDOOR_TRUTH = {"tau_hours": 10.0, "alpha": 0.05, "heat_rate": 0.4, "offset": 18.0}


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


def simulate_absolute(model, days):
    """
    Simulate a battery under the model.

    Returns:
        (temperature, soc_kwh) dicts keyed by minutes from the start of the simulation, every STEP minutes
    """
    dt_hours = STEP / 60.0
    temp = 12.0
    soc = 5.0
    temperature = {}
    soc_kwh = {}
    for minute_absolute in range(0, days * 24 * 60 + 1, STEP):
        temperature[minute_absolute] = temp
        soc_kwh[minute_absolute] = soc
        power = power_at(minute_absolute)
        target = model.get("alpha", 1.0) * ambient_at(minute_absolute) + model.get("offset", 0.0)
        temp += dt_hours * ((target - temp) / model["tau_hours"] + model["heat_rate"] * power)
        # Alternate the direction of the battery level change so the level stays in range; the fit uses |power|
        soc += power * dt_hours * (1 if (minute_absolute // (24 * 60)) % 2 == 0 else -1)
    return temperature, soc_kwh


def minutes_ago(series, now_absolute, history_minutes):
    """A series keyed by minutes from the start, as minutes ago from now_absolute, back to history_minutes."""
    return {now_absolute - minute: value for minute, value in series.items() if 0 <= now_absolute - minute <= history_minutes}


def simulate_history(model, days):
    """
    Simulate a battery under the model and return the history series keyed by minutes ago.

    Returns:
        (temperature, soc_kwh, ambient) dicts keyed by minutes ago
    """
    total = days * 24 * 60
    temperature, soc_kwh = simulate_absolute(model, days)
    ambient = {total - minute: ambient_at(minute) for minute in temperature}
    return minutes_ago(temperature, total, total), minutes_ago(soc_kwh, total, total), ambient


def test_solve_linear():
    """The solver returns the solution and None for a singular system."""
    failed = False
    solution = solve_linear([[2, 1, 0], [1, 3, 1], [0, 1, 4]], [3, 5, 5])
    failed = check(failed, solution is not None and all(abs(v - 1.0) < 1e-9 for v in solution), "solve_linear expected [1, 1, 1], got {}".format(solution))
    solution = solve_linear([[3, 1], [1, 2]], [4, 3])
    failed = check(failed, solution is not None and all(abs(v - 1.0) < 1e-9 for v in solution), "solve_linear 2x2 expected [1, 1], got {}".format(solution))
    failed = check(failed, solve_linear([[1, 2, 3], [2, 4, 6], [1, 1, 1]], [1, 2, 3]) is None, "solve_linear should return None for a singular system")
    return failed


def test_fit_recovers_model():
    """Fitting simulated history recovers the model that made it; a battery in a heated room fits a long tau instead."""
    failed = False
    history_minutes = 7 * 24 * 60
    temperature, soc_kwh, ambient = simulate_history(MODEL, days=7)
    power = battery_power_from_soc(soc_kwh, history_minutes, STEP)
    fit = thermal_fit(temperature, power, ambient, history_minutes, STEP)
    failed = check(failed, fit is not None, "thermal_fit returned None on clean simulated history")
    if fit:
        failed = check(failed, abs(fit["tau_hours"] - MODEL["tau_hours"]) < 0.05, "tau_hours {} expected {}".format(fit["tau_hours"], MODEL["tau_hours"]))
        failed = check(failed, abs(fit["heat_rate"] - MODEL["heat_rate"]) < 0.01, "heat_rate {} expected {}".format(fit["heat_rate"], MODEL["heat_rate"]))
        failed = check(failed, fit["rmse"] < 0.001, "rmse {} expected near 0".format(fit["rmse"]))

    # Indoors the outdoor temperature barely reaches the battery: the fit is still accepted, with a tau far longer than outdoors
    temperature, soc_kwh, ambient = simulate_history(INDOOR_TRUTH, days=7)
    power = battery_power_from_soc(soc_kwh, history_minutes, STEP)
    fit = thermal_fit(temperature, power, ambient, history_minutes, STEP)
    failed = check(failed, fit is not None and fit["tau_hours"] > 3 * MODEL["tau_hours"], "an indoor battery should fit a long tau, got {}".format(fit))
    return failed


def fit_equations(temperature, power, ambient):
    """The normal equations of a week of history, to check they are solvable."""
    stats = thermal_statistics(temperature, power, ambient, 7 * 24 * 60, STEP)
    return stats["xtx"], stats["xty"]


def test_fit_and_forecast_edges():
    """A slightly negative heat term fits the time constant alone."""
    failed = False
    step = STEP
    dt_hours = step / 60.0
    # Sums from k = dt/6 and h = -0.001: the heat term is dropped and tau comes from the first column alone
    k_true = dt_hours / 6.0
    xs = [(10.0 - 0.1 * i, (i % 3) * 1.0) for i in range(400)]
    ys = [k_true * a - 0.001 * p for a, p in xs]
    stats = {
        "xtx": [[sum(a * a for a, _ in xs), sum(a * p for a, p in xs)], [sum(a * p for a, p in xs), sum(p * p for _, p in xs)]],
        "xty": [sum(a * y for (a, _), y in zip(xs, ys)), sum(p * y for (_, p), y in zip(xs, ys))],
        "yty": sum(y * y for y in ys),
        "weight": 400.0,
        "samples": 400,
    }
    fit = thermal_fit_statistics(stats, step)
    failed = check(failed, fit is not None and fit["heat_rate"] == 0 and 5.0 < fit["tau_hours"] < 7.0, "a slightly negative heat term should fit tau alone, got {}".format(fit))

    return failed


def test_fit_ignores_glitches():
    """
    A dropped battery level reading (a huge apparent power), a 0 °C reading or a sudden jump in one step would otherwise
    dominate the sums and stay in the stored evidence for months: they are left out, and the fit is unchanged.
    """
    failed = False
    history_minutes = 7 * 24 * 60
    temperature, soc_kwh, ambient = simulate_history(MODEL, days=7)
    clean = thermal_fit(temperature, battery_power_from_soc(soc_kwh, history_minutes, STEP), ambient, history_minutes, STEP)

    glitched_soc = dict(soc_kwh)
    glitched_soc[3000] = 0.0
    glitched_temperature = dict(temperature)
    glitched_temperature[4000] = 0.0
    glitched_temperature[5000] = temperature[5000] + 6.0
    power = battery_power_from_soc(glitched_soc, history_minutes, STEP)
    stats = thermal_statistics(glitched_temperature, power, ambient, history_minutes, STEP, max_power=10.0)
    glitched = thermal_fit_statistics(stats, STEP)
    failed = check(failed, clean is not None and glitched is not None, "both should fit")
    if clean and glitched:
        failed = check(failed, abs(glitched["tau_hours"] - clean["tau_hours"]) < 0.05 * clean["tau_hours"], "a glitch should not move tau, {} vs {}".format(glitched["tau_hours"], clean["tau_hours"]))
        failed = check(failed, abs(glitched["heat_rate"] - clean["heat_rate"]) < 0.05 * clean["heat_rate"], "a glitch should not move the heat rate, {} vs {}".format(glitched["heat_rate"], clean["heat_rate"]))
    unlimited = thermal_fit_statistics(thermal_statistics(temperature, power, ambient, history_minutes, STEP), STEP)
    failed = check(failed, unlimited is None or abs(unlimited["tau_hours"] - clean["tau_hours"]) > 0.2 * clean["tau_hours"], "without a power limit the dropped reading should distort the fit (so the test means something), got {}".format(unlimited))

    # A NaN in stored sums is not valid, so it is reseeded rather than merged into every later fit
    nan_stats = dict(stats)
    nan_stats["xty"] = [float("nan"), stats["xty"][1]]
    failed = check(failed, valid_thermal_statistics(stats) and not valid_thermal_statistics(nan_stats), "stored sums holding a NaN should not be valid")
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
    # Some power, so the equations are solvable and the rejection comes from the negative a rather than a singular matrix
    some_power = {minute: (1.0 if (minute // 60) % 3 == 0 else 0.0) for minute in range(0, 7 * 24 * 60 + 1, STEP)}
    failed = check(failed, solve_linear(*fit_equations(anti, some_power, flat_ambient)) is not None, "the implausible history should give solvable equations")
    failed = check(failed, thermal_fit(anti, some_power, flat_ambient, 7 * 24 * 60, STEP) is None, "an implausible history should not fit")

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
    held = thermal_forecast(10.0, {"tau_hours": 1.0, "heat_rate": 0.0}, {0: 0.0}, {}, 24 * 60, STEP)
    failed = check(failed, held[24 * 60 - STEP] < 0.1, "forecast should approach the held ambient, got {}".format(held[24 * 60 - STEP]))

    # Before the outdoor forecast starts the battery holds its temperature, then it follows the outdoor temperature
    late = thermal_forecast(10.0, {"tau_hours": 1.0, "heat_rate": 0.0}, {60: 0.0}, {}, 6 * 60, STEP)
    failed = check(failed, late[60] == 10.0 and late[6 * 60 - STEP] < 1.0, "forecast should hold until the outdoor forecast starts, then follow it, got {} {}".format(late[60], late[6 * 60 - STEP]))
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


TEMP_SENSOR = "sensor.battery_temperature_thermal_test"
TEMP_SENSOR_2 = "sensor.battery_temperature_thermal_test_2"


def records_from_minutes(series, now_utc, unit=None):
    """HA-style history records, oldest first, from a series keyed by minutes ago."""
    attributes = {"unit_of_measurement": unit} if unit else {}
    return [[{"state": str(series[minute]), "last_updated": (now_utc - timedelta(minutes=minute)).strftime(TIME_FORMAT), "attributes": attributes} for minute in sorted(series, reverse=True)]]


def test_fetch_integration(my_predbat):
    """
    battery_thermal_update() seeds the model's sums from a week of history, adds each cycle's new step, saves the sums once
    the store is ready and reads them back after a restart. The thermal forecast is fitted and logged each cycle whether or
    not battery_temperature_outdoor is on (shadow mode); only with it on does it replace the look back. It follows the
    outdoor temperature and the previous best plan, and without an outdoor temperature nothing is read or fitted.
    """
    failed = False
    days = BATTERY_THERMAL_FIT_DAYS
    start_utc = my_predbat.now_utc - timedelta(days=days)
    temperature_abs, soc_abs = simulate_absolute(MODEL, days + 1)
    clock = {"now": days * 24 * 60}  # minutes from the start of the simulation
    # The outdoor temperature sensor, hourly, from the start of the simulation to 2 days past its end
    results = {(start_utc + timedelta(hours=hour)).strftime(TIME_FORMAT): round(ambient_at(hour * 60), 3) for hour in range(0, (days + 3) * 24)}
    state = {"results": results, "temperature_history": True}
    reads = []
    logged = []
    thermal_lines = []

    def set_clock(minute_absolute):
        """Move Predbat's now and its in-memory histories to minute_absolute of the simulation."""
        clock["now"] = minute_absolute
        my_predbat.now_utc = start_utc + timedelta(minutes=minute_absolute)
        my_predbat.soc_kwh_history = fill_minutes(minutes_ago(soc_abs, minute_absolute, BATTERY_TEMPERATURE_HISTORY_DAYS * 24 * 60))

    def fill_minutes(series):
        """Step-hold a 5-minute series onto every minute, as minute_data does."""
        return {minute: series[minute - minute % STEP] for minute in range(0, max(series) + 1) if (minute - minute % STEP) in series}

    def fake_history(entity_id, days=30, required=True, tracked=True):
        """Serve the simulated temperature and battery level history up to the clock, recording each read."""
        reads.append((entity_id, days, tracked))
        if entity_id == TEMP_SENSOR:
            if not state["temperature_history"]:
                return None
            return records_from_minutes({m: round(v, 2) for m, v in minutes_ago(temperature_abs, clock["now"], days * 24 * 60).items()}, my_predbat.now_utc)
        if entity_id == TEMP_SENSOR_2:
            return records_from_minutes({m: round(v + 2.0, 2) for m, v in minutes_ago(temperature_abs, clock["now"], days * 24 * 60).items()}, my_predbat.now_utc)
        if entity_id == my_predbat.prefix + ".soc_kw_h0":
            return records_from_minutes({m: round(v, 4) for m, v in minutes_ago(soc_abs, clock["now"], days * 24 * 60).items()}, my_predbat.now_utc, unit="kWh")
        return None

    saved_history = my_predbat.get_history_wrapper
    saved_state = my_predbat.get_state_wrapper
    saved_log = my_predbat.log

    def fake_log(text, *args, **kwargs):
        """Keep the forecast log lines, pass everything to the real log."""
        if text.startswith("Battery temperature forecast:"):
            logged.append(text)
        if text.startswith("Battery thermal") or text.startswith("Battery temperature forecast:"):
            thermal_lines.append(text)
        return saved_log(text, *args, **kwargs)

    def fake_state(entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Serve the outdoor temperature results; everything else from the real wrapper."""
        if entity_id == "sensor." + my_predbat.prefix + "_temperature" and attribute == "results":
            return state["results"]
        return saved_state(entity_id, default=default, attribute=attribute, refresh=refresh, required_unit=required_unit, raw=raw)

    fields = [
        "battery_temperature_outdoor",
        "battery_thermal_model",
        "battery_thermal_stats",
        "battery_thermal_last",
        "battery_thermal_loaded",
        "battery_thermal_load_failures",
        "battery_thermal_last_seed",
        "battery_thermal_seed_attempts",
        "battery_thermal_restored",
        "battery_thermal_last_save",
        "battery_thermal_dirty",
        "battery_power_plan_best",
        "battery_rate_max_charge",
        "battery_rate_max_discharge",
        "max_days_previous",
        "now_utc",
        "soc_kwh_history",
        "components",
    ]
    saved = {field: getattr(my_predbat, field) for field in fields}
    saved_arg = my_predbat.args.get("battery_temperature_history", None)

    def restart(storage):
        """A fresh start: nothing in memory, the given storage."""
        my_predbat.components = MockStorageComponents(storage)
        my_predbat.battery_thermal_model = None
        my_predbat.battery_thermal_stats = None
        my_predbat.battery_thermal_last = None
        my_predbat.battery_thermal_loaded = False
        my_predbat.battery_thermal_load_failures = []
        my_predbat.battery_thermal_last_seed = None
        my_predbat.battery_thermal_seed_attempts = 0
        my_predbat.battery_thermal_restored = False
        my_predbat.battery_thermal_last_save = None
        my_predbat.battery_thermal_dirty = False
        my_predbat.battery_power_plan_best = {}

    def cycle(soc_fresh=True):
        """One Predbat cycle's share: the temperature forecast, then the model update (as in fetch_sensor_data)."""
        my_predbat.fetch_battery_temperature()
        my_predbat.battery_thermal_update(soc_fresh=soc_fresh)

    tmpdir = tempfile.mkdtemp()
    my_predbat.get_history_wrapper = fake_history
    my_predbat.get_state_wrapper = fake_state
    my_predbat.log = fake_log
    my_predbat.args["battery_temperature_history"] = TEMP_SENSOR
    my_predbat.max_days_previous = 2
    # A 6 kW battery (rates are kW per minute), so the simulated 5 kW charges are within the glitch limit
    my_predbat.battery_rate_max_charge = 6.0 / 60.0
    my_predbat.battery_rate_max_discharge = 6.0 / 60.0
    try:
        storage = make_test_storage(my_predbat, tmpdir + "/a")
        restart(storage)
        set_clock(days * 24 * 60)

        # Off (the default) with an empty store: seeded from a week of history read untracked and fitted at once (shadow mode),
        # not saved until the store is ready; the plan keeps the look back
        my_predbat.battery_temperature_outdoor = False
        cycle()
        failed = check(failed, logged and "using history" in logged[-1] and "thermal none (not fitted)" in logged[-1], "the first cycle should log the look back with no fit yet, got {}".format(logged[-1:]))
        model = my_predbat.battery_thermal_model
        failed = check(failed, model is not None, "off should still fit from the seed")
        if model:
            failed = check(failed, abs(model["tau_hours"] - MODEL["tau_hours"]) / MODEL["tau_hours"] < 0.1, "fitted tau {} expected about {}".format(model["tau_hours"], MODEL["tau_hours"]))
            failed = check(failed, abs(model["heat_rate"] - MODEL["heat_rate"]) / MODEL["heat_rate"] < 0.1, "fitted heat rate {} expected about {}".format(model["heat_rate"], MODEL["heat_rate"]))
        failed = check(failed, (TEMP_SENSOR, days, False) in reads and (my_predbat.prefix + ".soc_kw_h0", days, False) in reads, "seed should read {} days untracked, read {}".format(days, reads))
        failed = check(failed, run_async(storage.load("predbat", BATTERY_THERMAL_STORE_KEY)) is None, "an empty store should not be saved over on the first cycle")
        seeded = my_predbat.battery_thermal_stats["samples"]

        # Off and fitted: the plan uses the look back, the thermal forecast is published as attributes and logged hourly
        set_clock(days * 24 * 60 + STEP)
        reads.clear()
        cycle()
        lookback = my_predbat.predict_battery_temperature(my_predbat.battery_temperature_history, step=PREDICT_STEP)
        failed = check(failed, my_predbat.battery_temperature_prediction == lookback, "off should give the look back forecast")
        failed = check(failed, saved_state(my_predbat.prefix + ".battery_temperature", attribute="model") == "history", "off should publish model history")
        failed = check(failed, saved_state(my_predbat.prefix + ".battery_temperature", attribute="thermal_tau_hours") is not None, "off should publish the fit as attributes")
        line = logged[-1] if logged else ""
        failed = check(failed, "using history" in line and "thermal [" in line and "outdoor [" in line and "planned power [" in line, "off should log both forecasts, got {}".format(line))
        history_part = line.split("history [")[1].split("]")[0].split(", ") if "history [" in line else []
        failed = check(failed, len(history_part) == 24, "the log should give 24 hourly values, got {}".format(len(history_part)))

        # On: the thermal forecast replaces the look back
        my_predbat.battery_temperature_outdoor = True

        # The next cycles forecast with the model and add one step each; the save waits for 3 empty reads over 10 minutes
        for k in range(2, 5):
            set_clock(days * 24 * 60 + k * STEP)
            reads.clear()
            cycle()
            failed = check(failed, all(read[1] != days for read in reads), "cycle {} should not re-read the fit history, read {}".format(k, reads))
        failed = check(failed, my_predbat.battery_thermal_stats["samples"] == seeded + 4, "4 cycles should add 4 steps, {} vs {}".format(my_predbat.battery_thermal_stats["samples"], seeded))
        failed = check(failed, my_predbat.battery_temperature_prediction != lookback, "on should forecast with the model")
        failed = check(failed, saved_state(my_predbat.prefix + ".battery_temperature", attribute="model") == "thermal", "on should publish model thermal")
        failed = check(failed, logged and "using thermal" in logged[-1], "on should log the thermal forecast in use")
        # The log page files any line containing "error" under Errors, so the model's routine lines must not say it
        failed = check(failed, any("fitted" in line for line in thermal_lines), "the fit should be logged, got {}".format(thermal_lines[:3]))
        misfiled = [line for line in thermal_lines if classify_log_line(line) != "log"]
        failed = check(failed, not misfiled, "routine thermal log lines should not be classed as errors or warnings, got {}".format(misfiled[:2]))
        stored = run_async(storage.load("predbat", BATTERY_THERMAL_STORE_KEY))
        # Ready on the third empty read (10 minutes after the first), so saved with 2 steps added; the next save is an hour later
        failed = check(failed, isinstance(stored, dict) and stored.get("stats", {}).get("samples") == seeded + 2, "the sums should be saved once the store is ready, got {}".format(stored and stored.get("stats", {}).get("samples")))

        # A stale battery level history adds nothing
        set_clock(days * 24 * 60 + 5 * STEP)
        last = my_predbat.battery_thermal_last
        cycle(soc_fresh=False)
        failed = check(failed, my_predbat.battery_thermal_stats["samples"] == seeded + 4 and my_predbat.battery_thermal_last == last, "a stale battery level history should add nothing")

        # A plan that charges at 5 kW for the next 3 hours warms the forecast
        idle = dict(my_predbat.battery_temperature_prediction)
        my_predbat.battery_power_plan_best = {(my_predbat.now_utc + timedelta(minutes=m)).strftime(TIME_FORMAT): (-5.0 if m < 180 else 0.0) for m in range(0, 24 * 60, PREDICT_STEP)}
        my_predbat.fetch_battery_temperature()
        charging = my_predbat.battery_temperature_prediction
        failed = check(failed, charging[180] - idle[180] > 3.0, "a 3 hour charge should warm the forecast, {} vs idle {}".format(charging.get(180), idle.get(180)))

        # A restart reads the sums back (no seed read) and picks up the steps since they were saved, decayed by age
        restart(storage)
        set_clock(days * 24 * 60 + 6 * STEP)
        reads.clear()
        # The first forecast after the restart runs before the update, so it fits from the store rather than logging no fit
        my_predbat.fetch_battery_temperature()
        failed = check(failed, logged and "thermal [" in logged[-1], "the first forecast after a restart should use the stored fit, got {}".format(logged[-1:]))
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_loaded and all(read[1] != days for read in reads), "a restart should read the store, not the history, read {}".format(reads))
        # Saved at +10 minutes, restarted at +30: the battery level history is Predbat's own and reads flat while it was down,
        # so the 4 steps between are skipped and the stored evidence only decays by age
        failed = check(
            failed, my_predbat.battery_thermal_stats["samples"] == seeded + 2 and my_predbat.battery_thermal_model is not None, "a restart should skip the steps since the save, got {}".format(my_predbat.battery_thermal_stats["samples"] - seeded)
        )
        decay = 0.5 ** ((4 * STEP) / (24 * 60) / BATTERY_THERMAL_HALF_LIFE_DAYS)
        failed = check(failed, abs(my_predbat.battery_thermal_stats["weight"] - stored["stats"]["weight"] * decay) < 1e-6, "the stored evidence should decay by age")
        failed = check(failed, stored.get("sensors") == [TEMP_SENSOR], "the store should record the sensors it was fitted on, got {}".format(stored.get("sensors")))
        # The cycle after adds its new steps again
        set_clock(days * 24 * 60 + 7 * STEP)
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_stats["samples"] == seeded + 3, "the cycle after a restart should add its step, got {}".format(my_predbat.battery_thermal_stats["samples"] - seeded))

        # Changed sensors (now two, one per inverter): the stored evidence is for the old one, so the model is seeded again,
        # reading both sensors and averaging them as the live history does
        restart(storage)
        my_predbat.args["battery_temperature_history"] = [TEMP_SENSOR, TEMP_SENSOR_2]
        reads.clear()
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, (TEMP_SENSOR, days, False) in reads and (TEMP_SENSOR_2, days, False) in reads, "changed sensors should seed again from every sensor, read {}".format(reads))
        failed = check(failed, my_predbat.battery_thermal_model is not None, "two sensors should still fit")
        my_predbat.args["battery_temperature_history"] = TEMP_SENSOR

        # No outdoor temperature on a new install: nothing is read or fitted, and the look back is used
        restart(make_test_storage(my_predbat, tmpdir + "/b"))
        state["results"] = None
        reads.clear()
        cycle()
        failed = check(failed, my_predbat.battery_thermal_stats is None and my_predbat.battery_thermal_model is None, "no outdoor temperature should not seed")
        failed = check(failed, all(read[1] != days for read in reads), "no outdoor temperature should not read the fit history, read {}".format(reads))
        failed = check(failed, saved_state(my_predbat.prefix + ".battery_temperature", attribute="model") == "history", "no model should publish model history")

        # The outdoor temperature arrives but the battery temperature history is empty: the seed is retried within the hour
        state["results"] = results
        state["temperature_history"] = False
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_stats is None, "an empty history should not seed")
        state["temperature_history"] = True
        set_clock(clock["now"] + BATTERY_THERMAL_RETRY_MINUTES - STEP)
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_stats is None, "the seed should not be retried before {} minutes".format(BATTERY_THERMAL_RETRY_MINUTES))
        set_clock(clock["now"] + STEP)
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_model is not None, "the seed should be retried after {} minutes".format(BATTERY_THERMAL_RETRY_MINUTES))

        # Seeds that keep finding nothing back off: after two empty seeds the next waits twice as long
        restart(make_test_storage(my_predbat, tmpdir + "/d"))
        state["temperature_history"] = False
        my_predbat.battery_thermal_update(soc_fresh=True)
        set_clock(clock["now"] + BATTERY_THERMAL_RETRY_MINUTES)
        my_predbat.battery_thermal_update(soc_fresh=True)
        state["temperature_history"] = True
        set_clock(clock["now"] + BATTERY_THERMAL_RETRY_MINUTES)
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_stats is None and my_predbat.battery_thermal_seed_attempts == 2, "after two empty seeds the third should wait {} minutes".format(2 * BATTERY_THERMAL_RETRY_MINUTES))
        set_clock(clock["now"] + BATTERY_THERMAL_RETRY_MINUTES)
        my_predbat.battery_thermal_update(soc_fresh=True)
        failed = check(failed, my_predbat.battery_thermal_stats is not None and my_predbat.battery_thermal_seed_attempts == 0, "the backed-off seed should run and reset the count")
        # A fault in the model is a warning: the update carries on without it, the forecast falls back to the look back
        warned = []
        saved_refresh, saved_forecast = my_predbat.battery_thermal_refresh, my_predbat.battery_thermal_forecast

        def boom(*args, **kwargs):
            """Fail as an unexpected bug would."""
            raise ZeroDivisionError("test fault")

        def warn_log(text, *args, **kwargs):
            """Keep the warnings."""
            if text.startswith("Warn:"):
                warned.append(text)
            return fake_log(text, *args, **kwargs)

        my_predbat.log = warn_log
        my_predbat.battery_thermal_refresh = boom
        my_predbat.battery_thermal_forecast = boom
        try:
            my_predbat.battery_thermal_update(soc_fresh=True)
            prediction = my_predbat.predict_battery_temperature(my_predbat.battery_temperature_history, step=PREDICT_STEP)
        finally:
            my_predbat.battery_thermal_refresh, my_predbat.battery_thermal_forecast = saved_refresh, saved_forecast
            my_predbat.log = fake_log
        failed = check(failed, any("update failed" in text for text in warned) and any("forecast failed" in text for text in warned), "faults should be logged as warnings, got {}".format(warned))
        failed = check(failed, prediction and saved_state(my_predbat.prefix + ".battery_temperature", attribute="model") == "history", "a forecast fault should fall back to the look back")
        failed = check(failed, my_predbat.battery_temperature_thermal == {}, "a forecast fault should leave no thermal forecast for the plan column")
    finally:
        my_predbat.get_history_wrapper = saved_history
        my_predbat.get_state_wrapper = saved_state
        my_predbat.log = saved_log
        for field, value in saved.items():
            setattr(my_predbat, field, value)
        if saved_arg is None:
            my_predbat.args.pop("battery_temperature_history", None)
        else:
            my_predbat.args["battery_temperature_history"] = saved_arg
    return failed


def test_statistics_merge():
    """Statistics of two halves merge to those of the whole; decay scales the older half; bad stored statistics are ignored."""
    failed = False
    temperature, soc_kwh, ambient = simulate_history(MODEL, days=7)
    history_minutes = 7 * 24 * 60
    power = battery_power_from_soc(soc_kwh, history_minutes, STEP)
    whole = thermal_statistics(temperature, power, ambient, history_minutes, STEP)
    older = thermal_statistics(temperature, power, ambient, history_minutes, STEP, newest_minute=3 * 24 * 60 + STEP)
    newer = thermal_statistics(temperature, power, ambient, 3 * 24 * 60, STEP)
    merged = thermal_statistics_merge(older, newer, 1.0)
    failed = check(failed, merged["samples"] == whole["samples"] and abs(merged["weight"] - whole["weight"]) < 1e-9, "merged halves should count as the whole")
    failed = check(failed, all(abs(merged["xtx"][i][j] - whole["xtx"][i][j]) < 1e-6 for i in range(2) for j in range(2)), "merged xtx should equal the whole")
    fit = thermal_fit_statistics(merged, STEP)
    failed = check(failed, fit is not None and abs(fit["tau_hours"] - MODEL["tau_hours"]) < 0.05 and fit["rmse"] < 0.001, "merged statistics should fit the model, got {}".format(fit))
    halved = thermal_statistics_merge(older, newer, 0.5)
    failed = check(failed, abs(halved["weight"] - (older["weight"] * 0.5 + newer["weight"])) < 1e-9, "decay should scale the stored weight")
    fresh = thermal_statistics_merge({"xtx": "bad"}, newer, 0.5)
    failed = check(failed, fresh["weight"] == newer["weight"] and fresh["xtx"] == newer["xtx"] and fresh["xtx"] is not newer["xtx"], "invalid stored statistics should be replaced by a copy of the new")
    failed = check(failed, thermal_fit_statistics({"xtx": None}, STEP) is None, "invalid statistics should not fit")
    for size in (3, 4):
        old_shape = {"xtx": [[1.0] * size for _ in range(size)], "xty": [0.0] * size, "yty": 0.0, "weight": 1000.0, "samples": 1000}
        failed = check(failed, thermal_fit_statistics(old_shape, STEP) is None, "statistics of an older {} parameter shape should not fit".format(size))
    return failed


def run_battery_thermal_tests(my_predbat=None):
    """Run the battery thermal model tests; returns True on failure."""
    failed = False
    failed |= test_solve_linear()
    failed |= test_fit_recovers_model()
    failed |= test_fit_rejects_short_or_implausible()
    failed |= test_fit_ignores_glitches()
    failed |= test_fit_and_forecast_edges()
    failed |= test_forecast_matches_simulation()
    failed |= test_ambient_from_results()
    failed |= test_plan_power_minutes()
    failed |= test_battery_power_from_soc()
    failed |= test_statistics_merge()
    if my_predbat is not None:
        failed |= test_fetch_integration(my_predbat)
    return failed
