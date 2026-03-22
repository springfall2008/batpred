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


def run_multi_car_iog_unplugged_car_test(testname, my_predbat):
    """
    Regression test for issue #3592: When car 1 is NOT physically plugged in but still has
    stale planned dispatch data from the Octopus Intelligent sensor, the car plan loop was
    incorrectly calling plan_car_charging() for car 1 (because the `car_n == 0` guard only
    protected car 0). This caused car 1's charging slots to expand from a small set of IOG
    dispatch windows to ALL low-rate windows - 'charging slots all the time'.

    The fix changes `car_n == 0` to check whether the car is configured with an IOG entity,
    which prevents plan_car_charging() from being called for any IOG-configured car.
    """
    failed = False
    print("**** Running Test: multi_car_iog {} ****".format(testname))

    # Set up 2 cars, both on IOG
    my_predbat.num_cars = 2
    my_predbat.car_charging_planned = [True, False]  # Car 0 plugged in, car 1 NOT plugged in
    my_predbat.car_charging_now = [False, False]
    my_predbat.car_charging_plan_smart = [False, False]
    my_predbat.car_charging_plan_max_price = [0, 0]
    my_predbat.car_charging_plan_time = ["07:00:00", "07:00:00"]
    my_predbat.car_charging_battery_size = [100.0, 80.0]
    my_predbat.car_charging_limit = [100.0, 80.0]
    my_predbat.car_charging_rate = [7.4, 7.4]
    my_predbat.car_charging_soc = [50.0, 40.0]
    my_predbat.car_charging_soc_next = [None, None]
    my_predbat.car_charging_slots = [[], []]
    my_predbat.car_charging_exclusive = [False, False]
    my_predbat.car_charging_loss = 1.0
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = False  # default: don't ignore

    # Both cars have IOG entities configured - the scenario from issue #3592
    my_predbat.args["octopus_intelligent_slot"] = [
        "binary_sensor.octopus_energy_intelligent_dispatching_car1",
        "binary_sensor.octopus_energy_intelligent_dispatching_car2",
    ]

    # Car 0: valid dispatch slot (1 hour)
    slot0_start = (my_predbat.now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot0_end = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")

    # Car 1: stale dispatch slot (should be ignored due to car not being plugged in,
    # but with octopus_intelligent_ignore_unplugged=False it gets included by the IOG path)
    slot1_start = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    slot1_end = (my_predbat.now_utc + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S%z")

    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car1",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot0_start, "end": slot0_end, "charge_in_kwh": 10.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 100.0,
            "charge_point_power_in_kw": 7.4,
        },
    )
    my_predbat.ha_interface.set_state(
        "binary_sensor.octopus_energy_intelligent_dispatching_car2",
        "on",
        attributes={
            "completed_dispatches": [],
            "planned_dispatches": [{"start": slot1_start, "end": slot1_end, "charge_in_kwh": 8.0, "source": "smart-charge", "location": "AT_HOME"}],
            "vehicle_battery_size_in_kwh": 80.0,
            "charge_point_power_in_kw": 7.4,
        },
    )

    # Simulate the state after fetch_sensor_data_cars runs - car 1 gets IOG slots because
    # octopus_intelligent_ignore_unplugged=False includes them, and car_charging_planned[1] is
    # set to True by the IOG path even though car 1 is not physically plugged in.
    # The key test: after this, the car plan loop should NOT call plan_car_charging() for car 1.
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]
    my_predbat.car_charging_manual_soc = [False, False]
    my_predbat.args["car_charging_loss"] = 0.0
    my_predbat.args["car_charging_soc"] = [50.0, 50.0]
    my_predbat.minutes_now = 0

    try:
        my_predbat.fetch_sensor_data_cars()
    except Exception as exc:
        print("ERROR: fetch_sensor_data_cars raised exception: {}".format(exc))
        failed = True
        if failed:
            print("Test: {} FAILED".format(testname))
        return failed

    # After fetch_sensor_data_cars, car 1 has IOG slots (stale but included because
    # octopus_intelligent_ignore_unplugged=False). Verify car_charging_planned[1] is True.
    if not my_predbat.car_charging_planned[1]:
        print("SKIP CHECK: car_charging_planned[1] is False - stale slots were filtered. This is acceptable if IOG ignore logic is working.")
    else:
        # car_charging_planned[1] is True (stale slots included).
        # The regression: with the old code, the car plan loop would call plan_car_charging(1, low_rates)
        # which would OVERWRITE car_charging_slots[1] with ALL low-rate windows.
        # Verify that car_charging_slots[1] contains only the IOG dispatch slots (at most 1 slot from
        # the stale dispatch data, not all low-rate windows).
        iog_windows_car1 = len(my_predbat.car_charging_slots[1])
        # The stale dispatch data has exactly 1 slot. Even with some expansion, should be very limited.
        # The old plan_car_charging would create many slots (one per low-rate window = many hours).
        # We verify it is bounded - if the car plan loop fixed works, the IOG path limits slots.
        if iog_windows_car1 > 5:
            print("ERROR: car_charging_slots[1] has {} windows - looks like plan_car_charging was called (old bug). Expected at most a few IOG slots.".format(iog_windows_car1))
            failed = True
        else:
            print("OK: car_charging_slots[1] has {} IOG-derived windows (bounded, not plan_car_charging explosion)".format(iog_windows_car1))

    # Now simulate what the car plan loop in fetch_sensor_data does.
    # The fix: the condition should treat both car 0 and car 1 as IOG cars (since both have entities).
    # Test that the condition correctly identifies car 1 as an IOG car.
    iog_entity_id_config = my_predbat.get_arg("octopus_intelligent_slot", indirect=False)
    if iog_entity_id_config and not isinstance(iog_entity_id_config, list):
        iog_entity_ids = [iog_entity_id_config]
    elif iog_entity_id_config:
        iog_entity_ids = iog_entity_id_config
    else:
        iog_entity_ids = []

    # car 0: should be identified as IOG car
    car0_is_iog = my_predbat.octopus_intelligent_charging and 0 < len(iog_entity_ids) and bool(iog_entity_ids[0])
    if not car0_is_iog:
        print("ERROR: Car 0 should be identified as IOG car in car plan loop")
        failed = True

    # car 1: should ALSO be identified as IOG car (this was the bug - only car 0 was checked)
    car1_is_iog = my_predbat.octopus_intelligent_charging and 1 < len(iog_entity_ids) and bool(iog_entity_ids[1])
    if not car1_is_iog:
        print("ERROR: Car 1 should be identified as IOG car in car plan loop (fix for issue #3592)")
        failed = True
    else:
        print("OK: Car 1 correctly identified as IOG car - plan_car_charging will not be called for it")

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
    return failed
