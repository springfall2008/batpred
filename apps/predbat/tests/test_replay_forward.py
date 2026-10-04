# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the forward replay of a Predbat log from a debug yaml (tests/replay_forward.py)."""

import os
import tempfile

from utils import MinuteArray
from tests.test_single_debug import apply_overrides
from tests.replay_forward import parse_windows, parse_log, shift_counter, set_export_window, summarise, first_window, export_mode_now, chart_replay, simulate_soc, install_logged_load_divergence, remove_logged_load_divergence, soc_rms_error, version_change, apply_logged_load_forecast, apply_logged_pv_forecast, parse_log, parse_override, rescan_rate_stats

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


def test_export_mode_now():
    """The plan's instruction at a minute: forced export below 99%, freeze at 99%, nothing at 100% or outside a window."""
    windows = [(540, 600, 18.5, 40.0), (600, 660, 18.5, 99.0), (660, 720, 18.5, 100.0), (1410, 1440, 18.5, 10.0), (1800, 1860, 18.5, 30.0)]
    # The last window is tomorrow at 06:00, so it must not count at 06:00 today
    cases = [(9 * 60, "export"), (10 * 60 + 30, "freeze"), (11 * 60 + 30, None), (8 * 60, None), (23 * 60 + 45, "export"), (6 * 60, None)]
    for minute, expected in cases:
        got = export_mode_now(windows, minute)
        if got != expected:
            print("ERROR: export_mode_now at minute {} gave {} expected {}".format(minute, got, expected))
            return 1
    if export_mode_now(None, 600) is not None:
        print("ERROR: no plan should mean no export")
        return 1
    return 0


def test_chart_replay():
    """A replay chart renders to a PNG without needing a display."""
    rows = [
        {"minutes_now": 540, "soc_percent": 80, "replanned": True, "logged": [(540, 600, 18.5, 40.0)], "replayed": [(570, 600, 18.5, 50.0)]},
        {"minutes_now": 545, "soc_percent": 78, "replanned": False, "logged": None, "replayed": None},
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


def run_replay_forward_tests(my_predbat):
    """Run every forward replay test, returning a non-zero count on failure."""
    failed = 0
    failed += test_parse_windows()
    failed += test_parse_log()
    failed += test_shift_counter()
    failed += test_set_export_window()
    failed += test_summarise()
    failed += test_export_mode_now()
    failed += test_chart_replay()
    failed += test_simulate_soc(my_predbat)
    failed += test_logged_load_divergence(my_predbat)
    failed += test_soc_rms_error()
    failed += test_version_change()
    failed += test_replay_inputs()
    failed += test_parse_override()
    failed += test_apply_overrides(my_predbat)
    failed += test_rescan_rate_stats(my_predbat)
    return failed
