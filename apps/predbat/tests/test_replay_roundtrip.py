# fmt: off
# pylint: disable=line-too-long
"""Round trip: run Predbat's main loop against the dummy inverter, then replay the log it wrote and expect the same plans.

SPIKE - checking that update_pred() can be driven cycle by cycle inside the test fixture.
"""

import math
import os
from datetime import datetime, timedelta

import pytz

from dummy_inverter import DummyInverter
from tests.replay_forward import replay_forward

START = datetime(2026, 6, 21, 6, 0)
CYCLES = 4


def set_clock(my_predbat, when):
    """Point update_time() at when (local time) by setting the clock skew from the real clock."""
    tz = pytz.timezone(my_predbat.args.get("timezone", "Europe/London"))
    target = tz.localize(when)
    now = datetime.now(tz)
    # Rounded up, so update_time()'s floor to the 5-minute step lands on when rather than the step before it
    my_predbat.args["clock_skew"] = int(math.ceil((target - now).total_seconds() / 60.0))
    return target


def sync_controls(my_predbat, dummy):
    """Copy what Predbat wrote to the dummy's control entities into the dummy's model."""
    for direction, field in dummy.control_fields():
        entity_id = dummy.control_info(direction, field)[0]
        value = my_predbat.ha_interface.get_state(entity_id)
        if value is not None:
            dummy.update_control(entity_id, value)


# kWh per minute behind each cumulative dummy sensor, and the total each starts the simulation at
HISTORY_RATES = {"load_lifetime": 0.5 / 60, "pv_lifetime": 1.0 / 60, "grid_import_lifetime": 0.3 / 60, "grid_export_lifetime": 0.2 / 60}
HISTORY_DAYS = 8


def make_history(my_predbat, dummy):
    """Return a get_history replacement: steady ramps for the dummy's cumulative sensors, the current state otherwise."""
    tz = pytz.timezone(my_predbat.args.get("timezone", "Europe/London"))

    def get_history(entity_id, now=None, days=30):
        """Synthetic history ending at now, oldest first, five minutes apart."""
        now = my_predbat.now_utc
        current = my_predbat.ha_interface.get_state(entity_id)
        rate = None
        for name, per_minute in HISTORY_RATES.items():
            if entity_id == dummy.entity("sensor", name):
                rate = per_minute
        points = []
        for back in range(min(days, HISTORY_DAYS) * 24 * 60, -1, -5):
            stamp = (now - timedelta(minutes=back)).astimezone(tz).isoformat()
            if rate is not None:
                state = round(float(current or 0) - rate * back, 4)
            else:
                # Status-like entities are read as text, and an unset one as unknown
                state = "unknown" if current is None else current
            points.append({"state": state, "last_updated": stamp, "last_changed": stamp, "attributes": {"unit_of_measurement": "kWh"}})
        return [points]

    return get_history


def run_replay_roundtrip_tests(my_predbat):
    """Drive a few update_pred() cycles against the dummy inverter, writing the log to a file."""
    log_path = os.path.abspath("roundtrip_predbat.log")
    lines = []
    original_log = my_predbat.log

    def log(message, *args, **kwargs):
        """Keep each log line with a timestamp in the live log's format, then log as normal."""
        lines.append("{}: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"), message))
        return original_log(message, *args, **kwargs)

    my_predbat.log = log
    my_predbat.download_predbat_releases = lambda *args, **kwargs: None
    dummy = DummyInverter(my_predbat, config={"battery_size": 10, "soc_initial": 50, "load": 0.5, "pv_peak": 4.0})
    dummy.totals.update({"load": 1000.0, "pv": 2000.0, "import": 600.0, "export": 400.0})
    original_history = my_predbat.ha_interface.get_history
    my_predbat.ha_interface.get_history = make_history(my_predbat, dummy)
    failed = 0
    saved_load_forecast = None
    try:
        my_predbat.args["rates_import"] = [{"start": "00:30:00", "end": "05:30:00", "rate": 7.5}, {"start": "05:30:00", "end": "00:30:00", "rate": 30.0}]
        my_predbat.args["rates_export"] = [{"start": "00:00:00", "end": "00:00:00", "rate": 15.0}]
        my_predbat.args["days_previous_auto"] = True
        my_predbat.args["load_forecast_only"] = False
        saved_load_forecast = my_predbat.args.pop("load_forecast", None)
        # update_time() logs a run marker, so the harness sets the clock directly rather than calling it
        my_predbat.now_utc = set_clock(my_predbat, START)
        dummy.publish_controls()
        dummy.automatic_config()
        for cycle in range(CYCLES):
            when = START + timedelta(minutes=5 * cycle)
            my_predbat.now_utc = set_clock(my_predbat, when)
            sync_controls(my_predbat, dummy)
            dummy.step(dummy.minutes_absolute())
            dummy.publish()
            # The inverter's clock is compared with the real one, not the simulated one
            my_predbat.ha_interface.set_state(dummy.entity("sensor", "time"), datetime.now(pytz.timezone(my_predbat.args.get("timezone", "Europe/London"))).strftime("%Y-%m-%d %H:%M:%S"))
            print("**** Round trip cycle {} at {} ****".format(cycle, when))
            my_predbat.update_pred(scheduled=True)
            if cycle == 0:
                my_predbat.config_root = "./"
                my_predbat.config_root_p = "./"
                my_predbat.create_debug_yaml()
                yaml_path = os.path.abspath("debug/predbat_debug_{}.yaml".format(my_predbat.now_utc.strftime("%H_%M_%S")))
    finally:
        my_predbat.__dict__.pop("log", None)
        del my_predbat.download_predbat_releases
        my_predbat.ha_interface.get_history = original_history
        with open(log_path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        print("Wrote {} log lines to {}".format(len(lines), log_path))
    replay_lines = []

    def replay_log(message, *args, **kwargs):
        """Keep the replay's own log lines too."""
        replay_lines.append("{}: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"), message))
        return original_log(message, *args, **kwargs)

    my_predbat.log = replay_log
    try:
        rows = replay_forward(my_predbat, yaml_path, log_path, quiet=False)
    finally:
        my_predbat.__dict__.pop("log", None)
        if saved_load_forecast is not None:
            my_predbat.args["load_forecast"] = saved_load_forecast
        with open("roundtrip_replay.log", "w") as handle:
            handle.write("\n".join(replay_lines) + "\n")
    print("Replayed {} runs".format(len(rows)))
    return failed
