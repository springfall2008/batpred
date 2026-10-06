# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the forward replay of a Predbat log from a debug yaml (tests/replay_forward.py)."""

import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

from utils import MinuteArray
from tests.test_single_debug import apply_overrides
from tests.replay_forward import parse_windows, parse_log, shift_counter, set_export_window, summarise, first_window, plan_state_now, chart_replay, simulate_soc, install_logged_load_divergence, remove_logged_load_divergence, soc_rms_error, version_change, apply_logged_load_forecast, apply_logged_pv_forecast, parse_log, parse_override, rescan_rate_stats, counter_gain, crosses_midnight, shift_cumulative, shift_windows, roll_over_midnight, apply_run, today_values, apply_logged_rates, apply_logged_load_exact, apply_logged_pv_exact

SAMPLE_LOG = """2026-10-01 08:30:00.575570: --------------- PredBat - update at 2026-10-01 08:30:00+01:00 with clock skew 0 minutes, minutes now 510
2026-10-01 08:30:00.577646: Predbat /config/github.py repository springfall2008/batpred version v9.3.3 currently running, latest version is v9.3.3, latest beta is v9.3.3
2026-10-01 08:30:02.135467: Current data so far today: load 5.74kWh, import 17.74kWh, export 4.57kWh, PV 0.21kWh
2026-10-01 08:30:02.324401: Today's load divergence 100.0%, in-day adjustment 92.66%, damping 0.95x, yesterday 88.64% today 100.0% blend 64.58%
2026-10-01 08:30:02.269308: Today's energy total net 13.17kWh, import 17.74kWh, export 4.57kWh, cost 66.82c, import 151.37c, export -84.54c, carbon 0.0kg
2026-10-01 08:30:02.366949: Inverter 0 SoC: 14.27kWh 85%, current charge rate 9200W, current discharge rate 9660W, current battery power -40W, current battery voltage 52.0V, grid power 6W, load power 506W, PV Power 558W
2026-10-01 08:30:02.468111: PV Forecast 44.6kWh and 10% Forecast 30.6kWh; PV cloud factor 0.2
2026-10-01 08:30:02.469744: Load divergence over 8.0 hours mean 363.79W, min 254.4W, max 748.8W, std dev 73.34W, divergence 10.08%
2026-10-01 08:30:02.467699: Best export window [ 01-10 08:35:00 - 01-10 12:30:00 @ 18.5c 51.0% ]
2026-10-01 08:30:03.627292: Best export window [ 01-10 09:00:00 - 01-10 09:30:00 @ 18.5c 100.0% ]
2026-10-01 08:30:04.419814: Export windows filtered [ 01-10 09:00:00 - 01-10 12:00:00 @ 18.5c 55.0%, 01-10 13:00:00 - 01-10 13:30:00 @ 18.5c 84.0% ]
2026-10-01 08:30:05.663044: Next export window will be: 2026-10-01 09:00:00+01:00 - 2026-10-01 12:01:00+01:00 at reserve (0, 55, 1.0)
2026-10-01 08:30:05.663103: Inverter 0 Adjust force export to True, change times from 00:00:00 - 00:00:00 to 09:00:00 - 12:01:00
2026-10-01 08:30:05.700000: Inverter 0 SoC: 14.20kWh 84%, current charge rate 9200W, current discharge rate 9660W, current battery power -40W, current battery voltage 52.0V, grid power 6W, load power 506W, PV Power 558W
2026-10-01 08:35:00.575570: --------------- PredBat - update at 2026-10-01 08:35:00+01:00 with clock skew 0 minutes, minutes now 515
2026-10-01 08:35:02.366949: Inverter 0 SoC: 13.92kWh 83%, current charge rate 9200W, current discharge rate 9660W, current battery power 4930W, current battery voltage 52.0V, grid power 6W, load power 412W, PV Power 570W
2026-10-01 08:40:00.575570: --------------- PredBat - update at 2026-10-01 08:40:00+01:00 with clock skew 0 minutes, minutes now 520
2026-10-01 08:40:01.000000: nothing the replay needs, so this run is dropped
"""


class FakeBat:
    """The few attributes set_export_window touches."""

    def __init__(self, rate_export=None):
        """Create the stand-in."""
        self.rate_export = rate_export
        self.export_window = None
        self.isExporting = None


def test_parse_windows():
    """A window_as_text string parses back into start, end, rate and percent."""
    windows = parse_windows("[ 01-10 09:00:00 - 01-10 12:00:00 @ 18.5c 55.0%, 01-10 23:55:00 - 02-10 00:00:00 @ 8.43c 0.0%, 02-10 06:00:00 - 02-10 07:00:00 @ 16.5c 60.0% ]", "01-10")
    if windows != [(540, 720, 18.5, 55.0), (1435, 1440, 8.43, 0.0), (1800, 1860, 16.5, 60.0)]:
        print("ERROR: parse_windows gave {}".format(windows))
        return 1
    if first_window(windows[2:]) != "06:00+1d-07:00+1d @60%":
        print("ERROR: a window tomorrow should be labelled as tomorrow: {}".format(first_window(windows[2:])))
        return 1
    if parse_windows("[  ]") != [] or first_window([]) != "-":
        print("ERROR: an empty window list should parse to nothing")
        return 1
    return 0


def test_parse_log():
    """Each update line starts a run, a run keeps what it logged, and one with no SoC is dropped."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(SAMPLE_LOG)
        runs = parse_log(path)
    if [run["minutes_now"] for run in runs] != [510, 515]:
        print("ERROR: expected runs at 510 and 515, got {}".format([run["minutes_now"] for run in runs]))
        return 1
    first, second = runs
    failed = 0
    if first["soc"] != ("14.27", "85", "-40") or first["today"] != ("5.74", "17.74", "4.57", "0.21"):
        print("ERROR: SoC or day counters parsed wrongly: {} {}".format(first["soc"], first["today"]))
        failed = 1
    if first["version"] != "v9.3.3":
        print("ERROR: version parsed wrongly: {}".format(first["version"]))
        failed = 1
    if first["cost"] != "66.82":
        print("ERROR: cost so far today parsed wrongly: {}".format(first["cost"]))
        failed = 1
    if first["inday"] != "92.66" or first["divergence"] != "10.08" or first["force"] != ("True", "09", "00", "12", "01"):
        print("ERROR: in-day adjustment, load divergence or force export parsed wrongly: {} {} {}".format(first["inday"], first["divergence"], first["force"]))
        failed = 1
    if parse_windows(first["filtered"], "01-10") != [(540, 720, 18.5, 55.0), (780, 810, 18.5, 84.0)]:
        print("ERROR: filtered windows parsed wrongly: {}".format(first["filtered"]))
        failed = 1
    if parse_windows(first["in_force"], "01-10") != [(515, 750, 18.5, 51.0)]:
        print("ERROR: the plan in force should be the run's first Best export window line: {}".format(first["in_force"]))
        failed = 1
    if first["next_limit"] != ("0", "55", "1.0"):
        print("ERROR: next export limit parsed wrongly: {}".format(first["next_limit"]))
        failed = 1
    if second["filtered"] is not None or second["force"] is not None:
        print("ERROR: a run that logged no re-plan should carry no windows")
        failed = 1
    return failed


def test_shift_counter():
    """Advancing a backwards cumulative history ramps the gap up to the new counter and ages the rest."""
    history = MinuteArray({0: 10.0, 1: 9.0, 2: 8.0}, 3)
    shifted = shift_counter(history, 2, 4.0)
    expected = [14.0, 12.0, 10.0, 9.0, 8.0]
    got = [shifted.get(key) for key in range(5)]
    if got != expected or len(shifted) != 5:
        print("ERROR: shift_counter gave {} expected {}".format(got, expected))
        return 1
    sparse = shift_counter({0: 3.0, 5: 1.0}, 2, 1.0)
    if sparse != {0: 4.0, 1: 3.5, 2: 3.0, 7: 1.0}:
        print("ERROR: a sparse dict history should shift its keys: {}".format(sparse))
        return 1
    if shift_counter(history, 0, 4.0) is not history:
        print("ERROR: a zero-minute shift should leave the history alone")
        return 1
    return 0


def test_set_export_window():
    """The log's force export line becomes the inverter's window, and a False clears it."""
    bat = FakeBat(rate_export={540: 18.5})
    set_export_window(bat, ("True", "09", "00", "12", "01"), 545, ("0", "55", "1.0"))
    if bat.export_window != [{"start": 540, "end": 721, "average": 18.5}] or not bat.isExporting or bat.export_limits != [(0, 55, 1.0)]:
        print("ERROR: window not set from the force export line: {} {}".format(bat.export_window, bat.isExporting))
        return 1
    set_export_window(bat, ("True", "09", "00", "12", "01"), 500)
    if bat.isExporting:
        print("ERROR: a window that has not started should not be exporting")
        return 1
    set_export_window(bat, ("True", "23", "00", "01", "00"), 1400)
    if bat.export_window[0]["end"] != 25 * 60:
        print("ERROR: a window crossing midnight should end the next day: {}".format(bat.export_window))
        return 1
    set_export_window(bat, ("False", "00", "00", "00", "00"), 545)
    if bat.export_window != [] or bat.isExporting or bat.export_limits != []:
        print("ERROR: force export False should clear the window")
        return 1
    return 0


def test_summarise():
    """Re-plans are counted as identical or as agreeing on the first window's start."""
    same = [(540, 720, 18.5, 55.0)]
    rows = [
        {"replanned": True, "logged": same, "replayed": same},
        {"replanned": True, "logged": same, "replayed": [(540, 720, 18.5, 60.0)]},
        {"replanned": True, "logged": same, "replayed": [(600, 720, 18.5, 55.0)]},
        {"replanned": False, "logged": None, "replayed": None},
    ]
    if summarise(rows) != (3, 1, 2):
        print("ERROR: summarise gave {} expected (3, 1, 2)".format(summarise(rows)))
        return 1
    rows.append({"replanned": True, "logged": same, "replayed": same, "after_version_change": True})
    if summarise(rows) != (3, 1, 2) or summarise(rows, after_version_change=True) != (1, 1, 1):
        print("ERROR: rows after a version change should be counted separately: {} {}".format(summarise(rows), summarise(rows, after_version_change=True)))
        return 1
    return 0


def test_plan_state_now():
    """The plan's state at a minute, by the web plan's rules: export instructions first, then charge, then a car slot."""
    exports = [(540, 600, 18.5, 40.0), (600, 660, 18.5, 99.0), (660, 720, 18.5, 100.0), (720, 780, 18.5, 90.0), (1800, 1860, 18.5, 30.0)]
    charges = [(0, 60, 7.6, 100.0), (60, 120, 7.6, 4.0), (120, 180, 7.6, 50.0), (180, 240, 7.6, 0.0), (660, 690, 7.6, 100.0)]
    car = [(240, 300)]
    # SoC 60%, reserve 4%; the last export window is tomorrow at 06:00, so it must not count at 06:00 today
    cases = [
        (30, "Chrg"),
        (90, "FrzChrg"),
        (150, "HoldChrg"),
        (200, None),
        (250, "Car"),
        (9 * 60, "Exp"),
        (10 * 60 + 30, "FrzExp"),
        (11 * 60 + 10, "Chrg"),
        (12 * 60 + 30, "HoldExp"),
        (6 * 60, None),
    ]
    for minute, expected in cases:
        got = plan_state_now(charges, exports, minute, 60, 4, car)
        if got != expected:
            print("ERROR: plan_state_now at minute {} gave {} expected {}".format(minute, got, expected))
            return 1
    if plan_state_now(None, None, 600, 60, 4, None) is not None:
        print("ERROR: no plan should mean plain demand")
        return 1
    return 0


def test_chart_replay():
    """A replay chart renders to a PNG without needing a display."""
    rows = [
        {"minutes_now": 540, "soc_percent": 80, "replanned": True, "logged": [(540, 600, 18.5, 40.0)], "replayed": [(570, 600, 18.5, 50.0)], "logged_charge": [], "replayed_charge": [(540, 570, 7.6, 100.0)], "reserve_percent": 4, "car_slots": [(540, 560)]},
        {"minutes_now": 545, "soc_percent": 78, "replanned": False, "logged": None, "replayed": None, "car_slots": [(540, 560)]},
        {"minutes_now": 550, "soc_percent": 75, "replanned": True, "logged": [(600, 660, 18.5, 99.0)], "replayed": [], "after_version_change": True},
    ]
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "replay.png")
        chart_replay(rows, path, title="test")
        with open(path, "rb") as handle:
            header = handle.read(8)
    if header != b"\x89PNG\r\n\x1a\n":
        print("ERROR: chart_replay did not write a PNG")
        return 1
    return 0


def test_simulate_soc(my_predbat):
    """With no PV and no plan the battery covers the actual load, losing it plus its losses; no time means no change."""
    if simulate_soc(my_predbat, 5.0, 0, 0.0, 1.0) != 5.0:
        print("ERROR: a zero-minute simulation should not move the SoC")
        return 1
    missing = object()
    saved = {key: getattr(my_predbat, key, missing) for key in ("charge_limit_best", "charge_window_best", "export_window_best", "export_limits_best", "soc_max", "reserve", "pv_forecast_minute_step", "load_minutes_step", "inverter_limit")}
    try:
        my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best = [], [], [], []
        my_predbat.soc_max = 10.0
        my_predbat.reserve = 0.5
        # The bare fixture has a zero inverter limit, which would stop the battery discharging at all
        my_predbat.inverter_limit = 5000 / 60000.0
        my_predbat.pv_forecast_minute_step = {}
        my_predbat.load_minutes_step = {}
        soc = simulate_soc(my_predbat, 5.0, 30, 0.0, 1.0)
    finally:
        # Restore exactly, removing what the fixture did not have, so no later test sees this one's state
        for key, value in saved.items():
            if value is missing:
                delattr(my_predbat, key)
            else:
                setattr(my_predbat, key, value)
    # 1 kWh of load drawn from the battery, divided by the discharge loss, so a little over 1 kWh
    if not (3.8 <= soc <= 4.0):
        print("ERROR: simulate_soc gave {} for 1 kWh of load from 5 kWh, expected a little under 4".format(soc))
        return 1
    return 0


def test_logged_load_divergence(my_predbat):
    """While installed, the logged divergence replaces the computed one; removing it restores the instance."""
    enabled = my_predbat.metric_load_divergence_enable
    try:
        install_logged_load_divergence(my_predbat)
        my_predbat.replay_load_divergence = 0.16
        my_predbat.metric_load_divergence_enable = True
        if my_predbat.get_load_divergence(0, {}) != 0.16:
            print("ERROR: the logged load divergence was not used")
            return 1
        my_predbat.metric_load_divergence_enable = False
        if my_predbat.get_load_divergence(0, {}) is not None:
            print("ERROR: a disabled load divergence should still read as None")
            return 1
    finally:
        remove_logged_load_divergence(my_predbat)
        my_predbat.metric_load_divergence_enable = enabled
    if "get_load_divergence" in my_predbat.__dict__ or "replay_load_divergence" in my_predbat.__dict__:
        print("ERROR: removing the override left state on the instance")
        return 1
    return 0


def test_soc_rms_error():
    """The RMS SoC error compares simulated and logged SoC, and is None when nothing was simulated."""
    rows = [{"soc_percent": 50, "soc_sim_percent": 53}, {"soc_percent": 60, "soc_sim_percent": 56}, {"soc_percent": 70, "soc_sim_percent": None}]
    if abs(soc_rms_error(rows) - (12.5 ** 0.5)) > 1e-9 or soc_rms_error([{"soc_percent": 1}]) is not None:
        print("ERROR: soc_rms_error gave {}".format(soc_rms_error(rows)))
        return 1
    return 0


def test_version_change():
    """The first run logging a different version is found; runs without a version line are skipped over."""
    runs = [{"version": "v9.3.1"}, {}, {"version": "v9.3.1"}, {"version": "v9.3.3"}, {"version": "v9.3.3"}]
    if version_change(runs) != (3, "v9.3.1", "v9.3.3") or version_change(runs[:3]) is not None:
        print("ERROR: version_change gave {}".format(version_change(runs)))
        return 1
    return 0


class InputBat:
    """The forecast attributes the replay-input helpers replace."""

    def __init__(self):
        """Start with a flat 0.1 kWh per 5 minutes load forecast and a flat PV forecast."""
        self.load_forecast = {minute: minute / 50.0 for minute in range(0, 24 * 60)}
        self.pv_forecast_minute = {minute: 0.01 for minute in range(24 * 60)}
        self.pv_forecast_minute10 = dict(self.pv_forecast_minute)
        self.pv_forecast_minute90 = dict(self.pv_forecast_minute)


def test_replay_inputs():
    """Logged load and PV forecasts are parsed and replace the replay's own from the logged start onwards."""
    lines = """2026-10-01 09:05:00.000000: --------------- PredBat - update at 2026-10-01 09:05:00+01:00 with clock skew 0 minutes, minutes now 545
2026-10-01 09:05:01.000000: Inverter 0 SoC: 10.0kWh 60%, current charge rate 9200W, current discharge rate 9660W, current battery power 0W
2026-10-01 09:05:01.100000: Replay input: PV forecast changed, 30-minute kWh from 09:00 p50 [1.5, 3.0] p10 [0.6, 1.2] p90 []
2026-10-01 09:05:01.200000: Replay input: load forecast, 5-minute Wh from 09:05 [200, 50]
"""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(lines)
        run = parse_log(path)[0]
    bat = InputBat()
    apply_logged_load_forecast(bat, run["load_input"])
    apply_logged_pv_forecast(bat, run["pv_input"])
    failed = 0
    # Forecast at 09:05 was 10.9 kWh cumulative; the log adds 0.2 then 0.05
    if abs(bat.load_forecast[545] - 10.9) > 1e-9 or abs(bat.load_forecast[550] - 11.1) > 1e-9 or abs(bat.load_forecast[555] - 11.15) > 1e-9 or bat.load_forecast[100] != 2.0:
        print("ERROR: logged load forecast applied wrongly: {} {} {}".format(bat.load_forecast[545], bat.load_forecast[550], bat.load_forecast[555]))
        failed = 1
    if abs(bat.pv_forecast_minute[545] - 0.05) > 1e-9 or abs(bat.pv_forecast_minute[575] - 0.1) > 1e-9 or bat.pv_forecast_minute[700] != 0.01:
        print("ERROR: logged PV forecast applied wrongly")
        failed = 1
    if bat.pv_forecast_minute90[545] != 0.01:
        print("ERROR: an empty logged series should leave that forecast alone")
        failed = 1
    return failed


def test_parse_override():
    """Overrides read numbers and booleans as such, and anything else as text."""
    cases = [("pv_metric90_weight=0.25", ("pv_metric90_weight", 0.25)), ("forecast_hours=30", ("forecast_hours", 30)), ("calculate_pv90_plan=False", ("calculate_pv90_plan", False)), ("mode=eco", ("mode", "eco"))]
    for text, expected in cases:
        if parse_override(text) != expected:
            print("ERROR: parse_override({}) gave {}".format(text, parse_override(text)))
            return 1
    try:
        parse_override("missing-equals")
    except ValueError:
        return 0
    print("ERROR: an override without = should be rejected")
    return 1


def test_apply_overrides(my_predbat):
    """An override sets a known setting; an unknown name is rejected rather than silently creating an attribute."""
    saved = my_predbat.pv_metric90_weight
    try:
        apply_overrides(my_predbat, {"pv_metric90_weight": 0.25})
        if my_predbat.pv_metric90_weight != 0.25:
            print("ERROR: the override was not applied")
            return 1
        try:
            apply_overrides(my_predbat, {"no_such_setting": 1})
        except ValueError:
            return 0
        print("ERROR: an unknown setting should be rejected")
        return 1
    finally:
        my_predbat.pv_metric90_weight = saved


RATE_STATS_KEYS = (
    "minutes_now",
    "forecast_minutes",
    "rate_import",
    "rate_import_base",
    "rate_export",
    "rate_export_base",
    "rate_min",
    "rate_max",
    "rate_average",
    "rate_min_minute",
    "rate_max_minute",
    "rate_min_forward",
    "rate_min_base",
    "rate_max_base",
    "rate_export_min",
    "rate_export_max",
    "rate_export_average",
    "rate_export_min_minute",
    "rate_export_max_minute",
    "rate_export_max_forward",
)


def test_rescan_rate_stats(my_predbat):
    """The rate stats are taken from now, so a passed import peak and export high drop out of them as the clock moves."""
    missing = object()
    saved = {key: getattr(my_predbat, key, missing) for key in RATE_STATS_KEYS}
    try:
        my_predbat.forecast_minutes = 24 * 60
        # Import 10p with a 30p peak 04:00-05:00; export 5p with a 20p high at the same time
        import_rates = {minute: 30.0 if 240 <= minute < 300 else 10.0 for minute in range(3 * 24 * 60)}
        export_rates = {minute: 20.0 if 240 <= minute < 300 else 5.0 for minute in range(3 * 24 * 60)}
        my_predbat.rate_import, my_predbat.rate_import_base = import_rates, dict(import_rates)
        my_predbat.rate_export, my_predbat.rate_export_base = export_rates, dict(export_rates)
        my_predbat.minutes_now = 0
        rescan_rate_stats(my_predbat)
        before = (my_predbat.rate_max, my_predbat.rate_max_base, my_predbat.rate_export_max, my_predbat.rate_export_max_forward[0])
        my_predbat.minutes_now = 360
        rescan_rate_stats(my_predbat)
        after = (my_predbat.rate_max, my_predbat.rate_max_base, my_predbat.rate_export_max, my_predbat.rate_export_max_forward[360])
        forward_min = my_predbat.rate_min_forward[360]
    finally:
        for key, value in saved.items():
            if value is missing:
                delattr(my_predbat, key)
            else:
                setattr(my_predbat, key, value)
    if before != (30.0, 30.0, 20.0, 20.0):
        print("ERROR: before the peak the stats should include it: {}".format(before))
        return 1
    if after != (10.0, 10.0, 5.0, 5.0) or forward_min != 10.0:
        print("ERROR: after the peak the stats should not include it: {} forward min {}".format(after, forward_min))
        return 1
    return 0


MIDNIGHT_LOG = """2026-10-03 23:55:00.000000: --------------- PredBat - update at 2026-10-03 23:55:00+01:00 with clock skew 0 minutes, minutes now 1435
2026-10-03 23:55:01.000000: Current data so far today: load 6.64kWh, import 17.07kWh, export 14.88kWh, PV 3.99kWh
2026-10-03 23:55:01.100000: Inverter 0 SoC: 4.05kWh 22%, current charge rate 5500W, current discharge rate 5500W, current battery power 5506W
2026-10-04 00:00:00.000000: --------------- PredBat - update at 2026-10-04 00:00:00+01:00 with clock skew 0 minutes, minutes now 0
2026-10-04 00:00:01.000000: Current data so far today: load 0.0kWh, import 0.0kWh, export 0.0kWh, PV 0.0kWh
2026-10-04 00:00:01.100000: Inverter 0 SoC: 3.56kWh 20%, current charge rate 5500W, current discharge rate 5500W, current battery power 5505W
2026-10-04 00:00:02.000000: Export windows filtered [ 04-10 00:25:00 - 04-10 00:30:00 @ 16.16p 24.0% ]
2026-10-04 00:00:03.000000: Inverter 0 SoC: 3.53kWh 20%, current charge rate 5500W, current discharge rate 5500W, current battery power 5505W
2026-10-04 00:00:04.000000: Starting comparison of tariffs
2026-10-04 00:01:00.000000: Current data so far today: load 9.9kWh, import 9.9kWh, export 9.9kWh, PV 9.9kWh
2026-10-04 00:01:01.000000: Export windows filtered [ 04-10 17:00:00 - 04-10 19:00:00 @ 29.0p 21.0% ]
"""


def test_parse_log_midnight():
    """A run keeps the SoC it planned from, and nothing logged by the midnight tariff comparison."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(MIDNIGHT_LOG)
        runs = parse_log(path)
    midnight = runs[1]
    failed = 0
    if midnight["soc"][0] != "3.56":
        print("ERROR: the SoC logged before the plan should be kept, got {}".format(midnight["soc"]))
        failed = 1
    if midnight["today"] != ("0.0", "0.0", "0.0", "0.0") or parse_windows(midnight["filtered"], "04-10") != [(25, 30, 16.16, 24.0)]:
        print("ERROR: the tariff comparison overwrote the run's own plan: {} {}".format(midnight["today"], midnight["filtered"]))
        failed = 1
    return failed


def test_counter_gain():
    """A day counter's gain is the difference, the reading itself across midnight, and nothing for a dip within a day."""
    if counter_gain("6.64", "6.61") != 6.64 - 6.61 or counter_gain("0.47", "6.64", new_day=True) != 0.47 or counter_gain(1.0, 1.0) != 0.0:
        print("ERROR: counter_gain gave {} {}".format(counter_gain("6.64", "6.61"), counter_gain("0.47", "6.64", new_day=True)))
        return 1
    if counter_gain("6.60", "6.64") != 0.0:
        print("ERROR: a dip within a day should count as no gain, got {}".format(counter_gain("6.60", "6.64")))
        return 1
    if not crosses_midnight({"time": "2026-10-03 23:55:00+01:00"}, {"time": "2026-10-04 00:00:00+01:00"}) or crosses_midnight({"time": "2026-10-04 00:00:00+01:00"}, {"time": "2026-10-04 00:05:00+01:00"}):
        print("ERROR: crosses_midnight should compare the runs' dates")
        return 1
    return 0


def test_shift_day_series():
    """Cumulative series restart from the new midnight; windows move back a day without touching the originals."""
    failed = 0
    shifted = shift_cumulative({0: 0.0, 1430: 9.5, 1440: 10.0, 1445: 10.25}, 1440)
    if shifted != {-1440: -10.0, -10: -0.5, 0: 0.0, 5: 0.25}:
        print("ERROR: shift_cumulative gave {}".format(shifted))
        failed = 1
    windows = [{"start": 1410, "end": 1440, "start_orig": 1380, "average": 7.62}]
    moved = shift_windows(windows, 1440)
    if moved != [{"start": -30, "end": 0, "start_orig": -60, "average": 7.62}] or windows[0]["start"] != 1410:
        print("ERROR: shift_windows gave {} and left {}".format(moved, windows))
        failed = 1
    return failed


def test_roll_over_midnight():
    """Rolling over moves the minute-keyed state back a day, advances midnight and leaves the plan's own timestamp alone."""
    bat = SimpleNamespace(
        rate_import={1435: 7.62, 1440: 7.62, 2880: 30.0},
        pv_forecast_minute={1500: 0.01},
        manual_charge_times=[1470],
        load_forecast={0: 0.0, 1440: 10.0, 1445: 10.25},
        load_forecast_array=[{1440: 10.0, 1450: 10.5}],
        charge_window_best=[{"start": 1470, "end": 1770, "average": 7.62}],
        car_charging_slots=[[{"start": 1500, "end": 1530, "kwh": 2.0}]],
        midnight_utc=datetime(2026, 10, 3, tzinfo=timezone.utc),
        minutes_now=1435,
        plan_last_updated_minutes=1430,
    )
    roll_over_midnight(bat)
    failed = 0
    if bat.rate_import != {-5: 7.62, 0: 7.62, 1440: 30.0} or bat.pv_forecast_minute != {60: 0.01} or bat.manual_charge_times != [30]:
        print("ERROR: minute-keyed state not moved back a day: {} {} {}".format(bat.rate_import, bat.pv_forecast_minute, bat.manual_charge_times))
        failed = 1
    if bat.load_forecast[5] != 0.25 or bat.load_forecast_array != [{0: 0.0, 10: 0.5}]:
        print("ERROR: the load forecast should count from the new midnight: {} {}".format(bat.load_forecast, bat.load_forecast_array))
        failed = 1
    if bat.charge_window_best[0]["start"] != 30 or bat.car_charging_slots[0][0]["end"] != 90:
        print("ERROR: windows not moved back a day: {} {}".format(bat.charge_window_best, bat.car_charging_slots))
        failed = 1
    if bat.midnight_utc != datetime(2026, 10, 4, tzinfo=timezone.utc) or bat.minutes_now != -5 or bat.plan_last_updated_minutes != 1430:
        print("ERROR: clock state wrong after the roll over: {} {} {}".format(bat.midnight_utc, bat.minutes_now, bat.plan_last_updated_minutes))
        failed = 1
    return failed


def test_set_export_window_after_midnight():
    """A window over midnight seen just after midnight started yesterday, so it is running now."""
    bat = FakeBat(rate_export={-15: 16.68})
    set_export_window(bat, ("True", "23", "45", "00", "01"), 0)
    if bat.export_window != [{"start": -15, "end": 1, "average": 16.68}] or not bat.isExporting:
        print("ERROR: a window over midnight seen after midnight should have started yesterday: {} {}".format(bat.export_window, bat.isExporting))
        return 1
    return 0


INVALID_LOG = """2026-10-04 04:01:03.000000: --------------- PredBat - update at 2026-10-04 04:00:00+01:00 with clock skew 0 minutes, minutes now 240
2026-10-04 04:01:18.000000: Inverter 0 SoC: 18.08kWh 100%, current charge rate 5500W, current discharge rate 5500W, current battery power 159W
2026-10-04 04:01:20.198077: Will recompute the plan as it is invalid
2026-10-04 04:01:20.198100: Recompute, previous plan is invalid...
2026-10-04 04:01:20.203586: Best export window [ 04-10 04:00:00 - 04-10 04:30:00 @ 14.95p 100.0% ]
2026-10-04 04:01:21.000000: Export windows filtered [ 04-10 06:00:00 - 04-10 07:00:00 @ 15.8p 63.0% ]
2026-10-04 12:05:16.706970: --------------- PredBat - update at 2026-10-04 12:05:00+01:00 with clock skew 0 minutes, minutes now 725
2026-10-04 12:05:16.800000: Inverter 0 SoC: 16.20kWh 90%, current charge rate 5500W, current discharge rate 5500W, current battery power 159W
2026-10-04 12:05:16.900000: Sensor changes require a replan, will recompute the plan
2026-10-04 12:05:17.111032: Will recompute the plan as it is invalid
2026-10-04 12:05:17.111195: Recompute is saving previous plan...
2026-10-04 12:05:17.115425: Best export window [ 04-10 14:00:00 - 04-10 14:30:00 @ 7.45p 100.0% ]
2026-10-04 12:05:19.357686: Export windows filtered [ 04-10 16:50:00 - 04-10 19:00:00 @ 23.02p 29.0% ]
"""


def test_parse_log_invalid_plan():
    """Only a run whose plan was really invalid is marked invalid; no recomputing run's working window list is taken as the plan in force."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(INVALID_LOG)
        run, recompute = parse_log(path)
    if not run.get("invalid") or run["in_force"] is not None or run["filtered"] is None:
        print("ERROR: an invalid-plan run parsed wrongly: invalid {} in force {} filtered {}".format(run.get("invalid"), run["in_force"], run["filtered"]))
        return 1
    # A sensor-triggered recompute logs the same "Will recompute" line, but its plan is valid and is compared with the new one
    if recompute.get("invalid") or not recompute.get("recompute") or recompute["in_force"] is not None:
        print("ERROR: a valid recompute parsed wrongly: invalid {} recompute {} in force {}".format(recompute.get("invalid"), recompute.get("recompute"), recompute["in_force"]))
        return 1
    return 0


def test_apply_run_clears_p90_signatures():
    """Each replayed run clears the p90 guard's signatures, as fetch does, so a p50-only PV change keeps the real p90."""
    bat = SimpleNamespace(
        minutes_now=500,
        now_utc=datetime(2026, 10, 4, 8, 20, tzinfo=timezone.utc),
        load_minutes={},
        import_today={},
        export_today={},
        pv_today={},
        inverters=[],
        rate_export={},
        pv_forecast_minute90_signatures=((1, 1.0, 0, 0), (1, 1.0, 0, 0)),
    )
    apply_run(bat, {"today": None, "force": None, "time": "2026-10-04 08:20:00+01:00"}, {"time": "2026-10-04 08:25:00+01:00", "minutes_now": 505, "soc": ("10.0", "55", "0")})
    if bat.pv_forecast_minute90_signatures is not None or bat.minutes_now != 505:
        print("ERROR: apply_run should clear the p90 signatures: {}".format(bat.pv_forecast_minute90_signatures))
        return 1
    return 0


STATE_RATES_LOG = """2026-10-04 11:50:00.000000: --------------- PredBat - update at 2026-10-04 11:50:00+01:00 with clock skew 0 minutes, minutes now 710
2026-10-04 11:50:01.000000: Replay input: rates changed, from 11:50 import [[0, 7.5], [10, 31.16]] 30 export [[0, 15.0]] 20 import_base [] 0 export_base [[0, 15.0]] 20
2026-10-04 11:55:00.000000: --------------- PredBat - update at 2026-10-04 11:55:00+01:00 with clock skew 0 minutes, minutes now 715
2026-10-04 11:55:01.000000: Current data so far today: load 3.96kWh, import 21.27kWh, export 8.57kWh, PV 3.46kWh
2026-10-04 11:55:01.100000: Replay input: state soc_kw 15.495 soc_max 18.08 inday 0.9763652840196139 cost_today 47.02738900000056 load_today 3.9639999999999986 import_today 21.27000000000001 export_today 8.570000000000007 pv_today None
2026-10-04 11:55:01.200000: Inverter 0 SoC: 15.50kWh 86%, current charge rate 9200W, current discharge rate 9660W, current battery power 0W
"""


def test_replay_state_and_rates():
    """The exact state line overrides the rounded values, and a rates change logged by a dropped run reaches the next run."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(STATE_RATES_LOG)
        runs = parse_log(path)
    failed = 0
    if len(runs) != 1 or runs[0].get("rates") is None:
        print("ERROR: the dropped run's rates should carry to the next run: {}".format(runs))
        return 1
    run = runs[0]
    if run["state"]["inday"] != 0.9763652840196139 or run["state"]["pv_today"] is not None:
        print("ERROR: state line parsed wrongly: {}".format(run["state"]))
        failed = 1
    # pv_today is None in the state, so the counters fall back to the rounded line
    if today_values(run) != ("3.96", "21.27", "8.57", "3.46"):
        print("ERROR: today_values should fall back when the state lacks a counter: {}".format(today_values(run)))
        failed = 1
    if today_values({"state": dict(run["state"], pv_today=3.46)}) != (3.9639999999999986, 21.27000000000001, 8.570000000000007, 3.46):
        print("ERROR: today_values should prefer the exact state counters")
        failed = 1
    inverter = SimpleNamespace(soc_kw=0, soc_percent=0)
    bat = SimpleNamespace(
        minutes_now=710,
        now_utc=datetime(2026, 10, 4, 10, 50, tzinfo=timezone.utc),
        load_minutes={},
        import_today={},
        export_today={},
        pv_today={},
        inverters=[inverter],
        soc_max=18.0,
        rate_import={minute: 20.0 for minute in range(0, 2000)},
        rate_export={minute: 5.0 for minute in range(0, 2000)},
        rate_import_base={minute: 20.0 for minute in range(0, 2000)},
        rate_export_base={},
    )
    apply_run(bat, {"today": None, "force": None, "time": "2026-10-04 11:50:00+01:00"}, run)
    if bat.soc_kw != 15.495 or inverter.soc_kw != 15.495 or bat.soc_max != 18.08 or bat.load_inday_adjustment != 0.9763652840196139 or bat.cost_today_sofar != 47.02738900000056:
        print("ERROR: exact state not applied: soc {} max {} inday {} cost {}".format(bat.soc_kw, bat.soc_max, bat.load_inday_adjustment, bat.cost_today_sofar))
        failed = 1
    # Rebuilt from 11:50: 7.5 for 10 minutes then 31.16 to 30 minutes, nothing after; before 11:50 untouched
    if bat.rate_import[709] != 20.0 or bat.rate_import[710] != 7.5 or bat.rate_import[719] != 7.5 or bat.rate_import[720] != 31.16 or bat.rate_import[739] != 31.16 or 740 in bat.rate_import:
        print("ERROR: import rates rebuilt wrongly")
        failed = 1
    if bat.rate_export_base.get(729) != 15.0 or 730 in bat.rate_export_base or bat.rate_export[729] != 15.0 or 730 in bat.rate_export:
        print("ERROR: export rates rebuilt wrongly")
        failed = 1
    # An empty series empties the rates from the start, as the live rates were
    if bat.rate_import_base.get(709) != 20.0 or 710 in bat.rate_import_base:
        print("ERROR: an empty series should leave no rates from its start")
        failed = 1
    return failed


def test_apply_logged_rates_from_before_midnight():
    """A rates line from the day before (carried over midnight by a dropped run) is moved back a day."""
    bat = SimpleNamespace(minutes_now=5, rate_import={})
    apply_logged_rates(bat, (23 * 60 + 55, {"import": ([(0, 10.0), (10, 20.0)], 20)}))
    if bat.rate_import.get(-5) != 10.0 or bat.rate_import.get(5) != 20.0 or 15 in bat.rate_import:
        print("ERROR: a rates line from before midnight applied wrongly: {}".format(sorted(bat.rate_import.items())[:3]))
        return 1
    return 0


def test_load_exact():
    """The exact load forecast line sets the two values per step the plan reads, removes ones logged None, and wins over the older slot line."""
    lines = """2026-10-04 19:30:00.000000: --------------- PredBat - update at 2026-10-04 19:30:00+01:00 with clock skew 0 minutes, minutes now 1170
2026-10-04 19:30:01.000000: Inverter 0 SoC: 10.0kWh 60%, current charge rate 9200W, current discharge rate 9660W, current battery power 0W
2026-10-04 19:30:01.200000: Replay input: load forecast, cumulative kWh at each 5-minute step and the minute after from 19:30 [[12.3456, 12.3489], [12.4, None]]
"""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(lines)
        run = parse_log(path)[0]
    bat = SimpleNamespace(load_forecast={minute: 1.0 for minute in range(1160, 1190)})
    apply_logged_load_exact(bat, run["load_exact"])
    forecast = bat.load_forecast
    if forecast[1170] != 12.3456 or forecast[1171] != 12.3489 or forecast[1175] != 12.4 or 1176 in forecast or forecast[1172] != 1.0 or forecast[1160] != 1.0:
        print("ERROR: exact load forecast applied wrongly: {}".format({minute: forecast.get(minute) for minute in (1170, 1171, 1172, 1175, 1176)}))
        return 1
    return 0


def test_load_compact():
    """The compact load line rebuilds each step's start and minute-after value exactly, in tenths of a Wh."""
    lines = """2026-10-06 10:00:00.000000: --------------- PredBat - update at 2026-10-06 10:00:00+01:00 with clock skew 0 minutes, minutes now 600
2026-10-06 10:00:01.000000: Inverter 0 SoC: 10.0kWh 60%, current charge rate 9200W, current discharge rate 9660W, current battery power 0W
2026-10-06 10:00:01.200000: Replay input: load from 10:00 base 12345.6 Wh, Wh/5min [48.1/9.6, 47.9/9.5, -0.2/0.0]
"""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(lines)
        run = parse_log(path)[0]
    bat = SimpleNamespace(load_forecast={minute: 1.0 for minute in range(590, 620)})
    apply_logged_load_exact(bat, run["load_exact"])
    expected = {600: 12.3456, 601: 12.3552, 605: 12.3937, 606: 12.4032, 610: 12.4416, 611: 12.4416, 602: 1.0}
    got = {minute: bat.load_forecast.get(minute) for minute in expected}
    if got != expected:
        print("ERROR: compact load forecast applied wrongly: {}".format(got))
        return 1
    return 0


def test_pv_exact():
    """The exact PV line sets each minute it covers from its runs, removes minutes logged None, and leaves the rest alone."""
    lines = """2026-10-04 19:30:00.000000: --------------- PredBat - update at 2026-10-04 19:30:00+01:00 with clock skew 0 minutes, minutes now 1170
2026-10-04 19:30:01.000000: Inverter 0 SoC: 10.0kWh 60%, current charge rate 9200W, current discharge rate 9660W, current battery power 0W
2026-10-04 19:30:01.100000: Replay input: PV forecast changed, per-minute kWh runs from 19:30 p50 [[0.0094, 3], [None, 2]] p10 [[0.03333333333333333, 5]] p90 [[0.0, 5]]
"""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(lines)
        run = parse_log(path)[0]
    bat = SimpleNamespace(pv_forecast_minute={minute: 1.0 for minute in range(1160, 1190)}, pv_forecast_minute10={}, pv_forecast_minute90={1200: 2.0})
    apply_logged_pv_exact(bat, run["pv_exact"])
    if bat.pv_forecast_minute[1172] != 0.0094 or 1173 in bat.pv_forecast_minute or 1174 in bat.pv_forecast_minute or bat.pv_forecast_minute[1175] != 1.0 or bat.pv_forecast_minute[1169] != 1.0:
        print("ERROR: exact p50 applied wrongly")
        return 1
    if bat.pv_forecast_minute10.get(1174) != 0.03333333333333333 or bat.pv_forecast_minute90.get(1174) != 0.0 or bat.pv_forecast_minute90.get(1200) != 2.0:
        print("ERROR: exact p10/p90 applied wrongly")
        return 1
    return 0


def test_cars_input():
    """A logged car state is parsed, carried past a dropped run, and set on the instance by apply_run."""
    lines = """2026-10-05 17:25:00.000000: --------------- PredBat - update at 2026-10-05 17:25:00+01:00 with clock skew 0 minutes, minutes now 1045
2026-10-05 17:25:01.000000: Replay input: cars changed {'car_charging_planned': [True], 'car_charging_soc': [33.11], 'car_charging_slots': [[{'start': 1290, 'end': 1350, 'kwh': 7.658, 'octopus': True}]], 'car_charging_limit_model': [9999.0]}
2026-10-05 17:25:41.000000: --------------- PredBat - update at 2026-10-05 17:25:00+01:00 with clock skew 0 minutes, minutes now 1045
2026-10-05 17:25:42.000000: Inverter 0 SoC: 18.08kWh 100%, current charge rate 9200W, current discharge rate 9660W, current battery power 0W
"""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(lines)
        runs = parse_log(path)
    if len(runs) != 1 or runs[0].get("cars", {}).get("car_charging_soc") != [33.11]:
        print("ERROR: the dropped run's car state should carry to the next run: {}".format(runs))
        return 1
    bat = SimpleNamespace(minutes_now=1040, now_utc=datetime(2026, 10, 5, 16, 20, tzinfo=timezone.utc), load_minutes={}, import_today={}, export_today={}, pv_today={}, inverters=[], rate_export={}, car_charging_planned=[False], car_charging_slots=[[]])
    apply_run(bat, {"today": None, "force": None, "time": "2026-10-05 17:20:00+01:00"}, runs[0])
    if bat.car_charging_planned != [True] or bat.car_charging_slots[0][0]["kwh"] != 7.658 or bat.car_charging_limit_model != [9999.0]:
        print("ERROR: car state not applied: {}".format(vars(bat)))
        return 1
    return 0


def test_inverter_input():
    """A logged inverter state carries past a dropped run and replaces the reconstructed export window; the exact
    divergence and the rates in force are applied too."""
    lines = """2026-10-05 23:31:30.000000: --------------- PredBat - update at 2026-10-05 23:30:00+01:00 with clock skew 0 minutes, minutes now 1410
2026-10-05 23:31:31.000000: Replay input: inverter changed {'charge_window': [{'start': 1410, 'end': 1440, 'average': 0}], 'charge_limit': [18.08], 'isCharging': True, 'reserve': 0.723}
2026-10-05 23:32:00.000000: --------------- PredBat - update at 2026-10-05 23:30:00+01:00 with clock skew 0 minutes, minutes now 1410
2026-10-05 23:32:01.000000: Inverter 0 SoC: 4.67kWh 26%, current charge rate 5500W, current discharge rate 5500W, current battery power 0W
2026-10-05 23:32:01.100000: Replay input: state soc_kw 4.665 soc_max 18.08 inday 0.95 cost_today -744.68 load_today 7.65 import_today 18.2 export_today 22.98 pv_today None charge_rate_now 0.0625 discharge_rate_now 0.09166666666666666 battery_temperature 23.0
2026-10-05 23:32:01.200000: Load divergence over 2.0 hours mean 300W, min 100W, max 900W, std dev 95W, divergence 31.6%
2026-10-05 23:32:01.300000: Replay input: load divergence 0.31
"""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "predbat.log")
        with open(path, "w") as handle:
            handle.write(lines)
        runs = parse_log(path)
    if len(runs) != 1 or runs[0].get("inverter", {}).get("charge_window") != [{"start": 1410, "end": 1440, "average": 0}]:
        print("ERROR: the dropped run's inverter state should carry to the next run: {}".format(runs))
        return 1
    bat = SimpleNamespace(minutes_now=1400, now_utc=datetime(2026, 10, 5, 22, 20, tzinfo=timezone.utc), load_minutes={}, import_today={}, export_today={}, pv_today={}, inverters=[], rate_export={}, charge_window=[], isCharging=False, replay_inverter_logged=True)
    apply_run(bat, {"today": None, "force": None, "time": "2026-10-05 23:20:00+01:00"}, runs[0])
    if bat.charge_window != [{"start": 1410, "end": 1440, "average": 0}] or bat.isCharging is not True or bat.reserve != 0.723:
        print("ERROR: inverter state not applied: {}".format(vars(bat)))
        return 1
    if bat.charge_rate_now != 0.0625 or bat.discharge_rate_now != 0.09166666666666666 or bat.battery_temperature != 23 or not isinstance(bat.battery_temperature, int):
        print("ERROR: the logged rates in force should be applied: {}".format(vars(bat)))
        return 1
    if bat.replay_load_divergence != 0.31:
        print("ERROR: the exact divergence should win over the rounded percentage: {}".format(bat.replay_load_divergence))
        return 1
    return 0


def run_replay_forward_tests(my_predbat):
    """Run every forward replay test, returning a non-zero count on failure."""
    failed = 0
    failed += test_parse_windows()
    failed += test_parse_log()
    failed += test_shift_counter()
    failed += test_set_export_window()
    failed += test_summarise()
    failed += test_plan_state_now()
    failed += test_chart_replay()
    failed += test_simulate_soc(my_predbat)
    failed += test_logged_load_divergence(my_predbat)
    failed += test_soc_rms_error()
    failed += test_version_change()
    failed += test_replay_inputs()
    failed += test_parse_override()
    failed += test_apply_overrides(my_predbat)
    failed += test_rescan_rate_stats(my_predbat)
    failed += test_parse_log_midnight()
    failed += test_counter_gain()
    failed += test_shift_day_series()
    failed += test_roll_over_midnight()
    failed += test_set_export_window_after_midnight()
    failed += test_parse_log_invalid_plan()
    failed += test_apply_run_clears_p90_signatures()
    failed += test_replay_state_and_rates()
    failed += test_apply_logged_rates_from_before_midnight()
    failed += test_load_exact()
    failed += test_load_compact()
    failed += test_pv_exact()
    failed += test_cars_input()
    failed += test_inverter_input()
    return failed
