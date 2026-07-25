# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction tariff module."""

import asyncio
from datetime import datetime, timedelta

import pytz

from annual_tariff import AnnualTariff, build_period_url


def build_agile_results(start_day, days, base_rate):
    """Build a synthetic Octopus half-hourly rate payload with a repeating daily shape."""
    results = []
    stamp = pytz.utc.localize(datetime(start_day.year, start_day.month, start_day.day))
    for slot in range(days * 48):
        valid_from = stamp + timedelta(minutes=30 * slot)
        valid_to = valid_from + timedelta(minutes=30)
        # Cheap overnight, expensive in the evening peak
        hour = valid_from.hour
        rate = base_rate * (0.3 if hour < 5 else (2.0 if 16 <= hour < 19 else 1.0))
        results.append(
            {
                "value_inc_vat": round(rate, 4),
                "valid_from": valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to": valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return results


def test_annual_tariff(my_predbat):
    """Verify Octopus date-ranged rate fetching, pagination, slicing and basic rates."""
    failed = False
    print("**** Testing annual_tariff ****")
    from datetime import date

    print("Test: build_period_url appends the date range without losing existing query parameters")
    start = pytz.utc.localize(datetime(2025, 3, 1))
    end = pytz.utc.localize(datetime(2025, 4, 1))
    plain = build_period_url("https://example.com/rates/", start, end)
    if "?period_from=2025-03-01T00:00Z" not in plain or "period_to=2025-04-01T00:00Z" not in plain:
        print("  ERROR: expected a period range in {}".format(plain))
        failed = True
    existing = build_period_url("https://example.com/rates/?page_size=100", start, end)
    if "&period_from=" not in existing or "page_size=100" not in existing:
        print("  ERROR: existing query parameters must be preserved, got {}".format(existing))
        failed = True

    print("Test: an Octopus URL tariff resolves rates for a specific date, following pagination")
    page_two = {"results": build_agile_results(date(2025, 3, 16), 16, 20.0), "next": None}
    page_one = {"results": build_agile_results(date(2025, 3, 1), 15, 20.0), "next": "https://example.com/page2"}

    calls = []

    async def fake_fetch(url):
        """Serve two pages of rate data and record the URLs requested."""
        calls.append(url)
        return page_two if "page2" in url else page_one

    config = {"import_octopus_url": "https://example.com/import/", "export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 60.0}
    tariff = AnnualTariff(config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
    ok = asyncio.run(tariff.fetch_month(2025, 3))
    if not ok:
        print("  ERROR: fetch_month should succeed with valid payloads")
        failed = True
    if not tariff.month_available(2025, 3):
        print("  ERROR: March 2025 should be reported as available")
        failed = True
    if len(calls) != 4:
        print("  ERROR: expected 4 requests (2 pages x import and export), got {}".format(len(calls)))
        failed = True

    print("Test: rates_for returns a 48 hour window keyed by absolute minute")
    midnight = pytz.utc.localize(datetime(2025, 3, 10))
    rate_import, rate_export = tariff.rates_for(midnight, 48 * 60)
    if len(rate_import) < 48 * 60:
        print("  ERROR: expected at least {} import rate minutes, got {}".format(48 * 60, len(rate_import)))
        failed = True
    if rate_import.get(0) is None:
        print("  ERROR: minute 0 must have an import rate")
        failed = True
    # 02:00 is in the cheap overnight band, 17:00 is in the peak band
    if not rate_import[120] < rate_import[17 * 60]:
        print("  ERROR: overnight rate {} should be below peak rate {}".format(rate_import[120], rate_import[17 * 60]))
        failed = True
    if abs(rate_import[120] - 6.0) > 0.01:
        print("  ERROR: overnight rate expected 6.0, got {}".format(rate_import[120]))
        failed = True

    print("Test: the second day of the window carries the following day's rates")
    if rate_import.get(24 * 60 + 120) is None:
        print("  ERROR: the second day of the window must be populated")
        failed = True

    print("Test: a failed download reports the month as unavailable rather than returning zeros")

    async def failing_fetch(url):
        """Simulate a download failure."""
        return None

    broken = AnnualTariff(config, log=print, predbat=my_predbat, fetch_json=failing_fetch)
    if asyncio.run(broken.fetch_month(2025, 4)):
        print("  ERROR: fetch_month should report failure when the download fails")
        failed = True
    if broken.month_available(2025, 4):
        print("  ERROR: a failed month must not be reported as available")
        failed = True

    print("Test: basic rates repeat a fixed daily pattern across the window")
    basic_config = {
        "rates_import": [{"start": "00:00:00", "end": "05:00:00", "rate": 7.0}, {"start": "05:00:00", "end": "00:00:00", "rate": 30.0}],
        "rates_export": [{"rate": 15.0}],
        "standing_charge_p_per_day": 45.0,
    }
    basic = AnnualTariff(basic_config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
    if not asyncio.run(basic.fetch_month(2025, 3)):
        print("  ERROR: basic rates should always be available")
        failed = True
    basic_import, basic_export = basic.rates_for(midnight, 48 * 60)
    if abs(basic_import[120] - 7.0) > 0.001:
        print("  ERROR: basic overnight import expected 7.0, got {}".format(basic_import[120]))
        failed = True
    if abs(basic_import[10 * 60] - 30.0) > 0.001:
        print("  ERROR: basic daytime import expected 30.0, got {}".format(basic_import[10 * 60]))
        failed = True
    if abs(basic_import[24 * 60 + 120] - 7.0) > 0.001:
        print("  ERROR: basic rates must repeat on day two, got {}".format(basic_import[24 * 60 + 120]))
        failed = True
    if abs(basic_export[10 * 60] - 15.0) > 0.001:
        print("  ERROR: basic export expected 15.0, got {}".format(basic_export[10 * 60]))
        failed = True

    print("Test: standing charge is carried through from config")
    if abs(tariff.standing_charge_p_per_day - 60.0) > 0.001:
        print("  ERROR: standing charge expected 60.0, got {}".format(tariff.standing_charge_p_per_day))
        failed = True

    return failed
