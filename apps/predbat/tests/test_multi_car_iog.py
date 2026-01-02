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

    # Process each car
    for car_n in range(min(len(entity_id_list), my_predbat.num_cars)):
        entity_id = entity_id_list[car_n]
        if not entity_id:
            continue

        completed = my_predbat.get_state_wrapper(entity_id=entity_id, attribute="completed_dispatches") or []
        planned = my_predbat.get_state_wrapper(entity_id=entity_id, attribute="planned_dispatches") or []

        if completed:
            my_predbat.octopus_slots += completed
        if planned:
            my_predbat.octopus_slots += planned


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
    my_predbat.octopus_slots = []

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
    my_predbat.octopus_slots = []
    process_octopus_intelligent_slots(my_predbat)

    if len(my_predbat.octopus_slots) != 1:
        print("ERROR: Expected 1 slot for single car, got {}".format(len(my_predbat.octopus_slots)))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True

    # Test 2: Multi-car config
    print("Test 2: Multi-car config with two cars")
    my_predbat.octopus_slots = []
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

    # Should have slots from both cars merged
    if len(my_predbat.octopus_slots) != 2:
        print("ERROR: Expected 2 slots (one from each car), got {}".format(len(my_predbat.octopus_slots)))
        print("Slots: {}".format(my_predbat.octopus_slots))
        failed = True

    # Test 3: Multi-car config with empty slot
    print("Test 3: Multi-car config with one empty/None slot")
    my_predbat.octopus_slots = []
    my_predbat.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_energy_intelligent_dispatching_car1", None]

    # Simulate the octopus intelligent slot processing
    process_octopus_intelligent_slots(my_predbat)

    # Should have slots from first car only
    if len(my_predbat.octopus_slots) != 1:
        print("ERROR: Expected 1 slot (from first car only), got {}".format(len(my_predbat.octopus_slots)))
        print("Slots: {}".format(my_predbat.octopus_slots))
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
    return failed
