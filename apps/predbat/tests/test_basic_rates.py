# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime


def assert_rates(rates, start_minute, end_minute, expect_rate):
    """
    Assert rates
    """
    end_minute = min(end_minute, len(rates))
    for minute in range(start_minute, end_minute):
        if rates[minute] != expect_rate:
            print("ERROR: Rate at minute {} should be {} got {}".format(minute, expect_rate, rates[minute]))
            results_short = {}
            for i in range(0, 48 * 60, 30):
                results_short[i] = rates[i]
            print("Rates: {}".format(results_short))
            return 1
    return 0


def test_basic_rates(my_predbat):
    """
    Test for basic rates function

    rates = basic_rates(self, info, rtype, prev=None, rate_replicate={}):
    """
    failed = 0

    old_midnight = my_predbat.midnight
    my_predbat.midnight = datetime.strptime("2025-07-05T00:00:00", "%Y-%m-%dT%H:%M:%S")

    print("*** Running test: Simple rate1")
    simple_rate = [
        {"rate": 5},
        {
            "rate": 10,
            "start": "17:00:00",
            "end": "19:00:00",
        },
    ]
    results = my_predbat.basic_rates(simple_rate, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)

    failed |= assert_rates(results, 0, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 10)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    print("*** Running test: Simple rate2")
    simple_rate2 = [{"rate": 5}, {"rate": 10, "start": "17:00:00", "end": "19:00:00", "day_of_week": 7}, {"rate": 9, "start": "17:00:00", "end": "19:00:00", "day_of_week": "5,6"}]
    results = my_predbat.basic_rates(simple_rate2, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)

    failed |= assert_rates(results, 0, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 9)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    print("*** Running test: Simple rate3")
    simple_rate3 = [
        {"rate": 10, "start": "01:00:00", "end": "17:00:00"},
        {
            "rate": 5,
            "start": "17:00:00",
            "end": "01:00:00",
        },
    ]
    results = my_predbat.basic_rates(simple_rate3, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)
    failed |= assert_rates(results, 0, 1 * 60, 5)
    failed |= assert_rates(results, 1 * 60, 17 * 60, 10)
    failed |= assert_rates(results, 17 * 60, 25 * 60, 5)
    failed |= assert_rates(results, 25 * 60, 17 * 60 + 24 * 60, 10)
    failed |= assert_rates(results, 17 * 60 + 24 * 60, 48 * 60, 5)

    print("*** Running test: Simple rate4")
    rate_override = [{"start": "12:00:00", "end": "13:00:00", "rate_increment": 1}]
    results = my_predbat.basic_rates(simple_rate2, "import")
    results = my_predbat.basic_rates(rate_override, "import", prev=results)
    failed |= assert_rates(results, 0, 12 * 60, 5)
    failed |= assert_rates(results, 12 * 60, 13 * 60, 6)
    failed |= assert_rates(results, 13 * 60, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 9)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 12 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 12 * 60, 24 * 60 + 13 * 60, 6)
    failed |= assert_rates(results, 24 * 60 + 13 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    print("*** Running test: Simple rate5")
    rate_override = [{"start": "12:00:00", "end": "13:00:00", "rate_increment": 1, "date": my_predbat.midnight.strftime("%Y-%m-%d")}]
    print(rate_override)
    results = my_predbat.basic_rates(simple_rate2, "import")
    results = my_predbat.basic_rates(rate_override, "import", prev=results)
    failed |= assert_rates(results, 0, 12 * 60, 5)
    failed |= assert_rates(results, 12 * 60, 13 * 60, 6)
    failed |= assert_rates(results, 13 * 60, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 9)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 12 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 12 * 60, 24 * 60 + 13 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 13 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    # Test 6: Midnight-spanning off-peak with day_of_week (Dutch tariff scenario)
    # Weekdays: 07:00-22:00 = 14 cent (peak), 22:00-07:00 = 8 cent (off-peak spans midnight)
    # Weekends: All day 8 cent flat rate
    # Simulate Saturday afternoon at 14:00
    print("*** Running test: Simple rate6 - Midnight spanning with day_of_week (Saturday afternoon)")
    my_predbat.midnight = datetime.strptime("2025-07-05T00:00:00", "%Y-%m-%dT%H:%M:%S")  # Saturday (day 6)
    old_minutes_now = my_predbat.minutes_now
    my_predbat.minutes_now = 14 * 60  # 14:00 Saturday afternoon
    dutch_rate = [
        {"start": "07:00:00", "end": "22:00:00", "rate": 14.0, "day_of_week": "1,2,3,4,5"},  # Weekday peak
        {"start": "22:00:00", "end": "07:00:00", "rate": 8.0, "day_of_week": "1,2,3,4,5"},  # Weekday off-peak (spans midnight)
        {"rate": 8.0, "day_of_week": "6,7"},  # Weekend flat rate
    ]
    results = my_predbat.basic_rates(dutch_rate, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)

    # Day 1 (Saturday): All day = 8 (weekend flat rate)
    failed |= assert_rates(results, 0, 24 * 60, 8)  # All day Saturday = 8

    # Day 2 (Sunday): All day = 8 (weekend flat rate)
    failed |= assert_rates(results, 24 * 60, 48 * 60, 8)  # All day Sunday = 8

    # Test 7: Monday (weekday) - peak/off-peak pattern
    print("*** Running test: Simple rate7 - Weekday peak/off-peak pattern")
    my_predbat.midnight = datetime.strptime("2025-07-07T00:00:00", "%Y-%m-%dT%H:%M:%S")  # Monday (day 1)
    my_predbat.minutes_now = 14 * 60  # 14:00 Monday afternoon
    results = my_predbat.basic_rates(dutch_rate, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)

    # Day 1 (Monday): 00:00-07:00 = 8 (off-peak from previous night), 07:00-22:00 = 14 (peak), 22:00-24:00 = 8 (off-peak)
    failed |= assert_rates(results, 0, 7 * 60, 8)  # 00:00-07:00 off-peak
    failed |= assert_rates(results, 7 * 60, 22 * 60, 14)  # 07:00-22:00 peak
    failed |= assert_rates(results, 22 * 60, 24 * 60, 8)  # 22:00-24:00 off-peak

    # Day 2 (Tuesday): Same pattern as Monday
    failed |= assert_rates(results, 24 * 60, 24 * 60 + 7 * 60, 8)  # 00:00-07:00 off-peak
    failed |= assert_rates(results, 24 * 60 + 7 * 60, 24 * 60 + 22 * 60, 14)  # 07:00-22:00 peak
    failed |= assert_rates(results, 24 * 60 + 22 * 60, 48 * 60, 8)  # 22:00-24:00 off-peak

    my_predbat.minutes_now = old_minutes_now
    my_predbat.midnight = old_midnight

    # Test 8: predbat_manual_api rate override only marks the actually-overridden window in
    # rate_replicate, not the whole day (issue #2578). get_manual_api() returns each override
    # wrapped as {"index": ..., "value": {...}} - before the fix, basic_rates() used that wrapper
    # directly as if it were the flat {"start", "end", "rate"} shape, so every lookup missed,
    # falling through to the "00:00:00" start/end default (which wraps to a full 24-hour range
    # since end<=start) and rate_increment=True/rate=0 (a real no-op on the rate value, but not on
    # the marker) - silently flagging every minute of the day as overridden.
    print("*** Running test: Manual API rate override marker scoping (issue #2578)")
    old_manual_api = my_predbat.manual_api
    my_predbat.manual_api = ["rates_import_override?start=17:00:00&end=19:00:00&rate=0"]
    try:
        info = my_predbat.get_arg("rates_import_override", [], indirect=False)
        base_rates = {minute: 25.0 for minute in range(-24 * 60, 48 * 60)}
        rate_replicate = {}
        results = my_predbat.basic_rates(info, "rates_import_override", base_rates, rate_replicate)

        if results.get(17 * 60 + 30) != 0.0:
            print(f"ERROR: Expected the overridden rate at 17:30 to be 0.0, got {results.get(17 * 60 + 30)}")
            failed = 1
        if results.get(10 * 60) != 25.0:
            print(f"ERROR: Expected the unaffected rate at 10:00 to stay 25.0, got {results.get(10 * 60)}")
            failed = 1
        if 10 * 60 in rate_replicate:
            print(f"ERROR: Expected minute 10:00 to NOT be marked as overridden, got {rate_replicate.get(10 * 60)!r}")
            failed = 1
        if 17 * 60 + 30 not in rate_replicate:
            print("ERROR: Expected minute 17:30 to be marked as overridden")
            failed = 1
        # Every 24-hour period the override recurs in should mark exactly its own 120-minute
        # window (17:00-19:00), never the whole day either side of it
        for minute in (0, 6 * 60, 16 * 60 + 59, 19 * 60, 20 * 60, 23 * 60 + 59):
            if minute in rate_replicate:
                print(f"ERROR: Minute {minute} outside the override window should not be marked, got {rate_replicate.get(minute)!r}")
                failed = 1
    finally:
        my_predbat.manual_api = old_manual_api

    # Test 9: manual API override is actually applied when it isn't already present in `info`
    # (e.g. rates_import, which - unlike rates_import_override - get_arg() never pre-merges).
    # This is the append branch of basic_rates()'s manual_items dedup loop.
    print("*** Running test: Manual API override applied when not pre-merged into info")
    old_manual_api = my_predbat.manual_api
    my_predbat.manual_api = ["rates_import?start=17:00:00&end=19:00:00&rate=3.5"]
    try:
        base_rates = {minute: 25.0 for minute in range(-24 * 60, 48 * 60)}
        rate_replicate = {}
        results = my_predbat.basic_rates([], "rates_import", base_rates, rate_replicate)

        if results.get(17 * 60 + 30) != 3.5:
            print(f"ERROR: Expected manual API override rate 3.5 at 17:30, got {results.get(17 * 60 + 30)}")
            failed = 1
        if results.get(10 * 60) != 25.0:
            print(f"ERROR: Expected the unaffected rate at 10:00 to stay 25.0, got {results.get(10 * 60)}")
            failed = 1
    finally:
        my_predbat.manual_api = old_manual_api

    # Test 10: a manual API override with an empty rate value (e.g. "...&rate=" with nothing after
    # the "=") must not crash basic_rates() - it should be treated as a bad rate and skipped.
    print("*** Running test: Manual API override with empty rate value does not crash")
    old_manual_api = my_predbat.manual_api
    my_predbat.manual_api = ["rates_import?start=17:00:00&end=19:00:00&rate="]
    try:
        base_rates = {minute: 25.0 for minute in range(-24 * 60, 48 * 60)}
        rate_replicate = {}
        try:
            results = my_predbat.basic_rates([], "rates_import", base_rates, rate_replicate)
        except IndexError as e:
            print(f"ERROR: basic_rates() raised IndexError on an empty manual API rate value: {e}")
            failed = 1
        else:
            if results.get(17 * 60 + 30) != 25.0:
                print(f"ERROR: Expected the bad override to be skipped, leaving 25.0 at 17:30, got {results.get(17 * 60 + 30)}")
                failed = 1
    finally:
        my_predbat.manual_api = old_manual_api

    # Test 11: include_manual_api=False (used by tariff comparison and annual replay, which
    # simulate a tariff other than the live one) must keep the live system's manual API overrides
    # out of the simulated result, even though `info` doesn't already contain them.
    print("*** Running test: include_manual_api=False keeps live overrides out of simulated tariffs")
    old_manual_api = my_predbat.manual_api
    my_predbat.manual_api = ["rates_import_override?start=17:00:00&end=19:00:00&rate=0"]
    try:
        base_rates = {minute: 25.0 for minute in range(-24 * 60, 48 * 60)}
        rate_replicate = {}
        results = my_predbat.basic_rates([], "rates_import_override", base_rates, rate_replicate, include_manual_api=False)

        if results.get(17 * 60 + 30) != 25.0:
            print(f"ERROR: Live manual API override leaked into a simulated tariff at 17:30, got {results.get(17 * 60 + 30)}")
            failed = 1
    finally:
        my_predbat.manual_api = old_manual_api

    # Test 12: a manual API override in the flat "command=value" shorthand form (rather than the
    # documented "command?start=...&end=...&rate=..." form) is not a usable rate override - it
    # must be reported via record_status(had_errors=True), not silently dropped.
    print("*** Running test: Manual API override in unsupported shorthand form is reported, not silently dropped")
    old_manual_api = my_predbat.manual_api
    old_had_errors = my_predbat.had_errors
    my_predbat.manual_api = ["rates_import=5"]
    my_predbat.had_errors = False
    try:
        base_rates = {minute: 25.0 for minute in range(-24 * 60, 48 * 60)}
        rate_replicate = {}
        results = my_predbat.basic_rates([], "rates_import", base_rates, rate_replicate)

        if not my_predbat.had_errors:
            print("ERROR: Expected had_errors to be set when a shorthand-form manual API rate override is ignored")
            failed = 1
        if results.get(10 * 60) != 25.0:
            print(f"ERROR: Expected the unusable override to be ignored, leaving 25.0 at 10:00, got {results.get(10 * 60)}")
            failed = 1
    finally:
        my_predbat.manual_api = old_manual_api
        my_predbat.had_errors = old_had_errors

    return failed
