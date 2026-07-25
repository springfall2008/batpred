# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for annual prediction sample day selection."""

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


def build_january(kwh_by_day, extra_days=1):
    """Build a FakeWeather covering January plus a buffer into February."""
    daily = {}
    for day_number, kwh in kwh_by_day.items():
        daily[date(2025, 1, day_number)] = kwh
    for offset in range(extra_days):
        daily[date(2025, 2, 1) + timedelta(days=offset)] = 1.0
    return FakeWeather(daily)


def test_annual_sampling(my_predbat):
    """Verify percentile sampling, weighting, determinism and degraded months."""
    failed = False
    print("**** Testing annual sample selection ****")

    # January: day N generates N kWh, so the sorted order is simply day order
    weather = build_january({day: float(day) for day in range(1, 32)})

    print("Test: two samples land on the 25th and 75th percentile days")
    samples = select_samples(weather, 2025, 1, 2)
    if len(samples) != 2:
        print("  ERROR: expected 2 samples, got {}".format(len(samples)))
        failed = True
    else:
        days = [day.day for day, _ in samples]
        # 31 candidates: indices int(31*0.25)=7 and int(31*0.75)=23 -> days 8 and 24
        if days != [8, 24]:
            print("  ERROR: expected days [8, 24], got {}".format(days))
            failed = True

    print("Test: weights sum to the number of days in the month")
    total_weight = sum(weight for _, weight in samples)
    if abs(total_weight - 31.0) > 1e-9:
        print("  ERROR: weights should sum to 31, got {}".format(total_weight))
        failed = True

    print("Test: selection is deterministic")
    if select_samples(weather, 2025, 1, 2) != samples:
        print("  ERROR: repeated selection returned different days")
        failed = True

    print("Test: samples are returned in date order")
    ordered = [day for day, _ in select_samples(weather, 2025, 1, 4)]
    if ordered != sorted(ordered):
        print("  ERROR: samples should be returned in date order, got {}".format(ordered))
        failed = True

    print("Test: samples are distinct")
    four = select_samples(weather, 2025, 1, 4)
    if len({day for day, _ in four}) != 4:
        print("  ERROR: expected 4 distinct days, got {}".format([day for day, _ in four]))
        failed = True

    print("Test: a day without a following day is excluded, since the 48 hour plan needs one")
    truncated = build_january({day: float(day) for day in range(1, 32)}, extra_days=0)
    truncated_days = [day for day, _ in select_samples(truncated, 2025, 1, 2)]
    if date(2025, 1, 31) in truncated_days:
        print("  ERROR: 31 January has no following day and must not be sampled")
        failed = True

    print("Test: a month with fewer candidates than samples uses every candidate")
    sparse = build_january({1: 5.0, 2: 9.0, 3: 1.0})
    sparse_samples = select_samples(sparse, 2025, 1, 4)
    # Day 3 has no following day (4 January is absent), so only days 1 and 2 are usable
    if len(sparse_samples) != 2:
        print("  ERROR: expected 2 usable samples, got {}".format(len(sparse_samples)))
        failed = True
    if abs(sum(weight for _, weight in sparse_samples) - 31.0) > 1e-9:
        print("  ERROR: weights must still sum to 31 when samples are scarce, got {}".format(sum(w for _, w in sparse_samples)))
        failed = True

    print("Test: a month with no usable candidates returns nothing")
    if select_samples(FakeWeather({}), 2025, 1, 2) != []:
        print("  ERROR: a month with no weather data should return no samples")
        failed = True

    print("Test: a battery-only run with no solar falls back to evenly spaced calendar days")
    no_solar = select_samples(FakeWeather({}), 2025, 1, 2, has_solar=False)
    if len(no_solar) != 2:
        print("  ERROR: with no solar, expected 2 calendar samples, got {}".format(len(no_solar)))
        failed = True
    elif [day.day for day in [entry[0] for entry in no_solar]] != [8, 24]:
        print("  ERROR: expected evenly spaced days [8, 24], got {}".format([entry[0].day for entry in no_solar]))
        failed = True

    return failed
