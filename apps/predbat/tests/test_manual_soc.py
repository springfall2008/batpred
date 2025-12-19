# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone, timedelta

def run_test_manual_soc(my_predbat):
    """
    Test manual SOC target feature
    """
    failed = False
    print("Test manual SOC target")


    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.now_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0

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

    # Test 5: Override SOC for a time slot that has already started
    print("Test 5: Override SOC for a time slot that has already started")
    # Set midnight_utc to a known time and simulate being at 5:40am (340 minutes from midnight)
    # The current 30-minute slot is 5:30-6:00 (330-360 minutes)
    # We should be able to override the 5:30 slot even though it started 10 minutes ago
    
    # Set midnight to a known time (Dec 19, 2025 00:00 UTC)
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    # Set now_utc to 5:40am (10 minutes into the 5:30 slot)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=340)
    my_predbat.minutes_now = 340  # 5:40am (10 minutes into the 5:30 slot)
    my_predbat.manual_select("manual_soc", "05:30=75")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if not my_predbat.manual_soc_keep:
        print("ERROR: T5 Expected manual_soc_keep to have entries for override in current slot but got empty dict")
        failed = True
    else:
        # Verify the override is at the correct time: 5:30am = 330 minutes from midnight
        # The slot should span 330-359 minutes (30 minutes starting at 5:30)
        expected_start = 330
        expected_end = 359
        
        # Check if we have 75% values at the expected time range
        minutes_with_75 = sorted([k for k, v in my_predbat.manual_soc_keep.items() if v == 75.0])
        
        if not minutes_with_75:
            print("ERROR: T5 Expected manual_soc_keep to have SOC target of 75% but got {}".format(my_predbat.manual_soc_keep))
            failed = True
        elif minutes_with_75[0] != expected_start or minutes_with_75[-1] != expected_end:
            print("ERROR: T5 Expected override at minutes {}-{} but got {}-{}".format(
                expected_start, expected_end, minutes_with_75[0], minutes_with_75[-1]))
            failed = True
        elif len(minutes_with_75) != 30:
            print("ERROR: T5 Expected 30 minutes with 75% SOC but got {} minutes".format(len(minutes_with_75)))
            failed = True
        else:
            print("PASS: T5 Manual SOC target correctly overrides current slot at minutes {}-{} (started at 5:30, now 5:40)".format(
                minutes_with_75[0], minutes_with_75[-1]))

    # Test 6: When time moves past the slot end, it should be dropped from the list
    print("Test 6: When time moves past the slot end, it should be dropped from the list")
    # Keep the existing selection from Test 5 (05:30=75) but move time to 6:00am
    # The 5:30-6:00 slot has now completely finished, so it should be dropped
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=360)
    my_predbat.minutes_now = 360  # 6:00am (slot 5:30-6:00 has just ended)
    
    # Read back the manual_soc_keep - the 05:30 slot should now be gone as it's in the past
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if my_predbat.manual_soc_keep:
        # Check if any entries remain - they should not as the slot is now completely in the past
        print("ERROR: T6 Expected manual_soc_keep to be empty as slot 5:30-6:00 has passed (now 6:00) but got {}".format(my_predbat.manual_soc_keep))
        failed = True
    else:
        print("PASS: T6 Manual SOC slot 5:30-6:00 correctly dropped when time moved to 6:00am")

    # Clean up
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.all_active_keep = {}
    my_predbat.manual_select("manual_soc", "off")

    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)

    return failed
