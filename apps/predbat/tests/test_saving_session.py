# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import yaml
import json
from datetime import datetime, timedelta
from ha import run_async


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
    ha.dummy_items["event.octopus_energy_a_12345678_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "event.octopus_energy_a_12345678_octoplus_saving_session_event"
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
        ["octopus_energy/join_octoplus_power_down_session_event", {"event_code": 987654, "entity_id": "event.octopus_energy_a_12345678_octoplus_saving_session_event"}],
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
    my_predbat.load_saving_slot(expected_saving, my_predbat.rate_import, export=False, rate_replicate=rate_import_replicated)
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


def test_saving_session_null_octopoints(my_predbat):
    """
    Test the octopus saving session with null octopoints_per_kwh
    This tests the fix for GitHub issue #3079
    """
    print("Test saving session with null octopoints_per_kwh (issue #3079)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    # Simulate data from a user who is no longer enrolled in saving sessions
    # All octopoints_per_kwh values are null, which previously caused TypeError
    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events: []
joined_events:
    - id: 1342
      start: '2025-03-03T18:00:00+00:00'
      end: '2025-03-03T19:00:00+00:00'
      duration_in_minutes: 60
      rewarded_octopoints: 296
      octopoints_per_kwh: null
      code: null
    - id: 1343
      start: '2025-03-04T18:00:00+00:00'
      end: '2025-03-04T19:00:00+00:00'
      duration_in_minutes: 60
      rewarded_octopoints: 16
      octopoints_per_kwh: null
      code: null
    - id: 1344
      start: '2025-03-05T18:00:00+00:00'
      end: '2025-03-05T19:00:00+00:00'
      duration_in_minutes: 60
      rewarded_octopoints: 64
      octopoints_per_kwh: null
      code: null
friendly_name: Octopus Intelligent Saving Sessions
"""

    session_binary = f"""
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:currency-usd
friendly_name: Octopus Intelligent Saving Sessions
"""

    ha.dummy_items["binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_a_12345678_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "event.octopus_energy_a_12345678_octoplus_saving_session_event"
    my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
    if "octopus_free_url" in my_predbat.args:
        del my_predbat.args["octopus_free_url"]
    my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 8

    # This should not raise TypeError anymore
    try:
        octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    except TypeError as e:
        print(f"ERROR: TypeError raised when handling null octopoints_per_kwh: {e}")
        failed = True
        return failed

    # All events with null octopoints_per_kwh should be ignored
    if octopus_saving_slots:
        print(f"ERROR: Expected no saving slots (null octopoints_per_kwh should be ignored), got {octopus_saving_slots}")
        failed = True

    if octopus_free_slots:
        print("ERROR: Expecting no free slots")
        failed = True

    print("PASS: Null octopoints_per_kwh handled correctly - no TypeError raised, events ignored")
    return failed


def test_saving_session_notify_config(my_predbat):
    """
    Test that set_event_notify configuration controls Octopus saving session notifications
    """
    print("Test saving session notification configuration")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_binary = f"""
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session
"""

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""

    # Test 1: Notifications enabled (default)
    print("  Test 1: Notifications enabled (set_event_notify not set, should default to True)")
    ha.dummy_items.clear()
    ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
    my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
    if "octopus_free_url" in my_predbat.args:
        del my_predbat.args["octopus_free_url"]
    my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
    # Don't set set_event_notify - should default to True
    if "set_event_notify" in my_predbat.args:
        del my_predbat.args["set_event_notify"]
    # Reset the last joined try timer so it will attempt to join
    my_predbat.octopus_last_joined_try = None

    ha.service_store_enable = True
    ha.service_store = []
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    # Should have notification service call
    notify_calls = [svc for svc in service_result if svc[0] == "notify/notify"]
    if len(notify_calls) != 1:
        print(f"ERROR: Expected 1 notification call with default set_event_notify, got {len(notify_calls)}")
        print(f"  Service calls: {service_result}")
        failed = True
    else:
        print("  PASS: Notification sent when set_event_notify defaults to True")

    # Test 2: Notifications explicitly enabled
    print("  Test 2: Notifications explicitly enabled (set_event_notify=True)")
    ha.dummy_items.clear()
    ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["set_event_notify"] = True
    # Reset the last joined try timer so it will attempt to join
    my_predbat.octopus_last_joined_try = None

    ha.service_store_enable = True
    ha.service_store = []
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    # Should have notification service call
    notify_calls = [svc for svc in service_result if svc[0] == "notify/notify"]
    if len(notify_calls) != 1:
        print(f"ERROR: Expected 1 notification call with set_event_notify=True, got {len(notify_calls)}")
        print(f"  Service calls: {service_result}")
        failed = True
    else:
        print("  PASS: Notification sent when set_event_notify=True")

    # Test 3: Notifications disabled
    print("  Test 3: Notifications disabled (set_event_notify=False)")
    ha.dummy_items.clear()
    ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    # Update config_index to set the value to False
    my_predbat.expose_config("set_event_notify", False, quiet=True)
    # Reset the last joined try timer so it will attempt to join
    my_predbat.octopus_last_joined_try = None

    ha.service_store_enable = True
    ha.service_store = []
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    # Should NOT have notification service call
    notify_calls = [svc for svc in service_result if svc[0] == "notify/notify"]
    if len(notify_calls) != 0:
        print(f"ERROR: Expected 0 notification calls with set_event_notify=False, got {len(notify_calls)}")
        print(f"  Service calls: {service_result}")
        failed = True
    else:
        print("  PASS: Notification blocked when set_event_notify=False")

    # Verify that the saving session was still joined (only notification blocked, not the join)
    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join service call even with notifications disabled, got {len(join_calls)}")
        failed = True
    else:
        print("  PASS: Saving session still joined when notifications disabled")

    if not failed:
        print("PASS: All notification configuration tests passed")

    return failed


def test_saving_session_axle_conflict(my_predbat):
    """
    Test that an available Octopus saving session is not auto-joined when it overlaps an Axle VPP session
    Covers GitHub issue #4120
    """
    print("Test saving session Axle conflict avoidance (issue #4120)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_binary = f"""
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session
"""

    # A single available saving session 18:30-19:30
    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""

    def setup_items():
        ha.dummy_items.clear()
        ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
        ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        # Reset throttle so a join is attempted
        my_predbat.octopus_last_joined_try = None

    # Test 1: Overlapping Axle session (Axle 19:00-20:00 overlaps saving 18:30-19:30) -> no join
    print("  Test 1: Overlapping Axle session blocks the join")
    setup_items()
    axle_overlap = [
        {
            "start_time": f"{date_today}T19:00:00+{tz_offset}:00",
            "end_time": f"{date_today}T20:00:00+{tz_offset}:00",
            "import_export": "export",
            "pence_per_kwh": 50,
        }
    ]
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions(axle_overlap)
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if join_calls:
        print(f"ERROR: Expected no join when overlapping an Axle session, got {join_calls}")
        failed = True
    else:
        print("  PASS: Join skipped when saving session overlaps an Axle session")

    # Test 2: Non-overlapping Axle session (Axle 12:00-13:00) -> join proceeds
    print("  Test 2: Non-overlapping Axle session allows the join")
    setup_items()
    axle_clear = [
        {
            "start_time": f"{date_today}T12:00:00+{tz_offset}:00",
            "end_time": f"{date_today}T13:00:00+{tz_offset}:00",
            "import_export": "export",
            "pence_per_kwh": 50,
        }
    ]
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions(axle_clear)
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join when Axle session does not overlap, got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds when no Axle session overlaps")

    # Test 3: No Axle sessions at all -> join proceeds (backwards compatible default)
    print("  Test 3: No Axle sessions allows the join")
    setup_items()
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join when no Axle sessions provided, got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds when no Axle sessions are provided")

    if not failed:
        print("PASS: All Axle conflict avoidance tests passed")

    # Restore default throttle state so we do not leak it to other tests
    my_predbat.octopus_last_joined_try = None

    return failed


def test_saving_session_zero_rate_skip(my_predbat):
    """
    Test that an available Octopus saving event with a zero (or negative) octopoints_per_kwh is not
    auto-joined, while a positive-rate event still is, and a null-rate event still falls back to the
    default rate unaffected. Covers GitHub issue #4593.

    The Octopus integration currently puts national Power Up (free electricity) events into the Power
    Down available_events set at 0 p/kWh (#4548 point 5), so without this guard every one of those gets
    join-attempted and rejected.
    """
    print("Test saving session zero reward rate is skipped (issue #4593)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_binary = """
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session
"""

    def setup_items(octopoints_per_kwh_yaml):
        session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: {octopoints_per_kwh_yaml}
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""
        ha.dummy_items.clear()
        ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
        ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        # Reset throttle so a join is attempted
        my_predbat.octopus_last_joined_try = None

    # Test 1: octopoints_per_kwh: 0 -> no join attempted
    print("  Test 1: Zero reward rate blocks the join")
    setup_items("0")
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if join_calls:
        print(f"ERROR: Expected no join for a zero reward rate event, got {join_calls}")
        failed = True
    else:
        print("  PASS: Join skipped for a zero reward rate event")

    # Test 2: octopoints_per_kwh: -10 -> no join attempted (defensive, matches saving_rate > 0 elsewhere)
    print("  Test 2: Negative reward rate blocks the join")
    setup_items("-10")
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if join_calls:
        print(f"ERROR: Expected no join for a negative reward rate event, got {join_calls}")
        failed = True
    else:
        print("  PASS: Join skipped for a negative reward rate event")

    # Test 3: octopoints_per_kwh: 500 -> join proceeds (unaffected)
    print("  Test 3: Positive reward rate still allows the join")
    setup_items("500")
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join for a positive reward rate event, got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds for a positive reward rate event")

    # Test 4: octopoints_per_kwh: null -> join proceeds using the default rate (unaffected)
    print("  Test 4: Null reward rate still allows the join (falls back to default rate)")
    setup_items("null")
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join for a null reward rate event (default rate applies), got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds for a null reward rate event")

    if not failed:
        print("PASS: All zero reward rate tests passed")

    # Restore default throttle state so we do not leak it to other tests
    my_predbat.octopus_last_joined_try = None

    return failed


def test_saving_session_min_octopoints_threshold(my_predbat):
    """
    Test that octopus_saving_session_min_octopoints_per_kwh gates the auto-join at a user-configured
    reward level, not just the fixed >0 check covered by test_saving_session_zero_rate_skip. Covers
    GitHub issue #4595 (gcoan's review of #4593): a user may want to also skip genuine but low-value
    Power Down sessions, not just the integration's mis-categorised zero-reward Power Up events.
    """
    print("Test saving session minimum octopoints threshold (issue #4595)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_binary = """
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session
"""

    def setup_items(octopoints_per_kwh_yaml, min_threshold=None):
        session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: {octopoints_per_kwh_yaml}
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""
        ha.dummy_items.clear()
        ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
        ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        if min_threshold is None:
            my_predbat.args.pop("octopus_saving_session_min_octopoints_per_kwh", None)
        else:
            my_predbat.args["octopus_saving_session_min_octopoints_per_kwh"] = min_threshold
        # Reset throttle so a join is attempted
        my_predbat.octopus_last_joined_try = None

    # Test 1: no threshold configured, moderate reward -> join proceeds (default threshold is 0)
    print("  Test 1: Default threshold (unset, i.e. 0) still allows a moderate reward")
    setup_items("50")
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join with the default threshold, got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds with the default threshold")

    # Test 2: threshold raised above the event's reward -> join is skipped
    print("  Test 2: A threshold above the event's reward blocks the join")
    setup_items("50", min_threshold=100)
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if join_calls:
        print(f"ERROR: Expected no join when the reward does not exceed the configured threshold, got {join_calls}")
        failed = True
    else:
        print("  PASS: Join skipped when the reward does not exceed the configured threshold")

    # Test 3: threshold raised but the event's reward still exceeds it -> join proceeds
    print("  Test 3: A reward that still exceeds a raised threshold still joins")
    setup_items("500", min_threshold=100)
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join when the reward exceeds the configured threshold, got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds when the reward exceeds the configured threshold")

    # Test 4: reward exactly equal to the threshold -> join is skipped (exceeds, not meets)
    print("  Test 4: A reward exactly equal to the threshold does not exceed it, so is skipped")
    setup_items("100", min_threshold=100)
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if join_calls:
        print(f"ERROR: Expected no join when the reward exactly equals the configured threshold, got {join_calls}")
        failed = True
    else:
        print("  PASS: Join skipped when the reward exactly equals the configured threshold")

    if not failed:
        print("PASS: All minimum octopoints threshold tests passed")

    # Restore default throttle/config state so we do not leak it to other tests
    my_predbat.octopus_last_joined_try = None
    my_predbat.args.pop("octopus_saving_session_min_octopoints_per_kwh", None)

    return failed


def test_saving_session_join_service_fallback(my_predbat):
    """
    Test that auto-join tries the current Bottle Cap Dave join service
    (join_octoplus_power_down_session_event) first, falling back to the deprecated
    join_octoplus_saving_session_event only when the current one is unavailable (e.g. an
    integration version that predates the Power Down rename). Covers GitHub issue #4548 point 3.
    """
    print("Test saving session join service fallback (issue #4548 point 3)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_binary = """
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session
"""

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""

    def setup_items():
        ha.dummy_items.clear()
        ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
        ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        # No octopus_saving_session_join configured, so this exercises the Bottle Cap Dave service
        # branch, not the select-entity branch (Octopus Energy Direct or similar)
        if "octopus_saving_session_join" in my_predbat.args:
            del my_predbat.args["octopus_saving_session_join"]
        my_predbat.octopus_last_joined_try = None
        my_predbat.octopus_join_service_power_down = None

    # Test 1: current service available -> used directly, no fallback
    print("  Test 1: Current service used when available")
    setup_items()
    ha.service_store_fail = set()
    ha.service_store_enable = True
    ha.service_store = []
    try:
        my_predbat.fetch_octopus_sessions()
        service_result = ha.get_service_store()
    finally:
        # TestHAInterface is a shared singleton across the whole test run - an exception mid-test
        # must not leak service_store_enable/service_store_fail into unrelated later tests.
        ha.service_store_enable = False
        ha.service_store_fail = set()

    services_called = [svc[0] for svc in service_result]
    if "octopus_energy/join_octoplus_power_down_session_event" not in services_called:
        print(f"ERROR: Expected the current service to be called, got {services_called}")
        failed = True
    elif "octopus_energy/join_octoplus_saving_session_event" in services_called:
        print(f"ERROR: Deprecated service should not be called when the current one succeeds, got {services_called}")
        failed = True
    else:
        print("  PASS: Current service called, no fallback")

    # Test 2: current service unavailable (older integration) -> falls back to the deprecated one
    print("  Test 2: Falls back to deprecated service when the current one is unavailable")
    setup_items()
    ha.service_store_fail = {"octopus_energy/join_octoplus_power_down_session_event"}
    ha.service_store_enable = True
    ha.service_store = []
    try:
        my_predbat.fetch_octopus_sessions()
        service_result = ha.get_service_store()
    finally:
        ha.service_store_enable = False
        ha.service_store_fail = set()

    services_called = [svc[0] for svc in service_result]
    if "octopus_energy/join_octoplus_saving_session_event" not in services_called:
        print(f"ERROR: Expected fallback to the deprecated service, got {services_called}")
        failed = True
    else:
        print("  PASS: Fell back to the deprecated service")

    # Test 3: once the current service is confirmed to work, a later join doesn't re-probe it - it's
    # called directly and no fallback is attempted even if the mock would otherwise report failure
    print("  Test 3: A confirmed-working current service is cached, not re-probed")
    setup_items()
    ha.service_store_fail = set()
    ha.service_store_enable = True
    ha.service_store = []
    try:
        my_predbat.fetch_octopus_sessions()
    finally:
        ha.service_store_enable = False
    if my_predbat.octopus_join_service_power_down is not True:
        print(f"ERROR: Expected octopus_join_service_power_down to be cached True after a successful join, got {my_predbat.octopus_join_service_power_down}")
        failed = True

    # Re-arm the same available event without resetting the cache, and make the mock report the
    # current service as failing - if the cache is respected, it's still the only one called.
    ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    my_predbat.octopus_last_joined_try = None
    ha.service_store_fail = {"octopus_energy/join_octoplus_power_down_session_event"}
    ha.service_store_enable = True
    ha.service_store = []
    try:
        my_predbat.fetch_octopus_sessions()
        service_result = ha.get_service_store()
    finally:
        ha.service_store_enable = False
        ha.service_store_fail = set()

    services_called = [svc[0] for svc in service_result]
    if "octopus_energy/join_octoplus_power_down_session_event" not in services_called:
        print(f"ERROR: Expected the cached current service to still be called to perform the join, got {services_called}")
        failed = True
    elif "octopus_energy/join_octoplus_saving_session_event" in services_called:
        print(f"ERROR: Expected no fallback/re-probe once the current service is cached as working, got {services_called}")
        failed = True
    else:
        print("  PASS: Cached current service used directly, no re-probe or fallback")

    if not failed:
        print("PASS: All join service fallback tests passed")

    my_predbat.octopus_last_joined_try = None
    my_predbat.octopus_join_service_power_down = None

    return failed


def test_trigger_callback_success_signal(my_predbat):
    """
    Test that UserInterface.trigger_callback() itself returns a real True/False success signal,
    not just the TestHAInterface mock used by test_saving_session_join_service_fallback.

    trigger_callback() is the production code HAInterface.call_service()'s loopback branch (used
    whenever websocket_active is False - standalone/Predbat.com/Docker installs with no linked HA,
    or transiently during a reconnect) delegates to. Before this fix it had no return statement at
    all, so it always returned None regardless of success - meaning octopus.py's
    `if not self.call_service_wrapper(...)` join-fallback logic would treat every loopback service
    call as "unavailable" and always fire the deprecated fallback service too, on every single join,
    indefinitely. This test exercises the real function directly so a regression here can't hide
    behind the mock the way it did before (issue raised in #4601 review).
    """
    print("Test trigger_callback returns a real success signal")
    failed = False

    result = run_async(my_predbat.trigger_callback({"domain": "switch", "service": "turn_on", "service_data": {}}))
    if result is not True:
        print(f"ERROR: Expected True for a matching EVENT_LISTEN_LIST entry, got {result}")
        failed = True
    else:
        print("  PASS: Matching listener returns True")

    result = run_async(my_predbat.trigger_callback({"domain": "octopus_energy", "service": "join_octoplus_power_down_session_event", "service_data": {}}))
    if result:
        print(f"ERROR: Expected a falsy result for a service with no matching listener (e.g. a third-party integration service loopback can't simulate), got {result}")
        failed = True
    else:
        print("  PASS: Unmatched service returns a falsy result")

    if not failed:
        print("PASS: All trigger_callback success signal tests passed")

    return failed


def test_saving_session_auto_join_toggle(my_predbat):
    """
    Test that the octopus_saving_auto_join switch controls whether available saving sessions are auto-joined
    Covers GitHub issue #4120
    """
    print("Test saving session auto-join toggle (issue #4120)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_binary = f"""
state: off
current_joined_event_start: null
current_joined_event_end: null
current_joined_event_duration_in_minutes: null
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session
"""

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""

    def setup_items():
        ha.dummy_items.clear()
        ha.dummy_items["binary_sensor.octopus_energy_test_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
        ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        # Reset throttle so a join is attempted
        my_predbat.octopus_last_joined_try = None

    # Test 1: auto-join disabled -> no join
    print("  Test 1: octopus_saving_auto_join=False blocks the join")
    setup_items()
    my_predbat.expose_config("octopus_saving_auto_join", False, quiet=True)
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if join_calls:
        print(f"ERROR: Expected no join when auto-join disabled, got {join_calls}")
        failed = True
    else:
        print("  PASS: Join skipped when octopus_saving_auto_join is False")

    # Test 2: auto-join enabled -> join proceeds
    print("  Test 2: octopus_saving_auto_join=True allows the join")
    setup_items()
    my_predbat.expose_config("octopus_saving_auto_join", True, quiet=True)
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    join_calls = [svc for svc in service_result if "join" in svc[0]]
    if len(join_calls) != 1:
        print(f"ERROR: Expected 1 join when auto-join enabled, got {len(join_calls)}: {service_result}")
        failed = True
    else:
        print("  PASS: Join proceeds when octopus_saving_auto_join is True")

    if not failed:
        print("PASS: All auto-join toggle tests passed")

    # Restore default state so we do not leak it to other tests
    my_predbat.expose_config("octopus_saving_auto_join", True, quiet=True)
    my_predbat.octopus_last_joined_try = None

    return failed


def test_saving_session_custom_entity_no_rewrite_match(my_predbat):
    """
    Test that available_events is read from the configured entity even when its name
    does not match the binary_sensor -> event rewrite pattern (no '_sessions' substring),
    and no rewritten entity exists at all.
    Covers GitHub issue #4573
    """
    print("Test saving session with custom entity name that does not match the rewrite pattern (issue #4573)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    # Custom entity name bridging a saving session event. It has no '_sessions' substring
    # so the legacy binary_sensor -> event rewrite would point at a non-existent entity if
    # it were ever applied. joined_events is empty (nothing joined yet) but available_events
    # is populated - this is exactly the state auto-join needs to act on.
    session_binary = f"""
state: off
available_events:
    - id: 9999
      start: '{date_today}T18:00:00+{tz_offset}:00'
      end: '{date_today}T19:00:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 505
      code: EVENT_TEST
joined_events: []
friendly_name: Predbat Octopus Power Down For Predbat
"""

    saved_args = my_predbat.args.copy()
    try:
        ha.dummy_items.clear()
        ha.dummy_items["binary_sensor.predbat_octopus_power_down_for_predbat"] = yaml.safe_load(session_binary)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        my_predbat.args["octopus_saving_session"] = "binary_sensor.predbat_octopus_power_down_for_predbat"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        if "octopus_saving_session_join" in my_predbat.args:
            del my_predbat.args["octopus_saving_session_join"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        # Reset throttle so a join is attempted
        my_predbat.octopus_last_joined_try = None

        ha.service_store_enable = True
        ha.service_store = []
        my_predbat.fetch_octopus_sessions()
        service_result = ha.get_service_store()
        ha.service_store_enable = False

        join_calls = [svc for svc in service_result if "join" in svc[0]]
        if len(join_calls) != 1:
            print(f"ERROR: Expected 1 join call reading available_events from the configured entity, got {len(join_calls)}: {service_result}")
            failed = True
        elif join_calls[0][1].get("entity_id") != "binary_sensor.predbat_octopus_power_down_for_predbat":
            print(f"ERROR: Expected join call to use the configured entity, got {join_calls[0][1]}")
            failed = True
        else:
            print("  PASS: available_events read from the configured entity despite no rewrite match")

        if not failed:
            print("PASS: Custom entity name (no rewrite match) auto-join test passed")
    finally:
        my_predbat.args = saved_args
        # Restore default throttle state so we do not leak it to other tests
        my_predbat.octopus_last_joined_try = None

    return failed


def test_saving_session_select_entity_join_defers_notify(my_predbat):
    """
    Test that the select-entity join path (octopus_saving_session_join, used by Octopus Energy
    Direct or similar) writes the select entity but sends no "joined" notification itself - the
    real join happens asynchronously on a later cycle (OctopusAPI's own select_event() ->
    process_commands() -> async_join_saving_session_events()), so notifying at write time would
    claim success before the join has even been attempted. Covers GitHub issue #4593.
    """
    print("Test select-entity join defers the joined notification (issue #4593)")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
account_id: A-4DD6C5EE
available_events:
    - id: 9999
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: TEST123
joined_events: []
friendly_name: Octoplus Saving Session Events
"""

    saved_args = my_predbat.args.copy()
    try:
        ha.dummy_items.clear()
        ha.dummy_items["event.octopus_energy_test_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
        ha.dummy_items["sensor.octopus_free_session"] = {}
        ha.dummy_items["select.predbat_saving_session_join"] = {"state": "", "attributes": {"options": []}}
        my_predbat.args["octopus_saving_session"] = "event.octopus_energy_test_octoplus_saving_session_event"
        my_predbat.args["octopus_saving_session_join"] = "select.predbat_saving_session_join"
        my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
        if "octopus_free_url" in my_predbat.args:
            del my_predbat.args["octopus_free_url"]
        my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10
        my_predbat.octopus_last_joined_try = None

        ha.service_store_enable = True
        ha.service_store = []
        my_predbat.fetch_octopus_sessions()
        service_result = ha.get_service_store()
        ha.service_store_enable = False

        select_calls = [svc for svc in service_result if svc[0] == "select/select_option"]
        notify_calls = [svc for svc in service_result if svc[0] == "notify/notify"]
        if len(select_calls) != 1:
            print(f"ERROR: Expected 1 select_option call to queue the join, got {len(select_calls)}: {service_result}")
            failed = True
        elif select_calls[0][1].get("entity_id") != "select.predbat_saving_session_join" or select_calls[0][1].get("option") != "TEST123":
            print(f"ERROR: select_option call had unexpected args: {select_calls[0][1]}")
            failed = True
        elif notify_calls:
            print(f"ERROR: Expected no notification from the select-entity path (deferred to the real join), got {notify_calls}")
            failed = True
        else:
            print("PASS: select_option queued the join with no premature notification")
    finally:
        my_predbat.args = saved_args
        my_predbat.octopus_last_joined_try = None

    return failed


def test_saving_session_default_rate(my_predbat):
    """
    Test that saving sessions with no octopoints_per_kwh use the default rate
    from octopus_saving_session_rate config (flexibility API migration)
    """
    print("Test saving session default rate injection")
    ha = my_predbat.ha_interface
    failed = False
    date_today = datetime.now().strftime("%Y-%m-%d")
    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    # Simulate new API format: joined events with no octopoints data
    session_binary = f"""
state: off
current_joined_event_start: '{date_today}T17:00:00+{tz_offset}:00'
current_joined_event_end: '{date_today}T18:00:00+{tz_offset}:00'
current_joined_event_duration_in_minutes: 60
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session (A-TEST1234)
"""

    # Events with null octopoints_per_kwh — simulating new flexibility API
    # which does not return rewardPerKwhInOctoPoints
    session_sensor = f"""
state: '2026-03-23T12:00:00+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-TEST1234
available_events: []
joined_events:
    - id: 2001
      start: '{date_today}T17:00:00+{tz_offset}:00'
      end: '{date_today}T18:00:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: null
      code: FLEX-2001
friendly_name: Octoplus Saving Session Events
"""

    ha.dummy_items["binary_sensor.octopus_energy_a_test1234_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_a_test1234_octoplus_saving_session_event"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "event.octopus_energy_a_test1234_octoplus_saving_session_event"
    my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
    if "octopus_free_url" in my_predbat.args:
        del my_predbat.args["octopus_free_url"]
    my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 8
    my_predbat.args["octopus_saving_session_rate"] = 100  # 100 p/kWh default

    ha.service_store_enable = True
    ha.service_store = []
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    ha.service_store_enable = False

    # Should have saving slots with default rate injected
    # octopus_saving_session_rate=100 p/kWh used as fallback when octopoints_per_kwh is null
    if not octopus_saving_slots:
        print("ERROR: Expected saving slots with default rate, got none")
        failed = True
    else:
        slot = octopus_saving_slots[0]
        expected_rate = 100.0  # default_rate_pence (100) * octopoints_per_penny (8) / octopoints_per_penny (8) = 100 p/kWh
        if slot.get("rate") != expected_rate:
            print(f"ERROR: Expected default rate {expected_rate}, got {slot.get('rate')}")
            failed = True
        else:
            print(f"PASS: Default rate correctly injected for null octopoints: {expected_rate} p/kWh")

    return failed


def test_saving_session_entity_regex_power_rename(my_predbat):
    """
    Test that the octopus_saving_session/octopus_free_session apps.yaml 're:' patterns
    match both the deprecated saving-session/free-electricity entity names and the
    replacement Power Down/Power Up entity names introduced by Octopus integration
    v19.0.0, and that each pattern does not cross-match the other event type.
    Covers GitHub issue #4548 point 2.
    """
    print("Test octopus_saving_session/octopus_free_session entity regex Power Down/Up rename (issue #4548 point 2)")
    failed = False

    saving_pattern = "re:(event.octopus_energy([0-9a-z_]+|)_(saving_session_events?|power_down_events))"
    free_pattern = "re:(event.octopus_energy_([0-9a-z_]+|)_octoplus_(free_electricity_session_events|power_up_events))"

    cases = [
        # (arg, pattern, state_keys, expected_match, description)
        ("octopus_saving_session", saving_pattern, ["event.octopus_energy_a4dd6c5ee_octoplus_saving_session_events"], "event.octopus_energy_a4dd6c5ee_octoplus_saving_session_events", "deprecated saving-session entity"),
        ("octopus_saving_session", saving_pattern, ["event.octopus_energy_a4dd6c5ee_octoplus_power_down_events"], "event.octopus_energy_a4dd6c5ee_octoplus_power_down_events", "new Power Down entity"),
        ("octopus_saving_session", saving_pattern, ["event.octopus_energy_a4dd6c5ee_octoplus_power_up_events"], None, "Power Up entity must not match the saving-session/Power Down pattern"),
        ("octopus_free_session", free_pattern, ["event.octopus_energy_a4dd6c5ee_octoplus_free_electricity_session_events"], "event.octopus_energy_a4dd6c5ee_octoplus_free_electricity_session_events", "deprecated free-electricity entity"),
        ("octopus_free_session", free_pattern, ["event.octopus_energy_a4dd6c5ee_octoplus_power_up_events"], "event.octopus_energy_a4dd6c5ee_octoplus_power_up_events", "new Power Up entity"),
        ("octopus_free_session", free_pattern, ["event.octopus_energy_a4dd6c5ee_octoplus_power_down_events"], None, "Power Down entity must not match the free-session/Power Up pattern"),
    ]

    for arg, pattern, state_keys, expected, description in cases:
        matched, resolved = my_predbat.resolve_arg_re(arg, pattern, state_keys)
        if expected is None:
            if matched:
                print(f"ERROR: {description} - expected no match, got {resolved}")
                failed = True
            else:
                print(f"PASS: {description} correctly did not match")
        else:
            if not matched or resolved != expected:
                print(f"ERROR: {description} - expected {expected}, got matched={matched} resolved={resolved}")
                failed = True
            else:
                print(f"PASS: {description} resolved to {resolved}")

    return failed
