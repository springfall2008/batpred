# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import copy

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
    my_predbat.dynamic_load()

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

    # Test 2: Low load - dynamic_load() classifies it but no longer cancels car slots itself; that
    # moved earlier in the cycle, before the rates are built (dynamic_load_car_check(), see
    # test_dynamic_load_car.py), so it can withhold an Intelligent dispatch's cheap rate too
    print("Test 2: Low load case leaves the car slots to dynamic_load_car_check()")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # 2kW - low load (< battery_rate_max_discharge * 0.9 * MINUTE_WATT / 1000 and < car_charging_threshold * 0.85)

    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 45, "kwh": 8.0}]

    # Call dynamic_load
    my_predbat.dynamic_load()

    # Verify low load status
    if my_predbat.load_last_status != "low":
        print(f"ERROR: Expected load_last_status to be 'low', got '{my_predbat.load_last_status}'")
        failed = True

    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0 or my_predbat.car_charging_slots[1][0]["kwh"] != 8.0:
        print(f"ERROR: dynamic_load() should leave car slots alone, got kwh {my_predbat.car_charging_slots[0][0]['kwh']} and {my_predbat.car_charging_slots[1][0]['kwh']}")
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
    my_predbat.dynamic_load()

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
    Test the high-load baseline scenarios without leaking car state into later tests.
    """
    state_fields = (
        "num_cars",
        "minutes_now",
        "battery_rate_max_discharge",
        "car_charging_threshold",
        "car_charging_slots",
        "car_energy_reported_load",
        "car_charging_hold",
        "car_charging_energy",
        "load_last_period",
        "load_last_status",
        "metric_dynamic_load_adjust",
        "dynamic_load_baseline",
    )
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field)) for field in state_fields}
    try:
        return _run_dynamic_load_high_load_baseline(my_predbat)
    finally:
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)


def _run_dynamic_load_high_load_baseline(my_predbat):
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
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - PREDICT_STEP, "end": minutes_end_slot, "kwh": 3.5}]
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

    # Test 5: A measured car-energy increment is removed before a high-load observation is carried
    # across the slot boundary. The residual should represent house load only, not the full car load.
    print("Test 5: Measured car energy is excluded from the next-slot baseline")
    my_predbat.load_last_period = 7.2  # 6kW car plus 1.2kW house load
    my_predbat.load_last_status = "high"  # Treat this as the second consecutive high reading
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_hold = True
    my_predbat.car_charging_energy = {0: 0.5}  # 0.5kWh measured in the latest 5-minute period
    my_predbat.car_charging_slots = [[], [], [], []]

    my_predbat.dynamic_load()

    expected_residual = 0.1  # (7.2kW * 5/60) - 0.5kWh
    next_slot_value = my_predbat.dynamic_load_baseline.get(minutes_end_slot)
    if next_slot_value is None or abs(next_slot_value - expected_residual) > 0.001:
        print(f"ERROR: Expected next-slot baseline to be {expected_residual}, got {next_slot_value}")
        failed = True

    # Test 6: When the car-energy sensor has no current increment, the planned-slot fallback is
    # converted from kW to kWh over the trailing period before it is removed from the measured load.
    print("Test 6: Planned car power fallback uses trailing-period kWh")
    my_predbat.load_last_period = 6.0  # 4.8kW car plus 1.2kW house load
    my_predbat.load_last_status = "high"
    my_predbat.car_charging_energy = {}
    my_predbat.car_charging_slots = [[{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now, "kwh": 2.4}], [], [], []]

    my_predbat.dynamic_load()

    expected_residual = 0.1  # 0.5kWh measured load - 2.4kWh / 30min * 5min = 0.1kWh
    next_slot_value = my_predbat.dynamic_load_baseline.get(minutes_end_slot)
    if next_slot_value is None or abs(next_slot_value - expected_residual) > 0.001:
        print(f"ERROR: Expected fallback next-slot baseline to be {expected_residual}, got {next_slot_value}")
        failed = True

    # Test 7: A positive but lagging sensor increment must not leave the planned car energy in
    # the residual baseline carried into the next slot.
    print("Test 7: Planned car energy covers a lagging sensor reading")
    my_predbat.load_last_period = 7.2  # 6kW car plus 1.2kW house load
    my_predbat.load_last_status = "high"
    my_predbat.car_charging_energy = {0: 0.1}  # Sensor has only caught up with part of the 0.5kWh
    my_predbat.car_charging_slots = [[{"start": my_predbat.minutes_now - PREDICT_STEP, "end": minutes_end_slot, "kwh": 3.5}], [], [], []]

    my_predbat.dynamic_load()

    expected_residual = 0.1  # 0.6kWh measured load - planned 3.5kWh / 35min * 5min = 0.1kWh
    next_slot_value = my_predbat.dynamic_load_baseline.get(minutes_end_slot)
    if next_slot_value is None or abs(next_slot_value - expected_residual) > 0.001:
        print(f"ERROR: Expected lagging-sensor next-slot baseline to be {expected_residual}, got {next_slot_value}")
        failed = True

    if not failed:
        print("*** Dynamic load high load baseline test PASSED")
    else:
        print("*** Dynamic load high load baseline test FAILED")

    return failed


LIVE_LOAD_POWER = "sensor.load_power_test_live"


def _set_live_load(my_predbat, state):
    """
    Point load_power at one entity holding state (in watts), or remove load_power when state is None.
    """
    my_predbat.ha_interface.dummy_items.pop(LIVE_LOAD_POWER, None)
    if state is None:
        my_predbat.args.pop("load_power", None)
        return
    my_predbat.args["load_power"] = LIVE_LOAD_POWER
    my_predbat.ha_interface.dummy_items[LIVE_LOAD_POWER] = state


def test_dynamic_load_high_live_confirm(my_predbat):
    """
    Test that a high load_last_period only counts as "high" once the live load_power confirms it.
    """
    state_fields = (
        "num_cars",
        "minutes_now",
        "battery_rate_max_discharge",
        "car_charging_threshold",
        "car_charging_slots",
        "car_energy_reported_load",
        "load_last_period",
        "load_last_status",
        "metric_dynamic_load_adjust",
        "dynamic_load_baseline",
    )
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field)) for field in state_fields}
    saved_load_power = my_predbat.args.get("load_power", None)
    try:
        return _run_dynamic_load_high_live_confirm(my_predbat)
    finally:
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
        my_predbat.ha_interface.dummy_items.pop(LIVE_LOAD_POWER, None)
        if saved_load_power is None:
            my_predbat.args.pop("load_power", None)
        else:
            my_predbat.args["load_power"] = saved_load_power


def _run_dynamic_load_high_live_confirm(my_predbat):
    """
    A Solis load counter moves in whole kWh, so one step inside the last five minutes reads as a load spike
    of up to 12kW while the house is using under 1kW. Unconfirmed by the live load_power it must not raise the
    load forecast or force a re-plan, which on 27 Oct 2026 cut a live export window short twice.
    """
    print("*** Running test: Dynamic load high load confirmed by live load")
    failed = False

    reset_inverter(my_predbat)
    my_predbat.num_cars = 1
    my_predbat.minutes_now = 18 * 60 + 45
    my_predbat.battery_rate_max_discharge = 5.12 / 60.0  # 5.12kW, the threshold for "high"
    my_predbat.car_charging_threshold = 6.0
    my_predbat.car_charging_slots = [[], [], [], []]
    my_predbat.car_energy_reported_load = False
    my_predbat.metric_dynamic_load_adjust = True

    cases = [
        # (description, previous status, counter kW, live load_power, expected status, expect a baseline, expect a re-plan)
        ("from low: counter step 9.6kW, live load 692W - not confirmed, stays low", "low", 9.6, 692, "low", False, False),
        ("from baseline: counter step 9.6kW, live load 692W - not confirmed, stays baseline", "baseline", 9.6, 692, "baseline", False, False),
        ("from high: counter 9.6kW, live load 692W - not confirmed, drops the raised load", "high", 9.6, 692, "baseline", False, True),
        ("from low: counter 9.6kW, live load 7000W - confirmed", "low", 9.6, 7000, "high", True, True),
        ("from low: counter 9.6kW, live load 4700W - within 90% of the threshold, confirmed", "low", 9.6, 4700, "high", True, True),
        ("from low: counter 9.6kW, live load unavailable - counter stands as before", "low", 9.6, "unavailable", "high", True, True),
        ("from low: counter 9.6kW, load_power not configured - counter alone as before", "low", 9.6, None, "high", True, True),
        ("from baseline: counter 0.7kW, live load 7000W - only a high counter is confirmed", "baseline", 0.7, 7000, "low", False, True),
    ]
    for test_n, (description, previous, counter_kw, live_state, expected_status, expect_baseline, expect_replan) in enumerate(cases, start=1):
        print("Test {}: {}".format(test_n, description))
        _set_live_load(my_predbat, live_state)
        my_predbat.load_last_status = previous
        my_predbat.load_last_period = counter_kw

        replan = my_predbat.dynamic_load()

        if my_predbat.load_last_status != expected_status:
            print("ERROR: Expected load_last_status '{}', got '{}'".format(expected_status, my_predbat.load_last_status))
            failed = True
        if bool(my_predbat.dynamic_load_baseline) != expect_baseline:
            print("ERROR: Expected baseline {}, got {}".format("entries" if expect_baseline else "none", my_predbat.dynamic_load_baseline))
            failed = True
        if replan != expect_replan:
            print("ERROR: Expected dynamic_load() to return {}, got {}".format(expect_replan, replan))
            failed = True

    # The car check classifies without a previous status, so its view of an unconfirmed high is unchanged: not "low"
    print("Test {}: car check view of an unconfirmed high is not low".format(len(cases) + 1))
    _set_live_load(my_predbat, 692)
    my_predbat.load_last_period = 9.6
    if my_predbat.dynamic_load_classify() != "baseline":
        print("ERROR: Expected dynamic_load_classify() with no previous status to return 'baseline', got '{}'".format(my_predbat.dynamic_load_classify()))
        failed = True

    if not failed:
        print("*** Dynamic load high load confirmed by live load test PASSED")
    else:
        print("*** Dynamic load high load confirmed by live load test FAILED")

    return failed
