# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import json
import yaml
from datetime import datetime, timedelta


def test_energydataservice(my_predbat):
    """
    Test the energy data service
    """
    failed = 0

    print("Test energy data service")

    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
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
