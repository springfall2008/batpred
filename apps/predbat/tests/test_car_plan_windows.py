# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime
import pytz
from utils import parse_car_plan_windows, in_car_plan_window

LONDON = pytz.timezone("Europe/London")


def _window(start, end):
    """A planned-attribute entry as output.py publishes it."""
    return {"start": start, "end": end, "kwh": 5.0, "average": 20.0, "cost": 1.0}


def test_car_plan_windows(my_predbat=None):
    """
    utils.parse_car_plan_windows() rebuilds the year on the car charging plan (published as
    "%m-%d %H:%M:%S") around now. Shared by every charger that follows the plan - myenergi,
    GivEnergy EVC, Ohme and the gateway EVC (#269, #5120 review).
    """
    failed = False
    print("**** Testing parse_car_plan_windows ****")

    cases = [
        # (name, now, start, end, expected start year, expected end year, now inside)
        ("ordinary window", datetime(2026, 6, 15, 14, 0), "06-15 13:00:00", "06-15 15:00:00", 2026, 2026, True),
        ("New Year window read before midnight", datetime(2026, 12, 31, 23, 45), "12-31 23:30:00", "01-01 01:30:00", 2026, 2027, True),
        # Once now itself is in January the Dec 31 start belongs to the previous year, or the window
        # lands a year in the future and a charging car is stopped mid-window
        ("New Year window read after midnight", datetime(2027, 1, 1, 0, 15), "12-31 23:30:00", "01-01 01:30:00", 2026, 2027, True),
        # A window wholly in January, read on 31 December, parses into January of the year ending and
        # has to move forward a year
        ("January window read on 31 December", datetime(2026, 12, 31, 23, 30), "01-01 00:30:00", "01-01 02:00:00", 2027, 2027, False),
        # A long window whose start is days old is still the current one, not a year rollover (#269)
        ("long active window", datetime(2026, 6, 16, 23, 5), "06-14 00:10:00", "06-17 02:00:00", 2026, 2026, True),
        ("midnight-crossing window", datetime(2026, 6, 15, 23, 30), "06-15 23:00:00", "06-16 00:30:00", 2026, 2026, True),
        # strptime with no year assumes 1900, which has no 29 February - the window used to be dropped
        ("leap day window", datetime(2028, 2, 29, 1, 0), "02-29 00:30:00", "02-29 05:00:00", 2028, 2028, True),
        ("window starting on leap day, read on 1 March", datetime(2028, 3, 1, 0, 30), "02-29 23:30:00", "03-01 02:00:00", 2028, 2028, True),
    ]
    for name, now_naive, start, end, start_year, end_year, inside in cases:
        now = LONDON.localize(now_naive)
        windows = parse_car_plan_windows([_window(start, end)], now, LONDON)
        if len(windows) != 1:
            print("ERROR: {}: expected one window, got {}".format(name, windows))
            failed = True
            continue
        start_dt, end_dt = windows[0]
        if (start_dt.year, end_dt.year) != (start_year, end_year) or in_car_plan_window(windows, now) != inside:
            print("ERROR: {}: got start={} end={} (now {}, inside {})".format(name, start_dt, end_dt, now, in_car_plan_window(windows, now)))
            failed = True

    # A malformed entry, or a stale 29 February one that has no counterpart in the neighbouring year,
    # is skipped without costing the rest of the plan
    now = LONDON.localize(datetime(2028, 9, 1, 12, 0))
    planned = [_window("not-a-date", "09-01 13:00:00"), {"start": None, "end": "09-01 13:00:00"}, {"end": "09-01 13:00:00"}, _window("02-29 01:00:00", "02-29 02:00:00"), _window("09-01 11:00:00", "09-01 13:00:00")]
    windows = parse_car_plan_windows(planned, now, LONDON)
    if len(windows) != 1 or not in_car_plan_window(windows, now):
        print("ERROR: malformed entries should be skipped and the valid window kept, got {}".format(windows))
        failed = True

    if not failed:
        print("**** All parse_car_plan_windows tests PASSED ****")
    return failed
