# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
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

    # Restore time context to current time
    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc =  my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)

    # Clean up
    my_predbat.manual_select("manual_demand", "off")
    return failed
