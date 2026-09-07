# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
from datetime import datetime, timedelta, timezone

from const import CAR_CHARGING_LIMIT_UNCAPPED
from prediction import Prediction
from tests.test_infra import reset_rates


def process_octopus_intelligent_slots(my_predbat):
    """
    Helper function to simulate the octopus intelligent slot processing from fetch_sensor_data
    """
    entity_id_config = my_predbat.get_arg("octopus_intelligent_slot", indirect=False)

    # Normalize to list
    if entity_id_config and not isinstance(entity_id_config, list):
        entity_id_list = [entity_id_config]
    elif entity_id_config:
        entity_id_list = entity_id_config
    else:
        entity_id_list = []

    # Process each car - match production: octopus_slots is a nested list [per-car slots]
    for car_n in range(min(len(entity_id_list), my_predbat.num_cars)):
        entity_id = entity_id_list[car_n]
        if not entity_id:
            continue

        completed = my_predbat.get_state_wrapper(entity_id=entity_id, attribute="completed_dispatches") or []
        planned = my_predbat.get_state_wrapper(entity_id=entity_id, attribute="planned_dispatches") or []

        if completed:
            my_predbat.octopus_slots[car_n] += completed
        if planned:
            my_predbat.octopus_slots[car_n] += planned


def run_multi_car_iog_test(testname, my_predbat):
    """
    Test multi-car Intelligent Octopus Go (IOG) support
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    # Setup test data - similar to what fetch_sensor_data does
    my_predbat.num_cars = 2
    my_predbat.car_charging_planned = [True, True]  # Both cars plugged in
    my_predbat.car_charging_now = [False, False]
    my_predbat.car_charging_plan_smart = [False, False]
    my_predbat.car_charging_plan_max_price = [0, 0]
    my_predbat.car_charging_plan_time = ["07:00:00", "07:00:00"]
    my_predbat.car_charging_battery_size = [100.0, 80.0]
    my_predbat.car_charging_limit = [100.0, 80.0]
    my_predbat.car_charging_rate = [7.4, 7.4]
    my_predbat.car_charging_slots = [[], []]
    my_predbat.car_charging_exclusive = [False, False]
    my_predbat.car_charging_loss = 1.0
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = False
    my_predbat.octopus_intelligent_consider_full = False
    # Match production: octopus_slots is a nested list, one sub-list per car
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]

    # Test 1: Single car config (backward compatibility)
    print("Test 1: Single car config (backward compatibility)")
    my_predbat.args["octopus_intelligent_slot"] = "binary_sensor.octopus_energy_intelligent_dispatching"

    # Mock entity state
    slot1_start = (my_predbat.now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot1_end = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")

    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot1_start, "end": slot1_end, "charge_in_kwh": 10.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 100.0,
            "charge_point_power_in_kw": 7.4,
        },
    )

    # Simulate the octopus intelligent slot processing from fetch_sensor_data
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]
    process_octopus_intelligent_slots(my_predbat)

    # Car 0 should have 1 slot; car 1 (no entity) should be empty
    if len(my_predbat.octopus_slots[0]) != 1:
        print("ERROR: Expected 1 slot for car 0, got {}".format(len(my_predbat.octopus_slots[0])))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True
    elif len(my_predbat.octopus_slots[1]) != 0:
        print("ERROR: Expected 0 slots for car 1 (not configured), got {}".format(len(my_predbat.octopus_slots[1])))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True

    # Test 2: Multi-car config
    print("Test 2: Multi-car config with two cars")
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]
    my_predbat.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_energy_intelligent_dispatching_car1", "binary_sensor.octopus_energy_intelligent_dispatching_car2"]

    # Mock entity states for both cars
    slot2_start = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot2_end = (my_predbat.now_utc + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S%z")

    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car1",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot1_start, "end": slot1_end, "charge_in_kwh": 10.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 100.0,
            "charge_point_power_in_kw": 7.4,
        },
    )

    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car2",
        "on",
        attributes={"completed_dispatches": [], "planned_dispatches": [{"start": slot2_start, "end": slot2_end, "charge_in_kwh": 8.0, "source": "smart-charge", "location": "AT_HOME"}], "vehicle_battery_size_in_kwh": 80.0, "charge_point_power_in_kw": 7.4},
    )

    # Simulate the octopus intelligent slot processing
    process_octopus_intelligent_slots(my_predbat)

    # Each car should have exactly 1 slot in its own sub-list
    if len(my_predbat.octopus_slots[0]) != 1:
        print("ERROR: Expected 1 slot for car 0, got {}".format(len(my_predbat.octopus_slots[0])))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True
    elif len(my_predbat.octopus_slots[1]) != 1:
        print("ERROR: Expected 1 slot for car 1, got {}".format(len(my_predbat.octopus_slots[1])))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True

    # Test 3: Multi-car config with None/empty second entity
    print("Test 3: Multi-car config with one empty/None slot")
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]
    my_predbat.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_energy_intelligent_dispatching_car1", None]

    # Simulate the octopus intelligent slot processing
    process_octopus_intelligent_slots(my_predbat)

    # Car 0 should have 1 slot; car 1 entity is None so skipped
    if len(my_predbat.octopus_slots[0]) != 1:
        print("ERROR: Expected 1 slot for car 0, got {}".format(len(my_predbat.octopus_slots[0])))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True
    elif len(my_predbat.octopus_slots[1]) != 0:
        print("ERROR: Expected 0 slots for car 1 (None entity), got {}".format(len(my_predbat.octopus_slots[1])))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_multi_car_iog_load_slots_test(testname, my_predbat):
    """
    Regression test for bug #3515: IndexError when load_octopus_slots is called for car 1
    because car_charging_soc was not initialized before fetch_sensor_data_cars ran.

    This test calls fetch_sensor_data_cars() directly (the function that contains the bug)
    so it will reproduce the IndexError if the fix is ever reverted.
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    # Setup: two cars, both with IOG slots
    my_predbat.num_cars = 2
    my_predbat.car_charging_planned = [True, True]
    my_predbat.car_charging_now = [False, False]
    my_predbat.car_charging_plan_smart = [False, False]
    my_predbat.car_charging_plan_max_price = [0, 0]
    my_predbat.car_charging_plan_time = ["07:00:00", "07:00:00"]
    my_predbat.car_charging_battery_size = [100.0, 80.0]
    my_predbat.car_charging_limit = [100.0, 80.0]
    my_predbat.car_charging_rate = [7.4, 7.4]
    my_predbat.car_charging_slots = [[], []]
    my_predbat.car_charging_exclusive = [True, True]
    my_predbat.car_charging_manual_soc = [False, False]
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = False
    my_predbat.octopus_intelligent_consider_full = False
    # octopus_slots must be pre-initialised per production code in fetch_sensor_data
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]
    # Pin now_utc to noon today so slot times (now+1h..now+3h) don't cross
    # midnight regardless of when the test runs. Also set minutes_now=0 so
    # slots are not filtered as past events (dynamic_load_car default is 720).
    now = datetime.now(tz=timezone.utc)
    my_predbat.now_utc = now.replace(hour=12, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = 0

    # apps.yaml config args needed by fetch_sensor_data_cars
    my_predbat.args["car_charging_loss"] = 0.0  # loss = 1 - 0.0 = 1.0
    my_predbat.args["car_charging_soc"] = [50.0, 50.0]  # 50% SoC for both cars
    my_predbat.args["car_charging_limit"] = [100.0, 80.0]  # must match num_cars

    # Two IOG sensors - one per car
    slot1_start = (my_predbat.now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot1_end = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot2_start = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot2_end = (my_predbat.now_utc + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S%z")

    my_predbat.args["octopus_intelligent_slot"] = [
        "binary_sensor.octopus_energy_intelligent_dispatching_car1",
        "binary_sensor.octopus_energy_intelligent_dispatching_car2",
    ]

    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car1",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot1_start, "end": slot1_end, "charge_in_kwh": 10.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 100.0,
            "charge_point_power_in_kw": 7.4,
        },
    )
    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car2",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot2_start, "end": slot2_end, "charge_in_kwh": 8.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 80.0,
            "charge_point_power_in_kw": 7.4,
        },
    )

    # This is the real function that contained the bug - call it directly.
    # Before the fix, car_charging_soc was initialized AFTER the IOG loop, so
    # load_octopus_slots(car_n=1, ...) would raise IndexError: list index out of range.
    try:
        my_predbat.fetch_sensor_data_cars()
    except IndexError as exc:
        print("ERROR: fetch_sensor_data_cars raised IndexError (regression of bug #3515): {}".format(exc))
        failed = True

    if not failed:
        # Both cars should have charging slots populated from their IOG dispatches
        if not my_predbat.car_charging_slots[0]:
            print("ERROR: Expected car 0 to have charging slots from IOG, got none")
            failed = True
        if not my_predbat.car_charging_slots[1]:
            print("ERROR: Expected car 1 to have charging slots from IOG, got none")
            failed = True
        # car_charging_soc must be a 2-element list (not the old empty/wrong-length list)
        if len(my_predbat.car_charging_soc) != 2:
            print("ERROR: Expected car_charging_soc to have 2 entries, got {}".format(len(my_predbat.car_charging_soc)))
            failed = True

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_multi_car_iog_unplugged_car_test(testname, my_predbat):
    """
    Regression test for issue #3592: When car 1 has an IOG entity configured but the old code
    only guarded car_n == 0, plan_car_charging() was incorrectly called for car 1 whenever
    car_charging_planned[1] was True (e.g. from stale dispatch slots). This caused car 1's
    charging plan to expand from the small set of IOG dispatch windows to ALL low-rate windows
    - 'charging slots all the time'.

    The fix changes `car_n == 0` to check whether the car is configured with an IOG entity,
    which prevents plan_car_charging() from being called for any IOG-configured car.

    This test exercises fetch_sensor_data_car_planning() directly - the extracted function
    that contains the fixed conditional.
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    # Set up 2 cars, both on IOG
    my_predbat.num_cars = 2
    my_predbat.car_charging_now = [False, False]
    my_predbat.car_charging_plan_smart = [False, False]
    my_predbat.car_charging_plan_max_price = [0, 0]
    my_predbat.car_charging_plan_time = ["23:59:00", "23:59:00"]  # ready at end of day - catches all low_rates windows
    my_predbat.car_charging_battery_size = [100.0, 80.0]
    my_predbat.car_charging_limit = [100.0, 80.0]
    my_predbat.car_charging_rate = [7.4, 7.4]
    my_predbat.car_charging_soc = [50.0, 40.0]
    my_predbat.car_charging_soc_next = [None, None]
    my_predbat.car_charging_exclusive = [False, False]
    my_predbat.car_charging_loss = 1.0
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = False

    # Both cars have IOG entities - the scenario from issue #3592
    my_predbat.args["octopus_intelligent_slot"] = [
        "binary_sensor.octopus_energy_intelligent_dispatching_car1",
        "binary_sensor.octopus_energy_intelligent_dispatching_car2",
    ]

    # Simulate state after fetch_sensor_data_cars() ran:
    # - Both cars have car_charging_planned = True (IOG path sets this when slots are present)
    # - car_charging_slots contains just the IOG-sourced slots (1 per car)
    iog_slot_car0 = [{"start": 60, "end": 120, "kwh": 7.4, "average": 7.0, "cost": 51.8, "soc": 57.4}]
    iog_slot_car1 = [{"start": 120, "end": 180, "kwh": 5.92, "average": 7.0, "cost": 41.44, "soc": 47.4}]
    my_predbat.car_charging_planned = [True, True]
    my_predbat.car_charging_slots = [list(iog_slot_car0), list(iog_slot_car1)]

    # Set up multiple low_rates windows so plan_car_charging would produce many slots if called
    my_predbat.low_rates = [
        {"start": 0, "end": 30, "average": 5.0},
        {"start": 60, "end": 90, "average": 5.0},
        {"start": 120, "end": 150, "average": 5.0},
        {"start": 180, "end": 210, "average": 5.0},
        {"start": 240, "end": 270, "average": 5.0},
        {"start": 1440, "end": 1470, "average": 5.0},
        {"start": 1500, "end": 1530, "average": 5.0},
    ]

    # Call the real function under test
    my_predbat.fetch_sensor_data_car_planning()

    # car 0: IOG car, slots must be unchanged (plan_car_charging must NOT have been called)
    if my_predbat.car_charging_slots[0] != iog_slot_car0:
        print("ERROR: car_charging_slots[0] was mutated - plan_car_charging should not have been called for an IOG car")
        print("  Expected: {}".format(iog_slot_car0))
        print("  Got:      {}".format(my_predbat.car_charging_slots[0]))
        failed = True
    else:
        print("OK: car_charging_slots[0] unchanged - plan_car_charging correctly skipped for IOG car 0")

    # car 1: IOG car, slots must be unchanged (this is the regression from issue #3592)
    if my_predbat.car_charging_slots[1] != iog_slot_car1:
        print("ERROR: car_charging_slots[1] was mutated - plan_car_charging should not have been called for an IOG car (issue #3592)")
        print("  Expected: {}".format(iog_slot_car1))
        print("  Got {} slots: {}".format(len(my_predbat.car_charging_slots[1]), my_predbat.car_charging_slots[1]))
        failed = True
    else:
        print("OK: car_charging_slots[1] unchanged - plan_car_charging correctly skipped for IOG car 1 (fix for issue #3592)")

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_multi_car_shared_charger_exclusive_test(testname, my_predbat):
    """
    Regression test for issue #4305: two car profiles sharing one physical charger (e.g. a single
    Zappi serving two configured car entries) can both read car_charging_planned/now = True off
    the same shared sensor, even though only one car is actually plugged in. The exclusive-charging
    break in fetch_sensor_data_car_planning() used to fire on car 0 regardless of whether car 0
    actually had anything to charge, silently preventing car 1 - the one genuinely plugged in and
    charging - from ever being planned at all.

    Car 0 here has SoC already equal to its own limit (no real charging need, so
    plan_car_charging() correctly returns an empty list for it) but still reads planned=True and
    exclusive=True off the shared sensor. Car 1 has a genuine SoC/limit gap. The fix only lets the
    exclusive break fire once a car has produced an actual non-empty plan, so car 1 must still get
    a chance to be planned.
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    my_predbat.num_cars = 2
    my_predbat.octopus_intelligent_charging = False
    my_predbat.car_charging_now = [True, True]
    my_predbat.car_charging_planned = [True, True]
    my_predbat.car_charging_exclusive = [True, True]
    my_predbat.car_charging_plan_smart = [False, False]
    my_predbat.car_charging_plan_max_price = [0, 0]
    my_predbat.car_charging_plan_time = ["23:59:00", "23:59:00"]
    my_predbat.car_charging_battery_size = [77.0, 84.0]
    my_predbat.car_charging_rate = [7.4, 7.4]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_soc_next = [None, None]
    my_predbat.car_charging_slots = [[], []]

    # Car 0 (not really plugged in - shared sensor false positive): SoC already at its own limit,
    # so plan_car_charging() should correctly produce nothing to do.
    # Car 1 (the one actually plugged in and charging): a genuine gap to close.
    my_predbat.car_charging_soc = [61.6, 63.0]
    my_predbat.car_charging_limit = [61.6, 67.2]

    # Windows relative to minutes_now - plan_car_charging() skips windows that fall before
    # minutes_now, so absolute offsets would go stale depending on what time the test runs.
    now = my_predbat.minutes_now
    my_predbat.low_rates = [
        {"start": now, "end": now + 30, "average": 5.0},
        {"start": now + 60, "end": now + 90, "average": 5.0},
        {"start": now + 120, "end": now + 150, "average": 5.0},
    ]

    my_predbat.fetch_sensor_data_car_planning()

    if my_predbat.car_charging_slots[0]:
        print("ERROR: car 0 should have an empty plan (SoC already at its own limit), got: {}".format(my_predbat.car_charging_slots[0]))
        failed = True
    else:
        print("OK: car 0 correctly produced an empty plan")

    if not my_predbat.car_charging_slots[1]:
        print("ERROR: car 1 should have been planned (issue #4305 regression) - the exclusive break on car 0's empty plan silently skipped car 1")
        failed = True
    else:
        print("OK: car 1 was planned despite car 0's planned=True/exclusive=True (issue #4305 fix)")

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_multi_car_iog_adhoc_dispatch_test(testname, my_predbat):
    """
    Regression test: with octopus_intelligent_ignore_unplugged on, an ad-hoc/short-notice
    Octopus dispatch (e.g. the user forcing a charge by editing the car's target in the Octopus
    app) can be actively charging (car_charging_now True) while car_charging_planned is still
    False - it hasn't yet had a cycle where load_octopus_slots() saw a matching dispatch and
    flipped it. Before the fix, fetch_sensor_data_cars()'s gate at fetch.py only checked
    car_charging_planned, so load_octopus_slots() was never called and car_charging_slots stayed
    empty - the plan/history "car" column silently showed nothing despite the car genuinely
    charging on a real (if ad-hoc) dispatch. This exercises the real fetch_sensor_data_cars() and
    checks car_charging_slots gets populated purely off car_charging_now.
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    my_predbat.num_cars = 1
    my_predbat.car_charging_planned = [False]  # not yet recognised as planned
    my_predbat.car_charging_now = [True]  # but it is actively charging right now
    my_predbat.car_charging_plan_smart = [False]
    my_predbat.car_charging_plan_max_price = [0]
    my_predbat.car_charging_plan_time = ["07:00:00"]
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.car_charging_limit = [100.0]
    my_predbat.car_charging_rate = [7.4]
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_exclusive = [False]
    my_predbat.car_charging_manual_soc = [False]
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = True  # the setting that gates on car_charging_planned
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[]]

    now = datetime.now(tz=timezone.utc)
    my_predbat.now_utc = now.replace(hour=12, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = 0

    my_predbat.args["car_charging_loss"] = 0.0
    my_predbat.args["car_charging_soc"] = [50.0]
    my_predbat.args["car_charging_limit"] = [100.0]
    my_predbat.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_energy_intelligent_dispatching_adhoc"]

    # Ad-hoc dispatch covering right now - as if the user just forced it via the Octopus app
    slot_start = (my_predbat.now_utc - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot_end = (my_predbat.now_utc + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%S%z")
    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_adhoc",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot_start, "end": slot_end, "charge_in_kwh": 3.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 100.0,
            "charge_point_power_in_kw": 7.4,
        },
    )

    my_predbat.fetch_sensor_data_cars()

    if not my_predbat.car_charging_slots[0]:
        print("ERROR: car_charging_slots[0] is empty - the ignore_unplugged gate blocked load_octopus_slots() despite car_charging_now being True")
        failed = True
    else:
        print("OK: car_charging_slots[0] populated from car_charging_now alone: {}".format(my_predbat.car_charging_slots[0]))

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_iog_model_limit_fetch_test(testname, my_predbat):
    """
    Issue #4967: with octopus_intelligent_consider_full off (the default), fetch_sensor_data_cars()
    must publish a model-facing car charge limit (car_charging_limit_model) that is uncapped for
    cars carrying IOG dispatch slots - so predict()'s fill clamp is inert and the prediction trusts
    the Octopus dispatch plan - while leaving the real car_charging_limit untouched and keeping the
    real limit for cars with no IOG slots. With the switch on the override must be absent (None).
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    # fetch_sensor_data_cars() rewrites a lot of shared fixture state (including car_charging_loss,
    # which it reads from the config item, not args) - snapshot and restore everything this test
    # touches so a module that runs after this one is not polluted (the shared-fixture trap)
    snapshot_attrs = [
        "num_cars",
        "car_charging_planned",
        "car_charging_now",
        "car_charging_plan_smart",
        "car_charging_plan_max_price",
        "car_charging_plan_time",
        "car_charging_battery_size",
        "car_charging_limit",
        "car_charging_limit_model",
        "car_charging_rate",
        "car_charging_slots",
        "car_charging_exclusive",
        "car_charging_manual_soc",
        "car_charging_soc",
        "car_charging_soc_next",
        "car_charging_loss",
        "octopus_intelligent_charging",
        "octopus_intelligent_ignore_unplugged",
        "octopus_intelligent_consider_full",
        "octopus_slots",
        "now_utc",
        "minutes_now",
    ]
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in snapshot_attrs if hasattr(my_predbat, attr)}
    arg_keys = ["car_charging_loss", "car_charging_soc", "car_charging_limit", "octopus_intelligent_slot"]
    saved_args = {key: copy.deepcopy(my_predbat.args[key]) for key in arg_keys if key in my_predbat.args}

    my_predbat.num_cars = 2
    my_predbat.car_charging_planned = [True, False]
    my_predbat.car_charging_now = [False, False]
    my_predbat.car_charging_plan_smart = [False, False]
    my_predbat.car_charging_plan_max_price = [0, 0]
    my_predbat.car_charging_plan_time = ["07:00:00", "07:00:00"]
    my_predbat.car_charging_battery_size = [100.0, 80.0]
    my_predbat.car_charging_limit = [80.0, 64.0]
    my_predbat.car_charging_rate = [7.4, 7.4]
    my_predbat.car_charging_slots = [[], []]
    my_predbat.car_charging_exclusive = [False, False]
    my_predbat.car_charging_manual_soc = [False, False]
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = True  # car 1 is unplugged, so it must get no IOG slots
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[], []]

    now = datetime.now(tz=timezone.utc)
    my_predbat.now_utc = now.replace(hour=12, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = 0

    my_predbat.args["car_charging_loss"] = 0.0
    my_predbat.args["car_charging_soc"] = [50.0, 50.0]
    my_predbat.args["car_charging_limit"] = [80.0, 80.0]
    my_predbat.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_energy_intelligent_dispatching_car_a", "binary_sensor.octopus_energy_intelligent_dispatching_car_b"]

    slot_start = (my_predbat.now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot_end = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car_a",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot_start, "end": slot_end, "charge_in_kwh": 3.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 100.0,
            "charge_point_power_in_kw": 7.4,
        },
    )
    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car_b",
        "off",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [],
            "vehicle_battery_size_in_kwh": 80.0,
            "charge_point_power_in_kw": 7.4,
        },
    )

    my_predbat.fetch_sensor_data_cars()

    if not my_predbat.car_charging_slots[0]:
        print("ERROR: car 0 should have IOG slots, got none")
        failed = True
    if my_predbat.car_charging_slots[1]:
        print("ERROR: car 1 should have no IOG slots (unplugged), got {}".format(my_predbat.car_charging_slots[1]))
        failed = True

    model = my_predbat.car_charging_limit_model
    if model is None:
        print("ERROR: car_charging_limit_model should be set with consider_full off and IOG slots present, got None")
        failed = True
    else:
        if model[0] != CAR_CHARGING_LIMIT_UNCAPPED:
            print("ERROR: car 0 model limit should be uncapped ({}), got {}".format(CAR_CHARGING_LIMIT_UNCAPPED, model[0]))
            failed = True
        if abs(model[1] - my_predbat.car_charging_limit[1]) > 0.01:
            print("ERROR: car 1 model limit should equal its real limit {}, got {}".format(my_predbat.car_charging_limit[1], model[1]))
            failed = True

    # The real limits must not have been raised - execute.py's car-full decision and the
    # plan_car_charging path still rely on them (80% of the Octopus-reported battery sizes)
    if abs(my_predbat.car_charging_limit[0] - 80.0) > 0.01 or abs(my_predbat.car_charging_limit[1] - 64.0) > 0.01:
        print("ERROR: real car_charging_limit changed unexpectedly: {}".format(my_predbat.car_charging_limit))
        failed = True

    # With consider_full on, load_octopus_slots() itself clamps the plan and predict() must keep
    # the real fill clamp - no model override
    my_predbat.octopus_intelligent_consider_full = True
    my_predbat.car_charging_slots = [[], []]
    my_predbat.octopus_slots = [[], []]  # fetch_sensor_data_cars appends to octopus_slots; the real caller resets it each cycle
    my_predbat.fetch_sensor_data_cars()
    if my_predbat.car_charging_limit_model is not None:
        print("ERROR: car_charging_limit_model should be None with consider_full on, got {}".format(my_predbat.car_charging_limit_model))
        failed = True
    if not my_predbat.car_charging_slots[0]:
        print("ERROR: car 0 should still have IOG slots with consider_full on (limit not yet reached), got none")
        failed = True

    # Restore the shared fixture state captured above
    for key in arg_keys:
        if key in saved_args:
            my_predbat.args[key] = saved_args[key]
        else:
            my_predbat.args.pop(key, None)
    for attr, value in saved.items():
        setattr(my_predbat, attr, value)

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_iog_consider_full_predict_test(testname, my_predbat):
    """
    Issue #4967: predict() clamps car load at Prediction.car_charging_limit and releases the battery
    discharge hold once the modelled car "fills". With the model-facing limit uncapped (IOG car,
    consider_full off) the full slot energy must be delivered and the hold kept for the whole slot;
    with no override (consider_full on, or a non-IOG car) the old clamped behaviour must remain.

    Scenario: one car, 10kWh of dispatch across a 2h slot at 5kW, real limit 5kWh from empty; flat
    1kW house load; battery full at 10kWh with charging disabled. Clamped: the car takes 5kWh (1h),
    the hold then releases and the battery serves the second hour of house load - import 6kWh, final
    SoC 9kWh. Uncapped: all 10kWh delivered, hold kept for both hours - import 12kWh, final SoC 10kWh.
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    snapshot_attrs = [
        "num_cars",
        "car_charging_slots",
        "car_charging_soc",
        "car_charging_soc_next",
        "car_charging_limit",
        "car_charging_limit_model",
        "car_charging_battery_size",
        "car_charging_loss",
        "car_energy_reported_load",
        "car_charging_from_battery",
        "set_charge_window",
        "set_export_window",
        "soc_kw",
        "soc_max",
        "reserve",
        "battery_loss",
        "battery_loss_discharge",
        "inverter_loss",
        "inverter_hybrid",
        "battery_rate_max_charge",
        "battery_rate_max_charge_dc",
        "battery_rate_max_discharge",
        "battery_rate_max_export",
        "battery_rate_min",
        "charge_rate_now",
        "discharge_rate_now",
        "inverter_limit",
        "export_limit",
        "pv_ac_limit",
        "import_today_now",
        "export_today_now",
        "load_minutes_now",
        "pv_today_now",
        "cost_today_sofar",
        "carbon_today_sofar",
        "iboost_today",
        "iboost_enable",
        "carbon_enable",
        "io_adjusted",
        "rate_import",
        "rate_export",
        "rate_min",
        "rate_max",
        "rate_min_base",
        "rate_max_base",
        "rate_export_min",
        "minutes_now",
        "combine_charge_slots",
        "best_soc_keep",
    ]
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in snapshot_attrs if hasattr(my_predbat, attr)}

    try:
        my_predbat.minutes_now = 0
        my_predbat.num_cars = 1
        my_predbat.car_charging_slots = [[{"start": 0, "end": 120, "kwh": 10.0, "average": 7.5, "cost": 75.0, "soc": 10.0, "octopus": True}]]
        my_predbat.car_charging_soc = [0.0]
        my_predbat.car_charging_soc_next = [None]
        my_predbat.car_charging_limit = [5.0]
        my_predbat.car_charging_battery_size = [50.0]
        my_predbat.car_charging_loss = 1.0
        my_predbat.car_energy_reported_load = True  # count the car behind the CT so it shows up in import
        my_predbat.car_charging_from_battery = False
        my_predbat.set_charge_window = True  # required for the car discharge hold in predict()
        my_predbat.set_export_window = False
        my_predbat.soc_kw = 10.0
        my_predbat.soc_max = 10.0
        my_predbat.reserve = 0.0
        my_predbat.battery_loss = 1.0
        my_predbat.battery_loss_discharge = 1.0
        my_predbat.inverter_loss = 1.0
        my_predbat.inverter_hybrid = False
        my_predbat.battery_rate_max_charge = 1 / 60.0
        my_predbat.battery_rate_max_charge_dc = 1 / 60.0
        my_predbat.battery_rate_max_discharge = 1 / 60.0
        my_predbat.battery_rate_max_export = 1 / 60.0
        my_predbat.battery_rate_min = 0
        my_predbat.charge_rate_now = 1 / 60.0
        my_predbat.discharge_rate_now = 1 / 60.0
        my_predbat.inverter_limit = 2 / 60.0
        my_predbat.export_limit = 10 / 60.0
        my_predbat.pv_ac_limit = 0
        my_predbat.import_today_now = 0.0
        my_predbat.export_today_now = 0.0
        my_predbat.load_minutes_now = 0.0
        my_predbat.pv_today_now = 0.0
        my_predbat.cost_today_sofar = 0.0
        my_predbat.carbon_today_sofar = 0.0
        my_predbat.iboost_today = 0.0
        my_predbat.iboost_enable = False
        my_predbat.carbon_enable = False
        my_predbat.io_adjusted = {}
        my_predbat.best_soc_keep = 0.0
        reset_rates(my_predbat, 10.0, 0.0)

        pv_step = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
        load_step = {minute: 1.0 / 12.0 for minute in range(0, my_predbat.forecast_minutes, 5)}  # flat 1kW house load
        end_record = 120

        results = {}
        for scenario, model_limit in [("clamped", None), ("uncapped", [CAR_CHARGING_LIMIT_UNCAPPED])]:
            my_predbat.car_charging_limit_model = model_limit
            pred = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
            (metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, final_soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g, predict_soc, car_soc_next, iboost_next, ib_run, ib_solar, ib_full) = pred.run_prediction(
                [], [], [], [], False, end_record
            )
            results[scenario] = {"import": import_kwh_battery + import_kwh_house, "soc": final_soc}
            print("  scenario {}: import {}kWh final soc {}kWh".format(scenario, results[scenario]["import"], results[scenario]["soc"]))

        # Clamped: 5kWh of car (fills after 1h) + 1kWh house from grid while the hold is active,
        # then the hold releases and the battery serves the second hour of house load
        if abs(results["clamped"]["import"] - 6.0) > 0.25:
            print("ERROR: clamped import should be ~6.0kWh, got {}".format(results["clamped"]["import"]))
            failed = True
        if abs(results["clamped"]["soc"] - 9.0) > 0.25:
            print("ERROR: clamped final soc should be ~9.0kWh (hold released after the modelled fill), got {}".format(results["clamped"]["soc"]))
            failed = True

        # Uncapped: the full 10kWh of dispatch is delivered and the hold survives the whole slot
        if abs(results["uncapped"]["import"] - 12.0) > 0.25:
            print("ERROR: uncapped import should be ~12.0kWh (full slot energy + house load), got {}".format(results["uncapped"]["import"]))
            failed = True
        if abs(results["uncapped"]["soc"] - 10.0) > 0.25:
            print("ERROR: uncapped final soc should be ~10.0kWh (discharge hold kept all slot), got {}".format(results["uncapped"]["soc"]))
            failed = True

    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_update_car_manual_soc_cap_test(testname, my_predbat):
    """
    Issue #4967: with the prediction's fill clamp inert the modelled car SoC can run past the real
    charge limit, so update_car_manual_soc() must cap the value it writes back to the manual car
    SoC tracker at the real per-car limit (a value below the limit passes through unchanged).
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in ["num_cars", "car_charging_manual_soc", "car_charging_soc", "car_charging_soc_next", "car_charging_limit"]}
    saved_manual_soc_switch = my_predbat.get_arg("car_charging_manual_soc", default=False)
    saved_soc_kwh = my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0)

    try:
        my_predbat.num_cars = 1
        my_predbat.car_charging_manual_soc = [True]
        # The car_charging_manual_soc_kwh config item is enable-gated on the car_charging_manual_soc
        # switch - turn it on so expose_config()/get_arg() actually store and return the value
        my_predbat.expose_config("car_charging_manual_soc", True)
        my_predbat.car_charging_soc = [4.0]
        my_predbat.car_charging_limit = [5.0]

        my_predbat.car_charging_soc_next = [12.0]  # model overshot the real limit
        my_predbat.update_car_manual_soc()
        written = my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0)
        if abs(written - 5.0) > 0.001:
            print("ERROR: manual SoC write-back should be capped at the real limit 5.0, got {}".format(written))
            failed = True

        my_predbat.car_charging_soc_next = [3.0]  # below the limit passes through
        my_predbat.update_car_manual_soc()
        written = my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0)
        if abs(written - 3.0) > 0.001:
            print("ERROR: manual SoC write-back below the limit should be unchanged at 3.0, got {}".format(written))
            failed = True
    finally:
        my_predbat.expose_config("car_charging_manual_soc_kwh", saved_soc_kwh)
        my_predbat.expose_config("car_charging_manual_soc", saved_manual_soc_switch)
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)

    if failed:
        print("Test: {} FAILED".format(testname))
    else:
        print("Test: {} PASSED".format(testname))

    return failed


def run_multi_car_iog_tests(my_predbat):
    """
    Run all multi-car IOG tests
    """
    failed = False
    failed |= run_multi_car_iog_test("multi_car_iog_basic", my_predbat)
    failed |= run_multi_car_iog_load_slots_test("multi_car_iog_load_slots_regression", my_predbat)
    failed |= run_multi_car_iog_unplugged_car_test("multi_car_iog_unplugged_car_3592", my_predbat)
    failed |= run_multi_car_shared_charger_exclusive_test("multi_car_shared_charger_exclusive_4305", my_predbat)
    failed |= run_multi_car_iog_adhoc_dispatch_test("multi_car_iog_adhoc_dispatch_car_charging_now", my_predbat)
    failed |= run_iog_model_limit_fetch_test("multi_car_iog_model_limit_fetch_4967", my_predbat)
    failed |= run_iog_consider_full_predict_test("multi_car_iog_consider_full_predict_4967", my_predbat)
    failed |= run_update_car_manual_soc_cap_test("multi_car_iog_manual_soc_cap_4967", my_predbat)
    return failed
