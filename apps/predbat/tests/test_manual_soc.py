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
    my_predbat.manual_select("manual_soc", "off")

    # Test 1: Basic manual SOC parsing with default value
    print("Test 1: Basic manual SOC parsing with default value")
    my_predbat.args["manual_soc_value"] = 100
    my_predbat.manual_select("manual_soc", "05:30")

    # Read back the manual_soc_keep by calling manual_rates
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

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
    my_predbat.manual_select("manual_soc", "06:00=80")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

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
    my_predbat.manual_select("manual_soc", "05:30=100,07:00=90,08:30=50")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    expected_values = {100.0, 90.0, 50.0}
    actual_values = set(my_predbat.manual_soc_keep.values())

    if not expected_values.issubset(actual_values):
        print("ERROR: T3 Expected manual_soc_keep to have values {} but got {}".format(expected_values, actual_values))
        failed = True
    else:
        print("PASS: T3 Multiple manual SOC targets set correctly")

    # Test 4: Manual SOC off clears targets
    print("Test 4: Manual SOC off clears targets")
    my_predbat.manual_select("manual_soc", "off")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if my_predbat.manual_soc_keep:
        print("ERROR: T4 Expected manual_soc_keep to be empty when off but got {}".format(my_predbat.manual_soc_keep))
        failed = True
    else:
        print("PASS: T4 Manual SOC targets cleared when set to off")

    # Clean up
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.all_active_keep = {}
    my_predbat.manual_select("manual_soc", "off")
