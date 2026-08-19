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
from tests.test_infra import run_async


class FakeAnnualStorage:
    """Minimal in-memory storage stub with async save/load, used to prove nothing is cached on a truncated download."""

    def __init__(self):
        """Start with an empty in-memory store and no recorded saves."""
        self.store = {}
        self.saved = False

    async def load(self, namespace, key):
        """Return the previously saved value for a key, or None if nothing was saved."""
        return self.store.get((namespace, key))

    async def save(self, namespace, key, value, format="json"):
        """Record that a save happened and remember the value under the given key."""
        self.saved = True
        self.store[(namespace, key)] = value


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

    print("Test: daily_kwh records a missing day in missing_days, consistent with minute_profile")
    bare_daily = OctopusConsumptionLoadProfile(api_key="x", account_id="A-1", log=print)
    bare_daily.daily_kwh(date(2025, 3, 12))
    if date(2025, 3, 12) not in bare_daily.missing_days:
        print("  ERROR: daily_kwh must record a missing day in missing_days just like minute_profile does")
        failed = True

    print("Test: a slot reported twice (e.g. the autumn clock-change day) discards that day entirely")
    duplicate_slot_results = list(results) + [dict(results[0])]
    duplicate_slot_parsed = parse_consumption_results(duplicate_slot_results)
    if date(2025, 3, 10) in duplicate_slot_parsed:
        print("  ERROR: a day with a slot reported twice must be discarded, not silently undercounted")
        failed = True

    print("Test: a page request failing mid-download is not cached and fetch() reports failure")
    calls = []

    async def fake_get_json(session, url):
        """Return a canned meter resolution, one good consumption page with a next link, then a failure."""
        calls.append(url)
        if len(calls) == 1:
            return {"properties": [{"electricity_meter_points": [{"mpan": "1200000000000", "is_export": False, "meters": [{"serial_number": "S1"}]}]}]}
        if len(calls) == 2:
            return {"results": results, "next": "https://api.octopus.energy/v1/electricity-meter-points/x/meters/y/consumption/?page=2"}
        return None

    fake_storage = FakeAnnualStorage()
    truncated_source = OctopusConsumptionLoadProfile(api_key="x", account_id="A-2", log=print, storage=fake_storage)
    truncated_source._get_json = fake_get_json
    fetch_result = run_async(truncated_source.fetch(2025))
    if fetch_result is not False:
        print("  ERROR: a download that fails mid-pagination must return False, got {}".format(fetch_result))
        failed = True
    if fake_storage.saved:
        print("  ERROR: a truncated download must not be written to storage")
        failed = True

    return failed
