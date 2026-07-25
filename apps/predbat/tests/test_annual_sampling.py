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

from annual import _percentile_indices, select_samples


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


def build_december(kwh_by_day, extra_days=1):
    """Build a FakeWeather covering December plus a buffer into January of the following year."""
    daily = {}
    for day_number, kwh in kwh_by_day.items():
        daily[date(2025, 12, day_number)] = kwh
    for offset in range(extra_days):
        daily[date(2026, 1, 1) + timedelta(days=offset)] = 1.0
    return FakeWeather(daily)


def _check_indices(indices, count, samples, label, failed):
    """Assert that indices from _percentile_indices() are distinct and within range.

    Shared by the degenerate-case checks below so each one only has to state its
    inputs and let this helper verify the two invariants every case must satisfy.
    Returns the (possibly updated) ``failed`` flag.
    """
    if len(set(indices)) != len(indices):
        print("  ERROR: {} produced duplicate indices, got {}".format(label, indices))
        failed = True
    if any(index < 0 or index >= count for index in indices):
        print("  ERROR: {} produced an out-of-range index, got {} for count {}".format(label, indices, count))
        failed = True
    return failed


def test_annual_sampling(my_predbat):
    """Verify percentile sampling, weighting, determinism, degraded months and year boundaries."""
    failed = False
    print("**** Testing annual sample selection ****")

    print("Test: _percentile_indices with no candidates returns nothing")
    if _percentile_indices(0, 2) != []:
        print("  ERROR: expected no indices for a candidate count of 0, got {}".format(_percentile_indices(0, 2)))
        failed = True

    print("Test: _percentile_indices with a single candidate always picks index 0")
    for requested in (1, 2, 5):
        single = _percentile_indices(1, requested)
        if single != [0]:
            print("  ERROR: expected [0] for count=1, samples={}, got {}".format(requested, single))
            failed = True

    print("Test: _percentile_indices with more samples requested than candidates stays in range")
    over_requested = _percentile_indices(3, 7)
    failed = _check_indices(over_requested, 3, 7, "samples > count", failed)

    print("Test: _percentile_indices with samples equal to count selects every index")
    exact = _percentile_indices(5, 5)
    if sorted(exact) != list(range(5)):
        print("  ERROR: expected every index 0-4 when samples == count, got {}".format(sorted(exact)))
        failed = True
    failed = _check_indices(exact, 5, 5, "samples == count", failed)

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

    print("Test: a battery-only run still includes the last day of the month when requested")
    # samples_per_month == days_in_month so every calendar day, including the last, is chosen;
    # weather has no data at all, confirming the no-solar branch never needs a following-day check
    no_solar_full_month = select_samples(FakeWeather({}), 2025, 12, 31, has_solar=False)
    if len(no_solar_full_month) != 31:
        print("  ERROR: expected all 31 December calendar days, got {}".format(len(no_solar_full_month)))
        failed = True
    else:
        last_day, last_weight = no_solar_full_month[-1]
        if last_day != date(2025, 12, 31):
            print("  ERROR: expected the last sample to be 31 December, got {}".format(last_day))
            failed = True
        if abs(last_weight - 1.0) > 1e-9:
            print("  ERROR: expected a weight of 1.0 for 31 December, got {}".format(last_weight))
            failed = True

    print("Test: a December selection with solar consults 1 January of the following year")
    december = build_december({day: float(day) for day in range(1, 32)})
    december_days = [day for day, _ in select_samples(december, 2025, 12, 31)]
    if date(2025, 12, 31) not in december_days:
        print("  ERROR: 31 December should be included when 1 January of the following year has data")
        failed = True

    print("Test: a December selection with solar excludes 31 December when the new year has no data")
    december_truncated = build_december({day: float(day) for day in range(1, 32)}, extra_days=0)
    december_truncated_days = [day for day, _ in select_samples(december_truncated, 2025, 12, 31)]
    if date(2025, 12, 31) in december_truncated_days:
        print("  ERROR: 31 December has no following day in the next year and must not be sampled")
        failed = True

    return failed
