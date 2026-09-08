# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import json
import yaml
from datetime import datetime, timedelta, timezone


def test_energydataservice(my_predbat):
    """
    Test the energy data service
    """
    failed = 0

    print("Test energy data service")
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    data_example = f"""
state: 1.34
state_class: total
current_price: 1.2706032
unit: kWh
currency: EUR
region: Finland
region_code: FI
tomorrow_valid: true
next_data_update: 13:39:54
today: 1.242, 1.242, 1.242, 1.242, 1.243, 1.243, 1.243, 1.243, 1.243, 1.243, 1.244, 1.245, 1.246, 1.246, 1.261, 1.271, 1.286, 1.295, 1.295, 1.288, 1.296, 1.284, 1.273, 1.289
tomorrow: 1.284, 1.273, 1.263, 1.283, 1.309, 1.333, 1.385, 1.389, 1.366, 1.336, 1.323, 1.316, 1.318, 1.338, 1.338, 1.314, 1.299, 1.299, 1.297, 1.286, 1.281, 1.277, 1.268
raw_today:
    - hour: '{today}T00:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T01:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T02:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T03:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T04:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T05:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T06:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T07:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T08:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T09:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T10:00:00+{tz_offset}:00'
      price: 1.244
    - hour: '{today}T11:00:00+{tz_offset}:00'
      price: 1.245
    - hour: '{today}T12:00:00+{tz_offset}:00'
      price: 1.246
    - hour: '{today}T13:00:00+{tz_offset}:00'
      price: 1.246
    - hour: '{today}T14:00:00+{tz_offset}:00'
      price: 1.261
    - hour: '{today}T15:00:00+{tz_offset}:00'
      price: 1.271
    - hour: '{today}T16:00:00+{tz_offset}:00'
      price: 1.286
    - hour: '{today}T17:00:00+{tz_offset}:00'
      price: 1.295
    - hour: '{today}T18:00:00+{tz_offset}:00'
      price: 1.295
    - hour: '{today}T19:00:00+{tz_offset}:00'
      price: 1.288
    - hour: '{today}T20:00:00+{tz_offset}:00'
      price: 1.296
    - hour: '{today}T21:00:00+{tz_offset}:00'
      price: 1.284
    - hour: '{today}T22:00:00+{tz_offset}:00'
      price: 1.273
    - hour: '{today}T23:00:00+{tz_offset}:00'
      price: 1.289
raw_tomorrow:
    - hour: '{tomorrow}T00:00:00+{tz_offset}:00'
      price: 1.284
    - hour: '{tomorrow}T01:00:00+{tz_offset}:00'
      price: 1.273
    - hour: '{tomorrow}T02:00:00+{tz_offset}:00'
      price: 1.263
    - hour: '{tomorrow}T03:00:00+{tz_offset}:00'
      price: 1.283
    - hour: '{tomorrow}T04:00:00+{tz_offset}:00'
      price: 1.309
    - hour: '{tomorrow}T05:00:00+{tz_offset}:00'
      price: 1.333
    - hour: '{tomorrow}T06:00:00+{tz_offset}:00'
      price: 1.385
    - hour: '{tomorrow}T07:00:00+{tz_offset}:00'
      price: 1.389
    - hour: '{tomorrow}T08:00:00+{tz_offset}:00'
      price: 1.366
    - hour: '{tomorrow}T09:00:00+{tz_offset}:00'
      price: 1.336
    - hour: '{tomorrow}T10:00:00+{tz_offset}:00'
      price: 1.323
    - hour: '{tomorrow}T11:00:00+{tz_offset}:00'
      price: 1.316
    - hour: '{tomorrow}T12:00:00+{tz_offset}:00'
      price: 1.318
    - hour: '{tomorrow}T13:00:00+{tz_offset}:00'
      price: 1.338
    - hour: '{tomorrow}T14:00:00+{tz_offset}:00'
      price: 1.338
    - hour: '{tomorrow}T15:00:00+{tz_offset}:00'
      price: 1.314
    - hour: '{tomorrow}T16:00:00+{tz_offset}:00'
      price: 1.299
    - hour: '{tomorrow}T17:00:00+{tz_offset}:00'
      price: 1.299
    - hour: '{tomorrow}T18:00:00+{tz_offset}:00'
      price: 1.297
    - hour: '{tomorrow}T19:00:00+{tz_offset}:00'
      price: 1.286
    - hour: '{tomorrow}T20:00:00+{tz_offset}:00'
      price: 1.281
    - hour: '{tomorrow}T21:00:00+{tz_offset}:00'
      price: 1.277
    - hour: '{tomorrow}T22:00:00+{tz_offset}:00'
      price: 1.268
    - hour: '{tomorrow}T23:00:00+{tz_offset}:00'
      price: 1.268
today_min:
    hour: '{today}T00:00:00+{tz_offset}:00'
    price: 1.242
today_max:
    hour: '{today}T20:00:00+{tz_offset}:00'
    price: 1.296
today_mean: 1.26
tomorrow_min:
    hour: '{tomorrow}T02:00:00+{tz_offset}:00'
    price: 1.263
tomorrow_max:
    hour: '{tomorrow}T07:00:00+{tz_offset}:00'
    price: 1.389
tomorrow_mean: 1.312
use_cent: false
attribution: Data sourced from Nord Pool
unit_of_measurement: EUR/kWh
device_class: monetary
icon: mdi:flash
friendly_name: Energi Data Service
"""

    ha = my_predbat.ha_interface
    ha.dummy_items["sensor.energi_data_service"] = yaml.safe_load(data_example)
    my_predbat.args["energi_data_service"] = "sensor.energi_data_service"
    rates = my_predbat.fetch_energidataservice_rates("sensor.energi_data_service")

    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(rates[minute])

    expected_show = [
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.4,
        124.4,
        124.4,
        124.4,
        124.5,
        124.5,
        124.5,
        124.5,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        126.1,
        126.1,
        126.1,
        126.1,
        127.1,
        127.1,
        127.1,
        127.1,
        128.6,
        128.6,
        128.6,
        128.6,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        128.8,
        128.8,
        128.8,
        128.8,
        129.6,
        129.6,
        129.6,
        129.6,
        128.4,
        128.4,
        128.4,
        128.4,
        127.3,
        127.3,
        127.3,
        127.3,
        128.9,
        128.9,
        128.9,
        128.9,
        128.4,
        128.4,
        128.4,
        128.4,
        127.3,
        127.3,
        127.3,
        127.3,
        126.3,
        126.3,
        126.3,
        126.3,
        128.3,
        128.3,
        128.3,
        128.3,
        130.9,
        130.9,
        130.9,
        130.9,
        133.3,
        133.3,
        133.3,
        133.3,
        138.5,
        138.5,
        138.5,
        138.5,
        138.9,
        138.9,
        138.9,
        138.9,
        136.6,
        136.6,
        136.6,
        136.6,
        133.6,
        133.6,
        133.6,
        133.6,
        132.3,
        132.3,
        132.3,
        132.3,
        131.6,
        131.6,
        131.6,
        131.6,
        131.8,
        131.8,
        131.8,
        131.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        131.4,
        131.4,
        131.4,
        131.4,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.7,
        129.7,
        129.7,
        129.7,
        128.6,
        128.6,
        128.6,
        128.6,
        128.1,
        128.1,
        128.1,
        128.1,
        127.7,
        127.7,
        127.7,
        127.7,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
    ]

    if json.dumps(show) != json.dumps(expected_show):
        print("ERROR: Expecting show should be:\n {} got:\n {}".format(expected_show, show))
        failed = 1

    return failed


def test_energydataservice_dst_spring_forward(my_predbat):
    """
    DST regression: Europe/Copenhagen spring forward (2024-03-31).
    Clocks jump 02:00 -> 03:00 (CET+01:00 -> CEST+02:00), producing a 23-hour day.
    Minute indices must be computed via UTC, not local clock time, so the DST gap
    (missing 2 AM) must not cause an offset shift in the rate dictionary.
    """
    failed = 0
    print("Test energy data service DST spring forward")

    # midnight in Copenhagen on spring-forward day: 2024-03-31T00:00:00+01:00
    midnight = datetime(2024, 3, 31, 0, 0, 0, tzinfo=timezone(timedelta(hours=1)))

    # 23 hourly entries; 2 AM does not exist (spring forward skips it).
    # Entries before the DST switch use +01:00; entries from 3 AM onward use +02:00.
    prices = [
        ("2024-03-31T00:00:00+01:00", 10.0),
        ("2024-03-31T01:00:00+01:00", 11.0),
        # 02:00 does not exist
        ("2024-03-31T03:00:00+02:00", 12.0),
        ("2024-03-31T04:00:00+02:00", 13.0),
        ("2024-03-31T05:00:00+02:00", 14.0),
        ("2024-03-31T06:00:00+02:00", 15.0),
        ("2024-03-31T07:00:00+02:00", 16.0),
        ("2024-03-31T08:00:00+02:00", 17.0),
        ("2024-03-31T09:00:00+02:00", 18.0),
        ("2024-03-31T10:00:00+02:00", 19.0),
        ("2024-03-31T11:00:00+02:00", 20.0),
        ("2024-03-31T12:00:00+02:00", 21.0),
        ("2024-03-31T13:00:00+02:00", 22.0),
        ("2024-03-31T14:00:00+02:00", 23.0),
        ("2024-03-31T15:00:00+02:00", 24.0),
        ("2024-03-31T16:00:00+02:00", 25.0),
        ("2024-03-31T17:00:00+02:00", 26.0),
        ("2024-03-31T18:00:00+02:00", 27.0),
        ("2024-03-31T19:00:00+02:00", 28.0),
        ("2024-03-31T20:00:00+02:00", 29.0),
        ("2024-03-31T21:00:00+02:00", 30.0),
        ("2024-03-31T22:00:00+02:00", 31.0),
        ("2024-03-31T23:00:00+02:00", 32.0),
    ]

    data = [{"hour": ts, "price": p} for ts, p in prices]
    rates = my_predbat.minute_data_hourly_rates(
        data,
        my_predbat.forecast_days + 1,
        midnight,
        rate_key="price",
        from_key="hour",
        scale=1.0,
        use_cent=True,
    )

    # Expected minute offsets (all computed via UTC relative to midnight+01:00):
    #   minute   0 = 00:00+01:00 = 23:00 UTC prev day  -> rate 10.0
    #   minute  60 = 01:00+01:00 = 00:00 UTC           -> rate 11.0
    #   minute 120 = 03:00+02:00 = 01:00 UTC (2 AM gap) -> rate 12.0
    #   minute 180 = 04:00+02:00 = 02:00 UTC           -> rate 13.0
    #   minute 1320 = 23:00+02:00 = 21:00 UTC          -> rate 32.0 (last hour of 23-hour day)
    expected = {
        0: 10.0,
        30: 10.0,
        59: 10.0,
        60: 11.0,
        119: 11.0,
        120: 12.0,
        179: 12.0,
        180: 13.0,
        1320: 32.0,
        1379: 32.0,
    }

    for minute, rate in expected.items():
        if rates.get(minute) != rate:
            print(f"ERROR: DST spring forward: minute {minute} expected {rate}, got {rates.get(minute)}")
            failed = 1

    # Regression: minute 0 must be present (old bug shifted entire dataset to avoid KeyError)
    if 0 not in rates:
        print("ERROR: DST spring forward: minute 0 missing (offset-shift regression)")
        failed = 1

    # Slots for the skipped 2 AM (minutes 120-179) must hold the 03:00+02:00 rate, not be absent
    for minute in range(120, 180):
        if rates.get(minute) != 12.0:
            print(f"ERROR: DST spring forward: minute {minute} (DST gap slot) expected 12.0, got {rates.get(minute)}")
            failed = 1
            break

    return failed


def test_energydataservice_dst_fall_back(my_predbat):
    """
    DST regression: Europe/Copenhagen fall back (2024-10-27).
    Clocks jump 03:00 -> 02:00 (CEST+02:00 -> CET+01:00), producing a 25-hour day.
    The 02:00 hour appears twice with different UTC offsets; each occurrence must
    map to a distinct minute slot (120 and 180 respectively).
    """
    failed = 0
    print("Test energy data service DST fall back")

    # midnight in Copenhagen on fall-back day: 2024-10-27T00:00:00+02:00
    midnight = datetime(2024, 10, 27, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))

    # 25 hourly entries; 02:00 appears twice (once as +02:00 CEST, once as +01:00 CET).
    prices = [
        ("2024-10-27T00:00:00+02:00", 10.0),
        ("2024-10-27T01:00:00+02:00", 11.0),
        ("2024-10-27T02:00:00+02:00", 12.0),  # first 2 AM (CEST)
        ("2024-10-27T02:00:00+01:00", 13.0),  # second 2 AM (CET, after fallback)
        ("2024-10-27T03:00:00+01:00", 14.0),
        ("2024-10-27T04:00:00+01:00", 15.0),
        ("2024-10-27T05:00:00+01:00", 16.0),
        ("2024-10-27T06:00:00+01:00", 17.0),
        ("2024-10-27T07:00:00+01:00", 18.0),
        ("2024-10-27T08:00:00+01:00", 19.0),
        ("2024-10-27T09:00:00+01:00", 20.0),
        ("2024-10-27T10:00:00+01:00", 21.0),
        ("2024-10-27T11:00:00+01:00", 22.0),
        ("2024-10-27T12:00:00+01:00", 23.0),
        ("2024-10-27T13:00:00+01:00", 24.0),
        ("2024-10-27T14:00:00+01:00", 25.0),
        ("2024-10-27T15:00:00+01:00", 26.0),
        ("2024-10-27T16:00:00+01:00", 27.0),
        ("2024-10-27T17:00:00+01:00", 28.0),
        ("2024-10-27T18:00:00+01:00", 29.0),
        ("2024-10-27T19:00:00+01:00", 30.0),
        ("2024-10-27T20:00:00+01:00", 31.0),
        ("2024-10-27T21:00:00+01:00", 32.0),
        ("2024-10-27T22:00:00+01:00", 33.0),
        ("2024-10-27T23:00:00+01:00", 34.0),  # 24 hours after midnight = minute 1440
    ]

    data = [{"hour": ts, "price": p} for ts, p in prices]
    rates = my_predbat.minute_data_hourly_rates(
        data,
        my_predbat.forecast_days + 1,
        midnight,
        rate_key="price",
        from_key="hour",
        scale=1.0,
        use_cent=True,
    )

    # Expected minute offsets (all via UTC relative to midnight+02:00 = 22:00 UTC prev day):
    #   minute   0 = 00:00+02:00 = 22:00 UTC prev  -> rate 10.0
    #   minute  60 = 01:00+02:00 = 23:00 UTC prev  -> rate 11.0
    #   minute 120 = 02:00+02:00 = 00:00 UTC        -> rate 12.0 (first 2 AM)
    #   minute 180 = 02:00+01:00 = 01:00 UTC        -> rate 13.0 (second 2 AM)
    #   minute 240 = 03:00+01:00 = 02:00 UTC        -> rate 14.0
    #   minute 1440 = 23:00+01:00 = 22:00 UTC       -> rate 34.0 (last of 25-hour day)
    expected = {
        0: 10.0,
        60: 11.0,
        120: 12.0,
        179: 12.0,
        180: 13.0,
        239: 13.0,
        240: 14.0,
        1440: 34.0,
        1499: 34.0,
    }

    for minute, rate in expected.items():
        if rates.get(minute) != rate:
            print(f"ERROR: DST fall back: minute {minute} expected {rate}, got {rates.get(minute)}")
            failed = 1

    # Regression: minute 0 must be present
    if 0 not in rates:
        print("ERROR: DST fall back: minute 0 missing (offset-shift regression)")
        failed = 1

    # Regression: duplicate 2 AM hours must map to distinct minute slots
    if rates.get(120) == rates.get(180):
        print(f"ERROR: DST fall back: both 2 AM occurrences map to same slot (rate {rates.get(120)}) - UTC disambiguation failed")
        failed = 1

    return failed


def test_energydataservice_dst_fill_forward(my_predbat):
    """
    Regression for the fill-forward fix: if minute 0 is absent from rate_data,
    gaps at the start must be filled from 24 hours ahead when available, or from
    the first valid rate - never by shifting the entire dataset.
    """
    failed = 0
    print("Test energy data service DST fill-forward (no offset shift)")

    midnight = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=1)))

    # Scenario A: data starts at minute 60, but minute 0+1440 exists (24h-ahead rate).
    # Minute 0 should be filled from minute 1440, not by shifting minute 60 -> minute 0.
    data_with_future = [
        {"hour": "2024-06-01T01:00:00+01:00", "price": 50.0},  # minute 60
        {"hour": "2024-06-01T02:00:00+01:00", "price": 51.0},  # minute 120
        {"hour": "2024-06-02T00:00:00+01:00", "price": 99.0},  # minute 1440 (24h ahead of minute 0)
    ]
    rates_a = my_predbat.minute_data_hourly_rates(
        data_with_future,
        my_predbat.forecast_days + 1,
        midnight,
        rate_key="price",
        from_key="hour",
        scale=1.0,
        use_cent=True,
    )

    # minute 60 must still hold the 01:00 rate (not shifted away)
    if rates_a.get(60) != 50.0:
        print(f"ERROR: fill-forward A: minute 60 expected 50.0 (no shift), got {rates_a.get(60)}")
        failed = 1
    # minutes 0-59 filled from minute+1440 (all 99.0 since the 24h-ahead slot covers 1440-1499)
    for m in range(0, 60):
        if rates_a.get(m) != 99.0:
            print(f"ERROR: fill-forward A: minute {m} expected 99.0 (from +24h slot), got {rates_a.get(m)}")
            failed = 1
            break

    # Scenario B: data starts at minute 60, no 24h-ahead entry.
    # Minute 0 should be filled with the first valid rate (50.0), not trigger a shift.
    data_no_future = [
        {"hour": "2024-06-01T01:00:00+01:00", "price": 50.0},  # minute 60
        {"hour": "2024-06-01T02:00:00+01:00", "price": 51.0},  # minute 120
    ]
    rates_b = my_predbat.minute_data_hourly_rates(
        data_no_future,
        my_predbat.forecast_days + 1,
        midnight,
        rate_key="price",
        from_key="hour",
        scale=1.0,
        use_cent=True,
    )

    # minute 60 must still hold the 01:00 rate (not shifted away)
    if rates_b.get(60) != 50.0:
        print(f"ERROR: fill-forward B: minute 60 expected 50.0 (no shift), got {rates_b.get(60)}")
        failed = 1
    # minute 0 must be filled with first valid rate (50.0)
    if rates_b.get(0) != 50.0:
        print(f"ERROR: fill-forward B: minute 0 expected 50.0 (first rate fallback), got {rates_b.get(0)}")
        failed = 1

    return failed


def run_energydataservice_tests(my_predbat):
    """Run all Energi Data Service tests."""
    failed = 0
    failed += test_energydataservice(my_predbat)
    failed += test_energydataservice_dst_spring_forward(my_predbat)
    failed += test_energydataservice_dst_fall_back(my_predbat)
    failed += test_energydataservice_dst_fill_forward(my_predbat)
    return failed
