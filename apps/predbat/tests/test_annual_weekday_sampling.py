# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the weekday-spread annual sample day selector."""

import calendar
from datetime import date, timedelta

from annual import select_samples


class FakeWeather:
    """A stub WeatherYear exposing only what select_samples needs."""

    def __init__(self, daily):
        """Hold a mapping of date to daily actual PV kWh."""
        self.daily = daily

    def has_actual(self, day):
        """Return True when the date has actual PV data."""
        return day in self.daily

    def daily_actual_kwh(self, day):
        """Return the actual PV kWh for the date."""
        return self.daily.get(day, 0.0)


def full_month(year, month, missing=()):
    """Return a FakeWeather covering a whole month plus two buffer days, minus `missing` days."""
    daily = {}
    days_in_month = calendar.monthrange(year, month)[1]
    for day_number in range(1, days_in_month + 1):
        if day_number in missing:
            continue
        daily[date(year, month, day_number)] = float(day_number)
    for offset in range(3):
        daily[date(year, month, days_in_month) + timedelta(days=offset + 1)] = 1.0
    return FakeWeather(daily)


def test_annual_weekday_sampling(my_predbat):
    """Weekday-spread sampling covers distinct weekdays spread across the weeks of the month."""
    failed = False

    print("Test: five samples cover five distinct weekdays spread across all seven")
    weather = full_month(2026, 7, ())
    samples = select_samples(weather, 2026, 7, 5, sampling="weekday_spread")
    days = [day for day, _ in samples]
    if len(days) != 5:
        print("  ERROR: expected 5 sample days, got {}".format(len(days)))
        failed = True
    weekdays = sorted({day.weekday() for day in days})
    if len(weekdays) != 5:
        print("  ERROR: expected 5 distinct weekdays, got {}".format(weekdays))
        failed = True
    # _percentile_indices(7, 5) spreads across all seven positions, so at least one
    # weekend day must appear - a Mon-Fri-only draw would bias Agile Outgoing.
    if not any(day.weekday() >= 5 for day in days):
        print("  ERROR: expected at least one weekend day among {}".format(days))
        failed = True

    print("Test: samples come from at least four distinct weeks of the month")
    # Weekday coverage is the actual goal (rate variation across the week), so a target whose
    # week block is already used still takes that block if the alternative is a duplicate
    # weekday - see _weekday_spread_days. With 5 targets and July's short final block (29-31,
    # no Sunday), that means 4 distinct blocks rather than 5.
    blocks = sorted({(day.day - 1) // 7 for day in days})
    if len(blocks) < 4:
        print("  ERROR: expected at least 4 distinct week blocks, got {} from {}".format(blocks, days))
        failed = True

    print("Test: weights sum to the number of days in the month")
    total_weight = sum(weight for _, weight in samples)
    if abs(total_weight - 31.0) > 1e-9:
        print("  ERROR: expected weights summing to 31 for July, got {}".format(total_weight))
        failed = True

    print("Test: selection is deterministic")
    again = [day for day, _ in select_samples(full_month(2026, 7, ()), 2026, 7, 5, sampling="weekday_spread")]
    if again != days:
        print("  ERROR: repeated selection differed: {} then {}".format(days, again))
        failed = True

    print("Test: a 28 day month with 5 samples still returns 5 distinct days")
    february = select_samples(full_month(2026, 2, ()), 2026, 2, 5, sampling="weekday_spread")
    february_days = [day for day, _ in february]
    if len(set(february_days)) != 5:
        print("  ERROR: expected 5 distinct days in February, got {}".format(february_days))
        failed = True
    if abs(sum(weight for _, weight in february) - 28.0) > 1e-9:
        print("  ERROR: February weights should sum to 28, got {}".format(sum(weight for _, weight in february)))
        failed = True

    print("Test: a missing target day falls back within its own week")
    # Remove the first seven days' worth of candidates except one, forcing the fallback.
    sparse = select_samples(full_month(2026, 7, (1, 2, 3, 4, 5, 6)), 2026, 7, 5, sampling="weekday_spread")
    sparse_days = [day for day, _ in sparse]
    if len(set(sparse_days)) != 5:
        print("  ERROR: expected 5 distinct days despite missing candidates, got {}".format(sparse_days))
        failed = True
    if any(day.day in (1, 2, 3, 4, 5, 6) for day in sparse_days):
        print("  ERROR: a day with no weather data was sampled: {}".format(sparse_days))
        failed = True

    print("Test: the default sampling mode is unchanged")
    default_days = [day for day, _ in select_samples(full_month(2026, 7, ()), 2026, 7, 2)]
    percentile_days = [day for day, _ in select_samples(full_month(2026, 7, ()), 2026, 7, 2, sampling="percentile")]
    if default_days != percentile_days:
        print("  ERROR: the default mode should equal percentile mode, got {} and {}".format(default_days, percentile_days))
        failed = True

    return failed
