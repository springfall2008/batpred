# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
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
    return failed