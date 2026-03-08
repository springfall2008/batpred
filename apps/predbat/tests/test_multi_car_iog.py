# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import timedelta


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
    # Ensure minutes_now is before the IOG slots so they are not filtered as past events.
    # dynamic_load_car sets minutes_now=720 which would put slots at ~60-120 min in the "past".
    my_predbat.minutes_now = 0

    # apps.yaml config args needed by fetch_sensor_data_cars
    my_predbat.args["car_charging_loss"] = 0.0  # loss = 1 - 0.0 = 1.0
    my_predbat.args["car_charging_soc"] = [50.0, 50.0]  # 50% SoC for both cars

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


def run_multi_car_iog_tests(my_predbat):
    """
    Run all multi-car IOG tests
    """
    failed = False
    failed |= run_multi_car_iog_test("multi_car_iog_basic", my_predbat)
    failed |= run_multi_car_iog_load_slots_test("multi_car_iog_load_slots_regression", my_predbat)
    return failed
