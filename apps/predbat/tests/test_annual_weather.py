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
    # The shared solar model applies a GTI-dependent cell-temperature derate (Task 1), so the
    # measured ratio sits a little above the raw 800/1000 GTI ratio; 0.02 comfortably covers that.
    if abs(ratio - 0.8) > 0.02:
        print("  ERROR: expected a P10 ratio near 0.8, got {}".format(ratio))
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
    if max(two_day.keys()) >= 48 * 60:
        print("  ERROR: pv_minutes must not emit minutes at or beyond the window length")
        failed = True
    if abs(sum(two_day.values()) - (actual_total + year.daily_actual_kwh(date(2025, 1, 11)))) > 0.01:
        print("  ERROR: the 48 hour window should equal two days of actual energy")
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

    return failed
