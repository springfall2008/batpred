# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction Open-Meteo weather module."""

import asyncio
from datetime import date, datetime, timedelta

import pytz

from annual_weather import AnnualWeather, percentile

ARRAYS = [{"kwp": 5.0, "declination": 35, "azimuth": 180, "efficiency": 0.95}]
TWO_ARRAYS = [
    {"kwp": 5.0, "declination": 35, "azimuth": 180, "efficiency": 0.95},
    {"kwp": 3.0, "declination": 45, "azimuth": 180, "efficiency": 0.95},
]


class FakeAnnualStorage:
    """Minimal in-memory async Storage stand-in for exercising the cache path with no network I/O."""

    def __init__(self):
        """Start with an empty cache and no recorded saves."""
        self.cache = {}
        self.saved_keys = []

    async def load(self, module, filename):
        """Return the cached payload for a module/filename pair, or None if never saved."""
        return self.cache.get((module, filename))

    async def save(self, module, filename, data, format="json", expiry=None):
        """Record a cache write, keyed by module and filename, ignoring format/expiry."""
        self.cache[(module, filename)] = data
        self.saved_keys.append((module, filename))


def build_hourly(start_day, days, peak_gti):
    """Build a synthetic Open-Meteo hourly payload with a fixed daily irradiance curve."""
    times = []
    gti = []
    temp = []
    wind = []
    curve = [0, 0, 0, 0, 0, 0, 0.1, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0, 0.95, 0.85, 0.7, 0.5, 0.3, 0.1, 0, 0, 0, 0, 0]
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for hour in range(24):
            times.append("{}T{:02d}:00".format(day.isoformat(), hour))
            gti.append(peak_gti * curve[hour])
            temp.append(15.0)
            wind.append(1.5)
    return {"hourly": {"time": times, "global_tilted_irradiance": gti, "temperature_2m": temp, "wind_speed_10m": wind}}


def test_annual_weather(my_predbat):
    """Verify weather fetching, P10 derivation from forecast error, and the fallback path."""
    failed = False
    print("**** Testing annual_weather ****")

    print("Test: percentile picks the expected order statistic")
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    got = percentile(values, 0.10)
    if got != 1.0:
        print("  ERROR: 10th percentile of 1..10 expected 1.0, got {}".format(got))
        failed = True
    if percentile([], 0.10) != 0.0:
        print("  ERROR: percentile of an empty list should be 0.0")
        failed = True

    # Actuals peak at 800, forecast peaks at 1000, so every day's actual/forecast ratio is 0.8
    start = date(2025, 1, 1)
    actual_payload = build_hourly(start, 40, 800.0)
    forecast_payload = build_hourly(start, 40, 1000.0)

    async def fake_fetch(url):
        """Return the archive or forecast payload depending on the host in the URL."""
        if "archive-api" in url:
            return actual_payload
        return forecast_payload

    weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=fake_fetch)
    year = asyncio.run(weather.fetch(2025))

    print("Test: the forecast archive is reported as available")
    if not year.forecast_available:
        print("  ERROR: forecast_available should be True when both payloads parse")
        failed = True

    print("Test: January's P10 ratio reflects the measured 0.8 forecast error")
    ratio = year.p10_ratio(1)
    # Every day in the fixture uses the identical curve, so the day-to-day ratio - and hence
    # the P10 order statistic over it - is fully deterministic. The shared solar model's
    # GTI-dependent cell-temperature derate (Task 1) means this is not the raw 800/1000 GTI
    # ratio: a naive implementation that skipped the temperature derate would land on exactly
    # 0.8, so a tight tolerance around the true measured value also catches that regression.
    if abs(ratio - 0.8162221180559525) > 1e-6:
        print("  ERROR: expected the measured P10 ratio of 0.8162221180559525, got {}".format(ratio))
        failed = True

    print("Test: actual daily energy is below forecast daily energy")
    day = date(2025, 1, 10)
    if not year.has_actual(day):
        print("  ERROR: expected actual data for {}".format(day))
        failed = True
    midnight = pytz.utc.localize(datetime(2025, 1, 10, 0, 0))
    actual_minutes = year.pv_minutes("actual", midnight, 24 * 60)
    forecast_minutes = year.pv_minutes("forecast", midnight, 24 * 60)
    actual_total = sum(actual_minutes.values())
    forecast_total = sum(forecast_minutes.values())
    if actual_total <= 0:
        print("  ERROR: actual PV for {} should be positive, got {}".format(day, actual_total))
        failed = True
    if forecast_total <= actual_total:
        print("  ERROR: forecast total {} should exceed actual total {}".format(forecast_total, actual_total))
        failed = True

    print("Test: pv_minutes covers a 48 hour window and is keyed by absolute minute")
    two_day = year.pv_minutes("actual", midnight, 48 * 60)
    if abs(sum(two_day.values()) - (actual_total + year.daily_actual_kwh(date(2025, 1, 11)))) > 0.01:
        print("  ERROR: the 48 hour window should equal two days of actual energy")
        failed = True

    print("Test: pv_minutes discards minutes at or beyond a window that ends mid-hour")
    # 32.5 hours: deliberately not hour-aligned, and lands inside an hour with non-zero
    # irradiance in the fixture curve (hour 8 of the day, curve value 0.5), so the cut is
    # meaningful rather than falling in an always-zero night hour.
    partial_window_minutes = 32 * 60 + 30
    partial_window = year.pv_minutes("actual", midnight, partial_window_minutes)
    if max(partial_window.keys()) >= partial_window_minutes:
        print("  ERROR: pv_minutes must not emit minutes at or beyond the window length")
        failed = True
    dropped_minute = partial_window_minutes + 10
    if dropped_minute not in two_day:
        print("  ERROR: test fixture problem: minute {} should exist in the full 48 hour window".format(dropped_minute))
        failed = True
    if dropped_minute in partial_window:
        print("  ERROR: minute {} is beyond the requested {} minute window and must be dropped".format(dropped_minute, partial_window_minutes))
        failed = True

    print("Test: pv_minutes_p10 scales the forecast series by the month ratio")
    p10_minutes = year.pv_minutes_p10(midnight, 24 * 60, 1)
    expected = forecast_total * year.p10_ratio(1)
    if abs(sum(p10_minutes.values()) - expected) > 0.01:
        print("  ERROR: P10 total {} expected {}".format(sum(p10_minutes.values()), expected))
        failed = True

    print("Test: a missing forecast archive falls back and records the degradation")

    async def actuals_only_fetch(url):
        """Serve actuals and fail every forecast request."""
        if "archive-api" in url:
            return actual_payload
        return None

    degraded_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=actuals_only_fetch, p10_fallback=0.7)
    degraded = asyncio.run(degraded_weather.fetch(2025))
    if degraded.forecast_available:
        print("  ERROR: forecast_available should be False when the forecast archive is empty")
        failed = True
    if abs(degraded.p10_ratio(1) - 0.7) > 1e-9:
        print("  ERROR: expected the 0.7 fallback ratio, got {}".format(degraded.p10_ratio(1)))
        failed = True
    if 1 not in degraded.fallback_months:
        print("  ERROR: January should be recorded in fallback_months")
        failed = True
    degraded_forecast = degraded.pv_minutes("forecast", midnight, 24 * 60)
    degraded_actual = degraded.pv_minutes("actual", midnight, 24 * 60)
    if abs(sum(degraded_forecast.values()) - sum(degraded_actual.values())) > 1e-9:
        print("  ERROR: with no forecast archive the forecast series must fall back to actuals")
        failed = True

    print("Test: a month with fewer than seven usable days falls back")
    sparse_actual = build_hourly(date(2025, 1, 1), 4, 800.0)
    sparse_forecast = build_hourly(date(2025, 1, 1), 4, 1000.0)

    async def sparse_fetch(url):
        """Serve only four days of data."""
        return sparse_actual if "archive-api" in url else sparse_forecast

    sparse_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=sparse_fetch, p10_fallback=0.7)
    sparse = asyncio.run(sparse_weather.fetch(2025))
    if abs(sparse.p10_ratio(1) - 0.7) > 1e-9:
        print("  ERROR: a four-day month should fall back to 0.7, got {}".format(sparse.p10_ratio(1)))
        failed = True

    print("Test: the P10 order statistic is the 10th percentile day, not the mean or the minimum")
    # Build February directly at the _derive_p10_ratios level, bypassing the GTI/solar-model
    # conversion, so the per-day actual/forecast ratios are exact and independently chosen:
    # 20 days, sorted ratios [0.5, 0.55, 0.9 x18]. With n=20 and fraction=0.10 the order
    # statistic index is ceil(20*0.10)-1 = 1, i.e. the *second* smallest sample (0.55) - clearly
    # distinct from the minimum (0.5), the mean (0.8625) and the median/mode (0.9).
    february_ratios = [0.5, 0.55] + [0.9] * 18
    february_actual_periods = {}
    february_forecast_periods = {}
    for day_index, day_ratio in enumerate(february_ratios, start=1):
        stamp = pytz.utc.localize(datetime(2025, 2, day_index, 12, 0))
        february_forecast_periods[stamp] = 10.0
        february_actual_periods[stamp] = 10.0 * day_ratio
    february_ratio_map, february_fallback_months = weather._derive_p10_ratios(february_actual_periods, february_forecast_periods, True)
    expected_february_p10 = sorted(february_ratios)[1]
    if abs(february_ratio_map.get(2, -1.0) - expected_february_p10) > 1e-9:
        print("  ERROR: expected February's P10 ratio to be the 10th percentile day {}, got {}".format(expected_february_p10, february_ratio_map.get(2)))
        failed = True
    if abs(february_ratio_map.get(2, -1.0) - min(february_ratios)) < 1e-9:
        print("  ERROR: February's P10 ratio must not equal the minimum day's ratio")
        failed = True
    mean_february_ratio = sum(february_ratios) / len(february_ratios)
    if abs(february_ratio_map.get(2, -1.0) - mean_february_ratio) < 1e-9:
        print("  ERROR: February's P10 ratio must not equal the mean of the month's ratios")
        failed = True
    if 2 in february_fallback_months:
        print("  ERROR: 20 usable day-pairs should not trigger the flat-derate fallback")
        failed = True

    print("Test: a cached response is only written when the payload is usable, and a cache hit avoids re-fetching")
    cache_storage = FakeAnnualStorage()
    fetch_calls = {"n": 0}

    async def counting_fetch(url):
        """Return the good fixture payloads while counting how many times it is called."""
        fetch_calls["n"] += 1
        return actual_payload if "archive-api" in url else forecast_payload

    cached_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, storage=cache_storage, fetch_json=counting_fetch)
    cached_year_first = asyncio.run(cached_weather.fetch(2025))
    if len(cache_storage.saved_keys) != 2:
        print("  ERROR: a usable actual payload and a usable forecast payload should both be cached, got {} saves".format(len(cache_storage.saved_keys)))
        failed = True
    calls_after_first_fetch = fetch_calls["n"]
    cached_year_second = asyncio.run(cached_weather.fetch(2025))
    if fetch_calls["n"] != calls_after_first_fetch:
        print("  ERROR: a cache hit should not call fetch_json again, but call count went from {} to {}".format(calls_after_first_fetch, fetch_calls["n"]))
        failed = True
    first_total = sum(cached_year_first.pv_minutes("actual", midnight, 24 * 60).values())
    second_total = sum(cached_year_second.pv_minutes("actual", midnight, 24 * 60).values())
    if abs(first_total - second_total) > 1e-9:
        print("  ERROR: a cache hit should reproduce the same result as a fresh fetch, got {} vs {}".format(first_total, second_total))
        failed = True

    print("Test: a truncated payload is rejected and never cached")
    truncated_storage = FakeAnnualStorage()
    truncated_payload = build_hourly(start, 5, 800.0)
    truncated_payload["hourly"]["global_tilted_irradiance"] = truncated_payload["hourly"]["global_tilted_irradiance"][:-5]

    async def truncated_fetch(url):
        """Always return a payload whose irradiance list is shorter than its time list."""
        return truncated_payload

    truncated_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, storage=truncated_storage, fetch_json=truncated_fetch)
    truncated_year = asyncio.run(truncated_weather.fetch(2025))
    if truncated_year.forecast_available:
        print("  ERROR: a truncated payload must not count as an available forecast series")
        failed = True
    if truncated_year.actual_periods:
        print("  ERROR: a truncated payload must not be used for the actual series either")
        failed = True
    if truncated_storage.saved_keys:
        print("  ERROR: a truncated payload must never be cached, but {} saves were recorded".format(truncated_storage.saved_keys))
        failed = True

    print("Test: a cache entry poisoned before this guard existed is discarded and re-fetched, not trusted forever")
    poisoned_storage = FakeAnnualStorage()
    poisoned_storage.cache[("annual", "weather_actual_2025_0_51.5_-0.1")] = truncated_payload
    recovering_calls = {"n": 0}

    async def recovering_fetch(url):
        """Return good fixture payloads, counting calls to prove the poisoned cache was bypassed."""
        recovering_calls["n"] += 1
        return actual_payload if "archive-api" in url else forecast_payload

    recovering_weather = AnnualWeather(ARRAYS, latitude=51.5, longitude=-0.1, log=print, storage=poisoned_storage, fetch_json=recovering_fetch)
    recovering_year = asyncio.run(recovering_weather.fetch(2025))
    if not recovering_year.has_actual(date(2025, 1, 10)):
        print("  ERROR: a poisoned cache entry should be discarded and re-fetched rather than trusted forever")
        failed = True
    if recovering_calls["n"] == 0:
        print("  ERROR: fetch_json should have been called since the cached actual payload was poisoned")
        failed = True

    print("Test: one array failing to parse abandons the whole series rather than blending a partial result")

    async def forecast_array_fails_fetch(url):
        """Serve good actuals for both arrays but fail the second array's forecast request."""
        if "archive-api" in url:
            return actual_payload
        if "tilt=45" in url:
            return None
        return forecast_payload

    partial_forecast_weather = AnnualWeather(TWO_ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=forecast_array_fails_fetch, p10_fallback=0.7)
    partial_forecast_year = asyncio.run(partial_forecast_weather.fetch(2025))
    if partial_forecast_year.forecast_available:
        print("  ERROR: forecast_available should be False when one of several arrays fails to parse")
        failed = True
    if 1 not in partial_forecast_year.fallback_months:
        print("  ERROR: a forecast series abandoned by one failed array should still record the flat-derate fallback")
        failed = True
    if not partial_forecast_year.has_actual(date(2025, 1, 10)):
        print("  ERROR: the actual series should still be populated when only the forecast fetch failed")
        failed = True

    print("Test: the actual series is also abandoned entirely when one of several arrays fails on that side")

    async def actual_array_fails_fetch(url):
        """Fail the second array's actual request while every forecast request succeeds."""
        if "archive-api" in url:
            return None if "tilt=45" in url else actual_payload
        return forecast_payload

    partial_actual_weather = AnnualWeather(TWO_ARRAYS, latitude=51.5, longitude=-0.1, log=print, fetch_json=actual_array_fails_fetch, p10_fallback=0.7)
    partial_actual_year = asyncio.run(partial_actual_weather.fetch(2025))
    if partial_actual_year.has_actual(date(2025, 1, 10)):
        print("  ERROR: the actual series should be empty when one of several arrays fails to parse")
        failed = True

    return failed
