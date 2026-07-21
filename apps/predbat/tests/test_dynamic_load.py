# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from const import PREDICT_STEP
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
    my_predbat.load_last_status = "baseline"  # Initialise status
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


def test_dynamic_load_high_load_baseline(my_predbat):
    """
    Test the dynamic_load high-load branch which raises the near-term load forecast
    (dynamic_load_baseline) to match an observed load spike, e.g. a car charging
    outside any known/planned (including Octopus dispatch) slot.
    """
    print("*** Running test: Dynamic load high load baseline")
    failed = False

    reset_inverter(my_predbat)
    my_predbat.num_cars = 1
    my_predbat.minutes_now = 12 * 60  # 12:00 PM, on a plan_interval_minutes boundary
    my_predbat.battery_rate_max_discharge = 5.0 / 60.0  # 5kW converted to kW per minute
    my_predbat.car_charging_threshold = 3.0
    my_predbat.metric_dynamic_load_adjust = True
    my_predbat.load_last_status = "baseline"
    my_predbat.load_last_car_slot = False
    my_predbat.load_last_period = 6.0  # 6kW - above the 5kW battery threshold, so "high"

    minutes_end_slot = my_predbat.minutes_now + my_predbat.plan_interval_minutes
    interval_minutes = list(range(my_predbat.minutes_now, minutes_end_slot, PREDICT_STEP))
    expected_value = my_predbat.load_last_period / 60 * PREDICT_STEP  # 0.5kWh per 5-minute step

    # Test 1: Car charging outside all known/Octopus slots - Predbat can't attribute the spike
    # to a recognised car window, so the whole load should be folded into the near-term baseline.
    print("Test 1: High load, car charging outside all known/Octopus slots")
    my_predbat.car_charging_slots = [[], [], [], []]
    my_predbat.car_energy_reported_load = True

    my_predbat.dynamic_load()

    if set(my_predbat.dynamic_load_baseline.keys()) != set(interval_minutes):
        print(f"ERROR: Expected baseline keys {interval_minutes}, got {sorted(my_predbat.dynamic_load_baseline.keys())}")
        failed = True
    for minute_absolute in interval_minutes:
        value = my_predbat.dynamic_load_baseline.get(minute_absolute)
        if value is None or abs(value - expected_value) > 0.001:
            print(f"ERROR: Expected baseline at {minute_absolute} to be {expected_value}, got {value}")
            failed = True

    # Test 2: Car recognised in a slot that fully covers the current interval and the load sensor
    # is known to include car energy - the car's own consumption absorbs the spike so no baseline
    # floor is needed.
    print("Test 2: High load, car recognised in a fully-covering slot")
    my_predbat.load_last_status = "baseline"
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": minutes_end_slot, "kwh": 3.0}]
    my_predbat.car_energy_reported_load = True

    my_predbat.dynamic_load()

    if my_predbat.dynamic_load_baseline:
        print(f"ERROR: Expected no baseline entries when the car slot fully covers the load spike, got {my_predbat.dynamic_load_baseline}")
        failed = True

    # Test 3: Same recognised car slot, but car_energy_reported_load is off (the default) -
    # Predbat has no way to know the load sensor includes the car's energy, so it must fall back
    # to folding in the whole spike, same as when the car is outside any slot.
    print("Test 3: High load, car recognised in slot but car_energy_reported_load is off")
    my_predbat.load_last_status = "baseline"
    my_predbat.car_energy_reported_load = False

    my_predbat.dynamic_load()

    if set(my_predbat.dynamic_load_baseline.keys()) != set(interval_minutes):
        print(f"ERROR: Expected baseline keys {interval_minutes} when car_energy_reported_load is off, got {sorted(my_predbat.dynamic_load_baseline.keys())}")
        failed = True
    for minute_absolute in interval_minutes:
        value = my_predbat.dynamic_load_baseline.get(minute_absolute)
        if value is None or abs(value - expected_value) > 0.001:
            print(f"ERROR: Expected baseline at {minute_absolute} to be {expected_value} when car_energy_reported_load is off, got {value}")
            failed = True

    # Test 4: Sustained high load over two consecutive checks in a row extends the baseline into
    # the following slot too, so the plan doesn't lag a whole cycle behind at the slot boundary.
    print("Test 4: High load sustained over two consecutive checks extends into the next slot")
    my_predbat.car_charging_slots = [[], [], [], []]
    my_predbat.car_energy_reported_load = True
    my_predbat.load_last_status = "baseline"  # First check - establishes the initial "high" reading

    my_predbat.dynamic_load()

    if set(my_predbat.dynamic_load_baseline.keys()) != set(interval_minutes):
        print(f"ERROR: First high reading should only cover the current slot, expected {interval_minutes}, got {sorted(my_predbat.dynamic_load_baseline.keys())}")
        failed = True

    my_predbat.dynamic_load()  # Second consecutive high check - should now extend into the next slot

    extended_interval_minutes = list(range(my_predbat.minutes_now, minutes_end_slot + my_predbat.plan_interval_minutes, PREDICT_STEP))
    if set(my_predbat.dynamic_load_baseline.keys()) != set(extended_interval_minutes):
        print(f"ERROR: Expected baseline keys {extended_interval_minutes} after two consecutive high checks, got {sorted(my_predbat.dynamic_load_baseline.keys())}")
        failed = True
    for minute_absolute in extended_interval_minutes:
        value = my_predbat.dynamic_load_baseline.get(minute_absolute)
        if value is None or abs(value - expected_value) > 0.001:
            print(f"ERROR: Expected baseline at {minute_absolute} to be {expected_value} after two consecutive high checks, got {value}")
            failed = True

    if not failed:
        print("*** Dynamic load high load baseline test PASSED")
    else:
        print("*** Dynamic load high load baseline test FAILED")

    return failed
