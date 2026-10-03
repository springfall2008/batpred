# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the replay-input log lines (Fetch.log_replay_*)."""

import re

SAVED = ("pv_forecast_minute", "pv_forecast_minute10", "pv_forecast_minute90", "load_forecast", "minutes_now", "forecast_minutes")


def capture(my_predbat, function):
    """Run function with my_predbat.log captured, returning the lines it logged."""
    lines = []
    my_predbat.log = lambda message, *args, **kwargs: lines.append(str(message))
    try:
        function()
    finally:
        del my_predbat.log
    return lines


def numbers(text):
    """The first bracketed list of numbers in text."""
    return [float(value) for value in re.search(r"\[([^\]]*)\]", text).group(1).split(",")]


def test_pv_forecast_logged_on_change(my_predbat):
    """The PV forecast is logged as half-hourly kWh when it changes, and not again while it stays the same."""
    my_predbat.minutes_now = 9 * 60 + 10
    my_predbat.pv_forecast_minute = {minute: 0.05 for minute in range(9 * 60, 10 * 60)}
    my_predbat.pv_forecast_minute10 = {minute: 0.02 for minute in range(9 * 60, 10 * 60)}
    my_predbat.pv_forecast_minute90 = {}
    my_predbat.replay_pv_signature = None
    lines = capture(my_predbat, my_predbat.log_replay_pv_forecast)
    if len(lines) != 1 or "PV forecast changed, 30-minute kWh from 09:00" not in lines[0]:
        print("ERROR: expected one PV forecast line from 09:00, got {}".format(lines))
        return 1
    p50 = numbers(lines[0].split("p50", 1)[1])
    if len(p50) != 96 or p50[:3] != [1.5, 1.5, 0.0]:
        print("ERROR: p50 half hours wrong: {}".format(p50[:3]))
        return 1
    if capture(my_predbat, my_predbat.log_replay_pv_forecast):
        print("ERROR: an unchanged forecast should not be logged again")
        return 1
    my_predbat.pv_forecast_minute = {minute: 0.06 for minute in range(9 * 60, 10 * 60)}
    if len(capture(my_predbat, my_predbat.log_replay_pv_forecast)) != 1:
        print("ERROR: a changed forecast should be logged")
        return 1
    return 0


def test_load_forecast_logged(my_predbat):
    """The load forecast is logged as Wh per 5 minutes across the plan, from the cumulative-from-midnight series."""
    my_predbat.minutes_now = 9 * 60 + 7
    my_predbat.forecast_minutes = 20
    # 0.1 kWh per 5 minutes, cumulative from midnight
    my_predbat.load_forecast = {minute: minute / 50.0 for minute in range(0, 24 * 60, 5)}
    lines = capture(my_predbat, my_predbat.log_replay_load_forecast)
    if len(lines) != 1 or "load forecast, 5-minute Wh from 09:05" not in lines[0]:
        print("ERROR: expected one load forecast line from 09:05, got {}".format(lines))
        return 1
    if numbers(lines[0]) != [100.0, 100.0, 100.0, 100.0, 100.0]:
        print("ERROR: load forecast slots wrong: {}".format(numbers(lines[0])))
        return 1
    my_predbat.load_forecast = {}
    if capture(my_predbat, my_predbat.log_replay_load_forecast):
        print("ERROR: no load forecast should log nothing")
        return 1
    return 0


def test_raw_load_logged(my_predbat):
    """The raw load counter and power for the last 10 minutes are logged newest first."""
    counter = {minute: 10.0 - minute * 0.01 for minute in range(20)}
    power = {minute: 600.0 for minute in range(20)}
    lines = capture(my_predbat, lambda: my_predbat.log_replay_raw_load(counter, power))
    if len(lines) != 1 or "raw load, newest first" not in lines[0]:
        print("ERROR: expected one raw load line, got {}".format(lines))
        return 1
    if numbers(lines[0])[:2] != [10.0, 9.99] or numbers(lines[0].split("load_power", 1)[1]) != [600.0] * 10:
        print("ERROR: raw load values wrong: {}".format(lines[0]))
        return 1
    lines = capture(my_predbat, lambda: my_predbat.log_replay_raw_load(counter, None))
    if "load_power W []" not in lines[0]:
        print("ERROR: no power data should log an empty power list: {}".format(lines))
        return 1
    lines = capture(my_predbat, lambda: my_predbat.log_replay_raw_load(None, None))
    if not lines or not lines[0].startswith("Warn:"):
        print("ERROR: bad input should warn, not raise: {}".format(lines))
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
    results["2026-10-01T10:15:00+0000"] = 2.5
    lines = capture(my_predbat, lambda: my_predbat.log_replay_ml_forecast(results))
    if len(lines) != 1 or "every" in lines[0] or "10:15:00" not in lines[0]:
        print("ERROR: uneven ML predictions should be logged in full, got {}".format(lines))
        return 1
    return 0


def test_logging_never_raises(my_predbat):
    """A malformed forecast is reported as a warning rather than raised into the main loop."""
    my_predbat.pv_forecast_minute = {"bad": "data"}
    my_predbat.replay_pv_signature = None
    my_predbat.minutes_now = 9 * 60 + 5
    my_predbat.forecast_minutes = 20
    my_predbat.load_forecast = {9 * 60 + 5: "bad"}
    lines = capture(my_predbat, my_predbat.log_replay_pv_forecast) + capture(my_predbat, my_predbat.log_replay_load_forecast)
    if len(lines) != 2 or not all(line.startswith("Warn:") for line in lines):
        print("ERROR: expected two warnings, got {}".format(lines))
        return 1
    return 0


def run_log_replay_inputs_tests(my_predbat):
    """Run the replay-input logging tests, restoring the shared fixture afterwards."""
    missing = object()
    saved = {key: getattr(my_predbat, key, missing) for key in SAVED + ("replay_pv_signature", "replay_ml_signature")}
    failed = 0
    try:
        failed += test_pv_forecast_logged_on_change(my_predbat)
        failed += test_load_forecast_logged(my_predbat)
        failed += test_raw_load_logged(my_predbat)
        failed += test_ml_forecast_logged_on_change(my_predbat)
        failed += test_logging_never_raises(my_predbat)
    finally:
        for key, value in saved.items():
            if value is missing:
                if key in my_predbat.__dict__:
                    del my_predbat.__dict__[key]
            else:
                setattr(my_predbat, key, value)
    return failed
