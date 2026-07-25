# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction load profile sources."""

import calendar
from datetime import date

from annual_load import OctopusConsumptionLoadProfile, SyntheticLoadProfile, build_load_forecast, parse_consumption_results, tilt_shape
from annual_profiles import DAY_BAND_SLOTS, NIGHT_BAND_SLOTS, half_hour_shape


def test_annual_load(my_predbat):
    """Verify the synthetic load profile preserves totals and tilts correctly."""
    failed = False
    print("**** Testing annual_load ****")

    print("Test: tilt_shape preserves the daily total exactly")
    base = half_hour_shape()
    for direction in ["night", "day", "flat"]:
        tilted = tilt_shape(base, direction)
        total = sum(tilted)
        if abs(total - 1.0) > 1e-9:
            print("  ERROR: tilt '{}' changed the total to {}".format(direction, total))
            failed = True
        if any(value < 0 for value in tilted):
            print("  ERROR: tilt '{}' produced a negative slot".format(direction))
            failed = True

    print("Test: tilt 'night' moves energy into the night band")
    night_tilted = tilt_shape(base, "night")
    base_night = sum(base[slot] for slot in NIGHT_BAND_SLOTS)
    tilted_night = sum(night_tilted[slot] for slot in NIGHT_BAND_SLOTS)
    if tilted_night <= base_night:
        print("  ERROR: night tilt should raise the night band from {} to more, got {}".format(base_night, tilted_night))
        failed = True

    print("Test: tilt 'day' moves energy into the day band")
    day_tilted = tilt_shape(base, "day")
    base_day = sum(base[slot] for slot in DAY_BAND_SLOTS)
    tilted_day = sum(day_tilted[slot] for slot in DAY_BAND_SLOTS)
    if tilted_day <= base_day:
        print("  ERROR: day tilt should raise the day band from {} to more, got {}".format(base_day, tilted_day))
        failed = True

    print("Test: tilt 'flat' is a no-op")
    flat_tilted = tilt_shape(base, "flat")
    if flat_tilted != base:
        print("  ERROR: flat tilt should leave the shape unchanged")
        failed = True

    print("Test: the twelve monthly totals sum to annual_kwh")
    annual_kwh = 3800.0
    source = SyntheticLoadProfile(annual_kwh=annual_kwh, shape="flat", year=2025)
    year_total = 0.0
    for month in range(1, 13):
        days_in_month = calendar.monthrange(2025, month)[1]
        month_total = sum(source.daily_kwh(date(2025, month, day)) for day in range(1, days_in_month + 1))
        year_total += month_total
    if abs(year_total - annual_kwh) > 1e-6:
        print("  ERROR: twelve months summed to {}, expected {}".format(year_total, annual_kwh))
        failed = True

    print("Test: January daily consumption exceeds July")
    january = source.daily_kwh(date(2025, 1, 15))
    july = source.daily_kwh(date(2025, 7, 15))
    if january <= july:
        print("  ERROR: January daily {} should exceed July daily {}".format(january, july))
        failed = True

    print("Test: minute_profile has 1440 entries summing to the day's kWh")
    day = date(2025, 3, 10)
    profile = source.minute_profile(day)
    if len(profile) != 1440:
        print("  ERROR: expected 1440 minutes, got {}".format(len(profile)))
        failed = True
    if abs(sum(profile) - source.daily_kwh(day)) > 1e-9:
        print("  ERROR: minute profile sums to {}, expected {}".format(sum(profile), source.daily_kwh(day)))
        failed = True

    print("Test: build_load_forecast produces a cumulative series Predbat can difference")
    forecast = build_load_forecast(source, date(2025, 3, 10), 2)
    if forecast.get(0) != 0.0:
        print("  ERROR: cumulative series must start at 0, got {}".format(forecast.get(0)))
        failed = True
    if 2 * 1440 not in forecast:
        print("  ERROR: cumulative series must include the final boundary minute {}".format(2 * 1440))
        failed = True
    for minute in range(1, 2 * 1440 + 1):
        if forecast[minute] < forecast[minute - 1] - 1e-12:
            print("  ERROR: cumulative series decreased at minute {}".format(minute))
            failed = True
            break
    expected_two_days = source.daily_kwh(date(2025, 3, 10)) + source.daily_kwh(date(2025, 3, 11))
    if abs(forecast[2 * 1440] - expected_two_days) > 1e-9:
        print("  ERROR: two-day total {} expected {}".format(forecast[2 * 1440], expected_two_days))
        failed = True

    print("Test: differencing the cumulative series recovers the per-minute profile")
    first_minute = forecast[1] - forecast[0]
    if abs(first_minute - profile[0]) > 1e-12:
        print("  ERROR: differenced minute 0 gave {}, expected {}".format(first_minute, profile[0]))
        failed = True

    print("Test: a zero annual figure produces a zero profile rather than dividing by zero")
    zero_source = SyntheticLoadProfile(annual_kwh=0.0, shape="flat", year=2025)
    if sum(zero_source.minute_profile(day)) != 0.0:
        print("  ERROR: zero annual kWh should give a zero profile")
        failed = True

    return failed


def test_annual_load_octopus(my_predbat):
    """Verify Octopus consumption parsing and its fallback behaviour."""
    failed = False
    print("**** Testing annual_load Octopus source ****")

    print("Test: parse_consumption_results maps half-hourly readings onto dates")
    results = []
    for slot in range(48):
        hour = slot // 2
        minute = 30 * (slot % 2)
        results.append(
            {
                "consumption": 0.25,
                "interval_start": "2025-03-10T{:02d}:{:02d}:00Z".format(hour, minute),
                "interval_end": "2025-03-10T{:02d}:{:02d}:00Z".format(hour, minute),
            }
        )
    parsed = parse_consumption_results(results)
    target = date(2025, 3, 10)
    if target not in parsed:
        print("  ERROR: expected {} in parsed output, got {}".format(target, list(parsed.keys())))
        failed = True
    elif len(parsed[target]) != 48:
        print("  ERROR: expected 48 slots, got {}".format(len(parsed[target])))
        failed = True
    elif abs(sum(parsed[target]) - 12.0) > 1e-9:
        print("  ERROR: expected 12.0 kWh for the day, got {}".format(sum(parsed[target])))
        failed = True

    print("Test: a partial day is reported as missing rather than silently understated")
    partial = parse_consumption_results(results[:20])
    if date(2025, 3, 10) in partial:
        print("  ERROR: a day with only 20 of 48 slots must not be returned as complete")
        failed = True

    print("Test: minute_profile falls back to the synthetic source for a missing day")
    fallback = SyntheticLoadProfile(annual_kwh=3800.0, shape="flat", year=2025)
    source = OctopusConsumptionLoadProfile(api_key="x", account_id="A-1", log=print, fallback=fallback)
    source.consumption = parse_consumption_results(results)

    present = source.minute_profile(date(2025, 3, 10))
    if abs(sum(present) - 12.0) > 1e-9:
        print("  ERROR: present day should use real data summing to 12.0, got {}".format(sum(present)))
        failed = True

    absent = source.minute_profile(date(2025, 3, 11))
    if abs(sum(absent) - fallback.daily_kwh(date(2025, 3, 11))) > 1e-9:
        print("  ERROR: missing day should fall back to synthetic, got {}".format(sum(absent)))
        failed = True
    if date(2025, 3, 11) not in source.missing_days:
        print("  ERROR: a fallback day must be recorded in missing_days")
        failed = True

    print("Test: no fallback and no data yields None so the caller can exclude the day")
    bare = OctopusConsumptionLoadProfile(api_key="x", account_id="A-1", log=print)
    if bare.minute_profile(date(2025, 3, 11)) is not None:
        print("  ERROR: with no data and no fallback minute_profile must return None")
        failed = True

    return failed
