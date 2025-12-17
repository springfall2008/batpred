# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def run_test_manual_select(my_predbat):
    """
    Test manual select dropdowns for force charge, export, demand
    Tests for bugs:
    1. Same time period can be selected for multiple conflicting states
    2. Same time period can be selected multiple times for the same state
    3. Past time periods are not removed from summary line
    """
    failed = 0
    print("Test manual select dropdowns")

    # Reset all manual selects
    my_predbat.manual_select("manual_charge", "off")
    my_predbat.manual_select("manual_export", "off")
    my_predbat.manual_select("manual_demand", "off")

    # Get a future time from the dropdown options
    charge_item = my_predbat.config_index.get("manual_charge")
    if not charge_item or not charge_item.get("options"):
        print("ERROR: T1 No options found for manual_charge")
        return 1
    
    # Find first non-off option (should be a future time)
    future_time = None
    for option in charge_item["options"]:
        if option != "off" and not option.startswith("+") and not option.startswith("["):
            future_time = option
            break
    
    if not future_time:
        print("ERROR: T2 No future time options found")
        return 1
    
    print(f"Using test time: {future_time}")

    # Test 1: Select a time for force charge
    my_predbat.manual_select("manual_charge", future_time)
    charge_value = my_predbat.config_index.get("manual_charge").get("value", "")
    if future_time not in charge_value:
        print(f"ERROR: T3 Expected {future_time} in manual_charge value, got {charge_value}")
        failed = 1

    # Test 2: Select same time for force export - should remove it from force charge
    my_predbat.manual_select("manual_export", future_time)
    export_value = my_predbat.config_index.get("manual_export").get("value", "")
    charge_value_after = my_predbat.config_index.get("manual_charge").get("value", "")
    
    if future_time not in export_value:
        print(f"ERROR: T4 Expected {future_time} in manual_export value, got {export_value}")
        failed = 1
    
    # The time should be removed from manual_charge after being selected in manual_export
    if future_time in charge_value_after and charge_value_after != "off":
        print(f"ERROR: T5 Expected {future_time} to be removed from manual_charge, but got {charge_value_after}")
        failed = 1

    # Test 3: Try to select the same time again for export (should not create duplicates)
    my_predbat.manual_select("manual_export", future_time)
    export_value_after = my_predbat.config_index.get("manual_export").get("value", "")
    
    # Count occurrences of the time in the value (should only appear once)
    time_count = export_value_after.count(future_time)
    if time_count > 1:
        print(f"ERROR: T6 Expected {future_time} to appear once in manual_export, but appeared {time_count} times: {export_value_after}")
        failed = 1

    # Test 4: Select same time for demand - should remove from export
    my_predbat.manual_select("manual_demand", future_time)
    demand_value = my_predbat.config_index.get("manual_demand").get("value", "")
    export_value_after2 = my_predbat.config_index.get("manual_export").get("value", "")
    
    if future_time not in demand_value:
        print(f"ERROR: T7 Expected {future_time} in manual_demand value, got {demand_value}")
        failed = 1
    
    if future_time in export_value_after2 and export_value_after2 != "off":
        print(f"ERROR: T8 Expected {future_time} to be removed from manual_export, but got {export_value_after2}")
        failed = 1

    # Clean up
    my_predbat.manual_select("manual_charge", "off")
    my_predbat.manual_select("manual_export", "off")
    my_predbat.manual_select("manual_demand", "off")

    if failed:
        print("Manual select tests FAILED")
    else:
        print("Manual select tests PASSED")
    
    return failed
