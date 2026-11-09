# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Today's figures on a clock-change day (GH#5760).

midnight_utc is now with the clock set to 00:00, so on a clock-change day it is not the real start of the day:
after the autumn change it is an hour after it, after the spring change an hour before. minutes_now counts from
midnight_utc, while history from minute_data(backwards=True) is keyed by real minutes back from now, so a "today so
far" read of that history at minutes_now dropped today's first hour (autumn) or counted yesterday's last (spring).

The scenarios run at 02:00 GMT on 25 Oct 2026 (autumn: 180 real minutes since midnight, minutes_now 120) and
03:00 BST on 28 Mar 2027 (spring: 120 real minutes, minutes_now 180), with a constant 0.01 kWh a minute in every
history, so the right "today" total is 1.8 kWh in autumn and 1.2 kWh in spring - and the old reads gave the reverse.
"""

import copy
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytz

import re

from utils import clock_change_minutes, local_day_end_minute, start_of_local_day

LONDON = pytz.timezone("Europe/London")
PER_MINUTE = 0.01
# (label, wall-clock now in London, real minutes since the start of the day, minutes_now)
CHANGE_DAYS = [
    ("autumn", datetime(2026, 10, 25, 2, 0), 180, 120),
    ("spring", datetime(2027, 3, 28, 3, 0), 120, 180),
]


def _london(wall):
    """A London wall-clock time as update_time() builds now_utc: localised, then normalised."""
    return LONDON.normalize(LONDON.localize(wall))


def _cumulative_back(minutes=3 * 24 * 60):
    """A cumulative history as minute_data(backwards=True) returns it, keyed by real minutes back from now, PER_MINUTE a minute."""
    total = minutes * PER_MINUTE
    return {minute_back: total - minute_back * PER_MINUTE for minute_back in range(minutes + 1)}


def _check(label, ok, detail=""):
    """Print a pass/fail line and return True when the check failed."""
    if ok:
        print("  PASS: {}".format(label))
        return False
    print("  ERROR: {} {}".format(label, detail))
    return True


def _test_helpers():
    """clock_change_minutes() and start_of_local_day() on change days, ordinary days, and zones that change at midnight."""
    print("clock change day: helpers")
    failed = False
    cases = [
        (_london(datetime(2026, 10, 25, 2, 0)), LONDON, -60),
        (_london(datetime(2026, 10, 25, 0, 30)), LONDON, 0),  # before the change, still BST
        (_london(datetime(2026, 10, 25, 23, 55)), LONDON, -60),
        (_london(datetime(2027, 3, 28, 3, 0)), LONDON, 60),
        (_london(datetime(2027, 3, 28, 0, 30)), LONDON, 0),  # before the change, still GMT
        (_london(datetime(2026, 10, 26, 2, 0)), LONDON, 0),
        (datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc), LONDON, -60),  # now given in UTC, as some tests do
        (datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc), LONDON, 0),  # summer, now in UTC: no change since midnight
        (datetime(2026, 10, 25, 2, 0, tzinfo=ZoneInfo("Europe/London")), ZoneInfo("Europe/London"), -60),
        (datetime(2027, 3, 28, 3, 0, tzinfo=ZoneInfo("Europe/London")), ZoneInfo("Europe/London"), 60),
    ]
    # Atlantic/Azores goes back at 01:00 to 00:00, so 00:00 happens twice; the day starts at the first one
    azores = pytz.timezone("Atlantic/Azores")
    cases.append((azores.localize(datetime(2026, 10, 25, 10, 0)), azores, -60))
    cases.append((azores.localize(datetime(2026, 10, 25, 0, 30), is_dst=True), azores, 0))
    # America/Havana goes forward at 00:00 to 01:00 in March, so 00:00 never happens; the day starts at 01:00
    havana = pytz.timezone("America/Havana")
    cases.append((havana.localize(datetime(2027, 3, 14, 10, 0)), havana, 60))
    for now, tz, expected in cases:
        got = clock_change_minutes(now, tz)
        failed |= _check("clock_change_minutes at {} in {} is {}".format(now, tz, expected), got == expected, "got {}".format(got))
    for now, expected in (
        (_london(datetime(2026, 10, 25, 0, 30)), 1500),
        (_london(datetime(2026, 10, 25, 2, 0)), 1440),
        (_london(datetime(2027, 3, 28, 0, 30)), 1380),
        (_london(datetime(2027, 3, 28, 3, 0)), 1440),
        (_london(datetime(2026, 10, 26, 2, 0)), 1440),
    ):
        got = local_day_end_minute(now, LONDON)
        failed |= _check("local_day_end_minute at {} is {}".format(now, expected), got == expected, "got {}".format(got))
    start = start_of_local_day(date(2026, 10, 25), azores)
    failed |= _check("Azores autumn day starts at the first 00:00 (UTC+0)", start.utcoffset() == timedelta(0), "got {}".format(start))
    start = start_of_local_day(date(2027, 3, 14), havana)
    failed |= _check("Havana spring day starts at 01:00 CDT, 05:00 UTC", start.astimezone(timezone.utc) == datetime(2027, 3, 14, 5, 0, tzinfo=timezone.utc), "got {}".format(start.astimezone(timezone.utc)))
    return failed


def _set_clock(my_predbat, wall):
    """Put my_predbat's clock where update_time() would at the London wall time wall."""
    now = _london(wall)
    my_predbat.local_tz = LONDON
    my_predbat.now_utc = now
    my_predbat.midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((now - my_predbat.midnight_utc).total_seconds() / 60)


def _test_today_cost(my_predbat):
    """today_cost() counts the real day: import, cost, standing charge once, and the battery level at real midnight."""
    print("clock change day: today_cost")
    failed = False
    rate = 20.0
    standing = 50.0
    for label, wall, real_minutes, minutes_now in CHANGE_DAYS:
        _set_clock(my_predbat, wall)
        failed |= _check("{} minutes_now is {}".format(label, minutes_now), my_predbat.minutes_now == minutes_now, "got {}".format(my_predbat.minutes_now))
        my_predbat.num_cars = 0
        my_predbat.octopus_slots = []
        my_predbat.carbon_enable = False
        my_predbat.metric_standing_charge = standing
        my_predbat.rate_import = {minute: rate for minute in range(-24 * 60, 2 * 24 * 60)}
        my_predbat.rate_export = {minute: 0.0 for minute in range(-24 * 60, 2 * 24 * 60)}
        # SoC history: 2.0 kWh at the real start of the day, 9.0 an hour either side of it, 5.0 now
        my_predbat.soc_kwh_history = {minute_back: 9.0 for minute_back in range(0, 2 * 24 * 60)}
        my_predbat.soc_kwh_history[0] = 5.0
        my_predbat.soc_kwh_history[real_minutes] = 2.0
        logged = []
        log_before = my_predbat.log
        my_predbat.log = lambda msg, *a, **kw: logged.append(msg)
        try:
            history = _cumulative_back()
            cost = my_predbat.today_cost(history, {}, {}, history, save=True)
        finally:
            my_predbat.log = log_before
        expected_import = real_minutes * PER_MINUTE
        expected_cost = standing + expected_import * rate
        cost_value = cost[0] if isinstance(cost, tuple) else cost
        failed |= _check("{} cost_today covers the real day ({} kWh, {}p)".format(label, expected_import, expected_cost), abs(cost_value - expected_cost) < 0.01, "got {}".format(cost_value))
        midnight_line = next((line for line in logged if line.startswith("Battery level now")), "")
        failed |= _check("{} battery level read at the real midnight".format(label), "at midnight 2.0kWh" in midnight_line, "log: {}".format(midnight_line))
    return failed


def _test_load_today_comparison(my_predbat):
    """load_today_comparison() counts today's actual load from the real start of the day, adds no forecast from before
    midnight_utc, and waits for three real hours of data before trusting the divergence."""
    print("clock change day: load_today_comparison")
    failed = False
    names = (
        "car_charging_hold",
        "car_charging_energy",
        "iboost_energy_subtract",
        "iboost_energy_today",
        "base_load",
        "load_forecast_only",
        "days_previous",
        "days_previous_weight",
        "load_minutes_age",
        "calculate_inday_adjustment",
        "metric_inday_adjust_damping",
        "manual_load_adjust",
        "load_scaling",
        "load_scaling_dynamic",
        "log",
    )
    saved = {name: getattr(my_predbat, name, None) for name in names}
    saved_history = my_predbat.__dict__.get("get_history_wrapper")
    logged = []
    try:
        my_predbat.car_charging_hold = False
        my_predbat.car_charging_energy = None
        my_predbat.iboost_energy_subtract = False
        my_predbat.iboost_energy_today = None
        my_predbat.base_load = 0.0
        my_predbat.load_forecast_only = False
        my_predbat.days_previous = [1]
        my_predbat.days_previous_weight = [1.0]
        my_predbat.load_minutes_age = 3
        my_predbat.calculate_inday_adjustment = True
        my_predbat.metric_inday_adjust_damping = 1.0
        my_predbat.manual_load_adjust = {}
        my_predbat.load_scaling = 1.0
        my_predbat.load_scaling_dynamic = {}
        my_predbat.log = lambda msg, *a, **kw: logged.append(msg)
        my_predbat.get_history_wrapper = lambda entity_id, **kw: None
        # Autumn, 02:00 GMT: 180 real minutes since midnight at 0.02 kWh a minute, earlier days at 0.01
        _set_clock(my_predbat, datetime(2026, 10, 25, 2, 0))
        per_minute = {minute_back: (0.02 if minute_back < 180 else 0.01) for minute_back in range(4 * 24 * 60)}
        total = sum(per_minute.values())
        load_minutes = {}
        running = total
        for minute_back in range(4 * 24 * 60 + 1):
            load_minutes[minute_back] = running
            running -= per_minute.get(minute_back, 0.0)
        # A load forecast with nothing in it but tonight's last hour (plan minutes 1380-1439)
        load_forecast = {}
        cumulative = 0.0
        for minute in range(0, 2 * 24 * 60):
            load_forecast[minute] = cumulative
            if 1380 <= minute < 1440:
                cumulative += 0.1
        my_predbat.load_today_comparison(load_minutes, load_forecast, {}, {}, minutes_now=my_predbat.minutes_now, save=True)
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
        if saved_history is None:
            my_predbat.__dict__.pop("get_history_wrapper", None)
        else:
            my_predbat.get_history_wrapper = saved_history
    actual_line = next((line for line in logged if line.startswith("Today's actual load so far")), "")
    predicted_line = next((line for line in logged if line.startswith("Today's predicted so far")), "")
    divergence_line = next((line for line in logged if line.startswith("Today's load divergence")), "")
    actual = float(re.search(r"so far ([\d.]+)kWh", actual_line).group(1)) if actual_line else None
    forecast_extra = float(re.search(r"and ([\d.]+)kWh forecast extra", predicted_line).group(1)) if predicted_line else None
    divergence = float(re.search(r"divergence ([\d.]+)%", divergence_line).group(1)) if divergence_line else None
    # 180 real minutes at 0.02 is 3.6 kWh (each 5-minute bucket reads from a minute earlier, so the first one takes 4 of
    # yesterday's minutes at 0.01, as on any day); the 120 minutes of the plan axis alone would give 2.4
    failed |= _check("autumn actual load so far covers the 180 real minutes (~3.6 kWh)", actual is not None and abs(actual - 3.6) < 0.1, "log: {}".format(actual_line))
    failed |= _check("no forecast added for the hour before midnight_utc", forecast_extra is not None and forecast_extra < 0.001, "log: {}".format(predicted_line))
    failed |= _check("divergence measured once three real hours of data exist", divergence is not None and divergence > 100.5, "log: {}".format(divergence_line))
    return failed


def _test_fetch_load_today(my_predbat):
    """fetch_load_today() reads today's load back to the real start of the day."""
    print("clock change day: fetch_load_today")
    failed = False
    for label, wall, real_minutes, _minutes_now in CHANGE_DAYS:
        _set_clock(my_predbat, wall)
        history = _cumulative_back()
        minute_data_load_before = my_predbat.__dict__.get("minute_data_load")
        fill_before = my_predbat.__dict__.get("fill_load_from_history")
        my_predbat.minute_data_load = lambda *a, **kw: (dict(history), 3)
        my_predbat.fill_load_from_history = lambda load_minutes, age: (load_minutes, age)
        try:
            my_predbat.fetch_load_today()
        finally:
            for name, value in (("minute_data_load", minute_data_load_before), ("fill_load_from_history", fill_before)):
                if value is None:
                    my_predbat.__dict__.pop(name, None)
                else:
                    setattr(my_predbat, name, value)
        expected = real_minutes * PER_MINUTE
        failed |= _check("{} load today so far is {} kWh".format(label, expected), abs(my_predbat.load_minutes_now - expected) < 0.001, "got {}".format(my_predbat.load_minutes_now))
    return failed


def _test_predheat(my_predbat):
    """PredHeat's cost today runs from the real start of the day."""
    print("clock change day: predheat today_cost")
    from predheat import PredHeat

    failed = False
    predheat = PredHeat(my_predbat)
    predheat.prefix = my_predbat.prefix
    for label, wall, real_minutes, minutes_now in CHANGE_DAYS:
        now = _london(wall)
        predheat.midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
        predheat.minutes_now = minutes_now
        predheat.day_start_minute = clock_change_minutes(now, LONDON)
        predheat.rate_import = {minute: 10.0 for minute in range(-24 * 60, 2 * 24 * 60)}
        cost = predheat.today_cost(_cumulative_back())
        expected = real_minutes * PER_MINUTE * 10.0
        failed |= _check("{} predheat cost today is {}p".format(label, expected), abs(cost - expected) < 0.01, "got {}".format(cost))
    return failed


def _test_load_ml_baseline():
    """The ML component re-derives today's load back to the real start of the day."""
    print("clock change day: load ML baseline")
    from load_ml_component import LoadMLComponent

    class MockBase:
        """Minimal PredBat stand-in for the ML component on a London clock."""

        def __init__(self, now):
            """Set the clock as update_time() would."""
            self.prefix = "predbat"
            self.config_root = None
            self.now_utc = now
            self.midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self.minutes_now = int((now - self.midnight_utc).total_seconds() / 60)
            self.local_tz = LONDON
            self.args = {}
            self.log_messages = []

        def log(self, msg):
            """Record a log message."""
            self.log_messages.append(msg)

        def get_arg(self, key, default=None, **kwargs):
            """Return the default for every key."""
            return default

    failed = False
    for label, wall, real_minutes, _minutes_now in CHANGE_DAYS:
        base = MockBase(_london(wall))
        component = LoadMLComponent(base, load_ml_enable=True)
        component.load_minutes_now = 24.9
        component.load_minutes_now_time = base.now_utc - timedelta(days=1)
        component.load_data = {minute: 0.05 for minute in range(0, 24 * 60, 5)}
        baseline = component._load_baseline_now()
        expected = real_minutes // 5 * 0.05
        failed |= _check("{} ML load baseline is {} kWh".format(label, expected), abs(baseline - expected) < 0.001, "got {}".format(baseline))
    return failed


def test_clock_change_day(my_predbat):
    """Run the clock-change-day tests, restoring the shared PredBat's clock and state."""
    print("**** Running clock change day tests ****")
    fields = [
        "local_tz",
        "now_utc",
        "midnight_utc",
        "minutes_now",
        "num_cars",
        "octopus_slots",
        "carbon_enable",
        "metric_standing_charge",
        "rate_import",
        "rate_export",
        "soc_kwh_history",
        "load_minutes",
        "load_minutes_age",
        "load_minutes_source",
        "load_minutes_now",
        "load_last_period",
    ]
    saved = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in fields}
    failed = False
    try:
        failed |= _test_helpers()
        failed |= _test_today_cost(my_predbat)
        failed |= _test_load_today_comparison(my_predbat)
        failed |= _test_fetch_load_today(my_predbat)
        failed |= _test_predheat(my_predbat)
        failed |= _test_load_ml_baseline()
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)
    return failed
