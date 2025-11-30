# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import reset_inverter


def test_dynamic_load_car_slot_cancellation(my_predbat):
    """
    Test the dynamic_load function to verify that car charging slots are cancelled
    when load_last_period is low and within the threshold
    """
    print("*** Running test: Dynamic load car slot cancellation")
    failed = False

    # Setup test parameters
    reset_inverter(my_predbat)
    my_predbat.num_cars = 2
    my_predbat.minutes_now = 12 * 60  # 12:00 PM
    my_predbat.battery_rate_max_discharge = 5.0 / 60.0  # 5kW converted to kW per minute
    my_predbat.car_charging_threshold = 3.0  # 3kW
    my_predbat.metric_dynamic_load_adjust = True
    my_predbat.load_last_status = "baseline"  # Initialize status
    my_predbat.load_last_car_slot = False

    # Test 1: High load - should set load_last_status to "high" but not cancel slots
    print("Test 1: High load case")
    my_predbat.load_last_period = 6.0  # 6kW - higher than battery_rate_max_discharge * MINUTE_WATT / 1000

    # Create car charging slots that overlap with current time period
    my_predbat.car_charging_slots = [[], [], [], []]
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now + 5, "end": my_predbat.minutes_now + 40, "kwh": 8.0}]

    # Store original slot data for comparison
    original_slot_0_kwh = my_predbat.car_charging_slots[0][0]["kwh"]
    original_slot_1_kwh = my_predbat.car_charging_slots[1][0]["kwh"]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify high load status
    if my_predbat.load_last_status != "high":
        print(f"ERROR: Expected load_last_status to be 'high', got '{my_predbat.load_last_status}'")
        failed = True

    # Verify slots were NOT cancelled (high load doesn't cancel slots)
    if my_predbat.car_charging_slots[0][0]["kwh"] != original_slot_0_kwh:
        print(f"ERROR: Car slot 0 kwh should not have changed, was {original_slot_0_kwh}, now {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if my_predbat.car_charging_slots[1][0]["kwh"] != original_slot_1_kwh:
        print(f"ERROR: Car slot 1 kwh should not have changed, was {original_slot_1_kwh}, now {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 2: Low load - should cancel car slots that overlap with current time
    print("Test 2: Low load case")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # 2kW - low load (< battery_rate_max_discharge * 0.9 * MINUTE_WATT / 1000 and < car_charging_threshold * 0.9)
    my_predbat.load_last_car_slot = True

    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 45, "kwh": 8.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify low load status
    if my_predbat.load_last_status != "low":
        print(f"ERROR: Expected load_last_status to be 'low', got '{my_predbat.load_last_status}'")
        failed = True

    # Verify that car slot 0 was cancelled (overlaps with current time)
    if my_predbat.car_charging_slots[0][0]["kwh"] != 0:
        print(f"ERROR: Car slot 0 should have been cancelled (kwh=0), but kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    # Verify that car slot 1 was also cancelled (overlaps with current 30-minute period)
    if my_predbat.car_charging_slots[1][0]["kwh"] != 0:
        print(f"ERROR: Car slot 1 should have been cancelled (kwh=0), but kwh = {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 3: Low load but slots don't overlap with current time - should not cancel
    print("Test 3.1: Low load with non-overlapping slots, first time car starts")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load
    my_predbat.load_last_car_slot = False

    # Create slots that don't overlap with current time period
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - 5, "end": my_predbat.minutes_now + 90, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now - 60, "end": my_predbat.minutes_now - 30, "kwh": 8.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0:
        print(f"ERROR: Car slot 0 should not have been cancelled, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if my_predbat.car_charging_slots[1][0]["kwh"] != 8.0:
        print(f"ERROR: Car slot 1 should not have been cancelled, kwh = {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 3: Low load but slots don't overlap with current time - should not cancel
    print("Test 3.2: Low load with non-overlapping slots")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load

    # Create slots that don't overlap with current time period
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - 5, "end": my_predbat.minutes_now + 90, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now - 60, "end": my_predbat.minutes_now - 30, "kwh": 8.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    if my_predbat.car_charging_slots[0][0]["kwh"] != 0:
        print(f"ERROR: Car slot 0 should have been cancelled, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if my_predbat.car_charging_slots[1][0]["kwh"] != 8.0:
        print(f"ERROR: Car slot 1 should not have been cancelled, kwh = {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 4: Just after midnight (minutes_now <= 5) - should not cancel even with low load
    print("Test 4: Low load just after midnight")
    my_predbat.minutes_now = 3  # 3 minutes after midnight
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load
    my_predbat.load_last_car_slot = False

    # Create slots that overlap with current time
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify slots were NOT cancelled (due to midnight exclusion)
    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0:
        print(f"ERROR: Car slot should not have been cancelled just after midnight, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    # Test 5: Metric dynamic load adjust disabled - should not cancel slots
    print("Test 5: Dynamic load adjust disabled")
    my_predbat.minutes_now = 12 * 60  # Reset to noon
    my_predbat.metric_dynamic_load_adjust = False  # Disable dynamic load adjust
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load

    # Create slots that overlap with current time
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - 5, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify slots were NOT cancelled (feature disabled)
    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0:
        print(f"ERROR: Car slot should not have been cancelled when feature disabled, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if not failed:
        print("*** Dynamic load car slot cancellation test PASSED")
    else:
        print("*** Dynamic load car slot cancellation test FAILED")

    return failed
