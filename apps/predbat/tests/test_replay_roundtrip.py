# fmt: off
# pylint: disable=line-too-long
"""Round trip: run Predbat's main loop against the dummy inverter, then replay the log it wrote and expect the same plans.

Every cycle Predbat logs the inputs a replay needs ("Replay input:" lines) alongside its plan. Replaying that log from a
debug yaml taken during the run must reproduce each re-plan line for line; a plan input that is not logged, or one the
replay does not apply, shows up as the first run whose plan lines differ. Both halves come from the code under test, so
nothing here goes stale when the planning algorithm changes.

The run uses its own PredBat instance and a temporary directory, so it leaves the shared fixture and the working
directory as it found them.
"""

import math
import os
import tempfile
from datetime import datetime, timedelta

import pytz

from dummy_inverter import DummyInverter
from predbat import PredBat
from tests.replay_forward import plan_trace, replay_forward, trace_differences
from tests.test_infra import FIXTURE_MINUTES_NOW, TestHAInterface

TIMEZONE = "Europe/London"
# Days of synthetic history behind the dummy's cumulative sensors, enough for the weighted-bucket load forecast
HISTORY_DAYS = 8
# Totals the dummy's cumulative sensors start the run at, so the history behind them stays positive
START_TOTALS = {"load": 1000.0, "pv": 2000.0, "import": 600.0, "export": 400.0}


def new_predbat(config_root):
    """A PredBat on a test HA interface, built as the shared fixture is, writing its files under config_root."""
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.minutes_now = FIXTURE_MINUTES_NOW
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=FIXTURE_MINUTES_NOW)
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.base = my_predbat
    my_predbat.ha_interface.history_enable = False
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.config_root = config_root
    my_predbat.config_root_p = config_root
    my_predbat.save_restore_dir = config_root
    # Never reach out to GitHub from a test
    my_predbat.download_predbat_releases = lambda *args, **kwargs: None
    return my_predbat


def set_clock(my_predbat, when):
    """Point update_time() at when (local time) through the clock skew, and return it as an aware datetime.

    The skew is rounded up so update_time()'s floor to the 5-minute step lands on when, not the step before. The
    harness never calls update_time() itself, as that logs a run marker.
    """
    tz = pytz.timezone(TIMEZONE)
    target = tz.localize(when)
    my_predbat.args["clock_skew"] = int(math.ceil((target - datetime.now(tz)).total_seconds() / 60.0))
    my_predbat.now_utc = target
    return target


def sync_controls(my_predbat, dummy):
    """Copy what Predbat wrote to the dummy's control entities into the dummy's model."""
    for direction, field in dummy.control_fields():
        entity_id = dummy.control_info(direction, field)[0]
        value = my_predbat.ha_interface.get_state(entity_id)
        if value is not None:
            dummy.update_control(entity_id, value)


def kw_at(profile, minute):
    """kW from a profile of values spread evenly across the day, at an absolute minute."""
    return float(profile[(minute % (24 * 60)) * len(profile) // (24 * 60)])


def make_history(my_predbat, dummy, load_profile, pv_profile):
    """A get_history replacement: the dummy's cumulative sensors as if the profiles had run for HISTORY_DAYS, the current state otherwise.

    Import and export are a flat share of load and PV, which only the history (not the plan) reads.
    """
    tz = pytz.timezone(TIMEZONE)
    sensors = {"load_lifetime": (load_profile, 1.0), "pv_lifetime": (pv_profile, 1.0), "grid_import_lifetime": (load_profile, 0.6), "grid_export_lifetime": (pv_profile, 0.2)}

    def get_history(entity_id, now=None, days=30):
        """Synthetic history ending now, oldest first, five minutes apart."""
        now = my_predbat.now_utc
        current = my_predbat.ha_interface.get_state(entity_id)
        profile = None
        for name, series in sensors.items():
            if entity_id == dummy.entity("sensor", name):
                profile = series
        span = min(days, HISTORY_DAYS) * 24 * 60
        now_minute = now.hour * 60 + now.minute
        totals = {}
        if profile:
            # Walk back from now, taking off what the profile drew in each 5-minute step
            total = float(current or 0)
            for back in range(0, span + 1, 5):
                totals[back] = total
                total -= kw_at(profile[0], now_minute - back - 5) * profile[1] * 5 / 60.0
        # Each entity keeps its own unit: a power sensor read as kWh turns 500 W into 500 kW
        unit = my_predbat.ha_interface.get_state(entity_id, attribute="unit_of_measurement") or None
        points = []
        for back in range(span, -1, -5):
            stamp = (now - timedelta(minutes=back)).astimezone(tz).isoformat()
            if profile:
                state = round(totals[back], 4)
            else:
                # Status-like entities are read as text, and an unset one as unknown
                state = "unknown" if current is None else current
            points.append({"state": state, "last_updated": stamp, "last_changed": stamp, "attributes": {"unit_of_measurement": unit} if unit else {}})
        return [points]

    return get_history


def capture_log(my_predbat, lines):
    """Make my_predbat.log keep each line, with a timestamp in the live log's format, as well as logging it."""
    original = type(my_predbat).log

    def log(message, *args, **kwargs):
        """Keep the line, then log as normal."""
        lines.append("{}: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"), message))
        return original(my_predbat, message, *args, **kwargs)

    my_predbat.log = log


def roundtrip(start, cycles, load_profile, pv_peak, folder):
    """Run cycles of update_pred() from start against the dummy inverter, then replay the log; return (live trace, replayed trace)."""
    my_predbat = new_predbat(folder)
    dummy = DummyInverter(my_predbat, config={"battery_size": 10, "soc_initial": 50, "load": load_profile, "pv_peak": pv_peak})
    dummy.totals.update(START_TOTALS)
    my_predbat.ha_interface.get_history = make_history(my_predbat, dummy, load_profile, [0.0])
    my_predbat.args["rates_import"] = [{"start": "00:30:00", "end": "05:30:00", "rate": 7.5}, {"start": "05:30:00", "end": "00:30:00", "rate": 30.0}]
    my_predbat.args["rates_export"] = [{"start": "00:00:00", "end": "00:00:00", "rate": 15.0}]
    # The weighted-bucket load forecast, as a live install without Load ML uses; the fixture's apps.yaml names a
    # GivTCP load forecast sensor that does not exist here
    my_predbat.args["days_previous_auto"] = True
    my_predbat.args["load_forecast_only"] = False
    my_predbat.args.pop("load_forecast", None)
    live_lines = []
    capture_log(my_predbat, live_lines)
    yaml_path = None
    set_clock(my_predbat, start)
    dummy.publish_controls()
    dummy.automatic_config()
    for cycle in range(cycles):
        set_clock(my_predbat, start + timedelta(minutes=5 * cycle))
        sync_controls(my_predbat, dummy)
        dummy.step(dummy.minutes_absolute())
        dummy.publish()
        # The inverter's clock is checked against the real one, not the simulated one
        my_predbat.ha_interface.set_state(dummy.entity("sensor", "time"), datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S"))
        my_predbat.update_pred(scheduled=True)
        if cycle == 0:
            my_predbat.create_debug_yaml()
            yaml_path = os.path.join(folder, "debug", "predbat_debug_{}.yaml".format(my_predbat.now_utc.strftime("%H_%M_%S")))
    log_path = os.path.join(folder, "predbat.log")
    with open(log_path, "w") as handle:
        handle.write("\n".join(live_lines) + "\n")

    replay_lines = []
    capture_log(my_predbat, replay_lines)
    replay_forward(my_predbat, yaml_path, log_path, quiet=True)
    return plan_trace(live_lines), plan_trace(replay_lines, replay=True)


def check_roundtrip(name, start, cycles, load_profile, pv_peak):
    """Run a round trip and report any re-plan whose plan lines differ; returns the number of failures."""
    with tempfile.TemporaryDirectory() as folder:
        live, replayed = roundtrip(start, cycles, load_profile, pv_peak, folder)
    compared = [label for label, lines in replayed.items() if lines]
    differences = trace_differences(live, replayed)
    if not compared:
        print("ERROR: {}: the replay re-planned on no runs, so nothing was compared".format(name))
        return 1
    if differences:
        label, live_lines, replayed_lines = differences[0]
        print("ERROR: {}: {} of {} replayed re-plans differ from the log, first at {}".format(name, len(differences), len(compared), label))
        for live_line, replayed_line in zip(live_lines or [], replayed_lines):
            if live_line != replayed_line:
                print("  log:    {}\n  replay: {}".format(live_line, replayed_line))
                break
        return 1
    print("{}: {} replayed re-plans match the log line for line".format(name, len(compared)))
    return 0


def run_replay_roundtrip_tests(my_predbat):
    """An hour of the main loop against the dummy inverter with flat load, replayed from a yaml taken after the first cycle."""
    return check_roundtrip("Round trip, one hour", datetime(2026, 6, 21, 6, 0), 13, [0.5], 4.0)
