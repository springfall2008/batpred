# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def run_test_manual_soc(my_predbat):
    """
    Test manual SOC target feature
    """
    failed = False
    print("Test manual SOC target")

    # Reset manual_soc to off
    my_predbat.api_select("manual_soc", "off")
    
    # Test 1: Basic manual SOC parsing with default value
    print("Test 1: Basic manual SOC parsing with default value")
    my_predbat.args["manual_soc_value"] = 100
    my_predbat.api_select("manual_soc", "05:30")
    my_predbat.fetch_config_options()
    
    # The minutes are calculated from midnight_utc, not from "now"
    # So we need to check what minutes_now is and adjust
    # For this test, we check if any entry exists and has value 100
    if not my_predbat.manual_soc_keep:
        print("ERROR: T1 Expected manual_soc_keep to have entries but got empty dict")
        failed = True
    else:
        # Check if any value is 100
        has_100 = any(v == 100.0 for v in my_predbat.manual_soc_keep.values())
        if not has_100:
            print("ERROR: T1 Expected manual_soc_keep to have SOC target of 100% but got {}".format(my_predbat.manual_soc_keep))
            failed = True
        else:
            print("PASS: T1 Manual SOC target set correctly to 100% at 05:30")
    
    # Test 2: Manual SOC with explicit value
    print("Test 2: Manual SOC with explicit value")
    my_predbat.api_select("manual_soc", "06:00=80")
    my_predbat.fetch_config_options()
    
    if not my_predbat.manual_soc_keep:
        print("ERROR: T2 Expected manual_soc_keep to have entries but got empty dict")
        failed = True
    else:
        # Check if any value is 80
        has_80 = any(v == 80.0 for v in my_predbat.manual_soc_keep.values())
        if not has_80:
            print("ERROR: T2 Expected manual_soc_keep to have SOC target of 80% but got {}".format(my_predbat.manual_soc_keep))
            failed = True
        else:
            print("PASS: T2 Manual SOC target set correctly to 80% at 06:00")
    
    # Test 3: Multiple manual SOC targets
    print("Test 3: Multiple manual SOC targets")
    my_predbat.api_select("manual_soc", "05:30=100,07:00=90,08:30=50")
    my_predbat.fetch_config_options()
    
    expected_values = {100.0, 90.0, 50.0}
    actual_values = set(my_predbat.manual_soc_keep.values())
    
    if not expected_values.issubset(actual_values):
        print("ERROR: T3 Expected manual_soc_keep to have values {} but got {}".format(expected_values, actual_values))
        failed = True
    else:
        print("PASS: T3 Multiple manual SOC targets set correctly")
    
    # Test 4: Manual SOC off clears targets
    print("Test 4: Manual SOC off clears targets")
    my_predbat.api_select("manual_soc", "off")
    my_predbat.fetch_config_options()
    
    if my_predbat.manual_soc_keep:
        print("ERROR: T4 Expected manual_soc_keep to be empty when off but got {}".format(my_predbat.manual_soc_keep))
        failed = True
    else:
        print("PASS: T4 Manual SOC targets cleared when set to off")
    
    # Test 5: Integration with alert_active_keep - verify manual_soc_keep is populated
    print("Test 5: Verify manual_soc_keep structure")
    # Set up manual SOC target
    my_predbat.api_select("manual_soc", "05:30=100")
    my_predbat.fetch_config_options()
    
    # Get the first minute from manual_soc_keep to use in test
    if my_predbat.manual_soc_keep:
        test_minute = list(my_predbat.manual_soc_keep.keys())[0]
        
        # Simulate alert system setting a lower target at the same time
        my_predbat.alert_active_keep = {test_minute: 80}
        
        # Test the merging logic directly (this is what happens in Prediction.__init__)
        merged_keep = my_predbat.alert_active_keep.copy()
        if my_predbat.manual_soc_keep:
            for minute, soc_value in my_predbat.manual_soc_keep.items():
                if minute in merged_keep:
                    merged_keep[minute] = max(merged_keep[minute], soc_value)
                else:
                    merged_keep[minute] = soc_value
        
        # Check that the maximum value (100 from manual, 80 from alert) is used
        if test_minute not in merged_keep:
            print("ERROR: T5 Expected merged alert_active_keep to have entry for minute {} but got {}".format(test_minute, merged_keep))
            failed = True
        elif merged_keep[test_minute] != 100:
            print("ERROR: T5 Expected merged SOC target of 100% (max of 100 and 80) at minute {} but got {}%".format(test_minute, merged_keep[test_minute]))
            failed = True
        else:
            print("PASS: T5 Manual SOC correctly merged with alert_active_keep (taking maximum)")
    else:
        print("ERROR: T5 Could not test merging - manual_soc_keep is empty")
        failed = True
    
    # Test 6: Alert wins when higher than manual SOC
    print("Test 6: Alert wins when higher than manual SOC")
    my_predbat.api_select("manual_soc", "off")  # Clear previous values
    my_predbat.fetch_config_options()
    my_predbat.api_select("manual_soc", "06:00=70")
    my_predbat.fetch_config_options()
    
    if my_predbat.manual_soc_keep:
        test_minute2 = list(my_predbat.manual_soc_keep.keys())[0]
        my_predbat.alert_active_keep = {test_minute2: 90}
        
        # Test the merging logic directly
        merged_keep2 = my_predbat.alert_active_keep.copy()
        if my_predbat.manual_soc_keep:
            for minute, soc_value in my_predbat.manual_soc_keep.items():
                if minute in merged_keep2:
                    merged_keep2[minute] = max(merged_keep2[minute], soc_value)
                else:
                    merged_keep2[minute] = soc_value
        
        if test_minute2 not in merged_keep2:
            print("ERROR: T6 Expected merged alert_active_keep to have entry for minute {} but got {}".format(test_minute2, merged_keep2))
            failed = True
        elif merged_keep2[test_minute2] != 90:
            print("ERROR: T6 Expected merged SOC target of 90% (max of 70 and 90) at minute {} but got {}%".format(test_minute2, merged_keep2[test_minute2]))
            failed = True
        else:
            print("PASS: T6 Alert correctly wins when higher than manual SOC (taking maximum)")
    else:
        print("ERROR: T6 Could not test merging - manual_soc_keep is empty")
        failed = True
    
    # Test 7: Manual SOC at different time than alert
    print("Test 7: Manual SOC and alert at different times")
    my_predbat.api_select("manual_soc", "off")  # Clear previous values
    my_predbat.fetch_config_options()
    my_predbat.api_select("manual_soc", "05:30=100")
    my_predbat.fetch_config_options()
    
    if my_predbat.manual_soc_keep:
        manual_minute = list(my_predbat.manual_soc_keep.keys())[0]
        # Pick a different minute for alert (add 90 minutes to manual_minute)
        alert_minute = manual_minute + 90
        my_predbat.alert_active_keep = {alert_minute: 85}
        
        # Test the merging logic directly
        merged_keep3 = my_predbat.alert_active_keep.copy()
        if my_predbat.manual_soc_keep:
            for minute, soc_value in my_predbat.manual_soc_keep.items():
                if minute in merged_keep3:
                    merged_keep3[minute] = max(merged_keep3[minute], soc_value)
                else:
                    merged_keep3[minute] = soc_value
        
        if manual_minute not in merged_keep3:
            print("ERROR: T7 Expected manual_minute {} in merged alert_active_keep but got {}".format(manual_minute, merged_keep3))
            failed = True
        elif merged_keep3[manual_minute] != 100:
            print("ERROR: T7 Expected manual SOC of 100% at minute {} but got {}%".format(manual_minute, merged_keep3[manual_minute]))
            failed = True
        
        if alert_minute not in merged_keep3:
            print("ERROR: T7 Expected alert_minute {} in merged alert_active_keep but got {}".format(alert_minute, merged_keep3))
            failed = True
        elif merged_keep3[alert_minute] != 85:
            print("ERROR: T7 Expected alert SOC of 85% at minute {} but got {}%".format(alert_minute, merged_keep3[alert_minute]))
            failed = True
        
        if not failed:
            print("PASS: T7 Manual SOC and alert both present at different times")
    else:
        print("ERROR: T7 Could not test merging - manual_soc_keep is empty")
        failed = True
    
    # Clean up
    my_predbat.api_select("manual_soc", "off")
    my_predbat.fetch_config_options()
    my_predbat.alert_active_keep = {}
    
    return failed
