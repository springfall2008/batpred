# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the replay-input log lines (Fetch.log_replay_*)."""

import ast
import re

SAVED = (
    "pv_light_dark",
    "pv_forecast_minute",
    "pv_forecast_minute10",
    "pv_forecast_minute90",
    "load_forecast",
    "minutes_now",
    "forecast_minutes",
    "rate_import",
    "rate_export",
    "rate_import_base",
    "rate_export_base",
    "soc_kw",
    "soc_max",
    "load_inday_adjustment",
    "cost_today_sofar",
    "load_minutes_now",
    "import_today_now",
    "export_today_now",
    "pv_today_now",
    "charge_rate_now",
    "discharge_rate_now",
    "num_cars",
    "car_charging_planned",
    "car_charging_now",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_limit",
    "car_charging_limit_model",
    "car_charging_battery_size",
    "car_charging_slots",
    "car_energy_reported_load",
    "charge_window",
    "charge_limit",
    "export_window",
    "export_limits",
    "isCharging",
    "isCharging_Target",
    "isExporting",
    "isExporting_Target",
    "reserve",
    "reserve_current",
    "battery_rate_max_charge",
    "battery_rate_max_discharge",
    "battery_temperature",
)


def capture(my_predbat, function):
    """Run function with my_predbat.log captured, returning the lines it logged."""
    lines = []
    # Put back whatever was there: another test may have left its own log on the shared fixture
    missing = object()
    previous = my_predbat.__dict__.get("log", missing)
    my_predbat.log = lambda message, *args, **kwargs: lines.append(str(message))
    try:
        function()
    finally:
        if previous is missing:
            del my_predbat.log
        else:
            my_predbat.log = previous
    return lines


def numbers(text):
    """The first bracketed list of numbers in text."""
    return [float(value) for value in re.search(r"\[([^\]]*)\]", text).group(1).split(",")]


def test_pv_forecast_logged_on_change(my_predbat):
    """The PV forecast is logged per minute across the plan, exactly, as runs, when it changes, and not again while it stays the same."""
    my_predbat.minutes_now = 9 * 60 + 10
    my_predbat.forecast_minutes = 60
    my_predbat.pv_forecast_minute = {minute: 0.05 for minute in range(9 * 60, 10 * 60)}
    my_predbat.pv_forecast_minute10 = {minute: 0.1 / 3 for minute in range(9 * 60, 10 * 60)}
    my_predbat.pv_forecast_minute90 = {}
    my_predbat.replay_pv_signature = None
    lines = capture(my_predbat, my_predbat.log_replay_pv_forecast)
    end = 60 + my_predbat.plan_interval_minutes
    expected = "Replay input: PV forecast changed, per-minute kWh runs from 09:10 p50 [[0.05, 50], [None, {}]] p10 [[{!r}, 50], [None, {}]] p90 [[None, {}]]".format(end - 50, 0.1 / 3, end - 50, end)
    if lines != [expected]:
        print("ERROR: PV forecast line wrong:\n  got      {}\n  expected {}".format(lines, expected))
        return 1
    if capture(my_predbat, my_predbat.log_replay_pv_forecast):
        print("ERROR: an unchanged forecast should not be logged again")
        return 1
    my_predbat.pv_forecast_minute[9 * 60 + 30] = 0.0500001
    if len(capture(my_predbat, my_predbat.log_replay_pv_forecast)) != 1:
        print("ERROR: a changed forecast should be logged, however small the change")
        return 1
    # A forecast reaching past the plan is logged to its end, as the plan's horizon moves on before the next change
    my_predbat.pv_forecast_minute[13 * 60] = 0.07
    line = capture(my_predbat, my_predbat.log_replay_pv_forecast)[0]
    if not line.split(" p10 ")[0].endswith("[0.07, 1]]") or not line.endswith("[None, {}]]".format(13 * 60 + 1 - my_predbat.minutes_now)):
        print("ERROR: the line should run to the end of the forecast: {}".format(line[-120:]))
        return 1
    return 0


def test_load_forecast_logged(my_predbat):
    """The load forecast is logged as the cumulative values the plan reads: each step's start and the minute after, across the plan."""
    my_predbat.minutes_now = 9 * 60 + 5
    my_predbat.forecast_minutes = 20
    # 0.02 kWh a minute, cumulative from midnight, with the minute after 09:15 missing
    my_predbat.load_forecast = {minute: minute / 50.0 for minute in range(0, 24 * 60)}
    del my_predbat.load_forecast[9 * 60 + 16]
    lines = capture(my_predbat, my_predbat.log_replay_load_forecast)
    steps = (20 + my_predbat.plan_interval_minutes) // 5
    if len(lines) != 1 or "load forecast, cumulative kWh at each 5-minute step and the minute after from 09:05 [[10.9, 10.92], [11.0, 11.02], [11.1, None]," not in lines[0]:
        print("ERROR: expected one load forecast line from 09:05, got {}".format(lines))
        return 1
    if lines[0].count("[") - 1 != steps:
        print("ERROR: the line should cover every step the plan builds ({}), got {}".format(steps, lines[0].count("[") - 1))
        return 1
    my_predbat.load_forecast = {}
    if capture(my_predbat, my_predbat.log_replay_load_forecast):
        print("ERROR: no load forecast should log nothing")
        return 1
    return 0


def test_ml_forecast_logged_on_change(my_predbat):
    """Load ML predictions are logged compactly when evenly spaced, in full otherwise, and only when they change."""
    my_predbat.replay_ml_signature = None
    results = {"2026-10-01T09:30:00+0000": 1.5, "2026-10-01T09:00:00+0000": 1.0, "2026-10-01T10:00:00+0000": 2.25}
    lines = capture(my_predbat, lambda: my_predbat.log_replay_ml_forecast(results))
    if len(lines) != 1 or "from 2026-10-01T09:00:00+0000 every 30 minutes kWh [1.0, 1.5, 2.25]" not in lines[0]:
        print("ERROR: expected a compact ML forecast line, got {}".format(lines))
        return 1
    if capture(my_predbat, lambda: my_predbat.log_replay_ml_forecast(dict(results))):
        print("ERROR: unchanged ML predictions should not be logged again")
        return 1
    # Values are logged exactly, not rounded
    results["2026-10-01T10:00:00+0000"] = 0.1 + 0.2
    lines = capture(my_predbat, lambda: my_predbat.log_replay_ml_forecast(results))
    if len(lines) != 1 or "kWh [1.0, 1.5, {!r}]".format(0.1 + 0.2) not in lines[0]:
        print("ERROR: ML values should be logged exactly, got {}".format(lines))
        return 1
    results["2026-10-01T10:15:00+0000"] = 2.5
    lines = capture(my_predbat, lambda: my_predbat.log_replay_ml_forecast(results))
    if len(lines) != 1 or "every" in lines[0] or "10:15:00" not in lines[0]:
        print("ERROR: uneven ML predictions should be logged in full, got {}".format(lines))
        return 1
    return 0


def test_load_forecast_exact(my_predbat):
    """The logged load forecast values read back as exactly the floats the plan uses."""
    my_predbat.minutes_now = 9 * 60
    my_predbat.forecast_minutes = 10
    my_predbat.load_forecast = {minute: minute * 0.013372222222222222 / 5 for minute in range(0, 24 * 60)}
    line = capture(my_predbat, my_predbat.log_replay_load_forecast)[0]
    first = re.search(r"\[\[([^\]]*)\]", line).group(1).split(", ")
    if [float(value) for value in first] != [my_predbat.load_forecast[540], my_predbat.load_forecast[541]]:
        print("ERROR: load forecast values should read back exactly: {}".format(first))
        return 1
    return 0


def test_load_forecast_compact(my_predbat):
    """A forecast in whole tenths of a Wh is logged as Wh per step, and every value the plan reads rebuilds exactly."""
    my_predbat.minutes_now = 9 * 60 + 5
    my_predbat.forecast_minutes = 20
    # The weighted-bucket forecast's shape: dp4 cumulative kWh, linear within each step, with awkward fractions
    forecast = {}
    cumulative = 7.8331
    for step_start in range(0, 24 * 60, 5):
        energy = 0.013372222222222222 + (step_start % 35) / 7000.0
        for offset in range(5):
            forecast[step_start + offset] = round(cumulative + energy * offset / 5, 4)
        cumulative += energy
    my_predbat.load_forecast = forecast
    lines = capture(my_predbat, my_predbat.log_replay_load_forecast)
    match = re.match(r"Replay input: load from 09:05 base (-?[\d.]+) Wh, Wh/5min \[(.*)\]$", lines[0]) if len(lines) == 1 else None
    if not match:
        print("ERROR: expected one compact load line from 09:05, got {}".format(lines))
        return 1
    tenths = round(float(match.group(1)) * 10)
    minute = my_predbat.minutes_now
    pairs = match.group(2).split(", ")
    if len(pairs) != (20 + my_predbat.plan_interval_minutes) // 5:
        print("ERROR: the line should cover every step the plan builds, got {}".format(len(pairs)))
        return 1
    for pair in pairs:
        step, first = (round(float(value) * 10) for value in pair.split("/"))
        if tenths / 10000 != forecast[minute] or (tenths + first) / 10000 != forecast[minute + 1]:
            print("ERROR: the compact line should rebuild the forecast exactly at minute {}: {} {}".format(minute, tenths / 10000, forecast[minute]))
            return 1
        tenths += step
        minute += 5
    if tenths / 10000 != forecast[minute]:
        print("ERROR: the last step should end on the forecast's next value")
        return 1
    return 0


def test_rates_logged_on_change(my_predbat):
    """Rates are logged from now as change points and their end, at full precision, and only when they change."""
    my_predbat.minutes_now = 9 * 60
    my_predbat.replay_rates_signature = None
    my_predbat.rate_import = {minute: 7.62 if minute < 10 * 60 else 31.1625 for minute in range(-60, 24 * 60)}
    my_predbat.rate_export = {minute: 15.0 for minute in range(0, 24 * 60)}
    my_predbat.rate_import_base = dict(my_predbat.rate_import)
    my_predbat.rate_export_base = {}
    lines = capture(my_predbat, my_predbat.log_replay_rates)
    expected = "Replay input: rates changed, from 09:00 import [[0, 7.62], [60, 31.1625]] 900 export [[0, 15.0]] 900 import_base [[0, 7.62], [60, 31.1625]] 900 export_base [] 0"
    if lines != [expected]:
        print("ERROR: rates line wrong:\n  got      {}\n  expected {}".format(lines, expected))
        return 1
    if capture(my_predbat, my_predbat.log_replay_rates):
        print("ERROR: unchanged rates should not be logged again")
        return 1
    my_predbat.rate_export[23 * 60] = 15.5
    if len(capture(my_predbat, my_predbat.log_replay_rates)) != 1:
        print("ERROR: a changed rate should be logged")
        return 1
    return 0


def test_state_logged(my_predbat):
    """The plan's starting state is logged so each value reads back as exactly the same float."""
    my_predbat.soc_kw = 4.104
    my_predbat.soc_max = 18.08
    my_predbat.load_inday_adjustment = 0.8347826086956521
    my_predbat.cost_today_sofar = 54.2213
    my_predbat.load_minutes_now = 3.09
    my_predbat.import_today_now = 17.05
    my_predbat.export_today_now = 7.64
    my_predbat.pv_today_now = None
    my_predbat.charge_rate_now = 25 / 60000.0
    my_predbat.discharge_rate_now = 0.09625
    my_predbat.battery_temperature = 24
    lines = capture(my_predbat, my_predbat.log_replay_state)
    expected = "Replay input: state soc_kw 4.104 soc_max 18.08 inday 0.8347826086956521 cost_today 54.2213 load_today 3.09 import_today 17.05 export_today 7.64 pv_today None charge_rate_now {!r} discharge_rate_now 0.09625 battery_temperature 24.0".format(25 / 60000.0)
    if lines != [expected]:
        print("ERROR: state line wrong:\n  got      {}\n  expected {}".format(lines, expected))
        return 1
    if float(lines[0].split("inday ")[1].split()[0]) != my_predbat.load_inday_adjustment:
        print("ERROR: the logged in-day adjustment should read back exactly")
        return 1
    return 0


def test_cars_logged_on_change(my_predbat):
    """The car state is logged as a dict that reads back exactly, only when it changes, and not at all without cars."""
    my_predbat.replay_cars_text = None
    my_predbat.num_cars = 1
    my_predbat.car_charging_planned = [True]
    my_predbat.car_charging_now = [False]
    my_predbat.car_charging_soc = [33.11]
    my_predbat.car_charging_soc_next = [33.11]
    my_predbat.car_charging_limit = [61.6]
    my_predbat.car_charging_limit_model = [9999.0]
    my_predbat.car_charging_battery_size = [77.0]
    my_predbat.car_charging_slots = [[{"start": 1290, "end": 1350, "kwh": 7.658, "average": 7.62, "cost": 58.35, "soc": 40.16, "octopus": True}]]
    my_predbat.car_energy_reported_load = False
    lines = capture(my_predbat, my_predbat.log_replay_cars)
    if len(lines) != 1 or not lines[0].startswith("Replay input: cars changed {"):
        print("ERROR: expected one cars line, got {}".format(lines))
        return 1
    cars = ast.literal_eval(lines[0].split("cars changed ", 1)[1])
    if cars["car_charging_slots"] != my_predbat.car_charging_slots or cars["car_charging_limit_model"] != [9999.0] or cars["car_charging_planned"] != [True]:
        print("ERROR: cars line does not read back: {}".format(cars))
        return 1
    if capture(my_predbat, my_predbat.log_replay_cars):
        print("ERROR: unchanged car state should not be logged again")
        return 1
    my_predbat.car_charging_soc = [33.2]
    if len(capture(my_predbat, my_predbat.log_replay_cars)) != 1:
        print("ERROR: a changed car SoC should be logged")
        return 1
    my_predbat.num_cars = 0
    my_predbat.replay_cars_text = None
    if capture(my_predbat, my_predbat.log_replay_cars):
        print("ERROR: no cars should log nothing")
        return 1
    return 0


def test_inverter_logged_on_change(my_predbat):
    """The inverter's programmed state is logged as a dict that reads back exactly, and only when it changes."""
    my_predbat.replay_inverter_text = None
    my_predbat.charge_window = [{"start": 1410, "end": 1440, "average": 0}, {"start": 2850, "end": 2880, "average": 0}]
    my_predbat.charge_limit = [18.08, 18.08]
    my_predbat.export_window = []
    my_predbat.export_limits = []
    my_predbat.isCharging = False
    my_predbat.isCharging_Target = 100
    my_predbat.isExporting = False
    my_predbat.isExporting_Target = 0
    my_predbat.reserve = 0.723
    my_predbat.reserve_current = 0.723
    my_predbat.battery_rate_max_charge = 5.5 / 60.0
    my_predbat.battery_rate_max_discharge = 5.5 / 60.0
    lines = capture(my_predbat, my_predbat.log_replay_inverter)
    if len(lines) != 1 or not lines[0].startswith("Replay input: inverter changed {"):
        print("ERROR: expected one inverter line, got {}".format(lines))
        return 1
    inverter = ast.literal_eval(lines[0].split("inverter changed ", 1)[1])
    if inverter["charge_window"] != my_predbat.charge_window or inverter["battery_rate_max_charge"] != 5.5 / 60.0 or inverter["reserve"] != 0.723:
        print("ERROR: inverter line does not read back: {}".format(inverter))
        return 1
    if capture(my_predbat, my_predbat.log_replay_inverter):
        print("ERROR: unchanged inverter state should not be logged again")
        return 1
    my_predbat.charge_window = []
    if len(capture(my_predbat, my_predbat.log_replay_inverter)) != 1:
        print("ERROR: a changed charge window should be logged")
        return 1
    return 0


def test_logging_never_raises(my_predbat):
    """A malformed forecast is reported as a warning rather than raised into the main loop."""
    my_predbat.pv_forecast_minute = {9 * 60 + 5: "bad"}
    my_predbat.replay_pv_signature = None
    my_predbat.minutes_now = 9 * 60 + 5
    my_predbat.forecast_minutes = 20
    my_predbat.load_forecast = {9 * 60 + 5: object()}
    my_predbat.rate_import = {0: "bad", "x": 1}
    my_predbat.replay_rates_signature = None
    my_predbat.soc_kw = "bad"
    my_predbat.num_cars = 1
    my_predbat.replay_cars_text = None
    my_predbat.car_charging_slots = [[{"start": object()}]]
    my_predbat.replay_inverter_text = None
    my_predbat.charge_window = [{"start": object()}]
    lines = capture(my_predbat, my_predbat.log_replay_pv_forecast) + capture(my_predbat, my_predbat.log_replay_load_forecast)
    lines += capture(my_predbat, my_predbat.log_replay_rates) + capture(my_predbat, my_predbat.log_replay_state) + capture(my_predbat, my_predbat.log_replay_cars)
    lines += capture(my_predbat, my_predbat.log_replay_inverter)
    if len(lines) != 6 or not all(line.startswith("Warn:") for line in lines):
        print("ERROR: expected six warnings, got {}".format(lines))
        return 1
    return 0


def test_unreadable_state_warns_once(my_predbat):
    """A value whose repr does not read back warns once per change, not on every cycle."""
    my_predbat.num_cars = 1
    my_predbat.replay_cars_text = None
    my_predbat.car_charging_slots = [[{"start": float("inf")}]]
    first = capture(my_predbat, my_predbat.log_replay_cars)
    second = capture(my_predbat, my_predbat.log_replay_cars)
    if len(first) != 1 or not first[0].startswith("Warn:") or second:
        print("ERROR: an unreadable car state should warn once, got {} then {}".format(first, second))
        return 1
    return 0


def test_comparison_runs_not_logged(my_predbat):
    """A tariff comparison run (save=False) logs no PV or ML replay line: its inputs are not the live plan's."""
    my_predbat.replay_pv_signature = None
    lines = capture(my_predbat, lambda: my_predbat.fetch_pv_forecast_and_dawn(save=False))
    if any(line.startswith("Replay input:") for line in lines):
        print("ERROR: a comparison run should not log the PV forecast for replay, got {}".format([line for line in lines if line.startswith("Replay input:")]))
        return 1
    if my_predbat.replay_pv_signature is not None:
        print("ERROR: a comparison run should not move the PV change signature")
        return 1
    return 0


def run_log_replay_inputs_tests(my_predbat):
    """Run the replay-input logging tests, restoring the shared fixture afterwards."""
    missing = object()
    saved = {key: getattr(my_predbat, key, missing) for key in SAVED + ("replay_pv_signature", "replay_ml_signature", "replay_rates_signature", "replay_cars_text", "replay_inverter_text")}
    failed = 0
    try:
        failed += test_pv_forecast_logged_on_change(my_predbat)
        failed += test_load_forecast_logged(my_predbat)
        failed += test_ml_forecast_logged_on_change(my_predbat)
        failed += test_load_forecast_exact(my_predbat)
        failed += test_load_forecast_compact(my_predbat)
        failed += test_rates_logged_on_change(my_predbat)
        failed += test_state_logged(my_predbat)
        failed += test_cars_logged_on_change(my_predbat)
        failed += test_inverter_logged_on_change(my_predbat)
        failed += test_logging_never_raises(my_predbat)
        failed += test_unreadable_state_warns_once(my_predbat)
        failed += test_comparison_runs_not_logged(my_predbat)
    finally:
        for key, value in saved.items():
            if value is missing:
                if key in my_predbat.__dict__:
                    del my_predbat.__dict__[key]
            else:
                setattr(my_predbat, key, value)
    return failed
