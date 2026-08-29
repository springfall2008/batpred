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


def build_current_pattern_rows(days=2, base_rate=9.0, peak_rate=16.0):
    """Build a synthetic bare-URL "current rates" payload with a fixed daily two-rate shape.

    Mirrors the verified real-world product OUTGOING-PRIME-FIX-12M-26-06-23: a 9.0p base
    rate with a 16.0p peak between 15:00 and 18:00 UTC, repeating daily. The anchor date is
    in July, so this UTC window is genuinely observed as 16:00-19:00 local (BST) - matching
    the real product's documented behaviour and giving the DST-boundary test something
    real to catch. Rows are returned newest first, as the real bare endpoint does.
    """
    results = []
    start = pytz.utc.localize(datetime(2026, 7, 20))
    for slot in range(days * 48):
        valid_from = start + timedelta(minutes=30 * slot)
        valid_to = valid_from + timedelta(minutes=30)
        rate = peak_rate if 15 <= valid_from.hour < 18 else base_rate
        results.append(
            {
                "value_inc_vat": rate,
                "valid_from": valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to": valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    results.reverse()
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
        self.expiries = {}

    async def load(self, namespace, key):
        """Return the previously saved value for a key, or None if nothing was saved."""
        return self.store.get((namespace, key))

    async def save(self, namespace, key, value, format="json", expiry=None):
        """Record the value under the given key as if it had been persisted to disk, capturing any expiry passed."""
        self.store[(namespace, key)] = value
        self.expiries[(namespace, key)] = expiry


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
    basic_rates_include_manual_api = []
    original_basic_rates = my_predbat.basic_rates

    def counting_basic_rates(info, rtype, prev=None, rate_replicate=None, include_manual_api=True):
        """Wrap basic_rates to count how many times it is actually invoked, then delegate to the real implementation."""
        basic_rates_calls.append(rtype)
        basic_rates_include_manual_api.append(include_manual_api)
        return original_basic_rates(info, rtype, prev=prev, rate_replicate=rate_replicate, include_manual_api=include_manual_api)

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
    if any(basic_rates_include_manual_api):
        print("  ERROR: expected annual replay to call basic_rates with include_manual_api=False, so a live manual API override never leaks into a historical replay, got {}".format(basic_rates_include_manual_api))
        failed = True

    print("Test: standing charge is carried through from config")
    if abs(tariff.standing_charge_p_per_day - 60.0) > 0.001:
        print("  ERROR: standing charge expected 60.0, got {}".format(tariff.standing_charge_p_per_day))
        failed = True

    print("Test: a month with real historical rates does not trigger the current-rates fallback")
    if tariff.fallback_months:
        print("  ERROR: a tariff with real historical data must not record a fallback, got {}".format(tariff.fallback_months))
        failed = True

    print("Test: an empty ranged download falls back to the bare-URL current-rates pattern, keyed by local time of day")
    bare_calls = {"import": 0, "export": 0}

    async def fallback_fetch(url):
        """Serve an empty ranged download (a tariff with no historical rates for this date) and a two-rate bare-URL pattern for the current rates, counting bare-URL requests per side."""
        if "period_from" in url:
            return {"results": [], "next": None}
        if "/import/" in url:
            bare_calls["import"] += 1
        elif "/export/" in url:
            bare_calls["export"] += 1
        return {"results": build_current_pattern_rows(), "next": None}

    fallback_config = {"import_octopus_url": "https://example.com/import/", "export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 0.0}
    fallback_tariff = AnnualTariff(fallback_config, log=print, predbat=my_predbat, fetch_json=fallback_fetch, timezone="Europe/London")

    if not asyncio.run(fallback_tariff.fetch_month(2025, 7)):
        print("  ERROR: fetch_month should fall back to the current pattern and report July 2025 available")
        failed = True
    if not fallback_tariff.month_available(2025, 7):
        print("  ERROR: July 2025 should be reported available via the fallback pattern")
        failed = True
    if (2025, 7, "import") not in fallback_tariff.fallback_months or (2025, 7, "export") not in fallback_tariff.fallback_months:
        print("  ERROR: expected both (2025, 7, 'import') and (2025, 7, 'export') recorded as fallback months, got {}".format(fallback_tariff.fallback_months))
        failed = True

    print("Test: the fallback pattern places the peak rate at the correct local hour either side of a DST boundary")
    # The synthetic bare pattern's peak is 15:00-18:00 UTC as genuinely observed in BST
    # (see build_current_pattern_rows), i.e. 16:00-19:00 local. Keying by UTC minute-of-day
    # instead of local would leave the peak at UTC 15:00-18:00 in both months below, so July's
    # assertion would still pass but January's would not - which is exactly what this pair of
    # assertions is designed to catch.
    july_midnight = pytz.utc.localize(datetime(2025, 7, 10))
    july_import, july_export = fallback_tariff.rates_for(july_midnight, 24 * 60)
    if abs(july_export[15 * 60] - 16.0) > 0.01:
        print("  ERROR: July (BST) peak export at UTC 15:00 (local 16:00) expected 16.0, got {}".format(july_export.get(15 * 60)))
        failed = True
    if abs(july_export[14 * 60 + 30] - 9.0) > 0.01:
        print("  ERROR: July (BST) base export at UTC 14:30 (local 15:30) expected 9.0, got {}".format(july_export.get(14 * 60 + 30)))
        failed = True
    if abs(july_import[15 * 60] - 16.0) > 0.01:
        print("  ERROR: import must fall back the same way as export; July peak import at UTC 15:00 expected 16.0, got {}".format(july_import.get(15 * 60)))
        failed = True

    if not asyncio.run(fallback_tariff.fetch_month(2025, 1)):
        print("  ERROR: fetch_month should fall back to the current pattern for January 2025 too")
        failed = True
    if not fallback_tariff.month_available(2025, 1):
        print("  ERROR: January 2025 should remain available via the fallback pattern (import falling back must not return False)")
        failed = True
    jan_midnight = pytz.utc.localize(datetime(2025, 1, 10))
    jan_import, jan_export = fallback_tariff.rates_for(jan_midnight, 24 * 60)
    if abs(jan_export[16 * 60] - 16.0) > 0.01:
        print("  ERROR: January (GMT) peak export at UTC 16:00 (local 16:00) expected 16.0, got {}".format(jan_export.get(16 * 60)))
        failed = True
    if abs(jan_export[15 * 60] - 9.0) > 0.01:
        print("  ERROR: January (GMT) base export at UTC 15:00 expected 9.0 (the peak must not have slid an hour early as it would with a UTC-keyed pattern), got {}".format(jan_export.get(15 * 60)))
        failed = True

    print("Test: the current-rates pattern is downloaded at most once per side across twelve fetch_month calls")
    count_calls = {"import": 0, "export": 0}

    async def counting_fallback_fetch(url):
        """Serve an always-empty ranged download and a bare-URL pattern, counting bare-URL requests per side."""
        if "period_from" in url:
            return {"results": [], "next": None}
        if "/import/" in url:
            count_calls["import"] += 1
        elif "/export/" in url:
            count_calls["export"] += 1
        return {"results": build_current_pattern_rows(), "next": None}

    counting_tariff = AnnualTariff(fallback_config, log=print, predbat=my_predbat, fetch_json=counting_fallback_fetch, timezone="Europe/London")
    for fallback_month in range(1, 13):
        asyncio.run(counting_tariff.fetch_month(2025, fallback_month))
    if count_calls["import"] != 1 or count_calls["export"] != 1:
        print("  ERROR: expected the bare-URL current pattern requested exactly once per side across twelve fetch_month calls, got {}".format(count_calls))
        failed = True

    print("Test: a tariff whose ranged and bare downloads are both empty is not silently priced free")
    empty_pattern_logged = []

    def capture_empty_pattern_log(message):
        """Capture log messages for assertion, while still printing them for visibility."""
        empty_pattern_logged.append(message)
        print(message)

    async def all_empty_fetch(url):
        """Simulate a tariff with no historical rates and no current rates either - every request returns nothing."""
        return {"results": [], "next": None}

    all_empty_config = {"import_octopus_url": "https://example.com/import/", "export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 0.0}
    all_empty_tariff = AnnualTariff(all_empty_config, log=capture_empty_pattern_log, predbat=my_predbat, fetch_json=all_empty_fetch, timezone="Europe/London")
    if asyncio.run(all_empty_tariff.fetch_month(2025, 8)):
        print("  ERROR: fetch_month should report unavailable when both the ranged and bare downloads are empty")
        failed = True
    if all_empty_tariff.month_available(2025, 8):
        print("  ERROR: August 2025 must not be reported available when nothing at all was returned")
        failed = True
    if all_empty_tariff.fallback_months:
        print("  ERROR: no fallback should be recorded when the bare pattern is also empty, got {}".format(all_empty_tariff.fallback_months))
        failed = True
    if not any(("export" in message) and ("unpaid" in message) for message in empty_pattern_logged):
        print("  ERROR: expected the existing 'treating export as unpaid' warning when export has no rates at all, got {}".format(empty_pattern_logged))
        failed = True
    if (2025, 8) in all_empty_tariff.unpaid_export_months:
        # Import also found nothing here, so the whole month is excluded from the
        # results entirely (fetch_month returned False above) - it contributes no row,
        # no cost, nothing. Surfacing "export was priced at zero" about a month that was
        # never billed at all would describe a month that doesn't appear anywhere in the
        # results document.
        print("  ERROR: a month already unavailable because import failed must not also be recorded in unpaid_export_months, got {}".format(all_empty_tariff.unpaid_export_months))
        failed = True

    print("Test: an open-ended (valid_to: null) row - how Octopus publishes a flat product's current rate - fills every slot of the day, not just 30 minutes")
    open_ended_calls = []

    async def open_ended_fetch(url):
        """Serve an empty ranged download and, for the bare URL, a single open-ended row exactly as OUTGOING-VAR-24-10-26 (a real flat export product) publishes its current rate."""
        if "period_from" in url:
            return {"results": [], "next": None}
        open_ended_calls.append(url)
        return {"results": [{"value_inc_vat": 12.0, "valid_from": "2026-03-01T00:00:00Z", "valid_to": None}], "next": None}

    open_ended_config = {"export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 0.0}
    open_ended_tariff = AnnualTariff(open_ended_config, log=print, predbat=my_predbat, fetch_json=open_ended_fetch, timezone="Europe/London")
    if not asyncio.run(open_ended_tariff.fetch_month(2025, 4)):
        print("  ERROR: fetch_month should fall back to the open-ended current pattern")
        failed = True
    open_ended_midnight = pytz.utc.localize(datetime(2025, 4, 10))
    _, open_ended_export = open_ended_tariff.rates_for(open_ended_midnight, 24 * 60)
    if len(open_ended_export) != 24 * 60:
        print("  ERROR: an open-ended row must rate every one of the day's 1440 minutes, got {} minutes rated".format(len(open_ended_export)))
        failed = True
    if any(abs(rate - 12.0) > 0.001 for rate in open_ended_export.values()):
        print("  ERROR: every minute should carry the open-ended rate 12.0, got distinct values {}".format(set(open_ended_export.values())))
        failed = True

    print("Test: a genuinely time-limited row still overrides an open-ended base rate for the slots it covers (most-recent-valid_from precedence)")

    # Mirrors the real OUTGOING-VAR-24-10-26 payload verified during review: a closed
    # historical rate (15.0p, in force until the rate change) plus the open-ended
    # current rate that superseded it (12.0p). The open-ended row's valid_from is more
    # recent, so it must win for every slot, not just the 30 minutes after it starts.
    async def precedence_fetch(url):
        """Serve an empty ranged download and, for the bare URL, an open-ended current rate plus the closed rate it superseded."""
        if "period_from" in url:
            return {"results": [], "next": None}
        return {
            "results": [
                {"value_inc_vat": 12.0, "valid_from": "2026-03-01T00:00:00Z", "valid_to": None},
                {"value_inc_vat": 15.0, "valid_from": "2024-10-25T23:00:00Z", "valid_to": "2026-03-01T00:00:00Z"},
            ],
            "next": None,
        }

    precedence_tariff = AnnualTariff(open_ended_config, log=print, predbat=my_predbat, fetch_json=precedence_fetch, timezone="Europe/London")
    if not asyncio.run(precedence_tariff.fetch_month(2025, 5)):
        print("  ERROR: fetch_month should fall back to the current pattern")
        failed = True
    precedence_midnight = pytz.utc.localize(datetime(2025, 5, 10))
    _, precedence_export = precedence_tariff.rates_for(precedence_midnight, 24 * 60)
    if len(precedence_export) != 24 * 60 or any(abs(rate - 12.0) > 0.001 for rate in precedence_export.values()):
        print("  ERROR: the more recent open-ended row (12.0) should win every slot over the older closed row (15.0), got values {}".format(set(precedence_export.values())))
        failed = True

    print("Test: a partial-day bare-URL payload (a legitimate partial trailing window, not an error) is refused as a fallback rather than accepted with holes")
    # Only 10 of 24 hours worth of half-hourly rows - exactly the shape a bare request
    # can legitimately be answered with depending on when it is made. Accepting this as
    # a pattern would leave 28 of the day's 48 slots unrated, which prices those hours
    # at zero - the exact failure mode the current-rates fallback exists to fix.
    partial_logged = []

    def capture_partial_log(message):
        """Capture log messages for assertion, while still printing them for visibility."""
        partial_logged.append(message)
        print(message)

    async def partial_day_fetch(url):
        """Serve an empty ranged download and, for the bare URL, only 10 hours of half-hourly rows."""
        if "period_from" in url:
            return {"results": [], "next": None}
        return {"results": build_current_pattern_rows(days=1, base_rate=9.0, peak_rate=9.0)[:20], "next": None}

    partial_config = {"import_octopus_url": "https://example.com/import/", "export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 0.0}
    partial_tariff = AnnualTariff(partial_config, log=capture_partial_log, predbat=my_predbat, fetch_json=partial_day_fetch, timezone="Europe/London")
    if asyncio.run(partial_tariff.fetch_month(2025, 6)):
        print("  ERROR: a partial-day current-rates pattern must not be accepted as a fallback; import should report the month unavailable")
        failed = True
    if partial_tariff.month_available(2025, 6):
        print("  ERROR: June 2025 must not be available from a partial-day pattern")
        failed = True
    if partial_tariff.fallback_months:
        print("  ERROR: a partial-day pattern must not be recorded as a usable fallback, got {}".format(partial_tariff.fallback_months))
        failed = True
    if (2025, 6) in partial_tariff.unpaid_export_months:
        # Import's only candidate pattern is the same partial-day payload, so import is
        # unavailable too and the whole month is excluded (fetch_month returned False
        # above) - it must not also show up as an "export priced at zero" month it was
        # never billed for.
        print("  ERROR: a month already unavailable because import failed must not also be recorded in unpaid_export_months, got {}".format(partial_tariff.unpaid_export_months))
        failed = True
    if not any("half-hour slot" in message.lower() for message in partial_logged):
        print("  ERROR: expected a warning explaining the pattern was refused for incomplete coverage, got {}".format(partial_logged))
        failed = True

    print("Test: a timezone whose UTC offset is not a whole multiple of 30 minutes (Asia/Kathmandu, +05:45) fails closed rather than producing a pattern with holes")
    # _stamped_rates_from_pattern looks up every real minute by flooring its local time
    # onto the fixed 0, 30, 60, ... grid. Kathmandu's +05:45 offset is 345 minutes, and
    # 345 % 30 == 15, so every half-hourly UTC row converts to a local time exactly 15
    # minutes off that grid - :15 and :45 past the hour, never :00 or :30. No amount of
    # half-hourly bare data can ever produce a pattern that lands on the standard grid
    # under this offset, so this must fail closed by design (like the partial-day case
    # above), not be quietly accepted with every lookup silently missing.
    kathmandu_tariff = AnnualTariff(fallback_config, log=print, predbat=my_predbat, fetch_json=fallback_fetch, timezone="Asia/Kathmandu")
    if asyncio.run(kathmandu_tariff.fetch_month(2025, 6)):
        print("  ERROR: a +05:45 offset timezone cannot produce a half-hour-grid-aligned pattern and must fail closed, got the month reported available")
        failed = True
    if kathmandu_tariff.month_available(2025, 6):
        print("  ERROR: a +05:45 offset timezone must not report the month available via a misaligned pattern")
        failed = True
    if kathmandu_tariff.fallback_months:
        print("  ERROR: a +05:45 offset timezone must not record a fallback it cannot actually deliver, got {}".format(kathmandu_tariff.fallback_months))
        failed = True

    print("Test: export priced at zero IS surfaced as unpaid when the month is otherwise available (import succeeds, export finds nothing at all)")
    # The mirror image of the two tests just above: there, suppressing unpaid_export_months
    # was correct because the whole month was excluded anyway. Here import succeeds, so the
    # month is billed and included in the results - which is exactly when a reader needs to
    # be told export contributed nothing to it.
    mixed_config = {"import_octopus_url": "https://example.com/import/", "export_octopus_url": "https://example.com/export/", "standing_charge_p_per_day": 0.0}

    async def import_ok_export_empty_fetch(url):
        """Serve real, paginated import data on every request but answer every export request (ranged and bare alike) with nothing at all."""
        if "/import/" in url:
            return {"results": build_agile_results(date(2025, 9, 1), 32, 20.0), "next": None}
        return {"results": [], "next": None}

    mixed_tariff = AnnualTariff(mixed_config, log=print, predbat=my_predbat, fetch_json=import_ok_export_empty_fetch, timezone="Europe/London")
    if not asyncio.run(mixed_tariff.fetch_month(2025, 9)):
        print("  ERROR: the month should be available - import succeeded even though export found nothing at all")
        failed = True
    if (2025, 9) not in mixed_tariff.unpaid_export_months:
        print("  ERROR: export-priced-at-zero should be surfaced when the month IS otherwise available (import succeeded), got {}".format(mixed_tariff.unpaid_export_months))
        failed = True

    print("Test: a genuine download failure (not a genuinely empty result) does not trigger the current-rates fallback")
    failure_bare_calls = []

    async def failure_then_bare_fetch(url):
        """Simulate the ranged download failing outright (not a clean zero-row response) while the bare URL would happily serve a pattern if it were ever called."""
        if "period_from" in url:
            return None
        failure_bare_calls.append(url)
        return {"results": build_current_pattern_rows(), "next": None}

    failure_config = {"import_octopus_url": "https://example.com/import/", "standing_charge_p_per_day": 0.0}
    failure_tariff = AnnualTariff(failure_config, log=print, predbat=my_predbat, fetch_json=failure_then_bare_fetch, timezone="Europe/London")
    if asyncio.run(failure_tariff.fetch_month(2025, 9)):
        print("  ERROR: a genuine download failure must not fall back and must report the month unavailable, exactly as before this feature existed")
        failed = True
    if failure_tariff.month_available(2025, 9):
        print("  ERROR: September 2025 must not be reported available after a genuine download failure")
        failed = True
    if failure_tariff.fallback_months:
        print("  ERROR: a genuine download failure must not be recorded as a fallback, got {}".format(failure_tariff.fallback_months))
        failed = True
    if failure_bare_calls:
        print("  ERROR: the bare-URL current pattern must never be requested after a genuine download failure (only after a confirmed, genuinely empty result), got {} calls".format(len(failure_bare_calls)))
        failed = True

    print("Test: the page-cap safety limit is also treated as a failure, not a genuinely empty result")
    cap_bare_calls = []

    async def cap_then_bare_fetch(url):
        """Simulate the ranged download hitting the page-cap (always reporting another page) while the bare URL would happily serve a pattern if it were ever called."""
        if "period_from" in url:
            return {"results": build_agile_results(date(2025, 10, 1), 1, 20.0), "next": url + "&more"}
        cap_bare_calls.append(url)
        return {"results": build_current_pattern_rows(), "next": None}

    cap_config = {"import_octopus_url": "https://example.com/import/", "standing_charge_p_per_day": 0.0}
    cap_fallback_tariff = AnnualTariff(cap_config, log=print, predbat=my_predbat, fetch_json=cap_then_bare_fetch, timezone="Europe/London")
    if asyncio.run(cap_fallback_tariff.fetch_month(2025, 10)):
        print("  ERROR: a page-cap truncation must not fall back and must report the month unavailable")
        failed = True
    if cap_bare_calls:
        print("  ERROR: the bare-URL current pattern must never be requested after a page-cap truncation, got {} calls".format(len(cap_bare_calls)))
        failed = True

    print("Test: the current-rates snapshot is cached with an expiry, not forever")
    expiry_storage = FakeAnnualStorage()
    expiry_tariff = AnnualTariff(fallback_config, log=print, predbat=my_predbat, storage=expiry_storage, fetch_json=fallback_fetch, timezone="Europe/London")
    asyncio.run(expiry_tariff.fetch_month(2025, 11))
    export_pattern_key = ("annual", "current_pattern_export_{}".format(AnnualTariff._url_cache_id(expiry_tariff.export_url)))
    export_pattern_expiry = expiry_storage.expiries.get(export_pattern_key)
    if export_pattern_expiry is None:
        print("  ERROR: expected the current-rates pattern cache entry to carry a non-None expiry (a 'now' snapshot must not be cached forever), got {}".format(expiry_storage.expiries))
        failed = True

    # The per-month ranged download, by contrast, covers a fixed historical date that can
    # never change, so it must still be cached with no expiry - proved here against a real,
    # non-empty ranged download (the expiry_tariff case above never actually cached a ranged
    # entry at all, since its ranged downloads were empty, so it couldn't tell them apart).
    history_storage = FakeAnnualStorage()
    history_tariff = AnnualTariff(config, log=print, predbat=my_predbat, storage=history_storage, fetch_json=fake_fetch, timezone="Europe/London")
    asyncio.run(history_tariff.fetch_month(2025, 3))
    history_export_key = ("annual", "rates_export_{}_2025_03".format(AnnualTariff._url_cache_id(history_tariff.export_url)))
    if history_export_key not in history_storage.expiries:
        print("  ERROR: expected the real historical download to have been cached at all, got {}".format(history_storage.store.keys()))
        failed = True
    if history_storage.expiries.get(history_export_key) is not None:
        print("  ERROR: a per-month historical download must still be cached with no expiry, got {}".format(history_storage.expiries.get(history_export_key)))
        failed = True

    print("Test: two tariffs with different URLs sharing one storage do not see each other's cached rates (the ranged, per-month path)")
    # Reproduces the reviewer's scenario: the web UI runs every job against one fixed
    # work dir, so its on-disk cache is shared across runs and tariffs, not scoped to
    # either. Before the URL was mixed into the cache key, tariff B here would have been
    # silently served tariff A's cached rows under the same bare "rates_import_2025_10"
    # key - reported status "ok", no rates_synthesised marker, no caveat, wrong price.
    shared_storage = FakeAnnualStorage()

    async def tariff_a_fetch(url):
        """Serve tariff A's own distinct rate shape (base rate 5.0p)."""
        return {"results": build_agile_results(date(2025, 10, 1), 32, 5.0), "next": None}

    async def tariff_b_fetch(url):
        """Serve tariff B's own distinct rate shape (base rate 30.0p) - must never be shadowed by A's cache entry."""
        return {"results": build_agile_results(date(2025, 10, 1), 32, 30.0), "next": None}

    tariff_a_config = {"import_octopus_url": "https://example.com/tariff-a/import/", "standing_charge_p_per_day": 0.0}
    tariff_b_config = {"import_octopus_url": "https://example.com/tariff-b/import/", "standing_charge_p_per_day": 0.0}

    tariff_a = AnnualTariff(tariff_a_config, log=print, predbat=my_predbat, storage=shared_storage, fetch_json=tariff_a_fetch, timezone="Europe/London")
    if not asyncio.run(tariff_a.fetch_month(2025, 10)):
        print("  ERROR: tariff A should fetch successfully")
        failed = True
    tariff_b = AnnualTariff(tariff_b_config, log=print, predbat=my_predbat, storage=shared_storage, fetch_json=tariff_b_fetch, timezone="Europe/London")
    if not asyncio.run(tariff_b.fetch_month(2025, 10)):
        print("  ERROR: tariff B should fetch successfully")
        failed = True

    cache_midnight = pytz.utc.localize(datetime(2025, 10, 10))
    a_import, _ = tariff_a.rates_for(cache_midnight, 60)
    b_import, _ = tariff_b.rates_for(cache_midnight, 60)
    # Minute 0 (00:00) is in the cheap overnight band: base_rate * 0.3
    if abs(a_import[0] - 1.5) > 0.01:
        print("  ERROR: tariff A's own rate expected 1.5 (5.0 * 0.3), got {}".format(a_import.get(0)))
        failed = True
    if abs(b_import[0] - 9.0) > 0.01:
        print("  ERROR: tariff B's own rate expected 9.0 (30.0 * 0.3), got {} - if this is 1.5 instead, tariff B was served tariff A's cached rows".format(b_import.get(0)))
        failed = True

    print("Test: the same tariff URL across two instances still hits its own cache (this is what makes re-runs fast)")

    async def must_not_be_called(url):
        """Fail the test outright if called - a second instance of the same URL must be served entirely from cache."""
        raise AssertionError("fetch_json must not be called; a second AnnualTariff for the same URL should be served from its own cached rows")

    tariff_a_again = AnnualTariff(tariff_a_config, log=print, predbat=my_predbat, storage=shared_storage, fetch_json=must_not_be_called, timezone="Europe/London")
    if not asyncio.run(tariff_a_again.fetch_month(2025, 10)):
        print("  ERROR: a second instance of the same tariff URL should be served from cache and still succeed")
        failed = True
    a_again_import, _ = tariff_a_again.rates_for(cache_midnight, 60)
    if abs(a_again_import[0] - 1.5) > 0.01:
        print("  ERROR: the cached rate should still be tariff A's own 1.5, got {}".format(a_again_import.get(0)))
        failed = True

    print("Test: two tariffs with different URLs sharing one storage do not see each other's cached current-rates pattern (the bare-URL fallback path)")
    pattern_storage = FakeAnnualStorage()

    async def tariff_c_bare_fetch(url):
        """Serve an empty ranged download and, for the bare URL, tariff C's own flat rate (5.0p)."""
        if "period_from" in url:
            return {"results": [], "next": None}
        return {"results": [{"value_inc_vat": 5.0, "valid_from": "2026-03-01T00:00:00Z", "valid_to": None}], "next": None}

    async def tariff_d_bare_fetch(url):
        """Serve an empty ranged download and, for the bare URL, tariff D's own flat rate (30.0p) - must never be shadowed by C's cache entry."""
        if "period_from" in url:
            return {"results": [], "next": None}
        return {"results": [{"value_inc_vat": 30.0, "valid_from": "2026-03-01T00:00:00Z", "valid_to": None}], "next": None}

    tariff_c_config = {"export_octopus_url": "https://example.com/tariff-c/export/", "standing_charge_p_per_day": 0.0}
    tariff_d_config = {"export_octopus_url": "https://example.com/tariff-d/export/", "standing_charge_p_per_day": 0.0}

    tariff_c = AnnualTariff(tariff_c_config, log=print, predbat=my_predbat, storage=pattern_storage, fetch_json=tariff_c_bare_fetch, timezone="Europe/London")
    asyncio.run(tariff_c.fetch_month(2025, 11))
    tariff_d = AnnualTariff(tariff_d_config, log=print, predbat=my_predbat, storage=pattern_storage, fetch_json=tariff_d_bare_fetch, timezone="Europe/London")
    asyncio.run(tariff_d.fetch_month(2025, 11))

    cache_midnight_2 = pytz.utc.localize(datetime(2025, 11, 10))
    _, c_export = tariff_c.rates_for(cache_midnight_2, 60)
    _, d_export = tariff_d.rates_for(cache_midnight_2, 60)
    if abs(c_export.get(0, -1) - 5.0) > 0.01:
        print("  ERROR: tariff C's own export rate expected 5.0, got {}".format(c_export.get(0)))
        failed = True
    if abs(d_export.get(0, -1) - 30.0) > 0.01:
        print("  ERROR: tariff D's own export rate expected 30.0, got {} - if this is 5.0 instead, tariff D was served tariff C's cached current-rates pattern".format(d_export.get(0)))
        failed = True

    print("Test: a mixed tariff prices each side from its own source")

    # Import and export are selected independently in the web UI, so "basic-rate import
    # with a downloaded export tariff" (price cap import + Octopus Outgoing Prime, say)
    # is an ordinary choice. rates_for used to gate BOTH sides on one
    # `self.import_url or self.export_url` test: this config took the URL branch, found
    # no import rows to stamp, and priced the whole year's import at zero - a plausible
    # looking run that was silently free. Asserting on the VALUES, not just presence,
    # is what makes that failure visible.
    async def mixed_export_fetch(url):
        """Serve a flat 12p export payload for the export URL; no import URL is ever requested."""
        return {"results": [{"value_inc_vat": 12.0, "valid_from": "2025-03-01T00:00:00Z", "valid_to": None}], "next": None}

    mixed_config = {"rates_import": [{"rate": 26.11}], "export_octopus_url": "https://example.com/mixed/export/", "standing_charge_p_per_day": 57.19}
    mixed = AnnualTariff(mixed_config, log=print, predbat=my_predbat, fetch_json=mixed_export_fetch, timezone="Europe/London")
    if not asyncio.run(mixed.fetch_month(2025, 3)):
        print("  ERROR: a basic-rate import with a downloaded export should be a usable month")
        failed = True
    mixed_import, mixed_export = mixed.rates_for(pytz.utc.localize(datetime(2025, 3, 10)), 24 * 60)
    if len(mixed_import) < 24 * 60:
        print("  ERROR: expected a full day of import rates from the basic pattern, got {} minutes".format(len(mixed_import)))
        failed = True
    if abs(mixed_import.get(0, -1) - 26.11) > 0.01:
        print("  ERROR: import should come from rates_import (26.11p), got {} - zero or missing means the basic import was ignored because export had a URL".format(mixed_import.get(0)))
        failed = True
    if abs(mixed_export.get(0, -1) - 12.0) > 0.01:
        print("  ERROR: export should come from the downloaded URL (12.0p), got {}".format(mixed_export.get(0)))
        failed = True

    print("Test: the mirror image - a downloaded import with a basic-rate export")

    async def mixed_import_fetch(url):
        """Serve a flat 30p import payload for the import URL."""
        return {"results": [{"value_inc_vat": 30.0, "valid_from": "2025-03-01T00:00:00Z", "valid_to": None}], "next": None}

    mirror_config = {"import_octopus_url": "https://example.com/mirror/import/", "rates_export": [{"rate": 4.1}], "standing_charge_p_per_day": 0.0}
    mirror = AnnualTariff(mirror_config, log=print, predbat=my_predbat, fetch_json=mixed_import_fetch, timezone="Europe/London")
    if not asyncio.run(mirror.fetch_month(2025, 3)):
        print("  ERROR: a downloaded import with a basic-rate export should be a usable month")
        failed = True
    mirror_import, mirror_export = mirror.rates_for(pytz.utc.localize(datetime(2025, 3, 10)), 24 * 60)
    if abs(mirror_import.get(0, -1) - 30.0) > 0.01:
        print("  ERROR: import should come from the downloaded URL (30.0p), got {}".format(mirror_import.get(0)))
        failed = True
    if abs(mirror_export.get(0, -1) - 4.1) > 0.01:
        print("  ERROR: export should come from rates_export (4.1p), got {} - missing means the basic export was ignored because import had a URL".format(mirror_export.get(0)))
        failed = True

    print("Test: a deliberate zero export rate is priced, not left unrated")
    # "No export payment" is a flat 0p rate. Every minute must be stamped 0, not left
    # absent: an absent rate reads as "unknown" to the caller rather than "unpaid", and
    # it must not raise the unpaid-export caveat, which is for a download that failed.
    zero_config = {"rates_import": [{"rate": 26.11}], "rates_export": [{"rate": 0.0}], "standing_charge_p_per_day": 0.0}
    zero = AnnualTariff(zero_config, log=print, predbat=my_predbat, timezone="Europe/London")
    if not asyncio.run(zero.fetch_month(2025, 3)):
        print("  ERROR: a zero export rate should still be a usable month")
        failed = True
    zero_import, zero_export = zero.rates_for(pytz.utc.localize(datetime(2025, 3, 10)), 24 * 60)
    if len(zero_export) < 24 * 60:
        print("  ERROR: expected a full day of export rates stamped at zero, got {} minutes".format(len(zero_export)))
        failed = True
    if set(zero_export.values()) != {0.0}:
        print("  ERROR: every export minute should be 0p, got values {}".format(sorted(set(zero_export.values()))[:5]))
        failed = True
    if abs(zero_import.get(0, -1) - 26.11) > 0.01:
        print("  ERROR: import should be unaffected by a zero export, got {}".format(zero_import.get(0)))
        failed = True
    if zero.unpaid_export_months:
        print("  ERROR: a deliberate zero export must not raise the unpaid-export caveat, got {}".format(zero.unpaid_export_months))
        failed = True

    return failed
