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
from datetime import date, datetime, timedelta

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


def build_month_aware_fetch(rate_by_month):
    """Build a fetch stub whose synthesised page's base rate depends on the requested month.

    Parses ``period_from``/``period_to`` out of the requested URL and serves a single,
    non-paginated page covering exactly that range. Because the base rate depends on the
    requested start month, a test can tell which month's download actually produced a
    given stamped rate - this is what makes it possible to prove ``rates_for``'s
    month-boundary merge picks up the *next* month's own download for dates that spill
    into it, rather than silently reusing the requesting month's download for those dates.
    """

    async def fetch(url):
        """Serve a single page of synthetic rates sized to the requested period_from/period_to."""
        start_str = url.split("period_from=")[1][:10]
        end_str = url.split("period_to=")[1][:10]
        start_day = date(*(int(part) for part in start_str.split("-")))
        end_day = date(*(int(part) for part in end_str.split("-")))
        days = (end_day - start_day).days
        base_rate = rate_by_month.get(start_day.month, 20.0)
        return {"results": build_agile_results(start_day, days, base_rate), "next": None}

    return fetch


class FakeAnnualStorage:
    """Minimal in-memory storage stub with async save/load, used to prove a truncated download is never cached."""

    def __init__(self):
        """Start with an empty in-memory store."""
        self.store = {}

    async def load(self, namespace, key):
        """Return the previously saved value for a key, or None if nothing was saved."""
        return self.store.get((namespace, key))

    async def save(self, namespace, key, value, format="json"):
        """Record the value under the given key as if it had been persisted to disk."""
        self.store[(namespace, key)] = value


def test_annual_tariff(my_predbat):
    """Verify Octopus date-ranged rate fetching, pagination, slicing, caching guards and basic rates."""
    failed = False
    print("**** Testing annual_tariff ****")

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
    if existing.count("page_size=100") != 1 or "page_size=1500" in existing:
        print("  ERROR: an explicit page_size must be preserved as-is, not duplicated or overridden, got {}".format(existing))
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

    print("Test: rates_for returns a 48 hour window keyed by absolute minute, for both import and export")
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
    # The export URL is fed the same synthetic payload shape as import in this fixture,
    # so its overnight/peak values must match the same 20.0 base rate formula. Asserting
    # on actual values (not just presence) proves the export branch is truly exercised -
    # deleting it outright would previously still pass this test.
    if len(rate_export) < 48 * 60:
        print("  ERROR: expected at least {} export rate minutes, got {}".format(48 * 60, len(rate_export)))
        failed = True
    if abs(rate_export[120] - 6.0) > 0.01:
        print("  ERROR: export overnight rate expected 6.0, got {}".format(rate_export[120]))
        failed = True
    if abs(rate_export[17 * 60] - 40.0) > 0.01:
        print("  ERROR: export peak rate expected 40.0, got {}".format(rate_export[17 * 60]))
        failed = True

    print("Test: the second day of the window carries the following day's rates")
    if rate_import.get(24 * 60 + 120) is None:
        print("  ERROR: the second day of the window must be populated")
        failed = True

    print("Test: an export-only tariff (no import URL) is reported available when its export download succeeds")
    export_only_config = {"export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 0.0}
    export_only_tariff = AnnualTariff(export_only_config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
    if not asyncio.run(export_only_tariff.fetch_month(2025, 3)):
        print("  ERROR: an export-only tariff should be available once its export download succeeds")
        failed = True
    if not export_only_tariff.month_available(2025, 3):
        print("  ERROR: March 2025 should be reported as available for an export-only tariff")
        failed = True

    print("Test: a 48 hour window starting on the last day of a month carries the following month's own download, across a December to January year boundary")
    dec_jan_config = {"import_octopus_url": "https://example.com/import/", "standing_charge_p_per_day": 0.0}
    dec_jan_tariff = AnnualTariff(dec_jan_config, log=print, predbat=my_predbat, fetch_json=build_month_aware_fetch({12: 50.0, 1: 90.0}))
    if not asyncio.run(dec_jan_tariff.fetch_month(2025, 12)):
        print("  ERROR: December 2025 should fetch successfully")
        failed = True
    if not asyncio.run(dec_jan_tariff.fetch_month(2026, 1)):
        print("  ERROR: January 2026 should fetch successfully")
        failed = True
    boundary_midnight = pytz.utc.localize(datetime(2025, 12, 31))
    boundary_import, _ = dec_jan_tariff.rates_for(boundary_midnight, 48 * 60)
    # Day one (31 Dec) overnight reflects December's own download: 50.0 * 0.3 = 15.0
    if abs(boundary_import[120] - 15.0) > 0.01:
        print("  ERROR: December overnight rate expected 15.0, got {}".format(boundary_import[120]))
        failed = True
    # Day two (1 Jan) overnight must reflect January's own download (90.0 * 0.3 = 27.0),
    # not December's download merely extending into the buffer day at the old rate (15.0) -
    # dropping the next-month merge entirely would leave this at 15.0 and still pass every
    # other assertion above, since December's own buffer days also contain stamps for 1 Jan.
    if abs(boundary_import[24 * 60 + 120] - 27.0) > 0.01:
        print("  ERROR: January overnight rate expected 27.0 (carried from the following month's own fetch), got {}".format(boundary_import[24 * 60 + 120]))
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

    print("Test: a failed page and a page-cap hit both refuse to cache a partial download")

    async def failing_page_fetch(url):
        """Simulate the very first page request failing outright."""
        return None

    fail_storage = FakeAnnualStorage()
    fail_tariff = AnnualTariff({"import_octopus_url": "https://example.com/import/", "standing_charge_p_per_day": 0.0}, log=print, predbat=my_predbat, storage=fail_storage, fetch_json=failing_page_fetch)
    if asyncio.run(fail_tariff.fetch_month(2025, 5)):
        print("  ERROR: fetch_month should fail when the first page request fails")
        failed = True
    if fail_storage.store:
        print("  ERROR: a failed page must not be cached, got {}".format(fail_storage.store))
        failed = True

    async def never_ending_fetch(url):
        """Always report another page is available, to exercise the pagination safety cap."""
        return {"results": build_agile_results(date(2025, 6, 1), 1, 20.0), "next": url + "&more"}

    cap_storage = FakeAnnualStorage()
    cap_tariff = AnnualTariff({"import_octopus_url": "https://example.com/import/", "standing_charge_p_per_day": 0.0}, log=print, predbat=my_predbat, storage=cap_storage, fetch_json=never_ending_fetch)
    if asyncio.run(cap_tariff.fetch_month(2025, 6)):
        print("  ERROR: fetch_month should fail when the page cap is hit with pages still remaining")
        failed = True
    if cap_storage.store:
        print("  ERROR: a page-cap truncation must not be cached, got {}".format(cap_storage.store))
        failed = True

    print("Test: {dno_region} in a tariff URL is substituted from config without mutating predbat.args")
    region_calls = []

    async def region_fetch(url):
        """Record the URL requested and serve a minimal single page of results."""
        region_calls.append(url)
        return {"results": build_agile_results(date(2025, 7, 1), 1, 20.0), "next": None}

    dno_region_before = my_predbat.args.get("dno_region")
    region_config = {"import_octopus_url": "https://example.com/rates-{dno_region}/", "dno_region": "A", "standing_charge_p_per_day": 0.0}
    region_tariff = AnnualTariff(region_config, log=print, predbat=my_predbat, fetch_json=region_fetch)
    if region_tariff.import_url != "https://example.com/rates-A/":
        print("  ERROR: expected the {{dno_region}} template resolved to A, got {}".format(region_tariff.import_url))
        failed = True
    asyncio.run(region_tariff.fetch_month(2025, 7))
    if not region_calls or "rates-A/" not in region_calls[0]:
        print("  ERROR: expected the requested URL to contain the resolved region, got {}".format(region_calls))
        failed = True
    if my_predbat.args.get("dno_region") != dno_region_before:
        print("  ERROR: dno_region must not be written into predbat.args (would clobber a live Octopus component's own region)")
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

    print("Test: an unconfigured tariff (no URLs and no rates_import) is reported unavailable rather than pricing at an implicit zero")
    empty_config = {"standing_charge_p_per_day": 0.0}
    empty_tariff = AnnualTariff(empty_config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
    if asyncio.run(empty_tariff.fetch_month(2025, 3)):
        print("  ERROR: an unconfigured tariff (no import URL and no rates_import) must not be reported as available")
        failed = True
    if empty_tariff.month_available(2025, 3):
        print("  ERROR: an unconfigured tariff must not be reported as available")
        failed = True

    print("Test: day_of_week/date basic rate entries are ignored (not applied using today's real weekday) and a warning is logged")
    logged = []

    def capturing_log(message):
        """Capture log messages for assertion, while still printing them for visibility."""
        logged.append(message)
        print(message)

    weekday_config = {
        "rates_import": [{"start": "00:00:00", "end": "00:00:00", "rate": 12.0}, {"start": "00:00:00", "end": "00:00:00", "rate": 99.0, "day_of_week": "1,2,3,4,5"}],
        "rates_export": [{"rate": 5.0}],
        "standing_charge_p_per_day": 0.0,
    }
    weekday_tariff = AnnualTariff(weekday_config, log=capturing_log, predbat=my_predbat, fetch_json=fake_fetch)
    if not asyncio.run(weekday_tariff.fetch_month(2025, 3)):
        print("  ERROR: basic rates should always be available")
        failed = True
    weekday_import, _ = weekday_tariff.rates_for(midnight, 24 * 60)
    if abs(weekday_import[0] - 12.0) > 0.001:
        print("  ERROR: the day_of_week entry must be ignored during a historical replay, expected the base rate 12.0, got {}".format(weekday_import[0]))
        failed = True
    if not any(("day_of_week" in message) and ("ignoring" in message.lower()) for message in logged):
        print("  ERROR: expected a warning that day_of_week/date entries cannot be honoured and are being ignored, got {}".format(logged))
        failed = True

    print("Test: basic rates are computed once and cached, not recomputed on every rates_for call")
    basic_rates_calls = []
    original_basic_rates = my_predbat.basic_rates

    def counting_basic_rates(info, rtype, prev=None, rate_replicate=None):
        """Wrap basic_rates to count how many times it is actually invoked, then delegate to the real implementation."""
        basic_rates_calls.append(rtype)
        return original_basic_rates(info, rtype, prev=prev, rate_replicate=rate_replicate)

    my_predbat.basic_rates = counting_basic_rates
    try:
        caching_tariff = AnnualTariff(basic_config, log=print, predbat=my_predbat, fetch_json=fake_fetch)
        asyncio.run(caching_tariff.fetch_month(2025, 3))
        caching_tariff.rates_for(midnight, 48 * 60)
        caching_tariff.rates_for(midnight, 48 * 60)
        caching_tariff.rates_for(pytz.utc.localize(datetime(2025, 3, 11)), 48 * 60)
    finally:
        my_predbat.basic_rates = original_basic_rates
    if basic_rates_calls.count("rates_import") != 1 or basic_rates_calls.count("rates_export") != 1:
        print("  ERROR: expected basic_rates called exactly once per rate type across three rates_for calls, got {}".format(basic_rates_calls))
        failed = True

    print("Test: standing charge is carried through from config")
    if abs(tariff.standing_charge_p_per_day - 60.0) > 0.001:
        print("  ERROR: standing charge expected 60.0, got {}".format(tariff.standing_charge_p_per_day))
        failed = True

    return failed
