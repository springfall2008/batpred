# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone, timedelta


def run_test_manual_times(my_predbat):
    """
    Test manual times feature
    """
    failed = False
    print("Test manual times")

    # Set up a known time context for consistent testing
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=0)  # Start at midnight
    my_predbat.minutes_now = 0

    # Reset manual_times to off
    my_predbat.manual_select("manual_demand", "off")

    # Test 1: Basic time parsing
    print("Test 1: Basic time parsing")
    my_predbat.manual_select("manual_demand", "05:30")

    # Read back the manual_demand_keep by calling manual_times
    manual_demand_keep = my_predbat.manual_times("manual_demand")

    if not manual_demand_keep:
        print("ERROR: T1 Expected manual_demand_keep to have entries but got empty list")
        failed = True
    else:
        # Check if we have the time in the list
        if 330 in manual_demand_keep:
            print("PASS: T1 Manual time set correctly at minute 330 (05:30)")
        else:
            print("ERROR: T1 Expected minute 330 in list but got {}".format(manual_demand_keep))
            failed = True

    # Test 2: Multiple time selections
    print("Test 2: Multiple time selections")
    my_predbat.manual_select("manual_demand", "05:30,07:00,08:30")

    manual_demand_keep = my_predbat.manual_times("manual_demand")

    expected_minutes = {330, 420, 510}
    actual_minutes = set(manual_demand_keep)

    if not expected_minutes.issubset(actual_minutes):
        print("ERROR: T2 Expected minutes {} but got {}".format(expected_minutes, actual_minutes))
        failed = True
    else:
        print("PASS: T2 Multiple manual times set correctly")

    # Test 3: Off clears times
    print("Test 3: Off clears times")
    my_predbat.manual_select("manual_demand", "off")

    manual_demand_keep = my_predbat.manual_times("manual_demand")

    if manual_demand_keep:
        print("ERROR: T3 Expected manual_demand_keep to be empty when off but got {}".format(manual_demand_keep))
        failed = True
    else:
        print("PASS: T3 Manual times cleared when set to off")

    # Test 4: Time within current slot that has already started
    print("Test 4: Time within current slot that has already started")
    # Set midnight to a known time and simulate being at 5:40am (340 minutes from midnight)
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    # Set now_utc to 5:40am (10 minutes into the 5:30 slot)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=340)
    my_predbat.minutes_now = 340  # 5:40am
    my_predbat.manual_select("manual_demand", "05:30")

    manual_demand_keep = my_predbat.manual_times("manual_demand")

    if not manual_demand_keep:
        print("ERROR: T4 Expected manual_demand_keep to have entries for current slot but got empty list")
        failed = True
    else:
        # Should have minute 330 (5:30am) as we're still in that slot
        if 330 in manual_demand_keep:
            print("PASS: T4 Manual time correctly includes current slot at minute 330 (started at 5:30, now 5:40)")
        else:
            print("ERROR: T4 Expected minute 330 in list but got {}".format(manual_demand_keep))
            failed = True

    # Test 5: When time moves past the slot, it should be dropped
    print("Test 5: When time moves past the slot, it should be dropped")
    # Keep the existing selection from Test 4 (05:30) but move time to 6:00am
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=360)
    my_predbat.minutes_now = 360  # 6:00am (slot 5:30-6:00 has just ended)

    manual_demand_keep = my_predbat.manual_times("manual_demand")

    if manual_demand_keep:
        print("ERROR: T5 Expected manual_demand_keep to be empty as slot 5:30 has passed (now 6:00) but got {}".format(manual_demand_keep))
        failed = True
    else:
        print("PASS: T5 Manual time slot 5:30 correctly dropped when time moved to 6:00am")

    # Test 6: Future times are kept
    print("Test 6: Future times are kept")
    # Set time to 5:00am and add times at 5:30, 6:00, 7:00
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=300)
    my_predbat.minutes_now = 300  # 5:00am
    my_predbat.manual_select("manual_demand", "05:30,06:00,07:00")

    manual_demand_keep = my_predbat.manual_times("manual_demand")

    expected_minutes = {330, 360, 420}
    actual_minutes = set(manual_demand_keep)

    if expected_minutes != actual_minutes:
        print("ERROR: T6 Expected minutes {} but got {}".format(expected_minutes, actual_minutes))
        failed = True
    else:
        print("PASS: T6 All future times kept correctly")

    # Test 7: Day of week support (if time format supports it)
    print("Test 7: Day of week format")
    # Set to Thursday Dec 19, 2025 at 10:00am
    my_predbat.midnight_utc = datetime(2025, 12, 18, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=600)  # 10:00am Thursday
    my_predbat.minutes_now = 600

    # Clear previous selection and set Friday 14:00
    my_predbat.manual_select("manual_demand", "off")

    print("Setting manual demand time to Friday 14:00 currently {}".format(my_predbat.now_utc.isoformat()))
    my_predbat.manual_select("manual_demand", "Fri 14:00")  # Friday 2pm
    print("Now calling keep retrieval for manual demand")

    manual_demand_keep = my_predbat.manual_times("manual_demand")

    # Friday 14:00: Dec 19 is Thursday, so Friday is +1 day = 1440 + 840 = 2280 minutes from midnight today
    expected_minute = 2280

    if expected_minute in manual_demand_keep:
        print("PASS: T7 Day of week format correctly scheduled for Friday 14:00 at minute {}".format(expected_minute))
    else:
        print("ERROR: T7 Expected minute {} for Friday 14:00 but got {}".format(expected_minute, manual_demand_keep))
        failed = True

    # Test 8: Bug #3075 - Selecting same time multiple times should not create duplicates
    print("Test 8: Bug #3075 - Selecting same time multiple times should not create duplicates")
    # Reset to midnight for clean test
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=0)  # Start at midnight
    my_predbat.minutes_now = 0

    # Clear any existing selection
    my_predbat.manual_select("manual_demand", "off")

    # Select the same time "07:30" three times
    print("  Selecting 07:30 (first time)")
    my_predbat.manual_select("manual_demand", "07:30")
    manual_demand_keep_1 = my_predbat.manual_times("manual_demand")

    print("  Selecting 07:30 (second time)")
    my_predbat.manual_select("manual_demand", "07:30")
    manual_demand_keep_2 = my_predbat.manual_times("manual_demand")

    print("  Selecting 07:30 (third time)")
    my_predbat.manual_select("manual_demand", "07:30")
    manual_demand_keep_3 = my_predbat.manual_times("manual_demand")

    # Check results
    expected_minute = 450  # 07:30 = 7*60 + 30 = 450 minutes from midnight

    # Count occurrences of the expected minute
    count_1 = manual_demand_keep_1.count(expected_minute)
    count_2 = manual_demand_keep_2.count(expected_minute)
    count_3 = manual_demand_keep_3.count(expected_minute)

    print("  After 1st select: {} occurrences of minute {} in {}".format(count_1, expected_minute, manual_demand_keep_1))
    print("  After 2nd select: {} occurrences of minute {} in {}".format(count_2, expected_minute, manual_demand_keep_2))
    print("  After 3rd select: {} occurrences of minute {} in {}".format(count_3, expected_minute, manual_demand_keep_3))

    if count_1 != 1:
        print("ERROR: T8 After first select, expected 1 occurrence of minute {} but got {}".format(expected_minute, count_1))
        failed = True
    elif count_2 != 1:
        print("ERROR: T8 After second select, expected 1 occurrence of minute {} but got {}".format(expected_minute, count_2))
        failed = True
    elif count_3 != 1:
        print("ERROR: T8 After third select, expected 1 occurrence of minute {} but got {}".format(expected_minute, count_3))
        failed = True
    else:
        print("PASS: T8 Selecting same time multiple times correctly maintains only one entry")

    # Test 9: Bug #3075 - Manual rates - same time with same rate should not create duplicates
    print("Test 9: Bug #3075 - Manual import rates - same time/same rate duplicates")
    # Reset to midnight for clean test
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=0)  # Start at midnight
    my_predbat.minutes_now = 0

    # Clear any existing selection
    my_predbat.manual_select("manual_import_rates", "off")

    # Select the same time "08:00=25.5" (same rate) three times
    print("  Selecting 08:00=25.5 (first time)")
    my_predbat.manual_select("manual_import_rates", "08:00=25.5")
    manual_import_keep_1 = my_predbat.manual_rates("manual_import_rates", default_rate=10.0)

    print("  Selecting 08:00=25.5 (second time)")
    my_predbat.manual_select("manual_import_rates", "08:00=25.5")
    manual_import_keep_2 = my_predbat.manual_rates("manual_import_rates", default_rate=10.0)

    print("  Selecting 08:00=25.5 (third time)")
    my_predbat.manual_select("manual_import_rates", "08:00=25.5")
    manual_import_keep_3 = my_predbat.manual_rates("manual_import_rates", default_rate=10.0)

    # Check results - minute 480 (08:00) should have rate 25.5
    expected_minute = 480  # 08:00 = 8*60 = 480 minutes from midnight

    # Count occurrences of the expected minute in the dictionary
    count_1 = 1 if expected_minute in manual_import_keep_1 else 0
    count_2 = 1 if expected_minute in manual_import_keep_2 else 0
    count_3 = 1 if expected_minute in manual_import_keep_3 else 0

    # Verify the rate is correct
    rate_1 = manual_import_keep_1.get(expected_minute, None)
    rate_2 = manual_import_keep_2.get(expected_minute, None)
    rate_3 = manual_import_keep_3.get(expected_minute, None)

    print("  After 1st select: minute {} present={}, rate={}".format(expected_minute, count_1 == 1, rate_1))
    print("  After 2nd select: minute {} present={}, rate={}".format(expected_minute, count_2 == 1, rate_2))
    print("  After 3rd select: minute {} present={}, rate={}".format(expected_minute, count_3 == 1, rate_3))

    if count_1 != 1 or rate_1 != 25.5:
        print("ERROR: T9 After first select, expected minute {} with rate 25.5 but got rate={}".format(expected_minute, rate_1))
        failed = True
    elif count_2 != 1 or rate_2 != 25.5:
        print("ERROR: T9 After second select, expected minute {} with rate 25.5 but got rate={}".format(expected_minute, rate_2))
        failed = True
    elif count_3 != 1 or rate_3 != 25.5:
        print("ERROR: T9 After third select, expected minute {} with rate 25.5 but got rate={}".format(expected_minute, rate_3))
        failed = True
    else:
        print("PASS: T9 Selecting same time/rate multiple times correctly maintains only one entry")

    # Test 10: Manual rates - same time with different rates should update (not duplicate)
    print("Test 10: Manual import rates - same time with different rates should update")

    # Clear any existing selection
    my_predbat.manual_select("manual_import_rates", "off")

    # Select 09:00 with rate 10.0
    print("  Selecting 09:00=10.0")
    my_predbat.manual_select("manual_import_rates", "09:00=10.0")
    manual_import_keep_1 = my_predbat.manual_rates("manual_import_rates", default_rate=5.0)

    # Select 09:00 again with rate 20.0 (should update, not add)
    print("  Selecting 09:00=20.0 (updating rate)")
    my_predbat.manual_select("manual_import_rates", "09:00=20.0")
    manual_import_keep_2 = my_predbat.manual_rates("manual_import_rates", default_rate=5.0)

    # Select 09:00 again with rate 30.0 (should update again)
    print("  Selecting 09:00=30.0 (updating rate again)")
    my_predbat.manual_select("manual_import_rates", "09:00=30.0")
    manual_import_keep_3 = my_predbat.manual_rates("manual_import_rates", default_rate=5.0)

    expected_minute = 540  # 09:00 = 9*60 = 540 minutes from midnight

    rate_1 = manual_import_keep_1.get(expected_minute, None)
    rate_2 = manual_import_keep_2.get(expected_minute, None)
    rate_3 = manual_import_keep_3.get(expected_minute, None)

    # Count total entries to ensure no duplicates
    total_entries_1 = len([k for k in manual_import_keep_1.keys() if k >= expected_minute and k < expected_minute + 30])
    total_entries_2 = len([k for k in manual_import_keep_2.keys() if k >= expected_minute and k < expected_minute + 30])
    total_entries_3 = len([k for k in manual_import_keep_3.keys() if k >= expected_minute and k < expected_minute + 30])

    print("  After selecting 09:00=10.0: rate={}, entries in slot={}".format(rate_1, total_entries_1))
    print("  After selecting 09:00=20.0: rate={}, entries in slot={}".format(rate_2, total_entries_2))
    print("  After selecting 09:00=30.0: rate={}, entries in slot={}".format(rate_3, total_entries_3))

    if rate_1 != 10.0:
        print("ERROR: T10 After first select, expected rate 10.0 but got {}".format(rate_1))
        failed = True
    elif rate_2 != 20.0:
        print("ERROR: T10 After second select with different rate, expected rate 20.0 but got {}".format(rate_2))
        failed = True
    elif rate_3 != 30.0:
        print("ERROR: T10 After third select with different rate, expected rate 30.0 but got {}".format(rate_3))
        failed = True
    elif total_entries_1 != 30 or total_entries_2 != 30 or total_entries_3 != 30:
        print("ERROR: T10 Expected 30 minute entries in each slot but got {}, {}, {}".format(total_entries_1, total_entries_2, total_entries_3))
        failed = True
    else:
        print("PASS: T10 Selecting same time with different rates correctly updates the rate")

    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)

    # Clean up
    my_predbat.manual_select("manual_demand", "off")
    my_predbat.manual_select("manual_import_rates", "off")
    return failed
