# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the forward replay of a Predbat log from a debug yaml (tests/replay_forward.py)."""

import os
import tempfile

from utils import MinuteArray
from tests.replay_forward import parse_windows, parse_log, shift_counter, set_export_window, summarise, first_window

SAMPLE_LOG = """2026-10-01 08:30:00.575570: --------------- PredBat - update at 2026-10-01 08:30:00+01:00 with clock skew 0 minutes, minutes now 510
2026-10-01 08:30:02.135467: Current data so far today: load 5.74kWh, import 17.74kWh, export 4.57kWh, PV 0.21kWh
2026-10-01 08:30:02.324401: Today's load divergence 100.0%, in-day adjustment 92.66%, damping 0.95x, yesterday 88.64% today 100.0% blend 64.58%
2026-10-01 08:30:02.366949: Inverter 0 SoC: 14.27kWh 85%, current charge rate 9200W, current discharge rate 9660W, current battery power -40W, current battery voltage 52.0V, grid power 6W, load power 506W, PV Power 558W
2026-10-01 08:30:02.468111: PV Forecast 44.6kWh and 10% Forecast 30.6kWh; PV cloud factor 0.2
2026-10-01 08:30:04.419814: Export windows filtered [ 01-10 09:00:00 - 01-10 12:00:00 @ 18.5c 55.0%, 01-10 13:00:00 - 01-10 13:30:00 @ 18.5c 84.0% ]
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
    windows = parse_windows("[ 01-10 09:00:00 - 01-10 12:00:00 @ 18.5c 55.0%, 01-10 23:55:00 - 02-10 00:00:00 @ 8.43c 0.0% ]")
    if windows != [("09:00", "12:00", 18.5, 55.0), ("23:55", "00:00", 8.43, 0.0)]:
        print("ERROR: parse_windows gave {}".format(windows))
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
    if first["inday"] != "92.66" or first["cloud"] != "0.2" or first["force"] != ("True", "09", "00", "12", "01"):
        print("ERROR: in-day adjustment, cloud factor or force export parsed wrongly: {} {} {}".format(first["inday"], first["cloud"], first["force"]))
        failed = 1
    if parse_windows(first["filtered"]) != [("09:00", "12:00", 18.5, 55.0), ("13:00", "13:30", 18.5, 84.0)]:
        print("ERROR: filtered windows parsed wrongly: {}".format(first["filtered"]))
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
    if shift_counter(history, 0, 4.0) is not history:
        print("ERROR: a zero-minute shift should leave the history alone")
        return 1
    return 0


def test_set_export_window():
    """The log's force export line becomes the inverter's window, and a False clears it."""
    bat = FakeBat(rate_export={540: 18.5})
    set_export_window(bat, ("True", "09", "00", "12", "01"), 545)
    if bat.export_window != [{"start": 540, "end": 721, "average": 18.5}] or not bat.isExporting:
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
    if bat.export_window != [] or bat.isExporting:
        print("ERROR: force export False should clear the window")
        return 1
    return 0


def test_summarise():
    """Re-plans are counted as identical or as agreeing on the first window's start."""
    same = [("09:00", "12:00", 18.5, 55.0)]
    rows = [
        {"replanned": True, "logged": same, "replayed": same},
        {"replanned": True, "logged": same, "replayed": [("09:00", "12:00", 18.5, 60.0)]},
        {"replanned": True, "logged": same, "replayed": [("10:00", "12:00", 18.5, 55.0)]},
        {"replanned": False, "logged": None, "replayed": None},
    ]
    if summarise(rows) != (3, 1, 2):
        print("ERROR: summarise gave {} expected (3, 1, 2)".format(summarise(rows)))
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
    return failed
