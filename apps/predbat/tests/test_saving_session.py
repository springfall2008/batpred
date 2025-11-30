# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import yaml
import json
from datetime import datetime, timedelta

def test_saving_session(my_predbat):
    """
    Test the octopus saving session
    """
    print("Test saving session")
    ha = my_predbat.ha_interface
    failed = False
    date_last_year = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    date_before_yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"
    session_binary = f"""

state: off
current_joined_event_start: '{date_today}T16:30:00+{tz_offset}:00'
current_joined_event_end: '{date_today}T17:30:00+{tz_offset}:00'
current_joined_event_duration_in_minutes: 60
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session (A-4DD6C5EE)
""".format(
        date_last_year=date_last_year, date_yesterday=date_yesterday, date_today=date_today, date_before_yesterday=date_before_yesterday, tz_offset=tz_offset
    )

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 1336
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: 987654
joined_events:
    - id: 1327
      start: '{date_last_year}T17:00:00+{tz_offset}:00'
      end: '{date_last_year}T18:00:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: 936
      octopoints_per_kwh: 576
    - id: 1334
      start: '{date_yesterday}T17:30:00+{tz_offset}:00'
      end: '{date_yesterday}T18:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 192
    - id: 1335
      start: '{date_today}T16:30:00+{tz_offset}:00'
      end: '{date_today}T17:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 448
    - id: 1336
      start: '{date_before_yesterday}T23:30:00+{tz_offset}:00'
      end: '{date_yesterday}T10:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 448
friendly_name: Octoplus Saving Session Events (A-12345678)
""".format(
        date_last_year=date_last_year, date_yesterday=date_yesterday, date_today=date_today, tz_offset=tz_offset
    )
    ha.dummy_items["binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_a_12345678_octoplus_saving_session_events"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"
    my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
    if "octopus_free_url" in my_predbat.args:
        del my_predbat.args["octopus_free_url"]
    my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10

    ha.service_store_enable = True
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    expected_saving = [
        {"start": "{}T17:30:00+{}:00".format(date_yesterday, tz_offset), "end": "{}T18:30:00+{}:00".format(date_yesterday, tz_offset), "rate": 19.2, "state": False},
        {"start": "{}T16:30:00+{}:00".format(date_today, tz_offset), "end": "{}T17:30:00+{}:00".format(date_today, tz_offset), "rate": 44.8, "state": False},
        {"start": "{}T23:30:00+{}:00".format(date_before_yesterday, tz_offset), "end": "{}T10:30:00+{}:00".format(date_yesterday, tz_offset), "rate": 44.8, "state": False},
    ]

    # Example format Sat 25/01
    date_today_service = datetime.now().strftime("%a %d/%m")
    expected_service = [
        ["octopus_energy/join_octoplus_saving_session_event", {"event_code": 987654, "entity_id": "event.octopus_energy_a_12345678_octoplus_saving_session_events"}],
        ["notify/notify", {"message": "Predbat: Joined Octopus saving event {} 18:30-19:30, 50.0 p/kWh".format(date_today_service)}],
    ]

    if json.dumps(octopus_saving_slots) != json.dumps(expected_saving):
        print("ERROR: Expecting saving slots should be {} got {}".format(expected_saving, octopus_saving_slots))
        failed = 1
    if json.dumps(service_result) != json.dumps(expected_service):
        print("ERROR: Expecting service store should be {} got {}".format(expected_service, service_result))
        failed = 1
    if octopus_free_slots:
        print("ERROR: Expecting no free slots")
        failed = 1

    rate_import_replicated = {}
    my_predbat.rate_import = {n: 0 for n in range(-24 * 60, 48 * 60)}
    my_predbat.load_saving_slot(expected_saving, export=False, rate_replicate=rate_import_replicated)
    price_ranges = [[(17.5 - 24) * 60, (18.5 - 24) * 60, 19.2], [(16.5) * 60, (17.5) * 60, 44.8], [-24 * 60, (10.5 - 24) * 60, 44.8]]
    for minute in range(-24 * 60, 48 * 60):
        rate = my_predbat.rate_import[minute]
        in_range = False
        for price_range in price_ranges:
            if minute >= price_range[0] and minute < price_range[1]:
                if rate != price_range[2]:
                    print("ERROR: Load Octopus Saving - minute {} Expecting rate to be {} got {}".format(minute, price_range[2], rate))
                    failed = 1
                    break
                in_range = True
        if not in_range:
            if rate != 0:
                print("ERROR: Load Octopus Saving - minute {} Expecting rate to be 0 got {}".format(minute, rate))
                failed = 1
                break

    return failed
