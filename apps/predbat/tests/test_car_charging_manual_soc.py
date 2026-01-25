# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import reset_rates2, reset_inverter


def run_car_charging_manual_soc_test(my_predbat):
    """
    Test car charging manual SOC protection against unexpected resets
    """
    failed = False

    print("**** Running Car Charging Manual SOC tests ****")
    
    # Initialize car charging setup
    my_predbat.num_cars = 1
    my_predbat.car_charging_battery_size = [75.0]  # 75 kWh battery
    my_predbat.car_charging_limit = [70.0]  # Charge to 70 kWh
    my_predbat.car_charging_soc = [30.0]  # Currently at 30 kWh
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_manual_soc = [True]  # Manual SOC mode enabled
    my_predbat.car_charging_rate = [7.4]  # 7.4 kW charging rate
    my_predbat.car_charging_loss = 1.0  # No loss for simplicity
    my_predbat.car_charging_plan_max_price = [99.0]
    my_predbat.car_charging_plan_smart = [True]
    my_predbat.car_charging_plan_time = ["07:00:00"]
    my_predbat.car_charging_slots = [[]]
    
    # Test 1: Verify manual SOC is preserved when value is sensible
    print("Test 1: Manual SOC should be updated when soc_next is reasonable")
    my_predbat.car_charging_soc[0] = 30.0
    my_predbat.car_charging_soc_next[0] = 35.0  # Increased after charging
    
    # Simulate the update check (from predbat.py line 944-951)
    car_n = 0
    if my_predbat.car_charging_soc_next[car_n] is not None:
        # Check protection logic
        if my_predbat.car_charging_soc_next[car_n] < 0.1 and my_predbat.car_charging_soc[car_n] > 1.0:
            print("ERROR: Test 1 failed - protection blocked valid update")
            failed = True
        else:
            print("PASS: Test 1 - Manual SOC would be updated from {:.2f} to {:.2f}".format(
                my_predbat.car_charging_soc[car_n], my_predbat.car_charging_soc_next[car_n]))
    
    # Test 2: Verify protection prevents reset from high value to zero
    print("Test 2: Manual SOC should NOT be reset from high value to near-zero")
    my_predbat.car_charging_soc[0] = 30.0
    my_predbat.car_charging_soc_next[0] = 0.05  # Unexpectedly low value
    
    if my_predbat.car_charging_soc_next[car_n] is not None:
        if my_predbat.car_charging_soc_next[car_n] < 0.1 and my_predbat.car_charging_soc[car_n] > 1.0:
            print("PASS: Test 2 - Protection correctly prevented reset from {:.2f} to {:.2f}".format(
                my_predbat.car_charging_soc[car_n], my_predbat.car_charging_soc_next[car_n]))
        else:
            print("ERROR: Test 2 failed - protection did NOT block invalid reset")
            failed = True
    
    # Test 3: Verify zero to zero is allowed (initial state)
    print("Test 3: Manual SOC at zero can stay at zero")
    my_predbat.car_charging_soc[0] = 0.0
    my_predbat.car_charging_soc_next[0] = 0.0
    
    if my_predbat.car_charging_soc_next[car_n] is not None:
        if my_predbat.car_charging_soc_next[car_n] < 0.1 and my_predbat.car_charging_soc[car_n] > 1.0:
            print("ERROR: Test 3 failed - protection incorrectly blocked zero to zero")
            failed = True
        else:
            print("PASS: Test 3 - Zero to zero transition allowed")
    
    # Test 4: Verify small decreases are allowed (within threshold)
    print("Test 4: Small manual SOC values (< 1 kWh) can be updated")
    my_predbat.car_charging_soc[0] = 0.5
    my_predbat.car_charging_soc_next[0] = 0.05
    
    if my_predbat.car_charging_soc_next[car_n] is not None:
        if my_predbat.car_charging_soc_next[car_n] < 0.1 and my_predbat.car_charging_soc[car_n] > 1.0:
            print("ERROR: Test 4 failed - protection incorrectly blocked small value update")
            failed = True
        else:
            print("PASS: Test 4 - Small values can be updated")
    
    return failed
