# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
import pytz


def run_test_manual_times(my_predbat):
    """
    Test the manual_times function to ensure:
    1. Past/completed time slots are automatically removed
    2. Duplicate time slots are deduplicated
    """
    failed = False
    print("**** Testing manual_times function ****")

    # Setup: Create a test time at 14:00 on Tuesday Nov 26, 2024
    utc = pytz.UTC
    test_time = datetime(2024, 11, 26, 14, 0, 0, tzinfo=utc)
    
    # Override the current time in my_predbat
    my_predbat.now_utc = test_time
    my_predbat.midnight_utc = test_time.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = my_predbat.midnight_utc
    my_predbat.minutes_now = int((test_time - my_predbat.midnight_utc).total_seconds() / 60)  # 840 minutes (14:00)

    # Test 1: Past times should be filtered out
    print("Test 1: Past times should be filtered out")
    # Input: Past time at 04:00 (10 hours ago) and future time at 16:00 (2 hours from now)
    test_value = "+Tue 04:00,Tue 16:00"
    result = my_predbat.manual_times("manual_charge", new_value=test_value)
    
    # Expected: Only future time (16:00 = 960 minutes) should remain
    # Past time (04:00 = 240 minutes) should be removed
    if 240 in result:  # 04:00 should NOT be in the result
        print("ERROR: Test 1 failed - past time (04:00) was not filtered out. Result: {}".format(result))
        failed = True
    if 960 not in result:  # 16:00 should be in the result
        print("ERROR: Test 1 failed - future time (16:00) was incorrectly filtered. Result: {}".format(result))
        failed = True
    print("  Result: {}".format(result))

    # Test 2: Duplicate times should be deduplicated
    print("Test 2: Duplicate times should be deduplicated")
    # Input: Same time slot repeated multiple times (simulating automation repeatedly setting the same slot)
    test_value = "+Wed 08:00,Wed 08:30,Wed 08:00,Wed 08:30,Wed 08:00,Wed 08:30"
    result = my_predbat.manual_times("manual_export", new_value=test_value)
    
    # Expected: Only unique times should be in result
    # Wed 08:00 = tomorrow at 08:00 = 1680 minutes from midnight_utc
    # Wed 08:30 = tomorrow at 08:30 = 1710 minutes from midnight_utc
    wed_08_00 = 1680  # Wednesday 08:00
    wed_08_30 = 1710  # Wednesday 08:30
    
    if result.count(wed_08_00) > 1:
        print("ERROR: Test 2 failed - Wed 08:00 appears {} times (should be 1). Result: {}".format(result.count(wed_08_00), result))
        failed = True
    if result.count(wed_08_30) > 1:
        print("ERROR: Test 2 failed - Wed 08:30 appears {} times (should be 1). Result: {}".format(result.count(wed_08_30), result))
        failed = True
    print("  Result: {}".format(result))

    # Test 3: Combined test - past times AND duplicates
    print("Test 3: Combined test - filter past times and deduplicate")
    # Input: Past time (Mon 10:00), duplicate future times (Wed 15:00 appears twice)
    test_value = "+Mon 10:00,Wed 15:00,Wed 15:00,Wed 18:00"
    result = my_predbat.manual_times("manual_demand", new_value=test_value)
    
    # Mon 10:00 is yesterday (past) - should be filtered
    # Expected result should only contain Wed 15:00 (once) and Wed 18:00
    # Wed 15:00 = tomorrow at 15:00 = 1440 + 900 = 2340 minutes from midnight_utc
    # Wed 18:00 = tomorrow at 18:00 = 1440 + 1080 = 2520 minutes from midnight_utc
    wed_15_00 = 2340  # Wednesday 15:00
    wed_18_00 = 2520  # Wednesday 18:00
    
    if wed_15_00 in result and result.count(wed_15_00) > 1:
        print("ERROR: Test 3 failed - Wed 15:00 appears {} times (should be 1). Result: {}".format(result.count(wed_15_00), result))
        failed = True
    if wed_18_00 not in result:
        print("ERROR: Test 3 failed - Wed 18:00 should be in result. Result: {}".format(result))
        failed = True
    print("  Result: {}".format(result))

    # Test 4: Edge case - time at current time slot should be included (active slot)
    # But time in the past should be filtered
    print("Test 4: Edge case - current time slot vs past slots")
    # Current time is 14:00, so let's test with 13:00 (past) and 15:00 (future)
    test_value = "+Tue 13:00,Tue 15:00"
    result = my_predbat.manual_times("manual_charge", new_value=test_value)
    
    # 13:00 (780 minutes) is in the past - should be filtered
    # 15:00 (900 minutes) is future - should remain
    if 780 in result:  # Past time should be filtered
        print("ERROR: Test 4 failed - past time slot (13:00) was not filtered. Result: {}".format(result))
        failed = True
    if 900 not in result:
        print("ERROR: Test 4 failed - future time (15:00) was filtered. Result: {}".format(result))
        failed = True
    print("  Result: {}".format(result))

    # Test 5: Only past times - result should be empty
    print("Test 5: Only past times - should return empty list")
    test_value = "+Tue 04:00,Tue 10:00,Tue 13:30"
    result = my_predbat.manual_times("manual_freeze_charge", new_value=test_value)
    
    if len(result) > 0:
        print("ERROR: Test 5 failed - expected empty list for all past times. Result: {}".format(result))
        failed = True
    print("  Result: {}".format(result))

    print("**** manual_times tests completed ****")
    return failed
